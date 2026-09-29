"""Userbot: доступ к СВОИМ чатам владельца через Telethon (его аккаунт, не бот).

Что умеет: список диалогов, поиск чата по имени/@username/id, история, образцы того, как
пишет сам владелец (для черновиков «его голосом»), отправка (только по кнопке владельца —
у модели инструмента отправки нет, см. tools/tg_chats.py) и, если USERBOT_NOTIFY, уведомления
о новых личных сообщениях с кнопкой «Предложить ответ».

Файл сессии (`USERBOT_SESSION`.session) = полный доступ к аккаунту. Создаётся один раз
командой `python -m oracle.userbot_login`, права 600, никому не отдавать.

Бот без userbot работает: не включён / нет ключей / нет входа → `ready = False`, в лог — почему,
а в `last_error` — код причины для /status: "config: …", "auth" (нет входа), "network: …" (нет
связи — app.py переподключает в фоне с растущей паузой). Подключение ограничено CONNECT_TIMEOUT:
Telethon без таймаута висит вечно, если TCP принят, а ответа нет.
Ошибки Telethon в публичных методах → ValueError с русским текстом (его увидит модель/владелец).
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator

from .. import timeutil
from ..db import normalize_text

log = logging.getLogger("oracle.userbot")

NOT_READY = ("userbot не подключён: включи USERBOT_ENABLED, задай TG_API_ID/TG_API_HASH "
             "и войди: python -m oracle.userbot_login")
NO_NETWORK = "userbot сейчас без связи с Telegram — переподключаюсь сам, попробуй через пару минут"
NOTIFY_EVERY = timedelta(minutes=5)       # не чаще одного уведомления на чат
NOTIFY_TEXT_MAX = 300
LAST_TEXT_MAX = 200
HISTORY_TEXT_MAX = 1500
SAMPLE_MAX = 300
SCAN_DIALOGS = 300                        # сколько диалогов просматривать при поиске
MEDIA = "[медиа]"
DEVICE_MODEL = "Baltia Oracle"
APP_VERSION = "0.1"
CONNECT_TIMEOUT = 30.0                    # connect + проверка входа + get_me — не дольше
_TME = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?@?([A-Za-z0-9_]{3,})/?$", re.I)
_USERNAME = re.compile(r"^@([A-Za-z0-9_]{3,})$")
_PHONE = re.compile(r"^\+\d{7,15}$")
_NUM = re.compile(r"^-?\d+$")


# ── мелочи (без Telethon — работают и с тестовыми двойниками) ─────────────────
def session_file(session: str | os.PathLike) -> Path:
    """Путь к файлу сессии так, как его строит Telethon (добавляет .session)."""
    s = str(session)
    return Path(s if s.endswith(".session") else s + ".session")


def entity_name(e: Any) -> str:
    """Человеческое имя пользователя/чата/канала."""
    if e is None:
        return "?"
    if getattr(e, "deleted", False):
        return "Удалённый аккаунт"
    title = getattr(e, "title", None)
    if title:
        return str(title)
    parts = [getattr(e, "first_name", None) or "", getattr(e, "last_name", None) or ""]
    name = " ".join(p.strip() for p in parts if p and p.strip())
    if name:
        return name
    u = getattr(e, "username", None)
    if u:
        return f"@{u}"
    return str(getattr(e, "id", "?"))


def peer_id(e: Any) -> int:
    """«Помеченный» id (как у диалогов: группы и каналы — отрицательные)."""
    try:
        from telethon import utils
        return int(utils.get_peer_id(e))
    except Exception:
        return int(getattr(e, "id"))


def _msg_text(m: Any) -> str:
    t = getattr(m, "message", None)
    if not isinstance(t, str):
        t = getattr(m, "raw_text", None) if isinstance(getattr(m, "raw_text", None), str) else ""
    return t.strip()


def _snip(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _dialog_kind(d: Any) -> str:
    if getattr(d, "is_user", False):
        return "user"
    if getattr(d, "is_group", False):
        return "group"
    if getattr(d, "is_channel", False):
        return "channel"
    return "group"


def _clamp(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(v) if not isinstance(v, bool) else default
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def _words(s: str) -> list[str]:
    return re.findall(r"[0-9a-zа-я_]+", normalize_text(s))


class NotReadyError(ValueError):
    """userbot не подключён (текст — что сделать владельцу)."""


def _is_entity_missing(e: Exception) -> bool:
    """Telethon не знает сущность по id (нет в кэше сессии) — лечится прогревом диалогов."""
    return isinstance(e, ValueError) and any(
        w in str(e) for w in ("Could not find the input entity", "Could not find the entity",
                               "Cannot find any entity"))


class Userbot:
    """Обёртка над TelegramClient. `client` можно подставить (тесты)."""

    def __init__(self, cfg: Any, db: Any, notifier: Any = None, client: Any = None):
        self.cfg = cfg
        self.db = db
        self.notifier = notifier
        self.client = client
        self.ready = False
        self.stopped = False                  # остановлен нами (stop), а не потерял связь
        self.last_error = ""                  # почему не подключён: "config: …" | "auth" | "network: …"
        self.me: Any = None
        self._last_notify: dict[int, Any] = {}
        self._handler_added = False
        self._warmed = False

    # ── жизненный цикл ──
    async def start(self) -> None:
        cfg = self.cfg
        self.ready = False
        self.stopped = False
        if not getattr(cfg, "userbot_enabled", False):
            log.info("userbot выключен (USERBOT_ENABLED)")
            self.last_error = "config: выключен (USERBOT_ENABLED)"
            return
        if not (getattr(cfg, "tg_api_id", 0) and getattr(cfg, "tg_api_hash", "")):
            log.info("userbot: нет TG_API_ID/TG_API_HASH — не подключаюсь")
            self.last_error = "config: нет TG_API_ID/TG_API_HASH"
            return
        if self.client is None:
            try:
                from telethon import TelegramClient
            except ImportError:
                log.warning("userbot: telethon не установлен (pip install telethon)")
                self.last_error = "config: не установлен telethon (pip install telethon)"
                return
            try:
                Path(cfg.userbot_session).parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            self.client = TelegramClient(cfg.userbot_session, cfg.tg_api_id, cfg.tg_api_hash,
                                         device_model=DEVICE_MODEL, app_version=APP_VERSION)

        async def connect_and_check() -> Any:
            await self.client.connect()
            if not await self.client.is_user_authorized():
                return None
            return await self.client.get_me()

        try:
            me = await asyncio.wait_for(connect_and_check(), CONNECT_TIMEOUT)
        except asyncio.CancelledError:
            await self._disconnect()
            raise
        except Exception as e:           # в т.ч. таймаут: TCP принят, а Telegram молчит
            name = type(e).__name__
            if isinstance(e, asyncio.TimeoutError):
                log.warning("userbot: Telegram не ответил за %.0f с — нет связи", CONNECT_TIMEOUT)
                self.last_error = "network: таймаут"
            elif "AuthKey" in name or "Unauthorized" in name or "SessionRevoked" in name:
                log.warning("userbot: Telegram не пускает (%s) — войди заново: python -m oracle.userbot_login", name)
                self.last_error = "auth"
            else:
                log.error("userbot: не подключился: %r", e)
                self.last_error = f"network: {type(e).__name__}"
            await self._disconnect()
            return
        if me is None:
            log.warning("userbot: нет входа — запусти python -m oracle.userbot_login")
            self.last_error = "auth"
            await self._disconnect()
            return
        self.me = me
        self.last_error = ""
        self._protect_session()
        self.ready = True
        log.info("userbot: вошёл как %s", entity_name(self.me))
        if getattr(cfg, "userbot_notify", False) and self.notifier is not None and not self._handler_added:
            try:
                from telethon import events
                self.client.add_event_handler(self._on_new_message, events.NewMessage(incoming=True))
                self._handler_added = True
            except Exception as e:
                log.error("userbot: не смог подписаться на новые сообщения: %r", e)

    async def stop(self) -> None:
        self.ready = False
        self.stopped = True
        await self._disconnect()

    async def wait_disconnected(self) -> None:
        """Дождаться, пока Telethon потеряет связь насовсем (его connection_retries кончились — клиент
        отключён, новые сообщения не приходят, запросы падают). Тогда ready=False и в last_error —
        «network: …»: /status скажет правду, а app.keep_userbot подключит заново."""
        client = self.client
        fut = getattr(client, "disconnected", None) if client is not None else None
        if fut is None:
            await asyncio.get_running_loop().create_future()      # клиент не умеет сказать — ждём вечно
            return
        try:
            await fut
        except asyncio.CancelledError:
            raise
        except Exception as e:           # Telethon кладёт в future ошибку, с которой сдался
            log.debug("userbot: связь потеряна: %r", e)
        if self.stopped:
            return
        self.ready = False
        self.last_error = "network: связь потеряна"
        log.warning("userbot: связь с Telegram потеряна")

    async def _disconnect(self) -> None:
        if self.client is None:
            return
        try:
            res = self.client.disconnect()
            if hasattr(res, "__await__"):
                await res
        except Exception as e:
            log.debug("userbot: disconnect: %r", e)

    def _protect_session(self) -> None:
        """Файл сессии — только владельцу (600)."""
        try:
            p = session_file(self.cfg.userbot_session)
            if p.exists() and (p.stat().st_mode & 0o077):
                p.chmod(0o600)
        except (OSError, TypeError, AttributeError):
            pass

    # ── защита ──
    def not_ready_text(self) -> str:
        """Почему не готов — человеческим текстом (нет связи — не повод перелогиниваться)."""
        if str(self.last_error or "").startswith("network"):
            return NO_NETWORK
        return NOT_READY

    def _need(self) -> Any:
        if not self.ready or self.client is None:
            raise NotReadyError(self.not_ready_text())
        return self.client

    def _fail(self, what: str, e: Exception) -> ValueError:
        """Исключение Telethon → ValueError с русским текстом."""
        log.warning("userbot: %s: %r", what, e)
        name = type(e).__name__
        secs = getattr(e, "seconds", None)
        if "FloodWait" in name or ("Flood" in name and secs):
            return ValueError(f"{what}: Telegram просит подождать {secs or 'немного'} с (защита от флуда)")
        if isinstance(e, (ConnectionError, OSError)) or "Connection" in name:
            return ValueError(f"{what}: нет связи с Telegram")
        if "Auth" in name or "Unauthorized" in name or "SessionRevoked" in name:
            return ValueError(f"{what}: Telegram не пускает — сессия отозвана? "
                              "Войди заново: python -m oracle.userbot_login")
        if "Blocked" in name:
            return ValueError(f"{what}: собеседник недоступен (блокировка)")
        if "Forbidden" in name or "Write" in name or "Banned" in name or "Restricted" in name:
            return ValueError(f"{what}: Telegram не разрешает ({name})")
        if _is_entity_missing(e) or "PeerId" in name or "Invalid" in name:
            return ValueError(f"{what}: Telegram не знает такой чат")
        return ValueError(f"{what}: {name}")

    async def _warm(self) -> None:
        """Прогнать диалоги, чтобы Telethon узнал сущности (после перезапуска кэш пуст)."""
        if self._warmed:
            return
        self._warmed = True
        try:
            async for _ in self.client.iter_dialogs(limit=SCAN_DIALOGS):
                pass
        except Exception as e:
            log.debug("userbot: прогрев диалогов: %r", e)

    async def _get_messages(self, chat: int, limit: int) -> list:
        client = self._need()
        try:
            res = await client.get_messages(chat, limit=limit)
        except ValueError as e:
            if not _is_entity_missing(e):
                raise
            await self._warm()
            res = await client.get_messages(chat, limit=limit)
        return list(res or [])

    async def _iter_dialogs(self, limit: int) -> AsyncIterator[Any]:
        async for d in self._need().iter_dialogs(limit=limit):
            yield d

    # ── чтение ──
    @staticmethod
    def _dialog_title(d: Any) -> str:
        return getattr(d, "name", None) or getattr(d, "title", None) or entity_name(getattr(d, "entity", None))

    def _dialog_dict(self, d: Any) -> dict:
        m = getattr(d, "message", None)
        text = _msg_text(m) if m is not None else ""
        if not text and m is not None:
            text = MEDIA if getattr(m, "media", None) else ""
        date = getattr(d, "date", None) or getattr(m, "date", None)
        return {
            "id": int(d.id),
            "title": self._dialog_title(d),
            "unread": int(getattr(d, "unread_count", 0) or 0),
            "last_text": _snip(text, LAST_TEXT_MAX),
            "last_out": bool(getattr(m, "out", False)) if m is not None else False,
            "last_date": timeutil.fmt_local(date, self.cfg.tz) if date else "—",
            "kind": _dialog_kind(d),
        }

    async def dialogs(self, limit: int = 20, unread_only: bool = False) -> list[dict]:
        """Свежие диалоги. unread_only — только с непрочитанными (личные первыми, архив мимо)."""
        self._need()
        limit = _clamp(limit, 20, 1, 100)
        scan = max(SCAN_DIALOGS, limit * 5) if unread_only else limit
        out: list[dict] = []
        try:
            async for d in self._iter_dialogs(scan):
                if unread_only:
                    if int(getattr(d, "unread_count", 0) or 0) <= 0 or getattr(d, "archived", False):
                        continue
                out.append(self._dialog_dict(d))
                if not unread_only and len(out) >= limit:
                    break
        except NotReadyError:
            raise
        except Exception as e:
            raise self._fail("не смог получить список чатов", e) from e
        if unread_only:   # люди важнее групп, группы — каналов; внутри — по свежести
            order = {"user": 0, "group": 1, "channel": 2}
            out.sort(key=lambda x: order.get(x["kind"], 1))
        return out[:limit]

    async def resolve(self, query: str | int, *, strict: bool = False) -> tuple[int, str]:
        """id / @username / t.me-ссылка / +телефон / кусок названия → (id чата, название).
        Не нашёл или нашёл несколько — ValueError с подсказкой. strict — @username/ссылку/телефон
        не угадывать по кускам названий (для отправки по сохранённому username: не тому человеку)."""
        client = self._need()
        if isinstance(query, bool) or query is None:
            raise ValueError("какой чат? назови имя, @username или id")
        if isinstance(query, int) or (isinstance(query, str) and _NUM.match(query.strip())):
            return await self._resolve_id(int(query))
        q = str(query).strip().strip("«»\"'")
        if not q:
            raise ValueError("какой чат? назови имя, @username или id")
        m = _USERNAME.match(q) or _TME.match(q)
        if m or _PHONE.match(q):
            key = m.group(1) if m else q
            try:
                ent = await client.get_entity(key)
            except Exception as e:
                log.info("userbot: get_entity(%s): %r", key, e)
                if m and not strict:   # может, это просто кусок названия с @
                    return await self._resolve_fuzzy(q.lstrip("@"), key)
                raise ValueError(f"не нашёл чат «{q}»") from e
            return peer_id(ent), entity_name(ent)
        if strict:
            raise ValueError(f"не нашёл «{q}»: нужен @username, ссылка t.me или телефон")
        return await self._resolve_fuzzy(q.lstrip("@").strip() or q)

    async def _resolve_id(self, cid: int) -> tuple[int, str]:
        client = self._need()
        try:
            ent = await client.get_entity(cid)
            return cid, entity_name(ent)
        except Exception as e:
            log.debug("userbot: get_entity(%s): %r", cid, e)
        try:   # сущности нет в кэше — ищем среди диалогов
            async for d in self._iter_dialogs(SCAN_DIALOGS):
                if int(d.id) == cid:
                    return cid, self._dialog_title(d)
        except Exception as e:
            raise self._fail("не смог найти чат", e) from e
        raise ValueError(f"не нашёл чат с id {cid}")

    async def _resolve_fuzzy(self, q: str, username: str = "") -> tuple[int, str]:
        nq = " ".join(_words(q))
        words = nq.split()
        if not words:
            raise ValueError(f"не нашёл чат «{q}»")
        from ..db import stem
        stems = [stem(w) for w in words]
        exact: list[tuple[int, str]] = []
        strict: list[tuple[int, str]] = []
        loose: list[tuple[int, str]] = []
        try:
            async for d in self._iter_dialogs(SCAN_DIALOGS):
                title = self._dialog_title(d)
                ent = getattr(d, "entity", None)
                uname = normalize_text(getattr(ent, "username", None) or "")
                hay = " ".join(_words(title))
                item = (int(d.id), title)
                if hay == nq or (uname and uname in (nq, normalize_text(username))):
                    exact.append(item)
                elif all(w in hay or (uname and w in uname) for w in words):
                    strict.append(item)
                else:   # «Маше» → «Маша»: основы слов как префиксы слов названия
                    tw = hay.split()
                    if all(any(t.startswith(s) for t in tw) for s in stems):
                        loose.append(item)
        except NotReadyError:
            raise
        except Exception as e:
            raise self._fail("не смог просмотреть чаты", e) from e
        for group in (exact, strict, loose):
            uniq = list(dict.fromkeys(group))
            if len(uniq) == 1:
                return uniq[0]
            if len(uniq) > 1:
                shown = "; ".join(f"«{t}» (id {i})" for i, t in uniq[:5])
                more = f" и ещё {len(uniq) - 5}" if len(uniq) > 5 else ""
                raise ValueError(f"под «{q}» подходит несколько чатов: {shown}{more} — уточни или назови id")
        raise ValueError(f"не нашёл чат «{q}»")

    async def history(self, chat: int | str, limit: int = 30) -> list[dict]:
        """Последние сообщения чата по порядку (старые сверху): {out, sender, text, date}."""
        if not isinstance(chat, int) or isinstance(chat, bool):
            chat, _ = await self.resolve(chat)
        limit = _clamp(limit, 30, 1, 100)
        try:
            msgs = await self._get_messages(chat, limit)
        except NotReadyError:
            raise
        except Exception as e:
            raise self._fail("не смог прочитать чат", e) from e
        out: list[dict] = []
        for m in reversed(msgs):   # Telethon отдаёт новые первыми
            if m is None or getattr(m, "action", None) is not None:   # служебные: «вступил в группу» и т.п.
                continue
            text = _msg_text(m) or (MEDIA if getattr(m, "media", None) else "")
            if not text:
                continue
            is_out = bool(getattr(m, "out", False))
            sender = "я" if is_out else entity_name(getattr(m, "sender", None))
            if sender == "?":
                sender = "собеседник"
            d = getattr(m, "date", None)
            out.append({"out": is_out, "sender": sender, "text": _snip(text, HISTORY_TEXT_MAX),
                        "date": timeutil.fmt_local(d, self.cfg.tz) if d else "—"})
        return out

    async def style_samples(self, chat: int | None = None, limit: int = 25) -> list[str]:
        """Как пишет сам владелец: его исходящие (≥ 2 слов), сначала из этого чата, потом из свежих личных.
        Лучшее усилие: ошибки глотаются, возвращается что набралось."""
        self._need()
        limit = _clamp(limit, 25, 1, 100)
        samples: list[str] = []
        seen: set[str] = set()

        def take(msgs: list) -> None:
            for m in msgs:
                if len(samples) >= limit:
                    return
                if not getattr(m, "out", False) or getattr(m, "fwd_from", None) is not None \
                        or getattr(m, "action", None) is not None:
                    continue
                t = _msg_text(m)
                if len(t.split()) < 2 or t in seen:
                    continue
                seen.add(t)
                samples.append(_snip(t, SAMPLE_MAX))

        if chat is not None:
            try:
                take(await self._get_messages(chat, 200))
            except Exception as e:
                log.info("userbot: образцы из чата %s: %r", chat, e)
        if len(samples) < limit:
            my_id = getattr(self.me, "id", None)
            looked = 0
            try:
                async for d in self._iter_dialogs(40):
                    if len(samples) >= limit or looked >= 8:
                        break
                    ent = getattr(d, "entity", None)
                    if not getattr(d, "is_user", False) or getattr(ent, "bot", False) \
                            or getattr(ent, "is_self", False) or int(d.id) == chat \
                            or (my_id is not None and int(d.id) == int(my_id)):
                        continue
                    looked += 1
                    try:
                        take(await self._get_messages(int(d.id), 50))
                    except Exception as e:
                        log.info("userbot: образцы из %s: %r", d.id, e)
            except Exception as e:
                log.info("userbot: образцы по диалогам: %r", e)
        return samples[:limit]

    # ── отправка (только по кнопке владельца) ──
    async def send(self, chat: int, text: str) -> bool:
        client = self._need()
        text = (text or "").strip()
        if not text:
            raise ValueError("пустое сообщение не отправляю")
        if len(text) > 4096:
            raise ValueError("слишком длинно для одного сообщения Telegram (больше 4096 символов)")
        try:
            try:
                await client.send_message(chat, text, parse_mode=None)
            except ValueError as e:
                if not _is_entity_missing(e):
                    raise
                await self._warm()
                await client.send_message(chat, text, parse_mode=None)
        except NotReadyError:
            raise
        except Exception as e:
            raise self._fail("не отправил", e) from e
        return True

    # ── уведомления о новых личных ──
    async def _on_new_message(self, event: Any) -> None:
        try:
            if not getattr(event, "is_private", False) or getattr(event, "out", False):
                return
            sender = await event.get_sender()
            if sender is None or getattr(sender, "bot", False) or getattr(sender, "is_self", False):
                return
            if self.me is not None and getattr(sender, "id", None) == getattr(self.me, "id", object()):
                return
            chat_id = int(event.chat_id)
            now = timeutil.now_utc()
            last = self._last_notify.get(chat_id)
            if last is not None and now - last < NOTIFY_EVERY:
                return
            self._last_notify[chat_id] = now
            msg = getattr(event, "message", None)
            text = _msg_text(msg) if msg is not None else ""
            text = _snip(text, NOTIFY_TEXT_MAX) if text else MEDIA
            line = f"💬 {entity_name(sender)}: {text}"
            buttons = [[("✍️ Предложить ответ", f"draft:new:{chat_id}")]]
            send_html = getattr(self.notifier, "send_html", None)
            if send_html is not None:   # чужой текст — как есть: его «[текст](ссылка)» не станет ссылкой
                await send_html(html.escape(line, quote=False), buttons=buttons, silent=False)
            else:
                await self.notifier.send(line, buttons=buttons, silent=False)
        except Exception:
            log.exception("userbot: уведомление о новом сообщении")
            return
        await self._record_notice(entity_name(sender), chat_id)

    async def _record_notice(self, name: str, chat_id: int) -> None:
        """Уведомление — в разговор: «ответь ему, что буду через 10 минут» голосом должен понять, кому.
        Только кто и где, без самого текста: написать владельцу может любой, и его слова в истории
        попали бы в ход владельца без защиты от чужого текста (TurnGuard). Прочитать — tg_read_chat:
        это чужой текст с пометкой, и ход после него «заражён»."""
        try:
            await self.db.add_message(
                "event", f"Владельцу пришло личное сообщение от «{name}» (chat_id {chat_id}); что там — "
                         f"tg_read_chat, ответ — tg_draft_reply", "system")
        except Exception:
            log.debug("userbot: не записал уведомление в разговор", exc_info=True)
