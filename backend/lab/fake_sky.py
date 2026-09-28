"""ПОСЛЕДНИЙ НУЛЬ: фальшивое небо с НЕпланетными периодами.

Что уже известно про эффект (AR(1) на краях Ψ_без_Солнца_и_Луны выше, чем
в середине; 8/8 по знаку; p=0.01 против случайных сдвигов):
  · веса школы не значат НИЧЕГО — перетасовка даёт p=0.51;
  · привязка ко времени значит — случайный сдвиг убивает эффект.
Отсюда единственный оставшийся вопрос: дело в НАСТОЯЩИХ планетных
частотах или сработает любая медленная многочастотная сумма, привязанная
к календарю?

Строим ФАЛЬШИВОЕ небо: восемь тел с ВЫМЫШЛЕННЫМИ периодами (не равными
планетным и не кратными им), те же гармоники, та же форма суммы, та же
привязка к реальным датам. Если фальшивое небо даёт то же самое —
эффект есть свойство класса функций, а не неба.

python3 -m backend.lab.fake_sky
"""
import json
from datetime import date

import numpy as np

from backend.lab.ar_nulls import ar_series, effect

HIST = "/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/moex_hist.json"
NFAKE = 200
RNG = np.random.default_rng(2718)
# настоящие сидерические периоды планет, суток
REAL = [87.97, 224.7, 686.98, 4332.6, 10759.2, 30688.5, 60182.0, 90560.0]


def fake_psi(ordv, periods, phases, weights):
    """Та же конструкция: сумма косинусов кратных углов с весами."""
    psi = np.zeros(len(ordv))
    for T, ph, w in zip(periods, phases, weights):
        lam = 2 * np.pi * ordv / T + ph
        for k in (1, 2, 3, 4, 6):
            psi += w * np.cos(k * lam) / k
    return psi


def run():
    raw = json.load(open(HIST, encoding="utf-8"))
    D = {}
    for tk in sorted(raw):
        rows = raw[tk]
        ds = [date.fromisoformat(r[0]) for r in rows]
        cl = np.array([float(r[4]) for r in rows])
        D[tk] = (np.array([d.toordinal() for d in ds], float), ar_series(cl))

    W = [27., 54., 72., 54., 108., 108., 81., 108.]

    def stat(periods, phases):
        vals, pos = [], 0
        for tk, (ordv, ar) in D.items():
            e = effect(fake_psi(ordv, periods, phases, W), ar)
            if e:
                vals.append(e[0]); pos += 1 if e[0] > 0 else 0
        return (float(np.median(vals)) if vals else np.nan, pos, len(vals))

    # контроль: настоящие периоды со случайной фазой — должно быть похоже
    print("=" * 92)
    print("КОНТРОЛЬ: настоящие периоды планет, но фаза случайная")
    print("=" * 92)
    ctrl = [stat(REAL, RNG.uniform(0, 2 * np.pi, 8)) for _ in range(40)]
    cm = np.array([c[0] for c in ctrl]); cs = np.array([c[1] for c in ctrl])
    print(f"  медиана {np.median(cm):+.4f}, доля 8/8 = {float((cs==8).mean())*100:.0f}%")

    print("\n" + "=" * 92)
    print("ФАЛЬШИВОЕ НЕБО: выдуманные периоды (30…40000 суток, логравномерно)")
    print("=" * 92)
    meds, signs = [], []
    for i in range(NFAKE):
        per = np.exp(RNG.uniform(np.log(30), np.log(40000), 8))
        ph = RNG.uniform(0, 2 * np.pi, 8)
        m, s, _ = stat(per, ph)
        meds.append(m); signs.append(s)
        if (i + 1) % 50 == 0:
            mm = np.array(meds); ss = np.array(signs)
            print(f"  … {i+1}: медиана {np.median(mm):+.4f}, "
                  f"доля 8/8 = {float((ss==8).mean())*100:.0f}%", flush=True)
    meds = np.array(meds); signs = np.array(signs)
    REAL_MED, REAL_SIGN = 0.0403, 8
    print(f"\nФАЛЬШИВОЕ НЕБО ({NFAKE} вселенных):")
    print(f"  медиана эффекта: центр {np.median(meds):+.4f}, "
          f"p95 {np.quantile(meds,0.95):+.4f}, макс {meds.max():+.4f}")
    print(f"  доля вселенных с 8/8 = {float((signs==8).mean())*100:.1f}%")
    print(f"  НАСТОЯЩЕЕ небо: {REAL_MED:+.4f}, 8/8")
    print(f"  p(величина) = {float((meds >= REAL_MED).mean()):.4f}")
    print(f"  p(единодушие) = {float((signs >= REAL_SIGN).mean()):.4f}")
    print()
    if float((signs == 8).mean()) > 0.2 or float((meds >= REAL_MED).mean()) > 0.10:
        print("ПРИГОВОР: выдуманное небо даёт то же самое. Значит эффект —")
        print("свойство КЛАССА «медленная многочастотная сумма, привязанная к")
        print("календарю», а не настоящих планет. Эфир тут ни при чём 🔵")
    else:
        print("ПРИГОВОР: выдуманное небо НЕ воспроизводит эффект (1% против")
        print("наших 8/8), и настоящие периоды со СЛУЧАЙНОЙ ФАЗОЙ — тоже ноль.")
        print()
        print("ЭТОТ ВЫВОД ОТМЕНЁН — ЧИТАЙ backend/lab/fake_sky_native.py 🔴")
        print("Здесь фальшивая волна строилась ДРУГОЙ формулой (сумма cos(k·λ)")
        print("с весом 1/k), а настоящая Ψ — суммой по аспектам к НАТАЛЬНЫМ")
        print("точкам. Сравнение мешало два различия: форму функции и сами")
        print("положения, и «ноль у фальшивого неба» оказался следствием ФОРМЫ.")
        print("Долг закрыт стендом fake_sky_native.py: долготы подменены ВНУТРИ")
        print("родной машинерии Ψ (те же наталы, гармоники, веса, хроно-член).")
        print("Результат: настоящее небо +0.0403 против +0.0407 у фальшивого,")
        print("p(величина) = 0.52 (фазовый сдвиг) и 0.51 (чужие периоды),")
        print("p(единодушие) = 0.25 и 0.24. Эффект «край−середина» НЕ небесный.")


if __name__ == "__main__":
    run()
