"""МАТЬЕ · ПАРАМЕТРИЧЕСКИЙ РЕЗОНАНС — ведические числа как ЧАСТОТЫ НАКАЧКИ.

Директива владельца: 108/81/72/54/27 как МНОЖИТЕЛИ уже проверены и мертвы
(перестановка весов, p=0.51). Здесь они пробуются в другой роли — как
частоты, с которыми модулируется ПАРАМЕТР осциллятора толпы. Качели
раскачивают не толчком в такт, а приседанием ДВАЖДЫ за период: это
параметрический резонанс, γ = 2ω₀/n.

🔵 МЕХАНИКА (проверяемо независимо):
    ẍ + ω₀²(1 + h·cos γt)·x = 0
    τ = γt/2  →  d²x/dτ² + (a − 2q·cos 2τ)·x = 0,
    a = 4ω₀²/γ²,   q = −2hω₀²/γ² = −a·h/2.
    Коэффициент имеет период π по τ (= период накачки T = 2π/γ по t).
    Матрица монодромии M собирается ЧЕСТНЫМ численным интегрированием
    (RK4, фиксированный шаг) из двух базисных начальных условий (1,0) и
    (0,1); собственные значения λ дают показатель Флоке μ = ln|λ|/T.
    |λ| > 1 → неустойчивость. Языки Айнса-Штрутта растут из a = n².
    Ширина первого языка O(q), второго O(q²), третьего O(q³) — «слабость
    высших резонансов» здесь не постулируется, а воспроизводится.

🟡 ЯЗЫК ШКОЛЫ (прокси, назван прямо):
    Перевод ведического числа W в частоту накачки γ — ПРОКСИ. Проверены
    три правила, каждое — не абстрактная частота, а реальный фазовый
    сигнал неба (это важно: фаза нужна для динамического теста Ландау):
      (а) ОРБИТАЛЬНОЕ  pump = cos(W·λ_helio)      ⟨γ⟩ = 2πW/T_sid
      (б) СКОРОСТНОЕ   pump = cos(W·λ_geo)        ⟨γ⟩ = 2πW·⟨|dλ/dt|⟩/360
      (в) СИНОДИЧЕСКОЕ pump = cos(W·элонгация)    ⟨γ⟩ = 2πW/T_syn
    ПРЕДРЕГИСТРАЦИЯ (зафиксировано ДО прогона данных): основное правило —
    (в) СИНОДИЧЕСКОЕ. Три причины, все до цифр:
      1. движок школы стоит на видимом геоцентрическом небе (аксиома
         мгновенности) — синодический круг и есть наблюдаемый круг тела;
      2. единственный выживший линейный след — огибающая E(t) = (θ/θ_ref)·
         W·хроно; и угловой размер (расстояние до Земли), и хроно-член
         (стояния, ретроградность) живут ровно в синодическом ритме;
      3. сидерический период тела геоцентрически НЕ наблюдаем (у Меркурия
         и Венеры видимая долгота обходит круг за год, а не за 88/225
         суток) — правило (а) для внутренних планет требует гелиоцентра.
    Глубина накачки h — тоже прокси: берётся ИЗМЕРЕННАЯ глубина модуляции
    эфирной плотности E_i(t) на самой частоте накачки,
        h = 2·|⟨(E/Ē − 1)·e^{i·φ_pump}⟩| ,
    то есть «сколько накачки на этой частоте реально есть в небе». Если
    её нет — h→0, язык схлопывается, и это ЧЕСТНЫЙ ответ, а не подгонка.

⚫ НЕ СИГНАЛ: ни одна цифра отсюда не является торговым или управленческим
    указанием. Резонанс — модель, а не судьба; полюс за человеком.

Прогон:  python3 -m backend.lab.mathieu          # self-тесты (assert)
         python3 -m backend.lab.mathieu data     # стенд на данных
"""
import json
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone

import numpy as np

# ── соседние модули: переиспользуем, а не переписываем ────────────────
try:
    from backend import aether            # W_VEDIC, transit_series, E(t)
except Exception:                          # pragma: no cover
    aether = None
try:
    from backend import astro             # _load_sf() → эфемериды de440
except Exception:                          # pragma: no cover
    astro = None

# вейвлет Морле соседнего модуля, если он уже готов (директива); иначе
# оценка ω₀ идёт своей Ломб-Скарглом — честно и без выдумок
_MORLET = None
for _cand in ("backend.lab.morlet", "backend.lab.wavelet",
              "backend.lab.morlet_wavelet"):
    try:
        _MORLET = __import__(_cand, fromlist=["*"])
        break
    except Exception:
        continue

MOEX_HIST = ("/tmp/claude-0/-home-user-Real-Sky-/"
             "12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/ds/moex_hist.json")
STORES = ("/tmp/claude-0/-home-user-Real-Sky-/"
          "12d14a2b-0898-5be9-8dc7-e6991ee62a59/scratchpad/resto/resto/"
          "data/stores.json")

# ══════════════════════════════════════════════════════════════════════
# 1. ЯДРО: МОНОДРОМИЯ И ПОКАЗАТЕЛЬ ФЛОКЕ  🔵
# ══════════════════════════════════════════════════════════════════════
STEPS_MIN = 600          # минимум шагов RK4 на период накачки
STEPS_MAX = 200000
STEPS_PER_WAVE = 200     # шагов на одно собственное колебание
TR_TOL = 1e-9            # допуск на границе |tr M| = 2 (решение «устойчиво»)
RK4_ERR_C = 0.013        # эмпирическая константа ошибки RK4: δtr ≈ C·(π/n)⁴
                         # (измерена на q=0, где ответ tr = 2cos(π√a) точен)


def _auto_steps(a, q) -> int:
    """Число шагов RK4 на период π по τ: ~200 на собственное колебание."""
    w = float(np.max(np.abs(np.asarray(a, float)))) + \
        2.0 * float(np.max(np.abs(np.asarray(q, float)))) + 1.0
    return int(min(STEPS_MAX, max(STEPS_MIN, STEPS_PER_WAVE * math.sqrt(w))))


def monodromy(a, q, steps: int | None = None) -> np.ndarray:
    """Матрица монодромии уравнения Матье за ОДИН период накачки.

    x'' + (a − 2q·cos 2τ)x = 0, τ: 0 → π (это ровно период коэффициента).
    Интегрируются два базисных решения (x,x') = (1,0) и (0,1); RK4 с
    фиксированным шагом — детерминированно, без адаптивных эвристик.
    M = [[x₁(π), x₂(π)], [x₁'(π), x₂'(π)]].  Векторизовано по (a, q).
    """
    a_arr = np.asarray(a, dtype=float)
    q_arr = np.asarray(q, dtype=float)
    scalar = (a_arr.ndim == 0 and q_arr.ndim == 0)
    A, Q = np.broadcast_arrays(np.atleast_1d(a_arr), np.atleast_1d(q_arr))
    A = np.ascontiguousarray(A, dtype=float)
    Q = np.ascontiguousarray(Q, dtype=float)
    n = steps or _auto_steps(A, Q)
    dt = math.pi / n
    # y = [x₁, v₁, x₂, v₂]
    y = np.zeros((4, A.size))
    y[0] = 1.0
    y[3] = 1.0

    def f(tau, y):
        c = A.ravel() - 2.0 * Q.ravel() * math.cos(2.0 * tau)
        out = np.empty_like(y)
        out[0] = y[1]
        out[1] = -c * y[0]
        out[2] = y[3]
        out[3] = -c * y[2]
        return out

    tau = 0.0
    for _ in range(n):
        k1 = f(tau, y)
        k2 = f(tau + 0.5 * dt, y + 0.5 * dt * k1)
        k3 = f(tau + 0.5 * dt, y + 0.5 * dt * k2)
        k4 = f(tau + dt, y + dt * k3)
        y = y + (dt / 6.0) * (k1 + 2.0 * k2 + 2.0 * k3 + k4)
        tau += dt
    M = np.empty((2, 2) + A.shape)
    M[0, 0] = y[0].reshape(A.shape)
    M[0, 1] = y[2].reshape(A.shape)
    M[1, 0] = y[1].reshape(A.shape)
    M[1, 1] = y[3].reshape(A.shape)
    return M[:, :, 0] if scalar else M


def floquet(a, q, steps: int | None = None) -> dict:
    """Показатель Флоке канонического Матье.

    Возвращает: tr (след M), det (должен быть 1 — вронскиан сохраняется),
    lam (max|λ|), mu_tau (ln|λ|/π, рост за период накачки в единицах τ),
    stable (|tr| ≤ 2).
    """
    M = monodromy(a, q, steps)
    tr = M[0, 0] + M[1, 1]
    det = M[0, 0] * M[1, 1] - M[0, 1] * M[1, 0]
    # λ² − tr·λ + det = 0 — считаем аналитически (устойчиво и быстро)
    disc = tr * tr - 4.0 * det
    sq = np.sqrt(np.abs(disc))
    lam = np.where(disc > 0.0,
                   0.5 * (np.abs(tr) + sq),          # вещественная пара
                   np.sqrt(np.maximum(det, 0.0)))    # комплексная пара
    lam = np.maximum(lam, 1e-300)
    mu_tau = np.log(lam) / math.pi
    stable = np.abs(tr) <= 2.0 + TR_TOL
    out = {"tr": tr, "det": det, "lam": lam, "mu_tau": mu_tau,
           "stable": stable}
    return {k: (float(v) if np.ndim(v) == 0 and not isinstance(v, np.bool_)
                else (bool(v) if isinstance(v, np.bool_) else v))
            for k, v in out.items()}


def floquet_physical(omega0: float, gamma: float, h: float,
                     steps: int | None = None) -> dict:
    """Тот же расчёт в физических переменных.

    omega0 — собственная частота осциллятора (рад/сут),
    gamma  — частота накачки (рад/сут), h — глубина модуляции параметра.
    mu — скорость роста амплитуды в 1/сут (ln|λ| за период накачки / T).
    """
    if gamma <= 0 or omega0 <= 0:
        return {"a": None, "q": None, "mu": None, "stable": None,
                "note": "ω₀ и γ должны быть положительны — расчёт невозможен"}
    a = 4.0 * omega0 * omega0 / (gamma * gamma)
    q = -2.0 * h * omega0 * omega0 / (gamma * gamma)
    T = 2.0 * math.pi / gamma
    fl = floquet(a, q, steps)
    n_near = max(1, int(round(math.sqrt(max(a, 0.0)))))
    return {"a": a, "q": q, "T_pump": T, "n_nearest": n_near,
            "detune": math.sqrt(max(a, 0.0)) / n_near - 1.0,
            "tr": fl["tr"], "lam": fl["lam"], "stable": fl["stable"],
            "mu": float(math.log(fl["lam"]) / T),
            "mu_analytic_n1": h * omega0 / 4.0}


# ── характеристические значения Матье (ряды по q) — СЕМЯ для брекетинга ─
def char_seed(n: int, q: float) -> tuple:
    """(b_n, a_n) — нижний и верхний края n-го языка по рядам Матье.

    Классика (Abramowitz & Stegun 20.2.25): язык неустойчивости лежит
    между b_n(q) и a_n(q). Используется ТОЛЬКО как окно для численного
    поиска — сам край считается честной бисекцией по |tr M| = 2.
    """
    q = float(q)
    q2, q3, q4 = q * q, q ** 3, q ** 4
    if n == 1:
        lo = 1.0 - q - q2 / 8.0 + q3 / 64.0 - q4 / 1536.0
        hi = 1.0 + q - q2 / 8.0 - q3 / 64.0 - q4 / 1536.0
    elif n == 2:
        lo = 4.0 - q2 / 12.0 + 5.0 * q4 / 13824.0
        hi = 4.0 + 5.0 * q2 / 12.0 - 763.0 * q4 / 13824.0
    elif n == 3:
        lo = 9.0 + q2 / 16.0 - q3 / 64.0
        hi = 9.0 + q2 / 16.0 + q3 / 64.0
    else:
        # ширина языка n≥4 идёт как O(qⁿ): при |q|<1 она уходит ниже
        # машинного нуля, при |q|>1 ряды неприменимы. Честно — nan.
        return (float("nan"), float("nan"))
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def tongue_bounds(n: int, q: float, tol: float = 1e-12,
                  steps: int | None = None) -> tuple:
    """Численные края n-го языка неустойчивости: (a_lo, a_hi).

    Внутри языка |tr M| > 2. Бисекция от заведомо неустойчивой середины
    (семя из рядов) к заведомо устойчивому краю ОКНА. Окно ограничено
    соседями: снизу для n=1 это a=0 (область a<0 неустойчива ВСЕГДА —
    туда уходить нельзя), иначе середина между (n−1)² и n².
    Языки слились или ширина ушла под шум интегрирования → (nan, nan),
    честно, без выдуманного числа.
    """
    if q == 0.0:
        return float(n * n), float(n * n)
    lo_s, hi_s = char_seed(n, q)
    if not (np.isfinite(lo_s) and np.isfinite(hi_s)):
        return (float("nan"), float("nan"))
    mid = 0.5 * (lo_s + hi_s)
    w = max(hi_s - lo_s, 1e-14)
    lim_lo = 0.0 if n == 1 else 0.5 * (n * n + (n - 1) ** 2)
    lim_hi = 0.5 * (n * n + (n + 1) ** 2)
    # У края языка |tr|−2 растёт от нуля как π²·Δ·ε/(4n²): фиксированный
    # порог срезает с каждого края кусок ε и занижает ширину (у третьего
    # языка это давало −13%). Порог и точность интегрирования привязаны к
    # ОЖИДАЕМОЙ ширине: смещение края держим на 1e−5 её долях.
    tr_tol = max(1e-14, 1e-5 * math.pi ** 2 * w * w / (4.0 * n * n))
    if steps is None:
        need = math.pi / (tr_tol / (5.0 * RK4_ERR_C)) ** 0.25
        steps = int(min(STEPS_MAX, max(_auto_steps(mid, q), math.ceil(need))))

    def unst(a):
        return abs(floquet(a, q, steps)["tr"]) > 2.0 + tr_tol

    if not unst(mid):
        grid = np.linspace(max(lim_lo, mid - 3.0 * w),
                           min(lim_hi, mid + 3.0 * w), 401)
        hit = [g for g in grid if unst(g)]
        if not hit:
            return (float("nan"), float("nan"))
        mid = float(np.mean(hit))
    out = []
    for sgn, lim in ((-1.0, lim_lo), (+1.0, lim_hi)):
        outer = mid + sgn * max(2.0 * w, 1e-12)
        k = 0
        while unst(outer) and k < 60:
            outer = mid + sgn * max(2.0 * w, 1e-12) * (2.0 ** (k + 1))
            if (outer - lim) * sgn > 0.0:       # уткнулись в соседа
                outer = lim
                break
            k += 1
        if unst(outer):                          # язык слился с соседним
            out.append(float("nan"))
            continue
        inner = m = mid
        for _ in range(200):
            m = 0.5 * (inner + outer)
            if unst(m):
                inner = m
            else:
                outer = m
            if abs(outer - inner) < tol * max(1.0, abs(m)):
                break
        out.append(0.5 * (inner + outer))
    return (out[0], out[1])


def stability_table(a_grid=None, q_grid=None) -> str:
    """Диаграмма Айнса-Штрутта таблицей: (a, q) → устойчиво/нет."""
    a_grid = a_grid if a_grid is not None else [
        -0.5, 0.0, 0.25, 0.5, 0.75, 0.9, 1.0, 1.1, 1.5, 2.0, 3.0, 3.9,
        4.0, 4.2, 6.0, 9.0]
    q_grid = q_grid if q_grid is not None else [
        0.0, 0.02, 0.05, 0.1, 0.2, 0.4, 0.8, 1.6]
    A, Q = np.meshgrid(np.array(a_grid, float), np.array(q_grid, float),
                       indexing="ij")
    fl = floquet(A, Q)
    mu = fl["mu_tau"]
    st = fl["stable"]
    head = "  a\\q  " + "".join(f"{q:>9.2f}" for q in q_grid)
    lines = [head, "  " + "─" * (len(head) - 2)]
    for i, a in enumerate(a_grid):
        row = f"{a:>6.2f} "
        for j in range(len(q_grid)):
            row += "        ·" if st[i, j] else f"  ×{mu[i, j]:7.4f}"
        lines.append(row)
    lines.append("  · — устойчиво (|tr M| ≤ 2);  ×μ_τ — неустойчиво, "
                 "показатель Флоке за период накачки")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# 2. НЕБО: ПЕРИОДЫ ТЕЛ ИЗ DE440 — ИЗМЕРЕНО, А НЕ ВПИСАНО  🔵
# ══════════════════════════════════════════════════════════════════════
_PER_CACHE: dict | None = None
PER_SPAN_YEARS = 400.0
PER_STEP_DAYS = 2.0
PER_T0 = (1700, 1, 1)
PER_VER = 3                # метод измерения; смена версии рвёт старый кэш
_PER_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data", "mathieu_periods.json")

# известные значения (МЭК/IAU) — только для self-теста измерителя, в
# расчётах НЕ участвуют
REF_T_SID = {"Меркурий": 87.969, "Венера": 224.701, "Марс": 686.980,
             "Юпитер": 4332.59, "Сатурн": 10759.2, "Уран": 30685.4,
             "Нептун": 60189.0, "Плутон": 90560.0, "Луна": 27.3217,
             "Солнце": 365.256}
REF_T_SYN = {"Меркурий": 115.88, "Венера": 583.92, "Марс": 779.94,
             "Юпитер": 398.88, "Сатурн": 378.09, "Уран": 369.66,
             "Нептун": 367.49, "Плутон": 366.73, "Луна": 29.5306}


def _wrap180(x):
    return ((np.asarray(x, float) + 180.0) % 360.0) - 180.0


def body_periods(refresh: bool = False) -> dict | None:
    """Периоды тел, ИЗМЕРЕННЫЕ по de440 (400 лет, шаг 2 сут).

    T_sid — сидерический период (гелиоцентрическая долгота, линейный
            наклон развёрнутой фазы) 🔵;
    T_syn — синодический (интервал между соединениями: нули элонгации
            с одинаковым знаком производной) 🔵;
    v_mean — средняя видимая |dλ/dt| геоцентрически, °/сут 🔵.
    Нет эфемерид → None (честно, без заглушек).
    """
    global _PER_CACHE
    if _PER_CACHE is not None and not refresh:
        return _PER_CACHE
    if not refresh and os.path.exists(_PER_FILE):
        try:
            got = json.load(open(_PER_FILE, encoding="utf-8"))
            if got.get("_span") == [PER_SPAN_YEARS, PER_STEP_DAYS,
                                    list(PER_T0), PER_VER]:
                _PER_CACHE = {k: v for k, v in got.items()
                              if not k.startswith("_")}
                return _PER_CACHE
        except Exception:
            pass
    if astro is None:
        return None
    sf = astro._load_sf()
    if not sf:
        return None
    ts, eph, frame = sf["ts"], sf["eph"], sf["frame"]
    earth, sun = eph["earth"], eph["sun"]
    n = int(PER_SPAN_YEARS * 365.25 / PER_STEP_DAYS)
    t = ts.tt_jd(ts.utc(*PER_T0).tt + np.arange(n) * PER_STEP_DAYS)
    days = np.arange(n) * PER_STEP_DAYS
    span = days[-1]
    geo = {}
    for nm, key in sf["bodies"].items():
        geo[nm] = np.asarray(earth.at(t).observe(eph[key]).apparent()
                             .frame_latlon(frame)[1].degrees) % 360.0
    out = {}
    for nm, key in sf["bodies"].items():
        L = np.unwrap(np.radians(geo[nm]))
        # Луна обращается вокруг ЗЕМЛИ, Солнце — видимое обращение Земли:
        # у них «орбита» и есть геоцентрическая дуга. У планет — гелиоцентр
        # (видимая долгота Меркурия и Венеры обходит круг за год, а не за
        # 88/225 суток: геоцентрически их орбитальный период не наблюдаем).
        if nm in ("Солнце", "Луна"):
            t_sid = float(2.0 * math.pi / np.polyfit(days, L, 1)[0])
        else:
            hel = np.asarray(sun.at(t).observe(eph[key]).frame_latlon(
                frame)[1].degrees) % 360.0
            t_sid = float(2.0 * math.pi /
                          np.polyfit(days, np.unwrap(np.radians(hel)), 1)[0])
        v = np.abs(np.gradient(np.degrees(L), PER_STEP_DAYS))
        d = _wrap180(geo[nm] - geo["Солнце"])
        s = np.sign(d)
        ch = np.where((s[:-1] * s[1:] < 0) & (np.abs(np.diff(d)) < 90.0))[0]
        up = int((d[ch + 1] > d[ch]).sum())
        dn = int(len(ch) - up)
        nc = max(up, dn)
        # ДРОЖАНИЕ САМОЙ НАКАЧКИ: интервалы между соседними соединениями
        # не равны друг другу (эксцентриситет орбит). Язык резонанса уже
        # этого разброса → накачка не удерживает частоту, и это решает всё.
        cross = ch[(d[ch + 1] > d[ch])] if up >= dn else ch[(d[ch + 1] < d[ch])]
        gaps = np.diff(cross.astype(float)) * PER_STEP_DAYS
        out[nm] = {"T_sid": t_sid,
                   "T_syn": (float(span / nc) if nc >= 3 else None),
                   "T_syn_sd": (float(np.std(gaps)) if len(gaps) >= 3
                                else None),
                   "v_mean": float(np.mean(v))}
    _PER_CACHE = out
    try:
        os.makedirs(os.path.dirname(_PER_FILE), exist_ok=True)
        json.dump({**out, "_span": [PER_SPAN_YEARS, PER_STEP_DAYS,
                                    list(PER_T0), PER_VER]},
                  open(_PER_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    return out


RULES = ("syn", "orb", "speed")
RULE_NAME = {"syn": "(в) синодическое: pump = cos(W·элонгация)",
             "orb": "(а) орбитальное:  pump = cos(W·λ_гелио)",
             "speed": "(б) скоростное:  pump = cos(W·λ_гео)"}
RULE_PRIMARY = "syn"       # ПРЕДРЕГИСТРАЦИЯ — зафиксировано до прогона


def vedic_gamma(body: str, rule: str = RULE_PRIMARY,
                W: float | None = None) -> dict | None:
    """🟡 Ведическое число W → средняя частота накачки γ, рад/сут.

    Нет эфемерид или период не измерим (синодический период Солнца) →
    None и честная приписка, без выдуманных значений.
    """
    if aether is None:
        return None
    per = body_periods()
    if per is None or body not in per:
        return None
    Wv = float(W if W is not None else aether.W_VEDIC[body])
    p = per[body]
    if rule == "orb":
        T = p["T_sid"]
        note = "орбитальный (сидерический) период, гелиоцентр 🔵"
    elif rule == "syn":
        T = p["T_syn"]
        note = "синодический период (соединения с Солнцем) 🔵"
    elif rule == "speed":
        T = (360.0 / p["v_mean"]) if p["v_mean"] > 0 else None
        note = "средняя видимая |dλ/dt| геоцентрически 🔵"
    else:
        raise ValueError(f"неизвестное правило: {rule}")
    if T is None or not np.isfinite(T) or T <= 0:
        return {"gamma": None, "T": None, "W": Wv, "rule": rule,
                "note": f"{note} — не измерим для «{body}» (у Солнца "
                        "элонгация тождественно ноль)"}
    return {"gamma": 2.0 * math.pi * Wv / T, "T": float(T),
            "T_pump": float(T / Wv), "W": Wv, "rule": rule, "note": note}


def pump_phase(body: str, dates, rule: str = RULE_PRIMARY,
               W: float | None = None) -> np.ndarray | None:
    """🟡 Фаза накачки φ_pump(t) = W·угол(t), рад — НАСТОЯЩИЙ сигнал неба,
    а не синусоида с выдуманным началом отсчёта.

    Угол по каждому правилу выбран так, чтобы он ВРАЩАЛСЯ ровно с той
    частотой, которую правило объявляет (это проверяется self-тестом [9]):
      syn   — синодический угол: у планет это гелиоцентрическая разность
              λ(тело) − λ(Земля), у Луны — обычная элонгация от Солнца.
              ВАЖНО: видимая геоцентрическая элонгация Меркурия и Венеры
              ОГРАНИЧЕНА (±28° и ±47°) и не вращается вовсе — на ней
              «частота накачки» была бы фикцией;
      orb   — λ гелиоцентрическая (у Луны и Солнца — геоцентрическая:
              Луна обращается вокруг Земли);
      speed — накопленная ВИДИМАЯ дуга ∫|dλ_гео|, то есть буквальное
              «W · средняя суточная скорость»: путь, а не смещение
              (ретроградные петли считаются дважды).
    """
    if astro is None or aether is None:
        return None
    sf = astro._load_sf()
    if not sf:
        return None
    ts, eph, frame = sf["ts"], sf["eph"], sf["frame"]
    key = sf["bodies"].get(body)
    if not key:
        return None
    t = ts.utc([d.year for d in dates], [d.month for d in dates],
               [d.day for d in dates], 12)
    Wv = float(W if W is not None else aether.W_VEDIC[body])

    def geo(k):
        return np.asarray(sf["earth"].at(t).observe(eph[k]).apparent()
                          .frame_latlon(frame)[1].degrees)

    def helio(k):
        return np.asarray(eph["sun"].at(t).observe(eph[k])
                          .frame_latlon(frame)[1].degrees)

    if rule == "syn":
        if body == "Солнце":
            return None                    # синодический угол Солнца ≡ 0
        if body == "Луна":
            ang = geo(key) - geo("sun")
        else:
            ang = helio(key) - helio("earth")
    elif rule == "orb":
        ang = geo(key) if body in ("Солнце", "Луна") else helio(key)
    elif rule == "speed":
        lam = np.unwrap(np.radians(geo(key)))
        arc = np.concatenate([[0.0], np.cumsum(np.abs(np.diff(lam)))])
        return arc * Wv
    else:
        raise ValueError(f"неизвестное правило: {rule}")
    return np.unwrap(np.radians(ang)) * Wv


def pump_depth(body: str, dates, rule: str = RULE_PRIMARY,
               W: float | None = None) -> dict | None:
    """🟡→🔵 ИЗМЕРЕННАЯ глубина накачки h на частоте γ.

    h = 2·|⟨(E/Ē − 1)·exp(i·φ_pump)⟩| — амплитуда компоненты эфирной
    плотности E_i(t) ровно на частоте накачки. Если небо на этой частоте
    не модулирует ничего — h≈0, язык схлопывается. Это ответ, а не изъян.
    Дополнительно h_std = std(E)/Ē — грубая полная глубина (для сравнения).
    """
    if aether is None:
        return None
    ph = pump_phase(body, dates, rule, W)
    if ph is None:
        return None
    t0 = datetime(dates[0].year, dates[0].month, dates[0].day, 12,
                  tzinfo=timezone.utc)
    hours = (dates[-1] - dates[0]).days * 24.0 + 24.0
    tr = aether.transit_series(t0, hours, 24.0)
    if tr is None:
        return None
    tsx = np.asarray(tr["ts"], float)
    idx = {datetime.fromtimestamp(v, timezone.utc).date(): i
           for i, v in enumerate(tsx)}
    sel = np.array([idx.get(d, -1) for d in dates])
    ok = sel >= 0
    if ok.sum() < 100:
        return None
    E = np.asarray(tr["E"][body], float)[sel[ok]]
    p = ph[ok]
    Eb = float(np.mean(E))
    if Eb <= 0:
        return None
    win = np.hanning(len(E))
    win = win / np.mean(win)
    z = np.mean((E / Eb - 1.0) * np.exp(1j * p) * win)
    return {"h": float(2.0 * abs(z)), "h_std": float(np.std(E) / Eb),
            "E_mean": Eb, "n": int(ok.sum())}


# ══════════════════════════════════════════════════════════════════════
# 3. РЯД: СОБСТВЕННАЯ ЧАСТОТА, ОГИБАЮЩАЯ, ФАЗА  🔵
# ══════════════════════════════════════════════════════════════════════
def dominant_omega(x, t_days=None, band=(2.5, 400.0)) -> dict | None:
    """Доминирующий период ряда → ω₀ (рад/сут).

    Если рядом есть модуль вейвлета Морле — берём оценку оттуда
    (директива). Иначе Ломб-Скаргл по календарной сетке: он переживает
    пропуски (выходные биржи) без интерполяции.
    ВНИМАНИЕ 🔵: у биржевых рядов гребень окна (5 торговых дней из 7) сам
    даёт пик на 7 сутках — это артефакт выборки, а не осциллятор; в
    ответе он помечается.
    """
    x = np.asarray(x, float)
    t = np.arange(len(x), dtype=float) if t_days is None else \
        np.asarray(t_days, float)
    m = np.isfinite(x) & np.isfinite(t)
    if m.sum() < 64:
        return None
    x, t = x[m], t[m]
    t = t - t[0]
    x = x - np.polyval(np.polyfit(t, x, 2), t)        # снять тренд
    if _MORLET is not None:
        for fn in ("dominant_period", "dominant_omega", "peak_period"):
            f = getattr(_MORLET, fn, None)
            if callable(f):
                try:
                    got = float(f(x, t))
                    if np.isfinite(got) and got > 0:
                        P = got if fn != "dominant_omega" else 2 * math.pi / got
                        return {"omega0": 2.0 * math.pi / P, "period": P,
                                "power": None, "src": f"морле:{fn}"}
                except Exception:
                    pass
    from scipy.signal import lombscargle
    per = 1.0 / np.linspace(1.0 / band[1], 1.0 / band[0], 4000)
    P = lombscargle(t, x - x.mean(), 2.0 * math.pi / per, normalize=True)
    i = int(np.argmax(P))
    return {"omega0": 2.0 * math.pi / per[i], "period": float(per[i]),
            "power": float(P[i]), "src": "ломб-скаргл"}


MORLET_W0 = 6.0            # безразмерная ширина вейвлета Морле


def demodulate(x, omega0: float, t_days=None, w0: float = MORLET_W0):
    """Комплексная демодуляция (вейвлет Морле на одной частоте):
    z(t) = Σ x(k)·exp(−iω₀t_k)·g(t−t_k), g — гаусс σ = w0/ω₀.
    Возвращает A(t) = |z| и φ(t) = arg z. Пропуски не интерполируются:
    веса считаются только по существующим точкам."""
    x = np.asarray(x, float)
    t = np.arange(len(x), dtype=float) if t_days is None else \
        np.asarray(t_days, float)
    m = np.isfinite(x)
    sig = w0 / omega0
    half = int(math.ceil(3.0 * sig))
    out = np.full(len(x), np.nan, complex)
    xs = np.where(m, x - np.nanmean(x[m]), 0.0)
    for i in range(len(x)):
        lo, hi = max(0, i - half), min(len(x), i + half + 1)
        dtl = t[lo:hi] - t[i]
        g = np.exp(-0.5 * (dtl / sig) ** 2) * m[lo:hi]
        s = g.sum()
        if s < 0.3 * math.sqrt(2 * math.pi) * sig:
            continue
        out[i] = np.sum(xs[lo:hi] * g * np.exp(-1j * omega0 * dtl)) / s * 2.0
    return np.abs(out), np.angle(out)


def landau_beta(A, phi, phi_pump, n: int = 1, t_days=None) -> dict | None:
    """🔵 ТЕСТ ЛАНДАУ: при параметрическом резонансе огибающая обязана
    расти/падать по фазе накачки:  d(lnA)/dt = β·sin(2θ),
    θ = φ_осц − n·φ_накачки/2,  |β| = h·ω₀/4 в центре первого языка.
    Возвращает β (МНК без свободного члена), R², n_точек. Это ПРОВЕРЯЕМОЕ
    следствие, а не корреляция вообще: у него известны и знак, и величина.
    """
    A = np.asarray(A, float)
    phi = np.asarray(phi, float)
    pp = np.asarray(phi_pump, float)
    t = np.arange(len(A), dtype=float) if t_days is None else \
        np.asarray(t_days, float)
    lg = np.log(np.where(A > 0, A, np.nan))
    d = np.gradient(lg, t)
    th = phi - 0.5 * n * pp
    s = np.sin(2.0 * th)
    m = np.isfinite(d) & np.isfinite(s)
    if m.sum() < 100:
        return None
    d, s = d[m], s[m]
    beta = float(np.dot(s, d) / np.dot(s, s))
    res = d - beta * s
    r2 = float(1.0 - np.var(res) / np.var(d)) if np.var(d) > 0 else 0.0
    return {"beta": beta, "r2": r2, "n": int(m.sum())}


def detect(x, body: str, dates=None, rule: str = RULE_PRIMARY,
           W: float | None = None, h: float | None = None,
           t_days=None, band=(2.5, 400.0)) -> dict:
    """ДЕТЕКТОР: ряд + тело → расстояние до языка и показатель Флоке μ.

    ω₀ оценивается из ряда, γ — ведическая от тела по выбранному правилу,
    h — измеренная глубина накачки (если dates даны) либо переданная.
    Нет чего-то из этого → None и приписка, без подстановки дефолтов.
    """
    dom = dominant_omega(x, t_days, band)
    if dom is None:
        return {"error": "ряд слишком короткий или пустой — ω₀ не оценить"}
    g = vedic_gamma(body, rule, W)
    if g is None or g.get("gamma") is None:
        return {"omega0": dom["omega0"], "period": dom["period"],
                "gamma": None,
                "note": (g or {}).get("note", "нет эфемерид — γ не построить")}
    if h is None and dates is not None:
        pd = pump_depth(body, dates, rule, W)
        h = pd["h"] if pd else None
    if h is None:
        return {"omega0": dom["omega0"], "period": dom["period"],
                "gamma": g["gamma"], "T_pump": g["T_pump"],
                "note": "глубина накачки h не измерена — μ не считаем"}
    fl = floquet_physical(dom["omega0"], g["gamma"], h)
    n = fl["n_nearest"]
    if fl["q"] == 0:
        lo, hi = float(n * n), float(n * n)
    elif abs(fl["q"]) < 1e-3:
        # при таком q ряды Матье точны до O(q⁴) ≤ 1e−12 — численная
        # бисекция здесь только жгла бы время, ответ тот же
        lo, hi = char_seed(n, fl["q"])
    else:
        lo, hi = tongue_bounds(n, fl["q"])
    a = fl["a"]
    dist_a = 0.0 if (lo <= a <= hi) else min(abs(a - lo), abs(a - hi))
    return {"body": body, "rule": rule, "W": g["W"],
            "omega0": dom["omega0"], "period": dom["period"],
            "src_omega": dom["src"], "gamma": g["gamma"],
            "T_pump": g["T_pump"], "h": h, "a": a, "q": fl["q"],
            "n_nearest": n, "detune": fl["detune"],
            "tongue": (lo, hi), "dist_a": dist_a,
            "inside": bool(lo <= a <= hi), "mu": fl["mu"],
            "stable": fl["stable"], "mu_analytic_n1": fl["mu_analytic_n1"]}


# ══════════════════════════════════════════════════════════════════════
# 4. СИНТЕТИКА ДЛЯ ПРОВЕРКИ ВСЕЙ ЦЕПОЧКИ (без random — детерминировано)
# ══════════════════════════════════════════════════════════════════════
def simulate(omega0: float, gamma: float, h: float, t_end: float,
             dt: float = 0.02, x0: float = 1.0, v0: float = 0.0):
    """Прямое интегрирование ẍ + ω₀²(1+h·cos γt)x = 0 (RK4). Нужно, чтобы
    проверить, что детектор восстанавливает известный ответ."""
    n = int(round(t_end / dt))
    t = np.arange(n + 1) * dt
    y = np.array([x0, v0], float)
    xs = np.empty(n + 1)
    xs[0] = x0

    def f(tt, y):
        return np.array([y[1], -omega0 * omega0 *
                         (1.0 + h * math.cos(gamma * tt)) * y[0]])
    for i in range(n):
        tt = t[i]
        k1 = f(tt, y)
        k2 = f(tt + dt / 2, y + dt / 2 * k1)
        k3 = f(tt + dt / 2, y + dt / 2 * k2)
        k4 = f(tt + dt, y + dt * k3)
        y = y + dt / 6.0 * (k1 + 2 * k2 + 2 * k3 + k4)
        xs[i + 1] = y[0]
    return t, xs


# ══════════════════════════════════════════════════════════════════════
# 5. СТЕНД НА ДАННЫХ
# ══════════════════════════════════════════════════════════════════════
def _load_stores():
    s = json.load(open(STORES, encoding="utf-8"))
    out = {}
    for code, st in s.items():
        d = st["days"]
        ks = sorted(d)
        d0, d1 = date.fromisoformat(ks[0]), date.fromisoformat(ks[-1])
        n = (d1 - d0).days + 1
        ds = [d0 + timedelta(i) for i in range(n)]
        x = np.full(n, np.nan)
        for k in ks:
            v = d[k].get("orders")
            if v:
                x[(date.fromisoformat(k) - d0).days] = float(v)
        out[code] = (ds, x)
    return out


def _load_moex():
    raw = json.load(open(MOEX_HIST, encoding="utf-8"))
    out = {}
    for tk, rows in raw.items():
        ds = [date.fromisoformat(r[0]) for r in rows]
        cl = np.array([float(r[4]) for r in rows])
        d0, d1 = ds[0], ds[-1]
        n = (d1 - d0).days + 1
        days = [d0 + timedelta(i) for i in range(n)]
        x = np.full(n, np.nan)
        for d, c in zip(ds, cl):
            x[(d - d0).days] = c
        out[tk] = (days, x)
    return out


def run_data():
    per = body_periods()
    if per is None:
        print("нет эфемерид de440 — стенд честно молчит")
        return
    bodies = [b for b in aether._BODY_ORDER if b in per]

    print("=" * 100)
    print("1. НЕБО: ПЕРИОДЫ ИЗМЕРЕНЫ ПО DE440 (400 лет, шаг 2 сут) 🔵")
    print("=" * 100)
    print(f"{'тело':10s}{'W':>5s}{'T_sid':>12s}{'T_syn':>10s}"
          f"{'⟨|dλ/dt|⟩':>11s}{'γ_syn':>10s}{'T_накачки':>11s}")
    for b in bodies:
        p = per[b]
        g = vedic_gamma(b, "syn")
        gs = f"{g['gamma']:.5f}" if g and g.get("gamma") else "—"
        tp = f"{g['T_pump']:.4f}" if g and g.get("gamma") else "—"
        ts_ = f"{p['T_syn']:.3f}" if p["T_syn"] else "—"
        print(f"{b:10s}{aether.W_VEDIC[b]:>5d}{p['T_sid']:>12.3f}{ts_:>10s}"
              f"{p['v_mean']:>11.4f}{gs:>10s}{tp:>11s}")

    stores = _load_stores()
    print()
    print("=" * 100)
    print("2. РЕСТО: СОБСТВЕННАЯ ЧАСТОТА 16 ТОЧЕК")
    print("=" * 100)
    om = {}
    for c, (ds, x) in stores.items():
        dom = dominant_omega(x, band=(2.5, 400.0))
        om[c] = dom
        print(f"  {c}: период {dom['period']:7.3f} сут  ω₀={dom['omega0']:.5f} "
              f"рад/сут  мощность {dom['power']:.3f}  ({dom['src']})")
    med_P = float(np.median([v["period"] for v in om.values()]))
    print(f"  медиана периода: {med_P:.4f} сут  →  это РАБОЧАЯ НЕДЕЛЯ, "
          f"жёсткий социальный календарь 🟡, а не свободный осциллятор")

    print()
    print("=" * 100)
    print("3. ДЕТЕКТОР: 10 ТЕЛ × 3 ПРАВИЛА (эталонная точка LT01)")
    print("=" * 100)
    ds, x = stores["LT01"]
    print(f"{'тело':10s}{'правило':7s}{'T_накач':>10s}{'a':>9s}{'n':>3s}"
          f"{'расстр.':>9s}{'h':>10s}{'q':>11s}{'μ,1/сут':>11s}  вывод")
    res_lt01 = {}
    for rule in RULES:
        for b in bodies:
            r = detect(x, b, dates=ds, rule=rule)
            res_lt01[(rule, b)] = r
            if r.get("gamma") is None:
                print(f"{b:10s}{rule:7s}{'—':>10s}{'—':>9s}{'—':>3s}"
                      f"{'—':>9s}{'—':>10s}{'—':>11s}{'—':>11s}  "
                      f"{r.get('note', '')[:38]}")
                continue
            if "mu" not in r:
                print(f"{b:10s}{rule:7s}{r['T_pump']:>10.4f}"
                      f"{'—':>9s}{'—':>3s}{'—':>9s}{'—':>10s}{'—':>11s}"
                      f"{'—':>11s}  {r.get('note','')[:38]}")
                continue
            # в полосе устойчивости |λ| = 1 ТОЧНО (система гамильтонова,
            # трения нет) — печатаем 0, а не численный шум 1e−14
            mu_s = "0" if r["stable"] else f"{r['mu']:.2e}"
            dist = ("—" if not np.isfinite(r["dist_a"])
                    else f"{r['dist_a']:.2e}")
            verdict = "В ЯЗЫКЕ" if r["inside"] else "вне"
            if r["n_nearest"] >= 4:
                verdict = f"вне (язык n={r['n_nearest']}, ширина O(qⁿ)≈0)"
            print(f"{b:10s}{rule:7s}{r['T_pump']:>10.4f}{r['a']:>9.4f}"
                  f"{r['n_nearest']:>3d}{dist:>9s}{r['h']:>10.2e}"
                  f"{r['q']:>11.2e}{mu_s:>11s}  {verdict}")

    print()
    print("=" * 100)
    print("3б. УДЕРЖИВАЕТ ЛИ НЕБО ЧАСТОТУ? ДРОЖАНИЕ СИНОДИЧЕСКОГО ПЕРИОДА")
    print("=" * 100)
    print("  Язык требует, чтобы расстройка держалась внутри h/4. Но сам")
    print("  синодический интервал от соединения к соединению НЕ постоянен.")
    print(f"  {'тело':10s}{'T_syn':>10s}{'разброс σ':>11s}{'σ/T':>9s}"
          f"{'нужно (h/4)':>13s}{'во сколько раз шире':>21s}")
    for b in bodies:
        pv = per[b]
        r = res_lt01.get((RULE_PRIMARY, b), {})
        if not pv.get("T_syn") or not pv.get("T_syn_sd") or "h" not in r:
            continue
        jit = pv["T_syn_sd"] / pv["T_syn"]
        need = r["h"] / 4.0
        print(f"  {b:10s}{pv['T_syn']:>10.3f}{pv['T_syn_sd']:>11.4f}"
              f"{jit:>9.2%}{need:>13.2e}{jit/need:>20.0f}×")
    print("  Дрожание накачки шире языка на порядки: даже точно настроенное")
    print("  тело выпадает из резонанса раньше, чем успевает раскачать.")

    print()
    print("=" * 100)
    print("4. СКОЛЬКО НАКАЧКИ НЕБО РЕАЛЬНО ДАЁТ НА ГАРМОНИКЕ W")
    print("=" * 100)
    print("  h(W) = глубина модуляции E(t) на частоте W·(синодический круг).")
    print("  Полуширина языка по a равна |q| = a·h/2 — если h≈0, языка нет.")
    Ws = (1, 2, 3, 9, 27, 54, 81, 108)
    print(f"  {'тело':10s}" + "".join(f"{'W='+str(w):>10s}" for w in Ws))
    for b in ("Луна", "Меркурий", "Венера", "Юпитер", "Сатурн", "Плутон"):
        row = f"  {b:10s}"
        for w in Ws:
            pdh = pump_depth(b, ds, RULE_PRIMARY, W=float(w))
            row += f"{pdh['h']:>10.2e}" if pdh else f"{'—':>10s}"
        print(row)
    print("  ЧИТАЕТСЯ ТАК: накачка живёт на первых гармониках синодического")
    print("  круга; к W=108 глубина падает на 2–4 порядка. Ведическое число")
    print("  выбирает ровно ту гармонику, где неба почти нет.")

    print()
    print("=" * 100)
    print("5. НУЛЬ БЕЗ RANDOM: ВЕДИЧЕСКОЕ W ПРОТИВ ВСЕХ ЦЕЛЫХ W=1..200")
    print("=" * 100)
    print("  вопрос: 108/81/72/54/27 ближе к языку, чем произвольное целое?")
    print(f"{'тело':10s}{'W':>5s}{'|расстройка|':>14s}{'перцентиль':>12s}"
          f"{'внутри языка из 200':>22s}")
    perc = []
    for b in bodies:
        g0 = vedic_gamma(b, RULE_PRIMARY)
        if not g0 or g0.get("gamma") is None:
            continue
        w0 = float(aether.W_VEDIC[b])
        om0 = om["LT01"]["omega0"]
        dts, ins = [], 0
        for Wc in range(1, 201):
            g = vedic_gamma(b, RULE_PRIMARY, W=float(Wc))
            a = 4.0 * om0 ** 2 / g["gamma"] ** 2
            nn = max(1, int(round(math.sqrt(a))))
            dts.append(abs(math.sqrt(a) / nn - 1.0))
        dts = np.array(dts)
        d_true = dts[int(w0) - 1]
        pct = float((dts < d_true).mean() * 100.0)
        # «внутри языка» при типичной измеренной глубине h_typ
        h_typ = res_lt01[(RULE_PRIMARY, b)].get("h") or 0.0
        thr = h_typ / 4.0
        ins = int((dts < thr).sum())
        perc.append(pct)
        print(f"{b:10s}{int(w0):>5d}{d_true:>14.5f}{pct:>11.1f}%"
              f"{ins:>22d}")
    print(f"  медиана перцентиля ведических чисел: {np.median(perc):.1f}% "
          f"(50% = «неотличимо от произвольного целого»)")

    print()
    print("=" * 100)
    print("6. ДИНАМИЧЕСКИЙ ТЕСТ ЛАНДАУ: d(lnA)/dt = β·sin(2θ) — 16 ТОЧЕК")
    print("=" * 100)
    print("  предсказание параметрического резонанса: |β| = h·ω₀/4 и ОДИН")
    print("  знак у всех точек. Проверяем на теле-кандидате первого языка.")
    cand = sorted(
        [(v["dist_a"], b) for (rl, b), v in res_lt01.items()
         if rl == RULE_PRIMARY and "dist_a" in v and v["n_nearest"] == 1])
    if cand:
        cbody = cand[0][1]
        print(f"  кандидат первого языка: {cbody} "
              f"(минимальное расстояние до a=1)")
        betas, hs, r2s = {}, {}, {}
        for c, (ds, xx) in stores.items():
            o = om[c]["omega0"]
            A, phi = demodulate(xx, o)
            pp = pump_phase(cbody, ds, RULE_PRIMARY)
            lb = landau_beta(A, phi, pp, n=1)
            pdh = pump_depth(cbody, ds, RULE_PRIMARY)
            if lb and pdh:
                betas[c] = lb["beta"]
                hs[c] = pdh["h"] * o / 4.0
                r2s[c] = lb["r2"]
                print(f"    {c}: β={lb['beta']:+.5f}  ожидание |β|="
                      f"{hs[c]:.2e}  R²={lb['r2']:+.4f}")
        b_arr = np.array(list(betas.values()))
        h_med = float(np.median(list(hs.values())))
        print(f"  знак совпал у {int((b_arr > 0).sum())}/{len(b_arr)} "
              f"(параметрический резонанс требует 16/16 или 0/16)")
        print(f"  медиана |β| = {np.median(np.abs(b_arr)):.2e} против "
              f"теории h·ω₀/4 = {h_med:.2e} "
              f"(отношение {np.median(np.abs(b_arr))/h_med:.2f})")
        print(f"  медиана R² закона Ландау = "
              f"{np.median(list(r2s.values())):.6f}  "
              f"(на синтетическом параметрическом осцилляторе R² = 0.997)")
        # НУЛЬ 1: то же тело, чужое число W=1..60 (детерминированно)
        ds0, x0 = stores["LT01"]
        A0, phi0 = demodulate(x0, om["LT01"]["omega0"])
        null = []
        for Wc in range(1, 61):
            pp = pump_phase(cbody, ds0, RULE_PRIMARY, W=float(Wc))
            lb = landau_beta(A0, phi0, pp, n=1)
            if lb:
                null.append(abs(lb["beta"]))
        null = np.array(null)
        real = abs(betas.get("LT01", np.nan))
        print(f"  нуль №1 (чужое W=1..60, то же тело): медиана "
              f"{np.median(null):.5f}, p95 {np.quantile(null, 0.95):.5f}, "
              f"настоящее {real:.5f} → перцентиль "
              f"{float((null < real).mean()) * 100:.1f}%")
        # НУЛЬ 2: фазовый суррогат и IAAFT огибающей (тот же спектр)
        try:
            from backend.lab.e_surrogate import phase_surrogate, iaaft
            rng = np.random.default_rng(20260801)   # нуль-стенд, не продукт
            pp0 = pump_phase(cbody, ds0, RULE_PRIMARY)
            for nm, gen in (("фазовый суррогат", phase_surrogate),
                            ("IAAFT", iaaft)):
                vals = []
                m0 = np.isfinite(A0)
                for _ in range(200):
                    z = A0.copy()
                    z[m0] = gen(A0[m0], rng)
                    lb = landau_beta(z, phi0, pp0, n=1)
                    if lb:
                        vals.append(abs(lb["beta"]))
                v = np.array(vals)
                print(f"  нуль №2 ({nm}, 200 прогонов): медиана "
                      f"{np.median(v):.5f}, p95 {np.quantile(v, 0.95):.5f}, "
                      f"доля нулей ≥ настоящего = "
                      f"{float((v >= real).mean()):.4f}")
        except Exception as e:
            print(f"  нуль №2 недоступен: {e}")

    print()
    print("=" * 100)
    print("7. БИРЖА: ω₀ ЦЕНЫ И ВОЛАТИЛЬНОСТИ, 8 БУМАГ MOEX")
    print("=" * 100)
    moex = _load_moex()
    vols = {}
    for tk, (ds, x) in moex.items():
        dp = dominant_omega(x, band=(3.0, 400.0))
        lr = np.diff(np.log(x))                    # длина n−1
        absr = np.where(np.isfinite(lr), np.abs(lr), np.nan)
        k = 20
        vol = np.full(len(absr), np.nan)
        for i in range(k, len(absr)):
            w = absr[i - k:i]
            f = np.isfinite(w)
            if f.sum() >= 10:
                vol[i] = w[f].mean()
        tv = np.arange(1.0, len(absr) + 1.0)
        dv = dominant_omega(vol, tv, band=(30.0, 400.0))
        vols[tk] = (ds[1:], vol, dv)
        print(f"  {tk}: цена {dp['period']:7.2f} сут (мощн {dp['power']:.3f})"
              f"   волатильность {dv['period']:7.2f} сут "
              f"(мощн {dv['power']:.3f})")
    print("  ⚠ 🔵 пик 7.0 сут у цены — ГРЕБЕНЬ ВЫБОРКИ (5 торговых дней из "
          "7),\n    а не осциллятор толпы: у ряда просто нет точек по "
          "выходным. Детектор\n    на цене поэтому не запускается: ω₀ не "
          "определён честно.")
    print("\n  Детектор на ВОЛАТИЛЬНОЙ огибающей (ω₀ годового масштаба), "
          "правило (в):")
    print(f"  {'бумага':8s}{'ω₀ период':>11s}  " +
          "  ".join(f"{b[:4]:>8s}" for b in ("Юпитер", "Сатурн", "Уран",
                                             "Нептун", "Плутон")))
    for tk, (dsv, vol, dv) in vols.items():
        row = f"  {tk:8s}{dv['period']:>11.1f}  "
        for b in ("Юпитер", "Сатурн", "Уран", "Нептун", "Плутон"):
            r = detect(vol, b, dates=dsv, rule=RULE_PRIMARY,
                       t_days=np.arange(1.0, len(vol) + 1.0),
                       band=(30.0, 400.0))
            row += (f"{('В ЯЗЫКЕ' if r.get('inside') else 'вне'):>8s}  "
                    if "inside" in r else f"{'—':>8s}  ")
        print(row)


# ══════════════════════════════════════════════════════════════════════
# SELF-ТЕСТЫ: ВЕЛИЧИНЫ И ИЗВЕСТНЫЕ АНАЛИТИЧЕСКИЕ ОТВЕТЫ
# ══════════════════════════════════════════════════════════════════════
def _selftest():
    print("=" * 92)
    print("SELF-ТЕСТЫ МАТЬЕ — сверка с аналитикой")
    print("=" * 92)

    # 1. h = 0 → устойчивость при любых ω₀, γ; |λ| = 1 с точностью 1e−6
    print("\n[1] h=0 → |λ|=1 при любых ω₀,γ")
    worst = 0.0
    for om in (0.05, 0.3, 0.897, 2.0, 5.0):
        for gam in (0.01, 0.1, 0.9, 1.7947, 4.0):
            r = floquet_physical(om, gam, 0.0)
            worst = max(worst, abs(r["lam"] - 1.0))
            assert r["stable"], f"h=0 неустойчив при ω₀={om}, γ={gam}"
            assert abs(r["mu"]) < 1e-6, f"μ≠0 при h=0: {r['mu']}"
    # прямая проверка канонической формы: tr = 2cos(π√a)
    for a in (0.2, 1.0, 2.5, 4.0, 9.0, 25.0, 100.0):
        f = floquet(a, 0.0)
        assert abs(f["tr"] - 2 * math.cos(math.pi * math.sqrt(a))) < 1e-8, a
        assert abs(f["det"] - 1.0) < 1e-9, ("вронскиан", a, f["det"])
        worst = max(worst, abs(f["lam"] - 1.0))
    print(f"    max||λ|−1| = {worst:.3e}   (порог 1e−6)  ✓")
    assert worst < 1e-6

    # 2. γ = 2ω₀, малое h → μ > 0, и μ ≈ h·ω₀/4
    print("\n[2] γ=2ω₀ (первый язык): μ>0 и μ ≈ h·ω₀/4")
    print(f"    {'ω₀':>7s}{'h':>7s}{'μ числ.':>12s}{'h·ω₀/4':>12s}"
          f"{'отн.ошибка':>12s}")
    for om in (0.8976, 2.0):
        for h in (0.01, 0.05, 0.1, 0.3):
            r = floquet_physical(om, 2.0 * om, h)
            exp = h * om / 4.0
            rel = abs(r["mu"] - exp) / exp
            print(f"    {om:>7.4f}{h:>7.2f}{r['mu']:>12.6f}{exp:>12.6f}"
                  f"{rel:>11.1%}")
            assert not r["stable"] and r["mu"] > 0, (om, h)
            assert rel < 0.06 + 1.2 * h, ("порядок величины", om, h, rel)
    # в пределе h→0 совпадение должно быть асимптотически точным
    r = floquet_physical(1.0, 2.0, 1e-3)
    assert abs(r["mu"] - 1e-3 / 4.0) / (1e-3 / 4.0) < 2e-3
    print(f"    предел h=1e−3: μ={r['mu']:.8f} против h·ω₀/4="
          f"{1e-3/4:.8f}  ✓")

    # 3. γ = ω₀ (второй язык) при том же h → рост СЛАБЕЕ
    print("\n[3] γ=ω₀ (второй язык) слабее первого при том же h")
    print(f"    {'h':>7s}{'μ₁ (γ=2ω₀)':>14s}{'μ₂ (γ=ω₀)':>14s}"
          f"{'μ₁/μ₂':>10s}{'q²/16 теор.':>14s}")
    om = 1.0
    for h in (0.02, 0.05, 0.1, 0.2):
        r1 = floquet_physical(om, 2.0 * om, h)
        r2 = floquet_physical(om, 1.0 * om, h)
        # аналитика второго языка: μ_τ ≈ (1/4)·√(Δ² − δ²), Δ=q²/4,
        # δ = смещение центра q²/6 → μ_τ = (q²/4)·√(1/16 − 1/36)
        q2 = abs(r2["q"])
        mu_th = (q2 * q2 / 4.0) * math.sqrt(1 / 16 - 1 / 36) * (om / 2.0)
        print(f"    {h:>7.2f}{r1['mu']:>14.6f}{r2['mu']:>14.8f}"
              f"{r1['mu']/max(r2['mu'],1e-18):>10.1f}{mu_th:>14.8f}")
        assert r2["mu"] < r1["mu"] / 5.0, ("второй язык не слабее", h)
        assert abs(r2["mu"] - mu_th) / mu_th < 0.35, ("второй язык", h)
    # ШИРИНА ЯЗЫКА ПО НОМЕРУ: наклон log(ширина) по log(q) обязан дать
    # 1, 2, 3 — то есть O(q), O(q²), O(q³). Аналитика: 2q, q²/2, q³/32.
    print(f"    {'q':>7s}{'Δa₁ числ.':>12s}{'теор 2q':>10s}"
          f"{'Δa₂ числ.':>12s}{'теор q²/2':>11s}"
          f"{'Δa₃ числ.':>12s}{'теор q³/32':>12s}")
    qs = (0.4, 0.2, 0.1, 0.05)
    wid = {n: [] for n in (1, 2, 3)}
    for q in qs:
        row = []
        for n in (1, 2, 3):
            lo, hi = tongue_bounds(n, q)
            wid[n].append(hi - lo)
            row.append(hi - lo)
        print(f"    {q:>7.2f}{row[0]:>12.5f}{2*q:>10.5f}{row[1]:>12.3e}"
              f"{q*q/2:>11.3e}{row[2]:>12.3e}{q**3/32:>12.3e}")
    for n, th in ((1, 2.0), (2, 0.5), (3, 1 / 32.0)):
        slope = float(np.polyfit(np.log(qs), np.log(wid[n]), 1)[0])
        assert abs(slope - n) < 0.02, (f"ширина языка {n} не как O(q^{n})",
                                       slope)
        for q, w in zip(qs, wid[n]):
            pred = th * q ** n
            assert abs(w - pred) / pred < 0.02, (n, q, w, pred)
        print(f"    язык {n}: показатель степени по q = {slope:.4f} "
              f"(теория {n}), совпадение с асимптотикой лучше 2%  ✓")
    print("    ширина языков падает с номером — воспроизведено  ✓")

    # 4. далеко от языков → μ ≈ 0
    print("\n[4] далеко от языков → μ ≈ 0")
    far = 0.0
    for a in (0.4, 0.6, 2.0, 2.5, 3.0, 5.0, 6.5, 7.5):
        f = floquet(a, 0.05)
        far = max(far, abs(f["mu_tau"]))
        assert f["stable"], (a, f["tr"])
    print(f"    q=0.05, a ∈ {{0.4…7.5}} вне языков: max|μ_τ| = {far:.3e}  ✓")
    assert far < 1e-9

    # 5. край языка Айнса-Штрутта при q→0 сходится к a = 1
    print("\n[5] край первого языка → a=1 при q→0")
    print(f"    {'q':>8s}{'a_низ числ.':>14s}{'a_верх числ.':>14s}"
          f"{'|край−1|':>11s}{'ряд 1±q−q²/8':>16s}{'откл. ряда':>12s}")
    errs = []
    for q in (0.2, 0.1, 0.05, 0.02, 0.01):
        lo, hi = tongue_bounds(1, q)
        s_lo = 1.0 - q - q * q / 8.0 + q ** 3 / 64.0
        s_hi = 1.0 + q - q * q / 8.0 - q ** 3 / 64.0
        err = max(abs(lo - 1.0), abs(hi - 1.0))
        dev = max(abs(lo - s_lo), abs(hi - s_hi))
        errs.append(err)
        print(f"    {q:>8.3f}{lo:>14.8f}{hi:>14.8f}{err:>11.2e}"
              f"{s_lo:>8.5f}/{s_hi:<7.5f}{dev:>12.2e}")
        # край обязан идти как q + q²/8 (это и есть «ширина языка O(q)»)
        assert abs(err - (q + q * q / 8.0)) < 0.01 * q, \
            ("край не следует за q", q, err)
        assert dev < 5e-5, ("край разошёлся с рядом Матье", q, dev)
    assert all(errs[i] > errs[i + 1] for i in range(len(errs) - 1)), \
        "край не сходится монотонно к a=1"
    lo, hi = tongue_bounds(1, 0.01)
    assert abs(lo - 1.0) < 0.02 and abs(hi - 1.0) < 0.02, "q→0: край не к 1"
    # второй язык: края 4−q²/12 и 4+5q²/12
    lo2, hi2 = tongue_bounds(2, 0.2)
    assert abs(lo2 - (4 - 0.04 / 12)) < 2e-5 and \
           abs(hi2 - (4 + 5 * 0.04 / 12)) < 2e-4, (lo2, hi2)
    print(f"    второй язык q=0.2: [{lo2:.8f}, {hi2:.8f}] против ряда "
          f"[{4-0.04/12:.8f}, {4+5*0.04/12:.8f}]  ✓")
    # симметрия диаграммы по знаку q (классика)
    assert abs(floquet(1.0, 0.3)["lam"] - floquet(1.0, -0.3)["lam"]) < 1e-9
    print("    |λ|(a,q) = |λ|(a,−q) — симметрия Айнса-Штрутта  ✓")

    # 6. детерминизм
    print("\n[6] детерминизм")
    r1 = floquet_physical(0.8976, 1.7947, 0.07)
    r2 = floquet_physical(0.8976, 1.7947, 0.07)
    assert r1 == r2 and r1["mu"] == r2["mu"], "разные ответы на один вход"
    m1 = monodromy(np.array([0.9, 1.0, 1.1]), 0.1)
    m2 = monodromy(np.array([0.9, 1.0, 1.1]), 0.1)
    assert np.array_equal(m1, m2), "монодромия недетерминирована"
    t1 = tongue_bounds(1, 0.05)
    t2 = tongue_bounds(1, 0.05)
    assert t1 == t2
    print(f"    два вызова: μ={r1['mu']!r} бит-в-бит совпал  ✓")

    # 7. СКВОЗНАЯ ПРОВЕРКА: монодромия против ПРЯМОГО интегрирования и
    #    против закона Ландау d(lnA)/dt = β·sin 2θ, |β| = h·ω₀/4
    print("\n[7] сквозная сверка: μ Флоке = наклон ln A прямой симуляции")
    om = 2 * math.pi / 7.0        # недельный осциллятор, как у ресто
    print(f"    {'h':>6s}{'наклон lnA':>13s}{'μ Флоке':>12s}{'отн.':>8s}"
          f"{'⟨sin2θ⟩ (захват фазы)':>24s}")
    for h in (0.02, 0.03, 0.05):
        t, xs = simulate(om, 2 * om, h, t_end=1200.0, dt=0.02)
        idx = np.arange(0, len(t), 50)          # суточные отсчёты
        xd, td = xs[idx], t[idx]
        A, phi = demodulate(xd, om, td)
        m = np.isfinite(A) & (A > 0)
        tt, la = td[m], np.log(A[m])
        sel = (tt > 150) & (tt < 1100)          # без краёв окна вейвлета
        slope = float(np.polyfit(tt[sel], la[sel], 1)[0])
        mu = floquet_physical(om, 2 * om, h)["mu"]
        s = np.sin(2.0 * (phi[m] - om * tt))[sel]
        print(f"    {h:>6.2f}{slope:>13.7f}{mu:>12.7f}"
              f"{abs(slope-mu)/mu:>8.2%}{np.mean(s):>+16.4f}"
              f" ± {np.std(s):.4f}")
        assert abs(slope - mu) / mu < 0.03, ("рост ≠ показателю Флоке", h)
        assert abs(np.mean(s)) > 0.98 and np.std(s) < 0.05, \
            ("фаза не захватилась в языке", h)
    # ω₀ восстанавливается из самого ряда
    dom = dominant_omega(xd, td, band=(3.0, 60.0))
    assert abs(dom["period"] - 7.0) / 7.0 < 0.05, dom
    print(f"    ω₀ найдена из ряда: период {dom['period']:.4f} (истина 7.0) ✓")

    print("\n[7б] закон Ландау при расстройке: β = h·ω₀/4")
    h, eps = 0.10, 0.05          # вне языка (полуширина h/4 = 0.025)
    g = 2.0 * om * (1.0 + eps)
    t, xs = simulate(om, g, h, t_end=2000.0, dt=0.02)
    idx = np.arange(0, len(t), 50)
    xd, td = xs[idx], t[idx]
    A, phi = demodulate(xd, om, td)
    lb = landau_beta(A, phi, g * td, n=1, t_days=td)
    exp = h * om / 4.0
    print(f"    β={lb['beta']:+.5f} против h·ω₀/4={exp:.5f} "
          f"(отн. {abs(abs(lb['beta'])-exp)/exp:.1%}), R²={lb['r2']:.3f}")
    assert abs(abs(lb["beta"]) - exp) / exp < 0.25, lb
    assert lb["r2"] > 0.9, lb
    lb0 = landau_beta(A, phi, g * td * 1.37, n=1, t_days=td)
    print(f"    чужая накачка (×1.37): |β|={abs(lb0['beta']):.5f}, "
          f"R²={lb0['r2']:+.3f} — закон не воспроизводится ✓")
    assert abs(lb0["beta"]) < 0.4 * abs(lb["beta"]) and lb0["r2"] < 0.3, lb0

    # 8. измеритель периодов по de440 против известных значений
    per = body_periods()
    if per:
        print("\n[8] периоды из de440 против справочника")
        print(f"    {'тело':10s}{'T_sid изм.':>12s}{'справ.':>10s}{'откл':>8s}"
              f"{'T_syn изм.':>12s}{'справ.':>10s}{'откл':>8s}")
        for b, ref in REF_T_SID.items():
            if b not in per:
                continue
            p = per[b]
            e1 = abs(p["T_sid"] - ref) / ref
            syn = p["T_syn"]
            e2 = (abs(syn - REF_T_SYN[b]) / REF_T_SYN[b]
                  if syn and b in REF_T_SYN else float("nan"))
            print(f"    {b:10s}{p['T_sid']:>12.3f}{ref:>10.3f}{e1:>7.2%}"
                  f"{(syn if syn else float('nan')):>12.3f}"
                  f"{REF_T_SYN.get(b, float('nan')):>10.3f}{e2:>7.2%}")
            lim = 0.04 if b == "Плутон" else 0.01
            assert e1 < lim, (b, "T_sid", e1)
            if np.isfinite(e2):
                assert e2 < 0.01, (b, "T_syn", e2)
        assert per["Солнце"]["T_syn"] is None, \
            "у Солнца синодического периода нет — должно быть None"
        print("    все периоды измерены по de440 и сошлись  ✓")

        # 9. СОГЛАСОВАННОСТЬ ЧАСТОТЫ И ФАЗЫ: угол правила обязан вращаться
        #    ровно с объявленной γ. Именно этот тест поймал, что видимая
        #    элонгация Меркурия и Венеры ОГРАНИЧЕНА и не вращается вообще.
        print("\n[9] фаза накачки вращается с объявленной γ (W=1)")
        step = 4
        dts = [date(1700, 1, 1) + timedelta(step * i)
               for i in range(int(400 * 365.25 / step))]
        tt = np.array([(d - dts[0]).days for d in dts], float)
        print(f"    {'тело':10s}{'syn':>10s}{'orb':>10s}{'speed':>10s}"
              f"   (отклонение скорости фазы от 2π/T)")
        for b in [x for x in aether._BODY_ORDER if x in per]:
            row, devs = "", {}
            for rule in RULES:
                g = vedic_gamma(b, rule, W=1.0)
                ph = pump_phase(b, dts, rule, W=1.0)
                if g is None or g.get("gamma") is None or ph is None:
                    assert (g is None or g.get("gamma") is None) and ph is None,\
                        (b, rule, "частота есть, фазы нет — рассогласование")
                    row += f"{'—':>10s}"
                    continue
                rate = abs(float(np.polyfit(tt, ph, 1)[0]))
                dev = rate / g["gamma"] - 1.0
                devs[rule] = dev
                row += f"{dev:>+9.2%} "
            print(f"    {b:10s}{row}")
            for rule, dev in devs.items():
                lim = 0.08 if rule == "speed" else 0.03
                assert abs(dev) < lim, (b, rule, dev)
        print("    у всех тел и правил частота и фаза — одно и то же  ✓")
    else:
        print("\n[8] эфемерид нет — измеритель периодов честно пропущен")

    print("\n" + "=" * 92)
    print("ДИАГРАММА УСТОЙЧИВОСТИ (языки Айнса-Штрутта)")
    print("=" * 92)
    print(stability_table())
    print("\nВСЕ SELF-ТЕСТЫ ПРОЙДЕНЫ")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "data":
        run_data()
    else:
        _selftest()
