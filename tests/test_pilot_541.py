# -*- coding: utf-8 -*-
"""ПИФИЯ v5.4.1 «ТРЕЗВЫЙ ПИЛОТ» (W2): офлайн-регрессии пилота миссии на фейках — стопы только в программе
(PYTHIA_EXCHANGE_STOP), узлы у денег через ai_v5.money_json, проверка входа у двери (_entry_gate), мысль о прибыли
(_profit_watch), приказ WAIT, персист gates/profits через state-файл. Ни сети, ни ключей, ни data/."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend import ai_pilot, ai_v5, config, mission, trader_risk

PRICE = 100.0
BOOK = {"best_bid": 99.9, "best_ask": 100.1}


class Broker:
    """Мини-биржа: заявки исполняются сразу (или висят NEW), стопы ставятся/снимаются, портфеля нет (dry-снимок)."""
    mode = "real"

    def __init__(self):
        self.placed, self.stops, self.stop_cancels, self.cancelled = [], [], [], []
        self.fill = True
        self.cancel_stop_ok = True
        self.listed = None                    # None → GetStopOrders недоступен; список → активные стопы
        self.mx = None
        self._n = 0

    async def place(self, figi, direction, lots, price=None, tag="", **kw):
        self._n += 1
        oid = f"F-{self._n}"
        self.placed.append({"order_id": oid, "direction": direction, "lots": int(lots), "price": price, "tag": tag})
        return {"ok": True, "order_id": oid}

    async def order_state(self, oid, **kw):
        return {"ok": True, "filled": self.fill, "status": "FILL" if self.fill else "NEW", "exec_lots": 0}

    async def cancel(self, oid, **kw):
        self.cancelled.append(oid)
        return {"ok": True}

    async def place_stop(self, figi, direction, lots, stop_price, tag="", **kw):
        self.stops.append({"direction": direction, "lots": int(lots), "stop": stop_price})
        return {"ok": True, "stop_order_id": f"S-{len(self.stops)}"}

    async def cancel_stop(self, sid):
        self.stop_cancels.append(sid)
        return {"ok": True} if self.cancel_stop_ok else {"ok": False, "error": "timeout (тест)"}

    async def stop_orders(self, *, strict=False):
        if self.listed is None:
            raise RuntimeError("GetStopOrders недоступен (тест)")
        return list(self.listed)

    async def max_lots(self, figi, price=None):
        return dict(self.mx) if self.mx else None

    async def portfolio(self):
        return {"mode": "dry", "positions": []}


class FakeMoney:
    """ai_v5.money_json: очереди ответов по маршрутам, счётчик вызовов, «молчание» (TimeoutError) по маршруту."""
    SILENT = "SILENT"

    def __init__(self):
        self.answers: dict = {}
        self.calls: list = []
        self.errors: list = []

    def queue(self, route, *answers):
        self.answers.setdefault(route, []).extend(answers)

    def count(self, route):
        return sum(1 for r, _ in self.calls if r == route)

    async def money_json(self, system, user, *, route, max_tokens=None):
        self.calls.append((route, user))
        q = self.answers.get(route) or []
        a = q.pop(0) if q else {}
        if a == self.SILENT:
            raise asyncio.TimeoutError()
        return a

    def note_error(self, text, route=""):
        self.errors.append((route, text))


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """Всё внешнее — фейки; умолчания 5.4.1 (стопы в программе, проверка входа, мысль о прибыли, деньги на PRO)."""
    monkeypatch.setattr(mission, "_M", {})
    monkeypatch.setattr(mission, "explain", None)
    monkeypatch.setattr(mission.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mission.store_v5, "mission_put", lambda ticker, data: None)
    monkeypatch.setattr(mission.store_v5, "trade_add", lambda rec: TRADES.append(rec))
    monkeypatch.setattr(mission.ledger, "schedule_after_close", Mock())
    monkeypatch.setattr(mission.ledger, "estimate", lambda rec: 0.0)
    monkeypatch.setattr(mission.bus, "stage", AsyncMock())
    monkeypatch.setattr(mission.market_ctx, "light", AsyncMock(return_value={"text": "живой рынок: тест"}))
    monkeypatch.setattr(mission, "_council_text", lambda: "итог совета: тест")
    monkeypatch.setattr(mission, "_scan_text", lambda m: "")
    monkeypatch.setattr(mission, "_mod", lambda name: None)          # newsflow/watch/council недоступны → пусто
    monkeypatch.setattr(config, "PYTHIA_PARTNERS", False)
    monkeypatch.setattr(config, "PYTHIA_EXCHANGE_STOP", False)
    monkeypatch.setattr(config, "PYTHIA_ENTRY_CHECK", True)
    monkeypatch.setattr(config, "PYTHIA_PROFIT_THINK", True)
    monkeypatch.setattr(config, "PYTHIA_MONEY_MODEL", "pro")
    monkeypatch.setattr(config, "PYTHIA_SOFT_STOP", True)
    monkeypatch.setattr(config, "PYTHIA_SOFT_TAKE", True)
    monkeypatch.setattr(ai_pilot, "market_clock", None)             # часы выключены: рынок открыт
    fake = FakeMoney()
    monkeypatch.setattr(mission.ai_v5, "money_json", fake.money_json)
    monkeypatch.setattr(mission.ai_v5, "note_error", fake.note_error)
    monkeypatch.setattr(mission.ai_v5, "flash_json", AsyncMock(side_effect=AssertionError("узлы у денег идут через money_json")))
    TRADES.clear()
    return fake


TRADES: list = []


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
    m.task = SimpleNamespace(done=lambda: False)   # «пилот жив» для status/фазы
    return m, p


def ex(do="BUY", entry=None, take=110.0, inv=98.0, why="тест"):
    return {"exec": {"do": do, "entry": entry, "take": take, "invalidation": inv, "why": why}}


async def tick(p, price, n=1):
    for _ in range(n):
        await p.tick(price, dict(BOOK, best_bid=round(price - 0.1, 4), best_ask=round(price + 0.1, 4)))
        await settle_bg(p)


async def settle_bg(p, n=200):
    """Дождаться фоновых задач пилота (PRO у двери / мысль о прибыли / трос / совет)."""
    for _ in range(n):
        busy = [t for t in p._background_tasks if not t.done()]
        if not busy:
            return
        await asyncio.sleep(0.005)


async def open_position(p, inv=98.0, take=110.0, price=PRICE):
    assert p.adopt_forecast(ex("BUY", None, take, inv))
    await tick(p, price)          # проверка у двери (ВОЙТИ по умолчанию → {} → ЖДАТЬ!) — ставим ответ явно
    await tick(p, price)
    assert p.position, p.last_action
    return p.position


# ── приказ WAIT ──────────────────────────────────────────────────────────────────────────────────────────────
def test_validate_exec_wait_forms_and_position_rule():
    ex1, err = mission._validate_exec({"do": "WAIT", "wait_for": "закрепление выше 101", "why": "перевеса нет",
                                       "confidence": 40, "levels": [99, 101], "news_ids": ["a1"]}, "long", 100)
    assert err is None and ex1["do"] == "WAIT" and ex1["entry"] is None and ex1["entry_kind"] == "сейчас"
    assert ex1["take"] is None and ex1["invalidation"] is None and ex1["wait_for"] == "закрепление выше 101"
    assert ex1["confidence"] == 40 and ex1["levels"] == [99.0, 101.0] and ex1["news_ids"] == ["a1"] and ex1["why"] == "перевеса нет"
    for do in ("ЖДАТЬ", "FLAT", "HOLD_FLAT", "wait"):
        out, err = mission._validate_exec({"do": do, "invalidation": 98}, "short", 100)
        assert out and out["do"] == "WAIT" and err is None, (do, err)
    assert mission._validate_exec({"do": "WAIT"}, "auto", 100)[0]["wait_for"] == "перевеса нет — вне рынка"
    _, err = mission._validate_exec({"do": "WAIT", "why": "x"}, "auto", 100, in_pos=True)
    assert err and "позиция открыта" in err and "WAIT недопустим" in err
    _, err = mission._validate_exec({"do": "HOLD"}, "auto", 100)
    assert err and "нужно BUY, SELL или WAIT (CLOSE — только при открытой позиции)" in err and "флета нет" not in err
    _, err = mission._validate_exec({"do": "CLOSE"}, "auto", 100)
    assert err and "позиции нет" in err and "WAIT" in err


def test_adopt_wait_leaves_pilot_out_of_market_and_exec_text_shows_it():
    m, p = make_pilot()
    ex1, _ = mission._validate_exec({"do": "WAIT", "wait_for": "закрепление выше 101", "why": "перевеса нет"}, "auto", 100)
    assert p.adopt_forecast({"exec": ex1}) is True
    assert p.plan is None and p.state == "ЖДУ_ПЛАН" and "вне рынка — ждал: закрепление выше 101" in p.last_action
    assert abs(p.review_ts - time.time() - ai_pilot.wait_review_sec()) < 5   # v5.4.2: PYTHIA_WAIT_REVIEW_SEC, не REVIEW_SEC
    m.exec, m.exec_ts = ex1, time.time()
    txt = mission._exec_text(m)
    assert "WAIT — вне рынка, ждём: закрепление выше 101" in txt and "перевеса нет" in txt
    assert "ПРИКАЗ СОВЕТА: WAIT" in p._situation_text(100.0)
    assert mission.status("TEST")["phase"] == "idle" and mission.status("TEST")["exec"]["do"] == "WAIT"

    async def scenario():
        await tick(p, 100.0)
        assert p.pending is None and not p.broker.placed, "WAIT — входа нет"

    asyncio.run(scenario())


def test_resume_with_wait_order_does_not_adopt(monkeypatch):
    m = mission.Mission("TEST", "Тест", "futures", "auto", 100000.0)
    mission._M["TEST"] = m
    m.exec = {"do": "WAIT", "entry": None, "entry_kind": "сейчас", "take": None, "invalidation": None, "why": "нет перевеса",
              "plan": "", "wait_for": "уровень 101", "confidence": None, "news_ids": [], "levels": [], "time_note": ""}
    m.exec_ts = time.time()
    adopt = Mock()

    class FakePilot:
        def __init__(self, *a, **k):
            self.stopping = self.panic_flag = self._cancel_entry = False
            self.last_action = "поднят"
            self.reanalyze_cb = None
            self.broker = SimpleNamespace(mode="dry")
            self.adopt_forecast = adopt
            self.position = None

        async def run(self):
            await asyncio.Event().wait()          # петля «живёт», пока тест не снимет задачу

    monkeypatch.setattr(mission, "MissionPilot", FakePilot)
    monkeypatch.setattr(mission, "_scan_start", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(mission, "_state_file_position", lambda: None)
    monkeypatch.setattr(mission, "_restore_pending_panics", lambda: None)
    monkeypatch.setattr(mission.tinkoff, "enabled", lambda: True)
    monkeypatch.setattr(mission, "_make_broker", lambda: SimpleNamespace(mode="dry"))

    async def scenario():
        r = await mission.resume("TEST")
        try:
            assert r["ok"] and m.phase == "idle" and "WAIT" in r["note"], r
            adopt.assert_not_called()
        finally:
            m.task.cancel()
            await asyncio.gather(m.task, return_exceptions=True)

    asyncio.run(scenario())


# ── узлы у денег через money_json, имя модели в текстах ─────────────────────────────────────────────────────
def test_guard_take_triage_go_through_money_json_with_pro_texts(offline, monkeypatch):
    fake = offline

    async def scenario():
        m, p = make_pilot()
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "ок"})
        pos = await open_position(p, inv=98.0, take=110.0)
        assert p._money_name() == "PRO" and "триггер PRO @98" in p.last_action and "трос в программе @" in p.last_action
        fake.queue("mission_guard", {"decision": "ЖДАТЬ", "why": "ложный прокол", "hold_until_price": 97.0})
        await tick(p, 97.6)
        assert fake.count("mission_guard") == 1 and p.guards[-1]["model"] == "PRO" and pos["invalidation"] == 97.0
        assert "PRO решил ЖДАТЬ" in p.last_action and m.handoffs[-1]["kind"] == "stop" and "PRO решил ждать" in m.handoffs[-1]["reason"]
        p._reanalyzing = False
        fake.queue("mission_take", {"decision": "ПОДЕРЖАТЬ", "why": "импульс", "lock_price": 105.0, "tp_next": 115.0})
        await tick(p, 110.5)
        assert fake.count("mission_take") == 1 and p.guards[-1]["side"] == "take" and "PRO решил ПОДЕРЖАТЬ" in p.last_action
        p._reanalyzing = False
        pos["opened_ts"] -= 2000
        fake.queue("event_triage", {"urgency": "ПЛАНОВО", "action": None, "why": "в русле"})
        t = p._ask_review("серьёзная новость: тест", kind="news")
        assert t is not None and "PRO-триаж" in p.last_action
        await t
        assert fake.count("event_triage") == 1 and p.triages[-1]["model"] == "PRO" and "PRO-триаж: ПЛАНОВО" in p.last_action
        routes = [r for r, _ in fake.calls]
        assert routes == ["mission_entry", "mission_guard", "mission_take", "event_triage"], routes
        # молчание триажа → «PRO не ответил — дежурный PRO как раньше»
        monkeypatch.setattr(mission, "EVENT_TRIAGE_TIMEOUT", 0.2)
        fake.queue("event_triage", FakeMoney.SILENT)
        t = p._ask_review("серьёзная новость: ещё", kind="news")
        await t
        assert p.triages[-1]["urgency"] == "СЕЙЧАС" and "PRO не ответил" in p.triages[-1]["why"]
        # PYTHIA_MONEY_MODEL=flash — тексты говорят FLASH
        monkeypatch.setattr(config, "PYTHIA_MONEY_MODEL", "flash")
        assert p._money_name() == "FLASH" and ai_v5.money_model() == "flash" and p.status()["money_model"] == "flash"

    asyncio.run(scenario())


def test_timeouts_entry_profit_from_config_guard_take_ten_triage_five(monkeypatch):
    # v5.4.2: у двери и в мысли о прибыли срок — из конфига (PYTHIA_ENTRY_TIMEOUT_SEC / PYTHIA_PROFIT_TIMEOUT_SEC, живьём);
    # трос и тейк — 10 мин, триаж — 5 мин, как в 5.4.1
    assert ai_pilot.GUARD_TIMEOUT == 600.0 and ai_pilot.TAKE_TIMEOUT == 600.0
    assert ai_pilot.ENTRY_TIMEOUT == float(config.PYTHIA_ENTRY_TIMEOUT_SEC) == ai_pilot.entry_timeout()
    assert ai_pilot.PROFIT_TIMEOUT == float(config.PYTHIA_PROFIT_TIMEOUT_SEC) == ai_pilot.profit_timeout()
    assert ai_pilot.DRIFT_FRAC == float(config.PYTHIA_ENTRY_DRIFT_PCT) / 100 == ai_pilot.drift_frac()
    monkeypatch.setattr(config, "PYTHIA_ENTRY_TIMEOUT_SEC", 1800)
    monkeypatch.setattr(config, "PYTHIA_PROFIT_TIMEOUT_SEC", 1500)
    assert ai_pilot.entry_timeout() == 1800.0 and ai_pilot.profit_timeout() == 1500.0
    assert mission.EVENT_TRIAGE_TIMEOUT == 300.0
    assert {"mission_entry", "mission_profit"} <= set(ai_v5.MONEY_ROUTES)


# ── проверка входа у двери ──────────────────────────────────────────────────────────────────────────────────
def test_entry_gate_wait_with_level_then_enter(offline):
    fake = offline

    async def scenario():
        m, p = make_pilot()
        assert p.adopt_forecast(ex("BUY", None, 110.0, 98.0))
        fake.queue("mission_entry", {"decision": "ЖДАТЬ", "why": "взять на откате", "entry": 99.2, "entry_kind": "откат",
                                     "invalidation": 97.5, "take": 108.0})
        await p.tick(100.0, BOOK)
        assert p.plan["gate_busy"] and p.state == "У_ДВЕРИ" and "PRO проверяет вход" in p.last_action
        assert p.status()["entry_gate"]["busy"] is True and mission.status("TEST")["phase"] == "entering"
        await settle_bg(p)
        plan = p.plan
        assert not plan.get("gate_busy") and plan["entry"] == 99.2 and plan["kind"] == "откат"
        assert plan["invalidation"] == 97.5 and plan["take"] == 108.0 and p.state == "ЗАСАДА" and p.pending is None
        assert "plan" in fake.calls[0][1].lower() or "ПЛАН ПИЛОТА" in fake.calls[0][1]
        assert "long сейчас" in fake.calls[0][1] and "живой рынок: тест" in fake.calls[0][1] and "итог совета: тест" in fake.calls[0][1]
        g = p.gates[-1]
        assert g["decision"] == "ЖДАТЬ" and g["entry"] == 99.2 and g["plan_ts"] == plan["ts"] and g["model"] == "PRO" and not g["silent"]
        st = p.status()["entry_gate"]
        assert st["decision"] == "ЖДАТЬ" and st["entry"] == 99.2 and st["entry_kind"] == "откат" and st["checks"] == 1 and st["next_in_s"] is None
        await tick(p, 99.6)
        assert fake.count("mission_entry") == 1 and p.pending is None, "выше уровня — PRO не дёргаем"
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "выкупают"})
        await tick(p, 99.21)
        assert fake.count("mission_entry") == 2 and p.pending and p.pending["invalidation"] == 97.5 and p.pending["take"] == 108.0
        assert "ПРОШЛЫЕ ОТВЕТЫ У ДВЕРИ" in fake.calls[-1][1] and "ЖДАТЬ — взять на откате" in fake.calls[-1][1]
        assert p.gates[-1]["decision"] == "ВОЙТИ" and "бью" in p.gates[-1]["applied"] and "ВОЙТИ" in p.last_action
        await tick(p, 99.21)
        assert p.position and p.position["invalidation"] == 97.5 and p.plan is None and p.status()["entry_gate"] is None
        assert "gates" in p._state_extra() and len(p._state_extra()["gates"]) == 2

    asyncio.run(scenario())


def test_entry_gate_wait_without_level_uses_wait_minutes_or_cooldown(offline, monkeypatch):
    fake = offline
    monkeypatch.setattr(config, "PYTHIA_ENTRY_CHECK_COOL_SEC", 120)

    async def scenario():
        m, p = make_pilot()
        assert p.adopt_forecast(ex("BUY"))
        fake.queue("mission_entry", {"decision": "ЖДАТЬ", "why": "подождать открытия США", "wait_minutes": 15})
        await tick(p, 100.0)
        plan = p.plan
        assert plan["entry"] is None and 890 <= plan["gate_after"] - time.time() <= 900 and p.gates[-1]["wait_minutes"] == 15
        await tick(p, 100.0)
        assert fake.count("mission_entry") == 1 and "велел ждать до" in p.last_action and p.state == "ЗАСАДА"
        assert 880 <= p.status()["entry_gate"]["next_in_s"] <= 900
        # ЖДАТЬ без уровня и без срока → PYTHIA_ENTRY_CHECK_COOL_SEC
        plan["gate_after"] = 0.0
        fake.queue("mission_entry", {"decision": "ЖДАТЬ", "why": "мутно"})
        await tick(p, 100.0)
        assert plan["entry"] is None and 110 <= plan["gate_after"] - time.time() <= 120, plan
        # уровень принят, а кривой стоп (выше уровня) — нет: план хранит прежний стоп
        plan["gate_after"] = 0.0
        fake.queue("mission_entry", {"decision": "ЖДАТЬ", "why": "на откате", "entry": 99.0, "entry_kind": "откат", "invalidation": 99.5})
        await tick(p, 100.0)
        assert plan["entry"] == 99.0 and plan["kind"] == "откат" and plan["invalidation"] == 98.0, plan
        assert "не с той стороны" in p.last_action and "gate_after" not in plan, p.last_action
        # уровень, при котором стоп плана оказывается не с той стороны (прорыв 97 при стопе 98) — отвергнут, срок COOL
        plan["gate_after"] = 0.0
        fake.queue("mission_entry", {"decision": "ЖДАТЬ", "why": "ниже", "entry": 97.0, "entry_kind": "откат"})
        await tick(p, 99.02)                                        # у уровня 99 — вторая проверка
        assert plan["entry"] == 99.0 and "отвергнут" in p.last_action and 110 <= plan["gate_after"] - time.time() <= 120, p.last_action
        # срок ЖДАТЬ зажат снизу минутой и сверху 4 часами
        plan["gate_after"] = 0.0
        fake.queue("mission_entry", {"decision": "ЖДАТЬ", "why": "долго", "wait_minutes": 600})
        await tick(p, 100.0)
        assert plan["gate_after"] - time.time() <= ai_pilot.GATE_WAIT_MAX_SEC + 1

    asyncio.run(scenario())


def test_entry_gate_cancel_with_and_without_council(offline):
    fake = offline

    async def scenario():
        m, p = make_pilot()
        p.review_ts = time.time() + 1800
        assert p.adopt_forecast(ex("BUY"))
        fake.queue("mission_entry", {"decision": "ОТМЕНИТЬ", "why": "идея отыграна"})
        await tick(p, 100.0)
        assert p.plan is None and p.state == "ЖДУ_ПЛАН" and not p.broker.placed
        assert m.handoffs[-1]["kind"] == "pilot" and m.handoffs[-1]["deferred"] and "вход отменён PRO у двери" in m.handoffs[-1]["reason"]
        assert p.review_ts >= time.time() + 1790 and "идея отыграна" in (p._review_reason or "")
        p.reanalyze_cb.assert_not_awaited()
        assert p.adopt_forecast(ex("BUY"))
        fake.queue("mission_entry", {"decision": "ОТМЕНИТЬ", "why": "нужен совет", "council": True})
        await tick(p, 100.0)
        assert p.plan is None and m.handoffs[-1]["kind"] == "entry" and not m.handoffs[-1]["deferred"]
        p.reanalyze_cb.assert_awaited_once()
        assert p.gates[-1]["council"] is True and "зову полный совет" in p.gates[-1]["applied"]

    asyncio.run(scenario())


def test_entry_gate_silence_and_unknown_answer_block_entry_then_retry(offline, monkeypatch):
    fake = offline
    monkeypatch.setattr(config, "PYTHIA_ENTRY_CHECK_COOL_SEC", 60)

    async def scenario():
        m, p = make_pilot()
        assert p.adopt_forecast(ex("BUY"))
        fake.queue("mission_entry", FakeMoney.SILENT)
        await tick(p, 100.0)
        assert p.pending is None and p.plan and 50 <= p.plan["gate_after"] - time.time() <= 60
        g = p.gates[-1]
        assert g["decision"] == "ЖДАТЬ" and g["silent"] and "PRO не ответил" in g["why"] and "таймаут" in g["why"]
        assert fake.errors and fake.errors[-1][0] == "mission_entry"
        await tick(p, 100.0)
        assert fake.count("mission_entry") == 1, "до срока PRO не спрашиваем"
        p.plan["gate_after"] = 0.0
        fake.queue("mission_entry", {"decision": "?!"})
        await tick(p, 100.0)
        assert p.pending is None and p.gates[-1]["silent"] and "непонятно" in p.gates[-1]["why"] and len(fake.errors) == 2
        p.plan["gate_after"] = 0.0
        fake.queue("mission_entry", {"decision": "войти", "why": "ок"})
        await tick(p, 100.0)
        assert p.pending and fake.count("mission_entry") == 3

    asyncio.run(scenario())


def test_entry_gate_answer_for_changed_plan_is_dropped(offline):
    fake = offline

    async def scenario():
        m, p = make_pilot()
        assert p.adopt_forecast(ex("BUY"))
        gate = asyncio.Event()

        async def slow(system, user, *, route, max_tokens=None):
            await gate.wait()
            fake.calls.append((route, user))
            return {"decision": "ВОЙТИ", "why": "поздно"}

        fake.money_json = slow
        mission.ai_v5.money_json = slow
        await p.tick(100.0, BOOK)
        assert p.plan["gate_busy"]
        assert p.adopt_forecast(ex("SELL", None, 90.0, 102.0))       # план сменился, пока PRO думал
        gate.set()
        await settle_bg(p)
        assert p.pending is None and not p.broker.placed and p.plan["side"] == "short"
        assert p.gates[-1]["decision"] == "ВОЙТИ" and "план сменился" in p.gates[-1]["applied"]
        assert p.status()["entry_gate"]["checks"] == 0, "ответ по старому плану к новому не относится"

    asyncio.run(scenario())


def test_entry_gate_enter_does_not_chase_adverse_drift(offline):
    fake = offline

    async def scenario():
        m, p = make_pilot()
        assert p.adopt_forecast(ex("BUY"))
        gate = asyncio.Event()

        async def slow(system, user, *, route, max_tokens=None):
            await gate.wait()
            fake.calls.append((route, user))
            return {"decision": "ВОЙТИ", "why": "ок"}

        mission.ai_v5.money_json = slow
        await p.tick(100.0, BOOK)                                   # снимок для PRO — 100
        assert p.plan["gate_busy"]
        await p.tick(101.5, dict(BOOK, best_bid=101.4, best_ask=101.6))   # пока думал — цена ушла на +1.5 % (хуже для BUY)
        gate.set()
        await settle_bg(p)
        plan = p.plan
        assert p.pending is None and not p.broker.placed, "за ценой не гонимся"
        assert plan["entry"] == 100.0 and plan["kind"] == "откат" and p.state == "ЗАСАДА", plan
        assert "не гонюсь" in p.gates[-1]["applied"] and "хуже снимка 100" in p.last_action, p.last_action
        # цена вернулась к снимку → у уровня новая проверка → ВОЙТИ → вход
        mission.ai_v5.money_json = fake.money_json
        plan["gate_after"] = 0.0
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "вернулась"})
        await tick(p, 100.03)
        assert p.pending and fake.count("mission_entry") == 2      # первый вызов — медленный ответ ВОЙТИ, второй — у уровня

    asyncio.run(scenario())


def test_entry_gate_off_enters_immediately_and_topup_skips_gate(offline, monkeypatch):
    fake = offline

    async def scenario():
        monkeypatch.setattr(config, "PYTHIA_ENTRY_CHECK", False)
        b = Broker()
        b.mx = {"buy": 15, "sell": 15}
        m, p = make_pilot(b)
        p.deposit_override = None
        assert p.adopt_forecast(ex("BUY"))
        await tick(p, 100.0)
        assert p.pending and p.pending["lots"] == 15 and fake.count("mission_entry") == 0 and p.status()["entry_check"] is False
        await tick(p, 100.0)
        assert p.position and p.position["topup_left"] == 2
        monkeypatch.setattr(config, "PYTHIA_ENTRY_CHECK", True)
        b.mx = {"buy": 2, "sell": 0}
        p.position["last_fill_ts"] -= ai_pilot.TOPUP_GAP_SEC + 1
        p._tick_n = 5
        await tick(p, 100.0)                                       # авто-добор по округлению биржи — без проверки
        assert p.pending and p.pending.get("topup") and fake.count("mission_entry") == 0
        await tick(p, 100.0)
        p.plan = {"side": "long", "entry": None, "kind": "сейчас", "take": 110.0, "invalidation": 98.0, "why": "ДОБРАТЬ", "ts": time.time()}
        fake.queue("mission_entry", {"decision": "ОТМЕНИТЬ", "why": "хватит"})
        await tick(p, 100.0)                                       # добор по решению PRO — через проверку
        assert fake.count("mission_entry") == 1 and p.plan is None and p.position["lots"] == 17

    asyncio.run(scenario())


# ── мысль о прибыли ─────────────────────────────────────────────────────────────────────────────────────────
def test_profit_watch_threshold_hold_lock_and_pacing(offline, monkeypatch):
    fake = offline
    monkeypatch.setattr(config, "PYTHIA_PROFIT_THINK_COOL_SEC", 600)

    async def scenario():
        m, p = make_pilot()
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "ок"})
        pos = await open_position(p, inv=98.0, take=110.0)
        entry = pos["entry"]
        await tick(p, entry + 5.0)
        assert fake.count("mission_profit") == 0 and p.status()["profit"]["progress_pct"] < 60
        fake.queue("mission_profit", {"decision": "ДЕРЖАТЬ", "why": "ход жив", "lock_price": 103.0, "take": 111.0})
        await tick(p, entry + 6.5)
        assert fake.count("mission_profit") == 1
        x = p.profits[-1]
        assert x["decision"] == "ДЕРЖАТЬ" and "пройдено" in x["reason"] and x["model"] == "PRO" and x["lock_price"] == 103.0
        assert pos["invalidation"] == 103.0 and pos["inv0"] == 103.0 and pos["profit_lock"] and pos["take"] == 111.0
        assert entry <= pos["hard_stop"] < 103.0, "трос от нового триггера, но не ниже входа"
        assert "прибыль заперта триггером 103" in x["applied"] and "цель 110 → 111" in x["applied"]
        assert 590 <= pos["profit_next"] - time.time() <= 600 and p.status()["profit"]["locked"] is True
        u = fake.calls[-1][1]
        assert "ПРИБЫЛЬ: пройдено" in u and "УРОВНИ ПОЗИЦИИ" in u and "живой рынок: тест" in u
        await tick(p, entry + 8.0)
        assert fake.count("mission_profit") == 1, "пейсинг PYTHIA_PROFIT_THINK_COOL_SEC"
        assert 580 <= p.status()["profit"]["next_in_s"] <= 600
        # у нового триггера — PRO у троса; ЖДАТЬ двигает триггер только выше троса (трос от запертой прибыли не ниже входа)
        hard = pos["hard_stop"]
        fake.queue("mission_guard", {"decision": "ЖДАТЬ", "why": "отскочит", "hold_until_price": 101.0})
        await tick(p, 102.9)
        assert fake.count("mission_guard") == 1 and pos["invalidation"] == 103.0 and pos["hard_stop"] == hard >= entry, pos
        p._reanalyzing = False
        pos["guard_next"] = 0.0
        fake.queue("mission_guard", {"decision": "ЖДАТЬ", "why": "отскочит", "hold_until_price": round(hard + 0.2, 2)})
        await tick(p, 102.9)
        assert fake.count("mission_guard") == 2 and pos["invalidation"] == round(hard + 0.2, 2) and pos["hard_stop"] == hard
        # свежий приказ той же стороны снимает запертую прибыль: стоп совета главнее
        p._reanalyzing = False
        assert p.adopt_forecast(ex("BUY", None, 115.0, 99.0))
        assert not pos.get("profit_lock") and pos["invalidation"] == 99.0 and pos["hard_stop"] < 99.0

    asyncio.run(scenario())


def test_profit_exit_reenter_and_bad_level(offline):
    fake = offline

    async def scenario():
        m, p = make_pilot()
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "ок"})
        pos = await open_position(p, inv=98.0, take=110.0)
        entry = pos["entry"]
        fake.queue("mission_profit", {"decision": "ВЫЙТИ_И_ПЕРЕЗАЙТИ", "why": "рывок", "reentry": 103.0, "reentry_kind": "откат", "take": 112.0})
        await tick(p, entry + 7.0)
        assert p.position is None and p.pnls[-1] > 0 and TRADES and "выйти и перезайти" in TRADES[-1]["why"]
        plan = p.plan
        assert plan and plan["entry"] == 103.0 and plan["kind"] == "откат" and plan["invalidation"] == 98.0 and plan["take"] == 112.0
        assert p.state == "ЗАСАДА" and "план перезайти" in p.last_action and p.profits[-1]["decision"] == "ВЫЙТИ_И_ПЕРЕЗАЙТИ"
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "откат выкупили"})
        await tick(p, 103.02)
        assert p.pending, "вход по плану перезахода — через проверку у двери"
        await tick(p, 103.02)
        pos2 = p.position
        assert pos2 and pos2["take"] == 112.0 and pos2["invalidation"] == 98.0
        # уровень не с той стороны (откат выше цены) → как ВЫЙТИ: закрыто, плана нет
        pos2["profit_next"] = 0.0
        fake.queue("mission_profit", {"decision": "ВЫЙТИ_И_ПЕРЕЗАЙТИ", "why": "ещё", "reentry": 111.0, "reentry_kind": "откат"})
        await tick(p, 109.0)
        assert p.position is None and p.plan is None and "перезайти негде" in TRADES[-1]["why"]
        assert "не с той стороны" in p.profits[-1]["applied"]
        # ВЫЙТИ — закрыто, обычный ход после закрытия (повод дежурному PRO, остыть)
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "ок"})
        p.pnls, p.session_risk = [], trader_risk.SessionRisk(100000.0)
        pos3 = await open_position(p, inv=98.0, take=110.0)
        fake.queue("mission_profit", {"decision": "ВЫЙТИ", "why": "выдохся"})
        await tick(p, pos3["entry"] + 7.0)
        assert p.position is None and "PRO решил выйти" in TRADES[-1]["why"] and p.last_action.startswith("ЗАКРЫЛ ВСЁ (мысль о прибыли")
        assert m.handoffs[-1]["kind"] == "pilot" and "после закрытия" in m.handoffs[-1]["reason"]

    asyncio.run(scenario())


def test_profit_council_and_silence(offline, monkeypatch):
    fake = offline

    async def scenario():
        m, p = make_pilot()
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "ок"})
        pos = await open_position(p, inv=98.0, take=110.0)
        entry = pos["entry"]
        fake.queue("mission_profit", {"decision": "СОВЕТ", "why": "спорно", "lock_price": 104.0})
        await tick(p, entry + 7.0)
        assert p.profits[-1]["decision"] == "СОВЕТ" and pos["invalidation"] == 104.0 and pos["profit_lock"]
        assert m.handoffs[-1]["kind"] == "profit" and not m.handoffs[-1]["deferred"] and "зовёт Совет" in m.handoffs[-1]["reason"]
        p.reanalyze_cb.assert_awaited_once()
        assert p._council_kind == "profit"
        # переворот по такому совету — без тишины
        pos["opened_ts"] = time.time()
        assert p.adopt_forecast(ex("SELL", None, 95.0, 108.0)) is True and p.plan["side"] == "short"
        p.plan = None
        # молчание → ДЕРЖАТЬ по правилу, ошибка в панель
        monkeypatch.setattr(ai_pilot, "PROFIT_TIMEOUT", 0.2)
        pos["profit_next"] = 0.0
        fake.queue("mission_profit", FakeMoney.SILENT)
        await tick(p, entry + 7.5)
        x = p.profits[-1]
        assert x["decision"] == "ДЕРЖАТЬ" and x["silent"] and "PRO не ответил" in x["why"] and p.position is pos
        assert fake.errors[-1][0] == "mission_profit"

    asyncio.run(scenario())


def test_shock_in_our_favour_goes_to_profit_not_triage(offline):
    fake = offline

    async def scenario():
        m, p = make_pilot()
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "ок"})
        pos = await open_position(p, inv=98.0, take=120.0)
        pos["opened_ts"] -= 1300
        t0 = time.time()
        p._px_hist = [(t0 - 400, 100.0), (t0 - 300, 100.0), (t0 - 200, 100.0), (t0 - 100, 100.0)]
        p.review_ts, p._last_review_ts = t0 + 1800, t0
        n_h = len(m.handoffs)
        fake.queue("mission_profit", {"decision": "ДЕРЖАТЬ", "why": "держим"})
        await tick(p, 102.0)
        assert fake.count("mission_profit") == 1 and fake.count("event_triage") == 0 and len(m.handoffs) == n_h
        assert "рывок в нашу сторону +2.00%" in p.profits[-1]["reason"] and p.review_ts >= t0 + 1790
        # против позиции → триаж как раньше
        t1 = time.time()
        p._px_hist = [(t1 - 400, 104.0), (t1 - 300, 104.0), (t1 - 200, 104.0), (t1 - 100, 104.0)]
        p._last_shock_ts = 0.0
        fake.queue("event_triage", {"urgency": "ПЛАНОВО", "why": "в русле"})
        await tick(p, 101.9)
        assert fake.count("event_triage") == 1 and fake.count("mission_profit") == 1 and m.handoffs[-1]["kind"] == "shock"

    asyncio.run(scenario())


# ── персист gates/profits и мысли о прибыли через state-файл ────────────────────────────────────────────────
def test_gates_and_profits_survive_restart(tmp_path):
    m, p = make_pilot()
    p.gates.append({"ts": time.time(), "decision": "ЖДАТЬ", "why": "тест", "plan_ts": 1.0})
    p.profits.append({"ts": time.time() - ai_pilot.PILOT_EXTRA_TTL_SEC - 5, "decision": "ДЕРЖАТЬ", "why": "старая"})
    p.profits.append({"ts": time.time(), "decision": "ДЕРЖАТЬ", "why": "свежая"})
    p.position = {"side": "long", "entry": 100.0, "lots": 8, "take": 110.0, "invalidation": 98.0, "opened_ts": time.time(),
                  "stop_id": None, "floating": 0.0, "profit_next": time.time() + 500, "profit_lock": True, "profit_last": time.time()}
    p._set_levels(p.position, 110.0, 98.0)
    p._state_path = tmp_path / "s.json"
    assert p._save_state()
    rec = json.loads(p._state_path.read_text(encoding="utf-8"))
    assert rec["pilot"]["gates"][0]["decision"] == "ЖДАТЬ" and [x["why"] for x in rec["pilot"]["profits"]] == ["свежая"]
    assert rec["position"]["profit_lock"] is True and rec["position"]["profit_next"] > time.time()
    m2, p2 = make_pilot()
    p2._state_path = p._state_path
    p2._restore_state(8)
    assert p2.gates[-1]["why"] == "тест" and [x["why"] for x in p2.profits] == ["свежая"]
    assert p2.position and p2.position["profit_lock"] and p2.position["profit_next"] > time.time()
    assert p2.position["hard_stop"] == p.position["hard_stop"] and p2.position.get("restop"), "стопа нет — restop (ничего не ставит)"


def test_hard_stop_not_below_entry_when_profit_locked():
    m, p = make_pilot()
    pos = {"side": "long", "entry": 100.0, "lots": 8, "take": 110.0, "invalidation": 100.5, "inv0": 100.5}
    assert p._hard_of(pos) < 100.0
    pos["profit_lock"] = True
    assert p._hard_of(pos) == 100.0
    short = {"side": "short", "entry": 100.0, "lots": 8, "take": 90.0, "invalidation": 99.5, "inv0": 99.5, "profit_lock": True}
    assert p._hard_of(short) == 100.0


# ── стопы только в программе ─────────────────────────────────────────────────────────────────────────────────
def test_flag_off_places_no_stop_and_virtual_trail_closes(offline):
    fake = offline

    async def scenario():
        m, p = make_pilot()
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "ок"})
        pos = await open_position(p, inv=98.0, take=110.0)
        assert not p.broker.stops and pos["stop_id"] is None and not pos.get("stop_request") and pos["hard_stop"] < 98.0
        assert p.status()["exchange_stop"] is False and "стопов на бирже нет" in p.last_action
        await tick(p, 100.0)
        assert not p.broker.stops
        n_o = len(p.broker.placed)
        await tick(p, pos["hard_stop"] - 0.5)
        assert p.position is None and len(p.broker.placed) == n_o + 1 and p.broker.placed[-1]["price"] is None
        assert not p.broker.stop_cancels and fake.count("mission_guard") == 0 and "аварийный трос" in p.last_action

    asyncio.run(scenario())


@pytest.mark.parametrize("listing", ["gone", "alive", "unavailable"])
def test_old_exchange_stop_is_removed_once_when_flag_off(listing):
    async def scenario():
        b = Broker()
        m, p = make_pilot(b)
        p.position = {"side": "long", "entry": 100.0, "lots": 8, "take": 110.0, "invalidation": 98.0, "opened_ts": time.time(),
                      "stop_id": "S-old", "floating": 0.0, "restop": True}
        p._set_levels(p.position, 110.0, 98.0)
        pos = p.position
        b.cancel_stop_ok = listing == "ok"
        b.listed = ([] if listing == "gone" else
                    [{"stopOrderId": "S-old", "figi": "F", "status": "STOP_ORDER_STATUS_ACTIVE"}] if listing == "alive" else None)
        await tick(p, 100.0)
        if listing == "gone":
            assert pos["stop_id"] is None and b.stop_cancels == ["S-old"] and not pos.get("restop") and not b.stops
        else:
            assert pos["stop_id"] == "S-old" and pos.get("restop") and pos["restop_after"] > time.time(), pos
            b.cancel_stop_ok = True
            pos["restop_after"] = 0.0
            await tick(p, 100.0)
            assert pos["stop_id"] is None and b.stop_cancels == ["S-old", "S-old"] and not b.stops
        await tick(p, 100.0)
        assert len(b.stop_cancels) == (1 if listing == "gone" else 2), "снят один раз"

    asyncio.run(scenario())


def test_flag_toggle_in_flight_both_directions(monkeypatch):
    async def scenario():
        monkeypatch.setattr(config, "PYTHIA_ENTRY_CHECK", False)
        b = Broker()
        m, p = make_pilot(b)
        monkeypatch.setattr(config, "PYTHIA_EXCHANGE_STOP", True)
        assert p.adopt_forecast(ex("BUY"))
        await tick(p, 100.0)
        await tick(p, 100.0)
        pos = p.position
        assert pos["stop_id"] == "S-1" and len(b.stops) == 1 and p.status()["exchange_stop"] is True
        monkeypatch.setattr(config, "PYTHIA_EXCHANGE_STOP", False)       # выключили в панели → снять на следующем restop
        pos["restop"] = True
        await tick(p, 100.0)
        assert pos["stop_id"] is None and b.stop_cancels == ["S-1"] and not pos.get("restop")
        monkeypatch.setattr(config, "PYTHIA_EXCHANGE_STOP", True)        # включили → трос снова на бирже
        pos["restop"] = True
        await tick(p, 100.0)
        assert pos["stop_id"] == "S-2" and len(b.stops) == 2 and abs(b.stops[-1]["stop"] - pos["hard_stop"]) < 1e-9
        # закрытие при включённом флаге снимает стоп перед рыночным ордером
        await tick(p, 111.0)                                              # тейк → PRO у тейка ({} → ЗАФИКСИРОВАТЬ)
        assert p.position is None and "S-2" in b.stop_cancels

    asyncio.run(scenario())


def test_status_has_541_fields():
    m, p = make_pilot()
    st = p.status()
    for k in ("exchange_stop", "money_model", "entry_gate", "gates", "profit", "profits", "entry_check", "profit_think"):
        assert k in st, k
    assert st["exchange_stop"] is False and st["money_model"] == "pro" and st["entry_gate"] is None and st["profit"] is None
    assert st["gates"] == [] and st["profits"] == [] and st["entry_check"] is True and st["profit_think"] is True
    json.dumps(st)
