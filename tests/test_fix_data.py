"""Регрессии слоя данных v3: busy_timeout, индексный дедуп фактов, одна транзакция строка+индекс,
несклейка фактов о разных людях (в т.ч. имена с маленькой буквы), необязательная чистка истории."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from oracle import db as dbmod
from oracle import usage
from oracle.db import DB
from oracle.tools import memory as mem


async def count(db, table: str) -> int:
    return int(await db.scalar(f"SELECT COUNT(*) FROM {table}"))


# ── A14: busy_timeout, чтобы мгновенная блокировка ждала, а не падала ──────────
async def test_busy_timeout_pragma_set(db):
    assert int(await db.scalar("PRAGMA busy_timeout")) == 5000


async def test_busy_timeout_set_on_memory_db_too():
    d = await DB(":memory:").open()
    try:
        assert int(await d.scalar("PRAGMA busy_timeout")) == 5000
    finally:
        await d.close()


# ── A23: точный дубль ищется индексом norm_key, а не перебором всей таблицы ────
async def test_exact_dup_lookup_uses_index_not_full_scan(db):
    plan = await db.fetchall(
        "EXPLAIN QUERY PLAN SELECT id, content, category FROM facts WHERE norm_key=? ORDER BY id LIMIT 1",
        ("любит кофе",))
    detail = " ".join(str(v) for r in plan for v in r.values()).lower()
    assert "ix_facts_norm" in detail          # использован индекс…
    assert "scan facts" not in detail         # …а не скан всей таблицы


async def test_exact_dedup_still_works_on_large_table(db, clock):
    # даже когда фактов много, точный повтор находится и не плодит дублей (и делает это по индексу)
    for i in range(200):
        await mem.add_fact(db, f"Факт номер {i}: любимое число {i * 7 + 1000}", "general")
    target, created = await mem.add_fact(db, "Любит крепкий кофе без сахара", "preference")
    assert created
    again, created2 = await mem.add_fact(db, "любит крепкий КОФЕ без сахара!!!")   # тот же факт, иначе набран
    assert again == target and created2 is False
    assert await count(db, "facts") == 201
    row = await db.fetchone("SELECT norm_key FROM facts WHERE id=?", (target,))
    assert row["norm_key"] == "любит крепкий кофе без сахара"


async def test_near_dup_replaces_and_reindexes(db, clock):
    fid, _ = await mem.add_fact(db, "Любит крепкий кофе без сахара", "preference")
    fid2, created = await mem.add_fact(db, "Любит крепкий кофе без сахара по утрам")
    assert fid2 == fid and created is False
    assert (await mem.get_fact(db, fid))["content"] == "Любит крепкий кофе без сахара по утрам"
    assert (await mem.get_fact(db, fid))["norm_key"] == "любит крепкий кофе без сахара по утрам"
    assert await db.search("fact", "утром") == [fid]
    assert await count(db, "facts") == 1


# ── A33: строка факта и её текст в поиске пишутся одной транзакцией ────────────
async def test_new_fact_row_and_index_are_one_transaction(db, monkeypatch):
    async def boom(c, kind, ref_id, body):
        raise RuntimeError("падение между записью строки и записью в индекс")
    monkeypatch.setattr(db, "index_put_tx", boom)
    with pytest.raises(RuntimeError):
        await mem.add_fact(db, "Любит зелёный чай без сахара")
    assert await count(db, "facts") == 0                 # строка откатилась вместе с индексом
    assert await db.search("fact", "чай") == []


async def test_near_dup_update_and_index_are_one_transaction(db, monkeypatch, clock):
    fid, _ = await mem.add_fact(db, "Любит крепкий кофе без сахара", "preference")

    async def boom(c, kind, ref_id, body):
        raise RuntimeError("падение при переиндексации")
    monkeypatch.setattr(db, "index_put_tx", boom)
    with pytest.raises(RuntimeError):
        await mem.add_fact(db, "Любит крепкий кофе без сахара по утрам")
    # формулировка не должна остаться переписанной, если индекс не обновился
    assert (await mem.get_fact(db, fid))["content"] == "Любит крепкий кофе без сахара"
    assert await db.search("fact", "сахар") == [fid]


async def test_opinion_row_and_index_are_one_transaction(db, monkeypatch):
    async def boom(c, kind, ref_id, body):
        raise RuntimeError("падение между записью позиции и индексом")
    monkeypatch.setattr(db, "index_put_tx", boom)
    with pytest.raises(RuntimeError):
        await mem.upsert_opinion(db, topic="Криптовалюты", stance="Спекуляция", confidence=60)
    assert await count(db, "opinions") == 0
    assert await db.search("opinion", "криптовалюта") == []


# ── A21: факты о разных людях не сливаются, даже если имя с маленькой буквы ────
async def test_lowercased_name_not_overwritten_by_self_fact(db, clock):
    ania = "аня не любит острую пищу из ресторанов"
    mineself = "не любит острую пищу из ресторанов"
    a, ca = await mem.add_fact(db, ania, "person")
    b, cb = await mem.add_fact(db, mineself, "preference")   # Жаккар 0.8, но подлежащее другое
    assert ca and cb and a != b
    assert {f["content"] for f in await mem.all_facts(db)} == {ania, mineself}
    assert (await mem.get_fact(db, a))["content"] == ania    # факт про Аню цел


async def test_two_lowercased_names_stay_separate(db, clock):
    base = " давно не любит очень острую пищу из местных ресторанов и кафе"
    # без исправления эти двое сливались: Жаккар ≥ порога, а имена в нижнем регистре не ловились
    assert mem.jaccard(mem.stems("аня" + base), mem.stems("оля" + base)) >= mem.NEAR_DUP
    fa, ca = await mem.add_fact(db, "аня" + base, "person")
    fb, cb = await mem.add_fact(db, "оля" + base, "person")
    assert ca and cb and fa != fb
    assert (await mem.get_fact(db, fa))["content"].startswith("аня")
    assert (await mem.get_fact(db, fb))["content"].startswith("оля")
    assert await count(db, "facts") == 2


async def test_same_subject_reformulation_still_merges(db, clock):
    # исправление не должно мешать честной переформулировке про одного и того же (подлежащее то же)
    fid, _ = await mem.add_fact(db, "Работает бэкенд-разработчиком в банке", "work")
    fid2, created = await mem.add_fact(db, "Работает бэкенд-разработчиком в крупном банке")
    assert fid2 == fid and created is False


# ── A23/миграция: старой базе без norm_key колонка добавляется и заполняется ───
async def test_norm_key_added_and_backfilled_for_old_db(tmp_path, clock):
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.executescript(
        "CREATE TABLE facts (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, "
        "category TEXT NOT NULL DEFAULT 'general', source TEXT NOT NULL DEFAULT 'chat', "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
        "INSERT INTO facts(content, category, created_at, updated_at) "
        "VALUES ('Любит крепкий кофе.', 'preference', 't', 't');")
    c.commit()
    c.close()
    d = await DB(p).open()
    try:
        cols = {r["name"] for r in await d.fetchall("PRAGMA table_info(facts)")}
        assert "norm_key" in cols
        assert (await d.fetchone("SELECT norm_key FROM facts"))["norm_key"] == "любит крепкий кофе"
        # и теперь точный дубль старой строки ловится индексным поиском, без нового ряда
        fid, created = await mem.add_fact(d, "любит крепкий КОФЕ!!!")
        assert created is False and await count(d, "facts") == 1
    finally:
        await d.close()


# ── A22: необязательная чистка истории; долговременная память не трогается ─────
async def test_prune_old_trims_history_but_keeps_memory(db, clock):
    await mem.add_fact(db, "Жену зовут Маша", "person")
    await mem.upsert_opinion(db, topic="Кофе", stance="Норм")
    await mem.add_journal(db, "заметка на ночь")

    for i in range(30):
        await db.add_message("user", f"старое {i}")
    await db.execute("UPDATE messages SET summarized=1")            # всё старое уже свёрнуто
    live1 = await db.add_message("user", "свежее один")            # актуальный контекст — не трогать
    live2 = await db.add_message("assistant", "свежее два")

    for i in range(10):
        sid = await db.execute("INSERT INTO summaries(content, from_id, to_id, created_at) VALUES(?,?,?,?)",
                               (f"конспект {i}", 1, 1, "2026-09-01T00:00:00+00:00"))
        await db.index_put("summary", sid, f"конспект {i}")

    await usage.record(db, model="deepseek-flash", usage={"prompt_tokens": 10, "completion_tokens": 5},
                       at=datetime(2026, 1, 1, tzinfo=timezone.utc))                # давно
    await usage.record(db, model="deepseek-flash", usage={"prompt_tokens": 10, "completion_tokens": 5})  # сегодня

    res = await dbmod.prune_old(db, keep_messages=5, keep_summaries=3, keep_days_usage=30)

    assert res == {"messages": 27, "summaries": 7, "llm_usage": 1}
    # реплики: осталось ровно 5, оба несвёрнутых свежих целы
    assert await count(db, "messages") == 5
    assert int(await db.scalar("SELECT COUNT(*) FROM messages WHERE summarized=0")) == 2
    assert await db.fetchone("SELECT id FROM messages WHERE id=?", (live1,)) is not None
    assert await db.fetchone("SELECT id FROM messages WHERE id=?", (live2,)) is not None
    # конспекты обрезаны до 3, вместе с их строками в поиске
    assert await count(db, "summaries") == 3
    assert int(await db.scalar("SELECT COUNT(*) FROM search_index WHERE kind='summary'")) == 3
    # учёт расходов: старое ушло, сегодняшнее осталось
    assert await count(db, "llm_usage") == 1
    # долговременная память нетронута
    assert await count(db, "facts") == 1
    assert await count(db, "opinions") == 1
    assert await count(db, "journal") == 1


async def test_prune_old_is_noop_on_small_db(db, clock):
    await db.add_message("user", "одно сообщение")
    await mem.add_fact(db, "Работает в банке", "work")
    res = await dbmod.prune_old(db)                    # значения по умолчанию — резать нечего
    assert res == {"messages": 0, "summaries": 0, "llm_usage": 0}
    assert await count(db, "messages") == 1 and await count(db, "facts") == 1


async def test_prune_old_never_deletes_unsummarized_messages(db, clock):
    for i in range(20):
        await db.add_message("user", f"свежак {i}")     # ничего не свёрнуто — весь контекст живой
    res = await dbmod.prune_old(db, keep_messages=5)
    assert res["messages"] == 0                          # несвёрнутое не режем никогда
    assert await count(db, "messages") == 20


# ── A22: горячие запросы рефлексии/тихой проверки идут по индексу ──────────────
async def test_messages_role_time_index_used(db):
    names = {r["name"] for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "ix_messages_role_time" in names
    plan = await db.fetchall(
        "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM messages WHERE role=? AND created_at >= ?",
        ("user", "2026-01-01"))
    detail = " ".join(str(v) for r in plan for v in r.values()).lower()
    assert "ix_messages_role_time" in detail
