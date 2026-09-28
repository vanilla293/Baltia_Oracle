"""Мозг: разговор с моделью, цикл инструментов, память диалога, инициатива и ночная рефлексия.

`Agent.handle` — одно сообщение владельца: системный промпт из характера и живого состояния
(память, позиции, дневник, конспект, долбящие будильники) + последние реплики → модель ↔ инструменты
(до LLM_MAX_STEPS шагов) → ответ. Что бот сделал инструментами, пишется в диалог событием
«[действия бота] create_reminder → ok #12 …» — следующий ход знает, что «его» — это напоминание #12.

`proactive` — бот пишет первым (follow-up, событие); `summarize_old` — сворачивает старые реплики
в конспект; `reset_context` — чистит краткосрочный контекст; `reflect` — ночная рефлексия:
дневник, новые факты, позиции, follow-up'ы.

Ходы `handle` и `proactive` идут строго по одному (блокировка): второй видит ответ первого.
Инструмент не должен синхронно звать агента — это взаимная блокировка; фоновая работа — через spawn.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Awaitable, Callable

from . import persona, timeutil
from .llm import LLMError
from .tools import base as tbase
from .tools.base import OutItem, ToolContext

log = logging.getLogger("oracle.agent")

EMPTY_TEXT = "Пусто. Скажи ещё раз?"
EVENT_PREFIX = "[событие, не реплика владельца] "
ACTIONS_PREFIX = "[действия бота] "
ACTIONS_MAX = 800            # длина записи «[действия бота] …»
SUMMARY_COUNT = 3            # сколько последних конспектов идёт в промпт
SUMMARY_MAX = 3000           # и сколько знаков они занимают вместе
HISTORY_ITEM_MAX = 6000      # одна реплика истории в контексте
VOICE_NOTE = "Сообщение пришло голосом (расшифровка может быть неточной)."
PROACTIVE_NOTE = "ВНУТРЕННИЙ ТРИГГЕР: владелец ничего не писал, ты пишешь первым."
PROACTIVE_TAIL = ("Напиши ему первым: коротко, по делу, в своём стиле. "
                  "Если повод потерял смысл — так и скажи одной фразой.")
SUMMARY_SYSTEM = ("Сверни кусок диалога в конспект для своей памяти: факты, решения, договорённости, "
                  "открытые вопросы, настроение владельца. 5–12 пунктов, по-русски, без воды.")
SUMMARY_ITEM_MAX = 1500      # одна реплика в тексте для конспекта
REFLECT_ITEM_MAX = 500       # одна реплика в материале рефлексии
REFLECT_MESSAGES = 150
REFLECT_FACTS, REFLECT_OPINIONS, REFLECT_IDEAS = 80, 30, 20
REFLECT_MAX_FACTS, REFLECT_MAX_OPINIONS, REFLECT_MAX_FOLLOWUPS = 8, 3, 2
REFLECT_WHY_DEFAULT = "пересмотрел сам на ночной рефлексии"

REFLECT_SYSTEM = """\
Ты — {name}, личный ИИ-напарник своего владельца{owner}. Сейчас ночная рефлексия: он спит, ты один перечитываешь прошедший день — \
разговор, свою память, прошлую запись в дневнике, его проекты, задачи и идеи — и думаешь сам. \
Дневник пишешь от первого лица, для себя: честно, конкретно, без дипломатии и без лести. \
Завтра эта запись ляжет тебе в контекст — пиши то, что реально пригодится.

Верни JSON-объект ровно такого вида:
{"journal": "…", "facts": [{"content": "…", "category": "…"}], \
"opinions": [{"topic": "…", "stance": "…", "reasons": "…", "confidence": 70, "opinion_id": 3, "why_changed": "…"}], \
"followups": [{"when": "YYYY-MM-DD HH:MM", "about": "…"}]}

journal — до 1200 знаков: что заметил за день; где он буксует и что откладывает; что его тревожит или радует; \
на что завтра надавить и о чём спросить; что ты сам думаешь о происходящем. С именами, цифрами и сроками, без общих слов.
facts — только новые устойчивые факты о нём (люди, планы, предпочтения, работа, здоровье), которых ещё нет \
в памяти ниже; не больше 8; одна мысль — один факт, в третьем лице. category — одна из: general, person, \
preference, plan, work, health, other.
opinions — только если ты реально сформировал позицию или изменил прежнюю; не больше 3. Меняешь \
существующую — укажи её opinion_id и why_changed: что именно тебя переубедило. confidence — 0–100.
followups — не больше 2: к чему ты сам вернёшься и напишешь ему первым. when — локальное время строго \
позже текущего момента, в разумное дневное время; about — заметка себе: о чём спросить и зачем. \
Уже стоящие follow-up'ы не дублируй.
Пустые списки — нормально. Ничего не выдумывай: только то, что есть в материалах."""


@dataclass
class AgentReply:
    text: str
    outbox: list[OutItem] = field(default_factory=list)
    deep: bool = False
    error: str | None = None


@dataclass
class Action:
    """Один вызов инструмента за ход: для записи «[действия бота] …»."""
    name: str
    ok: bool
    line: str


# ── мелочи ───────────────────────────────────────────────────────────────────
def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: max(0, n - 1)].rstrip() + "…"


def _one_line(v: Any, n: int) -> str:
    return _clip(" ".join(str(v if v is not None else "").split()), n)


def _module(name: str) -> Any:
    """Модуль инструментов по имени (лениво) или None, если его нет/он сломан."""
    try:
        return importlib.import_module(f"{tbase.__package__}.{name}")
    except Exception as e:
        log.warning("модуль oracle.tools.%s недоступен: %s", name, e)
        return None


async def _safe(fn: Callable[[], Awaitable[Any]], default: Any, what: str) -> Any:
    """Источник контекста, который не имеет права уронить ответ."""
    try:
        return await fn()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("контекст «%s» недоступен — отвечаю без него", what)
        return default


def load_tools() -> list[str]:
    """Импортировать все модули инструментов (они регистрируются сами). Сломанный или отсутствующий
    модуль не валит бота — его инструментов просто не будет. → имена модулей, которые не загрузились."""
    from . import tools as pkg
    failed = []
    for m in pkg.MODULES:
        try:
            importlib.import_module(f"{pkg.__name__}.{m}")
        except Exception as e:
            log.warning("инструменты %s не загружены: %s: %s", m, type(e).__name__, e)
            failed.append(m)
    return failed


def merge_history(items: list[tuple[str, str]]) -> list[dict]:
    """(роль, текст) → сообщения для модели: подряд идущие одной роли склеены через пустую строку,
    ведущие реплики ассистента отброшены (первой после system должна быть user)."""
    out: list[dict] = []
    for role, content in items:
        if not content:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + content
        else:
            out.append({"role": role, "content": content})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


_LABEL_KEYS = ("title", "text", "name", "content", "about", "topic", "chat")


def describe_action(name: str, result: str) -> Action:
    """Результат инструмента (JSON-строка от dispatch) → «name → ok #12 «текст» пн 29.09 07:30»
    или «name → ошибка: …»."""
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict) and data.get("ok") is False:
        return Action(name, False, f"{name} → ошибка: {_one_line(data.get('error') or 'без текста', 160)}")
    line = f"{name} → ok"
    if isinstance(data, dict):
        rid = data.get("id", data.get("draft_id"))
        if isinstance(rid, (int, str)) and not isinstance(rid, bool) and str(rid).strip():
            line += f" #{_one_line(rid, 20)}"
        for k in _LABEL_KEYS:
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                line += f" «{_one_line(v, 48)}»"
                break
        when = data.get("when")
        if isinstance(when, str) and when.strip():
            line += f" {_one_line(when, 40)}"
    return Action(name, True, line)


def actions_text(actions: list[Action]) -> str:
    """Запись для диалога: «[действия бота] a → ok #1; b → ошибка: …» (не длиннее ACTIONS_MAX)."""
    return _clip(ACTIONS_PREFIX + "; ".join(a.line for a in actions), ACTIONS_MAX)


def _proactive_prompt(trigger: str) -> str:
    sep = " " if trigger[-1:] in ".!?…" else ". "
    return f"[внутренний триггер] {trigger}{sep}{PROACTIVE_TAIL}"


def _fmt_when(value: Any, tz) -> str:
    """UTC ISO из базы → 'пн 28.09 09:00'; кривое/пустое → ""."""
    try:
        return timeutil.fmt_local(value, tz) if value else ""
    except (TypeError, ValueError, OverflowError):
        return ""


def _items(v: Any) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return [v]
    return []


def _as_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return "\n".join(str(x).strip() for x in v if str(x).strip())
    return str(v).strip()


class Agent:
    """Мозг бота. Один на процесс; регистрирует себя в `ctx.services.agent`."""

    def __init__(self, ctx: ToolContext):
        self.ctx = ctx
        if ctx.services is not None:
            ctx.services.agent = self
        self._lock = asyncio.Lock()           # ходы диалога — строго по одному
        self._sum_lock = asyncio.Lock()       # конспектирование — не параллельно само с собой
        self._reflect_lock = asyncio.Lock()
        self._sum_task: asyncio.Task | None = None
        self.failed_modules = load_tools()

    @property
    def cfg(self):
        return self.ctx.cfg

    @property
    def db(self):
        return self.ctx.db

    @property
    def llm(self):
        return self.ctx.llm

    # ── сообщение владельца ──────────────────────────────────────────────────
    async def handle(self, text: str, *, via: str = "text", deep: bool | None = None) -> AgentReply:
        """Ответить на сообщение владельца. Никогда не бросает (кроме отмены): ошибка → ⚠️-ответ."""
        text = str(text or "").strip()
        if not text:
            return AgentReply(text=EMPTY_TEXT)
        async with self._lock:
            turn = self.ctx.child()
            actions: list[Action] = []
            deep_v = bool(deep) if deep is not None else False
            try:
                if deep is None:
                    deep_v = await self._deep_mode()
                await self.db.add_message("user", text, via if via in ("text", "voice") else "text")
                system = await self._system(text, deep=deep_v, extra=VOICE_NOTE if via == "voice" else "")
                msgs = await self._history(system, fallback=text)
                final = await self._run(msgs, deep=deep_v, turn=turn, actions=actions)
                await self._log_actions(actions)
                await self.db.add_message("assistant", final, "text")
            except LLMError as e:
                log.warning("модель не ответила: %s", e)
                await self._log_actions(actions)
                return AgentReply(text=f"⚠️ {e}", outbox=turn.outbox, deep=deep_v, error=str(e))
            except Exception as e:
                log.exception("agent.handle упал")
                await self._log_actions(actions)
                return AgentReply(text=f"⚠️ Что-то сломалось внутри: {type(e).__name__}. Детали в логе.",
                                  outbox=turn.outbox, deep=deep_v, error=f"{type(e).__name__}: {e}")
            await self._maybe_summarize()
        return AgentReply(text=final, outbox=turn.outbox, deep=deep_v)

    async def _deep_mode(self) -> bool:
        try:
            return str(await self.db.kv_get("mode", "fast") or "").strip().lower() == "deep"
        except Exception:
            log.exception("не прочитал режим из kv — считаю быстрым")
            return False

    # ── бот пишет первым ─────────────────────────────────────────────────────
    async def proactive(self, trigger: str) -> str:
        """Бот пишет первым (follow-up, событие) → текст для отправки. Вложения из outbox инструментов
        (файлы, кнопки) уходят через notifier сразу; сам текст отправляет вызывающий (scheduler).
        LLMError пробрасывается — scheduler решает, что делать."""
        trig = " ".join(str(trigger or "").split()) or "повод не указан"
        async with self._lock:
            turn = self.ctx.child()
            actions: list[Action] = []
            try:
                system = await self._system(trig, deep=False, extra=PROACTIVE_NOTE)
                msgs = await self._history(system, tail=_proactive_prompt(trig))
                final = await self._run(msgs, deep=False, turn=turn, actions=actions)
            finally:
                await self._log_actions(actions)
            await self._deliver(turn.outbox)
            await self.db.add_message("assistant", final, "system")
        return final

    async def _deliver(self, outbox: list[OutItem]) -> None:
        notifier = getattr(self.ctx.services, "notifier", None)
        if notifier is None or not outbox:
            return
        for item in outbox:
            try:
                if item.kind == "file" and item.data is not None:
                    await notifier.send_file(item.data, item.filename or "file.bin", item.text)
                elif item.kind == "voice" and item.data is not None:
                    await notifier.send_voice(item.data, item.filename or "voice.mp3", item.text)
                elif item.text:
                    await notifier.send(item.text, item.buttons)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("не отправил вложение (%s)", item.kind)

    # ── системный промпт и история ───────────────────────────────────────────
    async def _owner_name(self) -> str:
        if self.cfg.owner_name:
            return self.cfg.owner_name
        name = await _safe(lambda: self.db.kv_get("owner_name", ""), "", "имя владельца")
        return str(name or "").strip()

    async def _summary_text(self) -> str:
        """Последние конспекты по порядку, вместе не длиннее SUMMARY_MAX (приоритет — свежим)."""
        rows = await self.db.fetchall(
            "SELECT content FROM summaries ORDER BY id DESC LIMIT ?", (SUMMARY_COUNT,))
        parts: list[str] = []
        total = 0
        for r in rows:                       # от свежего к старому
            c = str(r.get("content") or "").strip()
            if not c:
                continue
            if total + len(c) > SUMMARY_MAX:
                if not parts:                # самый свежий сам не влез — берём его начало
                    parts.append(_clip(c, SUMMARY_MAX))
                break
            parts.append(c)
            total += len(c) + 2
        return "\n\n".join(reversed(parts))

    async def _system(self, query: str, *, deep: bool, extra: str = "") -> str:
        """Системный промпт. Каждый источник живого состояния — отдельно: сломанный не мешает ответу."""
        db = self.db
        mem = _module("memory")
        facts, opinions, journal = [], [], None
        if mem is not None:
            facts = await _safe(lambda: mem.facts_for_prompt(db, query), [], "факты")
            opinions = await _safe(lambda: mem.opinions_for_prompt(db, query), [], "позиции")
            journal = await _safe(lambda: mem.latest_journal(db), None, "дневник")
        summary = await _safe(self._summary_text, "", "конспект")
        nags = await _safe(lambda: db.fetchall(
            "SELECT id, text, challenge FROM reminders WHERE nag_active=1 AND status='active' ORDER BY id"),
            [], "долбящие напоминания")
        return persona.build_system(
            name=self.cfg.bot_name, owner_name=await self._owner_name(),
            now=timeutil.fmt_now_for_prompt(self.cfg.tz),
            facts=facts or [], opinions=opinions or [], journal=journal, summary=summary or "",
            nags=nags or [], deep=deep, extra=extra)

    async def _history(self, system: str, *, tail: str | None = None, fallback: str = "") -> list[dict]:
        """[system, …последние реплики…(, tail)]. События — от роли user с пометкой, что это не он."""
        rows = await _safe(lambda: self.db.recent_messages(self.cfg.history_messages), [], "история")
        items: list[tuple[str, str]] = []
        for r in rows:
            content = _clip(str(r.get("content") or "").strip(), HISTORY_ITEM_MAX)
            if not content:
                continue
            role = r.get("role")
            if role in ("user", "assistant"):
                items.append((role, content))
            else:                            # event и всё незнакомое
                items.append(("user", EVENT_PREFIX + content))
        if tail:
            items.append(("user", tail))
        msgs = merge_history(items)
        if not msgs and fallback:
            msgs = [{"role": "user", "content": fallback}]
        return [{"role": "system", "content": system}, *msgs]

    # ── цикл «модель → инструменты» ──────────────────────────────────────────
    async def _run(self, msgs: list[dict], *, deep: bool, turn: ToolContext, actions: list[Action]) -> str:
        """Модель ↔ инструменты до LLM_MAX_STEPS шагов → итоговый текст. `msgs` дополняется на месте,
        `actions` — вызовами инструментов (их видно вызывающему даже при ошибке модели)."""
        schemas = tbase.schemas(self.cfg) or None
        final: str | None = None
        for _ in range(max(1, int(self.cfg.llm_max_steps))):
            resp = await self.llm.complete(msgs, tools=schemas, deep=deep)
            if not resp.tool_calls:
                final = resp.content or ""
                break
            msgs.append(resp.to_message())
            for call in resp.tool_calls:
                result = await self._call_tool(call.name, call.arguments, turn)
                msgs.append({"role": "tool", "tool_call_id": call.id, "content": result})
                actions.append(describe_action(call.name, result))
        if final is None:                    # шаги кончились — пусть подведёт итог без инструментов
            resp = await self.llm.complete(msgs, tools=schemas, tool_choice="none", deep=deep)
            final = resp.content or ""
        final = final.strip()
        if not final:
            final = "Готово." if any(a.ok for a in actions) else "…"
        return final

    @staticmethod
    async def _call_tool(name: str, arguments: Any, turn: ToolContext) -> str:
        try:
            return await tbase.dispatch(name, arguments, turn)
        except asyncio.CancelledError:
            raise
        except Exception as e:               # dispatch и так ловит почти всё; это — последняя страховка
            log.exception("dispatch %s упал", name)
            return json.dumps({"ok": False, "error": f"внутренняя ошибка: {type(e).__name__}: {e}"},
                              ensure_ascii=False)

    async def _log_actions(self, actions: list[Action]) -> None:
        """Записать в диалог, что сделано инструментами (до ответа ассистента). Повторно — не пишет."""
        if not actions:
            return
        text = actions_text(actions)
        actions.clear()
        try:
            await self.db.add_message("event", text, "system")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("не записал журнал действий: %s", text)

    # ── конспект старого диалога ─────────────────────────────────────────────
    async def _unsummarized(self) -> int:
        return int(await self.db.scalar("SELECT COUNT(*) FROM messages WHERE summarized=0") or 0)

    async def _maybe_summarize(self) -> None:
        try:
            if self._sum_task is not None and not self._sum_task.done():
                return
            if await self._unsummarized() > self.cfg.history_messages + self.cfg.summary_chunk:
                self._sum_task = self.ctx.services.spawn(self.summarize_old(), name="summarize")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("не запустил конспектирование")

    def _transcript(self, rows: list[dict], *, me: str, limit: int) -> str:
        who = {"user": "Владелец", "assistant": me}
        lines = []
        for r in rows:
            content = _clip(" ".join(str(r.get("content") or "").split()), limit)
            if not content:
                continue
            when = _fmt_when(r.get("created_at"), self.cfg.tz)
            name = who.get(r.get("role"), "Событие")
            lines.append(f"[{when}] {name}: {content}" if when else f"{name}: {content}")
        return "\n".join(lines)

    async def summarize_old(self) -> bool:
        """Свернуть самые старые SUMMARY_CHUNK реплик в конспект, если за окном истории их набралось
        больше, чем на один кусок. → True, если свернул."""
        cfg, db = self.cfg, self.db
        async with self._sum_lock:
            if await self._unsummarized() <= cfg.history_messages + cfg.summary_chunk:
                return False
            rows = await db.fetchall(
                "SELECT id, role, content, created_at FROM messages WHERE summarized=0 ORDER BY id LIMIT ?",
                (int(cfg.summary_chunk),))
            if not rows:
                return False
            transcript = self._transcript(rows, me="Ты", limit=SUMMARY_ITEM_MAX)
            try:
                text = await self.llm.ask(SUMMARY_SYSTEM, transcript, deep=False)
            except LLMError as e:
                log.warning("конспект не получился: %s", e)
                return False
            text = str(text or "").strip()
            if not text:
                log.warning("модель вернула пустой конспект — попробую в другой раз")
                return False
            first, last = rows[0], rows[-1]
            period = f"[{_fmt_when(first['created_at'], cfg.tz)} — {_fmt_when(last['created_at'], cfg.tz)}]"
            async with db.transaction() as c:
                await c.execute(
                    "INSERT INTO summaries(content, from_id, to_id, created_at) VALUES(?,?,?,?)",
                    (f"{period}\n{text}", first["id"], last["id"], timeutil.iso(timeutil.now_utc())))
                await c.execute(
                    "UPDATE messages SET summarized=1 WHERE summarized=0 AND id BETWEEN ? AND ?",
                    (first["id"], last["id"]))
            log.info("свернул реплики #%s–#%s в конспект", first["id"], last["id"])
            return True

    async def reset_context(self) -> int:
        """Забыть краткосрочный контекст (/reset): все несвёрнутые реплики помечаются свёрнутыми.
        Долговременная память (факты, позиции, дневник, конспекты) не трогается. → сколько реплик."""
        return await self.db.execute("UPDATE messages SET summarized=1 WHERE summarized=0")

    # ── ночная рефлексия ─────────────────────────────────────────────────────
    async def reflect(self) -> str | None:
        """Ночная рефлексия (deep): дневник, новые факты, позиции, follow-up'ы. → текст записи дневника
        или None (за сутки он ничего не писал, модель не ответила или не дала дневника)."""
        async with self._reflect_lock:
            db, cfg = self.db, self.cfg
            since = timeutil.iso(timeutil.now_utc() - timedelta(hours=24))
            n = await db.scalar("SELECT COUNT(*) FROM messages WHERE role='user' AND created_at >= ?", (since,))
            if not n:
                log.info("рефлексия: за сутки ни одного его сообщения — пропускаю")
                return None
            user = await self._reflect_material(since)
            owner = await self._owner_name()
            system = (REFLECT_SYSTEM.replace("{name}", cfg.bot_name)
                      .replace("{owner}", f" ({owner})" if owner else ""))
            try:
                data = await self.llm.ask_json(system, user, deep=True)
            except LLMError as e:
                log.warning("рефлексия: модель не ответила: %s", e)
                return None
            except ValueError as e:
                log.warning("рефлексия: ответ не JSON: %s", e)
                return None
            if not isinstance(data, dict):
                log.warning("рефлексия: ответ не объект: %r", str(data)[:200])
                return None
            return await self._persist_reflection(data)

    async def _reflect_material(self, since: str) -> str:
        """Всё, что бот перечитывает ночью, одним текстом. Каждый источник — отдельно и без падений."""
        db, tz = self.db, self.cfg.tz
        mem = _module("memory")
        parts = [f"СЕЙЧАС: {timeutil.fmt_now_for_prompt(tz)}"]

        rows = await _safe(lambda: db.fetchall(
            "SELECT role, content, created_at FROM messages WHERE created_at >= ? ORDER BY id DESC LIMIT ?",
            (since, REFLECT_MESSAGES)), [], "разговор за сутки")
        parts.append("РАЗГОВОР ЗА СУТКИ:\n" + (self._transcript(list(reversed(rows)), me="Я",
                                                                limit=REFLECT_ITEM_MAX) or "—"))

        facts = await _safe(lambda: mem.facts_for_prompt(db, "", REFLECT_FACTS), [], "факты") if mem else []
        parts.append("ЧТО Я О НЁМ УЖЕ ЗНАЮ:\n" + ("\n".join(
            f"- #{f['id']} [{f.get('category') or 'general'}] {f['content']}" for f in facts) or "— ничего"))

        opinions = await _safe(lambda: mem.opinions_for_prompt(db, "", REFLECT_OPINIONS), [], "позиции") \
            if mem else []
        if opinions:
            parts.append("МОИ ПОЗИЦИИ:\n" + "\n".join(
                f"- #{o['id']} {o['topic']}: {o['stance']} (уверенность {o.get('confidence', 60)}%)"
                for o in opinions))

        journal = await _safe(lambda: mem.latest_journal(db), None, "дневник") if mem else None
        if journal and journal.get("content"):
            when = _fmt_when(journal.get("created_at"), tz)
            parts.append(f"МОЯ ПРОШЛАЯ ЗАПИСЬ В ДНЕВНИКЕ{' (' + when + ')' if when else ''}:\n{str(journal['content']).strip()}")

        projects = await _safe(lambda: db.fetchall(
            "SELECT p.id, p.name, p.goal, p.status, "
            "(SELECT COUNT(*) FROM tasks t WHERE t.project_id=p.id AND t.status IN ('todo','doing')) AS open_tasks "
            "FROM projects p WHERE p.status IN ('active','paused') ORDER BY p.status, p.updated_at DESC LIMIT 20"),
            [], "проекты")
        if projects:
            parts.append("ЕГО ПРОЕКТЫ:\n" + "\n".join(
                f"- #{p['id']} {p['name']} ({'на паузе' if p['status'] == 'paused' else 'активен'}, "
                f"открытых задач: {p['open_tasks']})" + (f" — цель: {_one_line(p['goal'], 200)}" if p.get("goal") else "")
                for p in projects))

        pr = _module("projects")
        due = await _safe(lambda: pr.due_tasks(db, tz, 2), [], "задачи со сроком") if pr else []
        if due:
            parts.append("ЗАДАЧИ С ГОРЯЩИМ СРОКОМ:\n" + "\n".join(
                f"- #{t['id']} {_one_line(t['text'], 200)} — срок {t.get('due_local') or '?'}"
                + (" (ПРОСРОЧЕНО)" if t.get("overdue") else "")
                + (f" [проект: {t['project']}]" if t.get("project") else "")
                for t in due[:20]))

        ideas = await _safe(lambda: db.fetchall(
            "SELECT id, title, score, status FROM ideas WHERE status IN ('new','thinking','in_work') "
            "ORDER BY updated_at DESC, id DESC LIMIT ?", (REFLECT_IDEAS,)), [], "идеи")
        if ideas:
            parts.append("ЕГО ИДЕИ В ДВИЖЕНИИ:\n" + "\n".join(
                f"- #{i['id']} {_one_line(i['title'], 150)} · "
                f"{str(i['score']) + '/10' if i.get('score') is not None else 'без оценки'} · {i['status']}"
                for i in ideas))

        followups = await self._active_followups()
        if followups:
            parts.append("УЖЕ СТОЯТ МОИ FOLLOW-UP'Ы:\n" + "\n".join(
                f"- #{r['id']} {_fmt_when(r.get('next_at'), tz) or 'без срока'} · {_one_line(r['text'], 200)}"
                for r in followups))

        parts.append("Подумай и верни JSON.")
        return "\n\n".join(parts)

    async def _active_followups(self) -> list[dict]:
        return await _safe(lambda: self.db.fetchall(
            "SELECT id, text, next_at FROM reminders WHERE kind='followup' AND status='active' "
            "ORDER BY (next_at IS NULL), next_at LIMIT 20"), [], "follow-up'ы")

    async def _persist_reflection(self, data: dict) -> str | None:
        """Сохранить итог рефлексии. Кривой элемент пропускается со строкой в логе, остальное пишется."""
        db = self.db
        mem = _module("memory")
        journal = _as_text(data.get("journal"))
        saved_journal = False
        if journal and mem is not None:
            try:
                await mem.add_journal(db, journal)
                saved_journal = True
            except Exception as e:
                log.warning("рефлексия: дневник не записан: %s", e)

        if mem is None:
            log.warning("рефлексия: модуля памяти нет — факты и позиции не сохраняю")
        facts = _items(data.get("facts"))[:REFLECT_MAX_FACTS] if mem else []
        opinions = _items(data.get("opinions"))[:REFLECT_MAX_OPINIONS] if mem else []
        for f in facts:
            if isinstance(f, str):
                content, cat = f, "general"
            elif isinstance(f, dict):
                content, cat = _as_text(f.get("content")), _as_text(f.get("category")) or "general"
            else:
                continue
            try:
                await mem.add_fact(db, content, cat, source="reflection")
            except Exception as e:
                log.info("рефлексия: факт пропущен (%r): %s", content[:80], e)

        for o in opinions:
            if not isinstance(o, dict):
                continue
            try:
                await mem.upsert_opinion(
                    db, topic=_as_text(o.get("topic")), stance=_as_text(o.get("stance")),
                    reasons=_as_text(o.get("reasons")), confidence=o.get("confidence", 60),
                    opinion_id=o.get("opinion_id"),
                    why_changed=_as_text(o.get("why_changed")) or REFLECT_WHY_DEFAULT)
            except Exception as e:
                log.info("рефлексия: позиция пропущена (%r): %s", str(o.get("topic"))[:80], e)

        rem = _module("reminders")
        followups = _items(data.get("followups"))[:REFLECT_MAX_FOLLOWUPS] if rem else []
        have = {" ".join(str(r["text"]).lower().split()) for r in await self._active_followups()}
        turn = self.ctx.child()
        for fu in followups:
            if not isinstance(fu, dict):
                continue
            about, when = _as_text(fu.get("about")), fu.get("when")
            if not about or not isinstance(when, str) or not when.strip():
                log.info("рефлексия: follow-up без about/when пропущен: %r", fu)
                continue
            key = " ".join(about.lower().split())
            if key in have:
                continue
            try:
                await rem.create_reminder(turn, text=about, when=when, kind="followup")
                have.add(key)
            except Exception as e:
                log.info("рефлексия: follow-up пропущен (%r, %r): %s", about[:80], when, e)

        return journal if saved_journal else None
