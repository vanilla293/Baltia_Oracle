"""Offline regressions for order ownership, uncertain cancellation, and journal fills."""
import asyncio
import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import httpx

from backend import ai_pilot, config, ledger, trader_broker


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def no_network(*args, **kwargs):
        raise AssertionError("execution regressions must never contact a broker")

    monkeypatch.setattr(trader_broker.tinkoff, "_post", no_network)
    monkeypatch.setattr(trader_broker, "_audit", lambda *args, **kwargs: None)
    # v5.4.1: умолчание — стопы только в программе (PYTHIA_EXCHANGE_STOP=0); эти регрессии проверяют механику
    # биржевого троса (UUID стопа, разрешитель по списку, отмена перед закрытием) — включаем его на время теста
    monkeypatch.setattr(config, "PYTHIA_EXCHANGE_STOP", True)


def make_pilot():
    broker = SimpleNamespace(
        mode="real",
        order_state=AsyncMock(return_value={"ok": True, "status": "NEW", "exec_lots": 0}),
        cancel=AsyncMock(return_value={"ok": False, "error": "timeout"}),
        place=AsyncMock(return_value={"ok": True, "order_id": "replacement"}),
        cancel_stop=AsyncMock(return_value={"ok": False, "error": "timeout"}),
        place_stop=AsyncMock(return_value={"ok": True, "stop_order_id": "new-stop"}),
    )
    pilot = ai_pilot.AIPilot("TEST", broker=broker)
    pilot._state_path = None
    pilot.figi = "TEST-FIGI"
    pilot.plan = {"side": "long", "entry": 100.0, "invalidation": 90.0, "take": 120.0}
    pilot.pending = {"order_id": "pending-1", "side": "long", "lots": 5,
                     "price": 100.0, "ts": 0.0, "attempts": 0,
                     "take": 120.0, "invalidation": 90.0}
    return pilot


def test_timeout_during_entry_cancel_does_not_reprice_or_forget_order():
    async def scenario():
        pilot = make_pilot()
        original = pilot.pending
        pilot._enter = AsyncMock()
        await pilot._pending_tick(100.0, {})
        assert pilot.pending is original
        assert pilot._cancel_entry
        pilot._enter.assert_not_awaited()
        pilot.broker.place.assert_not_awaited()
        pilot.broker.order_state.return_value = {"ok": True, "filled": True, "exec_lots": 5}
        await pilot._pending_tick(100.0, {})
        assert pilot.pending is None
        assert pilot.position["lots"] == 5
        assert not pilot._cancel_entry
        pilot.broker.place_stop.assert_awaited_once()

    asyncio.run(scenario())


def test_successful_cancel_with_unknown_final_fills_retains_order():
    async def scenario():
        pilot = make_pilot()
        original = pilot.pending
        pilot.broker.cancel.return_value = {"ok": True}
        pilot.broker.order_state.side_effect = [
            {"ok": True, "exec_lots": 1}, {"ok": False, "error": "timeout"},
            {"ok": True, "exec_lots": 3, "status": "CANCELLED"},
            {"ok": True, "exec_lots": 3, "status": "CANCELLED"},
        ]
        assert await pilot._cancel_pending() == (0, None)
        assert pilot.pending is original
        # The next poll discovers fills that arrived during cancellation.
        assert await pilot._cancel_pending() == (3, original)
        assert pilot.pending is None

    asyncio.run(scenario())


def test_cancel_retry_never_forgets_previously_reported_fills():
    async def scenario():
        pilot = make_pilot()
        original = pilot.pending
        pilot.broker.order_state.side_effect = [
            {"ok": True, "exec_lots": 2}, {"ok": False, "error": "timeout"},
            {"ok": True, "status": "CANCELLED"}, {"ok": True, "status": "CANCELLED"},
        ]
        assert await pilot._cancel_pending() == (0, None)
        assert await pilot._cancel_pending() == (2, original)
        assert pilot.pending is None

    asyncio.run(scenario())


def test_close_instruction_survives_uncertain_cancel():
    async def scenario():
        pilot = make_pilot()
        pilot._cancel_entry = True
        pilot._close_pending = "close requested"
        await pilot._pending_tick(100.0, {})
        assert pilot.pending
        assert pilot._close_pending == "close requested"

    asyncio.run(scenario())


def test_closed_market_retries_cancel_on_later_ticks():
    async def scenario():
        pilot = make_pilot()
        pilot._tick_n = 1
        market = {"open": False, "next_open_in_s": 300}
        await pilot._closed_tick(100.0, market, first=True)
        assert pilot.pending
        assert "подтверждение отмены" in pilot.last_action
        pilot.broker.order_state.return_value = {"ok": True, "status": "CANCELLED", "exec_lots": 0}
        await pilot._closed_tick(100.0, market, first=False)
        assert pilot.pending is None
        assert pilot.broker.cancel.await_count == 2

    asyncio.run(scenario())


def test_panic_keeps_waiting_for_uncertain_entry_before_marking_stopped():
    async def scenario():
        pilot = make_pilot()
        pilot.panic_flag = True
        await pilot.tick(100.0)
        assert pilot.pending
        assert pilot.state != "СТОП"
        assert "жду подтверждения" in pilot.last_action

    asyncio.run(scenario())


def test_stop_replacement_does_not_duplicate_unconfirmed_old_stop():
    async def scenario():
        pilot = make_pilot()
        position = {"side": "long", "lots": 5, "entry": 100.0,
                    "invalidation": 90.0, "stop_id": "old-stop", "restop": True}
        pilot.position = position
        await pilot._replace_stop(position)
        assert position["stop_id"] == "old-stop"
        assert position["restop"] and position["restop_after"] > 0
        pilot.broker.place_stop.assert_not_awaited()
        pilot.broker.cancel_stop.return_value = {"ok": True}
        await pilot._replace_stop(position)
        assert position["stop_id"] == "new-stop"
        assert "restop" not in position
        pilot.broker.place_stop.assert_awaited_once()

    asyncio.run(scenario())


def test_broker_uses_unique_uuid_request_ids_and_returns_exchange_id(monkeypatch):
    async def scenario():
        post = AsyncMock(return_value={"orderId": "exchange-123", "executionReportStatus": "NEW"})
        broker = trader_broker.Broker("sandbox", account_id="offline", poster=post)
        monkeypatch.setattr(trader_broker, "_now", lambda: 1000.0)
        first, second = await asyncio.gather(*[
            broker.place("TEST", trader_broker.BUY, 1, 100.0, tag="same") for _ in range(2)
        ])
        request_ids = [call.args[1]["orderId"] for call in post.await_args_list]
        assert request_ids[0] != request_ids[1]
        assert all(str(uuid.UUID(value)) == value for value in request_ids)
        assert first["order_id"] == second["order_id"] == "exchange-123"
        assert {first["request_id"], second["request_id"]} == set(request_ids)

    asyncio.run(scenario())


@pytest.mark.parametrize("lots, price", [
    (True, 100), (1.5, 100), (float("inf"), 100), (float("nan"), 100),
    (1, float("inf")), (1, float("nan")), (1, 0), (1, -100), (1, True), (1, 1e300), (2 ** 63, 100),
])
def test_invalid_broker_numbers_never_send_or_simulate_fills(lots, price):
    async def scenario():
        post = AsyncMock()
        broker = trader_broker.Broker("sandbox", account_id="offline", poster=post)
        assert not (await broker.place("TEST", trader_broker.BUY, lots, price))["ok"]
        assert not (await broker.place_stop("TEST", trader_broker.SELL, lots, price))["ok"]
        broker.mode = "dry"
        assert not (await broker.place("TEST", trader_broker.BUY, lots, price))["ok"]
        post.assert_not_awaited()

    asyncio.run(scenario())


def test_quotation_rounding_carries_whole_unit():
    assert trader_broker._to_quotation(1.9999999999) == {"units": 2, "nano": 0}
    assert trader_broker._to_quotation(-1.9999999999) == {"units": -2, "nano": 0}


def test_cancelled_pilot_waits_for_background_work_to_finish():
    async def scenario():
        pilot = make_pilot()
        ready, cancelled = asyncio.Event(), asyncio.Event()

        async def child():
            ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cancelled.set()

        async def loop():
            pilot._spawn_background(child())
            await asyncio.Event().wait()

        pilot._run_loop = loop
        running = asyncio.create_task(pilot.run())
        await ready.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert cancelled.is_set()
        assert not pilot._background_tasks

    asyncio.run(scenario())


def test_stop_cancels_pending_ai_result_before_it_can_apply():
    async def scenario():
        pilot = make_pilot()
        ready, result = asyncio.Event(), asyncio.Event()
        apply = Mock()

        async def review():
            ready.set()
            await result.wait()
            apply()

        pilot._spawn_background(review())
        await ready.wait()
        pilot.stop()
        result.set()
        await pilot._cancel_background()
        apply.assert_not_called()
        assert not pilot._background_tasks

    asyncio.run(scenario())


def test_partial_broker_journal_stays_reconcilable_until_all_units_arrive(monkeypatch):
    trade = {"id": 1, "ticker": "TEST", "side": "long", "lots": 20,
             "entry": 100.0, "exit_px": 115.0, "opened_ts": 1000.0,
             "closed_ts": 1100.0, "source": "est", "data": {"lot": 1}, "ops": []}
    operations = [
        {"id": "buy", "kind": "buy", "qty": 20, "price": 100.0,
         "ts": 1001.0, "payment": -2000.0, "figi": "TEST-FIGI"},
        {"id": "sell1", "kind": "sell", "qty": 10, "price": 110.0,
         "ts": 1099.0, "payment": 1100.0, "figi": "TEST-FIGI"},
    ]
    monkeypatch.setattr(ledger, "mode", lambda: "broker")
    monkeypatch.setattr(ledger, "_keys", AsyncMock(return_value=["TEST-FIGI"]))
    monkeypatch.setattr(ledger, "_horizon", lambda *args: None)
    monkeypatch.setattr(ledger.store_v5, "trades_full", lambda *args, **kwargs: [trade])
    monkeypatch.setattr(ledger.store_v5, "ops_list", lambda *args, **kwargs: operations)
    update = Mock(side_effect=lambda tid, changes: trade.update(changes))
    monkeypatch.setattr(ledger.store_v5, "trade_update", update)

    async def scenario():
        result = await ledger.reconcile_ticker("TEST")
        assert result["partial"] == 1 and result["matched"] == 0
        assert trade["source"] == "partial"
        # An unmatched purchase of ten units is inventory, not a realised loss.
        assert trade["pnl_gross"] == 100.0
        operations.append({"id": "sell2", "kind": "sell", "qty": 10, "price": 120.0,
                           "ts": 1100.0, "payment": 1200.0, "figi": "TEST-FIGI"})
        result = await ledger.reconcile_ticker("TEST")
        assert result["matched"] == 1 and result["partial"] == 0
        assert trade["source"] == "broker" and trade["pnl_gross"] == 300.0
        assert set(trade["ops"]) == {"buy", "sell1", "sell2"}
        assert (await ledger.reconcile_ticker("TEST"))["matched"] == 0
        assert update.call_count == 2

    asyncio.run(scenario())


def make_exit_pilot():
    pilot = make_pilot()
    pilot.pending = None
    pilot.plan = None
    pilot.position = {"side": "long", "lots": 5, "entry": 100.0, "opened_ts": 1.0,
                      "take": 120.0, "invalidation": 90.0, "stop_id": None}
    return pilot


def test_accepted_close_waits_for_fills_and_applies_each_delta_once():
    async def scenario():
        pilot = make_exit_pilot()
        assert not await pilot._close_all(110.0, "close", False)
        assert pilot.position["lots"] == 5 and not pilot.pnls
        pilot.broker.order_state.return_value = {"ok": True, "status": "PARTIALLYFILL", "exec_lots": 2}
        assert not await pilot._close_all(110.0, "close", False)
        assert pilot.position["lots"] == 3 and pilot.pnls == [20.0]
        assert not await pilot._close_all(110.0, "close", False)
        assert pilot.position["lots"] == 3 and pilot.pnls == [20.0]
        pilot.broker.order_state.return_value = {"ok": True, "filled": True, "exec_lots": 5}
        assert await pilot._close_all(110.0, "close", False)
        assert pilot.position is None and pilot.pnls == [20.0, 30.0]
        pilot.broker.place.assert_awaited_once()

    asyncio.run(scenario())


def test_cancelled_partial_exit_retries_only_confirmed_remaining_quantity():
    async def scenario():
        pilot = make_exit_pilot()
        pilot.broker.order_state.side_effect = [
            {"ok": True, "status": "PARTIALLYFILL", "exec_lots": 2},
            {"ok": True, "status": "CANCELLED", "exec_lots": 2},
            {"ok": True, "filled": True, "exec_lots": 3},
        ]
        assert not await pilot._close_all(110.0, "close", False)
        assert not await pilot._close_all(110.0, "close", False)
        assert "exit_order" not in pilot.position and pilot.position["lots"] == 3
        assert await pilot._close_all(110.0, "close", False)
        assert [call.args[2] for call in pilot.broker.place.await_args_list] == [5, 3]
        assert sum(pilot.pnls) == 50.0

    asyncio.run(scenario())


def test_reduce_waits_for_fill_and_restores_stop_for_actual_remainder():
    async def scenario():
        pilot = make_exit_pilot()
        pos = pilot.position
        await pilot._reduce(pos, 2, 110.0, "margin")
        assert pos["lots"] == 5 and not pilot.pnls
        pilot.broker.place_stop.assert_not_awaited()
        pilot.broker.order_state.return_value = {"ok": True, "status": "PARTIALLYFILL", "exec_lots": 1}
        await pilot.tick(110.0)
        await pilot.tick(110.0)
        assert pos["lots"] == 4 and pilot.pnls == [10.0]
        pilot.broker.order_state.return_value = {"ok": True, "filled": True, "exec_lots": 2}
        await pilot.tick(110.0)
        assert pos["lots"] == 3 and pilot.pnls == [10.0, 10.0]
        assert "exit_order" not in pos
        pilot.broker.place.assert_awaited_once()
        assert pilot.broker.place_stop.await_args.args[2] == 3

    asyncio.run(scenario())


def test_unconfirmed_stop_cancel_blocks_manual_close_and_preserves_id():
    async def scenario():
        pilot = make_exit_pilot()
        pilot.position["stop_id"] = "existing-stop"
        pilot._real_own_lots = AsyncMock(return_value=5)
        assert not await pilot._close_all(110.0, "close", False)
        assert pilot.position["stop_id"] == "existing-stop"
        assert not await pilot._close_all(110.0, "close", False)
        pilot.broker.place.assert_not_awaited()
        assert pilot.broker.cancel_stop.await_count == 2

    asyncio.run(scenario())


def test_timeout_close_survives_restart_and_queries_same_request_uuid(tmp_path):
    async def scenario():
        saved = tmp_path / "pilot.json"
        posted = []

        async def post(path, body):
            if path.endswith("PostSandboxOrder"):
                persisted = json.loads(saved.read_text(encoding="utf-8"))
                assert persisted["position"]["exit_order"]["request_id"] == body["orderId"]
                posted.append(body)
                raise httpx.ReadTimeout("response lost after exchange accepted order")
            raise httpx.ReadTimeout("state temporarily unavailable")

        pilot = make_exit_pilot()
        pilot._state_path = saved
        pilot.broker = trader_broker.Broker("sandbox", account_id="offline", poster=post)
        assert not await pilot._close_all(110.0, "close", False)
        assert pilot.position["lots"] == 5
        order = pilot.position["exit_order"]
        assert order["id_type"] == "request"
        assert order["order_id"] == posted[0]["orderId"]
        assert not await pilot._close_all(110.0, "close", False)
        assert len(posted) == 1

        restored = make_exit_pilot()
        restored._state_path = saved
        queries = []

        async def recovered(path, body):
            queries.append((path, body))
            assert path.endswith("GetSandboxOrderState")
            return {"orderId": "exchange-order", "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
                    "lotsExecuted": 5}

        restored.broker = trader_broker.Broker("sandbox", account_id="offline", poster=recovered)
        restored._restore_state(0)  # portfolio already reflects the closed position
        assert restored.position["lots"] == 5  # cumulative order status owns this delta
        assert await restored._close_all(110.0, "close", False)
        assert restored.position is None and restored.pnls == [50.0]
        assert queries[0][1]["orderId"] == posted[0]["orderId"]
        assert queries[0][1]["orderIdType"] == "ORDER_ID_TYPE_REQUEST"
        assert not saved.exists()

    asyncio.run(scenario())


def test_restart_after_partial_close_does_not_account_old_fills_twice(tmp_path):
    async def scenario():
        pilot = make_exit_pilot()
        pilot._state_path = tmp_path / "pilot.json"
        pilot.broker.order_state.return_value = {"ok": True, "status": "PARTIALLYFILL", "exec_lots": 2}
        assert not await pilot._close_all(110.0, "close", False)
        assert pilot.position["lots"] == 3
        restored = make_exit_pilot()
        restored._state_path = pilot._state_path
        restored._restore_state(1)  # portfolio newer than last persisted partial fill
        assert restored.position["lots"] == 3
        restored.broker.order_state.return_value = {"ok": True, "filled": True, "exec_lots": 5}
        assert await restored._close_all(110.0, "close", False)
        assert restored.pnls == [30.0]
        restored.broker.place.assert_not_awaited()

    asyncio.run(scenario())


def test_order_is_not_sent_if_identity_cannot_be_persisted():
    async def scenario():
        pilot = make_exit_pilot()
        pilot._save_state = Mock(return_value=False)
        assert not await pilot._close_all(110.0, "close", False)
        assert pilot.position["lots"] == 5
        pilot.broker.place.assert_not_awaited()

    asyncio.run(scenario())


def test_panic_close_is_sent_even_if_identity_cannot_be_persisted():
    # W4 (п. 6): постоянная ошибка диска не запирает паническое закрытие — уходит без персиста, с пометкой
    async def scenario():
        pilot = make_exit_pilot()
        pilot._save_state = Mock(return_value=False)
        pilot.panic_flag = True
        pilot.broker.order_state.return_value = {"ok": True, "filled": True, "exec_lots": 5}
        assert await pilot._close_all(110.0, "ПАНИКА владельца", False)
        pilot.broker.place.assert_awaited_once()
        assert pilot.position is None and pilot.pnls == [50.0]

    asyncio.run(scenario())


def make_tick_pilot():
    """Пилот для тиков: с депозитом (иначе маржевой дозор пилота без денег закрыл бы позицию раньше стопа)."""
    pilot = make_exit_pilot()
    pilot.deposit = 100000.0
    return pilot


def _stale_request(pilot, age=400.0, request_id="lost-uuid"):
    request = {"request_id": request_id, "lots": 5, "direction": trader_broker.SELL, "price": 90.0,
               "uncertain": True, "created_ts": time.time() - age}
    pilot.position["stop_request"] = request
    pilot.position["restop_after"] = 0.0
    return request


def _listed_stop(stop_id="found-stop", lots="5", units=90, nano=0, age=390.0, **extra):
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age))
    return {"stopOrderId": stop_id, "figi": "TEST-FIGI", "direction": trader_broker.STOP_SELL,
            "lotsRequested": lots, "stopPrice": {"units": units, "nano": nano, "currency": "rub"},
            "createDate": created, "status": "STOP_ORDER_STATUS_ACTIVE", **extra}


@pytest.mark.parametrize("listed", [False, True])
def test_stale_unknown_stop_request_is_resolved_by_exchange_listing(listed):
    # W4 (п. 1): запрос старше 5 мин не висит вечно — список стопов биржи: найден → свой; нет → заново новым UUID
    async def scenario():
        pilot = make_exit_pilot()
        request = _stale_request(pilot)
        pilot.broker.stop_orders = AsyncMock(return_value=[_listed_stop()] if listed else [])
        seen = []

        async def place_stop(*args, **kwargs):
            seen.append(pilot.position["stop_request"]["request_id"])
            return {"ok": True, "stop_order_id": "new-stop"}

        pilot.broker.place_stop.side_effect = place_stop
        await pilot._replace_stop(pilot.position)
        assert "stop_request" not in pilot.position and not pilot.position.get("restop")
        if listed:
            assert pilot.position["stop_id"] == "found-stop" and not seen
        else:
            assert pilot.position["stop_id"] == "new-stop" and len(seen) == 1
            assert seen[0] != request["request_id"] and str(uuid.UUID(seen[0])) == seen[0]

    asyncio.run(scenario())


@pytest.mark.parametrize("wrong", ["lots", "price", "direction", "older"])
def test_listing_match_requires_same_body_and_not_older_stop(wrong):
    async def scenario():
        pilot = make_exit_pilot()
        _stale_request(pilot)
        other = {"lots": _listed_stop(lots="4"), "price": _listed_stop(units=91),
                 "direction": _listed_stop(direction=trader_broker.STOP_BUY),
                 "older": _listed_stop(age=1000.0)}[wrong]
        pilot.broker.stop_orders = AsyncMock(return_value=[other])
        await pilot._replace_stop(pilot.position)
        assert pilot.position["stop_id"] == "new-stop"   # чужой/старый стоп не принят — трос поставлен заново
        pilot.broker.place_stop.assert_awaited_once()

    asyncio.run(scenario())


def test_stale_stop_request_waits_honestly_when_listing_is_unavailable():
    async def scenario():
        pilot = make_exit_pilot()
        request = _stale_request(pilot)
        await pilot._replace_stop(pilot.position)
        assert pilot.position["stop_request"] is request
        assert "повтор заблокирован" in pilot.last_action and "список стопов биржи недоступен" in pilot.last_action
        assert pilot.position["restop_after"] > time.time()
        pilot.broker.place_stop.assert_not_awaited()

    asyncio.run(scenario())


def test_rejected_replay_of_unknown_stop_closes_the_request_and_rearms_with_new_uuid():
    # W4 (п. 1): определённый отказ на повторе неопределённого запроса = запрос закрыт (раньше висел навсегда)
    async def scenario():
        pilot = make_tick_pilot()
        request = _stale_request(pilot, age=10.0)
        pilot.broker.stop_orders = AsyncMock(return_value=[])
        pilot.broker.place_stop.return_value = {"ok": False, "error": "30079: инструмент недоступен"}
        await pilot._replace_stop(pilot.position)
        assert "stop_request" not in pilot.position and pilot.position["restop"]
        assert pilot.position["restop_after"] > time.time()     # бэкофф после отказа, не штурм
        assert pilot.broker.place_stop.await_count == 1
        await pilot.tick(100.0)                                  # бэкофф не вышел — тик биржу не дёргает
        assert pilot.broker.place_stop.await_count == 1 and pilot.position["restop"]
        assert "трос на бирже" not in pilot.last_action and pilot.position["lots"] == 5
        pilot.position["restop_after"] = 0.0
        seen = []

        async def place_stop(*args, **kwargs):
            seen.append(pilot.position["stop_request"]["request_id"])
            return {"ok": True, "stop_order_id": "new-stop"}

        pilot.broker.place_stop.side_effect = place_stop
        await pilot.tick(100.0)
        assert pilot.position["stop_id"] == "new-stop" and pilot.broker.place_stop.await_count == 2
        assert pilot.broker.place_stop.await_args_list[1].args[1:] == pilot.broker.place_stop.await_args_list[0].args[1:]
        assert seen == [seen[0]] and seen[0] != request["request_id"]

    asyncio.run(scenario())


@pytest.mark.parametrize("real_lots", [5, 0])
def test_unconfirmed_stop_cancel_is_confirmed_by_listing_when_stop_is_gone(real_lots):
    # W4 (A7/A10): CancelStopOrder «не подтверждена», а в списке стопов его нет → отмена подтверждена; остаток — по счёту
    async def scenario():
        pilot = make_exit_pilot()
        pilot.position["stop_id"] = "existing-stop"
        pilot.broker.stop_orders = AsyncMock(return_value=[_listed_stop(stop_id="someone-else", lots="1", units=80)])
        pilot._real_own_lots = AsyncMock(return_value=real_lots)
        pilot.broker.order_state.return_value = {"ok": True, "filled": True, "exec_lots": 5}
        assert await pilot._close_all(110.0, "close", False)
        assert pilot.position is None
        pilot.broker.cancel_stop.assert_awaited_once_with("existing-stop")
        if real_lots:
            pilot.broker.place.assert_awaited_once()
            assert pilot.pnls == [50.0]
        else:
            pilot.broker.place.assert_not_awaited()        # трос исполнился сам: закрытие по цене стопа, без ордера
            assert pilot.pnls == [-50.0]

    asyncio.run(scenario())


@pytest.mark.parametrize("real_lots", [5, 0])
def test_restop_after_vanished_stop_verifies_account_even_when_portfolio_was_unavailable(real_lots):
    # W4 (A7): стопа на бирже нет — он мог исполниться: новый трос только после сверки остатка со счётом; счёт не
    # отвечает → трос отложен, а не поставлен «по памяти» на возможно пустой счёт
    async def scenario():
        pilot = make_tick_pilot()
        pilot.position["stop_id"] = "existing-stop"
        pilot.position["restop"] = True
        pilot.broker.stop_orders = AsyncMock(return_value=[])
        pilot._real_own_lots = AsyncMock(return_value=None)
        await pilot._replace_stop(pilot.position)
        assert pilot.position["stop_id"] is None and pilot.position["verify_lots"] and pilot.position["restop"]
        pilot.broker.place_stop.assert_not_awaited()
        pilot._real_own_lots = AsyncMock(return_value=real_lots)
        pilot.broker.portfolio = AsyncMock(return_value={"mode": "real", "positions": (
            [{"figi": "TEST-FIGI", "qty": real_lots}] if real_lots else [])})
        pilot.position["restop_after"] = 0.0
        await pilot.tick(100.0)
        if real_lots:
            pilot.broker.place_stop.assert_awaited_once()
            assert pilot.position["stop_id"] == "new-stop" and "verify_lots" not in pilot.position
        else:
            pilot.broker.place_stop.assert_not_awaited()      # счёт пуст: закрытие вне петли учтено, троса-сироты нет
            assert pilot.position is None and pilot.pnls == [-50.0]

    asyncio.run(scenario())


def test_unconfirmed_stop_cancel_still_blocks_close_while_stop_is_listed_alive():
    async def scenario():
        pilot = make_exit_pilot()
        pilot.position["stop_id"] = "existing-stop"
        pilot.broker.stop_orders = AsyncMock(return_value=[_listed_stop(stop_id="existing-stop")])
        pilot._real_own_lots = AsyncMock(return_value=5)
        assert not await pilot._close_all(110.0, "close", False)
        assert pilot.position["stop_id"] == "existing-stop"
        assert "отмена троса не подтверждена" in pilot.last_action
        pilot.broker.place.assert_not_awaited()

    asyncio.run(scenario())


def test_virtual_stop_closes_in_the_same_tick_while_stop_request_hangs():
    # W4 (п. 2): висящий запрос стопа не выключает виртуальный стоп: цена за уровнем → закрытие в тот же тик
    async def scenario():
        pilot = make_tick_pilot()
        _stale_request(pilot)
        pilot.broker.stop_orders = AsyncMock(return_value=[])
        pilot.broker.cancel_stop.return_value = {"ok": True}
        pilot.broker.order_state.return_value = {"ok": True, "filled": True, "exec_lots": 5}
        await pilot.tick(85.0)
        assert pilot.position is None and pilot.last_action.startswith("ЗАКРЫЛ ВСЁ (аварийный стоп @90"), pilot.last_action
        pilot.broker.place_stop.assert_awaited_once()          # запрос закрыт → трос заново …
        pilot.broker.cancel_stop.assert_awaited_once_with("new-stop")   # … и снят перед закрытием
        pilot.broker.place.assert_awaited_once()

    asyncio.run(scenario())


def test_panic_with_unverifiable_stop_is_honest_then_force_closes():
    # W4 (п. 3): при панике отмена троса не подтверждается N тиков → снять все стопы по figi через список;
    # список недоступен → честно «нужен force», без вранья про «отбито биржей»; force закрывает
    async def scenario():
        pilot = make_tick_pilot()
        _stale_request(pilot)                                    # брокер без списка стопов → не сверить
        pilot.broker.order_state.return_value = {"ok": True, "filled": True, "exec_lots": 5}
        await pilot.tick(85.0)
        assert pilot.position and "список стопов биржи недоступен" in pilot.last_action
        assert pilot.position["close_fail"].startswith("аварийный стоп @90"), pilot.position["close_fail"]
        pilot.broker.place.assert_not_awaited()
        pilot.panic()
        for _ in range(ai_pilot.PANIC_STOP_SWEEP_TICKS):
            await pilot.tick(85.0)
        assert pilot.position and pilot.panic_blocked and pilot.status()["panic_blocked"]
        assert pilot.last_action.startswith("ПАНИКА владельца — ") and "force" in pilot.last_action
        assert "отбито биржей" not in pilot.last_action
        pilot.broker.place.assert_not_awaited()
        pilot.panic(force=True)
        await pilot.tick(85.0)
        assert pilot.position is None and pilot.state == "СТОП"
        pilot.broker.place.assert_awaited_once()

    asyncio.run(scenario())


def test_panic_sweeps_all_figi_stops_via_listing_after_n_ticks():
    async def scenario():
        pilot = make_tick_pilot()
        pilot.position["stop_id"] = "existing-stop"
        alive = [_listed_stop(stop_id="existing-stop"), _listed_stop(stop_id="manual-stop", lots="2", units=85),
                 dict(_listed_stop(stop_id="other-figi"), figi="OTHER", instrumentUid="OTHER")]
        cancelled = []

        async def cancel_stop(stop_id):
            if stop_id == "existing-stop" and len(cancelled) < ai_pilot.PANIC_STOP_SWEEP_TICKS:
                cancelled.append(stop_id)
                return {"ok": False, "error": "timeout"}
            cancelled.append(stop_id)
            alive[:] = [so for so in alive if so["stopOrderId"] != stop_id]
            return {"ok": True}

        pilot.broker.cancel_stop.side_effect = cancel_stop
        pilot.broker.stop_orders = AsyncMock(side_effect=lambda **kwargs: list(alive))
        pilot._real_own_lots = AsyncMock(return_value=5)
        pilot.broker.order_state.return_value = {"ok": True, "filled": True, "exec_lots": 5}
        pilot.panic()
        for _ in range(ai_pilot.PANIC_STOP_SWEEP_TICKS - 1):
            await pilot.tick(100.0)
            assert pilot.position and "попытка" in pilot.last_action
        await pilot.tick(100.0)
        assert pilot.position is None and pilot.state == "СТОП"
        assert [so["stopOrderId"] for so in alive] == ["other-figi"]   # чужой инструмент не тронут
        assert "manual-stop" in cancelled
        pilot.broker.place.assert_awaited_once()

    asyncio.run(scenario())


def test_mismatched_state_file_is_deferred_instead_of_raising(tmp_path):
    # W4 (п. 7): заявка другого счёта/режима — файл отложен (.mismatch-*), честная пометка, без цикла падений
    saved = tmp_path / "pilot.json"
    saved.write_text(json.dumps({"figi": "TEST-FIGI", "base": "TEST", "account_id": "other-account", "mode": "real",
                                 "pending": {"order_id": "entry-1", "lots": 3, "side": "long", "price": 100.0},
                                 "position": None}), encoding="utf-8")
    pilot = make_pilot()
    pilot.pending = None
    pilot._state_path = saved
    pilot.broker.account_id = "offline"
    pilot._restore_state(0)
    assert pilot.pending is None and not saved.exists()
    deferred = list(tmp_path.glob("pilot.json.mismatch-*"))
    assert len(deferred) == 1
    assert json.loads(deferred[0].read_text(encoding="utf-8"))["pending"]["order_id"] == "entry-1"
    assert "другому счёту" in pilot.last_action and "other-account" in pilot.last_action
    assert pilot.status()["state_note"] == pilot.last_action


def test_panic_flag_is_not_saved_or_inherited_without_position_or_pending(tmp_path):
    # W4 №1 (проверяющий): ПАНИКА — свойство позиции/заявки, не инструмента: файл с одной секцией pilot (память
    # проколов) не рождает следующий пилот того же тикера в СТОП «ПАНИКА владельца»
    saved = tmp_path / "pilot.json"
    pilot = make_pilot()
    pilot.pending = None
    pilot._state_path = saved
    pilot._state_extra = lambda: {"puncture_seen": [["вниз", 195, "угроза", 1.0]]}
    pilot.panic_flag = pilot.panic_force = True
    pilot._close_pending = "закрыть"
    assert pilot._save_state() and saved.exists()
    rec = json.loads(saved.read_text(encoding="utf-8"))
    assert rec["pilot"] and "panic" not in rec and "panic_force" not in rec and rec["position"] is None
    fresh = make_pilot()
    fresh.pending = None
    fresh._state_path = saved
    fresh._restore_state(0)
    assert not fresh.panic_flag and not fresh.panic_force and fresh._close_pending is None and fresh.pending is None
    # старый файл (написан прежним кодом: panic:true без позиции и заявок) — тоже не наследуется
    saved.write_text(json.dumps({"figi": "TEST-FIGI", "base": "TEST", "position": None, "pending": None,
                                 "panic": True, "panic_force": True, "close_pending": "закрыть",
                                 "pilot": {"puncture_seen": []}}), encoding="utf-8")
    legacy = make_pilot()
    legacy.pending = None
    legacy._state_path = saved
    legacy._restore_state(0)
    assert not legacy.panic_flag and not legacy.panic_force and legacy._close_pending is None
    # с заявкой в файле паника наследуется вместе с ней (их семантика сохранена)
    pilot.pending = {"order_id": "pending-1", "lots": 5, "side": "long", "price": 100.0, "ts": 0.0, "attempts": 0}
    assert pilot._save_state()
    rec = json.loads(saved.read_text(encoding="utf-8"))
    assert rec["panic"] and rec["panic_force"]
    owned = make_pilot()
    owned.pending = None
    owned._state_path = saved
    owned._restore_state(0)
    assert owned.panic_flag and owned.panic_force and owned._close_pending == "закрыть"
    assert owned.pending["order_id"] == "pending-1" and owned._cancel_entry


def _raising_poster(exc):
    async def post(path, body):
        raise exc
    return post


def test_broker_reports_not_found_only_for_genuine_not_found_by_request_uuid():
    # W4 №2а: «биржа такой заявки не знает» (404 / 400 с кодом-текстом «не найдена») отличимо от «сервис недоступен»
    TinkoffError = trader_broker.tinkoff.TinkoffError

    async def scenario():
        for exc in (TinkoffError(404, "NOT_FOUND", "50005", "order not found", "GetOrderState"),
                    TinkoffError(400, "INVALID_ARGUMENT", "50005", "заявка не найдена"),
                    TinkoffError(400, "INVALID_ARGUMENT", "", "Order not found")):
            b = trader_broker.Broker("sandbox", account_id="offline", poster=_raising_poster(exc))
            st = await b.order_state("req-1", request_id=True)
            assert st["ok"] and st["not_found"] and st["status"] == "NOT_FOUND" and not st["filled"] and st["exec_lots"] == 0
            assert (await b.cancel("req-1", request_id=True))["not_found"]
            by_exchange_id = await b.order_state("exchange-1")
            assert not by_exchange_id["ok"] and not by_exchange_id.get("not_found")   # биржевой id: заявка была
        for exc in (TinkoffError(401, "UNAUTHENTICATED", "40003", "token"), TinkoffError(403, "PERMISSION_DENIED", "40002", "no rights"),
                    TinkoffError(429, "RESOURCE_EXHAUSTED", "80002", "limit"), TinkoffError(408, "", "", "timeout"),
                    TinkoffError(409, "", "", "conflict"), TinkoffError(503, "UNAVAILABLE", "", "down"),
                    TinkoffError(400, "INVALID_ARGUMENT", "30042", "insufficient funds"), RuntimeError("connection reset")):
            b = trader_broker.Broker("sandbox", account_id="offline", poster=_raising_poster(exc))
            st = await b.order_state("req-1", request_id=True)
            assert not st["ok"] and not st.get("not_found"), exc
            assert not (await b.cancel("req-1", request_id=True))["ok"]

    asyncio.run(scenario())


def test_connect_error_on_post_is_a_definite_refusal_but_lost_response_is_not():
    # W4 №2г: соединение не установлено → запрос не отправлен → не фантом; ответ потерян → по-прежнему uncertain
    async def scenario():
        for exc, uncertain in ((httpx.ConnectError("dns failure"), False), (httpx.ConnectTimeout("connect"), False),
                               (httpx.ReadTimeout("read"), True), (httpx.ReadError("reset"), True),
                               (RuntimeError("connection reset by peer"), True)):
            b = trader_broker.Broker("sandbox", account_id="offline", poster=_raising_poster(exc))
            r = await b.place("TEST", trader_broker.BUY, 1, 100.0)
            assert not r["ok"] and r["uncertain"] is uncertain, exc
            b.mode = "real"
            rs = await b.place_stop("TEST", trader_broker.SELL, 1, 90.0)
            assert not rs["ok"] and rs["uncertain"] is uncertain, exc

    asyncio.run(scenario())


def _not_found():
    return {"ok": True, "status": "NOT_FOUND", "not_found": True, "filled": False, "exec_lots": 0}


@pytest.mark.parametrize("age", [10.0, 200.0])
def test_phantom_entry_request_is_released_only_after_age(age):
    # W4 №2б: заявка по request-UUID «не найдена» биржей дольше ORDER_REQUEST_MAX_AGE_SEC — терминал с 0 исполнений
    async def scenario():
        pilot = make_pilot()
        original = pilot.pending
        original["id_type"] = "request"
        original["request_ts"] = time.time() - age
        original["not_found_since"] = time.time() - age        # «не найдена» наблюдается столько же (V, e)
        pilot.broker.order_state.return_value = _not_found()
        pilot.broker.cancel.return_value = {"ok": True, "status": "NOT_FOUND", "not_found": True}
        part, po = await pilot._cancel_pending()
        if age > ai_pilot.ORDER_REQUEST_MAX_AGE_SEC:
            assert (part, po) == (0, original) and pilot.pending is None and not pilot._cancel_entry
        else:
            assert (part, po) == (0, None) and pilot.pending is original and pilot._cancel_entry
            assert "не находит заявку" in pilot.last_action
        # обычный опрос (без снятия): старый фантом — как отказ биржи с бэкоффом, свежий — ждём
        again = make_pilot()
        again.pending["id_type"] = "request"
        again.pending["request_ts"] = time.time() - age
        again.pending["not_found_since"] = time.time() - age
        again.pending["ts"] = time.time()                     # TTL не вышел — снятия нет, только опрос
        again.broker.order_state.return_value = _not_found()
        await again._pending_tick(100.0, {})
        if age > ai_pilot.ORDER_REQUEST_MAX_AGE_SEC:
            assert again.pending is None and again._entry_fail == 1 and again.no_entry_until > time.time()
            assert "NOT_FOUND" in again.last_action
        else:
            assert again.pending is not None and again._entry_fail == 0
        # заявка с биржевым id никогда не «фантом»
        exch = make_pilot()
        exch.pending.update(id_type="exchange", request_ts=time.time() - 1000)
        exch.broker.order_state.return_value = _not_found()
        assert await exch._cancel_pending() == (0, None) and exch.pending is not None

    asyncio.run(scenario())


def test_panic_force_drops_unresolvable_entry_request_after_attempts_and_closes():
    # W4 №2в: неразрешимая заявка входа при ПАНИКЕ — N попыток честно (повтор = force), с force снята из учёта
    # (UUID в тексте/логе), позиция закрыта; петля может завершиться
    async def scenario():
        pilot = make_tick_pilot()
        pilot.pending = {"order_id": "phantom-uuid", "request_id": "phantom-uuid", "id_type": "request", "side": "long",
                         "lots": 2, "price": 100.0, "ts": time.time(), "request_ts": time.time(), "attempts": 0,
                         "take": 120.0, "invalidation": 90.0, "topup": True}

        async def state(order_id, **kwargs):
            if order_id == "phantom-uuid":
                return {"ok": False, "error": "timeout"}
            return {"ok": True, "filled": True, "exec_lots": 5}

        pilot.broker.order_state.side_effect = state
        pilot.panic()
        for n in range(1, ai_pilot.PANIC_PENDING_DROP_ATTEMPTS + 1):
            await pilot.tick(100.0)
            assert pilot.pending and pilot.position and pilot.state != "СТОП"
        assert pilot.panic_blocked and "phantom-uuid" in pilot.last_action and "force" in pilot.last_action
        pilot.broker.place.assert_not_awaited()
        pilot.panic(force=True)
        await pilot.tick(100.0)
        assert pilot.pending is None and pilot.position is None and pilot.state == "СТОП"
        pilot.broker.place.assert_awaited_once()
        assert pilot.broker.place.await_args.args[2] == 5

    asyncio.run(scenario())


def test_phantom_exit_order_is_released_and_remainder_rechecked():
    # W4 №2: то же для заявки выхода — фантом старше ORDER_REQUEST_MAX_AGE_SEC терминален, остаток по счёту, закрытие заново
    async def scenario():
        pilot = make_exit_pilot()
        pilot.broker.place.return_value = {"ok": False, "uncertain": True, "error": "connection reset"}
        pilot.broker.order_state.return_value = _not_found()
        assert not await pilot._close_all(110.0, "close", False)
        order = pilot.position["exit_order"]
        assert order["id_type"] == "request" and "жду исполнения" in pilot.last_action
        assert not await pilot._close_all(110.0, "close", False)          # свежий фантом — ждём
        assert pilot.position["exit_order"] is order
        order["request_ts"] = time.time() - ai_pilot.ORDER_REQUEST_MAX_AGE_SEC - 10
        order["not_found_since"] = time.time() - ai_pilot.ORDER_REQUEST_MAX_AGE_SEC - 10
        assert not await pilot._close_all(110.0, "close", False)
        assert "exit_order" not in pilot.position and pilot.position["exit_check"] and pilot.position["lots"] == 5
        pilot.broker.place.return_value = {"ok": True, "order_id": "replacement"}
        pilot.broker.order_state.return_value = {"ok": True, "filled": True, "exec_lots": 5}
        pilot._real_own_lots = AsyncMock(return_value=5)
        assert await pilot._close_all(110.0, "close", False)
        assert pilot.position is None and pilot.pnls == [50.0]
        assert pilot.broker.place.await_count == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("real_lots", [0, 5])
def test_deferred_remainder_check_survives_to_the_next_close(real_lots):
    # W4 №4: стоп исчез (мог исполниться), портфель на этом тике недоступен → закрытие отложено, а СВЕРКА ПОМНИТСЯ:
    # следующее закрытие сначала спрашивает счёт (пусто → без ордера), а не шлёт рыночную на все лоты вслепую
    async def scenario():
        pilot = make_tick_pilot()
        pilot.position["stop_id"] = "existing-stop"
        pilot.broker.stop_orders = AsyncMock(return_value=[])
        pilot._real_own_lots = AsyncMock(return_value=None)
        assert not await pilot._close_all(110.0, "close", False)
        assert pilot.position["stop_id"] is None and pilot.position["exit_check"] and pilot.position["exit_check_strict"]
        assert "отложено" in pilot.last_action
        pilot.broker.place.assert_not_awaited()
        pilot._real_own_lots = AsyncMock(return_value=real_lots)
        pilot.broker.order_state.return_value = {"ok": True, "filled": True, "exec_lots": 5}
        await pilot.tick(110.0)                                  # close_fail → повтор закрытия
        assert pilot.position is None
        if real_lots:
            pilot.broker.place.assert_awaited_once()
            assert pilot.pnls == [50.0]
        else:
            pilot.broker.place.assert_not_awaited()
            assert pilot.pnls == [-50.0]                        # трос исполнился: учтено по цене стопа

    asyncio.run(scenario())


def test_remainder_check_flags_survive_restart(tmp_path):
    saved = tmp_path / "pilot.json"
    pilot = make_exit_pilot()
    pilot._state_path = saved
    pilot.position.update(exit_check=True, exit_check_strict=True, verify_lots=True)
    assert pilot._save_state()
    restored = make_exit_pilot()
    restored._state_path = saved
    restored._restore_state(5)
    assert restored.position["exit_check"] and restored.position["exit_check_strict"] and restored.position["verify_lots"]


@pytest.mark.parametrize("real_lots", [0, 3])
def test_terminal_exit_order_rechecks_account_before_resubmitting(real_lots):
    # W4 (сцена «владелец продал в выходе»): заявка выхода снята биржей без исполнения → остаток по счёту, не по памяти
    async def scenario():
        pilot = make_exit_pilot()
        pilot.broker.order_state.side_effect = [
            {"ok": True, "status": "CANCELLED", "exec_lots": 0},
            {"ok": True, "filled": True, "exec_lots": 3},
        ]
        pilot._real_own_lots = AsyncMock(return_value=real_lots)
        assert not await pilot._close_all(110.0, "close", False)
        assert pilot.position["lots"] == 5 and pilot.position["exit_check"]
        assert await pilot._close_all(110.0, "close", False)
        assert pilot.position is None
        lots_sent = [call.args[2] for call in pilot.broker.place.await_args_list]
        if real_lots:
            assert lots_sent == [5, 3] and pilot.pnls == [20.0, 30.0]
        else:
            assert lots_sent == [5] and pilot.pnls == [50.0]

    asyncio.run(scenario())


def test_cancelled_post_entry_recovers_its_persisted_identity(tmp_path):
    async def scenario():
        pilot = make_pilot()
        pilot.pending = None
        pilot._state_path = tmp_path / "pilot.json"
        pilot._refresh_max = AsyncMock()
        pilot.max_lots = Mock(return_value=5)
        posted = []

        async def post(path, body):
            posted.append(body)
            raise asyncio.CancelledError

        pilot.broker = trader_broker.Broker("sandbox", account_id="offline", poster=post)
        with pytest.raises(asyncio.CancelledError):
            await pilot._enter(100.0, {})
        saved = json.loads(pilot._state_path.read_text(encoding="utf-8"))
        assert saved["pending"]["request_id"] == posted[0]["orderId"]
        restored = make_pilot()
        restored.pending = None
        restored.broker.mode = "sandbox"
        restored.broker.account_id = "offline"
        restored._state_path = pilot._state_path
        restored._restore_state(5)
        assert restored.pending["order_id"] == posted[0]["orderId"]
        restored.broker.order_state.return_value = {"ok": True, "filled": True, "exec_lots": 5}
        await restored._pending_tick(100.0, {})
        assert restored.pending is None and restored.position["lots"] == 5
        restored.broker.place.assert_not_awaited()

    asyncio.run(scenario())


def test_confirmed_post_fill_is_not_lost_when_followup_state_request_fails():
    async def scenario():
        pilot = make_pilot()
        pilot.pending = None
        pilot._refresh_max = AsyncMock()
        pilot.max_lots = Mock(return_value=5)
        pilot.broker.place.return_value = {"ok": True, "order_id": "filled-entry", "status": "FILL", "exec_lots": 5}
        pilot.broker.order_state.return_value = {"ok": False, "error": "state unavailable"}
        await pilot._enter(100.0, {})
        await pilot._pending_tick(100.0, {})
        assert pilot.pending is None
        assert pilot.position["lots"] == 5
        pilot.broker.place_stop.assert_awaited_once()

    asyncio.run(scenario())


def test_malformed_post_response_is_uncertain_instead_of_a_safe_retry():
    async def scenario():
        broker = trader_broker.Broker("sandbox", account_id="offline", poster=AsyncMock(return_value=None))
        result = await broker.place("TEST", trader_broker.SELL, 5)
        assert not result["ok"] and result["uncertain"]
        assert result["id_type"] == "request"
        uuid.UUID(result["order_id"])

    asyncio.run(scenario())


def test_close_waits_for_inflight_stop_replacement_then_cancels_new_stop():
    async def scenario():
        pilot = make_exit_pilot()
        pos = pilot.position
        entered, release = asyncio.Event(), asyncio.Event()

        async def place_stop(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"ok": True, "stop_order_id": "new-stop"}

        pilot.broker.place_stop.side_effect = place_stop
        pilot.broker.cancel_stop.return_value = {"ok": True}
        pilot.broker.order_state.return_value = {"ok": True, "filled": True}
        replacing = asyncio.create_task(pilot._replace_stop(pos))
        await entered.wait()
        closing = asyncio.create_task(pilot._close_all(110.0, "close", False))
        await asyncio.sleep(0)
        pilot.broker.place.assert_not_awaited()
        release.set()
        await replacing
        assert await closing
        assert pilot.position is None
        pilot.broker.cancel_stop.assert_awaited_once_with("new-stop")
        pilot.broker.place.assert_awaited_once()

    asyncio.run(scenario())


def test_stop_replacement_cannot_run_while_exit_is_outstanding():
    async def scenario():
        pilot = make_exit_pilot()
        assert not await pilot._close_all(110.0, "close", False)
        await pilot._replace_stop(pilot.position)
        pilot.broker.place_stop.assert_not_awaited()

    asyncio.run(scenario())


def test_closed_market_stopping_still_paces_unresolved_order_polls(monkeypatch):
    async def scenario():
        pilot = make_pilot()
        pilot.stopping = True
        pilot.prepare = AsyncMock(return_value=True)
        pilot._market_closed = Mock(return_value=True)
        pauses = AsyncMock()
        monkeypatch.setattr(ai_pilot.asyncio, "sleep", pauses)
        monkeypatch.setattr(ai_pilot.tinkoff, "last_price", AsyncMock(return_value={"price": 100.0}))
        ticks = 0

        async def tick(*args):
            nonlocal ticks
            ticks += 1
            if ticks == 2:
                pilot.pending = None

        pilot.tick = tick
        await pilot._run_loop()
        assert ticks == 2
        assert pauses.await_count == 2
        assert all(call.args == (ai_pilot.TICK_SEC,) for call in pauses.await_args_list)

    asyncio.run(scenario())


def test_stop_timeout_replays_same_persisted_id_and_parameters_after_restart(tmp_path):
    async def scenario():
        pilot = make_exit_pilot()
        pilot._state_path = tmp_path / "pilot.json"
        sent = []

        async def lost(path, body):
            assert path.endswith("PostStopOrder")
            sent.append(body)
            saved = json.loads(pilot._state_path.read_text(encoding="utf-8"))
            assert saved["position"]["stop_request"]["request_id"] == body["orderId"]
            raise httpx.ReadTimeout("stop accepted, response lost")

        pilot.broker = trader_broker.Broker("sandbox", account_id="offline", poster=lost)
        pilot.broker.mode = "real"
        await pilot._replace_stop(pilot.position)
        first_request = dict(pilot.position["stop_request"])
        assert first_request["uncertain"] and pilot.position["stop_id"] is None

        async def found(path, body):
            assert path.endswith("PostStopOrder")
            sent.append(body)
            return {"stopOrderId": "original-stop", "orderRequestId": body["orderId"]}

        restored = make_exit_pilot()
        restored._state_path = pilot._state_path
        restored.broker = trader_broker.Broker("sandbox", account_id="offline", poster=found)
        restored.broker.mode = "real"
        restored._restore_state(5)
        restored.position["invalidation"] = 85.0  # a new desired level cannot mutate the outstanding request
        await restored._replace_stop(restored.position)
        assert sent[0] == sent[1]
        assert restored.position["stop_id"] == "original-stop"
        assert "stop_request" not in restored.position
        assert restored.position["restop"]  # only now can the old stop be cancelled/replaced

    asyncio.run(scenario())


def test_unknown_stop_placement_blocks_an_additional_market_close():
    async def scenario():
        pilot = make_exit_pilot()
        requests = []

        async def lost(path, body):
            requests.append((path, body))
            assert path.endswith("PostStopOrder"), "a market close would race an unknown protective stop"
            raise httpx.ReadTimeout("unknown stop")

        pilot.broker = trader_broker.Broker("sandbox", account_id="offline", poster=lost)
        pilot.broker.mode = "real"
        pilot._real_own_lots = AsyncMock(return_value=0)  # even an empty, possibly stale snapshot cannot resolve it
        await pilot._replace_stop(pilot.position)
        assert not await pilot._close_all(110.0, "close", False)
        assert pilot.position and pilot.position["stop_request"]
        assert len(requests) == 2
        assert requests[0][1] == requests[1][1]

    asyncio.run(scenario())


@pytest.mark.parametrize("intent", ["stop", "panic"])
def test_control_intent_before_prepare_preserves_durable_unresolved_order(tmp_path, intent):
    original = make_pilot()
    original._state_path = tmp_path / "pilot.json"
    original._save_state()
    before = original._state_path.read_bytes()
    fresh = make_pilot()
    fresh.pending = None
    fresh._state_path = original._state_path
    getattr(fresh, intent)()
    assert original._state_path.read_bytes() == before
    fresh._restore_state(0)
    assert fresh.pending["order_id"] == "pending-1"
    assert fresh.panic_flag == (intent == "panic")
    assert fresh.stopping == (intent == "stop")


@pytest.mark.parametrize("created_ts", [None, 1.0])
def test_old_or_undated_unknown_stop_never_replays_after_retention_is_unclear(created_ts):
    async def scenario():
        pilot = make_exit_pilot()
        request = {"request_id": str(uuid.uuid4()), "lots": 5, "direction": trader_broker.SELL,
                   "price": 90.0, "uncertain": True, "created_ts": created_ts}
        pilot.position["stop_request"] = request
        await pilot._replace_stop(pilot.position)
        assert pilot.position["stop_request"] is request
        assert "повтор заблокирован" in pilot.last_action
        pilot.broker.place_stop.assert_not_awaited()

    asyncio.run(scenario())


def test_known_rejected_stop_is_rearmed_after_restart(tmp_path):
    async def scenario():
        pilot = make_exit_pilot()
        pilot._state_path = tmp_path / "pilot.json"
        pilot.broker.place_stop.return_value = {"ok": False, "error": "explicit stop rejection"}
        await pilot._replace_stop(pilot.position)
        assert not pilot.position.get("stop_request") and pilot.position["restop"]
        saved = json.loads(pilot._state_path.read_text(encoding="utf-8"))
        saved["position"].pop("restop", None)  # legacy files did not preserve this flag
        saved["position"].pop("restop_after", None)
        pilot._state_path.write_text(json.dumps(saved), encoding="utf-8")
        restored = make_exit_pilot()
        restored._state_path = pilot._state_path
        restored._restore_state(5)
        assert restored.position["restop"]
        await restored._replace_stop(restored.position)
        assert restored.position["stop_id"] == "new-stop"
        restored.broker.place_stop.assert_awaited_once()

    asyncio.run(scenario())
