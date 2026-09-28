"""ГДЕ ЖИВЁТ ЭФФЕКТ: В УГЛАХ ИЛИ В ПЛОТНОСТИ E(t)?

Контекст. Эффект «край−середина» в AR(1) по корзинам сырой Ψ без Солнца и
Луны: +0.0403, знак 8/8. Он пережил 200 циклических сдвигов Ψ (p=0.01), но
НЕ пережил подмену неба внутри родной машинерии (fake_sky_native: чужие
периоды дают +0.0407, p=0.52). Формула Ψ = Σ E_i(t)·B·cos(k·d_i(t)) состоит
из ДВУХ временных функций: угловой части cos(k·d) и плотности E_i(t),
которая считается по настоящим эфемеридам (расстояния/скорости).

В fake_sky_native подменялись ТОЛЬКО углы — E оставалась настоящей и
НЕсдвинутой. Циклический сдвиг же катает по времени всё разом. Отсюда
гипотеза: эффект сидит не в аспектах, а в E(t).

Три опыта, разделяющие слагаемые:
  A  E ≡ 1 — углы настоящие, плотность убита.
  B  Ψ_shiftE   = Σ E_i(t+Δ)·cos(k·d_i(t))  — сдвинута только плотность.
  C  Ψ_shiftAng = Σ E_i(t)·cos(k·d_i(t+Δ))  — сдвинуты только углы.

Читается так: если A ≈ 0 и B убивает, а C нет — величина есть E(t), то
есть медленная функция геометрии Земля↔тело, а аспекты декорация. Если A
держится и C убивает — наоборот.

Никакого random в продукте: сдвиги детерминированы фиксированным сидом.

python3 -m backend.lab.e_vs_angles
"""
import json
from datetime import date, datetime, timezone

import numpy as np

from backend import aether
# АРХИВНЫЙ СТЕНД: воспроизводит СТАРУЮ формулу Ψ со спектральным сродством.
# Из боевого aether AFFINITY удалён (v3.7, вердикт мировоззрения 05.08.2026:
# «подгонка под ответ») — здесь константа оставлена локально, чтобы стенд
# продолжал воспроизводить ровно то, что было измерено в июле 2026.
AFFINITY_ARCHIVE = 1.5

from backend.lab.ar_nulls import ar_series, effect

HIST = "/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/moex_hist.json"
NSHIFT = 60
RNG = np.random.default_rng(31072026)
DROP = ("Солнце", "Луна")          # работаем с Ψ без Солнца и Луны


def raw_tracks(dates, ticker):
    """Сырые дорожки: ts, углы d_i(t) и плотности E_i(t) по телам (без
    Солнца и Луны). Ничего не суммируем — суммирование ниже, чтобы можно
    было двигать слагаемые по отдельности."""
    g = aether.genesis_for(ticker)
    nat = aether.natal_lons(g["genesis"])
    if nat is None:
        return None
    t0 = datetime(dates[0].year, dates[0].month, dates[0].day, 12,
                  tzinfo=timezone.utc)
    tr = aether.transit_series(t0, (dates[-1] - dates[0]).days * 24 + 24, 24.0)
    lam0 = np.array([r["lon"] for r in nat], float)[None, :]
    D, E, B = {}, {}, {}
    for nm in aether._BODY_ORDER:
        if nm in DROP:
            continue
        D[nm] = np.abs(aether._wrap180(tr["lam"][nm][:, None] - lam0))
        E[nm] = tr["E"][nm]
        B[nm] = AFFINITY_ARCHIVE if aether.W_VEDIC[nm] == g["band"] else 1.0
    return np.asarray(tr["ts"], float), D, E, B


def psi_from(D, E, B, shift_e=0, shift_a=0, e_const=False):
    """Сборка Ψ из дорожек. shift_e/shift_a — циклический сдвиг только
    плотности / только углов (в узлах суточной сетки)."""
    out = None
    for nm in D:
        d = np.roll(D[nm], shift_a, axis=0) if shift_a else D[nm]
        k = aether._ASP_K[np.argmin(
            np.abs(d[:, :, None] - aether._ASP_ANG[None, None, :]), axis=2)]
        cosp = np.sum(B[nm] * np.cos(np.radians(k * d)), axis=1)
        if e_const:
            term = cosp                       # E ≡ 1
        else:
            e = np.roll(E[nm], shift_e) if shift_e else E[nm]
            term = np.sum(e[:, None] * B[nm] *
                          np.cos(np.radians(k * d)), axis=1)
        out = term if out is None else out + term
    return out / 100.0


def _median_sign(vals):
    v = np.array([x for x in vals if np.isfinite(x)])
    return (float(np.median(v)), int((v > 0).sum()), len(v)) if len(v) else (np.nan, 0, 0)


def run():
    raw = json.load(open(HIST, encoding="utf-8"))
    T = {}
    for tk in sorted(raw):
        rows = raw[tk]
        ds = [date.fromisoformat(r[0]) for r in rows]
        cl = np.array([float(r[4]) for r in rows])
        got = raw_tracks(ds, tk)
        if not got:
            continue
        ts, D, E, B = got
        pmap = {datetime.fromtimestamp(t, timezone.utc).date(): i
                for i, t in enumerate(ts)}
        idx = np.array([pmap.get(d, -1) for d in ds])
        T[tk] = dict(D=D, E=E, B=B, ar=ar_series(cl), idx=idx, n=len(ts))

    def onday(psi, t):
        pv = np.where(t["idx"] >= 0, psi[np.clip(t["idx"], 0, len(psi) - 1)],
                      np.nan)
        return pv

    print("=" * 96)
    print("ГДЕ ЖИВЁТ ЭФФЕКТ · Ψ без Солнца и Луны, 8 бумаг MOEX")
    print("=" * 96)

    base, aconst = [], []
    for tk, t in T.items():
        p0 = psi_from(t["D"], t["E"], t["B"])
        pA = psi_from(t["D"], t["E"], t["B"], e_const=True)
        e0 = effect(onday(p0, t), t["ar"])
        eA = effect(onday(pA, t), t["ar"])
        base.append(e0[0] if e0 else np.nan)
        aconst.append(eA[0] if eA else np.nan)
        print(f"  {tk}: база {e0[0]:+.4f}(p={e0[1]:.4f})   "
              f"A·E≡1 {eA[0]:+.4f}(p={eA[1]:.4f})")
    m0 = _median_sign(base)
    mA = _median_sign(aconst)
    print(f"\n  БАЗА       : медиана {m0[0]:+.4f}  знак {m0[1]}/{m0[2]}")
    print(f"  A · E ≡ 1  : медиана {mA[0]:+.4f}  знак {mA[1]}/{mA[2]}")

    print("\n" + "=" * 96)
    print(f"B/C · СДВИГ ТОЛЬКО ОДНОГО СЛАГАЕМОГО ({NSHIFT} сдвигов)")
    print("=" * 96)
    nmin = min(t["n"] for t in T.values())
    shifts = RNG.integers(200, nmin - 200, size=NSHIFT)
    resB, resC = [], []
    for sh in shifts:
        vb, vc = [], []
        for tk, t in T.items():
            s = int(sh) % t["n"]
            eb = effect(onday(psi_from(t["D"], t["E"], t["B"], shift_e=s), t),
                        t["ar"])
            ec = effect(onday(psi_from(t["D"], t["E"], t["B"], shift_a=s), t),
                        t["ar"])
            vb.append(eb[0] if eb else np.nan)
            vc.append(ec[0] if ec else np.nan)
        resB.append(_median_sign(vb))
        resC.append(_median_sign(vc))
        if len(resB) % 20 == 0:
            print(f"  … {len(resB)} сдвигов посчитано")

    for nm, res in (("B · сдвинута только E(t)", resB),
                    ("C · сдвинуты только углы", resC)):
        med = np.array([r[0] for r in res])
        uni = np.array([r[1] == r[2] for r in res])
        p = float((med >= m0[0]).mean())
        print(f"\n{nm}:")
        print(f"  медиана нуля: центр {np.median(med):+.4f}  "
              f"p95 {np.quantile(med, 0.95):+.4f}  макс {med.max():+.4f}")
        print(f"  доля 8/8 = {uni.mean()*100:.1f}%")
        print(f"  p(величина) = {p:.4f}   "
              f"{'ЭФФЕКТ ВЫШЕ НУЛЯ' if p < 0.05 else 'НУЛЬ ВОСПРОИЗВОДИТ ЭФФЕКТ'}")

    print("\nЧИТАЕТСЯ: где сдвиг убивает — там и живёт временная привязка.")


if __name__ == "__main__":
    # реальные проверки сборки Ψ, а не факт запуска
    _D = {"Марс": np.tile(np.linspace(0.0, 180.0, 64)[:, None], (1, 3))}
    _E = {"Марс": np.linspace(1.0, 2.0, 64)}
    _B = {"Марс": 1.0}
    _p = psi_from(_D, _E, _B)
    assert _p.shape == (64,), "Ψ обязана быть рядом длины сетки"
    assert np.all(np.isfinite(_p)), "в Ψ не должно быть NaN"
    _pc = psi_from(_D, _E, _B, e_const=True)
    assert not np.allclose(_p, _pc), "E≡1 обязана менять Ψ"
    assert np.allclose(_pc, psi_from(_D, {"Марс": np.ones(64)}, _B)), \
        "E≡1 обязана совпадать с настоящей E из единиц"
    _ps = psi_from(_D, _E, _B, shift_e=7)
    assert np.allclose(np.sort(_ps), np.sort(psi_from(
        {"Марс": _D["Марс"]}, {"Марс": np.roll(_E["Марс"], 7)}, _B))), \
        "сдвиг E обязан быть эквивалентен перекатке самого ряда E"
    assert np.allclose(psi_from(_D, _E, _B, shift_a=0), _p), \
        "нулевой сдвиг обязан возвращать базу"
    _pa = psi_from(_D, _E, _B, shift_a=13)
    assert not np.allclose(_pa, _p), "сдвиг углов обязан менять Ψ"
    run()
