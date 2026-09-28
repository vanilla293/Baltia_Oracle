"""ВЕЙВЛЕТ МОРЛЕ — время-частотная карта вместо Фурье и Гильберта.

🔵 МЕХАНИКА. Здесь нет ни одной астрологической величины: это чистый
цифровой анализ сигнала, проверяемый на синтетике с известными ответами.

Зачем. Фурье усредняет по всему окну: он честно скажет, ЧТО в ряду есть,
но не скажет КОГДА — периодограмма ряда «сначала период 20, потом 60»
почти неотличима от периодограммы их суммы. Гильберт даёт ОДНУ мгновенную
фазу на всю полосу: для суммы двух тонов он аналитически возвращает не
два периода, а один — средневзвешенный (при равных амплитудах ровно
гармоническое среднее). Оба размазывают то, что нам нужно: короткие,
перемежаемые всплески быстрых тел.

Непрерывное вейвлет-преобразование (CWT) даёт карту (время × масштаб).

Соглашения — Torrence & Compo, «A Practical Guide to Wavelet Analysis»,
BAMS 79(1), 1998. Все нормировки оттуда, чтобы числа сходились с
литературой и с аналитикой.

  ядро:      ψ₀(η) = π^(−1/4)·exp(i·ω₀·η)·exp(−η²/2),  ω₀ = 6
  образ:     ψ̂₀(sω) = π^(−1/4)·H(ω)·exp(−(sω−ω₀)²/2)
  CWT:       W(s,n) = Σ_k x̂_k · ψ̂*(s·ω_k) · exp(i·ω_k·n·δt)
             ψ̂(sω) = (2πs/δt)^(1/2) · ψ̂₀(sω)
  период:    T = 4π·s/(ω₀+√(2+ω₀²));  при ω₀=6  T = 1.03300·s
  конус:     e-folding = √2·s; внутри конуса значения — NaN, ими нельзя
             пользоваться, там вейвлет вылез за край ряда

Про допустимость. Строго ψ должна иметь нулевое среднее; у Морле оно
равно π^(−1/4)·√(2π)·exp(−ω₀²/2). Директива называет ~1e−5 — это оценка
для ω₀≈4.8; при ω₀=6 остаток равен 2.9e−8, то есть запас ещё на три
порядка больше. Проверяется в self-тесте численно.

python3 -m backend.lab.morlet
"""
import numpy as np

OMEGA0 = 6.0            # безразмерная несущая ядра (стандарт)
C_DELTA = 0.776         # константа реконструкции, T&C табл. 2, Морле ω₀=6
PSI0_AT_ZERO = np.pi ** -0.25   # ψ₀(0) = π^(−1/4) = 0.7511
SCALE_DECORR = 0.60     # длина декорреляции по масштабу (для когерентности)


# ---------------------------------------------------------------- ядро

def fourier_factor(w0=OMEGA0):
    """Множитель перехода масштаб → фурье-период. При ω₀=6 равен 1.03300."""
    return 4.0 * np.pi / (w0 + np.sqrt(2.0 + w0 * w0))


def scale_to_period(s, w0=OMEGA0):
    """s → T. НЕ путать: s и T различаются на 3.3%, это не одно и то же."""
    return np.asarray(s, float) * fourier_factor(w0)


def period_to_scale(T, w0=OMEGA0):
    """T → s."""
    return np.asarray(T, float) / fourier_factor(w0)


def morlet_time(eta, w0=OMEGA0):
    """Комплексное ядро во времени ψ₀(η). Нужно для проверки допустимости."""
    eta = np.asarray(eta, float)
    return PSI0_AT_ZERO * np.exp(1j * w0 * eta) * np.exp(-0.5 * eta * eta)


def morlet_hat(s, w, w0=OMEGA0, dt=1.0):
    """Образ ядра ψ̂(sω) с нормировкой T&C: (2πs/δt)^(1/2)·ψ̂₀(sω)."""
    w = np.asarray(w, float)
    expo = -0.5 * (s * w - w0) ** 2
    return (np.sqrt(2.0 * np.pi * s / dt) * PSI0_AT_ZERO
            * np.exp(expo) * (w > 0.0))


def admissibility_residual(w0=OMEGA0):
    """|∫ψ₀ dη| — аналитический остаток допустимости (в идеале ноль)."""
    return PSI0_AT_ZERO * np.sqrt(2.0 * np.pi) * np.exp(-0.5 * w0 * w0)


def make_scales(n, dt=1.0, dj=1.0 / 24.0, s0=None, j_tot=None, w0=OMEGA0):
    """Логарифмическая сетка масштабов s_j = s0·2^(j·dj)."""
    if s0 is None:
        s0 = 2.0 * dt                      # мельче Найквиста смысла нет
    if j_tot is None:
        j_tot = int(np.floor(np.log2(n * dt / s0) / dj))
    j = np.arange(j_tot + 1)
    return s0 * 2.0 ** (j * dj)


# ---------------------------------------------------------------- CWT

def coi_edge_period(n, dt=1.0, w0=OMEGA0):
    """Наибольший ДОПУСТИМЫЙ период в каждой точке ряда (граница конуса).

    Условие «вейвлет не вылез за край»: √2·s ≤ расстояние до ближайшего
    края. Отсюда T ≤ ff·d/√2.
    """
    idx = np.arange(n)
    d = np.minimum(idx, n - 1 - idx) * dt
    return fourier_factor(w0) * d / np.sqrt(2.0)


def cwt(x, dt=1.0, dj=1.0 / 24.0, s0=None, j_tot=None, w0=OMEGA0,
        pad=True, remove_mean=True):
    """Непрерывное вейвлет-преобразование Морле через FFT-свёртку.

    Возвращает словарь:
      W        — комплексные коэффициенты, ВНУТРИ КОНУСА NaN (рабочая версия)
      W_full   — те же коэффициенты без маски (для обратного преобразования)
      in_coi   — bool-маска: True = точка внутри конуса, пользоваться нельзя
      scales   — масштабы s_j
      periods  — фурье-периоды T_j = 1.033·s_j
      power    — |W|² с NaN внутри конуса
      power_rect — |W|²/s (снятие масштабного смещения, Liu et al. 2007)
      amp, phase — |W| и arg(W) с NaN внутри конуса
      coi      — граница конуса в единицах периода, по одному числу на отсчёт
      dt, dj, w0, mean — параметры и снятое среднее
    """
    x = np.asarray(x, float).ravel()
    n0 = x.size
    if n0 < 8:
        raise ValueError("ряд короче 8 отсчётов — вейвлет бессмыслен")
    mean = float(x.mean()) if remove_mean else 0.0
    xz = x - mean

    if pad:
        n = 1
        while n < 2 * n0:               # запас, чтобы циклическая свёртка
            n <<= 1                     # не заворачивала большие масштабы
        xz = np.concatenate([xz, np.zeros(n - n0)])
    else:
        n = n0

    scales = make_scales(n0, dt, dj, s0, j_tot, w0)
    w = 2.0 * np.pi * np.fft.fftfreq(n, d=dt)
    X = np.fft.fft(xz)

    W = np.empty((scales.size, n0), dtype=complex)
    for j, s in enumerate(scales):
        psi = morlet_hat(s, w, w0, dt)
        W[j] = np.fft.ifft(X * np.conj(psi))[:n0]

    periods = scale_to_period(scales, w0)
    idx = np.arange(n0)
    d = np.minimum(idx, n0 - 1 - idx) * dt
    in_coi = (np.sqrt(2.0) * scales[:, None]) > d[None, :]

    Wm = W.copy()
    Wm[in_coi] = np.nan + 1j * np.nan
    pw = np.abs(Wm) ** 2
    return {
        "W": Wm, "W_full": W, "in_coi": in_coi,
        "scales": scales, "periods": periods,
        "power": pw, "power_rect": pw / scales[:, None],
        "amp": np.abs(Wm), "phase": np.angle(Wm),
        "coi": coi_edge_period(n0, dt, w0),
        "dt": dt, "dj": dj, "w0": w0, "mean": mean, "n": n0,
    }


def global_power(res):
    """Глобальный вейвлет-спектр: среднее |W|² по времени, конус исключён.

    Пик этого спектра для чистой синусоиды приходится РОВНО на её
    фурье-период: множитель 4π/(ω₀+√(2+ω₀²)) как раз компенсирует
    смещение √s, вносимое нормировкой T&C. Это аналитический факт,
    он проверяется в self-тесте.
    """
    p = res["power"]
    cnt = np.sum(~res["in_coi"], axis=1)
    g = np.full(p.shape[0], np.nan)
    ok = cnt > 0
    if np.any(ok):
        g[ok] = np.nansum(np.where(res["in_coi"], 0.0, p)[ok], axis=1) / cnt[ok]
    return g


def ridge(res, refine=True):
    """Гребень: период максимума мощности в каждый момент времени.

    Внутри конуса — NaN. refine=True добавляет параболическое уточнение
    по log2(s), это даёт период точнее шага сетки dj.
    """
    p = res["power"]
    per = res["periods"]
    dj = res["dj"]
    n = p.shape[1]
    out = np.full(n, np.nan)
    ok = ~np.all(np.isnan(p), axis=0)
    for t in np.flatnonzero(ok):
        col = p[:, t]
        j = int(np.nanargmax(col))
        lp = np.log2(per[j])
        if refine and 0 < j < col.size - 1:
            a, b, c = col[j - 1], col[j], col[j + 1]
            if np.isfinite(a) and np.isfinite(c):
                den = a - 2.0 * b + c
                if den != 0.0:
                    sh = 0.5 * (a - c) / den
                    if abs(sh) <= 1.0:
                        lp = lp + sh * dj
        out[t] = 2.0 ** lp
    return out


def icwt(res):
    """Обратное преобразование (T&C, ур. 11).

    x_n = dj·√δt/(C_δ·ψ₀(0)) · Σ_j Re(W(s_j,n))/√s_j  + среднее
    Работает по W_full: внутри конуса значения нужны для суммы, просто
    результат у самых краёв менее точен — это честная физика конуса.
    """
    W = res["W_full"]
    s = res["scales"]
    coef = res["dj"] * np.sqrt(res["dt"]) / (C_DELTA * PSI0_AT_ZERO)
    return coef * np.sum(np.real(W) / np.sqrt(s)[:, None], axis=0) + res["mean"]


# ------------------------------------------------- когерентность двух рядов

def _smooth_tw(P, scales, dt, dj):
    """Сглаживание Torrence & Webster 1999: гаусс по времени (σ = s),
    boxcar по масштабу длиной 0.60/dj. Нужно для вейвлет-когерентности:
    без сглаживания она тождественно равна единице."""
    n = P.shape[1]
    k = 2.0 * np.pi * np.fft.fftfreq(n, d=dt)
    F = np.fft.fft(P, axis=1)
    G = np.exp(-0.5 * (scales[:, None] * k[None, :]) ** 2)
    out = np.fft.ifft(F * G, axis=1)
    m = int(round(SCALE_DECORR / dj))
    if m > 1:
        ker = np.ones(m) / m
        pad = m // 2
        ext = np.concatenate([out[:1].repeat(pad, 0), out,
                              out[-1:].repeat(pad, 0)], axis=0)
        sm = np.empty_like(out)
        for j in range(out.shape[0]):
            sm[j] = ext[j:j + m].mean(axis=0)
        out = sm
    return out


def wavelet_coherence(res_x, res_y):
    """Квадрат вейвлет-когерентности R²(s,t) ∈ [0,1] и разность фаз.

    R² = |S(s⁻¹·Wxy)|² / (S(s⁻¹|Wx|²)·S(s⁻¹|Wy|²)),  S — сглаживание.
    Внутри конуса (объединение двух конусов) — NaN.
    """
    Wx, Wy = res_x["W_full"], res_y["W_full"]
    if Wx.shape != Wy.shape:
        raise ValueError("ряды разной длины или разные сетки масштабов")
    s = res_x["scales"]
    dt, dj = res_x["dt"], res_x["dj"]
    inv = 1.0 / s[:, None]
    Wxy = Wx * np.conj(Wy)
    Sxy = _smooth_tw(Wxy * inv, s, dt, dj)
    Sxx = np.real(_smooth_tw(np.abs(Wx) ** 2 * inv, s, dt, dj))
    Syy = np.real(_smooth_tw(np.abs(Wy) ** 2 * inv, s, dt, dj))
    den = Sxx * Syy
    with np.errstate(invalid="ignore", divide="ignore"):
        R2 = np.where(den > 0, np.abs(Sxy) ** 2 / den, np.nan)
    R2 = np.clip(R2, 0.0, 1.0)
    ph = np.angle(Sxy)
    bad = res_x["in_coi"] | res_y["in_coi"]
    R2[bad] = np.nan
    ph[bad] = np.nan
    return {"R2": R2, "phase": ph, "periods": res_x["periods"],
            "scales": s}


def phase_coherence(res_x, res_y, win_factor=2.0):
    """Локальный по времени аналог PLV на каждом масштабе.

    γ(s,t) = |⟨exp(i·(φx−φy))⟩| по гауссову окну шириной σ = win_factor·s.
    Отличие от классического PLV: усреднение НЕ по всему ряду, а по окну,
    пропорциональному масштабу — поэтому виден момент, когда связь есть,
    и момент, когда её нет.
    """
    Wx, Wy = res_x["W_full"], res_y["W_full"]
    s = res_x["scales"]
    dt = res_x["dt"]
    n = Wx.shape[1]
    z = np.exp(1j * (np.angle(Wx) - np.angle(Wy)))
    k = 2.0 * np.pi * np.fft.fftfreq(n, d=dt)
    G = np.exp(-0.5 * ((win_factor * s)[:, None] * k[None, :]) ** 2)
    zs = np.fft.ifft(np.fft.fft(z, axis=1) * G, axis=1)
    gam = np.clip(np.abs(zs), 0.0, 1.0)
    lag = np.angle(zs)
    bad = res_x["in_coi"] | res_y["in_coi"]
    gam[bad] = np.nan
    lag[bad] = np.nan
    return {"plv": gam, "lag": lag, "periods": res_x["periods"]}


# ---------------------------------------------------------------- self-тест

def _peaks(y, rel=0.02):
    """Локальные максимумы выше rel·max — рябь округления не считаем."""
    y = np.nan_to_num(np.asarray(y, float), nan=0.0)
    thr = rel * y.max()
    return [i for i in range(1, len(y) - 1)
            if y[i] > y[i - 1] and y[i] > y[i + 1] and y[i] >= thr]


def _dip(y, i1, i2):
    """Глубина провала между двумя пиками: min(пик)/дно. 1.0 = слитно."""
    lo = min(i1, i2)
    hi = max(i1, i2)
    val = np.min(y[lo:hi + 1])
    return (min(y[i1], y[i2]) / val) if val > 0 else np.inf


if __name__ == "__main__":
    from scipy.signal import hilbert

    ff = fourier_factor()
    print("=" * 72)
    print("МОРЛЕ ω₀=6 · множитель масштаб→период = %.5f" % ff)
    res_adm = admissibility_residual()
    print("остаток допустимости |∫ψ₀| = %.3e "
          "(директива ~1e−5 — это про ω₀≈4.8)" % res_adm)
    assert abs(ff - 1.03300) < 1e-4
    assert res_adm < 1e-7
    # численное среднее дискретизованного ядра
    eta = np.arange(-8.0, 8.0 + 1e-9, 0.001)
    num_mean = abs(np.trapezoid(morlet_time(eta), eta))
    print("численно |∫ψ₀ dη| на сетке 0.001 = %.3e" % num_mean)
    assert num_mean < 1e-6

    # ---------------------------------------------------------------- 1
    print("\n[1] ЧИСТАЯ СИНУСОИДА, период 40")
    N, T_TRUE, A = 1024, 40.0, 1.0
    t = np.arange(N, dtype=float)
    x1 = A * np.cos(2 * np.pi * t / T_TRUE)
    r1 = cwt(x1, dt=1.0, dj=1.0 / 32.0)
    g1 = global_power(r1)
    jmax = int(np.nanargmax(g1))
    # уточнение параболой по log2(T)
    a, b, c = g1[jmax - 1], g1[jmax], g1[jmax + 1]
    sh = 0.5 * (a - c) / (a - 2 * b + c)
    T_hat = 2.0 ** (np.log2(r1["periods"][jmax]) + sh * r1["dj"])
    err1 = abs(T_hat - T_TRUE) / T_TRUE
    # аналитическая амплитуда пика: u* = (ω₀+√(ω₀²+2))/2
    wc = 2 * np.pi / T_TRUE
    u_star = (OMEGA0 + np.sqrt(OMEGA0 ** 2 + 2.0)) / 2.0
    s_star = u_star / wc
    amp_pred = (A / 2.0) * np.sqrt(2 * np.pi * s_star) * PSI0_AT_ZERO * \
        np.exp(-0.5 * (u_star - OMEGA0) ** 2)
    amp_obs = float(np.nanmax(r1["amp"]))
    err_amp = abs(amp_obs - amp_pred) / amp_pred
    print("  период по пику глобального спектра = %.3f (истина 40)"
          "  → ошибка %.3f%%" % (T_hat, 100 * err1))
    print("  |W|max: предсказано аналитически %.4f, измерено %.4f"
          "  → расхождение %.3f%%" % (amp_pred, amp_obs, 100 * err_amp))
    assert err1 < 0.05, err1
    assert err1 < 0.01, ("ожидали куда лучше 5%%", err1)
    assert err_amp < 0.02, err_amp

    # ---------------------------------------------------------------- 2
    print("\n[2] ЧИРП: частота растёт линейно")
    N2 = 2048
    t2 = np.arange(N2, dtype=float)
    f0, f1 = 1.0 / 80.0, 1.0 / 12.0
    kk = (f1 - f0) / N2
    x2 = np.cos(2 * np.pi * (f0 * t2 + 0.5 * kk * t2 ** 2))
    r2 = cwt(x2, dt=1.0, dj=1.0 / 32.0)
    rg = ridge(r2)
    good = np.isfinite(rg)
    # берём только середину, где конус не мешает и период разрешён
    lo, hi = int(0.15 * N2), int(0.85 * N2)
    seg = rg[lo:hi]
    assert np.all(np.isfinite(seg))
    d = np.diff(seg)
    frac_down = float(np.mean(d <= 1e-9))
    T_true2 = 1.0 / (f0 + kk * t2[lo:hi])
    rel = np.abs(seg - T_true2) / T_true2
    print("  гребень: %d точек вне конуса, монотонно вниз %.2f%% шагов"
          % (int(good.sum()), 100 * frac_down))
    print("  период гребня от %.2f до %.2f (истина от %.2f до %.2f)"
          % (seg[0], seg[-1], T_true2[0], T_true2[-1]))
    print("  медианная относительная ошибка периода = %.3f%%"
          % (100 * np.median(rel)))
    assert frac_down > 0.999, frac_down
    assert np.median(rel) < 0.03, np.median(rel)
    assert seg[0] > seg[-1] * 3, "гребень обязан съехать в разы"

    # ---------------------------------------------------------------- 3
    print("\n[3] ДЕЛЬТА-ВСПЛЕСК")
    N3, N0 = 1024, 400
    x3 = np.zeros(N3)
    x3[N0] = 1.0
    r3 = cwt(x3, dt=1.0, dj=1.0 / 16.0, remove_mean=False)
    small = np.flatnonzero(r3["periods"] <= 16.0)
    devs = []
    for j in small:
        row = r3["amp"][j]
        if np.all(np.isnan(row)):
            continue
        devs.append(int(np.nanargmax(row)) - N0)
    devs = np.array(devs)
    print("  масштабов с T≤16: %d, смещение максимума от %d до %d отсчётов"
          % (len(devs), devs.min(), devs.max()))
    assert len(devs) >= 8
    assert np.all(np.abs(devs) <= 2), devs

    # ---------------------------------------------------------------- 4
    print("\n[4] СУММА ДВУХ СИНУСОИД: T=20 и T=60 — CWT vs ФУРЬЕ vs ГИЛЬБЕРТ")
    N4 = 4096
    t4 = np.arange(N4, dtype=float)
    TA, TB, AA, AB = 20.0, 60.0, 1.0, 1.0
    x4 = AA * np.sin(2 * np.pi * t4 / TA) + AB * np.sin(2 * np.pi * t4 / TB)

    # --- CWT
    r4 = cwt(x4, dt=1.0, dj=1.0 / 32.0)
    g4 = global_power(r4)
    per4 = r4["periods"]
    pk = _peaks(g4)
    pk = sorted(pk, key=lambda i: -g4[i])[:2]
    pk = sorted(pk, key=lambda i: per4[i])
    Tc = [per4[i] for i in pk]
    dip_cwt = _dip(g4, pk[0], pk[1]) if len(pk) == 2 else 1.0
    print("  CWT     : максимумов %d, периоды %s, ошибка %.2f%% / %.2f%%, "
          "провал ×%.1f"
          % (len(_peaks(g4)), " и ".join("%.2f" % v for v in Tc),
             100 * abs(Tc[0] - TA) / TA, 100 * abs(Tc[1] - TB) / TB, dip_cwt))

    # --- Фурье (периодограмма на той же сетке периодов)
    Xf = np.abs(np.fft.rfft(x4 - x4.mean())) ** 2
    fr = np.fft.rfftfreq(N4, d=1.0)
    with np.errstate(divide="ignore"):
        perf = np.where(fr > 0, 1.0 / np.maximum(fr, 1e-12), np.inf)
    sel = (perf >= per4[0]) & (perf <= per4[-1])
    pf, vf = perf[sel][::-1], Xf[sel][::-1]     # по возрастанию периода
    pkf = sorted(_peaks(vf), key=lambda i: -vf[i])[:2]
    pkf = sorted(pkf, key=lambda i: pf[i])
    Tf = [pf[i] for i in pkf]
    dip_fft = _dip(vf, pkf[0], pkf[1])
    print("  ФУРЬЕ   : максимумов 2, периоды %s, ошибка %.2f%% / %.2f%%, "
          "провал ×%.3g"
          % (" и ".join("%.2f" % v for v in Tf),
             100 * abs(Tf[0] - TA) / TA, 100 * abs(Tf[1] - TB) / TB, dip_fft))

    # --- Гильберт: ОДНА мгновенная фаза на весь ряд
    z4 = hilbert(x4 - x4.mean())
    ph4 = np.unwrap(np.angle(z4))
    inst_w = np.gradient(ph4)                    # рад/отсчёт
    core = slice(200, N4 - 200)
    iw = inst_w[core]
    ok_w = iw[iw > 1e-6]
    inst_T = 2 * np.pi / ok_w
    T_med = float(np.median(inst_T))
    # аналитика: при равных амплитудах мгн. частота = (ω1+ω2)/2 ⇒
    # период = гармоническое среднее 2·TA·TB/(TA+TB) = 30.0
    T_harm = 2 * TA * TB / (TA + TB)
    hist, edges = np.histogram(np.log2(np.clip(inst_T, 1, 1e3)), bins=60)
    hist = hist.astype(float)
    ctr = 2.0 ** (0.5 * (edges[1:] + edges[:-1]))
    ph_peaks = _peaks(hist, rel=0.05)
    near = np.mean((np.abs(inst_T - TA) / TA < 0.05) |
                   (np.abs(inst_T - TB) / TB < 0.05))
    hA = hist[int(np.argmin(np.abs(ctr - TA)))] / hist.max()
    hB = hist[int(np.argmin(np.abs(ctr - TB)))] / hist.max()
    print("  ГИЛЬБЕРТ: медианный мгновенный период %.3f "
          "(аналитика: гармоническое среднее %.1f)" % (T_med, T_harm))
    print("            заметных максимумов в гистограмме периодов: %d, "
          "главный при T=%.2f" % (len(ph_peaks), ctr[int(np.argmax(hist))]))
    print("            высота гистограммы у истинных периодов: "
          "T=20 → %.3f от пика, T=60 → %.3f от пика" % (hA, hB))
    print("            доля времени, когда мгн. период в ±5%% от 20 или 60:"
          " %.2f%%" % (100 * near))
    print("            ошибка относительно компонент: %+.1f%% и %+.1f%%"
          % (100 * (T_med - TA) / TA, 100 * (T_med - TB) / TB))

    assert len(pk) == 2 and dip_cwt > 3.0, dip_cwt
    assert abs(Tc[0] - TA) / TA < 0.03 and abs(Tc[1] - TB) / TB < 0.03
    assert dip_fft > 3.0
    assert abs(T_med - T_harm) / T_harm < 0.02, (T_med, T_harm)
    assert near < 0.05, near          # Гильберт почти никогда не прав
    assert len(ph_peaks) == 1, ph_peaks           # ОДНА мода вместо двух
    assert abs(ctr[int(np.argmax(hist))] - T_harm) / T_harm < 0.06
    assert hA < 0.05 and hB < 0.05, (hA, hB)      # у истинных периодов пусто

    # --- 4b: то же ДВА тона, но ПОСЛЕДОВАТЕЛЬНО. Фурье их не различает.
    print("\n[4b] НЕСТАЦИОНАРНОСТЬ: «20 потом 60» против «60 потом 20»")
    # Два ряда с ОДИНАКОВЫМ набором компонент, но обратным порядком во
    # времени. Периодограмма обязана быть почти одна и та же — Фурье
    # инвариантен к перестановке кусков. Вейвлет обязан их различить.
    hlf = N4 // 2
    x_ab = np.concatenate([np.sin(2 * np.pi * t4[:hlf] / TA),
                           np.sin(2 * np.pi * t4[:hlf] / TB)])
    x_ba = np.concatenate([np.sin(2 * np.pi * t4[:hlf] / TB),
                           np.sin(2 * np.pi * t4[:hlf] / TA)])
    Sab = np.abs(np.fft.rfft(x_ab - x_ab.mean())) ** 2
    Sba = np.abs(np.fft.rfft(x_ba - x_ba.mean())) ** 2
    l1 = 0.5 * np.abs(Sab / Sab.sum() - Sba / Sba.sum()).sum()
    d_sig = 0.5 * np.abs(np.abs(x_ab) / np.abs(x_ab).sum()
                         - np.abs(x_ba) / np.abs(x_ba).sum()).sum()
    r_ab, r_ba = cwt(x_ab, dj=1.0 / 32.0), cwt(x_ba, dj=1.0 / 32.0)
    g_ab, g_ba = ridge(r_ab), ridge(r_ba)
    q1 = float(np.nanmedian(g_ab[int(0.15 * N4):int(0.40 * N4)]))
    q2 = float(np.nanmedian(g_ab[int(0.60 * N4):int(0.85 * N4)]))
    p1 = float(np.nanmedian(g_ba[int(0.15 * N4):int(0.40 * N4)]))
    p2 = float(np.nanmedian(g_ba[int(0.60 * N4):int(0.85 * N4)]))
    both = np.isfinite(g_ab) & np.isfinite(g_ba)
    d_cwt = float(np.mean(np.abs(np.log2(g_ab[both] / g_ba[both]))))
    fin = np.flatnonzero(np.isfinite(g_ab))
    mid = fin[(g_ab[fin] > 0.5 * (TA + TB)) & (fin > N4 * 0.3)]
    t_switch = int(mid.min()) if mid.size else -1
    print("  ряды различны: L1 по |x| = %.3f, но L1 спектров = %.4f "
          "(0 = Фурье их не различает вовсе)" % (d_sig, l1))
    print("  CWT: «20→60» гребень %.2f → %.2f;  «60→20» гребень %.2f → %.2f;"
          "  среднее |log2| расхождение гребней = %.3f октавы"
          % (q1, q2, p1, p2, d_cwt))
    print("  момент переключения найден на отсчёте %d (истина %d, "
          "ошибка %.1f%% длины ряда)"
          % (t_switch, hlf, 100 * abs(t_switch - hlf) / N4))
    assert abs(q1 - TA) / TA < 0.05 and abs(q2 - TB) / TB < 0.05
    assert abs(p1 - TB) / TB < 0.05 and abs(p2 - TA) / TA < 0.05
    assert abs(t_switch - hlf) < 0.06 * N4
    assert l1 < 0.02, l1                  # спектры совпали
    assert d_cwt > 1.0, d_cwt             # гребни разошлись более чем вдвое

    # ---------------------------------------------------------------- 5
    print("\n[5] КОНУС ВЛИЯНИЯ")
    r5 = cwt(np.sin(2 * np.pi * t / 25.0), dt=1.0, dj=1.0 / 16.0)
    inside = r5["in_coi"]
    vals = r5["W"]
    n_nan_in = int(np.isnan(vals.real)[inside].sum())
    n_fin_out = int(np.isfinite(vals.real)[~inside].sum())
    n_bad_out = int(np.isnan(vals.real)[~inside].sum())
    frac = inside.mean()
    print("  ячеек всего %d, внутри конуса %d (%.1f%%)"
          % (inside.size, inside.sum(), 100 * frac))
    print("  внутри конуса NaN: %d/%d; вне конуса конечных: %d, NaN: %d"
          % (n_nan_in, int(inside.sum()), n_fin_out, n_bad_out))
    # граница конуса проверяется аналитически: T_max(t) = ff·d/√2
    cc = r5["coi"]
    j_probe, t_probe = 30, 300
    allowed = cc[t_probe]
    print("  в точке t=%d допустимый период ≤ %.2f; масштаб T=%.2f %s"
          % (t_probe, allowed, r5["periods"][j_probe],
             "запрещён" if inside[j_probe, t_probe] else "разрешён"))
    assert n_nan_in == int(inside.sum())
    assert n_bad_out == 0
    assert 0.05 < frac < 0.75
    assert abs(cc[0]) < 1e-12 and cc[N // 2] > cc[10]

    # ---------------------------------------------------------------- 6
    print("\n[6] СОХРАНЕНИЕ ЭНЕРГИИ / ОБРАТНОЕ ПРЕОБРАЗОВАНИЕ")
    N6 = 2048
    t6 = np.arange(N6, dtype=float)
    x6 = (np.sin(2 * np.pi * t6 / 17.0) + 0.7 * np.sin(2 * np.pi * t6 / 55.0)
          + 0.4 * np.sin(2 * np.pi * t6 / 190.0) + 3.0)
    r6 = cwt(x6, dt=1.0, dj=1.0 / 32.0)
    rec = icwt(r6)
    rms = np.sqrt(np.mean((x6 - x6.mean()) ** 2))
    err_full = np.sqrt(np.mean((rec - x6) ** 2)) / rms
    ii = slice(int(0.1 * N6), int(0.9 * N6))
    err_in = np.sqrt(np.mean((rec[ii] - x6[ii]) ** 2)) / rms
    # энергия: Σ|W|²·dj·dt/(C_δ·s) должна дать дисперсию ряда
    e_w = (r6["dj"] * r6["dt"] / C_DELTA) * np.sum(
        np.abs(r6["W_full"]) ** 2 / r6["scales"][:, None]) / N6
    var = float(np.var(x6))
    print("  реконструкция: RMS-ошибка по всему ряду %.3f%%, "
          "по середине (10–90%%) %.3f%%" % (100 * err_full, 100 * err_in))
    print("  дисперсия ряда %.5f, восстановленная из |W|² %.5f "
          "→ расхождение %.2f%%" % (var, e_w, 100 * abs(e_w - var) / var))
    assert err_full < 0.05, err_full
    assert err_in < 0.02, err_in
    assert abs(e_w - var) / var < 0.05

    # ---------------------------------------------------------------- 7
    print("\n[7] ВЕЙВЛЕТ-КОГЕРЕНТНОСТЬ (локальный аналог PLV)")
    N7 = 2048
    t7 = np.arange(N7, dtype=float)
    TS = 64.0
    lag = np.pi / 3.0
    a7 = np.sin(2 * np.pi * t7 / TS)
    b7 = np.sin(2 * np.pi * t7 / TS - lag)
    ra, rb = cwt(a7, dj=1.0 / 24.0), cwt(b7, dj=1.0 / 24.0)
    co = wavelet_coherence(ra, rb)
    pc = phase_coherence(ra, rb)
    jt = int(np.argmin(np.abs(ra["periods"] - TS)))
    R2_line = np.nanmean(co["R2"][jt])
    lag_hat = float(np.nanmedian(pc["lag"][jt]))
    plv_line = float(np.nanmean(pc["plv"][jt]))
    print("  один и тот же тон со сдвигом π/3: R² = %.4f, PLV = %.4f, "
          "сдвиг %.4f рад (истина %.4f)"
          % (R2_line, plv_line, lag_hat, lag))
    assert R2_line > 0.95 and plv_line > 0.95
    assert abs(lag_hat - lag) < 0.02

    # связь только на ПЕРВОЙ половине — локальность против классического PLV
    half = N7 // 2
    c7 = np.concatenate([np.sin(2 * np.pi * t7[:half] / TS - lag),
                         np.sin(2 * np.pi * t7[half:] / TS
                                - lag - 4 * np.pi * t7[half:] / (7 * TS))])
    rc = cwt(c7, dj=1.0 / 24.0)
    pc2 = phase_coherence(ra, rc)
    g_first = float(np.nanmean(pc2["plv"][jt, int(0.15 * N7):int(0.40 * N7)]))
    g_second = float(np.nanmean(pc2["plv"][jt, int(0.60 * N7):int(0.85 * N7)]))
    ph_a = np.angle(hilbert(a7))
    ph_c = np.angle(hilbert(c7))
    plv_global = float(np.abs(np.mean(np.exp(1j * (ph_a - ph_c)))))
    print("  связь только в 1-й половине: локальный PLV %.3f → %.3f, "
          "глобальный PLV по Гильберту %.3f (одно число на весь ряд)"
          % (g_first, g_second, plv_global))
    assert g_first > 0.9
    assert g_second < 0.5
    assert g_first - g_second > 0.4

    print("\n" + "=" * 72)
    print("ВСЕ SELF-ТЕСТЫ ПРОЙДЕНЫ")
    print("⚫ Это инструмент измерения, а не сигнал и не приказ.")
