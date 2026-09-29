"""Напоминания: разовые и повторяющиеся, будильники с «долбёжкой» и задачкой.

Строка `reminders`: `local_start` (первое срабатывание, локальное наивное) + `rrule` + `tz`;
ближайшее срабатывание — `next_at` (UTC ISO), его двигает scheduler. Долбёжка: `nag=1` —
после срабатывания повторять каждые `nag_interval_min`, пока владелец не отметит (`ack_reminder`).
`challenge=1` — отметить можно только решив задачку кнопкой (будильник «не дай проспать»);
пока такой будильник звонит (долбит или отложен «💤»), ни отметить, ни отменить, ни перенести его нельзя.

Публичные функции зовут scheduler, bot, calendar и другие инструменты; модели — инструменты внизу.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

from .. import timeutil
from .base import ToolContext, tool

KINDS = ("reminder", "wake", "event", "task", "followup")
MAX_TEXT = 1000
YEARS = (1900, 2199)                 # разумный диапазон дат от модели
_NO_REPEAT = {"", "none", "no", "null", "once", "нет", "разово", "однократно", "-"}
_TRUE = {"1", "true", "yes", "on", "да", "y"}
_FALSE = {"0", "false", "no", "off", "нет", "n", ""}


# ── мелкие помощники (их же берёт calendar) ─────────────────────────────────
def as_bool(v: Any, default: bool | None = None) -> bool | None:
    """Булево от модели: true/"true"/"да"/1 → True; None → default."""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return default


def as_id(v: Any, what: str = "id") -> int:
    """id от модели: 12, "12", "#12" → 12; иначе ValueError."""
    try:
        if isinstance(v, bool):
            raise ValueError
        return int(str(v).strip().lstrip("#№").strip())
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{what} должен быть числом, а пришло «{v}»") from None


def zone(tz: Any, fallback: str = "UTC") -> ZoneInfo:
    """ZoneInfo из ZoneInfo или имени пояса (кривое имя → fallback)."""
    if isinstance(tz, ZoneInfo):
        return tz
    try:
        return ZoneInfo(str(tz))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return ZoneInfo(fallback)


def to_local(when: Any, tz: ZoneInfo, *, default_time=None) -> datetime:
    """Строка 'YYYY-MM-DD HH:MM' / aware или наивный datetime / date → aware локальное время.
    Год вне 1900–2199 — ValueError (и от опечаток модели, и от переполнений дат)."""
    if isinstance(when, datetime):
        d = when.replace(tzinfo=tz) if when.tzinfo is None else when
    elif isinstance(when, date):
        d = datetime.combine(when, default_time or time(9, 0)).replace(tzinfo=tz)
    elif isinstance(when, str):
        d = timeutil.parse_local(when, tz, **({"default_time": default_time} if default_time is not None else {}))
    else:
        raise ValueError(f"не понял время «{when}» — нужен формат YYYY-MM-DD HH:MM")
    if not YEARS[0] <= d.year <= YEARS[1]:
        raise ValueError(f"странная дата «{when}» — год должен быть между {YEARS[0]} и {YEARS[1]}")
    try:
        return d.astimezone(tz)
    except (OverflowError, ValueError):
        raise ValueError(f"странная дата «{when}»") from None


_BY_RANGES = {"BYMONTH": (1, 12), "BYMONTHDAY": (-31, 31), "BYYEARDAY": (-366, 366), "BYWEEKNO": (-53, 53),
              "BYHOUR": (0, 23), "BYMINUTE": (0, 59), "BYSECOND": (0, 59), "BYSETPOS": (-366, 366)}
_NONZERO = {"BYMONTHDAY", "BYYEARDAY", "BYWEEKNO", "BYSETPOS"}
_MONTH_DAYS = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
_BYDAY = re.compile(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)")


def _feasible(rule: str) -> bool:
    """Даёт ли правило хоть одну дату. Невыполнимое (BYMONTH=13, 30 февраля, 6-й понедельник месяца)
    dateutil перебирал бы до 9999 года, подвешивая бота на секунды, — отсекаем заранее и быстро."""
    parts = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
    for key, (lo, hi) in _BY_RANGES.items():
        for x in filter(None, parts.get(key, "").split(",")):
            n = int(x)
            if not lo <= n <= hi or (n == 0 and key in _NONZERO):
                return False
    lim = 5 if parts.get("FREQ") == "MONTHLY" or "BYMONTH" in parts else 53
    for x in filter(None, parts.get("BYDAY", "").split(",")):
        m = _BYDAY.fullmatch(x)
        if not m or (m.group(1) and not 1 <= abs(int(m.group(1))) <= lim):
            return False
    if "BYMONTH" in parts and "BYMONTHDAY" in parts:
        months = [int(x) for x in parts["BYMONTH"].split(",") if x]
        days = [int(x) for x in parts["BYMONTHDAY"].split(",") if x]
        if not any(abs(d) <= _MONTH_DAYS[m - 1] for m in months for d in days):
            return False
    # остаток проверяем «годовой проекцией» правила: годовой перебор до 9999 — доли секунды, а не секунды
    keep = [p for p in rule.split(";") if p and not p.startswith(("INTERVAL=", "COUNT=", "UNTIL="))]
    if "BYSETPOS" not in parts:
        keep = ["FREQ=YEARLY" if p.startswith("FREQ=") else p for p in keep]
    start = datetime(2000, 1, 1)
    return rrulestr(";".join(keep), dtstart=start).after(start, inc=True) is not None


def clean_repeat(repeat: Any, tz: ZoneInfo | None = None) -> str | None:
    """Правило повтора от модели → нормализованный RRULE или None («none», "" — без повтора).
    tz — пояс, в котором раскрывается правило: в него переводится UNTIL=…Z."""
    if repeat is None:
        return None
    if not isinstance(repeat, str):
        raise ValueError(f"повтор должен быть строкой RRULE, а пришло «{repeat}»")
    if repeat.strip().lower() in _NO_REPEAT:
        return None
    try:
        rule = timeutil.normalize_rrule(repeat, tz)
        ok = rule is None or _feasible(rule)
    except ValueError as e:
        if re.search(r"[а-яё]", str(e), re.I):     # наш человеческий текст из normalize_rrule
            raise
        raise ValueError(f"не понял правило повтора «{repeat}» — нужен RRULE вроде FREQ=DAILY "
                         f"или FREQ=WEEKLY;BYDAY=MO,WE,FR") from None
    except Exception:  # dateutil умеет кидать и другое
        raise ValueError(f"не понял правило повтора «{repeat}» — нужен RRULE вроде FREQ=DAILY "
                         f"или FREQ=WEEKLY;BYDAY=MO,WE,FR") from None
    if not ok:
        raise ValueError(f"правило повтора «{repeat}» не даёт ни одной даты — проверь значения BY…-частей")
    return rule


def _clean_text(text: Any) -> str:
    s = "" if text is None else str(text)
    s = s.strip()
    if not s:
        raise ValueError("пустой текст напоминания — о чём напомнить?")
    if len(s) > MAX_TEXT:
        raise ValueError(f"текст напоминания длиннее {MAX_TEXT} символов — сократи до сути")
    return s


def _as_int(v: Any, default: int) -> int:
    try:
        if v is None or isinstance(v, bool):
            return default
        return int(float(v))
    except (TypeError, ValueError, OverflowError):
        return default


def _next_at(rrule: str | None, local_start: str, tz: ZoneInfo) -> datetime:
    """Ближайшее срабатывание (≥ сейчас) или ValueError человеческим текстом.
    Разовое, опоздавшее не больше чем на PAST_GRACE («через минуту», а модель думала дольше), — не ошибка:
    сработает на ближайшем тике."""
    now = timeutil.now_utc()
    try:
        nxt = timeutil.next_occurrence(rrule, local_start, tz, inclusive=True,
                                       after=now if rrule else now - timeutil.PAST_GRACE)
    except ValueError:
        raise
    except Exception:
        raise ValueError(f"не смог разобрать правило повтора «{rrule}»") from None
    if nxt is None:
        if rrule:
            raise ValueError("у повтора нет будущих срабатываний — проверь UNTIL/COUNT и дату начала")
        when = timeutil.fmt_local(timeutil.localize(datetime.fromisoformat(local_start), tz), tz)
        raise ValueError(f"это время уже прошло: {when} (сейчас {timeutil.fmt_local(now, tz)})")
    return nxt


WAKE_NOTE = ("Будильник — это сообщения Telegram каждые несколько минут. Если телефон на ночь в «Не беспокоить» — "
             "разреши Telegram в исключениях и не глуши чат бота, иначе не услышишь.")
WAKE_NOTE_KEY = "wake_dnd_told"
CHALLENGE_BUSY = ("будильник с задачкой сейчас звонит — снять его можно только решив задачку кнопкой; "
                  "отменить или перенести — после")


def ringing_challenge(row: dict) -> bool:
    """Будильник с задачкой звонит прямо сейчас (долбит или отложен «💤») — трогать его можно только задачкой."""
    return bool(row.get("challenge")) and bool(row.get("nag_active") or row.get("snooze_at")) \
        and (row.get("status") or "active") == "active"


# ── публичный API ───────────────────────────────────────────────────────────
async def get_reminder(db, rid: int) -> dict | None:
    try:
        rid = int(rid)
    except (TypeError, ValueError, OverflowError):
        return None
    return await db.fetchone("SELECT * FROM reminders WHERE id=?", (rid,))


async def create_reminder(ctx: ToolContext, *, text: str, when: str | datetime, rrule: str | None = None,
                          kind: str = "reminder", nag: bool | None = None, challenge: bool | None = None,
                          nag_interval_min: int | None = None, ref_type: str | None = None,
                          ref_id: int | None = None, tz: ZoneInfo | None = None,
                          nag_max: int | None = None) -> dict:
    """Создать напоминание. Возвращает строку reminders + "when_local" и "repeat".
    tz — пояс, в котором читается when и раскрывается повтор (по умолчанию — текущий ctx.tz; календарь
    передаёт пояс события, чтобы событие и напоминание о нём не разъезжались). nag_max — сколько раз
    долбить (по умолчанию cfg.nag_max)."""
    text = _clean_text(text)
    kind = (str(kind or "reminder")).strip().lower()
    if kind not in KINDS:
        raise ValueError(f"неизвестный вид напоминания «{kind}» — бывает: {', '.join(KINDS)}")
    tz = tz or ctx.tz
    local = to_local(when, tz)
    rule = clean_repeat(rrule, tz)
    local_start = timeutil.naive_str(local)
    nxt = _next_at(rule, local_start, tz)

    is_wake = kind == "wake"
    nag_v = as_bool(nag, is_wake)
    challenge_v = as_bool(challenge, bool(ctx.cfg.wake_challenge) if is_wake else False)
    interval = max(1, _as_int(nag_interval_min, ctx.cfg.nag_interval_min))
    n_max = max(1, _as_int(nag_max, int(ctx.cfg.nag_max)))
    now = timeutil.iso(timeutil.now_utc())
    rid = await ctx.db.execute(
        "INSERT INTO reminders(text, kind, local_start, tz, rrule, next_at, status, nag, nag_interval_min, "
        "nag_max, challenge, ref_type, ref_id, created_at) VALUES(?,?,?,?,?,?,'active',?,?,?,?,?,?,?)",
        (text, kind, local_start, tz.key, rule, timeutil.iso(nxt), int(bool(nag_v)), interval,
         n_max, int(bool(challenge_v)), ref_type,
         int(ref_id) if ref_id is not None else None, now))
    row = await get_reminder(ctx.db, rid) or {}
    row["when_local"] = timeutil.fmt_local(nxt, ctx.tz)       # модель считает время в текущем поясе
    row["repeat"] = timeutil.describe_rrule(rule)
    return row


async def list_active(db, limit: int = 50) -> list[dict]:
    """Активные напоминания: ближайшие первыми, без next_at (долбящие, отложенные) — в конце."""
    return await db.fetchall(
        "SELECT * FROM reminders WHERE status='active' "
        "ORDER BY (next_at IS NULL), next_at, id LIMIT ?", (max(1, _as_int(limit, 50)),))


async def cancel_reminder(db, rid: int) -> bool:
    """Отменить. False — не нашлось или уже отменено."""
    try:
        rid = int(rid)
    except (TypeError, ValueError, OverflowError):
        return False
    n = await db.execute(
        "UPDATE reminders SET status='cancelled', nag_active=0, nag_next_at=NULL, snooze_at=NULL "
        "WHERE id=? AND status!='cancelled'", (rid,))
    return n > 0


async def cancel_by_ref(db, ref_type: str, ref_id: int) -> int:
    """Отменить все активные напоминания, привязанные к сущности (событие, задача…). → сколько."""
    return await db.execute(
        "UPDATE reminders SET status='cancelled', nag_active=0, nag_next_at=NULL, snooze_at=NULL "
        "WHERE ref_type=? AND ref_id=? AND status='active'", (ref_type, int(ref_id)))


async def ack_reminder(db, rid: int) -> dict | None:
    """Отметить: стоп долбёжки и «отложенного»; будущих срабатываний нет → status='done'."""
    row = await get_reminder(db, rid)
    if row is None:
        return None
    await db.execute(
        "UPDATE reminders SET nag_active=0, nag_next_at=NULL, nag_count=0, snooze_at=NULL, "
        "status=CASE WHEN next_at IS NULL AND status='active' THEN 'done' ELSE status END "
        "WHERE id=?", (row["id"],))
    return await get_reminder(db, row["id"])


async def snooze_reminder(db, rid: int, minutes: int) -> dict | None:
    """Отложить на minutes (1..1440): долбёжка стоп, повтор в snooze_at, статус снова active.
    Отменённое не оживляется — None, как и для несуществующего."""
    row = await get_reminder(db, rid)
    if row is None or row.get("status") == "cancelled":
        return None
    m = min(1440, max(1, _as_int(minutes, 10)))
    at = timeutil.iso(timeutil.now_utc() + timedelta(minutes=m))
    await db.execute(
        "UPDATE reminders SET nag_active=0, nag_next_at=NULL, snooze_at=?, status='active' "
        "WHERE id=? AND status!='cancelled'",
        (at, row["id"]))
    return await get_reminder(db, row["id"])


_KIND_MARK = {"wake": "⏰ ", "event": "📅 ", "followup": "🧠 "}


def render_reminder(row: dict, tz: Any) -> str:
    """Одна строка для списка: '#12 · пн 29.09 07:30 · каждый день · ⏰ Подъём · долбит до отметки, с задачкой'."""
    z = zone(tz, str(row.get("tz") or "UTC"))
    if row.get("next_at"):
        when = timeutil.fmt_local(row["next_at"], z)
    elif row.get("nag_active"):
        when = "сейчас"
    else:
        when = "—"
    text = " ".join(str(row.get("text") or "").split())
    if len(text) > 200:
        text = text[:199].rstrip() + "…"
    text = _KIND_MARK.get(str(row.get("kind") or ""), "") + text
    s = " · ".join((f"#{row.get('id')}", when, timeutil.describe_rrule(row.get("rrule")), text))
    flags = []
    if row.get("nag"):
        flags.append("долбит до отметки")
    if row.get("challenge"):
        flags.append("с задачкой")
    if row.get("snooze_at") and row.get("status", "active") == "active":
        flags.append(f"отложено до {timeutil.fmt_local(row['snooze_at'], z)}")
    if row.get("status") and row["status"] != "active":
        flags.append({"done": "выполнено", "cancelled": "отменено"}.get(row["status"], row["status"]))
    if flags:
        s += " · " + ", ".join(flags)
    if row.get("nag_active"):
        s += " (долбит сейчас)"
    return s


# ── инструменты для модели ──────────────────────────────────────────────────
def _brief(row: dict, tz: ZoneInfo) -> dict:
    """Короткая карточка напоминания для модели."""
    when = timeutil.fmt_local(row["next_at"], tz) if row.get("next_at") else None
    out = {"id": row["id"], "text": row["text"], "when": when,
           "repeat": timeutil.describe_rrule(row.get("rrule")), "kind": row["kind"],
           "nag": bool(row.get("nag")), "nagging": bool(row.get("nag_active"))}
    if row.get("challenge"):
        out["challenge"] = True
    if row.get("snooze_at"):
        out["snoozed_until"] = timeutil.fmt_local(row["snooze_at"], tz)
    return out


async def _active_or_fail(ctx: ToolContext, rid: Any) -> dict:
    rid = as_id(rid)
    row = await get_reminder(ctx.db, rid)
    if row is None:
        raise ValueError(f"напоминания #{rid} нет")
    if row["status"] != "active":
        st = {"done": "уже выполнено", "cancelled": "уже отменено"}.get(row["status"], row["status"])
        raise ValueError(f"напоминание #{rid} {st} — поставь новое")
    return row


@tool("create_reminder",
      "Поставить напоминание или будильник. when — локальное время 'YYYY-MM-DD HH:MM' "
      "(«через час», «завтра в 7», «в пятницу» посчитай сам от текущего момента). "
      "repeat — только для повторяющихся: правило RRULE без префикса, например FREQ=DAILY (каждый день), "
      "FREQ=WEEKLY;BYDAY=MO,WE,FR (по пн, ср, пт), FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR (по будням), "
      "FREQ=MONTHLY (каждый месяц в тот же день), FREQ=YEARLY, FREQ=DAILY;INTERVAL=2 (через день); "
      "«до 5 октября включительно» — ;UNTIL=20261005T235959 (местное время, без Z); "
      "when при этом — первое срабатывание. kind=wake — будильник («разбуди», «не дай проспать», «подъём»): "
      "долбит каждые несколько минут, пока он не встанет, и снимается только решением задачки кнопкой. "
      "nag=true — обычное напоминание, которое долбит, пока его не отметят (для важного, что нельзя пропустить). "
      "Разовое время в прошлом — ошибка.",
      {"text": {"type": "string", "description": "о чём напомнить — коротко, как напишешь ему в момент срабатывания"},
       "when": {"type": "string", "description": "локальное 'YYYY-MM-DD HH:MM'"},
       "repeat": {"type": "string", "description": "RRULE: FREQ=DAILY | FREQ=WEEKLY;BYDAY=MO,WE,FR | …; не указывай для разового"},
       "nag": {"type": "boolean", "description": "долбить, пока не отметит"},
       "kind": {"type": "string", "enum": ["reminder", "wake"],
                "description": "wake — будильник с задачкой; по умолчанию reminder"}},
      required=["text", "when"])
async def t_create_reminder(ctx: ToolContext, *, text: str, when: str, repeat: str | None = None,
                            nag: Any = None, kind: str | None = None) -> dict:
    k = (str(kind).strip().lower() if kind else "reminder") or "reminder"
    if k not in ("reminder", "wake"):
        k = "reminder"
    row = await create_reminder(ctx, text=text, when=when, rrule=repeat, kind=k, nag=as_bool(nag))
    out = {"ok": True, "id": row["id"], "text": row["text"], "when": row["when_local"],
           "repeat": row["repeat"], "kind": row["kind"], "nag": bool(row["nag"])}
    if row.get("challenge"):
        out["challenge"] = True
    if row["kind"] == "wake" and not await ctx.db.kv_get(WAKE_NOTE_KEY):   # один раз — с первым будильником
        out["note"] = WAKE_NOTE
        await ctx.db.kv_set(WAKE_NOTE_KEY, True)
    return out


@tool("list_reminders",
      "Список активных напоминаний и будильников: id, текст, когда ближайшее срабатывание, повтор, "
      "долбит ли (nag) и долбит ли прямо сейчас (nagging).")
async def t_list_reminders(ctx: ToolContext) -> dict:
    rows = await list_active(ctx.db, 50)
    return {"ok": True, "items": [_brief(r, ctx.tz) for r in rows]}


@tool("update_reminder",
      "Изменить активное напоминание по id. Передавай только то, что меняется: text — новый текст; "
      "when — новое время 'YYYY-MM-DD HH:MM' (для повторяющегося — новое первое срабатывание, время дня берётся "
      "из него, и сдвигается ВСЯ серия); repeat — новое правило RRULE (FREQ=DAILY, FREQ=WEEKLY;BYDAY=MO,TH …), "
      "а \"\" или \"none\" — убрать повтор и оставить одно ближайшее срабатывание; nag — долбить ли до отметки "
      "(nag=false заодно останавливает текущую долбёжку). only_next=true — у повторяющегося тронуть только "
      "ближайшее срабатывание (его время — when из list_reminders), серия остаётся как была: без when — просто "
      "пропустить его («завтра не буди»), с when — перенести его на это время («завтра разбуди в 9, а не в 7»). "
      "Текущая долбёжка при переносе сбрасывается. Будильник с задачкой, пока звонит, не переносится.",
      {"id": {"type": "integer"},
       "text": {"type": "string"},
       "when": {"type": "string", "description": "локальное 'YYYY-MM-DD HH:MM'"},
       "repeat": {"type": "string", "description": "RRULE; \"\" или \"none\" — без повтора"},
       "nag": {"type": "boolean"},
       "only_next": {"type": "boolean", "description": "только ближайшее срабатывание повторяющегося"}},
      required=["id"])
async def t_update_reminder(ctx: ToolContext, *, id: Any, text: str | None = None, when: str | None = None,
                            repeat: str | None = None, nag: Any = None, only_next: Any = None) -> dict:
    row = await _active_or_fail(ctx, id)
    old_tz = zone(row.get("tz"), ctx.tz.key)
    text_given = text is not None and str(text).strip() != ""
    new_text = _clean_text(text) if text_given else row["text"]
    new_nag = as_bool(nag, bool(row["nag"]))
    when_given = when is not None and str(when).strip() != ""
    if as_bool(only_next, False) and row.get("rrule"):
        if repeat is not None:
            raise ValueError("only_next — только про ближайшее срабатывание; правило повтора меняй отдельным вызовом")
        return await _only_next(ctx, row, text=new_text, when=when if when_given else None, nag=new_nag)
    if ringing_challenge(row) and (when_given or repeat is not None or (row.get("nag") and not new_nag)):
        raise ValueError(CHALLENGE_BUSY)
    if not when_given and repeat is None:           # только текст/nag — расписание не трогаем
        if not new_nag and (row.get("nag_active") or row.get("nag_next_at")):
            # «не долби меня этим»: стоп текущей долбёжки; разовому, которому больше нечего ждать, — конец
            await ctx.db.execute(
                "UPDATE reminders SET text=?, nag=0, nag_active=0, nag_next_at=NULL, nag_count=0, "
                "status=CASE WHEN next_at IS NULL AND snooze_at IS NULL THEN 'done' ELSE status END WHERE id=?",
                (new_text, row["id"]))
        else:
            await ctx.db.execute("UPDATE reminders SET text=?, nag=? WHERE id=?",
                                 (new_text, int(bool(new_nag)), row["id"]))
    else:
        # новое время модель считает по текущим часам — в текущем поясе; без нового времени
        # остаётся пояс строки (иначе старое наивное local_start прочиталось бы в чужом поясе)
        tz = ctx.tz if when_given else old_tz
        rule = clean_repeat(repeat, tz) if repeat is not None else row.get("rrule")
        if when_given:
            local_start = timeutil.naive_str(to_local(when, tz))
        elif rule is None and row.get("rrule") and row.get("next_at"):
            # повтор убрали — остаётся ближайшее плановое срабатывание
            local_start = timeutil.naive_str(timeutil.from_iso(row["next_at"]).astimezone(tz))
        else:
            local_start = row["local_start"]
        nxt = _next_at(rule, local_start, tz)
        await ctx.db.execute(
            "UPDATE reminders SET text=?, nag=?, local_start=?, tz=?, rrule=?, next_at=?, nag_active=0, "
            "nag_next_at=NULL, nag_count=0, snooze_at=NULL, status='active' WHERE id=?",
            (new_text, int(bool(new_nag)), local_start, tz.key, rule, timeutil.iso(nxt), row["id"]))
    new = await get_reminder(ctx.db, row["id"])
    b = _brief(new, ctx.tz)
    out = {"ok": True, "id": b["id"], "text": b["text"], "when": b["when"], "repeat": b["repeat"],
           "kind": b["kind"], "nag": b["nag"], "nagging": b["nagging"]}
    if new.get("status") != "active":
        out["status"] = new["status"]
    return out


async def _only_next(ctx: ToolContext, row: dict, *, text: str, when: str | None, nag: bool) -> dict:
    """Пропустить (или перенести на when) только ближайшее срабатывание повторяющегося; серия — как была.
    Сработает, потому что scheduler считает следующее вхождение от local_start после «сейчас».
    Оговорка: следующий update_reminder с when/repeat пересчитает next_at от local_start и пропуск снимет."""
    if ringing_challenge(row):
        raise ValueError(CHALLENGE_BUSY)
    tz = zone(row.get("tz"), ctx.tz.key)
    cur = timeutil.from_iso(row.get("next_at"))
    if cur is None:
        raise ValueError(f"у напоминания #{row['id']} нет ближайшего срабатывания — пропускать нечего")
    once = None
    if when is not None:          # сначала разовое: не вышло (время в прошлом) — серию не трогаем
        once = await create_reminder(ctx, text=text, when=when, kind=row.get("kind") or "reminder", nag=nag,
                                     challenge=bool(row.get("challenge")),
                                     nag_interval_min=row.get("nag_interval_min"), nag_max=row.get("nag_max"))
    try:
        nxt = timeutil.next_occurrence(row["rrule"], row["local_start"], tz, after=cur)
    except Exception:
        nxt = None
    await ctx.db.execute(
        "UPDATE reminders SET next_at=?, nag_active=0, nag_next_at=NULL, nag_count=0, snooze_at=NULL, "
        "status=? WHERE id=?",
        (timeutil.iso(nxt) if nxt else None, "active" if nxt else "done", row["id"]))
    out = {"ok": True, "id": row["id"], "text": row["text"], "skipped": timeutil.fmt_local(cur, ctx.tz),
           "repeat": timeutil.describe_rrule(row.get("rrule")),
           "series_next": timeutil.fmt_local(nxt, ctx.tz) if nxt else None}
    if once is not None:
        out["once"] = {"id": once["id"], "when": once["when_local"], "text": once["text"]}
    return out


@tool("cancel_reminder",
      "Отменить (удалить) напоминание или будильник по id; у повторяющегося — все будущие срабатывания. "
      "Остановить текущую долбёжку — ack_reminder; пропустить или перенести одно срабатывание повторяющегося — "
      "update_reminder(only_next=true). Будильник с задачкой, пока звонит, отменить нельзя — только задачкой. "
      "Если не знаешь id — сначала list_reminders.",
      {"id": {"type": "integer"}}, required=["id"])
async def t_cancel_reminder(ctx: ToolContext, *, id: Any) -> dict:
    row = await _active_or_fail(ctx, id)
    if ringing_challenge(row):
        raise ValueError(CHALLENGE_BUSY)
    await cancel_reminder(ctx.db, row["id"])
    return {"ok": True, "id": row["id"], "text": row["text"], "cancelled": True}


@tool("ack_reminder",
      "Отметить напоминание по id: остановить долбёжку, а разовое — закрыть (он сказал «сделал», «встал», "
      "«хватит», «понял»). Повторяющееся после отметки продолжит срабатывать по расписанию. "
      "Будильник с задачкой, пока долбит, так не снять — только решив задачку кнопкой.",
      {"id": {"type": "integer"}}, required=["id"])
async def t_ack_reminder(ctx: ToolContext, *, id: Any) -> dict:
    row = await _active_or_fail(ctx, id)
    if ringing_challenge(row):
        raise ValueError("это будильник с задачкой — снять его можно только решив задачку кнопкой")
    if not row.get("rrule"):        # разовое отмечено (хоть и заранее) — больше не сработает
        await ctx.db.execute("UPDATE reminders SET next_at=NULL WHERE id=?", (row["id"],))
    new = await ack_reminder(ctx.db, row["id"]) or row
    return {"ok": True, "id": new["id"], "text": new["text"], "status": new["status"],
            "next": timeutil.fmt_local(new["next_at"], ctx.tz) if new.get("next_at") else None}


@tool("snooze_reminder",
      "Отложить то, что сейчас сработало или долбит, на minutes минут — как кнопка «💤»: «отложи на 10 минут», "
      "«дай ещё полчаса поспать», «напомни об этом через час ещё раз». Долбёжка останавливается и через minutes "
      "начнётся снова; расписание повторяющегося не меняется (перенести само время — update_reminder). "
      "Будильник с задачкой и после этого снимается только задачкой. id — из «СЕЙЧАС ДОЛБЯТ» или list_reminders.",
      {"id": {"type": "integer"},
       "minutes": {"type": "integer", "description": "на сколько минут отложить, 1–1440 (по умолчанию 10)"}},
      required=["id"])
async def t_snooze_reminder(ctx: ToolContext, *, id: Any, minutes: Any = 10) -> dict:
    rid = as_id(id)
    row = await get_reminder(ctx.db, rid)
    if row is None or row.get("status") == "cancelled":       # сработавшее разовое (done) — можно, как кнопкой
        raise ValueError(f"напоминания #{rid} нет или оно отменено — поставь новое")
    try:
        m = int(float(minutes if minutes not in (None, "") else 10))
    except (TypeError, ValueError):
        raise ValueError("minutes — число минут, 1–1440") from None
    if not 1 <= m <= 1440:
        raise ValueError("отложить можно на 1–1440 минут")
    new = await snooze_reminder(ctx.db, rid, m) or row
    out = {"ok": True, "id": new["id"], "text": new["text"],
           "until": timeutil.fmt_local(new["snooze_at"], ctx.tz) if new.get("snooze_at") else None}
    if new.get("challenge"):
        out["note"] = "будильник с задачкой: когда зазвонит снова, снять его можно только задачкой"
    return out
