"""Сведение правок четырёх групп: запросы между модулями (обработчики ↔ инструменты ↔ промпт ↔ конфиг)
и мелкие дыры, найденные при проходе по сценариям владельца."""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from oracle import persona, timeutil
from oracle.agent import GATED_TOOLS, Agent, TurnGuard
from oracle.bot import handlers as H
from oracle.db import DB
from oracle.llm import human_error
from oracle.tools import base as tb
from test_bot import FakeUserbot, bot, cbq, deps, h, msg  # noqa: F401  (фикстуры)

UTC = timezone.utc
rem = importlib.import_module("oracle.tools.reminders")
bd = importlib.import_module("oracle.tools.birthdays")
importlib.import_module("oracle.tools.settings")


async def call(ctx: Any, name: str, **args: Any) -> dict:
    return json.loads(await tb.dispatch(name, args, ctx))


async def _bday(db: Any, tg: str = "masha_k") -> int:
    return await db.execute("INSERT INTO birthdays(name, month, day, year, relation, tg_username, created_at) "
                            "VALUES(?,?,?,?,?,?,?)", ("Маша", 9, 28, 1996, "сестра", tg,
                                                      "2026-01-01T00:00:00+00:00"))


async def _ringing_wake(ctx: Any, db: Any) -> int:
    row = await rem.create_reminder(ctx, text="Подъём", when="2026-09-29 06:30", kind="wake")
    await db.execute("UPDATE reminders SET nag_active=1 WHERE id=?", (row["id"],))
    return row["id"]


# ── поздравление: варианты и «Поздравить…» ──────────────────────────────────
async def test_bday_send_stops_congratulate_nag(h, deps, ctx, db, bot):
    deps.userbot = FakeUserbot()
    bid = await _bday(db)
    b = await bd.get_birthday(db, bid)
    nag_id = await bd.start_nag(ctx, b)
    assert nag_id and (await rem.get_reminder(db, nag_id))["status"] == "active"
    n = await bd.store_greeting(db, bid, "Маша, с днём рождения!")
    await h.on_callback(cbq(bot, f"bday:send:{bid}:{n}"))
    assert deps.userbot.sent == [(555, "Маша, с днём рождения!")]
    assert (await rem.get_reminder(db, nag_id))["status"] == "cancelled"      # поздравил — не долбит


async def test_regen_gives_variant_button_and_old_variant_still_sends(h, deps, db, bot, fake_llm):
    deps.userbot = FakeUserbot()
    deps.ctx.cfg = replace(deps.ctx.cfg, userbot_enabled=True)
    deps.ctx.services.userbot = deps.userbot
    bid = await _bday(db)
    fake_llm.script = ["Вариант один", "Вариант два"]
    await h.on_callback(cbq(bot, f"bday:regen:{bid}"))
    await h.on_callback(cbq(bot, f"bday:regen:{bid}"))
    sends = [row for s in deps.notifier.sent for row in (s["buttons"] or []) for _, d in row if d.startswith("bday:send")]
    assert [d for row in sends for _, d in [row[0]]] == [f"bday:send:{bid}:1", f"bday:send:{bid}:2"]
    await h.on_callback(cbq(bot, f"bday:send:{bid}:1"))                      # выбрал первый — уйдёт первый
    assert deps.userbot.sent == [(555, "Вариант один")]


async def test_greeting_variants_are_pruned_and_deleted_with_birthday(ctx, db):
    bid = await _bday(db)
    for i in range(bd.VARIANTS_KEEP + 3):
        await bd.store_greeting(db, bid, f"текст {i}")
    assert await db.kv_get(bd.variant_key(bid, 1)) is None
    assert await db.kv_get(bd.variant_key(bid, bd.VARIANTS_KEEP + 3)) == f"текст {bd.VARIANTS_KEEP + 2}"
    await db.kv_set(f"bday_sent:{bid}:2026", "x")
    await call(ctx, "delete_birthday", id=bid)
    left = await db.scalar("SELECT COUNT(*) FROM kv WHERE key LIKE ? OR key LIKE ? OR key LIKE ?",
                           (f"bday_greeting:{bid}%", f"bday_gen:{bid}", f"bday_sent:{bid}:%"))
    assert left == 0


async def test_person_facts_match_latvian_names(ctx, db):
    mem = importlib.import_module("oracle.tools.memory")
    await mem.add_fact(db, "Jānis Bērziņš любит рыбалку на Даугаве", "person")
    await mem.add_fact(db, "Маша не пьёт кофе", "person")
    facts = await bd._person_facts(db, "Jānis")
    assert facts == ["Jānis Bērziņš любит рыбалку на Даугаве"]


# ── будильник ────────────────────────────────────────────────────────────────
async def test_reminders_list_hides_cancel_for_ringing_challenge(h, ctx, db, notifier, bot):
    rid = await _ringing_wake(ctx, db)
    other = await rem.create_reminder(ctx, text="Позвонить маме", when="2026-09-28 18:00")
    await h.cmd_reminders(msg(bot, "/reminders"))
    datas = [d for row in notifier.sent[-1]["buttons"] for _, d in row]
    assert datas == [f"rem:del:{other['id']}"] and f"rem:del:{rid}" not in datas


async def test_wake_solved_in_the_evening_has_no_good_morning(h, ctx, db, notifier, bot, clock, monkeypatch):
    rid = await _ringing_wake(ctx, db)
    clock.set(datetime(2026, 9, 28, 17, 0, tzinfo=UTC))                          # 20:00 МСК
    monkeypatch.setattr(H, "make_challenge", lambda rng=None: ("20 + 22 = ?", 42, [41, 42, 52, 32]))
    await h.on_callback(cbq(bot, f"rem:done:{rid}"))
    await h.on_callback(cbq(bot, f"wake:ans:{rid}:42"))
    assert notifier.texts()[-1] == "✅ Проснулся."


async def test_first_wake_tells_about_do_not_disturb_once(ctx):
    a = await call(ctx, "create_reminder", text="Подъём", when="2026-09-29 07:00", kind="wake")
    b = await call(ctx, "create_reminder", text="Подъём", when="2026-09-30 07:00", kind="wake")
    r = await call(ctx, "create_reminder", text="Кофе", when="2026-09-29 08:00")
    assert "Не беспокоить" in a["note"] and "note" not in b and "note" not in r


async def test_snooze_by_voice(ctx, db, clock):
    r = await rem.create_reminder(ctx, text="Выпить таблетку", when="2026-09-28 09:00", nag=True)
    await db.execute("UPDATE reminders SET nag_active=1, next_at=NULL WHERE id=?", (r["id"],))
    s = await call(ctx, "snooze_reminder", id=r["id"], minutes=30)
    assert s["ok"] and s["until"] and "09:30" in s["until"]
    row = await rem.get_reminder(db, r["id"])
    assert row["nag_active"] == 0 and row["snooze_at"] and row["status"] == "active"
    # сработавшее разовое без долбёжки — «напомни через час ещё раз» тоже работает, как кнопка
    once = await rem.create_reminder(ctx, text="Кофе", when="2026-09-28 09:05")
    await db.execute("UPDATE reminders SET status='done', next_at=NULL, last_fired_at=? WHERE id=?",
                     (timeutil.iso(timeutil.now_utc()), once["id"]))
    assert (await call(ctx, "snooze_reminder", id=once["id"], minutes=60))["ok"]
    assert (await rem.get_reminder(db, once["id"]))["status"] == "active"
    await rem.cancel_reminder(db, once["id"])
    assert (await call(ctx, "snooze_reminder", id=once["id"]))["ok"] is False
    assert (await call(ctx, "snooze_reminder", id=r["id"], minutes=5000))["ok"] is False
    wake = await _ringing_wake(ctx, db)
    w = await call(ctx, "snooze_reminder", id=wake, minutes=5)
    assert w["ok"] and "задачкой" in w["note"]
    assert rem.ringing_challenge(await rem.get_reminder(db, wake))      # снять — всё равно только задачкой


async def test_prompt_lists_snoozed_challenge_alarm(ctx, db):
    rid = await _ringing_wake(ctx, db)
    await rem.snooze_reminder(db, rid, 5)
    system = await Agent(ctx)._system("привет", deep=False)
    assert f"#{rid} Подъём — отложен" in system and "только кнопкой" in system


# ── /status, /today, /news ───────────────────────────────────────────────────
async def test_status_shows_zone_in_use_and_bad_timezone(h, deps, notifier, bot):
    deps.cfg = replace(deps.cfg, timezone="Mars/Olympus")
    await h.cmd_status(msg(bot, "/status"))
    text = notifier.texts()[-1]
    assert "Часовой пояс: UTC (TIMEZONE='Mars/Olympus' не распознан)" in text


async def test_backup_file_is_private(h, deps, db, notifier, bot, monkeypatch):
    seen: dict = {}
    real = db.backup

    async def spy(dest: Any) -> int:
        n = await real(dest)
        seen["mode"] = os.stat(dest).st_mode & 0o777
        return n

    monkeypatch.setattr(db, "backup", spy)
    await h.cmd_backup(msg(bot, "/backup"))
    assert seen["mode"] == 0o600 and notifier.sent[-1]["kind"] == "file"


# ── настройки голосом ────────────────────────────────────────────────────────
async def test_voice_settings_tools(ctx, db):
    r = await call(ctx, "set_voice_replies", mode="always")
    assert r["ok"] and r["mode"] == "always" and await db.kv_get("tts_mode") == "always"
    assert "недоступна" in r["note"]                                     # озвучки в тестах нет
    ctx.services.tts = SimpleNamespace(available=lambda: True)
    r = await call(ctx, "set_voice_replies", mode="off")
    assert r["mode"] == "off" and "note" not in r and await db.kv_get("tts_mode") in H.TTS_MODES
    assert (await call(ctx, "set_voice_replies", mode="громко"))["ok"] is False
    r = await call(ctx, "set_thinking_mode", mode="deep")
    assert r["ok"] and await db.kv_get("mode") == "deep"
    await db.add_message("user", "старое", "text")
    r = await call(ctx, "reset_conversation")
    assert r["ok"] and r["dropped"] >= 1
    assert await db.scalar("SELECT COUNT(*) FROM messages WHERE summarized=0") == 0


def test_settings_tools_need_owner_request():
    g = TurnGuard(tainted=True)
    for name in ("reset_conversation", "set_voice_replies", "set_thinking_mode"):
        assert name in GATED_TOOLS and g.check(name, {}) is not None


# ── промпт ───────────────────────────────────────────────────────────────────
def test_persona_rules_for_new_tools_and_forwards():
    p = persona.PERSONA
    for needle in ("get_agenda", "only_next=true", "[Переслано от …]", "set_voice_replies", "set_thinking_mode",
                   "reset_conversation", "birthday_greeting(id)", "snooze_reminder", "deep_think_idea"):
        assert needle in p
    assert "сразу напиши живое поздравление" not in p       # противоречило add_birthday
    s = persona.build_system(name="Оракул", now="пн", owner_gender="f")
    assert persona.FEMALE_NOTE in s
    assert persona.FEMALE_NOTE not in persona.build_system(name="Оракул", now="пн")


def test_owner_gender_from_env(monkeypatch, tmp_path):
    from oracle import config
    monkeypatch.setenv("OWNER_GENDER", "ж")
    assert config.load(tmp_path / "none.env").owner_gender == "f"
    monkeypatch.setenv("OWNER_GENDER", "male")
    assert config.load(tmp_path / "none.env").owner_gender == "m"


# ── сеть и модель ────────────────────────────────────────────────────────────
async def test_net_tool_deadline(ctx, monkeypatch):
    async def slow(ctx: Any, **kw: Any) -> dict:
        await asyncio.sleep(5)
        return {"ok": True}

    monkeypatch.setitem(tb.REGISTRY, "get_weather", tb.Tool("get_weather", "погода", {"type": "object",
                                                            "properties": {}, "required": []}, slow))
    monkeypatch.setattr(tb, "NET_TOOL_DEADLINE", 0.05)
    r = json.loads(await tb.dispatch("get_weather", {}, ctx))
    assert r["ok"] is False and "не уложился" in r["error"]


def test_transport_error_mentions_base_url_for_local_models():
    import httpx
    e = httpx.ConnectError("refused")
    assert "интернет" in human_error(None, exc=e)[0]
    assert "LLM_BASE_URL" in human_error(None, exc=e, local=True)[0]


# ── база после аварии ────────────────────────────────────────────────────────
def _crashed_copy(tmp_path: Any, corrupt: bool) -> str:
    """Копия базы вместе с непустым -wal, снятая, пока связь открыта (как после падения)."""
    src = tmp_path / "live.db"
    conn = sqlite3.connect(src)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.executemany("INSERT INTO t VALUES (?)", [("строка " * 50,) for _ in range(400)])
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("INSERT INTO t VALUES ('после чекпойнта')")
    conn.commit()
    dst = tmp_path / "copy.db"
    shutil.copy(src, dst)
    shutil.copy(str(src) + "-wal", str(dst) + "-wal")
    conn.close()
    if corrupt:
        with open(dst, "r+b") as f:
            f.seek(4096 * 3)
            f.write(b"\xff" * 4096 * 4)
    return str(dst)


async def test_db_open_after_crash_checks_integrity(tmp_path):
    ok_path = _crashed_copy(tmp_path, corrupt=False)
    assert os.path.getsize(ok_path + "-wal") > 0
    d = await DB(ok_path).open()
    assert await d.scalar("SELECT COUNT(*) FROM t") == 401
    await d.close()
    bad = tmp_path / "bad"
    bad.mkdir()
    bad_path = _crashed_copy(bad, corrupt=True)
    broken = DB(bad_path)
    with pytest.raises(RuntimeError, match="повреждена.*/backup"):
        await broken.open()
    assert broken.conn is None                                   # связь закрыта — процесс не виснет
    assert os.path.exists(bad_path + "-wal")                     # сами ничего не удаляем


async def test_app_exits_cleanly_when_db_cannot_open(monkeypatch, cfg, capsys):
    from oracle import app
    monkeypatch.setattr(app.config, "load", lambda *a, **k: replace(cfg, bot_token="123456:TEST-token"))

    async def boom(self: Any) -> Any:
        raise RuntimeError("база повреждена")

    monkeypatch.setattr(DB, "open", boom)
    assert await app.main() == 1
    assert "база повреждена" in capsys.readouterr().err


# ── scheduler: дайджест в разговоре целиком ──────────────────────────────────
async def test_scheduler_records_whole_digest(ctx, db, clock, monkeypatch):
    sched = importlib.import_module("oracle.services.scheduler")
    tnews = importlib.import_module("oracle.tools.news")
    digest = "\n".join(f"{i}. Сюжет {i}: " + "подробности " * 30 for i in range(1, 9))
    assert 1500 < len(digest) < sched.RECORD_MAX

    async def fake(ctx: Any, topic: str = "", *, deep: bool = True) -> str:
        return digest

    monkeypatch.setattr(tnews, "news_digest", fake)
    s = sched.Scheduler(ctx)
    assert await s._job("news") is True
    rows = [r["content"] for r in await db.recent_messages(5) if r["role"] == "event"]
    assert rows and "Сюжет 8" in rows[-1]
