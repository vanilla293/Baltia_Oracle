"""Общее для pytest (5.4.1): аудит брокера уходит во временный файл — тесты не пишут в data/trader_audit.jsonl."""
import pathlib
import tempfile

import pytest

from backend import trader_broker


@pytest.fixture(autouse=True, scope="session")
def _broker_audit_to_tmp():
    saved = trader_broker._AUDIT_PATH
    trader_broker._AUDIT_PATH = pathlib.Path(tempfile.mkdtemp(prefix="pythia_tests_")) / "trader_audit.jsonl"
    try:
        yield
    finally:
        trader_broker._AUDIT_PATH = saved
