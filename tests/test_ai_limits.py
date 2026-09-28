"""Регрессия 24.09.2026 «ИИ без границ»: форма запроса к DeepSeek, узлы у денег без починки и без повтора
без размышления, своя очередь для узлов у денег, честная обрезка; v5.4.1 — пределы входов возвращены
(60 000 / 300 000 / 450 000). Без сети: ai.ask подменяется фейком."""
import asyncio
import os

import httpx
import pytest
from openai import APITimeoutError

from backend import ai, ai_v5, config


def _kw(model, *, thinking=True, json_mode=False, effort="", max_tokens=None):
    return ai._build_kwargs(model, [{"role": "user", "content": "x"}], json_mode=json_mode, thinking=thinking,
                            effort=effort, temperature=1.0,
                            max_tokens=config.AI_MAX_TOKENS if max_tokens is None else max_tokens)


def test_default_request_sends_max_tokens_but_no_effort():
    """Потолок — явный максимум модели (без него сервер режет 64K вместе с размышлением); effort не шлём."""
    kw = _kw(config.DEEPSEEK_MODEL)
    assert kw["max_tokens"] == 393_216 and config.AI_MAX_TOKENS == 393_216
    assert "reasoning_effort" not in kw and "extra_body" not in kw and "temperature" not in kw
    kf = _kw(config.DEEPSEEK_MODEL_FAST, json_mode=True)
    assert kf["model"] == "deepseek-flash" and kf["max_tokens"] == 393_216 and "extra_body" not in kf
    assert kf["response_format"] == {"type": "json_object"}


def test_explicit_effort_and_thinking_off_still_work():
    assert _kw(config.DEEPSEEK_MODEL, effort="max")["reasoning_effort"] == "max"
    assert "reasoning_effort" not in _kw(config.DEEPSEEK_MODEL, effort="auto")
    off = _kw("deepseek-flash", thinking=False, max_tokens=20)
    assert off["extra_body"] == {"thinking": {"type": "disabled"}} and off["max_tokens"] == 20 and off["temperature"] == 1.0


@pytest.mark.parametrize("model,v4", [("deepseek-flash", True), ("deepseek-v4-pro", True), ("deepseek-v4-flash", True),
                                      ("deepseek-v4.1-pro", True), ("deepseek-reasoner", False), ("deepseek-chat", False),
                                      ("deepseek-v3", False), ("gpt-x", False)])
def test_model_family_detection(model, v4):
    assert ai._is_v4(model) is v4


def test_max_tokens_clamp_to_api_maximum(monkeypatch):
    monkeypatch.setenv("AI_MAX_TOKENS", "999999")
    config._refresh()
    try:
        assert config.AI_MAX_TOKENS == 393_216
        monkeypatch.setenv("AI_MAX_TOKENS", "0")
        config._refresh()
        assert config.AI_MAX_TOKENS == 0 and "max_tokens" not in _kw(config.DEEPSEEK_MODEL)
    finally:
        monkeypatch.delenv("AI_MAX_TOKENS", raising=False)
        config._refresh()
    assert config.AI_MAX_TOKENS == 393_216


def test_timeout_is_retried_once_only():
    e = APITimeoutError(request=httpx.Request("POST", "https://api.deepseek.com/x"))
    assert ai._retry_delay(e, 1) == 4.0 and ai._retry_delay(e, 2) is None


def test_thinking_tail_always_marked():
    assert ai._tail_of_thinking("") == ""
    assert ai._tail_of_thinking('черновик {"decision": "ЖДАТЬ"} … нет, СЛИТЬ').startswith(ai.THINK_MARK)
    long = "x" * 100_000 + "\nВЕРДИКТ: " + "y" * 500
    out = ai._tail_of_thinking(long)
    assert out.startswith(ai.THINK_MARK) and "ВЕРДИКТ" in out


def _fake_ask(script):
    """script: список ответов по порядку; calls копит маршруты и thinking."""
    calls = []

    async def ask(system, user, *, model=None, json_mode=False, thinking=None, max_tokens=None, route="", api_key=None, **_):
        calls.append((route, thinking))
        return script.pop(0)
    return ask, calls


@pytest.mark.parametrize("route", sorted(ai_v5.MONEY_ROUTES))
def test_money_routes_never_repaired_or_rethought_without_thinking(monkeypatch, route):
    ask, calls = _fake_ask([ai.THINK_MARK + "\nчерновик", ai.THINK_MARK + "\nснова"])
    monkeypatch.setattr(ai_v5.ai, "ask", ask)
    monkeypatch.setattr(ai_v5, "key", lambda: "k")
    with pytest.raises(RuntimeError):
        asyncio.run(ai_v5._json_with_repair("s", "u", model="m", think=True, route=route, max_tokens=None))
    assert [c[0] for c in calls] == [route, route + "_retry"] and all(c[1] is True for c in calls)
    ask, calls = _fake_ask(["{кривой json", '{"decision": "ЖДАТЬ"}'])
    monkeypatch.setattr(ai_v5.ai, "ask", ask)
    assert asyncio.run(ai_v5._json_with_repair("s", "u", model="m", think=True, route=route, max_tokens=None)) == {"decision": "ЖДАТЬ"}
    assert [c[0] for c in calls] == [route, route + "_retry"] and not any("_repair" in c[0] or "_nothink" in c[0] for c in calls)


def test_mechanics_route_keeps_nothink_and_repair(monkeypatch):
    ask, calls = _fake_ask([ai.THINK_MARK + "\nдумала", "{кривой", '{"ok": 1}'])
    monkeypatch.setattr(ai_v5.ai, "ask", ask)
    monkeypatch.setattr(ai_v5, "key", lambda: "k")
    assert asyncio.run(ai_v5._json_with_repair("s", "u", model="m", think=True, route="group", max_tokens=None)) == {"ok": 1}
    assert [c[0] for c in calls] == ["group", "group_nothink", "group_repair"]
    assert calls[1][1] is False and calls[2][1] is False


def test_money_routes_have_their_own_queue():
    assert ai_v5._sem_for("mission_guard") is ai_v5.SEM_MONEY and ai_v5._sem_for("triage") is ai_v5.SEM_FLASH
    saved = (ai_v5.SEM_FLASH, ai_v5.SEM_MONEY)

    async def scenario():
        ai_v5.SEM_FLASH, ai_v5.SEM_MONEY = asyncio.Semaphore(6), asyncio.Semaphore(3)   # свои на этот loop, без утечки
        held = [asyncio.create_task(ai_v5.SEM_FLASH.acquire()) for _ in range(16)]
        await asyncio.sleep(0)
        assert ai_v5.SEM_FLASH.locked()
        t0 = asyncio.get_running_loop().time()
        async with ai_v5._sem_for("mission_guard"):
            pass
        assert asyncio.get_running_loop().time() - t0 < 0.5     # очередь разметки не держит узел у денег
        for t in held:
            t.cancel()
        await asyncio.gather(*held, return_exceptions=True)
    try:
        asyncio.run(scenario())
    finally:
        ai_v5.SEM_FLASH, ai_v5.SEM_MONEY = saved


@pytest.mark.parametrize("route", ["mission_guard", "mission_exec"])
def test_money_route_retry_is_checked_for_thinking_mark_and_is_single(monkeypatch, route):
    """Находка проверяющего: кривой JSON → повтор ушёл в размышления с черновиком — черновик решением не становится;
    и на весь вызов ровно один повтор."""
    ask, calls = _fake_ask(["{кривой", ai.THINK_MARK + '\nчерновик {"decision": "СЛИТЬ"} … нет, ЖДАТЬ {"decision": "ЖДАТЬ"}'])
    monkeypatch.setattr(ai_v5.ai, "ask", ask)
    monkeypatch.setattr(ai_v5, "key", lambda: "k")
    with pytest.raises(RuntimeError):
        asyncio.run(ai_v5._json_with_repair("s", "u", model="m", think=True, route=route, max_tokens=None))
    assert [c[0] for c in calls] == [route, route + "_retry"]
    ask, calls = _fake_ask([ai.THINK_MARK + "\nдумала", "{кривой", '{"decision": "ЖДАТЬ"}'])
    monkeypatch.setattr(ai_v5.ai, "ask", ask)
    with pytest.raises(RuntimeError):
        asyncio.run(ai_v5._json_with_repair("s", "u", model="m", think=True, route=route, max_tokens=None))
    assert len(calls) == 2 and all(c[1] is True for c in calls)


def test_input_limits_restored(monkeypatch):
    """v5.4.1 «ТРЕЗВЫЙ ПИЛОТ»: пределы ×3 из 5.3.3 (200 000 / 600 000 / 900 000) давали промпты по 400–600 тыс. симв.,
    в которых модель тонула; возвращены 60 000 / 300 000 / 450 000 — ножниц по-прежнему нет, выше пределов сжимает FLASH."""
    for k in ("PYTHIA_CTX_LIMIT", "PYTHIA_PROMPT_SOFT", "PYTHIA_PROMPT_CAP"):
        monkeypatch.delenv(k, raising=False)
    config._refresh()
    try:
        assert (config.PYTHIA_CTX_LIMIT, config.PYTHIA_PROMPT_SOFT, config.PYTHIA_PROMPT_CAP) == (60_000, 300_000, 450_000)
    finally:
        config._refresh()
    assert os.environ.get("AI_REASONING_EFFORT", "") == "" and config.AI_REASONING_EFFORT == ""
