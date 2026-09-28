"""ОРАКУЛ // ПИФИЯ — ЭФИРНЫЙ СЛОЙ (перенос движка REAL SKY, fin ENGINE 16).

Что это: волна Ψ инструмента — резонанс транзитного неба с наталом генезиса
через эфирную плотность E_i = d_ap·W_flux·хроно-член (Козырев): вес тела —
фотометрия (альбедо·апертура², v3.9), частоты — реальные dλ/dt; гармоники
аспектов k∈{1,6,4,3,2}; поверх — «обновление от цены»: аналитический сигнал
Габора–Гильберта на РЕАЛЬНЫХ свечах, сцепка фаз PLV эфир↔цена, аффинная
калибровка эфира в шкалу цены, многоуровневые ПЛИТЫ (день/неделя/месяц —
квантили реальной цены, Хайндман–Фан тип 7) и учёт «где потенциал волны уже
реализован ценой, где ещё жив».

Рамка честности (закон школы, печатается в каждом блоке):
  🔵 механика — эфемериды DE440 (skyfield), квантили цены, Гильберт/PLV;
  🟡 язык школы — Ψ, E_i, генезисы, ширины окон: символическая система,
     НЕ физика рынка; все прокси названы;
  ⚫ полюс за человеком — слой НЕ даёт торговых указаний и не предсказывает
     рынок; это фильтр-контекст. 18+.

Контракт — как у astro.py: compute_context(...) → dict (ошибки честные,
без выдуманных чисел), render_for_ai(ctx) → текст-блок для ИИ (НЕ дублирует
астро-блок: небо/аспекты/станции живут там, здесь — волна×цена),
chart_payload(ctx) → серии для графика, short_line(ctx) → строка для шапки.
Эфемерид нет → mode="unavailable" с причиной; цены нет → считается только
небесная часть. Никакого random — детерминизм байт-в-байт.
"""
from __future__ import annotations

import math
import threading
import time as _time
from datetime import datetime, timedelta, timezone

import numpy as np

from . import astro

# ══════════════════════════════════════════════════════════════════════
# КОНСТАНТЫ ШКОЛЫ (перенос из REAL SKY unified_tensor/fin_futures, с атрибуцией)
# ══════════════════════════════════════════════════════════════════════
# лесенка 9 «нота тела» 🟡 — с v3.9 ТОЛЬКО МЕТКА band (подпись генезиса/актива),
# НЕ вес и НЕ частота. Вердикт мировоззрения 05.08.2026: «частоты из справочника
# у планет — грубая подгонка, нумерология в литералах». Мощь тел считается
# фотометрией (ALBEDO_G ниже), частоты — реальными dλ/dt эфемерид.
W_VEDIC = {"Плутон": 108, "Сатурн": 108, "Уран": 108, "Юпитер": 54,
           "Венера": 54, "Луна": 27, "Меркурий": 27, "Марс": 72,
           "Солнце": 54, "Нептун": 81}
# геометрические альбедо тел 🔵 (измеренные величины). КАНОННЫЙ БЛОК —
# единственный источник таблицы в Пифии: reactor_sky и прочие ИМПОРТИРУЮТ
# отсюда, литерал-копий не заводить. Солнце самосветящееся — альбедо
# неприменимо, вес по апертуре: albedo := 1.0 (честный прокси, назван).
# Прокси: фазовый угол опущен — вес по полному видимому диску, не по фазе.
ALBEDO_G = {"Меркурий": 0.142, "Венера": 0.689, "Марс": 0.170,
            "Юпитер": 0.538, "Сатурн": 0.499, "Уран": 0.488,
            "Нептун": 0.442, "Луна": 0.136, "Плутон": 0.52, "Солнце": 1.0}
ALBEDO_REF = ALBEDO_G["Луна"]   # 0.136 — нормировка «Луна на среднем расстоянии ≈ 1»
# радиусы IAU, км 🔵 — для видимого углового диаметра D_apparent
R_IAU_KM = {"Солнце": 696000.0, "Луна": 1737.4, "Меркурий": 2439.7,
            "Венера": 6051.8, "Марс": 3389.5, "Юпитер": 69911.0,
            "Сатурн": 58232.0, "Уран": 25362.0, "Нептун": 24622.0,
            "Плутон": 1188.3}
MOON_MEAN_KM = 384400.0
THETA_REF_DEG = math.degrees(2.0 * math.asin(R_IAU_KM["Луна"] / MOON_MEAN_KM))
EPS_CHRONO = 0.02      # °/сут; регуляризация хроно-члена (станция — плато)
CHRONO_CAP = 50.0      # кэп хроно-члена Козырева
# аспект → гармоника: 0°→k1, 60°→k6, 90°→k4, 120°→k3, 180°→k2
_ASP_ANG = np.array([0.0, 60.0, 90.0, 120.0, 180.0])
_ASP_K = np.array([1.0, 6.0, 4.0, 3.0, 2.0])
# v3.7: AFFINITY (спектральное сродство 1.5 телу ноты band) УДАЛЁН — вердикт
# мировоззрения 05.08.2026: «подгонка под ответ». Вклад тел равноправен (B=1),
# отбор — физикой E (диск × хроно-член). См. note в psi_of / wave.
PLV_LOCK = 0.7         # порог «фазы сцеплены» (Лашо 1999)
EDGE_FRAC = 12         # краевая зона Гильберта: n//12 точек с каждого края
Q_PLATES = (10.0, 50.0, 90.0)   # плиты: квантили цены (Хайндман–Фан тип 7)
LOUD_THRESHOLD = 0.35  # порог «пучность/тишина» — единая константа школы
MICRO_HALF_LIFE_H = 6.0  # полураспад веса свежести микро-плит скальпа
E_FLOOR_FRAC = 0.02    # энергия ниже 2% пика окна — тишина (шум утечки FFT)

# ИЗМЕРЕНО, А НЕ ОБЪЯВЛЕНО (стенды backend/lab/, июль 2026). Слой прогнали
# тем же протоколом, что убил небесные гипотезы на ресторанных данных:
#   · 8 бумаг MOEX, 3354 торговых дня 2013-2026, кластер = календарный день,
#     249 небесных признаков, train до 2023 / held-out после, BH q=0.10;
#   · НАПРАВЛЕНИЕ: ноль признаков прошёл даже обучение (прежние 28 «прошедших»
#     оказались артефактом слияния скоррелированных бумаг в один пул);
#   · ВОЛАТИЛЬНОСТЬ: 36 признаков пережили held-out, но нуль из перестановки
#     годовых блоков даёт p = 0.0500 — ровно граница, то есть неотличимо;
#     контроль добил: ЧИСТЫЙ ЛИНЕЙНЫЙ ТРЕНД «выжил бы» тот же тест, а сами
#     выжившие коррелируют с пустым медленным трендом на 0.78…0.97;
#   · СЦЕПКА PLV: против четырёх нулей (сдвиг Ψ, фазовый суррогат, чужой
#     эфир, гладкий шум) настоящий PLV бьёт нуль только у Brent на дневном
#     масштабе (98.7 / 96.5 / 99.5 перцентиля); у трёх акций — нет.
# Отсюда рамка ниже: не «наука пока не поняла», а «проверено и не подтвердилось».
FRAME = ("эфир — символический слой школы REAL SKY 🟡 · его предсказательная "
         "сила ИЗМЕРЕНА и не подтвердилась: в строгих тестах (плацебо, "
         "held-out, поправка на множественность, перестановочные нули) "
         "поведение слоя неотличимо от случайного 🔵 · слой остаётся как "
         "геометрия неба и как демонстрация фазового анализа, а НЕ как "
         "предсказатель · НЕ торговый сигнал и не инвестиционный совет · "
         "рынок не предсказывается · 18+ · полюс и решения за человеком ⚫")

# генезисы инструментов — канон школы REAL SKY (первая сделка / генезис-блок);
# ключ — корень тикера; band — гармоника лесенки 9 🟡. Расширяется конфигом.
GENESIS = {
    "BR":  {"genesis": (1988, 6, 23, 12, 0, 0), "band": 108,
            "note": "первый день торгов Brent-фьючерса (ICE, Лондон) — канон школы"},
    "GD":  {"genesis": (1974, 12, 31, 12, 0, 0), "band": 108,
            "note": "старт золотых фьючерсов COMEX — канон школы"},
    "GOLD": {"genesis": (1974, 12, 31, 12, 0, 0), "band": 108,
             "note": "старт золотых фьючерсов COMEX — канон школы"},
    "BTC": {"genesis": (2009, 1, 3, 18, 15, 5), "band": 27,
            "note": "генезис-блок Bitcoin — канон первоисточника"},
    "ETH": {"genesis": (2015, 7, 30, 15, 26, 13), "band": 27,
            "note": "генезис-блок Ethereum (30.07.2015 15:26:13 UTC)"},
    "SBER": {"genesis": (2007, 7, 20, 12, 0, 0), "band": 54,
             "note": "старт торгов обыкновенными акциями после сплита — условность 🟡"},
    "MOEX_MUNDANE": {"genesis": (2011, 12, 19, 12, 0, 0), "band": 72,
                     "note": "объединение ММВБ-РТС (Московская биржа) — мунданный якорь 🟡"},
}
USD_GENESIS = (1913, 12, 23, 23, 2, 0)   # закон о ФРС — линейка фиата 🟡
USD_BAND = 72

_BODY_ORDER = ["Солнце", "Луна", "Меркурий", "Венера", "Марс",
               "Юпитер", "Сатурн", "Уран", "Нептун", "Плутон"]


# ══════════════════════════════════════════════════════════════════════
# НЕБЕСНАЯ ЧАСТЬ: транзитные λ_i(t), E_i(t), Ψ(t)  (эфемериды — через astro)
# ══════════════════════════════════════════════════════════════════════
_NATAL_CACHE: dict = {}
_TRANS_CACHE: dict = {}
_TRANS_LOCK = threading.Lock()


def _sf():
    return astro._load_sf()


def w_flux(name: str, d_ap):
    """Вес тела 🔵 W_flux = (albedo_g/albedo_Луны)·d_ap² — фотометрия: поток
    отражённого света ~ альбедо × видимая площадь диска. d_ap — видимый диаметр
    в лунных единицах (θ/THETA_REF_DEG); Луна на среднем расстоянии → 1.
    Солнце — по апертуре (albedo := 1, самосветящееся); фазовый угол опущен —
    прокси назван в блоке констант. Принимает скаляр и numpy-массив."""
    return (ALBEDO_G[name] / ALBEDO_REF) * np.square(d_ap)


def _omega_of(v_deg_day) -> float:
    """ω осциллятора Курамото 🔵: 2π·|dλ/dt|/360 — рад/сут из РЕАЛЬНОЙ угловой
    скорости эфемерид. Станция (v→0) даёт ω→0: замершее тело не крутит фазу
    (v3.9; справочник 2π·W_VEDIC/60 убит — вердикт 05.08.2026)."""
    return 2.0 * math.pi * abs(float(v_deg_day)) / 360.0


def _vel_of(trans: dict) -> dict:
    """Скорости dλ/dt (°/сут) серии. Канонный путь — ключ trans["vel"]
    (его пишет transit_series); для сторонних серий без него — честный
    градиент по trans["lam"]/trans["ts"] (нужно ≥2 точки). Одна точка без
    vel → ValueError: скорости не выдумываются (NO DUMMIES)."""
    if "vel" in trans:
        return trans["vel"]
    ts_d = np.asarray(trans["ts"], float) / 86400.0
    if ts_d.size < 2:
        raise ValueError("нет скоростей: серия из одной точки без ключа vel")
    return {nm: np.gradient(np.degrees(np.unwrap(np.radians(
        np.asarray(trans["lam"][nm], float)))), ts_d)
        for nm in trans["lam"]}


def natal_lons(genesis: tuple) -> list | None:
    """Геоцентрические apparent-долготы 10 тел на момент генезиса 🔵. Кэш."""
    key = tuple(int(v) for v in genesis)
    if key in _NATAL_CACHE:
        return _NATAL_CACHE[key]
    sf = _sf()
    if not sf:
        return None
    t = sf["ts"].utc(*key)
    rows = []
    for nm in _BODY_ORDER:
        bkey = sf["bodies"].get(nm)
        if not bkey:
            return None
        q = sf["earth"].at(t).observe(sf["eph"][bkey]).apparent().frame_latlon(sf["frame"])
        # β не выбрасываем: Ψ стоит на долготах (канон), но широта уже посчитана
        # и обязана дожить до вызывающего — иначе первый, кто захочет назвать
        # созвездие натала инструмента, снова получит плоское небо.
        rows.append({"name": nm,
                     "lon": round(float(q[1].degrees) % 360.0, 2),
                     "lat": round(float(q[0].degrees), 3)})
    _NATAL_CACHE[key] = rows
    return rows


def transit_series(t0: datetime, hours: float, step_h: float) -> dict | None:
    """Транзитные λ_i(t), скорости vel_i(t) и эфирные плотности E_i(t) от t0
    (UTC) на hours вперёд с шагом step_h. ОДИН вызов skyfield на тело 🔵.
    E_i = d_ap·W_flux·min(cap, 1+1/(|dλ/dt|+ε)),
    W_flux = (albedo_g/0.136)·d_ap², d_ap = θ_i/θ_ref (лунные единицы) —
    т.е. E = (albedo_g/0.136)·d_ap³·K_chrono: вес — фотометрия (альбедо ×
    апертура²), а не ступени лесенки (v3.9, вердикт 05.08.2026).
    В out дополнительно ключ "vel" — знаковые dλ/dt, °/сут."""
    key = (t0.replace(second=0, microsecond=0).isoformat(), round(hours, 3),
           round(step_h, 4))
    with _TRANS_LOCK:
        if key in _TRANS_CACHE:
            return _TRANS_CACHE[key]
    sf = _sf()
    if not sf:
        return None
    n = int(round(hours / step_h)) + 1
    hh = t0.hour + t0.minute / 60.0 + np.arange(n, dtype=float) * step_h
    t = sf["ts"].utc(t0.year, t0.month, t0.day, hh)
    dt_days = step_h / 24.0
    ts_unix = np.array([t0.timestamp() + k * step_h * 3600.0 for k in range(n)])
    lam, E, bet, vel = {}, {}, {}, {}
    for nm in _BODY_ORDER:
        bkey = sf["bodies"].get(nm)
        if not bkey:
            return None
        q = sf["earth"].at(t).observe(sf["eph"][bkey]).apparent().frame_latlon(sf["frame"])
        L = np.asarray(q[1].degrees) % 360.0
        theta = np.degrees(2.0 * np.arcsin(
            np.clip(R_IAU_KM[nm] / np.asarray(q[2].km, dtype=float), 0.0, 1.0)))
        v_sign = np.gradient(np.degrees(np.unwrap(np.radians(L))), dt_days)
        v = np.abs(v_sign)
        chrono = np.minimum(CHRONO_CAP, 1.0 + 1.0 / (v + EPS_CHRONO))
        lam[nm] = L
        bet[nm] = np.asarray(q[0].degrees)   # широта β — скалярный слой v3.0
        vel[nm] = v_sign                     # знаковые dλ/dt, °/сут (v3.9)
        d_ap = theta / THETA_REF_DEG
        E[nm] = d_ap * w_flux(nm, d_ap) * chrono
    out = {"ts": ts_unix, "lam": lam, "E": E, "beta": bet, "vel": vel}
    with _TRANS_LOCK:
        if len(_TRANS_CACHE) >= 8:
            for old in sorted(_TRANS_CACHE)[:-7]:
                _TRANS_CACHE.pop(old, None)
        _TRANS_CACHE[key] = out
    return out


CAL_HALF_FRAC = 0.35   # полураспад веса калибровки: 35% окна назад — вес ½


def _wq(vals, weights, q: float) -> float:
    """Взвешенный квантиль (детерминированный): сортировка по значению,
    интерполяция по накопленному весу."""
    v = np.asarray(vals, float)
    wt = np.asarray(weights, float)
    idx = np.argsort(v)
    v, wt = v[idx], wt[idx]
    cw = np.cumsum(wt)
    cw = cw / cw[-1]
    return float(np.interp(q, cw, v))


def _wrap180(x):
    return ((np.asarray(x, dtype=float) + 180.0) % 360.0) - 180.0


def _norm01(x):
    x = np.asarray(x, dtype=float)
    lo, hi = float(x.min()), float(x.max())
    return (x - lo) / (hi - lo) if hi > lo else np.zeros(x.shape)


def psi_of(trans: dict, natal_rows: list, band: int) -> np.ndarray:
    """Ψ(t) = ΣᵢΣⱼ E_i(t)·cos(k_ij·Δλ) / (10·10) → min-max 0..1.
    Формула школы REAL SKY (fin ENGINE 16). v3.7: спектральное сродство
    (буст B=AFFINITY телу ноты band) удалено — подгонка, вердикт мировоззрения
    05.08.2026; вклад тел равноправен, отбор — физикой E (v3.9: вес —
    фотометрия альбедо·апертура², см. transit_series). Параметр band сохранён
    в сигнатуре как МЕТКА школы (подпись генезиса) — веса он не даёт."""
    lam0 = np.array([r["lon"] for r in natal_rows], dtype=float)[None, :]
    psi = np.zeros(len(trans["ts"]))
    for nm in _BODY_ORDER:
        d = np.abs(_wrap180(trans["lam"][nm][:, None] - lam0))
        k = _ASP_K[np.argmin(np.abs(d[:, :, None] - _ASP_ANG[None, None, :]), axis=2)]
        psi += (trans["E"][nm][:, None] * np.cos(np.radians(k * d))).sum(axis=1)
    psi /= float(len(_BODY_ORDER) * lam0.size)
    return _norm01(psi)


# ══════════════════════════════════════════════════════════════════════
# ЦЕНОВАЯ ЧАСТЬ: Гильберт, сцепка, плиты, потенциал
# ══════════════════════════════════════════════════════════════════════

def _analytic(x):
    """z = x_detrended + i·H[x]: дискретный Гильберт (Marple 1999, FFT) 🔵.

    v2.9: РАЗРЫВ ВРЕМЕННОЙ ПЕТЛИ — добивка до 2n, кольцо FFT разомкнуто.
    v3.1 (аудит №3, «краевой эффект»): зеркальная добивка отражала будущее
    как прошлое задом наперёд — наклон фазы у ПРАВОГО края (секунда «сейчас»)
    ломался о складку. Теперь хвост — ЭКСТРАПОЛЯЦИЯ: линейный тренд последних
    баров продолжает сигнал вправо с настоящим локальным наклоном, а к шву
    кольца хвост плавно (полукосинус) приводится к левому краю — стыка нет.
    Правый край чист от звона Гиббса, левый защищён как раньше."""
    x = np.asarray(x, dtype=float)
    n = x.size
    xd = x - x.mean()
    ext = None
    if n >= 16:
        # AR(4) по последним барам: модель продолжает КОЛЕБАНИЕ (для тона —
        # точно), а не рисует рампу; расходится/взрывается → честный откат
        # на зеркало (гвард ниже)
        L = min(64, max(12, n // 2))
        seg = xd[-L:]
        p_ar = 4
        Y = seg[p_ar:]
        Xm = np.column_stack([seg[p_ar - 1 - j:L - 1 - j] for j in range(p_ar)])
        try:
            coef = np.linalg.lstsq(Xm, Y, rcond=None)[0]
            buf = list(seg[-p_ar:][::-1])           # [x[-1], x[-2], ...]
            lim = 5.0 * (float(np.max(np.abs(xd))) + 1e-12)
            tail_ar = np.empty(n)
            ok_ar = True
            for k in range(n):
                v = float(np.dot(coef, buf))
                if not math.isfinite(v) or abs(v) > lim:
                    ok_ar = False                   # AR разошёлся — без
                    break                           # переполнений, сразу откат
                tail_ar[k] = v
                buf = [v] + buf[:p_ar - 1]
            if ok_ar:
                # шов кольца: кроссфейд AR-хвоста в зеркало — у правого края
                # фаза продолжается честно (AR), к шву хвост становится
                # зеркалом (спектрально родным, кольцо замкнуто без скачка)
                kk = np.arange(1, n + 1, dtype=float)
                u = (1.0 - np.cos(np.pi * kk / n)) / 2.0
                ext = np.concatenate([xd, tail_ar * (1.0 - u) + xd[::-1] * u])
        except Exception:
            ext = None
    if ext is None:
        ext = np.concatenate([xd, xd[::-1]]) if n >= 4 else xd
    m = ext.size
    X = np.fft.fft(ext)
    h = np.zeros(m)
    if m % 2 == 0:
        h[0] = h[m // 2] = 1.0
        h[1:m // 2] = 2.0
    else:
        h[0] = 1.0
        h[1:(m + 1) // 2] = 2.0
    return np.fft.ifft(X * h)[:n]


def plates_of(closes) -> dict | None:
    """Плиты уровня: квантили 10/50/90 реальной цены (тип 7) + края окна 🔵."""
    c = np.asarray([v for v in closes if v is not None], dtype=float)
    if c.size < 8:
        return None
    q = [float(np.percentile(c, p)) for p in Q_PLATES]
    return {"floor": q[0], "mid": q[1], "ceil": q[2],
            "lo": float(c.min()), "hi": float(c.max()), "n": int(c.size)}


def _plv(ph_a, ph_b, edge):
    """PLV = |⟨e^{iΔφ}⟩| по ядру ряда (края Гильберта исключены) 🔵."""
    dphi = ph_a - ph_b
    core = dphi[edge:len(dphi) - edge] if len(dphi) > 2 * edge + 4 else dphi
    return float(np.abs(np.exp(1j * core).mean())), float(np.cos(dphi[-edge - 1]
                 if len(dphi) > edge + 1 else dphi[-1]))


def _bandpass_like(x, ref):
    """Бедросян-фильтр (аудит №3) 🔵: Гильберт честен только на узкополосном
    сигнале, а сырая цена — широкополосный фрактальный шум (фаза-мусор).
    Пропускаем цену через полосу ±1 октава вокруг ДОМИНАНТЫ эфирной волны ref:
    фаза цены сравнивается с эфиром на его же частоте. Не вышло определить
    доминанту — честно возвращаем исходный ряд (None-политика без подмен)."""
    x = np.asarray(x, float)
    r = np.asarray(ref, float)
    n = x.size
    if n < 24 or r.size != n:
        return x
    R = np.abs(np.fft.rfft(r - r.mean()))
    if R.size < 4:
        return x
    f0 = int(np.argmax(R[1:])) + 1          # доминанта волны (без DC)
    if f0 < 1:
        return x
    lo_b, hi_b = max(1, f0 // 2), min(R.size - 1, f0 * 2)
    X = np.fft.rfft(x - x.mean())
    mask = np.zeros(X.size)
    mask[lo_b:hi_b + 1] = 1.0
    return np.fft.irfft(X * mask, n)


def _plv_percentile(w, ph_price, edge, plv_real, nshift: int = 48):
    """Перцентиль настоящего PLV в нуле циклических сдвигов эфира 🔵.

    Нуль сохраняет спектр Ψ полностью и рвёт только временную привязку —
    ровно то, что нужно, чтобы отделить сцепку от общей гладкости.
    Сдвигов немного (48): это идёт в живом ответе, а не в стенде; полный
    нуль на 239 сдвигов + фазовые суррогаты живёт в backend/lab/plv_null.py.
    """
    w = np.asarray(w, float)
    n = w.size
    # Порог был 32 — и это дыра: на ДНЕВНОМ масштабе окно как раз n=30, то
    # есть проверка молчала ровно там, где число PLV самое громкое (0.957)
    # и самое незаслуженное. Нуль обязан считаться и на коротком окне —
    # он там даже нужнее: на 30 точках высокий PLV получается почти всегда.
    if n < 16:
        return None
    vals = []
    for k in range(1, nshift + 1):
        sh = int(k * n / (nshift + 1))
        try:
            ph_s = np.unwrap(np.angle(_analytic(np.roll(w, sh))))
            v, _ = _plv(ph_s, ph_price, edge)
        except Exception:
            continue
        if v == v:
            vals.append(float(v))
    if len(vals) < 16:
        return None
    return round(float((np.asarray(vals) < plv_real).mean() * 100.0), 1)


def couple_wave_price(psi_ts, psi_vals, price_ts, price_close) -> dict | None:
    """«Эфир обновляется от цены»: Ψ пересемплируется на сетку свечей, обе
    линии — в аналитический сигнал, сцепка PLV + мгновенная синфазность +
    аффинная калибровка эфира в шкалу цены (min-max по общему окну) 🔵.
    Мало точек → None (NO DUMMIES)."""
    pt = np.asarray(price_ts, dtype=float)
    pc = np.asarray(price_close, dtype=float)
    ok = np.isfinite(pc)
    pt, pc = pt[ok], pc[ok]
    if pt.size < 16:
        return None
    lo, hi = max(float(psi_ts[0]), float(pt[0])), min(float(psi_ts[-1]), float(pt[-1]))
    m = (pt >= lo) & (pt <= hi)
    if m.sum() < 16:
        return None
    pt, pc = pt[m], pc[m]
    w = np.interp(pt, psi_ts, psi_vals)
    edge = max(3, len(pt) // EDGE_FRAC)
    # ДВА фазовых конвейера (стенд v3.1.1, 120 якорей × 4 инструмента):
    # · полоса Бедросяна — для PLV/W: узкополосная фаза честна для метрик
    #   волновой компоненты (аудит №3);
    # · СЫРАЯ фаза — для зеркала: полоса вырезает трендовую компоненту, по
    #   которой связь и судится, и рвёт знак m_inst (на BRU6 полосная фаза
    #   давала +0.18 при сырой −0.95 — зеркало ложно слетало, проекция
    #   переворачивалась на рост). Зеркало судит вся связь, не полоса.
    pc_nb = _bandpass_like(pc, w)              # Бедросян: полоса волны
    ph_p = np.unwrap(np.angle(_analytic(pc_nb)))
    ph_w = np.unwrap(np.angle(_analytic(w)))
    plv, sync_now = _plv(ph_w, ph_p, edge)
    dphi = ph_p - ph_w
    core = dphi[edge:len(dphi) - edge] if len(dphi) > 2 * edge + 4 else dphi
    # мгновенное зеркало: косинус разности СЫРЫХ фаз, сглаженный за
    # M_SMOOTH баров ядра — переворот виден в ту же свечу, без лага corr
    ph_p_raw = np.unwrap(np.angle(_analytic(pc)))
    d_raw = ph_p_raw - ph_w
    core_raw = d_raw[edge:len(d_raw) - edge] if len(d_raw) > 2 * edge + 4 else d_raw
    m_inst = (float(np.mean(np.cos(core_raw[-M_SMOOTH:])))
              if core_raw.size >= M_SMOOTH else None)
    # индекс обмотки W (аудит №3): накрут развёрнутых фаз за окно, в оборотах.
    # PLV слеп к сдвигу на полный круг; W видит скрученную пружину: цена
    # отстала от эфира на целые циклы → разрядка обязана отмотать их назад
    winding = float((core[-1] - core[0]) / (2.0 * np.pi)) if core.size >= 8 else None
    # v2.9: калибровка ДЫШИТ НАСТОЯЩИМ — взвешенные квантили 5/95 с
    # затуханием веса в прошлое (полураспад CAL_HALF_FRAC окна): аномальный
    # шпиль месячной давности больше не держит масштаб проекции в заложниках
    age = (pt[-1] - pt) / max(float(pt[-1] - pt[0]), 1e-9)
    wgt = 0.5 ** (age / CAL_HALF_FRAC)
    p_lo, p_hi = _wq(pc, wgt, 0.05), _wq(pc, wgt, 0.95)
    w_lo, w_hi = _wq(w, wgt, 0.05), _wq(w, wgt, 0.95)
    # пол ширины окна волны (веер v3.0): плоская свежая волна давала
    # w_hi−w_lo→0 и масштаб a взрывался — раздуваем симметрично до 0.10
    if w_hi - w_lo < 0.10:
        mid_w = (w_hi + w_lo) / 2.0
        w_lo, w_hi = mid_w - 0.05, mid_w + 0.05
    if w_hi - w_lo < 1e-12:
        a, b = 0.0, (p_lo + p_hi) / 2.0
    else:
        a = (p_hi - p_lo) / (w_hi - w_lo)
        b = p_lo - a * w_lo
    # ── ЧЕСТНЫЙ НУЛЬ ДЛЯ PLV (стенд backend/lab/plv_null.py) ──
    # Голое число PLV не значит ничего: два гладких узкополосных сигнала дают
    # высокий PLV почти всегда. Измерено на 4 инструментах × 4 нуля (сдвиг Ψ,
    # фазовый суррогат, чужой эфир, гладкий шум): медиана нуля 0.35-0.77, а
    # p95 доходит до 0.95. Настоящий PLV НИ У ОДНОГО инструмента не превысил
    # свой p95 (лучшее — Сбербанк 94-й перцентиль), у Газпрома оказался ХУЖЕ
    # нуля. Поэтому слово «сцеплены» теперь заслуживается перцентилем, а не
    # порогом 0.7: считаем нуль циклическими сдвигами прямо здесь 🔵.
    plv_p = _plv_percentile(w, ph_p, edge, plv)
    short = " · окно короткое (n={}), на таком PLV высок почти всегда 🟡".format(
        int(np.asarray(w).size)) if np.asarray(w).size < 64 else ""
    if plv_p is not None and plv_p >= 95.0:
        lock = (f"фазы сцеплены — PLV {plv:.3f} выше {plv_p:.0f}% своего нуля "
                f"(циклические сдвиги эфира), это НЕ артефакт гладкости{short}")
    elif plv_p is not None:
        lock = (f"PLV {plv:.3f}, но нуль даёт столько же: перцентиль {plv_p:.0f} "
                "— в этом окне сцепка НЕ доказана, число декоративно 🟡")
    elif plv >= PLV_LOCK:
        lock = "фазы сцеплены — эфир дышит вместе с ценой в этом окне"
    elif plv >= 0.4:
        lock = "сцепка частичная — эфир и цена то в ногу, то врозь"
    else:
        lock = "фазы свободны — в этом окне эфир шёл своим ходом (честно)"
    return {"plv": round(plv, 3), "sync_now": round(sync_now, 3),
            "m_inst": (round(m_inst, 3) if m_inst is not None else None),
            "winding": (round(winding, 2) if winding is not None else None),
            "lock": lock, "n": int(pt.size), "plv_pct": plv_p,
            "affine_a": a, "affine_b": b,
            "w_lo": w_lo, "w_hi": w_hi,   # окно калибровки: зеркало флипает В НЁМ
            "corr": round(float(np.corrcoef(w, pc)[0, 1]), 3) if pt.size > 3 else None,
            "mirror": bool(pt.size > 3 and float(np.corrcoef(w, pc)[0, 1]) < -0.3)}


def potential_of(psi_fwd, couple, plates, now_price) -> dict | None:
    """«Где потенциал реализован, где жив»: проекция волны вперёд в шкале цены
    (аффинная калибровка окна; зеркальная сцепка честно переворачивает знак),
    ширина неопределённости = (1−PLV)·полокна 🟡. Сравнение с плитами 🔵."""
    if couple is None or plates is None or now_price is None:
        return None
    a, b = couple["affine_a"], couple["affine_b"]
    w = np.asarray(psi_fwd, dtype=float)
    if couple.get("mirror"):
        # честный флип — вокруг середины ОКНА КАЛИБРОВКИ [w_lo,w_hi], не полной
        # шкалы: флип 1−w при асимметричном окне сдвигал проекцию на
        # a·(1−w_hi−w_lo) и мог целиком увести цель за пределы виденных цен
        w = couple.get("w_lo", 0.0) + couple.get("w_hi", 1.0) - w
    center = a * w + b
    half = (1.0 - couple["plv"]) * 0.5 * (plates["hi"] - plates["lo"])
    up_target = float(center.max())
    dn_target = float(center.min())
    up_room_wave = up_target - float(now_price)
    dn_room_wave = float(now_price) - dn_target
    span = max(plates["hi"] - plates["lo"], 1e-9)
    live_up = max(0.0, min(1.0, up_room_wave / span))
    live_dn = max(0.0, min(1.0, dn_room_wave / span))
    def _word(x):
        if x >= 0.5:
            return "потенциал жив (большая часть хода впереди)"
        if x >= 0.15:
            return "потенциал частично реализован"
        return "потенциал в основном реализован ценой"
    return {"center": center, "half_band": half,
            "up_target": round(up_target, 4), "dn_target": round(dn_target, 4),
            "live_up": round(live_up, 2), "live_dn": round(live_dn, 2),
            "word_up": _word(live_up), "word_dn": _word(live_dn),
            "mirrored": bool(couple.get("mirror"))}


# ══════════════════════════════════════════════════════════════════════
# СКАЛЬП-МАШИНЕРИЯ (перенос проходов 8/11 и фин-ядра REAL SKY)
# ══════════════════════════════════════════════════════════════════════
# импеданс угла (chrono_tok REAL SKY): Z(θ)=max(0, 1−Σaₖcos kθ); калибровка
# журнала: Z(0)=0, 60°→0.91, 90°→1.30, 120°→0.69, 180°→1.06, max≈1.57 у 152°
_AK = {1: 0.30, 2: 0.10, 3: 0.26, 4: 0.10, 6: 0.30}
Z_FLOOR = 0.10
# Курамото неба (quantum_kontur №22 / fin_futures): детерминированный Эйлер
KUR_STEPS, KUR_DT, KUR_K = 600, 0.05, 0.9
# инерция зоны Дирака 🔵 (v3.9, кинематика вместо нот band): σ — время, за
# которое пара проходит полуширину орба DIRAC_ORB_DEG своей относительной
# скоростью; клампы 15..120 мин — шкала школы 🟡 (_SIGMA_BY_BAND убит)
DIRAC_ORB_DEG = 0.05     # полуширина точного угла, °
SIGMA_MIN_FLOOR = 15.0   # мин — быстрые пары не схлопывают зону в ноль
SIGMA_MIN_CAP = 120.0    # мин — медленные пары не растягивают зону в сутки
_GLYPH = {"Солнце": "☉", "Луна": "☽", "Меркурий": "☿", "Венера": "♀",
          "Марс": "♂", "Юпитер": "♃", "Сатурн": "♄", "Уран": "♅",
          "Нептун": "♆", "Плутон": "♇"}
_ASP_NAME = {0.0: "соединение", 60.0: "секстиль", 90.0: "квадрат",
             120.0: "трин", 180.0: "оппозиция"}


def impedance(theta) -> float:
    """Z(θ): 0 — сверхпроводник (точный угол), 1 — номинал, выше — нагрев."""
    z = 1.0
    for k, a in _AK.items():
        z -= a * math.cos(math.radians(k * float(theta)))
    return max(0.0, z)


def kuramoto_r(trans: dict, k_idx: int) -> float:
    """Когерентность сегодняшнего неба r∈0..1 (детерминированный Курамото:
    фазы — транзитные λ, ω = 2π·|dλ/dt|/360 — рад/сут из РЕАЛЬНЫХ угловых
    скоростей эфемерид; станция → ω≈0, замершее тело не крутит фазу; БЕЗ
    random). v3.9: справочник 2π·W_VEDIC/60 убит — вердикт 05.08.2026."""
    th = np.radians(np.array([trans["lam"][nm][k_idx] for nm in _BODY_ORDER]))
    vel = _vel_of(trans)
    om = np.array([_omega_of(vel[nm][k_idx]) for nm in _BODY_ORDER])
    for _ in range(KUR_STEPS):
        th = th + KUR_DT * (om + KUR_K * np.mean(
            np.sin(th[None, :] - th[:, None]), axis=1))
    return float(np.abs(np.mean(np.exp(1j * th))))


def dirac_windows(trans: dict, band: int, top: int = 10) -> list:
    """Точки Дирака окна: точные углы пар транзит×транзит (0/60/90/120/180°).
    Момент — линейная интерполяция нуля отклонения на сетке (≈минуты);
    зона ± КИНЕМАТИКА 🔵 (v3.9): σ = clamp(15 мин, ORB/|dθ_rel/dt|, 120 мин) —
    время, за которое пара своей относительной скоростью проходит полуширину
    орба 0.05°. Инерция по ноте band убита (вердикт 05.08.2026); параметр band
    сохранён в сигнатуре как метка школы — на ширину зоны не влияет.
    Скальп-тайминг: узлы времени, где геометрия неба щёлкает точно."""
    ts = trans["ts"]
    out = []
    names = _BODY_ORDER
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            sep = np.abs(_wrap180(trans["lam"][names[i]] - trans["lam"][names[j]]))
            for ang, aname in _ASP_NAME.items():
                d = sep - ang
                sgn = np.sign(d)
                seen_here = False              # одна запись на пару×аспект в окне
                for k in range(1, len(ts)):
                    if seen_here:
                        break
                    # ноль сетки = точный угол; берём только НАЧАЛО нулевой серии
                    hit_exact = (sgn[k - 1] == 0 and (k - 1 == 0 or sgn[k - 2] != 0))
                    if not hit_exact and (sgn[k - 1] == 0 or sgn[k] == sgn[k - 1]):
                        continue
                    if abs(d[k - 1]) > 3.0:        # далёкий разрыв — не аспект
                        continue
                    if hit_exact:
                        t_ex = float(ts[k - 1])
                    else:
                        frac = abs(d[k - 1]) / (abs(d[k - 1]) + abs(d[k]) + 1e-12)
                        t_ex = float(ts[k - 1] + frac * (ts[k] - ts[k - 1]))
                    seen_here = True
                    # σ по кинематике: относительная скорость пары у узла
                    dt_pair_d = max((float(ts[k]) - float(ts[k - 1])) / 86400.0,
                                    1e-12)
                    rate = abs(float(d[k]) - float(d[k - 1])) / dt_pair_d  # °/сут
                    sigma = (DIRAC_ORB_DEG / rate * 1440.0
                             if rate > 1e-12 else SIGMA_MIN_CAP)
                    out.append({
                        "t_exact": t_ex,
                        "pair": f"{_GLYPH[names[i]]}{_GLYPH[names[j]]}",
                        "names": f"{names[i]}–{names[j]}",
                        "aspect": aname,
                        "sigma_min": int(round(min(max(sigma, SIGMA_MIN_FLOOR),
                                                   SIGMA_MIN_CAP))),
                    })
    out.sort(key=lambda x: x["t_exact"])
    return out[:top]


def adler_capture(trans: dict, natal_rows: list, k_idx: int) -> dict:
    """«Захват дня» (проход 11 REAL SKY, Адлер 1946/Арнольд): каждый транзитный
    голос против хора натала генезиса. Голоса — РЕАЛЬНЫЕ угловые скорости
    |dλ/dt| (°/сут) 🔵 (v3.9: лесенка W_vedic как источник частот убита —
    вердикт 05.08.2026); хватка — проводимости 1/Z(θ) углов к наталу против
    средней парной проводимости натала; язык Арнольда ε = Δ·хватка; в языке —
    угол упора, вне — биение."""
    vel = _vel_of(trans)
    f = {nm: abs(float(vel[nm][k_idx])) for nm in _BODY_ORDER}
    f_bar = float(np.mean(list(f.values())))
    delta = float(np.median([abs(v - f_bar) for v in f.values()]))
    lam0 = {r["name"]: r["lon"] for r in natal_rows}
    # внутренняя ткань натала: средняя парная проводимость
    g_pairs = []
    ns = list(lam0)
    for i in range(len(ns)):
        for j in range(i + 1, len(ns)):
            th = abs(_wrap180(lam0[ns[i]] - lam0[ns[j]]))
            g_pairs.append(1.0 / max(impedance(th), Z_FLOOR))
    g_nom = float(np.mean(g_pairs)) or 1e-9
    rows = []
    for nm in _BODY_ORDER:
        lt = float(trans["lam"][nm][k_idx])
        gs = [1.0 / max(impedance(abs(_wrap180(lt - lam0[j]))), Z_FLOOR)
              for j in ns]
        grip = float(np.mean(gs)) / g_nom
        eps = delta * grip
        df = f[nm] - f_bar
        row = {"name": nm, "glyph": _GLYPH[nm], "grip": round(grip, 2),
               "eps": round(eps, 1)}
        if eps > 1e-9 and abs(df) <= eps:
            row["captured"] = True
            row["psi_deg"] = round(math.degrees(math.asin(
                max(-1.0, min(1.0, df / eps)))), 1)
        else:
            row["captured"] = False
            beat = math.sqrt(max(df * df - eps * eps, 1e-12))
            row["beat"] = round(beat, 1)
        rows.append(row)
    rows.sort(key=lambda r: (-r["grip"], r["name"]))
    cap = [r for r in rows if r["captured"]]
    return {"rows": rows[:6], "captured": [r["name"] for r in cap],
            "word": (f"хор генезиса сегодня держат: "
                     + ", ".join(f"{r['glyph']}{r['name']}" for r in cap[:4])
                     if cap else
                     "ни один голос дня не в языке захвата — контракт держит "
                     "свой ритм")}


def price_pulse(price_ts, price_close) -> dict | None:
    """Пульс цены (перенос прохода 8 🔵): огибающая аналитического сигнала,
    прилив/отлив (квантили 30/70), рывок dA/dt и d²A/dt², «сжатая пружина» =
    сильный рывок в застое (отлив × почти стоящая фаза). Чистая обработка
    сигнала на РЕАЛЬНЫХ свечах."""
    ts, cl = np.asarray(price_ts, float), np.asarray(price_close, float)
    if cl.size < 24:
        return None
    z = _analytic(cl)
    env = np.abs(z)
    ph = np.unwrap(np.angle(z))
    edge = max(3, cl.size // EDGE_FRAC)
    core = slice(edge, cl.size - edge)
    q30, q70 = (float(np.percentile(env[core], q)) for q in (30, 70))
    dt = float(np.median(np.diff(ts))) / 3600.0 or 1.0     # часы
    speed = np.gradient(env, dt)
    accel = np.gradient(speed, dt)
    phase_rate = np.abs(np.gradient(ph, dt))
    standing = float(np.percentile(phase_rate[core], 30))
    k = cl.size - edge - 1                                  # «сейчас» вне края
    ebb = env[k] <= q30
    still = phase_rate[k] <= standing
    q80a = float(np.percentile(np.abs(accel[core]), 80))
    spring = bool(ebb and still and abs(accel[k]) >= q80a)
    state = ("прилив энергии" if env[k] >= q70 else
             "отлив (застой)" if ebb else "средняя вода")
    word = state + (
        " · СЖАТАЯ ПРУЖИНА: рывок зреет в тишине" if spring else
        (" · фаза почти стоит" if still and ebb else ""))
    return {"env_now": round(float(env[k]), 4), "state": state,
            "speed": round(float(speed[k]), 4),
            "accel": round(float(accel[k]), 4),
            "spring": spring, "word": word}


def rolling_plv(psi_ts, psi_vals, price_ts, price_close, win: int = 24) -> dict | None:
    """Скользящая сцепка эфир↔цена: PLV в окне win точек — «жива ли связь
    ПРЯМО СЕЙЧАС» и куда дышит (растёт/падает против своей медианы)."""
    pt = np.asarray(price_ts, float)
    pc = np.asarray(price_close, float)
    ok = np.isfinite(pc)
    pt, pc = pt[ok], pc[ok]
    if pt.size < win + 8:
        return None
    lo, hi = max(float(psi_ts[0]), float(pt[0])), min(float(psi_ts[-1]), float(pt[-1]))
    m = (pt >= lo) & (pt <= hi)
    if m.sum() < win + 8:
        return None
    pt, pc = pt[m], pc[m]
    w = np.interp(pt, psi_ts, psi_vals)
    dphi = (np.unwrap(np.angle(_analytic(w)))
            - np.unwrap(np.angle(_analytic(pc))))
    vals = [float(np.abs(np.exp(1j * dphi[i - win:i]).mean()))
            for i in range(win, len(dphi) + 1)]
    now, med = vals[-1], float(np.median(vals))
    return {"now": round(now, 3), "median": round(med, 3),
            "trend": ("сцепка крепнет" if now > med + 0.05 else
                      "сцепка слабеет" if now < med - 0.05 else "сцепка ровная"),
            "curve_tail": [round(v, 3) for v in vals[-12:]]}


# ══════════════════════════════════════════════════════════════════════
# ЭНЕРГИЯ, ТОЛПА, ЭФИРНЫЕ ПЛИТЫ (v2.7: скальп в реальном времени)
# ══════════════════════════════════════════════════════════════════════
OMEGA0 = 6.0     # ω₀ вейвлета Морле (канон Торренса–Компо, табл. 1)
CWT_DJ = 0.5     # шаг шкал s_j = s₀·2^{j·δj}


def morlet_cwt(x, dt):
    """CWT Морле по рецепту Torrence & Compo 1998 (BAMS 79): FFT-свёртка
    (ур. 4), нормировка (2πs/δt)^½ (ур. 6), шкалы s₀=2δt·2^{j·δj} (ур. 9-10),
    период λ=4πs/(ω₀+√(2+ω₀²)), конус влияния √2·s; мощность ВЫПРЯМЛЕНА
    делением на шкалу (Liu–Liang–Weisberg 2007). Перенос из REAL SKY 🔵."""
    x = np.asarray(x, float)
    n = x.size
    xh = np.fft.fft(x - x.mean())
    omega = 2.0 * np.pi * np.fft.fftfreq(n, d=dt)
    s0 = 2.0 * dt
    J = max(1, int(math.log2(n * dt / s0) / CWT_DJ))
    scales = s0 * 2.0 ** (CWT_DJ * np.arange(J + 1))
    W = np.empty((scales.size, n), complex)
    for i, s in enumerate(scales):
        psi_hat = (math.pi ** -0.25 * math.sqrt(2.0 * math.pi * s / dt)
                   * np.exp(-0.5 * (s * omega - OMEGA0) ** 2) * (omega > 0))
        W[i] = np.fft.ifft(xh * np.conj(psi_hat))
    lam = 4.0 * math.pi / (OMEGA0 + math.sqrt(2.0 + OMEGA0 ** 2))
    periods = scales * lam
    power = (np.abs(W) ** 2) / scales[:, None]
    t_edge = np.minimum(np.arange(n), np.arange(n)[::-1]) * dt
    # КАНОН Торренса-Компо: e-folding √2·s по ВРЕМЕНИ ⇒ доверен период
    # P ≤ λ·d/√2 ≈ 0.73·d (веер нашёл: родительский порт REAL SKY доверял
    # краю вдвое шире — √2·d·λ; для живого правого края канон обязателен)
    coi_period = t_edge * lam / math.sqrt(2.0)
    return periods, power, coi_period


def energy_pulse(price_ts, price_close, step_min: float) -> dict | None:
    """«Энергия бьёт ключом / энергии нет» — в реальном времени, на каждый бар:
    выпрямленная мощность CWT в конусе доверия, ранжированная против
    собственного распределения окна (0..1). Правый край честен: доверенных
    шкал у свежих баров меньше — trust_frac показывает, чему верим 🔵."""
    ts, cl = np.asarray(price_ts, float), np.asarray(price_close, float)
    if cl.size < 48:
        return None
    dt_h = step_min / 60.0
    periods, power, coi = morlet_cwt(cl, dt_h)          # периоды в часах
    trust = periods[:, None] <= np.maximum(coi[None, :], periods[0])
    p_tr = np.where(trust, power, 0.0)
    e = p_tr.sum(axis=0)
    n_tr = trust.sum(axis=0)
    e = e / np.maximum(n_tr, 1)                          # средняя доверенная
    e_max = float(np.max(e))
    if e_max <= 0.0 or float(np.std(e)) < 1e-15:
        return None       # мёртвое окно: распределения нет — ранг был бы ложью
    # ранг 0..1: средний ранг при связках (двойной argsort на плоских участках
    # раздавал связкам ранги по порядку индекса — мёртвая сессия «била ключом»)
    uq, inv, cnt = np.unique(e, return_inverse=True, return_counts=True)
    start = np.concatenate(([0.0], np.cumsum(cnt)[:-1].astype(float)))
    order = (start + (cnt - 1) / 2.0)[inv]
    e01 = order / max(len(e) - 1, 1)
    # пол шума: утечка FFT на плоских кусках — тишина, а не ранжируемый сигнал
    e01 = np.where(e < E_FLOOR_FRAC * e_max, 0.0, e01)
    # доминирующий период ОКНА: средняя доверенная мощность шкалы (конус
    # правого края честно слеп к медленным — потому среднее по доверенной
    # области, и только шкалы с ≥2 циклами в окне)
    k = len(e) - 1
    span_h = float(ts[-1] - ts[0]) / 3600.0
    p_avg = np.array([float(power[i, trust[i]].mean()) if trust[i].any() else -1.0
                      for i in range(len(periods))])
    p_avg[periods > span_h / 2.0] = -1.0
    dom_h = float(periods[int(np.argmax(p_avg))]) if p_avg.max() > 0 else None
    terc = np.percentile(periods, [33, 66])
    def _band(p):
        return 0 if p <= terc[0] else (1 if p <= terc[1] else 2)
    bands = []
    for j in range(len(e)):
        cj = power[:, j].copy(); cj[~trust[:, j]] = -1.0
        bands.append(_band(float(periods[int(np.argmax(cj))]))
                     if cj.max() > 0 else 1)
    now = float(e01[k])
    # ПУЧНОСТИ ЭНЕРГИИ: только ПОДТВЕРЖДЁННЫЕ локальные максимумы ранга ≥0.7.
    # Правый край не пучность: он ещё не подтверждён соседом справа и «ехал»
    # с каждым обновлением (маркер вечно сидел на кончике — убрано v2.7.3);
    # текущую энергию честно несут now/word и чип ⚡ терминала.
    anti = [[int(ts[j]), round(float(e01[j]), 2)]
            for j in range(1, len(e01) - 1)
            if e01[j] >= 0.7 and e01[j] >= e01[j - 1] and e01[j] > e01[j + 1]]
    if now >= 0.7:
        word = "энергия бьёт ключом — ход питается глубиной"
    elif now <= 0.3:
        word = "энергии нет — ход без топлива (типичный фон отката)"
    else:
        word = "энергия средняя"
    return {"series": [[int(t), round(float(v), 3), int(b)]
                       for t, v, b in zip(ts, e01, bands)],
            "antinodes": anti[-8:],
            "now": round(now, 3), "word": word,
            "dominant_min": (round(dom_h * 60.0) if dom_h else None),
            "half_swing_min": (round(dom_h * 30.0) if dom_h else None),
            "trust_frac": round(float(n_tr[k]) / len(periods), 2),
            "dominant_scope": "окно целиком (конус правого края честен)",
            "note": ("CWT Морле ω₀=6 (Торренс–Компо 1998, выпрямление Liu "
                     "2007) на РЕАЛЬНОЙ цене 🔵; ранг против своего окна; "
                     "у правого края доверенных глубин меньше (конус) — "
                     "trust_frac говорит, чему верим")}


def volume_profile(rows, bins: int = 40) -> dict | None:
    """Профиль объёма (толпа 🔵, Стейдлмайер): объём бара размазан по его
    диапазону H..L; POC — самый торгуемый уровень, зона ценности 70%."""
    hs, ls, vs = [], [], []
    for r in (rows or []):
        try:
            if isinstance(r, dict):
                h, l, v = float(r["h"]), float(r["l"]), float(r.get("v") or 0)
            else:
                h, l, v = float(r[2]), float(r[3]), float(r[5] if len(r) > 5 else 0)
            if v > 0 and h >= l:
                hs.append(h); ls.append(l); vs.append(v)
        except Exception:
            continue
    if len(vs) < 12:
        return None
    lo, hi = min(ls), max(hs)
    if hi - lo < 1e-9:
        return None
    edges = np.linspace(lo, hi, bins + 1)
    prof = np.zeros(bins)
    for h, l, v in zip(hs, ls, vs):
        a = np.searchsorted(edges, l, "right") - 1
        b = np.searchsorted(edges, h, "left")
        a, b = max(0, a), min(bins - 1, b)
        prof[a:b + 1] += v / (b - a + 1)
    poc_i = int(np.argmax(prof))
    total = prof.sum()
    inc = {poc_i}
    lo_i = hi_i = poc_i
    while prof[list(inc)].sum() < 0.70 * total and (lo_i > 0 or hi_i < bins - 1):
        cand = []
        if lo_i > 0:
            cand.append((prof[lo_i - 1], lo_i - 1))
        if hi_i < bins - 1:
            cand.append((prof[hi_i + 1], hi_i + 1))
        _, j = max(cand)
        inc.add(j)
        lo_i, hi_i = min(lo_i, j), max(hi_i, j)
    mid = lambda i: float((edges[i] + edges[i + 1]) / 2.0)
    return {"poc": round(mid(poc_i), 4), "val": round(mid(lo_i), 4),
            "vah": round(mid(hi_i), 4),
            "note": "профиль объёма: POC и зона ценности 70% — где стояла толпа 🔵"}


def aether_plates(w_ts, w_psi, price_ts, price_close, top: int = 4) -> list:
    """ЭФИРНЫЕ ПЛИТЫ: уровни цены, на которых волна ставила свои гребни и
    впадины в этом окне. Гребень/впадина Ψ — насыщение фазы школы: цена этих
    минут — уровень, где «пройти сложно» 🟡. Вес = глубина экстремума волны."""
    pt = np.asarray(price_ts, float)
    pc = np.asarray(price_close, float)
    ok = np.isfinite(pc)
    pt, pc = pt[ok], pc[ok]
    if pt.size < 24:
        return []
    w = np.interp(pt, w_ts, w_psi)
    d = np.diff(w)
    marks = []
    for i in range(1, len(w) - 1):
        if d[i - 1] == 0 or d[i - 1] * d[i] > 0:
            continue
        marks.append((float(pc[i]), abs(float(w[i]) - 0.5) * 2.0))
    if not marks:
        return []
    tol = (float(pc.max()) - float(pc.min())) / 40.0 or 1e-9
    marks.sort()
    clusters = []
    for p, wgt in marks:
        if clusters and p - clusters[-1]["ps"][-1] <= tol:
            clusters[-1]["ps"].append(p)
            clusters[-1]["w"] += wgt
        else:
            clusters.append({"ps": [p], "w": wgt})
    out = [{"price": round(float(np.mean(c["ps"])), 4),
            "weight": round(c["w"], 2), "hits": len(c["ps"])}
           for c in clusters]
    out.sort(key=lambda x: -x["weight"])
    return out[:top]


def micro_plates(w_ts, w_psi, price_ts, price_close,
                 now_price: float | None = None, top: int = 6,
                 extra_marks: list | None = None) -> list:
    """МИКРО-ПЛИТЫ СКАЛЬПА: внутридневные уровни из трёх честных источников —
    1) цена минут гребней/впадин ТОНКОЙ волны (вес = амплитуда 🟡);
    2) цена минут узлов Ψ=0.5 (переход фазы; вес = порог пучности 🟡);
    3) extra_marks [(price, weight, ts)] — напр., минуты пучностей энергии 🔵.
    Сетка кластера вдвое мельче aether_plates (диапазон/80). Всё умножено на
    свежесть (полураспад MICRO_HALF_LIFE_H): плита, поставленная час назад,
    тяжелее вчерашней — уровни переезжают вместе с ценой."""
    pt = np.asarray(price_ts, float)
    pc = np.asarray(price_close, float)
    ok = np.isfinite(pc)
    pt, pc = pt[ok], pc[ok]
    if pt.size < 24:
        return []
    w = np.interp(pt, w_ts, w_psi)
    d = np.diff(w)
    t_last = float(pt[-1])
    fresh = lambda tm: 0.5 ** ((t_last - tm) / (MICRO_HALF_LIFE_H * 3600.0))
    marks = []
    for i in range(1, len(w) - 1):        # гребни/впадины волны
        if d[i - 1] == 0 or d[i - 1] * d[i] > 0:
            continue
        marks.append((float(pc[i]),
                      abs(float(w[i]) - 0.5) * 2.0 * fresh(float(pt[i])),
                      float(pt[i])))
    last_node = -10
    for i in range(len(w) - 1):           # узлы: Ψ проходит середину
        if w[i] == w[i + 1] or (w[i] - 0.5) * (w[i + 1] - 0.5) > 0:
            continue
        if i - last_node == 1 and w[i] == 0.5:
            last_node = i                 # точное попадание в сетку — тот же узел
            continue
        last_node = i
        marks.append((float(pc[i]), LOUD_THRESHOLD * fresh(float(pt[i])),
                      float(pt[i])))
    for p_e, w_e, t_e in (extra_marks or []):   # пучности энергии и пр.
        marks.append((float(p_e), float(w_e) * fresh(float(t_e)), float(t_e)))
    if not marks:
        return []
    tol = (float(pc.max()) - float(pc.min())) / 80.0 or 1e-9
    marks.sort()
    clusters = []
    for p, wgt, tm in marks:
        if clusters and p - clusters[-1]["ps"][-1] <= tol:
            clusters[-1]["ps"].append(p)
            clusters[-1]["w"] += wgt
            clusters[-1]["t"] = max(clusters[-1]["t"], tm)
        else:
            clusters.append({"ps": [p], "w": wgt, "t": tm})
    out = [{"price": round(float(np.mean(c["ps"])), 4),
            "weight": round(c["w"], 3), "hits": len(c["ps"]),
            "age_min": int(round((t_last - c["t"]) / 60.0))}
           for c in clusters]
    out.sort(key=lambda x: -x["weight"])
    out = out[:top]
    # ближние к цене — первыми: скальперу важна дистанция, не абсолютный вес
    if now_price is not None:
        out.sort(key=lambda x: abs(x["price"] - float(now_price)))
    return out


def wave_antinodes(w_ts, w_psi, k_now: int) -> dict:
    """ПУЧНОСТИ ВОЛНЫ (язык школы 🟡): зона |2Ψ−1| ≥ LOUD_THRESHOLD — волна
    «громкая» (насыщение фазы), между зонами тишина. past — пики пучностей в
    окне; forward — тайминг впереди: пики И входы/выходы громкой зоны (волна
    внутри дня часто монотонна — вход в пучность и есть событие скальпа).
    now — где волна прямо сейчас."""
    p = np.asarray(w_psi, float)
    t = np.asarray(w_ts, float)
    amp = np.abs(2.0 * p - 1.0)
    past, fwd = [], []
    for i in range(1, len(p) - 1):        # пики пучностей
        if (p[i] - p[i - 1]) * (p[i + 1] - p[i]) > 0 or amp[i] < LOUD_THRESHOLD:
            continue
        item = {"t": int(t[i]),
                "kind": "гребень" if p[i] >= p[i - 1] else "впадина",
                "amp": round(float(amp[i]), 2), "event": "пик"}
        (fwd if i >= k_now else past).append(item)
    for i in range(max(k_now, 1), len(p)):  # будущие границы громкой зоны
        a0, a1 = float(amp[i - 1]), float(amp[i])
        if a0 < LOUD_THRESHOLD <= a1:
            fwd.append({"t": int(t[i]),
                        "kind": "гребень" if p[i] >= 0.5 else "впадина",
                        "amp": round(a1, 2), "event": "вход в пучность"})
        elif a1 < LOUD_THRESHOLD <= a0:
            fwd.append({"t": int(t[i]),
                        "kind": "гребень" if p[i - 1] >= 0.5 else "впадина",
                        "amp": round(a0, 2), "event": "выход из пучности"})
    fwd.sort(key=lambda x: x["t"])
    j = min(max(k_now, 0), len(p) - 1)
    now = ("в пучности " + ("гребневой" if p[j] >= 0.5 else "впадинной")
           if amp[j] >= LOUD_THRESHOLD else "в тишине")
    return {"past": past[-6:], "forward": fwd[:6], "now": now,
            "note": ("пучность = |2Ψ−1| ≥ " + str(LOUD_THRESHOLD) +
                     " — единый порог школы; насыщение фазы, не событие 🟡")}


# ── СТАРШИЕ КЛЮЧИ: небесные полости Гребенникова (ЭПС 🟡) ──
# Гребенников (1927-2001, «Мой мир» 1997; заявка на открытие № 32-ОТ-11170,
# 1985, с Золотарёвым — не зарегистрирована): форма сама генерирует поле.
# ЭПС физически не подтверждён — берём как метафору формы 🟡. Математика —
# честная 🔵: изопериметрическое неравенство (Штейнер 1838), сота Хейлза
# (Honeycomb Conjecture, 2001), добротность резонатора (Гельмгольц/Рэлей).
# Замкнутая аспектная фигура (трин/тау/крест/прямоугольник) = «полость»;
# Q = стенки (орбисы) × гладкость (равносторонность) × симметрия (изопери-
# метрия) × масштаб √(n/4). Веса E в Q не входят намеренно: «полость дышит,
# даже если планеты тихие» — постулат школы; наполнение ΣE репортится рядом.
ORB_CAVITY = 6.0     # орбис ребра: дальше стенка разомкнута
SIGMA_WALL = 3.0     # добротность стенки (гаусс отклонения от точного аспекта)
SIGMA_ROUGH = 2.0    # шероховатость: разброс отклонений между стенками
N_MAX_CAV = 4        # полная сота зодиака — 4 стенки
_CAV_ASPECTS = (60.0, 90.0, 120.0, 180.0)


def _sep_deg(a: float, b: float) -> float:
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)


def q_cavity(lams, ideals) -> float:
    """Добротность полости: lams — долготы вершин в порядке обхода, ideals —
    точные углы рёбер. Прокси площади (честно): плоский многоугольник,
    вписанный в единичный круг эклиптики (широты отброшены — 1D)."""
    lam = np.asarray(lams, float)
    n = lam.size
    d = np.array([abs(_sep_deg(lam[j], lam[(j + 1) % n]) - ideals[j])
                  for j in range(n)])
    if np.any(d > ORB_CAVITY):
        return 0.0                                           # стенки нет
    walls = float(np.exp(-np.mean((d / SIGMA_WALL) ** 2)))
    smooth = 1.0 / (1.0 + float(np.var(d)) / SIGMA_ROUGH ** 2)
    th = np.sort(np.radians(lam))
    dth = np.diff(np.append(th, th[0] + 2 * np.pi))
    S = 0.5 * abs(float(np.sum(np.sin(dth))))                # шнурок
    P = float(np.sum(2.0 * np.sin(dth / 2.0)))               # хорды
    if P < 1e-9:
        return 0.0                                           # стеллиум
    iq = 4.0 * np.pi * S / P ** 2
    sym = min(1.0, iq / ((np.pi / n) / np.tan(np.pi / n)))
    return float(math.sqrt(n / N_MAX_CAV) * walls * smooth * sym)


def _cavity_name(edges: list, diag_ok: bool, n: int) -> str:
    ms = sorted(edges)
    if ms == [120.0, 120.0, 120.0]:
        return "Большой трин"
    if ms == [90.0, 90.0, 180.0]:
        return "Тау-квадрат"
    if ms == [90.0, 90.0, 90.0, 90.0] and diag_ok:
        return "Большой крест"
    if ms == [60.0, 60.0, 120.0, 120.0] and diag_ok:
        return "Мистический прямоугольник"
    return f"полость ({n}-угольник)"


def cavities_of(trans: dict, k: int) -> dict:
    """Поиск замкнутых аспектных фигур момента: соединения склеены в кластер
    (средняя долгота по кругу), циклы 3-4 вершин по последовательным парам
    обхода, вложенные фигуры поглощаются большими."""
    from itertools import combinations
    lam0 = {nm: float(trans["lam"][nm][k]) for nm in _BODY_ORDER}
    E0 = {nm: float(trans["E"][nm][k]) for nm in _BODY_ORDER}
    # кластеры соединений (union-find по sep ≤ орбис)
    names = list(_BODY_ORDER)
    parent = list(range(len(names)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            if _sep_deg(lam0[names[i]], lam0[names[j]]) <= ORB_CAVITY:
                parent[find(i)] = find(j)
    groups: dict = {}
    for i in range(len(names)):
        groups.setdefault(find(i), []).append(names[i])
    verts = []
    for g in groups.values():
        ang = [math.radians(lam0[nm]) for nm in g]
        mean = math.degrees(math.atan2(
            sum(math.sin(a) for a in ang), sum(math.cos(a) for a in ang))) % 360.0
        verts.append({"names": g, "lam": mean,
                      "fill": sum(E0[nm] for nm in g)})
    found = []
    for r in (4, 3):
        for combo in combinations(range(len(verts)), r):
            vs = sorted(combo, key=lambda i: verts[i]["lam"])
            lams = [verts[i]["lam"] for i in vs]
            ideals = []
            ok = True
            for j in range(r):
                sep = _sep_deg(lams[j], lams[(j + 1) % r])
                best = min(_CAV_ASPECTS, key=lambda a: abs(sep - a))
                if abs(sep - best) > ORB_CAVITY:
                    ok = False
                    break
                ideals.append(best)
            if not ok:
                continue
            q = q_cavity(lams, ideals)
            if q <= 0.0:
                continue
            diag_ok = (r == 4 and
                       abs(_sep_deg(lams[0], lams[2]) - 180.0) <= ORB_CAVITY and
                       abs(_sep_deg(lams[1], lams[3]) - 180.0) <= ORB_CAVITY)
            bodies = [nm for i in vs for nm in verts[i]["names"]]
            found.append({"name": _cavity_name(ideals, diag_ok, r),
                          "bodies": bodies, "q": round(q, 3),
                          "fill": round(sum(verts[i]["fill"] for i in vs), 1),
                          "_set": frozenset(bodies)})
    # поглощение: подмножество большей фигуры не репортится
    found.sort(key=lambda f: (-len(f["_set"]), -f["q"]))
    keep = []
    for f in found:
        if any(f["_set"] < g["_set"] for g in keep):
            continue
        keep.append(f)
    keep.sort(key=lambda f: -f["q"])
    for f in keep:
        f.pop("_set", None)
    return {"figures": keep[:3],
            "word": ("полость звучит — форма сама держит поле"
                     if keep else "замкнутых фигур нет — нёбо без сот"),
            "note": ("ЭПС Гребенникова — метафора формы 🟡 (физически не "
                     "подтверждён); геометрия фигур — факт эфемерид 🔵 "
                     "(изопериметрия, сота Хейлза); площадь — плоский прокси "
                     "1D-эклиптики; полость не предсказывает ⚫")}


# ── СТАРШИЕ КЛЮЧИ: аккорды Кили (симпатическая вибрационная физика 🟡) ──
# Кили (1837-1898): резонанс не тона, а АККОРДА (терции/квинты); документально
# разоблачён (Scientific American 1899, сжатый воздух) — берём как словарь 🟡.
# Честный мост 🔵 — Гельмгольц 1863: консонанс = минимум биений, чистые
# отношения 2:1 3:2 4:3 5:4 6:5; чем консонантнее, тем острее минимум.
# НЕ дубль n:m-сцепок: там фазы медленных волн на окне (Тасс), здесь —
# МГНОВЕННОЕ отношение угловых скоростей тел из DE440. 1:1 исключён (унисон
# Солнце-Меркурий был бы вечным «аккордом»).
_KEELY_RATIOS = ((2, 1), (3, 2), (4, 3), (5, 4), (6, 5), (5, 3), (8, 5))
KEELY_SIG0 = 0.004     # допуск октавы ~0.6%; терции шире (σ·√(n·m))
KEELY_C_MIN = 0.10     # порог ребра аккорда
KEELY_V_MIN = 1e-3     # °/сут; стационар — тишина (честно выпадает)


def _keely_matrix(v, E):
    """Матрица консонанса пар: относительная расстройка от чистой пропорции,
    вес чистоты 1/√(n·m). Противоход (знаки скоростей разные) — не аккорд:
    симпатическая вибрация — совибрация. Оба ретро — сонаправлены, можно."""
    n = len(v)
    C = np.zeros((n, n))
    ok = np.abs(v) >= KEELY_V_MIN
    for i in range(n):
        for j in range(i + 1, n):
            if not (ok[i] and ok[j]) or v[i] * v[j] < 0:
                continue
            q = max(abs(v[i]), abs(v[j])) / min(abs(v[i]), abs(v[j]))
            for a, b in _KEELY_RATIOS:
                r = a / b
                w = (a * b) ** -0.5
                sig = KEELY_SIG0 * (a * b) ** 0.5
                C[i, j] = C[j, i] = max(
                    C[i, j], w * math.exp(-((q - r) / (r * sig)) ** 2))
    return C


def keely_chords(v: list, E: list, names: list) -> dict:
    """Аккорды Кили: связные компоненты ≥3 тел в графе консонанса скоростей.
    R — суммарная громкость аккордов (0 ровно, если аккорда нет). 🔵 —
    отношение скоростей из эфемерид; 🟡 — музыкальная пропорция как метафора
    соизмеримости (не акустика, не Гц); ⚫ — аккорд не событие и не прогноз."""
    v = np.asarray(v, float)
    E = np.asarray(E, float)
    C = _keely_matrix(v, E)
    A = C >= KEELY_C_MIN
    parent = list(range(len(v)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(v)):
        for j in range(i + 1, len(v)):
            if A[i, j]:
                parent[find(i)] = find(j)
    comp: dict = {}
    for i in range(len(v)):
        comp.setdefault(find(i), []).append(i)
    chords = [c for c in comp.values() if len(c) >= 3]
    W = np.sqrt(np.outer(np.abs(E), np.abs(E)))
    R = float(sum(C[i, j] * W[i, j] for c in chords
                  for i in c for j in c if i < j and A[i, j]))
    # тритон-мосты и Φ-каналы (аудит №3, зеркало натального ENGINE 26):
    # диссонанс 36:25 — «искра» (не консонанс); отношение у степени золотого
    # сечения — КАМ-канал, который резонансами не рвётся. Идентификация 2%.
    TRI_R, PHI_ = 36.0 / 25.0, (1.0 + math.sqrt(5.0)) / 2.0
    tritones, phi_ch = [], []
    for i in range(len(v)):
        for j in range(i + 1, len(v)):
            if abs(v[i]) < 1e-9 or abs(v[j]) < 1e-9 or v[i] * v[j] < 0:
                continue
            q = max(abs(v[i]), abs(v[j])) / min(abs(v[i]), abs(v[j]))
            dv = abs(q - TRI_R) / TRI_R
            if dv <= 0.02:
                tritones.append({"pair": f"{names[i]}·{names[j]}",
                                 "q": round(float(q), 4), "dev": round(dv, 4)})
            for kk, pk in enumerate((PHI_, PHI_ ** 2, PHI_ ** 3), start=1):
                dp_ = abs(q - pk) / pk
                if dp_ <= 0.02:
                    phi_ch.append({"pair": f"{names[i]}·{names[j]}",
                                   "q": round(float(q), 4), "k": kk,
                                   "dev": round(dp_, 4)})
                    break
    tritones.sort(key=lambda x: x["dev"])
    phi_ch.sort(key=lambda x: x["dev"])
    return {"r": round(R, 3),
            "tritones": tritones[:3], "phi": phi_ch[:3],
            "chords": [[names[i] for i in c] for c in chords],
            "pairs": sorted(
                [{"pair": f"{names[i]}·{names[j]}", "c": round(float(C[i, j]), 3)}
                 for c in chords for i in c for j in c if i < j and A[i, j]],
                key=lambda x: -x["c"])[:6],
            "word": ("макро-аккорд звучит — окно пересборки структуры"
                     if chords else "аккорда нет — тела не соизмеримы"),
            "note": ("Кили/Понд как словарь 🟡, механика консонанса — "
                     "Гельмгольц 1863 🔵; музыкальная пропорция — метафора "
                     "соизмеримости скоростей, не акустика; не событие ⚫")}


def keely_of(trans: dict, k: int) -> dict:
    """Аккорд Кили на сетке транзитов: скорости — конечная разность развёрнутой
    долготы (как в ядре REAL SKY), веса — E_i момента."""
    ts = np.asarray(trans["ts"], float)
    if len(ts) < 3:
        return {"r": 0.0, "chords": [], "pairs": [],
                "word": "мало точек", "note": "нет сетки"}
    k = min(max(k, 1), len(ts) - 2)
    dt_d = (ts[k + 1] - ts[k - 1]) / 86400.0
    names, v, E = [], [], []
    for nm in _BODY_ORDER:
        lam = np.unwrap(np.asarray(trans["lam"][nm], float), period=360.0)
        names.append(nm)
        v.append(float(lam[k + 1] - lam[k - 1]) / dt_d)
        E.append(float(trans["E"][nm][k]))
    return keely_chords(v, E, names)


# ── СТАРШИЕ КЛЮЧИ: версоры Штейнмеца-Долларда (натяжение/разрядка 🟡) ──
# Штейнмец 1893/1897 🔵: фазор («версор») свёл цепи переменного тока к
# алгебре; аналитический сигнал Габора — фазор, ставший функцией времени.
# Доллард (Borderland 1985-87) 🟡: язык «диэлектрик копит — магнетизм
# разряжает»; продольные волны — маргиналия, взят только словарь.
# Честный прокси 🔵: энергетика осциллятора U=½ω̄²x² («диэлектрик», сжатая
# пружина) и K=½v² («магнетизм», разрядка) — LC-контур байт-в-байт.
# «Пробой зреет» = натяжение без движения ≥ четверти периода несущей
# (замер фазы Гильберт не показывает — детектор по выдержке, не по ω).


def _medfilt(x, w: int):
    if w < 3:
        return np.asarray(x, float)
    r = w // 2
    xp = np.pad(np.asarray(x, float), r, mode="edge")
    return np.array([float(np.median(xp[i:i + w])) for i in range(len(x))])


def versor_split(psi, dt: float, w_med: int = 9, mk_eps: float = 0.15,
                 q_ed: float = 70.0, dwell_frac: float = 0.25,
                 guard: int = 8):
    """→ (E_d, M_k, tension, ω̄) в [0..1] или None (волна не колеблется —
    честное молчание, не дефолт)."""
    x = np.asarray(psi, float) - float(np.mean(psi))
    z = _analytic(x)
    omega = np.gradient(np.unwrap(np.angle(z)), dt)
    omega_s = _medfilt(omega, w_med)
    pos = omega_s[omega_s > 0]
    if not pos.size:
        return None
    om_bar = float(np.median(pos))
    v = np.gradient(x, dt)
    E_d = 0.5 * om_bar ** 2 * x ** 2
    M_k = 0.5 * v ** 2
    scale = float(np.max(E_d + M_k)) or 1e-12
    E_d, M_k = E_d / scale, M_k / scale
    cond = (E_d >= np.percentile(E_d, q_ed)) & (M_k < mk_eps)
    need = max(2, int(round(dwell_frac * (2 * np.pi / om_bar) / dt)))
    # окно короче периода несущей — планка выдержки честно опускается до
    # трети окна, иначе натяжение не могло бы сработать никогда
    need = min(need, max(2, x.size // 3))
    tension = np.zeros(x.size, bool)
    run = 0
    for i, c in enumerate(cond):
        run = run + 1 if c else 0
        if run >= need:
            tension[i - run + 1:i + 1] = True
    tension[:guard] = tension[-guard:] = False
    return E_d, M_k, tension, om_bar


def versor_of(psi, dt_h: float, k: int, w_ts=None) -> dict:
    vs = versor_split(psi, dt_h)
    if vs is None:
        return {"none": True,
                "note": "волна в окне не колеблется — версор честно молчит"}
    E_d, M_k, tension, om_bar = vs
    j = min(max(k, 0), len(E_d) - 1)
    zones = []
    if w_ts is not None:
        t_arr = np.asarray(w_ts, float)
        i0 = None
        for i, f in enumerate(tension):
            if f and i0 is None:
                i0 = i
            elif not f and i0 is not None:
                zones.append([int(t_arr[i0]), int(t_arr[i - 1])])
                i0 = None
        if i0 is not None:
            zones.append([int(t_arr[i0]), int(t_arr[-1])])
    if tension[j]:
        word = "натяжение у предела — пружина сжата, разрядки нет (пробой зреет)"
    elif E_d[j] > M_k[j]:
        word = "диэлектрическая фаза — поле копит натяжение"
    else:
        word = "магнитная фаза — разрядка бежит"
    return {"ed": round(float(E_d[j]), 3), "mk": round(float(M_k[j]), 3),
            "tension": bool(tension[j]),
            "t_frac": round(float(tension.mean()), 3),
            "zones": zones[-8:],
            "period_h": round(2 * math.pi / om_bar, 1),
            "word": word,
            "note": ("E_d/M_k — потенциальная/кинетическая энергия осциллятора "
                     "волны Ψ (LC-аналог) 🔵; «эфир»/«пробой» — язык линии "
                     "Штейнмец→Доллард 🟡; не событие и не сигнал ⚫")}


def session_vwap(rows) -> dict | None:
    """VWAP последней сессии + полосы ±1σ/±2σ (взвешенное отклонение) 🔵.
    Якорь — начало последних суток UTC в данных; типичная цена (H+L+C)/3."""
    ts, tp, vv = [], [], []
    for r in (rows or []):
        try:
            if isinstance(r, dict):
                t = r.get("t")
                if isinstance(t, str):
                    t = datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
                h, l, c, v = (float(r["h"]), float(r["l"]), float(r["c"]),
                              float(r.get("v") or 0))
            else:
                t = float(r[0]); h, l, c, v = map(float, (r[2], r[3], r[4],
                                                          r[5] if len(r) > 5 else 0))
            if v > 0:
                ts.append(float(t)); tp.append((h + l + c) / 3.0); vv.append(v)
        except Exception:
            continue
    if len(vv) < 12:
        return None
    day0 = datetime.fromtimestamp(ts[-1], timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    sel = [i for i, t in enumerate(ts) if t >= day0]
    if len(sel) < 6:
        sel = list(range(len(ts)))[-48:]
    tp_a = np.array([tp[i] for i in sel]); vv_a = np.array([vv[i] for i in sel])
    sw = float(vv_a.sum()) or 1e-9
    vw = float((tp_a * vv_a).sum() / sw)
    sd = math.sqrt(float((vv_a * (tp_a - vw) ** 2).sum() / sw))
    return {"vwap": round(vw, 4), "sd": round(sd, 4),
            "up1": round(vw + sd, 4), "dn1": round(vw - sd, 4),
            "up2": round(vw + 2 * sd, 4), "dn2": round(vw - 2 * sd, 4),
            "note": "VWAP сессии + полосы ±1σ/±2σ (взвешенные) — линейка толпы 🔵"}


# ── EBS Элерса («Cycle Analytics for Traders» 2013, гл. 12) 🔵 ──
# Режим «рельс/качели» для скальпа. Оригинал в EasyLanguage В ГРАДУСАХ —
# здесь радианы (360/Dur → 2π/Dur, 1.414·180/SSF → 1.414·π/SSF).
# HP нарочно ОДНОполюсный (гл. 12, не roofing): тренд просачивается —
# именно это делает EBS детектором трендового режима. Только закрытые бары.
# Находка веера: на чистом цикле EBS — скруглённая меандра, касается ±0.85
# на каждом горбе ⇒ рельс = ОДНОЗНАКОВАЯ ВЫДЕРЖКА у рельса, не мгновение.
EBS_DURATION = 40    # баров (книжный канон; «тренд или цикл на 40 баров»)
EBS_SSF = 10
EBS_RAIL = 0.85      # порог рельса (эмпирика Элерса 🟡)
EBS_DWELL = 18       # ≈0.45·Duration; цикл держит ≤0.40·N — рельс не притворится
EBS_WARMUP = max(100, 2 * EBS_DURATION)   # прогрев рекурсии — None, честно


def ebs_series(close, duration: int = EBS_DURATION, ssf: int = EBS_SSF):
    c = np.asarray(close, float)
    n = c.size
    th = 2.0 * math.pi / duration
    a = (1.0 - math.sin(th)) / math.cos(th)
    a1 = math.exp(-1.414 * math.pi / ssf)
    b1 = 2.0 * a1 * math.cos(1.414 * math.pi / ssf)
    c2, c3 = b1, -a1 * a1
    c1 = 1.0 - c2 - c3
    hp = np.zeros(n)
    filt = np.zeros(n)
    for t in range(1, n):
        hp[t] = 0.5 * (1.0 + a) * (c[t] - c[t - 1]) + a * hp[t - 1]
        if t >= 2:
            filt[t] = c1 * (hp[t] + hp[t - 1]) / 2.0 + c2 * filt[t - 1] \
                + c3 * filt[t - 2]
    f1 = np.roll(filt, 1)
    f2 = np.roll(filt, 2)
    f1[0] = 0.0
    f2[:2] = 0.0
    wave = (filt + f1 + f2) / 3.0
    pwr = (filt ** 2 + f1 ** 2 + f2 ** 2) / 3.0
    ebs = np.full(n, np.nan)
    ok = pwr > 1e-24
    ebs[ok] = wave[ok] / np.sqrt(pwr[ok])
    ebs[:EBS_WARMUP] = np.nan
    return ebs


def ebs_of(rows) -> dict | None:
    _, cl = _rows_ts_close(rows)
    if len(cl) < EBS_WARMUP + 8:
        return None
    e = ebs_series(cl)
    valid = e[~np.isnan(e)]
    if valid.size < EBS_DWELL + 2:
        return None
    now = float(valid[-1])
    run = 0
    if now > EBS_RAIL:
        run = 1
        for x in valid[-2::-1]:
            if x > EBS_RAIL:
                run += 1
            else:
                break
    elif now < -EBS_RAIL:
        run = 1
        for x in valid[-2::-1]:
            if x < -EBS_RAIL:
                run += 1
            else:
                break
    if run >= EBS_DWELL:
        mode = "рельс вверх" if now > 0 else "рельс вниз"
    else:
        mode = "качели"
    return {"now": round(now, 3), "mode": mode, "rail_run": int(run),
            "note": ("EBS Элерса (2013, гл.12) на закрытых барах 🔵; рельс = "
                     f"однознаковая выдержка ≥{EBS_DWELL} баров у ±{EBS_RAIL} "
                     "(пороги — эмпирика 🟡); режим фильтра, не направление ⚫")}


# ── ЗАРЯД ТОЛПЫ: живая шкала −1..+1 для минутного скальпа ──
# Вайкофф: объём = усилие, ход = результат; VSA Уильямса: участие толпы.
# направление = медианный наклон K закрытых баров (факт 🔵, не прогноз);
# сила = энергия-ворота × объём-усилитель (0.5→1.0 при z_V до +2);
# r Курамото в формулу НЕ вплетён (он 🟡-язык неба — рамку не смешиваем).
CHARGE_K = 8         # медианы половинок 4+4 гасят одиночный выброс-бар


def charge_of(closes, vols, e01_now, d_effort=None,
              k: int = CHARGE_K) -> dict | None:
    c = np.asarray(closes, float)
    v = np.asarray(vols, float)
    if c.size < max(k, EFFORT_WIN) + 2 or e01_now is None:
        return None
    half = k // 2
    slope = float(np.median(c[-half:]) - np.median(c[-k:-half]))
    noise = 1.4826 * float(np.median(np.abs(np.diff(c))[-EFFORT_WIN:]))
    if noise < 1e-12:
        return None                       # мёртвое окно — честный None
    direction = float(np.clip(slope / (noise * math.sqrt(half)), -1.0, 1.0))
    w = v[-EFFORT_WIN:]
    sv = float(np.std(w))
    z_v = float((v[-1] - w.mean()) / sv) if sv > 1e-12 else 0.0
    strength = float(e01_now) * (0.5 + 0.5 * float(np.clip(z_v / 2.0, 0.0, 1.0)))
    ch = direction * strength
    e01 = float(e01_now)
    if ch >= 0.5:
        word = "толпа заряжена вверх — ход питается"
    elif ch >= 0.2:
        word = "толпа тянет вверх, заряд умеренный"
    elif ch <= -0.5:
        word = "толпа заряжена вниз — ход питается"
    elif ch <= -0.2:
        word = "толпа тянет вниз, заряд умеренный"
    elif e01 >= 0.7:
        word = "толпа в перетяг — энергия горит на месте"
        if d_effort is not None and d_effort >= EFFORT_THR:
            word += " (стена: усилие без хода)"
    elif e01 <= 0.3:
        word = "толпа вялая — поле без топлива"
    else:
        word = "заряд слабый, фон"
    return {"charge": round(ch, 3), "dir": round(direction, 3),
            "z_v": round(z_v, 2), "strength": round(strength, 3),
            "word": word,
            "note": (f"направление — медианный наклон K={k} закрытых баров, "
                     "факт 🔵, не прогноз; сила = e01·(0.5+0.5·clip(z_V/2)) — "
                     "состояние среды ⚫; веса и планки — прокси 🟡; "
                     "не торговый сигнал, 18+")}


def _rows_cv(rows) -> tuple[list, list]:
    cs, vs = [], []
    for r in (rows or []):
        try:
            if isinstance(r, dict):
                c, v = float(r["c"]), float(r.get("v") or 0)
            else:
                c, v = float(r[4]), float(r[5] if len(r) > 5 else 0)
            cs.append(c)
            vs.append(v)
        except Exception:
            continue
    return cs, vs


# ── ИМПЛОЗИЯ: Вайкофф как импеданс среды (минутный микроуровень) ──
# Z(t)=V/(|ΔC|+тик): гигантский объём при микро-ходе — «стена»: среда встала,
# ликвидность впитывается («стоячий вихрь» Ацюковского/Шаубергера — язык 🟡;
# сама аномалия отношения объём/ход выше 99-го перцентиля окна — факт 🔵).
IMPL_Q = 99.0
IMPL_MIN_BARS = 60


def implosion_of(rows) -> dict | None:
    cs, vs = _rows_cv(rows)
    if len(cs) < IMPL_MIN_BARS:
        return None
    c = np.asarray(cs, float)
    v = np.asarray(vs, float)
    dc = np.abs(np.diff(c))
    vv = v[1:]
    if not np.any(dc > 0) or float(vv.max()) <= 0:
        return None
    tick = float(np.median(dc[dc > 0])) * 0.1
    z = vv / (dc + max(tick, 1e-9))
    if float(np.std(z)) < 1e-12:
        return None
    p99 = float(np.percentile(z, IMPL_Q))
    k = len(z) - 2                         # последний ЗАКРЫТЫЙ бар
    z_now = float(z[k])
    act = bool(p99 > 0 and z_now >= p99)
    return {"z": round(z_now, 1), "p99": round(p99, 1),
            "ratio": round(z_now / p99, 2) if p99 > 0 else None,
            "active": act,
            "price": round(float(c[k + 1]), 4) if act else None,
            "word": ("ИМПЛОЗИЯ: объём встал в цену — стена, среда впитывает"
                     if act else "среда проводит — стены нет"),
            "note": ("Z=V/(|ΔC|+тик), стена = Z ≥ P99 окна 🔵; «вихрь/"
                     "впитывание» — язык школы 🟡; уровень — цена бара-стены; "
                     "не указание ⚫")}


# ── ЭФИРНЫЕ ПОЛОСТИ: незаполненные разрывы трёх баров (имбаланс) ──
# Импульс пролетел диапазон без проторговки — «пустота, куда среда тянется»
# (Гребенников/Шаубергер — язык 🟡); сам разрыв и его незаполненность — факт
# баров 🔵. Верх-разрыв: low[i+1] > high[i-1]; низ: high[i+1] < low[i-1].
VOID_MAX = 3


def voids_of(rows) -> list:
    hs, ls = [], []
    for r in (rows or []):
        try:
            if isinstance(r, dict):
                h, l = float(r["h"]), float(r["l"])
            else:
                h, l = float(r[2]), float(r[3])
            hs.append(h)
            ls.append(l)
        except Exception:
            continue
    n = len(hs)
    if n < 12:
        return []
    h = np.asarray(hs, float)
    l = np.asarray(ls, float)
    out = []
    # последний бар ещё формируется — полости с его участием не фиксируем
    for i in range(1, n - 2):
        if l[i + 1] > h[i - 1]:            # полость вверх-импульса (ниже цены)
            lo, hi = float(h[i - 1]), float(l[i + 1])
            if i + 2 < n:
                later_min = float(np.min(l[i + 2:]))
                if later_min <= lo:
                    continue               # заполнена целиком
                hi = min(hi, later_min)    # остаток пустоты
            if hi - lo > 1e-9:
                out.append({"lo": round(lo, 4), "hi": round(hi, 4),
                            "dir": "вверх", "i": i})
        elif h[i + 1] < l[i - 1]:          # полость вниз-импульса (выше цены)
            lo, hi = float(h[i + 1]), float(l[i - 1])
            if i + 2 < n:
                later_max = float(np.max(h[i + 2:]))
                if later_max >= hi:
                    continue
                lo = max(lo, later_max)
            if hi - lo > 1e-9:
                out.append({"lo": round(lo, 4), "hi": round(hi, 4),
                            "dir": "вниз", "i": i})
    # лимит ПО СТОРОНЕ (веер v3.0): раньше «3 свежие» резали поперёк
    # направлений и ближний магнит одной стороны вылетал ради дальних другой
    out.sort(key=lambda z: -z["i"])        # свежие первыми внутри стороны
    for z in out:
        z.pop("i", None)
    ups = [z for z in out if z["dir"] == "вверх"][:VOID_MAX]
    dns = [z for z in out if z["dir"] == "вниз"][:VOID_MAX]
    return ups + dns


# ── ρ РЫНКА: межрыночная когерентность (Курамото самого рынка) ──
# Теневой вход в один актив деформирует фазы смежных: считаем r фаз
# Гильберта лог-приростов корзины 🔵; «струна натянута» — язык школы 🟡.
RHO_MIN_ASSETS = 3
RHO_MIN_BARS = 48


def market_coherence(series: dict) -> dict | None:
    phases = []
    names = []
    for name, closes in (series or {}).items():
        c = np.asarray([x for x in (closes or []) if x], float)
        if c.size < RHO_MIN_BARS or np.any(c <= 0):
            continue
        ph = np.angle(_analytic(np.diff(np.log(c))))
        phases.append(ph)
        names.append(name)
    if len(phases) < RHO_MIN_ASSETS:
        return None
    L = min(p.size for p in phases)
    M = np.stack([p[-L:] for p in phases])
    edge = max(3, L // EDGE_FRAC)
    r_t = np.abs(np.mean(np.exp(1j * M), axis=0))[edge:-edge]
    if r_t.size < 8:
        return None
    r_now = float(np.mean(r_t[-max(4, r_t.size // 4):]))
    if r_now >= 0.7:
        word = "рынок натянут одной струной — сетка фаз слиплась"
    elif r_now <= 0.3:
        word = "активы врозь — сетка дышит свободно"
    else:
        word = "сетка фаз обычная"
    return {"r": round(r_now, 3), "assets": names,
            "word": word,
            "note": ("Курамото-r фаз Гильберта лог-приростов корзины 🔵 "
                     "(часовые закрытия, грубое выравнивание хвостом — "
                     "прокси 🟡); деформация сетки — не событие ⚫")}


# ── ГИСТЕРЕЗИС ЗЕРКАЛА: corr дышит вокруг порога −0.3, и флаг зеркала
# перещёлкивался между обновлениями — проекция «рисовала то одно будущее,
# то другое». Вход в зеркало ниже −0.35, выход выше −0.25: внутри полосы
# держим прежнее состояние. Память на процесс (ключ тикер|интервал|шкала);
# честно: при перезапуске сервера первый расчёт берёт сырой порог −0.3.
MIRROR_ON, MIRROR_OFF = -0.35, -0.25       # запасной путь: оконная корреляция
M_ON, M_OFF = -0.5, -0.25                  # главный путь: мгновенный cos(φp−φw)
M_SMOOTH = 8                               # сглаживание m_inst, баров ядра
# ── СКЛЕЙКА ВРЕМЕНИ (аудит №3): фрактальный резонанс масштабов 🔵 ──
# Небо одно, а графики разные — шизофрения таймфреймов. Медленные фазы
# интерполируются на быструю сетку (свирепый снэппинг), резонанс — согласие
# косинусов разности фаз за последние бары. r≈1 — матрёшка сложена: и
# макро-волна, и средняя, и минутная бьют в одну сторону.
FRACTAL_MID, FRACTAL_SLOW = 4, 12
FRACTAL_TAIL = 8


def _agg_close(c, m: int):
    k = c.size // m
    if k < 8:
        return None
    return c[:k * m].reshape(k, m)[:, -1], (np.arange(k) + 1.0) * m - 1.0


def fractal_resonance(closes) -> dict | None:
    """r01∈[0..1]: согласие фаз масштабов 1×/4×/12× на быстрой сетке."""
    if closes is None:
        closes = []
    c = np.asarray([x for x in closes if x is not None], float)
    if c.size < 96:
        return None
    idx = np.arange(c.size, dtype=float)
    ph_f = np.unwrap(np.angle(_analytic(c)))
    parts = []
    for m in (FRACTAL_MID, FRACTAL_SLOW):
        agg = _agg_close(c, m)
        if agg is None:
            continue
        cm, im = agg
        ph_m = np.unwrap(np.angle(_analytic(cm)))
        ph_mi = np.interp(idx, im, ph_m)      # медленная фаза на быстрой сетке
        parts.append(np.cos(ph_f - ph_mi))
    if not parts:
        return None
    r = float(np.mean([np.mean(p[-FRACTAL_TAIL:]) for p in parts]))
    r01 = (r + 1.0) / 2.0
    if r01 >= 0.75:
        word = "матрёшка сложена — все масштабы бьют в одну сторону"
    elif r01 <= 0.4:
        word = "масштабы спорят — быстрый ход против медленного, среда рвёт"
    else:
        word = "масштабы согласны частично"
    return {"r": round(r01, 3), "word": word,
            "note": ("фазы Гильберта 1×/4×/12× на общей сетке (np.interp — "
                     "снэппинг размерностей) 🔵; «матрёшка» — язык школы 🟡")}


# ── ЧИРИКОВ (аудит №3): перекрытие резонансов = окно хаоса 🔵 ──
# Когда зоны двух узлов Дирака пересекаются (K=Σσ/зазор ≥ 1), система
# по Чирикову уходит в динамический хаос: уровни и техника ломаются.
def chirikov_of(nodes) -> list:
    out = []
    ns = sorted([n for n in (nodes or [])
                 if n.get("t_exact") and n.get("sigma_min")],
                key=lambda n: float(n["t_exact"]))
    for a, b in zip(ns, ns[1:]):
        gap = abs(float(b["t_exact"]) - float(a["t_exact"]))
        halfsum = (float(a["sigma_min"]) + float(b["sigma_min"])) * 60.0
        if gap < 1.0:
            gap = 1.0
        K = halfsum / gap
        if K >= 1.0:
            out.append({"t0": int(min(float(a["t_exact"]), float(b["t_exact"]))),
                        "t1": int(max(float(a["t_exact"]), float(b["t_exact"]))),
                        "k": round(K, 2),
                        "pairs": [a.get("pair"), b.get("pair")],
                        "word": "перекрытие резонансов — окно хаоса: уровни ненадёжны"})
    return out[:4]


_MIRROR_STATE: dict = {}


def _mirror_hyst(key: str, corr, raw: bool, m_inst=None) -> bool:
    """Зеркало v3.1 (аудит №3): главный триггер — МГНОВЕННЫЙ косинус разности
    фаз Гильберта (переворот виден в ту же свечу; корреляция за окно опаздывала
    на 10–15 баров, пока старые плюсы не вымоются). Гистерезис остаётся —
    флаттер у порога не дребезжит. Нет фаз → запасной путь по corr."""
    prev = _MIRROR_STATE.get(key)
    if m_inst is not None:
        if prev is None:
            m = m_inst < M_ON
        elif prev:
            m = m_inst < M_OFF
        else:
            m = m_inst < M_ON
    elif corr is None:
        return raw
    else:
        if prev is None:
            m = bool(raw)
        elif prev:
            m = corr < MIRROR_OFF
        else:
            m = corr < MIRROR_ON
    if len(_MIRROR_STATE) > 64:
        _MIRROR_STATE.clear()
    _MIRROR_STATE[key] = m
    return m


# ── СВЕТОФОР ПОЛЯ: честная сборка пяти УЖЕ посчитанных величин с
# именованными весами. Не сигнал и не направление ⚫ — одно число «насколько
# поле заряжено»: каждая часть видна в parts, сборка проверяема руками.
FIELD_W = {"энергия": 0.35, "сцепка": 0.25, "версор": 0.15,
           "плита": 0.15, "узел": 0.10}


def field_state(ctx: dict) -> dict | None:
    sc = ctx.get("scalp") or {}
    if not sc or sc.get("error"):
        return None
    parts = {}
    en = sc.get("energy") or {}
    if en.get("now") is not None:
        parts["энергия"] = float(en["now"])
    rp = sc.get("plv_roll") or {}
    if rp.get("now") is not None:
        parts["сцепка"] = float(rp["now"])
    ver = (ctx.get("keys") or {}).get("versor") or {}
    if not ver.get("none") and ver.get("ed") is not None:
        parts["версор"] = (1.0 if ver.get("tension")
                           else (0.6 if ver["ed"] > ver["mk"] else 0.3))
    npc = ctx.get("now_price")
    pls = (sc.get("plates_micro") or []) + (sc.get("plates_aether") or [])
    day_pl = ((ctx.get("scales") or {}).get("day") or {}).get("plates") or {}
    span = float(day_pl.get("hi") or 0) - float(day_pl.get("lo") or 0)
    if npc is not None and pls and span > 0:
        d = min(abs(float(p["price"]) - float(npc)) for p in pls)
        parts["плита"] = math.exp(-d / (0.25 * span))
    try:
        now_ts = datetime.fromisoformat(ctx["ts"]).timestamp()
    except (ValueError, KeyError, TypeError):
        now_ts = None
    wins = [w for w in (sc.get("windows") or [])
            if now_ts and float(w.get("t_exact", 0)) >= now_ts]
    if wins:
        nxt = min(wins, key=lambda w: w["t_exact"])
        parts["узел"] = math.exp(-(float(nxt["t_exact"]) - now_ts)
                                 / (float(nxt["sigma_min"]) * 120.0))
    if not parts:
        return None
    wsum = sum(FIELD_W[k] for k in parts)
    score = sum(FIELD_W[k] * v for k, v in parts.items()) / wsum
    if score >= 0.7:
        word = "поле заряжено — среда живая"
    elif score >= 0.4:
        word = "поле среднее"
    else:
        word = "поле вялое — среда без топлива"
    return {"score": round(score, 3), "word": word,
            "parts": {k: round(v, 3) for k, v in parts.items()},
            "note": ("сборка с именованными весами "
                     + " ".join(f"{k} {FIELD_W[k]}" for k in FIELD_W)
                     + "; состояние среды, не сигнал и не направление ⚫")}


# ── УСИЛИЕ/РЕЗУЛЬТАТ (VSA, Вайкофф/Уильямс 🔵): расхождение объёма и хода ──
EFFORT_WIN = 20        # окно z-оценок
EFFORT_THR = 2.0       # |D| выше — расхождение достойно слова


def effort_result(rows) -> dict | None:
    """D = z(V) − z(|ΔC|) последнего ЗАКРЫТОГО бара: усилие (объём) против
    результата (ход). D≥+2 — усилие без хода (стена/поглощение);
    D≤−2 — ход без усилия (разрежённая среда)."""
    cs, vs = [], []
    for r in (rows or []):
        try:
            if isinstance(r, dict):
                c, v = float(r["c"]), float(r.get("v") or 0)
            else:
                c, v = float(r[4]), float(r[5] if len(r) > 5 else 0)
            cs.append(c)
            vs.append(v)
        except Exception:
            continue
    if len(cs) < EFFORT_WIN + 4:
        return None
    dc = np.abs(np.diff(np.asarray(cs, float)))
    vv = np.asarray(vs, float)[1:]
    k = len(dc) - 2                       # последний ЗАКРЫТЫЙ бар
    wv = vv[k - EFFORT_WIN + 1:k + 1]
    wd = dc[k - EFFORT_WIN + 1:k + 1]
    sv, sd = float(np.std(wv)), float(np.std(wd))
    if sv < 1e-12 or sd < 1e-12:
        return None                       # мёртвое окно — честный None
    D = float((vv[k] - wv.mean()) / sv - (dc[k] - wd.mean()) / sd)
    if D >= EFFORT_THR:
        word = "усилие без хода — стена/поглощение объёма"
    elif D <= -EFFORT_THR:
        word = "ход без усилия — разрежённая среда"
    else:
        word = "усилие и ход согласны"
    return {"d": round(D, 2), "word": word,
            "note": ("VSA 🔵: z-оценки окна " + str(EFFORT_WIN) +
                     " закрытых баров; расхождение — свойство среды, "
                     "не указание ⚫")}


# параметры волны по таймфрейму: (горизонт часов, шаг минут, дней свечей)
_IV_SEC = {"1m": 60, "5m": 300, "10m": 600, "15m": 900, "30m": 1800,
           "1h": 3600, "4h": 14400, "1d": 86400,
           # без этих двух ключей шаг дальней линии в chart_payload молча
           # падал на 3600 с: на недельном/месячном графике «дальний эфир»
           # рисовался ЧАСОВОЙ сеткой поверх недельных свечей
           "1w": 604800, "1M": 2592000}
# (горизонт_ч, шаг_волны_мин, суток_свечей). ГЛАВНОЕ ПРО ЭТОТ СЛОВАРЬ:
# окно сцепки — ПОЛОВИНА окна волны. compute_context строит t0 = now−горизонт
# и длину 2·горизонт, а цена лежит только СЛЕВА от «сейчас»; правая половина —
# это проекция, ей встречаться не с чем. Значит в ГОРИЗОНТ обязаны помещаться
# минимум 16 баров своего ТФ, иначе couple_wave_price честно вернёт None и
# эфирная лента пропадёт с графика (chart_payload уйдёт в ether_raw).
# Замерено стендом backend/lab/why_oil.py на 4 инструментах:
#   · «1w»: горизонт был 2160 ч = 90 сут → 12-13 недельных баров < 16, сцепка
#     не считалась НИ РАЗУ ни у одного инструмента (13 баров у всех четырёх);
#   · «1M»: ключа в словаре не было вовсе, tf_params молча отдавал профиль
#     «1h» — окно ±24 ч поверх МЕСЯЧНЫХ свечей за 7 суток, 0-1 бар на входе.
# Профили 1m…1d не тронуты: их числа остаются байт-в-байт прежними.
_TF_WAVE = {"1m": (6.0, 5.0, 1), "5m": (12.0, 10.0, 2), "10m": (12.0, 10.0, 2),
            "15m": (18.0, 15.0, 3), "30m": (24.0, 20.0, 5),
            "1h": (24.0, 30.0, 7), "4h": (72.0, 60.0, 30),
            "1d": (720.0, 360.0, 60), "1w": (4320.0, 1440.0, 360),
            "1M": (17280.0, 5760.0, 1440)}


def tf_params(interval: str) -> tuple:
    return _TF_WAVE.get(interval, _TF_WAVE["1h"])


def _step_min_of(rows) -> float:
    ts, _ = _rows_ts_close(rows)
    if len(ts) < 3:
        return 60.0
    return max(1.0, float(np.median(np.diff(ts))) / 60.0)


# ══════════════════════════════════════════════════════════════════════
# ПУБЛИЧНЫЙ КОНТРАКТ
# ══════════════════════════════════════════════════════════════════════

def _rows_ts_close(rows) -> tuple[list, list]:
    """Адаптер свечей Пифии: список dict {"t": ISO, "o/h/l/c": float, ...}
    ИЛИ список [ts, o, h, l, c, v]. → (unix_ts[], close[])."""
    ts, cl = [], []
    for r in (rows or []):
        try:
            if isinstance(r, dict):
                t = r.get("t")
                if isinstance(t, str):
                    t = datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
                c = r.get("c")
            else:
                t = float(r[0])
                if t > 1e11:                    # миллисекунды → секунды
                    t /= 1000.0
                c = r[4] if len(r) > 4 else r[1]
            if t is None or c is None:
                continue
            ts.append(float(t)); cl.append(float(c))
        except Exception:
            continue
    return ts, cl


def candles_from_dossier(d: dict) -> dict:
    """Мост из досье Пифии: три масштаба свечей → формат compute_context.
    Единая точка правды для провязки (collect_dossier кладёт эти ключи)."""
    return {"day": (d or {}).get("candles_intraday_5m") or [],
            "week": (d or {}).get("candles_hourly") or [],
            "month": (d or {}).get("candles_daily_tail") or []}


def genesis_for(ticker: str, issue_date: str | None = None) -> dict:
    """Генезис по корню тикера; вне реестра — дата ВЫПУСКА бумаги с карточки
    MOEX ISS (честная 🔵-дата, время условно 🟡), иначе мунданный якорь.
    Раньше все бумаги вне реестра получали ОДИН мунданный якорь — одна и та
    же волна на разных акциях («повторяет сюжеты»); теперь у каждой своя."""
    root = "".join(ch for ch in (ticker or "").upper() if ch.isalpha())[:4]
    for pref in (root, root[:3], root[:2]):
        if pref in GENESIS:
            g = dict(GENESIS[pref])
            g["key"] = pref
            g["fallback"] = False
            return g
    if issue_date:
        try:
            y, m, d = (int(x) for x in str(issue_date)[:10].split("-"))
            return {"genesis": (y, m, d, 7, 0), "band": 72,
                    "key": f"ISSUE:{root or ticker[:6]}",
                    "fallback": False, "issue": True,
                    "note": (f"генезис = дата выпуска бумаги {issue_date} "
                             "(карточка MOEX ISS 🔵); время 07:00 UTC — "
                             "открытие торгов, условность 🟡")}
        except (ValueError, AttributeError):
            pass
    g = dict(GENESIS["MOEX_MUNDANE"])
    g["key"] = "MOEX_MUNDANE"
    g["fallback"] = True
    g["note"] = ("генезис тикера не в реестре и дата выпуска не пришла — "
                 "взят мунданный якорь Московской биржи (честная условность "
                 "🟡); точный генезис можно добавить в aether.GENESIS")
    return g


def compute_context(ticker: str,
                    candles: dict | None = None,
                    now: datetime | None = None,
                    horizon_h: float | None = None,
                    step_min: float | None = None,
                    interval: str = "1h",
                    genesis_date: str | None = None) -> dict:
    """Эфирный контекст инструмента.

    candles: {"day": [[ts,o,h,l,c,v]...] 5м/10м за 1-2 суток,
              "week": [...] часовые ~7 дней, "month": [...] дневные ~30-60}
    (любой поднабор; ts — unix-секунды UTC). Нет свечей → небесная часть без
    сцепки/плит. Нет эфемерид → mode="unavailable" (честно)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    _h, _s, _ = tf_params(interval)
    if horizon_h is None:
        horizon_h = _h
    if step_min is None:
        step_min = _s
    g = genesis_for(ticker, genesis_date)
    nat = natal_lons(g["genesis"])
    if nat is None:
        return {"mode": "unavailable", "ticker": ticker,
                "reason": "нет эфемерид (.bsp) — эфирный слой честно молчит",
                "frame": FRAME}
    # окно ЕДЕТ с шагом волны (не с часом!): квантование к часу морозило небо
    # на 59 минут — картинка «повторяла сюжет» до скачка. Теперь каждые
    # step_min минут окно сдвигается, кэш транзитов работает внутри шага.
    step_s = float(step_min) * 60.0
    t0 = datetime.fromtimestamp(
        math.floor((now - timedelta(hours=horizon_h)).timestamp() / step_s)
        * step_s, timezone.utc)
    span_h = horizon_h * 2.0
    trans = transit_series(t0, span_h, step_min / 60.0)
    if trans is None:
        return {"mode": "unavailable", "ticker": ticker,
                "reason": "эфемериды не отвечают", "frame": FRAME}
    psi = psi_of(trans, nat, g["band"])
    usd = psi_of(trans, natal_lons(USD_GENESIS), USD_BAND)
    k_now = int(np.argmin(np.abs(trans["ts"] - now.timestamp())))
    # длинное окно (канон REAL SKY: ±30 дней, шаг 6 ч) — для недели/месяца
    t0_long = (now - timedelta(days=30)).replace(hour=0, minute=0, second=0,
                                                 microsecond=0)
    trans_long = transit_series(t0_long, 60 * 24.0, 6.0)
    psi_long = psi_of(trans_long, nat, g["band"]) if trans_long else None
    usd_long = psi_of(trans_long, natal_lons(USD_GENESIS), USD_BAND) \
        if trans_long else None
    k_now_long = (int(np.argmin(np.abs(trans_long["ts"] - now.timestamp())))
                  if trans_long else 0)

    ctx = {"mode": "precise", "ticker": ticker, "ts": now.isoformat(),
           "genesis": {"key": g["key"], "when": "-".join(str(x) for x in g["genesis"][:3]),
                       "band": g["band"], "note": g["note"],
                       "fallback": bool(g.get("fallback"))},
           "wave": {"ts": trans["ts"].tolist(),
                    "psi": [round(float(x), 4) for x in psi],
                    "usd": [round(float(x), 4) for x in usd],
                    "now_idx": k_now,
                    "now_val": round(float(psi[k_now]), 4),
                    "note": ("вес тела — фотометрия (альбедо·апертура²), "
                             "частоты — реальные dλ/dt 🔵; ступени лесенки и "
                             "AFFINITY больше не источник мощи — вердикт "
                             "мировоззрения 05.08.2026; band — метка школы 🟡; "
                             "прокси: фазовый угол опущен, Солнце по апертуре")},
           "frame": FRAME}
    if trans_long is not None and psi_long is not None:
        ctx["wave_long"] = {"ts": trans_long["ts"].tolist(),
                            "psi": [round(float(x), 4) for x in psi_long],
                            "now_idx": k_now_long}

    # фиат-фазы: актив против линейки доллара (перенос прохода 8) —
    # на ДЛИННОМ окне (месячное дыхание), фолбэк — суточное
    psi_f = psi_long if psi_long is not None else psi
    usd_f = usd_long if usd_long is not None else usd
    edge = max(3, len(psi_f) // EDGE_FRAC)
    ph_a = np.unwrap(np.angle(_analytic(psi_f)))
    ph_u = np.unwrap(np.angle(_analytic(usd_f)))
    fp, fs = _plv(ph_a, ph_u, edge)
    ctx["fiat"] = {"plv": round(fp, 3), "sync_now": round(fs, 3),
                   "word": ("дышит вместе с фиатной линейкой"
                            if fp >= PLV_LOCK else "дышит сам, мимо линейки")}

    # ── старшие ключи: Гребенников (полости) · Штейнмец-Доллард (версор) ·
    # Кили (аккорды) — каждый в своей защите, сбой не роняет разбор ──
    try:
        ctx["keys"] = {"cavity": cavities_of(trans, k_now),
                       "keely": keely_of(trans, k_now),
                       "versor": versor_of(psi, step_min / 60.0, k_now,
                                           trans["ts"])}
    except Exception as e:
        ctx["keys"] = {"error": str(e)[:120]}

    # ── ценовая часть по масштабам ──
    scales = {}
    now_price = None
    for scale in ("day", "week", "month"):
        rows = (candles or {}).get(scale) or []
        ts_p, cl_p = _rows_ts_close(rows)
        if cl_p:
            now_price = float(cl_p[-1])
        pl = plates_of(cl_p)
        # день цепляется к тонкому окну (±24ч, 30мин), неделя/месяц — к
        # длинному (±30 дней, 6ч — канон окна REAL SKY)
        if scale == "day" or trans_long is None:
            w_ts, w_psi, w_k = trans["ts"], psi, k_now
        else:
            w_ts, w_psi, w_k = trans_long["ts"], psi_long, k_now_long
        cp = couple_wave_price(w_ts, w_psi, ts_p, cl_p) if pl else None
        if cp is not None:
            cp["mirror_raw"] = cp["mirror"]
            # два зеркала (стенд v3.1.1): ВПЕРЁД — мгновенная сырая фаза
            # (знак будущего лучше: 0.49 против 0.44 у corr); НАЗАД — фаза
            # окна по corr (наложение прошлого лучше: 0.30 против 0.25).
            # Разошлись — это не баг, а информация: флип виден у «сейчас».
            cp["mirror"] = _mirror_hyst(f"{ticker}|{interval}|{scale}",
                                        cp.get("corr"), cp["mirror"],
                                        m_inst=cp.get("m_inst"))
            cp["mirror_past"] = _mirror_hyst(f"{ticker}|{interval}|{scale}|past",
                                             cp.get("corr"), cp["mirror_raw"])
        pot = None
        if cp is not None:
            fwd = w_psi[w_k:]
            pot = potential_of(fwd, cp, pl, now_price)
            if pot is not None:
                pot = dict(pot)
                pot["center"] = [round(float(x), 4) for x in pot["center"]]
        scales[scale] = {"plates": pl, "couple": cp, "potential": pot,
                         "bars": len(rows)}
    ctx["scales"] = scales
    ctx["now_price"] = now_price

    # ── скальп-блок (проходы 8/11): тайминг и пульс внутри суток ──
    try:
        day_rows = (candles or {}).get("day") or []
        ts_d, cl_d = _rows_ts_close(day_rows)
        scalp = {
            "r_sky": round(kuramoto_r(trans, k_now), 3),
            "windows": [dict(w, when=datetime.fromtimestamp(
                w["t_exact"], timezone.utc).strftime("%d.%m %H:%M"))
                for w in dirac_windows(trans, g["band"])],
            "capture": adler_capture(trans, nat, k_now),
        }
        an = wave_antinodes(trans["ts"], psi, k_now)
        for w in an["forward"]:
            w["when"] = datetime.fromtimestamp(
                w["t"], timezone.utc).strftime("%d.%m %H:%M")
        scalp["antinodes"] = an
        if cl_d:
            scalp["pulse"] = price_pulse(ts_d, cl_d)
            scalp["plv_roll"] = rolling_plv(trans["ts"], psi, ts_d, cl_d)
            step_real = _step_min_of(day_rows)
            scalp["energy"] = energy_pulse(ts_d, cl_d, step_real)
            scalp["vprofile"] = volume_profile(day_rows)
            scalp["vwap"] = session_vwap(day_rows)
            # плиты — по ДЛИННОЙ волне × неделя/месяц (у суточной волны в
            # перекрытии с прошлым может не быть экстремумов — это не сбой);
            # добор: суточная волна × свечи дня
            _wk_rows = (candles or {}).get("week") or (candles or {}).get("month") or []
            _wts, _wcl = _rows_ts_close(_wk_rows)
            plates_a = []
            if trans_long is not None and _wcl:
                plates_a = aether_plates(trans_long["ts"], psi_long, _wts, _wcl)
            if len(plates_a) < 2:
                plates_a += [x for x in aether_plates(trans["ts"], psi, ts_d, cl_d)
                             if all(abs(x["price"] - y["price"]) > 1e-9
                                    for y in plates_a)]
            scalp["plates_aether"] = plates_a[:4]
            # микро-плиты скальпа: гребни/узлы тонкой волны + минуты пучностей
            # энергии, мелкая сетка, свежесть в весе — уровни едут с ценой
            extras = []
            en_p = scalp.get("energy") or {}
            if en_p.get("antinodes"):
                ts_arr = np.asarray(ts_d, float)
                cl_arr = np.asarray(cl_d, float)
                for t_a, v_a in en_p["antinodes"]:
                    j = int(np.argmin(np.abs(ts_arr - t_a)))
                    extras.append((float(cl_arr[j]), float(v_a), float(t_a)))
            scalp["plates_micro"] = micro_plates(trans["ts"], psi, ts_d, cl_d,
                                                 now_price, extra_marks=extras)
            scalp["effort"] = effort_result(day_rows)
            # минутный скальп v2.8: заряд толпы + режим Элерса
            cs_v, vs_v = _rows_cv(day_rows)
            scalp["charge"] = charge_of(
                cs_v, vs_v, (scalp.get("energy") or {}).get("now"),
                (scalp.get("effort") or {}).get("d"))
            scalp["ebs"] = ebs_of(day_rows)
            scalp["implosion"] = implosion_of(day_rows)
            scalp["voids"] = voids_of(day_rows)
            scalp["fractal"] = fractal_resonance(cl_d)
        scalp["chirikov"] = chirikov_of(scalp.get("windows"))
        scalp["interval"] = interval
        ctx["scalp"] = scalp
    except Exception as e:
        ctx["scalp"] = {"error": str(e)[:120]}

    # светофор поля — сборка готовых частей, в своей защите
    try:
        ctx["field"] = field_state(ctx)
    except Exception as e:
        ctx["field"] = {"error": str(e)[:120]}

    # ── глубинный эфир (документ владельца, полное принятие) — модуль рядом ──
    try:
        from . import aether_deep
        deep = {"simplex": aether_deep.simplex_of(
            t0, span_h, step_min / 60.0, k_now)}
        if "beta" in trans:
            deep["scalar"] = aether_deep.scalar_stress(
                trans["lam"], trans["beta"], trans["E"], k_now)
        sc_d = ctx.get("scalp")
        if isinstance(sc_d, dict) and not sc_d.get("error"):
            day_rows2 = (candles or {}).get("day") or []
            cs2, vs2 = _rows_cv(day_rows2)
            deep["hurst"] = aether_deep.hurst_memory(cs2)
            # аудит №3: топология пустоты зависит от памяти среды — H<0.5
            # среда огрызается (полость втягивает возвратом), H>0.5 тренд
            # держит колею (полость — пройденная броня, опора/крышка)
            H_v = (deep.get("hurst") or {}).get("h")
            if H_v is not None and sc_d.get("voids"):
                kind_v = ("вакуум-магнит: возврат втянет" if H_v < 0.5
                          else "броня: опора/крышка на пути")
                for v_ in sc_d["voids"]:
                    v_["kind"] = kind_v
            deep["kozyrev"] = aether_deep.kozyrev_density(vs2)
            cpd2 = (scales.get("day") or {}).get("couple")
            ts_d2, cl_d2 = _rows_ts_close(day_rows2)
            if cpd2 and len(cl_d2) >= 24:
                grad = np.gradient(np.asarray(psi[:k_now + 1], float))
                if cpd2.get("mirror"):
                    grad = -grad
                pr2 = np.interp(trans["ts"][:k_now + 1], ts_d2, cl_d2)
                deep["shadow"] = aether_deep.shadow_imbalance(
                    grad[1:], np.diff(pr2))
        ctx["deep"] = deep
    except Exception as e:
        ctx["deep"] = {"error": str(e)[:120]}

    # ── створы: окна фазы вперёд (веер v3.0) — свой try/except ──
    try:
        from . import gates as _g
        sc_g = ctx.get("scalp") if isinstance(ctx.get("scalp"), dict) else {}
        day_g = (ctx.get("scales") or {}).get("day") or {}
        cpd, potd = day_g.get("couple"), day_g.get("potential")
        pl_g = day_g.get("plates") or {}
        level_items = [(float(x["price"]), "Э⚡")
                       for x in (sc_g.get("plates_aether") or [])]
        level_items += [(float(x["price"]), "Эµ")
                        for x in (sc_g.get("plates_micro") or [])]
        for kq in ("floor", "mid", "ceil"):
            if pl_g.get(kq) is not None:
                level_items.append((float(pl_g[kq]), "квантиль"))
        for v_ in (sc_g.get("voids") or []):
            level_items.append((float(v_["lo"]), "край полости"))
            level_items.append((float(v_["hi"]), "край полости"))
        ver_g = (ctx.get("keys") or {}).get("versor") or {}
        span_g = max(float(pl_g.get("hi", 1.0)) - float(pl_g.get("lo", 0.0)),
                     1e-9)
        if sc_g and not sc_g.get("error"):
            fr_g = (sc_g.get("fractal") or {}).get("r")
            sc_g["gates"] = _g.gates_of(
                trans["ts"], psi, k_now,
                (potd or {}).get("center"), (cpd or {}).get("plv"),
                sc_g.get("windows") or [], level_items, span_g,
                bool(ver_g.get("tension")),
                float(ver_g.get("period_h") or 24.0), interval,
                frac01=fr_g, chaos=sc_g.get("chirikov"))
        if trans_long is not None and psi_long is not None:
            mo = (ctx.get("scales") or {}).get("month") or {}
            wk = (ctx.get("scales") or {}).get("week") or {}
            src = mo if (mo.get("couple") and mo.get("potential")) else wk
            pl_far = src.get("plates") or pl_g
            lv_far = [(float(x["price"]), "Э⚡")
                      for x in (sc_g.get("plates_aether") or [])]
            for kq in ("floor", "mid", "ceil"):
                if pl_far.get(kq) is not None:
                    lv_far.append((float(pl_far[kq]), "квантиль"))
            span_far = max(float(pl_far.get("hi", 1.0))
                           - float(pl_far.get("lo", 0.0)), 1e-9)
            ctx["gates_far"] = _g.far_gates(
                trans_long["ts"], psi_long, k_now_long,
                (src.get("potential") or {}).get("center"),
                (src.get("couple") or {}).get("plv"),
                dirac_windows(trans_long, g["band"]), lv_far, span_far)
    except Exception as e:
        ctx["gates_error"] = str(e)[:120]

    # сводный вердикт (язык школы, без торговых слов)
    day = scales.get("day") or {}
    cp = day.get("couple")
    pot = day.get("potential")
    bits = []
    if cp:
        bits.append(f"сцепка эфир↔цена (день): PLV {cp['plv']}"
                    + (" · ЗЕРКАЛО" if cp.get("mirror") else ""))
    if pot:
        bits.append(f"вверх: {pot['word_up']} · вниз: {pot['word_dn']}")
    if not bits:
        bits.append("цены не поданы — только небесная волна")
    ctx["verdict"] = " · ".join(bits)
    return ctx


def render_for_ai(ctx: dict) -> str:
    """Блок для ИИ. НЕ дублирует астро-блок (планеты/аспекты/станции — там);
    здесь ТОЛЬКО волна×цена: плиты, сцепка, потенциал, окна волны."""
    if ctx.get("mode") != "precise":
        return ("[ЭФИР недоступен: " + str(ctx.get("reason", "?")) + "]\n" + FRAME)
    g = ctx["genesis"]
    L = [f"ЭФИРНЫЙ СЛОЙ (движок REAL SKY; генезис {g['key']} {g['when']}, "
         f"band {g['band']} — метка школы, не вес"
         + (" — МУНДАННЫЙ ФОЛБЭК, генезис тикера не в реестре" if g["fallback"] else "")
         + ")",
         f"Волна Ψ сейчас: {ctx['wave']['now_val']} (0..1, внутриоконная линейка)."
         f" Фиат-линейка: {ctx['fiat']['word']} (PLV {ctx['fiat']['plv']}).", ""]
    names = {"day": "ДЕНЬ (внутри суток)", "week": "НЕДЕЛЯ", "month": "МЕСЯЦ"}
    for key in ("day", "week", "month"):
        sc = (ctx.get("scales") or {}).get(key) or {}
        pl, cp, pot = sc.get("plates"), sc.get("couple"), sc.get("potential")
        if not pl:
            continue
        row = [f"{names[key]} [{sc.get('bars', 0)} свечей]: плиты цены "
               f"{pl['floor']:.2f} / {pl['mid']:.2f} / {pl['ceil']:.2f} "
               f"(края окна {pl['lo']:.2f}–{pl['hi']:.2f})"]
        if cp:
            row.append(f"  сцепка эфир↔цена: PLV {cp['plv']} ({cp['lock']})"
                       + (f"; corr {cp['corr']} — ЗЕРКАЛЬНАЯ фаза окна, проекция "
                          f"перевёрнута честно" if cp.get("mirror") else
                          (f"; corr {cp['corr']}" if cp.get("corr") is not None else "")))
        if pot:
            row.append(f"  проекция волны в шкале цены: вверх до ~{pot['up_target']:.2f} "
                       f"({pot['word_up']}), вниз до ~{pot['dn_target']:.2f} "
                       f"({pot['word_dn']}); полоса неопределённости "
                       f"±{pot['half_band']:.2f} (растёт при слабой сцепке)")
        L += row
    if ctx.get("now_price") is not None:
        L.append(f"Цена сейчас: {ctx['now_price']:.2f}.")
    sc = ctx.get("scalp") or {}
    if sc and not sc.get("error"):
        L += ["", f"ВНУТРИ СУТОК (скальп-тайминг): когерентность неба r={sc.get('r_sky')}."]
        if sc.get("windows"):
            L.append("  Узлы времени (точные углы пар, зона ± время "
                     "прохождения орба парой):")
            for w in sc["windows"][:6]:
                L.append(f"    {w['when']} UTC — {w['pair']} {w['aspect']} "
                         f"(±{w['sigma_min']} мин)")
        cap = sc.get("capture") or {}
        if cap:
            L.append(f"  Захват дня (Адлер): {cap.get('word')}")
        pu = sc.get("pulse")
        if pu:
            L.append(f"  Пульс цены: {pu['word']} (dA/dt {pu['speed']}, "
                     f"d²A/dt² {pu['accel']})")
        rp = sc.get("plv_roll")
        if rp:
            L.append(f"  Сцепка сейчас (скользящее окно): {rp['now']} против "
                     f"медианы {rp['median']} — {rp['trend']}")
        imp = sc.get("implosion")
        if imp and imp.get("active"):
            L.append(f"  {imp['word']} — уровень {imp['price']} "
                     f"(Z {imp['z']} против P99 {imp['p99']})")
        vds = sc.get("voids") or []
        if vds:
            L.append("  ЭФИРНЫЕ ПОЛОСТИ (незаполненные разрывы — «пустота, "
                     "куда среда тянется» 🟡, факт баров 🔵): " + ", ".join(
                         f"{z['lo']}–{z['hi']} ({z['dir']})" for z in vds))
        chg = sc.get("charge")
        if chg:
            L.append(f"  ЗАРЯД ТОЛПЫ: {chg['charge']:+.2f} — {chg['word']} "
                     f"(направление {chg['dir']:+.2f} 🔵 факт баров, "
                     f"сила {chg['strength']})")
        ebx = sc.get("ebs")
        if ebx:
            L.append(f"  Режим Элерса (EBS): {ebx['mode']} "
                     f"(EBS {ebx['now']:+.2f}) — режим фильтра, не направление ⚫")
        en = sc.get("energy")
        if en:
            L.append(f"  ЭНЕРГИЯ сейчас: {en['now']} — {en['word']}"
                     + (f"; качели ≈{en['dominant_min']} мин (полуход "
                        f"~{en['half_swing_min']} мин)" if en.get('dominant_min') else "")
                     + f"; доверие глубине {en['trust_frac']}")
        vp = sc.get("vprofile")
        if vp:
            L.append(f"  Толпа (профиль объёма): POC {vp['poc']} · зона "
                     f"ценности {vp['val']}–{vp['vah']}")
        vw = sc.get("vwap")
        if vw:
            L.append(f"  VWAP сессии: {vw['vwap']} (±1σ {vw['dn1']}–{vw['up1']}, "
                     f"±2σ {vw['dn2']}–{vw['up2']})")
        pa = sc.get("plates_aether")
        if pa:
            L.append("  ЭФИРНЫЕ ПЛИТЫ (цена гребней/впадин волны — где пройти "
                     "сложно 🟡): " + ", ".join(
                         f"{x['price']} (вес {x['weight']})" for x in pa))
        pm = sc.get("plates_micro")
        if pm:
            L.append("  МИКРО-ПЛИТЫ скальпа (гребни/узлы тонкой волны 🟡 + "
                     "минуты пучностей энергии 🔵; вес × свежесть, ближние к "
                     "цене первыми): " + ", ".join(
                         f"{x['price']} ({x['age_min']} мин назад, вес "
                         f"{x['weight']})" for x in pm))
        ana = sc.get("antinodes") or {}
        if ana.get("now"):
            L.append(f"  Волна сейчас: {ana['now']} (пучность = |2Ψ−1|≥0.35, "
                     "единый порог школы 🟡).")
        anf = ana.get("forward") or []
        if anf:
            L.append("  ПУЧНОСТИ волны впереди (насыщение фазы, не событие 🟡): "
                     + ", ".join(
                         f"{w.get('when', w['t'])} UTC {w['event']} "
                         f"({w['kind']}, амп {w['amp']})" for w in anf[:4]))
        ena = (sc.get("energy") or {}).get("antinodes") or []
        if ena:
            L.append("  Пучности ЭНЕРГИИ за окно (максимумы CWT-ранга ≥0.7 🔵): "
                     + ", ".join(datetime.fromtimestamp(
                         t, timezone.utc).strftime("%H:%M") for t, _ in ena)
                     + " UTC")
    ky = ctx.get("keys") or {}
    if ky and not ky.get("error"):
        L += ["", "СТАРШИЕ КЛЮЧИ (Гребенников · Штейнмец-Доллард · Кили):"]
        cav = ky.get("cavity") or {}
        figs = cav.get("figures") or []
        for f in figs[:2]:
            L.append(f"  Полость: {f['name']} [{'·'.join(f['bodies'])}] "
                     f"Q {f['q']} · наполнение {f['fill']}")
        if not figs:
            L.append("  Полостей нет — небо без сот, форма молчит.")
        ver = ky.get("versor") or {}
        if ver.get("none"):
            L.append("  Версор: волна не колеблется — честное молчание.")
        elif ver:
            L.append(f"  Версор Ψ: E_d {ver.get('ed')} / M_k {ver.get('mk')} — "
                     f"{ver.get('word')}")
        ke = ky.get("keely") or {}
        if ke:
            L.append(f"  Аккорд Кили: {ke.get('word')}"
                     + ((" [" + " · ".join("+".join(c) for c in ke["chords"])
                         + f"; R {ke.get('r')}]") if ke.get("chords") else ""))
        L.append("  (полость/версор/аккорд — язык школы 🟡 на геометрии "
                 "эфемерид 🔵; не событие и не сигнал ⚫)")
    fld = ctx.get("field")
    if fld and not fld.get("error"):
        L.append("")
        L.append(f"СВЕТОФОР ПОЛЯ: {fld['score']} — {fld['word']} (части: "
                 + ", ".join(f"{k} {v}" for k, v in fld["parts"].items())
                 + "; веса именованы в note, не сигнал ⚫)")
    eff = (ctx.get("scalp") or {}).get("effort")
    if eff and abs(eff.get("d", 0)) >= EFFORT_THR:
        L.append(f"УСИЛИЕ/РЕЗУЛЬТАТ: D {eff['d']} — {eff['word']}")
    gts = (ctx.get("scalp") or {}).get("gates") or []
    if gts:
        L.append("")
        L.append("СТВОРЫ (окна фазы вперёд — состояние поля, не приказ ⚫):")
        for g_ in gts:
            L.append(f"  {g_['when']} UTC — {g_['word']} (score {g_['score']})")
    gf = ctx.get("gates_far") or []
    if gf:
        L.append("ДАЛЬНИЕ ОКНА (длинная волна ±30 дней): " + "; ".join(
            f"{g_['when']} {g_['kind']}"
            + (f" у {g_['target']}" if g_.get("target") is not None else "")
            for g_ in gf))
    dp = ctx.get("deep") or {}
    if dp and not dp.get("error"):
        rows_d = [(k_, t_) for k_, t_ in (
            ("simplex", "Симплекс 3D"), ("scalar", "Скалярное напряжение"),
            ("shadow", "Теневой дисбаланс"), ("hurst", "Память Хёрста"),
            ("kozyrev", "Плотность времени")) if dp.get(k_)]
        if rows_d:
            L.append("")
            L.append("ГЛУБИННЫЙ ЭФИР:")
            for k_, ttl in rows_d:
                L.append(f"  {ttl}: {dp[k_].get('word')}")
    L += ["",
          "ПРИМЕНЕНИЕ: это контекст-фильтр волновой фазы, а не указание к сделке. "
          "Плиты — статистика реальной цены 🔵; волна и проекция — язык школы 🟡; "
          "слабая/зеркальная сцепка = волне в этом окне доверия меньше — скажи это "
          "прямо в выводе. " + FRAME]
    return "\n".join(L)


def chart_payload(ctx: dict) -> dict:
    """Серии для графика (lightweight-charts): эфир прошлого в шкале цены,
    проекция вперёд (центр + полоса), плиты, вертикаль «сейчас»."""
    if ctx.get("mode") != "precise":
        return {"available": False, "reason": ctx.get("reason")}
    sc = (ctx.get("scales") or {}).get("day") or {}
    cp, pot, pl = sc.get("couple"), sc.get("potential"), sc.get("plates")
    w = ctx["wave"]
    out = {"available": True, "frame": FRAME,
           "now_ts": int(w["ts"][w["now_idx"]]),
           "plates": pl, "fiat": ctx.get("fiat"),
           "verdict": ctx.get("verdict"),
           "windows": [{"t": int(x["t_exact"]), "pair": x["pair"],
                        "aspect": x["aspect"], "sigma_min": x["sigma_min"]}
                       for x in ((ctx.get("scalp") or {}).get("windows") or [])],
           "energy": ((ctx.get("scalp") or {}).get("energy") or {}).get("series"),
           "energy_word": ((ctx.get("scalp") or {}).get("energy") or {}).get("word"),
           "plates_aether": (ctx.get("scalp") or {}).get("plates_aether") or [],
           "plates_micro": (ctx.get("scalp") or {}).get("plates_micro") or [],
           "antinodes": (ctx.get("scalp") or {}).get("antinodes") or {},
           "energy_antinodes": ((ctx.get("scalp") or {}).get("energy")
                                or {}).get("antinodes") or [],
           "vprofile": (ctx.get("scalp") or {}).get("vprofile"),
           "vwap": (ctx.get("scalp") or {}).get("vwap"),
           "line": short_line(ctx)}
    sc_all = ctx.get("scalp") or {}
    out["interval"] = sc_all.get("interval")     # фронт сверяет со своим
    out["computed_at"] = ctx.get("ts")
    en_all = sc_all.get("energy") or {}
    out["dominant_min"] = en_all.get("dominant_min")
    out["half_swing_min"] = en_all.get("half_swing_min")
    out["energy_now"] = en_all.get("now")     # явный «сейчас» для чипа ⚡
    out["energy_trust"] = en_all.get("trust_frac")
    out["charge"] = sc_all.get("charge")      # заряд толпы (минутный скальп)
    out["ebs"] = sc_all.get("ebs")            # режим Элерса рельс/качели
    out["genesis"] = ctx.get("genesis")       # чей якорь у волны — видно в панели
    out["implosion"] = sc_all.get("implosion")  # стена импеданса (имплозия)
    out["voids"] = sc_all.get("voids") or []    # незаполненные полости
    out["gates"] = sc_all.get("gates") or []    # створы: окна фазы вперёд
    out["gates_far"] = ctx.get("gates_far") or []
    out["fractal"] = sc_all.get("fractal")      # склейка масштабов (аудит №3)
    out["chirikov"] = sc_all.get("chirikov") or []  # окна хаоса
    dp_all = ctx.get("deep") or {}
    out["deep"] = dp_all if not dp_all.get("error") else None
    # дальняя линия: длинная волна в шкале цены week/month-сцепки (веер v3.0)
    wl = ctx.get("wave_long")
    far_sc = far_src = None
    for key2 in ("week", "month"):
        s2 = (ctx.get("scales") or {}).get(key2) or {}
        if s2.get("couple") and s2.get("potential"):
            far_sc, far_src = s2, key2
            break
    if wl and far_sc:
        cp2, pot2 = far_sc["couple"], far_sc["potential"]
        ts_l = np.asarray(wl["ts"], float)
        psi_l = np.asarray(wl["psi"], float)
        if cp2.get("mirror"):
            psi_l = cp2.get("w_lo", 0.0) + cp2.get("w_hi", 1.0) - psi_l
        center_l = cp2["affine_a"] * psi_l + cp2["affine_b"]
        half2 = float(pot2["half_band"])
        step_far = max(_IV_SEC.get(out.get("interval") or "1h", 3600), 3600)
        now_ts2 = float(w["ts"][w["now_idx"]])
        grid = np.arange(math.ceil(now_ts2 / step_far) * step_far,
                         float(ts_l[-1]) + 1.0, step_far)[:1500]
        if grid.size >= 2:
            c_g = np.interp(grid, ts_l, center_l)
            out["ether_far"] = [[int(tt), round(float(cc), 4),
                                 round(float(cc - half2), 4),
                                 round(float(cc + half2), 4)]
                                for tt, cc in zip(grid, c_g)]
            out["far_plv"] = cp2["plv"]
            out["far_mirror"] = bool(cp2.get("mirror"))
            out["far_src"] = far_src
    fld = ctx.get("field")
    out["field"] = fld if (fld and not fld.get("error")) else None
    eff = sc_all.get("effort")
    out["effort"] = eff if (eff and abs(eff.get("d", 0)) >= EFFORT_THR) else None
    ver_k = (ctx.get("keys") or {}).get("versor") or {}
    out["tension_zones"] = ver_k.get("zones") or []
    out["tension_now"] = bool(ver_k.get("tension"))
    # ближние плиты сверху/снизу — для чипов дистанции
    npc = ctx.get("now_price")
    allp = ((sc_all.get("plates_micro") or []) +
            (sc_all.get("plates_aether") or []))
    if npc is not None and allp:
        ups = [p["price"] for p in allp if p["price"] > npc]
        dns = [p["price"] for p in allp if p["price"] < npc]
        out["plate_up"] = round(min(ups), 4) if ups else None
        out["plate_dn"] = round(max(dns), 4) if dns else None
    # старшие ключи — чип только когда звучат
    ky = ctx.get("keys") or {}
    cavf = ((ky.get("cavity") or {}).get("figures") or [])
    kel = ky.get("keely") or {}
    ksum = {}
    if cavf:
        ksum["cavity"] = {"name": cavf[0]["name"], "q": cavf[0]["q"]}
    if kel.get("chords"):
        ksum["keely"] = {"r": kel.get("r"),
                         "best": (kel.get("pairs") or [{}])[0].get("pair")}
    if kel.get("tritones"):
        ksum["tritone"] = kel["tritones"][0]["pair"]
    if kel.get("phi"):
        ksum["phi"] = kel["phi"][0]["pair"]
    out["keys"] = ksum or None
    if cp and pot:
        a, b = cp["affine_a"], cp["affine_b"]
        psi = np.asarray(w["psi"], dtype=float)
        if cp.get("mirror"):
            # флип вокруг середины окна калибровки (см. potential_of)
            psi = cp.get("w_lo", 0.0) + cp.get("w_hi", 1.0) - psi
        mapped = a * psi + b
        k = w["now_idx"]
        psi_p = np.asarray(w["psi"], dtype=float)
        if cp.get("mirror_past", cp.get("mirror")):
            psi_p = cp.get("w_lo", 0.0) + cp.get("w_hi", 1.0) - psi_p
        mapped_p = a * psi_p + b        # прошлое — в фазе своего окна (corr)
        out["ether_past"] = [[int(t), round(float(v), 4)]
                             for t, v in zip(w["ts"][:k + 1], mapped_p[:k + 1])]
        out["mirror_past"] = bool(cp.get("mirror_past", cp.get("mirror")))
        out["mirror_split"] = bool(out["mirror_past"] != bool(cp.get("mirror")))
        # трубка вероятности (аудит №3): не линия-приговор, а квантовый
        # коридор. σ = полполосы неуверенности; 68% = ±σ, 99% = ±2.3σ.
        # PLV→1 сжимает трубку в луч, хаос честно раздувает её в облако.
        sig = float(pot["half_band"]) * 0.5
        out["ether_forward"] = [[int(t), round(float(v), 4),
                                 round(float(v - sig), 4), round(float(v + sig), 4),
                                 round(float(v - 2.3 * sig), 4),
                                 round(float(v + 2.3 * sig), 4)]
                                for t, v in zip(w["ts"][k:], mapped[k:])]
        out["plv"] = cp["plv"]
        out["mirror"] = bool(cp.get("mirror"))
        out["m_inst"] = cp.get("m_inst")       # мгновенная синфазность
        out["winding"] = cp.get("winding")     # обмотка W, обороты
        wnd = cp.get("winding")
        if wnd is not None:
            if abs(wnd) < 0.5:
                out["winding_word"] = "струна расслаблена — цена дышит с эфиром"
            elif wnd > 0:
                out["winding_word"] = (f"перегрев +{abs(wnd):.1f} об. — цена "
                                       "накрутила фазу поверх эфира")
            else:
                out["winding_word"] = (f"пружина −{abs(wnd):.1f} об. — эфир "
                                       "ушёл вперёд, разрядка догонит")
    else:
        out["ether_raw"] = [[int(t), v] for t, v in zip(w["ts"], w["psi"])]
    return out


def short_line(ctx: dict) -> str:
    if ctx.get("mode") != "precise":
        return "эфир: нет эфемерид"
    sc = (ctx.get("scales") or {}).get("day") or {}
    cp = sc.get("couple")
    bits = [f"Ψ {ctx['wave']['now_val']}"]
    if cp:
        bits.append(f"PLV {cp['plv']}" + ("· зеркало" if cp.get("mirror") else ""))
    bits.append(ctx["fiat"]["word"].split(",")[0])
    if (ctx.get("genesis") or {}).get("fallback"):
        bits.append("якорь мунданный")   # у бумаги нет своего генезиса — честно
    return "эфир: " + " · ".join(bits)


# кэш контекста по тикеру (расчёт — секунды CPU)
_CTX_CACHE: dict = {}
_CTX_LOCK = threading.Lock()
AETHER_CACHE_SEC = 300.0


def cached_context(ticker: str, candles: dict | None = None,
                   max_age: float = AETHER_CACHE_SEC,
                   interval: str = "1h",
                   genesis_date: str | None = None) -> dict:
    now = _time.time()
    # БАГ, найденный при экспериментах с генезисом: ключ был "тикер|интервал"
    # и НЕ включал genesis_date. Спрашиваешь тот же тикер с ДРУГОЙ датой
    # рождения — получаешь кэш, посчитанный по старой. Пять минут молча
    # неверного эфира. Генезис теперь часть ключа.
    key = f"{ticker}|{interval}|{genesis_date or '-'}"
    with _CTX_LOCK:
        hit = _CTX_CACHE.get(key)
        if hit and now - hit[0] < max_age and candles is None:
            return hit[1]
    ctx = compute_context(ticker, candles, interval=interval,
                          genesis_date=genesis_date)
    with _CTX_LOCK:
        if len(_CTX_CACHE) >= 16:
            for old in sorted(_CTX_CACHE, key=lambda k: _CTX_CACHE[k][0])[:-15]:
                _CTX_CACHE.pop(old, None)
        _CTX_CACHE[key] = (_time.time(), ctx)
    return ctx


def purge_caches(what: str = "all") -> dict:
    """Чистка кэшей эфира. Нужна, когда меняется генезис, эфемериды или ключ.

    what: "ctx" — только готовые контексты (живут 5 минут),
          "sky" — транзиты и наталы (тяжёлые, но привязаны к датам),
          "all" — всё. Возвращает, сколько записей выброшено 🔵.
    """
    n = {}
    if what in ("ctx", "all"):
        with _CTX_LOCK:
            n["контексты"] = len(_CTX_CACHE); _CTX_CACHE.clear()
    if what in ("sky", "all"):
        with _TRANS_LOCK:
            n["транзиты"] = len(_TRANS_CACHE); _TRANS_CACHE.clear()
        n["наталы"] = len(_NATAL_CACHE); _NATAL_CACHE.clear()
    return n


async def acontext(ticker: str, candles: dict | None = None,
                   max_age: float = AETHER_CACHE_SEC,
                   interval: str = "1h",
                   genesis_date: str | None = None) -> dict:
    import asyncio
    return await asyncio.to_thread(cached_context, ticker, candles, max_age,
                                   interval, genesis_date)


# ══════════════════════════════════════════════════════════════════════
# SELF-TEST (детерминированный, сеть не нужна; эфемеридные блоки — если есть .bsp)
# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # 1) Гильберт (v2.9 — зеркальная добивка, кольцо FFT разорвано):
    #    H[cos]≈sin в ядре окна; правка честности: круговая «точность 1e-9»
    #    была артефактом кольца (конец склеен с началом)
    n = 256
    t = np.arange(n)
    x = np.cos(2 * np.pi * 8 * t / n)
    z = _analytic(x)
    core = slice(n // 8, -n // 8)
    assert np.max(np.abs(z.imag[core] - np.sin(2 * np.pi * 8 * t / n)[core])) < 0.02
    assert np.std(np.abs(z)[core]) < 0.01          # огибающая тона ≈ константа
    #    ПЕТЛЯ РАЗОРВАНА: тихий левый край не заражается правым тоном
    x_lp = np.concatenate([np.zeros(128), np.sin(2 * np.pi * np.arange(128) / 32)])
    assert float(np.abs(_analytic(x_lp)[:100]).mean()) < 0.05, "кольцо Фурье живо"

    # 2) плиты: порядок и края
    pl = plates_of([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert pl["lo"] <= pl["floor"] < pl["mid"] < pl["ceil"] <= pl["hi"]
    assert plates_of([1, 2, 3]) is None            # мало точек → честный None

    # 3) сцепка: волна с самой собой → PLV 1; с противофазой → PLV 1, corr −1
    #    и зеркальный флаг; с несоизмеримой — PLV низкий
    ts = np.arange(0, 200) * 600.0
    wave = 0.5 + 0.4 * np.sin(2 * np.pi * ts / (ts[-1] / 5.0))
    cp1 = couple_wave_price(ts, wave, ts, 100 + 10 * wave)
    assert cp1 and cp1["plv"] > 0.98 and cp1["corr"] > 0.99 and not cp1["mirror"]
    cp2 = couple_wave_price(ts, wave, ts, 100 - 10 * wave)
    assert cp2 and cp2["plv"] > 0.98 and cp2["corr"] < -0.99 and cp2["mirror"]
    wave2 = 0.5 + 0.4 * np.sin(2 * np.pi * ts / (ts[-1] / 7.3) + 1.1)
    cp3 = couple_wave_price(ts, wave2, ts, 100 + 10 * wave)
    assert cp3 and cp3["plv"] < cp1["plv"]

    # 4) аффинная калибровка: эфир 0..1 ложится в min-max цены окна
    mapped = cp1["affine_a"] * wave + cp1["affine_b"]
    assert abs(mapped.min() - (100 + 10 * wave).min()) < 1e-9
    assert abs(mapped.max() - (100 + 10 * wave).max()) < 1e-9

    # 5) потенциал: у зеркала проекция переворачивается; слова честные
    pl5 = {"floor": 95.0, "mid": 100.0, "ceil": 105.0, "lo": 92.0, "hi": 108.0, "n": 42}
    pot = potential_of(wave[-40:], cp1, pl5, 100.0)
    assert pot and 0.0 <= pot["live_up"] <= 1.0 and 0.0 <= pot["live_dn"] <= 1.0
    pot_m = potential_of(wave[-40:], cp2, pl5, 100.0)
    assert pot_m and pot_m["mirrored"]

    # 5б) адаптер свечей: dict с ISO-временем и список с миллисекундами — одно
    rows_d = [{"t": "2026-07-30T10:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 87.1, "v": 5},
              {"t": "2026-07-30T10:05:00+00:00", "o": 1, "h": 2, "l": 0.5, "c": 87.3, "v": 5}]
    ts_a, cl_a = _rows_ts_close(rows_d)
    assert len(ts_a) == 2 and cl_a == [87.1, 87.3] and ts_a[1] - ts_a[0] == 300.0
    rows_l = [[1785412800000, 1, 2, 0.5, 87.1, 5]]        # мс → с
    ts_b, _ = _rows_ts_close(rows_l)
    assert abs(ts_b[0] - 1785412800.0) < 1e-6
    assert _rows_ts_close([{"t": None, "c": 1}, "мусор"]) == ([], [])
    cd = candles_from_dossier({"candles_daily_tail": rows_d})
    assert cd["month"] == rows_d and cd["day"] == [] and cd["week"] == []

    # 5в) скальп-машинерия (проходы 8/11)
    #    импеданс — калибровка журнала REAL SKY байт-в-байт
    for th_, z_ in ((0.0, 0.0), (60.0, 0.91), (90.0, 1.30), (120.0, 0.69),
                    (180.0, 1.06)):
        assert abs(impedance(th_) - z_) < 0.005, (th_, impedance(th_))
    #    точки Дирака: синтетические λ — линейное схождение пары через 60°
    ts_syn = np.arange(0, 49) * 1800.0
    lam_syn = {nm: np.full(49, 200.0) for nm in _BODY_ORDER}
    lam_syn["Луна"] = 138.0 + 0.5 * np.arange(49)          # sep 62→38: секстиль на k=4
    tr_syn = {"ts": ts_syn, "lam": lam_syn,
              "E": {nm: np.ones(49) for nm in _BODY_ORDER}}
    dw = dirac_windows(tr_syn, 108, top=100)
    lun60 = [w for w in dw if "Луна" in w["names"] and w["aspect"] == "секстиль"]
    assert lun60 and abs(lun60[0]["t_exact"] - 7200.0) < 60.0, lun60[:1]
    #    дедуп: на пару×аспект — не больше одной записи в окне
    keys = [(w["names"], w["aspect"]) for w in dw]
    assert len(keys) == len(set(keys)), "дубли пар в окнах Дирака"
    #    v3.9: σ — кинематика, не нота band. Синтетическая Луна идёт
    #    0.5°/1800 с = 24°/сут → σ = 0.05°/24·1440 = 3 мин → кламп снизу 15.
    #    (было: assert sigma_min == 120 — инерция по ноте band 108, убита)
    assert lun60[0]["sigma_min"] == 15, lun60[0]
    #    медленная пара упирается в верхний кламп 120 мин
    lam_slow = {nm: np.full(49, 200.0) for nm in _BODY_ORDER}
    lam_slow["Луна"] = 139.9995 + 0.0000005 * np.arange(49)   # почти станция
    lam_slow["Солнце"] = np.full(49, 200.0)
    lam_slow["Марс"] = 260.0 - 0.0022 * np.arange(49)          # ~0.1°/сут к 60°
    dw_s = dirac_windows({"ts": ts_syn, "lam": lam_slow,
                          "E": {nm: np.ones(49) for nm in _BODY_ORDER}},
                         108, top=100)
    slow60 = [w for w in dw_s if "Марс" in w["names"] and "Луна" not in w["names"]]
    assert slow60 and all(w["sigma_min"] == 120 for w in slow60), slow60[:2]
    #    физика веса (v3.9): Луна на среднем расстоянии ≈ 1; монотонность
    #    по диску и по альбедо; Солнце — по апертуре; ω станции → 0
    assert abs(float(w_flux("Луна", 1.0)) - 1.0) < 1e-12
    assert float(w_flux("Луна", 1.1)) > float(w_flux("Луна", 1.0)) \
        > float(w_flux("Луна", 0.9))                       # ближе/больше диск → выше
    assert float(w_flux("Венера", 0.5)) > float(w_flux("Марс", 0.5))  # альбедо
    assert ALBEDO_G["Солнце"] == 1.0                       # апертура, честный note
    assert _omega_of(0.0) == 0.0                           # станция не крутит фазу
    assert _omega_of(13.2) > _omega_of(1.0) > 0.0          # монотонность ω
    assert abs(_omega_of(-1.0) - _omega_of(1.0)) < 1e-15   # знак не важен
    #    Курамото на синтетике: диапазон и детерминизм (ω из градиента lam)
    r_syn = kuramoto_r(tr_syn, 0)
    assert 0.0 <= r_syn <= 1.0 and r_syn == kuramoto_r(tr_syn, 0)
    #    серия из одной точки без vel → честный отказ, не выдуманные скорости
    try:
        kuramoto_r({"ts": np.array([0.0]),
                    "lam": {nm: np.array([0.0]) for nm in _BODY_ORDER}}, 0)
        raise AssertionError("одна точка без vel обязана падать честно")
    except ValueError:
        pass
    #    захват Адлера: структура, детерминизм, хватка соединения > квадрата
    nat_syn = [{"name": nm, "lon": 10.0 + 33.0 * i}
               for i, nm in enumerate(_BODY_ORDER)]
    cap = adler_capture(tr_syn, nat_syn, 0)
    assert cap["rows"] and all("grip" in r for r in cap["rows"])
    assert cap == adler_capture(tr_syn, nat_syn, 0)         # детерминизм
    #    пульс цены: чистый тон — огибающая константа, пружины нет
    tt = np.arange(96) * 300.0
    tone = 100.0 + 3.0 * np.cos(2 * np.pi * 8 * np.arange(96) / 96)
    pu = price_pulse(tt, tone)
    assert pu is not None and pu["spring"] is False
    assert price_pulse(tt[:10], tone[:10]) is None          # мало точек → None
    #    скользящая сцепка: волна с самой собой → PLV≈1, «ровная»
    wave_s = 0.5 + 0.4 * np.sin(2 * np.pi * np.arange(96) / 48.0)
    rp = rolling_plv(tt, wave_s, tt, 100 + 10 * wave_s)
    assert rp and rp["now"] > 0.98 and rp["trend"] == "сцепка ровная", rp

    # 5г) v2.7: энергия/профиль/плиты
    #    энергия: чистый тон — доминантный период равен периоду тона (±30%)
    n7 = 288                                       # сутки 5-минуток
    t7 = np.arange(n7) * 300.0
    tone7 = 100 + 2 * np.cos(2 * np.pi * t7 / (3600.0 * 4))   # период 4 ч
    en = energy_pulse(t7, tone7, 5.0)
    assert en is not None and 0.0 <= en["now"] <= 1.0
    assert en["dominant_min"] and abs(en["dominant_min"] - 240) < 80, en["dominant_min"]
    assert len(en["series"]) == n7 and all(len(q) == 3 for q in en["series"])
    assert energy_pulse(t7[:20], tone7[:20], 5.0) is None     # мало → None
    #    v2.7.2: мёртвое окно — честный None (раньше плоская цена «била ключом»)
    assert energy_pulse(t7, np.full(n7, 100.0), 5.0) is None
    #    полу-мёртвая сессия: в плоской половине ранги на полу (пол шума 2%)
    half7 = np.concatenate([np.full(144, 100.0),
                            100.0 + np.sin(2 * np.pi * np.arange(144) / 12.0)])
    enh = energy_pulse(t7, half7, 5.0)
    assert enh and max(v for _, v, _ in enh["series"][:100]) <= 0.3, \
        "мёртвая половина сессии бьёт ключом"
    #    профиль объёма: объём сосредоточен у 100 → POC рядом, VAL≤POC≤VAH
    rows7 = [{"h": 100.5 + 0.1 * (i % 3), "l": 99.5 - 0.1 * (i % 3),
              "c": 100.0, "v": 1000} for i in range(30)]
    rows7 += [{"h": 110.0, "l": 109.0, "c": 109.5, "v": 10}]
    vp = volume_profile(rows7)
    assert vp and abs(vp["poc"] - 100.0) < 1.0 and vp["val"] <= vp["poc"] <= vp["vah"]
    assert volume_profile(rows7[:5]) is None
    #    эфирные плиты: волна с экстремумами на известных ценах
    wts = np.arange(0, 97) * 900.0
    wpsi = 0.5 + 0.45 * np.sin(2 * np.pi * wts / (3600.0 * 8))
    price7 = 100 + 5 * np.cos(2 * np.pi * wts / (3600.0 * 8))
    pl7 = aether_plates(wts, wpsi, wts, price7)
    assert pl7 and all(p["weight"] > 0 for p in pl7)
    #    экстремумы Ψ (±45°-фаза синуса) → цена ≈ 100 (cos в нуле): плита у 100
    assert any(abs(p["price"] - 100.0) < 1.5 for p in pl7), pl7
    #    таймфреймы: карта параметров полна и детерминирована
    assert tf_params("5m") == (12.0, 10.0, 2) and tf_params("нет")[0] == 24.0
    #    ИНВАРИАНТ ОКНА СЦЕПКИ (см. комментарий у _TF_WAVE): окно сцепки —
    #    половина окна волны, поэтому в ГОРИЗОНТ обязаны влезать >=16 баров
    #    своего ТФ, иначе couple_wave_price вернёт None и лента исчезнет.
    #    Раньше «1w» давал 12.8 бара, а «1M» вообще уходил в профиль «1h».
    for _iv, _sec in _IV_SEC.items():
        _h, _s, _dd = tf_params(_iv)
        assert _iv in _TF_WAVE, _iv                     # ни одной тихой подмены
        assert _h * 3600.0 / _sec >= 16.0, (_iv, _h * 3600.0 / _sec)
        assert _dd * 24.0 >= _h, (_iv, _dd, _h)         # свечей хватает на окно
        assert _s * 60.0 <= _sec * 8.0, (_iv, _s, _sec)  # шаг волны не грубее ТФ

    # 5д) v2.7.1: микро-плиты, пучности волны и энергии
    #    рампа цены: каждый экстремум волны на своей цене; свежий тяжелее
    wts2 = np.arange(0, 97) * 900.0                 # 24 ч, шаг 15 мин
    wpsi2 = 0.5 + 0.45 * np.sin(2 * np.pi * wts2 / (3600.0 * 8))
    ramp = np.linspace(90.0, 110.0, 97)
    mp7 = micro_plates(wts2, wpsi2, wts2, ramp)
    assert mp7 and all(p["weight"] > 0 and p["age_min"] >= 0 for p in mp7)
    #    свежесть: волна над серединой (без узлов) — одинаковые гребни, но
    #    поздний тяжелее раннего (полураспад 6 ч)
    hi_w = 0.75 + 0.2 * np.sin(2 * np.pi * wts2 / (3600.0 * 8))
    mp_h = micro_plates(wts2, hi_w, wts2, ramp, top=6)
    def _w_at(pp):
        return next(x["weight"] for x in mp_h if abs(x["price"] - pp) < 0.5)
    assert _w_at(105.0) > _w_at(98.33) > _w_at(91.67), mp_h   # гребни 18/10/2 ч
    #    узлы: монотонная волна без экстремумов, один проход через 0.5 → 1 плита;
    #    точное попадание 0.5 в сетку не считается дважды (v2.7.2)
    mono = np.linspace(0.2, 0.8, 97)
    mp_n = micro_plates(wts2, mono, wts2, ramp)
    assert len(mp_n) == 1 and abs(mp_n[0]["price"] - 100.0) < 0.5, mp_n
    assert mp_n[0]["hits"] == 1, mp_n
    #    extra_marks (пучности энергии) вливаются и взвешиваются свежестью
    mp_e = micro_plates(wts2, mono, wts2, ramp,
                        extra_marks=[(93.0, 0.9, float(wts2[-1]))])
    assert any(abs(x["price"] - 93.0) < 0.3 for x in mp_e), mp_e
    #    при now_price ближние к цене — первыми (дистанция важнее веса)
    mp8 = micro_plates(wts2, wpsi2, wts2, ramp, now_price=110.0)
    d8 = [abs(p["price"] - 110.0) for p in mp8]
    assert d8 == sorted(d8), d8
    assert micro_plates(wts2, wpsi2, wts2[:5], ramp[:5]) == []   # мало → []
    #    пучности волны: громкая волна даёт пики и границы зоны; тихая — ничего
    an7 = wave_antinodes(wts2, wpsi2, 48)
    assert an7["past"] and an7["forward"]
    assert any(x["event"] == "пик" for x in an7["forward"])
    assert any("пучность" in x["event"] for x in an7["forward"])   # вход/выход
    assert all(x["amp"] >= LOUD_THRESHOLD for x in an7["past"] + an7["forward"])
    assert {x["kind"] for x in an7["past"] + an7["forward"]} <= {"гребень", "впадина"}
    assert an7["now"].startswith("в ")
    an8 = wave_antinodes(wts2, 0.5 + 0.1 * np.sin(
        2 * np.pi * wts2 / (3600.0 * 8)), 48)
    assert not an8["past"] and not an8["forward"], "тихая волна дала пучности"
    assert an8["now"] == "в тишине"
    #    пучности энергии: список максимумов ранга, все ≥0.7
    assert en.get("antinodes") and all(v >= 0.7 for _, v in en["antinodes"])

    # 5д2) v2.7.2: зеркальный флип — вокруг середины ОКНА калибровки, не 0.5
    #    полной шкалы: асимметричное окно [0..0.5] с зеркальной ценой не должно
    #    выводить проекцию за пределы виденных цен
    ts_m2 = np.arange(0, 200) * 600.0
    w_as = 0.25 + 0.25 * np.sin(2 * np.pi * ts_m2 / (ts_m2[-1] / 5.0))
    price_as = 200.0 - 40.0 * w_as
    cp_as = couple_wave_price(ts_m2, w_as, ts_m2, price_as)
    assert cp_as and cp_as["mirror"] and "w_lo" in cp_as and "w_hi" in cp_as
    pot_as = potential_of(w_as[-40:], cp_as, plates_of(price_as.tolist()),
                          float(price_as[-1]))
    assert pot_as is not None
    #    допуск 5% размаха: взвешенные квантили v2.9 дают дискретную асимметрию
    #    интерполяции; ловим СИСТЕМАТИЧЕСКИЙ сдвиг (был 100% размаха), не шум
    rng_as = float(price_as.max() - price_as.min())
    assert (float(np.min(pot_as["center"])) >= float(price_as.min()) - 0.05 * rng_as
            and float(np.max(pot_as["center"])) <= float(price_as.max()) + 0.05 * rng_as), \
        "зеркальная проекция ушла далеко за окно виденных цен"

    #    гистерезис зеркала (v2.8): внутри полосы −0.35..−0.25 состояние
    #    держится — проекция не прыгает между обновлениями
    _MIRROR_STATE.clear()
    assert _mirror_hyst("t|i|s", -0.40, True) is True     # старт — сырой флаг
    assert _mirror_hyst("t|i|s", -0.28, False) is True    # полоса — держим зеркало
    assert _mirror_hyst("t|i|s", -0.20, False) is False   # выше −0.25 — выкл
    assert _mirror_hyst("t|i|s", -0.32, True) is False    # полоса — держим не-зеркало
    assert _mirror_hyst("t|i|s", -0.40, True) is True     # ниже −0.35 — вкл
    _MIRROR_STATE.clear()

    #    усилие/результат (VSA): всплеск объёма без хода → D ≥ порога
    rows_e = [{"c": 100.0 + 0.1 * math.sin(i / 3.0), "v": 1000.0}
              for i in range(40)]
    rows_e[-2] = {"c": rows_e[-3]["c"], "v": 8000.0}
    eff7 = effort_result(rows_e)
    assert eff7 and eff7["d"] >= EFFORT_THR, eff7
    assert effort_result(rows_e[:10]) is None

    # 5ж) v2.8: EBS Элерса и заряд толпы (формулы веера, прогнаны им же)
    ramp_e = np.linspace(0.0, 60.0, 600)
    eb_r = ebs_series(ramp_e)
    assert float(np.nanmin(eb_r[EBS_WARMUP:])) > 0.999, "рампа не рельс"
    sin20 = np.sin(2 * np.pi * np.arange(600) / 20.0)
    v_s = ebs_series(sin20)[EBS_WARMUP:]
    vv_s = v_s[~np.isnan(v_s)]
    crosses = int(np.sum(np.diff(np.sign(vv_s)) != 0))
    assert crosses >= 1.5 * len(vv_s) / 20.0, crosses
    assert all(float(np.min(np.abs(vv_s[i:i + 20]))) < 0.6
               for i in range(0, len(vv_s) - 20, 20)), "цикл без провалов |EBS|"
    rows_re = [[i * 60.0, 0, 0, 0, 100.0 + 0.1 * i, 10.0] for i in range(600)]
    ebo = ebs_of(rows_re)
    assert ebo and ebo["mode"] == "рельс вверх" and ebo["rail_run"] >= EBS_DWELL
    assert ebs_of(rows_re[:50]) is None                    # прогрев → None
    #    заряд: рампа+объём → >+0.5 (зеркально <−0.5); пила → ~0; слабая
    #    энергия глушит; мёртвое окно → None
    cl_up = list(100 + 0.05 * np.arange(60))
    vv_up = list(100 + 5.0 * np.arange(60))
    chu = charge_of(cl_up, vv_up, 0.9)
    assert chu and chu["charge"] > 0.5, chu
    chd = charge_of(cl_up[::-1], vv_up, 0.9)
    assert chd and chd["charge"] < -0.5, chd
    chs = charge_of(list(100 + 0.5 * np.array([1.0, -1.0] * 30)),
                    [100.0] * 60, 0.8)
    assert chs is not None and abs(chs["charge"]) < 0.3, chs
    assert charge_of([100.0] * 60, [100.0] * 60, 0.9) is None
    chw = charge_of(cl_up, vv_up, 0.05)
    assert chw and abs(chw["charge"]) < 0.2, chw
    for c_t in (chu, chd, chs, chw):
        for bad in ("покупай", "продавай", "лонг", "шорт"):
            assert bad not in c_t["word"], bad

    # 5з) v2.9: калибровка-призрак, имплозия, полости, ρ рынка
    #    призрак старого шпиля не держит масштаб (вес затухает в прошлое)
    price_sp = (100 + 10 * wave).copy()
    price_sp[:12] = 300.0                      # древний шпиль в начале окна
    cp_sp = couple_wave_price(ts, wave, ts, price_sp)
    top_sp = cp_sp["affine_a"] * cp_sp["w_hi"] + cp_sp["affine_b"]
    assert top_sp < 150.0, top_sp
    #    имплозия: объём встал в цену → стена на цене бара; ровный ход — нет
    rows_im = [{"c": 100.0 + 0.01 * math.sin(i / 5.0), "v": 100.0}
               for i in range(120)]
    rows_im[-2] = {"c": rows_im[-3]["c"], "v": 50000.0}
    im = implosion_of(rows_im)
    assert im and im["active"] and abs(im["price"] - rows_im[-2]["c"]) < 1e-3
    rows_im2 = [{"c": 100 + 0.1 * i, "v": 100.0 + 10 * math.sin(i / 3.0)}
                for i in range(120)]
    im2 = implosion_of(rows_im2)
    assert im2 is not None and not im2["active"]
    #    полости: незаполненный разрыв найден с точными краями; заполненный — нет
    base_v = [{"h": 100.0, "l": 99.0} for _ in range(9)]
    rows_v = base_v + [{"h": 104.0, "l": 100.0}, {"h": 106.0, "l": 104.5}] + \
        [{"h": 106.5, "l": 105.0} for _ in range(6)]
    vz = voids_of(rows_v)
    assert vz and any(z["dir"] == "вверх" and abs(z["lo"] - 100.0) < 1e-9
                      and abs(z["hi"] - 104.5) < 1e-9 for z in vz), vz
    rows_vf = base_v + [{"h": 104.0, "l": 100.0}, {"h": 106.0, "l": 104.5}] + \
        [{"h": 106.5, "l": 99.0}]
    assert voids_of(rows_vf) == []
    #    ρ рынка: один ритм → струна; разные ритмы → ниже; <3 активов → None
    t_mc = np.arange(200)
    base_c = 100 + np.cumsum(0.1 * np.sin(t_mc / 7.0))
    mc = market_coherence({"A": list(base_c), "B": list(base_c * 1.5 + 3),
                           "C": list(base_c * 0.5)})
    assert mc and mc["r"] > 0.8, mc
    mc2 = market_coherence({
        "A": list(100 + np.cumsum(0.1 * np.sin(t_mc / 5.0))),
        "B": list(100 + np.cumsum(0.1 * np.sin(t_mc / 11.0 + 1.0))),
        "C": list(100 + np.cumsum(0.1 * np.sin(t_mc / 23.0 + 2.0)))})
    assert mc2 and mc2["r"] < mc["r"], (mc2["r"], mc["r"])
    assert market_coherence({"A": list(base_c), "B": list(base_c)}) is None

    # 5е) старшие ключи: Гребенников / Кили / Штейнмец-Доллард
    #    полость: точный Большой трин Q=√(3/4); сдвиг вершины — падение
    #    строго монотонно; за орбисом — 0; стеллиум — не полость
    ideals3 = [120.0, 120.0, 120.0]
    q0 = q_cavity([0.0, 120.0, 240.0], ideals3)
    assert abs(q0 - math.sqrt(3.0 / 4.0)) < 1e-9
    qs = [q_cavity([0.0, 120.0 + s, 240.0], ideals3) for s in (0.0, 1.0, 3.0, 5.0)]
    assert all(a > b for a, b in zip(qs, qs[1:])), qs
    assert q_cavity([0.0, 127.0, 240.0], ideals3) == 0.0
    q_cross = q_cavity([0.0, 90.0, 180.0, 270.0], [90.0] * 4)
    q_rect = q_cavity([0.0, 60.0, 180.0, 240.0], [60.0, 120.0, 60.0, 120.0])
    q_tau = q_cavity([0.0, 90.0, 180.0], [90.0, 90.0, 180.0])
    assert q_cross > q_rect > q0 > q_tau        # симметрия соты правит
    assert q_cavity([10.0, 10.0, 10.0], ideals3) == 0.0
    #    Кили: 10:15:20 — один аккорд из трёх тел; несоизмеримые — R=0 ровно;
    #    противоход рвёт аккорд; оба ретро — сонаправлены
    r1 = keely_chords([10.0, 15.0, 20.0], [1, 1, 1], ["A", "B", "C"])
    assert len(r1["chords"]) == 1 and len(r1["chords"][0]) == 3 and r1["r"] > 1.0
    r2 = keely_chords([10.0, 13.7, 21.3], [1, 1, 1], ["A", "B", "C"])
    assert not r2["chords"] and r2["r"] == 0.0
    assert not keely_chords([10.0, -15.0, 7.7], [1, 1, 1], ["A", "B", "C"])["chords"]
    r4 = keely_chords([-10.0, -15.0, -20.0], [1, 1, 1], ["A", "B", "C"])
    assert len(r4["chords"]) == 1
    #    версор: синус — противофаза E_d/M_k, сумма ≈ константа, натяжения нет
    dtv = 0.01
    tv = np.arange(2048) * dtv
    E_v, M_v, tn_v, om_v = versor_split(np.sin(2 * np.pi * 2.0 * tv), dtv)
    assert float(np.corrcoef(E_v[50:-50], M_v[50:-50])[0, 1]) < -0.95
    s_v = E_v[50:-50] + M_v[50:-50]
    assert float(np.std(s_v) / np.mean(s_v)) < 0.02
    assert not tn_v.any(), "синус дал ложное натяжение"
    assert abs(om_v - 2 * np.pi * 2.0) / (2 * np.pi * 2.0) < 0.05
    #    пила с плато (подъём 0.4T, плато 0.4T, сброс 0.2T): натяжение на
    #    плато ловится, на равномерном подъёме — нет
    tp = np.arange(4096) * dtv
    ph_p = (tp % 1.0) / 1.0
    saw = np.where(ph_p < 0.4, ph_p / 0.4,
                   np.where(ph_p < 0.8, 1.0, 1.0 - (ph_p - 0.8) / 0.2))
    E_2, M_2, tn_2, _ = versor_split(saw, dtv)
    pl_m = (ph_p >= 0.45) & (ph_p < 0.8)
    assert float(tn_2[pl_m].mean()) > 0.5, "плато не поймано"
    ri_m = (ph_p >= 0.05) & (ph_p < 0.35)
    assert float(tn_2[ri_m].mean()) < 0.05, "ложное натяжение на подъёме"

    #    VWAP: постоянная цена → vwap = цена, σ = 0
    rows_vw = [{"t": "2026-07-30T%02d:00:00Z" % h, "h": 101.0, "l": 99.0,
                "c": 100.0, "v": 100} for h in range(12)]
    vwp = session_vwap(rows_vw)
    assert vwp and abs(vwp["vwap"] - 100.0) < 1e-9 and vwp["sd"] < 1e-9
    assert session_vwap(rows_vw[:3]) is None

    # 6) генезисы: корень тикера, фолбэк честный
    assert genesis_for("BRU6")["key"] == "BR" and not genesis_for("BRU6")["fallback"]
    assert genesis_for("BR-9.26")["key"] == "BR"
    assert genesis_for("XYZQ")["fallback"] and "не в реестре" in genesis_for("XYZQ")["note"]
    #    v2.8.1: дата выпуска бумаги — свой якорь (у каждой бумаги своя волна)
    gi = genesis_for("QWRT", "2007-07-20")
    assert gi.get("issue") and not gi["fallback"] and gi["genesis"][:3] == (2007, 7, 20)
    assert genesis_for("BRU6", "2020-01-01")["key"] == "BR"    # реестр сильнее даты
    assert genesis_for("XYZQ", "мусор")["fallback"]            # кривая дата → фолбэк

    # 7) запрет торговых слов в честной рамке и вердиктах
    blob = (FRAME + " " + str(GENESIS)).lower()
    for bad in ("покупай", "продавай", "лонг", "шорт", "buy", "sell"):
        assert bad not in blob, bad

    # 8) эфемеридная часть — только если рядом есть .bsp (честный пропуск иначе)
    if astro._load_sf():
        nat = natal_lons(GENESIS["BR"]["genesis"])
        assert nat and len(nat) == 10
        t0 = datetime(2026, 7, 30, tzinfo=timezone.utc)
        tr = transit_series(t0, 24.0, 1.0)
        assert tr and len(tr["ts"]) == 25
        #    v3.9: серия несёт реальные скорости; Луна ~13°/сут, станций нет
        assert "vel" in tr and all(nm in tr["vel"] for nm in _BODY_ORDER)
        assert 10.0 < float(np.abs(tr["vel"]["Луна"]).mean()) < 16.0
        #    вес — фотометрия: Луна ≈ 1 (E/K_chrono = (alb/alb)·d_ap³ у Луны),
        #    и диск Луны на порядки громче диска Юпитера
        _chr_moon = np.minimum(CHRONO_CAP,
                               1.0 + 1.0 / (np.abs(tr["vel"]["Луна"]) + EPS_CHRONO))
        _dap3 = float((tr["E"]["Луна"] / _chr_moon).mean())
        assert 0.5 < _dap3 < 2.0, _dap3                    # «Луна ≈ 1»
        assert float(tr["E"]["Луна"].mean()) > 50.0 * float(tr["E"]["Юпитер"].mean())
        psi = psi_of(tr, nat, 108)
        assert psi.shape == (25,) and 0.0 <= psi.min() and psi.max() <= 1.0
        assert np.array_equal(psi, psi_of(tr, nat, 108))       # детерминизм
        ctx = compute_context("BRU6", None, now=datetime(2026, 7, 30, 12,
                              tzinfo=timezone.utc))
        assert ctx["mode"] == "precise" and ctx["genesis"]["key"] == "BR"
        txt = render_for_ai(ctx)
        assert "ЭФИРНЫЙ СЛОЙ" in txt and "18+" in txt
        for bad in ("покупай", "продавай", "лонг", "шорт", "buy", "sell"):
            assert bad not in txt.lower(), bad
        #    v2.7.1: payload графика несёт пучности; forward-узлы со временем
        pay = chart_payload(ctx)
        assert pay["available"] and isinstance(pay["antinodes"], dict)
        assert all("when" in x for x in pay["antinodes"].get("forward") or [])
        #    v2.7.2: payload несёт интервал (фронт сверяет со своим) и светофор
        assert pay.get("interval") == "1h" and pay.get("computed_at")
        #    v2.8.1: окно едет с шагом волны (не замирает на час)
        assert int(ctx["wave"]["ts"][0]) % 1800 == 0
        assert pay.get("genesis", {}).get("key") == "BR"
        fldt = ctx.get("field")
        if fldt and not fldt.get("error"):
            assert 0.0 <= fldt["score"] <= 1.0 and fldt["parts"]
            for bad in ("покупай", "продавай", "лонг", "шорт"):
                assert bad not in (fldt["word"] + fldt["note"]).lower(), bad
        print("aether: эфемеридные тесты пройдены ✓ (Ψ детерминирован, контекст жив)")
    else:
        print("aether: .bsp не найден — эфемеридные тесты честно пропущены")
    # ── v3.1 (аудит №3): новые self-тесты ──
    #  Т-а: AR-хвост — фаза правого края растущей синусоиды растёт (зеркало
    #  ломало наклон о складку); частота на краю конечна и положительна
    tt = np.arange(64, dtype=float)
    xs = np.sin(2 * np.pi * tt / 16.0 + 0.3)
    ph_e = np.unwrap(np.angle(_analytic(xs)))
    dph_edge = ph_e[-1] - ph_e[-2]
    assert 0.0 < dph_edge < 1.2, dph_edge          # ~2π/16≈0.39, знак верный
    #  Т-б: Бедросян — фаза цены в полосе волны; PLV зашумлённой цены
    #  с фильтром заметно выше, чем без него
    wv = 0.5 + 0.45 * np.sin(2 * np.pi * tt / 16.0)
    noise_hf = (2.5 * np.sin(2 * np.pi * tt / 2.3 + 1.0)
                + 1.7 * np.sin(2 * np.pi * tt / 3.7 + 2.0)
                + 1.3 * np.sin(2 * np.pi * tt / 5.1)
                + 0.9 * np.sin(2 * np.pi * tt / 50.0))
    pc_noisy = 100 + 10 * (wv - 0.5) + noise_hf
    e4 = max(3, len(tt) // EDGE_FRAC)
    ph_w4 = np.unwrap(np.angle(_analytic(wv)))
    plv_raw, _ = _plv(ph_w4, np.unwrap(np.angle(_analytic(pc_noisy))), e4)
    plv_bp, _ = _plv(ph_w4, np.unwrap(np.angle(
        _analytic(_bandpass_like(pc_noisy, wv)))), e4)
    assert plv_bp > plv_raw + 0.1, (plv_raw, plv_bp)
    #  Т-в: couple несёт m_inst и winding; синфазная цена — m_inst≈1, W≈0
    ts9 = 1_800_000_000.0 + tt * 3600.0
    cp9 = couple_wave_price(ts9, wv, ts9, 100 + 10 * (wv - 0.5))
    assert cp9 and cp9["m_inst"] > 0.8 and abs(cp9["winding"]) < 0.5, cp9
    #  Т-г: мгновенное зеркало — гистерезис на m_inst: −0.6 включает,
    #  −0.4 держит, −0.1 выключает
    _MIRROR_STATE.clear()
    assert _mirror_hyst("t9", None, False, m_inst=-0.6) is True
    assert _mirror_hyst("t9", None, False, m_inst=-0.4) is True
    assert _mirror_hyst("t9", None, False, m_inst=-0.1) is False
    #  Т-г2: два зеркала независимы — corr-путь и inst-путь под своими ключами
    _MIRROR_STATE.clear()
    assert _mirror_hyst("tw|fwd", -0.6, True, m_inst=0.9) is False
    assert _mirror_hyst("tw|past", -0.6, True) is True    # raw=corr<−0.3 (прод)
    assert _MIRROR_STATE["tw|fwd"] is False and _MIRROR_STATE["tw|past"] is True
    assert _mirror_hyst("tw|past", -0.28, True) is True   # гистерезис держит
    assert _mirror_hyst("tw|past", -0.10, True) is False  # и честно отпускает
    #  Т-д: обмотка — цена с лишним полным оборотом фазы против волны
    ph_extra = 2 * np.pi * tt / 16.0 + 2 * np.pi * (tt / tt[-1])
    cp10 = couple_wave_price(ts9, wv, ts9, 100 + 10 * np.sin(ph_extra))
    assert cp10 and cp10["winding"] > 0.5, cp10 and cp10["winding"]
    #  Т-в2 (стенд v3.1.1): зеркало слепо к полосе — m_inst считается по
    #  СЫРОЙ фазе; ломаем полосу нарочно и убеждаемся, что судья не дрогнул
    _bp_keep = _bandpass_like
    globals()["_bandpass_like"] = lambda x, ref: -np.asarray(x, float)
    cp9b = couple_wave_price(ts9, wv, ts9, 100 + 10 * (wv - 0.5))
    globals()["_bandpass_like"] = _bp_keep
    assert cp9b and abs(cp9b["m_inst"] - cp9["m_inst"]) < 1e-9,         (cp9["m_inst"], cp9b["m_inst"])
    #  Т-е: склейка масштабов — самоподобная волна складывает матрёшку
    tt2 = np.arange(240, dtype=float)
    base = np.sin(2 * np.pi * tt2 / 48.0)
    fr1 = fractal_resonance(100 + 10 * base)
    assert fr1 and fr1["r"] > 0.6, fr1
    #  Т-ж: Чириков — узлы с перекрытием зон дают окно, разнесённые — нет
    ch1 = chirikov_of([{"t_exact": 1e9, "sigma_min": 60, "pair": "a"},
                       {"t_exact": 1e9 + 3600, "sigma_min": 60, "pair": "b"}])
    assert ch1 and ch1[0]["k"] >= 1.0, ch1
    assert not chirikov_of([{"t_exact": 1e9, "sigma_min": 30, "pair": "a"},
                            {"t_exact": 1e9 + 86400, "sigma_min": 30, "pair": "b"}])
    #  Т-з: тритон и Φ в аккордах Пифии — идентификация, не консонанс
    kk9 = keely_chords([10.0, 14.4, 16.18, 3.0], [5.0, 5.0, 5.0, 5.0],
                       ["А", "Б", "В", "Г"])
    assert any(t9["pair"] == "А·Б" for t9 in kk9["tritones"]), kk9["tritones"]
    assert any(p9["pair"] == "А·В" and p9["k"] == 1 for p9 in kk9["phi"]), kk9["phi"]
    print("aether v3.1: тесты аудита №3 пройдены ✓ (AR-край, Бедросян, "
          "зеркало-инстант, обмотка, матрёшка, Чириков, тритон/Φ)")
    print("aether: все self-тесты пройдены ✓")
