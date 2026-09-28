# -*- coding: utf-8 -*-
"""ПИФИЯ v5 — РЫНОЧНЫЕ ЧАСЫ: время с поправкой на сервер биржи и статус торгов по площадке бумаги.

Воля владельца: «запустил — пришло время, биржа закрыта — программа стопорится;
новости в фоне копятся» и «биржа открывается не в 10:00, а раньше» — часы обязаны отражать
реальные сессии бумаги (утренняя с 07:00, вечерняя, выходные), а не общий регламент «с 10:00».
Часы машины могут врать (контейнер отстаёт на часы), поэтому «сейчас» для биржи берём не с
локальных часов, а со сдвигом к серверу биржи.

  sync()      — раз в SYNC_SEC сдвиг локальных часов к серверу: заголовок Date ответа
                Tinkoff (после любого запроса `tinkoff._post`), без токена — MOEX ISS
                (только заголовок Date, никаких данных индексов); без сети — локальные
                часы и предупреждение в лог.
  now()       — epoch с поправкой; now_msk() — datetime МСК; skew() — сдвиг для статуса.
  status(ticker, asset_class, instrument_id=None) →
      {"open": bool, "reason": "…", "next_open_ts": float|None, "next_open_in_s": int|None,
       "next_open_msk": "…"|None,
       "session": "утренняя|основная|вечерняя|выходная|клиринг|закрыто|выходной",
       "exchange": "MOEX_MRNG_EVNG_E_WKND_DLR"|…|None, "sessions_today": [окна дня в МСК],
       "source": "tinkoff|schedule", "static": bool, "ts": float, "note": "…", "skew_s": float}
      Площадка — поле `exchange` инструмента (tinkoff.resolve, кэш) в верхнем регистре; расписание
      TradingSchedules берётся именно по ней (кэш SCHED_TTL по площадке). Окна сделок дня: с
      opening_auction_start + 10 мин (если аукцион есть; на выходных это 09:50 → 10:00, брокерский
      start 02:00 не берём) или со start, до end; внутри дня — паузы по классу актива (биржа их
      в API не отдаёт): акции — аукцион открытия основной 09:50–10:00, аукцион закрытия 18:40–18:50,
      пауза 18:50–19:05 перед вечерней; фьючерсы — клиринги 14:00–14:05 и 18:45–19:05. Сессия по
      времени: утренняя (до 10:00), основная (10:00–18:50; фьючерсы до 18:45), вечерняя (после
      19:05), выходная (сб/вс). «Открыто сейчас» — по-прежнему GetTradingStatus (NORMAL_TRADING +
      лимитные заявки + API); расписание — календарь, next_open и describe. Разошлись — статус
      главнее, в note честно. Без токена / при сбое — статический регламент по классу актива
      (акции 07:00–09:50 утренняя, 10:00–18:40 основная, 19:05–23:50 вечерняя; фьючерсы
      07:00–23:50 с клирингами; валюта 07:00–23:50; выходные — «зависит от бумаги, без токена
      считаем закрытым») с честной пометкой «расписание статическое».
  is_open(...) → bool; describe(status_dict) → строка для ИИ и панели.

Классы актива: share (акции/ETF/облигации), future(s), currency. Данные — только из ответа
биржи или из регламента; ничего не выдумываем: нет сети — так и пишем в note.

Self-тест (без сети): python3 -m backend.market_clock
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

try:
    from . import tinkoff
except ImportError:                                        # запуск как скрипт
    import tinkoff                                         # noqa: E401

log = logging.getLogger("pythia.market_clock")

MSK = timezone(timedelta(hours=3))
SYNC_SEC = 600.0            # сдвиг часов пересчитываем раз в ~10 мин
STATUS_TTL = 60.0           # GetTradingStatus — кэш 60 с
SCHED_TTL = 6 * 3600.0      # TradingSchedules — кэш 6 ч (по площадке)
AUCTION_MIN = 10            # сделки начинаются через 10 мин после начала аукциона открытия
MOEX_CLOCK_URL = "https://iss.moex.com/iss/index.json"   # только заголовок Date, данных не берём

# ── площадки и паузы по классу актива (МСК) ─────────────────────────────────────────
_EXCHANGE_DEFAULT = {"share": "MOEX", "future": "FORTS", "currency": "MOEX"}   # если инструмент не резолвится
_RESOLVE_KIND = {"share": "share", "future": "futures", "currency": "currency"}  # что понимает tinkoff.resolve
#  паузы внутри торгового дня, которых нет в TradingSchedules: (от, до, сессия-метка, пометка)
_PAUSES: dict[str, list[tuple[str, str, str, str]]] = {
    "share": [("09:50", "10:00", "закрыто", "аукцион открытия основной сессии"),
              ("18:40", "18:50", "закрыто", "аукцион закрытия — закрытие основной"),
              ("18:50", "19:05", "закрыто", "пауза перед вечерней сессией")],
    "future": [("14:00", "14:05", "клиринг", "дневной клиринг"),
               ("18:45", "19:05", "клиринг", "вечерний клиринг")],
    "currency": [],
}
_MAIN_END = {"share": "18:50", "future": "18:45", "currency": "19:05"}    # граница «основная → вечерняя»
#  статический регламент (без токена / без расписания): будни (аукцион, начало сделок, конец)
_STATIC_DAY = {"share": ("06:50", "07:00", "23:50"), "future": ("06:50", "07:00", "23:50"),
               "currency": (None, "07:00", "23:50")}
# известные праздники (месяц, день) — переносы по постановлениям не учтены, об этом честно в note
_HOLIDAYS = {(1, 1), (1, 2), (1, 7), (2, 23), (3, 8), (5, 1), (5, 9), (6, 12), (11, 4)}
_STATIC_NOTE = "расписание статическое (регламент МосБиржи, переносы праздников не учтены)"
_WEEKDAY_GEN = ("понедельника", "вторника", "среды", "четверга", "пятницы", "субботы", "воскресенья")

# Tinkoff GetTradingStatus → человеку
_STATUS_RU = {
    "SECURITY_TRADING_STATUS_NORMAL_TRADING": "торги идут",
    "SECURITY_TRADING_STATUS_DEALER_NORMAL_TRADING": "торги идут (дилер)",
    "SECURITY_TRADING_STATUS_NOT_AVAILABLE_FOR_TRADING": "торги недоступны",
    "SECURITY_TRADING_STATUS_DEALER_NOT_AVAILABLE_FOR_TRADING": "торги недоступны (дилер)",
    "SECURITY_TRADING_STATUS_OPENING_PERIOD": "период открытия",
    "SECURITY_TRADING_STATUS_CLOSING_PERIOD": "период закрытия",
    "SECURITY_TRADING_STATUS_BREAK_IN_TRADING": "перерыв в торгах",
    "SECURITY_TRADING_STATUS_DEALER_BREAK_IN_TRADING": "перерыв в торгах (дилер)",
    "SECURITY_TRADING_STATUS_CLOSING_AUCTION": "аукцион закрытия",
    "SECURITY_TRADING_STATUS_DARK_POOL_AUCTION": "аукцион крупных пакетов",
    "SECURITY_TRADING_STATUS_DISCRETE_AUCTION": "дискретный аукцион",
    "SECURITY_TRADING_STATUS_OPENING_AUCTION_PERIOD": "аукцион открытия",
    "SECURITY_TRADING_STATUS_TRADING_AT_CLOSING_AUCTION_PRICE": "торги по цене закрытия",
    "SECURITY_TRADING_STATUS_SESSION_ASSIGNED": "сессия назначена",
    "SECURITY_TRADING_STATUS_SESSION_CLOSE": "сессия закрыта",
    "SECURITY_TRADING_STATUS_SESSION_OPEN": "сессия открыта",
    "SECURITY_TRADING_STATUS_UNSPECIFIED": "статус не задан",
}
_OPEN_STATUSES = {"SECURITY_TRADING_STATUS_NORMAL_TRADING",
                  "SECURITY_TRADING_STATUS_DEALER_NORMAL_TRADING"}

# ── состояние ─────────────────────────────────────────────────────────────────────
_skew = 0.0                 # server − local, с
_skew_src = "local"         # tinkoff | moex | local
_sync_ts = 0.0              # локальное время последней попытки sync
_sync_note = "часы ещё не сверялись с биржей"
_sync_lock = asyncio.Lock()
_status_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_sched_cache: dict[str, tuple[float, list[dict]]] = {}      # площадка → (ts, дни)
_last: dict | None = None   # последний статус — для панели без ожидания


def _cfg(name: str, default):
    try:
        try:
            from . import config as _c
        except ImportError:
            import config as _c
        v = getattr(_c, name, default)
        return default if v is None else v
    except Exception:                                      # noqa: BLE001
        return default


def enabled() -> bool:
    """PYTHIA_MARKET_CLOCK: 1 — стопор при закрытом рынке (по умолчанию), 0 — не стопорить."""
    return bool(_cfg("PYTHIA_MARKET_CLOCK", True))


def norm_class(asset_class: str | None) -> str:
    a = (asset_class or "").strip().lower()
    if a in ("future", "futures", "фьючерс", "фьючерсы", "срочный"):
        return "future"
    if a in ("currency", "currencies", "fx", "валюта"):
        return "currency"
    return "share"


# ── время ─────────────────────────────────────────────────────────────────────────
def now() -> float:
    return time.time() + _skew


def now_msk() -> datetime:
    return datetime.fromtimestamp(now(), MSK)


def skew() -> dict:
    return {"skew_s": round(_skew, 1), "source": _skew_src,
            "synced_ts": _sync_ts or None, "age_s": int(time.time() - _sync_ts) if _sync_ts else None,
            "note": _sync_note}


def _apply_skew(server_ts: float, local_ts: float, src: str) -> None:
    global _skew, _skew_src, _sync_note
    prev = _skew
    _skew = float(server_ts - local_ts)
    _skew_src = src
    d = timedelta(seconds=int(abs(_skew)))
    _sync_note = (f"часы сверены с {src}: локальные {'отстают' if _skew > 0 else 'спешат'} на {d}"
                  if abs(_skew) >= 2 else f"часы сверены с {src}: сдвига нет")
    if abs(_skew - prev) >= 2 or abs(_skew) >= 60:
        log.warning("рыночные часы: %s (сдвиг %+.0f с)", _sync_note, _skew)


async def _moex_date() -> float | None:
    """Заголовок Date у MOEX ISS — только часы, никаких данных индексов (воля владельца)."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=6.0) as c:
            r = await c.head(MOEX_CLOCK_URL)
            d = r.headers.get("date")
            return parsedate_to_datetime(d).timestamp() if d else None
    except Exception as e:                                 # noqa: BLE001
        log.info("рыночные часы: MOEX ISS не ответил: %s", str(e)[:80])
        return None


async def sync(force: bool = False) -> dict:
    """Сверить локальные часы с сервером биржи (Tinkoff → MOEX ISS → локальные + предупреждение)."""
    global _sync_ts, _skew_src, _sync_note
    if not force and _sync_ts and time.time() - _sync_ts < SYNC_SEC:
        return skew()
    async with _sync_lock:
        if not force and _sync_ts and time.time() - _sync_ts < SYNC_SEC:
            return skew()
        _sync_ts = time.time()
        # 1) Tinkoff: любой ответ _post оставляет заголовок Date; если давно не было — дёрнем расписание
        try:
            if tinkoff.enabled():
                sd = tinkoff.server_date() if hasattr(tinkoff, "server_date") else None
                if not sd or time.time() - sd[1] > SYNC_SEC:
                    await _schedule(_EXCHANGE_DEFAULT["share"], force=True)
                    sd = tinkoff.server_date() if hasattr(tinkoff, "server_date") else None
                if sd and sd[0] > 0:
                    _apply_skew(sd[0], sd[1], "tinkoff")
                    return skew()
        except Exception as e:                             # noqa: BLE001
            log.info("рыночные часы: Tinkoff не дал Date: %s", str(e)[:80])
        # 2) MOEX ISS — только Date
        t0 = time.time()
        ts = await _moex_date()
        if ts:
            _apply_skew(ts, (t0 + time.time()) / 2, "moex")
            return skew()
        # 3) без сети — локальные часы, честно
        _skew_src = "local"
        _sync_note = ("сети нет — время локальное, сдвиг к бирже неизвестен" if not _skew else
                      f"сети нет — время локальное, держу прошлый сдвиг {_skew:+.0f} с")
        log.warning("рыночные часы: %s", _sync_note)
        return skew()


# ── план дня: окна сделок и паузы ─────────────────────────────────────────────────
def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _day0(d: date) -> float:
    return datetime(d.year, d.month, d.day, tzinfo=MSK).timestamp()


def _hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, MSK).strftime("%H:%M")


def _is_trading_day(d: date) -> bool:
    """Календарь регламента (без биржи): будни минус известные праздники. Выходные торги
    отдельных площадок здесь не видны — их знает только расписание биржи."""
    return d.weekday() < 5 and (d.month, d.day) not in _HOLIDAYS


def _session_name(minute: int, cls: str, weekend: bool) -> str:
    if weekend:
        return "выходная"
    if minute < _hm("10:00"):
        return "утренняя"
    if minute < _hm(_MAIN_END.get(cls, "18:50")):
        return "основная"
    return "вечерняя"


def _day_plan(d: date, cls: str, start_ts: float, end_ts: float, weekend: bool,
              auction_ts: float | None = None) -> list[dict]:
    """Окна дня в epoch: сделки start_ts…end_ts минус паузы класса актива →
    [{"a","b","session","open","why"}]; аукцион открытия — первым окном (закрыто)."""
    win: list[dict] = []
    if auction_ts is not None and auction_ts < start_ts:
        win.append({"a": auction_ts, "b": start_ts, "session": "закрыто", "open": False,
                    "why": "аукцион открытия — сделок нет"})
    day0 = _day0(d)

    bounds = [day0 + _hm("10:00") * 60, day0 + _hm(_MAIN_END.get(cls, "18:50")) * 60]   # утренняя | основная | вечерняя

    def _open(a: float, b: float) -> None:
        """Окно сделок a…b, разрезанное по границам сессий — метка сессии точная в любой момент."""
        cuts = [a] + [x for x in bounds if a < x < b] + [b]
        for x, y in zip(cuts, cuts[1:]):
            sess = _session_name(int((x - day0) // 60), cls, weekend)
            win.append({"a": x, "b": y, "session": sess, "open": True, "why": f"{sess} сессия до {_hhmm(y)}"})

    cur = start_ts
    # выходная сессия акций (сб/вс): аукциона закрытия и паузы перед вечерней нет — сплошное окно
    pauses = [] if (weekend and cls == "share") else _PAUSES.get(cls, [])
    for a, b, sess, why in pauses:
        pa, pb = day0 + _hm(a) * 60, day0 + _hm(b) * 60
        if pb <= cur or pa >= end_ts:
            continue
        if pa > cur:
            _open(cur, pa)
        win.append({"a": max(pa, cur), "b": min(pb, end_ts), "session": sess, "open": False, "why": why})
        cur = min(pb, end_ts)
    if cur < end_ts:
        _open(cur, end_ts)
    return win


def _static_day(d: date, cls: str) -> dict:
    """День по регламенту: {"trading","weekend","windows","auction","note"}."""
    if not _is_trading_day(d):
        wk = d.weekday() >= 5
        return {"trading": False, "weekend": wk, "windows": [], "auction": None,
                "why": ("выходной — зависит от бумаги, без расписания биржи считаем закрытым" if wk
                        else "праздник — торгов нет")}
    auc, st, en = _STATIC_DAY.get(cls, _STATIC_DAY["share"])
    day0 = _day0(d)
    auction = day0 + _hm(auc) * 60 if auc else None
    return {"trading": True, "weekend": False, "auction": auction,
            "windows": _day_plan(d, cls, day0 + _hm(st) * 60, day0 + _hm(en) * 60, False, auction)}


def _broker_day(day: dict, cls: str) -> tuple[date | None, dict | None]:
    """День из TradingSchedules → план: сделки с opening_auction_start + AUCTION_MIN (если аукцион
    есть) или со start, до end; паузы — по классу актива. Нет даты — (None, None)."""
    try:
        y, mo, dd = (int(x) for x in str(day.get("date") or "")[:10].split("-"))
        d = date(y, mo, dd)
    except (TypeError, ValueError):
        return None, None
    wk = d.weekday() >= 5
    if not day.get("is_trading_day"):
        return d, {"trading": False, "weekend": wk, "windows": [], "auction": None,
                   "why": ("выходной — на этой площадке торгов нет" if wk else "праздник — торгов нет")}
    auction = day.get("opening_auction_start")
    start = (auction + AUCTION_MIN * 60) if auction else day.get("start")
    end = day.get("end")
    note = ""
    if not start:
        return d, None                                     # окон биржа не дала — день не разобран
    if wk and not auction:                                 # выходной без аукциона: брокерский start (02:00) — не сделки
        start = max(float(start), _day0(d) + _hm("10:00") * 60)
        note = "выходной: сделки считаем с 10:00 (аукциона биржа не дала)"
    if wk and cls == "share" and end:                      # выходная сессия акций Мосбиржи — до 19:00 (брокер даёт 23:49)
        end = min(float(end), _day0(d) + _hm("19:00") * 60)
    if not end:                                            # конец дня биржа не дала — регламент, честно
        end = _day0(d) + _hm(_STATIC_DAY.get(cls, _STATIC_DAY["share"])[2]) * 60
        note = "конец дня — по регламенту"
    if end <= start:
        return d, None
    return d, {"trading": True, "weekend": wk, "auction": auction, "note": note,
               "windows": _day_plan(d, cls, float(start), float(end), wk, auction)}


def _days_from_broker(days: list[dict], cls: str) -> dict[date, dict]:
    out: dict[date, dict] = {}
    for day in days or []:
        d, plan = _broker_day(day, cls)
        if d is not None and plan is not None:
            out[d] = plan
    return out


def _days_static(d0: date, cls: str, span: int = 16) -> dict[date, dict]:
    return {d0 + timedelta(days=i): _static_day(d0 + timedelta(days=i), cls) for i in range(-1, span)}


def _next_open(days: dict[date, dict], t: float) -> float | None:
    best = None
    for plan in days.values():
        for w in plan.get("windows") or []:
            if w["open"] and w["a"] > t and (best is None or w["a"] < best):
                best = w["a"]
    return best


def _sessions_today(plan: dict | None) -> list[dict]:
    return [{"from": _hhmm(w["a"]), "to": _hhmm(w["b"]), "session": w["session"], "open": bool(w["open"]),
             "why": w["why"]} for w in (plan or {}).get("windows") or []]


def _eval(t: float, days: dict[date, dict], cls: str) -> dict | None:
    """Статус по плану дней: open/session/reason/next_open_ts/sessions_today; None — день t
    в плане не найден (расписание не покрывает)."""
    dt = datetime.fromtimestamp(t, MSK)
    plan = days.get(dt.date())
    if plan is None:
        return None
    base = {"sessions_today": _sessions_today(plan)}
    if not plan.get("trading"):
        return {**base, "open": False, "session": "выходной", "reason": plan.get("why") or "торгов нет",
                "next_open_ts": _next_open(days, t)}
    win = plan["windows"]
    for w in win:
        if w["a"] <= t < w["b"]:
            reason = w["why"]
            if not w["open"] and plan.get("weekend") and w is win[0] and plan.get("auction"):
                reason = "выходной: аукцион открытия, сделки с " + _hhmm(w["b"])
            return {**base, "open": bool(w["open"]), "session": w["session"], "reason": reason,
                    "next_open_ts": None if w["open"] else _next_open(days, t)}
    first_open = next((w for w in win if w["open"]), None)
    if first_open and t < first_open["a"]:
        if plan.get("weekend"):
            reason = "выходной: сделки с " + _hhmm(first_open["a"]) + \
                     (f" после аукциона {_hhmm(plan['auction'])}" if plan.get("auction") else "")
        else:
            reason = "ночь — торги ещё не начались"
    else:
        reason = "торги на сегодня завершены"
    return {**base, "open": False, "session": "закрыто", "reason": reason, "next_open_ts": _next_open(days, t)}


def _static_status(cls: str, ts: float | None = None) -> dict:
    """Статус по регламенту МосБиржи (без биржи): open/session/reason/next_open_ts/sessions_today (МСК)."""
    t = ts if ts is not None else now()
    st = _eval(t, _days_static(datetime.fromtimestamp(t, MSK).date(), cls), cls) or \
        {"open": False, "session": "закрыто", "reason": "торгов нет", "next_open_ts": None, "sessions_today": []}
    st["static"] = True
    st["exchange"] = None
    return st


# ── расписание Tinkoff по площадке ────────────────────────────────────────────────
async def _schedule(exchange: str, force: bool = False) -> list[dict]:
    """TradingSchedules Tinkoff по площадке на неделю вперёд (кэш SCHED_TTL по площадке); нет — []."""
    ex = (exchange or "").upper().strip() or _EXCHANGE_DEFAULT["share"]
    c = _sched_cache.get(ex)
    if c and not force and time.time() - c[0] < SCHED_TTL:
        return c[1]
    days: list[dict] = []
    try:
        if tinkoff.enabled() and hasattr(tinkoff, "trading_schedules"):
            # `from` не может быть раньше текущей даты биржи (ошибка 30003) — берём «сейчас» по её часам
            frm = datetime.fromtimestamp(now(), timezone.utc)
            days = await tinkoff.trading_schedules(ex, frm, frm + timedelta(days=9)) or []
    except Exception as e:                                 # noqa: BLE001
        log.info("рыночные часы: расписание %s недоступно: %s", ex, str(e)[:80])
        days = []
    if days or not c:
        _sched_cache[ex] = (time.time(), days)
    return days


async def _instrument(ticker: str, cls: str) -> tuple[str | None, str | None]:
    """(площадка, uid|figi) инструмента через tinkoff.resolve (свой кэш 6 ч); сбой — (None, None)."""
    try:
        inst = await tinkoff.resolve(ticker, _RESOLVE_KIND.get(cls, "share"))
    except Exception as e:                                 # noqa: BLE001
        log.info("рыночные часы: resolve %s: %s", ticker, str(e)[:80])
        return None, None
    if not isinstance(inst, dict):
        return None, None
    ex = str(inst.get("exchange") or "").upper().strip() or None
    return ex, (inst.get("uid") or inst.get("figi") or None)


# ── статус торгов ─────────────────────────────────────────────────────────────────
def _fmt_next(ts: float | None) -> str | None:
    """«07:00 МСК» / «завтра 10:00 МСК» / «07:00 МСК понедельника 28.09»."""
    if not ts:
        return None
    dt = datetime.fromtimestamp(ts, MSK)
    today = now_msk().date()
    if dt.date() == today:
        return dt.strftime("%H:%M МСК")
    if dt.date() == today + timedelta(days=1):
        return "завтра " + dt.strftime("%H:%M МСК")
    return dt.strftime("%H:%M МСК ") + _WEEKDAY_GEN[dt.weekday()] + dt.strftime(" %d.%m")


def _finish(st: dict, source: str, note: str) -> dict:
    t = now()
    nxt = st.get("next_open_ts")
    st = dict(st)
    st.setdefault("static", source != "tinkoff" and not st.get("exchange"))
    st.setdefault("exchange", None)
    st.setdefault("sessions_today", [])
    st.update({"source": source, "ts": t, "note": note, "skew_s": round(_skew, 1),
               "next_open_ts": nxt,
               "next_open_in_s": max(0, int(nxt - t)) if nxt else None,
               "next_open_msk": _fmt_next(nxt),
               "msk": now_msk().strftime("%d.%m %H:%M")})
    return st


async def status(ticker: str, asset_class: str | None = None, instrument_id: str | None = None) -> dict:
    """Статус торгов по инструменту: Tinkoff (GetTradingStatus + TradingSchedules по площадке бумаги),
    иначе регламент. Статус биржи главнее расписания; расписание — календарь, next_open, describe."""
    global _last
    cls = norm_class(asset_class)
    key = ((ticker or "").upper().strip(), cls)
    c = _status_cache.get(key)
    if c and time.time() - c[0] < STATUS_TTL:
        return c[1]
    try:
        await sync()
    except Exception as e:                                 # noqa: BLE001
        log.info("рыночные часы: sync споткнулся: %s", str(e)[:80])
    t = now()
    static = _static_status(cls, t)
    out: dict | None = None
    try:
        if tinkoff.enabled():
            # 1) площадка бумаги и её расписание
            exchange, iid = (None, None)
            if key[0]:
                exchange, iid = await _instrument(key[0], cls)
            iid = instrument_id or iid
            ex_note = ""
            if not exchange:
                exchange = _EXCHANGE_DEFAULT.get(cls, "MOEX")
                ex_note = f"; площадка инструмента неизвестна — расписание {exchange}"
            days = _days_from_broker(await _schedule(exchange), cls)
            sched = _eval(t, days, cls) if days else None
            if sched is None:                              # расписания нет / день не покрыт — регламент
                sched = dict(static)
                ex_note += "; расписание площадки не получено — регламент" if not days else \
                    "; расписание площадки не покрывает день — регламент"
            else:
                sched["static"] = False
                if sched.get("next_open_ts") is None and not sched.get("open"):
                    sched["next_open_ts"] = static.get("next_open_ts")
                    if sched["next_open_ts"]:
                        ex_note += "; время открытия — по статическому расписанию"
            sched["exchange"] = exchange
            # 2) статус биржи — главнее
            ts = None
            try:
                ts = await tinkoff.trading_status(iid) if iid else None
            except Exception as e:                         # noqa: BLE001
                log.info("рыночные часы: GetTradingStatus %s: %s", key[0], str(e)[:80])
            st_code = (ts or {}).get("status") if isinstance(ts, dict) else None
            if st_code:
                open_ = (st_code in _OPEN_STATUSES and ts.get("limit_order") is not False
                         and ts.get("api_available") is not False)
                reason = _STATUS_RU.get(st_code, st_code)
                if st_code in _OPEN_STATUSES and not open_:
                    reason += (" — заявки через API недоступны" if ts.get("api_available") is False
                               else " — лимитные заявки не принимаются")
                note = f"статус от Tinkoff, расписание площадки {exchange}" + ex_note
                if open_:
                    if sched["open"]:
                        session, reason, nxt = sched["session"], reason + ", " + sched["reason"], None
                    else:
                        dt = datetime.fromtimestamp(t, MSK)
                        session = _session_name(dt.hour * 60 + dt.minute, cls, dt.weekday() >= 5)
                        nxt = None
                        note += "; по расписанию сейчас закрыто — верим бирже"
                else:
                    if not sched["open"]:                  # согласны: причина по расписанию, статус биржи следом
                        session, nxt = sched["session"], sched["next_open_ts"]
                        reason = sched["reason"] + "; биржа: " + reason
                    else:
                        # биржа «закрыто» в окне, которое расписание считает открытым: следующее открытие —
                        # по расписанию площадки, а без него — по регламенту (статика при «открыто» даёт None)
                        session = "закрыто"
                        nxt = _next_open(days, t) if days else _next_open(
                            _days_static(datetime.fromtimestamp(t, MSK).date(), cls), t)
                        reason += f" (по расписанию {sched['reason']} — верим бирже)"
                        note += "; расписание говорит «открыто», статус биржи главнее"
                out = _finish({"open": bool(open_), "session": session, "reason": reason, "next_open_ts": nxt,
                               "tinkoff_status": st_code, "exchange": exchange, "static": bool(sched.get("static")),
                               "sessions_today": sched.get("sessions_today") or []}, "tinkoff", note)
            else:                                          # статуса нет — по расписанию площадки, честно
                note = (f"статус Tinkoff не получен; по расписанию площадки {exchange}" if not sched.get("static")
                        else "статус Tinkoff не получен; " + _STATIC_NOTE) + ex_note
                out = _finish(sched, "schedule", note)
    except Exception as e:                                 # noqa: BLE001
        log.info("рыночные часы: Tinkoff статус %s не получен: %s", key[0], str(e)[:80])
    if out is None:
        out = _finish(static, "schedule", _STATIC_NOTE + ("" if tinkoff.enabled() else "; токена Tinkoff нет"))
    _status_cache[key] = (time.time(), out)
    _last = out
    return out


async def is_open(ticker: str, asset_class: str | None = None, instrument_id: str | None = None) -> bool:
    try:
        return bool((await status(ticker, asset_class, instrument_id)).get("open"))
    except Exception:                                      # noqa: BLE001
        return True                                        # часы сломались — не блокируем


def describe(st: dict | None) -> str:
    """Строка для ИИ и панели: «рынок закрыт до 07:00 МСК (ночь — торги ещё не начались) — вход возможен
    только с открытия» / «рынок закрыт до 10:00 МСК (выходной: сделки с 10:00 после аукциона 09:50) — …» /
    «рынок закрыт до 07:00 МСК понедельника 28.09 (выходной — на этой площадке торгов нет) — …» /
    «рынок открыт: торги идут, утренняя сессия до 09:50»."""
    if not st:
        return "рынок: статус неизвестен"
    if st.get("open"):
        s = f"рынок открыт: {st.get('reason') or 'торги идут'}"
        if st.get("session") in ("утренняя", "основная", "вечерняя", "выходная") and st["session"] not in s:
            s += f" ({st['session']} сессия)"
    else:
        nxt = st.get("next_open_msk")
        s = "рынок закрыт" + (f" до {nxt}" if nxt else "") + f" ({st.get('reason') or 'торгов нет'})"
        s += " — вход возможен только с открытия"
    if st.get("source") == "schedule":
        s += "; по статическому расписанию" if st.get("static", True) else \
            f"; статус биржи не получен, по расписанию площадки {st.get('exchange') or '?'}"
    return s


def last() -> dict | None:
    """Последний известный статус (панель без ожидания сети)."""
    return _last


async def snapshot(ticker: str | None = None, asset_class: str | None = None) -> dict:
    """Для GET /api/v5/state: {"open","reason","next_open_ts","session","exchange","sessions_today","skew_s",…}."""
    try:
        st = await status(ticker or "SBER", asset_class or "share")
    except Exception as e:                                 # noqa: BLE001
        st = _last or _finish(_static_status("share"), "schedule", f"часы споткнулись: {str(e)[:60]}")
    return {"open": bool(st.get("open")), "reason": st.get("reason"), "next_open_ts": st.get("next_open_ts"),
            "next_open_msk": st.get("next_open_msk"), "next_open_in_s": st.get("next_open_in_s"),
            "session": st.get("session"), "source": st.get("source"), "note": st.get("note"),
            "exchange": st.get("exchange"), "sessions_today": list(st.get("sessions_today") or []),
            "skew_s": round(_skew, 1), "skew_source": _skew_src, "ts": st.get("ts"),
            "msk": st.get("msk"), "enabled": enabled(), "ticker": (ticker or "SBER").upper(),
            "text": describe(st)}


def reset_cache() -> None:
    global _last
    _status_cache.clear()
    _sched_cache.clear()
    _last = None


# ══════════════════════════════════════════════════════════════════════════════
# Self-тест: без сети — фейковые площадки (MOEX_MRNG_EVNG_E_WKND_DLR, FORTS_FUTURES_WEEKEND, старая MOEX),
# GetTradingStatus, ночь/утренняя/паузы/вечерняя/выходные, статус главнее расписания, статика без токена,
# сдвиг часов
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    def msk(y, mo, d, h, mi) -> float:
        return datetime(y, mo, d, h, mi, tzinfo=MSK).timestamp()

    # ── статический регламент (без токена): акции ──
    st = _static_status("share", msk(2026, 9, 23, 3, 15))            # среда, ночь → 07:00, не 10:00
    assert not st["open"] and st["session"] == "закрыто" and "ночь" in st["reason"], st
    assert st["next_open_ts"] == msk(2026, 9, 23, 7, 0), datetime.fromtimestamp(st["next_open_ts"], MSK)
    assert st["static"] and st["exchange"] is None
    st = _static_status("share", msk(2026, 9, 23, 6, 55))             # аукцион открытия
    assert not st["open"] and "аукцион открытия" in st["reason"] and st["next_open_ts"] == msk(2026, 9, 23, 7, 0)
    st = _static_status("share", msk(2026, 9, 23, 7, 30))             # утренняя сессия — открыто
    assert st["open"] and st["session"] == "утренняя" and st["next_open_ts"] is None and "09:50" in st["reason"], st
    st = _static_status("share", msk(2026, 9, 23, 9, 55))             # аукцион открытия основной
    assert not st["open"] and "аукцион открытия основной" in st["reason"] and st["next_open_ts"] == msk(2026, 9, 23, 10, 0)
    st = _static_status("share", msk(2026, 9, 23, 12, 0))             # основная
    assert st["open"] and st["session"] == "основная" and st["next_open_ts"] is None and "18:40" in st["reason"]
    st = _static_status("share", msk(2026, 9, 23, 18, 45))            # аукцион закрытия
    assert not st["open"] and "закрытие основной" in st["reason"] and st["next_open_ts"] == msk(2026, 9, 23, 19, 5)
    st = _static_status("share", msk(2026, 9, 23, 18, 55))            # пауза перед вечерней
    assert not st["open"] and "пауза перед вечерней" in st["reason"] and st["next_open_ts"] == msk(2026, 9, 23, 19, 5)
    st = _static_status("share", msk(2026, 9, 23, 20, 0))             # вечерняя
    assert st["open"] and st["session"] == "вечерняя" and "23:50" in st["reason"]
    st = _static_status("share", msk(2026, 9, 25, 23, 55))            # пятница после закрытия → понедельник 07:00
    assert not st["open"] and "завершены" in st["reason"] and st["next_open_ts"] == msk(2026, 9, 28, 7, 0)
    st = _static_status("share", msk(2026, 9, 26, 12, 0))             # суббота без токена — честно закрыто
    assert not st["open"] and st["session"] == "выходной" and "зависит от бумаги" in st["reason"]
    assert st["next_open_ts"] == msk(2026, 9, 28, 7, 0) and st["sessions_today"] == []
    st = _static_status("share", msk(2026, 11, 4, 12, 0))             # праздник (среда)
    assert not st["open"] and "праздник" in st["reason"] and st["next_open_ts"] == msk(2026, 11, 5, 7, 0)
    sess = [(w["from"], w["to"], w["session"], w["open"]) for w in _static_status("share", msk(2026, 9, 23, 12, 0))["sessions_today"]]
    assert sess == [("06:50", "07:00", "закрыто", False), ("07:00", "09:50", "утренняя", True),
                    ("09:50", "10:00", "закрыто", False), ("10:00", "18:40", "основная", True),
                    ("18:40", "18:50", "закрыто", False), ("18:50", "19:05", "закрыто", False),
                    ("19:05", "23:50", "вечерняя", True)], sess
    # ── фьючерсы и валюта ──
    st = _static_status("future", msk(2026, 9, 23, 14, 2))            # дневной клиринг
    assert not st["open"] and st["session"] == "клиринг" and st["next_open_ts"] == msk(2026, 9, 23, 14, 5)
    st = _static_status("future", msk(2026, 9, 23, 18, 50))           # вечерний клиринг
    assert not st["open"] and st["session"] == "клиринг" and st["next_open_ts"] == msk(2026, 9, 23, 19, 5)
    st = _static_status("future", msk(2026, 9, 23, 7, 30))            # фьючерсы с 07:00
    assert st["open"] and st["session"] == "утренняя" and "10:00" in st["reason"], st
    st = _static_status("future", msk(2026, 9, 23, 12, 0))
    assert st["open"] and st["session"] == "основная" and "14:00" in st["reason"], st
    st = _static_status("future", msk(2026, 9, 23, 3, 0))
    assert not st["open"] and st["next_open_ts"] == msk(2026, 9, 23, 7, 0)
    st = _static_status("currency", msk(2026, 9, 23, 7, 30))
    assert st["open"] and st["session"] == "утренняя" and "10:00" in st["reason"], st
    st = _static_status("currency", msk(2026, 9, 23, 18, 55))               # валюта без пауз
    assert st["open"] and st["session"] == "основная" and "19:05" in st["reason"], st
    assert _static_status("currency", msk(2026, 9, 23, 20, 0))["session"] == "вечерняя"
    assert norm_class("futures") == "future" and norm_class("Акция") == "share" and norm_class("fx") == "currency"

    # ── план дня из ответа биржи: аукцион + 10 мин, брокерский start 02:00 на выходных не берём ──
    d_sat, ok = _broker_day({"date": "2026-09-26", "is_trading_day": True, "opening_auction_start": msk(2026, 9, 26, 9, 50),
                             "start": msk(2026, 9, 26, 2, 0), "end": msk(2026, 9, 26, 23, 49)}, "share")
    assert d_sat == date(2026, 9, 26) and ok["weekend"] and ok["windows"][1]["a"] == msk(2026, 9, 26, 10, 0), ok
    assert ok["windows"][1]["session"] == "выходная" and ok["windows"][-1]["b"] == msk(2026, 9, 26, 19, 0), \
        "выходная сессия акций — сплошное окно до 19:00 (брокерский end 23:49 не берём)"
    assert all(w["open"] for w in ok["windows"][1:]), "на выходных у акций пауз нет"
    _d, no_start = _broker_day({"date": "2026-09-26", "is_trading_day": True}, "share")
    assert no_start is None and _broker_day({"date": "мусор"}, "share") == (None, None)
    _d, no_end = _broker_day({"date": "2026-09-23", "is_trading_day": True, "start": msk(2026, 9, 23, 7, 0)}, "future")
    assert no_end["note"] == "конец дня — по регламенту" and no_end["windows"][-1]["b"] == msk(2026, 9, 23, 23, 50)

    # ── сдвиг часов ──
    _apply_skew(time.time() + 13 * 3600, time.time(), "tinkoff")
    assert 13 * 3600 - 5 <= _skew <= 13 * 3600 + 5 and skew()["source"] == "tinkoff" and "отстают" in skew()["note"]
    assert abs(now() - (time.time() + _skew)) < 1

    # ── фейковый Tinkoff: площадки бумаг и их расписания ──
    EX = {"SBER": "moex_mrng_evng_e_wknd_dlr", "SIZ6": "forts_futures_weekend", "OLD": "moex", "NOEX": ""}

    def _fake_days(ex: str, a: datetime, b: datetime) -> list[dict]:
        out = []
        d = a.astimezone(MSK).date()
        while d <= b.astimezone(MSK).date():
            wk = d.weekday() >= 5
            day0 = _day0(d)
            if ex == "MOEX_MRNG_EVNG_E_WKND_DLR":
                row = ({"is_trading_day": True, "opening_auction_start": day0 + _hm("09:50") * 60,
                        "start": day0 + _hm("02:00") * 60, "end": day0 + _hm("23:49") * 60} if wk else
                       {"is_trading_day": True, "opening_auction_start": day0 + _hm("06:50") * 60,
                        "start": day0 + _hm("07:00") * 60, "end": day0 + _hm("23:49") * 60})
            elif ex == "FORTS_FUTURES_WEEKEND":
                row = ({"is_trading_day": True, "opening_auction_start": day0 + _hm("09:50") * 60,
                        "start": day0 + _hm("10:00") * 60, "end": day0 + _hm("19:00") * 60} if wk else
                       {"is_trading_day": True, "opening_auction_start": day0 + _hm("06:50") * 60,
                        "start": day0 + _hm("07:00") * 60, "end": day0 + _hm("23:50") * 60})
            else:                                          # старая MOEX: выходных нет
                row = ({"is_trading_day": False} if wk else
                       {"is_trading_day": True, "opening_auction_start": day0 + _hm("06:50") * 60,
                        "start": day0 + _hm("07:00") * 60, "end": day0 + _hm("18:54") * 60})
            out.append({"exchange": ex, "date": d.isoformat(), **row})
            d += timedelta(days=1)
        return out

    class FakeTk:
        on = True
        st: dict | None = {"status": "SECURITY_TRADING_STATUS_NORMAL_TRADING", "limit_order": True,
                           "market_order": True, "api_available": True}
        now_ts = time.time()
        posts = 0
        fail = False
        no_days = False
        resolved: list = []
        sched_calls: list = []

        @classmethod
        def enabled(cls):
            return cls.on

        @classmethod
        def server_date(cls):
            return (cls.now_ts, time.time())

        @classmethod
        async def resolve(cls, t, kind):
            cls.resolved.append((t, kind))
            return {"figi": "FIGI-" + t, "uid": "UID-" + t, "exchange": EX.get(t, "moex")}

        @classmethod
        async def trading_status(cls, iid):
            cls.posts += 1
            if cls.fail:
                raise RuntimeError("сети нет")
            return cls.st

        @classmethod
        async def trading_schedules(cls, ex, a, b):
            cls.sched_calls.append(ex)
            return [] if cls.no_days else _fake_days(ex, a, b)

    globals()["tinkoff"] = FakeTk
    CLOSED = {"status": "SECURITY_TRADING_STATUS_NOT_AVAILABLE_FOR_TRADING", "limit_order": False,
              "market_order": False, "api_available": True}
    OPEN = {"status": "SECURITY_TRADING_STATUS_NORMAL_TRADING", "limit_order": True, "market_order": True,
            "api_available": True}

    def at(y, mo, d, h, mi, st: dict | None = None) -> None:
        """Перевести часы биржи на момент (МСК) и сбросить кэш статусов (расписания — остаются)."""
        global _skew, _sync_ts
        _skew = msk(y, mo, d, h, mi) - time.time()
        FakeTk.now_ts = time.time() + _skew
        _sync_ts = time.time()
        _status_cache.clear()
        if st is not None:
            FakeTk.st = st

    async def main():
        global _sync_ts
        # sync: Tinkoff даёт Date → сдвиг
        FakeTk.now_ts = time.time() + 13 * 3600
        _sync_ts = 0.0
        r = await sync(force=True)
        assert r["source"] == "tinkoff" and 13 * 3600 - 5 <= r["skew_s"] <= 13 * 3600 + 5, r
        reset_cache()
        FakeTk.sched_calls.clear()
        # ── акции на MOEX_MRNG_EVNG_E_WKND_DLR: ночь среды → «закрыт до 07:00», не до 10:00 ──
        at(2026, 9, 23, 2, 20, CLOSED)
        s = await status("SBER", "share")
        assert not s["open"] and s["source"] == "tinkoff" and s["exchange"] == "MOEX_MRNG_EVNG_E_WKND_DLR", s
        assert s["next_open_ts"] == msk(2026, 9, 23, 7, 0) and s["next_open_msk"] == "07:00 МСК", s
        assert s["session"] == "закрыто" and s["reason"] == "ночь — торги ещё не начались; биржа: торги недоступны", s
        assert s["next_open_in_s"] == max(0, int(msk(2026, 9, 23, 7, 0) - now()))
        assert FakeTk.sched_calls == ["MOEX_MRNG_EVNG_E_WKND_DLR"], FakeTk.sched_calls
        assert FakeTk.resolved[-1] == ("SBER", "share") and FakeTk.posts == 1
        d = describe(s)
        assert d.startswith("рынок закрыт до 07:00 МСК (ночь") and "вход возможен только с открытия" in d, d
        assert [w["from"] for w in s["sessions_today"] if w["open"]] == ["07:00", "10:00", "19:05"], s["sessions_today"]
        s2 = await status("SBER", "share")
        assert s2 is s and FakeTk.posts == 1, "кэш 60 с"
        # 07:30 — утренняя сессия открыта
        at(2026, 9, 23, 7, 30, OPEN)
        s = await status("SBER", "share", "UID-SBER")
        assert s["open"] and s["session"] == "утренняя" and s["next_open_ts"] is None and s["next_open_in_s"] is None, s
        assert "утренняя сессия до 09:50" in s["reason"] and "рынок открыт" in describe(s), describe(s)
        assert FakeTk.sched_calls == ["MOEX_MRNG_EVNG_E_WKND_DLR"], "расписание площадки — из кэша 6 ч"
        assert await is_open("SBER", "share", "UID-SBER") is True
        # 18:55 — пауза перед вечерней, откроется 19:05
        at(2026, 9, 23, 18, 55, CLOSED)
        s = await status("SBER", "share")
        assert not s["open"] and s["session"] == "закрыто" and "пауза перед вечерней" in s["reason"], s
        assert s["next_open_ts"] == msk(2026, 9, 23, 19, 5) and describe(s).startswith("рынок закрыт до 19:05 МСК"), s
        # 18:55, но биржа говорит NORMAL_TRADING — верим бирже
        at(2026, 9, 23, 18, 55, OPEN)
        s = await status("SBER", "share")
        assert s["open"] and s["session"] == "вечерняя" and "верим бирже" in s["note"], s
        # 12:00 по расписанию основная, но статус «недоступны» — статус главнее, следующее окно 19:05
        at(2026, 9, 23, 12, 0, CLOSED)
        s = await status("SBER", "share")
        assert not s["open"] and s["session"] == "закрыто" and "по расписанию основная" in s["reason"], s
        assert s["next_open_ts"] == msk(2026, 9, 23, 19, 5) and "главнее" in s["note"], s
        # 20:00 вечерняя открыта; 23:55 → завтра 07:00
        at(2026, 9, 23, 20, 0, OPEN)
        s = await status("SBER", "share")
        assert s["open"] and s["session"] == "вечерняя" and "23:49" in s["reason"], s
        at(2026, 9, 23, 23, 55, CLOSED)
        s = await status("SBER", "share")
        assert "завершены" in s["reason"] and s["next_open_ts"] == msk(2026, 9, 24, 7, 0) and s["next_open_msk"] == "завтра 07:00 МСК", s
        # суббота 09:00 — выходной торговый день, сделки с 10:00 после аукциона 09:50 (start 02:00 брокера не берём)
        at(2026, 9, 26, 9, 0, CLOSED)
        s = await status("SBER", "share")
        assert not s["open"] and s["next_open_ts"] == msk(2026, 9, 26, 10, 0), s
        assert s["reason"].startswith("выходной: сделки с 10:00 после аукциона 09:50; биржа:"), s["reason"]
        assert describe(s).startswith("рынок закрыт до 10:00 МСК (выходной: сделки с 10:00 после аукциона 09:50"), describe(s)
        at(2026, 9, 26, 9, 55, CLOSED)
        s = await status("SBER", "share")
        assert "аукцион открытия, сделки с 10:00" in s["reason"] and s["next_open_ts"] == msk(2026, 9, 26, 10, 0), s
        # суббота 12:00 при NORMAL_TRADING — открыто, выходная сессия
        at(2026, 9, 26, 12, 0, OPEN)
        s = await status("SBER", "share")
        assert s["open"] and s["session"] == "выходная" and "(выходная сессия)" not in describe(s) and "выходная сессия до" in s["reason"], s
        # старая площадка MOEX без выходных → в субботу «до 07:00 понедельника»
        at(2026, 9, 26, 12, 0, CLOSED)
        s = await status("OLD", "share")
        assert s["exchange"] == "MOEX" and not s["open"] and s["session"] == "выходной", s
        assert s["next_open_ts"] == msk(2026, 9, 28, 7, 0) and s["next_open_msk"] == "07:00 МСК понедельника 28.09", s
        assert "на этой площадке торгов нет" in s["reason"] and "до 07:00 МСК понедельника" in describe(s), describe(s)
        assert "MOEX" in FakeTk.sched_calls and "MOEX_MRNG_EVNG_E_WKND_DLR" in FakeTk.sched_calls
        # инструмент без поля exchange → площадка по классу, честная пометка
        s = await status("NOEX", "share")
        assert s["exchange"] == "MOEX" and "площадка инструмента неизвестна" in s["note"], s
        # ── фьючерс на FORTS_FUTURES_WEEKEND ──
        at(2026, 9, 23, 14, 2, CLOSED)
        s = await status("SiZ6", "futures")
        assert s["exchange"] == "FORTS_FUTURES_WEEKEND" and FakeTk.resolved[-1] == ("SIZ6", "futures"), (s, FakeTk.resolved[-1])
        assert not s["open"] and s["session"] == "клиринг" and s["next_open_ts"] == msk(2026, 9, 23, 14, 5), s
        at(2026, 9, 23, 7, 30, OPEN)
        s = await status("SiZ6", "futures")
        assert s["open"] and s["session"] == "утренняя" and "до 10:00" in s["reason"], s
        at(2026, 9, 23, 12, 0, OPEN)
        s = await status("SiZ6", "futures")
        assert s["open"] and s["session"] == "основная" and "до 14:00" in s["reason"], s
        at(2026, 9, 26, 18, 30, OPEN)                        # суббота — выходная сессия (клиринг 18:45, день до 19:00)
        s = await status("SiZ6", "futures")
        assert s["open"] and s["session"] == "выходная" and "до 18:45" in s["reason"], s
        assert s["sessions_today"][-1]["to"] == "19:00" and not s["sessions_today"][-1]["open"], s["sessions_today"]
        at(2026, 9, 26, 19, 30, CLOSED)                      # суббота после 19:00 → воскресенье 10:00
        s = await status("SiZ6", "futures")
        assert not s["open"] and "завершены" in s["reason"] and s["next_open_msk"] == "завтра 10:00 МСК", s
        # торги идут, но API-заявки недоступны → закрыто
        at(2026, 9, 23, 12, 0, {"status": "SECURITY_TRADING_STATUS_NORMAL_TRADING", "limit_order": True,
                                "market_order": True, "api_available": False})
        s = await status("SBER", "share", "UID-SBER")
        assert not s["open"] and "API" in s["reason"] and s["session"] == "закрыто", s
        # статус Tinkoff не получен, расписание площадки есть → по расписанию, source schedule, честно
        at(2026, 9, 23, 7, 30, OPEN)
        FakeTk.fail = True
        s = await status("SBER", "share", "UID-SBER")
        assert s["source"] == "schedule" and not s["static"] and s["exchange"] == "MOEX_MRNG_EVNG_E_WKND_DLR", s
        assert s["open"] and s["session"] == "утренняя" and "не получен" in s["note"], s
        assert "по расписанию площадки MOEX_MRNG_EVNG_E_WKND_DLR" in describe(s), describe(s)
        # …и расписания нет → статический регламент с честной пометкой
        reset_cache()
        FakeTk.no_days = True
        s = await status("SBER", "share", "UID-SBER")
        assert s["source"] == "schedule" and s["static"] and "статическое" in s["note"] and "статическому" in describe(s), s
        assert s["open"] == _static_status("share")["open"] and s["exchange"] == "MOEX_MRNG_EVNG_E_WKND_DLR"
        FakeTk.fail, FakeTk.no_days = False, False
        # без токена → регламент; фьючерсы по своему; выходной — честно закрыто
        FakeTk.on = False
        at(2026, 9, 23, 7, 30)
        s = await status("SiZ6", "futures")
        assert s["source"] == "schedule" and "токена" in s["note"] and s["open"] and s["session"] == "утренняя", s
        assert s["exchange"] is None and s["static"]
        at(2026, 9, 26, 12, 0)
        s = await status("SBER", "share")
        assert not s["open"] and "зависит от бумаги" in s["reason"] and "понедельника" in s["next_open_msk"], s
        snap = await snapshot("SBER", "share")
        assert set(snap) >= {"open", "reason", "next_open_ts", "session", "skew_s", "text", "enabled", "exchange", "sessions_today"}, snap
        assert last() is not None and snap["text"] == describe(s)
        # describe без статуса — честно
        assert describe(None) == "рынок: статус неизвестен"
        # sync без Tinkoff и без сети → локальные часы, предупреждение
        globals()["_moex_date"] = _no_net
        _sync_ts = 0.0
        r = await sync(force=True)
        assert r["source"] == "local" and "сети нет" in r["note"], r
        print("market_clock self-test OK")

    async def _no_net():
        return None

    asyncio.run(main())
    sys.exit(0)
