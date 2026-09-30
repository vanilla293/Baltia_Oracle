# -*- coding: utf-8 -*-
"""ПИФИЯ v5.4.2 «СВОБОДНЫЙ ПИЛОТ»: ритм пилота миссии на фейках — код не решает за ИИ ни в одну сторону, а будит его.

Приказ WAIT: уровни приказа под наблюдением и ритм PYTHIA_WAIT_REVIEW_SEC; прокол сканера без плана — «вне рынка»;
поводы пилота вне рынка без плана (приказ протух, лотов 0) — перепроверка скоро; первый совет без приказа — пилот без
плана, а не мёртвая миссия; переворот при молодой позиции — выход без переворота; НОВЫЙ_АНАЛИЗ в окне совета —
отложен, не потерян; совет по поводу дольше PYTHIA_COUNCIL_MAX_SEC — прерван, пилот разморожен; мысль о прибыли не
теряется, пока дежурный PRO думает; молодая позиция — событие к триажу; resume без приказа — перепроверка скоро.
Ни сети, ни ключей, ни data/."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend import ai_pilot, bus, config, mission, trader_risk

PRICE = 100.0
BOOK = {"best_bid": 99.9, "best_ask": 100.1}


class Broker:
    """Мини-биржа: заявки исполняются сразу, стопы ставятся, портфеля нет (dry-снимок)."""
    mode = "real"

    def __init__(self):
        self.placed, self.stops, self.cancelled = [], [], []
        self.mx = None
        self._n = 0

    async def place(self, figi, direction, lots, price=None, tag="", **kw):
        self._n += 1
        oid = f"F-{self._n}"
        self.placed.append({"order_id": oid, "direction": direction, "lots": int(lots), "price": price, "tag": tag})
        return {"ok": True, "order_id": oid}

    async def order_state(self, oid, **kw):
        return {"ok": True, "filled": True, "status": "FILL", "exec_lots": 0}

    async def cancel(self, oid, **kw):
        self.cancelled.append(oid)
        return {"ok": True}

    async def place_stop(self, figi, direction, lots, stop_price, tag="", **kw):
        self.stops.append({"direction": direction, "lots": int(lots), "stop": stop_price})
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
    """ai_v5.money_json: очереди ответов по маршрутам и счётчик вызовов; note_error — в список."""

    def __init__(self):
        self.answers: dict = {}
        self.calls: list = []
        self.errors: list = []

    def queue(self, route, *answers):
        self.answers.setdefault(route, []).extend(answers)

    def count(self, route):
        return sum(1 for r, _ in self.calls if r == route)

    async def money_json(self, system, user, *, route, max_tokens=None, **kw):
        self.calls.append((route, user))
        q = self.answers.get(route) or []
        return q.pop(0) if q else {}

    def note_error(self, text, route=""):
        self.errors.append((route, text))


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """Всё внешнее — фейки; вход без проверки у двери (ритм пилота проверяется отдельно от двери)."""
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
    monkeypatch.setattr(config, "PYTHIA_PARTNERS", False)
    monkeypatch.setattr(config, "PYTHIA_EXCHANGE_STOP", False)
    monkeypatch.setattr(config, "PYTHIA_ENTRY_CHECK", False)
    monkeypatch.setattr(config, "PYTHIA_PROFIT_THINK", True)
    monkeypatch.setattr(config, "PYTHIA_EVENT_TRIAGE", True)
    monkeypatch.setattr(config, "PYTHIA_MONEY_MODEL", "pro")
    monkeypatch.setattr(ai_pilot, "market_clock", None)             # часы выключены: рынок открыт
    fake = FakeMoney()
    monkeypatch.setattr(mission.ai_v5, "money_json", fake.money_json)
    monkeypatch.setattr(mission.ai_v5, "note_error", fake.note_error)
    monkeypatch.setattr(mission.ai_v5, "pro_json", AsyncMock(side_effect=AssertionError("PRO в этих тестах не зовут")))
    return fake


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
    m.task = SimpleNamespace(done=lambda: False)   # «пилот жив» для status/фазы
    return m, p


def ex(do="BUY", entry=None, take=110.0, inv=98.0, why="тест"):
    return {"exec": {"do": do, "entry": entry, "take": take, "invalidation": inv, "why": why}}


def wait_order(m, p, levels=(99.0, 101.0)):
    ex1, err = mission._validate_exec({"do": "WAIT", "wait_for": "пробой 101 на объёме", "why": "перевеса нет",
                                       "levels": list(levels)}, "auto", PRICE)
    assert err is None and ex1["levels"] == list(levels)
    m.exec, m.exec_ts = ex1, time.time()
    assert p.adopt_forecast({"exec": ex1})
    assert p.plan is None and p.state == "ЖДУ_ПЛАН"
    return ex1


async def tick(p, price, n=1):
    for _ in range(n):
        await p.tick(price, dict(BOOK, best_bid=round(price - 0.1, 4), best_ask=round(price + 0.1, 4)))
        await settle_bg(p)


async def settle_bg(p, n=300):
    for _ in range(n):
        if not [t for t in p._background_tasks if not t.done()]:
            return
        await asyncio.sleep(0.005)


async def open_position(p, inv=98.0, take=110.0, price=PRICE):
    assert p.adopt_forecast(ex("BUY", None, take, inv))
    for _ in range(4):
        await tick(p, price)
        if p.position:
            break
    assert p.position, p.last_action
    return p.position


# ── 1. приказ WAIT: уровни под наблюдением, ритм PYTHIA_WAIT_REVIEW_SEC ───────────────────────────────────────
def test_wait_level_cross_pulls_review_within_event_min_gap():
    async def scenario():
        m, p = make_pilot()
        wait_order(m, p)
        p._last_review_ts = time.time() - 30                  # PRO ответил полминуты назад
        await tick(p, 100.0, n=2)                             # первый тик — цена отсчёта и стакан; второй — рынок жив
        assert p.review_ts <= m.exec_ts + float(config.PYTHIA_WAIT_REVIEW_SEC) + 1, "WAIT — не сон на PYTHIA_REVIEW_SEC"
        assert p.review_ts > time.time() + 60, "ритм WAIT не дёргает PRO каждую минуту"
        n_h = len(m.handoffs)
        p._wait_watch(101.3)                                  # цена прошла 101 снизу вверх
        h = m.handoffs[-1]
        assert len(m.handoffs) == n_h + 1 and h["kind"] == "wait_level" and not h["deferred"], h
        assert "WAIT: цена 101.3 прошла уровень 101 из приказа совета — реши по живой картине" in (p._review_reason or "")
        assert p.review_ts <= p._last_review_ts + mission.EVENT_MIN_GAP_SEC + 1
        assert p.review_ts <= time.time() + mission.EVENT_MIN_GAP_SEC
        assert not p._review_pulled, "уровень WAIT — не событие: пейсинг событий не тратится"
        # один уровень — один повод; другой уровень (99 сверху вниз) — свой повод
        p.review_ts = time.time() + 1800
        p._wait_watch(100.5)
        p._wait_watch(101.6)
        assert len(m.handoffs) == n_h + 1, "тот же уровень второй раз не будит"
        p._wait_watch(98.8)
        assert len(m.handoffs) == n_h + 2 and "уровень 99" in m.handoffs[-1]["reason"]
        assert p.plan is None and p.pending is None and not p.broker.placed, "код не входит за ИИ — только будит"

    asyncio.run(scenario())


def test_wait_watch_quiet_without_wait_or_with_plan():
    async def scenario():
        m, p = make_pilot()
        m.exec, m.exec_ts = {"do": "BUY", "levels": [101.0]}, time.time()
        await tick(p, 100.0, n=2)
        p._wait_watch(101.5)
        assert not m.handoffs and p._wait_st is None, "приказ не WAIT — наблюдения нет"
        wait_order(m, p)
        p._wait_watch(100.0)
        assert p.adopt_forecast(ex("BUY", entry=95.0, inv=93.0))   # засада есть — WAIT-наблюдение молчит
        p._wait_watch(101.5)
        assert not [h for h in m.handoffs if h["kind"] == "wait_level"]

    asyncio.run(scenario())


# ── 2. прокол сканера без плана — «вне рынка» ───────────────────────────────────────────────────────────────
def _scan(side, lo, hi, pers):
    return lambda t: {"puncture_first": {"side": side, "p_lo": lo, "p_hi": hi, "persistence": pers, "depth_mean": 0.9,
                                         "ticks": 90}, "punctures": {"bin_step": 0.1}}


def test_puncture_under_wait_wakes_review_as_possible_entry(offline, monkeypatch):
    fake = offline
    monkeypatch.setattr(mission, "PUNCTURE_CHECK_SEC", 0.0)

    async def scenario():
        m, p = make_pilot()
        wait_order(m, p)
        await tick(p, 100.0)
        p.review_ts, p._last_review_ts, p._last_event_review_ts = time.time() + 1800, 0.0, 0.0
        monkeypatch.setattr(mission, "_scan_status_raw", _scan("вверх", 100.6, 101.0, 0.8))
        p._puncture_watch(100.2)
        pu = p.puncture
        assert pu and pu["role"] == "вне рынка" and pu["our_side"] is None and not pu["in_pos"] and pu["pending"], pu
        assert "мы вне рынка без плана — прокол вне рынка — возможный момент входа в сторону прокола" in pu["text"]
        h = m.handoffs[-1]
        assert h["kind"] == "puncture" and not h["deferred"] and "(вне рынка: полоса 100.6–101, без плана)" in h["reason"], h
        assert p.review_ts <= time.time() + 1 and fake.count("event_triage") == 0, "без позиции — сразу PRO, без триажа"
        assert p._situation_for_ai(100.2).startswith("ПРОКОЛ СКАНЕРА: сторона ВВЕРХ")

    asyncio.run(scenario())


def test_puncture_against_play_mode_stays_quiet(monkeypatch):
    monkeypatch.setattr(mission, "PUNCTURE_CHECK_SEC", 0.0)

    async def scenario():
        m, p = make_pilot(play="long")
        wait_order(m, p)
        await tick(p, 100.0)
        monkeypatch.setattr(mission, "_scan_status_raw", _scan("вниз", 99.0, 99.4, 0.9))
        p._puncture_watch(100.0)
        assert p.puncture is None and "режим игры long" in (p._puncture_now or {}).get("state", "")
        assert not m.handoffs

    asyncio.run(scenario())


# ── 3. поводы пилота вне рынка без плана — v5.4.4: к ПЛАНОВОЙ перепроверке (воля владельца «только триггеры и раз в
#       30 мин»; в 5.4.2 они звали PRO через EVENT_MIN_GAP_SEC — петли «перепроверка — отказ — перепроверка») ─────────
def test_stale_order_flat_waits_planned_review():
    async def scenario():
        m, p = make_pilot()
        assert p.adopt_forecast(ex("BUY", entry=95.0, take=110.0, inv=93.0))     # засада ниже рынка
        p.plan["ts"] -= ai_pilot.PLAN_TTL_SEC + 10
        p.review_ts, p._last_review_ts = time.time() + 1800, time.time() - 600
        await tick(p, 100.0)
        assert p.plan is None and p.position is None and "приказ протух" in (p._review_reason or "")
        h = m.handoffs[-1]
        assert h["kind"] == "pilot" and h["deferred"] and h["reason"] == "приказ протух", h
        assert p.review_ts >= time.time() + 1790, p.review_ts - time.time()
        assert "повод к плановой перепроверке" in p.last_action, p.last_action
        p.reanalyze_cb.assert_not_awaited()

    asyncio.run(scenario())


def test_zero_lots_flat_waits_planned_review_with_reason():
    async def scenario():
        m, p = make_pilot()
        p.broker.mx = {"buy": 8, "sell": 0}                  # шорт недоступен
        p.review_ts = time.time() + 1800
        assert p.adopt_forecast(ex("SELL", None, 90.0, 102.0))
        await tick(p, 100.0)
        assert p.plan is None and p.pending is None and not p.broker.placed
        h = m.handoffs[-1]
        assert h["kind"] == "pilot" and h["deferred"] and "биржа не даёт ни лота short" in h["reason"], h
        assert p.review_ts >= time.time() + 1790, "лотов 0 — не рыночный повод: к плановой перепроверке"
        assert "биржа не даёт ни лота short" in (p._review_reason or "")

    asyncio.run(scenario())


def test_after_close_pause_kept_for_close_reasons(monkeypatch):
    monkeypatch.setattr(config, "PYTHIA_AFTER_CLOSE_SEC", 0)
    m, p = make_pilot()
    p.review_ts = time.time() + 1800
    p._fire_reanalyze("после закрытия — огромный анализ с нуля")
    assert p.review_ts >= time.time() + 1790, "0 → после закрытия ждём плановой (воля владельца), как было"


# ── 4. первый совет не собрал приказ — пилот без плана, а не мёртвая миссия ────────────────────────────────────
def _council_offline(monkeypatch, exec_answers, frame=None):
    async def fit(blocks):
        return blocks, []

    async def fake_run(self):
        await asyncio.Event().wait()                     # петля «живёт», пока тест не снимет задачу

    answers = list(exec_answers)
    prompts: list = []

    async def pro_json(system, user, *, route="pro", **kw):
        prompts.append(user)
        a = answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        return a

    monkeypatch.setattr(mission.MissionPilot, "run", fake_run)
    monkeypatch.setattr(mission, "_make_broker", lambda: Broker())
    monkeypatch.setattr(mission.tinkoff, "enabled", lambda: True)
    monkeypatch.setattr(mission, "market_clock", None)
    monkeypatch.setattr(mission, "_scan_start", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(mission, "_restore_pending_panics", lambda: None)
    monkeypatch.setattr(mission, "_state_file_position", lambda: None)
    monkeypatch.setattr(mission.market_ctx, "build", AsyncMock(return_value={"price": 100, "text": "offline"}))
    monkeypatch.setattr(mission, "_account_position_text", AsyncMock(return_value=""))
    monkeypatch.setattr(mission, "_partners_block", AsyncMock(return_value=""))
    monkeypatch.setattr(mission, "_news_for", AsyncMock(return_value=([], "")))
    monkeypatch.setattr(mission, "_watch_text", lambda since: "")
    monkeypatch.setattr(mission.scout, "run", AsyncMock(return_value=("", [])))
    monkeypatch.setattr(mission, "_fit_blocks", fit)
    monkeypatch.setattr(mission, "_shrink_one", AsyncMock(side_effect=lambda text, label: text))
    monkeypatch.setattr(mission, "_stream_stage", AsyncMock(return_value="offline analysis"))
    monkeypatch.setattr(mission.ai_v5, "pro_json", pro_json)
    if frame is not None:
        monkeypatch.setattr(mission, "_mod", lambda name: SimpleNamespace(present_frame=frame) if name == "council" else None)
    return prompts


def test_first_council_failure_starts_idle_pilot_with_planned_review(monkeypatch):
    _council_offline(monkeypatch, [{"do": "BUY"}, {"do": "BUY"}])     # без invalidation — приказ не собирается

    async def scenario():
        r = await mission.start("SBER", "auto")
        assert r["ok"], r
        m = mission._M["SBER"]
        await m.council_task
        try:
            assert "приказ не собрался" in (m.error or "")
            assert m.pilot_alive() and m.phase == "idle" and mission.status("SBER")["phase"] == "idle", m.phase
            p = m.pilot
            assert p.plan is None and p.state == "ЖДУ_ПЛАН" and p.reanalyze_cb is not None
            # v5.4.4: не через 5 мин (REVIEW_RETRY_SEC убран), а на плановой перепроверке — раньше будят триггеры
            assert p.review_ts >= time.time() + float(config.PYTHIA_REVIEW_SEC) - 10, p.review_ts - time.time()
            assert (p._review_reason or "").startswith("совет не собрал приказ: приказ не собрался")
            assert p._review_reason.endswith("реши по живой картине") and "без плана" in m.note
        finally:
            m.task.cancel()
            await asyncio.gather(m.task, return_exceptions=True)

    asyncio.run(scenario())


def test_exec_timeout_gets_second_attempt_and_pilot_before_frame(monkeypatch):
    seen: list = []

    async def frame(kind, payload):
        mm = mission._M["SBER"]
        seen.append(bool(mm.pilot and mm.pilot.plan))    # рамка — ПОСЛЕ того, как пилот получил приказ
        return {"headline": "рамка"}

    prompts = _council_offline(monkeypatch, [asyncio.TimeoutError(),
                                             {"do": "BUY", "entry": None, "take": 104, "invalidation": 98}], frame=frame)

    async def scenario():
        await mission.start("SBER", "auto")
        m = mission._M["SBER"]
        await m.council_task
        try:
            assert m.error is None and m.exec["do"] == "BUY" and len(prompts) == 2, (m.error, len(prompts))
            assert "ПРИКАЗ ОТКЛОНЁН КОДОМ" not in prompts[1], "молчание — не ошибка в числах"
            for _ in range(200):
                if m.frame:
                    break
                await asyncio.sleep(0.005)
            assert m.frame == {"headline": "рамка"} and seen == [True], seen
        finally:
            m.task.cancel()
            await asyncio.gather(m.task, return_exceptions=True)

    asyncio.run(scenario())


# ── 5. переворот при молодой позиции — выход без переворота ───────────────────────────────────────────────────
def test_flip_quiet_closes_young_position_without_reversal():
    async def scenario():
        m, p = make_pilot()
        pos = await open_position(p)
        assert p.adopt_forecast(ex("SELL", None, 90.0, 102.0)) is False
        assert p.position is pos and p.plan is None and p._close_pending, p.last_action
        assert "переворот отклонён" in p.last_action and "закрываю без переворота" in p.last_action
        # v5.4.4: вход в другую сторону решит PRO по поводу «после закрытия» (PYTHIA_AFTER_CLOSE_SEC), не через 5 мин
        assert "закрыта без переворота" in (p._review_reason or "") and p.review_ts > time.time() + 300
        for _ in range(4):
            await tick(p, 100.0)
            if p.position is None:
                break
        assert p.position is None and [x["tag"] for x in p.broker.placed] == ["aip-entry", "aip-close"], p.broker.placed
        await tick(p, 100.0, n=2)
        assert p.position is None and p.pending is None and len(p.broker.placed) == 2, "переворота нет"

    asyncio.run(scenario())


def test_flip_quiet_ignores_wait_and_same_side():
    m, p = make_pilot()
    p.position = {"side": "long", "entry": 100.0, "lots": 5, "take": 104.0, "invalidation": 97.0,
                  "opened_ts": time.time(), "stop_id": None, "floating": 0.0}
    assert p.adopt_forecast(ex("BUY", None, 105.0, 98.0)) is True and not p._close_pending
    assert p.position["take"] == 105.0, "та же сторона — обновил уровни, не закрыл"


# ── 6. НОВЫЙ_АНАЛИЗ в окне совета — v5.4.4: не ждёт окна, чтобы запустить совет сам (тик §3б), а уходит поводом к
#       плановой перепроверке; окно — от КОНЦА прошлого совета (воля владельца 30.09.2026) ───────────────────────────
def test_new_analysis_in_council_gap_goes_to_planned_review():
    async def scenario():
        m, p = make_pilot()
        gap = float(config.PYTHIA_COUNCIL_GAP_SEC)
        m.council_ts, m.council_end_ts = time.time() - 2400, time.time() - 600   # начат 40 мин назад, кончился 10 мин назад
        p._last_reanalyze_ts = time.time() - 4000
        p.review_ts = time.time() + 1800
        why = "перепроверка потребовала свежий разбор: картина сломалась"
        p._fire_reanalyze(why, kind="council")
        assert not p._reanalyzing and p._reanalyze_pending is None, "совет сам не запустится"
        assert "окно откроется в" in p._council_blocked and "от конца совета" in p._council_blocked, p._council_blocked
        assert p._council_deferred == why and why in (p._review_reason or "")
        assert p.review_ts >= time.time() + 1790, "повод к плановой, перепроверку не тянет"
        assert m.handoffs[-1]["kind"] == "council" and m.handoffs[-1]["deferred"]
        n_h = len(m.handoffs)
        await tick(p, 100.0, n=2)
        m.council_end_ts = time.time() - gap - 1             # окно открылось — тик совет всё равно не зовёт
        await tick(p, 100.0)
        p.reanalyze_cb.assert_not_awaited()
        assert len(m.handoffs) == n_h and p._reanalyze_pending is None
        # PRO на плановой снова сказал НОВЫЙ_АНАЛИЗ — окно открыто: совет
        p._fire_reanalyze(why, kind="council")
        assert p._reanalyzing
        await settle_bg(p)
        p.reanalyze_cb.assert_awaited_once()

    asyncio.run(scenario())


def test_council_window_counts_from_end_not_start():
    """Совет шёл 40 мин: начат 45 мин назад, кончился 5 мин назад — окно 30 мин от КОНЦА ещё закрыто (в 5.4.3 окно
    считалось от начала и было бы открыто)."""
    async def scenario():
        m, p = make_pilot()
        m.council_ts, m.council_end_ts = time.time() - 2700, time.time() - 300
        assert 1400 <= mission._council_gap_left(m) <= 1500
        p._fire_reanalyze("мягкий стоп: PRO решил ждать и передать задачу Совету — вынос", force=True, kind="stop")
        assert not p._reanalyzing and m.handoffs[-1]["kind"] == "stop" and m.handoffs[-1]["deferred"], m.handoffs[-1]
        assert "мягкий стоп" in (p._review_reason or "") and p.review_ts > time.time() + 1700
        m.council_end_ts = time.time() - 1900                # окно открыто — просьба узла зовёт совет
        p._fire_reanalyze("мягкий стоп: PRO решил ждать и передать задачу Совету — вынос", force=True, kind="stop")
        assert p._reanalyzing and p._council_kind == "stop" and not m.handoffs[-1]["deferred"]
        await settle_bg(p)
        p.reanalyze_cb.assert_awaited_once()

    asyncio.run(scenario())


def test_deferred_parent_council_goes_to_planned_review():
    """Страховка: отложенный пейсингом родителя совет (_reanalyze_pending) тик §3б сам не запускает — повод к плановой."""
    async def scenario():
        m, p = make_pilot()
        p._reanalyze_pending = "перепроверка потребовала свежий разбор: x"
        p._last_reanalyze_ts = time.time() - 4000
        await tick(p, 100.0)
        p.reanalyze_cb.assert_not_awaited()
        assert p._reanalyze_pending is None and "свежий разбор: x" in (p._review_reason or "")

    asyncio.run(scenario())


def test_routing_by_kind_not_by_words_in_pro_why():
    async def scenario():
        m, p = make_pilot()
        m.council_ts = time.time() - 4000
        p._last_reanalyze_ts = time.time() - 4000
        # в объяснении PRO — слова поводов пилота; это всё равно просьба о совете
        p._fire_reanalyze("перепроверка потребовала свежий разбор: после закрытия вход невозможен, приказ протух")
        assert p._reanalyzing and m.handoffs[-1]["kind"] == "council"
        await settle_bg(p)
        p.reanalyze_cb.assert_awaited_once()
        p._reanalyzing = False
        p._fire_reanalyze("серия отказов биржи")             # повод кода (родитель) — дежурному PRO
        assert m.handoffs[-1]["kind"] == "pilot" and p.reanalyze_cb.await_count == 1

    asyncio.run(scenario())


# ── 7. совет по поводу дольше PYTHIA_COUNCIL_MAX_SEC — прерван, пилот разморожен ──────────────────────────────
def test_council_timeout_unfreezes_pilot_and_keeps_position(offline, monkeypatch):
    fake = offline

    async def slow_council(m_, reason, first):
        await asyncio.Event().wait()

    async def scenario():
        m, p = make_pilot()
        pos = await open_position(p)
        monkeypatch.setattr(config, "PYTHIA_COUNCIL_MAX_SEC", 0.2)
        monkeypatch.setattr(mission, "_council", slow_council)
        monkeypatch.setattr(mission, "_state_file_position", lambda: None)
        monkeypatch.setattr(mission, "_restore_pending_panics", lambda: None)
        mission._bind_pilot(m, p)                            # настоящий колбэк совета с пределом
        p.review_ts, p._last_reanalyze_ts = time.time() + 1800, time.time() - 4000
        p._fire_reanalyze("перепроверка потребовала свежий разбор: картина сломалась", kind="council")
        assert p._reanalyzing
        for _ in range(200):
            if not p._reanalyzing:
                break
            await asyncio.sleep(0.01)
        await settle_bg(p)
        assert not p._reanalyzing and p.position is pos, "разморожен, позиция на месте"
        assert m.council_task.done() and not m.council_running()
        # v5.4.4: обрыв совета — не рыночный повод: плановая перепроверка (её срок не тянется к «через 3 мин»)
        assert p.review_ts > time.time() + mission.EVENT_MIN_GAP_SEC + 60, p.review_ts - time.time()
        assert "совет не уложился" in (p._review_reason or "") and "прерван" in (m.error or "")
        assert any(r == "mission_council" for r, _ in fake.errors), fake.errors
        # ревью 5.4.2: окно совета — от обрыва: НОВЫЙ_АНАЛИЗ сразу после него ждёт окна, а не крутит «совет — обрыв»
        assert time.time() - m.council_ts < 5, m.council_ts
        assert "Последний полный совет прерван 0 мин назад (не уложился в срок)" in p._situation_text(100.0)
        monkeypatch.setattr(config, "PYTHIA_COUNCIL_GAP_SEC", 1800)
        p._fire_reanalyze("перепроверка потребовала свежий разбор: снова", kind="council")
        assert not p._reanalyzing and p._council_deferred and p._reanalyze_pending is None, p.last_action
        assert "свежий разбор: снова" in (p._review_reason or "")

    asyncio.run(scenario())


# ── 8. мысль о прибыли, пока дежурный PRO думает, — не теряется ───────────────────────────────────────────────
def test_profit_trigger_waits_for_review_then_runs(offline):
    fake = offline

    async def scenario():
        m, p = make_pilot()
        pos = await open_position(p, inv=98.0, take=110.0)
        pos["profit_next"] = 0.0

        async def slow_review(price):
            await asyncio.sleep(0.05)
        p._review = slow_review
        p.prices.append(107.0)
        t = asyncio.create_task(p._review_bg(107.0))
        await asyncio.sleep(0.01)
        assert p._review_busy
        p._profit_watch(107.0, pos)                          # 75 % пути к тейку — повод есть, PRO занят
        assert pos.get("profit_pending", "").startswith("пройдено") and fake.count("mission_profit") == 0
        await t
        await settle_bg(p)
        assert fake.count("mission_profit") == 1 and "profit_pending" not in pos, "мысль — сразу после ответа PRO"
        assert p._review_started_ts > 0

    asyncio.run(scenario())


# ── 9. молодая позиция: событие — к триажу (решает ИИ), а не тишина кода ──────────────────────────────────────
def test_young_position_event_goes_to_triage(offline):
    fake = offline

    async def scenario():
        m, p = make_pilot()
        await open_position(p)                               # только что вошли
        p.review_ts, p._last_review_ts, p._last_event_review_ts = time.time() + 1800, 0.0, 0.0
        fake.queue("event_triage", {"urgency": "СЕЙЧАС", "action": None, "why": "новость ломает идею"})
        t = p._ask_review("серьёзная новость: тест", kind="news")
        assert t is not None, "молодая позиция — тоже через триаж"
        await t
        assert fake.count("event_triage") == 1 and p.triages[-1]["urgency"] == "СЕЙЧАС"
        assert not m.handoffs[-1]["deferred"] and p.review_ts <= time.time() + 1

    asyncio.run(scenario())


# ── 10. resume без приказа — v5.4.4: плановая перепроверка от последнего решения PRO (просрочена — сразу) ─────────
def test_resume_without_adopted_order_reviews_on_plan(monkeypatch):
    async def fake_run(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(mission.MissionPilot, "run", fake_run)
    monkeypatch.setattr(mission, "_make_broker", lambda: Broker())
    monkeypatch.setattr(mission.tinkoff, "enabled", lambda: True)
    monkeypatch.setattr(mission, "_scan_start", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(mission, "_state_file_position", lambda: None)
    monkeypatch.setattr(mission, "_restore_pending_panics", lambda: None)

    async def scenario():
        m = mission.Mission("TEST", "Тест", "futures", "auto", 100000.0)
        mission._M["TEST"] = m
        m.exec = {"do": "WAIT", "entry": None, "entry_kind": "сейчас", "take": None, "invalidation": None,
                  "why": "нет перевеса", "wait_for": "уровень 101", "levels": [101.0]}
        m.exec_ts = time.time() - 7200                       # последний приказ совета — 2 ч назад: плановая просрочена
        r = await mission.resume("TEST")
        try:
            p = m.pilot
            assert r["ok"] and m.phase == "idle" and p.plan is None, r
            assert p.review_ts <= time.time() + max(ai_pilot.OPEN_REVIEW_GRACE_SEC, 120.0) + 1
            assert "пилот поднят заново: приказа нет — реши по живой картине" in (p._review_reason or "")
        finally:
            m.task.cancel()
            await asyncio.gather(m.task, return_exceptions=True)
        # решение PRO было 10 мин назад — плановая через 20 мин, а не «скоро»
        m.pilot, m.task = None, None
        m.reviews = [{"ts": time.time() - 600, "choice": "ЖДЁМ", "why": "т", "in_pos": False, "price": 100.0}]
        r = await mission.resume("TEST")
        try:
            p = m.pilot
            assert r["ok"] and 1100 <= p.review_ts - time.time() <= 1210, p.review_ts - time.time()
            assert abs(p._last_review_ts - m.reviews[-1]["ts"]) < 1, "новости — с прошлого решения, не со старта"
        finally:
            m.task.cancel()
            await asyncio.gather(m.task, return_exceptions=True)

    asyncio.run(scenario())


# ══ ревью 5.4.2 (часть 2) ══════════════════════════════════════════════════════════════════════════════════════════
# ── рывок в нашу сторону, пока дежурный PRO думает, — мысль о прибыли после ответа без порога хода ─────────────────
def test_shock_rally_during_review_runs_profit_thought_after_answer(offline, monkeypatch):
    fake = offline
    monkeypatch.setattr(config, "PYTHIA_SHOCK_PCT", 1.0)
    monkeypatch.setattr(config, "PYTHIA_SHOCK_WIN_SEC", 600)

    async def scenario():
        m, p = make_pilot()
        pos = await open_position(p, inv=98.0, take=110.0)
        pos["profit_next"] = 0.0
        entry = pos["entry"]
        now = time.time()
        p._px_hist[:] = [(now - 400, entry), (now - 300, entry), (now - 200, entry)]
        p._last_shock_ts = 0.0

        async def slow_review(price):
            await asyncio.sleep(0.05)
        p._review = slow_review
        t = asyncio.create_task(p._review_bg(entry))
        await asyncio.sleep(0.01)
        assert p._review_busy
        cur = round(entry * 1.013, 2)
        p.prices.append(cur)
        p._shock_watch(cur)                                  # +1.3 % за окно — рывок в плюс, PRO занят
        assert pos.get("profit_pending_kind") == "shock" and fake.count("mission_profit") == 0, pos
        assert p._profit_trigger(cur, pos) is None, "до порога хода к тейку далеко — повод только от рывка"
        await t
        await settle_bg(p)
        assert fake.count("mission_profit") == 1, "рывок израсходован — мысль о прибыли сразу после ответа PRO"
        assert "profit_pending" not in pos and "profit_pending_kind" not in pos
        assert "рывок в нашу сторону" in str(pos.get("profit_reason") or p.profits[-1].get("reason") or ""), \
            (pos.get("profit_reason"), p.profits[-1:])

    asyncio.run(scenario())


# ── новый приказ не несёт рамку прошлого совета ────────────────────────────────────────────────────────────────
def test_new_order_drops_previous_frame(monkeypatch):
    seen: list = []

    async def frame(kind, payload):
        seen.append(mission._M["SBER"].frame)
        return {"headline": f"рамка {len(seen)}"}

    _council_offline(monkeypatch, [{"do": "BUY", "entry": None, "take": 104, "invalidation": 98},
                                   {"do": "WAIT", "wait_for": "закрепление выше 101", "why": "мутно"}], frame=frame)

    async def scenario():
        await mission.start("SBER", "auto")
        m = mission._M["SBER"]
        await m.council_task
        try:
            for _ in range(200):
                if m.frame:
                    break
                await asyncio.sleep(0.005)
            assert m.frame == {"headline": "рамка 1"}
            ex2 = await mission._council(m, "повтор", False)
            assert ex2 and ex2["do"] == "WAIT"
            assert m.frame in (None, {"headline": "рамка 2"}), m.frame
            for _ in range(200):
                if len(seen) >= 2:
                    break
                await asyncio.sleep(0.005)
            assert seen[1] is None, "рамка прошлого совета снята, пока новая не пришла"
        finally:
            m.task.cancel()
            await asyncio.gather(m.task, return_exceptions=True)

    asyncio.run(scenario())


# ── проверка ритма 29.09.2026: база 30 мин, триггеры будят раньше — и не теряются ─────────────────────────────
def test_wait_base_cadence_is_thirty_minutes_by_default():
    assert float(config.PYTHIA_WAIT_REVIEW_SEC) == 1800.0 == ai_pilot.wait_review_sec() == float(ai_pilot.REVIEW_SEC), \
        "воля владельца: в базе PRO смотрит рынок раз в 30 мин, раньше — только по триггерам"

    async def scenario():
        m, p = make_pilot()
        wait_order(m, p)
        assert 1790 <= p.review_ts - time.time() <= 1801, p.review_ts - time.time()
        await tick(p, 100.0, n=2)
        assert 1790 <= p.review_ts - time.time() <= 1801, "без триггеров ритм WAIT не короче 30 мин"

    asyncio.run(scenario())


def test_event_during_council_not_lost_after_council_wait():
    """Серьёзная новость пришла, пока шёл совет (он её не видел); совет ответил WAIT — повод не пропадает:
    дежурный PRO вернётся к нему через EVENT_MIN_GAP_SEC, а не через 30 мин."""
    async def scenario():
        m, p = make_pilot()
        wait_order(m, p)
        p._last_review_ts = time.time() - 1200
        p._reanalyzing = True                                   # идёт совет
        p._ask_review_now("серьёзная новость: ЦБ поднял ставку", kind="news")
        assert p._review_pulled and p._review_reason and p.review_ts <= time.time() + 1
        ex1, err = mission._validate_exec({"do": "WAIT", "wait_for": "реакция на ставку", "why": "ждём"}, "auto", PRICE)
        assert err is None
        m.exec, m.exec_ts = ex1, time.time()
        p._reanalyzing = False
        assert p.adopt_forecast({"exec": ex1})                  # совет кончился WAIT
        assert p._review_reason and "ЦБ поднял ставку" in p._review_reason, "повод события, которого совет не видел, жив"
        assert p.review_ts <= time.time() + mission.EVENT_MIN_GAP_SEC + 1, p.review_ts - time.time()
        # без события совет WAIT — обычный ритм, повода нет
        m2, p2 = make_pilot()
        wait_order(m2, p2)
        assert not p2._review_reason and p2.review_ts > time.time() + 1700

    asyncio.run(scenario())


def test_market_open_grace_kept_under_wait():
    """Долго закрытый рынок открылся при приказе WAIT: ритм WAIT не отменяет запас OPEN_REVIEW_GRACE_SEC стакану."""
    async def scenario():
        m, p = make_pilot()
        wait_order(m, p)
        await tick(p, 100.0)                                    # цена отсчёта WAIT и живой стакан
        old = time.time() - 4000                                # приказ, старт и ответ PRO — давно: ритм WAIT «просрочен»
        m.exec_ts, p._last_review_ts, p._review_started_ts = old, old, old
        if hasattr(p, "started_ts"):
            p.started_ts = old
        p._opened_ts = time.time()                              # рынок только что открылся
        p.review_ts = time.time() + ai_pilot.OPEN_REVIEW_GRACE_SEC
        p._wait_watch(100.0)                                    # ритм WAIT смотрит до плановой перепроверки
        assert p.review_ts >= p._opened_ts + ai_pilot.OPEN_REVIEW_GRACE_SEC - 1, p.review_ts - time.time()

    asyncio.run(scenario())


def test_two_triggers_same_tick_second_not_marked_deferred():
    async def scenario():
        m, p = make_pilot()
        wait_order(m, p)
        p._last_review_ts = time.time() - 1200
        p._ask_review_now("резкий ход +1.2%", kind="shock")
        p._ask_review_now("WAIT: цена прошла уровень 101", kind="wait_level")
        assert not m.handoffs[-1]["deferred"], m.handoffs[-1]
        assert "дежурный PRO решит в" in (p.last_action or ""), p.last_action   # v5.4.4: время решения — абсолютное

    asyncio.run(scenario())
