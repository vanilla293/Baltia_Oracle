"""Схема настроек: покрытие всех полей .env, маскирование секретов, проверка ввода и сборка
изменений для .env. Ни сети, ни файлов — только данные.
"""
from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from oracle import config
from oracle.config import Settings
from oracle import settings_schema as ss
from oracle.settings_schema import (SECTION_ORDER, SETTINGS, get_setting, is_mask, mask_secret,
                                    masked_values, schema_public, sections, to_env_updates, validate)

KEYS = {s.key for s in SETTINGS}

# многопользовательские поля v3 убирает, ENV_FILE — служебная (не пишется в .env)
_REMOVED = {"ALLOW_REQUESTS", "ENV_FILE"}
# новые поля v3, которых ещё нет в config.py (их добавит интегратор)
_NEW_V3 = {"DASHBOARD_HOST", "DASHBOARD_PORT", "DASHBOARD_OPEN", "FILES_ROOTS",
           "FILES_WORKSPACE", "USAGE_PEAK_PRICING", "SINGLE_INSTANCE"}


def _config_env_names() -> set[str]:
    """Имена переменных окружения, которые читает oracle/config.py (по вызовам _get/_bool/…)."""
    text = Path(config.__file__).read_text("utf-8")
    names = set(re.findall(r'_(?:get|bool|int|float|opt_float|list|hhmm)\(\s*"([A-Z_][A-Z0-9_]*)"',
                           text))
    return names


# ── покрытие ─────────────────────────────────────────────────────────────────────
def test_every_config_field_present():
    expected = _config_env_names() - _REMOVED
    missing = sorted(expected - KEYS)
    assert not missing, f"в схеме нет полей из config.py: {missing}"


def test_removed_multiuser_field_absent():
    assert "ALLOW_REQUESTS" not in KEYS       # версия на одного человека


def test_new_v3_settings_present_and_placed():
    for k in _NEW_V3:
        assert k in KEYS, f"нет новой настройки {k}"
    assert get_setting("DASHBOARD_PORT").section == "Панель"
    assert get_setting("FILES_WORKSPACE").section == "Файлы"
    assert get_setting("SINGLE_INSTANCE").section == "Прочее"


def test_keys_unique_and_sections_known():
    keys = [s.key for s in SETTINGS]
    assert len(keys) == len(set(keys)), "дубли ключей"
    for s in SETTINGS:
        assert s.section in SECTION_ORDER, f"{s.key}: неизвестная секция {s.section}"


def test_sections_order_and_nonempty():
    got = [sec["name"] for sec in sections()]
    assert got == [n for n in SECTION_ORDER]         # порядок как задан, все непустые
    for sec in sections():
        assert sec["settings"], f"пустая секция {sec['name']}"


def test_schema_public_shape():
    for s in schema_public():
        assert set(s) >= {"key", "title", "help", "type", "choices", "default", "secret", "section"}
        assert s["type"] in {"str", "int", "float", "bool", "secret", "choice", "time"}
        if s["type"] == "choice":
            assert s["choices"], f"{s['key']}: choice без вариантов"


# ── маскирование ─────────────────────────────────────────────────────────────────
def test_mask_secret_shows_edges_only():
    assert mask_secret("sk-abcdef0123456789c4d5") == "sk-…c4d5"
    assert mask_secret("") == ""
    assert mask_secret("short") == "…rt"
    assert "…" in mask_secret("1234567890abcdef")
    assert is_mask("sk-…c4d5") and not is_mask("sk-realvalue")


def test_masked_values_hide_secrets_show_plain(cfg):
    c = replace(cfg, bot_token="123456789:AA" + "x" * 30, llm_api_key="sk-secret000000000000c4d5",
                bot_name="Оракул", owner_id=42)
    vals = masked_values(c)
    assert set(vals) == KEYS                          # все поля присутствуют
    assert is_mask(vals["BOT_TOKEN"]) and vals["BOT_TOKEN"].endswith("xxxx")
    assert vals["DEEPSEEK_API_KEY"] == "sk-…c4d5"     # ключ виден только маской
    assert vals["OWNER_NAME"] == ""                   # не задано
    assert vals["OWNER_ID"] == "42"                   # обычное значение — как есть
    assert vals["BOT_NAME"] == "Оракул"


def test_masked_values_getattr_fallback_for_new_fields(cfg):
    # у cfg ещё нет полей v3 — берутся значения по умолчанию через getattr
    vals = masked_values(cfg)
    assert vals["DASHBOARD_HOST"] == "127.0.0.1"
    assert vals["DASHBOARD_PORT"] == "8765"
    assert vals["DASHBOARD_OPEN"] == "1"
    assert vals["SINGLE_INSTANCE"] == "1"
    assert vals["USAGE_PEAK_PRICING"] == "1"
    assert vals["FILES_WORKSPACE"].endswith("Oracle")


def test_masked_values_bool_and_blank_zero(cfg):
    c = replace(cfg, show_transcript=True, web_search=False, owner_id=0, weather_lat=None)
    vals = masked_values(c)
    assert vals["SHOW_TRANSCRIPT"] == "1"
    assert vals["WEB_SEARCH"] == "0"
    assert vals["OWNER_ID"] == ""                     # 0 = не задано
    assert vals["WEATHER_LAT"] == ""


def test_masked_values_news_feeds_default_blank(cfg):
    c = replace(cfg, news_feeds=config.DEFAULT_FEEDS)
    assert masked_values(c)["NEWS_FEEDS"] == ""       # встроенный набор = пусто
    c2 = replace(cfg, news_feeds=("https://a/rss", "https://b/rss"))
    assert masked_values(c2)["NEWS_FEEDS"] == "https://a/rss, https://b/rss"


# ── проверка ввода ─────────────────────────────────────────────────────────────────
def test_validate_unknown_key():
    ok, err = validate("NOPE", "x")
    assert not ok and "неизвестн" in err.lower()


def test_validate_int_and_ranges():
    assert validate("LLM_MAX_STEPS", "3") == (True, "3")
    assert validate("LLM_MAX_STEPS", "") == (True, "")       # пусто = умолчание
    assert validate("LLM_MAX_STEPS", "0")[0] is False
    assert validate("HISTORY_MESSAGES", "2")[0] is False     # < 4
    assert validate("DASHBOARD_PORT", "70000")[0] is False
    assert validate("DASHBOARD_PORT", "8765") == (True, "8765")
    assert validate("OWNER_ID", "-5")[0] is False
    assert validate("OWNER_ID", "abc")[0] is False


def test_validate_float():
    assert validate("LLM_TEMPERATURE", "0,5") == (True, "0.5")   # запятая → точка
    assert validate("LLM_TEMPERATURE", "1.0") == (True, "1")
    assert validate("LLM_TEMPERATURE", "3")[0] is False
    assert validate("WEATHER_LAT", "95")[0] is False
    assert validate("WEATHER_LAT", "54.71") == (True, "54.71")


def test_validate_bool():
    for v in ("1", "true", "да", "on", "вкл"):
        assert validate("WEB_SEARCH", v) == (True, "1")
    for v in ("0", "нет", "off", ""):
        assert validate("WEB_SEARCH", v) == (True, "0")
    assert validate("WEB_SEARCH", "ага")[0] is False


def test_validate_time():
    assert validate("MORNING_BRIEF_TIME", "8:5") == (True, "08:05")
    assert validate("MORNING_BRIEF_TIME", "off") == (True, "")
    assert validate("MORNING_BRIEF_TIME", "") == (True, "")
    assert validate("MORNING_BRIEF_TIME", "25:00")[0] is False
    assert validate("MORNING_BRIEF_TIME", "мусор")[0] is False


def test_validate_choice():
    assert validate("STT_PROVIDER", "GROQ") == (True, "groq")    # регистр не важен
    assert validate("LLM_DEEP_EFFORT", "auto") == (True, "auto")
    assert validate("STT_PROVIDER", "нет_такого")[0] is False
    assert validate("LOG_LEVEL", "info") == (True, "INFO")


def test_validate_timezone_and_url():
    assert validate("TIMEZONE", "MSK") == (True, "Europe/Moscow")
    assert validate("TIMEZONE", "UTC+3")[0] is True
    assert validate("TIMEZONE", "Мордор")[0] is False
    assert validate("TIMEZONE", "")[0] is False
    assert validate("LLM_BASE_URL", "ftp://x")[0] is False
    assert validate("LLM_BASE_URL", "https://api.deepseek.com")[0] is True


def test_validate_secret_passthrough():
    assert validate("BOT_TOKEN", "  abc  ") == (True, "abc")
    assert validate("BOT_TOKEN", "sk-…c4d5")[0] is True          # маска — валидна (не меняли)


# ── форма → изменения .env ───────────────────────────────────────────────────────
def test_to_env_updates_skips_masks_and_unchanged():
    current = {"BOT_NAME": "Оракул", "BOT_TOKEN": "sk-…c4d5", "LLM_TEMPERATURE": "1"}
    form = {
        "BOT_NAME": "Оракул",           # не менялось
        "BOT_TOKEN": "sk-…c4d5",        # секрет остался маской
        "LLM_TEMPERATURE": "0.7",       # изменилось
    }
    upd = to_env_updates(form, current)
    assert upd == {"LLM_TEMPERATURE": "0.7"}


def test_to_env_updates_new_secret_included():
    current = {"BOT_TOKEN": "sk-…c4d5"}
    upd = to_env_updates({"BOT_TOKEN": "123456:NEWTOKENVALUE0000000000000"}, current)
    assert upd == {"BOT_TOKEN": "123456:NEWTOKENVALUE0000000000000"}


def test_to_env_updates_clear_sets_none():
    current = {"WEATHER_CITY": "Калининград", "GROQ_API_KEY": "gsk_realkeyvalue000"}
    upd = to_env_updates({"WEATHER_CITY": "", "GROQ_API_KEY": ""}, current)
    assert upd["WEATHER_CITY"] is None           # поле очистили → стереть строку
    assert upd["GROQ_API_KEY"] is None           # секрет был задан и очищен


def test_to_env_updates_ignores_unknown_and_invalid():
    upd = to_env_updates({"NOPE": "x", "LLM_TEMPERATURE": "999"}, {"LLM_TEMPERATURE": "1"})
    assert upd == {}                              # неизвестное и невалидное отброшены


def test_to_env_updates_bool_normalized_change():
    current = {"WEB_SEARCH": "1"}
    # выключение пишется явным «0» (стереть строку нельзя — умолчание вернуло бы True)
    assert to_env_updates({"WEB_SEARCH": "нет"}, current) == {"WEB_SEARCH": "0"}
    assert to_env_updates({"WEB_SEARCH": "1"}, current) == {}                       # не менялось


def test_to_env_updates_without_current_treats_masks_only():
    # без current: секрет-маска пропускается, обычное поле считается изменившимся
    upd = to_env_updates({"BOT_TOKEN": "sk-…c4d5", "BOT_NAME": "Имя"})
    assert upd == {"BOT_NAME": "Имя"}
