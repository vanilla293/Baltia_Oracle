"""РАСЧЛЕНЕНИЕ Ψ: Солнце отдельно, всё остальное отдельно.

Найденный эффект (AR(1) в середине корзин Ψ возвратнее, чем на краях,
знак 8/8, переживает эпохи и чужую волну, гибнет на квартальном сдвиге и
выживает на полугодовом) надо разложить. Вопрос ровно один: это Солнце
или это остальное небо?

  Ψ_SUN    — та же формула, но вклад ТОЛЬКО Солнца.
  Ψ_NOSUN  — все тела КРОМЕ Солнца.
  Ψ_NOSUNMOON — без Солнца и Луны (Луна тоже завязана на Солнце через фазу).
Никакой min-max нормировки: работаем с сырыми значениями.

python3 -m backend.lab.psi_split
"""
import json
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


def psi_bodies(dates, ticker=None, genesis=None, band=None):
    """Сырая Ψ ПО КАЖДОМУ ТЕЛУ отдельно — общий движок для psi_parts.

    Возвращает (ts, {имя тела: Ψ_тела}) на суточной сетке от dates[0] до
    dates[-1]. genesis/band можно задать напрямую (объекты вне реестра
    тикеров — например пиццерии), иначе берутся из aether.genesis_for.
    Никаких заглушек: нет эфемерид → None.
    """
    if genesis is None or band is None:
        g = aether.genesis_for(ticker)
        genesis = g["genesis"] if genesis is None else genesis
        band = g["band"] if band is None else band
    nat = aether.natal_lons(genesis)
    if nat is None:
        return None
    t0 = datetime(dates[0].year, dates[0].month, dates[0].day, 12,
                  tzinfo=timezone.utc)
    tr = aether.transit_series(t0, (dates[-1] - dates[0]).days * 24 + 24, 24.0)
    if tr is None:
        return None
    lam0 = np.array([r["lon"] for r in nat], float)[None, :]
    parts = {}
    for nm in aether._BODY_ORDER:
        d = np.abs(aether._wrap180(tr["lam"][nm][:, None] - lam0))
        k = aether._ASP_K[np.argmin(
            np.abs(d[:, :, None] - aether._ASP_ANG[None, None, :]), axis=2)]
        B = AFFINITY_ARCHIVE if aether.W_VEDIC[nm] == band else 1.0
        parts[nm] = np.sum(tr["E"][nm][:, None] * B * np.cos(np.radians(k * d)),
                           axis=1) / 100.0
    return np.asarray(tr["ts"], float), parts


def psi_parts(dates, ticker):
    """Сырая Ψ по группам тел."""
    got = psi_bodies(dates, ticker)
    if got is None:
        return None
    ts, parts = got
    full = sum(parts.values())
    sun = parts["Солнце"]
    nosun = full - sun
    nosunmoon = nosun - parts.get("Луна", 0)
    return ts, {"Ψ полная": full, "Ψ только Солнце": sun,
                "Ψ без Солнца": nosun, "Ψ без Солнца и Луны": nosunmoon}


def run():
    raw = json.load(open(HIST, encoding="utf-8"))
    print("=" * 96)
    print("РАСЧЛЕНЕНИЕ Ψ · эффект «край минус середина» в AR(1)")
    print("=" * 96)
    acc = {}
    for tk in sorted(raw):
        rows = raw[tk]
        ds = [date.fromisoformat(r[0]) for r in rows]
        cl = np.array([float(r[4]) for r in rows])
        ar = ar_series(cl)
        got = psi_parts(ds, tk)
        if not got:
            continue
        ts, P = got
        pmap = {datetime.fromtimestamp(t, timezone.utc).date(): i
                for i, t in enumerate(ts)}
        line = f"{tk}: "
        for nm, p in P.items():
            pv = np.array([p[pmap[d]] if d in pmap else np.nan for d in ds])
            e = effect(pv, ar)
            acc.setdefault(nm, []).append(e if e else (np.nan, 1.0))
            line += f"{nm}={e[0]:+.4f}(p={e[1]:.4f})  " if e else f"{nm}=—  "
        print(line)
    print()
    for nm, vals in acc.items():
        d = np.array([v[0] for v in vals]); p = np.array([v[1] for v in vals])
        good = np.isfinite(d)
        print(f"{nm:24s}: медиана {np.median(d[good]):+.4f}  "
              f"знак совпал {int((d[good] > 0).sum())}/{int(good.sum())}  "
              f"p<0.01 у {int((p[good] < 0.01).sum())} бумаг")
    print()
    print("ЧИТАЕТСЯ ТАК: если единодушие 8/8 держится только у «только Солнце» —")
    print("это солнцестояния, а планеты мебель. Если только у «без Солнца» —")
    print("это не Солнце. Если у обеих — Солнце основное, планеты модулируют.")


if __name__ == "__main__":
    run()
