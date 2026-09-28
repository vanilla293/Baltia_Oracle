# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — СВОД РЕШЕНИЙ (единый мозг Фармилы).

Одна структура, куда стекается ВСЁ, и откуда выходит ОДНО решение под любой
сюжет. Не набор сигналов рядом, а свод: каждый фактор голосует, свод выбирает
стратегию под ситуацию, режет вход под комиссию и ведёт позицию по-разному
в зависимости от того, вне рынка мы или уже влетели и «отлетели».

Что учитывается ВОЕДИНО (любой источник опционален — NO DUMMIES, нет → не голосует):
  · 🔵 глубокое небо (reactor: окно Пригожина, Курамото r) — МАКРО-режим/наклон;
  · 🔵 микроструктура (рентген: кто в стакане — ММ/Кит/ловушка/инфаркт) — МИКРО;
  · 🔵 директива (стакан+небо+волна → long/short/flat) — сведённый вектор;
  · 🟡 комиссия 0.02%/сторона (0.04% за круг) — ход должен перекрыть круг, иначе ждём;
  · размер — адаптивный (25% база, скачок→макс, хаос→вне) из trader_risk;
  · состояние позиции — вне рынка / в позиции / «отлетели» против нас.

СТРАТЕГИИ (свод сам выбирает под «кто в стакане»):
  Казимир-прострел · За-Китом · Фейд-стоп-ханта · Возврат-к-VWAP · Против-иллюзии
  · Наблюдение (по умолчанию — когда чистого сетапа нет).

Устойчивость: любой сбой входа → безопасное «ждать», а не случайное действие.
⚫ Свод — карта давления и план, НЕ приказ и НЕ обещание прибыли. Курок и
ответственность за человеком. Плечо 18+. Рынок не предсказуем.
"""
from __future__ import annotations

try:
    from . import trader_risk
except ImportError:                                        # запуск как скрипт
    import trader_risk
try:
    from . import leverage as _leverage
except ImportError:                                        # запуск как скрипт
    try:
        import leverage as _leverage
    except ImportError:
        _leverage = None

# комиссия и порог полезности хода
# Владелец уточнил: ИТОГО 0.04% за ОБА (покупка+продажа) → 0.02% за сторону.
COMMISSION_SIDE = 0.00025       # 0.025%/сторона = 0.05% круг (уточнение владельца)
ADVERSE_WAIT = 0.004            # «отлетели» ≥0.4% против — включается режим наблюдения


def _f(x, d=0.0):
    try:
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


# ── комиссионный фильтр ПО МАСШТАБУ игры ────────────────────────────────────
def commission_gate(expected_move_frac: float,
                    commission_side: float = COMMISSION_SIDE) -> dict:
    """Стоит ли входить — с учётом МАСШТАБА хода (слова владельца: «на микро
    0.10–0.30% это одно, на больших другое»). Комиссия round-trip 0.04%
    (0.02%/сторона, уточнение владельца) фиксирована, но её ВЕС разный:
      · микро (<0.15%): комиссия съедает почти всё → нужен запас ×2.5 + проскальз.;
      · обычный (0.15–0.5%): запас ×1.5;
      · крупный (>0.5%): комиссия ничтожна → достаточно ×1.1 (просто перекрыть).
    «Играем пока не повернётся» — цель не фиксированная, exp — ОЦЕНКА хода до
    разворота (ATR/до плиты)."""
    rt = 2.0 * commission_side                             # round-trip, напр. 0.0008
    exp = abs(_f(expected_move_frac))
    if exp < 0.0015:
        tier, min_rr = "микро", 2.5
    elif exp < 0.005:
        tier, min_rr = "обычный", 1.5
    else:
        tier, min_rr = "крупный", 1.1
    need = rt * min_rr
    net = exp - rt                                         # чистый край после комиссии
    ok = exp >= need
    return {"ok": bool(ok), "tier": tier, "round_trip": rt,
            "need_move": round(need, 5), "expected": round(exp, 5),
            "net_edge": round(net, 5),
            "note": (f"[{tier}] ход {exp*100:.3f}% "
                     + ("перекрывает" if ok else "НЕ перекрывает")
                     + f" комиссию {rt*100:.2f}% за круг с запасом (нужно ≥{need*100:.3f}%); "
                     + f"чистый край {net*100:+.3f}%")}


# ── защита от ЛОЖНОЙ остановки: ДВА ФАКТОРА (тенденция + Хёрст) ─────────────
def stop_guard(side: str, price: float, stop: float, xray: dict,
               trend: str = "flat",
               base_extra: float = 0.0010, cap_extra: float = 0.0050) -> dict:
    """Защита от ложного выбивания стопа — допуск НЕ фиксированный, а от ДВУХ
    ФАКТОРОВ (слова владельца: «мелкий буфер пережуют и не заметят; по
    тенденции; Херст — два фактора; макс слив только если 0.5%»):

      · ТЕНДЕНЦИЯ: позиция ПО тренду (long в росте / short в падении) → прокол
        против — почти наверняка вынос слабаков, допуск РАСШИРЯЕТСЯ (+0.20%);
        позиция ПРОТИВ тренда → допуск минимальный, из-под тренда не геройствуем;
      · ХЁРСТ: H<0.5 (возвратный рынок) → клевки синтетические, допуск +0.20%;
        H>0.5 (трендовый пробой) → пробой скорее реален, допуска не добавляем.

    Итог: base 0.10% + 0.20% (по тренду) + 0.20% (H<0.5) = МАКСИМУМ 0.50% —
    только там признаём слив. CVD ПРОТИВ нас (реальные деньги бьют в пробой)
    срезает допуск вдвое. Глубже допуска — реальный пробой, выходим всегда.

    Возврат: hold_through / exit / ok (не у стопа)."""
    price = _f(price); stop = _f(stop)
    m = (xray or {}).get("metrics") or {}
    actor = (xray or {}).get("actor") or ""
    hurst = _f(m.get("hurst"), 0.5)
    cvd = _f(m.get("cvd"))
    if price <= 0 or stop <= 0:
        return {"decision": "ok", "reason": "нет цены/стопа"}

    breached = (side == "long" and price <= stop) or (side == "short" and price >= stop)
    if not breached:
        return {"decision": "ok", "reason": "цена не у стопа"}

    extra = (abs(price - stop) / stop) if stop else 1.0     # глубина за стопом

    # фактор 1: тенденция
    with_trend = ((side == "long" and trend == "up") or
                  (side == "short" and trend == "down"))
    # фактор 2: Хёрст
    mean_revert = hurst < 0.5
    stop_hunt = "ложный клевок" in actor
    cvd_favors = (side == "long" and cvd >= 0) or (side == "short" and cvd <= 0)

    tol = base_extra
    if with_trend:
        tol += 0.0020
    if mean_revert:
        tol += 0.0020
    if not cvd_favors:
        tol *= 0.5                                          # лента бьёт в пробой
    tol = min(tol, cap_extra)                               # потолок 0.5% всегда

    looks_false = with_trend or mean_revert or stop_hunt
    if looks_false and extra <= tol:
        return {"decision": "hold_through", "extra_frac": round(extra, 5),
                "tol_frac": round(tol, 5),
                "reason": (f"ложная остановка: допуск {tol*100:.2f}% "
                           f"(тенденция {'ЗА' if with_trend else 'нет'}, "
                           f"Хёрст {hurst:.2f}{'<0.5' if mean_revert else ''}, "
                           f"CVD {cvd:+.0f}), глубина {extra*100:.3f}% ≤ допуска — "
                           "держим сквозь прокол, буфер ММ не пережуёт")}
    return {"decision": "exit", "extra_frac": round(extra, 5),
            "tol_frac": round(tol, 5),
            "reason": (f"реальный пробой: глубина {extra*100:.3f}% > допуска "
                       f"{tol*100:.2f}% (тенденция {'за нас' if with_trend else 'против'}, "
                       f"Хёрст {hurst:.2f}, CVD {cvd:+.0f}) — выходим, капитал важнее")}


# ── выбор стратегии под «кто в стакане» ─────────────────────────────────────
def pick_strategy(actor: str, dir_hint: str, metrics: dict) -> dict:
    """Свод сам выбирает стратегию под ситуацию. side — куда стратегия целит
    (long/short/none), plan — как входить/где стоп/цель на языке школы."""
    a = actor or ""
    m = metrics or {}
    cvd = _f(m.get("cvd")); obi = _f(m.get("obi"))
    cvd_side = "long" if cvd > 0 else ("short" if cvd < 0 else dir_hint)

    if "расчищает трубу" in a:                              # Казимир+отмены = прострел
        return {"name": "Казимир-прострел", "side": (dir_hint or cvd_side),
                "plan": "вход в сторону вакуума ДО первого тика цены; стоп за "
                        "сжатой пружиной; цель — следующая стена стакана"}
    if "КИТ грузит" in a:                                   # набор айсберга
        side = "long" if obi >= 0 else "short"
        return {"name": "За-Китом", "side": side,
                "plan": "вход в сторону впитывания ПОСЛЕ release айсберга; стоп "
                        "под уровнем айсберга; цель — импульс освобождённой цены"}
    if "ложный клевок" in a:                                # Stop-Hunt
        # клевок ПРОТИВ толпы: входим ПРОТИВ прокола (лови вынос слабаков)
        side = "long" if dir_hint != "short" else "short"
        return {"name": "Фейд-стоп-ханта", "side": side,
                "plan": "не выходить на проколе; лимитки на дно ложного выноса — "
                        "ММ сам нальёт по лучшей цене; стоп за экстремумом прокола"}
    if "истощение у" in a:                                  # смерть агрессора у VWAP
        side = "short" if _f(m.get("vwap_dev")) > 0 else "long"
        return {"name": "Возврат-к-VWAP", "side": side,
                "plan": "резина тянет к центру: вход против растяжки к VWAP; "
                        "стоп за экстремумом растяжки; цель — VWAP"}
    if "иллюзию" in a:                                      # OBI≠CVD
        return {"name": "Против-иллюзии", "side": cvd_side,
                "plan": "стакан пугает рисунком, реальные деньги (CVD) в другую "
                        "сторону — вход по CVD; стоп за нарисованной стеной"}
    return {"name": "Наблюдение", "side": "none",
            "plan": "чистого сетапа нет — вне рынка, ждём, когда стакан назовёт "
                    "себя (Кит/ММ/прострел)"}


# ── ведение ОТКРЫТОЙ позиции (сюжет «отлетели») ─────────────────────────────
def manage_position(position: dict, macro_dir: str, xray: dict,
                    commission_side: float = COMMISSION_SIDE) -> dict:
    """Мы уже в позиции. Главный сюжет владельца: «виден переворот, но общая
    ВНИЗ, мы отлетели — ждём, смотрим по ситуации». Не паниковать, не
    переворачиваться раньше подтверждения, не доливать в убыток вслепую."""
    side = position.get("side")                            # 'long' | 'short'
    adverse = _f(position.get("adverse_frac"))             # доля хода ПРОТИВ нас (>0)
    a = (xray or {}).get("actor") or ""
    m = (xray or {}).get("metrics") or {}
    cvd = _f(m.get("cvd"))

    # 1) хаос — выходим/паника вне зависимости от направления
    if "ИНФАРКТ" in a:
        return {"action": "exit", "reason": "инфаркт спреда — рынок разорван, "
                "закрываем/безубыток, не ждём", "manage": True}

    against = ((side == "long" and macro_dir == "down") or
               (side == "short" and macro_dir == "up"))
    # разворот в НАШУ пользу подтверждён лентой? (CVD развернулся к нам)
    fav_confirmed = ((side == "long" and cvd > 0) or (side == "short" and cvd < 0))
    reversal_seen = ("ложный клевок" in a) or ("истощение у" in a) or ("КИТ грузит" in a)

    # 2) СЮЖЕТ ВЛАДЕЛЬЦА: отлетели против общей — режим наблюдения
    if against and adverse >= ADVERSE_WAIT:
        if reversal_seen and not fav_confirmed:
            return {"action": "wait", "reason":
                    "отлетели против общей (тренд против нас), переворот ВИДЕН, "
                    "но лентой (CVD) НЕ подтверждён — ждём и смотрим по ситуации: "
                    "не доливаем, не переворачиваемся, держим стоп", "manage": True}
        if fav_confirmed:
            return {"action": "hold", "reason":
                    "отлетели, но разворот в нашу сторону подтверждается CVD — "
                    "держим позицию и стоп, готовим частичную фиксацию на возврате",
                    "manage": True}
        return {"action": "reduce", "reason":
                "отлетели против общей, разворота не видно — режем риск "
                "(частичный выход), остаток под стоп", "manage": True}

    # 3) идём по тренду и разворот ПРОТИВ нас проявляется → подтяжка/фиксация
    if not against and reversal_seen and not fav_confirmed:
        return {"action": "trail", "reason":
                "в прибыль по тренду, но виден признак разворота против — "
                "подтягиваем трейлинг-стоп, частично фиксируем", "manage": True}

    return {"action": "hold", "reason": "позиция в согласии со средой — держим, "
            "стоп по плану", "manage": True}


# ── ГЛАВНЫЙ СВОД ────────────────────────────────────────────────────────────
def decide(ctx: dict) -> dict:
    """Единое решение из всего контекста. Устойчиво: нехватка данных или сбой
    ветки → безопасное 'wait', никогда случайное действие.

    ctx (всё опционально):
      directive: {dir, confidence}          — сведённый вектор
      xray:      classify(...)              — кто в стакане (+metrics)
      macro_dir: 'up'|'down'|'flat'         — из reactor/директивы
      reactor:   {bifurcation, kuramoto_r}  — режим неба
      position:  {side, entry, adverse_frac}|None
      expected_move_frac: float             — оценка хода до цели (ATR/вакуум)
      deposit, price, go_per_lot, point_value, commission_side
    """
    try:
        d = ctx or {}
        directive = d.get("directive") or {}
        xray = d.get("xray") or {}
        actor = xray.get("actor") or ""
        metrics = xray.get("metrics") or {}
        macro_dir = d.get("macro_dir") or "flat"
        comm = _f(d.get("commission_side"), COMMISSION_SIDE)
        position = d.get("position")

        # 0) жёсткий guard: инфаркт спреда бьёт всё
        if "ИНФАРКТ" in actor:
            if position:
                mp = manage_position(position, macro_dir, xray, comm)
                return _wrap(mp["action"], mp["reason"], strategy="Инфаркт-guard",
                             lots=0, extra={"guard": "spread_infarct"})
            return _wrap("wait", "инфаркт спреда — рыночные входы запрещены, ждём "
                         "восстановления спреда", strategy="Инфаркт-guard", lots=0,
                         extra={"guard": "spread_infarct"})

        # 1) уже в позиции → ведём её (сюжет «отлетели» и пр.)
        if position and position.get("side"):
            mp = manage_position(position, macro_dir, xray, comm)
            return _wrap(mp["action"], mp["reason"], strategy="Ведение-позиции",
                         lots=position.get("lots", 0), extra={"managing": True})

        # 2) вне рынка → возможен свежий вход
        sizing = trader_risk.situation_to_sizing(xray)
        if sizing.get("risk_off"):
            return _wrap("wait", "risk_off (хаос/истощение) — вне рынка", strategy="Наблюдение",
                         lots=0, extra={"sizing": sizing})

        dir_hint = directive.get("dir") or ("long" if _f(metrics.get("cvd")) > 0
                                            else ("short" if _f(metrics.get("cvd")) < 0 else "flat"))
        strat = pick_strategy(actor, dir_hint, metrics)
        if strat["side"] == "none" or dir_hint == "flat":
            return _wrap("wait", "чистого сетапа нет — ждём, когда стакан назовёт себя",
                         strategy=strat["name"], lots=0)

        # 3) комиссионный фильтр миллиметра
        gate = commission_gate(d.get("expected_move_frac", 0.0), comm)
        if not gate["ok"]:
            return _wrap("wait", "комиссия съест: " + gate["note"] + " — ждём крупнее",
                         strategy=strat["name"], lots=0, extra={"commission": gate})

        # 4) размер: ЛЕСТНИЦА ОРАКУЛА (size_frac — доля ГО от убеждённости),
        #    фолбэк — адаптивный размер (25% база / скачок→макс)
        deposit = _f(d.get("deposit")); price = _f(d.get("price"))
        go = _f(d.get("go_per_lot")); pv = _f(d.get("point_value"), 1.0)
        frac_over = d.get("size_frac")
        lots = 0; size_info = {}
        if deposit > 0 and price > 0 and go > 0:
            if frac_over is not None and _leverage is not None:
                fr = max(0.0, min(1.0, _f(frac_over)))
                lots = _leverage.lots_from(deposit, fr, go)
                size_info = {"lots": lots, "frac": round(fr, 3),
                             "reason": f"лестница Оракула: {round(fr*100)}% ГО"}
            else:
                size_info = trader_risk.adaptive_size(
                    deposit, price, point_value=pv, go_per_lot=go,
                    spike=sizing.get("spike", False),
                    spike_strength=sizing.get("spike_strength", 0.0),
                    risk_off=False)
                lots = size_info.get("lots", 0)
        if lots < 1:
            return _wrap("wait", "размер под сетап < 1 лота (депозит мал или "
                         "предохранитель срезал) — вне рынка", strategy=strat["name"],
                         lots=0, extra={"size": size_info, "commission": gate})

        action = "enter_long" if strat["side"] == "long" else "enter_short"
        return _wrap(action,
                     f"стратегия «{strat['name']}»: {strat['plan']}. "
                     f"Размер {lots} лот(ов) ({size_info.get('reason','')}). "
                     f"{gate['note']}.",
                     strategy=strat["name"], lots=lots, side=strat["side"],
                     extra={"size": size_info, "commission": gate, "sizing": sizing,
                            "directive_dir": dir_hint, "actor": actor})
    except Exception as e:                                  # устойчивость: сбой → ждать
        return _wrap("wait", f"сбой свода ({str(e)[:60]}) — безопасное ожидание, "
                     "не действуем вслепую", strategy="Устойчивость", lots=0)


def _wrap(action, reason, *, strategy, lots, side=None, extra=None):
    out = {"action": action, "side": side, "strategy": strategy, "lots": lots,
           "reason": reason,
           "frame": "⚫ решение свода — план на давлениях, НЕ приказ и НЕ обещание "
                    "прибыли. Курок за человеком. Плечо 18+, рынок не предсказуем."}
    if extra:
        out.update({k: v for k, v in extra.items() if k not in out})
    return out


# ── self-test: сюжеты владельца, чистая композиция ──────────────────────────
if __name__ == "__main__":
    # A) комиссия ПО МАСШТАБУ (0.025%/сторона → круг 0.05%, уточнение
    #    владельца): микро строже, крупняк — только перекрыть.
    #    (Тест был сломан в архиве: константу уточнили, числа теста — нет.)
    g = commission_gate(0.0005)                            # 0.05% → микро, need 0.125% → мимо
    assert g["ok"] is False and g["tier"] == "микро" and abs(g["round_trip"] - 0.0005) < 1e-9
    g_micro_ok = commission_gate(0.0014)                   # 0.14% ≥ 0.125% → микро проходит
    assert g_micro_ok["ok"] is True and g_micro_ok["tier"] == "микро"
    assert commission_gate(0.0030)["ok"] is True and commission_gate(0.0030)["tier"] == "обычный"
    big = commission_gate(0.010)                           # 1% → крупный, комиссия ничтожна
    assert big["ok"] is True and big["tier"] == "крупный" and big["net_edge"] > 0

    # A2) защита от ЛОЖНОЙ остановки — ДВА ФАКТОРА (тенденция + Хёрст):
    #    клевок при H<0.5 и CVD за нас → держим (базовый случай)
    sg_false = stop_guard("long", price=99.95, stop=100.0,
                          xray={"actor": "МАРКЕТМЕЙКЕР: ложный клевок (Stop-Hunt)",
                                "metrics": {"hurst": 0.3, "cvd": 40}})
    assert sg_false["decision"] == "hold_through", sg_false
    #    ОБА фактора ЗА (по тренду + H<0.5): допуск 0.5% — прокол 0.4% ДЕРЖИМ
    #    (раньше 0.2% выбивало — «ММ пережуёт»; теперь буфер настоящий)
    sg_two = stop_guard("long", price=99.6, stop=100.0, trend="up",
                        xray={"metrics": {"hurst": 0.4, "cvd": 10}})
    assert sg_two["decision"] == "hold_through" and abs(sg_two["tol_frac"] - 0.005) < 1e-9, sg_two
    #    но потолок жёсткий: глубже 0.5% — слив признаём ВСЕГДА
    sg_cap = stop_guard("long", price=99.4, stop=100.0, trend="up",
                        xray={"metrics": {"hurst": 0.4, "cvd": 10}})
    assert sg_cap["decision"] == "exit", sg_cap
    #    против тренда + H>0.5 + лента бьёт в пробой → выходим сразу
    sg_real = stop_guard("long", price=99.5, stop=100.0, trend="down",
                         xray={"metrics": {"hurst": 0.6, "cvd": -80}})
    assert sg_real["decision"] == "exit", sg_real
    #    цена не у стопа → ok
    assert stop_guard("long", 101.0, 100.0, {})["decision"] == "ok"

    # B) выбор стратегии под актора
    assert pick_strategy("МАРКЕТМЕЙКЕР расчищает трубу (Казимир+отмены)", "long",
                         {"cvd": 100})["name"] == "Казимир-прострел"
    assert pick_strategy("КИТ грузит айсберг", "long", {"obi": 0.5})["side"] == "long"
    assert pick_strategy("МАРКЕТМЕЙКЕР: ложный клевок (Stop-Hunt)", "long",
                         {})["name"] == "Фейд-стоп-ханта"
    assert pick_strategy("толпа-шум", "flat", {})["side"] == "none"

    # C) ГЛАВНЫЙ СЮЖЕТ ВЛАДЕЛЬЦА: лонг, общая ВНИЗ, отлетели, переворот виден,
    #    но CVD не подтвердил → «ждём, смотрим по ситуации»
    pos = {"side": "long", "entry": 100.0, "adverse_frac": 0.008, "lots": 1}
    xray_rev = {"actor": "МАРКЕТМЕЙКЕР: ложный клевок (Stop-Hunt)",
                "metrics": {"cvd": -50}}                    # лента ещё против нас
    dec = decide({"position": pos, "macro_dir": "down", "xray": xray_rev})
    assert dec["action"] == "wait", dec
    assert "не переворачиваемся" in dec["reason"] and "по ситуации" in dec["reason"]

    #    а если разворот ПОДТВЕРЖДЁН лентой (CVD>0) → держим
    dec2 = decide({"position": pos, "macro_dir": "down",
                   "xray": {"actor": "истощение у растяжки VWAP", "metrics": {"cvd": 80}}})
    assert dec2["action"] == "hold", dec2

    #    отлетели, разворота НЕТ вовсе → режем риск
    dec3 = decide({"position": pos, "macro_dir": "down",
                   "xray": {"actor": "толпа-шум", "metrics": {"cvd": -30}}})
    assert dec3["action"] == "reduce", dec3

    # D) инфаркт спреда бьёт всё (вне позиции → wait; guard помечен)
    di = decide({"xray": {"actor": "ИНФАРКТ СПРЕДА (хаос)"}})
    assert di["action"] == "wait" and di.get("guard") == "spread_infarct"

    # E) свежий вход: Казимир-прострел, ход перекрывает комиссию, депозит есть →
    #    enter с адаптивным размером ≥1 лот
    dfull = decide({
        "directive": {"dir": "long", "confidence": 0.7},
        "xray": {"actor": "МАРКЕТМЕЙКЕР расчищает трубу (Казимир+отмены)",
                 "confidence": 0.7, "metrics": {"cvd": 200, "obi": 0.4}},
        "macro_dir": "up", "expected_move_frac": 0.004,     # 0.4% > 0.16%
        "deposit": 8000, "price": 11000, "go_per_lot": 1200, "point_value": 1.0})
    assert dfull["action"] == "enter_long" and dfull["lots"] >= 1, dfull
    assert dfull["strategy"] == "Казимир-прострел"

    # F) тот же вход, но ход НЕ перекрывает комиссию → wait
    dfee = decide({**{
        "directive": {"dir": "long"}, "macro_dir": "up",
        "xray": {"actor": "МАРКЕТМЕЙКЕР расчищает трубу (Казимир+отмены)",
                 "metrics": {"cvd": 200, "obi": 0.4}},
        "deposit": 8000, "price": 11000, "go_per_lot": 1200, "point_value": 1.0},
        "expected_move_frac": 0.0005})                      # 0.05% < 0.16%
    assert dfee["action"] == "wait" and "комиссия съест" in dfee["reason"], dfee

    # F2) ЛЕСТНИЦА ОРАКУЛА: size_frac=1.0 → макс ГО (8000//1200=6 лотов);
    #     size_frac=0 → вне рынка (лестница сказала «ноль»)
    base_ctx = {
        "directive": {"dir": "long", "confidence": 0.7},
        "xray": {"actor": "МАРКЕТМЕЙКЕР расчищает трубу (Казимир+отмены)",
                 "confidence": 0.7, "metrics": {"cvd": 200, "obi": 0.4}},
        "macro_dir": "up", "expected_move_frac": 0.004,
        "deposit": 8000, "price": 11000, "go_per_lot": 1200, "point_value": 1.0}
    d_max = decide({**base_ctx, "size_frac": 1.0})
    assert d_max["action"] == "enter_long" and d_max["lots"] == 6, d_max
    assert "лестница Оракула" in d_max["reason"]
    d_zero = decide({**base_ctx, "size_frac": 0.0})
    assert d_zero["action"] == "wait", d_zero

    # G) устойчивость: мусорный контекст не роняет — безопасное wait
    assert decide({"xray": {"actor": None}, "directive": None})["action"] == "wait"

    # H) рамка ⚫ всегда на месте
    assert "НЕ приказ" in dfull["frame"] and "18+" in dfull["frame"]

    print("decision self-test OK: свод сводит небо+рентген+директиву+комиссию+"
          "размер в ОДНО решение; сюжет «отлетели против общей» → ждём по "
          "ситуации; комиссия 0.04% режет мелкий ход; инфаркт-guard; устойчив к сбою")
