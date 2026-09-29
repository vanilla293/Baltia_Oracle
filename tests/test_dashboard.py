"""Локальная веб-панель: страница отдаётся только с токеном, API возвращает данные провайдера,
настройки проверяются, действия доходят до state, сервер привязан к 127.0.0.1.

Сеть — только петля 127.0.0.1 через тестовые утилиты aiohttp; настоящих внешних запросов нет.
"""
from __future__ import annotations

import asyncio

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from oracle.dashboard import DashboardState, FakeState, create_app, start_dashboard
from oracle.dashboard import server as srv

TOKEN = "test-token-abcdefghijklmnop"


@pytest_asyncio.fixture
async def state() -> FakeState:
    return FakeState()


@pytest_asyncio.fixture
async def client(state):
    app = create_app(state, token=TOKEN)
    async with TestClient(TestServer(app)) as c:
        yield c


def _q(path: str) -> str:
    return path + ("&" if "?" in path else "?") + "t=" + TOKEN


# ── страница и токен ───────────────────────────────────────────────────────────────
async def test_page_served_with_token(client):
    r = await client.get(_q("/"))
    assert r.status == 200
    assert r.content_type == "text/html"
    body = await r.text()
    assert "<title>" in body and "Оракул" in body
    assert "/api/status" in body                      # это действительно наша страница


async def test_page_forbidden_without_token(client):
    r = await client.get("/")
    assert r.status == 403


async def test_api_unauthorized_without_token(client):
    r = await client.get("/api/status")
    assert r.status == 401
    r2 = await client.get("/api/status?t=wrong")
    assert r2.status == 401


async def test_token_via_header(client):
    r = await client.get("/api/status", headers={"X-Dash-Token": TOKEN})
    assert r.status == 200
    data = await r.json()
    assert data["telegram"]["ok"] is True


# ── разделы API ────────────────────────────────────────────────────────────────────
async def test_status_section(client, state):
    data = await (await client.get(_q("/api/status"))).json()
    assert data["bot_name"] == "Оракул"
    assert data["telegram"]["username"] == "baltia_oracle_bot"
    assert data["cost"]["today"] == pytest.approx(0.0123)


async def test_all_sections_return_fake_data(client, state):
    checks = {
        "dialog": lambda d: d[0]["role"] == "user",
        "memory": lambda d: d[0]["content"].startswith("Любит"),
        "opinions": lambda d: d[0]["topic"] == "утренние совещания",
        "journal": lambda d: "день" in d[0]["content"].lower(),
        "reminders": lambda d: d[0]["text"] == "Позвонить в банк",
        "ideas": lambda d: d[0]["title"].startswith("Мини"),
        "projects": lambda d: d[0]["tasks"][0]["text"] == "выбрать плитку",
        "news": lambda d: d[0]["source"] == "РБК",
    }
    for section, ok in checks.items():
        r = await client.get(_q("/api/" + section))
        assert r.status == 200, section
        assert ok(await r.json()), section


async def test_usage_section_days_param(client):
    data = await (await client.get(_q("/api/usage?days=7"))).json()
    assert data["days"] == 7
    assert data["total_cost"] == pytest.approx(0.42)
    assert data["by_day"][0]["day"] == "2026-09-28"


async def test_logs_section_after_id_and_level(client):
    allrows = await (await client.get(_q("/api/logs?level=DEBUG"))).json()
    assert len(allrows) == 2
    # фильтр по уровню
    warn = await (await client.get(_q("/api/logs?level=WARNING"))).json()
    assert [r["level"] for r in warn] == ["WARNING"]
    # after_id отдаёт только новое
    after = await (await client.get(_q("/api/logs?after_id=1&level=DEBUG"))).json()
    assert [r["id"] for r in after] == [2]


async def test_dialog_limit_param(client, state):
    state._dialog = [{"role": "user", "content": f"m{i}", "ts": i} for i in range(10)]
    data = await (await client.get(_q("/api/dialog?limit=3"))).json()
    assert len(data) == 3 and data[-1]["content"] == "m9"


async def test_unknown_section_404(client):
    r = await client.get(_q("/api/nope"))
    assert r.status == 404


async def test_settings_get_shape(client):
    data = await (await client.get(_q("/api/settings"))).json()
    assert {"sections", "settings", "values"} <= set(data)
    assert data["sections"][0]["name"] == "Telegram"
    assert "BOT_TOKEN" in data["values"]
    assert "…" in data["values"]["BOT_TOKEN"]          # секрет замаскирован


# ── сохранение настроек ────────────────────────────────────────────────────────────
async def test_settings_post_valid_reports_changed(client, state):
    form = {"BOT_NAME": "Новое имя", "DASHBOARD_PORT": "9000", "BOT_TOKEN": "sk-…c4d5"}
    r = await client.post(_q("/api/settings"), json=form)
    assert r.status == 200
    res = await r.json()
    assert res["ok"] is True
    assert set(res["changed"]) == {"BOT_NAME", "DASHBOARD_PORT"}   # маска-секрет пропущена
    assert res["restart_needed"] is True
    assert state.saved and set(state.saved[-1]) == {"BOT_NAME", "DASHBOARD_PORT"}


async def test_settings_post_invalid_reports_errors(client, state):
    r = await client.post(_q("/api/settings"), json={"LLM_TEMPERATURE": "999", "DASHBOARD_PORT": "-1"})
    res = await r.json()
    assert res["ok"] is False
    assert set(res["errors"]) == {"LLM_TEMPERATURE", "DASHBOARD_PORT"}
    assert res["changed"] == []
    assert state.saved == []                           # ничего не сохранили


async def test_settings_post_rejects_non_object(client):
    r = await client.post(_q("/api/settings"), data="not-json",
                          headers={"Content-Type": "text/plain"})
    assert r.status == 400


# ── действия ────────────────────────────────────────────────────────────────────────
async def test_action_reaches_state(client, state):
    r = await client.post(_q("/api/action"), json={"name": "clear_conflict"})
    assert r.status == 200
    assert (await r.json())["ok"] is True
    assert state.actions == [("clear_conflict", {})]


async def test_action_with_params(client, state):
    await client.post(_q("/api/action"), json={"name": "run_brief", "params": {"deep": True}})
    assert state.actions[-1] == ("run_brief", {"deep": True})


async def test_action_missing_name_400(client):
    r = await client.post(_q("/api/action"), json={"params": {}})
    assert r.status == 400


async def test_action_error_becomes_500(client, state):
    async def boom(name, params=None):
        raise RuntimeError("bang")
    state.action = boom
    r = await client.post(_q("/api/action"), json={"name": "x"})
    assert r.status == 500


# ── запуск сервера ─────────────────────────────────────────────────────────────────
async def test_start_dashboard_binds_localhost(monkeypatch):
    monkeypatch.setattr(srv, "_is_headless", lambda: True)      # не открывать браузер в тесте
    dash = await start_dashboard(FakeState(), port=0, open_browser=True)
    try:
        assert dash.url.startswith("http://127.0.0.1:")
        assert dash.token in dash.url
        assert dash.host == "127.0.0.1"
        assert dash.port > 0
        async with aiohttp.ClientSession() as s:
            async with s.get(dash.url) as r:               # с токеном — страница
                assert r.status == 200
                assert "Оракул" in await r.text()
            base = dash.url.split("?")[0]
            async with s.get(base + "api/status") as r2:   # без токена — 401
                assert r2.status == 401
    finally:
        await dash.stop()


async def test_start_dashboard_open_browser_disabled(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(srv, "_safe_open", lambda u: called.append(u))
    monkeypatch.setattr(srv, "_is_headless", lambda: False)
    dash = await start_dashboard(FakeState(), port=0, open_browser=False)
    await asyncio.sleep(0.05)
    await dash.stop()
    assert called == []


async def test_start_dashboard_open_browser_thread(monkeypatch):
    import threading
    done = threading.Event()
    seen: list[str] = []

    def rec(u):
        seen.append(u)
        done.set()

    monkeypatch.setattr(srv, "_safe_open", rec)
    monkeypatch.setattr(srv, "_is_headless", lambda: False)
    dash = await start_dashboard(FakeState(), port=0, open_browser=True)
    try:
        assert done.wait(2.0)
        assert seen == [dash.url]
    finally:
        await dash.stop()


# ── контракт ────────────────────────────────────────────────────────────────────────
def test_fakestate_matches_protocol():
    assert isinstance(FakeState(), DashboardState)
