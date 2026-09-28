# -*- coding: utf-8 -*-
"""ПИФИЯ v5.4.2 «СВОБОДНЫЙ ПИЛОТ» — офлайн-регрессии базового пилота (backend/ai_pilot.py):
разбор решения перепроверки без молчаливого ЖДЁМ (_parse_choice → токен или None), killswitch по кругу, а не по
куску исполнения, шаг перепроверки после приказа WAIT (PYTHIA_WAIT_REVIEW_SEC), план «прорыв» не хоронится до
пробития, сроки у денег и допуск дрейфа — из конфига живьём. Ни сети, ни ключей, ни data/."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend import ai_pilot, config, mission, trader_risk

BOOK = {"best_bid": 99.9, "best_ask": 100.1}


class Broker:
    """Мини-биржа: заявки принимаются; состояние заявки выхода — по очереди states (исполнение частями);
    стопов на бирже нет (стопы в программе), портфель — dry-снимок."""
    mode = "real"

    def __init__(self, states=None):
        self.placed, self.stops, self.stop_cancels = [], [], []
        self.states = list(states or [])
        self._n = 0

    async def place(self, figi, direction, lots, price=None, tag="", **kw):
        self._n += 1
        oid = f"C-{self._n}"
        self.placed.append({"order_id": oid, "direction": direction, "lots": int(lots), "price": price, "tag": tag})
        return {"ok": True, "order_id": oid}

    async def order_state(self, oid, **kw):
        if self.states:
            return dict(self.states.pop(0))
        return {"ok": True, "filled": True, "status": "FILL"}

    async def cancel(self, oid, **kw):
        return {"ok": True}

    async def place_stop(self, figi, direction, lots, stop_price, tag="", **kw):
        self.stops.append(stop_price)
        return {"ok": True, "stop_order_id": f"S-{len(self.stops)}"}

    async def cancel_stop(self, sid):
        self.stop_cancels.append(sid)
        return {"ok": True}

    async def stop_orders(self, *, strict=False):
        return []

    async def max_lots(self, figi, price=None):
        return None

    async def portfolio(self):
        return {"mode": "dry", "positions": []}


def part(n):
    return {"ok": True, "filled": False, "status": "EXECUTION_REPORT_STATUS_PARTIALLYFILL", "exec_lots": n}


def full(n):
    return {"ok": True, "filled": True, "status": "EXECUTION_REPORT_STATUS_FILL", "exec_lots": n}


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_pilot, "market_clock", None)             # часы выключены: рынок открыт
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PYTHIA_EXCHANGE_STOP", False)
    monkeypatch.setattr(config, "PYTHIA_SOFT_STOP", True)
    monkeypatch.setattr(config, "PYTHIA_SOFT_TAKE", True)
    monkeypatch.setattr(config, "PYTHIA_ENTRY_CHECK", True)
    monkeypatch.setattr(config, "PYTHIA_PROFIT_THINK", True)
    monkeypatch.setattr(config, "PYTHIA_PARTNERS", False)


def make(cls=None, broker=None):
    p = (cls or ai_pilot.AIPilot)("TEST", deposit=100000.0, broker=broker or Broker())
    p.figi, p.asset_class = "F", "futures"
    p.go_per_lot, p.tick_size, p.point_value = 12000.0, 0.01, 1.0
    p.deposit = 100000.0
    p.session_risk = trader_risk.SessionRisk(100000.0)
    p._sr_day = p._msk_day()
    p._state_path = None
    p._prepared = True
    p.reanalyze_cb = AsyncMock()
    return p


def ex(do="BUY", entry=None, take=None, inv=None, why="тест", **kw):
    return {"exec": dict({"do": do, "entry": entry, "take": take, "invalidation": inv, "why": why}, **kw)}


def put_long(p, lots=3, entry=100.0, inv=98.0):
    p.position = {"side": "long", "entry": entry, "lots": lots, "take": None, "invalidation": inv, "inv0": inv,
                  "opened_ts": time.time() - 3600, "stop_id": None, "floating": 0.0}
    p.state = "В_ПОЗИЦИИ"
    return p.position


async def settle_bg(p, n=200):
    for _ in range(n):
        if not [t for t in p._background_tasks if not t.done()]:
            return
        await asyncio.sleep(0.005)


# ── 1. разбор решения перепроверки: слово ИИ — решение, непонятное — не ЖДЁМ, а «решения нет» ──────────────────
@pytest.mark.parametrize("raw, in_pos, side, want", [
    # вне позиции: латиница и синонимы — вход, а не ожидание
    ("КУПИТЬ_СЕЙЧАС", False, None, "КУПИТЬ_СЕЙЧАС"),
    ("BUY", False, None, "КУПИТЬ_СЕЙЧАС"),
    ("buy_now", False, None, "КУПИТЬ_СЕЙЧАС"),
    ("LONG", False, None, "КУПИТЬ_СЕЙЧАС"),
    ("ЛОНГ", False, None, "КУПИТЬ_СЕЙЧАС"),
    ("КУПИТЬ СЕЙЧАС!", False, None, "КУПИТЬ_СЕЙЧАС"),
    ("SELL", False, None, "ПРОДАТЬ_СЕЙЧАС"),
    ("SHORT", False, None, "ПРОДАТЬ_СЕЙЧАС"),
    ("ШОРТ", False, None, "ПРОДАТЬ_СЕЙЧАС"),
    ("ЖДЁМ", False, None, "ЖДЁМ"),
    ("ждем", False, None, "ЖДЁМ"),
    ("WAIT", False, None, "ЖДЁМ"),
    ("НОВЫЙ_АНАЛИЗ", False, None, "НОВЫЙ_АНАЛИЗ"),
    ("COUNCIL", False, None, "НОВЫЙ_АНАЛИЗ"),
    # в позиции: выход словами соседних узлов — выход, а не держать
    ("CLOSE", True, "long", "ЗАКРЫТЬ"),
    ("EXIT", True, "short", "ЗАКРЫТЬ"),
    ("ЗАФИКСИРОВАТЬ", True, "long", "ЗАКРЫТЬ"),
    ("ЗАКРЫТЬ ВСЁ", True, None, "ЗАКРЫТЬ"),
    ("СЛИТЬ", True, "long", "ЗАКРЫТЬ"),
    ("SELL", True, "long", "ЗАКРЫТЬ"),
    ("ПРОДАТЬ", True, "long", "ЗАКРЫТЬ"),
    ("BUY", True, "short", "ЗАКРЫТЬ"),
    ("BUY", True, "long", "ДОБРАТЬ"),
    ("КУПИТЬ ещё", True, "long", "ДОБРАТЬ"),
    ("продать ещё", True, "short", "ДОБРАТЬ"),
    ("ПЕРЕВЕРНУТЬ в шорт", True, "long", "ПЕРЕВЕРНУТЬ"),
    ("FLIP", True, "long", "ПЕРЕВЕРНУТЬ"),
    ("HOLD", True, "long", "ЖДЁМ"),
    ("ДЕРЖАТЬ, не закрывать", True, "long", "ЖДЁМ"),
    # решения нет: пусто, непонятно, отрицание, два решения, «войти» без стороны
    ("", False, None, None),
    ("", True, "long", None),
    ("   ", False, None, None),
    ("ВОЙТИ", False, None, None),
    ("бла-бла", False, None, None),
    ("НЕ ПОКУПАТЬ, ЖДЁМ", False, None, None),
    ("НЕ ЗАКРЫВАТЬ", True, "long", None),
    ("КУПИТЬ или ЖДАТЬ", False, None, None),
    ("КУПИТЬ", True, None, None),
])
def test_parse_choice_matrix(raw, in_pos, side, want):
    assert ai_pilot.AIPilot._parse_choice(raw, in_pos, side) == want


def test_legacy_review_keeps_old_fallback_hold():
    """Путь 4.x (AIPilot._review, PYTHIA_LEGACY_PILOT): непонятый ответ — как раньше ЖДЁМ (держим как есть)."""
    async def scenario():
        p = make()
        p._gather_news = AsyncMock(return_value="")
        p._last_verdict = AsyncMock(return_value="вердикт")
        p._ai_ask_json = AsyncMock(return_value={"choice": "бла-бла", "why": "?"})
        await p._review(100.0)
        assert p.last_review["choice"] == "ЖДЁМ" and p.plan is None
        p._ai_ask_json = AsyncMock(return_value={"choice": "BUY", "why": "пробой", "invalidation": 99.0})
        await p._review(100.0)
        assert p.last_review["choice"] == "КУПИТЬ_СЕЙЧАС" and p.plan and p.plan["side"] == "long"

    asyncio.run(scenario())


# ── 2. killswitch видит круг, а не кусок исполнения ──────────────────────────────────────────────────────────
def test_losing_exit_in_three_chunks_is_one_loss():
    async def scenario():
        b = Broker(states=[part(1), part(2), full(3)])
        p = make(broker=b)
        put_long(p, lots=3, entry=100.0)
        await p._close_all(99.0, "тест: выход", reanalyze=False)       # кусок 1
        assert p.position and p.position["lots"] == 2
        await p.tick(99.0, BOOK)                                       # кусок 2
        assert p.position and p.position["lots"] == 1
        await p.tick(99.0, BOOK)                                       # кусок 3 — круг закрыт
        assert p.position is None and len(b.placed) == 1, "одна заявка выхода"
        assert len(p.pnls) == 3 and sum(p.pnls) == pytest.approx(-3.0), "журнал — по кускам"
        sr = p.session_risk
        assert sr.streak == 1 and not sr.locked and sr.pnl == pytest.approx(-3.0), sr.state()
        assert "ЗАКРЫЛ ВСЁ" in p.last_action and "-3" in p.last_action
        # ещё два убыточных круга — серия 3 (тормоз жив, но считает сделки)
        for _ in range(2):
            put_long(p, lots=1, entry=100.0)
            await p._close_all(99.5, "тест: ещё выход", reanalyze=False)
            assert p.position is None
        assert sr.streak == 3 and sr.locked and "серия убытков 3" in (sr.reason or ""), sr.state()

    asyncio.run(scenario())


def test_winning_chunks_reset_streak_once():
    async def scenario():
        p = make(broker=Broker(states=[part(1), full(2)]))
        p.session_risk.streak = 2                                     # две убыточные сделки до этого
        put_long(p, lots=2, entry=100.0)
        await p._close_all(101.0, "тест: тейк", reanalyze=False)
        await p.tick(101.0, BOOK)
        assert p.position is None and len(p.pnls) == 2
        assert p.session_risk.streak == 0 and p.session_risk.pnl == pytest.approx(2.0)

    asyncio.run(scenario())


def test_reduce_in_two_chunks_is_one_record():
    async def scenario():
        p = make(broker=Broker(states=[part(1), full(2)]))
        pos = put_long(p, lots=5, entry=100.0)
        await p._reduce(pos, 2, 99.0, "дозор: ужатие")               # кусок 1 — запись ещё не отдана
        assert pos["lots"] == 4 and p.session_risk.streak == 0 and p.session_risk.pnl == 0.0
        await p._reduce(pos, 2, 99.0, "дозор: ужатие")               # кусок 2 — ужатие завершено
        assert pos["lots"] == 3 and p.position is pos and not pos.get("exit_order")
        assert p.session_risk.streak == 1 and p.session_risk.pnl == pytest.approx(-2.0) and len(p.pnls) == 2
        # позиция закрыта дальше одним куском: killswitch получает только новое (ужатие не задваивается)
        await p._close_all(99.0, "тест: закрыть остаток", reanalyze=False)
        assert p.position is None and p.session_risk.streak == 2
        assert p.session_risk.pnl == pytest.approx(-5.0) and sum(p.pnls) == pytest.approx(-5.0)

    asyncio.run(scenario())


def test_partial_exit_then_external_close_counts_once():
    """Выход исполнился частью, остаток закрыла биржа/владелец (сверка): один круг — одна запись, куски не теряются."""
    async def scenario():
        b = Broker(states=[part(1)])
        p = make(broker=b)
        pos = put_long(p, lots=3, entry=100.0, inv=98.0)
        await p._close_all(99.0, "тест: выход", reanalyze=False)
        assert pos["lots"] == 2 and p.session_risk.pnl == 0.0
        pos.pop("exit_order", None)                                  # заявка ушла (снята) — остаток закрыт вне петли
        b.portfolio = AsyncMock(return_value={"mode": "real", "cash": 99000.0, "positions": [{"figi": "F", "qty": 0}]})
        p.adopt_account = False
        p._tick_n = 1                                                # без запроса ГО к Tinkoff (сети нет)
        await p._reconcile(99.0)
        assert p.position is None
        sr = p.session_risk
        assert sr.streak == 1 and sr.pnl == pytest.approx(-1.0 + (98.0 - 100.0) * 2), sr.state()

    asyncio.run(scenario())


def test_risk_fed_survives_restart_record():
    """risk_fed пишется в state-файл рядом с exit_pnl_total — после рестарта круг не задваивается."""
    p = make()
    pos = put_long(p)
    pos["exit_pnl_total"], pos["risk_fed"] = -2.0, -2.0
    p._state_path = config.DATA_DIR / "aipilot_state.json"
    assert p._save_state()
    q = make()
    q._state_path = p._state_path
    q._restore_state(3)
    assert q.position and q.position.get("risk_fed") == -2.0 and q.position.get("exit_pnl_total") == -2.0
    q._finish_closed(q.position, "тест", False)
    assert q.session_risk.pnl == 0.0, "отданное до рестарта второй раз не пишется"


# ── 3. приказ WAIT: перепроверка через PYTHIA_WAIT_REVIEW_SEC, текст без «перевеса нет» ─────────────────────────
def test_wait_order_review_cadence(monkeypatch):
    monkeypatch.setattr(config, "PYTHIA_WAIT_REVIEW_SEC", 600, raising=False)     # живьём из конфига
    p = make()
    now = time.time()
    assert p.review_ts > now + 1000                                  # по умолчанию — плановый шаг
    assert p.adopt_forecast(ex("WAIT", wait_for="закрепление выше 101")) is True
    assert p.plan is None and p.state == "ЖДУ_ПЛАН"
    assert abs(p.review_ts - time.time() - 600) < 3, p.review_ts - time.time()
    assert p.last_action == "совет: вне рынка — ждал: закрепление выше 101; перепроверка через 10 мин", p.last_action
    # пустой wait_for — честно «условие не названо», никакого «перевеса нет»
    p.review_ts = time.time() - 1
    assert p.adopt_forecast(ex("WAIT", why=""))
    assert "ждал: условие не названо" in p.last_action and "перевеса нет" not in p.last_action
    assert abs(p.review_ts - time.time() - 600) < 3
    # перепроверку уже подтянули раньше (повод/событие) — WAIT её не отодвигает
    p.review_ts = time.time() + 90
    assert p.adopt_forecast(ex("WAIT", wait_for="x"))
    assert p.review_ts - time.time() < 91 and "перепроверка через 2 мин" in p.last_action, p.last_action
    # живой конфиг: сменили — следующий WAIT по новому шагу
    monkeypatch.setattr(config, "PYTHIA_WAIT_REVIEW_SEC", 300, raising=False)
    p.review_ts = time.time() + 5000
    assert p.adopt_forecast(ex("WAIT", wait_for="x")) and abs(p.review_ts - time.time() - 300) < 3


def test_wait_cadence_holds_across_reviews_and_ends_with_plan(monkeypatch):
    monkeypatch.setattr(config, "PYTHIA_WAIT_REVIEW_SEC", 600, raising=False)

    async def scenario():
        p = make()
        spawned = []

        async def fake_review(price):
            spawned.append(price)

        p._review_bg = fake_review
        assert p.adopt_forecast(ex("WAIT", wait_for="уровень 101"))
        p.review_ts = 0.0                                             # плановая пришла
        await p.tick(100.0, BOOK)
        await settle_bg(p)
        assert spawned == [100.0] and abs(p.review_ts - time.time() - 600) < 3, "вне рынка по WAIT — шаг короткий"
        assert not p.broker.placed, "WAIT — входа нет"
        # приказ стороны — шаг прежний (REVIEW_SEC)
        assert p.adopt_forecast(ex("BUY", entry=95.0, inv=94.0))
        assert p._review_gap() == ai_pilot.REVIEW_SEC
        # позиция при WAIT (на всякий случай) — плановый шаг
        q = make()
        put_long(q)
        assert q.adopt_forecast(ex("WAIT", wait_for="x")) and q.review_ts - time.time() > ai_pilot.REVIEW_SEC - 5

    asyncio.run(scenario())


def test_no_dummies_path_unchanged():
    p = make()
    assert p.adopt_forecast({"exec": {"do": "BUY"}}) is False                      # нет invalidation
    assert p.review_ts - time.time() <= 300 + 1 and "NO DUMMIES" in p.last_action


# ── 4. план «прорыв» до пробития не хоронится ──────────────────────────────────────────────────────────────
class BreakPilot(ai_pilot.AIPilot):
    """Вход на пробитии как у MissionPilot: только когда цена за уровнем; у двери — записать и не входить."""
    ready = True

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.gates = []

    def _entry_ready(self, price, lvl):
        if (self.plan or {}).get("kind") != "прорыв":
            return super()._entry_ready(price, lvl)
        beyond = price >= lvl if self.plan["side"] == "long" else price <= lvl
        return beyond and self.ready

    async def _entry_gate(self, price, book):
        self.gates.append(price)
        return False


def test_breakout_not_dead_before_level_then_dead_after():
    async def scenario():
        p = make(cls=BreakPilot)
        p.ready = False
        assert p.adopt_forecast(ex("BUY", entry=101.0, inv=100.5, take=104.0))
        p.plan["kind"] = "прорыв"
        for px in (100.0, 100.3, 100.7, 100.2):                      # уровень не пробит: цена и под стопом — жив
            await p.tick(px, BOOK)
            assert p.plan and p.state != "ЖДУ_ПЛАН" and "мертва" not in p.last_action, (px, p.last_action)
        p.reanalyze_cb.assert_not_awaited()
        await p.tick(101.1, BOOK)                                     # пробит (подтверждения ждём)
        assert p.plan and p.plan.get("crossed")
        await p.tick(100.4, BOOK)                                     # откат за стоп после пробоя — идея мертва
        assert p.plan is None and "мертва ДО входа" in p.last_action
        for _ in range(5):
            await asyncio.sleep(0)
        assert p.reanalyze_cb.await_count == 1

    asyncio.run(scenario())


def test_breakout_sell_and_crossing_goes_to_door():
    async def scenario():
        p = make(cls=BreakPilot)
        assert p.adopt_forecast(ex("SELL", entry=99.0, inv=99.5, take=96.0))
        p.plan["kind"] = "прорыв"
        await p.tick(100.0, BOOK)                                     # цена выше стопа, но уровень 99 не пробит
        assert p.plan and "мертва" not in p.last_action and p.gates == []
        await p.tick(98.9, BOOK)                                      # пробит вниз → проверка у двери
        assert p.plan and p.gates == [98.9]

    asyncio.run(scenario())


@pytest.mark.parametrize("kind, entry, px", [("откат", 99.5, 98.5), ("сейчас", None, 98.9), (None, 99.5, 98.5)])
def test_pullback_and_now_plans_dead_as_before(kind, entry, px):
    async def scenario():
        p = make(cls=BreakPilot)
        assert p.adopt_forecast(ex("BUY", entry=entry, inv=99.0, take=104.0))
        if kind:
            p.plan["kind"] = kind
        await p.tick(px, BOOK)
        assert p.plan is None and "мертва ДО входа" in p.last_action and p.gates == []

    asyncio.run(scenario())


def test_mission_pilot_breakout_waits_for_break(monkeypatch, tmp_path):
    """Наследник миссии: вход на пробитии 101 со стопом 100.5 при цене 100 — план жив, ждёт пробития."""
    monkeypatch.setattr(mission, "_M", {})
    monkeypatch.setattr(mission, "explain", None)
    monkeypatch.setattr(mission.store_v5, "mission_put", lambda ticker, data: None)
    monkeypatch.setattr(mission.store_v5, "trade_add", lambda rec: None)
    monkeypatch.setattr(mission.bus, "stage", AsyncMock())
    monkeypatch.setattr(mission.ledger, "schedule_after_close", Mock())
    monkeypatch.setattr(mission.market_ctx, "light", AsyncMock(return_value={"text": "живой рынок: тест"}))
    monkeypatch.setattr(mission, "_council_text", lambda: "итог совета: тест")
    monkeypatch.setattr(mission, "_scan_text", lambda m: "")
    monkeypatch.setattr(mission, "_mod", lambda name: None)
    monkeypatch.setattr(mission.ai_v5, "money_json", AsyncMock(side_effect=AssertionError("до пробития PRO не зовут")))
    monkeypatch.setattr(mission.ai_v5, "flash_json", AsyncMock(side_effect=AssertionError("FLASH не зовут")))

    async def scenario():
        m = mission.Mission("TEST", "Тест", "futures", "auto", 100000.0)
        mission._M["TEST"] = m
        p = mission.MissionPilot("TEST", deposit=100000.0, broker=Broker(), mission=m)
        p.figi, p.asset_class = "F", "futures"
        p.go_per_lot, p.tick_size, p.point_value, p.deposit = 12000.0, 0.01, 1.0, 100000.0
        p.session_risk = trader_risk.SessionRisk(100000.0)
        p._sr_day, p._state_path, p._prepared = p._msk_day(), None, True
        p.reanalyze_cb = AsyncMock()
        m.pilot = p
        m.task = SimpleNamespace(done=lambda: False)
        assert p.adopt_forecast(ex("BUY", entry=101.0, inv=100.5, take=104.0, entry_kind="прорыв"))
        assert p.plan and p.plan["kind"] == "прорыв"
        await p.tick(100.0, BOOK)
        await settle_bg(p)
        assert p.plan and p.position is None and p.pending is None and "мертва" not in p.last_action, p.last_action
        p.reanalyze_cb.assert_not_awaited()

    asyncio.run(scenario())


# ── 5. сроки у денег и допуск дрейфа — из конфига живьём ──────────────────────────────────────────────────────
def test_money_timeouts_and_drift_read_config_live(monkeypatch):
    monkeypatch.setattr(config, "PYTHIA_ENTRY_TIMEOUT_SEC", 1500, raising=False)
    monkeypatch.setattr(config, "PYTHIA_PROFIT_TIMEOUT_SEC", 900, raising=False)
    monkeypatch.setattr(config, "PYTHIA_ENTRY_DRIFT_PCT", 2.5, raising=False)
    assert ai_pilot.entry_timeout() == 1500.0 and ai_pilot.profit_timeout() == 900.0
    assert ai_pilot.drift_frac() == pytest.approx(0.025)
    # подмена константы модуля (стенд/тест) главнее конфига — старые подмены ENTRY_TIMEOUT продолжают работать
    monkeypatch.setattr(ai_pilot, "ENTRY_TIMEOUT", 0.3)
    monkeypatch.setattr(ai_pilot, "PROFIT_TIMEOUT", 0.2)
    monkeypatch.setattr(ai_pilot, "DRIFT_FRAC", 0.05)
    assert ai_pilot.entry_timeout() == 0.3 and ai_pilot.profit_timeout() == 0.2 and ai_pilot.drift_frac() == 0.05
