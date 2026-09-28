# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ · ЛАБ — ВАЛИДАТОР ПРЕДСКАЗАНИЙ (что показала машина vs что сделала цена).

Заказ владельца: «наложи предугадыватель на реальную цену, сравнивай, что
показывает программа и что делает цена, ищи чёткие паттерны».

Метод — СТРОГО WALK-FORWARD (причинный, без подглядывания в будущее):
  · идём по реальному тиковому ряду скользящей точкой решения;
  · в каждой точке t признаки считаются ТОЛЬКО по данным ДО t
    (окно прошлого): CVD потока, ветвление Хоукса, Хёрст-DFA, Пригожин-AR1,
    хвост Хилла, наклон цены, сжатие спреда;
  · машина выдаёт вердикт (направление + уверенность);
  · ФАКТ берётся ПОСЛЕ t: ход цены за горизонт H (в секундах);
  · сверяем: угадала сторону или нет; копим статистику.

Против самообмана (обязательная часть, иначе цифры бессмысленны):
  · НУЛЕВЫЕ МОДЕЛИ рядом: «всегда вверх», «моментум» (по знаку прошлого
    хода), «контр-моментум». Машина должна бить их, а не просто иметь
    hit-rate > 50%;
  · биномиальный тест: сколько сигма от честной монеты (без scipy —
    нормальное приближение);
  · разрезы по режимам: только там, где выборка ≥ MIN_CELL наблюдений.

⚫ Результат — измерение прошлого на конкретной выборке, НЕ доказательство
работоспособности в будущем и НЕ сигнал. Отрицательный результат считается
результатом и печатается честно. 18+.

Запуск: python3 -m backend.lab.forward_test --db data/moex_real.db --out отчёт.txt
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from datetime import datetime, timezone

import numpy as np

try:
    from .. import bifurcation, hawkes
except ImportError:                                        # запуск как скрипт
    sys.path.insert(0, __file__.rsplit("/backend/", 1)[0])
    from backend import bifurcation, hawkes

# ── параметры валидации (единственное место) ────────────────────────────────
LOOKBACK_N = 400        # тиков прошлого в окне признаков
HORIZONS_S = (60, 180, 600)   # горизонты факта, секунды
STEP_S = 30             # шаг точки решения, секунды
MIN_TICKS = 1200        # меньше тиков в ряду — инструмент не валидируем
MIN_CELL = 25           # меньше наблюдений в разрезе — не судим (NO DUMMIES)
DEAD_FRAC = 5e-5        # ход меньше — «стоял на месте» (не победа и не провал)


def _f(x, d=0.0):
    try:
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


def load_series(db_path: str, sec: str) -> dict:
    """Тиковый ряд сделок + ряд спреда по инструменту."""
    db = sqlite3.connect(db_path)
    try:
        tr = db.execute("SELECT ts_ms,price,qty,side FROM trades WHERE figi=? "
                        "ORDER BY ts_ms", (sec,)).fetchall()
        bk = db.execute("SELECT ts_ms,spread_bps FROM book WHERE figi=? "
                        "ORDER BY ts_ms", (sec,)).fetchall()
    finally:
        db.close()
    return {"ts": [r[0] for r in tr], "px": [r[1] for r in tr],
            "qty": [r[2] for r in tr], "side": [r[3] for r in tr],
            "bts": [r[0] for r in bk], "spread": [r[1] for r in bk]}


def instruments(db_path: str) -> list:
    db = sqlite3.connect(db_path)
    try:
        return [r[0] for r in db.execute(
            "SELECT figi, COUNT(*) c FROM trades GROUP BY figi "
            "HAVING c >= ? ORDER BY c DESC", (MIN_TICKS,)).fetchall()]
    finally:
        db.close()


# ── признаки ТОЛЬКО по прошлому (причинность) ──────────────────────────────
def features_at(d: dict, i: int, spread_at) -> dict | None:
    """Окно [i-LOOKBACK_N, i) — строго до точки решения i."""
    lo = i - LOOKBACK_N
    if lo < 0:
        return None
    px = d["px"][lo:i]
    if len(px) < LOOKBACK_N // 2 or min(px) <= 0:
        return None
    ts = d["ts"][lo:i]
    qty = d["qty"][lo:i]
    side = d["side"][lo:i]

    cvd = sum(q * s for q, s in zip(qty, side))
    vol = sum(qty) or 1.0
    cvd_norm = cvd / vol                       # −1..+1: перевес агрессора

    a = np.asarray(px, dtype=float)
    slope = float(np.polyfit(np.arange(a.size), a, 1)[0]) / a[-1]  # наклон/цена
    ret_win = (a[-1] - a[0]) / a[0]
    vol_win = float(np.std(np.diff(np.log(a)))) if a.size > 2 else 0.0

    hu = bifurcation.hurst_dfa(px)
    pr = bifurcation.prigogine_market(px, win=min(200, len(px)))
    ta = bifurcation.hill_tail(px)
    hk = hawkes.branching_times(ts) if len(ts) >= 60 else None

    sp0, sp1 = spread_at(ts[0]), spread_at(ts[-1])
    squeeze = ((sp0 - sp1) / sp0) if sp0 else 0.0

    return {
        "cvd_norm": cvd_norm, "slope": slope, "ret_win": ret_win,
        "vol_win": vol_win,
        "hurst": (hu or {}).get("H"),
        "phi": (pr or {}).get("phi"),
        "alpha": (ta or {}).get("alpha"),
        "hawkes": (hk or {}).get("n"),
        "squeeze": squeeze,
    }


def predict(f: dict) -> tuple:
    """Предсказание машины: направление и уверенность 0..1.

    Логика — та же, что в боевом Оракуле, но на доступных здесь голосах
    (стакана в публичном источнике нет): поток CVD, наклон цены, режим
    Хёрста (моментум/возврат), Пригожин (близость слома усиливает).
    Возврат: (dir ∈ {+1,−1,0}, confidence)."""
    votes = []
    # голос потока: реальные деньги агрессора
    c = _f(f.get("cvd_norm"))
    if abs(c) > 0.02:
        votes.append((1.0 if c > 0 else -1.0, min(1.0, abs(c) * 4.0), 1.0))
    # голос наклона (моментум) — вес зависит от режима Хёрста
    h = f.get("hurst")
    s = _f(f.get("slope"))
    if s != 0.0:
        if h is None:
            w = 0.6
            sgn = 1.0 if s > 0 else -1.0
        elif h > 0.55:                        # персистентно → ход продолжится
            w, sgn = 1.0, (1.0 if s > 0 else -1.0)
        elif h < 0.45:                        # антиперсистентно → развернётся
            w, sgn = 0.8, (-1.0 if s > 0 else 1.0)
        else:
            w, sgn = 0.3, (1.0 if s > 0 else -1.0)
        votes.append((sgn, min(1.0, abs(s) * 2e4), w))
    if not votes:
        return 0, 0.0
    num = sum(sg * cf * w for sg, cf, w in votes)
    den = sum(cf * w for sg, cf, w in votes) or 1e-9
    score = num / den
    conf = min(1.0, abs(score) * (sum(w for _, _, w in votes) / 2.0))
    if abs(score) < 0.15:
        return 0, conf
    return (1 if score > 0 else -1), conf


def run_instrument(sec: str, d: dict) -> list:
    """Walk-forward по одному инструменту → список наблюдений."""
    ts, px = d["ts"], d["px"]
    n = len(ts)
    if n < MIN_TICKS:
        return []
    # быстрый доступ к спреду по времени
    bts, spr = d["bts"], d["spread"]

    def spread_at(t):
        if not bts:
            return 0.0
        k = min(range(len(bts)), key=lambda j: abs(bts[j] - t)) if len(bts) < 400 \
            else int(np.searchsorted(bts, t, side="left"))
        k = max(0, min(len(spr) - 1, k))
        return _f(spr[k])

    obs = []
    step_ms = STEP_S * 1000
    t_next = ts[LOOKBACK_N] if n > LOOKBACK_N else None
    if t_next is None:
        return []
    arr_ts = np.asarray(ts)
    for i in range(LOOKBACK_N, n):
        if ts[i] < t_next:
            continue
        t_next = ts[i] + step_ms
        f = features_at(d, i, spread_at)
        if f is None:
            continue
        pdir, conf = predict(f)
        p0 = px[i]
        if p0 <= 0:
            continue
        row = {"sec": sec, "t": ts[i], "p0": p0, "dir": pdir, "conf": conf, **f}
        # ФАКТ: ход цены ПОСЛЕ точки решения на каждом горизонте
        ok_any = False
        for H in HORIZONS_S:
            j = int(np.searchsorted(arr_ts, ts[i] + H * 1000, side="left"))
            if j >= n:
                row[f"fwd{H}"] = None
                continue
            row[f"fwd{H}"] = (px[j] - p0) / p0
            ok_any = True
        if ok_any:
            obs.append(row)
    return obs


# ── статистика и нулевые модели ────────────────────────────────────────────
def _binom_sigma(hits: int, n: int, p: float = 0.5) -> float:
    """Сколько сигма от честной монеты (нормальное приближение)."""
    if n <= 0:
        return 0.0
    mu = n * p
    sd = math.sqrt(n * p * (1 - p)) or 1e-9
    return (hits - mu) / sd


def _eff_sigma(sigma: float, n: int, horizon_s: int, step_s: int = None) -> float:
    """СИГМА С ПОПРАВКОЙ НА ПЕРЕКРЫТИЕ ОКОН — критично, иначе цифры врут.

    Шаг решения STEP_S=30с при горизонте 180с означает, что одно и то же
    движение цены попадает в статистику 6 раз: наблюдения НЕ независимы, и
    наивная сигма завышена в √(H/step) раз. Замер веером: наивные +3.29
    превращаются в +1.34 — то есть находка, выглядевшая значимой, шум.

    n_eff = n · step/H (число реально независимых событий);
    σ_eff = σ · √(n_eff/n). При n_eff < MIN_CELL судить нельзя вообще."""
    step = step_s or STEP_S
    if n <= 0 or horizon_s <= 0:
        return 0.0
    n_eff = n * step / float(horizon_s)
    if n_eff >= n:
        return sigma
    return sigma * math.sqrt(max(0.0, n_eff) / n)


def score_model(obs: list, H: int, name: str, pick) -> dict | None:
    """Оценить модель: pick(row) → +1/−1/0. Считаем только не-нулевые ходы."""
    hits = miss = 0
    rets = []
    for r in obs:
        fwd = r.get(f"fwd{H}")
        if fwd is None or abs(fwd) < DEAD_FRAC:
            continue
        d = pick(r)
        if not d:
            continue
        if (d > 0) == (fwd > 0):
            hits += 1
        else:
            miss += 1
        rets.append(d * fwd)                   # ход «в свою сторону»
    n = hits + miss
    if n < MIN_CELL:
        return None
    hr = hits / n
    mean_ret = float(np.mean(rets)) if rets else 0.0
    sg = _binom_sigma(hits, n)
    n_eff = n * STEP_S / float(H) if H else n
    return {"model": name, "n": n, "hit_rate": hr,
            "sigma": sg,
            "sigma_eff": _eff_sigma(sg, n, H),
            "n_eff": round(n_eff, 1),
            "judgeable": bool(n_eff >= MIN_CELL),
            "mean_ret_bps": mean_ret * 1e4,
            "sum_ret_bps": float(np.sum(rets)) * 1e4}


def analyze(obs: list) -> dict:
    """Полный разбор: машина против нулевых моделей, разрезы по режимам."""
    out = {"n_obs": len(obs), "horizons": {}}
    for H in HORIZONS_S:
        models = []
        for nm, pick in (
            ("МАШИНА (Оракул)", lambda r: r["dir"]),
            ("НУЛЬ: всегда вверх", lambda r: 1),
            ("НУЛЬ: моментум", lambda r: (1 if r["ret_win"] > 0 else -1)),
            ("НУЛЬ: контр-моментум", lambda r: (-1 if r["ret_win"] > 0 else 1)),
            ("НУЛЬ: только CVD", lambda r: (1 if r["cvd_norm"] > 0 else -1)),
        ):
            m = score_model(obs, H, nm, pick)
            if m:
                models.append(m)
        # разрезы для машины: по уверенности и по режимам
        cuts = []
        for cname, flt in (
            ("уверенность ≥0.5", lambda r: r["conf"] >= 0.5),
            ("уверенность ≥0.7", lambda r: r["conf"] >= 0.7),
            ("Хёрст>0.55 (моментум)", lambda r: (r.get("hurst") or 0) > 0.55),
            ("Хёрст<0.45 (возврат)", lambda r: 0 < (r.get("hurst") or 1) < 0.45),
            ("хвост α<2 (тяжёлый)", lambda r: 0 < (r.get("alpha") or 9) < 2),
            ("Хоукс n≥0.7", lambda r: (r.get("hawkes") or 0) >= 0.7),
            ("спред сжимался", lambda r: (r.get("squeeze") or 0) > 0.1),
            ("φ≥0.9 (у порога)", lambda r: (r.get("phi") or 0) >= 0.9),
        ):
            sub = [r for r in obs if flt(r)]
            m = score_model(sub, H, cname, lambda r: r["dir"])
            if m:
                cuts.append(m)
        out["horizons"][H] = {"models": models, "cuts": cuts}
    return out


def render(res: dict, per_sec: dict, title: str) -> list:
    L = [title, ""]
    L.append(f"наблюдений (точек решения): {res['n_obs']}")
    L.append("метод: walk-forward, признаки строго из прошлого, факт — после")
    L.append("⚫ измерение прошлого на конкретной выборке, НЕ сигнал и НЕ обещание")
    L.append("")
    for H in HORIZONS_S:
        blk = res["horizons"].get(H) or {}
        if not blk.get("models"):
            continue
        L.append("=" * 74)
        L.append(f"ГОРИЗОНТ {H} СЕКУНД")
        L.append("=" * 74)
        L.append(f"{'модель':26s}{'N':>7s}{'угадано':>10s}{'σ наив':>9s}"
                 f"{'σ ЧЕСТНАЯ':>11s}{'ход':>11s}")
        for m in blk["models"]:
            mark = ("  ←" if abs(m["sigma_eff"]) >= 2 and m["judgeable"] else
                    "  (n_eff<30: не судим)" if not m["judgeable"] else "")
            L.append(f"{m['model']:26s}{m['n']:7d}{m['hit_rate']*100:9.1f}%"
                     f"{m['sigma']:+9.2f}{m['sigma_eff']:+11.2f}"
                     f"{m['mean_ret_bps']:+10.2f}бп{mark}")
        if blk.get("cuts"):
            L.append("")
            L.append("  РАЗРЕЗЫ машины (где она работает лучше/хуже):")
            L.append(f"  {'условие':26s}{'N':>7s}{'угадано':>10s}"
                     f"{'σ':>9s}{'средний ход':>14s}")
            for c in sorted(blk["cuts"], key=lambda x: -x["hit_rate"]):
                star = "  ←" if abs(c["sigma"]) >= 2 else ""
                L.append(f"  {c['model']:26s}{c['n']:7d}{c['hit_rate']*100:9.1f}%"
                         f"{c['sigma']:+9.2f}{c['mean_ret_bps']:+13.2f}бп{star}")
        L.append("")
    if per_sec:
        L.append("=" * 74)
        L.append("ПО ИНСТРУМЕНТАМ (горизонт 180с, модель — машина)")
        L.append("=" * 74)
        L.append(f"{'инстр':8s}{'N':>7s}{'угадано':>10s}{'σ':>9s}{'средний ход':>14s}")
        for sec, m in sorted(per_sec.items(), key=lambda x: -(x[1] or {}).get("hit_rate", 0)):
            if not m:
                continue
            star = "  ←" if abs(m["sigma"]) >= 2 else ""
            L.append(f"{sec:8s}{m['n']:7d}{m['hit_rate']*100:9.1f}%"
                     f"{m['sigma']:+9.2f}{m['mean_ret_bps']:+13.2f}бп{star}")
    return L


def main() -> None:
    ap = argparse.ArgumentParser(description="Валидация предсказаний на реальных данных")
    ap.add_argument("--db", default="data/moex_real.db")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    secs = instruments(a.db)
    all_obs = []
    per_sec = {}
    for sec in secs:
        d = load_series(a.db, sec)
        obs = run_instrument(sec, d)
        if obs:
            all_obs.extend(obs)
            per_sec[sec] = score_model(obs, 180, sec, lambda r: r["dir"])
        print(f"  {sec}: тиков={len(d['ts'])}, точек решения={len(obs)}", flush=True)
    if not all_obs:
        print("нет наблюдений — валидировать нечего")
        return
    res = analyze(all_obs)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    lines = render(res, per_sec,
                   f"ВАЛИДАЦИЯ ПРЕДСКАЗАНИЙ НА РЕАЛЬНЫХ ДАННЫХ MOEX\n"
                   f"что показала машина vs что сделала цена · {now}")
    txt = "\n".join(lines)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(txt + "\n")
        print(f"\nотчёт: {a.out}")
    print(txt)


if __name__ == "__main__":
    main()
