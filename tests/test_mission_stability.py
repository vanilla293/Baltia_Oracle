"""Offline lifecycle regressions: stop/panic must win over asynchronous work."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend import bus, council, explain, mission, trader_broker, watch


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    saved = {}
    monkeypatch.setattr(mission, "_M", {})
    monkeypatch.setattr(mission, "_panic_refused", {})   # W4: «повторная ПАНИКА = force» не переживает тест
    monkeypatch.setattr(mission.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mission, "_last_resume_ts", 0)
    monkeypatch.setattr(bus, "_RUNS", {})
    monkeypatch.setattr(bus, "_sink", None)
    monkeypatch.setattr(mission, "explain", None)
    monkeypatch.setattr(mission, "_scan_stop_quiet", Mock())
    monkeypatch.setattr(mission, "_scan_start", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(mission.store_v5, "mission_put", lambda ticker, data: saved.update({ticker: data}))
    monkeypatch.setattr(mission.store_v5, "mission_get", lambda ticker: saved.get(ticker))
    monkeypatch.setattr(mission.store_v5, "mission_all", lambda: dict(saved))
    monkeypatch.setattr(mission.tinkoff, "enabled", lambda: True)
    monkeypatch.setattr(mission, "_flat_account", AsyncMock(return_value={"ok": True, "note": "closed"}))
    return saved


@pytest.mark.parametrize("action", ["stop", "panic"])
@pytest.mark.parametrize("started", [False, True])
def test_stop_and_panic_cancel_council_and_release_run(monkeypatch, action, started):
    entered = asyncio.Event()

    async def blocked_scan(m):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(mission, "_scan_start", blocked_scan)

    async def scenario():
        result = await mission.start("SBER", "auto")
        m = mission._M["SBER"]
        if started:
            await entered.wait()
        stopped = await getattr(mission, action)("SBER")
        assert stopped["ok"]
        assert m.council_task.cancelled()
        assert m.pilot is None
        assert not m.auto_resume
        assert m.phase == ("panic" if action == "panic" else "stopped")
        assert bus.run(result["run_id"])["status"] == "cancelled"
        assert bus.active("mission") is None
        assert not (await mission.council_again("SBER", "late pilot callback"))["ok"]

    asyncio.run(scenario())


def test_waiting_council_owns_task_instead_of_borrowing_caller(monkeypatch):
    m = mission.Mission("SBER", "Sber", "share", "auto")
    mission._M[m.ticker] = m

    async def done(m, reason, first):
        bus.end_run(m.run_id)
        return {"do": "BUY"}

    monkeypatch.setattr(mission, "_council", done)

    async def scenario():
        assert (await mission.council_again("SBER", "first", wait=True))["ok"]
        assert m.council_task is not asyncio.current_task()
        assert not m.council_running()
        assert (await mission.council_again("SBER", "second", wait=True))["ok"]

    asyncio.run(scenario())


def test_late_ai_answer_after_cancellation_cannot_launch_pilot(monkeypatch):
    # 5.4.2: приказ уходит пилоту ДО рамки (рамка — фоном), поэтому «поздний ответ ИИ» — это шифровальщик (exec)
    entered = asyncio.Event()

    async def late_exec(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Some adapters finish returning a buffered response during cancel.
            return {"do": "BUY", "entry": 99, "take": 103, "invalidation": 98}

    async def frame(*args, **kwargs):
        return {"headline": "frame"}

    async def fit(blocks):
        return blocks, []

    pilot = Mock()
    monkeypatch.setattr(mission, "MissionPilot", pilot)
    monkeypatch.setattr(mission, "market_clock", None)
    monkeypatch.setattr(mission.market_ctx, "build", AsyncMock(return_value={"price": 100, "text": "offline"}))
    monkeypatch.setattr(mission, "_account_position_text", AsyncMock(return_value=""))
    monkeypatch.setattr(mission, "_partners_block", AsyncMock(return_value=""))
    monkeypatch.setattr(mission, "_news_for", AsyncMock(return_value=([], "")))
    monkeypatch.setattr(mission, "_scan_text", lambda m: "")
    monkeypatch.setattr(mission, "_council_text", lambda: "")
    monkeypatch.setattr(mission, "_watch_text", lambda since: "")
    monkeypatch.setattr(mission.scout, "run", AsyncMock(return_value=("", [])))
    monkeypatch.setattr(mission, "_fit_blocks", fit)
    monkeypatch.setattr(mission, "_shrink_one", AsyncMock(side_effect=lambda text, label: text))
    monkeypatch.setattr(mission, "_stream_stage", AsyncMock(return_value="offline analysis"))
    monkeypatch.setattr(mission.ai_v5, "pro_json", late_exec)
    monkeypatch.setattr(mission, "_mod", lambda name: SimpleNamespace(present_frame=frame))

    async def scenario():
        await mission.start("SBER", "auto")
        await asyncio.wait_for(entered.wait(), 10)
        await mission.stop("SBER")
        m = mission._M["SBER"]
        assert m.council_task.cancelled()
        assert m.exec is None
        assert m.phase == "stopped"
        pilot.assert_not_called()

    asyncio.run(scenario())


def test_old_mission_cannot_start_second_pilot_via_reanalysis(monkeypatch):
    async def scenario():
        current = mission.Mission("SBER", "Sber", "share", "auto")
        old = mission.Mission("GAZP", "Gazprom", "share", "auto")
        mission._M.update(SBER=current, GAZP=old)
        current.task = asyncio.create_task(asyncio.Event().wait())
        try:
            result = await mission.council_again("GAZP", "button")
            assert not result["ok"]
            assert old.council_task is None
        finally:
            current.task.cancel()
            await asyncio.gather(current.task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("reload", [False, True])
def test_watchdog_respects_manual_stop_across_restart(monkeypatch, reload):
    m = mission.Mission("SBER", "Sber", "share", "auto")
    mission._M[m.ticker] = m
    monkeypatch.setattr(mission, "_state_file_position", lambda: {"base": "SBER", "side": "long", "lots": 3})
    resume = AsyncMock()
    monkeypatch.setattr(mission, "resume", resume)

    async def scenario():
        await mission.stop("SBER")
        if reload:
            mission._M.clear()
        await mission._watchdog_tick()
        resume.assert_not_awaited()

    asyncio.run(scenario())


def test_watchdog_still_recovers_unexpected_pilot_failure(monkeypatch):
    m = mission.Mission("SBER", "Sber", "share", "auto")
    m.phase = "error"
    mission._M[m.ticker] = m
    monkeypatch.setattr(mission, "_state_file_position", lambda: {"base": "SBER", "side": "long", "lots": 3})
    resume = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(mission, "resume", resume)
    asyncio.run(mission._watchdog_tick())
    resume.assert_awaited_once_with("SBER")


def test_panic_targets_absent_ticker_even_with_other_missions(monkeypatch):
    mission._M["SBER"] = mission.Mission("SBER", "Sber", "share", "auto")
    close = AsyncMock(return_value={"ok": False, "note": "broker rejected"})
    monkeypatch.setattr(mission, "_flat_account", close)
    result = asyncio.run(mission.panic("GAZP"))
    assert not result["ok"]
    assert result["failed"] == ["GAZP"]
    assert close.await_args.args[0].ticker == "GAZP"
    assert mission._M["SBER"].auto_resume
    assert not mission._M["GAZP"].auto_resume
    assert mission._M["GAZP"].error == "broker rejected"


def test_duplicate_panic_closes_once_and_blocks_new_trading(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def close(m):
        calls.append(m.ticker)
        entered.set()
        await release.wait()
        return {"ok": True, "note": "closed"}

    monkeypatch.setattr(mission, "_flat_account", close)

    async def scenario():
        first = asyncio.create_task(mission.panic("SBER"))
        await entered.wait()
        second = asyncio.create_task(mission.panic("SBER"))
        await asyncio.sleep(0)
        assert not (await mission.start("GAZP", "auto"))["ok"]
        assert not (await mission.resume("SBER"))["ok"]
        assert (await mission.stop("SBER"))["ok"]
        assert mission._M["SBER"].phase == "panic"
        release.set()
        assert all(r["ok"] for r in await asyncio.gather(first, second))
        assert calls == ["SBER"]

    asyncio.run(scenario())


def test_shutdown_cancels_council_without_setting_manual_stop(monkeypatch):
    async def scenario():
        await mission.start("SBER", "auto")
        m = mission._M["SBER"]
        await mission.shutdown_tasks()
        assert m.council_task.cancelled()
        assert m.auto_resume
        assert bus.active("mission") is None

    asyncio.run(scenario())


def test_disconnected_panic_caller_does_not_lose_closure_or_result(monkeypatch, isolated_state):
    entered, release = asyncio.Event(), asyncio.Event()

    async def close(m):
        entered.set()
        await release.wait()
        return {"ok": False, "note": "broker rejected"}

    monkeypatch.setattr(mission, "_flat_account", close)

    async def scenario():
        caller = asyncio.create_task(mission.panic("SBER"))
        await entered.wait()
        m = mission._M["SBER"]
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert m.live()
        assert not m.panic_task.cancelled()
        release.set()
        await m.panic_task
        assert m.phase == "error"
        assert isolated_state["SBER"]["error"] == "broker rejected"
        assert not m.auto_resume

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["daily", "update", "human", "watch"])
def test_cancelled_council_and_watch_release_busy_registry(monkeypatch, kind):
    entered = asyncio.Event()
    saved = Mock()
    monkeypatch.setattr(council.store_v5, "council_put", saved)
    monkeypatch.setattr(council.store_v5, "kv_set", Mock())
    monkeypatch.setattr(council, "latest", lambda: None)

    async def blocked_stage(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(bus, "stage", blocked_stage)

    async def scenario():
        work = {"daily": lambda: council.daily(), "update": lambda: council.update_with_news([], "test"),
                "human": lambda: council.human("test"), "watch": lambda: watch.tick()}
        task = asyncio.create_task(work[kind]())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not bus.running()
        assert bus.recent()[0]["status"] == "cancelled"
        assert council._busy() is None
        if kind != "watch":
            assert saved.call_args.kwargs["status"] == "cancelled"

    asyncio.run(scenario())


def test_watch_tick_does_not_overlap_existing_tick(monkeypatch):
    collect = AsyncMock()
    monkeypatch.setattr(watch.newsflow, "collect", collect)
    bus.start_run("watch")
    assert asyncio.run(watch.tick(force=True)) is None
    collect.assert_not_awaited()


def test_stale_pilot_done_callback_cannot_overwrite_replacement(monkeypatch):
    m = mission.Mission("SBER", "Sber", "share", "auto")
    mission._M[m.ticker] = m

    async def scenario():
        old_task = asyncio.create_task(asyncio.sleep(0))
        await old_task
        m.task = asyncio.create_task(asyncio.Event().wait())
        m.phase = "armed"
        try:
            mission._pilot_done(m, old_task)
            assert m.phase == "armed"
        finally:
            m.task.cancel()
            await asyncio.gather(m.task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("result", [
    {"ok": True, "status": "EXECUTION_REPORT_STATUS_NEW", "order_id": "exchange-1"},
    {"ok": True, "status": "EXECUTION_REPORT_STATUS_PARTIALLYFILL", "exec_lots": 1, "order_id": "exchange-1"},
    {"ok": False, "uncertain": True, "id_type": "request", "order_id": "request-1", "request_id": "request-1"},
])
def test_direct_panic_keeps_accepted_partial_or_unknown_orders(monkeypatch, result):
    b = trader_broker.Broker("dry")
    b.mode, b.account_id = "real", "account-1"
    monkeypatch.setattr(trader_broker, "_audit", Mock())
    monkeypatch.setattr(b, "portfolio", AsyncMock(return_value={"positions": [
        {"figi": "FIGI", "type": "futures", "qty": 3}]}))
    monkeypatch.setattr(b, "stop_orders", AsyncMock(return_value=[]))
    monkeypatch.setattr(b, "place", AsyncMock(return_value=result))
    r = asyncio.run(b.flat_all(figi="FIGI"))
    assert r["closed"] == 0
    assert len(r["pending"]) == 1
    assert r["pending"][0]["order_id"] == result["order_id"]
    assert r["pending"][0]["account_id"] == "account-1"


def test_direct_panic_does_not_double_sell_if_stop_cancellation_failed(monkeypatch):
    b = trader_broker.Broker("dry")
    b.mode, b.account_id = "real", "account-1"
    monkeypatch.setattr(trader_broker, "_audit", Mock())
    monkeypatch.setattr(b, "portfolio", AsyncMock(return_value={"positions": [
        {"figi": "FIGI", "type": "futures", "qty": 3}]}))
    monkeypatch.setattr(b, "stop_orders", AsyncMock(return_value=[{"figi": "FIGI", "stopOrderId": "STOP"}]))
    monkeypatch.setattr(b, "cancel_stop", AsyncMock(return_value={"ok": False}))
    place = AsyncMock()
    monkeypatch.setattr(b, "place", place)
    r = asyncio.run(b.flat_all(figi="FIGI"))
    assert not r["ok"]
    assert r["closed"] == 0
    place.assert_not_awaited()


def test_direct_panic_does_not_trade_when_stop_list_is_unknown(monkeypatch):
    b = trader_broker.Broker("dry")
    b.mode, b.account_id = "real", "account-1"
    monkeypatch.setattr(trader_broker, "_audit", Mock())
    monkeypatch.setattr(b, "portfolio", AsyncMock(return_value={"positions": [
        {"figi": "FIGI", "type": "futures", "qty": 3}]}))
    monkeypatch.setattr(b, "_post", AsyncMock(side_effect=TimeoutError("offline")))
    place = AsyncMock()
    monkeypatch.setattr(b, "place", place)
    result = asyncio.run(b.flat_all(figi="FIGI"))
    assert not result["ok"]
    place.assert_not_awaited()


def _offline_direct_broker(monkeypatch):
    b = trader_broker.Broker("dry")
    b.mode, b.account_id = "real", "account-1"
    monkeypatch.setattr(trader_broker, "_audit", Mock())
    monkeypatch.setattr(mission, "_flat_account", _REAL_FLAT_ACCOUNT)
    monkeypatch.setattr(mission, "_make_broker", lambda: b)
    monkeypatch.setattr(mission.tinkoff, "accounts", AsyncMock(return_value=[{"id": "account-1"}]))
    monkeypatch.setattr(mission.tinkoff, "resolve", AsyncMock(return_value={"figi": "FIGI", "lot": 1}))
    monkeypatch.setattr(b, "portfolio", AsyncMock(return_value={"positions": [
        {"figi": "FIGI", "type": "futures", "qty": 3}]}))
    monkeypatch.setattr(b, "stop_orders", AsyncMock(return_value=[]))
    monkeypatch.setattr(b, "orders", AsyncMock(return_value=[]))   # W4: flat_all снимает активные заявки (GetOrders)
    return b


def test_panic_durable_intent_exists_before_order_io_and_survives_cancel(monkeypatch, isolated_state):
    b = _offline_direct_broker(monkeypatch)
    entered = asyncio.Event()
    request_ids = []
    m = mission.Mission("SBER", "Sber", "share", "auto")

    async def place(*args, **kwargs):
        request_ids.append(kwargs["request_id"])
        assert isolated_state["SBER"]["panic_orders"][0]["request_id"] == request_ids[0]
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(b, "place", place)
    state = AsyncMock(return_value={"ok": True, "filled": True})
    monkeypatch.setattr(b, "order_state", state)

    async def scenario():
        task = asyncio.create_task(mission._flat_account(m))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        restored = mission._from_store("SBER")
        assert restored.panic_orders[0]["order_id"] == request_ids[0]
        result = await mission._flat_account(restored)
        assert result["ok"] and not restored.panic_orders
        assert len(request_ids) == 1
        state.assert_awaited_once_with(request_ids[0], request_id=True)

    asyncio.run(scenario())


def test_panic_does_not_submit_if_intent_cannot_be_persisted(monkeypatch):
    b = _offline_direct_broker(monkeypatch)
    place = AsyncMock()
    monkeypatch.setattr(b, "place", place)
    monkeypatch.setattr(mission.store_v5, "mission_put", Mock(side_effect=OSError("disk full")))
    m = mission.Mission("SBER", "Sber", "share", "auto")
    result = asyncio.run(mission._flat_account(m))
    assert not result["ok"]
    assert not m.panic_orders
    place.assert_not_awaited()


def test_direct_panic_reconciles_saved_order_without_resubmitting(monkeypatch, isolated_state):
    # Exercise the production helper, which the general fixture replaces.
    monkeypatch.setattr(mission, "_flat_account", _REAL_FLAT_ACCOUNT)
    m = mission.Mission("SBER", "Sber", "share", "auto")
    m.auto_resume = False
    m.panic_orders = [{"order_id": "request-1", "request_id": "request-1", "id_type": "request",
                       "account_id": "account-1", "lots": 3, "figi": "FIGI"}]
    mission._persist(m)
    restored = mission._from_store("SBER")
    mission._M["SBER"] = restored
    broker = SimpleNamespace(account_id=None, mode="real", order_state=AsyncMock(side_effect=[
        {"ok": False, "error": "network"},
        {"ok": True, "filled": False, "status": "PARTIALLYFILL", "exec_lots": 1, "order_id": "exchange-1"},
        {"ok": True, "filled": True, "exec_lots": 3, "order_id": "exchange-1"},
    ]), flat_all=AsyncMock())
    monkeypatch.setattr(mission, "_make_broker", lambda: broker)

    async def scenario():
        first = await mission.panic("SBER")
        assert first["pending"] == ["SBER"]
        assert not (await mission.start("GAZP", "auto"))["ok"]
        assert not (await mission.resume("SBER"))["ok"]
        mission._panic_refused.clear()          # V (g): повтор в PANIC_REPEAT_SEC = force; здесь проверяем сверку без force
        second = await mission.panic("SBER")
        assert second["pending"] == ["SBER"]
        assert restored.panic_orders[0]["id_type"] == "exchange"
        assert restored.panic_orders[0]["exec_lots"] == 1
        mission._panic_refused.clear()
        third = await mission.panic("SBER")
        assert third["ok"] and third["pending"] == []
        assert not restored.panic_orders
        assert isolated_state["SBER"]["panic_orders"] == []
        broker.flat_all.assert_not_awaited()
        assert broker.order_state.await_args_list[0].kwargs == {"request_id": True}
        assert broker.order_state.await_args_list[-1].args == ("exchange-1",)

    asyncio.run(scenario())


def _saved_close(ticker="SBER", age=0.0):
    m = mission.Mission(ticker, "Sber", "share", "auto")
    m.auto_resume = False
    m.panic_orders = [{"order_id": "request-1", "request_id": "request-1", "id_type": "request",
                       "account_id": "account-1", "lots": 3, "figi": "FIGI", "mode": "real", "ts": time.time() - age}]
    mission._persist(m)
    mission._M[ticker] = m
    return m


def test_force_panic_drops_saved_unconfirmed_close_and_closes_by_portfolio(monkeypatch, isolated_state):
    # W4 №3: force не ждёт закрытие, которого биржа не подтверждает (сеть): запись сброшена (UUID в note), живая
    # прежняя заявка снята через GetOrders → CancelOrder, позиция закрыта по портфелю; миссия больше не «занята»
    b = _offline_direct_broker(monkeypatch)
    m = _saved_close()
    monkeypatch.setattr(b, "order_state", AsyncMock(return_value={"ok": False, "error": "network"}))
    monkeypatch.setattr(b, "stop_orders", AsyncMock(side_effect=TimeoutError("offline")))   # закрыть может только force
    place = AsyncMock(return_value={"ok": True, "order_id": "exchange-2", "status": "EXECUTION_REPORT_STATUS_FILL", "filled": True})
    monkeypatch.setattr(b, "place", place)
    monkeypatch.setattr(b, "orders", AsyncMock(return_value=[
        {"orderId": "old-close", "figi": "FIGI", "executionReportStatus": "EXECUTION_REPORT_STATUS_NEW"}]))
    cancel = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(b, "cancel", cancel)

    async def scenario():
        waiting = await mission.panic("SBER")
        assert waiting["pending"] == ["SBER"] and m.panic_orders and place.await_count == 0
        assert m.live() and not (await mission.resume("SBER"))["ok"]
        forced = await mission.panic("SBER", force=True)
        assert forced["ok"] and forced["force"] and forced["pending"] == [] and not m.panic_orders
        cancel.assert_awaited_once_with("old-close")
        place.assert_awaited_once()
        assert "сброшены" in forced["note"] and "request-1" in forced["note"] and "не сверены" in forced["note"]
        assert isolated_state["SBER"]["panic_orders"] == [] and not m.live() and mission._active() is None
        assert m.panic_force is False                        # force одноразовый: сторож дальше только опрашивает

    asyncio.run(scenario())


@pytest.mark.parametrize("age", [10.0, 200.0])
def test_not_found_saved_close_is_dropped_only_after_age_then_closed_by_portfolio(monkeypatch, isolated_state, age):
    # W4 №3: биржа не знает закрывающую заявку (по request-UUID) дольше ORDER_REQUEST_MAX_AGE_SEC → фантом сброшен,
    # позиция закрыта по портфелю; свежий «не найдена» — ещё ждём (репликация у брокера)
    b = _offline_direct_broker(monkeypatch)
    m = _saved_close(age=age)
    m.panic_orders[0]["not_found_since"] = time.time() - age   # «не найдена» наблюдается столько же (V, e)
    monkeypatch.setattr(b, "order_state", AsyncMock(return_value={
        "ok": True, "status": "NOT_FOUND", "not_found": True, "filled": False, "exec_lots": 0}))
    place = AsyncMock(return_value={"ok": True, "order_id": "exchange-2", "status": "EXECUTION_REPORT_STATUS_FILL", "filled": True})
    monkeypatch.setattr(b, "place", place)

    async def scenario():
        r = await mission.panic("SBER")
        if age > trader_broker.ORDER_REQUEST_MAX_AGE_SEC:
            assert r["ok"] and r["pending"] == [] and not m.panic_orders and "до биржи не дошли" in r["note"]
            place.assert_awaited_once()
            assert not m.live()
        else:
            assert r["pending"] == ["SBER"] and m.panic_orders and m.panic_orders[0]["order_id"] == "request-1"
            place.assert_not_awaited()

    asyncio.run(scenario())


def test_new_start_after_restart_waits_for_persisted_panic(monkeypatch):
    m = mission.Mission("SBER", "Sber", "share", "auto")
    m.auto_resume = False
    m.panic_orders = [{"order_id": "exchange-1", "account_id": "account-1", "lots": 3}]
    mission._persist(m)
    assert not mission._M
    result = asyncio.run(mission.start("GAZP", "auto"))
    assert not result["ok"]
    assert mission._M["SBER"].panic_orders
    assert "GAZP" not in mission._M


def _break_panic_store(monkeypatch, failure):
    if failure == "list_unreadable":
        monkeypatch.setattr(mission.store_v5, "mission_all", Mock(side_effect=OSError("database locked")))
    else:
        monkeypatch.setattr(mission.store_v5, "mission_all", lambda: {
            "GAZP": {"panic_orders": [{"order_id": "unresolved-close"}]}})
        monkeypatch.setattr(mission.store_v5, "mission_get", Mock(side_effect=OSError("database locked")))


@pytest.mark.parametrize("failure", ["list_unreadable", "mission_unreadable"])
@pytest.mark.parametrize("action", ["start", "resume", "panic", "council"])
def test_unreadable_panic_reservations_block_new_work(monkeypatch, failure, action):
    # W4 (п. 4): нечитаемые аварийные заявки блокируют НОВУЮ работу (start/resume/council) исключением; ПАНИКА без
    # живого пилота — честный отказ (не исключение) с подсказкой «повтори/force», ничего на бирже не тронуто
    _break_panic_store(monkeypatch, failure)
    council_work = AsyncMock()
    pilot = Mock()
    monkeypatch.setattr(mission, "_council", council_work)
    monkeypatch.setattr(mission, "MissionPilot", pilot)

    async def scenario():
        work = {"start": lambda: mission.start("GAZP", "auto"),
                "resume": lambda: mission.resume("GAZP"),
                "council": lambda: mission.council_again("GAZP", "test")}
        if action == "panic":
            result = await mission.panic("GAZP")
            assert not result["ok"] and result["failed"] == ["GAZP"] and not result["force"]
            assert "сохранённые аварийные заявки" in result["note"] and "force" in result["note"]
        else:
            with pytest.raises(RuntimeError, match="сохранённые аварийные заявки"):
                await work[action]()
        assert not mission._M
        assert not bus.running()
        council_work.assert_not_called()
        pilot.assert_not_called()
        mission._flat_account.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["list_unreadable", "mission_unreadable"])
def test_unreadable_panic_reservations_do_not_stop_panic_of_live_pilot(monkeypatch, failure):
    # W4 (п. 4): живой пилот получает panic() ДО чтения базы и независимо от неё — он закрывает через брокера
    _break_panic_store(monkeypatch, failure)
    m = mission.Mission("SBER", "Sber", "share", "auto")
    m.pilot = Mock(panic=Mock(), panic_flag=False, panic_blocked=False, last_action="в позиции")
    mission._M["SBER"] = m

    async def scenario():
        m.task = asyncio.create_task(asyncio.Event().wait())
        try:
            result = await mission.panic("SBER")
            assert result["ok"] and result["panic"] == ["SBER"] and not result["failed"]
            assert "база миссий не читается" in result["note"]
            m.pilot.panic.assert_called_once_with(force=False)
            assert m.phase == "panic" and not m.auto_resume
            mission._flat_account.assert_not_awaited()
            # повторное нажатие, пока пилот упёрся (стопы не сверить) — force для пилота
            m.pilot.panic_flag, m.pilot.panic_blocked = True, True
            again = await mission.panic("SBER")
            assert again["ok"] and m.pilot.panic.call_args.kwargs == {"force": True}
        finally:
            m.task.cancel()
            await asyncio.gather(m.task, return_exceptions=True)

    asyncio.run(scenario())


def test_unreadable_store_panic_without_pilot_repeats_as_force(monkeypatch):
    # W4 (п. 5): после честного отказа повторная ПАНИКА (в окно PANIC_REPEAT_SEC) или force закрывает по счёту напрямую
    monkeypatch.setattr(mission.store_v5, "mission_all", Mock(side_effect=OSError("database locked")))
    close = AsyncMock(return_value={"ok": True, "note": "closed"})
    monkeypatch.setattr(mission, "_flat_account", close)

    async def scenario():
        first = await mission.panic("GAZP")
        assert not first["ok"] and close.await_count == 0
        second = await mission.panic("GAZP")
        assert second["ok"] and second["force"] and second["panic"] == ["GAZP"]
        close.assert_awaited_once()
        assert close.await_args.args[0].panic_force is True
        assert mission._M["GAZP"].phase == "panic"

    asyncio.run(scenario())


def test_unreadable_state_file_panic_refuses_then_force_closes_by_account(monkeypatch):
    (mission.config.DATA_DIR / "aipilot_state.json").write_text("{not json", encoding="utf-8")
    close = AsyncMock(return_value={"ok": True, "note": "closed"})
    monkeypatch.setattr(mission, "_flat_account", close)
    resume = AsyncMock()
    monkeypatch.setattr(mission, "resume", resume)

    async def scenario():
        refused = await mission.panic("SBER")
        assert not refused["ok"] and refused["failed"] == ["SBER"] and "state-файл пилота не читается" in refused["note"]
        close.assert_not_awaited()
        resume.assert_not_awaited()
        forced = await mission.panic("SBER", force=True)
        assert forced["ok"] and forced["force"] and forced["panic"] == ["SBER"]
        close.assert_awaited_once()
        assert close.await_args.args[0].panic_force is True
        # без тикера и без читаемого state-файла — чью заявку закрывать, неизвестно: честный отказ, не 503
        mission._M.clear()
        nobody = await mission.panic(None)
        assert not nobody["ok"] and "назови тикер" in nobody["note"]

    asyncio.run(scenario())


def test_direct_panic_force_cancels_orders_and_closes_when_stop_list_is_unknown(monkeypatch):
    b = trader_broker.Broker("dry")
    b.mode, b.account_id = "real", "account-1"
    monkeypatch.setattr(trader_broker, "_audit", Mock())
    monkeypatch.setattr(b, "portfolio", AsyncMock(return_value={"positions": [
        {"figi": "FIGI", "type": "futures", "qty": 3}]}))
    calls = []

    async def post(path, body):
        name = path.rsplit("/", 1)[-1]
        calls.append(name)
        if name == "GetStopOrders":
            raise TimeoutError("offline")
        if name == "GetOrders":
            return {"orders": [{"orderId": "entry-1", "figi": "FIGI", "executionReportStatus": "EXECUTION_REPORT_STATUS_NEW"},
                               {"orderId": "done-1", "figi": "FIGI", "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL"},
                               {"orderId": "other-1", "figi": "OTHER", "executionReportStatus": "EXECUTION_REPORT_STATUS_NEW"}]}
        if name == "PostOrder":
            return {"orderId": "exchange-1", "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL", "lotsExecuted": 3}
        return {}

    monkeypatch.setattr(b, "_post", post)
    refused = asyncio.run(b.flat_all(figi="FIGI"))
    assert not refused["ok"] and refused["closed"] == 0 and refused["stops_unknown"] and "force" in refused["note"]
    assert "PostOrder" not in calls and "CancelOrder" not in calls
    calls.clear()
    forced = asyncio.run(b.flat_all(figi="FIGI", force=True))
    assert forced["ok"] and forced["closed"] == 1 and forced["orders"] == 1 and forced["stops_unknown"]
    assert calls.count("CancelOrder") == 1 and calls.index("CancelOrder") < calls.index("PostOrder")
    assert "не сверены" in forced["note"]


def test_direct_panic_checks_persist_gate_before_cancelling_stops(monkeypatch):
    # W4 (п. 5): гейт персиста ДО снятия стопов — иначе «стопы сняты, закрытия нет»
    b = trader_broker.Broker("dry")
    b.mode, b.account_id = "real", "account-1"
    monkeypatch.setattr(trader_broker, "_audit", Mock())
    monkeypatch.setattr(b, "portfolio", AsyncMock(return_value={"positions": [
        {"figi": "FIGI", "type": "futures", "qty": 3}]}))
    monkeypatch.setattr(b, "stop_orders", AsyncMock(return_value=[{"figi": "FIGI", "stopOrderId": "STOP"}]))
    monkeypatch.setattr(b, "orders", AsyncMock(return_value=[]))
    cancel_stop = AsyncMock(return_value={"ok": True})
    place = AsyncMock(return_value={"ok": True, "order_id": "x", "status": "FILL", "filled": True})
    monkeypatch.setattr(b, "cancel_stop", cancel_stop)
    monkeypatch.setattr(b, "place", place)
    gate = Mock(side_effect=OSError("disk full"))
    refused = asyncio.run(b.flat_all(figi="FIGI", on_check=gate))
    assert not refused["ok"] and "не сохраняется" in refused["note"]
    cancel_stop.assert_not_awaited()
    place.assert_not_awaited()
    forced = asyncio.run(b.flat_all(figi="FIGI", on_check=gate, force=True))
    assert forced["ok"] and forced["closed"] == 1 and forced["stops"] == 1 and "без персиста" in forced["note"]


def test_dry_mode_cannot_falsely_confirm_a_real_panic_order(monkeypatch):
    b = _offline_direct_broker(monkeypatch)
    b.mode = "dry"
    state = AsyncMock(return_value={"ok": True, "filled": True})
    monkeypatch.setattr(b, "order_state", state)
    m = mission.Mission("SBER", "Sber", "share", "auto")
    m.panic_orders = [{"order_id": "real-order", "account_id": "real-account", "mode": "real"}]
    result = asyncio.run(mission._flat_account(m))
    assert not result["ok"]
    assert m.panic_orders
    state.assert_not_awaited()


@pytest.mark.parametrize("stopped", [True, False])
def test_saved_unfilled_entry_reserves_account_and_is_recovered(monkeypatch, stopped):
    m = mission.Mission("SBER", "Sber", "share", "auto")
    m.auto_resume = not stopped
    mission._persist(m)
    (mission.config.DATA_DIR / "aipilot_state.json").write_text(json.dumps({
        "base": "SBER", "position": None, "pending": {"order_id": "entry-1", "lots": 3}
    }), encoding="utf-8")
    resume = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(mission, "resume", resume)

    async def scenario():
        result = await mission.start("GAZP", "auto")
        assert not result["ok"]
        assert not mission._M
        await mission._watchdog_tick()
        if stopped:
            resume.assert_awaited_once_with("SBER", settle_only=True)
        else:
            resume.assert_awaited_once_with("SBER")

    asyncio.run(scenario())


def test_missing_mission_with_saved_order_is_recovered_only_for_settlement(monkeypatch):
    (mission.config.DATA_DIR / "aipilot_state.json").write_text(json.dumps({
        "base": "SBER", "position": None, "pending": {"order_id": "entry-1", "lots": 3}
    }), encoding="utf-8")
    resume = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(mission, "resume", resume)
    asyncio.run(mission._watchdog_tick())
    resume.assert_awaited_once_with("SBER", settle_only=True)
    assert not mission._M["SBER"].auto_resume


@pytest.mark.parametrize("panic", [False, True])
def test_settlement_recovery_preserves_saved_order_until_real_restore(monkeypatch, panic):
    saved = {"base": "SBER", "figi": "FIGI", "position": None, "account_id": "account-1", "mode": "real",
             "panic": False, "pending": {"order_id": "entry-1", "lots": 3, "price": 100, "side": "long"}}
    path = mission.config.DATA_DIR / "aipilot_state.json"
    path.write_text(json.dumps(saved), encoding="utf-8")
    m = mission.Mission("SBER", "Sber", "share", "auto")
    m.auto_resume = False
    mission._M["SBER"] = m
    entered, release = asyncio.Event(), asyncio.Event()
    b = SimpleNamespace(account_id="account-1", mode="real")
    monkeypatch.setattr(mission, "_make_broker", lambda: b)

    async def prepare(pilot):
        assert json.loads(path.read_text(encoding="utf-8")) == saved
        pilot.figi = "FIGI"
        pilot._restore_state(0)
        assert pilot.pending["order_id"] == "entry-1"
        assert pilot.stopping and pilot._cancel_entry
        assert pilot.panic_flag is panic
        entered.set()
        await release.wait()
        return False  # this case verifies restoration; order settlement is tested by execution tests

    monkeypatch.setattr(mission.MissionPilot, "prepare", prepare)

    async def scenario():
        result = await mission.resume("SBER", settle_only=True, panic_mode=panic)
        assert result["ok"]
        await asyncio.wait_for(entered.wait(), 1)
        assert not m.auto_resume
        release.set()
        await m.task

    asyncio.run(scenario())


@pytest.mark.parametrize("with_ticker", [True, False])
@pytest.mark.parametrize("kind", ["pending", "exit_order", "stop_request"])
def test_panic_recovers_saved_pilot_order_instead_of_submitting_duplicate(monkeypatch, with_ticker, kind):
    saved = {"base": "SBER", "figi": "FIGI", "position": None}
    order = {"order_id": "old-order", "lots": 3}
    if kind == "pending":
        saved["pending"] = order
    else:
        saved["position"] = {"side": "long", "lots": 3, kind: order}
    (mission.config.DATA_DIR / "aipilot_state.json").write_text(json.dumps(saved), encoding="utf-8")
    resume = AsyncMock(return_value={"ok": True, "note": "recovering"})
    monkeypatch.setattr(mission, "resume", resume)
    result = asyncio.run(mission.panic("SBER" if with_ticker else None))
    assert result["ok"] and result["pending"] == ["SBER"]
    resume.assert_awaited_once_with("SBER", settle_only=True, panic_mode=True)
    mission._flat_account.assert_not_awaited()


def test_explanation_arriving_during_ai_response_gets_its_own_flush(monkeypatch):
    m = SimpleNamespace(ticker="SBER", name="Sber", run_id=None, explain=[], memory="")
    first_entered, release_first, second_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def ask(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            first_entered.set()
            await release_first.wait()
        else:
            second_entered.set()
        return "offline explanation"

    monkeypatch.setattr(explain, "_ST", {})
    monkeypatch.setattr(explain, "GATHER_SEC", 0)
    monkeypatch.setattr(explain, "MIN_GAP_SEC", 0)
    monkeypatch.setattr(explain, "enabled", lambda: True)
    monkeypatch.setattr(explain, "_ask", ask)
    monkeypatch.setattr(explain.compress, "fit", AsyncMock(side_effect=lambda blocks: blocks))

    async def scenario():
        try:
            explain.note(m, "entry", "first")
            await first_entered.wait()
            explain.note(m, "review", "second")
            release_first.set()
            await asyncio.wait_for(second_entered.wait(), 1)
            task = explain._state(m)["task"]
            if task:
                await task
            assert [row["title"] for row in m.explain] == ["first", "second"]
            assert not explain.pending(m)
        finally:
            await explain.shutdown_tasks()

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["reset", "shutdown"])
def test_explanation_lifecycle_cancels_both_writing_and_memory(monkeypatch, action):
    m = SimpleNamespace(ticker="SBER", name="Sber", run_id=None, explain=[], memory="",
                        reviews=[{"choice": "wait", "ts": 1}], handoffs=[], pilot=None)
    explain_entered, memory_entered = asyncio.Event(), asyncio.Event()

    async def write(*args, **kwargs):
        explain_entered.set()
        await asyncio.Event().wait()

    async def memory(*args, **kwargs):
        memory_entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(explain, "_ST", {})
    monkeypatch.setattr(explain, "GATHER_SEC", 0)
    monkeypatch.setattr(explain, "enabled", lambda: True)
    monkeypatch.setattr(explain, "_ask", write)
    monkeypatch.setattr(explain.ai_v5, "flash_text", memory)
    monkeypatch.setattr(explain.compress, "fit", AsyncMock(side_effect=lambda blocks: blocks))
    monkeypatch.setattr(explain.compress, "shrink", AsyncMock(side_effect=lambda text, **kwargs: text))
    persist = Mock()

    async def scenario():
        explain.note(m, "entry", "first", persist=persist)
        explain.memorize_bg(m, "test", persist=persist)
        await asyncio.gather(explain_entered.wait(), memory_entered.wait())
        st = explain._state(m)
        tasks = [st["task"], st["mem_task"]]
        if action == "reset":
            explain.reset("SBER")
        else:
            await explain.shutdown_tasks()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert all(task.cancelled() for task in tasks)
        assert m.memory == "" and not m.explain
        persist.assert_not_called()

    asyncio.run(scenario())


_REAL_FLAT_ACCOUNT = mission._flat_account
