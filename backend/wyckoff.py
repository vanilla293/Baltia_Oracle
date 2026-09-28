"""ОРАКУЛ // ПИФИЯ — метод Вайкоффа (структура рынка + VSA).

Локальный расчёт по свечам (никаких внешних запросов):
  • торговый диапазон (бокс) — расширяющееся сканирование: creek (верх) / ice (низ);
  • события Вайкоффа в хронологии: SC/BC (кульминации), AR (авто-ралли/реакция),
    ST (вторичный тест), SPRING/UPTHRUST (ложные проколы), SOS/SOW (знак силы/слабости),
    LPS/LPSY (последняя точка поддержки/предложения), пробои;
  • VSA-знаки: НЕТ СПРОСА / НЕТ ПРЕДЛОЖЕНИЯ / ОСТАНАВЛИВАЮЩИЙ ОБЪЁМ / АБСОРБЦИЯ
    (усилие без результата);
  • фаза (A–E, накопление/распределение) и bias-счёт −100…+100 с временным затуханием;
  • характер объёма: спрос vs предложение на росте/падении.

Выход: dict для UI/графика + render_for_ai() для думающих стадий конвейера.
Это структурный расчёт-фильтр, как и астро: подсказка ИИ и оператору, не сигнал сам по себе.
"""
from __future__ import annotations


# ── русские имена событий (UI) ────────────────────────────────────────
EVENT_RU = {
    "SC": "КУЛЬМИНАЦИЯ ПРОДАЖ", "BC": "КУЛЬМИНАЦИЯ ПОКУПОК",
    "AR": "АВТО-РАЛЛИ", "ARD": "АВТО-РЕАКЦИЯ",
    "ST": "ВТОРИЧНЫЙ ТЕСТ", "STD": "ВТОРИЧНЫЙ ТЕСТ (верх)",
    "SPRING": "СПРИНГ", "UT": "АПТРАСТ",
    "SOS": "ЗНАК СИЛЫ (SOS)", "SOW": "ЗНАК СЛАБОСТИ (SOW)",
    "LPS": "ПОСЛЕДНЯЯ ОПОРА (LPS)", "LPSY": "ПОСЛЕДНЕЕ ПРЕДЛОЖЕНИЕ (LPSY)",
    "BREAKOUT": "ПРОБОЙ ВВЕРХ", "BREAKDOWN": "ПРОБОЙ ВНИЗ",
    "NO_DEMAND": "НЕТ СПРОСА", "NO_SUPPLY": "НЕТ ПРЕДЛОЖЕНИЯ",
    "STOP_VOL": "ОСТАНАВЛИВАЮЩИЙ ОБЪЁМ", "ABSORB_UP": "АБСОРБЦИЯ ПОКУПОК",
    "ABSORB_DN": "АБСОРБЦИЯ ПРОДАЖ",
}
# вес события в bias (плюс = за покупателя/накопление)
_W = {
    "SC": +14, "BC": -14, "AR": +5, "ARD": -5, "ST": +7, "STD": -7,
    "SPRING": +26, "UT": -26, "SOS": +20, "SOW": -20, "LPS": +15, "LPSY": -15,
    "BREAKOUT": +12, "BREAKDOWN": -12,
    "NO_DEMAND": -6, "NO_SUPPLY": +6, "STOP_VOL": +11,
    "ABSORB_UP": -9, "ABSORB_DN": +9,
}
_BULL = {k for k, v in _W.items() if v > 0}


def _atr_series(cnd: list[dict], period: int = 14) -> list[float]:
    """ATR на каждый бар по ПРЕДЫДУЩИМ барам (сам бар не входит в среднее —
    иначе залповый бар кульминации глушит собственный сигнал знаменателем)."""
    trs = [0.0]
    for i in range(1, len(cnd)):
        h, l, pc = cnd[i]["h"], cnd[i]["l"], cnd[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    out = []
    for i in range(len(cnd)):
        w = trs[max(0, i - period): i]        # окно заканчивается на i-1
        out.append(sum(w) / len(w) if w else (trs[i] or 0.0))
    return out


def _avg_vol_series(cnd: list[dict], period: int = 20) -> list[float]:
    """Средний объём по ПРЕДЫДУЩИМ барам (без текущего) — залп виден как залп."""
    out = []
    for i in range(len(cnd)):
        w = [c["v"] for c in cnd[max(0, i - period): i]]
        out.append(sum(w) / len(w) if w else float(cnd[i]["v"] or 0))
    return out


def analyze(candles: list[dict], tf: str = "D1", window: int = 55) -> dict:
    """Разбор Вайкоффа по одной серии свечей [{t,o,h,l,c,v}] (хронологический)."""
    cnd = [c for c in (candles or []) if c.get("h") and c.get("l")]
    if len(cnd) < 25:
        return {}
    cnd = cnd[-window:]
    n = len(cnd)
    atr = _atr_series(cnd)
    avol = _avg_vol_series(cnd)

    warm = min(10, n - 5)                    # первые бары — установка бокса
    box_hi = max(c["h"] for c in cnd[:warm])
    box_lo = min(c["l"] for c in cnd[:warm])

    # Закреплён ли бокс. Окно часто входит В СЕРЕДИНЕ тренда: жёсткий бокс от
    # первых баров тогда ядовит (лёд/крик застревают на старых экстремумах —
    # SC читается как пробой, SOS/UT у реальных краёв не срабатывают никогда).
    # Правило Вайкоффа: диапазон рождается КУЛЬМИНАЦИЕЙ (SC/BC) и авто-реакцией.
    # До кульминации бокс СКОЛЬЗИТ за ценой; плоский прогрев закрепляет сразу.
    warm_net = abs(cnd[warm - 1]["c"] - cnd[0]["o"])
    warm_rng = box_hi - box_lo
    anchored = warm_rng > 0 and warm_net < 0.45 * warm_rng   # плоский старт → бокс валиден

    events: list[dict] = []
    sc_i = bc_i = None
    sos_i = sow_i = None
    broke_up = broke_dn = False

    def ev(i, typ, price, note=""):
        events.append({"i": i, "t": cnd[i].get("t"), "type": typ,
                       "name": EVENT_RU.get(typ, typ),
                       "price": round(float(price), 6), "note": note,
                       "bull": typ in _BULL})

    for i in range(warm, n):
        c = cnd[i]
        a = atr[i] or 1e-9
        av = avol[i] or 1e-9
        spread = c["h"] - c["l"]
        vr = c["v"] / av                      # объём к среднему ПРЕДЫДУЩИХ
        sr = spread / a                       # размах к ATR ПРЕДЫДУЩИХ
        prev_c = cnd[i - 1]["c"]
        upbar = c["c"] > prev_c
        dnbar = c["c"] < prev_c
        cpos = (c["c"] - c["l"]) / spread if spread > 0 else 0.5

        if not anchored:
            # бокс скользит за трендом: диапазон последних warm баров
            box_hi = max(x["h"] for x in cnd[max(0, i - warm + 1): i + 1])
            box_lo = min(x["l"] for x in cnd[max(0, i - warm + 1): i + 1])

        # ── кульминации (широкий бар + залповый объём у края) ─────────
        # Классика: широкий размах, кульминационный объём, закрытие ЗАМЕТНО
        # ОТБИТО от экстремума (отказ внутри бара), а не «у льда ±0.05 ATR».
        climaxed = False
        if vr > 2.1 and sr > 1.7:
            near_lo = c["l"] <= box_lo + 0.35 * a
            near_hi = c["h"] >= box_hi - 0.35 * a
            if dnbar and near_lo and sc_i is None and cpos >= 0.28:
                sc_i = i
                climaxed = True
                anchored = True
                ev(i, "SC", c["l"], f"объём ×{vr:.1f}, размах ×{sr:.1f} — паника слита в чьи-то руки")
                box_lo = c["l"]                 # низ SC задаёт лёд диапазона
                # крик перезакрепляется от локального свинга, а не от древнего
                # максимума тренда (AR затем поднимет его по факту откупа)
                box_hi = max(x["h"] for x in cnd[max(0, i - 2): i + 1])
            elif upbar and near_hi and bc_i is None and cpos <= 0.72:
                bc_i = i
                climaxed = True
                anchored = True
                ev(i, "BC", c["h"], f"объём ×{vr:.1f} — эйфория выкуплена продавцом")
                box_hi = c["h"]                 # верх BC задаёт крик диапазона
                box_lo = min(x["l"] for x in cnd[max(0, i - 2): i + 1])

        # ── авто-ралли / авто-реакция и вторичные тесты ────────────────
        if sc_i is not None and i - sc_i <= 6 and not any(e["type"] == "AR" for e in events):
            if c["c"] - cnd[sc_i]["l"] >= 1.6 * a and upbar:
                ev(i, "AR", c["c"], "первый откуп после кульминации — верх бокса намечен")
                box_hi = max(x["h"] for x in cnd[sc_i:i + 1])   # верх AR = крик
        if sc_i is not None and i - sc_i >= 3 and not any(e["type"] == "ST" for e in events):
            if abs(c["l"] - cnd[sc_i]["l"]) <= 0.5 * a and c["v"] < 0.75 * cnd[sc_i]["v"]:
                ev(i, "ST", c["l"], "ретест дна на сухом объёме — предложение иссякло")
        if bc_i is not None and i - bc_i <= 6 and not any(e["type"] == "ARD" for e in events):
            if cnd[bc_i]["h"] - c["c"] >= 1.6 * a and dnbar:
                ev(i, "ARD", c["c"], "первый сброс после эйфории — низ бокса намечен")
                box_lo = min(x["l"] for x in cnd[bc_i:i + 1])   # низ AR = лёд
        if bc_i is not None and i - bc_i >= 3 and not any(e["type"] == "STD" for e in events):
            if abs(c["h"] - cnd[bc_i]["h"]) <= 0.5 * a and c["v"] < 0.75 * cnd[bc_i]["v"]:
                ev(i, "STD", c["h"], "ретест вершины на сухом объёме — спрос иссяк")

        # ── проколы бокса: спринг / аптраст / пробои (только закреплённый
        #    бокс: на скользящем это был бы спам по всему тренду) ─────────
        if anchored and not climaxed and c["l"] < box_lo - 0.05 * a:
            swept_lo = (i >= 9 and not broke_dn
                        and c["l"] < min(x["l"] for x in cnd[i - 8: i]) - 1e-9)
            if c["c"] > box_lo and (c["l"] < box_lo - 0.15 * a or swept_lo):
                q = "чистый (низкий объём)" if vr < 1.1 else ("с абсорбцией" if vr > 1.8 else "рабочий")
                ev(i, "SPRING", c["l"], f"вынос стопов под лёд и возврат — {q}; топливо для роста")
                box_lo = min(box_lo, c["l"])
            elif c["c"] <= box_lo and sr > 1.25 and vr > 1.35:
                ev(i, "SOW", c["c"], f"закрытие под боксом на расширении (объём ×{vr:.1f}) — знак слабости")
                sow_i = i; broke_dn = True; broke_up = False
                box_lo = c["l"]
            else:
                box_lo = c["l"]
        if anchored and not climaxed and c["h"] > box_hi + 0.05 * a:
            swept_hi = (i >= 9 and not broke_up
                        and c["h"] > max(x["h"] for x in cnd[i - 8: i]) + 1e-9)
            if c["c"] < box_hi and (c["h"] > box_hi + 0.15 * a or swept_hi):
                q = "классический" if vr > 1.3 else "слабый"
                ev(i, "UT", c["h"], f"прокол крика и возврат — {q} аптраст; ловушка для покупателя")
                box_hi = max(box_hi, c["h"])
            elif c["c"] >= box_hi and sr > 1.25 and vr > 1.35:
                ev(i, "SOS", c["c"], f"закрытие над боксом на расширении (объём ×{vr:.1f}) — знак силы")
                sos_i = i; broke_up = True; broke_dn = False
                box_hi = c["h"]
            else:
                box_hi = c["h"]

        # ── LPS / LPSY (ретест пробитого края) ─────────────────────────
        if sos_i is not None and i > sos_i and not any(e["type"] == "LPS" for e in events):
            if dnbar and c["l"] > box_lo and abs(c["l"] - box_hi) <= 0.8 * a and c["v"] < av:
                ev(i, "LPS", c["l"], "откат к бывшему сопротивлению держится на малом объёме")
        if sow_i is not None and i > sow_i and not any(e["type"] == "LPSY" for e in events):
            if upbar and abs(c["h"] - box_lo) <= 0.8 * a and c["v"] < av:
                ev(i, "LPSY", c["h"], "слабый возврат к пробитой поддержке — предложение сверху")

        # ── VSA-знаки ──────────────────────────────────────────────────
        if climaxed:
            continue
        if i >= 2:
            v1, v2 = cnd[i - 1]["v"], cnd[i - 2]["v"]
            if upbar and sr < 0.62 and c["v"] < v1 and c["v"] < v2:
                ev(i, "NO_DEMAND", c["c"], "рост на узком баре без объёма — покупателя нет")
            if dnbar and sr < 0.62 and c["v"] < v1 and c["v"] < v2:
                ev(i, "NO_SUPPLY", c["c"], "падение без объёма — продавец выдохся")
        if dnbar and vr > 2.0 and cpos > 0.62:
            ev(i, "STOP_VOL", c["l"], f"залповый объём ×{vr:.1f}, закрытие сверху — падение остановлено покупкой")
        if vr > 1.8 and abs(c["c"] - prev_c) < 0.25 * a and sr > 0.8:
            if cpos < 0.4:
                ev(i, "ABSORB_UP", c["h"], "усилие вверх без результата — покупки поглощаются (раздача?)")
            elif cpos > 0.6:
                ev(i, "ABSORB_DN", c["l"], "усилие вниз без результата — продажи поглощаются (набор?)")

    last = cnd[-1]
    a_last = atr[-1] or 1e-9
    box_h = box_hi - box_lo
    pos = (last["c"] - box_lo) / box_h if box_h > 0 else 0.5

    # ── bias с временным затуханием ────────────────────────────────────
    bias = 0.0
    for e in events:
        bias += _W.get(e["type"], 0) * (0.93 ** (n - 1 - e["i"]))
    bias = max(-100, min(100, round(bias)))

    # ── характер объёма: рост vs падение (последние 15 баров) ──────────
    tailn = min(15, n - 1)
    uv = [cnd[i]["v"] for i in range(n - tailn, n) if cnd[i]["c"] > cnd[i - 1]["c"]]
    dv = [cnd[i]["v"] for i in range(n - tailn, n) if cnd[i]["c"] < cnd[i - 1]["c"]]
    up_avg = sum(uv) / len(uv) if uv else 0.0
    dn_avg = sum(dv) / len(dv) if dv else 0.0
    vol_char = None
    if up_avg and dn_avg:
        r = up_avg / dn_avg
        vol_char = (f"объём на росте ×{r:.2f} к объёму на падении — "
                    + ("спрос доминирует" if r > 1.15 else
                       "предложение доминирует" if r < 0.87 else "паритет"))

    # ── фаза ───────────────────────────────────────────────────────────
    recent = {e["type"]: (n - 1 - e["i"]) for e in events}  # тип → баров назад (посл.)
    trend_box = box_h > 7.5 * a_last
    held_up = broke_up and last["c"] > box_hi - 0.2 * a_last
    held_dn = broke_dn and last["c"] < box_lo + 0.2 * a_last
    if not anchored:
        # кульминации не было, прогрев трендовый → диапазона нет: читаем
        # тренд по чистому сносу за окно (скользящий бокс его маскирует)
        drift = last["c"] - cnd[0]["c"]
        phase = "тренд (диапазон не сформирован)"
        if abs(drift) > 4 * a_last:
            read = "МАРК-АП" if drift > 0 else "МАРК-ДАУН"
        else:
            read = "НЕЙТРАЛЬНО"
    elif held_up:
        phase, read = "D/E — марк-ап (выход из диапазона вверх)", "НАКОПЛЕНИЕ ЗАВЕРШЕНО"
    elif held_dn:
        phase, read = "D/E — марк-даун (выход из диапазона вниз)", "РАСПРЕДЕЛЕНИЕ ЗАВЕРШЕНО"
    elif recent.get("SPRING", 99) <= 10:
        phase, read = "C — спринг (тест предложения)", "НАКОПЛЕНИЕ"
    elif recent.get("UT", 99) <= 10:
        phase, read = "C — аптраст (тест спроса)", "РАСПРЕДЕЛЕНИЕ"
    elif ("SC" in recent or "BC" in recent) and 0.02 < pos < 0.98:
        if bias >= 15:
            phase, read = "B — построение причины", "СКЛОНЯЕТСЯ К НАКОПЛЕНИЮ"
        elif bias <= -15:
            phase, read = "B — построение причины", "СКЛОНЯЕТСЯ К РАСПРЕДЕЛЕНИЮ"
        else:
            phase, read = "B — построение причины", "ХАРАКТЕР НЕ ОПРЕДЕЛЁН"
    elif trend_box:
        up = last["c"] > cnd[0]["c"]
        phase = "тренд (диапазон не сформирован)"
        read = "МАРК-АП" if up else "МАРК-ДАУН"
    else:
        phase = "A/B — диапазон"
        read = ("СКЛОНЯЕТСЯ К НАКОПЛЕНИЮ" if bias >= 15 else
                "СКЛОНЯЕТСЯ К РАСПРЕДЕЛЕНИЮ" if bias <= -15 else "НЕЙТРАЛЬНО")

    # причина → следствие (ширина причины в ATR ≈ потенциал хода)
    cause = round(box_h / a_last, 1) if a_last else None

    ev_out = [{k: e[k] for k in ("t", "type", "name", "price", "note", "bull")}
              for e in events]
    return {
        "tf": tf, "bars": n,
        "box_high": round(box_hi, 6), "box_low": round(box_lo, 6),
        "box_atr": cause, "pos_in_box": round(pos, 2),
        "phase": phase, "read": read, "bias": bias,
        "vol_character": vol_char,
        "events": ev_out[-10:],
        "events_all": ev_out,
        "last_close": last["c"],
    }


def analyze_dossier(dossier: dict) -> dict:
    """Два таймфрейма из уже собранных свечей досье: D1 (структура) + H1 (интрадей)."""
    out: dict = {}
    daily = dossier.get("candles_daily_tail") or []
    hourly = dossier.get("candles_hourly") or []
    d = analyze(daily, tf="D1", window=55)
    if d:
        out["daily"] = d
    h = analyze(hourly, tf="H1", window=60)
    if h:
        out["hourly"] = h
    if d:
        out["summary"] = summary_line(d)
    return out


def summary_line(w: dict) -> str:
    if not w:
        return ""
    return (f"ВАЙКОФФ {w['tf']}: {w['read']} · фаза {w['phase']} · bias {w['bias']:+d} · "
            f"бокс {w['box_low']}…{w['box_high']} (поз. {int(w['pos_in_box']*100)}%)")


def _render_one(w: dict) -> str:
    L = [f"[{w['tf']}] {w['read']} — фаза: {w['phase']}. Bias {w['bias']:+d} (−100 медведи … +100 быки).",
         f"  Бокс (диапазон): низ (ice) {w['box_low']} … верх (creek) {w['box_high']}; "
         f"цена в боксе на {int(w['pos_in_box']*100)}%; ширина причины ≈ {w['box_atr']} ATR "
         f"(закон причина→следствие: чем шире причина, тем длиннее ход из неё)."]
    if w.get("vol_character"):
        L.append(f"  Усилие/результат: {w['vol_character']}.")
    if w.get("events"):
        L.append("  События (хронология, свежие в конце):")
        for e in w["events"]:
            t = str(e.get("t") or "")[:10]
            L.append(f"    {t} · {e['name']} @ {e['price']} — {e['note']}")
    return "\n".join(L)


def render_for_ai(wy: dict) -> str:
    """Текстовый блок для думающих стадий."""
    if not wy or not wy.get("daily"):
        return ""
    L = ["РАСЧЁТ ПО МЕТОДУ ВАЙКОФФА (структура + VSA, локально из свечей):",
         _render_one(wy["daily"])]
    if wy.get("hourly"):
        L.append(_render_one(wy["hourly"]))
    L.append(
        "  ПРИМЕНЕНИЕ: три закона Вайкоффа — спрос/предложение, причина→следствие, "
        "усилие→результат. Composite Man строит причину в боксе (накопление/распределение), "
        "тестирует её спрингом/аптрастом (снятие стопов = топливо), и ведёт цену из бокса "
        "(SOS/LPS вверх, SOW/LPSY вниз). Сверяй события с лентой/стаканом: спринг + дельта "
        "покупателя + стена bid = сильный лонг-сетап; аптраст + агрессор-продавец = раздача. "
        "Это структурный фильтр, вес имеет совпадение с кухней и таймингом.")
    return "\n".join(L)
