# -*- coding: utf-8 -*-
"""ПИФИЯ v5 — хранилище (SQLite data/pythia_v5.db).

Отдельный файл от старой базы: старт/выход её не стирают — новости копятся
3 дня, советы, заметки дозора, миссии и журнал сделок переживают перезапуск.
Функции синхронные и короткие (локальный SQLite, миллисекунды).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path

from . import config

DB_PATH: Path = config.DATA_DIR / "pythia_v5.db"
log = logging.getLogger("pythia.store_v5")


@contextlib.contextmanager
def _c():
    con = sqlite3.connect(DB_PATH, timeout=15)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=15000")
        with con:
            yield con
    finally:
        con.close()


def _schema() -> None:
    with _c() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS news_raw (
            id TEXT PRIMARY KEY, ts REAL, source TEXT, title TEXT, summary TEXT,
            link TEXT, published TEXT, relevant INTEGER, added_ts REAL);
        CREATE INDEX IF NOT EXISTS idx_news_ts ON news_raw(ts);
        CREATE TABLE IF NOT EXISTS news_ai (id TEXT PRIMARY KEY, ts REAL, json TEXT);
        CREATE TABLE IF NOT EXISTS council_runs (
            run_id TEXT PRIMARY KEY, kind TEXT, ts REAL, json TEXT, status TEXT);
        CREATE TABLE IF NOT EXISTS watch_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, json TEXT, seen INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS missions (ticker TEXT PRIMARY KEY, ts REAL, json TEXT);
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, side TEXT, lots INTEGER,
            entry REAL, exit_px REAL, pnl REAL, opened_ts REAL, closed_ts REAL,
            why TEXT, mode TEXT, json TEXT);
        CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS ops (
            id TEXT PRIMARY KEY, ts REAL, kind TEXT, figi TEXT, uid TEXT, qty INTEGER,
            price REAL, payment REAL, fee REAL, raw TEXT);
        CREATE INDEX IF NOT EXISTS idx_ops_ts ON ops(ts);
        """)
        _migrate(c)


# v5.3 фаза 4 (W1): журнал по операциям брокера — новые колонки trades. ALTER TABLE только при отсутствии
# колонки: _schema идемпотентна, старая база владельца не пересоздаётся (сделки остаются).
TRADE_EXTRA = (("figi", "TEXT"), ("entry_real", "REAL"), ("exit_real", "REAL"), ("fee", "REAL"),
               ("fee_est", "REAL"), ("pnl_gross", "REAL"), ("pnl_net", "REAL"), ("source", "TEXT"),
               ("synced_ts", "REAL"), ("ops", "TEXT"), ("ops_alloc", "TEXT"))


def _migrate(c) -> None:
    have = {r[1] for r in c.execute("PRAGMA table_info(trades)").fetchall()}
    for col, typ in TRADE_EXTRA:
        if col not in have:
            c.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")


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
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _schema()
        return
    except (sqlite3.Error, OSError) as exc:
        if not _is_corrupt(exc):
            raise RuntimeError(_OPEN_FAIL.format(db=DB_PATH)) from exc
        first = exc
    try:
        aside = _set_aside(Path(str(DB_PATH)))
        _schema()
    except (sqlite3.Error, OSError) as exc:
        raise RuntimeError(_OPEN_FAIL.format(db=DB_PATH)) from exc
    log.warning("база %s повреждена (%s) — отложена как %s, начата новая", DB_PATH, str(first)[:80], aside)


_init()


def _j(s: str | None):
    try:
        return json.loads(s) if s else None
    except Exception:            # noqa: BLE001
        return None


def _d(o) -> str:
    return json.dumps(o, ensure_ascii=False)


# ── новости ──────────────────────────────────────────────────────────────
def news_id(link: str | None, title: str | None) -> str:
    base = (link or "").strip() or (title or "").strip().lower()
    return hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()


def news_upsert_raw_ids(items: list[dict]) -> list[str]:
    """Сырые новости → база. id = sha1(link или title); уже лежащие (в базе или
    раньше в этой же пачке) пропускаются. Возврат: id ДОБАВЛЕННЫХ строк в порядке
    items — по ним целевой сбор (Google News по инструменту) отбирает и размечает
    только новое, не трогая всё окно."""
    added: list[str] = []
    now = time.time()
    with _c() as c:
        for it in items or []:
            nid = it.get("id") or news_id(it.get("link"), it.get("title"))
            ts = float(it.get("ts") or 0) or now
            # Одна атомарная запись: два сборщика могут одновременно получить
            # одну новость. Проверка SELECT перед INSERT оставляла окно гонки.
            cur = c.execute("INSERT INTO news_raw(id,ts,source,title,summary,link,published,relevant,added_ts)"
                            " VALUES(?,?,?,?,?,?,?,NULL,?) ON CONFLICT(id) DO NOTHING",
                            (nid, ts, it.get("source", ""), it.get("title", ""),
                             (it.get("summary") or "")[:4000], it.get("link", ""),
                             it.get("published", ""), now))
            if cur.rowcount:
                added.append(nid)
    return added


def news_upsert_raw(items: list[dict]) -> int:
    """Обёртка над news_upsert_raw_ids: число добавленных (контракт §1)."""
    return len(news_upsert_raw_ids(items))


def _row(r) -> dict:
    return {"id": r["id"], "ts": r["ts"], "source": r["source"], "title": r["title"],
            "summary": r["summary"], "link": r["link"], "published": r["published"],
            "relevant": r["relevant"], "added_ts": r["added_ts"]}


def news_untriaged(days: float) -> list[dict]:
    cut = time.time() - days * 86400
    with _c() as c:
        rows = c.execute("SELECT * FROM news_raw WHERE relevant IS NULL AND ts>=? ORDER BY ts DESC",
                         (cut,)).fetchall()
    return [_row(r) for r in rows]


def news_set_relevant(ids: list[str], flag: int) -> None:
    if not ids:
        return
    with _c() as c:
        c.executemany("UPDATE news_raw SET relevant=? WHERE id=?", [(int(flag), i) for i in ids])


def news_relevant_uncharacterized(days: float) -> list[dict]:
    cut = time.time() - days * 86400
    with _c() as c:
        rows = c.execute("SELECT r.* FROM news_raw r LEFT JOIN news_ai a ON a.id=r.id "
                         "WHERE r.relevant=1 AND a.id IS NULL AND r.ts>=? ORDER BY r.ts DESC",
                         (cut,)).fetchall()
    return [_row(r) for r in rows]


def news_ai_put(nid: str, data: dict) -> None:
    with _c() as c:
        c.execute("INSERT OR REPLACE INTO news_ai(id,ts,json) VALUES(?,?,?)",
                  (nid, time.time(), _d(data)))


def news_ai_get(ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    out = {}
    with _c() as c:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            for r in c.execute(f"SELECT id,json FROM news_ai WHERE id IN ({q})", chunk):
                out[r["id"]] = _j(r["json"]) or {}
    return out


def _merge(rows, with_ai: bool) -> list[dict]:
    items = [_row(r) for r in rows]
    if with_ai and items:
        ai = news_ai_get([x["id"] for x in items])
        for x in items:
            x["ai"] = ai.get(x["id"])
    return items


def news_window(days: float, relevant_only: bool = True, with_ai: bool = True) -> list[dict]:
    cut = time.time() - days * 86400
    with _c() as c:
        if relevant_only:
            rows = c.execute("SELECT * FROM news_raw WHERE relevant=1 AND ts>=? ORDER BY ts DESC",
                             (cut,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM news_raw WHERE ts>=? ORDER BY ts DESC", (cut,)).fetchall()
    return _merge(rows, with_ai)


def news_by_ids(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    full = [i for i in ids if len(i) > 8]
    short = [i for i in ids if len(i) <= 8]
    with _c() as c:
        rows = []
        if full:
            q = ",".join("?" * len(full))
            rows += c.execute(f"SELECT * FROM news_raw WHERE id IN ({q})", full).fetchall()
        for s in short:                      # короткие id из ответов ИИ (префикс)
            rows += c.execute("SELECT * FROM news_raw WHERE id LIKE ?", (s + "%",)).fetchall()
    seen, uniq = set(), []
    for r in rows:
        if r["id"] not in seen:
            seen.add(r["id"])
            uniq.append(r)
    return _merge(uniq, True)


def news_since(ts: float, relevant_only: bool = True) -> list[dict]:
    with _c() as c:
        if relevant_only:
            rows = c.execute("SELECT * FROM news_raw WHERE relevant=1 AND added_ts>? ORDER BY ts DESC",
                             (ts,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM news_raw WHERE added_ts>? ORDER BY ts DESC", (ts,)).fetchall()
    return _merge(rows, True)


def news_stats(days: float) -> dict:
    cut = time.time() - days * 86400
    with _c() as c:
        raw = c.execute("SELECT COUNT(*) FROM news_raw WHERE ts>=?", (cut,)).fetchone()[0]
        rel = c.execute("SELECT COUNT(*) FROM news_raw WHERE ts>=? AND relevant=1", (cut,)).fetchone()[0]
        unt = c.execute("SELECT COUNT(*) FROM news_raw WHERE ts>=? AND relevant IS NULL", (cut,)).fetchone()[0]
        ch = c.execute("SELECT COUNT(*) FROM news_raw r JOIN news_ai a ON a.id=r.id WHERE r.ts>=?",
                       (cut,)).fetchone()[0]
    return {"raw": raw, "relevant": rel, "characterized": ch, "untriaged": unt, "days": days}


def news_purge(older_days: float = 14) -> int:
    cut = time.time() - older_days * 86400
    with _c() as c:
        n = c.execute("DELETE FROM news_raw WHERE ts<?", (cut,)).rowcount
        c.execute("DELETE FROM news_ai WHERE id NOT IN (SELECT id FROM news_raw)")
    return n


# ── советы ───────────────────────────────────────────────────────────────
def council_put(run_id: str, kind: str, data: dict, status: str = "done") -> None:
    with _c() as c:
        c.execute("INSERT OR REPLACE INTO council_runs(run_id,kind,ts,json,status) VALUES(?,?,?,?,?)",
                  (run_id, kind, time.time(), _d(data), status))


def _crow(r, with_data: bool) -> dict:
    d = {"run_id": r["run_id"], "kind": r["kind"], "ts": r["ts"], "status": r["status"]}
    if with_data:
        d["data"] = _j(r["json"]) or {}
    return d


def council_latest(kind: str | None = None) -> dict | None:
    with _c() as c:
        if kind:
            r = c.execute("SELECT * FROM council_runs WHERE kind=? AND status='done' ORDER BY ts DESC LIMIT 1",
                          (kind,)).fetchone()
        else:
            r = c.execute("SELECT * FROM council_runs WHERE status='done' ORDER BY ts DESC LIMIT 1").fetchone()
    return _crow(r, True) if r else None


def council_get(run_id: str) -> dict | None:
    with _c() as c:
        r = c.execute("SELECT * FROM council_runs WHERE run_id=?", (run_id,)).fetchone()
    return _crow(r, True) if r else None


def council_list(limit: int = 20) -> list[dict]:
    with _c() as c:
        rows = c.execute("SELECT * FROM council_runs ORDER BY ts DESC LIMIT ?", (int(limit),)).fetchall()
    out = []
    for r in rows:
        d = _crow(r, True)
        data = d.pop("data") or {}
        d["reason"] = data.get("reason")
        d["regime"] = (data.get("summary") or {}).get("regime")
        d["picks"] = [p.get("ticker") for p in (data.get("summary") or {}).get("picks") or []]
        out.append(d)
    return out


# ── дозор ────────────────────────────────────────────────────────────────
def watch_add(data: dict) -> int:
    with _c() as c:
        cur = c.execute("INSERT INTO watch_notes(ts,json,seen) VALUES(?,?,0)",
                        (float(data.get("ts") or time.time()), _d(data)))
        return int(cur.lastrowid)


def watch_list(limit: int = 30, unseen_only: bool = False) -> list[dict]:
    with _c() as c:
        q = "SELECT * FROM watch_notes" + (" WHERE seen=0" if unseen_only else "") + \
            " ORDER BY ts DESC LIMIT ?"
        rows = c.execute(q, (int(limit),)).fetchall()
    return [{"id": r["id"], "ts": r["ts"], "seen": r["seen"], "data": _j(r["json"]) or {}} for r in rows]


def watch_mark_seen(ids: list[int]) -> None:
    if not ids:
        return
    with _c() as c:
        c.executemany("UPDATE watch_notes SET seen=1 WHERE id=?", [(int(i),) for i in ids])


def watch_unseen_count() -> int:
    with _c() as c:
        return int(c.execute("SELECT COUNT(*) FROM watch_notes WHERE seen=0").fetchone()[0])


# ── миссии и сделки ──────────────────────────────────────────────────────
def mission_put(ticker: str, data: dict) -> None:
    with _c() as c:
        c.execute("INSERT OR REPLACE INTO missions(ticker,ts,json) VALUES(?,?,?)",
                  ((ticker or "").upper(), time.time(), _d(data)))


def mission_get(ticker: str) -> dict | None:
    with _c() as c:
        r = c.execute("SELECT json FROM missions WHERE ticker=?", ((ticker or "").upper(),)).fetchone()
    return _j(r["json"]) if r else None


def mission_all() -> dict:
    with _c() as c:
        rows = c.execute("SELECT ticker,json FROM missions").fetchall()
    return {r["ticker"]: _j(r["json"]) or {} for r in rows}


def mission_del(ticker: str) -> None:
    with _c() as c:
        c.execute("DELETE FROM missions WHERE ticker=?", ((ticker or "").upper(),))


TRADE_COLS = ("ticker", "side", "lots", "entry", "exit_px", "pnl", "opened_ts", "closed_ts", "why", "mode") + \
             tuple(col for col, _ in TRADE_EXTRA)


def trade_add(rec: dict) -> int:
    """Сделка пилота: pnl — по цене тика (оценка), fee_est/source/figi — если пилот их дал (ledger дополнит
    настоящими entry_real/exit_real/fee/pnl_net по операциям брокера)."""
    extra = {col: rec.get(col) for col, _ in TRADE_EXTRA}
    extra["source"] = rec.get("source") or "est"
    extra["ops"] = _d(rec["ops"]) if rec.get("ops") else None
    extra["ops_alloc"] = _d(rec["ops_alloc"]) if rec.get("ops_alloc") else None
    cols = ",".join(extra)
    with _c() as c:
        cur = c.execute(
            f"INSERT INTO trades(ticker,side,lots,entry,exit_px,pnl,opened_ts,closed_ts,why,mode,json,{cols})"
            f" VALUES(?,?,?,?,?,?,?,?,?,?,?,{','.join('?' * len(extra))})",
            ((rec.get("ticker") or "").upper(), rec.get("side"), int(rec.get("lots") or 0),
             rec.get("entry"), rec.get("exit_px"), float(rec.get("pnl") or 0.0),
             rec.get("opened_ts"), rec.get("closed_ts") or time.time(),
             (rec.get("why") or "")[:300], rec.get("mode"), _d(rec), *extra.values()))
        return int(cur.lastrowid)


def trade_update(tid: int, fields: dict) -> bool:
    """Дополнить сделку (сверка ledger): только известные колонки; ops — список id операций (json)."""
    upd = {k: v for k, v in (fields or {}).items() if k in TRADE_COLS and k != "ticker"}
    if "ops" in upd and not isinstance(upd["ops"], str):
        upd["ops"] = _d(upd["ops"]) if upd["ops"] else None
    if "ops_alloc" in upd and not isinstance(upd["ops_alloc"], str):
        upd["ops_alloc"] = _d(upd["ops_alloc"]) if upd["ops_alloc"] else None
    if not upd:
        return False
    with _c() as c:
        cur = c.execute("UPDATE trades SET " + ",".join(f"{k}=?" for k in upd) + " WHERE id=?",
                        (*upd.values(), int(tid)))
        return cur.rowcount > 0


def trades(ticker: str | None = None, limit: int = 200) -> list[dict]:
    with _c() as c:
        if ticker:
            rows = c.execute("SELECT * FROM trades WHERE ticker=? ORDER BY id DESC LIMIT ?",
                             (ticker.upper(), int(limit))).fetchall()
        else:
            rows = c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [_trow(r) for r in rows]


def _trow(r, with_data: bool = False) -> dict:
    """Строка сделки → dict. Нетто: pnl_net от брокера, иначе pnl − fee_est (оценка); est — брокер не подтвердил."""
    src = r["source"] or "est"
    est = src not in ("broker", "mock")
    fee = r["fee"] if r["fee"] is not None else (r["fee_est"] or 0.0)
    net = r["pnl_net"] if r["pnl_net"] is not None else round(float(r["pnl"] or 0.0) - float(fee or 0.0), 2)
    gross = r["pnl_gross"] if r["pnl_gross"] is not None else r["pnl"]
    d = {"id": r["id"], "ticker": r["ticker"], "side": r["side"], "lots": r["lots"],
         "entry": r["entry"], "exit_px": r["exit_px"], "pnl": r["pnl"],
         "opened_ts": r["opened_ts"], "closed_ts": r["closed_ts"], "why": r["why"], "mode": r["mode"],
         "figi": r["figi"], "entry_real": r["entry_real"], "exit_real": r["exit_real"],
         "fee": round(float(fee or 0.0), 2), "fee_est": r["fee_est"], "pnl_gross": gross, "pnl_net": r["pnl_net"],
         "net": round(float(net), 2), "source": src, "est": est, "synced_ts": r["synced_ts"]}
    if with_data:
        d["data"] = _j(r["json"]) or {}
        d["ops"] = _j(r["ops"]) or []
        d["ops_alloc"] = _j(r["ops_alloc"]) or {}
    return d


def trades_full(ticker: str | None = None, since_ts: float | None = None, limit: int = 1000) -> list[dict]:
    """Сделки с json и списком операций (для сверки ledger), по возрастанию времени закрытия."""
    q, args = "SELECT * FROM trades", []
    cond = []
    if ticker:
        cond.append("ticker=?"); args.append(ticker.upper())
    if since_ts:
        cond.append("closed_ts>=?"); args.append(float(since_ts))
    if cond:
        q += " WHERE " + " AND ".join(cond)
    q += " ORDER BY closed_ts ASC, id ASC LIMIT ?"
    args.append(int(limit))
    with _c() as c:
        rows = c.execute(q, args).fetchall()
    return [_trow(r, True) for r in rows]


def trades_between(from_ts: float, to_ts: float, ticker: str | None = None) -> list[dict]:
    """Сделки, закрытые в окне [from_ts, to_ts) — для итогов дня."""
    with _c() as c:
        if ticker:
            rows = c.execute("SELECT * FROM trades WHERE closed_ts>=? AND closed_ts<? AND ticker=? ORDER BY closed_ts",
                             (float(from_ts), float(to_ts), ticker.upper())).fetchall()
        else:
            rows = c.execute("SELECT * FROM trades WHERE closed_ts>=? AND closed_ts<? ORDER BY closed_ts",
                             (float(from_ts), float(to_ts))).fetchall()
    return [_trow(r) for r in rows]


MSK_OFFSET = 3 * 3600      # день журнала — по МСК (без перевода часов)


def _day_of(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(float(ts or 0) + MSK_OFFSET))


def trades_summary(ticker: str | None = None) -> dict:
    """{count, pnl (нетто), gross, fee, net, wins, losses, est: bool (есть сделки без подтверждения брокера),
    confirmed: сколько по операциям, by_day:[{day, count, net, gross, fee}] (по МСК, новые дни первыми)}.
    Нетто = сумма pnl_net по сделкам брокера и (pnl − fee_est) по оценочным — честно смешанная, флаг est."""
    with _c() as c:
        if ticker:
            rows = c.execute("SELECT * FROM trades WHERE ticker=?", (ticker.upper(),)).fetchall()
        else:
            rows = c.execute("SELECT * FROM trades").fetchall()
    items = [_trow(r) for r in rows]
    gross = sum(float(t["pnl_gross"] or 0.0) for t in items)
    fee = sum(float(t["fee"] or 0.0) for t in items)
    net = sum(float(t["net"] or 0.0) for t in items)
    by: dict[str, dict] = {}
    for t in items:
        d = by.setdefault(_day_of(t["closed_ts"]), {"day": _day_of(t["closed_ts"]), "count": 0, "net": 0.0,
                                                    "gross": 0.0, "fee": 0.0})
        d["count"] += 1
        d["net"] += float(t["net"] or 0.0)
        d["gross"] += float(t["pnl_gross"] or 0.0)
        d["fee"] += float(t["fee"] or 0.0)
    for d in by.values():
        for k in ("net", "gross", "fee"):
            d[k] = round(d[k], 2)
    return {"count": len(items), "pnl": round(net, 2), "gross": round(gross, 2), "fee": round(fee, 2),
            "net": round(net, 2),
            "wins": sum(1 for t in items if float(t["net"] or 0) > 0),
            "losses": sum(1 for t in items if float(t["net"] or 0) < 0),
            "est": any(t["est"] for t in items), "confirmed": sum(1 for t in items if not t["est"]),
            "by_day": sorted(by.values(), key=lambda d: d["day"], reverse=True)[:60]}


# ── операции брокера (v5.3 фаза 4 · W1) ──────────────────────────────────
def ops_upsert(items: list[dict]) -> int:
    """Нормализованные операции (tinkoff.operations) → таблица ops; id PK — повтор не плодит. Возврат: добавлено."""
    n = 0
    with _c() as c:
        for op in items or []:
            oid = str((op or {}).get("id") or "")
            if not oid:
                continue
            kind = op.get("kind") or "other"
            cur = c.execute("INSERT OR IGNORE INTO ops(id,ts,kind,figi,uid,qty,price,payment,fee,raw)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (oid, float(op.get("ts") or 0), kind, op.get("figi"),
                             op.get("uid"), int(op.get("qty") or 0), op.get("price"), op.get("payment"),
                             op.get("fee"), _d(op)))
            if cur.rowcount > 0:
                n += 1
            else:                                    # уже лежит: вид мог быть переклассифицирован (fee → margin)
                c.execute("UPDATE ops SET kind=?, raw=? WHERE id=? AND kind<>?", (kind, _d(op), oid, kind))
    return n


def ops_list(keys: list[str] | None = None, from_ts: float | None = None, to_ts: float | None = None,
             kinds: tuple[str, ...] | None = None) -> list[dict]:
    """Операции по инструменту (figi ИЛИ uid из keys), окну и видам — по возрастанию времени; полный dict из raw."""
    q, args, cond = "SELECT raw FROM ops", [], []
    keys = [k for k in (keys or []) if k]
    if keys:
        ph = ",".join("?" * len(keys))
        cond.append(f"(figi IN ({ph}) OR uid IN ({ph}))"); args += keys + keys
    if from_ts is not None:
        cond.append("ts>=?"); args.append(float(from_ts))
    if to_ts is not None:
        cond.append("ts<=?"); args.append(float(to_ts))
    if kinds:
        cond.append("kind IN (" + ",".join("?" * len(kinds)) + ")"); args += list(kinds)
    if cond:
        q += " WHERE " + " AND ".join(cond)
    q += " ORDER BY ts ASC, id ASC"
    with _c() as c:
        rows = c.execute(q, args).fetchall()
    return [x for x in (_j(r["raw"]) for r in rows) if x]


def ops_stats(from_ts: float | None = None, to_ts: float | None = None) -> dict:
    """{count, by_kind:{kind: n}, fee: комиссии сделок (поле commission у buy/sell + BROKER_FEE без родителя —
    дочерние дублируют commission и не считаются дважды), margin: плата за маржу, last_ts}."""
    cond, args = [], []
    if from_ts is not None:
        cond.append("ts>=?"); args.append(float(from_ts))
    if to_ts is not None:
        cond.append("ts<?"); args.append(float(to_ts))
    where = (" WHERE " + " AND ".join(cond)) if cond else ""
    with _c() as c:
        rows = c.execute(f"SELECT kind, COUNT(*), COALESCE(SUM(fee),0), MAX(ts) FROM ops{where} GROUP BY kind",
                         args).fetchall()
        fee_rows = c.execute(f"SELECT raw FROM ops{where}{' AND' if cond else ' WHERE'} kind IN ('fee','margin')",
                             args).fetchall()
    by = {r[0]: int(r[1]) for r in rows}
    fee = sum(float(r[2] or 0) for r in rows if r[0] in ("buy", "sell"))
    margin = 0.0
    for fr in fee_rows:
        op = _j(fr["raw"]) or {}
        if op.get("kind") == "margin":
            margin += abs(float(op.get("payment") or 0.0))
        elif not op.get("parent"):
            fee += abs(float(op.get("payment") or 0.0))
    last = max((float(r[3] or 0) for r in rows), default=None)
    return {"count": sum(by.values()), "by_kind": by, "fee": round(fee, 2), "margin": round(margin, 2),
            "last_ts": last or None}


def kv_get(key: str, default=None):
    with _c() as c:
        r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    if not r:
        return default
    v = _j(r["value"])
    return default if v is None else v


def kv_set(key: str, value) -> None:
    with _c() as c:
        c.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, _d(value)))


if __name__ == "__main__":
    import os
    import tempfile
    DB_PATH = Path(tempfile.mkdtemp()) / "t.db"   # noqa: F811
    _schema()
    n = news_upsert_raw([{"source": "s", "title": "t1", "link": "http://a", "ts": time.time()},
                         {"source": "s", "title": "t2", "link": "http://b", "ts": time.time()},
                         {"source": "s", "title": "t1", "link": "http://a", "ts": time.time()}])
    assert n == 2, n
    unt = news_untriaged(3)
    assert len(unt) == 2
    news_set_relevant([unt[0]["id"]], 1)
    news_set_relevant([unt[1]["id"]], 0)
    assert len(news_relevant_uncharacterized(3)) == 1
    news_ai_put(unt[0]["id"], {"tone": 5, "one_liner": "x"})
    w = news_window(3)
    assert len(w) == 1 and w[0]["ai"]["tone"] == 5
    assert news_by_ids([unt[0]["id"][:6]])[0]["id"] == unt[0]["id"]
    st = news_stats(3)
    assert st == {"raw": 2, "relevant": 1, "characterized": 1, "untriaged": 0, "days": 3}, st
    # news_upsert_raw_ids: только добавленные; дубли в базе и внутри пачки не считаются
    ids3 = news_upsert_raw_ids([{"source": "s", "title": "t3", "link": "http://c", "ts": time.time()},
                                {"source": "s", "title": "t1", "link": "http://a", "ts": time.time()},
                                {"source": "s", "title": "t3", "link": "http://c", "ts": time.time()}])
    assert ids3 == [news_id("http://c", "t3")], ids3
    assert news_upsert_raw_ids([]) == [] and news_upsert_raw([{"title": "t3", "link": "http://c"}]) == 0
    assert news_stats(3)["raw"] == 3 and [x["id"] for x in news_untriaged(3)] == ids3
    council_put("r1", "daily", {"summary": {"regime": "risk-on", "picks": [{"ticker": "SBER"}]}})
    assert council_latest()["kind"] == "daily" and council_list()[0]["picks"] == ["SBER"]
    wid = watch_add({"severity": 80, "note": "n"})
    assert watch_unseen_count() == 1
    watch_mark_seen([wid])
    assert watch_unseen_count() == 0 and watch_list()[0]["data"]["severity"] == 80
    mission_put("sber", {"play": "auto"})
    assert mission_get("SBER")["play"] == "auto" and "SBER" in mission_all()
    t1 = trade_add({"ticker": "SBER", "side": "long", "lots": 3, "entry": 100, "exit_px": 101, "pnl": 30})
    trade_add({"ticker": "SBER", "side": "short", "lots": 1, "entry": 100, "exit_px": 101, "pnl": -10,
               "fee_est": 0.5, "figi": "F1"})
    s = trades_summary("SBER")
    assert (s["count"], s["pnl"], s["wins"], s["losses"], s["est"], s["confirmed"]) == (2, 19.5, 1, 1, True, 0), s
    assert s["net"] == 19.5 and s["gross"] == 20.0 and s["fee"] == 0.5 and s["by_day"][0]["count"] == 2, s
    row = trades("SBER")[0]
    assert row["est"] and row["source"] == "est" and row["net"] == -10.5 and row["figi"] == "F1" and row["fee"] == 0.5
    # сверка ledger: настоящие цены и комиссия → нетто от брокера, est снимается; ops — список id
    assert trade_update(t1, {"entry_real": 100.1, "exit_real": 101.0, "fee": 1.2, "pnl_gross": 27.0, "pnl_net": 25.8,
                             "source": "broker", "synced_ts": time.time(), "ops": ["op1", "op2"], "ticker": "HACK"})
    full = trades_full("SBER")
    assert full[0]["id"] == t1 and full[0]["ops"] == ["op1", "op2"] and full[0]["ticker"] == "SBER" and \
        full[0]["net"] == 25.8 and not full[0]["est"] and full[0]["data"]["lots"] == 3
    s = trades_summary("SBER")
    assert (s["net"], s["gross"], s["fee"], s["confirmed"], s["est"]) == (15.3, 17.0, 1.7, 1, True), s
    assert not trade_update(t1, {"nope": 1}) and trades_between(0, 1) == [] and len(trades_between(0, time.time() + 1)) == 2
    # операции: идемпотентно, фильтр по figi/uid и видам
    ops = [{"id": "o1", "ts": 10.0, "kind": "buy", "figi": "F1", "uid": "U1", "qty": 2, "price": 100.0, "payment": -200.0, "fee": 0.1},
           {"id": "o2", "ts": 11.0, "kind": "fee", "figi": "F1", "uid": "U1", "qty": 0, "payment": -0.3, "fee": 0.0},
           {"id": "o3", "ts": 12.0, "kind": "sell", "figi": "F2", "uid": "U2", "qty": 2, "price": 101.0, "payment": 202.0, "fee": 0.1}]
    assert ops_upsert(ops) == 3 and ops_upsert(ops) == 0 and ops_upsert([{"id": ""}]) == 0
    assert ops_upsert([dict(ops[1], kind="margin")]) == 0 and ops_list(["F1"], kinds=("margin",))[0]["id"] == "o2"
    assert ops_upsert([ops[1]]) == 0 and ops_list(["F1"], kinds=("fee",))[0]["id"] == "o2"     # вид обновлён обратно
    assert [o["id"] for o in ops_list(["U1"])] == ["o1", "o2"] and [o["id"] for o in ops_list(["F2"])] == ["o3"]
    assert [o["id"] for o in ops_list(["F1", "U2"], kinds=("buy", "sell"))] == ["o1", "o3"]
    assert ops_list(["F1"], from_ts=10.5) and ops_list(["F1"], from_ts=10.5)[0]["id"] == "o2" and ops_list(["ZZ"]) == []
    st_ = ops_stats()
    assert st_["count"] == 3 and st_["by_kind"] == {"buy": 1, "fee": 1, "sell": 1} and st_["fee"] == 0.5 and st_["last_ts"] == 12.0, st_
    # дочерняя BROKER_FEE дублирует commission родителя — не удваивается; MARGIN_FEE — отдельно; окно to_ts
    ops_upsert([{"id": "o4", "ts": 13.0, "kind": "fee", "figi": "F1", "uid": "U1", "qty": 0, "payment": -0.1, "fee": 0.0, "parent": "o1"},
                {"id": "o5", "ts": 14.0, "kind": "margin", "figi": None, "uid": None, "qty": 0, "payment": -7.0, "fee": 0.0}])
    st2 = ops_stats()
    assert st2["fee"] == 0.5 and st2["margin"] == 7.0 and st2["count"] == 5 and st2["by_kind"]["margin"] == 1, st2
    assert ops_stats(from_ts=13.5)["margin"] == 7.0 and ops_stats(to_ts=13.5)["margin"] == 0.0 and ops_stats(12.5, 13.5)["fee"] == 0.0
    # миграция: база старой схемы (без новых колонок) → _schema добавляет колонки, сделки на месте, повтор безвреден
    old_db = Path(tempfile.mkdtemp()) / "old.db"
    con = sqlite3.connect(old_db)
    con.executescript("""CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, side TEXT, lots INTEGER,
        entry REAL, exit_px REAL, pnl REAL, opened_ts REAL, closed_ts REAL, why TEXT, mode TEXT, json TEXT);
        INSERT INTO trades(ticker,side,lots,entry,exit_px,pnl,opened_ts,closed_ts,why,mode,json)
        VALUES('GAZP','long',2,10,11,20,1,2,'w','dry','{}');""")
    con.commit(); con.close()
    DB_PATH = old_db   # noqa: F811
    _schema(); _schema()
    with contextlib.closing(sqlite3.connect(old_db)) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(trades)").fetchall()}
    assert {c for c, _ in TRADE_EXTRA} <= cols, cols
    g = trades("GAZP")[0]
    assert g["pnl"] == 20 and g["est"] and g["net"] == 20.0 and g["source"] == "est" and g["fee"] == 0.0
    assert trades_summary("GAZP")["by_day"][0]["day"] == "1970-01-01"
    os.remove(old_db)
    DB_PATH = Path(tempfile.mkdtemp()) / "t2.db"   # noqa: F811
    _schema()
    kv_set("k", {"a": 1})
    assert kv_get("k")["a"] == 1 and kv_get("zz", 7) == 7
    os.remove(DB_PATH)
    print("store_v5 self-test OK")
