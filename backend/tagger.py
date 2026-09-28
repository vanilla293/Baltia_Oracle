# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — ФИЗИК (Агент 2: разметка макро-состояния среды).

Проект PREDATOR, Агент 2 «Macro & Ether Engine». Задача владельца:
«параллельно с Пылесосом высчитывать макро-состояние среды и размечать
временной ряд тегами плотности — где среда ВЯЗКАЯ, где НАЭЛЕКТРИЗОВАННАЯ».

Композиция (переиспользует уже построенные движки, не переписывает):
  · Пригожин (bifurcation.prigogine_market) — критическое замедление: среда
    перестаёт гасить отклонения → близость слома;
  · Ляпунов (bifurcation.lyapunov) — горизонт предсказуемости: короткий
    горизонт = наэлектризовано (хаос), длинный = вязко (инерция);
  · Хилл (bifurcation.hill_tail) — толщина хвостов: тонкие → спокойно,
    толстые → редкий удар близко;
  · Казимир (microstructure.casimir) — давление внутри сужающегося спреда
    ∝1/a⁴: сжатая пружина = наэлектризовано;
  · фаза-модулятор (bif_calendar.phase_modulator) — пучность стоячей волны
    циклов повышает восприимчивость среды 🟡.

ТЕГ ПЛОТНОСТИ (energy 0..1):
  energy = w·[давление слома + (1−предсказуемость) + толстые хвосты +
              Казимир-сжатие + пучность модулятора]
  · energy ≥ 0.6 → «наэлектризованная» (пробой зреет, ловушки живут);
  · energy ≤ 0.3 → «вязкая» (инерция, ходы вязнут);
  · иначе        → «обычная».
Веса открыты, части видны — это карта, не приказ.

🔵 механика — Пригожин/Ляпунов/Хилл/Казимир проверяемы; 🟡 порог плотности и
вес модулятора — калибровка школы; ⚫ тег — состояние среды, НЕ сигнал. 18+.
NO DUMMIES: мало данных → None и честный note. Без random.

Self-тест: python3 -m backend.tagger
"""
from __future__ import annotations

try:
    from . import bifurcation, microstructure
    from . import bif_calendar
except ImportError:                                        # запуск как скрипт
    import bifurcation, microstructure, bif_calendar  # noqa: E401

# ── пороги тега плотности (единственное место) ──────────────────────────────
E_HOT = 0.60      # energy выше — НАЭЛЕКТРИЗОВАННАЯ среда
E_COLD = 0.30     # energy ниже — ВЯЗКАЯ среда
W_BIF = 0.30      # давление слома (Пригожин/Шеффер/LPPLS)
W_LYAP = 0.25     # непредсказуемость (короткий горизонт Ляпунова)
W_TAIL = 0.15     # толстые хвосты (Хилл)
W_CASIMIR = 0.20  # сжатие спреда (Казимир)
W_MOD = 0.10      # пучность фазы-модулятора 🟡
MIN_BARS = 64

FRAME = ("🔵 Пригожин/Ляпунов/Хилл/Казимир · 🟡 порог плотности и вес "
         "модулятора — калибровка школы · ⚫ тег среды, НЕ сигнал. 18+")


def _f(x, d=0.0):
    try:
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


def tag_now(closes, *, spread_bps=None, norm_spread_bps=3.0,
            now_ts=None, sens_mult=None) -> dict | None:
    """Мгновенный тег плотности среды по ряду цен (+опц. спред/модулятор).

    closes — ряд цен (сырьё из Пылесоса или свечей); spread_bps — текущий
    спред для Казимира; sens_mult — если уже посчитан модулятор, иначе
    считается из now_ts. Нет данных → None (NO DUMMIES)."""
    bc = bifurcation.bifurcation_context(closes)
    s = bc.get("summary") or {}
    if s.get("pressure") is None:
        return None
    parts = {}
    # 1) давление слома (0..1)
    parts["bif"] = max(0.0, min(1.0, _f(s.get("pressure"))))
    # 2) непредсказуемость = 1 − предсказуемость
    pred = s.get("predictability")
    parts["lyap"] = (1.0 - max(0.0, min(1.0, _f(pred)))) if pred is not None else 0.5
    # 3) толстые хвосты (Хилл): α=2 → 1.0, α≥4 → 0.0
    alpha = s.get("tail_alpha")
    if alpha is not None:
        parts["tail"] = max(0.0, min(1.0, (4.0 - _f(alpha)) / 2.0))
    else:
        parts["tail"] = 0.0
    # 4) Казимир: сжатие спреда ∝ 1/a⁴, нормируем в 0..1 через a
    if spread_bps is not None and norm_spread_bps:
        cas = microstructure.casimir(_f(spread_bps), _f(norm_spread_bps))
        a = _f(cas.get("a"), 1.0)
        parts["casimir"] = max(0.0, min(1.0, 1.0 - a))     # a<1 сжат → к 1
        casimir_note = cas.get("word")
    else:
        parts["casimir"] = 0.0
        casimir_note = "спред не подан"
    # 5) пучность фазы-модулятора (R хора циклов) 🟡
    if sens_mult is None:
        try:
            mod = bif_calendar.phase_modulator(now_ts)
            R = _f(mod.get("R"))
        except Exception:                                   # noqa: BLE001
            R = 0.0
    else:
        R = max(0.0, min(1.0, (_f(sens_mult) - 1.0) / max(1e-9,
                bif_calendar.SENS_GAIN)))
    parts["mod"] = max(0.0, min(1.0, R))

    energy = (W_BIF * parts["bif"] + W_LYAP * parts["lyap"]
              + W_TAIL * parts["tail"] + W_CASIMIR * parts["casimir"]
              + W_MOD * parts["mod"])
    energy = max(0.0, min(1.0, energy / (W_BIF + W_LYAP + W_TAIL
                                         + W_CASIMIR + W_MOD)))
    if energy >= E_HOT:
        density = "наэлектризованная"
        word = "среда наэлектризована — пробой зреет, ловушки живут"
    elif energy <= E_COLD:
        density = "вязкая"
        word = "среда вязкая — ходы вязнут, инерция держит"
    else:
        density = "обычная"
        word = "среда в норме"
    return {"density": density, "energy": round(energy, 3),
            "parts": {k: round(v, 3) for k, v in parts.items()},
            "regime": s.get("regime"), "casimir": casimir_note,
            "word": word, "frame": FRAME}


def tag_series(ts_prices, *, win: int = 128, step: int = 8,
               spread_series=None) -> list:
    """Разметить ВЕСЬ ряд: скользящим окном win с шагом step → теги плотности
    во времени. ts_prices — [(ts, price)…]. Возврат: [{ts, density, energy}…].
    Так Пылесос-лог получает временну́ю карту «вязко/наэлектризовано»."""
    rows = [(int(t), _f(p)) for t, p in (ts_prices or []) if p]
    if len(rows) < MIN_BARS:
        return []
    out = []
    for end in range(win, len(rows) + 1, step):
        seg = rows[end - win:end]
        closes = [p for _, p in seg]
        sp = None
        if spread_series and end - 1 < len(spread_series):
            sp = spread_series[end - 1]
        tg = tag_now(closes, spread_bps=sp)
        if tg is not None:
            out.append({"ts": rows[end - 1][0], "density": tg["density"],
                        "energy": tg["energy"]})
    return out


# ── self-test: детерминированная синтетика ──────────────────────────────────
if __name__ == "__main__":
    import numpy as np
    n = 400
    i = np.arange(n, dtype=float)
    qn = (np.sin(i * 1.7183) * 0.5 + np.sin(i * 2.7183) * 0.3
          + np.sin(i * 0.9137) * 0.2)
    lm = np.empty(n); lm[0] = 0.3141
    for k in range(1, n):
        lm[k] = 4.0 * lm[k - 1] * (1.0 - lm[k - 1])

    # 1) ВЯЗКО: гладкий персистентный ход, широкий спред → низкая энергия
    calm = 100.0 * np.exp(np.cumsum(0.001 * (np.sin(i * 0.045)
                                             + 0.5 * np.sin(i * 0.013)) + 0.0008))
    t_calm = tag_now(calm, spread_bps=6.0, norm_spread_bps=3.0)
    assert t_calm and t_calm["energy"] < 0.5, t_calm

    # 2) НАЭЛЕКТРИЗОВАНО: пузырь-сингулярность + сжатый спред → высокая энергия
    dt = (n + 25 - i)
    bubble = 100.0 * np.exp(0.05 - 0.004 * dt ** 0.45
                            * (1.0 + 0.05 * np.cos(8.0 * np.log(dt))))
    t_hot = tag_now(bubble, spread_bps=0.6, norm_spread_bps=3.0)
    assert t_hot and t_hot["energy"] > t_calm["energy"], (t_hot, t_calm)

    # 3) Казимир двигает энергию: тот же ряд, сжатый спред → энергия выше
    t_wide = tag_now(bubble, spread_bps=6.0, norm_spread_bps=3.0)
    t_tight = tag_now(bubble, spread_bps=0.5, norm_spread_bps=3.0)
    assert t_tight["parts"]["casimir"] > t_wide["parts"]["casimir"]
    assert t_tight["energy"] >= t_wide["energy"]

    # 4) хаос логистики → короткий горизонт Ляпунова → lyap-часть высока
    chaos = 100.0 * np.exp(np.cumsum((lm - 0.5) * 0.01))
    t_ch = tag_now(chaos)
    assert t_ch and t_ch["parts"]["lyap"] >= 0.5, t_ch

    # 5) разметка ряда во времени: среда ГРЕЕТСЯ к сингулярности (энергия
    #    последнего окна выше первого), спред к концу сжимается
    series = [(1000 + k * 60, float(bubble[k])) for k in range(n)]
    spread_ser = list(6.0 - 5.5 * (i / (n - 1)))           # 6.0 → 0.5 бпс
    tags = tag_series(series, win=128, step=16, spread_series=spread_ser)
    assert tags and all("density" in x and "ts" in x for x in tags)
    assert tags[-1]["energy"] > tags[0]["energy"], \
        [x["energy"] for x in tags]                        # к слому — горячее
    assert tags[-1]["ts"] > tags[0]["ts"]                  # метки времени идут

    # 6) NO DUMMIES: короткий ряд → None / пустой список
    assert tag_now([1.0, 2.0, 3.0]) is None
    assert tag_series([(1, 1.0), (2, 2.0)]) == []

    # 7) детерминизм байт-в-байт
    assert tag_now(bubble, spread_bps=0.6) == tag_now(bubble, spread_bps=0.6)
    assert "НЕ сигнал" in FRAME

    print("tagger self-test OK: Физик метит среду вязкая/наэлектризованная из "
          "Пригожина+Ляпунова+Хилла+Казимира+модулятора; сжатый спред греет "
          "энергию, хаос режет горизонт; разметка ряда во времени; NO DUMMIES")
