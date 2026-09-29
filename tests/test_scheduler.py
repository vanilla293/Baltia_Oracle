"""Планировщик и утренняя сводка: срабатывания, повторы, пропущенное, долбёжка, отложенные,
follow-up'ы, ежедневные задачи, уборка диалога, цикл; сводка через модель и запасной шаблон."""
from __future__ import annotations

import asyncio
import importlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from oracle import timeutil
from oracle.llm import LLMError
from oracle.services import brief
from oracle.services import scheduler as sch
from oracle.services.scheduler import Scheduler

from conftest import FakeNotifier

@pytest.fixture(autouse=True)
def _owner_already_talked(monkeypatch):
    """Эти тесты — про механику ежедневных задач; «владелец ещё не писал» проверяется отдельно."""
    async def yes(self):
        return True
    monkeypatch.setattr(Scheduler, "_owner_talked", yes)


MSK = ZoneInfo("Europe/Moscow")
UTC = timezone.utc
QUIET = dict(morning_brief_time="", birthday_time="", news_digest_time="", reflection_time="")


# ── помощники ────────────────────────────────────────────────────────────────
def utc(y, mo, d, h, mi=0) -> datetime:
    """Местное московское время → aware UTC."""
    return datetime(y, mo, d, h, mi, tzinfo=MSK).astimezone(UTC)


def set_local(clock, y, mo, d, h, mi=0) -> None:
    clock.set(utc(y, mo, d, h, mi))


def jobs_cfg(ctx, **kw):
    ctx.cfg = replace(ctx.cfg, **{**QUIET, **kw})
    return ctx


@pytest.fixture
def sctx(ctx):
    """Контекст без ежедневных задач — чтобы тесты напоминаний не ловили сводки."""
    return jobs_cfg(ctx)


@pytest.fixture(autouse=True)
def no_weather(monkeypatch):
    """Погода в тестах — без сети: по умолчанию «не настроена»."""
    try:
        from oracle.tools import news
    except ImportError:
        return

    async def none(ctx):
        return None

    monkeypatch.setattr(news, "weather_line", none)


async def add_rem(ctx, text, local, *, rrule=None, kind="reminder", nag=False, challenge=False,
                  interval=3, nag_max=20, next_at=None, status="active") -> int:
    """Строка reminders напрямую (без модуля reminders): next_at = local_start, если не задан."""
    start = datetime.fromisoformat(local)
    nxt = next_at if next_at is not None else timeutil.iso(start.replace(tzinfo=MSK))
    return await ctx.db.execute(
        "INSERT INTO reminders(text, kind, local_start, tz, rrule, next_at, status, nag, nag_interval_min, "
        "nag_max, challenge, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (text, kind, timeutil.naive_str(start), "Europe/Moscow", rrule, nxt, status, int(nag), interval,
         nag_max, int(challenge), timeutil.iso(timeutil.now_utc())))


async def row(ctx, rid) -> dict:
    return await ctx.db.fetchone("SELECT * FROM reminders WHERE id=?", (rid,))


async def events(ctx) -> list[str]:
    rows = await ctx.db.fetchall("SELECT content FROM messages WHERE role='event' ORDER BY id")
    return [r["content"] for r in rows]


def std_buttons(rid):
    return [[("✅ Готово", f"rem:done:{rid}"), ("💤 10 мин", f"rem:snz:{rid}:10"), ("💤 1 час", f"rem:snz:{rid}:60")]]


def wake_buttons(rid):
    return [[("✅ Встал", f"rem:done:{rid}"), ("💤 5 мин", f"rem:snz:{rid}:5")]]


class FakeAgent:
    def __init__(self, reply="Слушай, ты резюме отправил? Неделю уже тянешь.", fail=False):
        self.reply, self.fail = reply, fail
        self.triggers: list[str] = []
        self.reflected = 0
        self.summarized = 0

    async def proactive(self, trigger: str) -> str:
        self.triggers.append(trigger)
        if self.fail:
            raise RuntimeError("модель легла")
        return self.reply

    async def reflect(self):
        self.reflected += 1
        return "запись в дневник"

    async def summarize_old(self) -> bool:
        self.summarized += 1
        return True


class FlakyNotifier(FakeNotifier):
    def __init__(self):
        super().__init__()
        self.fail = True
        self.attempts = 0

    async def send(self, text, buttons=None, *, silent=False):
        self.attempts += 1
        if self.fail:
            raise ConnectionError("telegram недоступен")
        return await super().send(text, buttons, silent=silent)


# ── срабатывания ─────────────────────────────────────────────────────────────
async def test_one_off_fires_once_and_becomes_done(sctx, clock, notifier):
    rid = await add_rem(sctx, "Позвонить маме", "2026-09-28 09:10")
    s = Scheduler(sctx)
    await s.tick()
    assert notifier.sent == []
    clock.advance(minutes=10)
    await s.tick()
    assert len(notifier.sent) == 1
    m = notifier.sent[0]
    assert m["text"] == "⏰ Позвонить маме"
    assert m["buttons"] == std_buttons(rid)
    r = await row(sctx, rid)
    assert r["status"] == "done" and r["next_at"] is None and r["fire_count"] == 1
    assert r["last_fired_at"] == timeutil.iso(clock.now) and r["nag_active"] == 0
    assert await events(sctx) == [f"Сработало напоминание #{rid}: Позвонить маме"]
    clock.advance(minutes=5)
    await s.tick()
    assert len(notifier.sent) == 1


async def test_real_create_reminder_fires(sctx, clock, notifier):
    rem = importlib.import_module("oracle.tools.reminders")
    r0 = await rem.create_reminder(sctx, text="Вынести мусор", when="2026-09-28 09:05")
    s = Scheduler(sctx)
    clock.advance(minutes=5)
    await s.tick()
    assert notifier.texts() == ["⏰ Вынести мусор"]
    assert (await rem.get_reminder(sctx.db, r0["id"]))["status"] == "done"


async def test_daily_recurring_advances_to_tomorrow(sctx, clock, notifier):
    rid = await add_rem(sctx, "Таблетки", "2026-09-28 09:05", rrule="FREQ=DAILY")
    s = Scheduler(sctx)
    clock.advance(minutes=5)
    await s.tick()
    r = await row(sctx, rid)
    assert r["next_at"] == timeutil.iso(utc(2026, 9, 29, 9, 5))
    assert r["status"] == "active" and r["fire_count"] == 1
    assert notifier.texts() == ["⏰ Таблетки"]
    await s.tick()
    assert len(notifier.sent) == 1
    set_local(clock, 2026, 9, 29, 9, 5)
    await s.tick()
    assert notifier.texts() == ["⏰ Таблетки", "⏰ Таблетки"]
    r = await row(sctx, rid)
    assert r["next_at"] == timeutil.iso(utc(2026, 9, 30, 9, 5)) and r["fire_count"] == 2


async def test_recurring_until_ends_and_closes(sctx, clock, notifier):
    rid = await add_rem(sctx, "Курс антибиотиков", "2026-09-28 09:00",
                        rrule="FREQ=DAILY;UNTIL=20260928T235959")
    await Scheduler(sctx).tick()
    r = await row(sctx, rid)
    assert r["next_at"] is None and r["status"] == "done"


async def test_missed_recurring_fires_once_and_jumps_forward(sctx, clock, notifier):
    set_local(clock, 2026, 9, 25, 6, 0)
    rid = await add_rem(sctx, "Таблетки", "2026-09-25 07:00", rrule="FREQ=DAILY")
    set_local(clock, 2026, 9, 28, 9, 0)          # бот лежал трое суток
    s = Scheduler(sctx)
    await s.tick()
    assert notifier.texts() == ["(пропустил, пока был выключен — было на пн 28.09 07:00) ⏰ Таблетки"]
    r = await row(sctx, rid)
    assert r["next_at"] == timeutil.iso(utc(2026, 9, 29, 7, 0)) and r["fire_count"] == 1
    await s.tick()
    assert len(notifier.sent) == 1


async def test_missed_one_off_has_prefix(sctx, clock, notifier):
    rid = await add_rem(sctx, "Забрать посылку", "2026-09-28 07:00")
    await Scheduler(sctx).tick()
    assert notifier.texts() == ["(пропустил, пока был выключен — было на пн 28.09 07:00) ⏰ Забрать посылку"]
    assert (await row(sctx, rid))["status"] == "done"


async def test_slightly_late_has_no_prefix(sctx, clock, notifier):
    await add_rem(sctx, "Чайник", "2026-09-28 08:52")        # 8 минут — ещё не «пропустил»
    await Scheduler(sctx).tick()
    assert notifier.texts() == ["⏰ Чайник"]


async def test_frequent_rule_created_long_ago_is_not_late(sctx, clock, notifier):
    # «каждые 5 минут» полгода назад: последнее вхождение — только что, опоздания нет
    rid = await add_rem(sctx, "Размяться", "2026-03-01 08:00", rrule="FREQ=MINUTELY;INTERVAL=5",
                        next_at=timeutil.iso(utc(2026, 9, 28, 8, 55)))
    await Scheduler(sctx).tick()
    assert notifier.texts() == ["⏰ Размяться"]
    assert (await row(sctx, rid))["next_at"] == timeutil.iso(utc(2026, 9, 28, 9, 5))


async def test_weekday_rule_skips_weekend(sctx, clock, notifier):
    set_local(clock, 2026, 10, 2, 9, 0)                        # пятница
    rid = await add_rem(sctx, "Спортзал", "2026-10-02 09:30", rrule="FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR")
    s = Scheduler(sctx)
    set_local(clock, 2026, 10, 2, 9, 30)
    await s.tick()
    assert notifier.texts() == ["⏰ Спортзал"]
    assert (await row(sctx, rid))["next_at"] == timeutil.iso(utc(2026, 10, 5, 9, 30))   # понедельник


async def test_event_and_task_texts(sctx, clock, notifier):
    e = await add_rem(sctx, "Через 30 мин: Стоматолог", "2026-09-28 09:00", kind="event")
    t = await add_rem(sctx, "Задача: отчёт [Кофейня]", "2026-09-28 09:00", kind="task")
    await Scheduler(sctx).tick()
    assert notifier.texts() == ["📅 Через 30 мин: Стоматолог", "⏰ Задача: отчёт [Кофейня]"]
    assert notifier.sent[0]["buttons"] == std_buttons(e) and notifier.sent[1]["buttons"] == std_buttons(t)


async def test_bad_rule_in_db_fires_once(sctx, clock, notifier):
    rid = await add_rem(sctx, "Кривое", "2026-09-28 09:00", rrule="мусор")
    ok = await add_rem(sctx, "Нормальное", "2026-09-28 09:00")
    s = Scheduler(sctx)
    await s.tick()
    await s.tick()
    assert notifier.texts() == ["⏰ Кривое", "⏰ Нормальное"]
    r = await row(sctx, rid)
    assert r["next_at"] is None and r["status"] == "done"
    assert (await row(sctx, ok))["status"] == "done"


async def test_corrupt_row_does_not_repeat_forever(sctx, clock, notifier):
    rid = await add_rem(sctx, "Битое", "2026-09-28 09:00", rrule="FREQ=DAILY")
    await sctx.db.execute("UPDATE reminders SET local_start='???' WHERE id=?", (rid,))
    s = Scheduler(sctx)
    await s.tick()
    await s.tick()
    assert notifier.texts() == ["⏰ Битое"]
    r = await row(sctx, rid)
    assert r["next_at"] is None and r["status"] == "done"


async def test_ticks_do_not_overlap(sctx, clock, notifier):
    await add_rem(sctx, "Один раз", "2026-09-28 09:00")
    s = Scheduler(sctx)
    await asyncio.gather(s.tick(), s.tick(), s.tick())
    assert notifier.texts() == ["⏰ Один раз"]


# ── долбёжка ─────────────────────────────────────────────────────────────────
async def test_nag_flow_escalates_and_gives_up(sctx, clock, notifier):
    rid = await add_rem(sctx, "Выпить таблетку", "2026-09-28 09:00", nag=True, interval=3, nag_max=5)
    s = Scheduler(sctx)
    t0 = clock.now
    await s.tick()
    r = await row(sctx, rid)
    assert r["nag_active"] == 1 and r["nag_count"] == 0 and r["status"] == "active" and r["next_at"] is None
    assert r["nag_next_at"] == timeutil.iso(t0 + timedelta(minutes=3))
    clock.advance(minutes=2)
    await s.tick()
    assert len(notifier.sent) == 1                     # до интервала — тихо
    lines = []
    for i in range(1, 6):
        clock.set(t0 + timedelta(minutes=3 * i))
        await s.tick()
        m = notifier.sent[-1]
        assert len(notifier.sent) == 1 + i
        head, tail = m["text"].split("\n")
        assert tail == f"⏰ Выпить таблетку  (#{rid}, {i}/5)"
        assert m["buttons"] == std_buttons(rid)
        lines.append(head)
    assert lines == [sch.nag_line(i, 5) for i in range(1, 6)]
    assert lines[0] == sch.NAG_LINES[0] and lines[-1] == sch.NAG_LINES[-1]
    assert len(set(lines)) == 5
    clock.set(t0 + timedelta(minutes=18))
    await s.tick()
    m = notifier.sent[-1]
    assert m["text"] == "Всё, сдаюсь. «Выпить таблетку» — отметь, когда сделаешь." and m["buttons"] is None
    r = await row(sctx, rid)
    assert r["nag_active"] == 0 and r["status"] == "done"
    clock.advance(minutes=30)
    await s.tick()
    assert len(notifier.sent) == 7                     # 1 срабатывание + 5 долбёжек + «сдаюсь»


async def test_recurring_nag_stays_active_after_giving_up(sctx, clock, notifier):
    rid = await add_rem(sctx, "Зарядка", "2026-09-28 09:00", rrule="FREQ=DAILY", nag=True, nag_max=1)
    s = Scheduler(sctx)
    await s.tick()
    clock.advance(minutes=3)
    await s.tick()
    clock.advance(minutes=3)
    await s.tick()
    assert notifier.sent[-1]["text"].startswith("Всё, сдаюсь.")
    r = await row(sctx, rid)
    assert r["status"] == "active" and r["nag_active"] == 0
    assert r["next_at"] == timeutil.iso(utc(2026, 9, 29, 9, 0))


def test_nag_lines_escalate():
    assert len(sch.NAG_LINES) >= 12 and len(sch.WAKE_LINES) >= 12
    assert len(set(sch.NAG_LINES)) == len(sch.NAG_LINES)
    assert len(set(sch.WAKE_LINES)) == len(sch.WAKE_LINES)
    assert sch.nag_line(1, 20) == sch.NAG_LINES[0]
    assert sch.nag_line(20, 20) == sch.NAG_LINES[-1]
    assert sch.nag_line(1, 20, wake=True) == sch.WAKE_LINES[0]
    assert sch.nag_line(20, 20, wake=True) == sch.WAKE_LINES[-1]
    idx = [sch.NAG_LINES.index(sch.nag_line(i, 20)) for i in range(1, 21)]
    assert idx == sorted(idx) and len(set(idx)) == 20          # при nag_max=20 — каждая фраза своя
    idx = [sch.WAKE_LINES.index(sch.nag_line(i, 7, wake=True)) for i in range(1, 8)]
    assert idx == sorted(idx) and idx[0] == 0 and idx[-1] == len(sch.WAKE_LINES) - 1
    assert sch.nag_line(99, 5) == sch.NAG_LINES[-1]            # за пределом — не падает
    assert sch.nag_line(1, 1) == sch.NAG_LINES[0]
    assert sch.nag_line(0, 0) == sch.NAG_LINES[0]


async def test_ack_stops_nags(sctx, clock, notifier):
    rem = importlib.import_module("oracle.tools.reminders")
    r0 = await rem.create_reminder(sctx, text="Позвонить в банк", when="2026-09-28 09:00", nag=True)
    s = Scheduler(sctx)
    await s.tick()
    clock.advance(minutes=3)
    await s.tick()
    assert len(notifier.sent) == 2
    await rem.ack_reminder(sctx.db, r0["id"])
    for _ in range(5):
        clock.advance(minutes=3)
        await s.tick()
    assert len(notifier.sent) == 2
    r = await row(sctx, r0["id"])
    assert r["status"] == "done" and r["nag_active"] == 0


async def test_stale_nag_after_downtime_stops(sctx, clock, notifier):
    rid = await add_rem(sctx, "Отправить документы", "2026-09-28 09:00", nag=True)
    s = Scheduler(sctx)
    await s.tick()
    clock.advance(hours=5)                              # бот лежал: долбить в 14:00 уже поздно
    await s.tick()
    assert notifier.sent[-1]["text"] == sch.STALE_GIVE_UP.format(text="Отправить документы")
    r = await row(sctx, rid)
    assert r["nag_active"] == 0 and r["status"] == "done"
    clock.advance(minutes=3)
    await s.tick()
    assert len(notifier.sent) == 2


# ── будильник ────────────────────────────────────────────────────────────────
async def test_wake_text_buttons_and_lines(sctx, clock, notifier):
    rid = await add_rem(sctx, "На работу к 10", "2026-09-28 09:00", kind="wake", nag=True, challenge=True)
    s = Scheduler(sctx)
    await s.tick()
    m = notifier.sent[0]
    assert m["text"] == "⏰ ПОДЪЁМ! На работу к 10" and m["buttons"] == wake_buttons(rid)
    clock.advance(minutes=3)
    await s.tick()
    m = notifier.sent[1]
    assert m["text"] == f"{sch.WAKE_LINES[0]}\n⏰ На работу к 10  (#{rid}, 1/20)"
    assert m["buttons"] == wake_buttons(rid)
    assert (await row(sctx, rid))["nag_count"] == 1


async def test_wake_missed_by_hours_does_not_nag(sctx, clock, notifier):
    rid = await add_rem(sctx, "Подъём", "2026-09-28 07:00", kind="wake", nag=True, challenge=True)
    await Scheduler(sctx).tick()
    assert notifier.texts() == ["(пропустил, пока был выключен — было на пн 28.09 07:00) ⏰ ПОДЪЁМ! Подъём"]
    r = await row(sctx, rid)
    assert r["nag_active"] == 0 and r["status"] == "done"


# ── отложенные ───────────────────────────────────────────────────────────────
async def test_snooze_refires_without_touching_schedule(sctx, clock, notifier):
    rem = importlib.import_module("oracle.tools.reminders")
    r0 = await rem.create_reminder(sctx, text="Полить цветы", when="2026-09-28 09:00", rrule="FREQ=DAILY")
    s = Scheduler(sctx)
    await s.tick()
    before = await row(sctx, r0["id"])
    assert before["next_at"] == timeutil.iso(utc(2026, 9, 29, 9, 0))
    await rem.snooze_reminder(sctx.db, r0["id"], 10)
    clock.advance(minutes=9)
    await s.tick()
    assert len(notifier.sent) == 1
    clock.advance(minutes=1)
    await s.tick()
    m = notifier.sent[-1]
    assert m["text"] == "💤→ ⏰ Полить цветы" and m["buttons"] == std_buttons(r0["id"])
    after = await row(sctx, r0["id"])
    assert after["next_at"] == before["next_at"] and after["rrule"] == before["rrule"]
    assert after["snooze_at"] is None and after["status"] == "active" and after["fire_count"] == 1
    await s.tick()
    assert len(notifier.sent) == 2
    assert (await events(sctx))[-1] == f"Сработало отложенное напоминание #{r0['id']}: Полить цветы"


async def test_snooze_one_off_closes_after_refire(sctx, clock, notifier):
    rem = importlib.import_module("oracle.tools.reminders")
    r0 = await rem.create_reminder(sctx, text="Забрать ключи", when="2026-09-28 09:00")
    s = Scheduler(sctx)
    await s.tick()
    assert (await row(sctx, r0["id"]))["status"] == "done"
    await rem.snooze_reminder(sctx.db, r0["id"], 10)
    assert (await row(sctx, r0["id"]))["status"] == "active"
    clock.advance(minutes=10)
    await s.tick()
    assert notifier.texts()[-1] == "💤→ ⏰ Забрать ключи"
    r = await row(sctx, r0["id"])
    assert r["status"] == "done" and r["snooze_at"] is None


async def test_snooze_pauses_and_resumes_nagging(sctx, clock, notifier):
    rem = importlib.import_module("oracle.tools.reminders")
    r0 = await rem.create_reminder(sctx, text="Встать", when="2026-09-28 09:00", kind="wake")
    s = Scheduler(sctx)
    await s.tick()
    await rem.snooze_reminder(sctx.db, r0["id"], 5)
    clock.advance(minutes=3)
    await s.tick()
    assert len(notifier.sent) == 1                      # отложено — не долбим
    clock.advance(minutes=2)
    await s.tick()
    assert notifier.sent[-1]["text"] == "💤→ ⏰ ПОДЪЁМ! Встать"
    assert notifier.sent[-1]["buttons"] == wake_buttons(r0["id"])
    r = await row(sctx, r0["id"])
    assert r["nag_active"] == 1 and r["nag_count"] == 0
    assert r["nag_next_at"] == timeutil.iso(clock.now + timedelta(minutes=3))
    clock.advance(minutes=3)
    await s.tick()
    assert notifier.sent[-1]["text"].endswith(f"(#{r0['id']}, 1/20)")


async def test_due_and_snooze_in_same_tick_send_once(sctx, clock, notifier):
    rid = await add_rem(sctx, "Проверить почту", "2026-09-28 09:00", rrule="FREQ=HOURLY")
    await sctx.db.execute("UPDATE reminders SET snooze_at=? WHERE id=?", (timeutil.iso(clock.now), rid))
    await Scheduler(sctx).tick()
    assert notifier.texts() == ["⏰ Проверить почту"]
    assert (await row(sctx, rid))["snooze_at"] is None


# ── follow-up'ы ──────────────────────────────────────────────────────────────
async def test_followup_goes_through_agent(sctx, clock, notifier):
    agent = FakeAgent()
    sctx.services.agent = agent
    rid = await add_rem(sctx, "спросить про резюме", "2026-09-28 09:00", kind="followup")
    await Scheduler(sctx).tick()
    await sctx.services.drain()
    assert agent.triggers == ["Ты сам поставил себе вернуться к теме: спросить про резюме"]
    assert notifier.texts() == [agent.reply] and notifier.sent[0]["buttons"] is None
    r = await row(sctx, rid)
    assert r["status"] == "done" and r["fire_count"] == 1
    assert await events(sctx) == []                     # сырой текст в диалог не пишем — это дело агента


async def test_followup_agent_failure_falls_back(sctx, clock, notifier):
    sctx.services.agent = FakeAgent(fail=True)
    rid = await add_rem(sctx, "спросить про анализы", "2026-09-28 09:00", kind="followup")
    await Scheduler(sctx).tick()
    await sctx.services.drain()
    assert notifier.texts() == ["🔔 Хотел вернуться к теме: спросить про анализы"]
    assert (await row(sctx, rid))["status"] == "done"


async def test_followup_empty_agent_reply_falls_back(sctx, clock, notifier):
    sctx.services.agent = FakeAgent(reply="   ")
    await add_rem(sctx, "про ремонт", "2026-09-28 09:00", kind="followup")
    await Scheduler(sctx).tick()
    await sctx.services.drain()
    assert notifier.texts() == ["🔔 Хотел вернуться к теме: про ремонт"]


async def test_followup_without_agent_sends_plain(sctx, clock, notifier):
    rid = await add_rem(sctx, "спросить про спортзал", "2026-09-28 09:00", kind="followup", nag=True)
    s = Scheduler(sctx)
    await s.tick()
    assert notifier.texts() == ["🔔 спросить про спортзал"] and notifier.sent[0]["buttons"] is None
    r = await row(sctx, rid)
    assert r["status"] == "done" and r["nag_active"] == 0          # follow-up никогда не долбит
    clock.advance(minutes=3)
    await s.tick()
    assert len(notifier.sent) == 1


# ── отправка ─────────────────────────────────────────────────────────────────
async def test_notifier_none_still_advances(sctx, clock):
    sctx.services.notifier = None
    a = await add_rem(sctx, "Раз", "2026-09-28 09:00")
    b = await add_rem(sctx, "Долбит", "2026-09-28 09:00", nag=True, nag_max=2)
    c = await add_rem(sctx, "Каждый день", "2026-09-28 09:00", rrule="FREQ=DAILY")
    s = Scheduler(sctx)
    await s.tick()
    assert (await row(sctx, a))["status"] == "done"
    assert (await row(sctx, c))["next_at"] == timeutil.iso(utc(2026, 9, 29, 9, 0))
    for _ in range(3):
        clock.advance(minutes=3)
        await s.tick()
    r = await row(sctx, b)
    assert r["nag_active"] == 0 and r["status"] == "done"
    assert len(await events(sctx)) == 3


async def test_send_failure_retries_next_tick(sctx, clock):
    flaky = FlakyNotifier()
    sctx.services.notifier = flaky
    rid = await add_rem(sctx, "Позвонить", "2026-09-28 09:00")
    s = Scheduler(sctx)
    await s.tick()
    r = await row(sctx, rid)
    assert r["status"] == "active" and r["fire_count"] == 0 and r["next_at"] is not None
    assert await events(sctx) == []
    flaky.fail = False
    clock.advance(seconds=15)
    await s.tick()
    assert flaky.texts() == ["⏰ Позвонить"]
    assert (await row(sctx, rid))["status"] == "done"


class RefusingNotifier(FlakyNotifier):
    """Telegram отказывает всерьёз (бот заблокирован) — это не «нет сети»."""

    async def send(self, text, buttons=None, *, silent=False):
        self.attempts += 1
        if self.fail:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        return await FakeNotifier.send(self, text, buttons, silent=silent)


async def test_send_failure_gives_up_after_a_while(sctx, clock):
    flaky = RefusingNotifier()
    sctx.services.notifier = flaky
    rid = await add_rem(sctx, "Позвонить", "2026-09-28 09:00")
    s = Scheduler(sctx)
    await s.tick()
    clock.advance(minutes=10)
    await s.tick()
    assert (await row(sctx, rid))["fire_count"] == 0
    clock.advance(minutes=25)
    await s.tick()                                      # 35 минут отказывает — не застреваем
    r = await row(sctx, rid)
    assert r["status"] == "done" and r["fire_count"] == 1
    assert flaky.attempts == 3
    assert await events(sctx) == [f"Не смог доставить напоминание #{rid} (Telegram не принимал): Позвонить"]
    # связь с владельцем вернулась — первым же удачным сообщением говорим, что не дошло
    flaky.fail = False
    await add_rem(sctx, "Следующее", "2026-09-28 09:40")
    clock.advance(minutes=5)
    await s.tick()
    assert flaky.texts()[0] == "⏰ Следующее"
    assert flaky.texts()[1].startswith("⚠️ Раньше Telegram не принимал") and "⏰ Позвонить" in flaky.texts()[1]
    clock.advance(minutes=1)
    await s.tick()
    assert len(flaky.sent) == 2


# ── ежедневные задачи ────────────────────────────────────────────────────────
@pytest.fixture
def fake_brief(monkeypatch):
    calls = []

    async def fake(ctx):
        calls.append(ctx)
        return "Доброе утро! Сегодня пусто — займись отчётом."

    monkeypatch.setattr(brief, "morning_brief", fake)
    return calls


async def test_morning_job_runs_once_per_day(ctx, clock, notifier, fake_brief):
    jobs_cfg(ctx, morning_brief_time="08:00")
    s = Scheduler(ctx)
    await s.tick()
    await ctx.services.drain()
    assert notifier.texts() == ["Доброе утро! Сегодня пусто — займись отчётом."]
    assert await ctx.db.kv_get("job:morning") == "2026-09-28"
    assert (await events(ctx))[-1].startswith("Утренняя сводка владельцу:")
    assert fake_brief[0].outbox == [] and fake_brief[0] is not ctx     # отдельный контекст фоновой задачи
    clock.advance(minutes=15)
    await s.tick()
    await ctx.services.drain()
    assert len(fake_brief) == 1
    set_local(clock, 2026, 9, 29, 7, 59)
    await s.tick()
    await ctx.services.drain()
    assert len(fake_brief) == 1
    set_local(clock, 2026, 9, 29, 8, 0)
    await s.tick()
    await ctx.services.drain()
    assert len(fake_brief) == 2 and await ctx.db.kv_get("job:morning") == "2026-09-29"


async def test_morning_job_catch_up_within_3h(ctx, clock, notifier, fake_brief):
    jobs_cfg(ctx, morning_brief_time="08:00")
    set_local(clock, 2026, 9, 28, 10, 59)
    await Scheduler(ctx).tick()
    await ctx.services.drain()
    assert len(fake_brief) == 1


async def test_morning_job_skipped_after_window(ctx, clock, notifier, fake_brief):
    jobs_cfg(ctx, morning_brief_time="08:00")
    set_local(clock, 2026, 9, 28, 11, 1)
    s = Scheduler(ctx)
    await s.tick()
    await ctx.services.drain()
    assert fake_brief == [] and notifier.sent == []
    assert await ctx.db.kv_get("job:morning") == "2026-09-28"          # день помечен — днём не всплывёт
    clock.advance(hours=2)
    await s.tick()
    await ctx.services.drain()
    assert fake_brief == []


async def test_daily_jobs_disabled_when_time_empty(ctx, clock, notifier, fake_brief):
    jobs_cfg(ctx)
    ctx.services.agent = FakeAgent()
    s = Scheduler(ctx)
    for h in range(24):
        set_local(clock, 2026, 9, 28, h, 1)
        await s.tick()
    await ctx.services.drain()
    assert fake_brief == [] and notifier.sent == [] and ctx.services.agent.reflected == 0
    for name in ("morning", "birthdays", "news", "reflection"):
        assert await ctx.db.kv_get(f"job:{name}") is None


async def test_invalid_job_time_is_ignored(ctx, clock, notifier, fake_brief):
    jobs_cfg(ctx, morning_brief_time="25:99")
    await Scheduler(ctx).tick()
    await ctx.services.drain()
    assert fake_brief == []


async def test_reflection_catch_up_over_midnight(ctx, clock, notifier):
    jobs_cfg(ctx, reflection_time="23:30")
    agent = FakeAgent()
    ctx.services.agent = agent
    set_local(clock, 2026, 9, 29, 0, 30)               # бот поднялся после полуночи
    s = Scheduler(ctx)
    await s.tick()
    await ctx.services.drain()
    assert agent.reflected == 1 and notifier.sent == []            # рефлексия молчаливая
    assert await ctx.db.kv_get("job:reflection") == "2026-09-28"
    set_local(clock, 2026, 9, 29, 23, 29)
    await s.tick()
    await ctx.services.drain()
    assert agent.reflected == 1
    set_local(clock, 2026, 9, 29, 23, 30)
    await s.tick()
    await ctx.services.drain()
    assert agent.reflected == 2


async def test_reflection_without_agent_is_skipped(ctx, clock, notifier):
    jobs_cfg(ctx, reflection_time="08:30")
    await Scheduler(ctx).tick()
    await ctx.services.drain()
    assert notifier.sent == [] and await ctx.db.kv_get("job:reflection") == "2026-09-28"


async def test_birthday_job_calls_birthday_jobs(ctx, clock, notifier, monkeypatch):
    bd = importlib.import_module("oracle.tools.birthdays")
    calls = []

    async def fake(c, **kw):
        calls.append(c)
        return 0

    monkeypatch.setattr(bd, "birthday_jobs", fake)
    jobs_cfg(ctx, birthday_time="09:00")
    await Scheduler(ctx).tick()
    await ctx.services.drain()
    assert len(calls) == 1 and calls[0].services is ctx.services


async def test_birthday_job_real_module(ctx, clock, notifier, fake_llm):
    importlib.import_module("oracle.tools.birthdays")
    await ctx.db.execute("INSERT INTO birthdays(name, month, day, relation, created_at) "
                         "VALUES('Маша', 9, 28, 'сестра', 'x')")
    fake_llm.script = ["Маш, с днём рождения! Обнимаю."]
    jobs_cfg(ctx, birthday_time="09:00")
    await Scheduler(ctx).tick()
    await ctx.services.drain()
    assert len(notifier.sent) == 1 and "Маша" in notifier.sent[0]["text"]
    assert "Маш, с днём рождения!" in notifier.sent[0]["text"]


async def test_news_job_sends_digest(ctx, clock, notifier, monkeypatch):
    news = importlib.import_module("oracle.tools.news")

    async def fake(c, topic=""):
        return "Главное за сутки: ничего не взорвалось."

    monkeypatch.setattr(news, "news_digest", fake)
    jobs_cfg(ctx, news_digest_time="09:00")
    await Scheduler(ctx).tick()
    await ctx.services.drain()
    assert notifier.texts() == ["🗞 Главное за сутки: ничего не взорвалось."]


async def test_job_errors_are_reported_briefly(ctx, clock, notifier, monkeypatch):
    news = importlib.import_module("oracle.tools.news")

    async def boom(c, *a, **kw):
        raise RuntimeError("всё сломалось")

    monkeypatch.setattr(brief, "morning_brief", boom)
    monkeypatch.setattr(news, "news_digest", boom)
    jobs_cfg(ctx, morning_brief_time="08:00", news_digest_time="09:00")
    s = Scheduler(ctx)
    await s.tick()
    await ctx.services.drain()
    assert sorted(notifier.texts()) == sorted(["не собрал сводку: всё сломалось",
                                               "не собрал сводку новостей: всё сломалось"])
    # задача помечена — на следующем тике не повторяется
    await s.tick()
    await ctx.services.drain()
    assert len(notifier.sent) == 2


async def test_step_failure_does_not_block_other_steps(ctx, clock, notifier, fake_brief):
    jobs_cfg(ctx, morning_brief_time="08:00")
    s = Scheduler(ctx)

    async def broken(now):
        raise RuntimeError("шаг сломан")

    s._due = broken
    await s.tick()
    await ctx.services.drain()
    assert len(fake_brief) == 1


# ── уборка ───────────────────────────────────────────────────────────────────
async def test_housekeeping_summarizes_old_messages(sctx, clock, notifier):
    agent = FakeAgent()
    sctx.services.agent = agent
    limit = sctx.cfg.history_messages + sctx.cfg.summary_chunk
    for i in range(limit):
        await sctx.db.add_message("user", f"реплика {i}")
    s = Scheduler(sctx)
    await s.tick()
    await sctx.services.drain()
    assert agent.summarized == 0
    await sctx.db.add_message("user", "ещё одна")
    await s.tick()
    await sctx.services.drain()
    assert agent.summarized == 1
    await s.tick()
    await sctx.services.drain()
    assert agent.summarized == 1                        # не чаще раза в 10 минут
    clock.advance(minutes=11)
    await s.tick()
    await sctx.services.drain()
    assert agent.summarized == 2


async def test_housekeeping_without_agent_is_noop(sctx, clock):
    for i in range(200):
        await sctx.db.add_message("user", f"реплика {i}")
    await Scheduler(sctx).tick()                        # просто не падает


# ── цикл ─────────────────────────────────────────────────────────────────────
async def test_start_and_stop_loop(sctx, clock, notifier):
    await add_rem(sctx, "Из цикла", "2026-09-28 09:00")
    s = Scheduler(sctx, tick_seconds=0.01)
    s.start()
    s.start()                                           # повторный старт — без второго цикла
    assert sctx.services.scheduler is s and s.running
    for _ in range(200):
        if notifier.sent:
            break
        await asyncio.sleep(0.01)
    await s.stop()
    assert not s.running
    assert notifier.texts() == ["⏰ Из цикла"]
    await s.stop()                                      # повторная остановка — тихо


async def test_loop_survives_tick_errors(sctx, clock):
    s = Scheduler(sctx, tick_seconds=0.01)
    calls = []

    async def flaky_tick():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("тик сломался")

    s.tick = flaky_tick
    s.start()
    for _ in range(200):
        if len(calls) >= 4:
            break
        await asyncio.sleep(0.01)
    await s.stop()
    assert len(calls) >= 4


# ── утренняя сводка ──────────────────────────────────────────────────────────
@pytest.fixture
async def brief_data(ctx, clock, monkeypatch):
    """День с событием, напоминаниями, ДР завтра, просроченной задачей, дневником и погодой."""
    cal = importlib.import_module("oracle.tools.calendar")
    rem = importlib.import_module("oracle.tools.reminders")
    importlib.import_module("oracle.tools.birthdays")
    importlib.import_module("oracle.tools.projects")
    mem = importlib.import_module("oracle.tools.memory")
    news = importlib.import_module("oracle.tools.news")

    async def weather(c):
        return "Москва: сейчас +10°, ясно; днём +8…+14°"

    monkeypatch.setattr(news, "weather_line", weather)
    await cal.add_event(ctx, title="Встреча с Петровым", start="2026-09-28 15:00", end="2026-09-28 16:00",
                        location="офис")
    await cal.add_event(ctx, title="Совещание во вторник", start="2026-09-29 15:00")
    await cal.add_event(ctx, title="День города", start="2026-09-28", all_day=True, remind_before_min=None)
    await rem.create_reminder(ctx, text="Позвонить маме", when="2026-09-28 18:00", rrule="FREQ=DAILY")
    await rem.create_reminder(ctx, text="Напоминание на завтра", when="2026-09-29 18:00")
    await rem.create_reminder(ctx, text="секретный follow-up", when="2026-09-28 19:00", kind="followup")
    await ctx.db.execute("INSERT INTO birthdays(name, month, day, year, relation, created_at) "
                         "VALUES('Маша', 9, 29, 1996, 'сестра', 'x')")
    await ctx.db.execute("INSERT INTO birthdays(name, month, day, created_at) VALUES('Далёкий', 12, 1, 'x')")
    await ctx.db.execute(
        "INSERT INTO tasks(text, status, priority, due_at, created_at) VALUES(?,?,?,?,?)",
        ("Отправить отчёт", "todo", 1, timeutil.iso(utc(2026, 9, 25, 18, 0)), "x"))
    await mem.add_journal(ctx.db, "Он третью неделю откладывает звонок юристу — надо надавить.")
    return ctx


async def test_morning_brief_uses_llm(brief_data, fake_llm):
    ctx = brief_data
    fake_llm.script = ["Понедельник, +10 и солнце. В 15:00 Петров, вечером — маме. И юристу позвони."]
    text = await brief.morning_brief(ctx)
    assert text == "Понедельник, +10 и солнце. В 15:00 Петров, вечером — маме. И юристу позвони."
    call = fake_llm.calls[-1]
    assert call["deep"] is False and call["temperature"] == 0.9
    system, user = call["messages"][0]["content"], call["messages"][1]["content"]
    assert "утреннюю сводку" in system and "1200" in system and "Оракул" in system
    assert "понедельник" in user
    assert "Погода: Москва: сейчас +10°, ясно; днём +8…+14°" in user
    assert "- 15:00–16:00 Встреча с Петровым (место: офис)" in user
    assert "- весь день День города" in user
    assert "Совещание во вторник" not in user
    assert "- 18:00 Позвонить маме (каждый день)" in user
    assert "Напоминание на завтра" not in user and "секретный" not in user
    assert "Через 30 мин" not in user                  # напоминания о событиях — не дублируем
    assert "- завтра: Маша (сестра), исполнится 30" in user and "Далёкий" not in user
    assert "ПРОСРОЧЕНО (срок был пт 25.09 18:00): Отправить отчёт, важная" in user
    assert "юристу" in user


async def test_morning_brief_fallback_on_llm_error(brief_data, fake_llm):
    def boom(messages, kw):
        raise LLMError("баланс кончился", status=402, fatal=True)

    fake_llm.script = [boom]
    text = await brief.morning_brief(brief_data)
    assert text.startswith("Доброе утро. Сегодня понедельник, 28 сентября.\nМосква: сейчас +10°")
    assert "• 15:00–16:00 Встреча с Петровым (место: офис)" in text
    assert "• весь день День города" in text
    assert "• 18:00 Позвонить маме (каждый день)" in text
    assert "• завтра — Маша (сестра), исполнится 30" in text
    assert "• просрочено: Отправить отчёт" in text
    assert "Надавлю на одно: «Отправить отчёт» висит с пт 25.09 18:00 — закрой сегодня." in text
    assert "секретный" not in text and "Совещание во вторник" not in text


async def test_morning_brief_fallback_on_empty_answer(brief_data, fake_llm):
    fake_llm.script = ["   "]
    text = await brief.morning_brief(brief_data)
    assert text.startswith("Доброе утро.")


async def test_morning_brief_empty_day(ctx, fake_llm):
    # утром пустой день — не повод писать: ни модели, ни сообщения
    assert await brief.morning_brief(ctx) == "" and fake_llm.calls == []
    # а если он сам спросил (/today) — ответ есть
    fake_llm.script = [lambda m, kw: (_ for _ in ()).throw(LLMError("нет связи"))]
    text = await brief.today_brief(ctx)
    assert text and "📅" not in text and "📌" not in text


async def test_morning_brief_journal_push_when_no_tasks(ctx, fake_llm):
    mem = importlib.import_module("oracle.tools.memory")
    await mem.add_journal(ctx.db, "- Вернуться к идее кофейни: он загорелся и пропал.\n- Спросить про сон.")
    fake_llm.script = [lambda m, kw: (_ for _ in ()).throw(LLMError("нет связи"))]
    text = await brief.morning_brief(ctx)
    assert text.rstrip().endswith("Из моих заметок: Вернуться к идее кофейни: он загорелся и пропал.")


async def test_morning_brief_ignores_stale_journal(ctx, clock, fake_llm):
    await ctx.db.execute("INSERT INTO journal(content, created_at) VALUES(?, ?)",
                         ("старая запись про юриста", timeutil.iso(clock.now - timedelta(days=10))))
    fake_llm.script = ["ок"]
    assert await brief.morning_brief(ctx) == ""        # старый дневник — не повестка: утро пустое, молчим
    await brief.morning_brief(ctx, mode="now")
    user = fake_llm.calls[-1]["messages"][1]["content"]
    assert "юриста" not in user


async def test_morning_brief_source_failure_is_isolated(brief_data, fake_llm, monkeypatch):
    from oracle.tools import calendar

    async def broken(*a, **kw):
        raise RuntimeError("календарь сломан")

    monkeypatch.setattr(calendar, "events_between", broken)
    fake_llm.script = ["ок"]
    assert await brief.morning_brief(brief_data) == "ок"
    user = fake_llm.calls[-1]["messages"][1]["content"]
    assert "Встреча с Петровым" not in user and "Позвонить маме" in user
    assert "Не удалось получить: events" in user
    # и шаблон без модели тоже собирается
    fake_llm.script = [lambda m, kw: (_ for _ in ()).throw(LLMError("x"))]
    text = await brief.morning_brief(brief_data)
    assert "Позвонить маме" in text and "Встреча" not in text


async def test_morning_brief_everything_broken_still_returns_text(ctx, fake_llm, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("всё сломано")

    monkeypatch.setattr(brief, "gather", boom)
    fake_llm.script = [lambda m, kw: (_ for _ in ()).throw(LLMError("x"))]
    text = await brief.morning_brief(ctx)
    assert text.startswith("Доброе утро. Сегодня понедельник, 28 сентября.")


async def test_morning_brief_event_crossing_midnight(ctx, clock, fake_llm):
    cal = importlib.import_module("oracle.tools.calendar")
    set_local(clock, 2026, 9, 27, 20, 0)
    await cal.add_event(ctx, title="Ночная смена", start="2026-09-27 22:00", end="2026-09-28 06:00",
                        remind_before_min=-1)
    set_local(clock, 2026, 9, 28, 5, 0)
    fake_llm.script = ["ок"]
    await brief.morning_brief(ctx)
    user = fake_llm.calls[-1]["messages"][1]["content"]
    assert "- идёт, до 06:00 Ночная смена" in user


# ── быстрый пересчёт повторов ────────────────────────────────────────────────
@pytest.mark.parametrize("rule", [
    "FREQ=DAILY", "FREQ=DAILY;INTERVAL=3", "FREQ=WEEKLY;BYDAY=MO,TH", "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,FR",
    "FREQ=WEEKLY;INTERVAL=3", "FREQ=HOURLY;INTERVAL=5", "FREQ=MINUTELY;INTERVAL=7", "FREQ=MONTHLY;BYMONTHDAY=-1",
    "FREQ=DAILY;BYDAY=SA,SU", "FREQ=YEARLY", "FREQ=DAILY;COUNT=400", "FREQ=DAILY;UNTIL=20261015T235959",
])
def test_fast_forward_matches_plain_iteration(rule):
    berlin = ZoneInfo("Europe/Berlin")                  # с переходом на зимнее 25.10
    # эталон (обычный перебор от DTSTART) для поминутного правила сам по себе медленный — старт поближе
    start = datetime(2026, 9, 1, 7, 30) if "MINUTELY" in rule else datetime(2025, 3, 3, 7, 30)
    for after in (datetime(2026, 9, 28, 6, 0, tzinfo=UTC), datetime(2026, 10, 24, 23, 59, tzinfo=UTC),
                  datetime(2026, 10, 25, 1, 0, tzinfo=UTC), datetime(2026, 12, 31, 22, 0, tzinfo=UTC)):
        assert sch.next_after(rule, start, berlin, after) == \
            timeutil.next_occurrence(rule, start, berlin, after=after)
        due = datetime(2025, 3, 3, 6, 30, tzinfo=UTC)
        last = sch.last_occurrence(rule, start, berlin, after, due)
        assert due <= last <= after
        if last > due:                                   # это настоящее вхождение и следующее — уже после after
            nxt = timeutil.next_occurrence(rule, start, berlin, after=last)
            assert nxt is None or nxt > after or nxt == last


def test_fast_forward_is_fast_for_old_frequent_rules():
    import time as _time
    t = _time.perf_counter()
    for _ in range(20):
        sch.next_after("FREQ=MINUTELY;INTERVAL=5", "2024-01-01T08:00:00", MSK,
                       datetime(2026, 9, 28, 6, 0, tzinfo=UTC))
    assert _time.perf_counter() - t < 1.0


def test_parse_hhmm_and_texts():
    assert sch.parse_hhmm("08:00").hour == 8 and sch.parse_hhmm("7.05").minute == 5
    assert sch.parse_hhmm("") is None and sch.parse_hhmm("25:00") is None and sch.parse_hhmm(None) is None
    assert sch.fire_text({"text": "  ", "kind": "zzz"}) == "⏰ (без текста)"
    assert sch.reminder_buttons({"id": 3, "kind": "followup"}) is None
    for b in (sch.reminder_buttons({"id": 10**9, "kind": "reminder"}), sch.reminder_buttons({"id": 10**9, "kind": "wake"})):
        assert all(len(cb.encode()) <= 64 for r in b for _, cb in r)


async def test_daily_digest_and_brief_wait_until_owner_talks(ctx, clock, fake_llm, monkeypatch):
    """Первый запуск: незнакомцу ни сводок, ни дайджестов — пока он сам не написал боту."""
    monkeypatch.undo()                                    # настоящий _owner_talked, без автофикстуры
    from dataclasses import replace as _r
    sctx = ctx
    sctx.cfg = _r(ctx.cfg, morning_brief_time="08:00", news_digest_time="08:30", birthday_time="", reflection_time="")
    s = Scheduler(sctx)
    await s.tick()
    await ctx.services.drain()
    assert ctx.services.notifier.sent == [] and fake_llm.calls == []
    assert await ctx.db.kv_get("job:morning") == "2026-09-28" and await ctx.db.kv_get("job:news") == "2026-09-28"
    await ctx.db.add_message("user", "привет")
    assert await s._owner_talked() is True
