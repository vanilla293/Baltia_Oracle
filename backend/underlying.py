# -*- coding: utf-8 -*-
"""ПЕРВОИСТОЧНИК: базовый актив фьючерса.

Фьючерс — производная, «игра». Базовый актив — «первород»: акция, индекс,
валюта. Модуль находит первоисточник, забирает его спот-цену и дневную
структуру, считает БАЗИС (контанго/бэквордация, в % и % годовых) и Вайкоффа
по споту — чтобы ИИ и оператор видели расхождение производной с основой.

Источники по типу базового актива:
  акция    → Tinkoff (share) или MOEX stock — цена + дневные свечи → Вайкофф
  индекс   → (v5.3) индексы MOEX ISS не используются: базовый актив фьючерса на индекс — сам фьючерс (Tinkoff)
  валюта   → CNY: MOEX CETS (CNYRUB_TOM); USD/EUR: официальный курс ЦБ РФ
  сырьё    → биржевого спота в свободном доступе нет — секция пропускается
"""
from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx

from . import moex, tinkoff, wyckoff

logger = logging.getLogger("pythia.underlying")

# ASSETCODE (FORTS) → как искать первоисточник
_INDEX_MAP = {"MIX": "IMOEX", "MXI": "IMOEX", "MM": "IMOEX", "MX": "IMOEX",
              "RTS": "RTSI", "RI": "RTSI", "RTSI": "RTSI"}
# ASSETCODE FORTS → биржевой тикер акции (у биржи свои имена)
_STOCK_ALIAS = {"SBRF": "SBER", "SBPR": "SBERP", "GAZR": "GAZP",
                "NOTK": "NVTK", "MTSI": "MTSS"}
# у валютных фьючерсов Tinkoff basicAsset приходит как "USD/RUB" — учим карты
# обеим формам (код контракта И имя пары)
_CBR_CCY = {"SI": "USD", "EU": "EUR", "ED": None,
            "USD/RUB": "USD", "EUR/RUB": "EUR", "USDRUB": "USD", "EURRUB": "EUR"}
_MOEX_CCY = {"CNY": "CNYRUB_TOM", "CR": "CNYRUB_TOM",
             "CNY/RUB": "CNYRUB_TOM", "CNYRUB": "CNYRUB_TOM"}
# сырьё/товар: биржевого рублёвого спота в свободном доступе нет — пропускаем
_COMMODITY = {"BR", "NG", "GD", "GLD", "SV", "SLV", "PLT", "PLD", "CU", "AL",
              "GOLD", "SILV", "BRENT", "NGAS", "WTI", "SUGAR", "WHEAT", "CORN"}
_LOT_GRID = (1, 2, 5, 10, 100, 1000, 10000, 100000)

_cache: dict[str, tuple[float, dict | None]] = {}
_CACHE_TTL = 60.0


def _fit_lot(fut_price: float, spot: float) -> int | None:
    """Множитель контракта, если биржа его не сообщила: базис мал (<2%),
    поэтому отношение цен ложится на решётку лотов почти точно."""
    if not fut_price or not spot:
        return None
    r = fut_price / spot
    # базис у ликвидных контрактов невелик: истинный множитель даёт |отклонение|<8%
    # ступени решётки в 10 раз: порог 20% однозначен и пропускает честную
    # дивидендную бэквордацию (фьюч ниже спота на размер дивиденда, до ~15%)
    best, err = None, 0.20
    for lot in _LOT_GRID:
        e = abs(r / lot - 1.0)
        if e < err:
            best, err = lot, e
    return best


import asyncio as _asyncio
_http: httpx.AsyncClient | None = None
_http_lock = _asyncio.Lock()


async def _client() -> httpx.AsyncClient:
    global _http
    h = _http
    if h is not None and not h.is_closed:
        return h
    async with _http_lock:
        if _http is None or _http.is_closed:
            _http = httpx.AsyncClient(
                timeout=httpx.Timeout(8.0),
                limits=httpx.Limits(max_keepalive_connections=2,
                                    max_connections=4, keepalive_expiry=60.0))
        return _http


async def aclose() -> None:
    global _http
    h, _http = _http, None
    if h is not None and not h.is_closed:
        try:
            await h.aclose()
        except Exception:
            pass


async def _cbr_rate(ccy: str) -> float | None:
    """Официальный курс ЦБ РФ (дневной фикс) — биржевого спота USD/EUR нет."""
    try:
        cl = await _client()
        r = await cl.get("https://www.cbr.ru/scripts/XML_daily.asp")
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for v in root.iter("Valute"):
            if (v.findtext("CharCode") or "").upper() == ccy:
                nom = float((v.findtext("Nominal") or "1").replace(",", "."))
                val = float((v.findtext("Value") or "0").replace(",", "."))
                return val / max(nom, 1)
    except Exception as e:
        logger.warning("ЦБ курс %s: %s", ccy, str(e)[:80])
    return None


async def _moex_currency(secid: str) -> float | None:
    try:
        d = await moex._iss(  # noqa: SLF001
            f"engines/currency/markets/selt/boards/CETS/securities/{secid}",
            {"iss.only": "marketdata", "marketdata.columns": "SECID,LAST,MARKETPRICE"})
        for r in moex._rows((d or {}).get("marketdata")):  # noqa: SLF001
            v = r.get("LAST") or r.get("MARKETPRICE")
            if v is not None:
                return float(v)
    except Exception:
        pass
    return None


async def _stock_spot(base: str) -> tuple[float | None, list[dict], str]:
    """Спот акции: Tinkoff если есть токен, иначе MOEX."""
    name = base
    price, candles = None, []
    if tinkoff.enabled():
        try:
            inst = await tinkoff.resolve(base, "share")
            if inst:
                name = inst.get("name") or base
                q = await tinkoff.last_price(inst.get("uid"))
                if q and q.get("price") is not None:
                    price = q["price"]
                candles = await tinkoff.candles(inst.get("uid"), "1d", 300) or []
        except Exception as e:
            logger.warning("underlying tinkoff %s: %s", base, str(e)[:80])
    if price is None:
        try:
            q = await moex.last_price(base, "share")
            if q and q.get("price") is not None:
                price = q["price"]
        except Exception:
            pass
    if not candles:
        try:
            candles = await moex.candles(base, "share", "1d", 210) or []
        except Exception:
            pass
    return price, candles, name


def _days_to_exp(expiration: str | None) -> int | None:
    if not expiration:
        return None
    try:
        exp = datetime.fromisoformat(str(expiration).replace("Z", "+00:00"))
        return max(0, (exp - datetime.now(timezone.utc)).days)
    except Exception:
        return None


async def collect(dossier: dict, curated_code: str) -> dict | None:
    """Собрать блок первоисточника для фьючерса. None — если спота нет."""
    inst = dossier.get("instrument") or {}
    fut_price = (dossier.get("price") or {}).get("price")
    base_raw = (inst.get("basic_asset") or dossier.get("basic_asset") or "").upper().strip()
    curated = (curated_code or "").upper().strip()
    # какой из кандидатов знают карты: базовый актив Tinkoff может прийти как
    # "USD/RUB", а карты знают код "SI" — проверяем оба, иначе валютный фьючерс
    # уходил в поиск «акции USD/RUB» на MOEX (404) и блок молча пропадал
    base = next((c for c in (base_raw, curated) if c and (
        c in _INDEX_MAP or c in _MOEX_CCY or c in _CBR_CCY or c in _COMMODITY)),
        None) or base_raw or curated
    if not base or not fut_price:
        return None
    now = time.time()
    hit = _cache.get(base)
    if hit and now - hit[0] < _CACHE_TTL:
        if hit[1] is None:      # негативный кэш: спота нет — не долбим источники
            return None         # каждые 3 секунды живым тикером цены
        cached = dict(hit[1])
    else:
        cached = None

    kind, spot, candles, name, note = None, None, [], base, ""
    if cached:
        kind, spot, candles, name, note = (cached["kind"], cached["spot"],
                                           cached.get("_candles", []),
                                           cached["name"], cached.get("note", ""))
    elif base in _INDEX_MAP:
        # v5.3: индексы MOEX ISS не используются (данные отстают — воля владельца): базовый актив
        # фьючерса на индекс — сам фьючерс, его цена и свечи идут через Tinkoff в досье; спота ISS нет
        _cache[base] = (now, None)
        return None
    elif base in _MOEX_CCY:
        kind, secid = "currency", _MOEX_CCY[base]
        spot = await _moex_currency(secid)
        name = secid
    elif base in _CBR_CCY:
        ccy = _CBR_CCY[base]
        if not ccy:
            return None
        kind, name = "currency", f"{ccy}/RUB (курс ЦБ)"
        spot = await _cbr_rate(ccy)
        note = "биржевого спота нет — сравнение с официальным курсом ЦБ (дневной фикс)"
    elif base in _COMMODITY:
        _cache[base] = (now, None)
        return None
    else:
        # предполагаем акцию; если не нашлась — это сырьё/прочее, спот недоступен
        if "/" in base:              # "XXX/YYY" — валютная пара, не тикер акции
            _cache[base] = (now, None)
            return None
        kind = "security"
        spot, candles, name = await _stock_spot(_STOCK_ALIAS.get(base, base))
        if spot is None:
            _cache[base] = (now, None)
            return None

    if spot is None:
        _cache[base] = (now, None)
        return None
    _cache[base] = (now, {"kind": kind, "spot": spot, "name": name,
                          "note": note, "_candles": candles})

    lot = inst.get("basic_asset_size") or dossier.get("basic_asset_size")
    try:
        lot = int(float(lot)) if lot else None
    except Exception:
        lot = None

    def _basis_for(l: int) -> float:
        return (float(fut_price) / l / float(spot) - 1.0) * 100.0

    # biржевой basicAssetSize — размер КОНТРАКТА, но не всегда множитель
    # КОТИРОВКИ: CNY-фьючерс — контракт 1000 юаней при котировке за 1 юань,
    # MIX/RTS — котировка в пунктах = индекс × 100. Если с заявленным лотом
    # базис абсурден — подбираем множитель по решётке сами (раньше секция
    # первоисточника у CR/MX/RI из-за этого молча пропадала).
    if not lot or abs(_basis_for(lot)) > 60.0:
        lot = _fit_lot(float(fut_price), float(spot)) or lot or 1

    per_unit = float(fut_price) / lot
    basis_pct = _basis_for(lot)
    if abs(basis_pct) > 60.0:   # множитель определить не удалось — лучше молчать, чем врать
        logger.warning("первоисточник %s: базис %.1f%% абсурден (lot=%s) — секция пропущена",
                       base, basis_pct, lot)
        _cache[base] = (now, None)
        return None
    dte = _days_to_exp(inst.get("expiration") or dossier.get("expiration"))
    annual = basis_pct * 365.0 / dte if dte and dte > 3 else None

    out = {
        "base": base, "name": name, "kind": kind, "spot": round(float(spot), 6),
        "lot": lot, "fut_per_unit": round(per_unit, 6),
        "basis_pct": round(basis_pct, 3),
        "basis_annual_pct": round(annual, 2) if annual is not None else None,
        "days_to_exp": dte,
        "state": "контанго" if basis_pct > 0.03 else ("бэквордация" if basis_pct < -0.03 else "паритет"),
        "note": note,
    }
    # дивиденд + консенсус первоисточника: близкая отсечка = справедливая
    # бэквордация фьючерса; консенсус базы = настроение умных денег по основе
    if kind == "security" and tinkoff.enabled():
        try:
            base_inst = await tinkoff.resolve(_STOCK_ALIAS.get(base, base), "share")
            if base_inst and base_inst.get("uid"):
                import asyncio as _aio
                dv, cons = await _aio.gather(
                    tinkoff.next_dividend(base_inst["uid"]),
                    tinkoff.consensus(base_inst["uid"]),
                    return_exceptions=True)
                if isinstance(dv, dict) and dv.get("days_to_record") is not None:
                    out["dividend"] = dv
                    # отсечка ДО экспирации → базис содержит дивиденд:
                    # считаем ЧИСТЫЙ базис сами, а не просим ИИ прикидывать в уме
                    dtr = dv.get("days_to_record")
                    if dte is not None and dtr is not None and dtr <= dte:
                        out["dividend_before_expiry"] = True
                        y = dv.get("yield_pct")
                        if y:
                            out["basis_ex_div_pct"] = round(basis_pct + float(y), 3)
                    elif dte is not None:
                        out["dividend_before_expiry"] = False
                if isinstance(cons, dict) and cons:
                    out["consensus"] = cons
        except Exception as e:
            logger.warning("корпфон первоисточника %s: %s", base, str(e)[:80])

    if candles:
        try:
            wy = wyckoff.analyze(candles, "1d")
            if wy:
                out["wyckoff"] = {k: wy.get(k) for k in
                                  ("read", "phase", "bias", "box_low", "box_high")}
                out["wyckoff_line"] = wyckoff.summary_line(wy)
        except Exception as e:
            logger.warning("wyckoff первоисточника %s: %s", base, str(e)[:80])
    return out


def divergence(under: dict, fut_wy: dict | None) -> str:
    """Расхождение структур: спот vs фьючерс — тревожный флаг для ИИ."""
    uw = (under or {}).get("wyckoff") or {}
    fw = ((fut_wy or {}).get("daily") or {})
    if not uw or not fw:
        return ""
    if uw.get("read") and fw.get("read") and uw["read"] != fw["read"]:
        return (f"⚠ РАСХОЖДЕНИЕ СТРУКТУР: первоисточник в «{uw['read']}» "
                f"(bias {uw.get('bias')}), фьючерс в «{fw['read']}» "
                f"(bias {fw.get('bias')}). Производная оторвалась от основы — "
                f"проверь, кто врёт: обычно прав первоисточник.")
    db = (fw.get("bias") or 0) - (uw.get("bias") or 0)
    if abs(db) >= 35:
        return (f"⚠ bias фьючерса и первоисточника разошлись на {db:+d} "
                f"пунктов — игра в производной опережает основу.")
    return ""


def render_for_ai(under: dict | None, fut_wy: dict | None = None) -> str:
    if not under:
        return ""
    L = [f"Базовый актив: {under['base']} — {under['name']} "
         f"({'акция' if under['kind']=='security' else ('индекс' if under['kind']=='index' else 'валюта')})",
         f"Спот: {under['spot']} · фьючерс на единицу базы: {under['fut_per_unit']} "
         f"(множитель контракта {under['lot']})",
         f"БАЗИС: {under['basis_pct']:+.2f}% — {under['state'].upper()}"
         + (f" · {under['basis_annual_pct']:+.1f}% годовых при {under['days_to_exp']} дн. до экспирации"
            if under.get("basis_annual_pct") is not None else "")]
    if under.get("note"):
        L.append(f"Примечание: {under['note']}")
    dv = under.get("dividend")
    if dv:
        line = (f"Дивиденд первоисточника: {dv.get('value')} {dv.get('currency','')} "
                f"(доходность {dv.get('yield_pct')}%), отсечка {str(dv.get('record_date'))[:10]} "
                f"— через {dv.get('days_to_record')} дн.")
        if under.get("dividend_before_expiry"):
            line += " ОТСЕЧКА ДО ЭКСПИРАЦИИ: бэквордация фьючерса ≈ дивиденду, слабостью НЕ является."
            if under.get("basis_ex_div_pct") is not None:
                line += (f" ЧИСТЫЙ базис (базис + дивиденд): {under['basis_ex_div_pct']:+.2f}% — "
                         f"вот реальная премия/дисконт производной без дивидендного сноса.")
        elif under.get("dividend_before_expiry") is False:
            line += " Отсечка ПОСЛЕ экспирации — на базис этого контракта дивиденд не давит."
        L.append(line)
    cons = under.get("consensus")
    if cons:
        L.append(f"Консенсус по первоисточнику: {cons.get('reco')} · таргет {cons.get('target')} "
                 f"(апсайд {cons.get('upside_pct')}%) · инвестдомов: {cons.get('houses_count')} — "
                 f"настроение умных денег по ОСНОВЕ, производная его отыгрывает.")
    if under.get("wyckoff_line"):
        L.append(f"Вайкофф ПЕРВОИСТОЧНИКА (D1): {under['wyckoff_line']}")
    div = divergence(under, fut_wy)
    if div:
        L.append(div)
    L.append("Трактовка: растущее контанго = деньги/ожидания дороже, аномальная "
             "бэквордация на акции = ждут дивиденд или продавливают производную; "
             "объём и структура первоисточника ведут — фьючерс догоняет.")
    return "\n".join(L)


def summary_line(under: dict | None) -> str:
    if not under:
        return ""
    s = f"⚓ {under['base']} {under['spot']} · базис {under['basis_pct']:+.2f}% ({under['state']})"
    if (under.get("wyckoff") or {}).get("read"):
        s += f" · спот: {under['wyckoff']['read']}"
    return s
