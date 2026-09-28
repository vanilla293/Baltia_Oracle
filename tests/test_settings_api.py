# -*- coding: utf-8 -*-
"""v5.4.1 «трезвый пилот»: переключатели панели «Ключи» — трос на бирже (PYTHIA_EXCHANGE_STOP) и модель узлов у денег
(PYTHIA_MONEY_MODEL) через POST /api/v5/keys; настройки в GET /api/v5/keys и GET /api/v5/state.

Офлайн: config_user.json — во временном каталоге (в data/ ничего не пишется), ИИ/биржа/база не трогаются."""
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api_v5, config

SETTING_ENV = ("PYTHIA_EXCHANGE_STOP", "PYTHIA_MONEY_MODEL", "PYTHIA_ENTRY_CHECK", "PYTHIA_PROFIT_THINK",
               "PYTHIA_PROFIT_THINK_PCT")


@pytest.fixture
def isolated_config(tmp_path):
    """Временный config_user.json и живые config.* по нему; после теста — настоящий конфиг и config._refresh()
    (порядок ручной: monkeypatch откатывает атрибуты уже после финализатора фикстуры, а _refresh должен видеть их
    настоящими)."""
    path = tmp_path / "config_user.json"
    path.write_text("{}", encoding="utf-8")
    saved_env = {k: os.environ.pop(k) for k in SETTING_ENV if k in os.environ}
    orig_path, orig_cfg = config.USER_CFG_PATH, config._user_cfg
    config.USER_CFG_PATH, config._user_cfg = path, {}
    config._refresh()
    try:
        yield path
    finally:
        config.USER_CFG_PATH, config._user_cfg = orig_path, orig_cfg
        os.environ.update(saved_env)
        config._refresh()


@pytest.fixture
def client(monkeypatch, isolated_config):
    monkeypatch.setattr(api_v5.ai, "reset_client", Mock())
    monkeypatch.setattr(api_v5.tinkoff, "aclose", AsyncMock())
    monkeypatch.setattr(api_v5, "telegram", None)
    monkeypatch.setattr(api_v5, "_mod", lambda name: None)          # совет/миссия/часы/журнал в state не нужны
    monkeypatch.setattr(api_v5, "_astro_line", lambda: "")
    monkeypatch.setattr(api_v5, "store_v5", SimpleNamespace(watch_list=lambda n: [], watch_unseen_count=lambda: 0))
    app = FastAPI()
    app.include_router(api_v5.router)
    with TestClient(app) as c:
        yield c


def _file(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_defaults_are_sober_pilot(client, isolated_config):
    """Умолчания 5.4.1: стопы только в программе, узлы у денег на PRO, проверка входа и мысль о прибыли включены."""
    expected = {"exchange_stop": False, "money_model": "pro", "entry_check": True, "profit_think": True,
                "profit_think_pct": 60.0}
    assert client.get("/api/v5/keys").json()["settings"] == expected
    assert client.get("/api/v5/state").json()["settings"] == expected
    assert _file(isolated_config) == {}


def test_toggles_are_written_and_live_including_false(client, isolated_config):
    """Включить и выключить: False хранится как "0" (set_many выкидывает только None/""), config.* — живые."""
    r = client.post("/api/v5/keys", json={"exchange_stop": True, "money_model": "flash"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["changed"] == ["exchange_stop", "money_model"]
    assert body["settings"]["exchange_stop"] is True and body["settings"]["money_model"] == "flash"
    assert _file(isolated_config) == {"PYTHIA_EXCHANGE_STOP": "1", "PYTHIA_MONEY_MODEL": "flash"}
    assert config.PYTHIA_EXCHANGE_STOP is True and config.PYTHIA_MONEY_MODEL == "flash"

    r = client.post("/api/v5/keys", json={"exchange_stop": False})
    assert r.status_code == 200 and r.json()["changed"] == ["exchange_stop"]
    assert r.json()["settings"]["exchange_stop"] is False and r.json()["settings"]["money_model"] == "flash"
    assert _file(isolated_config) == {"PYTHIA_EXCHANGE_STOP": "0", "PYTHIA_MONEY_MODEL": "flash"}
    assert config.PYTHIA_EXCHANGE_STOP is False and config.get_bool("PYTHIA_EXCHANGE_STOP", True) is False

    r = client.post("/api/v5/keys", json={"money_model": " PRO "})
    assert r.status_code == 200 and r.json()["settings"]["money_model"] == "pro"
    assert _file(isolated_config)["PYTHIA_MONEY_MODEL"] == "pro" and config.PYTHIA_MONEY_MODEL == "pro"
    assert client.get("/api/v5/state").json()["settings"] == {
        "exchange_stop": False, "money_model": "pro", "entry_check": True, "profit_think": True, "profit_think_pct": 60.0}


@pytest.mark.parametrize("bad", [
    {"exchange_stop": "yes"}, {"exchange_stop": 1}, {"exchange_stop": "0"}, {"exchange_stop": "null"},
    {"money_model": "gpt"}, {"money_model": 5}, {"money_model": ""}, {"money_model": ["pro"]},
    {"money_model": "flash", "exchange_stop": "true"},                 # одно поле битое — не записано ничего
    {"tinkoff": "t.valid-token-1234567890", "money_model": "x"},       # ключ верный, настройка нет — ключ тоже не пишется
])
def test_invalid_setting_rejects_whole_request(client, isolated_config, bad):
    r = client.post("/api/v5/keys", json=bad)
    assert r.status_code == 400, r.text
    assert "error" in r.json()
    assert _file(isolated_config) == {}
    assert config.PYTHIA_EXCHANGE_STOP is False and config.PYTHIA_MONEY_MODEL == "pro"


def test_key_only_payload_leaves_settings_alone(client, isolated_config):
    """Запрос без переключателей не трогает PYTHIA_*: ничего лишнего в changed и в файле."""
    r = client.post("/api/v5/keys", json={"telegram_chat": "12345"})
    assert r.status_code == 200 and r.json()["changed"] == ["telegram_chat"]
    assert _file(isolated_config) == {"TG_CHAT_ID": "12345"}
    assert r.json()["settings"]["exchange_stop"] is False and r.json()["settings"]["money_model"] == "pro"


def test_settings_block_survives_broken_config(monkeypatch):
    """Сломанный config.* не роняет state: settings_block отдаёт умолчания 5.4.1."""
    monkeypatch.setattr(api_v5.config, "PYTHIA_MONEY_MODEL", object(), raising=False)
    s = api_v5.settings_block()
    assert s["money_model"] == "pro" and s["exchange_stop"] in (True, False)
