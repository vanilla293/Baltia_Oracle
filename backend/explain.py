# -*- coding: utf-8 -*-
"""ПИФИЯ v5.3 — ТОЛМАЧ и ПАМЯТЬ МИССИИ.

Воля владельца: «отдельный FLASH, который каждое решение подробно описывает владельцу —
непонятно, что и как происходит, после совета часто одна и та же картинка»; «сжатие с каждым
ключевым узлом: накопилось — ужать, этап прошёл — идём дальше».

ТОЛМАЧ. После ключевых узлов миссии (приказ совета принят, вход исполнен, добор, решение
перепроверки, ответ у троса, закрытие — тейк/стоп/CLOSE/паника, стопор «рынок закрыт /
открылся», отказ биржи с текстом ошибки) FLASH пишет владельцу 3–7 живых предложений: что
произошло, почему (по данным: цена, уровни, новости, совет), что пилот делает дальше — только из
событий (5.4.3: взведённый вход, уровни, когда следующее решение; без пересказа «пилот ждёт…» от себя).
Свободная форма, route "explain". Дебаунс: не чаще раза в MIN_GAP_SEC (20 с) — события
за окно объединяются в одно объяснение; тот же узел с тем же текстом за DEDUP_SEC не повторяется
(отказ биржи каждый тик — одно объяснение). Сбой FLASH → честная заглушка «толмач не ответил»,
этап миссии не страдает. Записи: m.explain (последние KEEP=40: ts, kind, title, text, refs,
events, ok), store через persist-колбэк миссии, шина bus.stage("mission", rid, "explain", …).

ПАМЯТЬ. По узлам (после каждого совета, после закрытия позиции, раз в PYTHIA_MEMORY_EVERY
перепроверок) FLASH сводит накопившееся (старая память, перепроверки, передачи, ответы у троса,
заголовки объяснений толмача — 5.4.3: не тексты, пересказ не становится «фактом» памяти, — сделки, новости
старше последнего совета) в ОДИН связный абзац
≤ MEMORY_LIMIT (1 500) симв. «что было и чем кончилось» с датами — m.memory; после этого сырые
списки миссия режет (последние 5 перепроверок, 3 ответа у троса, 5 передач, 10 объяснений).
Критичное (позиция, уровни, приказ, сделки) не сжимается — оно живёт в своих блоках.
Абзац длиннее лимита дожимает compress.shrink (без дублирования — тот же FLASH-сжиматель).

Модуль не знает класса Mission: объект миссии duck-typed (ticker, name, run_id, explain, memory,
memory_ts, memory_n, reviews, handoffs, pilot.guards), контекст и сохранение — колбэки миссии.
Self-тест (без сети): python3 -m backend.explain
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from . import ai_v5, bus, compress, config

log = logging.getLogger("pythia.explain")

MIN_GAP_SEC = 20.0        # толмач: не чаще одного объяснения в N с (события за окно — в одно)
GATHER_SEC = 1.5          # первое событие ждёт N с — соседние узлы (вход + трос) попадут в то же объяснение
DEDUP_SEC = 600.0         # тот же узел с тем же текстом за N с не объясняем повторно (отказ биржи каждый тик)
KEEP = 40                 # сколько объяснений держим у миссии
PREV_SHOW = 2             # сколько прошлых объяснений видит FLASH (чтобы не рисовать ту же картинку)
FLASH_TIMEOUT = 300.0
PRO_TIMEOUT = 600.0
MEMORY_LIMIT = 1500       # память миссии — один абзац ≤ N симв.
MEMORY_TIMEOUT = 300.0
# что режется после успешной памяти (сколько сырых записей оставить)
KEEP_REVIEWS, KEEP_GUARDS, KEEP_HANDOFFS, KEEP_EXPLAIN = 5, 3, 5, 10

# виды узлов: значок и человеческое имя (фронт и статус)
KINDS = {
    "council": ("🏛", "приказ совета"),
    "entry": ("▶", "вход"),
    "topup": ("➕", "добор"),
    "review": ("🔁", "перепроверка"),
    "guard": ("🪢", "у троса"),
    "take": ("🎯", "у тейка"),
    "entry_check": ("🚪", "у двери"),         # 5.4.1: проверка входа по живому рынку
    "profit": ("💰", "мысль о прибыли"),          # 5.4.1: в плюсе — держать / выйти / перезайти / совет
    "triage": ("⚡", "триаж события"),
    "puncture": ("🕳", "прокол сканера"),
    "close": ("⏹", "закрытие"),
    "market": ("🕰", "рынок"),
    "refusal": ("⚠", "отказ биржи"),
    "other": ("•", "событие"),
}
# при объединении событий в одно объяснение — вид записи по самому важному узлу
_PRIORITY = ("close", "guard", "take", "profit", "entry", "entry_check", "topup", "council", "market", "refusal", "review",
             "puncture", "triage", "other")

_ST: dict[str, dict] = {}     # ticker → {queue, task, last_ts, ctx, persist, recent, mem_task}


def enabled() -> bool:
    return bool(getattr(config, "PYTHIA_EXPLAIN", True))


def model() -> str:
    return "pro" if str(getattr(config, "PYTHIA_EXPLAIN_MODEL", "flash")).lower() == "pro" else "flash"


def memory_every() -> int:
    return max(1, int(getattr(config, "PYTHIA_MEMORY_EVERY", 5)))


def _state(m: Any) -> dict:
    t = str(getattr(m, "ticker", "") or "?").upper()
    st = _ST.get(t)
    if st is None:
        st = {"queue": [], "task": None, "last_ts": 0.0, "ctx": None, "persist": None,
              "recent": {}, "mem_task": None}
        _ST[t] = st
    return st


def reset(ticker: str | None = None) -> None:
    """Забыть очереди (self-тесты, новая миссия по тикеру)."""
    if ticker is None:
        states = list(_ST.values())
        _ST.clear()
    else:
        states = [_ST.pop(str(ticker).upper(), None)]
    for st in states:
        for key in ("task", "mem_task"):
            task = (st or {}).get(key)
            if task and not task.done():
                task.cancel()


async def shutdown_tasks() -> None:
    """Дождаться остановки толмача и памяти перед закрытием AI-клиента."""
    tasks = {st[key] for st in list(_ST.values()) for key in ("task", "mem_task")
             if st.get(key) and not st[key].done()}
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _current(m: Any, st: dict) -> bool:
    return _ST.get(str(getattr(m, "ticker", "") or "?").upper()) is st


def _short(x: Any, n: int) -> str:
    s = str(x or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ── толмач: очередь событий ───────────────────────────────────────────────────
def note(m: Any, kind: str, title: str, detail: str = "", refs: dict | None = None, *,
         ctx: Callable[[], dict] | None = None, persist: Callable[[], None] | None = None) -> bool:
    """Ключевой узел миссии → в очередь толмача; объяснение пойдёт через GATHER_SEC (или когда
    пройдёт MIN_GAP_SEC с прошлого). Синхронно, из любого места петли. Вне цикла событий событие
    ждёт ручного flush(). Возвращает True, если событие принято (не дубль, толмач включён)."""
    if not enabled() or m is None:
        return False
    st = _state(m)
    if ctx is not None:
        st["ctx"] = ctx
    if persist is not None:
        st["persist"] = persist
    kind = kind if kind in KINDS else "other"
    title, detail = _short(" ".join(str(title or "").split()), 200), _short(" ".join(str(detail or "").split()), 1200)   # одна строка на событие
    now = time.time()
    key = (kind, title, detail)
    for k, ts in list(st["recent"].items()):     # чистим старые ключи
        if now - ts > DEDUP_SEC:
            st["recent"].pop(k, None)
    if key in st["recent"]:
        log.debug("толмач %s: повтор узла «%s» — пропускаю", getattr(m, "ticker", "?"), title)
        return False
    st["recent"][key] = now
    st["queue"].append({"ts": now, "kind": kind, "title": title, "detail": detail,
                        "refs": dict(refs or {})})
    _schedule(m, st)
    return True


def pending(m: Any) -> int:
    return len(_state(m)["queue"])


def _schedule(m: Any, st: dict) -> None:
    t = st.get("task")
    if t is not None and not t.done():
        return
    delay = max(GATHER_SEC, st["last_ts"] + MIN_GAP_SEC - time.time())
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:                              # вне цикла — flush() позовут руками
        return
    st["task"] = loop.create_task(_later(m, st, delay))


async def _later(m: Any, st: dict, delay: float) -> None:
    cancelled = False
    try:
        await asyncio.sleep(delay)
        await flush(m)
    except asyncio.CancelledError:
        cancelled = True
        raise
    except Exception as e:                            # noqa: BLE001 — толмач никогда не роняет миссию
        log.warning("толмач %s: %s", getattr(m, "ticker", "?"), str(e)[:120])
    finally:
        if st.get("task") is asyncio.current_task():
            st["task"] = None
        if not cancelled and _current(m, st) and st["queue"]:
            _schedule(m, st)


def _rid(m: Any) -> str | None:
    return getattr(m, "run_id", None)


def _event_line(e: dict) -> str:
    return f"{ai_v5.fmt_ts(e.get('ts'))} · {e.get('title')}" + (f": {e['detail']}" if e.get("detail") else "")


def prompt(ctx: dict, events: list[dict], prev: list[dict]) -> tuple[str, str]:
    """(system, user) толмача. system ≤ 12 строк, свободная форма, время МСК дано."""
    ticker, name = ctx.get("ticker") or "?", ctx.get("name") or ""
    tm = ctx.get("time_msk") or ai_v5.now_msk_str()
    system = (
        f"Ты — толмач миссии по {ticker} ({name}): переводишь владельцу на человеческий, что только что "
        f"сделал пилот и почему. Сейчас {tm}.\n"
        "Дано: что произошло за последние секунды (одно или несколько событий), ситуация пилота (цена, позиция, "
        "план, уровни, P/L), приказ совета, последние решения (перепроверки, ответы у троса, передачи), "
        "память миссии, свежие новости, связанные бумаги и твои прошлые объяснения.\n"
        "Напиши 3–7 живых предложений: что произошло, почему (по данным: цена, уровни, новости, совет), "
        "что пилот делает дальше — только из событий: взведённый вход, уровни, когда следующее решение.\n"
        "Пиши как коллега за соседним столом: свободно, без канцелярита, без заголовков и списков, без "
        "повтора прошлого объяснения — если картина та же, скажи, что изменилось, а что нет.\n"
        "Числа только из данных; чего нет — так и скажи. Не приписывай пилоту действий, которых в событиях нет.\n"
        "Верни только текст объяснения.")
    parts = [f"ОБЪЕКТ: {ticker} — {name}. ВРЕМЯ: {tm}. Режим игры: {ctx.get('play') or '—'}. Цена: {ctx.get('price')}."]
    if ctx.get("market"):
        parts.append(f"РЫНОК: {ctx['market']}.")
    parts += ["", "═══ ЧТО ПРОИЗОШЛО ═══"] + [_event_line(e) for e in events]

    def block(title: str, body: str) -> None:
        b = (body or "").strip()
        if b:
            parts.extend(["", f"═══ {title} ═══", b])

    block("СИТУАЦИЯ ПИЛОТА", ctx.get("situation", ""))
    block("ПРИКАЗ СОВЕТА", ctx.get("exec", ""))
    block("ПОСЛЕДНИЕ РЕШЕНИЯ", ctx.get("decisions", ""))
    block("ПАМЯТЬ МИССИИ (что было и чем кончилось)", ctx.get("memory", ""))
    block("СВЕЖИЕ НОВОСТИ (после совета, время МСК)", ctx.get("news", ""))
    block("СВЯЗАННЫЕ БУМАГИ", ctx.get("partners", ""))
    if prev:
        block("ТВОИ ПРОШЛЫЕ ОБЪЯСНЕНИЯ", "\n".join(f"{ai_v5.fmt_ts(p.get('ts'))} · {p.get('text')}" for p in prev))
    parts += ["", "Объясни владельцу, что произошло и что дальше."]
    return system, "\n".join(parts)


def _stub(events: list[dict], err: str) -> str:
    """Честная заглушка: толмач не ответил — по факту, что случилось."""
    facts = "; ".join(_event_line(e) for e in events)
    return f"Толмач не ответил ({_short(err, 120) or 'сбой ИИ'}). По факту: {facts}."


async def _ask(system: str, user: str, route: str, timeout: float | None = None) -> str:
    if model() == "pro":
        return await asyncio.wait_for(ai_v5.pro_text(system, user, route=route), timeout or PRO_TIMEOUT)
    return await asyncio.wait_for(ai_v5.flash_text(system, user, route=route), timeout or FLASH_TIMEOUT)


async def flush(m: Any) -> dict | None:
    """Объяснить всё, что накопилось в очереди, одной записью. Сбой ИИ → заглушка, запись всё равно
    есть (ok=False). Возвращает запись или None (очередь пуста)."""
    st = _state(m)
    events, st["queue"] = list(st["queue"]), []
    if not events:
        return None
    rid = _rid(m)
    titles = " · ".join(dict.fromkeys(e["title"] for e in events))
    kind = min((e["kind"] for e in events), key=lambda k: _PRIORITY.index(k) if k in _PRIORITY else 99)
    refs: dict = {}
    for e in events:
        refs.update(e.get("refs") or {})
    try:
        await bus.stage("mission", rid, "explain", "start", ticker=getattr(m, "ticker", None),
                        detail=f"толмач пишет: {titles}")
    except Exception:                                 # noqa: BLE001
        pass
    ctx: dict = {}
    try:
        if st.get("ctx"):
            ctx = st["ctx"]() or {}
    except Exception as e:                            # noqa: BLE001
        log.info("толмач %s: контекст не собрался: %s", getattr(m, "ticker", "?"), str(e)[:100])
    ctx.setdefault("ticker", getattr(m, "ticker", "?"))
    ctx.setdefault("name", getattr(m, "name", ""))
    ctx.setdefault("memory", getattr(m, "memory", "") or "")
    prev = [x for x in (getattr(m, "explain", None) or []) if x.get("ok")][-PREV_SHOW:]
    ok, text = True, ""
    try:
        blocks = await compress.fit({k: ctx.get(k) or "" for k in ("situation", "exec", "decisions", "memory",
                                                                     "news", "partners") if ctx.get(k)})
        ctx.update(blocks)
        s, u = prompt(ctx, events, prev)
        text = (await _ask(s, u, "explain") or "").strip()
        if not text:
            raise RuntimeError("пустой ответ")
    except Exception as e:                            # noqa: BLE001
        ok, text = False, _stub(events, str(e))
        log.info("толмач %s: %s", getattr(m, "ticker", "?"), str(e)[:100])
    if not _current(m, st):
        return None
    rec = {"ts": time.time(), "kind": kind, "title": titles, "text": text, "refs": refs,
           "events": [{k: e.get(k) for k in ("ts", "kind", "title", "detail")} for e in events],
           "ok": ok, "model": model()}
    lst = getattr(m, "explain", None)
    if not isinstance(lst, list):
        lst = []
        try:
            m.explain = lst
        except Exception:                             # noqa: BLE001
            pass
    lst.append(rec)
    del lst[:-KEEP]
    st["last_ts"] = time.time()
    try:
        await bus.stage("mission", rid, "explain", "done" if ok else "error", ticker=getattr(m, "ticker", None),
                        detail=(titles if ok else f"толмач не ответил — {titles}"), data=rec)
    except Exception:                                 # noqa: BLE001
        pass
    try:
        if _current(m, st) and st.get("persist"):
            st["persist"]()
    except Exception as e:                            # noqa: BLE001
        log.info("толмач %s: не сохранился: %s", getattr(m, "ticker", "?"), str(e)[:80])
    if st["queue"]:                                   # пока писал — прилетело ещё: следующее окно
        _schedule(m, st)
    return rec


def items(m: Any, n: int = KEEP) -> list[dict]:
    lst = getattr(m, "explain", None) or []
    return [{k: x.get(k) for k in ("ts", "kind", "title", "text", "refs", "ok", "model")} for x in lst[-n:]]


def text(m: Any, n: int = 10) -> str:
    """Объяснения толмача строками: «HH:MM · заголовок — текст» (5.4.3: в память идут только заголовки — _accumulated)."""
    lst = getattr(m, "explain", None) or []
    return "\n".join(f"{ai_v5.fmt_ts(x.get('ts'))} · {x.get('title')} — {x.get('text')}" for x in lst[-n:])


# ── память миссии: сжатие по узлам ────────────────────────────────────────────
def memory_prompt(ctx: dict, why: str, accumulated: str, old: str) -> tuple[str, str]:
    ticker, name = ctx.get("ticker") or "?", ctx.get("name") or ""
    tm = ctx.get("time_msk") or ai_v5.now_msk_str()
    system = (
        f"Ты — память миссии по {ticker} ({name}). Сейчас {tm}. Повод свести память: {_short(why, 160)}.\n"
        "Дано: прошлая память (если была), накопившееся с тех пор — перепроверки, проверки входа у двери, мысли о "
        "прибыли, передачи совету, ответы у троса и тейка, заголовки объяснений толмача (пересказ, не факт), сделки, "
        "новости до последнего совета — и текущий приказ для ориентира.\n"
        f"Сведи всё в ОДИН связный абзац не длиннее {MEMORY_LIMIT} символов: что было и чем кончилось, с датами "
        "и временем МСК, с ключевыми числами (входы, стопы, тейки, P/L), какие идеи отработали, какие умерли и "
        "почему; что дали решения — вход, выход, отмена, ожидание, удержание: цена при решении и куда она ушла после "
        "(в %) и в чью пользу: для ожидания — сколько прошла цена без нас в сторону идеи, для входа — за нас или "
        "против; без советов; что из новостей ещё держит рынок.\n"
        "Прошлую память не переписывай с нуля — ужми и продолжи; что уже ни на что не влияет, отпускай. Текущий "
        "приказ дан только как ориентир — в абзац его не переписывай, он живёт в своём блоке.\n"
        "Только факты из данных, без советов и прогнозов; «решения не было» (модель не ответила или ответ не "
        "разобран) — сбой, а не решение ждать. Верни только абзац.")
    parts = [f"ОБЪЕКТ: {ticker} — {name}. ВРЕМЯ: {tm}. Цена: {ctx.get('price')}."]
    if old.strip():
        parts += ["", "═══ ПРОШЛАЯ ПАМЯТЬ ═══", old.strip()]
    parts += ["", "═══ НАКОПИЛОСЬ С ТЕХ ПОР ═══", accumulated.strip() or "(ничего нового)"]
    if ctx.get("exec"):
        parts += ["", "═══ ТЕКУЩИЙ ПРИКАЗ (ориентир — в абзац не включать) ═══", str(ctx["exec"]).strip()]
    parts += ["", f"Сведи в один абзац ≤ {MEMORY_LIMIT} символов."]
    return system, "\n".join(parts)


_SILENT = ("НЕТ_ОТВЕТА", "НЕ_РАЗОБРАН")      # v5.4.2: сбой ИИ записан под своим именем — это не решение


def _num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _px(v: float) -> str:
    return repr(round(v, 6))


_SIDE_RU = {"long": "лонг", "short": "шорт"}
_CHOICE_SIDE = {"КУПИТЬ_СЕЙЧАС": "long", "ПРОДАТЬ_СЕЙЧАС": "short"}     # вход перепроверки — сторона из слова
_CHOICE_LABEL = {"КУПИТЬ_СЕЙЧАС": "КУПИТЬ", "ПРОДАТЬ_СЕЙЧАС": "ПРОДАТЬ"}


def choice_label(r: dict | None) -> str:
    """Ревью 5.4.3: решение перепроверки так, как модель его выбирала (та же карта, что mission._choice_label — толмач
    duck-typed и миссию не импортирует): КУПИТЬ_СЕЙЧАС / ПРОДАТЬ_СЕЙЧАС — «КУПИТЬ» / «ПРОДАТЬ», ЖДЁМ в позиции —
    «ДЕРЖАТЬ». Только подача модели: канон в записи прежний."""
    c = str((r or {}).get("choice") or "")
    if c in _CHOICE_LABEL:
        return _CHOICE_LABEL[c]
    return "ДЕРЖАТЬ" if c == "ЖДЁМ" and (r or {}).get("in_pos") else c


def _pc(v: float) -> str:
    """Процент со знаком: ≥ 0.1 — одна цифра, меньше — две; «-0.00» → «+0.00»."""
    s = f"{v:+.1f}" if abs(v) >= 0.1 else f"{v:+.2f}"
    return "+" + s[1:] if s.startswith("-") and float(s) == 0 else s


def _since(price: Any, now: Any, how: str = "", side: str | None = None) -> str:
    """Цена решения и ход с тех пор: « @100.0 → сейчас 101.4 (+1.4 %)»; без текущей цены — « @100.0».
    5.4.3 — в чью пользу: how="wait" (ожидание вне рынка) — «(лонг отсюда +1.4 %, шорт -1.4 %)»; how="plan" (ответ у
    двери по плану стороны side) — «(+1.4 % в сторону плана)»; how="deal" (вход / позиция стороны side) —
    «(за лонг +1.4 %)». Только числа, без оценки."""
    p0 = _num(price)
    if p0 is None:
        return ""
    p1 = _num(now)
    if p1 is None:
        return f" @{_px(p0)}"
    pct = (p1 / p0 - 1) * 100
    if how == "wait":
        tail = f"лонг отсюда {_pc(pct)} %, шорт {_pc(-pct)} %"
    elif how in ("plan", "deal") and side in _SIDE_RU:
        d = pct if side == "long" else -pct
        tail = f"{_pc(d)} % в сторону плана" if how == "plan" else f"за {_SIDE_RU[side]} {_pc(d)} %"
    else:
        tail = f"{_pc(pct)} %"
    return f" @{_px(p0)} → сейчас {_px(p1)} ({tail})"


def _decided(rec: dict, key: str, now: Any, how: str = "", side: str | None = None, label: str | None = None) -> str:
    """Запись узла строкой: «ЖДЁМ @100.0 → сейчас 101.4 (лонг отсюда +1.4 %, шорт -1.4 %) — почему». Сбой ИИ
    (silent, НЕТ_ОТВЕТА, НЕ_РАЗОБРАН) — «(модель не ответила — решения не было)», а не ЖДАТЬ: хода цены к нему не
    приписываем. label — как показать решение (ревью 5.4.3: метка модели, choice_label), нет — как в записи."""
    d = str(rec.get(key) or "")
    if rec.get("silent") or d.upper() in _SILENT:
        return ("(ответ модели не разобран — решения не было)" if d.upper() == "НЕ_РАЗОБРАН"
                else "(модель не ответила — решения не было)")
    return f"{label or d}{_since(rec.get('price'), now, how, side)} — {rec.get('why') or ''}"


def _pos_side_at(pos: Any, ts: Any) -> str | None:
    """Сторона позиции пилота, если решение ts принято уже в ней (позиция открыта раньше решения), иначе None.
    Хранится только текущая позиция — про прошлые круги запись не знает (тогда None)."""
    if not isinstance(pos, dict) or pos.get("side") not in _SIDE_RU:
        return None
    try:
        return pos["side"] if float(ts or 0) >= float(pos.get("opened_ts") or 0) > 0 else None
    except (TypeError, ValueError):
        return None


def _review_how(r: dict, pos: Any) -> tuple[str, str | None]:
    """Как показать ход цены после перепроверки: вход — «за лонг/шорт»; в позиции (in_pos в записи, если есть, или
    текущая позиция открыта раньше решения) — «за» сторону позиции (ПЕРЕВЕРНУТЬ — за новую сторону); вне рынка
    (ЖДЁМ / НОВЫЙ_АНАЛИЗ / ВНЕ_РЕЖИМА без позиции) — обе стороны: «лонг отсюда …, шорт …». Ревью 5.4.3: сторона — из
    записи (pos_side, пишет mission._review с 5.4.3), нет — от текущей позиции, открытой раньше решения, и при in_pos
    True тоже (записи 5.4.3 до правки: in_pos есть, pos_side нет)."""
    c = str(r.get("choice") or "").upper()
    if c in _CHOICE_SIDE:
        return "deal", _CHOICE_SIDE[c]
    side = r.get("pos_side") if r.get("pos_side") in _SIDE_RU else None
    in_pos = r.get("in_pos")
    if in_pos is None or (in_pos and not side):
        side = side or _pos_side_at(pos, r.get("ts"))
        in_pos = bool(in_pos) or side is not None
    if in_pos or c in ("ДОБРАТЬ", "ПЕРЕВЕРНУТЬ"):     # ходы только из позиции: сторона неизвестна — просто ход цены
        if side and c == "ПЕРЕВЕРНУТЬ":
            side = "short" if side == "long" else "long"
        return ("deal", side) if side else ("", None)
    return "wait", None


def _review_row(r: dict, pos: Any, now: Any) -> str:
    """Перепроверка строкой для памяти: метка модели (choice_label; ЖДЁМ, принятый в позиции, — «ДЕРЖАТЬ» и у старых
    записей без in_pos) и ход цены в чью пользу (_review_how)."""
    how, side = _review_how(r, pos)
    lab = choice_label(r)
    if lab == "ЖДЁМ" and how == "deal":
        lab = "ДЕРЖАТЬ"
    return _decided(r, "choice", now, how, side, label=lab)


def _accumulated(m: Any, ctx: dict) -> str:
    """Что сводим: перепроверки, проверки у двери, мысли о прибыли, передачи, ответы у троса и тейка,
    заголовки объяснений, сделки, старые новости. v5.4.2: у решений с ценой — ход с тех пор до текущей цены (ctx
    price): что дало ожидание, видно числом; молчание модели — «решения не было», не ЖДАТЬ. 5.4.3: в чью пользу —
    ожидание вне рынка «лонг отсюда …, шорт …», дверь «… в сторону плана», вход и позиция «за лонг/шорт …»; толмач —
    только заголовки (не пересказ)."""
    L: list[str] = []
    now = ctx.get("price")
    p = getattr(m, "pilot", None)
    pos = getattr(p, "position", None) if p is not None else None
    rv = getattr(m, "reviews", None) or []
    if rv:
        L.append("Перепроверки: " + "; ".join(f"{ai_v5.fmt_ts(r.get('ts'))} {_review_row(r, pos, now)}" for r in rv))
    since_mem = float(getattr(m, "memory_ts", None) or 0.0)     # двери и прибыль пилот не режет — только новое
    for attr, title in (("gates", "Проверки входа у двери"), ("profits", "Мысли о прибыли")):
        xs = [x for x in (list(getattr(p, attr, None) or []) if p is not None else [])
              if isinstance(x, dict) and float(x.get("ts") or 0) > since_mem]
        if xs:
            # 5.4.3: дверь — «в сторону плана» (ВОЙТИ — «за» сторону сделки); прибыль — «за» сторону позиции
            L.append(f"{title}: " + "; ".join(
                f"{ai_v5.fmt_ts(x.get('ts'))} "
                + _decided(x, "decision", now,
                           "plan" if attr == "gates" and str(x.get("decision") or "") != "ВОЙТИ" else "deal",
                           x.get("side") if x.get("side") in _SIDE_RU else None)
                for x in xs))
    hs = getattr(m, "handoffs", None) or []
    if hs:
        L.append("Передачи: " + "; ".join(f"{ai_v5.fmt_ts(h.get('ts'))} [{h.get('kind') or 'council'}] {h.get('reason') or ''}"
                                          for h in hs))
    gs = list(getattr(p, "guards", None) or []) if p is not None else []
    if gs:
        # ревью 5.4.3: сторона позиции — из записи ответа (pos_side, пишет AIPilot._apply_guard/_apply_take): память
        # сводится и после закрытия, когда текущей позиции уже нет; старые записи — от текущей позиции
        L.append("Ответы у троса и тейка: " + "; ".join(
            f"{ai_v5.fmt_ts(g.get('ts'))} {'у тейка ' if g.get('side') == 'take' else ''}"
            f"{_decided(g, 'decision', now, 'deal', g.get('pos_side') if g.get('pos_side') in _SIDE_RU else _pos_side_at(pos, g.get('ts')))}"
            for g in gs))
    # 5.4.3: толмач — только заголовки: его тексты — пересказ («пилот ждёт пробоя…»), в памяти он становился «фактом»;
    # факты уже есть в перепроверках, дверях, мыслях о прибыли, ответах у троса и в сделках
    ex = [x for x in (getattr(m, "explain", None) or [])[-KEEP:] if isinstance(x, dict) and x.get("title")]
    if ex:
        L.append("Толмач писал владельцу (только заголовки, пересказ не факт): "
                 + "; ".join(f"{ai_v5.fmt_ts(x.get('ts'))} {x.get('title')}" for x in ex))
    for key, title in (("trades", "Сделки"), ("news_old", "Новости до последнего совета")):
        if ctx.get(key):
            L.append(f"{title}:\n{str(ctx[key]).strip()}")
    return "\n".join(L)


def memory_status(m: Any) -> dict:
    return {"text": getattr(m, "memory", "") or "", "ts": getattr(m, "memory_ts", None) or None,
            "n": int(getattr(m, "memory_n", 0) or 0), "limit": MEMORY_LIMIT}


async def memorize(m: Any, why: str, *, ctx: Callable[[], dict] | None = None,
                   persist: Callable[[], None] | None = None) -> str | None:
    """Свести накопившееся в абзац ≤ MEMORY_LIMIT → m.memory; успех → сырые списки режутся
    (последние KEEP_REVIEWS/KEEP_GUARDS/KEEP_HANDOFFS/KEEP_EXPLAIN). Сбой ИИ → память прежняя,
    списки не тронуты, стадия «memory» error. Возвращает абзац или None."""
    if not enabled() or m is None:
        return None
    st = _state(m)
    if ctx is not None:
        st["ctx"] = ctx
    if persist is not None:
        st["persist"] = persist
    rid = _rid(m)
    c: dict = {}
    try:
        if st.get("ctx"):
            c = st["ctx"]() or {}
    except Exception as e:                            # noqa: BLE001
        log.info("память %s: контекст не собрался: %s", getattr(m, "ticker", "?"), str(e)[:100])
    c.setdefault("ticker", getattr(m, "ticker", "?"))
    c.setdefault("name", getattr(m, "name", ""))
    old = getattr(m, "memory", "") or ""
    acc = _accumulated(m, c)
    if not acc.strip() and not old.strip():
        return None                                   # сводить нечего
    try:
        await bus.stage("mission", rid, "memory", "start", ticker=getattr(m, "ticker", None),
                        detail=f"память миссии: свожу накопившееся ({why})")
    except Exception:                                 # noqa: BLE001
        pass
    try:
        acc = await compress.shrink(acc, label="накопившееся миссии")   # выше PYTHIA_CTX_LIMIT — ужать, ниже — как есть
        s, u = memory_prompt(c, why, acc, old)
        out = (await asyncio.wait_for(ai_v5.flash_text(s, u, route="memory"), MEMORY_TIMEOUT) or "").strip()
        if not out:
            raise RuntimeError("пустой ответ")
        if len(out) > MEMORY_LIMIT:
            out = await compress.shrink(out, MEMORY_LIMIT, "память миссии")
        if len(out) > MEMORY_LIMIT + 200:
            out = compress.clip(out, MEMORY_LIMIT, "память миссии")
    except Exception as e:                            # noqa: BLE001
        log.info("память %s: %s — сырые записи остаются", getattr(m, "ticker", "?"), str(e)[:100])
        try:
            await bus.stage("mission", rid, "memory", "error", ticker=getattr(m, "ticker", None),
                            detail=f"память не сведена: {str(e)[:100]} — сырые записи остаются")
        except Exception:                             # noqa: BLE001
            pass
        return None
    if not _current(m, st):
        return None
    m.memory, m.memory_ts, m.memory_n = out, time.time(), int(getattr(m, "memory_n", 0) or 0) + 1
    # этап прошёл — сырые списки ужимаем; критичное (позиция, уровни, приказ, сделки) не трогаем
    try:
        rv = getattr(m, "reviews", None)
        if isinstance(rv, list):
            del rv[:-KEEP_REVIEWS]
        hs = getattr(m, "handoffs", None)
        if isinstance(hs, list):
            del hs[:-KEEP_HANDOFFS]
        p = getattr(m, "pilot", None)
        gs = getattr(p, "guards", None) if p is not None else None
        if isinstance(gs, list):
            del gs[:-KEEP_GUARDS]
        ex = getattr(m, "explain", None)
        if isinstance(ex, list):
            del ex[:-KEEP_EXPLAIN]
    except Exception as e:                            # noqa: BLE001
        log.info("память %s: списки не ужались: %s", getattr(m, "ticker", "?"), str(e)[:80])
    try:
        await bus.stage("mission", rid, "memory", "done", ticker=getattr(m, "ticker", None),
                        detail=f"память миссии сведена ({len(out)} симв., {why})", data=memory_status(m))
    except Exception:                                 # noqa: BLE001
        pass
    try:
        if _current(m, st) and st.get("persist"):
            st["persist"]()
    except Exception as e:                            # noqa: BLE001
        log.info("память %s: не сохранилась: %s", getattr(m, "ticker", "?"), str(e)[:80])
    return out


def memorize_bg(m: Any, why: str, *, ctx: Callable[[], dict] | None = None,
                persist: Callable[[], None] | None = None) -> bool:
    """Свести память фоном (не блокируя петлю); уже идёт → False."""
    if not enabled() or m is None:
        return False
    st = _state(m)
    t = st.get("mem_task")
    if t is not None and not t.done():
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    st["mem_task"] = loop.create_task(memorize(m, why, ctx=ctx, persist=persist))
    return True


def status(m: Any) -> dict:
    """Для mission.status(): объяснения и память."""
    return {"items": items(m), "memory": memory_status(m), "enabled": enabled(), "model": model(),
            "pending": pending(m)}


# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    from types import SimpleNamespace

    class FakeAI:
        calls: list = []
        users: list = []
        fail = False
        now_msk_str = staticmethod(ai_v5.now_msk_str)
        fmt_ts = staticmethod(ai_v5.fmt_ts)

        async def flash_text(self, system, user, *, think=False, route="flash", max_tokens=None):
            self.calls.append(route)
            self.users.append(user)
            if self.fail:
                raise RuntimeError("сеть")
            if route == "memory":
                return "ПАМЯТЬ: 23.09 совет BUY 100, вход 99.5, тейк 103, стоп 98; перепроверки ЖДЁМ; всё кончилось хорошо."
            if route == "shrink":
                lim = int(user.split("ЛИМИТ: ")[1].split(" ")[0])
                return user.split("\n\nТЕКСТ:\n", 1)[1][:lim]
            return "Пилот вошёл в лонг 8 лотов по 99.52, потому что цена подошла к засаде. Дальше ждём тейк 103."

        async def pro_text(self, system, user, *, route="pro", max_tokens=None):
            self.calls.append("pro:" + route)
            return "PRO-объяснение"

    fake = FakeAI()
    globals()["ai_v5"] = fake
    compress.ai_v5 = fake
    events: list = []

    async def sink(ev):
        events.append(ev)

    persisted: list = []

    def mk():
        m = SimpleNamespace(ticker="TEST", name="Тест", run_id="mission-1", explain=[], memory="", memory_ts=None,
                            memory_n=0, reviews=[{"ts": time.time() - 100 * i, "choice": "ЖДЁМ", "why": f"план жив {i}", "price": 100}
                                                  for i in range(8)],
                            handoffs=[{"ts": time.time(), "kind": "pilot", "reason": f"повод {i}"} for i in range(7)],
                            pilot=SimpleNamespace(guards=[{"ts": time.time(), "decision": "ЖДАТЬ", "price": 97, "why": f"г{i}"}
                                                          for i in range(5)]))
        return m

    def ctx_of(m):
        return lambda: {"ticker": m.ticker, "name": m.name, "play": "long", "price": 99.52,
                        "situation": "ПОЗИЦИЯ: long 8 лот @99.52", "exec": "Приказ: BUY entry=99.5 take=103 inv=98",
                        "decisions": "перепроверка ЖДЁМ", "news": "[a1b2c3] 23.09 12:00 · rbc · ЦБ", "partners": "VTBR ρ=+0.81",
                        "market": "рынок открыт: торги идут", "news_old": "[old001] 22.09 · старая новость",
                        "trades": "long 8 лот 99.5→100.2 P/L +560"}

    async def main():
        bus.set_sink(sink)
        config.PYTHIA_EXPLAIN = True
        config.PYTHIA_EXPLAIN_MODEL = "flash"
        assert enabled() and model() == "flash"
        m = mk()
        rid = bus.start_run("mission", {"ticker": "TEST"})
        m.run_id = rid
        # ── дебаунс: два узла подряд → одно объяснение; system ≤ 12 строк; блоки в промпте ──
        assert note(m, "entry", "Вход исполнен", "long 8 лот @99.52", {"price": 99.52}, ctx=ctx_of(m),
                    persist=lambda: persisted.append(1))
        assert note(m, "guard", "FLASH у троса: ЖДАТЬ", "ложный прокол", {"trigger": 98})
        assert not note(m, "guard", "FLASH у троса: ЖДАТЬ", "ложный прокол"), "тот же узел — дубль"
        assert pending(m) == 2 and _state(m)["task"] is not None
        for _ in range(400):
            if m.explain:
                break
            await asyncio.sleep(0.01)
        assert len(m.explain) == 1, m.explain
        rec = m.explain[0]
        assert rec["ok"] and rec["kind"] == "guard" and "Вход исполнен" in rec["title"] and "FLASH у троса" in rec["title"]
        assert rec["refs"] == {"price": 99.52, "trigger": 98} and len(rec["events"]) == 2 and rec["model"] == "flash"
        assert "вошёл в лонг" in rec["text"] and persisted == [1]
        u = fake.users[-1]
        s, _ = prompt(ctx_of(m)(), rec["events"], [])
        assert s.count("\n") < 12 and "МСК" in s and "толмач" in s.lower()
        for piece in ("═══ ЧТО ПРОИЗОШЛО ═══", "Вход исполнен: long 8 лот @99.52", "FLASH у троса: ЖДАТЬ: ложный прокол",
                      "═══ СИТУАЦИЯ ПИЛОТА ═══", "═══ ПРИКАЗ СОВЕТА ═══", "═══ ПОСЛЕДНИЕ РЕШЕНИЯ ═══",
                      "═══ СВЕЖИЕ НОВОСТИ", "═══ СВЯЗАННЫЕ БУМАГИ ═══", "РЫНОК: рынок открыт", "Режим игры: long"):
            assert piece in u, (piece, u[:1500])
        assert "ПАМЯТЬ МИССИИ" not in u and "ПРОШЛЫЕ ОБЪЯСНЕНИЯ" not in u, "памяти и прошлых объяснений ещё нет"
        st_ev = [(e["stage"], e["status"]) for e in events if e.get("stage") == "explain"]
        assert st_ev == [("explain", "start"), ("explain", "done")], st_ev
        assert [e for e in events if e.get("stage") == "explain" and e["status"] == "done"][-1]["data"]["text"] == rec["text"]
        assert items(m)[0]["title"] == rec["title"] and "Вход исполнен" in text(m)
        st = status(m)
        assert st["enabled"] and st["items"] and st["memory"]["n"] == 0 and st["pending"] == 0
        # ── второе объяснение видит прошлое (не рисует ту же картинку) и идёт не раньше MIN_GAP ──
        _state(m)["last_ts"] = time.time()
        assert note(m, "review", "Перепроверка: ЖДЁМ", "план жив")
        t = _state(m)["task"]
        await asyncio.sleep(0.05)
        assert len(m.explain) == 1, "ждём окно MIN_GAP_SEC"
        _state(m)["last_ts"] = 0.0
        t.cancel()
        await flush(m)
        assert len(m.explain) == 2 and "ТВОИ ПРОШЛЫЕ ОБЪЯСНЕНИЯ" in fake.users[-1] and "вошёл в лонг" in fake.users[-1]
        assert m.explain[-1]["kind"] == "review"
        # ── сбой FLASH → честная заглушка, запись есть, шина error, этап не падает ──
        fake.fail = True
        note(m, "close", "Закрыто по тейку", "P/L +560")
        rec3 = await flush(m)
        assert rec3 and not rec3["ok"] and "Толмач не ответил" in rec3["text"] and "Закрыто по тейку" in rec3["text"]
        assert events[-1]["stage"] == "explain" and events[-1]["status"] == "error"
        fake.fail = False
        assert await flush(m) is None, "очередь пуста"
        # ── KEEP: не больше 40 записей ──
        for i in range(45):
            note(m, "other", f"событие {i}")
            await flush(m)
        assert len(m.explain) == KEEP
        # ── память: абзац ≤ 1500, списки ужаты, шина, персист; повтор — прошлая память в промпте ──
        m2 = mk()
        m2.run_id = rid
        m2.explain = [{"ts": time.time(), "kind": "entry", "title": f"т{i}", "text": f"объяснение {i}", "ok": True}
                      for i in range(12)]
        persisted.clear()
        fake.calls.clear()
        out = await memorize(m2, "после совета", ctx=ctx_of(m2), persist=lambda: persisted.append("mem"))
        assert out and out.startswith("ПАМЯТЬ:") and len(out) <= MEMORY_LIMIT and m2.memory == out and m2.memory_n == 1
        assert "memory" in fake.calls and persisted == ["mem"]
        um = fake.users[-1]
        for piece in ("═══ НАКОПИЛОСЬ С ТЕХ ПОР ═══", "Перепроверки:", "план жив 7", "Передачи:", "повод 6", "Ответы у троса и тейка:",
                      "ЖДЁМ @100.0 → сейчас 99.52 (лонг отсюда -0.5 %, шорт +0.5 %) — план жив 7",
                      "ЖДАТЬ @97.0 → сейчас 99.52 (+2.6 %) — г4",
                      "Толмач писал владельцу (только заголовки, пересказ не факт): ", " т11", "Сделки:",
                      "Новости до последнего совета:", "старая новость", "═══ ТЕКУЩИЙ ПРИКАЗ"):
            assert piece in um, (piece, um[:2000])
        assert "═══ ПРОШЛАЯ ПАМЯТЬ" not in um
        assert "объяснение 11" not in um and "Объяснения толмача:" not in um, "5.4.3: пересказ толмача — не факт памяти"
        sm, _ = memory_prompt(ctx_of(m2)(), "после совета", "x", "")
        assert sm.count("\n") < 12 and "МСК" in sm and str(MEMORY_LIMIT) in sm
        assert "что дали решения — вход, выход, отмена, ожидание, удержание: цена при решении и куда она ушла после" in sm
        assert ("в чью пользу: для ожидания — сколько прошла цена без нас в сторону идеи, для входа — за нас или "
                "против; без советов") in sm and "без оценки" not in sm, sm
        assert "FLASH" not in sm, "у троса думает модель денег, не обязательно FLASH (v5.4.2)"
        st_, _ = prompt(ctx_of(m2)(), [], [])
        assert "что пилот делает дальше — только из событий: взведённый вход, уровни, когда следующее решение" in st_
        assert "за чем владельцу следить" not in st_ and st_.count("\n") < 12
        # ── v5.4.2: ход цены после решения; молчание модели — «решения не было», а не ЖДАТЬ ──
        t9 = time.time()
        m9 = SimpleNamespace(ticker="T9", name="т", reviews=[
            {"ts": t9, "choice": "ЖДЁМ", "why": "коридор", "price": 100.0},
            {"ts": t9, "choice": "НЕТ_ОТВЕТА", "why": "таймаут", "price": 100.2, "silent": True},
            {"ts": t9, "choice": "НЕ_РАЗОБРАН", "why": "«может быть»", "price": 100.3}],
            handoffs=[], memory_ts=t9 - 60, pilot=SimpleNamespace(
                guards=[{"ts": t9, "decision": "ПОДЕРЖАТЬ", "side": "take", "price": 101.0, "why": "импульс"}],
                gates=[{"ts": t9 - 600, "decision": "ВОЙТИ", "price": 90.0, "why": "до памяти — уже сведено"},
                       {"ts": t9, "decision": "ЖДАТЬ", "price": 100.0, "why": "откат к 99.7", "side": "long"},
                       {"ts": t9, "decision": "ЖДАТЬ", "price": 100.1, "why": "PRO не ответил", "silent": True}],
                profits=[{"ts": t9, "decision": "ДЕРЖАТЬ", "price": 101.4, "why": "ход жив"}]))
        acc9 = _accumulated(m9, {"price": 101.4})
        for piece in ("ЖДЁМ @100.0 → сейчас 101.4 (лонг отсюда +1.4 %, шорт -1.4 %) — коридор",
                      "(модель не ответила — решения не было)", "(ответ модели не разобран — решения не было)",
                      "Проверки входа у двери: ", "ЖДАТЬ @100.0 → сейчас 101.4 (+1.4 % в сторону плана) — откат к 99.7",
                      "Мысли о прибыли: ", "ДЕРЖАТЬ @101.4 → сейчас 101.4 (+0.00 %) — ход жив",
                      "у тейка ПОДЕРЖАТЬ @101.0 → сейчас 101.4 (+0.4 %) — импульс"):
            assert piece in acc9, (piece, acc9)
        assert "НЕТ_ОТВЕТА" not in acc9 and "НЕ_РАЗОБРАН" not in acc9 and "таймаут" not in acc9, acc9
        assert "PRO не ответил" not in acc9 and "до памяти" not in acc9 and "ВОЙТИ" not in acc9, acc9
        assert "ЖДЁМ @100.0 — коридор" in _accumulated(m9, {}), "нет текущей цены — только цена решения"
        # ── 5.4.3: в чью пользу — вход «за лонг/шорт», ЖДЁМ в позиции — «за» сторону позиции, ЖДАТЬ шорт-плана ──
        m10 = SimpleNamespace(ticker="T10", name="т", handoffs=[], memory_ts=t9 - 60, reviews=[
            {"ts": t9 - 30, "choice": "КУПИТЬ_СЕЙЧАС", "why": "пробой", "price": 100.0},
            {"ts": t9, "choice": "ЖДЁМ", "why": "держу", "price": 100.0},
            {"ts": t9, "choice": "ЖДЁМ", "why": "вне", "price": 100.0, "in_pos": False},
            {"ts": t9, "choice": "ДОБРАТЬ", "why": "ещё", "price": 100.0, "in_pos": True}],
            pilot=SimpleNamespace(position={"side": "short", "opened_ts": t9 - 10}, guards=[
                {"ts": t9, "decision": "ЖДАТЬ", "side": "stop", "price": 100.0, "why": "ложный прокол"}],
                gates=[{"ts": t9, "decision": "ЖДАТЬ", "price": 100.0, "why": "откат", "side": "short"}], profits=[]))
        acc10 = _accumulated(m10, {"price": 101.0})
        for piece in ("КУПИТЬ @100.0 → сейчас 101.0 (за лонг +1.0 %) — пробой",     # ревью 5.4.3: метки модели
                      "ДЕРЖАТЬ @100.0 → сейчас 101.0 (за шорт -1.0 %) — держу",
                      "ЖДЁМ @100.0 → сейчас 101.0 (лонг отсюда +1.0 %, шорт -1.0 %) — вне",
                      # ревью 5.4.3: in_pos без pos_side (записи 5.4.3 до правки) — сторона от текущей позиции
                      "ДОБРАТЬ @100.0 → сейчас 101.0 (за шорт -1.0 %) — ещё",
                      "ЖДАТЬ @100.0 → сейчас 101.0 (за шорт -1.0 %) — ложный прокол",
                      "ЖДАТЬ @100.0 → сейчас 101.0 (-1.0 % в сторону плана) — откат"):
            assert piece in acc10, (piece, acc10)
        assert _pc(-0.0001) == "+0.00" and _pc(-0.05) == "-0.05" and _pc(-1.44) == "-1.4", (_pc(-0.0001), _pc(-0.05))
        # ── ревью 5.4.3: позиция уже закрыта (узел «закрытие») — сторона из записей (pos_side), а не из текущей позиции ──
        m11 = SimpleNamespace(ticker="T11", name="т", handoffs=[], memory_ts=t9 - 60, reviews=[
            {"ts": t9, "choice": "ЖДЁМ", "why": "держу", "price": 100.0, "in_pos": True, "pos_side": "long"},
            {"ts": t9, "choice": "ПЕРЕВЕРНУТЬ", "why": "слом", "price": 100.0, "in_pos": True, "pos_side": "long"},
            {"ts": t9, "choice": "ЗАКРЫТЬ", "why": "выдохлось", "price": 100.0, "in_pos": True, "pos_side": "short"}],
            pilot=SimpleNamespace(position=None, gates=[], profits=[], guards=[
                {"ts": t9, "decision": "ЖДАТЬ", "side": "stop", "price": 100.0, "why": "вынос", "pos_side": "long"},
                {"ts": t9, "decision": "ПОДЕРЖАТЬ", "side": "take", "price": 100.0, "why": "ход", "pos_side": "short"}]))
        acc11 = _accumulated(m11, {"price": 101.0})
        for piece in ("ДЕРЖАТЬ @100.0 → сейчас 101.0 (за лонг +1.0 %) — держу",
                      "ПЕРЕВЕРНУТЬ @100.0 → сейчас 101.0 (за шорт -1.0 %) — слом",
                      "ЗАКРЫТЬ @100.0 → сейчас 101.0 (за шорт -1.0 %) — выдохлось",
                      "ЖДАТЬ @100.0 → сейчас 101.0 (за лонг +1.0 %) — вынос",
                      "у тейка ПОДЕРЖАТЬ @100.0 → сейчас 101.0 (за шорт -1.0 %) — ход"):
            assert piece in acc11, (piece, acc11)
        assert choice_label({"choice": "КУПИТЬ_СЕЙЧАС"}) == "КУПИТЬ" and choice_label({"choice": "ЖДЁМ", "in_pos": True}) == "ДЕРЖАТЬ"
        assert choice_label({"choice": "ЖДЁМ", "in_pos": False}) == "ЖДЁМ" and choice_label(None) == ""
        assert len(m2.reviews) == KEEP_REVIEWS and m2.reviews[-1]["why"] == "план жив 7"
        assert len(m2.handoffs) == KEEP_HANDOFFS and len(m2.pilot.guards) == KEEP_GUARDS and len(m2.explain) == KEEP_EXPLAIN
        assert events[-1]["stage"] == "memory" and events[-1]["status"] == "done" and events[-1]["data"]["n"] == 1
        out2 = await memorize(m2, "закрытие позиции")
        assert out2 and "═══ ПРОШЛАЯ ПАМЯТЬ ═══" in fake.users[-1] and "ПАМЯТЬ: 23.09" in fake.users[-1] and m2.memory_n == 2
        # толмач теперь видит память
        note(m2, "council", "Совет вынес приказ", "BUY сейчас")
        await flush(m2)
        assert "═══ ПАМЯТЬ МИССИИ" in fake.users[-1] and "ПАМЯТЬ: 23.09" in fake.users[-1]
        # ── память: FLASH не уложился → дожим compress.shrink; сбой → прежняя память, списки целы ──
        long_mem = "м" * 4000

        async def long_flash(system, user, *, think=False, route="flash", max_tokens=None):
            if route == "memory":
                return long_mem
            return await FakeAI.flash_text(fake, system, user, think=think, route=route, max_tokens=max_tokens)
        fake.flash_text = long_flash
        out3 = await memorize(m2, "N перепроверок")
        assert out3 and len(out3) <= MEMORY_LIMIT, len(out3)
        del fake.flash_text
        fake.fail = True
        m2.reviews = [{"ts": time.time(), "choice": "ЖДЁМ", "why": f"r{i}"} for i in range(9)]
        assert await memorize(m2, "сбой") is None and m2.memory == out3 and len(m2.reviews) == 9
        assert events[-1]["stage"] == "memory" and events[-1]["status"] == "error"
        fake.fail = False
        # ── фоновая память: второй вызов, пока идёт первый, отклоняется ──
        assert memorize_bg(m2, "фон") and not memorize_bg(m2, "фон ещё раз")
        await _state(m2)["mem_task"]
        assert m2.memory_n == 4
        # ── PRO-модель толмача; выключен конфигом ──
        config.PYTHIA_EXPLAIN_MODEL = "pro"
        note(m2, "market", "Рынок закрыт до 10:00 МСК")
        rec_p = await flush(m2)
        assert rec_p["model"] == "pro" and rec_p["text"] == "PRO-объяснение" and "pro:explain" in fake.calls
        config.PYTHIA_EXPLAIN_MODEL = "flash"
        config.PYTHIA_EXPLAIN = False
        assert not note(m2, "entry", "выкл") and await memorize(m2, "выкл") is None and not status(m2)["enabled"]
        config.PYTHIA_EXPLAIN = True
        reset("TEST")
        assert "TEST" not in _ST
        bus.end_run(rid)

    asyncio.run(main())
    print("explain self-test OK: дебаунс и объединение узлов, дубли, промпт ≤ 12 строк, заглушка при сбое, "
          "KEEP 40, память ≤ 1500 с ужатием списков, прошлая память, дожим shrink, сбой памяти без потерь, фон, PRO, выкл.")
