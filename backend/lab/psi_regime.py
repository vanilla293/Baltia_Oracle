"""ЗАМЕР ПО ЕГО ЗАКАЗУ: волатильность в корзинах по Ψ, 13 лет, 8 бумаг.

Он предложил: экстремумы Ψ (близко к 0 и к 1) против нейтрали (0.4-0.6),
отклик — реализованная волатильность за прошлые 5 дней. Ψ предопределена
эфемеридами, значит подгонки быть не может.

ЛОВУШКА, КОТОРУЮ НАДО ОБОЙТИ (нашлась в коде psi_of): Ψ нормируется
min-max ВНУТРИ ОКНА. То есть 0.0 и 1.0 есть в каждом окне ВСЕГДА, по
построению — это минимум и максимум окна, а не абсолютные уровни. Поэтому
считаем ДВЕ версии:
   RAW  — сырая Ψ до нормировки (абсолютный уровень, сравнимый между годами);
   ROLL — как в продукте: min-max по скользящему окну ±30 суток.
Первая отвечает на вопрос «есть ли абсолютный уровень эфира», вторая — на
вопрос «работает ли то, что видит пользователь».

python3 -m backend.lab.psi_regime
"""
import json, math
from datetime import date, datetime, timedelta, timezone

import numpy as np
from scipy import stats

from backend import aether
# АРХИВНЫЙ СТЕНД: воспроизводит СТАРУЮ формулу Ψ со спектральным сродством.
# Из боевого aether AFFINITY удалён (v3.7, вердикт мировоззрения 05.08.2026:
# «подгонка под ответ») — здесь константа оставлена локально, чтобы стенд
# продолжал воспроизводить ровно то, что было измерено в июле 2026.
AFFINITY_ARCHIVE = 1.5


HIST = "/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/moex_hist.json"
VOL_WIN = 5
ROLL_HALF = 30      # ±30 суток, как окно продукта


def psi_raw(ts_unix, natal_rows, band):
    """Ψ до min-max — абсолютный уровень (копия psi_of без _norm01)."""
    tr = aether.transit_series_at(ts_unix) if hasattr(aether, "transit_series_at") else None
    return tr


def build_psi(dates, ticker):
    """Сырая и оконная Ψ по дневной сетке."""
    g = aether.genesis_for(ticker)
    nat = aether.natal_lons(g["genesis"])
    if nat is None:
        return None, None
    t0 = datetime(dates[0].year, dates[0].month, dates[0].day, 12,
                  tzinfo=timezone.utc)
    span_h = (dates[-1] - dates[0]).days * 24 + 24
    tr = aether.transit_series(t0, span_h, 24.0)      # шаг сутки
    lam0 = np.array([r["lon"] for r in nat], float)[None, :]
    psi = np.zeros(len(tr["ts"]))
    for nm in aether._BODY_ORDER:
        d = np.abs(aether._wrap180(tr["lam"][nm][:, None] - lam0))
        k = aether._ASP_K[np.argmin(np.abs(d[:, :, None] - aether._ASP_ANG[None, None, :]),
                                    axis=2)]
        B = AFFINITY_ARCHIVE if aether.W_VEDIC[nm] == g["band"] else 1.0
        psi += np.sum(tr["E"][nm][:, None] * B * np.cos(np.radians(k * d)), axis=1)
    psi = psi / 100.0
    ts = np.asarray(tr["ts"], float)
    # оконная нормировка как в продукте
    roll = np.full(len(psi), np.nan)
    for i in range(len(psi)):
        lo = max(0, i - ROLL_HALF); hi = min(len(psi), i + ROLL_HALF + 1)
        w = psi[lo:hi]
        rng = w.max() - w.min()
        roll[i] = (psi[i] - w.min()) / rng if rng > 0 else 0.0
    return ts, psi, roll


def run():
    raw = json.load(open(HIST, encoding="utf-8"))
    print("=" * 96)
    print("ВОЛАТИЛЬНОСТЬ В КОРЗИНАХ ПО Ψ · 13 лет · его гипотеза")
    print("=" * 96)
    res = {}
    for tk in sorted(raw):
        rows = raw[tk]
        ds = [date.fromisoformat(r[0]) for r in rows]
        cl = np.array([float(r[4]) for r in rows])
        lr = np.diff(np.log(cl))
        vol = np.full(len(ds), np.nan)
        for i in range(VOL_WIN, len(lr)):
            vol[i] = np.std(lr[i - VOL_WIN:i], ddof=1)   # ТОЛЬКО прошлое
        ts, praw, proll = build_psi(ds, tk)
        # сопоставляем по дате
        pmap = {datetime.fromtimestamp(t, timezone.utc).date(): i
                for i, t in enumerate(ts)}
        idx = [pmap.get(d) for d in ds]
        ok = np.array([i is not None for i in idx])
        pr = np.array([praw[i] if i is not None else np.nan for i in idx])
        pl = np.array([proll[i] if i is not None else np.nan for i in idx])
        out = {}
        for nm, p in (("сырая", (pr - np.nanmin(pr)) / (np.nanmax(pr) - np.nanmin(pr))),
                      ("оконная", pl)):
            m = np.isfinite(p) & np.isfinite(vol)
            if nm == "сырая":
                # у сырой Ψ за 13 лет глобальный min-max оставляет почти всё
                # в узкой полосе — фиксированные отсечки 0.2/0.8 дают пустые
                # корзины. Берём КВАНТИЛИ её собственного распределения:
                # нижние 20% / серединные 20% / верхние 20%.
                q = np.nanquantile(p[m], [0.2, 0.4, 0.6, 0.8])
                lo = m & (p <= q[0]); mid = m & (p >= q[1]) & (p <= q[2])
                hi = m & (p >= q[3])
            else:
                lo = m & (p < 0.2); mid = m & (p >= 0.4) & (p <= 0.6); hi = m & (p > 0.8)
            if lo.sum() < 30 or mid.sum() < 30 or hi.sum() < 30:
                continue
            ext = lo | hi
            u, pu = stats.mannwhitneyu(vol[ext], vol[mid], alternative="two-sided")
            t, pt = stats.ttest_ind(vol[ext], vol[mid], equal_var=False)
            out[nm] = dict(n_lo=int(lo.sum()), n_mid=int(mid.sum()), n_hi=int(hi.sum()),
                           v_lo=float(np.mean(vol[lo])), v_mid=float(np.mean(vol[mid])),
                           v_hi=float(np.mean(vol[hi])),
                           med_lo=float(np.median(vol[lo])), med_mid=float(np.median(vol[mid])),
                           med_hi=float(np.median(vol[hi])),
                           p_mw=float(pu), p_t=float(pt),
                           ratio=float(np.mean(vol[ext]) / np.mean(vol[mid])))
        res[tk] = out
        for nm, o in out.items():
            print(f"{tk} [{nm:7s}] n(низ/сер/верх)={o['n_lo']}/{o['n_mid']}/{o['n_hi']}  "
                  f"σ: {o['v_lo']*100:.3f}% / {o['v_mid']*100:.3f}% / {o['v_hi']*100:.3f}%  "
                  f"край/середина ×{o['ratio']:.3f}  Манн-Уитни p={o['p_mw']:.4f}  "
                  f"t-тест p={o['p_t']:.4f}")
    # поправка на 8 бумаг
    print("\nПОПРАВКА НА МНОЖЕСТВЕННОСТЬ (Холм, 8 бумаг, порог 0.01 по его условию):")
    for nm in ("сырая", "оконная"):
        ps = sorted((res[tk][nm]["p_mw"], tk) for tk in res if nm in res[tk])
        m = len(ps)
        win = []
        for r, (p, tk) in enumerate(ps, 1):
            thr = 0.01 / (m - r + 1)
            if p <= thr:
                win.append((tk, p, thr))
            else:
                break
        print(f"  {nm:8s}: прошли {len(win)} из {m} → "
              f"{[(t, round(p,5)) for t, p, _ in win] if win else 'НИКТО'}")
    json.dump(res, open("/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/psi_regime.json",
                        "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("→ ds/psi_regime.json")


if __name__ == "__main__":
    run()
