# -*- coding: utf-8 -*-
"""ПИФИЯ v5.4.4 «СВЯЗЬ С БРОКЕРОМ» (часть Y: брокер и петля пилота) — офлайн-регрессии по жалобе владельца 30.09.2026:
«пилот работал без перерыву, как минимум так показывал», «написал вход, но не вошёл, также было с выходом, закрытие».

Токен отозван → петля честно «нет доступа» (feed), ни тиков, ни ИИ, ни заявок; заявки 40002 → без лесенки и без петли
PRO; отказы биржи — пауза, а не каждый тик, причина видна (broker_refusal); потерянный ответ — опрос с паузой; портфель не
прочитан ≠ 0 лотов (state-файл цел); ЗАКРЫТЬ не теряется за ужатием; добор совета у уровня взводится; killswitch —
одна запись и хук; лимиты счёта (GetMaxLots); статус для панели (loop_alive, ticking, feed …); два домена API Т-Банка;
панель проблем и Telegram; тихая смерть пилота в миссии. Ни сети, ни ключей, ни data/."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from backend import ai_pilot, api_v5, config, mission, telegram, tinkoff, trader_broker, trader_risk

BOOK = {"best_bid": 99.9, "best_ask": 100.1}
AUTH = {"ts": 0.0, "path": "GetLastPrices", "status": 401, "code": "40003", "kind": "auth",
        "reason": "токен Т-Банка не принят (40003) — выпусти новый с полным доступом и вставь в «Ключи»",
        "text": "Tinkoff 401 · 40003: Authentication token is missing or invalid [GetLastPrices]"}


class Broker:
    """Мини-биржа: заявки исполняются сразу, отказ — текстом/полями как у trader_broker.Broker."""
    mode = "real"

    def __init__(self):
        self.placed: list[dict] = []
        self.attempts = 0
        self.refuse: dict | None = None        # {"error", "err_kind", "err_code"} — отказ PostOrder
        self.uncertain = False                 # ответ PostOrder потерян
        self.state_fail: dict | None = None    # GetOrderState не отвечает
        self.mx: dict | None = None
        self.mx_calls = 0
        self.pf: list | None = None            # портфель (None — зеркала нет, пусто)
        self.pf_error: str | None = None

    async def place(self, figi, direction, lots, price=None, tag="", **kw):
        self.attempts += 1
        if self.refuse:
            return {"ok": False, **self.refuse}
        oid = f"F-{self.attempts}"
        self.placed.append({"order_id": oid, "direction": direction, "lots": int(lots), "price": price, "tag": tag})
        if self.uncertain:
            return {"ok": False, "uncertain": True, "error": "нет связи с Т-Банком (таймаут)", "order_id": oid}
        return {"ok": True, "order_id": oid}

    async def order_state(self, oid, **kw):
        if self.state_fail:
            return {"ok": False, **self.state_fail}
        return {"ok": True, "filled": True, "status": "FILL", "exec_lots": 0}

    async def cancel(self, oid, **kw):
        return {"ok": True}

    async def place_stop(self, *a, **kw):
        return {"ok": True, "stop_order_id": "S-1"}

    async def cancel_stop(self, sid):
        return {"ok": True}

    async def max_lots(self, figi, price=None):
        self.mx_calls += 1
        return dict(self.mx) if self.mx else None

    async def portfolio(self):
        if self.pf_error:
            return {"cash": None, "positions": [], "error": self.pf_error}
        return {"mode": "real", "cash": None, "positions": list(self.pf or [])}


class Tk:
    """Фейк модуля tinkoff для петли пилота: цена, почему её нет, проверка токена, эпоха токена."""

    def __init__(self):
        self.price: float | None = 100.0
        self.err: dict | None = None
        self.access: dict = {"ok": True, "trade": True, "kind": "ok", "reason": "токен принят: полный доступ",
                             "access": "FULL_ACCESS"}
        self.checks = 0
        self.epoch = 0
        self.accs: list | None = [{"id": "acc-1", "access": "FULL_ACCESS"}]
        self.pf: dict | None = {"free_rub": 100000.0, "total_rub": 100000.0, "positions": []}
        self.calls: list[str] = []

    async def last_price(self, figi):
        self.calls.append("GetLastPrices")
        return {"price": self.price} if self.price else None

    def price_error(self, figi):
        return dict(self.err) if (self.price is None and self.err) else None

    async def check_access(self, timeout=15.0):
        self.checks += 1
        return dict(self.access)

    def token_epoch(self):
        return self.epoch

    async def orderbook(self, figi, depth=10):
        return dict(BOOK)

    async def accounts(self):
        return self.accs

    async def portfolio(self, acc):
        return self.pf

    async def resolve(self, ticker, ac):
        return {"figi": "FIGI-T", "lot": 1, "minPriceIncrement": {"units": 0, "nano": 10000000}}

    async def futures_margin(self, figi):
        return {"margin_buy": 12000.0, "margin_sell": 12000.0, "min_price_increment": 0.01,
                "min_price_increment_amount": 0.01}

    classify = staticmethod(tinkoff.classify)

    def failure_text(self, default=""):
        return AUTH["reason"] if self.accs is None else default


@pytest.fixture
def tk(monkeypatch):
    fake = Tk()
    monkeypatch.setattr(ai_pilot, "tinkoff", fake)
    monkeypatch.setattr(ai_pilot, "market_clock", None)     # рынок открыт (часы выключены)
    return fake


def make_pilot(position: bool = False, lots: int = 8) -> ai_pilot.AIPilot:
    p = ai_pilot.AIPilot("TEST", deposit=100000.0, broker=Broker())
    p._state_path = None
    p.figi, p.asset_class = "FIGI-T", "futures"
    p.go_per_lot, p.go_sell, p.tick_size, p.point_value = 12000.0, 12000.0, 0.01, 1.0
    p.deposit = 100000.0
    p.session_risk = trader_risk.SessionRisk(100000.0)
    p._sr_day = p._msk_day()
    p._prepared = True
    if position:
        p.position = {"side": "long", "entry": 100.0, "lots": lots, "take": 110.0, "invalidation": 98.0, "inv0": 98.0,
                      "opened_ts": time.time() - 3600, "stop_id": None, "floating": 0.0}
        p.state = "В_ПОЗИЦИИ"
    return p


def run(coro):
    return asyncio.run(coro)


# ── 1. связь: токен отозван → честно «нет доступа», ни тика, ни ИИ, ни заявок; вернулась — сама ───────────────
def test_revoked_token_goes_no_access_once_without_ticks_and_recovers(tk):
    async def scenario():
        p = make_pilot(position=True)
        p.broker.pf = [{"figi": "FIGI-T", "qty": 8, "avg": 100.0}]     # на счёте те же 8 лотов (сверка после связи)
        seen = []
        p._on_feed_change = lambda ok, info: seen.append((ok, info))
        p.loop_alive = True
        tk.price, tk.err = None, dict(AUTH, ts=time.time())
        tk.access = {"ok": False, "trade": False, "kind": "auth", "reason": AUTH["reason"]}
        p.review_ts = 0.0                                   # перепроверка «пора» — всё равно не зовётся
        assert await p._loop_step() is True
        assert p.feed["ok"] is False and p.feed["kind"] == "auth" and p.state == "НЕТ_ДОСТУПА"
        assert tk.checks == 1 and p._last_tick_ts == 0.0 and p._tick_n == 0
        la = p.last_action
        assert "НЕТ ДОСТУПА" in la and "40003" in la and "без защиты" in la and "long 8 лот" in la, la
        for _ in range(4):
            await p._loop_step()
        assert p.last_action == la and tk.checks == 1 and not p.broker.placed and p._tick_n == 0
        assert [ok for ok, _ in seen] == [False] and not p.broker_ok()
        st = p.status()
        assert st["ticking"] is False and st["loop_alive"] is True and st["last_tick_ts"] is None
        assert st["feed"]["kind"] == "auth" and st["review_busy"] is False and st["review_started_ts"] is None
        # новый токен в «Ключах» → проверка сразу (новая эпоха), цена пошла → связь вернулась, пилот тикает
        tk.epoch += 1
        tk.price, tk.err = 100.2, None
        tk.access = {"ok": True, "trade": True, "kind": "ok", "reason": "ок"}
        await p._loop_step()
        assert p.feed["ok"] and p.state == "В_ПОЗИЦИИ" and [ok for ok, _ in seen] == [False, True]
        assert seen[-1][1]["was"] == "auth" and p.ticking() and p._tick_n == 1

    run(scenario())


def test_plain_missing_price_waits_grace_then_no_link_closed_market_is_ok(tk, monkeypatch):
    async def scenario():
        p = make_pilot()
        p.loop_alive = True
        tk.price, tk.err = None, None                        # цены нет, ошибки нет
        await p._loop_step()
        assert p.feed["ok"] is True and p._px_fail and p._px_fail["kind"] == "no_price"   # запас PYTHIA_FEED_GRACE_SEC
        p._px_fail["since"] -= ai_pilot.feed_grace_sec() + 1
        await p._loop_step()
        assert p.feed["ok"] is False and p.feed["kind"] == "no_price" and p.state == "НЕТ_СВЯЗИ", p.feed
        # рынок закрыт: цены нет — это ночь, не авария; брокер достижим
        p.market = {"open": False}
        p._feed_recompute(time.time())
        assert p.feed["ok"] is True and p.feed["kind"] == "closed" and p.broker_ok()

    run(scenario())


def test_ticking_needs_live_loop_fresh_tick_and_feed(tk, monkeypatch):
    p = make_pilot()
    assert p.ticking() is False                              # петля не поднята
    p.loop_alive, p.last_tick_ts = True, time.time()
    assert p.ticking() is True and p.status()["ticking"] is True
    p.last_tick_ts = time.time() - ai_pilot.tick_stale_sec() - 1
    assert p.ticking() is False
    p.market = {"open": False}                               # закрыт: тик раз в PYTHIA_CLOSED_TICK_SEC — 3 шага запаса
    assert p.ticking() is True
    p.market = None
    p.last_tick_ts = time.time()
    p.feed = {"ok": False, "kind": "network", "reason": "x", "since": time.time()}
    assert p.ticking() is False


# ── 2. заявки 40002: одна попытка, сразу «нет доступа», без лесенки и без ИИ; полный доступ — снова торгует ───────
def test_rights_refusal_on_entry_no_ladder_then_full_access_clears(tk):
    async def scenario():
        p = make_pilot()
        p.loop_alive = True
        p.broker.refuse = {"error": "Tinkoff 403 · 40002: Insufficient privileges [PostOrder]", "err_kind": "rights",
                           "err_code": "40002", "err_status": 403}
        assert p.adopt_forecast({"exec": {"do": "BUY", "entry": None, "take": 110.0, "invalidation": 98.0, "why": "т"}})
        await p.tick(100.0, BOOK)
        assert p.broker.attempts == 1 and p._entry_fail == 0 and p.no_entry_until == 0.0 and p.plan is not None
        assert p.feed["kind"] == "rights" and p.state == "НЕТ_ДОСТУПА"
        br = p.status()["broker_refusal"]
        assert br["what"] == "entry" and br["code"] == "40002" and br["kind"] == "rights" and br["count"] == 1
        tk.access = {"ok": True, "trade": False, "kind": "rights", "reason": "токен только для чтения", "access": "READ_ONLY"}
        for _ in range(6):
            await p._loop_step()
        assert p.broker.attempts == 1 and p._tick_n == 1, "без доступа петля не тикает и не штурмует биржу"
        p._feed_probe_ts -= ai_pilot.FEED_PROBE_SEC
        await p._loop_step()
        assert p.feed["ok"] is False and "только для чтения" in p.feed["reason"]
        # полный доступ, пауза отказа вышла → следующий шаг торгует
        p.broker.refuse = None
        tk.access = {"ok": True, "trade": True, "kind": "ok", "reason": "ок", "access": "FULL_ACCESS"}
        p._trade_refusal["hold_until"] = 0.0
        p._feed_probe_ts -= ai_pilot.FEED_PROBE_SEC
        await p._loop_step()
        assert p.feed["ok"] and p.broker.placed, p.last_action
        await p.tick(100.0, BOOK)
        assert p.position and p.broker_refusal is None and p._rights_n == 0

    run(scenario())


# ── 3. отказы биржи: закрытие с паузой 2→4→… с, ПАНИКА не чаще PANIC_RETRY_SEC, причина видна ────────────────
def test_close_refusal_is_paced_not_every_tick_and_keeps_decision(tk):
    async def scenario():
        p = make_pilot(position=True)
        p.broker.refuse = {"error": "Tinkoff 400 · 30042: Not enough assets", "err_kind": "other", "err_code": "30042"}
        assert p.adopt_forecast({"exec": {"do": "CLOSE", "why": "т"}})
        await p.tick(100.0, BOOK)
        pos = p.position
        assert pos and pos["close_refusals"] == 1 and 1.0 < pos["close_retry_at"] - time.time() <= 2.0
        assert p.broker_refusal["what"] == "close" and "недостаточно средств" in p.broker_refusal["text"]
        for _ in range(10):
            await p.tick(100.0, BOOK)
        assert p.broker.attempts == 1 and "повтор через" in p.last_action and "решение закрыть не потеряно" in p.last_action
        assert p.status()["close_retry_in_s"] >= 1
        pos["close_retry_at"] = time.time() - 1
        await p.tick(100.0, BOOK)
        assert p.broker.attempts == 2 and pos["close_refusals"] == 2 and 3.0 < pos["close_retry_at"] - time.time() <= 4.0
        # ПАНИКА: нажатие — попытка сразу, дальше не чаще PANIC_RETRY_SEC
        p.panic()
        await p.tick(100.0, BOOK)
        assert p.broker.attempts == 3 and pos["close_retry_at"] - time.time() > ai_pilot.PANIC_RETRY_SEC - 1
        for _ in range(5):
            await p.tick(100.0, BOOK)
        assert p.broker.attempts == 3 and p.last_action.startswith("ПАНИКА владельца — закрытие отбито биржей")
        p.broker.refuse = None
        pos["close_retry_at"] = time.time() - 1
        await p.tick(100.0, BOOK)
        assert p.position is None and p.state == "СТОП" and p.broker_refusal is None

    run(scenario())


def test_entry_backoff_text_keeps_reason_and_lost_response_is_honest(tk):
    async def scenario():
        p = make_pilot()
        p.broker.refuse = {"error": "Tinkoff 400 · 30042: Not enough assets", "err_kind": "other", "err_code": "30042"}
        p.adopt_forecast({"exec": {"do": "BUY", "entry": None, "take": 110.0, "invalidation": 98.0, "why": "т"}})
        await p.tick(100.0, BOOK)
        assert p._entry_fail == 1 and "бэкофф, попытка 1" in p.last_action
        await p.tick(100.0, BOOK)
        assert p.last_action.startswith("бэкофф после отказа биржи (") and "30042" in p.last_action, p.last_action
        # ответ PostOrder потерян: не «бью агрессивной лимиткой», а «отправлена, ответ потерян»
        p.broker.refuse, p.broker.uncertain = None, True
        p.no_entry_until = 0.0
        p.broker.state_fail = {"error": "нет связи с Т-Банком (таймаут)", "err_kind": "network"}
        await p.tick(100.0, BOOK)
        assert p.pending and "ответ биржи потерян" in p.last_action and "бью" not in p.last_action, p.last_action

    run(scenario())


def test_lost_response_poll_is_paced_up_to_30s(tk):
    async def scenario():
        p = make_pilot()
        p.pending = {"order_id": "req-1", "request_id": "req-1", "id_type": "request", "side": "long", "lots": 5,
                     "price": 100.0, "ts": time.time(), "attempts": 0, "take": 110.0, "invalidation": 98.0}
        calls = []

        async def state(oid, **kw):
            calls.append(oid)
            return {"ok": False, "error": "read timed out", "err_kind": "network"}
        p.broker.order_state = state
        await p._pending_tick(100.0, BOOK)                  # первый сбой — повтор следующим тиком
        await p._pending_tick(100.0, BOOK)                  # второй — пауза 2 с
        assert len(calls) == 2 and 1.0 < p.pending["poll_at"] - time.time() <= 2.0
        for _ in range(5):
            await p._pending_tick(100.0, BOOK)
        assert len(calls) == 2 and "следующий опрос через" in p.last_action and p.pending is not None
        for k in range(8):
            p.pending["poll_at"] = 0.0
            await p._pending_tick(100.0, BOOK)
        assert p.pending["poll_at"] - time.time() <= ai_pilot.POLL_RETRY_MAX_SEC + 0.5

    run(scenario())


def test_zero_lots_reason_is_named_without_endless_retries(tk):
    async def scenario():
        p = make_pilot()
        p.broker.mx = {"buy": 0, "sell": 195}
        p.adopt_forecast({"exec": {"do": "BUY", "entry": None, "take": 110.0, "invalidation": 98.0, "why": "т"}})
        await p.tick(100.0, BOOK)
        assert p.plan is None and not p.broker.placed
        assert p.last_action.startswith("депозит не тянет ни лота: покупка недоступна: маржа исчерпана, GetMaxLots 0 лотов")
        assert p.broker_refusal["code"] == "GetMaxLots=0" and p.account_limits["buy_lots"] == 0
        assert "покупка недоступна" in p.account_limits["note"] and p.account_limits["sell_lots"] == 195

    run(scenario())


def test_account_limits_refresh_once_a_minute_and_dry_note(tk):
    async def scenario():
        p = make_pilot(position=True)
        p.broker.mx = {"buy": 3, "sell": 11}
        await p.tick(100.0, BOOK)
        lim = p.status()["account_limits"]
        assert lim["buy_lots"] == 3 and lim["sell_lots"] == 11 and "купить 3 / продать 11" in lim["note"], lim
        n = p.broker.mx_calls
        for _ in range(5):
            await p.tick(100.0, BOOK)
        assert p.broker.mx_calls == n, "не чаще раза в LIMITS_EVERY_SEC"
        d = make_pilot()
        d.broker.mode = "dry"
        d.broker.max_lots = AsyncMock(return_value=None)
        await d._refresh_limits(100.0, force=True)
        assert d.account_limits["buy_lots"] is None and "режим dry" in d.account_limits["note"]

    run(scenario())


# ── 4. ЗАКРЫТЬ не теряется за ужатием (D7 S8); добор совета у уровня взводится (D5); killswitch — одна запись (D4) ──
def test_close_during_reduce_is_queued_and_executed_next_tick(tk):
    async def scenario():
        p = make_pilot(position=True)
        p.position["closing"] = "reduce"                   # маржевой дозор ужимает позицию
        assert await p._close_all(100.0, "решение перепроверки: закрыть") is False
        assert p._close_pending == "решение перепроверки: закрыть" and "в очереди" in p.last_action
        p.position.pop("closing")
        await p.tick(100.0, BOOK)
        assert p.position is None and p.broker.placed[-1]["tag"] == "aip-close"
        # «закрытие уже идёт» (флаг держит другое закрытие) — второй ордер не шлём, как прежде
        q = make_pilot(position=True)
        q.position["closing"] = "close"
        assert await q._close_all(100.0, "дубль") is False and q._close_pending is None and not q.broker.placed

    run(scenario())


def test_council_same_side_level_arms_addon_plan_and_drops_it_on_close(tk):
    async def scenario():
        p = make_pilot(position=True, lots=5)
        p.prices.append(100.5)
        ok = p.adopt_forecast({"exec": {"do": "BUY", "entry": 99.0, "take": 108.0, "invalidation": 97.5, "why": "добрать на откате к 99"}})
        assert ok and p.plan and p.plan["entry"] == 99.0 and p.plan["add_to"] == "long", p.plan
        assert p.position["invalidation"] == 97.5 and p.position["take"] == 108.0 and "добор взведён" in p.last_action
        await p.tick(100.5, BOOK)
        assert not p.broker.placed and "добор" in p.last_action
        await p.tick(99.02, {"best_bid": 98.99, "best_ask": 99.05})
        assert p.broker.placed and p.broker.placed[-1]["tag"] == "aip-entry" and p.pending["topup"], p.last_action
        # позиция закрыта — план добора к ней не превращается в новый вход
        r = make_pilot(position=True)
        r.adopt_forecast({"exec": {"do": "BUY", "entry": 99.0, "take": 108.0, "invalidation": 97.5, "why": "т"}})
        r._finish_closed(r.position, "тест", reanalyze=False)
        assert r.plan is None

    run(scenario())


def test_killswitch_announced_once_with_hook_and_refusal_reason(tk):
    async def scenario():
        p = make_pilot()
        hits = []
        p._on_killswitch = lambda info: hits.append(info)
        p.session_risk.locked, p.session_risk.reason = True, "серия убытков 3 подряд — стоп сессии"
        await p.tick(100.0, BOOK)
        assert len(hits) == 1 and hits[0]["reason"].startswith("серия убытков") and p.state == "СТОП"
        assert "KILLSWITCH: серия убытков 3 подряд" in p.last_action, p.last_action
        la = p.last_action
        await p.tick(100.0, BOOK)
        assert len(hits) == 1 and p.last_action == la, "одна запись — не каждый тик заново"
        assert p.adopt_forecast({"exec": {"do": "BUY", "entry": None, "take": 110.0, "invalidation": 98.0}}) is False
        assert "killswitch" in p.adopt_refused and "серия убытков" in p.adopt_refused
        assert p.killswitch_reason().startswith("серия убытков")
        p.session_risk = trader_risk.SessionRisk(100000.0)
        assert p.adopt_forecast({"exec": {"do": "BUY", "entry": None, "take": 110.0, "invalidation": 98.0}})
        assert p.adopt_refused is None

    run(scenario())


# ── 5. prepare(): непрочитанный портфель ≠ 0 лотов; счёт не прочитан — отказ с причиной, файл цел ───────────────
def _state_file(tmp_path, lots=3):
    path = tmp_path / "aipilot_state.json"
    path.write_text(json.dumps({
        "figi": "FIGI-T", "base": "TEST", "ts": time.time(), "account_id": "acc-1", "mode": "real", "pending": None,
        "foreign_lots": 0, "close_pending": None,
        "position": {"side": "long", "entry": 100.0, "lots": lots, "take": 110.0, "invalidation": 98.0, "inv0": 98.0,
                     "opened_ts": time.time() - 3600, "stop_id": None, "holds": 0}}), encoding="utf-8")
    return path


def _fresh(path):
    p = ai_pilot.AIPilot("TEST", deposit=100000.0, broker=Broker())
    p._state_path = path
    p.asset_class = "futures"
    return p


def test_prepare_unread_portfolio_keeps_state_file_and_verifies_later(tk, tmp_path, monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "backend.instruments", SimpleNamespace(get=lambda t: {}))

    async def scenario():
        path = _state_file(tmp_path)
        tk.pf = None                                        # GetPortfolio не ответил
        p = _fresh(path)
        assert await p.prepare() is True and path.exists()
        assert p.position["lots"] == 3 and p.position["unverified"] and p._acct_unverified
        assert "СВЕРИТЬ СО СЧЁТОМ" in p.last_action and p.status()["account_unverified"] is True
        # входа и добора до сверки нет
        assert p.adopt_forecast({"exec": {"do": "BUY", "entry": 99.9, "take": 110.0, "invalidation": 98.0}})   # добор у уровня
        p.broker.pf_error = "GetPortfolio: 500"
        p._tick_n = 1
        await p.tick(99.95, BOOK)
        assert not p.broker.placed and "ждёт сверки" in p.last_action
        # первая удачная сверка: на счёте 3 лота — подтверждено
        p.broker.pf_error, p.broker.pf = None, [{"figi": "FIGI-T", "qty": 3, "avg": 100.0}]
        await p._reconcile(100.0)
        assert not p._acct_unverified and not p.position.get("unverified") and "подтверждена" in p.last_action
        # другой прогон: на счёте пусто — позиция закрыта вне программы, P/L не выдумываем
        path2 = _state_file(tmp_path, lots=2)
        q = _fresh(path2)
        tk.pf = None
        assert await q.prepare() is True
        q.broker.pf = []
        await q._reconcile(100.0)
        assert q.position is None and q.pnls == [] and "P/L неизвестен" in q.last_action and not path2.exists()

    run(scenario())


def test_prepare_refuses_with_reason_when_accounts_unread_and_keeps_file(tk, tmp_path):
    async def scenario():
        path = _state_file(tmp_path)
        tk.accs = None                                      # 401: счёт не прочитан
        p = _fresh(path)
        told = []
        p.on_prepared = lambda ok, why: told.append((ok, why))
        await p._run_loop()
        assert p.prepare_error and "счёт Т-Банка не прочитан" in p.prepare_error and "40003" in p.prepare_error
        assert told == [(False, p.prepare_error)] and p.loop_alive is False and path.exists()
        # портфель не прочитан и денег не узнать — отказ с причиной, файл цел
        tk.accs, tk.pf = [{"id": "acc-1"}], None
        r = ai_pilot.AIPilot("TEST", deposit=None, broker=Broker())
        r._state_path = path
        assert await r.prepare() is False and "портфель счёта не прочитан" in r.prepare_error and path.exists()

    run(scenario())


# ── 6. миссия: тихая смерть, зомби на «продолжить», паника при 401, позиция на счёте «неизвестна» ──────────────
def test_mission_pilot_failure_is_reported_not_silent(monkeypatch):
    persisted = []
    monkeypatch.setattr(mission, "_persist", lambda m: persisted.append(m.phase))
    monkeypatch.setattr(mission, "_bg", lambda coro: coro.close())
    m = mission.Mission("TEST", "Тест", "futures", "auto")
    mission._M["TEST"] = m
    try:
        p = mission.MissionPilot("TEST", deposit=None, broker=Broker(), mission=m)
        p._state_path = None
        m.pilot = p
        m.note = "пилот запускается (real): готовлю счёт и контракт; план принят"
        mission._pilot_prepared(m, p, True, "")
        assert m.note == "пилот запущен (real): план принят" and m.error is None
        mission._pilot_prepared(m, p, False, "счёт Т-Банка не прочитан: токен не принят (40003)")
        assert m.phase == "error" and m.error.startswith("пилот не стартовал: счёт Т-Банка не прочитан")
        # задача пилота закончилась без исключения, а prepare отказался — это ошибка, не «остановлен»
        m.error, m.phase = None, "entering"
        p.prepare_error = "портфель счёта не прочитан"

        class T:
            def cancelled(self):
                return False

            def exception(self):
                return None
        t = T()
        m.task = t
        mission._pilot_done(m, t)
        assert m.phase == "error" and "портфель счёта не прочитан" in m.error and m.note.startswith("пилот не стартовал")
    finally:
        mission._M.pop("TEST", None)


def test_resume_on_zombie_pilot_tells_the_truth():
    p = mission.MissionPilot("TEST", deposit=None, broker=Broker(), mission=None)
    p._state_path = None
    p.loop_alive = True
    p.feed = {"ok": False, "kind": "auth", "reason": AUTH["reason"], "since": time.time() - 600}
    r = mission._alive_note(p)
    assert not r["ok"] and "не работает" in r["note"] and "40003" in r["note"] and "10 мин" in r["note"]
    p.feed = {"ok": True, "kind": "ok", "reason": "", "since": None}
    p.last_tick_ts = time.time() - 300
    r = mission._alive_note(p)
    assert not r["ok"] and "не тикает 300 с" in r["note"]
    p.last_tick_ts = time.time()
    assert mission._alive_note(p)["ok"] and "уже работает" in mission._alive_note(p)["note"]
    p.loop_alive = False
    assert "готовится" in mission._alive_note(p)["note"]


def test_panic_and_account_position_texts_on_401(monkeypatch):
    async def scenario():
        monkeypatch.setattr(mission.tinkoff, "enabled", lambda: True)
        monkeypatch.setattr(mission.tinkoff, "accounts", AsyncMock(return_value=None))
        monkeypatch.setattr(mission.tinkoff, "failure_text", lambda default="": AUTH["reason"])
        monkeypatch.setattr(mission, "_make_broker", lambda: trader_broker.Broker("dry"))
        m = mission.Mission("TEST", "Тест", "futures", "auto")
        r = await mission._flat_account(m)
        assert not r["ok"] and "токен Т-Банка не принят" in r["note"] and "не найден" not in r["note"], r
        txt = await mission._account_position_text(m)
        assert txt.startswith("ПОЗИЦИЯ НА СЧЁТЕ НЕИЗВЕСТНА") and "40003" in txt, txt
        monkeypatch.setattr(mission.tinkoff, "accounts", AsyncMock(return_value=[{"id": "acc-1"}]))
        monkeypatch.setattr(mission.tinkoff, "portfolio", AsyncMock(return_value=None))
        assert "портфель не прочитан" in await mission._account_position_text(m)

    run(scenario())


def test_phase_is_honest_when_link_is_down():
    m = mission.Mission("TEST", "Тест", "futures", "auto")
    p = mission.MissionPilot("TEST", deposit=None, broker=Broker(), mission=m)
    p._state_path = None
    m.pilot = p
    loop = asyncio.new_event_loop()
    try:
        m.task = loop.create_future()                       # «задача жива»
        p.pending = {"order_id": "x"}
        p.state = "НЕТ_СВЯЗИ"
        assert mission._phase(m) == "no_link"
        p.state = "НЕТ_ДОСТУПА"
        assert mission._phase(m) == "no_access"
    finally:
        loop.close()


# ── 7. Т-Банк: два домена — переключение только когда запрос точно не ушёл; коды 40003/40002 с подсказкой ──────
def test_tinkoff_failover_never_repeats_orders_on_lost_response(monkeypatch):
    async def scenario():
        hits = []
        mode = {}

        def handler(req):
            host, meth = req.url.host, req.url.path.rsplit("/", 1)[-1]
            hits.append((host, meth))
            act = mode.get(host)
            if act == "connect":
                raise httpx.ConnectError("DNS failure", request=req)
            if act == "read":
                raise httpx.ReadTimeout("read timed out", request=req)
            if act == 502:
                return httpx.Response(502, json={"code": 14, "message": "", "description": "bad gateway"})
            return httpx.Response(200, json={"lastPrices": [], "orderId": "E-1"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(tinkoff, "_http", client)
        monkeypatch.setattr(tinkoff.config, "TINKOFF_TOKEN", "t.fake")
        monkeypatch.delenv("PYTHIA_TINKOFF_BASE", raising=False)
        tb, tk_ = tinkoff._host(tinkoff.BASES[0]), tinkoff._host(tinkoff.BASES[1])
        post = "tinkoff.public.invest.api.contract.v1.OrdersService/PostOrder"
        try:
            for act, method, both in (("connect", post, True), ("read", post, False), (502, post, False),
                                      ("read", f"{tinkoff.MD}/GetLastPrices", True), (502, f"{tinkoff.MD}/GetLastPrices", True)):
                monkeypatch.setattr(tinkoff, "BASE", tinkoff.BASES[0])
                hits.clear()
                mode.clear()
                mode[tb] = act
                try:
                    await tinkoff._post(method, {})
                except Exception:                            # noqa: BLE001
                    pass
                assert [h for h, _ in hits] == ([tb, tk_] if both else [tb]), (act, method, hits)
        finally:
            await client.aclose()
            tinkoff.reset_errors()

    run(scenario())


def test_tinkoff_error_texts_carry_code_and_hint():
    e = tinkoff.TinkoffError(401, "16", "40003", "Authentication token is missing or invalid", "x/PostOrder")
    assert "401 · 40003" in str(e) and "токен Т-Банка не принят" in tinkoff.humanize_api_error(e)
    assert tinkoff.classify(e)[0] == "auth"
    e2 = tinkoff.TinkoffError(403, "7", "40002", "Insufficient privileges", "x/PostOrder")
    assert tinkoff.classify(e2)[0] == "rights" and "нет прав" in tinkoff.humanize_api_error(e2)
    rec = tinkoff.note_error(e2, "tinkoff.public.invest.api.contract.v1.OrdersService/PostOrder")
    assert rec["code"] == "40002" and rec["source"] == "orders" and rec["kind"] == "rights"
    tinkoff.reset_errors()


# ── 8. панель проблем и Telegram ───────────────────────────────────────────────────────────────────────────────
def test_health_link_problems_and_no_false_dead_market(monkeypatch):
    now = time.time()
    monkeypatch.setattr(tinkoff, "errors", lambda: {})
    monkeypatch.setattr(tinkoff, "last_error", lambda: None)
    monkeypatch.setattr(api_v5.ai_v5, "last_error", lambda: None)      # чужие ошибки DeepSeek прошлых тестов — мимо
    monkeypatch.setattr(api_v5.astro, "peek_context", lambda: (None, None))
    pst = {"position": {"side": "long", "lots": 1950, "stop_id": None}, "killswitch": {"locked": False},
           "price_ts": now - 900, "market_alive": False, "loop_alive": True, "ticking": False,
           "feed": {"ok": False, "kind": "rights", "reason": "у токена нет прав (40002)", "since": now - 120}}
    snap = {"active": "AFLT", "missions": {"AFLT": {"ticker": "AFLT", "live": True, "pilot": pst, "error": None}}}
    h = api_v5.health_block(keys={"deepseek": True, "tinkoff": True, "dry": False}, mission_snap=snap,
                            market={"open": True, "enabled": True}, now=now)
    txt = " | ".join(x["text"] for x in h["problems"])
    assert [(x["key"], x["level"]) for x in h["problems"]] == [("pilot:feed", "err")], h["problems"]
    assert "нет доступа" in txt and "1950 лот БЕЗ ЗАЩИТЫ" in txt and "мёртв" not in txt and "старой цене" not in txt


def test_telegram_pilot_done_is_not_a_stop_and_words(monkeypatch):
    got = []
    monkeypatch.setattr(telegram, "bound", lambda: True)
    monkeypatch.setattr(telegram, "notify", lambda node, line, silent=False: got.append(line))
    monkeypatch.setattr(telegram, "_snapshot", lambda: {"active": "SBER", "missions": {"SBER": {
        "ticker": "SBER", "phase": "entering", "pilot": {"loop_alive": False}}}})

    async def scenario():
        await telegram.on_event({"type": "v5", "scope": "mission", "stage": "pilot", "status": "done", "ticker": "SBER",
                                 "detail": "пилот запускается (real): готовлю счёт и контракт; план принят"})
        assert got == []
        await telegram.on_event({"type": "v5", "scope": "mission", "stage": "pilot", "status": "progress", "ticker": "SBER",
                                 "detail": "НЕТ ДОСТУПА к брокеру: токен Т-Банка не принят (40003)"})
        await telegram.on_event({"type": "v5", "scope": "mission", "stage": "pilot", "status": "progress", "ticker": "SBER",
                                 "detail": "цена 95 уже за invalidation 96 — идея мертва ДО входа"})
        await telegram.on_event({"type": "v5", "scope": "mission", "stage": "entry", "status": "done", "ticker": "SBER",
                                 "detail": "PRO у двери: ЖДАТЬ — откат к 299.5"})
        assert len(got) == 3 and "НЕТ ДОСТУПА" in got[0] and "мертва" in got[1] and "Дверь SBER" in got[2], got

    run(scenario())
