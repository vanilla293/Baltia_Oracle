"""Отправка владельцу через бота: реализация протокола `Notifier` из tools/base.py.

Длинный текст режется `split_message`, каждый кусок уходит как HTML (`md_to_html`) без
превью ссылок, кнопки — только под последним куском. Telegram не принял разметку
(TelegramBadRequest) — тот же кусок уходит простым текстом; попросил подождать
(TelegramRetryAfter) — ждём и пробуем ещё раз. Куски одного сообщения идут подряд:
чужая отправка (напоминание из планировщика) не вклинится посередине.
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
import re
from typing import Any

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import BufferedInputFile, LinkPreviewOptions

from ..tools.base import Buttons
from .keyboards import build
from .render import md_to_html, plain, split_message, utf16_len

log = logging.getLogger("oracle.bot.notifier")

CAPTION_MAX = 1000               # у Telegram — 1024 видимых символа
HTML_MAX = 4096
UTF16_SAFE = 4000                # видимый текст сообщения — до 4096 единиц UTF-16
RETRY_CAP = 120.0                # дольше ждать на флуд-контроле не будем
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
_TAGS = re.compile(r"<[^>]+>")


def html_to_text(html: str) -> str:
    """HTML Telegram → простой текст (для запасной отправки без разметки)."""
    return _html.unescape(_TAGS.sub("", html or ""))


class BotNotifier:
    """Всё, что бот пишет сам (ответы, напоминания, итоги фоновых задач), — в один чат."""

    resumable = True             # send(progress=…): повтор длинного текста — с первого недошедшего куска

    def __init__(self, bot: Any, chat_id: int):
        self.bot = bot
        self.chat_id = chat_id
        self._lock = asyncio.Lock()

    # ── низкий уровень ──
    async def _call(self, fn: Any, **kw: Any) -> Any:
        """Вызов метода бота; на TelegramRetryAfter — подождать и повторить один раз."""
        try:
            return await fn(**kw)
        except TelegramRetryAfter as e:
            wait = min(float(getattr(e, "retry_after", 1) or 1), RETRY_CAP)
            log.warning("Telegram просит подождать %.0f с", wait)
            await asyncio.sleep(wait)
            return await fn(**kw)

    async def _send_chunk(self, chunk: str, markup: Any, silent: bool) -> Any:
        base = {"chat_id": self.chat_id, "reply_markup": markup,
                "disable_notification": True if silent else None}
        try:
            return await self._call(self.bot.send_message, text=md_to_html(chunk), parse_mode="HTML",
                                    link_preview_options=NO_PREVIEW, **base)
        except TelegramBadRequest as e:
            log.warning("HTML не принят (%s) — отправляю простым текстом", e)
        text = plain(chunk).strip() or chunk
        try:
            return await self._call(self.bot.send_message, text=text, parse_mode=None,
                                    link_preview_options=NO_PREVIEW, **base)
        except TelegramBadRequest:
            if markup is None:
                raise
            log.exception("и простым текстом с кнопками не ушло — отправляю без кнопок")
            base["reply_markup"] = None
            return await self._call(self.bot.send_message, text=text, parse_mode=None,
                                    link_preview_options=NO_PREVIEW, **base)

    # ── протокол Notifier ──
    async def send(self, text: str, buttons: Buttons | None = None, *, silent: bool = False,
                   progress: list[int] | None = None) -> int | None:
        """Текст (markdown) → одно или несколько сообщений. → message_id последнего; пусто → None.
        progress — [сколько кусков уже дошло]: эти пропускаются, счётчик растёт после каждого дошедшего
        (оборвалось на третьем — повтор с тем же progress начнёт с третьего, первые два не задвоятся)."""
        chunks: list[str] = []
        for c in split_message(text or ""):
            if utf16_len(c) > UTF16_SAFE:          # эмодзи-простыня: в UTF-16 вдвое длиннее
                chunks += split_message(c, UTF16_SAFE // 2)
            else:
                chunks.append(c)
        chunks = [c for c in chunks if md_to_html(c).strip() or plain(c).strip()]
        if not chunks:
            return None
        markup = build(buttons)
        last = None
        if progress is not None and not progress:
            progress.append(0)
        start = max(0, int(progress[0])) if progress else 0
        async with self._lock:
            for i, chunk in enumerate(chunks):
                if i < start:
                    continue
                msg = await self._send_chunk(chunk, markup if i == len(chunks) - 1 else None, silent)
                last = getattr(msg, "message_id", None)
                if progress is not None:
                    progress[0] = i + 1
        return last

    async def send_html(self, html: str, buttons: Buttons | None = None, *, silent: bool = False) -> int | None:
        """Готовый HTML (уже экранированный) одним сообщением; не принят или слишком длинный —
        простым текстом (теги сняты, сущности раскрыты). → message_id последнего; пусто → None."""
        if not (html or "").strip():
            return None
        markup = build(buttons)
        quiet = True if silent else None
        async with self._lock:
            if len(html) <= HTML_MAX:
                try:
                    msg = await self._call(self.bot.send_message, chat_id=self.chat_id, text=html,
                                           parse_mode="HTML", link_preview_options=NO_PREVIEW,
                                           reply_markup=markup, disable_notification=quiet)
                    return getattr(msg, "message_id", None)
                except TelegramBadRequest as e:
                    log.warning("HTML не принят (%s) — отправляю простым текстом", e)
            chunks = split_message(html_to_text(html))
            last = None
            for i, chunk in enumerate(chunks):
                msg = await self._call(self.bot.send_message, chat_id=self.chat_id, text=chunk, parse_mode=None,
                                       link_preview_options=NO_PREVIEW,
                                       reply_markup=markup if i == len(chunks) - 1 else None,
                                       disable_notification=quiet)
                last = getattr(msg, "message_id", None)
            return last

    @staticmethod
    def _caption_kw(caption: str) -> tuple[dict, str]:
        """(аргументы подписи, текст, который уйдёт отдельным сообщением — если подпись длинная)."""
        caption = (caption or "").strip()
        if not caption:
            return {}, ""
        if len(caption) > CAPTION_MAX:
            return {}, caption
        return {"caption": md_to_html(caption), "parse_mode": "HTML"}, ""

    async def _send_media(self, method: Any, field: str, data: bytes, filename: str, caption: str) -> Any:
        kw, extra = self._caption_kw(caption)
        file = BufferedInputFile(bytes(data), filename=filename)
        async with self._lock:
            try:
                msg = await self._call(method, chat_id=self.chat_id, **{field: file}, **kw)
            except TelegramBadRequest:
                if not kw:
                    raise
                log.warning("подпись к файлу не принята — отправляю простым текстом")
                file = BufferedInputFile(bytes(data), filename=filename)
                msg = await self._call(method, chat_id=self.chat_id, **{field: file},
                                       caption=plain(caption)[:1024], parse_mode=None)
        if extra:
            await self.send(extra)
        return msg

    async def send_file(self, data: bytes, filename: str, caption: str = "") -> None:
        await self._send_media(self.bot.send_document, "document", data, filename or "file.bin", caption)

    async def send_voice(self, data: bytes, filename: str = "voice.mp3", caption: str = "") -> None:
        """Голосовое. Если у владельца запрещены голосовые (VOICE_MESSAGES_FORBIDDEN) — аудиофайлом."""
        try:
            await self._send_media(self.bot.send_voice, "voice", data, filename or "voice.mp3", caption)
        except TelegramBadRequest as e:
            if "VOICE_MESSAGES_FORBIDDEN" not in str(e).upper():
                raise
            log.info("голосовые запрещены настройками — отправляю аудиофайлом")
            await self._send_media(self.bot.send_audio, "audio", data, filename or "voice.mp3", caption)
