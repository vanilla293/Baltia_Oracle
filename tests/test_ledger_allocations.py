"""Broker operations can contain several journal exits. Verify durable allocation."""
import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from backend import ledger, store_v5


@pytest.fixture
def journal(monkeypatch, tmp_path):
    monkeypatch.setattr(store_v5, "DB_PATH", tmp_path / "ledger.db")
    store_v5._init()
    monkeypatch.setattr(ledger, "mode", lambda: "broker")
    monkeypatch.setattr(ledger, "_keys", AsyncMock(return_value=["TEST-FIGI"]))
    now = time.time()
    operations = [
        {"id": "entry5", "kind": "buy", "qty": 5, "price": 100.0, "ts": now - 90,
         "payment": -500.0, "fee": 5.0, "figi": "TEST-FIGI"},
        {"id": "exit5", "kind": "sell", "qty": 5, "price": 110.0, "ts": now - 30,
         "payment": 550.0, "fee": 5.0, "figi": "TEST-FIGI"},
    ]
    monkeypatch.setattr(store_v5, "ops_list", lambda *args, **kwargs: operations)

    def add(lots, offset=0):
        return store_v5.trade_add({"ticker": "TEST", "figi": "TEST-FIGI", "side": "long",
                                  "lots": lots, "lot": 1, "entry": 100, "exit_px": 110,
                                  "opened_ts": now - 95, "closed_ts": now - 25 + offset})
    return operations, add


@pytest.mark.parametrize("child_fees", [False, True])
def test_one_operation_allocates_across_two_journal_rows_and_reloads(journal, child_fees):
    operations, add = journal
    if child_fees:
        for op in list(operations):
            op["fee"] = 0
            operations.append({"id": op["id"] + "-fee", "kind": "fee", "parent": op["id"],
                               "ts": op["ts"], "payment": -5.0})
    add(2); add(3, 1)
    result = asyncio.run(ledger.reconcile_ticker("TEST"))
    assert result["matched"] == 2 and result["added"] == 0
    store_v5._init()  # Reads the durable quantity reservations, not process state.
    rows = store_v5.trades_full("TEST")
    assert [r["pnl_gross"] for r in rows] == [20, 30]
    assert [r["fee"] for r in rows] == [4, 6]
    assert [r["pnl_net"] for r in rows] == [16, 24]
    assert rows[0]["ops_alloc"]["fills"]["entry5"] == [[0, 2]]
    assert rows[1]["ops_alloc"]["fills"]["entry5"] == [[2, 3]]
    again = asyncio.run(ledger.reconcile_ticker("TEST"))
    assert again["matched"] == again["added"] == 0
    assert len(store_v5.trades_full("TEST")) == 2


@pytest.mark.parametrize("child_fees", [False, True])
def test_later_partial_fill_uses_only_unallocated_quantity(journal, child_fees):
    operations, add = journal
    operations[1].update(qty=2, payment=220.0, fee=2.0)
    if child_fees:
        for op in list(operations):
            operations.append({"id": op["id"] + "-fee", "kind": "fee", "parent": op["id"],
                               "ts": op["ts"], "payment": -op["fee"]})
            op["fee"] = 0
    add(2)
    assert asyncio.run(ledger.reconcile_ticker("TEST"))["matched"] == 1
    operations[1].update(qty=5, payment=550.0, fee=0.0 if child_fees else 5.0)
    if child_fees:
        operations[3]["payment"] = -5.0
    add(3, 1)
    result = asyncio.run(ledger.reconcile_ticker("TEST"))
    assert result["matched"] == 1 and result["added"] == 0
    rows = store_v5.trades_full("TEST")
    assert sum(r["fee"] for r in rows) == 10
    assert sum(r["pnl_net"] for r in rows) == 40


def test_unrecorded_remainder_is_recovered_once_with_its_commission(journal):
    operations, add = journal
    add(2)
    result = asyncio.run(ledger.reconcile_ticker("TEST"))
    assert result["matched"] == 1 and result["added"] == 1
    rows = store_v5.trades_full("TEST")
    assert sorted(r["lots"] for r in rows) == [2, 3]
    assert sum(r["fee"] for r in rows) == 10
    assert sum(r["pnl_net"] for r in rows) == 40
    assert asyncio.run(ledger.reconcile_ticker("TEST"))["added"] == 0


def test_quantity_slicing_preserves_distinct_fill_prices_and_gaps():
    op = {"id": "mixed", "kind": "buy", "qty": 6, "payment": -630.0, "fee": 6.0,
          "trades": [{"price": 100.0, "qty": 3}, {"price": 110.0, "qty": 3}]}
    parts, fills = ledger._take([op], 4, {"mixed": [[2, 2]]})
    assert fills == [(100.0, 2), (110.0, 2)]
    assert sum(p["payment"] for p in parts) == -420
    assert sum(p["fee"] for p in parts) == 4
    assert ledger._allocations(parts, [])["fills"]["mixed"] == [[0, 2], [4, 2]]


def test_delayed_reconciliation_is_cancelled_before_clients_close(monkeypatch):
    monkeypatch.setattr(ledger, "mode", lambda: "mock")
    monkeypatch.setattr(ledger, "_TASKS", set())
    sync = AsyncMock()
    monkeypatch.setattr(ledger, "sync_and_reconcile", sync)

    async def scenario():
        assert ledger.schedule_after_close("TEST", delay=60)
        await asyncio.sleep(0)
        await ledger.shutdown_tasks()
        assert not ledger._TASKS
        sync.assert_not_awaited()

    asyncio.run(scenario())
