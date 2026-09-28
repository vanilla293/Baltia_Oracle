# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — МАШИНА СОСТОЯНИЙ ФАРМИЛЫ (мозг исполнителя).

Чистая логика решений, БЕЗ сети и ордеров → проверяема здесь целиком.
Не считает миллисекунды на C++ (это следующий слой), а держит ФАЗЫ сделки
из txt и на каждом тике выдаёт намерение (что сделать рукам-брокеру).

ФАЗЫ (State Machine из txt: «Запал → Разведка → Капкан → Трейлинг»):
  IDLE   — вне рынка, ждём директиву Аналитика.
  ARMED  — Аналитик дал вектор (LONG/SHORT), ищем сетап входа.
  RECON  — вошли разведкой (малый объём).
  TRAP   — цена клюнула ПРОТИВ входа; решаем: синтетический сброс слабаков
           (Херст<0.5, агрессор иссяк) → догруз лимитками, или реальный слом
           (агрессор льёт против) → выход.
  LOADED — полный объём набран.
  TRAIL  — едем в прибыли, трейлим по плотностям, ждём смерти импульса.

ТРИГГЕРЫ ВХОДА (gate): директива Аналитика · вакуум по курсу · агрессор
подтверждает · спред в норме · Херст>0.5 (тренд, не флет).
ТРИГГЕРЫ ВЫХОДА (смерть импульса, из txt):
  · стена-затвор впереди (ММ ставит бетон) · смерть агрессора (лента иссякла)
  · встречная абсорбция (объём летит, цена стоит) · инфаркт спреда · стоп
  · разворот директивы Аналитика.
Фаза-состояние ⚫: это карта решений, НЕ обещание прибыли.
"""
from __future__ import annotations

# пороги школы Фармилы (осознанные дефолты; 🟡 калибруются под актив)
HURST_TREND = 0.5        # >0.5 тренд, <0.5 возврат к среднему (синтетический клевок)
AGG_DEAD = 0.35       # доля «сейчас/пик» скорости ленты → топливо кончилось
WALL_AHEAD_MULT = 20.0        # стена по курсу ≥ ×медианы в ближней зоне = затвор
ABSORB_MOVE_EPS = 0.0002      # «цена стоит»: |ход| < 2 bps при кипящей ленте


def _f(x, d=0.0):
    try:
        v = float(x)
        return v if v == v else d       # NaN → d
    except (TypeError, ValueError):
        return d


class Farmila:
    """Машина состояний одной позиции по одному инструменту.

    feats на каждом тике (из maya/стакана/ленты) — dict:
      dir        : 'long'|'short'|'flat'   — директива Аналитика (якорь макро)
      vacuum_ahead : bool                  — есть вакуум по курсу входа
      vacuum_bps : float                   — дистанция до вакуума
      aggressor  : float 0..1              — сторона агрессии ленты (>.5 buy)
      agg_speed  : float 0..1              — скорость ленты сейчас/пик (VPIN-прокси)
      spread_bad : bool                    — инфаркт спреда (от SpreadGuard)
      hurst      : float                   — экспонента Херста микро-движения
      wall_ahead_mult : float              — плотность стены по курсу (×медианы)
      price      : float                   — текущая цена
      poke_against : bool                  — цена клюнула против входа
      move_frac  : float                   — |ход цены| за окно (доля)
      stop_hit   : bool                    — цена пробила стоп
    """

    def __init__(self, plan: dict, *, symbol: str = ""):
        self.symbol = symbol
        self.plan = plan               # kapkan_plan(...) результат
        self.state = "IDLE"
        self.side: str | None = None
        self.filled = 0                # набранных лотов
        self.entry: float | None = None
        self.reason = "старт"

    def _act(self, action: str, **kw) -> dict:
        return {"action": action, "state": self.state, "side": self.side,
                "filled": self.filled, "reason": self.reason, **kw}

    def step(self, feats: dict) -> dict:
        d = feats.get("dir")
        # разворот директивы Аналитика при открытой позиции → немедленный выход
        if self.state in ("RECON", "TRAP", "LOADED", "TRAIL") and self.side and d not in (self.side, None):
            self.reason = f"Аналитик развернул вектор ({self.side}→{d}) — выход"
            return self._close("flip")
        # инфаркт спреда / стоп — выход из любой открытой фазы
        if self.state in ("RECON", "TRAP", "LOADED", "TRAIL"):
            if feats.get("spread_bad"):
                self.reason = "инфаркт спреда — рынок разорван, выход"
                return self._close("spread")
            if feats.get("stop_hit"):
                self.reason = "стоп пробит — выход"
                return self._close("stop")

        m = getattr(self, f"_st_{self.state.lower()}")
        return m(feats)

    # ── фазы ──────────────────────────────────────────────────────────────
    def _st_idle(self, f):
        if f.get("dir") in ("long", "short"):
            self.side = f["dir"]
            self.state = "ARMED"
            self.reason = f"Аналитик: вектор {self.side} — ищем сетап"
        return self._act("wait")

    def _st_armed(self, f):
        # gate входа: вакуум по курсу + агрессор за нас + спред ок + тренд (Херст)
        agg = _f(f.get("aggressor"), 0.5)
        agg_ok = (agg > 0.55) if self.side == "long" else (agg < 0.45)
        gate = (f.get("vacuum_ahead") and agg_ok and not f.get("spread_bad")
                and _f(f.get("hurst"), 0.5) >= HURST_TREND)
        if gate:
            self.entry = _f(f.get("price"))
            self.filled = int(self.plan.get("recon_lots", 0))
            self.state = "RECON"
            self.reason = "сетап собран: вакуум+агрессор+тренд — разведвход"
            return self._act("enter", lots=self.filled, kind="market",
                             side=self.side)
        self.reason = "жду сетап (нет вакуума/агрессора/тренд слабый)"
        return self._act("wait")

    def _st_recon(self, f):
        # ложный клевок против входа → фаза Капкана
        if f.get("poke_against"):
            self.state = "TRAP"
            self.reason = "клевок против входа — проверяю: синтетика или слом"
            return self._act("hold")
        # импульс пошёл — доливаем остаток лимитками и едем
        if self._impulse_ok(f):
            self.state = "LOADED"
            self.reason = "импульс подтверждён — ставлю догруз-лимитки Капкана"
            return self._act("arm_tranches", tranches=self.plan.get("tranches", []))
        return self._act("hold")

    def _st_trap(self, f):
        # Херст<0.5 И агрессор НЕ льёт против → синтетический сброс слабаков:
        # ММ сам нальёт нам по лучшей цене → держим и ждём догруз лимитками
        hurst = _f(f.get("hurst"), 0.5)
        agg = _f(f.get("aggressor"), 0.5)
        real_dump = (agg < 0.4) if self.side == "long" else (agg > 0.6)
        if hurst < HURST_TREND and not real_dump:
            self.state = "LOADED"
            self.reason = ("синтетический сброс (Херст<0.5, реальных продаж нет) — "
                           "лимитки Капкана ловят вынос по лучшей цене")
            return self._act("arm_tranches", tranches=self.plan.get("tranches", []))
        # реальный слом: агрессор льёт против нас → выход
        self.reason = "реальный слом (агрессор льёт против) — выход из разведки"
        return self._close("break")

    def _st_loaded(self, f):
        self.state = "TRAIL"
        self.reason = "полный объём — перехожу в трейлинг летящей позиции"
        return self._act("trail")

    def _st_trail(self, f):
        # смерть импульса → фиксируем максимум (триггеры из txt)
        exit_reason = self._exhaustion(f)
        if exit_reason:
            self.reason = exit_reason
            return self._close("take")
        return self._act("trail")

    # ── помощники ─────────────────────────────────────────────────────────
    def _impulse_ok(self, f) -> bool:
        agg = _f(f.get("aggressor"), 0.5)
        agg_ok = (agg > 0.55) if self.side == "long" else (agg < 0.45)
        return bool(f.get("vacuum_ahead") and agg_ok
                    and _f(f.get("agg_speed"), 0) > AGG_DEAD)

    def _exhaustion(self, f) -> str | None:
        """Какой из сигналов смерти импульса сработал (или None — держим)."""
        # 1) стена-затвор впереди (Казимир: ММ поставил бетон)
        if _f(f.get("wall_ahead_mult"), 0) >= WALL_AHEAD_MULT:
            return "стена-затвор впереди (ММ закрыл шлюз) — фиксирую у стены"
        # 2) кислородное голодание: лента иссякла
        if _f(f.get("agg_speed"), 1.0) < AGG_DEAD:
            return "смерть агрессора (лента иссякла) — топливо кончилось, выход"
        # 3) встречная абсорбция: объём кипит, а цена стоит
        if (_f(f.get("agg_speed"), 0) > 0.6
                and _f(f.get("move_frac"), 1.0) < ABSORB_MOVE_EPS):
            return "встречная абсорбция (объём летит, цена стоит) — айсберг, выход"
        return None

    def _close(self, kind: str) -> dict:
        lots = self.filled
        self.state = "IDLE"
        self.side_closed = self.side
        self.side = None
        self.filled = 0
        self.entry = None
        return self._act("close", close_kind=kind, lots=lots)


# ── self-test: полный жизненный цикл на синтетических тиках ──────────────────
if __name__ == "__main__":
    from trader_risk import kapkan_plan, size_by_risk

    plan = kapkan_plan(10, entry=90000, direction="long", atr=100, reloads=2)
    F = Farmila(plan, symbol="Si")

    # 1) IDLE: без директивы — ждём
    assert F.step({"dir": "flat"})["action"] == "wait" and F.state == "IDLE"

    # 2) директива LONG → ARMED
    F.step({"dir": "long"}); assert F.state == "ARMED" and F.side == "long"

    # 3) слабый сетап (нет вакуума) — ждём
    a = F.step({"dir": "long", "vacuum_ahead": False, "aggressor": 0.7, "hurst": 0.6})
    assert a["action"] == "wait" and F.state == "ARMED"

    # 4) чистый сетап → разведвход RECON
    a = F.step({"dir": "long", "vacuum_ahead": True, "aggressor": 0.65,
                "hurst": 0.6, "price": 90000, "spread_bad": False})
    assert a["action"] == "enter" and F.state == "RECON" and F.filled == plan["recon_lots"]

    # 5) ложный клевок вниз → TRAP
    a = F.step({"dir": "long", "poke_against": True})
    assert F.state == "TRAP" and a["action"] == "hold"

    # 6) синтетический сброс (Херст<0.5, реальных продаж нет) → догруз, LOADED
    a = F.step({"dir": "long", "hurst": 0.3, "aggressor": 0.5})
    assert a["action"] == "arm_tranches" and F.state == "LOADED"

    # 7) LOADED → TRAIL
    a = F.step({"dir": "long"}); assert a["action"] == "trail" and F.state == "TRAIL"

    # 8) держим, пока топливо есть
    a = F.step({"dir": "long", "wall_ahead_mult": 3, "agg_speed": 0.8,
                "move_frac": 0.01})
    assert a["action"] == "trail" and F.state == "TRAIL"

    # 9) стена-затвор впереди → фиксация
    a = F.step({"dir": "long", "wall_ahead_mult": 30, "agg_speed": 0.8})
    assert a["action"] == "close" and a["close_kind"] == "take" and F.state == "IDLE"

    # ── ветка реального слома в TRAP → выход ──
    G = Farmila(plan, symbol="Si"); G.step({"dir": "long"})
    G.step({"dir": "long", "vacuum_ahead": True, "aggressor": 0.65, "hurst": 0.6,
            "price": 90000})
    G.step({"dir": "long", "poke_against": True})              # TRAP
    a = G.step({"dir": "long", "hurst": 0.3, "aggressor": 0.2})  # реальный слив
    assert a["action"] == "close" and a["close_kind"] == "break"

    # ── разворот директивы Аналитика закрывает позицию ──
    H = Farmila(plan); H.step({"dir": "long"})
    H.step({"dir": "long", "vacuum_ahead": True, "aggressor": 0.65, "hurst": 0.6,
            "price": 90000})
    a = H.step({"dir": "short"})
    assert a["action"] == "close" and a["close_kind"] == "flip"

    # ── инфаркт спреда и стоп рвут любую фазу ──
    for bad in ({"spread_bad": True}, {"stop_hit": True}):
        K = Farmila(plan); K.step({"dir": "long"})
        K.step({"dir": "long", "vacuum_ahead": True, "aggressor": 0.65,
                "hurst": 0.6, "price": 90000})
        a = K.step({"dir": "long", **bad})
        assert a["action"] == "close", bad

    print("trader_strategy self-test OK: Запал→Разведка→Капкан(синтетика vs слом)"
          "→Трейлинг→фиксация; разворот/спред/стоп рвут позицию мгновенно")
