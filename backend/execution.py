# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — СНАЙПЕР (умное исполнение, спринт «Сырая дата» №3).

Приказ владельца: «если бот ударит рыночным ордером всем объёмом в момент
бифуркации, спред порвёт его на британский флаг. Мимикрируй под смарт-мани:
теневые лимитки, TWAP/айсберг-нарезка, лови ММ на встречных сбросах».

Устройство (чистая математика плана — тестируется без сети; исполнение
ведёт автопилот через брокера, по траншу за раз):

  · НАРЕЗКА (iceberg slicing): объём режется на транши; размер транша
    ограничен УЧАСТИЕМ — не больше PART_CAP видимой ликвидности ближней
    стороны стакана (не двигаем цену собственным входом);
  · ТЕНЕВАЯ ЛИМИТКА (shadow limit): пассивный вход НА своей стороне спреда
    (long → у best_bid) — не платим спред, не светим агрессию; таймаут
    SHADOW_TTL, цена ушла — перевзвод;
  · ЛОВЛЯ СБРОСА (liquidity grab): лимитка-капкан ГЛУБЖЕ рынка на уровне
    недавнего выноса — ММ, снимающий стопы толпы, сам наливает нам по
    лучшей цене (механика «Капкана» из txt владельца);
  · ЭСКАЛАЦИЯ («Абсолют»: рыночные ВХОДЫ запрещены): сингулярность после
    DEADLINE перевзводит тень у свежего best (мейкер, спред не платим);
    в обычном парламенте недобор просто снимается. Рыночным бот может
    только ВЫХОДИТЬ (ликвидация, Time_Killswitch, ПАНИКА).

⚫ Снайпер — тактика исполнения, не сигнал. Проскальзывание и спред — цена
реальности; модуль её минимизирует, а не отменяет. 18+.

Self-тест: python3 -m backend.execution
"""
from __future__ import annotations

import math

# ── константы (единственное место) ──────────────────────────────────────────
PART_CAP = 0.25        # транш ≤ 25% видимой ближней ликвидности
MAX_TRANCHES = 6       # больше — уже размазня, не вход
SHADOW_TTL = 25.0      # секунд жизни теневой лимитки до перевзвода
GRAB_LOOKBACK = 120    # тиков истории для уровня недавнего выноса
GRAB_FRAC = 0.35       # доля объёма в капкан на сбросе (если вынос виден)
DEADLINE_SING = 90.0   # сингулярность: добор рыночным после (сек)
DEADLINE_NORM = 240.0  # парламент: недобор снимается после (сек)
URGENT_STATES = ("СИНГУЛЯРНОСТЬ",)
# ── солитонный выход (Проект PREDATOR, Агент 4) ─────────────────────────────
# Приказ владельца: «динамический выход перед бетонной стеной по уравнению
# солитона». Импульс цены после срыва — уединённая волна: разгон, гребень,
# затухание об стену ликвидности (плиту). Огибающая солитона A·sech²((x−x₀)/w)
# 🟡 — метафора формы импульса (не физический закон рынка): пик проходим, за
# ним волна рассыпается — выходим ДО стены и ДО распада гребня, не на пике жад-
# ности. WALL_MARGIN — насколько раньше стены фиксируем; CREST_DECAY — доля
# спада гребня от пика, после которой волна признана рассыпающейся.
WALL_MARGIN = 0.20     # выходим, не дойдя до стены долю расстояния «вход→стена»
CREST_DECAY = 0.30     # спад скорости гребня от пика ≥ 30% → волна распадается
SECH2_EXIT = 0.55      # огибающая sech² упала ниже этой доли пика → выход
# ── Фибоначчи-сетка выхода (PREDATOR: рассеивание footprint) ────────────────
# Вместо ОДНОГО тейк-профита (круглое число — приманка для стоп-хантера и
# жирный след в стакане) выход рассеивается сеткой лимиток на уровнях
# золотого сечения от базовой единицы хода: 0.382 · 0.618 · 1.618.
# Объём по уровням делится тоже золотым сечением (веса ∝ φ⁻ᵏ), и спираль
# ДЫШИТ средой (дуальная механика oracle.dual_env):
#   · щёлочь (mean-reversion, α≥3) — СЖИМАЮЩАЯСЯ спираль: больший объём на
#     ближних уровнях (ход короткий, забираем раньше);
#   · кислота (breakout, α<2)     — РАСШИРЯЮЩАЯСЯ: больший объём на дальнем
#     1.618 (импульс с тяжёлым хвостом кормит дальний уровень).
# 🟡 Уровни Фибо — конвенция, не закон рынка: их польза здесь — рассеивание
# объёма по НЕкруглым ценам (меньше footprint), а не магия числа.
PHI = (1.0 + 5.0 ** 0.5) / 2.0             # 1.6180339…
FIB_LEVELS = (0.382, 0.618, 1.618)         # доли базовой единицы хода
_FIB_W_RAW = (1.0, 1.0 / PHI, 1.0 / PHI ** 2)    # 1 : 0.618 : 0.382

FRAME = ("⚫ Снайпер — тактика исполнения, не сигнал: минимизирует спред и "
         "воздействие, не отменяет их. 18+")


def _f(x, d=0.0):
    try:
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


def near_liquidity(book: dict | None, side: str, levels: int = 5) -> float:
    """Видимая ликвидность ближней стороны: long ест ask, short ест bid."""
    if not isinstance(book, dict):
        return 0.0
    rows = (book.get("levels_ask") if side == "long"
            else book.get("levels_bid")) or []
    return float(sum(_f(r.get("q")) for r in rows[:levels]))


def grab_level(prices, side: str) -> float | None:
    """Уровень недавнего ВЫНОСА: экстремум последних GRAB_LOOKBACK тиков,
    который рынок уже покинул (сброс случился — капкан ставится туда,
    куда ММ повторит поход за стопами)."""
    p = [_f(x) for x in (prices or []) if x]
    if len(p) < 30:
        return None
    seg = p[-GRAB_LOOKBACK:]
    last = seg[-1]
    if side == "long":
        lo = min(seg)
        return lo if last > lo * 1.0008 else None      # вынос вниз был и ушли
    hi = max(seg)
    return hi if last < hi * 0.9992 else None


def plan_entry(side: str, total_lots: int, price: float, book: dict | None,
               *, state: str = "ПАРЛАМЕНТ", prices=None,
               tick_size: float = 0.0) -> dict:
    """План входа. Возврат: {tranches: [...], deadline_s, note}.

    Транш: {lots, kind: 'shadow'|'grab'|'market', price|None}.
    Сумма lots по траншам == total_lots всегда."""
    total = int(total_lots)
    if total <= 0 or price <= 0:
        return {"tranches": [], "deadline_s": 0.0,
                "note": "нулевой объём — плана нет", "frame": FRAME}
    urgent = state in URGENT_STATES
    liq = near_liquidity(book, side)
    # размер транша: участие ≤ PART_CAP ближней ликвидности (и не пыль)
    if liq > 0:
        slice_lots = max(1, int(liq * PART_CAP))
    else:
        slice_lots = max(1, total // 3)
    n_tr = min(MAX_TRANCHES, max(1, -(-total // slice_lots)))   # ceil
    per = [total // n_tr] * n_tr
    for i in range(total % n_tr):
        per[i] += 1
    per = [x for x in per if x > 0]

    bb = _f((book or {}).get("best_bid"))
    ba = _f((book or {}).get("best_ask"))
    tick = tick_size if tick_size > 0 else max(price * 1e-5, 0.0001)
    shadow_px = (bb if side == "long" else ba) or price
    # теневая лимитка стоит НА своей стороне (не пересекает спред)
    shadow_px = round(shadow_px + (tick if side == "long" else -tick) * 0.0, 10)
    # СПРИНТ «АБСОЛЮТ»: рыночные ВХОДЫ ЗАПРЕЩЕНЫ — оплата спреда + комиссия
    # 5 бп убивают скальп. Сингулярность заходит АГРЕССИВНОЙ ТЕНЬЮ: лимитка,
    # улучшенная на тик ВНУТРЬ спреда (мы всё ещё мейкер — спред не платим);
    # спред в один тик → встаём на свой best (внутрь некуда). Недобор при
    # убежавшей цене — принятая цена запрета: перевзвод, не рыночный удар.
    if bb > 0 and ba > bb:
        if side == "long":
            aggr_px = min(bb + tick, ba - tick)
            aggr_px = aggr_px if aggr_px > bb else bb
        else:
            aggr_px = max(ba - tick, bb + tick)
            aggr_px = aggr_px if aggr_px < ba else ba
    else:
        aggr_px = shadow_px
    aggr_px = round(aggr_px, 10)

    tranches = []
    grab = grab_level(prices, side)
    grab_lots = 0
    if grab is not None and total > 1:
        grab_lots = max(1, int(total * GRAB_FRAC))
        grab_lots = min(grab_lots, total - 1)
    rest = total - grab_lots
    # первый транш: сингулярность — агрессивная тень внутрь спреда,
    # парламент — обычная тень на своей стороне
    first = per[0] if per else rest
    first = min(first, rest)
    if first > 0:
        tranches.append({"lots": first, "kind": "shadow",
                         "price": aggr_px if urgent else shadow_px,
                         "aggressive": bool(urgent)})
        rest -= first
    # остальные — тени по мере исполнения (TWAP-темп задаёт автопилот)
    i = 1
    while rest > 0:
        take = min(per[i % len(per)] if per else rest, rest)
        tranches.append({"lots": take, "kind": "shadow", "price": shadow_px})
        rest -= take
        i += 1
    if grab_lots > 0:
        tranches.append({"lots": grab_lots, "kind": "grab",
                         "price": round(grab, 10)})
    total_planned = sum(t["lots"] for t in tranches)
    assert total_planned == total, (total_planned, total)
    return {
        "tranches": tranches,
        "deadline_s": DEADLINE_SING if urgent else DEADLINE_NORM,
        "urgent": urgent,
        "participation": (round(min(1.0, (tranches[0]["lots"] / liq)), 3)
                          if liq > 0 else None),
        "note": (f"{len(tranches)} траншей на {total} лот: "
                 + ("агрессивная тень В СПРЕД (рыночный вход запрещён), "
                    if urgent else "тень у спреда, ")
                 + (f"капкан {grab_lots} лот на сбросе @{grab}, " if grab_lots
                    else "")
                 + f"участие ≤{int(PART_CAP*100)}% ближней ликвидности"),
        "frame": FRAME,
    }


def escalate(plan: dict, started_ts: float, now_ts: float,
             filled_lots: int, total_lots: int) -> dict:
    """Судьба недобранного плана. Рыночный добор ЗАПРЕЩЁН («Абсолют»):
    сингулярность после дедлайна ПЕРЕВЗВОДИТ тень у свежего best (остаёмся
    мейкером), парламент — снимает остаток (недобор ≠ беда)."""
    left = max(0, int(total_lots) - int(filled_lots))
    if left == 0:
        return {"action": "done", "left": 0}
    age = max(0.0, now_ts - started_ts)
    if age < _f(plan.get("deadline_s"), DEADLINE_NORM):
        return {"action": "wait", "left": left, "age_s": round(age, 1)}
    if plan.get("urgent"):
        return {"action": "reprice_rest", "left": left,
                "note": (f"дедлайн сингулярности: перевзвод тени на {left} лот "
                         "у свежего best (рыночный добор запрещён)")}
    return {"action": "cancel_rest", "left": left,
            "note": f"дедлайн парламента: снимаем недобор {left} лот"}


def reprice_shadow(shadow_px: float, side: str, book: dict | None,
                   placed_ts: float, now_ts: float) -> dict:
    """Теневая лимитка: жива, пока цена у неё; TTL вышел или best ушёл —
    перевзвод на новую тень."""
    bb = _f((book or {}).get("best_bid"))
    ba = _f((book or {}).get("best_ask"))
    best = bb if side == "long" else ba
    if not best:
        return {"action": "hold"}
    moved = abs(best - shadow_px) / shadow_px if shadow_px else 1.0
    if now_ts - placed_ts >= SHADOW_TTL or moved > 0.0008:
        return {"action": "replace", "new_price": best,
                "note": f"тень устарела (сдвиг {moved*100:.2f}%) — перевзвод"}
    return {"action": "hold"}


def casimir_tensor(spread_median: float, *, alpha=None) -> dict:
    """ТЕНЗОР КАЗИМИРА 🟡 (спринт «Абсолют»): фрактальная сетка волатильности
    вместо стандартного отклонения. σ за N тиков в тяжёлых хвостах (α<2) не
    сходится — сетка масштабируется от МЕДИАННОГО СПРЕДА (он «дышит» со
    средой) степенями золотого сечения:

        W_Φ(t) = S_median(t) · [Φ⁻¹, Φ⁰, Φ¹, Φ^(2/α)]

    Уровни: 1 — микро-шум (зона абсорбции), 2 — базовое состояние,
    3 — граница упругости (щёлочь бьёт на возврат отсюда), 4 — радиус
    «квантового пузыря» (цель ультра-пробоя в кислоте).

    ЧЕСТНАЯ пометка: расширение 4-го уровня скромное — при α=1.5 показатель
    2/α=1.33, т.е. Φ^1.33/Φ¹ ≈ ×1.17 к границе упругости, не «взрыв»;
    формула реализована как задана, её предсказательная сила нулями НЕ
    проверена. α вне (0;∞) или None → показатель по дефолту α=3 (2/α=0.667,
    честно указано в note). Нет спреда → None (NO DUMMIES)."""
    s = _f(spread_median)
    if s <= 0:
        return {"levels": None, "note": "нет медианного спреда — тензор не строим"}
    a = _f(alpha, 3.0)
    a = max(1.5, min(4.0, a if a > 0 else 3.0))
    expo4 = 2.0 / a
    lv = [s / PHI, s, s * PHI, s * PHI ** expo4]
    return {"levels": [round(x, 10) for x in lv],
            "expo4": round(expo4, 3),
            "note": (f"W_Φ = S·[Φ⁻¹,1,Φ,Φ^{expo4:.2f}] 🟡 (конвенция лога, "
                     "нулями не проверена"
                     + ("; α не измерена — дефолт 3.0" if alpha is None else "")
                     + ")")}


def fib_unit(spread_median: float, *, alpha=None, vpin=None) -> dict:
    """ДИНАМИЧЕСКАЯ база Фибоначчи-сетки 🟡 (лог изысканий PREDATOR):
    не статичный «×1.618», а Φ в переменной степени 3/α — тяжёлые хвосты
    (кислота, α<2) раздвигают цель, умеренные (щёлочь) сжимают:

        unit = spread_median · Φ^(3/α) / √VPIN

    α=1.78 → Φ^1.685 ≈ ×2.25; α=3 → Φ¹ = ×1.618. Деление на √VPIN — из
    солитон-выхода лога (низкая токсичность = поток чистый, ход дальше).
    Показатель клипуется [0.75; 2.0] (α∈[1.5; 4]), VPIN снизу 0.05, итоговый
    множитель капом ×8 — санитарные границы, не сигнал. Нет спреда → None
    (NO DUMMIES). ⚫ масштаб тейков, не предсказание хода. 18+."""
    s = _f(spread_median)
    if s <= 0:
        return {"unit": None, "note": "нет медианного спреда — базу не выдумываем"}
    a = _f(alpha, 3.0)
    a = max(1.5, min(4.0, a if a > 0 else 3.0))
    expo = max(0.75, min(2.0, 3.0 / a))
    mult = PHI ** expo
    # VPIN передан → делим ВСЕГДА, с клипом [0.05; 1.0]: vpin=0.0 — валидное
    # значение метрики (идеально сбалансированный поток), а не «нет данных»;
    # без клипа между 0.0 и 0.001 был бы разрыв ×4.5
    v_used = vpin is not None
    if v_used:
        mult /= max(0.05, min(1.0, _f(vpin))) ** 0.5
    mult = min(8.0, mult)
    return {"unit": round(s * mult, 10), "mult": round(mult, 3),
            "expo": round(expo, 3),
            "note": (f"unit = спред·Φ^(3/α)={PHI:.3f}^{expo:.2f}"
                     + ("/√VPIN" if v_used else "")
                     + f" = ×{mult:.2f} 🟡 (конвенция лога, нулями не проверена"
                     + ("; α не измерена — дефолт 3.0" if alpha is None else "")
                     + ")")}


def plan_exit_fib(side: str, entry: float, lots: int, *,
                  book: dict | None = None, unit: float | None = None,
                  env: str = "неизвестно", tick_size: float = 0.0,
                  wall: float | None = None,
                  levels: tuple | None = None,
                  weights: tuple | None = None) -> dict:
    """ФИБОНАЧЧИ-СЕТКА ВЫХОДА: вместо одного тейка — лимитки на уровнях
    золотого сечения. Возврат: {orders: [{lots, price, level}], note, frame}.

      side  — 'long' (выход = sell-лимитки выше входа) | 'short' (ниже);
      entry — цена входа; lots — объём позиции на выход (сумма orders == lots);
      unit  — базовая единица дистанции В ЦЕНЕ. Не задана → ширина спреда из
              book (best_ask − best_bid). Нет ни того ни другого → плана нет
              (NO DUMMIES: без меры хода уровни не выдумываем);
      env   — среда дуальной механики (oracle.dual_env): 'кислота' →
              расширяющаяся спираль (объём к дальнему 1.618); всё остальное
              ('щёлочь'/'переход'/'неизвестно'/не передано) → сжимающаяся,
              консервативный дефолт — недоказанная среда тейки не раздвигает;
      wall  — плита ликвидности по ходу: уровни ЗА стеной срезаются к стене
              с запасом WALL_MARGIN (не ставим тейк в бетон).

    Смысл рассеивания: одна крупная лимитка на круглом уровне — маркер для
    стоп-хантера и лёгкая мишень фронтраннинга; три некруглых куска в
    0.382/0.618/1.618 единицы хода режут footprint и усредняют выход по
    форме импульса. ⚫ тактика исполнения, не сигнал. 18+."""
    total = int(lots)
    if total <= 0 or not entry or entry <= 0:
        return {"orders": [], "note": "нулевой объём/цена — сетки нет",
                "frame": FRAME}
    u = _f(unit)
    if u <= 0:
        bb, ba = _f((book or {}).get("best_bid")), _f((book or {}).get("best_ask"))
        u = (ba - bb) if (bb > 0 and ba > bb) else 0.0
    if u <= 0:
        return {"orders": [], "note": ("нет ни unit, ни спреда из стакана — "
                                       "базовая единица хода не измерена, "
                                       "сетку не выдумываем"), "frame": FRAME}
    sgn = 1.0 if side == "long" else -1.0
    # уровни/веса по умолчанию — фибо-сетка; вызывающий может подать свои
    # (тензор Казимира) — тогда развороты среды его же забота
    lv_list = tuple(levels) if levels else FIB_LEVELS
    if weights:
        w = tuple(weights)
    else:
        # спираль дышит средой: кислота разворачивает веса к дальнему уровню
        w = tuple(reversed(_FIB_W_RAW)) if env == "кислота" else _FIB_W_RAW
    wsum = sum(w)
    # раскладка лотов: floor + крупнейшие остатки, сумма == total всегда
    exact = [total * x / wsum for x in w]
    alloc = [int(x) for x in exact]
    rem = total - sum(alloc)
    order = sorted(range(len(w)), key=lambda i: exact[i] - alloc[i], reverse=True)
    for i in order[:rem]:
        alloc[i] += 1
    tick = tick_size if tick_size > 0 else 0.0
    orders = []
    capped = 0
    for lv, lot in zip(lv_list, alloc):
        if lot <= 0:
            continue
        px = entry + sgn * u * lv
        if tick > 0:                       # обычный снап: В СВОЮ сторону
            steps = (px - entry) / tick
            steps = math.ceil(steps) if sgn > 0 else math.floor(steps)
            px = entry + steps * tick
        # плита по ходу — проверяется ПОСЛЕ снапа: и сырой уровень за срезом,
        # и уровень, ВЫТОЛКНУТЫЙ туда снапом «от входа», режутся к стене.
        # Срезанный снапится К ВХОДУ (floor long / ceil short): округление от
        # входа ставило бы тейк на стену или за неё — ровно тот бетон, от
        # которого бережёт WALL_MARGIN. Минимум — один тик от входа.
        if wall and ((wall - entry) * sgn) > 0:
            lim = entry + (wall - entry) * (1.0 - WALL_MARGIN)
            if (px - lim) * sgn > 0:
                px = lim
                if tick > 0:
                    steps = (lim - entry) / tick
                    steps = math.floor(steps) if sgn > 0 else math.ceil(steps)
                    if abs(steps) < 1:
                        steps = sgn
                    px = entry + steps * tick
                capped += 1
        orders.append({"lots": lot, "price": round(px, 10), "level": lv})
    # срез к стене мог слепить уровни в одну цену — сливаем в ОДИН ордер:
    # две лимитки на одной цене — это не рассеивание, а жирный footprint
    merged: list[dict] = []
    for o in orders:
        if merged and abs(merged[-1]["price"] - o["price"]) < 1e-12:
            merged[-1]["lots"] += o["lots"]
        else:
            merged.append(dict(o))
    orders = merged
    assert sum(o["lots"] for o in orders) == total, (orders, total)
    spiral = ("расширяющаяся (кислота: хвост кормит дальний 1.618)"
              if env == "кислота" else
              "сжимающаяся (консервативный дефолт для щёлочи/перехода/"
              "неизвестной среды: недоказанная среда тейки не раздвигает)")
    return {"orders": orders, "unit": round(u, 10), "spiral": spiral,
            "note": (f"{len(orders)} лимиток на {total} лот, уровни "
                     f"{'/'.join(str(o['level']) for o in orders)}·unit, "
                     f"спираль {spiral}"
                     + (f", {capped} уровн. срезано к стене" if capped else "")),
            "frame": FRAME}


def plan_exit_tensor(side: str, entry: float, lots: int,
                     spread_median: float, *, alpha=None,
                     env: str = "неизвестно", tick_size: float = 0.0,
                     wall: float | None = None) -> dict:
    """Выход по координатам ТЕНЗОРА КАЗИМИРА 🟡 (спринт «Абсолют»):
    38.2% объёма — на границе упругости (S·Φ, уровень 3),
    61.8% объёма — на радиусе квантового пузыря (S·Φ^(2/α), уровень 4).
    Уровни сортируются по дистанции (при α>2 радиус пузыря БЛИЖЕ границы
    упругости — Φ^(2/α)<Φ — это свойство заданной формулы, не баг).
    Вся механика (стена, снап к тику, слияние, точность лотов) — от
    plan_exit_fib. ⚫ тактика исполнения, не сигнал. 18+."""
    ct = casimir_tensor(spread_median, alpha=alpha)
    if not ct.get("levels"):
        return {"orders": [], "note": ct["note"], "frame": FRAME}
    s = ct["levels"][1]                      # S_median (уровень 2, база)
    pairs = sorted(((ct["levels"][2] / s, 0.382),
                    (ct["levels"][3] / s, 0.618)))
    plan = plan_exit_fib(side, entry, lots, unit=s, env=env,
                         tick_size=tick_size, wall=wall,
                         levels=tuple(p[0] for p in pairs),
                         weights=tuple(p[1] for p in pairs))
    plan["tensor"] = ct
    # МЕТКА ПО УРОВНЯМ ТЕНЗОРА, а не по FIB-константе (ревью «Абсолюта»):
    # dual_env объявляет кислоту и при α∈[2;3) с φ≥0.985, а там 2/α<1 —
    # уровень 4 (радиус пузыря) оказывается БЛИЖЕ уровня 3 (граница
    # упругости), и фраза «хвост кормит дальний 1.618» врала бы.
    e4 = ct["expo4"]
    plan["spiral"] = (f"тензор Казимира: 61.8% на уровне 4 (радиус пузыря "
                      f"Φ^{e4:.2f}), 38.2% на уровне 3 (граница упругости Φ)"
                      + ("; уровень 4 ДАЛЬШЕ уровня 3" if e4 > 1.0
                         else "; уровень 4 БЛИЖЕ уровня 3 (2/α<1)"))
    plan["note"] = (f"{len(plan['orders'])} лимиток на {int(lots)} лот по "
                    f"координатам тензора; " + plan["spiral"])
    return plan


def grid_fill_apply(grid: list, order_id: str, pos_lots: int,
                    exec_lots: int | None = None) -> dict:
    """Учёт исполнения одной лимитки Фибо-сетки (чистая математика — брокера
    здесь нет, автопилот лишь склеивает). grid — список ордеров вида
    {order_id, lots, price, level}; order_id — исполнившийся.

    exec_lots — НАКОПИТЕЛЬНОЕ число исполненных лотов по этому ордеру, как
    его отдаёт биржа (Tinkoff lotsExecuted): счётчик не обнуляется между
    опросами. Поэтому книжится ТОЛЬКО ПРИРОСТ сверх уже зачтённого (поле
    booked): иначе повторный опрос той же частично исполненной лимитки
    списывал бы одни и те же лоты снова и снова. None → полное исполнение
    остатка ордера.

    Частичное исполнение оставляет ордер в сетке (booked растёт, живой
    остаток = lots − booked); полное — убирает его из сетки.

    Возврат: {grid: сетка после учёта, filled: {lots, price, level} | None,
              left_lots: лоты позиции после списания, partial: bool}.
    Неизвестный order_id / нулевой прирост → filled=None, ничего не тронуто
    (сверка с биржей разберётся — не выдумываем исполнений)."""
    left = [dict(g) for g in (grid or [])]
    idx = hit = None
    for i, g in enumerate(left):
        if g.get("order_id") == order_id:
            idx, hit = i, dict(g)
            break
    if hit is None:
        return {"grid": left, "filled": None, "left_lots": int(pos_lots),
                "partial": False}
    total = int(hit["lots"])
    booked = int(hit.get("booked") or 0)
    cum = total if exec_lots is None else max(0, min(total, int(exec_lots)))
    delta = cum - booked                       # ТОЛЬКО прирост, не счётчик
    if delta <= 0:                             # нового исполнения нет
        return {"grid": left, "filled": None, "left_lots": int(pos_lots),
                "partial": False}
    partial = cum < total
    if partial:
        left[idx]["booked"] = cum              # ордер жив, зачтено cum
    else:
        left.pop(idx)
    return {"grid": left,
            "filled": {"lots": delta, "price": hit["price"],
                       "level": hit["level"]},
            "left_lots": max(0, int(pos_lots) - delta),
            "partial": partial}


def soliton_exit(side: str, entry: float, price: float, wall: float | None,
                 gains_hist, *, wall_margin: float = WALL_MARGIN) -> dict:
    """ДИНАМИЧЕСКИЙ ВЫХОД ПО СОЛИТОНУ (Агент 4).

    Импульс после срыва — уединённая волна: разгон → гребень → затухание об
    стену (плиту). Три причины выйти, любая срабатывает:
      1. СТЕНА рядом: цена прошла ≥(1−wall_margin) пути «вход→стена» — фиксируем
         ДО бетона (ММ там нальёт против нас);
      2. ГРЕБЕНЬ РАССЫПАЕТСЯ: скорость хода (dG/dt по истории прибыли) упала
         с пика на ≥CREST_DECAY — волна теряет энергию;
      3. ОГИБАЮЩАЯ sech² гребня опустилась ниже SECH2_EXIT от пика — форма
         солитона говорит «пик пройден».
    gains_hist — список долей прибыли позиции во времени (из автопилота).
    Возврат: {exit: bool, reason, ...}. ⚫ тактика выхода, не сигнал."""
    import math
    g = [float(x) for x in (gains_hist or [])]
    now_gain = ((price - entry) / entry if side == "long"
                else (entry - price) / entry) if entry else 0.0
    out = {"exit": False, "now_gain": round(now_gain, 5)}

    # 1) близость СТЕНЫ (плиты ликвидности)
    if wall and entry and wall != entry:
        path = abs(wall - entry)
        done = abs(price - entry) / path if path else 0.0
        toward = ((wall > entry) == (side == "long"))    # стена по ходу?
        out["wall_progress"] = round(done, 3)
        if toward and done >= (1.0 - wall_margin):
            out.update(exit=True, reason=(
                f"стена в {(1-done)*100:.1f}% хода — фиксируем ДО бетона "
                f"(прошли {done*100:.0f}% пути вход→стена)"))
            return out

    # нужна история для формы гребня
    if len(g) < 5:
        out["reason"] = "мало истории гребня — держим"
        return out
    peak = max(g)
    out["peak_gain"] = round(peak, 5)
    if peak <= 0:
        out["reason"] = "гребень ещё не сформирован (прибыли нет)"
        return out

    # 2) скорость гребня рассыпается: dG/dt сейчас против пиковой dG/dt
    vel = [g[k] - g[k - 1] for k in range(1, len(g))]
    vpeak = max(vel) if vel else 0.0
    vnow = sum(vel[-3:]) / min(3, len(vel)) if vel else 0.0
    if vpeak > 0 and vnow <= vpeak * (1.0 - CREST_DECAY) and now_gain > 0:
        out.update(exit=True, reason=(
            f"гребень рассыпается: скорость {vnow:+.4f} против пика "
            f"{vpeak:+.4f} (−{CREST_DECAY*100:.0f}%) — волна теряет энергию"))
        return out

    # 3) огибающая sech²: моделируем гребень как A·sech²((t−t₀)/w), t₀ — бар
    #    пика, w — полуширина по спаду; текущая огибающая ниже порога → выход
    ipk = g.index(peak)
    half = max(1, len(g) - ipk)
    dt = (len(g) - 1 - ipk) / half
    env = 1.0 / (math.cosh(dt) ** 2)                      # sech²(Δ/w) ∈ (0,1]
    out["sech2_env"] = round(env, 3)
    if ipk < len(g) - 1 and env <= SECH2_EXIT and now_gain > 0:
        out.update(exit=True, reason=(
            f"огибающая солитона {env:.2f}≤{SECH2_EXIT} от пика — пик пройден, "
            "форма волны рассыпается"))
        return out

    out["reason"] = "волна на гребне — держим ход"
    return out


# ── self-test: чистая математика плана ──────────────────────────────────────
if __name__ == "__main__":
    book = {"best_bid": 99.5, "best_ask": 100.5,
            "levels_ask": [{"p": 100.5 + i, "q": 40} for i in range(10)],
            "levels_bid": [{"p": 99.5 - i, "q": 40} for i in range(10)]}

    # 1) нарезка: 30 лотов при ближней ликвидности 200 → транш ≤ 50 (25%),
    #    сумма траншей всегда равна плану
    pl = plan_entry("long", 30, 100.0, book, state="ПАРЛАМЕНТ")
    assert sum(t["lots"] for t in pl["tranches"]) == 30
    assert all(t["lots"] <= 50 for t in pl["tranches"])
    #    парламент: первый транш — ТЕНЬ у best_bid, не рыночный
    assert pl["tranches"][0]["kind"] == "shadow"
    assert abs(pl["tranches"][0]["price"] - 99.5) < 1e-9

    # 2) сингулярность («Абсолют»: рыночные входы ЗАПРЕЩЕНЫ): первый транш —
    #    АГРЕССИВНАЯ ТЕНЬ внутрь спреда (мейкер), дедлайн короткий
    ps = plan_entry("long", 30, 100.0, book, state="СИНГУЛЯРНОСТЬ")
    assert ps["urgent"] and ps["tranches"][0]["kind"] == "shadow"
    assert ps["tranches"][0]["price"] is not None          # НЕ рыночный
    assert ps["tranches"][0].get("aggressive") is True
    #    цена внутри спреда [bb; ba], спред не пересекаем (bb=99.5, ba=100.5)
    assert 99.5 <= ps["tranches"][0]["price"] < 100.5, ps["tranches"][0]
    assert ps["deadline_s"] == DEADLINE_SING < DEADLINE_NORM
    #    ни одного рыночного транша ВО ВСЁМ плане
    assert all(t["kind"] != "market" for t in ps["tranches"])
    #    спред в один тик: агрессивная тень не пересекает — встаёт на best
    thin_sp = {"best_bid": 100.0, "best_ask": 100.0001,
               "levels_ask": [{"p": 100.0001, "q": 50}],
               "levels_bid": [{"p": 100.0, "q": 50}]}
    ps1 = plan_entry("long", 4, 100.0, thin_sp, state="СИНГУЛЯРНОСТЬ",
                     tick_size=0.0001)
    assert ps1["tranches"][0]["price"] <= 100.0001 - 0.0001 + 1e-12 or \
        abs(ps1["tranches"][0]["price"] - 100.0) < 1e-9, ps1["tranches"][0]

    # 3) капкан на сбросе: недавний вынос вниз виден → часть объёма ГЛУБЖЕ
    #    рынка на уровне выноса
    prices = [100.0] * 60 + [99.2] + [100.0] * 59          # прокол вниз и ушли
    pg = plan_entry("long", 20, 100.0, book, prices=prices)
    grabs = [t for t in pg["tranches"] if t["kind"] == "grab"]
    assert grabs and abs(grabs[0]["price"] - 99.2) < 1e-9, pg
    assert sum(t["lots"] for t in pg["tranches"]) == 20
    #    рынок ЕЩЁ у дна выноса → капкан не ставится (сброс не завершён)
    assert grab_level([100.0] * 30 + [99.2, 99.21], "long") is None
    #    short: капкан на верхнем выносе
    pr_up = [100.0] * 60 + [100.9] + [100.0] * 59
    assert abs(grab_level(pr_up, "short") - 100.9) < 1e-9

    # 4) участие: тонкий стакан (ликвидность 8) → транш 2 лота максимум
    thin = {"best_bid": 99.5, "best_ask": 100.5,
            "levels_ask": [{"p": 100.5, "q": 8}], "levels_bid": [{"p": 99.5, "q": 8}]}
    pt = plan_entry("long", 12, 100.0, thin)
    assert all(t["lots"] <= 2 for t in pt["tranches"]
               if t["kind"] != "grab"), pt
    assert sum(t["lots"] for t in pt["tranches"]) == 12

    # 5) эскалация («Абсолют»): сингулярность ПЕРЕВЗВОДИТ тень (не рыночный!),
    #    парламент снимает недобор
    import time as _t
    t0 = 1000.0
    assert escalate(ps, t0, t0 + 10, 10, 30)["action"] == "wait"
    e_s = escalate(ps, t0, t0 + DEADLINE_SING + 1, 10, 30)
    assert e_s["action"] == "reprice_rest" and e_s["left"] == 20, e_s
    assert "запрещён" in e_s["note"]
    e_n = escalate(pl, t0, t0 + DEADLINE_NORM + 1, 10, 30)
    assert e_n["action"] == "cancel_rest"
    assert escalate(pl, t0, t0 + 999, 30, 30)["action"] == "done"

    # 6) перевзвод тени: цена ушла → replace на новый best; свежая — hold
    rp = reprice_shadow(99.5, "long", {"best_bid": 99.62, "best_ask": 100.6},
                        t0, t0 + 5)
    assert rp["action"] == "replace" and abs(rp["new_price"] - 99.62) < 1e-9
    assert reprice_shadow(99.5, "long", book, t0, t0 + 5)["action"] == "hold"
    assert reprice_shadow(99.5, "long", book, t0,
                          t0 + SHADOW_TTL + 1)["action"] == "replace"

    # 7) мусор не роняет
    assert plan_entry("long", 0, 100.0, None)["tranches"] == []
    assert plan_entry("long", 5, 0.0, None)["tranches"] == []

    # 8) СОЛИТОННЫЙ ВЫХОД (Агент 4): три причины выйти
    #    (а) стена рядом: прошли ≥80% пути вход→стена → выход ДО бетона
    se_wall = soliton_exit("long", 100.0, 100.9, wall=101.0,
                           gains_hist=[0.001, 0.003, 0.006, 0.008, 0.009])
    assert se_wall["exit"] and "стена" in se_wall["reason"], se_wall
    #    стена ещё далеко (прошли 20%), скорость ровная → держим
    se_far = soliton_exit("long", 100.0, 100.2, wall=101.0,
                          gains_hist=[0.0004, 0.0008, 0.0012, 0.0016, 0.002])
    assert se_far["exit"] is False, se_far
    #    (б) гребень рассыпается: разгон был крутой, теперь скорость сдохла
    se_crest = soliton_exit("long", 100.0, 100.4, wall=None,
                            gains_hist=[0.0, 0.002, 0.005, 0.009, 0.010,
                                        0.0101, 0.0102, 0.0103])
    assert se_crest["exit"] and "гребень" in se_crest["reason"], se_crest
    #    (в) огибающая sech² упала от пика (цена сползает с гребня) → выход
    se_env = soliton_exit("long", 100.0, 100.3, wall=None,
                          gains_hist=[0.001, 0.004, 0.008, 0.006, 0.004, 0.003])
    assert se_env["exit"], se_env
    #    ровный разгон на гребне без стены → держим ход
    se_ride = soliton_exit("long", 100.0, 100.5, wall=None,
                           gains_hist=[0.001, 0.002, 0.003, 0.004, 0.005])
    assert se_ride["exit"] is False, se_ride
    #    мало истории и стена далеко → держим (NO DUMMIES)
    assert soliton_exit("long", 100.0, 100.1, None, [0.001])["exit"] is False
    #    short-сторона: стена снизу, прошли путь → выход
    se_sh = soliton_exit("short", 100.0, 99.1, wall=99.0,
                         gains_hist=[0.001, 0.003, 0.006, 0.008, 0.009])
    assert se_sh["exit"] and "стена" in se_sh["reason"], se_sh
    #    детерминизм
    assert soliton_exit("long", 100.0, 100.4, None,
                        [0.0, 0.002, 0.005, 0.009, 0.010, 0.0101, 0.0102]) == \
        soliton_exit("long", 100.0, 100.4, None,
                     [0.0, 0.002, 0.005, 0.009, 0.010, 0.0101, 0.0102])

    # 9) ФИБОНАЧЧИ-СЕТКА ВЫХОДА: рассеивание вместо одного тейка
    #    (а) базовая раскладка: сумма лотов точна, уровни 0.382/0.618/1.618,
    #        щёлочь → сжимающаяся спираль (ближний уровень — самый жирный)
    fx = plan_exit_fib("long", 100.0, 10, unit=1.0, env="щёлочь")
    assert sum(o["lots"] for o in fx["orders"]) == 10
    assert [o["level"] for o in fx["orders"]] == [0.382, 0.618, 1.618]
    assert fx["orders"][0]["lots"] >= fx["orders"][1]["lots"] \
        >= fx["orders"][2]["lots"], fx
    assert abs(fx["orders"][0]["price"] - 100.382) < 1e-9
    assert abs(fx["orders"][2]["price"] - 101.618) < 1e-9

    #    (б) кислота разворачивает спираль: дальний 1.618 — самый жирный
    fa = plan_exit_fib("long", 100.0, 10, unit=1.0, env="кислота")
    assert fa["orders"][-1]["lots"] >= fa["orders"][0]["lots"], fa
    assert "расширяющаяся" in fa["spiral"]
    assert sum(o["lots"] for o in fa["orders"]) == 10

    #    (в) short зеркален: уровни НИЖЕ входа
    fs = plan_exit_fib("short", 100.0, 9, unit=1.0, env="щёлочь")
    assert all(o["price"] < 100.0 for o in fs["orders"])
    assert abs(fs["orders"][2]["price"] - 98.382) < 1e-9

    #    (г) unit по умолчанию — ширина спреда из стакана
    fb = plan_exit_fib("long", 100.0, 6, book=book)     # спред 1.0
    assert abs(fb["unit"] - 1.0) < 1e-9
    #        ни unit, ни стакана → плана нет (NO DUMMIES)
    assert plan_exit_fib("long", 100.0, 6)["orders"] == []
    assert plan_exit_fib("long", 100.0, 0, unit=1.0)["orders"] == []

    #    (д) мелкая позиция: 1-2 лота не рассыпаются в пыль, сумма точна
    f1 = plan_exit_fib("long", 100.0, 1, unit=1.0)
    assert sum(o["lots"] for o in f1["orders"]) == 1 and len(f1["orders"]) == 1
    f2 = plan_exit_fib("long", 100.0, 2, unit=1.0, env="кислота")
    assert sum(o["lots"] for o in f2["orders"]) == 2

    #    (е) стена по ходу: уровень за плитой срезается к стене с запасом
    fw = plan_exit_fib("long", 100.0, 10, unit=1.0, wall=101.0)
    far = fw["orders"][-1]
    assert far["price"] <= 100.0 + 1.0 * (1 - WALL_MARGIN) + 1e-9, fw
    assert "срезано к стене" in fw["note"]
    #        стена ПРОТИВ хода не трогает сетку
    fnw = plan_exit_fib("long", 100.0, 10, unit=1.0, wall=99.0)
    assert abs(fnw["orders"][-1]["price"] - 101.618) < 1e-9
    #        СТЕНА+ТИК: срезанный уровень снапится К ВХОДУ, а не за стену
    #        (ceil выталкивал тейк 100.8 → 101.4, ЗА стену 101.0)
    for tk in (0.7, 0.5, 0.3):
        fwt = plan_exit_fib("long", 100.0, 10, unit=1.0, wall=101.0,
                            tick_size=tk)
        for o in fwt["orders"]:
            assert o["price"] <= 100.8 + 1e-9, (tk, fwt)      # не глубже среза
            assert o["price"] > 100.0, (tk, fwt)              # и не в входе
    fst = plan_exit_fib("short", 100.0, 10, unit=1.0, wall=99.0, tick_size=0.7)
    for o in fst["orders"]:
        assert o["price"] >= 99.2 - 1e-9, fst                 # зеркально short
        assert o["price"] < 100.0, fst
    #        слипшиеся после среза уровни сливаются в ОДИН ордер (не дубль),
    #        сумма лотов сохраняется
    fmg = plan_exit_fib("long", 100.0, 10, unit=1.0, wall=100.5)
    prices = [o["price"] for o in fmg["orders"]]
    assert len(prices) == len(set(prices)), fmg               # цены уникальны
    assert sum(o["lots"] for o in fmg["orders"]) == 10, fmg

    #    (ж) снап к шагу цены — в свою сторону, не ближе входа
    ft = plan_exit_fib("long", 100.0, 6, unit=1.0, tick_size=0.05)
    for o in ft["orders"]:
        steps = (o["price"] - 100.0) / 0.05
        assert abs(steps - round(steps)) < 1e-6, o       # на сетке тика
    assert ft["orders"][0]["price"] >= 100.382 - 1e-9    # не придвинулся к входу

    #    (з) детерминизм
    assert plan_exit_fib("long", 100.0, 10, unit=1.0, env="кислота") == \
        plan_exit_fib("long", 100.0, 10, unit=1.0, env="кислота")

    #    (и0) учёт исполнений сетки: снятие ордера, уменьшение лотов,
    #         неизвестный ордер не трогает ничего (NO DUMMIES)
    g0 = [{"order_id": "a", "lots": 5, "price": 100.4, "level": 0.382},
          {"order_id": "b", "lots": 3, "price": 100.6, "level": 0.618},
          {"order_id": "c", "lots": 2, "price": 101.6, "level": 1.618}]
    r1 = grid_fill_apply(g0, "a", 10)
    assert r1["left_lots"] == 5 and len(r1["grid"]) == 2
    assert r1["filled"]["level"] == 0.382
    r2 = grid_fill_apply(r1["grid"], "c", r1["left_lots"])
    assert r2["left_lots"] == 3 and [g["order_id"] for g in r2["grid"]] == ["b"]
    r3 = grid_fill_apply(r2["grid"], "нет_такого", r2["left_lots"])
    assert r3["filled"] is None and r3["left_lots"] == 3 and len(r3["grid"]) == 1
    assert grid_fill_apply([], "x", 7) == {"grid": [], "filled": None,
                                           "left_lots": 7, "partial": False}
    #         лоты не уходят в минус даже при рассинхроне
    assert grid_fill_apply(g0, "a", 3)["left_lots"] == 0
    #         вход не мутирует (автопилот держит grid в position)
    assert g0[0]["order_id"] == "a" and len(g0) == 3

    #    (и0б) ЧАСТИЧНОЕ исполнение (Tinkoff lotsExecuted): книжатся ровно
    #          исполненные лоты, ордер ОСТАЁТСЯ в сетке с остатком —
    #          иначе исполненная часть выпадает из учёта и продаётся дважды
    rp = grid_fill_apply(g0, "a", 10, exec_lots=2)
    assert rp["partial"] is True and rp["filled"]["lots"] == 2
    assert rp["left_lots"] == 8, rp
    assert len(rp["grid"]) == 3 and rp["grid"][0]["booked"] == 2, rp
    #          exec_lots НАКОПИТЕЛЬНЫЙ: повторный опрос с тем же счётчиком
    #          НЕ списывает те же лоты второй раз (иначе двойной учёт)
    rp_again = grid_fill_apply(rp["grid"], "a", rp["left_lots"], exec_lots=2)
    assert rp_again["filled"] is None and rp_again["left_lots"] == 8, rp_again
    #          биржа долила ещё 2 (счётчик 4) — книжится ТОЛЬКО прирост
    rp_more = grid_fill_apply(rp["grid"], "a", rp["left_lots"], exec_lots=4)
    assert rp_more["filled"]["lots"] == 2 and rp_more["left_lots"] == 6, rp_more
    assert rp_more["grid"][0]["booked"] == 4
    #          добор до полного объёма закрывает ордер, списывая только остаток
    rp2 = grid_fill_apply(rp_more["grid"], "a", rp_more["left_lots"], exec_lots=5)
    assert rp2["partial"] is False and rp2["filled"]["lots"] == 1
    assert rp2["left_lots"] == 5, rp2
    assert [g["order_id"] for g in rp2["grid"]] == ["b", "c"], rp2
    #          полное исполнение (exec_lots=None) после частичного списывает
    #          только НЕзачтённый остаток
    rp3 = grid_fill_apply(rp["grid"], "a", rp["left_lots"])
    assert rp3["filled"]["lots"] == 3 and rp3["left_lots"] == 5, rp3
    #          exec_lots=0 (заявка принята, но не исполнена) — ничего не трогаем
    r0 = grid_fill_apply(g0, "a", 10, exec_lots=0)
    assert r0["filled"] is None and r0["left_lots"] == 10 and len(r0["grid"]) == 3
    #          exec_lots больше объёма ордера — клипуется объёмом ордера
    rc = grid_fill_apply(g0, "a", 10, exec_lots=99)
    assert rc["filled"]["lots"] == 5 and rc["partial"] is False
    #          exec_lots=None == полное исполнение (старый контракт цел)
    assert grid_fill_apply(g0, "a", 10) == grid_fill_apply(g0, "a", 10, None)

    #    (и-т) ТЕНЗОР КАЗИМИРА: W_Φ = S·[Φ⁻¹,1,Φ,Φ^(2/α)]
    ct = casimir_tensor(1.0, alpha=1.5)
    lv = ct["levels"]
    assert abs(lv[0] - 1/PHI) < 1e-9 and lv[1] == 1.0
    assert abs(lv[2] - PHI) < 1e-9
    assert abs(lv[3] - PHI ** (2/1.5)) < 1e-9          # Φ^1.333 ≈ 1.899
    assert lv[3] > lv[2]                               # кислота: 4-й дальше 3-го
    ct3 = casimir_tensor(1.0, alpha=3.0)
    assert ct3["levels"][3] < ct3["levels"][2]         # щёлочь: 4-й БЛИЖЕ (2/α<1)
    assert casimir_tensor(0.0)["levels"] is None       # NO DUMMIES
    assert "дефолт 3.0" in casimir_tensor(1.0)["note"]
    assert abs(casimir_tensor(2.0, alpha=1.5)["levels"][2] - 2.0 * PHI) < 1e-9

    #    (и-т2) выход по тензору: 38.2% на границе упругости, 61.8% на радиусе
    #    пузыря; сумма лотов точна; уровни отсортированы по дистанции
    pt_ac = plan_exit_tensor("long", 100.0, 10, 1.0, alpha=1.5)
    assert sum(o["lots"] for o in pt_ac["orders"]) == 10
    assert len(pt_ac["orders"]) == 2
    #    кислота: ближний Φ (4 лота=38.2%), дальний Φ^1.33 (6 лотов=61.8%)
    assert pt_ac["orders"][0]["lots"] == 4 and pt_ac["orders"][1]["lots"] == 6
    assert abs(pt_ac["orders"][0]["price"] - (100.0 + PHI)) < 1e-6
    assert pt_ac["orders"][1]["price"] > pt_ac["orders"][0]["price"]
    #    щёлочь (α=3): уровень 4 ближе уровня 3 — сортировка меняет порядок,
    #    но веса едут за уровнями (0.618 остаётся на Φ^(2/α))
    pt_al = plan_exit_tensor("long", 100.0, 10, 1.0, alpha=3.0)
    assert sum(o["lots"] for o in pt_al["orders"]) == 10
    assert pt_al["orders"][0]["price"] < pt_al["orders"][1]["price"]
    assert pt_al["orders"][0]["lots"] == 6              # 61.8% на ближнем Φ^0.67
    #    short зеркален, стена работает через plan_exit_fib
    pt_sh = plan_exit_tensor("short", 100.0, 10, 1.0, alpha=1.5, wall=98.5)
    assert all(o["price"] < 100.0 for o in pt_sh["orders"])
    assert all(o["price"] >= 98.5 + 1.5 * WALL_MARGIN - 1e-9
               for o in pt_sh["orders"])
    assert plan_exit_tensor("long", 100.0, 5, 0.0)["orders"] == []

    #    (и) fib_unit: Φ^(3/α) — кислота раздвигает базу, щёлочь сжимает
    u_acid = fib_unit(1.0, alpha=1.78)
    u_alk = fib_unit(1.0, alpha=3.0)
    assert abs(u_acid["mult"] - 2.25) < 0.02, u_acid      # Φ^1.685 ≈ 2.25
    assert abs(u_alk["mult"] - 1.618) < 0.001, u_alk      # Φ¹
    assert u_acid["unit"] > u_alk["unit"]
    #        √VPIN: чистый поток (VPIN 0.07) раздвигает, кэп ×8 держит
    u_v = fib_unit(1.0, alpha=1.78, vpin=0.07)
    assert u_v["unit"] > u_acid["unit"] and u_v["mult"] <= 8.0
    assert fib_unit(1.0, alpha=1.78, vpin=0.001)["mult"] == 8.0   # кэп
    #        vpin=0.0 — валидное значение (клип к полу 0.05, БЕЗ разрыва
    #        против vpin=0.001), note честен про применение /√VPIN
    assert fib_unit(1.0, alpha=1.78, vpin=0.0) == \
        fib_unit(1.0, alpha=1.78, vpin=0.001)
    assert "/√VPIN" not in fib_unit(1.0, alpha=3.0)["note"]
    assert "/√VPIN" in fib_unit(1.0, alpha=3.0, vpin=0.5)["note"]
    assert "дефолт 3.0" in fib_unit(1.0)["note"]          # честность про α
    #        экстремальные α клипуются, нет спреда → None (NO DUMMIES)
    assert fib_unit(1.0, alpha=0.5)["expo"] == 2.0
    assert fib_unit(1.0, alpha=99.0)["expo"] == 0.75
    assert fib_unit(0.0)["unit"] is None
    #        сцепка: динамическая база кормит сетку
    fd = plan_exit_fib("long", 100.0, 10,
                       unit=fib_unit(1.0, alpha=1.78)["unit"], env="кислота")
    assert fd["orders"][-1]["price"] > 100.0 + 1.618, fd  # дальше статичной

    print("execution self-test OK: нарезка с участием ≤25% ликвидности, тень "
          "у спреда в парламенте, рыночный первый в сингулярности, капкан на "
          "сбросе, эскалация по дедлайну, перевзвод тени; СОЛИТОН-выход — "
          "стена/распад гребня/спад огибающей sech², ровный гребень держим; "
          "ФИБО-СЕТКА выхода — 0.382/0.618/1.618, спираль дышит средой, "
          "стена срезает, тик снапится, сумма лотов точна")
