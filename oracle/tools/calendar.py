"""Календарь: события (разовые, повторяющиеся, на весь день), напоминания о них, выгрузка .ics.

Строка `events`: `local_start` (наивное локальное начало) + `tz` + `rrule`; `starts_at`/`ends_at` —
UTC первого вхождения. У события на весь день конец исключающий: полночь дня после последнего
(как DTEND;VALUE=DATE в iCalendar). Повторы раскрываются по местному времени — «каждый вторник
в 19:00» не съезжает при переходе на летнее/зимнее время.

Напоминание о событии — строка reminders (kind='event', ref_type='event', ref_id=id события);
за сколько минут напоминать, хранится в kv `event_remind:<id>` (None — не напоминать),
чтобы при переносе события пересоздать напоминание так же.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

from .. import timeutil
from .base import OutItem, ToolContext, tool
from .reminders import as_bool, as_id, cancel_by_ref, clean_repeat, create_reminder, to_local, zone

log = logging.getLogger("oracle.tools.calendar")

UTC = timeutil.UTC
MAX_TITLE = 300
MAX_LOCATION = 300
MAX_NOTES = 4000
MAX_EXPAND = 500                    # вхождений одного повторяющегося события за один запрос
MAX_REMIND_MIN = 366 * 1440         # напоминать не раньше чем за год
DEFAULT_REMIND_MIN = 30
KV_REMIND = "event_remind:{}"
_WD = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_DATE_ONLY = re.compile(r"\s*(\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{4})\s*")
_TIME_ONLY = re.compile(r"\s*\d{1,2}[:.]\d{2}\s*")
_NO = {"", "none", "null", "no", "false", "нет", "не надо", "-"}


# ── мелочи ───────────────────────────────────────────────────────────────────
def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def human_minutes(m: int) -> str:
    """30 → «30 мин», 90 → «1 час 30 мин», 1440 → «сутки», 2880 → «2 дня»."""
    if m < 60:
        return f"{m} мин"
    if m < 1440:
        h, r = divmod(m, 60)
        return f"{h} {plural(h, 'час', 'часа', 'часов')}" + (f" {r} мин" if r else "")
    d, r = divmod(m, 1440)
    h = r // 60
    head = "сутки" if d == 1 else f"{d} {plural(d, 'день', 'дня', 'дней')}"
    return head + (f" {h} ч" if h else "")


def _fmt_day(d: date, tz: ZoneInfo) -> str:
    wd = timeutil.weekday_name(d, full=False)
    return f"{wd} {d:%d.%m}" if d.year == timeutil.now_local(tz).year else f"{wd} {d:%d.%m.%Y}"


def _fmt_span(s_utc: datetime, e_utc: datetime | None, all_day: bool, tz: ZoneInfo) -> str:
    """'пн 28.09 15:00–16:00' / 'чт 01.10, весь день' / 'чт 01.10 — сб 03.10, весь день'."""
    if all_day:
        d0 = s_utc.astimezone(tz).date()
        d1 = (e_utc.astimezone(tz) - timedelta(seconds=1)).date() if e_utc and e_utc > s_utc else d0
        if d1 <= d0:
            return f"{_fmt_day(d0, tz)}, весь день"
        return f"{_fmt_day(d0, tz)} — {_fmt_day(d1, tz)}, весь день"
    base = timeutil.fmt_local(s_utc, tz)
    if not e_utc or e_utc <= s_utc:
        return base
    sl, el = s_utc.astimezone(tz), e_utc.astimezone(tz)
    if sl.date() == el.date():
        return f"{base}–{el:%H:%M}"
    return f"{base} — {timeutil.fmt_local(e_utc, tz)}"


def _clean_str(v: Any, limit: int) -> str:
    return ("" if v is None else str(v)).strip()[:limit]


def _clean_title(title: Any) -> str:
    t = " ".join(("" if title is None else str(title)).split())
    if not t:
        raise ValueError("у события нет названия — как его назвать?")
    if len(t) > MAX_TITLE:
        raise ValueError(f"название длиннее {MAX_TITLE} символов — подробности положи в notes")
    return t


def _remind_minutes(v: Any) -> int | None:
    """remind_before_min от модели → минуты (≥ 0) или None (не напоминать)."""
    if v is None or isinstance(v, bool):
        return None if v is None or v is False else DEFAULT_REMIND_MIN
    if isinstance(v, str) and v.strip().lower() in _NO:
        return None
    try:
        m = int(float(v))
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"remind_before_min — число минут (30, 60, 1440), а пришло «{v}»") from None
    if m < 0:
        return None
    return min(m, MAX_REMIND_MIN)


def _midnight(d: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, time(0, 0)).replace(tzinfo=tz)


def _parse_start(start: Any, tz: ZoneInfo, all_day: bool) -> datetime:
    if all_day:
        return _midnight(to_local(start, tz, default_time=time(0, 0)).date(), tz)
    return to_local(start, tz)


def _parse_end(end: Any, start_l: datetime, tz: ZoneInfo, all_day: bool) -> datetime:
    """Конец события (aware локальный). Для all_day — исключающий: полночь после последнего дня."""
    empty = end is None or (isinstance(end, str) and not end.strip())
    if all_day:
        if empty:
            return _midnight(start_l.date() + timedelta(days=1), tz)
        last = to_local(end, tz, default_time=time(0, 0)).date()
        if last < start_l.date():
            raise ValueError("конец события раньше начала")
        return _midnight(last + timedelta(days=1), tz)
    if empty:
        return start_l + timedelta(hours=1)
    if isinstance(end, str) and _TIME_ONLY.fullmatch(end):
        h, m = (int(x) for x in re.split(r"[:.]", end.strip()))
        if h > 23 or m > 59:
            raise ValueError(f"не понял время конца «{end}»")
        e = start_l.replace(hour=h, minute=m, second=0, microsecond=0)
        if e < start_l:                       # «23:00–01:00» — через полночь
            e += timedelta(days=1)
        return e
    e = to_local(end, tz)
    if e < start_l:
        raise ValueError("конец события раньше начала")
    return e


def _check_rule(rule: str | None, start_naive: datetime) -> None:
    if not rule:
        return
    try:
        first = rrulestr(rule, dtstart=start_naive).after(start_naive, inc=True)
    except Exception:
        raise ValueError(f"не понял правило повтора «{rule}»") from None
    if first is None:
        raise ValueError("правило повтора не даёт ни одной даты — проверь UNTIL/COUNT")


# ── сдвиг правила повтора для напоминания «за N дней» ────────────────────────
def shift_rrule(rule: str, day_offset: int, event_day: date, time_changed: bool) -> str | None:
    """Правило для напоминания, которое на day_offset дней раньше события.

    Дни недели/месяца в BYDAY/BYMONTHDAY сдвигаются вместе с датой. None — сдвинуть
    честно не получается (сложное правило) — тогда напоминание заранее не ставим.
    """
    parts: dict[str, str] = {}
    for p in rule.split(";"):
        if "=" in p:
            k, v = p.split("=", 1)
            parts[k.strip().upper()] = v.strip().upper()
    if time_changed and any(k in parts for k in ("BYHOUR", "BYMINUTE", "BYSECOND")):
        return None
    if day_offset == 0:
        return rule
    if any(k in parts for k in ("BYYEARDAY", "BYWEEKNO", "BYSETPOS", "BYEASTER")):
        return None
    freq = parts.get("FREQ", "")
    if "BYDAY" in parts:
        if "BYMONTHDAY" in parts or "BYMONTH" in parts:
            return None
        days = [d for d in parts["BYDAY"].split(",") if d]
        if not days or any(d not in _WD for d in days):     # «1MO», «-1FR» — не двигаем
            return None
        parts["BYDAY"] = ",".join(dict.fromkeys(_WD[(_WD.index(d) - day_offset) % 7] for d in days))
        if freq == "WEEKLY" and int(parts.get("INTERVAL", "1") or 1) > 1:
            wk = parts.get("WKST", "MO")
            parts["WKST"] = _WD[(_WD.index(wk) - day_offset) % 7] if wk in _WD else "MO"
    elif "BYMONTHDAY" in parts:
        new = []
        for x in parts["BYMONTHDAY"].split(","):
            try:
                n = int(x)
            except ValueError:
                return None
            k = n - day_offset
            if n > 0 and k < 1:              # ушли в прошлый месяц: 0 → последний день (-1)
                if "BYMONTH" in parts:
                    return None
                k -= 1
            if k < -28 or k == 0:
                return None
            new.append(str(k))
        parts["BYMONTHDAY"] = ",".join(dict.fromkeys(new))
    else:
        rem_day = event_day - timedelta(days=day_offset)
        if "BYMONTH" in parts:
            if rem_day.month != event_day.month:
                return None
        elif freq == "MONTHLY":
            k = event_day.day - day_offset
            if k < 1:
                if k - 1 < -28:
                    return None
                parts["BYMONTHDAY"] = str(k - 1)
        elif freq == "YEARLY" and (rem_day.month, rem_day.day) == (2, 29):
            parts["BYMONTH"], parts["BYMONTHDAY"] = "2", "-1"
    return ";".join(f"{k}={v}" for k, v in parts.items())


# ── строки и напоминание ────────────────────────────────────────────────────
async def get_event(db, eid: int) -> dict | None:
    try:
        eid = int(eid)
    except (TypeError, ValueError, OverflowError):
        return None
    return await db.fetchone("SELECT * FROM events WHERE id=?", (eid,))


def _reminder_text(ev: dict, rb: int, start_l: datetime) -> str:
    tail = f" ({ev['location']})" if ev.get("location") else ""
    if ev.get("all_day"):
        days = rb // 1440
        lead = "Сегодня" if days == 0 else "Завтра" if days == 1 else \
            f"Через {days} {plural(days, 'день', 'дня', 'дней')}"
        return f"{lead}: {ev['title']}{tail}"
    if rb == 0:
        return f"Сейчас: {ev['title']}{tail}"
    s = f"Через {human_minutes(rb)}: {ev['title']}{tail}"
    return s + (f" — в {start_l:%H:%M}" if rb >= 60 else "")


async def _make_reminder(ctx: ToolContext, ev: dict, rb: int | None) -> tuple[dict | None, str | None]:
    """Поставить напоминание о событии. → (строка reminders | None, пояснение, почему не поставил)."""
    if rb is None:
        return None, None
    tz = zone(ev.get("tz"), ctx.tz.key)
    start_l = datetime.fromisoformat(ev["local_start"]).replace(tzinfo=tz)
    if ev.get("all_day"):
        rem_l = datetime.combine(start_l.date() - timedelta(days=rb // 1440), time(9, 0)).replace(tzinfo=tz)
    else:
        rem_l = start_l - timedelta(minutes=rb)        # арифметика по местным часам
    rule = ev.get("rrule")
    if rule:
        off = (start_l.date() - rem_l.date()).days
        rule = shift_rrule(rule, off, start_l.date(), rem_l.time() != start_l.time())
        if rule is None:
            return None, ("для такого правила повтора напоминание заранее не ставлю — "
                          "если нужно, поставь отдельное через create_reminder")
    elif rem_l < timeutil.now_utc():
        return None, f"напоминание не ставил: его время ({timeutil.fmt_local(rem_l, tz)}) уже прошло"
    try:
        rem = await create_reminder(ctx, text=_reminder_text(ev, rb, start_l), when=rem_l, rrule=rule,
                                    kind="event", ref_type="event", ref_id=ev["id"])
    except ValueError as e:
        return None, f"напоминание не поставил: {e}"
    return rem, None


def _summary(ev: dict, tz: ZoneInfo, rem: dict | None, note: str | None) -> dict:
    ez = zone(ev.get("tz"), tz.key)
    out = {"ok": True, "id": ev["id"], "title": ev["title"],
           "when": _fmt_span(timeutil.from_iso(ev["starts_at"]), timeutil.from_iso(ev.get("ends_at")),
                             bool(ev.get("all_day")), ez if ev.get("all_day") else tz),
           "all_day": bool(ev.get("all_day")), "location": ev.get("location") or "",
           "repeat": timeutil.describe_rrule(ev.get("rrule")),
           "reminder": rem["when_local"] if rem else None}
    if rem:
        out["reminder_id"] = rem["id"]
    if note:
        out["note"] = note
    return out


# ── публичный API ───────────────────────────────────────────────────────────
async def add_event(ctx: ToolContext, *, title: str, start: str | datetime, end: str | datetime | None = None,
                    all_day: bool = False, location: str = "", notes: str = "", repeat: str | None = None,
                    remind_before_min: int | None = DEFAULT_REMIND_MIN) -> dict:
    """Добавить событие (+ напоминание). → строка events + "when", "repeat", "reminder",
    "reminder_id", "note" (почему напоминания нет, если его нет)."""
    tz = ctx.tz
    title = _clean_title(title)
    all_day = bool(as_bool(all_day, False))
    start_l = _parse_start(start, tz, all_day)
    end_l = _parse_end(end, start_l, tz, all_day)
    rule = clean_repeat(repeat)
    local_start = timeutil.naive_str(start_l)
    _check_rule(rule, datetime.fromisoformat(local_start))
    rb = _remind_minutes(remind_before_min)
    eid = await ctx.db.execute(
        "INSERT INTO events(title, local_start, tz, starts_at, ends_at, all_day, location, notes, rrule, "
        "status, created_at) VALUES(?,?,?,?,?,?,?,?,?,'active',?)",
        (title, local_start, tz.key, timeutil.iso(start_l), timeutil.iso(end_l), int(all_day),
         _clean_str(location, MAX_LOCATION), _clean_str(notes, MAX_NOTES), rule,
         timeutil.iso(timeutil.now_utc())))
    await ctx.db.kv_set(KV_REMIND.format(eid), rb)
    ev = await get_event(ctx.db, eid) or {}
    rem, note = await _make_reminder(ctx, ev, rb)
    s = _summary(ev, tz, rem, note)
    return {**ev, "when": s["when"], "repeat": s["repeat"], "reminder": s["reminder"],
            "reminder_id": rem["id"] if rem else None, "note": note}


async def update_event(ctx: ToolContext, eid: Any, *, title: str | None = None, start: Any = None,
                       end: Any = None, all_day: Any = None, location: str | None = None,
                       notes: str | None = None, repeat: str | None = None,
                       remind_before_min: Any = "keep") -> dict:
    """Изменить событие; перенос начала сохраняет длительность; напоминание пересоздаётся."""
    eid = as_id(eid)
    ev = await get_event(ctx.db, eid)
    if ev is None:
        raise ValueError(f"события #{eid} нет")
    if ev["status"] != "active":
        raise ValueError(f"событие #{eid} отменено")
    tz = zone(ev.get("tz"), ctx.tz.key)
    old_all_day = bool(ev["all_day"])
    new_all_day = bool(as_bool(all_day, old_all_day))
    old_start = datetime.fromisoformat(ev["local_start"]).replace(tzinfo=tz)
    old_end = (timeutil.from_iso(ev.get("ends_at")) or old_start).astimezone(tz)
    old_dur = old_end.replace(tzinfo=None) - old_start.replace(tzinfo=None)     # по местным часам
    start_given = start is not None and str(start).strip() != ""
    end_given = end is not None and str(end).strip() != ""

    if start_given:
        new_start = _parse_start(start, tz, new_all_day)
    else:
        new_start = _midnight(old_start.date(), tz) if new_all_day else old_start
    if end_given:
        new_end = _parse_end(end, new_start, tz, new_all_day)
    elif new_all_day != old_all_day:
        new_end = _parse_end(None, new_start, tz, new_all_day)
    elif start_given:
        new_end = (new_start.replace(tzinfo=None) + max(old_dur, timedelta(0))).replace(tzinfo=tz)
        if new_all_day:
            new_end = _midnight(new_end.date(), tz) if new_end.date() > new_start.date() else \
                _midnight(new_start.date() + timedelta(days=1), tz)
    else:
        new_end = old_end
    if new_end < new_start:
        raise ValueError("конец события раньше начала")

    rule = clean_repeat(repeat) if repeat is not None else ev.get("rrule")
    local_start = timeutil.naive_str(new_start)
    _check_rule(rule, datetime.fromisoformat(local_start))
    new_title = _clean_title(title) if title is not None and str(title).strip() else ev["title"]
    new_loc = _clean_str(location, MAX_LOCATION) if location is not None else ev["location"]
    new_notes = _clean_str(notes, MAX_NOTES) if notes is not None else ev["notes"]
    if remind_before_min is None or remind_before_min == "keep":     # не сказали — как было
        rb = await ctx.db.kv_get(KV_REMIND.format(eid), DEFAULT_REMIND_MIN)
        rb = _remind_minutes(rb)
    else:
        rb = _remind_minutes(remind_before_min)
    await ctx.db.execute(
        "UPDATE events SET title=?, local_start=?, tz=?, starts_at=?, ends_at=?, all_day=?, location=?, "
        "notes=?, rrule=? WHERE id=?",
        (new_title, local_start, tz.key, timeutil.iso(new_start), timeutil.iso(new_end), int(new_all_day),
         new_loc, new_notes, rule, eid))
    await ctx.db.kv_set(KV_REMIND.format(eid), rb)
    await cancel_by_ref(ctx.db, "event", eid)
    ev = await get_event(ctx.db, eid) or {}
    rem, note = await _make_reminder(ctx, ev, rb)
    return _summary(ev, ctx.tz, rem, note)


async def cancel_event(db, eid: int) -> dict | None:
    """Отменить событие и его напоминания. → строка события + "reminders_cancelled" (None — не нашлось)."""
    ev = await get_event(db, eid)
    if ev is None:
        return None
    await db.execute("UPDATE events SET status='cancelled' WHERE id=?", (ev["id"],))
    n = await cancel_by_ref(db, "event", ev["id"])
    await db.kv_delete(KV_REMIND.format(ev["id"]))
    ev = await get_event(db, ev["id"]) or ev
    ev["reminders_cancelled"] = n
    return ev


def _overlaps(a_s: datetime, a_e: datetime, s: datetime, e: datetime) -> bool:
    """[a_s, a_e) пересекается с [s, e); событие без длительности — если начало внутри окна."""
    return a_s < e and (a_e > s or a_s >= s)


_PERIOD = {"MINUTELY": timedelta(minutes=1), "HOURLY": timedelta(hours=1),
           "DAILY": timedelta(days=1), "WEEKLY": timedelta(weeks=1)}


def _fast_start(rule: str, start: datetime, lo: datetime) -> datetime:
    """dateutil перебирает вхождения от самого DTSTART — у «каждые 5 минут» с прошлого года это
    сотни тысяч шагов. Сдвигаем DTSTART к окну на целое число периодов (сетка вхождений та же)."""
    parts = dict(p.split("=", 1) for p in rule.upper().split(";") if "=" in p)
    per = _PERIOD.get(parts.get("FREQ", ""))
    if per is None or "COUNT" in parts or "BYSETPOS" in parts:
        return start
    try:
        step = per * max(1, int(parts.get("INTERVAL", "1")))
    except ValueError:
        return start
    k = (lo - start) // step - 1
    return start + step * k if k > 0 else start


def _occurrences(ev: dict, s: datetime, e: datetime, fallback: ZoneInfo) -> list[tuple[datetime, datetime]]:
    """Вхождения события в окно [s, e) — пары (начало, конец) в UTC."""
    ez = zone(ev.get("tz"), fallback.key)
    st = timeutil.from_iso(ev["starts_at"])
    en = timeutil.from_iso(ev.get("ends_at")) or st
    if en < st:
        en = st
    if not ev.get("rrule"):
        return [(st, en)] if _overlaps(st, en, s, e) else []
    start_naive = datetime.fromisoformat(ev["local_start"]).replace(tzinfo=None)
    dur = max(en.astimezone(ez).replace(tzinfo=None) - start_naive, timedelta(0))
    lo = s.astimezone(ez).replace(tzinfo=None) - dur - timedelta(days=1)
    hi = e.astimezone(ez).replace(tzinfo=None) + timedelta(days=1)
    try:
        rr = rrulestr(ev["rrule"], dtstart=_fast_start(ev["rrule"], start_naive, lo))
    except Exception as ex:          # кривое правило в базе — показываем как разовое
        log.warning("событие #%s: плохое правило %r: %s", ev.get("id"), ev.get("rrule"), ex)
        return [(st, en)] if _overlaps(st, en, s, e) else []
    out: list[tuple[datetime, datetime]] = []
    for i, occ in enumerate(rr.xafter(lo, inc=True)):
        if occ > hi or len(out) >= MAX_EXPAND or i >= MAX_EXPAND * 4:
            break
        o_s = timeutil.localize(occ, ez).astimezone(UTC)
        o_e = timeutil.localize(occ + dur, ez).astimezone(UTC)
        if _overlaps(o_s, o_e, s, e):
            out.append((o_s, o_e))
    return out


async def events_between(db, tz: Any, start_utc: datetime, end_utc: datetime) -> list[dict]:
    """Активные события в окне [start_utc, end_utc), повторы раскрыты. Каждый элемент — строка
    events + "occurs_at" (UTC ISO), "occurs_end" (UTC ISO), "occurs_local" ('пн 28.09 15:00'
    или 'чт 01.10, весь день'). Отсортировано по occurs_at."""
    z = zone(tz)
    s = start_utc if start_utc.tzinfo else start_utc.replace(tzinfo=UTC)
    e = end_utc if end_utc.tzinfo else end_utc.replace(tzinfo=UTC)
    rows = await db.fetchall(
        "SELECT * FROM events WHERE status='active' AND (rrule IS NOT NULL OR starts_at < ?) ORDER BY id",
        (timeutil.iso(e + timedelta(days=1)),))
    out: list[dict] = []
    for ev in rows:
        try:
            occs = _occurrences(ev, s, e, z)
        except Exception:            # одна битая строка не должна ломать весь календарь
            log.exception("событие #%s не раскрылось", ev.get("id"))
            continue
        for o_s, o_e in occs:
            item = dict(ev)
            item["occurs_at"] = timeutil.iso(o_s)
            item["occurs_end"] = timeutil.iso(o_e)
            if ev.get("all_day"):
                item["occurs_local"] = f"{_fmt_day(o_s.astimezone(zone(ev.get('tz'), z.key)).date(), z)}, весь день"
            else:
                item["occurs_local"] = timeutil.fmt_local(o_s, z)
            out.append(item)
    out.sort(key=lambda x: (x["occurs_at"], x["id"]))
    return out


# ── iCalendar (RFC 5545) ─────────────────────────────────────────────────────
_CTL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def ics_escape(s: Any) -> str:
    """TEXT по RFC 5545: \\ ; , и переводы строк экранируются, управляющие символы выкидываются."""
    s = _CTL.sub("", "" if s is None else str(s))
    s = s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return s.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")


def ics_fold(line: str) -> str:
    """Свернуть строку по 75 октетов (UTF-8), не разрывая многобайтовые символы."""
    if len(line.encode("utf-8")) <= 75:
        return line
    chunks, cur, size, limit = [], [], 0, 75
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > limit:
            chunks.append("".join(cur))
            cur, size, limit = [], 0, 74          # продолжение начинается с пробела
        cur.append(ch)
        size += n
    chunks.append("".join(cur))
    return "\r\n ".join(chunks)


def _utc_stamp(d: datetime) -> str:
    return d.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _offset(td: timedelta) -> str:
    secs = int(td.total_seconds())
    sign = "+" if secs >= 0 else "-"
    secs = abs(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{sign}{h:02d}{m:02d}" + (f"{s:02d}" if s else "")


def _vtimezone(name: str, y0: int, y1: int) -> list[str]:
    """VTIMEZONE с реальными переходами пояса за годы y0..y1 (из базы tzdata)."""
    tz = ZoneInfo(name)
    t = datetime(y0, 1, 1, tzinfo=UTC)
    end = datetime(y1 + 1, 1, 1, tzinfo=UTC)
    first = t.astimezone(tz)
    prev = first.utcoffset() or timedelta(0)
    obs = [("DAYLIGHT" if first.dst() else "STANDARD", t, prev, prev, first.tzname() or name)]
    day = timedelta(days=1)
    while t < end:
        nt = t + day
        off = nt.astimezone(tz).utcoffset() or timedelta(0)
        if off != prev:
            lo, hi = 0, 1440                                  # минута перехода внутри суток
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if (t + timedelta(minutes=mid)).astimezone(tz).utcoffset() == prev:
                    lo = mid
                else:
                    hi = mid
            at = t + timedelta(minutes=hi)
            loc = at.astimezone(tz)
            obs.append(("DAYLIGHT" if loc.dst() else "STANDARD", at, prev, off, loc.tzname() or name))
            prev = off
        t = nt
    lines = ["BEGIN:VTIMEZONE", f"TZID:{name}"]
    for kind, at, frm, to, tzname in obs:
        local_wall = (at + frm).replace(tzinfo=None)          # DTSTART — по старому смещению
        lines += [f"BEGIN:{kind}", f"DTSTART:{local_wall:%Y%m%dT%H%M%S}", f"TZOFFSETFROM:{_offset(frm)}",
                  f"TZOFFSETTO:{_offset(to)}", f"TZNAME:{ics_escape(tzname)}", f"END:{kind}"]
    lines.append("END:VTIMEZONE")
    return lines


def _ics_rrule(rule: str, all_day: bool, tz: ZoneInfo) -> str:
    """UNTIL у нас локальный наивный; в iCalendar — дата (весь день) или UTC 'Z' (с TZID)."""
    def fix(m: re.Match) -> str:
        naive = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")
        if all_day:
            return f"UNTIL={naive:%Y%m%d}"
        return "UNTIL=" + _utc_stamp(timeutil.localize(naive, tz))
    return re.sub(r"UNTIL=(\d{8}T\d{6})Z?", fix, rule.upper())


def _vevent(ev: dict, tz_name: str, stamp: str, tzids: dict[str, int]) -> list[str]:
    ez = zone(ev.get("tz") or tz_name, tz_name)
    st = timeutil.from_iso(ev["starts_at"])
    en = timeutil.from_iso(ev.get("ends_at"))
    rule = ev.get("rrule")
    lines = ["BEGIN:VEVENT", f"UID:{ev['id']}@baltia-oracle", f"DTSTAMP:{stamp}"]
    if ev.get("created_at"):
        lines.append(f"CREATED:{_utc_stamp(timeutil.from_iso(ev['created_at']))}")
    lines.append(f"SUMMARY:{ics_escape(ev.get('title'))}")
    if ev.get("all_day"):
        d0 = datetime.fromisoformat(ev["local_start"]).date()
        d1 = en.astimezone(ez).date() if en else d0
        if d1 <= d0:
            d1 = d0 + timedelta(days=1)
        lines += [f"DTSTART;VALUE=DATE:{d0:%Y%m%d}", f"DTEND;VALUE=DATE:{d1:%Y%m%d}"]
    elif rule:
        ls = datetime.fromisoformat(ev["local_start"]).replace(tzinfo=None)
        lines.append(f"DTSTART;TZID={ez.key}:{ls:%Y%m%dT%H%M%S}")
        if en and en > st:
            lines.append(f"DTEND;TZID={ez.key}:{en.astimezone(ez):%Y%m%dT%H%M%S}")
        tzids[ez.key] = min(tzids.get(ez.key, ls.year), ls.year)
    else:
        lines.append(f"DTSTART:{_utc_stamp(st)}")
        if en and en > st:
            lines.append(f"DTEND:{_utc_stamp(en)}")
    if rule:
        lines.append(f"RRULE:{_ics_rrule(rule, bool(ev.get('all_day')), ez)}")
    if ev.get("location"):
        lines.append(f"LOCATION:{ics_escape(ev['location'])}")
    if ev.get("notes"):
        lines.append(f"DESCRIPTION:{ics_escape(ev['notes'])}")
    lines += ["STATUS:CONFIRMED", "END:VEVENT"]
    return lines


def build_ics(events: list[dict], tz_name: str) -> bytes:
    """Календарь .ics (RFC 5545): CRLF, строки свёрнуты по 75 октетов, повторы — RRULE с TZID
    и VTIMEZONE. Одно событие — один VEVENT (раскрытые вхождения с тем же id склеиваются)."""
    tz_name = zone(tz_name).key
    stamp = _utc_stamp(timeutil.now_utc())
    tzids: dict[str, int] = {}
    body: list[str] = []
    seen: set = set()
    for ev in events:
        if ev.get("id") in seen:
            continue
        seen.add(ev.get("id"))
        try:
            body += _vevent(ev, tz_name, stamp, tzids)
        except Exception:
            log.exception("событие #%s не попало в .ics", ev.get("id"))
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Baltia Oracle//Calendar 1.0//RU",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:Baltia Oracle",
             f"X-WR-TIMEZONE:{tz_name}"]
    y_last = timeutil.now_utc().year + 5
    for name, y0 in sorted(tzids.items()):
        lines += _vtimezone(name, max(1970, min(y0, y_last)), y_last)
    lines += body
    lines.append("END:VCALENDAR")
    return ("\r\n".join(ics_fold(x) for x in lines) + "\r\n").encode("utf-8")


# ── инструменты для модели ──────────────────────────────────────────────────
def _bound(v: Any, tz: ZoneInfo, *, is_end: bool) -> datetime:
    """Граница периода: дата без времени — начало дня (для конца — конец дня включительно)."""
    if isinstance(v, str) and _DATE_ONLY.fullmatch(v):
        d = to_local(v, tz, default_time=time(0, 0)).date()
        return _midnight(d + timedelta(days=1) if is_end else d, tz)
    return to_local(v, tz)


def _item(it: dict, tz: ZoneInfo) -> dict:
    ez = zone(it.get("tz"), tz.key)
    all_day = bool(it.get("all_day"))
    out = {"id": it["id"], "title": it["title"],
           "when": _fmt_span(timeutil.from_iso(it["occurs_at"]), timeutil.from_iso(it.get("occurs_end")),
                             all_day, ez if all_day else tz),
           "all_day": all_day, "location": it.get("location") or "",
           "repeat": timeutil.describe_rrule(it.get("rrule"))}
    if it.get("notes"):
        out["notes"] = it["notes"][:300]
    return out


@tool("add_event",
      "Добавить событие в календарь: встреча, приём у врача, поездка, дедлайн, праздник. "
      "start — локальное 'YYYY-MM-DD HH:MM'; для события на весь день (all_day=true) — просто 'YYYY-MM-DD'. "
      "end — конец в том же формате или просто 'HH:MM' того же дня; не указан — +1 час, а для all_day — тот же день "
      "(многодневное: end = последний день 'YYYY-MM-DD'). repeat — RRULE для повторяющихся: FREQ=WEEKLY;BYDAY=TU "
      "(каждый вторник), FREQ=MONTHLY, FREQ=YEARLY; start — первое вхождение. "
      "remind_before_min — за сколько минут напомнить: по умолчанию 30; 0 — в момент начала; 60 — за час; "
      "1440 — за сутки; -1 — не напоминать. Для all_day напоминание в 09:00 дня события (или за N суток в 09:00). "
      "Для простого «напомни мне» без события — create_reminder.",
      {"title": {"type": "string", "description": "короткое название"},
       "start": {"type": "string", "description": "'YYYY-MM-DD HH:MM' или 'YYYY-MM-DD' для all_day"},
       "end": {"type": "string", "description": "'YYYY-MM-DD HH:MM', 'HH:MM' или 'YYYY-MM-DD' для all_day"},
       "all_day": {"type": "boolean", "description": "событие на весь день (без времени)"},
       "location": {"type": "string", "description": "место"},
       "notes": {"type": "string", "description": "подробности"},
       "repeat": {"type": "string", "description": "RRULE: FREQ=WEEKLY;BYDAY=TU | FREQ=YEARLY | …"},
       "remind_before_min": {"type": "integer", "description": "за сколько минут напомнить; -1 — не напоминать"}},
      required=["title", "start"])
async def t_add_event(ctx: ToolContext, *, title: str, start: str, end: str | None = None, all_day: Any = None,
                      location: str = "", notes: str = "", repeat: str | None = None,
                      remind_before_min: Any = DEFAULT_REMIND_MIN) -> dict:
    ad = as_bool(all_day)
    if ad is None:      # «5 октября — день рождения мамы»: дата без времени → на весь день
        ad = isinstance(start, str) and bool(_DATE_ONLY.fullmatch(start))
    rb = DEFAULT_REMIND_MIN if remind_before_min is None else remind_before_min   # null от модели = «не сказал»
    ev = await add_event(ctx, title=title, start=start, end=end, all_day=ad, location=location or "",
                         notes=notes or "", repeat=repeat, remind_before_min=rb)
    rem = {"id": ev["reminder_id"], "when_local": ev["reminder"]} if ev.get("reminder_id") else None
    return _summary(ev, ctx.tz, rem, ev.get("note"))


@tool("list_events",
      "События календаря за период, повторяющиеся раскрыты по датам. from/to — локальные 'YYYY-MM-DD' "
      "или 'YYYY-MM-DD HH:MM'; дата в to — включительно (весь тот день). По умолчанию — с текущего момента "
      "на 7 дней вперёд; только from — неделя от него.",
      {"from": {"type": "string", "description": "начало периода"},
       "to": {"type": "string", "description": "конец периода"}})
async def t_list_events(ctx: ToolContext, **kw: Any) -> dict:
    tz = ctx.tz
    frm, to = kw.get("from"), kw.get("to")
    start = _bound(frm, tz, is_end=False) if frm not in (None, "") else timeutil.now_utc()
    end = _bound(to, tz, is_end=True) if to not in (None, "") else start + timedelta(days=7)
    if end <= start:
        raise ValueError("конец периода раньше начала")
    if end - start > timedelta(days=400):
        raise ValueError("период больше года — сузь его")
    items = await events_between(ctx.db, tz, start, end)
    out = {"ok": True, "from": timeutil.fmt_local(start, tz), "to": timeutil.fmt_local(end, tz),
           "count": len(items), "items": [_item(it, tz) for it in items[:60]]}
    if len(items) > 60:
        out["note"] = "показаны первые 60 — сузь период, чтобы увидеть остальные"
    return out


@tool("update_event",
      "Изменить событие по id. Передавай только то, что меняется: title, start/end ('YYYY-MM-DD HH:MM'; "
      "перенос начала без end сдвигает и конец — длительность сохраняется), all_day, location, notes, "
      "repeat (RRULE; \"\" или \"none\" — убрать повтор), remind_before_min (-1 — не напоминать). "
      "Напоминание о событии пересоздаётся под новое время само.",
      {"id": {"type": "integer"},
       "title": {"type": "string"},
       "start": {"type": "string"},
       "end": {"type": "string"},
       "all_day": {"type": "boolean"},
       "location": {"type": "string"},
       "notes": {"type": "string"},
       "repeat": {"type": "string"},
       "remind_before_min": {"type": "integer"}},
      required=["id"])
async def t_update_event(ctx: ToolContext, *, id: Any, title: str | None = None, start: str | None = None,
                         end: str | None = None, all_day: Any = None, location: str | None = None,
                         notes: str | None = None, repeat: str | None = None,
                         remind_before_min: Any = "keep") -> dict:
    return await update_event(ctx, id, title=title, start=start, end=end, all_day=all_day, location=location,
                              notes=notes, repeat=repeat, remind_before_min=remind_before_min)


@tool("cancel_event",
      "Отменить (удалить) событие по id вместе с его напоминанием. Не знаешь id — сначала list_events.",
      {"id": {"type": "integer"}}, required=["id"])
async def t_cancel_event(ctx: ToolContext, *, id: Any) -> dict:
    eid = as_id(id)
    ev = await get_event(ctx.db, eid)
    if ev is None:
        raise ValueError(f"события #{eid} нет")
    if ev["status"] != "active":
        raise ValueError(f"событие #{eid} уже отменено")
    ev = await cancel_event(ctx.db, eid) or ev
    return {"ok": True, "id": ev["id"], "title": ev["title"], "reminders_cancelled": ev.get("reminders_cancelled", 0)}


@tool("export_calendar",
      "Выгрузить календарь файлом .ics: владелец откроет его, и события добавятся в его календарь "
      "(Google, Apple, Outlook). days_ahead — на сколько дней вперёд (по умолчанию 90). "
      "Файл уйдёт владельцу сам — просто скажи, что отправил.",
      {"days_ahead": {"type": "integer", "description": "дней вперёд, 1..3650"}})
async def t_export_calendar(ctx: ToolContext, *, days_ahead: Any = 90) -> dict:
    try:
        days = int(float(days_ahead)) if days_ahead is not None else 90
    except (TypeError, ValueError, OverflowError):
        days = 90
    days = min(3650, max(1, days))
    start = _midnight(ctx.now_local().date(), ctx.tz)           # с начала сегодняшнего дня
    items = await events_between(ctx.db, ctx.tz, start, ctx.now_utc() + timedelta(days=days))
    uniq: dict[int, dict] = {}
    for it in items:
        uniq.setdefault(it["id"], it)
    n = len(uniq)
    if n == 0:
        return {"ok": True, "events": 0, "note": f"на ближайшие {days} {plural(days, 'день', 'дня', 'дней')} "
                                                 f"событий нет — файл не отправлял"}
    data = build_ics(list(uniq.values()), ctx.tz.key)
    ctx.outbox.append(OutItem(kind="file", filename="calendar.ics", data=data,
                              text=f"Календарь: {n} {plural(n, 'событие', 'события', 'событий')} — "
                                   f"открой файл, и он добавится в твой календарь"))
    return {"ok": True, "events": n, "note": "файл отправлен владельцу"}
