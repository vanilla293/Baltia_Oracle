"""ОТДЕЛЯЕМ «ЧАСТЬ ПРО ФРС» ОТ НЕФТИ.

Вопрос владельца: цену нефти двигают решения ФРС, сделки ОПЕК+ и отчёты
EIA по запасам. Если вычесть эту макро-компоненту, станет ли остаток
чище — и проявится ли на нём эфирный слой лучше, чем на сыром ряде?

Что здесь делается:
  1. Дневная история Brent (две независимые серии: спот EIA/FRED с 1987 и
     фронтальный фьючерс ICE с 2007) — только из локального файла,
     скачанного заранее модулем oil_fetch. Сети в рантайме нет.
  2. Разделение дисперсии: сколько «энергии» Σr² приходится на макро-дни
     (окно вокруг FOMC / ОПЕК+ / EIA) и сколько остаётся на тихих днях.
  3. Тот же тест, что дал единственный выживший след на акциях: инерция
     AR(1) доходностей за 20 дней в СЕРЕДИННОЙ корзине сырой Ψ ниже, чем
     на краях. Ψ — без Солнца и Луны (psi_split), генезис BR.
     Считается на трёх рядах: полном, очищенном вычитанием макро и на
     подряде из одних тихих дней.
  4. Нуль: случайные циклические сдвиги Ψ на ОЧИЩЕННОМ ряде. Сдвиг рвёт
     привязку ко времени, сохраняя медленность волны и блочность корзин.

Две оговорки, без которых числа врут:
  · окно ±1 вокруг ЕЖЕНЕДЕЛЬНОЙ среды EIA съедает вт-ср-чт, и «тихими»
    остаются понедельник с пятницей. Это уже не «чистый рынок», а эффект
    дня недели. Поэтому основной набор — ТЕСНЫЙ (EIA только сам день), а
    ШИРОКИЙ (EIA ±1) считается рядом как проверка.
  · календарь ОПЕК+ честно обрывается на 2024-12-31 (macro_calendar),
    поэтому основное окно замера — 2013-01-01 … 2024-12-31.

python3 -m backend.lab.oil_frs
"""
import json
import os
from datetime import date, datetime, timezone

import numpy as np
from scipy import stats

from backend.lab import macro_calendar as mc
from backend.lab.ar_nulls import AR_WIN, effect
from backend.lab.psi_split import psi_parts

OIL = "/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/brent_daily.json"
PSI_CACHE = "/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/oil_psi_cache.npz"
OUT = "/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/oil_frs.json"

GENESIS_DAY = date(1988, 6, 24)     # раньше генезиса BR волны нет по смыслу
NSHIFT = 400
RNG = np.random.default_rng(20260731)


# ══════════════════════════════════════════════════════════════════════
# Данные
# ══════════════════════════════════════════════════════════════════════
def load_oil() -> dict:
    """{'BRENT_SPOT': (dates, close), ...} из локального файла."""
    if not os.path.exists(OIL):
        raise SystemExit("нет ds/brent_daily.json — сначала "
                         "python3 -m backend.lab.oil_fetch")
    blob = json.load(open(OIL, encoding="utf-8"))
    out = {}
    for nm in ("BRENT_SPOT", "BRENT_FUT"):
        rows = [r for r in blob.get(nm, [])
                if date.fromisoformat(r[0]) >= GENESIS_DAY]
        if len(rows) < 500:
            continue
        out[nm] = ([date.fromisoformat(r[0]) for r in rows],
                   np.array([float(r[1]) for r in rows], float))
    return out


def psi_map(d0: date, d1: date) -> dict:
    """{дата: сырая Ψ без Солнца и Луны} для генезиса BR. Кэш на диске —
    расчёт эфемерид за 38 лет не бесплатный, но детерминирован."""
    if os.path.exists(PSI_CACHE):
        z = np.load(PSI_CACHE, allow_pickle=True)
        if str(z["span"]) == f"{d0}|{d1}":
            return {date.fromisoformat(s): float(v)
                    for s, v in zip(z["days"], z["psi"])}
    span = []
    d = d0
    while d <= d1:
        span.append(d)
        d = date.fromordinal(d.toordinal() + 1)
    got = psi_parts(span, "BRU6")
    if not got:
        raise SystemExit("psi_parts вернул None — нет эфемерид")
    ts, parts = got
    p = parts["Ψ без Солнца и Луны"]
    days = [datetime.fromtimestamp(t, timezone.utc).date() for t in ts]
    np.savez(PSI_CACHE, span=f"{d0}|{d1}",
             days=np.array([x.isoformat() for x in days]), psi=np.asarray(p))
    return dict(zip(days, [float(x) for x in p]))


# ══════════════════════════════════════════════════════════════════════
# Макро-разметка
# ══════════════════════════════════════════════════════════════════════
def macro_masks(days, d0: date, d1: date, eia_half: int) -> dict:
    """Булевы маски по дням ряда: FOMC / OPEC / EIA / любой макро."""
    md = mc.macro_days(d0, d1)
    w = {"FOMC": mc.window(md["FOMC"], 1),
         "OPEC": mc.window(md["OPEC"], 1),
         "EIA": mc.window(md["EIA"], eia_half)}
    m = {k: np.array([d in v for d in days], bool) for k, v in w.items()}
    m["ЛЮБОЙ"] = m["FOMC"] | m["OPEC"] | m["EIA"]
    return m


def class_id(masks) -> np.ndarray:
    """Класс дня: 0 тихий, 1 EIA, 2 OPEC, 3 FOMC (приоритет по «громкости»)."""
    c = np.zeros(len(masks["ЛЮБОЙ"]), int)
    c[masks["EIA"]] = 1
    c[masks["OPEC"]] = 2
    c[masks["FOMC"]] = 3
    return c


def variance_split(r: np.ndarray, masks: dict, cls: np.ndarray) -> dict:
    """Сколько дисперсии сидит на макро-днях."""
    tot = float(np.sum(r ** 2))
    out = {"n": int(len(r)), "sigma_дн_%": float(r.std() * 100)}
    for k in ("FOMC", "OPEC", "EIA", "ЛЮБОЙ"):
        m = masks[k]
        out[k] = {
            "доля_дней": float(m.mean()),
            "доля_Σr2": float(np.sum(r[m] ** 2) / tot) if tot > 0 else None,
            "σ_внутри_%": float(r[m].std() * 100) if m.sum() > 2 else None,
            "σ_вне_%": float(r[~m].std() * 100) if (~m).sum() > 2 else None,
        }
        if out[k]["σ_вне_%"] and out[k]["σ_внутри_%"]:
            out[k]["σ_отношение"] = out[k]["σ_внутри_%"] / out[k]["σ_вне_%"]
        else:
            out[k]["σ_отношение"] = None      # пустой класс — честное None
    # доля дисперсии ДОХОДНОСТИ, объяснённая классом (η² по средним) —
    # это «направленный» вклад макро, он всегда мизерный
    m_all = r.mean()
    ss_t = float(np.sum((r - m_all) ** 2))
    ss_b = 0.0
    for c in np.unique(cls):
        s = cls == c
        ss_b += s.sum() * (r[s].mean() - m_all) ** 2
    out["η2_среднее"] = float(ss_b / ss_t) if ss_t > 0 else None
    # доля дисперсии ВОЛАТИЛЬНОСТИ (r²), объяснённая классом
    y = r ** 2
    my = y.mean()
    sst = float(np.sum((y - my) ** 2))
    ssb = sum((cls == c).sum() * (y[cls == c].mean() - my) ** 2
              for c in np.unique(cls))
    out["R2_волатильности"] = float(ssb / sst) if sst > 0 else None
    return out


# ══════════════════════════════════════════════════════════════════════
# AR(1) и очистка
# ══════════════════════════════════════════════════════════════════════
def ar_from_returns(lr: np.ndarray, n_days: int) -> np.ndarray:
    """AR(1) по скользящему окну AR_WIN доходностей, выровнено на дни ряда.
    Тот же конвейер, что в ar_nulls.ar_series, но берёт готовые доходности
    (нужно, чтобы считать AR на ОЧИЩЕННОМ ряде)."""
    ar = np.full(n_days, np.nan)
    for i in range(AR_WIN + 1, len(lr)):
        w = lr[i - AR_WIN:i]
        if w.std() > 0:
            ar[i] = float(np.corrcoef(w[:-1], w[1:])[0, 1])
    return ar


def ar_quiet_subseries(lr: np.ndarray, quiet: np.ndarray,
                       n_days: int) -> np.ndarray:
    """AR(1) по подряду ТОЛЬКО тихих дней.

    Выравнивание такое же, как в ar_nulls.ar_series: доходность lr[j]
    приходит на день j+1, окно из AR_WIN последних тихих доходностей
    приписывается тому дню, на который пришла ПОСЛЕДНЯЯ из них. Заглядывания
    вперёд нет, и значение всегда стоит на тихом дне."""
    idx = np.where(quiet)[0]
    ar = np.full(n_days, np.nan)
    sub = lr[idx]
    for j in range(AR_WIN + 1, len(sub) + 1):
        w = sub[j - AR_WIN:j]
        if w.std() > 0:
            ar[idx[j - 1] + 1] = float(np.corrcoef(w[:-1], w[1:])[0, 1])
    return ar


def clean_returns(r: np.ndarray, cls: np.ndarray) -> np.ndarray:
    """Вычитаем макро: внутри каждого класса убираем среднее и приводим
    разброс к общему. Уходит и направленный сдвиг макро-дня, и его
    повышенная волатильность; календарь при этом не рвётся."""
    out = r.astype(float).copy()
    s_all = r.std()
    for c in np.unique(cls):
        m = cls == c
        if m.sum() < 10:
            continue
        s = r[m].std()
        out[m] = (r[m] - r[m].mean()) / (s if s > 0 else 1.0) * s_all
    return out


# ══════════════════════════════════════════════════════════════════════
# Нуль: случайные циклические сдвиги Ψ
# ══════════════════════════════════════════════════════════════════════
def shift_null(psi: np.ndarray, ar: np.ndarray, n: int = NSHIFT) -> dict:
    e0 = effect(psi, ar)
    if not e0:
        return {"эффект": None, "note": "мало данных"}
    vals = []
    for _ in range(n):
        sh = int(RNG.integers(60, max(61, min(3000, len(psi) - 60))))
        e = effect(np.roll(psi, sh), ar)
        if e:
            vals.append(e[0])
    v = np.array(vals)
    return {"эффект": e0[0], "p_mwu": e0[1], "n_нулей": int(len(v)),
            "нуль_медиана": float(np.median(v)),
            "нуль_p95": float(np.quantile(v, 0.95)),
            "перцентиль": float((v < e0[0]).mean() * 100),
            "p_против_нуля": float((v >= e0[0]).mean())}


# ══════════════════════════════════════════════════════════════════════
# Прогон
# ══════════════════════════════════════════════════════════════════════
def run_series(name, days, close, pm, d0, d1, eia_half, nshift=NSHIFT):
    sel = [i for i, d in enumerate(days) if d0 <= d <= d1 and d in pm]
    days = [days[i] for i in sel]
    close = close[sel]
    if len(days) < 400:
        return None
    psi = np.array([pm[d] for d in days], float)
    lr = np.diff(np.log(close))
    r = np.concatenate([[np.nan], lr])          # выровнено на дни
    rr = r[1:]
    masks = macro_masks(days[1:], d0, d1, eia_half)
    cls = class_id(masks)
    vs = variance_split(rr, masks, cls)

    ar_full = ar_from_returns(lr, len(days))
    lr_clean = clean_returns(lr, cls)
    ar_clean = ar_from_returns(lr_clean, len(days))
    quiet = ~masks["ЛЮБОЙ"]
    ar_quiet = ar_quiet_subseries(lr, quiet, len(days))

    res = {"название": name, "окно": f"{days[0]} … {days[-1]}",
           "дней": len(days), "тихих_дней": int(quiet.sum()),
           "дисперсия": vs}
    res["полный"] = shift_null(psi, ar_full, nshift)
    res["очищенный"] = shift_null(psi, ar_clean, nshift)
    res["только_тихие"] = shift_null(psi, ar_quiet, nshift)
    # для контраста: эффект на макро-днях (AR полного ряда, но корзины
    # строятся только по макро-дням)
    ar_macro = ar_full.copy()
    ar_macro[np.concatenate([[True], quiet])] = np.nan
    e = effect(psi, ar_macro)
    res["только_макро"] = {"эффект": e[0], "p_mwu": e[1]} if e else None
    # состав тихих дней по дням недели — чтобы не выдать артефакт за рынок
    wd = np.array([d.weekday() for d in days[1:]])
    res["тихие_по_дням_недели"] = {
        ["пн", "вт", "ср", "чт", "пт", "сб", "вс"][k]:
        int(((wd == k) & quiet).sum()) for k in range(7)
        if ((wd == k)).sum()}
    res["PLV"] = plv_block(days, close, cls, psi)
    return res


def plv_block(days, close, cls, psi, nshift=240):
    """PLV эфир↔цена на полном и на очищенном ценовом пути.

    Нефть — единственный инструмент, у которого PLV когда-либо побил свой
    нуль (98.7 перцентиля на дневном BRU6). Если вычитание макро и правда
    чистит ряд, перцентиль обязан подрасти. Очищенный «путь цены» строится
    из очищенных доходностей: cumprod(exp(r_clean)) от того же старта.
    Нуль — циклические сдвиги Ψ, как в plv_null."""
    from backend import aether
    lr = np.diff(np.log(close))
    paths = {"полный": close,
             "очищенный": close[0] * np.exp(np.concatenate(
                 [[0.0], np.cumsum(clean_returns(lr, cls))]))}
    out = {}
    edge = max(3, len(days) // aether.EDGE_FRAC)
    ph_w = np.unwrap(np.angle(aether._analytic(psi)))
    for nm, pc in paths.items():
        # тот же конвейер, что в aether.couple_wave_price: полоса Бедросяна
        # по образцу Ψ, аналитический сигнал, PLV с обрезкой краёв
        ph_p = np.unwrap(np.angle(aether._analytic(
            aether._bandpass_like(np.asarray(pc, float), psi))))
        plv, _ = aether._plv(ph_w, ph_p, edge)
        vals = []
        for _ in range(nshift):
            sh = int(RNG.integers(60, max(61, min(3000, len(psi) - 60))))
            ph_s = np.unwrap(np.angle(aether._analytic(np.roll(psi, sh))))
            v, _ = aether._plv(ph_s, ph_p, edge)
            if v == v:
                vals.append(float(v))
        v = np.array(vals)
        out[nm] = {"plv": float(plv), "n": int(len(days)),
                   "нуль_медиана": float(np.median(v)),
                   "нуль_p95": float(np.quantile(v, 0.95)),
                   "перцентиль": float((v < plv).mean() * 100),
                   "p_против_нуля": float((v >= plv).mean())}
    return out


def _fmt(d):
    if not d or d.get("эффект") is None:
        return "—"
    return (f"{d['эффект']:+.4f} (p_mwu={d['p_mwu']:.4f}, "
            f"перц.нуля {d['перцентиль']:.1f}%, p={d['p_против_нуля']:.3f})")


def run(nshift=NSHIFT):
    oil = load_oil()
    d_lo = min(v[0][0] for v in oil.values())
    d_hi = max(v[0][-1] for v in oil.values())
    pm = psi_map(d_lo, d_hi)

    print("=" * 100)
    print("НЕФТЬ: ОТДЕЛЯЕМ МАКРО (ФРС / ОПЕК+ / EIA) ОТ ЭФИРА")
    print("=" * 100)
    cov = mc.coverage(date(2013, 1, 1), d_hi)
    print(f"  покрытие ОПЕК+: {cov['OPEC_покрытие']}  → основное окно "
          f"замера 2013-01-01 … 2024-12-31")
    print(f"  {cov['OPEC_note']}")
    for nm, (ds, cl) in oil.items():
        print(f"  {nm}: {len(ds)} дней {ds[0]} … {ds[-1]}")

    runs = []
    plan = [
        ("СПОТ 2013-2024 · тесный EIA", "BRENT_SPOT",
         date(2013, 1, 1), date(2024, 12, 31), 0),
        ("СПОТ 2013-2024 · широкий EIA", "BRENT_SPOT",
         date(2013, 1, 1), date(2024, 12, 31), 1),
        ("ФЬЮЧ 2013-2024 · тесный EIA", "BRENT_FUT",
         date(2013, 1, 1), date(2024, 12, 31), 0),
        ("ФЬЮЧ 2013-2024 · широкий EIA", "BRENT_FUT",
         date(2013, 1, 1), date(2024, 12, 31), 1),
        ("СПОТ 1988-2026 (ОПЕК не покрыт)", "BRENT_SPOT",
         GENESIS_DAY, d_hi, 0),
        ("ФЬЮЧ 2007-2026 (ОПЕК не покрыт)", "BRENT_FUT",
         date(2007, 7, 30), d_hi, 0),
    ]
    for title, key, a, b, half in plan:
        if key not in oil:
            continue
        res = run_series(title, oil[key][0], oil[key][1], pm, a, b, half,
                         nshift)
        if not res:
            continue
        runs.append(res)
        v = res["дисперсия"]
        print("\n" + "-" * 100)
        print(f"{title}   [{res['окно']}]  дней {res['дней']}, "
              f"тихих {res['тихих_дней']} "
              f"({res['тихих_дней']/res['дней']*100:.0f}%)")
        print(f"  σ дневная {v['sigma_дн_%']:.2f}%")
        for k in ("FOMC", "OPEC", "EIA", "ЛЮБОЙ"):
            q = v[k]
            if q["σ_отношение"] is None:
                print(f"    {k:6s}: дней {q['доля_дней']*100:5.1f}%  — пусто")
                continue
            print(f"    {k:6s}: дней {q['доля_дней']*100:5.1f}%  "
                  f"Σr² {q['доля_Σr2']*100:5.1f}%  "
                  f"σ внутри {q['σ_внутри_%']:.2f}% / вне {q['σ_вне_%']:.2f}% "
                  f"= {q['σ_отношение']:.3f}")
        print(f"    η² по средним доходностям: {v['η2_среднее']*100:.3f}%   "
              f"R² волатильности по классам: {v['R2_волатильности']*100:.3f}%")
        print(f"  эффект «край−середина» AR(1) по сырой Ψ без Солнца и Луны:")
        print(f"    полный ряд      : {_fmt(res['полный'])}")
        print(f"    очищенный       : {_fmt(res['очищенный'])}")
        print(f"    только тихие дни: {_fmt(res['только_тихие'])}")
        if res["только_макро"]:
            print(f"    только макро-дни: {res['только_макро']['эффект']:+.4f} "
                  f"(p_mwu={res['только_макро']['p_mwu']:.4f})")
        print(f"    тихие по дням недели: {res['тихие_по_дням_недели']}")
        if res.get("PLV"):
            for nm, q in res["PLV"].items():
                if q:
                    print(f"    PLV {nm:10s}: {q['plv']:.3f}  нуль медиана "
                          f"{q['нуль_медиана']:.3f} p95 {q['нуль_p95']:.3f}  "
                          f"перц. {q['перцентиль']:.1f}%  "
                          f"p={q['p_против_нуля']:.3f}")

    print("\n" + "=" * 100)
    print("СВОДКА: стало ли лучше после вычитания макро")
    print("=" * 100)
    print(f"{'ряд':38s} {'полный':>22s} {'очищенный':>22s} {'тихие':>22s}")
    for res in runs:
        def s(k):
            d = res[k]
            if not d or d.get("эффект") is None:
                return "—"
            return f"{d['эффект']:+.4f} p={d['p_против_нуля']:.3f}"
        print(f"{res['название'][:38]:38s} {s('полный'):>22s} "
              f"{s('очищенный'):>22s} {s('только_тихие'):>22s}")
    json.dump(runs, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"\n→ {OUT}")
    return runs


if __name__ == "__main__":
    # ── быстрые честные проверки логики до тяжёлого прогона ──
    m = mc.macro_days(date(2020, 1, 1), date(2020, 12, 31))
    assert date(2020, 3, 15) in m["FOMC"] and date(2020, 4, 12) in m["OPEC"]

    # очистка обязана убить разницу дисперсий между классами
    _r = np.concatenate([np.full(400, 0.0), np.full(400, 0.0)])
    _r[:400] = np.linspace(-1, 1, 400)
    _r[400:] = np.linspace(-5, 5, 400) + 3.0
    _c = np.concatenate([np.zeros(400, int), np.ones(400, int)])
    _cl = clean_returns(_r, _c)
    assert abs(_cl[:400].mean()) < 1e-9 and abs(_cl[400:].mean()) < 1e-9
    assert abs(_cl[:400].std() / _cl[400:].std() - 1.0) < 1e-9, "разброс не выровнен"
    assert abs(_cl.std() - _r.std()) < 1e-9, "общий разброс должен сохраниться"

    # AR по готовым доходностям = AR по ценам из ar_nulls
    from backend.lab.ar_nulls import ar_series
    _p = np.cumprod(1 + np.sin(np.arange(500) / 7.0) * 0.01) * 50
    _a1 = ar_series(_p)
    _a2 = ar_from_returns(np.diff(np.log(_p)), len(_p))
    _ok = np.isfinite(_a1) & np.isfinite(_a2)
    assert _ok.sum() > 400 and np.allclose(_a1[_ok], _a2[_ok]), "AR разъехался"

    # подряд тихих дней: значение обязано стоять ТОЛЬКО на тихих днях
    _qr = np.zeros(499, bool); _qr[::2] = True          # маска по доходностям
    _qd = np.concatenate([[False], _qr])                # …она же по дням (сдвиг 1)
    _aq = ar_quiet_subseries(np.diff(np.log(_p)), _qr, 500)
    assert np.all(np.isnan(_aq[~_qd])), "AR тихого подряда протёк на макро-дни"
    assert np.isfinite(_aq).sum() > 200, np.isfinite(_aq).sum()
    # и он обязан отличаться от AR полного ряда (считан по другим данным)
    _af = ar_from_returns(np.diff(np.log(_p)), 500)
    _both = np.isfinite(_aq) & np.isfinite(_af)
    assert _both.sum() > 100 and not np.allclose(_aq[_both], _af[_both])

    # разделение дисперсии на игрушке: класс с втрое большим разбросом
    _mk = {"FOMC": _c.astype(bool), "OPEC": np.zeros(800, bool),
           "EIA": np.zeros(800, bool), "ЛЮБОЙ": _c.astype(bool)}
    # очищенный ЦЕНОВОЙ ПУТЬ обязан стартовать с той же цены и иметь ту же длину
    _pp = _p
    _path = _pp[0] * np.exp(np.concatenate(
        [[0.0], np.cumsum(clean_returns(np.diff(np.log(_pp)),
                                        np.zeros(len(_pp) - 1, int)))]))
    assert len(_path) == len(_pp) and abs(_path[0] - _pp[0]) < 1e-9
    # один класс без сдвига среднего → путь обязан совпасть с исходным
    assert np.allclose(_path, _pp * np.exp(-np.diff(np.log(_pp)).mean()
                                           * np.arange(len(_pp))), rtol=1e-6)

    _vs = variance_split(_r, _mk, _c)
    assert _vs["FOMC"]["σ_отношение"] > 4.0, _vs["FOMC"]["σ_отношение"]
    assert _vs["FOMC"]["доля_Σr2"] > _vs["FOMC"]["доля_дней"]

    run()
