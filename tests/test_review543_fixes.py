# -*- coding: utf-8 -*-
"""ПИФИЯ 5.4.3 — исправления по ревью (часть F1, код): на фейках, без сети, ключей и data/.

D1 — будильник ЖДЁМ снимается только явным решением (ЖДЁМ с другим entry, вход/план, НОВЫЙ_АНАЛИЗ, начало совета,
позиция/заявка, сработал и PRO ответил); ЖДЁМ без entry его оставляет; уровень пройден, пока PRO думал, — повод сразу.
D2 — сработавший уровень не будит повторно в пределах PYTHIA_EVENT_COOL_SEC; в ситуации «будильник на X уже срабатывал».
D3 — будильник только из entry ответа ЖДЁМ вне рынка (решение принималось без позиции).
D5 — трос: hold_until_price в коридоре «трос < X < цена» — новый триггер, у него вопрос сразу; вне коридора — не принят,
видно в записи и last_action.
D6 — ДЕРЖАТЬ: повтор прежнего стопа хуже запертой прибыли код не применяет; отказы (сторона, мусор) видны модели.
Плюс: серия ЖДЁМ — свой счётчик (память режет m.reviews), память «в чью пользу» по pos_side, метки модели (КУПИТЬ,
ДЕРЖАТЬ), запас открытия рынка у wait_level, «совет идёт», уровень плана в дрейфе, числа-не-уровни в ориентире WAIT."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend import ai_pilot, ai_v5, api_chat, config, explain, mission, trader_risk

PRICE = 100.0


class Broker:
    """Мини-биржа: заявки исполняются сразу, портфеля нет (dry-снимок)."""
    mode = "real"

    def __init__(self):
        self.placed, self.stops = [], []
        self._n = 0

    async def place(self, figi, direction, lots, price=None, tag="", **kw):
        self._n += 1
        self.placed.append({"order_id": f"F-{self._n}", "direction": direction, "lots": int(lots), "price": price, "tag": tag})
        return {"ok": True, "order_id": f"F-{self._n}"}

    async def order_state(self, oid, **kw):
        return {"ok": True, "filled": True, "status": "FILL", "exec_lots": 0}

    async def cancel(self, oid, **kw):
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


class FakeAI:
    """ai_v5.money_json / pro_json: очереди ответов по маршрутам; during — что «рынок делает», пока PRO думает."""

    def __init__(self):
        self.answers: dict = {}
        self.calls: list = []
        self.during = None

    def queue(self, route, *answers):
        self.answers.setdefault(route, []).extend(answers)

    def count(self, route):
        return sum(1 for r, _ in self.calls if r == route)

    def last_user(self, route):
        return [u for r, u in self.calls if r == route][-1]

    async def _answer(self, route, user):
        self.calls.append((route, user))
        if self.during is not None:
            fn, self.during = self.during, None
            fn()
        q = self.answers.get(route) or []
        return q.pop(0) if q else {}

    async def money_json(self, system, user, *, route, max_tokens=None, attempt_timeout=None):
        return await self._answer(route, user)

    async def pro_json(self, system, user, *, route="pro", max_tokens=None, attempt_timeout=None):
        return await self._answer(route, user)

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
    monkeypatch.setattr(mission, "_scan_status_raw", lambda t: None)
    monkeypatch.setattr(mission, "_mod", lambda name: None)
    try:
        from backend import astro
        monkeypatch.setattr(astro, "acontext", AsyncMock(return_value=None))
    except Exception:                                  # noqa: BLE001 — без неба перепроверка живёт
        pass
    for k, v in (("PYTHIA_PARTNERS", False), ("PYTHIA_EXCHANGE_STOP", False), ("PYTHIA_ENTRY_CHECK", True),
                 ("PYTHIA_PROFIT_THINK", True), ("PYTHIA_MONEY_MODEL", "pro"), ("PYTHIA_EVENT_TRIAGE", True),
                 ("PYTHIA_EVENT_COOL_SEC", 900)):
        monkeypatch.setattr(config, k, v, raising=False)
    monkeypatch.setattr(ai_pilot, "market_clock", None)             # часы выключены: рынок открыт
    f = FakeAI()
    monkeypatch.setattr(mission.ai_v5, "money_json", f.money_json)
    monkeypatch.setattr(mission.ai_v5, "pro_json", f.pro_json)
    monkeypatch.setattr(mission.ai_v5, "note_error", f.note_error)
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


def long_pos(entry=100.0, inv=98.0, take=104.0, age=3600.0):
    return {"side": "long", "entry": entry, "lots": 3, "take": take, "invalidation": inv, "inv0": inv,
            "opened_ts": time.time() - age, "stop_id": None, "floating": 0.0}


def wakes(m):
    return [h for h in m.handoffs if h.get("kind") == "wait_level"]


async def armed(fake, p, level=101.0, at=100.0):
    """PRO ответил ЖДЁМ с уровнем → будильник (снимок цены at)."""
    p.prices.append(at)
    fake.queue("mission_review", {"choice": "ЖДЁМ", "why": f"пробоя {level:g} нет", "entry": level})
    await p._review(at)
    assert p._wake and p._wake["level"] == level and not p._wake["fired"], p._wake


# ── D1: будильник снимается только явным решением ───────────────────────────────────────────────────────────────────
def test_wait_without_entry_keeps_alarm_and_level_still_wakes(fake):
    """Путь 1 ревью: плановая перепроверка ЖДЁМ без entry («будильник стоит — ждём его») будильник не стирает."""
    async def scenario():
        m, p = make_pilot()
        await tick(p, 100.0, n=2)
        await armed(fake, p)
        ts0 = p._wake["ts"]
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "будильник на 101 стоит — ждём его"})
        p.prices.append(100.3)
        await p._review(100.3)
        w = p._wake
        assert w and w["level"] == 101.0 and w["ts"] == ts0 and not w["fired"], "ЖДЁМ без entry — будильник остаётся"
        assert m.reviews[-1]["wake"] == 101.0 and m.reviews[-1]["wake_kept"] is True, m.reviews[-1]
        assert "будильник у 101 остаётся (из ЖДЁМ " in p.last_action, p.last_action
        # тот же уровень ещё раз — прежний будильник (время и цена решения не переписаны)
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "всё ещё 101", "entry": 101.0})
        await p._review(100.4)
        assert p._wake is w and p._wake["ts"] == ts0 and p._wake["ref"] == 100.0
        p._last_review_ts -= mission.EVENT_MIN_GAP_SEC + 60
        p._wake_watch(101.4)
        assert len(wakes(m)) == 1 and "прошла уровень 101" in wakes(m)[0]["reason"], m.handoffs

    asyncio.run(scenario())


def test_other_entry_moves_alarm_and_explicit_answers_clear_it(fake):
    async def scenario():
        m, p = make_pilot()
        await tick(p, 100.0, n=2)
        await armed(fake, p)
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "уровень ниже", "entry": 99.0})
        await p._review(100.0)
        assert p._wake["level"] == 99.0 and p._wake["dir"] == "down" and not m.reviews[-1].get("wake_kept")
        fake.queue("mission_review", {"choice": "НОВЫЙ_АНАЛИЗ", "why": "новость о дивиденде"})
        await p._review(100.0)
        assert p._wake is None, "НОВЫЙ_АНАЛИЗ снимает будильник"
        await armed(fake, p)
        fake.queue("mission_review", {"choice": "КУПИТЬ", "why": "пробой ближе", "entry": 100.8, "entry_kind": "прорыв",
                                      "invalidation": 99.5})
        await p._review(100.0)
        assert p._wake is None and p.plan and p.plan["entry"] == 100.8, "вход/план снимает будильник"
        p.plan = None
        await armed(fake, p)
        assert p.adopt_forecast({"exec": {"do": "WAIT", "wait_for": "т", "why": "т"}}) and p._wake is None, \
            "новый приказ совета снимает будильник"

    asyncio.run(scenario())


def test_council_start_drops_alarm(fake, monkeypatch):
    async def scenario():
        m, p = make_pilot()
        await tick(p, 100.0, n=2)
        await armed(fake, p)
        monkeypatch.setattr(mission, "_scan_start", AsyncMock(return_value={"ok": True}))
        monkeypatch.setattr(mission.market_ctx, "build", AsyncMock(side_effect=RuntimeError("досье недоступно")))
        monkeypatch.setattr(mission.bus, "end_run", lambda *a, **k: None)
        assert await mission._council(m, "тест", first=False) is None
        assert p._wake is None, "начало совета снимает будильник"

    asyncio.run(scenario())


def test_level_crossed_while_pro_thinks_wakes_right_after_answer(fake):
    """Путь 2 ревью: цена прошла уровень, пока PRO думал; ответ без entry — повод wait_level сразу после записи."""
    async def scenario():
        m, p = make_pilot()
        await tick(p, 100.0, n=2)
        await armed(fake, p)
        p._last_review_ts -= mission.EVENT_MIN_GAP_SEC + 60
        p._review_busy = True                              # тик во время раздумий: будильник молчит
        p._wake_watch(101.4)
        assert not wakes(m) and not p._wake["fired"]
        p._review_busy = False
        p.prices.append(100.5)                             # снимок до уровня …
        fake.during = lambda: p.prices.append(101.4)       # … а пока PRO думает, цена за уровнем
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "ждём 101"})
        await p._review(100.5)
        assert len(wakes(m)) == 1, m.handoffs
        h = wakes(m)[0]
        assert "цена 101.4 прошла уровень 101" in h["reason"] and "(пока ты думал над прошлым ответом)" in h["reason"], h
        assert p._wake["fired"] and p.review_ts <= p._last_review_ts + mission.EVENT_MIN_GAP_SEC + 1, "обычный пейсинг"
        assert "сработал в" in p._situation_text(101.4)
        # ответ на сработавший будильник снимает его
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "вынос без объёма"})
        await p._review(101.4)
        assert p._wake is None

    asyncio.run(scenario())


def test_new_alarm_crossed_during_think_wakes_and_seen_level_is_consumed(fake):
    async def scenario():
        m, p = make_pilot()
        await tick(p, 100.0, n=2)
        p.prices.append(100.0)
        fake.during = lambda: p.prices.append(101.3)
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "жду 101", "entry": 101.0})
        await p._review(100.0)
        assert p._wake and p._wake["fired"] and len(wakes(m)) == 1, (p._wake, m.handoffs)
        # PRO уже видел цену за уровнем (снимок за уровнем) — будильник отработал без нового повода
        m.handoffs.clear()
        p._wake_fired.clear()
        p._wake = {"level": 101.0, "ref": 100.0, "dir": "up", "ts": time.time(), "why": "т", "fired": None}
        sit = p._situation_text(101.2)
        assert "цена уже за уровнем (сейчас 101.2); это не вход" in sit, sit
        p.prices.append(101.2)
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "видел, вынос"})
        await p._review(101.2)
        assert p._wake is None and not wakes(m) and p._wake_fired, "PRO решил по уровню — будильник снят"

    asyncio.run(scenario())


# ── D2: без пинг-понга у одного уровня ──────────────────────────────────────────────────────────────────────────────
def test_same_level_does_not_wake_again_within_event_cool(fake):
    async def scenario():
        m, p = make_pilot()
        await tick(p, 100.0, n=2)
        await armed(fake, p)
        p._last_review_ts -= mission.EVENT_MIN_GAP_SEC + 60
        p._wake_watch(101.05)
        assert len(wakes(m)) == 1
        fired_ts = p._wake["fired"]
        # PRO снова ЖДЁМ у 101 (снимок 101.05 → будильник вниз); цена 100.95 — тот же уровень, окно не прошло
        p.prices.append(101.05)
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "у 101 без объёма", "entry": 101.0})
        await p._review(101.05)
        assert p._wake and p._wake["dir"] == "down" and not p._wake["fired"]
        p._last_review_ts -= mission.EVENT_MIN_GAP_SEC + 60
        for px in (100.95, 101.06, 100.94):
            p._wake_watch(px)
        assert len(wakes(m)) == 1, "тот же уровень в окне PYTHIA_EVENT_COOL_SEC — не будит"
        sit = p._situation_text(100.95)
        assert f"будильник на 101 уже срабатывал в {p._hhmm(fired_ts)} — повторно разбудит не раньше " \
               f"{p._hhmm(fired_ts + 900)}" in sit, sit
        # допуск max(шаг цены, 0.05 %): 101.03 — тот же уровень, 101.2 — другой
        assert p._wake_recent(101.03) is not None and p._wake_recent(101.2) is None
        # окно прошло — уровень снова может разбудить
        p._wake_fired[-1]["ts"] -= 901
        p._wake_watch(100.94)
        assert len(wakes(m)) == 2 and p._wake["fired"]

    asyncio.run(scenario())


def test_fired_line_names_when_same_level_may_wake_again(fake):
    async def scenario():
        m, p = make_pilot()
        await tick(p, 100.0, n=2)
        await armed(fake, p)
        p._last_review_ts -= mission.EVENT_MIN_GAP_SEC + 60
        p._wake_watch(101.2)
        f = p._wake["fired"]
        sit = p._situation_text(101.2)
        assert (f"сработал в {p._hhmm(f)}, это и есть повод перепроверки; тот же уровень в ЖДЁМ повторно разбудит не "
                f"раньше {p._hhmm(f + 900)}") in sit, sit

    asyncio.run(scenario())


# ── D3 и [low]: будильник — только из решения вне рынка ─────────────────────────────────────────────────────────────
def test_hold_answer_after_position_closed_during_think_sets_no_alarm(fake):
    async def scenario():
        m, p = make_pilot()
        await tick(p, 100.0, n=2)
        p.position = long_pos()
        p.prices.append(100.0)

        def closed():
            p.position = None                           # трос закрыл позицию, пока PRO думал
        fake.during = closed
        fake.queue("mission_review", {"choice": "ДЕРЖАТЬ", "why": "добор у 101", "entry": 101.0, "invalidation": 98.5})
        await p._review(100.0)
        assert p._wake is None and m.reviews[-1]["in_pos"] is True and "wake" not in m.reviews[-1], (p._wake, m.reviews[-1])
        assert "БУДИЛЬНИК" not in p._situation_text(100.0)

    asyncio.run(scenario())


# ── запас открытия рынка у wait_level ───────────────────────────────────────────────────────────────────────────────
def test_wait_level_respects_market_open_grace(fake):
    async def scenario():
        m, p = make_pilot()
        await tick(p, 100.0, n=2)
        await armed(fake, p)
        p._last_review_ts = time.time() - 3600
        p._opened_ts = time.time()                         # рынок только что открылся (гэп за уровень)
        p._wake_watch(101.5)
        assert len(wakes(m)) == 1
        assert p.review_ts >= p._opened_ts + ai_pilot.OPEN_REVIEW_GRACE_SEC - 1, p.review_ts - time.time()

    asyncio.run(scenario())


# ── D6: ДЕРЖАТЬ — повтор прежнего стопа не отдаёт запертую прибыль, отказы видны ──────────────────────────────────
def test_hold_repeat_of_old_stop_keeps_locked_profit_and_refusals_are_visible(fake):
    async def scenario():
        m, p = make_pilot()
        await tick(p, 104.5, n=2)
        m.exec, m.exec_ts = {"do": "BUY", "entry": None, "invalidation": 98.0, "take": 104.0, "why": "т"}, time.time()
        pos = long_pos(inv=102.0, take=106.0)
        pos.update(take_holds=1, inv0=102.0, lock_from=98.0)
        p._set_levels(pos, None, 102.0)
        p.position = pos
        hard0 = pos["hard_stop"]
        p.prices.append(104.5)
        fake.queue("mission_review", {"choice": "ДЕРЖАТЬ", "why": "ход жив", "invalidation": 98.0, "take": 104.0})
        await p._review(104.5)
        assert pos["invalidation"] == 102.0 and pos["take"] == 106.0 and pos["hard_stop"] == hard0 and pos["take_holds"] == 1
        rt = m.reviews[-1]["retune"]
        assert not rt["applied"] and len(rt["refused"]) == 2, rt
        assert "стоп 98 — повтор прежнего (стоп приказа совета) хуже запертой прибыли: триггер 102 остаётся" in rt["refused"][0]
        assert "тейк 104 не выше цены 104.5 (long) — тейк прежний 106" in rt["refused"][1]
        assert p.last_action.startswith("перепроверка: ДЕРЖАТЬ; не принято: стоп 98"), p.last_action
        sit = p._situation_text(104.5)
        assert ": ДЕРЖАТЬ — ход жив" in sit and "код не принял: стоп 98 — повтор прежнего" in sit, sit
        # мусор — отказ с причиной; новый стоп PRO (не повтор) главнее запертой прибыли (v5.4.1)
        fake.queue("mission_review", {"choice": "ДЕРЖАТЬ", "why": "шире", "invalidation": "abc", "take": 107.0})
        await p._review(104.5)
        rt2 = m.reviews[-1]["retune"]
        assert rt2["refused"] == ["стоп «abc» — не число больше нуля"] and rt2["applied"] == ["тейк 106 → 107"], rt2
        fake.queue("mission_review", {"choice": "ДЕРЖАТЬ", "why": "шире", "invalidation": 101.0})
        await p._review(104.5)
        assert pos["invalidation"] == 101.0 and "трос 102 → 101 (отодвинут)" in m.reviews[-1]["retune"]["applied"][0]
        # стоп прошлого ответа в этой позиции — тоже «прежний» при запертой прибыли
        pos["take_holds"], pos["profit_lock"] = 1, True
        p._set_levels(pos, None, 103.0)
        fake.queue("mission_review", {"choice": "ДЕРЖАТЬ", "why": "повтор", "invalidation": 101.0})
        await p._review(104.5)
        assert pos["invalidation"] == 103.0 and "стоп прошлого ответа перепроверки" in m.reviews[-1]["retune"]["refused"][0]

    asyncio.run(scenario())


def test_hold_tolmach_refs_are_applied_levels(fake, monkeypatch):
    got: list = []
    monkeypatch.setattr(mission, "_tolmach", lambda m, kind, title, detail="", refs=None: got.append((title, detail, refs)))

    async def scenario():
        m, p = make_pilot()
        await tick(p, 101.0, n=2)
        p.position = long_pos()
        p.prices.append(101.0)
        fake.queue("mission_review", {"choice": "ДЕРЖАТЬ", "why": "подтягиваю", "invalidation": 99.0, "take": 100.0})
        await p._review(101.0)
        title, detail, refs = got[-1]
        assert title == "Перепроверка: ДЕРЖАТЬ" and refs["choice"] == "ЖДЁМ", got[-1]
        assert refs["invalidation"] == 99.0 and refs["take"] is None, "толмачу — применённые уровни"
        assert "трос 98 → 99" in detail and "не принято: тейк 100 не выше цены 101" in detail, detail

    asyncio.run(scenario())


# ── D5: трос — hold_until_price однозначно ──────────────────────────────────────────────────────────────────────────
def test_guard_hold_until_price_is_new_trigger_or_visibly_refused(fake):
    async def scenario():
        m, p = make_pilot()
        await tick(p, 97.9, n=2)
        pos = long_pos(inv=98.0)
        p._set_levels(pos, None, 98.0)
        p.position = pos
        hard = pos["hard_stop"]
        assert 96 < hard < 97
        await p._apply_guard({"decision": "ЖДАТЬ", "why": "вынос", "hold_until_price": 97.0, "hold_minutes": 10}, 97.9, pos)
        g = p.guards[-1]
        assert pos["invalidation"] == 97.0 and pos["hard_stop"] == hard and pos["guard_next"] == 0.0, pos
        assert g["hold_until"] == 97.0 and g["hold_minutes"] is None and g["pos_side"] == "long", g
        assert "новый триггер 97 — у него спрошу снова" in p.last_action, p.last_action
        # за хвост вне коридора (за тросом / выше цены) — не принят, триггер прежний, срок hold_minutes
        await p._apply_guard({"decision": "ЖДАТЬ", "why": "ещё", "hold_until_price": round(hard - 0.1, 2),
                              "hold_minutes": 5}, 96.9, pos)
        g2 = p.guards[-1]
        assert pos["invalidation"] == 97.0 and g2["hold_until"] is None and g2["hold_until_ai"] == round(hard - 0.1, 2)
        assert f"за аварийным тросом {hard:g}" in g2["hold_note"] and "не принят" in p.last_action, (g2, p.last_action)
        assert 290 < pos["guard_next"] - time.time() <= 300
        assert pos["holds"] == 2
        gt = p._guards_text()                              # модель у троса видит, что код сделал с её числом
        assert "трос: ЖДАТЬ @97.9 — вынос [новый триггер 97.0]" in gt and "ещё [hold_until_price" in gt \
            and "не принят: за аварийным тросом" in gt, gt

    asyncio.run(scenario())


# ── серия ЖДЁМ — свой счётчик ───────────────────────────────────────────────────────────────────────────────────────
def test_wait_streak_survives_memory_trim_and_restart(fake, monkeypatch):
    async def scenario():
        m, p = make_pilot()
        for i in range(12):
            px = 100.0 + i * 0.1
            p.prices.append(px)
            fake.queue("mission_review", {"choice": "ЖДЁМ", "why": f"ждём {i}"})
            await p._review(px)
            del m.reviews[:-5]                             # память режет сырой список (explain.KEEP_REVIEWS)
        ws = m.wait_streak
        assert ws["n"] == 12 and ws["price"] == 100.0 and ws["hi"] == pytest.approx(101.1), ws
        ws["ts"] = time.time() - 7 * 3600
        sit = p._situation_text(101.2)
        assert "ЖДЁМ подряд: 12 за 420 мин (с " in sit and "от первого ЖДЁМ (100) +1.20 %" in sit, sit
        dig = mission._reviews_digest(m.reviews, 101.2, m.wait_streak)
        assert "ЖДЁМ ×12 (" in dig and "цена 100 → 101.2 (+1.20 %) сейчас" in dig, dig
        assert "ЖДЁМ ×12" in mission._prev_text(m)
        # переживает рестарт (персист как соседние поля)
        saved: dict = {}
        monkeypatch.setattr(mission.store_v5, "mission_put", lambda t, d: saved.update(d))
        mission._persist(m)
        monkeypatch.setattr(mission.store_v5, "mission_get", lambda t: saved)
        assert mission._from_store("TEST").wait_streak["n"] == 12
        # любой другой ответ снимает серию
        fake.queue("mission_review", {"choice": "КУПИТЬ", "why": "пробой", "invalidation": 99.0})
        await p._review(101.2)
        assert m.wait_streak is None

    asyncio.run(scenario())


# ── память: «в чью пользу» по стороне из записи ─────────────────────────────────────────────────────────────────────
def test_review_record_keeps_position_side_for_memory(fake):
    async def scenario():
        m, p = make_pilot()
        await tick(p, 100.0, n=2)
        p.position = long_pos()
        p.prices.append(100.0)
        fake.queue("mission_review", {"choice": "ДЕРЖАТЬ", "why": "ход жив"})
        await p._review(100.0)
        r = m.reviews[-1]
        assert r["in_pos"] is True and r["pos_side"] == "long", r
        p.position = None                                  # узел «закрытие»: позиции уже нет
        acc = explain._accumulated(m, {"price": 101.0})
        assert "ДЕРЖАТЬ @100.0 → сейчас 101.0 (за лонг +1.0 %) — ход жив" in acc, acc

    asyncio.run(scenario())


# ── метки модели: КУПИТЬ / ДЕРЖАТЬ вместо канона ────────────────────────────────────────────────────────────────────
def test_model_sees_labels_not_canon(fake):
    async def scenario():
        m, p = make_pilot()
        await tick(p, 100.0, n=2)
        p.prices.append(100.0)
        fake.queue("mission_review", {"choice": "КУПИТЬ", "why": "откат к 99.5", "entry": 99.5, "entry_kind": "откат",
                                      "invalidation": 98.5})
        await p._review(100.0)
        assert m.reviews[-1]["choice"] == "КУПИТЬ_СЕЙЧАС", "канон в записи прежний"
        sit = p._situation_text(100.0)
        assert "): КУПИТЬ — откат к 99.5" in sit and "КУПИТЬ_СЕЙЧАС" not in sit, sit
        assert "перепроверка: КУПИТЬ → long" in p.last_action, p.last_action
        ctx = mission._explain_ctx(m)
        assert ": КУПИТЬ — откат к 99.5" in ctx["decisions"] and "КУПИТЬ_СЕЙЧАС" not in ctx["decisions"]
        st = mission.status("TEST")["reviews"][-1]
        assert st["choice"] == "КУПИТЬ_СЕЙЧАС" and st["in_pos"] is False and st["price"] == 100.0, st
        assert mission._choice_label({"choice": "ЖДЁМ", "in_pos": True}) == "ДЕРЖАТЬ"
        assert mission._choice_label({"choice": "ЖДЁМ", "in_pos": False}) == "ЖДЁМ"

    asyncio.run(scenario())


def test_chat_shows_labels_and_neutral_state(monkeypatch):
    snap = {"active": "SBER", "missions": {"SBER": {
        "ticker": "SBER", "name": "Сбербанк", "asset_class": "share", "play": "auto", "phase": "in_position",
        "price": 101.0, "started_ts": time.time(), "exec_ts": time.time(), "exec": {"do": "HOLD", "why": "т"},
        "pilot": {"state": "В_ПОЗИЦИИ", "last_action": "совет: вне рынка — ждал: пробой", "mode": "real",
                  "position": {"side": "long", "lots": 3, "entry": 100.0}, "plan": None, "guards": []},
        "reviews": [{"ts": time.time(), "choice": "ЖДЁМ", "why": "держим", "price": 100.0, "in_pos": True}],
        "explain": [{"ts": time.time(), "title": "Перепроверка: ДЕРЖАТЬ", "text": "Пилот ждёт пробоя 101."}]}}}
    monkeypatch.setitem(api_chat._STUBS, "mission", SimpleNamespace(snapshot=lambda: snap))
    txt, _ = api_chat._mission_text()
    assert ": ДЕРЖАТЬ @100 → сейчас 101 (+1.00 %) — держим" in txt, txt
    assert "Пилот: в позиции long 3 лот @100.0;" in txt and "ждал: пробой" not in txt, txt
    assert "Толмач писал владельцу (только заголовки, пересказ не факт): " in txt and "Пилот ждёт пробоя" not in txt


# ── «совет идёт» и длительность прерванного совета ──────────────────────────────────────────────────────────────────
def test_council_line_while_council_runs_and_after_timeout(fake):
    async def scenario():
        m, p = make_pilot()
        m.council_ts, m.council_dur = time.time() - 120, 840.0
        m.council_task = asyncio.get_running_loop().create_future()      # совет идёт
        sit = p._situation_text(100.0)
        assert f"Полный совет идёт с {p._hhmm(m.council_ts)} (уже 2 мин); прошлый длился 14 мин" in sit, sit
        assert "Последний полный совет: 2 мин назад, длился 14" not in sit
        m.council_task.cancel()
        m.council_task = None
        m.council_ts = time.time() - 3600
        p._council_timeout(3600)
        assert 3590 <= m.council_dur <= 3610, m.council_dur
        assert "Последний полный совет прерван 0 мин назад (не уложился в срок, шёл 60 мин)" in p._situation_text(100.0)

    asyncio.run(scenario())


# ── дрейф у плана с уровнем — «уровень плана», а не «цена решения» ──────────────────────────────────────────────────
def test_drift_note_names_plan_level(fake, monkeypatch):
    async def scenario():
        m, p = make_pilot()
        monkeypatch.setattr(config, "PYTHIA_ENTRY_CHECK", False)
        p.plan = {"side": "long", "entry": 101.0, "kind": "прорыв", "invalidation": 99.0, "take": 106.0, "why": "т",
                  "ts": time.time(), "src": "review", "snap_price": 100.0, "snap_ts": time.time()}
        assert await p._entry_gate(102.5, book(102.5)) is False and p.plan is None
        rr = p._review_reason or ""
        assert ("от уровня плана цена прошла 1.49 % в сторону сделки (уровень плана 101, решение при 100, сейчас 102.5)"
                in rr) and "решение при 101" not in rr, rr

    asyncio.run(scenario())


# ── числа-не-уровни в ориентире WAIT ────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text, price, want", [
    ("выше 71.5 при RSI выше 70", 69.8, [71.5]),
    ("закрепление 3 свечи выше 3.05", 3.0, [3.05]),
    ("в течение 1 сессии выше 1.02", 1.0, [1.02]),
    ("пробой 101 на объёме 100 тыс. лотов", 100.0, [101.0]),
    ("MA200 и EMA20 на M15, выше 101", 100.0, [101.0]),
    ("отбился 2 раза от 99, объём 3× среднего", 100.0, [99.0]),
    ("Si 95.5 и IMOEX 2800, пробой 101", 100.0, [101.0]),
    ("закрепление выше 15.08", 15.0, [15.08]),
    ("пробой 12.10", 12.0, [12.1]),
    ("пробой 3.12 и откат к 3.05", 3.1, [3.12, 3.05]),
    ("закрепление выше 1 250", 1240.0, [1250.0]),
    ("после отчёта 31 октября, выше 30.5", 30.0, [30.5]),
    ("после 30.09.2026 и 30.09 в 10:00 — пробой 30.5", 30.0, [30.5]),
    ("выше 101 закрытием часа, 2 закрытия подряд", 100.0, [101.0]),
])
def test_levels_from_text_skips_counters_and_keeps_dd_mm_prices(text, price, want):
    assert mission._levels_from_text(text, price) == want
