"""Регрессии группы «время»: доставка при обрыве сети, ежедневные задачи с повтором, дни рождения
(«поздравить» с долбёжкой, догон), гонки состояния напоминаний, будильник с задачкой, смена пояса,
перенос одного срабатывания, повестка на день, сводка на остаток дня, мелочи разбора времени."""
from __future__ import annotations

import asyncio
import importlib
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from oracle import timeutil
from oracle.services import brief
from oracle.services import scheduler as sch
from oracle.services.scheduler import Scheduler
from oracle.tools import base as tb
from oracle.tools import birthdays as bd
from oracle.tools import calendar as cal
from oracle.tools import reminders as rem

@pytest.fixture(autouse=True)
def _owner_already_talked(monkeypatch):
    """Эти тесты — про механику ежедневных задач; «владелец ещё не писал» проверяется отдельно."""
    async def yes(self):
        return True
    monkeypatch.setattr(Scheduler, "_owner_talked", yes)


from conftest import FakeLLM, FakeNotifier

MSK = ZoneInfo("Europe/Moscow")
BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc
QUIET = dict(morning_brief_time="", birthday_time="", news_digest_time="", reflection_time="")


def utc(y, mo, d, h, mi=0, tz=MSK) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=tz).astimezone(UTC)


def set_local(clock, y, mo, d, h, mi=0) -> None:
    clock.set(utc(y, mo, d, h, mi))


def jobs(ctx, **kw):
    ctx.cfg = replace(ctx.cfg, **{**QUIET, **kw})
    return ctx


async def call(ctx, tool_name: str, /, **args) -> dict:
    return json.loads(await tb.dispatch(tool_name, args, ctx))


async def row(ctx, rid) -> dict:
    return await ctx.db.fetchone("SELECT * FROM reminders WHERE id=?", (rid,))


async def dialog_events(ctx) -> list[str]:
    return [r["content"] for r in await ctx.db.fetchall("SELECT content FROM messages WHERE role='event' ORDER BY id")]


@pytest.fixture(autouse=True)
def no_weather(monkeypatch):
    news = importlib.import_module("oracle.tools.news")

    async def none(ctx):
        return None

    monkeypatch.setattr(news, "weather_line", none)


class DownNotifier(FakeNotifier):
    """Telegram недоступен, пока down=True; exc — чем падать."""

    def __init__(self, exc: BaseException | None = None, fail_times: int | None = None):
        super().__init__()
        self.down = True
        self.exc = exc or ConnectionError("network is unreachable")
        self.fail_times = fail_times
        self.attempts = 0

    async def send(self, text, buttons=None, *, silent=False):
        self.attempts += 1
        if self.fail_times is not None:
            if self.fail_times > 0:
                self.fail_times -= 1
                raise self.exc
        elif self.down:
            raise self.exc
        return await super().send(text, buttons, silent=silent)


# ── F1: обрыв сети не теряет напоминания ──────────────────────────────────────
async def test_outage_reminders_are_delivered_late_not_lost(ctx, clock):
    jobs(ctx)
    net = DownNotifier()
    ctx.services.notifier = net
    one = await rem.create_reminder(ctx, text="позвонить маме", when="2026-09-28 09:05")
    daily = await rem.create_reminder(ctx, text="таблетки", when="2026-09-28 09:05", rrule="FREQ=DAILY")
    s = Scheduler(ctx)
    set_local(clock, 2026, 9, 28, 9, 5)
    for _ in range(40):                                   # 40 минут без связи
        await s.tick()
        clock.advance(minutes=1)
    assert net.sent == []
    assert (await row(ctx, one["id"]))["status"] == "active" and (await row(ctx, one["id"]))["fire_count"] == 0
    assert (await row(ctx, daily["id"]))["next_at"] == timeutil.iso(utc(2026, 9, 28, 9, 5))
    assert await dialog_events(ctx) == []                 # никаких ложных «Сработало»
    net.down = False
    await s.tick()
    texts = net.texts()
    assert texts == ["(пропустил, пока не было связи — было на пн 28.09 09:05) ⏰ позвонить маме",
                     "(пропустил, пока не было связи — было на пн 28.09 09:05) ⏰ таблетки"]
    assert (await row(ctx, one["id"]))["status"] == "done"
    assert (await row(ctx, daily["id"]))["next_at"] == timeutil.iso(utc(2026, 9, 29, 9, 5))
    await s.tick()
    assert len(net.sent) == 2


async def test_outage_stops_the_tick_after_first_network_error(ctx, clock):
    jobs(ctx)
    net = DownNotifier()
    ctx.services.notifier = net
    for i in range(5):
        await rem.create_reminder(ctx, text=f"дело {i}", when="2026-09-28 09:00")
    await Scheduler(ctx).tick()
    assert net.attempts == 1                              # не 5 × таймаут на каждом тике


def test_transient_errors_are_recognized():
    from aiogram.exceptions import (TelegramBadRequest, TelegramEntityTooLarge, TelegramForbiddenError,
                                    TelegramNetworkError, TelegramServerError)
    from aiogram.methods import SendMessage
    m = SendMessage(chat_id=1, text="x")
    assert sch.is_transient(TelegramNetworkError(method=m, message="Request timeout error"))
    assert sch.is_transient(TelegramServerError(method=m, message="Bad Gateway"))
    assert sch.is_transient(asyncio.TimeoutError()) and sch.is_transient(ConnectionResetError())
    assert not sch.is_transient(TelegramEntityTooLarge(method=m, message="too large"))
    assert not sch.is_transient(TelegramForbiddenError(method=m, message="bot was blocked by the user"))
    assert not sch.is_transient(TelegramBadRequest(method=m, message="chat not found"))
    assert not sch.is_transient(RuntimeError("x"))


async def test_followup_send_is_retried(ctx, clock, monkeypatch):
    jobs(ctx)
    monkeypatch.setattr(sch, "FOLLOWUP_RETRY", (0, 0, 0))

    class Agent:
        async def proactive(self, trigger):
            return "Ну что, резюме отправил?"

    ctx.services.agent = Agent()
    net = DownNotifier(fail_times=2)
    ctx.services.notifier = net
    await rem.create_reminder(ctx, text="про резюме", when="2026-09-28 09:00", kind="followup")
    await Scheduler(ctx).tick()
    await ctx.services.drain()
    assert net.texts() == ["Ну что, резюме отправил?"] and net.attempts == 3


# ── F2/F25/F6: ежедневные задачи не одноразовые ───────────────────────────────
async def test_birthday_greeting_retried_after_failed_send(ctx, clock, fake_llm):
    jobs(ctx, birthday_time="09:00")
    net = DownNotifier(fail_times=1)
    ctx.services.notifier = net
    bid = await ctx.db.execute("INSERT INTO birthdays(name, month, day, relation, created_at) "
                               "VALUES('Маша', 9, 28, 'сестра', 'x')")
    fake_llm.script = ["Маш, с днём рождения!"]
    s = Scheduler(ctx)
    await s.tick()
    await ctx.services.drain()
    assert net.sent == [] and await ctx.db.kv_get("job:birthdays") is None
    assert (await bd.get_birthday(ctx.db, bid))["last_greeted_year"] is None
    clock.advance(minutes=1)
    await s.tick()                                        # пауза перед повтором
    await ctx.services.drain()
    assert net.attempts == 1
    clock.advance(minutes=5)
    await s.tick()
    await ctx.services.drain()
    assert len(net.sent) == 1 and "Маш, с днём рождения!" in net.texts()[0]
    assert await ctx.db.kv_get("job:birthdays") == "2026-09-28"
    greeting_calls = [c for c in fake_llm.calls if "поздравление" in c["messages"][0]["content"]]
    assert len(greeting_calls) == 1                       # повтор не переписывает поздравление заново
    clock.advance(minutes=30)
    await s.tick()
    await ctx.services.drain()
    assert len(net.sent) == 1


async def test_birthday_job_is_not_spawned_twice_while_running(ctx, clock):
    jobs(ctx, birthday_time="09:00")
    await ctx.db.execute("INSERT INTO birthdays(name, month, day, created_at) VALUES('Маша', 9, 28, 'x')")
    gate = asyncio.Event()

    class SlowLLM(FakeLLM):
        async def ask(self, system, user, **kw):
            await gate.wait()
            return "Маша, поздравляю!"

    ctx.llm = SlowLLM()
    s = Scheduler(ctx)
    for _ in range(4):                                    # модель думает дольше нескольких тиков
        await s.tick()
        await asyncio.sleep(0)
        clock.advance(seconds=15)
    gate.set()
    await ctx.services.drain()
    assert len([t for t in ctx.services.notifier.texts() if t.startswith("🎂")]) == 1


async def test_morning_brief_recorded_only_after_delivery(ctx, clock, monkeypatch):
    jobs(ctx, morning_brief_time="08:00")
    made = []

    async def fake(c):
        made.append(1)
        return "Доброе утро! Сегодня пусто."

    monkeypatch.setattr(brief, "morning_brief", fake)
    net = DownNotifier(fail_times=1)
    ctx.services.notifier = net
    s = Scheduler(ctx)
    await s.tick()
    await ctx.services.drain()
    assert net.sent == [] and await dialog_events(ctx) == []
    clock.advance(minutes=6)
    await s.tick()
    await ctx.services.drain()
    assert net.texts() == ["Доброе утро! Сегодня пусто."] and len(made) == 1
    assert await dialog_events(ctx) == ["Утренняя сводка владельцу:\nДоброе утро! Сегодня пусто."]
    assert await ctx.db.kv_get("job:morning") == "2026-09-28"


async def test_morning_brief_gives_up_after_catch_up_window(ctx, clock, monkeypatch):
    jobs(ctx, morning_brief_time="08:00")

    async def fake(c):
        return "сводка"

    monkeypatch.setattr(brief, "morning_brief", fake)
    net = DownNotifier()
    ctx.services.notifier = net
    s = Scheduler(ctx)
    for _ in range(40):                                   # 08:00 → 11:20 каждые 5 минут
        await s.tick()
        await ctx.services.drain()
        clock.advance(minutes=5)
    assert await ctx.db.kv_get("job:morning") == "2026-09-28"
    tries = net.attempts
    assert 3 <= tries <= 15                               # с паузами, а не каждый тик
    net.down = False
    await s.tick()
    await ctx.services.drain()
    assert net.sent == [] and net.attempts == tries


async def test_daily_job_cancelled_mid_run_reruns_after_restart(ctx, clock, monkeypatch):
    jobs(ctx, morning_brief_time="08:00")
    gate = asyncio.Event()

    async def slow(c):
        await gate.wait()
        return "сводка"

    monkeypatch.setattr(brief, "morning_brief", slow)
    await Scheduler(ctx).tick()
    await ctx.services.drain(0.05)                       # остановка бота посреди задачи
    assert await ctx.db.kv_get("job:morning") is None
    gate.set()
    clock.advance(minutes=1)
    await Scheduler(ctx).tick()                           # новый процесс
    await ctx.services.drain()
    assert ctx.services.notifier.texts() == ["сводка"]


async def test_birthday_caught_up_after_late_start(ctx, clock, fake_llm):
    jobs(ctx, birthday_time="09:00")
    await ctx.db.execute("INSERT INTO birthdays(name, month, day, created_at) VALUES('Маша', 9, 28, 'x')")
    fake_llm.script = ["Маша, с днём рождения!"]
    set_local(clock, 2026, 9, 28, 12, 30)                 # ПК включили после обеда
    await Scheduler(ctx).tick()
    await ctx.services.drain()
    assert ctx.services.notifier.texts()[0].startswith("🎂 Сегодня день рождения: Маша")


async def test_birthday_belated_next_day(ctx, clock, fake_llm):
    jobs(ctx, birthday_time="09:00")
    bid = await ctx.db.execute("INSERT INTO birthdays(name, month, day, year, created_at) "
                               "VALUES('Маша', 9, 28, 1996, 'x')")
    fake_llm.script = ["Маш, прости, что с опозданием — с днём рождения!"]
    set_local(clock, 2026, 9, 29, 9, 0)                   # бот лежал весь день рождения
    s = Scheduler(ctx)
    await s.tick()
    await ctx.services.drain()
    text = ctx.services.notifier.texts()[0]
    assert text.startswith("🎂 Вчера был день рождения: Маша — исполнилось 30.")
    assert "лучше поздно" in text
    assert "запоздалое" in fake_llm.calls[-1]["messages"][1]["content"]
    assert (await bd.get_birthday(ctx.db, bid))["last_greeted_year"] == 2026
    set_local(clock, 2026, 9, 30, 9, 0)
    await s.tick()
    await ctx.services.drain()
    assert len([t for t in ctx.services.notifier.texts() if t.startswith("🎂")]) == 1


async def test_birthday_late_evening_time_after_midnight(ctx, clock, fake_llm):
    jobs(ctx, birthday_time="23:30")
    await ctx.db.execute("INSERT INTO birthdays(name, month, day, created_at) VALUES('Маша', 9, 28, 'x')")
    fake_llm.script = ["Маша, поздравляю!"]
    set_local(clock, 2026, 9, 29, 0, 20)
    await Scheduler(ctx).tick()
    await ctx.services.drain()
    assert ctx.services.notifier.texts()[0].startswith("🎂 Вчера был день рождения: Маша")


async def test_adding_yesterdays_birthday_does_not_trigger_belated(ctx, clock):
    await call(ctx, "add_birthday", name="Петя", date="27.09")
    assert await bd.birthday_jobs(ctx) == 0 and ctx.services.notifier.sent == []


# ── F4/F17: «поздравить» с долбёжкой ─────────────────────────────────────────
async def test_birthday_day_sets_relentless_congrats_reminder(ctx, clock, fake_llm):
    jobs(ctx, birthday_time="09:00")
    bid = await ctx.db.execute("INSERT INTO birthdays(name, month, day, relation, created_at) "
                               "VALUES('Маша', 9, 28, 'сестра', 'x')")
    fake_llm.script = ["Маш, с днём рождения!"]
    s = Scheduler(ctx)
    await s.tick()
    await ctx.services.drain()
    rows = await ctx.db.fetchall("SELECT * FROM reminders WHERE ref_type='birthday'")
    assert len(rows) == 1
    r = rows[0]
    assert r["ref_id"] == bid and r["nag"] == 1 and r["nag_interval_min"] == 60 and r["status"] == "active"
    # 10:00 + нажимы 11:00…21:00 + «сдаюсь» в 22:00 — всё не позже NAG_UNTIL
    assert r["next_at"] == timeutil.iso(utc(2026, 9, 28, 10, 0)) and r["nag_max"] == 11
    n = ctx.services.notifier
    set_local(clock, 2026, 9, 28, 10, 0)
    await s.tick()
    assert n.texts()[-1] == "⏰ Поздравить с днём рождения: Маша (сестра)"
    assert n.sent[-1]["buttons"][0][0] == ("✅ Готово", f"rem:done:{r['id']}")
    set_local(clock, 2026, 9, 28, 11, 0)
    await s.tick()
    assert n.texts()[-1].endswith(f"(#{r['id']}, 1/11)")
    await rem.ack_reminder(ctx.db, r["id"])
    for h in (12, 13, 14):
        set_local(clock, 2026, 9, 28, h, 0)
        await s.tick()
    assert n.texts()[-1].endswith(f"(#{r['id']}, 1/11)")


async def test_delete_birthday_cancels_congrats_reminder(ctx, clock):
    r = await call(ctx, "add_birthday", name="Маша", date="28.09")     # сегодня: сразу «поздравить»
    assert r["ok"] and "birthday_greeting" in r["note"]
    (nag,) = await ctx.db.fetchall("SELECT * FROM reminders WHERE ref_type='birthday'")
    assert nag["status"] == "active" and nag["nag"] == 1
    assert (await bd.get_birthday(ctx.db, r["id"]))["last_greeted_year"] == 2026
    await call(ctx, "delete_birthday", id=r["id"])
    assert (await row(ctx, nag["id"]))["status"] == "cancelled"


async def test_moving_birthday_cancels_congrats_reminder(ctx, clock):
    r = await call(ctx, "add_birthday", name="Маша", date="28.09")
    (nag,) = await ctx.db.fetchall("SELECT * FROM reminders WHERE ref_type='birthday'")
    await call(ctx, "update_birthday", id=r["id"], date="15.10")
    assert (await row(ctx, nag["id"]))["status"] == "cancelled"


# ── F21: поздравление при сохранении — только когда уместно ──────────────────
async def test_add_birthday_note_depends_on_days_left(ctx, clock):
    far = await call(ctx, "add_birthday", name="Лёха", date="14 марта", notes="фанат рыбалки")
    assert "Напиши поздравление" not in far["note"] and "не пиши" in far["note"]
    soon = await call(ctx, "add_birthday", name="Маша", date="29.09")
    assert "завтра" in soon["note"] and f"birthday_greeting(id={soon['id']})" in soon["note"]
    desc = tb.REGISTRY["add_birthday"].description
    assert "сразу напиши" not in desc and "birthday_greeting" in desc


async def test_greeting_written_the_day_before_is_reused_on_the_day(ctx, clock):
    b = await call(ctx, "add_birthday", name="Маша", date="29.09")
    ctx.llm = FakeLLM(["Маш, с днём рождения! Горы ждут."])
    g = await call(ctx, "birthday_greeting", id=b["id"])
    assert g["ok"]
    set_local(clock, 2026, 9, 29, 9, 0)
    ctx.llm = FakeLLM(["другой текст"])
    assert await bd.birthday_jobs(ctx) == 1
    assert ctx.services.notifier.texts()[-1].endswith("Маш, с днём рождения! Горы ждут.")
    assert ctx.llm.calls == []


# ── F3/F15/F16: отмена/перенос во время отправки не затираются ───────────────
class SlowNotifier(FakeNotifier):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, text, buttons=None, *, silent=False):
        self.entered.set()
        await self.release.wait()
        return await super().send(text, buttons, silent=silent)


async def test_cancel_during_send_is_not_reverted(ctx, clock):
    jobs(ctx)
    slow = SlowNotifier()
    ctx.services.notifier = slow
    r = await rem.create_reminder(ctx, text="Пить таблетки", when="2026-09-28 09:00", rrule="FREQ=DAILY")
    t = asyncio.ensure_future(Scheduler(ctx).tick())
    await slow.entered.wait()
    assert await rem.cancel_reminder(ctx.db, r["id"])
    slow.release.set()
    await t
    g = await row(ctx, r["id"])
    assert g["status"] == "cancelled"
    assert await dialog_events(ctx) == []


async def test_update_during_send_is_not_reverted(ctx, clock):
    jobs(ctx)
    slow = SlowNotifier()
    ctx.services.notifier = slow
    r = await rem.create_reminder(ctx, text="x", when="2026-09-28 09:00", rrule="FREQ=DAILY")
    t = asyncio.ensure_future(Scheduler(ctx).tick())
    await slow.entered.wait()
    u = await call(ctx, "update_reminder", id=r["id"], when="2026-09-28 10:00", repeat="none")
    assert u["ok"]
    slow.release.set()
    await t
    g = await row(ctx, r["id"])
    assert g["status"] == "active" and g["next_at"] == timeutil.iso(utc(2026, 9, 28, 10, 0))


async def test_cancelled_earlier_in_batch_is_not_sent(ctx, clock):
    jobs(ctx)
    slow = SlowNotifier()
    ctx.services.notifier = slow
    a = await rem.create_reminder(ctx, text="A", when="2026-09-28 09:00")
    b = await rem.create_reminder(ctx, text="B", when="2026-09-28 09:00")
    t = asyncio.ensure_future(Scheduler(ctx).tick())
    await slow.entered.wait()
    await rem.cancel_reminder(ctx.db, b["id"])
    slow.release.set()
    await t
    assert slow.texts() == ["⏰ A"] and (await row(ctx, b["id"]))["status"] == "cancelled"
    assert (await row(ctx, a["id"]))["status"] == "done"


# ── F5/F18: будильник с задачкой не снимается в обход задачки ─────────────────
async def _ringing_wake(ctx, clock):
    jobs(ctx)
    w = await rem.create_reminder(ctx, text="Подъём", when="2026-09-29 07:00", kind="wake", rrule="FREQ=DAILY")
    set_local(clock, 2026, 9, 29, 7, 0)
    await Scheduler(ctx).tick()
    assert (await row(ctx, w["id"]))["nag_active"] == 1
    return w


async def test_cancel_refused_while_challenge_alarm_rings(ctx, clock):
    w = await _ringing_wake(ctx, clock)
    r = await call(ctx, "cancel_reminder", id=w["id"])
    assert r["ok"] is False and "задачк" in r["error"]
    g = await row(ctx, w["id"])
    assert g["status"] == "active" and g["nag_active"] == 1
    for args in ({"when": "2026-09-29 09:00"}, {"repeat": "none"}, {"nag": False}):
        r = await call(ctx, "update_reminder", id=w["id"], **args)
        assert r["ok"] is False and "задачк" in r["error"], args
    assert (await call(ctx, "update_reminder", id=w["id"], text="Вставай, работа"))["ok"]
    assert "только задачкой" in tb.REGISTRY["cancel_reminder"].description


async def test_snooze_does_not_open_challenge_bypass(ctx, clock):
    w = await _ringing_wake(ctx, clock)
    await rem.snooze_reminder(ctx.db, w["id"], 5)
    for name in ("ack_reminder", "cancel_reminder"):
        r = await call(ctx, name, id=w["id"])
        assert r["ok"] is False and "задачк" in r["error"], name
    assert (await row(ctx, w["id"]))["snooze_at"] is not None


async def test_quiet_challenge_alarm_can_be_cancelled(ctx, clock):
    w = await rem.create_reminder(ctx, text="Подъём", when="2026-09-29 07:00", kind="wake", rrule="FREQ=DAILY")
    assert (await call(ctx, "cancel_reminder", id=w["id"]))["ok"]


# ── F8: nag=false гасит текущую долбёжку ──────────────────────────────────────
async def test_nag_false_stops_current_nagging(ctx, clock):
    jobs(ctx)
    r = await rem.create_reminder(ctx, text="таблетки", when="2026-09-28 10:00", rrule="FREQ=DAILY", nag=True)
    s = Scheduler(ctx)
    set_local(clock, 2026, 9, 28, 10, 0)
    await s.tick()
    u = await call(ctx, "update_reminder", id=r["id"], nag=False)
    assert u["ok"] and u["nag"] is False and u["nagging"] is False
    for _ in range(10):
        clock.advance(minutes=5)
        await s.tick()
    assert ctx.services.notifier.texts() == ["⏰ таблетки"]
    g = await row(ctx, r["id"])
    assert g["status"] == "active" and g["next_at"] == timeutil.iso(utc(2026, 9, 29, 10, 0))


async def test_nag_false_closes_nagging_one_off(ctx, clock):
    jobs(ctx)
    r = await rem.create_reminder(ctx, text="документы", when="2026-09-28 09:00", nag=True)
    s = Scheduler(ctx)
    await s.tick()
    u = await call(ctx, "update_reminder", id=r["id"], nag="false")
    assert u["ok"] and u.get("status") == "done"
    clock.advance(minutes=10)
    await s.tick()
    assert len(ctx.services.notifier.sent) == 1


# ── F9: пропущенное за время простоя не запускает долбёжку ────────────────────
async def test_stale_occurrence_does_not_start_nag_cycle(ctx, clock):
    jobs(ctx)
    set_local(clock, 2026, 9, 27, 8, 0)
    r = await rem.create_reminder(ctx, text="таблетки", when="2026-09-27 10:00", rrule="FREQ=DAILY", nag=True)
    set_local(clock, 2026, 9, 28, 9, 0)                   # бот лежал сутки
    s = Scheduler(ctx)
    await s.tick()
    for _ in range(9):
        clock.advance(minutes=5)
        await s.tick()
    texts = ctx.services.notifier.texts()
    assert texts == ["(пропустил, пока был выключен — было на вс 27.09 10:00) ⏰ таблетки"]
    assert (await row(ctx, r["id"]))["nag_active"] == 0


# ── F7: смена TIMEZONE ───────────────────────────────────────────────────────
async def test_update_reminder_after_timezone_change_uses_current_zone(ctx, clock):
    w = await rem.create_reminder(ctx, text="Подъём", when="2026-09-29 07:00", kind="wake", rrule="FREQ=DAILY")
    ctx.cfg = replace(ctx.cfg, timezone="Europe/Kaliningrad")
    u = await call(ctx, "update_reminder", id=w["id"], when="2026-09-30 07:00")
    assert u["ok"] and u["when"] == "ср 30.09 07:00"
    g = await row(ctx, w["id"])
    assert g["tz"] == "Europe/Kaliningrad" and g["local_start"] == "2026-09-30T07:00:00"
    # только текст — пояс строки не трогаем, время прежнее
    x = await rem.create_reminder(ctx, text="x", when="2026-10-01 12:00")
    ctx.cfg = replace(ctx.cfg, timezone="Europe/Moscow")
    before = (await row(ctx, x["id"]))["next_at"]
    await call(ctx, "update_reminder", id=x["id"], text="y")
    assert (await row(ctx, x["id"]))["next_at"] == before


async def test_event_update_after_timezone_change(ctx, clock):
    ev = await cal.add_event(ctx, title="Йога", start="2026-09-29 19:00", repeat="FREQ=WEEKLY;BYDAY=TU")
    ctx.cfg = replace(ctx.cfg, timezone="Europe/Berlin")
    # только название: событие остаётся в своём поясе, и напоминание — в том же поясе
    await cal.update_event(ctx, ev["id"], title="Йога в зале")
    (r,) = await ctx.db.fetchall("SELECT * FROM reminders WHERE ref_type='event' AND status='active'")
    e = await cal.get_event(ctx.db, ev["id"])
    assert r["tz"] == e["tz"] == "Europe/Moscow"
    dec_event = timeutil.next_occurrence(e["rrule"], e["local_start"], MSK, after=utc(2026, 12, 1, 0, 0))
    dec_rem = timeutil.next_occurrence(r["rrule"], r["local_start"], MSK, after=utc(2026, 12, 1, 0, 0))
    assert dec_event - dec_rem == timedelta(minutes=30)
    # новое время — в текущем поясе владельца
    u = await cal.update_event(ctx, ev["id"], start="2026-09-29 19:00")
    assert u["when"] == "вт 29.09 19:00–20:00"
    e = await cal.get_event(ctx.db, ev["id"])
    assert e["tz"] == "Europe/Berlin" and e["starts_at"] == timeutil.iso(utc(2026, 9, 29, 19, 0, tz=BERLIN))


async def test_all_day_event_keeps_its_day_after_timezone_change(ctx, clock):
    ev = await cal.add_event(ctx, title="Отпуск", start="2026-10-05", all_day=True, remind_before_min=None)
    ctx.cfg = replace(ctx.cfg, timezone="America/New_York")
    await cal.update_event(ctx, ev["id"], end="2026-10-07")
    e = await cal.get_event(ctx.db, ev["id"])
    assert e["local_start"] == "2026-10-05T00:00:00" and e["tz"] == "America/New_York"


# ── F10: UNTIL=…Z переводится в пояс правила ─────────────────────────────────
def test_normalize_rrule_until_z_is_converted():
    assert timeutil.normalize_rrule("FREQ=DAILY;UNTIL=20261005T060000Z", MSK) == "FREQ=DAILY;UNTIL=20261005T090000"
    assert timeutil.normalize_rrule("FREQ=DAILY;UNTIL=20261005T235959Z", MSK) == "FREQ=DAILY;UNTIL=20261005T235959"
    assert timeutil.normalize_rrule("FREQ=DAILY;UNTIL=20261005T060000Z") == "FREQ=DAILY;UNTIL=20261005T060000"


async def test_until_in_utc_keeps_last_occurrence(ctx, clock):
    r = await rem.create_reminder(ctx, text="x", when="2026-10-01 09:00",
                                  rrule="FREQ=DAILY;UNTIL=20261005T060000Z")
    last = timeutil.next_occurrence(r["rrule"], r["local_start"], MSK, after=utc(2026, 10, 5, 8, 0))
    assert last == utc(2026, 10, 5, 9, 0)


# ── F11: «через минуту» не падает на границе минуты ──────────────────────────
async def test_one_minute_ahead_survives_minute_boundary(ctx, clock):
    set_local(clock, 2026, 9, 28, 12, 1)
    clock.advance(seconds=3)
    r = await call(ctx, "create_reminder", text="чайник", when="2026-09-28 12:01")
    assert r["ok"] and r["when"] == "пн 28.09 12:01"
    assert timeutil.parse_local("12:01", MSK).date() == date(2026, 9, 28)
    clock.advance(minutes=10)
    bad = await call(ctx, "create_reminder", text="чайник", when="2026-09-28 12:01")
    assert bad["ok"] is False and "уже прошло" in bad["error"] and "сейчас пн 28.09 12:11" in bad["error"]


# ── F13: напоминание заранее для 31-го числа и 1 марта ───────────────────────
async def test_monthly_31st_reminder_only_before_real_events(ctx, clock):
    ev = await cal.add_event(ctx, title="Платёж", start="2026-10-31 10:00", repeat="FREQ=MONTHLY",
                             remind_before_min=1440)
    (r,) = await ctx.db.fetchall("SELECT * FROM reminders WHERE ref_type='event'")
    e = await cal.get_event(ctx.db, ev["id"])
    events, rems, after = [], [], utc(2026, 10, 1, 0, 0)
    for _ in range(7):
        after = timeutil.next_occurrence(e["rrule"], e["local_start"], MSK, after=after)
        events.append(after)
    after = utc(2026, 10, 1, 0, 0)
    for _ in range(7):
        after = timeutil.next_occurrence(r["rrule"], r["local_start"], MSK, after=after)
        rems.append(after)
    assert [x + timedelta(days=1) for x in rems] == events


async def test_yearly_first_of_march_reminder_in_leap_year(ctx, clock):
    await cal.add_event(ctx, title="Годовщина", start="2027-03-01", all_day=True, repeat="FREQ=YEARLY",
                        remind_before_min=1440)
    (r,) = await ctx.db.fetchall("SELECT * FROM reminders WHERE ref_type='event'")
    nxt = timeutil.next_occurrence(r["rrule"], r["local_start"], MSK, after=utc(2027, 6, 1, 0, 0))
    assert nxt.astimezone(MSK).date() == date(2028, 2, 29)
    first = timeutil.next_occurrence(r["rrule"], r["local_start"], MSK, after=utc(2026, 10, 1, 0, 0))
    assert first.astimezone(MSK).date() == date(2027, 2, 28)


def test_shift_rrule_cases():
    assert cal.shift_rrule("FREQ=MONTHLY", 1, date(2026, 10, 29), False) is None
    assert cal.shift_rrule("FREQ=YEARLY", 1, date(2027, 1, 1), False) == "FREQ=YEARLY"       # 31.12 — всегда
    assert cal.shift_rrule("FREQ=YEARLY", 2, date(2027, 3, 1), False) == "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=-2"


# ── F14: повторённый час перехода на зимнее ──────────────────────────────────
def test_inclusive_next_occurrence_in_repeated_hour():
    after = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)     # 02:30 CET, второй проход
    nxt = timeutil.next_occurrence("FREQ=DAILY", "2026-10-01T02:45:00", BERLIN, after=after, inclusive=True)
    assert nxt is not None and nxt >= after
    one = timeutil.next_occurrence(None, "2026-10-25T02:45:00", BERLIN, after=after, inclusive=True)
    assert one == datetime(2026, 10, 25, 1, 45, tzinfo=UTC)


# ── F19: перенести одно срабатывание, серия остаётся ─────────────────────────
async def test_only_next_moves_one_occurrence(ctx, clock):
    jobs(ctx)
    set_local(clock, 2026, 9, 28, 22, 0)
    w = await rem.create_reminder(ctx, text="Подъём", when="2026-09-29 07:00", kind="wake", rrule="FREQ=DAILY",
                                  nag=False)
    u = await call(ctx, "update_reminder", id=w["id"], when="2026-09-29 09:00", only_next=True)
    assert u["ok"] and u["skipped"] == "вт 29.09 07:00" and u["series_next"] == "ср 30.09 07:00"
    assert u["once"]["when"] == "вт 29.09 09:00"
    g = await row(ctx, w["id"])
    assert g["local_start"] == "2026-09-29T07:00:00" and g["rrule"] == "FREQ=DAILY"
    s = Scheduler(ctx)
    fired = []
    for day, hh in ((29, 7), (29, 9), (30, 7), (1, 7)):
        set_local(clock, 2026, 9 if day > 2 else 10, day, hh, 0)
        n = len(ctx.services.notifier.sent)
        await s.tick()
        if len(ctx.services.notifier.sent) > n:
            fired.append((day, hh))
    assert fired == [(29, 9), (30, 7), (1, 7)]


async def test_only_next_without_when_skips(ctx, clock):
    r = await rem.create_reminder(ctx, text="спортзал", when="2026-09-29 19:00", rrule="FREQ=DAILY")
    u = await call(ctx, "update_reminder", id=r["id"], only_next=True)
    assert u["ok"] and u["series_next"] == "ср 30.09 19:00" and "once" not in u
    bad = await call(ctx, "update_reminder", id=r["id"], only_next=True, when="2026-09-27 10:00")
    assert bad["ok"] is False
    assert (await row(ctx, r["id"]))["next_at"] == timeutil.iso(utc(2026, 9, 30, 19, 0))


# ── F20: повестка на любой день одним вызовом ────────────────────────────────
async def test_get_agenda_tomorrow_without_events(ctx, clock):
    projects = importlib.import_module("oracle.tools.projects")
    set_local(clock, 2026, 9, 28, 20, 0)
    await rem.create_reminder(ctx, text="Подъём", when="2026-09-29 07:00", kind="wake", rrule="FREQ=DAILY")
    await rem.create_reminder(ctx, text="спросить про юриста", when="2026-09-29 12:00", kind="followup")
    await ctx.db.execute("INSERT INTO tasks(text, status, priority, due_at, created_at) VALUES(?,?,?,?,?)",
                         ("Сдать отчёт", "todo", 2, timeutil.iso(utc(2026, 9, 29, 18, 0)), "x"))
    await ctx.db.execute("INSERT INTO birthdays(name, month, day, created_at) VALUES('Маша', 9, 29, 'x')")
    assert projects
    r = await call(ctx, "get_agenda", date="2026-09-29")
    assert r["ok"] and r["events"] == []
    assert [(x["when"], x["text"], x["kind"]) for x in r["reminders"]] == [("вт 29.09 07:00", "Подъём", "будильник")]
    assert [t["text"] for t in r["tasks"]] == ["Сдать отчёт"]
    assert [b["name"] for b in r["birthdays"]] == ["Маша"]
    assert "note" not in r
    # повторяющийся будильник виден и послезавтра, хотя next_at — завтра
    r = await call(ctx, "get_agenda", date="2026-09-30")
    assert [x["when"] for x in r["reminders"]] == ["ср 30.09 07:00"] and r["birthdays"] == [] and r["tasks"] == []
    r = await call(ctx, "get_agenda", **{"from": "2026-10-10", "to": "2026-10-10"})
    assert [x["when"] for x in r["reminders"]] == ["сб 10.10 07:00"]
    assert (await call(ctx, "get_agenda", **{"from": "2026-10-01", "to": "2026-12-31"}))["ok"] is False


# ── F22: /today вечером — без «доброго утра» и без прошедшего ────────────────
async def test_today_brief_in_the_evening(ctx, clock, fake_llm):
    await cal.add_event(ctx, title="Утренняя встреча", start="2026-09-28 10:00", remind_before_min=None)
    await cal.add_event(ctx, title="Ужин", start="2026-09-28 22:00", remind_before_min=None)
    set_local(clock, 2026, 9, 28, 21, 0)
    fake_llm.script = ["Вечером — ужин в 22:00."]
    assert await brief.today_brief(ctx) == "Вечером — ужин в 22:00."
    system, user = (m["content"] for m in fake_llm.calls[-1]["messages"])
    assert "утреннюю сводку" not in system and "Сейчас утро" not in system and "21:00" in system
    assert "Ужин" in user and "Утренняя встреча" not in user
    fake_llm.script = [lambda m, kw: (_ for _ in ()).throw(RuntimeError("нет связи"))]
    text = await brief.today_brief(ctx)
    assert not text.startswith("Доброе утро") and "Ужин" in text and "Утренняя" not in text
    # утренняя сводка — как была
    fake_llm.script = ["ок"]
    await brief.morning_brief(ctx)
    assert "утреннюю сводку" in fake_llm.calls[-1]["messages"][0]["content"]


# ── F23/F24: тексты ──────────────────────────────────────────────────────────
def test_owner_gender_in_greetings():
    assert bd.owner_female(SimpleNamespace(cfg=SimpleNamespace(owner_gender="f")))
    assert not bd.owner_female(SimpleNamespace(cfg=SimpleNamespace()))
    assert "Рада" in bd.fallback_greeting({"name": "Петя"}, female=True)
    assert "Рад," in bd.fallback_greeting({"name": "Петя"})


async def test_wake_give_up_text(ctx, clock):
    jobs(ctx)
    w = await rem.create_reminder(ctx, text="Подъём", when="2026-09-28 09:00", kind="wake", rrule="FREQ=DAILY",
                                  nag_max=1)
    s = Scheduler(ctx)
    await s.tick()
    for _ in range(2):
        clock.advance(minutes=3)
        await s.tick()
    assert ctx.services.notifier.texts()[-1] == "Всё, сдаюсь — похоже, проспал. Следующий будильник — вт 29.09 09:00."
    assert (await row(ctx, w["id"]))["nag_active"] == 0


async def test_prenotice_uses_plural(ctx, clock):
    await ctx.db.execute("INSERT INTO birthdays(name, month, day, remind_days_before, created_at) "
                         "VALUES('Иван', 10, 3, 5, 'x')")
    await bd.birthday_jobs(ctx)
    assert ctx.services.notifier.texts()[0].startswith("🎁 Через 5 дней день рождения: Иван.")


# ── F26: ISO со смещением без двоеточия и с долями секунды ───────────────────
def test_parse_local_iso_variants():
    assert timeutil.parse_local("2026-09-30T08:00+0300", MSK) == datetime(2026, 9, 30, 8, 0, tzinfo=MSK)
    d = timeutil.parse_local("2026-09-30T08:00:00.5+03:00", MSK)
    assert (d.hour, d.minute) == (8, 0)


async def test_late_evening_birthday_reminder_does_not_nag_at_night(ctx, clock, fake_llm):
    await ctx.db.execute("INSERT INTO birthdays(name, month, day, created_at) VALUES('Маша', 9, 28, 'x')")
    set_local(clock, 2026, 9, 28, 21, 30)                 # проверка запоздала до вечера
    fake_llm.script = ["Маша, поздравляю!"]
    assert await bd.birthday_jobs(ctx) == 1
    (r,) = await ctx.db.fetchall("SELECT * FROM reminders WHERE ref_type='birthday'")
    assert r["nag"] == 0 and r["next_at"] == timeutil.iso(utc(2026, 9, 28, 21, 45))
