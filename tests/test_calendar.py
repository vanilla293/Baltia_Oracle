"""Календарь: события, раскрытие повторов, напоминания о событиях, .ics, инструменты модели."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from oracle import timeutil
from oracle.tools import base as tb
from oracle.tools import calendar as cal
from oracle.tools import reminders as rem

MSK = ZoneInfo("Europe/Moscow")
UTC = timezone.utc
NOW = datetime(2026, 9, 28, 6, 0, tzinfo=UTC)          # = clock: пн 28.09.2026 09:00 МСК


async def call(ctx, name: str, **args) -> dict:
    return json.loads(await tb.dispatch(name, args, ctx))


async def linked(ctx, eid: int, status: str = "active") -> list[dict]:
    return await ctx.db.fetchall("SELECT * FROM reminders WHERE ref_type='event' AND ref_id=? AND status=? "
                                 "ORDER BY id", (eid, status))


def unfold(data: bytes) -> list[str]:
    """Строки .ics после развёртки (RFC 5545 §3.1)."""
    return data.decode("utf-8").replace("\r\n ", "").split("\r\n")


# ── add_event ───────────────────────────────────────────────────────────────
async def test_add_timed_event_with_default_reminder(ctx):
    ev = await cal.add_event(ctx, title="  Встреча с Петей ", start="2026-09-29 15:00",
                             location="Кофейня, Невский", notes="взять ноут")
    assert ev["title"] == "Встреча с Петей" and ev["status"] == "active" and ev["all_day"] == 0
    assert ev["local_start"] == "2026-09-29T15:00:00" and ev["tz"] == "Europe/Moscow"
    assert ev["starts_at"] == "2026-09-29T12:00:00+00:00" and ev["ends_at"] == "2026-09-29T13:00:00+00:00"
    assert ev["when"] == "вт 29.09 15:00–16:00" and ev["repeat"] == "однократно"
    assert ev["reminder"] == "вт 29.09 14:30" and ev["note"] is None
    rows = await linked(ctx, ev["id"])
    assert len(rows) == 1 and rows[0]["id"] == ev["reminder_id"]
    r = rows[0]
    assert r["kind"] == "event" and r["nag"] == 0 and r["rrule"] is None
    assert r["text"] == "Через 30 мин: Встреча с Петей (Кофейня, Невский)"
    assert r["next_at"] == "2026-09-29T11:30:00+00:00"
    assert await ctx.db.kv_get(f"event_remind:{ev['id']}") == 30


@pytest.mark.parametrize("rb,text,next_at", [
    (0, "Сейчас: Созвон", "2026-09-29T12:00:00+00:00"),
    (90, "Через 1 час 30 мин: Созвон — в 15:00", "2026-09-29T10:30:00+00:00"),
    (120, "Через 2 часа: Созвон — в 15:00", "2026-09-29T10:00:00+00:00"),
    (1440, "Через сутки: Созвон — в 15:00", "2026-09-28T12:00:00+00:00"),
    ("45", "Через 45 мин: Созвон", "2026-09-29T11:15:00+00:00"),
])
async def test_reminder_lead_texts(ctx, rb, text, next_at):
    ev = await cal.add_event(ctx, title="Созвон", start="2026-09-29 15:00", remind_before_min=rb)
    (r,) = await linked(ctx, ev["id"])
    assert r["text"] == text and r["next_at"] == next_at


@pytest.mark.parametrize("rb", [None, -1, "нет"])
async def test_no_reminder(ctx, rb):
    ev = await cal.add_event(ctx, title="x", start="2026-09-29 15:00", remind_before_min=rb)
    assert ev["reminder"] is None and ev["reminder_id"] is None and ev["note"] is None
    assert await linked(ctx, ev["id"]) == []


async def test_reminder_moment_passed_does_not_fail(ctx):
    ev = await cal.add_event(ctx, title="Скоро", start="2026-09-28 09:10")
    assert ev["id"] and ev["reminder"] is None
    assert "уже прошло" in ev["note"]
    assert await linked(ctx, ev["id"]) == []
    past = await cal.add_event(ctx, title="Было", start="2026-09-20 10:00")   # прошлое событие — можно
    assert past["id"] and past["reminder"] is None


async def test_end_variants(ctx):
    ev = await cal.add_event(ctx, title="a", start="2026-09-29 15:00", end="16:30", remind_before_min=None)
    assert ev["ends_at"] == "2026-09-29T13:30:00+00:00"
    ev = await cal.add_event(ctx, title="b", start="2026-09-29 23:00", end="01:00", remind_before_min=None)
    assert ev["ends_at"] == "2026-09-29T22:00:00+00:00" and ev["when"] == "вт 29.09 23:00 — ср 30.09 01:00"
    ev = await cal.add_event(ctx, title="c", start="2026-09-29 15:00", end="2026-10-01 12:00", remind_before_min=None)
    assert ev["ends_at"] == "2026-10-01T09:00:00+00:00"
    ev = await cal.add_event(ctx, title="d", start=datetime(2026, 9, 29, 12, 0, tzinfo=UTC),
                             end=datetime(2026, 9, 29, 12, 0, tzinfo=UTC), remind_before_min=None)
    assert ev["starts_at"] == ev["ends_at"] == "2026-09-29T12:00:00+00:00"
    with pytest.raises(ValueError, match="раньше начала"):
        await cal.add_event(ctx, title="e", start="2026-09-29 15:00", end="2026-09-29 14:00")
    with pytest.raises(ValueError, match="не понял время конца"):
        await cal.add_event(ctx, title="e", start="2026-09-29 15:00", end="25:00")


@pytest.mark.parametrize("kw,err", [
    ({"title": "  "}, "нет названия"),
    ({"title": "x" * 301}, "длиннее"),
    ({"start": "послезавтра"}, "не понял"),
    ({"repeat": "каждую среду"}, "FREQ"),
    ({"repeat": "FREQ=DAILY;COUNT=0"}, "ни одной даты"),
    ({"remind_before_min": "полчаса"}, "число минут"),
    ({"remind_before_min": "1e400"}, "число минут"),
    ({"start": "9999-12-31 23:30"}, "год должен быть"),
    ({"start": "9999-12-31", "all_day": True}, "год должен быть"),
    ({"end": "0001-01-01 10:00"}, "год должен быть"),
    ({"repeat": "FREQ=WEEKLY;BYMONTH=2;BYMONTHDAY=31"}, "ни одной даты"),
])
async def test_add_event_bad_input(ctx, kw, err):
    args = {"title": "ok", "start": "2026-09-29 15:00", **kw}
    with pytest.raises(ValueError, match=err):
        await cal.add_event(ctx, **args)
    assert await ctx.db.scalar("SELECT COUNT(*) FROM events") == 0


async def test_all_day_event(ctx):
    ev = await cal.add_event(ctx, title="Отпуск", start="2026-10-01", all_day=True)
    assert ev["all_day"] == 1 and ev["local_start"] == "2026-10-01T00:00:00"
    assert ev["starts_at"] == "2026-09-30T21:00:00+00:00" and ev["ends_at"] == "2026-10-01T21:00:00+00:00"
    assert ev["when"] == "чт 01.10, весь день"
    (r,) = await linked(ctx, ev["id"])
    assert r["text"] == "Сегодня: Отпуск" and r["local_start"] == "2026-10-01T09:00:00"
    # время у all_day игнорируется, многодневное — конец включительно
    ev = await cal.add_event(ctx, title="Поход", start="2026-10-01 15:00", end="2026-10-03", all_day="true",
                             location="Карелия", remind_before_min=1440)
    assert ev["starts_at"] == "2026-09-30T21:00:00+00:00" and ev["ends_at"] == "2026-10-03T21:00:00+00:00"
    assert ev["when"] == "чт 01.10 — сб 03.10, весь день"
    (r,) = await linked(ctx, ev["id"])
    assert r["text"] == "Завтра: Поход (Карелия)" and r["local_start"] == "2026-09-30T09:00:00"
    # за 3 дня в 09:00 = ровно сейчас — ставится
    ev = await cal.add_event(ctx, title="Сдача", start="2026-10-01", all_day=True, remind_before_min=3 * 1440 + 100)
    (r,) = await linked(ctx, ev["id"])
    assert r["text"] == "Через 3 дня: Сдача" and r["next_at"] == "2026-09-28T06:00:00+00:00"
    with pytest.raises(ValueError, match="раньше начала"):
        await cal.add_event(ctx, title="x", start="2026-10-05", end="2026-10-01", all_day=True)


async def test_all_day_today_after_nine_skips_reminder(ctx, clock):
    ev = await cal.add_event(ctx, title="Ровно в девять", start="2026-09-28", all_day=True)
    assert ev["reminder"] == "пн 28.09 09:00"                   # 09:00 и сейчас 09:00 — ещё ставится
    clock.advance(minutes=1)
    ev = await cal.add_event(ctx, title="Сегодняшнее", start="2026-09-28", all_day=True)
    assert ev["reminder"] is None and "уже прошло" in ev["note"]


# ── напоминания о повторяющихся событиях ───────────────────────────────────
async def test_recurring_event_reminder_same_rule(ctx):
    ev = await cal.add_event(ctx, title="Планёрка", start="2026-09-21 10:00", repeat="FREQ=DAILY")
    (r,) = await linked(ctx, ev["id"])
    assert r["rrule"] == "FREQ=DAILY" and r["local_start"] == "2026-09-21T09:30:00"
    assert r["next_at"] == "2026-09-28T06:30:00+00:00"          # сегодня 09:30


async def test_recurring_reminder_crossing_midnight_shifts_byday(ctx):
    ev = await cal.add_event(ctx, title="Ночной релиз", start="2026-10-05 00:15", repeat="FREQ=WEEKLY;BYDAY=MO")
    (r,) = await linked(ctx, ev["id"])
    assert r["rrule"] == "FREQ=WEEKLY;BYDAY=SU" and r["local_start"] == "2026-10-04T23:45:00"
    nxt = timeutil.from_iso(r["next_at"]).astimezone(MSK)
    assert (nxt.weekday(), nxt.hour, nxt.minute) == (6, 23, 45)


async def test_monthly_first_day_reminder_day_before(ctx):
    ev = await cal.add_event(ctx, title="Аренда", start="2026-10-01 10:00", repeat="FREQ=MONTHLY",
                             remind_before_min=1440)
    (r,) = await linked(ctx, ev["id"])
    assert r["rrule"] == "FREQ=MONTHLY;BYMONTHDAY=-1"
    n1 = timeutil.from_iso(r["next_at"])
    assert n1.astimezone(MSK).date().isoformat() == "2026-09-30"
    n2 = timeutil.next_occurrence(r["rrule"], r["local_start"], MSK, after=n1)
    n3 = timeutil.next_occurrence(r["rrule"], r["local_start"], MSK, after=n2)
    assert [d.astimezone(MSK).date().isoformat() for d in (n2, n3)] == ["2026-10-31", "2026-11-30"]


async def test_complex_rule_skips_reminder_with_note(ctx):
    ev = await cal.add_event(ctx, title="Клуб", start="2026-10-05 19:00", repeat="FREQ=MONTHLY;BYDAY=1MO",
                             remind_before_min=1440)
    assert ev["id"] and ev["reminder"] is None and "повтора" in ev["note"]


async def test_recurring_all_day_yearly(ctx):
    ev = await cal.add_event(ctx, title="Годовщина", start="2025-10-05", all_day=True, repeat="FREQ=YEARLY",
                             remind_before_min=2 * 1440)
    (r,) = await linked(ctx, ev["id"])
    assert r["text"] == "Через 2 дня: Годовщина" and r["rrule"] == "FREQ=YEARLY"
    assert timeutil.from_iso(r["next_at"]).astimezone(MSK) == datetime(2026, 10, 3, 9, 0, tzinfo=MSK)


def test_shift_rrule_unit():
    d = datetime(2026, 10, 5).date()            # понедельник
    assert cal.shift_rrule("FREQ=DAILY", 0, d, True) == "FREQ=DAILY"
    assert cal.shift_rrule("FREQ=DAILY;BYHOUR=10", 0, d, True) is None
    assert cal.shift_rrule("FREQ=DAILY;BYHOUR=10", 1, d, False) == "FREQ=DAILY;BYHOUR=10"
    assert cal.shift_rrule("FREQ=WEEKLY;BYDAY=MO,WE", 1, d, False) == "FREQ=WEEKLY;BYDAY=SU,TU"
    assert cal.shift_rrule("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE", 1, d, False) == \
        "FREQ=WEEKLY;INTERVAL=2;BYDAY=SU,TU;WKST=SU"
    assert cal.shift_rrule("FREQ=WEEKLY;BYDAY=MO", 2, d, False) == "FREQ=WEEKLY;BYDAY=SA"
    assert cal.shift_rrule("FREQ=MONTHLY;BYMONTHDAY=15", 1, d, False) == "FREQ=MONTHLY;BYMONTHDAY=14"
    assert cal.shift_rrule("FREQ=MONTHLY;BYMONTHDAY=1", 1, d, False) == "FREQ=MONTHLY;BYMONTHDAY=-1"
    assert cal.shift_rrule("FREQ=MONTHLY;BYMONTHDAY=-1", 1, d, False) == "FREQ=MONTHLY;BYMONTHDAY=-2"
    assert cal.shift_rrule("FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=8", 1, d, False) == "FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=7"
    assert cal.shift_rrule("FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=1", 1, d, False) is None
    assert cal.shift_rrule("FREQ=YEARLY", 1, datetime(2028, 3, 1).date(), False) == \
        "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=-1"
    assert cal.shift_rrule("FREQ=YEARLY", 1, d, False) == "FREQ=YEARLY"
    assert cal.shift_rrule("FREQ=YEARLY;BYMONTH=10", 7, d, False) is None
    assert cal.shift_rrule("FREQ=MONTHLY;BYDAY=1MO", 1, d, False) is None
    assert cal.shift_rrule("FREQ=MONTHLY;BYSETPOS=-1;BYDAY=FR", 1, d, False) is None


# ── events_between ──────────────────────────────────────────────────────────
async def test_events_between_mixed(ctx):
    a = await cal.add_event(ctx, title="Врач", start="2026-09-30 11:00", remind_before_min=None)
    await cal.add_event(ctx, title="Далеко", start="2026-11-30 11:00", remind_before_min=None)
    await cal.add_event(ctx, title="Вчера", start="2026-09-27", all_day=True, remind_before_min=None)
    today = await cal.add_event(ctx, title="Сегодня весь день", start="2026-09-28", all_day=True,
                                remind_before_min=None)
    ongoing = await cal.add_event(ctx, title="Конференция", start="2026-09-28 08:00", end="2026-09-28 18:00",
                                  remind_before_min=None)
    foot = await cal.add_event(ctx, title="Футбол", start="2026-09-01 19:00", repeat="FREQ=WEEKLY;BYDAY=TU",
                               remind_before_min=None)
    bday = await cal.add_event(ctx, title="Годовщина", start="2025-10-05", all_day=True, repeat="FREQ=YEARLY",
                               remind_before_min=None)
    gone = await cal.add_event(ctx, title="Отменённое", start="2026-09-29 12:00", remind_before_min=None)
    await cal.cancel_event(ctx.db, gone["id"])

    items = await cal.events_between(ctx.db, MSK, NOW, NOW + timedelta(days=14))
    got = [(i["title"], i["occurs_local"]) for i in items]
    assert got == [
        ("Сегодня весь день", "пн 28.09, весь день"),
        ("Конференция", "пн 28.09 08:00"),
        ("Футбол", "вт 29.09 19:00"),
        ("Врач", "ср 30.09 11:00"),
        ("Годовщина", "пн 05.10, весь день"),
        ("Футбол", "вт 06.10 19:00"),
    ]
    assert [i["occurs_at"] for i in items] == sorted(i["occurs_at"] for i in items)
    f = [i for i in items if i["id"] == foot["id"]]
    assert [i["occurs_at"] for i in f] == ["2026-09-29T16:00:00+00:00", "2026-10-06T16:00:00+00:00"]
    assert f[0]["occurs_end"] == "2026-09-29T17:00:00+00:00" and f[0]["rrule"] == "FREQ=WEEKLY;BYDAY=TU"
    by_id = {i["id"]: i for i in items}
    assert by_id[a["id"]]["occurs_at"] == a["starts_at"]
    assert by_id[today["id"]]["occurs_at"] == "2026-09-27T21:00:00+00:00"
    assert by_id[ongoing["id"]]["occurs_at"] == "2026-09-28T05:00:00+00:00"
    assert by_id[bday["id"]]["occurs_at"] == "2026-10-04T21:00:00+00:00"


async def test_events_between_boundaries(ctx):
    ev = await cal.add_event(ctx, title="x", start="2026-09-29 10:00", remind_before_min=None)   # 07:00Z–08:00Z
    s = datetime(2026, 9, 29, 7, 0, tzinfo=UTC)
    assert len(await cal.events_between(ctx.db, MSK, s, s + timedelta(minutes=1))) == 1
    assert await cal.events_between(ctx.db, MSK, s - timedelta(hours=1), s) == []            # конец исключён
    assert await cal.events_between(ctx.db, MSK, s + timedelta(hours=1), s + timedelta(hours=2)) == []
    # наивные границы — как UTC; пояс строкой
    assert len(await cal.events_between(ctx.db, "Europe/Moscow", datetime(2026, 9, 29), datetime(2026, 9, 30))) == 1
    zero = await cal.add_event(ctx, title="z", start="2026-09-30 10:00", end="2026-09-30 10:00",
                               remind_before_min=None)
    z = timeutil.from_iso(zero["starts_at"])
    assert [i["id"] for i in await cal.events_between(ctx.db, MSK, z, z + timedelta(hours=1))] == [zero["id"]]
    assert ev["id"] != zero["id"]


async def test_recurring_multi_day_overlap_and_cap(ctx):
    # еженедельное двухдневное (сб–вс): в окне пн–пт его нет, окно на выходные — есть
    await cal.add_event(ctx, title="Дача", start="2026-09-05", end="2026-09-06", all_day=True,
                        repeat="FREQ=WEEKLY", remind_before_min=None)
    wk = await cal.events_between(ctx.db, MSK, NOW, NOW + timedelta(days=4))
    assert wk == []
    sunday = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    (it,) = await cal.events_between(ctx.db, MSK, sunday, sunday + timedelta(hours=1))
    assert it["occurs_local"] == "сб 03.10, весь день" and it["occurs_end"] == "2026-10-04T21:00:00+00:00"
    await cal.add_event(ctx, title="Пинг", start="2026-09-28 09:00", repeat="FREQ=MINUTELY;INTERVAL=5",
                        remind_before_min=None)
    many = await cal.events_between(ctx.db, MSK, NOW, NOW + timedelta(days=90))
    assert len([i for i in many if i["title"] == "Пинг"]) == cal.MAX_EXPAND


@pytest.mark.parametrize("start,rule", [
    ("2024-01-01 00:02", "FREQ=MINUTELY;INTERVAL=7"),
    ("2023-03-15 10:20", "FREQ=HOURLY;INTERVAL=5;BYMINUTE=20"),
    ("2020-02-29 19:00", "FREQ=DAILY;INTERVAL=3"),
    ("2021-06-02 08:00", "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE,SU"),
    ("2021-06-02 08:00", "FREQ=WEEKLY;INTERVAL=3;BYDAY=TU;UNTIL=20261215T000000"),
    ("2019-01-15 12:00", "FREQ=MONTHLY"),
    ("2020-01-01 09:00", "FREQ=DAILY;COUNT=5000"),
])
async def test_fast_forward_matches_full_expansion(ctx, monkeypatch, start, rule):
    await cal.add_event(ctx, title="x", start=start, repeat=rule, remind_before_min=None)
    window = (datetime(2026, 10, 1, 3, 0, tzinfo=UTC), datetime(2026, 10, 22, 3, 0, tzinfo=UTC))
    fast = [i["occurs_at"] for i in await cal.events_between(ctx.db, MSK, *window)]
    monkeypatch.setattr(cal, "_fast_start", lambda r, st, lo: st)
    full = [i["occurs_at"] for i in await cal.events_between(ctx.db, MSK, *window)]
    assert fast == full and fast


async def test_old_minutely_event_is_fast(ctx):
    import time
    await cal.add_event(ctx, title="Пинг", start="2016-01-01 00:00", end="00:00", repeat="FREQ=MINUTELY;INTERVAL=5",
                        remind_before_min=None)
    t = time.monotonic()
    items = await cal.events_between(ctx.db, MSK, NOW, NOW + timedelta(hours=1))
    assert time.monotonic() - t < 1.0 and len(items) == 12


async def test_recurring_keeps_local_time_over_dst(ctx):
    ctx.cfg = replace(ctx.cfg, timezone="Europe/Berlin")
    ev = await cal.add_event(ctx, title="Бранч", start="2026-10-18 10:00", repeat="FREQ=WEEKLY",
                             remind_before_min=None)
    assert ev["tz"] == "Europe/Berlin"
    items = await cal.events_between(ctx.db, ctx.tz, datetime(2026, 10, 17, tzinfo=UTC),
                                     datetime(2026, 11, 2, tzinfo=UTC))
    assert [i["occurs_at"] for i in items] == ["2026-10-18T08:00:00+00:00", "2026-10-25T09:00:00+00:00",
                                               "2026-11-01T09:00:00+00:00"]


async def test_broken_rule_in_db_does_not_crash(ctx):
    ev = await cal.add_event(ctx, title="x", start="2026-09-29 10:00", remind_before_min=None)
    await ctx.db.execute("UPDATE events SET rrule='FREQ=NONSENSE' WHERE id=?", (ev["id"],))
    items = await cal.events_between(ctx.db, MSK, NOW, NOW + timedelta(days=3))
    assert [i["id"] for i in items] == [ev["id"]]


# ── отмена и изменение ──────────────────────────────────────────────────────
async def test_cancel_event_cancels_reminder(ctx):
    ev = await cal.add_event(ctx, title="Стрижка", start="2026-09-30 12:00")
    assert len(await linked(ctx, ev["id"])) == 1
    r = await call(ctx, "cancel_event", id=ev["id"])
    assert r == {"ok": True, "id": ev["id"], "title": "Стрижка", "reminders_cancelled": 1}
    assert await ctx.db.kv_get(cal.KV_REMIND.format(ev["id"])) is None     # kv события не копится
    assert await linked(ctx, ev["id"]) == [] and len(await linked(ctx, ev["id"], "cancelled")) == 1
    assert await cal.events_between(ctx.db, MSK, NOW, NOW + timedelta(days=7)) == []
    r = await call(ctx, "cancel_event", id=ev["id"])
    assert r["ok"] is False and "уже отменено" in r["error"]
    r = await call(ctx, "cancel_event", id=999)
    assert r["ok"] is False and "#999" in r["error"]
    assert await cal.cancel_event(ctx.db, 999) is None


async def test_update_event_moves_and_recreates_reminder(ctx):
    ev = await cal.add_event(ctx, title="Встреча", start="2026-09-29 15:00", end="2026-09-29 17:00",
                             remind_before_min=60)
    old_rid = ev["reminder_id"]
    r = await call(ctx, "update_event", id=ev["id"], start="2026-10-01 11:00", location="Офис")
    assert r["ok"] and r["when"] == "чт 01.10 11:00–13:00" and r["location"] == "Офис"
    assert r["reminder"] == "чт 01.10 10:00"
    act = await linked(ctx, ev["id"])
    assert len(act) == 1 and act[0]["id"] != old_rid and act[0]["text"] == "Через 1 час: Встреча (Офис) — в 11:00"
    assert (await rem.get_reminder(ctx.db, old_rid))["status"] == "cancelled"
    g = await cal.get_event(ctx.db, ev["id"])
    assert g["starts_at"] == "2026-10-01T08:00:00+00:00" and g["ends_at"] == "2026-10-01T10:00:00+00:00"


async def test_update_event_fields(ctx):
    ev = await cal.add_event(ctx, title="Йога", start="2026-09-29 19:00", repeat="FREQ=WEEKLY;BYDAY=TU",
                             notes="коврик")
    r = await call(ctx, "update_event", id=ev["id"], title="Йога в зале", end="21:00", notes="")
    assert r["ok"] and r["title"] == "Йога в зале" and r["repeat"] == "каждую неделю (вт)"
    g = await cal.get_event(ctx.db, ev["id"])
    assert g["notes"] == "" and g["ends_at"] == "2026-09-29T18:00:00+00:00" and g["local_start"] == ev["local_start"]
    (rm,) = await linked(ctx, ev["id"])
    assert rm["text"] == "Через 30 мин: Йога в зале" and rm["rrule"] == "FREQ=WEEKLY;BYDAY=TU"
    r = await call(ctx, "update_event", id=ev["id"], repeat="none", remind_before_min=-1)
    assert r["ok"] and r["repeat"] == "однократно" and r["reminder"] is None
    assert await linked(ctx, ev["id"]) == []
    assert await ctx.db.kv_get(f"event_remind:{ev['id']}", "missing") is None
    # remind_before_min не передан — остаётся «не напоминать»
    r = await call(ctx, "update_event", id=ev["id"], start="2026-09-30 19:00")
    assert r["ok"] and r["reminder"] is None and r["when"] == "ср 30.09 19:00–21:00"


async def test_update_event_to_all_day_and_errors(ctx):
    ev = await cal.add_event(ctx, title="Выезд", start="2026-10-02 10:00")
    r = await call(ctx, "update_event", id=ev["id"], all_day=True)
    assert r["ok"] and r["all_day"] is True and r["when"] == "пт 02.10, весь день"
    (rm,) = await linked(ctx, ev["id"])
    assert rm["text"] == "Сегодня: Выезд" and rm["local_start"] == "2026-10-02T09:00:00"
    r = await call(ctx, "update_event", id=ev["id"], start="2026-10-05")
    assert r["ok"] and r["when"] == "пн 05.10, весь день"
    r = await call(ctx, "update_event", id=ev["id"], end="2026-10-01")
    assert r["ok"] is False and "раньше начала" in r["error"]
    r = await call(ctx, "update_event", id=12345, title="x")
    assert r["ok"] is False and "#12345" in r["error"]
    await cal.cancel_event(ctx.db, ev["id"])
    r = await call(ctx, "update_event", id=ev["id"], title="x")
    assert r["ok"] is False and "отменено" in r["error"]


# ── iCalendar ───────────────────────────────────────────────────────────────
def test_ics_escape_and_fold():
    assert cal.ics_escape("a\\b;c,d\r\ne\nf\rg\x07") == "a\\\\b\\;c\\,d\\ne\\nf\\ng"
    short = "SUMMARY:коротко"
    assert cal.ics_fold(short) == short
    line = "DESCRIPTION:" + "Длинный русский текст с ёжиками, 😀 эмодзи и цифрами 1234567890. " * 5
    folded = cal.ics_fold(line)
    parts = folded.split("\r\n")
    assert len(parts) > 1
    for i, p in enumerate(parts):
        b = p.encode("utf-8")
        assert len(b) <= 75
        assert i == 0 or p.startswith(" ")
        b.decode("utf-8")                    # ни один многобайтовый символ не разрезан
    assert folded.replace("\r\n ", "") == line


async def test_build_ics(ctx):
    once = await cal.add_event(ctx, title="Встреча, важная; с \\ и\nпереносом", start="2026-09-29 15:00",
                               location="Кафе «Ёж», Невский 1", notes="Обсудить:\n1) бюджет; 2) сроки. " * 6,
                               remind_before_min=None)
    weekly = await cal.add_event(ctx, title="Футбол", start="2026-09-01 19:00",
                                 repeat="FREQ=WEEKLY;BYDAY=TU;UNTIL=20261231T235959", remind_before_min=None)
    allday = await cal.add_event(ctx, title="Отпуск", start="2026-10-01", end="2026-10-03", all_day=True,
                                 remind_before_min=None)
    yearly = await cal.add_event(ctx, title="Годовщина", start="2025-10-05", all_day=True,
                                 repeat="FREQ=YEARLY;UNTIL=20301005T000000", remind_before_min=None)
    items = await cal.events_between(ctx.db, MSK, NOW, NOW + timedelta(days=60))
    data = cal.build_ics(items, "Europe/Moscow")
    assert isinstance(data, bytes)
    text = data.decode("utf-8")
    assert text.endswith("END:VCALENDAR\r\n")
    assert "\n" not in text.replace("\r\n", "")                 # только CRLF
    for raw in text.split("\r\n"):
        assert len(raw.encode("utf-8")) <= 75
    lines = unfold(data)
    assert lines[:4] == ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Baltia Oracle//Calendar 1.0//RU",
                         "CALSCALE:GREGORIAN"]
    assert "X-WR-TIMEZONE:Europe/Moscow" in lines
    assert lines.count("BEGIN:VEVENT") == lines.count("END:VEVENT") == 4        # вхождения склеены
    for ev in (once, weekly, allday, yearly):
        assert lines.count(f"UID:{ev['id']}@baltia-oracle") == 1
    assert lines.count("DTSTAMP:20260928T060000Z") == 4

    def block(eid: int) -> list[str]:
        i = lines.index(f"UID:{eid}@baltia-oracle")
        j = lines.index("END:VEVENT", i)
        return lines[i:j]

    b = block(once["id"])
    assert "SUMMARY:Встреча\\, важная\\; с \\\\ и переносом" in b      # название — в одну строку
    assert "LOCATION:Кафе «Ёж»\\, Невский 1" in b
    assert "DTSTART:20260929T120000Z" in b and "DTEND:20260929T130000Z" in b
    desc = next(x for x in b if x.startswith("DESCRIPTION:"))
    assert desc.startswith("DESCRIPTION:Обсудить:\\n1) бюджет\\; 2) сроки. ")
    assert not any(x.startswith("RRULE") for x in b)

    b = block(weekly["id"])
    assert "DTSTART;TZID=Europe/Moscow:20260901T190000" in b
    assert "DTEND;TZID=Europe/Moscow:20260901T200000" in b
    assert "RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261231T205959Z" in b

    b = block(allday["id"])
    assert "DTSTART;VALUE=DATE:20261001" in b and "DTEND;VALUE=DATE:20261004" in b

    b = block(yearly["id"])
    assert "DTSTART;VALUE=DATE:20251005" in b and "DTEND;VALUE=DATE:20251006" in b
    assert "RRULE:FREQ=YEARLY;UNTIL=20301005" in b

    # VTIMEZONE для TZID есть и описан до событий
    tzi = lines.index("BEGIN:VTIMEZONE")
    assert lines[tzi + 1] == "TZID:Europe/Moscow" and tzi < lines.index("BEGIN:VEVENT")
    assert "TZOFFSETTO:+0300" in lines[tzi:lines.index("END:VTIMEZONE")]


def test_build_ics_vtimezone_with_dst(clock):
    ev = {"id": 1, "title": "Бранч", "local_start": "2026-10-18T10:00:00", "tz": "Europe/Berlin",
          "starts_at": "2026-10-18T08:00:00+00:00", "ends_at": "2026-10-18T09:00:00+00:00", "all_day": 0,
          "location": "", "notes": "", "rrule": "FREQ=WEEKLY", "status": "active"}
    lines = unfold(cal.build_ics([ev], "Europe/Berlin"))
    tz = lines[lines.index("BEGIN:VTIMEZONE"):lines.index("END:VTIMEZONE")]
    assert "TZID:Europe/Berlin" in tz
    i = tz.index("DTSTART:20261025T030000")                 # CEST → CET: 03:00 по летнему
    assert tz[i - 1] == "BEGIN:STANDARD" and tz[i + 1:i + 4] == ["TZOFFSETFROM:+0200", "TZOFFSETTO:+0100",
                                                                  "TZNAME:CET"]
    i = tz.index("DTSTART:20270328T020000")                 # CET → CEST
    assert tz[i - 1] == "BEGIN:DAYLIGHT" and tz[i + 1:i + 3] == ["TZOFFSETFROM:+0100", "TZOFFSETTO:+0200"]
    assert "DTSTART;TZID=Europe/Berlin:20261018T100000" in lines


def test_build_ics_empty_and_junk(clock):
    lines = unfold(cal.build_ics([], "Europe/Moscow"))
    assert lines[0] == "BEGIN:VCALENDAR" and lines[-2] == "END:VCALENDAR" and lines[-1] == ""
    assert "BEGIN:VEVENT" not in lines and "BEGIN:VTIMEZONE" not in lines
    # битая строка пропускается, а не роняет выгрузку
    lines = unfold(cal.build_ics([{"id": 5, "title": "x"}], "Europe/Moscow"))
    assert "BEGIN:VEVENT" not in lines


# ── инструменты ─────────────────────────────────────────────────────────────
async def test_tool_add_event(ctx):
    r = await call(ctx, "add_event", title="Стоматолог", start="2026-09-30 11:00", location="Клиника")
    assert r["ok"] and r["title"] == "Стоматолог" and r["when"] == "ср 30.09 11:00–12:00"
    assert r["all_day"] is False and r["location"] == "Клиника" and r["repeat"] == "однократно"
    assert r["reminder"] == "ср 30.09 10:30" and isinstance(r["reminder_id"], int) and "note" not in r
    # дата без времени и без all_day → на весь день; null в remind_before_min → по умолчанию
    r = await call(ctx, "add_event", title="ДР мамы", start="2026-10-05", repeat="FREQ=YEARLY",
                   remind_before_min=None)
    assert r["ok"] and r["all_day"] is True and r["when"] == "пн 05.10, весь день" and r["repeat"] == "каждый год"
    assert r["reminder"] == "пн 05.10 09:00"
    r = await call(ctx, "add_event", title="Скоро", start="2026-09-28 09:05")
    assert r["ok"] and r["reminder"] is None and "уже прошло" in r["note"]
    r = await call(ctx, "add_event", title="Плохо", start="2026-09-30 11:00", end="2026-09-30 10:00")
    assert r["ok"] is False and "раньше начала" in r["error"]
    r = await call(ctx, "add_event", title="Без начала")
    assert r["ok"] is False and "start" in r["error"]


async def test_tool_list_events(ctx):
    await cal.add_event(ctx, title="Сегодня рано", start="2026-09-28 08:00", remind_before_min=None)
    await cal.add_event(ctx, title="Завтра", start="2026-09-29 12:00", end="13:30", location="Офис",
                        notes="n" * 500, remind_before_min=None)
    await cal.add_event(ctx, title="Через 10 дней", start="2026-10-08 12:00", remind_before_min=None)
    await cal.add_event(ctx, title="Каждый день", start="2026-09-01 20:00", repeat="FREQ=DAILY",
                        remind_before_min=None)
    r = await call(ctx, "list_events")
    titles = [i["title"] for i in r["items"]]
    assert r["ok"] and "Сегодня рано" not in titles and "Через 10 дней" not in titles
    assert titles.count("Каждый день") == 7 and r["count"] == 8
    tmr = next(i for i in r["items"] if i["title"] == "Завтра")
    assert tmr == {"id": tmr["id"], "title": "Завтра", "when": "вт 29.09 12:00–13:30", "all_day": False,
                   "location": "Офис", "repeat": "однократно", "notes": "n" * 300}
    r = await call(ctx, "list_events", **{"from": "2026-09-28", "to": "2026-09-28"})
    assert [i["title"] for i in r["items"]] == ["Сегодня рано", "Каждый день"]
    r = await call(ctx, "list_events", **{"from": "2026-10-08"})
    assert r["items"][0]["title"] == "Через 10 дней" and r["count"] == 8
    r = await call(ctx, "list_events", to="2026-09-29 12:00")
    assert [i["title"] for i in r["items"]] == ["Каждый день"]
    r = await call(ctx, "list_events", **{"from": "2026-10-02", "to": "2026-10-01"})
    assert r["ok"] is False and "раньше начала" in r["error"]
    r = await call(ctx, "list_events", **{"from": "2026-01-01", "to": "2027-12-31"})
    assert r["ok"] is False and "больше года" in r["error"]
    r = await call(ctx, "list_events", **{"from": "вчера"})
    assert r["ok"] is False and "не понял" in r["error"]
    r = await call(ctx, "list_events", **{"from": "9999-12-25"})
    assert r["ok"] is False and "год должен быть" in r["error"]


async def test_tool_list_events_truncates(ctx):
    await cal.add_event(ctx, title="Пинг", start="2026-09-28 10:00", repeat="FREQ=HOURLY", remind_before_min=None)
    r = await call(ctx, "list_events")
    assert r["count"] > 60 and len(r["items"]) == 60 and "первые 60" in r["note"]


async def test_tool_export_calendar(ctx):
    r = await call(ctx, "export_calendar")
    assert r["ok"] and r["events"] == 0 and ctx.outbox == []
    await cal.add_event(ctx, title="Утром было", start="2026-09-28 07:00", remind_before_min=None)
    await cal.add_event(ctx, title="Футбол", start="2026-09-01 19:00", repeat="FREQ=WEEKLY;BYDAY=TU")
    await cal.add_event(ctx, title="Через полгода", start="2027-03-01 10:00")
    r = await call(ctx, "export_calendar", days_ahead=30)
    assert r == {"ok": True, "events": 2, "note": "файл отправлен владельцу"}
    (out,) = ctx.outbox
    assert isinstance(out, tb.OutItem) and out.kind == "file" and out.filename == "calendar.ics"
    assert out.text == "Календарь: 2 события — открой файл, и он добавится в твой календарь"
    lines = unfold(out.data)
    assert lines.count("BEGIN:VEVENT") == 2 and "SUMMARY:Футбол" in lines and "SUMMARY:Утром было" in lines
    ctx.outbox.clear()
    r = await call(ctx, "export_calendar", days_ahead="мусор")
    assert r["ok"] and r["events"] == 2
    r = await call(ctx, "export_calendar", days_ahead=400)
    assert r["events"] == 3 and ctx.outbox[-1].text.startswith("Календарь: 3 события")
    r = await call(ctx, "export_calendar", days_ahead=10 ** 12)
    assert r["ok"] and r["events"] == 3
    r = await call(ctx, "export_calendar", days_ahead="1e400")
    assert r["ok"] and r["events"] == 2


def test_calendar_tool_schemas(cfg):
    names = {s["function"]["name"]: s["function"] for s in tb.schemas(cfg)}
    for n in ("add_event", "list_events", "update_event", "cancel_event", "export_calendar"):
        assert n in names
    assert names["add_event"]["parameters"]["required"] == ["title", "start"]
    assert set(names["list_events"]["parameters"]["properties"]) == {"from", "to"}
    assert "YYYY-MM-DD HH:MM" in names["add_event"]["description"]


def test_plural_and_human_minutes():
    assert [cal.plural(n, "событие", "события", "событий") for n in (1, 2, 5, 11, 21, 22, 112)] == \
        ["событие", "события", "событий", "событий", "событие", "события", "событий"]
    assert cal.human_minutes(5) == "5 мин" and cal.human_minutes(60) == "1 час"
    assert cal.human_minutes(125) == "2 часа 5 мин" and cal.human_minutes(300) == "5 часов"
    assert cal.human_minutes(1440) == "сутки" and cal.human_minutes(1500) == "сутки 1 ч"
    assert cal.human_minutes(2880) == "2 дня" and cal.human_minutes(7200) == "5 дней"
