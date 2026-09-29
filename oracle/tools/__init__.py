"""Инструменты агента. `load_all()` импортирует модули — они регистрируют себя в `base.REGISTRY`."""
from __future__ import annotations

import importlib

from .base import REGISTRY, OutItem, Services, ToolContext, dispatch, schemas, tool  # noqa: F401

MODULES = (
    "reminders", "calendar", "birthdays", "ideas", "projects", "memory", "news", "tg_chats", "settings",
)


def load_all() -> None:
    for m in MODULES:
        importlib.import_module(f"{__name__}.{m}")
