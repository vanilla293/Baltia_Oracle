# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — ЛИМИТНАЯ ТАКТИКА (вход без хаоса, по уму).

По прямым словам владельца — как Фармила реально жмёт на кнопки:
  · лимитку ставим ТОЛЬКО когда цена РЕАЛЬНО дошла до точки (arm_limit),
    заранее висящих ордеров нет — «а так заранее не стоит»;
  · заходим по ВЫГОДЕ: лимит у уровня, либо по ближайшему если импульс убегает
    (entry_style) — «либо лимитку ставил либо по ближайшему, по уму»;
  · если цена валится — ловим ЛЮБОЙ серьёзный отскок (catch_bounce);
  · видим ПЛИТЫ (крупные стены) как зоны игры и обновляем их постоянно
    (zone_map) — «зоны плиты видел и обновлял постоянно»;
  · отличаем ПРИЗРАК (мелькнул и снят — спуфинг) от реального упора
    (wall_is_ghost) — «призрак что реально»;
  · в турбулентности играем между ближней плитой сверху и снизу (turbulence_zones).

Всё — чистая арифметика по снимкам, без сети и ордеров. Проверяемо здесь.
⚫ тактика входа, НЕ приказ и НЕ обещание прибыли. Курок за человеком. 18+.
"""
from __future__ import annotations

import statistics

ARM_DIST = 0.0015       # лимит «взводится», когда цена в пределах 0.15% от точки
FAST_MOMENTUM = 0.0020       # ход ≥0.20% за окно = импульс убегает → бери ближайший
DROP_SERIOUS = 0.0040       # падение ≥0.40% от локального пика = «валится»
BOUNCE_MIN = 0.0015       # отскок ≥0.15% от дна = серьёзный, ловим
WALL_MULT = 8.0          # заявка ≥ ×медианы объёма уровней = ПЛИТА (стена)
GHOST_PERSIST = 0.5          # стена присутствует <50% тиков = ПРИЗРАК (спуфинг)


def _f(x, d=0.0):
    try:
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


# ── взвод лимитки: только когда цена дошла до точки ─────────────────────────
def arm_limit(price: float, level: float, side: str,
              arm_dist: float = ARM_DIST) -> dict:
    """Ставить ли лимит ПРЯМО СЕЙЧАС. Заранее — нет: взводим, только когда цена
    подошла к уровню ближе arm_dist. «а так заранее не стоит»."""
    price = _f(price); level = _f(level)
    if price <= 0 or level <= 0:
        return {"arm": False, "reason": "нет цены/уровня"}
    dist = abs(price - level) / price
    armed = dist <= arm_dist
    return {"arm": bool(armed), "dist_frac": round(dist, 5), "level": level,
            "side": side,
            "reason": (f"цена в {dist*100:.3f}% от точки {level} — "
                       + ("ВЗВОДИМ лимит" if armed else "далеко, ждём подхода (не ставим заранее)"))}


# ── стиль входа: лимит по выгоде vs ближайший при импульсе ──────────────────
def entry_style(price: float, level: float, side: str, momentum_frac: float,
                spread_bps: float = 2.0, fast: float = FAST_MOMENTUM) -> dict:
    """Как заходить: если импульс убегает (|momentum|≥fast в сторону сделки) —
    берём БЛИЖАЙШИЙ (рыночный/almost), чтобы не пропустить; иначе ставим ЛИМИТ
    у выгодного уровня. «покупал по выгоде либо лимитку либо по ближайшему»."""
    price = _f(price); level = _f(level); m = _f(momentum_frac)
    toward = (side == "long" and m < 0) or (side == "short" and m > 0)  # цена идёт к нашему уровню
    running = (side == "long" and m > 0) or (side == "short" and m < 0)  # уходит без нас
    if running and abs(m) >= fast:
        return {"style": "nearest", "price": price,
                "reason": f"импульс убегает ({m*100:+.3f}%) — берём ближайший, чтобы не пропустить"}
    return {"style": "limit", "price": level,
            "reason": (f"импульс спокойный/к нам ({m*100:+.3f}%) — лимит по выгоде у {level}"
                       + (" (цена подходит)" if toward else ""))}


# ── ловля серьёзного отскока на падении ─────────────────────────────────────
def catch_bounce(prices, drop: float = DROP_SERIOUS,
                 bounce: float = BOUNCE_MIN) -> dict:
    """Цена валится — ловим ЛЮБОЙ серьёзный отскок: было падение ≥drop от
    локального пика, затем разворот ≥bounce от дна. «любой отскок серьёзный ловил»."""
    p = [_f(x) for x in (prices or []) if _f(x) > 0]
    if len(p) < 3:
        return {"bounce": False, "reason": "мало точек"}
    hi = max(p)
    i_hi = p.index(hi)
    after = p[i_hi:]
    lo = min(after)
    cur = p[-1]
    if lo <= 0:
        return {"bounce": False, "reason": "нет дна"}
    drop_f = (hi - lo) / hi
    reb_f = (cur - lo) / lo
    on = drop_f >= drop and reb_f >= bounce and cur > lo
    return {"bounce": bool(on), "high": round(hi, 6), "low": round(lo, 6),
            "cur": round(cur, 6), "drop_frac": round(drop_f, 5),
            "rebound_frac": round(reb_f, 5),
            "reason": (f"падение {drop_f*100:.2f}% от пика, отскок {reb_f*100:.2f}% от дна — "
                       + ("ЛОВИМ отскок" if on else "серьёзного отскока пока нет"))}


# ── призрак или упор: стена настоящая? ──────────────────────────────────────
def wall_is_ghost(presence_ticks, persist_min: float = GHOST_PERSIST) -> dict:
    """Стена — призрак (спуфинг: мелькнула и снята) или упор. presence_ticks —
    список bool по последним тикам: была ли стена на месте. Присутствие <50%
    тиков = ПРИЗРАК, не виснем на нём. «призрак что реально»."""
    seq = [bool(x) for x in (presence_ticks or [])]
    if not seq:
        return {"ghost": None, "persistence": None, "reason": "нет истории тиков"}
    persist = sum(seq) / len(seq)
    ghost = persist < persist_min
    return {"ghost": bool(ghost), "persistence": round(persist, 3),
            "reason": (f"стена держится {persist*100:.0f}% тиков — "
                       + ("ПРИЗРАК (спуфинг), игнорируем" if ghost else "реальный упор"))}


# ── плиты: карта крупных стен как зон игры ──────────────────────────────────
def zone_map(bids, asks, price: float = None, wall_mult: float = WALL_MULT) -> dict:
    """ПЛИТЫ — уровни с объёмом ≥ wall_mult×медианы. Возвращает ближнюю
    поддержку (bid-плита) и сопротивление (ask-плита). Пересчитывается каждый
    вызов — «обновлял постоянно»."""
    def slabs(levels):
        vols = [_f(l.get("q")) for l in (levels or []) if _f(l.get("q")) > 0]
        if len(vols) < 3:
            return []
        med = statistics.median(vols)
        out = [{"p": _f(l.get("p")), "q": _f(l.get("q"))}
               for l in levels if _f(l.get("q")) >= wall_mult * med and med > 0]
        return out
    bid_slabs = slabs(bids)
    ask_slabs = slabs(asks)
    support = max(bid_slabs, key=lambda s: s["p"], default=None)     # ближняя снизу — верхняя из bid-плит
    resistance = min(ask_slabs, key=lambda s: s["p"], default=None)  # ближняя сверху — нижняя из ask-плит
    return {"support": support, "resistance": resistance,
            "bid_slabs": bid_slabs, "ask_slabs": ask_slabs,
            "note": (f"плиты: поддержка {support['p'] if support else '—'}, "
                     f"сопротивление {resistance['p'] if resistance else '—'}")}


# ── турбулентность: играем между плитами ────────────────────────────────────
def turbulence_zones(zmap: dict, price: float) -> dict:
    """В турбулентности играем ЗОНАМИ между ближней плитой снизу (лонг от неё)
    и сверху (шорт от неё), пока плиты держатся. «если в турбулентности играл зоны»."""
    price = _f(price)
    sup = (zmap or {}).get("support")
    res = (zmap or {}).get("resistance")
    lower = sup["p"] if sup else None
    upper = res["p"] if res else None
    play = None
    if lower and upper and lower < price < upper:
        play = "range: лонг от нижней плиты, шорт от верхней, стоп за плитой"
    elif lower and price <= lower * 1.001:
        play = "у нижней плиты — отскок вверх (лонг), стоп под плитой"
    elif upper and price >= upper * 0.999:
        play = "у верхней плиты — отбой вниз (шорт), стоп над плитой"
    return {"lower_wall": lower, "upper_wall": upper, "play": play or "плит рядом нет — ждём",
            "note": "зоны обновляются каждый тик; на пробое плиты — переоценка"}


# ── self-test: чистая арифметика по снимкам ─────────────────────────────────
if __name__ == "__main__":
    # 1) взвод лимитки: близко → взводим, далеко → нет
    assert arm_limit(100.0, 99.9, "long")["arm"] is True          # 0.1% < 0.15%
    assert arm_limit(100.0, 98.0, "long")["arm"] is False         # 2% далеко
    assert "заранее" in arm_limit(100.0, 98.0, "long")["reason"]

    # 2) стиль входа: импульс убегает вверх (лонг) → ближайший; спокойный → лимит
    e_fast = entry_style(100.0, 99.9, "long", momentum_frac=0.003)   # +0.3% убегает
    assert e_fast["style"] == "nearest"
    e_calm = entry_style(100.0, 99.9, "long", momentum_frac=-0.0005)  # к нам, спокойно
    assert e_calm["style"] == "limit" and e_calm["price"] == 99.9

    # 3) отскок: падение затем разворот от дна → ловим
    b = catch_bounce([100, 101, 102, 99, 98, 99.5])
    assert b["bounce"] is True and b["low"] == 98
    #    просто падение без разворота → не ловим
    assert catch_bounce([102, 101, 100, 99, 98])["bounce"] is False

    # 4) призрак vs упор
    assert wall_is_ghost([True, False, False, False])["ghost"] is True     # 25%
    assert wall_is_ghost([True, True, True, True])["ghost"] is False       # 100%
    assert wall_is_ghost([])["ghost"] is None

    # 5) плиты: крупная стена среди мелочи
    bids = [{"p": 99.9, "q": 10}, {"p": 99.8, "q": 12}, {"p": 99.5, "q": 500},
            {"p": 99.4, "q": 11}]                                          # плита на 99.5
    asks = [{"p": 100.1, "q": 10}, {"p": 100.2, "q": 9}, {"p": 100.5, "q": 400},
            {"p": 100.6, "q": 8}]                                          # плита на 100.5
    zm = zone_map(bids, asks, price=100.0)
    assert zm["support"]["p"] == 99.5 and zm["resistance"]["p"] == 100.5, zm

    # 6) турбулентность: цена между плитами → range-игра
    tz = turbulence_zones(zm, 100.0)
    assert tz["lower_wall"] == 99.5 and tz["upper_wall"] == 100.5
    assert "range" in tz["play"]
    #    цена у нижней плиты → лонг от неё
    tz2 = turbulence_zones(zm, 99.5)
    assert "нижней плиты" in tz2["play"]

    print("limits self-test OK: лимит взводится ТОЛЬКО у точки (не заранее); "
          "вход по выгоде/ближайший по импульсу; ловит серьёзный отскок; "
          "видит плиты и играет зоны; отличает призрак от упора")
