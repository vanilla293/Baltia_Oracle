"""Offline API regressions: invalid input must not mutate live configuration."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api_council, api_mission, api_v5, bus


@pytest.fixture
def keys_client(monkeypatch):
    writes = Mock()
    legacy_write = Mock()
    reset = Mock()
    close_broker = AsyncMock()
    close_telegram = AsyncMock()
    monkeypatch.setattr(api_v5.config, "set_many", writes)
    monkeypatch.setattr(api_v5.config, "set_deepseek_keys", legacy_write)
    monkeypatch.setattr(api_v5.ai, "reset_client", reset)
    monkeypatch.setattr(api_v5.tinkoff, "aclose", close_broker)
    monkeypatch.setattr(api_v5, "telegram", SimpleNamespace(aclose=close_telegram))
    monkeypatch.setattr(api_v5, "keys_status", lambda: {"deepseek": True})
    app = FastAPI()
    app.include_router(api_v5.router)
    with TestClient(app) as client:
        yield client, writes, legacy_write, reset, close_broker, close_telegram


@pytest.mark.parametrize("invalid", [
    {"tinkoff": "не токен"},
    {"telegram_token": "missing-colon"},
    {"telegram_chat": "not-a-chat-id"},
    {"deepseek": [123]},
    {"deepseek": [True]},
    {"deepseek": [{"key": "sk-test"}]},
    {"tinkoff": {"token": "test"}},
    {"telegram_token": ["123:abc"]},
])
def test_invalid_key_bundle_does_not_partially_save(keys_client, invalid):
    client, writes, legacy_write, reset, close_broker, close_telegram = keys_client
    payload = {"deepseek": "sk-test", "tinkoff": "test-broker",
               "telegram_token": "123:test", "telegram_chat": "12345", **invalid}
    response = client.post("/api/v5/keys", json=payload)
    assert response.status_code == 400
    writes.assert_not_called()
    legacy_write.assert_not_called()
    reset.assert_not_called()
    close_broker.assert_not_awaited()
    close_telegram.assert_not_awaited()


def test_valid_key_bundle_saved_after_full_validation(keys_client):
    """Пул DeepSeek — через set_deepseek_keys (её перехватывает демо-режим), остальное — одной записью set_many."""
    client, writes, legacy_write, reset, close_broker, close_telegram = keys_client
    response = client.post("/api/v5/keys", json={
        "deepseek": "sk-one; sk-two, sk-one", "tinkoff": " test-broker\n",
        "telegram_token": "123:test", "telegram_chat": -100123,
    })
    assert response.status_code == 200
    assert response.json()["changed"] == ["deepseek", "tinkoff", "telegram_token", "telegram_chat"]
    writes.assert_called_once_with({"TINKOFF_TOKEN": "test-broker", "TG_BOT_TOKEN": "123:test",
                                   "TG_CHAT_ID": "-100123"})
    legacy_write.assert_called_once_with(["sk-one", "sk-two"])
    reset.assert_called_once_with()
    close_broker.assert_awaited_once_with()
    close_telegram.assert_awaited_once_with()


@pytest.mark.parametrize("deposit", ["nan", "NaN", "inf", "-inf", "1e9999", True, False, -1, "-10"])
def test_invalid_deposit_rejected_before_start(monkeypatch, deposit):
    start = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(api_mission.mission, "start", start)
    app = FastAPI()
    app.include_router(api_mission.router)
    with TestClient(app) as client:
        response = client.post("/api/v5/mission/start", json={"ticker": "SBER", "deposit": deposit})
    assert response.status_code == 400
    start.assert_not_awaited()


@pytest.mark.parametrize("deposit, expected", [(8000, 8000.0), ("8000.50", 8000.5),
                                               (0, None), ("0", None), (None, None), ("", None)])
def test_valid_deposit_and_auto_deposit_preserved(monkeypatch, deposit, expected):
    start = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(api_mission.mission, "start", start)
    result = asyncio.run(api_mission.api_mission_start({"ticker": "sber", "deposit": deposit}))
    assert result["ok"]
    assert start.await_args.args == ("SBER", "auto")
    assert start.await_args.kwargs["deposit"] == expected


@pytest.fixture
def isolated_runs(monkeypatch):
    monkeypatch.setattr(bus, "_RUNS", {})
    monkeypatch.setattr(bus, "_sink", None)
    monkeypatch.setattr(api_council, "_TASKS", set())
    monkeypatch.setattr(api_council, "_TASK_SCOPES", {})


@pytest.mark.parametrize("first, second", [("daily", "daily"), ("daily", "human"),
                                          ("human", "rerun"), ("rerun", "daily"),
                                          ("watch", "watch"), ("daily", "watch")])
def test_pending_run_rejects_concurrent_request(monkeypatch, isolated_runs, first, second):
    async def scenario():
        entered, register, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        started = []

        async def fake_run(scope):
            entered.set()
            await register.wait()
            rid = bus.start_run(scope)
            started.append(rid)
            try:
                await finish.wait()
            finally:
                bus.end_run(rid)
            return {"run_id": rid}

        monkeypatch.setattr(api_council.council, "daily", lambda *args: fake_run("daily"))
        monkeypatch.setattr(api_council.council, "human", lambda *args: fake_run("human"))
        monkeypatch.setattr(api_council.council, "update_with_news", lambda *args: fake_run("daily"))
        monkeypatch.setattr(api_council.watch, "tick", lambda **kwargs: fake_run("watch"))

        async def call(kind):
            if kind == "human":
                return await api_council.api_human({"text": "test"})
            if kind == "rerun":
                return await api_council.api_rerun({})
            if kind == "watch":
                return await api_council.api_watch_tick()
            return await api_council.api_daily({})

        request = asyncio.create_task(call(first))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert not bus.running()  # The first task has not registered its run yet.
            duplicate = await call(second)
            assert duplicate.status_code == 409
            register.set()
            response = await asyncio.wait_for(request, 1)
            assert response["ok"] and response["run_id"] == started[0]
            assert len(started) == 1
        finally:
            register.set()
            finish.set()
            await asyncio.gather(request, *list(api_council._TASKS), return_exceptions=True)
        assert not api_council._TASKS
        assert not api_council._TASK_SCOPES

    asyncio.run(scenario())


def test_failed_launch_cannot_report_another_runs_success(monkeypatch, isolated_runs):
    async def fail(*args):
        raise RuntimeError("launch failed")

    async def unrelated_run(*args):
        await asyncio.sleep(0)
        return "daily-unrelated"

    monkeypatch.setattr(api_council.council, "daily", fail)
    monkeypatch.setattr(api_council, "_wait_run", unrelated_run)
    response = asyncio.run(api_council.api_daily({}))
    assert response.status_code == 500
    assert b"launch failed" in response.body


@pytest.mark.parametrize("endpoint,function", [("resume", "resume"), ("panic", "panic"), ("reanalyze", "council_again")])
def test_mission_recovery_failure_returns_readable_json(monkeypatch, endpoint, function):
    import asyncio
    import json
    from unittest.mock import AsyncMock
    from backend import api_mission

    note = "не удалось проверить сохранённые аварийные заявки — новые операции заблокированы"
    monkeypatch.setattr(api_mission.mission, function, AsyncMock(side_effect=RuntimeError(note)))
    response = asyncio.run(getattr(api_mission, "api_mission_" + endpoint)({"ticker": "TEST"}))
    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["ok"] is False and body["note"] == note
