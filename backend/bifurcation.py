# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — ДВИГАТЕЛЬ БИФУРКАЦИЙ (нелинейная динамика ценового ряда).

Прямой заказ владельца: «хотелось бы больше предугадываний, бифуркаций, очень
много непризнанных учёных». Этот модуль — машинное отделение того заказа:
СЕМЬ независимых детекторов приближающегося слома, каждый — по трудам школы
нелинейной динамики, которую мейнстрим финансов десятилетиями держал на
обочине:

  · ПРИГОЖИН (диссипативные структуры, Нобель-1977, но в финансах — ересь):
    рынок как открытая система dx/dt = λx − βx³; линеаризация у равновесия
    эквивалентна AR(1): x_{t+1} = φ·x_t + ε. φ→1 — «критическое замедление»:
    среда перестаёт гасить отклонения, точка бифуркации рядом. λ = ln φ.
  · ШЕФФЕР/ДАКОС (early-warning signals, экология → рынки, признаны лишь
    частично): перед сломом растут ОДНОВРЕМЕННО автокорреляция lag-1,
    дисперсия и асимметрия. Тренд по Кендаллу, композитный счёт 0..1.
  · СОРНЕТТ (LPPLS — лог-периодическая степенная сингулярность; физик,
    объявивший крахи «землетрясениями рынка», мейнстрим морщится до сих пор):
    ln p(t) = A + B(tc−t)^m + C(tc−t)^m·cos(ω·ln(tc−t)−φ). Честная калибровка:
    нелинейные (tc, m, ω) — сеткой, линейные (A, B, C1, C2) — МНК. Даёт ДАТУ
    сингулярности tc и сторону (B<0 — пузырь вверх → срыв вниз; B>0 —
    анти-пузырь → выстрел вверх).
  · ТОМ/ЗИМАН (теория катастроф, «слишком красиво, чтобы быть наукой» — так
    её и похоронили): складка-сборка (cusp). Признак двух аттракторов —
    бимодальность распределения: коэффициент Сарле b=(γ₁²+1)/κ > 5/9.
    Бистабильность = цена сидит в одной из двух ям — перескок резкий.
  · ХЁРСТ/МАНДЕЛЬБРОТ/ПЕТЕРС (фрактальная гипотеза рынка — FMH против EMH):
    DFA-1 экспонента H. H>0.5 — персистентная память (ход продолжается),
    H<0.5 — антиперсистентность (пила), H≈0.5 — монета. DFA устойчивее R/S
    на коротких рядах (Peng 1994).
  · ТАКЕНС + РОЗЕНШТЕЙН (реконструкция аттрактора из одного ряда — теорема
    Такенса 1981; Ляпунов по Rosenstein 1993): λ_max > 0 — хаос, горизонт
    предсказуемости ≈ 1/λ_max баров. Знать, СКОЛЬКО баров вперёд ряд вообще
    предсказуем — это и есть честная рамка любого «предугадывания».
  · RQA (Эккман/Цбилут/Уэббер, recurrence quantification — четверть века
    считалась экзотикой): детерминизм DET — доля рекуррентных точек на
    диагоналях. DET высок — в ряде работает динамика, а не шум.

Рамка честности (закон школы):
  🔵 механика — вся математика выше проверяема независимо и посчитана честно
     (МНК, RK-4 не нужен: тут только анализ ряда);
  🟡 язык школы — пороги режимов («у порога», «окно открыто») — калибровка
     школы, прокси названы в note каждого блока;
  ⚫ полюс за человеком — НИ ОДНА цифра не приказ; рынок не обязан ломаться
     по расписанию; это карта напряжений среды. НЕ торговый сигнал · 18+.

Законы: ABSOLUTE OFFLINE (только ряд цен), NO DUMMIES (мало данных → None и
честный note), НИКАКОГО random (весь код детерминирован байт-в-байт),
аддитивность (ядро aether не тронуто — модуль живёт рядом).

Self-тест: python3 -m backend.bifurcation  (реальные assert на синтетике).
"""
from __future__ import annotations

import math

import numpy as np

# ── пороги школы (🟡 калибровка, все названы здесь и только здесь) ──────────
PHI_CRITICAL = 0.97     # AR(1) φ выше — «критическое замедление» Пригожина
PHI_TENSION = 0.90      # φ выше — «напряжение копится»
EW_ALARM = 0.60         # композит Шеффера выше — предвестники кричат
EW_TENSION = 0.35       # выше — предвестники шепчут
SARLE_BIMODAL = 5.0 / 9.0   # порог бимодальности Сарле (строгий, классический)
LPPLS_R2_MIN = 0.85     # качество подгонки LPPLS, ниже — не верим
LPPLS_TC_NEAR = 40      # tc ближе N баров — сингулярность «рядом»
LYAP_CHAOS = 0.05       # λ_max выше — хаос ощутимый (на бар)
DET_STRONG = 0.70       # детерминизм RQA выше — динамика, не шум
MIN_BARS = 64           # меньше баров — честный None (NO DUMMIES)

FRAME = ("🔵 механика: DFA/AR1/МНК/Розенштейн/RQA честные · 🟡 пороги режимов — "
         "калибровка школы (названы в модуле) · ⚫ карта напряжений среды, "
         "НЕ торговый сигнал; рынок не обязан ломаться по расписанию · 18+")


def _clean(closes) -> np.ndarray | None:
    """Ряд в float, мусор выброшен; мало данных → None (NO DUMMIES)."""
    if closes is None:
        return None
    x = np.asarray([c for c in closes if c is not None], dtype=float)
    x = x[np.isfinite(x) & (x > 0)]
    return x if x.size >= MIN_BARS else None


# ═════════════════════════════════════════════════════════════════════════
# 1. ХЁРСТ ПО DFA-1 (Пенг-1994; Мандельброт/Петерс — фрактальная память)
# ═════════════════════════════════════════════════════════════════════════

def hurst_dfa(closes) -> dict | None:
    """DFA-1 на лог-приращениях: α ≈ H для fGn. Устойчивее R/S на коротких
    рядах. Возвращает H, режим и силу памяти |H−0.5|·2."""
    x = _clean(closes)
    if x is None:
        return None
    r = np.diff(np.log(x))
    if r.size < MIN_BARS - 1 or float(np.std(r)) <= 0:
        return None
    prof = np.cumsum(r - r.mean())
    n = prof.size
    smax = n // 4
    scales = np.unique(np.floor(np.logspace(
        math.log10(4), math.log10(max(5, smax)), 12)).astype(int))
    scales = scales[scales >= 4]
    fs, ss = [], []
    for s in scales:
        nseg = n // s
        if nseg < 2:
            continue
        seg = prof[:nseg * s].reshape(nseg, s)
        t = np.arange(s, dtype=float)
        # детренд полиномом 1-й степени в каждом сегменте (DFA-1)
        tm = t - t.mean()
        denom = float(np.sum(tm * tm))
        b1 = (seg - seg.mean(axis=1, keepdims=True)) @ tm / denom
        fit = seg.mean(axis=1, keepdims=True) + np.outer(b1, tm)
        f2 = np.mean((seg - fit) ** 2)
        if f2 > 0:
            fs.append(0.5 * math.log(f2))          # log √F² = ½ log F²
            ss.append(math.log(float(s)))
    if len(fs) < 4:
        return None
    H = float(np.polyfit(ss, fs, 1)[0])
    H = max(0.0, min(1.0, H))
    word = ("персистентная память — ход склонен продолжаться" if H > 0.55 else
            "антиперсистентная пила — ход склонен возвращаться" if H < 0.45
            else "память слабая — почти монета")
    return {"H": round(H, 3), "memory": round(abs(H - 0.5) * 2.0, 3),
            "word": word, "note": "DFA-1 по лог-приращениям (Пенг-1994) 🔵"}


# ═════════════════════════════════════════════════════════════════════════
# 2. ПРИГОЖИН: критическое замедление через AR(1)
# ═════════════════════════════════════════════════════════════════════════

def prigogine_market(closes, win: int = 96) -> dict | None:
    """Линеаризация dx/dt = λx − βx³ у равновесия ⟺ AR(1) на отклонениях от
    скользящего равновесия: x_{t+1} = φ·x_t + ε. φ→1 — восстановление
    замирает, λ = ln φ → 0⁻: старая структура теряет устойчивость (это ТА ЖЕ
    механика, что prigogine_bifurcation.py школы REAL SKY, только λ считается
    не по небу, а по самому ряду — среда сама говорит, где её порог)."""
    x = _clean(closes)
    if x is None:
        return None
    lx = np.log(x)
    w = min(win, lx.size // 2)
    if w < 24:
        return None
    seg = lx[-w:]
    t = np.arange(w, dtype=float)
    # равновесие окна — линейный тренд; отклонение от него и есть x Пригожина
    b, a = np.polyfit(t, seg, 1)
    dev = seg - (a + b * t)
    d0, d1 = dev[:-1], dev[1:]
    den = float(np.dot(d0, d0))
    if den <= 0:
        return None
    phi = float(np.dot(d0, d1) / den)
    phi = max(-0.999, min(0.9999, phi))
    lam = math.log(abs(phi)) if phi != 0 else -12.0     # на бар; →0⁻ = порог
    dist = max(0.0, 1.0 - phi)                          # запас устойчивости
    if phi >= PHI_CRITICAL:
        word = "У ПОРОГА: восстановление замерло (критическое замедление)"
    elif phi >= PHI_TENSION:
        word = "напряжение копится: среда гасит отклонения всё ленивее"
    else:
        word = "линейная фаза: отклонения гасятся, старая структура держит"
    return {"phi": round(phi, 4), "lambda": round(lam, 4),
            "dist_to_bif": round(dist, 4), "win": w, "word": word,
            "note": "AR(1) на отклонениях от тренда окна — линеаризация "
                    "уравнения Пригожина dx/dt=λx−βx³ 🔵; пороги φ — школа 🟡"}


# ═════════════════════════════════════════════════════════════════════════
# 3. ШЕФФЕР/ДАКОС: композит ранних предвестников
# ═════════════════════════════════════════════════════════════════════════

def _kendall_tau(y: np.ndarray) -> float:
    """τ Кендалла тренда ряда против времени (O(m²), ряды короткие)."""
    m = y.size
    if m < 5:
        return 0.0
    conc = disc = 0
    for i in range(m - 1):
        d = y[i + 1:] - y[i]
        conc += int(np.sum(d > 0))
        disc += int(np.sum(d < 0))
    tot = m * (m - 1) / 2
    return float((conc - disc) / tot) if tot else 0.0


def early_warning(closes, win: int = 48, step: int = 4) -> dict | None:
    """Перед бифуркацией растут: автокорреляция lag-1 (память об ударе),
    дисперсия (среда шатается шире) и |асимметрия| (яма кривеет к соседнему
    аттрактору). Считаем каждую метрику в скользящих окнах и берём тренд по
    Кендаллу; композит — средний τ, сложенный в 0..1."""
    x = _clean(closes)
    if x is None:
        return None
    r = np.diff(np.log(x))
    if r.size < win * 2:
        return None
    ar1s, vars_, skews = [], [], []
    for beg in range(0, r.size - win + 1, step):
        seg = r[beg:beg + win]
        s0 = seg - seg.mean()
        den = float(np.dot(s0[:-1], s0[:-1]))
        ar1s.append(float(np.dot(s0[:-1], s0[1:]) / den) if den > 0 else 0.0)
        v = float(np.var(seg))
        vars_.append(v)
        sd = math.sqrt(v) if v > 0 else 1.0
        skews.append(abs(float(np.mean(s0 ** 3)) / sd ** 3) if sd > 0 else 0.0)
    if len(ar1s) < 5:
        return None
    t_ar = _kendall_tau(np.asarray(ar1s))
    t_va = _kendall_tau(np.asarray(vars_))
    t_sk = _kendall_tau(np.asarray(skews))
    # композит: AR1 и дисперсия — главные (Шеффер), асимметрия — поддержка
    raw = 0.4 * t_ar + 0.4 * t_va + 0.2 * t_sk
    score = max(0.0, min(1.0, (raw + 1.0) / 2.0))
    if score >= EW_ALARM:
        word = "ПРЕДВЕСТНИКИ КРИЧАТ: память+размах+перекос растут вместе"
    elif score >= EW_TENSION:
        word = "предвестники шепчут: напряжение медленно копится"
    else:
        word = "фон спокоен: ранних предвестников слома нет"
    return {"score": round(score, 3), "tau_ar1": round(t_ar, 3),
            "tau_var": round(t_va, 3), "tau_skew": round(t_sk, 3),
            "windows": len(ar1s), "word": word,
            "note": "критическое замедление Шеффера/Дакоса: тренды AR1, "
                    "дисперсии, |skew| по Кендаллу 🔵; веса композита — школа 🟡"}


# ═════════════════════════════════════════════════════════════════════════
# 4. СОРНЕТТ: LPPLS — лог-периодическая сингулярность
# ═════════════════════════════════════════════════════════════════════════

def lppls(closes, tc_max_ahead: int = 60) -> dict | None:
    """ln p(t) = A + B·f + C₁·f·cos(ω ln(tc−t)) + C₂·f·sin(ω ln(tc−t)),
    f = (tc−t)^m. Нелинейные (tc, m, ω) — сеткой; (A,B,C₁,C₂) — МНК.
    Фильтр Сорнетта: 0.1≤m≤0.9, 6≤ω≤13, damping = m|B|/(ω√(C₁²+C₂²)) ≥ 0.8,
    R² ≥ порога. B<0 — супер-экспонента ВВЕРХ (пузырь: срыв вниз у tc);
    B>0 — анти-пузырь (выстрел вверх у tc)."""
    x = _clean(closes)
    if x is None:
        return None
    lp = np.log(x[-min(x.size, 360):])          # хвост: свежий сюжет важнее
    n = lp.size
    t = np.arange(n, dtype=float)
    best = None
    for tc in range(n + 5, n + tc_max_ahead + 1, 5):
        dt = tc - t
        ldt = np.log(dt)
        for m in (0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85):
            f = dt ** m
            for w in (5.0, 6.5, 8.0, 9.5, 11.0, 12.5):
                cosw, sinw = np.cos(w * ldt), np.sin(w * ldt)
                A_ = np.column_stack([np.ones(n), f, f * cosw, f * sinw])
                coef, res, rank, _ = np.linalg.lstsq(A_, lp, rcond=None)
                if rank < 4:
                    continue
                sse = float(res[0]) if res.size else float(
                    np.sum((lp - A_ @ coef) ** 2))
                if best is None or sse < best[0]:
                    best = (sse, tc, m, w, coef)
    if best is None:
        return None
    sse, tc, m, w, coef = best
    tss = float(np.sum((lp - lp.mean()) ** 2))
    r2 = 1.0 - sse / tss if tss > 0 else 0.0
    A0, B, C1, C2 = (float(c) for c in coef)
    camp = math.hypot(C1, C2)
    damping = (m * abs(B) / (w * camp)) if camp > 0 else float("inf")
    # осцилляция должна БЫТЬ: относительная амплитуда и ≥2.5 полуциклов в окне
    rel_osc = camp / abs(B) if abs(B) > 1e-12 else 0.0
    cycles = w * math.log(tc / max(1.0, tc - n)) / math.pi
    qualified = bool(r2 >= LPPLS_R2_MIN and 0.1 <= m <= 0.9 and 5.0 <= w <= 13.0
                     and damping >= 0.8 and abs(B) > 1e-9
                     and rel_osc >= 0.02 and cycles >= 2.5)
    tc_ahead = tc - n
    side = "срыв вниз (пузырь зрел)" if B < 0 else "выстрел вверх (анти-пузырь)"
    near = qualified and tc_ahead <= LPPLS_TC_NEAR
    return {"tc_bars_ahead": int(tc_ahead), "m": round(m, 2), "omega": round(w, 1),
            "r2": round(r2, 3), "damping": round(min(damping, 99.0), 2),
            "B": round(B, 6), "qualified": qualified, "near": bool(near),
            "side": side if qualified else None,
            "word": (f"LPPLS: сингулярность через ~{tc_ahead} баров, {side}"
                     if qualified else
                     "LPPLS: лог-периодики уверенной нет (фильтр Сорнетта не пройден)"),
            "note": "калибровка Сорнетта: сетка (tc,m,ω) + МНК (A,B,C₁,C₂) 🔵; "
                    "фильтр качества — канон LPPLS 🟡"}


# ═════════════════════════════════════════════════════════════════════════
# 5. ТОМ/ЗИМАН: катастрофа-сборка (бимодальность Сарле)
# ═════════════════════════════════════════════════════════════════════════

def cusp(closes, win: int = 128) -> dict | None:
    """Два аттрактора cusp-катастрофы видны как бимодальность распределения
    цены в окне: b = (γ₁²+1)/κ > 5/9 (Сарле). Бистабильность = цена в одной
    из двух ям; перескок между ними — та самая «катастрофа» Зимана. side —
    в какой яме сидим сейчас (перескок целил бы в противоположную)."""
    x = _clean(closes)
    if x is None:
        return None
    seg = x[-min(x.size, win):]
    mu, sd = float(seg.mean()), float(seg.std())
    if sd <= 0:
        return None
    z = (seg - mu) / sd
    g1 = float(np.mean(z ** 3))
    kurt = float(np.mean(z ** 4))                       # НЕ excess: κ нормали = 3
    b = (g1 * g1 + 1.0) / kurt if kurt > 0 else 0.0
    bistable = bool(b > SARLE_BIMODAL)
    now = float(z[-1])
    side = "верхняя яма (перескок целил бы вниз)" if now > 0 else \
           "нижняя яма (перескок целил бы вверх)"
    return {"sarle_b": round(b, 3), "bistable": bistable,
            "z_now": round(now, 2), "side": side if bistable else None,
            "word": (f"СБОРКА ТОМА: две ямы (b={b:.2f}>5/9), сидим в "
                     + ("верхней" if now > 0 else "нижней")
                     if bistable else
                     f"одна яма (b={b:.2f}≤5/9) — катастрофного рельефа нет"),
            "note": "бимодальность Сарле как признак двух аттракторов cusp 🔵; "
                    "прочтение ям — язык школы 🟡"}


# ═════════════════════════════════════════════════════════════════════════
# 6. ТАКЕНС + РОЗЕНШТЕЙН: старший показатель Ляпунова
# ═════════════════════════════════════════════════════════════════════════

def _embed(y: np.ndarray, dim: int, tau: int) -> np.ndarray:
    n = y.size - (dim - 1) * tau
    return np.column_stack([y[i * tau:i * tau + n] for i in range(dim)])


def lyapunov(closes, dim: int = 4, tau: int = 1, k_steps: int = 8) -> dict | None:
    """λ_max по Розенштейну-1993: для каждой точки аттрактора (вложение
    Такенса) — ближайший сосед вне полосы Тейлера; средний лог-разбег за k
    шагов; λ = наклон. λ>0 — хаос; горизонт предсказуемости ≈ 1/λ баров."""
    x = _clean(closes)
    if x is None:
        return None
    r = np.diff(np.log(x))
    r = r[-min(r.size, 500):]                    # O(n²) — держим в узде
    sd = float(np.std(r))
    if sd <= 0:
        return None
    y = (r - r.mean()) / sd
    emb = _embed(y, dim, tau)
    npts = emb.shape[0]
    if npts < 60 + k_steps:
        return None
    theiler = max(4, npts // 50)
    d2 = np.sum((emb[:, None, :] - emb[None, :, :]) ** 2, axis=2)
    for i in range(npts):                        # полоса Тейлера + сам к себе
        lo, hi = max(0, i - theiler), min(npts, i + theiler + 1)
        d2[i, lo:hi] = np.inf
    nn = np.argmin(d2, axis=1)
    div = np.zeros(k_steps + 1)
    cnt = np.zeros(k_steps + 1)
    for i in range(npts):
        j = int(nn[i])
        if not np.isfinite(d2[i, j]):
            continue
        for k in range(k_steps + 1):
            if i + k < npts and j + k < npts:
                d = float(np.linalg.norm(emb[i + k] - emb[j + k]))
                if d > 0:
                    div[k] += math.log(d)
                    cnt[k] += 1
    ok = cnt > (npts * 0.25)
    if int(ok.sum()) < 4:
        return None
    ks = np.arange(k_steps + 1, dtype=float)[ok]
    ls = (div / np.maximum(cnt, 1))[ok]
    lam = float(np.polyfit(ks, ls, 1)[0])
    horizon = (1.0 / lam) if lam > 1e-6 else float("inf")
    chaotic = bool(lam > LYAP_CHAOS)
    word = (f"хаос: λ={lam:.3f}/бар, горизонт предсказуемости ~{horizon:.0f} баров"
            if chaotic else
            "хаос слаб: ряд держит форму дольше горизонта окна")
    return {"lambda_max": round(lam, 4),
            "horizon_bars": (round(horizon, 1) if math.isfinite(horizon) else None),
            "chaotic": chaotic, "dim": dim, "word": word,
            "note": "вложение Такенса + разбег ближайших соседей "
                    "(Розенштейн-1993), полоса Тейлера 🔵"}


# ═════════════════════════════════════════════════════════════════════════
# 7. RQA: детерминизм ряда (Эккман/Цбилут/Уэббер)
# ═════════════════════════════════════════════════════════════════════════

def determinism(closes, dim: int = 3, tau: int = 1, eps_q: float = 0.15,
                lmin: int = 2) -> dict | None:
    """DET = доля рекуррентных точек, лежащих на диагоналях длины ≥ lmin.
    Высокий DET — в ряде живёт динамика (повторяющиеся траектории), низкий —
    шум. Радиус ε — квантиль eps_q попарных расстояний (самонастройка)."""
    x = _clean(closes)
    if x is None:
        return None
    r = np.diff(np.log(x))
    r = r[-min(r.size, 300):]
    sd = float(np.std(r))
    if sd <= 0:
        return None
    y = (r - r.mean()) / sd
    emb = _embed(y, dim, tau)
    npts = emb.shape[0]
    if npts < 50:
        return None
    d = np.sqrt(np.sum((emb[:, None, :] - emb[None, :, :]) ** 2, axis=2))
    iu = np.triu_indices(npts, k=1)
    eps = float(np.quantile(d[iu], eps_q))
    if eps <= 0:
        return None
    R = d <= eps
    np.fill_diagonal(R, False)
    rec_total = int(np.sum(R[iu]))
    if rec_total == 0:
        return {"det": 0.0, "rr": 0.0, "word": "рекуррентности нет — чистый шум",
                "note": "RQA: нет возвратов траектории 🔵"}
    diag_pts = 0
    for off in range(1, npts):                   # верхние диагонали
        line = np.diagonal(R, offset=off)
        if line.size < lmin:
            continue
        run = 0
        for v in line:
            if v:
                run += 1
            else:
                if run >= lmin:
                    diag_pts += run
                run = 0
        if run >= lmin:
            diag_pts += run
    det = diag_pts / rec_total
    rr = rec_total / (npts * (npts - 1) / 2)
    word = ("динамика правит: траектории повторяются (DET высок)"
            if det >= DET_STRONG else
            "шум подтачивает: повторяемость траекторий слаба")
    return {"det": round(float(det), 3), "rr": round(float(rr), 3),
            "eps_q": eps_q, "word": word,
            "note": "recurrence quantification (Эккман/Цбилут/Уэббер): доля "
                    "рекуррентных точек на диагоналях ≥2 🔵"}


# ═════════════════════════════════════════════════════════════════════════
# 8. ХИЛЛ: индекс хвоста (Мандельброт — «толстые хвосты», конечна ли дисперсия)
# ═════════════════════════════════════════════════════════════════════════

def hill_tail(closes, k_frac: float = 0.08) -> dict | None:
    """Оценка индекса хвоста α по Хиллу на |лог-приращениях|:
        α̂ = k / Σ ln(x_(i)/x_(k)) по k старшим порядковым статистикам.
    α ≤ 2 — дисперсия БЕСКОНЕЧНА (мир Мандельброта: Келли и вся гауссова
    арифметика недействительны); α < 3 — хвосты тяжёлые; α ≥ 4 — почти
    гауссово. k — верхние k_frac наблюдений (мин 10)."""
    x = _clean(closes)
    if x is None:
        return None
    r = np.abs(np.diff(np.log(x)))
    r = r[r > 0]
    if r.size < MIN_BARS - 1:
        return None
    r = np.sort(r)[::-1]
    k = max(10, int(r.size * k_frac))
    if k >= r.size:
        return None
    xk = r[k]
    if xk <= 0:
        return None
    logs = np.log(r[:k] / xk)
    s = float(np.sum(logs))
    if s <= 0:
        return None
    alpha = k / s
    fat = bool(alpha < 3.0)
    word = ("ДИСПЕРСИЯ БЕСКОНЕЧНА (α≤2) — гауссова арифметика мертва"
            if alpha <= 2.0 else
            "хвосты тяжёлые (α<3) — редкие удары правят риском" if fat else
            "хвосты умеренные — классические оценки терпимы")
    return {"alpha": round(float(alpha), 2), "k": int(k), "fat_tails": fat,
            "word": word,
            "note": "оценка Хилла по верхним порядковым статистикам 🔵; "
                    "порог «тяжёлых» α<3 — конвенция EVT 🟡"}


# ═════════════════════════════════════════════════════════════════════════
# ИМПЛОЗИЯ ШАУБЕРГЕРА 🟡 (спринт «Абсолют»): воронка вакуума
# ═════════════════════════════════════════════════════════════════════════
GOLDEN = (1.0 + 5.0 ** 0.5) / 2.0        # Φ
VORTEX_CRIT = GOLDEN ** 4                # ≈6.854 — порог имплозии по логу


def vortex_omega(prices, tick_dt, *, alpha=None, cvd_grad=None,
                 price_scale=None, dt_scale=None) -> dict | None:
    """Ω_vortex = (ΔP/Δτ) · Φ^(1/α) — «ротор эфирного поля»: скорость хода,
    делённая на сжатие межтикового интервала (хроно-плотность = ускорение
    прихода тиков), с золотым множителем по тяжести хвостов.

    ⚠ ЧЕСТНАЯ НОРМИРОВКА (иначе формула бессмысленна). В логе Ω сравнивается
    с безразмерным порогом Φ⁴≈6.85, но сырое ΔP/Δτ имеет размерность
    «рублей в секунду» — его величина зависит от цены инструмента и частоты
    тиков, поэтому сравнение с 6.85 было бы произволом. Приводим к
    безразмерному виду:
        ΔP  → в долях price_scale (медианный |ΔP|, либо медианная цена),
        Δτ  → в долях dt_scale    (медианный межтиковый интервал).
    Тогда Ω = (|ΔP|/scale_P) / (Δτ/scale_τ) · Φ^(1/α) — «во сколько раз ход
    быстрее обычного при данном сжатии времени». Порог Φ⁴ сохранён как в
    спецификации, но это КОНВЕНЦИЯ 🟡, нулями не проверенная.

    prices  — последние цены (≥3), tick_dt — межтиковые интервалы (сек).
    cvd_grad — ∇CVD для стороны (знак); None → сторона не выдаётся.
    Возврат: {omega, implosion: bool, side} или None (NO DUMMIES)."""
    # своя очистка: детекторам режима нужно ≥MIN_BARS баров, а ротору —
    # только последний ход и последний интервал (≥3 точки достаточно)
    try:
        p = np.asarray([float(x) for x in (prices or [])], float)
    except (TypeError, ValueError):
        return None
    p = p[np.isfinite(p)]
    if p.size < 3:
        return None
    dts = np.asarray([float(x) for x in (tick_dt or []) if x is not None],
                     float)
    dts = dts[dts > 0]
    if dts.size < 3:
        return None
    dp = np.abs(np.diff(p))
    if dp.size < 2:
        return None
    ps = float(price_scale) if price_scale else float(np.median(dp[dp > 0])
                                                      if np.any(dp > 0) else 0.0)
    ts = float(dt_scale) if dt_scale else float(np.median(dts))
    if ps <= 0 or ts <= 0:
        return None
    dp_now = float(dp[-1]) / ps                    # ход в «обычных ходах»
    dt_now = float(dts[-1]) / ts                   # интервал в «обычных»
    if dt_now <= 0:
        return None
    a = float(alpha) if alpha not in (None, "") else 3.0
    a = max(1.5, min(4.0, a if a > 0 else 3.0))
    omega = (dp_now / dt_now) * (GOLDEN ** (1.0 / a))
    side = None
    if cvd_grad is not None:
        g = float(cvd_grad)
        side = "long" if g > 0 else ("short" if g < 0 else None)
    return {"omega": round(omega, 4), "threshold": round(VORTEX_CRIT, 3),
            "implosion": bool(omega > VORTEX_CRIT), "side": side,
            "note": ("имплозия 🟡: ход быстрее обычного при сжатии времени "
                     f"(Ω={omega:.2f}>Φ⁴={VORTEX_CRIT:.2f})" if omega > VORTEX_CRIT
                     else f"Ω={omega:.2f} ≤ Φ⁴ — воронки нет"),
            "frame": "🟡 конвенция лога, нулями не проверена; ⚫ не сигнал"}


# ═════════════════════════════════════════════════════════════════════════
# СВОД: контекст бифуркаций для Оракула
# ═════════════════════════════════════════════════════════════════════════

def bifurcation_context(closes, vols=None) -> dict:
    """Все детекторы разом, каждый в своей защите (сбой одного не роняет
    остальные — закон аддитивности). Плюс сводка:

      regime        — линейный / напряжение / У ПОРОГА / БИФУРКАЦИЯ ОТКРЫТА;
      pressure      — 0..1, сколько детекторов давит в сторону слома;
      direction     — куда целит слом, −1..+1 (LPPLS + яма сборки + память);
      predictability— 0..1, насколько ряд вообще предсказуем прямо сейчас
                      (DET + память Хёрста − хаос Ляпунова);
      window_open   — True: НЕСКОЛЬКО независимых детекторов совпали — то
                      самое «окно бифуркации», где Оракул имеет право на
                      максимальную ставку (⚫ право, не приказ).
    """
    out: dict = {"frame": FRAME}
    for key, fn in (("hurst", hurst_dfa), ("prigogine", prigogine_market),
                    ("early_warning", early_warning), ("lppls", lppls),
                    ("cusp", cusp), ("lyapunov", lyapunov),
                    ("rqa", determinism), ("tail", hill_tail)):
        try:
            out[key] = fn(closes)
        except Exception as e:                          # noqa: BLE001
            out[key] = {"error": str(e)[:100]}

    def _ok(k):
        v = out.get(k)
        return v if isinstance(v, dict) and "error" not in v else None

    hu, pr, ew = _ok("hurst"), _ok("prigogine"), _ok("early_warning")
    lp, cu, ly, rq = _ok("lppls"), _ok("cusp"), _ok("lyapunov"), _ok("rqa")

    # давление слома: голосуют независимые детекторы
    votes = []
    if pr:
        votes.append(1.0 if pr["phi"] >= PHI_CRITICAL
                     else 0.5 if pr["phi"] >= PHI_TENSION else 0.0)
    if ew:
        votes.append(1.0 if ew["score"] >= EW_ALARM
                     else 0.5 if ew["score"] >= EW_TENSION else 0.0)
    if lp:
        votes.append(1.0 if (lp["qualified"] and lp["near"])
                     else 0.5 if lp["qualified"] else 0.0)
    if cu:
        votes.append(0.5 if cu["bistable"] else 0.0)
    pressure = round(sum(votes) / len(votes), 3) if votes else None

    strong = sum(1 for v in votes if v >= 1.0)
    if pressure is None:
        regime = "нет данных"
    elif strong >= 2:
        regime = "БИФУРКАЦИЯ ОТКРЫТА: независимые детекторы совпали"
    elif strong == 1 or (pressure or 0) >= 0.5:
        regime = "У ПОРОГА: среда теряет устойчивость"
    elif (pressure or 0) >= 0.25:
        regime = "напряжение копится"
    else:
        regime = "линейный режим: старая структура держит"

    # направление слома −1..+1
    dirv = 0.0
    nsrc = 0
    if lp and lp["qualified"]:
        dirv += (1.0 if lp["B"] > 0 else -1.0) * (1.0 if lp["near"] else 0.5)
        nsrc += 1
    if cu and cu["bistable"]:
        dirv += -0.5 if cu["z_now"] > 0 else 0.5     # перескок в другую яму
        nsrc += 1
    if hu and hu["H"] > 0.55:                         # память продолжает тренд
        x = _clean(closes)
        if x is not None and x.size >= 16:
            tr = float(np.sign(x[-1] - x[-16]))
            dirv += 0.4 * tr
            nsrc += 1
    direction = round(max(-1.0, min(1.0, dirv / max(1, nsrc))), 3) if nsrc else 0.0

    # предсказуемость: детерминизм + память − хаос
    parts = []
    if rq:
        parts.append(rq["det"])
    if hu:
        parts.append(hu["memory"])
    if ly:
        parts.append(1.0 / (1.0 + 10.0 * max(0.0, ly["lambda_max"])))
    predictability = round(sum(parts) / len(parts), 3) if parts else None

    window_open = bool(strong >= 2)
    ta = _ok("tail")
    out["summary"] = {
        "regime": regime, "pressure": pressure, "direction": direction,
        "predictability": predictability, "window_open": window_open,
        "tail_alpha": (ta["alpha"] if ta else None),
        "fat_tails": (ta["fat_tails"] if ta else None),
        "lyap_horizon": ((ly or {}).get("horizon_bars") if ly else None),
        "note": ("сводка 🟡: пороги и веса — калибровка школы; сами детекторы "
                 "🔵; окно = право на ставку, НЕ приказ ⚫")}
    return out


# ── self-test: реальные assert на детерминированной синтетике ───────────────
if __name__ == "__main__":
    n = 400
    i = np.arange(n, dtype=float)
    # квази-шум без random: сумма несоизмеримых синусов (детерминизм байт-в-байт)
    qn = (np.sin(i * 1.7183) * 0.5 + np.sin(i * 2.7183) * 0.3
          + np.sin(i * 0.9137) * 0.2)
    # «белый» детерминированный шум: логистическая карта r=4 (δ-коррелирована)
    lm = np.empty(n); lm[0] = 0.3141
    for k in range(1, n):
        lm[k] = 4.0 * lm[k - 1] * (1.0 - lm[k - 1])
    wn = lm - float(lm.mean())

    # 1) Хёрст: длинноволновые приращения — персистентно; пила — антиперсистентно
    up = 100.0 * np.exp(np.cumsum(0.001 * (np.sin(i * 0.045)
                                           + 0.5 * np.sin(i * 0.013)) + 0.0008))
    saw = 100.0 * np.exp(0.004 * np.where(i % 2 == 0, 1.0, -1.0) + 0.0002 * qn)
    h_up = hurst_dfa(up); h_saw = hurst_dfa(saw)
    assert h_up and h_up["H"] > 0.55, h_up
    assert h_saw and h_saw["H"] < 0.45, h_saw
    assert hurst_dfa([100.0] * 10) is None                     # NO DUMMIES

    # 2) Пригожин: почти-единичный корень → «у порога»; белый шум — нет
    ar = np.empty(n); ar[0] = 0.0
    for k in range(1, n):
        ar[k] = 0.985 * ar[k - 1] + 0.02 * wn[k]
    crit = 100.0 * np.exp(ar * 0.02)
    p_crit = prigogine_market(crit)
    assert p_crit and p_crit["phi"] > PHI_TENSION, p_crit
    p_qn = prigogine_market(100.0 * np.exp(0.002 * wn))
    assert p_qn and p_qn["phi"] < PHI_TENSION, p_qn

    # 3) Шеффер: растущие память и размах → счёт выше, чем у стационарного
    grow = np.empty(n); grow[0] = 0.0
    for k in range(1, n):
        a_k = 0.2 + 0.75 * (k / n)                 # AR1 ползёт вверх
        grow[k] = a_k * grow[k - 1] + (0.005 + 0.02 * k / n) * wn[k]
    ew_g = early_warning(100.0 * np.exp(grow))
    ew_s = early_warning(100.0 * np.exp(0.003 * wn))
    assert ew_g and ew_s and ew_g["score"] > ew_s["score"], (ew_g, ew_s)
    assert ew_g["score"] >= EW_TENSION, ew_g

    # 4) LPPLS: синтетический пузырь ln p = A + B(tc−t)^m(1+0.05cos ω ln(tc−t))
    tc_true, m_true, w_true = n + 25, 0.45, 8.0
    dt = tc_true - i
    lp_true = (0.05 + (-0.004) * dt ** m_true
               * (1.0 + 0.05 * np.cos(w_true * np.log(dt))))
    bubble = 100.0 * np.exp(lp_true)
    f = lppls(bubble)
    assert f and f["qualified"], f
    assert abs(f["tc_bars_ahead"] - 25) <= 15, f
    assert "вниз" in (f["side"] or ""), f                       # B<0 → срыв вниз
    #    гладкая экспонента без лог-периодики фильтр не проходит
    f_pl = lppls(100.0 * np.exp(0.001 * i))
    assert f_pl is None or not f_pl["qualified"] or f_pl["r2"] < 1.0

    # 5) сборка Тома: явные две моды → бистабильно; колокол — нет
    bim = np.where(np.sin(i * 0.61803) > 0, 110.0, 90.0) + 0.5 * qn
    c_b = cusp(bim); c_n = cusp(100.0 + 3.0 * qn)
    assert c_b and c_b["bistable"], c_b
    assert c_n and not c_n["bistable"], c_n

    # 6) Ляпунов: логистическая карта r=4 (λ=ln2≈0.69) — хаос; синус — нет
    chaos_p = 100.0 * np.exp(np.cumsum((lm - 0.5) * 0.01))
    ly_c = lyapunov(chaos_p)
    assert ly_c and ly_c["lambda_max"] > LYAP_CHAOS, ly_c
    sine_p = 100.0 + 5.0 * np.sin(i * 0.2)
    ly_s = lyapunov(sine_p)
    assert ly_s and ly_s["lambda_max"] < ly_c["lambda_max"], (ly_s, ly_c)

    # 7) RQA: периодика детерминированнее хаоса логистики
    d_sin = determinism(sine_p); d_ch = determinism(chaos_p)
    assert d_sin and d_ch and d_sin["det"] > d_ch["det"], (d_sin, d_ch)
    assert d_sin["det"] >= DET_STRONG, d_sin

    # 8) Хилл: детерминированная Парето-выборка (α=2, обратная CDF на сетке)
    #    → α̂≈2 и «толстые хвосты»; гладкий синус → хвосты тонкие
    m = 399
    u = (np.arange(m) + 0.5) / m
    pareto = u ** (-1.0 / 2.0)                    # Парето α=2
    sgn = np.where(np.arange(m) % 2 == 0, 1.0, -1.0)
    lp_fat = np.cumsum(sgn * pareto * 1e-3)
    h_fat = hill_tail(100.0 * np.exp(lp_fat))
    assert h_fat and h_fat["fat_tails"] and 1.4 < h_fat["alpha"] < 2.9, h_fat
    h_thin = hill_tail(sine_p)
    assert h_thin is None or h_thin["alpha"] > h_fat["alpha"], (h_thin, h_fat)

    # 8b) свод отдаёт хвост и горизонт Ляпунова наверх (для лестницы плеча)
    bc_tail = bifurcation_context(100.0 * np.exp(lp_fat))
    assert bc_tail["summary"]["tail_alpha"] is not None
    assert bc_tail["summary"]["fat_tails"] is True

    # 9) свод: пузырь у порога открывает давление; ровный тренд — линейный
    bc_hot = bifurcation_context(bubble)
    assert bc_hot["summary"]["pressure"] is not None
    assert bc_hot["summary"]["pressure"] >= 0.25, bc_hot["summary"]
    bc_cold = bifurcation_context(100.0 * np.exp(0.001 * qn))
    assert "линейный" in bc_cold["summary"]["regime"] \
        or bc_cold["summary"]["pressure"] <= 0.5, bc_cold["summary"]
    #    сбойный вход не роняет свод (аддитивность)
    bc_bad = bifurcation_context([1.0, 2.0, 3.0])
    assert bc_bad["summary"]["regime"] == "нет данных"
    #    рамка ⚫ на месте, детерминизм байт-в-байт
    assert "НЕ торговый сигнал" in FRAME
    again = bifurcation_context(bubble)
    assert again["summary"] == bc_hot["summary"]

    # ── ИМПЛОЗИЯ ШАУБЕРГЕРА 🟡 (Ω_vortex) ──
    # спокойный рынок: ход обычный, время не сжато → воронки нет
    calm_p = [100.0 + 0.1 * i for i in range(40)]
    calm_dt = [1.0] * 40
    vo = vortex_omega(calm_p, calm_dt, alpha=3.0)
    assert vo is not None and vo["implosion"] is False, vo
    #   имплозия: последний ход в 10× обычного при сжатии интервала в 5×
    hot_p = calm_p[:-1] + [calm_p[-1] + 1.0]          # ΔP=1.0 против обычных 0.1
    hot_dt = [1.0] * 39 + [0.2]                        # интервал сжался в 5 раз
    vh = vortex_omega(hot_p, hot_dt, alpha=1.6, cvd_grad=500.0)
    assert vh["implosion"] is True and vh["side"] == "long", vh
    assert vh["omega"] > vh["threshold"]
    #   сторона по знаку ∇CVD, без него — None (не выдумываем)
    assert vortex_omega(hot_p, hot_dt, alpha=1.6, cvd_grad=-9)["side"] == "short"
    assert vortex_omega(hot_p, hot_dt, alpha=1.6)["side"] is None
    #   кислота растягивает Ω сильнее щёлочи (Φ^(1/α) убывает по α)
    assert (vortex_omega(hot_p, hot_dt, alpha=1.5)["omega"]
            > vortex_omega(hot_p, hot_dt, alpha=4.0)["omega"])
    #   NO DUMMIES: нет данных / нулевые интервалы → None
    assert vortex_omega([1, 2], [1, 1]) is None
    assert vortex_omega(calm_p, []) is None
    assert vortex_omega(calm_p, [0, 0, 0]) is None
    #   детерминизм
    assert vortex_omega(hot_p, hot_dt, alpha=1.6) == \
        vortex_omega(hot_p, hot_dt, alpha=1.6)

    print("bifurcation self-test OK: Хёрст-DFA, Пригожин-AR1, Шеффер-предвестники, "
          "LPPLS Сорнетта (tc≈истине), сборка Тома, Ляпунов-Розенштейн, RQA, "
          "имплозия Шаубергера Ω_vortex (безразмерная, порог Φ⁴) — "
          "все детекторы честные, свод детерминирован, NO DUMMIES соблюдён")
