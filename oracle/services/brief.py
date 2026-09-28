"""Утренняя сводка: что сегодня (события, напоминания, дни рождения, сроки), погода и одно дело,
на которое стоит надавить.

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

SYSTEM = """\
Ты — {name}, личный ИИ {owner}: напарник со своим умом и мнением, а не услужливый ассистент. Сейчас утро, ты пишешь ему утреннюю сводку — первое сообщение за день.

КАК ПИСАТЬ
- Коротко и по-человечески, на «ты», живым русским. Без канцелярита, без воды, без «желаю продуктивного дня».
- Начни с приветствия, привязанного к дню недели или погоде, — одна живая фраза, не открытка.
- Дальше — что сегодня: события со временем, напоминания, дни рождения. Пустой день так и назови — это тоже новость.
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
        out.append({"time": _event_time(ev, tz, s, e), "title": _cut(ev.get("title")),
                    "location": _cut(ev.get("location"), 100), "at": ev.get("occurs_at") or ""})
    return out


async def _reminders(ctx: ToolContext, s: datetime, e: datetime) -> list[dict]:
    from ..tools import reminders
    tz = ctx.tz
    lo, hi = timeutil.iso(s), timeutil.iso(e)
    out = []
    for r in await reminders.list_active(ctx.db, 300):
        # follow-up'ы — внутренняя повестка бота; напоминания о событиях дублировали бы сами события
        if r.get("kind") in ("followup", "event"):
            continue
        at = r.get("next_at")
        if not at or not (lo <= at < hi):
            continue
        out.append({"time": timeutil.from_iso(at).astimezone(tz).strftime("%H:%M"),
                    "text": _cut(r.get("text")), "kind": r.get("kind") or "reminder",
                    "repeat": timeutil.describe_rrule(r["rrule"]) if r.get("rrule") else "",
                    "nag": bool(r.get("nag"))})
    return out


async def _birthdays(ctx: ToolContext) -> list[dict]:
    from ..tools import birthdays
    out = []
    for b in await birthdays.upcoming(ctx.db, ctx.tz, BIRTHDAY_DAYS):
        try:
            nd = date.fromisoformat(str(b.get("next_date")))
        except ValueError:
            nd = None
        out.append({"name": _cut(b.get("name"), 100), "relation": _cut(b.get("relation"), 60),
                    "days_left": int(b.get("days_left") or 0), "turns": b.get("turns"),
                    "date": _day(nd) if nd else "", "notes": _cut(b.get("notes"), 150)})
    return out


async def _tasks(ctx: ToolContext) -> list[dict]:
    from ..tools import projects
    out = []
    for t in await projects.due_tasks(ctx.db, ctx.tz, TASK_DAYS):
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


async def gather(ctx: ToolContext) -> dict:
    """Все данные для сводки. Каждый источник необязателен: ошибка → пустая часть и запись в лог."""
    s, e = day_bounds(ctx)
    data: dict[str, Any] = {"today": ctx.now_local().date(), "weather": None, "events": [],
                            "reminders": [], "birthdays": [], "tasks": [], "journal": None, "failed": []}
    sources = (("events", lambda: _events(ctx, s, e)), ("reminders", lambda: _reminders(ctx, s, e)),
               ("birthdays", lambda: _birthdays(ctx)), ("tasks", lambda: _tasks(ctx)),
               ("weather", lambda: _weather(ctx)), ("journal", lambda: _journal(ctx)))
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
    parts.append(_section("События сегодня", [_event_str(x) for x in d.get("events") or []]))
    parts.append(_section("Напоминания на сегодня", [_reminder_str(x) for x in d.get("reminders") or []]))
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
    lines = [f"Доброе утро. Сегодня {timeutil.weekday_name(today)}, {today.day} {timeutil.month_gen(today.month)}."]
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
        blocks.insert(0, "Встреч и напоминаний на сегодня нет — день свободный.")
    push = _push_line(d)
    if push:
        blocks.append(push)
    return "\n".join(lines) + "\n\n" + "\n\n".join(blocks)


# ── главное ──────────────────────────────────────────────────────────────────
async def morning_brief(ctx: ToolContext) -> str:
    """Утренняя сводка владельцу. Модель не ответила (LLMError или что угодно) — шаблон."""
    try:
        d = await gather(ctx)
    except Exception:   # gather сам не падает, но сводка не должна зависеть ни от чего
        log.exception("сводка: сбор данных")
        d = {"today": ctx.now_local().date()}
    try:
        cfg = ctx.cfg
        owner = (cfg.owner_name or "").strip()
        system = SYSTEM.format(name=cfg.bot_name, owner=f"владельца ({owner})" if owner else "владельца",
                               limit=BRIEF_MAX)
        text = await ctx.llm.ask(system, format_data(ctx, d), deep=False, temperature=0.9)
        text = str(text or "").strip()
        if text:
            return text
        log.warning("сводка: модель вернула пустоту — шлю шаблон")
    except Exception as e:
        log.warning("сводка: модель не ответила (%s) — шлю шаблон", e)
    return fallback_text(d)
