"""Несколько людей в одном боте. У каждого своё пространство: база, память, позиции бота, дневник,
напоминания, идеи, свой агент и свой планировщик. Общие только клиенты модели и распознавания (ключи одни).

Главный — первый в OWNER_ID; его данные — data/oracle.db, как и раньше. Остальные — data/users/<id>/oracle.db.
По умолчанию бот — для одного человека, и этот модуль не включается. Как появляется второй:
  • вписан в .env: OWNER_ID=главный,второй;
  • или (ALLOW_REQUESTS=1) сам пишет боту /start → главному приходит «просится — пустить?» с кнопками.
    Пущенные хранятся в базе главного (kv «users»); убрать — /users (кнопка «✖️ Убрать»).
Кого убрали — его пространство засыпает (планировщик стоит, данные на диске), вернуть — снова «Пустить».
Userbot (чтение чатов) — только у главного: сессия — это его аккаунт.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from html import escape
from pathlib import Path
from typing import Any

log = logging.getLogger("oracle.tenants")

USERS_KEY = "users"                  # в базе главного: id людей, которых он пустил кнопкой
DENIED_KEY = "users_denied"          # …и кому отказал (их запросы больше не беспокоят)
REQUEST_EVERY = 24 * 3600.0          # один и тот же человек просится не чаще раза в сутки
MAX_USERS = 20                       # предохранитель: бот личный, не сервис

WELCOME = ("Привет. Тебя пустили — теперь я работаю и на тебя. У тебя своё пространство: память, "
           "напоминания, идеи и дневник — чужие я не вижу и твои никому не показываю.\n\n"
           "Пиши или присылай голосовые как человеку. Что умею — /help.")


def user_label(user: Any) -> str:
    """«Имя Фамилия (@ник)» — для сообщения главному (HTML-экранировано)."""
    name = " ".join(x for x in (getattr(user, "first_name", "") or "", getattr(user, "last_name", "") or "") if x)
    nick = getattr(user, "username", "") or ""
    out = escape(name.strip() or "Без имени")
    return out + (f" (@{escape(nick)})" if nick else "")


@dataclass
class Tenant:
    """Пространство одного человека."""
    uid: int
    cfg: Any
    db: Any
    ctx: Any
    deps: Any
    agent: Any = None
    scheduler: Any = None
    news: Any = None
    router: Any = None
    primary: bool = False
    own: bool = True                 # ресурсы (база, новости) открыл менеджер — ему и закрывать


class Tenants:
    """Кто допущен и чьё пространство. `allowed(uid)` — для OwnerOnly; `tenant(uid)` — для обработчиков."""

    def __init__(self, cfg: Any, bot: Any, *, llm: Any, stt: Any = None, tts: Any = None,
                 primary: Tenant, base_dir: Path | None = None):
        self.cfg = cfg
        self.bot = bot
        self.llm = llm
        self.stt = stt
        self.tts = tts
        self.primary = primary
        self.primary_id = int(primary.uid)
        primary.primary = True
        primary.own = False                       # главного открыл app — он его и закроет
        self.by_id: dict[int, Tenant] = {self.primary_id: primary}
        self.dormant: dict[int, Tenant] = {}
        self.static: set[int] = {int(u) for u in getattr(cfg, "owners", ()) or ()}
        self.base_dir = Path(base_dir or Path(cfg.data_dir) / "users")
        self.dp: Any = None
        self._requested: dict[int, float] = {}
        self._names: dict[int, str] = {}      # имя из запроса доступа — чтобы бот знал, как его зовут
        self._lock = asyncio.Lock()

    # ── кто допущен ──
    def allowed(self, uid: Any) -> bool:
        try:
            return int(uid) in self.by_id
        except (TypeError, ValueError):
            return False

    def tenant(self, uid: Any) -> Tenant | None:
        try:
            return self.by_id.get(int(uid))
        except (TypeError, ValueError):
            return None

    async def _approved(self) -> list[int]:
        raw = await self.primary.db.kv_get(USERS_KEY, []) or []
        return [int(x) for x in raw if str(x).lstrip("-").isdigit()]

    async def _save_approved(self, ids: list[int]) -> None:
        await self.primary.db.kv_set(USERS_KEY, sorted(set(int(x) for x in ids)))

    # ── маршрутизация ──
    def attach(self, dp: Any) -> None:
        """Подключить роутеры всех пространств к диспетчеру (каждый — только для своего человека)."""
        self.dp = dp
        for t in self.by_id.values():
            self._include(t)

    def _include(self, t: Tenant) -> None:
        if self.dp is None or t.router is not None:
            return
        from aiogram import F

        from .bot.handlers import build_router
        r = build_router(t.deps)
        r.message.filter(F.from_user.id == t.uid)
        r.edited_message.filter(F.from_user.id == t.uid)
        r.callback_query.filter(F.from_user.id == t.uid)
        t.router = r
        self.dp.include_router(r)

    # ── жизненный цикл ──
    async def load(self) -> None:
        """Открыть пространства всех, кто в .env и кого главный пустил раньше."""
        ids = [u for u in self.static if u != self.primary_id]
        ids += [u for u in await self._approved() if u not in ids and u != self.primary_id]
        for uid in ids[:MAX_USERS]:
            try:
                await self.open(uid)
            except Exception:
                log.exception("пространство %s не открылось", uid)

    async def open(self, uid: int) -> Tenant:
        """Открыть (или разбудить) пространство человека и запустить его планировщик."""
        uid = int(uid)
        if uid in self.by_id:
            return self.by_id[uid]
        t = self.dormant.pop(uid, None)
        if t is None:
            t = await self._build(uid)
        if t.scheduler is not None and not t.scheduler.running:
            t.scheduler.start()
        self.by_id[uid] = t
        self._include(t)
        log.info("пространство %s открыто (%s)", uid, t.cfg.db_path)
        return t

    async def _build(self, uid: int) -> Tenant:
        from .agent import Agent
        from .bot.handlers import Deps
        from .bot.notifier import BotNotifier
        from .db import DB
        from .services.news import NewsService
        from .services.scheduler import Scheduler
        from .tools.base import Services, ToolContext

        folder = self.base_dir / str(uid)
        cfg = replace(self.cfg, owner_id=uid, owner_ids=(uid,), owner_name="", owner_gender="m",
                      data_dir=folder, db_path=folder / "oracle.db",
                      userbot_enabled=False, userbot_notify=False)
        db = await DB(cfg.db_path).open()
        services = Services()
        ctx = ToolContext(cfg=cfg, db=db, llm=self.llm, services=services)
        news = NewsService(cfg, db)
        services.news = news
        services.tts = self.tts
        notifier = BotNotifier(self.bot, uid)
        services.notifier = notifier
        try:                                  # прерванные разборы идей — прежний статус
            from .tools import ideas as _ideas
            await _ideas.recover_interrupted(db)
        except Exception:
            log.exception("пространство %s: не восстановил прерванные разборы идей", uid)
        agent = Agent(ctx)
        scheduler = Scheduler(ctx)
        deps = Deps(cfg=cfg, db=db, llm=self.llm, ctx=ctx, agent=agent, stt=self.stt, tts=self.tts,
                    userbot=None, news=news, notifier=notifier, scheduler=scheduler)
        return Tenant(uid=uid, cfg=cfg, db=db, ctx=ctx, deps=deps, agent=agent, scheduler=scheduler, news=news)

    async def stop_all(self) -> None:
        """Остановить и закрыть всё, кроме главного (его закрывает app)."""
        for t in [*self.by_id.values(), *self.dormant.values()]:
            if not t.own:
                continue
            await _quiet(t.scheduler.stop() if t.scheduler is not None else None)
            await _quiet(t.ctx.services.drain(timeout=5.0))
            await _quiet(t.news.aclose() if t.news is not None else None)
            await _quiet(t.db.close())

    # ── доступ ──
    async def request_access(self, user: Any) -> str:
        """Чужой написал /start. → "sent" (главного спросили), "wait" (уже спрашивали недавно),
        "denied" (главный отказал), "full" (мест нет)."""
        uid = int(getattr(user, "id", 0) or 0)
        if not uid or self.allowed(uid):
            return "wait"
        denied = await self.primary.db.kv_get(DENIED_KEY, []) or []
        if uid in denied:
            return "denied"
        if len(self.by_id) >= MAX_USERS:
            return "full"
        now = time.monotonic()
        last = self._requested.get(uid)
        if last is not None and now - last < REQUEST_EVERY:
            return "wait"
        self._requested[uid] = now
        first = str(getattr(user, "first_name", "") or "").strip()
        if first:
            if len(self._names) >= 500:
                self._names.clear()
            self._names[uid] = first[:64]
        text = (f"👤 {user_label(user)}, id <code>{uid}</code>, просится пользоваться ботом.\n"
                "Если пустишь — у него будет своё пространство: своя память, напоминания и идеи. "
                "Твоё он не увидит, его — ты тоже.")
        buttons = [[("✅ Пустить", f"user:add:{uid}"), ("✖️ Нет", f"user:deny:{uid}")]]
        notifier = self.primary.ctx.services.notifier
        if notifier is None:
            return "wait"
        send_html = getattr(notifier, "send_html", None)
        if send_html is not None:
            await send_html(text, buttons)
        else:
            await notifier.send(text, buttons)
        log.info("человек %s просит доступ — спросил главного", uid)
        return "sent"

    async def approve(self, uid: int) -> Tenant:
        uid = int(uid)
        async with self._lock:
            if uid == self.primary_id or uid in self.by_id:
                return self.by_id[uid]
            if len(self.by_id) >= MAX_USERS:
                raise ValueError(f"больше {MAX_USERS} человек не пускаю — бот личный")
            ids = await self._approved()
            if uid not in ids:
                await self._save_approved([*ids, uid])
            denied = [d for d in (await self.primary.db.kv_get(DENIED_KEY, []) or []) if d != uid]
            await self.primary.db.kv_set(DENIED_KEY, denied)
            t = await self.open(uid)
            name = self._names.pop(uid, "")
            if name and not await _quiet_value(t.db.kv_get("owner_name", ""), ""):
                await _quiet(t.db.kv_set("owner_name", name))
        try:
            await self.bot.send_message(uid, WELCOME, parse_mode=None)
        except Exception as e:
            log.warning("не смог поприветствовать %s: %s", uid, e)
        return t

    async def deny(self, uid: int) -> None:
        uid = int(uid)
        denied = await self.primary.db.kv_get(DENIED_KEY, []) or []
        if uid not in denied:
            await self.primary.db.kv_set(DENIED_KEY, [*denied, uid][-500:])

    async def remove(self, uid: int) -> bool:
        """Убрать пущенного кнопкой. Главного и тех, кто в .env, так не убрать (ValueError)."""
        uid = int(uid)
        if uid == self.primary_id:
            raise ValueError("себя убрать нельзя")
        if uid in self.static:
            raise ValueError("он вписан в OWNER_ID в .env — убери оттуда и перезапусти бота")
        async with self._lock:
            await self._save_approved([u for u in await self._approved() if u != uid])
            t = self.by_id.pop(uid, None)
            if t is None:
                return False
            if t.scheduler is not None:
                await _quiet(t.scheduler.stop())
            self.dormant[uid] = t              # данные остаются; роутер молчит — middleware не пустит
        log.info("пространство %s усыплено", uid)
        return True

    async def listing(self) -> list[dict]:
        """Кто сейчас допущен: [{uid, name, primary, static}] — для /users."""
        out = []
        for uid, t in self.by_id.items():
            name = str(await _quiet_value(t.db.kv_get("owner_name", ""), "") or "")
            out.append({"uid": uid, "name": name or (getattr(t.cfg, "owner_name", "") or ""),
                        "primary": uid == self.primary_id, "static": uid in self.static})
        return out


async def _quiet(aw: Any) -> None:
    if aw is None:
        return
    try:
        await aw
    except Exception:
        log.exception("пространство: шаг остановки")


async def _quiet_value(aw: Any, default: Any) -> Any:
    try:
        return await aw
    except Exception:
        return default
