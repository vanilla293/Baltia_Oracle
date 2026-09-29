"""Ядро: время и повторы, база и поиск по-русски, сборка запроса к DeepSeek, реестр инструментов."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from oracle import timeutil
from oracle.db import fts_query, stem
from oracle.llm import LLM, LLMError, LLMResponse, ToolCall, extract_json
from oracle.tools import base as tb

MSK = ZoneInfo("Europe/Moscow")
BERLIN = ZoneInfo("Europe/Berlin")


def test_parse_local_formats(clock):
    d = timeutil.parse_local("2026-09-29 07:30", MSK)
    assert d.tzinfo == MSK and (d.hour, d.minute) == (7, 30)
    assert timeutil.parse_local("2026-09-29T07:30", MSK).hour == 7
    assert timeutil.parse_local("29.09.2026 07:30", MSK).day == 29
    assert timeutil.parse_local("2026-09-29", MSK).hour == 9
    # «08:00» при сейчас 09:00 МСК → завтра
    d = timeutil.parse_local("08:00", MSK)
    assert d.date().isoformat() == "2026-09-29"
    d = timeutil.parse_local("2026-09-29T04:30:00+00:00", MSK)
    assert d.hour == 7
    with pytest.raises(ValueError):
        timeutil.parse_local("завтра утром", MSK)
    for odd in ("9999-12-31 23:30", "0001-01-01 01:00", "31.12.2300"):    # опечатки модели → ValueError,
        with pytest.raises(ValueError, match="год"):                        # а не OverflowError дальше
            timeutil.parse_local(odd, MSK)


def test_next_occurrence_once_and_daily(clock):
    start = datetime(2026, 9, 28, 10, 0)             # сегодня 10:00 МСК, сейчас 09:00
    n = timeutil.next_occurrence(None, start, MSK)
    assert n == datetime(2026, 9, 28, 7, 0, tzinfo=timezone.utc)
    clock.advance(hours=2)
    assert timeutil.next_occurrence(None, start, MSK) is None
    n = timeutil.next_occurrence("FREQ=DAILY", start, MSK)
    assert n == datetime(2026, 9, 29, 7, 0, tzinfo=timezone.utc)
    n = timeutil.next_occurrence("RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", start, MSK)
    assert n.astimezone(MSK).weekday() == 1


def test_next_occurrence_keeps_local_time_over_dst(clock):
    clock.set(datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc))
    start = datetime(2026, 10, 20, 7, 0)             # Берлин: 25.10 переход на зимнее
    n = timeutil.next_occurrence("FREQ=DAILY", start, BERLIN,
                                 after=datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc))
    assert n.astimezone(BERLIN).hour == 7 and n.astimezone(BERLIN).day == 26


def test_normalize_rrule():
    assert timeutil.normalize_rrule("rrule:freq=daily") == "FREQ=DAILY"
    assert timeutil.normalize_rrule("FREQ=WEEKLY;UNTIL=20261231T000000Z") == "FREQ=WEEKLY;UNTIL=20261231T000000"
    assert timeutil.normalize_rrule("") is None
    with pytest.raises(ValueError):
        timeutil.normalize_rrule("FREQ=SECONDLY")
    with pytest.raises(ValueError):
        timeutil.normalize_rrule("FREQ=MINUTELY")
    with pytest.raises(ValueError):
        timeutil.normalize_rrule("BYDAY=MO")
    assert timeutil.describe_rrule("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR") == "по будням"


def test_stem_and_query():
    assert stem("кофейню") == stem("кофейня")
    assert stem("идеи") == stem("идея")
    q = fts_query("Моя идея про кофейню!")
    assert '"кофейн"*' in q and " OR " in q


async def test_db_kv_messages_search(db):
    await db.kv_set("x", {"a": 1})
    assert await db.kv_get("x") == {"a": 1}
    assert await db.kv_get("nope", 5) == 5
    await db.add_message("user", "привет")
    await db.add_message("assistant", "здорово")
    msgs = await db.recent_messages(10)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    await db.index_put("idea", 1, "Кофейня на колёсах у вокзала")
    await db.index_put("idea", 2, "Приложение для учёта тренировок")
    await db.index_put("fact", 1, "любит кофе")
    assert await db.search("idea", "про кофейню") == [1]
    assert await db.search("idea", "тренировки") == [2]
    assert await db.search("idea", "") == []
    await db.index_put("idea", 1, "Велосипеды")
    assert await db.search("idea", "кофейня") == []
    await db.index_delete("idea", 2)
    assert await db.search("idea", "тренировки") == []
    assert await db.kv_delete("x") is True and await db.kv_get("x") is None
    assert await db.kv_delete("x") is False


def test_config_key_follows_base_url_and_paths_are_rooted(monkeypatch, tmp_path):
    from oracle import config
    for k in ("DEEPSEEK_API_KEY", "LLM_API_KEY", "LLM_BASE_URL", "DATA_DIR", "DB_PATH", "USERBOT_SESSION"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / "none.env"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds")
    monkeypatch.setenv("LLM_API_KEY", "sk-or")
    assert config.load(env).llm_api_key == "sk-ds"                   # адрес DeepSeek — его ключ
    monkeypatch.setenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    assert config.load(env).llm_api_key == "sk-or"                   # чужой адрес — ключ DeepSeek не уходит
    monkeypatch.delenv("LLM_API_KEY")
    assert config.load(env).llm_api_key == "sk-ds"                   # другого ключа нет — берём, что есть
    monkeypatch.setenv("DB_PATH", "var/o.db")
    monkeypatch.setenv("USERBOT_SESSION", "var/ub")
    s = config.load(env)
    assert s.db_path == config.ROOT / "var" / "o.db"                # от папки проекта, а не от cwd
    assert s.userbot_session == str(config.ROOT / "var" / "ub")
    assert s.data_dir == config.ROOT / "data"


def test_persona_owner_name_reads_right():
    from oracle.persona import build_system
    s = build_system(name="Оракул", owner_name="Андрей", now="сейчас")
    assert "личный ИИ владельца (Андрей)." in s and "ВЛАДЕЛЕЦ: Андрей" in s
    assert "личный ИИ владельца." in build_system(name="Оракул", now="сейчас")


def test_payload_fast_disables_thinking(cfg):
    llm = LLM(cfg)
    p = llm.build_payload([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])
    assert p["model"] == "deepseek-flash"
    assert p["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in p and "temperature" in p
    assert p["tool_choice"] == "auto" and p["max_tokens"] == cfg.llm_fast_max_tokens


def test_payload_deep_thinks(cfg):
    llm = LLM(cfg)
    p = llm.build_payload([{"role": "user", "content": "hi"}], deep=True, json_mode=True)
    assert "thinking" not in p and p["reasoning_effort"] == "high"
    assert "temperature" not in p and p["response_format"] == {"type": "json_object"}


def test_payload_other_provider(cfg):
    from dataclasses import replace
    c = replace(cfg, llm_base_url="http://localhost:11434/v1", llm_model="llama3", llm_model_deep="llama3")
    p = LLM(c).build_payload([{"role": "user", "content": "hi"}], deep=True)
    assert "thinking" not in p and "reasoning_effort" not in p and p["model"] == "llama3"


def test_to_message_passes_reasoning_back():
    r = LLMResponse(content="", reasoning="думаю…", tool_calls=[ToolCall("c1", "f", "{}")])
    m = r.to_message()
    assert m["reasoning_content"] == "думаю…" and m["tool_calls"][0]["function"]["name"] == "f"
    assert "reasoning_content" not in LLMResponse(content="x", reasoning="r").to_message()


def _mock_llm(cfg, handler):
    client = httpx.AsyncClient(base_url=cfg.llm_base_url, transport=httpx.MockTransport(handler))
    return LLM(cfg, client=client)


async def test_complete_parses_tool_calls(cfg):
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["thinking"] == {"type": "disabled"}
        return httpx.Response(200, json={"model": "deepseek-flash", "choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "reasoning_content": "r",
            "tool_calls": [{"id": "x1", "type": "function",
                            "function": {"name": "save_idea", "arguments": "{\"title\":\"t\"}"}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    llm = _mock_llm(cfg, handler)
    r = await llm.complete([{"role": "user", "content": "x"}])
    assert r.tool_calls[0].name == "save_idea" and r.reasoning == "r"
    assert llm.usage_total["calls"] == 1


async def test_complete_fatal_and_retry(cfg, monkeypatch):
    import asyncio
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    n = {"c": 0}

    def h401(req):
        return httpx.Response(401, json={"error": {"message": "bad key"}})
    with pytest.raises(LLMError) as ei:
        await _mock_llm(cfg, h401).complete([{"role": "user", "content": "x"}])
    assert ei.value.fatal and "401" in str(ei.value)

    def flaky(req):
        n["c"] += 1
        if n["c"] < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
    r = await _mock_llm(cfg, flaky).complete([{"role": "user", "content": "x"}])
    assert r.content == "ok" and n["c"] == 3


async def test_deep_model_fallback(cfg, monkeypatch):
    from dataclasses import replace
    c = replace(cfg, llm_model_deep="deepseek-v4-pro")
    seen = []

    def h(req):
        body = json.loads(req.content)
        seen.append(body["model"])
        if body["model"] == "deepseek-v4-pro":
            return httpx.Response(400, json={"error": {"message": "Model Not Exist"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
    r = await _mock_llm(c, h).complete([{"role": "user", "content": "x"}], deep=True)
    assert r.content == "ok" and seen == ["deepseek-v4-pro", "deepseek-flash"]


async def _nosleep(*a, **k):
    return None


def test_extract_json():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('вот: {"a": [1,2]} конец') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        extract_json("")


async def test_registry_dispatch(ctx):
    @tb.tool("t_echo", "эхо", {"x": {"type": "string"}, "n": {"type": "integer"}}, required=["x"])
    async def _echo(c, x, n=1):
        if x == "bad":
            raise ValueError("плохой x")
        return {"ok": True, "x": x * n}
    try:
        assert json.loads(await tb.dispatch("t_echo", '{"x":"a","n":2,"junk":1}', ctx)) == {"ok": True, "x": "aa"}
        assert "не хватает" in await tb.dispatch("t_echo", "{}", ctx)
        assert "плохой x" in await tb.dispatch("t_echo", '{"x":"bad"}', ctx)
        assert "не JSON" in await tb.dispatch("t_echo", "{oops", ctx)
        assert "нет такого" in await tb.dispatch("nope", "{}", ctx)
        assert any(s["function"]["name"] == "t_echo" for s in tb.schemas(ctx.cfg))
    finally:
        tb.REGISTRY.pop("t_echo", None)


def test_config_telegram_api_url_and_env_file(tmp_path, monkeypatch):
    from oracle import config
    env = tmp_path / "alt.env"
    env.write_text("BOT_NAME=Тестовый\nTELEGRAM_API_URL=http://127.0.0.1:8081/\n", encoding="utf-8")
    for k in ("BOT_NAME", "TELEGRAM_API_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ENV_FILE", str(env))
    c = config.load()
    assert c.bot_name == "Тестовый" and c.telegram_api_url == "http://127.0.0.1:8081"
    for k in ("BOT_NAME", "TELEGRAM_API_URL"):
        monkeypatch.delenv(k, raising=False)
