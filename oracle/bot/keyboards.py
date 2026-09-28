"""Кнопки: `Buttons` из tools/base.py → InlineKeyboardMarkup aiogram.

Ряды пар (надпись, callback_data). callback_data у Telegram — не длиннее 64 байт (UTF-8):
длиннее обрезается по границе символа, пустые надписи и данные пропускаются. Данные вида
http(s)://… или tg://… превращаются в кнопку-ссылку. Лимиты Telegram: до 8 кнопок в ряду,
до 100 на сообщение — лишнее отбрасывается, а не роняет отправку.
"""
from __future__ import annotations

import logging
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger("oracle.bot.keyboards")

CALLBACK_MAX_BYTES = 64
TEXT_MAX = 64
ROW_MAX = 8
TOTAL_MAX = 100
_URL_PREFIXES = ("http://", "https://", "tg://")


def clip_bytes(s: str, limit: int = CALLBACK_MAX_BYTES) -> str:
    """Строка не длиннее limit байт UTF-8 (без обрезанных посередине символов)."""
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    return b[:limit].decode("utf-8", errors="ignore")


def _pairs(row: Any) -> list[tuple[Any, Any]]:
    """Ряд → пары. Один кортеж ("надпись", "data") вместо ряда тоже понимаем."""
    if isinstance(row, (tuple, list)) and len(row) == 2 and all(isinstance(x, str) for x in row):
        return [(row[0], row[1])]
    if not isinstance(row, (list, tuple)):
        return []
    out = []
    for b in row:
        if isinstance(b, (tuple, list)) and len(b) >= 2:
            out.append((b[0], b[1]))
    return out


def button(text: Any, data: Any) -> InlineKeyboardButton | None:
    label = " ".join(str(text or "").split())[:TEXT_MAX]
    value = str(data or "").strip()
    if not label or not value:
        return None
    if value.startswith(_URL_PREFIXES):
        return InlineKeyboardButton(text=label, url=value)
    clipped = clip_bytes(value)
    if clipped != value:
        log.warning("callback_data длиннее %s байт — обрезаю: %r", CALLBACK_MAX_BYTES, value)
    return InlineKeyboardButton(text=label, callback_data=clipped)


def build(buttons: Any) -> InlineKeyboardMarkup | None:
    """Buttons → InlineKeyboardMarkup; пусто или ничего годного → None."""
    if not buttons or not isinstance(buttons, (list, tuple)):
        return None
    rows: list[list[InlineKeyboardButton]] = []
    total = 0
    for row in buttons:
        cur: list[InlineKeyboardButton] = []
        for text, data in _pairs(row):
            if total >= TOTAL_MAX:
                break
            b = button(text, data)
            if b is None:
                continue
            if len(cur) >= ROW_MAX:
                rows.append(cur)
                cur = []
            cur.append(b)
            total += 1
        if cur:
            rows.append(cur)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def to_buttons(markup: Any) -> list[list[tuple[str, str]]]:
    """InlineKeyboardMarkup → Buttons (только callback-кнопки) — чтобы пересобрать клавиатуру без одной кнопки."""
    rows = getattr(markup, "inline_keyboard", None) or []
    out = []
    for row in rows:
        cur = [(b.text, b.callback_data) for b in row if getattr(b, "callback_data", None)]
        if cur:
            out.append(cur)
    return out


def without(markup: Any, data: str) -> InlineKeyboardMarkup | None:
    """Та же клавиатура без кнопки с callback_data == data (None — если кнопок не осталось)."""
    rows = [[(t, d) for t, d in row if d != data] for row in to_buttons(markup)]
    return build([r for r in rows if r])
