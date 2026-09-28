"""МАТЬЕ · СКОЛЬЗЯЩИЙ СКАНЕР ЯЗЫКОВ — РЕШАЮЩАЯ ПРОВЕРКА ВЕДИЧЕСКИХ ЧИСЕЛ.

Как МНОЖИТЕЛИ 108/81/72/54/27 уже мертвы (перестановка весов, p=0.51).
Директива владельца: они не множители, а ЧАСТОТЫ НАКАЧКИ. Здесь это
проверяется до конца — и в строгой форме, и в самой щедрой, какую
допускает честность.

🔵 МЕХАНИКА (проверяемо независимо от школы):
  · ω₀(t) — собственная частота отклика, скользящим окном. Оценка:
    детренд(2) + окно Ханна + дополнение нулями ×8 + параболическое
    уточнение пика. Для ресторана недельный цикл очевиден, поэтому
    считаются ДВА трека: (1) сам недельный пик и (2) второй пик после
    ВЫЧИТАНИЯ недельной гребёнки 7 / 3.5 / 2.333 сут методом наименьших
    квадратов.
  · γ_i(t) — МГНОВЕННАЯ частота накачки тела: γ_i = W_i·|dθ_syn/dt|,
    производная реальной фазы неба. Именно мгновенная: синодический
    интервал дрожит (σ/T от 0.22% у Урана до 8.4% у Меркурия), и это
    дрожание — физика, а не шум измерения. Именно оно и двигает систему
    относительно языка, потому что ω₀ ресторана прибит к календарю.
  · a = 4ω₀²/γ², √a = 2ω₀/γ. Расстояние до ближайшего языка Матье —
    dist = |2ω₀/γ − n|, n = ближайшее целое ≥ 1. Показатель роста μ по
    Флоке — честной монодромией из mathieu.py, без приближений.
  · h — ИЗМЕРЕННАЯ глубина модуляции эфирной плотности E_i(t) ровно на
    частоте накачки в этом же окне (формула mathieu.pump_depth).

🟡 ЯЗЫК ШКОЛЫ (прокси назван прямо):
  Перевод ведического числа W в частоту накачки — ПРОКСИ. Правило
  зафиксировано в mathieu.RULE_PRIMARY = «syn» (синодическое) ДО прогона
  данных и здесь не пересматривается.

⚫ НЕ СИГНАЛ: ни одна цифра отсюда не является торговым или
  управленческим указанием. Резонанс — модель, а не судьба.

ГИПОТЕЗА (записана до счёта): в окнах, где система близка к языку,
отклик обязан вести себя ИНАЧЕ — сильнее раскачка, выше волатильность
заказов/цены, чаще экстремумы.

ТРИ НУЛЯ:
  (а) те же ведические числа, ПЕРЕТАСОВАННЫЕ между телами — ВСЕ 15120
      различных перестановок, исчерпывающе, без единого random;
  (б) произвольные целые того же порядка — сплошной перебор W = 1..200
      по каждому телу плюс 1000 случайных наборов;
  (в) IAAFT-суррогат самого отклика (тот же спектр и то же
      распределение) — 200 реализаций.

Прогон:  python3 -m backend.lab.mathieu_scan          # self-тесты (assert)
         python3 -m backend.lab.mathieu_scan data     # прогон на данных
"""
import itertools
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone

import numpy as np

from backend.lab import mathieu as MT

try:
    from backend import aether
except Exception:                                      # pragma: no cover
    aether = None
try:
    from backend.lab.e_surrogate import iaaft
except Exception:                                      # pragma: no cover
    iaaft = None

MOEX_HIST = MT.MOEX_HIST
STORES = MT.STORES

# ── параметры сканера (зафиксированы до прогона на данных) ────────────
WIN_RESTO = 182          # окно ресто, сут (полгода = ровно 26 недель)
STEP_RESTO = 7           # шаг окна, сут (кратен неделе: фаза календаря та же)
WIN_MOEX = 365           # окно биржи, сут (год)
STEP_MOEX = 14           # шаг окна биржи, сут
BAND_WEEK = (5.0, 10.0)          # полоса поиска недельного пика
BAND_RESTO2 = (2.5, 91.0)        # полоса второго пика ресто
BAND_VOL = (20.0, 180.0)         # полоса огибающей волатильности биржи
WEEK_HARM = (7.0, 3.5, 7.0 / 3.0)  # недельная гребёнка, которую вычитаем
BUCKET_FRAC = 1.0 / 3.0          # корзины: нижняя и верхняя треть по dist
PAD = 8                          # дополнение нулями периодограммы
MIN_WIN = 30                     # минимум окон, иначе корзины не считаем
LAGS = tuple(range(-270, 271, 15))   # сут; + = небо РАНЬШЕ отклика
NULL_B_DRAWS = 1000              # наборов «произвольных целых»
NULL_C_DRAWS = 200               # IAAFT-суррогатов
W_ORDER = (10, 130)              # «тот же порядок»: ведические 27…108
NULL_SEED = 20260801             # нуль-стенд, не продукт


# ══════════════════════════════════════════════════════════════════════
# 1. СОБСТВЕННАЯ ЧАСТОТА ОТКЛИКА СКОЛЬЗЯЩИМ ОКНОМ  🔵
# ══════════════════════════════════════════════════════════════════════
def _hann(n):
    return 0.5 - 0.5 * np.cos(2.0 * math.pi * np.arange(n) / n)


def periodogram(x, dt: float = 1.0, pad: int = PAD):
    """Периодограмма равномерного ряда: детренд(2) + Ханн + нули ×pad.

    Дополнение нулями не добавляет информации — оно уплотняет сетку,
    чтобы пик не приходилось искать между узлами; разрешающая способность
    остаётся 1/(N·dt).
    """
    x = np.asarray(x, float)
    n = len(x)
    t = np.arange(n, dtype=float)
    x = x - np.polyval(np.polyfit(t, x, 2), t)
    x = x * _hann(n)
    N = 1
    while N < pad * n:
        N *= 2
    X = np.fft.rfft(x, N)
    P = (np.abs(X) ** 2)[1:]
    f = np.fft.rfftfreq(N, dt)[1:]
    return 1.0 / f, P


def dominant_period(x, band, dt: float = 1.0, pad: int = PAD):
    """Доминирующий период в полосе + параболическое уточнение пика.

    Уточнение — по log-мощности трёх узлов вокруг максимума в координате
    ЧАСТОТЫ (там пик симметричен; в периоде — нет).
    """
    per, P = periodogram(x, dt, pad)
    sel = (per >= band[0]) & (per <= band[1])
    if sel.sum() < 3:
        return None
    idx = np.where(sel)[0]
    j = idx[int(np.argmax(P[idx]))]
    if j <= 0 or j >= len(P) - 1 or min(P[j - 1], P[j], P[j + 1]) <= 0:
        return {"period": float(per[j]), "power": float(P[j])}
    y0, y1, y2 = np.log(P[j - 1]), np.log(P[j]), np.log(P[j + 1])
    den = y0 - 2.0 * y1 + y2
    sh = 0.0 if den == 0 else 0.5 * (y0 - y2) / den
    sh = float(np.clip(sh, -1.0, 1.0))
    f0 = 1.0 / per[j]
    df = 1.0 / per[j + 1] - f0
    return {"period": float(1.0 / (f0 + sh * df)), "power": float(P[j])}


def strip_harmonics(x, periods=WEEK_HARM, dt: float = 1.0):
    """Вычесть МНК-подгонку синусов/косинусов заданных периодов + тренд.

    Это и есть «недельный вычесть»: календарная гребёнка снимается
    целиком, вместе с обертонами 3.5 и 2.333 сут — они принадлежат той же
    жёсткой семидневке, а не отдельному осциллятору.
    """
    x = np.asarray(x, float)
    n = len(x)
    t = np.arange(n, dtype=float) * dt
    cols = [np.ones(n), t, t * t]
    for p in periods:
        cols.append(np.cos(2.0 * math.pi * t / p))
        cols.append(np.sin(2.0 * math.pi * t / p))
    A = np.column_stack(cols)
    m = np.isfinite(x)
    if m.sum() < A.shape[1] + 5:
        return x - np.nanmean(x)
    beta = np.linalg.lstsq(A[m], x[m], rcond=None)[0]
    return x - A @ beta


def omega_track(x, win: int, step: int, band, drop_week: bool = False,
                max_nan: float = 0.15) -> dict:
    """ω₀(t) скользящим окном; центр окна — индекс середины.

    Пропуски внутри окна закрываются линейной интерполяцией, но окно, где
    их больше max_nan, выбрасывается целиком (нет данных → нет окна, а не
    выдуманный ряд).
    """
    x = np.asarray(x, float)
    n = len(x)
    cen, om, per, pw = [], [], [], []
    for s in range(0, n - win + 1, step):
        w = x[s:s + win]
        m = np.isfinite(w)
        if m.mean() < 1.0 - max_nan or m.sum() < win // 2:
            continue
        if not m.all():
            w = np.interp(np.arange(win), np.where(m)[0], w[m])
        if drop_week:
            w = strip_harmonics(w)
        d = dominant_period(w, band)
        if d is None or not np.isfinite(d["period"]) or d["period"] <= 0:
            continue
        cen.append(s + win // 2)
        per.append(d["period"])
        om.append(2.0 * math.pi / d["period"])
        pw.append(d["power"])
    return {"centers": np.array(cen, int), "omega0": np.array(om, float),
            "period": np.array(per, float), "power": np.array(pw, float),
            "win": win, "n": n}


# ══════════════════════════════════════════════════════════════════════
# 2. НЕБО: МГНОВЕННАЯ ЧАСТОТА НАКАЧКИ  🟡→🔵
# ══════════════════════════════════════════════════════════════════════
_SKY: dict = {}


def sky_pump(dates) -> dict | None:
    """Фаза накачки при W=1 и эфирная плотность E(t) для всех тел.

    φ_W(t) = W·φ₁(t) — линейность по W позволяет обратиться к эфемеридам
    ОДИН раз, а затем перебирать любые W (в том числе 15120 перестановок
    нуля) без единого нового вызова skyfield. Нет эфемерид → None.
    """
    if aether is None:
        return None
    key = (dates[0].isoformat(), dates[-1].isoformat(), len(dates))
    if key in _SKY:
        return _SKY[key]
    per = MT.body_periods()
    if per is None:
        return None
    t0 = datetime(dates[0].year, dates[0].month, dates[0].day, 12,
                  tzinfo=timezone.utc)
    hours = (dates[-1] - dates[0]).days * 24.0
    tr = aether.transit_series(t0, hours, 24.0)
    if tr is None:
        return None
    idx = {datetime.fromtimestamp(v, timezone.utc).date(): i
           for i, v in enumerate(np.asarray(tr["ts"], float))}
    sel = np.array([idx.get(d, -1) for d in dates])
    if (sel < 0).any():
        return None
    phi1, Emat, dphi = {}, {}, {}
    for b in aether._BODY_ORDER:
        if per.get(b, {}).get("T_syn") is None:
            continue                       # у Солнца синодического угла нет
        ph = MT.pump_phase(b, dates, MT.RULE_PRIMARY, W=1.0)
        if ph is None:
            continue
        phi1[b] = ph
        dphi[b] = np.abs(np.gradient(ph, 1.0))
        Emat[b] = np.asarray(tr["E"][b], float)[sel]
    out = {"phi1": phi1, "dphi1": dphi, "E": Emat, "dates": dates,
           "run": {}}
    _SKY[key] = out
    return out


def gamma_running(sky: dict, body: str, win: int) -> np.ndarray:
    """γ̄(c)/W — средняя мгновенная накачка в окне с центром c, по всем c.

    Считается один раз кумулятивной суммой: дальше любое W и любой сдвиг
    берутся лукапом. Где окно не помещается — NaN, а не край-эффект.
    """
    key = (body, win)
    if key in sky["run"]:
        return sky["run"][key]
    d1 = sky["dphi1"][body]
    n = len(d1)
    cs = np.concatenate([[0.0], np.cumsum(d1)])
    out = np.full(n, np.nan)
    half = win // 2
    lo = np.arange(n) - half
    ok = (lo >= 0) & (lo + win <= n)
    out[ok] = (cs[lo[ok] + win] - cs[lo[ok]]) / win
    sky["run"][key] = out
    return out


def h_at(sky: dict, body: str, W: float, centers, win: int) -> np.ndarray:
    """h(окно) — измеренная глубина модуляции E ровно на частоте накачки:
    h = 2·|⟨(E/Ē − 1)·e^{i·W·φ₁}·Ханн⟩|. Нужна для μ, не для корзин."""
    p1 = sky["phi1"][body]
    E = sky["E"][body]
    half = win // 2
    wn = _hann(win)
    wn = wn / wn.mean()
    out = np.full(len(centers), np.nan)
    for k, c in enumerate(centers):
        lo = c - half
        if lo < 0 or lo + win > len(E):
            continue
        e = E[lo:lo + win]
        eb = float(np.mean(e))
        if eb <= 0:
            continue
        z = np.mean((e / eb - 1.0) * np.exp(1j * p1[lo:lo + win] * W) * wn)
        out[k] = float(2.0 * abs(z))
    return out


def tongue_track(omega0, gamma, h=None, with_mu: bool = False) -> dict:
    """Расстояние до ближайшего языка и (по требованию) показатель Флоке.

    dist = |2ω₀/γ − n|, n = ближайшее целое ≥ 1. Полуширина ПЕРВОГО языка
    в этих же единицах равна h/4 (следствие q = −a·h/2 и Δa₁ = 2|q|).
    """
    om = np.asarray(omega0, float)
    ga = np.asarray(gamma, float)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = 2.0 * om / ga
    n = np.maximum(1, np.rint(np.where(np.isfinite(r), r, 1.0))).astype(int)
    dist = np.abs(r - n)
    out = {"ratio": r, "n": n, "dist": dist, "a": r * r}
    if with_mu and h is not None:
        mu = np.full(len(r), np.nan)
        unst = np.zeros(len(r), bool)
        for k in range(len(r)):
            if not (np.isfinite(h[k]) and np.isfinite(ga[k]) and ga[k] > 0):
                continue
            fl = MT.floquet_physical(float(om[k]), float(ga[k]),
                                     float(h[k]))
            mu[k] = fl["mu"]
            # ВАЖНО: в полосе устойчивости система гамильтонова, |λ|=1
            # ТОЧНО, и μ там — численный шум ~1e−15/T, то есть формально
            # положительный. Считать «окно в языке» по знаку μ нельзя;
            # честный критерий — |tr M| > 2 из ядра.
            unst[k] = not fl["stable"]
        out["mu"] = mu
        out["unstable"] = unst
        out["half_width"] = np.asarray(h, float) / 4.0
    return out


# ══════════════════════════════════════════════════════════════════════
# 3. МЕТРИКИ ОТКЛИКА В ОКНЕ  🔵
# ══════════════════════════════════════════════════════════════════════
def rel_series(x, kind: str = "rel", k: int = 28) -> np.ndarray:
    """Отклик, очищенный от уровня: заказы → x/скользящее среднее − 1,
    цена → Δln(цена). Без заглушек: где базы нет, там NaN."""
    x = np.asarray(x, float)
    if kind == "ret":
        lx = np.log(np.where(x > 0, x, np.nan))
        return np.concatenate([[np.nan], np.diff(lx)])
    n = len(x)
    fin = np.isfinite(x)
    xz = np.where(fin, x, 0.0)
    cs = np.concatenate([[0.0], np.cumsum(xz)])
    cn = np.concatenate([[0.0], np.cumsum(fin.astype(float))])
    lo = np.maximum(0, np.arange(n) - k // 2)
    hi = np.minimum(n, np.arange(n) + k // 2 + 1)
    cnt = cn[hi] - cn[lo]
    base = np.where(cnt >= k // 2, (cs[hi] - cs[lo]) / np.maximum(cnt, 1), np.nan)
    return np.where(fin & (base > 0), x / base - 1.0, np.nan)


def resp_metrics(r, centers, win: int) -> dict:
    """Как ведёт себя отклик в окне (r — уже очищенный ряд).

      вола  — std(r)                    «выше волатильность»
      разм  — p95(r) − p5(r)            «шире размах»
      экстр — доля |r| > 2.5·σ_ряда     «чаще экстремумы»
      амп   — √(2·⟨r²⟩)                 «сильнее раскачка»
    """
    r = np.asarray(r, float)
    fin = r[np.isfinite(r)]
    sig = float(np.std(fin)) if len(fin) > 10 else np.nan
    half = win // 2
    out = {kk: np.full(len(centers), np.nan)
           for kk in ("вола", "разм", "экстр", "амп")}
    for i, c in enumerate(centers):
        lo, hi = max(0, c - half), min(len(r), c - half + win)
        w = r[lo:hi]
        m = np.isfinite(w)
        if m.sum() < win // 2:
            continue
        w = w[m]
        out["вола"][i] = float(np.std(w))
        out["разм"][i] = float(np.quantile(w, 0.95) - np.quantile(w, 0.05))
        out["экстр"][i] = float(np.mean(np.abs(w) > 2.5 * sig))
        out["амп"][i] = float(np.sqrt(2.0 * np.mean(w * w)))
    return out


# ══════════════════════════════════════════════════════════════════════
# 4. КОРЗИНЫ: «БЛИЗКО К ЯЗЫКУ» ПРОТИВ «ДАЛЕКО»  🔵
# ══════════════════════════════════════════════════════════════════════
def bucket_effect(dist, metric, frac: float = BUCKET_FRAC):
    """Эффект = (близко − далеко)/σ по метрике отклика.

    Корзины — нижняя и верхняя треть окон ПО РАССТОЯНИЮ до языка. Знак:
    гипотеза требует d > 0.
    ВНИМАНИЕ 🔵: окна перекрываются (шаг много меньше окна), поэтому
    обычный p-уровень здесь заведомо занижен и НЕ приводится. Единственный
    законный судья — нули ниже: они сохраняют то же перекрытие полностью.
    """
    dist = np.asarray(dist, float)
    metric = np.asarray(metric, float)
    m = np.isfinite(dist) & np.isfinite(metric)
    if m.sum() < MIN_WIN:
        return None
    d, v = dist[m], metric[m]
    sd = float(np.std(v))
    if sd <= 0:
        return None
    lo = float(np.quantile(d, frac))
    hi = float(np.quantile(d, 1.0 - frac))
    near, far = d <= lo, d >= hi
    if near.sum() < 8 or far.sum() < 8:
        return None
    return {"d": float((v[near].mean() - v[far].mean()) / sd),
            "n_near": int(near.sum()), "n_far": int(far.sum()),
            "near_mean": float(v[near].mean()),
            "far_mean": float(v[far].mean())}


def effect_at(grun, omega0, centers, W, metric, lag: int = 0, n: int = 0,
              fix_omega: bool = False):
    """Эффект корзин при сдвиге неба на lag суток (+ = небо раньше).

    fix_omega=True — ω₀ берётся ОДНА на весь ряд (медиана трека). Это
    важный вариант, а не мелочь: при скользящей ω₀ расстояние до языка
    зависит от той же оценки периода, что снимается с самого отклика, и
    возникает КОНФАУНД «окно, где отклик сильнее, даёт другую ω₀».
    С фиксированной ω₀ расстояние двигает ТОЛЬКО небо. Self-тест [7]
    показывает цену вопроса числом.
    """
    cs = np.asarray(centers, int) - int(lag)
    ok = (cs >= 0) & (cs < n)
    if ok.sum() < MIN_WIN:
        return None
    om = np.asarray(omega0, float)
    if fix_omega:
        om = np.full(len(om), float(np.median(om)))
    g = grun[cs[ok]] * W
    tg = tongue_track(om[ok], g)
    return bucket_effect(tg["dist"], np.asarray(metric)[ok])


def effect_lagscan(grun, omega0, centers, W, metric, n: int,
                   lags=LAGS, fix_omega: bool = False) -> tuple:
    """max по сдвигам d со знаком гипотезы. Возвращает (d, lag)."""
    best, bl = None, None
    for L in lags:
        e = effect_at(grun, omega0, centers, W, metric, L, n, fix_omega)
        if e is None:
            continue
        if best is None or e["d"] > best:
            best, bl = e["d"], L
    return best, bl


def summary(dmap: dict) -> dict:
    """Свод по всем парам (ряд × тело): медиана, единодушие, лучшее тело."""
    vals = [v for v in dmap.values() if v is not None and np.isfinite(v)]
    if not vals:
        return {"S1": np.nan, "S3": np.nan, "S2": np.nan, "best": None,
                "per_body": {}, "n": 0}
    a = np.array(vals, float)
    per_body = {}
    for (_, b) in dmap:
        if b in per_body:
            continue
        v = [dmap[(s, bb)] for (s, bb) in dmap
             if bb == b and dmap[(s, bb)] is not None
             and np.isfinite(dmap[(s, bb)])]
        if v:
            per_body[b] = float(np.median(v))
    if per_body:
        best = max(per_body, key=lambda k: per_body[k])
        S2 = per_body[best]
    else:
        best, S2 = None, np.nan
    return {"S1": float(np.median(a)), "S3": float((a > 0).mean()),
            "S2": float(S2), "best": best, "per_body": per_body,
            "n": len(a)}


# ══════════════════════════════════════════════════════════════════════
# 5. СИНТЕТИКА: КОНТРОЛЬ ВСЕЙ ЦЕПОЧКИ (без random)
# ══════════════════════════════════════════════════════════════════════
def sim_sweep(omega0: float, gamma_fn, h: float, delta: float,
              n_days: int, dt: float = 0.02, force_amp: float = 0.005):
    """Затухающий параметрический осциллятор с ПЛАВАЮЩЕЙ накачкой:

        ẍ + 2δẋ + ω₀²(1 + h·cos φ(t))·x = F(t),   φ̇ = γ(t)

    F — детерминированная плотная гребёнка несоизмеримых линий (шум без
    random), чтобы отклик выбирал СВОЮ ω₀, а не ближайшую линию.
    Когда γ(t) проходит через 2ω₀, система входит в первый язык — это
    ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ: не найдя эффект здесь, сканер не найдёт его
    нигде. Возвращает (сутки, x, накопленная фаза накачки).
    """
    n = int(round(n_days / dt))
    t = np.arange(n + 1) * dt
    nf = 96
    gold = 0.5 * (math.sqrt(5.0) - 1.0)
    frq = 0.05 + (2.0 - 0.05) * (np.arange(nf) + 0.5) / nf
    phs = 2.0 * math.pi * ((np.arange(nf) + 1) * gold % 1.0)

    def F(tt):
        return force_amp * float(np.sum(np.sin(frq * tt + phs)))

    y = np.array([0.0, 0.0])
    xs = np.empty(n + 1)
    phis = np.empty(n + 1)
    xs[0] = 0.0
    phis[0] = 0.0
    ph = 0.0
    for i in range(n):
        tt = t[i]
        g = gamma_fn(tt)

        def f(s, yy, ph_=ph, g_=g, t_=tt):
            om2 = omega0 * omega0 * (1.0 + h * math.cos(ph_ + g_ * s))
            return np.array([yy[1], -2.0 * delta * yy[1] - om2 * yy[0]
                             + F(t_ + s)])
        k1 = f(0.0, y)
        k2 = f(dt / 2, y + dt / 2 * k1)
        k3 = f(dt / 2, y + dt / 2 * k2)
        k4 = f(dt, y + dt * k3)
        y = y + dt / 6.0 * (k1 + 2 * k2 + 2 * k3 + k4)
        ph += g * dt
        xs[i + 1] = y[0]
        phis[i + 1] = ph
    step = int(round(1.0 / dt))
    return t[::step], xs[::step], phis[::step]


def annual_part(g, period: float = 365.25):
    """Разложить бегущую накачку на ЧИСТО ГОДОВУЮ часть и остаток.

    🔵 Незакрытый долг всей охоты — отделить небо от обычной годовой
    сезонности. У внешних планет синодический круг сам ≈ год (367…378
    сут), поэтому их «частота накачки» почти целиком — календарный год.
    Возвращает (годовая часть с сохранённым средним, остаток с тем же
    средним, доля объяснённой дисперсии R²).
    """
    g = np.asarray(g, float)
    n = len(g)
    t = np.arange(n, dtype=float)
    A = np.column_stack([np.ones(n), np.cos(2 * math.pi * t / period),
                         np.sin(2 * math.pi * t / period)])
    m = np.isfinite(g)
    if m.sum() < 10:
        return g, g, float("nan")
    beta = np.linalg.lstsq(A[m], g[m], rcond=None)[0]
    fit = A @ beta
    v = float(np.var(g[m]))
    r2 = float(1.0 - np.var(g[m] - fit[m]) / v) if v > 0 else float("nan")
    resid = np.where(m, g - fit + float(np.mean(g[m])), np.nan)
    return np.where(m, fit, np.nan), resid, r2


def _run_mean(v, win):
    """Скользящее среднее по окну с центром в точке (NaN там, где не лезет)."""
    v = np.asarray(v, float)
    n = len(v)
    cs = np.concatenate([[0.0], np.cumsum(v)])
    out = np.full(n, np.nan)
    half = win // 2
    lo = np.arange(n) - half
    ok = (lo >= 0) & (lo + win <= n)
    out[ok] = (cs[lo[ok] + win] - cs[lo[ok]]) / win
    return out


# ══════════════════════════════════════════════════════════════════════
# 6. ЗАГРУЗКА ДАННЫХ
# ══════════════════════════════════════════════════════════════════════
def load_resto() -> dict:
    """16 пиццерий: дневные заказы на СПЛОШНОЙ календарной сетке."""
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
        out[code] = {"dates": ds, "x": x, "kind": "rel"}
    return out


def load_moex() -> dict:
    """8 бумаг MOEX: цена на календарной сетке + огибающая волатильности.

    🔵 ОГОВОРКА, обязательная: биржевой ряд имеет дыры по выходным, и на
    ЦЕНЕ это даёт ложный пик 7.0 сут (гребёнка выборки, а не осциллятор) —
    поймано ещё в mathieu.py. Поэтому собственная частота здесь берётся с
    ОГИБАЮЩЕЙ ВОЛАТИЛЬНОСТИ (20-дневное среднее |Δln|), полоса 20…180
    сут: на этих масштабах закрытие двухдневной дыры не значит ничего.
    Направление цены не трогаем вовсе — оно уже измерено и равно нулю.
    """
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
        m = np.isfinite(x)
        xi = np.interp(np.arange(n), np.where(m)[0], x[m])
        lr = np.concatenate([[np.nan], np.diff(np.log(xi))])
        vol = _run_mean(np.abs(np.nan_to_num(lr)), 21)
        out[tk] = {"dates": days, "x": x, "vol": vol, "kind": "ret"}
    return out


# ══════════════════════════════════════════════════════════════════════
# 7. ПРОГОН НА ДАННЫХ
# ══════════════════════════════════════════════════════════════════════
def prepare(series: dict, win, step, band, drop_week, track_key,
            metric_key="вола") -> dict:
    """Один раз: ω₀-трек, метрики отклика, бегущая накачка по всем телам."""
    out = {}
    for code, S in series.items():
        sky = S.get("sky")
        if sky is None:
            continue
        tr = omega_track(S[track_key], win, step, band, drop_week=drop_week)
        if len(tr["centers"]) < MIN_WIN:
            continue
        r = rel_series(S["x"], S["kind"])
        met = resp_metrics(r, tr["centers"], win)
        grun = {b: gamma_running(sky, b, win) for b in sky["phi1"]}
        out[code] = {"tr": tr, "met": met, "grun": grun,
                     "n": len(S["x"]), "sky": sky, "r": r}
    return out


def effects(prep: dict, Wmap: dict, metric_key="вола", lag: int = 0,
            lagscan: bool = False, fix_omega: bool = False) -> dict:
    dmap = {}
    for code, P in prep.items():
        for b, W in Wmap.items():
            if b not in P["grun"]:
                continue
            if lagscan:
                d, _ = effect_lagscan(P["grun"][b], P["tr"]["omega0"],
                                      P["tr"]["centers"], float(W),
                                      P["met"][metric_key], P["n"],
                                      fix_omega=fix_omega)
                dmap[(code, b)] = d
            else:
                e = effect_at(P["grun"][b], P["tr"]["omega0"],
                              P["tr"]["centers"], float(W),
                              P["met"][metric_key], lag, P["n"], fix_omega)
                dmap[(code, b)] = e["d"] if e else None
    return dmap


def _fmt_p(p):
    return "<0.0007" if p < 0.0007 else f"{p:.4f}"


def run_data():                                            # noqa: C901
    if aether is None:
        print("нет модуля aether — стенд честно молчит")
        return
    per = MT.body_periods()
    if per is None:
        print("нет эфемерид de440 — стенд честно молчит")
        return
    rng = np.random.default_rng(NULL_SEED)     # ТОЛЬКО нуль-стенд

    print("=" * 106)
    print("0. ЧТО ЗА ЧАСТОТА ПОЛУЧАЕТСЯ ИЗ ВЕДИЧЕСКОГО ЧИСЛА 🟡→🔵")
    print("=" * 106)
    print("   правило зафиксировано ДО прогона: γ = W·|d(синод.угол)/dt|")
    print(f"   {'тело':10s}{'W':>5s}{'T_syn':>10s}{'дрожание':>10s}"
          f"{'γ̄':>10s}{'T_накачки':>11s}{'2·T_накачки':>13s}{'в неделях':>11s}")
    for b in aether._BODY_ORDER:
        p = per.get(b, {})
        W = aether.W_VEDIC[b]
        if not p.get("T_syn"):
            print(f"   {b:10s}{W:>5d}{'—':>10s}{'—':>10s}{'—':>10s}"
                  f"{'—':>11s}{'—':>13s}{'синод. угла нет':>11s}")
            continue
        Tp = p["T_syn"] / W
        print(f"   {b:10s}{W:>5d}{p['T_syn']:>10.3f}"
              f"{p['T_syn_sd']/p['T_syn']:>9.2%}"
              f"{2*math.pi*W/p['T_syn']:>10.5f}{Tp:>11.4f}{2*Tp:>13.4f}"
              f"{2*Tp/7.0:>11.4f}")
    print("   🔵 читается так: у внешних планет синодический круг ≈ год, и")
    print("   деление его на 108 даёт полупериод ≈ 3.5 сут — то есть 2·T ≈")
    print("   неделя. Это арифметика календаря, а не открытие; проверяем,")
    print("   значит ли она хоть что-нибудь для отклика.")

    # ── РЕСТО ────────────────────────────────────────────────────────
    resto = load_resto()
    for code, S in resto.items():
        S["sky"] = sky_pump(S["dates"])
    resto = {c: S for c, S in resto.items() if S.get("sky")}

    print()
    print("=" * 106)
    print(f"1. РЕСТО: ω₀ СКОЛЬЗЯЩИМ ОКНОМ ({WIN_RESTO} сут, шаг "
          f"{STEP_RESTO} сут), 16 ТОЧЕК")
    print("=" * 106)
    prep_w = prepare(resto, WIN_RESTO, STEP_RESTO, BAND_WEEK, False, "x")
    prep_2 = prepare(resto, WIN_RESTO, STEP_RESTO, BAND_RESTO2, True, "x")
    print(f"   {'точка':7s}{'дней':>6s}{'окон':>6s}"
          f"{'недельный пик':>15s}{'σ':>8s}{'2-й пик':>10s}{'σ':>8s}")
    for c in sorted(prep_w):
        t1 = prep_w[c]["tr"]
        t2 = prep_2[c]["tr"] if c in prep_2 else None
        p2 = np.median(t2["period"]) if t2 is not None else float("nan")
        s2 = np.std(t2["period"]) if t2 is not None else float("nan")
        print(f"   {c:7s}{prep_w[c]['n']:>6d}{len(t1['centers']):>6d}"
              f"{np.median(t1['period']):>15.4f}{np.std(t1['period']):>8.4f}"
              f"{p2:>10.3f}{s2:>8.3f}")
    a1 = np.concatenate([P["tr"]["period"] for P in prep_w.values()])
    a2 = np.concatenate([P["tr"]["period"] for P in prep_2.values()])
    print(f"   ВСЕ 16: недельный пик {np.median(a1):.4f} ± {np.std(a1):.4f} "
          f"сут — ЖЁСТКИЙ КАЛЕНДАРЬ 🟡, ω₀ практически не гуляет")
    print(f"   ВСЕ 16: второй пик {np.median(a2):.2f} сут (σ {np.std(a2):.2f})"
          f" — вот он и гуляет")
    print("   ⇒ значит расстояние до языка двигает не ω₀, а мгновенная γ:")
    print("     дрожание синодического интервала. Так и считаем.")

    # ── строгий детектор на эталонной точке ──────────────────────────
    code0 = "LT01" if "LT01" in prep_w else sorted(prep_w)[0]
    P0 = prep_w[code0]
    bodies = [b for b in aether._BODY_ORDER if b in P0["grun"]]
    print()
    print("=" * 106)
    print(f"2. ДЕТЕКТОР ПО ТЕЛАМ (точка {code0}, недельный трек, "
          f"{len(P0['tr']['centers'])} окон)")
    print("=" * 106)
    print(f"   {'тело':10s}{'W':>5s}{'2ω₀/γ̄':>9s}{'n':>3s}"
          f"{'dist медиана':>14s}{'dist мин':>10s}{'dist макс':>11s}"
          f"{'h медиана':>11s}{'полушир.h/4':>13s}{'окон μ>0':>10s}")
    nmu, nwin = 0, 0
    for b in bodies:
        W = float(aether.W_VEDIC[b])
        g = P0["grun"][b][P0["tr"]["centers"]] * W
        hh = h_at(P0["sky"], b, W, P0["tr"]["centers"], WIN_RESTO)
        tg = tongue_track(P0["tr"]["omega0"], g, hh, with_mu=True)
        npos = int(tg["unstable"].sum())
        nmu += npos
        nwin += len(tg["dist"])
        print(f"   {b:10s}{int(W):>5d}{np.median(tg['ratio']):>9.4f}"
              f"{int(np.median(tg['n'])):>3d}{np.median(tg['dist']):>14.5f}"
              f"{tg['dist'].min():>10.5f}{tg['dist'].max():>11.5f}"
              f"{np.nanmedian(hh):>11.2e}{np.nanmedian(hh)/4:>13.2e}"
              f"{npos:>10d}")
    print(f"   Точка {code0}: {nmu} окон из {nwin} действительно в языке.")
    print("   (Знак μ для этого счёта НЕ годится: в полосе устойчивости")
    print("   система гамильтонова, |λ|=1 точно, и μ там — численный шум")
    print("   ~1e−15. Честный критерий — |tr M| > 2 из ядра mathieu.py.)")
    print()
    print("   ТО ЖЕ ПО ВСЕМ 16 ТОЧКАМ — можно ли вообще набрать корзину")
    print("   «в языке»? Корзине нужно ≥8 окон у пары (ряд × тело):")
    print(f"   {'тело':10s}{'окон в языке':>14s}{'из':>8s}{'доля':>9s}"
          f"{'языки n':>10s}{'рост за окно':>14s}{'пар с ≥8 окнами':>18s}")
    tot_in = tot_all = tot_pairs = 0
    strict_mask = {}
    for b in bodies:
        W = float(aether.W_VEDIC[b])
        nin = nall = npair = 0
        ns, gr = set(), []
        for code, P in prep_w.items():
            g = P["grun"][b][P["tr"]["centers"]] * W
            hh = h_at(P["sky"], b, W, P["tr"]["centers"], WIN_RESTO)
            tg = tongue_track(P["tr"]["omega0"], g, hh, with_mu=True)
            u = tg["unstable"]
            if u.sum() >= 8 and (~u).sum() >= 8:
                strict_mask[(code, b)] = u.copy()
            nin += int(u.sum())
            nall += len(u)
            npair += 1 if u.sum() >= 8 else 0
            if u.any():
                ns |= set(int(v) for v in tg["n"][u])
                gr += list(tg["mu"][u] * WIN_RESTO)
        tot_in += nin
        tot_all += nall
        tot_pairs += npair
        g_med = f"{np.median(gr):.3f}" if gr else "—"
        print(f"   {b:10s}{nin:>14d}{nall:>8d}{nin/nall:>9.2%}"
              f"{str(sorted(ns)) if ns else '—':>10s}{g_med:>14s}"
              f"{npair:>18d}")
    print(f"   ИТОГО: {tot_in} окон из {tot_all} ({tot_in/tot_all:.2%}); "
          f"пар (ряд × тело) с полноценной корзиной «в языке»: "
          f"{tot_pairs} из {len(prep_w)*len(bodies)}.")
    print("   ⇒ СТРОГУЮ КОРЗИНУ НЕ ИЗ ЧЕГО СЛОЖИТЬ: там, где h велика")
    print("   (Меркурий h=0.16), расстояние до языка на порядок больше")
    print("   полуширины; там, где расстояние мало (Сатурн, Уран, Плутон —")
    print("   dist ~1e−4…3e−2), измеренная h падает до 1e−5…1e−3.")
    print()
    print("=" * 106)
    print("2б. СТРОГАЯ ФОРМА ТАМ, ГДЕ ОНА ВСЁ-ТАКИ СЧИТАЕТСЯ")
    print("=" * 106)
    strict_real = {}
    if not strict_mask:
        print("   ни одной пары с полноценными корзинами — строгий тест "
              "невозможен")
    else:
        print(f"   пар с корзинами «в языке»/«вне»: {len(strict_mask)}")
        print(f"   {'пара':20s}{'окон в языке':>14s}{'вне':>7s}"
              f"{'вола в языке':>14s}{'вне':>10s}{'d':>9s}")
        for (code, b), u in sorted(strict_mask.items()):
            v = prep_w[code]["met"]["вола"]
            m = np.isfinite(v)
            sd = float(np.std(v[m]))
            a_, b_ = v[u & m], v[(~u) & m]
            d = float((a_.mean() - b_.mean()) / sd)
            strict_real[(code, b)] = d
            print(f"   {code+'×'+b:20s}{int(u.sum()):>14d}"
                  f"{int((~u).sum()):>7d}{a_.mean():>14.4f}"
                  f"{b_.mean():>10.4f}{d:>+9.4f}")
        arr = np.array(list(strict_real.values()))
        print(f"   медиана d = {np.median(arr):+.4f}, знак d>0 у "
              f"{int((arr > 0).sum())}/{len(arr)}")
        if iaaft is not None:
            nsC = 0
            meds = []
            for _ in range(NULL_C_DRAWS):
                vals = []
                for (code, b), u in strict_mask.items():
                    x = resto[code]["x"]
                    m0 = np.isfinite(x)
                    z = x.copy()
                    z[m0] = iaaft(x[m0], rng)
                    mt = resp_metrics(rel_series(z, "rel"),
                                      prep_w[code]["tr"]["centers"],
                                      WIN_RESTO)["вола"]
                    m = np.isfinite(mt)
                    sd = float(np.std(mt[m]))
                    if sd <= 0:
                        continue
                    vals.append(float((mt[u & m].mean() -
                                       mt[(~u) & m].mean()) / sd))
                if vals:
                    meds.append(float(np.median(vals)))
            meds = np.array(meds)
            nsC = float((meds >= np.median(arr)).mean())
            print(f"   нуль IAAFT ({len(meds)} реализаций): медиана "
                  f"{np.median(meds):+.4f}, p95 "
                  f"{np.quantile(meds, 0.95):+.4f}, макс {meds.max():+.4f}")
            print(f"   p(строгая форма) = {_fmt_p(nsC)}")
    print()
    print("   Дальше гипотеза берётся в САМОЙ ЩЕДРОЙ форме: корзины по")
    print("   БЛИЗОСТИ (нижняя треть dist против верхней), условие на")
    print("   неустойчивость снято совсем.")

    # ── БИРЖА ────────────────────────────────────────────────────────
    moex = load_moex()
    for tk, S in moex.items():
        S["sky"] = sky_pump(S["dates"])
    moex = {c: S for c, S in moex.items() if S.get("sky")}
    prep_m = prepare(moex, WIN_MOEX, STEP_MOEX, BAND_VOL, False, "vol")
    print()
    print("=" * 106)
    print(f"3. БИРЖА: ω₀ ОГИБАЮЩЕЙ ВОЛАТИЛЬНОСТИ ({WIN_MOEX} сут, шаг "
          f"{STEP_MOEX} сут), 8 БУМАГ")
    print("=" * 106)
    print("   🔵 на цене ω₀ не берём: пик 7.0 сут там — гребёнка выборки")
    print(f"   {'бумага':8s}{'дней':>6s}{'окон':>6s}{'ω₀ период':>12s}"
          f"{'σ':>9s}")
    for c in sorted(prep_m):
        t = prep_m[c]["tr"]
        print(f"   {c:8s}{prep_m[c]['n']:>6d}{len(t['centers']):>6d}"
              f"{np.median(t['period']):>12.2f}{np.std(t['period']):>9.2f}")

    # ── корзины ──────────────────────────────────────────────────────
    Wved = {b: float(aether.W_VEDIC[b]) for b in bodies}
    print()
    print("=" * 106)
    print("4. КОРЗИНЫ «БЛИЗКО К ЯЗЫКУ» ПРОТИВ «ДАЛЕКО» — НАСТОЯЩИЕ ЧИСЛА")
    print("=" * 106)
    print("   d = (среднее метрики близко − далеко)/σ; гипотеза требует d>0.")
    print("   S1 — медиана по всем парам (ряд × тело), S3 — доля пар с d>0.")
    print("   Четыре варианта одной и той же гипотезы, от буквального до")
    print("   самого щедрого:")
    print("     A  ω₀ скользящая, сдвиг 0        — буквально по директиве")
    print("     B  ω₀ скользящая, max по сдвигам ±270 сут")
    print("     C  ω₀ фиксированная, сдвиг 0     — без конфаунда ω₀")
    print("     D  ω₀ фиксированная, max по сдвигам — самый щедрый")
    print(f"   {'набор':7s}{'трек':11s}{'метрика':8s}{'пар':>5s}"
          f"{'A: S1':>9s}{'S3':>7s}{'B: S1':>9s}{'C: S1':>9s}{'D: S1':>9s}"
          f"{'  лучшее тело (A)':18s}")
    RUNS = [("РЕСТО", "недельный", prep_w), ("РЕСТО", "2-й пик", prep_2),
            ("БИРЖА", "вол-огиб", prep_m)]
    table = {}
    for nm, tag, prep in RUNS:
        for metric in ("вола", "амп", "экстр"):
            sA = summary(effects(prep, Wved, metric))
            sB = summary(effects(prep, Wved, metric, lagscan=True))
            sC = summary(effects(prep, Wved, metric, fix_omega=True))
            sD = summary(effects(prep, Wved, metric, lagscan=True,
                                 fix_omega=True))
            table[(nm, tag, metric)] = (sA, sB, sC, sD)
            print(f"   {nm:7s}{tag:11s}{metric:8s}{sA['n']:>5d}"
                  f"{sA['S1']:>+9.4f}{sA['S3']:>6.0%}{sB['S1']:>+9.4f}"
                  f"{sC['S1']:>+9.4f}{sD['S1']:>+9.4f}"
                  f"  {str(sA['best']):16s}")

    MAIN = ("РЕСТО", "недельный", "вола")
    rA, rB, rC, rD = table[MAIN]
    print(f"\n   ГЛАВНЫЙ ПРОГОН {MAIN[0]}/{MAIN[1]}/{MAIN[2]}:")
    print(f"     A  S1={rA['S1']:+.4f}  S3={rA['S3']:.1%}  "
          f"S2={rA['S2']:+.4f} ({rA['best']})")
    print(f"     D  S1={rD['S1']:+.4f}  S3={rD['S3']:.1%}  "
          f"S2={rD['S2']:+.4f} ({rD['best']})")
    print("   по телам (медиана d по 16 точкам):")
    print(f"     {'тело':10s}{'W':>4s}{'2ω₀/γ̄':>9s}{'dist медиана':>14s}"
          f"{'d (A)':>9s}{'d (D)':>9s}")
    for b in bodies:
        vA = rA["per_body"].get(b)
        vD = rD["per_body"].get(b)
        if vA is None:
            continue
        g = P0["grun"][b][P0["tr"]["centers"]] * aether.W_VEDIC[b]
        tg = tongue_track(P0["tr"]["omega0"], g)
        print(f"     {b:10s}{aether.W_VEDIC[b]:>4d}"
              f"{np.median(tg['ratio']):>9.4f}{np.median(tg['dist']):>14.5f}"
              f"{vA:>+9.4f}{vD:>+9.4f}")

    # ── 4б. СКОЛЬКО В ЭТОМ ПРОСТО ГОДА ───────────────────────────────
    print()
    print("=" * 106)
    print("4б. НЕЗАКРЫТЫЙ ДОЛГ: СКОЛЬКО В «РАССТОЯНИИ ДО ЯЗЫКА» ПРОСТО ГОДА")
    print("=" * 106)
    print("   У внешних планет синодический круг сам ≈ год (367…378 сут),")
    print("   поэтому их накачка почти целиком — календарь. Разлагаем")
    print("   бегущую γ на чисто ГОДОВУЮ часть и ОСТАТОК и считаем то же")
    print("   самое тремя способами.")
    print(f"   {'тело':10s}{'R² года в γ':>13s}{'S1 (A) полная':>15s}"
          f"{'год':>9s}{'остаток':>10s}{'S1 (D) полная':>15s}{'год':>9s}"
          f"{'остаток':>10s}")
    prep_year, prep_res = {}, {}
    r2s = {}
    for code, P in prep_w.items():
        gy, gr, rr = {}, {}, {}
        for b in bodies:
            fit, res, r2 = annual_part(P["grun"][b])
            gy[b], gr[b], rr[b] = fit, res, r2
        prep_year[code] = dict(P, grun=gy)
        prep_res[code] = dict(P, grun=gr)
        r2s[code] = rr
    for b in bodies:
        r2b = float(np.median([r2s[c][b] for c in r2s]))
        W1 = {b: float(aether.W_VEDIC[b])}
        sA = summary(effects(prep_w, W1, "вола"))
        sAy = summary(effects(prep_year, W1, "вола"))
        sAr = summary(effects(prep_res, W1, "вола"))
        sD = summary(effects(prep_w, W1, "вола", lagscan=True,
                             fix_omega=True))
        sDy = summary(effects(prep_year, W1, "вола", lagscan=True,
                              fix_omega=True))
        sDr = summary(effects(prep_res, W1, "вола", lagscan=True,
                              fix_omega=True))
        print(f"   {b:10s}{r2b:>12.1%}{sA['S1']:>+15.4f}{sAy['S1']:>+9.4f}"
              f"{sAr['S1']:>+10.4f}{sD['S1']:>+15.4f}{sDy['S1']:>+9.4f}"
              f"{sDr['S1']:>+10.4f}")
    sAy_all = summary(effects(prep_year, Wved, "вола"))
    sDy_all = summary(effects(prep_year, Wved, "вола", lagscan=True,
                              fix_omega=True))
    sAr_all = summary(effects(prep_res, Wved, "вола"))
    sDr_all = summary(effects(prep_res, Wved, "вола", lagscan=True,
                              fix_omega=True))
    print(f"   ВСЕ ТЕЛА: полная A={rA['S1']:+.4f}  год={sAy_all['S1']:+.4f}"
          f"  остаток={sAr_all['S1']:+.4f}")
    print(f"   ВСЕ ТЕЛА: полная D={rD['S1']:+.4f}  год={sDy_all['S1']:+.4f}"
          f"  остаток={sDr_all['S1']:+.4f}")

    # ── 4в. НАСКОЛЬКО ЭФФЕКТ ВООБЩЕ ЗАВИСИТ ОТ ВЕЛИЧИНЫ W ───────────
    # ── кэш эффектов по всем целым W (лукап для нулей а и б) ─────────
    Wgrid = list(range(1, 201))
    print("\n   … считаю кэш эффектов для W=1..200 по каждой паре "
          "(ряд × тело) …", flush=True)
    cacheA, cacheD = {}, {}
    for code, P in prep_w.items():
        for b in bodies:
            for W in Wgrid:
                e = effect_at(P["grun"][b], P["tr"]["omega0"],
                              P["tr"]["centers"], float(W),
                              P["met"]["вола"], 0, P["n"])
                cacheA[(code, b, W)] = e["d"] if e else None
                d2, _ = effect_lagscan(P["grun"][b], P["tr"]["omega0"],
                                       P["tr"]["centers"], float(W),
                                       P["met"]["вола"], P["n"],
                                       fix_omega=True)
                cacheD[(code, b, W)] = d2

    def summ_map(cache, Wm):
        return summary({(c, b): cache[(c, b, int(Wm[b]))]
                        for c in prep_w for b in bodies})

    Wint = {b: int(aether.W_VEDIC[b]) for b in bodies}
    chkA, chkD = summ_map(cacheA, Wint), summ_map(cacheD, Wint)
    assert abs(chkA["S1"] - rA["S1"]) < 1e-12, (chkA["S1"], rA["S1"])
    assert abs(chkD["S1"] - rD["S1"]) < 1e-12, (chkD["S1"], rD["S1"])

    # ══════════════════════════════════════════════════════════════════
    print()
    print("=" * 106)
    print("5. НУЛЬ (а): ТЕ ЖЕ ЧИСЛА, ПЕРЕТАСОВАННЫЕ МЕЖДУ ТЕЛАМИ")
    print("=" * 106)
    Wvals = [int(aether.W_VEDIC[b]) for b in bodies]
    perms = sorted(set(itertools.permutations(Wvals)))
    print(f"   тел {len(bodies)}, числа {sorted(Wvals)}")
    print(f"   различных перестановок {len(perms)} — берём ВСЕ, "
          f"ни одного random")
    A1, A3, D1 = [], [], []
    for pm in perms:
        Wm = dict(zip(bodies, pm))
        sA = summ_map(cacheA, Wm)
        sD = summ_map(cacheD, Wm)
        A1.append(sA["S1"]); A3.append(sA["S3"]); D1.append(sD["S1"])
    A1, A3, D1 = np.array(A1), np.array(A3), np.array(D1)
    pA_A = float((A1 >= rA["S1"]).mean())
    pA_S3 = float((A3 >= rA["S3"]).mean())
    pA_D = float((D1 >= rD["S1"]).mean())
    print(f"   вариант A: настоящее {rA['S1']:+.4f}; нуль — медиана "
          f"{np.median(A1):+.4f}, p95 {np.quantile(A1,0.95):+.4f}, "
          f"макс {A1.max():+.4f}  →  p = {_fmt_p(pA_A)}")
    print(f"   вариант D: настоящее {rD['S1']:+.4f}; нуль — медиана "
          f"{np.median(D1):+.4f}, p95 {np.quantile(D1,0.95):+.4f}, "
          f"макс {D1.max():+.4f}  →  p = {_fmt_p(pA_D)}")
    print(f"   единодушие S3: настоящее {rA['S3']:.1%}  →  "
          f"p = {_fmt_p(pA_S3)}")

    print()
    print("=" * 106)
    print("6. НУЛЬ (б): ПРОИЗВОЛЬНЫЕ ЦЕЛЫЕ ТОГО ЖЕ ПОРЯДКА")
    print("=" * 106)
    print("   СНАЧАЛА ГЛАВНОЕ ПРО СТРУКТУРУ: сколько РАЗНЫХ ответов даёт")
    print("   перебор W вообще. Если dist монотонна по γ (ratio не")
    print("   переходит через целое внутри ряда), порядок окон один и тот")
    print("   же при любом W, и эффект принимает всего два значения ±X —")
    print("   тогда «ведическое число как частота» не выбирает ничего,")
    print("   кроме знака.")
    print(f"   {'тело':10s}{'разных |d| (округл. 0.01)':>27s}"
          f"{'доля двух главных':>20s}")
    for b in bodies:
        row = np.array([np.median([cacheA[(c, b, W)] for c in prep_w
                                   if cacheA[(c, b, W)] is not None])
                        for W in Wgrid])
        rr = np.round(np.abs(row), 2)
        vals, cnts = np.unique(rr, return_counts=True)
        top = float(np.sort(cnts)[-2:].sum()) / len(rr)
        print(f"   {b:10s}{len(vals):>27d}{top:>19.0%}")
    print()
    print("   сплошной перебор W=1..200 по каждому телу "
          "(медиана d по 16 точкам, вариант A):")
    print(f"   {'тело':10s}{'W вед.':>7s}{'d(вед.)':>10s}"
          f"{'перцентиль из 200':>19s}{'макс d':>9s}{'при W':>7s}")
    pcts = []
    for b in bodies:
        row = np.array([np.median([cacheA[(c, b, W)] for c in prep_w
                                   if cacheA[(c, b, W)] is not None])
                        for W in Wgrid])
        Wv = int(aether.W_VEDIC[b])
        dv = row[Wv - 1]
        pct = float((row < dv).mean() * 100.0)
        pcts.append(pct)
        j = int(np.argmax(row))
        print(f"   {b:10s}{Wv:>7d}{dv:>+10.4f}{pct:>18.1f}%"
              f"{row[j]:>+9.4f}{Wgrid[j]:>7d}")
    print(f"   медиана перцентиля ведических чисел: {np.median(pcts):.1f}% "
          f"(50% = неотличимо от произвольного целого)")
    B1, B3, BD = [], [], []
    for _ in range(NULL_B_DRAWS):
        Wm = {b: int(rng.integers(W_ORDER[0], W_ORDER[1] + 1))
              for b in bodies}
        sA = summ_map(cacheA, Wm)
        sD = summ_map(cacheD, Wm)
        B1.append(sA["S1"]); B3.append(sA["S3"]); BD.append(sD["S1"])
    B1, B3, BD = np.array(B1), np.array(B3), np.array(BD)
    pB_A = float((B1 >= rA["S1"]).mean())
    pB_S3 = float((B3 >= rA["S3"]).mean())
    pB_D = float((BD >= rD["S1"]).mean())
    print(f"   {NULL_B_DRAWS} наборов целых [{W_ORDER[0]}..{W_ORDER[1]}]:")
    print(f"   вариант A: нуль — медиана {np.median(B1):+.4f}, p95 "
          f"{np.quantile(B1,0.95):+.4f}, макс {B1.max():+.4f}  →  "
          f"p = {_fmt_p(pB_A)}")
    print(f"   вариант D: нуль — медиана {np.median(BD):+.4f}, p95 "
          f"{np.quantile(BD,0.95):+.4f}, макс {BD.max():+.4f}  →  "
          f"p = {_fmt_p(pB_D)}")
    print(f"   единодушие S3  →  p = {_fmt_p(pB_S3)}")

    print()
    print("=" * 106)
    print(f"7. НУЛЬ (в): IAAFT-СУРРОГАТ ОТКЛИКА, {NULL_C_DRAWS} РЕАЛИЗАЦИЙ")
    print("=" * 106)
    pC_A = pC_D = pC_S3 = float("nan")
    if iaaft is None:
        print("   модуль e_surrogate недоступен — нуль (в) честно пропущен")
    else:
        print("   тот же спектр И то же распределение заказов; небо и")
        print("   ведические числа НАСТОЯЩИЕ — рвётся только временная")
        print("   привязка отклика к небу.")
        C1, C3, CD = [], [], []
        for it in range(NULL_C_DRAWS):
            dmA, dmD = {}, {}
            for code, S in resto.items():
                if code not in prep_w:
                    continue
                x = S["x"]
                m = np.isfinite(x)
                z = x.copy()
                z[m] = iaaft(x[m], rng)
                tr = omega_track(z, WIN_RESTO, STEP_RESTO, BAND_WEEK)
                if len(tr["centers"]) < MIN_WIN:
                    continue
                met = resp_metrics(rel_series(z, "rel"), tr["centers"],
                                   WIN_RESTO)
                P = prep_w[code]
                for b in bodies:
                    W = float(aether.W_VEDIC[b])
                    e = effect_at(P["grun"][b], tr["omega0"], tr["centers"],
                                  W, met["вола"], 0, P["n"])
                    dmA[(code, b)] = e["d"] if e else None
                    d2, _ = effect_lagscan(P["grun"][b], tr["omega0"],
                                           tr["centers"], W, met["вола"],
                                           P["n"], fix_omega=True)
                    dmD[(code, b)] = d2
            sA, sD = summary(dmA), summary(dmD)
            C1.append(sA["S1"]); C3.append(sA["S3"]); CD.append(sD["S1"])
            if (it + 1) % 50 == 0:
                print(f"     … {it+1}/{NULL_C_DRAWS}  медиана S1(A) "
                      f"{np.median(C1):+.4f}", flush=True)
        C1, C3, CD = np.array(C1), np.array(C3), np.array(CD)
        pC_A = float((C1 >= rA["S1"]).mean())
        pC_S3 = float((C3 >= rA["S3"]).mean())
        pC_D = float((CD >= rD["S1"]).mean())
        print(f"   вариант A: нуль — медиана {np.median(C1):+.4f}, p95 "
              f"{np.quantile(C1,0.95):+.4f}, макс {C1.max():+.4f}  →  "
              f"p = {_fmt_p(pC_A)}")
        print(f"   вариант D: нуль — медиана {np.median(CD):+.4f}, p95 "
              f"{np.quantile(CD,0.95):+.4f}, макс {CD.max():+.4f}  →  "
              f"p = {_fmt_p(pC_D)}")
        print(f"   единодушие S3  →  p = {_fmt_p(pC_S3)}")

    print()
    print("=" * 106)
    print("8. ВЕРДИКТ")
    print("=" * 106)
    psA = [p for p in (pA_A, pB_A, pC_A) if np.isfinite(p)]
    psD = [p for p in (pA_D, pB_D, pC_D) if np.isfinite(p)]
    aliveA = bool(psA) and max(psA) < 0.05
    aliveD = bool(psD) and max(psD) < 0.05
    print(f"   Строгая форма (|tr M|>2): {tot_in} окон из {tot_all} "
          f"({tot_in/tot_all:.2%}), полноценной корзины нет ни у одной")
    print(f"   пары из {len(prep_w)*len(bodies)} — проверять в ней нечего.")
    print(f"   Вариант A (буквально по директиве): S1={rA['S1']:+.4f}, "
          f"S3={rA['S3']:.1%};  p(а)={_fmt_p(pA_A)}  p(б)={_fmt_p(pB_A)}  "
          f"p(в)={_fmt_p(pC_A)}")
    print(f"   Вариант D (самый щедрый):          S1={rD['S1']:+.4f}, "
          f"S3={rD['S3']:.1%};  p(а)={_fmt_p(pA_D)}  p(б)={_fmt_p(pB_D)}  "
          f"p(в)={_fmt_p(pC_D)}")
    print(f"   ВЕДИЧЕСКИЕ ЧИСЛА КАК ЧАСТОТЫ НАКАЧКИ: "
          f"{'ОЖИВАЮТ' if (aliveA or aliveD) else 'НЕ ОЖИВАЮТ'}")
    print("   ⚫ ни одна цифра отсюда не является указанием к действию.")


# ══════════════════════════════════════════════════════════════════════
# SELF-ТЕСТЫ: ВЕЛИЧИНЫ И ИЗВЕСТНЫЕ АНАЛИТИЧЕСКИЕ ОТВЕТЫ
# ══════════════════════════════════════════════════════════════════════
def _selftest():                                           # noqa: C901
    print("=" * 92)
    print("SELF-ТЕСТЫ СКАНЕРА МАТЬЕ")
    print("=" * 92)

    # [1] периодограмма: чистый тон восстанавливается лучше 0.5%
    print("\n[1] dominant_period на чистых тонах")
    print(f"    {'T истинный':>12s}{'найдено':>12s}{'ошибка':>10s}")
    for T in (7.0, 12.5, 31.0, 64.0, 100.0):
        n = 512
        t = np.arange(n, dtype=float)
        x = np.sin(2 * math.pi * t / T + 0.7)
        d = dominant_period(x, (max(2.5, T / 3), min(400.0, T * 3)))
        err = abs(d["period"] - T) / T
        print(f"    {T:>12.3f}{d['period']:>12.4f}{err:>9.3%}")
        assert err < 0.005, (T, d)
    n = 730
    t = np.arange(n, dtype=float)
    x = 1.0 * np.sin(2 * math.pi * t / 7.0) + 3.0 * np.sin(2 * math.pi * t / 33.0)
    d = dominant_period(x, (2.5, 200.0))
    assert abs(d["period"] - 33.0) / 33.0 < 0.01, d
    print(f"    два тона (7 слабый, 33 сильный): найден {d['period']:.3f} ✓")

    # [2] вычитание недельной гребёнки открывает второй пик
    print("\n[2] strip_harmonics: недельная гребёнка вычтена")
    x = (5.0 * np.sin(2 * math.pi * t / 7.0)
         + 2.0 * np.sin(2 * math.pi * t / 3.5 + 1.1)
         + 1.0 * np.sin(2 * math.pi * t / 29.0 + 0.3))
    d_raw = dominant_period(x, (2.5, 200.0))
    y = strip_harmonics(x)
    d_cut = dominant_period(y, (2.5, 200.0))
    print(f"    до вычитания:  {d_raw['period']:.4f} сут (неделя)")
    print(f"    после:         {d_cut['period']:.4f} сут (истина 29.0)")
    assert abs(d_raw["period"] - 7.0) / 7.0 < 0.01, d_raw
    assert abs(d_cut["period"] - 29.0) / 29.0 < 0.02, d_cut
    resid = float(np.abs(np.fft.rfft(y * _hann(n))[round(n / 7.0)]))
    full = float(np.abs(np.fft.rfft(x * _hann(n))[round(n / 7.0)]))
    print(f"    амплитуда на 7 сут: {full:.1f} → {resid:.3f} "
          f"({resid/full:.4%} от исходной) ✓")
    assert resid / full < 0.01, (resid, full)

    # [3] tongue_track согласован с ядром mathieu.py бит-в-бит
    print("\n[3] dist = |2ω₀/γ − n| согласовано с a = 4ω₀²/γ²")
    om = np.array([0.8976, 0.9, 1.0, 2.0])
    ga = np.array([1.7947, 1.5, 3.0, 2.0])
    tg = tongue_track(om, ga)
    for k in range(len(om)):
        fp = MT.floquet_physical(float(om[k]), float(ga[k]), 0.01)
        assert abs(tg["a"][k] - fp["a"]) < 1e-12, (k, tg["a"][k], fp["a"])
        assert tg["n"][k] == fp["n_nearest"], (k, tg["n"][k])
        assert abs(tg["dist"][k] - abs(fp["detune"]) * tg["n"][k]) < 1e-12
    print(f"    a, n и расстройка совпали с ядром на {len(om)} точках ✓")
    tg1 = tongue_track(np.array([1.0]), np.array([2.0]), np.array([0.1]),
                       with_mu=True)
    exp = 0.1 * 1.0 / 4.0
    print(f"    центр первого языка: dist={tg1['dist'][0]:.1e}, "
          f"μ={tg1['mu'][0]:.6f} против h·ω₀/4={exp:.6f} "
          f"({abs(tg1['mu'][0]-exp)/exp:.2%}) ✓")
    assert tg1["dist"][0] == 0.0 and tg1["mu"][0] > 0
    assert abs(tg1["mu"][0] - exp) / exp < 0.05
    tg2 = tongue_track(np.array([1.0]), np.array([2.0 / 1.2]),
                       np.array([0.1]), with_mu=True)
    print(f"    расстройка +20%: dist={tg2['dist'][0]:.4f}, "
          f"μ={tg2['mu'][0]:.2e} — язык не достаёт ✓")
    assert abs(tg2["mu"][0]) < 1e-12 and tg2["dist"][0] > 0.15

    # [4] rel_series / resp_metrics считают ИЗВЕСТНЫЕ величины
    print("\n[4] метрики отклика на аналитически известном ряде")
    nn = 700
    tt = np.arange(nn, dtype=float)
    amp = 0.20
    xs = 100.0 * (1.0 + amp * np.sin(2 * math.pi * tt / 7.0))
    r = rel_series(xs, "rel")
    ctr = np.array([200, 350, 500])
    mm = resp_metrics(r, ctr, 182)
    # аналитика: база — скользящее среднее L=29 точек по синусу периода 7,
    # ядро Дирихле D = sin(πL/T)/(L·sin(π/T)) = 1/29 (29/7 = 4 + 1/7).
    # Значит r = A(1−D)·sin, откуда амп = A(1−D), вола = A(1−D)/√2.
    L = 29
    D = math.sin(math.pi * L / 7.0) / (L * math.sin(math.pi / 7.0))
    a_th = amp * (1.0 - D)
    print(f"    синус ±{amp:.0%}: амп={mm['амп'][0]:.6f} против теории "
          f"A(1−1/29)={a_th:.6f};  вола={mm['вола'][0]:.6f} против "
          f"{a_th/math.sqrt(2):.6f}")
    assert abs(D - 1.0 / 29.0) < 1e-12, D
    assert abs(mm["амп"][0] - a_th) < 2e-4, (mm["амп"][0], a_th)
    assert abs(mm["вола"][0] - a_th / math.sqrt(2)) < 2e-4, mm["вола"][0]
    assert abs(mm["разм"][0] - 2.0 * a_th * math.sin(0.9 * math.pi / 2)) \
        < 0.02, mm["разм"][0]
    lg = rel_series(np.exp(0.001 * tt) * 100.0, "ret")
    assert abs(np.nanmean(lg) - 0.001) < 1e-9, np.nanmean(lg)
    print(f"    Δln на экспоненте: {np.nanmean(lg):.6f} (истина 0.001000) ✓")

    # [5] ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ: сканер обязан НАЙТИ настоящий резонанс
    print("\n[5] положительный контроль: накачка медленно проходит язык")
    om0 = 2 * math.pi / 7.0
    h, delta, Psw, nd, sw = 0.25, 0.030, 2000.0, 6000, 0.08
    print(f"    ω₀=2π/7, h={h}, δ={delta} (порог языка h>4δ/ω₀="
          f"{4*delta/om0:.3f}), качание γ ±{sw:.0%} с периодом {Psw:.0f} сут")

    def gfn(x):
        return 2.0 * om0 * (1.0 + sw * math.cos(2 * math.pi * x / Psw))
    _, xs, phs = sim_sweep(om0, gfn, h, delta, nd)
    xr = 200.0 + 20.0 * xs / float(np.std(xs))
    tr = omega_track(xr, WIN_RESTO, STEP_RESTO, BAND_WEEK)
    assert len(tr["centers"]) > 100, len(tr["centers"])
    assert abs(np.median(tr["period"]) - 7.0) / 7.0 < 0.02, \
        np.median(tr["period"])
    print(f"    окон {len(tr['centers'])}, ω₀ найдена из ряда: период "
          f"{np.median(tr['period']):.4f} (истина 7.0) ✓")
    grun = _run_mean(np.gradient(phs, 1.0), WIN_RESTO)
    met = resp_metrics(rel_series(xr, "rel"), tr["centers"], WIN_RESTO)

    def _pair(g, om, ct, mt, n):
        e = effect_at(g, om, ct, 1.0, mt, 0, n)
        d0 = e["d"] if e else float("nan")
        dm, lg = effect_lagscan(g, om, ct, 1.0, mt, n)
        ef = effect_at(g, om, ct, 1.0, mt, 0, n, fix_omega=True)
        f0 = ef["d"] if ef else float("nan")
        fm, fl = effect_lagscan(g, om, ct, 1.0, mt, n, fix_omega=True)
        return d0, dm, lg, f0, fm, fl
    d0, dm, lg, f0, fm, fl = _pair(grun, tr["omega0"], tr["centers"],
                                   met["вола"], len(xr))
    print(f"    ω₀ скользящая: d(0)={d0:+.4f}   max по сдвигам {dm:+.4f} "
          f"при {lg:+d} сут")
    print(f"    ω₀ фиксированная: d(0)={f0:+.4f}   max по сдвигам "
          f"{fm:+.4f} при {fl:+d} сут")
    print(f"    отклик ОТСТАЁТ от языка — инерция осциллятора 1/δ="
          f"{1/delta:.0f} сут, и это физика, а не подгонка")
    assert d0 > 0.5, ("сканер не видит настоящий резонанс", d0)
    assert dm > 1.0 and lg > 0, (dm, lg)
    assert fm > 1.0, ("фиксированная ω₀ теряет настоящий резонанс", fm)

    # [6] ОТРИЦАТЕЛЬНЫЙ КОНТРОЛЬ: та же машина, но h = 0
    print("\n[6] отрицательный контроль: h=0 (параметрической связи нет)")
    _, xs0, phs0 = sim_sweep(om0, gfn, 0.0, delta, nd)
    xr0 = 200.0 + 20.0 * xs0 / float(np.std(xs0))
    tr0 = omega_track(xr0, WIN_RESTO, STEP_RESTO, BAND_WEEK)
    grun0 = _run_mean(np.gradient(phs0, 1.0), WIN_RESTO)
    met0 = resp_metrics(rel_series(xr0, "rel"), tr0["centers"], WIN_RESTO)
    z0, zm, zl, zf0, zfm, zfl = _pair(grun0, tr0["omega0"], tr0["centers"],
                                      met0["вола"], len(xr0))
    print(f"    ω₀ скользящая: d(0)={z0:+.4f}   max по сдвигам {zm:+.4f}")
    print(f"    ω₀ фиксированная: d(0)={zf0:+.4f}   max по сдвигам "
          f"{zfm:+.4f}")
    print(f"    сигнал/шум по max: {dm/max(zm,1e-9):.1f}× (скользящая), "
          f"{fm/max(zfm,1e-9):.1f}× (фиксированная) ✓")
    assert abs(z0) < 0.5 * d0 and zm < 0.5 * dm, (z0, zm)
    assert zfm < 0.5 * fm, (zfm, fm)

    # [7] ЧУЖАЯ НАКАЧКА + ЦЕНА КОНФАУНДА СКОЛЬЗЯЩЕЙ ω₀
    print("\n[7] чужая накачка (γ×1.618, до целого далеко всегда)")
    tt2 = np.arange(len(xr), dtype=float)
    alien = 2.0 * om0 * 1.618 * (1.0 + sw * np.cos(2 * math.pi * tt2 / 137.0))
    ga = _run_mean(alien, WIN_RESTO)
    tga = tongue_track(tr["omega0"], ga[tr["centers"]])
    a0, am, al, af0, afm, afl = _pair(ga, tr["omega0"], tr["centers"],
                                      met["вола"], len(xr))
    print(f"    расстояние до языка: медиана {np.median(tga['dist']):.4f} "
          f"— язык недостижим по построению")
    print(f"    ω₀ скользящая: d(0)={a0:+.4f}  max {am:+.4f}  ← ЛОЖНЫЙ "
          f"эффект: {a0/d0:.0%} от настоящего")
    print(f"    ω₀ фиксированная: d(0)={af0:+.4f}  max {afm:+.4f}  ← "
          f"чисто")
    print("    ВЫВОД (важен для чтения данных): скользящая ω₀ снимается с")
    print("    того же отклика, поэтому расстояние до языка частично несёт")
    print("    состояние самого отклика. Нули (а),(б),(в) этот конфаунд")
    print("    воспроизводят целиком, поэтому p остаются честными; но")
    print("    вариант с фиксированной ω₀ печатается отдельно.")
    assert np.median(tga["dist"]) > 0.3, tga
    assert abs(af0) < 0.15 and afm < 0.25, (af0, afm)
    assert afm < 0.3 * fm, (afm, fm)

    # [8] детерминизм и отсутствие random в рабочем пути
    print("\n[8] детерминизм")
    a1 = dominant_period(np.sin(np.arange(300.0) / 3.0), (2.5, 100.0))
    a2 = dominant_period(np.sin(np.arange(300.0) / 3.0), (2.5, 100.0))
    assert a1 == a2
    gd = np.array([1.7947, 1.5, 3.0, 2.0])
    t1 = tongue_track(om, gd)
    t2 = tongue_track(om, gd)
    assert np.array_equal(t1["dist"], t2["dist"])
    e1 = effect_at(grun, tr["omega0"], tr["centers"], 1.0, met["вола"], 0,
                   len(xr))
    e2 = effect_at(grun, tr["omega0"], tr["centers"], 1.0, met["вола"], 0,
                   len(xr))
    assert e1 == e2 and e1["d"] == e2["d"], (e1, e2)
    src = open(__file__, encoding="utf-8").read()
    work = src.split("def run_data")[0]
    assert "np.random" not in work and "import random" not in work, \
        "генератор случайных чисел просочился в рабочий путь"
    prod = src.split("def _selftest")[0]
    assert work.count("default_rng") == 0
    assert prod.count("default_rng") == 1, "нуль-стенд должен быть один"
    print("    два вызова совпали бит-в-бит; генератор случайных чисел "
          "живёт ровно в одном месте — нуль-стенде run_data ✓")

    # [9] summary ведёт себя как обещано
    print("\n[9] summary: медиана, единодушие, лучшее тело")
    dm = {("A", "Юпитер"): 0.5, ("B", "Юпитер"): 0.7,
          ("A", "Сатурн"): -0.2, ("B", "Сатурн"): -0.4}
    s = summary(dm)
    assert s["n"] == 4 and abs(s["S1"] - 0.15) < 1e-12, s
    assert abs(s["S3"] - 0.5) < 1e-12 and s["best"] == "Юпитер", s
    assert abs(s["S2"] - 0.6) < 1e-12, s
    print(f"    S1={s['S1']:+.3f} S3={s['S3']:.2f} S2={s['S2']:.3f} "
          f"({s['best']}) ✓")

    # [10] небо: мгновенная γ согласована со средней 2πW/T_syn
    per = MT.body_periods()
    if per is not None and aether is not None:
        print("\n[10] мгновенная γ против средней 2πW/T_syn (100 лет)")
        dts = [date(1930, 1, 1) + timedelta(i) for i in range(36525)]
        sky = sky_pump(dts)
        if sky is not None:
            print(f"     {'тело':10s}{'⟨γ⟩ мгнов.':>13s}{'2πW/T_syn':>13s}"
                  f"{'откл':>8s}{'дрожание σ/γ':>15s}")
            for b in [x for x in aether._BODY_ORDER if x in sky["phi1"]]:
                W = float(aether.W_VEDIC[b])
                g = sky["dphi1"][b] * W
                ref = 2.0 * math.pi * W / per[b]["T_syn"]
                dev = abs(float(np.mean(g)) / ref - 1.0)
                print(f"     {b:10s}{np.mean(g):>13.5f}{ref:>13.5f}"
                      f"{dev:>7.2%}{np.std(g)/np.mean(g):>14.2%}")
                assert dev < 0.02, (b, dev)
            gr = gamma_running(sky, "Сатурн", WIN_RESTO)
            man = float(np.mean(sky["dphi1"]["Сатурн"][1000 - 91:1000 + 91]))
            assert abs(gr[1000] - man) < 1e-12, (gr[1000], man)
            assert np.isnan(gr[10]) and np.isnan(gr[-10])
            print("     мгновенная и средняя накачка — одно и то же; "
                  "бегущее среднее совпало с прямым ✓")
    else:
        print("\n[10] эфемерид нет — небесная часть честно пропущена")

    print("\n" + "=" * 92)
    print("ВСЕ SELF-ТЕСТЫ СКАНЕРА ПРОЙДЕНЫ")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "data":
        run_data()
    else:
        _selftest()
