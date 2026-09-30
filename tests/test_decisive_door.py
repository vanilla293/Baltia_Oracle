# -*- coding: utf-8 -*-
"""ПИФИЯ 5.4.3 «РЕШИТЕЛЬНЫЙ ПИЛОТ» (C2 — дверь, трос/тейк, память): код только показывает числа и будит, решает ИИ.

Воля владельца 30.09.2026: «всё ещё играет ответами ждать — "прорыва нет, сидим ждём"». Здесь закреплено:
- причина ответа ЖДАТЬ у двери — слова модели (plan["gate_wait_why"]), а не пометка кода: на следующем вопросе нет
  «ПОМЕТКА К ЭТОМУ ВОПРОСУ» с этой причиной, она одна — в блоке прошлых ответов, с исходом «→ сейчас … в сторону
  плана» и строкой-фактом о серии ЖДАТЬ; у двери нет дубля строки «ПРОВЕРКА ВХОДА»; last_action — «ответ ЖДАТЬ,
  повтор в …» без «велел»;
- дрейф от снимка решения — нейтрально и с направлением («в сторону сделки» / «против сделки»), без «ушла за время
  раздумий»; пометки кода к вопросу — «ПОМЕТКА КОДА»;
- у троса и тейка — счёт как факт («уже ждал N раз из M; после M-го — слив по правилу»), и правило кода ему верно;
- память миссии: ожидание вне рынка — «лонг отсюда …, шорт …», дверь — «… в сторону плана», толмач — только
  заголовки.
Ни сети, ни ключей, ни data/ (tests/conftest.py: аудит брокера во временном файле, ручки на умолчаниях)."""
import asyncio
import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend import ai_pilot, config, explain, mission, trader_risk


class Broker:
    """Мини-биржа: заявки исполняются сразу, портфеля нет (dry-снимок), GetMaxLots — нет."""
    mode = "real"

    def __init__(self):
        self.placed, self.stops, self.cancelled = [], [], []
        self._n = 0

    async def place(self, figi, direction, lots, price=None, tag="", **kw):
        self._n += 1
        self.placed.append({"order_id": f"F-{self._n}", "direction": direction, "lots": int(lots), "price": price,
                            "tag": tag})
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
        return None

    async def portfolio(self):
        return {"mode": "dry", "positions": []}


class FakeMoney:
    """ai_v5.money_json: очереди ответов по маршрутам; записывает (маршрут, system, user)."""
    SILENT = "SILENT"

    def __init__(self):
        self.answers: dict = {}
        self.calls: list = []

    def queue(self, route, *answers):
        self.answers.setdefault(route, []).extend(answers)

    def count(self, route):
        return sum(1 for r, _, _ in self.calls if r == route)

    def last(self, route):
        return [(s, u) for r, s, u in self.calls if r == route][-1]

    async def money_json(self, system, user, *, route, max_tokens=None, attempt_timeout=None):
        self.calls.append((route, system, user))
        q = self.answers.get(route) or []
        a = q.pop(0) if q else {}
        if a == self.SILENT:
            raise asyncio.TimeoutError()
        return a

    def note_error(self, text, route=""):
        pass


@pytest.fixture(autouse=True)
def fake(monkeypatch, tmp_path):
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
    for k, v in (("PYTHIA_PARTNERS", False), ("PYTHIA_EXCHANGE_STOP", False), ("PYTHIA_ENTRY_CHECK", True),
                 ("PYTHIA_PROFIT_THINK", False), ("PYTHIA_MONEY_MODEL", "pro"), ("PYTHIA_SOFT_STOP", True),
                 ("PYTHIA_SOFT_TAKE", True), ("PYTHIA_ENTRY_TIMEOUT_SEC", 1200), ("PYTHIA_SILENT_RETRY_SEC", 120),
                 ("PYTHIA_ENTRY_FRESH_SEC", 1200), ("PYTHIA_ENTRY_DRIFT_PCT", 1.0), ("PYTHIA_ENTRY_CHECK_COOL_SEC", 300),
                 ("PYTHIA_SOFT_STOP_MAX_HOLDS", 3), ("PYTHIA_SOFT_TAKE_MAX_HOLDS", 2)):
        monkeypatch.setattr(config, k, v, raising=False)
    monkeypatch.setattr(ai_pilot, "market_clock", None)             # часы выключены: рынок открыт
    f = FakeMoney()
    monkeypatch.setattr(mission.ai_v5, "money_json", f.money_json)
    monkeypatch.setattr(mission.ai_v5, "note_error", f.note_error)
    monkeypatch.setattr(mission.ai_v5, "flash_json", AsyncMock(side_effect=AssertionError("узлы у денег — money_json")))
    return f


def make_pilot(play="auto"):
    m = mission.Mission("TEST", "Тест", "futures", play, 100000.0)
    mission._M["TEST"] = m
    p = mission.MissionPilot("TEST", deposit=100000.0, broker=Broker(), mission=m)
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


# ── 1. ЖДАТЬ у двери: причина — не «пометка к вопросу», а запись с исходом по цене ──────────────────────────────────
@pytest.mark.parametrize("do,inv,take,p1,move", [("BUY", 98.0, 110.0, 100.8, "+0.80"),
                                                 ("SELL", 102.0, 90.0, 99.5, "+0.50"),
                                                 ("BUY", 98.0, 110.0, 99.6, "-0.40")])
def test_door_wait_reason_is_data_not_code_note(fake, do, inv, take, p1, move):
    why = "пробой без объёма, жду закрепления"

    async def scenario():
        m, p = make_pilot()
        assert p.adopt_forecast(ex(do, None, take, inv))
        fake.queue("mission_entry", {"decision": "ЖДАТЬ", "why": why})
        await tick(p, 100.0)
        plan = p.plan
        assert fake.count("mission_entry") == 1 and p.pending is None and plan, p.last_action
        # причина — слова модели, отдельно от пометок кода
        assert plan["gate_wait_why"] == why and "gate_note" not in plan, plan
        assert "PRO у двери ответ ЖДАТЬ, повтор в" in p.last_action or "PRO у двери: ЖДАТЬ" in p.last_action
        await tick(p, 100.0)                                         # до ответа перепроверки — не спрашиваем
        assert fake.count("mission_entry") == 1
        # 5.4.4 (воля владельца 30.09: PRO — раз в 30 мин и по рыночным триггерам): ЖДАТЬ без уровня и срока — не
        # повтор через PYTHIA_ENTRY_CHECK_COOL_SEC, а следующий вопрос у двери после ответа дежурного PRO на перепроверке
        assert re.search(r"PRO у двери ответ ЖДАТЬ; следующий вопрос у двери — после ответа дежурного PRO на "
                         r"перепроверке \(плановая в \d\d:\d\d\) — ", p.last_action), p.last_action
        assert "велел" not in p.last_action and why not in p.last_action, p.last_action
        # перепроверка/триаж: строка ПРОВЕРКА ВХОДА — факт с ценой и сроком, причина в ней ровно один раз
        gl = p._gate_line(plan)
        assert re.search(r"PRO у двери ответил ЖДАТЬ в \d\d:\d\d при цене 100 \(сейчас 100, \+0\.00 % в сторону плана\) "
                         r"— следующий вопрос у двери после ответа дежурного PRO на перепроверке \(этого\)", gl), gl
        assert gl.count(why) == 1 and "велел" not in gl, gl
        # перепроверка ответила (5.4.4), цена прошла — второй вопрос
        p._last_review_ts = time.time() + 1.0
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "закрепились"})
        await tick(p, p1)
        assert fake.count("mission_entry") == 2
        _, u = fake.last("mission_entry")
        assert "ПОМЕТКА К ЭТОМУ ВОПРОСУ" not in u and "ПОМЕТКА КОДА" not in u, u
        assert u.count(why) == 1, "причина прошлого ЖДАТЬ — один раз, в блоке прошлых ответов"
        assert "ПРОВЕРКА ВХОДА:" not in u and "сейчас проверяет вход у двери" not in u, "у двери — без дубля строки"
        assert "ПРОШЛЫЕ ОТВЕТЫ У ДВЕРИ" in u
        line = [x for x in u.splitlines() if why in x][0]
        assert line.endswith(f"ЖДАТЬ — {why} → сейчас {p1:g} ({move} % в сторону плана)"), line
        head = re.search(r"ЖДАТЬ по этому плану: 1 раз за \d+ мин; цена 100 → ([\d.]+) \(([+-][\d.]+) % в сторону "
                         r"плана\); план протухнет через (\d+) мин", u)
        assert head and head.group(1) == f"{p1:g}" and head.group(2) == move, u[:3000]
        assert 0 < int(head.group(3)) <= int(ai_pilot.PLAN_TTL_SEC // 60)
        assert p.pending, "ВОЙТИ по живой цене — заявка"
        # флаг двери снят: перепроверка снова видит строку ПРОВЕРКА ВХОДА (пока план жив)
        assert not getattr(p, "_door_asking", False)

    asyncio.run(scenario())


def test_gate_line_is_back_for_review_after_door_question(fake):
    async def scenario():
        m, p = make_pilot()
        assert p.adopt_forecast(ex("BUY"))
        fake.queue("mission_entry", {"decision": "ЖДАТЬ", "why": "мутно", "wait_minutes": 10})
        await tick(p, 100.0)
        await tick(p, 100.2)                                         # до срока ЖДАТЬ — вопроса нет, цена живёт
        assert fake.count("mission_entry") == 1
        sit = p._situation_text(100.2)
        assert "ПРОВЕРКА ВХОДА: PRO у двери ответил ЖДАТЬ в" in sit and "мутно" in sit, sit
        assert "(сейчас 100.2, +0.20 % в сторону плана)" in sit, sit
        # серия ЖДАТЬ считается, молчание в серию не входит и ЖДАТЬ за ИИ не пишется
        p.plan["gate_after"] = 0.0
        fake.queue("mission_entry", FakeMoney.SILENT)
        await tick(p, 100.3)
        txt = p._gates_text(p.plan)
        assert txt.startswith("ЖДАТЬ по этому плану: 1 раз за "), txt
        assert "решения не было" in txt and txt.count("ЖДАТЬ — ") == 1, txt
        assert all("→ сейчас 100.3" in x for x in txt.splitlines()[1:]), txt

    asyncio.run(scenario())


def test_code_note_survives_silence_and_is_dropped_after_answer(fake):
    async def scenario():
        m, p = make_pilot()
        p.prices.append(101.5)
        p._plan_from_review("КУПИТЬ_СЕЙЧАС", {"invalidation": 98.0, "take": 110.0}, "т", 100.0, 101.5, snap_ts=time.time())
        note = p.plan["gate_note"]
        fake.queue("mission_entry", FakeMoney.SILENT)
        await tick(p, 101.5)
        assert p.plan["gate_note"] == note, "молчание — вопрос не отвечен: пометка кода остаётся к повтору"
        # 5.4.4: молчание у двери — без быстрого повтора: следующий вопрос после ответа дежурного PRO на перепроверке
        p._last_review_ts = time.time() + 1.0
        fake.queue("mission_entry", {"decision": "ЖДАТЬ", "why": "откат к 100.8"})
        await tick(p, 101.5)
        assert "ПОМЕТКА КОДА: " + note in fake.last("mission_entry")[1]
        assert "gate_note" not in p.plan and p.plan["gate_wait_why"] == "откат к 100.8", p.plan
        p._last_review_ts = time.time() + 2.0                      # 5.4.4: ЖДАТЬ без уровня и срока — до перепроверки
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "ок"})
        await tick(p, 101.4)
        u = fake.last("mission_entry")[1]
        assert "ПОМЕТКА" not in u, "пометка кода отвечена — на следующий вопрос не переносится; причина ЖДАТЬ — не пометка"

    asyncio.run(scenario())


# ── 2. дрейф — нейтрально и с направлением ─────────────────────────────────────────────────────────────────────────
def test_drift_toward_deal_is_neutral_with_direction(fake):
    P = mission.MissionPilot
    assert P._drift_text("long", 100, 101.5) == ("с момента решения цена прошла 1.50 % в сторону сделки (решение при 100, "
                                                 "сейчас 101.5): вход сейчас — по 101.5")
    assert P._drift_text("long", 100, 98.5, tail=False) == ("с момента решения цена прошла 1.50 % против сделки "
                                                            "(решение при 100, сейчас 98.5)")
    assert "в сторону сделки" in P._drift_text("short", 100, 98.5) and "против сделки" in P._drift_text("short", 100, 101)
    assert P._toward_plan("short", 100, 99) == pytest.approx(1.0) and P._toward_plan("long", 0, 99) is None

    async def scenario():
        m, p = make_pilot()
        p.prices.append(101.5)
        p._plan_from_review("КУПИТЬ_СЕЙЧАС", {"invalidation": 98.0, "take": 110.0}, "импульс", 100.0, 101.5,
                            snap_ts=time.time())
        assert p.plan["entry"] is None and "в сторону сделки" in p.plan["gate_note"]
        assert "за время раздумий" not in p.plan["why"] and "ушла" not in p.plan["why"], p.plan["why"]
        fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "импульс жив"})
        await tick(p, 101.5)
        u = fake.last("mission_entry")[1]
        assert ("ПОМЕТКА КОДА: с момента решения цена прошла 1.50 % в сторону сделки (решение при 100, сейчас 101.5): "
                "вход сейчас — по 101.5") in u
        assert "за время раздумий" not in u and "цена ушла" not in u and p.pending
        # SELL: цена упала — это тоже «в сторону сделки»
        m2, p2 = make_pilot()
        p2.prices.append(98.4)
        p2._plan_from_review("ПРОДАТЬ_СЕЙЧАС", {"invalidation": 102.0, "take": 95.0}, "т", 100.0, 98.4, snap_ts=time.time())
        assert p2.plan["gate_note"].startswith("с момента решения цена прошла 1.60 % в сторону сделки"), p2.plan

    asyncio.run(scenario())


# ── 3. трос и тейк: счёт как факт, правило кода ему верно ───────────────────────────────────────────────────────
async def _open_long(p, fake, inv=98.0, take=103.0):
    assert p.adopt_forecast(ex("BUY", None, take, inv))
    fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "ок"})
    await tick(p, 100.0)
    await tick(p, 100.0)
    assert p.position, p.last_action
    return p.position


def test_guard_and_take_show_holds_as_fact(fake):
    async def scenario():
        m, p = make_pilot()
        pos = await _open_long(p, fake)
        pos["holds"] = 1
        fake.queue("mission_guard", {"decision": "ЖДАТЬ", "why": "вынос стопов"})
        await p._stop_guard(97.9, pos)
        s, _ = fake.last("mission_guard")
        assert "У этого троса уже ждал 1 раз из 3; после 3-го — слив по правилу, без вопроса." in s, s
        assert "можно ещё" not in s
        pos["take_holds"] = 1
        fake.queue("mission_take", {"decision": "ПОДЕРЖАТЬ", "why": "разгон"})
        await p._take_guard(103.1, pos)
        s2, _ = fake.last("mission_take")
        assert "Уже держал у тейка 1 раз из 2; после 2-го — фиксация по правилу, без вопроса." in s2, s2
        assert "можно ещё" not in s2
        # правило кода совпадает с текстом: после 3-го ЖДАТЬ за триггером — слив без вопроса
        pos["holds"] = 3
        n = fake.count("mission_guard")
        placed = len(p.broker.placed)
        await tick(p, 97.5)
        assert fake.count("mission_guard") == n and len(p.broker.placed) > placed, p.last_action
        assert p.broker.placed[-1]["direction"] in ("SELL", "sell", 2, "ORDER_DIRECTION_SELL"), p.broker.placed[-1]

    asyncio.run(scenario())


# ── 4. память миссии: ожидание — в чью пользу; толмач — только заголовки ─────────────────────────────────────────
def test_memory_waiting_shows_both_sides_and_door_plan_side(fake):
    now = time.time()
    m = SimpleNamespace(
        ticker="SBER", name="Сбербанк", memory="", memory_ts=None, handoffs=[],
        reviews=[{"ts": now, "choice": "ЖДЁМ", "why": "прорыва нет", "price": 100.0}],
        explain=[{"ts": now, "kind": "review", "title": "Перепроверка: ЖДЁМ",
                  "text": "Пилот ждёт пробоя 102, владельцу следить за 101.5.", "ok": True}],
        pilot=SimpleNamespace(guards=[], profits=[], position=None,
                              gates=[{"ts": now, "decision": "ЖДАТЬ", "why": "откат к 99.7", "price": 100.0,
                                      "side": "short"}]))
    acc = explain._accumulated(m, {"price": 101.2})
    assert "ЖДЁМ @100.0 → сейчас 101.2 (лонг отсюда +1.2 %, шорт -1.2 %) — прорыва нет" in acc, acc
    assert "ЖДАТЬ @100.0 → сейчас 101.2 (-1.2 % в сторону плана) — откат к 99.7" in acc, acc
    assert "Перепроверка: ЖДЁМ" in acc and "Пилот ждёт пробоя" not in acc and "следить" not in acc, acc
    s, _ = explain.memory_prompt({"ticker": "SBER", "name": "Сбербанк"}, "тест", acc, "")
    assert "для ожидания — сколько прошла цена без нас в сторону идеи, для входа — за нас или против" in s, s
    st, _ = explain.prompt({"ticker": "SBER", "name": "Сбербанк"}, [], [])
    assert "только из событий: взведённый вход, уровни, когда следующее решение" in st, st


def test_memory_after_real_door_wait_carries_plan_side(fake):
    async def scenario():
        m, p = make_pilot()
        assert p.adopt_forecast(ex("SELL", None, 90.0, 102.0))
        fake.queue("mission_entry", {"decision": "ЖДАТЬ", "why": "подожду отскока"})
        await tick(p, 100.0)
        acc = explain._accumulated(m, {"price": 99.0})
        assert "ЖДАТЬ @100.0 → сейчас 99.0 (+1.0 % в сторону плана) — подожду отскока" in acc, acc

    asyncio.run(scenario())
