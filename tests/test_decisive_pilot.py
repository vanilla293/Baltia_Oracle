# -*- coding: utf-8 -*-
"""ПИФИЯ v5.4.3 «РЕШИТЕЛЬНЫЙ ПИЛОТ»: что модель видит в ситуации и приказе, и будильник ЖДЁМ — на фейках.

Воля владельца 30.09.2026: «всё ещё играет ответами ждать — "прорыва нет, сидим ждём"». Код не решает за ИИ ни в одну
сторону — он показывает цену ожидания числами и будит: взведённый вход подан как решение («ВХОД ВЗВЕДЁН», «ПРОБОЙ
ПРОЙДЕН»), а не «жду»; прошлая перепроверка — с ценой решения и ходом с тех пор, серия ЖДЁМ вне рынка — строкой-фактом;
приказ WAIT — ориентир (не условие), цена тогда → сейчас, план — прошлым мнением; совет видит перепроверки свёрнутыми с
исходом и нейтральное состояние пилота; уровень, названный в ЖДЁМ, — будильник (один раз, с пейсингом, не при закрытом
рынке); WAIT без levels — числа из ориентира в ±5 % (время и даты не в счёт); у НОВЫЙ_АНАЛИЗ — длительность прошлого
совета. Ни сети, ни ключей, ни data/."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend import ai_pilot, ai_v5, config, mission, trader_risk

PRICE = 100.0


class Broker:
    """Мини-биржа: заявки исполняются сразу, портфеля нет (dry-снимок)."""
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
    """ai_v5.money_json / pro_json: очереди ответов по маршрутам, промпты — в calls."""

    def __init__(self):
        self.answers: dict = {}
        self.calls: list = []
        self.errors: list = []

    def queue(self, route, *answers):
        self.answers.setdefault(route, []).extend(answers)

    def count(self, route):
        return sum(1 for r, _ in self.calls if r == route)

    def last_user(self, route):
        return [u for r, u in self.calls if r == route][-1]

    async def _answer(self, route, user):
        self.calls.append((route, user))
        q = self.answers.get(route) or []
        return q.pop(0) if q else {}

    async def money_json(self, system, user, *, route, max_tokens=None, attempt_timeout=None):
        return await self._answer(route, user)

    async def pro_json(self, system, user, *, route="pro", max_tokens=None, attempt_timeout=None):
        return await self._answer(route, user)

    def note_error(self, text, route=""):
        self.errors.append((route, text))


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
                 ("PYTHIA_PROFIT_THINK", True), ("PYTHIA_MONEY_MODEL", "pro"), ("PYTHIA_EVENT_TRIAGE", True)):
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


def plan(side="long", entry=102.0, kind="прорыв", inv=99.0, take=106.0, age_s=0.0):
    return {"side": side, "entry": entry, "kind": kind, "invalidation": inv, "take": take,
            "why": "тест", "ts": time.time() - age_s, "src": "review"}


def wait_exec(wait_for="пробой 101 на объёме", price=PRICE, age_s=0.0, **kw):
    ex, err = mission._validate_exec(dict({"do": "WAIT", "wait_for": wait_for, "why": "перевеса нет"}, **kw), "auto", price)
    assert err is None, err
    ex["price"] = price                                    # так приказ хранит цену совета (_council)
    return ex, time.time() - age_s


# ── 1. взведённый вход — решение, а не «жду» ─────────────────────────────────────────────────────────────────────────
def test_armed_breakout_unbroken_line_has_distance_age_and_ttl():
    m, p = make_pilot()
    p.plan = plan(entry=102.0, age_s=30 * 60 + 5)
    sit = p._situation_text(101.5)
    assert ("ВХОД ВЗВЕДЁН (пробой): long при проходе 102 — до уровня +0.49 %, стоп 99.0, тейк 106.0; "
            f"взведён 30 мин, протухнет через {int(ai_pilot.PLAN_TTL_SEC // 60) - 31} мин") in sit, sit
    assert "ЖДУ ПРОБИТИЯ" not in sit and "жду" not in sit.lower(), sit
    assert "вход взведён (пробой): long при проходе 102 (цена 101.5, до уровня +0.49 %; вход после 2 тиков" \
        in p._entry_wait_text(101.5, 102.0)


def test_armed_breakout_crossed_by_price_or_mark():
    m, p = make_pilot()
    p.plan = plan(entry=102.0)
    sit = p._situation_text(102.6)
    assert "ПРОБОЙ ПРОЙДЕН: long, уровень 102, цена 102.6 (+0.59 % за уровнем); вход по пробою — после 2 тиков" in sit, sit
    assert "ВХОД ВЗВЕДЁН" not in sit
    p.plan["crossed"] = 102.0                              # пробили, цена вернулась под уровень
    sit2 = p._situation_text(101.8)
    assert "ПРОБОЙ ПРОЙДЕН: long, уровень 102, цена 101.8 (-0.20 % за уровнем — цена вернулась к уровню)" in sit2, sit2
    p.plan["crossed"] = 103.0                              # пометка от ДРУГОГО уровня — этот не пройден
    assert "ВХОД ВЗВЕДЁН (пробой): long при проходе 102" in p._situation_text(101.8)
    p.plan = plan(side="short", entry=98.0, inv=101.0, take=94.0)
    assert "ВХОД ВЗВЕДЁН (пробой): short при проходе 98 — до уровня -0.51 %" in p._situation_text(98.5)
    assert "ПРОБОЙ ПРОЙДЕН: short, уровень 98, цена 97.5 (+0.51 % за уровнем)" in p._situation_text(97.5)


def test_armed_pullback_line_and_base_pilot_texts():
    m, p = make_pilot()
    p.plan = plan(entry=99.0, kind="откат", inv=97.0, take=104.0)
    sit = p._situation_text(100.0)
    assert "ВХОД ВЗВЕДЁН (лимит на откате): long @99 — до уровня -1.00 %, стоп 97.0, тейк 104.0; взведён 0 мин" in sit, sit
    assert "ЗАСАДА (откат)" not in sit
    b = ai_pilot.AIPilot("TEST", deposit=100000.0, broker=Broker())
    b.plan = plan(entry=99.0, kind="откат")
    assert b._entry_wait_text(100.0, 99.0) == "вход взведён: засада long @99 (цена 100, вход в имба-момент)"
    assert "ВХОД ВЗВЕДЁН (засада): long @99.0 (вход в имба-момент)" in b._situation_text(100.0)
    assert "жду имба-момент" not in b._situation_text(100.0)


# ── 2. прошлая перепроверка с исходом, серия ЖДЁМ вне рынка ─────────────────────────────────────────────────────────
def test_last_review_line_has_decision_price_and_move(fake):
    async def scenario():
        m, p = make_pilot()
        p.prices.append(100.0)
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "прорыва нет", "note": "т"})
        await p._review(100.0)
        assert m.reviews[-1]["in_pos"] is False and m.reviews[-1]["price"] == 100.0 and p.last_review["price"] == 100.0
        sit = p._situation_text(101.0)
        assert "Прошлая перепроверка (0 мин назад, цена 100): ЖДЁМ — прорыва нет; с тех пор 101 (+1.00 %)" in sit, sit
        assert "ЖДЁМ подряд" not in sit, "один ЖДЁМ — не серия"

    asyncio.run(scenario())


def test_wait_streak_fact_line(fake):
    async def scenario():
        m, p = make_pilot()
        for px in (100.0, 100.4, 100.7):
            p.prices.append(px)
            fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "прорыва нет — сидим ждём", "note": "т"})
            await p._review(px)
        now = time.time()
        # ревью 5.4.3: серия — свой счётчик (m.wait_streak), а не хвост m.reviews (его режет память)
        ws = m.wait_streak
        assert ws and ws["n"] == 3 and ws["price"] == 100.0 and ws["lo"] == 100.0 and ws["hi"] == 100.7, ws
        ws["ts"] = now - 40 * 60                                 # серия началась 40 мин назад
        p._flat_track(99.5)                                      # тики за серию — мин/макс
        p._flat_track(101.2)
        p._flat["ts"] = now - 3600
        sit = p._situation_text(101.0)
        want = (f"ЖДЁМ подряд: 3 за 40 мин (с {ai_v5.fmt_ts(now - 2400)}); вне рынка с {ai_v5.fmt_ts(now - 3600)}; "
                f"цена за серию 99.5–101.2 (размах 1.71 %), от первого ЖДЁМ (100) +1.00 %")
        assert want in sit, sit
        # в позиции ЖДЁМ — удержание: серия «вне рынка» снята
        p.position = {"side": "long", "entry": 100.0, "lots": 1, "take": None, "invalidation": 98.0,
                      "opened_ts": now - 60, "stop_id": None, "floating": 0.0}
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "держим", "note": "т"})
        await p._review(101.0)
        assert m.wait_streak is None and "ЖДЁМ подряд" not in p._situation_text(101.0)
        p.position = None
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "т", "note": "т"})
        await p._review(101.0)
        assert m.wait_streak["n"] == 1 and "ЖДЁМ подряд" not in p._situation_text(101.0), "серия прервана решением в позиции"
        p.position = {"side": "long", "entry": 100.0, "lots": 1, "take": None, "invalidation": 98.0,
                      "opened_ts": now, "stop_id": None, "floating": 0.0}
        p._flat_track(101.0)
        assert p._flat is None and m.wait_streak is None, "позиция открыта — отсчёт вне рынка и серия сняты"

    asyncio.run(scenario())


# ── 3. приказ WAIT: ориентир, цена тогда → сейчас, план — прошлым мнением ────────────────────────────────────────────
def test_wait_exec_text_and_situation_show_cost_of_waiting():
    m, p = make_pilot()
    ex, ts = wait_exec(age_s=20 * 60 + 5, plan="ждём пробоя", time_note="после 15:00")
    assert p.adopt_forecast({"exec": ex})
    m.exec, m.exec_ts = ex, ts
    p.prices.append(101.0)
    txt = mission._exec_text(m)
    assert (f"Приказ совета {ai_v5.fmt_ts(ts)} (20 мин назад, цена тогда 100): WAIT — вне рынка. "
            "Ориентир совета (не условие): пробой 101 на объёме. С тех пор цена 100 → 101 (+1.00 %).") in txt, txt
    assert "Будильник кода у уровней 101 (числа из ориентира совета) — проход цены будит дежурного PRO, это не вход." in txt
    assert "Уверенность — — перевеса нет" in txt and "совет ждал" not in txt
    assert "\nКак совет видел ведение 20 мин назад (прошлое мнение): план — ждём пробоя; тайминг — после 15:00" in txt, txt
    assert "\nПлан: " not in txt and "\nТайминг: " not in txt
    sit = p._situation_text(101.0)
    assert ("ПРИКАЗ СОВЕТА (20 мин назад): вне рынка — прошлое мнение, не запрет; с тех пор цена +1.00 %. Реши заново — "
            + mission.prompts_mission.review_options("auto", False)) in sit, sit
    assert "пробой 101 на объёме" not in sit, "ориентир — в блоке приказа, в ситуации не дублируется"
    # старый приказ без цены (до 5.4.3) — без хода, честно
    ex.pop("price")
    assert "цена тогда" not in mission._exec_text(m) and "С тех пор" not in mission._exec_text(m)
    assert "; с тех пор цена" not in p._situation_text(101.0)


def test_council_stores_exec_price_and_duration(monkeypatch):
    saved: list = []

    async def fit(blocks):
        return blocks, []

    async def fake_run(self):
        await asyncio.Event().wait()

    async def pro_json(system, user, *, route="pro", **kw):
        return {"do": "WAIT", "wait_for": "закрепление выше 101", "why": "мутно"}

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
    monkeypatch.setattr(mission.store_v5, "mission_put", lambda ticker, data: saved.append(dict(data)))

    async def scenario():
        r = await mission.start("SBER", "auto")
        assert r["ok"], r
        m = mission._M["SBER"]
        await m.council_task
        try:
            assert m.error is None and m.exec["do"] == "WAIT" and m.exec["price"] == 100.0, (m.error, m.exec)
            assert m.exec["levels"] == [101.0] and m.exec["levels_src"] == "wait_for", m.exec
            assert m.council_dur is not None and 0.0 <= m.council_dur < 60.0, m.council_dur
            assert saved and saved[-1]["council_dur"] == m.council_dur and saved[-1]["exec"]["price"] == 100.0
            sit = m.pilot._situation_text(100.5)
            assert ("Последний полный совет: 0 мин назад, длился 0 мин (пока совет идёт, пилот не входит и не "
                    "перепроверяет, взведённый вход без позиции снимается)") in sit, sit
            assert "цена тогда 100" in mission._exec_text(m)
        finally:
            m.task.cancel()
            await asyncio.gather(m.task, return_exceptions=True)

    asyncio.run(scenario())


def test_from_store_restores_council_duration(monkeypatch):
    rec = {"ticker": "TEST", "name": "Тест", "asset_class": "futures", "play": "auto", "phase": "idle",
           "council_dur": 1260.0, "exec": None}
    monkeypatch.setattr(mission.store_v5, "mission_get", lambda t: rec)
    m = mission._from_store("TEST")
    assert m.council_dur == 1260.0
    rec.pop("council_dur")
    assert mission._from_store("TEST").council_dur is None


# ── 4. блок prev для совета: свёрнутые ЖДЁМ с исходом, нейтральное состояние пилота ─────────────────────────────────
def test_prev_block_folds_waits_with_price_outcome():
    m, p = make_pilot()
    t0 = time.time() - 3600
    m.reviews = [
        {"ts": t0, "choice": "ЖДЁМ", "why": "прорыва нет", "price": 100.0, "in_pos": False},
        {"ts": t0 + 600, "choice": "ЖДЁМ", "why": "прорыва нет", "price": 100.3, "in_pos": False},
        {"ts": t0 + 1200, "choice": "ЖДЁМ", "why": "сидим ждём пробоя", "price": 100.5, "in_pos": False},
        {"ts": t0 + 1800, "choice": "КУПИТЬ_СЕЙЧАС", "why": "пробой", "price": 100.8, "in_pos": False},
        {"ts": t0 + 2400, "choice": "ЗАКРЫТЬ", "why": "выдохлось", "price": 101.6, "in_pos": True},
        {"ts": t0 + 3000, "choice": "ЖДЁМ", "why": "после выхода", "price": 101.4, "in_pos": False},
        {"ts": t0 + 3300, "choice": "ЖДЁМ", "why": "откат не пришёл", "price": 101.2, "in_pos": False},
    ]
    p.prices.append(101.5)
    p.last_action = "совет: вне рынка — ждал: пробой 101; перепроверка через 30 мин"
    txt = mission._prev_text(m)
    assert "ЖДЁМ ×3 (" in txt and "цена 100 → 100.8 (+0.80 %) к следующему решению — последнее: сидим ждём пробоя" in txt, txt
    assert f"{ai_v5.fmt_ts(t0 + 1800)} КУПИТЬ @100.8 → сейчас 101.5 (+0.69 %) — пробой" in txt, txt   # метка модели
    assert f"{ai_v5.fmt_ts(t0 + 2400)} ЗАКРЫТЬ @101.6 → сейчас 101.5 (-0.10 %) — выдохлось" in txt, txt
    assert "ЖДЁМ ×2 (" in txt and "цена 101.4 → 101.5 (+0.10 %) сейчас — последнее: откат не пришёл" in txt, txt
    assert txt.count("прорыва нет") == 0, "20 одинаковых «ждём» не повторяются — свёрнуты"
    assert "Пилот: вне рынка; результат сессии" in txt and "ждал: пробой 101" not in txt, txt
    p.plan = plan(entry=102.0)
    assert "Пилот: вход взведён: long прорыв @102, стоп 99.0, тейк 106.0;" in mission._prev_text(m)
    p.plan = None
    p.position = {"side": "long", "entry": 100.0, "lots": 3, "take": 104.0, "invalidation": 98.0,
                  "opened_ts": time.time(), "stop_id": None, "floating": 0.0}
    txt3 = mission._prev_text(m)
    assert "Пилот: в позиции long 3 лот @100;" in txt3 and "ОТКРЫТАЯ ПОЗИЦИЯ: long 3 лот" in txt3, txt3


# ── 5. будильник ЖДЁМ ───────────────────────────────────────────────────────────────────────────────────────────────
def test_wait_with_level_sets_alarm_and_wakes_once(fake):
    async def scenario():
        m, p = make_pilot()
        await tick(p, 100.0, n=2)                          # рынок жив
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "пробоя 101 нет", "entry": 101.0, "entry_kind": "прорыв"})
        await p._review(100.0)
        w = p._wake
        assert w and w["level"] == 101.0 and w["ref"] == 100.0 and w["dir"] == "up" and not w["fired"], w
        assert m.reviews[-1]["wake"] == 101.0 and p.plan is None and p.pending is None
        assert "будильник у 101" in p.last_action, p.last_action
        st = p.status()["wake"]
        assert st["level"] == 101.0 and st["dir"] == "up" and st["why"] == "пробоя 101 нет", st
        assert "БУДИЛЬНИК: уровень 101 из твоего ЖДЁМ" in p._situation_text(100.3)
        n_h = len(m.handoffs)
        p._wake_watch(100.8)
        assert len(m.handoffs) == n_h, "под уровнем — тишина"
        p._wake_watch(101.2)
        h = m.handoffs[-1]
        assert len(m.handoffs) == n_h + 1 and h["kind"] == "wait_level" and not h["deferred"], h
        assert h["reason"].startswith("цена 101.2 прошла уровень 101, который ты назвал в ЖДЁМ ") \
            and h["reason"].endswith("— реши по живой картине"), h
        assert p.review_ts <= p._last_review_ts + mission.EVENT_MIN_GAP_SEC + 1, "пейсинг как у уровней WAIT"
        assert not p._review_pulled, "будильник — не событие: пейсинг событий не тратится"
        p._wake_watch(100.5)
        p._wake_watch(101.5)
        assert len(m.handoffs) == n_h + 1, "будильник срабатывает один раз"
        assert "сработал" in p._situation_text(101.5)
        assert p.plan is None and not p.broker.placed, "код не входит за ИИ — только будит"
        # следующий ответ снимает будильник
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "ложный вынос"})
        p.prices.append(101.5)
        await p._review(101.5)
        assert p._wake is None and p.status()["wake"] is None and "wake" not in m.reviews[-1]

    asyncio.run(scenario())


def test_alarm_down_direction_and_quiet_conditions(fake, monkeypatch):
    async def scenario():
        m, p = make_pilot()
        await tick(p, 100.0, n=2)
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "продавец не выдохся", "entry": 98.5})
        await p._review(100.0)
        assert p._wake and p._wake["dir"] == "down", p._wake
        n_h = len(m.handoffs)
        p._review_busy = True                              # PRO думает прямо сейчас — его ответ решит
        p._wake_watch(98.4)
        assert len(m.handoffs) == n_h and not p._wake["fired"]
        p._review_busy = False
        monkeypatch.setattr(p, "_market_closed", lambda: True)   # рынок закрыт — не будим
        p._wake_watch(98.4)
        assert len(m.handoffs) == n_h and not p._wake["fired"]
        monkeypatch.setattr(p, "_market_closed", lambda: False)
        p._reanalyzing = True                              # идёт совет — не будим
        p._wake_watch(98.4)
        assert len(m.handoffs) == n_h
        p._reanalyzing = False
        p._wake_watch(98.4)
        assert len(m.handoffs) == n_h + 1 and "прошла уровень 98.5" in m.handoffs[-1]["reason"]
        # план появился — будильник снят
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "т", "entry": 99.0})
        await p._review(100.0)
        assert p._wake
        p.plan = plan(entry=102.0)
        p._wake_watch(98.0)
        assert p._wake is None

    asyncio.run(scenario())


def test_no_alarm_without_level_in_position_or_with_plan(fake):
    async def scenario():
        m, p = make_pilot()
        p.prices.append(100.0)
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "без уровня"})
        await p._review(100.0)
        assert p._wake is None and "wake" not in m.reviews[-1], "entry нет — будильника нет"
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "уровень на цене", "entry": 100.0})
        await p._review(100.0)
        assert p._wake is None, "уровень равен цене решения — направления нет"
        p.plan = plan(entry=102.0)                         # взведённый вход остаётся как есть
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "пусть взведён", "entry": 101.0})
        await p._review(100.0)
        assert p._wake is None and p.plan and p.plan["entry"] == 102.0
        p.plan = None
        p.position = {"side": "long", "entry": 100.0, "lots": 3, "take": 104.0, "invalidation": 98.0, "inv0": 98.0,
                      "opened_ts": time.time() - 3600, "stop_id": None, "floating": 0.0}
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "держим", "entry": 101.0})
        await p._review(100.0)
        assert p._wake is None and m.reviews[-1]["in_pos"] is True, "в позиции ЖДЁМ — удержание, не будильник"

    asyncio.run(scenario())


def test_alarm_tick_path_pulls_review(fake):
    """Тик: будильник проверяется в MissionPilot.tick (как уровни WAIT) и сам поднимает перепроверку."""
    async def scenario():
        m, p = make_pilot()
        await tick(p, 100.0, n=2)
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "жду 101", "entry": 101.0})
        await p._review(100.0)
        p._last_review_ts -= mission.EVENT_MIN_GAP_SEC + 60        # PRO ответил давно
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "вынос без объёма"})
        await tick(p, 101.3)
        assert fake.count("mission_review") == 2, fake.calls
        ur = fake.last_user("mission_review")
        assert "ПОВОД ПЕРЕПРОВЕРКИ (внеплановая): цена 101.3 прошла уровень 101, который ты назвал в ЖДЁМ" in ur, ur[-1500:]
        assert "Прошлая перепроверка (0 мин назад, цена 100): ЖДЁМ — жду 101; с тех пор 101.3 (+1.30 %)" in ur
        assert p._wake is None

    asyncio.run(scenario())


# ── 6. WAIT без levels: числа из ориентира в ±5 %, время и даты не в счёт ───────────────────────────────────────────
@pytest.mark.parametrize("text, price, want", [
    ("закрепление выше 101,5 на объёме после 11:20; откат к 98.7 или 250", 100.0, [101.5, 98.7]),
    ("после 30.09.2026 и 30.09 в 10:00 — пробой 30.5", 30.0, [30.5]),
    ("ход 1.5 % за 10 мин, пробой 1.52", 1.5, [1.52]),
    ("нужен объём покупателя", 100.0, []),
    ("пробой 101, потом 101", 100.0, [101.0]),
    ("", 100.0, []),
    ("пробой 101", None, []),
])
def test_levels_from_text(text, price, want):
    assert mission._levels_from_text(text, price) == want


def test_wait_without_levels_takes_numbers_from_wait_for_and_wakes():
    ex, err = mission._validate_exec({"do": "WAIT", "wait_for": "закрепление выше 101 после 11:20, до 30.09", "why": "т"},
                                     "auto", 100.0)
    assert err is None and ex["levels"] == [101.0] and ex["levels_src"] == "wait_for", ex
    ex2, _ = mission._validate_exec({"do": "WAIT", "wait_for": "выше 101", "levels": [99.0], "why": "т"}, "auto", 100.0)
    assert ex2["levels"] == [99.0] and "levels_src" not in ex2, "уровни совета главнее текста"
    ex3, _ = mission._validate_exec({"do": "WAIT", "wait_for": "пробой 120", "why": "т"}, "auto", 100.0)
    assert ex3["levels"] == [] and "levels_src" not in ex3, "дальше ±5 % — не будильник"

    async def scenario():
        m, p = make_pilot()
        m.exec, m.exec_ts = ex, time.time()
        assert p.adopt_forecast({"exec": ex}) and p.plan is None
        await tick(p, 100.0, n=2)
        p._last_review_ts = time.time() - 30
        p._wait_watch(101.3)
        h = m.handoffs[-1]
        assert h["kind"] == "wait_level" and "WAIT: цена 101.3 прошла уровень 101 из приказа совета" in h["reason"], h
        assert p.plan is None and not p.broker.placed

    asyncio.run(scenario())


# ── 7. цена НОВЫЙ_АНАЛИЗ: длительность прошлого совета и что пилот делает, пока совет идёт ───────────────────────────
def test_council_line_shows_duration_and_freeze():
    m, p = make_pilot()
    m.council_ts, m.council_dur = time.time() - 600, 420.0
    sit = p._situation_text(100.0)
    assert ("Последний полный совет: 10 мин назад, длился 7 мин (пока совет идёт, пилот не входит и не перепроверяет, "
            "взведённый вход без позиции снимается)") in sit, sit
    m.council_dur = None
    assert "Последний полный совет: 10 мин назад (пока совет идёт" in p._situation_text(100.0)
