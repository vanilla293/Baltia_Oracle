# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — ЛЕСТНИЦА ПЛЕЧА (размер от убеждённости, не от привычки).

Аудит (веер 06.08.2026), причина №9 слабости: «размер позиции не зависит от
силы сигнала — фолбэк-входы жёстко 1 лот; confidence, score, net_edge
вычисляются и выбрасываются». Жалоба владельца «на макс плечи не заходит» —
прямое следствие. Этот модуль лечит именно это.

Идея — ЛЕСТНИЦА: доля депозита в обеспечении растёт со СВОДНОЙ убеждённостью
Оракула и предсказуемостью среды, до МАКСИМУМА в подтверждённом окне
бифуркации; и падает до нуля в хаосе. Математика ставки — дробный Келли:

    f*(Келли) = p − (1−p)/RR       (p — вероятность, RR — прибыль/риск)

Полный Келли на реальном рынке самоубийствен (оценка p шумит), поэтому
ставится ДОЛЯ Келли (KELLY_FRAC), а лестница режется:
  · губернатором просадки (после серии потерь ставка сжимается — анти-мартингейл;
    «отыгрыша» не существует);
  · killswitch SessionRisk (−6%/день, серия убытков) — НЕ отключаем никогда;
  · хаос/инфаркт спреда → 0.

⚫ Лестница считает ДАВЛЕНИЕ НА ДЕПОЗИТ и честно показывает дистанцию до
ликвидации; макс плечо в окне — осознанное право владельца (заказ: «на всю,
хардкор»), НЕ рекомендация и НЕ обещание прибыли. Плечо 18+.

Self-тест: python3 -m backend.leverage
"""
from __future__ import annotations


# ── константы лестницы (единственное место) ─────────────────────────────────
BASE_FRAC = 0.25       # ступень покоя: столько ГО от депозита без сигнала
MAX_FRAC = 1.00        # потолок лестницы (весь депозит в ГО — макс плечо)
WINDOW_FLOOR = 0.90    # окно бифуркации + сильный Оракул → не ниже этой доли
KELLY_FRAC = 0.5       # доля Келли (полный Келли шумит — берём половину)
CONF_FULL = 0.75       # убеждённость, при которой лестница выходит на потолок
PRED_FLOOR = 0.30      # предсказуемость ниже — лестница режется вдвое
DD_GOV_K = 2.0         # губернатор: множитель = 1 − K·просадка (не ниже MIN)
DD_GOV_MIN = 0.30      # ниже этой доли губернатор не жмёт (капитал уже мал)
# ── АППАРАТНЫЕ ГУБЕРНАТОРЫ (спринт «Хищник», прокол №2) ────────────────────
# Приказ владельца: «объём должен зависеть от экспоненты Ляпунова: горизонт
# видимости падает = объём режется аппаратно, независимо от уверенности
# Оракула»; «Келли в мире Мандельброта — математический суицид».
HORIZON_MIN = 8.0      # баров: горизонт Ляпунова ниже — режем пропорционально
HORIZON_CUT_MIN = 0.10  # даже в полном хаосе оставляем не более 10% ступени
TAIL_ALPHA_KELLY = 3.0  # Хилл α ниже — Келли ОТКЛЮЧЁН (его посылки мертвы)
TAIL_CAP_LO, TAIL_CAP_HI = 0.20, 1.00   # кэп доли по α: α=2→0.2 … α=4→1.0

FRAME = ("⚫ лестница считает давление на депозит и дистанцию до ликвидации; "
         "макс плечо в окне — право владельца, НЕ рекомендация. Тормоза: "
         "killswitch −6%/день и серия убытков — не отключаются. 18+")


def _f(x, d=0.0):
    try:
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


def kelly_fraction(p: float, rr: float) -> float:
    """Классический Келли: f* = p − (1−p)/RR, обрезан в [0, 1]."""
    p = max(0.0, min(1.0, _f(p)))
    rr = max(1e-9, _f(rr, 1.0))
    return max(0.0, min(1.0, p - (1.0 - p) / rr))


def drawdown_frac(pnls, deposit: float) -> float:
    """Текущая просадка от пика кривой капитала по списку P/L сделок (журнал
    trades.jsonl — аудит: «пишется и не читается» — теперь и сюда читается)."""
    dep = max(1e-9, _f(deposit))
    eq = peak = 0.0
    for p in pnls or []:
        eq += _f(p)
        peak = max(peak, eq)
    return max(0.0, min(1.0, (peak - eq) / dep))


def tail_cap(alpha: float) -> float:
    """Кэп доли ГО по индексу хвоста Хилла: α=2 (бесконечная дисперсия) →
    0.2; α≥4 (почти гаусс) → 1.0. Линейно между. Это tail-risk parity
    вместо гауссовой самоуверенности."""
    a = _f(alpha)
    return max(TAIL_CAP_LO, min(TAIL_CAP_HI, (a - 1.5) / 2.5))


def ladder(confidence: float, predictability: float, *,
           window_open: bool = False, chaos: bool = False,
           locked: bool = False, dd: float = 0.0,
           p_dir: float | None = None, rr: float | None = None,
           lyap_horizon: float | None = None,
           tail_alpha: float | None = None) -> dict:
    """Доля депозита в ГО [0..1] от состояния Оракула.

    confidence    — |2P−1| Оракула (0..1);
    predictability— 0..1 (двигатель бифуркаций + сцепка);
    window_open   — окно бифуркации (детекторы совпали) → право на максимум;
    chaos         — инфаркт спреда/risk_off → 0 всегда;
    locked        — killswitch сработал → 0 всегда;
    dd            — текущая просадка дня (0..1) → губернатор;
    p_dir, rr     — дробный Келли сверху, но ТОЛЬКО в тонких хвостах
                    (α≥3): в мире Мандельброта посылки Келли мертвы —
                    он отключается, правит tail-кэп (прокол №2);
    lyap_horizon  — горизонт предсказуемости 1/λ в барах: ниже HORIZON_MIN —
                    объём режется АППАРАТНО, невзирая на уверенность;
    tail_alpha    — индекс хвоста Хилла: кэп доли по тяжести хвостов.
    """
    conf = max(0.0, min(1.0, _f(confidence)))
    pred = max(0.0, min(1.0, _f(predictability)))
    if locked:
        return {"frac": 0.0, "rung": "KILLSWITCH", "note":
                "killswitch сработал — торговля заблокирована до сброса", "frame": FRAME}
    if chaos:
        return {"frac": 0.0, "rung": "ХАОС", "note":
                "инфаркт спреда/risk_off — вне рынка", "frame": FRAME}

    # основная ступень: от базы к потолку по убеждённости
    t = min(1.0, conf / CONF_FULL)
    frac = BASE_FRAC + (MAX_FRAC - BASE_FRAC) * t
    rung = "разгон по убеждённости"

    # предсказуемость среды: непредсказуемо → полступени
    if pred < PRED_FLOOR:
        frac *= 0.5
        rung = "непредсказуемая среда — полступени"

    # окно бифуркации: детекторы совпали и Оракул уверен → пол не ниже 0.9
    if window_open and conf >= 0.5 and pred >= PRED_FLOOR:
        frac = max(frac, WINDOW_FLOOR)
        rung = "ОКНО БИФУРКАЦИИ — максимум лестницы"

    # дробный Келли сверху — ТОЛЬКО в тонких хвостах (α≥3 или α неизвестна):
    # формула требует конечной дисперсии; в зоне Мандельброта она лжёт
    kelly = None
    fat = tail_alpha is not None and _f(tail_alpha) < TAIL_ALPHA_KELLY
    if p_dir is not None and rr is not None and not fat:
        kelly = kelly_fraction(p_dir, rr) * KELLY_FRAC / 0.5
        kelly = min(1.0, kelly * 2.0)
        if kelly < frac:
            frac = kelly
            rung = f"Келли режет ступень (f½={kelly:.2f})"
    elif fat and p_dir is not None:
        rung += " · Келли ОТКЛЮЧЁН (толстые хвосты — его посылки мертвы)"

    # ── АППАРАТНЫЕ ГУБЕРНАТОРЫ: режут ПОСЛЕ пола окна, невзирая на всё ──
    if lyap_horizon is not None and _f(lyap_horizon) < HORIZON_MIN:
        cut = max(HORIZON_CUT_MIN, _f(lyap_horizon) / HORIZON_MIN)
        frac *= cut
        rung += (f" · ГУБЕРНАТОР ЛЯПУНОВА ×{cut:.2f} "
                 f"(горизонт {_f(lyap_horizon):.1f}<{HORIZON_MIN:.0f} баров)")
    if fat:
        cap = tail_cap(tail_alpha)
        if frac > cap:
            frac = cap
            rung += f" · TAIL-КЭП {cap:.2f} (Хилл α={_f(tail_alpha):.2f})"

    # губернатор просадки: анти-мартингейл
    gov = max(DD_GOV_MIN, 1.0 - DD_GOV_K * max(0.0, min(1.0, _f(dd))))
    if gov < 1.0:
        frac *= gov
        rung += f" · губернатор просадки ×{gov:.2f}"

    frac = max(0.0, min(MAX_FRAC, frac))
    return {"frac": round(frac, 3), "rung": rung,
            "kelly": (round(kelly, 3) if kelly is not None else None),
            "governor": round(gov, 3),
            "tail_capped": bool(fat), "note":
            (f"доля ГО {frac*100:.0f}% депозита: убеждённость {conf:.2f}, "
             f"предсказуемость {pred:.2f}"
             + (", ОКНО открыто" if window_open else "")),
            "frame": FRAME}


def lots_from(deposit: float, frac: float, go_per_lot: float) -> int:
    """Доля → лоты. Пол 1 лот, если доля > 0 и депозит тянет хотя бы один
    (заказ владельца: «набираем до макса, пока не даст»)."""
    dep, go = _f(deposit), max(1e-9, _f(go_per_lot))
    fr = max(0.0, min(1.0, _f(frac)))
    lots = int(dep * fr / go)
    if lots < 1 and fr > 0 and dep >= go:
        lots = 1
    return max(0, lots)


# ── self-test ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # 1) Келли: p=0.6, RR=1.5 → f*=0.6−0.4/1.5=0.333; p=0.5,RR=1 → 0
    assert abs(kelly_fraction(0.6, 1.5) - (0.6 - 0.4 / 1.5)) < 1e-9
    assert kelly_fraction(0.5, 1.0) == 0.0
    assert kelly_fraction(0.9, 2.0) > kelly_fraction(0.6, 2.0)   # монотонно по p

    # 2) лестница монотонна по убеждённости; хаос и killswitch — нули всегда
    lo = ladder(0.2, 0.6)
    hi = ladder(0.8, 0.6)
    assert lo["frac"] < hi["frac"], (lo, hi)
    assert hi["frac"] == MAX_FRAC                     # 0.8 ≥ CONF_FULL → потолок
    assert ladder(0.9, 0.9, chaos=True)["frac"] == 0.0
    assert ladder(0.9, 0.9, locked=True)["frac"] == 0.0
    assert ladder(0.9, 0.9, locked=True)["rung"] == "KILLSWITCH"

    # 3) окно бифуркации поднимает слабую ступень до пола 0.9
    w = ladder(0.55, 0.5, window_open=True)
    assert w["frac"] >= WINDOW_FLOOR, w
    assert "ОКНО" in w["rung"]
    #    но не при непредсказуемой среде
    wn = ladder(0.55, 0.1, window_open=True)
    assert wn["frac"] < WINDOW_FLOOR, wn

    # 4) непредсказуемость режет ступень вдвое
    a = ladder(0.6, 0.6); b = ladder(0.6, 0.1)
    assert b["frac"] < a["frac"]

    # 5) губернатор просадки: −20% дня → множитель 0.6
    g = ladder(0.8, 0.6, dd=0.2)
    assert g["frac"] < hi["frac"] and abs(g["governor"] - 0.6) < 1e-9

    # 6) Келли сверху: уверенный вход со слабым RR режется
    k = ladder(0.8, 0.6, p_dir=0.55, rr=1.0)
    assert k["frac"] < hi["frac"], k

    # 7) просадка по журналу: +100, −50, −80 → пик 100, капитал −30 → 1.3%/10к
    dd = drawdown_frac([100, -50, -80], 10000)
    assert abs(dd - 0.013) < 1e-9, dd
    assert drawdown_frac([], 10000) == 0.0

    # 8) лоты: 8000₽, доля 1.0, ГО 1200 → 6; доля 0.1 → пол 1 лот; 0 → 0
    assert lots_from(8000, 1.0, 1200) == 6
    assert lots_from(8000, 0.1, 1200) == 1
    assert lots_from(8000, 0.0, 1200) == 0
    assert lots_from(800, 0.5, 1200) == 0             # депозит не тянет лота

    # 9) АППАРАТНЫЙ ГУБЕРНАТОР ЛЯПУНОВА (прокол №2): горизонт 2 бара режет
    #    даже уверенное окно, невзирая на убеждённость
    g_ly = ladder(0.9, 0.8, window_open=True, lyap_horizon=2.0)
    full = ladder(0.9, 0.8, window_open=True)
    assert g_ly["frac"] <= full["frac"] * (2.0 / HORIZON_MIN) + 1e-9, (g_ly, full)
    assert "ЛЯПУНОВА" in g_ly["rung"]
    #    горизонт длинный → не режет
    assert ladder(0.9, 0.8, lyap_horizon=50.0)["frac"] == full["frac"]

    # 10) TAIL-КЭП (мир Мандельброта): α=2 → доля ≤ 0.2 даже в окне с conf 0.9
    g_t = ladder(0.9, 0.8, window_open=True, tail_alpha=2.0)
    assert g_t["frac"] <= TAIL_CAP_LO + 1e-9 and g_t["tail_capped"], g_t
    assert abs(tail_cap(2.0) - 0.2) < 1e-9 and abs(tail_cap(4.0) - 1.0) < 1e-9
    #    α=3.5 (хвосты умеренные) → кэп не мешает потолку... α≥3: Келли жив
    assert ladder(0.9, 0.8, tail_alpha=4.0)["frac"] == 1.0

    # 11) Келли ОТКЛЮЧЁН в толстых хвостах: сильный RR не спасает его посылки
    k_fat = ladder(0.8, 0.6, p_dir=0.9, rr=3.0, tail_alpha=2.2)
    assert k_fat["kelly"] is None and "ОТКЛЮЧЁН" in k_fat["rung"], k_fat
    k_thin = ladder(0.8, 0.6, p_dir=0.9, rr=3.0, tail_alpha=4.0)
    assert k_thin["kelly"] is not None

    # 12) рамка ⚫ на месте
    assert "18+" in hi["frame"] and "killswitch" in hi["frame"]

    print("leverage self-test OK: лестница монотонна, окно даёт право на "
          "максимум, хаос/killswitch — ноль, губернатор Ляпунова режет "
          "аппаратно, tail-кэп Хилла правит в толстых хвостах, Келли жив "
          "только в тонких, губернатор просадки жмёт")
