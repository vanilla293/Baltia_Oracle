"""ЭФИРНЫЙ СТЕНД ПИФИИ НА БИРЖЕ — тот же жёсткий протокол, что убил
небесные слои на ресторанах. Теперь на рынке, где эфир «убеждает».

Полигон: 8 бумаг MOEX, дневные бары 2013-2026, ~25 тыс бумаго-дней.
Признаки: 249 небесных (backend/lab/sky_feats.py), разделённых на
  ЧАСЫ — заведомый календарь (долгота Солнца, фаза Луны, полугодовая),
  АСПЕКТЫ — косинусы кратных углов всех 45 пар, гармоники 1/2/3/4/6,
  СТАНЦИИ — хроно-член 1/(|dλ/dt|+ε),
  ЭФИР — E_i школы и скалярное напряжение.

Мишени: знак завтрашней доходности (направление) и |доходность|
(волатильность). Направление — то, ради чего эфир и строился.

ПРОТОКОЛ (без поблажек):
  1. Обучение только на TRAIN (до 2022-12-31), приговор на HELD-OUT
     (2023-01-01 .. конец) — отбор held-out не видит.
  2. Каждый признак судится отдельно, затем поправка Бенджамини-Хохберга
     q=0.10 на все 249.
  3. ПЛАЦЕБО: те же признаки, сдвинутые во времени на +173 и −211 дней
     (не кратно году и не кратно лунному месяцу).
  4. ЧАСТНЫЙ ТЕСТ: из каждого небесного признака вычитается проекция на
     ЧАСЫ (регрессия на календарные признаки). Остаётся ли что-нибудь,
     когда календарь убран, — вот и весь вопрос.

python3 -m backend.lab.market_ether_lab
"""
import json, math, os, sys
from datetime import date, datetime, timedelta

import numpy as np
from scipy import stats

from backend.lab.sky_feats import build

HIST = "/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/moex_hist.json"
SPLIT = date(2023, 1, 1)
PLACEBO = (173, -211)
Q = 0.10


def load_panel():
    raw = json.load(open(HIST, encoding="utf-8"))
    panel = {}
    for tk, rows in raw.items():
        ds = [date.fromisoformat(r[0]) for r in rows]
        cl = np.array([float(r[4]) for r in rows])
        ret = np.full(len(cl), np.nan)
        ret[:-1] = np.diff(np.log(cl))       # доходность СЛЕДУЮЩЕГО дня
        panel[tk] = {"dates": ds, "close": cl, "ret_next": ret}
    return panel


def t_of(x, y):
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 100:
        return 0.0, 1.0
    r, p = stats.pearsonr(x[m], y[m])
    n = m.sum()
    t = r * math.sqrt(max(n - 2, 1) / max(1 - r * r, 1e-12))
    return float(t), float(p)


def screen(panel, names, F, dmap, target, shift=0, resid_clock=False):
    """Объединяем бумаги: для каждого признака — t-статистика по пулу."""
    clock = [i for i, n in enumerate(names) if n.startswith("часы:")]
    Xs, Ys, Ts = [], [], []
    for tk, d in panel.items():
        idx, ys = [], []
        for k, dt in enumerate(d["dates"]):
            src = dt - timedelta(shift)
            j = dmap.get(src)
            if j is None:
                continue
            y = d[target][k]
            if not np.isfinite(y):
                continue
            idx.append(j); ys.append(y)
        if len(idx) < 300:
            continue
        Xs.append(F[idx]); Ys.append(np.array(ys))
        Ts.append(np.array([d["dates"][k] for k, dt in enumerate(d["dates"])
                            if (dt - timedelta(shift)) in dmap
                            and np.isfinite(d[target][k])]))
    if not Xs:
        return None
    X = np.vstack(Xs); Y = np.concatenate(Ys); T = np.concatenate(Ts)
    if resid_clock and clock:
        C = np.column_stack([X[:, clock], np.ones(len(X))])
        beta, *_ = np.linalg.lstsq(C, X, rcond=None)
        X = X - C @ beta                      # убираем календарь из ВСЕХ
    tr = np.array([t < SPLIT for t in T])
    te = ~tr
    out = []
    for i, nm in enumerate(names):
        t1, p1 = t_of(X[tr, i], Y[tr])
        t2, p2 = t_of(X[te, i], Y[te])
        out.append((nm, t1, p1, t2, p2))
    return out, int(tr.sum()), int(te.sum())


def bh(ps, q=Q):
    idx = np.argsort(ps)
    m = len(ps)
    keep = np.zeros(m, bool)
    for r, i in enumerate(idx, 1):
        if ps[i] <= q * r / m:
            keep[idx[:r]] = True
    return keep


def report(res, tag, ntr, nte):
    names = [r[0] for r in res]
    p_tr = np.array([r[2] for r in res])
    t_te = np.array([r[3] for r in res])
    p_te = np.array([r[4] for r in res])
    keep = bh(p_tr)
    print(f"\n── {tag} ── train {ntr} набл., held-out {nte}")
    print(f"   прошли BH q=0.10 на TRAIN: {int(keep.sum())} из {len(res)}")
    if not keep.sum():
        print("   → на обучении не выжил НИ ОДИН признак")
        return []
    sel = [(names[i], res[i][1], p_tr[i], t_te[i], p_te[i])
           for i in np.where(keep)[0]]
    sel.sort(key=lambda r: -abs(r[1]))
    conf = [s for s in sel if s[4] < 0.05 and s[1] * s[3] > 0]
    for nm, t1, p1, t2, p2 in sel[:12]:
        mark = "✔ ДЕРЖИТСЯ" if (p2 < 0.05 and t1 * t2 > 0) else "✗ рассыпался"
        print(f"   {nm:34s} train t={t1:+6.2f} p={p1:.2e} │ "
              f"held-out t={t2:+6.2f} p={p2:6.3f}  {mark}")
    if len(sel) > 12:
        print(f"   … ещё {len(sel)-12}")
    print(f"   ИТОГ: пережили held-out {len(conf)} из {len(sel)}")
    return conf


def run():
    panel = load_panel()
    all_d = sorted({d for v in panel.values() for d in v["dates"]})
    lo, hi = all_d[0] - timedelta(400), all_d[-1] + timedelta(10)
    grid = [lo + timedelta(i) for i in range((hi - lo).days + 1)]
    names, F = build(grid)
    dmap = {d: i for i, d in enumerate(grid)}
    print("=" * 96)
    print(f"ЭФИРНЫЙ СТЕНД НА БИРЖЕ · {len(panel)} бумаг · "
          f"{sum(len(v['dates']) for v in panel.values())} бумаго-дней · "
          f"{len(names)} небесных признаков")
    print(f"train до {SPLIT}, held-out после — отбор его не видит")
    print("=" * 96)

    summary = {}
    for target, tname in (("ret_next", "НАПРАВЛЕНИЕ (доходность завтра)"),):
        r = screen(panel, names, F, dmap, target)
        conf = report(r[0], tname, r[1], r[2])
        summary[tname] = len(conf)
        for sh in PLACEBO:
            rp = screen(panel, names, F, dmap, target, shift=sh)
            cp = report(rp[0], f"ПЛАЦЕБО сдвиг {sh:+d} дн · {tname}", rp[1], rp[2])
            summary[f"плацебо {sh}"] = len(cp)
        rr = screen(panel, names, F, dmap, target, resid_clock=True)
        cc = report(rr[0], f"БЕЗ КАЛЕНДАРЯ (часы вычтены) · {tname}", rr[1], rr[2])
        summary["без календаря"] = len(cc)

    # мишень «волатильность»
    for tk, d in panel.items():
        d["absret"] = np.abs(d["ret_next"])
    r = screen(panel, names, F, dmap, "absret")
    conf_v = report(r[0], "ВОЛАТИЛЬНОСТЬ (|доходность| завтра)", r[1], r[2])
    summary["волатильность"] = len(conf_v)
    rv = screen(panel, names, F, dmap, "absret", resid_clock=True)
    cv = report(rv[0], "ВОЛАТИЛЬНОСТЬ БЕЗ КАЛЕНДАРЯ", rv[1], rv[2])
    summary["волатильность без календаря"] = len(cv)
    for sh in PLACEBO:
        rp = screen(panel, names, F, dmap, "absret", shift=sh)
        cp = report(rp[0], f"ПЛАЦЕБО {sh:+d} · волатильность", rp[1], rp[2])
        summary[f"волат. плацебо {sh}"] = len(cp)

    print("\n" + "=" * 96)
    print("СВОДКА (сколько признаков пережило held-out)")
    print("=" * 96)
    for k, v in summary.items():
        print(f"  {k:44s}: {v}")
    return summary


if __name__ == "__main__":
    run()
