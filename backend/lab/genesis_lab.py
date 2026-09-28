"""ГЕНЕЗИС ПОД ОГОНЬ: значит ли вообще что-нибудь дата рождения инструмента?

Владелец: «нельзя у нефти считать с её рождения, надо считать с того, что
сейчас». Гипотеза: для фьючерса живой генезис — не запуск инструмента в
1988-м, а старт текущего контракта / экспирация предыдущего / мунданный
якорь биржи.

Проверяем это НЕ на «стало красивее», а перцентилем настоящего генезиса в
распределении СЛУЧАЙНЫХ генезисов. Если канон не бьёт случайные даты —
значит натальные долготы не несут ничего, и спор о правильной дате пустой.

МЕТРИКА — готовая, не изобретённая: эффект «край минус середина» в AR(1)
доходностей за 20 дней (backend.lab.ar_nulls: ar_series, effect) на СЫРОЙ Ψ
БЕЗ СОЛНЦА И ЛУНЫ (механика — backend.lab.psi_split: psi_parts; там же
показано, как Ψ строится от произвольного генезиса). Это единственный
след, переживший эпохи, чужую волну и 200 циклических сдвигов
(медиана +0.0403, p<0.01 у 7 бумаг из 8).

ЧТО ВАЖНО ЗНАТЬ ПРО ВЫБОРКУ: из 8 бумаг MOEX только у SBER свой генезис в
реестре (2007-07-20). Остальные 7 падают в фолбэк MOEX_MUNDANE
(2011-12-19) — то есть «8 бумаг» жили всего на ДВУХ разных генезисах.

КАНДИДАТЫ (band у всех берётся канонный для бумаги — варьируем ТОЛЬКО дату):
  1) канон школы (SBER 2007-07-20 / остальные — мунданный якорь MOEX);
  2) мунданный якорь MOEX 2011-12-19;
  3) канон нефти BR 1988-06-23 (первый день торгов Brent на ICE);
  4) старт текущего контракта BRU6 — 2025-08-01. Обоснование: BRU6 = Brent
     сентября 2026 (MOEX BR-9.26), экспирация привязана к ICE Brent Sep-26
     (последний рабочий день июля 2026, ≈2026-07-31); «примерно за год до
     экспирации» → 2025-08-01. Дата оценочная 🟡, названа прямо;
  5) экспирация предыдущего контракта BRQ6 (Brent августа 2026, ICE
     ≈2026-06-30) 🟡;
  6) первая дата в данных самой бумаги;
  7) ЖИВОЙ СКОЛЬЗЯЩИЙ генезис: на каждый день натал берётся за t−365 и
     t−90 суток (буквальное «считать с того, что сейчас»);
  8) КОНТРОЛЬ-НУЛЬ: 20 случайных дат 1970-2020 (rng с фиксированным
     семенем — это стенд, не продукт) и 20 случайных лагов для скользящего.

python3 -m backend.lab.genesis_lab
"""
import json
from datetime import date, datetime, timedelta, timezone

import numpy as np

from backend import aether
# АРХИВНЫЙ СТЕНД: воспроизводит СТАРУЮ формулу Ψ со спектральным сродством.
# Из боевого aether AFFINITY удалён (v3.7, вердикт мировоззрения 05.08.2026:
# «подгонка под ответ») — здесь константа оставлена локально, чтобы стенд
# продолжал воспроизводить ровно то, что было измерено в июле 2026.
AFFINITY_ARCHIVE = 1.5

from backend.lab.ar_nulls import ar_series, effect

HIST = "/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/moex_hist.json"
OUT = "/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/genesis_lab.json"
SKIP = ("Солнце", "Луна")        # что выкидываем из транзитной суммы
N_RAND = 20                      # размер нуля
SEED = 20260731
RAND_FROM = date(1970, 1, 1)
RAND_TO = date(2020, 12, 31)
ROLL_LAGS = (365, 90)            # «живой» генезис: натал за t−lag суток


# ══════════════════════════════════════════════════════════════════════
# Ψ от произвольного генезиса (формула ядра не тронута — повтор psi_parts)
# ══════════════════════════════════════════════════════════════════════
def grid_for(dates):
    """Транзитная сетка по дням, как в psi_parts: t0 = первый день 12:00 UTC."""
    t0 = datetime(dates[0].year, dates[0].month, dates[0].day, 12,
                  tzinfo=timezone.utc)
    return aether.transit_series(t0, (dates[-1] - dates[0]).days * 24 + 24, 24.0)


def natal_lam(genesis):
    """Натальные долготы 10 тел как строка (1,10) или None."""
    nat = aether.natal_lons(tuple(int(v) for v in genesis))
    if nat is None:
        return None
    return np.array([r["lon"] for r in nat], float)[None, :]


def psi_body(tr, nm, lam0, band, unit_e=False):
    """Вклад одного транзитного тела в сырую Ψ (та же формула, что в ядре).
    unit_e=True — E≡1: остаётся чистая аспектная геометрия натала."""
    d = np.abs(aether._wrap180(tr["lam"][nm][:, None] - lam0))
    k = aether._ASP_K[np.argmin(
        np.abs(d[:, :, None] - aether._ASP_ANG[None, None, :]), axis=2)]
    B = AFFINITY_ARCHIVE if aether.W_VEDIC[nm] == band else 1.0
    e = np.ones(len(tr["ts"])) if unit_e else tr["E"][nm]
    return np.sum(e[:, None] * B * np.cos(np.radians(k * d)), axis=1) / 100.0


def psi_raw(tr, lam0, band, skip=SKIP):
    """Сырая Ψ (без min-max) с выброшенными транзитными телами skip."""
    tot = np.zeros(len(tr["ts"]))
    for nm in aether._BODY_ORDER:
        if nm in skip:
            continue
        tot += psi_body(tr, nm, lam0, band)
    return tot


def psi_env(tr, band, skip=SKIP):
    """ОГИБАЮЩАЯ БЕЗ НАТАЛА ВООБЩЕ: cos(kΔ) заменён на 1, остаётся Σ E_b(t)·B.
    Генезис в этой величине не участвует ни в каком виде."""
    tot = np.zeros(len(tr["ts"]))
    for nm in aether._BODY_ORDER:
        if nm in skip:
            continue
        B = AFFINITY_ARCHIVE if aether.W_VEDIC[nm] == band else 1.0
        tot += tr["E"][nm] * B * 10.0 / 100.0
    return tot


def psi_geom(tr, lam0, band, skip=SKIP):
    """ТОЛЬКО ГЕОМЕТРИЯ НАТАЛА: E≡1, остаётся сумма cos(kΔ) — вклад генезиса
    в чистом виде, без энергетической огибающей."""
    tot = np.zeros(len(tr["ts"]))
    for nm in aether._BODY_ORDER:
        if nm in skip:
            continue
        tot += psi_body(tr, nm, lam0, band, unit_e=True)
    return tot


def psi_rolling(tr, band, lag, skip=SKIP):
    """ЖИВОЙ генезис: на день t натал берётся с той же сетки за t−lag суток.
    Первые lag дней натала нет → NaN (метрика их сама маскирует)."""
    L = np.stack([tr["lam"][nm] for nm in aether._BODY_ORDER], axis=1)
    Lsh = np.full_like(L, np.nan)
    Lsh[lag:] = L[:-lag]
    tot = np.zeros(len(tr["ts"]))
    for nm in aether._BODY_ORDER:
        if nm in skip:
            continue
        d = np.abs(aether._wrap180(tr["lam"][nm][:, None] - Lsh))
        with np.errstate(invalid="ignore"):
            k = aether._ASP_K[np.argmin(
                np.abs(d[:, :, None] - aether._ASP_ANG[None, None, :]), axis=2)]
        B = AFFINITY_ARCHIVE if aether.W_VEDIC[nm] == band else 1.0
        tot = tot + np.sum(tr["E"][nm][:, None] * B * np.cos(np.radians(k * d)),
                           axis=1) / 100.0
    return tot


def pct(real, nulls):
    """Перцентиль настоящего значения в распределении нулей, %."""
    a = np.asarray(nulls, float)
    a = a[np.isfinite(a)]
    if a.size == 0 or not np.isfinite(real):
        return float("nan")
    return float((a < real).mean() * 100.0)


# ══════════════════════════════════════════════════════════════════════
def rand_dates(n, seed=SEED):
    """Детерминированный нуль: n дат равномерно в диапазоне (стенд, не продукт)."""
    rng = np.random.default_rng(seed)
    lo, hi = RAND_FROM.toordinal(), RAND_TO.toordinal()
    return [date.fromordinal(int(o)) for o in rng.integers(lo, hi + 1, n)]


def candidates(tk, first):
    """Кандидаты генезиса для бумаги. band всегда канонный — варьируем дату."""
    g = aether.genesis_for(tk)
    return [
        ("канон школы", tuple(g["genesis"])),
        ("мунданный MOEX 2011-12-19", (2011, 12, 19, 12, 0, 0)),
        ("канон нефти BR 1988-06-23", (1988, 6, 23, 12, 0, 0)),
        ("старт контракта BRU6 2025-08-01", (2025, 8, 1, 12, 0, 0)),
        ("экспирация пред. BRQ6 2026-06-30", (2026, 6, 30, 12, 0, 0)),
        ("первая дата данных бумаги", (first.year, first.month, first.day, 12, 0, 0)),
    ]


def run():
    raw = json.load(open(HIST, encoding="utf-8"))
    tks = sorted(raw)
    rnd = rand_dates(N_RAND)

    print("=" * 100)
    print("ГЕНЕЗИС ПОД ОГОНЬ · метрика: край−середина AR(1) на сырой Ψ без Солнца и Луны")
    print("=" * 100)
    print("Реестр генезисов по бумагам (что стоит сейчас):")
    for tk in tks:
        g = aether.genesis_for(tk)
        print(f"  {tk:5s} {'-'.join(str(v) for v in g['genesis'][:3]):12s} "
              f"band={g['band']:3d}  key={g['key']:13s} "
              f"{'ФОЛБЭК' if g['fallback'] else 'реестр'}")

    # ── подготовка: цены, AR(1), транзитные сетки (общие для одинаковых дат)
    tick, grids = {}, {}
    for tk in tks:
        rows = raw[tk]
        ds = [date.fromisoformat(r[0]) for r in rows]
        cl = np.array([float(r[4]) for r in rows])
        key = (ds[0], ds[-1])
        if key not in grids:
            tr = grid_for(ds)
            pmap = {datetime.fromtimestamp(t, timezone.utc).date(): i
                    for i, t in enumerate(tr["ts"])}
            grids[key] = (tr, pmap)
        tr, pmap = grids[key]
        idx = np.array([pmap.get(d, -1) for d in ds])
        tick[tk] = dict(ds=ds, ar=ar_series(cl), key=key, idx=idx,
                        band=aether.genesis_for(tk)["band"])
    print(f"\nсеток транзитов: {len(grids)} (бумаги с одинаковым диапазоном "
          f"дат делят сетку); бумаг: {len(tks)}")

    def map_eff(tk, p):
        """Эффект по готовому дневному ряду Ψ (сажаем на торговые даты)."""
        t = tick[tk]
        pv = np.where(t["idx"] >= 0, p[t["idx"]], np.nan)
        return effect(pv, t["ar"]), pv

    def eff_for(tk, lam0):
        """Эффект для бумаги при заданных натальных долготах."""
        t = tick[tk]
        tr, _ = grids[t["key"]]
        return map_eff(tk, psi_raw(tr, lam0, t["band"]))

    # ── НУЛЬ: 20 случайных генезисов
    print("\n" + "=" * 100)
    print(f"НУЛЬ · {N_RAND} случайных генезисов {RAND_FROM}…{RAND_TO}")
    print("=" * 100)
    null_per_tk = {tk: [] for tk in tks}
    null_med, null_sign, null_corr = [], [], []
    canon_psi = {}
    for tk in tks:
        lam0 = natal_lam(aether.genesis_for(tk)["genesis"])
        _, canon_psi[tk] = eff_for(tk, lam0)
    for rd in rnd:
        lam0 = natal_lam((rd.year, rd.month, rd.day, 12, 0, 0))
        vals = []
        for tk in tks:
            e, pv = eff_for(tk, lam0)
            v = e[0] if e else np.nan
            null_per_tk[tk].append(v)
            vals.append(v)
            m = np.isfinite(pv) & np.isfinite(canon_psi[tk])
            null_corr.append(float(np.corrcoef(pv[m], canon_psi[tk][m])[0, 1]))
        vals = np.array(vals, float)
        null_med.append(float(np.nanmedian(vals)))
        null_sign.append(int(np.nansum(vals > 0)))
    null_med = np.array(null_med)
    print(f"  медианы эффекта по 8 бумагам для 20 случайных дат:")
    print("   ", "  ".join(f"{v:+.4f}" for v in np.sort(null_med)))
    print(f"  разброс нуля: мин {null_med.min():+.4f}  медиана "
          f"{np.median(null_med):+.4f}  макс {null_med.max():+.4f}  "
          f"σ {null_med.std(ddof=1):.4f}")
    print(f"  знак «край>середина» у случайных: медиана {np.median(null_sign):.1f}/8, "
          f"минимум {min(null_sign)}/8, максимум {max(null_sign)}/8")
    print(f"  корреляция Ψ(случайный генезис) с Ψ(канон): медиана "
          f"{np.median(null_corr):+.3f}, |r| медиана {np.median(np.abs(null_corr)):.3f}, "
          f"размах {min(null_corr):+.3f}…{max(null_corr):+.3f}")

    # ── КАНДИДАТЫ
    print("\n" + "=" * 100)
    print("КАНДИДАТЫ ГЕНЕЗИСА · эффект (медиана по 8 бумагам) и перцентиль среди 20 случайных")
    print("=" * 100)
    print(f"{'генезис':38s} {'эффект':>9s} {'знак':>6s} {'перц.':>7s}  "
          f"{'p<0.01':>6s}")
    table, per_ticker = [], {}
    names = [n for n, _ in candidates(tks[0], tick[tks[0]]["ds"][0])]
    for ci, nm in enumerate(names):
        ds_row, ps_row, pcs = [], [], []
        for tk in tks:
            gen = candidates(tk, tick[tk]["ds"][0])[ci][1]
            e, _ = eff_for(tk, natal_lam(gen))
            d = e[0] if e else np.nan
            p = e[1] if e else np.nan
            ds_row.append(d)
            ps_row.append(p)
            pcs.append(pct(d, null_per_tk[tk]))
            per_ticker.setdefault(tk, {})[nm] = dict(
                d=None if not np.isfinite(d) else round(float(d), 5),
                p=None if not np.isfinite(p) else round(float(p), 5),
                pct=round(pcs[-1], 1), genesis=list(gen[:3]))
        d_arr, p_arr = np.array(ds_row), np.array(ps_row)
        med = float(np.nanmedian(d_arr))
        pc_med = pct(med, null_med)
        print(f"{nm:38s} {med:+9.4f} {int(np.nansum(d_arr>0)):4d}/8 "
              f"{pc_med:6.1f}% {int(np.nansum(p_arr<0.01)):5d}/8")
        table.append(dict(name=nm, med=round(med, 5),
                          sign=int(np.nansum(d_arr > 0)),
                          pct_med=round(pc_med, 1),
                          n_p01=int(np.nansum(p_arr < 0.01)),
                          pct_per_ticker=[round(x, 1) for x in pcs]))

    # ── ГДЕ ЖИВЁТ ЭФФЕКТ: огибающая E(t) без натала против чистой геометрии
    print("\n" + "=" * 100)
    print("РАЗБОР Ψ · что в ней вообще зависит от генезиса")
    print("=" * 100)
    decomp = {}
    for nm2, fn in (("огибающая Σ E(t), натала нет", "env"),
                    ("только геометрия натала (E≡1)", "geom")):
        ds_row, corr = [], []
        for tk in tks:
            t = tick[tk]
            tr, _ = grids[t["key"]]
            if fn == "env":
                p = psi_env(tr, t["band"])
            else:
                p = psi_geom(tr, natal_lam(aether.genesis_for(tk)["genesis"]),
                             t["band"])
            e, pv = map_eff(tk, p)
            ds_row.append(e[0] if e else np.nan)
            m = np.isfinite(pv) & np.isfinite(canon_psi[tk])
            corr.append(float(np.corrcoef(pv[m], canon_psi[tk][m])[0, 1]))
        d_arr = np.array(ds_row, float)
        med = float(np.nanmedian(d_arr))
        print(f"  {nm2:32s} эффект {med:+.4f}  знак "
              f"{int(np.nansum(d_arr>0))}/8  перцентиль среди случайных "
              f"генезисов {pct(med, null_med):5.1f}%  r с канонной Ψ "
              f"{np.median(corr):+.3f}")
        decomp[nm2] = dict(med=round(med, 5), sign=int(np.nansum(d_arr > 0)),
                           pct=round(pct(med, null_med), 1),
                           r_canon=round(float(np.median(corr)), 4))

    # ── ЧТО ЖЕ ТАКОЕ ЭТА ОГИБАЮЩАЯ (раз генезис в ней не участвует)
    tk0 = tks[0]
    tr0, _ = grids[tick[tk0]["key"]]
    env0 = psi_env(tr0, tick[tk0]["band"])
    doy = np.array([datetime.fromtimestamp(t, timezone.utc).timetuple().tm_yday
                    for t in tr0["ts"]], float)
    print("\n  из чего сделана огибающая (доля дисперсии по телам, r с Σ E):")
    contrib = {}
    for nm in aether._BODY_ORDER:
        if nm in SKIP:
            continue
        B = AFFINITY_ARCHIVE if aether.W_VEDIC[nm] == tick[tk0]["band"] else 1.0
        e = tr0["E"][nm] * B * 10.0 / 100.0
        contrib[nm] = (float(np.std(e) / np.std(env0)),
                       float(np.corrcoef(e, env0)[0, 1]))
    for nm, (s, r) in sorted(contrib.items(), key=lambda kv: -kv[1][0]):
        print(f"    {nm:9s} σ/σ(Σ) = {s:5.2f}   r = {r:+.3f}")
    r_year = float(np.corrcoef(env0, np.cos(2 * np.pi * doy / 365.25))[0, 1])
    r_half = float(np.corrcoef(env0, np.cos(2 * np.pi * doy / 182.625))[0, 1])
    print(f"    r(огибающая, годовая гармоника) = {r_year:+.3f}; "
          f"полугодовая = {r_half:+.3f}")

    # ── ЖИВОЙ СКОЛЬЗЯЩИЙ ГЕНЕЗИС + свой нуль (случайные лаги)
    print("\n" + "=" * 100)
    print("ЖИВОЙ СКОЛЬЗЯЩИЙ ГЕНЕЗИС · натал на каждый день берётся за t−lag суток")
    print("=" * 100)
    rng = np.random.default_rng(SEED + 1)
    lag_null = sorted(int(x) for x in rng.integers(30, 1500, N_RAND))

    def roll_med(lag):
        vals = []
        for tk in tks:
            t = tick[tk]
            tr, _ = grids[t["key"]]
            p = psi_rolling(tr, t["band"], lag)
            pv = np.where(t["idx"] >= 0, p[t["idx"]], np.nan)
            e = effect(pv, t["ar"])
            vals.append(e[0] if e else np.nan)
        return np.array(vals, float)

    roll_null = np.array([float(np.nanmedian(roll_med(l))) for l in lag_null])
    print(f"  нуль скользящего (20 случайных лагов 30…1500 дн): "
          f"мин {roll_null.min():+.4f}  медиана {np.median(roll_null):+.4f}  "
          f"макс {roll_null.max():+.4f}")
    roll_rows = []
    for lag in ROLL_LAGS:
        v = roll_med(lag)
        med = float(np.nanmedian(v))
        pc = pct(med, roll_null)
        print(f"  lag={lag:4d} дн: эффект {med:+.4f}  знак "
              f"{int(np.nansum(v>0))}/8  перцентиль среди случайных лагов {pc:.1f}%")
        roll_rows.append(dict(lag=lag, med=round(med, 5),
                              sign=int(np.nansum(v > 0)), pct=round(pc, 1)))

    # ── ВЫВОД
    print("\n" + "=" * 100)
    print("ЧТЕНИЕ")
    print("=" * 100)
    canon = table[0]
    print(f"  канон школы: {canon['med']:+.4f}, перцентиль среди случайных "
          f"{canon['pct_med']:.1f}%")
    if canon["pct_med"] >= 95.0:
        print("  → канон БЬЁТ случайные генезисы: дата рождения несёт сигнал.")
    else:
        print("  → канон НЕ бьёт случайные генезисы. Дата рождения инструмента "
              "не отличима от произвольной даты: эффект живёт не в натале.")
    best = max(table, key=lambda r: r["med"])
    print(f"  сильнейший кандидат по величине: «{best['name']}» {best['med']:+.4f} "
          f"(перцентиль {best['pct_med']:.1f}%) — величина без перцентиля "
          f"ничего не значит.")
    json.dump(dict(table=table, per_ticker=per_ticker, roll=roll_rows,
                   decomp=decomp,
                   env_bodies={k: [round(v[0], 4), round(v[1], 4)]
                               for k, v in contrib.items()},
                   env_season=dict(year=round(r_year, 4), half=round(r_half, 4)),
                   null_med=[round(float(v), 5) for v in null_med],
                   roll_null=[round(float(v), 5) for v in roll_null],
                   lag_null=lag_null,
                   null_dates=[d.isoformat() for d in rnd],
                   null_corr_abs_med=round(float(np.median(np.abs(null_corr))), 4)),
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"→ {OUT}")


# ══════════════════════════════════════════════════════════════════════
def _selftest():
    """Настоящие assert: проверяем величины, а не факт запуска."""
    ds = [date(2013, 3, 25) + timedelta(days=i) for i in range(400)]
    tr = grid_for(ds)
    # сетка как в psi_parts: сутки от первой даты + один запасной день
    assert len(tr["ts"]) == len(ds) + 1, len(tr["ts"])

    lam_a = natal_lam((2007, 7, 20, 12, 0, 0))
    lam_b = natal_lam((1988, 6, 23, 12, 0, 0))
    assert lam_a is not None and lam_a.shape == (1, 10)
    assert np.abs(lam_a - lam_b).max() > 1.0        # разные наталы — разные λ

    # 1. выброшены ровно Солнце и Луна: часть + Солнце + Луна = полная Ψ
    full = psi_raw(tr, lam_a, 54, skip=())
    nosm = psi_raw(tr, lam_a, 54)
    s = psi_body(tr, "Солнце", lam_a, 54)
    m = psi_body(tr, "Луна", lam_a, 54)
    assert np.abs(full - (nosm + s + m)).max() < 1e-9

    # 2. Ψ детерминирована (никакого random) и реально зависит от генезиса
    assert np.array_equal(nosm, psi_raw(tr, lam_a, 54))
    other = psi_raw(tr, lam_b, 54)
    assert np.abs(nosm - other).max() > 0.05, np.abs(nosm - other).max()

    # 3. band реально меняет Ψ (AFFINITY у тел своей ноты)
    assert np.abs(psi_raw(tr, lam_a, 108) - nosm).max() > 0.05

    # 4. скользящий генезис: ровно lag NaN в начале, дальше конечные числа
    pr = psi_rolling(tr, 54, 90)
    assert np.isnan(pr[:90]).all() and np.isfinite(pr[90:]).all()
    assert np.abs(pr[90:] - nosm[90:]).max() > 0.05   # это не то же самое

    # 5. огибающая не зависит от генезиса, геометрия — зависит
    assert np.array_equal(psi_env(tr, 54), psi_env(tr, 54))
    g_a, g_b = psi_geom(tr, lam_a, 54), psi_geom(tr, lam_b, 54)
    assert np.abs(g_a - g_b).max() > 0.01, np.abs(g_a - g_b).max()
    # огибающая — верхняя грань |Ψ|: |cos| ≤ 1 при том же E
    assert psi_env(tr, 54).min() > 0.0
    assert np.abs(nosm).max() <= psi_env(tr, 54).max() + 1e-9

    # 6. перцентиль считается как надо
    assert pct(5.0, [1, 2, 3, 4]) == 100.0
    assert pct(0.0, [1, 2, 3, 4]) == 0.0
    assert pct(2.5, [1, 2, 3, 4]) == 50.0
    assert np.isnan(pct(float("nan"), [1, 2]))

    # 7. нуль детерминирован при том же семени и лежит в диапазоне
    r1, r2 = rand_dates(N_RAND), rand_dates(N_RAND)
    assert r1 == r2 and len(r1) == N_RAND
    assert all(RAND_FROM <= d <= RAND_TO for d in r1)

    # 8. метрика честно отказывается на коротком ряде
    assert effect(np.arange(50.0), np.arange(50.0)) is None
    print("selftest genesis_lab: ok")


if __name__ == "__main__":
    _selftest()
    run()
