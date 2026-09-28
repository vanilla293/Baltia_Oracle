# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — РЕНТГЕН СТАКАНА (микроструктура рынка).

Мозг Фармилы на миллиметрах: по потоку снимков стакана+ленты считает ПО
ФОРМУЛАМ все сигналы из txt и КЛАССИФИЦИРУЕТ ситуацию — «тут маркетмейкер
рисует, тут Кит грузит айсберг, тут толпа паникует, тут вакуум-ловушка».
Чистая арифметика по снимкам — БЕЗ сети и ордеров, проверяема здесь целиком.

Формулы (каждая — отдельная проверяемая функция):
  · OBI  — дисбаланс стакана: (bidV−askV)/(bidV+askV)         [куда давит ММ стенами]
  · CVD  — накопленная дельта ленты: Σ(buyV−sellV)            [реальные деньги]
  · VPIN — токсичность потока: |buy−sell|/(buy+sell) по объёму [умные деньги пылесосят]
  · cancel_velocity — скорость отмен уровней между снимками    [пульс: ММ расчищает трубу]
  · VWAP + отклонение — гравитационный центр дня               [резина натянута]
  · Hurst — экспонента Хёрста: >0.5 тренд, <0.5 возврат        [ложный клевок vs слом]
  · casimir — сжатие спреда (∝1/a⁴): критическое сжатие = взрыв [пружина сжата]
  · absorption — объём кипит, цена стоит → айсберг Кита         [встречная абсорбция]
  · exhaustion — скорость ленты падает → топливо кончилось     [смерть агрессора]

classify(...) сводит их в ИМЕНОВАННУЮ ситуацию + рекомендованную позу.
Рамка ⚫: карта давлений, НЕ обещание прибыли, рынок не предсказуем, 18+.
"""
from __future__ import annotations

import math

# пороги школы микроструктуры (🟡 калибруются под актив)
VPIN_TOXIC = 0.35        # выше — поток токсичный (умные деньги)
CANCEL_STORM = 3.0         # отмен/уровней за тик в N раз выше фона → шквал
CASIMIR_CRIT = 0.40        # спред сжат до <40% нормы устойчиво → критично
ABSORB_MOVE = 0.0003      # |ход| < 3 bps при большом объёме = абсорбция
EXHAUST_FALL = 0.4         # скорость ленты упала до <40% пика → истощение
WALL_MULT = 20.0        # заявка ≥ ×медианы = стена
HURST_TREND = 0.5


def _f(x, d=0.0):
    try:
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


def _levels_vol(levels, n=10) -> float:
    return sum(_f(l.get("q")) for l in (levels or [])[:n])


# ── формулы ─────────────────────────────────────────────────────────────────
def obi(bids, asks, n=10) -> float:
    """Order Book Imbalance: −1 (давят вниз стенами ask) … +1 (вверх)."""
    bv, av = _levels_vol(bids, n), _levels_vol(asks, n)
    tot = bv + av
    return (bv - av) / tot if tot > 0 else 0.0


def cvd(trades) -> float:
    """Cumulative Volume Delta: Σ(buy−sell) по ленте (реальные удары)."""
    s = 0.0
    for t in (trades or []):
        v = _f(t.get("q"))
        s += v if t.get("dir") == "buy" else -v
    return s


def vpin(trades) -> float:
    """Токсичность потока: |Σbuy−Σsell| / Σ(all). →1 = однонаправленный
    пылесос умных денег; →0 = сбалансированная паника толпы."""
    b = sum(_f(t.get("q")) for t in (trades or []) if t.get("dir") == "buy")
    s = sum(_f(t.get("q")) for t in (trades or []) if t.get("dir") == "sell")
    tot = b + s
    return abs(b - s) / tot if tot > 0 else 0.0


def cancel_velocity(prev_levels, cur_levels, side_key="p") -> float:
    """Скорость отмен: доля уровней прошлого снимка, ИСЧЕЗНУВШИХ в текущем.
    Шквал отмен = ММ в панике расчищает трубу перед прострелом."""
    prev = {round(_f(l.get(side_key)), 6) for l in (prev_levels or [])}
    cur = {round(_f(l.get(side_key)), 6) for l in (cur_levels or [])}
    if not prev:
        return 0.0
    gone = len(prev - cur)
    return gone / len(prev)


def vwap(trades) -> float | None:
    """Гравитационный центр: Σ(p·v)/Σv по ленте. None если ленты нет."""
    pv = sum(_f(t.get("p")) * _f(t.get("q")) for t in (trades or []))
    v = sum(_f(t.get("q")) for t in (trades or []))
    return pv / v if v > 0 else None


def vwap_deviation(price, vw) -> float:
    """Натяжение резины: (цена−VWAP)/VWAP. Большое |откл| → возврат к центру."""
    if not vw or vw <= 0:
        return 0.0
    return (price - vw) / vw


def hurst(prices) -> float:
    """Экспонента Хёрста (rescaled range, упрощённый R/S).
    >0.5 тренд (реальный полёт), <0.5 возврат к среднему (синтетический клевок).
    <8 точек → 0.5 (не судим)."""
    p = [_f(x) for x in (prices or [])]
    n = len(p)
    if n < 8:
        return 0.5
    rets = [p[i + 1] - p[i] for i in range(n - 1)]
    mean = sum(rets) / len(rets)
    dev = 0.0
    cum, maxc, minc = 0.0, 0.0, 0.0
    for r in rets:
        cum += r - mean
        maxc = max(maxc, cum)
        minc = min(minc, cum)
    R = maxc - minc
    S = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))
    if S <= 0 or R <= 0:
        return 0.5
    rs = R / S
    h = math.log(rs) / math.log(len(rets))
    return max(0.0, min(1.0, h))


def casimir(spread_bps, norm_bps) -> dict:
    """Сжатие спреда как давление вакуума ∝1/a⁴ (эффект Казимира из txt).
    Спред сузился в 2× → давление ×16. Устойчивое критическое сжатие = взрыв."""
    norm_bps = max(1e-9, norm_bps)
    a = max(1e-9, spread_bps / norm_bps)         # относительный «зазор» a
    pressure = 1.0 / (a ** 4)                     # относительное давление
    crit = a <= CASIMIR_CRIT
    return {"a": round(a, 3), "pressure": round(pressure, 2), "critical": bool(crit),
            "word": ("пружина сжата до предела — жди микро-отмены и пробой"
                     if crit else "спред не сжат")}


def absorption(price_move_frac, trade_vol, vol_norm) -> dict:
    """Встречная абсорбция: объём КИПИТ, а цена СТОИТ → Кит держит айсберг."""
    hot = trade_vol >= vol_norm * 1.5
    stuck = abs(_f(price_move_frac)) < ABSORB_MOVE
    on = bool(hot and stuck)
    return {"absorbing": on, "hot": bool(hot), "stuck": bool(stuck),
            "word": ("абсорбция: объём летит, цена стоит — Кит гасит айсбергом"
                     if on else "абсорбции нет")}


def exhaustion(speed_now, speed_peak) -> dict:
    """Кислородное голодание: скорость ленты упала → двигатель заглох."""
    peak = max(1e-9, speed_peak)
    ratio = speed_now / peak
    dead = ratio < EXHAUST_FALL
    return {"exhausted": bool(dead), "ratio": round(ratio, 3),
            "word": ("смерть агрессора: лента иссякла, топливо кончилось"
                     if dead else "лента жива")}


def wall_kind(persistence) -> str:
    """Стена настоящая или призрак — по устойчивости во времени (доля тиков)."""
    if persistence is None:
        return "неизвестно"
    return "упор" if persistence >= 0.5 else "призрак"


# ── классификатор ситуации: КТО сейчас в стакане ────────────────────────────
def classify(feats: dict) -> dict:
    """Сводит формулы в ИМЕНОВАННУЮ ситуацию + рекомендованную позу Фармилы.

    feats: obi, cvd, vpin, cancel_vel, vwap_dev, hurst, casimir(dict),
           absorption(dict), exhaustion(dict), spread_bad, wall_ahead_mult,
           poke_against, move_frac, aggressor.
    Возврат: {actor, posture, confidence, signals[], note}.
    """
    sig = []
    vp = _f(feats.get("vpin"))
    hu = _f(feats.get("hurst"), 0.5)
    cas = feats.get("casimir") or {}
    ab = feats.get("absorption") or {}
    ex = feats.get("exhaustion") or {}
    cvel = _f(feats.get("cancel_vel"))
    obi_v = _f(feats.get("obi"))
    dev = _f(feats.get("vwap_dev"))

    actor = "толпа-шум"
    posture = "ждать чистый сетап"
    conf = 0.3

    # 1) Кит грузит айсберг (абсорбция + токсичный поток) — готовим вход ЗА ним
    if ab.get("absorbing") and vp >= VPIN_TOXIC:
        actor = "КИТ грузит айсберг"
        posture = "готовить вход в сторону впитывания (Кит наберёт — отпустит, цена улетит)"
        sig.append("абсорбция+VPIN токсичный")
        conf = 0.75
    # 2) Синтетический клевок (Херст<0.5, реальных продаж по CVD нет) — держать/ловить
    elif hu < HURST_TREND and feats.get("poke_against") and _f(feats.get("cvd")) >= 0:
        actor = "МАРКЕТМЕЙКЕР: ложный клевок (сброс слабаков)"
        posture = "не выходить — это Stop-Hunt; лимитки на дно прокола, ММ нальёт по лучшей цене"
        sig.append("Херст<0.5 + CVD не красный")
        conf = 0.7
    # 3) Казимир-сжатие + шквал отмен — взрыв близко, вход до первого тика
    elif cas.get("critical") and cvel >= 0.3:
        actor = "МАРКЕТМЕЙКЕР расчищает трубу (Казимир+отмены)"
        posture = "пружина сжата, ММ снимает лимитки — вход в сторону вакуума ДО тика цены"
        sig.append("Казимир критичен + шквал отмен")
        conf = 0.7
    # 4) Смерть агрессора на растяжении от VWAP — импульс сдох, выход/разворот
    elif ex.get("exhausted") and abs(dev) > 0.003:
        actor = "истощение у растяжки VWAP"
        posture = "топливо кончилось, резина тянет к центру — фиксировать/ждать разворот к VWAP"
        sig.append("exhaustion + |VWAP-откл|>0.3%")
        conf = 0.6
    # 5) ММ рисует стены (сильный OBI), но CVD против рисунка — иллюзия
    elif abs(obi_v) > 0.5 and (obi_v * _f(feats.get("cvd"))) < 0:
        actor = "МАРКЕТМЕЙКЕР рисует иллюзию (OBI≠CVD)"
        posture = "стакан пугает в одну сторону, реальных денег там нет — ждать разворот в сторону CVD"
        sig.append("дивергенция OBI и CVD")
        conf = 0.6
    # 6) Инфаркт спреда — руки прочь
    if feats.get("spread_bad"):
        actor = "ИНФАРКТ СПРЕДА (хаос)"
        posture = "рынок разорван — рыночные входы запрещены, только выход/безубыток"
        sig.append("спред разорван")
        conf = 0.9

    return {"actor": actor, "posture": posture, "confidence": round(conf, 2),
            "signals": sig,
            "metrics": {"obi": round(obi_v, 3), "cvd": round(_f(feats.get('cvd')), 1),
                        "vpin": round(vp, 3), "hurst": round(hu, 3),
                        "cancel_vel": round(cvel, 3), "vwap_dev": round(dev, 4),
                        "casimir_a": cas.get("a")},
            "note": "⚫ классификация давлений, НЕ приказ. Рынок не предсказуем. 18+"}


# ── сборка живого рентгена из стакана+ленты Tinkoff ─────────────────────────
def xray_live(ob: dict | None, tape: dict | None, *,
              norm_spread_bps: float = 3.0, prev_asks=None, prev_bids=None,
              prices=None, vwap_now=None, vol_now=None, vol_norm=None,
              speed_now=None, speed_peak=None, poke_against=None) -> dict:
    """Мгновенный рентген из одного снимка Tinkoff: OBI, спред/Казимир из
    стакана; CVD/VPIN из агрегатов ленты (buy_vol/sell_vol); отмены — если
    передан прошлый снимок. Возвращает classify(...). Нет стакана → None.

    Дополнительные входы (аудит: 3 из 6 веток классификатора были мертвы,
    Хёрст стоял константой 0.5) — все опциональны, NO DUMMIES:
      prices     — история mid-цены → живой Хёрст + move_frac абсорбции;
      vwap_now   — сессионный VWAP → vwap_dev (ветка «истощение у растяжки»);
      vol_now/vol_norm   — объём ленты сейчас/норма → absorption (ветка «КИТ»);
      speed_now/speed_peak — сделок в окне сейчас/пик → exhaustion;
      poke_against — прокол недавнего экстремума с возвратом (ветка Stop-Hunt)."""
    if not isinstance(ob, dict):
        return {"available": False, "note": "стакан недоступен"}
    bids = ob.get("levels_bid") or ob.get("top_bids") or []
    asks = ob.get("levels_ask") or ob.get("top_asks") or []
    if not bids or not asks:
        return {"available": False, "note": "уровней стакана нет"}
    buy_v = _f((tape or {}).get("buy_vol"))
    sell_v = _f((tape or {}).get("sell_vol"))
    tot = buy_v + sell_v
    feats = {
        "obi": obi(bids, asks),
        "cvd": buy_v - sell_v,
        "vpin": (abs(buy_v - sell_v) / tot) if tot > 0 else 0.0,
        "cancel_vel": (0.5 * cancel_velocity(prev_bids, bids)
                       + 0.5 * cancel_velocity(prev_asks, asks))
                      if (prev_bids or prev_asks) else 0.0,
        "casimir": casimir(_f(ob.get("spread_bps"), norm_spread_bps), norm_spread_bps),
        "hurst": hurst(prices) if prices else 0.5,
        "spread_bad": _f(ob.get("spread_bps")) > norm_spread_bps * 3.0,
    }
    p = [_f(x) for x in (prices or []) if x]
    mid = p[-1] if p else None
    if vwap_now and mid:
        feats["vwap_dev"] = vwap_deviation(mid, _f(vwap_now))
    if vol_now is not None and vol_norm:
        move = (abs(p[-1] - p[-40]) / p[-40]) if len(p) >= 40 and p[-40] else 0.0
        feats["absorption"] = absorption(move, _f(vol_now), _f(vol_norm))
        feats["move_frac"] = move
    if speed_now is not None and speed_peak:
        feats["exhaustion"] = exhaustion(_f(speed_now), _f(speed_peak))
    if poke_against is not None:
        feats["poke_against"] = bool(poke_against)
    res = classify(feats)
    res["available"] = True
    return res


# ── self-test: чистая математика на синтетических снимках ────────────────────
if __name__ == "__main__":
    # OBI: bid-объём вдвое больше ask → положительный
    bids = [{"p": 100 - i * 0.1, "q": 200} for i in range(10)]
    asks = [{"p": 100 + i * 0.1, "q": 100} for i in range(10)]
    o = obi(bids, asks)
    assert 0.3 < o < 0.34, o                         # (2000-1000)/3000

    # CVD и VPIN: 700 buy vs 300 sell
    trades = [{"p": 100, "q": 700, "dir": "buy"}, {"p": 100, "q": 300, "dir": "sell"}]
    assert cvd(trades) == 400.0
    assert abs(vpin(trades) - 0.4) < 1e-9            # |700-300|/1000

    # скорость отмен: из 10 уровней исчезли 4
    prev = [{"p": 100 - i * 0.1} for i in range(10)]
    cur = [{"p": 100 - i * 0.1} for i in range(6)]
    assert abs(cancel_velocity(prev, cur) - 0.4) < 1e-9

    # VWAP и отклонение
    vw = vwap([{"p": 100, "q": 1}, {"p": 102, "q": 1}])
    assert vw == 101.0
    assert abs(vwap_deviation(103, 101) - (2 / 101)) < 1e-9

    # Хёрст: персистентный ряд (прогоны в одну сторону) → >0.5;
    #        антиперсистентный (пила тик-в-тик) → <0.5.
    runs = ([1] * 5 + [-1] * 5) * 2                  # длинные прогоны = тренд
    trend = [100.0]
    for r in runs:
        trend.append(trend[-1] + r)
    saw = [100 + (1 if i % 2 else -1) for i in range(20)]   # пила = возврат
    assert hurst(trend) > 0.5, hurst(trend)
    assert hurst(saw) < 0.5, hurst(saw)
    assert hurst([1, 2, 3]) == 0.5                   # мало точек — не судим

    # Казимир: спред сжат вчетверо → давление ×256, критично
    c = casimir(0.5, 2.0)                             # a=0.25 → 1/0.25^4=256
    assert c["critical"] and abs(c["pressure"] - 256.0) < 1.0
    assert casimir(2.0, 2.0)["critical"] is False

    # абсорбция: объём кипит, цена стоит
    assert absorption(0.0001, 1000, 500)["absorbing"] is True
    assert absorption(0.01, 1000, 500)["absorbing"] is False   # цена ушла
    assert absorption(0.0001, 100, 500)["absorbing"] is False  # объёма мало

    # истощение
    assert exhaustion(2, 10)["exhausted"] is True    # 0.2 < 0.4
    assert exhaustion(8, 10)["exhausted"] is False

    # стена: устойчивость
    assert wall_kind(0.8) == "упор" and wall_kind(0.2) == "призрак"

    # ── классификатор: Кит грузит айсберг ──
    cl = classify({"absorption": {"absorbing": True}, "vpin": 0.5, "cvd": 500,
                   "hurst": 0.6})
    assert "КИТ" in cl["actor"] and cl["confidence"] >= 0.7

    # ── синтетический клевок ММ (Stop-Hunt) ──
    cl2 = classify({"hurst": 0.3, "poke_against": True, "cvd": 10, "vpin": 0.2})
    assert "ложный клевок" in cl2["actor"] and "не выходить" in cl2["posture"]

    # ── Казимир + отмены → взрыв близко ──
    cl3 = classify({"casimir": {"critical": True}, "cancel_vel": 0.5, "hurst": 0.6,
                    "vpin": 0.2})
    assert "расчищает трубу" in cl3["actor"]

    # ── ММ рисует иллюзию: OBI вниз, CVD вверх ──
    cl4 = classify({"obi": -0.7, "cvd": 300, "vpin": 0.2, "hurst": 0.6})
    assert "иллюзию" in cl4["actor"]

    # ── инфаркт спреда перебивает всё ──
    cl5 = classify({"spread_bad": True, "absorption": {"absorbing": True}, "vpin": 0.5})
    assert "ИНФАРКТ" in cl5["actor"] and cl5["confidence"] >= 0.9

    # ── живой рентген из снимка Tinkoff ──
    ob = {"levels_bid": [{"p": 100 - i * 0.1, "q": 200} for i in range(20)],
          "levels_ask": [{"p": 100.1 + i * 0.1, "q": 100} for i in range(20)],
          "spread_bps": 1.0}
    tape = {"buy_vol": 800, "sell_vol": 200}
    xl = xray_live(ob, tape, norm_spread_bps=3.0)
    assert xl["available"] and xl["metrics"]["obi"] > 0 and xl["metrics"]["cvd"] == 600
    assert abs(xl["metrics"]["vpin"] - 0.6) < 1e-9
    assert xray_live(None, None)["available"] is False

    print("microstructure self-test OK: OBI/CVD/VPIN/отмены/VWAP/Хёрст/Казимир/"
          "абсорбция/истощение — по формулам; классификатор различает "
          "ММ / Кита / ловушку / инфаркт; живой рентген из снимка Tinkoff собран")
