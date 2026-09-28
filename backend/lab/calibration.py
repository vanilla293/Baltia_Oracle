# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ · ЛАБ — КАЛИБРОВКА ВЕРОЯТНОСТЕЙ («70% — это сколько на самом деле?»).

Прямой вопрос владельца: «когда он пишет процент — 100% или 70% — какой шанс
попадания они реально имеют?»

Это вопрос о КАЛИБРОВКЕ, и он важнее точности. Модель может угадывать 55% и
быть полезной, если её «80%» действительно означает 80%. И наоборот — модель
с хорошей точностью, но врущими процентами, опасна: размер позиции считается
ОТ уверенности (leverage.ladder), поэтому завышенная уверенность = завышенное
плечо ровно там, где модель не права.

Что считает модуль:
  · НАДЁЖНОСТЬ ПО БИНАМ: разбить предсказания по заявленной уверенности
    (50-60 / 60-70 / 70-80 / 80-90 / 90-100%) и сравнить с фактической долей
    попаданий в каждом бине. Идеал — диагональ;
  · BRIER SCORE: средний квадрат ошибки вероятности. 0 — идеал, 0.25 — монета.
    Отдельно разложение на КАЛИБРОВКУ и РАЗРЕШАЮЩУЮ СПОСОБНОСТЬ (Murphy);
  · ПЕРЕКОС: систематически завышена уверенность или занижена;
  · КАЛИБРОВОЧНАЯ ПОПРАВКА: во что реально превращать заявленный процент
    (изотоническая-лайт: монотонная подгонка по бинам).

⚫ Калибровка измеряется на прошлой выборке; на новых данных она может
поехать. НЕ сигнал. 18+.

Запуск: python3 -m backend.lab.calibration --db data/moex_real.db
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

BINS = [(0.50, 0.55), (0.55, 0.60), (0.60, 0.70), (0.70, 0.80),
        (0.80, 0.90), (0.90, 1.001)]
MIN_CELL = 25
COMMISSION_BPS = 5.0


def conf_to_prob(conf: float, direction: int) -> float:
    """Уверенность машины → заявленная вероятность своей стороны.

    В Оракуле confidence = |2P−1|, то есть P = 0.5 + conf/2 для названной
    стороны. Здесь то же преобразование: conf 0 → 50%, conf 1 → 100%."""
    if not direction:
        return 0.5
    return 0.5 + max(0.0, min(1.0, conf)) / 2.0


def collect(db: str, H: int = 180) -> list:
    """Наблюдения: заявленная вероятность против факта."""
    out = []
    for sec in ft.instruments(db):
        d = ft.load_series(db, sec)
        for r in ft.run_instrument(sec, d):
            fwd = r.get(f"fwd{H}")
            if fwd is None or abs(fwd) < ft.DEAD_FRAC or not r["dir"]:
                continue
            p = conf_to_prob(r["conf"], r["dir"])
            hit = 1 if (r["dir"] > 0) == (fwd > 0) else 0
            out.append({"sec": sec, "p": p, "hit": hit,
                        "ret": r["dir"] * fwd * 1e4, "conf": r["conf"]})
    return out


def reliability(obs: list) -> list:
    """Надёжность по бинам: заявлено против фактического."""
    rows = []
    for lo, hi in BINS:
        cell = [o for o in obs if lo <= o["p"] < hi]
        if len(cell) < MIN_CELL:
            rows.append({"bin": f"{lo*100:.0f}–{hi*100:.0f}%", "n": len(cell),
                         "claimed": None, "actual": None, "gap": None,
                         "mean_bps": None, "skip": True})
            continue
        claimed = float(np.mean([o["p"] for o in cell]))
        actual = float(np.mean([o["hit"] for o in cell]))
        rows.append({"bin": f"{lo*100:.0f}–{hi*100:.0f}%", "n": len(cell),
                     "claimed": claimed, "actual": actual,
                     "gap": actual - claimed,
                     "mean_bps": float(np.mean([o["ret"] for o in cell])),
                     "skip": False})
    return rows


def brier(obs: list) -> dict:
    """Brier score и его разложение (Murphy): калибровка/разрешение/неопределённость."""
    if not obs:
        return {}
    p = np.asarray([o["p"] for o in obs])
    y = np.asarray([o["hit"] for o in obs], dtype=float)
    bs = float(np.mean((p - y) ** 2))
    base = float(np.mean(y))
    unc = base * (1 - base)                    # неопределённость выборки
    # разложение по бинам
    cal = res = 0.0
    n = len(obs)
    for lo, hi in BINS:
        idx = [i for i in range(n) if lo <= p[i] < hi]
        if len(idx) < MIN_CELL:
            continue
        nk = len(idx)
        pk = float(np.mean(p[idx]))
        yk = float(np.mean(y[idx]))
        cal += nk * (pk - yk) ** 2
        res += nk * (yk - base) ** 2
    cal /= n
    res /= n
    return {"brier": bs, "brier_coin": 0.25, "base_rate": base,
            "calibration": cal, "resolution": res, "uncertainty": unc,
            "skill_vs_coin": 1.0 - bs / 0.25}


def isotonic_lite(rel: list) -> list:
    """Монотонная поправка: во что превращать заявленный процент.
    Простое усреднение соседних бинов, нарушающих монотонность (PAVA-lite)."""
    pts = [(r["claimed"], r["actual"], r["n"]) for r in rel if not r["skip"]]
    if len(pts) < 2:
        return []
    vals = [list(x) for x in pts]
    changed = True
    while changed:
        changed = False
        for i in range(len(vals) - 1):
            if vals[i][1] > vals[i + 1][1]:    # нарушение монотонности
                w = vals[i][2] + vals[i + 1][2]
                m = (vals[i][1] * vals[i][2] + vals[i + 1][1] * vals[i + 1][2]) / w
                vals[i][1] = vals[i + 1][1] = m
                changed = True
    return [{"claimed": v[0], "corrected": v[1], "n": v[2]} for v in vals]


def render(obs: list, H: int) -> list:
    L = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    L.append("КАЛИБРОВКА ВЕРОЯТНОСТЕЙ — «когда машина пишет N%, попадает ли в N%»")
    L.append(f"реальные данные MOEX · горизонт {H}с · наблюдений {len(obs)} · {now}")
    L.append("⚫ измерение прошлой выборки; на новых данных калибровка может поехать")
    L.append("")
    rel = reliability(obs)
    L.append("=" * 74)
    L.append("1. НАДЁЖНОСТЬ ПО БИНАМ (заявлено → на самом деле)")
    L.append("=" * 74)
    L.append(f"{'бин уверенности':18s}{'N':>7s}{'заявлено':>11s}{'фактически':>13s}"
             f"{'разрыв':>10s}{'ход':>11s}")
    for r in rel:
        if r["skip"]:
            L.append(f"{r['bin']:18s}{r['n']:7d}"
                     + f"{'мало данных — не судим':>34s}")
            continue
        flag = ""
        if r["gap"] is not None:
            if r["gap"] < -0.10:
                flag = "  ← ВРЁТ (завышает)"
            elif r["gap"] > 0.10:
                flag = "  ← занижает"
        L.append(f"{r['bin']:18s}{r['n']:7d}{r['claimed']*100:10.1f}%"
                 f"{r['actual']*100:12.1f}%{r['gap']*100:+9.1f}пп"
                 f"{r['mean_bps']:+10.2f}бп{flag}")
    L.append("")
    b = brier(obs)
    if b:
        L.append("=" * 74)
        L.append("2. BRIER SCORE (качество вероятностей; 0 идеал, 0.25 монета)")
        L.append("=" * 74)
        L.append(f"  Brier = {b['brier']:.4f}   (монета = 0.25)")
        L.append(f"  навык против монеты: {b['skill_vs_coin']*100:+.1f}%")
        L.append(f"  разложение: калибровка {b['calibration']:.4f} "
                 f"(чем меньше — тем честнее проценты)")
        L.append(f"              разрешение {b['resolution']:.4f} "
                 f"(чем больше — тем полезнее различает случаи)")
        L.append(f"  базовая доля попаданий выборки: {b['base_rate']*100:.1f}%")
        if b["calibration"] > b["resolution"]:
            L.append("  ⚠ калибровка ХУЖЕ разрешения: проценты врут сильнее,")
            L.append("    чем модель различает ситуации — цифру уверенности")
            L.append("    нельзя подавать в размер позиции без поправки.")
    iso = isotonic_lite(rel)
    if iso:
        L.append("")
        L.append("=" * 74)
        L.append("3. ПОПРАВКА: во что реально превращать заявленный процент")
        L.append("=" * 74)
        for r in iso:
            L.append(f"  машина говорит {r['claimed']*100:.0f}%  →  реально "
                     f"{r['corrected']*100:.0f}%   (N={r['n']})")
    # экономика по бинам
    L.append("")
    L.append("=" * 74)
    L.append("4. ЭКОНОМИКА ПО УВЕРЕННОСТИ (окупает ли высокая уверенность комиссию)")
    L.append("=" * 74)
    for r in rel:
        if r["skip"]:
            continue
        net = r["mean_bps"] - COMMISSION_BPS
        verdict = "плюс" if net > 0 else "УБЫТОК"
        L.append(f"  {r['bin']:14s} ход {r['mean_bps']:+7.2f}бп − комиссия "
                 f"{COMMISSION_BPS:.0f}бп = {net:+7.2f}бп  {verdict}")
    return L


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/moex_real.db")
    ap.add_argument("--horizon", type=int, default=180)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    obs = collect(a.db, a.horizon)
    if not obs:
        print("нет наблюдений")
        return
    lines = render(obs, a.horizon)
    txt = "\n".join(lines)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(txt + "\n")
    print(txt)


if __name__ == "__main__":
    main()
