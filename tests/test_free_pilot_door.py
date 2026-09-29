# -*- coding: utf-8 -*-
"""ПИФИЯ v5.4.2 «СВОБОДНЫЙ ПИЛОТ»: дверь, перепроверка, мысль о прибыли и приказ совета на фейках — код не решает за ИИ.

Молчание / таймаут / непонятный ответ — не «ЖДАТЬ»/«ДЕРЖАТЬ»/«ЖДЁМ» от имени ИИ и не вход вслепую: запись кода
(НЕТ_ОТВЕТА / НЕ_РАЗОБРАН, silent, source «код») и скорый повтор (PYTHIA_SILENT_RETRY_SEC). Слово решения — по словарю
узла (ai_v5.decision_of): «BUY» у лонга — ВОЙТИ, «НЕ ВХОДИТЬ» — не разобрано. Свежее решение PRO по живому рынку
(перепроверка, уровень от двери, перезаход) исполняется без второго вопроса у двери; приказ совета и протухшее / уехавшее
решение — через дверь. Дрейф за время раздумий — новый вопрос, а не засада по старой цене. Ни сети, ни ключей, ни data/."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend import ai_pilot, config, mission, trader_risk


class Broker:
    """Мини-биржа: заявки исполняются сразу, портфеля нет (dry-снимок), GetMaxLots — по mx."""
    mode = "real"

    def __init__(self):
        self.placed, self.stops, self.cancelled = [], [], []
        self.mx = None
        self._n = 0

    async def place(self, figi, direction, lots, price=None, tag="", **kw):
        self._n += 1
        self.placed.append({"order_id": f"F-{self._n}", "direction": direction, "lots": int(lots), "price": price, "tag": tag})
        return {"ok": True, "order_id": f"F-{self._n}"}

    async def order_state(self, oid, **kw):
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


class FakeMoney:
    """ai_v5.money_json / pro_json: очереди ответов по маршрутам, «молчание» (TimeoutError), срок попытки записывается."""
    SILENT = "SILENT"

    def __init__(self):
        self.answers: dict = {}
        self.calls: list = []                 # (маршрут, user, attempt_timeout)
        self.errors: list = []
        self.during = None                    # колбэк «пока ИИ думает» (рынок живёт)

    def queue(self, route, *answers):
        self.answers.setdefault(route, []).extend(answers)

    def count(self, route):
        return sum(1 for r, _, _ in self.calls if r == route)

    def last_user(self, route):
        return [u for r, u, _ in self.calls if r == route][-1]

    async def _answer(self, route, user, attempt_timeout=None):
        self.calls.append((route, user, attempt_timeout))
        if self.during:
            await self.during(route)
        q = self.answers.get(route) or []
        a = q.pop(0) if q else {}
        if a == self.SILENT:
            raise asyncio.TimeoutError()
        return a

    async def money_json(self, system, user, *, route, max_tokens=None, attempt_timeout=None):
        return await self._answer(route, user, attempt_timeout)

    async def pro_json(self, system, user, *, route="pro", max_tokens=None, attempt_timeout=None):
        return await self._answer(route, user, attempt_timeout)

    def note_error(self, text, route=""):
        self.errors.append((route, text))


@pytest.fixture(autouse=True)
def free(monkeypatch, tmp_path):
    """Всё внешнее — фейки; умолчания 5.4.2 (дверь включена, мысль о прибыли, деньги на PRO, стопы в программе)."""
    monkeypatch.setattr(mission, "_M", {})
    monkeypatch.setattr(mission, "explain", None)
    monkeypatch.setattr(mission.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mission.store_v5, "mission_put", lambda ticker, data: None)
    monkeypatch.setattr(mission.store_v5, "trade_add", lambda rec: None)
    monkeypatch.setattr(mission.ledger, "schedule_after_close", Mock())
    monkeypatch.setattr(mission.ledger, "estimate", lambda rec: 0.0)
    monkeypatch.setattr(mission.bus, "stage", AsyncMock())
    monkeypatch.setattr(mission.market_ctx, "light", AsyncMock(return_value={"text": "живой рынок: тест"}))
    monkeypatch.setattr(mission, "_council_text", lambda: "итог совета: тест")
    monkeypatch.setattr(mission, "_scan_text", lambda m: "")
    monkeypatch.setattr(mission, "_mod", lambda name: None)
    try:
        from backend import astro
        monkeypatch.setattr(astro, "acontext", AsyncMock(return_value=None))
    except Exception:                                  # noqa: BLE001 — без неба перепроверка живёт
        pass
    for k, v in (("PYTHIA_PARTNERS", False), ("PYTHIA_EXCHANGE_STOP", False), ("PYTHIA_ENTRY_CHECK", True),
                 ("PYTHIA_PROFIT_THINK", True), ("PYTHIA_MONEY_MODEL", "pro"), ("PYTHIA_SOFT_STOP", True),
                 ("PYTHIA_SOFT_TAKE", True), ("PYTHIA_ENTRY_TIMEOUT_SEC", 1200), ("PYTHIA_PROFIT_TIMEOUT_SEC", 1200),
                 ("PYTHIA_SILENT_RETRY_SEC", 120), ("PYTHIA_ENTRY_SILENT_MAX", 2), ("PYTHIA_ENTRY_FRESH_SEC", 1200),
                 ("PYTHIA_ENTRY_DRIFT_PCT", 1.0), ("PYTHIA_ENTRY_CHECK_COOL_SEC", 300)):
        monkeypatch.setattr(config, k, v, raising=False)
    monkeypatch.setattr(ai_pilot, "market_clock", None)             # часы выключены: рынок открыт
    fake = FakeMoney()
    monkeypatch.setattr(mission.ai_v5, "money_json", fake.money_json)
    monkeypatch.setattr(mission.ai_v5, "pro_json", fake.pro_json)
    monkeypatch.setattr(mission.ai_v5, "note_error", fake.note_error)
    monkeypatch.setattr(mission.ai_v5, "flash_json", AsyncMock(side_effect=AssertionError("узлы у денег идут через money_json")))
    return fake


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


def ex(do="BUY", entry=None, take=110.0, inv=98.0, why="совет: тест"):
    return {"exec": {"do": do, "entry": entry, "take": take, "invalidation": inv, "why": why}}


def book(price):
    return {"best_bid": round(price - 0.1, 4), "best_ask": round(price + 0.1, 4)}


async def settle_bg(p, n=300):
    for _ in range(n):
        if not [t for t in p._background_tasks if not t.done()]:
            return
        await asyncio.sleep(0.005)


async def tick(p, price, n=1):
    for _ in range(n):
        await p.tick(price, book(price))
        await settle_bg(p)


async def open_long(p, fake, inv=98.0, take=110.0, price=100.0):
    assert p.adopt_forecast(ex("BUY", None, take, inv))
    fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "ок"})
    await tick(p, price)
    await tick(p, price)
    assert p.position, p.last_action
    return p.position


# ── приказ совета: словарь решения, нейтральные заглушки, правило пробоя ────────────────────────────────────────────
def test_validate_exec_synonyms_placeholders_and_breakout_rule():
    for w in ("КУПИТЬ", "лонг", "Покупка", "buy", "LONG"):
        out, err = mission._validate_exec({"do": w, "invalidation": 98}, "auto", 100)
        assert err is None and out["do"] == "BUY", (w, err)
    for w in ("ПРОДАТЬ", "шорт", "SELL"):
        out, err = mission._validate_exec({"do": w, "invalidation": 102}, "auto", 100)
        assert err is None and out["do"] == "SELL", (w, err)
    for w in ("ЖДЁМ", "ждем", "NO_TRADE", "FLAT", "wait"):
        out, err = mission._validate_exec({"do": w}, "auto", 100)
        assert err is None and out["do"] == "WAIT", (w, err)
    for w in ("FLAT", "EXIT", "ЗАКРЫТЬ"):
        out, err = mission._validate_exec({"do": w}, "auto", 100, in_pos=True)
        assert err is None and out["do"] == "CLOSE", (w, err)
    out, err = mission._validate_exec({"decision": "BUY", "invalidation": 98}, "auto", 100)
    assert err is None and out["do"] == "BUY", "ключ decision тоже читается"
    assert mission._validate_exec({"do": "НЕ ПОКУПАТЬ", "invalidation": 98}, "auto", 100)[1], "отрицание — не решение"
    # заглушки нейтральны: код не дописывает за ИИ «перевеса нет»
    w0, _ = mission._validate_exec({"do": "WAIT"}, "auto", 100)
    assert w0["wait_for"] == "условие входа не названо — реши по живой картине" and w0["why"] == "(причина не указана)"
    # ошибка уровней: сторона принята, WAIT как выход не подсказывается
    err = mission._validate_exec({"do": "BUY", "invalidation": 101}, "auto", 100)[1]
    assert err.startswith("сторона BUY принята — исправь уровни:") and "WAIT" not in err, err
    # пробой: стоп между ценой и уровнем законен; стоп выше уровня BUY — ошибка уровня
    out, err = mission._validate_exec({"do": "BUY", "entry": 101, "invalidation": 100.5, "take": 104}, "auto", 100)
    assert err is None and out["entry_kind"] == "прорыв" and out["invalidation"] == 100.5, err
    out, err = mission._validate_exec({"do": "SELL", "entry": 99, "invalidation": 99.5, "take": 96}, "auto", 100)
    assert err is None and out["entry_kind"] == "прорыв", err
    assert "не ниже entry 101" in mission._validate_exec({"do": "BUY", "entry": 101, "invalidation": 101.5}, "auto", 100)[1]
    # подсказка «сейчас»/«now» — вход сразу, геометрия её не переделывает в пробой/засаду
    for hint in ("сейчас", "now"):
        out, err = mission._validate_exec({"do": "BUY", "entry": 100.3, "entry_kind": hint, "invalidation": 98}, "auto", 100)
        assert err is None and out["entry"] is None and out["entry_kind"] == "сейчас", (hint, out)
    assert mission._validate_exec({"do": "BUY", "entry": 100.3, "invalidation": 98}, "auto", 100)[0]["entry_kind"] == "прорыв", \
        "без подсказки решает геометрия"


# ── дверь: молчание, слово решения, свежее решение, приказ совета, дрейф ───────────────────────────────────────────
def test_door_silence_is_no_decision_and_retries_soon(free):
    fake = free

    async def scenario():
        m, p = make_pilot()
        assert p.adopt_forecast(ex("BUY"))
        fake.queue("mission_entry", FakeMoney.SILENT)
        await tick(p, 100.0)
        assert p.pending is None and not p.broker.placed and p.plan, "молчание — не вход"
        g = p.gates[-1]
        assert g["decision"] == "НЕТ_ОТВЕТА" and g["silent"] and g["source"] == "код" and "решения не было" in g["why"], g
        assert g["entry"] is None and g["wait_minutes"] is None and not g["council"], "поля ответа не читаются"
        assert all(x["decision"] != "ЖДАТЬ" for x in p.gates), "ЖДАТЬ за ИИ не записан"
        retry = float(config.PYTHIA_SILENT_RETRY_SEC)
        assert 0 < p.plan["gate_after"] - time.time() <= retry and p.plan["gate_silent"] == 1
        assert fake.calls[-1][2] == float(config.PYTHIA_ENTRY_TIMEOUT_SEC), "срок попытки — PYTHIA_ENTRY_TIMEOUT_SEC"
        assert fake.errors and fake.errors[-1][0] == "mission_entry"
        await tick(p, 100.0)
        assert fake.count("mission_entry") == 1, "до срока повтора PRO не спрашиваем"
        assert "не ответил у двери" in p.last_action and "велел ждать" not in p.last_action, p.last_action
        assert "ЖДАТЬ" not in p._gates_text(p.plan) and "решения не было" in p._gate_line(p.plan)
        assert "велел ждать" not in p._gate_line(p.plan)
        # второе молчание подряд — счёт, исполнения по молчанию нет
        p.plan["gate_after"] = 0.0
        fake.queue("mission_entry", FakeMoney.SILENT)
        await tick(p, 100.0)
        assert p.pending is None and p.plan["gate_silent"] == 2 and "дверь молчит 2 раз подряд" in p.gates[-1]["applied"]
        # настоящий ответ сбрасывает счёт; повторный вопрос видит прошлое молчание как «ответа не было»
        p.plan["gate_after"] = 0.0
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "стакан ожил"})
        await tick(p, 100.0)
        assert p.pending and "gate_silent" not in p.plan and "не ответил у двери" in fake.last_user("mission_entry")

    asyncio.run(scenario())


@pytest.mark.parametrize("side,word,enter", [("long", "BUY", True), ("long", "ВОЙТИ", True), ("long", "НЕ ВХОДИТЬ", False),
                                             ("long", "SELL", False), ("short", "SHORT", True), ("long", "ВОЙТИ или ЖДАТЬ", False)])
def test_door_word_by_plan_side(free, side, word, enter):
    fake = free

    async def scenario():
        m, p = make_pilot()
        assert p.adopt_forecast(ex("BUY") if side == "long" else ex("SELL", None, 90.0, 102.0))
        fake.queue("mission_entry", {"decision": word, "why": "т"})
        await tick(p, 100.0)
        g = p.gates[-1]
        if enter:
            assert p.pending and g["decision"] == "ВОЙТИ", (word, g)
        else:
            assert p.pending is None and g["decision"] == "НЕ_РАЗОБРАН" and g["silent"] and p.plan, (word, g)
            assert 0 < p.plan["gate_after"] - time.time() <= float(config.PYTHIA_SILENT_RETRY_SEC)

    asyncio.run(scenario())


def test_fresh_review_plan_enters_without_door_stale_and_drifted_go_through_door(free, monkeypatch):
    fake = free

    async def scenario():
        m, p = make_pilot()
        p.prices.append(100.0)
        p._plan_from_review("КУПИТЬ_СЕЙЧАС", {"invalidation": 98.0, "take": 110.0}, "лента за нас", 100.0, 100.0,
                            snap_ts=time.time())
        assert p.plan["src"] == "review" and p.plan["snap_price"] == 100.0
        await tick(p, 100.4)                                        # цена в пределах PYTHIA_ENTRY_DRIFT_PCT
        assert p.pending and fake.count("mission_entry") == 0, p.last_action
        # решение протухло (старше PYTHIA_ENTRY_FRESH_SEC) → дверь
        m2, p2 = make_pilot()
        p2._plan_from_review("КУПИТЬ_СЕЙЧАС", {"invalidation": 98.0}, "т", 100.0, 100.0,
                             snap_ts=time.time() - float(config.PYTHIA_ENTRY_FRESH_SEC) - 5)
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "ок"})
        await tick(p2, 100.1)
        assert fake.count("mission_entry") == 1 and p2.pending
        # цена ушла хуже снимка больше PYTHIA_ENTRY_DRIFT_PCT → дверь
        m3, p3 = make_pilot()
        p3._plan_from_review("КУПИТЬ_СЕЙЧАС", {"invalidation": 98.0}, "т", 100.0, 100.0, snap_ts=time.time())
        fake.queue("mission_entry", {"decision": "ЖДАТЬ", "why": "уехала"})
        await tick(p3, 101.5)
        assert fake.count("mission_entry") == 2 and p3.pending is None
        # после ЖДАТЬ без уровня — снова дверь, а не «свежий вход» (ответ двери новее решения перепроверки)
        p3.plan["gate_after"] = 0.0
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "теперь да"})
        await tick(p3, 100.2)
        assert fake.count("mission_entry") == 3 and p3.pending
        # выключенная свежесть (0) → всегда дверь
        monkeypatch.setattr(config, "PYTHIA_ENTRY_FRESH_SEC", 0)
        m4, p4 = make_pilot()
        p4._plan_from_review("КУПИТЬ_СЕЙЧАС", {"invalidation": 98.0}, "т", 100.0, 100.0, snap_ts=time.time())
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "ок"})
        await tick(p4, 100.0)
        assert fake.count("mission_entry") == 4

    asyncio.run(scenario())


def test_council_plan_always_asks_the_door(free):
    fake = free

    async def scenario():
        m, p = make_pilot()
        assert p.adopt_forecast(ex("BUY"))
        assert p.plan["src"] == "council" and p.plan.get("snap_ts")
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "ок"})
        await tick(p, 100.0)
        assert fake.count("mission_entry") == 1 and p.pending

    asyncio.run(scenario())


def test_door_level_is_fresh_and_enter_at_level_without_second_question(free):
    fake = free

    async def scenario():
        m, p = make_pilot()
        assert p.adopt_forecast(ex("BUY"))
        fake.queue("mission_entry", {"decision": "ЖДАТЬ", "why": "на откате", "entry": 99.2, "entry_kind": "откат",
                                     "invalidation": 97.5})
        await tick(p, 100.0)
        assert p.plan["src"] == "gate_level" and p.plan["entry"] == 99.2 and p.pending is None
        await tick(p, 99.6)
        assert p.pending is None and fake.count("mission_entry") == 1
        await tick(p, 99.21)
        assert p.pending and fake.count("mission_entry") == 1, p.last_action

    asyncio.run(scenario())


def test_review_drift_keeps_now_with_note_and_door_enter_drift_reasks(free):
    fake = free

    async def scenario():
        m, p = make_pilot()
        p.prices.append(101.5)
        p._plan_from_review("КУПИТЬ_СЕЙЧАС", {"invalidation": 98.0, "take": 110.0}, "т", 100.0, 101.5, snap_ts=time.time())
        plan = p.plan
        assert plan["entry"] is None and plan["kind"] == "сейчас", "не засада по старой цене"
        assert plan["gate_note"] == "цена ушла на 1.50% за время раздумий (было 100, стало 101.5)", plan
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "импульс жив"})
        await tick(p, 101.5)
        assert fake.count("mission_entry") == 1 and p.pending, "дрейф — вопрос двери с пометкой, ВОЙТИ по живой цене"
        assert "ПОМЕТКА К ЭТОМУ ВОПРОСУ: цена ушла на 1.50%" in fake.last_user("mission_entry")

        # ВОЙТИ, а цена за время раздумий двери ушла хуже её снимка → сразу новый вопрос, не засада
        m2, p2 = make_pilot()
        assert p2.adopt_forecast(ex("BUY"))

        async def moves(route):
            if route == "mission_entry" and fake.count("mission_entry") == 2:
                p2.prices.append(101.3)
        fake.during = moves
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "ок"})
        await tick(p2, 100.0)
        fake.during = None
        assert p2.pending is None and p2.plan["entry"] is None and p2.plan["gate_after"] <= time.time()
        assert "цена ушла на 1.30% за время раздумий (было 100, стало 101.3)" == p2.plan["gate_note"], p2.plan

    asyncio.run(scenario())


def test_enter_blocked_by_transient_ban_is_remembered(free):
    fake = free

    async def scenario():
        m, p = make_pilot()
        assert p.adopt_forecast(ex("BUY"))

        async def backoff(route):                                   # пока PRO думал — биржа отбила, бэкофф
            p.no_entry_until = time.time() + 1000
        fake.during = backoff
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "ок"})
        await tick(p, 100.0)
        fake.during = None
        assert p.pending is None and p.plan["approved_until"] > time.time() and "бэкофф" in p.gates[-1]["applied"]
        assert "сказал ВОЙТИ" in p._gate_line(p.plan)
        p.no_entry_until = 0.0
        await tick(p, 100.1)
        assert p.pending and fake.count("mission_entry") == 1, "вход по одобрению, без нового вопроса"
        # одобрено, но пока ждали снятия запрета, цена ушла дальше PYTHIA_ENTRY_DRIFT_PCT → новый вопрос с пометкой
        m2, p2 = make_pilot()
        assert p2.adopt_forecast(ex("BUY"))

        async def backoff2(route):
            p2.no_entry_until = time.time() + 1000
        fake.during = backoff2
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "ок"})
        await tick(p2, 100.0)
        fake.during = None
        p2.no_entry_until = 0.0
        fake.queue("mission_entry", {"decision": "ЖДАТЬ", "why": "уехала"})
        await tick(p2, 101.5)
        assert p2.pending is None and fake.count("mission_entry") == 3 and "approved_until" not in p2.plan
        assert "от одобренной у двери 100 (стало 101.5)" in fake.last_user("mission_entry")

    asyncio.run(scenario())


def test_zero_lots_drops_plan_honestly_and_asks_review(free):
    fake = free

    async def scenario():
        b = Broker()
        b.mx = {"buy": 5, "sell": 0}
        m, p = make_pilot(b)
        assert p.adopt_forecast(ex("SELL", None, 90.0, 102.0))
        await tick(p, 100.0)
        assert p.plan is None and fake.count("mission_entry") == 0 and not b.placed
        assert "биржа не даёт short: лотов 0 — шорт недоступен или нет ГО" in (p._review_reason or ""), p._review_reason
        assert m.handoffs[-1]["kind"] == "pilot"

    asyncio.run(scenario())


def test_same_side_review_updates_plan_in_place_while_door_thinks(free):
    fake = free

    async def scenario():
        m, p = make_pilot()
        assert p.adopt_forecast(ex("BUY"))
        gate = asyncio.Event()

        async def hold(route):
            await gate.wait()
        fake.during = hold
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "ок"})
        await p.tick(100.0, book(100.0))
        plan = p.plan
        assert plan["gate_busy"]
        p._plan_from_review("КУПИТЬ_СЕЙЧАС", {"invalidation": 97.0, "take": 112.0}, "согласен", 100.0, 100.0,
                            snap_ts=time.time())
        assert p.plan is plan and plan["gate_busy"] and plan["invalidation"] == 97.0 and plan["src"] == "review"
        gate.set()
        await settle_bg(p)
        fake.during = None
        assert p.pending and p.pending["invalidation"] == 97.0 and fake.count("mission_entry") == 1, p.last_action

    asyncio.run(scenario())


def test_in_place_level_change_waits_for_new_level_then_enters_fresh(free):
    fake = free

    async def scenario():
        m, p = make_pilot()
        assert p.adopt_forecast(ex("BUY"))
        gate = asyncio.Event()

        async def hold(route):
            await gate.wait()
        fake.during = hold
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "ок"})
        await p.tick(100.0, book(100.0))
        plan = p.plan
        p._plan_from_review("КУПИТЬ_СЕЙЧАС", {"entry": 99.0, "entry_kind": "откат", "invalidation": 97.0}, "лучше на откате",
                            100.0, 100.0, snap_ts=time.time())
        assert p.plan is plan and plan["entry"] == 99.0 and plan["kind"] == "откат"
        gate.set()
        await settle_bg(p)
        fake.during = None
        assert p.pending is None and "войду у нового уровня" in p.gates[-1]["applied"], p.gates[-1]
        await tick(p, 99.02)                                        # уровень — свежее решение перепроверки, без двери
        assert p.pending and fake.count("mission_entry") == 1, p.last_action

    asyncio.run(scenario())


def test_money_prep_is_bounded_separately_from_ai_time(free, monkeypatch):
    fake = free
    monkeypatch.setattr(mission.MissionPilot, "MONEY_PREP_SEC", 0.05)

    async def slow_fit(blocks):
        await asyncio.sleep(5)
        return blocks, []
    monkeypatch.setattr(mission, "_fit_blocks", slow_fit)

    async def scenario():
        m, p = make_pilot()
        assert p.adopt_forecast(ex("BUY"))
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "ок"})
        await tick(p, 100.0)
        assert p.pending and fake.count("mission_entry") == 1, "сжатие не успело — PRO всё равно спрошен, с тем, что есть"
        assert "подготовка данных не уложилась" in fake.last_user("mission_entry")
        await tick(p, 100.0)
        pos = p.position
        fake.queue("mission_profit", {"decision": "ДЕРЖАТЬ", "why": "ход жив"})
        await tick(p, pos["entry"] + 7.0)
        assert p.profits[-1]["decision"] == "ДЕРЖАТЬ" and "подготовка данных не уложилась" in fake.last_user("mission_profit")

    asyncio.run(scenario())


# ── перепроверка: словарь, не разобрано, режим игры, решение во время совета ────────────────────────────────────────
def test_review_words_and_garbage(free):
    fake = free

    async def scenario():
        m, p = make_pilot()
        p.prices.append(100.0)
        for w in ("BUY", "LONG", "лонг"):
            p.plan = None
            fake.queue("mission_review", {"choice": w, "why": "т", "invalidation": 98.0})
            await p._review(100.0)
            assert m.reviews[-1]["choice"] == "КУПИТЬ_СЕЙЧАС" and p.plan and p.plan["side"] == "long", (w, m.reviews[-1])
        p.plan = None
        n = len(m.reviews)
        p._review_reason = "резкий ход: тест"
        p.review_ts = time.time() + 1800
        for junk in ("может быть", "НЕ ПОКУПАТЬ", ""):
            fake.queue("mission_review", {"choice": junk, "why": "?"})
            await p._review(100.0)
            assert len(m.reviews) == n and p.plan is None, (junk, m.reviews[-1:])
            assert p._review_reason == "резкий ход: тест" and p.review_ts <= time.time() + mission.REVIEW_RETRY_SEC + 1
            assert "ответ не разобран" in p.last_action, p.last_action
        assert all(r["choice"] != "ЖДЁМ" for r in m.reviews), "ЖДЁМ за ИИ не записан"
        # в позиции «CLOSE» — закрыть
        pos = await open_long(p, fake)
        fake.queue("mission_review", {"decision": "CLOSE", "why": "выдохлось"})
        await p._review(100.0)
        assert p.position is None and m.reviews[-1]["choice"] == "ЗАКРЫТЬ" and pos

    asyncio.run(scenario())


def test_review_play_mode_out_of_mode_and_flip_to_close(free):
    fake = free

    async def scenario():
        m, p = make_pilot(play="long")
        p.prices.append(100.0)
        p.review_ts = time.time() + 1800
        fake.queue("mission_review", {"choice": "ПРОДАТЬ_СЕЙЧАС", "why": "вниз", "invalidation": 102.0})
        await p._review(100.0)
        r = m.reviews[-1]
        assert r["choice"] == "ВНЕ_РЕЖИМА" and r["ai_choice"] == "ПРОДАТЬ_СЕЙЧАС" and p.plan is None, r
        assert p._review_reason == "прошлый ответ ПРОДАТЬ_СЕЙЧАС запрещён режимом long"
        assert p.review_ts <= time.time() + mission.REVIEW_RETRY_SEC + 1
        await open_long(p, fake)
        fake.queue("mission_review", {"choice": "ПЕРЕВЕРНУТЬ", "why": "разворот"})
        await p._review(100.0)
        r = m.reviews[-1]
        assert r["choice"] == "ЗАКРЫТЬ" and r["ai_choice"] == "ПЕРЕВЕРНУТЬ" and p.position is None, r
        assert r["why"].startswith("[ПЕРЕВЕРНУТЬ против режима long — только ЗАКРЫТЬ]")

    asyncio.run(scenario())


def test_review_buy_during_council_is_deferred_not_lost(free):
    fake = free

    async def scenario():
        m, p = make_pilot()
        p.prices.append(100.0)
        p._reanalyzing = True
        fake.queue("mission_review", {"choice": "КУПИТЬ_СЕЙЧАС", "why": "пробили", "invalidation": 98.0})
        await p._review(100.0)
        assert p.plan is None and "решение перепроверки КУПИТЬ_СЕЙЧАС отложено: идёт совет" in (p._review_reason or "")
        assert "отложено: идёт совет" in p.last_action
        # совет ответил WAIT — решение перепроверки не пропало: повод жив, дежурный PRO вернётся вскоре
        wait, _ = mission._validate_exec({"do": "WAIT", "wait_for": "т"}, "auto", 100)
        assert p.adopt_forecast({"exec": wait})
        assert "отложено: идёт совет" in (p._review_reason or "") and p.review_ts <= time.time() + mission.EVENT_MIN_GAP_SEC + 1

    asyncio.run(scenario())


# ── мысль о прибыли: словарь и молчание ────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("word", ["ЗАФИКСИРОВАТЬ", "CLOSE", "SELL", "забрать прибыль"])
def test_profit_exit_words(free, word):
    fake = free

    async def scenario():
        m, p = make_pilot()
        pos = await open_long(p, fake)
        fake.queue("mission_profit", {"decision": word, "why": "выдохся"})
        await tick(p, pos["entry"] + 7.0)
        assert p.position is None and p.profits[-1]["decision"] == "ВЫЙТИ", (word, p.profits[-1])

    asyncio.run(scenario())


def test_profit_silence_and_garbage_are_no_decision_with_quick_retry(free):
    fake = free

    async def scenario():
        m, p = make_pilot()
        pos = await open_long(p, fake)
        inv0, take0 = pos["invalidation"], pos["take"]
        retry = float(config.PYTHIA_SILENT_RETRY_SEC)
        fake.queue("mission_profit", FakeMoney.SILENT)
        await tick(p, pos["entry"] + 7.0)
        x = p.profits[-1]
        assert x["decision"] == "НЕТ_ОТВЕТА" and x["silent"] and x["source"] == "код" and p.position is pos, x
        assert pos["invalidation"] == inv0 and pos["take"] == take0 and 0 < pos["profit_next"] - time.time() <= retry
        assert [c[2] for c in fake.calls if c[0] == "mission_profit"][-1] == float(config.PYTHIA_PROFIT_TIMEOUT_SEC)
        assert "ДЕРЖАТЬ" not in p._profits_text(pos) and "решения не было" in p._profits_text(pos)
        pos["profit_next"] = 0.0
        fake.queue("mission_profit", {"decision": "НЕ ВЫХОДИТЬ?", "lock_price": 105.0})
        await tick(p, pos["entry"] + 7.2)
        x = p.profits[-1]
        assert x["decision"] == "НЕ_РАЗОБРАН" and x["lock_price"] is None and pos["invalidation"] == inv0, x
        assert 0 < pos["profit_next"] - time.time() <= retry

    asyncio.run(scenario())


# ── трос и тейк: только словарь; молчание и непонятный ответ — страховка кода, записанная как действие кода ──────────
def test_guard_and_take_vocabulary(free):
    fake = free

    async def scenario():
        m, p = make_pilot()
        pos = await open_long(p, fake)
        for ans, want in (({"decision": "HOLD", "why": "т"}, "ЖДАТЬ"), ({"choice": "EXIT"}, "СЛИТЬ"),
                          ({"decision": "НЕ СЛИВАТЬ — держать", "why": "вынос стопов"}, "ЖДАТЬ")):
            fake.queue("mission_guard", ans)
            r = await p._stop_guard(97.9, pos)
            assert r["decision"] == want and not r.get(ai_pilot.RULE_KEY), (ans, r)
        fake.queue("mission_guard", {"decision": "???", "why": "лента выкупает"})
        r = await p._stop_guard(97.9, pos)
        assert r["decision"] == "СЛИТЬ" and r[ai_pilot.RULE_KEY] == "НЕ_РАЗОБРАН" and r[ai_pilot.RULE_RAW] == "???", r
        # ревью 5.4.2 (финал): поля ИИ «rule»/«raw»/«detail» — его текст, не флаг кода; приватные ключи флага из ответа
        # ИИ вычищаются (флаг ставит только код)
        fake.queue("mission_guard", {"decision": "ЖДАТЬ", "why": "т", "rule": "держу, пока 97 не пробит", "raw": "x"},
                   {"decision": "ЖДАТЬ", "why": "т", "_rule": "НЕТ_ОТВЕТА", "_detail": "подлог", "_raw": "y"})
        for _ in range(2):
            r = await p._stop_guard(97.9, pos)
            assert r["decision"] == "ЖДАТЬ" and not any(k in r for k in ("_rule", "_detail", "_raw")), r
        for ans, want in (({"decision": "CLOSE"}, "ЗАФИКСИРОВАТЬ"), ({"decision": "HOLD"}, "ПОДЕРЖАТЬ"),
                          ({"decision": "ПОДЕРЖАТЬ", "rule": "трейлинг", "_rule": "НЕ_РАЗОБРАН"}, "ПОДЕРЖАТЬ")):
            fake.queue("mission_take", ans)
            r = await p._take_guard(110.2, pos)
            assert r["decision"] == want and not r.get(ai_pilot.RULE_KEY), (ans, r)
        fake.queue("mission_take", {})
        r = await p._take_guard(110.2, pos)
        assert r["decision"] == "ЗАФИКСИРОВАТЬ" and r[ai_pilot.RULE_KEY] == "НЕ_РАЗОБРАН", r

    asyncio.run(scenario())


def test_guard_and_take_fallback_is_code_rule_not_ai_decision(free, monkeypatch):
    """Ревью 5.4.2: у троса ответ не разобран / у тейка молчание — выход по правилу остаётся (страховка), но запись —
    действие кода (НЕ_РАЗОБРАН / НЕТ_ОТВЕТА, silent, source «код», сырой ответ), причина закрытия — «по правилу», не
    «PRO решил слить»; в промптах — «ответа не было — выход по правилу»."""
    fake = free

    async def scenario():
        m, p = make_pilot()
        pos = await open_long(p, fake)
        fake.queue("mission_guard", {"decision": "может быть", "why": "лента выкупает"})
        await tick(p, 97.9)
        assert p.position is None, p.last_action
        g = p.guards[-1]
        assert g["decision"] == "НЕ_РАЗОБРАН" and g["silent"] and g["source"] == "код" and g["raw"] == "может быть" \
            and g["applied"] == "по правилу: СЛИТЬ" and "ответа не было" in g["why"] and g["note"] == "лента выкупает", g
        assert "мягкий стоп по правилу: ответ не разобран (может быть)" in p.last_action and "решил слить" not in p.last_action
        gt = p._guards_text()
        assert "трос: ответа не было — выход по правилу" in gt and "трос: СЛИТЬ" not in gt and "НЕ_РАЗОБРАН" not in gt, gt
        # тейк: PRO молчит (таймаут) — фиксация по правилу, запись кода
        m2, p2 = make_pilot()
        pos2 = await open_long(p2, fake, take=101.0)
        fake.queue("mission_take", FakeMoney.SILENT)
        await tick(p2, 101.2)
        assert p2.position is None, p2.last_action
        g2 = p2.guards[-1]
        assert g2["side"] == "take" and g2["decision"] == "НЕТ_ОТВЕТА" and g2["silent"] and g2["source"] == "код" \
            and g2["applied"] == "по правилу: ЗАФИКСИРОВАТЬ" and "не ответил" in g2["why"], g2
        assert "фиксация по правилу: PRO не ответил" in p2.last_action, p2.last_action
        assert "тейк: ответа не было — выход по правилу" in p2._guards_text()

    asyncio.run(scenario())


# ── тексты для ИИ: WAIT совета — прошлое мнение, а не запрет ───────────────────────────────────────────────────────
def test_wait_texts_are_neutral(free):
    m, p = make_pilot()
    wait, _ = mission._validate_exec({"do": "WAIT", "wait_for": "закрепление выше 101", "why": "мутно"}, "auto", 100)
    assert p.adopt_forecast({"exec": wait})
    m.exec, m.exec_ts = wait, time.time()
    assert "WAIT — совет ждал: закрепление выше 101" in mission._exec_text(m) and "перевеса нет" not in mission._exec_text(m)
    sit = p._situation_text(100.0)
    # ревью 5.4.2: выбор — из списка перепроверки (режим игры, НОВЫЙ_АНАЛИЗ), а не зашитые три слова
    assert ("ПРИКАЗ СОВЕТА (0 мин назад): вне рынка; совет ждал: закрепление выше 101. Это прошлое мнение, а не запрет: "
            "реши заново — КУПИТЬ_СЕЙЧАС | ЖДЁМ | ПРОДАТЬ_СЕЙЧАС | НОВЫЙ_АНАЛИЗ") in sit, sit
    m2, p2 = make_pilot(play="long")
    assert p2.adopt_forecast({"exec": wait})
    m2.exec, m2.exec_ts = wait, time.time()
    sit2 = p2._situation_text(100.0)
    assert "реши заново — КУПИТЬ_СЕЙЧАС | ЖДЁМ | НОВЫЙ_АНАЛИЗ" in sit2 and "ПРОДАТЬ" not in sit2, sit2


# ══ ревью 5.4.2 (часть 2) ══════════════════════════════════════════════════════════════════════════════════════════
# ── приказ HOLD: держать как есть без добора ───────────────────────────────────────────────────────────────────────
def test_council_hold_updates_levels_without_topup(free):
    """Позиция по размеру биржи; вердикт совета «держать» → HOLD: стоп и тейк обновлены, добора нет (биржа даёт ещё —
    aip-topup не шлётся), дверь не спрашивается; явный BUY той же стороны — добор до максимума, как раньше."""
    fake = free

    async def scenario():
        b = Broker()
        b.mx = {"buy": 3, "sell": 3}
        m, p = make_pilot(b)
        p.deposit_override = None
        pos = await open_long(p, fake, inv=98.0, take=104.0)
        assert p._sized_by_broker and pos["lots"] == 3, pos
        assert pos["topup_left"] > 0, "остаток авто-добора входа — его снимет HOLD (ревью 5.4.2, финал)"
        b.mx = {"buy": 4, "sell": 4}
        p._mx = None
        p.prices.append(103.0)
        hold, err = mission._validate_exec({"do": "ДЕРЖАТЬ", "invalidation": 101.0, "take": 107.0,
                                            "why": "держать, стоп подтянуть к 101"}, "auto", 103.0, in_pos=True,
                                           pos_side="long")
        assert err is None and hold["do"] == "HOLD", err
        m.exec, m.exec_ts = hold, time.time()
        assert p.adopt_forecast({"exec": hold})
        assert pos["invalidation"] == 101.0 and pos["take"] == 107.0 and p.plan is None and not pos.get("topup_left")
        assert "HOLD — держать позицию как есть, без добора" in mission._exec_text(m)
        n_o, n_entry = len(b.placed), fake.count("mission_entry")
        pos["last_fill_ts"] = 0.0
        await tick(p, 103.0, n=13)
        assert len(b.placed) == n_o and pos["lots"] == 3 and fake.count("mission_entry") == n_entry, b.placed
        # null — прежний уровень; стоп и тейк несогласованы (стоп выше тейка у лонга) — приказ отклонён, позиция не тронута
        assert p.adopt_forecast({"exec": {"do": "HOLD", "invalidation": None, "take": None}})
        assert pos["invalidation"] == 101.0 and pos["take"] == 107.0
        assert not p.adopt_forecast({"exec": {"do": "HOLD", "invalidation": 108.0}}) and pos["invalidation"] == 101.0
        assert "HOLD отклонён" in p.last_action and "тейк 107.0 не выше стопа 108.0" in p.last_action, p.last_action
        # явный «добрать» (BUY той же стороны «сейчас») — добор до максимума, как было
        assert p.adopt_forecast(ex("BUY", None, 107.0, 101.0))
        assert pos["topup_left"] > 0
        pos["last_fill_ts"] = 0.0
        await tick(p, 103.0, n=7)
        assert [o for o in b.placed if o["tag"] == "aip-topup"], b.placed

    asyncio.run(scenario())


# ── ответ двери про прежний план не отменяет свежее решение, обновившее план на месте ─────────────────────────────
@pytest.mark.parametrize("answer", ["ОТМЕНИТЬ", "ЖДАТЬ", "SILENT"])
def test_stale_door_answer_does_not_veto_fresh_review(free, answer):
    fake = free

    async def scenario():
        m, p = make_pilot()
        assert p.adopt_forecast(ex("BUY"))
        gate = asyncio.Event()

        async def hold(route):
            await gate.wait()
        fake.during = hold
        fake.queue("mission_entry", FakeMoney.SILENT if answer == "SILENT" else
                   {"decision": answer, "why": "прежний план слабый", "council": True, "wait_minutes": 30})
        await p.tick(100.0, book(100.0))
        plan = p.plan
        assert plan["gate_busy"] and plan["src"] == "council"
        await asyncio.sleep(0.01)
        # пока дверь думает, дежурный PRO по живым данным решил купить — план обновлён на месте
        p._plan_from_review("КУПИТЬ_СЕЙЧАС", {"invalidation": 97.0, "take": 112.0}, "лента за нас", 100.0, 100.0,
                            snap_ts=time.time())
        assert p.plan is plan and plan["src"] == "review" and plan["snap_ts"] > plan["gate_ts"]
        gate.set()
        await settle_bg(p)
        fake.during = None
        g = p.gates[-1]
        assert p.plan is plan and g["applied"] == "план обновлён свежим решением за время ответа — не применено", g
        assert not plan.get("gate_busy") and "gate_wait_ts" not in plan and "gate_after" not in plan, plan
        assert not m.handoffs and not p._reanalyzing and p.reanalyze_cb.await_count == 0, "ни совета, ни отмены"
        assert "gate_silent" not in plan
        n = fake.count("mission_entry")
        await tick(p, 100.1)                                         # свежее решение — без второго вопроса у двери
        assert p.pending and p.pending["invalidation"] == 97.0 and fake.count("mission_entry") == n, p.last_action

    asyncio.run(scenario())


def test_in_place_update_restarts_plan_ttl_and_door_enter_applies(free):
    """Срок плана (PLAN_TTL_SEC) — от свежего решения: приказ совета состарился, пока дверь думала, перепроверка обновила
    план на месте — ВОЙТИ двери исполняется, а не «приказ протух»."""
    fake = free

    async def scenario():
        m, p = make_pilot()
        assert p.adopt_forecast(ex("BUY"))
        gate = asyncio.Event()

        async def hold(route):
            await gate.wait()
        fake.during = hold
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "ок"})
        await p.tick(100.0, book(100.0))
        plan = p.plan
        plan["ts"] = time.time() - ai_pilot.PLAN_TTL_SEC - 10           # приказ совета протух за время раздумий
        await asyncio.sleep(0.01)
        p._plan_from_review("КУПИТЬ_СЕЙЧАС", {"invalidation": 97.0}, "всё ещё да", 100.0, 100.0, snap_ts=time.time())
        assert p.plan is plan and time.time() - plan["ts"] < 5, "срок — от нового решения"
        gate.set()
        await settle_bg(p)
        fake.during = None
        assert p.pending and "протух" not in p.gates[-1]["applied"], (p.gates[-1], p.last_action)

    asyncio.run(scenario())


def test_in_place_update_drops_crossed_mark(free):
    fake = free

    async def scenario():
        m, p = make_pilot()
        assert p.adopt_forecast({"exec": {"do": "BUY", "entry": 101.0, "entry_kind": "прорыв", "take": 106.0,
                                          "invalidation": 100.5, "why": "совет: пробой"}})
        gate = asyncio.Event()

        async def hold(route):
            await gate.wait()
        fake.during = hold
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "ок"})
        await p.tick(101.1, book(101.1))
        await p.tick(101.1, book(101.1))
        plan = p.plan
        assert plan["gate_busy"] and plan.get("crossed") == 101.0, plan
        p._plan_from_review("КУПИТЬ_СЕЙЧАС", {"entry": 102.0, "entry_kind": "прорыв", "invalidation": 101.3}, "выше",
                            101.1, 101.1, snap_ts=time.time())
        assert p.plan is plan and "crossed" not in plan and plan["entry"] == 102.0
        gate.set()
        await settle_bg(p)
        fake.during = None

    asyncio.run(scenario())


# ── пробой: новый уровень от двери после пробития старого — идея жива до пробития нового ──────────────────────────
def test_door_new_breakout_level_after_crossing_survives_dip(free):
    fake = free

    async def scenario():
        m, p = make_pilot()
        assert p.adopt_forecast({"exec": {"do": "BUY", "entry": 101.0, "entry_kind": "прорыв", "take": 106.0,
                                          "invalidation": 100.5, "why": "совет: пробой 101"}})
        assert p.plan["kind"] == "прорыв"
        await tick(p, 100.0)
        fake.queue("mission_entry", {"decision": "ЖДАТЬ", "why": "ложный пробой — жду выше", "entry": 102.0,
                                     "entry_kind": "прорыв", "invalidation": 101.3})
        await tick(p, 101.1, n=mission.BREAK_CONFIRM_TICKS)          # 101 пробит → дверь: ЖДАТЬ пробоя 102, стоп 101.3
        assert fake.count("mission_entry") == 1 and p.plan and p.plan["entry"] == 102.0 and p.plan["kind"] == "прорыв"
        assert p.plan["invalidation"] == 101.3 and p.plan["src"] == "gate_level", p.plan
        await tick(p, 101.1)                                         # ниже нового стопа, но 102 не пробит — жив
        assert p.plan and p.pending is None and "мертва" not in p.last_action, p.last_action
        await tick(p, 102.1, n=mission.BREAK_CONFIRM_TICKS)          # пробит новый уровень — вход (решение свежее)
        assert p.pending and fake.count("mission_entry") == 1, p.last_action

    asyncio.run(scenario())


# ── план добора без позиции — не новый вход ────────────────────────────────────────────────────────────────────────
def test_leftover_topup_plan_never_opens_new_position(free, monkeypatch):
    fake = free
    monkeypatch.setattr(config, "PYTHIA_SOFT_TAKE", False)             # тейк — сразу в кассу (как в сценарии ревью)

    async def scenario():
        m, p = make_pilot()
        pos = await open_long(p, fake, inv=98.0, take=100.8)
        p._plan_from_review("КУПИТЬ_СЕЙЧАС", {"invalidation": 98.0, "take": 100.8}, "добрать", 100.5, 100.5,
                            topup=True, snap_ts=time.time())
        assert p.plan and p.plan["src"] == "topup"
        await tick(p, 100.9)                                         # тейк взят — позиция закрыта
        assert p.position is None and p.plan is None, (p.plan, p.last_action)
        n = len(p.broker.placed)
        await tick(p, 100.6, n=3)
        assert len(p.broker.placed) == n and p.pending is None and fake.count("mission_entry") == 1, p.broker.placed
        # план добора пережил позицию другим путём — дверь снимает его, а не бьёт свежий вход
        p.plan = {"side": "long", "entry": None, "kind": "сейчас", "take": 104.0, "invalidation": 98.0, "why": "добор",
                  "src": "topup", "snap_price": 100.6, "snap_ts": time.time(), "ts": time.time()}
        await tick(p, 100.6)
        assert p.plan is None and p.last_action == "добор снят: позиции уже нет" and p.state == "ЖДУ_ПЛАН"
        assert len(p.broker.placed) == n and fake.count("mission_entry") == 1

    asyncio.run(scenario())


# ── дверь выключена: решение, от которого цена уехала, не догоняем ──────────────────────────────────────────────────
def test_door_off_drift_is_not_chased(free, monkeypatch):
    fake = free
    monkeypatch.setattr(config, "PYTHIA_ENTRY_CHECK", False)

    async def scenario():
        m, p = make_pilot()
        p.prices.append(103.5)
        p._plan_from_review("КУПИТЬ_СЕЙЧАС", {"invalidation": 99.0, "take": 104.0}, "импульс", 100.0, 103.5,
                            snap_ts=time.time())
        assert p.plan and p.plan["gate_note"], p.plan
        p.review_ts = time.time() + 1800
        await tick(p, 103.5)
        assert p.plan is None and p.pending is None and not p.broker.placed and fake.count("mission_entry") == 0
        assert "цена ушла на 3.50% от снимка решения 100 (стало 103.5) — реши заново по живой цене" in p._review_reason
        assert p.review_ts <= max(time.time(), p._last_review_ts) + mission.EVENT_MIN_GAP_SEC + 1
        assert m.handoffs[-1]["kind"] == "pilot"
        # в пределах PYTHIA_ENTRY_DRIFT_PCT — вход сразу, как было
        m2, p2 = make_pilot()
        p2._plan_from_review("КУПИТЬ_СЕЙЧАС", {"invalidation": 99.0, "take": 104.0}, "т", 100.0, 100.3, snap_ts=time.time())
        await tick(p2, 100.3)
        assert p2.pending and fake.count("mission_entry") == 0
        # приказ совета при выключенной двери — вход сразу (как в 5.4.1)
        m3, p3 = make_pilot()
        assert p3.adopt_forecast(ex("BUY"))
        await tick(p3, 100.0)
        assert p3.pending

    asyncio.run(scenario())


# ── «после закрытия» — по строке кода, а не по слову «закрытие» в тексте ИИ ────────────────────────────────────────
def test_door_cancel_with_closing_word_in_why_pulls_review_soon(free, monkeypatch):
    fake = free
    monkeypatch.setattr(config, "PYTHIA_AFTER_CLOSE_SEC", 900)

    async def scenario():
        m, p = make_pilot()
        assert p.adopt_forecast(ex("BUY"))
        p.review_ts = time.time() + 1800
        fake.queue("mission_entry", {"decision": "ОТМЕНИТЬ", "why": "ход отыгран, закрытие часа ниже 100"})
        await tick(p, 100.0)
        assert p.plan is None and "закрытие часа" in (p._review_reason or "")
        assert p.review_ts - time.time() <= mission.EVENT_MIN_GAP_SEC + 1, p.review_ts - time.time()
        # настоящий повод «после закрытия» — пауза PYTHIA_AFTER_CLOSE_SEC, как было
        m2, p2 = make_pilot()
        p2.review_ts = time.time() + 1800
        p2._fire_reanalyze("после закрытия — огромный анализ с нуля")
        assert p2.review_ts - time.time() >= 890

    asyncio.run(scenario())


# ══ ревью 5.4.2 (финал) ════════════════════════════════════════════════════════════════════════════════════════════
# ── HOLD: живая цена приказ не отклоняет — пройденный стоп становится триггером, пройденный тейк — вопросом тейка ─────
def test_hold_stop_passed_by_live_price_becomes_trigger(free):
    """Совет проверил HOLD по цене 103 (стоп 102 ниже цены), пока приказ шёл к пилоту, цена ушла на 101.8 — за новый
    стоп. Раньше HOLD отклонялся целиком и оставался прежний, более свободный стоп 98; теперь стоп 102 принят и ближайший
    тик спрашивает у троса (решает ИИ, а не код)."""
    fake = free

    async def scenario():
        m, p = make_pilot()
        pos = await open_long(p, fake, inv=98.0, take=110.0)
        await tick(p, 103.0)
        hold, err = mission._validate_exec({"do": "HOLD", "invalidation": 102.0, "take": 108.0, "why": "стоп к 102"},
                                           "auto", 103.0, in_pos=True, pos_side="long")
        assert err is None and hold["do"] == "HOLD", err
        p.prices.append(101.8)                                        # цена прошла стоп, пока приказ шёл
        assert p.adopt_forecast({"exec": hold}), p.last_action
        assert pos["invalidation"] == 102.0 and pos["take"] == 108.0 and "уже за стопом" in p.last_action, p.last_action
        n = fake.count("mission_guard")
        fake.queue("mission_guard", {"decision": "ЖДАТЬ", "why": "вынос стопов, лента выкупает"})
        await tick(p, 101.8)
        assert fake.count("mission_guard") == n + 1, "стоп HOLD — триггер: вопрос у троса"
        assert p.position is pos and pos["holds"] == 1 and p.guards[-1]["decision"] == "ЖДАТЬ", p.last_action
        # тейк, который цена уже прошла, — вопрос мягкого тейка
        m2, p2 = make_pilot()
        pos2 = await open_long(p2, fake, inv=98.0, take=110.0)
        p2.prices.append(104.5)
        assert p2.adopt_forecast({"exec": {"do": "HOLD", "invalidation": None, "take": 104.0}}), p2.last_action
        assert pos2["take"] == 104.0 and "уже у тейка" in p2.last_action
        n_t = fake.count("mission_take")
        fake.queue("mission_take", {"decision": "ПОДЕРЖАТЬ", "why": "импульс жив", "lock_price": 102.0})
        await tick(p2, 104.5)
        assert fake.count("mission_take") == n_t + 1 and p2.position is pos2 and pos2["take_holds"] == 1, p2.last_action

    asyncio.run(scenario())


# ── HOLD со стопом прежним, цена уже за ним: счётчик «ждать» у троса не сбрасывается ──────────────────────────────────
def test_hold_keeps_holds_when_price_past_old_stop(free, monkeypatch):
    fake = free
    monkeypatch.setattr(config, "PYTHIA_SOFT_STOP_MAX_HOLDS", 2, raising=False)

    async def scenario():
        m, p = make_pilot()
        pos = await open_long(p, fake, inv=98.0, take=110.0)
        fake.queue("mission_guard", {"decision": "ЖДАТЬ", "why": "терпим"})
        await tick(p, 97.9)
        assert p.position is pos and pos["holds"] == 1, p.last_action
        # совет по поводу троса: HOLD, стоп прежний — круг «трос ждёт → совет держит» не обнуляет предел
        assert p.adopt_forecast({"exec": {"do": "HOLD", "invalidation": None, "take": None}}) and pos["holds"] == 1
        assert p.adopt_forecast({"exec": {"do": "HOLD", "invalidation": 98.0, "take": None}}) and pos["holds"] == 1, \
            "тот же стоп числом — тоже прежний"
        pos["guard_next"] = 0.0
        fake.queue("mission_guard", {"decision": "ЖДАТЬ", "why": "ещё терпим"})
        await tick(p, 97.9)
        assert pos["holds"] == 2 and p.position is pos
        assert p.adopt_forecast({"exec": {"do": "HOLD", "invalidation": None, "take": None}}) and pos["holds"] == 2
        n = fake.count("mission_guard")
        await tick(p, 97.9)
        assert p.position is None and "предел" in p.last_action and fake.count("mission_guard") == n, p.last_action
        # цена не за стопом — HOLD без нового стопа сбрасывает счётчик, как раньше
        m2, p2 = make_pilot()
        pos2 = await open_long(p2, fake, inv=98.0, take=110.0)
        pos2["holds"] = 1
        assert p2.adopt_forecast({"exec": {"do": "HOLD", "invalidation": None, "take": None}}) and pos2["holds"] == 0

    asyncio.run(scenario())


# ── HOLD снимает остаток авто-добора и заявку добора в полёте ────────────────────────────────────────────────────────
def test_hold_cancels_residual_topup_and_topup_order(free):
    fake = free

    async def scenario():
        b = Broker()
        b.mx = {"buy": 3, "sell": 3}
        m, p = make_pilot(b)
        p.deposit_override = None
        pos = await open_long(p, fake, inv=98.0, take=110.0)
        assert p._sized_by_broker and pos["topup_left"] > 0, pos
        p.pending = {"side": "long", "lots": 2, "price": 100.1, "ts": time.time(), "attempts": 0, "take": 110.0,
                     "invalidation": 98.0, "why": "добор до максимума", "topup": True}
        p._cancel_entry = False
        assert p.adopt_forecast({"exec": {"do": "HOLD", "invalidation": 99.0, "take": None}})
        assert pos["topup_left"] == 0 and p._cancel_entry, "HOLD — не добор: остаток снят, заявка добора — снять тиком"

    asyncio.run(scenario())


# ── HOLD до prepare(): уровни совета — к позиции со счёта (после проверки сторон), а не временные 2 % ────────────────
def test_hold_before_prepare_applies_council_levels_to_account_position(free):
    async def scenario():
        m, p = make_pilot()
        p._prepared = False
        assert p.adopt_forecast({"exec": {"do": "HOLD", "invalidation": 101.0, "take": 108.0, "why": "держать"}})
        assert p._pending_hold and p._pending_hold["inv"] == 101.0 and "уровни совета" in p.last_action
        assert p._absorb_account(3, 100.0, None, "на счёте при старте") and p._pending_hold is None
        pos = p.position
        assert pos["invalidation"] == 101.0 and pos["take"] == 108.0 and not pos.get("levels_placeholder"), pos
        assert "уровни приказа HOLD: стоп 101, тейк 108" in p.last_action, p.last_action
        # на счёте шорт, а уровни — лонга: несогласованы для шорта → временные, причина — в повод дежурному PRO
        m2, p2 = make_pilot()
        p2._prepared = False
        p2.review_ts = time.time() + 1800
        assert p2.adopt_forecast({"exec": {"do": "HOLD", "invalidation": 97.0, "take": 106.0}})
        assert p2._absorb_account(-3, 100.0, None, "на счёте при старте")
        pos2 = p2.position
        assert pos2["side"] == "short" and pos2["levels_placeholder"] and pos2["take"] is None, pos2
        assert abs(pos2["invalidation"] - 102.0) < 1e-9 and "HOLD не принят" in p2.last_action, p2.last_action
        assert "уровни приказа HOLD к позиции со счёта short не приняты" in (p2._review_reason or ""), p2._review_reason
        assert p2.review_ts <= time.time() + 91
        # новый приказ до подготовки снимает HOLD
        m3, p3 = make_pilot()
        p3._prepared = False
        assert p3.adopt_forecast({"exec": {"do": "HOLD", "invalidation": 97.0, "take": 106.0}})
        assert p3.adopt_forecast(ex("BUY", None, 107.0, 98.5)) and p3._pending_hold is None
        assert p3._absorb_account(3, 100.0, None, "на счёте при старте")
        assert p3.position["invalidation"] == 98.5 and p3.position["take"] == 107.0

    asyncio.run(scenario())


# ── план ДОБРАТЬ ждёт, а владелец перевернул позицию руками: не закрываем его новую позицию ─────────────────────────
def test_owner_flip_drops_topup_plan_no_close(free):
    fake = free

    async def scenario():
        m, p = make_pilot()
        pos = await open_long(p, fake, inv=98.0, take=110.0)
        p._plan_from_review("КУПИТЬ_СЕЙЧАС", {"invalidation": 98.0, "take": 110.0}, "добрать", 100.0, 100.0,
                            topup=True, snap_ts=time.time())
        assert p.plan and p.plan["src"] == "topup"
        p.no_entry_until = time.time() + 600                         # добор ждёт (бэкофф)
        await tick(p, 100.2)
        assert p.plan and p.plan["src"] == "topup" and p.position is pos
        n = len(p.broker.placed)
        # владелец перевернул руками: на счёте шорт 2
        assert p._absorb_account(-2, 100.3, 100.3, "сверка с биржей")
        assert p.position["side"] == "short" and p.plan is None, (p.plan, p.position)
        await tick(p, 100.3, n=3)
        assert p.position and p.position["side"] == "short" and len(p.broker.placed) == n, p.broker.placed[n:]
        # и тик сам: план добора к позиции другой стороны — не вердикт переворота (снят, закрытия нет)
        p.plan = {"side": "long", "entry": None, "kind": "сейчас", "take": 110.0, "invalidation": 98.0, "why": "добор",
                  "src": "topup", "snap_price": 100.3, "snap_ts": time.time(), "ts": time.time()}
        await tick(p, 100.3)
        assert p.plan is None and p.position and p.position["side"] == "short" and len(p.broker.placed) == n
        # настоящий вердикт переворота (не добор) — закрытие, как было
        p.plan = {"side": "long", "entry": None, "kind": "сейчас", "take": 110.0, "invalidation": 98.0, "why": "флип",
                  "src": "review", "snap_price": 100.3, "snap_ts": time.time(), "ts": time.time()}
        await tick(p, 100.3)
        assert [o for o in p.broker.placed[n:] if o["tag"] == "aip-close"], p.broker.placed[n:]

    asyncio.run(scenario())


# ── устаревший ответ двери: по пометке, а не по времени (настоящая перепроверка: снимок до раздумий PRO) ────────────
async def _wait_for(cond, n=400):
    for _ in range(n):
        if cond():
            return True
        await asyncio.sleep(0.005)
    return False


@pytest.mark.parametrize("answer", ["ОТМЕНИТЬ", "ЖДАТЬ", "SILENT"])
@pytest.mark.parametrize("council_level", [True, False])
def test_stale_door_answer_marker_with_real_review(free, answer, council_level):
    """Перепроверка берёт снимок (snap_ts) ДО раздумий PRO; дверь спрашивают, пока PRO думает (gate_ts позже снимка);
    PRO решает КУПИТЬ — план обновлён на месте. Ответ двери ЖДАТЬ/ОТМЕНИТЬ/молчание — про прежний план: не применяется
    (пометка gate_stale), запись ссылается на спрошенный план (gate_plan_ts) и в истории нового плана не считается."""
    fake = free

    async def scenario():
        m, p = make_pilot()
        review_gate, door_gate = asyncio.Event(), asyncio.Event()

        async def hold(route):
            if route == "mission_review":
                await review_gate.wait()
            elif route == "mission_entry":
                await door_gate.wait()
        fake.during = hold
        fake.queue("mission_review", {"choice": "КУПИТЬ_СЕЙЧАС", "why": "лента за нас", "invalidation": 97.0, "take": 112.0})
        fake.queue("mission_entry", FakeMoney.SILENT if answer == "SILENT" else
                   {"decision": answer, "why": "стоп совета 98 тесный", "council": False, "wait_minutes": 30})
        if council_level:
            assert p.adopt_forecast(ex("BUY", entry=99.0))                # совет: откат @99
            await p.tick(100.0, book(100.0))
            assert p.plan and not p.plan.get("gate_busy"), p.last_action
        p.review_ts = 0
        await p.tick(100.0, book(100.0))                                   # плановая перепроверка ушла к PRO
        assert await _wait_for(lambda: fake.count("mission_review") == 1)
        await asyncio.sleep(0.02)
        if not council_level:
            assert p.adopt_forecast(ex("BUY"))                             # совет закончился, пока PRO думает
            await p.tick(100.0, book(100.0))
        else:
            await p.tick(99.0, book(99.0))                                 # цена у уровня совета — дверь
        plan = p.plan
        assert plan and plan.get("gate_busy") and plan["src"] == "council", (plan, p.last_action)
        old_ts, old_how = plan["ts"], plan["gate_how"]
        assert plan["gate_plan_ts"] == old_ts
        assert await _wait_for(lambda: fake.count("mission_entry") == 1)
        await asyncio.sleep(0.01)
        review_gate.set()                                                  # PRO: КУПИТЬ_СЕЙЧАС по живому рынку
        assert await _wait_for(lambda: not p._review_busy)
        assert p.plan is plan and plan["src"] == "review" and plan["invalidation"] == 97.0 and plan.get("gate_stale")
        assert plan["snap_ts"] < plan["gate_ts"], "снимок перепроверки старше вопроса двери — времена не помогли бы"
        door_gate.set()
        assert await _wait_for(lambda: not plan.get("gate_busy"))
        await asyncio.sleep(0.02)
        fake.during = None
        g = p.gates[-1]
        assert g["applied"] == "план обновлён свежим решением за время ответа — не применено", g
        assert "gate_stale" not in plan and not m.handoffs and "gate_after" not in plan, plan
        # запись — о спрошенном плане; в истории обновлённого плана её нет
        assert g["plan_ts"] == old_ts != plan["ts"] and g["how"] == old_how, (g, plan["ts"])
        assert p._gates_text(plan) == "" and "проверок входа" not in p._gate_line(plan)
        assert p._entry_gate_status()["checks"] == 0 and p._entry_gate_status()["decision"] is None
        n = fake.count("mission_entry")
        px = 99.05 if council_level else 100.05
        await tick(p, px)                                                  # свежее решение — без второго вопроса
        assert p.pending and fake.count("mission_entry") == n, p.last_action

    asyncio.run(scenario())
