"""Интеграция панели с настоящим DashboardStateImpl: временная база + FakeLLM, без сети.

Проверяем, что провайдер состояния приложения (oracle.app.DashboardStateImpl) отдаёт /api/status
и делает круг /api/settings (прочитать → изменить → записать в .env). Баланс DeepSeek не
запрашивается: базовый адрес модели не deepseek, поэтому usage.balance сразу возвращает None
(никаких сетевых запросов).
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from oracle import usage
from oracle.app import DashboardStateImpl
from oracle.bot.handlers import Deps
from oracle.dashboard.server import create_app
from oracle.runtime import LogBuffer, Runtime
from oracle.tools.base import Services, ToolContext

TOKEN = "test-token"


def _state(cfg, db, fake_llm, tmp_path):
    # адрес модели — не deepseek: usage.balance() не пойдёт в сеть, а сразу вернёт None
    local_cfg = replace(cfg, llm_base_url="http://127.0.0.1:1/v1")
    services = Services()
    rt = Runtime()
    rt.note_telegram({"ok": True, "username": "baltia_test_bot", "id": 1})
    services.runtime = rt
    services.usage = usage
    ctx = ToolContext(cfg=local_cfg, db=db, llm=fake_llm, services=services)
    deps = Deps(cfg=local_cfg, db=db, llm=fake_llm, ctx=ctx)
    logbuf = LogBuffer()
    return DashboardStateImpl(deps, runtime=rt, logbuffer=logbuf, env_path=tmp_path / ".env"), rt


async def test_api_status_shape(cfg, db, fake_llm, tmp_path):
    state, rt = _state(cfg, db, fake_llm, tmp_path)
    app = create_app(state, token=TOKEN)
    async with TestClient(TestServer(app)) as client:
        r = await client.get(f"/api/status?t={TOKEN}")
        assert r.status == 200
        data = await r.json()
    assert data["bot_name"] == cfg.bot_name
    assert data["telegram"] == {"ok": True, "username": "baltia_test_bot", "id": 1}
    assert set(data["conflict"]) == {"active", "count", "hint"}
    assert data["conflict"]["active"] is False
    assert set(data["cost"]) == {"today", "month", "calls", "currency"}
    assert data["cost"]["today"] == 0.0 and data["cost"]["calls"] == 0
    assert data["balance"] == {"is_available": False, "balances": []}   # не deepseek → без сети
    assert data["model"] == {"fast": cfg.llm_model, "deep": cfg.llm_model_deep}
    assert data["single_instance_ok"] is True and data["twins"] == []


async def test_api_settings_roundtrip_writes_env(cfg, db, fake_llm, tmp_path):
    state, rt = _state(cfg, db, fake_llm, tmp_path)
    app = create_app(state, token=TOKEN)
    async with TestClient(TestServer(app)) as client:
        got = await (await client.get(f"/api/settings?t={TOKEN}")).json()
        assert "BOT_TOKEN" in got["values"] and got["sections"]
        assert "…" in got["values"]["BOT_TOKEN"]                     # секрет — только маской

        # валидная правда: меняем имя бота и порт панели
        r = await client.post(f"/api/settings?t={TOKEN}",
                              json={"BOT_NAME": "Балтия", "DASHBOARD_PORT": "8790"})
        res = await r.json()
        assert res["ok"] is True and res["restart_needed"] is True
        assert set(res["changed"]) == {"BOT_NAME", "DASHBOARD_PORT"} and res["errors"] == {}

        env_text = (tmp_path / ".env").read_text(encoding="utf-8")
        assert "BOT_NAME=Балтия" in env_text and "DASHBOARD_PORT=8790" in env_text

        # невалидное значение — ошибка по полю, .env не переписывается второй раз
        r = await client.post(f"/api/settings?t={TOKEN}", json={"DASHBOARD_PORT": "70000"})
        res = await r.json()
        assert res["ok"] is False and "DASHBOARD_PORT" in res["errors"] and res["changed"] == []


async def test_api_requires_token(cfg, db, fake_llm, tmp_path):
    state, _ = _state(cfg, db, fake_llm, tmp_path)
    app = create_app(state, token=TOKEN)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/api/status")).status == 401
        assert (await client.get("/api/status?t=wrong")).status == 401


async def test_api_logs_from_buffer(cfg, db, fake_llm, tmp_path):
    state, rt = _state(cfg, db, fake_llm, tmp_path)
    state.logbuffer.emit(_rec("oracle.app", "старт панели"))
    app = create_app(state, token=TOKEN)
    async with TestClient(TestServer(app)) as client:
        rows = await (await client.get(f"/api/logs?t={TOKEN}")).json()
    assert any(r["message"] == "старт панели" for r in rows)


async def test_action_clear_conflict_is_readonly(cfg, db, fake_llm, tmp_path):
    state, rt = _state(cfg, db, fake_llm, tmp_path)
    rt.note_conflict()
    app = create_app(state, token=TOKEN)
    async with TestClient(TestServer(app)) as client:
        res = await (await client.post(f"/api/action?t={TOKEN}",
                                       json={"name": "clear_conflict"})).json()
    assert res["ok"] is True and isinstance(res["twins"], list)   # реальный сканер процессов, без сети
    assert rt.conflict_at is None and rt.conflict_count == 0


def _rec(logger: str, message: str):
    import logging
    return logging.LogRecord(logger, logging.INFO, __file__, 1, message, (), None)
