"""Инструменты агента: реестр, контекст вызова, исходящие вложения.

Модуль инструмента регистрирует функции декоратором `@tool(...)`. Обработчик —
`async def h(ctx: ToolContext, **args) -> dict | str`. Результат уходит модели как JSON.
Ошибки ловит `dispatch` и отдаёт модели {"ok": false, "error": "..."} — бот не падает.

Всё, что инструмент хочет показать владельцу помимо ответа модели (файл .ics, черновик
с кнопками «Отправить / Переписать»), он кладёт в `ctx.outbox` — бот отправит это после ответа.

Слишком длинный результат режется по структуре, а не посреди JSON: из самого большого списка
убираются элементы с конца (`keep="head"`, по умолчанию) или с начала (`keep="tail"` — для
истории чата, где свежее в конце), и модель видит, сколько не влезло (`omitted`).
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

CANCEL_GRACE = 2.0      # сколько ждать отменённые фоновые задачи при остановке

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
    tts: Any = None          # services.tts.TTS (инструменту настроек: доступна ли озвучка)
    runtime: Any = None      # runtime.Runtime (живучесть экземпляра, конфликт токена — для дашборда)
    usage: Any = None        # модуль oracle.usage (учёт расходов) — для /status и дашборда
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
        """Дождаться (или отменить по таймауту) фоновые задачи — при остановке и в тестах. Отменённым
        даётся пара секунд доработать свои finally (вернуть статус идеи, снять пометку) — до закрытия базы."""
        if not self._tasks:
            return
        pending = list(self._tasks)
        _, still = await asyncio.wait(pending, timeout=timeout)
        for t in still:
            t.cancel()
        if still:
            await asyncio.wait(still, timeout=CANCEL_GRACE)


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
    keep: str = "head"          # что оставить из длинного списка в результате: head | tail

    def schema(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, properties: dict | None = None,
         required: list[str] | tuple[str, ...] = (),
         enabled: Callable[["Settings"], bool] | None = None,
         keep: str = "head") -> Callable[[Handler], Handler]:
    """Зарегистрировать инструмент. properties — JSON Schema свойств аргументов. keep — какой конец
    длинного списка в результате сохранить, если всё не влезает: "head" (начало) или "tail" (конец,
    например история чата, где свежие сообщения последние)."""
    params = {"type": "object", "properties": properties or {}, "required": list(required)}
    if keep not in ("head", "tail"):
        raise ValueError(f"инструмент {name}: keep — head или tail")

    def deco(fn: Handler) -> Handler:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"инструмент {name}: обработчик должен быть async")
        if name in REGISTRY and REGISTRY[name].handler is not fn:
            log.debug("инструмент %s переопределён", name)
        REGISTRY[name] = Tool(name, description, params, fn, enabled, keep)
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
# сетевые инструменты — со страховочным сроком: зависшая лента или сайт не держат весь ход. Не все
# инструменты: birthday_greeting и tg_draft_reply внутри ждут модель (до llm_fast_timeout)
NET_TOOLS = frozenset({"get_news", "read_url", "get_weather", "web_search"})
NET_TOOL_DEADLINE = 90.0
NET_TIMEOUT_ERROR = "инструмент не уложился во время — сеть медлит; скажи владельцу как есть"


def _dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def fit_list(items: list, budget: int, keep: str = "head") -> tuple[list, int]:
    """Сколько элементов списка влезает в budget знаков JSON → (оставленные в прежнем порядке, сколько
    выкинуто). keep="head" — оставить начало, "tail" — конец (свежие сообщения чата). Хотя бы один
    элемент остаётся всегда."""
    seq = items if keep == "head" else list(reversed(items))
    kept: list = []
    used = 2
    for it in seq:
        n = len(_dumps(it)) + 2
        if kept and used + n > budget:
            break
        kept.append(it)
        used += n
    if keep != "head":
        kept.reverse()
    return kept, len(items) - len(kept)


def _shrink(result: dict, keep: str) -> str | None:
    """Длинный результат → тот же JSON, но из самого большого списка выкинуто лишнее (с пометкой)."""
    lists = [k for k, v in result.items() if isinstance(v, list) and v]
    if not lists:
        return None
    key = max(lists, key=lambda k: len(_dumps(result[k])))
    items = result[key]
    rest = {k: v for k, v in result.items() if k != key}
    budget = MAX_RESULT_CHARS - len(_dumps(rest)) - 300
    if budget <= 0:
        return None
    kept, dropped = fit_list(items, budget, keep)
    if not dropped:
        return None
    what = "самые старые" if keep == "tail" else "последние"
    out = {**rest, key: kept, "omitted": dropped,
           "omitted_note": f"не влезло {dropped} из {len(items)} ({what}) — скажи об этом, если важно"}
    s = _dumps(out)
    return s if len(s) <= MAX_RESULT_CHARS else None


def _to_json(result: Any, keep: str = "head") -> str:
    if isinstance(result, str):
        s = result
    else:
        s = _dumps(result)
        if len(s) > MAX_RESULT_CHARS and isinstance(result, dict):
            s = _shrink(result, keep) or s
    if len(s) > MAX_RESULT_CHARS:           # последний рубеж: один огромный текст
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
        if name in NET_TOOLS:
            result = await asyncio.wait_for(t.handler(ctx, **clean), NET_TOOL_DEADLINE)
        else:
            result = await t.handler(ctx, **clean)
    except asyncio.TimeoutError:
        log.warning("инструмент %s не уложился в %.0f с", name, NET_TOOL_DEADLINE)
        return _to_json({"ok": False, "error": NET_TIMEOUT_ERROR})
    except (ValueError, KeyError, LookupError) as e:   # ожидаемые ошибки данных — модели текстом
        return _to_json({"ok": False, "error": str(e)})
    except Exception as e:
        log.exception("инструмент %s упал", name)
        return _to_json({"ok": False, "error": f"внутренняя ошибка: {type(e).__name__}: {e}"})
    return _to_json(result, t.keep)
