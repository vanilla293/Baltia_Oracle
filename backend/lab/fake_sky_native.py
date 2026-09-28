"""ЧИСТЫЙ ТЕСТ: ДЕЛО В НЕБЕ ИЛИ В ФОРМЕ ФУНКЦИИ?

Предыстория долга. Стенд fake_sky.py строил фальшивое небо ДРУГОЙ формулой
(сумма cos(k·λ) с весом 1/k), а настоящая Ψ — сумма по аспектам к НАТАЛЬНЫМ
точкам с эфирной плотностью E_i. Такое сравнение мешает два различия сразу:
форму функции и сами положения тел. Приговор оттуда брать нельзя.

Здесь долг закрывается. Подменяются ТОЛЬКО ДОЛГОТЫ λ_i(t) внутри РОДНОЙ
машинерии Ψ. Байт-в-байт остаётся всё остальное:
  · те же натальные точки lam0 (aether.natal_lons от того же генезиса);
  · те же аспектные гармоники k∈{1,6,4,3,2} и то же правило ближайшего аспекта;
  · те же веса W_VEDIC и то же спектральное сродство AFFINITY по band;
  · тот же хроно-член и та же θ/θ_ref — то есть E_i(t) берётся НАСТОЯЩАЯ,
    посчитанная по настоящим эфемеридам (мы не трогаем плотность, только угол);
  · та же нормировка — её нет, работаем с сырой Ψ (min-max внутри окна даёт
    0 и 1 по построению, для замеров это яд, см. psi_split.py).

Два способа подмены — они отвечают на РАЗНЫЕ вопросы:
  A. ФАЗОВЫЙ СДВИГ. К долготе каждого тела прибавлен свой случайный
     постоянный угол φ_i. Периоды, скорости, ретроградности, вся динамика —
     настоящие. Разрушены ВЗАИМНЫЕ конфигурации и привязка к наталу.
     Вопрос: важны ли реальные взаимные положения тел?
  B. ЧУЖИЕ ПЕРИОДЫ. Долгота каждого тела заменена равномерным вращением со
     СЛУЧАЙНЫМ периодом того же порядка (настоящий период × log-равномерный
     множитель из [1/3, 3]) и случайной начальной фазой.
     Вопрос: важны ли реальные периоды?

Настоящие «периоды» меряются по РАЗВЁРНУТОЙ долготе самих эфемерид, а не
берутся из справочника: геоцентрическая долгота Меркурия и Венеры обходит
зодиак за ГОД, а не за 88/225 суток — справочный сидерический период тут
был бы просто неверным числом.

Статистика та же, что во всех предыдущих нулях (ar_nulls.effect):
эффект = средний AR(1) на краях корзин Ψ минус в середине; по 8 бумагам
берём медиану эффекта и число бумаг с совпавшим знаком.

python3 -m backend.lab.fake_sky_native
"""
import json
import time
from datetime import date, datetime, timezone

import numpy as np
from scipy import stats

from backend import aether
# АРХИВНЫЙ СТЕНД: воспроизводит СТАРУЮ формулу Ψ со спектральным сродством.
# Из боевого aether AFFINITY удалён (v3.7, вердикт мировоззрения 05.08.2026:
# «подгонка под ответ») — здесь константа оставлена локально, чтобы стенд
# продолжал воспроизводить ровно то, что было измерено в июле 2026.
AFFINITY_ARCHIVE = 1.5

from backend.lab.ar_nulls import ar_series, effect

HIST = "/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/moex_hist.json"
OUT = "/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/fake_sky_native.json"
NREAL = 300               # реализаций каждого способа (в задании минимум 200)
PERIOD_SPREAD = 3.0       # «тот же порядок»: период × [1/3 … 3], логравномерно
SEED_A, SEED_B = 20260731, 731202
# границы «ближайшего аспекта» — середины между 0/60/90/120/180
_BND = np.array([30.0, 75.0, 105.0, 150.0])
DAY = 86400.0


# ══════════════════════════════════════════════════════════════════════
# РОДНАЯ МАШИНЕРИЯ Ψ (с подменяемыми долготами)
# ══════════════════════════════════════════════════════════════════════

def _k_of(d, fast=True):
    """Гармоника ближайшего аспекта. fast — через digitize, точно то же самое
    (совпадение с argmin проверено ассертом в self-тесте)."""
    if fast:
        return aether._ASP_K[np.digitize(d, _BND, right=True)]
    return aether._ASP_K[np.argmin(
        np.abs(d[:, :, None] - aether._ASP_ANG[None, None, :]), axis=2)]


def psi_parts_native(lam, E, lam0, band, fast=True):
    """Вклад каждого тела в сырую Ψ. Копия строк aether.psi_of / psi_parts,
    отличие ровно одно: долготы приходят аргументом, а не из эфемерид."""
    out = {}
    for nm in aether._BODY_ORDER:
        d = np.abs(aether._wrap180(lam[nm][:, None] - lam0))
        k = _k_of(d, fast)
        B = AFFINITY_ARCHIVE if aether.W_VEDIC[nm] == band else 1.0
        out[nm] = np.sum(E[nm][:, None] * B * np.cos(np.radians(k * d)),
                         axis=1) / 100.0
    return out


# ══════════════════════════════════════════════════════════════════════
# ДАННЫЕ
# ══════════════════════════════════════════════════════════════════════

def load():
    """По каждой бумаге: настоящие λ_i и E_i на дневной сетке, натал, band,
    индексы торговых дней и ряд AR(1)."""
    raw = json.load(open(HIST, encoding="utf-8"))
    D = {}
    for tk in sorted(raw):
        rows = raw[tk]
        ds = [date.fromisoformat(r[0]) for r in rows]
        cl = np.array([float(r[4]) for r in rows])
        g = aether.genesis_for(tk)
        nat = aether.natal_lons(g["genesis"])
        if nat is None:
            continue
        t0 = datetime(ds[0].year, ds[0].month, ds[0].day, 12,
                      tzinfo=timezone.utc)
        tr = aether.transit_series(t0, (ds[-1] - ds[0]).days * 24 + 24, 24.0)
        if tr is None:
            continue
        ts = np.asarray(tr["ts"], float)
        pmap = {datetime.fromtimestamp(t, timezone.utc).date(): i
                for i, t in enumerate(ts)}
        idx = np.array([pmap.get(d, -1) for d in ds], int)
        D[tk] = {"ts": ts, "lam": {nm: tr["lam"][nm] for nm in aether._BODY_ORDER},
                 "E": {nm: tr["E"][nm] for nm in aether._BODY_ORDER},
                 "lam0": np.array([r["lon"] for r in nat], float)[None, :],
                 "band": g["band"], "idx": idx, "ar": ar_series(cl),
                 "genesis": g["genesis"],
                 "doy": np.array([d.timetuple().tm_yday for d in ds], float)}
    return D


def to_days(D, tk):
    """Значения Ψ, разложенные на торговые дни бумаги (нет дня → nan)."""
    def f(v):
        idx = D[tk]["idx"]
        out = np.full(len(idx), np.nan)
        ok = idx >= 0
        out[ok] = v[idx[ok]]
        return out
    return f


def eff_periods(D):
    """Настоящий период обращения долготы по РАЗВЁРНУТОМУ ряду эфемерид."""
    tk = max(D, key=lambda k: len(D[k]["ts"]))
    ts = D[tk]["ts"]
    days = (ts[-1] - ts[0]) / DAY
    per = {}
    for nm in aether._BODY_ORDER:
        L = np.degrees(np.unwrap(np.radians(D[tk]["lam"][nm])))
        per[nm] = float(days / (abs(L[-1] - L[0]) / 360.0))
    return per


# ══════════════════════════════════════════════════════════════════════
# СПОСОБЫ ПОДМЕНЫ ДОЛГОТ
# ══════════════════════════════════════════════════════════════════════

def lam_real(D, tk, _):
    return D[tk]["lam"]


def make_shift(phi):
    """A. Настоящая динамика + постоянный сдвиг φ_i у каждого тела."""
    def f(D, tk, _):
        return {nm: (D[tk]["lam"][nm] + phi[nm]) % 360.0
                for nm in aether._BODY_ORDER}
    return f


def make_uniform(periods, phase0, epoch):
    """B. Равномерное вращение: λ_i(t) = φ0_i + 360·(t−epoch)/T_i."""
    def f(D, tk, _):
        dt = (D[tk]["ts"] - epoch) / DAY
        return {nm: (phase0[nm] + 360.0 * dt / periods[nm]) % 360.0
                for nm in aether._BODY_ORDER}
    return f


# ══════════════════════════════════════════════════════════════════════
# СТАТИСТИКА
# ══════════════════════════════════════════════════════════════════════

def runlen(pv):
    """МЕДЛЕННОСТЬ ВОЛНЫ: средняя длина непрерывной серии дней, проведённых в
    одной и той же корзине Ψ. Это и есть «блочность» — та самая, из-за которой
    эффективное число независимых наблюдений много меньше числа дней."""
    m = np.isfinite(pv)
    q = np.nanquantile(pv[m], [0.2, 0.4, 0.6, 0.8])
    lab = np.where(m, np.digitize(pv, q), -1)
    return float(len(lab) / (int(np.count_nonzero(np.diff(lab))) + 1))


def stat(D, lam_fn, fast=True):
    """Медиана эффекта по бумагам и число совпавших знаков — для двух версий Ψ:
    полной и «без Солнца и Луны» (именно на второй жил найденный эффект).
    Плюс медленность волны — для диагноза, чем величина эффекта объясняется."""
    res = {"full": [], "nosunmoon": []}
    rl = {"full": [], "nosunmoon": []}
    for tk in D:
        parts = psi_parts_native(lam_fn(D, tk, None), D[tk]["E"],
                                 D[tk]["lam0"], D[tk]["band"], fast)
        full = sum(parts.values())
        nsm = full - parts["Солнце"] - parts["Луна"]
        f = to_days(D, tk)
        for key, v in (("full", full), ("nosunmoon", nsm)):
            pv = f(v)
            e = effect(pv, D[tk]["ar"])
            res[key].append(e[0] if e else np.nan)
            rl[key].append(runlen(pv))
    out = {}
    for key, vals in res.items():
        v = np.array(vals, float)
        g = np.isfinite(v)
        out[key] = (float(np.median(v[g])) if g.any() else np.nan,
                    int((v[g] > 0).sum()), int(g.sum()), v,
                    float(np.median(rl[key])))
    return out


def _pv(nulls, real, n):
    """Односторонние p с поправкой (r+1)/(n+1) — нуль не может дать 0.0000."""
    nulls = np.asarray(nulls, float)
    g = np.isfinite(nulls)
    return float((int((nulls[g] >= real).sum()) + 1) / (int(g.sum()) + 1))


def run():
    t_start = time.time()
    D = load()
    per = eff_periods(D)
    epoch = min(D[tk]["ts"][0] for tk in D)

    print("=" * 96)
    print("ЧИСТЫЙ ТЕСТ: подмена ДОЛГОТ внутри родной машинерии Ψ")
    print("=" * 96)
    print(f"бумаг: {len(D)}  ({', '.join(sorted(D))})")
    print("периоды обращения долготы, измеренные по самим эфемеридам, суток:")
    print("   " + "  ".join(f"{nm}={per[nm]:.0f}" for nm in aether._BODY_ORDER))
    print("   (Меркурий/Венера ≈ год — геоцентрическая долгота, не сидерический "
          "период)")

    # ОГОВОРКА, без которой «8 бумаг» читаются неверно
    gen = {}
    for tk in D:
        gen.setdefault(D[tk]["genesis"], []).append(tk)
    print(f"РАЗНЫХ ГЕНЕЗИСОВ (то есть разных волн Ψ): {len(gen)}")
    for g, tks in gen.items():
        print(f"   {g} → {', '.join(sorted(tks))}")
    print("   → «8/8 по знаку» это НЕ восемь независимых проверок: у семи бумаг")
    print("     волна одна и та же (мунданный якорь MOEX), а сами бумаги —")
    print("     скоррелированный рынок. Нули ниже устроены так же, поэтому")
    print("     сравнение честное; но само по себе 8/8 весит мало.")

    real = stat(D, lam_real)
    print("\nНАСТОЯЩЕЕ НЕБО")
    for key, ttl in (("full", "Ψ полная            "),
                     ("nosunmoon", "Ψ без Солнца и Луны ")):
        m, s, n, v, r = real[key]
        print(f"  {ttl}: медиана {m:+.4f}  знак совпал {s}/{n}   "
              f"блок {r:.1f} дн   [{'  '.join(f'{x:+.3f}' for x in v)}]")

    rows = {}
    for tag, seed, ttl in (
            ("A", SEED_A, "A · ФАЗОВЫЙ СДВИГ каждого тела "
                          "(периоды настоящие, конфигурации разрушены)"),
            ("B", SEED_B, "B · ЧУЖИЕ ПЕРИОДЫ "
                          "(равномерное вращение, T×[1/3…3], фаза случайна)")):
        rng = np.random.default_rng(seed)
        print("\n" + "=" * 96)
        print(f"{ttl}   ·   {NREAL} реализаций")
        print("=" * 96)
        acc = {k: {"med": [], "sign": [], "rl": []}
               for k in ("full", "nosunmoon")}
        for i in range(NREAL):
            if tag == "A":
                phi = {nm: float(rng.uniform(0, 360))
                       for nm in aether._BODY_ORDER}
                fn = make_shift(phi)
            else:
                fac = np.exp(rng.uniform(-np.log(PERIOD_SPREAD),
                                         np.log(PERIOD_SPREAD),
                                         len(aether._BODY_ORDER)))
                periods = {nm: per[nm] * float(fac[j])
                           for j, nm in enumerate(aether._BODY_ORDER)}
                ph0 = {nm: float(rng.uniform(0, 360))
                       for nm in aether._BODY_ORDER}
                fn = make_uniform(periods, ph0, epoch)
            st = stat(D, fn)
            for k in acc:
                acc[k]["med"].append(st[k][0])
                acc[k]["sign"].append(st[k][1])
                acc[k]["rl"].append(st[k][4])
            if (i + 1) % 75 == 0:
                mm = np.array(acc["nosunmoon"]["med"])
                ss = np.array(acc["nosunmoon"]["sign"])
                print(f"  … {i+1}: [без Солнца и Луны] медиана нуля "
                      f"{np.nanmedian(mm):+.4f}, доля 8/8 = "
                      f"{float((ss == 8).mean())*100:.1f}%, p(вел.) = "
                      f"{_pv(mm, real['nosunmoon'][0], NREAL):.4f}",
                      flush=True)
        rows[tag] = acc

        for key, ttl2 in (("full", "Ψ полная"),
                          ("nosunmoon", "Ψ без Солнца и Луны")):
            m = np.array(acc[key]["med"], float)
            s = np.array(acc[key]["sign"], float)
            rl = np.array(acc[key]["rl"], float)
            r_med, r_sign, r_n, _, r_rl = real[key]
            g = np.isfinite(m)
            print(f"\n  [{ttl2}]  настоящее: {r_med:+.4f}, {r_sign}/{r_n}")
            print(f"    нуль: медиана {np.median(m[g]):+.4f}  "
                  f"p95 {np.quantile(m[g], 0.95):+.4f}  макс {m[g].max():+.4f}")
            print(f"    единодушие нуля: медиана {np.median(s):.1f}/8, "
                  f"доля 8/8 = {float((s == 8).mean())*100:.1f}%, "
                  f"доля 0/8 = {float((s == 0).mean())*100:.1f}%")
            print(f"    p(величина) = {_pv(m, r_med, NREAL):.4f}     "
                  f"p(единодушие знака) = {_pv(s, r_sign, NREAL):.4f}")
            print(f"    настоящее выше "
                  f"{float((m[g] < r_med).mean())*100:.1f}% фальшивых небес")
            rho = float(stats.spearmanr(rl[g], m[g]).statistic)
            print(f"    МЕДЛЕННОСТЬ: блок нуля {np.median(rl):.1f} дн "
                  f"(настоящее небо {r_rl:.1f} дн, выше "
                  f"{float((rl < r_rl).mean())*100:.0f}% нулей); "
                  f"ρ(блок, эффект) = {rho:+.2f}")

    # ── контроль формы: равномерное вращение с НАСТОЯЩИМИ периодами и
    #    НАСТОЯЩЕЙ стартовой фазой — цена самой замены эфемериды на круг
    ph_real = {nm: float(D[max(D, key=lambda k: len(D[k]["ts"]))]["lam"][nm][0])
               for nm in aether._BODY_ORDER}
    ctrl = stat(D, make_uniform(per, ph_real, epoch))
    print("\n" + "=" * 96)
    print("КОНТРОЛЬ ФОРМЫ: равномерное вращение с НАСТОЯЩИМИ периодами и "
          "настоящей стартовой фазой")
    print("=" * 96)
    for key, ttl in (("full", "Ψ полная"), ("nosunmoon", "Ψ без Солнца и Луны")):
        m, s, n, _, r = ctrl[key]
        print(f"  {ttl:22s}: медиана {m:+.4f}  знак {s}/{n}  блок {r:.1f} дн   "
              f"(настоящее небо: {real[key][0]:+.4f}, {real[key][1]}/{real[key][2]})")
    print("  читается: сколько эффекта переживает потерю ретроградностей и")
    print("  неравномерности хода при СОХРАНЁННЫХ периодах и фазах.")

    # ══════════════════════════════════════════════════════════════════
    # C · ДИАГНОЗ: КАКОЕ ТЕЛО ЧТО ДЕРЖИТ
    # Сдвигаем по очереди ТОЛЬКО ОДНО тело, остальные девять настоящие.
    # Развёртка детерминированная (шаг 10°), сам ноль исключён из нуля.
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 96)
    print("C · ПОТЕЛЬНАЯ РАЗВЁРТКА: сдвинуто ТОЛЬКО одно тело, девять настоящие")
    print("=" * 96)
    print(f"{'тело':10s} {'Ψ полная: медиана нуля':>24s} {'размах':>18s} "
          f"{'p':>7s} | {'Ψ без С/Л: медиана':>20s} {'p':>7s}")
    body_p = {}
    for nm in aether._BODY_ORDER:
        res = {"full": [], "nosunmoon": []}
        for ph in np.arange(10.0, 360.0, 10.0):
            st = stat(D, make_shift({b: (ph if b == nm else 0.0)
                                     for b in aether._BODY_ORDER}))
            for k in res:
                res[k].append(st[k][0])
        pf = _pv(res["full"], real["full"][0], len(res["full"]))
        pn = _pv(res["nosunmoon"], real["nosunmoon"][0], len(res["nosunmoon"]))
        body_p[nm] = (pf, pn)
        mf = np.array(res["full"])
        if nm == "Солнце":
            mf_sun = mf
        print(f"{nm:10s} {np.median(mf):+24.4f} "
              f"{f'[{mf.min():+.4f}…{mf.max():+.4f}]':>18s} {pf:7.4f} | "
              f"{np.median(res['nosunmoon']):+20.4f} {pn:7.4f}")

    # тонкая развёртка по Солнцу — это и есть нуль «сдвинуть календарь»
    fine = [stat(D, make_shift({b: (ph if b == "Солнце" else 0.0)
                                for b in aether._BODY_ORDER}))["full"][0]
            for ph in np.arange(2.0, 360.0, 2.0)]
    p_sun = _pv(fine, real["full"][0], len(fine))
    print(f"\n  10 тел × 2 версии = 20 проверок; при 35 сдвигах минимальное "
          f"возможное p = 0.0278,\n  и по случайности таких ожидается ~0.6 "
          f"штуки. Смотреть надо не на p, а на РАЗМЕР\n  падения: у Солнца "
          f"{real['full'][0]:+.4f}→{np.median(mf_sun):+.4f}, у всех прочих "
          f"десятые доли того же.")
    print(f"\n  тонкая развёртка Солнца (179 сдвигов по 2° ≈ сдвиг календаря):")
    print(f"    настоящее {real['full'][0]:+.4f} против медианы нуля "
          f"{np.median(fine):+.4f}, размах [{min(fine):+.4f}…{max(fine):+.4f}]")
    print(f"    p = {p_sun:.4f}  (настоящее выше "
          f"{float((np.array(fine) < real['full'][0]).mean())*100:.1f}% сдвигов)")
    e_sun = D[max(D, key=lambda k: len(D[k]['ts']))]["E"]["Солнце"]
    print(f"    подмена чистая: E Солнца при этом не трогается и сама по себе "
          f"постоянна\n    (разброс {100*e_sun.std()/e_sun.mean():.2f}% от "
          f"среднего) — двигается ровно календарная фаза.")
    fz = np.asarray(fine) - np.mean(fine)
    lag = next((k for k in range(1, 60)
                if float(np.corrcoef(fz, np.roll(fz, k))[0, 1]) < 0.5), 60)
    print(f"    ЧЕСТНО ПРО ЭТО p: 179 сдвигов НЕ независимы — кривая "
          f"декоррелирует за {lag*2}°,\n    то есть независимых сдвигов ~"
          f"{360/(lag*2):.0f}, и честное p тут порядка "
          f"{1/(360/(lag*2)+1):.2f}, а не {p_sun:.4f}.")

    # опорная точка: чистый сезон вместо Ψ
    sv, sp = [], 0
    for tk in D:
        e = effect(np.cos(2 * np.pi * D[tk]["doy"] / 365.25), D[tk]["ar"])
        if e:
            sv.append(e[0]); sp += 1 if e[0] > 0 else 0
    print(f"  для сравнения — ЧИСТЫЙ СЕЗОН cos(2π·день года/365.25) вместо Ψ: "
          f"медиана {np.median(sv):+.4f}, знак {sp}/{len(sv)}")

    # ── приговор
    key = "nosunmoon"
    pa = _pv(rows["A"][key]["med"], real[key][0], NREAL)
    pb = _pv(rows["B"][key]["med"], real[key][0], NREAL)
    sa = _pv(rows["A"][key]["sign"], real[key][1], NREAL)
    sb = _pv(rows["B"][key]["sign"], real[key][1], NREAL)
    print("\n" + "=" * 96)
    print("ПРИГОВОР (по Ψ без Солнца и Луны — там жил эффект)")
    print("=" * 96)
    print(f"  A (взаимные положения разрушены): p(вел.)={pa:.4f}  p(знак)={sa:.4f}")
    print(f"  B (чужие периоды):                p(вел.)={pb:.4f}  p(знак)={sb:.4f}")
    if max(pa, pb) < 0.05 and max(sa, sb) < 0.05:
        print("  → НЕБО. Обе подмены внутри родной формулы убивают эффект:")
        print("    дело не в классе функций, а в конкретных долготах.")
    elif min(pa, pb) > 0.10 and min(sa, sb) > 0.10:
        print("  → ФОРМА. Любое небо в этой формуле даёт то же самое:")
        print("    эффект — свойство конструкции Ψ, а не положений тел.")
    else:
        print("  → РАСЩЕПЛЕНИЕ. Один способ бьёт, другой нет — читай числа "
              "выше по каждому отдельно, общего вывода нет.")
    fa = _pv(rows["A"]["full"]["med"], real["full"][0], NREAL)
    fb = _pv(rows["B"]["full"]["med"], real["full"][0], NREAL)
    print(f"\n  Отдельно по Ψ ПОЛНОЙ (Солнце и Луна внутри) подмена ЛОМАЕТ "
          f"эффект: p(A)={fa:.4f}, p(B)={fb:.4f}.")
    print("  Но развёртка C показывает, кто это держит: сдвиг ОДНОГО Солнца "
          f"роняет\n  эффект с {real['full'][0]:+.4f} до медианы "
          f"{np.median(fine):+.4f} (p={p_sun:.4f}), а сдвиг любого другого тела")
    print("  не меняет почти ничего. Долгота Солнца — это день года и ничего")
    print("  больше. То есть уцелел не «эфир», а КАЛЕНДАРНАЯ СЕЗОННОСТЬ, и она")
    print("  живёт в самом быстром, солнечном члене — при 13 годах данных это")
    print("  ~13 независимых циклов и 2 независимые волны Ψ на 8 бумаг.")

    json.dump({"real": {k: [real[k][0], real[k][1], real[k][2],
                            list(map(float, real[k][3])), real[k][4]]
                        for k in real},
               "ctrl_uniform_real": {k: [ctrl[k][0], ctrl[k][1], ctrl[k][4]]
                                     for k in ctrl},
               "periods": per, "nreal": NREAL,
               "body_sweep_p": body_p, "sun_fine": fine, "p_sun": p_sun,
               "season": [float(np.median(sv)), sp],
               "A": {k: dict(rows["A"][k]) for k in rows["A"]},
               "B": {k: dict(rows["B"][k]) for k in rows["B"]}},
              open(OUT, "w", encoding="utf-8"))
    print(f"\nсырые распределения: {OUT}   ({time.time()-t_start:.0f} с)")


# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # ── self-тест: машинерия обязана быть той же, что в продукте ──────
    D = load()
    assert len(D) == 8, f"ожидалось 8 бумаг, пришло {len(D)}"
    tk = sorted(D)[0]
    lam, E, lam0, band = D[tk]["lam"], D[tk]["E"], D[tk]["lam0"], D[tk]["band"]

    # 1. быстрый digitize даёт БАЙТ-В-БАЙТ то же, что родной argmin
    for nm in aether._BODY_ORDER:
        d = np.abs(aether._wrap180(lam[nm][:, None] - lam0))
        assert np.array_equal(_k_of(d, True), _k_of(d, False)), nm
    p_fast = sum(psi_parts_native(lam, E, lam0, band, True).values())
    p_slow = sum(psi_parts_native(lam, E, lam0, band, False).values())
    assert np.array_equal(p_fast, p_slow), "digitize ≠ argmin в сумме"

    # 2. наша сырая Ψ — это ровно продуктовая psi_of до нормировки
    nat = aether.natal_lons(D[tk]["genesis"])
    prod = aether.psi_of({"ts": D[tk]["ts"], "lam": lam, "E": E}, nat, band)
    assert np.allclose(prod, aether._norm01(p_fast), atol=1e-12), \
        "сырая Ψ не сводится к продуктовой psi_of"

    # 3. подмена «сдвиг на 0°» и «сдвиг на 360°» обязана быть тождеством
    z = {nm: 0.0 for nm in aether._BODY_ORDER}
    f360 = {nm: 360.0 for nm in aether._BODY_ORDER}
    r0 = stat(D, lam_real)
    assert stat(D, make_shift(z))["nosunmoon"][:3] == r0["nosunmoon"][:3]
    assert np.allclose(sum(psi_parts_native(make_shift(f360)(D, tk, None),
                                            E, lam0, band).values()),
                       p_fast, atol=1e-9), "сдвиг 360° не тождествен"

    # 4. настоящий эффект — тот самый, что записан в журнале (знак и порядок)
    m_nsm, s_nsm, n_nsm, _, rl_nsm = r0["nosunmoon"]
    assert n_nsm == 8 and s_nsm == 8, f"единодушие потеряно: {s_nsm}/{n_nsm}"
    assert 0.02 < m_nsm < 0.08, f"медиана эффекта уехала: {m_nsm}"
    assert rl_nsm > 10.0, f"волна без Солнца обязана быть медленной: {rl_nsm}"

    # 5. подмена ДЕЙСТВИТЕЛЬНО меняет Ψ (иначе тест был бы пустым)
    sh = make_shift({nm: 137.0 for nm in aether._BODY_ORDER})
    p_sh = sum(psi_parts_native(sh(D, tk, None), E, lam0, band).values())
    assert not np.allclose(p_sh, p_fast, atol=1e-6), "сдвиг ничего не изменил"
    assert abs(np.corrcoef(p_sh, p_fast)[0, 1]) < 0.9, "сдвиг слишком слаб"

    # 6. равномерное вращение сохраняет период с точностью лучше 1%
    per = eff_periods(D)
    ep = min(D[t]["ts"][0] for t in D)
    u = make_uniform(per, {nm: 0.0 for nm in aether._BODY_ORDER}, ep)(D, tk, None)
    for nm in ("Луна", "Солнце", "Юпитер"):
        L = np.degrees(np.unwrap(np.radians(u[nm])))
        days = (D[tk]["ts"][-1] - D[tk]["ts"][0]) / DAY
        got = days / (abs(L[-1] - L[0]) / 360.0)
        assert abs(got / per[nm] - 1) < 0.01, f"{nm}: {got} vs {per[nm]}"

    # 7. p-значения обязаны лежать в (0,1]
    assert 0 < _pv([0.0, 1.0, 2.0], 1.0, 3) <= 1.0

    # 8. мера медленности обязана различать быструю и медленную волну
    tt = np.arange(2000.0)
    assert runlen(np.cos(2 * np.pi * tt / 2.5)) < 3.0, "быстрая волна не быстрая"
    assert runlen(np.cos(2 * np.pi * tt / 900.0)) > 50.0, "медленная не медленная"
    assert runlen(tt) > 300.0, "монотонный ряд обязан дать длинные блоки"

    # 9. потельная развёртка обязана быть осмысленной: Солнце — самый громкий
    #    член полной Ψ, Плутон — самый тихий (иначе диагноз блока C пустой)
    amp = {nm: float(np.std(v))
           for nm, v in psi_parts_native(lam, E, lam0, band).items()}
    assert max(amp, key=amp.get) == "Солнце", f"громче Солнца: {amp}"
    assert min(amp, key=amp.get) == "Плутон", f"тише Плутона: {amp}"
    assert amp["Солнце"] > 20 * amp["Меркурий"], "Солнце обязано доминировать"

    print("self-тест: 9 блоков ассертов пройдено\n")
    run()
