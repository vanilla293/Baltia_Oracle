"""Управление людьми — только для главного (первый в OWNER_ID).

Кнопки из запроса доступа: `user:add:<id>` — пустить, `user:deny:<id>` — отказать; `/users` — кто сейчас
пользуется ботом, с кнопками `user:del:<id>` — убрать (его пространство засыпает, данные остаются).
Роутер подключается раньше пространств: иначе кнопку главного перехватил бы его же роутер («Кнопка устарела»).
"""
from __future__ import annotations

import logging
from html import escape
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from .keyboards import build

log = logging.getLogger("oracle.bot.admin")


def parse_user_cb(data: Any) -> tuple[str, int] | None:
    """'user:add:123' → ('add', 123); кривое → None."""
    parts = str(data or "").split(":")
    if len(parts) != 3 or parts[0] != "user" or parts[1] not in ("add", "deny", "del"):
        return None
    try:
        uid = int(parts[2])
    except ValueError:
        return None
    return (parts[1], uid) if uid > 0 else None


def users_text(rows: list[dict]) -> tuple[str, list[list[tuple[str, str]]]]:
    """Текст /users (HTML) и кнопки «убрать» для тех, кого пустили кнопкой."""
    lines = ["👥 <b>Кто пользуется ботом</b> — у каждого своё пространство:"]
    buttons: list[list[tuple[str, str]]] = []
    for r in rows:
        name = escape(r.get("name") or "без имени")
        tag = "ты" if r.get("primary") else ("из .env" if r.get("static") else "пущен кнопкой")
        lines.append(f"• {name} — <code>{int(r['uid'])}</code> ({tag})")
        if not r.get("primary") and not r.get("static"):
            buttons.append([(f"✖️ Убрать {r.get('name') or r['uid']}"[:40], f"user:del:{int(r['uid'])}")])
    if len(rows) == 1:
        lines.append("\nПока только ты. Второй человек пишет боту /start — тебе придёт запрос с кнопкой «Пустить».")
    return "\n".join(lines), buttons


def build_admin_router(tenants: Any) -> Router:
    router = Router(name="oracle-admin")
    primary = int(tenants.primary_id)
    router.message.filter(F.from_user.id == primary)
    router.callback_query.filter(F.from_user.id == primary)

    @router.callback_query(F.data.startswith("user:"))
    async def on_user_button(cb: CallbackQuery) -> None:
        parsed = parse_user_cb(cb.data)
        if parsed is None:
            await cb.answer("Кнопка устарела")
            return
        action, uid = parsed
        try:
            if action == "add":
                await tenants.approve(uid)
                note, text = "Пустил", f"✅ Пустил <code>{uid}</code> — у него своё пространство, я ему написал."
            elif action == "deny":
                await tenants.deny(uid)
                note, text = "Отказал", f"✖️ Не пустил <code>{uid}</code>. Больше его запросы не покажу."
            else:
                removed = await tenants.remove(uid)
                note = "Убрал" if removed else "Его уже нет"
                text = (f"Убрал <code>{uid}</code>: бот ему больше не отвечает, его данные лежат в "
                        f"data/users/{uid}/ — вернуть можно, пустив снова.") if removed else None
        except ValueError as e:
            await cb.answer(str(e)[:190], show_alert=True)
            return
        except Exception as e:
            log.exception("управление людьми: %s %s", action, uid)
            await cb.answer(f"Не вышло: {type(e).__name__}", show_alert=True)
            return
        await cb.answer(note)
        if text and cb.message is not None:
            try:
                await cb.message.edit_text(text, parse_mode="HTML")
            except Exception:
                await cb.message.answer(text, parse_mode="HTML")

    @router.message(Command("users", ignore_case=True), ~F.forward_origin)
    async def cmd_users(message: Message) -> None:
        text, buttons = users_text(await tenants.listing())
        await message.answer(text, parse_mode="HTML", reply_markup=build(buttons) if buttons else None)

    return router
