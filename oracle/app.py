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
import logging
import sqlite3
import sys
from typing import Any, Awaitable

from . import config
from .bot.render import redact

log = logging.getLogger("oracle")

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
    return tuple(str(getattr(cfg, n, "") or "") for n in names if getattr(cfg, n, ""))


def setup_logging(level: str, secrets: tuple[str, ...] = ()) -> None:
    root = logging.getLogger()
    before = set(root.handlers)
    logging.basicConfig(level=getattr(logging, str(level or "INFO").upper(), logging.INFO), format=LOG_FORMAT)
    for handler in root.handlers:
        if handler not in before:           # свой обработчик; чужие (если лог уже настроен) не трогаем
            handler.setFormatter(RedactingFormatter(LOG_FORMAT, secrets))
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def problems_text(cfg: config.Settings) -> str:
    probs = cfg.problems() or ["нет BOT_TOKEN — возьми у @BotFather и впиши в .env"]
    return ("Baltia Oracle не может запуститься — не хватает настроек:\n"
            + "\n".join(f"  • {p}" for p in probs)
            + "\nЗаполни .env (рядом с папкой oracle) и запусти снова: python -m oracle")


def is_setup_mode(cfg: config.Settings) -> bool:
    """Нет владельца или ключа модели — только /start с подсказкой, без агента и планировщика."""
    return not (cfg.owner_id and cfg.llm_api_key)


def bot_commands() -> list:
    from aiogram.types import BotCommand

    from .bot.handlers import COMMANDS
    return [BotCommand(command=c, description=d) for c, d in COMMANDS]


def build_dispatcher(cfg: config.Settings, deps: Any) -> Any:
    """Dispatcher: «только владелец и только в личке» на сообщения и кнопки, очередь реплик по порядку
    прихода, учёт обновлений в работе (`dp.oracle_inflight`) + роутер обработчиков."""
    from aiogram import Dispatcher

    from .bot.handlers import build_router
    from .bot.middleware import Inflight, OwnerOnly, TurnOrder

    dp = Dispatcher()
    inflight = Inflight()
    dp.update.outer_middleware(inflight)
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


async def main() -> int:
    """Запустить бота. → код выхода (0 — штатная остановка, 1 — не настроен / токен не принят)."""
    cfg = config.load()
    setup_logging(cfg.log_level, _secrets(cfg))
    if not cfg.tz_ok:
        log.warning("настройка: TIMEZONE=%r не распознан — работаю по %s; будильники съедут. Нужен вид "
                    "Europe/Moscow (или MSK, UTC+3)", cfg.timezone, cfg.tz.key)
    for w in cfg.warnings:
        log.warning("настройка: %s", w)
    if not cfg.bot_token:
        print(problems_text(cfg), file=sys.stderr)
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
        return 1
    llm = LLM(cfg)
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
    ub_task: asyncio.Task | None = None
    try:
        me = await whoami(bot)
        if me is None:
            print("Telegram не принял BOT_TOKEN — проверь его у @BotFather.", file=sys.stderr)
            return 1
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
        dp = build_dispatcher(cfg, deps)
        try:
            from aiogram.types import BotCommandScopeAllPrivateChats
            await bot.set_my_commands(bot_commands(), scope=BotCommandScopeAllPrivateChats())
        except Exception as e:
            log.warning("не смог обновить меню команд: %s", e)
        if cfg.userbot_enabled:
            ub_task = asyncio.create_task(keep_userbot(userbot), name="userbot")

        mode = str(await db.kv_get("mode", "fast") or "fast")
        ub_state = "подключается" if cfg.userbot_enabled else "выключен"
        log.info("Baltia Oracle запущен%s: модель %s (глубокая %s), режим %s, распознавание: %s, userbot: %s, пояс %s",
                 f" как @{me.username}" if getattr(me, "username", None) else "",
                 cfg.llm_model, cfg.llm_model_deep, mode, stt.describe(), ub_state, cfg.tz.key)
        if setup:
            log.warning("режим настройки — отвечаю только на /start: %s", "; ".join(cfg.problems()))

        # сессию бота закрываем сами, в самом конце: дорабатывающим ответам она ещё нужна
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), close_bot_session=False)
    finally:
        inflight = getattr(dp, "oracle_inflight", None)
        await asyncio.gather(
            _quietly("ответы в работе", finish_inflight(inflight.pending(), notifier) if inflight else None),
            _quietly("планировщик", scheduler.stop() if scheduler is not None else None))
        if ub_task is not None:
            ub_task.cancel()
            await asyncio.gather(ub_task, return_exceptions=True)
        await _quietly("userbot", userbot.stop())
        await _quietly("фоновые задачи", services.drain(timeout=5.0))
        await _quietly("новости", news.aclose())
        await _quietly("распознавание", stt.aclose())
        await _quietly("модель", llm.aclose())
        await _quietly("база", db.close())
        await _quietly("сессия бота", bot.session.close())
        log.info("остановлен")
    return 0
