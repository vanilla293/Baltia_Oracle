"""Сборка и запуск: конфиг → база → модель → инструменты → сервисы → агент и планировщик → бот.

Без BOT_TOKEN запускаться бессмысленно — печатаем, чего не хватает, и выходим с кодом 1.
Без OWNER_ID или ключа модели бот стартует в «режиме настройки»: агент и планировщик не
запускаются, на /start бот присылает человеку его id и подсказку, что вписать в .env.
Остановка (Ctrl+C / SIGTERM) — аккуратная: планировщик, userbot, фоновые задачи, клиенты, база.
"""
from __future__ import annotations

import logging
import sys
from typing import Any, Awaitable

from . import config

log = logging.getLogger("oracle")

NOISY_LOGGERS = ("httpx", "httpcore", "aiogram", "aiohttp", "telethon", "asyncio")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, str(level or "INFO").upper(), logging.INFO), format=LOG_FORMAT)
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
    """Dispatcher: «только владелец» на сообщения и кнопки + роутер обработчиков."""
    from aiogram import Dispatcher

    from .bot.handlers import build_router
    from .bot.middleware import OwnerOnly

    dp = Dispatcher()
    guard = OwnerOnly(cfg)
    dp.message.outer_middleware(guard)
    dp.callback_query.outer_middleware(guard)
    dp.include_router(build_router(deps))
    return dp


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
    setup_logging(cfg.log_level)
    if not cfg.bot_token:
        print(problems_text(cfg), file=sys.stderr)
        return 1

    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.exceptions import TelegramUnauthorizedError
    from aiogram.utils.token import TokenValidationError

    try:
        bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML,
                                                              link_preview_is_disabled=True))
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

    db = await DB(cfg.db_path).open()
    llm = LLM(cfg)
    try:
        from . import tools
        tools.load_all()
    except Exception as e:     # агент ещё раз загрузит, что сможет, и запомнит, что не загрузилось
        log.warning("не все инструменты загрузились: %s: %s", type(e).__name__, e)
    services = Services()
    ctx = ToolContext(cfg=cfg, db=db, llm=llm, services=services)
    news = NewsService(cfg, db)
    services.news = news
    stt = STT(cfg)
    tts = TTS(cfg)
    notifier = BotNotifier(bot, cfg.owner_id)
    services.notifier = notifier
    userbot = Userbot(cfg, db, notifier)
    services.userbot = userbot
    agent: Any = None
    scheduler: Any = None
    try:
        if cfg.userbot_enabled:
            try:
                await userbot.start()
            except Exception:
                log.exception("userbot не стартовал")
        setup = is_setup_mode(cfg)
        if not setup:
            from .agent import Agent
            from .services.scheduler import Scheduler
            agent = Agent(ctx)                  # сам регистрируется в services.agent
            scheduler = Scheduler(ctx)
            scheduler.start()
        deps = Deps(cfg=cfg, db=db, llm=llm, ctx=ctx, agent=agent, stt=stt, tts=tts, userbot=userbot,
                    news=news, notifier=notifier, scheduler=scheduler)
        dp = build_dispatcher(cfg, deps)

        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            print("Telegram не принял BOT_TOKEN — проверь его у @BotFather.", file=sys.stderr)
            return 1
        except Exception as e:     # сеть — polling сам переподключится
            log.warning("не смог спросить у Telegram, кто я: %s", e)
            me = None
        try:
            await bot.set_my_commands(bot_commands())
        except Exception as e:
            log.warning("не смог обновить меню команд: %s", e)

        mode = str(await db.kv_get("mode", "fast") or "fast")
        ub_state = "подключён" if userbot.ready else ("не подключён" if cfg.userbot_enabled else "выключен")
        log.info("Baltia Oracle запущен%s: модель %s (глубокая %s), режим %s, распознавание: %s, userbot: %s, пояс %s",
                 f" как @{me.username}" if me is not None and getattr(me, "username", None) else "",
                 cfg.llm_model, cfg.llm_model_deep, mode, stt.describe(), ub_state, cfg.timezone)
        if setup:
            log.warning("режим настройки — отвечаю только на /start: %s", "; ".join(cfg.problems()))

        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if scheduler is not None:
            await _quietly("планировщик", scheduler.stop())
        await _quietly("userbot", userbot.stop())
        await _quietly("фоновые задачи", services.drain(timeout=5.0))
        await _quietly("новости", news.aclose())
        await _quietly("распознавание", stt.aclose())
        await _quietly("модель", llm.aclose())
        await _quietly("база", db.close())
        await _quietly("сессия бота", bot.session.close())
        log.info("остановлен")
    return 0
