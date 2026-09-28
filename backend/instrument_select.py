# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — СЕЛЕКТОР ФЬЮЧЕРСА (какой именно контракт брать).

Отвечает на живые вопросы владельца:
  · «какой ИМЕННО Si он возьмёт?»  → ближний (front-month) ликвидный контракт,
    не истёкший; дальние экспирации с пустым стаканом отсекаются.
  · «может лучше CR — кто быстрее движется, того и берём?» → сравнение баз
    Si (USD/RUB) и CR (CNY/RUB) по СКОРОСТИ (дневной размах, реализованная
    волатильность) с учётом ликвидности.
  · «влезет ли в мои 8000₽?» → фильтр по ГО (гарантийному обеспечению) с
    запасом на неблагоприятный ход: контракт, чьё ГО не оставляет буфера,
    выбрасывается. CR дешевле по ГО — на малом депозите обычно он.

Ничего не выдумывает (NO DUMMIES): нет данных по контракту — он просто не
участвует. Всё параметрами — реальные цифры подаёт fetch из Tinkoff.

⚫ Это выбор инструмента, а НЕ обещание прибыли. Фьючерс с плечом — там, где
теряет большинство. Рынок не предсказуем. 18+.
"""
from __future__ import annotations

import math

MARGIN_BUFFER = 0.6      # ГО должно занимать ≤ 1/(1+buffer) депозита (запас на ход)
MIN_EXPIRY_DAYS = 4        # ближе к экспирации не входим (риск сжатия/ГО-скачка)
MIN_VOLUME = 1000     # контракт с оборотом ниже — неликвид, пропускаем


def _f(x, d=0.0):
    try:
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


def affordable_lots(account_rub: float, margin_per_lot_rub: float,
                    buffer: float = MARGIN_BUFFER) -> int:
    """Сколько лотов реально можно держать: ГО×(1+буфер) должно влезать в
    депозит (буфер — подушка на неблагоприятный ход до стоп-аута)."""
    need = _f(margin_per_lot_rub) * (1.0 + buffer)
    if need <= 0:
        return 0
    return int(_f(account_rub) // need)


def front_month(contracts: list[dict]) -> dict | None:
    """Ближний живой контракт одной базы: минимальная экспирация > порога с
    достаточным оборотом. Дальние пустые — мимо."""
    live = [c for c in (contracts or [])
            if _f(c.get("expiry_days")) >= MIN_EXPIRY_DAYS
            and _f(c.get("volume")) >= MIN_VOLUME]
    if not live:
        return None
    # ближайшая экспирация; при равенстве — больший оборот
    return sorted(live, key=lambda c: (_f(c.get("expiry_days")),
                                       -_f(c.get("volume"))))[0]


def _speed(c: dict) -> float:
    """Скорость движения контракта: дневной размах в % (реализованная воля).
    Именно это владелец зовёт «кто быстрее движется»."""
    return _f(c.get("day_range_pct"))


def _liquidity(c: dict) -> float:
    """Ликвидность: оборот, штрафованный шириной спреда (узкий спред лучше)."""
    vol = _f(c.get("volume"))
    spr = max(0.1, _f(c.get("spread_bps"), 1.0))
    return vol / spr


def choose(account_rub: float, contracts: list[dict], *,
           buffer: float = MARGIN_BUFFER) -> dict:
    """Главный выбор. Группирует контракты по базе (Si/CR/…), берёт по
    front-month каждой базы, отбирает влезающие в депозит и возвращает
    самый БЫСТРЫЙ×ЛИКВИДНЫЙ. Если самый быстрый не влезает по ГО — честно
    сообщает и берёт следующий, который влезает."""
    bases: dict[str, list[dict]] = {}
    for c in (contracts or []):
        bases.setdefault(c.get("base") or c.get("code", "?")[:2], []).append(c)

    fronts = []
    for base, lst in bases.items():
        fm = front_month(lst)
        if fm:
            fm = dict(fm)
            fm["_lots"] = affordable_lots(account_rub, fm.get("margin_per_lot_rub"), buffer)
            fm["_affordable"] = fm["_lots"] >= 1
            fm["_speed"] = _speed(fm)
            fm["_liq"] = _liquidity(fm)
            fm["_score"] = fm["_speed"] * math.log1p(fm["_liq"])
            fronts.append(fm)

    if not fronts:
        return {"chosen": None, "note": "живых ликвидных контрактов нет "
                "(биржа закрыта / нет данных по фьючерсам)", "ranked": []}

    ranked = sorted(fronts, key=lambda c: -c["_score"])
    afford = [c for c in ranked if c["_affordable"]]

    notes = []
    fastest = ranked[0]
    if afford:
        chosen = afford[0]
        if fastest is not chosen:
            notes.append(
                f"{fastest.get('code')} движется быстрее ({fastest['_speed']:.2f}%/день), "
                f"но его ГО {fastest.get('margin_per_lot_rub'):.0f}₽ не оставляет буфера "
                f"в депозите {account_rub:.0f}₽ — берём {chosen.get('code')} "
                f"(ГО {chosen.get('margin_per_lot_rub'):.0f}₽, влезает {chosen['_lots']} лот(а/ов))")
        else:
            notes.append(
                f"{chosen.get('code')} и самый быстрый ({chosen['_speed']:.2f}%/день), "
                f"и влезает в депозит: {chosen['_lots']} лот(а/ов), "
                f"ГО {chosen.get('margin_per_lot_rub'):.0f}₽")
    else:
        chosen = None
        cheapest = min(ranked, key=lambda c: _f(c.get("margin_per_lot_rub"), 1e18))
        notes.append(
            f"НИ ОДИН контракт не влезает в {account_rub:.0f}₽ с запасом: даже "
            f"самый дешёвый {cheapest.get('code')} требует ГО "
            f"{cheapest.get('margin_per_lot_rub'):.0f}₽×(1+{buffer:g}). "
            f"Депозит мал для фьючерса с буфером — пополнить или снизить буфер осознанно")

    # прозрачность риска на выбранном
    per_move = None
    if chosen:
        pv = _f(chosen.get("point_value_rub"), 1.0)
        per_move = {
            "point_value_rub": pv,
            "lots": chosen["_lots"],
            "risk_1pct_move_rub": round(_f(chosen.get("price")) * 0.01 * pv * chosen["_lots"], 1),
            "hint": ("ход цены на 1% при "
                     f"{chosen['_lots']} лот(ах) ≈ "
                     f"{round(_f(chosen.get('price')) * 0.01 * pv * chosen['_lots'], 0):.0f}₽ P/L"),
        }

    return {
        "chosen": ({"code": chosen.get("code"), "name": chosen.get("name"),
                    "base": chosen.get("base"), "price": chosen.get("price"),
                    "margin_per_lot_rub": chosen.get("margin_per_lot_rub"),
                    "expiry_days": chosen.get("expiry_days"),
                    "lots": chosen["_lots"], "speed_pct": round(chosen["_speed"], 3),
                    "volume": chosen.get("volume")} if chosen else None),
        "risk": per_move,
        "note": " · ".join(notes),
        "ranked": [{"code": c.get("code"), "base": c.get("base"),
                    "speed_pct": round(c["_speed"], 3),
                    "margin_per_lot_rub": c.get("margin_per_lot_rub"),
                    "affordable": c["_affordable"], "lots": c["_lots"]}
                   for c in ranked],
        "frame": "⚫ выбор инструмента, НЕ приказ и НЕ обещание прибыли. "
                 "Фьючерс с плечом — 18+, рынок не предсказуем.",
    }


# ── self-test: реальные сценарии владельца (8000₽), чистая логика ────────────
if __name__ == "__main__":
    # депозит владельца ~8000₽; Si дороже по ГО, CR дешевле
    Si = {"code": "SiU6", "base": "Si", "name": "USD/RUB", "price": 80000,
          "margin_per_lot_rub": 6000, "spread_bps": 1.5, "day_range_pct": 1.2,
          "volume": 500000, "expiry_days": 40, "point_value_rub": 1.0}
    Si_far = {"code": "SiZ6", "base": "Si", "price": 80500,
              "margin_per_lot_rub": 6100, "spread_bps": 4.0, "day_range_pct": 1.1,
              "volume": 20000, "expiry_days": 130, "point_value_rub": 1.0}
    CR = {"code": "CRU6", "base": "CR", "name": "CNY/RUB", "price": 11000,
          "margin_per_lot_rub": 1200, "spread_bps": 2.0, "day_range_pct": 0.9,
          "volume": 300000, "expiry_days": 40, "point_value_rub": 1.0}

    # 1) депозит 8000₽: Si не влезает с буфером (6000×1.6=9600>8000), CR влезает
    r = choose(8000, [Si, Si_far, CR])
    assert r["chosen"] is not None
    assert r["chosen"]["code"] == "CRU6", r["chosen"]
    assert r["chosen"]["lots"] >= 1
    assert "не оставляет буфера" in r["note"] or "берём CRU6" in r["note"]

    # 2) front-month: из двух Si выбирается ближний ликвидный (SiU6, не SiZ6)
    fm = front_month([Si, Si_far])
    assert fm["code"] == "SiU6", fm

    # 3) депозит 50000₽: оба влезают → берём БЫСТРЕЙШИЙ×ликвидный (Si, 1.2%>0.9%)
    r2 = choose(50000, [Si, CR])
    assert r2["chosen"]["code"] == "SiU6", r2["chosen"]
    assert r2["chosen"]["lots"] >= 1

    # 4) affordable_lots арифметика
    assert affordable_lots(8000, 1200, 0.6) == int(8000 // (1200 * 1.6))  # =4
    assert affordable_lots(8000, 6000, 0.6) == 0                          # 9600>8000

    # 5) вообще нет живых контрактов → честный None
    r3 = choose(8000, [{"code": "SiH1", "base": "Si", "expiry_days": 1, "volume": 0}])
    assert r3["chosen"] is None and "нет" in r3["note"]

    # 6) риск-прозрачность присутствует
    assert r["risk"] and r["risk"]["lots"] == r["chosen"]["lots"]

    # 7) рамка ⚫ на месте
    assert "18+" in r["frame"]

    print("instrument_select self-test OK: 8000₽ → CR (Si не влезает с буфером); "
          "front-month ближний ликвидный; 50000₽ → быстрейший Si; риск прозрачен")
