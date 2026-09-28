"""ОРАКУЛ // ПИФИЯ — ГЛУБИННЫЙ ЭФИР (документ владельца, полное принятие).

Аддитивный модуль РЯДОМ с aether.py (канон школы: ядро не трогаем, новый
слой — новым модулем). Пять линий документа, не вошедших в v2.9:

1. 3D-СИМПЛЕКС ГРЕБЕННИКОВА: планеты образуют ОБЪЁМНЫЕ фигуры — объём
   тетраэдра единичных направлений 4 тяжёлых тел (настоящие XYZ DE440,
   не только долгота). Замирание объёма при высокой симметрии = «резонанс
   полости» (ЭПС — язык 🟡, геометрия — факт 🔵).
2. СКАЛЯРНОЕ НАПРЯЖЕНИЕ (Тесла/Штейнмец/Мейл по документу): противофаза
   180° — не тишина, а сжатие. S(t)=Σ√(EᵢEⱼ)·exp(−(Δλ−180)²/2σ²)·
   (1−|sin(βᵢ−βⱼ)|) — с ШИРОТАМИ β (лоб в лоб жмёт сильнее).
3. ТЕНЕВОЙ ДИСБАЛАНС (Vshadow документа, честная версия): накопленное
   расхождение между градиентом проекции эфира и фактическим ходом цены.
   Поле давит — цена стоит → невидимое поглощение (аналогия тёмной
   материи: видим не её, а искривление орбит) 🟡; само расхождение —
   вычислимый факт 🔵.
4. ПАМЯТЬ ХЁРСТА (дробное исчисление документа, честный вход): показатель
   H агрегированной дисперсией — длинная память среды («шрамы») против
   короткой. Индекс памяти α = 2−2H для дробного веса прошлого.
5. ПЛОТНОСТЬ ВРЕМЕНИ КОЗЫРЕВА (эквивольюмный взгляд): биржевое время
   течёт объёмом, не минутами. Плотность = объём бара к медиане окна:
   сжатое время (объём хлещет) / растянутое (флэт).

Рамка везде: 🔵 механика · 🟡 язык школы (прокси назван) · ⚫ не сигнал,
не приказ, 18+. Никакого random, всё детерминировано, офлайн-расчёт.
"""
from __future__ import annotations

import math

import numpy as np

from . import astro

# тяжёлые тела симплекса (документ: «4 самые тяжёлые»)
SIMPLEX_BODIES = ("Юпитер", "Сатурн", "Уран", "Нептун")
SIGMA_SCALAR = 6.0          # орбис противофазы, °
SHADOW_WIN = 96             # окно теневого интеграла, баров
HURST_MIN = 64              # минимум баров для H
KOZYREV_WIN = 240           # окно медианы объёма


# ── 1. 3D-симплекс Гребенникова ──────────────────────────────────────

def _xyz_series(t0, hours: float, step_h: float):
    """Единичные направления тел из Земли (эклиптика J2000) на сетке 🔵."""
    sf = astro._load_sf()
    if not sf:
        return None
    n = int(round(hours / step_h)) + 1
    hh = t0.hour + t0.minute / 60.0 + np.arange(n, dtype=float) * step_h
    t = sf["ts"].utc(t0.year, t0.month, t0.day, hh)
    ts_unix = np.array([t0.timestamp() + k * step_h * 3600.0 for k in range(n)])
    out = {}
    for nm in SIMPLEX_BODIES:
        bkey = sf["bodies"].get(nm)
        if not bkey:
            return None
        q = sf["earth"].at(t).observe(sf["eph"][bkey]).apparent()
        v = q.position.au                       # (3, n)
        norm = np.linalg.norm(v, axis=0)
        out[nm] = (v / np.maximum(norm, 1e-12)).T   # (n, 3) единичные
    return {"ts": ts_unix, "u": out}


def simplex_volume(units: dict, k: int) -> float:
    """Объём тетраэдра 4 единичных направлений (×6 — чистый детерминант) 🔵."""
    a, b, c, d = (units[nm][k] for nm in SIMPLEX_BODIES)
    return abs(float(np.linalg.det(np.stack([b - a, c - a, d - a]))))


def simplex_of(t0, hours: float, step_h: float, k_now: int) -> dict | None:
    xyz = _xyz_series(t0, hours, step_h)
    if xyz is None:
        return None
    n = len(xyz["ts"])
    vol = np.array([simplex_volume(xyz["u"], k) for k in range(n)])
    # |det| = 6·объём; максимум правильного тетраэдра на единичной сфере:
    # 6·(8/(9√3)) = 16/(3√3)
    v_max = 16.0 / (3.0 * math.sqrt(3.0))
    v01 = vol / v_max
    k = min(max(k_now, 1), n - 2)
    dv = float(v01[k + 1] - v01[k - 1]) / (2.0 * step_h)   # 1/час
    frozen = abs(dv) < 0.002 and v01[k] > 0.25
    if frozen:
        word = "симплекс замер при объёме — полость дышит (резонанс формы)"
    elif v01[k] < 0.05:
        word = "тела почти в плоскости — полость сложена"
    else:
        word = "симплекс перестраивается"
    return {"v": round(float(v01[k]), 3), "dv_h": round(dv, 4),
            "frozen": bool(frozen), "word": word,
            "note": ("объём тетраэдра единичных направлений 4 тяжёлых тел "
                     "(XYZ DE440) 🔵, нормирован на максимум правильного; "
                     "«полость/резонанс» — язык ЭПС Гребенникова 🟡; "
                     "не событие ⚫")}


# ── 2. Скалярное напряжение (противофазы с широтами) ─────────────────

def scalar_stress(lam: dict, beta: dict, E: dict, k: int) -> dict | None:
    """S(t): противофаза не гасится, а сжимается в продольное напряжение 🟡.
    Гауссиана на 180° по долготам × (1−|sin Δβ|): лоб-в-лоб жмёт сильнее 🔵."""
    names = list(lam.keys())
    if len(names) < 4:
        return None
    s = 0.0
    top = None
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            # v3.1 (аудит №3): ИСТИННЫЙ 3D-угол вместо (1−|sinΔβ|) —
            # старый штраф глушил идеальную струну (+2°/−2° в оппозиции —
            # прямая через центр Земли, а формула видела «разлёт» 4°)
            la_, ba_ = math.radians(float(lam[a][k])), math.radians(float(beta[a][k]))
            lb_, bb_ = math.radians(float(lam[b][k])), math.radians(float(beta[b][k]))
            dot = (math.cos(ba_) * math.cos(la_) * math.cos(bb_) * math.cos(lb_)
                   + math.cos(ba_) * math.sin(la_) * math.cos(bb_) * math.sin(lb_)
                   + math.sin(ba_) * math.sin(bb_))
            th = math.degrees(math.acos(max(-1.0, min(1.0, dot))))
            g = math.exp(-((180.0 - th) ** 2) / (2.0 * SIGMA_SCALAR ** 2))
            if g < 1e-6:
                continue
            w = math.sqrt(max(float(E[a][k]), 0.0) * max(float(E[b][k]), 0.0))
            val = w * g
            s += val
            if top is None or val > top[0]:
                top = (val, f"{a}·{b}", round(th, 1))
    if top is None:
        return {"s": 0.0, "pair": None,
                "word": "противофаз нет — скалярное поле молчит",
                "note": "ни одна пара не в орбисе 180±6° 🔵"}
    return {"s": round(s, 2), "pair": top[1], "sep": top[2],
            "word": ("скалярное сжатие: " + top[1] +
                     " в противофазе — тишина снаружи, давление внутри"),
            "note": ("S=Σ√(EᵢEⱼ)·гаусс(180°−Θ,σ=6°), Θ=acos(uᵢ·uⱼ) — истинный "
                     "3D-угол (аудит №3: ложный штраф широты убит) 🔵; "
                     "«продольное сжатие» — язык Теслы 🟡; не приказ ⚫")}


# ── 3. Теневой дисбаланс (Vshadow честной сборки) ────────────────────

def shadow_imbalance(center_grad, price_ret, win: int = SHADOW_WIN) -> dict | None:
    """Накопленное расхождение давления поля и хода цены 🔵-расчёт/🟡-чтение.
    z-нормировка обеих серий на окне, интеграл разности: |D| высок и растёт —
    поле давит, цена не идёт (невидимое поглощение — как тёмная материя:
    видим искривление, не массу)."""
    g = np.asarray(center_grad, float)
    r = np.asarray(price_ret, float)
    L = min(g.size, r.size)
    if L < 24:
        return None
    g, r = g[-L:], r[-L:]
    w = min(win, L)
    g, r = g[-w:], r[-w:]
    sg, sr = float(np.std(g)), float(np.std(r))
    if sg < 1e-12 or sr < 1e-12:
        return None
    d = np.cumsum(g / sg - r / sr)
    d_now = float(d[-1]) / w
    rising = bool(abs(d[-1]) > abs(d[max(0, len(d) - 8)]))
    if abs(d_now) >= 0.35 and rising:
        side = "вверх" if d_now > 0 else "вниз"
        word = (f"теневое поглощение: поле давит {side}, цена не идёт — "
                "невидимые руки принимают поток")
    elif abs(d_now) >= 0.2:
        word = "расхождение поля и хода копится"
    else:
        word = "поле и ход согласны — теней не видно"
    return {"d": round(d_now, 3), "rising": rising, "word": word,
            "note": ("∫(z(∇center)−z(ΔC)) на окне " + str(w) + " баров — "
                     "расхождение вычислимо 🔵; «теневой пул/поглощение» — "
                     "прочтение школы 🟡 (прямых данных тёмных пулов нет); "
                     "не событие ⚫")}


# ── 4. Память Хёрста (шрамы среды) ───────────────────────────────────

def hurst_memory(closes) -> dict | None:
    """H агрегированной дисперсией (детерминированный, без R/S-шума):
    var(агрегата m) ~ m^(2H−2) для приростов. H>0.5 — длинная память
    (тренды-шрамы), H<0.5 — антиперсистентность (среда огрызается) 🔵."""
    c = np.asarray([x for x in (closes or []) if x], float)
    if c.size < HURST_MIN or np.any(c <= 0):
        return None
    r = np.diff(np.log(c))
    if float(np.std(r)) < 1e-12:
        return None
    ms, vs = [], []
    m = 1
    while m <= len(r) // 8:
        k = len(r) // m
        agg = r[:k * m].reshape(k, m).sum(axis=1)
        if agg.size >= 8 and float(np.var(agg)) > 0:
            ms.append(m)
            vs.append(float(np.var(agg)))
        m *= 2
    if len(ms) < 3:
        return None
    slope = float(np.polyfit(np.log(ms), np.log(vs), 1)[0])
    H = max(0.05, min(0.95, slope / 2.0))
    alpha = 2.0 - 2.0 * H            # индекс дробной памяти документа
    if H >= 0.6:
        word = "память среды длинная — шрамы держат колею"
    elif H <= 0.4:
        word = "среда огрызается — ход встречает откат"
    else:
        word = "память обычная"
    return {"h": round(H, 3), "alpha": round(alpha, 3), "word": word,
            "note": ("H агрегированной дисперсией по лог-приростам 🔵; "
                     "α=2−2H — индекс памяти дробного веса прошлого "
                     "(Риман–Лиувилль — линия документа) 🟡-чтение; "
                     "не прогноз ⚫")}


# ── 5. Плотность времени Козырева (эквивольюмный взгляд) ─────────────

def kozyrev_density(vols, win: int = KOZYREV_WIN) -> dict | None:
    """Биржевое время течёт объёмом: плотность = объём закрытого бара к
    медиане окна 🔵. Сжатое время (льёт) / растянутое (флэт) — язык
    хроно-плотности Козырева 🟡."""
    v = np.asarray([x for x in (vols or []) if x is not None], float)
    if v.size < 24:
        return None
    w = v[-min(win, v.size):]
    med = float(np.median(w[w > 0])) if np.any(w > 0) else 0.0
    if med <= 0:
        return None
    k = len(v) - 2                    # последний закрытый
    rho = float(v[k]) / med
    if rho >= 3.0:
        word = "время сжато — сквозь бар льёт тройная река"
    elif rho <= 0.33:
        word = "время растянуто — минуты пустые, эфир копится"
    else:
        word = "время течёт ровно"
    return {"rho": round(rho, 2), "word": word,
            "note": ("ρ_τ = объём закрытого бара / медиана окна 🔵; "
                     "«плотность времени» — язык Козырева 🟡; ось графика "
                     "остаётся часовой, ρ_τ — линза, не пересборка ⚫")}


# ── self-тесты ───────────────────────────────────────────────────────

if __name__ == "__main__":
    # симплекс: правильный тетраэдр → v01=1; компланарные → 0
    tet = {nm: np.array([p]) for nm, p in zip(SIMPLEX_BODIES, [
        [1, 1, 1], [1, -1, -1], [-1, 1, -1], [-1, -1, 1]])}
    for nm in tet:
        tet[nm] = tet[nm] / np.linalg.norm(tet[nm], axis=1, keepdims=True)
    v = simplex_volume(tet, 0) / (16.0 / (3.0 * math.sqrt(3.0)))
    assert abs(v - 1.0) < 1e-9, v
    flat = {nm: np.array([[math.cos(a), math.sin(a), 0.0]])
            for nm, a in zip(SIMPLEX_BODIES, [0.0, 1.0, 2.0, 3.0])}
    assert simplex_volume(flat, 0) < 1e-12

    # скалярное напряжение: точная противофаза в плоскости — пик; та же
    # противофаза с разбегом широт — слабее; без противофаз — ноль
    lam1 = {"A": [0.0], "B": [180.0], "C": [90.0], "D": [40.0]}
    beta0 = {k: [0.0] for k in lam1}
    E1 = {k: [10.0] for k in lam1}
    s1 = scalar_stress(lam1, beta0, E1, 0)
    assert s1["s"] > 9.0 and s1["pair"] == "A·B", s1
    beta5 = dict(beta0)
    beta5["B"] = [60.0]
    s2 = scalar_stress(lam1, beta5, E1, 0)
    assert s2["s"] < s1["s"], (s1["s"], s2["s"])
    lam0 = {"A": [0.0], "B": [50.0], "C": [90.0], "D": [140.0]}
    assert scalar_stress(lam0, beta0, E1, 0)["s"] == 0.0

    # теневой дисбаланс: поле давит вверх, цена стоит → d > 0.35 и слово
    g = np.ones(96) * 1.0
    g[:40] = np.sin(np.arange(40))          # разнообразие для z-нормы
    r0 = np.sin(np.arange(96)) * 0.1        # цена топчется
    sh = shadow_imbalance(g, r0)
    assert sh and sh["d"] > 0.35 and "поглощение" in sh["word"], sh
    sh0 = shadow_imbalance(g, g)            # поле и ход совпадают → согласие
    assert sh0 and abs(sh0["d"]) < 0.05, sh0
    assert shadow_imbalance(np.ones(96), r0) is None   # мёртвый градиент

    # Хёрст: персистентная кумсумма синуса низкой частоты → H высокий;
    # антиперсистентная пила → H низкий; мёртвое окно → None
    up = 100 * np.exp(np.cumsum(np.full(512, 1e-3)))
    hp = hurst_memory(list(up * (1 + 0.001 * np.sin(np.arange(512) / 50))))
    assert hp and hp["h"] > 0.6, hp
    alt = np.array([1.0, -1.0] * 256)
    saw = 100 + 0.5 * alt * (1.0 + 0.01 * np.sin(np.arange(512) / 7.0))
    hs = hurst_memory(list(saw))
    assert hs and hs["h"] < 0.4, hs
    assert hurst_memory([100.0] * 200) is None

    # Козырев: всплеск объёма в закрытом баре → сжатое время; пустой — растянутое
    vv = [100.0] * 240 + [1000.0, 50.0]
    kz = kozyrev_density(vv)
    assert kz and kz["rho"] >= 3.0 and "сжато" in kz["word"], kz
    vv2 = [100.0] * 240 + [10.0, 50.0]
    kz2 = kozyrev_density(vv2)
    assert kz2 and kz2["rho"] <= 0.33 and "растянуто" in kz2["word"], kz2

    # запрет торговых слов
    blob = " ".join(str(x) for x in (s1, sh, hp, kz)).lower()
    for bad in ("покупай", "продавай", "лонг", "шорт", "buy", "sell"):
        assert bad not in blob, bad
    print("aether_deep: все self-тесты пройдены ✓")
