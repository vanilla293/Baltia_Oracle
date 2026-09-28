"""Offline regressions: startup must never destroy a database on failure."""
import contextlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from backend import memory, store_v5


@pytest.mark.parametrize("module,schema", [(store_v5, "_schema"), (memory, "_create_schema")])
@pytest.mark.parametrize("error", [sqlite3.OperationalError("database is locked"),
                                  sqlite3.OperationalError("database or disk is full"),
                                  PermissionError("read only"), ValueError("migration bug")])
def test_init_failure_preserves_database_and_sidecars(tmp_path, monkeypatch, module, schema, error):
    db = tmp_path / "history.db"
    originals = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = Path(str(db) + suffix)
        originals[path] = ("valuable history " + suffix).encode()
        path.write_bytes(originals[path])
    monkeypatch.setattr(module, "DB_PATH", db)
    with patch.object(module, schema, side_effect=error) as initialize:
        with pytest.raises((RuntimeError, ValueError)):
            module._init()
    assert initialize.call_count == 1
    assert {p: p.read_bytes() for p in originals} == originals


@pytest.mark.parametrize("module", [store_v5, memory])
def test_corrupt_database_is_set_aside_and_app_starts(tmp_path, monkeypatch, module):
    """Настоящее повреждение файла: база с хвостами откладывается в .broken-<время>, приложение стартует с чистой."""
    db = tmp_path / "history.db"
    original = b"unreadable but potentially recoverable history" * 100
    db.write_bytes(original)
    monkeypatch.setattr(module, "DB_PATH", db)
    module._init()
    aside = sorted(tmp_path.glob("history.db.broken-*"))
    assert len(aside) == 1 and aside[0].read_bytes() == original
    with contextlib.closing(sqlite3.connect(db)) as con:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0] > 0
    module._init()   # повторный старт — обычный, ничего больше не откладывается
    assert len(sorted(tmp_path.glob("history.db.broken-*"))) == 1


@pytest.mark.parametrize("module", [store_v5, memory])
def test_set_aside_moves_database_with_sidecars(tmp_path, monkeypatch, module):
    """Хвосты -wal/-shm/-journal уезжают вместе с базой под одной меткой; SQLite сам может убрать негодный -wal при открытии,
    поэтому проверяем перенос напрямую."""
    db = tmp_path / "history.db"
    for suffix, body in (("", b"main"), ("-wal", b"wal tail"), ("-shm", b"shm"), ("-journal", b"journal")):
        Path(str(db) + suffix).write_bytes(body)
    aside = module._set_aside(db)
    tag = aside[len(str(db)):]
    assert tag.startswith(".broken-")
    for suffix, body in (("", b"main"), ("-wal", b"wal tail"), ("-shm", b"shm"), ("-journal", b"journal")):
        assert not Path(str(db) + suffix).exists()
        assert Path(str(db) + suffix + tag).read_bytes() == body


@pytest.mark.parametrize("module,schema", [(store_v5, "_schema"), (memory, "_create_schema")])
def test_locked_database_is_not_treated_as_corrupt(tmp_path, monkeypatch, module, schema):
    db = tmp_path / "history.db"
    db.write_bytes(b"valuable history")
    monkeypatch.setattr(module, "DB_PATH", db)
    with patch.object(module, schema, side_effect=sqlite3.DatabaseError("database is locked")):
        with pytest.raises(RuntimeError, match="Файлы базы не удалены"):
            module._init()
    assert db.read_bytes() == b"valuable history" and not list(tmp_path.glob("*.broken-*"))


def test_legacy_memory_migration_keeps_analysis(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    with contextlib.closing(sqlite3.connect(db)) as con:
        con.execute("CREATE TABLE analyses (ticker TEXT PRIMARY KEY, analyst TEXT)")
        con.execute("INSERT INTO analyses VALUES ('SBER', 'saved analysis')")
        con.commit()
    monkeypatch.setattr(memory, "DB_PATH", db)
    memory._init()
    memory._init()
    with memory._conn() as con:
        row = con.execute("SELECT analyst, thinking_json FROM analyses").fetchone()
        assert tuple(row) == ("saved analysis", None)


def test_news_duplicate_does_not_reset_existing_ai_or_relevance(tmp_path, monkeypatch):
    monkeypatch.setattr(store_v5, "DB_PATH", tmp_path / "news.db")
    store_v5._init()
    item = {"id": "story-a", "title": "first version"}
    assert store_v5.news_upsert_raw_ids([item, item]) == ["story-a"]
    store_v5.news_set_relevant(["story-a"], 1)
    store_v5.news_ai_put("story-a", {"tone": 7})
    assert store_v5.news_upsert_raw_ids([dict(item, title="duplicate")]) == []
    row = store_v5.news_window(1)[0]
    assert row["title"] == "first version"
    assert row["relevant"] == 1 and row["ai"] == {"tone": 7}


def test_concurrent_collectors_insert_each_story_once(tmp_path, monkeypatch):
    monkeypatch.setattr(store_v5, "DB_PATH", tmp_path / "news.db")
    store_v5._init()
    items = [{"id": f"story-{i:03}", "title": f"story {i}"} for i in range(40)]
    ready = threading.Barrier(4)
    # Synchronize the first read in the old SELECT-then-INSERT implementation.
    # With the fixed atomic INSERT, no pre-read is needed and SQLite serializes writers.
    pre_read = threading.Barrier(4)
    original_connection = store_v5._c

    class Connection:
        def __init__(self, con):
            self.con = con
            self.first_read = True

        def execute(self, sql, params=()):
            result = self.con.execute(sql, params)
            if sql.startswith("SELECT 1 FROM news_raw") and self.first_read:
                self.first_read = False
                result.fetchall()
                pre_read.wait(timeout=10)
            return result

    @contextlib.contextmanager
    def connection():
        with original_connection() as con:
            yield Connection(con)

    def collect():
        ready.wait(timeout=10)
        return store_v5.news_upsert_raw_ids(items)

    with patch.object(store_v5, "_c", connection), ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(collect) for _ in range(4)]
        added = [nid for task in futures for nid in task.result(timeout=20)]
    assert sorted(added) == [item["id"] for item in items]
    assert store_v5.news_stats(1)["raw"] == 40
