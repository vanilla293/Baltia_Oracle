# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — PREDATOR (оркестратор конвейера 4 агентов).

Сценарий владельца дословно: «сиди кушай данные, пока 00:00 не стукнет, там
накопив кучу данных смотришь на каждую секунду — где пишет вверх, где так-сяк,
и смотрим, как работает вакуум на бирже, толпа с маркетмейкерами».

Оркестратор связывает агентов в один прогон:
  1. ПЫЛЕСОС (recorder) — пишет сырые тики и стакан в SQLite до отсечки времени
     (по умолчанию ближайшая полночь локально; --until HH:MM переопределяет);
  2. на отсечке — стоп записи;
  3. ФИЗИК (tagger) — размечает mid-ряд каждого инструмента: где среда вязкая,
     где наэлектризованная (посекундная карта плотности);
  4. ПАТОЛОГОАНАТОМ (patterns) — ищет ультра-пробои, мотает на 60с, выдаёт
     сигнатуру ловушки ММ; сигнатуры → JSON для Снайпера.

Всё офлайн после записи, детерминированно. Сеть — только приём даты Пылесосом
(read-only, ордера НЕ шлются). Ключ — ТОЛЬКО из окружения TINKOFF_TOKEN.

Запуск у владельца:
    export TINKOFF_TOKEN=t.***
    python3 -m backend.predator CR Si SILV                # до ближайшей 00:00
    python3 -m backend.predator CR --until 23:30          # своя отсечка
    python3 -m backend.predator --analyze CR              # только разбор БД

⚫ Итог — карта прошлого, НЕ обещание будущего и НЕ сигнал. 18+.

Self-тест (без сети): python3 -m backend.predator --selftest
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta

try:
    from . import config, patterns, recorder, tagger, tinkoff
except ImportError:                                        # запуск как скрипт
    import config, patterns, recorder, tagger, tinkoff  # noqa: E401

log = logging.getLogger("pythia.predator")


def seconds_until(hhmm: str | None) -> float:
    """Секунд до отсечки: HH:MM сегодня (или завтра, если уже прошло); None →
    ближайшая полночь. Без сети, чистая арифметика времени."""
    now = datetime.now()
    if hhmm:
        h, m = (int(x) for x in hhmm.split(":", 1))
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    else:
        target = now.replace(hour=0, minute=0, second=0, microsecond=0) \
            + timedelta(days=1)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def analyze_db(db_path: str, figi: str, ticker: str = "") -> dict:
    """ФИЗИК + ПАТОЛОГОАНАТОМ по накопленной БД одного инструмента."""
    data = patterns.load_from_db(db_path, figi)
    label = ticker or figi
    # Физик: посекундная карта плотности по mid-ряду (+спред для Казимира)
    ts_prices = list(zip(data["book_ts"], data["mid"]))
    dens = tagger.tag_series(ts_prices, spread_series=data.get("spread_bps"))
    hot = sum(1 for d in dens if d["density"] == "наэлектризованная")
    cold = sum(1 for d in dens if d["density"] == "вязкая")
    # Патологоанатом: пробои + сигнатуры ловушек
    mined = patterns.mine(data)
    log.info("[%s] тиков стакана=%d сделок=%d | плотность: горячих окон=%d "
             "вязких=%d | пробоев=%d", label, len(data["mid"]),
             len(data["trade_ts"]), hot, cold, mined["n_breakouts"])
    if mined.get("signature_up"):
        log.info("[%s] ВВЕРХ: %s", label, mined["signature_up"]["portrait"])
    if mined.get("signature_down"):
        log.info("[%s] ВНИЗ: %s", label, mined["signature_down"]["portrait"])
    return {"ticker": label, "figi": figi,
            "density": {"hot_windows": hot, "cold_windows": cold,
                        "total_windows": len(dens)},
            "breakouts": mined["n_breakouts"],
            "signature_up": mined.get("signature_up"),
            "signature_down": mined.get("signature_down")}


async def run(tickers: list[str], until: str | None = None) -> None:
    """Полный прогон: запись до отсечки → разбор → сигнатуры в JSON."""
    figis, tk_by_figi = [], {}
    for tk in tickers:
        inst = await tinkoff.resolve(tk, "futures")
        figi = (inst or {}).get("figi") or (inst or {}).get("uid")
        if figi:
            figis.append(figi); tk_by_figi[figi] = tk
            log.info("%s → %s", tk, figi)
        else:
            log.warning("не разрешил %s — пропуск", tk)
    if not figis:
        log.error("нет инструментов")
        return
    left = seconds_until(until)
    log.info("ПЫЛЕСОС: копим %d инструментов ещё %.0f мин (до %s)",
             len(figis), left / 60.0, until or "ближайшей 00:00")
    rec = recorder.TickRecorder(figis)
    rec.start()
    try:
        await asyncio.sleep(left)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("прервано вручную — разбираю что накопил")
    finally:
        await rec.stop()
    log.info("ЗАПИСЬ ЗАВЕРШЕНА: %s", rec.stats())

    # разбор накопленного (Физик + Патологоанатом)
    report = {}
    for figi in figis:
        try:
            report[tk_by_figi[figi]] = analyze_db(rec.db_path, figi,
                                                  tk_by_figi[figi])
        except Exception as e:                              # noqa: BLE001
            log.warning("разбор %s споткнулся: %s", tk_by_figi[figi], str(e)[:100])
    out = config.DATA_DIR / "predator_signatures.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    log.info("СИГНАТУРЫ ЛОВУШЕК → %s", out)


async def analyze_only(tickers: list[str]) -> None:
    """Только разбор уже накопленной БД (без новой записи)."""
    db_path = str(config.DATA_DIR / "ticks.db")
    for tk in tickers:
        inst = await tinkoff.resolve(tk, "futures")
        figi = (inst or {}).get("figi") or (inst or {}).get("uid") or tk
        try:
            analyze_db(db_path, figi, tk)
        except Exception as e:                              # noqa: BLE001
            log.warning("разбор %s: %s", tk, str(e)[:100])


# ── self-test: оркестрация БЕЗ сети (синтетическая БД) ──────────────────────
def _selftest() -> None:
    import os
    import sqlite3
    import tempfile

    # 1) арифметика отсечки: до полуночи всегда в (0, 24ч]; HH:MM в прошлом →
    #    завтра (>0); детерминизм знака
    s_mid = seconds_until(None)
    assert 0 < s_mid <= 24 * 3600 + 1, s_mid
    assert seconds_until("00:01") > 0

    # 2) сборка синтетической БД Пылесоса и разбор её Физиком+Патологоанатомом
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
    db = sqlite3.connect(tmp.name)
    db.executescript(recorder._SCHEMA)
    # фон + один срыв вверх +1.5% без отката, с предвестником (сжатие спреда,
    # односторонний buy-поток, перекладка OBI вверх)
    t = 0; base = 100.0
    book_rows, trade_rows = [], []
    for _ in range(120):                                   # спокойный фон
        book_rows.append((t * 1000, "F", 0.0, 3.0, base - 0.5, base + 0.5,
                          100.0, 100.0, "{}"))
        if t % 3 == 0:
            trade_rows.append((t * 1000, "F", base, 2.0,
                               1 if t % 6 == 0 else -1, "d"))
        t += 1
    for k in range(60):                                    # предвестник 60с
        book_rows.append((t * 1000, "F", min(0.6, k / 100.0),
                          max(0.6, 3.0 - k * 0.04), base - 0.5, base + 0.5,
                          150.0, 100.0, "{}"))
        for _r in range(1 + k // 12):
            trade_rows.append((t * 1000 + _r * 50, "F", base, 3.0, 1, "d"))
        t += 1
    for k in range(30):                                    # срыв +1.5%
        p = base * (1.0 + 0.015 * (k + 1) / 30.0)
        book_rows.append((t * 1000, "F", 0.5, 1.0, p - 0.4, p + 0.4,
                          200.0, 100.0, "{}"))
        trade_rows.append((t * 1000, "F", p, 5.0, 1, "d"))
        t += 1
    db.executemany("INSERT INTO book(ts_ms,figi,obi,spread_bps,best_bid,"
                   "best_ask,bid_vol,ask_vol,levels) VALUES(?,?,?,?,?,?,?,?,?)",
                   book_rows)
    db.executemany("INSERT INTO trades(ts_ms,figi,price,qty,side,raw_dir) "
                   "VALUES(?,?,?,?,?,?)", trade_rows)
    db.commit(); db.close()

    rep = analyze_db(tmp.name, "F", "TESTFUT")
    assert rep["breakouts"] >= 1, rep
    assert rep["density"]["total_windows"] > 0, rep
    sig = rep["signature_up"]
    assert sig is None or sig["side"] == "up"             # ≥3 пробоев для сигнатуры
    # один срыв → сигнатура ещё не набирается (MIN_EVENTS), но пробой найден
    assert rep["breakouts"] == 1, rep

    os.unlink(tmp.name)
    for ext in ("-wal", "-shm"):
        try:
            os.unlink(tmp.name + ext)
        except OSError:
            pass
    print("predator self-test OK: отсечка времени считается, синтетическая БД "
          "Пылесоса разбирается Физиком (карта плотности) и Патологоанатомом "
          "(пробой найден); оркестрация офлайн, детерминизм")


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    args = sys.argv[1:]
    if not args or args[0] == "--selftest":
        _selftest()
        return
    if args[0] == "--analyze":
        asyncio.run(analyze_only(args[1:]))
        return
    until = None
    if "--until" in args:
        i = args.index("--until")
        until = args[i + 1]
        args = args[:i] + args[i + 2:]
    asyncio.run(run(args, until))


if __name__ == "__main__":
    main()
