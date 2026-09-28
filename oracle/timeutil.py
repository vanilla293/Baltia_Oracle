"""Время: локальное ↔ UTC, разбор дат от модели, повторы (RRULE).

Правило проекта: в базе все моменты — UTC ISO ('2026-09-28T05:30:00+00:00');
первое срабатывание повторяющихся вещей хранится как локальное «наивное» время
(`local_start`, 'YYYY-MM-DDTHH:MM:SS') плюс имя часового пояса — так повторы
«каждый день в 7:00» не съезжают при переходе на летнее/зимнее время.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

UTC = timezone.utc

# подменяемые часы (тесты ставят свои)
_clock: Callable[[], datetime] = lambda: datetime.now(UTC)


def set_clock(fn: Callable[[], datetime] | None) -> None:
    """Подменить «сейчас» (тесты). None — вернуть настоящее время."""
    global _clock
    _clock = fn or (lambda: datetime.now(UTC))


def now_utc() -> datetime:
    d = _clock()
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def now_local(tz: ZoneInfo) -> datetime:
    return now_utc().astimezone(tz)


def iso(dt: datetime) -> str:
    """Aware datetime → UTC ISO с точностью до секунды."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime: нужен часовой пояс")
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def from_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def naive_str(dt: datetime) -> str:
    """Локальное наивное время для local_start."""
    return dt.replace(tzinfo=None, microsecond=0).isoformat()


def localize(naive: datetime, tz: ZoneInfo) -> datetime:
    return naive.replace(tzinfo=tz)


_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
_WEEKDAYS_FULL = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
_MONTHS_GEN = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
               "августа", "сентября", "октября", "ноября", "декабря")


def weekday_name(d: date, full: bool = True) -> str:
    return (_WEEKDAYS_FULL if full else _WEEKDAYS)[d.weekday()]


def month_gen(m: int) -> str:
    return _MONTHS_GEN[m - 1]


def fmt_local(dt_utc: datetime | str | None, tz: ZoneInfo, with_weekday: bool = True) -> str:
    """'пн 29.09 07:30' (год — если не текущий)."""
    if isinstance(dt_utc, str):
        dt_utc = from_iso(dt_utc)
    if dt_utc is None:
        return "—"
    d = dt_utc.astimezone(tz)
    now = now_local(tz)
    s = d.strftime("%d.%m %H:%M") if d.year == now.year else d.strftime("%d.%m.%Y %H:%M")
    return f"{_WEEKDAYS[d.weekday()]} {s}" if with_weekday else s


def fmt_now_for_prompt(tz: ZoneInfo) -> str:
    """'2026-09-28 21:40, воскресенье (Europe/Moscow, UTC+03:00)' — для модели."""
    d = now_local(tz)
    off = d.strftime("%z")
    off = f"UTC{off[:3]}:{off[3:]}" if off else "UTC"
    return f"{d.strftime('%Y-%m-%d %H:%M')}, {weekday_name(d.date())} ({tz.key}, {off})"


_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
    "%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S",
)


def parse_local(s: str, tz: ZoneInfo, *, default_time: time = time(9, 0),
                prefer_future: bool = True) -> datetime:
    """Строка от модели → aware локальное время.

    Понимает 'YYYY-MM-DD HH:MM[:SS]', ISO с 'T' (и со смещением — тогда переводит в tz),
    'DD.MM.YYYY HH:MM', одну дату 'YYYY-MM-DD' / 'DD.MM.YYYY' (время = default_time)
    и одно время 'HH:MM' (сегодня, а если уже прошло и prefer_future — завтра).
    """
    if not isinstance(s, str) or not s.strip():
        raise ValueError("пустая дата/время")
    raw = s.strip().replace("Z", "+00:00")
    # ISO со смещением
    if re.search(r"[+-]\d{2}:?\d{2}$", raw) and "T" in raw:
        try:
            d = datetime.fromisoformat(raw)
            return d.astimezone(tz)
        except ValueError:
            pass
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            d = datetime.strptime(raw, fmt).date()
            return datetime.combine(d, default_time).replace(tzinfo=tz)
        except ValueError:
            continue
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", raw)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h < 24 and mi < 60:
            now = now_local(tz)
            d = now.replace(hour=h, minute=mi, second=0, microsecond=0)
            if prefer_future and d <= now:
                d += timedelta(days=1)
            return d
    raise ValueError(f"не понял дату/время «{s}» — нужен формат YYYY-MM-DD HH:MM")


# ── повторы ──────────────────────────────────────────────────────────────────
_FORBIDDEN_FREQ = re.compile(r"FREQ=SECONDLY", re.I)


def normalize_rrule(rule: str | None) -> str | None:
    """'RRULE:FREQ=DAILY' → 'FREQ=DAILY'; UNTIL с 'Z' → локальный (dtstart у нас наивный).
    Пусто → None. Бросает ValueError на заведомо плохое правило."""
    if not rule:
        return None
    r = rule.strip()
    if not r:
        return None
    r = re.sub(r"^RRULE:", "", r, flags=re.I).strip().upper()
    r = re.sub(r"UNTIL=(\d{8}T\d{6})Z", r"UNTIL=\1", r)
    r = re.sub(r"UNTIL=(\d{8})(?=;|$)", r"UNTIL=\1T235959", r)
    if "FREQ=" not in r:
        raise ValueError(f"в правиле повтора нет FREQ: «{rule}»")
    if _FORBIDDEN_FREQ.search(r):
        raise ValueError("повтор чаще раза в минуту запрещён")
    m = re.search(r"FREQ=MINUTELY", r)
    if m:
        iv = re.search(r"INTERVAL=(\d+)", r)
        if not iv or int(iv.group(1)) < 5:
            raise ValueError("поминутный повтор — не чаще раза в 5 минут (INTERVAL>=5)")
    # проверка, что dateutil его понимает
    rrulestr(r, dtstart=datetime(2026, 1, 1, 9, 0))
    return r


def next_occurrence(rule: str | None, local_start: datetime | str, tz: ZoneInfo,
                    after: datetime | None = None, *, inclusive: bool = False) -> datetime | None:
    """Следующее срабатывание (aware UTC) строго после `after` (UTC, по умолчанию — сейчас).

    Без правила: сам local_start, если он позже after (или равен при inclusive), иначе None.
    """
    if isinstance(local_start, str):
        local_start = datetime.fromisoformat(local_start)
    start_naive = local_start.replace(tzinfo=None) if local_start.tzinfo is None \
        else local_start.astimezone(tz).replace(tzinfo=None)
    after = after or now_utc()
    after_local_naive = after.astimezone(tz).replace(tzinfo=None, microsecond=0)
    if not rule:
        cand = localize(start_naive, tz)
        ok = cand >= after if inclusive else cand > after
        return cand.astimezone(UTC) if ok else None
    rr = rrulestr(normalize_rrule(rule), dtstart=start_naive)
    nxt = rr.after(after_local_naive, inc=inclusive)
    # страховка от DST: локальное наивное сравнение могло дать момент ≤ after
    while nxt is not None and localize(nxt, tz) <= after and not inclusive:
        nxt = rr.after(nxt, inc=False)
    return localize(nxt, tz).astimezone(UTC) if nxt else None


_RR_DAYS = {"MO": "пн", "TU": "вт", "WE": "ср", "TH": "чт", "FR": "пт", "SA": "сб", "SU": "вс"}


def describe_rrule(rule: str | None) -> str:
    """Короткое человеческое описание повтора."""
    if not rule:
        return "однократно"
    r = rule.upper()
    iv = re.search(r"INTERVAL=(\d+)", r)
    n = int(iv.group(1)) if iv else 1
    days = re.search(r"BYDAY=([A-Z,0-9+-]+)", r)
    dl = [d[-2:] for d in days.group(1).split(",")] if days else []
    if "FREQ=DAILY" in r:
        return "каждый день" if n == 1 else f"каждые {n} дн."
    if "FREQ=WEEKLY" in r:
        if set(dl) == {"MO", "TU", "WE", "TH", "FR"}:
            return "по будням"
        if set(dl) == {"SA", "SU"}:
            return "по выходным"
        base = "каждую неделю" if n == 1 else f"каждые {n} нед."
        return base + (f" ({', '.join(_RR_DAYS.get(d, d) for d in dl)})" if dl else "")
    if "FREQ=MONTHLY" in r:
        return "каждый месяц" if n == 1 else f"каждые {n} мес."
    if "FREQ=YEARLY" in r:
        return "каждый год"
    if "FREQ=HOURLY" in r:
        return "каждый час" if n == 1 else f"каждые {n} ч"
    if "FREQ=MINUTELY" in r:
        return f"каждые {n} мин"
    return rule
