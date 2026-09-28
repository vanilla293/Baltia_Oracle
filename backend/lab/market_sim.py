# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ · ЛАБ — СИМУЛЯТОР РЫНКА (стенд для конвейера PREDATOR).

Это НЕ движок и НЕ витрина — лабораторный стенд. Он производит поток кадров
в ТОЧНОМ формате Tinkoff MarketDataStream (trade / orderbook), чтобы прогнать
весь веер агентов (Пылесос→Физик→Патологоанатом→Снайпер) БЕЗ живой биржи:
на машине владельца тот же конвейер получит живой стакан вместо симулятора.

Внутрь ЗАШИТА структура, которую агенты и должны находить (иначе стенд
бессмысленен): эпизоды жизни цены —
  · ТИШИНА       — вязкая среда, двусторонний ровный поток, широкий спред;
  · НАКОПЛЕНИЕ   — ММ сжимает спред (Казимир-пружина), грузит айсберг
                   (объём кипит, цена стоит — скрытая абсорбция), перекладывает
                   плиту (OBI ползёт в сторону будущего пробоя);
  · СРЫВ         — каскадный ультра-пробой >1% без отката (толпа догоняет);
  · ИСТОЩЕНИЕ    — гребень рассыпается, поток гаснет.

Псевдослучайность — сид-детерминированный numpy Generator (байт-в-байт
воспроизводимо по seed); это ЛАБ-стенд, а не «random в ядре» (закон школы
про ядро не нарушен — здесь нет расчёта неба).

Запуск-демо: python3 -m backend.lab.market_sim
"""
from __future__ import annotations

import numpy as np


def _q(x: float) -> dict:
    """float → Quotation Tinkoff {units, nano}."""
    u = int(x)
    return {"units": u, "nano": int(round((x - u) * 1e9))}


def _iso(ms: int) -> str:
    """ms unix → ISO с миллисекундами (как отдаёт Tinkoff)."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000.0, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _book_frame(ms: int, mid: float, obi_target: float, spread_bps: float,
                rng, wall_side: str | None = None, wall_mult: float = 1.0):
    """Снимок стакана на 10 уровней с заданным дисбалансом и (опц.) плитой."""
    half = mid * spread_bps / 2e4
    bb, ba = mid - half, mid + half
    step = max(mid * 5e-5, half)
    # базовые объёмы уровней + шум; дисбаланс задаём масштабом сторон
    base = 100.0
    k = (1.0 + obi_target) / max(1e-9, 1.0 - obi_target)   # bid/ask отношение
    bids, asks = [], []
    for i in range(10):
        qb = base * (0.7 ** i) * (k ** 0.5) * (1 + 0.1 * rng.standard_normal())
        qa = base * (0.7 ** i) / (k ** 0.5) * (1 + 0.1 * rng.standard_normal())
        bids.append({"price": _q(bb - i * step), "quantity": str(int(max(1, qb)))})
        asks.append({"price": _q(ba + i * step), "quantity": str(int(max(1, qa)))})
    if wall_side == "bid":                                 # плита-стена снизу
        bids[3]["quantity"] = str(int(base * 6 * wall_mult))
    elif wall_side == "ask":
        asks[3]["quantity"] = str(int(base * 6 * wall_mult))
    return {"orderbook": {"bids": bids, "asks": asks, "time": _iso(ms)}}


def _trade_frame(ms: int, price: float, qty: float, side: int):
    return {"trade": {"direction": ("TRADE_DIRECTION_BUY" if side > 0
                                    else "TRADE_DIRECTION_SELL"),
                      "price": _q(price), "quantity": str(int(max(1, qty))),
                      "time": _iso(ms)}}


def frames(seed: int = 7, episodes: int = 4, base: float = 100.0):
    """Генератор кадров сессии. Каждый эпизод: тишина→накопление→срыв→истощение.
    Возвращает список кадров (trade/orderbook) с растущими таймстампами (мс)."""
    rng = np.random.default_rng(seed)
    out = []
    t = 1_700_000_000_000                                  # старт мс (фикс)
    price = base
    for _ in range(episodes):
        # ── ТИШИНА (90 с): вязко, двусторонний поток, широкий спред ──
        for s in range(90):
            out.append(_book_frame(t, price, 0.0, 3.0 + 0.3 * rng.standard_normal(), rng))
            if s % 2 == 0:
                side = 1 if rng.random() > 0.5 else -1
                out.append(_trade_frame(t + 200, price, 2 + rng.integers(0, 3), side))
            price *= 1.0 + 0.00002 * rng.standard_normal()
            t += 1000
        # ── НАКОПЛЕНИЕ (60 с): спред сжимается, OBI ползёт вверх, айсберг ──
        up = rng.random() > 0.35                           # сторона будущего срыва
        obi_dir = 1.0 if up else -1.0
        for s in range(60):
            spread = max(0.6, 3.0 - s * 0.04)
            obi = obi_dir * min(0.6, s / 100.0)
            out.append(_book_frame(t, price, obi, spread, rng,
                                   wall_side=("ask" if up else "bid"),
                                   wall_mult=1.0 + s / 60.0))
            # айсберг: поток УЧАЩАЕТСЯ и односторонний, а цена почти стоит
            reps = 1 + s // 12
            for r in range(reps):
                out.append(_trade_frame(t + r * 60, price,
                                        3 + rng.integers(0, 3),
                                        1 if up else -1))
            price *= 1.0 + 0.00001 * obi_dir               # едва ползёт
            t += 1000
        # ── СРЫВ (30 с): каскад >1.3% без отката, толпа догоняет ──
        p0 = price
        move = (0.013 + 0.004 * rng.random()) * obi_dir
        for s in range(30):
            price = p0 * (1.0 + move * (s + 1) / 30.0)
            out.append(_book_frame(t, price, 0.5 * obi_dir, 1.0, rng))
            out.append(_trade_frame(t, price, 5 + rng.integers(0, 5),
                                    1 if up else -1))
            t += 1000
        # ── ИСТОЩЕНИЕ (40 с): гребень гаснет, поток редеет, спред обратно ──
        for s in range(40):
            out.append(_book_frame(t, price, 0.0, 2.0 + s * 0.02, rng))
            if s % 3 == 0:
                out.append(_trade_frame(t, price, 2, 1 if rng.random() > 0.5 else -1))
            price *= 1.0 + 0.00003 * rng.standard_normal()
            t += 1000
    return out


if __name__ == "__main__":
    fr = frames(seed=7, episodes=2)
    n_tr = sum(1 for f in fr if "trade" in f)
    n_ob = sum(1 for f in fr if "orderbook" in f)
    assert n_tr > 200 and n_ob > 400, (n_tr, n_ob)
    # детерминизм по сиду
    assert frames(7, 1)[:20] == frames(7, 1)[:20]
    # первый кадр — валидный стакан Tinkoff-формата
    f0 = next(f for f in fr if "orderbook" in f)
    assert "bids" in f0["orderbook"] and "time" in f0["orderbook"]
    print(f"market_sim OK: {len(fr)} кадров ({n_tr} сделок, {n_ob} стаканов), "
          "формат Tinkoff, детерминизм по сиду")
