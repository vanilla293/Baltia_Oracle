"""Первое знакомство: владелец не теряется, двойной запуск подсказывается, голосом по умолчанию не отвечаем,
пока бот его не знает — тон сдержанный."""
from __future__ import annotations

import logging
from dataclasses import replace

from oracle import persona
from oracle.agent import Agent
from oracle.app import TwinHint, remember_owner
from oracle.config import Settings


async def test_owner_is_remembered_and_restored(cfg, db):
    c = replace(cfg, owner_id=123456789)
    assert (await remember_owner(c, db)).owner_id == 123456789
    lost = replace(cfg, owner_id=0)                       # .env перезаписан — OWNER_ID пропал
    assert (await remember_owner(lost, db)).owner_id == 123456789
    new = replace(cfg, owner_id=555)                      # явно вписан другой — он и главный
    assert (await remember_owner(new, db)).owner_id == 555
    assert (await remember_owner(lost, db)).owner_id == 555


async def test_fresh_db_without_owner_stays_in_setup(cfg, db):
    assert (await remember_owner(replace(cfg, owner_id=0), db)).owner_id == 0


def test_twin_hint_on_conflict(caplog):
    """Конфликт двух копий: английские строки aiogram скрыты, подсказка по-русски — раз в 10 минут."""
    now = [1000.0]
    f = TwinHint(clock=lambda: now[0])
    err = logging.LogRecord("aiogram.dispatcher", logging.ERROR, __file__, 1,
                            "Failed to fetch updates - %s: %s", ("TelegramConflictError", "terminated by other"), None)
    sleep = logging.LogRecord("aiogram.dispatcher", logging.WARNING, __file__, 1,
                              "Sleep for %f seconds and try again... (tryings = %d, bot id = %d)", (1.0, 0, 1), None)
    with caplog.at_level(logging.WARNING, logger="oracle"):
        for _ in range(5):
            assert f.filter(err) is False and f.filter(sleep) is False
            now[0] += 10
        now[0] += 700                                     # через 10+ минут — одно напоминание
        assert f.filter(err) is False
    hints = [r for r in caplog.records if "запущен дважды" in r.getMessage()]
    assert len(hints) == 2
    # конфликт давно прошёл — обычные сетевые «Sleep for» снова видны
    now[0] += 120
    assert f.filter(sleep) is True
    other = logging.LogRecord("aiogram.dispatcher", logging.ERROR, __file__, 1,
                              "Failed to fetch updates - %s: %s", ("TelegramNetworkError", "timeout"), None)
    assert f.filter(other) is True


def test_voice_replies_off_by_default():
    assert Settings().tts_default == "off"


def test_acquaintance_note_only_for_new_owner():
    base = dict(name="Оракул", now="2026-09-29 10:00")
    assert persona.ACQUAINTANCE_NOTE in persona.build_system(**base, new_owner=True)
    assert persona.ACQUAINTANCE_NOTE not in persona.build_system(**base, new_owner=False)
    assert "не больше одного вопроса" in persona.PERSONA and "не пересказывай" in persona.PERSONA


async def test_agent_marks_new_owner_until_it_knows_him(ctx, fake_llm):
    fake_llm.script = ["Привет."]
    await Agent(ctx).handle("привет")
    assert persona.ACQUAINTANCE_NOTE in fake_llm.calls[-1]["messages"][0]["content"]
    for i in range(5):
        await ctx.db.execute("INSERT INTO facts(content, category, source, created_at, updated_at) VALUES(?,?,?,?,?)",
                             (f"факт номер {i} про него", "general", "chat", "2026-09-28T06:00:00+00:00",
                              "2026-09-28T06:00:00+00:00"))
    fake_llm.script = ["Ага."]
    await Agent(ctx).handle("как дела")
    assert persona.ACQUAINTANCE_NOTE not in fake_llm.calls[-1]["messages"][0]["content"]


async def test_unconfigured_copy_says_so_on_buttons():
    """Копия без OWNER_ID на нажатие кнопки говорит, что она не настроена, — не безликое «Нет доступа»."""
    from types import SimpleNamespace
    from aiogram.types import CallbackQuery
    from oracle.bot.middleware import SETUP_BUTTON, OwnerOnly
    answered = []

    class CB(CallbackQuery):
        async def answer(self, text=None, show_alert=None, **kw):   # type: ignore[override]
            answered.append((text, show_alert))

    from aiogram.types import User
    cb = CB(id="1", from_user=User(id=5, is_bot=False, first_name="x"), chat_instance="c", data="idea:deep:1")
    guard = OwnerOnly(Settings(owner_id=0))
    async def handler(e, d):
        raise AssertionError("не должен дойти до обработчика")
    await guard(handler, cb, {})
    assert answered == [(SETUP_BUTTON, True)]


def test_env_example_has_each_setting_once():
    """Каждая настройка в .env.example — ровно одной строкой (дубль молча перебил бы первую)."""
    import collections
    import re
    from pathlib import Path
    s = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    keys = re.findall(r"(?m)^([A-Z][A-Z0-9_]*)=", s)
    assert [k for k, n in collections.Counter(keys).items() if n > 1] == []
    assert [ln for ln in s.splitlines() if ln and not ln.startswith("#") and "=" not in ln] == []


def test_empty_os_env_does_not_hide_env_file(tmp_path, monkeypatch):
    """Пустая системная переменная OWNER_ID не перебивает вписанный в .env id; непустая — главнее."""
    from oracle import config
    env = tmp_path / "o.env"
    env.write_text("OWNER_ID=173682354\n", encoding="utf-8")
    monkeypatch.setenv("OWNER_ID", "")
    c = config.load(env)
    assert c.owners == (173682354,) and config.LOADED_ENV == env
    monkeypatch.setenv("OWNER_ID", "555")
    assert config.load(env).owners == (555,)
    monkeypatch.delenv("OWNER_ID", raising=False)


def test_env_duplicates_bom_quotes_spaces(tmp_path, monkeypatch):
    """Дописал OWNER_ID сверху, а пустая строка осталась ниже; BOM, кавычки, пробелы — id всё равно читается."""
    from oracle import config
    env = tmp_path / "d.env"
    env.write_bytes("﻿OWNER_ID = \"173682354\"  \n# коммент\nBOT_NAME=Оракул # имя\nOWNER_ID=\n".encode("utf-8"))
    for k in ("OWNER_ID", "BOT_NAME"):
        monkeypatch.delenv(k, raising=False)
    c = config.load(env)
    assert c.owners == (173682354,) and c.bot_name == "Оракул"
    for k in ("OWNER_ID", "BOT_NAME"):
        monkeypatch.delenv(k, raising=False)
