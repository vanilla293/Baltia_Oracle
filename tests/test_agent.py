"""Агент: ход диалога, цикл инструментов, журнал действий, инициатива, конспект, сброс, рефлексия."""
from __future__ import annotations

import asyncio
import importlib
import json
from dataclasses import replace

import pytest

import oracle.tools as tools_pkg
from oracle.agent import (ACTIONS_MAX, EMPTY_TEXT, EVENT_PREFIX, PROACTIVE_NOTE, SUMMARY_MAX, SUMMARY_SYSTEM,
                          VOICE_NOTE, Agent, AgentReply, actions_text, describe_action, merge_history)
from oracle.llm import LLMError, LLMResponse, ToolCall
from oracle.tools import base as tb
from oracle.tools.base import OutItem, Services, ToolContext

MEM = "oracle.tools.memory"
REM = "oracle.tools.reminders"
PRJ = "oracle.tools.projects"


# ── помощники ────────────────────────────────────────────────────────────────
def boom(msg: str = "сервер модели сбоит (503)"):
    def f(messages, kw):
        raise LLMError(msg)
    return f


def make_agent(ctx: ToolContext, **cfg_kw) -> Agent:
    if cfg_kw:
        ctx = ToolContext(cfg=replace(ctx.cfg, **cfg_kw), db=ctx.db, llm=ctx.llm, services=ctx.services)
    return Agent(ctx)


async def dialog(db) -> list[tuple[str, str]]:
    return [(r["role"], r["content"]) for r in await db.fetchall("SELECT role, content FROM messages ORDER BY id")]


def system_of(call: dict) -> str:
    assert call["messages"][0]["role"] == "system"
    return call["messages"][0]["content"]


@pytest.fixture
def test_tools():
    """Два временных инструмента: эхо с id и инструмент, кладущий вложения в outbox."""
    @tb.tool("agent_test_echo", "эхо для тестов агента", {"x": {"type": "string"}})
    async def _echo(ctx, x="пинг"):
        return {"ok": True, "id": 7, "text": x}

    @tb.tool("agent_test_outbox", "кладёт вложения для тестов агента", {})
    async def _outbox(ctx):
        ctx.outbox.append(OutItem(kind="file", data=b"BEGIN:VCALENDAR", filename="calendar.ics", text="Календарь"))
        ctx.outbox.append(OutItem(kind="text", text="Черновик готов",
                                  buttons=[[("📨 Отправить", "draft:send:1")]]))
        return {"ok": True}

    yield
    for n in ("agent_test_echo", "agent_test_outbox"):
        tb.REGISTRY.pop(n, None)


# ── чистые функции ───────────────────────────────────────────────────────────
def test_merge_history():
    items = [("assistant", "хвост"), ("user", "x"), ("user", "y"), ("assistant", "b"), ("assistant", ""),
             ("assistant", "c"), ("user", "z")]
    assert merge_history(items) == [
        {"role": "user", "content": "x\n\ny"},
        {"role": "assistant", "content": "b\n\nc"},
        {"role": "user", "content": "z"},
    ]
    assert merge_history([("assistant", "a"), ("assistant", "b")]) == []
    assert merge_history([]) == []


def test_describe_action():
    ok = describe_action("create_reminder", json.dumps(
        {"ok": True, "id": 12, "text": "Позвонить  маме\nвечером", "when": "пн 29.09 07:30"}, ensure_ascii=False))
    assert ok.ok and ok.line == "create_reminder → ok #12 «Позвонить маме вечером» пн 29.09 07:30"
    err = describe_action("forget", json.dumps({"ok": False, "error": "факта #9 нет в памяти"}, ensure_ascii=False))
    assert not err.ok and err.line == "forget → ошибка: факта #9 нет в памяти"
    assert describe_action("list_reminders", '{"ok": true, "items": []}').line == "list_reminders → ok"
    assert describe_action("x", "не json…[обрезано]").line == "x → ok"
    assert describe_action("x", '{"ok": true, "id": true}').line == "x → ok"         # bool — не id
    assert describe_action("tg_draft_reply", '{"ok": true, "draft_id": 3}').line == "tg_draft_reply → ok #3"
    long = describe_action("save_idea", json.dumps({"ok": True, "id": 1, "title": "т" * 300}))
    assert len(long.line) < 90


def test_actions_text_capped():
    acts = [describe_action("save_idea", json.dumps({"ok": True, "id": i, "title": "идея " * 20}))
            for i in range(40)]
    s = actions_text(acts)
    assert s.startswith("[действия бота] save_idea → ok #0") and len(s) <= ACTIONS_MAX and s.endswith("…")


# ── handle: обычный ответ ────────────────────────────────────────────────────
async def test_init_registers_agent_and_loads_tools(ctx):
    agent = Agent(ctx)
    assert ctx.services.agent is agent
    assert agent.failed_modules == []
    assert {"remember", "create_reminder", "save_idea", "add_event"} <= set(tb.REGISTRY)


async def test_broken_tool_module_is_tolerated(ctx, monkeypatch):
    monkeypatch.setattr(tools_pkg, "MODULES", (*tools_pkg.MODULES, "no_such_module_xyz"))
    agent = Agent(ctx)
    assert agent.failed_modules == ["no_such_module_xyz"]


async def test_plain_answer_saved(ctx, fake_llm, db):
    fake_llm.script = ["  Привет. Чего хотел?  "]
    r = await Agent(ctx).handle("  привет ")
    assert isinstance(r, AgentReply)
    assert (r.text, r.outbox, r.deep, r.error) == ("Привет. Чего хотел?", [], False, None)
    rows = await db.fetchall("SELECT role, content, via FROM messages ORDER BY id")
    assert [(x["role"], x["content"], x["via"]) for x in rows] == [
        ("user", "привет", "text"), ("assistant", "Привет. Чего хотел?", "text")]
    call = fake_llm.calls[0]
    assert "СЕЙЧАС: 2026-09-28 09:00, понедельник" in system_of(call)
    assert call["messages"][1:] == [{"role": "user", "content": "привет"}]
    assert call["deep"] is False and "tool_choice" not in call
    names = {t["function"]["name"] for t in call["tools"]}
    assert {"remember", "create_reminder", "schedule_followup"} <= names
    assert len(fake_llm.calls) == 1


async def test_empty_text(ctx, fake_llm, db):
    r = await Agent(ctx).handle("   ")
    assert r.text == EMPTY_TEXT and r.outbox == [] and r.error is None
    assert fake_llm.calls == [] and await dialog(db) == []


async def test_voice_note_and_via(ctx, fake_llm, db):
    agent = Agent(ctx)
    fake_llm.script = ["ок", "ок"]
    await agent.handle("напомни купить хлеб", via="voice")
    assert VOICE_NOTE in system_of(fake_llm.calls[0])
    await agent.handle("и молоко", via="что-то странное")
    assert VOICE_NOTE not in system_of(fake_llm.calls[1])
    vias = [r["via"] for r in await db.fetchall("SELECT via FROM messages WHERE role='user' ORDER BY id")]
    assert vias == ["voice", "text"]


async def test_deep_mode_from_kv(ctx, fake_llm, db):
    agent = Agent(ctx)
    await db.kv_set("mode", "deep")
    fake_llm.script = ["глубоко", "быстро"]
    r = await agent.handle("подумай как следует")
    assert r.deep is True and fake_llm.calls[0]["deep"] is True
    assert "РЕЖИМ: глубокий" in system_of(fake_llm.calls[0])
    r = await agent.handle("а теперь коротко", deep=False)      # явный флаг сильнее kv
    assert r.deep is False and fake_llm.calls[1]["deep"] is False
    await db.kv_set("mode", "fast")
    fake_llm.script = ["x"]
    r = await agent.handle("/deep вопрос", deep=True)
    assert r.deep is True and fake_llm.calls[2]["deep"] is True


# ── история ──────────────────────────────────────────────────────────────────
async def test_history_merges_event_and_drops_leading_assistant(ctx, fake_llm, db):
    await db.add_message("assistant", "старый хвост")
    await db.add_message("user", "напомни через час выпить воды")
    await db.add_message("assistant", "Ок, напомню.")
    await db.add_message("event", "Сработало напоминание #3: вода", "system")
    fake_llm.script = ["Молодец."]
    await Agent(ctx).handle("выпил")
    msgs = fake_llm.calls[0]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[1]["content"] == "напомни через час выпить воды"
    assert msgs[3]["content"] == EVENT_PREFIX + "Сработало напоминание #3: вода\n\nвыпил"


async def test_history_window_and_summarized_excluded(ctx, fake_llm, db):
    agent = make_agent(ctx, history_messages=4)
    for role, text in [("user", "u1"), ("assistant", "a1"), ("user", "u2"), ("assistant", "a2"), ("user", "u3")]:
        await db.add_message(role, text)
    await db.execute("UPDATE messages SET summarized=1 WHERE content='a2'")
    fake_llm.script = ["ок"]
    await agent.handle("u4")
    msgs = fake_llm.calls[0]["messages"]
    # последние 4 несвёрнутых: a1, u2, u3, u4 → ведущий a1 отброшен, u3+u4 склеены
    assert msgs[1:] == [{"role": "user", "content": "u2\n\nu3\n\nu4"}]


# ── системный промпт ─────────────────────────────────────────────────────────
async def test_system_prompt_has_live_state(ctx, fake_llm, db):
    mem = importlib.import_module(MEM)
    await mem.add_fact(db, "Работает архитектором в Калининграде", "work")
    await mem.upsert_opinion(db, topic="его идея с кофейней", stance="Не взлетит без проходного трафика",
                             reasons="район спальный", confidence=70)
    await mem.add_journal(db, "Третий день откладывает звонок юристу.")
    for i in range(1, 5):
        await db.execute("INSERT INTO summaries(content, from_id, to_id, created_at) VALUES(?,?,?,?)",
                         (f"конспект {i}", i, i, "2026-09-27T00:00:00+00:00"))
    await db.execute(
        "INSERT INTO reminders(text, kind, local_start, tz, next_at, status, nag, nag_active, challenge, created_at) "
        "VALUES('Подъём', 'wake', '2026-09-28T07:00:00', 'Europe/Moscow', NULL, 'active', 1, 1, 1, "
        "'2026-09-27T00:00:00+00:00')")
    await db.execute(
        "INSERT INTO reminders(text, kind, local_start, tz, next_at, status, nag, nag_active, created_at) "
        "VALUES('Старый', 'reminder', '2026-09-27T07:00:00', 'Europe/Moscow', NULL, 'done', 1, 1, "
        "'2026-09-27T00:00:00+00:00')")
    await db.kv_set("owner_name", "Андрей")
    fake_llm.script = ["ок"]
    await Agent(ctx).handle("что думаешь про кофейню?")
    s = system_of(fake_llm.calls[0])
    assert "Работает архитектором в Калининграде" in s and "[work]" in s
    assert "Не взлетит без проходного трафика" in s and "уверенность 70%" in s
    assert "Третий день откладывает звонок юристу." in s
    assert "конспект 1" not in s and s.index("конспект 2") < s.index("конспект 3") < s.index("конспект 4")
    assert "#1 Подъём — будильник с задачкой" in s and "Старый" not in s
    assert "ВЛАДЕЛЕЦ: Андрей" in s and "личный ИИ владельца (Андрей)" in s
    assert VOICE_NOTE not in s and "РЕЖИМ: глубокий" not in s


async def test_owner_name_from_cfg_wins(ctx, fake_llm, db):
    await db.kv_set("owner_name", "Андрей")
    fake_llm.script = ["ок"]
    await make_agent(ctx, owner_name="Саша", bot_name="Балтия").handle("привет")
    s = system_of(fake_llm.calls[0])
    assert "ВЛАДЕЛЕЦ: Саша" in s and "Андрей" not in s and "Ты — Балтия" in s


async def test_summary_text_capped(ctx, db):
    agent = Agent(ctx)
    for ch in "ABC":
        await db.execute("INSERT INTO summaries(content, from_id, to_id, created_at) VALUES(?,?,?,?)",
                         (ch * 1400, 1, 1, "2026-09-27T00:00:00+00:00"))
    s = await agent._summary_text()
    assert s == "B" * 1400 + "\n\n" + "C" * 1400          # A не влез, порядок хронологический
    await db.execute("INSERT INTO summaries(content, from_id, to_id, created_at) VALUES(?,?,?,?)",
                     ("D" * 5000, 1, 1, "2026-09-27T00:00:00+00:00"))
    s = await agent._summary_text()
    assert len(s) == SUMMARY_MAX and s.startswith("DDD") and s.endswith("…")


async def test_broken_context_source_does_not_kill_reply(ctx, fake_llm, db, monkeypatch):
    mem = importlib.import_module(MEM)
    await mem.add_journal(db, "запись")

    def bad_facts(*a, **k):
        raise RuntimeError("индекс сломан")

    async def bad_journal(*a, **k):
        raise RuntimeError("нет таблицы")

    monkeypatch.setattr(mem, "facts_for_prompt", bad_facts)
    monkeypatch.setattr(mem, "latest_journal", bad_journal)
    fake_llm.script = ["живой"]
    r = await Agent(ctx).handle("ты тут?")
    assert r.text == "живой" and r.error is None
    s = system_of(fake_llm.calls[0])
    assert "пока почти ничего" in s and "ТВОЙ ДНЕВНИК" not in s


# ── цикл инструментов ────────────────────────────────────────────────────────
async def test_tool_call_remember_roundtrip(ctx, fake_llm, db):
    mem = importlib.import_module(MEM)
    fake_llm.script = [
        {"name": "remember", "args": {"content": "Жену зовут Маша", "category": "person"},
         "reasoning": "это стоит запомнить"},
        "Запомнил: жена — Маша.",
    ]
    r = await Agent(ctx).handle("мою жену зовут Маша")
    assert r.text == "Запомнил: жена — Маша." and r.error is None
    facts = await mem.all_facts(db)
    assert [(f["content"], f["category"], f["source"]) for f in facts] == [("Жену зовут Маша", "person", "chat")]
    rows = await dialog(db)
    assert [x[0] for x in rows] == ["user", "event", "assistant"]          # журнал — до ответа
    assert rows[1][1] == "[действия бота] remember → ok #1 «Жену зовут Маша»"
    second = fake_llm.calls[1]["messages"]
    asst, tool_msg = second[-2], second[-1]
    assert asst["role"] == "assistant" and asst["reasoning_content"] == "это стоит запомнить"
    assert asst["tool_calls"][0]["id"] == "call_0"
    assert asst["tool_calls"][0]["function"]["name"] == "remember"
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "call_0"
    assert json.loads(tool_msg["content"])["ok"] is True
    assert second[-3] == {"role": "user", "content": "мою жену зовут Маша"}


async def test_action_log_visible_in_next_turn(ctx, fake_llm, db):
    rem = importlib.import_module(REM)
    fake_llm.script = [
        {"name": "create_reminder", "args": {"text": "позвонить маме", "when": "2026-09-28 18:00"}},
        "Поставил на 18:00.",
        {"name": "cancel_reminder", "args": {"id": 1}},
        "Отменил.",
    ]
    agent = Agent(ctx)
    await agent.handle("напомни позвонить маме в 18")
    assert [r["text"] for r in await rem.list_active(db)] == ["позвонить маме"]
    r = await agent.handle("отмени его")
    assert r.text == "Отменил." and await rem.list_active(db) == []
    hist = fake_llm.calls[2]["messages"]
    assert hist[1:] == [
        {"role": "user", "content": "напомни позвонить маме в 18\n\n" + EVENT_PREFIX
         + "[действия бота] create_reminder → ok #1 «позвонить маме» пн 28.09 18:00"},
        {"role": "assistant", "content": "Поставил на 18:00."},
        {"role": "user", "content": "отмени его"},
    ]


async def test_several_calls_in_one_step(ctx, fake_llm, db):
    importlib.import_module(MEM)
    fake_llm.script = [
        {"calls": [{"name": "remember", "args": {"content": "Любит крепкий кофе", "category": "preference"}},
                   {"name": "remember", "args": {"content": "x"}}]},
        "Кофе запомнил, второе — пустое.",
    ]
    await Agent(ctx).handle("запомни про кофе")
    tail = fake_llm.calls[1]["messages"][-3:]
    assert [m["role"] for m in tail] == ["assistant", "tool", "tool"]
    assert [m["tool_call_id"] for m in tail[1:]] == ["call_0", "call_1"]
    assert json.loads(tail[1]["content"])["ok"] is True and json.loads(tail[2]["content"])["ok"] is False
    log_row = (await dialog(db))[1]
    assert log_row[0] == "event"
    assert log_row[1].startswith("[действия бота] remember → ok #1 «Любит крепкий кофе»; remember → ошибка: факт")


async def test_unknown_tool_error_passed_back(ctx, fake_llm, db):
    fake_llm.script = [{"name": "launch_rockets", "args": {"target": "луна"}}, "Такого не умею."]
    r = await Agent(ctx).handle("запусти ракеты")
    assert r.text == "Такого не умею."
    tool_msg = fake_llm.calls[1]["messages"][-1]
    res = json.loads(tool_msg["content"])
    assert tool_msg["role"] == "tool" and res["ok"] is False and "нет такого инструмента" in res["error"]
    assert (await dialog(db))[1] == ("event", "[действия бота] launch_rockets → ошибка: "
                                              "нет такого инструмента: launch_rockets")


async def test_bad_arguments_json_passed_back(ctx, fake_llm, test_tools):
    fake_llm.script = [LLMResponse(tool_calls=[ToolCall("c9", "agent_test_echo", "{oops")],
                                   finish_reason="tool_calls"),
                       "Повторю."]
    r = await Agent(ctx).handle("эхо")
    tool_msg = fake_llm.calls[1]["messages"][-1]
    assert tool_msg["tool_call_id"] == "c9" and "не JSON" in json.loads(tool_msg["content"])["error"]
    assert r.text == "Повторю."


async def test_dispatch_crash_is_contained(ctx, fake_llm, db, monkeypatch, test_tools):
    async def crash(*a, **k):
        raise RuntimeError("всё плохо")
    monkeypatch.setattr(tb, "dispatch", crash)
    fake_llm.script = [{"name": "agent_test_echo", "args": {"x": "a"}}, "Сломалось, но живу."]
    r = await Agent(ctx).handle("эхо")
    assert r.text == "Сломалось, но живу." and r.error is None
    res = json.loads(fake_llm.calls[1]["messages"][-1]["content"])
    assert res == {"ok": False, "error": "внутренняя ошибка: RuntimeError: всё плохо"}


async def test_max_steps_exhausted_forces_final_answer(ctx, fake_llm, db, test_tools):
    agent = make_agent(ctx, llm_max_steps=2)
    await db.kv_set("mode", "deep")
    fake_llm.script = [{"name": "agent_test_echo", "args": {"x": "1"}},
                       {"name": "agent_test_echo", "args": {"x": "2"}},
                       {"name": "agent_test_echo", "args": {"x": "3"}},      # проигнорирован: tool_choice none
                       ]
    fake_llm.script[2] = LLMResponse(content="Итог: эхо дважды.", finish_reason="stop")
    r = await agent.handle("эхо до упора")
    assert r.text == "Итог: эхо дважды." and r.deep is True
    assert len(fake_llm.calls) == 3
    assert "tool_choice" not in fake_llm.calls[0] and "tool_choice" not in fake_llm.calls[1]
    last = fake_llm.calls[2]
    assert last["tool_choice"] == "none" and last["tools"] and last["deep"] is True
    assert [m["role"] for m in last["messages"][-4:]] == ["assistant", "tool", "assistant", "tool"]
    log_row = (await dialog(db))[1][1]
    assert log_row == "[действия бота] agent_test_echo → ok #7 «1»; agent_test_echo → ok #7 «2»"


async def test_final_call_returning_tool_calls_is_ignored(ctx, fake_llm, db, test_tools):
    agent = make_agent(ctx, llm_max_steps=1)
    fake_llm.script = [{"name": "agent_test_echo", "args": {"x": "1"}},
                       {"name": "agent_test_echo", "args": {"x": "2"}}]   # модель упрямо зовёт инструмент
    r = await agent.handle("эхо")
    assert r.text == "Готово."                                             # инструмент был успешен
    assert await db.scalar("SELECT COUNT(*) FROM messages WHERE content LIKE '%«2»%'") == 0


async def test_empty_final_fallbacks(ctx, fake_llm, test_tools):
    agent = Agent(ctx)
    fake_llm.script = [{"name": "agent_test_echo", "args": {}}, "   "]
    assert (await agent.handle("раз")).text == "Готово."
    fake_llm.script = [""]
    assert (await agent.handle("два")).text == "…"
    fake_llm.script = [{"name": "nope"}, ""]
    assert (await agent.handle("три")).text == "…"                          # инструмент был, но с ошибкой


async def test_outbox_returned(ctx, fake_llm, test_tools):
    fake_llm.script = [{"name": "agent_test_outbox"}, "Вот файл."]
    r = await Agent(ctx).handle("дай календарь")
    assert r.text == "Вот файл." and [o.kind for o in r.outbox] == ["file", "text"]
    assert ctx.outbox == []                                                # общий контекст не засоряется


# ── ошибки ───────────────────────────────────────────────────────────────────
async def test_llm_error_reply_keeps_user_message(ctx, fake_llm, db):
    fake_llm.script = [boom()]
    r = await Agent(ctx).handle("привет")
    assert r.text == "⚠️ сервер модели сбоит (503)" and r.error == "сервер модели сбоит (503)"
    assert r.outbox == [] and r.deep is False
    assert await dialog(db) == [("user", "привет")]


async def test_llm_error_after_tool_keeps_actions_and_outbox(ctx, fake_llm, db, test_tools):
    fake_llm.script = [{"name": "agent_test_outbox"}, boom("модель не ответила вовремя (таймаут)")]
    r = await Agent(ctx).handle("сделай")
    assert r.text.startswith("⚠️ модель не ответила") and len(r.outbox) == 2
    assert await dialog(db) == [("user", "сделай"), ("event", "[действия бота] agent_test_outbox → ok")]


async def test_unexpected_exception_reply(ctx, fake_llm, db):
    fake_llm.script = [lambda m, kw: 1 / 0]
    r = await Agent(ctx).handle("привет")
    assert r.text == "⚠️ Что-то сломалось внутри: ZeroDivisionError. Детали в логе."
    assert r.error.startswith("ZeroDivisionError")
    assert await dialog(db) == [("user", "привет")]


async def test_turns_are_serialized(cfg, db, notifier, clock):
    class SlowLLM:
        def __init__(self):
            self.calls: list[list[dict]] = []
            self.gate = asyncio.Event()

        async def complete(self, messages, **kw):
            self.calls.append([dict(m) for m in messages])
            n = len(self.calls)
            if n == 1:
                await self.gate.wait()
            return LLMResponse(content=f"ответ {n}")

    llm = SlowLLM()
    agent = Agent(ToolContext(cfg=cfg, db=db, llm=llm, services=Services(notifier=notifier)))
    t1 = asyncio.create_task(agent.handle("первый"))
    t2 = None
    try:
        for _ in range(300):                       # база — в потоке aiosqlite, ждём по-настоящему
            if llm.calls:
                break
            await asyncio.sleep(0.01)
        assert len(llm.calls) == 1
        t2 = asyncio.create_task(agent.handle("второй"))
        await asyncio.sleep(0.2)
        assert len(llm.calls) == 1                 # второй ждёт, пока первый не ответит
        assert await db.scalar("SELECT COUNT(*) FROM messages WHERE content='второй'") == 0
    finally:
        llm.gate.set()
    r1, r2 = await asyncio.gather(t1, t2)
    assert (r1.text, r2.text) == ("ответ 1", "ответ 2")
    assert llm.calls[1][1:] == [{"role": "user", "content": "первый"},
                                {"role": "assistant", "content": "ответ 1"},
                                {"role": "user", "content": "второй"}]


# ── бот пишет первым ─────────────────────────────────────────────────────────
async def test_proactive_sends_outbox_and_saves_reply(ctx, fake_llm, db, notifier, test_tools):
    await db.add_message("user", "резюме отправлю завтра")
    await db.add_message("assistant", "Смотри, проверю.")
    fake_llm.script = [{"name": "agent_test_outbox"}, "Ну что, отправил резюме?"]
    text = await Agent(ctx).proactive("спросить, отправил ли резюме")
    assert text == "Ну что, отправил резюме?"
    call = fake_llm.calls[0]
    assert PROACTIVE_NOTE in system_of(call) and call["deep"] is False and call["tools"]
    assert call["messages"][-1] == {
        "role": "user",
        "content": "[внутренний триггер] спросить, отправил ли резюме. Напиши ему первым: коротко, по делу, "
                   "в своём стиле. Если повод потерял смысл — так и скажи одной фразой."}
    assert call["messages"][-2] == {"role": "assistant", "content": "Смотри, проверю."}
    assert [(s["kind"], s.get("filename"), s["text"]) for s in notifier.sent] == [
        ("file", "calendar.ics", "Календарь"), ("text", None, "Черновик готов")]
    assert notifier.sent[1]["buttons"] == [[("📨 Отправить", "draft:send:1")]]
    rows = await db.fetchall("SELECT role, content, via FROM messages ORDER BY id")
    assert [(r["role"], r["via"]) for r in rows[2:]] == [("event", "system"), ("assistant", "system")]
    assert rows[-1]["content"] == "Ну что, отправил резюме?"


async def test_proactive_empty_history_and_punctuation(ctx, fake_llm, db):
    fake_llm.script = ["Как там юрист?"]
    assert await Agent(ctx).proactive("  спросить про юриста?  ") == "Как там юрист?"
    msgs = fake_llm.calls[0]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert msgs[1]["content"].startswith("[внутренний триггер] спросить про юриста? Напиши ему первым")


async def test_proactive_without_notifier(ctx, fake_llm, db, test_tools):
    ctx.services.notifier = None
    fake_llm.script = [{"name": "agent_test_outbox"}, "Держи."]
    assert await Agent(ctx).proactive("событие") == "Держи."


async def test_proactive_notifier_failure_is_not_fatal(ctx, fake_llm, db, notifier, test_tools, monkeypatch):
    async def broken(*a, **k):
        raise RuntimeError("telegram лежит")
    monkeypatch.setattr(notifier, "send_file", broken)
    fake_llm.script = [{"name": "agent_test_outbox"}, "Держи."]
    assert await Agent(ctx).proactive("событие") == "Держи."
    assert [s["kind"] for s in notifier.sent] == ["text"]


async def test_proactive_llm_error_raises(ctx, fake_llm, db):
    fake_llm.script = [boom()]
    with pytest.raises(LLMError):
        await Agent(ctx).proactive("спросить про резюме")
    assert await dialog(db) == []


# ── конспект, сброс ──────────────────────────────────────────────────────────
async def _fill(db, n: int) -> None:
    for i in range(1, n + 1):
        role = ("user", "assistant", "event")[(i - 1) % 3]
        await db.add_message(role, f"m{i}", "system" if role == "event" else "text")


async def test_summarize_old(ctx, fake_llm, db):
    agent = make_agent(ctx, history_messages=4, summary_chunk=4)
    await _fill(db, 10)
    fake_llm.script = ["- решили X\n- он устал"]
    assert await agent.summarize_old() is True
    s = await db.fetchall("SELECT * FROM summaries")
    assert len(s) == 1 and (s[0]["from_id"], s[0]["to_id"]) == (1, 4)
    assert s[0]["content"] == "[пн 28.09 09:00 — пн 28.09 09:00]\n- решили X\n- он устал"
    flags = [r["summarized"] for r in await db.fetchall("SELECT summarized FROM messages ORDER BY id")]
    assert flags == [1, 1, 1, 1, 0, 0, 0, 0, 0, 0]
    call = fake_llm.calls[0]
    assert call["messages"][0]["content"] == SUMMARY_SYSTEM and call["deep"] is False
    user = call["messages"][1]["content"]
    assert user.splitlines() == ["[пн 28.09 09:00] Владелец: m1", "[пн 28.09 09:00] Ты: m2",
                                 "[пн 28.09 09:00] Событие: m3", "[пн 28.09 09:00] Владелец: m4"]
    # осталось 6 ≤ 4 + 4 — сворачивать нечего, модель не зовём
    assert await agent.summarize_old() is False and len(fake_llm.calls) == 1
    # конспект попадает в системный промпт
    fake_llm.script = ["ок"]
    await agent.handle("что решили?")
    assert "- решили X" in system_of(fake_llm.calls[1])


async def test_summarize_old_llm_error(ctx, fake_llm, db):
    agent = make_agent(ctx, history_messages=4, summary_chunk=4)
    await _fill(db, 10)
    fake_llm.script = [boom()]
    assert await agent.summarize_old() is False
    assert await db.scalar("SELECT COUNT(*) FROM summaries") == 0
    assert await db.scalar("SELECT COUNT(*) FROM messages WHERE summarized=1") == 0


async def test_handle_spawns_summarize(ctx, fake_llm, db):
    agent = make_agent(ctx, history_messages=4, summary_chunk=4)
    await _fill(db, 8)
    fake_llm.script = ["ответ", "- конспект"]
    r = await agent.handle("ещё")                   # 10 несвёрнутых > 8
    assert r.text == "ответ"
    await ctx.services.drain()
    assert await db.scalar("SELECT COUNT(*) FROM summaries") == 1
    assert fake_llm.calls[1]["messages"][0]["content"] == SUMMARY_SYSTEM


async def test_handle_below_threshold_does_not_summarize(ctx, fake_llm, db):
    agent = make_agent(ctx, history_messages=4, summary_chunk=4)
    await _fill(db, 5)
    fake_llm.script = ["ответ"]
    await agent.handle("ещё")                       # 7 несвёрнутых ≤ 8
    await ctx.services.drain()
    assert len(fake_llm.calls) == 1 and await db.scalar("SELECT COUNT(*) FROM summaries") == 0


async def test_reset_context(ctx, fake_llm, db):
    mem = importlib.import_module(MEM)
    await mem.add_fact(db, "Жену зовут Маша", "person")
    await _fill(db, 3)
    agent = Agent(ctx)
    assert await agent.reset_context() == 3
    assert await db.recent_messages(10) == []
    assert await agent.reset_context() == 0
    assert len(await mem.all_facts(db)) == 1
    fake_llm.script = ["С чистого листа."]
    await agent.handle("начнём заново")
    assert fake_llm.calls[0]["messages"][1:] == [{"role": "user", "content": "начнём заново"}]


# ── ночная рефлексия ─────────────────────────────────────────────────────────
async def test_reflect_persists_everything(ctx, fake_llm, db):
    mem = importlib.import_module(MEM)
    rem = importlib.import_module(REM)
    await db.add_message("user", "опять не позвонил юристу, завтра точно")
    await db.add_message("assistant", "Ты это третий день говоришь.")
    await mem.add_fact(db, "Жену зовут Маша", "person")
    op = await mem.upsert_opinion(db, topic="его привычка откладывать", stance="Это страх, а не лень",
                                  confidence=60)
    data = {
        "journal": "Третий день тянет с юристом. Завтра спрошу прямо, что мешает.",
        "facts": [{"content": "Ищет юриста по договору аренды", "category": "work"},
                  {"content": "x", "category": "work"},                       # слишком короткий
                  "Боится конфликтов с арендодателем"],
        "opinions": [{"topic": "его привычка откладывать", "stance": "Это уже привычка, а не страх",
                      "reasons": "третий раз подряд", "confidence": 75, "opinion_id": op["id"],
                      "why_changed": "повторяется без внешних причин"},
                     {"topic": "", "stance": "пустая тема"},                  # невалидная
                     {"topic": "кофейня", "stance": "Не взлетит", "confidence": "80%"}],
        "followups": [{"when": "2026-09-29 12:00", "about": "спросить, позвонил ли юристу"},
                      {"when": "2026-09-27 12:00", "about": "это уже в прошлом"}],
    }
    fake_llm.script = [json.dumps(data, ensure_ascii=False)]
    out = await Agent(ctx).reflect()
    assert out == data["journal"]
    assert (await mem.latest_journal(db))["content"] == data["journal"]

    facts = {f["content"]: f for f in await mem.all_facts(db)}
    assert set(facts) == {"Жену зовут Маша", "Ищет юриста по договору аренды", "Боится конфликтов с арендодателем"}
    assert facts["Ищет юриста по договору аренды"]["source"] == "reflection"
    assert facts["Ищет юриста по договору аренды"]["category"] == "work"

    changed = await mem.get_opinion(db, op["id"])
    assert changed["stance"] == "Это уже привычка, а не страх" and changed["confidence"] == 75
    assert json.loads(changed["history"])[-1]["why_changed"] == "повторяется без внешних причин"
    new_op = await mem.find_opinion(db, "кофейня")
    assert new_op is not None and new_op["confidence"] == 80
    assert await db.scalar("SELECT COUNT(*) FROM opinions") == 2

    rows = await rem.list_active(db)
    assert [(r["text"], r["kind"], r["next_at"]) for r in rows] == [
        ("спросить, позвонил ли юристу", "followup", "2026-09-29T09:00:00+00:00")]

    call = fake_llm.calls[0]
    assert call["deep"] is True and call["json_mode"] is True
    system, user = call["messages"][0]["content"], call["messages"][1]["content"]
    assert "Ты — Оракул, личный ИИ-напарник своего владельца." in system and '"followups"' in system and "до 1200 знаков" in system
    assert "СЕЙЧАС: 2026-09-28 09:00, понедельник" in user
    assert "Владелец: опять не позвонил юристу" in user and "Я: Ты это третий день говоришь." in user
    assert "#1 [person] Жену зовут Маша" in user and "Это страх, а не лень" in user


async def test_reflect_material_projects_ideas_followups(ctx, fake_llm, db):
    importlib.import_module(PRJ)
    rem = importlib.import_module(REM)
    importlib.import_module(MEM)
    now = "2026-09-27T10:00:00+00:00"
    await db.add_message("user", "как дела с сайтом?")
    await db.execute("INSERT INTO projects(name, goal, status, created_at, updated_at) VALUES(?,?,?,?,?)",
                     ("Сайт студии", "запуск до ноября", "active", now, now))
    await db.execute("INSERT INTO projects(name, status, created_at, updated_at) VALUES(?,?,?,?)",
                     ("Старый проект", "done", now, now))
    await db.execute("INSERT INTO tasks(project_id, text, status, due_at, created_at) VALUES(1,?,?,?,?)",
                     ("сверстать главную", "todo", "2026-09-27T09:00:00+00:00", now))
    await db.execute("INSERT INTO tasks(project_id, text, status, created_at) VALUES(1,?,?,?)",
                     ("купить домен", "doing", now))
    await db.execute("INSERT INTO ideas(title, content, score, status, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                     ("Кофейня у вокзала", "…", 6, "new", now, now))
    await db.execute("INSERT INTO ideas(title, content, status, created_at, updated_at) VALUES(?,?,?,?,?)",
                     ("Брошенная идея", "…", "dropped", now, now))
    await rem.create_reminder(ctx, text="спросить про сайт", when="2026-09-30 12:00", kind="followup")
    fake_llm.script = [json.dumps({"journal": "Сайт буксует.", "facts": [], "opinions": [],
                                   "followups": [{"when": "2026-09-29 12:00", "about": "Спросить  про сайт"},
                                                 {"when": "2026-09-29 13:00", "about": "второй"},
                                                 {"when": "2026-09-29 14:00", "about": "третий — лишний"}]},
                                  ensure_ascii=False)]
    assert await Agent(ctx).reflect() == "Сайт буксует."
    user = fake_llm.calls[0]["messages"][1]["content"]
    assert "#1 Сайт студии (активен, открытых задач: 2) — цель: запуск до ноября" in user
    assert "Старый проект" not in user
    assert "сверстать главную — срок вс 27.09 12:00 (ПРОСРОЧЕНО) [проект: Сайт студии]" in user
    assert "Кофейня у вокзала · 6/10 · new" in user and "Брошенная идея" not in user
    assert "УЖЕ СТОЯТ МОИ FOLLOW-UP'Ы:\n- #1 ср 30.09 12:00 · спросить про сайт" in user
    texts = sorted(r["text"] for r in await rem.list_active(db))
    assert texts == ["второй", "спросить про сайт"]          # дубль и третий (сверх лимита) не поставлены


async def test_reflect_none_without_user_messages_today(ctx, fake_llm, db, clock):
    assert await Agent(ctx).reflect() is None
    await db.add_message("user", "вчерашнее")
    clock.advance(hours=25)
    await db.add_message("assistant", "ответ бота")
    await db.add_message("event", "Сработало напоминание #1", "system")
    assert await Agent(ctx).reflect() is None
    assert fake_llm.calls == []


async def test_reflect_llm_error_and_bad_json(ctx, fake_llm, db):
    await db.add_message("user", "день был")
    agent = Agent(ctx)
    fake_llm.script = [boom()]
    assert await agent.reflect() is None
    fake_llm.script = ["это вообще не json"]
    assert await agent.reflect() is None
    fake_llm.script = ["[1, 2, 3]"]
    assert await agent.reflect() is None
    assert await db.scalar("SELECT COUNT(*) FROM journal") == 0


async def test_reflect_survives_weird_shapes(ctx, fake_llm, db):
    mem = importlib.import_module(MEM)
    await db.add_message("user", "день был")
    fake_llm.script = [json.dumps({"journal": ["строка 1", "строка 2"], "facts": "не список",
                                   "opinions": [42, None], "followups": {"when": 5, "about": "x"}},
                                  ensure_ascii=False)]
    assert await Agent(ctx).reflect() == "строка 1\nстрока 2"
    assert (await mem.latest_journal(db))["content"] == "строка 1\nстрока 2"
    assert await db.scalar("SELECT COUNT(*) FROM facts") == 0
    assert await db.scalar("SELECT COUNT(*) FROM reminders") == 0


async def test_reflect_empty_journal_returns_none_but_keeps_facts(ctx, fake_llm, db):
    mem = importlib.import_module(MEM)
    await db.add_message("user", "у меня теперь собака Бублик")
    fake_llm.script = [json.dumps({"journal": "", "facts": [{"content": "Есть собака Бублик", "category": "person"}]},
                                  ensure_ascii=False)]
    assert await Agent(ctx).reflect() is None
    assert [f["content"] for f in await mem.all_facts(db)] == ["Есть собака Бублик"]
    assert await mem.latest_journal(db) is None


async def test_missing_memory_module_degrades_quietly(ctx, fake_llm, db, monkeypatch):
    import oracle.agent as ag
    real = ag._module
    monkeypatch.setattr(ag, "_module", lambda name: None if name == "memory" else real(name))
    fake_llm.script = ["живой", json.dumps({"journal": "запись", "facts": [{"content": "Любит чай"}]},
                                           ensure_ascii=False)]
    agent = Agent(ctx)
    r = await agent.handle("привет")
    assert r.text == "живой" and "пока почти ничего" in system_of(fake_llm.calls[0])
    assert await agent.reflect() is None                  # дневник писать некуда
    assert await db.scalar("SELECT COUNT(*) FROM facts") == 0


async def test_reflect_owner_name_in_prompt(ctx, fake_llm, db):
    await db.kv_set("owner_name", "Андрей")
    await db.add_message("user", "день был")
    fake_llm.script = ['{"journal": "ок"}']
    assert await Agent(ctx).reflect() == "ок"
    assert "личный ИИ-напарник своего владельца (Андрей)." in fake_llm.calls[0]["messages"][0]["content"]
