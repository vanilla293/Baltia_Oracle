"""ЗАМЕР №2 ПО ЕГО ЗАКАЗУ: не уровень, а СТРУКТУРА.

  A. БИРЖА: AR(1) доходностей за прошлые 20 дней — мера инерции (тренд/
     возврат/шум) — в корзинах по квинтилям СЫРОЙ Ψ.
  B. РЕСТОРАНЫ: 16 точек, 4 года дневного потока заказов. Среда без
     спекулянтов: просто люди, которые хотят есть. Меряем среднее и
     разброс потока в скользящем окне 5 дней в тех же корзинах по Ψ,
     где Ψ считается от ГЕНЕЗИСА КАЖДОЙ ТОЧКИ (дата открытия).

Всё смотрит только в прошлое: и AR(1), и std берутся по прошедшим дням.

python3 -m backend.lab.psi_structure
"""
import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
from scipy import stats

from backend import aether
# АРХИВНЫЙ СТЕНД: воспроизводит СТАРУЮ формулу Ψ со спектральным сродством.
# Из боевого aether AFFINITY удалён (v3.7, вердикт мировоззрения 05.08.2026:
# «подгонка под ответ») — здесь константа оставлена локально, чтобы стенд
# продолжал воспроизводить ровно то, что было измерено в июле 2026.
AFFINITY_ARCHIVE = 1.5

from backend.lab.psi_regime import build_psi

HIST = "/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/moex_hist.json"
STORES = "/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/resto/resto/data/stores.json"
AR_WIN = 20
V_WIN = 5


def buckets(p, m):
    q = np.nanquantile(p[m], [0.2, 0.4, 0.6, 0.8])
    return (m & (p <= q[0]), m & (p >= q[1]) & (p <= q[2]), m & (p >= q[3]))


def judge(name, val, lo, mid, hi):
    ext = lo | hi
    u, pu = stats.mannwhitneyu(val[ext], val[mid], alternative="two-sided")
    return dict(n=(int(lo.sum()), int(mid.sum()), int(hi.sum())),
                v=(float(np.mean(val[lo])), float(np.mean(val[mid])),
                   float(np.mean(val[hi]))),
                med=(float(np.median(val[lo])), float(np.median(val[mid])),
                     float(np.median(val[hi]))),
                p=float(pu))


def psi_for(dates, ticker=None, genesis=None):
    if genesis is not None:
        g = {"genesis": genesis, "band": 54}
        nat = aether.natal_lons(genesis)
        if nat is None:
            return None
        t0 = datetime(dates[0].year, dates[0].month, dates[0].day, 12,
                      tzinfo=timezone.utc)
        tr = aether.transit_series(t0, (dates[-1] - dates[0]).days * 24 + 24, 24.0)
        lam0 = np.array([r["lon"] for r in nat], float)[None, :]
        psi = np.zeros(len(tr["ts"]))
        for nm in aether._BODY_ORDER:
            d = np.abs(aether._wrap180(tr["lam"][nm][:, None] - lam0))
            k = aether._ASP_K[np.argmin(
                np.abs(d[:, :, None] - aether._ASP_ANG[None, None, :]), axis=2)]
            B = AFFINITY_ARCHIVE if aether.W_VEDIC[nm] == g["band"] else 1.0
            psi += np.sum(tr["E"][nm][:, None] * B * np.cos(np.radians(k * d)), axis=1)
        return np.asarray(tr["ts"], float), psi / 100.0
    ts, praw, _ = build_psi(dates, ticker)
    return ts, praw


def run():
    print("=" * 96)
    print("A · БИРЖА: ИНЕРЦИЯ AR(1) ЗА ПРОШЛЫЕ 20 ДНЕЙ В КОРЗИНАХ ПО СЫРОЙ Ψ")
    print("=" * 96)
    raw = json.load(open(HIST, encoding="utf-8"))
    res_a = {}
    for tk in sorted(raw):
        rows = raw[tk]
        ds = [date.fromisoformat(r[0]) for r in rows]
        cl = np.array([float(r[4]) for r in rows])
        lr = np.diff(np.log(cl))
        ar = np.full(len(ds), np.nan)
        for i in range(AR_WIN + 1, len(lr)):
            w = lr[i - AR_WIN:i]
            if w.std() > 0:
                ar[i] = float(np.corrcoef(w[:-1], w[1:])[0, 1])
        ts, p = psi_for(ds, ticker=tk)
        pmap = {datetime.fromtimestamp(t, timezone.utc).date(): i
                for i, t in enumerate(ts)}
        pv = np.array([p[pmap[d]] if d in pmap else np.nan for d in ds])
        m = np.isfinite(pv) & np.isfinite(ar)
        lo, mid, hi = buckets(pv, m)
        r = judge(tk, ar, lo, mid, hi)
        res_a[tk] = r
        print(f"{tk}: n={r['n']}  AR(1) низ/сер/верх = "
              f"{r['v'][0]:+.4f} / {r['v'][1]:+.4f} / {r['v'][2]:+.4f}   "
              f"Манн-Уитни p={r['p']:.4f}")

    print("\n" + "=" * 96)
    print("B · РЕСТОРАНЫ: ПОТОК ЗАКАЗОВ В КОРЗИНАХ ПО Ψ ОТ ГЕНЕЗИСА ТОЧКИ")
    print("=" * 96)
    st = json.load(open(STORES, encoding="utf-8"))
    res_b = {}
    for code in sorted(st):
        s = st[code]
        days = s["days"]
        ds = sorted(date.fromisoformat(k) for k in days
                    if days[k].get("orders") is not None)
        if len(ds) < 300:
            continue
        y = np.array([float(days[d.isoformat()]["orders"]) for d in ds])
        gen = tuple(int(x) for x in s["genesis"].split("-")) + (12, 0, 0)
        got = psi_for(ds, genesis=gen)
        if got is None:
            print(f"{code}: нет эфемерид"); continue
        ts, p = got
        pmap = {datetime.fromtimestamp(t, timezone.utc).date(): i
                for i, t in enumerate(ts)}
        pv = np.array([p[pmap[d]] if d in pmap else np.nan for d in ds])
        # ровность потока: std/среднее по ПРОШЛЫМ 5 дням
        cv = np.full(len(ds), np.nan)
        lvl = np.full(len(ds), np.nan)
        for i in range(V_WIN, len(ds)):
            w = y[i - V_WIN:i]
            if w.mean() > 0:
                cv[i] = w.std(ddof=1) / w.mean()
                lvl[i] = w.mean()
        m = np.isfinite(pv) & np.isfinite(cv)
        if m.sum() < 200:
            continue
        lo, mid, hi = buckets(pv, m)
        rc = judge(code, cv, lo, mid, hi)
        rl = judge(code, lvl, lo, mid, hi)
        res_b[code] = {"cv": rc, "lvl": rl}
        print(f"{code}: разброс потока (std/сред) низ/сер/верх = "
              f"{rc['v'][0]:.4f} / {rc['v'][1]:.4f} / {rc['v'][2]:.4f}  p={rc['p']:.4f}   │  "
              f"уровень {rl['v'][0]:.0f} / {rl['v'][1]:.0f} / {rl['v'][2]:.0f}  p={rl['p']:.4f}")

    print("\nПОПРАВКА ХОЛМА, порог 0.01:")
    for nm, res, key in (("биржа AR(1)", res_a, None),
                         ("ресторан разброс", res_b, "cv"),
                         ("ресторан уровень", res_b, "lvl")):
        ps = sorted((r[key]["p"] if key else r["p"], k) for k, r in res.items())
        m = len(ps); win = []
        for i, (p, k) in enumerate(ps, 1):
            if p <= 0.01 / (m - i + 1):
                win.append((k, round(p, 5)))
            else:
                break
        print(f"  {nm:20s}: прошли {len(win)} из {m} → {win if win else 'НИКТО'}")
    json.dump({"birzha_ar": res_a, "resto": res_b},
              open("/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/psi_structure.json",
                   "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("→ ds/psi_structure.json")


if __name__ == "__main__":
    run()
