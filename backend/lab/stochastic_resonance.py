"""СТОХАСТИЧЕСКИЙ РЕЗОНАНС — Бенци, Сутера, Вульпиани, J. Phys. A 14, L453 (1981).

Зачем это здесь. Линейные методы (Фурье, Гильберт, Пирсон, PLV) уже отработали:
направление цены по небу — ноль, углы в линейной метрике — p=0.51, веса школы как
множители — p=0.51. Выжила одна огибающая E(t) медленных тел. Линейная метрика
задаёт вопрос «сдвигает ли небо среднее» и на него честно отвечает «нет».

Стохастический резонанс задаёт ДРУГОЙ вопрос. Толпа сидит не на прямой, а в
двуямном потенциале: яма «дома» и яма «заказал». Слабая макро-накачка (огибающая
Юпитера и Сатурна, сезон) ПО ОПРЕДЕЛЕНИЮ подпороговая — сама она через барьер не
перебрасывает, и никакая линейная регрессия её не увидит. Но шум нужной силы
делает переходы возможными, и они идут В ТАКТ с накачкой. Отклик при этом
НЕМОНОТОНЕН по силе шума: слишком тихо — переходов нет, слишком громко — переходы
есть, но накачку не слышно. Между ними — максимум.

🔵 МЕХАНИКА (проверяемо независимо, ответы известны из литературы):
    динамика   ẋ = x − x³ + A·cos(Ωt) + ξ(t)
    потенциал  U(x) = −x²/2 + x⁴/4,  минимумы x=±1,  барьер ΔU = 1/4
    порог      A_c = 2/(3√3) = 0.384900 — выше него накачка сама валит барьер
    Крамерс    r_K = (1/(√2·π))·exp(−ΔU/D)
    резонанс   2·r_K = Ω/(2π)  (условие директивы; в RMP 70,223 встречается и
               другая конвенция 2·T_K = T_Ω — обе считаются, обе печатаются)
    отклик     γ²(Ω) — квадрат когерентности вход-выход на частоте накачки.
               γ² = |⟨Y_k⟩|²/⟨|Y_k|²⟩ — доля мощности отклика на частоте Ω,
               когерентная с накачкой. Это нормированный SNR: монотонная
               функция от S/N, значит argmax тот же, а величина ограничена [0,1]
               и не скачет от оценки шумовой полки.

🟡 ЯЗЫК ШКОЛЫ (прокси названы прямо):
  · единица времени модели = 1 сутки. Это ПРОКСИ времени релаксации толпы
    («решаю сегодня или нет»), а не измеренная величина;
  · связь «эфир → координата x» безразмерна и школой не откалибрована. Поэтому
    калибровка фиксируется явно: медиана D_eff по окну приравнивается к
    крамерсовскому оптимуму. Абсолютное значение D после этого НЕ информативно —
    информативна ФОРМА кривой и разброс D_eff/median(D_eff), а он от калибровки
    не зависит вовсе;
  · «яма 1 дома / яма 2 заказал» — метафора школы, а не измеренный потенциал.

⚫ НЕ СИГНАЛ. Ни вероятность перехода, ни сила ξ ничего не предписывают человеку
и ничего не предсказывают про события. Это геометрия неба, пересчитанная в
язык одной физической модели.

ПРО RANDOM. Интегрирование стохастического уравнения без генератора невозможно,
поэтому генератор живёт ТОЛЬКО в стенде-валидаторе simulate() и требует явного
seed (без него — ValueError). В прикладном пути (ether_noise, hourly_transition)
генератора нет ни одного: ξ(t) берётся из эфемерид.

  python3 -m backend.lab.stochastic_resonance          # пять self-тестов
  python3 -m backend.lab.stochastic_resonance sky      # почасовая кривая ξ
  python3 -m backend.lab.stochastic_resonance resto    # проверка на данных + нуль
"""
import json
import math
import os
import sys
import warnings
from datetime import date, datetime, timedelta, timezone

import numpy as np

from backend import aether
from backend.lab import morlet

# ══════════════════════════════════════════════════════════════════════
# 🔵 ЧАСТЬ 1. АНАЛИТИКА ДВУЯМНОГО ПОТЕНЦИАЛА — ни одного случайного числа
# ══════════════════════════════════════════════════════════════════════
BARRIER = 0.25                              # ΔU = U(0) − U(±1)
A_CRIT = 2.0 / (3.0 * math.sqrt(3.0))       # 0.3849001795 — порог бистабильности
KRAMERS_PREF = 1.0 / (math.sqrt(2.0) * math.pi)   # 0.2250790790


def potential(x, F=0.0):
    """U(x) = −x²/2 + x⁴/4 − F·x. F — статический наклон (мгновенная накачка)."""
    x = np.asarray(x, float)
    return -0.5 * x * x + 0.25 * x ** 4 - F * x


def force(x, F=0.0):
    """−dU/dx = x − x³ + F."""
    x = np.asarray(x, float)
    return x - x * x * x + F


def extrema(F):
    """Точные экстремумы наклонённого потенциала: корни x − x³ + F = 0.

    Тригонометрическое решение депрессированной кубики (Виет). Три вещественных
    корня существуют ровно при |F| < A_c — это тот же порог 2/(3√3), что и в
    литературе, и это проверяется self-тестом.

    Возвращает (x_лев, x_верх, x_прав); при |F| ≥ A_c — (nan, nan, nan).
    """
    F = np.asarray(F, float)
    u = 1.5 * math.sqrt(3.0) * F                       # = (3√3/2)·F, |u|<1 ⇔ |F|<A_c
    bad = np.abs(u) >= 1.0
    phi = np.arccos(np.clip(u, -1.0, 1.0)) / 3.0
    c = 2.0 / math.sqrt(3.0)
    x_r = c * np.cos(phi)
    x_t = c * np.cos(phi - 2.0 * np.pi / 3.0)
    x_l = c * np.cos(phi - 4.0 * np.pi / 3.0)
    if np.ndim(F) == 0:
        if bad:
            return (float("nan"),) * 3
        return float(x_l), float(x_t), float(x_r)
    x_l = np.where(bad, np.nan, x_l)
    x_t = np.where(bad, np.nan, x_t)
    x_r = np.where(bad, np.nan, x_r)
    return x_l, x_t, x_r


def barrier_heights(F):
    """(ΔU из левой ямы, ΔU из правой ямы) при наклоне F.

    В первом порядке по F: ΔU_лев = 1/4 − F, ΔU_прав = 1/4 + F. Точная формула
    учитывает смещение самих экстремумов; расхождение — O(F²) и проверяется.
    """
    xl, xt, xr = extrema(F)
    ut = potential(xt, F)
    return ut - potential(xl, F), ut - potential(xr, F)


def kramers_rate(D, dU=BARRIER):
    """r_K = (1/(√2·π))·exp(−ΔU/D). D — интенсивность шума ⟨ξ(t)ξ(0)⟩=2D·δ(t)."""
    D = np.asarray(D, float)
    dU = np.asarray(dU, float)
    out = np.where(D > 0.0, KRAMERS_PREF * np.exp(-dU / np.where(D > 0, D, 1.0)),
                   0.0)
    return float(out) if np.ndim(D) == 0 and np.ndim(dU) == 0 else out


def d_opt_kramers(Omega, dU=BARRIER):
    """Оптимум из условия директивы 2·r_K = Ω/(2π).

    exp(−ΔU/D) = √2·Ω/4  ⇒  D = ΔU / ln(4/(√2·Ω)).
    """
    arg = 4.0 / (math.sqrt(2.0) * Omega)
    if arg <= 1.0:
        return float("nan")          # частота так высока, что условия нет
    return dU / math.log(arg)


def d_opt_timescale(Omega, dU=BARRIER):
    """Альтернативная конвенция «два среднеожидания = период»: 2/r_K = 2π/Ω.

    Печатается справочно, чтобы не выдавать одну конвенцию за единственную.
    """
    arg = math.pi * KRAMERS_PREF / Omega * 1.0
    # r_K = Ω/π  ⇒  exp(−ΔU/D) = Ω/(π·pref)
    val = Omega / (math.pi * KRAMERS_PREF)
    if val <= 0.0 or val >= 1.0:
        return float("nan")
    return -dU / math.log(val)


# ══════════════════════════════════════════════════════════════════════
# 🔵 ЧАСТЬ 2. СТЕНД-ВАЛИДАТОР — ЕДИНСТВЕННОЕ МЕСТО С ГЕНЕРАТОРОМ
# ══════════════════════════════════════════════════════════════════════
def simulate(D, A, Omega, t_end, dt=0.01, x0=-1.0, seed=None,
             decim=20, n_paths=1, block=4000, drive=None):
    """Эйлер–Маруяма для ẋ = x − x³ + A·cos(Ωt) + ξ, ⟨ξξ⟩ = 2D·δ.

    D — скаляр или вектор: весь список интенсивностей считается ОДНИМ проходом,
    поэтому сетка по D стоит столько же времени, сколько одна точка.

    seed ОБЯЗАТЕЛЕН: без него ValueError. Это граница закона «никакого random» —
    стенд имеет право на генератор только с явно зафиксированным зерном.

    drive — необязательная ГОТОВАЯ накачка длиной nstep = t_end/dt вместо
    A·cos(Ωt). Нужна ровно для одной проверки: подать накачку той же частоты,
    амплитуды и мощности, но с ПЕРЕМЕШАННЫМИ фазами. Если пик кривой отклика
    переживёт такую подмену, значит меряется не резонанс, а «шум добавил
    движения».

    Возвращает {"x": (n_D, n_paths, n_out), "t", "D", "A", "Omega", "dt_out"}.
    """
    if seed is None:
        raise ValueError("simulate: зерно обязательно — стенд должен быть "
                         "воспроизводим побитово")
    Dv = np.atleast_1d(np.asarray(D, float))
    if np.any(Dv < 0):
        raise ValueError("интенсивность шума D не может быть отрицательной")
    nstep = int(round(t_end / dt))
    if nstep % decim:
        raise ValueError("длина ряда должна делиться на прореживание decim")
    nout = nstep // decim
    if drive is not None:
        drive = np.asarray(drive, float).ravel()
        if drive.size != nstep:
            raise ValueError(f"drive должен быть длиной {nstep}, а он {drive.size}")
    rng = np.random.default_rng(int(seed))
    x = np.full((Dv.size, n_paths), float(x0))
    out = np.empty((Dv.size, n_paths, nout), float)
    sig = np.sqrt(2.0 * Dv * dt)[:, None]
    quiet = not np.any(Dv > 0.0)

    step, j = 0, 0
    while step < nstep:
        nb = min(block, nstep - step)
        drv = (drive[step:step + nb] if drive is not None
               else A * np.cos(Omega * (np.arange(step, step + nb) * dt)))
        noise = (None if quiet
                 else rng.standard_normal((nb, Dv.size, n_paths)))
        for b in range(nb):
            x = x + (x - x * x * x + drv[b]) * dt
            if not quiet:
                x = x + sig * noise[b]
            if (step + b + 1) % decim == 0:
                out[:, :, j] = x
                j += 1
        step += nb
    t = (np.arange(1, nout + 1) * decim * dt)
    return {"x": out, "t": t, "D": Dv, "A": float(A), "Omega": float(Omega),
            "dt_out": decim * dt}


def count_transitions(x, thr=0.5):
    """Число переходов между ямами по триггеру Шмитта на порогах ±thr.

    Дрожание около дна ямы переходом не считается — засчитывается только
    достижение противоположного порога.
    """
    x = np.asarray(x, float)
    if x.ndim > 1:
        flat = x.reshape(-1, x.shape[-1])
        return np.array([count_transitions(r, thr) for r in flat],
                        int).reshape(x.shape[:-1])
    s = np.zeros(x.size, np.int8)
    s[x > thr] = 1
    s[x < -thr] = -1
    s = s[s != 0]
    if s.size < 2:
        return 0
    return int(np.count_nonzero(np.diff(s) != 0))


def two_state(x, thr=0.5):
    """Двухпозиционный отклик s(t) ∈ {−1,+1} по триггеру Шмитта на ±thr.

    Так СР и определён в литературе: отклик — это ПЕРЕХОДЫ, а не дрожание на
    дне ямы. Если мерить когерентность по самому x, при малом шуме получится
    почти единица — но не от резонанса, а от линейного внутриямного отклика
    амплитуды χ·A с χ = 1/U''(±1) = 1/2. Такой ответ ничего не говорит о
    перебросах через барьер, поэтому x перед измерением бинаризуется.
    """
    x = np.asarray(x, float)
    s = np.zeros(x.shape, np.int8)
    s[x > thr] = 1
    s[x < -thr] = -1
    n = x.shape[-1]
    idx = np.where(s != 0, np.arange(n), 0)
    np.maximum.accumulate(idx, axis=-1, out=idx)
    out = np.take_along_axis(s, idx, axis=-1).astype(float)
    head = out[..., :1] == 0.0                    # до первого срабатывания
    if np.any(head):
        first = np.where(np.any(s != 0, axis=-1),
                         np.take_along_axis(s, np.argmax(s != 0, axis=-1)[..., None],
                                            axis=-1)[..., 0],
                         np.sign(x[..., 0]))
        out = np.where(out == 0.0, first[..., None], out)
    return out


def coherence_at(drive, x, dt_out, Omega, seg_periods=4, skip_periods=8):
    """γ²(Ω) и SNR отклика на частоте накачки.

    Сегменты содержат ЦЕЛОЕ число периодов накачки, поэтому Ω попадает ровно в
    узел сетки Фурье — утечки нет, окно не нужно. Первые skip_periods периодов
    выбрасываются как переходный процесс.

    γ² = |⟨Y_k⟩|² / ⟨|Y_k|²⟩ — когерентная доля мощности отклика.
    SNR = |⟨Y_k⟩|² / ⟨|Y_k − ⟨Y_k⟩|²⟩ — то же в виде отношения сигнал/шум.

    ВЫРОЖДЕНИЕ. Если отклик не меняется вовсе (ни одного переброса через
    барьер — two_state постоянен), то на частоте накачки p_coh = p_tot = 0,
    и отношение 0/0 в плавающей арифметике даёт РОВНО 1.0, то есть
    «идеальную когерентность» у мёртвого ряда. Это ложь того же класса, что
    заглушка вместо измерения: кривая отклика получала бы фальшивый пик на
    тихом краю сетки. Такие точки помечаются NaN — мерить там нечего.
    """
    per_samp = 2.0 * np.pi / Omega / dt_out
    if abs(per_samp - round(per_samp)) > 1e-9:
        raise ValueError("период накачки не кратен шагу вывода — будет утечка")
    per_samp = int(round(per_samp))
    seg = per_samp * seg_periods
    off = per_samp * skip_periods
    x = np.asarray(x, float)[..., off:]
    n = x.shape[-1]
    nseg = n // seg
    if nseg < 4:
        raise ValueError("меньше четырёх сегментов — оценка бессмысленна")
    xs = x[..., :nseg * seg].reshape(x.shape[:-1] + (nseg, seg))
    Y = np.fft.rfft(xs, axis=-1)[..., seg_periods]
    mY = Y.mean(axis=-1)
    p_coh = np.abs(mY) ** 2
    p_tot = (np.abs(Y) ** 2).mean(axis=-1)
    p_noi = np.maximum(p_tot - p_coh, 1e-300)
    # Порог мёртвой линии: по Парсевалю |Y_k|² ≤ (seg·rms)². Живая линия у
    # идеального меандра даёт p_tot ≈ 0.4·(seg·rms)², мёртвая — 1.6e−33 от
    # неё (машинный ноль). Порог 1e−20 стоит между ними с запасом в 13
    # порядков в обе стороны.
    ms = (xs ** 2).mean(axis=-1).mean(axis=-1)
    dead = p_tot <= 1e-20 * (seg ** 2) * np.maximum(ms, 1e-300)
    g2 = np.clip(p_coh / np.maximum(p_tot, 1e-300), 0.0, 1.0)
    snr = p_coh / p_noi
    n_dead = int(np.count_nonzero(dead))
    return {"gamma2": np.where(dead, np.nan, g2),
            "snr": np.where(dead, np.nan, snr),
            "dead": dead, "n_dead": n_dead, "nseg": nseg}


def shuffled_drive(A, Omega, t_end, dt=0.01, blk_periods=4, seed=20260801):
    """Накачка A·cos(Ωt + φ_k): та же частота, амплитуда и мощность, но фаза
    переставляется каждые blk_periods периодов — когерентности нет.

    Это нуль для кривой отклика: резонанс обязан на такой накачке умереть.
    Зерно обязательно и фиксировано, ряд воспроизводим побитово.
    """
    nstep = int(round(t_end / dt))
    t = np.arange(nstep) * dt
    blk = blk_periods * 2.0 * np.pi / Omega
    k = (t // blk).astype(int)
    phi = np.random.default_rng(int(seed)).uniform(0.0, 2.0 * np.pi, k.max() + 1)
    return A * np.cos(Omega * t + phi[k])


def response_curve(D_grid, A, Omega, n_periods=200, seg_periods=4,
                   skip_periods=8, dt=0.01, decim=20, n_paths=6, seed=20260801,
                   shuffle_phase=False, phase_seed=777):
    """Кривая отклика γ²(D) на сетке D. Один прогон на всю сетку.

    shuffle_phase=True подменяет когерентную накачку на фазово-перемешанную
    той же мощности — нуль, на котором пик обязан пропасть.
    """
    per = 2.0 * np.pi / Omega
    t_end = per * n_periods
    drive = (shuffled_drive(A, Omega, t_end, dt=dt, seed=phase_seed)
             if shuffle_phase else None)
    sim = simulate(D_grid, A, Omega, t_end, dt=dt, seed=seed, decim=decim,
                   n_paths=n_paths, drive=drive)
    drv = A * np.cos(Omega * sim["t"])
    st = two_state(sim["x"])
    co = coherence_at(drv, st, sim["dt_out"], Omega,
                      seg_periods=seg_periods, skip_periods=skip_periods)
    cox = coherence_at(drv, sim["x"], sim["dt_out"], Omega,
                       seg_periods=seg_periods, skip_periods=skip_periods)
    # усреднение по путям ансамбля; мёртвые пути (ни одного переброса) — NaN и
    # в среднее не входят. Если мертвы ВСЕ пути точки D, ответ честно NaN.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        g2 = np.nanmean(co["gamma2"], axis=1)
        snr = np.nanmean(co["snr"], axis=1)
        g2x = np.nanmean(cox["gamma2"], axis=1)
    tr = count_transitions(sim["x"]).mean(axis=1)
    return {"D": sim["D"], "gamma2": g2, "snr": snr, "trans": tr,
            "gamma2_x": g2x,
            "dead_paths": co["dead"].sum(axis=1),
            "rate": tr / t_end,
            "t_end": t_end, "nseg": co["nseg"], "sim": sim}


# ══════════════════════════════════════════════════════════════════════
# 🟡 ЧАСТЬ 3. ПРИКЛАДНОЙ ПУТЬ — ξ(t) И НАКАЧКА ИЗ ЭФЕМЕРИД, RANDOM НЕТ
# ══════════════════════════════════════════════════════════════════════
FAST_BODIES = ["Луна", "Меркурий", "Венера"]     # быстрые: источник ξ
SLOW_BODIES = ["Юпитер", "Сатурн"]               # медленные: источник накачки
FAST_CUT_D = 30.0        # ξ — полосы КОРОЧЕ 30 суток
FAST_LOW_D = 1.0         # нижняя граница полосы (ниже суток содержания нет)
SLOW_LO_D = 150.0        # накачка — полоса 150…900 суток (синодика Ю и С)
SLOW_HI_D = 700.0
PAD_FAST_D = 120.0       # поле под конус для полосы ≤30 сут (нужно ≥42)
PAD_SLOW_D = 1100.0      # поле под конус для полосы ≤700 сут (нужно ≥959)
WIN_DAYS = 30.0          # окно локальной силы шума
DJ = 1.0 / 12.0
S0_DAYS = 0.5

RESTO = ("/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/"
         "scratchpad/resto/resto/data/stores.json")
MOEX = ("/tmp/claude-0/-home-user-Real-Sky-/12d14a2b-0898-5be9-8dc7-e6991ee62a59/"
        "scratchpad/ds/moex_hist.json")


def band_part(x, dt_days, pmin, pmax, dj=DJ):
    """Полосовая реконструкция Морле (T&C ур. 11, суммирование по подмножеству
    масштабов). Среднее НЕ возвращается: полоса не содержит постоянной."""
    res = morlet.cwt(np.asarray(x, float), dt=dt_days, dj=dj,
                     s0=max(S0_DAYS, 2.0 * dt_days))
    per = res["periods"]
    sel = (per >= pmin) & (per <= pmax)
    if not np.any(sel):
        return np.zeros(len(x)), res
    W = res["W_full"][sel]
    s = res["scales"][sel]
    coef = res["dj"] * math.sqrt(res["dt"]) / (morlet.C_DELTA * morlet.PSI0_AT_ZERO)
    return coef * np.sum(np.real(W) / np.sqrt(s)[:, None], axis=0), res


def _movmean(x, w):
    """Скользящее среднее по центрированному окну w отсчётов, края — отражением."""
    x = np.asarray(x, float)
    if w < 2:
        return x.copy()
    pad = w // 2
    ext = np.concatenate([x[pad:0:-1], x, x[-2:-2 - pad:-1]])
    ker = np.ones(w) / w
    return np.convolve(ext, ker, mode="same")[pad:pad + x.size]


def corr_time(x, dt_days):
    """τ_c = ∫₀^{τ₀} C(τ)dτ / C(0), интеграл до первого нуля автоковариации.

    Для белого шума даёт dt/2, для AR(1) с коэффициентом φ — dt·(½+φ/(1−φ)).
    Второе проверяется self-тестом на аналитически известном ответе.
    """
    x = np.asarray(x, float)
    z = x - x.mean()
    n = z.size
    m = 1
    while m < 2 * n:
        m <<= 1
    F = np.fft.rfft(z, m)
    c = np.fft.irfft(F * np.conj(F), m)[:n]
    if c[0] <= 0:
        return dt_days * 0.5
    c = c / c[0]
    neg = np.flatnonzero(c <= 0)
    k = int(neg[0]) if neg.size else n
    return dt_days * (0.5 + float(c[1:k].sum()))


def ether_split(t0, days, step_h=1.0):
    """Раскладывает эфемериды на медленную накачку и быстрый остаток.

    Два разных поля под конус влияния вейвлета: короткой полосе хватает 120
    суток, длинной нужно почти три года. Поэтому накачка считается на СУТОЧНОЙ
    сетке в широком окне (она гладкая, чаще незачем) и интерполируется на
    рабочую почасовую сетку; ξ считается прямо на почасовой.

    Возвращает массивы уже БЕЗ полей:
      ts    — unix-время рабочего окна
      fastE — E_Луна + E_Меркурий + E_Венера (сырая огибающая быстрых тел)
      xi    — полоса 1…30 суток от fastE — ЭТО И ЕСТЬ ξ(t)
      xi_hp — тот же остаток вторым способом: fastE минус скользящее 30-суточное
              среднее. Держится как перекрёстная проверка построения
      slow  — E_Юпитер + E_Сатурн, полоса 150…700 суток (накачка)
    """
    dt_d = step_h / 24.0
    # ── быстрый канал: почасовая сетка, поле 120 суток
    t_f = t0 - timedelta(days=PAD_FAST_D)
    tr = aether.transit_series(t_f, days * 24.0 + 2.0 * PAD_FAST_D * 24.0, step_h)
    if tr is None:
        return None
    fast = sum(tr["E"][nm] for nm in FAST_BODIES)
    xi_full, res_f = band_part(fast, dt_d, FAST_LOW_D, FAST_CUT_D)
    w30 = int(round(FAST_CUT_D / dt_d))
    xi_hp_full = fast - _movmean(fast, w30)
    npad = int(round(PAD_FAST_D * 24.0 / step_h))
    n_work = int(round(days * 24.0 / step_h)) + 1
    sl = slice(npad, npad + n_work)
    ts = np.asarray(tr["ts"], float)[sl]

    # ── медленный канал: суточная сетка, поле 1100 суток, затем интерполяция
    t_s = t0 - timedelta(days=PAD_SLOW_D)
    trs = aether.transit_series(t_s, (days + 2.0 * PAD_SLOW_D) * 24.0, 24.0)
    if trs is None:
        return None
    slow_raw = sum(trs["E"][nm] for nm in SLOW_BODIES)
    slow_b, res_s = band_part(slow_raw, 1.0, SLOW_LO_D, SLOW_HI_D)
    ts_s = np.asarray(trs["ts"], float)
    slow = np.interp(ts, ts_s, slow_b)

    return {
        "ts": ts, "dt_days": dt_d,
        "fastE": fast[sl], "xi": xi_full[sl], "xi_hp": xi_hp_full[sl],
        "slow": slow, "slow_daily": slow_b, "ts_slow": ts_s,
        "res_fast": res_f, "res_slow": res_s,
        "periods": res_f["periods"],
    }


def drive_period(res_slow, pmin=SLOW_LO_D):
    """Господствующий период накачки — пик глобального вейвлет-спектра."""
    g = morlet.global_power(res_slow)
    per = res_slow["periods"]
    ok = np.isfinite(g) & (per >= pmin)
    if not np.any(ok):
        return None
    idx = np.flatnonzero(ok)
    return float(per[idx[int(np.nanargmax(g[ok]))]])


def hourly_transition(t0, days, A=0.20, win_days=WIN_DAYS, step_h=1.0,
                      gauge="median_to_opt"):
    """ПОЧАСОВАЯ таблица: сила ξ, локальная интенсивность D_eff, барьер и
    вероятность перехода через барьер за час.

    Модель времени: 1 единица = 1 сутки 🟡. r_K возвращается в 1/сутки,
    вероятность перехода за час p = 1 − exp(−r/24).

    gauge задаёт единственный незакреплённый множитель — связь «эфир → x»:
      median_to_opt — медиана D_eff приравнена крамерсовскому оптимуму 🟡;
      unit_var      — ξ приведён к единичной дисперсии (тоже произвол).
    Величина D_eff после калибровки НЕ является измерением. Измерением
    является отношение D_eff/median(D_eff) — оно от калибровки не зависит.
    """
    if not (0.0 <= A < A_CRIT):
        raise ValueError(f"накачка должна быть подпороговой: A < {A_CRIT:.6f}")
    sp = ether_split(t0, days, step_h=step_h)
    if sp is None:
        return None
    dt_d = sp["dt_days"]
    xi = sp["xi"]
    T_drive = drive_period(sp["res_slow"])
    if T_drive is None:
        return None
    Omega = 2.0 * np.pi / T_drive
    D_opt = d_opt_kramers(Omega)

    tau = corr_time(xi, dt_d)
    w = int(round(win_days / dt_d))
    sig2 = np.maximum(_movmean(xi * xi, w) - _movmean(xi, w) ** 2, 0.0)
    D_raw = sig2 * tau                       # ⟨ξξ⟩ интеграл = 2D ⇒ D = σ²·τ_c

    med = float(np.median(D_raw))
    if gauge == "median_to_opt":
        scale = D_opt / med if med > 0 else 1.0
    elif gauge == "unit_var":
        scale = 1.0 / max(float(np.var(xi)), 1e-300) * tau ** 0
    else:
        raise ValueError("неизвестная калибровка")
    D_eff = D_raw * scale
    xi_g = xi * math.sqrt(scale)

    # накачка нормируется по ПИКУ, а не по дисперсии: только тогда A —
    # действительно та амплитуда, которую сравнивают с порогом A_c.
    # Опорная выборка берётся из широкого окна (рабочее окно может быть короче
    # годового цикла, и тогда локальный размах ничего не значит).
    sd = sp["slow_daily"]
    nc = min(800, max(0, (len(sd) - 400) // 2))
    core = sd[nc:len(sd) - nc] if len(sd) - 2 * nc >= 400 else sd
    s_med = float(np.median(core))
    s_pk = float(np.percentile(np.abs(core - s_med), 99.0))
    s = sp["slow"]
    s_n = np.clip((s - s_med) / s_pk, -1.0, 1.0) if s_pk > 0 else np.zeros_like(s)
    F = A * s_n
    dU_l, dU_r = barrier_heights(F)          # из ямы «дома» → наружу
    r_day = kramers_rate(D_eff, dU_l)
    p_hour = 1.0 - np.exp(-r_day / 24.0)
    r_ref = kramers_rate(np.full_like(D_eff, float(np.median(D_eff))), dU_l)
    return {
        "ts": sp["ts"], "xi": xi_g, "xi_raw": xi, "sigma": np.sqrt(sig2 * scale),
        "D_eff": D_eff, "D_opt": D_opt, "ratio": D_eff / D_opt,
        "tau_c_days": tau, "T_drive_days": T_drive, "Omega": Omega,
        "drive": s_n, "F": F, "dU": dU_l, "dU_right": dU_r,
        "r_day": r_day, "p_hour": p_hour,
        "p_hour_ref": 1.0 - np.exp(-r_ref / 24.0),
        "A": A, "scale": scale, "xi_hp": sp["xi_hp"],
        "note": ("🟡 единица времени = 1 сутки (прокси времени релаксации толпы); "
                 "🟡 связь эфир→x закреплена калибровкой " + gauge +
                 ", абсолютное D не измерение; ⚫ не сигнал"),
    }


# ══════════════════════════════════════════════════════════════════════
# ПРОВЕРКА НА ДАННЫХ: подпись СР — накачка слышна лучше при СРЕДНЕМ шуме
# ══════════════════════════════════════════════════════════════════════
def _resto_daily():
    """Дневные остатки заказов по 16 пиццериям: снят день недели и тренд."""
    d = json.load(open(RESTO, encoding="utf-8"))
    out = {}
    for code, st in d.items():
        days = st.get("days") or {}
        rows = sorted(days.items())
        ds, ordv = [], []
        for k, v in rows:
            o = v.get("orders")
            if o is None or float(o) <= 0:
                continue
            ds.append(date.fromisoformat(k))
            ordv.append(float(o))
        if len(ds) < 500:
            continue
        y = np.log(np.array(ordv))
        dow = np.array([x.weekday() for x in ds])
        for k in range(7):
            m = dow == k
            if m.sum() > 5:
                y[m] -= np.median(y[m])
        w = 91
        trend = np.array([np.median(y[max(0, i - w // 2):i + w // 2 + 1])
                          for i in range(y.size)])
        out[code] = (ds, y - trend)
    return out


def _sr_stat(sky_noise, drive, resid, nb=3):
    """Подпись СР: связь накачка↔отклик по корзинам силы шума.

    Корзины по D_eff. Внутри каждой — корреляция накачки с остатком спроса.
    СР предсказывает перевёрнутое U: в СРЕДНЕЙ корзине связь сильнее, чем на
    краях. Статистика = c_середина − (c_тихо + c_громко)/2.
    """
    m = np.isfinite(sky_noise) & np.isfinite(drive) & np.isfinite(resid)
    if m.sum() < 300:
        return None
    q = np.quantile(sky_noise[m], np.linspace(0, 1, nb + 1))
    cs = []
    for i in range(nb):
        lo, hi = q[i], q[i + 1]
        sel = m & (sky_noise >= lo) & (sky_noise <= hi)
        if sel.sum() < 60:
            return None
        a, b = drive[sel], resid[sel]
        if a.std() <= 0 or b.std() <= 0:
            return None
        cs.append(float(np.corrcoef(a, b)[0, 1]))
    cs = np.array(cs)
    return float(np.abs(cs[nb // 2]) - 0.5 * (np.abs(cs[0]) + np.abs(cs[-1]))), cs


# ══════════════════════════════════════════════════════════════════════
# ЗАПУСКИ
# ══════════════════════════════════════════════════════════════════════
def run_sky(t0=None, days=400, step_h=1.0, show_h=72):
    t0 = t0 or datetime(2026, 8, 1, 0, tzinfo=timezone.utc)
    r = hourly_transition(t0, days, step_h=step_h)
    ts = r["ts"]
    print("=" * 92)
    print("ПОЧАСОВАЯ КРИВАЯ ЭФИРНОГО ШУМА ξ И ВЕРОЯТНОСТИ ПЕРЕХОДА ЧЕРЕЗ БАРЬЕР")
    print("=" * 92)
    print(f"окно {datetime.fromtimestamp(ts[0], timezone.utc):%Y-%m-%d} … "
          f"{datetime.fromtimestamp(ts[-1], timezone.utc):%Y-%m-%d}, шаг {step_h:g} ч, "
          f"{len(ts)} отсчётов")
    print(f"накачка A·cos: E_Юпитер+E_Сатурн, полоса {SLOW_LO_D:.0f}…{SLOW_HI_D:.0f} сут; "
          f"господствующий период {r['T_drive_days']:.1f} сут, Ω={r['Omega']:.5f} рад/сут")
    print(f"ξ: E_Луна+E_Меркурий+E_Венера, полоса {FAST_LOW_D:.0f}…{FAST_CUT_D:.0f} сут, "
          f"τ_c={r['tau_c_days']:.3f} сут; амплитуда накачки A={r['A']:.2f} "
          f"(порог A_c={A_CRIT:.4f})")
    print(f"D_opt по условию 2r_K=Ω/2π = {r['D_opt']:.5f};  "
          f"альтернативная конвенция 2T_K=T_Ω = {d_opt_timescale(r['Omega']):.5f}")
    print()
    print("🔵 ЧИСЛА, НЕ ЗАВИСЯЩИЕ ОТ КАЛИБРОВКИ:")
    rr = r["ratio"] / float(np.median(r["ratio"]))
    qs = np.percentile(rr, [1, 5, 25, 50, 75, 95, 99])
    print(f"  сила эфирного шума гуляет в {float(rr.max()/rr.min()):.1f} раза "
          f"(D_eff/медиана: {rr.min():.3f} … {rr.max():.1f})")
    print("  квантили D_eff/медиана 1/5/25/50/75/95/99 %: " +
          " ".join(f"{v:.3g}" for v in qs))
    print(f"  часов в пределах фактора 2 от D_opt: "
          f"{float(np.mean((r['ratio'] > 0.5) & (r['ratio'] < 2.0)) * 100):.1f}%")
    top = int(np.argmax(r["sigma"]))
    print(f"  громче всего эфир {datetime.fromtimestamp(ts[top], timezone.utc):%Y-%m-%d}: "
          f"σ_loc {r['sigma'][top]/np.median(r['sigma']):.1f}× медианы — это станция "
          f"Венеры/Меркурия. 🔵 дата станции — эфемерида, 🟡 ВЫСОТА пика задана "
          f"школьными CHRONO_CAP=50 и EPS_CHRONO=0.02, она не измерена")
    xi = r["xi_raw"]
    sub, _ = band_part(xi, r_dt(step_h), 0.0, 2.0)
    print(f"  доля дисперсии ξ на периодах короче 2 суток: "
          f"{float(np.var(sub) / np.var(xi)) * 100:.3f}% — почасовой структуры "
          f"у геоцентрического эфира нет, вся жизнь на 1…30 сутках")
    print(f"  перекрёстная проверка построения ξ: corr(Морле, высокочастотный "
          f"фильтр 30 сут) = {float(np.corrcoef(xi, r['xi_hp'])[0, 1]):+.4f}")
    print()
    print(" дата UTC            ξ       σ_loc   D/D_opt  накачка    ΔU     r,1/сут    p за час")
    for i in range(0, min(len(ts), show_h), 6):
        d = datetime.fromtimestamp(ts[i], timezone.utc)
        print(f" {d:%Y-%m-%d %H:%M}  {r['xi'][i]:+8.4f} {r['sigma'][i]:8.4f} "
              f"{r['ratio'][i]:8.3f} {r['drive'][i]:+8.3f} {r['dU'][i]:7.4f} "
              f"{r['r_day'][i]:9.5f} {r['p_hour'][i]:11.8f}")
    print()
    q = np.percentile(r["p_hour"], [5, 50, 95])
    qr = np.percentile(r["p_hour_ref"], [5, 50, 95])
    print(f"p за час: 5% {q[0]:.3e}  медиана {q[1]:.3e}  95% {q[2]:.3e}  "
          f"размах ×{q[2]/max(q[0],1e-300):.3g}")
    print(f"  если шум зажать на медиане (работает одна накачка): размах "
          f"×{qr[2]/max(qr[0],1e-300):.3g}")
    print(f"  если накачку выключить (работает один шум): размах ×"
          f"{_swing_noise_only(r):.3g}")
    print("  🟡 размах — экспонента наклона: множитель ≈ exp(2A/D). A=0.20 выбрано "
          "по стенду, а не измерено; при A=0.10 множитель падает на порядки.")
    print(r["note"])
    return r


def r_dt(step_h):
    return step_h / 24.0


def _swing_noise_only(r):
    """Размах p за час, если убрать накачку (ΔU держим на 1/4)."""
    rd = kramers_rate(r["D_eff"], BARRIER)
    p = 1.0 - np.exp(-rd / 24.0)
    a, b = np.percentile(p, [5, 95])
    return float(b / max(a, 1e-300))


def _moex_daily():
    """Дневные ряды MOEX: отклик — |логдоходность| (активность рынка)."""
    raw = json.load(open(MOEX, encoding="utf-8"))
    out = {}
    for tk, rows in raw.items():
        ds = [date.fromisoformat(r[0]) for r in rows]
        cl = np.array([float(r[4]) for r in rows])
        lr = np.abs(np.diff(np.log(cl)))
        y = np.log(lr + 1e-6)
        w = 91
        trend = np.array([np.median(y[max(0, i - w // 2):i + w // 2 + 1])
                          for i in range(y.size)])
        out[tk] = (ds[1:], y - trend)
    return out


def _sr_check(series_by_store, title, nsur=300, seed=20260801):
    """Общий стенд: подпись СР против фазового суррогата и IAAFT."""
    from backend.lab.e_surrogate import phase_surrogate, iaaft
    rng = np.random.default_rng(seed)
    st = series_by_store
    all_d = sorted({d for v in st.values() for d in v[0]})
    d0, d1 = all_d[0], all_d[-1]
    ndays = (d1 - d0).days + 1
    t0 = datetime(d0.year, d0.month, d0.day, 12, tzinfo=timezone.utc)
    print("=" * 92)
    print(f"{title}: {len(st)} рядов, {d0} … {d1} ({ndays} дн)")
    print("=" * 92)
    print("гипотеза СР: связь «медленная накачка ↔ отклик» должна быть СИЛЬНЕЕ "
          "при среднем\nуровне эфирного шума, чем при тихом и при громком "
          "(перевёрнутое U по корзинам D_eff)")
    r = hourly_transition(t0, ndays, step_h=6.0)
    ts = r["ts"]
    dts = [datetime.fromtimestamp(t, timezone.utc).date() for t in ts]
    agg = {}
    for i, d in enumerate(dts):
        a = agg.setdefault(d, [0.0, 0.0, 0])
        a[0] += r["D_eff"][i]; a[1] += r["drive"][i]; a[2] += 1
    dmap = {d: (v[0] / v[2], v[1] / v[2]) for d, v in agg.items()}

    def stat_all(noise_by_date):
        vals, cs = [], []
        for code, (ds, res) in sorted(st.items()):
            nz = np.array([noise_by_date.get(d, np.nan) for d in ds])
            dr = np.array([dmap[d][1] if d in dmap else np.nan for d in ds])
            got = _sr_stat(nz, dr, res)
            if got:
                vals.append(got[0]); cs.append(got[1])
        return np.array(vals), cs

    base = {d: dmap[d][0] for d in dmap}
    v0, cs0 = stat_all(base)
    if v0.size == 0:
        print("не хватило данных")
        return
    print(f"настоящий эфир: медиана статистики {np.median(v0):+.4f}, "
          f"знак совпал {int((v0 > 0).sum())}/{v0.size}")
    mc = np.mean(np.abs(np.array(cs0)), axis=0)
    print(f"средний |corr| накачка↔остаток по корзинам шума: "
          f"тихо {mc[0]:.4f} · середина {mc[1]:.4f} · громко {mc[-1]:.4f}")

    keys = sorted(dmap)
    series = np.array([dmap[k][0] for k in keys])
    for nm, gen in (("ФАЗОВЫЙ СУРРОГАТ", phase_surrogate), ("IAAFT", iaaft)):
        meds, sgn = [], []
        for _ in range(nsur):
            sur = gen(series, rng)
            m, s = stat_all({k: sur[i] for i, k in enumerate(keys)})
            if m.size:
                meds.append(float(np.median(m))); sgn.append(int((m > 0).sum()))
        meds = np.array(meds); sgn = np.array(sgn)
        pm = float((meds >= np.median(v0)).mean())
        ps = float((sgn >= int((v0 > 0).sum())).mean())
        print(f"{nm}: нуль медиана {np.median(meds):+.4f} "
              f"(p95 {np.quantile(meds, 0.95):+.4f}) → p(величина)={pm:.4f}, "
              f"p(единодушие)={ps:.4f} "
              f"{'ВЫШЕ НУЛЯ' if (pm < 0.05 and ps < 0.05) else 'НЕОТЛИЧИМО'}")
    return v0


def run_resto(nsur=300, seed=20260801):
    st = _resto_daily()
    if not st:
        print("нет ресторанных данных — нечего проверять")
        return
    return _sr_check(st, "ПОДПИСЬ СР НА ЗАКАЗАХ ПИЦЦЕРИЙ", nsur, seed)


def run_moex(nsur=300, seed=20260801):
    st = _moex_daily()
    if not st:
        print("нет биржевых данных — нечего проверять")
        return
    return _sr_check(st, "ПОДПИСЬ СР НА АКТИВНОСТИ MOEX (|логдоходность|)",
                     nsur, seed)


# ══════════════════════════════════════════════════════════════════════
# SELF-ТЕСТЫ
# ══════════════════════════════════════════════════════════════════════
def _test_analytic():
    print("─ АНАЛИТИКА ────────────────────────────────────────────────────")
    assert abs(potential(1.0) + 0.25) < 1e-15, "U(1) = −1/4"
    assert abs(potential(-1.0) + 0.25) < 1e-15, "U(−1) = −1/4"
    assert abs(potential(0.0)) < 1e-15, "U(0) = 0"
    assert abs(float(potential(0.0) - potential(1.0)) - BARRIER) < 1e-15
    assert abs(A_CRIT - 0.3849001794597505) < 1e-15, "A_c = 2/(3√3)"
    xl, xt, xr = extrema(0.0)
    assert abs(xl + 1) < 1e-12 and abs(xt) < 1e-12 and abs(xr - 1) < 1e-12
    # корни совпадают с численными
    for F in (0.05, 0.15, 0.3, -0.2):
        a = np.sort(np.array(extrema(F)))
        b = np.sort(np.roots([-1.0, 0.0, 1.0, F]).real)
        assert np.max(np.abs(a - b)) < 1e-10, f"корни при F={F}"
    # за порогом бистабильности решения нет
    assert not np.isfinite(extrema(A_CRIT + 1e-6)[0]), "выше A_c ям быть не может"
    assert np.isfinite(extrema(A_CRIT - 1e-6)[0]), "ниже A_c две ямы есть"
    # первый порядок: ΔU ≈ 1/4 − F, ошибка O(F²)
    for F, tol in ((0.01, 2e-4), (0.001, 2e-6)):
        d = barrier_heights(F)[0]
        assert abs(d - (BARRIER - F)) < tol, f"ΔU(F={F}) = {d}"
    e1 = abs(barrier_heights(0.01)[0] - (BARRIER - 0.01))
    e2 = abs(barrier_heights(0.001)[0] - (BARRIER - 0.001))
    assert 60 < e1 / e2 < 140, f"ошибка должна падать как F²: {e1/e2:.1f}"
    # Крамерс и обращение условия резонанса
    assert abs(kramers_rate(0.25) - KRAMERS_PREF * math.exp(-1.0)) < 1e-15
    for Om in (0.05, 0.19635, 0.5):
        D = d_opt_kramers(Om)
        assert abs(2.0 * kramers_rate(D) - Om / (2.0 * np.pi)) < 1e-14, \
            "обращение условия 2·r_K = Ω/(2π)"
    print(f"  ΔU={BARRIER}  A_c={A_CRIT:.9f}  r_K(D=1/4)={kramers_rate(0.25):.6f}")
    print(f"  ΔU(F=0.01)={barrier_heights(0.01)[0]:.8f} против линейного "
          f"{BARRIER-0.01:.8f}, ошибка {e1:.2e} (падает как F²: ×{e1/e2:.0f})")


def _test_signal_tools():
    print("─ ИНСТРУМЕНТЫ (ответы известны аналитически) ───────────────────")
    # τ_c для AR(1): dt·(½ + φ/(1−φ)) — считаем по точной автоковариации
    phi, n = 0.8, 60000
    g = np.random.default_rng(7).standard_normal(n)
    x = np.empty(n); x[0] = g[0]
    for i in range(1, n):
        x[i] = phi * x[i - 1] + g[i]
    tau = corr_time(x, 1.0)
    tau_th = 0.5 + phi / (1 - phi)
    assert abs(tau / tau_th - 1) < 0.15, f"τ_c={tau:.3f} против {tau_th:.3f}"
    # полосовая реконструкция Морле: два тона с известными амплитудами
    dt = 1.0
    t = np.arange(0, 2400, dt)
    y = 1.0 * np.cos(2 * np.pi * t / 10.0) + 2.0 * np.cos(2 * np.pi * t / 200.0)
    c = slice(600, 1800)                       # окно вне конуса влияния
    lo, _ = band_part(y, dt, FAST_LOW_D, FAST_CUT_D)
    hi, _ = band_part(y, dt, 100.0, 400.0)
    full, _ = band_part(y, dt, 0.0, 1e9)
    a_lo = float(np.sqrt(2) * lo[c].std())
    a_hi = float(np.sqrt(2) * hi[c].std())
    err = float(np.max(np.abs(full[c] + y.mean() - y[c])))
    assert abs(a_lo - 1.0) < 0.05, f"амплитуда короткого тона {a_lo:.3f} ≠ 1"
    assert abs(a_hi - 2.0) < 0.05, f"амплитуда длинного тона {a_hi:.3f} ≠ 2"
    assert np.corrcoef(lo[c], np.cos(2 * np.pi * t[c] / 10.0))[0, 1] > 0.999
    assert err < 0.02 * y[c].std(), f"полная реконструкция T&C: макс|Δ|={err:.4f}"
    # разделение полос честное: короткая полоса не тащит длинный тон
    assert abs(np.corrcoef(lo[c], np.cos(2 * np.pi * t[c] / 200.0))[0, 1]) < 0.02
    print(f"  τ_c(AR1 φ=0.8) = {tau:.3f} при теории {tau_th:.3f}")
    print(f"  полосы Морле: короткий тон {a_lo:.4f} (ждём 1.0), "
          f"длинный {a_hi:.4f} (ждём 2.0)")
    print(f"  полная реконструкция T&C: макс|Δ| = {err:.5f} "
          f"при ст.откл ряда {y[c].std():.4f} ({100*err/y[c].std():.2f}%)")


def _test_sr():
    Omega = 2.0 * np.pi / 32.0          # период 32 ед., ровно 160 отсчётов вывода
    n_per = 400          # 98 сегментов по 4 периода — меньше оценка γ² шумит
    A_sub, A_sup = 0.20, 0.60

    print("─ ТЕСТ 1. БЕЗ ШУМА И ПОДПОРОГОВАЯ НАКАЧКА → ПЕРЕХОДОВ НЕТ ──────")
    s1 = simulate(0.0, A_sub, Omega, 32.0 * 40, dt=0.01, x0=-1.0, seed=1)
    x1 = s1["x"][0, 0]
    n1 = count_transitions(x1)
    assert A_sub < A_CRIT, "накачка должна быть подпороговой"
    assert n1 == 0, f"переходов быть не может, а их {n1}"
    assert x1.max() < -0.5, f"траектория обязана остаться в левой яме: max={x1.max():.4f}"
    xl_min, _, _ = extrema(A_sub)
    assert abs(x1.max() - xl_min) < 0.05, \
        f"вершина колебаний {x1.max():.4f} должна лечь на смещённый минимум {xl_min:.4f}"
    print(f"  A={A_sub} < A_c={A_CRIT:.4f}, D=0: переходов {n1}, "
          f"x ∈ [{x1.min():.4f}, {x1.max():.4f}], смещённый минимум {xl_min:.4f}")

    print("─ ТЕСТ 1б. МЁРТВЫЙ ОТКЛИК → NaN, А НЕ ФАЛЬШИВАЯ ЕДИНИЦА ────────")
    per_s = int(round(2.0 * np.pi / Omega / 0.2))
    n_dead = per_s * 4 * 30
    co_dead = coherence_at(None, -np.ones((1, 1, n_dead)), 0.2, Omega)
    assert np.all(np.isnan(co_dead["gamma2"])), \
        f"постоянный отклик обязан дать NaN, а дал {co_dead['gamma2']}"
    assert np.all(np.isnan(co_dead["snr"]))
    assert co_dead["n_dead"] == 1
    # живой отклик той же амплитуды — идеальный меандр на частоте накачки
    tt = np.arange(n_dead) * 0.2
    co_live = coherence_at(None, np.sign(np.cos(Omega * tt))[None, None, :],
                           0.2, Omega)
    assert co_live["n_dead"] == 0 and co_live["gamma2"].ravel()[0] > 0.99, \
        f"идеальный меандр обязан дать γ²≈1: {co_live['gamma2']}"
    # и сквозь стенд: шум так мал, что ям не покинуть ни разу
    rc0 = response_curve(np.array([0.004, 0.008]), 0.0, Omega, n_periods=200,
                         n_paths=4, seed=5)
    assert np.all(rc0["trans"] == 0) and np.all(np.isnan(rc0["gamma2"])), \
        f"нет ни одного переброса → γ² обязан быть NaN: {rc0['gamma2']}"
    print(f"  постоянный отклик: γ²={co_dead['gamma2'].ravel()[0]} (NaN — верно), "
          f"мёртвых путей {co_dead['n_dead']}")
    print(f"  идеальный меандр:  γ²={co_live['gamma2'].ravel()[0]:.6f}, "
          f"мёртвых путей {co_live['n_dead']}")
    print(f"  стенд D={rc0['D'][0]:.3f}/{rc0['D'][1]:.3f}, A=0: переходов "
          f"{rc0['trans'][0]:.0f}/{rc0['trans'][1]:.0f}, γ²={rc0['gamma2']}, "
          f"мёртвых путей {rc0['dead_paths']}")

    print("─ ТЕСТ 2+3. КРИВАЯ ОТКЛИКА ПО D: НЕМОНОТОННОСТЬ И ПОЛОЖЕНИЕ ПИКА")
    D_grid = np.geomspace(0.02, 1.2, 15)
    rc = response_curve(D_grid, A_sub, Omega, n_periods=n_per, n_paths=6,
                        seed=20260801)
    g = rc["gamma2"]
    print("      D      γ²(переходы)   SNR      переходов   γ²(сырой x)")
    for D, gg, ss, tt, gx in zip(rc["D"], g, rc["snr"], rc["trans"], rc["gamma2_x"]):
        print(f"    {D:7.4f}    {gg:.4f}    {ss:8.3f}   {tt:8.1f}      {gx:.4f}")
    k = int(np.argmax(g))
    assert 0 < k < g.size - 1, f"максимум на краю сетки (индекс {k}) — это не резонанс"
    assert g[k] > g[0] * 1.3 and g[k] > g[-1] * 1.3, \
        "пик обязан быть выше обоих краёв хотя бы в 1.3 раза"
    D_max = float(rc["D"][k])
    D_th = d_opt_kramers(Omega)
    fac = max(D_max / D_th, D_th / D_max)
    assert fac < 2.0, f"пик D={D_max:.4f} против Крамерса {D_th:.4f}: фактор {fac:.2f}"
    print(f"  максимум γ²={g[k]:.4f} при D={D_max:.4f}; "
          f"Крамерс 2r_K=Ω/2π даёт {D_th:.4f} → фактор {fac:.2f} (< 2)")
    print(f"  края: γ²(D={rc['D'][0]:.3f})={g[0]:.4f}, "
          f"γ²(D={rc['D'][-1]:.3f})={g[-1]:.4f}; "
          f"альтернативная конвенция даёт D={d_opt_timescale(Omega):.4f}")

    # сама формула Крамерса — против измеренной частоты переходов БЕЗ накачки
    Dk = np.array([0.06, 0.09, 0.14])
    sk = simulate(Dk, 0.0, Omega, 32.0 * (n_per // 2), dt=0.01, seed=42, n_paths=8)
    meas = count_transitions(sk["x"]).mean(axis=1) / sk["t"][-1]
    print("  формула Крамерса против измерения (накачки нет, A=0):")
    for D, m in zip(Dk, meas):
        th = kramers_rate(D)
        f = max(m / th, th / m)
        assert f < 2.0, f"D={D}: измерено {m:.5f}, теория {th:.5f}, фактор {f:.2f}"
        print(f"    D={D:.3f}: измерено r={m:.5f}, теория r_K={th:.5f}, "
              f"фактор {f:.2f}")

    print("─ ТЕСТ 3б. НУЛЬ: ФАЗЫ НАКАЧКИ ПЕРЕМЕШАНЫ → ПИК ОБЯЗАН ПРОПАСТЬ ─")
    drv_s = shuffled_drive(A_sub, Omega, 32.0 * n_per, dt=0.01, seed=777)
    drv_c = A_sub * np.cos(Omega * np.arange(drv_s.size) * 0.01)
    # мощность и амплитуда те же — меняется только фаза
    assert abs(drv_s.std() / drv_c.std() - 1.0) < 0.02, "мощность накачки съехала"
    assert abs(np.abs(drv_s).max() - A_sub) < 1e-9, "амплитуда накачки съехала"
    rc_s = response_curve(D_grid, A_sub, Omega, n_periods=n_per, n_paths=6,
                          seed=20260801, shuffle_phase=True, phase_seed=777)
    gs = rc_s["gamma2"]
    gain_c = float(np.nanmax(g) - g[0])
    gain_s = float(np.nanmax(gs) - gs[0])
    print(f"  мощность накачки: когерентная σ={drv_c.std():.5f}, "
          f"перемешанная σ={drv_s.std():.5f} (отношение {drv_s.std()/drv_c.std():.4f})")
    print("      D      γ²(когерентная)   γ²(фазы перемешаны)")
    for D, gg, ss in zip(rc_s["D"], g, gs):
        print(f"    {D:7.4f}       {gg:.4f}              {ss:.4f}")
    print(f"  пик: когерентная {np.nanmax(g):.4f} против перемешанной "
          f"{np.nanmax(gs):.4f} → падение в {np.nanmax(g)/max(np.nanmax(gs),1e-9):.1f} раз")
    print(f"  прибавка от шума: когерентная {gain_c:+.4f}, "
          f"перемешанная {gain_s:+.4f}; размах кривой "
          f"{np.nanmax(g)-np.nanmin(g):.4f} против {np.nanmax(gs)-np.nanmin(gs):.4f}")
    assert np.nanmax(gs) < 0.15, \
        f"на перемешанных фазах γ² обязан лечь: max={np.nanmax(gs):.4f}"
    assert np.nanmax(g) > 4.0 * np.nanmax(gs), \
        "пик пережил перемешивание фаз — это не резонанс"
    assert gain_s < 0.10 < gain_c, \
        f"прибавка от шума: когерентная {gain_c:.3f}, перемешанная {gain_s:.3f}"

    print("─ ТЕСТ 4. НАДПОРОГОВАЯ НАКАЧКА: ПЕРЕХОДЫ БЕЗ ШУМА, ПИК РАЗМЫТ ──")
    npd = 40
    s4 = simulate(0.0, A_sup, Omega, 32.0 * npd, dt=0.01, x0=-1.0, seed=4)
    n4 = count_transitions(s4["x"][0, 0])
    assert A_sup > A_CRIT
    assert n4 >= 2 * (npd - 2), f"ждём ~2 перехода за период, всего {n4} за {npd}"
    rc2 = response_curve(D_grid, A_sup, Omega, n_periods=n_per, n_paths=6,
                         seed=20260801)
    g2 = rc2["gamma2"]
    k2 = int(np.argmax(g2))
    gain1 = float(g.max() - g[0])       # сколько отклика СОЗДАЛ шум
    gain2 = float(g2.max() - g2[0])
    assert g[0] < 0.10, f"подпорогово на тихом краю переключений быть не может: {g[0]:.4f}"
    assert g2[0] > 0.80, f"надпорогово без шума ямы обязаны щёлкать в такт: {g2[0]:.4f}"
    assert k2 <= 1, f"надпорогово максимум обязан уехать на тихий край, а он на {k2}"
    assert gain2 < 0.05 and gain1 > 0.35, \
        f"прибавка от шума: подпорог {gain1:.3f}, надпорог {gain2:.3f}"
    print(f"  D=0, A={A_sup}: переходов {n4} за {npd} периодов "
          f"({n4/npd:.2f} на период, теория 2.00)")
    print(f"  γ² на тихом краю: подпорог {g[0]:.4f} → надпорог {g2[0]:.4f}; "
          f"максимум надпороговой кривой на индексе {k2} (край)")
    print(f"  прибавка, созданная шумом (max − тихий край): подпорог "
          f"{gain1:+.4f}, надпорог {gain2:+.4f} — пик размылся")
    for D, gg in zip(rc2["D"], g2):
        print(f"    D={D:7.4f}   γ²={gg:.4f}")

    print("─ ТЕСТ 5. ДЕТЕРМИНИЗМ ПРИ ФИКСИРОВАННОМ ЗЕРНЕ ──────────────────")
    a = simulate([0.05, 0.2], A_sub, Omega, 32.0 * 8, seed=777, n_paths=3)["x"]
    b = simulate([0.05, 0.2], A_sub, Omega, 32.0 * 8, seed=777, n_paths=3)["x"]
    c = simulate([0.05, 0.2], A_sub, Omega, 32.0 * 8, seed=778, n_paths=3)["x"]
    assert np.array_equal(a, b), "одно зерно — побитово один ряд"
    assert not np.array_equal(a, c), "разные зёрна обязаны разойтись"
    try:
        simulate(0.1, A_sub, Omega, 32.0, seed=None)
        raise AssertionError("простыня без зерна должна быть запрещена")
    except ValueError:
        pass
    print(f"  seed=777 дважды: max|Δ| = {np.max(np.abs(a - b)):.1e}; "
          f"seed=778: max|Δ| = {np.max(np.abs(a - c)):.4f}; без зерна — ValueError")
    return rc, rc2


def _test_applied():
    """Прикладной путь: детерминизм без генератора и границы величин."""
    print("─ ПРИКЛАДНОЙ ПУТЬ (эфемериды, генератора нет) ──────────────────")
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    a = hourly_transition(t0, 200, step_h=3.0)
    b = hourly_transition(t0, 200, step_h=3.0)
    assert a is not None, "эфемериды не загрузились"
    for k in ("xi", "D_eff", "p_hour", "dU", "drive"):
        assert np.array_equal(a[k], b[k]), f"{k}: прикладной путь обязан быть " \
                                           "побитово детерминирован"
    assert np.isfinite(a["xi"]).all() and np.isfinite(a["p_hour"]).all()
    assert (a["p_hour"] > 0).all() and (a["p_hour"] < 1).all()
    assert (a["dU"] > 0).all(), "барьер обязан оставаться положительным"
    assert a["dU"].max() <= BARRIER + a["A"] + 1e-9
    assert abs(float(np.median(a["ratio"])) - 1.0) < 1e-9, "калибровка по медиане"
    assert np.max(np.abs(a["F"])) <= a["A"] + 1e-12, "|F| не может превысить A"
    c = float(np.corrcoef(a["xi_raw"], a["xi_hp"])[0, 1])
    assert c > 0.85, f"два способа выделить ξ обязаны совпасть, а corr={c:.3f}"
    sub, _ = band_part(a["xi_raw"], 3.0 / 24.0, 0.0, 2.0)
    frac = float(np.var(sub) / np.var(a["xi_raw"]))
    assert frac < 0.10, f"внутрисуточной доли у геоцентрического ξ быть не должно: {frac:.3f}"
    try:
        hourly_transition(t0, 60, A=0.5)
        raise AssertionError("надпороговая накачка должна быть запрещена")
    except ValueError:
        pass
    print(f"  два независимых вызова совпали побитово; corr(Морле, ВЧ-фильтр) = {c:.4f}")
    print(f"  доля дисперсии ξ короче 2 суток = {frac*100:.2f}%; "
          f"ΔU ∈ [{a['dU'].min():.4f}, {a['dU'].max():.4f}]; "
          f"A ≥ A_c отвергается")
    print(f"  T_накачки = {a['T_drive_days']:.1f} сут, D_opt = {a['D_opt']:.5f}, "
          f"τ_c(ξ) = {a['tau_c_days']:.3f} сут")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    mode = args[0] if args else ""
    if mode in ("", "test", "all"):
        print("=" * 92)
        print("СТОХАСТИЧЕСКИЙ РЕЗОНАНС · SELF-ТЕСТЫ (ответы известны из литературы)")
        print("=" * 92)
        _test_analytic()
        _test_signal_tools()
        _test_sr()
        _test_applied()
        print("\nвсе семь тестов пройдены (добавлены 1б — мёртвый отклик → NaN,\n      и 3б — нуль по перемешанным фазам накачки)")
    if mode in ("sky", "all"):
        print()
        run_sky()
    if mode in ("resto", "all"):
        print()
        run_resto(nsur=200)
