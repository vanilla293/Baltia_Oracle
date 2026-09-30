"""Три доводки устойчивости после аудита: копия базы не блокирует бота, напоминание не долбит при сбое записи,
/reset ждёт конспектирование."""
from __future__ import annotations

from datetime import timedelta

import pytest

from oracle import timeutil
from oracle.db import DB


async def test_backup_uses_separate_connection(tmp_path, monkeypatch):
    db = await DB(tmp_path / "b.db").open()
    try:
        await db.kv_set("x", {"a": 1})
        seen = {}
        orig = db.conn.execute

        async def watch(sql, *a, **k):
            if "VACUUM" in sql.upper():
                seen["main_vacuum"] = True     # VACUUM не должен идти по основному соединению
            return await orig(sql, *a, **k)

        monkeypatch.setattr(db.conn, "execute", watch)
        dest = tmp_path / "copy.db"
        size = await db.backup(dest)
        assert size > 0 and dest.exists()
        assert "main_vacuum" not in seen       # шёл на отдельном соединении
        # база-копия открывается и содержит данные
        db2 = await DB(dest).open()
        try:
            assert await db2.kv_get("x") == {"a": 1}
        finally:
            await db2.close()
    finally:
        await db.close()


async def test_reminder_not_refired_when_state_write_fails(ctx, clock, monkeypatch):
    from oracle.services import scheduler as sch
    from oracle.tools import reminders

    now = timeutil.now_utc()
    await reminders.create_reminder(ctx, text="позвонить маме",
                                    when=(now + timedelta(minutes=1)).astimezone(ctx.tz).strftime("%Y-%m-%d %H:%M"))
    s = sch.Scheduler(ctx)
    clock.advance(minutes=2)
    # запись состояния после доставки срывается (база занята)
    orig = ctx.db.execute
    calls = {"n": 0}

    async def flaky(sql, params=()):
        if sql.strip().upper().startswith("UPDATE REMINDERS SET FIRE_COUNT"):
            calls["n"] += 1
            raise RuntimeError("database is locked")
        return await orig(sql, params)

    monkeypatch.setattr(ctx.db, "execute", flaky)
    await s.tick()
    fired1 = [t for t in ctx.services.notifier.texts() if "позвонить маме" in t]
    assert len(fired1) == 1 and calls["n"] == 1         # доставлено один раз, запись сорвалась
    await s.tick()                                       # следующий тик — НЕ долбит
    fired2 = [t for t in ctx.services.notifier.texts() if "позвонить маме" in t]
    assert len(fired2) == 1, f"повтор каждый тик: {len(fired2)}"
    # спустя окно FIRE_GUARD пробует снова
    clock.advance(minutes=6)
    monkeypatch.setattr(ctx.db, "execute", orig)        # база «отпустила»
    await s.tick()
    fired3 = [t for t in ctx.services.notifier.texts() if "позвонить маме" in t]
    assert len(fired3) == 2 and calls["n"] == 1


async def test_reset_waits_for_summarization(ctx):
    from oracle.agent import Agent
    a = Agent(ctx)
    await a._sum_lock.acquire()      # как будто конспектирование в процессе
    import asyncio
    task = asyncio.ensure_future(a.reset_context())
    await asyncio.sleep(0.05)
    assert not task.done()           # reset ждёт блокировку конспектирования
    a._sum_lock.release()
    assert await task >= 0
