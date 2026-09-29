"""Адаптивная модель: быстрая сама передаёт сложный вопрос глубокой (think_deeper)."""
from __future__ import annotations

from dataclasses import replace

from oracle.agent import ESCALATE_TEXT, THINK_DEEPER, Agent


def _names(call: dict) -> list[str]:
    return [t["function"]["name"] for t in (call.get("tools") or [])]


async def test_fast_model_escalates_to_deep(ctx, fake_llm, notifier):
    fake_llm.script = [{"name": THINK_DEEPER, "args": {"reason": "решение про деньги"}}, "Глубокий разбор."]
    reply = await Agent(ctx).handle("вложить половину накоплений в крипту?")
    assert reply.text == "Глубокий разбор." and reply.deep is True and reply.error is None
    first, second = fake_llm.calls
    assert first["deep"] is False and THINK_DEEPER in _names(first)
    assert second["deep"] is True and THINK_DEEPER not in _names(second)
    assert ESCALATE_TEXT in notifier.texts()
    # ход в истории один: вопрос и ответ, без следов переключения
    roles = [r["role"] for r in await ctx.db.fetchall("SELECT role FROM messages ORDER BY id")]
    assert roles == ["user", "assistant"]


async def test_no_escalation_after_actions(ctx, fake_llm, notifier):
    fake_llm.script = [
        {"name": "remember", "args": {"content": "Любит кофе без сахара", "category": "preference"}},
        {"name": THINK_DEEPER, "args": {"reason": "поздно"}},
        "Запомнил.",
    ]
    reply = await Agent(ctx).handle("запомни: кофе без сахара")
    assert reply.text == "Запомнил." and reply.deep is False
    assert all(c["deep"] is False for c in fake_llm.calls)
    assert ESCALATE_TEXT not in notifier.texts()


async def test_no_escalation_tool_in_deep_mode_or_when_off(ctx, fake_llm):
    fake_llm.script = ["ок"]
    await Agent(ctx).handle("вопрос", deep=True)
    assert THINK_DEEPER not in _names(fake_llm.calls[-1])
    ctx.cfg = replace(ctx.cfg, llm_auto_deep=False)
    fake_llm.script = ["ок"]
    await Agent(ctx).handle("ещё вопрос")
    assert THINK_DEEPER not in _names(fake_llm.calls[-1])


def test_default_deep_model_is_pro_for_deepseek(tmp_path, monkeypatch):
    from oracle import config
    for k in ("LLM_MODEL_DEEP", "LLM_MODEL", "LLM_BASE_URL", "LLM_AUTO_DEEP"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / "e.env"
    env.write_text("", encoding="utf-8")
    c = config.load(env)
    assert c.llm_model == "deepseek-flash" and c.llm_model_deep == "deepseek-v4-pro" and c.llm_auto_deep
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "llama3")
    c = config.load(env)
    assert c.llm_model_deep == "llama3"
