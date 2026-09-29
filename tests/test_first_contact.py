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
    f = TwinHint()
    rec = logging.LogRecord("aiogram.dispatcher", logging.ERROR, __file__, 1,
                            "Failed to fetch updates - %s: %s", ("TelegramConflictError", "terminated by other"), None)
    with caplog.at_level(logging.WARNING, logger="oracle"):
        assert f.filter(rec) is True and f.filter(rec) is True
    hints = [r for r in caplog.records if "запущен дважды" in r.getMessage()]
    assert len(hints) == 1                                # не чаще раза в 10 минут


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
