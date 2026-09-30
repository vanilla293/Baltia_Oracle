# -*- coding: utf-8 -*-
"""ПИФИЯ 5.4.4 (X2): дверь, мысль о прибыли и точность исполнения решений ИИ — на фейках, без сети и ключей.

Жалоба владельца 30.09.2026: «почему-то он написал вход, но не вошёл, также было с выходом, закрытие»; воля: PRO — раз в
30 мин (плановая перепроверка) и по рыночным триггерам. Здесь закреплено:
- D1: вид входа (сейчас / откат / прорыв) — по СНИМКУ, который видела модель (перепроверка — цена в её промпте, дверь —
  plan["gate_px"], мысль о прибыли — pos["profit_px"]), а не по цене после раздумий; пройденный прорыв входит,
  пройденный откат — по лучшей цене; повтор того же решения (та же сторона и уровень) не снимает летящую заявку, не
  сбрасывает пометку пробития и не выбрасывает ВОЙТИ двери;
- D3: стопы и выходы ИИ — по снимку; живая цена уже за стопом нового входа — «идея мертва до входа» (честная запись, повод
  дежурному PRO), у ДЕРЖАТЬ / lock_price / ДОБРАТЬ — уровень ИИ становится триггером (тик спросит у троса); подмена
  уровня кодом видна (запись, толмач, last_action);
- C5: условный ВОЙТИ у двери (уровень далеко в невыгодную сторону) — засада у уровня, а не вход по текущей цене;
- D2: переспрос двери из-за дрейфа — не больше одного на план; ВОЙТИ после пометки о дрейфе исполняется по правилу
  отношения (тейк − цена)/(цена − стоп) ≥ 1, иначе «ход отыгран: вход не исполнен» (повод — к плановой);
- D7: ДОБРАТЬ с уровнем — засада добора; прорыв без уровня — «уровень не дан — вход сейчас»; прорыв со стопом по ту
  сторону цены решения мёртв и до пробоя; добор на максимуме — толмачу;
- ритм PRO у двери и в мысли о прибыли: ЖДАТЬ без уровня и срока, молчание, непонятный ответ — без быстрых повторов
  (у двери — до ответа дежурного PRO на перепроверке); срок ЖДАТЬ ≥ 10 мин, не больше 3 кругов на план; отказ биржи
  после ВОЙТИ — повтор заявки без нового вопроса; ответ мысли о прибыли по сменившейся позиции — стадия шины закрыта."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend import ai_pilot, config, mission, trader_risk


class Broker:
    """Мини-биржа: заявки исполняются сразу (или висят NEW — hang), портфеля нет (dry-снимок), GetMaxLots — по mx."""
    mode = "real"

    def __init__(self, hang: bool = False, reject: int = 0):
        self.placed, self.stops, self.cancelled = [], [], []
        self.mx = None
        self.hang, self.reject = hang, reject
        self._n = 0

    async def place(self, figi, direction, lots, price=None, tag="", **kw):
        if self.reject > 0 and tag == "aip-entry":
            self.reject -= 1
            return {"ok": False, "error": "30042: недостаточно средств для совершения сделки"}
        self._n += 1
        self.placed.append({"order_id": f"F-{self._n}", "direction": direction, "lots": int(lots), "price": price,
                            "tag": tag})
        return {"ok": True, "order_id": f"F-{self._n}"}

    async def order_state(self, oid, **kw):
        if self.hang and oid not in self.cancelled:
            return {"ok": True, "filled": False, "status": "EXECUTION_REPORT_STATUS_NEW", "exec_lots": 0}
        if oid in self.cancelled:
            return {"ok": True, "filled": False, "status": "EXECUTION_REPORT_STATUS_CANCELLED", "exec_lots": 0}
        return {"ok": True, "filled": True, "status": "FILL", "exec_lots": 0}

    async def cancel(self, oid, **kw):
        self.cancelled.append(oid)
        return {"ok": True}

    async def place_stop(self, figi, direction, lots, stop_price, tag="", **kw):
        self.stops.append(stop_price)
        return {"ok": True, "stop_order_id": f"S-{len(self.stops)}"}

    async def cancel_stop(self, sid):
        return {"ok": True}

    async def stop_orders(self, *, strict=False):
        return []

    async def max_lots(self, figi, price=None):
        return dict(self.mx) if self.mx else None

    async def portfolio(self):
        return {"mode": "dry", "positions": []}


class FakeAI:
    """money_json / pro_json: очереди ответов по маршрутам; during(route) — рынок живёт, пока ИИ думает."""
    SILENT = "SILENT"

    def __init__(self):
        self.answers: dict = {}
        self.calls: list = []
        self.errors: list = []
        self.during = None

    def queue(self, route, *answers):
        self.answers.setdefault(route, []).extend(answers)

    def count(self, route):
        return sum(1 for r, _ in self.calls if r == route)

    def last_user(self, route):
        return [u for r, u in self.calls if r == route][-1]

    async def _answer(self, route, user):
        self.calls.append((route, user))
        if self.during:
            res = self.during(route)
            if asyncio.iscoroutine(res):
                await res
        q = self.answers.get(route) or []
        a = q.pop(0) if q else {}
        if a == self.SILENT:
            raise asyncio.TimeoutError()
        return a

    async def money_json(self, system, user, *, route, max_tokens=None, attempt_timeout=None):
        return await self._answer(route, user)

    async def pro_json(self, system, user, *, route="pro", max_tokens=None, attempt_timeout=None):
        return await self._answer(route, user)

    def note_error(self, text, route=""):
        self.errors.append((route, text))


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setattr(mission, "_M", {})
    monkeypatch.setattr(mission, "explain", None)
    monkeypatch.setattr(mission.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mission.store_v5, "mission_put", lambda ticker, data: None)
    monkeypatch.setattr(mission.store_v5, "trade_add", lambda rec: None)
    monkeypatch.setattr(mission.ledger, "schedule_after_close", Mock())
    monkeypatch.setattr(mission.ledger, "estimate", lambda rec: 0.0)
    stages: list = []

    async def stage(scope, rid, st, status, **kw):
        stages.append((st, status, str(kw.get("detail") or ""), kw.get("data")))
    monkeypatch.setattr(mission.bus, "stage", stage)
    told: list = []
    monkeypatch.setattr(mission, "_tolmach",
                        lambda m, kind, title, detail="", refs=None: told.append((kind, title, detail)))
    monkeypatch.setattr(mission.market_ctx, "light", AsyncMock(return_value={"text": "живой рынок: тест"}))
    monkeypatch.setattr(mission, "_council_text", lambda: "итог совета: тест")
    monkeypatch.setattr(mission, "_scan_text", lambda m: "")
    monkeypatch.setattr(mission, "_scan_status_raw", lambda t: None)
    monkeypatch.setattr(mission, "_mod", lambda name: None)
    try:
        from backend import astro
        monkeypatch.setattr(astro, "acontext", AsyncMock(return_value=None))
    except Exception:                                  # noqa: BLE001 — без неба перепроверка живёт
        pass
    for k, v in (("PYTHIA_PARTNERS", False), ("PYTHIA_EXCHANGE_STOP", False), ("PYTHIA_ENTRY_CHECK", True),
                 ("PYTHIA_PROFIT_THINK", True), ("PYTHIA_MONEY_MODEL", "pro"), ("PYTHIA_EVENT_TRIAGE", True),
                 ("PYTHIA_SOFT_STOP", True), ("PYTHIA_SOFT_TAKE", True), ("PYTHIA_ENTRY_DRIFT_PCT", 1.0),
                 ("PYTHIA_ENTRY_FRESH_SEC", 1200), ("PYTHIA_PROFIT_THINK_COOL_SEC", 900)):
        monkeypatch.setattr(config, k, v, raising=False)
    monkeypatch.setattr(ai_pilot, "market_clock", None)             # часы выключены: рынок открыт
    f = FakeAI()
    monkeypatch.setattr(mission.ai_v5, "money_json", f.money_json)
    monkeypatch.setattr(mission.ai_v5, "pro_json", f.pro_json)
    monkeypatch.setattr(mission.ai_v5, "note_error", f.note_error)
    f.stages, f.told = stages, told
    return f


def make_pilot(broker=None, play="auto"):
    m = mission.Mission("TEST", "Тест", "futures", play, 100000.0)
    mission._M["TEST"] = m
    p = mission.MissionPilot("TEST", deposit=100000.0, broker=broker or Broker(), mission=m)
    p.figi, p.asset_class = "F", "futures"
    p.go_per_lot, p.tick_size, p.point_value = 12000.0, 0.01, 1.0
    p.deposit = 100000.0
    p.session_risk = trader_risk.SessionRisk(100000.0)
    p._sr_day = p._msk_day()
    p._state_path = None
    p._prepared = True
    p.reanalyze_cb = AsyncMock()
    m.pilot = p
    m.task = SimpleNamespace(done=lambda: False)
    return m, p


def book(price):
    return {"best_bid": round(price - 0.1, 4), "best_ask": round(price + 0.1, 4)}


async def settle_bg(p, n=400):
    for _ in range(n):
        if not [t for t in p._background_tasks if not t.done()]:
            return
        await asyncio.sleep(0.005)


async def tick(p, price, n=1):
    for _ in range(n):
        await p.tick(price, book(price))
        await settle_bg(p)


def council(m, p, do="BUY", entry=None, kind=None, take=110.0, inv=98.0, price=100.0):
    """Приказ совета по цене совета price (снимок совета — m.ctx_price) → план пилота."""
    ex, err = mission._validate_exec({"do": do, "entry": entry, "entry_kind": kind, "take": take, "invalidation": inv,
                                      "why": "совет: тест"}, m.play, price)
    assert err is None, err
    m.ctx_price, m.exec, m.exec_ts = price, ex, time.time()
    assert p.adopt_forecast({"exec": ex}), p.last_action
    return ex


def entries(p):
    return [o for o in p.broker.placed if o["tag"] in ("aip-entry", "aip-cross", "aip-topup")]


def long_pos(p, entry=100.0, lots=5, inv=98.0, take=110.0):
    p.position = {"side": "long", "entry": entry, "lots": lots, "take": take, "invalidation": inv, "inv0": inv,
                  "opened_ts": time.time() - 3600, "stop_id": None, "floating": 0.0}
    p.position["hard_stop"] = p._hard_of(p.position)
    p.state = "В_ПОЗИЦИИ"
    return p.position


def review_answered(p):
    """Дежурный PRO ответил на перепроверке (плановой или по триггеру) — после пометок двери."""
    p._last_review_ts = time.time() + 1.0


# ══ D1: вид входа — по снимку, который видела модель ════════════════════════════════════════════════════════════
def test_entry_kind_by_snapshot_hint_and_passed_level():
    ek = mission._entry_kind
    assert ek("BUY", 300.0, 299.5, "прорыв") == "прорыв" and ek("BUY", 296.6, 297.0, "откат") == "откат"
    # подсказка не согласована со снимком: без допуска — геометрия снимка, с допуском «пройден рядом» — вид подсказки
    assert ek("BUY", 300.0, 300.6, "прорыв") == "откат"
    assert ek("BUY", 300.0, 300.6, "прорыв", passed_pct=0.01) == "прорыв", "пройденный прорыв"
    assert ek("BUY", 296.6, 296.4, "откат", passed_pct=0.01) == "откат", "пройденный откат"
    assert ek("BUY", 290.0, 300.0, "прорыв", passed_pct=0.01) == "откат", "далеко пройден — геометрия"
    assert ek("SELL", 100.0, 99.7, "прорыв", passed_pct=0.01) == "прорыв" and ek("SELL", 100.0, 99.7, "прорыв") == "откат"
    assert ek("BUY", 101.0, 100.0) == "прорыв" and ek("BUY", 99.0, 100.0, "breakout") == "откат"
    assert ek("BUY", 101.0, 100.0, "сейчас") == "сейчас" and ek("BUY", None, 100.0, "прорыв") == "сейчас"
    # приказ совета — как было (его цена и есть снимок, допуска нет)
    out, err = mission._validate_exec({"do": "BUY", "entry": 99.8, "entry_kind": "прорыв", "invalidation": 99.0}, "auto", 100)
    assert err is None and out["entry_kind"] == "откат", out


@pytest.mark.parametrize("do,snap,level,during,inv,take,ticks", [
    ("КУПИТЬ", 299.5, 300.0, 300.4, 298.5, 304.0, (300.5, 300.8)),    # S1: пробой пройден, пока PRO думал
    ("ПРОДАТЬ", 100.5, 100.0, 99.7, 101.2, 97.0, (99.6, 99.4)),       # зеркально
])
def test_review_breakout_passed_while_thinking_enters(fake, do, snap, level, during, inv, take, ticks):
    async def scenario():
        m, p = make_pilot()
        p.prices.append(snap)
        fake.during = lambda route: p.prices.append(during) if route == "mission_review" else None
        fake.queue("mission_review", {"choice": do, "entry": level, "entry_kind": "прорыв", "invalidation": inv,
                                      "take": take, "why": f"вход на пробитии {level:g}", "note": "т"})
        await p._review(snap)
        fake.during = None
        assert p.plan and p.plan["kind"] == "прорыв" and p.plan["entry"] == level and p.plan["snap_price"] == snap, p.plan
        assert "вход на пробитии" in p.last_action, p.last_action
        for px in ticks:
            await tick(p, px)
        assert entries(p) and fake.count("mission_entry") == 0, (p.last_action, p.broker.placed)

    asyncio.run(scenario())


def test_review_pullback_passed_while_thinking_enters_at_better_price_or_dies(fake):
    async def scenario():
        # S2: откат 296.6 при снимке 297.0; пока PRO думал — 296.4: засада, вход по лучшей цене (не «пробой вверх»)
        m, p = make_pilot()
        p.prices.append(297.0)
        fake.during = lambda route: p.prices.append(296.4) if route == "mission_review" else None
        fake.queue("mission_review", {"choice": "КУПИТЬ", "entry": 296.6, "entry_kind": "откат", "invalidation": 295.8,
                                      "take": 300.0, "why": "засада на откате к 296.6", "note": "т"})
        await p._review(297.0)
        fake.during = None
        assert p.plan and p.plan["kind"] == "откат" and p.plan["entry"] == 296.6, p.plan
        await tick(p, 296.4)
        assert entries(p) and entries(p)[0]["price"] < 296.6 and fake.count("mission_entry") == 0, p.last_action
        # тот же ответ, а пока думал — цена ушла за стоп 295.8: идея мертва до входа, входа нет
        m2, p2 = make_pilot()
        p2.prices.append(297.0)
        fake.during = lambda route: p2.prices.append(295.5) if route == "mission_review" else None
        fake.queue("mission_review", {"choice": "КУПИТЬ", "entry": 296.6, "entry_kind": "откат", "invalidation": 295.8,
                                      "take": 300.0, "why": "засада на откате к 296.6", "note": "т"})
        await p2._review(297.0)
        fake.during = None
        assert p2.plan is None and "идея мертва до входа" in p2.last_action, p2.last_action
        for px in (296.7, 296.9):
            await tick(p2, px)
        assert not entries(p2)

    asyncio.run(scenario())


def test_door_wait_breakout_level_kind_by_door_snapshot(fake):
    async def scenario():
        # S10: дверь ЖДАТЬ «прорыв 101» при снимке 100.6; пока думала — 101.3: вид — прорыв, не «откат @101»
        m, p = make_pilot()
        council(m, p, "BUY", None, None, take=106.0, inv=99.0, price=100.0)
        fake.during = lambda route: p.prices.append(101.3) if route == "mission_entry" else None
        fake.queue("mission_entry", {"decision": "ЖДАТЬ", "entry": 101.0, "entry_kind": "прорыв",
                                     "why": "вход на пробитии 101 по объёму"})
        await tick(p, 100.6)
        fake.during = None
        pl = p.plan
        assert pl["kind"] == "прорыв" and pl["entry"] == 101.0 and pl["src"] == "gate_level" and pl["snap_price"] == 100.6, pl
        for px in (101.4, 101.8):
            await tick(p, px)
        assert entries(p) and fake.count("mission_entry") == 1, p.last_action

    asyncio.run(scenario())


def test_door_wait_level_named_in_decision_word(fake):
    async def scenario():
        m, p = make_pilot()
        council(m, p, "BUY", None, None, take=106.0, inv=99.0, price=100.0)
        fake.queue("mission_entry", {"decision": "ЖДАТЬ ПРОБОЙ 101", "why": "объёма нет"})
        await tick(p, 100.2)
        if p.gates[-1]["decision"] == "ЖДАТЬ":                        # слово разобрано как ЖДАТЬ — уровень из него
            assert p.plan["entry"] == 101.0 and p.plan["kind"] == "прорыв" and p.plan["src"] == "gate_level", p.plan
        assert not entries(p)

    asyncio.run(scenario())


def test_reaffirm_while_door_thinks_keeps_enter(fake):
    async def scenario():
        # S14: пробой 300 пройден, дверь думает; перепроверка подтверждает ту же засаду при 300.6 — ВОЙТИ двери в силе
        m, p = make_pilot()
        council(m, p, "BUY", 300.0, "прорыв", take=306.0, inv=298.8, price=299.5)
        await tick(p, 299.6)
        state = {"done": False}

        async def during(route):
            if route == "mission_entry" and not state["done"]:
                state["done"] = True
                p.prices.append(300.6)
                fake.queue("mission_review", {"choice": "КУПИТЬ", "entry": 300.0, "entry_kind": "прорыв",
                                              "invalidation": 298.9, "take": 306.5, "why": "пробой 300 подтверждаю",
                                              "note": "т"})
                await p._review(300.2)
                assert p.plan["kind"] == "прорыв" and p.plan.get("crossed") == 300.0, p.plan
                assert "то же решение: план на месте" in p.last_action and "дверь уже думает" in p.last_action
        fake.during = during
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "пробой на объёме"})
        for px in (300.2, 300.3):
            await tick(p, px)
        fake.during = None
        assert entries(p) and p.gates[-1]["decision"] == "ВОЙТИ" and "сменил вход" not in p.gates[-1]["applied"], \
            (p.gates[-1], p.last_action)
        assert m.reviews[-1].get("applied", "").startswith("план на месте (то же решение)"), m.reviews[-1]

    asyncio.run(scenario())


def test_reaffirm_while_order_flies_keeps_order_new_level_cancels(fake):
    async def scenario():
        # S19: заявка входа в полёте, перепроверка подтверждает тот же пробой — заявка не снимается, её уровни — новые
        m, p = make_pilot(broker=Broker(hang=True))
        council(m, p, "BUY", 300.0, "прорыв", take=306.0, inv=298.8, price=299.5)
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "пробой на объёме"})
        for px in (300.2, 300.3):
            await tick(p, px)
        assert p.pending and p.plan.get("crossed") == 300.0
        n_break = p._break_n
        p.prices.append(300.35)
        fake.queue("mission_review", {"choice": "КУПИТЬ", "entry": 300.0, "entry_kind": "прорыв", "invalidation": 299.2,
                                      "take": 307.0, "why": "пробой 300 подтверждаю", "note": "т"})
        await p._review(300.35)
        assert not p._cancel_entry and p.plan["kind"] == "прорыв" and p.plan.get("crossed") == 300.0, p.plan
        assert p._break_n == n_break and p.pending["invalidation"] == 299.2 and p.pending["take"] == 307.0, p.pending
        assert "заявка в полёте не снята" in p.last_action, p.last_action
        # другой уровень той же стороны — новое решение: летящая заявка снимается
        fake.queue("mission_review", {"choice": "КУПИТЬ", "entry": 299.0, "entry_kind": "откат", "invalidation": 298.0,
                                      "take": 306.0, "why": "лучше на откате", "note": "т"})
        await p._review(300.35)
        assert p._cancel_entry and p.plan["entry"] == 299.0 and p.plan["kind"] == "откат", p.plan

    asyncio.run(scenario())


# ══ D3: стопы и выходы ИИ — по снимку; подмена видна ════════════════════════════════════════════════════════════
def test_review_stop_crossed_during_think_is_dead_idea_not_silent_substitution(fake):
    async def scenario():
        # S15: стоп ИИ 300.2 ниже снимка 300.8, а пока думал — 300.1: не аварийный стоп молча, а «идея мертва до входа»
        m, p = make_pilot()
        p.prices.append(300.8)
        p.review_ts = time.time() + 1800
        fake.during = lambda route: p.prices.append(300.1) if route == "mission_review" else None
        fake.queue("mission_review", {"choice": "КУПИТЬ", "entry": None, "entry_kind": "сейчас", "invalidation": 300.2,
                                      "take": 303.0, "why": "лонг, стоп под 300.2", "note": "т"})
        await p._review(300.8)
        fake.during = None
        assert p.plan is None and p.state == "ЖДУ_ПЛАН", p.plan
        assert "идея мертва до входа: цена 300.1 уже за стопом 300.2 (стоп ИИ; решение при 300.8)" in p.last_action
        assert "идея мертва до входа" in m.reviews[-1]["applied"] and "идея мертва до входа" in p.last_review["applied"]
        assert any(t[1] == "Вход не взведён: идея мертва до входа" for t in fake.told), fake.told
        assert "идея мертва до входа" in (p._review_reason or ""), "повод дежурному PRO"
        for px in (300.1, 299.9):
            await tick(p, px)
        assert not entries(p)

    asyncio.run(scenario())


def test_review_code_substitutions_are_visible(fake):
    async def scenario():
        m, p = make_pilot()
        p.prices.append(100.0)
        fake.queue("mission_review", {"choice": "КУПИТЬ", "entry": None, "why": "импульс", "take": 99.0, "note": "т"})
        await p._review(100.0)
        pl = p.plan
        assert pl and abs(pl["invalidation"] - 99.4) < 1e-6 and pl["take"] is None, pl
        assert "стоп ИИ не дан — код поставил аварийный 99.4" in p.last_action, p.last_action
        assert "тейк 99 не выше цены решения 100 — без тейка" in p.last_action
        assert "код поставил аварийный" in m.reviews[-1]["applied"]
        assert any(t[1] == "Решение перепроверки: как исполнено" and "аварийный" in t[2] for t in fake.told), fake.told

    asyncio.run(scenario())


def test_hold_stop_crossed_during_think_becomes_trigger_and_guard_asks(fake):
    async def scenario():
        # S16: ДЕРЖАТЬ «стоп к 301.2» при снимке 301.5, пока думал — 301.1: стоп принят как триггер, тик спросит у троса
        m, p = make_pilot()
        pos = long_pos(p, entry=300.0, inv=299.0, take=306.0)
        p.prices.append(301.5)
        fake.during = lambda route: p.prices.append(301.1) if route == "mission_review" else None
        fake.queue("mission_review", {"choice": "ДЕРЖАТЬ", "invalidation": 301.2, "take": None,
                                      "why": "прибыль не отдаём: стоп к 301.2", "note": "т"})
        await p._review(301.5)
        fake.during = None
        assert pos["invalidation"] == 301.2, pos
        rt = m.reviews[-1]["retune"]
        assert rt["applied"] == ["трос 299 → 301.2 — цена 301.1 уже за ним: ближайший тик спросит у троса"], rt
        fake.queue("mission_guard", {"decision": "ЖДАТЬ", "why": "вынос стопов"})
        await tick(p, 301.0)
        assert fake.count("mission_guard") == 1 and p.guards[-1]["decision"] == "ЖДАТЬ" and p.position is pos

    asyncio.run(scenario())


def test_profit_lock_by_snapshot_crossed_lock_is_trigger(fake):
    async def scenario():
        m, p = make_pilot()
        pos = long_pos(p, entry=100.0, inv=98.0, take=110.0)
        fake.during = lambda route: p.prices.append(103.8) if route == "mission_profit" else None
        fake.queue("mission_profit", {"decision": "ДЕРЖАТЬ", "why": "ход жив, но 104 не отдаём", "lock_price": 104.0})
        await tick(p, 106.5)                                         # 65 % хода — мысль о прибыли, снимок 106.5
        fake.during = None
        assert pos["profit_px"] == 106.5 and pos["invalidation"] == 104.0 and pos["profit_lock"], pos
        x = p.profits[-1]
        assert "прибыль заперта триггером 104" in x["applied"] and "цена 103.8 уже за ним" in x["applied"], x
        fake.queue("mission_guard", {"decision": "СЛИТЬ", "why": "прибыль не отдаём"})
        await tick(p, 103.8)
        assert fake.count("mission_guard") == 1 and p.position is None, p.last_action

    asyncio.run(scenario())


def test_topup_stop_by_snapshot_crossed_is_trigger_and_topup_not_armed(fake):
    async def scenario():
        m, p = make_pilot()
        pos = long_pos(p, entry=100.0, lots=2, inv=98.0, take=106.0)
        p.prices.append(100.5)
        fake.during = lambda route: p.prices.append(100.1) if route == "mission_review" else None
        fake.queue("mission_review", {"choice": "ДОБРАТЬ", "invalidation": 100.2, "why": "добрать, стоп 100.2", "note": "т"})
        await p._review(100.5)
        fake.during = None
        assert pos["invalidation"] == 100.2 and p.plan is None, (pos, p.plan)
        assert "добор не взведён: цена 100.1 уже за стопом позиции 100.2" in p.last_action, p.last_action
        assert "трос 98 → 100.2 — цена 100.1 уже за ним" in p.last_action

    asyncio.run(scenario())


# ══ C5: условный ВОЙТИ у двери — засада у уровня ═════════════════════════════════════════════════════════════════
def test_conditional_enter_far_level_becomes_ambush_then_enters_at_level(fake):
    async def scenario():
        m, p = make_pilot()
        council(m, p, "BUY", None, None, take=305.0, inv=294.0, price=297.5)
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "entry": 296.6, "entry_kind": "откат",
                                     "why": "войти на откате к 296.6 — лента выкупает там"})
        await tick(p, 297.5)
        assert not entries(p), "условный ВОЙТИ — не рыночный вход по 297.5"
        pl = p.plan
        assert pl["entry"] == 296.6 and pl["kind"] == "откат" and pl["src"] == "gate_level", pl
        g = p.gates[-1]
        assert g["decision"] == "ВОЙТИ" and "условный ВОЙТИ: уровень откат @296.6 далеко от цены 297.5" in g["applied"], g
        await tick(p, 297.0)
        assert not entries(p)
        await tick(p, 296.62)                                        # уровень — решение самого PRO, свежее: без вопроса
        assert entries(p) and fake.count("mission_entry") == 1, p.last_action
        # уровень у самой цены (≤ 0.1 %) — обычный ВОЙТИ по текущей цене
        m2, p2 = make_pilot()
        council(m2, p2, "BUY", None, None, take=305.0, inv=294.0, price=297.5)
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "entry": 297.3, "entry_kind": "откат", "why": "на откате"})
        await tick(p2, 297.5)
        assert entries(p2), p2.last_action
        # пройденный пробой — вход сейчас, не засада
        m3, p3 = make_pilot()
        council(m3, p3, "BUY", None, None, take=305.0, inv=294.0, price=297.5)
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "entry": 297.0, "entry_kind": "прорыв", "why": "при пробое 297"})
        await tick(p3, 297.5)
        assert entries(p3), p3.last_action

    asyncio.run(scenario())


def test_conditional_words_without_level_wait_for_review(fake):
    async def scenario():
        m, p = make_pilot()
        council(m, p, "BUY", None, None, take=110.0, inv=98.0, price=100.0)
        plan = p.plan
        assert p._gate_conditional({"decision": "ВОЙТИ ПОЗЖЕ"}, "ВОЙТИ ПОЗЖЕ", "", plan, 100.0, 100.0) == {"entry": None}
        assert p._gate_conditional({"decision": "ВОЙТИ НА ОТКАТЕ 99.2"}, "ВОЙТИ НА ОТКАТЕ 99.2", "", plan, 100.0, 100.0) \
            == {"entry": 99.2, "kind": "откат"}
        assert p._gate_conditional({"decision": "BUY_LIMIT", "entry": 99.5}, "BUY_LIMIT", "", plan, 100.0, 100.0) \
            == {"entry": 99.5, "kind": "откат"}
        assert p._gate_conditional({"decision": "ВОЙТИ", "entry": 99.5}, "ВОЙТИ", "импульс жив", plan, 100.0, 100.0) is None
        assert p._gate_conditional({"decision": "ВОЙТИ", "entry": 99.5, "entry_kind": "сейчас"}, "ВОЙТИ", "при 99.5",
                                   plan, 100.0, 100.0) is None
        fake.queue("mission_entry", {"decision": "ВОЙТИ ПОЗЖЕ", "why": "стакан пуст"})
        await tick(p, 100.0)
        assert not entries(p) and p.plan, p.last_action
        if p.gates[-1]["decision"] == "ВОЙТИ":                        # слово условия без уровня — как ЖДАТЬ без уровня
            assert p.plan.get("gate_review_ts") and "условный ВОЙТИ без уровня" in p.gates[-1]["applied"], p.gates[-1]

    asyncio.run(scenario())


# ══ D2: дрейф у двери — не больше одного переспроса на план ═══════════════════════════════════════════════════════
def _trend(p, fake, step_pct):
    def during(route):
        if route == "mission_entry":
            p.prices.append(round(p.prices[-1] * (1 + step_pct / 100.0), 4))
    fake.during = during


def test_drift_reask_once_then_enter_by_ratio(fake):
    async def scenario():
        # S3: тренд, каждая мысль двери +1.1 %: первый ВОЙТИ — один переспрос, второй ВОЙТИ исполняется
        m, p = make_pilot()
        council(m, p, "BUY", None, None, take=110.0, inv=98.0, price=100.0)
        _trend(p, fake, 1.1)
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "импульс жив"}, {"decision": "ВОЙТИ", "why": "идём"},
                   {"decision": "ВОЙТИ", "why": "лишний"})
        await tick(p, 100.0)
        assert "ВОЙТИ — переспрашиваю из-за дрейфа (1 раз)" in p.last_action and not entries(p), p.last_action
        assert p.plan["drift_asked"] == 1 and "ПОМЕТКА КОДА" not in fake.last_user("mission_entry")
        await tick(p, p.prices[-1])
        fake.during = None
        assert fake.count("mission_entry") == 2 and entries(p), p.last_action
        assert "ВОЙТИ исполнен после дрейфа" in p.gates[-1]["applied"], p.gates[-1]
        assert "ПОМЕТКА КОДА: с момента решения цена прошла 1.10 % в сторону сделки" in fake.last_user("mission_entry")

    asyncio.run(scenario())


def test_drift_second_enter_bad_ratio_is_played_out(fake):
    async def scenario():
        # S3b: пробой 101, стоп 100.3, тейк 106; второй ВОЙТИ при 103.3 — отношение < 1: «ход отыгран», к плановой
        m, p = make_pilot()
        council(m, p, "BUY", 101.0, "прорыв", take=106.0, inv=100.3, price=100.0)
        await tick(p, 100.5)
        review_at = p.review_ts
        _trend(p, fake, 1.05)
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "пробой"}, {"decision": "ВОЙТИ", "why": "держится"})
        for px in (101.1, 101.2):
            await tick(p, px)
        await tick(p, p.prices[-1])
        fake.during = None
        assert fake.count("mission_entry") == 2 and not entries(p) and p.plan is None, p.last_action
        g = p.gates[-1]
        assert "ход отыгран: вход не исполнен" in g["applied"] and "(тейк−цена)/(цена−стоп) = 0.8" in g["applied"], g
        assert "ход отыгран" in (p._review_reason or "") and p.review_ts == review_at, "повод — к плановой, PRO не тянем"
        assert any(t[1] == "Ход отыгран: вход не исполнен" for t in fake.told)
        await tick(p, 103.5)
        assert fake.count("mission_entry") == 2

    asyncio.run(scenario())


def test_review_drift_is_the_one_reask_then_door_enter_executes(fake):
    async def scenario():
        m, p = make_pilot()
        p.prices.append(100.0)
        fake.during = lambda route: p.prices.append(101.2) if route == "mission_review" else None
        fake.queue("mission_review", {"choice": "КУПИТЬ", "entry": None, "entry_kind": "сейчас", "invalidation": 97.0,
                                      "take": 110.0, "why": "импульс, вход сейчас", "note": "т"})
        await p._review(100.0)
        assert p.plan["drift_asked"] == 1 and p.plan["gate_note"], p.plan
        _trend(p, fake, 1.2)
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "да"})
        await tick(p, p.prices[-1])
        fake.during = None
        assert fake.count("mission_entry") == 1 and entries(p), p.last_action

    asyncio.run(scenario())


# ══ D7 ═══════════════════════════════════════════════════════════════════════════════════════════════════════════
def test_topup_with_level_arms_topup_ambush_now_is_honest(fake):
    async def scenario():
        m, p = make_pilot()
        p.broker.mx = {"buy": 3, "sell": 3}
        pos = long_pos(p, entry=100.0, lots=2, inv=98.0, take=106.0)
        p.prices.append(100.4)
        fake.queue("mission_review", {"choice": "ДОБРАТЬ", "entry": 99.2, "entry_kind": "откат", "invalidation": 98.0,
                                      "take": 106.0, "why": "добрать на откате к 99.2", "note": "т"})
        await p._review(100.4)
        assert p.plan["entry"] == 99.2 and p.plan["kind"] == "откат" and p.plan["src"] == "topup", p.plan
        assert "добор у уровня (откат) @99.2" in p.last_action, p.last_action
        await tick(p, 100.4)
        assert not entries(p), "добор у уровня, а не сейчас по 100.4"
        await tick(p, 99.21)
        assert entries(p) and entries(p)[-1]["price"] < 100.0 and fake.count("mission_entry") == 0, p.last_action
        # ДОБРАТЬ без уровня — честно «добор сейчас»
        m2, p2 = make_pilot()
        long_pos(p2, entry=100.0, lots=2)
        p2.prices.append(100.4)
        fake.queue("mission_review", {"choice": "ДОБРАТЬ", "why": "добрать", "note": "т"})
        await p2._review(100.4)
        assert p2.plan["entry"] is None and "добор сейчас" in p2.last_action, p2.last_action

    asyncio.run(scenario())


def test_breakout_without_level_is_now_with_honest_note(fake):
    async def scenario():
        m, p = make_pilot()
        p.prices.append(100.0)
        fake.queue("mission_review", {"choice": "КУПИТЬ", "entry": None, "entry_kind": "прорыв", "invalidation": 99.0,
                                      "take": 103.0, "why": "вход на пробитии максимума часа", "note": "т"})
        await p._review(100.0)
        assert p.plan["entry"] is None and p.plan["kind"] == "сейчас", p.plan
        assert "уровень пробоя не дан — вход сейчас" in p.last_action and \
            "уровень пробоя не дан — вход сейчас" in m.reviews[-1]["applied"], (p.last_action, m.reviews[-1])
        await tick(p, 100.0)
        assert entries(p) and fake.count("mission_entry") == 0

    asyncio.run(scenario())


def test_breakout_stop_beyond_decision_price_dies_before_break(fake):
    async def scenario():
        # S11: пробой 101, стоп 99.5 НИЖЕ цены совета 100 — цена ушла под стоп до пробоя: идея мертва, входа после нет
        m, p = make_pilot()
        council(m, p, "BUY", 101.0, "прорыв", take=104.0, inv=99.5, price=100.0)
        await tick(p, 99.4)
        assert p.plan is None and "мертва" in p.last_action, p.last_action
        for px in (100.0, 101.1, 101.2):
            await tick(p, px)
        assert not entries(p) and fake.count("mission_entry") == 0
        # классический стоп пробоя (между ценой и уровнем) до пробития спит — как было
        m2, p2 = make_pilot()
        council(m2, p2, "BUY", 101.0, "прорыв", take=104.0, inv=100.5, price=100.0)
        await tick(p2, 100.2)
        assert p2.plan and "мертва" not in p2.last_action, p2.last_action

    asyncio.run(scenario())


def test_topup_at_max_lots_goes_to_tolmach(fake):
    async def scenario():
        m, p = make_pilot()
        p.broker.mx = {"buy": 0, "sell": 5}
        long_pos(p, entry=100.0, lots=5)
        p._sized_by_broker = True
        p.prices.append(100.2)
        fake.queue("mission_review", {"choice": "ДОБРАТЬ", "why": "ещё", "note": "т"})
        await p._review(100.2)
        await tick(p, 100.2)
        assert p.plan is None and not entries(p) and "добор невозможен" in p.last_action, p.last_action
        assert any(t[0] == "topup" and t[1] == "Добор не исполнен: позиция уже на максимуме" for t in fake.told), fake.told

    asyncio.run(scenario())


# ══ мысль о прибыли ══════════════════════════════════════════════════════════════════════════════════════════════
def test_profit_answer_for_changed_position_closes_bus_stage(fake):
    async def scenario():
        m, p = make_pilot()
        long_pos(p, entry=100.0, inv=98.0, take=110.0)

        def during(route):
            if route == "mission_profit":
                p.position = None                                  # позиция закрыта (тейк/трос), пока PRO думал
        fake.during = during
        fake.queue("mission_profit", {"decision": "ВЫЙТИ", "why": "выдохся"})
        await tick(p, 106.5)
        fake.during = None
        st = [s for s in fake.stages if s[0] == "profit"]
        assert st and st[0][1] == "start" and st[-1][1] == "done", st
        assert "ответ выброшен: позиция уже закрыта" in st[-1][2] and "ВЫЙТИ" in st[-1][2], st[-1]

    asyncio.run(scenario())


def test_profit_thought_cancelled_by_close_closes_stage(fake):
    async def scenario():
        m, p = make_pilot()
        long_pos(p, entry=100.0, inv=98.0, take=110.0)
        hold = asyncio.Event()

        async def during(route):
            if route == "mission_profit":
                await hold.wait()                                  # PRO думает долго
        fake.during = during
        await p.tick(106.5, book(106.5))
        for _ in range(50):
            await asyncio.sleep(0)
        assert p.position and p.position.get("profit_busy"), p.last_action
        await p._close_all(106.5, "тест: трос", reanalyze=False)  # закрытие снимает мысль о прибыли
        await settle_bg(p)
        fake.during = None
        st = [s for s in fake.stages if s[0] == "profit"]
        assert st and st[-1][1] == "done" and "мысль о прибыли прервана: позиция закрыта" in st[-1][2], st

    asyncio.run(scenario())


def test_profit_silence_has_no_quick_retry(fake):
    async def scenario():
        m, p = make_pilot()
        pos = long_pos(p, entry=100.0, inv=98.0, take=110.0)
        fake.queue("mission_profit", FakeAI.SILENT)
        await tick(p, 106.5)
        x = p.profits[-1]
        assert x["decision"] == "НЕТ_ОТВЕТА" and x["silent"] and "решения не было" in x["why"], x
        assert pos["profit_next"] - time.time() > 800, "без быстрого повтора: полный пейсинг мысли о прибыли"
        await tick(p, 106.8)
        assert fake.count("mission_profit") == 1

    asyncio.run(scenario())


# ══ устойчивость: дверь и мысль о прибыли на случайных ответах не падают ═══════════════════════════════════════════
DOOR_ANSWERS = [
    {"decision": "ВОЙТИ"}, {"decision": "ВОЙТИ", "entry": 99.2, "entry_kind": "откат", "why": "на откате"},
    {"decision": "ВОЙТИ НА ОТКАТЕ 99.4"}, {"decision": "ВОЙТИ ПОЗЖЕ", "wait_minutes": 3}, {"decision": "BUY_LIMIT", "entry": "99,5"},
    {"decision": "ВОЙТИ", "entry": "abc", "entry_kind": "прорыв", "why": "если пробьёт"}, {"decision": "ЖДАТЬ"},
    {"decision": "ЖДАТЬ", "entry": 101.2, "entry_kind": "прорыв", "invalidation": 100.4, "take": 104.0},
    {"decision": "ЖДАТЬ", "wait_minutes": "7", "entry": 0}, {"decision": "ОТМЕНИТЬ", "council": False},
    {"decision": "???"}, "SILENT", {"decision": "ЖДАТЬ", "entry": 100.0, "invalidation": -1, "take": "x"},
]
PROFIT_ANSWERS = [
    {"decision": "ДЕРЖАТЬ", "lock_price": 103.0}, {"decision": "ДЕРЖАТЬ", "lock_price": "abc", "take": -2},
    {"decision": "ВЫЙТИ"}, {"decision": "ВЫЙТИ_И_ПЕРЕЗАЙТИ", "reentry": 102.0, "reentry_kind": "откат"},
    {"decision": "ВЫЙТИ_И_ПЕРЕЗАЙТИ", "reentry": "?", "reentry_kind": "прорыв"}, {"decision": "СОВЕТ", "lock_price": 101.0},
    {"decision": "может быть"}, "SILENT",
]


def test_door_and_profit_fuzz_no_exceptions(fake, caplog):
    import logging
    import random
    caplog.set_level(logging.WARNING, logger="pythia.mission")
    rng = random.Random(544)

    async def one():
        m, p = make_pilot(broker=Broker(hang=rng.random() < 0.3, reject=rng.choice([0, 0, 1])))
        px0 = round(100 + rng.uniform(-1.0, 1.0), 2)
        if rng.random() < 0.5:
            kind = rng.choice([None, "откат", "прорыв"])
            entry = None if kind is None else round(px0 + rng.choice([-1, 1]) * rng.uniform(0.2, 1.2), 2)
            do = rng.choice(["BUY", "SELL"])
            inv = round((min(px0, entry or px0) - 1.5) if do == "BUY" else (max(px0, entry or px0) + 1.5), 2)
            take = round((max(px0, entry or px0) + 3) if do == "BUY" else (min(px0, entry or px0) - 3), 2)
            council(m, p, do, entry, kind, take=take, inv=inv, price=px0)
            drift = rng.choice([0.0, 0.004, 0.012, -0.012])
            fake.during = lambda route: p.prices.append(round(p.prices[-1] * (1 + drift), 4)) \
                if route == "mission_entry" and p.prices else None
            for _ in range(3):
                a = rng.choice(DOOR_ANSWERS)
                fake.queue("mission_entry", a if a == "SILENT" else dict(a))
            for _ in range(4):
                await tick(p, round((p.prices[-1] if p.prices else px0) * (1 + rng.uniform(-0.006, 0.006)), 2))
                if rng.random() < 0.3:
                    review_answered(p)
        else:
            long_pos(p, entry=100.0, inv=98.0, take=rng.choice([105.0, 110.0]))
            fake.during = lambda route: p.prices.append(round(p.prices[-1] * (1 + rng.uniform(-0.02, 0.02)), 4)) \
                if route == "mission_profit" and p.prices else None
            a = rng.choice(PROFIT_ANSWERS)
            fake.queue("mission_profit", a if a == "SILENT" else dict(a))
            await tick(p, round(rng.uniform(103.5, 106.0), 2))
            await tick(p, round(rng.uniform(101.0, 106.0), 2))
        fake.during = None

    async def scenario():
        for _ in range(250):
            await one()

    asyncio.run(scenario())
    broken = [r.getMessage() for r in caplog.records if "не применил" in r.getMessage()]
    assert not broken, broken[:3]                                  # ответ у двери / мысль о прибыли упали бы молча


# ══ ритм PRO у двери ═════════════════════════════════════════════════════════════════════════════════════════════
def test_door_wait_without_level_or_term_waits_for_review(fake):
    async def scenario():
        m, p = make_pilot()
        council(m, p, "BUY", None, None, take=110.0, inv=98.0, price=100.0)
        fake.queue("mission_entry", {"decision": "ЖДАТЬ", "why": "спред 3×"})
        await tick(p, 100.0)
        assert p.plan.get("gate_review_ts") and not p.plan.get("gate_after"), p.plan
        assert "без уровня и срока — следующий вопрос у двери после ответа дежурного PRO" in p.gates[-1]["applied"]
        p.plan["gate_review_ts"] -= 3600                           # час спустя — без ответа перепроверки вопроса нет
        await tick(p, 100.1, n=3)
        assert fake.count("mission_entry") == 1 and "после ответа дежурного PRO на перепроверке" in p.last_action
        assert "следующий вопрос у двери после ответа дежурного PRO на перепроверке (этого)" in p._gate_line(p.plan)
        review_answered(p)
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "спред сжался"})
        await tick(p, 100.1)
        assert fake.count("mission_entry") == 2 and entries(p), p.last_action

    asyncio.run(scenario())


def test_door_timed_wait_at_least_ten_minutes_and_three_rounds(fake):
    async def scenario():
        m, p = make_pilot()
        council(m, p, "BUY", None, None, take=110.0, inv=98.0, price=100.0)
        for i in range(3):
            fake.queue("mission_entry", {"decision": "ЖДАТЬ", "why": f"минуту ({i})", "wait_minutes": 1})
            await tick(p, 100.0)
            assert 590 <= p.plan["gate_after"] - time.time() <= 601, p.plan
            assert f"срок {i + 1} из 3" in p.gates[-1]["applied"], p.gates[-1]
            p.plan["gate_after"] = 0.0
        assert p.plan.get("gate_review_ts") and "сроки у двери по этому плану исчерпаны" in p.gates[-1]["applied"]
        await tick(p, 100.0, n=2)
        assert fake.count("mission_entry") == 3, "после третьего срока — ждём перепроверку"
        review_answered(p)
        fake.queue("mission_entry", {"decision": "ЖДАТЬ", "why": "ещё минуту", "wait_minutes": 1})
        await tick(p, 100.0)
        assert fake.count("mission_entry") == 4 and not p.plan.get("gate_after") and p.plan.get("gate_review_ts")
        assert "сроков у двери по этому плану уже 3" in p.gates[-1]["applied"], p.gates[-1]

    asyncio.run(scenario())


def test_door_silence_waits_for_review_not_quick_retry(fake):
    async def scenario():
        m, p = make_pilot()
        council(m, p, "BUY", None, None, take=110.0, inv=98.0, price=100.0)
        fake.queue("mission_entry", FakeAI.SILENT)
        await tick(p, 100.0)
        g = p.gates[-1]
        assert g["decision"] == "НЕТ_ОТВЕТА" and g["silent"] and "после ответа дежурного PRO на перепроверке" in g["why"]
        assert not p.plan.get("gate_after") and p.plan.get("gate_review_ts") and p.plan["gate_silent"] == 1, p.plan
        p.plan["gate_review_ts"] -= 600                            # 10 минут спустя — не 120 с повтора
        await tick(p, 100.0, n=2)
        assert fake.count("mission_entry") == 1 and "не ответил у двери — решения не было" in p.last_action
        review_answered(p)
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "стакан ожил"})
        await tick(p, 100.0)
        assert fake.count("mission_entry") == 2 and entries(p) and "gate_silent" not in (p.plan or {}), p.last_action

    asyncio.run(scenario())


def test_exchange_reject_after_enter_retries_without_new_door_question(fake):
    async def scenario():
        # S17: ВОЙТИ у двери, биржа отбила (30042) — после бэкоффа заявка без нового вопроса PRO
        m, p = make_pilot(broker=Broker(reject=1))
        council(m, p, "BUY", None, None, take=104.0, inv=98.0, price=100.0)
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "да"}, {"decision": "ВОЙТИ", "why": "лишний вопрос"})
        await tick(p, 100.0)
        assert p.plan and not p.pending and p.plan["approved_until"] >= p.plan["ts"] + ai_pilot.PLAN_TTL_SEC - 1
        assert "у двери сказал ВОЙТИ" in p._gate_line(p.plan)
        p.no_entry_until = 0.0
        await tick(p, 100.05)
        assert fake.count("mission_entry") == 1 and entries(p), p.last_action

    asyncio.run(scenario())
