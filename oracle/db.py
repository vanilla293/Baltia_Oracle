"""SQLite-хранилище (aiosqlite): схема, простые запросы, полнотекстовый поиск по-русски.

Одна база — `data/oracle.db`. Все моменты времени — UTC ISO (см. timeutil).
Поиск: таблица FTS5 `search_index(kind, ref_id, body)`; модули кладут туда текст своих
записей (`index_put`) и ищут (`search`). Русский без морфологии: запрос режется на основы
(грубый стеммер) и ищется префиксами — «кофейню» найдёт «кофейня», «кофейни». Слова с
диакритикой (латышские, украинские имена) не рвутся: «Jānis» ищется как «janis».

Схема растёт миграциями: версия — в `PRAGMA user_version`, шаги — `MIGRATIONS`. База от более
новой версии бота не открывается (иначе старый код тихо испортит её). Файлы базы — только для
владельца процесса (0600): там вся память, дневник и чужая переписка.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable

import aiosqlite

from .timeutil import iso, now_utc
from .usage import USAGE_SCHEMA

log = logging.getLogger("oracle.db")

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- диалог; role: user | assistant | event (сработало напоминание, проактивное сообщение, итог фоновой задачи)
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    via         TEXT NOT NULL DEFAULT 'text',     -- text | voice | system
    created_at  TEXT NOT NULL,
    summarized  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_messages_sum ON messages(summarized, id);

-- конспекты старого диалога
CREATE TABLE IF NOT EXISTS summaries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT NOT NULL,
    from_id     INTEGER NOT NULL,
    to_id       INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);

-- что бот знает о владельце
CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT 'general',  -- general | person | preference | plan | work | health | other
    source      TEXT NOT NULL DEFAULT 'chat',     -- chat | reflection | manual
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- собственные позиции бота
CREATE TABLE IF NOT EXISTS opinions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic       TEXT NOT NULL,
    stance      TEXT NOT NULL,
    reasons     TEXT NOT NULL DEFAULT '',
    confidence  INTEGER NOT NULL DEFAULT 60,      -- 0..100
    history     TEXT NOT NULL DEFAULT '[]',       -- JSON [{stance, reasons, confidence, why_changed, changed_at}]
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- дневник бота (ночная рефлексия): его заметки о владельце и планы, к чему вернуться
CREATE TABLE IF NOT EXISTS journal (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    text             TEXT NOT NULL,
    kind             TEXT NOT NULL DEFAULT 'reminder', -- reminder | wake | event | task | followup
    local_start      TEXT NOT NULL,                    -- первое срабатывание, локальное наивное
    tz               TEXT NOT NULL,
    rrule            TEXT,                             -- RFC 5545 без 'RRULE:'; NULL — однократно
    next_at          TEXT,                             -- UTC ISO; NULL — плановых срабатываний больше нет
    status           TEXT NOT NULL DEFAULT 'active',   -- active | done | cancelled
    nag              INTEGER NOT NULL DEFAULT 0,       -- 1 — долбить, пока не отметит
    nag_interval_min INTEGER NOT NULL DEFAULT 3,
    nag_max          INTEGER NOT NULL DEFAULT 20,
    nag_active       INTEGER NOT NULL DEFAULT 0,
    nag_next_at      TEXT,
    nag_count        INTEGER NOT NULL DEFAULT 0,
    challenge        INTEGER NOT NULL DEFAULT 0,       -- 1 — отметить можно, только решив задачку
    snooze_at        TEXT,                             -- UTC ISO повтор после «отложить»
    ref_type         TEXT,                             -- event | task | birthday | idea | project
    ref_id           INTEGER,
    fire_count       INTEGER NOT NULL DEFAULT 0,
    last_fired_at    TEXT,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rem_next ON reminders(status, next_at);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    local_start TEXT NOT NULL,                 -- локальное наивное начало
    tz          TEXT NOT NULL,
    starts_at   TEXT NOT NULL,                 -- UTC ISO (первое начало)
    ends_at     TEXT,                          -- UTC ISO
    all_day     INTEGER NOT NULL DEFAULT 0,
    location    TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    rrule       TEXT,
    status      TEXT NOT NULL DEFAULT 'active',  -- active | cancelled
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS birthdays (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    month               INTEGER NOT NULL,
    day                 INTEGER NOT NULL,
    year                INTEGER,
    relation            TEXT NOT NULL DEFAULT '',
    notes               TEXT NOT NULL DEFAULT '',
    tg_username         TEXT NOT NULL DEFAULT '',
    remind_days_before  INTEGER NOT NULL DEFAULT 1,
    last_greeted_year   INTEGER,
    last_prenotice_year INTEGER,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ideas (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    content         TEXT NOT NULL,
    evaluation      TEXT NOT NULL DEFAULT '',
    deep_evaluation TEXT NOT NULL DEFAULT '',
    score           INTEGER,                   -- 1..10, честная оценка бота
    tags            TEXT NOT NULL DEFAULT '',  -- через запятую
    status          TEXT NOT NULL DEFAULT 'new', -- new | thinking | in_work | parked | dropped | done
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    goal        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active',  -- active | paused | done | dropped
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    text        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'todo',    -- todo | doing | done | dropped
    priority    INTEGER NOT NULL DEFAULT 2,      -- 1 высокий, 2 обычный, 3 низкий
    due_at      TEXT,                            -- UTC ISO
    created_at  TEXT NOT NULL,
    done_at     TEXT
);

CREATE TABLE IF NOT EXISTS news_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    url          TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL,
    summary      TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    fetched_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_news_pub ON news_items(published_at);

-- черновики ответов в чужие чаты (userbot); отправка — только кнопкой владельца
CREATE TABLE IF NOT EXISTS drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    chat_title  TEXT NOT NULL DEFAULT '',
    incoming    TEXT NOT NULL DEFAULT '',
    draft       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | dropped
    created_at  TEXT NOT NULL
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    kind UNINDEXED, ref_id UNINDEXED, body, tokenize='unicode61'
);
"""

# ── русский «стеммер» для поиска ─────────────────────────────────────────────
_ENDINGS = sorted({
    "иями", "ями", "ами", "его", "ого", "ему", "ому", "ими", "ыми", "ешь", "ишь", "ете", "ите",
    "ает", "яет", "ует", "ют", "ут", "ать", "ять", "еть", "ить", "уть", "ться", "тся",
    "иях", "ях", "ах", "ией", "ей", "ой", "ий", "ый", "ая", "яя", "ое", "ее", "ые", "ие",
    "ую", "юю", "ом", "ем", "ам", "ям", "ов", "ев", "ия", "ье", "ья", "ы", "и", "а", "я",
    "о", "е", "у", "ю", "ь", "й",
}, key=len, reverse=True)

# буквы любых алфавитов и цифры: «Jānis», «Києва», «Bērziņš» — одно слово, а не обрывки
_WORD = re.compile(r"[^\W_]+")


def normalize_text(s: str) -> str:
    return (s or "").lower().replace("ё", "е")


def words(s: str) -> list[str]:
    """Слова текста в нижнем регистре (ё → е) — как их режет поиск."""
    return _WORD.findall(normalize_text(s))


def stem(word: str) -> str:
    """Грубая основа русского слова: срезать одно окончание, оставить ≥ 3 букв."""
    w = normalize_text(word)
    if len(w) <= 3 or not re.search(r"[а-я]", w):
        return w
    out = w
    for e in _ENDINGS:
        if w.endswith(e) and len(w) - len(e) >= 3:
            out = w[: -len(e)]
            break
    if len(w) >= 8:  # беглые гласные («тренировки» / «тренировок») — берём основу короче
        out = out[: max(5, len(w) - 3)]
    return out


def search_stems(word: str) -> list[str]:
    """Префиксы для поиска слова: его основа, а у длинного русского слова — ещё и основа покороче:
    «тренировками» → «тренировк», «трениро» — иначе не найдётся «тренировок» (беглая гласная)."""
    s = stem(word)
    out = [s]
    if len(word) >= 8 and re.search(r"[а-я]", s):
        short = s[: max(5, len(s) - 2)]
        if short != s:
            out.append(short)
    return out


def fts_query(text: str, max_terms: int = 12) -> str:
    """Текст запроса → FTS5-выражение: основы с префиксом через OR (search_stems). Пусто → ''."""
    terms: list[str] = []
    for w in words(text):
        if len(w) < 2:
            continue
        for t in search_stems(w):
            if t and t not in terms and len(terms) < max_terms:
                terms.append(t)
        if len(terms) >= max_terms:
            break
    return " OR ".join(f'"{t}"*' for t in terms)


# ── миграции: версия → шаг (выполняется в одной транзакции при open) ─────────
async def _m2_index_summaries(c: aiosqlite.Connection) -> None:
    """v2: recall ищет и в конспектах старых разговоров — проиндексировать уже сохранённые."""
    async with c.execute("SELECT id, content FROM summaries WHERE id NOT IN "
                         "(SELECT ref_id FROM search_index WHERE kind='summary')") as cur:
        rows = await cur.fetchall()
    for r in rows:
        await c.execute("INSERT INTO search_index(kind, ref_id, body) VALUES('summary',?,?)",
                        (int(r[0]), normalize_text(str(r[1] or ""))))


MIGRATIONS: dict[int, Callable[[aiosqlite.Connection], Awaitable[None]]] = {
    2: _m2_index_summaries,
}


def _private(path: str | Path) -> None:
    """Файл — только владельцу процесса (0600). Не вышло (Windows, чужой файл) — не страшно."""
    if os.name != "posix":
        return
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


class DB:
    """Тонкая обёртка над aiosqlite: одна связь, коммит после каждой записи."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn: aiosqlite.Connection | None = None
        self._wlock = asyncio.Lock()
        self.fts = True

    async def open(self) -> "DB":
        """Открыть (создать) базу, довести схему до SCHEMA_VERSION. Ошибка → связь закрыта, исключение
        наружу: иначе поток aiosqlite остаётся жить, процесс виснет и systemd/docker его не перезапустят."""
        if self.path != ":memory:":
            self._prepare_file()
            # непустой -wal до открытия = прошлый процесс не закрыл базу (упал, выключили питание)
            crashed = False
            with contextlib.suppress(OSError):
                crashed = os.path.getsize(self.path + "-wal") > 0
            if crashed:
                await self._check_integrity()
        self.conn = await aiosqlite.connect(self.path)
        try:
            self.conn.row_factory = aiosqlite.Row
            fresh = await self._scalar_raw(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='messages'") == 0
            await self.conn.execute("PRAGMA journal_mode=WAL")
            await self.conn.execute("PRAGMA foreign_keys=ON")
            await self.conn.executescript(SCHEMA)
            await self.conn.executescript(USAGE_SCHEMA)   # учёт расходов на модель (oracle.usage)
            try:
                await self.conn.executescript(FTS_SCHEMA)
            except Exception as e:  # sqlite без FTS5 — поиск деградирует до LIKE
                log.warning("FTS5 недоступен (%s) — поиск будет простым", e)
                self.fts = False
                await self.conn.execute(
                    "CREATE TABLE IF NOT EXISTS search_index (kind TEXT, ref_id INTEGER, body TEXT)")
            await self.conn.commit()
            await self._migrate(fresh)
            if self.path != ":memory:":        # WAL и shm SQLite создаёт с правами самой базы
                for suffix in ("", "-wal", "-shm"):
                    if os.path.exists(self.path + suffix):
                        _private(self.path + suffix)
        except BaseException:
            conn, self.conn = self.conn, None
            if conn is not None:
                with contextlib.suppress(Exception):
                    await conn.close()
            raise
        return self

    async def _check_integrity(self) -> None:
        """После аварийной остановки: база цела? Проверка — отдельной связью только для чтения: она
        не сливает -wal в файл базы и не удаляет его при закрытии (там могут быть последние записи).
        Повреждена — понятная ошибка; проверить не вышло (занята, нет прав) — открываем как обычно."""
        rows = await asyncio.to_thread(_quick_check_ro, self.path)
        if rows is None:
            log.warning("база: прошлый запуск завершился аварийно, а проверить целостность не вышло")
            return
        if rows != ["ok"]:
            name = Path(self.path).name
            raise RuntimeError(
                f"база {self.path} повреждена ({'; '.join(rows[:3])[:300]}). Останови бота, отложи {name}, "
                f"{name}-wal и {name}-shm в сторону и верни копию из /backup (см. README, «Восстановление "
                f"из копии»)")
        log.info("база: прошлый запуск завершился аварийно — проверил, база цела")

    def _prepare_file(self) -> None:
        """Папка (если её нет — только владельцу) и пустой файл базы с правами 0600 до первой записи."""
        path = Path(self.path)
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists():
            with contextlib.suppress(OSError):
                os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))

    async def _scalar_raw(self, sql: str) -> Any:
        assert self.conn is not None
        async with self.conn.execute(sql) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def _migrate(self, fresh: bool) -> None:
        """Довести схему до SCHEMA_VERSION шагами из MIGRATIONS. Новая база создаётся сразу в последней
        версии; база без user_version (первые выпуски) — версия из kv schema_version (1)."""
        ver = int(await self._scalar_raw("PRAGMA user_version") or 0)
        if fresh:
            ver = SCHEMA_VERSION
        elif ver == 0:
            try:
                ver = int(await self.kv_get("schema_version", 1) or 1)
            except (TypeError, ValueError):
                ver = 1
        if ver > SCHEMA_VERSION:
            raise RuntimeError(
                f"база {self.path} от более новой версии бота (схема v{ver}, этот код знает до "
                f"v{SCHEMA_VERSION}) — обнови код бота или верни базу из бэкапа")
        if ver < SCHEMA_VERSION:
            async with self.transaction() as c:
                for v in range(ver + 1, SCHEMA_VERSION + 1):
                    step = MIGRATIONS.get(v)
                    if step is not None:
                        await step(c)
                        log.info("база: миграция до v%s", v)
                await c.execute(f"PRAGMA user_version={int(SCHEMA_VERSION)}")
        elif int(await self._scalar_raw("PRAGMA user_version") or 0) != SCHEMA_VERSION:
            await self.conn.execute(f"PRAGMA user_version={int(SCHEMA_VERSION)}")
            await self.conn.commit()
        if str(await self.kv_get("schema_version")) != str(SCHEMA_VERSION):
            await self.kv_set("schema_version", str(SCHEMA_VERSION))

    async def backup(self, dest: str | Path) -> int:
        """Резервная копия (VACUUM INTO) в dest с правами 0600 → размер в байтах. dest не должен
        существовать (или должен быть пустым файлом)."""
        assert self.conn is not None, "DB не открыта"
        dest = Path(dest)
        if not dest.exists():               # сразу 0600: в копии вся память и переписка
            os.close(os.open(dest, os.O_CREAT | os.O_WRONLY, 0o600))
        async with self._wlock:
            if self.conn.in_transaction:    # хвост незакоммиченной записи — VACUUM внутри транзакции нельзя
                await self.conn.commit()
            await self.conn.execute("VACUUM INTO ?", (str(dest),))
        _private(dest)
        return dest.stat().st_size

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    # ── базовые запросы ──
    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """INSERT/UPDATE/DELETE → lastrowid (для INSERT) или число затронутых строк."""
        assert self.conn is not None, "DB не открыта"
        async with self._wlock:
            try:
                cur = await self.conn.execute(sql, tuple(params))
                await self.conn.commit()
            except Exception:
                # упавшая запись не должна оставить связь в открытой транзакции: иначе следующий
                # чужой commit зафиксирует мусор, а VACUUM INTO (/backup) откажет
                with contextlib.suppress(Exception):
                    await self.conn.rollback()
                raise
            if sql.lstrip().upper().startswith("INSERT"):
                return int(cur.lastrowid or 0)
            return int(cur.rowcount or 0)

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        assert self.conn is not None, "DB не открыта"
        async with self.conn.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        assert self.conn is not None, "DB не открыта"
        async with self.conn.execute(sql, tuple(params)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = await self.fetchone(sql, params)
        return next(iter(row.values())) if row else None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Несколько записей атомарно: `async with db.transaction() as c: await c.execute(...)`."""
        assert self.conn is not None, "DB не открыта"
        async with self._wlock:
            try:
                yield self.conn
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise

    # ── kv ──
    async def kv_get(self, key: str, default: Any = None) -> Any:
        row = await self.fetchone("SELECT value FROM kv WHERE key=?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return row["value"]

    async def kv_set(self, key: str, value: Any) -> None:
        await self.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)))

    async def kv_delete(self, key: str) -> bool:
        """Удалить ключ. True — был."""
        return await self.execute("DELETE FROM kv WHERE key=?", (key,)) > 0

    # ── поиск ──
    async def index_put(self, kind: str, ref_id: int, body: str) -> None:
        """Положить/заменить текст записи в поисковый индекс."""
        async with self.transaction() as c:
            await c.execute("DELETE FROM search_index WHERE kind=? AND ref_id=?", (kind, ref_id))
            await c.execute("INSERT INTO search_index(kind, ref_id, body) VALUES(?,?,?)",
                            (kind, ref_id, normalize_text(body)))

    async def index_delete(self, kind: str, ref_id: int) -> None:
        await self.execute("DELETE FROM search_index WHERE kind=? AND ref_id=?", (kind, ref_id))

    async def search(self, kind: str, query: str, limit: int = 10) -> list[int]:
        """id записей вида `kind`, лучшие совпадения первыми. Пустой запрос → []."""
        q = fts_query(query)
        if not q:
            return []
        if self.fts:
            try:
                rows = await self.fetchall(
                    "SELECT ref_id FROM search_index WHERE search_index MATCH ? AND kind=? "
                    "ORDER BY bm25(search_index) LIMIT ?", (q, kind, int(limit)))
                return [int(r["ref_id"]) for r in rows]
            except Exception as e:  # кривой запрос — не роняем, падаем на LIKE
                log.debug("fts match failed: %s", e)
        terms = [t.strip('"*') for t in q.split(" OR ")]
        where = " OR ".join("body LIKE ?" for _ in terms)
        rows = await self.fetchall(
            f"SELECT ref_id FROM search_index WHERE kind=? AND ({where}) LIMIT ?",
            (kind, *[f"%{t}%" for t in terms], int(limit)))
        return [int(r["ref_id"]) for r in rows]

    # ── диалог ──
    async def add_message(self, role: str, content: str, via: str = "text") -> int:
        return await self.execute(
            "INSERT INTO messages(role, content, via, created_at) VALUES(?,?,?,?)",
            (role, content, via, iso(now_utc())))

    async def recent_messages(self, limit: int) -> list[dict]:
        rows = await self.fetchall(
            "SELECT id, role, content, via, created_at FROM messages WHERE summarized=0 "
            "ORDER BY id DESC LIMIT ?", (int(limit),))
        return list(reversed(rows))


def _quick_check_ro(path: str) -> list[str] | None:
    """PRAGMA quick_check связью только для чтения → строки результата (["ok"] — цела);
    None — проверить не удалось (база занята, нет прав и т.п.)."""
    try:
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError as e:
        log.debug("quick_check: не открыл %s: %s", path, e)
        return None
    except sqlite3.DatabaseError as e:
        return [str(e)]
    try:
        return [str(r[0]) for r in conn.execute("PRAGMA quick_check").fetchall()]
    except sqlite3.OperationalError as e:
        log.debug("quick_check %s: %s", path, e)
        return None
    except sqlite3.DatabaseError as e:          # «database disk image is malformed», «file is not a database»
        return [str(e)]
    finally:
        conn.close()


async def open_db(path: str | Path) -> DB:
    return await DB(path).open()
