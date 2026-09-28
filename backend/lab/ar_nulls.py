"""ЕДИНСТВЕННЫЙ ВЫЖИВШИЙ СЛЕД — ПОД ОГОНЬ.

Замер №2 дал: инерция AR(1) доходностей в СЕРЕДИННОЙ корзине Ψ
систематически НИЖЕ (сильнее возврат к среднему), чем на краях, и знак
одинаков у ВСЕХ 8 бумаг, а 4 проходят Холма при пороге 0.01.
Корзины при этом перемешаны по годам (средний год 2019.2/2019.2/2019.5) —
значит это не эпохи.

Прежде чем радоваться, три нуля:
  N1 ЧУЖАЯ Ψ — берём волну ДРУГОЙ бумаги (другой генезис, та же гладкость).
     Если чужая работает так же, генезис ни при чём.
  N2 СДВИГ Ψ во времени на ±91, ±183, ±274 дня. Если эффект держится при
     сдвиге — это не привязка, а форма.
  N3 ДЕНЬ ГОДА — Ψ содержит Солнце, значит частично это сезон. Считаем то
     же самое, заменив Ψ на чистый день года. Если сезон даёт тот же
     эффект — мы померили сезон.

python3 -m backend.lab.ar_nulls
"""
import json
from datetime import date, datetime, timezone

import numpy as np
from scipy import stats

from backend.lab.psi_regime import build_psi

HIST = "/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/moex_hist.json"
AR_WIN = 20


def ar_series(cl):
    lr = np.diff(np.log(cl))
    ar = np.full(len(cl), np.nan)
    for i in range(AR_WIN + 1, len(lr)):
        w = lr[i - AR_WIN:i]
        if w.std() > 0:
            ar[i] = float(np.corrcoef(w[:-1], w[1:])[0, 1])
    return ar


def effect(pv, ar):
    """Эффект = средний AR(1) на краях минус в середине. Плюс p."""
    m = np.isfinite(pv) & np.isfinite(ar)
    if m.sum() < 300:
        return None
    q = np.nanquantile(pv[m], [0.2, 0.4, 0.6, 0.8])
    lo = m & (pv <= q[0]); mid = m & (pv >= q[1]) & (pv <= q[2]); hi = m & (pv >= q[3])
    if min(lo.sum(), mid.sum(), hi.sum()) < 100:
        return None
    ext = lo | hi
    d = float(np.mean(ar[ext]) - np.mean(ar[mid]))
    p = float(stats.mannwhitneyu(ar[ext], ar[mid], alternative="two-sided").pvalue)
    return d, p


def run():
    raw = json.load(open(HIST, encoding="utf-8"))
    data, psi = {}, {}
    for tk in sorted(raw):
        rows = raw[tk]
        ds = [date.fromisoformat(r[0]) for r in rows]
        cl = np.array([float(r[4]) for r in rows])
        ts, p, _ = build_psi(ds, tk)
        pmap = {datetime.fromtimestamp(t, timezone.utc).date(): i
                for i, t in enumerate(ts)}
        data[tk] = dict(ds=ds, ar=ar_series(cl),
                        doy=np.array([d.timetuple().tm_yday for d in ds], float))
        psi[tk] = np.array([p[pmap[d]] if d in pmap else np.nan for d in ds])

    print("=" * 96)
    print("СВОЙ ЭФИР (то, что нашлось)")
    print("=" * 96)
    real = {}
    for tk in sorted(data):
        e = effect(psi[tk], data[tk]["ar"])
        if e:
            real[tk] = e
            print(f"  {tk}: край−середина = {e[0]:+.4f}  p={e[1]:.5f}")
    med_real = float(np.median([v[0] for v in real.values()]))
    print(f"  медиана эффекта по 8 бумагам: {med_real:+.4f}, "
          f"знак совпал у {sum(1 for v in real.values() if v[0] > 0)}/8")

    print("\n" + "=" * 96)
    print("N1 · ЧУЖАЯ Ψ (волна другой бумаги)")
    print("=" * 96)
    alien = []
    for tk in sorted(data):
        for tk2 in sorted(psi):
            if tk2 == tk:
                continue
            n = min(len(psi[tk2]), len(data[tk]["ar"]))
            e = effect(psi[tk2][:n], data[tk]["ar"][:n])
            if e:
                alien.append(e[0])
    alien = np.array(alien)
    print(f"  n={len(alien)}  медиана {np.median(alien):+.4f}  "
          f"доля положительных {float((alien>0).mean())*100:.0f}%")
    print(f"  настоящая медиана {med_real:+.4f} → выше "
          f"{float((alien < med_real).mean())*100:.1f}% чужих")

    print("\n" + "=" * 96)
    print("N2 · СДВИГ Ψ ВО ВРЕМЕНИ")
    print("=" * 96)
    for sh in (-274, -183, -91, 91, 183, 274):
        vals = []
        for tk in sorted(data):
            e = effect(np.roll(psi[tk], sh), data[tk]["ar"])
            if e:
                vals.append(e[0])
        v = np.array(vals)
        print(f"  сдвиг {sh:+4d} дн: медиана {np.median(v):+.4f}  "
              f"знак совпал {int((v>0).sum())}/{len(v)}")

    print("\n" + "=" * 96)
    print("N3 · ЧИСТЫЙ ДЕНЬ ГОДА вместо Ψ (это же сезон?)")
    print("=" * 96)
    for tk in sorted(data):
        # сезон как косинус дня года — «край/середина» = зима-лето / весна-осень
        s = np.cos(2 * np.pi * data[tk]["doy"] / 365.25)
        e = effect(s, data[tk]["ar"])
        if e:
            print(f"  {tk}: край−середина = {e[0]:+.4f}  p={e[1]:.5f}")

    print("\nВЫВОД: эффект живой ТОЛЬКО если он выше чужой Ψ, гибнет при "
          "сдвигах и не воспроизводится чистым сезоном.")


if __name__ == "__main__":
    run()
