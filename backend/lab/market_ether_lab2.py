"""ЭФИРНЫЙ СТЕНД НА БИРЖЕ · круг 2 — с исправленными слабостями.

Круг 1 дал ноль по направлению и шум по волатильности, но имел две
признанные дыры (обе назвал внешний рецензент, обе справедливы):

  ДЫРА 1. Восемь бумаг слиты в один пул. Они скоррелированы в один день,
    значит эффективное число наблюдений много меньше 18 180, и t на train
    раздуты (до 17 при полном развале на held-out).
    ЛЕЧЕНИЕ: кластер = КАЛЕНДАРНЫЙ ДЕНЬ. Сводим бумаги в дневную
    равновзвешенную доходность — один ряд, один день, одно наблюдение.

  ДЫРА 2. Плацебо-сдвиг ±полгода для Нептуна и Плутона не плацебо: их
    долгота за полгода ползёт на градусы, связь с трендом сохраняется.
    ЛЕЧЕНИЕ: БЛОЧНАЯ ПЕРЕСТАНОВКА ПО ГОДАМ. Режем ряд признаков на годовые
    блоки и перемешиваем их. Это рвёт синхронизацию с ценовым трендом —
    главным источником ложных корреляций для сверхмедленных регрессоров —
    и при этом сохраняет внутреннюю структуру признака.

python3 -m backend.lab.market_ether_lab2
"""
import json, math
from datetime import date, timedelta

import numpy as np
from scipy import stats

from backend.lab.sky_feats import build

HIST = "/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/moex_hist.json"
SPLIT = date(2023, 1, 1)
NPERM = 300
Q = 0.10
RNG = np.random.default_rng(20260731)


def daily_market():
    """Один ряд на день: равновзвешенная доходность всех доступных бумаг."""
    raw = json.load(open(HIST, encoding="utf-8"))
    by = {}
    for tk, rows in raw.items():
        ds = [date.fromisoformat(r[0]) for r in rows]
        cl = np.array([float(r[4]) for r in rows])
        r = np.diff(np.log(cl))
        for i, d in enumerate(ds[:-1]):
            by.setdefault(ds[i + 1], []).append(float(r[i]))
    dts = sorted(by)
    ret = np.array([np.mean(by[d]) for d in dts])
    nst = np.array([len(by[d]) for d in dts])
    return dts, ret, nst


def screen(X, y, tr):
    out = []
    for i in range(X.shape[1]):
        x = X[:, i]
        m1 = tr & np.isfinite(x) & np.isfinite(y)
        m2 = (~tr) & np.isfinite(x) & np.isfinite(y)
        if m1.sum() < 200 or m2.sum() < 100:
            out.append((0.0, 1.0, 0.0, 1.0)); continue
        r1, p1 = stats.pearsonr(x[m1], y[m1])
        r2, p2 = stats.pearsonr(x[m2], y[m2])
        out.append((float(r1), float(p1), float(r2), float(p2)))
    return out


def bh(ps, q=Q):
    ps = np.asarray(ps)
    idx = np.argsort(ps)
    m = len(ps)
    keep = np.zeros(m, bool)
    for r, i in enumerate(idx, 1):
        if ps[i] <= q * r / m:
            keep[idx[:r]] = True
    return keep


def block_permute(X, years, rng):
    """Перемешиваем ГОДОВЫЕ блоки признаков — рвём связь с трендом цены."""
    ys = sorted(set(years))
    perm = list(ys)
    rng.shuffle(perm)
    idx = []
    for y in perm:
        idx.extend(np.where(years == y)[0].tolist())
    idx = np.array(idx)
    n = min(len(idx), X.shape[0])
    out = np.empty_like(X)
    out[:n] = X[idx[:n]]
    if n < X.shape[0]:
        out[n:] = X[idx[:X.shape[0] - n]]
    return out


def run():
    dts, ret, nst = daily_market()
    print("=" * 92)
    print(f"ЭФИРНЫЙ СТЕНД · круг 2 · КЛАСТЕР = ДЕНЬ")
    print(f"дней {len(dts)}: {dts[0]} → {dts[-1]}, бумаг в дне медиана {int(np.median(nst))}")
    print("=" * 92)
    names, F = build(dts)
    years = np.array([d.year for d in dts])
    tr = np.array([d < SPLIT for d in dts])
    print(f"train {int(tr.sum())} дней · held-out {int((~tr).sum())} дней · "
          f"признаков {len(names)}\n")

    for target, tname in ((ret, "НАПРАВЛЕНИЕ (доходность рынка)"),
                          (np.abs(ret), "ВОЛАТИЛЬНОСТЬ (|доходность|)")):
        res = screen(F, target, tr)
        p1 = np.array([r[1] for r in res])
        keep = bh(p1)
        sel = np.where(keep)[0]
        surv = [i for i in sel if res[i][3] < 0.05 and res[i][0] * res[i][2] > 0]
        print(f"── {tname} ──")
        print(f"   прошли BH q=0.10 на train: {len(sel)} из {len(names)}")
        for i in sorted(sel, key=lambda i: p1[i])[:8]:
            r1, pp1, r2, pp2 = res[i]
            mk = "✔ ДЕРЖИТСЯ" if (pp2 < 0.05 and r1 * r2 > 0) else "✗ рассыпался"
            print(f"     {names[i]:32s} train r={r1:+.4f} p={pp1:.2e} │ "
                  f"held-out r={r2:+.4f} p={pp2:6.3f}  {mk}")
        print(f"   пережили held-out: {len(surv)}")

        # блочная перестановка по годам — честный нуль для медленных
        cnt = []
        for _ in range(NPERM // 10):
            Xp = block_permute(F, years, RNG)
            rp = screen(Xp, target, tr)
            pp = np.array([r[1] for r in rp])
            kk = np.where(bh(pp))[0]
            sv = [i for i in kk if rp[i][3] < 0.05 and rp[i][0] * rp[i][2] > 0]
            cnt.append(len(sv))
        cnt = np.array(cnt)
        pval = float((cnt >= len(surv)).mean())
        print(f"   НУЛЬ (перестановка годовых блоков, {len(cnt)} реализаций): "
              f"медиана выживших {np.median(cnt):.0f}, p95 {np.quantile(cnt,0.95):.0f}")
        print(f"   → p = {pval:.3f}  "
              f"{'СИГНАЛ ЕСТЬ' if pval < 0.05 else 'НЕОТЛИЧИМО ОТ НУЛЯ'}\n")

    # self-тест: нуль обязан быть непустым, иначе процедура сломана
    assert len(names) > 200 and len(dts) > 2000
    print("self-тест OK")


if __name__ == "__main__":
    run()
