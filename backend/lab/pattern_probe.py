# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ · ЛАБ — ПРОВЕРКА НАЙДЕННЫХ ПАТТЕРНОВ (строгий разбор).

Первый прогон валидатора (forward_test) нашёл на реальных данных MOEX три
кандидата в паттерн. Этот модуль их ПЫТАЕТСЯ УБИТЬ — как положено:

  К1. АНТИМОМЕНТУМ: то, что выросло за окно, в ближайшие минуты падает
      (контр-моментум 55.0% при σ=+3.17 на 180с; моментум зеркально плох).
  К2. ХОУКС ЛОМАЕТ ПРЕДСКАЗАНИЯ: при n≥0.7 (самоподпитка потока) машина
      угадывает хуже монеты.
  К3. РАЗБРОС ПО ИНСТРУМЕНТАМ: SBER 60.2% (σ+2.68), GAZP 34.6% (σ−4.11)
      — второй похож на инвертированный сигнал.

Проверки против самообмана:
  · РАСКОЛ ВЫБОРКИ: первая половина сессии против второй. Паттерн, живущий
    только в одной половине, — шум;
  · ПО ИНСТРУМЕНТАМ: держится ли знак на всех, или тянут один-два;
  · ИЗДЕРЖКИ: сравнение среднего хода с комиссией круга (COMMISSION_BPS);
    угадывать направление и терять деньги — обычное дело;
  · ГРАДАЦИЯ СИЛЫ: растёт ли эффект с величиной прошлого хода (у настоящей
    возвратности должна быть монотонность);
  · МНОЖЕСТВЕННОСТЬ: поправка Бонферрони на число проверенных гипотез.

⚫ Результат — измерение одной сессии. Отрицательный вывод печатается так
же честно, как положительный. НЕ сигнал. 18+.

Запуск: python3 -m backend.lab.pattern_probe --db data/moex_real.db
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timezone

import numpy as np

try:
    from . import forward_test as ft
except ImportError:                                        # запуск как скрипт
    sys.path.insert(0, __file__.rsplit("/backend/", 1)[0])
    from backend.lab import forward_test as ft

COMMISSION_BPS = 5.0     # круг (вход+выход) у владельца: 0.025%×2 = 5 бп
MIN_CELL = 30
N_HYPOTHESES = 12        # сколько гипотез проверено (для Бонферрони)


def sigma(hits: int, n: int) -> float:
    if n <= 0:
        return 0.0
    return (hits - n * 0.5) / (math.sqrt(n * 0.25) or 1e-9)


def eval_rule(obs: list, H: int, pick, label: str) -> dict | None:
    """Оценка правила: угадывание, средний ход, ход ПОСЛЕ комиссии."""
    hits = miss = 0
    rets = []
    for r in obs:
        fwd = r.get(f"fwd{H}")
        if fwd is None or abs(fwd) < ft.DEAD_FRAC:
            continue
        d = pick(r)
        if not d:
            continue
        (hits, miss) = (hits + 1, miss) if (d > 0) == (fwd > 0) else (hits, miss + 1)
        rets.append(d * fwd)
    n = hits + miss
    if n < MIN_CELL:
        return None
    mean_bps = float(np.mean(rets)) * 1e4
    return {"label": label, "n": n, "hit": hits / n, "sigma": sigma(hits, n),
            "mean_bps": mean_bps, "net_bps": mean_bps - COMMISSION_BPS,
            "sum_bps": float(np.sum(rets)) * 1e4}


def split_halves(obs: list) -> tuple:
    """Раскол по времени: первая половина сессии / вторая."""
    s = sorted(obs, key=lambda r: r["t"])
    mid = len(s) // 2
    return s[:mid], s[mid:]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/moex_real.db")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    secs = ft.instruments(a.db)
    obs = []
    by_sec = {}
    for sec in secs:
        d = ft.load_series(a.db, sec)
        o = ft.run_instrument(sec, d)
        obs.extend(o)
        by_sec[sec] = o
    if not obs:
        print("нет наблюдений")
        return

    L = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    L.append("ПРОВЕРКА НАЙДЕННЫХ ПАТТЕРНОВ — РЕАЛЬНЫЕ ДАННЫЕ MOEX")
    L.append(f"наблюдений: {len(obs)} · сформировано {now}")
    L.append(f"комиссия круга принята {COMMISSION_BPS} бп (0.025%×2)")
    L.append("⚫ измерение одной сессии, НЕ сигнал, НЕ обещание прибыли. 18+")
    L.append("")

    anti = lambda r: (-1 if r["ret_win"] > 0 else 1)      # noqa: E731
    mom = lambda r: (1 if r["ret_win"] > 0 else -1)       # noqa: E731

    # ── К1: АНТИМОМЕНТУМ ──
    L.append("=" * 74)
    L.append("К1. АНТИМОМЕНТУМ (то, что выросло — падает)")
    L.append("=" * 74)
    for H in ft.HORIZONS_S:
        m = eval_rule(obs, H, anti, f"антимоментум {H}с")
        mm = eval_rule(obs, H, mom, f"моментум {H}с")
        if m:
            L.append(f"  {H:4d}с: угадано {m['hit']*100:.1f}% (σ={m['sigma']:+.2f}), "
                     f"ход {m['mean_bps']:+.2f}бп, после комиссии "
                     f"{m['net_bps']:+.2f}бп, N={m['n']}")
    L.append("")
    L.append("  РАСКОЛ ВЫБОРКИ (первая половина сессии / вторая):")
    h1, h2 = split_halves(obs)
    for nm, part in (("1-я половина", h1), ("2-я половина", part2 := h2)):
        m = eval_rule(part, 180, anti, nm)
        if m:
            L.append(f"    {nm}: {m['hit']*100:.1f}% (σ={m['sigma']:+.2f}), "
                     f"ход {m['mean_bps']:+.2f}бп, N={m['n']}")
    L.append("")
    L.append("  ПО ИНСТРУМЕНТАМ (180с):")
    signs = []
    for sec, o in sorted(by_sec.items(), key=lambda x: -len(x[1])):
        m = eval_rule(o, 180, anti, sec)
        if m:
            signs.append(m["hit"] > 0.5)
            L.append(f"    {sec:6s} {m['hit']*100:5.1f}% (σ={m['sigma']:+5.2f}) "
                     f"ход {m['mean_bps']:+6.2f}бп  N={m['n']}")
    if signs:
        L.append(f"    → знак держится на {sum(signs)} из {len(signs)} инструментов")
    L.append("")
    L.append("  ГРАДАЦИЯ ПО СИЛЕ ПРОШЛОГО ХОДА (растёт ли возврат с амплитудой):")
    qs = np.quantile([abs(r["ret_win"]) for r in obs], [0.25, 0.5, 0.75])
    bands = [("слабый ход", lambda r: abs(r["ret_win"]) <= qs[0]),
             ("средний", lambda r: qs[0] < abs(r["ret_win"]) <= qs[1]),
             ("сильный", lambda r: qs[1] < abs(r["ret_win"]) <= qs[2]),
             ("очень сильный", lambda r: abs(r["ret_win"]) > qs[2])]
    for bn, flt in bands:
        sub = [r for r in obs if flt(r)]
        m = eval_rule(sub, 180, anti, bn)
        if m:
            L.append(f"    {bn:15s} {m['hit']*100:5.1f}% (σ={m['sigma']:+5.2f}) "
                     f"ход {m['mean_bps']:+6.2f}бп → после комиссии "
                     f"{m['net_bps']:+6.2f}бп  N={m['n']}")

    # ── К2: ХОУКС ЛОМАЕТ ПРЕДСКАЗАНИЯ ──
    L.append("")
    L.append("=" * 74)
    L.append("К2. ВЫСОКИЙ ХОУКС ЛОМАЕТ ПРЕДСКАЗУЕМОСТЬ")
    L.append("=" * 74)
    for lab, flt in (("Хоукс n≥0.7", lambda r: (r.get("hawkes") or 0) >= 0.7),
                     ("Хоукс n<0.7", lambda r: 0 < (r.get("hawkes") or 0) < 0.7)):
        sub = [r for r in obs if flt(r)]
        for nm, pick in (("машина", lambda r: r["dir"]),
                         ("антимоментум", anti)):
            m = eval_rule(sub, 180, pick, f"{lab}/{nm}")
            if m:
                L.append(f"  {lab:14s} {nm:14s} {m['hit']*100:5.1f}% "
                         f"(σ={m['sigma']:+5.2f}) ход {m['mean_bps']:+6.2f}бп N={m['n']}")

    # ── К3: РАЗБРОС ПО ИНСТРУМЕНТАМ (машина) ──
    L.append("")
    L.append("=" * 74)
    L.append("К3. РАЗБРОС ПО ИНСТРУМЕНТАМ (машина) + проверка на устойчивость")
    L.append("=" * 74)
    for sec, o in sorted(by_sec.items(), key=lambda x: -len(x[1])):
        m = eval_rule(o, 180, lambda r: r["dir"], sec)
        if not m:
            continue
        a1, a2 = split_halves(o)
        m1 = eval_rule(a1, 180, lambda r: r["dir"], "1п")
        m2 = eval_rule(a2, 180, lambda r: r["dir"], "2п")
        st = (f"1п={m1['hit']*100:.0f}% 2п={m2['hit']*100:.0f}%"
              if m1 and m2 else "раскол не судим (мало данных)")
        L.append(f"  {sec:6s} всего {m['hit']*100:5.1f}% (σ={m['sigma']:+5.2f})  {st}")

    # ── ИТОГ С ПОПРАВКОЙ НА МНОЖЕСТВЕННОСТЬ ──
    L.append("")
    L.append("=" * 74)
    L.append("ИТОГ")
    L.append("=" * 74)
    m180 = eval_rule(obs, 180, anti, "антимоментум")
    need_sigma = 2.58 + 0.5 * math.log(N_HYPOTHESES)   # грубая поправка Бонферрони
    L.append(f"  порог значимости с поправкой на {N_HYPOTHESES} гипотез: "
             f"|σ| ≳ {need_sigma:.2f}")
    if m180:
        verdict = ("ПРОШЁЛ" if abs(m180["sigma"]) >= need_sigma
                   else "НЕ прошёл порог (нужна ещё сессия)")
        L.append(f"  антимоментум 180с: σ={m180['sigma']:+.2f} → {verdict}")
        L.append(f"  экономика: ход {m180['mean_bps']:+.2f}бп против комиссии "
                 f"{COMMISSION_BPS}бп → чистыми {m180['net_bps']:+.2f}бп")
        if m180["net_bps"] < 0:
            L.append("  ⚠ ГЛАВНОЕ: направление угадывается, но амплитуда МЕНЬШЕ")
            L.append("    издержек — торговать это в лоб УБЫТОЧНО. Нужен фильтр")
            L.append("    на крупные ходы либо горизонт больше.")
    txt = "\n".join(L)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(txt + "\n")
    print(txt)


if __name__ == "__main__":
    main()
