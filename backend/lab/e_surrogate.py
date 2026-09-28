"""ПОСЛЕДНИЙ ЧЕСТНЫЙ НУЛЬ ДЛЯ E(t) — спектральные суррогаты вместо np.roll.

Итог всей охоты: носителем эффекта «край минус середина» в AR(1) оказалась
НЕ астрология, а огибающая E(t) = (θ/θ_ref)·W·хроно — видимый угловой
размер тела и хроно-член, взрывающийся в стояниях. Подмена долгот её не
трогает (p=0.51), смена генезиса не трогает (65-й перцентиль), а
циклический сдвиг — трогает и убивает (p=0.01).

НО циклический сдвиг сам по себе нечестен: np.roll вставляет ШОВ, рвущий
автокорреляцию. На PLV это уже поймали — расхождение с честным нулём до
65.8 перцентильных пункта. Значит последний живой нуль подозрителен ровно
так же.

Честный нуль для гладкого автокоррелированного ряда — суррогат с ТЕМ ЖЕ
спектром мощности и случайными фазами (Теилер 1992). Он сохраняет всю
автокорреляцию и гладкость, шва не вносит, разрушает только временную
привязку. Плюс IAAFT — тот же спектр И то же распределение значений.

python3 -m backend.lab.e_surrogate
"""
import json
from datetime import date, datetime, timezone

import numpy as np

from backend import aether
from backend.lab.ar_nulls import ar_series, effect
from backend.lab.psi_split import psi_parts

HIST = "/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/moex_hist.json"
NSUR = 300
RNG = np.random.default_rng(20260801)


def phase_surrogate(x, rng):
    """Тот же спектр мощности, случайные фазы (Theiler 1992)."""
    n = len(x)
    X = np.fft.rfft(x - x.mean())
    ph = rng.uniform(0, 2 * np.pi, len(X))
    ph[0] = 0.0
    if n % 2 == 0:
        ph[-1] = 0.0
    return np.fft.irfft(np.abs(X) * np.exp(1j * ph), n) + x.mean()


def iaaft(x, rng, iters=40):
    """Тот же спектр И то же распределение значений (Schreiber-Schmitz)."""
    n = len(x)
    amp = np.abs(np.fft.rfft(x))
    srt = np.sort(x)
    y = rng.permutation(x)
    for _ in range(iters):
        Y = np.fft.rfft(y)
        y = np.fft.irfft(amp * np.exp(1j * np.angle(Y)), n)
        y = srt[np.argsort(np.argsort(y))]
    return y


def run():
    raw = json.load(open(HIST, encoding="utf-8"))
    PSI, AR = {}, {}
    for tk in sorted(raw):
        rows = raw[tk]
        ds = [date.fromisoformat(r[0]) for r in rows]
        cl = np.array([float(r[4]) for r in rows])
        ts, parts = psi_parts(ds, tk)
        pmap = {datetime.fromtimestamp(t, timezone.utc).date(): i
                for i, t in enumerate(ts)}
        p = parts["Ψ без Солнца и Луны"]
        PSI[tk] = np.array([p[pmap[d]] if d in pmap else np.nan for d in ds])
        AR[tk] = ar_series(cl)

    def stat(gen):
        vals, pos = [], 0
        for tk in PSI:
            x = PSI[tk]
            m = np.isfinite(x)
            z = x.copy()
            z[m] = gen(x[m])
            e = effect(z, AR[tk])
            if e:
                vals.append(e[0]); pos += 1 if e[0] > 0 else 0
        return (float(np.median(vals)) if vals else np.nan, pos, len(vals))

    med0, sg0, n0 = stat(lambda v: v)
    print("=" * 92)
    print(f"НАСТОЯЩАЯ Ψ без Солнца и Луны: {med0:+.4f}, знак {sg0}/{n0}")
    print("=" * 92)

    for name, gen in (("ФАЗОВЫЙ СУРРОГАТ (тот же спектр)",
                       lambda v: phase_surrogate(v, RNG)),
                      ("IAAFT (спектр + распределение)",
                       lambda v: iaaft(v, RNG))):
        meds, sgs = [], []
        for i in range(NSUR):
            m, s, _ = stat(gen)
            meds.append(m); sgs.append(s)
            if (i + 1) % 100 == 0:
                mm = np.array(meds); ss = np.array(sgs)
                print(f"  … {name[:20]} {i+1}: медиана {np.median(mm):+.4f}, "
                      f"8/8 у {float((ss==8).mean())*100:.1f}%", flush=True)
        meds = np.array(meds); sgs = np.array(sgs)
        pm = float((meds >= med0).mean()); ps = float((sgs >= sg0).mean())
        print(f"\n{name}:")
        print(f"  нуль: медиана {np.median(meds):+.4f}, p95 {np.quantile(meds,0.95):+.4f}, "
              f"макс {meds.max():+.4f}, доля 8/8 = {float((sgs==8).mean())*100:.1f}%")
        print(f"  p(величина) = {pm:.4f}   p(единодушие) = {ps:.4f}   "
              f"{'ВЫШЕ НУЛЯ' if (pm < 0.05 and ps < 0.05) else 'НЕОТЛИЧИМО'}\n")

    assert n0 == 8 and np.isfinite(med0)
    print("self-тест OK")


if __name__ == "__main__":
    run()
