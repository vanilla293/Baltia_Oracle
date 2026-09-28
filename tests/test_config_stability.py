"""Configuration must not diverge from disk when persistence fails."""
import json

import pytest

from backend import config


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    path = tmp_path / "config_user.json"
    original = {"TINKOFF_TOKEN": "fake-old-token", "DEEPSEEK_KEYS": ["fake-old-key"],
                "INSTRUMENT_KEYS": {"SBER": "fake-personal-key"}, "NEWS_DAYS": 3}
    path.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(config, "USER_CFG_PATH", path)
    monkeypatch.setattr(config, "_user_cfg", original.copy())
    monkeypatch.setattr(config, "_key_health", {"fake-old-key": 42})
    refreshed = []
    monkeypatch.setattr(config, "_refresh", lambda: refreshed.append(config._user_cfg.copy()))
    return path, original, refreshed


@pytest.mark.parametrize("operation", [
    lambda: config.set_many({"TINKOFF_TOKEN": "fake-new-token", "NEWS_DAYS": 5}),
    lambda: config.set_deepseek_keys(["fake-new-key"]),
    lambda: config.set_instrument_key("SBER", "fake-new-key"),
    config.wipe_keys,
])
def test_failed_replace_preserves_active_settings(monkeypatch, isolated_config, operation):
    path, original, refreshed = isolated_config

    def fail_replace(*args):
        raise PermissionError("file is locked")

    monkeypatch.setattr(config.os, "replace", fail_replace)
    with pytest.raises(PermissionError):
        operation()
    assert config._user_cfg == original
    assert json.loads(path.read_text(encoding="utf-8")) == original
    assert config._key_health == {"fake-old-key": 42}
    assert refreshed == []


def test_success_publishes_one_complete_snapshot(isolated_config):
    path, original, refreshed = isolated_config
    config.set_many({"TINKOFF_TOKEN": "fake-new-token", "NEWS_DAYS": 5, "DEEPSEEK_KEYS": None})
    expected = {**original, "TINKOFF_TOKEN": "fake-new-token", "NEWS_DAYS": 5}
    expected.pop("DEEPSEEK_KEYS")
    assert json.loads(path.read_text(encoding="utf-8")) == config._user_cfg == expected
    assert refreshed == [expected]


@pytest.mark.parametrize("content", ["{broken", "null", "[]", '"text"'])
def test_invalid_file_is_preserved_and_reported(isolated_config, content):
    path, original, refreshed = isolated_config
    path.write_text(content, encoding="utf-8")
    with pytest.raises(RuntimeError, match="исходный файл сохранён"):
        config._load_user_cfg()
    assert path.read_text(encoding="utf-8") == content
    assert config._user_cfg == original
    assert not refreshed


def test_empty_file_means_no_settings_not_failure(isolated_config):
    """Пустой файл (обрыв записи старой версии) — не повреждение: старт с пустыми настройками, файл цел."""
    path, original, refreshed = isolated_config
    path.write_text("   \n", encoding="utf-8")
    assert config._load_user_cfg() == {} and config._user_cfg == {}
    assert path.read_text(encoding="utf-8") == "   \n"


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "1e999", "broken"])
def test_nonfinite_numeric_settings_use_defaults(monkeypatch, value):
    monkeypatch.setattr(config, "_user_cfg", {"TEST_NUMBER": value})
    assert config.get_int("TEST_NUMBER", 3) == 3
    assert config.get_float("TEST_NUMBER", 0.5) == 0.5
