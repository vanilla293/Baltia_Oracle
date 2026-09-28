"""ОРАКУЛ // ПИФИЯ — конвейер анализа.

Поток (как в ТЗ):
  0. собрать МАКСИМУМ данных по каждому инструменту (Tinkoff, фолбэк MOEX);
     выгрузить новости за 2 суток; подтянуть погоду где релевантно;
  1. РАСПРЕДЕЛИТЕЛЬ — раскидать новости по инструментам + выжимки;
  2. ФОН — общий рыночный фон (вторая ИИ);
  3. на каждый инструмент: АНАЛИТИК (думает, без формата, стрим) →
     КРИТИК (антитезис) → СИНТЕЗ (огромный вердикт, стрим) →
     ШИФРОВКА (только тут — JSON прогноза).

Все стадии шлют события через on_event (для WebSocket): размышление и текст
стримятся в реальном времени.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Awaitable, Callable

from . import (ai, aether, astro, config, instruments, maya, memory, moex, news,
               reactor_sky, underlying, prompts,
               tinkoff, weather as weather_mod, wyckoff)
from .tinkoff import daily_metrics, intraday_metrics

logger = logging.getLogger("pythia.pipeline")

Emit = Callable[[dict], Awaitable[None]]

# ── Реестр ЖИВЫХ разборов: тикер → {stage, started}. Единый источник истины
#    для восстановления фронта после смерти сокета/перезагрузки страницы.
RUNNING: dict[str, dict] = {}


_LIVE_CAP = 300_000        # потолок живого буфера на поле (символов)


_BUF_FIELDS = ("think", "analyst", "critic", "verdict")


def _mark(tk: str, stage: str) -> None:
    r = RUNNING.get(tk)
    if r is None:
        RUNNING[tk] = {"stage": stage, "started": time.time(), "seq": 0,
                       "buf": {f: [] for f in _BUF_FIELDS},
                       "buflen": {f: 0 for f in _BUF_FIELDS}}
    else:
        r["stage"] = stage


def _mirror(tk: str, field: str, delta: str) -> int:
    """Копим живой стрим в реестре и выдаём порядковый номер дельты: зритель,
    подключившийся посередине, забирает снимок через /api/live и дальше
    принимает только дельты с seq больше снимка — без дублей.

    Буфер — СПИСОК чанков (не строка): раньше на КАЖДУЮ дельту мы делали
    (буфер + delta)[-CAP:], то есть копировали весь буфер (до 300К символов)
    заново тысячи раз за стадию — O(n²) прямо в event loop, тормозя стрим всех
    объектов и живой тикер цены. Теперь append O(1), обрезка с начала — амортиз."""
    r = RUNNING.get(tk)
    if not r:
        return 0
    r["seq"] = r.get("seq", 0) + 1          # seq растёт ВСЕГДА, даже при обрезке
    parts = r.setdefault("buf", {}).setdefault(field, [])
    lens = r.setdefault("buflen", {})
    parts.append(delta)
    lens[field] = lens.get(field, 0) + len(delta)
    while lens[field] > _LIVE_CAP and len(parts) > 1:
        lens[field] -= len(parts.pop(0))
    return r["seq"]


def _buf_str(b: dict, field: str) -> str:
    v = b.get(field, "")
    return "".join(v) if isinstance(v, list) else (v or "")


def live_snapshot(tk: str) -> dict | None:
    """Снимок идущего разбора для только что подключившегося зрителя."""
    r = RUNNING.get((tk or "").upper())
    if not r:
        return None
    b = r.get("buf") or {}
    return {"code": (tk or "").upper(), "stage": r.get("stage"),
            "seq": r.get("seq", 0), "elapsed": round(time.time() - r["started"], 1),
            "think": _buf_str(b, "think"), "analyst": _buf_str(b, "analyst"),
            "critic": _buf_str(b, "critic"), "verdict": _buf_str(b, "verdict")}


_RUN_STALE_SEC = 3 * 3600.0   # разбор дольше 3ч живым не бывает — чистим призраков


def running_now() -> dict:
    now = time.time()
    for tk in [t for t, r in RUNNING.items() if now - r["started"] > _RUN_STALE_SEC]:
        RUNNING.pop(tk, None)
    return {tk: {"stage": r["stage"], "elapsed": round(now - r["started"], 1)}
            for tk, r in RUNNING.items()}


async def _noop(_e: dict) -> None:
    pass


def _num(v):
    """→ float | None. Строки '105,3' терпим, NaN/inf/мусор — в None."""
    try:
        if isinstance(v, str):
            v = v.replace(",", ".").strip()
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _sanitize_forecast(f: dict | None, cur_price=None) -> dict | None:
    """Шифровщик — LLM: типы плавают (строки вместо чисел, NaN, мусорные поля).
    Приводим к жёсткому контракту, чтобы график/будильники/озвучка не ловили
    сюрпризов. Ничего не выдумываем: невалидное → None/пусто."""
    if not isinstance(f, dict):
        return None
    out: dict = {}
    act = str(f.get("action") or "").upper()
    out["action"] = act if act in ("LONG", "SHORT", "FLAT") else "FLAT"
    for k in ("conviction", "risk_pct", "potential_pct"):
        v = _num(f.get(k))
        out[k] = int(max(0, min(100, v))) if v is not None else None
    out["current_price"] = _num(f.get("current_price")) or _num(cur_price)
    for h in ("forecast_1h", "forecast_24h", "forecast_day"):
        b = f.get(h)
        if isinstance(b, dict):
            d = str(b.get("direction") or "flat").lower()
            pb = _num(b.get("probability"))
            out[h] = {"direction": d if d in ("up", "down", "flat") else "flat",
                      "target": _num(b.get("target")), "low": _num(b.get("low")),
                      "high": _num(b.get("high")),
                      "probability": int(max(0, min(100, pb))) if pb is not None else None}
        else:
            out[h] = None
    for k in ("support", "resistance"):
        vals = f.get(k) if isinstance(f.get(k), (list, tuple)) else []
        out[k] = [x for x in (_num(v) for v in vals) if x is not None][:6]
    out["entry"] = _num(f.get("entry"))
    out["invalidation"] = _num(f.get("invalidation"))
    for k in ("manipulation", "wyckoff_phase", "wyckoff_note", "astro_note",
              "aether_note", "summary", "voice_line"):
        v = f.get(k)
        out[k] = str(v)[:600] if isinstance(v, str) and v.strip() else ""
    rk = f.get("risks") if isinstance(f.get("risks"), (list, tuple)) else []
    out["risks"] = [str(r)[:300] for r in rk if isinstance(r, str) and r.strip()][:6]
    # ── ИИ-ПИЛОТ: исполняемый приказ шифровщика (аддон CODER_EXEC_ADDON).
    #    Строгий контракт (закалка веером): do∈{BUY,SELL}, invalidation>0 И
    #    все уровни на ПРАВИЛЬНОЙ стороне (BUY: стоп ниже входа/тейка, тейк
    #    выше; SELL зеркально) — иначе блок НЕ проходит: позиция «на максимум»
    #    без стопа или со стопом не с той стороны — мгновенная смерть депозита.
    ex = f.get("exec")
    out["exec"] = None
    if isinstance(ex, dict):
        do = str(ex.get("do") or "").upper()
        inv = _num(ex.get("invalidation"))
        entry = _num(ex.get("entry"))
        take = _num(ex.get("take"))
        ok = do in ("BUY", "SELL") and inv is not None and inv > 0
        if ok and entry is not None and entry <= 0:
            ok = False
        ref = entry if entry is not None else out.get("current_price")
        if ok and ref:
            if do == "BUY" and not (inv < ref and (take is None or take > ref)):
                ok = False
            if do == "SELL" and not (inv > ref and (take is None or take < ref)):
                ok = False
        if ok:
            out["exec"] = {"do": do, "entry": entry, "take": take,
                           "invalidation": inv,
                           "why": str(ex.get("why") or "")[:300]}
            # одна истина для UI и исполнения: верхнеуровневые поля = exec
            # (иначе владелец видит на карточке один стоп, а трос стоит по
            # другому — находка веера)
            out["action"] = "LONG" if do == "BUY" else "SHORT"
            if entry is not None:
                out["entry"] = entry
            out["invalidation"] = inv
        else:
            out["exec_error"] = ("exec отклонён санитайзером: стороны/знаки "
                                 "уровней не сходятся или нет invalidation>0")
    return out




# ──────────────────────────────────────────────────────────────────────
# Сбор досье (Tinkoff → фолбэк MOEX)
# ──────────────────────────────────────────────────────────────────────

async def collect_dossier(ticker: str, asset_class: str) -> dict:
    d: dict = {}
    if tinkoff.enabled():
        try:
            d = await tinkoff.collect_dossier(ticker, asset_class)
        except Exception as e:
            logger.warning("tinkoff dossier %s failed: %s", ticker, str(e)[:100])
            d = {"ticker": ticker, "asset_class": asset_class, "errors": [str(e)[:80]]}
    else:
        d = {"ticker": ticker, "asset_class": asset_class, "source": "moex",
             "errors": ["Tinkoff токен не задан — стакан/лента недоступны, режим MOEX"]}

    have_price = bool(d.get("price"))
    have_daily = bool(d.get("daily_metrics"))
    if not have_price or not have_daily:
        # фолбэк MOEX для свечей/цены
        try:
            mp = await moex.last_price(ticker, asset_class)
            if mp and not have_price:
                d["price"] = {"price": mp["price"], "source": "moex",
                              "ts": mp.get("ts"), "volume_today": mp.get("volume_today"),
                              "change_pct": mp.get("change_pct")}
            daily = await moex.candles(ticker, asset_class, "1d", 200)
            intra = await moex.candles(ticker, asset_class, "10m", 3)
            if daily and not have_daily:
                d["candles_daily_tail"] = daily[-60:]
                d["daily_metrics"] = daily_metrics(daily)
            if intra and not d.get("intraday_metrics"):
                d["candles_intraday_5m"] = intra[-120:]
                d["intraday_metrics"] = intraday_metrics(intra)
            if not d.get("instrument"):
                d["instrument"] = {"ticker": ticker, "name": ticker}
            d.setdefault("source", "moex")
        except Exception as e:
            logger.warning("moex fallback %s failed: %s", ticker, str(e)[:100])
    try:
        wy = wyckoff.analyze_dossier(d)
        if wy:
            d["wyckoff"] = wy
    except Exception as e:
        logger.warning("wyckoff %s failed: %s", ticker, str(e)[:100])
    # ── МАЙЯ (канон 05.08): вакуум в стакане ордеров — разрежения, стены,
    #    тяга имплозии; сбой не роняет досье (аддитивность) ──
    try:
        my = maya.analyze(d.get("orderbook"), d.get("tape"))
        if my:
            d["maya"] = my
    except Exception as e:
        logger.warning("maya %s failed: %s", ticker, str(e)[:100])
    # ── ЭФИРНЫЙ СЛОЙ (REAL SKY): волна генезиса × реальные свечи; тяжёлый
    #    skyfield уводится в поток; сбой не роняет досье (аддитивность) ──
    try:
        try:
            idate = await moex.issue_date(ticker)   # генезис бумаги вне реестра
        except Exception:
            idate = None
        ae = await asyncio.to_thread(
            aether.compute_context, ticker, aether.candles_from_dossier(d),
            None, None, None, "1h", idate)
        if ae:
            d["aether"] = ae
    except Exception as e:
        logger.warning("aether %s failed: %s", ticker, str(e)[:100])
    # ── КОРПОРАТИВНЫЙ ФОН (Tinkoff): консенсус аналитиков + ближайший дивиденд
    if tinkoff.enabled() and asset_class == "share":
        uid = (d.get("instrument") or {}).get("uid") or d.get("uid")
        if uid:
            c, dv = await asyncio.gather(tinkoff.consensus(uid),
                                         tinkoff.next_dividend(uid),
                                         return_exceptions=True)
            if isinstance(c, dict) and c:
                d["consensus"] = c
            elif isinstance(c, Exception):
                logger.warning("consensus %s: %s", ticker, str(c)[:80])
            if isinstance(dv, dict) and dv:
                d["dividend_next"] = dv
            elif isinstance(dv, Exception):
                logger.warning("next_dividend %s: %s", ticker, str(dv)[:80])
    # ── ПЕРВОИСТОЧНИК: у фьючерса есть базовый актив — «первород», на
    #    котором стоит вся игра. Спот, базис, Вайкофф основы.
    try:
        if asset_class == "futures":
            u = await underlying.collect(d, ticker)
            if u:
                d["underlying"] = u
    except Exception as e:
        logger.warning("первоисточник %s: %s", ticker, str(e)[:100])
    return d


# ──────────────────────────────────────────────────────────────────────
# Рендер досье в текст для ИИ
# ──────────────────────────────────────────────────────────────────────

def _fmt(v, suf=""):
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "да" if v else "нет"
    if isinstance(v, float):
        return f"{v:g}{suf}"
    return f"{v}{suf}"


def render_dossier(d: dict, *, full: bool = True) -> str:
    L = []
    inst = d.get("instrument", {}) or {}
    L.append(f"ИНСТРУМЕНТ: {inst.get('name','?')} [{inst.get('ticker', d.get('ticker'))}] "
             f"класс={d.get('asset_class')} валюта={inst.get('currency')} лот={inst.get('lot')} "
             f"источник={d.get('source')}")
    if inst.get("expiration"):
        L.append(f"  фьючерс: базовый_актив={inst.get('basic_asset')} экспирация={inst.get('expiration')} "
                 f"шаг_цены={inst.get('min_price_increment')}")
    if inst.get("sector"):
        L.append(f"  сектор={inst.get('sector')} страна={inst.get('country')} биржа={inst.get('exchange')}")

    fu = d.get("fundamentals") or {}
    if fu:
        def _fv(k, suf=""):
            v = fu.get(k)
            return (f"{_fmt(v)}{suf}") if v is not None else None
        parts = []
        for label, key, suf in (("P/E", "pe_ttm", ""), ("P/B", "pb", ""), ("P/S", "ps_ttm", ""),
                                ("EV/EBITDA", "ev_ebitda", ""), ("ROE", "roe", "%"), ("ROA", "roa", "%"),
                                ("EPS", "eps_ttm", ""), ("дивдоходность", "div_yield_ttm", "%"),
                                ("beta", "beta", ""), ("free-float", "free_float", "%")):
            val = _fv(key, suf)
            if val is not None:
                parts.append(f"{label}={val}")
        cap = fu.get("market_cap")
        head = f"капитализация={_fmt(cap)} {fu.get('currency','')}".strip() if cap is not None else ""
        rng = ""
        if fu.get("high_52w") is not None or fu.get("low_52w") is not None:
            rng = f" диапазон_52нед={_fmt(fu.get('low_52w'))}…{_fmt(fu.get('high_52w'))}"
        L.append("ФУНДАМЕНТАЛ: " + "; ".join([x for x in [head] if x] + parts) + rng)

    p = d.get("price") or {}
    L.append(f"ЦЕНА (реальная): {_fmt(p.get('price'))} (источник {p.get('source','tinkoff')})"
             + (f", изм.за день {_fmt(p.get('change_pct'),'%')}" if p.get('change_pct') is not None else "")
             + (f", объём_сегодня {_fmt(p.get('volume_today'))}" if p.get('volume_today') is not None else ""))

    ts = d.get("trading_status") or {}
    if ts:
        L.append(f"ТОРГИ: статус={ts.get('status')} лимитные={_fmt(ts.get('limit_order'))} "
                 f"рыночные={_fmt(ts.get('market_order'))}")

    dm = d.get("daily_metrics") or {}
    if dm:
        L.append("ДНЕВНЫЕ МЕТРИКИ:")
        L.append(f"  закрытие={_fmt(dm.get('last_close'))} изм.день={_fmt(dm.get('day_change_pct'),'%')} "
                 f"RSI14={_fmt(dm.get('rsi14'))} ATR14={_fmt(dm.get('atr14'))} вола(год)={_fmt(dm.get('vol_annual_pct'),'%')}")
        L.append(f"  EMA9={_fmt(dm.get('ema9'))} EMA21={_fmt(dm.get('ema21'))} "
                 f"EMA50={_fmt(dm.get('ema50'))} EMA200={_fmt(dm.get('ema200'))} "
                 f">EMA50={_fmt(dm.get('above_ema50'))} >EMA200={_fmt(dm.get('above_ema200'))}")
        L.append(f"  диапазон20: low={_fmt(dm.get('low_20d'))} high={_fmt(dm.get('high_20d'))} "
                 f"(до high {_fmt(dm.get('dist_to_high20_pct'),'%')}, до low {_fmt(dm.get('dist_to_low20_pct'),'%')}); "
                 f"экстремумы периода {_fmt(dm.get('low_range'))}…{_fmt(dm.get('high_range'))}")
        L.append(f"  ср.объём20д={_fmt(dm.get('avg_volume_20d'))}")

    im = d.get("intraday_metrics") or {}
    if im:
        L.append(f"СЕССИЯ ({im.get('session_date')}): откр={_fmt(im.get('session_open'))} "
                 f"посл={_fmt(im.get('session_last'))} H={_fmt(im.get('session_high'))} L={_fmt(im.get('session_low'))} "
                 f"диапазон={_fmt(im.get('session_range_pct'),'%')} изм={_fmt(im.get('session_change_pct'),'%')} "
                 f"VWAP={_fmt(im.get('vwap'))} к_VWAP={_fmt(im.get('vs_vwap_pct'),'%')} объём={_fmt(im.get('session_volume'))}")

    ob = d.get("orderbook") or {}
    if ob:
        L.append(f"СТАКАН: bid={_fmt(ob.get('best_bid'))} ask={_fmt(ob.get('best_ask'))} "
                 f"спред={_fmt(ob.get('spread'))} ({_fmt(ob.get('spread_bps'),' bps')}) "
                 f"дисбаланс={_fmt(ob.get('imbalance'))} (объём bid={_fmt(ob.get('bid_vol'))} / ask={_fmt(ob.get('ask_vol'))})")
        wb, wa = ob.get("wall_bid"), ob.get("wall_ask")
        if wb:
            L.append(f"  стена BID: {_fmt(wb.get('q'))} лотов @ {_fmt(wb.get('p'))}")
        if wa:
            L.append(f"  стена ASK: {_fmt(wa.get('q'))} лотов @ {_fmt(wa.get('p'))}")
        if ob.get("limit_up") or ob.get("limit_down"):
            L.append(f"  лимиты: вверх={_fmt(ob.get('limit_up'))} вниз={_fmt(ob.get('limit_down'))}")

    tp = d.get("tape") or {}
    if tp and tp.get("count"):
        L.append(f"ЛЕНТА (за {tp.get('window_min')}мин): сделок={tp.get('count')} "
                 f"buy_vol={_fmt(tp.get('buy_vol'))} sell_vol={_fmt(tp.get('sell_vol'))} "
                 f"ДЕЛЬТА={_fmt(tp.get('delta'))} агрессор_buy={_fmt(tp.get('aggressor_ratio'))} "
                 f"ср.размер={_fmt(tp.get('avg_trade_size'))}")
        if tp.get("largest_prints"):
            pr = "; ".join(f"{x['side']} {x['q']}@{x['p']}" for x in tp["largest_prints"][:3])
            L.append(f"  крупные принты: {pr}")

    my = d.get("maya")
    if my:
        L.append("")
        L.append("═══ СТАКАН: ВАКУУМ · СИНГУЛЯРНОСТИ · ТЯГА ═══")
        L.append(maya.render_for_ai(my))

    tt = d.get("tinkoff_tech") or {}
    if tt.get("rsi_tinkoff"):
        last = tt["rsi_tinkoff"][-1] if tt["rsi_tinkoff"] else {}
        L.append(f"ТЕХ Tinkoff: RSI(посл)={_fmt(last.get('signal'))}")
    if tt.get("macd_tinkoff"):
        last = tt["macd_tinkoff"][-1] if tt["macd_tinkoff"] else {}
        L.append(f"  MACD={_fmt(last.get('macd'))} signal={_fmt(last.get('signal'))}")

    if d.get("margin"):
        m = d["margin"]
        L.append(f"ГО фьючерса: покупка={_fmt(m.get('margin_buy'))} продажа={_fmt(m.get('margin_sell'))}")
    if d.get("dividends"):
        dv = d["dividends"][-2:]
        L.append("ДИВИДЕНДЫ (последние): " +
                 "; ".join(f"{x['value']} (rec {str(x.get('record_date'))[:10]}, "
                           f"yield {x.get('yield')}%)" for x in dv))

    if full:
        dt = d.get("candles_daily_tail") or []
        if dt:
            L.append("ПОСЛЕДНИЕ ДНЕВНЫЕ СВЕЧИ (дата O/H/L/C V):")
            for c in dt[-15:]:
                L.append(f"  {str(c['t'])[:10]}: {c['o']}/{c['h']}/{c['l']}/{c['c']} v={c['v']}")
        ht = d.get("candles_hourly") or []
        if ht:
            L.append("ПОСЛЕДНИЕ ЧАСОВЫЕ СВЕЧИ:")
            for c in ht[-10:]:
                L.append(f"  {str(c['t'])[5:16].replace('T',' ')}: {c['o']}/{c['h']}/{c['l']}/{c['c']} v={c['v']}")

    wy = d.get("wyckoff") or {}
    if wy.get("daily"):
        L.append(wyckoff.summary_line(wy["daily"]))
        if wy.get("hourly"):
            L.append(wyckoff.summary_line(wy["hourly"]))

    c = d.get("consensus")
    dv = d.get("dividend_next")
    if c or dv:
        L.append("")
        L.append("═══ КОРПОРАТИВНЫЙ ФОН (Tinkoff) ═══")
        if c:
            L.append(f"КОНСЕНСУС АНАЛИТИКОВ: {c.get('reco')} · таргет {_fmt(c.get('target'))} "
                     f"(апсайд {_fmt(c.get('upside_pct'),'%')}) · домов: {c.get('houses_count')}")
            for h in (c.get("houses_top") or [])[:3]:
                L.append(f"  {h.get('company')}: {h.get('reco')} → {_fmt(h.get('target'))}")
        if dv:
            dtr = dv.get("days_to_record")
            L.append(f"БЛИЖАЙШИЙ ДИВИДЕНД: {_fmt(dv.get('value'))} {dv.get('currency','')} "
                     f"(доходность {_fmt(dv.get('yield_pct'),'%')}), отсечка {str(dv.get('record_date'))[:10]}"
                     + (f" — через {dtr} дн" if dtr is not None else ""))
            if dtr is not None and dtr <= 45:
                L.append("  ⚠ ОТСЕЧКА БЛИЗКО: после неё гэп вниз ≈ на размер дивиденда; "
                         "фьючерс на эту акцию справедливо НИЖЕ спота (бэквордация = дивиденд, "
                         "не сигнал слабости).")

    u = d.get("underlying")
    if u:
        L.append("")
        L.append("═══ ПЕРВОИСТОЧНИК (БАЗОВЫЙ АКТИВ) ═══")
        L.append(underlying.render_for_ai(u, d.get("wyckoff")))

    if d.get("errors"):
        L.append("ПРИМЕЧАНИЯ СБОРА: " + "; ".join(d["errors"]))
    return "\n".join(L)


def render_news_for_ticker(news_list: list[dict]) -> str:
    if not news_list:
        return ""
    arrow = {"up": "▲", "down": "▼", "mixed": "◆"}
    L = []
    for n in news_list:
        L.append(f"{arrow.get(n.get('direction'),'•')} [{n.get('strength','?')}] "
                 f"{n.get('headline','')}: {n.get('digest','')}")
    return "\n".join(L)


# ──────────────────────────────────────────────────────────────────────
# Главный конвейер
# ──────────────────────────────────────────────────────────────────────

async def run_pipeline(tickers: list[str], on_event: Emit = _noop,
                       mode: str = "predator") -> dict:
    t_start = time.time()
    mode = mode if mode in prompts.MODES else prompts.DEFAULT_MODE
    # астро-контекст (один на прогон; тяжёлый расчёт — в потоке, с кэшем)
    try:
        astro_ctx = await astro.acontext()
        astro_text = astro.render_for_ai(astro_ctx)
    except Exception as e:
        logger.warning("astro failed: %s", str(e)[:80])
        astro_ctx, astro_text = {}, ""
    md = prompts.MODES.get(mode, {})
    await on_event({"type": "mode", "mode": mode, "name": md.get("name"),
                    "icon": md.get("icon")})
    if astro_ctx:
        await on_event({"type": "astro", "astro": astro_ctx,
                        "line": astro.short_line(astro_ctx)})
    metas = []
    seen: set[str] = set()
    for code in tickers:
        cu = (code or "").upper().strip()
        if not cu or cu in seen:            # дубль в одном запросе = два конвейера
            continue                        # на один тикер и гонка записей — отсекаем
        seen.add(cu)
        if cu in RUNNING:                   # уже разбирается (другая вкладка/устройство):
            await on_event({"type": "skip", "ticker": cu, "reason": "busy",
                            "detail": f"{cu}: уже разбирается — второй конвейер не запускаю"})
            continue
        it = instruments.get(cu)
        if it:
            metas.append({"ticker": it["code"], "name": it["name"],
                          "asset_class": it["asset_class"],
                          "category": it["category"], "weather": it["weather"]})
        else:
            metas.append({"ticker": cu, "name": cu,
                          "asset_class": instruments.guess_asset_class(cu),
                          "category": "Прочее", "weather": []})
    if not metas:
        await on_event({"type": "error", "detail": "не выбран ни один инструмент"})
        return {"ok": False}
    # тикеры застолблены СРАЗУ (атомарно в одном тике event loop): перезагрузка
    # страницы во время сбора данных теперь тоже видит живой разбор в /api/running
    for m in metas:
        _mark(m["ticker"], "collect")

    # ── 0. Данные ───────────────────────────────────────────────────
    await on_event({"type": "stage", "stage": "collect", "status": "start",
                    "detail": f"сбор данных по {len(metas)} объектам + новости за {config.NEWS_DAYS} сут"})

    news_task = asyncio.create_task(news.fetch_news())
    dossier_tasks = {m["ticker"]: asyncio.create_task(
        collect_dossier(m["ticker"], m["asset_class"])) for m in metas}
    weather_tasks = {m["ticker"]: asyncio.create_task(
        weather_mod.fetch_weather(m["weather"]))
        for m in metas if m["weather"]}

    try:
        news_items = await news_task
    except Exception as e:
        logger.warning("news fetch failed: %s", str(e)[:100])
        news_items = []
    dossiers = {}
    for tk, t in dossier_tasks.items():
        try:
            dossiers[tk] = await t
        except Exception as e:
            dossiers[tk] = {"ticker": tk, "errors": [str(e)[:80]]}
        await on_event({"type": "stage", "stage": "dossier", "ticker": tk,
                        "status": "done",
                        "price": (dossiers[tk].get("price") or {}).get("price")})
    weather = {}
    for tk, t in weather_tasks.items():
        try:
            weather[tk] = await t
        except Exception:
            weather[tk] = []

    news_block_common = news.render_for_ai(news_items, limit=70)
    await on_event({"type": "stage", "stage": "collect", "status": "done",
                    "detail": f"новостей собрано: {len(news_items)}"})

    async def _key_fatal(tk: str, stage: str, e: Exception) -> None:
        """Ошибка уровня ключа: дальше стадии на этом ключе бессмысленны.
        401 → ключ снимается (в шапке загорится «нет ключа»), объект
        пропускается, остальной пакет продолжает разбор."""
        msg = ai.humanize_error(e)
        bad_key = ("401" in str(getattr(e, "status_code", "")) or "401" in str(e)
                   or "недопустимые символы" in msg)
        if bad_key:
            config.set_instrument_key(tk, None)
            await on_event({"type": "key_rejected", "ticker": tk})
        await on_event({"type": "stage", "stage": stage, "ticker": tk,
                        "status": "error"})
        await on_event({"type": "error", "ticker": tk,
                        "detail": f"{tk}: {msg}"})

    # ── По каждому инструменту: ВСЁ на его собственном ключе ────────
    # У каждого объекта — свой новостник на ЕГО ключе: сам отбирает релевантные
    # новости, ставит теги и даёт рыночный фон. v2.5.2: объекты пакета идут
    # ПАРАЛЛЕЛЬНО (у каждого свой ключ — очереди нет), запись в базу дописывается
    # ПОСЛЕ КАЖДОЙ СТАДИИ (обрыв больше не сжигает новости/мышление/разбор),
    # новости улетают на фронт сразу после своей стадии, критик стримится.
    results = {}

    async def _analyze_one(m: dict) -> None:
        tk = m["ticker"]
        _mark(tk, "collect")
        try:
            await _analyze_one_inner(m)
        except Exception as e:              # непредвиденное: пакет живёт, оператор видит причину
            logger.exception("конвейер %s упал", tk)
            try:
                await on_event({"type": "error", "ticker": tk,
                                "detail": f"{tk}: конвейер упал — {ai.humanize_error(e)[:160]}"})
            except Exception:
                pass
        finally:
            RUNNING.pop(tk, None)

    async def _analyze_one_inner(m: dict) -> None:
        tk = m["ticker"]
        tk_key = config.key_for(tk)  # персональный ключ этого объекта
        d = dossiers.get(tk, {})
        logger.info("%s: СБОР ЗАВЕРШЁН — далее только стадии ИИ; запросы к "
                    "Tinkoff/MOEX в логе после этой строки идут от интерфейса "
                    "(живой тикер цены/будильники), не от конвейера", tk)
        dossier_full = render_dossier(d, full=True)
        dossier_key = render_dossier(d, full=False)
        weather_txt = weather_mod.render_for_ai(weather.get(tk, []))
        cur_price = (d.get("price") or {}).get("price")
        wy = d.get("wyckoff") or {}
        wyckoff_txt = wyckoff.render_for_ai(wy)
        if wy.get("daily"):
            await on_event({"type": "wyckoff", "ticker": tk, "wyckoff": wy,
                            "line": wyckoff.summary_line(wy["daily"])})
        ae = d.get("aether") or {}
        aether_txt = aether.render_for_ai(ae) if ae else ""
        if ae.get("mode") == "precise":
            await on_event({"type": "aether", "ticker": tk,
                            "aether": aether.chart_payload(ae),
                            "line": aether.short_line(ae)})
        # ⚛ РЕАКТОР НЕБА (канон 05.08): панель по наталу ЭТОГО тикера
        # дописывается к общему астро-блоку; отказ → астро-блок без добавки
        astro_text_tk = astro_text
        try:
            r_ctx = await asyncio.to_thread(reactor_sky.reactor_context, tk)
            r_txt = reactor_sky.render_for_ai(r_ctx)
            if r_txt:
                astro_text_tk = (astro_text + "\n" + r_txt) if astro_text else r_txt
        except Exception as e:
            logger.warning("reactor_sky %s: %s", tk, str(e)[:100])

        # запись создаётся сразу и ДОПИСЫВАЕТСЯ после каждой стадии
        rec = {"ticker": tk, "asset_class": m["asset_class"], "name": m["name"],
               "created_at": time.time(), "dossier": d, "news": [], "tags": [],
               "background": "", "analyst": "", "critic": "", "verdict": "",
               "forecast": None, "thinking": {},
               "aether": (aether.chart_payload(ae)
                          if ae.get("mode") == "precise" else None)}
        # мышление копим списком дельт (ai теперь не собирает всю строку на
        # каждом чанке — full=None), склеиваем только при сохранении
        think_store: dict[str, list] = {}

        async def _save() -> None:
            rec["thinking"] = {k: "".join(v)[-200000:] for k, v in think_store.items() if v}
            try:
                await memory.save_analysis(rec)
            except Exception as e:
                logger.warning("save %s: %s", tk, str(e)[:90])

        # 3a. НОВОСТИ — умный распределитель (думает: отбор + теги + рыночный фон)
        _mark(tk, "news")
        await on_event({"type": "stage", "stage": "news", "ticker": tk, "status": "start"})
        news_rows: list = []
        news_tags: list = []
        market_note = ""
        # целевые новости ИМЕННО этой компании: 14 дней, фолбэк — последняя за 90
        tk_items, tk_note = [], ""
        try:
            tk_items, tk_note = await news.fetch_for_ticker(
                tk, m["name"], asset_class=m["asset_class"])
        except Exception as e:
            logger.warning("news_for_ticker %s: %s", tk, str(e)[:90])
        news_block = ""
        if tk_items:
            news_block += (f"═══ НОВОСТИ КОМПАНИИ ({tk_note}) ═══\n"
                           + news.render_for_ai(tk_items, limit=25) + "\n\n")
        elif tk_note:
            news_block += f"═══ НОВОСТИ КОМПАНИИ ═══\n({tk_note})\n\n"
        news_block += (f"═══ ОБЩИЙ ПОТОК РЫНКА (фон, {config.NEWS_DAYS} дн) ═══\n"
                       + news_block_common)
        if news_items or tk_items:
            try:
                nobj = await ai.ask_json(
                    prompts.NEWS_SYS,
                    prompts.news_user(tk, m["name"], m["asset_class"], news_block),
                    thinking=config.NEWS_THINKING,
                    effort=config.AI_REASONING_EFFORT,
                    route="news", api_key=tk_key)
                if isinstance(nobj, dict):
                    news_rows = nobj.get("relevant") or []
                    news_tags = nobj.get("tags") or []
                    market_note = nobj.get("market_note") or ""
            except Exception as e:
                logger.warning("news %s failed: %s", tk, str(e)[:150])
                if ai.is_key_error(e):
                    await _key_fatal(tk, "news", e)
                    return
        news_txt = render_news_for_ticker(news_rows)
        await on_event({"type": "stage", "stage": "news", "ticker": tk, "status": "done",
                        "detail": f"релевантных: {len(news_rows)} · тегов: {len(news_tags)}"})
        if news_tags:
            await on_event({"type": "tags", "ticker": tk, "tags": news_tags})
        # новости уходят на панель СРАЗУ, не через полчаса в финальном result
        await on_event({"type": "news", "ticker": tk, "news": news_rows,
                        "tags": news_tags})
        background_text = market_note  # рыночный фон для этого объекта
        rec["news"], rec["tags"], rec["background"] = news_rows, news_tags, market_note
        await _save()

        # 3b. АНАЛИТИК (стрим)
        _mark(tk, "analyst")
        await on_event({"type": "stage", "stage": "analyst", "ticker": tk,
                        "status": "start"})
        await on_event({"type": "think", "stage": "analyst", "ticker": tk,
                        "delta": "◎ МЫШЛЕНИЕ АНАЛИТИКА\n"})

        async def _think(_dl, full, _tk=tk):
            think_store.setdefault("analyst", []).append(_dl)
            await on_event({"type": "think", "stage": "analyst", "ticker": _tk,
                            "delta": _dl, "seq": _mirror(_tk, "think", _dl)})

        async def _text(_dl, full, _tk=tk):
            await on_event({"type": "text", "stage": "analyst", "ticker": _tk,
                            "delta": _dl, "seq": _mirror(_tk, "analyst", _dl)})

        async def _retryA(_tk=tk):
            await on_event({"type": "restage", "stage": "analyst", "ticker": _tk})

        analyst_text = ""
        try:
            analyst_text = await ai.stream(
                prompts.analyst_sys(mode),
                prompts.analyst_user(tk, m["name"], dossier_full, news_txt,
                                     background_text, weather_txt,
                                     astro_text_tk, cur_price,
                                     wyckoff_text=wyckoff_txt,
                                     aether_text=aether_txt),
                on_think=_think, on_text=_text, on_retry=_retryA, thinking=True,
                effort=config.AI_REASONING_EFFORT_MAX, route="analyst", api_key=tk_key)
        except Exception as e:
            logger.warning("analyst %s failed: %s", tk, str(e)[:150])
            if ai.is_key_error(e):
                await _key_fatal(tk, "analyst", e)
                return
            analyst_text = f"[ошибка аналитика: {ai.humanize_error(e)[:160]}]"
        await on_event({"type": "stage", "stage": "analyst", "ticker": tk,
                        "status": "done"})
        rec["analyst"] = analyst_text
        await _save()

        # 3b. КРИТИК — теперь тоже стрим: живое мышление и текст в реальном времени
        _mark(tk, "critic")
        await on_event({"type": "stage", "stage": "critic", "ticker": tk,
                        "status": "start"})
        await on_event({"type": "think", "stage": "critic", "ticker": tk,
                        "delta": "\n\n◎ МЫШЛЕНИЕ КРИТИКА\n"})

        async def _thinkC(_dl, full, _tk=tk):
            think_store.setdefault("critic", []).append(_dl)
            await on_event({"type": "think", "stage": "critic", "ticker": _tk,
                            "delta": _dl, "seq": _mirror(_tk, "think", _dl)})

        async def _textC(_dl, full, _tk=tk):
            await on_event({"type": "text", "stage": "critic", "ticker": _tk,
                            "delta": _dl, "seq": _mirror(_tk, "critic", _dl)})

        async def _retryC(_tk=tk):
            await on_event({"type": "restage", "stage": "critic", "ticker": _tk})

        critic_text = ""
        try:
            critic_text = await ai.stream(
                prompts.critic_sys(mode),
                prompts.critic_user(tk, analyst_text, dossier_full, news_txt,
                                    wyckoff_text=wyckoff_txt),
                on_think=_thinkC, on_text=_textC, on_retry=_retryC, thinking=True,
                effort=config.AI_REASONING_EFFORT,   # без max_tokens: критик тоже сам решает объём
                route="critic", api_key=tk_key)
        except Exception as e:
            logger.warning("critic %s failed: %s", tk, str(e)[:150])
            critic_text = f"[критик недоступен: {ai.humanize_error(e)[:120]}]"
        await on_event({"type": "stage", "stage": "critic", "ticker": tk,
                        "status": "done", "text": critic_text})
        rec["critic"] = critic_text
        await _save()

        # 3c. СИНТЕЗ (стрим)
        _mark(tk, "synthesis")
        await on_event({"type": "stage", "stage": "synthesis", "ticker": tk,
                        "status": "start"})
        await on_event({"type": "think", "stage": "synthesis", "ticker": tk,
                        "delta": "\n\n◎ МЫШЛЕНИЕ СИНТЕЗА (вердикт)\n"})

        async def _think2(_dl, full, _tk=tk):
            think_store.setdefault("synthesis", []).append(_dl)
            await on_event({"type": "think", "stage": "synthesis", "ticker": _tk,
                            "delta": _dl, "seq": _mirror(_tk, "think", _dl)})

        async def _text2(_dl, full, _tk=tk):
            await on_event({"type": "text", "stage": "synthesis", "ticker": _tk,
                            "delta": _dl, "seq": _mirror(_tk, "verdict", _dl)})

        async def _retryS(_tk=tk):
            await on_event({"type": "restage", "stage": "synthesis", "ticker": _tk})

        verdict_text = ""
        try:
            verdict_text = await ai.stream(
                prompts.synthesis_sys(mode),
                prompts.synthesis_user(tk, m["name"], analyst_text, critic_text,
                                       dossier_key, astro_text_tk, cur_price,
                                       wyckoff_text=wyckoff_txt,
                                       aether_text=aether_txt),
                on_think=_think2, on_text=_text2, on_retry=_retryS, thinking=True,
                effort=config.AI_REASONING_EFFORT_MAX, route="synthesis", api_key=tk_key)
        except Exception as e:
            logger.warning("synthesis %s failed: %s", tk, str(e)[:150])
            verdict_text = analyst_text  # запасной вердикт
        await on_event({"type": "stage", "stage": "synthesis", "ticker": tk,
                        "status": "done"})
        rec["verdict"] = verdict_text
        await _save()

        # 3d. ШИФРОВКА → JSON. Шифровщик теперь УМНЫЙ (та же модель pro + thinking)
        #     и СВОБОДНЫЙ по объёму (без потолка токенов) — формат он держит сам
        #     через json_mode (response_format=json_object), а думать может сколько
        #     нужно, чтобы точнее извлечь числа из вердикта.
        _mark(tk, "coder")
        await on_event({"type": "stage", "stage": "coder", "ticker": tk,
                        "status": "start"})
        # ИИ-ПИЛОТ: шифровщик получает боевой аддон — обязан выдать
        # исполняемый exec-блок (BUY/SELL, флет запрещён). Рубильник читается
        # ЧЕРЕЗ config (тумблер панели → config_user.json, откат на env):
        # раньше смотрели только окружение, и режим, включённый кнопкой,
        # не доезжал до шифровщика — приказа не было, пилот стоял пустой.
        ai_pilot_on = config.get_bool("PYTHIA_AI_PILOT", False)
        coder_sys = prompts.CODER_SYS
        if ai_pilot_on:
            coder_sys = prompts.CODER_SYS + prompts.CODER_EXEC_ADDON
        forecast = None
        try:
            forecast = await ai.ask_json(
                coder_sys,
                prompts.coder_user(tk, m["name"], cur_price, verdict_text),
                route="coder", api_key=tk_key)
        except Exception as e:
            logger.warning("coder %s failed: %s", tk, str(e)[:150])
        if not isinstance(forecast, dict):   # второй заход: сжатый вердикт + жёсткая команда
            try:
                forecast = await ai.ask_json(
                    coder_sys,
                    prompts.coder_user(tk, m["name"], cur_price, verdict_text[-8000:])
                    + "\n\nВЕРНИ СТРОГО ОДИН JSON-ОБЪЕКТ ПО СХЕМЕ, БЕЗ ЕДИНОГО СИМВОЛА ВНЕ JSON.",
                    route="coder2", api_key=tk_key)
                logger.info("coder %s: второй заход собрал JSON", tk)
            except Exception as e:
                logger.warning("coder2 %s failed: %s", tk, str(e)[:150])
        forecast = _sanitize_forecast(forecast, cur_price)
        # ИИ-ПИЛОТ: «ФЛЕТ ЗАПРЕЩЁН» дожимается КОДОМ, не только промптом
        # (находка веера: без exec режим молча сидел вне рынка). Третий заход —
        # только за exec-блоком, короткий и жёсткий.
        if (ai_pilot_on
                and isinstance(forecast, dict) and not forecast.get("exec")):
            try:
                ex2 = await ai.ask_json(
                    prompts.CODER_SYS + prompts.CODER_EXEC_ADDON,
                    (f"ОБЪЕКТ: {tk}. Текущая цена: {cur_price}.\n\n"
                     f"ВЕРДИКТ (сжат):\n{verdict_text[-6000:]}\n\n"
                     "Прошлый ответ пришёл БЕЗ валидного exec-блока. ФЛЕТ "
                     "ЗАПРЕЩЁН. Верни СТРОГО один JSON вида "
                     '{"exec": {"do": "BUY|SELL", "entry": <число или null>, '
                     '"take": <число или null>, "invalidation": <число>, '
                     '"why": "<фраза>"}} — и больше НИЧЕГО. Для BUY '
                     "invalidation НИЖЕ входа/цены, take ВЫШЕ; для SELL "
                     "зеркально."),
                    route="coder_exec", api_key=tk_key)
                if isinstance(ex2, dict):
                    merged = dict(forecast)
                    merged["exec"] = (ex2.get("exec")
                                      if isinstance(ex2.get("exec"), dict) else ex2)
                    redone = _sanitize_forecast(merged, cur_price)
                    if isinstance(redone, dict) and redone.get("exec"):
                        forecast = redone
                        logger.info("coder_exec %s: дожим собрал exec-блок", tk)
            except Exception as e:
                logger.warning("coder_exec %s failed: %s", tk, str(e)[:120])
        if isinstance(forecast, dict) and cur_price and not forecast.get("current_price"):
            forecast["current_price"] = cur_price
        if isinstance(forecast, dict) and wy.get("daily"):
            forecast.setdefault("wyckoff_phase", f"{wy['daily']['read']} · {wy['daily']['phase']}")
        await on_event({"type": "stage", "stage": "coder", "ticker": tk,
                        "status": "done" if forecast else "error"})

        rec["forecast"] = forecast
        rec["created_at"] = time.time()
        await _save()
        results[tk] = {"forecast": forecast, "verdict": verdict_text}
        await on_event({"type": "result", "ticker": tk, "forecast": forecast,
                        "underlying": d.get("underlying"),
                        "consensus": d.get("consensus"),
                        "dividend_next": d.get("dividend_next"),
                        "verdict": verdict_text, "analyst": analyst_text,
                        "critic": critic_text,
                        "news": news_rows, "tags": news_tags,
                        "wyckoff": wy or None,
                "aether": (aether.chart_payload(ae) if ae.get("mode") == "precise" else None),
                        "price": cur_price})

    # объекты пакета — параллельно: у каждого свой ключ DeepSeek, друг друга не ждут
    try:
        await asyncio.gather(*(_analyze_one(m) for m in metas),
                             return_exceptions=True)
    finally:
        for m in metas:                     # страховка: RUNNING не оставляет призраков,
            RUNNING.pop(m["ticker"], None)  # даже если задачу срубили на выключении

    tok = ai.tokens()
    await on_event({"type": "done", "elapsed_sec": round(time.time() - t_start, 1),
                    "tickers": [m["ticker"] for m in metas], "tokens": tok})
    return {"ok": True, "results": results, "tokens": tok}
