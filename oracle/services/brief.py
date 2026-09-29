"""Утренняя сводка: что сегодня (события, напоминания, дни рождения, сроки), погода и одно дело,
на которое стоит надавить. Та же сводка «на остаток дня» — для /today в любое время (`today_brief`),
и повестка на любой период — для инструмента get_agenda (`gather(ctx, start, end)`).

Данные собираются из модулей инструментов (лениво — модуль может отсутствовать или упасть:
тогда просто нет этой части). Текст пишет модель голосом бота; не ответила — детерминированный
шаблон по тем же данным, чтобы утро без сводки не осталось никогда.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import timeutil
from ..tools.base import ToolContext

log = logging.getLogger("oracle.brief")

UTC = timeutil.UTC
BRIEF_MAX = 1200                 # ориентир для модели, символов
BIRTHDAY_DAYS = 3
TASK_DAYS = 1
JOURNAL_FRESH = timedelta(days=7)   # дневник старше — уже не повестка
JOURNAL_MAX = 1500
TEXT_MAX = 200

# сводка утром (первое сообщение за день) и по /today в любое время — «на остаток дня»
_MODES = {
    "morning": dict(situation="Сейчас утро, ты пишешь ему утреннюю сводку — первое сообщение за день.",
                    opening="Начни с приветствия, привязанного к дню недели или погоде, — одна живая фраза, "
                            "не открытка.",
                    scope="сегодня", empty="день"),
    "now": dict(situation="Сейчас {now} — он сам попросил сводку на остаток дня.",
                opening="Без приветствий по времени суток («доброе утро» и т.п.) — сразу к делу, "
                        "можно одной фразой про погоду.",
                scope="осталось на сегодня (прошедшее не перечисляй)", empty="остаток дня"),
}

SYSTEM = """\
Ты — {name}, личный ИИ {owner}: напарник со своим умом и мнением, а не услужливый ассистент. {situation}

КАК ПИСАТЬ
- Коротко и по-человечески, на «ты», живым русским. Без канцелярита, без воды, без «желаю продуктивного дня».
- {opening}
- Дальше — что {scope}: события со временем, напоминания, дни рождения. Пустой {empty} так и назови — это тоже новость.
- В конце — одно дело, на которое стоит надавить: из твоего дневника или из просроченных задач. Конкретно: что сделать и почему именно сегодня. Одно, а не пять.
- Без списков ради списков: пара пунктов — обычным текстом; список — только когда дел правда много.
- Только то, что есть в данных ниже. Ничего не выдумывай — ни встреч, ни людей, ни погоды. Чего нет в данных — не упоминай.
- Не длиннее {limit} символов. Без заголовков. Эмодзи — максимум пара и только по делу.\
"""


# ── мелочи ───────────────────────────────────────────────────────────────────
def _zone(tz: Any) -> ZoneInfo:
    if isinstance(tz, ZoneInfo):
        return tz
    try:
        return ZoneInfo(str(tz))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return ZoneInfo("UTC")


def _cut(s: Any, n: int = TEXT_MAX) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _day(d: date) -> str:
    """'ср 30.09'."""
    return f"{timeutil.weekday_name(d, full=False)} {d:%d.%m}"


def _when_days(left: int) -> str:
    return {0: "сегодня", 1: "завтра", 2: "послезавтра"}.get(left, f"через {left} дн.")


def day_bounds(ctx: ToolContext) -> tuple[datetime, datetime]:
    """Локальные сутки «сегодня» → (начало, конец) в UTC."""
    tz = ctx.tz
    today = ctx.now_local().date()
    start = datetime.combine(today, time(0, 0)).replace(tzinfo=tz)
    end = datetime.combine(today + timedelta(days=1), time(0, 0)).replace(tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def _event_time(ev: dict, tz: ZoneInfo, day_start: datetime, day_end: datetime) -> str:
    """'15:00–16:00' / '15:00' / 'весь день' / 'до 11:00' (началось вчера)."""
    if ev.get("all_day"):
        return "весь день"
    s = timeutil.from_iso(ev.get("occurs_at"))
    e = timeutil.from_iso(ev.get("occurs_end"))
    if s is None:
        return "?"
    sl = s.astimezone(tz)
    el = e.astimezone(tz) if e and e > s else None
    if s < day_start:
        if el is not None and e < day_end:
            return f"идёт, до {el:%H:%M}"
        return "идёт весь день"
    if el is not None and e <= day_end and el.date() == sl.date():
        return f"{sl:%H:%M}–{el:%H:%M}"
    return f"{sl:%H:%M}"


# ── сбор данных (каждый источник сам по себе: упал один — остальные живы) ───────
async def _events(ctx: ToolContext, s: datetime, e: datetime) -> list[dict]:
    from ..tools import calendar
    tz = ctx.tz
    out = []
    for ev in await calendar.events_between(ctx.db, tz, s, e):
        at = timeutil.from_iso(ev.get("occurs_at"))
        out.append({"time": _event_time(ev, tz, s, e), "title": _cut(ev.get("title")),
                    "location": _cut(ev.get("location"), 100), "at": ev.get("occurs_at") or "",
                    "date": _day(at.astimezone(tz).date()) if at else ""})
    return out


REMINDER_TIMES_MAX = 62          # срабатываний одного напоминания в повестке (два раза в день на месяц)
REMINDERS_MAX = 200             # и всех вместе


def _times_in(r: dict, s: datetime, e: datetime, fallback: ZoneInfo) -> list[datetime]:
    """Срабатывания напоминания в окне [s, e) (UTC), по порядку, — не больше REMINDER_TIMES_MAX + 1 (лишнее
    значит «дальше ещё есть»). У повторяющегося next_at — только ближайшее срабатывание, а окно может быть
    и на неделю — дальше раскрываем правило. Раньше next_at не показываем: там мог быть пропуск (only_next)."""
    out: set[datetime] = set()
    snz = timeutil.from_iso(r.get("snooze_at"))
    if snz is not None and s <= snz < e:
        out.add(snz)
    nxt = timeutil.from_iso(r.get("next_at"))
    cursor: datetime | None = None
    if nxt is not None:
        if s <= nxt < e:
            out.add(nxt)
            cursor = nxt
        elif r.get("rrule") and nxt < s:
            cursor = s - timedelta(seconds=1)
    if cursor is not None and r.get("rrule"):
        from .scheduler import next_after
        tz = _zone(r.get("tz") or fallback)
        try:
            for _ in range(REMINDER_TIMES_MAX + 1):
                occ = next_after(r["rrule"], r["local_start"], tz, cursor)
                if occ is None or occ >= e:
                    break
                out.add(occ)
                cursor = occ
        except Exception as ex:     # кривое правило в базе — показываем, что успели
            log.debug("повестка: правило #%s не раскрылось: %s", r.get("id"), ex)
    return sorted(out)[:REMINDER_TIMES_MAX + 1]


async def _reminders(ctx: ToolContext, s: datetime, e: datetime) -> list[dict]:
    from ..tools import reminders
    tz = ctx.tz
    out = []
    for r in await reminders.list_active(ctx.db, 300):
        # follow-up'ы — внутренняя повестка бота; напоминания о событиях и задачах дублировали бы
        # сами события и задачи (срок задачи — в её же день)
        if r.get("kind") in ("followup", "event", "task"):
            continue
        times = _times_in(r, s, e, tz)
        more = len(times) > REMINDER_TIMES_MAX         # частый повтор: дальше в окне ещё есть — не молчать
        for i, at in enumerate(times[:REMINDER_TIMES_MAX]):
            loc = at.astimezone(tz)
            out.append({"time": loc.strftime("%H:%M"), "date": _day(loc.date()), "at": timeutil.iso(at),
                        "text": _cut(r.get("text")), "kind": r.get("kind") or "reminder",
                        "repeat": timeutil.describe_rrule(r["rrule"]) if r.get("rrule") else "",
                        "nag": bool(r.get("nag")), **({"more": True} if more and i == REMINDER_TIMES_MAX - 1 else {})})
    out.sort(key=lambda x: x["at"])
    return out[:REMINDERS_MAX]


async def _birthdays(ctx: ToolContext, s: datetime | None = None, e: datetime | None = None) -> list[dict]:
    """ДР в ближайшие BIRTHDAY_DAYS дня (сводка) или те, что попадают в окно [s, e) (повестка)."""
    from ..tools import birthdays
    tz = ctx.tz
    days, d0, d1 = BIRTHDAY_DAYS, None, None
    if s is not None and e is not None:
        d0, d1 = s.astimezone(tz).date(), (e - timedelta(seconds=1)).astimezone(tz).date()
        days = (d1 - ctx.now_local().date()).days
        if days < 0:
            return []
    out = []
    for b in await birthdays.upcoming(ctx.db, tz, days):
        try:
            nd = date.fromisoformat(str(b.get("next_date")))
        except ValueError:
            nd = None
        if d0 is not None and (nd is None or not d0 <= nd <= d1):
            continue
        out.append({"name": _cut(b.get("name"), 100), "relation": _cut(b.get("relation"), 60),
                    "days_left": int(b.get("days_left") or 0), "turns": b.get("turns"),
                    "date": _day(nd) if nd else "", "notes": _cut(b.get("notes"), 150)})
    return out


async def _tasks(ctx: ToolContext, s: datetime | None = None, e: datetime | None = None) -> list[dict]:
    """Задачи со сроком: просроченные + на ближайшие сутки (сводка) или со сроком в окне [s, e)
    (повестка; просроченные — только если окно включает «сейчас»)."""
    from ..tools import projects
    now = timeutil.now_utc()
    days = TASK_DAYS if e is None else max(0.0, (e - now) / timedelta(days=1))
    out = []
    for t in await projects.due_tasks(ctx.db, ctx.tz, days):
        if s is not None and e is not None:
            due = timeutil.from_iso(t.get("due_at"))
            in_window = due is not None and s <= due < e
            if not in_window and not (t.get("overdue") and s <= now < e):
                continue
        out.append({"text": _cut(t.get("text")), "project": _cut(t.get("project"), 80) or None,
                    "overdue": bool(t.get("overdue")), "due": t.get("due_local") or "",
                    "priority": int(t.get("priority") or 2)})
    # просроченные и важные — первыми
    out.sort(key=lambda x: (not x["overdue"], x["priority"]))
    return out


async def _weather(ctx: ToolContext) -> str | None:
    from ..tools import news
    w = await news.weather_line(ctx)
    return (str(w).strip() or None) if w else None


async def _journal(ctx: ToolContext) -> dict | None:
    from ..tools import memory
    j = await memory.latest_journal(ctx.db)
    if not j or not str(j.get("content") or "").strip():
        return None
    created = timeutil.from_iso(j.get("created_at"))
    if created is not None and timeutil.now_utc() - created > JOURNAL_FRESH:
        return None
    content = str(j["content"]).strip()
    if len(content) > JOURNAL_MAX:
        content = content[: JOURNAL_MAX - 1].rstrip() + "…"
    day = created.astimezone(ctx.tz).strftime("%d.%m") if created else ""
    return {"content": content, "date": day}


async def gather(ctx: ToolContext, start: datetime | None = None, end: datetime | None = None, *,
                 extras: bool | None = None) -> dict:
    """Все данные для сводки. Каждый источник необязателен: ошибка → пустая часть и запись в лог.

    Без start/end — сегодняшние сутки (утренняя сводка). С окном [start, end) — повестка на период:
    события, напоминания (повторы раскрыты), ДР и задачи со сроком именно в этом окне.
    extras — погода, дневник, ДР на 3 дня вперёд и задачи на сутки, как в сводке (по умолчанию —
    только без окна)."""
    windowed = start is not None or end is not None
    s0, e0 = day_bounds(ctx)
    s, e = start or s0, end or e0
    extras = (not windowed) if extras is None else extras
    data: dict[str, Any] = {"today": ctx.now_local().date(), "weather": None, "events": [],
                            "reminders": [], "birthdays": [], "tasks": [], "journal": None, "failed": []}
    sources: tuple = (("events", lambda: _events(ctx, s, e)), ("reminders", lambda: _reminders(ctx, s, e)))
    if extras:
        sources += (("birthdays", lambda: _birthdays(ctx)), ("tasks", lambda: _tasks(ctx)),
                    ("weather", lambda: _weather(ctx)), ("journal", lambda: _journal(ctx)))
    else:
        sources += (("birthdays", lambda: _birthdays(ctx, s, e)), ("tasks", lambda: _tasks(ctx, s, e)))
    for key, make in sources:
        try:
            data[key] = await make()
        except Exception as ex:   # в т.ч. ImportError — модуля нет
            log.warning("сводка: источник %s не ответил: %s", key, ex)
            data["failed"].append(key)
    return data


# ── текст для модели ─────────────────────────────────────────────────────────
def _event_str(ev: dict) -> str:
    s = f"{ev['time']} {ev['title']}"
    return s + (f" (место: {ev['location']})" if ev.get("location") else "")


def _reminder_str(r: dict) -> str:
    s = f"{r['time']} " + ("будильник: " if r.get("kind") == "wake" else "") + r["text"]
    extra = [x for x in (r.get("repeat"), "долбит до отметки" if r.get("nag") and r.get("kind") != "wake" else "") if x]
    return s + (f" ({', '.join(extra)})" if extra else "")


def _bday_str(b: dict, *, short: bool = False) -> str:
    who = b["name"] + (f" ({b['relation']})" if b.get("relation") else "")
    when = _when_days(b["days_left"])
    if b["days_left"] >= 2 and b.get("date"):
        when += f", {b['date']}"
    s = f"{when}: {who}" if not short else f"{when} — {who}"
    if b.get("turns"):
        s += f", {'исполняется' if b['days_left'] == 0 else 'исполнится'} {b['turns']}"
    if b.get("notes") and not short:
        s += f" (заметки: {b['notes']})"
    return s


def _task_str(t: dict) -> str:
    head = f"ПРОСРОЧЕНО (срок был {t['due']})" if t["overdue"] else f"срок {t['due']}"
    s = f"{head}: {t['text']}"
    if t.get("project"):
        s += f" [проект {t['project']}]"
    if t.get("priority") == 1:
        s += ", важная"
    return s


def _section(title: str, rows: list[str], empty: str = "нет") -> str:
    return f"{title}:\n" + ("\n".join(f"- {r}" for r in rows) if rows else f"- {empty}")


def format_data(ctx: ToolContext, d: dict) -> str:
    """Компактный блок данных для модели."""
    parts = [f"Сейчас: {timeutil.fmt_now_for_prompt(ctx.tz)}"]
    owner = (ctx.cfg.owner_name or "").strip()
    if owner:
        parts.append(f"Владелец: {owner}")
    parts.append(f"Погода: {d.get('weather') or 'нет данных'}")
    rest = d.get("mode") == "now"
    parts.append(_section("События до конца дня" if rest else "События сегодня",
                          [_event_str(x) for x in d.get("events") or []]))
    parts.append(_section("Напоминания до конца дня" if rest else "Напоминания на сегодня",
                          [_reminder_str(x) for x in d.get("reminders") or []]))
    parts.append(_section(f"Дни рождения в ближайшие {BIRTHDAY_DAYS} дня",
                          [_bday_str(x) for x in d.get("birthdays") or []]))
    parts.append(_section("Задачи со сроком (просроченные и на ближайшие сутки)",
                          [_task_str(x) for x in d.get("tasks") or []]))
    j = d.get("journal")
    if j:
        when = f" от {j['date']}" if j.get("date") else ""
        parts.append(f"Твой дневник (запись{when}) — твоя собственная повестка, к чему ты хотел вернуться:\n"
                     f"{j['content']}")
    else:
        parts.append("Твой дневник: свежих записей нет.")
    if d.get("failed"):
        parts.append("Не удалось получить: " + ", ".join(d["failed"]) + " — об этом не выдумывай.")
    return "\n\n".join(parts)


# ── запасной шаблон ──────────────────────────────────────────────────────────
def _push_line(d: dict) -> str:
    """Одно дело, на которое стоит надавить."""
    tasks = d.get("tasks") or []
    overdue = [t for t in tasks if t["overdue"]]
    if overdue:
        t = overdue[0]
        return f"Надавлю на одно: «{t['text']}» висит с {t['due']} — закрой сегодня."
    if tasks:
        t = tasks[0]
        return f"Не упусти: «{t['text']}» — срок {t['due']}."
    j = d.get("journal")
    if j:
        first = next((ln.strip(" -•*") for ln in j["content"].splitlines() if ln.strip(" -•*")), "")
        if first:
            return f"Из моих заметок: {_cut(first)}"
    return ""


def fallback_text(d: dict) -> str:
    """Сводка без модели — по тем же данным."""
    today: date = d.get("today") or timeutil.now_utc().date()
    rest = d.get("mode") == "now"
    day = f"{timeutil.weekday_name(today)}, {today.day} {timeutil.month_gen(today.month)}"
    lines = [f"Что осталось на сегодня ({day})." if rest else f"Доброе утро. Сегодня {day}."]
    if d.get("weather"):
        lines.append(d["weather"])
    blocks: list[str] = []
    if d.get("events"):
        blocks.append("📅 События:\n" + "\n".join(f"• {_event_str(x)}" for x in d["events"]))
    if d.get("reminders"):
        blocks.append("⏰ Напоминания:\n" + "\n".join(f"• {_reminder_str(x)}" for x in d["reminders"]))
    if d.get("birthdays"):
        blocks.append("🎂 Дни рождения:\n" + "\n".join(f"• {_bday_str(x, short=True)}" for x in d["birthdays"]))
    if d.get("tasks"):
        blocks.append("📌 Сроки:\n" + "\n".join(
            f"• {'просрочено' if t['overdue'] else 'до ' + t['due']}: {t['text']}"
            + (f" [{t['project']}]" if t.get("project") else "") for t in d["tasks"]))
    if not d.get("events") and not d.get("reminders"):
        blocks.insert(0, "До конца дня встреч и напоминаний нет." if rest
                      else "Встреч и напоминаний на сегодня нет — день свободный.")
    push = _push_line(d)
    if push:
        blocks.append(push)
    return "\n".join(lines) + "\n\n" + "\n\n".join(blocks)


# ── главное ──────────────────────────────────────────────────────────────────
async def morning_brief(ctx: ToolContext, *, mode: str = "morning") -> str:
    """Утренняя сводка владельцу. mode="now" — сводка на остаток дня (для /today в любое время):
    без «доброго утра» и без того, что уже прошло. Модель не ответила (LLMError или что угодно) — шаблон."""
    mode = mode if mode in _MODES else "morning"
    try:
        if mode == "now":
            _, day_end = day_bounds(ctx)
            d = await gather(ctx, timeutil.now_utc(), day_end, extras=True)
        else:
            d = await gather(ctx)
    except Exception:   # gather сам не падает, но сводка не должна зависеть ни от чего
        log.exception("сводка: сбор данных")
        d = {"today": ctx.now_local().date()}
    d["mode"] = mode
    try:
        cfg = ctx.cfg
        owner = (cfg.owner_name or "").strip()
        parts = {k: v.format(now=ctx.now_local().strftime("%H:%M")) for k, v in _MODES[mode].items()}
        system = SYSTEM.format(name=cfg.bot_name, owner=f"владельца ({owner})" if owner else "владельца",
                               limit=BRIEF_MAX, **parts)
        text = await ctx.llm.ask(system, format_data(ctx, d), deep=False, temperature=0.9)
        text = str(text or "").strip()
        if text:
            return text
        log.warning("сводка: модель вернула пустоту — шлю шаблон")
    except Exception as e:
        log.warning("сводка: модель не ответила (%s) — шлю шаблон", e)
    return fallback_text(d)


async def today_brief(ctx: ToolContext) -> str:
    """Сводка на остаток сегодняшнего дня — для /today (утренняя начинается с «доброго утра»)."""
    return await morning_brief(ctx, mode="now")
