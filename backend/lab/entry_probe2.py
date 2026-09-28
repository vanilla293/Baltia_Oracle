# -*- coding: utf-8 -*-
"""ЛАБ · вход №2: те же механики на ЛУЧШЕЙ известной ячейке («очень сильный ход», 600с)
плюс разложение эффекта лимитки на «скидка» и «отбор» и робастность без dead-фильтра."""
from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, "/home/user/Real_Sky-/pythia")
sys.path.insert(0, "/home/user/Real_Sky-/pythia/backend/lab")
from backend.lab.entry_probe import (  # noqa: E402
    METHODS, evaluate, entry_now, load_cached, fmt, COMMISSION_BPS)

DB = "/home/user/Real_Sky-/pythia/data/moex_real.db"


def main():
    obs, series = load_cached(DB)

    # ── 1. ищем ячейку «очень сильный ход» (порог по |ret_win|), 600с ──────
    print("=" * 96)
    print("ПОИСК ЯЧЕЙКИ «ОЧЕНЬ СИЛЬНЫЙ ХОД» на 600с (вход сразу, антимоментум)")
    print("=" * 96)
    aw = np.array([abs(r["ret_win"]) for r in obs])
    best = None
    for q in (50, 60, 70, 75, 80, 85, 90, 92, 95):
        thr = float(np.percentile(aw, q))
        sub = [r for r in obs if abs(r["ret_win"]) >= thr]
        m = evaluate(sub, series, 600, entry_now)
        if not m:
            continue
        print(f"  |ret_win| >= q{q} ({thr*1e4:6.1f}бп): N={m['n']:4d} "
              f"попад={m['hit_rate']*100:5.1f}% σ={m['sigma']:+5.2f} "
              f"ход={m['mean_bps']:+6.2f}бп нетто={m['net_bps']:+6.2f}бп")
        if m["n"] >= 30 and (best is None or m["net_bps"] > best[1]["net_bps"]):
            best = (thr, m, sub, q)
    if best is None:
        print("  ячейка не найдена")
        return
    thr, bm, sub, q = best
    print(f"\n  → взята ячейка q{q}: |ret_win| >= {thr*1e4:.1f}бп, N={bm['n']}")

    # ── 2. все механики входа на этой ячейке ──────────────────────────────
    for H in (600, 180):
        print()
        print("=" * 96)
        print(f"МЕХАНИКИ ВХОДА на ячейке «сильный ход» · горизонт {H}с · выход t0+H")
        print("=" * 96)
        for name, fn, kw in METHODS:
            m = evaluate(sub, series, H, fn, kw)
            print(fmt(name, m))
            if m and m["fill_rate"] < 0.999:
                b2 = evaluate(sub, series, H, entry_now, only=set(m["keys"]))
                print(fmt("      ↳ база на той же подвыборке", b2))
                if b2:
                    disc = m["mean_bps"] - b2["mean_bps"]
                    sel = b2["mean_bps"] - bm["mean_bps"] if H == 600 else None
                    print(f"      ↳ скидка входа = {disc:+.2f}бп"
                          + (f", отбор подвыборки = {sel:+.2f}бп" if sel is not None else ""))

    # ── 3. робастность: без фильтра «стоял на месте» ───────────────────────
    print()
    print("=" * 96)
    print("РОБАСТНОСТЬ: без dead-фильтра (все наблюдения), весь набор")
    print("=" * 96)
    for H in (180, 600):
        print(f"  --- горизонт {H}с ---")
        for name, fn, kw in METHODS:
            m = evaluate(obs, series, H, fn, kw, dead_filter=False)
            print(fmt(name, m))

    # ── 4. разложение эффекта лимитки на весь набор ───────────────────────
    print()
    print("=" * 96)
    print("РАЗЛОЖЕНИЕ ЛИМИТКИ: скидка входа vs отбор подвыборки (весь набор)")
    print("=" * 96)
    from backend.lab.entry_probe import entry_limit
    for H in (180, 600):
        base_all = evaluate(obs, series, H, entry_now)
        for pull in (2.0, 5.0, 10.0):
            m = evaluate(obs, series, H, entry_limit, {"pull_bps": pull})
            if not m:
                continue
            b2 = evaluate(obs, series, H, entry_now, only=set(m["keys"]))
            disc = m["mean_bps"] - b2["mean_bps"]
            sel = b2["mean_bps"] - base_all["mean_bps"]
            print(f"  H={H:3d}с откат {pull:4.1f}бп: итог={m['mean_bps']:+6.2f}бп = "
                  f"база{base_all['mean_bps']:+6.2f} + отбор{sel:+6.2f} + скидка{disc:+6.2f}"
                  f"  | нетто={m['net_bps']:+6.2f}бп, входов {m['fill_rate']*100:.1f}%")

    # ── 5. по инструментам: лучший реальный вариант против базы, 180с ─────
    print()
    print("=" * 96)
    print("ПО ИНСТРУМЕНТАМ (180с): база vs задержка 15с vs лимит 2бп")
    print("=" * 96)
    from backend.lab.entry_probe import entry_delay
    for sec in sorted({r["sec"] for r in obs}):
        s = [r for r in obs if r["sec"] == sec]
        a = evaluate(s, series, 180, entry_now)
        b = evaluate(s, series, 180, entry_delay, {"sec_delay": 15})
        c = evaluate(s, series, 180, entry_limit, {"pull_bps": 2.0})
        def z(m):
            return ("—" if not m else
                    f"N={m['n']:4d} {m['hit_rate']*100:5.1f}% нетто={m['net_bps']:+6.2f}бп")
        print(f"  {sec:6s} база: {z(a):34s} задержка15: {z(b):34s} лимит2: {z(c)}")


if __name__ == "__main__":
    main()
