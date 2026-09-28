# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — ДИРЕКТИВА АНАЛИТИКА (мозг, курок у человека).

Сводит уже готовые сигналы в ОДИН честный вердикт направления на ближайший
час: куда смотрит среда, где зона входа, где стоп, что отменяет идею.
Это АНАЛИЗ, а НЕ автоторговля: бот НЕ выставляет ордера — решение и курок
за человеком (по воле владельца: «Аналитик + пульт, курок твой»).

Композиция (всё уже считается в проекте, тут только сведение):
  · maya.analyze(стакан)  — ЖИВАЯ тяга: куда всасывает цену прямо сейчас
    (сильнейший вход, реальный стакан Tinkoff); + консенсус/агрессор.
  · reactor_sky.reactor_context — макро-наклон: окно Пригожина (когда среда
    готова порваться), Курамото-когерентность, Доплер (дыхание).
  · волна Ψ (aether) — наклон эфирной волны актива (опц.).

Логика вердикта — детерминированная (никакого random): каждый источник даёт
голос со своим весом, живой стакан весит больше «неба». Режим (тренд/флет/
бифуркация) задаёт, насколько агрессивно читать сигнал. Рамка ⚫ жёсткая:
НЕ торговый сигнал, рынок не предсказуем, 18+.
"""
from __future__ import annotations

# веса голосов (живой стакан > небо): сумма нормируется
W_MAYA = 0.55        # тяга живого стакана — главный голос
W_WAVE = 0.20        # наклон волны Ψ актива
W_REACTOR = 0.25        # макро-наклон неба (Доплер/Курамото)

CONF_STRONG = 0.6         # порог «уверенного» вердикта
FLAT_BAND = 0.15        # |счёт| ниже — FLAT (нет перевеса)


def _sign_maya(m: dict) -> float:
    """Голос живого стакана: −1 вниз … +1 вверх, взвешен согласием ленты."""
    if not (isinstance(m, dict) and m.get("available")):
        return 0.0
    pull = (m.get("pull") or {})
    side = pull.get("side")
    if side not in ("вверх", "вниз"):
        return 0.0
    base = 1.0 if side == "вверх" else -1.0
    # усиление, если агрессор ленты согласен с тягой
    ag = m.get("aggressor") or {}
    if ag.get("with_pull") is True:
        base *= 1.0
    elif ag.get("with_pull") is False:
        base *= 0.5                    # лента против тяги — ослабляем
    # глубина ближнего вакуума добавляет уверенности
    depth = 0.0
    for k in ("vacuum_up", "vacuum_down"):
        v = m.get(k) or {}
        if (side == "вверх" and k == "vacuum_up") or (side == "вниз" and k == "vacuum_down"):
            depth = float(v.get("depth") or 0.0)
    return max(-1.0, min(1.0, base * (0.5 + 0.5 * depth)))


def _sign_wave(wave_slope) -> float:
    """Наклон волны Ψ: >0 растёт (вверх), <0 падает. Клампим в [−1,1]."""
    try:
        s = float(wave_slope)
    except (TypeError, ValueError):
        return 0.0
    return max(-1.0, min(1.0, s * 3.0))      # 🟡 масштаб школы (не насыщать мгновенно)


def _sign_reactor(rc: dict) -> tuple[float, dict]:
    """Макро-наклон неба из reactor_context + пометки режима."""
    if not isinstance(rc, dict) or rc.get("mode") != "precise":
        return 0.0, {}
    tilt = 0.0
    # Доплер: дыхание карты (атака/всасывание) как грубый наклон
    bodies = rc.get("bodies") or []
    ins = sum(1 for b in bodies if (b or {}).get("doppler", "").startswith("атака"))
    outs = sum(1 for b in bodies if (b or {}).get("doppler", "").startswith("всас"))
    if ins + outs:
        tilt += 0.4 * (ins - outs) / (ins + outs)
    kur = rc.get("kuramoto") or {}
    r = float(kur.get("r") or 0.0)
    regime = {"prigogine": rc.get("prigogine"), "kuramoto_r": r,
              "catalyst": kur.get("catalyst")}
    return max(-1.0, min(1.0, tilt)), regime


def directive(maya_now: dict | None, reactor_ctx: dict | None = None,
              wave_slope: float | None = None) -> dict:
    """Итоговый вердикт направления. Всё в защите — недостающий источник
    просто не голосует (NO DUMMIES), вердикт честно слабеет."""
    vm = _sign_maya(maya_now or {})
    vw = _sign_wave(wave_slope)
    vr, regime = _sign_reactor(reactor_ctx or {})
    have = []
    num = 0.0
    den = 0.0
    if maya_now and (maya_now.get("available")):
        num += W_MAYA * vm; den += W_MAYA; have.append("стакан")
    if wave_slope is not None:
        num += W_WAVE * vw; den += W_WAVE; have.append("волна")
    if reactor_ctx and reactor_ctx.get("mode") == "precise":
        num += W_REACTOR * vr; den += W_REACTOR; have.append("небо")
    score = (num / den) if den > 0 else 0.0

    has_book = "стакан" in have
    if abs(score) < FLAT_BAND:
        dr = "flat"
    else:
        dr = "long" if score > 0 else "short"
    conf = min(1.0, abs(score) / max(CONF_STRONG, 1e-9))
    # ГЛАВНЫЙ голос — живой стакан. Нет его (биржа закрыта / нет токена) →
    # это лишь слабый макро-наклон неба, уверенность жёстко режется, чтобы
    # ночная волна не выдавала «ЛОНГ 93%». Честность > красивого числа.
    if not has_book:
        conf = min(conf, 0.30)

    # режим: открытое окно Пригожина = высокая готовность среды рваться
    pr = (regime or {}).get("prigogine") or {}
    biff = bool(pr.get("crossing"))
    r = (regime or {}).get("kuramoto_r") or 0.0
    if biff:
        reg = "бифуркация — среда готова к слому, читаем сигнал агрессивнее"
    elif r >= 0.7:
        reg = "когерентный тренд (Курамото r высокий) — движения продолжаются"
    else:
        reg = "флет/шум — только чистые сетапы, мелочь игнорируем"

    # зоны действия из живого стакана (курок жмёт человек)
    entry_note = stop_note = invalid = "—"
    if isinstance(maya_now, dict) and maya_now.get("available"):
        vu, vd = maya_now.get("vacuum_up") or {}, maya_now.get("vacuum_down") or {}
        wa, wb = maya_now.get("wall_ask") or {}, maya_now.get("wall_bid") or {}
        if dr == "long":
            entry_note = f"вход у поддержки/на ложном проколе вниз (вакуум под ценой {vd.get('p_from','?')})"
            stop_note = f"стоп под стеной bid {wb.get('p','?')}"
            invalid = "идея отменена, если стена bid оказалась призраком и агрессор льёт вниз"
        elif dr == "short":
            entry_note = f"вход у сопротивления/на ложном выносе вверх (вакуум над ценой {vu.get('p_from','?')})"
            stop_note = f"стоп над стеной ask {wa.get('p','?')}"
            invalid = "идея отменена, если стена ask призрак и агрессор льёт вверх"
        else:
            entry_note = "нет перевеса — вне рынка, ждём чистый сетап"

    dir_word = {"long": "ЛОНГ (вверх)", "short": "ШОРТ (вниз)", "flat": "ВНЕ РЫНКА"}[dr]
    return {
        "dir": dr, "dir_word": dir_word,
        "score": round(score, 3), "confidence": round(conf, 2),
        "strong": conf >= 1.0,
        "regime": reg, "bifurcation": biff,
        "sources": have,
        "votes": {"стакан": round(vm, 2), "волна": round(vw, 2), "небо": round(vr, 2)},
        "entry": entry_note, "stop": stop_note, "invalidation": invalid,
        "reason": (f"вердикт {dir_word} (уверенность {round(conf*100)}%): "
                   f"голоса — стакан {round(vm,2)}, волна {round(vw,2)}, небо {round(vr,2)}; "
                   f"режим: {reg}"),
        "frame": ("⚫ ДИРЕКТИВА, а НЕ приказ: бот НЕ торгует сам, курок за тобой. "
                  "Это карта давлений, рынок не предсказуем. НЕ инвест-совет · 18+"),
    }


# ── self-test: чистая композиция, сеть не нужна ─────────────────────────────
if __name__ == "__main__":
    # 1) стакан тянет вверх + агрессор согласен + небо в атаке → ЛОНГ
    maya_up = {"available": True,
               "pull": {"side": "вверх"},
               "aggressor": {"with_pull": True},
               "vacuum_up": {"depth": 0.9, "p_from": 101.1},
               "vacuum_down": {"depth": 0.2, "p_from": 98.0},
               "wall_bid": {"p": 99.5}, "wall_ask": {"p": 102.0}}
    rc_up = {"mode": "precise",
             "bodies": [{"doppler": "атака (идёт к нам)"}, {"doppler": "атака"}],
             "kuramoto": {"r": 0.8, "catalyst": "Плутон"},
             "prigogine": {"crossing": True, "date": "2026-08-06"}}
    d = directive(maya_up, rc_up, wave_slope=0.05)
    assert d["dir"] == "long" and d["confidence"] > 0.5, d
    assert d["bifurcation"] is True and "бифуркация" in d["regime"]
    assert "стоп под стеной bid 99.5" in d["stop"]

    # 2) стакан тянет вниз, лента ПРОТИВ тяги → сигнал слабее, но SHORT
    maya_dn = {"available": True, "pull": {"side": "вниз"},
               "aggressor": {"with_pull": False},
               "vacuum_down": {"depth": 0.8, "p_from": 98.0},
               "wall_ask": {"p": 102.0}}
    d2 = directive(maya_dn, None, None)
    assert d2["dir"] == "short" and "стакан" in d2["sources"] and "небо" not in d2["sources"]

    # 3) симметрия/нет данных → FLAT честно
    d3 = directive({"available": True, "pull": {"side": None}}, None, None)
    assert d3["dir"] == "flat" and d3["confidence"] == 0.0

    # 4) вообще нет источников → FLAT, никаких выдумок
    d4 = directive(None, None, None)
    assert d4["dir"] == "flat" and d4["sources"] == []

    # 5) рамка ⚫ и «курок за тобой» всегда на месте
    assert "курок за тобой" in d["frame"] and "18+" in d["frame"]

    # 6) БЕЗ живого стакана уверенность жёстко режется (ночная волна ≠ 93%)
    d_night = directive(None, rc_up, wave_slope=0.5)     # только небо+волна
    assert "стакан" not in d_night["sources"]
    assert d_night["confidence"] <= 0.30, d_night        # не раздувается

    print("directive self-test OK: голоса сведены (стакан>небо), режим по "
          "Пригожину/Курамото, зоны входа-стопа из живого стакана, курок у человека")
