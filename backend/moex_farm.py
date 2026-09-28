# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — ФАРМ MOEX ISS (реальные биржевые данные, без токена).

Публичный ISS Московской биржи отдаёт БЕЗ авторизации:
  · marketdata — BID/OFFER/SPREAD/LAST/NUMTRADES/VOLTODAY по ВСЕМ бумагам
    рынка одним запросом (вежливо к серверу: 1 запрос = весь рынок);
  · trades — ленту сделок с ценой, объёмом, СТОРОНОЙ агрессора (BUYSELL)
    и временем, до 5000 записей за запрос, с пагинацией по `start`.
Стакан (orderbook) публично закрыт — поэтому OBI недоступен, и модуль
честно его не выдумывает (NO DUMMIES): пишет только то, что реально есть.

Что даёт этот источник конвейеру PREDATOR:
  · mid = (BID+OFFER)/2 и spread_bps — ряд для Патологоанатома и Казимира;
  · сырые сделки со стороной — CVD, VPIN и ветвление Хоукса;
  · всё РЕАЛЬНОЕ: это фактическая микроструктура MOEX, не симулятор.

Схема БД совместима с recorder.py (Пылесос), поэтому patterns.load_from_db
читает её без изменений. Поле obi пишется как 0.0 с пометкой в note_source
(стакана нет) — чтобы потребитель не принял ноль за измерение.

Сеть здесь ЗАКОННА и READ-ONLY: приём публичных рыночных данных, ордера не
шлются, авторизация не требуется. ⚫ Данные — сырьё, не сигнал. 18+.

Запуск:
    python3 -m backend.moex_farm --minutes 30            # фарм 30 минут
    python3 -m backend.moex_farm --minutes 120 --db data/moex.db
    python3 -m backend.moex_farm --selftest              # без сети
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("pythia.moex_farm")

ISS = "https://iss.moex.com/iss"
UA = "pythia-research/4.3 (educational microstructure study)"
POLL_SEC = 3.0          # период опроса котировок (вежливо: 2 запроса/3с)
TRADES_EVERY = 40       # каждый N-й тик тянем ленты (~раз в 2 минуты)
TRADES_LIMIT = 1500     # записей ленты за запрос: хватает на перекрытие,
                        # но не топит цикл (5000 × 16 инстр. вешали фарм)
HTTP_TIMEOUT = 12.0     # ISS иногда «держит» соединение — рвём раньше
LEDGER_MAX = 6          # по скольким инструментам тянуть ЛЕНТУ (остальные —
                        # только котировки: ряд цен и спред всё равно есть)

# фьючерсы FORTS и акции TQBR — фармим и те, и те
FUTURES = ["CRU6", "SiU6", "BRU6", "GDU6", "SVU6", "RIU6", "MXU6", "NGU6"]
SHARES = ["SBER", "GAZP", "LKOH", "GMKN", "VTBR", "ROSN", "NVTK", "MTSS"]
# ленты — только по самым ликвидным (там микроструктура и живёт)
LEDGER_FUT = ["CRU6", "SiU6", "BRU6"]
LEDGER_SHR = ["SBER", "GAZP", "LKOH"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id      INTEGER PRIMARY KEY,
    ts_ms   INTEGER NOT NULL,
    figi    TEXT NOT NULL,          -- тикер MOEX (SECID)
    price   REAL NOT NULL,
    qty     REAL NOT NULL,
    side    INTEGER NOT NULL,       -- +1 агрессор-покупатель, -1 продавец
    raw_dir TEXT,
    tradeno INTEGER                 -- номер сделки ISS (дедуп/пагинация)
);
CREATE TABLE IF NOT EXISTS book (
    id         INTEGER PRIMARY KEY,
    ts_ms      INTEGER NOT NULL,
    figi       TEXT NOT NULL,
    obi        REAL NOT NULL,       -- 0.0: публичного стакана нет (см. note)
    spread_bps REAL NOT NULL,
    best_bid   REAL, best_ask REAL,
    bid_vol    REAL, ask_vol REAL,  -- NULL: объёмы стакана недоступны
    levels     TEXT                 -- NULL: уровней стакана нет
);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY, v TEXT
);
CREATE INDEX IF NOT EXISTS ix_trades_figi_ts ON trades(figi, ts_ms);
CREATE INDEX IF NOT EXISTS ix_book_figi_ts   ON book(figi, ts_ms);
CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_no ON trades(figi, tradeno);
"""

NOTE_SOURCE = ("MOEX ISS публичный: mid/spread из marketdata (BID/OFFER), "
               "сделки со стороной из trades; СТАКАНА НЕТ (закрыт публично) "
               "→ obi=0.0 не измерение, а отсутствие данных; "
               "bid_vol/ask_vol/levels = NULL")


def _get(path: str, params: dict) -> dict:
    """GET к ISS с таймаутом. Ошибка сети → пустой dict (петля не падает)."""
    url = f"{ISS}/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:                                   # noqa: BLE001
        log.debug("ISS %s: %s", path, str(e)[:80])
        return {}


def _rows(block: dict, name: str) -> list:
    """Блок ISS {columns, data} → список словарей."""
    b = (block or {}).get(name) or {}
    cols = b.get("columns") or []
    return [dict(zip(cols, row)) for row in (b.get("data") or [])]


def quotes_futures() -> list:
    """Котировки ВСЕХ фьючерсов одним запросом."""
    d = _get("engines/futures/markets/forts/securities.json", {
        "iss.meta": "off", "iss.only": "marketdata",
        "marketdata.columns": "SECID,BID,OFFER,SPREAD,LAST,NUMTRADES,VOLTODAY,UPDATETIME"})
    return _rows(d, "marketdata")


def quotes_shares() -> list:
    """Котировки ВСЕХ акций TQBR одним запросом."""
    d = _get("engines/stock/markets/shares/boards/TQBR/securities.json", {
        "iss.meta": "off", "iss.only": "marketdata",
        "marketdata.columns": "SECID,BID,OFFER,SPREAD,LAST,NUMTRADES,VALTODAY,UPDATETIME"})
    return _rows(d, "marketdata")


def trades_of(secid: str, is_future: bool, start: int = 0) -> list:
    """Лента сделок инструмента — СВЕЖИЕ сделки (reversed=1).

    Без reversed ISS отдаёт НАЧАЛО дня (первые 5000 сделок с открытия) —
    для микроструктуры «сейчас» это бесполезно. reversed=1 отдаёт хвост
    ленты: для ликвидной бумаги 5000 записей ≈ последний час торгов.
    Перекрытие пачек безвредно — дедуп по UNIQUE(figi, tradeno)."""
    if is_future:
        path = f"engines/futures/markets/forts/securities/{secid}/trades.json"
    else:
        path = f"engines/stock/markets/shares/boards/TQBR/securities/{secid}/trades.json"
    d = _get(path, {"iss.meta": "off", "iss.only": "trades",
                    "limit": TRADES_LIMIT, "reversed": 1, "start": start})
    return _rows(d, "trades")


def _ts_ms(row: dict) -> int:
    """SYSTIME/TRADETIME → unix ms (МСК = UTC+3)."""
    s = row.get("SYSTIME") or ""
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return int((dt.timestamp() - 3 * 3600) * 1000)       # МСК → UTC
    except (ValueError, TypeError):
        return int(time.time() * 1000)


def _side(row: dict) -> int:
    """BUYSELL: 'B' покупатель-агрессор → +1, 'S' → −1, иначе 0 (пропуск)."""
    v = str(row.get("BUYSELL") or "").upper()
    return 1 if v == "B" else (-1 if v == "S" else 0)


class MoexFarm:
    """Фарм реальных данных MOEX: котировки в ряд, сделки в ленту."""

    def __init__(self, db_path: str, futures=None, shares=None):
        self.db_path = db_path
        self.futures = list(futures if futures is not None else FUTURES)
        self.shares = list(shares if shares is not None else SHARES)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('source',?)",
                        (NOTE_SOURCE,))
        self.db.commit()
        self.rows_book = 0
        self.rows_trades = 0
        self.last_tradeno: dict = {}
        self.polls = 0
        self.errors = 0

    # ── котировки → строка ряда (mid/spread) ───────────────────────────────
    def _sink_quotes(self, rows: list, wanted: set) -> int:
        now_ms = int(time.time() * 1000)
        batch = []
        for r in rows:
            sec = r.get("SECID")
            if sec not in wanted:
                continue
            bid, ask = r.get("BID"), r.get("OFFER")
            if not bid or not ask or bid <= 0 or ask <= 0:
                continue                                     # нет котировки — не выдумываем
            spread_bps = (ask - bid) / bid * 1e4 if bid else 0.0
            batch.append((now_ms, sec, 0.0, spread_bps, float(bid), float(ask),
                          None, None, None))
        if batch:
            self.db.executemany(
                "INSERT INTO book(ts_ms,figi,obi,spread_bps,best_bid,best_ask,"
                "bid_vol,ask_vol,levels) VALUES(?,?,?,?,?,?,?,?,?)", batch)
            self.rows_book += len(batch)
        return len(batch)

    # ── лента сделок (инкрементально по TRADENO) ───────────────────────────
    def _sink_trades(self, secid: str, is_future: bool) -> int:
        rows = trades_of(secid, is_future)
        if not rows:
            return 0
        last = self.last_tradeno.get(secid, 0)
        batch = []
        mx = last
        for r in rows:
            tno = int(r.get("TRADENO") or 0)
            if tno <= last:
                continue
            side = _side(r)
            if side == 0:
                continue
            px = float(r.get("PRICE") or 0)
            qty = float(r.get("QUANTITY") or 0)
            if px <= 0 or qty <= 0:
                continue
            batch.append((_ts_ms(r), secid, px, qty, side,
                          str(r.get("BUYSELL") or ""), tno))
            mx = max(mx, tno)
        if batch:
            self.db.executemany(
                "INSERT OR IGNORE INTO trades(ts_ms,figi,price,qty,side,"
                "raw_dir,tradeno) VALUES(?,?,?,?,?,?,?)", batch)
            self.rows_trades += len(batch)
            self.last_tradeno[secid] = mx
        return len(batch)

    def tick(self, with_trades: bool = False) -> dict:
        """Один цикл: котировки обоих рынков (+опц. ленты сделок)."""
        got_b = got_t = 0
        try:
            if self.futures:
                got_b += self._sink_quotes(quotes_futures(), set(self.futures))
            if self.shares:
                got_b += self._sink_quotes(quotes_shares(), set(self.shares))
        except Exception as e:                               # noqa: BLE001
            self.errors += 1
            log.warning("котировки: %s", str(e)[:80])
        self.db.commit()          # котировки фиксируем СРАЗУ: тяжёлая выкачка
                                  # лент ниже не должна задерживать ряд цен
        if with_trades:
            # ленты — только по ликвидным: тяжёлая выкачка по всем 16 вешала
            # цикл на минуты и ряд цен переставал обновляться
            for sec in [s for s in self.futures if s in LEDGER_FUT][:LEDGER_MAX]:
                try:
                    got_t += self._sink_trades(sec, True)
                except Exception:                            # noqa: BLE001
                    self.errors += 1
            for sec in [s for s in self.shares if s in LEDGER_SHR][:LEDGER_MAX]:
                try:
                    got_t += self._sink_trades(sec, False)
                except Exception:                            # noqa: BLE001
                    self.errors += 1
        self.db.commit()
        self.polls += 1
        return {"book": got_b, "trades": got_t}

    def run(self, minutes: float, poll_sec: float = POLL_SEC) -> dict:
        """Фарм в течение minutes минут. Возврат: статистика."""
        t_end = time.time() + minutes * 60.0
        n = 0
        log.info("ФАРМ ПОШЁЛ: %d фьюч + %d акций → %s (%.0f мин)",
                 len(self.futures), len(self.shares), self.db_path, minutes)
        while time.time() < t_end:
            t0 = time.time()
            n += 1
            # ленты — начиная с 5-го тика: пусть ряд цен сперва пойдёт
            r = self.tick(with_trades=(n >= 5 and n % TRADES_EVERY == 5))
            if n % 20 == 0:
                log.info("тик %d: строк книги=%d сделок=%d (ошибок %d)",
                         n, self.rows_book, self.rows_trades, self.errors)
            dt = poll_sec - (time.time() - t0)
            if dt > 0:
                time.sleep(dt)
        return self.stats()

    def stats(self) -> dict:
        return {"db": self.db_path, "polls": self.polls,
                "rows_book": self.rows_book, "rows_trades": self.rows_trades,
                "errors": self.errors,
                "instruments": len(self.futures) + len(self.shares)}

    def close(self) -> None:
        self.db.commit()
        self.db.close()


def _selftest() -> None:
    """Без сети: разбор строк ISS и запись в БД."""
    import tempfile, os
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
    f = MoexFarm(tmp.name, futures=["CRU6"], shares=[])

    # 1) котировки: mid/spread считаются, пустая котировка пропускается
    n = f._sink_quotes([
        {"SECID": "CRU6", "BID": 12.357, "OFFER": 12.358},
        {"SECID": "CRU6", "BID": 0, "OFFER": 0},          # нет котировки
        {"SECID": "XXXX", "BID": 1, "OFFER": 2},          # чужой инструмент
    ], {"CRU6"})
    assert n == 1, n
    row = f.db.execute("SELECT best_bid,best_ask,spread_bps,obi,levels "
                       "FROM book").fetchone()
    assert abs(row[0] - 12.357) < 1e-9 and abs(row[1] - 12.358) < 1e-9
    assert abs(row[2] - (0.001 / 12.357 * 1e4)) < 1e-6      # спред в бпс
    assert row[3] == 0.0 and row[4] is None                 # стакана нет — честно

    # 2) сторона агрессора и время
    assert _side({"BUYSELL": "B"}) == 1 and _side({"BUYSELL": "S"}) == -1
    assert _side({"BUYSELL": "N"}) == 0                     # не классифицирована
    t = _ts_ms({"SYSTIME": "2026-08-07 21:00:00"})
    assert t > 1_700_000_000_000                            # мс, не секунды

    # 3) разбор блока ISS
    rows = _rows({"trades": {"columns": ["TRADENO", "PRICE"],
                             "data": [[1, 12.3], [2, 12.4]]}}, "trades")
    assert rows == [{"TRADENO": 1, "PRICE": 12.3}, {"TRADENO": 2, "PRICE": 12.4}]

    # 4) метка источника записана (потребитель узнает про отсутствие стакана)
    src = f.db.execute("SELECT v FROM meta WHERE k='source'").fetchone()[0]
    assert "СТАКАНА НЕТ" in src

    f.close(); os.unlink(tmp.name)
    for ext in ("-wal", "-shm"):
        try: os.unlink(tmp.name + ext)
        except OSError: pass
    print("moex_farm self-test OK: котировки → mid/spread, пустые пропущены, "
          "сторона агрессора и время разобраны, отсутствие стакана помечено "
          "честно (obi=0.0 + note), схема совместима с patterns.load_from_db")


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Фарм реальных данных MOEX ISS")
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--db", default="data/moex.db")
    ap.add_argument("--poll", type=float, default=POLL_SEC)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
        return
    farm = MoexFarm(a.db)
    try:
        st = farm.run(a.minutes, a.poll)
        print("ФАРМ ЗАВЕРШЁН:", st)
    except KeyboardInterrupt:
        print("прервано:", farm.stats())
    finally:
        farm.close()


if __name__ == "__main__":
    main()
