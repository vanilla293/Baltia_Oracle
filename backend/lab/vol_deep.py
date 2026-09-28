"""ЕДИНСТВЕННЫЙ ЖИВОЙ СЛЕД: волатильность рынка против годового нуля.

Круг 2 стенда дал по волатильности 36 признаков, переживших held-out, при
нуле (перестановка годовых блоков) с медианой 8 и p95=28 → p=0.033. Но
перестановок было всего 30, то есть p=0.033 — это ровно «одна из тридцати».
Слишком грубо, чтобы что-то утверждать. Здесь:

  • 300 перестановок вместо 30 — точность p до 0.003;
  • печатаем, КАКИЕ именно признаки выживают (может, это одно семейство);
  • отдельно проверяем, не сводится ли всё к календарю: тот же тест на
    остатках после вычитания солнечно-лунных признаков;
  • контроль знака: у выжившего признака знак связи обязан совпадать на
    train и held-out (иначе это не сигнал, а переворот).

python3 -m backend.lab.vol_deep
"""
import json
from datetime import date

import numpy as np
from scipy import stats

from backend.lab.sky_feats import build
from backend.lab.market_ether_lab2 import (daily_market, screen, bh,
                                           block_permute, SPLIT)

NPERM = 300
RNG = np.random.default_rng(4242)


def survivors(F, y, tr):
    res = screen(F, y, tr)
    p1 = np.array([r[1] for r in res])
    sel = np.where(bh(p1))[0]
    surv = [i for i in sel if res[i][3] < 0.05 and res[i][0] * res[i][2] > 0]
    return res, sel, surv


def run():
    dts, ret, _ = daily_market()
    names, F = build(dts)
    years = np.array([d.year for d in dts])
    tr = np.array([d < SPLIT for d in dts])
    y = np.abs(ret)
    print("=" * 92)
    print(f"ВОЛАТИЛЬНОСТЬ ПРОТИВ ГОДОВОГО НУЛЯ · {len(dts)} дней · {NPERM} перестановок")
    print("=" * 92)

    res, sel, surv = survivors(F, y, tr)
    print(f"прошли BH на train {len(sel)}, пережили held-out {len(surv)}\n")
    print("ВЫЖИВШИЕ (train r → held-out r):")
    fam = {}
    for i in sorted(surv, key=lambda i: -abs(res[i][2]))[:20]:
        print(f"  {names[i]:34s} {res[i][0]:+.4f} → {res[i][2]:+.4f}  "
              f"p_ho={res[i][3]:.4f}")
    for i in surv:
        fam[names[i].split(":")[0]] = fam.get(names[i].split(":")[0], 0) + 1
    print(f"\nпо семействам: {fam}")
    pairs = {}
    for i in surv:
        if names[i].startswith("аспект:"):
            pr = names[i].split(":")[1].split("·")[0]
            pairs[pr] = pairs.get(pr, 0) + 1
    print(f"пары-аспекты среди выживших: {dict(sorted(pairs.items(), key=lambda kv: -kv[1]))}")

    cnt = []
    for k in range(NPERM):
        Xp = block_permute(F, years, RNG)
        _, _, sv = survivors(Xp, y, tr)
        cnt.append(len(sv))
        if (k + 1) % 60 == 0:
            c = np.array(cnt)
            print(f"  … {k+1} перестановок: медиана {np.median(c):.0f}, "
                  f"p≈{(c >= len(surv)).mean():.3f}", flush=True)
    cnt = np.array(cnt)
    p = float((cnt >= len(surv)).mean())
    print(f"\nНУЛЬ: медиана {np.median(cnt):.0f}, p95 {np.quantile(cnt,0.95):.0f}, "
          f"максимум {cnt.max()}")
    print(f"НАСТОЯЩИХ выживших {len(surv)} → p = {p:.4f}  "
          f"{'СИГНАЛ ВЫШЕ НУЛЯ' if p < 0.05 else 'НЕОТЛИЧИМО ОТ НУЛЯ'}")

    # тот же тест БЕЗ КАЛЕНДАРЯ
    clock = [i for i, n in enumerate(names) if n.startswith("часы:")]
    C = np.column_stack([F[:, clock], np.ones(len(F))])
    beta, *_ = np.linalg.lstsq(C, F, rcond=None)
    Fr = F - C @ beta
    _, sel2, surv2 = survivors(Fr, y, tr)
    cnt2 = []
    for _ in range(NPERM // 3):
        Xp = block_permute(Fr, years, RNG)
        _, _, sv = survivors(Xp, y, tr)
        cnt2.append(len(sv))
    cnt2 = np.array(cnt2)
    p2 = float((cnt2 >= len(surv2)).mean())
    print(f"\nБЕЗ КАЛЕНДАРЯ: выживших {len(surv2)}, нуль медиана "
          f"{np.median(cnt2):.0f} → p = {p2:.4f}  "
          f"{'держится' if p2 < 0.05 else 'исчезает'}")
    assert len(cnt) == NPERM
    print("\nself-тест OK")


if __name__ == "__main__":
    run()
