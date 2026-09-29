"""Несколько людей в одном боте: запрос доступа, «Пустить» главным, раздельные пространства, /users, «Убрать»."""
from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest_asyncio
from aiogram.types import CallbackQuery, Chat, Message, User

from oracle import app
from oracle.agent import Agent
from oracle.bot.admin import parse_user_cb, users_text
from oracle.bot.handlers import Deps
from oracle.bot.middleware import ACCESS_TEXT, STRANGER_TEXT
from oracle.bot.notifier import BotNotifier
from oracle.tenants import WELCOME, Tenant, Tenants

from test_bot import RecordingSession

UTC = timezone.utc
MAIN, SECOND, THIRD = 42, 77, 88
_ids = itertools.count(1000)


def _user(uid: int) -> User:
    return User(id=uid, is_bot=False, first_name={MAIN: "Главный", SECOND: "Маша", THIRD: "Петя"}.get(uid, "Кто-то"))


def _msg(text: str, uid: int) -> Message:
    return Message(message_id=next(_ids), date=datetime(2026, 9, 28, 6, 0, tzinfo=UTC),
                   chat=Chat(id=uid, type="private"), from_user=_user(uid), text=text)


def _cb(data: str, uid: int) -> CallbackQuery:
    return CallbackQuery(id=str(next(_ids)), from_user=_user(uid), chat_instance="ci", data=data,
                         message=Message(message_id=next(_ids), date=datetime(2026, 9, 28, 6, 0, tzinfo=UTC),
                                         chat=Chat(id=uid, type="private"), text="x"))


def _sent(reqs: list, chat: int | None = None) -> list[str]:
    return [r.text for r in reqs if type(r).__name__ in ("SendMessage", "EditMessageText")
            and (chat is None or int(getattr(r, "chat_id", 0) or 0) == chat)]


@pytest_asyncio.fixture
async def multi(cfg, db, ctx, fake_llm):
    from aiogram import Bot
    from aiogram.types import Update

    quiet = dict(morning_brief_time="", news_digest_time="", reflection_time="", birthday_time="",
                 allow_requests=True)
    cfg = replace(cfg, **quiet)
    ctx.cfg = cfg
    session = RecordingSession()
    bot = Bot("123456:TEST-token", session=session)
    notifier = BotNotifier(bot, MAIN)
    ctx.services.notifier = notifier
    agent = Agent(ctx)
    deps = Deps(cfg=cfg, db=db, llm=fake_llm, ctx=ctx, agent=agent, notifier=notifier)
    tenants = Tenants(cfg, bot, llm=fake_llm,
                      primary=Tenant(uid=MAIN, cfg=cfg, db=db, ctx=ctx, deps=deps, agent=agent))
    await tenants.load()
    dp = app.build_dispatcher(cfg, deps, tenants)
    counter = itertools.count(1)

    async def feed(**kw: Any) -> list:
        before = len(session.requests)
        await dp.feed_update(bot, Update(update_id=next(counter), **kw))
        return session.requests[before:]

    yield SimpleNamespace(feed=feed, tenants=tenants, session=session, db=db, cfg=cfg)
    await tenants.stop_all()


async def test_access_request_approve_and_separate_spaces(multi, fake_llm, tmp_path):
    # чужой: /start → просьба уходит главному с кнопками; обычный текст — не к агенту
    reqs = await multi.feed(message=_msg("/start", SECOND))
    assert _sent(reqs, SECOND) == [ACCESS_TEXT["sent"]]
    ask = [r for r in reqs if type(r).__name__ == "SendMessage" and r.chat_id == MAIN]
    assert ask and "просится" in ask[0].text and "Маша" in ask[0].text
    datas = [b.callback_data for row in ask[0].reply_markup.inline_keyboard for b in row]
    assert datas == [f"user:add:{SECOND}", f"user:deny:{SECOND}"]
    reqs = await multi.feed(message=_msg("привет", SECOND))
    assert fake_llm.calls == [] and MAIN not in [getattr(r, "chat_id", None) for r in reqs]

    # главный пускает → второму приветствие, у него своя база
    reqs = await multi.feed(callback_query=_cb(f"user:add:{SECOND}", MAIN))
    assert multi.tenants.allowed(SECOND)
    assert WELCOME in _sent(reqs, SECOND)
    second = multi.tenants.tenant(SECOND)
    assert await second.db.kv_get("owner_name") == "Маша"          # имя из запроса — в его пространство
    assert second.cfg.db_path == multi.cfg.data_dir / "users" / str(SECOND) / "oracle.db"
    assert second.cfg.userbot_enabled is False and second.db is not multi.db

    # второй рассказывает о себе → факт в ЕГО базе, у главного — пусто
    fake_llm.script = [{"name": "remember", "args": {"content": "Любит зелёный чай", "category": "preference"}},
                       "Запомнила."]
    reqs = await multi.feed(message=_msg("запомни: я люблю зелёный чай", SECOND))
    assert "Запомнила." in _sent(reqs, SECOND)
    assert [f["content"] for f in await second.db.fetchall("SELECT content FROM facts")] == ["Любит зелёный чай"]
    assert await multi.db.fetchall("SELECT content FROM facts") == []

    # главный спрашивает — в его промпте нет ничего из пространства второго
    fake_llm.script = ["Пока почти ничего."]
    reqs = await multi.feed(message=_msg("что ты обо мне знаешь?", MAIN))
    system = fake_llm.calls[-1]["messages"][0]["content"]
    assert "зелёный чай" not in system.lower() and "Пока почти ничего." in _sent(reqs, MAIN)
    history = " ".join(m["content"] for m in fake_llm.calls[-1]["messages"][1:])
    assert "зелёный чай" not in history.lower()


async def test_second_user_cannot_manage_people(multi):
    await multi.tenants.approve(SECOND)
    reqs = await multi.feed(callback_query=_cb(f"user:add:{THIRD}", SECOND))
    assert not multi.tenants.allowed(THIRD)
    assert any(type(r).__name__ == "AnswerCallbackQuery" for r in reqs)
    reqs = await multi.feed(message=_msg("/users", SECOND))
    assert not any("Кто пользуется ботом" in t for t in _sent(reqs))


async def test_users_list_and_remove(multi, fake_llm):
    await multi.tenants.approve(SECOND)
    reqs = await multi.feed(message=_msg("/users", MAIN))
    text = _sent(reqs, MAIN)[-1]
    assert "Кто пользуется ботом" in text and str(SECOND) in text and "ты" in text
    reqs = await multi.feed(callback_query=_cb(f"user:del:{SECOND}", MAIN))
    assert not multi.tenants.allowed(SECOND) and SECOND in multi.tenants.dormant
    fake_llm.script = []
    await multi.feed(message=_msg("ты тут?", SECOND))
    assert fake_llm.calls == []                             # агент второго больше не зовётся
    # пустить снова — то же пространство, данные на месте
    await multi.tenants.approve(SECOND)
    assert multi.tenants.allowed(SECOND) and SECOND not in multi.tenants.dormant


async def test_deny_is_remembered(multi):
    await multi.feed(message=_msg("/start", THIRD))
    await multi.feed(callback_query=_cb(f"user:deny:{THIRD}", MAIN))
    reqs = await multi.feed(message=_msg("/start", THIRD))
    assert _sent(reqs, THIRD) == [STRANGER_TEXT]
    assert not [r for r in reqs if getattr(r, "chat_id", None) == MAIN]      # главного не дёргаем


async def test_owner_list_from_env_opens_spaces(cfg, db, ctx, fake_llm):
    from aiogram import Bot
    c = replace(cfg, owner_id=MAIN, owner_ids=(MAIN, SECOND), morning_brief_time="", news_digest_time="",
                reflection_time="", birthday_time="")
    ctx.cfg = c
    bot = Bot("123456:TEST-token", session=RecordingSession())
    agent = Agent(ctx)
    deps = Deps(cfg=c, db=db, llm=fake_llm, ctx=ctx, agent=agent)
    t = Tenants(c, bot, llm=fake_llm, primary=Tenant(uid=MAIN, cfg=c, db=db, ctx=ctx, deps=deps, agent=agent))
    await t.load()
    try:
        assert t.allowed(SECOND)
        try:
            await t.remove(SECOND)
            assert False, "вписанного в .env кнопкой не убрать"
        except ValueError as e:
            assert "OWNER_ID" in str(e)
    finally:
        await t.stop_all()


def test_parse_and_render_helpers():
    assert parse_user_cb("user:add:77") == ("add", 77)
    assert parse_user_cb("user:nope:77") is None and parse_user_cb("user:add:x") is None
    assert parse_user_cb("user:add:-5") is None
    text, buttons = users_text([{"uid": 42, "name": "Я", "primary": True, "static": True},
                                {"uid": 77, "name": "Маша <b>", "primary": False, "static": False}])
    assert "&lt;b&gt;" in text and buttons == [[("✖️ Убрать Маша <b>", "user:del:77")]]


async def test_idea_deep_button_works_for_owner_and_second(multi, fake_llm):
    """«🧠 Додумать глубоко» у главного и у второго — свой разбор, а не «Нет доступа»."""
    from oracle.bot.middleware import NO_ACCESS
    from oracle.tools import ideas
    await multi.tenants.approve(SECOND)
    for uid in (MAIN, SECOND):
        t = multi.tenants.tenant(uid)
        idea = await ideas.t_save_idea(t.ctx, title=f"Идея {uid}", content="кофейня у вокзала",
                                     evaluation="норм", score=5)
        fake_llm.script = ["Разбор. Оценка: 6/10"]
        reqs = await multi.feed(callback_query=_cb(f"idea:deep:{idea['id']}", uid))
        answers = [r.text for r in reqs if type(r).__name__ == "AnswerCallbackQuery"]
        assert answers and NO_ACCESS not in answers, answers
        await t.ctx.services.drain(5)
        row = await t.db.fetchone("SELECT deep_evaluation FROM ideas WHERE id=?", (idea["id"],))
        assert "Оценка: 6/10" in row["deep_evaluation"]


async def test_requests_off_by_default(cfg, db, ctx, fake_llm):
    """По умолчанию (ALLOW_REQUESTS=0) чужой /start — «Это личный бот», главного не дёргаем."""
    from aiogram import Bot
    from aiogram.types import Update
    c = replace(cfg, morning_brief_time="", news_digest_time="", reflection_time="", birthday_time="")
    assert c.allow_requests is False
    ctx.cfg = c
    session = RecordingSession()
    bot = Bot("123456:TEST-token", session=session)
    agent = Agent(ctx)
    deps = Deps(cfg=c, db=db, llm=fake_llm, ctx=ctx, agent=agent, notifier=BotNotifier(bot, MAIN))
    dp = app.build_dispatcher(c, deps)                    # один человек: без пространств
    await dp.feed_update(bot, Update(update_id=1, message=_msg("/start", SECOND)))
    assert _sent(session.requests, SECOND) == [STRANGER_TEXT]
    assert not [r for r in session.requests if getattr(r, "chat_id", None) == MAIN]
