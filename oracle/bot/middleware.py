"""Бот личный: слушается только владельца (OWNER_ID).

Внешний (outer) middleware на сообщения и нажатия кнопок. Владелец проходит дальше.
OWNER_ID ещё не задан — на /start бот присылает человеку его id и подсказку, что вписать в .env.
Чужим: сообщения молча отбрасываются (раз в час — короткое «Это личный бот.»), кнопки
отвечают «Нет доступа».
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

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


class OwnerOnly(BaseMiddleware):
    """Пропускает только владельца. Ставится как outer middleware на message и callback_query."""

    def __init__(self, cfg: Any, *, stranger_reply: bool = True, reply_every: float = REPLY_EVERY,
                 clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self.stranger_reply = stranger_reply
        self.reply_every = float(reply_every)
        self._clock = clock
        self._replied: dict[int, float] = {}

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
            return await handler(event, data)
        try:
            if isinstance(event, CallbackQuery):
                await event.answer(NO_ACCESS, show_alert=True)
            elif isinstance(event, Message):
                await self._stranger_message(event, uid)
        except Exception as e:   # ответ чужому — не повод падать
            log.debug("ответ постороннему не ушёл: %r", e)
        return None

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
        log.info("посторонний %s написал боту — игнорирую", uid)
        if self.stranger_reply and chat_type == "private" and self._may_reply(uid):
            await message.answer(STRANGER_TEXT, parse_mode=None)
