"""Telegram: команды, текст, голос, кнопки.

Текст и расшифровка голосового уходят агенту (`Agent.handle`); пока он думает, в чате висит
«печатает…». Ответ — текстом (markdown → HTML), следом вложения из outbox (файлы, кнопки)
и, если включено, голосом (edge-tts). Команды работают напрямую с модулями — без модели
(кроме /today, /news и /deep). Кнопки — по таблице callback_data из ARCHITECTURE.md;
разбор — чистой функцией `parse_cb`, на нажатие отвечаем всегда.

Любое исключение в обработчике превращается в «⚠️ Ошибка: …», а не в молчание.
Модули инструментов и сервисов импортируются лениво — внутри обработчиков.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import random
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command

from .. import timeutil
from ..tools.base import Buttons, OutItem, ToolContext
from . import keyboards
from .notifier import NO_PREVIEW, BotNotifier
from .render import escape, md_to_html

log = logging.getLogger("oracle.bot")


@dataclass
class Deps:
    """Всё, что нужно обработчикам. Любой сервис может быть None (не включён / режим настройки)."""
    cfg: Any
    db: Any
    llm: Any
    ctx: ToolContext                 # базовый контекст инструментов (services — живые)
    agent: Any = None                # agent.Agent; None — режим настройки
    stt: Any = None                  # services.stt.STT
    tts: Any = None                  # services.tts.TTS
    userbot: Any = None              # services.userbot.Userbot
    news: Any = None                 # services.news.NewsService
    notifier: Any = None             # BotNotifier (чат владельца)
    scheduler: Any = None            # services.scheduler.Scheduler


# ── тексты ───────────────────────────────────────────────────────────────────
COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "знакомство"),
    ("help", "что умею и как просить"),
    ("today", "сводка на сегодня"),
    ("reminders", "напоминания и будильники"),
    ("calendar", "календарь: /calendar [дней]"),
    ("ics", "выгрузить календарь файлом .ics"),
    ("birthdays", "дни рождения"),
    ("ideas", "последние идеи"),
    ("idea", "идея целиком: /idea N"),
    ("projects", "проекты"),
    ("news", "новости с разбором: /news [тема]"),
    ("memory", "что я о тебе помню"),
    ("forget", "забыть факт: /forget N"),
    ("opinions", "мои позиции"),
    ("journal", "мой дневник"),
    ("mode", "быстрый ⇄ глубокий режим"),
    ("deep", "подумать глубоко: /deep вопрос"),
    ("voice", "отвечать голосом: нет / на голос / всегда"),
    ("reset", "начать разговор с чистого листа"),
    ("backup", "резервная копия базы"),
    ("chats", "твои непрочитанные чаты"),
    ("status", "состояние бота"),
)

START_TEXT = """\
Привет{name}. Я {bot} — твой личный напарник, а не справочное бюро: помню, что ты рассказывал, \
держу свои позиции и спорю, когда не согласен, слежу за твоими делами.

Что умею:
⏰ напоминания и будильники — такие, что не отстанут, пока не встанешь
📅 календарь (и выгрузка в твой календарь файлом)
🎂 дни рождения — напомню заранее и напишу поздравление без штампов
💡 идеи — оценю честно, без лести; могу додумать глубоко
📋 проекты и задачи со сроками
🗞 новости с разбором: где факт, где заявление, где пропаганда
🧠 память о тебе, мои собственные позиции и дневник

Просто пришли голосовое — или напиши, как человеку. Команды — /help."""

HELP_TEXT = """\
Проще всего — голосом или обычными словами. Например:
• «разбуди меня завтра в 6:30 и не отставай, пока не встану»
• «напоминай по будням в 9 делать зарядку»
• «у Лёхи др 14 марта, он фанат рыбалки»
• «у меня идея: …» — оценю честно
• «найди мою идею про кофейню»
• «что в новостях про …»
• «заведи проект …», «добавь задачу …»
• «запиши встречу в пятницу в 15:00»
• «что ты думаешь о …»

Команды:
/today — сводка на сегодня
/reminders — напоминания (кнопки ✖️ — отменить)
/calendar [дней] — календарь, по умолчанию на неделю
/ics — выгрузить календарь файлом
/birthdays — дни рождения
/ideas — последние идеи; /idea N — одна целиком
/projects — проекты
/news [тема] — новости с разбором
/memory — что я о тебе помню; /forget N — забыть факт
/opinions — мои позиции; /journal — мой дневник
/mode — быстрый ⇄ глубокий режим; /deep вопрос — один вопрос глубоко
/voice — отвечать голосом: нет / на голосовые / всегда
/reset — начать разговор с чистого листа (память остаётся)
/backup — резервная копия базы
/chats — твои непрочитанные чаты (если подключён userbot)
/status — состояние бота"""

DEEP_NOTE = "🧠 Думаю глубоко — это может занять пару минут."
STALE = "Кнопка устарела"
NOT_HEARD = "Не разобрал ни слова. Повтори?"
STT_HOWTO = ("Голосовые пока не понимаю — распознавание не настроено. Самое простое: бесплатный ключ "
             "на console.groq.com → строка GROQ_API_KEY=… в .env и перезапуск. Или локально, без "
             "интернета: pip install -r requirements-voice.txt (faster-whisper подхватится сам).")
USERBOT_HOWTO = ("Userbot не подключён, поэтому твоих чатов я не вижу. Чтобы подключить:\n"
                 "1) в .env: USERBOT_ENABLED=true, TG_API_ID и TG_API_HASH (берутся на my.telegram.org);\n"
                 "2) один раз войти: python -m oracle.userbot_login;\n"
                 "3) перезапустить бота.\n"
                 "Писать в чаты сам я не буду — только черновики, отправляешь ты кнопкой.")
OTHER_MEDIA = "Пока понимаю текст и голосовые. Картинки и файлы не читаю — перескажи словами."
MODE_TEXT = {
    "deep": "🧠 Режим: глубокий. Думаю дольше (бывает, минуты), зато основательно — для разборов "
            "и сложных вопросов. /mode — вернуть быстрый.",
    "fast": "⚡ Режим: быстрый. Отвечаю за секунды. Один сложный вопрос — /deep вопрос, "
            "весь разговор глубоко — /mode.",
}
TTS_MODES = ("off", "mirror", "always")
TTS_NEXT = {"off": "mirror", "mirror": "always", "always": "off"}
TTS_TEXT = {
    "off": "🔇 Голосом не отвечаю — только текстом.",
    "mirror": "🎙 На голосовые отвечаю голосом (и текстом), на текст — текстом.",
    "always": "🔊 Отвечаю голосом всегда (и текстом тоже).",
}
TTS_STATUS = {"off": "выключены", "mirror": "голосом на голосовые", "always": "всегда"}
FACT_CATEGORIES = {"person": "Люди", "preference": "Предпочтения", "plan": "Планы", "work": "Работа",
                   "health": "Здоровье", "general": "Общее", "other": "Другое"}

MAX_DOWNLOAD = 20 * 1024 * 1024        # больше Bot API скачать не даёт
BACKUP_MAX = 49 * 1024 * 1024          # и загрузить больше 50 МБ — тоже
TRANSCRIPT_MAX = 3000
REMINDER_BUTTONS = 20
FACT_BUTTONS = 30
CALENDAR_MAX_ITEMS = 60
ERR_MAX = 400


# ── чистые помощники ─────────────────────────────────────────────────────────
CB_ARITY: dict[tuple[str, str], int] = {
    ("rem", "done"): 1, ("rem", "snz"): 2, ("rem", "del"): 1,
    ("wake", "ans"): 2,
    ("bday", "regen"): 1, ("bday", "send"): 1,
    ("idea", "deep"): 1,
    ("draft", "new"): 1, ("draft", "send"): 1, ("draft", "regen"): 1, ("draft", "drop"): 1,
    ("fact", "del"): 1,
}
_CB_INT = re.compile(r"^-?\d{1,20}$")


def parse_cb(data: Any) -> tuple[str, str, list[int]] | None:
    """'rem:snz:12:10' → ('rem', 'snz', [12, 10]). Неизвестное, не та арность, не числа → None."""
    if not isinstance(data, str) or not data or len(data.encode("utf-8")) > keyboards.CALLBACK_MAX_BYTES:
        return None
    parts = data.split(":")
    if len(parts) < 2:
        return None
    key = (parts[0], parts[1])
    raw = parts[2:]
    if CB_ARITY.get(key) != len(raw) or not all(_CB_INT.match(x) for x in raw):
        return None
    return key[0], key[1], [int(x) for x in raw]


def make_challenge(rng: random.Random | Any = None) -> tuple[str, int, list[int]]:
    """Задачка для будильника: (вопрос, ответ, 4 перемешанных варианта). Сумма двух двузначных —
    сонному мозгу надо проснуться, чтобы решить, но не больше."""
    r = rng or random
    a, b = r.randint(12, 89), r.randint(12, 89)
    answer = a + b
    options = {answer}
    deltas = (1, 2, 9, 10, 11, -1, -2, -9, -10, -11)
    while len(options) < 4:
        options.add(answer + r.choice(deltas))
    opts = list(options)
    r.shuffle(opts)
    return f"{a} + {b} = ?", answer, opts


def challenge_buttons(rid: int, options: list[int]) -> Buttons:
    return [[(str(n), f"wake:ans:{int(rid)}:{int(n)}") for n in options]]


def challenge_key(rid: int) -> str:
    return f"challenge:{int(rid)}"


def command_args(text: Any) -> str:
    """'/news@Bot  про нефть' → 'про нефть'."""
    parts = str(text or "").strip().split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def int_arg(s: str, default: int, lo: int, hi: int) -> int:
    m = re.match(r"\s*-?\d+", s or "")
    if not m:
        return default
    return max(lo, min(hi, int(m.group(0))))


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def err_text(e: BaseException) -> str:
    """Текст ошибки для владельца: ValueError — как есть (он человеческий), прочее — с типом."""
    s = str(e).strip()
    if not isinstance(e, ValueError):
        s = f"{type(e).__name__}: {s}" if s else type(e).__name__
    s = s or "неизвестная ошибка"
    return s if len(s) <= ERR_MAX else s[: ERR_MAX - 1] + "…"


def _cut(s: str, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


class _Cb:
    """Ответ на нажатие кнопки — ровно один раз (Telegram ждёт его, иначе крутит часики)."""

    def __init__(self, query: Any):
        self.query = query
        self.answered = False

    async def answer(self, text: str | None = None, *, alert: bool = False) -> None:
        if self.answered:
            return
        self.answered = True
        try:
            await self.query.answer(text=_cut(text, 200) if text else None, show_alert=True if alert else None)
        except Exception as e:     # устаревший query и т.п. — не страшно
            log.debug("answerCallbackQuery: %r", e)


# ── обработчики ──────────────────────────────────────────────────────────────
class Handlers:
    """Обработчики команд, сообщений и кнопок. Методы можно звать напрямую (тесты)."""

    TYPING_EVERY = 4.0

    def __init__(self, deps: Deps):
        self.d = deps
        self._sending: set[int] = set()
        self.routes: dict[tuple[str, str], Callable[[Any, _Cb, list[int]], Awaitable[None]]] = {
            ("rem", "done"): self.cb_rem_done, ("rem", "snz"): self.cb_rem_snooze,
            ("rem", "del"): self.cb_rem_delete, ("wake", "ans"): self.cb_wake_answer,
            ("bday", "regen"): self.cb_bday_regen, ("bday", "send"): self.cb_bday_send,
            ("idea", "deep"): self.cb_idea_deep,
            ("draft", "new"): self.cb_draft_new, ("draft", "send"): self.cb_draft_send,
            ("draft", "regen"): self.cb_draft_regen, ("draft", "drop"): self.cb_draft_drop,
            ("fact", "del"): self.cb_fact_delete,
        }

    def commands(self) -> dict[str, Callable[[Any], Awaitable[None]]]:
        return {name: getattr(self, f"cmd_{name}") for name, _ in COMMANDS}

    # ── мелочи ──
    @property
    def cfg(self) -> Any:
        return self.d.cfg

    @property
    def db(self) -> Any:
        return self.d.db

    @property
    def ctx(self) -> ToolContext:
        return self.d.ctx

    @property
    def tz(self) -> Any:
        return self.cfg.tz

    def _bot(self, event: Any) -> Any:
        try:
            bot = getattr(event, "bot", None)
        except Exception:
            bot = None
        return bot or getattr(self.d.notifier, "bot", None)

    @staticmethod
    def _chat_of(event: Any) -> int | None:
        """Чат события: у сообщения — его чат, у нажатия кнопки — чат сообщения с кнопкой."""
        msg = event if getattr(event, "chat", None) is not None else getattr(event, "message", None)
        chat = getattr(getattr(msg, "chat", None), "id", None)
        if chat is None:
            chat = getattr(getattr(event, "from_user", None), "id", None)
        return chat

    def _out(self, event: Any) -> Any:
        """Кому отвечать: notifier владельца, а если сообщение пришло из другого чата — в тот чат."""
        n = self.d.notifier
        chat_id = self._chat_of(event)
        if n is None or chat_id is None or getattr(n, "chat_id", chat_id) == chat_id:
            return n
        bot = getattr(n, "bot", None)
        return BotNotifier(bot, chat_id) if bot is not None else n

    @asynccontextmanager
    async def _typing(self, event: Any, action: str = ChatAction.TYPING) -> AsyncIterator[None]:
        """Пока внутри — раз в TYPING_EVERY секунд «печатает…» (или «записывает голосовое…»)."""
        bot, chat_id = self._bot(event), self._chat_of(event)
        if bot is None or chat_id is None or not hasattr(bot, "send_chat_action"):
            yield
            return

        async def loop() -> None:
            while True:
                try:
                    await bot.send_chat_action(chat_id=chat_id, action=action)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.debug("send_chat_action: %r", e)
                await asyncio.sleep(self.TYPING_EVERY)

        task = asyncio.create_task(loop())
        await asyncio.sleep(0)             # первое «печатает…» уходит сразу, а не после ответа
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _deep_mode(self) -> bool:
        return str(await self.db.kv_get("mode", "fast") or "").strip().lower() == "deep"

    async def _tts_mode(self) -> str:
        v = str(await self.db.kv_get("tts_mode", self.cfg.tts_default) or "").strip().lower()
        return v if v in TTS_MODES else "off"

    def _userbot(self) -> Any:
        ub = self.d.userbot or getattr(self.ctx.services, "userbot", None)
        return ub if ub is not None and getattr(ub, "ready", False) else None

    def _setup_text(self) -> str:
        probs = self.cfg.problems()
        if not probs:
            return "Мозг не запущен — перезапусти бота."
        return ("Я пока в режиме настройки — думать не могу, не хватает:\n"
                + "\n".join(f"• {p}" for p in probs) + "\nПоправь .env и перезапусти бота.")

    def _local_midnight(self) -> datetime:
        return datetime.combine(timeutil.now_local(self.tz).date(), time(0, 0), tzinfo=self.tz)

    async def _report(self, event: Any, e: BaseException) -> None:
        try:
            out = self._out(event)
            if out is not None:
                await out.send(f"⚠️ Ошибка: {err_text(e)}")
        except Exception:
            log.exception("не смог сообщить об ошибке")

    # ── обёртки для aiogram ──
    async def guard(self, fn: Callable[[Any], Awaitable[None]], message: Any) -> None:
        try:
            await fn(message)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("обработчик %s упал", getattr(fn, "__name__", fn))
            await self._report(message, e)

    def wrap(self, fn: Callable[[Any], Awaitable[None]]) -> Callable[[Any], Awaitable[None]]:
        async def handler(message: Any) -> None:
            await self.guard(fn, message)
        handler.__name__ = getattr(fn, "__name__", "handler")
        return handler

    # ── доставка ответа ──
    async def _deliver(self, out: Any, items: list[OutItem] | None) -> None:
        """Вложения из outbox инструментов: файлы, голос, сообщения с кнопками."""
        for item in items or []:
            try:
                if item.kind == "file" and item.data is not None:
                    await out.send_file(item.data, item.filename or "file.bin", item.text)
                elif item.kind == "voice" and item.data is not None:
                    await out.send_voice(item.data, item.filename or "voice.mp3", item.text)
                elif item.text:
                    await out.send(item.text, item.buttons)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("вложение %s не ушло", item.kind)
                await out.send(f"⚠️ Не отправил вложение ({item.filename or item.kind}): {err_text(e)}")

    async def _answer(self, message: Any, text: str, *, via: str = "text", deep: bool | None = None) -> None:
        """Реплика владельца → агент → ответ текстом, вложения, при нужде — голосом."""
        out = self._out(message)
        agent = self.d.agent
        if agent is None:
            await out.send(self._setup_text())
            return
        thinking_deep = deep if deep is not None else await self._deep_mode()
        if thinking_deep:
            await out.send(DEEP_NOTE)
        async with self._typing(message):
            reply = await agent.handle(text, via=via, deep=deep)
        reply_text = str(getattr(reply, "text", "") or "")
        if reply_text.strip():
            await out.send(reply_text)
        await self._deliver(out, getattr(reply, "outbox", None))
        await self._speak(message, out, reply_text, via, error=getattr(reply, "error", None))

    async def _speak(self, event: Any, out: Any, text: str, via: str, *, error: Any = None) -> None:
        tts = self.d.tts
        if tts is None or error or not text.strip():
            return
        try:
            if not tts.available():
                return
            mode = await self._tts_mode()
            if not (mode == "always" or (mode == "mirror" and via == "voice")):
                return
            async with self._typing(event, ChatAction.RECORD_VOICE):
                audio = await tts.synth(text)
            await out.send_voice(audio, "voice.mp3")
        except asyncio.CancelledError:
            raise
        except Exception as e:          # голос — приятное дополнение, текст уже ушёл
            log.warning("озвучка не удалась: %s", e)

    # ── сообщения ──
    async def on_text(self, message: Any) -> None:
        await self._answer(message, message.text or "", via="text")

    @staticmethod
    def _voice_file(message: Any) -> tuple[Any, str, str] | None:
        if getattr(message, "voice", None):
            return message.voice, "voice.ogg", "audio/ogg"
        audio = getattr(message, "audio", None)
        if audio:
            return audio, getattr(audio, "file_name", None) or "audio.mp3", \
                getattr(audio, "mime_type", None) or "audio/mpeg"
        if getattr(message, "video_note", None):
            return message.video_note, "video.mp4", "video/mp4"
        return None

    async def on_voice(self, message: Any) -> None:
        """Голосовое / аудио / кружок → текст → агент."""
        out = self._out(message)
        found = self._voice_file(message)
        if found is None:
            return
        media, filename, mime = found
        if int(getattr(media, "file_size", 0) or 0) > MAX_DOWNLOAD:
            await out.send("Файл больше 20 МБ — такие Telegram ботам скачивать не даёт. Пришли короче.")
            return
        stt = self.d.stt
        if stt is None or not stt.available():
            reason = str(getattr(stt, "reason", "") or "").strip()
            specific = reason and "не настроено" not in reason      # например, «STT_PROVIDER=groq, но нет ключа»
            await out.send((reason[:1].upper() + reason[1:] + " " if specific else "") + STT_HOWTO)
            return
        if self.d.agent is None:
            await out.send(self._setup_text())
            return
        from ..services.stt import STTError

        async with self._typing(message):
            buf = io.BytesIO()
            await self._bot(message).download(media, destination=buf)
            try:
                text = await stt.transcribe(buf.getvalue(), filename, mime)
            except STTError as e:
                await out.send(f"Не расслышал: {e}")
                return
        text = " ".join(str(text or "").split())
        if not text:
            await out.send(NOT_HEARD)
            return
        if self.cfg.show_transcript:
            await self._send_transcript(out, text)
        await self._answer(message, text, via="voice")

    @staticmethod
    async def _send_transcript(out: Any, text: str) -> None:
        t = text if len(text) <= TRANSCRIPT_MAX else text[:TRANSCRIPT_MAX].rstrip() + "…"
        send_html = getattr(out, "send_html", None)
        if send_html is not None:
            await send_html(f"🎙 <i>{escape(t)}</i>")
        else:
            await out.send(f"🎙 {t}")

    async def on_other(self, message: Any) -> None:
        """Фото, файлы, стикеры: подпись — агенту (с пометкой, что вложения он не видит)."""
        caption = str(getattr(message, "caption", "") or "").strip()
        if caption and not caption.startswith("/"):
            await self._answer(message, f"[к сообщению приложен файл или картинка — ты их не видишь] {caption}")
            return
        await self._out(message).send(OTHER_MEDIA)

    async def on_unknown_command(self, message: Any) -> None:
        cmd = str(message.text or "").split()[0].split("@")[0] if message.text else "/?"
        await self._out(message).send(f"Не знаю команды {cmd}. Список — /help.")

    # ── команды ──
    async def cmd_start(self, message: Any) -> None:
        first = str(getattr(getattr(message, "from_user", None), "first_name", "") or "").strip()
        if first and not (self.cfg.owner_name or "").strip():
            await self.db.kv_set("owner_name", first)     # агент зовёт по имени, пока OWNER_NAME не задан
        name = (self.cfg.owner_name or first or "").strip()
        text = START_TEXT.format(name=f", {name}" if name else "", bot=self.cfg.bot_name)
        if self.d.agent is None:
            text += "\n\n" + self._setup_text()
        await self._out(message).send(text)

    async def cmd_help(self, message: Any) -> None:
        await self._out(message).send(HELP_TEXT)

    async def cmd_today(self, message: Any) -> None:
        from ..services import brief
        async with self._typing(message):
            text = await brief.morning_brief(self.ctx.child())
        await self._out(message).send(str(text or "").strip() or "Сводка пустая — на сегодня ничего не нашёл.")

    async def cmd_reminders(self, message: Any) -> None:
        from ..tools import reminders as rem
        rows = await rem.list_active(self.db, 50)
        out = self._out(message)
        if not rows:
            await out.send("Активных напоминаний нет. Скажи «напомни завтра в 9 позвонить маме» — поставлю.")
            return
        lines = ["⏰ Напоминания:"] + [rem.render_reminder(r, self.tz) for r in rows]
        if len(rows) > REMINDER_BUTTONS:
            lines.append(f"\nКнопки — для первых {REMINDER_BUTTONS}; остальные отменяй словами: «отмени #N».")
        btns = [(f"✖️ #{r['id']}", f"rem:del:{r['id']}") for r in rows[:REMINDER_BUTTONS]]
        await out.send("\n".join(lines), [btns[i:i + 4] for i in range(0, len(btns), 4)])

    async def cmd_calendar(self, message: Any) -> None:
        from ..tools import calendar as cal
        days = int_arg(command_args(message.text), 7, 1, 366)
        start = self._local_midnight()
        items = await cal.events_between(self.db, self.tz, start, start + timedelta(days=days))
        out = self._out(message)
        span = f"{days} {plural(days, 'день', 'дня', 'дней')}"
        if not items:
            await out.send(f"📅 На {span} вперёд пусто. Скажи «запиши встречу в пятницу в 15:00» — внесу.")
            return
        lines = [f"📅 Календарь на {span}:"]
        for it in items[:CALENDAR_MAX_ITEMS]:
            s = f"• {it['occurs_local']} — {it['title']}"
            if it.get("location"):
                s += f" ({it['location']})"
            if it.get("rrule"):
                s += " 🔁"
            lines.append(s)
        if len(items) > CALENDAR_MAX_ITEMS:
            lines.append(f"…и ещё {len(items) - CALENDAR_MAX_ITEMS}. Сузь окно: /calendar 3")
        await out.send("\n".join(lines))

    async def cmd_ics(self, message: Any) -> None:
        from ..tools import calendar as cal
        start = self._local_midnight()
        uniq: dict[int, dict] = {}
        for it in await cal.events_between(self.db, self.tz, start, start + timedelta(days=90)):
            uniq.setdefault(it["id"], it)
        for r in await self.db.fetchall("SELECT * FROM events WHERE status='active' AND rrule IS NOT NULL"):
            uniq.setdefault(r["id"], r)
        out = self._out(message)
        if not uniq:
            await out.send("Календарь пуст — на 90 дней вперёд ни одного события, файл не отправляю.")
            return
        n = len(uniq)
        data = cal.build_ics(list(uniq.values()), self.tz.key)
        await out.send_file(data, "calendar.ics",
                            f"Календарь: {n} {plural(n, 'событие', 'события', 'событий')} — открой файл, "
                            f"и он добавится в твой календарь")

    async def cmd_birthdays(self, message: Any) -> None:
        from ..tools import birthdays as bd
        rows = await bd.upcoming(self.db, self.tz, 365)
        out = self._out(message)
        if not rows:
            await out.send("Дней рождения не знаю. Скажи «у Маши др 14 марта» — запомню и напомню заранее.")
            return
        lines = ["🎂 Дни рождения:"]
        for b in rows[:60]:
            nd = date.fromisoformat(b["next_date"])
            left = int(b["days_left"])
            when = "сегодня" if left == 0 else "завтра" if left == 1 else f"через {left} {plural(left, 'день', 'дня', 'дней')}"
            who = b["name"] + (f" ({b['relation']})" if (b.get("relation") or "").strip() else "")
            s = f"• {bd.human_date(nd.month, nd.day)}, {timeutil.weekday_name(nd, full=False)} — {who} · {when}"
            if b.get("turns"):
                s += f" · исполнится {b['turns']}"
            lines.append(s)
        await out.send("\n".join(lines))

    async def cmd_ideas(self, message: Any) -> None:
        from ..tools import ideas
        rows = await ideas.recent_ideas(self.db, limit=15)
        out = self._out(message)
        if not rows:
            await out.send("Идей пока нет. Скажи «у меня идея: …» — оценю честно и сохраню.")
            return
        lines = ["💡 Последние идеи:"]
        for r in rows:
            score = f"{r['score']}/10" if r.get("score") is not None else "—"
            created = timeutil.from_iso(r.get("created_at"))
            day = created.astimezone(self.tz).strftime("%d.%m") if created else ""
            status = ideas.STATUS_RU.get(r.get("status"), r.get("status"))
            lines.append(f"#{r['id']} [{score}] {r['title']} · {status}" + (f" · {day}" if day else ""))
        lines.append("\n/idea N — открыть целиком")
        await out.send("\n".join(lines))

    async def cmd_idea(self, message: Any) -> None:
        from ..tools import ideas
        arg = command_args(message.text).lstrip("#№").strip()
        out = self._out(message)
        if not arg.isdigit():
            await out.send("Какую? Номер из /ideas: /idea 3")
            return
        row = await ideas.load_idea(self.db, int(arg))
        if row is None:
            await out.send(f"Идеи #{arg} нет. Список — /ideas.")
            return
        await out.send(ideas.idea_card(row, self.tz), ideas.deep_buttons(row["id"]))

    async def cmd_projects(self, message: Any) -> None:
        from ..tools import projects
        res = await projects.t_list_projects(self.ctx.child(), status="all")
        items = res.get("items") or []
        live = [p for p in items if p.get("status") in ("active", "paused")]
        closed = len(items) - len(live)
        out = self._out(message)
        if not live:
            tail = f" (закрытых: {closed})" if closed else ""
            await out.send(f"Открытых проектов нет{tail}. Скажи «заведи проект …» — заведу.")
            return
        lines = ["📋 Проекты:"]
        for p in live:
            s = f"#{p['id']} {p['name']}" + (" ⏸ на паузе" if p["status"] == "paused" else "")
            s += f" · открыто {p.get('open_tasks', 0)}, сделано {p.get('done_tasks', 0)}"
            if p.get("next_due"):
                s += f" · ближайший срок {p['next_due']}"
            idle = p.get("idle_days")
            if idle is not None and idle >= 7:
                s += f" · без движения {idle} {plural(idle, 'день', 'дня', 'дней')}"
            if (p.get("goal") or "").strip():
                s += f"\n    цель: {p['goal'].strip()}"
            lines.append(s)
        if closed:
            lines.append(f"\nЗакрытых: {closed}.")
        await out.send("\n".join(lines))

    async def cmd_news(self, message: Any) -> None:
        from ..tools import news as tnews
        topic = command_args(message.text)
        out = self._out(message)
        await out.send("Собираю новости" + (f" про «{topic}»" if topic else "") + "…")
        async with self._typing(message):
            text = await tnews.news_digest(self.ctx.child(), topic)
        await out.send("🗞 " + (str(text or "").strip() or "Пусто."))

    async def cmd_memory(self, message: Any) -> None:
        from ..tools import memory
        rows = await memory.all_facts(self.db, limit=200)
        out = self._out(message)
        if not rows:
            await out.send("Пока ничего о тебе не записал. Расскажи о себе — запомню.")
            return
        groups: dict[str, list[dict]] = {}
        for r in rows:
            groups.setdefault(r.get("category") or "general", []).append(r)
        lines = [f"🧠 Что я о тебе помню ({len(rows)}):"]
        order = list(FACT_CATEGORIES) + sorted(set(groups) - set(FACT_CATEGORIES))
        for cat in order:
            if cat not in groups:
                continue
            lines.append(f"\n**{FACT_CATEGORIES.get(cat, cat)}**")
            lines += [f"#{r['id']} {r['content']}" for r in groups[cat]]
        lines.append("\nЗабыть — кнопкой ниже (последние записи) или /forget N.")
        recent = sorted(rows, key=lambda r: r["id"], reverse=True)[:FACT_BUTTONS]
        btns = [(f"✖️ #{r['id']}", f"fact:del:{r['id']}") for r in recent]
        await out.send("\n".join(lines), [btns[i:i + 5] for i in range(0, len(btns), 5)])

    async def cmd_forget(self, message: Any) -> None:
        from ..tools import memory
        arg = command_args(message.text).lstrip("#№").strip()
        out = self._out(message)
        if not arg.isdigit():
            await out.send("Что забыть? Номер факта из /memory: /forget 12")
            return
        row = await memory.delete_fact(self.db, int(arg))
        if row is None:
            await out.send(f"Факта #{arg} нет. Список — /memory.")
            return
        await out.send(f"Забыл #{row['id']}: «{row['content']}».")

    async def cmd_opinions(self, message: Any) -> None:
        rows = await self.db.fetchall(
            "SELECT * FROM opinions ORDER BY confidence DESC, updated_at DESC, id DESC LIMIT 50")
        out = self._out(message)
        if not rows:
            await out.send("Своих позиций пока не записал. Спроси, что я думаю о чём-нибудь, — сформулирую.")
            return
        lines = ["🧭 Мои позиции:"]
        for r in rows:
            try:
                changes = len(json.loads(r.get("history") or "[]"))
            except (TypeError, ValueError):
                changes = 0
            ch = f"менял {changes} {plural(changes, 'раз', 'раза', 'раз')}" if changes else "не менял"
            lines.append(f"#{r['id']} **{r['topic']}** — {r['stance']} (уверенность {r['confidence']}%, {ch})")
        await out.send("\n".join(lines))

    async def cmd_journal(self, message: Any) -> None:
        rows = await self.db.fetchall("SELECT * FROM journal ORDER BY id DESC LIMIT 3")
        out = self._out(message)
        if not rows:
            await out.send("Дневник пуст — первая запись появится после ночной рефлексии.")
            return
        blocks = [f"📓 **{timeutil.fmt_local(r['created_at'], self.tz)}**\n{r['content']}" for r in rows]
        await out.send("\n\n".join(blocks))

    async def cmd_mode(self, message: Any) -> None:
        new = "fast" if await self._deep_mode() else "deep"
        await self.db.kv_set("mode", new)
        await self._out(message).send(MODE_TEXT[new])

    async def cmd_deep(self, message: Any) -> None:
        q = command_args(message.text)
        if not q:
            await self._out(message).send("Что обдумать? Пиши: /deep вопрос — отвечу глубоко, общий режим не трогаю.")
            return
        await self._answer(message, q, via="text", deep=True)

    async def cmd_voice(self, message: Any) -> None:
        new = TTS_NEXT[await self._tts_mode()]
        await self.db.kv_set("tts_mode", new)
        text = TTS_TEXT[new]
        tts = self.d.tts
        if new != "off" and (tts is None or not tts.available()):
            text += "\n(Но озвучка сейчас недоступна: не установлен edge-tts — pip install edge-tts.)"
        await self._out(message).send(text + "\n/voice — переключить дальше.")

    async def cmd_reset(self, message: Any) -> None:
        if self.d.agent is not None:
            n = await self.d.agent.reset_context()
        else:
            n = await self.db.execute("UPDATE messages SET summarized=1 WHERE summarized=0")
        await self._out(message).send(
            f"🧹 Начали с чистого листа: убрал из разговора {n} {plural(n, 'реплику', 'реплики', 'реплик')}. "
            f"Память, позиции и дневник на месте.")

    async def cmd_backup(self, message: Any) -> None:
        data_dir = Path(self.cfg.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp = data_dir / f".backup-{uuid.uuid4().hex}.db"
        out = self._out(message)
        keep = False
        try:
            await self.db.execute("VACUUM INTO ?", (str(tmp),))
            size = tmp.stat().st_size
            if size > BACKUP_MAX:
                keep = True
                await out.send(f"Бэкап вышел {size / 1048576:.0f} МБ — больше, чем Telegram пропускает (50 МБ). "
                               f"Оставил файл на сервере: `{tmp}`")
                return
            data = await asyncio.to_thread(tmp.read_bytes)
            name = f"oracle-backup-{timeutil.now_local(self.tz):%Y%m%d}.db"
            human = f"{size / 1048576:.1f} МБ" if size >= 1048576 else f"{max(1, size // 1024)} КБ"
            await out.send_file(data, name, f"💾 Резервная копия базы, {human}. Тут вся память, дневник и "
                                            f"переписка — храни в надёжном месте.")
        finally:
            if not keep:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    log.warning("не удалил временный бэкап %s", tmp)

    async def cmd_chats(self, message: Any) -> None:
        out = self._out(message)
        ub = self._userbot()
        if ub is None:
            await out.send(USERBOT_HOWTO)
            return
        async with self._typing(message):
            items = await ub.dialogs(limit=15, unread_only=True)
        if not items:
            await out.send("Непрочитанных нет — всё разобрано.")
            return
        lines = ["💬 Непрочитанные:"]
        btns: Buttons = []
        for c in items:
            last = _cut(c.get("last_text") or "", 120)
            lines.append(f"• {c['title']} ({c.get('unread', 0)})" + (f": {last}" if last else ""))
            btns.append([(f"✍️ Ответ: {_cut(c['title'], 28)}", f"draft:new:{c['id']}")])
        await out.send("\n".join(lines), btns)

    async def cmd_status(self, message: Any) -> None:
        from ..tools import reminders as rem
        cfg, d = self.cfg, self.d
        lines = [f"⚙️ **{cfg.bot_name}** — состояние"]
        probs = cfg.problems()
        lines.append("⚠️ Не настроено:\n" + "\n".join(f"• {p}" for p in probs) if probs else "✅ Ключи на месте")
        if d.agent is None:
            lines.append("Режим настройки: мозг не запущен.")
        lines.append(f"Модели: {cfg.llm_model} (быстрый) / {cfg.llm_model_deep} (глубокий)")
        lines.append(f"Режим: {'глубокий' if await self._deep_mode() else 'быстрый'}")
        tts_ok = d.tts is not None and d.tts.available()
        lines.append(f"Ответы голосом: {TTS_STATUS[await self._tts_mode()]}"
                     + ("" if tts_ok else " (озвучка недоступна: нет edge-tts)"))
        stt = d.stt
        if stt is None:
            lines.append("Распознавание голоса: нет")
        elif hasattr(stt, "describe"):
            lines.append(f"Распознавание голоса: {stt.describe()}")
        else:
            lines.append(f"Распознавание голоса: {'есть' if stt.available() else 'нет'}")
        if self._userbot() is not None:
            ub_state = "подключён"
        elif cfg.userbot_enabled:
            ub_state = "включён, но не подключён (python -m oracle.userbot_login)"
        else:
            ub_state = "выключен"
        lines.append(f"Userbot: {ub_state}")
        sched = d.scheduler
        lines.append("Планировщик: " + ("работает" if sched is not None and getattr(sched, "running", True)
                                         else "не запущен"))
        lines.append("Расписание: " + ", ".join(
            f"{what} {getattr(cfg, attr, '') or 'выкл.'}"
            for what, attr in (("сводка", "morning_brief_time"), ("дни рождения", "birthday_time"),
                               ("новости", "news_digest_time"), ("рефлексия", "reflection_time"))))
        usage = getattr(d.llm, "usage_total", None) or {}
        lines.append(f"Модель с запуска: вызовов — {usage.get('calls', 0)}, токенов на вход — "
                     f"{usage.get('prompt_tokens', 0)}, на выход — {usage.get('completion_tokens', 0)}")
        failed = getattr(d.agent, "failed_modules", None)
        if failed:
            lines.append("Не загрузились инструменты: " + ", ".join(map(str, failed)))
        facts = await self.db.scalar("SELECT COUNT(*) FROM facts") or 0
        n_ideas = await self.db.scalar("SELECT COUNT(*) FROM ideas") or 0
        active = await self.db.scalar("SELECT COUNT(*) FROM reminders WHERE status='active'") or 0
        lines.append(f"Память: фактов — {facts}, идей — {n_ideas}, активных напоминаний — {active}")
        lines.append(f"Часовой пояс: {cfg.timezone}, сейчас {timeutil.fmt_local(timeutil.now_utc(), self.tz)}")
        nxt = await rem.list_active(self.db, 3)
        if nxt:
            lines.append("\nБлижайшие напоминания:")
            lines += [rem.render_reminder(r, self.tz) for r in nxt]
        await self._out(message).send("\n".join(lines))

    # ── кнопки ──
    async def on_callback(self, query: Any) -> None:
        cb = _Cb(query)
        try:
            parsed = parse_cb(getattr(query, "data", None))
            route = self.routes.get((parsed[0], parsed[1])) if parsed else None
            if route is None:
                await cb.answer(STALE)
                return
            await route(query, cb, parsed[2])
        except asyncio.CancelledError:
            raise
        except ValueError as e:          # ожидаемое: «черновик уже отправлен», «userbot не подключён»…
            if cb.answered:
                await self._out(query).send(f"⚠️ {err_text(e)}")
            else:
                await cb.answer(err_text(e), alert=True)
        except Exception as e:
            log.exception("кнопка %r упала", getattr(query, "data", None))
            await cb.answer("Ошибка")
            await self._report(query, e)
        finally:
            await cb.answer()

    async def _set_kb(self, query: Any, markup: Any) -> None:
        msg = getattr(query, "message", None)
        chat = getattr(getattr(msg, "chat", None), "id", None)
        mid = getattr(msg, "message_id", None)
        bot = self._bot(query)
        if chat is None or mid is None or bot is None:
            return
        try:
            await bot.edit_message_reply_markup(chat_id=chat, message_id=mid, reply_markup=markup)
        except Exception as e:   # сообщение старое/удалено/не изменилось — не страшно
            log.debug("edit_message_reply_markup: %r", e)

    async def _clear_kb(self, query: Any) -> None:
        await self._set_kb(query, None)

    async def _drop_button(self, query: Any, data: str) -> None:
        markup = getattr(getattr(query, "message", None), "reply_markup", None)
        await self._set_kb(query, keyboards.without(markup, data) if markup is not None else None)

    async def _edit_text(self, query: Any, text: str, buttons: Buttons | None) -> bool:
        msg = getattr(query, "message", None)
        chat = getattr(getattr(msg, "chat", None), "id", None)
        mid = getattr(msg, "message_id", None)
        bot = self._bot(query)
        html = md_to_html(text)
        if chat is None or mid is None or bot is None or len(html) > 4096 or not html.strip():
            return False
        try:
            await bot.edit_message_text(text=html, chat_id=chat, message_id=mid, parse_mode="HTML",
                                        reply_markup=keyboards.build(buttons), link_preview_options=NO_PREVIEW)
            return True
        except Exception as e:
            log.debug("edit_message_text: %r", e)
            return False

    async def _send_challenge(self, query: Any, rid: int, prefix: str = "") -> None:
        question, answer, options = make_challenge()
        await self.db.kv_set(challenge_key(rid), answer)
        await self._out(query).send(f"{prefix}🧮 Докажи, что проснулся: {question}",
                                    challenge_buttons(rid, options))

    async def cb_rem_done(self, query: Any, cb: _Cb, args: list[int]) -> None:
        from ..tools import reminders as rem
        rid = args[0]
        row = await rem.get_reminder(self.db, rid)
        if row is None or row.get("status") == "cancelled":
            await self._clear_kb(query)
            await cb.answer("Напоминания уже нет")
            return
        if row.get("challenge") and row.get("nag_active"):
            await self._send_challenge(query, rid)
            await cb.answer("Сначала реши задачку")
            return
        await rem.ack_reminder(self.db, rid)
        await self._clear_kb(query)
        await cb.answer("Отмечено")

    async def cb_rem_snooze(self, query: Any, cb: _Cb, args: list[int]) -> None:
        from ..tools import reminders as rem
        rid, minutes = args
        row = await rem.get_reminder(self.db, rid)
        if row is None or row.get("status") == "cancelled":
            await self._clear_kb(query)
            await cb.answer("Напоминания уже нет")
            return
        row = await rem.snooze_reminder(self.db, rid, minutes)
        await self._clear_kb(query)
        at = timeutil.from_iso((row or {}).get("snooze_at"))
        if at is None:
            await cb.answer("Отложил")
            return
        local = at.astimezone(self.tz)
        same_day = local.date() == timeutil.now_local(self.tz).date()
        await cb.answer(f"💤 Напомню в {local:%H:%M}" if same_day else f"💤 Напомню {local:%d.%m в %H:%M}")

    async def cb_rem_delete(self, query: Any, cb: _Cb, args: list[int]) -> None:
        from ..tools import reminders as rem
        rid = args[0]
        ok = await rem.cancel_reminder(self.db, rid)
        await self._drop_button(query, f"rem:del:{rid}")
        await cb.answer(f"Отменил #{rid}" if ok else "Уже отменено")

    async def cb_wake_answer(self, query: Any, cb: _Cb, args: list[int]) -> None:
        from ..tools import reminders as rem
        rid, n = args
        key = challenge_key(rid)
        correct = await self.db.kv_get(key)
        row = await rem.get_reminder(self.db, rid)
        await self._clear_kb(query)
        if correct is None:
            if row and row.get("status") == "active" and row.get("challenge") and row.get("nag_active"):
                await self._send_challenge(query, rid)
                await cb.answer("Задачка устарела — вот новая")
            else:
                await cb.answer("Уже не актуально")
            return
        try:
            ok = int(n) == int(correct)
        except (TypeError, ValueError):
            ok = False
        if not ok:
            await self._send_challenge(query, rid, prefix="❌ Мимо. ")
            await cb.answer("Мимо")
            return
        await self.db.kv_delete(key)
        if row is not None:
            await rem.ack_reminder(self.db, rid)
            try:
                await self.db.add_message("event", f"Владелец решил задачку будильника #{rid} «{row['text']}» — "
                                                   f"проснулся", "system")
            except Exception:
                log.debug("не записал событие про будильник", exc_info=True)
        await self._out(query).send("✅ Проснулся. Доброе утро.")
        await cb.answer("Верно")

    async def cb_bday_regen(self, query: Any, cb: _Cb, args: list[int]) -> None:
        from ..tools import birthdays as bd
        if await bd.get_birthday(self.db, args[0]) is None:
            await cb.answer("Этого дня рождения уже нет")
            return
        await cb.answer("Пишу другой вариант…")
        ctx = self.ctx.child()
        async with self._typing(query):
            b, text = await bd.regenerate_greeting(ctx, args[0])
        await self._out(query).send(text, bd.greeting_buttons(ctx, b))

    async def cb_bday_send(self, query: Any, cb: _Cb, args: list[int]) -> None:
        from ..tools import birthdays as bd
        bid = args[0]
        b = await bd.get_birthday(self.db, bid)
        if b is None:
            await cb.answer("Этого дня рождения уже нет")
            return
        ub = self._userbot()
        if ub is None:
            await cb.answer("Userbot не подключён — отправить не могу. Перешли поздравление сам.", alert=True)
            return
        tg = str(b.get("tg_username") or "").strip().lstrip("@")
        if not tg:
            await cb.answer(f"У «{b['name']}» не записан username в Telegram.", alert=True)
            return
        text = await self.db.kv_get(bd.greeting_key(bid))
        if not isinstance(text, str) or not text.strip():
            await cb.answer("Текста поздравления нет — нажми «Другой вариант».", alert=True)
            return
        sent_key = f"bday_sent:{bid}"
        if await self.db.kv_get(sent_key) == text:
            await cb.answer("Это поздравление уже отправлено")
            return
        if bid in self._sending:
            await cb.answer("Уже отправляю…")
            return
        self._sending.add(bid)
        try:
            await cb.answer("Отправляю…")
            chat_id, title = await ub.resolve("@" + tg)
            await ub.send(chat_id, text)
            await self.db.kv_set(sent_key, text)
        finally:
            self._sending.discard(bid)
        await self._drop_button(query, f"bday:send:{bid}")
        await self._out(query).send(f"📨 Отправил поздравление: {title} (@{tg}).")
        try:
            await self.db.add_message("event", f"Владелец отправил через userbot поздравление с днём рождения "
                                               f"({b['name']}): {text}", "system")
        except Exception:
            log.debug("не записал событие про поздравление", exc_info=True)

    async def cb_idea_deep(self, query: Any, cb: _Cb, args: list[int]) -> None:
        from ..tools import ideas  # noqa: F401  (регистрирует deep_think_idea)
        from ..tools.base import dispatch
        res = json.loads(await dispatch("deep_think_idea", {"id": args[0]}, self.ctx.child()))
        if not res.get("ok"):
            await cb.answer(str(res.get("error") or "не вышло"), alert=True)
        elif res.get("started") is False:
            await cb.answer("Уже думаю над ней — пришлю")
        else:
            await cb.answer("Думаю, пришлю")

    async def cb_draft_new(self, query: Any, cb: _Cb, args: list[int]) -> None:
        from ..tools import tg_chats
        await cb.answer("Пишу черновик…")
        ctx = self.ctx.child()
        async with self._typing(query):
            await tg_chats.make_draft(ctx, args[0])
        await self._deliver(self._out(query), ctx.outbox)

    async def cb_draft_send(self, query: Any, cb: _Cb, args: list[int]) -> None:
        from ..tools import tg_chats
        text = await tg_chats.send_draft(self.ctx.child(), args[0])
        await self._clear_kb(query)
        await cb.answer("Отправлено")
        await self._out(query).send(text)

    async def cb_draft_regen(self, query: Any, cb: _Cb, args: list[int]) -> None:
        from ..tools import tg_chats
        await cb.answer("Переписываю…")
        ctx = self.ctx.child()
        async with self._typing(query):
            res = await tg_chats.regen_draft(ctx, args[0])
        if not await self._edit_text(query, res.get("text") or "", res.get("buttons")):
            await self._clear_kb(query)
            await self._deliver(self._out(query), ctx.outbox)

    async def cb_draft_drop(self, query: Any, cb: _Cb, args: list[int]) -> None:
        from ..tools import tg_chats
        ok = await tg_chats.drop_draft(self.db, args[0])
        await self._clear_kb(query)
        await cb.answer("Ок, не отправляю" if ok else "Черновик уже не актуален")

    async def cb_fact_delete(self, query: Any, cb: _Cb, args: list[int]) -> None:
        from ..tools import memory
        row = await memory.delete_fact(self.db, args[0])
        await self._drop_button(query, f"fact:del:{args[0]}")
        await cb.answer(f"Забыл #{args[0]}" if row else "Уже забыто")


# ── роутер ───────────────────────────────────────────────────────────────────
def build_router(deps: Deps) -> Router:
    """Роутер aiogram со всеми обработчиками. Порядок важен: команды → голос → неизвестная
    команда → текст → всё прочее."""
    h = Handlers(deps)
    router = Router(name="oracle")
    for name, fn in h.commands().items():
        router.message.register(h.wrap(fn), Command(name))
    router.message.register(h.wrap(h.on_voice), F.voice | F.audio | F.video_note)
    router.message.register(h.wrap(h.on_unknown_command), F.text.startswith("/"))
    router.message.register(h.wrap(h.on_text), F.text)
    router.message.register(h.wrap(h.on_other), F.photo | F.document | F.video | F.sticker | F.animation
                            | F.location | F.contact)

    async def on_callback(callback_query: Any) -> None:
        await h.on_callback(callback_query)

    router.callback_query.register(on_callback)
    router.oracle_handlers = h           # type: ignore[attr-defined]  — доступ из тестов/приложения
    return router
