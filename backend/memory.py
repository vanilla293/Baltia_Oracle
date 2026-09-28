"""ОРАКУЛ // ПИФИЯ — хранилище (SQLite).

Держит последний полный анализ по каждому инструменту (досье, привязанные
новости, фон, разбор аналитика, разнос критика, итоговый вердикт, прогноз-JSON)
и историю чата с памятью по каждому инструменту.

sqlite3 синхронный — оборачиваем в asyncio.to_thread, чтобы не блокировать loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path

from . import config

logger = logging.getLogger("pythia.memory")

DB_PATH = config.DATA_DIR / "pythia.db"


import contextlib


@contextlib.contextmanager
def _conn():
    """Соединение на операцию: раньше `with sqlite3.connect(...)` только
    коммитил, но НЕ закрывал сокет к файлу — за долгую сессию копились
    открытые дескрипторы. Теперь соединение гарантированно закрывается."""
    c = sqlite3.connect(DB_PATH, timeout=15)
    try:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=15000")
        with c:          # транзакция: commit/rollback
            yield c
    finally:
        c.close()


def _create_schema() -> None:
    with _conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS analyses (
            ticker TEXT PRIMARY KEY,
            asset_class TEXT,
            name TEXT,
            created_at REAL,
            dossier_json TEXT,
            news_json TEXT,
            background TEXT,
            analyst TEXT,
            critic TEXT,
            verdict TEXT,
            forecast_json TEXT,
            tags_json TEXT,
            thinking_json TEXT
        )""")
        columns = {row[1] for row in c.execute("PRAGMA table_info(analyses)")}
        if "thinking_json" not in columns:
            c.execute("ALTER TABLE analyses ADD COLUMN thinking_json TEXT")
        c.execute("""
        CREATE TABLE IF NOT EXISTS chat (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            role TEXT,
            content TEXT,
            created_at REAL
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_chat_ticker ON chat(ticker, id)")


def _delete_db_files() -> int:
    """Физически удалить файл базы и все хвосты WAL/journal. Работает даже
    если база залочена/повреждена после аварийного выключения."""
    n = 0
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = Path(str(DB_PATH) + suffix)
        try:
            if p.exists():
                p.unlink()
                n += 1
        except Exception as e:
            logger.warning("не удалось удалить %s: %s", p.name, str(e)[:80])
    return n


# Настоящее повреждение файла базы (не блокировка, не диск, не права): такую базу откладываем рядом
# под именем .broken-<время> и стартуем с чистой — приложение поднимается, история не удалена.
# Всё остальное («database is locked» от второго экземпляра, полный диск, права, сбой миграции) —
# стоп с понятным сообщением, файлы на месте: их нельзя ни стирать, ни трогать.
_CORRUPT_MARKS = ("not a database", "malformed", "file is encrypted", "unsupported file format")
_OPEN_FAIL = ("Не удалось открыть базу {db}. Файлы базы не удалены. "
              "Закройте другой экземпляр ПИФИИ, проверьте место и права доступа; "
              "перед восстановлением сохраните копию базы и файлов -wal/-shm/-journal.")


def _is_corrupt(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.DatabaseError) and any(m in str(exc).lower() for m in _CORRUPT_MARKS)


def _set_aside(db: Path) -> str:
    """Переименовать повреждённую базу с хвостами в .broken-<время>; занятый файл (Windows) → PermissionError наверх."""
    tag = ".broken-" + time.strftime("%Y%m%d-%H%M%S")
    for suf in ("-journal", "-shm", "-wal", ""):      # хвосты первыми: занятый хвост → основной файл не сдвинут
        p = Path(str(db) + suf)
        if p.exists():
            p.rename(Path(str(db) + suf + tag))
    return str(db) + tag


def _init() -> None:
    """Создать схему, сохранив существующую историю при любой ошибке: повреждённый файл откладывается
    рядом (.broken-<время>) и база начинается заново; блокировка/диск/права — стоп с сообщением."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _create_schema()
        return
    except (sqlite3.Error, OSError) as exc:
        if not _is_corrupt(exc):
            raise RuntimeError(_OPEN_FAIL.format(db=DB_PATH)) from exc
        first = exc
    try:
        aside = _set_aside(Path(str(DB_PATH)))
        _create_schema()
    except (sqlite3.Error, OSError) as exc:
        raise RuntimeError(_OPEN_FAIL.format(db=DB_PATH)) from exc
    logger.warning("база %s повреждена (%s) — отложена как %s, начата новая", DB_PATH, str(first)[:80], aside)


_init()


def _hard_reset_sync() -> int:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    n = _delete_db_files()
    _create_schema()  # пересоздать пустую схему
    return n


async def hard_reset() -> int:
    """Гарантированно чистый старт: стираем базу с диска и создаём заново.
    Не зависит от состояния БД (лок/хвосты/повреждение после краша).
    Ключи API не трогаются — они в config_user.json, не в базе."""
    return await asyncio.to_thread(_hard_reset_sync)


def _save_analysis_sync(rec: dict) -> None:
    with _conn() as c:
        c.execute("""
        INSERT INTO analyses
          (ticker, asset_class, name, created_at, dossier_json, news_json,
           background, analyst, critic, verdict, forecast_json, tags_json,
           thinking_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(ticker) DO UPDATE SET
          asset_class=excluded.asset_class, name=excluded.name,
          created_at=excluded.created_at, dossier_json=excluded.dossier_json,
          news_json=excluded.news_json, background=excluded.background,
          analyst=excluded.analyst, critic=excluded.critic,
          verdict=excluded.verdict, forecast_json=excluded.forecast_json,
          tags_json=excluded.tags_json, thinking_json=excluded.thinking_json
        """, (
            rec["ticker"], rec.get("asset_class"), rec.get("name"),
            rec.get("created_at", time.time()),
            json.dumps(rec.get("dossier"), ensure_ascii=False),
            json.dumps(rec.get("news"), ensure_ascii=False),
            rec.get("background", ""), rec.get("analyst", ""),
            rec.get("critic", ""), rec.get("verdict", ""),
            json.dumps(rec.get("forecast"), ensure_ascii=False),
            json.dumps(rec.get("tags") or [], ensure_ascii=False),
            json.dumps(rec.get("thinking"), ensure_ascii=False),
        ))


async def save_analysis(rec: dict) -> None:
    await asyncio.to_thread(_save_analysis_sync, rec)


def _get_analysis_sync(ticker: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM analyses WHERE ticker=?",
                        (ticker.upper(),)).fetchone()
    if not row:
        return None
    d = dict(row)
    for k_json, k_out in (("dossier_json", "dossier"), ("news_json", "news"),
                          ("forecast_json", "forecast"), ("tags_json", "tags"),
                          ("thinking_json", "thinking")):
        try:
            d[k_out] = json.loads(d.pop(k_json) or "null")
        except Exception:
            d[k_out] = None
    return d


async def get_analysis(ticker: str) -> dict | None:
    return await asyncio.to_thread(_get_analysis_sync, ticker)


def _list_analyses_sync() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT ticker, name, asset_class, created_at, forecast_json "
            "FROM analyses ORDER BY created_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["forecast"] = json.loads(d.pop("forecast_json") or "null")
        except Exception:
            d["forecast"] = None
        out.append(d)
    return out


async def list_analyses() -> list[dict]:
    return await asyncio.to_thread(_list_analyses_sync)


def _add_chat_sync(ticker: str, role: str, content: str) -> None:
    with _conn() as c:
        c.execute("INSERT INTO chat (ticker, role, content, created_at) VALUES (?,?,?,?)",
                  (ticker.upper(), role, content, time.time()))


async def add_chat(ticker: str, role: str, content: str) -> None:
    await asyncio.to_thread(_add_chat_sync, ticker, role, content)


def _get_chat_sync(ticker: str, limit: int) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT role, content, created_at FROM chat WHERE ticker=? "
            "ORDER BY id DESC LIMIT ?", (ticker.upper(), limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


async def get_chat(ticker: str, limit: int = 40) -> list[dict]:
    return await asyncio.to_thread(_get_chat_sync, ticker, limit)


def _clear_chat_sync(ticker: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM chat WHERE ticker=?", (ticker.upper(),))


async def clear_chat(ticker: str) -> None:
    await asyncio.to_thread(_clear_chat_sync, ticker)


def _reset_all_sync() -> None:
    with _conn() as c:
        c.execute("DELETE FROM analyses")
        c.execute("DELETE FROM chat")


async def reset_all() -> None:
    """Полностью обнулить разборы и историю чата (ключи API не трогаются —
    они в config_user.json). Используется для чистого старта."""
    await asyncio.to_thread(_reset_all_sync)
