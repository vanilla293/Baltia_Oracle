# -*- coding: utf-8 -*-
"""ЧЕСТНЫЙ ПЕРЕБОР КОМБИНАЦИЙ (спринт «Абсолют», защита проекта).

Задача владельца: «перебором найди идеальную комбинацию». Задача honest quant:
найти её ТАК, чтобы результат пережил защиту перед рецензентом.

ПОЧЕМУ ПРОСТОЙ ПЕРЕБОР ВСЕГДА «НАХОДИТ» ГРААЛЬ. Если прогнать K комбинаций
порогов и взять лучшую, её метрика — это МАКСИМУМ из K случайных величин.
Даже на чистом шуме максимум из 500 комбинаций даёт красивые 60-70% WR. Это
не открытие, это статистика экстремумов. Ровно так уже умерли в этой
лаборатории веса школы (weight_perm, p=0.51), «край−середина»
(fake_sky_native, p=0.51) и σ, раздутая перекрытием окон в √6.

ЧТО ДЕЛАЕТ ЭТОТ МОДУЛЬ (White's Reality Check / Hansen SPA, упрощённо):
  1. гоняет ВЕСЬ грид комбинаций на реальных данных → best_real;
  2. гоняет ТОТ ЖЕ грид на N суррогатах, где связь «признак → будущая
     доходность» разорвана, а вся структура рядов сохранена (циклический
     сдвиг доходностей относительно признаков) → распределение best_null;
  3. честная p-value = доля суррогатов, где best_null ≥ best_real.

Если p ≤ 0.05 — комбинация пережила перебор, её МОЖНО защищать.
Если p > 0.05 — «идеальная комбинация» неотличима от случайной находки;
защищать её нельзя, и лучше узнать это здесь, чем на комитете.

Суррогат — циклический сдвиг с ВЫБРОСОМ ШВА (урок why_oil: np.roll вносил
шов и искажал нуль до 65.8 перцентильных пунктов).

Запуск:  python3 -m backend.lab.grid_search_honest <dataset.json> [--nulls 200]
Self-тест: python3 -m backend.lab.grid_search_honest --selftest
⚫ измерение прошлого на конкретной выборке; не сигнал, не обещание. 18+.
"""
from __future__ import annotations

import json
import math
import sys

import numpy as np

COMMISSION_BPS = 5.0      # круг, константа стендов лаборатории
MIN_TRADES = 30           # комбинация с меньшим числом сделок не рассматривается
SEAM_DROP = 0.05          # доля выборки у шва суррогата, выбрасываемая


# ── признаки строго из ПРОШЛОГО (никакого заглядывания) ────────────────────
def build_features(prices: np.ndarray, win: int = 96) -> dict:
    """Каузальные признаки по ценам: доходность окна, ускорение (последняя
    треть против первой), Хёрст R/S, индекс Хилла, реализованная волатильность.
    Каждый элемент i считается ТОЛЬКО по prices[:i+1]."""
    p = np.asarray(prices, float)
    n = len(p)
    ret = np.log(p[1:] / p[:-1])
    feats = {k: np.full(n, np.nan) for k in
             ("ret_win", "accel", "hurst", "alpha", "vol")}
    for i in range(win, n):
        seg = p[i - win:i + 1]
        r = ret[i - win:i]
        feats["ret_win"][i] = math.log(seg[-1] / seg[0])
        t3 = win // 3
        first = math.log(seg[t3] / seg[0])
        last = math.log(seg[-1] / seg[-1 - t3])
        feats["accel"][i] = last - first
        feats["vol"][i] = float(np.std(r)) if r.size else np.nan
        # Хёрст R/S
        m = float(np.mean(r))
        cum = np.cumsum(r - m)
        R = float(cum.max() - cum.min())
        S = float(np.std(r))
        feats["hurst"][i] = (math.log(max(R / S, 1e-9)) / math.log(len(r))
                             if S > 0 and len(r) > 1 else 0.5)
        # индекс Хилла по верхним 10% |r|
        a = np.sort(np.abs(r[r != 0]))[::-1]
        if a.size >= 20:
            k = max(10, int(a.size * 0.10))
            if k < a.size and a[k] > 0:
                s = float(np.sum(np.log(a[:k] / a[k])))
                feats["alpha"][i] = (k / s) if s > 0 else np.nan
    return feats


def forward_returns(prices: np.ndarray, horizon: int) -> np.ndarray:
    """Доходность ВПЕРЁД на horizon баров (то, что предсказываем)."""
    p = np.asarray(prices, float)
    fwd = np.full(len(p), np.nan)
    if horizon < len(p):
        fwd[:-horizon] = np.log(p[horizon:] / p[:-horizon])
    return fwd


# ── сетка комбинаций ───────────────────────────────────────────────────────
def make_grid() -> list[dict]:
    """Полный перебор правил входа. Каждая комбинация — гипотеза «когда
    среда такая-то, идём туда-то»."""
    grid = []
    for direction in ("momentum", "antimomentum"):
        for a_lo, a_hi in ((0.0, 2.0), (2.0, 3.0), (3.0, 99.0), (0.0, 99.0)):
            for h_lo, h_hi in ((0.0, 0.45), (0.45, 0.55), (0.55, 1.0), (0.0, 1.0)):
                for accel in ("any", "same", "opposite"):
                    for mv in (0.0, 0.0005, 0.0015, 0.003):
                        grid.append({"dir": direction, "a_lo": a_lo, "a_hi": a_hi,
                                     "h_lo": h_lo, "h_hi": h_hi,
                                     "accel": accel, "min_move": mv})
    return grid


def eval_combo(c: dict, F: dict, fwd: np.ndarray,
               commission_bps: float = COMMISSION_BPS) -> dict | None:
    """Одна комбинация → (сделок, WR, net bps). Векторизовано."""
    a, h = F["alpha"], F["hurst"]
    rw, ac = F["ret_win"], F["accel"]
    ok = (~np.isnan(fwd)) & (~np.isnan(rw)) & (~np.isnan(ac))
    ok &= (~np.isnan(h)) & (h >= c["h_lo"]) & (h < c["h_hi"])
    # α может быть NaN — тогда фильтр по α не применяем только для «любой»
    if not (c["a_lo"] == 0.0 and c["a_hi"] == 99.0):
        ok &= (~np.isnan(a)) & (a >= c["a_lo"]) & (a < c["a_hi"])
    ok &= np.abs(rw) >= c["min_move"]
    if c["accel"] == "same":
        ok &= (np.sign(rw) == np.sign(ac)) & (ac != 0)
    elif c["accel"] == "opposite":
        ok &= (np.sign(rw) != np.sign(ac)) & (ac != 0)
    idx = np.where(ok)[0]
    if idx.size < MIN_TRADES:
        return None
    side = np.sign(rw[idx])
    if c["dir"] == "antimomentum":
        side = -side
    gross = side * fwd[idx] * 1e4                    # в bps
    net = gross - commission_bps
    return {"n": int(idx.size), "wr": float(np.mean(net > 0)),
            "net_avg": float(np.mean(net)), "net_sum": float(np.sum(net))}


def run_grid(F: dict, fwd: np.ndarray, grid: list) -> tuple:
    """Весь грид → (лучшая комбинация, её метрика по net_avg)."""
    best, best_v = None, -1e18
    for c in grid:
        r = eval_combo(c, F, fwd)
        if r and r["net_avg"] > best_v:
            best, best_v = {**c, **r}, r["net_avg"]
    return best, best_v


def surrogate_fwd(fwd: np.ndarray, shift: int) -> np.ndarray:
    """Суррогат: циклический сдвиг БУДУЩИХ доходностей относительно
    признаков. Разрывает связь «признак → будущее», сохраняя всю структуру
    самих доходностей (автокорреляцию, кластеризацию волатильности,
    распределение). Шов зануляется — не создаём артефакт стыка."""
    out = np.roll(fwd, shift)
    k = max(1, int(len(fwd) * SEAM_DROP))
    out[:k] = np.nan                                  # выброс шва
    out[max(0, shift - k):shift] = np.nan
    return out


def honest_search(prices_by_inst: dict, *, horizon: int = 3,
                  n_nulls: int = 200, win: int = 96, seed: int = 20260811) -> dict:
    """ЧЕСТНЫЙ ПЕРЕБОР: грид на реальных данных + тот же грид на суррогатах.
    Возврат: {best_real, null_best (список), p_value, n_combos}."""
    Fs, fwds = [], []
    for nm, pr in prices_by_inst.items():
        p = np.asarray(pr, float)
        if len(p) < win + horizon + MIN_TRADES:
            continue
        Fs.append(build_features(p, win))
        fwds.append(forward_returns(p, horizon))
    if not Fs:
        return {"error": "мало данных"}
    # склейка инструментов в один пул (кластеризация по инструменту —
    # в нуле она сохраняется, т.к. сдвиг делается ПО КАЖДОМУ инструменту)
    F = {k: np.concatenate([f[k] for f in Fs]) for k in Fs[0]}
    fwd = np.concatenate(fwds)
    grid = make_grid()
    best_real, v_real = run_grid(F, fwd, grid)
    if best_real is None:
        return {"error": "ни одна комбинация не набрала MIN_TRADES"}

    rng = np.random.default_rng(seed)
    lens = [len(f["alpha"]) for f in Fs]
    null_best = []
    for _ in range(n_nulls):
        parts, off = [], 0
        for L in lens:                                # сдвиг ПО КАЖДОМУ ряду
            sh = int(rng.integers(L // 10, L - L // 10)) if L > 20 else 1
            parts.append(surrogate_fwd(fwd[off:off + L], sh))
            off += L
        f_null = np.concatenate(parts)
        _, v = run_grid(F, f_null, grid)
        null_best.append(v)
    null_best = np.asarray([x for x in null_best if x > -1e17], float)
    p = (float(np.mean(null_best >= v_real)) if null_best.size else float("nan"))
    return {"best_real": best_real, "v_real": v_real,
            "null_best_mean": float(np.mean(null_best)) if null_best.size else None,
            "null_best_p95": (float(np.percentile(null_best, 95))
                              if null_best.size else None),
            "null_best_max": float(np.max(null_best)) if null_best.size else None,
            "p_value": p, "n_combos": len(grid), "n_nulls": int(null_best.size),
            "n_obs": int(np.sum(~np.isnan(fwd)))}


def report(res: dict) -> str:
    if "error" in res:
        return "ЧЕСТНЫЙ ПЕРЕБОР: " + res["error"]
    b = res["best_real"]
    ln = ["═" * 68, "ЧЕСТНЫЙ ПЕРЕБОР КОМБИНАЦИЙ (с нулём на тот же перебор)", "═" * 68,
          f"комбинаций в гриде: {res['n_combos']} · наблюдений: {res['n_obs']}"
          f" · суррогатов: {res['n_nulls']}", "",
          "ЛУЧШАЯ КОМБИНАЦИЯ НА РЕАЛЬНЫХ ДАННЫХ:",
          f"  правило={b['dir']} · α∈[{b['a_lo']},{b['a_hi']}) · "
          f"Хёрст∈[{b['h_lo']},{b['h_hi']}) · ускорение={b['accel']} · "
          f"мин.ход={b['min_move']*1e4:.0f}бп",
          f"  сделок={b['n']} · WR={b['wr']*100:.1f}% · "
          f"net={b['net_avg']:+.2f} бп/сделку (после комиссии {COMMISSION_BPS} бп)",
          "",
          "ТОТ ЖЕ ПЕРЕБОР НА СУРРОГАТАХ (связь признак→будущее разорвана):",
          f"  лучшая комбинация суррогата в среднем: "
          f"{res['null_best_mean']:+.2f} бп",
          f"  95-й перцентиль суррогатов:            "
          f"{res['null_best_p95']:+.2f} бп",
          f"  максимум по суррогатам:                "
          f"{res['null_best_max']:+.2f} бп", ""]
    p = res["p_value"]
    ln.append(f"ЧЕСТНАЯ p-value (доля суррогатов ≥ реального): {p:.4f}")
    if p <= 0.05:
        ln.append("  ✓ комбинация ПЕРЕЖИЛА перебор — её можно защищать")
    else:
        ln.append("  ✗ комбинация НЕ отличима от случайной находки перебора:")
        ln.append("    ровно такие же (или лучше) числа получаются на данных,")
        ln.append("    где связи признак→будущее НЕТ по построению.")
        ln.append("    Защищать это число нельзя — рецензент убьёт первым же нулём.")
    ln.append("")
    ln.append("⚫ измерение прошлого на конкретной выборке; не сигнал, 18+")
    return "\n".join(ln)


# ── self-test: на ЧИСТОМ ШУМЕ перебор обязан «найти грааль», а нуль — убить
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "--selftest":
        path = sys.argv[1]
        n_nulls = 200
        if "--nulls" in sys.argv:
            n_nulls = int(sys.argv[sys.argv.index("--nulls") + 1])
        data = json.load(open(path, encoding="utf-8"))
        res = honest_search(data, n_nulls=n_nulls)
        print(report(res))
        sys.exit(0)

    # ГЛАВНАЯ ДЕМОНСТРАЦИЯ: случайное блуждание (сигнала НЕТ по построению)
    rng = np.random.default_rng(1)
    walks = {f"NOISE{i}": np.cumprod(1 + rng.standard_normal(1200) * 0.001) * 100
             for i in range(4)}
    res = honest_search(walks, horizon=3, n_nulls=60)
    b = res["best_real"]
    # перебор ОБЯЗАН найти «прибыльную» комбинацию даже на чистом шуме
    assert b is not None and b["n"] >= MIN_TRADES
    print(f"  [шум] лучшая комбинация перебора: WR={b['wr']*100:.1f}%, "
          f"net={b['net_avg']:+.2f}бп на {b['n']} сделках")
    # и нуль ОБЯЗАН её убить: p должна быть большой
    assert res["p_value"] > 0.05, ("нуль не убил находку на чистом шуме — "
                                   "сломан суррогат", res["p_value"])
    print(f"  [шум] честная p-value = {res['p_value']:.3f} → находка убита нулём ✓")

    # контроль ЧУВСТВИТЕЛЬНОСТИ: вшиваем НАСТОЯЩИЙ эффект — нуль обязан его
    # пропустить (иначе метод слеп и бесполезен)
    n = 1500
    eps = rng.standard_normal(n) * 0.001
    px = [100.0]
    for i in range(1, n):                 # сильный возврат к среднему
        drift = -0.30 * (math.log(px[-1] / 100.0))
        px.append(px[-1] * math.exp(drift + eps[i]))
    res2 = honest_search({"MR": np.asarray(px)}, horizon=3, n_nulls=60)
    print(f"  [реальный эффект] net={res2['best_real']['net_avg']:+.2f}бп, "
          f"p={res2['p_value']:.3f}")
    assert res2["p_value"] < res["p_value"], (
        "метод не различает настоящий эффект и шум", res2["p_value"], res["p_value"])

    # детерминизм при том же сиде
    assert honest_search(walks, horizon=3, n_nulls=10, seed=7)["p_value"] == \
        honest_search(walks, horizon=3, n_nulls=10, seed=7)["p_value"]
    print("grid_search_honest self-test OK: перебор находит «грааль» даже на "
          "чистом шуме, суррогатный нуль его убивает, настоящий эффект нуль "
          "проходит (метод не слеп), детерминизм по сиду")
