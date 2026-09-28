"""ОРАКУЛ // ПИФИЯ — МАЙЯ: вакуум в стакане ордеров (финансовая кавитация).

Канон мировоззрения 05.08.2026: маркетмейкер действует не массой, а
ТОПОЛОГИЕЙ — создаёт синтетические разрежения (вакуум) в стакане; толпа
всасывается в пустоту (имплозия по Пригожину). Словарь слоя:
∇·J = −κ·∂P_synth/∂t — язык школы 🟡, не физический закон.

Механика 🔵 (проверяемая арифметика по РЕАЛЬНЫМ уровням стакана Tinkoff):
  · плотность ликвидности — суммарная заявленная масса q на скользящем окне
    уровней (VAC_WIN) каждой стороны;
  · вакуум-зона = окно с МИНИМАЛЬНОЙ суммой q (максимальное разрежение)
    над ценой (ask) и под ценой (bid); глубина = 1 − min/среднее;
  · стена = уровень с массой ≥ WALL_MULT × медианы стороны;
  · тяга (pull) = сторона с более глубоким и близким вакуумом: цену
    всасывает в разрежение (имплозия), стена — упор с другой стороны;
  · связка с лентой: aggressor_ratio показывает, кто продавливает.

Рамка честности: 🔵 арифметика уровней · 🟡 словарь Майи (вакуум/имплозия) ·
⚫ полюс за человеком — механика Майи НЕ торговый сигнал · 18+.
NO DUMMIES: нет стакана / мало уровней → available=False + честный note.
Никакого random — детерминизм байт-в-байт.
"""
from __future__ import annotations

VAC_WIN = 5        # скользящее окно уровней для плотности
MIN_LEVELS = 8     # меньше уровней стороны — вакуум честно не считается
WALL_MULT = 3.0    # стена = заявка ≥ 3× медианы стороны
PULL_GAP = 0.05    # разница глубин вакуума меньше — «равновесие», тяги нет

VERDICT_FRAME = ("⚫ вакуум/имплозия/сингулярность — словарь первоисточника 🟡 "
                 "(∇·J = −κ·∂P/∂t), суммы уровней — арифметика стакана 🔵 · "
                 "НЕ торговый сигнал · рынок не предсказывается · 18+")


def _levels(ob: dict, key_full: str, key_top: str) -> list:
    """Уровни стороны: полные levels_* (v3.7), фолбэк — top_* (10 уровней)."""
    lv = ob.get(key_full) or ob.get(key_top) or []
    out = []
    for r in lv:
        try:
            p, q = float(r["p"]), float(r["q"])
        except (KeyError, TypeError, ValueError):
            continue
        if p > 0 and q >= 0:
            out.append({"p": p, "q": q})
    return out


def _median(xs: list) -> float:
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return float(s[n // 2]) if n % 2 else float((s[n // 2 - 1] + s[n // 2]) / 2.0)


def _side_scan(levels: list, best: float) -> dict | None:
    """Разбор одной стороны: вакуум (окно минимальной массы), стена, плотности.
    levels — от лучшей цены вглубь (как отдаёт Tinkoff). None — мало уровней."""
    if len(levels) < MIN_LEVELS:
        return None
    qs = [r["q"] for r in levels]
    n = len(qs)
    w = min(VAC_WIN, n)
    sums = [sum(qs[i:i + w]) for i in range(n - w + 1)]
    mean_s = sum(sums) / len(sums)
    if mean_s <= 0:
        return None
    i_min = min(range(len(sums)), key=lambda i: (sums[i], i))
    depth = max(0.0, 1.0 - sums[i_min] / mean_s)          # 0..1: разрежение
    p_from, p_to = levels[i_min]["p"], levels[i_min + w - 1]["p"]
    dist_lvl = i_min                                       # близость к цене
    dist_bps = abs(p_from - best) / best * 1e4 if best else None
    med = _median(qs)
    wall = None
    i_wall = max(range(n), key=lambda i: (qs[i], -i))
    if med > 0 and qs[i_wall] >= WALL_MULT * med:
        wall = {"p": levels[i_wall]["p"], "q": qs[i_wall],
                "mult": round(qs[i_wall] / med, 1)}
    return {"vacuum": {"p_from": p_from, "p_to": p_to,
                       "depth": round(depth, 3),
                       "dist_levels": dist_lvl,
                       "dist_bps": round(dist_bps, 1) if dist_bps is not None else None,
                       "sum_q": sums[i_min]},
            "wall": wall, "median_q": med, "levels_n": n}


def analyze(ob: dict | None, tape: dict | None = None) -> dict:
    """Вакуум в стакане: разрежения, стены, направление тяги (имплозии).
    Нет стакана / мало уровней → available=False + честный note (NO DUMMIES)."""
    if not isinstance(ob, dict) or not ob:
        return {"available": False,
                "note": "стакан недоступен — вакуум не считается (NO DUMMIES)",
                "verdict": VERDICT_FRAME}
    bids = _levels(ob, "levels_bid", "top_bids")
    asks = _levels(ob, "levels_ask", "top_asks")
    best_bid = float(ob.get("best_bid") or (bids[0]["p"] if bids else 0.0))
    best_ask = float(ob.get("best_ask") or (asks[0]["p"] if asks else 0.0))
    dn = _side_scan(bids, best_bid)     # разрежение ПОД ценой
    up = _side_scan(asks, best_ask)     # разрежение НАД ценой
    if dn is None and up is None:
        return {"available": False,
                "note": (f"уровней мало (bid={len(bids)}, ask={len(asks)}, "
                         f"нужно ≥{MIN_LEVELS}) — вакуум честно не считается"),
                "verdict": VERDICT_FRAME}
    out = {"available": True,
           "vacuum_up": up["vacuum"] if up else None,
           "vacuum_down": dn["vacuum"] if dn else None,
           "wall_ask": up["wall"] if up else None,
           "wall_bid": dn["wall"] if dn else None,
           "levels_used": {"bid": dn["levels_n"] if dn else len(bids),
                           "ask": up["levels_n"] if up else len(asks)},
           "note": None, "verdict": VERDICT_FRAME}
    if (up and up["levels_n"] <= 10) or (dn and dn["levels_n"] <= 10):
        out["note"] = ("стакан пришёл топ-10 уровней (старый формат) — вакуум "
                       "посчитан по видимой части, полные 50 уровней точнее")
    # ── тяга: цену всасывает в более глубокое И близкое разрежение ──
    def _score(side: dict | None) -> float:
        if not side or not side["vacuum"]:
            return 0.0
        v = side["vacuum"]
        return v["depth"] / (1.0 + v["dist_levels"] / float(VAC_WIN))
    s_up, s_dn = _score(up), _score(dn)
    if abs(s_up - s_dn) < PULL_GAP:
        pull = {"side": None, "score_up": round(s_up, 3),
                "score_dn": round(s_dn, 3),
                "word": "разрежения симметричны — выраженной тяги нет"}
    elif s_up > s_dn:
        pull = {"side": "вверх", "score_up": round(s_up, 3),
                "score_dn": round(s_dn, 3),
                "word": "тяга ВВЕРХ: имплозия в разрежение над ценой"}
    else:
        pull = {"side": "вниз", "score_up": round(s_up, 3),
                "score_dn": round(s_dn, 3),
                "word": "тяга ВНИЗ: имплозия в разрежение под ценой"}
    out["pull"] = pull
    # ── лента: кто продавливает (агрессор) ──
    ar = (tape or {}).get("aggressor_ratio") if isinstance(tape, dict) else None
    if ar is not None:
        who = ("агрессор — покупатель" if ar > 0.55 else
               "агрессор — продавец" if ar < 0.45 else "агрессия сбалансирована")
        agree = None
        if pull["side"] == "вверх":
            agree = bool(ar > 0.55)
        elif pull["side"] == "вниз":
            agree = bool(ar < 0.45)
        out["aggressor"] = {"ratio": ar, "word": who, "with_pull": agree}
    else:
        out["aggressor"] = None
    return out


def render_for_ai(m: dict | None) -> str:
    """Компактный блок Майи для досье/ИИ."""
    if not isinstance(m, dict) or not m:
        return ""
    L = []
    if not m.get("available"):
        L.append("вакуум: " + (m.get("note") or "нет данных"))
        L.append(m.get("verdict", VERDICT_FRAME))
        return "\n".join(L)
    vu, vd = m.get("vacuum_up"), m.get("vacuum_down")
    if vu:
        L.append(f"ВАКУУМ над ценой (ask): {vu['p_from']}…{vu['p_to']} "
                 f"глубина={vu['depth']:g} (масса окна {vu['sum_q']:g}, "
                 f"{vu['dist_levels']} уровней от лучшей)")
    if vd:
        L.append(f"ВАКУУМ под ценой (bid): {vd['p_from']}…{vd['p_to']} "
                 f"глубина={vd['depth']:g} (масса окна {vd['sum_q']:g}, "
                 f"{vd['dist_levels']} уровней от лучшей)")
    wa, wb = m.get("wall_ask"), m.get("wall_bid")
    if wa:
        L.append(f"СТЕНА ask: {wa['q']:g} лотов @ {wa['p']} (×{wa['mult']:g} медианы)")
    if wb:
        L.append(f"СТЕНА bid: {wb['q']:g} лотов @ {wb['p']} (×{wb['mult']:g} медианы)")
    p = m.get("pull") or {}
    if p:
        L.append(f"ТЯГА: {p.get('word', '—')} "
                 f"(вверх {p.get('score_up')}, вниз {p.get('score_dn')})")
    ag = m.get("aggressor")
    if ag:
        L.append(f"ЛЕНТА: {ag['word']} (ratio={ag['ratio']:g})"
                 + ("" if ag.get("with_pull") is None else
                    " — продавливает В разрежение" if ag["with_pull"]
                    else " — против тяги стакана"))
    if m.get("note"):
        L.append("прим.: " + m["note"])
    L.append(m.get("verdict", VERDICT_FRAME))
    return "\n".join(L)


# ══════════════════════════════════════════════════════════════════════
# SELF-TEST — синтетический стакан из литералов (без сети, NO DUMMIES)
# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # стакан: 50 уровней с шагом 0.1; в ask на уровнях 10..14 выложен РАЗРЫВ
    # (q=1 вместо 100) — вакуум; в bid на уровне 5 выложена СТЕНА (q=1000)
    bids = [{"p": round(100.0 - 0.1 * i, 1),
             "q": 1000 if i == 5 else 100} for i in range(50)]
    asks = [{"p": round(100.1 + 0.1 * i, 1),
             "q": 1 if 10 <= i <= 14 else 100} for i in range(50)]
    ob = {"best_bid": 100.0, "best_ask": 100.1,
          "levels_bid": bids, "levels_ask": asks}
    m = analyze(ob, {"aggressor_ratio": 0.62})
    assert m["available"] is True
    # вакуум найден ТАМ, где выложен разрыв: окно 10..14 → цены 101.1…101.5
    vu = m["vacuum_up"]
    assert vu and abs(vu["p_from"] - 101.1) < 1e-9 and abs(vu["p_to"] - 101.5) < 1e-9
    assert vu["dist_levels"] == 10 and vu["sum_q"] == 5
    assert vu["depth"] > 0.9                       # разрежение почти полное
    # стена найдена ТАМ, где выложена: bid уровень 5 → цена 99.5
    wb = m["wall_bid"]
    assert wb and abs(wb["p"] - 99.5) < 1e-9 and wb["q"] == 1000
    assert m["wall_ask"] is None                   # в ask стены не выкладывали
    # bid-сторона ровная — вакуум мелкий, тяга ВВЕРХ (имплозия в разрыв ask)
    assert m["vacuum_down"]["depth"] < vu["depth"]
    assert m["pull"]["side"] == "вверх" and m["pull"]["score_up"] > m["pull"]["score_dn"]
    # лента согласована: агрессор-покупатель продавливает в разрежение
    assert m["aggressor"]["word"] == "агрессор — покупатель"
    assert m["aggressor"]["with_pull"] is True
    # зеркальный случай: разрыв в bid → тяга ВНИЗ; агрессор-продавец согласен
    bids2 = [{"p": round(100.0 - 0.1 * i, 1),
              "q": 1 if 3 <= i <= 7 else 100} for i in range(50)]
    asks2 = [{"p": round(100.1 + 0.1 * i, 1), "q": 100} for i in range(50)]
    m2 = analyze({"best_bid": 100.0, "best_ask": 100.1,
                  "levels_bid": bids2, "levels_ask": asks2},
                 {"aggressor_ratio": 0.30})
    assert m2["pull"]["side"] == "вниз"
    assert abs(m2["vacuum_down"]["p_from"] - 99.7) < 1e-9
    assert m2["aggressor"]["with_pull"] is True
    # ровный стакан без разрывов и стен → тяги нет (равновесие)
    flat = analyze({"best_bid": 100.0, "best_ask": 100.1,
                    "levels_bid": [{"p": 100.0 - 0.1 * i, "q": 100} for i in range(50)],
                    "levels_ask": [{"p": 100.1 + 0.1 * i, "q": 100} for i in range(50)]})
    assert flat["pull"]["side"] is None and flat["wall_bid"] is None
    # NO DUMMIES: нет стакана / мало уровней → честный отказ, не заглушка
    e1 = analyze(None)
    assert e1["available"] is False and "NO DUMMIES" in e1["note"]
    e2 = analyze({"top_bids": [{"p": 100.0, "q": 5}],
                  "top_asks": [{"p": 100.1, "q": 5}]})
    assert e2["available"] is False and "уровней мало" in e2["note"]
    # фолбэк на top_* (старый формат 10 уровней) работает и честно помечен
    ob10 = {"best_bid": 100.0, "best_ask": 100.1,
            "top_bids": [{"p": round(100.0 - 0.1 * i, 1), "q": 100} for i in range(10)],
            "top_asks": [{"p": round(100.1 + 0.1 * i, 1),
                          "q": 1 if 5 <= i <= 9 else 100} for i in range(10)]}
    m10 = analyze(ob10)
    assert m10["available"] is True and "топ-10" in (m10["note"] or "")
    assert m10["pull"]["side"] == "вверх"
    # рендер: рамка ⚫/18+ всегда, торговых слов нет, вырожденные входы не падают
    for blob in (m, m2, flat, e1, e2, m10):
        txt = render_for_ai(blob)
        assert "18+" in txt and "⚫" in txt
        low = txt.lower()
        for bad in ("покупай", "продавай", "лонг", "шорт", "buy", "sell"):
            assert bad not in low, bad
    assert render_for_ai(None) == "" and render_for_ai({}) == ""
    # детерминизм байт-в-байт
    assert repr(analyze(ob, {"aggressor_ratio": 0.62})) == repr(m)
    print("maya: self-test пройден ✓ (вакуум/стена/тяга находятся там, "
          "где выложены; отказы честные)")
