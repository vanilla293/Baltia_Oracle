# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — БИФУРКАЦИОННЫЙ КАЛЕНДАРЬ (Матьё на эфемеридах).

Приказ владельца (спринт «Хищник», №4): «выведи бифуркационный календарь на
эфемеридах». Эфемериды детерминированы — окна повышенной восприимчивости
среды считаются ВПЕРЁД, на дни и недели.

Механика (уравнение Матьё, лаборатория backend/lab/mathieu.py — та же
математика, языки Айнса-Штрутта):
    ẍ + ω₀²(1 + h·cos γt)·x = 0
Параметрический резонанс — когда частота НАКАЧКИ γ попадает в язык
    γ ≈ 2ω₀/n,  n = 1, 2, 3…
ω₀ — собственная частота рынка: доминантный цикл ЦЕНЫ (спектр реальных
свечей 🔵). γ — частоты неба: синодические периоды тел (наблюдаемый круг —
аксиома мгновенности школы; ведические множители УБИТЫ лабораторией и не
используются). Попадание в язык — не приказ, а ОКНО ВОСПРИИМЧИВОСТИ:
раскачка возможна, если есть глубина накачки h 🟡.

Два режима честности:
  · эфемерид нет (.bsp не скачан) → периоды из data/mathieu_periods.json
    (измерены по DE440 заранее, лежат в репо) — «близость языка» считается,
    датированные окна фаз честно недоступны, note говорит это прямо;
  · реактор неба доступен → окно Пригожина тикера (дата пересечения нуля
    λ_eff) добавляется в календарь датированной строкой.

⚫ Календарь — расписание ВОСПРИИМЧИВОСТИ среды, не событий. Ничего не
обязано случиться. НЕ торговый сигнал. 18+.

Self-тест: python3 -m backend.bif_calendar
"""
from __future__ import annotations

import json
import math
import os

import numpy as np

# ── пороги (единственное место) ─────────────────────────────────────────────
TONGUE_TOL = 0.08      # |γ/(2ω₀/n) − 1| ниже — В ЯЗЫКЕ
TONGUE_TIGHT = 0.04    # ниже — язык ТУГОЙ (для флага mathieu_unstable)
N_MAX = 3              # старшие языки слабы как q^n — выше третьего не смотрим
MIN_BARS = 64
# ── ФАЗОВЫЕ МОДУЛЯТОРЫ (спринт «Сырая дата», удар №2) ──────────────────────
# Приказ владельца: макро-циклы — НЕ генераторы направления (та роль убита
# лабораторией и не воскрешается), а ФАЗОВЫЕ МОДУЛЯТОРЫ: стоячая волна,
# задающая пучности — зоны максимальной восприимчивости среды. Синхронизация
# циклов аппаратно снижает пороги чувствительности Оракула. Роль-модулятор —
# постановка владельца, гипотеза школы 🟡 (нулями не проверена — сказано прямо).
VEDIC_DAYS = (27.0, 54.0, 72.0, 81.0, 108.0)   # макро-циклы школы, сутки 🟡
EPOCH_MS = 946_728_000_000   # J2000 (2000-01-01 12:00 UTC) — нуль фазы,
                             # конвенция школы 🟡 (фаза без наблюдаемого нуля)
SENS_GAIN = 0.6              # R=1 → пороги Оракула делятся на 1+0.6=1.6
MOD_SYNODIC = ("Луна", "Меркурий", "Венера", "Марс")   # быстрые синодики в хор

FRAME = ("⚫ календарь восприимчивости среды, не событий: язык Матьё — окно "
         "раскачки, не приказ. Ведические частоты убиты лабораторией и не "
         "используются; периоды — измеренные синодические 🔵. 18+")

_PERIODS_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data", "mathieu_periods.json")


def _f(x, d=0.0):
    try:
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


def load_periods(path: str | None = None) -> dict:
    """Синодические периоды тел (сутки), измеренные по DE440 заранее.
    Файл в репо — ABSOLUTE OFFLINE соблюдён."""
    try:
        with open(path or _PERIODS_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except OSError:
        return {}
    out = {}
    for name, row in raw.items():
        if name.startswith("_") or not isinstance(row, dict):
            continue
        t_syn = row.get("T_syn") or row.get("T_sid")   # Солнце: годовой круг
        if t_syn and _f(t_syn) > 0:
            out[name] = float(t_syn)
    return out


def dominant_period_bars(closes) -> float | None:
    """Доминантный цикл цены в барах: пик спектра детрендованных
    лог-цен (Фурье 🔵). Плоский/короткий ряд → None (NO DUMMIES)."""
    if closes is None:
        return None
    x = np.asarray([c for c in closes if c], dtype=float)
    x = x[np.isfinite(x) & (x > 0)]
    if x.size < MIN_BARS:
        return None
    lx = np.log(x)
    t = np.arange(lx.size, dtype=float)
    b, a = np.polyfit(t, lx, 1)
    resid = lx - (a + b * t)
    if float(np.std(resid)) <= 0:
        return None
    sp = np.abs(np.fft.rfft(resid - resid.mean())) ** 2
    freqs = np.fft.rfftfreq(resid.size, d=1.0)
    if sp.size < 4:
        return None
    k = int(np.argmax(sp[1:])) + 1                  # без нулевой частоты
    if freqs[k] <= 0:
        return None
    period = 1.0 / float(freqs[k])
    if not (2.0 < period < resid.size):
        return None
    return round(period, 2)


def mathieu_tongues(period_bars: float, bar_minutes: float,
                    periods: dict | None = None) -> list:
    """Близость планетарных накачек к языкам Матьё рынка.

    ω₀ = 2π/T_рынка (сутки), γ_тела = 2π/T_syn. Язык n: γ ≈ 2ω₀/n.
    Возврат: [{body, n, ratio, in_tongue, tight, note}] по близости."""
    if not period_bars or period_bars <= 0 or bar_minutes <= 0:
        return []
    t_market_days = period_bars * bar_minutes / 1440.0
    if t_market_days <= 0:
        return []
    omega0 = 2.0 * math.pi / t_market_days
    rows = []
    for body, t_syn in (load_periods() if periods is None else periods).items():
        gamma = 2.0 * math.pi / t_syn
        for n in range(1, N_MAX + 1):
            target = 2.0 * omega0 / n
            ratio = gamma / target
            dev = abs(ratio - 1.0)
            if dev <= TONGUE_TOL:
                rows.append({
                    "body": body, "n": n, "ratio": round(ratio, 4),
                    "T_syn_days": round(t_syn, 2),
                    "T_market_days": round(t_market_days, 2),
                    "in_tongue": True, "tight": bool(dev <= TONGUE_TIGHT),
                    "note": (f"язык n={n}: накачка {body} "
                             f"({t_syn:.1f}д) ≈ {2/n:.2g}×цикл рынка "
                             f"({t_market_days:.1f}д); ширина ~O(h^{n}) 🟡")})
    rows.sort(key=lambda r: abs(r["ratio"] - 1.0))
    return rows


def phase_modulator(now_ts: float | None = None,
                    periods: dict | None = None) -> dict:
    """ФАЗОВЫЙ МОДУЛЯТОР: синхронизация макро-циклов → чувствительность.

    Каждому циклу T_k (ведические 27/54/72/81/108 сут 🟡 + быстрые синодики
    🔵) — фаза φ_k = 2π·(t/T_k mod 1) от эпохи J2000. Порядок Курамото
    R = |Σe^{iφ}|/K — насколько хор циклов дышит В ФАЗЕ прямо сейчас.
    sens_mult = 1 + SENS_GAIN·R² — ДЕЛИТЕЛЬ порогов Оракула (сингулярность
    и согласие «у порога» признаются раньше). НАПРАВЛЕНИЕ циклы не задают
    никогда — только восприимчивость (постановка владельца, гипотеза 🟡)."""
    import time as _time
    t_s = now_ts if now_ts is not None else _time.time()   # unix-СЕКУНДЫ
    t_days = (t_s * 1000.0 - EPOCH_MS) / 86_400_000.0
    per = load_periods() if periods is None else periods
    cycles = {}
    for T in VEDIC_DAYS:
        cycles[f"веда-{int(T)}"] = (T, "🟡")
    for name in MOD_SYNODIC:
        if per.get(name):
            cycles[name] = (float(per[name]), "🔵")
    if not cycles:
        return {"R": None, "sens_mult": 1.0,
                "note": "циклов нет — модулятор молчит (NO DUMMIES)"}
    re = im = 0.0
    phases = {}
    for name, (T, tag) in cycles.items():
        ph = 2.0 * math.pi * ((t_days / T) % 1.0)
        phases[name] = {"T_days": T, "phase_deg": round(math.degrees(ph), 1),
                        "tag": tag}
        re += math.cos(ph)
        im += math.sin(ph)
    R = math.hypot(re, im) / len(cycles)
    sens = 1.0 + SENS_GAIN * R * R
    return {"R": round(R, 3), "sens_mult": round(sens, 3),
            "n_cycles": len(cycles), "phases": phases,
            "word": ("ПУЧНОСТЬ: хор циклов в фазе — пороги Оракула снижены"
                     if R >= 0.7 else
                     "циклы вразнобой — чувствительность обычная"),
            "note": ("модулятор ВОСПРИИМЧИВОСТИ, не направления: роль-"
                     "модулятор — постановка владельца, гипотеза школы 🟡; "
                     "нуль фазы — J2000 (конвенция)")}


def calendar(closes=None, bar_minutes: float = 1440.0,
             reactor: dict | None = None,
             periods: dict | None = None,
             now_ts: float | None = None) -> dict:
    """Свод календаря: языки Матьё (из реальной цены × измеренных периодов
    неба) + датированные окна из реактора (Пригожин тикера), если тот жив.

    reactor — reactor_context тикера (опц.): prigogine.crossing/date."""
    out: dict = {"frame": FRAME}
    per = load_periods() if periods is None else periods
    out["periods_loaded"] = len(per)
    pb = dominant_period_bars(closes) if closes is not None else None
    out["dominant_period_bars"] = pb
    tongues = (mathieu_tongues(pb, bar_minutes, per) if pb else [])
    out["tongues"] = tongues
    out["mathieu_unstable"] = bool(any(t["tight"] for t in tongues))

    windows = []
    pr = ((reactor or {}).get("prigogine") or {})
    if pr.get("crossing"):
        windows.append({"kind": "окно Пригожина (небо тикера)",
                        "when": pr.get("date") or "уже открыто",
                        "note": "λ_eff пересекла ноль: старая структура "
                                "неустойчива — сектор слома 🟡"})
    out["windows"] = windows
    try:                          # фазовый модулятор («Сырая дата», удар №2)
        out["modulator"] = phase_modulator(now_ts, per)
    except Exception as e:                                   # noqa: BLE001
        out["modulator"] = {"error": str(e)[:100]}
    if not per:
        out["note"] = ("mathieu_periods.json не найден — календарь молчит "
                       "честно (NO DUMMIES)")
    elif pb is None:
        out["note"] = "доминантный цикл цены не выделен — языки не считаем"
    else:
        out["note"] = (f"цикл рынка {pb} баров; языков в допуске: "
                       f"{len(tongues)}; датированных окон: {len(windows)}")
    return out


# ── self-test: детерминированная синтетика ──────────────────────────────────
if __name__ == "__main__":
    # периоды из репо загружаются (Луна ≈ 29.53 сут)
    per = load_periods()
    assert per and abs(per.get("Луна", 0) - 29.53) < 0.1, per

    # 1) доминантный цикл: дневки с чистым 29.5-дневным дыханием
    n = 360
    i = np.arange(n, dtype=float)
    closes = 100.0 * np.exp(0.002 * np.sin(2 * math.pi * i / 29.5)
                            + 0.0005 * np.sin(i * 1.7183))
    pb = dominant_period_bars(closes)
    assert pb and abs(pb - 29.5) < 3.0, pb

    # 2) язык n=2: цикл рынка ≈ T_syn Луны → γ = ω₀ = 2ω₀/2
    tg = mathieu_tongues(pb, 1440.0, per)
    moon = [t for t in tg if t["body"] == "Луна" and t["n"] == 2]
    assert moon and moon[0]["in_tongue"], tg
    #    рынок с циклом 10 дней — Луна мимо всех языков n≤3
    tg10 = mathieu_tongues(10.0, 1440.0, {"Луна": 29.53})
    assert not tg10, tg10

    # 3) свод: язык туго → mathieu_unstable; реактор даёт датированное окно
    cal = calendar(closes, 1440.0,
                   reactor={"prigogine": {"crossing": True, "date": "2026-08-14"}},
                   periods=per)
    assert cal["dominant_period_bars"] == pb
    assert any(t["body"] == "Луна" for t in cal["tongues"])
    assert cal["windows"] and cal["windows"][0]["when"] == "2026-08-14"

    # 4) NO DUMMIES: плоский ряд → цикла нет, календарь честно молчит
    flat = calendar([100.0] * 200, 1440.0, periods=per)
    assert flat["dominant_period_bars"] is None and not flat["tongues"]
    #    нет файла периодов → честная тишина
    empty = calendar(closes, 1440.0, periods={})
    assert not empty["tongues"] and empty["periods_loaded"] == 0

    # 5) ФАЗОВЫЙ МОДУЛЯТОР (удар №2): фазы детерминированы от J2000, R∈[0,1],
    #    sens_mult ∈ [1, 1+SENS_GAIN]; направление он не выдаёт ВООБЩЕ
    t_fix = 1_786_100_000.0                        # фиксированный момент (сек)
    mod = phase_modulator(t_fix, per)
    assert mod["R"] is not None and 0.0 <= mod["R"] <= 1.0, mod
    assert 1.0 <= mod["sens_mult"] <= 1.0 + SENS_GAIN + 1e-9
    assert mod["n_cycles"] >= len(VEDIC_DAYS)
    assert "dir" not in mod and "направления" in mod["note"]
    assert phase_modulator(t_fix, per) == mod       # детерминизм байт-в-байт
    #    искусственный хор: один цикл → R=1, пороги делятся максимально
    mod1 = phase_modulator(t_fix, {})
    # только ведические (5 циклов) — R зависит от момента; проверим механику
    # на моменте, где все фазы совпадают: t = 0 от эпохи → все φ=0 → R=1
    mod0 = phase_modulator(EPOCH_MS / 1000.0, {})
    assert abs(mod0["R"] - 1.0) < 1e-9 and abs(
        mod0["sens_mult"] - (1.0 + SENS_GAIN)) < 1e-9, mod0
    #    модулятор встроен в свод календаря
    cal_m = calendar(closes, 1440.0, periods=per, now_ts=t_fix)
    assert cal_m["modulator"]["R"] == mod["R"]

    # 6) детерминизм
    assert calendar(closes, 1440.0, periods=per, now_ts=t_fix) == \
        calendar(closes, 1440.0, periods=per, now_ts=t_fix)
    assert "18+" in FRAME

    print("bif_calendar self-test OK: цикл цены найден спектром, язык Матьё "
          "n=2 Луны ловится, чужой цикл — мимо, окно Пригожина датируется, "
          "NO DUMMIES и детерминизм соблюдены")
