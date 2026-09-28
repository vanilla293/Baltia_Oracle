# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — ХОУКС (критичность потока сделок, Филимонов–Сорнетт).

Приказ владельца (спринт «Хищник», №2): «бот должен видеть, где маркетмейкер
абсорбирует удары (токсичный поток)».

Самовозбуждающийся процесс Хоукса: каждая сделка повышает интенсивность
будущих сделок. Ключевое число — ВЕТВЛЕНИЕ n (branching ratio): среднее число
«дочерних» сделок, порождённых одной. Филимонов и Сорнетт (2012, «Quantifying
reflexivity») показали: n — мера РЕФЛЕКСИВНОСТИ рынка; n→1 — критичность:
поток питает сам себя, лавина близко (та же математика, что докритический
ядерный реактор).

Оценка БЕЗ подгонки ядра — через фактор Фано бинированных счётов 🔵:
для кластерного процесса Хоукса на больших бинах
    F = Var(N)/E(N) → 1/(1−n)²   ⇒   n̂ = 1 − 1/√F  (F≥1; F<1 → n̂=0).
Пуассоновский (некластерный) поток даёт F=1 → n̂=0. Метод устойчив к форме
ядра — именно поэтому он честнее EM-подгонки на 1.5-секундных агрегатах.

🟡 прокси названы: счёты у нас — ПРИРОСТЫ скользящего окна ленты Tinkoff
(не сырые тики), бин = тик петли; интерпретация «токсичности» — язык школы.
⚫ n — термометр критичности среды, НЕ приказ. Рынок не предсказуем. 18+.

Self-тест: python3 -m backend.hawkes
"""
from __future__ import annotations

import math

# ── пороги (единственное место) ─────────────────────────────────────────────
N_WARM = 0.80        # ветвление выше — рефлексивность разогрета
N_CRITICAL = 0.95    # выше — КРИТИЧНОСТЬ: лавина питает сама себя
MIN_BINS = 40        # меньше бинов — честный None (NO DUMMIES)
MIN_EVENTS = 60      # меньше сырых событий — EM не судит (NO DUMMIES)
EM_ITERS = 30        # фиксированные итерации EM — детерминизм байт-в-байт
EM_LOOKBACK = 200    # предшественников на событие (обрез хвоста ядра)
MAX_EVENTS = 3000    # хвост событий в EM (O(N·K) в узде)

FRAME = ("⚫ ветвление Хоукса — термометр критичности потока, НЕ приказ. "
         "Оценка по фактору Фано 🔵 на приростах окна ленты (прокси 🟡). 18+")


def _f(x, d=0.0):
    try:
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


def branching(counts) -> dict | None:
    """Ветвление n̂ по фактору Фано счётов сделок в бинах.

    counts — список чисел сделок за бин (приросты, не кумулятив).
    Возврат: n, fano, word; None при нехватке данных."""
    xs = [max(0.0, _f(c)) for c in (counts or [])]
    if len(xs) < MIN_BINS:
        return None
    mean = sum(xs) / len(xs)
    if mean <= 0:
        return None
    var = sum((x - mean) ** 2 for x in xs) / len(xs)
    fano = var / mean
    n = max(0.0, 1.0 - 1.0 / math.sqrt(fano)) if fano > 1.0 else 0.0
    n = min(0.999, n)
    if n >= N_CRITICAL:
        word = "КРИТИЧНОСТЬ: поток питает сам себя — лавина рядом"
    elif n >= N_WARM:
        word = "рефлексивность разогрета: сделки рожают сделки"
    else:
        word = "поток докритичен: сделки в основном внешние"
    return {"n": round(n, 3), "fano": round(fano, 2), "bins": len(xs),
            "word": word, "frame": FRAME}


def branching_times(times_ms, iters: int = EM_ITERS) -> dict | None:
    """ВЕТВЛЕНИЕ ПО СЫРЫМ ТАЙМСТАМПАМ (удар №1: математика по миллисекундам).

    EM-оценка процесса Хоукса с экспоненциальным ядром
        λ(t) = μ + n·β·Σ_{t_j<t} e^{−β(t−t_j)}
    E-шаг: вероятность, что событие i — фоновое (иммигрант) либо дитя j;
    M-шаг: μ, n, β из ожиданий. Инициализация фиксирована, итераций ровно
    EM_ITERS — детерминизм байт-в-байт. Это оценка Филимонова–Сорнетта
    «сколько сделок рождено сделками» на настоящих событиях, без бинов 🔵."""
    ts = sorted(float(t) for t in (times_ms or []) if t is not None)
    if len(ts) > MAX_EVENTS:
        ts = ts[-MAX_EVENTS:]
    n_ev = len(ts)
    if n_ev < MIN_EVENTS:
        return None
    t0, t1 = ts[0], ts[-1]
    span = t1 - t0
    if span <= 0:
        return None
    gaps = [b - a for a, b in zip(ts[:-1], ts[1:]) if b > a]
    if not gaps:
        return None
    mean_gap = sum(gaps) / len(gaps)
    mu = 0.5 * n_ev / span                      # фон: половина интенсивности
    n = 0.5                                     # старт ветвления
    beta = 1.0 / mean_gap                       # ядро ~ средний межтик
    for _ in range(max(1, int(iters))):
        sum_bg = 0.0
        sum_tr = 0.0
        sum_tr_dt = 0.0
        for i in range(n_ev):
            ti = ts[i]
            lam_tr = 0.0
            contribs = []
            j0 = max(0, i - EM_LOOKBACK)
            for j in range(j0, i):
                dt = ti - ts[j]
                w = n * beta * math.exp(-beta * dt)
                if w > 1e-15:
                    contribs.append((w, dt))
                    lam_tr += w
            lam = mu + lam_tr
            if lam <= 0:
                continue
            sum_bg += mu / lam
            for w, dt in contribs:
                p = w / lam
                sum_tr += p
                sum_tr_dt += p * dt
        mu = max(1e-12, sum_bg / span)
        n = min(0.999, max(0.0, sum_tr / n_ev))
        if sum_tr > 0 and sum_tr_dt > 0:
            beta = max(1e-9, sum_tr / sum_tr_dt)
    if n >= N_CRITICAL:
        word = "КРИТИЧНОСТЬ: поток питает сам себя — лавина рядом"
    elif n >= N_WARM:
        word = "рефлексивность разогрета: сделки рожают сделки"
    else:
        word = "поток докритичен: сделки в основном внешние"
    return {"n": round(n, 3), "mu_per_s": round(mu * 1000.0, 3),
            "beta_per_s": round(beta * 1000.0, 4),
            "half_life_ms": round(math.log(2.0) / beta, 1),
            "events": n_ev, "span_s": round(span / 1000.0, 1),
            "word": word, "method": "EM по сырым таймстампам 🔵",
            "frame": FRAME}


def branching_best(times_ms=None, counts=None) -> dict | None:
    """Лучшее доступное: сырые таймстампы (поток жив) → EM; иначе честный
    Фано-фолбэк на бинах (REST). Метод виден в ответе — деградация не молчит."""
    r = branching_times(times_ms) if times_ms else None
    if r is not None:
        return r
    r = branching(counts) if counts else None
    if r is not None:
        r = dict(r)
        r["method"] = "Фано по бинам (REST-фолбэк 🟡 — поток мёртв)"
    return r


def toxicity(hawkes_n: float | None, vpin: float | None,
             absorbing: bool | None) -> dict:
    """Токсичность потока = критичность × односторонность × абсорбция.

    Сюжет владельца: ММ впитывает удары айсбергом. Признак: поток критичен
    (n высок), односторонен (VPIN высок), а цена стоит (absorption). Это
    место, где «толпа бьёт — стена ест», и после release цена летит."""
    n = _f(hawkes_n)
    vp = _f(vpin)
    score = 0.0
    parts = []
    if n >= N_WARM:
        score += 0.4 * min(1.0, (n - N_WARM) / (1.0 - N_WARM))
        parts.append(f"ветвление {n:.2f}")
    if vp >= 0.4:
        score += 0.3 * min(1.0, (vp - 0.4) / 0.6)
        parts.append(f"VPIN {vp:.2f}")
    if absorbing:
        score += 0.3
        parts.append("абсорбция")
    toxic = bool(score >= 0.5)
    return {"score": round(score, 3), "toxic": toxic,
            "word": ("ТОКСИЧНЫЙ ПОТОК: " + " + ".join(parts) +
                     " — ММ впитывает удары, release будет резким"
                     if toxic else "поток обычный"),
            "parts": parts}


# ── self-test: детерминированные потоки, без random ─────────────────────────
if __name__ == "__main__":
    # 1) равномерный поток (Var=0 → F=0) → n=0, докритично
    uni = branching([4] * 100)
    assert uni and uni["n"] == 0.0 and "докритичен" in uni["word"], uni

    # 2) пуассоно-подобный (лёгкая детерминированная рябь, F≈1) → n≈0
    ripple = [4 + (1 if i % 7 == 0 else 0) for i in range(140)]
    rp = branching(ripple)
    assert rp and rp["n"] < 0.3, rp

    # 3) кластерный (лавины: длинные нули и залпы) → n высокое
    burst = ([0] * 9 + [40]) * 12                    # среднее 4, Var огромна
    bu = branching(burst)
    assert bu and bu["n"] > N_WARM, bu
    assert bu["fano"] > 10

    # 4) NO DUMMIES: мало бинов → None
    assert branching([1, 2, 3]) is None
    assert branching([0] * 100) is None              # мёртвый поток — не судим

    # 5) монотонность: сильнее кластеризация → выше n
    mild = ([2] * 4 + [12]) * 24                     # кластеры слабее
    mi = branching(mild)
    assert mi and mi["n"] < bu["n"], (mi, bu)

    # 6) токсичность: критичность + VPIN + абсорбция → toxic
    t = toxicity(0.97, 0.7, True)
    assert t["toxic"] and "ТОКСИЧНЫЙ" in t["word"], t
    assert toxicity(0.3, 0.1, False)["toxic"] is False
    #    одна абсорбция без критики и VPIN — ещё не токсичность
    assert toxicity(None, None, True)["toxic"] is False

    # 7) детерминизм байт-в-байт
    assert branching(burst) == bu

    # ── EM по СЫРЫМ ТАЙМСТАМПАМ (удар №1) ──
    # 8) регулярная решётка (нет кластеров) → n низкое
    reg = [i * 250.0 for i in range(240)]                # тик каждые 250мс
    em_reg = branching_times(reg)
    assert em_reg and em_reg["n"] < 0.35, em_reg
    # 9) детерминированный каскад: иммигрант каждые 1200мс, у каждого двое
    #    детей (+40мс, +110мс) и внук (+40+45мс) → 3/4 событий — потомки
    casc = []
    for k in range(80):
        base = k * 1200.0
        casc += [base, base + 40.0, base + 85.0, base + 110.0]
    em_c = branching_times(casc)
    assert em_c and em_c["n"] > em_reg["n"] + 0.25, (em_c, em_reg)
    assert em_c["n"] >= 0.5, em_c                        # потомков большинство
    assert em_c["half_life_ms"] < 600.0                  # ядро короткое, не бины
    # 10) NO DUMMIES: мало событий / нулевой размах → None
    assert branching_times([1.0, 2.0, 3.0]) is None
    assert branching_times([5.0] * 100) is None
    # 11) детерминизм EM байт-в-байт
    assert branching_times(casc) == em_c
    # 12) branching_best: сырые времена главнее бинов; метод виден
    bb = branching_best(times_ms=casc, counts=[4] * 100)
    assert bb and "EM" in bb["method"]
    bb2 = branching_best(times_ms=None, counts=burst)
    assert bb2 and "Фано" in bb2["method"] and "фолбэк" in bb2["method"]
    assert branching_best(None, None) is None

    print("hawkes self-test OK: Фано на бинах (фолбэк) + EM по сырым "
          "таймстампам (решётка→докритично, каскад→потомки видны, ядро в "
          "миллисекундах), выбор метода честен, NO DUMMIES, детерминизм")
