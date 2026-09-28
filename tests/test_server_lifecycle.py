"""Server lifecycle verification with every network producer replaced by a fake."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend import api_chat, api_council, ledger, mission, news, server, telegram, underlying, watch, weather


def test_startup_tasks_are_stopped_before_http_clients_close(monkeypatch):
    events = []
    expected = {"instruments", "astro", "prewarm", "telegram", "watch", "mission", "ledger"}

    async def worker(name):
        events.append(("start", name))
        try:
            await asyncio.Event().wait()
        finally:
            events.append(("stop", name))

    for module, function, name in (
        (server.instruments, "fetch_universe", "instruments"),
        (server.astro, "acontext", "astro"),
        (server.tinkoff, "prewarm", "prewarm"),
        (telegram, "loop", "telegram"), (watch, "loop", "watch"),
        (mission, "watchdog", "mission"), (ledger, "loop", "ledger"),
    ):
        monkeypatch.setattr(module, function, lambda name=name: worker(name))

    async def close(name):
        events.append(("close", name))

    for module in (telegram, server.ai, server.tinkoff, server.moex, server.instruments, news, weather, underlying):
        monkeypatch.setattr(module, "aclose", lambda name=module.__name__: close(name))
    monkeypatch.setattr(telegram, "flush_batches", AsyncMock())
    monkeypatch.setattr(telegram, "wrap_sink", lambda sink: sink)
    monkeypatch.setattr(server.tinkoff, "enabled", lambda: True)
    monkeypatch.setattr(ledger, "mode", lambda: "broker")
    monkeypatch.setattr(server, "_lan_ips", lambda: [])
    monkeypatch.setattr(server, "_BACKGROUND_TASKS", set())
    monkeypatch.setattr(server.bus, "_sink", None)
    for flag in ("RESET_ON_START", "WIPE_KEYS_ON_START", "WIPE_ON_EXIT"):
        monkeypatch.setattr(server.config, flag, False)
    monkeypatch.delenv("PYTHIA_MOCK_AI", raising=False)
    monkeypatch.delenv("PYTHIA_LEGACY_PILOT", raising=False)

    async def scenario():
        try:
            await server._startup()
            await asyncio.sleep(0)
            assert {name for event, name in events if event == "start"} == expected
            assert len(server._BACKGROUND_TASKS) == len(expected)
            await server._shutdown()
            first_close = next(i for i, (event, _) in enumerate(events) if event == "close")
            assert {name for event, name in events[:first_close] if event == "stop"} == expected
            assert not server._BACKGROUND_TASKS
        finally:
            await server._stop_background()

    asyncio.run(scenario())


def test_user_work_and_emergency_result_finish_before_client_teardown(monkeypatch):
    events = []
    monkeypatch.setattr(server, "_AUTO", {})
    monkeypatch.setattr(server, "_AIPILOT", {})
    monkeypatch.setattr(api_chat, "_STATE", {"task": None, "run_id": None})
    monkeypatch.setattr(mission, "shutdown_tasks", AsyncMock())

    async def worker(name):
        try:
            await asyncio.Event().wait()
        finally:
            events.append(name)

    async def finish_close():
        await asyncio.sleep(0)
        events.append("panic result persisted")

    async def scenario():
        council = asyncio.create_task(worker("council stopped"))
        chat = asyncio.create_task(worker("chat stopped"))
        pilot = asyncio.create_task(worker("pilot stopped"))
        scan = asyncio.create_task(worker("scanner stopped"))
        panic = asyncio.create_task(finish_close())
        monkeypatch.setattr(api_council, "_TASKS", {council})
        api_chat._STATE["task"] = chat
        monkeypatch.setattr(mission, "_M", {"TEST": SimpleNamespace(task=pilot, panic_task=panic)})
        monkeypatch.setattr(server.maya_scan, "_SCANS", {"TEST": {"task": scan}})
        await asyncio.sleep(0)
        await server._stop_owned_work()
        events.append("clients may close")
        assert all(t.done() for t in (council, chat, pilot, scan, panic))
        assert not panic.cancelled()
        assert set(events[:-1]) == {"council stopped", "chat stopped", "pilot stopped",
                                  "scanner stopped", "panic result persisted"}
        mission.shutdown_tasks.assert_awaited_once()

    asyncio.run(scenario())
