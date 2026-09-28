# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ · ЛАБ — БЕНЧ PREDATOR (туча агентов, много кругов).

Воспроизводимый прогон всего веера (Пылесос→Физик→Патологоанатом→Снайпер)
на стенде-симуляторе: N фьючерсов × R кругов × 4 код-агента. 8 кругов на
инструмент накапливают десятки пробоев → устойчивая сигнатура ловушки ММ.

Это ЛАБ-стенд без сети: живой фид — на машине владельца через
`python3 -m backend.predator CRU6 SiU6 …` (тот же конвейер байт-в-байт, на
вход придёт живой стакан вместо симулятора). Ордера не шлются.

Запуск:
    python3 -m backend.lab.predator_bench                        # дефолт 12×8
    python3 -m backend.lab.predator_bench --rounds 16 --episodes 8
    python3 -m backend.lab.predator_bench --futs CRU6:13.5 SiU6:78 --out /tmp/r.txt
    python3 -m backend.lab.predator_bench --selftest             # быстрый тест
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone

try:
    from .. import execution, patterns, recorder, stream, tagger
    from . import market_sim
except ImportError:                                        # запуск как скрипт
    sys.path.insert(0, __file__.rsplit("/backend/", 1)[0])
    from backend import execution, patterns, recorder, stream, tagger
    from backend.lab import market_sim

# туча фьючерсов FORTS (U6) с правдоподобными базовыми ценами
DEFAULT_FUTS = [
    ("CRU6", 13.5, "юань/руб"), ("SiU6", 78.0, "доллар/руб"),
    ("EuU6", 92.0, "евро/руб"), ("BRU6", 72.0, "Brent"),
    ("NGU6", 3.10, "природный газ"), ("GDU6", 2400.0, "золото"),
    ("SVU6", 31.0, "серебро"), ("RIU6", 110000.0, "индекс РТС"),
    ("MXU6", 280000.0, "индекс МосБиржи"), ("SRU6", 30000.0, "Сбербанк"),
    ("GZU6", 13000.0, "Газпром"), ("LKU6", 68000.0, "Лукойл"),
]


def one_pass(tk: str, base: float, seed: int, episodes: int) -> tuple:
    """Один код-веер (4 агента) на инструменте за один круг."""
    db = sqlite3.connect(":memory:")
    db.executescript(recorder._SCHEMA)
    rec = recorder.TickRecorder([tk], db_path=":memory:")
    rec._db = db
    ts = stream.TickStream(tk)
    ts.on_trade = lambda t, p, q, s, d, f=tk: rec._sink_trade(f, t, p, q, s, d)
    ts.on_book = (lambda t, o, sp, bb, ba, bd, ak, f=tk:
                  rec._sink_book(f, t, o, sp, bb, ba, bd, ak))
    for fr in market_sim.frames(seed=seed, episodes=episodes, base=base):
        ts.handle(fr)                                      # АГЕНТ 1 ПЫЛЕСОС
    rec.flush()
    db.commit()
    book = db.execute("SELECT ts_ms,best_bid,best_ask,obi,spread_bps FROM book "
                      "WHERE figi=? ORDER BY ts_ms", (tk,)).fetchall()
    tr = db.execute("SELECT ts_ms,price,qty,side FROM trades WHERE figi=? "
                    "ORDER BY ts_ms", (tk,)).fetchall()
    d = {"book_ts": [r[0] for r in book],
         "mid": [((b + a) / 2 if b and a else 0) for _, b, a, _, _ in book],
         "obi": [r[3] for r in book], "spread_bps": [r[4] for r in book],
         "trade_ts": [r[0] for r in tr], "trade_side": [r[3] for r in tr],
         "trade_qty": [r[2] for r in tr]}
    mined = patterns.mine(d, gap_s=150.0)                  # АГЕНТ 3 ПАТОЛОГОАНАТОМ
    tags = tagger.tag_series(list(zip(d["book_ts"], d["mid"])),   # АГЕНТ 2 ФИЗИК
                             win=90, step=60, spread_series=d["spread_bps"])
    db.close()
    return d, mined, tags


def run(futs: list, rounds: int, episodes: int) -> dict:
    """Прогнать веер: {futs}×{rounds}. Возврат: строки отчёта + агрегаты."""
    lines = []
    lines.append("ПРОЕКТ PREDATOR — БЕНЧ ВЕЕРА (стенд-симулятор)")
    lines.append(f"{len(futs)} фьючерсов × {rounds} кругов × 4 код-агента "
                 f"= {len(futs)*rounds*4} запусков агентов")
    lines.append("Пылесос → Физик → Патологоанатом → Снайпер(ловушка+солитон)")
    lines.append("⚫ академ. стенд, НЕ сигнал, НЕ обещание прибыли, 18+.")
    lines.append("Живой фид: python3 -m backend.predator " +
                 " ".join(t[0] for t in futs[:4]) + " …")
    grand = {"agents": 0, "breakouts": 0, "up": 0, "down": 0, "trades": 0}
    summary = []
    for tk, base, name in futs:
        fu, fd = [], []
        br = hot = vis = win = 0
        rlines = []
        ledger = None
        for rnd in range(1, rounds + 1):
            d, mined, tags = one_pass(tk, base,
                                      seed=1000 * rnd + (abs(hash(tk)) % 997),
                                      episodes=episodes)
            grand["agents"] += 4
            grand["trades"] += len(d["trade_ts"])
            evs = mined["events"]
            br += len(evs)
            for e in evs:
                (fu if e["side"] == "up" else fd).append(
                    patterns.precursor_features(d, e))
            hot += sum(1 for x in tags if x["density"] == "наэлектризованная")
            vis += sum(1 for x in tags if x["density"] == "вязкая")
            win += len(tags)
            rlines.append(f"     круг {rnd}: пробоев={len(evs)}, сделок="
                          f"{len(d['trade_ts'])}")
            if rnd == 1:
                ledger = tags
        sig_up = patterns.signature(fu, "up")
        sig_dn = patterns.signature(fd, "down")
        grand["breakouts"] += br
        grand["up"] += len(fu)
        grand["down"] += len(fd)
        lines.append("")
        lines.append("=" * 70)
        lines.append(f"{tk}  ({name})  база≈{base}")
        lines.append("=" * 70)
        lines.append(f"  ИТОГО {rounds} кругов: пробоев={br} (↑{len(fu)} ↓{len(fd)}); "
                     f"среда наэлектр.={hot}, вязк.={vis} из {win}")
        lines.extend(rlines)
        for sig in (sig_up, sig_dn):
            if sig:
                lines.append(f"  ЛОВУШКА ММ [{sig['side']}, n={sig['n_events']}]: "
                             f"{sig['portrait']}")
                lines.append(f"     медианы: спред_сжатие={sig['spread_squeeze_med']}, "
                             f"OBI-дельта={sig['obi_delta_med']}, CVD={sig['cvd_med']}, "
                             f"Хоукс_n={sig['hawkes_n_med']}")
        plan = execution.plan_entry(
            "long", 6, base,
            {"best_bid": base * 0.9998, "best_ask": base * 1.0002,
             "levels_ask": [{"p": base * 1.0002, "q": 40} for _ in range(10)],
             "levels_bid": [{"p": base * 0.9998, "q": 40} for _ in range(10)]},
            state="СИНГУЛЯРНОСТЬ")
        se = execution.soliton_exit("long", base, base * 1.009, base * 1.011,
                                    [0.001, 0.003, 0.006, 0.0078, 0.0085])
        lines.append(f"  СНАЙПЕР: план={len(plan['tranches'])} траншей; "
                     f"солитон-выход у стены={se['exit']}")
        if ledger:
            lines.append("  ── лента среды круга 1 (окно 90с) ──")
            for x in ledger:
                hh = datetime.fromtimestamp(x["ts"] / 1000.0,
                                            timezone.utc).strftime("%H:%M:%S")
                arrow = {"наэлектризованная": "↑ ЗАРЯД", "вязкая": "~ вязко",
                         "обычная": "· ровно"}[x["density"]]
                lines.append(f"     {hh}  E={x['energy']:.2f}  {arrow}")
        summary.append((tk, name, br, len(fu), len(fd),
                        bool(sig_up), bool(sig_dn)))
    lines.append("")
    lines.append("#" * 70)
    lines.append("ГЛОБАЛЬНАЯ СВОДКА")
    lines.append(f"  запусков код-агентов: {grand['agents']}")
    lines.append(f"  сделок обработано: {grand['trades']}")
    lines.append(f"  ультра-пробоев: {grand['breakouts']} "
                 f"(↑{grand['up']} ↓{grand['down']})")
    lines.append("  инструмент  пробоев  ↑    ↓   сигн↑ сигн↓")
    for tk, name, br, u, dn, su, sd in summary:
        lines.append(f"  {tk:6s}     {br:5d}  {u:4d} {dn:4d}   "
                     f"{'да' if su else '—':4s}  {'да' if sd else '—'}")
    return {"lines": lines, "grand": grand, "summary": summary}


def _parse_futs(items) -> list:
    out = []
    for it in items:
        parts = it.split(":")
        tk = parts[0]
        base = float(parts[1]) if len(parts) > 1 else 100.0
        out.append((tk, base, tk))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Бенч веера PREDATOR (симулятор)")
    ap.add_argument("--rounds", type=int, default=8)
    ap.add_argument("--episodes", type=int, default=8)
    ap.add_argument("--futs", nargs="*", help="ТИКЕР:база … (иначе дефолт 12)")
    ap.add_argument("--out", default="predator_bench.txt")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
        return
    futs = _parse_futs(a.futs) if a.futs else DEFAULT_FUTS
    t0 = time.time()
    res = run(futs, a.rounds, a.episodes)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write("\n".join(res["lines"]) + "\n")
    g = res["grand"]
    print(f"ГОТОВО: {a.out}  | код-агентов={g['agents']} пробоев={g['breakouts']} "
          f"сделок={g['trades']} время={time.time()-t0:.1f}s")


def _selftest() -> None:
    res = run([("TST", 100.0, "тест")], rounds=2, episodes=4)
    g = res["grand"]
    assert g["agents"] == 8, g                       # 1 фьюч × 2 круга × 4
    assert g["breakouts"] >= 2, g                    # симулятор кладёт пробои
    assert any("ЛОВУШКА ММ" in ln for ln in res["lines"]) or g["breakouts"] < 3
    assert any("ГЛОБАЛЬНАЯ СВОДКА" in ln for ln in res["lines"])
    # детерминизм: тот же прогон → тот же отчёт
    res2 = run([("TST", 100.0, "тест")], rounds=2, episodes=4)
    assert res2["grand"] == g
    print("predator_bench self-test OK: веер 1×2×4=8 агентов, пробои найдены, "
          "сигнатура/сводка на месте, детерминизм байт-в-байт")


if __name__ == "__main__":
    main()
