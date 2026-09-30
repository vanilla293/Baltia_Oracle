"""Починки LLM/агента (аудит A7, A19, A8, A31):
  • reasoning_content прокидывается на шагах с пустым размышлением (иначе глубокий цикл падает с 400);
  • откат «глубокая→быстрая» пересобирает payload (без reasoning_effort, с thinking:disabled, быстрые бюджеты);
  • глубокий режим тоже подаёт признак жизни, а эскалация не обещает «пару минут»;
  • ночная рефлексия при отказе JSON+размышление откатывается на JSON без размышления.
"""
from __future__ import annotations

import asyncio
import importlib
import json
from dataclasses import replace

import httpx

from oracle.agent import DEEP_SLOW_TEXT, ESCALATE_TEXT, Agent
from oracle.llm import LLM, LLMError, LLMResponse, ToolCall

TOOL = {"type": "function", "function": {"name": "f", "parameters": {"type": "object", "properties": {}}}}


def _mock_llm(cfg, handler) -> LLM:
    client = httpx.AsyncClient(base_url=cfg.llm_base_url, transport=httpx.MockTransport(handler))
    return LLM(cfg, client=client)


# ── A7: reasoning_content не теряется на шаге с пустым размышлением ──────────────
def test_to_message_keeps_empty_reasoning_in_thinking_mode():
    # думающий шаг с tool_calls, но пустым размышлением — поле обязано уйти (пустым), иначе 400
    m = LLMResponse(tool_calls=[ToolCall("c1", "f", "{}")], reasoning="", thinking=True).to_message()
    assert m["reasoning_content"] == ""
    # непустое размышление — как раньше
    m2 = LLMResponse(tool_calls=[ToolCall("c2", "f", "{}")], reasoning="думаю", thinking=True).to_message()
    assert m2["reasoning_content"] == "думаю"
    # не думающий шаг с пустым размышлением — поля быть не должно (быстрый режим)
    m3 = LLMResponse(tool_calls=[ToolCall("c3", "f", "{}")], reasoning="", thinking=False).to_message()
    assert "reasoning_content" not in m3
    # без tool_calls поле не шлём вовсе
    assert "reasoning_content" not in LLMResponse(content="x", reasoning="r", thinking=True).to_message()


async def test_deep_tool_loop_roundtrips_empty_reasoning(cfg):
    """Глубокий многошаговый цикл: второй шаг вернул tool_calls с ПУСТЫМ размышлением.
    Эмулируем правило DeepSeek (любое ассистентское сообщение с tool_calls обязано нести
    reasoning_content, иначе 400) и проверяем, что цикл доходит до итога."""
    step = {"n": 0}

    def h(req):
        body = json.loads(req.content)
        for m in body["messages"]:
            if m.get("role") == "assistant" and m.get("tool_calls") and "reasoning_content" not in m:
                return httpx.Response(400, json={"error": {"message": "reasoning_content must be passed back"}})
        step["n"] += 1
        if step["n"] == 1:                       # думал и позвал инструмент (размышление непустое)
            return httpx.Response(200, json={"model": "deepseek-v4-pro", "choices": [{
                "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": "", "reasoning_content": "прикидываю",
                    "tool_calls": [{"id": "a1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}}]})
        if step["n"] == 2:                       # снова инструмент, но размышление ПУСТОЕ — опасный случай
            return httpx.Response(200, json={"model": "deepseek-v4-pro", "choices": [{
                "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": "", "reasoning_content": "",
                    "tool_calls": [{"id": "a2", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}}]})
        return httpx.Response(200, json={"model": "deepseek-v4-pro", "choices": [{
            "finish_reason": "stop", "message": {"role": "assistant", "content": "итог"}}]})

    llm = _mock_llm(cfg, h)
    msgs: list[dict] = [{"role": "user", "content": "сложный вопрос"}]
    r1 = await llm.complete(msgs, tools=[TOOL], deep=True)
    assert r1.thinking and r1.reasoning == "прикидываю"
    msgs.append(r1.to_message())
    msgs.append({"role": "tool", "tool_call_id": "a1", "content": "{}"})

    r2 = await llm.complete(msgs, tools=[TOOL], deep=True)   # без фикса это упало бы 400
    assert r2.thinking and r2.reasoning == ""
    assert r2.to_message()["reasoning_content"] == ""        # ключевое: поле есть, хоть и пустое
    msgs.append(r2.to_message())
    msgs.append({"role": "tool", "tool_call_id": "a2", "content": "{}"})

    r3 = await llm.complete(msgs, tools=[TOOL], deep=True)
    assert r3.content == "итог"


# ── A19: откат «глубокая→быстрая» пересобирает payload под быструю модель ────────
async def test_deep_fallback_rebuilds_fast_payload(cfg):
    c = replace(cfg, llm_model_deep="deepseek-v4-pro")
    seen: list[dict] = []

    def h(req):
        body = json.loads(req.content)
        seen.append(body)
        if body["model"] == "deepseek-v4-pro":
            return httpx.Response(400, json={"error": {"message": "Model Not Exist"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    r = await _mock_llm(c, h).complete([{"role": "user", "content": "x"}], deep=True)
    assert r.content == "ok"
    deep_req, fast_req = seen
    assert deep_req["model"] == "deepseek-v4-pro" and deep_req["reasoning_effort"] == "high"
    assert deep_req["max_tokens"] == c.llm_deep_max_tokens and "thinking" not in deep_req
    # запрос пересобран целиком: размышление выключено, effort убран, температура и быстрые бюджеты вернулись
    assert fast_req["model"] == "deepseek-flash"
    assert fast_req.get("thinking") == {"type": "disabled"}
    assert "reasoning_effort" not in fast_req
    assert fast_req["max_tokens"] == c.llm_fast_max_tokens
    assert fast_req["temperature"] == c.llm_temperature


# ── A8: глубокий режим тоже не молчит, а эскалация не обещает «пару минут» ───────
def test_escalate_text_is_honest():
    assert "несколько минут" in ESCALATE_TEXT
    assert "пару минут" not in ESCALATE_TEXT and "пара минут" not in ESCALATE_TEXT


async def test_deep_mode_sends_slow_notice(ctx, notifier, db):
    class Slow:
        async def complete(self, messages, **kw):
            await asyncio.sleep(0.3)
            return LLMResponse(content="ответ")

    ctx.llm = Slow()
    agent = Agent(ctx)
    agent.deep_slow_notice_after = 0.05            # порог для теста — крохотный
    r = await agent.handle("подумай серьёзно", deep=True)
    assert r.text == "ответ" and r.deep is True
    assert notifier.texts() == [DEEP_SLOW_TEXT] and notifier.sent[0]["silent"] is True


# ── A31: рефлексия откатывается на JSON без размышления, если JSON+размышление отказал ──
class _DeepJSONFails:
    """ask_json падает в глубоком режиме (JSON+thinking), но работает в быстром."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.deep_tries = 0
        self.fast_tries = 0

    async def ask_json(self, system, user, *, deep=False, **kw):
        if deep:
            self.deep_tries += 1
            raise LLMError("JSON-режим с размышлением не поддержан (400)", status=400)
        self.fast_tries += 1
        return self.payload

    async def complete(self, *a, **k):
        raise AssertionError("complete не должен вызываться в этом тесте")

    async def aclose(self):
        pass


async def test_reflect_falls_back_to_fast_json(ctx, db):
    await db.add_message("user", "весь день чинил краны, вымотался")
    payload = {"journal": "день про краны", "facts": [], "opinions": [], "followups": []}
    llm = _DeepJSONFails(payload)
    ctx.llm = llm
    out = await Agent(ctx).reflect()
    assert out == "день про краны"
    assert llm.deep_tries == 1 and llm.fast_tries == 1
    mem = importlib.import_module("oracle.tools.memory")
    assert (await mem.latest_journal(db))["content"] == "день про краны"


async def test_reflect_returns_none_when_json_never_comes(ctx, db):
    await db.add_message("user", "привет")

    class _AllFail:
        async def ask_json(self, system, user, *, deep=False, **kw):
            raise LLMError("нет JSON", status=400)

        async def complete(self, *a, **k):
            raise AssertionError("complete не должен вызываться")

        async def aclose(self):
            pass

    ctx.llm = _AllFail()
    assert await Agent(ctx).reflect() is None
