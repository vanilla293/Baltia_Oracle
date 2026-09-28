# -*- coding: utf-8 -*-
"""ПОИСК КОМБИНАЦИИ v2 — «по уму» (спринт «Абсолют», задание владельца).

Отличия от grid_search_honest (v1) — ровно те, из-за которых проценты
владельца расходились с прошлым отчётом:

  1. ДВЕ СИСТЕМЫ СЧЁТА ПОБЕД, обе печатаются рядом:
       hit%  — попадание ПО ЗНАКУ хода (качество предсказания; так считает
               ВАЛИДАЦИЯ_ЧЕСТНАЯ и, судя по всему, владелец);
       WRnet — доля сделок с плюсом ПОСЛЕ комиссии 5 бп (экономика; так
               считал v1 — отсюда «27%» против «50%»: обе цифры верны,
               они отвечают на РАЗНЫЕ вопросы).
  2. ПРИЗНАКИ МИРОВОЗЗРЕНИЯ, которых не было в v1 (там только цена):
       casimir_a — Казимир-пружина на свечах: сжатие диапазона баров
                   против медианы (спред недоступен в свечах — прокси 🟡);
       vflow     — CVD-прокси из объёмов: Σ sign(close−open)·vol / Σ vol
                   (поток агрессора без ленты — прокси 🟡);
       vsurge    — всплеск объёма (лавина Хоукса на свечах — прокси 🟡);
       tod       — время суток МСК (календарная сезонность — ЕДИНСТВЕННОЕ,
                   что в лаборатории школы стабильно переживало нули).
  3. TRAIN/HOLDOUT: комбинация ВЫБИРАЕТСЯ на первых 70% времени (по
     t-статистике, не по среднему — устойчивее), а ОЦЕНИВАЕТСЯ на
     последних 30%, которых выбор не видел.
  4. НУЛЬ НА ВСЮ ПРОЦЕДУРУ: на каждом суррогате заново выполняется ВЕСЬ
     конвейер (выбор на train → оценка на holdout). p-value — доля
     суррогатов, чей holdout-результат ≥ реального. Это White's Reality
     Check на процедуру целиком, а не на одну комбинацию.

⚫ измерение прошлого; не сигнал, не обещание прибыли. 18+.
Запуск: python3 -m backend.lab.grid_search_v2 /tmp/dataset_ohlcv.json
        [--horizon N] [--nulls 100]
Self-тест: python3 -m backend.lab.grid_search_v2 --selftest
"""
from __future__ import annotations

import json
import math
import sys

import numpy as np

COMMISSION_BPS = 5.0
MIN_TRAIN = 50            # меньше сделок на train — комбинация не кандидат
MIN_HOLD = 25             # меньше на holdout — «данных нет»
SEAM_DROP = 0.05
MSK_OFFSET = 3 * 3600     # ts в данных — UTC; торговое время смотрим в МСК


# ── признаки: строго каузальные, всё мировоззрение в прокси-виде ───────────
def build_features(recs: list[dict], win: int = 96) -> dict:
    """recs: [{ts,o,c,h,l,v}...]. Все признаки бара i — только из баров ≤ i."""
    n = len(recs)
    c = np.asarray([r["c"] for r in recs], float)
    o = np.asarray([r["o"] for r in recs], float)
    h = np.asarray([r["h"] for r in recs], float)
    lo = np.asarray([r["l"] for r in recs], float)
    v = np.asarray([r["v"] for r in recs], float)
    ts = np.asarray([r["ts"] for r in recs], float)
    ret = np.concatenate([[np.nan], np.log(c[1:] / c[:-1])])
    rng = np.maximum(h - lo, 1e-12) / np.maximum(c, 1e-12)   # диапазон бара
    bar_sign = np.sign(c - o)

    F = {k: np.full(n, np.nan) for k in
         ("ret_win", "accel", "hurst", "alpha", "casimir_a", "vflow",
          "vsurge", "tod")}
    hour_msk = ((ts + MSK_OFFSET) % 86400) / 3600.0
    # время суток: 0 утро [9.5;12), 1 день [12;16), 2 вечер [16;19),
    # 3 вечёрка [19;24) — сетка МСК
    F["tod"] = np.select([hour_msk < 12.0, hour_msk < 16.0, hour_msk < 19.0],
                         [0.0, 1.0, 2.0], default=3.0)
    for i in range(win, n):
        r = ret[i - win + 1:i + 1]
        r = r[~np.isnan(r)]
        if r.size < win // 2:
            continue
        F["ret_win"][i] = math.log(c[i] / c[i - win])
        t3 = win // 3
        F["accel"][i] = (math.log(c[i] / c[i - t3])
                         - math.log(c[i - win + t3] / c[i - win]))
        m = float(np.mean(r))
        cum = np.cumsum(r - m)
        R = float(cum.max() - cum.min())
        S = float(np.std(r))
        F["hurst"][i] = (math.log(max(R / S, 1e-9)) / math.log(len(r))
                         if S > 0 else 0.5)
        a = np.sort(np.abs(r[r != 0]))[::-1]
        if a.size >= 20:
            k = max(10, int(a.size * 0.10))
            if k < a.size and a[k] > 0:
                s = float(np.sum(np.log(a[:k] / a[k])))
                if s > 0:
                    F["alpha"][i] = k / s
        # Казимир-пружина 🟡: средний диапазон 3 последних баров / медиана окна
        med_rng = float(np.median(rng[i - win + 1:i + 1]))
        if med_rng > 0:
            F["casimir_a"][i] = float(np.mean(rng[i - 2:i + 1])) / med_rng
        # поток 🟡: CVD-прокси за 12 баров (2 часа)
        w12 = slice(max(0, i - 11), i + 1)
        vt = float(np.sum(v[w12]))
        if vt > 0:
            F["vflow"][i] = float(np.sum(bar_sign[w12] * v[w12])) / vt
        # всплеск объёма 🟡: 3 последних бара против медианы троек
        med_v = float(np.median(v[i - win + 1:i + 1])) * 3.0
        if med_v > 0:
            F["vsurge"][i] = float(np.sum(v[i - 2:i + 1])) / med_v
    return F


def forward_returns(recs: list[dict], horizon: int) -> np.ndarray:
    c = np.asarray([r["c"] for r in recs], float)
    fwd = np.full(len(c), np.nan)
    if horizon < len(c):
        fwd[:-horizon] = np.log(c[horizon:] / c[:-horizon])
    return fwd


# ── сетка комбинаций (мировоззрение в каждой оси) ──────────────────────────
def make_grid() -> list[dict]:
    grid = []
    for direction in ("momentum", "antimomentum"):
        for a_band in ((0.0, 2.0), (2.0, 3.0), (3.0, 99.0), (0.0, 99.0)):
            for h_band in ((0.0, 0.45), (0.55, 1.0), (0.0, 1.0)):
                for accel in ("any", "same"):
                    for cas in ("any", "сжат"):        # Казимир a < 0.6
                        for vf in ("any", "along", "against"):
                            for tod in ("any", "утро", "вечер"):
                                for mv in (0.0015, 0.003):
                                    grid.append({
                                        "dir": direction, "a": a_band,
                                        "h": h_band, "accel": accel,
                                        "cas": cas, "vf": vf, "tod": tod,
                                        "mv": mv})
    return grid


def combo_mask(c: dict, F: dict) -> np.ndarray:
    rw, ac = F["ret_win"], F["accel"]
    ok = (~np.isnan(rw)) & (~np.isnan(ac))
    ok &= np.abs(rw) >= c["mv"]
    if c["a"] != (0.0, 99.0):
        ok &= (~np.isnan(F["alpha"])) & (F["alpha"] >= c["a"][0]) \
            & (F["alpha"] < c["a"][1])
    if c["h"] != (0.0, 1.0):
        ok &= (~np.isnan(F["hurst"])) & (F["hurst"] >= c["h"][0]) \
            & (F["hurst"] < c["h"][1])
    if c["accel"] == "same":
        ok &= (np.sign(rw) == np.sign(ac)) & (ac != 0)
    if c["cas"] == "сжат":
        ok &= (~np.isnan(F["casimir_a"])) & (F["casimir_a"] < 0.6)
    if c["vf"] == "along":
        ok &= (~np.isnan(F["vflow"])) & (np.sign(F["vflow"]) == np.sign(rw)) \
            & (np.abs(F["vflow"]) > 0.15)
    elif c["vf"] == "against":
        ok &= (~np.isnan(F["vflow"])) & (np.sign(F["vflow"]) != np.sign(rw)) \
            & (np.abs(F["vflow"]) > 0.15)
    if c["tod"] == "утро":
        ok &= F["tod"] == 0.0
    elif c["tod"] == "вечер":
        ok &= F["tod"] >= 2.0
    return ok


def eval_combo(c: dict, F: dict, fwd: np.ndarray, idx_mask: np.ndarray,
               commission_bps: float = COMMISSION_BPS,
               min_n: int = MIN_TRAIN) -> dict | None:
    ok = combo_mask(c, F) & idx_mask & (~np.isnan(fwd))
    idx = np.where(ok)[0]
    if idx.size < min_n:
        return None
    side = np.sign(F["ret_win"][idx])
    if c["dir"] == "antimomentum":
        side = -side
    gross = side * fwd[idx] * 1e4
    net = gross - commission_bps
    sd = float(np.std(net))
    t = float(np.mean(net) / (sd / math.sqrt(len(net)))) if sd > 0 else 0.0
    return {"n": int(idx.size),
            "hit": float(np.mean(gross > 0)),          # ПО ЗНАКУ (как владелец)
            "wr_net": float(np.mean(net > 0)),         # ПОСЛЕ комиссии (v1)
            "gross_avg": float(np.mean(gross)),
            "net_avg": float(np.mean(net)), "t": t}


# ── процедура: выбор на train → оценка на holdout ──────────────────────────
def procedure(F: dict, fwd: np.ndarray, train_mask: np.ndarray,
              hold_mask: np.ndarray, grid: list) -> dict | None:
    """Выбор лучшей по t-статистике на train; отчёт — по holdout."""
    best, best_t = None, -1e18
    for c in grid:
        r = eval_combo(c, F, fwd, train_mask, min_n=MIN_TRAIN)
        if r and r["t"] > best_t:
            best, best_t = c, r["t"]
    if best is None:
        return None
    tr = eval_combo(best, F, fwd, train_mask, min_n=MIN_TRAIN)
    ho = eval_combo(best, F, fwd, hold_mask, min_n=MIN_HOLD)
    return {"combo": best, "train": tr, "hold": ho}


def surrogate_fwd_parts(fwds: list, lens: list, rng) -> np.ndarray:
    parts = []
    for f, L in zip(fwds, lens):
        sh = int(rng.integers(L // 10, L - L // 10)) if L > 20 else 1
        out = np.roll(f, sh)
        k = max(1, int(L * SEAM_DROP))
        out[:k] = np.nan
        out[max(0, sh - k):sh] = np.nan
        parts.append(out)
    return np.concatenate(parts)


def honest_search_v2(data: dict, *, horizon: int = 6, n_nulls: int = 100,
                     win: int = 96, train_frac: float = 0.7,
                     seed: int = 20260811) -> dict:
    Fs, fwds, lens, tmasks, hmasks = [], [], [], [], []
    for nm, recs in data.items():
        if len(recs) < win * 3:
            continue
        F = build_features(recs, win)
        fwd = forward_returns(recs, horizon)
        L = len(recs)
        cut = int(L * train_frac)
        tm = np.zeros(L, bool)
        tm[:cut] = True
        hm = np.zeros(L, bool)
        hm[cut:] = True
        Fs.append(F)
        fwds.append(fwd)
        lens.append(L)
        tmasks.append(tm)
        hmasks.append(hm)
    if not Fs:
        return {"error": "мало данных"}
    F = {k: np.concatenate([f[k] for f in Fs]) for k in Fs[0]}
    fwd = np.concatenate(fwds)
    train = np.concatenate(tmasks)
    hold = np.concatenate(hmasks)
    grid = make_grid()

    real = procedure(F, fwd, train, hold, grid)
    if real is None or real["hold"] is None:
        return {"error": "процедура не дала кандидата с достаточным n"}
    v_real = real["hold"]["net_avg"]

    rng = np.random.default_rng(seed)
    null_hold = []
    for _ in range(n_nulls):
        f_null = surrogate_fwd_parts(fwds, lens, rng)
        pr = procedure(F, f_null, train, hold, grid)
        if pr and pr["hold"]:
            null_hold.append(pr["hold"]["net_avg"])
    null_hold = np.asarray(null_hold, float)
    p = (float(np.mean(null_hold >= v_real)) if null_hold.size
         else float("nan"))
    return {"real": real, "p_value": p, "n_combos": len(grid),
            "null_mean": float(np.mean(null_hold)) if null_hold.size else None,
            "null_p95": (float(np.percentile(null_hold, 95))
                         if null_hold.size else None),
            "n_nulls_ok": int(null_hold.size),
            "n_obs": int(np.sum(~np.isnan(fwd)))}


def _fmt_combo(c: dict) -> str:
    return (f"{c['dir']} · α∈[{c['a'][0]},{c['a'][1]}) · "
            f"H∈[{c['h'][0]},{c['h'][1]}) · accel={c['accel']} · "
            f"Казимир={c['cas']} · поток={c['vf']} · время={c['tod']} · "
            f"ход≥{c['mv']*1e4:.0f}бп")


def report_v2(res: dict, tag: str = "") -> str:
    if "error" in res:
        return "ПОИСК v2: " + res["error"]
    r = res["real"]
    tr, ho = r["train"], r["hold"]
    ln = ["═" * 70, f"ПОИСК КОМБИНАЦИИ v2 {tag} — выбор на train, отчёт на holdout",
          "═" * 70,
          f"комбинаций: {res['n_combos']} · наблюдений: {res['n_obs']} · "
          f"суррогатов: {res['n_nulls_ok']}", "",
          "ВЫБРАННАЯ КОМБИНАЦИЯ (по t-статистике на train):",
          "  " + _fmt_combo(r["combo"]), "",
          "  ── ДВЕ СИСТЕМЫ СЧЁТА ПОБЕД (обе честные, вопросы разные) ──",
          f"  TRAIN  : n={tr['n']:5d} · hit(по знаку)={tr['hit']*100:5.1f}% · "
          f"WRnet={tr['wr_net']*100:5.1f}% · gross={tr['gross_avg']:+.2f} · "
          f"net={tr['net_avg']:+.2f} бп",
          f"  HOLDOUT: n={ho['n']:5d} · hit(по знаку)={ho['hit']*100:5.1f}% · "
          f"WRnet={ho['wr_net']*100:5.1f}% · gross={ho['gross_avg']:+.2f} · "
          f"net={ho['net_avg']:+.2f} бп", "",
          f"НУЛЬ НА ВСЮ ПРОЦЕДУРУ (выбор+оценка на суррогатах):",
          f"  средний holdout суррогатов: {res['null_mean']:+.2f} бп · "
          f"p95: {res['null_p95']:+.2f} бп",
          f"  честная p-value = {res['p_value']:.4f}"]
    if res["p_value"] <= 0.05 and ho["net_avg"] > 0:
        ln.append("  ✓ ПЕРЕЖИЛА и перебор, и holdout — кандидат на защиту")
    else:
        ln.append("  ✗ не пережила: результат объясним самим перебором")
    ln.append("⚫ измерение прошлого; не сигнал, 18+")
    return "\n".join(ln)


# ── self-test ───────────────────────────────────────────────────────────────
def _mk_recs(prices, vols=None, t0=1750000000, dt=600):
    vols = vols if vols is not None else [1000.0] * len(prices)
    return [{"ts": t0 + i * dt, "o": p, "c": p, "h": p * 1.001,
             "l": p * 0.999, "v": vols[i]} for i, p in enumerate(prices)]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "--selftest":
        data = json.load(open(sys.argv[1], encoding="utf-8"))
        n_nulls = 100
        if "--nulls" in sys.argv:
            n_nulls = int(sys.argv[sys.argv.index("--nulls") + 1])
        horizon = 6
        if "--horizon" in sys.argv:
            horizon = int(sys.argv[sys.argv.index("--horizon") + 1])
        res = honest_search_v2(data, horizon=horizon, n_nulls=n_nulls)
        print(report_v2(res, f"(горизонт {horizon} баров)"))
        sys.exit(0)

    rng = np.random.default_rng(3)
    # 1) чистый шум: процедура может выбрать комбинацию, но p обязана быть
    #    большой (holdout+нуль на процедуру не дают себя обмануть)
    walks = {f"N{i}": _mk_recs(list(np.cumprod(
        1 + rng.standard_normal(1400) * 0.001) * 100)) for i in range(3)}
    r0 = honest_search_v2(walks, horizon=3, n_nulls=40)
    assert "error" in r0 or r0["p_value"] > 0.05, r0.get("p_value")
    print(f"  [шум] p={r0.get('p_value', 'нет кандидата')} → не обманулись ✓")

    # 2) вшитый эффект: сильный возврат к среднему — обязаны найти и
    #    подтвердить (p мала, holdout в плюсе), hit > WRnet при комиссии
    n = 2000
    eps = rng.standard_normal(n) * 0.0012
    px = [100.0]
    for i in range(1, n):
        px.append(px[-1] * math.exp(-0.35 * math.log(px[-1] / 100.0) + eps[i]))
    r1 = honest_search_v2({"MR": _mk_recs(px)}, horizon=3, n_nulls=40)
    assert "error" not in r1, r1
    ho = r1["real"]["hold"]
    assert r1["p_value"] <= 0.10, r1["p_value"]
    assert ho["net_avg"] > 0, ho
    #    попадание по знаку ≥ WRnet: комиссия отнимает часть знаковых побед —
    #    та самая разница «процентов владельца» и «процентов v1»
    assert ho["hit"] >= ho["wr_net"], (ho["hit"], ho["wr_net"])
    print(f"  [эффект] holdout hit={ho['hit']*100:.1f}% vs WRnet="
          f"{ho['wr_net']*100:.1f}% (комиссия объясняет разрыв), "
          f"net={ho['net_avg']:+.2f}бп, p={r1['p_value']:.3f} ✓")

    # 3) детерминизм
    assert honest_search_v2(walks, horizon=3, n_nulls=8, seed=5) == \
        honest_search_v2(walks, horizon=3, n_nulls=8, seed=5)
    print("grid_search_v2 self-test OK: шум не проходит, эффект проходит, "
          "hit≥WRnet под комиссией, детерминизм")
