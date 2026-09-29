"""Telegram-слой: кнопки, отправитель, «только владелец», разбор callback_data, будильная задачка,
обработчики команд/сообщений/кнопок, сборка роутера и приложения. Сети нет: бот — фейк."""
from __future__ import annotations

import asyncio
import importlib
import itertools
import json
import random
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import (Audio, BufferedInputFile, CallbackQuery, Chat, File, InlineKeyboardMarkup, Message,
                           User, VideoNote, Voice)

from oracle.bot import handlers as H
from oracle.bot import keyboards
from oracle.bot.handlers import Deps, Handlers, build_router, challenge_buttons, make_challenge, parse_cb
from oracle.bot.middleware import NO_ACCESS, STRANGER_TEXT, OwnerOnly, id_text, is_owner, is_start
from oracle.bot.notifier import BotNotifier
from oracle.tools.base import OutItem

OWNER = 42
UTC = timezone.utc


# ── фейки ────────────────────────────────────────────────────────────────────
class FakeBot:
    """Записывает вызовы методов бота. fail[name] — очередь исключений для этого метода."""

    def __init__(self, file_bytes: bytes = b"OggS-audio") -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, list[Exception]] = {}
        self.file_bytes = file_bytes
        self._ids = itertools.count(1000)

    async def _rec(self, name: str, kw: dict) -> Any:
        self.calls.append((name, kw))
        q = self.fail.get(name)
        if q:
            raise q.pop(0)
        return SimpleNamespace(message_id=next(self._ids))

    async def send_message(self, **kw: Any) -> Any:
        return await self._rec("send_message", kw)

    async def send_document(self, **kw: Any) -> Any:
        return await self._rec("send_document", kw)

    async def send_voice(self, **kw: Any) -> Any:
        return await self._rec("send_voice", kw)

    async def send_audio(self, **kw: Any) -> Any:
        return await self._rec("send_audio", kw)

    async def send_chat_action(self, **kw: Any) -> Any:
        return await self._rec("send_chat_action", kw)

    async def edit_message_reply_markup(self, **kw: Any) -> Any:
        return await self._rec("edit_message_reply_markup", kw)

    async def edit_message_text(self, **kw: Any) -> Any:
        return await self._rec("edit_message_text", kw)

    async def download(self, file: Any, destination: Any = None, **kw: Any) -> Any:
        self.calls.append(("download", {"file": file}))
        destination.write(self.file_bytes)
        return destination

    async def __call__(self, method: Any, request_timeout: Any = None) -> Any:
        """Сюда приходят message.answer(...) / query.answer(...) у объектов aiogram, привязанных к боту."""
        self.calls.append((type(method).__name__, {"method": method}))
        return True

    def named(self, name: str) -> list[dict]:
        return [kw for n, kw in self.calls if n == name]


def bad_request(text: str = "Bad Request: can't parse entities") -> TelegramBadRequest:
    return TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message=text)


class NotifierWithHtml:
    """FakeNotifier из conftest + send_html (как у BotNotifier)."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.html: list[str] = []

    async def send(self, text: str, buttons: Any = None, *, silent: bool = False) -> Any:
        return await self.inner.send(text, buttons, silent=silent)

    async def send_html(self, html: str, buttons: Any = None, *, silent: bool = False) -> Any:
        self.html.append(html)
        return 1

    async def send_file(self, data: bytes, filename: str, caption: str = "") -> None:
        await self.inner.send_file(data, filename, caption)

    async def send_voice(self, data: bytes, filename: str = "voice.mp3", caption: str = "") -> None:
        await self.inner.send_voice(data, filename, caption)


@dataclass
class Reply:
    text: str
    outbox: list = field(default_factory=list)
    deep: bool = False
    error: str | None = None


class FakeAgent:
    def __init__(self, reply: Reply | None = None) -> None:
        self.reply = reply or Reply("Ответ агента **жирно**")
        self.calls: list[tuple[str, str, Any]] = []
        self.failed_modules: list[str] = []

    async def handle(self, text: str, *, via: str = "text", deep: bool | None = None, **_kw: Any) -> Reply:
        self.calls.append((text, via, deep))
        return self.reply

    async def reset_context(self) -> int:
        return 7


class FakeSTT:
    reason = ""

    def __init__(self, text: str = "напомни завтра в девять", ok: bool = True, error: Exception | None = None):
        self.text, self.ok, self.error = text, ok, error
        self.calls: list[tuple[bytes, str, str]] = []

    def available(self) -> bool:
        return self.ok

    def describe(self) -> str:
        return "Groq (тест)"

    async def transcribe(self, data: bytes, filename: str = "voice.ogg", mime: str = "audio/ogg") -> str:
        self.calls.append((data, filename, mime))
        if self.error:
            raise self.error
        return self.text


class FakeTTS:
    def __init__(self, ok: bool = True, fail: bool = False) -> None:
        self.ok, self.fail = ok, fail
        self.calls: list[str] = []

    def available(self) -> bool:
        return self.ok

    async def synth(self, text: str) -> bytes:
        self.calls.append(text)
        if self.fail:
            raise RuntimeError("edge-tts упал")
        return b"ID3-mp3"


class FakeUserbot:
    def __init__(self, ready: bool = True) -> None:
        self.ready = ready
        self.sent: list[tuple[int, str]] = []
        self.resolved: list[tuple[Any, dict]] = []

    async def dialogs(self, limit: int = 20, unread_only: bool = False) -> list[dict]:
        return [{"id": 555, "title": "Маша", "unread": 2, "last_text": "ты где?", "last_out": False,
                 "last_date": "пн 28.09 08:50", "kind": "user"},
                {"id": -100123, "title": "Работа", "unread": 5, "last_text": "созвон в 11", "last_out": False,
                 "last_date": "пн 28.09 08:40", "kind": "group"}]

    async def resolve(self, query: Any, **kw: Any) -> tuple[int, str]:
        self.resolved.append((query, kw))
        if str(query).lstrip("@") == "masha_k":
            return 555, "Маша"
        if isinstance(query, int) or str(query).lstrip("-").isdigit():
            return int(query), "Маша"
        raise ValueError(f"не нашёл чат «{query}»")

    async def history(self, chat: Any, limit: int = 30) -> list[dict]:
        return [{"out": False, "sender": "Маша", "text": "ты где?", "date": "08:50"}]

    async def style_samples(self, chat: Any = None, limit: int = 25) -> list[str]:
        return ["ок", "ща буду"]

    async def send(self, chat: int, text: str) -> bool:
        self.sent.append((chat, text))
        return True


_ids = itertools.count(1)


def user(uid: int = OWNER) -> User:
    return User(id=uid, is_bot=False, first_name="Олег")


def msg(bot: FakeBot, text: str | None = None, uid: int = OWNER, **kw: Any) -> Message:
    return Message(message_id=next(_ids), date=datetime(2026, 9, 28, 6, 0, tzinfo=UTC),
                   chat=Chat(id=uid, type="private"), from_user=user(uid), text=text, **kw).as_(bot)


def cbq(bot: FakeBot, data: str, uid: int = OWNER, markup: InlineKeyboardMarkup | None = None,
        text: str = "сообщение с кнопками", chat_type: str = "private") -> CallbackQuery:
    m = Message(message_id=next(_ids), date=datetime(2026, 9, 28, 6, 0, tzinfo=UTC),
                chat=Chat(id=uid, type=chat_type), from_user=User(id=1, is_bot=True, first_name="bot"),
                text=text, reply_markup=markup)
    return CallbackQuery(id=str(next(_ids)), from_user=user(uid), chat_instance="ci", data=data,
                         message=m).as_(bot)


def cb_answers(bot: FakeBot) -> list[AnswerCallbackQuery]:
    return [kw["method"] for n, kw in bot.calls if n == "AnswerCallbackQuery"]


# ── фикстуры ─────────────────────────────────────────────────────────────────
@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def deps(cfg, db, fake_llm, notifier, ctx) -> Deps:
    return Deps(cfg=cfg, db=db, llm=fake_llm, ctx=ctx, agent=FakeAgent(), stt=FakeSTT(), tts=FakeTTS(),
                notifier=notifier)


@pytest.fixture
def h(deps) -> Handlers:
    hh = Handlers(deps)
    hh.TYPING_EVERY = 0.01
    return hh


# ── keyboards ────────────────────────────────────────────────────────────────
def test_build_basic_rows():
    kb = keyboards.build([[("✅ Готово", "rem:done:1"), ("💤 10 мин", "rem:snz:1:10")], [("✖️", "rem:del:1")]])
    assert isinstance(kb, InlineKeyboardMarkup)
    assert [[b.callback_data for b in row] for row in kb.inline_keyboard] == [
        ["rem:done:1", "rem:snz:1:10"], ["rem:del:1"]]
    assert kb.inline_keyboard[0][0].text == "✅ Готово"


@pytest.mark.parametrize("empty", [None, [], [[]], [[("", "x")]], [[("текст", "")]], "строка", 5])
def test_build_empty_is_none(empty):
    assert keyboards.build(empty) is None


def test_build_clips_callback_data_to_64_bytes():
    kb = keyboards.build([[("кнопка", "ы" * 50)]])
    data = kb.inline_keyboard[0][0].callback_data
    assert len(data.encode("utf-8")) <= 64
    assert data == "ы" * 32
    assert keyboards.clip_bytes("abc") == "abc"
    assert len(keyboards.clip_bytes("🙂" * 30).encode()) <= 64


def test_build_url_button_single_tuple_row_and_limits():
    kb = keyboards.build([("Сайт", "https://example.com"), [("a", "x:1")] * 10])
    first = kb.inline_keyboard[0][0]
    assert first.url == "https://example.com" and first.callback_data is None
    assert [len(r) for r in kb.inline_keyboard] == [1, 8, 2]
    many = keyboards.build([[(str(i), f"x:{i}")] for i in range(150)])
    assert sum(len(r) for r in many.inline_keyboard) == keyboards.TOTAL_MAX


def test_build_accepts_lists_from_json_and_cuts_long_labels():
    kb = keyboards.build([[["надпись " * 20, "fact:del:3"]]])
    assert kb.inline_keyboard[0][0].callback_data == "fact:del:3"
    assert len(kb.inline_keyboard[0][0].text) <= keyboards.TEXT_MAX


def test_without_removes_one_button():
    kb = keyboards.build([[("#1", "rem:del:1"), ("#2", "rem:del:2")], [("#3", "rem:del:3")]])
    rest = keyboards.without(kb, "rem:del:2")
    assert [[b.callback_data for b in r] for r in rest.inline_keyboard] == [["rem:del:1"], ["rem:del:3"]]
    assert keyboards.without(keyboards.build([[("#1", "rem:del:1")]]), "rem:del:1") is None
    assert keyboards.without(None, "x") is None


# ── notifier ─────────────────────────────────────────────────────────────────
async def test_notifier_sends_html_without_preview_and_buttons(bot):
    n = BotNotifier(bot, OWNER)
    mid = await n.send("**Привет** <мир>", [[("Ок", "rem:done:1")]])
    (kw,) = bot.named("send_message")
    assert kw["chat_id"] == OWNER
    assert kw["text"] == "<b>Привет</b> &lt;мир&gt;"
    assert kw["parse_mode"] == "HTML"
    assert kw["link_preview_options"].is_disabled is True
    assert kw["reply_markup"].inline_keyboard[0][0].callback_data == "rem:done:1"
    assert kw["disable_notification"] is None
    assert mid is not None


async def test_notifier_empty_text_sends_nothing(bot):
    n = BotNotifier(bot, OWNER)
    assert await n.send("") is None
    assert await n.send("   \n ") is None
    assert await n.send("```\n```") is None
    assert bot.calls == []


async def test_notifier_long_text_buttons_only_on_last_chunk(bot):
    n = BotNotifier(bot, OWNER)
    text = "\n\n".join(f"Абзац {i}. " + "слово " * 150 for i in range(8))
    last = await n.send(text, [[("Кнопка", "fact:del:1")]], silent=True)
    sent = bot.named("send_message")
    assert len(sent) > 1
    assert all(len(kw["text"]) <= 4096 for kw in sent)
    assert [kw["reply_markup"] is not None for kw in sent] == [False] * (len(sent) - 1) + [True]
    assert all(kw["disable_notification"] is True for kw in sent)
    assert last is not None


async def test_notifier_falls_back_to_plain_text_on_bad_request(bot):
    bot.fail["send_message"] = [bad_request()]
    n = BotNotifier(bot, OWNER)
    await n.send("**жирно** и [ссылка](https://e.com)", [[("Ок", "x:1")]])
    first, second = bot.named("send_message")
    assert first["parse_mode"] == "HTML"
    assert second["parse_mode"] is None
    assert second["text"] == "жирно и ссылка (https://e.com)"
    assert second["reply_markup"] is not None


async def test_notifier_drops_buttons_if_plain_with_buttons_also_fails(bot):
    bot.fail["send_message"] = [bad_request(), bad_request("Bad Request: BUTTON_DATA_INVALID")]
    n = BotNotifier(bot, OWNER)
    await n.send("текст", [[("Ок", "x:1")]])
    calls = bot.named("send_message")
    assert len(calls) == 3 and calls[2]["reply_markup"] is None


async def test_notifier_retry_after_sleeps_and_retries(bot, monkeypatch):
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr("oracle.bot.notifier.asyncio.sleep", fake_sleep)
    bot.fail["send_message"] = [TelegramRetryAfter(method=SendMessage(chat_id=1, text="x"),
                                                   message="Too Many Requests", retry_after=3)]
    n = BotNotifier(bot, OWNER)
    await n.send("текст")
    assert slept == [3.0]
    assert len(bot.named("send_message")) == 2


async def test_notifier_send_file_and_voice(bot):
    n = BotNotifier(bot, OWNER)
    await n.send_file(b"BEGIN:VCALENDAR", "calendar.ics", "Календарь: **2** события")
    (doc,) = bot.named("send_document")
    assert isinstance(doc["document"], BufferedInputFile)
    assert doc["document"].filename == "calendar.ics" and doc["document"].data == b"BEGIN:VCALENDAR"
    assert doc["caption"] == "Календарь: <b>2</b> события" and doc["parse_mode"] == "HTML"
    await n.send_voice(b"mp3", "voice.mp3")
    (v,) = bot.named("send_voice")
    assert v["voice"].filename == "voice.mp3" and "caption" not in v


async def test_notifier_long_caption_goes_as_separate_message(bot):
    n = BotNotifier(bot, OWNER)
    await n.send_file(b"x", "a.txt", "длинно " * 300)
    assert "caption" not in bot.named("send_document")[0]
    assert len(bot.named("send_message")) >= 1


async def test_notifier_caption_bad_request_resends_plain(bot):
    bot.fail["send_document"] = [bad_request()]
    n = BotNotifier(bot, OWNER)
    await n.send_file(b"x", "a.db", "**Бэкап**")
    first, second = bot.named("send_document")
    assert second["parse_mode"] is None and second["caption"] == "Бэкап"


async def test_notifier_voice_forbidden_falls_back_to_audio(bot):
    bot.fail["send_voice"] = [bad_request("Bad Request: VOICE_MESSAGES_FORBIDDEN")]
    n = BotNotifier(bot, OWNER)
    await n.send_voice(b"mp3")
    assert len(bot.named("send_audio")) == 1


async def test_notifier_send_html_and_fallback(bot):
    n = BotNotifier(bot, OWNER)
    await n.send_html("🎙 <i>привет &amp; пока</i>")
    assert bot.named("send_message")[0]["parse_mode"] == "HTML"
    bot.fail["send_message"] = [bad_request()]
    await n.send_html("<i>a &lt; b</i>")
    last = bot.named("send_message")[-1]
    assert last["parse_mode"] is None and last["text"] == "a < b"
    assert await n.send_html("  ") is None


# ── middleware ───────────────────────────────────────────────────────────────
def test_is_owner(cfg):
    assert is_owner(cfg, OWNER)
    assert is_owner(cfg, str(OWNER))
    assert not is_owner(cfg, 7)
    assert not is_owner(cfg, None)
    assert not is_owner(cfg, True)
    assert not is_owner(replace(cfg, owner_id=0), 0)
    assert not is_owner(replace(cfg, owner_id=0), 5)


def test_is_start_and_id_text():
    assert is_start("/start") and is_start("/start@MyBot") and is_start("/start payload")
    assert not is_start("/startle") and not is_start("start") and not is_start(None)
    assert id_text(123).startswith("Твой Telegram id: <code>123</code>\nВпиши в .env строку OWNER_ID=123 и "
                                   "перезапусти бота — после этого я буду слушаться только тебя.")
    assert "другая, старая копия" in id_text(123)


class _Handler:
    def __init__(self) -> None:
        self.events: list = []

    async def __call__(self, event: Any, data: dict) -> str:
        self.events.append(event)
        return "handled"


def answered_texts(bot: FakeBot) -> list[str]:
    return [kw["method"].text for n, kw in bot.calls if n == "SendMessage"]


async def test_middleware_owner_passes(cfg, bot):
    mw, handler = OwnerOnly(cfg), _Handler()
    assert await mw(handler, msg(bot, "привет"), {}) == "handled"
    assert await mw(handler, cbq(bot, "rem:done:1"), {}) == "handled"
    assert len(handler.events) == 2 and bot.calls == []


async def test_middleware_setup_mode_start_replies_with_id(cfg, bot):
    mw, handler = OwnerOnly(replace(cfg, owner_id=0)), _Handler()
    assert await mw(handler, msg(bot, "/start", uid=777), {}) is None
    assert handler.events == []
    (m,) = [kw["method"] for n, kw in bot.calls if n == "SendMessage"]
    assert m.text == id_text(777) and m.parse_mode == "HTML" and m.chat_id == 777


async def test_middleware_setup_mode_other_text_gets_hint_once(cfg, bot):
    now = [0.0]
    mw = OwnerOnly(replace(cfg, owner_id=0), clock=lambda: now[0])
    handler = _Handler()
    await mw(handler, msg(bot, "привет", uid=5), {})
    await mw(handler, msg(bot, "эй", uid=5), {})
    assert handler.events == []
    assert len(answered_texts(bot)) == 1 and "/start" in answered_texts(bot)[0]


async def test_middleware_stranger_ignored_with_hourly_reply(cfg, bot):
    now = [1000.0]
    mw = OwnerOnly(cfg, clock=lambda: now[0])
    handler = _Handler()
    for _ in range(3):
        assert await mw(handler, msg(bot, "дай доступ", uid=9), {}) is None
    assert answered_texts(bot) == [STRANGER_TEXT]
    now[0] += 3601
    await mw(handler, msg(bot, "ну пожалуйста", uid=9), {})
    await mw(handler, msg(bot, "/start", uid=10), {})
    assert answered_texts(bot) == [STRANGER_TEXT] * 3
    assert handler.events == []


async def test_middleware_stranger_reply_can_be_disabled(cfg, bot):
    mw = OwnerOnly(cfg, stranger_reply=False)
    await mw(_Handler(), msg(bot, "эй", uid=9), {})
    assert bot.calls == []


async def test_middleware_stranger_callback_no_access(cfg, bot):
    mw, handler = OwnerOnly(cfg), _Handler()
    assert await mw(handler, cbq(bot, "rem:done:1", uid=9), {}) is None
    (a,) = cb_answers(bot)
    assert a.text == NO_ACCESS and a.show_alert is True
    assert handler.events == []


# ── parse_cb, задачка ────────────────────────────────────────────────────────
@pytest.mark.parametrize("data, want", [
    ("rem:done:12", ("rem", "done", [12])),
    ("rem:snz:12:10", ("rem", "snz", [12, 10])),
    ("rem:del:3", ("rem", "del", [3])),
    ("wake:ans:5:77", ("wake", "ans", [5, 77])),
    ("bday:regen:1", ("bday", "regen", [1])),
    ("bday:send:1", ("bday", "send", [1])),
    ("idea:deep:9", ("idea", "deep", [9])),
    ("draft:new:-1001234567890", ("draft", "new", [-1001234567890])),
    ("draft:send:4", ("draft", "send", [4])),
    ("draft:regen:4", ("draft", "regen", [4])),
    ("draft:drop:4", ("draft", "drop", [4])),
    ("fact:del:8", ("fact", "del", [8])),
])
def test_parse_cb_valid(data, want):
    assert parse_cb(data) == want


@pytest.mark.parametrize("data", [
    None, "", "rem", "rem:done", "rem:done:x", "rem:done:1:2", "rem:snz:1", "rem:nope:1", "zzz:done:1",
    "rem:done:1.5", "rem:done: 1", "wake:ans:1", 12, "x" * 70, "fact:del:" + "9" * 25,
])
def test_parse_cb_malformed(data):
    assert parse_cb(data) is None


def test_make_challenge_is_consistent():
    for seed in range(50):
        q, ans, opts = make_challenge(random.Random(seed))
        a, b = (int(x) for x in q.replace(" = ?", "").split(" + "))
        assert a + b == ans
        assert len(opts) == 4 and len(set(opts)) == 4 and ans in opts
        assert all(o > 0 for o in opts)
    q1 = make_challenge(random.Random(1))
    assert q1 == make_challenge(random.Random(1))
    q, ans, opts = make_challenge()
    assert ans in opts


def test_challenge_buttons_format():
    btns = challenge_buttons(7, [10, 11, 20, 21])
    assert btns == [[("10", "wake:ans:7:10"), ("11", "wake:ans:7:11"), ("20", "wake:ans:7:20"),
                     ("21", "wake:ans:7:21")]]
    assert all(parse_cb(d) for _, d in btns[0])


def test_small_helpers():
    assert H.command_args("/news@Bot  про нефть ") == "про нефть"
    assert H.command_args("/news") == ""
    assert H.int_arg("14", 7, 1, 366) == 14 and H.int_arg("abc", 7, 1, 366) == 7 and H.int_arg("9999", 7, 1, 366) == 366
    assert [H.plural(n, "день", "дня", "дней") for n in (1, 2, 5, 11, 21, 22, 112)] == \
        ["день", "дня", "дней", "дней", "день", "дня", "дней"]
    assert H.err_text(ValueError("нет такого")) == "нет такого"
    assert H.err_text(RuntimeError("бум")) == "RuntimeError: бум"
    assert H.err_text(KeyError()) == "KeyError"


# ── роутер и приложение ──────────────────────────────────────────────────────
def test_build_router_smoke(deps):
    router = build_router(deps)
    assert isinstance(router.oracle_handlers, Handlers)
    assert len(router.message.handlers) == len(H.COMMANDS) + 5
    assert len(router.edited_message.handlers) == 1
    assert len(router.callback_query.handlers) == 1
    assert set(router.oracle_handlers.routes) == set(H.CB_ARITY)


def test_app_imports_and_dispatcher(cfg, deps):
    import oracle.__main__ as entry
    from oracle import app
    assert callable(entry.run) and callable(app.main)
    dp = app.build_dispatcher(cfg, deps)
    assert set(dp.resolve_used_update_types()) == {"message", "edited_message", "callback_query"}
    cmds = app.bot_commands()
    assert [c.command for c in cmds] == [c for c, _ in H.COMMANDS]
    assert all(1 <= len(c.description) <= 256 for c in cmds)
    assert app.is_setup_mode(replace(cfg, owner_id=0)) and app.is_setup_mode(replace(cfg, llm_api_key=""))
    assert not app.is_setup_mode(cfg)


async def test_app_main_without_token_exits_1(monkeypatch, capsys, cfg):
    from oracle import app
    monkeypatch.setattr(app.config, "load", lambda *a, **k: replace(cfg, bot_token=""))
    assert await app.main() == 1
    err = capsys.readouterr().err
    assert "BOT_TOKEN" in err and "@BotFather" in err


async def test_app_main_bad_token_exits_1(monkeypatch, capsys, cfg):
    from oracle import app
    monkeypatch.setattr(app.config, "load", lambda *a, **k: replace(cfg, bot_token="не-токен"))
    assert await app.main() == 1
    assert "BOT_TOKEN" in capsys.readouterr().err


def test_main_entry_handles_keyboard_interrupt(monkeypatch):
    import oracle.__main__ as entry

    def boom(coro: Any) -> int:
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(entry.asyncio, "run", boom)
    assert entry.run() == 0


# ── сообщения ────────────────────────────────────────────────────────────────
async def test_text_goes_to_agent_and_reply_is_sent(h, deps, notifier, bot):
    await h.on_text(msg(bot, "как дела?"))
    assert deps.agent.calls == [("как дела?", "text", None)]
    assert notifier.texts() == ["Ответ агента **жирно**"]
    assert deps.tts.calls == []                     # mirror: на текст — текстом
    assert bot.named("send_chat_action")[0]["action"] == "typing"


async def test_outbox_items_are_delivered(h, deps, notifier, bot):
    deps.agent.reply = Reply("Готово", outbox=[
        OutItem(kind="file", text="Календарь", data=b"ICS", filename="calendar.ics"),
        OutItem(kind="text", text="Черновик", buttons=[[("📨", "draft:send:1")]]),
        OutItem(kind="voice", data=b"mp3")])
    await h.on_text(msg(bot, "выгрузи"))
    kinds = [(s["kind"], s["text"]) for s in notifier.sent]
    assert kinds == [("text", "Готово"), ("file", "Календарь"), ("text", "Черновик"), ("voice", "")]
    assert notifier.sent[2]["buttons"] == [[("📨", "draft:send:1")]]


async def test_deep_mode_note_first(h, deps, notifier, bot, db):
    await db.kv_set("mode", "deep")
    await h.on_text(msg(bot, "разбери"))
    assert notifier.texts()[0] == H.DEEP_NOTE
    assert deps.agent.calls[0][2] is None           # режим решает сам агент по kv


async def test_tts_always_speaks_text_replies(h, deps, notifier, bot, db):
    await db.kv_set("tts_mode", "always")
    await h.on_text(msg(bot, "привет"))
    assert deps.tts.calls == ["Ответ агента **жирно**"]
    assert notifier.sent[-1]["kind"] == "voice"


async def test_tts_skipped_on_agent_error_and_failures_ignored(h, deps, notifier, bot, db):
    await db.kv_set("tts_mode", "always")
    deps.agent.reply = Reply("⚠️ модель легла", error="модель легла")
    await h.on_text(msg(bot, "привет"))
    assert deps.tts.calls == []
    deps.agent.reply = Reply("норм")
    deps.tts.fail = True
    await h.on_text(msg(bot, "ещё"))
    assert notifier.texts()[-1] == "норм"           # голос упал — текст всё равно ушёл


async def test_voice_message_transcribed_and_answered_by_voice(h, deps, notifier, bot, db):
    await db.kv_set("tts_mode", "mirror")                  # по умолчанию голосом не отвечаем — включён явно
    notifier_html = NotifierWithHtml(notifier)
    deps.notifier = notifier_html
    m = msg(bot, voice=Voice(file_id="f1", file_unique_id="u1", duration=3, file_size=2000))
    await h.on_voice(m)
    assert deps.stt.calls == [(b"OggS-audio", "voice.ogg", "audio/ogg")]
    assert notifier_html.html == ["🎙 <i>напомни завтра в девять</i>"]
    assert deps.agent.calls == [("напомни завтра в девять", "voice", None)]
    assert notifier.sent[-1]["kind"] == "voice"     # mirror: на голос — голосом
    assert bot.named("download")[0]["file"].file_id == "f1"


async def test_voice_transcript_escaped_and_plain_fallback(h, deps, notifier, bot):
    deps.stt.text = "a <b> & c"
    notifier_html = NotifierWithHtml(notifier)
    deps.notifier = notifier_html
    await h.on_voice(msg(bot, voice=Voice(file_id="f", file_unique_id="u", duration=1)))
    assert notifier_html.html == ["🎙 <i>a &lt;b&gt; &amp; c</i>"]
    deps.notifier = notifier                        # без send_html — простой текст
    await h.on_voice(msg(bot, voice=Voice(file_id="f", file_unique_id="u", duration=1)))
    assert "🎙 a <b> & c" in notifier.texts()


async def test_voice_transcript_can_be_hidden(h, deps, notifier, bot):
    deps.cfg = replace(deps.cfg, show_transcript=False)
    await h.on_voice(msg(bot, voice=Voice(file_id="f", file_unique_id="u", duration=1)))
    assert not any(t.startswith("🎙") for t in notifier.texts())
    assert deps.agent.calls


async def test_audio_and_video_note_filenames(h, deps, bot):
    await h.on_voice(msg(bot, audio=Audio(file_id="a", file_unique_id="u", duration=5, file_name="rec.m4a",
                                          mime_type="audio/mp4")))
    await h.on_voice(msg(bot, audio=Audio(file_id="a", file_unique_id="u", duration=5)))
    await h.on_voice(msg(bot, video_note=VideoNote(file_id="v", file_unique_id="u", length=240, duration=5)))
    assert [(c[1], c[2]) for c in deps.stt.calls] == [
        ("rec.m4a", "audio/mp4"), ("audio.mp3", "audio/mpeg"), ("video.mp4", "video/mp4")]


async def test_voice_too_big(h, deps, notifier, bot):
    await h.on_voice(msg(bot, voice=Voice(file_id="f", file_unique_id="u", duration=900,
                                          file_size=25 * 1024 * 1024)))
    assert "20 МБ" in notifier.texts()[0]
    assert deps.stt.calls == [] and bot.named("download") == []


async def test_voice_without_stt_explains_how_to_enable(h, deps, notifier, bot):
    deps.stt = FakeSTT(ok=False)
    await h.on_voice(msg(bot, voice=Voice(file_id="f", file_unique_id="u", duration=1)))
    text = notifier.texts()[0]
    assert "console.groq.com" in text and "GROQ_API_KEY" in text and "requirements-voice.txt" in text
    assert deps.agent.calls == []


async def test_voice_stt_error_and_empty(h, deps, notifier, bot):
    from oracle.services.stt import STTError
    deps.stt = FakeSTT(error=STTError("сервис распознавания не отвечает"))
    await h.on_voice(msg(bot, voice=Voice(file_id="f", file_unique_id="u", duration=1)))
    assert notifier.texts() == ["Не расслышал: сервис распознавания не отвечает"]
    deps.stt = FakeSTT(text="   ")
    await h.on_voice(msg(bot, voice=Voice(file_id="f", file_unique_id="u", duration=1)))
    assert notifier.texts()[-1] == H.NOT_HEARD
    assert deps.agent.calls == []


async def test_setup_mode_text_and_start(h, deps, notifier, bot):
    deps.agent = None
    deps.cfg = replace(deps.cfg, llm_api_key="")
    await h.on_text(msg(bot, "привет"))
    assert "DEEPSEEK_API_KEY" in notifier.texts()[0]
    await h.cmd_start(msg(bot, "/start"))
    assert "режиме настройки" in notifier.texts()[-1]


async def test_other_media_and_unknown_command(h, deps, notifier, bot):
    await h.on_other(msg(bot))
    assert notifier.texts()[-1] == H.OTHER_MEDIA
    await h.on_other(msg(bot, caption="что тут?"))
    assert deps.agent.calls[-1][0].endswith("что тут?")
    await h.on_unknown_command(msg(bot, "/foo@Bot bar"))
    assert notifier.texts()[-1] == "Не знаю команды /foo. Список — /help."


async def test_guard_turns_exceptions_into_error_message(h, notifier, bot):
    async def boom(message: Any) -> None:
        raise RuntimeError("сломалось")

    await h.wrap(boom)(msg(bot, "x"))
    assert notifier.texts() == ["⚠️ Ошибка: RuntimeError: сломалось"]


# ── команды ──────────────────────────────────────────────────────────────────
async def test_start_and_help(h, notifier, bot, db):
    await h.cmd_start(msg(bot, "/start"))
    assert "Олег" in notifier.texts()[0] and "голосовое" in notifier.texts()[0]
    assert await db.kv_get("owner_name") == "Олег"        # имя для агента, пока OWNER_NAME пуст
    await h.cmd_help(msg(bot, "/help"))
    help_text = notifier.texts()[1]
    for cmd, _ in H.COMMANDS:
        if cmd not in ("start", "help"):
            assert f"/{cmd}" in help_text
    assert "не отставай, пока не встану" in help_text and "кофейню" in help_text


async def test_mode_toggle(h, notifier, bot, db):
    await h.cmd_mode(msg(bot, "/mode"))
    assert await db.kv_get("mode") == "deep" and "глубокий" in notifier.texts()[-1]
    await h.cmd_mode(msg(bot, "/mode"))
    assert await db.kv_get("mode") == "fast" and "быстрый" in notifier.texts()[-1]


async def test_voice_mode_cycle(h, deps, notifier, bot, db):
    seen = []
    for _ in range(4):
        await h.cmd_voice(msg(bot, "/voice"))
        seen.append(await db.kv_get("tts_mode"))
    assert seen == ["mirror", "always", "off", "mirror"]       # из умолчания off
    deps.tts = FakeTTS(ok=False)
    await db.kv_set("tts_mode", "off")
    await h.cmd_voice(msg(bot, "/voice"))
    assert "edge-tts" in notifier.texts()[-1]


async def test_deep_command(h, deps, notifier, bot):
    await h.cmd_deep(msg(bot, "/deep"))
    assert "Что обдумать" in notifier.texts()[-1] and deps.agent.calls == []
    await h.cmd_deep(msg(bot, "/deep в чём смысл?"))
    assert deps.agent.calls == [("в чём смысл?", "text", True)]
    assert H.DEEP_NOTE in notifier.texts()


async def test_reset(h, deps, notifier, bot, db):
    await h.cmd_reset(msg(bot, "/reset"))
    assert "7 реплик" in notifier.texts()[-1]
    deps.agent = None
    await db.add_message("user", "раз")
    await h.cmd_reset(msg(bot, "/reset"))
    assert "1 реплику" in notifier.texts()[-1]
    assert await db.recent_messages(10) == []


async def test_today_uses_brief(h, notifier, bot, monkeypatch):
    brief = importlib.import_module("oracle.services.brief")

    async def fake_brief(ctx: Any) -> str:
        return "Сегодня: ничего."

    monkeypatch.setattr(brief, "today_brief", fake_brief)       # /today — остаток дня, не «доброе утро»
    await h.cmd_today(msg(bot, "/today"))
    assert notifier.texts() == ["Сегодня: ничего."]


async def test_news_command(h, db, notifier, bot, monkeypatch):
    tnews = importlib.import_module("oracle.tools.news")
    calls = []

    async def fake_digest(ctx: Any, topic: str = "", *, deep: bool = True) -> str:
        calls.append((topic, deep))
        return "Сюжет 1"

    monkeypatch.setattr(tnews, "news_digest", fake_digest)
    await h.cmd_news(msg(bot, "/news нефть"))
    assert calls == [("нефть", False)]                 # быстрый режим — быстрая модель, без минут ожидания
    assert notifier.texts() == ["Собираю новости про «нефть»…", "🗞 Сюжет 1"]
    await db.kv_set("mode", "deep")
    await h.cmd_news(msg(bot, "/news"))
    assert calls[-1] == ("", True)
    assert "пару минут" in notifier.texts()[-2]


async def test_reminders_list_with_delete_buttons(h, ctx, notifier, bot):
    rem = importlib.import_module("oracle.tools.reminders")
    assert "нет" in (await _run(h.cmd_reminders, bot, notifier))
    for i in range(22):
        await rem.create_reminder(ctx, text=f"Дело {i}", when=f"2026-09-29 {8 + i % 10:02d}:{i:02d}")
    text = await _run(h.cmd_reminders, bot, notifier)
    assert text.startswith("⏰ Напоминания:") and "Дело 0" in text and "первых 20" in text
    buttons = notifier.sent[-1]["buttons"]
    flat = [d for row in buttons for _, d in row]
    assert len(flat) == 20 and all(d.startswith("rem:del:") for d in flat)
    assert all(len(row) <= 4 for row in buttons)


async def _run(fn: Any, bot: FakeBot, notifier: Any, text: str | None = None) -> str:
    await fn(msg(bot, text or "/cmd"))
    return notifier.texts()[-1]


async def test_calendar_and_ics(h, ctx, notifier, bot):
    cal = importlib.import_module("oracle.tools.calendar")
    assert "пусто" in await _run(h.cmd_calendar, bot, notifier, "/calendar")
    await h.cmd_ics(msg(bot, "/ics"))
    assert "не отправляю" in notifier.texts()[-1]
    await cal.add_event(ctx, title="Встреча с Петей", start="2026-09-29 15:00", location="кафе")
    await cal.add_event(ctx, title="Планёрка", start="2026-09-28 10:00", repeat="FREQ=WEEKLY;BYDAY=MO")
    await cal.add_event(ctx, title="Далеко", start="2026-10-20 10:00")
    text = await _run(h.cmd_calendar, bot, notifier, "/calendar 3")
    assert "3 дня" in text and "Встреча с Петей" in text and "(кафе)" in text and "Планёрка" in text
    assert "Далеко" not in text
    await h.cmd_ics(msg(bot, "/ics"))
    f = notifier.sent[-1]
    assert f["kind"] == "file" and f["filename"] == "calendar.ics"
    assert f["data"].startswith(b"BEGIN:VCALENDAR") and "3 события" in f["text"]


async def test_birthdays(h, db, notifier, bot):
    importlib.import_module("oracle.tools.birthdays")
    assert "не знаю" in await _run(h.cmd_birthdays, bot, notifier)
    await db.execute("INSERT INTO birthdays(name, month, day, year, relation, created_at) VALUES(?,?,?,?,?,?)",
                     ("Маша", 9, 29, 1996, "сестра", "2026-01-01T00:00:00+00:00"))
    await db.execute("INSERT INTO birthdays(name, month, day, created_at) VALUES(?,?,?,?)",
                     ("Лёха", 3, 14, "2026-01-01T00:00:00+00:00"))
    text = await _run(h.cmd_birthdays, bot, notifier)
    lines = text.split("\n")
    assert lines[1] == "• 29 сентября, вт — Маша (сестра) · завтра · исполнится 30"
    assert lines[2].startswith("• 14 марта") and "Лёха" in lines[2] and "через 167 дней" in lines[2]


async def test_ideas_and_idea(h, db, notifier, bot):
    importlib.import_module("oracle.tools.ideas")
    assert "Идей пока нет" in await _run(h.cmd_ideas, bot, notifier)
    now = "2026-09-27T10:00:00+00:00"
    iid = await db.execute("INSERT INTO ideas(title, content, evaluation, score, status, created_at, updated_at) "
                           "VALUES(?,?,?,?,?,?,?)", ("Кофейня", "у вокзала", "рынок тесный", 6, "new", now, now))
    await db.execute("INSERT INTO ideas(title, content, status, created_at, updated_at) VALUES(?,?,?,?,?)",
                     ("Без оценки", "x", "parked", now, now))
    text = await _run(h.cmd_ideas, bot, notifier)
    assert f"#{iid} [6/10] Кофейня · новая · 27.09" in text and "[—] Без оценки · отложена" in text
    assert "Какую?" in await _run(h.cmd_idea, bot, notifier, "/idea")
    assert "Идеи #99 нет" in await _run(h.cmd_idea, bot, notifier, "/idea 99")
    card = await _run(h.cmd_idea, bot, notifier, f"/idea #{iid}")
    assert "Кофейня" in card and "рынок тесный" in card
    assert notifier.sent[-1]["buttons"] == [[("🧠 Додумать глубоко", f"idea:deep:{iid}")]]


async def test_projects(h, ctx, notifier, bot):
    projects = importlib.import_module("oracle.tools.projects")
    assert "Открытых проектов нет" in await _run(h.cmd_projects, bot, notifier)
    p = await projects.t_create_project(ctx, name="Ремонт", goal="к зиме")
    await projects.t_add_task(ctx, text="купить краску", project=p["id"])
    done = await projects.t_create_project(ctx, name="Старое")
    await projects.t_update_project(ctx, project=done["id"], status="done")
    text = await _run(h.cmd_projects, bot, notifier)
    assert f"#{p['id']} Ремонт · открыто 1, сделано 0" in text and "цель: к зиме" in text
    assert "Закрытых: 1" in text and "Старое" not in text


async def test_memory_forget_and_fact_button(h, db, notifier, bot):
    memory = importlib.import_module("oracle.tools.memory")
    assert "ничего о тебе" in await _run(h.cmd_memory, bot, notifier)
    ids = [(await memory.add_fact(db, f"Факт номер {i} про работу и жизнь", "work" if i % 2 else "person"))[0]
           for i in range(35)]
    text = await _run(h.cmd_memory, bot, notifier)
    assert "**Люди**" in text and "**Работа**" in text and f"#{ids[0]} Факт номер 0" in text
    flat = [d for row in notifier.sent[-1]["buttons"] for _, d in row]
    assert len(flat) == 30 and flat[0] == f"fact:del:{ids[-1]}"
    assert "Что забыть" in await _run(h.cmd_forget, bot, notifier, "/forget")
    assert f"Забыл #{ids[0]}" in await _run(h.cmd_forget, bot, notifier, f"/forget {ids[0]}")
    assert "нет" in await _run(h.cmd_forget, bot, notifier, f"/forget {ids[0]}")
    kb = keyboards.build([[("✖️ #1", f"fact:del:{ids[1]}"), ("✖️ #2", f"fact:del:{ids[2]}")]])
    q = cbq(bot, f"fact:del:{ids[1]}", markup=kb)
    await h.on_callback(q)
    assert await memory.get_fact(db, ids[1]) is None
    (edit,) = bot.named("edit_message_reply_markup")
    assert [b.callback_data for b in edit["reply_markup"].inline_keyboard[0]] == [f"fact:del:{ids[2]}"]
    assert cb_answers(bot)[-1].text == f"Забыл #{ids[1]}"


async def test_opinions_and_journal(h, db, notifier, bot):
    assert "позиций пока не" in await _run(h.cmd_opinions, bot, notifier)
    hist = json.dumps([{"stance": "старое", "why_changed": "аргумент"}], ensure_ascii=False)
    now = "2026-09-27T10:00:00+00:00"
    await db.execute("INSERT INTO opinions(topic, stance, reasons, confidence, history, created_at, updated_at) "
                     "VALUES(?,?,?,?,?,?,?)", ("Крипта", "пузырь", "", 70, hist, now, now))
    await db.execute("INSERT INTO opinions(topic, stance, confidence, created_at, updated_at) VALUES(?,?,?,?,?)",
                     ("Спорт", "нужен", 90, now, now))
    text = await _run(h.cmd_opinions, bot, notifier)
    assert text.index("Спорт") < text.index("Крипта")
    assert "(уверенность 70%, менял 1 раз)" in text and "(уверенность 90%, не менял)" in text
    assert "Дневник пуст" in await _run(h.cmd_journal, bot, notifier)
    for i in range(4):
        await db.execute("INSERT INTO journal(content, created_at) VALUES(?,?)", (f"запись {i}", now))
    text = await _run(h.cmd_journal, bot, notifier)
    assert "запись 3" in text and "запись 1" in text and "запись 0" not in text


async def test_backup_sends_file_and_cleans_up(h, cfg, db, notifier, bot):
    await db.kv_set("x", 1)
    await h.cmd_backup(msg(bot, "/backup"))
    f = notifier.sent[-1]
    assert f["kind"] == "file" and f["filename"] == "oracle-backup-20260928.db"
    assert f["data"].startswith(b"SQLite format 3")
    assert list(cfg.data_dir.glob(".backup-*")) == []


async def test_chats(h, deps, notifier, bot):
    text = await _run(h.cmd_chats, bot, notifier)
    assert "my.telegram.org" in text and "userbot_login" in text
    deps.userbot = FakeUserbot()
    text = await _run(h.cmd_chats, bot, notifier)
    assert "• Маша (2): ты где?" in text and "• Работа (5)" in text
    assert notifier.sent[-1]["buttons"] == [[("✍️ Ответ: Маша", "draft:new:555")],
                                            [("✍️ Ответ: Работа", "draft:new:-100123")]]


async def test_status(h, deps, db, notifier, bot, ctx):
    rem = importlib.import_module("oracle.tools.reminders")
    await rem.create_reminder(ctx, text="Позвонить маме", when="2026-09-28 18:00")
    await db.kv_set("mode", "deep")
    text = await _run(h.cmd_status, bot, notifier)
    assert "✅ Ключи на месте" in text and "Режим: глубокий" in text
    assert "Groq (тест)" in text and "Userbot: выключен" in text
    assert "активных напоминаний — 1" in text and "Позвонить маме" in text
    assert "Планировщик: не запущен" in text
    assert "Расписание: сводка 08:00, дни рождения 09:00, новости 09:00, рефлексия 03:30" in text


# ── кнопки ───────────────────────────────────────────────────────────────────
async def test_callback_malformed_answers_stale(h, bot):
    for data in ("rem:done:x", "oops", "rem:zzz:1", ""):
        await h.on_callback(cbq(bot, data))
    assert [a.text for a in cb_answers(bot)] == [H.STALE] * 4


async def test_callback_always_answered_even_on_crash(h, notifier, bot, monkeypatch):
    async def boom(query: Any, cb: Any, args: list[int]) -> None:
        raise RuntimeError("упало")

    h.routes[("rem", "done")] = boom
    await h.on_callback(cbq(bot, "rem:done:1"))
    assert len(cb_answers(bot)) == 1
    assert notifier.texts() == ["⚠️ Ошибка: RuntimeError: упало"]


async def test_rem_done_acks_and_clears_keyboard(h, ctx, db, bot):
    rem = importlib.import_module("oracle.tools.reminders")
    row = await rem.create_reminder(ctx, text="Таблетка", when="2026-09-28 10:00", nag=True)
    await db.execute("UPDATE reminders SET next_at=NULL, nag_active=1, nag_count=2 WHERE id=?", (row["id"],))
    await h.on_callback(cbq(bot, f"rem:done:{row['id']}"))
    new = await rem.get_reminder(db, row["id"])
    assert new["status"] == "done" and new["nag_active"] == 0
    assert cb_answers(bot)[-1].text == "Отмечено"
    assert bot.named("edit_message_reply_markup")[-1]["reply_markup"] is None
    await h.on_callback(cbq(bot, "rem:done:9999"))
    assert cb_answers(bot)[-1].text == "Напоминания уже нет"


async def test_wake_challenge_flow(h, ctx, db, notifier, bot, monkeypatch):
    rem = importlib.import_module("oracle.tools.reminders")
    row = await rem.create_reminder(ctx, text="Подъём", when="2026-09-29 06:30", kind="wake")
    rid = row["id"]
    assert row["challenge"] == 1
    await db.execute("UPDATE reminders SET nag_active=1 WHERE id=?", (rid,))
    monkeypatch.setattr(H, "make_challenge", lambda rng=None: ("20 + 22 = ?", 42, [41, 42, 52, 32]))

    await h.on_callback(cbq(bot, f"rem:done:{rid}"))
    assert (await db.kv_get(f"challenge:{rid}"))["a"] == 42
    assert notifier.texts()[-1] == "🧮 Докажи, что проснулся: 20 + 22 = ?"
    qid = (await db.kv_get(f"challenge:{rid}"))["id"]                 # номер задачки — в кнопках
    assert notifier.sent[-1]["buttons"] == challenge_buttons(rid, [41, 42, 52, 32], qid)
    assert (await rem.get_reminder(db, rid))["nag_active"] == 1
    assert cb_answers(bot)[-1].text == "Сначала реши задачку"

    monkeypatch.setattr(H, "make_challenge", lambda rng=None: ("30 + 30 = ?", 60, [60, 61, 50, 70]))
    await h.on_callback(cbq(bot, f"wake:ans:{rid}:41"))
    assert notifier.texts()[-1] == "❌ Мимо. 🧮 Докажи, что проснулся: 30 + 30 = ?"
    assert (await db.kv_get(f"challenge:{rid}"))["a"] == 60
    assert (await rem.get_reminder(db, rid))["nag_active"] == 1

    await h.on_callback(cbq(bot, f"wake:ans:{rid}:60"))
    assert notifier.texts()[-1] == "✅ Проснулся. Доброе утро."
    new = await rem.get_reminder(db, rid)
    assert new["nag_active"] == 0
    assert await db.kv_get(f"challenge:{rid}") is None
    assert cb_answers(bot)[-1].text == "Верно"
    msgs = await db.recent_messages(5)
    assert "проснулся" in msgs[-1]["content"]

    await h.on_callback(cbq(bot, f"wake:ans:{rid}:60"))    # старая кнопка
    assert cb_answers(bot)[-1].text == "Уже не актуально"


async def test_wake_answer_without_stored_challenge_issues_new_one(h, ctx, db, notifier, bot):
    rem = importlib.import_module("oracle.tools.reminders")
    row = await rem.create_reminder(ctx, text="Подъём", when="2026-09-29 06:30", kind="wake")
    await db.execute("UPDATE reminders SET nag_active=1 WHERE id=?", (row["id"],))
    await h.on_callback(cbq(bot, f"wake:ans:{row['id']}:5"))
    assert await db.kv_get(f"challenge:{row['id']}") is not None
    assert "Докажи" in notifier.texts()[-1]


async def test_rem_snooze_and_delete(h, ctx, db, bot):
    rem = importlib.import_module("oracle.tools.reminders")
    row = await rem.create_reminder(ctx, text="Чайник", when="2026-09-28 10:00")
    await db.execute("UPDATE reminders SET last_fired_at=? WHERE id=?",   # «💤» — под сработавшим
                     ("2026-09-28T06:00:00+00:00", row["id"]))
    await h.on_callback(cbq(bot, f"rem:snz:{row['id']}:10"))
    new = await rem.get_reminder(db, row["id"])
    assert new["snooze_at"] == "2026-09-28T06:10:00+00:00"
    assert cb_answers(bot)[-1].text == "💤 Напомню в 09:10"
    kb = keyboards.build([[(f"✖️ #{row['id']}", f"rem:del:{row['id']}"), ("✖️ #99", "rem:del:99")]])
    await h.on_callback(cbq(bot, f"rem:del:{row['id']}", markup=kb))
    assert (await rem.get_reminder(db, row["id"]))["status"] == "cancelled"
    assert cb_answers(bot)[-1].text == f"Отменил #{row['id']}"
    left = bot.named("edit_message_reply_markup")[-1]["reply_markup"]
    assert [b.callback_data for b in left.inline_keyboard[0]] == ["rem:del:99"]
    await h.on_callback(cbq(bot, f"rem:snz:{row['id']}:10"))   # отменённое не воскрешаем
    assert (await rem.get_reminder(db, row["id"]))["status"] == "cancelled"
    assert cb_answers(bot)[-1].text == "Напоминания уже нет"
    await h.on_callback(cbq(bot, f"rem:del:{row['id']}"))
    assert cb_answers(bot)[-1].text == "Уже отменено"


async def test_rem_snooze_across_midnight_shows_date(h, ctx, db, bot, clock):
    rem = importlib.import_module("oracle.tools.reminders")
    row = await rem.create_reminder(ctx, text="Спать", when="2026-09-28 23:50")
    clock.set(datetime(2026, 9, 28, 20, 55, tzinfo=UTC))      # 23:55 МСК
    await db.execute("UPDATE reminders SET last_fired_at=? WHERE id=?", ("2026-09-28T20:50:00+00:00", row["id"]))
    await h.on_callback(cbq(bot, f"rem:snz:{row['id']}:60"))
    assert cb_answers(bot)[-1].text == "💤 Напомню 29.09 в 00:55"


async def _add_bday(db: Any, tg: str = "masha_k") -> int:
    return await db.execute("INSERT INTO birthdays(name, month, day, year, relation, tg_username, created_at) "
                            "VALUES(?,?,?,?,?,?,?)", ("Маша", 9, 28, 1996, "сестра", tg,
                                                      "2026-01-01T00:00:00+00:00"))


async def test_bday_regen(h, deps, db, notifier, bot, fake_llm):
    bd = importlib.import_module("oracle.tools.birthdays")
    bid = await _add_bday(db)
    fake_llm.script = ["Маша, с тридцатником! Пусть этот год будет про тебя, а не про чужие планы."]
    await h.on_callback(cbq(bot, f"bday:regen:{bid}"))
    assert cb_answers(bot)[0].text == "Пишу другой вариант…"
    assert notifier.texts()[-1].startswith("Маша, с тридцатником")
    assert await db.kv_get(bd.greeting_key(bid)) == notifier.texts()[-1]
    assert notifier.sent[-1]["buttons"] == [[("🔁 Другой вариант", f"bday:regen:{bid}")]]
    await h.on_callback(cbq(bot, "bday:regen:999"))
    assert cb_answers(bot)[-1].text == "Этого дня рождения уже нет"


async def test_bday_send_via_userbot(h, deps, ctx, db, notifier, bot):
    bd = importlib.import_module("oracle.tools.birthdays")
    bid = await _add_bday(db)
    await h.on_callback(cbq(bot, f"bday:send:{bid}"))
    assert cb_answers(bot)[-1].show_alert is True and "Userbot" in cb_answers(bot)[-1].text
    ub = FakeUserbot()
    deps.userbot = ub
    await h.on_callback(cbq(bot, f"bday:send:{bid}"))
    assert "Текста поздравления нет" in cb_answers(bot)[-1].text
    await db.kv_set(bd.greeting_key(bid), "С днём рождения, Маша!")
    shown = "🎂 Сегодня ДР: Маша\n\nС днём рождения, Маша!"                # кнопка — под этим текстом
    await h.on_callback(cbq(bot, f"bday:send:{bid}", text=shown))
    assert ub.sent == [(555, "С днём рождения, Маша!")]
    assert ub.resolved == [("@masha_k", {"strict": True})]            # username — без угадывания по именам
    assert notifier.texts()[-1] == "📨 Отправил поздравление: Маша (@masha_k):\n«С днём рождения, Маша!»"
    await h.on_callback(cbq(bot, f"bday:send:{bid}", text=shown))  # второй раз — нет
    assert ub.sent == [(555, "С днём рождения, Маша!")]
    assert cb_answers(bot)[-1].text == "Поздравление уже отправлено"


async def test_bday_send_without_username(h, deps, db, bot):
    importlib.import_module("oracle.tools.birthdays")
    deps.userbot = FakeUserbot()
    bid = await _add_bday(db, tg="")
    await h.on_callback(cbq(bot, f"bday:send:{bid}"))
    assert "username" in cb_answers(bot)[-1].text


async def test_idea_deep_button(h, ctx, db, notifier, bot, fake_llm):
    ideas = importlib.import_module("oracle.tools.ideas")
    now = "2026-09-27T10:00:00+00:00"
    iid = await db.execute("INSERT INTO ideas(title, content, status, created_at, updated_at) VALUES(?,?,?,?,?)",
                           ("Кофейня", "у вокзала", "new", now, now))
    fake_llm.script = ["Разбор: рынок тесный.\nОценка: 5/10\nВердикт: проверить"]
    await h.on_callback(cbq(bot, f"idea:deep:{iid}"))
    assert cb_answers(bot)[-1].text == "Думаю, пришлю"
    await h.on_callback(cbq(bot, f"idea:deep:{iid}"))
    assert cb_answers(bot)[-1].text == "Уже думаю над ней — пришлю"
    await ctx.services.drain()
    assert any("Додумал идею" in t for t in notifier.texts())
    assert (await ideas.load_idea(db, iid))["score"] == 5
    await h.on_callback(cbq(bot, "idea:deep:999"))
    assert cb_answers(bot)[-1].text == "Этой идеи уже нет"                 # без имён инструментов


async def test_draft_buttons(h, deps, ctx, db, notifier, bot, fake_llm):
    tg = importlib.import_module("oracle.tools.tg_chats")
    await h.on_callback(cbq(bot, "draft:new:555"))                 # userbot не подключён
    assert "userbot не подключён" in notifier.texts()[-1]
    ub = FakeUserbot()
    deps.userbot = ub
    ctx.services.userbot = ub
    fake_llm.script = ["ща буду", "уже бегу"]
    await h.on_callback(cbq(bot, "draft:new:555"))
    assert notifier.texts()[-1] == "✍️ Черновик для «Маша»:\n\n```\nща буду\n```"
    did = int(notifier.sent[-1]["buttons"][0][0][1].split(":")[-1])

    await h.on_callback(cbq(bot, f"draft:regen:{did}"))
    (edit,) = bot.named("edit_message_text")
    assert "уже бегу" in edit["text"] and edit["parse_mode"] == "HTML"
    new_id = int(edit["reply_markup"].inline_keyboard[0][0].callback_data.split(":")[-1])
    assert new_id != did

    await h.on_callback(cbq(bot, f"draft:send:{did}"))              # старый вариант — отказ
    assert cb_answers(bot)[-1].show_alert is True
    assert ub.sent == []
    await h.on_callback(cbq(bot, f"draft:send:{new_id}"))
    assert ub.sent == [(555, "уже бегу")]
    assert notifier.texts()[-1] == "📨 Отправил в «Маша»."
    assert cb_answers(bot)[-1].text == "Отправлено"

    fake_llm.script = ["третий"]
    await h.on_callback(cbq(bot, "draft:new:555"))
    third = int(notifier.sent[-1]["buttons"][0][0][1].split(":")[-1])
    await h.on_callback(cbq(bot, f"draft:drop:{third}"))
    assert cb_answers(bot)[-1].text == "Ок, не отправляю"
    assert (await tg.get_draft(db, third))["status"] == "dropped"
    await h.on_callback(cbq(bot, f"draft:drop:{third}"))
    assert cb_answers(bot)[-1].text == "Черновик уже не актуален"


async def test_draft_regen_falls_back_to_new_message_when_edit_fails(h, deps, ctx, db, notifier, bot, fake_llm):
    importlib.import_module("oracle.tools.tg_chats")
    ub = FakeUserbot()
    deps.userbot = ub
    ctx.services.userbot = ub
    fake_llm.script = ["раз", "два"]
    await h.on_callback(cbq(bot, "draft:new:555"))
    did = int(notifier.sent[-1]["buttons"][0][0][1].split(":")[-1])
    bot.fail["edit_message_text"] = [bad_request("Bad Request: message can't be edited")]
    await h.on_callback(cbq(bot, f"draft:regen:{did}"))
    assert notifier.texts()[-1] == "✍️ Черновик для «Маша»:\n\n```\nдва\n```"


async def test_typing_indicator_repeats_while_waiting(h, deps, bot):
    class SlowAgent(FakeAgent):
        async def handle(self, text: str, *, via: str = "text", deep: bool | None = None, **_kw: Any) -> Reply:
            await asyncio.sleep(0.05)
            return Reply("ок")

    deps.agent = SlowAgent()
    await h.on_text(msg(bot, "подумай"))
    assert len(bot.named("send_chat_action")) >= 3
    n = len(bot.named("send_chat_action"))
    await asyncio.sleep(0.03)
    assert len(bot.named("send_chat_action")) == n               # после ответа — не печатает


# ── сквозной прогон через настоящий Dispatcher и Bot (сессия — запись вместо сети) ──
class RecordingSession(BaseSession):
    """Сессия aiogram без сети: запоминает методы и отвечает правдоподобными объектами."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list = []
        self._mid = itertools.count(500)

    async def make_request(self, bot: Any, method: Any, timeout: Any = None) -> Any:
        self.requests.append(method)
        ret = getattr(method, "__returning__", None)
        if ret is Message:
            return Message(message_id=next(self._mid), date=datetime(2026, 9, 28, 6, 0, tzinfo=UTC),
                           chat=Chat(id=int(getattr(method, "chat_id", OWNER) or OWNER), type="private"))
        if ret is File:
            return File(file_id=method.file_id, file_unique_id="u", file_path="voice/file_1.oga")
        if ret is User:
            return User(id=1, is_bot=True, first_name="Оракул", username="oracle_test_bot")
        return True

    async def stream_content(self, url: str, headers: Any = None, timeout: int = 30,
                             chunk_size: int = 65536, raise_for_status: bool = True):
        yield b"OggS-real-download"

    async def close(self) -> None:
        pass


@pytest.fixture
def live(cfg, deps):
    from aiogram import Bot
    from aiogram.types import Update

    from oracle import app
    session = RecordingSession()
    real_bot = Bot("123456:TEST-token", session=session)
    deps.notifier = BotNotifier(real_bot, OWNER)
    dp = app.build_dispatcher(cfg, deps)
    counter = itertools.count(1)

    async def feed(**kw: Any) -> list:
        before = len(session.requests)
        await dp.feed_update(real_bot, Update(update_id=next(counter), **kw))
        return session.requests[before:]

    return SimpleNamespace(bot=real_bot, session=session, dp=dp, feed=feed, deps=deps)


def _live_msg(text: str | None = None, uid: int = OWNER, **kw: Any) -> Message:
    return Message(message_id=next(_ids), date=datetime(2026, 9, 28, 6, 0, tzinfo=UTC),
                   chat=Chat(id=uid, type="private"), from_user=user(uid), text=text, **kw)


def _sent_texts(reqs: list) -> list[str]:
    return [r.text for r in reqs if type(r).__name__ == "SendMessage"]


async def test_live_owner_text_reaches_agent(live):
    reqs = await live.feed(message=_live_msg("привет"))
    assert live.deps.agent.calls == [("привет", "text", None)]
    assert _sent_texts(reqs) == ["Ответ агента <b>жирно</b>"]
    sent = [r for r in reqs if type(r).__name__ == "SendMessage"][0]
    assert sent.parse_mode == "HTML" and sent.link_preview_options.is_disabled is True
    assert any(type(r).__name__ == "SendChatAction" for r in reqs)


async def test_live_stranger_is_ignored(live):
    reqs = await live.feed(message=_live_msg("дай доступ", uid=9))
    assert live.deps.agent.calls == []
    assert _sent_texts(reqs) == [STRANGER_TEXT]


async def test_live_commands_and_unknown(live, db):
    reqs = await live.feed(message=_live_msg("/mode"))
    assert await db.kv_get("mode") == "deep" and "глубокий" in _sent_texts(reqs)[0]
    reqs = await live.feed(message=_live_msg("/foo"))
    assert _sent_texts(reqs) == ["Не знаю команды /foo. Список — /help."]
    reqs = await live.feed(message=_live_msg("/deep зачем"))
    assert live.deps.agent.calls == [("зачем", "text", True)]


async def test_live_voice_download_and_transcribe(live):
    await live.deps.db.kv_set("tts_mode", "mirror")
    reqs = await live.feed(message=_live_msg(voice=Voice(file_id="vf", file_unique_id="u", duration=2)))
    assert live.deps.stt.calls[0][0] == b"OggS-real-download"
    texts = _sent_texts(reqs)
    assert texts[0] == "🎙 <i>напомни завтра в девять</i>"
    assert live.deps.agent.calls == [("напомни завтра в девять", "voice", None)]
    assert any(type(r).__name__ == "SendVoice" for r in reqs)          # mirror


async def test_live_callbacks(live):
    reqs = await live.feed(callback_query=CallbackQuery(
        id="cq1", from_user=user(), chat_instance="ci", data="rem:nope:1",
        message=_live_msg("кнопки")))
    (ans,) = [r for r in reqs if type(r).__name__ == "AnswerCallbackQuery"]
    assert ans.text == H.STALE
    reqs = await live.feed(callback_query=CallbackQuery(
        id="cq2", from_user=user(9), chat_instance="ci", data="rem:done:1", message=_live_msg("кнопки")))
    (ans,) = [r for r in reqs if type(r).__name__ == "AnswerCallbackQuery"]
    assert ans.text == NO_ACCESS and ans.show_alert is True


async def test_voice_without_stt_mentions_specific_reason(h, deps, notifier, bot):
    stt = FakeSTT(ok=False)
    stt.reason = "STT_PROVIDER=groq, но нет GROQ_API_KEY."
    deps.stt = stt
    await h.on_voice(msg(bot, voice=Voice(file_id="f", file_unique_id="u", duration=1)))
    assert notifier.texts()[0].startswith("STT_PROVIDER=groq, но нет GROQ_API_KEY. Голосовые пока не понимаю")
    stt.reason = "распознавание голоса не настроено."
    await h.on_voice(msg(bot, voice=Voice(file_id="f", file_unique_id="u", duration=1)))
    assert notifier.texts()[1] == H.STT_HOWTO


async def test_notifier_resplits_emoji_heavy_chunks(bot):
    n = BotNotifier(bot, OWNER)
    await n.send("🙂" * 3000)                      # 3000 символов, но 6000 единиц UTF-16
    sent = bot.named("send_message")
    assert len(sent) >= 2
    assert all(len(kw["text"].encode("utf-16-le")) // 2 <= 4096 for kw in sent)
    assert "".join(kw["text"] for kw in sent) == "🙂" * 3000


async def test_live_photo_and_service_messages(live):
    from aiogram.types import PhotoSize
    reqs = await live.feed(message=_live_msg(photo=[PhotoSize(file_id="p", file_unique_id="u", width=1, height=1)]))
    assert _sent_texts(reqs) == [H.OTHER_MEDIA]
    reqs = await live.feed(message=_live_msg(new_chat_members=[user(5)]))
    assert _sent_texts(reqs) == [] and live.deps.agent.calls == []


async def test_app_main_full_wiring_without_network(monkeypatch, cfg):
    """main() целиком: база, инструменты, агент, планировщик, диспетчер — и аккуратная остановка.
    Telegram и polling подменены; ежедневные задачи выключены, чтобы никто не пошёл в сеть."""
    importlib.import_module("oracle.agent")
    importlib.import_module("oracle.services.scheduler")
    from aiogram import Bot, Dispatcher

    from oracle import app
    test_cfg = replace(cfg, bot_token="123456:TEST-token", morning_brief_time="", news_digest_time="",
                       reflection_time="", birthday_time="")
    monkeypatch.setattr(app.config, "load", lambda *a, **k: test_cfg)
    seen: dict[str, Any] = {}

    async def fake_get_me(self: Any, *a: Any, **k: Any) -> User:
        return User(id=1, is_bot=True, first_name="Оракул", username="oracle_test_bot")

    async def fake_set_commands(self: Any, commands: list, *a: Any, **k: Any) -> bool:
        seen["commands"] = [c.command for c in commands]
        return True

    async def fake_polling(self: Any, *bots: Any, **kw: Any) -> None:
        seen["allowed"] = kw.get("allowed_updates")
        # по умолчанию бот — для одного: один роутер, без управления людьми
        names = [r.name for r in self.sub_routers]
        assert names == ["oracle"]
        seen["handlers"] = len(self.sub_routers[0].message.handlers)
        await asyncio.sleep(0.05)                   # планировщик успевает сделать тик

    monkeypatch.setattr(Bot, "get_me", fake_get_me)
    monkeypatch.setattr(Bot, "set_my_commands", fake_set_commands)
    monkeypatch.setattr(Dispatcher, "start_polling", fake_polling)
    assert await app.main() == 0
    assert set(seen["allowed"]) == {"message", "edited_message", "callback_query"}
    assert seen["commands"][0] == "start" and "status" in seen["commands"]
    assert seen["handlers"] == len(H.COMMANDS) + 5


async def test_app_main_setup_mode_skips_agent(monkeypatch, cfg):
    from aiogram import Bot, Dispatcher

    from oracle import app
    monkeypatch.setattr(app.config, "load", lambda *a, **k: replace(cfg, bot_token="123456:TEST", owner_id=0))
    created: list = []

    async def fake_get_me(self: Any, *a: Any, **k: Any) -> User:
        return User(id=1, is_bot=True, first_name="Оракул", username="oracle_test_bot")

    async def ok(self: Any, *a: Any, **k: Any) -> bool:
        return True

    async def fake_polling(self: Any, *bots: Any, **kw: Any) -> None:
        created.append(self.sub_routers[0].oracle_handlers.d.agent)

    monkeypatch.setattr(Bot, "get_me", fake_get_me)
    monkeypatch.setattr(Bot, "set_my_commands", ok)
    monkeypatch.setattr(Dispatcher, "start_polling", fake_polling)
    assert await app.main() == 0
    assert created == [None]
