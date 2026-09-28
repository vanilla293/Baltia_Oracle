"""ЕГО ТРЕТИЙ НУЛЬ: а не даст ли ЛЮБАЯ медленная сумма то же самое?

Веса школы (108/81/72/54/27) взяты из ведической традиции. Если эффект
держится на КОНКРЕТНОЙ конфигурации неба, то перетасовка весов между
телами должна его ломать. Если же любая перетасовка даёт то же — значит
работает не небо, а сам класс «гладкая сумма косинусов от медленных
долгот», и это артефакт метода.

Тасуем веса ТОЛЬКО между планетами (Солнце и Луна и так исключены).

python3 -m backend.lab.weight_perm
"""
import json
from datetime import date, datetime, timezone

import numpy as np

from backend import aether
from backend.lab.ar_nulls import ar_series, effect

HIST = "/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/moex_hist.json"
NPERM = 300
RNG = np.random.default_rng(31337)
PLANETS = ["Меркурий", "Венера", "Марс", "Юпитер", "Сатурн", "Уран",
           "Нептун", "Плутон"]


def parts_of(dates, ticker):
    """Вклад каждой планеты ОТДЕЛЬНО, без веса — вес наложим потом."""
    g = aether.genesis_for(ticker)
    nat = aether.natal_lons(g["genesis"])
    t0 = datetime(dates[0].year, dates[0].month, dates[0].day, 12,
                  tzinfo=timezone.utc)
    tr = aether.transit_series(t0, (dates[-1] - dates[0]).days * 24 + 24, 24.0)
    lam0 = np.array([r["lon"] for r in nat], float)[None, :]
    out = {}
    for nm in PLANETS:
        d = np.abs(aether._wrap180(tr["lam"][nm][:, None] - lam0))
        k = aether._ASP_K[np.argmin(
            np.abs(d[:, :, None] - aether._ASP_ANG[None, None, :]), axis=2)]
        # E без веса: (θ/θ_ref)·хроно — вес W вынесен наружу
        e_unw = tr["E"][nm] / aether.W_VEDIC[nm]
        out[nm] = np.sum(e_unw[:, None] * np.cos(np.radians(k * d)), axis=1) / 100.0
    return np.asarray(tr["ts"], float), out


def run():
    raw = json.load(open(HIST, encoding="utf-8"))
    PARTS, AR, DS = {}, {}, {}
    for tk in sorted(raw):
        rows = raw[tk]
        ds = [date.fromisoformat(r[0]) for r in rows]
        cl = np.array([float(r[4]) for r in rows])
        ts, pr = parts_of(ds, tk)
        pmap = {datetime.fromtimestamp(t, timezone.utc).date(): i
                for i, t in enumerate(ts)}
        idx = [pmap.get(d) for d in ds]
        PARTS[tk] = {nm: np.array([v[i] if i is not None else np.nan
                                   for i in idx]) for nm, v in pr.items()}
        AR[tk] = ar_series(cl); DS[tk] = ds

    def stat(weights):
        vals, pos = [], 0
        for tk in PARTS:
            psi = sum(weights[nm] * PARTS[tk][nm] for nm in PLANETS)
            e = effect(psi, AR[tk])
            if e:
                vals.append(e[0]); pos += 1 if e[0] > 0 else 0
        return (float(np.median(vals)) if vals else np.nan, pos, len(vals))

    W0 = {nm: float(aether.W_VEDIC[nm]) for nm in PLANETS}
    med0, sign0, n0 = stat(W0)
    print("=" * 92)
    print(f"ВЕСА ШКОЛЫ {W0}")
    print(f"  медиана эффекта {med0:+.4f}, знак совпал {sign0}/{n0}")
    print("=" * 92)
    vals = list(W0.values())
    meds, signs = [], []
    for i in range(NPERM):
        sh = list(vals); RNG.shuffle(sh)
        w = {nm: sh[j] for j, nm in enumerate(PLANETS)}
        m, s, _ = stat(w)
        meds.append(m); signs.append(s)
        if (i + 1) % 75 == 0:
            mm = np.array(meds); ss = np.array(signs)
            print(f"  … {i+1}: медиана нуля {np.median(mm):+.4f}, "
                  f"доля 8/8 = {float((ss == 8).mean())*100:.1f}%, "
                  f"p(медиана) = {float((mm >= med0).mean()):.3f}", flush=True)
    meds = np.array(meds); signs = np.array(signs)
    print(f"\nПЕРЕТАСОВКА ВЕСОВ ({NPERM} комбинаций):")
    print(f"  медиана эффекта: центр {np.median(meds):+.4f}, "
          f"p95 {np.quantile(meds,0.95):+.4f}, макс {meds.max():+.4f}")
    print(f"  единодушие: медиана {np.median(signs):.1f}/8, "
          f"доля 8/8 = {float((signs == 8).mean())*100:.1f}%")
    print(f"  p(величина) = {float((meds >= med0).mean()):.4f}")
    print(f"  p(единодушие) = {float((signs >= sign0).mean()):.4f}")
    print()
    # ЧИТАТЬ НАДО ПО p, А НЕ ПО ДОЛЕ 8/8 (первая версия судила по порогу 0.5
    # и печатала неверный вывод — исправлено).
    p_val = float((meds >= med0).mean())
    p_sgn = float((signs >= sign0).mean())
    if p_val > 0.10 and p_sgn > 0.10:
        print("ЧИТАЕТСЯ: перетасовка НИЧЕГО не меняет (p по величине "
              f"{p_val:.2f}, по знаку {p_sgn:.2f}). Веса школы — украшение:")
        print("любое распределение тех же чисел по телам даёт тот же эффект.")
        print("Значит дело НЕ в весах. Остаётся вопрос, дело ли в самих")
        print("планетных частотах — это проверяет стенд fake_sky.py.")
    else:
        print("ЧИТАЕТСЯ: перетасовка ломает эффект → конфигурация весов важна.")
    json.dump({"med0": med0, "sign0": sign0, "meds": meds.tolist(),
               "signs": signs.tolist()},
              open("/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/weight_perm.json",
                   "w", encoding="utf-8"))


if __name__ == "__main__":
    run()
