"""Напоминания: создание, повторы, будильник, отметка/отложить/отмена, инструменты модели."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from oracle import timeutil
from oracle.tools import base as tb
from oracle.tools import reminders as rem
from dataclasses import replace as with_cfg

MSK = ZoneInfo("Europe/Moscow")
UTC = timezone.utc


async def call(ctx, name: str, **args) -> dict:
    return json.loads(await tb.dispatch(name, args, ctx))


# ── create_reminder ─────────────────────────────────────────────────────────
async def test_once_reminder(ctx):
    r = await rem.create_reminder(ctx, text="  Позвонить маме  ", when="2026-09-28 10:00")
    assert r["text"] == "Позвонить маме"
    assert r["next_at"] == "2026-09-28T07:00:00+00:00"
    assert r["local_start"] == "2026-09-28T10:00:00" and r["tz"] == "Europe/Moscow"
    assert r["rrule"] is None and r["status"] == "active" and r["kind"] == "reminder"
    assert r["nag"] == 0 and r["challenge"] == 0 and r["nag_active"] == 0
    assert r["nag_interval_min"] == 3 and r["nag_max"] == 20
    assert r["when_local"] == "пн 28.09 10:00" and r["repeat"] == "однократно"


async def test_now_exactly_is_allowed(ctx):
    r = await rem.create_reminder(ctx, text="сейчас", when="2026-09-28 09:00")
    assert r["next_at"] == "2026-09-28T06:00:00+00:00"


async def test_daily_reminder_already_passed_today(ctx):
    r = await rem.create_reminder(ctx, text="Таблетки", when="2026-09-28 08:00", rrule="rrule:freq=daily")
    assert r["rrule"] == "FREQ=DAILY"
    assert r["local_start"] == "2026-09-28T08:00:00"
    assert r["next_at"] == "2026-09-29T05:00:00+00:00"
    assert r["repeat"] == "каждый день"


async def test_weekday_reminder(ctx):
    # суббота 26.09 07:30, по будням → понедельник 28.09 07:30 уже прошёл → вторник 29.09
    r = await rem.create_reminder(ctx, text="Спортзал", when="2026-09-26 07:30",
                                  rrule="FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR")
    nxt = timeutil.from_iso(r["next_at"]).astimezone(MSK)
    assert (nxt.date().isoformat(), nxt.hour, nxt.minute) == ("2026-09-29", 7, 30)
    assert r["repeat"] == "по будням"


async def test_weekly_specific_days(ctx):
    r = await rem.create_reminder(ctx, text="Бассейн", when="2026-09-28 20:00", rrule="FREQ=WEEKLY;BYDAY=WE,FR")
    nxt = timeutil.from_iso(r["next_at"]).astimezone(MSK)
    assert nxt.date().isoformat() == "2026-09-30" and nxt.hour == 20


async def test_past_once_is_error(ctx):
    with pytest.raises(ValueError, match="это время уже прошло: пн 28.09 08:00"):
        await rem.create_reminder(ctx, text="x", when="2026-09-28 08:00")
    assert await ctx.db.scalar("SELECT COUNT(*) FROM reminders") == 0


async def test_rrule_without_future_is_error(ctx):
    with pytest.raises(ValueError, match="нет будущих срабатываний"):
        await rem.create_reminder(ctx, text="x", when="2026-08-01 07:00", rrule="FREQ=DAILY;UNTIL=20260901T000000Z")


async def test_rrule_with_count_in_past(ctx):
    with pytest.raises(ValueError, match="нет будущих"):
        await rem.create_reminder(ctx, text="x", when="2026-09-01 07:00", rrule="FREQ=DAILY;COUNT=3")


@pytest.mark.parametrize("kw,err", [
    ({"text": "   "}, "пустой"),
    ({"text": "x" * 1001}, "1000"),
    ({"kind": "alarm"}, "неизвестный вид"),
    ({"when": "завтра утром"}, "не понял"),
    ({"when": ""}, "пуст"),
    ({"when": 12345}, "не понял"),
    ({"rrule": "BYDAY=MO"}, "FREQ"),
    ({"rrule": "FREQ=SECONDLY"}, "чаще"),
    ({"rrule": "FREQ=DAILY;FOO=1"}, "не понял правило"),
    ({"rrule": "FREQ=DAILY;BYMONTH=13"}, "ни одной даты"),
    ({"rrule": "FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30"}, "ни одной даты"),
    ({"rrule": "FREQ=MONTHLY;BYDAY=6MO"}, "ни одной даты"),
    ({"rrule": "FREQ=HOURLY;BYMINUTE=60"}, "ни одной даты"),
    ({"rrule": "FREQ=DAILY;BYHOUR=9,x"}, "не понял правило"),
    ({"when": "9999-12-31 23:59"}, "год должен быть"),
    ({"when": "0001-01-01 10:00"}, "год должен быть"),
    ({"when": datetime(9999, 12, 31, 23, 0)}, "год должен быть"),
])
async def test_bad_input(ctx, kw, err):
    args = {"text": "ok", "when": "2026-09-29 10:00", **kw}
    with pytest.raises(ValueError, match=err):
        await rem.create_reminder(ctx, **args)


def test_impossible_rules_rejected_fast():
    import time
    t = time.monotonic()
    for r in ("FREQ=DAILY;BYMONTH=13", "FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30", "FREQ=MINUTELY;INTERVAL=5;BYMONTH=4;BYMONTHDAY=31",
              "FREQ=YEARLY;BYYEARDAY=366;BYMONTH=1"):
        with pytest.raises(ValueError, match="ни одной даты"):
            rem.clean_repeat(r)
    assert time.monotonic() - t < 2.0          # без проверки — секунды на каждое (dateutil перебирает до 9999 г.)
    for ok in ("FREQ=MONTHLY;BYDAY=5MO", "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29", "FREQ=MONTHLY;BYSETPOS=-1;BYDAY=FR",
               "FREQ=YEARLY;BYWEEKNO=53;BYDAY=MO", "FREQ=MINUTELY;INTERVAL=5;BYHOUR=9", "FREQ=MONTHLY;BYMONTHDAY=-1"):
        assert rem.clean_repeat(ok) == ok


async def test_text_exactly_1000_ok(ctx):
    r = await rem.create_reminder(ctx, text="я" * 1000, when="2026-09-29 10:00")
    assert len(r["text"]) == 1000


async def test_when_as_aware_datetime(ctx):
    r = await rem.create_reminder(ctx, text="x", when=datetime(2026, 9, 29, 4, 30, tzinfo=UTC))
    assert r["local_start"] == "2026-09-29T07:30:00" and r["next_at"] == "2026-09-29T04:30:00+00:00"


async def test_when_as_naive_datetime_is_local(ctx):
    r = await rem.create_reminder(ctx, text="x", when=datetime(2026, 9, 29, 7, 30))
    assert r["next_at"] == "2026-09-29T04:30:00+00:00"


async def test_when_time_only_goes_to_future(ctx):
    r = await rem.create_reminder(ctx, text="x", when="08:15")      # 08:15 уже было → завтра
    assert r["local_start"] == "2026-09-29T08:15:00"


async def test_wake_defaults(ctx):
    r = await rem.create_reminder(ctx, text="Подъём", when="2026-09-29 07:00", kind="wake", rrule="FREQ=DAILY")
    assert r["kind"] == "wake" and r["nag"] == 1 and r["challenge"] == 1
    assert r["nag_interval_min"] == ctx.cfg.nag_interval_min and r["nag_max"] == ctx.cfg.nag_max


async def test_wake_respects_config_and_overrides(ctx):
    ctx.cfg = with_cfg(ctx.cfg, wake_challenge=False, nag_interval_min=5, nag_max=7)
    r = await rem.create_reminder(ctx, text="Подъём", when="2026-09-29 07:00", kind="WAKE")
    assert r["nag"] == 1 and r["challenge"] == 0 and r["nag_interval_min"] == 5 and r["nag_max"] == 7
    r = await rem.create_reminder(ctx, text="Подъём", when="2026-09-29 07:00", kind="wake",
                                  nag=False, challenge=True, nag_interval_min=0)
    assert r["nag"] == 0 and r["challenge"] == 1 and r["nag_interval_min"] == 1


async def test_nag_for_plain_reminder_and_refs(ctx):
    r = await rem.create_reminder(ctx, text="Сдать отчёт", when="2026-09-29 10:00", nag="true",
                                  nag_interval_min="10", kind="task", ref_type="task", ref_id=7)
    assert r["nag"] == 1 and r["challenge"] == 0 and r["nag_interval_min"] == 10
    assert r["ref_type"] == "task" and r["ref_id"] == 7


# ── list / ack / snooze / cancel ────────────────────────────────────────────
async def test_list_active_order_and_nagging(ctx):
    a = await rem.create_reminder(ctx, text="позже", when="2026-09-30 10:00")
    b = await rem.create_reminder(ctx, text="раньше", when="2026-09-28 12:00")
    c = await rem.create_reminder(ctx, text="долбит", when="2026-09-28 09:30", nag=True)
    d = await rem.create_reminder(ctx, text="отменено", when="2026-09-28 11:00")
    # как после срабатывания разового с долбёжкой: next_at=NULL, nag_active=1
    await ctx.db.execute("UPDATE reminders SET next_at=NULL, nag_active=1 WHERE id=?", (c["id"],))
    await rem.cancel_reminder(ctx.db, d["id"])
    rows = await rem.list_active(ctx.db)
    assert [r["id"] for r in rows] == [b["id"], a["id"], c["id"]]
    assert len(await rem.list_active(ctx.db, limit=1)) == 1


async def test_ack_once_nagging_becomes_done(ctx):
    r = await rem.create_reminder(ctx, text="x", when="2026-09-28 09:30", nag=True)
    await ctx.db.execute("UPDATE reminders SET next_at=NULL, nag_active=1, nag_count=4, "
                         "nag_next_at='2026-09-28T06:33:00+00:00', snooze_at='2026-09-28T07:00:00+00:00' "
                         "WHERE id=?", (r["id"],))
    a = await rem.ack_reminder(ctx.db, r["id"])
    assert a["status"] == "done" and a["nag_active"] == 0 and a["nag_next_at"] is None
    assert a["snooze_at"] is None and a["nag_count"] == 0


async def test_ack_recurring_stays_active(ctx):
    r = await rem.create_reminder(ctx, text="x", when="2026-09-29 07:00", rrule="FREQ=DAILY", kind="wake")
    await ctx.db.execute("UPDATE reminders SET nag_active=1 WHERE id=?", (r["id"],))
    a = await rem.ack_reminder(ctx.db, r["id"])
    assert a["status"] == "active" and a["nag_active"] == 0 and a["next_at"] == r["next_at"]


async def test_ack_missing_and_cancelled(ctx):
    assert await rem.ack_reminder(ctx.db, 999) is None
    assert await rem.ack_reminder(ctx.db, "мусор") is None
    r = await rem.create_reminder(ctx, text="x", when="2026-09-29 07:00")
    await ctx.db.execute("UPDATE reminders SET next_at=NULL WHERE id=?", (r["id"],))
    await rem.cancel_reminder(ctx.db, r["id"])
    assert (await rem.ack_reminder(ctx.db, r["id"]))["status"] == "cancelled"


async def test_snooze(ctx, clock):
    r = await rem.create_reminder(ctx, text="x", when="2026-09-28 09:05", nag=True)
    await ctx.db.execute("UPDATE reminders SET next_at=NULL, nag_active=1, status='done' WHERE id=?", (r["id"],))
    s = await rem.snooze_reminder(ctx.db, r["id"], 10)
    assert s["status"] == "active" and s["nag_active"] == 0 and s["nag_next_at"] is None
    assert s["snooze_at"] == "2026-09-28T06:10:00+00:00"
    assert (await rem.snooze_reminder(ctx.db, r["id"], 0))["snooze_at"] == "2026-09-28T06:01:00+00:00"
    assert (await rem.snooze_reminder(ctx.db, r["id"], 99999))["snooze_at"] == "2026-09-29T06:00:00+00:00"
    assert (await rem.snooze_reminder(ctx.db, r["id"], "abc"))["snooze_at"] == "2026-09-28T06:10:00+00:00"
    assert await rem.snooze_reminder(ctx.db, 12345, 10) is None
    await rem.cancel_reminder(ctx.db, r["id"])                  # отменённое «отложить» не оживляет
    assert await rem.snooze_reminder(ctx.db, r["id"], 10) is None
    assert (await rem.get_reminder(ctx.db, r["id"]))["status"] == "cancelled"


async def test_cancel_and_cancel_by_ref(ctx):
    r = await rem.create_reminder(ctx, text="x", when="2026-09-29 07:00", nag=True)
    await ctx.db.execute("UPDATE reminders SET nag_active=1 WHERE id=?", (r["id"],))
    assert await rem.cancel_reminder(ctx.db, r["id"]) is True
    g = await rem.get_reminder(ctx.db, r["id"])
    assert g["status"] == "cancelled" and g["nag_active"] == 0
    assert await rem.cancel_reminder(ctx.db, r["id"]) is False
    assert await rem.cancel_reminder(ctx.db, 999) is False
    assert await rem.cancel_reminder(ctx.db, None) is False

    for _ in range(2):
        await rem.create_reminder(ctx, text="e", when="2026-09-29 07:00", kind="event", ref_type="event", ref_id=5)
    other = await rem.create_reminder(ctx, text="o", when="2026-09-29 07:00", kind="event",
                                      ref_type="event", ref_id=6)
    assert await rem.cancel_by_ref(ctx.db, "event", 5) == 2
    assert await rem.cancel_by_ref(ctx.db, "event", 5) == 0
    assert (await rem.get_reminder(ctx.db, other["id"]))["status"] == "active"


# ── render ──────────────────────────────────────────────────────────────────
async def test_render_reminder(ctx):
    r = await rem.create_reminder(ctx, text="Подъём", when="2026-09-29 07:30", rrule="FREQ=DAILY")
    assert rem.render_reminder(r, MSK) == f"#{r['id']} · вт 29.09 07:30 · каждый день · Подъём"
    w = await rem.create_reminder(ctx, text="Вставай", when="2026-09-29 07:00", kind="wake")
    s = rem.render_reminder(w, "Europe/Moscow")
    assert s.startswith(f"#{w['id']} · вт 29.09 07:00 · однократно · ⏰ Вставай")
    assert "долбит до отметки" in s and "с задачкой" in s and "долбит сейчас" not in s
    w2 = dict(w, next_at=None, nag_active=1)
    s2 = rem.render_reminder(w2, MSK)
    assert " · сейчас · " in s2 and s2.endswith("(долбит сейчас)")


def test_render_reminder_edge_rows(clock):
    row = {"id": 3, "text": "многострочный\n  текст " + "я" * 300, "kind": "reminder", "next_at": None,
           "rrule": None, "nag": 0, "challenge": 0, "nag_active": 0, "status": "done",
           "snooze_at": None, "tz": "Europe/Moscow"}
    s = rem.render_reminder(row, MSK)
    assert s.startswith("#3 · — · однократно · многострочный текст ") and "\n" not in s
    assert "…" in s and s.endswith("выполнено")
    snoozed = dict(row, text="x", status="active", snooze_at="2026-09-28T06:10:00+00:00")
    assert "отложено до пн 28.09 09:10" in rem.render_reminder(snoozed, MSK)
    assert rem.render_reminder(dict(row, text="x", status="active"), "Нет/Такого").startswith("#3 · —")


# ── инструменты ─────────────────────────────────────────────────────────────
async def test_tool_create_and_list(ctx):
    r = await call(ctx, "create_reminder", text="Вынести мусор", when="2026-09-28 20:00")
    assert r == {"ok": True, "id": r["id"], "text": "Вынести мусор", "when": "пн 28.09 20:00",
                 "repeat": "однократно", "kind": "reminder", "nag": False}
    w = await call(ctx, "create_reminder", text="Подъём", when="2026-09-29 07:00", kind="wake",
                   repeat="FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR")
    assert w["ok"] and w["kind"] == "wake" and w["nag"] is True and w["challenge"] is True
    assert w["repeat"] == "по будням" and w["when"] == "вт 29.09 07:00"
    n = await call(ctx, "create_reminder", text="Важно", when="2026-09-29 12:00", nag=True, kind="event")
    assert n["kind"] == "reminder" and n["nag"] is True          # в инструменте только reminder|wake
    lst = await call(ctx, "list_reminders")
    assert lst["ok"] and [i["text"] for i in lst["items"]] == ["Вынести мусор", "Подъём", "Важно"]
    assert set(lst["items"][0]) >= {"id", "text", "when", "repeat", "kind", "nag", "nagging"}
    assert lst["items"][1]["nagging"] is False


async def test_tool_create_errors(ctx):
    r = await call(ctx, "create_reminder", text="x", when="2026-09-27 10:00")
    assert r["ok"] is False and "уже прошло" in r["error"]
    r = await call(ctx, "create_reminder", text="x")
    assert r["ok"] is False and "when" in r["error"]
    r = await call(ctx, "create_reminder", text="x", when="2026-09-29 10:00", repeat="каждый день")
    assert r["ok"] is False and "FREQ" in r["error"]


async def test_tool_create_repeat_none(ctx):
    r = await call(ctx, "create_reminder", text="x", when="2026-09-29 10:00", repeat="none")
    assert r["ok"] and r["repeat"] == "однократно"


async def test_tool_update_text_only_keeps_schedule(ctx):
    r = await rem.create_reminder(ctx, text="x", when="2026-09-28 09:30", nag=True)
    await ctx.db.execute("UPDATE reminders SET next_at=NULL, nag_active=1 WHERE id=?", (r["id"],))
    u = await call(ctx, "update_reminder", id=r["id"], text="новый текст")
    assert u["ok"] and u["text"] == "новый текст"
    g = await rem.get_reminder(ctx.db, r["id"])
    assert g["nag_active"] == 1 and g["next_at"] is None     # расписание не трогали


async def test_tool_update_when_and_repeat(ctx):
    r = await rem.create_reminder(ctx, text="x", when="2026-09-28 10:00", nag=True)
    await ctx.db.execute("UPDATE reminders SET nag_active=1, nag_count=3, snooze_at='2026-09-28T06:30:00+00:00' "
                         "WHERE id=?", (r["id"],))
    u = await call(ctx, "update_reminder", id=str(r["id"]), when="2026-09-30 18:00", repeat="FREQ=DAILY")
    assert u["ok"] and u["when"] == "ср 30.09 18:00" and u["repeat"] == "каждый день"
    g = await rem.get_reminder(ctx.db, r["id"])
    assert g["local_start"] == "2026-09-30T18:00:00" and g["rrule"] == "FREQ=DAILY"
    assert g["next_at"] == "2026-09-30T15:00:00+00:00"
    assert g["nag_active"] == 0 and g["nag_count"] == 0 and g["snooze_at"] is None and g["nag"] == 1


async def test_tool_update_remove_repeat_uses_next(ctx, clock):
    r = await rem.create_reminder(ctx, text="x", when="2026-09-20 08:00", rrule="FREQ=DAILY")
    assert r["next_at"] == "2026-09-29T05:00:00+00:00"
    u = await call(ctx, "update_reminder", id=r["id"], repeat="")
    assert u["ok"] and u["repeat"] == "однократно" and u["when"] == "вт 29.09 08:00"
    g = await rem.get_reminder(ctx.db, r["id"])
    assert g["rrule"] is None and g["local_start"] == "2026-09-29T08:00:00"
    # «none» — тоже без повтора; повтор добавляем обратно и снова убираем
    await call(ctx, "update_reminder", id=r["id"], repeat="FREQ=WEEKLY")
    u = await call(ctx, "update_reminder", id=r["id"], repeat="none")
    assert u["ok"] and u["repeat"] == "однократно"


async def test_tool_update_repeat_only_recomputes(ctx):
    r = await rem.create_reminder(ctx, text="x", when="2026-09-28 10:00")
    u = await call(ctx, "update_reminder", id=r["id"], repeat="FREQ=WEEKLY;BYDAY=TU")
    assert u["ok"] and u["when"] == "вт 29.09 10:00"


async def test_tool_update_errors(ctx):
    r = await call(ctx, "update_reminder", id=404, text="x")
    assert r["ok"] is False and "#404" in r["error"]
    x = await rem.create_reminder(ctx, text="x", when="2026-09-29 10:00")
    await rem.cancel_reminder(ctx.db, x["id"])
    r = await call(ctx, "update_reminder", id=x["id"], text="y")
    assert r["ok"] is False and "отменено" in r["error"]
    y = await rem.create_reminder(ctx, text="y", when="2026-09-29 10:00")
    r = await call(ctx, "update_reminder", id=y["id"], when="2026-09-01 10:00")
    assert r["ok"] is False and "прошло" in r["error"]
    assert (await rem.get_reminder(ctx.db, y["id"]))["local_start"] == "2026-09-29T10:00:00"
    r = await call(ctx, "update_reminder", id="abc", text="y")
    assert r["ok"] is False and "числом" in r["error"]


async def test_tool_cancel(ctx):
    x = await rem.create_reminder(ctx, text="x", when="2026-09-29 10:00")
    r = await call(ctx, "cancel_reminder", id=x["id"])
    assert r == {"ok": True, "id": x["id"], "text": "x", "cancelled": True}
    r = await call(ctx, "cancel_reminder", id=x["id"])
    assert r["ok"] is False and "отменено" in r["error"]


async def test_tool_ack_refuses_nagging_challenge(ctx):
    w = await rem.create_reminder(ctx, text="Подъём", when="2026-09-29 07:00", kind="wake")
    await ctx.db.execute("UPDATE reminders SET next_at=NULL, nag_active=1 WHERE id=?", (w["id"],))
    r = await call(ctx, "ack_reminder", id=w["id"])
    assert r["ok"] is False and "задачк" in r["error"]
    assert (await rem.get_reminder(ctx.db, w["id"]))["nag_active"] == 1


async def test_tool_ack_ok(ctx):
    x = await rem.create_reminder(ctx, text="x", when="2026-09-28 09:10", nag=True)
    await ctx.db.execute("UPDATE reminders SET next_at=NULL, nag_active=1 WHERE id=?", (x["id"],))
    r = await call(ctx, "ack_reminder", id=x["id"])
    assert r["ok"] and r["status"] == "done" and r["next"] is None
    # разовое, отмеченное заранее, больше не сработает
    y = await rem.create_reminder(ctx, text="позвонить", when="2026-09-28 18:00")
    r = await call(ctx, "ack_reminder", id=y["id"])
    assert r["ok"] and r["status"] == "done" and r["next"] is None
    assert (await rem.get_reminder(ctx.db, y["id"]))["next_at"] is None
    # будильник с задачкой, который ещё не долбит, отметить можно
    w = await rem.create_reminder(ctx, text="w", when="2026-09-29 07:00", kind="wake", rrule="FREQ=DAILY")
    r = await call(ctx, "ack_reminder", id=w["id"])
    assert r["ok"] and r["status"] == "active" and r["next"] == "вт 29.09 07:00"


def test_tool_schemas_registered(cfg):
    names = {s["function"]["name"]: s["function"] for s in tb.schemas(cfg)}
    for n in ("create_reminder", "list_reminders", "update_reminder", "cancel_reminder", "ack_reminder"):
        assert n in names
    p = names["create_reminder"]["parameters"]
    assert p["required"] == ["text", "when"]
    assert p["properties"]["kind"]["enum"] == ["reminder", "wake"]
    assert "YYYY-MM-DD HH:MM" in names["create_reminder"]["description"]
    assert "FREQ=WEEKLY;BYDAY=MO,WE,FR" in names["create_reminder"]["description"]


def test_helpers():
    assert rem.as_bool("да") is True and rem.as_bool("false") is False and rem.as_bool(None, True) is True
    assert rem.as_bool("что-то", None) is None and rem.as_bool(0) is False
    assert rem.as_id("#12") == 12 and rem.as_id(7) == 7
    with pytest.raises(ValueError):
        rem.as_id(True)
    assert rem.clean_repeat("нет") is None and rem.clean_repeat(None) is None
    with pytest.raises(ValueError):
        rem.clean_repeat(5)
    assert rem.zone("Нет/Такого").key == "UTC"
