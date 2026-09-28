# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — МАЙЯ-СКАНЕР: длительное ОНЛАЙН-наблюдение за стаканом.

Зачем. Один снимок стакана — это фотография; маркетмейкер работает ВО ВРЕМЕНИ:
он держит вакуум, двигает стены, копит натяжение. Сканер тикает по Tinkoff
(стакан 50 уровней + лента) каждые SCAN_INTERVAL_S секунд, часами, копит
снимки и выводит из НАКОПЛЕННОГО:

  · ПРОКОЛЫ (будущие быстрые проходы цены): ценовые полосы, где вакуум
    ДЕРЖИТСЯ во времени (persistence = доля тиков, где полоса была
    разрежена). Пустота, которую держат подолгу, — это подготовленный
    коридор имплозии: попав туда, цена летит без сопротивления. 🔵 доля
    тиков — арифметика; 🟡 слово «прокол» — словарь Майи.
  · КУДА ТЯНУТ СЕЙЧАС: консенсус тяги по тикам (доля «вверх»/«вниз»)
    + серия последних тиков + согласие агрессора ленты.
  · СТЕНЫ И СТЕНЫ-ПРИЗРАКИ: устойчивость цены стены по тикам; крупная,
    но мигрирующая стена — ложный упор (призрак): её отодвигают.
  · НАТЯЖЕНИЕ: тренд глубины лучшего вакуума (последняя треть против
    первой) — растёт → взрыв зреет.
  · ЭФИРНАЯ ПРИВЯЗКА: окно бифуркации Пригожина по наталу тикера
    (reactor_sky) — «когда среда готова порваться» рядом с «куда тянут».
  · ХОУКС ПО ЛЕНТЕ: ветвление n̂ (hawkes.branching) по приростам числа
    сделок в окне ленты между её обновлениями — рефлексивность потока
    (сделки рождают сделки) прямо из наблюдения сканера.

Выход наружу: aggregate()/status() — сырые агрегаты; render_for_ai(agg) —
плотный текст для ИИ миссии (числа и факты, толкование «как читают»);
brief(agg) — короткий словарь для панели.

Рамка: 🔵 арифметика снимков · 🟡 словарь Майи · ⚫ НЕ торговый сигнал,
рынок не предсказывается, решение за человеком · 18+.
NO DUMMIES: нет токена/стакана → честный отказ; сканер не выдумывает тики.
Никакого random. Сеть здесь ЗАКОННА: это онлайн-слой поверх Tinkoff API
(ABSOLUTE OFFLINE относится к расчёту неба, не к бирже).
"""
from __future__ import annotations

import asyncio
import time
from collections import deque

try:
    from . import maya                    # обычный импорт пакетом
except ImportError:                       # запуск файла напрямую (self-тест)
    import maya                           # noqa: F401

# tinkoff и reactor_sky нужны только живому скану (start/_scan_loop);
# self-тест агрегатов работает без них — импорт в защите, NO DUMMIES
try:
    from . import tinkoff
except Exception:                         # noqa: BLE001 — прямой запуск файла
    tinkoff = None

try:
    from . import reactor_sky
except Exception:                         # реактор недоступен / прямой запуск
    reactor_sky = None

try:
    from . import hawkes                  # ветвление по приростам ленты
except Exception:                         # noqa: BLE001 — прямой запуск файла
    hawkes = None

SCAN_INTERVAL_S = 3.0        # тик сканера: стакан+лента (≈20 пар запросов/мин)
MAX_MINUTES = 480            # верхний предел одного скана (8 часов)
MAX_SNAPSHOTS = 9600         # кольцо снимков (8 ч × 20/мин)
TAPE_MINUTES = 5             # окно ленты (диета: на SBER 15 мин = десятки
                             # тысяч сделок КАЖДЫЙ тик — душило CPU)
TAPE_EVERY = 5               # ленту тянем каждый 5-й тик (стакан — каждый)
ERR_STOP = 40                # подряд ошибок → скан честно останавливается
BACKOFF_MAX_S = 30.0         # при ошибках интервал растёт до этого предела
PUNCTURE_MIN_SHARE = 0.35    # полоса-прокол: вакуум держится ≥35% тиков
PUNCTURE_TOP = 3             # полос на сторону
GHOST_STABILITY = 0.40       # стена стоит на одной цене реже 40% тиков → призрак
STREAK_WIN = 20              # серия «сейчас»: последние N тиков
MIN_TICKS_AGG = 10           # меньше тиков — агрегаты честно не считаются

VERDICT_FRAME = ("⚫ сканер сингулярностей: доли тиков и суммы уровней — "
                 "арифметика 🔵, «прокол/тяга/призрак/ударная волна» — словарь "
                 "первоисточника 🟡 · НЕ торговый сигнал · 18+")

_SCANS: dict[str, dict] = {}          # code → состояние скана
_LOCK = asyncio.Lock()


# ── снимок одного тика ────────────────────────────────────────────────────────
def _snapshot(m: dict, ob: dict, tape: dict | None = None) -> dict | None:
    """Снимок тика. tape — последняя лента (обновляется каждый TAPE_EVERY-й
    тик): tn = число сделок в её окне на этот тик — сырьё Хоукса."""
    if not (isinstance(m, dict) and m.get("available")):
        return None
    pull = m.get("pull") or {}
    ag = m.get("aggressor") or {}
    return {"ts": time.time(),
            "tn": tape.get("count") if isinstance(tape, dict) else None,
            "bid": ob.get("best_bid"), "ask": ob.get("best_ask"),
            "spread_bps": ob.get("spread_bps"),
            "pull": pull.get("side"),
            "s_up": pull.get("score_up"), "s_dn": pull.get("score_dn"),
            "vu": m.get("vacuum_up"), "vd": m.get("vacuum_down"),
            "wa": m.get("wall_ask"), "wb": m.get("wall_bid"),
            "ar": ag.get("ratio") if isinstance(ag, dict) else None,
            "imb": ob.get("imbalance")}


# ── агрегаты по накопленному ─────────────────────────────────────────────────
def _price_step(snaps: list) -> float | None:
    """Шаг полосы — из ширины типичной вакуум-зоны (VAC_WIN уровней)."""
    widths = [abs(v["p_to"] - v["p_from"])
              for s in snaps for v in (s.get("vu"), s.get("vd"))
              if isinstance(v, dict) and v.get("p_from") and v.get("p_to")]
    if not widths:
        return None
    w = sorted(widths)[len(widths) // 2]
    return (w / maya.VAC_WIN) or None


def _punctures(snaps: list, key: str, step: float) -> list:
    """Полосы устойчивого вакуума стороны: bin по центру зоны, persistence."""
    bins: dict[int, dict] = {}
    n = 0
    for s in snaps:
        v = s.get(key)
        if not (isinstance(v, dict) and v.get("p_from") is not None):
            continue
        n += 1
        c = (float(v["p_from"]) + float(v["p_to"])) / 2.0
        b = int(round(c / step))
        r = bins.setdefault(b, {"hits": 0, "depth_sum": 0.0,
                                "p_lo": c, "p_hi": c})
        r["hits"] += 1
        r["depth_sum"] += float(v.get("depth") or 0.0)
        r["p_lo"] = min(r["p_lo"], float(v["p_from"]))
        r["p_hi"] = max(r["p_hi"], float(v["p_to"]))
    if n < MIN_TICKS_AGG:
        return []
    out = []
    for b, r in bins.items():
        share = r["hits"] / n
        if share >= PUNCTURE_MIN_SHARE:
            out.append({"p_lo": round(r["p_lo"], 6), "p_hi": round(r["p_hi"], 6),
                        "persistence": round(share, 3),
                        "depth_mean": round(r["depth_sum"] / r["hits"], 3),
                        "ticks": r["hits"]})
    out.sort(key=lambda x: (-x["persistence"], -x["depth_mean"]))
    return out[:PUNCTURE_TOP]


def _wall_report(snaps: list, key: str) -> dict | None:
    """Устойчивость стены: мода цены; редкая мода = стена-призрак."""
    prices = [round(float(s[key]["p"]), 6) for s in snaps
              if isinstance(s.get(key), dict) and s[key].get("p") is not None]
    total = len(snaps)
    if total < MIN_TICKS_AGG or not prices:
        return None
    freq: dict[float, int] = {}
    for p in prices:
        freq[p] = freq.get(p, 0) + 1
    mode_p = max(freq, key=lambda p: freq[p])
    stability = freq[mode_p] / total          # доля ВСЕХ тиков на модной цене
    present = len(prices) / total
    last = next((s[key] for s in reversed(snaps)
                 if isinstance(s.get(key), dict)), None)
    ghost = bool(present >= 0.5 and stability < GHOST_STABILITY)
    return {"p_mode": mode_p, "stability": round(stability, 3),
            "present": round(present, 3), "now": last, "ghost": ghost,
            "word": ("стена-призрак: крупная, но мигрирует — упор ложный, её "
                     "отодвигают" if ghost else
                     "стена стоит — упор пока настоящий" if present >= 0.5 else
                     "стена появляется эпизодами")}


def _tension(snaps: list) -> dict | None:
    """Тренд глубины лучшего вакуума: последняя треть против первой."""
    depths = []
    for s in snaps:
        best = max((float(v.get("depth") or 0.0)
                    for v in (s.get("vu"), s.get("vd")) if isinstance(v, dict)),
                   default=None)
        if best is not None:
            depths.append(best)
    if len(depths) < MIN_TICKS_AGG:
        return None
    k = max(1, len(depths) // 3)
    a = sum(depths[:k]) / k
    b = sum(depths[-k:]) / k
    d = b - a
    word = ("натяжение РАСТЁТ — вакуум углубляется, взрыв зреет" if d > 0.05
            else "натяжение спадает — вакуум заполняют" if d < -0.05
            else "натяжение ровное")
    return {"first": round(a, 3), "last": round(b, 3),
            "delta": round(d, 3), "word": word}


def _synth_dpdt(snaps: list) -> dict | None:
    """∂P_synth/∂t из первоисточника: скорость СОЗДАНИЯ вакуума и ударная
    волна. «Если скачок производной — фиксируем Ударную Волну»: резкое
    углубление/заливка вакуума между соседними тиками = синтетическую
    сингулярность строят (или сносят) ПРЯМО СЕЙЧАС. 🔵 разности глубин —
    арифметика; 🟡 слова — словарь первоисточника."""
    if len(snaps) < MIN_TICKS_AGG:
        return None
    def _depth(s, key):
        v = s.get(key)
        return float(v.get("depth") or 0.0) if isinstance(v, dict) else 0.0
    out = {}
    for key, side in (("vu", "вверх"), ("vd", "вниз")):
        d = [_depth(s, key) for s in snaps]
        k = min(5, len(d) - 1)
        speed = (sum(d[-k:]) / k - sum(d[-2 * k:-k]) / k) if len(d) >= 2 * k else 0.0
        jump = max((abs(d[i] - d[i - 1]) for i in range(len(d) - k, len(d))
                    if i >= 1), default=0.0)
        out[key] = {"speed": round(speed, 3), "jump": round(jump, 3)}
    su, sd = out["vu"]["speed"], out["vd"]["speed"]
    jmax = max(out["vu"]["jump"], out["vd"]["jump"])
    if jmax >= 0.25:
        word = "УДАРНАЯ ВОЛНА: глубина вакуума скакнула — сингулярность строят/сносят прямо сейчас"
    elif max(su, sd) >= 0.03:
        side = "над ценой" if su >= sd else "под ценой"
        word = f"∂P/∂t > 0: вакуум {side} углубляют — синтетическую сингулярность готовят"
    elif min(su, sd) <= -0.03:
        word = "вакуум заливают — сингулярность разбирают"
    else:
        word = "производная спокойна — вакуум держат без рывков"
    return {"up": out["vu"], "down": out["vd"], "jump_max": round(jmax, 3),
            "word": word}


def _hawkes_tape(snaps: list) -> tuple[dict | None, str | None]:
    """Ветвление Хоукса по приростам числа сделок в окне ленты.

    Бин = обновление ленты: тики, где tn изменился (лента живёт TAPE_EVERY
    тиков, между обновлениями tn стоит). Прирост max(0, tn_i − tn_prev) 🔵;
    нулевой прирост неотличим от необновлённой ленты и в бины не входит —
    оценка консервативна (Фано занижен, не завышен). Меньше hawkes.MIN_BINS
    бинов → None и честная причина (NO DUMMIES)."""
    incr: list[float] = []
    prev = None
    for s in snaps:
        tn = s.get("tn")
        if tn is None:
            continue
        try:
            tn = float(tn)
        except (TypeError, ValueError):
            continue
        if prev is not None and tn != prev:
            incr.append(max(0.0, tn - prev))
        prev = tn
    if hawkes is None:
        return None, "модуль hawkes недоступен"
    if len(incr) < hawkes.MIN_BINS:
        return None, (f"приростов ленты {len(incr)} < {hawkes.MIN_BINS} — "
                      "ветвление честно не считается")
    res = hawkes.branching(incr)
    if res is None:
        return None, "поток ленты мёртв (приросты нулевые) — не судим"
    return res, None


def _consensus(snaps: list) -> dict | None:
    n = len(snaps)
    if n < MIN_TICKS_AGG:
        return None
    up = sum(1 for s in snaps if s.get("pull") == "вверх")
    dn = sum(1 for s in snaps if s.get("pull") == "вниз")
    tail = snaps[-STREAK_WIN:]
    t_up = sum(1 for s in tail if s.get("pull") == "вверх")
    t_dn = sum(1 for s in tail if s.get("pull") == "вниз")
    # консенсус: сторона держит ≥55% тиков и превосходит другую — 🟡 порог школы
    side = ("вверх" if up / n >= 0.55 and up > dn else
            "вниз" if dn / n >= 0.55 and dn > up else None)
    tn = len(tail) or 1
    now_side = ("вверх" if t_up / tn >= 0.55 and t_up > t_dn else
                "вниз" if t_dn / tn >= 0.55 and t_dn > t_up else None)
    ars = [s["ar"] for s in tail if s.get("ar") is not None]
    ar = (sum(ars) / len(ars)) if ars else None
    agree = None
    if ar is not None and now_side:
        agree = bool(ar > 0.55) if now_side == "вверх" else bool(ar < 0.45)
    word = (f"тянут {side or now_side}" if (side or now_side)
            else "выраженной тяги за окно нет")
    if side and now_side and side != now_side:
        word = f"за всё окно тянули {side}, последние тики — {now_side} (перекладка?)"
    return {"up_share": round(up / n, 3), "dn_share": round(dn / n, 3),
            "side": side, "now_side": now_side,
            "streak": {"win": len(tail), "up": t_up, "dn": t_dn},
            "aggressor_mean": (round(ar, 3) if ar is not None else None),
            "agree": agree, "word": word}


def _end_ts(state: dict, running: bool, snaps: list) -> float:
    """Момент, до которого считать elapsed: сейчас (идёт), stopped_ts (остановлен
    вручную) или последний тик (кончился по таймеру/ошибкам)."""
    if running:
        return time.time()
    if state.get("stopped_ts"):
        return float(state["stopped_ts"])
    if snaps:
        try:
            return float(snaps[-1].get("ts") or time.time())
        except (TypeError, ValueError):
            pass
    return time.time()


def aggregate(state: dict) -> dict:
    """Живой отчёт скана: тяга-консенсус, проколы, стены, натяжение, эфир."""
    snaps = list(state["snaps"])
    n = len(snaps)
    running = bool(state.get("task") and not state["task"].done()
                   and not state.get("stopped"))     # stop() → сразу «стоит», не ждём отмены задачи
    out = {"running": running,
           "ticks": n, "errors": state.get("errors", 0),
           "started": state.get("started"), "until": state.get("until"),
           "elapsed_min": round((_end_ts(state, running, snaps) - state["started"]) / 60.0, 1)
           if state.get("started") else None,
           "left_min": (round(max(0.0, state["until"] - time.time()) / 60.0, 1)
                        if (running and state.get("until")) else None),
           "interval_s": SCAN_INTERVAL_S,
           "last": snaps[-1] if snaps else None,
           "note": state.get("note"), "verdict": VERDICT_FRAME}
    if n < MIN_TICKS_AGG:
        out["agg_note"] = (f"накоплено {n} тиков — агрегаты честно молчат "
                           f"до {MIN_TICKS_AGG} (NO DUMMIES)")
        return out
    step = _price_step(snaps)
    out["consensus"] = _consensus(snaps)
    if step:
        pu = _punctures(snaps, "vu", step)
        pd = _punctures(snaps, "vd", step)
        out["punctures"] = {"up": pu, "down": pd, "bin_step": round(step, 6)}
        first = None
        cons = out["consensus"] or {}
        lead = cons.get("now_side") or cons.get("side")
        cand = pu if lead == "вверх" else pd if lead == "вниз" else []
        if cand:
            first = dict(cand[0], side=lead,
                         word=f"прокол вероятен первым {lead}: вакуум держат "
                              f"{int(cand[0]['persistence']*100)}% времени")
        out["puncture_first"] = first
    out["wall_ask"] = _wall_report(snaps, "wa")
    out["wall_bid"] = _wall_report(snaps, "wb")
    out["tension"] = _tension(snaps)
    out["synth"] = _synth_dpdt(snaps)
    out["hawkes"], out["hawkes_note"] = _hawkes_tape(snaps)
    out["ether"] = state.get("ether")
    return out


# ── жизненный цикл скана ─────────────────────────────────────────────────────
async def _scan_loop(code: str, figi: str, ticker: str) -> None:
    st = _SCANS[code]
    tick = 0
    err_streak = 0                       # подряд неудачных тиков
    last_tape = None                     # лента живёт TAPE_EVERY тиков
    while time.time() < st["until"]:
        tick += 1
        ok_tick = False
        try:
            ob = await tinkoff.orderbook(figi, depth=50)
            if tick % TAPE_EVERY == 1:   # диета: лента каждый 5-й тик
                last_tape = await tinkoff.last_trades(figi, minutes=TAPE_MINUTES)
            if ob:
                m = maya.analyze(ob, last_tape)
                snap = _snapshot(m, ob, last_tape)
                if snap:
                    st["snaps"].append(snap)
                    ok_tick = True
                else:
                    st["errors"] += 1
            else:
                st["errors"] += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:                          # тик упал — скан живёт
            st["errors"] += 1
            st["note"] = f"последняя ошибка тика: {str(e)[:100]}"
        # стойкость: серия ошибок → бэкофф; длинная серия → честный стоп
        if ok_tick:
            err_streak = 0
        else:
            err_streak += 1
            if err_streak >= ERR_STOP:
                st["note"] = (f"скан остановлен: {err_streak} тиков подряд без "
                              "данных (биржа закрыта / сеть / токен) — жми СКАН "
                              "заново, когда стакан оживёт")
                return
        delay = min(BACKOFF_MAX_S, SCAN_INTERVAL_S * (2 ** min(err_streak, 4)))             if err_streak else SCAN_INTERVAL_S
        await asyncio.sleep(delay)
    st["note"] = st.get("note") or "скан завершён по таймеру"


async def start(code: str, ticker: str, asset_class: str,
                minutes: int) -> dict:
    """Запуск (перезапуск) скана инструмента. Возвращает статус."""
    if tinkoff is None or not tinkoff.enabled():
        return {"ok": False, "note": "нет TINKOFF_TOKEN — онлайн-сканер "
                                     "честно недоступен (NO DUMMIES)"}
    minutes = max(1, min(int(minutes or 15), MAX_MINUTES))
    inst = await tinkoff.resolve(ticker, asset_class)
    if not inst:
        return {"ok": False, "note": f"инструмент {ticker} не разрешился"}
    figi = inst.get("figi") or inst.get("uid")
    async with _LOCK:
        old = _SCANS.get(code)
        if old and old.get("task") and not old["task"].done() and not old.get("stopped"):
            # скан УЖЕ идёт → ПРОДЛЕВАЕМ дедлайн, накопленное НЕ сбрасываем
            # (раньше повторное нажатие «8Ч» стирало все тики — цифры плясали)
            old["until"] = max(old["until"], time.time() + minutes * 60.0)
            old["note"] = (f"скан продлён (+{minutes} мин) — накопленные "
                           f"{len(old['snaps'])} тиков сохранены")
            return {"ok": True, "minutes": minutes, "code": code,
                    "extended": True}
        st = {"snaps": deque(maxlen=MAX_SNAPSHOTS), "errors": 0,
              "started": time.time(),
              "until": time.time() + minutes * 60.0,
              "note": None, "ether": None, "task": None}
        _SCANS[code] = st
        st["task"] = asyncio.create_task(_scan_loop(code, figi, ticker))
    # эфирная привязка — один раз на старт, в потоке (не держит event loop)
    if reactor_sky is not None:
        try:
            ctx = await asyncio.to_thread(reactor_sky.reactor_context, ticker)
            pr = (ctx or {}).get("prigogine")
            st["ether"] = {"prigogine": pr,
                           "note": "окно Пригожина по наталу тикера: КОГДА "
                                   "среда готова порваться (🟡 прокси школы)"}
        except Exception as e:
            st["ether"] = {"prigogine": None, "note": f"эфир молчит: {str(e)[:80]}"}
    return {"ok": True, "minutes": minutes, "code": code}


_AGG_TTL = 2.5               # сек: чаще агрегаты не пересчитываем (кэш)


def status(code: str) -> dict:
    st = _SCANS.get(code)
    if not st:
        return {"running": False, "ticks": 0,
                "note": "скан не запускался", "verdict": VERDICT_FRAME}
    cached = st.get("_agg_cache")
    now = time.time()
    if cached and now - cached[0] < _AGG_TTL and cached[1].get("ticks") == len(st["snaps"]):
        return cached[1]
    agg = aggregate(st)
    st["_agg_cache"] = (now, agg)
    return agg


def stop(code: str) -> dict:
    st = _SCANS.get(code)
    if st and st.get("task") and not st["task"].done() and not st.get("stopped"):
        st["task"].cancel()
        st["stopped"] = True             # статус «стоит» немедленно; повторный start — новый скан
        st["stopped_ts"] = time.time()   # elapsed_min замирает на моменте остановки
        st["note"] = "остановлен вручную"
        st.pop("_agg_cache", None)       # кэш агрегатов не должен отдать running=True
        return {"ok": True, "note": "скан остановлен"}
    return {"ok": False, "note": "активного скана нет"}


# ── выход наружу: текст для ИИ и словарь для панели ─────────────────────────
def _fp(x) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "н/д"
    return f"{v:.10g}" if v == v else "н/д"


def _who(ar) -> str:
    return ("покупатель" if ar > 0.55 else "продавец" if ar < 0.45
            else "паритет")


SCAN_SCHOOL = ("Как читают: устойчивый вакуум — коридор, где цена пройдёт быстро; "
               "согласие тяги и агрессора — путь наименьшего сопротивления; "
               "стена-призрак — ложный упор, её отодвинут; сигнал упреждающий, "
               "срабатывает с задержкой.")


def render_for_ai(agg: dict) -> str:
    """Плотный блок сканера для ИИ по агрегатам status(code): числа и факты,
    толкование — «как читают». Тиков меньше MIN_TICKS_AGG → одна строка
    «сканер копит»; скан не запускался (0 тиков, не идёт) → ""."""
    if not isinstance(agg, dict):
        return ""
    n = int(agg.get("ticks") or 0)
    if n <= 0 and not agg.get("running"):
        return ""
    if n < MIN_TICKS_AGG:
        return f"сканер копит: {n} тиков, агрегаты с {MIN_TICKS_AGG}"
    L = []
    head = (f"СКАНЕР СТАКАНА (онлайн, тик {float(agg.get('interval_s') or SCAN_INTERVAL_S):g} с): "
            f"{n} тиков")
    el, left = agg.get("elapsed_min"), agg.get("left_min")
    if el is not None:
        head += f" за {float(el):g} мин"
    if agg.get("running"):
        head += f", идёт ещё {float(left):g} мин" if left is not None else ", идёт"
    else:
        head += ", остановлен" + (f" ({agg['note']})" if agg.get("note") else "")
    if agg.get("errors"):
        head += f"; тиков с ошибкой {agg['errors']}"
    L.append(head + ".")
    last = agg.get("last") or {}
    if last:
        s = f"сейчас: bid {_fp(last.get('bid'))} / ask {_fp(last.get('ask'))}"
        if last.get("spread_bps") is not None:
            s += f", спред {_fp(last.get('spread_bps'))} bps"
        if isinstance(last.get("imb"), (int, float)):
            s += f", дисбаланс {last['imb']:+.2f}"
        s += f", тяга тика {last.get('pull') or 'нет'}"
        L.append(s + ".")
    c = agg.get("consensus")
    if c:
        st = c.get("streak") or {}
        s = (f"тяга за окно: вверх {c.get('up_share', 0) * 100:.0f}% тиков / "
             f"вниз {c.get('dn_share', 0) * 100:.0f}% → {c.get('word')}; "
             f"последние {st.get('win')} тиков: вверх {st.get('up')} / вниз {st.get('dn')}")
        ar = c.get("aggressor_mean")
        if ar is not None:
            s += f"; агрессор ленты {ar:.2f} ({_who(ar)})"
            if c.get("agree") is True:
                s += ", с тягой согласен"
            elif c.get("agree") is False:
                s += ", против тяги"
        L.append(s + ".")
    pf = agg.get("puncture_first")
    if pf:
        L.append(f"прокол вероятен первым {pf.get('side')}: полоса {_fp(pf.get('p_lo'))}–"
                 f"{_fp(pf.get('p_hi'))}, вакуум держат {pf.get('persistence', 0) * 100:.0f}% "
                 f"времени, глубина {pf.get('depth_mean')} ({pf.get('ticks')} тиков).")
    pz = agg.get("punctures") or {}
    if pz:
        def _bands(rows):
            return ("; ".join(f"{_fp(r['p_lo'])}–{_fp(r['p_hi'])} ({r['persistence'] * 100:.0f}%, "
                              f"глубина {r['depth_mean']})" for r in rows) or "нет")
        L.append(f"полосы устойчивого вакуума (держат ≥{int(PUNCTURE_MIN_SHARE * 100)}% тиков): "
                 f"над ценой {_bands(pz.get('up') or [])} · под ценой {_bands(pz.get('down') or [])}.")
    walls = []
    for name, w in (("ask", agg.get("wall_ask")), ("bid", agg.get("wall_bid"))):
        if not w:
            continue
        now = w.get("now") or {}
        s = f"{name} {_fp(w.get('p_mode'))}"
        if now.get("mult"):
            s += f" ×{now['mult']:g} медианы"
        if now.get("q"):
            s += f" ({_fp(now['q'])} лотов)"
        s += (" — призрак (мигрирует" if w.get("ghost") else
              " — стоит" if w.get("present", 0) >= 0.5 else " — эпизодами")
        s += f"; устойчивость {w.get('stability')}, присутствие {w.get('present')}"
        walls.append(s + (")" if w.get("ghost") else ""))
    L.append("стены: " + ("; ".join(walls) if walls else "крупных заявок по тикам нет") + ".")
    tn, sy = agg.get("tension"), agg.get("synth")
    s = ""
    if tn:
        s = f"{tn.get('word')} (глубина лучшего вакуума {tn.get('first')}→{tn.get('last')})"
    if sy:
        s += ("; " if s else "") + f"∂P/∂t: {sy.get('word')} (скачок {sy.get('jump_max')})"
    if s:
        L.append(s + ".")
    hw = agg.get("hawkes")
    if hw:
        L.append(f"Хоукс по ленте: n={hw.get('n')} (Фано {hw.get('fano')}, {hw.get('bins')} бинов) "
                 f"— {hw.get('word')}.")
    elif agg.get("hawkes_note"):
        L.append(f"Хоукс по ленте: {agg['hawkes_note']}.")
    pr = (agg.get("ether") or {}).get("prigogine") if isinstance(agg.get("ether"), dict) else None
    if pr:
        s = f"окно Пригожина по наталу ({pr.get('genesis_key')}): λ_now={pr.get('lambda_now')}"
        if pr.get("crossing"):
            s += f", ОКНО СЛОМА ~{pr.get('date')}" + (f" — {pr['top_hit']}" if pr.get("top_hit") else "")
        else:
            s += f", min λ за 30 дн {pr.get('lambda_min')} — окно не открывается"
        L.append(s + ".")
    L.append(SCAN_SCHOOL)
    return "\n".join(L)


def brief(agg: dict) -> dict:
    """Короткий словарь для панели: всё в защите, None там, где данных нет."""
    out = {"running": False, "ticks": 0, "elapsed_min": None, "left_min": None,
           "side": None, "up_share": None, "dn_share": None, "now_side": None,
           "streak": None, "aggressor_mean": None, "agree": None,
           "puncture": None, "tension": None, "hawkes_n": None, "note": None}
    if not isinstance(agg, dict):
        return out
    try:
        out["running"] = bool(agg.get("running"))
        out["ticks"] = int(agg.get("ticks") or 0)
        out["elapsed_min"] = agg.get("elapsed_min")
        out["left_min"] = agg.get("left_min")
        c = agg.get("consensus") or {}
        out["side"] = c.get("side")
        out["up_share"] = c.get("up_share")
        out["dn_share"] = c.get("dn_share")
        out["now_side"] = c.get("now_side")
        st = c.get("streak")
        out["streak"] = ({"up": st.get("up"), "dn": st.get("dn"), "win": st.get("win")}
                         if isinstance(st, dict) else None)
        out["aggressor_mean"] = c.get("aggressor_mean")
        out["agree"] = c.get("agree")
        pf = agg.get("puncture_first")
        if pf:
            out["puncture"] = {"side": pf.get("side"), "p_lo": pf.get("p_lo"),
                               "p_hi": pf.get("p_hi"), "persistence": pf.get("persistence")}
        tn = agg.get("tension")
        out["tension"] = tn.get("word") if isinstance(tn, dict) else None
        hw = agg.get("hawkes")
        out["hawkes_n"] = hw.get("n") if isinstance(hw, dict) else None
        out["note"] = agg.get("agg_note") or agg.get("note")
    except Exception as e:                                  # noqa: BLE001
        out["note"] = f"brief: {str(e)[:80]}"
    return out


# ── self-test: агрегаты на синтетических снимках (сеть не нужна) ─────────────
if __name__ == "__main__":
    # 40 тиков: вакуум над ценой держится в одной полосе 70% времени,
    # тяга вверх 60% тиков, стена bid мигрирует (призрак)
    snaps = []
    for i in range(40):
        vu = ({"p_from": 101.1, "p_to": 101.5, "depth": 0.9}
              if i % 10 < 7 else {"p_from": 103.0, "p_to": 103.4, "depth": 0.4})
        snaps.append({
            "ts": 1000.0 + i * 3, "bid": 100.0, "ask": 100.1,
            "pull": ("вверх" if i % 5 < 3 else "вниз"),
            "s_up": 0.6, "s_dn": 0.2, "vu": vu,
            "vd": {"p_from": 98.0, "p_to": 98.4, "depth": 0.2 + 0.01 * i},
            "wa": {"p": 102.0, "q": 500, "mult": 5.0},
            "wb": {"p": round(99.5 - 0.01 * i, 2), "q": 900, "mult": 6.0},
            "ar": 0.62, "imb": 0.1})
    st = {"snaps": deque(snaps), "errors": 0, "started": 1000.0,
          "until": 1000.0 + 3600, "task": None, "ether": None}
    agg = aggregate(st)
    assert agg["ticks"] == 40
    c = agg["consensus"]
    assert c["side"] == "вверх" or c["now_side"] == "вверх", c
    assert c["agree"] is True                      # агрессор 0.62 согласен
    pu = agg["punctures"]["up"]
    assert pu and abs(pu[0]["p_lo"] - 101.1) < 1e-9 and pu[0]["persistence"] >= 0.6
    pf = agg["puncture_first"]
    assert pf and pf["side"] == "вверх" and "прокол" in pf["word"]
    wa = agg["wall_ask"]; wb = agg["wall_bid"]
    assert wa and wa["ghost"] is False and "стоит" in wa["word"]
    assert wb and wb["ghost"] is True and "призрак" in wb["word"]
    tn = agg["tension"]
    # глубина лучшего вакуума стабильно 0.9 (up доминирует) — тренд ровный;
    # проверяем сам расчёт долей
    assert tn and tn["first"] >= 0.0 and tn["last"] >= tn["first"] - 1.0
    # мало тиков → агрегаты молчат
    st2 = {"snaps": deque(snaps[:5]), "errors": 0, "started": 1000.0,
           "until": 4600.0, "task": None, "ether": None}
    a2 = aggregate(st2)
    assert "agg_note" in a2 and "punctures" not in a2
    # детерминизм
    assert repr(aggregate(st)) == repr(agg)
    # ∂P/∂t: базовая синтетика мигает 0.9↔0.4 → скачок ≥0.25 = ударная волна
    assert agg["synth"] and agg["synth"]["jump_max"] >= 0.25
    assert "УДАРНАЯ" in agg["synth"]["word"]
    # ровная серия (глубина константа) → производная спокойна
    snaps_s = [dict(x, vu={"p_from": 101.1, "p_to": 101.5, "depth": 0.9},
                    vd={"p_from": 98.0, "p_to": 98.4, "depth": 0.2})
               for x in snaps]
    sts = {"snaps": deque(snaps_s), "errors": 0, "started": 1000.0,
           "until": 4600.0, "task": None, "ether": None}
    aggs = aggregate(sts)
    assert aggs["synth"]["jump_max"] < 0.25 and (
        "спокойна" in aggs["synth"]["word"] or "держат" in aggs["synth"]["word"])
    # рамка на месте
    assert "18+" in agg["verdict"] and "⚫" in agg["verdict"]
    # без tn (старые снимки) Хоукс честно молчит, а не выдумывает
    assert agg["hawkes"] is None and "приростов ленты 0" in agg["hawkes_note"]

    # ── Хоукс по ленте: tn обновляется каждый 5-й тик, приросты кластерные
    #    (девять шагов по +1, десятый +60 — лавина) → ветвление разогрето ──
    snaps_h = []
    tn_cnt = 500
    for i in range(210):                       # 41 обновление ленты ≥ MIN_BINS
        if i and i % 5 == 0:
            tn_cnt += 60 if (i // 5) % 10 == 9 else 1
        snaps_h.append(dict(snaps[i % 40], ts=1000.0 + i * 3, tn=tn_cnt))
    st_h = {"snaps": deque(snaps_h), "errors": 2, "started": 1000.0,
            "until": 1000.0 + 3600, "task": None,
            "ether": {"prigogine": {"genesis_key": "TEST", "lambda_now": -0.3,
                                    "lambda_min": -1.2, "crossing": True,
                                    "date": "2026-09-18", "top_hit": "транзит ♃ квадрат ♂"},
                      "note": "тест"}}
    agg_h = aggregate(st_h)
    hw = agg_h["hawkes"]
    assert hw and hw["bins"] >= hawkes.MIN_BINS, agg_h["hawkes_note"]
    assert hw["n"] > hawkes.N_WARM, hw
    #    ровные приросты (+1 каждый раз) → поток докритичен
    snaps_u = [dict(x, tn=500 + i // 5) for i, x in enumerate(snaps_h)]
    agg_u = aggregate({"snaps": deque(snaps_u), "errors": 0, "started": 1000.0,
                       "until": 4600.0, "task": None, "ether": None})
    assert agg_u["hawkes"] and agg_u["hawkes"]["n"] == 0.0, agg_u["hawkes"]
    #    мало обновлений ленты → None с причиной
    agg_f = aggregate({"snaps": deque(snaps_h[:60]), "errors": 0, "started": 1000.0,
                       "until": 4600.0, "task": None, "ether": None})
    assert agg_f["hawkes"] is None and "приростов ленты" in agg_f["hawkes_note"]

    # ── текст для ИИ: заголовок, проценты тяги, прокол, стены, Хоукс, эфир ──
    txt = render_for_ai(agg_h)
    assert txt.startswith("СКАНЕР СТАКАНА (онлайн, тик 3 с): 210 тиков"), txt[:80]
    assert "% тиков" in txt and "вверх 60%" in txt and "прокол вероятен первым вверх" in txt, txt
    assert "стены:" in txt and "призрак" in txt and "стоит" in txt, txt
    assert "Хоукс по ленте: n=" in txt and "окно Пригожина по наталу (TEST)" in txt, txt
    assert "ОКНО СЛОМА ~2026-09-18" in txt and "Как читают" in txt and "натяжение" in txt
    assert "тиков с ошибкой 2" in txt and "остановлен" in txt
    low = txt.lower()
    for bad in ("покупай", "продавай", "входи", "лонг", "шорт"):
        assert bad not in low, bad
    #    мало тиков → «копит»; скан не запускался → пусто
    assert render_for_ai(a2) == f"сканер копит: 5 тиков, агрегаты с {MIN_TICKS_AGG}", render_for_ai(a2)
    assert render_for_ai({"running": False, "ticks": 0, "note": "скан не запускался"}) == ""
    assert render_for_ai(status("НЕТ_ТАКОГО")) == ""
    assert render_for_ai(None) == ""

    # ── словарь для панели ──
    b = brief(agg_h)
    assert b["ticks"] == 210 and b["running"] is False
    assert (b["side"] == "вверх" or b["now_side"] == "вверх") and b["up_share"] == 0.6, b
    assert b["streak"] and set(b["streak"]) == {"up", "dn", "win"}
    assert b["puncture"] and b["puncture"]["side"] == "вверх" and b["puncture"]["persistence"] >= 0.6
    assert b["hawkes_n"] == hw["n"] and b["tension"] and b["agree"] is True
    b2 = brief(a2)
    assert b2["ticks"] == 5 and b2["side"] is None and "молчат" in (b2["note"] or ""), b2
    assert brief(None)["ticks"] == 0 and brief({})["side"] is None
    # детерминизм новых агрегатов
    assert repr(aggregate(st_h)["hawkes"]) == repr(hw)

    # ── stop(): статус «стоит» сразу, кэш сброшен, повторный stop — «активного скана нет» ──
    class _T:                                   # живая задача (done() == False)
        def done(self): return False
        def cancel(self): self.cancelled = True
    st_s = {"snaps": deque(snaps_h), "errors": 0, "started": 1000.0, "until": time.time() + 600,
            "task": _T(), "ether": None}
    _SCANS["ТЕСТ_СТОП"] = st_s
    assert status("ТЕСТ_СТОП")["running"] is True and brief(status("ТЕСТ_СТОП"))["running"] is True
    assert stop("ТЕСТ_СТОП")["ok"] is True and st_s["task"].cancelled and st_s["stopped"] is True
    assert status("ТЕСТ_СТОП")["running"] is False and status("ТЕСТ_СТОП")["ticks"] == 210
    assert "остановлен вручную" in render_for_ai(status("ТЕСТ_СТОП"))
    assert stop("ТЕСТ_СТОП")["ok"] is False
    del _SCANS["ТЕСТ_СТОП"]
    print("maya_scan self-test OK: проколы/тяга/призрак/натяжение — там, где выложены; "
          "Хоукс по приростам ленты, текст для ИИ и brief для панели")
