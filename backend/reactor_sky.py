"""ОРАКУЛ // ПИФИЯ — ⚛ РЕАКТОР НЕБА (панель по канону мировоззрения 05.08.2026).

Не «знаки», а инженерная спецификация машины дня для думающих стадий ИИ:

  · ХРОНО-УДАР (Козырев): K_chrono = min(50, 1 + 1/(|dλ/dt| + 0.02)) —
    станция планеты (dλ/dt→0) работает как сопло, эффект ×50;
  · ИМПЕДАНС БИРКЕЛАНДА Z(θ): трин 120° = сверхпроводник (поток без работы),
    квадрат/оппозиция = ТЭН (генерация через сопротивление, Джоуль-Ленц);
    единственный источник формулы — aether.impedance (реюз, не копия);
  · ДОПЛЕР: радиальная скорость тела центральной разностью ±12 ч по дистанции —
    атака (тело идёт к нам, волна сжимается) / всасывание (уходит, растяжение);
  · КУРАМОТО/ХАКЕН: когерентность r (реюз aether.kuramoto_r; ω осцилляторов —
    РЕАЛЬНЫЕ |dλ/dt| эфемерид, станция → ω≈0 — v3.9, справочник нот убит) +
    катализатор — голос с максимальной сцепкой к средней фазе финального
    ансамбля; рабы — голоса, подчинённые среднему полю;
  · ПРИГОЖИН — окно неба по наталу ТИКЕРА: λ_eff(t) на суточной сетке 30 дней,
    жёсткие углы 90/180 (орб 3°) транзитов к наталу генезиса грузят λ вниз;
    первое пересечение нуля = дата окна слома;
  · КАМ (Колмогоров–Арнольд–Мозер): вместо герц-справочника — иррациональность
    отношений угловых скоростей пар; у золотого сечения — Φ-канал (устойчиво),
    у точной дроби p/r — рациональный мост (хрупко, резонансная раскачка).

Рамка честности (закон школы):
  🔵 механика — DE440 (skyfield), центральные разности, Z(θ), Курамото-Эйлер;
  🟡 язык школы — λ-нейтраль Пригожина, веса аспектов, пороги Φ-канала:
     символическая система, все прокси названы в note;
  ⚫ полюс за человеком — НЕ торговый сигнал, рынок не предсказывается, 18+.

ABSOLUTE OFFLINE: эфемериды — локальный .bsp через astro._load_sf(); ни одного
сетевого запроса. NO DUMMIES: нет данных → None + честный note. Без random —
детерминизм байт-в-байт.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np

try:
    from . import astro, aether
    from .aether import impedance  # Z(θ) — единый источник форка (реюз)
except ImportError:  # прямой запуск self-теста: python3 backend/reactor_sky.py
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(
        _os.path.abspath(__file__))))
    from backend import astro, aether
    from backend.aether import impedance

# ── константы (общие берутся из aether — литерал-копия была бы багом) ──
EPS_CHRONO = aether.EPS_CHRONO        # 0.02 °/сут — регуляризация станции
CHRONO_CAP = aether.CHRONO_CAP        # кэп хроно-члена Козырева (×50)
STATION_K = 5.0                       # K_chrono ≥ 5 → «станция» (хроно-узел)
PHI = (1.0 + 5.0 ** 0.5) / 2.0        # золотое сечение
HARD_ORB = 3.0                        # орб жёстких углов Пригожина, °
ASPECT_ORB = 4.0                      # орб классификации пар для Z-панели, °
LAMBDA_BASE = 0.5                     # 🟡 нейтраль неба: прокси, назван в note
DOPPLER_EPS_KMS = 0.35                # |v_r| ниже — «разворот» (turn)
KAM_V_MIN = 0.05                      # °/сут; медленнее — станция, КАМ молчит
AU_KM = 149_597_870.7
_BODY_ORDER = aether._BODY_ORDER
_GLYPH = aether._GLYPH
_HARD_W = {180.0: 1.0, 90.0: 0.75}    # 🟡 вес жёсткого угла (оппозиция/квадрат)
_ASP_ANGS = (0.0, 60.0, 90.0, 120.0, 180.0)
_ASP_NAME = {0.0: "соединение", 60.0: "секстиль", 90.0: "квадрат",
             120.0: "трин", 180.0: "оппозиция"}

FRAME_R = ("🔵 механика DE440/Z(θ)/Курамото · 🟡 язык школы (λ-нейтраль, веса "
           "аспектов, пороги Φ — прокси названы) · ⚫ полюс за человеком: "
           "НЕ торговый сигнал · рынок не предсказывается · 18+")


def _wrap180(x: float) -> float:
    return ((float(x) + 180.0) % 360.0) - 180.0


# ══════════════════════════════════════════════════════════════════════
# 1. ХРОНО-УДАР
# ══════════════════════════════════════════════════════════════════════

def k_chrono(spd_deg_day: float) -> float:
    """K = min(cap, 1 + 1/(|v|+ε)) — станция (v→0) даёт кэп ×50 🔵."""
    return float(min(CHRONO_CAP, 1.0 + 1.0 / (abs(float(spd_deg_day)) + EPS_CHRONO)))


# ══════════════════════════════════════════════════════════════════════
# 2. ДОПЛЕР
# ══════════════════════════════════════════════════════════════════════

def doppler_word(v_r_kms: float) -> str:
    """Классификация радиальной скорости: атака / всасывание / разворот."""
    if abs(v_r_kms) < DOPPLER_EPS_KMS:
        return "разворот"
    return "атака (идёт к нам, волна сжата)" if v_r_kms < 0 else \
        "всасывание (уходит, волна растянута)"


def _bodies_now(sf: dict, now: datetime) -> list | None:
    """Позиции/скорости/дистанции 10 тел центральной разностью ±12 ч 🔵.
    Один вызов skyfield на тело (массив из 3 времён)."""
    hh = now.hour + now.minute / 60.0 + np.array([-12.0, 0.0, 12.0])
    t = sf["ts"].utc(now.year, now.month, now.day, hh)
    rows = []
    for nm in _BODY_ORDER:
        bkey = sf["bodies"].get(nm)
        if not bkey:
            return None
        q = sf["earth"].at(t).observe(sf["eph"][bkey]).apparent().frame_latlon(sf["frame"])
        lam = np.asarray(q[1].degrees) % 360.0
        dist = np.asarray(q[2].au, dtype=float)
        spd = _wrap180(lam[2] - lam[0])            # °/сут (разность за 24 ч)
        v_r = (dist[2] - dist[0]) * AU_KM / 86400.0  # км/с по дистанции
        kc = k_chrono(spd)
        rows.append({"name": nm, "glyph": _GLYPH[nm],
                     "lon": round(float(lam[1]), 3),
                     "dist_au": round(float(dist[1]), 5),
                     "spd": round(float(spd), 4),
                     "k_chrono": round(kc, 2),
                     "station": bool(kc >= STATION_K),
                     "v_r_kms": round(float(v_r), 3),
                     "doppler": doppler_word(v_r)})
    return rows


# ══════════════════════════════════════════════════════════════════════
# 3. РЕАКТОРЫ Z(θ): сверхпроводники и ТЭНы
# ══════════════════════════════════════════════════════════════════════

def _impedance_pairs(rows: list) -> dict:
    """Пары тел по текущим λ: трины/секстили = поток, квадраты/оппозиции = ТЭНы.
    Z — реюз aether.impedance; мощность ТЭНа = Z·√(K_a·K_b) (хроно-масса пары)."""
    sup, heat = [], []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            sep = abs(_wrap180(rows[i]["lon"] - rows[j]["lon"]))
            ang = min(_ASP_ANGS, key=lambda a: abs(sep - a))
            dev = abs(sep - ang)
            if dev > ASPECT_ORB:
                continue
            z = impedance(sep)
            rec = {"pair": rows[i]["glyph"] + rows[j]["glyph"],
                   "names": f"{rows[i]['name']}–{rows[j]['name']}",
                   "asp": _ASP_NAME[ang], "dev": round(dev, 2),
                   "Z": round(z, 2)}
            if ang in (60.0, 120.0):
                sup.append(rec)
            elif ang in (90.0, 180.0):
                rec["power"] = round(z * math.sqrt(
                    rows[i]["k_chrono"] * rows[j]["k_chrono"]), 2)
                heat.append(rec)
    sup.sort(key=lambda r: (r["Z"], r["dev"]))
    heat.sort(key=lambda r: -r.get("power", 0.0))
    return {"superconductors": sup[:3], "heaters": heat[:3],
            "note": "Z(θ) — реюз aether.impedance 🔵; мощность ТЭНа = "
                    "Z·√(K_a·K_b) — хроно-масса пары, прокси 🟡"}


# ══════════════════════════════════════════════════════════════════════
# 4. КУРАМОТО-КАТАЛИЗАТОР (реюз aether.kuramoto_r + расширение)
# ══════════════════════════════════════════════════════════════════════

def _kuramoto_final_phases(lons: dict, spds: dict) -> np.ndarray:
    """Финальные фазы того же детерминированного Эйлера, что aether.kuramoto_r
    (те же KUR_STEPS/KUR_DT/KUR_K; ω = aether._omega_of — 2π·|dλ/dt|/360 из
    РЕАЛЬНЫХ скоростей эфемерид, станция → ω≈0; рассинхрон с aether был бы
    багом; v3.9: справочник 2π·W_VEDIC/60 убит — вердикт 05.08.2026)."""
    th = np.radians(np.array([lons[nm] for nm in _BODY_ORDER]))
    om = np.array([aether._omega_of(spds[nm]) for nm in _BODY_ORDER])
    for _ in range(aether.KUR_STEPS):
        th = th + aether.KUR_DT * (om + aether.KUR_K * np.mean(
            np.sin(th[None, :] - th[:, None]), axis=1))
    return th


def kuramoto_catalyst(rows: list) -> dict:
    """r неба (реюз aether.kuramoto_r; ω — реальные dλ/dt тел, v3.9) +
    катализатор: голос с максимальной сцепкой cos(θ_i − ψ) к средней фазе ψ
    финального ансамбля; рабы — сцепка ≥ 0.7 (подчинение Хакена), свободные —
    остальные."""
    lons = {r["name"]: float(r["lon"]) for r in rows}
    spds = {r["name"]: float(r["spd"]) for r in rows}
    trans_like = {"lam": {nm: np.array([lons[nm]]) for nm in _BODY_ORDER},
                  "vel": {nm: np.array([spds[nm]]) for nm in _BODY_ORDER}}
    r_val = aether.kuramoto_r(trans_like, 0)
    th = _kuramoto_final_phases(lons, spds)
    psi = float(np.angle(np.mean(np.exp(1j * th))))
    coup = np.cos(th - psi)
    order = sorted(range(len(_BODY_ORDER)), key=lambda i: -float(coup[i]))
    cat_i = order[0]
    slaved = [_BODY_ORDER[i] for i in order[1:] if float(coup[i]) >= 0.7]
    free = [_BODY_ORDER[i] for i in order[1:] if float(coup[i]) < 0.7]
    word = ("хор сцеплен — пейсмейкер ведёт" if r_val >= 0.6 else
            "частичная сцепка" if r_val >= 0.35 else
            "голоса разобраны — каждый о своём")
    return {"r": round(float(r_val), 3),
            "catalyst": _BODY_ORDER[cat_i],
            "catalyst_glyph": _GLYPH[_BODY_ORDER[cat_i]],
            "slaved": slaved, "free": free, "word": word,
            "note": "r — реюз aether.kuramoto_r 🔵, ω — реальные |dλ/dt| "
                    "эфемерид (станция → ω≈0, справочник нот убит v3.9); "
                    "катализатор/рабы — сцепка к средней фазе финального "
                    "ансамбля (порог 0.7 🟡)"}


# ══════════════════════════════════════════════════════════════════════
# 5. ПРИГОЖИН — окно неба по наталу тикера
# ══════════════════════════════════════════════════════════════════════

def _lambda_series(lam: dict, nat_lons: dict) -> np.ndarray:
    """λ_eff(t) = LAMBDA_BASE − Σ нагрузка жёстких углов (90/180, орб 3°)
    транзитов к наталу. Чистая функция — тестируется литералами."""
    names_t = [nm for nm in _BODY_ORDER if nm in lam]
    n = len(lam[names_t[0]]) if names_t else 0
    load = np.zeros(n)
    for nm in names_t:
        lt = np.asarray(lam[nm], dtype=float)
        for lon0 in nat_lons.values():
            sep = np.abs(((lt - float(lon0) + 180.0) % 360.0) - 180.0)
            for ang, w in _HARD_W.items():
                dev = np.abs(sep - ang)
                hit = dev <= HARD_ORB
                if hit.any():
                    load[hit] += w * (1.0 - dev[hit] / HARD_ORB)
    return LAMBDA_BASE - load


def _first_crossing(lam_eff: np.ndarray, ts: np.ndarray):
    """Первый уход λ_eff ниже нуля → (индекс, unix-время) либо None."""
    for k in range(len(lam_eff)):
        if lam_eff[k] < 0.0:
            return k, float(ts[k])
    return None


def prigogine_window(ticker: str, now: datetime, days: int = 30) -> dict | None:
    """Окно слома неба по наталу генезиса тикера: суточная сетка days дней.
    Нет эфемерид/натала → None (NO DUMMIES, note у вызывающего)."""
    g = aether.genesis_for(ticker)
    nat = aether.natal_lons(g["genesis"])
    if not nat:
        return None
    t0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    trans = aether.transit_series(t0, days * 24.0, 24.0)
    if trans is None:
        return None
    nat_lons = {r["name"]: r["lon"] for r in nat}
    lam_eff = _lambda_series(trans["lam"], nat_lons)
    cross = _first_crossing(lam_eff, trans["ts"])
    out = {"lambda_now": round(float(lam_eff[0]), 3),
           "lambda_min": round(float(lam_eff.min()), 3),
           "crossing": bool(cross), "date": None, "top_hit": None,
           "genesis_key": g["key"], "genesis_fallback": bool(g.get("fallback")),
           "note": ("λ_eff = 0.5 − Σ жёстких углов 90/180 (орб 3°) транзитов "
                    "к наталу генезиса; нейтраль 0.5 — прокси 🟡 (термодинамики "
                    "карты у бумаги нет), геометрия углов — DE440 🔵")}
    if cross:
        k, t_unix = cross
        out["date"] = datetime.fromtimestamp(
            t_unix, timezone.utc).strftime("%Y-%m-%d")
        # самый глубокий вклад в день пересечения — кто грузит λ
        best = None
        for nm in _BODY_ORDER:
            lt = float(trans["lam"][nm][k])
            for r0 in nat:
                sep = abs(_wrap180(lt - r0["lon"]))
                for ang, w in _HARD_W.items():
                    dev = abs(sep - ang)
                    if dev <= HARD_ORB:
                        depth = w * (1.0 - dev / HARD_ORB)
                        if best is None or depth > best[0]:
                            best = (depth, f"транзит {_GLYPH[nm]}{nm} "
                                    f"{_ASP_NAME[ang]} наталу "
                                    f"{_GLYPH[r0['name']]}{r0['name']}")
        out["top_hit"] = best[1] if best else None
    return out


# ══════════════════════════════════════════════════════════════════════
# 6. КАМ — мера иррациональности отношений скоростей
# ══════════════════════════════════════════════════════════════════════

def _frac_grid():
    """Сетка дробей p/r (r≤5, p≤9) ≥ 1 с подписью наименьшего знаменателя."""
    grid = {}
    for r in range(1, 6):
        for p in range(1, 10):
            v = p / r
            if v < 1.0:
                continue
            key = round(v, 9)
            if key not in grid or r < grid[key][1]:
                grid[key] = (f"{p}/{r}", r)
    vals = sorted(grid)
    return vals, {v: grid[v][0] for v in vals}


_FRAC_VALS, _FRAC_LBL = _frac_grid()


def kam_of(q: float) -> dict | None:
    """Мера иррациональности отношения q=|v_a|/|v_b| (формула контракта):
    irr = |q − ближайшая p/r| (r≤5, p≤9), нормированная на полуширину
    локального интервала между соседними дробями, клип 0..1.
    Φ-канал: |q−φ|<0.06 или |q−1/φ|<0.04 (устойчиво, КАМ-тор держит);
    рациональный мост: irr<0.02 (хрупко, резонансная раскачка)."""
    if q is None or not math.isfinite(q) or q <= 0.0:
        return None
    phi_dev = min(abs(q - PHI), abs(q - 1.0 / PHI))
    is_phi = (abs(q - PHI) < 0.06) or (abs(q - 1.0 / PHI) < 0.04)
    qq = q if q >= 1.0 else 1.0 / q     # отношение симметрично
    if qq > _FRAC_VALS[-1]:
        # за сеткой низких резонансов — глубоко иррационально для наших порядков
        return {"q": round(q, 4), "irr": 1.0, "near": None,
                "phi": is_phi, "phi_dev": round(phi_dev, 4), "rational": False}
    i = int(np.argmin([abs(qq - v) for v in _FRAC_VALS]))
    f = _FRAC_VALS[i]
    neigh = []
    if i > 0:
        neigh.append(f - _FRAC_VALS[i - 1])
    if i + 1 < len(_FRAC_VALS):
        neigh.append(_FRAC_VALS[i + 1] - f)
    half = max(min(neigh) / 2.0, 1e-9) if neigh else 1e-9
    irr = min(abs(qq - f) / half, 1.0)
    return {"q": round(q, 4), "irr": round(irr, 3), "near": _FRAC_LBL[f],
            "phi": is_phi, "phi_dev": round(phi_dev, 4),
            "rational": bool(irr < 0.02)}


def kam_pairs(rows: list) -> dict:
    """Φ-каналы (топ-3) и рациональный мост (топ-1) по парам текущих скоростей.
    Станции (|v| < 0.05°/сут) честно исключены — отношение не определено."""
    live = [r for r in rows if abs(r["spd"]) >= KAM_V_MIN]
    chans, bridges = [], []
    for i in range(len(live)):
        for j in range(i + 1, len(live)):
            q = abs(live[i]["spd"]) / abs(live[j]["spd"])
            k = kam_of(q)
            if not k:
                continue
            rec = {"pair": live[i]["glyph"] + live[j]["glyph"],
                   "names": f"{live[i]['name']}/{live[j]['name']}", **k}
            if k["phi"]:
                chans.append(rec)
            if k["rational"]:
                bridges.append(rec)
    chans.sort(key=lambda r: r["phi_dev"])
    bridges.sort(key=lambda r: r["irr"])
    return {"phi_channels": chans[:3], "bridge": bridges[0] if bridges else None,
            "skipped_stations": [r["name"] for r in rows
                                 if abs(r["spd"]) < KAM_V_MIN],
            "note": ("вместо герц-справочника — иррациональность отношений "
                     "угловых скоростей (КАМ) 🔵; пороги Φ/моста — шкала "
                     "школы 🟡")}


# ══════════════════════════════════════════════════════════════════════
# СБОРКА КОНТЕКСТА + ТЕКСТ ДЛЯ ИИ
# ══════════════════════════════════════════════════════════════════════

def reactor_context(ticker: str | None = None,
                    now: datetime | None = None) -> dict:
    """Панель реактора неба. Каждый орган — в своей защите: сбой одного
    даёт None + note, остальные живут (аддитивность)."""
    now = (now or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    sf = astro._load_sf()
    if not sf:
        return {"mode": "unavailable", "ticker": ticker,
                "reason": "эфемериды (.bsp) недоступны — реактор честно молчит",
                "frame": FRAME_R}
    err = ""
    try:
        rows = _bodies_now(sf, now)
    except Exception as e:                       # noqa: BLE001 — защита слоя
        rows = None
        err = ": " + str(e)[:120]
    if not rows:
        return {"mode": "unavailable", "ticker": ticker,
                "reason": "кинематика тел не посчиталась" + err,
                "frame": FRAME_R}
    ctx = {"mode": "precise", "ticker": (ticker or "").upper() or None,
           "ts": now.isoformat(), "bodies": rows,
           "stations": [{"name": r["name"], "glyph": r["glyph"],
                         "k_chrono": r["k_chrono"]}
                        for r in rows if r["station"]],
           "frame": FRAME_R}
    try:
        ctx["impedance"] = _impedance_pairs(rows)
    except Exception as e:                       # noqa: BLE001
        ctx["impedance"] = None
        ctx["impedance_note"] = "Z-панель не посчиталась: " + str(e)[:100]
    try:
        ctx["kuramoto"] = kuramoto_catalyst(rows)
    except Exception as e:                       # noqa: BLE001
        ctx["kuramoto"] = None
        ctx["kuramoto_note"] = "Курамото не посчитался: " + str(e)[:100]
    try:
        ctx["prigogine"] = prigogine_window(ticker, now) if ticker else None
        if ticker and ctx["prigogine"] is None:
            ctx["prigogine_note"] = ("окно Пригожина не посчиталось: натал "
                                     "генезиса недоступен (NO DUMMIES)")
        elif not ticker:
            ctx["prigogine_note"] = "тикер не задан — окно по наталу не считается"
    except Exception as e:                       # noqa: BLE001
        ctx["prigogine"] = None
        ctx["prigogine_note"] = "окно Пригожина упало: " + str(e)[:100]
    try:
        ctx["kam"] = kam_pairs(rows)
    except Exception as e:                       # noqa: BLE001
        ctx["kam"] = None
        ctx["kam_note"] = "КАМ не посчитался: " + str(e)[:100]
    return ctx


def render_for_ai(ctx: dict) -> str:
    """Компактная панель для думающих стадий (≤ ~1400 токенов).
    НЕ дублирует астро-блок (созвездия/аспекты/станции-даты живут там):
    здесь — механика реактора."""
    if not isinstance(ctx, dict) or not ctx:
        return ""
    L = ["═══ ⚛ РЕАКТОР НЕБА (канон 05.08: хроно-удар · Z(θ) · Доплер · "
         "Курамото · Пригожин · КАМ) ═══"]
    if ctx.get("mode") != "precise":
        L.append(f"недоступен: {ctx.get('reason', 'нет данных')}")
        L.append(ctx.get("frame", FRAME_R))
        return "\n".join(L)
    rows = ctx.get("bodies") or []
    top_k = sorted(rows, key=lambda r: -r["k_chrono"])[:3]
    st = ctx.get("stations") or []
    L.append("ХРОНО-УДАР 🔵 K=min(50,1+1/(|v|+0.02)): "
             + " · ".join(f"{r['glyph']}{r['name']} K={r['k_chrono']:g}"
                          + (" СТАНЦИЯ-СОПЛО" if r["station"] else "")
                          for r in top_k)
             + ("" if st else " · станций нет — хроно-узлы не активны"))
    dop = sorted(rows, key=lambda r: -abs(r["v_r_kms"]))[:4]
    L.append("ДОПЛЕР 🔵 (v_r ±12ч по дистанции): "
             + " · ".join(f"{r['glyph']} {r['v_r_kms']:+.1f} км/с {r['doppler']}"
                          for r in dop))
    imp = ctx.get("impedance")
    if imp:
        if imp["superconductors"]:
            L.append("СВЕРХПРОВОДНИКИ (поток без работы, Z→0): "
                     + " · ".join(f"{r['pair']} {r['asp']} Z={r['Z']:g}"
                                  for r in imp["superconductors"]))
        if imp["heaters"]:
            L.append("ТЭНы (генерация через сопротивление): "
                     + " · ".join(f"{r['pair']} {r['asp']} Z={r['Z']:g} "
                                  f"мощн={r['power']:g}"
                                  for r in imp["heaters"]))
        if not imp["superconductors"] and not imp["heaters"]:
            L.append("РЕАКТОРЫ Z(θ): точных углов сейчас нет — контур разомкнут")
    else:
        L.append("РЕАКТОРЫ Z(θ): " + ctx.get("impedance_note", "нет данных"))
    kur = ctx.get("kuramoto")
    if kur:
        L.append(f"КУРАМОТО: r={kur['r']:g} — {kur['word']}; катализатор "
                 f"{kur['catalyst_glyph']}{kur['catalyst']}; рабы: "
                 + (", ".join(kur["slaved"]) if kur["slaved"] else "нет")
                 + "; свободны: "
                 + (", ".join(kur["free"][:4]) if kur["free"] else "нет"))
    else:
        L.append("КУРАМОТО: " + ctx.get("kuramoto_note", "нет данных"))
    pr = ctx.get("prigogine")
    if pr:
        gen = pr["genesis_key"] + (" (фолбэк 🟡)" if pr["genesis_fallback"] else "")
        if pr["crossing"]:
            L.append(f"ПРИГОЖИН (натал {gen}): λ_now={pr['lambda_now']:+g}, "
                     f"ОКНО СЛОМА ~{pr['date']}"
                     + (f" — {pr['top_hit']}" if pr["top_hit"] else ""))
        else:
            L.append(f"ПРИГОЖИН (натал {gen}): λ_now={pr['lambda_now']:+g}, "
                     f"min λ за 30 дн = {pr['lambda_min']:+g} — окно НЕ "
                     "открывается, линейная фаза")
        L.append("  " + pr["note"])
    else:
        L.append("ПРИГОЖИН: " + ctx.get("prigogine_note", "нет данных"))
    kam = ctx.get("kam")
    if kam:
        if kam["phi_channels"]:
            L.append("КАМ Φ-каналы (устойчиво, тор держит): "
                     + " · ".join(f"{r['pair']} q={r['q']:g} (Δφ={r['phi_dev']:g})"
                                  for r in kam["phi_channels"]))
        else:
            L.append("КАМ: Φ-каналов сейчас нет")
        if kam["bridge"]:
            b = kam["bridge"]
            L.append(f"КАМ рациональный мост (хрупко, раскачка): {b['pair']} "
                     f"q={b['q']:g}≈{b['near']}")
    else:
        L.append("КАМ: " + ctx.get("kam_note", "нет данных"))
    L.append(ctx.get("frame", FRAME_R))
    return "\n".join(L)


# ══════════════════════════════════════════════════════════════════════
# SELF-TEST
# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # 1) хроно-удар: клампы и монотонность
    assert k_chrono(0.0) == CHRONO_CAP == 50.0            # станция → кэп
    assert abs(k_chrono(1.0) - (1.0 + 1.0 / 1.02)) < 1e-9
    assert k_chrono(0.01) > k_chrono(0.1) > k_chrono(1.0) > k_chrono(13.2)
    assert k_chrono(-0.5) == k_chrono(0.5)                # знак не важен
    assert 1.0 < k_chrono(500.0) < 1.01

    # 2) Z-калибровка (журнал REAL SKY): Z(0)=0, 90°>номинала, 120° — провал
    assert impedance(0.0) < 1e-9
    assert abs(impedance(90.0) - 1.30) < 0.05
    assert abs(impedance(120.0) - 0.69) < 0.05
    assert abs(impedance(60.0) - 0.91) < 0.05
    assert impedance(120.0) < impedance(90.0)             # трин — поток, квадрат — ТЭН
    assert impedance(180.0) > 1.0

    # 3) Доплер: классификация
    assert "атака" in doppler_word(-5.0)
    assert "всасывание" in doppler_word(5.0)
    assert doppler_word(0.1) == "разворот"

    # 3б) Курамото v3.9: ω — реальные скорости (станция → ω=0), детерминизм
    assert aether._omega_of(0.0) == 0.0                  # замершее тело не крутит
    assert aether._omega_of(13.2) > aether._omega_of(1.0) > 0.0
    rows_syn = [{"name": nm, "glyph": _GLYPH[nm], "lon": 36.0 * i,
                 "spd": (13.2 if nm == "Луна" else 0.05 * i)}
                for i, nm in enumerate(_BODY_ORDER)]
    kc_syn = kuramoto_catalyst(rows_syn)
    assert 0.0 <= kc_syn["r"] <= 1.0 and kc_syn["catalyst"] in _BODY_ORDER
    assert kc_syn == kuramoto_catalyst(rows_syn)         # детерминизм байт-в-байт

    # 4) КАМ: золотое сечение — Φ-канал; 3/2 — рациональный мост
    kφ = kam_of(PHI)
    assert kφ and kφ["phi"] and not kφ["rational"]
    kφi = kam_of(1.0 / PHI)                               # обратное отношение
    assert kφi and kφi["phi"]
    k32 = kam_of(1.5)
    assert k32 and k32["rational"] and k32["near"] == "3/2" and k32["irr"] < 0.02
    kr2 = kam_of(math.sqrt(2.0))                          # иррационален, не Φ
    assert kr2 and not kr2["rational"] and not kr2["phi"] and kr2["irr"] > 0.1
    kbig = kam_of(4000.0)                                 # за сеткой дробей
    assert kbig and kbig["irr"] == 1.0 and kbig["near"] is None
    assert kam_of(0.0) is None and kam_of(float("nan")) is None
    for probe in (0.3, 0.77, 1.0, 1.618, 2.5, 9.0, 100.0):
        kk = kam_of(probe)
        assert kk and 0.0 <= kk["irr"] <= 1.0             # кламп меры

    # 5) Пригожин: чистая λ-серия на литералах (без эфемерид)
    lam_syn = {"Марс": np.array([80.0, 85.0, 90.0, 95.0])}   # идёт на квадрат к 0°
    nat_syn = {"Солнце": 0.0}
    le = _lambda_series(lam_syn, nat_syn)
    assert le.shape == (4,)
    assert le[0] == LAMBDA_BASE and le[1] == LAMBDA_BASE      # орб 3° ещё не взят
    assert le[2] < 0.0, le                                    # точный квадрат: 0.5−0.75<0
    cr = _first_crossing(le, np.array([0.0, 1.0, 2.0, 3.0]))
    assert cr and cr[0] == 2 and cr[1] == 2.0
    lam_far = {"Марс": np.array([40.0, 41.0, 42.0])}          # без жёстких углов
    lef = _lambda_series(lam_far, nat_syn)
    assert np.all(lef == LAMBDA_BASE)
    assert _first_crossing(lef, np.array([0.0, 1.0, 2.0])) is None

    # 6) рендер: вырожденные входы не падают, рамка на месте
    assert render_for_ai({}) == "" and render_for_ai(None) == ""
    t_un = render_for_ai({"mode": "unavailable", "reason": "нет .bsp",
                          "frame": FRAME_R})
    assert "недоступен" in t_un and "18+" in t_un

    # 7) эфемеридная часть — если .bsp рядом (иначе честный пропуск)
    if astro._load_sf():
        NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
        ctx1 = reactor_context("BRU6", now=NOW)
        assert ctx1["mode"] == "precise" and len(ctx1["bodies"]) == 10
        for r in ctx1["bodies"]:
            assert 1.0 <= r["k_chrono"] <= 50.0
            assert r["doppler"] in ("разворот",) or "волна" in r["doppler"]
        assert ctx1["kuramoto"] and 0.0 <= ctx1["kuramoto"]["r"] <= 1.0
        assert ctx1["kuramoto"]["catalyst"] in _BODY_ORDER
        assert ctx1["prigogine"] and ctx1["prigogine"]["genesis_key"] == "BR"
        assert isinstance(ctx1["prigogine"]["crossing"], bool)
        assert ctx1["kam"] is not None
        ctx2 = reactor_context("BRU6", now=NOW)
        assert repr(ctx1) == repr(ctx2), "детерминизм нарушен"
        txt = render_for_ai(ctx1)
        assert "⚛ РЕАКТОР" in txt and "🔵" in txt and "⚫" in txt and "18+" in txt
        assert len(txt) < 4500, len(txt)                  # ≤ ~1400 токенов
        low = txt.lower()
        for bad in ("покупай", "продавай", "лонг", "шорт", "buy", "sell"):
            assert bad not in low, bad
        ctx0 = reactor_context(None, now=NOW)             # без тикера — честно
        assert ctx0["prigogine"] is None and "тикер" in ctx0["prigogine_note"]
        assert "18+" in render_for_ai(ctx0)
        print("reactor_sky: эфемеридные тесты пройдены ✓ (панель жива, "
              "детерминизм байт-в-байт)")
    else:
        print("reactor_sky: .bsp не найден — эфемеридные тесты честно пропущены")
    print("reactor_sky: self-test пройден ✓")
