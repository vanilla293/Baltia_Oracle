"""Инструменты агента: реестр, контекст вызова, исходящие вложения.

Модуль инструмента регистрирует функции декоратором `@tool(...)`. Обработчик —
`async def h(ctx: ToolContext, **args) -> dict | str`. Результат уходит модели как JSON.
Ошибки ловит `dispatch` и отдаёт модели {"ok": false, "error": "..."} — бот не падает.

Всё, что инструмент хочет показать владельцу помимо ответа модели (файл .ics, черновик
с кнопками «Отправить / Переписать»), он кладёт в `ctx.outbox` — бот отправит это после ответа.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol
from zoneinfo import ZoneInfo

from .. import timeutil

if TYPE_CHECKING:  # только для подсказок типов
    from ..config import Settings
    from ..db import DB
    from ..llm import LLM

log = logging.getLogger("oracle.tools")

# кнопки: ряды пар (надпись, callback_data ≤ 64 байт)
Buttons = list[list[tuple[str, str]]]


@dataclass
class OutItem:
    """Что отправить владельцу помимо текста ответа."""
    kind: str                       # "text" | "file" | "voice"
    text: str = ""                  # текст сообщения или подпись к файлу
    data: bytes | None = None
    filename: str = ""
    buttons: Buttons | None = None


class Notifier(Protocol):
    """Отправка владельцу вне потока «вопрос → ответ» (напоминания, фоновые итоги).
    Реализация — в bot/, в тестах — фейк."""

    async def send(self, text: str, buttons: Buttons | None = None, *, silent: bool = False) -> int | None: ...
    async def send_file(self, data: bytes, filename: str, caption: str = "") -> None: ...
    async def send_voice(self, data: bytes, filename: str = "voice.mp3", caption: str = "") -> None: ...


@dataclass
class Services:
    """Живые сервисы, доступные инструментам. Любой может быть None (не включён/тесты)."""
    notifier: Any = None     # Notifier
    scheduler: Any = None    # services.scheduler.Scheduler
    userbot: Any = None      # services.userbot.Userbot
    news: Any = None         # services.news.NewsService
    agent: Any = None        # agent.Agent (для фоновых задач: глубокая оценка идеи и т.п.)
    _tasks: set = field(default_factory=set)

    def spawn(self, coro: Awaitable[Any], name: str = "bg") -> asyncio.Task:
        """Фоновая задача: ссылка держится до конца, исключение пишется в лог, а не теряется."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("фоновая задача %s упала: %r", name, t.exception(), exc_info=t.exception())

        task.add_done_callback(_done)
        return task

    async def drain(self, timeout: float = 5.0) -> None:
        """Дождаться (или отменить по таймауту) фоновые задачи — при остановке и в тестах."""
        if not self._tasks:
            return
        pending = list(self._tasks)
        _, still = await asyncio.wait(pending, timeout=timeout)
        for t in still:
            t.cancel()


@dataclass
class ToolContext:
    cfg: "Settings"
    db: "DB"
    llm: "LLM"
    services: Services = field(default_factory=Services)
    outbox: list[OutItem] = field(default_factory=list)

    @property
    def tz(self) -> ZoneInfo:
        return self.cfg.tz

    def now_local(self) -> datetime:
        return timeutil.now_local(self.cfg.tz)

    def now_utc(self) -> datetime:
        return timeutil.now_utc()

    def child(self) -> "ToolContext":
        """Тот же контекст с пустым outbox (для фоновых задач)."""
        return ToolContext(cfg=self.cfg, db=self.db, llm=self.llm, services=self.services)


Handler = Callable[..., Awaitable[Any]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Handler
    enabled: Callable[["Settings"], bool] | None = None

    def schema(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, properties: dict | None = None,
         required: list[str] | tuple[str, ...] = (),
         enabled: Callable[["Settings"], bool] | None = None) -> Callable[[Handler], Handler]:
    """Зарегистрировать инструмент. properties — JSON Schema свойств аргументов."""
    params = {"type": "object", "properties": properties or {}, "required": list(required)}

    def deco(fn: Handler) -> Handler:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"инструмент {name}: обработчик должен быть async")
        if name in REGISTRY and REGISTRY[name].handler is not fn:
            log.debug("инструмент %s переопределён", name)
        REGISTRY[name] = Tool(name, description, params, fn, enabled)
        return fn

    return deco


def available(cfg: "Settings") -> list[Tool]:
    out = []
    for t in REGISTRY.values():
        try:
            if t.enabled is None or t.enabled(cfg):
                out.append(t)
        except Exception:  # кривой предикат не должен ронять агента
            log.exception("enabled() инструмента %s", t.name)
    return out


def schemas(cfg: "Settings") -> list[dict]:
    return [t.schema() for t in available(cfg)]


MAX_RESULT_CHARS = 14_000


def _to_json(result: Any) -> str:
    if isinstance(result, str):
        s = result
    else:
        s = json.dumps(result, ensure_ascii=False, default=str)
    if len(s) > MAX_RESULT_CHARS:
        s = s[:MAX_RESULT_CHARS] + "…[обрезано]"
    return s


async def dispatch(name: str, arguments: str | dict | None, ctx: ToolContext) -> str:
    """Вызвать инструмент по имени с JSON-аргументами от модели → JSON-строка результата."""
    t = REGISTRY.get(name)
    if t is None or (t.enabled is not None and not t.enabled(ctx.cfg)):
        return _to_json({"ok": False, "error": f"нет такого инструмента: {name}"})
    if isinstance(arguments, dict):
        args = arguments
    else:
        try:
            args = json.loads(arguments or "{}")
        except ValueError:
            return _to_json({"ok": False, "error": "аргументы не JSON — повтори вызов с корректным JSON"})
    if not isinstance(args, dict):
        return _to_json({"ok": False, "error": "аргументы должны быть объектом"})
    props = t.parameters.get("properties", {})
    missing = [r for r in t.parameters.get("required", []) if args.get(r) in (None, "")]
    if missing:
        return _to_json({"ok": False, "error": f"не хватает аргументов: {', '.join(missing)}"})
    clean = {k: v for k, v in args.items() if k in props}
    try:
        result = await t.handler(ctx, **clean)
    except (ValueError, KeyError, LookupError) as e:   # ожидаемые ошибки данных — модели текстом
        return _to_json({"ok": False, "error": str(e)})
    except Exception as e:
        log.exception("инструмент %s упал", name)
        return _to_json({"ok": False, "error": f"внутренняя ошибка: {type(e).__name__}: {e}"})
    return _to_json(result)
