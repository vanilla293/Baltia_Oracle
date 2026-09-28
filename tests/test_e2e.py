"""Сквозные сценарии без сети: настоящая база, настоящий агент, настоящие инструменты и планировщик;
модель — FakeLLM по сценарию, часы — фикстура clock, Telegram — FakeNotifier.

Проверяем стыки модулей: модель → tools.dispatch → строка в базе → scheduler.tick → сообщение
владельцу с кнопками (callback_data в формате, который разбирает bot/handlers.parse_cb)."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from oracle import timeutil
from oracle.agent import ACTIONS_PREFIX, Agent
from oracle.bot.handlers import parse_cb
from oracle.services.scheduler import WAKE_LINES, Scheduler
from oracle.tools import base as tb
from oracle.tools import birthdays as bd
from oracle.tools import reminders as rem

MSK = ZoneInfo("Europe/Moscow")
UTC = timezone.utc
QUIET = dict(morning_brief_time="", birthday_time="", news_digest_time="", reflection_time="")


def local(y, mo, d, h, mi=0) -> datetime:
    """Московское время → aware UTC."""
    return datetime(y, mo, d, h, mi, tzinfo=MSK).astimezone(UTC)


def call(tool_name: str, /, **args) -> dict:
    """Шаг сценария FakeLLM: модель зовёт инструмент."""
    return {"name": tool_name, "args": args}


def cb_data(buttons) -> list[str]:
    return [data for row in buttons or [] for _, data in row]


async def dialog(db) -> list[tuple[str, str, str]]:
    return [(r["role"], r["via"], r["content"])
            for r in await db.fetchall("SELECT role, via, content FROM messages ORDER BY id")]


@pytest.fixture
def world(ctx):
    """Бот целиком, как его собирает app.main: агент регистрируется в services, планировщик — рядом.
    Ежедневные задачи выключены (их включают сценарии, которым они нужны)."""
    ctx.cfg = replace(ctx.cfg, **QUIET)
    agent = Agent(ctx)
    assert ctx.services.agent is agent and agent.failed_modules == []
    return agent, Scheduler(ctx)


# ── (a) + (e): «напомни завтра в 7» → строка → срабатывание с кнопками ─────────
async def test_reminder_from_words_to_telegram(world, ctx, fake_llm, notifier, clock, db):
    agent, sched = world
    fake_llm.script = [
        call("create_reminder", text="позвонить маме", when="2026-09-29 07:00"),
        "Поставил: завтра в 7:00 напомню позвонить маме.",
    ]
    reply = await agent.handle("напомни завтра в 7 утра позвонить маме")
    assert reply.error is None and reply.text == "Поставил: завтра в 7:00 напомню позвонить маме."

    rows = await db.fetchall("SELECT * FROM reminders")
    assert len(rows) == 1
    r = rows[0]
    assert (r["text"], r["kind"], r["status"], r["rrule"]) == ("позвонить маме", "reminder", "active", None)
    assert (r["local_start"], r["tz"], r["next_at"]) == ("2026-09-29T07:00:00", "Europe/Moscow",
                                                          "2026-09-29T04:00:00+00:00")
    assert r["nag"] == 0 and r["challenge"] == 0

    # (e) первый вызов видел инструмент; второй — ответ инструмента с тем же tool_call_id
    assert len(fake_llm.calls) == 2
    first = fake_llm.calls[0]
    assert "create_reminder" in {t["function"]["name"] for t in first["tools"]}
    assert first["messages"][-1] == {"role": "user", "content": "напомни завтра в 7 утра позвонить маме"}
    msgs = fake_llm.calls[1]["messages"]
    asked = msgs[-2]
    assert asked["role"] == "assistant" and [c["id"] for c in asked["tool_calls"]] == ["call_0"]
    assert asked["tool_calls"][0]["function"]["name"] == "create_reminder"
    answer = msgs[-1]
    assert answer["role"] == "tool" and answer["tool_call_id"] == asked["tool_calls"][0]["id"]
    result = json.loads(answer["content"])
    assert result["ok"] is True and result["id"] == r["id"] and result["when"] == "вт 29.09 07:00"

    # в диалоге: реплика, журнал действий (следующий ход знает #id), ответ
    log = await dialog(db)
    assert log[0] == ("user", "text", "напомни завтра в 7 утра позвонить маме")
    assert log[1][0] == "event" and log[1][2].startswith(ACTIONS_PREFIX)
    assert f"create_reminder → ok #{r['id']} «позвонить маме» вт 29.09 07:00" in log[1][2]
    assert log[2] == ("assistant", "text", reply.text)

    # до срока — тишина
    clock.set(datetime(2026, 9, 29, 3, 59, tzinfo=UTC))
    await sched.tick()
    assert notifier.sent == []

    # в срок — «⏰ позвонить маме» с кнопками, которые понимает бот
    clock.set(datetime(2026, 9, 29, 4, 0, tzinfo=UTC))
    await sched.tick()
    assert notifier.texts() == ["⏰ позвонить маме"]
    data = cb_data(notifier.sent[0]["buttons"])
    assert f"rem:done:{r['id']}" in data and f"rem:snz:{r['id']}:10" in data
    assert all(parse_cb(d) is not None for d in data)
    fired = await rem.get_reminder(db, r["id"])
    assert fired["status"] == "done" and fired["next_at"] is None and fired["fire_count"] == 1
    assert (await dialog(db))[-1] == ("event", "system", f"Сработало напоминание #{r['id']}: позвонить маме")

    # второй тик ничего не повторяет
    clock.advance(minutes=5)
    await sched.tick()
    assert len(notifier.sent) == 1


# ── (b) будильник: срабатывание → долбёжка → отметка → тишина ─────────────────
async def test_wake_alarm_nags_until_acked(world, ctx, fake_llm, notifier, clock, db):
    agent, sched = world
    fake_llm.script = [call("create_reminder", text="Подъём", when="2026-09-29 06:30", kind="wake"),
                       "Разбужу в 6:30 и не отстану."]
    await agent.handle("разбуди меня завтра в 6:30 и не отставай")
    r = (await db.fetchall("SELECT * FROM reminders"))[0]
    assert (r["kind"], r["nag"], r["challenge"]) == ("wake", 1, 1)
    assert r["nag_interval_min"] == ctx.cfg.nag_interval_min == 3 and r["nag_max"] == ctx.cfg.nag_max
    rid = r["id"]

    clock.set(local(2026, 9, 29, 6, 30))
    await sched.tick()
    assert notifier.texts() == ["⏰ ПОДЪЁМ! Подъём"]
    assert notifier.sent[0]["buttons"] == [[("✅ Встал", f"rem:done:{rid}"), ("💤 5 мин", f"rem:snz:{rid}:5")]]
    row = await rem.get_reminder(db, rid)
    assert row["nag_active"] == 1 and row["nag_next_at"] == timeutil.iso(local(2026, 9, 29, 6, 33))

    # до интервала — тишина; через интервал — первая фраза долбёжки
    clock.advance(minutes=2)
    await sched.tick()
    assert len(notifier.sent) == 1
    clock.advance(minutes=1)
    await sched.tick()
    assert len(notifier.sent) == 2
    nag = notifier.texts()[-1]
    assert nag.startswith(WAKE_LINES[0]) and f"⏰ Подъём  (#{rid}, 1/{ctx.cfg.nag_max})" in nag
    assert cb_data(notifier.sent[-1]["buttons"])[0] == f"rem:done:{rid}"

    # пока долбит, модель снять будильник с задачкой не может — только кнопкой с задачкой
    refused = json.loads(await tb.dispatch("ack_reminder", {"id": rid}, ctx.child()))
    assert refused["ok"] is False and "задачк" in refused["error"]
    # в системном промпте следующего хода видно, что будильник долбит
    fake_llm.script = ["Вставай."]
    await agent.handle("ещё 5 минут")
    assert f"#{rid} Подъём — будильник с задачкой" in fake_llm.calls[-1]["messages"][0]["content"]

    # владелец решил задачку → бот зовёт ack_reminder → долбёжка стоп, разовое закрыто
    acked = await rem.ack_reminder(db, rid)
    assert acked["nag_active"] == 0 and acked["nag_next_at"] is None and acked["status"] == "done"
    sent = len(notifier.sent)
    for _ in range(3):
        clock.advance(minutes=3)
        await sched.tick()
    assert len(notifier.sent) == sent


# ── (c) идея: сохранить с оценкой → потом найти «про кофейню» ────────────────
async def test_idea_saved_then_found(world, ctx, fake_llm, notifier, clock, db):
    agent, _ = world
    fake_llm.script = [
        call("save_idea", title="Кофейня у вокзала",
             content="Маленькая кофейня навынос у вокзала для пассажиров утренних электричек",
             evaluation="Трафик есть, но аренда у вокзала дорогая; проверить поток за неделю.",
             score=5, tags=["общепит"]),
        "Честно: 5/10. Сохранил.",
    ]
    reply = await agent.handle("у меня идея: кофейня навынос у вокзала")
    idea = await db.fetchone("SELECT * FROM ideas")
    assert idea["title"] == "Кофейня у вокзала" and idea["score"] == 5 and idea["status"] == "new"
    # кнопка «додумать глубоко» уходит вложением после ответа
    assert [cb_data(o.buttons) for o in reply.outbox] == [[f"idea:deep:{idea['id']}"]]
    assert parse_cb(f"idea:deep:{idea['id']}") == ("idea", "deep", [idea["id"]])

    clock.advance(days=3)

    def answer_from_tool(messages, kw):
        found = json.loads(messages[-1]["content"])
        assert messages[-1]["role"] == "tool" and found["ok"]
        hit = found["items"][0]
        return f"Нашёл: #{hit['id']} {hit['title']}"

    fake_llm.script = [call("find_ideas", query="про кофейню"), answer_from_tool]
    reply = await agent.handle("найди мою идею про кофейню")
    assert reply.text == f"Нашёл: #{idea['id']} Кофейня у вокзала"

    direct = json.loads(await tb.dispatch("find_ideas", {"query": "про кофейню"}, ctx.child()))
    assert direct["items"][0]["id"] == idea["id"] and direct["items"][0]["match"] is True


# ── (d) день рождения: сохранить → накануне предупредить → в день поздравление ──
async def test_birthday_prenotice_and_greeting_via_scheduler(world, ctx, fake_llm, notifier, clock, db):
    agent, sched = world
    ctx.cfg = replace(ctx.cfg, birthday_time="09:00")
    fake_llm.script = [call("add_birthday", name="Маша", date="30.09.1996", relation="сестра"),
                       "Запомнил: Маша, 30 сентября, исполнится 30."]
    await agent.handle("у сестры Маши др 30 сентября 1996")
    b = await db.fetchone("SELECT * FROM birthdays")
    assert (b["name"], b["month"], b["day"], b["year"], b["relation"]) == ("Маша", 9, 30, 1996, "сестра")

    # накануне в BIRTHDAY_TIME — предупреждение (без модели)
    clock.set(local(2026, 9, 29, 9, 0))
    await sched.tick()
    await ctx.services.drain()
    assert notifier.texts() == ["🎁 Завтра день рождения: Маша (сестра) — исполнится 30.\n"
                                "Подарок или поздравление лучше решить заранее — могу помочь."]

    # в сам день — поздравление от модели с кнопкой «другой вариант»
    greeting = "Маш, с днём рождения! Помню, как ты учила меня кататься на велике."
    fake_llm.script = [greeting]
    clock.set(local(2026, 9, 30, 9, 0))
    await sched.tick()
    await ctx.services.drain()
    assert len(notifier.sent) == 2
    sent = notifier.sent[-1]
    assert sent["text"].startswith("🎂 Сегодня день рождения: Маша (сестра) — исполняется 30.")
    assert sent["text"].endswith(greeting)
    assert sent["buttons"] == [[("🔁 Другой вариант", f"bday:regen:{b['id']}")]]   # userbot выключен — без «отправить»
    assert parse_cb(f"bday:regen:{b['id']}") == ("bday", "regen", [b["id"]])
    assert await db.kv_get(bd.greeting_key(b["id"])) == greeting
    assert (await bd.get_birthday(db, b["id"]))["last_greeted_year"] == 2026
    assert "ОТ ИМЕНИ владельца" in fake_llm.calls[-1]["messages"][0]["content"]
    assert ("event", "system", "Сегодня ДР у Маша; поздравление-черновик отправлено владельцу") in await dialog(db)

    # тот же день, ещё тик — повторно не поздравляет
    clock.advance(minutes=30)
    await sched.tick()
    await ctx.services.drain()
    assert len(notifier.sent) == 2


# ── follow-up: модель ставит себе вернуться → планировщик зовёт агента → он пишет первым ──
async def test_followup_goes_through_agent(world, ctx, fake_llm, notifier, clock, db):
    agent, sched = world
    fake_llm.script = [call("schedule_followup", when="2026-09-30 12:00",
                            about="спросить, отправил ли резюме — тянет неделю"),
                       "Ок, отправляй. Я спрошу в среду."]
    await agent.handle("резюме отправлю на днях")
    fu = await db.fetchone("SELECT * FROM reminders WHERE kind='followup'")
    assert fu is not None and fu["nag"] == 0

    fake_llm.script = ["Ну что, резюме ушло?"]
    clock.set(local(2026, 9, 30, 12, 0))
    await sched.tick()
    await ctx.services.drain()
    assert notifier.texts() == ["Ну что, резюме ушло?"]
    assert notifier.sent[0]["buttons"] is None
    trigger = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "спросить, отправил ли резюме" in trigger and trigger.startswith("[внутренний триггер]")
    assert (await dialog(db))[-1] == ("assistant", "system", "Ну что, резюме ушло?")
    assert (await rem.get_reminder(db, fu["id"]))["status"] == "done"
