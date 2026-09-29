"""Userbot (фейковый Telethon-клиент), инструменты tg_* и черновики ответов, вход userbot_login."""
from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from conftest import FakeLLM, with_cfg
from oracle.llm import LLMError
from oracle.services import userbot as U
from oracle.services.userbot import Userbot
from oracle.tools import base as tb
from oracle.tools import tg_chats as tg

UTC = timezone.utc


# ── двойник Telethon ─────────────────────────────────────────────────────────
def user(uid, first, last=None, username=None, bot=False):
    return NS(id=uid, first_name=first, last_name=last, username=username, bot=bot, is_self=False, deleted=False)


def group(gid, title):
    return NS(id=gid, title=title, username=None)


def msg(text, out=False, minute=0, sender=None, media=None, action=None, fwd=None, hour=5):
    return NS(message=text, out=out, date=datetime(2026, 9, 28, hour, minute, tzinfo=UTC),
              sender=sender, media=media, action=action, fwd_from=fwd)


def dialog(did, name, entity, *, unread=0, last=None, kind="user", archived=False):
    return NS(id=did, name=name, title=name, entity=entity, unread_count=unread, message=last,
              date=last.date if last else None, is_user=kind == "user", is_group=kind == "group",
              is_channel=kind == "channel", archived=archived)


class FloodWaitError(Exception):
    def __init__(self, seconds):
        super().__init__(f"A wait of {seconds} seconds is required")
        self.seconds = seconds


class FakeClient:
    """То немногое из TelegramClient, что зовёт Userbot. messages — по порядку (старые первыми)."""

    def __init__(self, dialogs=(), messages=None, *, authorized=True, me=None, entities=None, cold=()):
        self.dialogs = list(dialogs)
        self.messages = dict(messages or {})
        self.authorized = authorized
        self.me = me or user(1, "Андрей", username="andrey")
        self.entities = dict(entities or {})
        self.cold = set(cold)            # id, которых «нет в кэше», пока не прогнали диалоги
        self.connected = False
        self.disconnects = 0
        self.sent: list[tuple] = []
        self.handlers: list[tuple] = []
        self.send_error: Exception | None = None
        self.connect_error: Exception | None = None
        self.dialog_scans: list = []

    async def connect(self):
        if self.connect_error:
            raise self.connect_error
        self.connected = True

    async def disconnect(self):
        self.connected = False
        self.disconnects += 1

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return self.me

    async def iter_dialogs(self, limit=None):
        self.dialog_scans.append(limit)
        self.cold.clear()
        for d in self.dialogs[: limit or None]:
            yield d

    async def get_messages(self, chat, limit=None):
        if chat in self.cold:
            raise ValueError(f"Could not find the input entity for PeerUser(user_id={chat})")
        items = list(reversed(self.messages.get(chat, [])))
        return items[:limit] if limit else items

    async def get_entity(self, key):
        if key in self.entities:
            return self.entities[key]
        for d in self.dialogs:
            if key == d.id:
                return d.entity
        raise ValueError(f'Cannot find any entity corresponding to "{key}"')

    def add_event_handler(self, callback, event=None):
        self.handlers.append((callback, event))

    async def send_message(self, chat, text, parse_mode=()):
        if self.send_error:
            raise self.send_error
        self.sent.append((chat, text, parse_mode))
        return NS(id=len(self.sent))


VASYA = user(101, "Вася", "Петров", username="vasya_p")
MASHA = user(102, "Маша")
MASHA_W = user(103, "Маша", "Работа")
SEREZHA = user(104, "Серёжа", "Иванов")
BOT = user(105, "Погодабот", username="weather_bot", bot=True)
SEMEN = user(106, "Семен", "Иваныч")
WORK = group(-100200, "Работа чат")
NEWS = group(-100300, "Новости канал")

VASYA_MSGS = [
    msg("привет, завтра в силе?", minute=0, sender=VASYA),
    msg("да, давай в 7 у метро", out=True, minute=1),
    msg("ок", out=True, minute=2),
    msg(None, minute=3, sender=VASYA, media=NS(photo=True)),
    msg("", minute=4, action=NS(kind="pin")),
    msg("слушай, а можешь пораньше? часов в 6", minute=5, sender=VASYA),
]


def make_client(**kw) -> FakeClient:
    dialogs = [
        dialog(101, "Вася Петров", VASYA, unread=2, last=VASYA_MSGS[-1]),
        dialog(-100300, "Новости канал", NEWS, unread=40, last=msg("x" * 500, minute=6), kind="channel"),
        dialog(102, "Маша", MASHA, unread=0, last=msg("люблю", out=True, minute=7)),
        dialog(-100200, "Работа чат", WORK, unread=5, last=msg(None, minute=8, media=NS(doc=1)), kind="group"),
        dialog(103, "Маша Работа", MASHA_W, unread=1, last=msg("отчёт где?", minute=9), archived=True),
        dialog(104, "Серёжа Иванов", SEREZHA, unread=0, last=msg("го", minute=10)),
        dialog(105, "Погодабот", BOT, unread=1, last=msg("+12", minute=11)),
        dialog(106, "Семен Иваныч", SEMEN, unread=0, last=msg("здрасьте", minute=12)),
    ]
    messages = {
        101: VASYA_MSGS,
        102: [msg("ты где", minute=0, sender=MASHA), msg("скоро буду, зай", out=True, minute=1),
              msg("люблю тебя очень", out=True, minute=2), msg("ок", out=True, minute=3)],
        104: [msg("погнали в субботу на рыбалку", out=True, minute=0),
              msg("да, давай в 7 у метро", out=True, minute=1)],       # дубль — не повторяем
        105: [msg("погода отличная сегодня", out=True, minute=0)],     # бот — не образец
        -100200: [msg("коллеги, отчёт готов", out=True, minute=0)],     # группа — не образец
    }
    kw.setdefault("entities", {"vasya_p": VASYA, "+79990001122": MASHA})
    return FakeClient(dialogs, messages, **kw)


@pytest.fixture
def ub_cfg(cfg):
    return with_cfg(cfg, userbot_enabled=True, tg_api_id=123, tg_api_hash="abc",
                    userbot_session=str(cfg.data_dir / "ub"))


@pytest.fixture
async def ub(ub_cfg, db, notifier, clock):
    client = make_client()
    u = Userbot(ub_cfg, db, notifier, client=client)
    await u.start()
    assert u.ready
    return u


# ── запуск ───────────────────────────────────────────────────────────────────
async def test_start_disabled_or_no_keys(cfg, db, clock):
    for c in (with_cfg(cfg, userbot_enabled=False, tg_api_id=1, tg_api_hash="h"),
              with_cfg(cfg, userbot_enabled=True, tg_api_id=0, tg_api_hash="h"),
              with_cfg(cfg, userbot_enabled=True, tg_api_id=1, tg_api_hash="")):
        client = make_client()
        u = Userbot(c, db, client=client)
        await u.start()
        assert u.ready is False and client.connected is False


async def test_start_without_login(ub_cfg, db, caplog, clock):
    client = make_client(authorized=False)
    u = Userbot(ub_cfg, db, client=client)
    with caplog.at_level("WARNING", logger="oracle.userbot"):
        await u.start()
    assert u.ready is False
    assert client.disconnects == 1 and client.connected is False
    assert "python -m oracle.userbot_login" in caplog.text
    with pytest.raises(ValueError, match="userbot не подключён"):
        await u.dialogs()
    for call in (u.resolve("Вася"), u.history(101), u.style_samples(101), u.send(101, "привет")):
        with pytest.raises(ValueError, match="USERBOT_ENABLED"):
            await call


async def test_start_connect_error_does_not_crash(ub_cfg, db, clock):
    client = make_client()
    client.connect_error = ConnectionError("нет сети")
    u = Userbot(ub_cfg, db, client=client)
    await u.start()
    assert u.ready is False


async def test_start_ok_and_notify_handler(ub_cfg, db, notifier, clock):
    client = make_client()
    u = Userbot(ub_cfg, db, notifier, client=client)
    await u.start()
    assert u.ready and u.me.id == 1 and client.handlers == []      # USERBOT_NOTIFY выключен
    await u.stop()
    assert not u.ready and client.connected is False

    client = make_client()
    u = Userbot(with_cfg(ub_cfg, userbot_notify=True), db, notifier, client=client)
    await u.start()
    assert len(client.handlers) == 1
    from telethon import events
    cb, ev = client.handlers[0]
    assert isinstance(ev, events.NewMessage)
    assert cb == u._on_new_message


async def test_start_chmods_session_file(ub_cfg, db, clock):
    path = U.session_file(ub_cfg.userbot_session)
    path.write_bytes(b"sqlite")
    os.chmod(path, 0o644)
    u = Userbot(ub_cfg, db, client=make_client())
    await u.start()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert U.session_file("a/b.session").name == "b.session"
    assert U.session_file("a/b").name == "b.session"


# ── уведомления о новых личных ───────────────────────────────────────────────
def event(sender, text="привет", *, private=True, chat_id=None, media=None):
    async def get_sender():
        return sender
    return NS(is_private=private, out=False, chat_id=chat_id if chat_id is not None else sender.id,
              get_sender=get_sender, message=NS(message=text, media=media))


async def test_notify_new_private_message(ub, notifier, clock):
    await ub._on_new_message(event(VASYA, "  здорово, как дела?  "))
    assert len(notifier.sent) == 1
    s = notifier.sent[0]
    assert s["text"] == "💬 Вася Петров: здорово, как дела?"
    assert s["buttons"] == [[("✍️ Предложить ответ", "draft:new:101")]] and s["silent"] is False
    # тот же чат в течение 5 минут — тишина, другой чат — можно
    clock.advance(minutes=4)
    await ub._on_new_message(event(VASYA, "алло"))
    await ub._on_new_message(event(MASHA, None, media=NS(photo=1)))
    assert notifier.texts()[-1] == "💬 Маша: [медиа]" and len(notifier.sent) == 2
    clock.advance(minutes=2)
    await ub._on_new_message(event(VASYA, "т" * 1000))
    assert len(notifier.sent) == 3
    assert len(notifier.texts()[-1]) <= len("💬 Вася Петров: ") + 300


async def test_notify_uses_escaped_html_when_notifier_can(ub, notifier, clock):
    """Чужой текст не рендерится как markdown: «[текст](ссылка)» не становится кликабельной ссылкой."""
    got: list = []

    async def send_html(html, buttons=None, *, silent=False):
        got.append((html, buttons, silent))

    notifier.send_html = send_html
    await ub._on_new_message(event(VASYA, "жми [сюда](https://evil.example) <b>срочно</b> & **важно**"))
    assert notifier.sent == []
    html, buttons, silent = got[0]
    assert html == ("💬 Вася Петров: жми [сюда](https://evil.example) &lt;b&gt;срочно&lt;/b&gt; &amp; **важно**")
    assert buttons == [[("✍️ Предложить ответ", "draft:new:101")]] and silent is False


async def test_notify_skips_bots_groups_and_self(ub, notifier):
    await ub._on_new_message(event(BOT, "погода"))
    await ub._on_new_message(event(VASYA, "в группе", private=False, chat_id=-100200))
    await ub._on_new_message(event(ub.me, "заметка себе"))
    await ub._on_new_message(NS(is_private=True, out=True))          # своё исходящее

    async def nobody():
        return None
    await ub._on_new_message(NS(is_private=True, out=False, chat_id=7, get_sender=nobody,
                                message=NS(message="?", media=None)))
    assert notifier.sent == []


async def test_notify_never_raises(ub, notifier):
    async def boom():
        raise RuntimeError("сеть")
    await ub._on_new_message(NS(is_private=True, out=False, get_sender=boom))
    assert notifier.sent == []


# ── диалоги ──────────────────────────────────────────────────────────────────
async def test_dialogs_recent(ub):
    items = await ub.dialogs(limit=4)
    assert [d["id"] for d in items] == [101, -100300, 102, -100200]
    v = items[0]
    assert v == {"id": 101, "title": "Вася Петров", "unread": 2,
                 "last_text": "слушай, а можешь пораньше? часов в 6", "last_out": False,
                 "last_date": "пн 28.09 08:05", "kind": "user"}
    assert items[1]["kind"] == "channel" and len(items[1]["last_text"]) <= 200
    assert items[1]["last_text"].endswith("…")
    assert items[2]["last_out"] is True
    assert items[3]["kind"] == "group" and items[3]["last_text"] == "[медиа]"


async def test_dialogs_unread_only(ub):
    items = await ub.dialogs(limit=20, unread_only=True)
    # личные первыми, потом группы, потом каналы; архив и прочитанные мимо
    assert [d["id"] for d in items] == [101, 105, -100200, -100300]
    assert all(d["unread"] > 0 for d in items)
    assert ub.client.dialog_scans[-1] >= 300            # для непрочитанных просматриваем с запасом
    assert len(await ub.dialogs(limit=1, unread_only=True)) == 1


async def test_dialogs_error_is_russian(ub):
    async def broken(limit=None):
        raise FloodWaitError(30)
        yield  # pragma: no cover
    ub.client.iter_dialogs = broken
    with pytest.raises(ValueError, match="подождать 30 с"):
        await ub.dialogs()


# ── поиск чата ───────────────────────────────────────────────────────────────
async def test_resolve_ids_and_usernames(ub):
    assert await ub.resolve(101) == (101, "Вася Петров")
    assert await ub.resolve("101") == (101, "Вася Петров")
    assert await ub.resolve("-100200") == (-100200, "Работа чат")
    assert await ub.resolve("@vasya_p") == (101, "Вася Петров")
    assert await ub.resolve("https://t.me/vasya_p") == (101, "Вася Петров")
    assert await ub.resolve("+79990001122") == (102, "Маша")
    with pytest.raises(ValueError, match="не нашёл чат с id 999"):
        await ub.resolve(999)
    with pytest.raises(ValueError, match="не нашёл"):
        await ub.resolve("+70000000000")
    for bad in ("", "  ", None, True):
        with pytest.raises(ValueError, match="какой чат"):
            await ub.resolve(bad)


async def test_resolve_fuzzy(ub):
    assert await ub.resolve("Маша") == (102, "Маша")               # точное совпадение важнее «Маша Работа»
    assert await ub.resolve("маша работа") == (103, "Маша Работа")
    assert await ub.resolve("петров") == (101, "Вася Петров")
    assert await ub.resolve("«Вася»") == (101, "Вася Петров")
    assert await ub.resolve("семён") == (106, "Семен Иваныч")      # ё = е
    assert await ub.resolve("Серёже") == (104, "Серёжа Иванов")    # падеж — по основе
    assert await ub.resolve("vasya") == (101, "Вася Петров")       # по username
    assert await ub.resolve("@Погодабот") == (105, "Погодабот")    # не username — ищем по названию


async def test_resolve_ambiguous_and_missing(ub):
    with pytest.raises(ValueError) as ei:
        await ub.resolve("работа")
    m = str(ei.value)
    assert "несколько" in m and "id -100200" in m and "id 103" in m
    with pytest.raises(ValueError, match="не нашёл чат «Зоопарк»"):
        await ub.resolve("Зоопарк")


async def test_resolve_ambiguous_lists_at_most_five(ub):
    ub.client.dialogs = [dialog(200 + i, f"Иван {i}", user(200 + i, "Иван", str(i))) for i in range(8)]
    with pytest.raises(ValueError) as ei:
        await ub.resolve("иван")
    m = str(ei.value)
    assert m.count("(id ") == 5 and "и ещё 3" in m


# ── история ──────────────────────────────────────────────────────────────────
async def test_history_chronological(ub):
    h = await ub.history(101)
    assert [(m["sender"], m["text"], m["out"]) for m in h] == [
        ("Вася Петров", "привет, завтра в силе?", False),
        ("я", "да, давай в 7 у метро", True),
        ("я", "ок", True),
        ("Вася Петров", "[медиа]", False),
        ("Вася Петров", "слушай, а можешь пораньше? часов в 6", False),
    ]                                                    # служебное сообщение пропущено
    assert h[0]["date"] == "пн 28.09 08:00"
    # служебное тоже занимает место в limit (так считает Telegram), но в ответ не попадает
    assert [m["text"] for m in await ub.history(101, limit=3)] == ["[медиа]", "слушай, а можешь пораньше? часов в 6"]
    assert (await ub.history("Вася"))[0]["text"] == "привет, завтра в силе?"
    assert await ub.history(999) == []


async def test_history_warms_entity_cache(ub_cfg, db, clock):
    client = make_client(cold={101})
    u = Userbot(ub_cfg, db, client=client)
    await u.start()
    h = await u.history(101)
    assert len(h) == 5 and client.dialog_scans            # прогрел диалоги и повторил


async def test_history_error_is_russian(ub):
    async def broken(chat, limit=None):
        raise ConnectionError("reset")
    ub.client.get_messages = broken
    with pytest.raises(ValueError, match="не смог прочитать чат: нет связи с Telegram"):
        await ub.history(101)


# ── образцы стиля ────────────────────────────────────────────────────────────
async def test_style_samples(ub):
    s = await ub.style_samples(101)
    # сначала этот чат (свежие первыми), «ок» — одно слово, мимо; потом личные: Маша, Серёжа; бот и группа — нет
    assert s == ["да, давай в 7 у метро", "люблю тебя очень", "скоро буду, зай", "погнали в субботу на рыбалку"]
    assert await ub.style_samples(101, limit=2) == ["да, давай в 7 у метро", "люблю тебя очень"]
    s = await ub.style_samples()
    assert "погода отличная сегодня" not in s and "коллеги, отчёт готов" not in s


async def test_style_samples_skip_forwards_and_survive_errors(ub):
    ub.client.messages[101] = [msg("чужой пересланный текст тут", out=True, fwd=NS(x=1)),
                               msg("мой собственный текст", out=True)]
    real = ub.client.get_messages

    async def flaky(chat, limit=None):
        if chat == 102:
            raise ConnectionError("reset")
        return await real(chat, limit=limit)
    ub.client.get_messages = flaky
    s = await ub.style_samples(101)
    assert s[0] == "мой собственный текст" and "чужой пересланный текст тут" not in s
    assert "погнали в субботу на рыбалку" in s


# ── отправка ─────────────────────────────────────────────────────────────────
async def test_send(ub):
    assert await ub.send(101, "  в 6 норм  ") is True
    assert ub.client.sent == [(101, "в 6 норм", None)]         # без markdown-разбора
    with pytest.raises(ValueError, match="пустое"):
        await ub.send(101, "   ")
    with pytest.raises(ValueError, match="4096"):
        await ub.send(101, "а" * 5000)
    ub.client.send_error = FloodWaitError(12)
    with pytest.raises(ValueError, match="не отправил: Telegram просит подождать 12 с"):
        await ub.send(101, "ещё")


# ── инструменты и черновики ──────────────────────────────────────────────────
def tool_names(cfg) -> set[str]:
    return {s["function"]["name"] for s in tb.schemas(cfg)}


def test_tools_only_when_enabled(cfg):
    tg_tools = {"tg_list_chats", "tg_read_chat", "tg_draft_reply"}
    assert not tg_tools & tool_names(with_cfg(cfg, userbot_enabled=False))
    names = tool_names(with_cfg(cfg, userbot_enabled=True))
    assert tg_tools <= names
    # у модели нет инструмента отправки в чужие чаты
    assert not [n for n in names if n.startswith("tg_") and "send" in n]
    assert set(tb.REGISTRY["tg_draft_reply"].parameters["required"]) == {"chat"}


async def call(ctx, name, /, **args) -> dict:
    return json.loads(await tb.dispatch(name, args, ctx))


@pytest.fixture
def uctx(ctx, ub, ub_cfg):
    ctx.cfg = ub_cfg
    ctx.services.userbot = ub
    return ctx


async def test_tools_need_ready_userbot(ctx, ub_cfg):
    ctx.cfg = ub_cfg
    r = await call(ctx, "tg_list_chats")
    assert r["ok"] is False and "python -m oracle.userbot_login" in r["error"]
    ctx.services.userbot = NS(ready=False)
    r = await call(ctx, "tg_read_chat", chat="Вася")
    assert r["ok"] is False and "USERBOT_ENABLED" in r["error"]
    r = await call(ctx, "tg_list_chats")
    assert r["ok"] is False
    # выключен в настройках — инструмента для модели просто нет
    ctx.cfg = with_cfg(ub_cfg, userbot_enabled=False)
    r = await call(ctx, "tg_list_chats")
    assert "нет такого инструмента" in r["error"]


async def test_tg_list_and_read(uctx):
    r = await call(uctx, "tg_list_chats")
    assert r["ok"] and r["count"] == 4 and r["chats"][0]["id"] == 101
    r = await call(uctx, "tg_list_chats", unread_only=False, limit="2")
    assert [c["id"] for c in r["chats"]] == [101, -100300]
    r = await call(uctx, "tg_list_chats", unread_only="нет", limit=500)
    assert r["count"] == 8
    r = await call(uctx, "tg_read_chat", chat="Вася", limit=4)
    assert r["ok"] and r["chat_id"] == 101 and r["chat"] == "Вася Петров" and r["count"] == 3
    assert r["messages"][-1]["text"] == "слушай, а можешь пораньше? часов в 6"
    r = await call(uctx, "tg_read_chat", chat="работа")
    assert r["ok"] is False and "несколько" in r["error"]


async def test_tg_list_empty_note(uctx):
    for d in uctx.services.userbot.client.dialogs:
        d.unread_count = 0
    r = await call(uctx, "tg_list_chats")
    assert r["ok"] and r["count"] == 0 and r["note"] == "непрочитанных нет"


async def test_draft_reply_saves_and_shows(uctx):
    uctx.cfg = with_cfg(uctx.cfg, owner_name="Андрей")
    uctx.llm = FakeLLM(["«Ответ: в 6 норм, давай»"])
    r = await call(uctx, "tg_draft_reply", chat="Вася", instruction="соглашайся на 6")
    assert r["ok"] is True
    did = r["draft_id"]
    assert r["chat"] == "Вася Петров" and r["draft"] == "в 6 норм, давай"
    assert "НЕ отправляешь" in r["note"] and "text" not in r and "buttons" not in r
    row = await uctx.db.fetchone("SELECT * FROM drafts WHERE id=?", (did,))
    assert row["chat_id"] == 101 and row["chat_title"] == "Вася Петров" and row["status"] == "pending"
    assert row["incoming"] == "слушай, а можешь пораньше? часов в 6"
    assert row["draft"] == "в 6 норм, давай"
    assert await uctx.db.kv_get(f"draft_instr:{did}") == "соглашайся на 6"
    item = uctx.outbox[-1]
    assert item.kind == "text" and item.text == "✍️ Черновик для «Вася Петров»:\n\n```\nв 6 норм, давай\n```"
    assert item.buttons == [[("📨 Отправить", f"draft:send:{did}"), ("🔁 Переписать", f"draft:regen:{did}")],
                            [("✖️ Не надо", f"draft:drop:{did}")]]
    # что ушло модели
    c = uctx.llm.calls[0]
    assert c["temperature"] == 0.9 and c["deep"] is False
    system, user_msg = c["messages"][0]["content"], c["messages"][1]["content"]
    assert "ОТ ИМЕНИ ВЛАДЕЛЬЦА (Андрей)" in system and "ИИ" in system
    assert "- да, давай в 7 у метро" in system and "- люблю тебя очень" in system   # образцы стиля
    assert "Вася Петров: слушай, а можешь пораньше? часов в 6" in user_msg
    assert "я: да, давай в 7 у метро" in user_msg
    assert "соглашайся на 6" in user_msg
    assert user_msg.index("привет, завтра в силе?") < user_msg.index("слушай, а можешь пораньше")
    # в callback_data укладываемся
    for row_ in item.buttons:
        for _, data in row_:
            assert len(data.encode()) <= 64


async def test_make_draft_by_id_for_notification_button(uctx):
    uctx.llm = FakeLLM(["давай"])
    r = await tg.make_draft(uctx, 101)
    assert r["ok"] and r["chat_id"] == 101 and r["text"].startswith("✍️ Черновик для «Вася Петров»")
    assert r["buttons"][0][0][1] == f"draft:send:{r['draft_id']}"
    assert await uctx.db.kv_get(f"draft_instr:{r['draft_id']}") is None


async def test_make_draft_edge_cases(uctx):
    with pytest.raises(ValueError, match="в какой чат"):
        await tg.make_draft(uctx, "  ")
    uctx.services.userbot.client.messages[106] = []
    with pytest.raises(ValueError, match="переписки с «Семен Иваныч» нет"):
        await tg.make_draft(uctx, "Семен")
    uctx.llm = FakeLLM(["Здравствуйте, Семён!"])
    r = await tg.make_draft(uctx, "Семен", "поздоровайся")
    assert r["draft"] == "Здравствуйте, Семён!"
    assert "образцов нет" not in uctx.llm.calls[0]["messages"][0]["content"]
    uctx.llm = FakeLLM(['""'])
    with pytest.raises(ValueError, match="пустой черновик"):
        await tg.make_draft(uctx, "Вася")

    def boom(messages, kw):
        raise LLMError("слишком много запросов к модели (429)")
    uctx.llm = FakeLLM([boom])
    r = await call(uctx, "tg_draft_reply", chat="Вася")
    assert r["ok"] is False and "не смог написать черновик" in r["error"] and "429" in r["error"]


async def test_draft_without_style_samples(uctx):
    ub = uctx.services.userbot

    async def none(chat=None, limit=25):
        return []
    ub.style_samples = none
    uctx.llm = FakeLLM(["ок"])
    await tg.make_draft(uctx, "Вася")
    assert "образцов нет" in uctx.llm.calls[0]["messages"][0]["content"]


async def test_send_draft_once(uctx):
    uctx.llm = FakeLLM(["в 6 норм"])
    did = (await tg.make_draft(uctx, "Вася"))["draft_id"]
    text = await tg.send_draft(uctx, did)
    assert text == "📨 Отправил в «Вася Петров»."
    assert uctx.services.userbot.client.sent == [(101, "в 6 норм", None)]
    row = await tg.get_draft(uctx.db, did)
    assert row["status"] == "sent"
    ev = (await uctx.db.recent_messages(5))[-1]
    assert ev["role"] == "event" and "в 6 норм" in ev["content"]
    with pytest.raises(ValueError, match="уже отправлен"):
        await tg.send_draft(uctx, did)
    assert len(uctx.services.userbot.client.sent) == 1
    with pytest.raises(ValueError, match="черновика #999 нет"):
        await tg.send_draft(uctx, 999)
    with pytest.raises(ValueError, match="числом"):
        await tg.send_draft(uctx, "abc")


async def test_send_draft_failure_keeps_pending(uctx):
    uctx.llm = FakeLLM(["в 6 норм"])
    did = (await tg.make_draft(uctx, "Вася"))["draft_id"]
    uctx.services.userbot.client.send_error = ConnectionError("reset")
    with pytest.raises(ValueError) as ei:
        await tg.send_draft(uctx, str(did))
    assert str(ei.value) == "не отправил в «Вася Петров»: нет связи с Telegram"
    assert (await tg.get_draft(uctx.db, did))["status"] == "pending"
    uctx.services.userbot.client.send_error = None
    assert "Отправил" in await tg.send_draft(uctx, f"#{did}")
    # userbot отвалился — отказ без смены статуса
    uctx.llm = FakeLLM(["ещё"])
    did2 = (await tg.make_draft(uctx, "Вася"))["draft_id"]
    uctx.services.userbot.ready = False
    with pytest.raises(ValueError, match="userbot не подключён"):
        await tg.send_draft(uctx, did2)
    assert (await tg.get_draft(uctx.db, did2))["status"] == "pending"


async def test_regen_draft(uctx):
    uctx.llm = FakeLLM(["в 6 норм", "не, в 6 не успею, давай в 7"])
    first = await tg.make_draft(uctx, "Вася", "откажись")
    res = await tg.regen_draft(uctx, first["draft_id"])
    assert res["ok"] and res["replaced"] == first["draft_id"] and res["draft_id"] != first["draft_id"]
    assert res["draft"] == "не, в 6 не успею, давай в 7"
    assert res["buttons"][0][0][1] == f"draft:send:{res['draft_id']}"
    assert (await tg.get_draft(uctx.db, first["draft_id"]))["status"] == "dropped"
    new = await tg.get_draft(uctx.db, res["draft_id"])
    assert new["status"] == "pending" and new["chat_id"] == 101 and new["incoming"]
    assert await uctx.db.kv_get(f"draft_instr:{res['draft_id']}") == "откажись"
    user_msg = uctx.llm.calls[1]["messages"][1]["content"]
    assert "откажись" in user_msg and "Прошлый вариант не подошёл" in user_msg and "в 6 норм" in user_msg
    assert len(uctx.outbox) == 2 and uctx.outbox[-1].text.endswith("давай в 7\n```")
    # старые кнопки больше не работают
    with pytest.raises(ValueError, match="заменён"):
        await tg.send_draft(uctx, first["draft_id"])
    with pytest.raises(ValueError, match="заменён"):
        await tg.regen_draft(uctx, first["draft_id"])
    with pytest.raises(ValueError, match="нет"):
        await tg.regen_draft(uctx, 12345)


async def test_drop_draft(uctx):
    uctx.llm = FakeLLM(["в 6 норм"])
    did = (await tg.make_draft(uctx, "Вася"))["draft_id"]
    assert await tg.drop_draft(uctx.db, did) is True
    assert await tg.drop_draft(uctx.db, did) is False
    assert await tg.drop_draft(uctx.db, "мусор") is False
    with pytest.raises(ValueError, match="отменён"):
        await tg.send_draft(uctx, did)
    assert uctx.services.userbot.client.sent == []


def test_clean_draft():
    assert tg.clean_draft("«да, давай»") == "да, давай"
    assert tg.clean_draft('"ок"') == "ок"
    assert tg.clean_draft("Ответ: норм") == "норм"
    assert tg.clean_draft("Ответ: «норм»") == "норм"
    assert tg.clean_draft("```\nпривет\n```") == "привет"
    assert tg.clean_draft("«Это» и «то»") == "«Это» и «то»"
    assert tg.clean_draft("  ") == ""
    assert len(tg.clean_draft("а" * 10000)) == tg.DRAFT_MAX


# ── вход ─────────────────────────────────────────────────────────────────────
def test_login_requires_api_keys(monkeypatch, capsys, tmp_path):
    from oracle import config, userbot_login
    from oracle.config import Settings
    monkeypatch.setattr(config, "load", lambda *a, **k: Settings(data_dir=tmp_path))
    assert userbot_login.main([]) == 2
    out = capsys.readouterr().out
    assert "TG_API_ID" in out and "my.telegram.org" in out and "API development tools" in out


def test_login_happy_path(monkeypatch, capsys, tmp_path):
    import telethon
    from oracle import config, userbot_login
    from oracle.config import Settings
    session = tmp_path / "sess" / "userbot"
    monkeypatch.setattr(config, "load", lambda *a, **k: Settings(
        data_dir=tmp_path, tg_api_id=7, tg_api_hash="h", userbot_session=str(session)))
    box: dict = {}
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda p="": prompts.append(p) or "+79990001122")
    monkeypatch.setattr(userbot_login.getpass, "getpass", lambda p="": prompts.append(p) or "secret")

    class FakeTC:
        def __init__(self, sess, api_id, api_hash, **kw):
            box["init"] = (sess, api_id, api_hash, kw)

        async def start(self, phone, password, code_callback=None):
            box["phone"] = phone()
            box["password"] = password()
            f = U.session_file(box["init"][0])
            f.write_bytes(b"sqlite")
            os.chmod(f, 0o644)

        async def get_me(self):
            return user(55, "Андрей", username="andrey")

        async def disconnect(self):
            box["disconnected"] = True

    monkeypatch.setattr(telethon, "TelegramClient", FakeTC)
    assert userbot_login.main([]) == 0
    assert box["init"][:3] == (str(session), 7, "h")
    assert box["init"][3] == {"device_model": "Baltia Oracle", "app_version": "0.1"}
    assert box["phone"] == "+79990001122" and box["password"] == "secret" and box["disconnected"]
    assert prompts == ["Телефон (+7…): ", "Пароль 2FA (если есть): "]
    f = U.session_file(session)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert "Андрей (@andrey), id 55" in out and "полный доступ" in out and "USERBOT_ENABLED" in out


def test_login_failure_is_reported(monkeypatch, capsys, tmp_path):
    import telethon
    from oracle import config, userbot_login
    from oracle.config import Settings
    monkeypatch.setattr(config, "load", lambda *a, **k: Settings(
        data_dir=tmp_path, tg_api_id=7, tg_api_hash="h", userbot_session=str(tmp_path / "u")))

    class FakeTC:
        def __init__(self, *a, **kw):
            pass

        async def start(self, **kw):
            raise RuntimeError("PHONE_NUMBER_INVALID")

        async def disconnect(self):
            pass

    monkeypatch.setattr(telethon, "TelegramClient", FakeTC)
    assert userbot_login.main([]) == 1
    assert "PHONE_NUMBER_INVALID" in capsys.readouterr().out
