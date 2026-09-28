# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — ПЫЛЕСОС (Агент 1: запись сырой даты в SQLite).

Проект PREDATOR, Агент 1 «Data Ingestion Unit». Задача владельца: «писать в
БД каждый чих — полный стакан (до 50 уровней), спред в миллисекундах, ленту
сделок (сторона/объём). Никаких минутных свечей — только сырой поток».

Устройство:
  · переиспользует stream.TickStream (WebSocket MarketDataStream, парсинг
    кадров, реконнект) — НЕ дубль клиента, а слой персистентности над ним
    (закон проекта: новый слой = модуль рядом, не копия);
  · sink-колбэки потока (on_trade/on_book) кладут строки в буфер; фоновый
    флашер батчами executemany пишет в SQLite (WAL) — запись не блокирует
    event-loop (тяжёлый commit уходит в asyncio.to_thread);
  · таблицы append-only: trades (t_ms, figi, price, qty, side, dir) и book
    (t_ms, figi, obi, spread_bps, bb, ba, bid_vol, ask_vol, levels JSON до 50);
  · ключ — ТОЛЬКО из окружения TINKOFF_TOKEN (в код секрет не пишется);
  · читает Патологоанатом (Агент 3) — это его «место преступления».

Сеть здесь ЗАКОННА и READ-ONLY: приём рыночной даты, ордера НЕ шлются.
NO DUMMIES: нет токена → честный отказ. Без random. ⚫ 18+.

Запуск (у владельца, ключ в окружении):
    export TINKOFF_TOKEN=t.***
    python3 -m backend.recorder CR Si SILV      # тикеры или figi

Self-тест (без сети — синтетические кадры): python3 -m backend.recorder --selftest
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import time

try:
    from . import config, stream, tinkoff
except ImportError:                                        # запуск как скрипт
    import config, stream, tinkoff  # noqa: E401

log = logging.getLogger("pythia.recorder")

FLUSH_ROWS = 500       # буфер строк, после которого флашим не дожидаясь таймера
FLUSH_SEC = 2.0        # период флаша (сырой поток — частый, но батчами)
MAX_LEVELS = 50        # глубина стакана в записи (приказ: «полный, 50 уровней»)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id      INTEGER PRIMARY KEY,
    ts_ms   INTEGER NOT NULL,
    figi    TEXT NOT NULL,
    price   REAL NOT NULL,
    qty     REAL NOT NULL,
    side    INTEGER NOT NULL,          -- +1 покупка (агрессор), -1 продажа
    raw_dir TEXT
);
CREATE TABLE IF NOT EXISTS book (
    id        INTEGER PRIMARY KEY,
    ts_ms     INTEGER NOT NULL,
    figi      TEXT NOT NULL,
    obi       REAL NOT NULL,           -- дисбаланс объёмов (bid−ask)/(bid+ask)
    spread_bps REAL NOT NULL,
    best_bid  REAL, best_ask REAL,
    bid_vol   REAL, ask_vol REAL,
    levels    TEXT                     -- JSON {"b":[[p,q]..],"a":[[p,q]..]}
);
CREATE INDEX IF NOT EXISTS ix_trades_figi_ts ON trades(figi, ts_ms);
CREATE INDEX IF NOT EXISTS ix_book_figi_ts   ON book(figi, ts_ms);
"""


def _levels_json(bids, asks) -> str:
    """Верхние MAX_LEVELS уровней стакана → компактный JSON [[цена, объём]…]."""
    def side(rows):
        out = []
        for r in (rows or [])[:MAX_LEVELS]:
            p = stream._q((r or {}).get("price"))
            q = float((r or {}).get("quantity", 0) or 0)
            out.append([round(p, 9), q])
        return out
    return json.dumps({"b": side(bids), "a": side(asks)}, ensure_ascii=False)


class TickRecorder:
    """Пылесос: N инструментов → одна SQLite-база сырых тиков и стакана."""

    def __init__(self, figis, db_path=None, token: str | None = None,
                 flush_rows: int = FLUSH_ROWS, flush_sec: float = FLUSH_SEC):
        self.figis = list(figis)
        self.token = token or os.getenv("TINKOFF_TOKEN")   # секрет — из env
        self.db_path = str(db_path or (config.DATA_DIR / "ticks.db"))
        self.flush_rows = flush_rows
        self.flush_sec = flush_sec
        self.streams: dict = {}
        self._trade_buf: list = []
        self._book_buf: list = []
        self.rows_trades = 0
        self.rows_book = 0
        self._db: sqlite3.Connection | None = None
        self._flush_task: asyncio.Task | None = None
        self._stop = False

    # ── БД: WAL, батч-запись (тяжёлое — в поток, event-loop свободен) ───────
    def _open_db(self) -> None:
        mem = self.db_path == ":memory:"
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        if not mem:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # ── sink-колбэки потока (дёшево: только в буфер) ────────────────────────
    def _sink_trade(self, figi, t_ms, price, qty, side, raw_dir) -> None:
        self._trade_buf.append((int(t_ms), figi, float(price), float(qty),
                                int(side), str(raw_dir)))

    def _sink_book(self, figi, t_ms, obi, spread_bps, bb, ba, bids, asks) -> None:
        bv = sum(float((r or {}).get("quantity", 0) or 0) for r in (bids or []))
        av = sum(float((r or {}).get("quantity", 0) or 0) for r in (asks or []))
        self._book_buf.append((int(t_ms), figi, float(obi), float(spread_bps),
                               float(bb or 0.0), float(ba or 0.0),
                               bv, av, _levels_json(bids, asks)))

    def flush(self) -> int:
        """Слить буферы в БД одним батчем. Возврат: сколько строк записано."""
        if self._db is None:
            return 0
        tb, bb = self._trade_buf, self._book_buf
        self._trade_buf, self._book_buf = [], []
        n = 0
        if tb:
            self._db.executemany(
                "INSERT INTO trades(ts_ms,figi,price,qty,side,raw_dir) "
                "VALUES(?,?,?,?,?,?)", tb)
            self.rows_trades += len(tb); n += len(tb)
        if bb:
            self._db.executemany(
                "INSERT INTO book(ts_ms,figi,obi,spread_bps,best_bid,best_ask,"
                "bid_vol,ask_vol,levels) VALUES(?,?,?,?,?,?,?,?,?)", bb)
            self.rows_book += len(bb); n += len(bb)
        if n:
            self._db.commit()
        return n

    async def _flush_loop(self) -> None:
        while not self._stop:
            await asyncio.sleep(self.flush_sec)
            if len(self._trade_buf) + len(self._book_buf) > 0:
                try:
                    await asyncio.to_thread(self.flush)   # commit не блокирует loop
                except Exception as e:                     # noqa: BLE001
                    log.warning("флаш споткнулся (%s) — данные ждут в буфере",
                                str(e)[:80])

    def start(self) -> None:
        """Открыть БД, поднять поток на каждый инструмент, запустить флашер."""
        if not self.token:
            raise RuntimeError("нет TINKOFF_TOKEN в окружении — Пылесос без "
                               "ключа не подключается (NO DUMMIES)")
        self._open_db()
        self._stop = False
        for figi in self.figis:
            ts = stream.TickStream(figi, self.token)
            ts.on_trade = (lambda t, p, q, s, d, f=figi:
                           self._sink_trade(f, t, p, q, s, d))
            ts.on_book = (lambda t, o, sp, bb, ba, bd, ak, f=figi:
                          self._sink_book(f, t, o, sp, bb, ba, bd, ak))
            ts.start()
            self.streams[figi] = ts
        self._flush_task = asyncio.get_event_loop().create_task(self._flush_loop())
        log.info("ПЫЛЕСОС пошёл: %s → %s", ", ".join(self.figis), self.db_path)

    async def stop(self) -> None:
        self._stop = True
        for ts in self.streams.values():
            await ts.stop()
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except (asyncio.CancelledError, Exception):    # noqa: BLE001
                pass
        self.flush()
        if self._db is not None:
            self._db.close()

    def stats(self) -> dict:
        return {"db": self.db_path,
                "rows_trades": self.rows_trades, "rows_book": self.rows_book,
                "buf_trades": len(self._trade_buf), "buf_book": len(self._book_buf),
                "streams": {f: t.stats() for f, t in self.streams.items()}}


async def _run(tickers: list[str]) -> None:
    figis = []
    for tk in tickers:
        if tk.startswith("BBG") or "-" in tk or len(tk) > 12:
            figis.append(tk)                               # похоже на figi/uid
            continue
        inst = await tinkoff.resolve(tk, "futures")        # тикер → figi
        figi = (inst or {}).get("figi") or (inst or {}).get("uid")
        if figi:
            figis.append(figi)
            log.info("%s → %s", tk, figi)
        else:
            log.warning("не разрешил тикер %s — пропускаю", tk)
    if not figis:
        log.error("нет инструментов для записи")
        return
    rec = TickRecorder(figis)
    rec.start()
    try:
        while True:
            await asyncio.sleep(30)
            log.info("ПЫЛЕСОС: %s", rec.stats())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await rec.stop()
        log.info("ПЫЛЕСОС остановлен: %s", rec.stats())


# ── self-test: запись/чтение БЕЗ сети (синтетические кадры) ──────────────────
def _selftest() -> None:
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    rec = TickRecorder(["TEST"], db_path=tmp.name)
    rec._open_db()
    ts = stream.TickStream("TEST")
    ts.on_trade = lambda t, p, q, s, d, f="TEST": rec._sink_trade(f, t, p, q, s, d)
    ts.on_book = (lambda t, o, sp, bb, ba, bd, ak, f="TEST":
                  rec._sink_book(f, t, o, sp, bb, ba, bd, ak))
    now = "2026-08-07T12:00:00.500Z"

    # три сделки (2 buy, 1 sell) + один снимок стакана на 3 уровня
    ts.handle({"trade": {"direction": "TRADE_DIRECTION_BUY",
                         "price": {"units": 90, "nano": 0}, "quantity": "3",
                         "time": now}})
    ts.handle({"trade": {"direction": "TRADE_DIRECTION_SELL",
                         "price": {"units": 90, "nano": 100_000_000},
                         "quantity": "5", "time": now}})
    ts.handle({"trade": {"direction": "TRADE_DIRECTION_BUY",
                         "price": {"units": 90, "nano": 200_000_000},
                         "quantity": "2", "time": now}})
    ts.handle({"orderbook": {
        "bids": [{"price": {"units": 90, "nano": 0}, "quantity": "300"},
                 {"price": {"units": 89, "nano": 0}, "quantity": "150"}],
        "asks": [{"price": {"units": 91, "nano": 0}, "quantity": "100"}],
        "time": now}})

    assert rec.flush() == 4, "должны записаться 3 сделки + 1 стакан"
    assert rec.rows_trades == 3 and rec.rows_book == 1

    db = rec._db
    # сделки: сторона и объёмы сохранены точно, сырой поток не агрегирован
    rows = db.execute("SELECT price,qty,side FROM trades ORDER BY id").fetchall()
    assert rows[0] == (90.0, 3.0, 1) and rows[1] == (90.1, 5.0, -1), rows
    assert rows[2] == (90.2, 2.0, 1)
    # дельта объёма (CVD) считается прямо из БД: +3 −5 +2 = 0
    cvd = db.execute("SELECT SUM(qty*side) FROM trades").fetchone()[0]
    assert cvd == 0.0, cvd
    # стакан: OBI и объёмы, 50-уровневый JSON round-trip
    b = db.execute("SELECT obi,bid_vol,ask_vol,levels FROM book").fetchone()
    assert abs(b[1] - 450.0) < 1e-9 and abs(b[2] - 100.0) < 1e-9   # bid/ask vol
    assert abs(b[0] - (450 - 100) / 550) < 1e-9                    # OBI
    lv = json.loads(b[3])
    assert lv["b"][0] == [90.0, 300.0] and lv["a"][0] == [91.0, 100.0], lv

    # детерминизм парсинга: те же кадры → те же строки
    rec2 = TickRecorder(["TEST"], db_path=":memory:")
    rec2._open_db()
    ts2 = stream.TickStream("TEST")
    ts2.on_trade = lambda t, p, q, s, d: rec2._sink_trade("TEST", t, p, q, s, d)
    ts2.handle({"trade": {"direction": "TRADE_DIRECTION_BUY",
                          "price": {"units": 90, "nano": 0}, "quantity": "3",
                          "time": now}})
    rec2.flush()
    assert rec2._db.execute("SELECT price,qty,side FROM trades").fetchone() \
        == (90.0, 3.0, 1)

    # NO DUMMIES: без токена start() честно падает
    try:
        TickRecorder(["X"], token="").start()
        raise AssertionError("должен был отказать без токена")
    except RuntimeError as e:
        assert "TINKOFF_TOKEN" in str(e)

    db.close()
    rec2._db.close()
    os.unlink(tmp.name)
    for ext in ("-wal", "-shm"):
        try:
            os.unlink(tmp.name + ext)
        except OSError:
            pass
    print("recorder self-test OK: сырые тики (сторона/объём/цена) и стакан "
          "(OBI/объёмы/50 уровней JSON) пишутся в SQLite батчем, CVD считается "
          "из БД, детерминизм парсинга, без токена — честный отказ")


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    args = sys.argv[1:]
    if not args or args[0] == "--selftest":
        _selftest()
        return
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
