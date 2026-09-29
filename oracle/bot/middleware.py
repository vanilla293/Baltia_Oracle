"""Бот личный: слушается только владельца (OWNER_ID) и только в личке.

Внешний (outer) middleware на сообщения и нажатия кнопок. Владелец проходит дальше — но только
из личного чата с ботом: в группе, куда бота кто-то добавил, его /backup или /memory ушли бы
всем участникам, поэтому вне лички бот молчит (кнопкам отвечает «Нет доступа»).
OWNER_ID ещё не задан — на /start бот присылает человеку его id и подсказку, что вписать в .env.
Чужим: сообщения молча отбрасываются (раз в час — короткое «Это личный бот.»), кнопки
отвечают «Нет доступа».

Ещё два маленьких middleware:
  • `TurnOrder` — реплики владельца доходят до агента в том порядке, в каком пришли. aiogram
    обрабатывает каждое обновление отдельной задачей, и текст, отправленный сразу после
    голосового, иначе обгонял бы его (голос сначала скачивается и распознаётся). Билет берётся
    при входе, до первого await, и помнит время прихода (`turn_arrived`: «через 3 минуты» считается
    от него, а не от момента, когда до сообщения дошла очередь); скачивание и распознавание идут
    параллельно, а ход агента (`wait_turn`) ждёт, пока закончатся все более ранние сообщения, —
    и, если ждать приходится дольше пары секунд, говорит об этом (`on_wait`: «⏳ …в очереди»).
    Ответ ушёл текстом — очередь свободна (`finish_turn`), озвучка ответа следующих не держит.
    Команды, которым агент не нужен, свой билет сразу отпускают (`release_turn`) — /news не
    задерживает следующий вопрос. Пересылка с комментарием приходит двумя сообщениями (комментарий
    первым): комментарий забирает пересланное, пришедшее сразу следом, в свой ход (`take_following`),
    а у пересланного хода уже нет (`turn_absorbed`) — иначе «запиши его др» спросило бы «чей?».
  • `Inflight` — какие обновления сейчас в работе: при остановке их дожидаются, прежде чем
    закрывать базу и модель.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from .. import timeutil

log = logging.getLogger("oracle.bot.middleware")

STRANGER_TEXT = "Это личный бот."
NO_ACCESS = "Нет доступа"
SETUP_HINT = "Я ещё не настроен. Пришли /start — скажу твой Telegram id, его нужно вписать в .env."
REPLY_EVERY = 3600.0              # чужому — не чаще раза в час
_START = re.compile(r"^/start(?:@\w+)?(?:\s|$)", re.I)
_MEMORY_MAX = 1000


def is_owner(cfg: Any, user_id: Any) -> bool:
    """Это владелец? OWNER_ID не задан (0) — владельца нет ни у кого."""
    owner = int(getattr(cfg, "owner_id", 0) or 0)
    if not owner or user_id is None or isinstance(user_id, bool):
        return False
    try:
        return int(user_id) == owner
    except (TypeError, ValueError):
        return False


def id_text(user_id: int) -> str:
    """Ответ на /start, пока OWNER_ID не задан (HTML)."""
    return (f"Твой Telegram id: <code>{int(user_id)}</code>\n"
            f"Впиши в .env строку OWNER_ID={int(user_id)} и перезапусти бота — "
            f"после этого я буду слушаться только тебя.")


def is_start(text: Any) -> bool:
    return bool(isinstance(text, str) and _START.match(text.strip()))


def event_chat(event: Any) -> Any:
    """Чат события: у сообщения — его чат, у кнопки — чат сообщения с кнопкой (None — не знаем)."""
    msg = event if isinstance(event, Message) else getattr(event, "message", None)
    return getattr(msg, "chat", None)


class OwnerOnly(BaseMiddleware):
    """Пропускает только владельца и только из лички. Ставится как outer middleware на message
    и callback_query."""

    def __init__(self, cfg: Any, *, stranger_reply: bool = True, reply_every: float = REPLY_EVERY,
                 clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self.stranger_reply = stranger_reply
        self.reply_every = float(reply_every)
        self._clock = clock
        self._replied: dict[int, float] = {}
        self._warned: set[int] = set()

    def _may_reply(self, uid: int | None) -> bool:
        """Не чаще раза в reply_every на человека (и память не пухнет от спамеров)."""
        if uid is None:
            return False
        now = self._clock()
        last = self._replied.get(uid)
        if last is not None and now - last < self.reply_every:
            return False
        if len(self._replied) >= _MEMORY_MAX:
            cutoff = now - self.reply_every
            self._replied = {k: v for k, v in self._replied.items() if v > cutoff}
            if len(self._replied) >= _MEMORY_MAX:
                self._replied.clear()
        self._replied[uid] = now
        return True

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        user = getattr(event, "from_user", None)
        uid = getattr(user, "id", None)
        if is_owner(self.cfg, uid):
            chat = event_chat(event)
            chat_type = getattr(chat, "type", None)
            if chat_type == "private":
                return await handler(event, data)
            log.info("владелец написал из чата %s (%s) — вне лички не отвечаю", getattr(chat, "id", None),
                     chat_type)
            if isinstance(event, CallbackQuery):
                try:                                 # ответить надо всегда — иначе крутятся часики
                    await event.answer(NO_ACCESS)
                except Exception as e:
                    log.debug("ответ на кнопку вне лички не ушёл: %r", e)
            return None
        try:
            if isinstance(event, CallbackQuery):
                await event.answer(NO_ACCESS, show_alert=True)
            elif isinstance(event, Message):
                await self._stranger_message(event, uid)
        except Exception as e:   # ответ чужому — не повод падать
            log.debug("ответ постороннему не ушёл: %r", e)
        return None

    def _log_stranger(self, uid: int | None) -> None:
        """Первое сообщение от каждого чужого — в лог заметно: вдруг OWNER_ID вписан с ошибкой."""
        if uid is None or uid in self._warned:
            log.info("посторонний %s написал боту — игнорирую", uid)
            return
        if len(self._warned) >= _MEMORY_MAX:
            self._warned.clear()
        self._warned.add(uid)
        log.warning("посторонний %s написал боту — игнорирую. Если это ты, OWNER_ID в .env неверный: "
                    "впиши OWNER_ID=%s и перезапусти бота", uid, uid)

    async def _stranger_message(self, message: Message, uid: int | None) -> None:
        owner_set = bool(int(getattr(self.cfg, "owner_id", 0) or 0))
        chat_type = getattr(getattr(message, "chat", None), "type", "private")
        if not owner_set:
            if uid is not None and is_start(message.text):
                log.info("OWNER_ID не задан; /start от %s — отправляю ему id", uid)
                await message.answer(id_text(uid), parse_mode="HTML")
            elif chat_type == "private" and self._may_reply(uid):
                await message.answer(SETUP_HINT, parse_mode=None)
            return
        self._log_stranger(uid)
        if self.stranger_reply and chat_type == "private" and self._may_reply(uid):
            await message.answer(STRANGER_TEXT, parse_mode=None)


# ── порядок реплик ───────────────────────────────────────────────────────────
QUEUE_NOTICE_SEC = 3.0            # ждёт прошлые сообщения дольше — сказать владельцу, что оно в очереди


@dataclass
class _Ticket:
    prev: asyncio.Future | None           # билет сообщения, пришедшего перед этим
    mine: asyncio.Future                  # готов, когда это сообщение обработано (и все до него)
    waited: bool = False
    noticed: bool = False                 # владельцу сказали «в очереди»
    arrived: datetime = field(default_factory=timeutil.now_utc)    # когда сообщение пришло
    event: Any = field(default=None, repr=False)                    # само сообщение
    next: "_Ticket | None" = field(default=None, repr=False)        # билет сообщения, пришедшего следом
    absorbed: bool = False                # его забрал в свой ход предыдущий (пересылка с комментарием)


_ticket: contextvars.ContextVar[_Ticket | None] = contextvars.ContextVar("oracle_turn_ticket", default=None)


def _finish_after(prev: asyncio.Future | None, mine: asyncio.Future) -> None:
    """Пометить свой билет выполненным — но не раньше предыдущего: иначе быстрая команда,
    пришедшая после голосового, «отпустила» бы и тех, кто пришёл после неё."""
    def done(_f: Any = None) -> None:
        if not mine.done():
            mine.set_result(None)
    if prev is None or prev.done():
        done()
    else:
        prev.add_done_callback(done)


class TurnOrder(BaseMiddleware):
    """Очередь реплик владельца по порядку прихода. Ставится outer middleware на message ПОСЛЕ
    OwnerOnly: до обработчика у владельца ничего не ждёт, так что билеты берутся в порядке
    обновлений (фильтры роутера — уже потом, и они бывают асинхронными)."""

    def __init__(self) -> None:
        self._tail: asyncio.Future | None = None
        self._last: _Ticket | None = None

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        prev = self._tail
        mine = asyncio.get_running_loop().create_future()
        self._tail = mine
        ticket = _Ticket(prev, mine, event=event)
        if self._last is not None and self._last.mine is prev:
            self._last.next = ticket
        self._last = ticket
        token = _ticket.set(ticket)
        try:
            return await handler(event, data)
        finally:
            _ticket.reset(token)
            _finish_after(prev, mine)       # и при ошибке, и при отмене — очередь не встанет


def turn_pending() -> bool:
    """Этому сообщению ещё предстоит ждать более ранние (wait_turn не вернётся сразу)?"""
    t = _ticket.get()
    return t is not None and not t.waited and t.prev is not None and not t.prev.done()


def turn_arrived() -> datetime | None:
    """Когда пришло сообщение, которое сейчас обрабатывается (вне TurnOrder — None)."""
    t = _ticket.get()
    return t.arrived if t is not None else None


async def wait_turn(on_wait: Callable[[], Awaitable[Any]] | None = None,
                    after: float = QUEUE_NOTICE_SEC) -> bool:
    """Дождаться, пока обработаются все сообщения, пришедшие раньше этого (вне TurnOrder — сразу).
    Ждать пришлось дольше `after` секунд — один раз зовётся `on_wait` (сказать владельцу, что сообщение
    в очереди). → сказали ли ему об этом (и при повторном вызове для того же сообщения)."""
    t = _ticket.get()
    if t is None:
        return False
    if t.waited:
        return t.noticed
    t.waited = True
    if t.prev is None or t.prev.done():
        return False
    task: asyncio.Task | None = None
    if on_wait is not None:
        async def later() -> None:
            await asyncio.sleep(max(0.0, float(after or 0)))
            if t.absorbed:                   # его ответит предыдущий ход — «в очереди» не про него
                return
            t.noticed = True
            try:
                await on_wait()
            except asyncio.CancelledError:
                raise
            except Exception as e:           # уведомление — не повод ломать ответ
                log.warning("не сказал, что сообщение в очереди: %r", e)
        task = asyncio.ensure_future(later())
    try:
        await asyncio.shield(t.prev)
    finally:
        if task is not None:
            if not t.noticed:                # ещё не начали говорить — не надо
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)    # начали — договорить до ответа
    return t.noticed


def finish_turn() -> None:
    """Агенту это сообщение больше не нужно (ответ ушёл текстом, дальше — озвучка): следующие
    реплики его не ждут. Порядок между ними сохраняется."""
    t = _ticket.get()
    if t is not None:
        _finish_after(t.prev, t.mine)


async def take_following(pred: Callable[[Any], bool], within: float) -> list[Any]:
    """Сообщения, пришедшие сразу следом за этим (подряд, не позже within секунд) и подходящие под pred, —
    забрать в ход этого: их обработчики агента уже не зовут (`turn_absorbed`). Ход этого ещё не кончился,
    так что они ждут в очереди и своего хода не начинали. Сначала — отдать управление: обновления из той же
    пачки getUpdates берут билеты, как только им дадут выполниться."""
    t = _ticket.get()
    if t is None:
        return []
    await asyncio.sleep(0)
    out: list[Any] = []
    nxt = t.next
    while (nxt is not None and not nxt.absorbed and not t.mine.done() and pred(nxt.event)
           and (nxt.arrived - t.arrived).total_seconds() <= within):
        nxt.absorbed = True
        out.append(nxt.event)
        nxt = nxt.next
    return out


def turn_absorbed() -> bool:
    """Это сообщение забрал в свой ход предыдущий (пересылка с комментарием) — отвечать на него не нужно."""
    t = _ticket.get()
    return t is not None and t.absorbed


def finish_after(prev: asyncio.Future | None, mine: asyncio.Future) -> None:
    """Пометить mine выполненным не раньше prev (цепочка по порядку прихода: например, озвучка ответов)."""
    _finish_after(prev, mine)


def release_turn() -> None:
    """Этому сообщению агент не нужен (команда со своим ответом) — следующие реплики его не ждут,
    но порядок между ними сохраняется."""
    t = _ticket.get()
    if t is not None and not t.waited:
        _finish_after(t.prev, t.mine)


# ── что сейчас в работе ──────────────────────────────────────────────────────
class Inflight(BaseMiddleware):
    """Запоминает задачи, в которых сейчас обрабатываются обновления (для аккуратной остановки:
    aiogram их не ждёт). Ставится outer middleware на update."""

    def __init__(self) -> None:
        self.tasks: set[asyncio.Task] = set()

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        task = asyncio.current_task()
        if task is not None:
            self.tasks.add(task)
        try:
            return await handler(event, data)
        finally:
            if task is not None:
                self.tasks.discard(task)

    def pending(self) -> list[asyncio.Task]:
        me = asyncio.current_task()
        return [t for t in self.tasks if not t.done() and t is not me]
