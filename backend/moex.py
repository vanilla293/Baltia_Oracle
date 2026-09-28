"""ОРАКУЛ // ПИФИЯ — резерв через MOEX ISS (без токена, с лагом ~15 мин).

Используется, если Tinkoff-токен не задан или не вернул свечи/цену:
график и базовый прогноз всё равно работают (стакан/лента недоступны).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger("pythia.moex")

# engine/market под класс актива
_ENGINE = {
    "share": ("stock", "shares"),
    "futures": ("futures", "forts"),
    "currency": ("currency", "selt"),
    "index": ("stock", "index"),
}
# MOEX ISS умеет только 1/10/60/24/7/31 — берём ближайший МЕЛЬЧЕ и честно
# склеиваем в запрошенный (_BUCKET_S), а не роняем в дневки
_IV = {"1m": 1, "5m": 1, "10m": 10, "15m": 10, "30m": 10,
       "1h": 60, "4h": 60, "1d": 24, "1w": 7, "1M": 31}
_BUCKET_S = {"5m": 300, "15m": 900, "30m": 1800, "4h": 14400}
_MSK_UTC_H = 3          # ISS отдаёт время торгов Москвой (UTC+3, без летнего)

_fut_map: dict = {"ts": 0.0, "by_asset": {}}
_FUT_TTL = 3600.0


# Наш код → ASSETCODE FORTS (у биржи свои имена: MX≠MIX, SBER≠SBRF и т.д.)
_ASSET_ALIAS = {
    "MX": "MIX", "MM": "MXI", "RI": "RTS",
    "GD": "GOLD", "SV": "SILV",
    "SBER": "SBRF", "SBERP": "SBPR", "GAZP": "GAZR",
    "NVTK": "NOTK", "MTSS": "MTSI",
}


_fut_lock = asyncio.Lock()


async def resolve_futures(base: str) -> str | None:
    """Базовый код фьючерса (BR/NG/SI/...) → ближайший активный контракт на FORTS.
    Без этого фолбэк MOEX для курируемых фьючерсов был мёртв (SECID 'BR' не существует).
    Коды с чужими именами у биржи (MX→MIX, SBER→SBRF...) идут через _ASSET_ALIAS."""
    import time as _t
    base = (base or "").upper().strip()
    if not base:
        return None
    now = _t.time()
    if not _fut_map["by_asset"] or now - _fut_map["ts"] > _FUT_TTL:
        # single-flight: без замка несколько фьючерсов (или тики цены каждые 3с)
        # на холодном/протухшем кэше разом запускали полную ~12с загрузку FORTS;
        # при недоступном ISS ts не бился и КАЖДЫЙ вызов повторял её заново.
        async with _fut_lock:
            now = _t.time()
            if not _fut_map["by_asset"] or now - _fut_map["ts"] > _FUT_TTL:  # double-check
                data = await _iss("engines/futures/markets/forts/securities",
                                  {"iss.only": "securities",
                                   "securities.columns": "SECID,ASSETCODE,LASTTRADEDATE"})
                by: dict[str, list] = {}
                for r in _rows((data or {}).get("securities")):
                    ac = (r.get("ASSETCODE") or "").upper()
                    sid = (r.get("SECID") or "").upper()
                    ltd = r.get("LASTTRADEDATE") or ""
                    if ac and sid:
                        by.setdefault(ac, []).append((ltd, sid))
                if by:
                    _fut_map["by_asset"] = by
                    _fut_map["ts"] = now
                else:
                    # ISS не отдал: не долбим его каждым вызовом — короткий кулдаун.
                    # Старую карту (если была) продолжаем обслуживать.
                    _fut_map["ts"] = now - _FUT_TTL + 60
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    keys = [base]
    ali = _ASSET_ALIAS.get(base)
    if ali:
        keys.append(ali)
    rows: list = []
    for k in keys:
        rows = _fut_map["by_asset"].get(k, [])
        if rows:
            break
    cands = [(d, s) for d, s in rows if d >= today]
    cands.sort()
    return cands[0][1] if cands else None


# Общий пул соединений к MOEX ISS: без него каждый вызов свечей/цены открывал
# новое TLS-соединение (фолбэк-тикер цены бил сюда каждые 3с при отсутствии токена).
_http: httpx.AsyncClient | None = None
_http_lock = asyncio.Lock()


async def _client() -> httpx.AsyncClient:
    global _http
    h = _http
    if h is not None and not h.is_closed:
        return h
    async with _http_lock:
        if _http is None or _http.is_closed:
            _http = httpx.AsyncClient(
                timeout=httpx.Timeout(12.0), follow_redirects=True,
                limits=httpx.Limits(max_keepalive_connections=10,
                                    max_connections=20, keepalive_expiry=60.0))
        return _http


async def aclose() -> None:
    global _http
    h, _http = _http, None
    if h is not None and not h.is_closed:
        try:
            await h.aclose()
        except Exception:
            pass


async def _iss(path: str, params: dict) -> dict | None:
    url = f"https://iss.moex.com/iss/{path}.json"
    try:
        cl = await _client()
        r = await cl.get(url, params=params)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning("ISS %s failed: %s", path, str(e)[:100])
        return None


def _rows(block: dict | None) -> list[dict]:
    if not block:
        return []
    cols = block.get("columns", [])
    return [dict(zip(cols, row)) for row in block.get("data", [])]


async def _candle_pages(engine: str, market: str, sec: str, frm: str,
                        iv: int) -> list[dict]:
    """ISS отдаёт максимум 500 свечей на страницу — С НАЧАЛА диапазона.
    Без листания минутный график замерзал на позавчера (и «обновлялся»
    только видимостью). Листаем через start до конца, потолок — защита."""
    rows: list[dict] = []
    start = 0
    for _ in range(24):                              # ≤ 12 000 строк
        data = await _iss(
            f"engines/{engine}/markets/{market}/securities/{sec}/candles",
            {"from": frm, "interval": iv, "iss.meta": "off", "start": start})
        page = _rows((data or {}).get("candles"))
        rows += page
        if len(page) < 500:
            break
        start += len(page)
    return rows


def _resample(rows: list[dict], bucket_s: int) -> list[dict]:
    """Склейка мелких свечей в запрошенный интервал (5м из 1м, 15/30м из 10м,
    4ч из 1ч): O — первая, C — последняя, H/L — крайние, V — сумма."""
    out: list[dict] = []
    last_b = None
    for r in rows:
        try:
            t = datetime.fromisoformat(r["t"].replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError, KeyError):
            continue
        b = int(t // bucket_s) * bucket_s
        if last_b is not None and b < last_b:
            continue        # строка из уже закрытого бакета — дубль времени запрещён
        if last_b == b and out:
            a = out[-1]
            a["h"] = max(a["h"], r["h"])
            a["l"] = min(a["l"], r["l"])
            a["c"] = r["c"]
            a["v"] += r["v"]
        else:
            out.append({"t": datetime.fromtimestamp(b, timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"],
                        "v": r["v"], "complete": True})
            last_b = b
    if out:
        out[-1]["complete"] = False     # бакет ещё идёт — честно
    return out


async def candles(ticker: str, asset_class: str, interval: str,
                  days_back: int) -> list[dict]:
    engine, market = _ENGINE.get(asset_class, ("stock", "shares"))
    iv = _IV.get(interval, 24)
    frm = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    rows = await _candle_pages(engine, market, ticker, frm, iv)
    if not rows and asset_class == "futures":
        sec = await resolve_futures(ticker)          # базовый код → живой контракт
        if sec and sec != ticker.upper():
            rows = await _candle_pages(engine, market, sec, frm, iv)
    out = []
    for r in rows:
        try:
            # Москва → честный UTC: ось времени едина с Тинькофф и эфиром
            # (раньше московское время подписывалось «Z» как есть — график
            # «жил в будущем» на 3 часа и расходился с реальным временем)
            t_utc = (datetime.fromisoformat((r.get("begin") or "").replace(" ", "T"))
                     - timedelta(hours=_MSK_UTC_H))
            out.append({
                "t": t_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "o": float(r.get("open") or 0), "h": float(r.get("high") or 0),
                "l": float(r.get("low") or 0), "c": float(r.get("close") or 0),
                "v": int(float(r.get("volume") or 0)), "complete": True,
            })
        except (ValueError, TypeError):
            continue
    b = _BUCKET_S.get(interval)
    if b and out:
        out = _resample(out, b)
    return out


_issue_cache: dict = {}


async def issue_date(ticker: str) -> str | None:
    """Дата выпуска бумаги (description.ISSUEDATE карточки ISS) — честный
    генезис-якорь для тикеров вне реестра эфира. Кэш на процесс; нет даты —
    None (эфир честно уйдёт в мунданный фолбэк)."""
    key = (ticker or "").upper()
    if key in _issue_cache:
        return _issue_cache[key]
    data = await _iss(f"securities/{ticker}",
                      {"iss.meta": "off", "iss.only": "description"})
    val = None
    for r in _rows((data or {}).get("description")):
        if str(r.get("name", "")).upper() == "ISSUEDATE":
            v = str(r.get("value") or "").strip()
            if v:
                val = v
            break
    _issue_cache[key] = val
    return val


async def last_price(ticker: str, asset_class: str,
                     _resolved: bool = False) -> dict | None:
    engine, market = _ENGINE.get(asset_class, ("stock", "shares"))
    data = await _iss(
        f"engines/{engine}/markets/{market}/securities/{ticker}",
        {"iss.meta": "off",
         "iss.only": "marketdata",
         "marketdata.columns": "SECID,LAST,MARKETPRICE,LCLOSEPRICE,LCURRENTPRICE,"
                               "LASTCHANGEPRC,VALTODAY,VOLTODAY,BID,OFFER,UPDATETIME"},
    )
    rows = _rows((data or {}).get("marketdata"))
    for r in rows:
        # вечер/выходной: LAST пуст — берём последнюю известную цену,
        # раньше терминал показывал «н/д» до открытия торгов
        last = r.get("LAST") or r.get("LCURRENTPRICE") or r.get("MARKETPRICE") \
            or r.get("LCLOSEPRICE")
        stale = not r.get("LAST")
        if last:
            return {
                "price": float(last),
                "stale": stale or None,
                "change_pct": r.get("LASTCHANGEPRC"),
                "value_today": r.get("VALTODAY"),
                "volume_today": r.get("VOLTODAY"),
                "bid": r.get("BID"), "offer": r.get("OFFER"),
                "ts": datetime.now(timezone.utc).timestamp(),
                "quote_time": r.get("UPDATETIME"),
                "source": "moex",
            }
    if asset_class == "futures" and not _resolved:
        sec = await resolve_futures(ticker)
        if sec and sec != ticker.upper():
            return await last_price(sec, asset_class, _resolved=True)
    return None
