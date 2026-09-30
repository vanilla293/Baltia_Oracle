# -*- coding: utf-8 -*-
"""ПИФИЯ v5.4.4 «ритм PRO» (воля владельца 30.09.2026: «только триггеры и раз в 30 мин»): когда пилот миссии зовёт
дежурного PRO и совет — на фейках, без сети, ключей и data/.

PRO зовётся только: (1) плановая перепроверка раз в PYTHIA_REVIEW_SEC (при WAIT — PYTHIA_WAIT_REVIEW_SEC); (2) рыночный
триггер — резкий ход, серьёзная новость (через триаж), проход уровня WAIT или будильника ЖДЁМ (вместе не больше
LEVEL_MAX_PER_HOUR в час), прокол сканера, открытие рынка, закрытие позиции (через PYTHIA_AFTER_CLOSE_SEC), «идея мертва
до входа», связь с брокером восстановлена; (3) совет — не чаще PYTHIA_COUNCIL_GAP_SEC от КОНЦА прошлого совета. Всё
прочее — молчание / таймаут / неразобранный ответ, ВНЕ_РЕЖИМА, приказ протух, лотов 0, ОТМЕНИТЬ у двери, просьба совета
в окне — повод к СЛЕДУЮЩЕЙ ПЛАНОВОЙ перепроверке (быстрого повтора REVIEW_RETRY_SEC больше нет). Связи с брокером нет —
PRO, триаж и совет не зовутся вовсе. Плюс видимость: метка и исход решения в статусе, живое состояние прокола, стадии
шины закрываются (review — в finally, news — «error» при отмене, pilot — progress + done), честные заметки killswitch и
непринятого приказа."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend import ai_pilot, config, mission, newsflow, trader_risk

PRICE = 100.0
BOOK = {"best_bid": 99.9, "best_ask": 100.1}


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


class FakeAI:
    """ai_v5.money_json / pro_json: очереди ответов по маршрутам, «молчание» (TimeoutError), счётчик вызовов."""
    SILENT = "SILENT"

    def __init__(self):
        self.answers: dict = {}
        self.calls: list = []
        self.errors: list = []

    def queue(self, route, *answers):
        self.answers.setdefault(route, []).extend(answers)

    def count(self, route):
        return sum(1 for r, _ in self.calls if r == route)

    async def _answer(self, route, user):
        self.calls.append((route, user))
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


@pytest.fixture(autouse=True)
def fake(monkeypatch, tmp_path):
    """Всё внешнее — фейки: шина и толмач пишутся в списки, ИИ — очереди, биржа — мини-брокер; дверь выключена (ритм
    PRO проверяется отдельно от двери агента X2)."""
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
    for k, v in (("PYTHIA_PARTNERS", False), ("PYTHIA_EXCHANGE_STOP", False), ("PYTHIA_ENTRY_CHECK", False),
                 ("PYTHIA_PROFIT_THINK", False), ("PYTHIA_MONEY_MODEL", "pro"), ("PYTHIA_EVENT_TRIAGE", True),
                 ("PYTHIA_REVIEW_SEC", 1800), ("PYTHIA_WAIT_REVIEW_SEC", 1800), ("PYTHIA_COUNCIL_GAP_SEC", 1800),
                 ("PYTHIA_AFTER_CLOSE_SEC", 900), ("PYTHIA_EVENT_COOL_SEC", 900), ("PYTHIA_QUIET_SEC", 900)):
        monkeypatch.setattr(config, k, v, raising=False)
    monkeypatch.setattr(ai_pilot, "market_clock", None)             # часы выключены: рынок открыт
    f = FakeAI()
    monkeypatch.setattr(mission.ai_v5, "money_json", f.money_json)
    monkeypatch.setattr(mission.ai_v5, "pro_json", f.pro_json)
    monkeypatch.setattr(mission.ai_v5, "note_error", f.note_error)
    no_net = AsyncMock(side_effect=AssertionError("в этих тестах ИИ по сети не зовут"))
    for name in ("pro_stream", "pro_text", "flash_json", "flash_text"):
        monkeypatch.setattr(mission.ai_v5, name, no_net)
    f.tolmach = []
    monkeypatch.setattr(mission, "_tolmach", lambda m, kind, title, detail="", refs=None:
                        f.tolmach.append({"kind": kind, "title": title, "detail": detail}))
    return f


def make_pilot(play="auto", broker=None):
    m = mission.Mission("TEST", "Тест", "futures", play, 100000.0)
    m.run_id = "run-TEST"
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
    m.task = SimpleNamespace(done=lambda: False)   # «пилот жив» для status/фазы/council_again
    return m, p


def ex(do="BUY", entry=None, take=110.0, inv=98.0, why="тест"):
    return {"exec": {"do": do, "entry": entry, "take": take, "invalidation": inv, "why": why}}


async def tick(p, price, n=1):
    for _ in range(n):
        await p.tick(price, dict(BOOK, best_bid=round(price - 0.1, 4), best_ask=round(price + 0.1, 4)))
        await settle_bg(p)


async def settle_bg(p, n=300):
    for _ in range(n):
        if not [t for t in p._background_tasks if not t.done()]:
            break
        await asyncio.sleep(0.005)
    for _ in range(5):                                  # _bg(bus.stage(...)) — фоновые события шины
        await asyncio.sleep(0)


async def open_position(p, inv=98.0, take=110.0, price=PRICE):
    assert p.adopt_forecast(ex("BUY", None, take, inv))
    for _ in range(4):
        await tick(p, price)
        if p.position:
            break
    assert p.position, p.last_action
    return p.position


def wait_order(m, p, levels=()):
    ex1, err = mission._validate_exec({"do": "WAIT", "wait_for": "картина без перевеса", "why": "перевеса нет",
                                       "levels": list(levels)}, "auto", PRICE)
    assert err is None
    m.exec, m.exec_ts = ex1, time.time()
    assert p.adopt_forecast({"exec": ex1}) and p.plan is None
    return ex1


def stages(name=None):
    """События шины (bus.stage — AsyncMock): [(stage, status, detail, data)]."""
    out = []
    for c in mission.bus.stage.call_args_list:
        a, kw = c.args, c.kwargs
        if len(a) >= 4 and (name is None or a[2] == name):
            out.append((a[2], a[3], kw.get("detail"), kw.get("data")))
    return out


# ── 1. быстрых повторов нет: молчание, неразобранный ответ, ВНЕ_РЕЖИМА — к плановой перепроверке ──────────────────
def test_fast_retry_constant_is_gone():
    assert not hasattr(mission, "REVIEW_RETRY_SEC"), "быстрый повтор перепроверки через 5 мин убран (5.4.4)"
    assert mission.WAKE_MIN_PCT == 0.3 and mission.WAKE_MIN_TICKS == 3 and mission.LEVEL_MAX_PER_HOUR == 2
    assert set(mission.TRIGGER_KINDS) >= {"shock", "news", "puncture", "wait_level", "open", "dead", "event"}
    assert not set(mission.TRIGGER_KINDS) & set(mission.QUIET_KINDS)


def test_silence_and_unparsed_answer_wait_for_planned_review(fake):
    async def scenario():
        m, p = make_pilot()
        p.prices.append(PRICE)
        # (а) плановая перепроверка: тик уже поставил следующий шаг (now + 30 мин) — молчание его не приближает
        p.review_ts = time.time() + 1800
        p._review_reason = "резкий ход: тест"
        fake.queue("mission_review", fake.SILENT)
        await p._review_bg(PRICE)
        await settle_bg(p)
        assert not m.reviews and p.review_ts >= time.time() + 1790, p.review_ts - time.time()
        assert p._review_reason == "резкий ход: тест", "повод не потерян"
        assert "дежурный PRO промолчал" in p.last_action and "плановой перепроверки" in p.last_action, p.last_action
        assert p._review_unparsed is None, "молчание — не «ответ не разобран»"
        assert any(s == ("review", "error") for s in [x[:2] for x in stages("review")]), stages("review")
        assert fake.tolmach[-1]["title"] == "PRO промолчал на перепроверке"
        # (б) перепроверку позвали не тиком (срок в прошлом): ставится плановый шаг, а не петля повторов
        p.review_ts = 0.0
        fake.queue("mission_review", {"choice": "может быть", "why": "?"})
        await p._review(PRICE)
        assert not m.reviews and p.review_ts >= time.time() + 1790, p.review_ts - time.time()
        up = p.status()["review_unparsed"]
        assert up and up["raw"] == "может быть" and up["hint"], up
        sit = p._situation_text(PRICE)
        assert "ПРОШЛЫЙ ОТВЕТ «может быть» НЕ РАЗОБРАН" in sit, sit
        assert fake.tolmach[-1]["title"] == "Ответ PRO на перепроверке не разобран"
        # (в) разобранный ответ снимает строку «не разобран»
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "перевеса нет"})
        await p._review(PRICE)
        assert m.reviews[-1]["choice"] == "ЖДЁМ" and p._review_unparsed is None
        assert "НЕ РАЗОБРАН" not in p._situation_text(PRICE)
        assert p.last_action.startswith("перепроверка: ЖДЁМ — вне рынка до следующего взгляда"), p.last_action
        assert fake.count("mission_review") == 3

    asyncio.run(scenario())


def test_out_of_mode_answer_waits_planned_review(fake):
    async def scenario():
        m, p = make_pilot(play="long")
        p.prices.append(PRICE)
        p.review_ts = time.time() + 1800
        fake.queue("mission_review", {"choice": "ПРОДАТЬ", "why": "вниз", "invalidation": 102.0})
        await p._review(PRICE)
        r = m.reviews[-1]
        assert r["choice"] == "ВНЕ_РЕЖИМА" and r["label"] == "ПРОДАТЬ — против режима игры", r
        assert p.review_ts >= time.time() + 1790 and "прошлый ответ ПРОДАТЬ запрещён режимом long" in p._review_reason
        assert "повод к плановой перепроверке" in p.status()["review_outcome"]["outcome"]

    asyncio.run(scenario())


# ── 2. поводы пилота: тихие — к плановой, рыночные (идея мертва, закрытие) — будят ────────────────────────────────
def test_quiet_pilot_reasons_do_not_pull_review(fake):
    async def scenario():
        m, p = make_pilot()
        p.prices.append(PRICE)
        p._last_review_ts = time.time() - 3000
        planned = time.time() + 1500
        for why in ("приказ протух (95 мин, предел 90)", "вход невозможен: биржа даёт 0 лотов",
                    "серия отказов биржи: 30042", "вход отменён PRO у двери: идея отыграна"):
            p.review_ts = planned
            n_h = len(m.handoffs)
            if why.startswith("вход отменён"):
                pulled = p._ask_review_now(why, kind="pilot")      # так зовёт дверь (X2) при ОТМЕНИТЬ без совета
                assert pulled is False
            else:
                p._fire_reanalyze(why)                             # так зовёт родитель (строка кода — повод пилота)
            assert p.review_ts == planned, (why, p.review_ts - time.time())
            assert not p._reanalyzing, "повод пилота — не совет"
            h = m.handoffs[-1]
            assert len(m.handoffs) == n_h + 1 and h["kind"] == "pilot" and h["deferred"], (why, h)
            assert why in p._review_reason
            assert "повод к плановой перепроверке" in p.last_action and "(в " in p.last_action, p.last_action
        assert [t["title"] for t in fake.tolmach] == ["Приказ протух — план снят"], fake.tolmach
        assert fake.count("mission_review") == 0

    asyncio.run(scenario())


def test_dead_idea_and_close_are_triggers(fake):
    async def scenario():
        m, p = make_pilot()
        p.prices.append(PRICE)
        p._last_review_ts = time.time() - 3000
        p.review_ts = time.time() + 1500
        p._fire_reanalyze("идея мертва до входа")
        assert p.review_ts <= time.time() + 1, "идея мертва до входа — рыночный триггер: PRO сейчас"
        assert m.handoffs[-1]["kind"] == "pilot" and not m.handoffs[-1]["deferred"]
        assert fake.tolmach[-1]["title"] == "Идея мертва до входа — план снят"
        assert "дежурный PRO решит в" in p.last_action, p.last_action
        # недавний ответ PRO — не раньше EVENT_MIN_GAP_SEC после него
        p._review_reason, p.review_ts, p._last_review_ts = None, time.time() + 1500, time.time() - 30
        p._fire_reanalyze("идея мертва до входа")
        assert abs(p.review_ts - (p._last_review_ts + mission.EVENT_MIN_GAP_SEC)) < 2
        # закрытие позиции — через PYTHIA_AFTER_CLOSE_SEC
        p._review_reason, p.review_ts, p._last_review_ts = None, time.time() + 1500, time.time() - 3000
        p._fire_reanalyze("после закрытия — огромный анализ с нуля")
        assert abs(p.review_ts - (time.time() + 900)) < 2, p.review_ts - time.time()
        assert not p._reanalyzing

    asyncio.run(scenario())


# ── 3. уровни: будильник не у самой цены, уровни WAIT и будильник — не больше 2 раз в час ───────────────────────────
def test_wake_level_min_distance(fake):
    async def scenario():
        m, p = make_pilot()
        p.prices.append(PRICE)
        wait_order(m, p)
        assert abs(p._wake_min_dist(100.0) - 0.3) < 1e-9 and abs(p._wake_min_dist(5.0) - 0.03) < 1e-9, "max(0.3 %, 3 шага)"
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "у 100.2 решится", "entry": 100.2},
                   {"choice": "ЖДЁМ", "why": "пробой 100.5", "entry": 100.5})
        await p._review(PRICE)
        r = m.reviews[-1]
        assert p._wake is None and "ближе 0.3 %" in r["wake_refused"] and "не поставлен" in r["wake_refused"], r
        assert p.last_review["wake_refused"] == r["wake_refused"] and "код не принял" in p._situation_text(PRICE)
        assert p.last_action.startswith("перепроверка: ЖДЁМ — вне рынка до следующего взгляда; уровень 100.2"), p.last_action
        await p._review(PRICE)
        assert p._wake and p._wake["level"] == 100.5 and p._wake["dir"] == "up", p._wake
        assert m.reviews[-1]["label"] == "ЖДЁМ — будильник 100.5"

    asyncio.run(scenario())


def test_levels_wake_pro_at_most_twice_an_hour(fake):
    async def scenario():
        m, p = make_pilot()
        wait_order(m, p, levels=(101.0, 102.0, 103.0))
        await tick(p, PRICE, n=2)                          # цена отсчёта и живой стакан
        p._last_review_ts = time.time() - 3000
        seen = []
        for px in (101.2, 102.2, 103.2):
            p.review_ts = time.time() + 1500               # как после ответа PRO
            p._wait_watch(px)
            seen.append((m.handoffs[-1]["kind"], m.handoffs[-1]["deferred"], p.review_ts <= time.time() + 1))
        assert seen[:2] == [("wait_level", False, True)] * 2, seen
        assert seen[2] == ("pilot", True, False), "третий уровень за час — повод к плановой"
        assert "уровни будили PRO уже 2 раза за час" in m.handoffs[-1]["reason"]
        assert p.status()["level_wakes_left"] == 0
        # будильник ЖДЁМ делит ту же квоту
        p._level_wakes = [time.time() - 3700, time.time() - 3650]      # старше часа — не в счёт
        assert p._level_quota_left() == 2
        assert p._level_wake("будильник: цена прошла 104") is True

    asyncio.run(scenario())


# ── 4. окно совета — от КОНЦА прошлого совета, для НОВЫЙ_АНАЛИЗ и для просьб узлов ─────────────────────────────────
def test_council_gap_counts_from_end(fake):
    async def scenario():
        m, p = make_pilot()
        now = time.time()
        m.council_ts, m.council_end_ts = now - 3000, now - 600      # начат 50 мин назад, кончился 10 мин назад
        assert 1190 <= mission._council_gap_left(m, now) <= 1200
        m.council_end_ts = 0.0                                       # конца нет (старая запись) — от начала
        assert mission._council_gap_left(m, now) == 0
        m.council_ts, m.council_end_ts = now - 3000, now - 600
        p.review_ts = now + 1500
        # НОВЫЙ_АНАЛИЗ перепроверки в окне — не совет, а повод к плановой
        p._fire_reanalyze("перепроверка потребовала свежий разбор: картина сломалась", kind="council")
        assert not p._reanalyzing and p._council_deferred and "окно откроется в" in p._council_blocked
        h = m.handoffs[-1]
        assert h["kind"] == "council" and h["deferred"] and p.review_ts == now + 1500, h
        assert p.status()["council_window_s"] > 1100 and p.status()["council_deferred"] is True
        # просьба узла (трос ЖДАТЬ) — тоже по окну; итог узла остаётся первым в last_action
        p.last_action = "PRO у троса: ЖДАТЬ — прокол"
        p._fire_reanalyze("мягкий стоп: PRO велел ждать — прокол", force=True, kind="stop")
        assert not p._reanalyzing and m.handoffs[-1]["kind"] == "stop" and m.handoffs[-1]["deferred"]
        assert p.last_action.startswith("PRO у троса: ЖДАТЬ — прокол · совет: совет не раньше"), p.last_action
        # окно открыто (прошлый совет кончился 40 мин назад, хотя начат 2 часа назад) — совет идёт
        m.council_ts, m.council_end_ts = now - 7200, now - 2400
        p._fire_reanalyze("мягкий тейк: PRO велел подержать", force=True, kind="take")
        assert p._reanalyzing and m.handoffs[-1]["kind"] == "take" and not m.handoffs[-1]["deferred"]
        assert p._council_kind == "take" and not p._council_blocked and p._council_deferred is None

    asyncio.run(scenario())


# ── 5. связи с брокером нет — PRO, триаж и совет не зовутся; восстановление — одна перепроверка ─────────────────────
def test_broker_down_no_pro_no_triage_no_council_then_one_review(fake):
    async def scenario():
        m, p = make_pilot()
        await open_position(p)
        p.position["opened_ts"] = time.time() - 3600
        p._last_review_ts = time.time() - 3000
        p.review_ts = time.time() + 1500
        p.broker_ok = lambda: False                                 # AIPilot.broker_ok (агент Y) — связи нет
        p.feed = {"ok": False, "kind": "auth", "cause": "401: токен не принят", "reason": "401: токен не принят",
                  "since": time.time() - 300}
        await p._review_bg(PRICE)
        assert fake.count("mission_review") == 0 and "связи с брокером нет" in p.last_action, p.last_action
        assert p._ask_review("резкий ход: −2 % за 5 мин", kind="shock") is None, "триаж без брокера не зовём"
        await mission.on_serious_news({"note": "ЦБ поднял ставку"})
        await settle_bg(p)
        assert fake.count("event_triage") == 0 and p.review_ts >= time.time() + 1400
        assert "резкий ход" in p._review_reason and "ЦБ поднял ставку" in p._review_reason
        p._fire_reanalyze("перепроверка потребовала свежий разбор", kind="council")
        assert not p._reanalyzing and m.handoffs[-1]["deferred"]
        r = await mission.council_again("TEST", "пересмотр по кнопке")
        assert r["ok"] is False and "нет связи с брокером" in r["note"] and "401" in r["note"], r
        assert m.council_task is None
        assert "СВЯЗЬ С БРОКЕРОМ: нет доступа (токен или права)" in p._situation_text(PRICE)
        p._on_feed_change(False, dict(p.feed, was="ok", down_s=0.0))
        await settle_bg(p)
        assert fake.tolmach[-1]["title"] == "Нет доступа к брокеру" and "не зову" in fake.tolmach[-1]["detail"]
        assert ("pilot", "error") in [x[:2] for x in stages("pilot")]
        # связь вернулась: одна перепроверка по поводу event (с накопленными поводами)
        p.broker_ok = lambda: True
        p.feed = {"ok": True, "kind": "ok", "reason": "", "since": None}
        p.last_action = "связь с брокером восстановлена (не было 5 мин 0 с: 401) — сверка со счётом, пилот снова ведёт"
        p._on_feed_change(True, {"ok": True, "kind": "ok", "was": "auth", "down_s": 300.0, "reason_was": "401"})
        h = m.handoffs[-1]
        assert h["kind"] == "event" and not h["deferred"] and p.review_ts <= time.time() + 1, h
        assert p.last_action.startswith("связь с брокером восстановлена (не было 5 мин 0 с: 401) — сверка со счётом"), \
            p.last_action
        assert "дежурный PRO решит в" in p.last_action and fake.tolmach[-1]["title"] == "Связь с брокером восстановлена"
        fake.queue("mission_review", {"choice": "ДЕРЖАТЬ", "why": "ставка в цене"})
        await tick(p, PRICE)
        assert fake.count("mission_review") == 1 and m.reviews[-1]["choice"] == "ЖДЁМ"
        assert "связь с брокером восстановлена" in fake.calls[-1][1] and "ЦБ поднял ставку" in fake.calls[-1][1]

    asyncio.run(scenario())


# ── 6. killswitch и непринятый приказ — честно ────────────────────────────────────────────────────────────────────
def test_killswitch_hook_and_refused_order_are_honest(fake):
    async def scenario():
        m, p = make_pilot()
        p._on_killswitch({"reason": "серия убытков 3 подряд", "ts": time.time(), "pnl": -1234.4, "trades": 3,
                          "position": {"side": "long", "lots": 2}})
        await settle_bg(p)
        assert m.note.startswith("killswitch: серия убытков 3 подряд (P/L сессии -1234 ₽, сделок 3) — торговля "
                                 "остановлена до нового торгового дня"), m.note
        assert "позицию long 2 лот пилот закрывает" in m.note
        assert fake.tolmach[-1]["title"] == "Killswitch: торговля остановлена"
        assert ("pilot", "error") in [x[:2] for x in stages("pilot")]
        # заперт — причина отказа и пометка совета по кнопке
        p.session_risk = SimpleNamespace(locked=True, state=lambda: {"reason": "дневной лимит −6 %"}, reason=None)
        why = mission._killswitch_why(p)
        assert why and why.startswith("killswitch заблокирован (дневной лимит −6 %)"), why
        p.adopt_refused = None
        assert mission._refused_why(p) == why
        p.adopt_refused = "KILLSWITCH: дневной лимит — приказы не принимаются"
        assert mission._refused_why(p) == p.adopt_refused, "причина отказа пилота — первой"
        r = await mission.council_again("TEST", "по кнопке")
        try:
            assert r["ok"] and "внимание: killswitch заблокирован" in r["note"] and "пилот не примет" in r["note"], r
        finally:
            await mission._cancel_council(m)            # совет не идёт дальше старта (ИИ по сети не зовём)

    asyncio.run(scenario())


def test_young_flip_sets_adopt_refused(fake):
    async def scenario():
        m, p = make_pilot()
        await open_position(p)
        assert p.adopt_forecast(ex("SELL", None, 90.0, 102.0)) is False
        assert p.adopt_refused and p.adopt_refused.startswith("переворот отклонён"), p.adopt_refused
        assert mission._refused_why(p) == p.adopt_refused
        assert p.review_ts <= time.time() + 900 + 1, "закрытие без переворота — повод через PYTHIA_AFTER_CLOSE_SEC"
        assert p.adopt_forecast(ex("HOLD", None, 112.0, 97.0)) is True and p.adopt_refused is None

    asyncio.run(scenario())


def test_unknown_account_position_is_not_a_position():
    assert mission._pos_known("ОТКРЫТАЯ ПОЗИЦИЯ НА СЧЁТЕ: long 3 лот @99") is True
    assert mission._pos_known("ПОЗИЦИЯ НА СЧЁТЕ НЕИЗВЕСТНА: счёт не прочитан (401)") is False
    assert mission._pos_known("") is False and mission._pos_known(None) is False


# ── 7. стадии шины закрываются ────────────────────────────────────────────────────────────────────────────────────
def test_news_stage_closes_on_cancel(monkeypatch):
    calls = []

    async def rec(scope, run_id, stage, status, **kw):
        calls.append((stage, status, kw.get("detail") or ""))

    async def hang(*a, **k):
        await asyncio.sleep(30)
        return []

    monkeypatch.setattr(newsflow.bus, "stage", rec)
    monkeypatch.setattr(newsflow, "collect_targeted", hang)
    monkeypatch.setattr(config, "PYTHIA_GNEWS", True, raising=False)

    async def scenario():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(newsflow.enrich_ticker("mission-TEST", "TEST", "Тест", "futures", 1), 0.05)

    asyncio.run(scenario())
    assert [(s, st) for s, st, _ in calls] == [("news", "start"), ("news", "error")], calls
    assert "сбор прерван" in calls[-1][2]
    assert getattr(newsflow.enrich_ticker, "closes_stage_on_cancel", False) is True


def test_mission_closes_news_stage_for_module_without_flag(fake, monkeypatch):
    async def enrich(*a, **k):                           # чужой / старый модуль: стадию при отмене не закрывает
        await asyncio.sleep(30)

    monkeypatch.setattr(mission, "_mod", lambda name: SimpleNamespace(enrich_ticker=enrich) if name == "newsflow" else None)
    monkeypatch.setattr(config, "PYTHIA_GNEWS", True, raising=False)

    async def scenario():
        m, p = make_pilot()
        await mission._enrich_news(m, "rid", timeout=0.05)
        assert ("news", "error") in [x[:2] for x in stages("news")], stages("news")

    asyncio.run(scenario())


def test_review_stage_closed_in_finally(fake, monkeypatch):
    async def scenario():
        m, p = make_pilot()
        p.prices.append(PRICE)
        fake.queue("mission_review", {"choice": "КУПИТЬ", "why": "т", "invalidation": 98.0})
        monkeypatch.setattr(p, "_review_execute", AsyncMock(side_effect=RuntimeError("исполнение споткнулось")))
        await p._review_bg(PRICE)
        await settle_bg(p)
        st = [x[:2] for x in stages("review")]
        assert st[0] == ("review", "start") and st[-1] == ("review", "error"), st
        assert not p._review_busy

    asyncio.run(scenario())


def test_pilot_note_progress_then_done(fake):
    async def scenario():
        m, p = make_pilot()
        mission._pilot_note(m, "приказ протух — повод к плановой")
        await settle_bg(p)
        ev = stages("pilot")
        assert [x[:2] for x in ev] == [("pilot", "progress"), ("pilot", "done")], ev
        assert ev[0][2] == "приказ протух — повод к плановой" and ev[1][2] is None and ev[1][3]["note"], ev
        mission.bus.stage.reset_mock()
        mission._pilot_note(m, "связь с брокером потеряна", final="error")
        await settle_bg(p)
        assert [x[:2] for x in stages("pilot")] == [("pilot", "error")]

    asyncio.run(scenario())


# ── 8. видимость: метка и исход решения, живое состояние прокола, строки брокера (агент Y) ───────────────────────
def test_review_label_and_outcome_in_status(fake):
    async def scenario():
        m, p = make_pilot()
        p.prices.append(PRICE)
        fake.queue("mission_review", {"choice": "ЗАСАДА", "why": "откат к 99", "entry": 99.0, "invalidation": 98.0,
                                      "take": 103.0, "entry_kind": "откат"})
        await p._review(PRICE)
        st = p.status()
        assert st["last_review"]["label"] == "КУПИТЬ — засада (откат) @99", st["last_review"]
        assert st["review_outcome"]["outcome"] == "засада взведена @99 (откат)", st["review_outcome"]
        assert mission.status("TEST")["reviews"][-1]["label"] == "КУПИТЬ — засада (откат) @99"
        ev = [x for x in stages("review") if x[1] == "done"]
        assert ev and ev[-1][2].startswith("КУПИТЬ — засада (откат) @99: откат к 99 → засада взведена @99"), ev
        assert ev[-1][3]["outcome"] == "засада взведена @99 (откат)"
        assert fake.tolmach[-1]["title"] == "Перепроверка: КУПИТЬ" and "Итог: засада взведена @99" in fake.tolmach[-1]["detail"]

    asyncio.run(scenario())


def test_puncture_state_is_live(fake):
    async def scenario():
        m, p = make_pilot()
        p.puncture = {"pending": True, "side": "вверх", "ts": time.time()}
        p.review_ts = time.time() + 600
        assert p.status()["puncture"]["state"].startswith("передан перепроверке PRO в "), p.status()["puncture"]
        p._review_busy = True
        assert p.status()["puncture"]["state"] == "дежурный PRO думает над ним сейчас"
        p._review_busy = False

    asyncio.run(scenario())


def test_broker_lines_in_situation(fake):
    async def scenario():
        m, p = make_pilot()
        now = time.time()
        p.account_limits = {"buy_lots": 0, "sell_lots": 4, "ts": now, "note": "покупка недоступна: маржа исчерпана"}
        assert p._limits_line().startswith("ЛИМИТЫ СЧЁТА: покупка недоступна: маржа исчерпана, продажа до 4 (GetMaxLots ")
        p.account_limits = {"buy_lots": None, "sell_lots": None, "ts": now, "note": "лимиты не получены: брокер не ответил",
                            "last_buy_lots": 3, "last_sell_lots": 2}
        assert p._limits_line().startswith("ЛИМИТЫ СЧЁТА: неизвестны — лимиты не получены: брокер не ответил; последние "
                                           "известные: покупка до 3, продажа до 2"), p._limits_line()
        p.broker_refusal = {"what": "закрытие", "code": "30042", "text": "недостаточно средств", "count": 3, "ts": now,
                            "kind": "funds"}
        assert p._refusal_line().startswith("ОТКАЗ БИРЖИ: закрытие, код 30042, «недостаточно средств», 3 раз, ")
        p.feed = {"ok": False, "kind": "network", "cause": "таймаут Т-Банка", "since": now - 120}
        assert p._feed_line(now).startswith("СВЯЗЬ С БРОКЕРОМ: нет с ") and "(2 мин) — таймаут Т-Банка" in p._feed_line(now)
        sit = p._situation_text(PRICE)
        assert "ЛИМИТЫ СЧЁТА: неизвестны" in sit and "ОТКАЗ БИРЖИ: закрытие" in sit and "СВЯЗЬ С БРОКЕРОМ: нет" in sit, sit
        p.feed = {"ok": True, "kind": "ok", "reason": "", "since": None}
        assert p._feed_line() == "" and "СВЯЗЬ С БРОКЕРОМ" not in p._situation_text(PRICE)

    asyncio.run(scenario())
