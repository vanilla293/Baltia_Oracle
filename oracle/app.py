"""Сборка и запуск: конфиг → база → модель → инструменты → сервисы → агент и планировщик → бот.

Без BOT_TOKEN запускаться бессмысленно — печатаем, чего не хватает, и выходим с кодом 1.
Без OWNER_ID или ключа модели бот стартует в «режиме настройки»: агент и планировщик не
запускаются, на /start бот присылает человеку его id и подсказку, что вписать в .env.

Порядок запуска: сначала «кто я» у Telegram (нет сети — ждём и повторяем, иначе ежедневная сводка
отметилась бы отправленной и потерялась), потом планировщик и polling. Userbot подключается
в фоне и переподключается сам: его зависший коннект не держит ни будильники, ни ответы.
Остановка (Ctrl+C / SIGTERM) — аккуратная: сначала дожидаемся ответов, которые уже в работе
(не успели — прерываем и говорим владельцу повторить), потом планировщик, userbot, фоновые
задачи, клиенты, база.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sqlite3
import sys
from typing import Any, Awaitable

from . import config
from .bot.render import redact

log = logging.getLogger("oracle")

RESTART_CODE = 75      # main() возвращает это, когда просит перезапуск (панель): start.sh/.bat перезапускают

# библиотеки, которые на DEBUG заливают лог (aiosqlite — ещё и текстом переписки в параметрах SQL)
NOISY_LOGGERS = ("httpx", "httpcore", "aiogram", "aiohttp", "telethon", "asyncio", "aiosqlite",
                 "filelock", "huggingface_hub", "faster_whisper", "primp", "urllib3")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
TELEGRAM_RETRY_MAX = 60.0         # пауза между попытками достучаться до Telegram на старте — не больше
USERBOT_RETRY_FIRST = 30.0        # userbot без связи: первая повторная попытка
USERBOT_RETRY_MAX = 900.0         # …и дальше не реже раза в 15 минут
INFLIGHT_WAIT = 12.0              # остановка: сколько ждать ответы в работе (весь бюджет — 30 с)
RESTART_NOTE_WAIT = 5.0           # …и сколько — отправку «повтори сообщение» (Telegram может лежать)
RESTART_NOTE = ("⚠️ Перезапускаюсь — не успел закончить ответ на последнее сообщение. "
                "Повтори его, пожалуйста.")


class RedactingFormatter(logging.Formatter):
    """Формат лога без секретов: токен бота (он бывает в адресе файла в тексте ошибки aiohttp) и ключи."""

    def __init__(self, fmt: str, secrets: tuple[str, ...] = ()):
        super().__init__(fmt)
        self.secrets = tuple(secrets)

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self.secrets)


def _secrets(cfg: Any) -> tuple[str, ...]:
    names = ("bot_token", "llm_api_key", "groq_api_key", "openai_api_key", "tg_api_hash")
    out = [str(getattr(cfg, n, "") or "") for n in names if getattr(cfg, n, "")]
    out += [k for k in getattr(cfg, "api_keys", ()) if k and k not in out]     # запасные ключи — тоже
    return tuple(out)


class TwinHint(logging.Filter):
    """Telegram отвечает Conflict на getUpdates — значит, этого бота опрашивает ещё одна программа с тем же
    токеном (второе окно, Pythia, другой компьютер). aiogram на это пишет две строки каждые несколько секунд,
    бесконечно. Здесь они глушатся: вместо них — одна подсказка по-русски раз в 10 минут, пока конфликт идёт."""

    HINT_EVERY = 600.0          # подсказка — не чаще
    QUIET_AFTER = 60.0          # «Sleep for …» глушим, пока конфликт был не раньше минуты назад

    def __init__(self, clock: Any = None, runtime: Any = None) -> None:
        super().__init__()
        import time
        self._clock = clock or time.monotonic
        self._last: float | None = None
        self._conflict_at: float | None = None
        self._runtime = runtime          # runtime.Runtime: отметить конфликт для баннера дашборда

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        now = self._clock()
        if args and "Conflict" in str(args[0]):
            self._conflict_at = now
            if self._runtime is not None:
                try:
                    self._runtime.note_conflict()
                except Exception:
                    pass
            if self._last is None or now - self._last > self.HINT_EVERY:
                self._last = now
                log.warning("Бот запущен дважды: с этим токеном работает ещё одна программа (старое окно бота, "
                            "Pythia, другой компьютер). Пока она работает, часть сообщений и кнопок уходит ей. "
                            "Закрой её — или в @BotFather /revoke и новый токен в BOT_TOKEN. "
                            "(Повторы этой ошибки скрыты, напомню через 10 минут, если не пройдёт.)")
            return False
        msg = record.msg if isinstance(record.msg, str) else ""
        if msg.startswith("Sleep for") and self._conflict_at is not None \
                and now - self._conflict_at < self.QUIET_AFTER:
            return False
        return True


def setup_logging(level: str, secrets: tuple[str, ...] = (), *, logbuffer: Any = None,
                  runtime: Any = None) -> None:
    root = logging.getLogger()
    before = set(root.handlers)
    logging.basicConfig(level=getattr(logging, str(level or "INFO").upper(), logging.INFO), format=LOG_FORMAT)
    for handler in root.handlers:
        if handler not in before:           # свой обработчик; чужие (если лог уже настроен) не трогаем
            handler.setFormatter(RedactingFormatter(LOG_FORMAT, secrets))
    if logbuffer is not None:               # буфер лога для дашборда: держим ровно один, свежий
        from .runtime import LogBuffer
        for h in list(root.handlers):
            if isinstance(h, LogBuffer):
                root.removeHandler(h)
        root.addHandler(logbuffer)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    disp = logging.getLogger("aiogram.dispatcher")
    for f in [f for f in disp.filters if isinstance(f, TwinHint)]:
        disp.removeFilter(f)                # пересоздаём с актуальным runtime (или без него в тестах)
    disp.addFilter(TwinHint(runtime=runtime))


def problems_text(cfg: config.Settings) -> str:
    probs = cfg.problems() or ["нет BOT_TOKEN — возьми у @BotFather и впиши в .env"]
    return ("Baltia Oracle не может запуститься — не хватает настроек:\n"
            + "\n".join(f"  • {p}" for p in probs)
            + "\nЗаполни .env (рядом с папкой oracle) и запусти снова: python -m oracle")


async def remember_owner(cfg: config.Settings, db: Any) -> config.Settings:
    """OWNER_ID из .env запоминаем в базе. Пропал из .env (архив распакован поверх, .env пересоздан) —
    берём прежнего владельца из базы, а не впадаем в «режим настройки». Сменить владельца — вписать
    новый OWNER_ID: он перезапишет запомненный."""
    from dataclasses import replace
    try:
        stored = int(await db.kv_get("owner_id", 0) or 0)
    except (TypeError, ValueError):
        stored = 0
    if cfg.owner_id:
        if stored != cfg.owner_id:
            await db.kv_set("owner_id", int(cfg.owner_id))
        return cfg
    if stored:
        log.warning("OWNER_ID в .env пуст — беру прежнего владельца из базы (%s). Впиши OWNER_ID=%s в .env, "
                    "чтобы не зависеть от базы", stored, stored)
        return replace(cfg, owner_id=stored)
    return cfg


async def apply_stored_settings(cfg: config.Settings, db: Any) -> config.Settings:
    """Наложить на cfg то, что бот настроил себе сам через инструменты (kv): город/пояс погоды,
    имя и род владельца. Так изменения из разговора («мой город Рига») применяются после перезапуска,
    оставаясь в базе, а не в .env. Пусто в kv — cfg не трогаем."""
    from dataclasses import replace
    over: dict[str, Any] = {}

    async def _kv(key: str) -> str:
        try:
            v = await db.kv_get(key, "")
        except Exception:
            return ""
        return str(v or "").strip()

    name = await _kv("owner_name")
    if name:
        over["owner_name"] = name[:64]
    gender = await _kv("owner_gender")
    if gender:
        over["owner_gender"] = config._gender(gender)
    city = await _kv("weather_city")
    if city:
        over["weather_city"] = city
    tzv = await _kv("timezone")
    if tzv:
        over["timezone"] = config.resolve_timezone(tzv) or tzv
    return replace(cfg, **over) if over else cfg


def is_setup_mode(cfg: config.Settings) -> bool:
    """Нет владельца или ключа модели — только /start с подсказкой, без агента и планировщика."""
    return not (cfg.owner_id and cfg.llm_api_key)


def bot_commands() -> list:
    from aiogram.types import BotCommand

    from .bot.handlers import COMMANDS
    return [BotCommand(command=c, description=d) for c, d in COMMANDS]


def build_dispatcher(cfg: config.Settings, deps: Any, runtime: Any = None) -> Any:
    """Dispatcher: «только свой и только в личке» на сообщения и кнопки, очередь реплик по порядку
    прихода, учёт обновлений в работе (`dp.oracle_inflight`) и один роутер (бот — для одного человека).

    runtime (oracle.runtime.Runtime) — при настоящем запуске: (1) каждое обновление помечает
    `note_update` (для дашборда «когда в последний раз опрашивали»); (2) если OWNER_ID не задан,
    первый написавший становится владельцем (claim), и по этому событию просим перезапуск, чтобы
    поднялся мозг. В тестах runtime нет — поведение прежнее (claim выключен)."""
    from aiogram import Dispatcher

    from .bot.handlers import build_router
    from .bot.middleware import Inflight, OwnerOnly, TurnOrder

    dp = Dispatcher()
    inflight = Inflight()
    dp.update.outer_middleware(inflight)
    if runtime is not None:
        async def _note_update(handler: Any, event: Any, data: dict) -> Any:
            try:
                runtime.note_update()
            except Exception:
                pass
            return await handler(event, data)
        dp.update.outer_middleware(_note_update)

        async def _on_claim(uid: int) -> None:
            runtime.request_restart("владелец определён — перезапуск, чтобы поднять мозг")
        guard = OwnerOnly(cfg, db=deps.db, on_claim=_on_claim)
    else:
        guard = OwnerOnly(cfg)
    dp.message.outer_middleware(guard)
    dp.message.outer_middleware(TurnOrder())       # после guard: билеты — только репликам владельца
    dp.edited_message.outer_middleware(guard)
    dp.callback_query.outer_middleware(guard)
    dp.include_router(build_router(deps))
    dp.oracle_inflight = inflight                   # type: ignore[attr-defined]
    return dp


async def whoami(bot: Any, *, first_delay: float = 2.0, max_delay: float = TELEGRAM_RETRY_MAX) -> Any:
    """Кто я в Telegram (bot.me() — запоминается, polling второй раз не спросит). Сети нет или Telegram
    лежит — ждём и повторяем, пока не ответит. Токен не принят → None."""
    from aiogram.exceptions import (TelegramNetworkError, TelegramNotFound, TelegramRetryAfter,
                                    TelegramServerError, TelegramUnauthorizedError)
    delay = first_delay
    while True:
        try:
            return await bot.me()
        except (TelegramUnauthorizedError, TelegramNotFound):
            return None
        except TelegramRetryAfter as e:
            wait = min(float(getattr(e, "retry_after", 0) or delay), max_delay)
            log.warning("Telegram просит подождать %.0f с", wait)
        except (TelegramNetworkError, TelegramServerError, asyncio.TimeoutError, OSError) as e:
            wait = delay
            delay = min(delay * 2, max_delay)
            log.warning("Telegram недоступен (%s) — повторю через %.0f с", e, wait)
        await asyncio.sleep(wait)


async def keep_userbot(userbot: Any, *, first_delay: float = USERBOT_RETRY_FIRST,
                       max_delay: float = USERBOT_RETRY_MAX) -> None:
    """Подключать userbot в фоне и держать подключённым. Нет связи — повтор с растущей паузой; нет входа
    или ключей — повтор не поможет, выходим (причина — в userbot.last_error, её показывает /status).
    Подключился — ждём, не потеряет ли Telethon связь насовсем (сам он переподключается лишь несколько
    раз и сдаётся): потерял — ready=False, «network: …» в /status и снова подключаемся."""
    delay = first_delay
    while True:
        try:
            await userbot.start()
        except asyncio.CancelledError:
            raise
        except Exception as e:           # start() сам ловит сетевые ошибки; сюда — только неожиданное
            log.exception("userbot не стартовал")
            userbot.last_error = f"error: {type(e).__name__}"
            return
        if userbot.ready:
            watch = getattr(userbot, "wait_disconnected", None)
            if watch is None:
                return
            delay = first_delay
            try:
                await watch()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("userbot: ожидание обрыва связи", exc_info=True)
            if getattr(userbot, "stopped", False):
                return
            log.warning("userbot: Telethon потерял связь с Telegram — переподключусь через %.0f с", delay)
            await asyncio.sleep(delay)
            continue
        if not str(getattr(userbot, "last_error", "") or "").startswith("network"):
            return
        log.info("userbot: нет связи с Telegram — попробую снова через %.0f с", delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, max_delay)


async def finish_inflight(tasks: list, notifier: Any, timeout: float = INFLIGHT_WAIT) -> None:
    """Остановка: дождаться ответов, которые уже в работе (aiogram их не ждёт, а база и модель вот-вот
    закроются). Не успели — прервать и сказать владельцу, что сообщение надо повторить: апдейт
    Telegram уже подтверждён и заново не придёт."""
    pending = [t for t in tasks if not t.done()]
    if not pending:
        return
    log.info("остановка: дожидаюсь ответов в работе (%d, до %.0f с)", len(pending), timeout)
    _, still = await asyncio.wait(pending, timeout=timeout)
    if not still:
        return
    for t in still:
        t.cancel()
    await asyncio.wait(still, timeout=2.0)
    log.warning("остановка: %d ответов не успели — прерваны", len(still))
    if notifier is not None:            # не дольше RESTART_NOTE_WAIT: весь бюджет остановки — 30 с
        try:
            await asyncio.wait_for(notifier.send(RESTART_NOTE), RESTART_NOTE_WAIT)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("остановка: не сказал владельцу повторить сообщение: %r", e)


async def _quietly(what: str, aw: Awaitable[Any] | None) -> None:
    """Шаг остановки: ошибка пишется в лог и не мешает остальным шагам."""
    if aw is None:
        return
    try:
        await aw
    except Exception:
        log.exception("остановка: %s", what)


def _epoch(iso_value: Any) -> float | None:
    """UTC ISO из базы → epoch-секунды для дашборда (или None)."""
    from . import timeutil
    try:
        dt = timeutil.from_iso(iso_value) if iso_value else None
        return dt.timestamp() if dt else None
    except Exception:
        return None


class DashboardStateImpl:
    """Провайдер данных для локальной панели (oracle.dashboard). Всё поверх живых deps/db/runtime/usage.

    Методы отдают JSON-совместимое; форма — по контракту билдера дашборда (см. его отчёт). Ни один
    метод не должен ронять сервер: исключения дашборд ловит сам, но мы и так стараемся не падать.
    """

    def __init__(self, deps: Any, *, runtime: Any, logbuffer: Any, env_path: Any) -> None:
        self.deps = deps
        self.cfg = deps.cfg
        self.db = deps.db
        self.runtime = runtime
        self.logbuffer = logbuffer
        self.env_path = env_path

    async def status(self) -> dict:
        from . import usage
        from .runtime import conflict_hint
        rt = self.runtime.status() if self.runtime is not None else {}
        cost = {"today": 0.0, "month": 0.0, "calls": 0, "currency": "USD"}
        try:
            today = await usage.summary(self.db, days=1)
            month = await usage.summary(self.db, days=30)
            cost = {"today": today["total_cost"], "month": month["total_cost"],
                    "calls": month["total_calls"], "currency": "USD"}
        except Exception:
            log.debug("панель: расходы недоступны", exc_info=True)
        balance = None
        try:
            balance = await usage.balance(self.cfg)
        except Exception:
            balance = None
        twins = rt.get("twins") or []
        conflict = dict(rt.get("conflict") or {})
        return {
            "bot_name": self.cfg.bot_name,
            "telegram": rt.get("telegram") or {"ok": False},
            "conflict": {"active": bool(conflict.get("active")), "count": int(conflict.get("count", 0)),
                         "hint": conflict_hint(twins)},
            "uptime_sec": rt.get("uptime_sec"),
            "last_update_ago_sec": rt.get("last_update_ago_sec"),
            "model": {"fast": self.cfg.llm_model, "deep": self.cfg.llm_model_deep},
            "cost": cost,
            "balance": balance or {"is_available": False, "balances": []},
            "twins": twins,
            "single_instance_ok": bool(rt.get("single_instance_ok", True)),
        }

    async def dialog(self, limit: int = 50) -> Any:
        rows = await self.db.fetchall(
            "SELECT role, content, created_at FROM messages ORDER BY id DESC LIMIT ?", (int(limit),))
        rows.reverse()
        return [{"role": r["role"], "content": r["content"], "ts": _epoch(r["created_at"])} for r in rows]

    async def memory(self) -> Any:
        rows = await self.db.fetchall(
            "SELECT id, content, category, created_at FROM facts ORDER BY id DESC LIMIT 500")
        return [{"id": r["id"], "content": r["content"], "category": r["category"],
                 "ts": _epoch(r["created_at"])} for r in rows]

    async def opinions(self) -> Any:
        rows = await self.db.fetchall(
            "SELECT id, topic, stance, reasons, confidence, updated_at FROM opinions "
            "ORDER BY confidence DESC, updated_at DESC, id DESC LIMIT 200")
        return [{"id": r["id"], "topic": r["topic"], "stance": r["stance"], "reasons": r["reasons"],
                 "confidence": r["confidence"], "ts": _epoch(r["updated_at"])} for r in rows]

    async def journal(self) -> Any:
        rows = await self.db.fetchall("SELECT id, content, created_at FROM journal ORDER BY id DESC LIMIT 60")
        return [{"id": r["id"], "content": r["content"], "ts": _epoch(r["created_at"])} for r in rows]

    async def reminders(self) -> Any:
        from . import timeutil
        rows = await self.db.fetchall(
            "SELECT id, text, kind, next_at, rrule, status FROM reminders WHERE status='active' "
            "ORDER BY (next_at IS NULL), next_at LIMIT 200")
        tz = self.cfg.tz
        out = []
        for r in rows:
            when = ""
            if r.get("next_at"):
                try:
                    when = timeutil.fmt_local(r["next_at"], tz)
                except Exception:
                    when = ""
            out.append({"id": r["id"], "text": r["text"], "kind": r["kind"],
                        "when_local": when, "repeat": bool(r.get("rrule"))})
        return out

    async def ideas(self) -> Any:
        from .tools import ideas
        rows = await ideas.recent_ideas(self.db, limit=200)
        return [{"id": r["id"], "title": r["title"], "score": r.get("score"), "status": r.get("status"),
                 "tags": r.get("tags", ""), "content": r.get("content", "")} for r in rows]

    async def projects(self) -> Any:
        from .tools import projects
        try:
            res = await projects.t_list_projects(self.deps.ctx.child(), status="all")
            return res.get("items") or []
        except Exception:
            log.debug("панель: проекты недоступны", exc_info=True)
            return []

    async def news_recent(self) -> Any:
        rows = await self.db.fetchall(
            "SELECT title, summary, source, url, published_at, fetched_at FROM news_items "
            "ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT 50")
        return [{"title": r["title"], "summary": r["summary"], "source": r["source"], "url": r["url"],
                 "ts": _epoch(r.get("published_at") or r.get("fetched_at"))} for r in rows]

    async def usage(self, days: int = 30) -> Any:
        from . import usage as usage_mod
        try:
            return await usage_mod.summary(self.db, int(days))
        except Exception:
            log.debug("панель: сводка расходов недоступна", exc_info=True)
            return {"total_cost": 0.0, "total_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                    "by_route": [], "by_day": [], "by_model": [], "days": int(days), "since": ""}

    async def logs(self, after_id: int = 0, level: str = "INFO") -> Any:
        if self.logbuffer is None:
            return []
        try:
            return self.logbuffer.records(after_id=int(after_id or 0), min_level=level)
        except Exception:
            return []

    async def settings_get(self) -> Any:
        from . import settings_schema as ss
        return {"sections": ss.sections(), "settings": ss.schema_public(),
                "values": ss.masked_values(self.cfg)}

    async def settings_set(self, form: dict) -> dict:
        from . import settings_schema as ss
        from . import runtime as rt_mod
        errors: dict[str, str] = {}
        for key, raw in (form or {}).items():
            if ss.get_setting(key) is None:
                continue
            ok, res = ss.validate(key, raw)
            if not ok:
                errors[key] = res
        if errors:
            return {"ok": False, "changed": [], "restart_needed": False, "errors": errors}
        updates = ss.to_env_updates(form, ss.masked_values(self.cfg))
        if updates:
            res = rt_mod.update_env(self.env_path, updates)
            if not res.get("ok"):
                return {"ok": False, "changed": [], "restart_needed": False,
                        "errors": {"_": "не смог записать .env — проверь права на файл"}}
        return {"ok": True, "changed": list(updates.keys()), "restart_needed": bool(updates), "errors": {}}

    async def action(self, name: str, params: dict | None = None) -> dict:
        from . import runtime as rt_mod
        name = str(name or "")
        if name == "clear_conflict":
            holders = rt_mod.find_holders(self.cfg.bot_token, self_pid=os.getpid())
            if self.runtime is not None:
                self.runtime.set_twins(holders)
                self.runtime.conflict_at = None
                self.runtime.conflict_count = 0
            same = [h for h in holders if h.get("same_token")]
            return {"ok": True, "twins": holders,
                    "note": rt_mod.conflict_hint(holders) or ("никто больше этот токен не держит"
                                                              if not same else "")}
        if name == "restart":
            if self.runtime is not None:
                self.runtime.request_restart("перезапуск из панели")
            return {"ok": True, "restart": True}
        if name == "run_brief":
            from .services import brief
            child = self.deps.ctx.child()
            notifier = self.deps.notifier

            async def _run() -> None:
                try:
                    text = await brief.morning_brief(child)
                    if notifier is not None and text:
                        await notifier.send(text)
                except Exception:
                    log.warning("панель: утренняя сводка не собралась", exc_info=True)
            self.deps.ctx.services.spawn(_run(), name="dash_brief")
            return {"ok": True, "started": True}
        return {"ok": False, "error": f"неизвестное действие: {name}"}


async def main() -> int:
    """Запустить бота. → код выхода (0 — штатная остановка, 1 — не настроен / токен не принят,
    RESTART_CODE — просьба перезапуститься от панели)."""
    cfg = config.load()
    from .runtime import InstanceLock, LogBuffer, Runtime, conflict_hint, find_holders, warn_owner
    rt = Runtime()
    rt.set_env_path(config.LOADED_ENV)
    logbuf = LogBuffer(secrets=_secrets(cfg))
    setup_logging(cfg.log_level, _secrets(cfg), logbuffer=logbuf, runtime=rt)
    if not cfg.tz_ok:
        log.warning("настройка: TIMEZONE=%r не распознан — работаю по %s; будильники съедут. Нужен вид "
                    "Europe/Moscow (или MSK, UTC+3)", cfg.timezone, cfg.tz.key)
    for w in cfg.warnings:
        log.warning("настройка: %s", w)
    if not cfg.bot_token:
        print(problems_text(cfg), file=sys.stderr)
        with contextlib.suppress(Exception):
            logging.getLogger().removeHandler(logbuf)
        return 1

    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.utils.token import TokenValidationError

    session = None
    if cfg.telegram_api_url:           # свой Bot API сервер (telegram-bot-api) вместо api.telegram.org
        from aiogram.client.session.aiohttp import AiohttpSession
        from aiogram.client.telegram import TelegramAPIServer
        session = AiohttpSession(api=TelegramAPIServer.from_base(cfg.telegram_api_url))
    try:
        bot = Bot(cfg.bot_token, session=session,
                  default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    except TokenValidationError:
        print("BOT_TOKEN в .env кривой — скопируй его у @BotFather целиком (вида 123456:ABC…).", file=sys.stderr)
        with contextlib.suppress(Exception):
            logging.getLogger().removeHandler(logbuf)
        return 1

    # Замок «одна копия из этой папки»: если отсюда уже запущена живая копия — не поднимаем второй
    # поллер (иначе ответы прыгали бы между копиями). Чужой процесс НЕ трогаем — только сообщаем.
    lock: Any = None
    if getattr(cfg, "single_instance", True):
        lock = InstanceLock(cfg.data_dir)
        holder = lock.acquire()
        if holder is not None:
            rt.single_instance_ok = False
            msg = (f"Отсюда ({cfg.data_dir}) уже запущена копия бота (pid {holder.get('pid')}). "
                   f"Работает она. Закрой это окно — или останови ту копию и запусти заново. "
                   f"Второй поллер я не поднимаю: иначе часть сообщений уходила бы туда.")
            print(msg, file=sys.stderr)
            log.error("замок занят: %s", msg)
            await bot.session.close()
            with contextlib.suppress(Exception):
                logging.getLogger().removeHandler(logbuf)
            return 1

    from .bot.handlers import Deps
    from .bot.notifier import BotNotifier
    from .db import DB
    from .llm import LLM
    from .services.news import NewsService
    from .services.stt import STT
    from .services.tts import TTS
    from .services.userbot import Userbot
    from .tools.base import Services, ToolContext

    try:
        db = await DB(cfg.db_path).open()
    except (sqlite3.Error, OSError, RuntimeError) as e:   # DB.open сама закрывает связь — процесс не виснет
        print(f"Не открывается база {cfg.db_path}: {e}\nЕсли файл повреждён — верни его из резервной копии "
              f"(/backup): README, «Восстановление из копии».", file=sys.stderr)
        await bot.session.close()
        if lock is not None:
            with contextlib.suppress(Exception):
                lock.release()
        with contextlib.suppress(Exception):
            logging.getLogger().removeHandler(logbuf)
        return 1
    cfg = await remember_owner(cfg, db)
    cfg = await apply_stored_settings(cfg, db)   # то, что бот настроил себе сам (город, пояс, имя) — из kv
    from . import usage
    llm = LLM(cfg)
    llm.on_usage = usage.make_hook(db, cfg)      # учёт расходов; хук сам глотает ошибки, ответ не ломает
    try:
        from . import tools
        tools.load_all()
    except Exception as e:     # агент ещё раз загрузит, что сможет, и запомнит, что не загрузилось
        log.warning("не все инструменты загрузились: %s: %s", type(e).__name__, e)
    interrupted: list = []
    try:                       # разборы идей, прерванные прошлым запуском, — вернуть идеям прежний статус
        from .tools import ideas as _ideas
        interrupted = await _ideas.recover_interrupted(db)
    except Exception:
        log.exception("не восстановил прерванные разборы идей")
    services = Services()
    services.runtime = rt                # дашборд/уведомления читают состояние процесса
    services.usage = usage              # /status и дашборд берут расходы отсюда
    ctx = ToolContext(cfg=cfg, db=db, llm=llm, services=services)
    news = NewsService(cfg, db)
    services.news = news
    stt = STT(cfg)
    tts = TTS(cfg)
    services.tts = tts                  # инструментам настроек: доступна ли озвучка
    notifier = BotNotifier(bot, cfg.owner_id)
    services.notifier = notifier
    userbot = Userbot(cfg, db, notifier)
    services.userbot = userbot
    agent: Any = None
    scheduler: Any = None
    dp: Any = None
    dash: Any = None
    ub_task: asyncio.Task | None = None
    exit_code = 0
    try:
        me = await whoami(bot)
        if me is None:
            rt.note_telegram({"ok": False, "error": "токен не принят"})
            print("Telegram не принял BOT_TOKEN — проверь его у @BotFather.", file=sys.stderr)
            return 1
        rt.note_telegram({"ok": True, "username": getattr(me, "username", None),
                          "id": int(getattr(me, "id", 0) or 0)})
        if cfg.owner_id and int(getattr(me, "id", 0) or 0) == int(cfg.owner_id):
            log.warning("OWNER_ID=%s — это id самого бота (цифры до «:» в BOT_TOKEN), а не твой: бот будет "
                        "считать тебя чужим. Очисти OWNER_ID в .env и перезапусти — на /start пришлю твой id",
                        cfg.owner_id)
        setup = is_setup_mode(cfg)
        if not setup:
            from .agent import Agent
            from .services.scheduler import Scheduler
            agent = Agent(ctx)                  # сам регистрируется в services.agent
            scheduler = Scheduler(ctx)
            scheduler.start()
            if interrupted:
                names = ", ".join(f"#{i.get('id')} «{i.get('title')}»" for i in interrupted[:5])
                try:
                    await notifier.send(f"⚠️ Перезапускался и не закончил глубокий разбор: {names}. "
                                        f"Скажи «додумай идею …» — начну заново.", silent=True)
                except Exception as e:
                    log.warning("не сказал о прерванных разборах: %s", e)
        deps = Deps(cfg=cfg, db=db, llm=llm, ctx=ctx, agent=agent, stt=stt, tts=tts, userbot=userbot,
                    news=news, notifier=notifier, scheduler=scheduler)
        dp = build_dispatcher(cfg, deps, rt)     # один роутер: бот служит одному человеку
        try:
            from aiogram.types import BotCommandScopeAllPrivateChats
            await bot.set_my_commands(bot_commands(), scope=BotCommandScopeAllPrivateChats())
        except Exception as e:
            log.warning("не смог обновить меню команд: %s", e)

        # локальная панель: адрес с токеном печатается в лог (открывается только на этой машине)
        try:
            from .dashboard import start_dashboard
            dash = await start_dashboard(
                DashboardStateImpl(deps, runtime=rt, logbuffer=logbuf, env_path=config.LOADED_ENV or config.ROOT / ".env"),
                host=getattr(cfg, "dashboard_host", "127.0.0.1"),
                port=getattr(cfg, "dashboard_port", 8765),
                open_browser=getattr(cfg, "dashboard_open", True))
            deps.dashboard = dash
            log.info("Панель: %s", dash.url)
        except Exception as e:
            log.warning("панель не поднялась (%s: %s) — бот работает без неё", type(e).__name__, e)

        if cfg.userbot_enabled:
            ub_task = asyncio.create_task(keep_userbot(userbot), name="userbot")

        mode = str(await db.kv_get("mode", "fast") or "fast")
        ub_state = "подключается" if cfg.userbot_enabled else "выключен"
        log.info("Baltia Oracle запущен%s: модель %s (глубокая %s), режим %s, распознавание: %s, userbot: %s, пояс %s",
                 f" как @{me.username}" if getattr(me, "username", None) else "",
                 cfg.llm_model, cfg.llm_model_deep, mode, stt.describe(), ub_state, cfg.tz.key)
        env = config.LOADED_ENV
        log.info("настройки: %s; OWNER_ID: %s", env if env is not None else "файл .env не найден",
                 cfg.owner_id or "не задан")
        if setup:
            log.warning("режим настройки — отвечаю только на /start: %s", "; ".join(cfg.problems()))

        # кто ещё держит наш токен (только чтение): нашли местную копию с тем же токеном — прямо
        # предупреждаем владельца, какое окно закрыть, и зажигаем баннер на дашборде
        try:
            holders = find_holders(cfg.bot_token, self_pid=os.getpid())
            rt.set_twins(holders)
            hint = conflict_hint(holders)
            if hint:
                log.warning("конфликт токена при старте:\n%s", hint)
                if cfg.owner_id:
                    await warn_owner(notifier, holders)
        except Exception:
            log.debug("проверка чужих держателей токена не удалась", exc_info=True)

        # сессию бота закрываем сами, в самом конце: дорабатывающим ответам она ещё нужна.
        # Параллельно ждём просьбу о перезапуске (панель): пришла — гасим поллер и выходим RESTART_CODE.
        poll = asyncio.create_task(
            dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), close_bot_session=False),
            name="polling")
        restart = asyncio.create_task(rt.wait_restart(), name="restart-wait")
        await asyncio.wait({poll, restart}, return_when=asyncio.FIRST_COMPLETED)
        if rt.restart_requested() and not poll.done():
            log.info("перезапуск по запросу: %s", rt.restart_reason)
            exit_code = RESTART_CODE
            with contextlib.suppress(Exception):     # мы сами гасим поллер — ошибки остановки не важны
                await dp.stop_polling()
            with contextlib.suppress(Exception):
                await poll
        else:
            await poll                               # обычная остановка (Ctrl+C / SIGTERM) или ошибка — как было
            if rt.restart_requested():
                exit_code = RESTART_CODE
        if not restart.done():
            restart.cancel()
            await asyncio.gather(restart, return_exceptions=True)
    finally:
        inflight = getattr(dp, "oracle_inflight", None)
        await asyncio.gather(
            _quietly("ответы в работе", finish_inflight(inflight.pending(), notifier) if inflight else None),
            _quietly("планировщик", scheduler.stop() if scheduler is not None else None))
        if ub_task is not None:
            ub_task.cancel()
            await asyncio.gather(ub_task, return_exceptions=True)
        await _quietly("userbot", userbot.stop())
        await _quietly("панель", dash.stop() if dash is not None else None)
        await _quietly("фоновые задачи", services.drain(timeout=5.0))
        await _quietly("новости", news.aclose())
        await _quietly("распознавание", stt.aclose())
        await _quietly("модель", llm.aclose())
        await _quietly("база", db.close())
        await _quietly("сессия бота", bot.session.close())
        if lock is not None:
            with contextlib.suppress(Exception):
                lock.release()
        with contextlib.suppress(Exception):
            logging.getLogger().removeHandler(logbuf)
        log.info("остановлен")
    return exit_code
