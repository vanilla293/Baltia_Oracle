"""Регрессии по кросс-групповым доработкам (этап верификации).

Здесь проверяется то, что «дошивалось» на финише поверх работы пяти чинильщиков — каждая правка
живёт в файле не «своей» группы, поэтому и тесты собраны отдельно:

A6  (brief.py)    — плановая утренняя сводка (strict=True) при ВРЕМЕННОМ сбое модели/сети пробрасывает
                    ошибку, чтобы планировщик повторил её в окне догона; при постоянной ошибке и в
                    интерактивном пути (strict=False, /today, панель) — как раньше, отдаёт шаблон.
A6  (agent.py)    — ночная рефлексия при временном сбое обеих попыток (deep→fast) пробрасывает ошибку
                    (планировщик повторит), при постоянной — молча возвращает None.
A22 (scheduler)   — после ночной рефлексии раз в сутки зовётся db.prune_old (не с горячего пути);
                    её сбой саму рефлексию не заваливает.
A30 (app.py)      — панель спрашивает баланс живого ключа (LLM._key_idx), а не всегда cfg.api_keys[0].
A33 (agent.py)    — конспект и его строка в поиске пишутся одной транзакцией (индекс не разъезжается).
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from oracle import db as dbmod
from oracle import usage as usage_mod
from oracle.agent import Agent
from oracle.llm import LLMError
from oracle.services import brief
from oracle.services.scheduler import Scheduler

MSK = ZoneInfo("Europe/Moscow")
UTC = timezone.utc
QUIET = dict(morning_brief_time="", birthday_time="", news_digest_time="", reflection_time="")


def set_local(clock, y, mo, d, h, mi=0) -> None:
    clock.set(datetime(y, mo, d, h, mi, tzinfo=MSK).astimezone(UTC))


def jobs_ctx(ctx, **kw):
    ctx.cfg = replace(ctx.cfg, **{**QUIET, **kw})
    return ctx


class _RecordAgent:
    """Агент-заглушка: рефлексия удаётся (или падает по сценарию), считаем вызовы."""

    def __init__(self, reflect_error: BaseException | None = None):
        self.reflected = 0
        self._error = reflect_error

    async def reflect(self):
        self.reflected += 1
        if self._error is not None:
            raise self._error
        return "запись в дневник"


# ── A6: brief.morning_brief(strict=...) ───────────────────────────────────────
@pytest.fixture
def brief_stub(monkeypatch):
    """gather и format_data не трогаем в этих тестах — важна только ветка обработки ошибки модели."""
    async def gather_min(ctx, *a, **kw):
        return {"today": ctx.now_local().date()}

    monkeypatch.setattr(brief, "gather", gather_min)
    monkeypatch.setattr(brief, "format_data", lambda ctx, d: "материал дня")


def _llm_raises(ctx, monkeypatch, err):
    async def boom(*a, **kw):
        raise err
    monkeypatch.setattr(ctx.llm, "ask", boom)


async def test_morning_brief_strict_reraises_transient(ctx, brief_stub, monkeypatch):
    _llm_raises(ctx, monkeypatch, LLMError("сервер модели сбоит", status=503))
    with pytest.raises(LLMError):
        await brief.morning_brief(ctx, mode="now", strict=True)


async def test_morning_brief_strict_returns_template_on_permanent(ctx, brief_stub, monkeypatch):
    # постоянная ошибка (кончились деньги) — повтор не поможет: как и раньше, отдаём шаблон
    _llm_raises(ctx, monkeypatch, LLMError("баланс кончился", status=402, fatal=True))
    text = await brief.morning_brief(ctx, mode="now", strict=True)
    assert text.startswith("Что осталось на сегодня")


async def test_morning_brief_nonstrict_returns_template_on_transient(ctx, brief_stub, monkeypatch):
    # интерактивный путь (/today, кнопка панели): владелец ждёт ответ здесь и сейчас — шаблон, не ошибка
    _llm_raises(ctx, monkeypatch, LLMError("сервер модели сбоит", status=503))
    text = await brief.morning_brief(ctx, mode="now")          # strict=False по умолчанию
    assert text.startswith("Что осталось на сегодня")


# ── A6: agent.reflect() пробрасывает временный сбой, но молчит на постоянном ────
class _TransientJSON:
    """Обе попытки ask_json (deep и fast) падают одинаково — сбой на последней и решает исход."""

    def __init__(self, err: LLMError):
        self._err = err
        self.tries = 0

    async def ask_json(self, system, user, *, deep=False, **kw):
        self.tries += 1
        raise self._err

    async def complete(self, *a, **k):
        raise AssertionError("complete не должен вызываться")

    async def aclose(self):
        pass


async def test_reflect_reraises_when_both_attempts_transient(ctx, db):
    await db.add_message("user", "день был длинный")
    llm = _TransientJSON(LLMError("сервер модели сбоит", status=503))
    ctx.llm = llm
    with pytest.raises(LLMError):
        await Agent(ctx).reflect()
    assert llm.tries == 2                    # обе попытки: глубокая и быстрая


async def test_reflect_returns_none_when_both_attempts_permanent(ctx, db):
    await db.add_message("user", "день был длинный")
    ctx.llm = _TransientJSON(LLMError("плохой запрос", status=400))     # 400 — не временная
    assert await Agent(ctx).reflect() is None


# ── A22: после рефлексии раз в сутки чистим историю (и её сбой не валит рефлексию) ─
async def test_reflection_job_prunes_history(ctx, clock, notifier, monkeypatch):
    jobs_ctx(ctx, reflection_time="23:30")
    agent = _RecordAgent()
    ctx.services.agent = agent
    calls: list[dict] = []

    async def fake_prune(db, **kw):
        calls.append(kw)
        return {"messages": 0, "summaries": 0, "llm_usage": 0}

    monkeypatch.setattr(dbmod, "prune_old", fake_prune)
    set_local(clock, 2026, 9, 29, 0, 30)                    # бот поднялся сразу после полуночи
    s = Scheduler(ctx)
    await s.tick()
    await ctx.services.drain()
    assert agent.reflected == 1
    assert calls == [{"keep_messages": 5000, "keep_summaries": 2000, "keep_days_usage": 180}]
    assert await ctx.db.kv_get("job:reflection") == "2026-09-28"    # задача закрыта


async def test_reflection_prune_failure_does_not_break_reflection(ctx, clock, notifier, monkeypatch):
    jobs_ctx(ctx, reflection_time="23:30")
    agent = _RecordAgent()
    ctx.services.agent = agent

    async def boom_prune(db, **kw):
        raise RuntimeError("диск переполнен")

    monkeypatch.setattr(dbmod, "prune_old", boom_prune)
    set_local(clock, 2026, 9, 29, 0, 30)
    s = Scheduler(ctx)
    await s.tick()
    await ctx.services.drain()
    # рефлексия прошла и закрыта: сбой чистки её не отменяет и не гонит повтор
    assert agent.reflected == 1
    assert await ctx.db.kv_get("job:reflection") == "2026-09-28"


async def test_transient_reflection_retries_then_prunes(ctx, clock, notifier, monkeypatch):
    """Временный сбой рефлексии → задача не закрыта, поставлен повтор; на повторе рефлексия удаётся
    и чистка зовётся ровно один раз (на неудачной попытке — не звалась)."""
    jobs_ctx(ctx, reflection_time="23:30")
    agent = _RecordAgent(reflect_error=LLMError("нет связи с сервером модели", status=None))
    ctx.services.agent = agent
    calls: list[dict] = []

    async def fake_prune(db, **kw):
        calls.append(kw)
        return {"messages": 0, "summaries": 0, "llm_usage": 0}

    monkeypatch.setattr(dbmod, "prune_old", fake_prune)
    set_local(clock, 2026, 9, 29, 0, 30)
    s = Scheduler(ctx)
    await s.tick()
    await ctx.services.drain()
    assert agent.reflected == 1 and calls == []                     # упала → чистки не было
    assert await ctx.db.kv_get("job:reflection") is None            # не закрыта
    assert "reflection" in s._job_retry_at                          # поставлен повтор

    agent._error = None                                             # модель ожила
    set_local(clock, 2026, 9, 29, 0, 36)                            # позже JOB_RETRY[0]
    await s.tick()
    await ctx.services.drain()
    assert agent.reflected == 2 and len(calls) == 1                 # повтор удался, чистка один раз
    assert await ctx.db.kv_get("job:reflection") == "2026-09-28"


# ── A30: панель спрашивает баланс ЖИВОГО ключа (LLM._key_idx) ──────────────────
async def test_dashboard_status_asks_balance_of_live_key(ctx, monkeypatch):
    from types import SimpleNamespace

    from oracle.app import DashboardStateImpl

    captured: dict = {}

    async def fake_balance(cfg, http=None, *, live_index=None):
        captured["live_index"] = live_index
        return {"is_available": True, "balances": []}

    monkeypatch.setattr(usage_mod, "balance", fake_balance)

    deps = SimpleNamespace(cfg=ctx.cfg, db=ctx.db, llm=SimpleNamespace(_key_idx=2))
    state = DashboardStateImpl(deps, runtime=None, logbuffer=None, env_path=None)
    out = await state.status()
    assert captured["live_index"] == 2                     # живой ключ, а не всегда нулевой
    assert out["balance"]["is_available"] is True


async def test_dashboard_status_without_llm_key_idx_is_safe(ctx, monkeypatch):
    from types import SimpleNamespace

    from oracle.app import DashboardStateImpl

    captured: dict = {}

    async def fake_balance(cfg, http=None, *, live_index=None):
        captured["live_index"] = live_index
        return None

    monkeypatch.setattr(usage_mod, "balance", fake_balance)
    deps = SimpleNamespace(cfg=ctx.cfg, db=ctx.db, llm=None)     # llm ещё не поднят
    state = DashboardStateImpl(deps, runtime=None, logbuffer=None, env_path=None)
    out = await state.status()
    assert captured["live_index"] is None                       # None безопасен: usage.balance берёт первый рабочий
    assert out["balance"] == {"is_available": False, "balances": []}


# ── A33: конспект и его строка поиска пишутся одной транзакцией ────────────────
async def test_summary_and_index_written_atomically(ctx, db, fake_llm, monkeypatch):
    # набиваем реплик больше, чем на окно истории + один кусок, чтобы summarize_old сработал
    cfg = ctx.cfg
    need = cfg.history_messages + cfg.summary_chunk + cfg.summary_chunk + 5
    for i in range(need):
        await db.add_message("user" if i % 2 == 0 else "assistant", f"реплика номер {i} про кофе")
    fake_llm.script = ["Свёрнутый конспект про кофе."]
    agent = Agent(ctx)
    assert await agent.summarize_old() is True

    sid = await db.scalar("SELECT id FROM summaries ORDER BY id DESC LIMIT 1")
    assert sid is not None
    # запись есть И её строка в поиске есть — обе в одной транзакции
    idx = await db.scalar("SELECT COUNT(*) FROM search_index WHERE kind='summary' AND ref_id=?", (int(sid),))
    assert idx == 1
    assert await db.search("summary", "кофе")            # находится по содержимому


async def test_summary_rolls_back_when_index_fails(ctx, db, fake_llm, monkeypatch):
    cfg = ctx.cfg
    need = cfg.history_messages + cfg.summary_chunk + cfg.summary_chunk + 5
    for i in range(need):
        await db.add_message("user" if i % 2 == 0 else "assistant", f"реплика номер {i} про чай")
    fake_llm.script = ["Свёрнутый конспект про чай."]

    async def boom(c, kind, ref_id, body):
        raise RuntimeError("индекс сломался")

    monkeypatch.setattr(db, "index_put_tx", boom)
    agent = Agent(ctx)
    with pytest.raises(RuntimeError):
        await agent.summarize_old()
    # транзакция откатилась целиком: конспекта нет и реплики остались несвёрнутыми (свернём в другой раз)
    assert await db.scalar("SELECT COUNT(*) FROM summaries") == 0
    assert await db.scalar("SELECT COUNT(*) FROM messages WHERE summarized=1") == 0


async def test_idea_write_rolls_back_when_index_fails(ctx, db, monkeypatch):
    from oracle.tools import ideas as ide

    async def boom(c, kind, ref_id, body):
        raise RuntimeError("индекс сломался")

    monkeypatch.setattr(db, "index_put_tx", boom)
    with pytest.raises(RuntimeError):
        await ide.t_save_idea(ctx, title="Кофейня у вокзала", content="кофе навынос", evaluation="ок", score=5)
    # строка идеи и её индекс — одной транзакцией: индекс упал → идея не осталась (A33)
    assert await db.scalar("SELECT COUNT(*) FROM ideas") == 0


async def test_project_write_and_index_atomic(ctx, db, monkeypatch):
    from oracle.tools import projects as prj

    # обычный путь: проект создан И находится по содержимому
    await prj.t_create_project(ctx, name="Сайт студии", description="лендинг", goal="запуск до ноября")
    pid = await db.scalar("SELECT id FROM projects ORDER BY id DESC LIMIT 1")
    assert await db.search("project", "лендинг") == [int(pid)]

    # индекс падает → изменения проекта откатываются целиком (A33)
    async def boom(c, kind, ref_id, body):
        raise RuntimeError("индекс сломался")

    monkeypatch.setattr(db, "index_put_tx", boom)
    with pytest.raises(RuntimeError):
        await prj.t_create_project(ctx, name="Второй проект", description="важное", goal="цель")
    assert await db.scalar("SELECT COUNT(*) FROM projects WHERE name='Второй проект'") == 0
