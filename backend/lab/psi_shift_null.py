"""РЕШАЮЩИЙ НУЛЬ: 200 случайных циклических сдвигов Ψ без Солнца и Луны.

Что уже устояло у эффекта «край минус середина» в AR(1):
  · не эпохи — корзины содержат все 13-14 лет, corr(Ψ, время) ≈ 0.06;
  · не Солнце — солнечная часть даёт 2/8 по знаку и медиану −0.008;
  · не чужая волна — чужая даёт +0.0055 против настоящих +0.028;
  · не чистый тренд — тренды дают значимость, но со СЛУЧАЙНЫМИ знаками,
    а тут знак совпал у 8 бумаг из 8.
Осталась одна дыра, и она серьёзная: Ψ без Солнца медленная, значит дни
одной корзины идут длинными кусками подряд, а AR(1) сам автокоррелирован.
Эффективное число наблюдений тогда много меньше шестисот, и p раздуты.

Честный нуль на это ровно один: СЛУЧАЙНЫЙ ЦИКЛИЧЕСКИЙ СДВИГ волны. Он
сохраняет и её медленность, и блочную структуру корзин, и всю
автокорреляцию отклика — рвётся только привязка ко времени. Считаем
статистику «сколько бумаг из 8 совпали знаком» и медиану эффекта.

python3 -m backend.lab.psi_shift_null
"""
import json
from datetime import date, datetime, timezone

import numpy as np

from backend.lab.psi_split import psi_parts
from backend.lab.ar_nulls import ar_series, effect

HIST = "/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/moex_hist.json"
NSHIFT = 200
RNG = np.random.default_rng(777)


def run():
    raw = json.load(open(HIST, encoding="utf-8"))
    P, AR = {}, {}
    for tk in sorted(raw):
        rows = raw[tk]
        ds = [date.fromisoformat(r[0]) for r in rows]
        cl = np.array([float(r[4]) for r in rows])
        got = psi_parts(ds, tk)
        if not got:
            continue
        ts, parts = got
        pmap = {datetime.fromtimestamp(t, timezone.utc).date(): i
                for i, t in enumerate(ts)}
        p = parts["Ψ без Солнца и Луны"]
        P[tk] = np.array([p[pmap[d]] if d in pmap else np.nan for d in ds])
        AR[tk] = ar_series(cl)

    def stat(shift):
        ds_, ok = [], 0
        for tk in P:
            e = effect(np.roll(P[tk], shift), AR[tk])
            if e:
                ds_.append(e[0]); ok += 1 if e[0] > 0 else 0
        return (float(np.median(ds_)) if ds_ else np.nan, ok, len(ds_))

    med0, sign0, n0 = stat(0)
    print("=" * 92)
    print(f"НАСТОЯЩАЯ: медиана эффекта {med0:+.4f}, знак совпал {sign0}/{n0}")
    print("=" * 92)
    meds, signs = [], []
    for i in range(NSHIFT):
        sh = int(RNG.integers(60, 3000))
        m, s, n = stat(sh)
        meds.append(m); signs.append(s)
        if (i + 1) % 50 == 0:
            mm = np.array(meds); ss = np.array(signs)
            print(f"  … {i+1} сдвигов: медиана нуля {np.median(mm):+.4f}, "
                  f"p(медиана) = {float((mm >= med0).mean()):.4f}, "
                  f"p(знак 8/8) = {float((ss >= sign0).mean()):.4f}", flush=True)
    meds = np.array(meds); signs = np.array(signs)
    p_med = float((meds >= med0).mean())
    p_sign = float((signs >= sign0).mean())
    print(f"\nНУЛЬ ({NSHIFT} случайных сдвигов):")
    print(f"  медиана эффекта: центр {np.median(meds):+.4f}, "
          f"p95 {np.quantile(meds, 0.95):+.4f}, макс {meds.max():+.4f}")
    print(f"  единодушие знака: медиана {np.median(signs):.1f}/8, "
          f"доля случаев с 8/8 = {float((signs == 8).mean())*100:.1f}%")
    print(f"\n  p по величине эффекта = {p_med:.4f}")
    print(f"  p по единодушию знака = {p_sign:.4f}")
    verdict = "ВЫШЕ НУЛЯ" if (p_med < 0.05 and p_sign < 0.05) else "НЕОТЛИЧИМО"
    print(f"\n  ПРИГОВОР: {verdict}")
    json.dump({"med_real": med0, "sign_real": sign0, "p_med": p_med,
               "p_sign": p_sign, "null_meds": meds.tolist(),
               "null_signs": signs.tolist()},
              open("/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/psi_shift_null.json",
                   "w", encoding="utf-8"))


if __name__ == "__main__":
    run()
