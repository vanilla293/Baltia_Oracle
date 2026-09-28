"""Общее для pytest (5.4.1): аудит брокера уходит во временный файл — тесты не пишут в data/trader_audit.jsonl.
5.4.2: ручки «свободного пилота» закреплены на умолчаниях — настройки владельца в data/config_user.json тесты не роняют."""
import pathlib
import tempfile

import pytest

from backend import config, trader_broker


@pytest.fixture(autouse=True)
def _free_pilot_defaults(monkeypatch):
    config.pin_free_pilot_defaults(lambda k, v: monkeypatch.setattr(config, k, v))


@pytest.fixture(autouse=True, scope="session")
def _broker_audit_to_tmp():
    saved = trader_broker._AUDIT_PATH
    trader_broker._AUDIT_PATH = pathlib.Path(tempfile.mkdtemp(prefix="pythia_tests_")) / "trader_audit.jsonl"
    try:
        yield
    finally:
        trader_broker._AUDIT_PATH = saved
