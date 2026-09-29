"""Вход userbot в аккаунт владельца: `python -m oracle.userbot_login`.

Один раз, в терминале: телефон → код из Telegram → пароль 2FA (если есть). Результат —
файл сессии `USERBOT_SESSION`.session. Он равен ПОЛНОМУ доступу к аккаунту: права 600,
никому не отдавать, в git не класть. Отозвать: Telegram → Настройки → Устройства.
"""
from __future__ import annotations

import asyncio
import getpass
import logging
import os
import sys
from pathlib import Path
from typing import Any

from . import config
from .services.userbot import APP_VERSION, DEVICE_MODEL, entity_name, session_file

API_HELP = """\
Нужны TG_API_ID и TG_API_HASH в .env.
Где взять: https://my.telegram.org → войди своим номером → API development tools →
создай приложение (название и описание любые) → скопируй App api_id и App api_hash:
  TG_API_ID=1234567
  TG_API_HASH=0123456789abcdef0123456789abcdef"""

WARNING = """\
⚠️  Файл сессии {path} — это полный доступ к твоему аккаунту Telegram.
   Никому его не отдавай, не выкладывай и не коммить в git. Если утёк — Telegram →
   Настройки → Устройства → завершить сеанс «{device}»."""


def _protect(path: Path) -> bool:
    try:
        if path.exists():
            path.chmod(0o600)
            return True
    except OSError as e:
        print(f"Не смог выставить права 600 на {path}: {e} — сделай вручную: chmod 600 {path}")
    return False


async def _login(cfg: Any) -> Any:
    from telethon import TelegramClient
    client = TelegramClient(cfg.userbot_session, cfg.tg_api_id, cfg.tg_api_hash,
                            device_model=DEVICE_MODEL, app_version=APP_VERSION)
    try:
        await client.start(phone=lambda: input("Телефон (+7…): ").strip(),
                           password=lambda: getpass.getpass("Пароль 2FA (если есть): "),
                           code_callback=lambda: input("Код из Telegram: ").strip())
        return await client.get_me()
    finally:
        res = client.disconnect()
        if hasattr(res, "__await__"):
            await res


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    cfg = config.load()
    if not (cfg.tg_api_id and cfg.tg_api_hash):
        print(API_HELP)
        return 2
    try:
        import telethon  # noqa: F401
    except ImportError:
        print("Не установлен telethon: pip install telethon")
        return 2
    # всё, что создаст вход (файл сессии, его журнал), — сразу только владельцу, без окна «0644 до chmod»
    old_mask = os.umask(0o077) if hasattr(os, "umask") else None
    try:
        return _run(cfg)
    finally:
        if old_mask is not None:
            os.umask(old_mask)


def _run(cfg: Any) -> int:
    session = Path(cfg.userbot_session)
    try:
        session.parent.mkdir(parents=True, exist_ok=True)
        sf = session_file(session)
        if not sf.exists():
            os.close(os.open(sf, os.O_CREAT | os.O_WRONLY, 0o600))
    except OSError as e:
        print(f"Не могу создать папку или файл для сессии {session.parent}: {e}")
        return 1
    print(f"Вход в Telegram для userbot. Сессия: {session_file(session)}")
    try:
        me = asyncio.run(_login(cfg))
    except (KeyboardInterrupt, EOFError):
        print("\nОтменено.")
        return 130
    except Exception as e:
        print(f"Не вышло войти: {type(e).__name__}: {e}")
        return 1
    path = session_file(session)
    _protect(path)
    uname = getattr(me, "username", None)
    who = entity_name(me) + (f" (@{uname})" if uname else "") + f", id {getattr(me, 'id', '?')}"
    print(f"Готово: вошёл как {who}.")
    print(WARNING.format(path=path, device=DEVICE_MODEL))
    if not cfg.userbot_enabled:
        print("Чтобы бот начал пользоваться: USERBOT_ENABLED=1 в .env и перезапусти бота.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
