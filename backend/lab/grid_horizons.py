# -*- coding: utf-8 -*-
"""Сетка ОКНО ПРОШЛОГО × ГОРИЗОНТ для правила АНТИМОМЕНТУМ (вход против хода окна).

Причинность строгая: ret_win считается по px[i-N .. i-1] (строго ДО точки
решения i), вход по px[i], факт — первый тик на/после ts[i]+H*1000.
Соглашения взяты байт-в-байт из backend/lab/forward_test.py: шаг решения 30с,
DEAD_FRAC=5e-5 (мёртвый ход не судим), MIN_CELL=30 (меньше — не судим).
Комиссия 5 бп за круг вычитается из среднего хода — обязательно.

⚫ Измерение прошлого на конкретной выборке. НЕ сигнал, НЕ обещание. 18+.
"""
from __future__ import annotations

import math
import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/backend/", 1)[0])
from backend.lab.forward_test import load_series, instruments  # noqa: E402

DB = "data/moex_real.db"
LOOKBACKS = (100, 200, 400, 800)
HORIZONS = (30, 60, 180, 600, 1800)
STEP_S = 30
DEAD_FRAC = 5e-5
MIN_CELL = 30
COMMISSION_BPS = 5.0


def sigma(hits: int, n: int) -> float:
    """(hits - n/2)/sqrt(n/4) — сколько сигма от честной монеты."""
    return (hits - n / 2.0) / math.sqrt(n / 4.0) if n > 0 else 0.0


def collect(db: str):
    """Наблюдения по всем инструментам: (sec, N, H) -> список (hit, ret, t)."""
    cells: dict = {}
    meta = {}
    for sec in instruments(db):
        d = load_series(db, sec)
        ts, px = d["ts"], d["px"]
        n = len(ts)
        arr_ts = np.asarray(ts)
        meta[sec] = (n, (ts[-1] - ts[0]) / 1000.0)
        for N in LOOKBACKS:
            if n <= N:
                continue
            t_next = ts[N]
            for i in range(N, n):
                if ts[i] < t_next:
                    continue
                t_next = ts[i] + STEP_S * 1000
                p_lo, p_hi = px[i - N], px[i - 1]      # строго прошлое
                if p_lo <= 0:
                    continue
                ret_win = (p_hi - p_lo) / p_lo
                if ret_win == 0.0:                      # нет хода — нет входа
                    continue
                pdir = -1 if ret_win > 0 else 1         # АНТИМОМЕНТУМ
                p0 = px[i]                              # вход по цене решения
                if p0 <= 0:
                    continue
                for H in HORIZONS:
                    j = int(np.searchsorted(arr_ts, ts[i] + H * 1000, side="left"))
                    if j >= n:
                        continue                        # факта нет — не судим
                    fwd = (px[j] - p0) / p0
                    if abs(fwd) < DEAD_FRAC:            # стоял на месте
                        continue
                    hit = (pdir > 0) == (fwd > 0)
                    cells.setdefault((sec, N, H), []).append(
                        (hit, pdir * fwd, ts[i]))
    return cells, meta


def agg(cells: dict, N: int, H: int, sec: str | None = None) -> dict | None:
    rows = []
    for (s, n_, h_), v in cells.items():
        if n_ == N and h_ == H and (sec is None or s == sec):
            rows.extend(v)
    n = len(rows)
    if n < MIN_CELL:
        return None
    hits = sum(1 for r in rows if r[0])
    mean_bps = float(np.mean([r[1] for r in rows])) * 1e4
    return {"n": n, "hits": hits, "hit_rate": hits / n, "sigma": sigma(hits, n),
            "mean_bps": mean_bps, "net_bps": mean_bps - COMMISSION_BPS,
            "med_bps": float(np.median([r[1] for r in rows])) * 1e4}


def non_overlap(cells: dict, N: int, H: int) -> dict | None:
    """Только непересекающиеся сделки (следующая после закрытия предыдущей)."""
    rows = []
    for (s, n_, h_), v in cells.items():
        if n_ != N or h_ != H:
            continue
        last = -10 ** 18
        for hit, ret, t in sorted(v, key=lambda x: x[2]):
            if t < last + H * 1000:
                continue
            last = t
            rows.append((hit, ret))
    n = len(rows)
    if n < MIN_CELL:
        return None
    hits = sum(1 for r in rows if r[0])
    mean_bps = float(np.mean([r[1] for r in rows])) * 1e4
    return {"n": n, "hit_rate": hits / n, "sigma": sigma(hits, n),
            "mean_bps": mean_bps, "net_bps": mean_bps - COMMISSION_BPS}


if __name__ == "__main__":
    cells, meta = collect(DB)
    for s, (n, dur) in meta.items():
        print(f"  {s}: тиков={n}, длительность={dur/60:.0f} мин, "
              f"плотность={n/dur:.2f} тик/с")
    print()
    hdr = f"{'окно':>6s}" + "".join(f"{H:>10d}с" for H in HORIZONS)
    for label, key in (("ПОПАДАНИЕ %", "hit_rate"), ("СИГМА", "sigma"),
                       ("СРЕДНИЙ ХОД, бп", "mean_bps"),
                       ("ЧИСТО ПОСЛЕ 5бп", "net_bps"), ("N", "n")):
        print(f"── {label} " + "─" * 40)
        print(hdr)
        for N in LOOKBACKS:
            line = f"{N:>6d}"
            for H in HORIZONS:
                a = agg(cells, N, H)
                if a is None:
                    line += f"{'—':>11s}"
                elif key == "hit_rate":
                    line += f"{a[key]*100:>11.1f}"
                elif key == "n":
                    line += f"{a[key]:>11d}"
                else:
                    line += f"{a[key]:>+11.2f}"
            print(line)
        print()

    best = None
    for N in LOOKBACKS:
        for H in HORIZONS:
            a = agg(cells, N, H)
            if a and (best is None or a["net_bps"] > best[2]["net_bps"]):
                best = (N, H, a)
    N, H, a = best
    print(f"ЛУЧШЕЕ ПО ЧИСТОМУ: окно {N} тиков × горизонт {H}с → "
          f"n={a['n']}, {a['hit_rate']*100:.1f}%, σ={a['sigma']:+.2f}, "
          f"{a['mean_bps']:+.2f} бп, чисто {a['net_bps']:+.2f} бп")
    no = non_overlap(cells, N, H)
    if no:
        print(f"  непересекающиеся: n={no['n']}, {no['hit_rate']*100:.1f}%, "
              f"σ={no['sigma']:+.2f}, {no['mean_bps']:+.2f} бп, "
              f"чисто {no['net_bps']:+.2f} бп")
    print("  по инструментам:")
    for s in sorted(meta):
        b = agg(cells, N, H, s)
        if b:
            print(f"    {s:6s} n={b['n']:5d} {b['hit_rate']*100:5.1f}% "
                  f"σ={b['sigma']:+6.2f} {b['mean_bps']:+7.2f} бп "
                  f"чисто {b['net_bps']:+7.2f} бп")

    # контроль: воспроизводим базу (окно 400 × 180с)
    base = agg(cells, 400, 180)
    if base:
        print(f"\nКОНТРОЛЬ базы (окно 400 × 180с): n={base['n']}, "
              f"{base['hit_rate']*100:.1f}%, σ={base['sigma']:+.2f}, "
              f"{base['mean_bps']:+.2f} бп, чисто {base['net_bps']:+.2f} бп")

    assert agg(cells, 400, 180) is not None, "база должна считаться"
    assert all(agg(cells, N, H) is None or agg(cells, N, H)["n"] >= MIN_CELL
               for N in LOOKBACKS for H in HORIZONS), "ячейки ниже MIN_CELL судим"
