# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — РИСК И РАЗМЕР ПОЗИЦИИ (устойчивость к тряскам).

Чистая математика денег — БЕЗ сети и БЕЗ ордеров, поэтому проверяема здесь
целиком. Именно этот модуль отвечает на «сколько взять» так, чтобы одна
тряска не обнулила депозит. Ответ владельцу прямо: «на всю сумму макс» и
«устойчивость к тряскам» — противоположности; здесь выбрана устойчивость,
а агрессия достигается ДРОБНЫМ входом («Капкан»), а не плечом на всё.

Состав:
  · size_by_risk(...)   — сколько лотов дать под ФИКСИРОВАННЫЙ риск и стоп
                          (а не «сколько влезет»); кэп по ГО и по доле депозита.
  · kapkan_plan(...)    — дробный вход: разведка малым → догруз лимитками на
                          ложном проколе (из txt: ММ сам наливает по лучшей цене).
  · stop_from_atr(...)  — стоп по волатильности, не по фантазии.
  · SpreadGuard         — «инфаркт спреда»: запрет рыночных входов, когда
                          стакан разорван (проскальзывание убьёт).
  · SessionRisk         — killswitch: дневной лимит потерь + серия убытков →
                          аппаратная блокировка до следующего расчёта Аналитика.
Рамка ⚫: это управление риском, НЕ обещание прибыли. Рынок не предсказуем.
"""
from __future__ import annotations

# ── дефолты школы риска (консервативные; владелец может поднять осознанно) ──
DEF_RISK_FRAC = 0.01      # доля депозита в риск на ОДНУ сделку (1%)
DEF_MAX_FRAC = 0.30      # верхний кэп ГО одной позиции от депозита (30%)
DEF_RECON_FRAC = 0.30      # «разведка»: доля целевого объёма первым входом
DEF_DAY_LOSS = 0.06      # дневной стоп БАЗОВЫЙ: −6% депозита при обычной среде
DEF_LOSS_STREAK = 3         # серия убытков подряд → стоп сессии
DEF_SPREAD_MULT = 3.0       # спред > 3× нормы → инфаркт спреда
# ── динамический killswitch (спринт «Хищник», прокол №5) ────────────────────
# Приказ владельца: «статический процент — логика хомяка. Killswitch должен
# привязываться к волатильности среды; управляемый хаос держим; остановка —
# при СТРУКТУРНОМ сломе модели, а не по фиксированному %».
ATR_REF_FRAC = 0.004     # опорная ATR-доля (0.4% за 15м бар — обычный фьючерс)
VOL_SCALE_MAX = 2.5      # дневной лимит расширяется волатильностью максимум ×2.5
HARD_DAY_CAP = 0.15      # АБСОЛЮТНЫЙ предел дня (не ритейл-заглушка, а
                         # предохранитель от смерти депозита за одну сессию)
MANAGED_STREAK_BONUS = 2  # в управляемом хаосе (окно бифуркации) серия +2


def size_by_risk(deposit: float, entry: float, stop: float, *,
                 point_value: float, go_per_lot: float,
                 risk_frac: float = DEF_RISK_FRAC,
                 max_frac: float = DEF_MAX_FRAC) -> dict:
    """Сколько ЛОТОВ взять, рискуя ровно risk_frac депозита до стопа.

    lots_risk = (deposit·risk_frac) / (|entry−stop|·point_value)
    но не больше, чем позволяет ГО при кэпе max_frac депозита.
    point_value — цена одного пункта за лот (руб/пункт·лот).
    go_per_lot — гарантийное обеспечение одного лота (руб).
    Нет валидного стопа/ГО → 0 лотов + честная причина (NO DUMMIES)."""
    dist = abs(float(entry) - float(stop))
    if dist <= 0 or point_value <= 0:
        return {"lots": 0, "reason": "нет расстояния до стопа / нулевой пункт",
                "risk_rub": 0.0}
    risk_rub = deposit * risk_frac
    lots_risk = risk_rub / (dist * point_value)
    lots_go = (deposit * max_frac / go_per_lot) if go_per_lot > 0 else lots_risk
    lots = int(max(0, min(lots_risk, lots_go)))
    binding = "риск" if lots_risk <= lots_go else "ГО-кэп"
    return {"lots": lots,
            "lots_by_risk": round(lots_risk, 2),
            "lots_by_go": round(lots_go, 2),
            "binding": binding,
            "risk_rub": round(min(lots, lots_risk) * dist * point_value, 2),
            "stop_dist": round(dist, 6),
            "note": (f"взято {lots} лот(ов): риск ~{round(risk_frac*100,2)}% "
                     f"депозита до стопа; ограничитель — {binding}. "
                     "Размер от РИСКА, а не «сколько влезет» — устойчивость к тряске")}


def size_with_leverage(deposit: float, entry: float, stop: float, *,
                        point_value: float, go_per_lot: float,
                        risk_frac: float = DEF_RISK_FRAC,
                        force_max: bool = False) -> dict:
    """Сайзер ПЛЕЧА для владельца, который хочет «считать от макс плеча».

    Отдаёт ЧЕСТНО оба числа:
      · lots_max_leverage = deposit // ГО — полное плечо биржи (весь депозит
        в обеспечение). Плюс дистанция до ЛИКВИДАЦИИ при таком объёме.
      · lots_survivable   = размер от риска (size_by_risk) — сколько можно
        держать, рискуя risk_frac депозита до стопа.
      · lots_min_viable   = 1 лот и какой % депозита он рискует (иногда даже
        1 лот — это уже слишком крупный риск для малого депозита; тогда честно
        говорим «депозит мал»).

    По умолчанию recommended = min(survivable, max_leverage). force_max=True
    возвращает макс-плечо, НО с меткой danger и дистанцией до стоп-аута —
    молча «на всю» не собираем. ⚫ курок и ответственность — за человеком."""
    dist = abs(float(entry) - float(stop))
    go = max(1e-9, float(go_per_lot))
    pv = max(1e-9, float(point_value))
    lots_max = int(deposit // go)                       # полное плечо биржи
    base = size_by_risk(deposit, entry, stop, point_value=pv, go_per_lot=go,
                        risk_frac=risk_frac)
    lots_surv = base["lots"]

    def liquidation(lots: int) -> dict:
        """Насколько тонок лёд при данном объёме: свободная маржа / стоимость
        хода. Мало лотов — большой запас; макс плечо — запас у нуля."""
        if lots <= 0:
            return {"lots": 0, "free_margin_rub": 0.0, "liq_points": None, "liq_pct": None}
        free = deposit - lots * go
        liq_pts = free / (lots * pv) if lots * pv > 0 else 0.0
        liq_pct = (liq_pts / float(entry) * 100.0) if entry else None
        return {"lots": lots, "free_margin_rub": round(free, 1),
                "liq_points": round(liq_pts, 1),
                "liq_pct": (round(liq_pct, 3) if liq_pct is not None else None)}

    risk_1lot = dist * pv
    risk_1lot_frac = risk_1lot / deposit if deposit > 0 else 1.0
    liq_max = liquidation(lots_max)

    recommended = min(lots_surv, lots_max)
    danger = False
    notes = []
    if lots_surv < 1:
        notes.append(f"даже 1 лот рискует {round(risk_1lot_frac*100,2)}% депозита "
                     f"({round(risk_1lot)}₽ при стопе {round(dist)} пунктов) — "
                     f"для {round(deposit)}₽ это уже крупно; либо тише стоп, либо пополнить")
    if force_max:
        recommended = lots_max
        danger = True
        notes.append(f"МАКС ПЛЕЧО {lots_max} лот(ов): ликвидация уже при ходе "
                     f"~{liq_max['liq_pct']}% против ({liq_max['liq_points']} пунктов), "
                     f"свободная маржа {liq_max['free_margin_rub']}₽. Один резкий тик — "
                     f"стоп-аут всего депозита. Это твой выбор, не рекомендация")
    else:
        notes.append(f"рекомендовано {recommended} лот(ов) от риска "
                     f"(макс плечо было бы {lots_max}, но ликвидация при "
                     f"~{liq_max['liq_pct']}% хода — не выживаемо)")

    return {
        "recommended_lots": int(max(0, recommended)),
        "lots_max_leverage": lots_max,
        "lots_survivable": lots_surv,
        "lots_min_viable": 1 if go * (1) <= deposit else 0,
        "risk_1lot_frac": round(risk_1lot_frac, 4),
        "liquidation_at_max": liq_max,
        "danger": danger,
        "note": " · ".join(notes),
        "frame": "⚫ размер позиции — расчёт давления на депозит, НЕ обещание "
                 "прибыли. Макс плечо на малом депозите ≈ ликвидация. 18+",
    }


def situation_to_sizing(classify_result: dict | None) -> dict:
    """Переводит вердикт рентгена (microstructure.classify) в параметры
    адаптивного размера — по мировоззрению владельца: «база 25%, наращиваем,
    на РЕЗКИЙ СКАЧОК — в макс, по ситуации».

    · «расчищает трубу (Казимир+отмены)» → импульс на пороге → СКАЧОК, сила высокая
    · «КИТ грузит айсберг» → набор перед выносом → скачок средней силы (готовимся)
    · «инфаркт спреда» / «истощение» → risk_off (тише или вне рынка)
    · прочее → база 25%.
    """
    actor = ((classify_result or {}).get("actor") or "")
    conf = float((classify_result or {}).get("confidence") or 0.0)
    spike, strength, risk_off = False, 0.0, False
    if "расчищает трубу" in actor:                          # Казимир+отмены = взрыв
        spike, strength = True, min(1.0, 0.7 + 0.3 * conf)
    elif "КИТ грузит" in actor:                             # набор айсберга — готовимся
        spike, strength = True, min(0.7, 0.4 + 0.3 * conf)
    elif "ИНФАРКТ" in actor:                                # хаос — руки прочь
        risk_off = True
    elif "истощение" in actor:                              # топливо кончилось — тише
        strength = 0.0
    return {"spike": spike, "spike_strength": round(strength, 3), "risk_off": risk_off,
            "why": actor or "нейтраль"}


def adaptive_size(deposit: float, price: float, *, point_value: float,
                  go_per_lot: float, base_frac: float = 0.25,
                  spike: bool = False, spike_strength: float = 0.0,
                  risk_off: bool = False, min_liq_pct: float = 2.0,
                  hardcore: bool = True, floor_one_lot: bool = True) -> dict:
    """Адаптивный размер по мировоззрению владельца (txt): нормально держим
    ~base_frac (25%) капитала в обеспечении; на РЕЗКОМ СКАЧКЕ наращиваем долю
    к максимуму пропорционально силе сигнала; в хаосе (risk_off) — вне рынка.

    frac = 0                       если risk_off
         = base + (1−base)·сила    если скачок
         = base                    иначе
    lots = ⌊deposit·frac / ГО⌋.

    По прямому указанию владельца:
      · floor_one_lot — «если на 25% не даёт купить, набираем до макса, пока
        не даст»: когда база не набирает даже 1 лота, а максимум ≥1 — берём
        минимум 1 лот (то есть долю поднимаем выше базовой, вплоть до макса).
      · hardcore (ПО УМОЛЧАНИЮ) — «сразу хардкор по базе»: предохранитель
        min_liq_pct НЕ срезает плечо. Реальный тормоз капитала — killswitch
        (SessionRisk: −6%/день, серия убытков), он остаётся всегда.
    liq_pct считается и показывается ЧЕСТНО в любом режиме. ⚫ курок за человеком."""
    go = max(1e-9, float(go_per_lot)); pv = max(1e-9, float(point_value))
    lots_max = int(deposit / go)
    if risk_off:
        return {"lots": 0, "lots_max": lots_max, "frac": 0.0,
                "reason": "risk_off (инфаркт/хаос) — вне рынка",
                "liq_pct": None, "spike": False, "capped_by_safety": False,
                "floored_to_one": False,
                "frame": "⚫ размер по ситуации, НЕ обещание прибыли. Плечо 18+, курок за тобой"}
    frac = base_frac
    if spike:
        frac = base_frac + (1.0 - base_frac) * max(0.0, min(1.0, spike_strength))
    frac = max(0.0, min(1.0, frac))
    lots = int(deposit * frac / go)

    def liq_pct(n: int) -> float | None:
        if n <= 0:
            return None
        free = deposit - n * go
        pts = free / (n * pv)
        return (pts / price * 100.0) if price else None

    # «набираем до макса, пока не даст»: база не тянет лота → берём минимум 1
    floored = False
    if lots < 1 and floor_one_lot and lots_max >= 1:
        lots = 1
        floored = True

    # предохранитель: не тоньше min_liq_pct — ТОЛЬКО если не hardcore
    capped = False
    if not hardcore:
        while lots > 1:
            lp = liq_pct(lots)
            if lp is None or lp >= min_liq_pct:
                break
            lots -= 1
            capped = True
    lots = max(0, min(lots, lots_max))
    return {
        "lots": lots, "lots_max": lots_max, "frac": round(frac, 3),
        "spike": bool(spike), "spike_strength": round(spike_strength, 3),
        "capped_by_safety": capped, "floored_to_one": floored,
        "hardcore": bool(hardcore),
        "liq_pct": (round(liq_pct(lots), 3) if lots else None),
        "reason": (f"{'СКАЧОК → наращиваем' if spike else 'база'} "
                   f"{round(frac*100)}% капитала = {lots} лот(ов) (макс {lots_max})"
                   + ("; база не тянула лота — набрали до 1 (до макса, пока не даст)" if floored else "")
                   + (f"; срезано предохранителем до ликвидации ≥{min_liq_pct}%" if capped else "")
                   + ("; ХАРДКОР: предохранитель выкл, тормоз — killswitch" if hardcore else "")),
        "frame": "⚫ размер по ситуации, НЕ обещание прибыли. Плечо 18+, курок за тобой",
    }


def stop_from_atr(entry: float, atr: float, direction: str,
                  k: float = 1.5) -> float:
    """Стоп по волатильности: entry ∓ k·ATR (long — ниже, short — выше)."""
    atr = max(0.0, float(atr))
    return entry - k * atr if direction == "long" else entry + k * atr


def kapkan_plan(total_lots: int, entry: float, *, direction: str,
                atr: float, recon_frac: float = DEF_RECON_FRAC,
                reloads: int = 2, step_atr: float = 0.4) -> dict:
    """Дробный вход «Капкан» из txt: разведка малым → догруз лимитками на
    ложном проколе ПРОТИВ входа (ММ сбрасывает слабаков — сам наливает нам).

    Возвращает: recon_lots (рыночный разведвход) + tranches[] (лимитные
    ордера на догруз чуть «хуже» текущей цены, куда ММ сделает вынос).
    long → лимитки НИЖЕ; short → ВЫШЕ. Итог всех траншей = total_lots."""
    total_lots = int(total_lots)
    if total_lots <= 0:
        return {"recon_lots": 0, "tranches": [], "note": "нулевой объём — вход не строится"}
    recon = max(1, int(round(total_lots * recon_frac)))
    recon = min(recon, total_lots)
    rest = total_lots - recon
    tranches = []
    if rest > 0 and reloads > 0:
        per = [rest // reloads] * reloads
        for i in range(rest % reloads):
            per[i] += 1
        sign = -1.0 if direction == "long" else 1.0
        for i, lots in enumerate(per, start=1):
            if lots <= 0:
                continue
            px = entry + sign * step_atr * atr * i
            tranches.append({"lots": lots, "price": round(px, 6),
                             "level": i, "kind": "лимит-догруз на проколе"})
    return {"recon_lots": recon, "recon_kind": "рыночная разведка",
            "tranches": tranches,
            "total": recon + sum(t["lots"] for t in tranches),
            "note": ("разведка малым объёмом; на ложном проколе против входа "
                     "срабатывают лимитки-догруза по ЛУЧШЕЙ цене — ММ сам "
                     "наливает нам полную корзину (механика «Капкана» из txt)")}


class SpreadGuard:
    """Инфаркт спреда: если стакан разорван — рыночные входы запрещены."""

    def __init__(self, mult: float = DEF_SPREAD_MULT):
        self.mult = mult

    def blocked(self, spread_bps: float, norm_bps: float) -> dict:
        norm_bps = max(1e-9, norm_bps)
        bad = spread_bps > norm_bps * self.mult
        return {"blocked": bool(bad), "spread_bps": spread_bps,
                "norm_bps": norm_bps, "ratio": round(spread_bps / norm_bps, 2),
                "word": ("инфаркт спреда: стакан разорван, рыночный вход "
                         "запрещён (проскальзывание)" if bad else "спред в норме")}


class SessionRisk:
    """ДИНАМИЧЕСКИЙ killswitch сессии (прокол №5 закрыт).

    Не статический процент, а физика среды:
      · дневной лимит = base × vol_scale (ATR среды против опорной ATR_REF),
        но НИКОГДА не шире HARD_DAY_CAP — абсолютный предохранитель;
      · управляемый хаос (окно бифуркации, модель жива) → серия убытков
        удлиняется на MANAGED_STREAK_BONUS: клевки ММ в зоне слома не
        выбивают бота с правильного вектора;
      · СТРУКТУРНЫЙ СЛОМ модели (lock_structural) — блокировка немедленно,
        независимо от процентов: модель ошиблась, а не среда шумнула.
    Раз сработал — торговля заблокирована до сброса Аналитиком (no «отыгрыш»)."""

    def __init__(self, deposit: float, day_loss_frac: float = DEF_DAY_LOSS,
                 loss_streak: int = DEF_LOSS_STREAK):
        self.deposit0 = deposit
        self.day_loss_frac = day_loss_frac
        self.loss_streak_max = loss_streak
        self.pnl = 0.0
        self.streak = 0
        self.locked = False
        self.reason: str | None = None
        self.vol_scale = 1.0
        self.managed = False

    def set_env(self, atr_frac: float | None = None,
                managed_chaos: bool = False) -> None:
        """Среда дня: ATR-доля расширяет лимит (шумный день ≠ плохой день),
        окно бифуркации с живой моделью удлиняет допустимую серию."""
        if atr_frac and atr_frac > 0:
            self.vol_scale = max(1.0, min(VOL_SCALE_MAX, atr_frac / ATR_REF_FRAC))
        self.managed = bool(managed_chaos)

    def day_limit_frac(self) -> float:
        """Действующий дневной лимит: base×vol, но не шире HARD_DAY_CAP."""
        return min(self.day_loss_frac * self.vol_scale, HARD_DAY_CAP)

    def lock_structural(self, reason: str) -> dict:
        """СТРУКТУРНЫЙ СЛОМ: модель назвала вектор, рынок пошёл против сверх
        допуска. Это не шум — это ошибка модели. Блокировка немедленно."""
        self.locked = True
        self.reason = f"СТРУКТУРНЫЙ СЛОМ: {reason}"
        return self.state()

    def record(self, trade_pnl: float) -> dict:
        """Занести результат сделки; вернуть состояние killswitch."""
        self.pnl += trade_pnl
        self.streak = self.streak + 1 if trade_pnl < 0 else 0
        lim = self.day_limit_frac()
        streak_max = self.loss_streak_max + (MANAGED_STREAK_BONUS
                                             if self.managed else 0)
        if self.pnl <= -self.deposit0 * lim:
            self.locked = True
            self.reason = (f"дневной стоп: потеряно "
                           f"{round(-self.pnl/self.deposit0*100,2)}% депозита "
                           f"(лимит {round(lim*100,1)}% при vol×{self.vol_scale:.2f})")
        elif self.streak >= streak_max:
            self.locked = True
            self.reason = (f"серия убытков {self.streak} подряд — стоп сессии"
                           + (" (в управляемом хаосе давали +2)" if self.managed
                              else ""))
        return self.state()

    def can_trade(self) -> bool:
        return not self.locked

    def reset(self) -> None:
        """Сброс на новом расчёте Аналитика (новая макро-сессия)."""
        self.streak = 0
        self.locked = False
        self.reason = None

    def state(self) -> dict:
        return {"pnl": round(self.pnl, 2), "streak": self.streak,
                "locked": self.locked, "reason": self.reason,
                "vol_scale": round(self.vol_scale, 2),
                "managed_chaos": self.managed,
                "day_loss_limit": round(-self.deposit0 * self.day_limit_frac(), 2)}


# ── self-test: чистая математика, сеть и ордера не нужны ─────────────────────
if __name__ == "__main__":
    # 1) размер от риска: 100к депозит, риск 1%, стоп в 200 пунктах, пункт 1 руб/лот
    r = size_by_risk(100000, entry=90000, stop=89800,
                     point_value=1.0, go_per_lot=4500)
    #   риск-лотов = 1000/(200·1)=5; ГО-лотов = 100000·0.3/4500≈6.67 → связывает риск
    assert r["lots"] == 5 and r["binding"] == "риск", r
    assert abs(r["risk_rub"] - 1000.0) < 1e-6

    # 2) ГО становится ограничителем при узком стопе
    r2 = size_by_risk(100000, entry=90000, stop=89990,
                      point_value=1.0, go_per_lot=4500)
    #   риск-лотов = 1000/10 = 100; ГО-кэп = 6 → связывает ГО
    assert r2["binding"] == "ГО-кэп" and r2["lots"] == 6, r2

    # 3) стоп по ATR
    assert stop_from_atr(100, 2, "long", 1.5) == 97.0
    assert stop_from_atr(100, 2, "short", 1.5) == 103.0

    # 4) Капкан: 10 лотов, разведка 30% = 3, остаток 7 двумя траншами (4+3),
    #    long → лимитки НИЖЕ входа
    k = kapkan_plan(10, entry=90000, direction="long", atr=100, reloads=2)
    assert k["recon_lots"] == 3 and k["total"] == 10
    assert len(k["tranches"]) == 2
    assert k["tranches"][0]["price"] < 90000 and k["tranches"][1]["price"] < k["tranches"][0]["price"]
    assert k["tranches"][0]["lots"] + k["tranches"][1]["lots"] == 7

    # short → лимитки ВЫШЕ
    ks = kapkan_plan(6, entry=90000, direction="short", atr=100, reloads=2)
    assert all(t["price"] > 90000 for t in ks["tranches"])

    # 5) инфаркт спреда
    sg = SpreadGuard(mult=3.0)
    assert sg.blocked(10, 2)["blocked"] is True         # 10 > 2·3
    assert sg.blocked(5, 2)["blocked"] is False         # 5 < 6

    # 6) killswitch: дневной стоп −6%
    sr = SessionRisk(100000, day_loss_frac=0.06, loss_streak=3)
    sr.record(-3000); assert sr.can_trade()
    st = sr.record(-3500)                                # суммарно −6500 > −6000
    assert st["locked"] and "дневной стоп" in st["reason"]

    # 7) сайзер плеча на РЕАЛЬНОМ депозите владельца 8000₽ (CR, ГО 1200):
    lv = size_with_leverage(8000, entry=11000, stop=10900,
                            point_value=1.0, go_per_lot=1200)
    #   макс плечо = 8000//1200 = 6 лотов; ликвидация при таком объёме — тонкая
    assert lv["lots_max_leverage"] == 6, lv
    assert lv["liquidation_at_max"]["liq_pct"] is not None
    assert lv["liquidation_at_max"]["liq_pct"] < 2.0        # макс плечо = стоп-аут при <2% хода
    #   выживаемый размер от риска 1% (80₽) при стопе 100 пунктов = 0 лотов
    assert lv["lots_survivable"] == 0 and lv["recommended_lots"] == 0
    assert "крупно" in lv["note"] or "выживаемо" in lv["note"]

    # 8) force_max честно помечает danger и считает дистанцию до ликвидации
    lvf = size_with_leverage(8000, entry=11000, stop=10900,
                             point_value=1.0, go_per_lot=1200, force_max=True)
    assert lvf["danger"] is True and lvf["recommended_lots"] == 6
    assert "МАКС ПЛЕЧО" in lvf["note"] and "стоп-аут" in lvf["note"]

    # 9) killswitch: серия убытков
    sr2 = SessionRisk(100000, day_loss_frac=0.9, loss_streak=3)
    sr2.record(-100); sr2.record(-100)
    assert sr2.can_trade()
    s2 = sr2.record(-100)
    assert s2["locked"] and "серия убытков" in s2["reason"]
    sr2.reset(); assert sr2.can_trade()                 # новый расчёт Аналитика снимает

    # 9b) ДИНАМИЧЕСКИЙ killswitch (прокол №5): волатильная среда расширяет
    #     дневной лимит (6% × vol2.0 = 12%), но НЕ шире HARD_DAY_CAP (15%)
    srd = SessionRisk(100000, day_loss_frac=0.06, loss_streak=3)
    srd.set_env(atr_frac=0.008)                          # ATR вдвое выше опоры
    assert abs(srd.vol_scale - 2.0) < 1e-9
    srd.record(-7000)                                    # −7% < лимита 12% — живём
    assert srd.can_trade(), srd.state()
    srd.record(-6000)                                    # −13% > 12% — стоп
    assert not srd.can_trade()
    #     кэп: даже ATR ×10 не расширяет лимит дальше 15%
    srh = SessionRisk(100000, day_loss_frac=0.06)
    srh.set_env(atr_frac=0.04)
    assert abs(srh.day_limit_frac() - HARD_DAY_CAP) < 1e-9

    # 9c) управляемый хаос: окно бифуркации удлиняет серию (3 → 5) —
    #     клевки ММ в зоне слома не выбивают бота с вектора
    srm = SessionRisk(100000, day_loss_frac=0.9, loss_streak=3)
    srm.set_env(managed_chaos=True)
    for _ in range(4):
        srm.record(-100)
    assert srm.can_trade(), srm.state()                  # 4 < 3+2
    assert not srm.record(-100)["locked"] is True or srm.streak == 5
    assert not srm.can_trade()                           # 5-й минус — стоп

    # 9d) СТРУКТУРНЫЙ СЛОМ: блокирует немедленно, невзирая на проценты
    srs = SessionRisk(100000)
    st_s = srs.lock_structural("вектор LPPLS short, рынок ушёл +2 ATR против")
    assert st_s["locked"] and "СТРУКТУРНЫЙ" in st_s["reason"]
    srs.reset(); assert srs.can_trade()

    # 10) нулевой стоп → 0 лотов честно
    assert size_by_risk(100000, 90000, 90000, point_value=1, go_per_lot=4500)["lots"] == 0

    # 11) АДАПТИВНЫЙ размер (txt: 25% база, скачок→макс, ХАРДКОР по базе)
    #    депозит 8000₽, CR (ГО 1200): база 25% → 1 лот; скачок 1.0 → макс
    a_base = adaptive_size(8000, 11000, point_value=1.0, go_per_lot=1200)
    assert a_base["lots"] == 1 and abs(a_base["frac"] - 0.25) < 1e-9, a_base   # 8000·0.25/1200=1.66→1
    a_spike = adaptive_size(8000, 11000, point_value=1.0, go_per_lot=1200,
                            spike=True, spike_strength=1.0)                     # hardcore по умолч.
    assert a_spike["lots"] == a_spike["lots_max"], a_spike                     # скачок 100% → макс
    assert a_spike["hardcore"] is True
    a_off = adaptive_size(8000, 11000, point_value=1.0, go_per_lot=1200, risk_off=True)
    assert a_off["lots"] == 0 and "вне рынка" in a_off["reason"]               # хаос → вне рынка
    #    предохранитель: с hardcore=False макс-плечо срезается (ликвидация тонкая)
    a_safe = adaptive_size(8000, 11000, point_value=1.0, go_per_lot=1200,
                           spike=True, spike_strength=1.0, hardcore=False, min_liq_pct=2.0)
    assert a_safe["lots"] <= a_spike["lots"] and a_safe["capped_by_safety"], (a_safe, a_spike)
    #    «набираем до макса, пока не даст»: на 4000₽ база 25% не тянет лота → берём 1
    a_floor = adaptive_size(4000, 11000, point_value=1.0, go_per_lot=1200)
    assert a_floor["lots"] == 1 and a_floor["floored_to_one"] is True, a_floor  # 4000·0.25/1200=0.83→0→1

    # 12) situation_to_sizing переводит вердикт рентгена в параметры размера
    s1 = situation_to_sizing({"actor": "МАРКЕТМЕЙКЕР расчищает трубу (Казимир+отмены)",
                              "confidence": 0.7})
    assert s1["spike"] is True and s1["spike_strength"] >= 0.7
    s2 = situation_to_sizing({"actor": "ИНФАРКТ СПРЕДА (хаос)", "confidence": 0.9})
    assert s2["risk_off"] is True
    s3 = situation_to_sizing({"actor": "толпа-шум", "confidence": 0.3})
    assert s3["spike"] is False and s3["risk_off"] is False

    print("trader_risk self-test OK: размер от риска (не «на всю сумму»), Капкан "
          "дробит вход, спред-guard, killswitch −6%/серия, нулевой стоп→0; сайзер "
          "плеча честно показывает макс-плечо И дистанцию до ликвидации (8000₽ → тонкий лёд)")
