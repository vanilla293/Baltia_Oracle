# -*- coding: utf-8 -*-
"""ПИФИЯ v5 — СЦЕНАРНЫЙ СТЕНД биржевых ситуаций (фаза 2, W1).

Воля владельца: «пройди все возможные пласты ситуаций на бирже, какие только могут быть, чтобы ИИ
полностью и трезво отыгрывал свою роль». Стенд — детерминированная сцена поверх настоящих
`mission.Mission` + `mission.MissionPilot` (петля `tick`, перепроверка PRO, FLASH у троса, толмач,
совет `council_again`, журнал сделок), где всё внешнее подменено скриптованными фейками:

- `FakeBroker` — биржа: исполнить / частично / отбить с текстом 30042 / не поставить стоп / тянуть
  закрытие; портфель «зеркалит» свои исполнения (или задаётся сценой — докупил/продал владелец);
- `FakeTinkoff` — лента: цена, стакан, «нет котировок», стакан не отдаётся;
- `FakeAI` — скриптованные ответы по маршрутам (mission_exec / mission_review / mission_guard /
  mission_take / event_triage / mission_entry / mission_profit / explain / memory / scout / shrink) с подсчётом
  вызовов по маршруту и модели (FLASH / PRO; узлы у денег — через `money_json` по PYTHIA_MONEY_MODEL, v5.4.1),
  «молчание» (таймаут) по маршруту;
- `FakeClock` — рыночные часы (открыто / закрыто / клиринг), `FakeWatch` — серьёзные заметки дозора.

Каждый сценарий — функция с настоящими `assert`, коротким именем и человеческим описанием
(docstring). Стенд собирает по сценарию: результат, сделанные ордера, вызовы ИИ по маршрутам,
последнее действие пилота и объяснение толмача. `run_all()` → отчёт-словарь и текстовая таблица.

Ничего в сеть и ни одного настоящего ключа: ИИ и биржа — фейки, база — временный файл, data/ не
трогается (state-файл пилота у сцен выключен, кроме сцены рестарта — там временный файл).
Поведение пилота детерминировано (тики — руками, без петли); только счётчики фоновых вызовов
толмача/памяти (explain, memory) могут отличаться на ±1 от прогона к прогону из-за окон дебаунса —
ни один assert на них не опирается.

Запуск: python3 -m backend.scenarios   (PYTHIA_SCEN_VERBOSE=1 — с логом пилота)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import (ai_pilot, ai_v5, bus, compress, config, correlate, explain, market_ctx, mission,
               scout, store_v5, trader_broker, trader_risk)
from .trader_broker import BUY, SELL, STOP_BUY, STOP_SELL

log = logging.getLogger("pythia.scenarios")

TICKER = "TEST"
FIGI = "FIGI-T"
PRICE0 = 100.0                      # цена сцены по умолчанию
GO = 12000.0                        # ГО фьючерса: 100 000 · 0.98 // 12 000 = 8 лотов
DEPOSIT = 100000.0
_real_ai_v5 = ai_v5


def MM() -> str:
    """Имя модели узлов у денег для текстов сцен: «PRO» (умолчание 5.4.1) или «FLASH» (PYTHIA_MONEY_MODEL)."""
    return str(_real_ai_v5.money_model()).upper()


def _f(x, d=0.0) -> float:
    try:
        return float(x) if x is not None else d
    except (TypeError, ValueError):
        return d


def book(price: float, spread: float = 0.1) -> dict:
    return {"best_bid": round(price - spread, 4), "best_ask": round(price + spread, 4)}


# ══════════════════════════════════════════════════════════════════════════════
# Фейки
# ══════════════════════════════════════════════════════════════════════════════
class FakeBroker:
    """Биржа со сценарным поведением. Портфель зеркалит собственные исполнения (`net`), пока сцена
    не подменит его руками (`pf_positions`) — так проверяются докупки/продажи владельца и лаг."""
    mode = "real"

    def __init__(self):
        self.placed: list[dict] = []          # все заявки (входы, доборы, закрытия)
        self.stops: list[dict] = []           # выставленные стоп-заявки
        self.stop_cancels: list[str] = []
        self.cancelled: list[str] = []
        self.flat_calls: list = []
        self.fill_next = True                 # place → FILL при первом опросе
        self.partial: dict[str, int] = {}     # order_id → налито лотов (PARTIAL)
        self.place_error: str | None = None   # биржа отбивает place этим текстом (None — принимает)
        self.reject_first_n = 0               # отбить только первые N заявок (0 — все, пока place_error)
        self.stop_ok = True                   # стоп-заявка ставится?
        self.stop_error = "30079: инструмент недоступен для торгов (тест)"
        self.cancel_stop_ok = True
        self.cancel_stop_error = "stop already executed"
        self.stop_status: dict[str, str] = {} # stop_order_id → active | cancelled | executed (для списка стопов)
        self.stops_fail = False               # GetStopOrders недоступен (strict → исключение, иначе [])
        self.orders_fail = False              # GetOrders недоступен
        self.market_pending = False           # рыночная заявка принята, но НЕ исполняется (висит NEW), пока сцена не решит
        self.flat_kwargs: list[dict] = []     # с чем звали flat_all (force / гейты)
        self.close_delay = 0.0                # тянуть закрытие (гонка двух задач)
        self.pf_positions: list | None = None # None → зеркало своих исполнений
        self.pf_error: str | None = None      # v5.4.4: портфель брокера не читается (сверка откладывается)
        self.pf_cash: float | None = None
        self.mx: dict | None = None           # ответ GetMaxLots {"buy","sell"} (None → локальный расчёт)
        self.mx_calls = 0
        self.net = 0                          # лоты на счёте по FIGI (зеркало)
        self.avg = 0.0
        self.account_id = None
        self._n = 0
        self._applied: set[str] = set()
        self._orders: dict[str, dict] = {}

    # ── учёт исполнений (зеркало счёта) ──
    def _apply(self, oid: str, lots: int) -> None:
        o = self._orders.get(oid) or {}
        if oid in self._applied or lots <= 0 or not o:
            return
        self._applied.add(oid)
        sgn = 1 if o["direction"] == BUY else -1
        px = _f(o.get("price")) or _f(o.get("mark"))
        new = self.net + sgn * lots
        if self.net == 0 or (self.net > 0) != (new > 0) and new != 0:
            self.avg = px
        elif (new > 0) == (self.net > 0) and abs(new) > abs(self.net):
            self.avg = (self.avg * abs(self.net) + px * lots) / max(1, abs(new))
        self.net = new

    async def max_lots(self, figi, price=None):
        self.mx_calls += 1
        return dict(self.mx) if self.mx else None

    async def place(self, figi, direction, lots, price=None, tag=""):
        if self.place_error and (self.reject_first_n <= 0 or self._n < self.reject_first_n):
            self._n += 1
            return {"ok": False, "error": self.place_error, "note": self.place_error}
        self._n += 1
        oid = f"F-{self._n}"
        rec = {"order_id": oid, "figi": figi, "direction": direction, "lots": int(lots),
               "price": price, "tag": tag, "mark": PRICE0}
        self.placed.append(rec)
        self._orders[oid] = rec
        if tag == "aip-close" and self.close_delay > 0:
            await asyncio.sleep(self.close_delay)
        if price is None and not self.market_pending:   # рыночный: исполнен сразу (в place) — и отчитывается FILL
            self._apply(oid, int(lots))
            rec["done"] = True
        return {"ok": True, "order_id": oid}

    async def order_state(self, oid):
        o = self._orders.get(oid) or {}
        if o.get("cancelled"):                # сцена сняла заявку (биржа/владелец): терминал с тем, что налито
            return {"ok": True, "filled": False, "status": "CANCELLED", "exec_lots": int(self.partial.get(oid) or 0)}
        if oid in self.partial:
            self._apply(oid, int(self.partial[oid]))
            return {"ok": True, "filled": False, "status": "PARTIAL", "exec_lots": self.partial[oid]}
        if o.get("done"):                     # W4 (W1): рыночная, исполненная в place, — FILL, а не NEW;
            return {"ok": True, "filled": True, "status": "FILL", "exec_lots": o["lots"]}   # fill_next — лишь задержка лимитки входа
        if o and o.get("price") is None and self.market_pending:     # рыночная принята, но висит (аукцион/пауза)
            return {"ok": True, "filled": False, "status": "NEW", "exec_lots": 0}
        if self.fill_next:
            self._apply(oid, int(o.get("lots") or 0))
            return {"ok": True, "filled": True, "status": "FILL"}
        return {"ok": True, "filled": False, "status": "NEW", "exec_lots": 0}

    async def cancel(self, oid):
        self.cancelled.append(oid)
        return {"ok": True}

    async def place_stop(self, figi, direction, lots, stop_price, tag=""):
        if not self.stop_ok:
            return {"ok": False, "error": self.stop_error, "note": self.stop_error}
        self.stops.append({"figi": figi, "direction": direction, "lots": int(lots), "stop": stop_price,
                           "created": time.time()})
        sid = f"S-{len(self.stops)}"
        self.stop_status[sid] = "active"
        return {"ok": True, "stop_order_id": sid}

    async def cancel_stop(self, sid):
        self.stop_cancels.append(sid)
        if not self.cancel_stop_ok:
            return {"ok": False, "error": self.cancel_stop_error}
        self.stop_status[sid] = "cancelled"
        return {"ok": True}

    async def stop_orders(self, *, strict=False):
        """GetStopOrders биржи: активные стопы в формате T-Invest (stopOrderId, direction, lotsRequested, stopPrice
        {units, nano}, createDate RFC3339, status). stops_fail → strict поднимает исключение (список НЕ получен)."""
        if self.stops_fail:
            if strict:
                raise RuntimeError("GetStopOrders: 503 сервис недоступен (тест)")
            return []
        out = []
        for i, s in enumerate(self.stops):
            sid = f"S-{i + 1}"
            if self.stop_status.get(sid, "active") != "active":
                continue
            px = float(s["stop"])
            out.append({"stopOrderId": sid, "figi": s["figi"], "instrumentUid": s["figi"],
                        "direction": STOP_SELL if s["direction"] == SELL else STOP_BUY,
                        "lotsRequested": str(int(s["lots"])),
                        "stopPrice": {"units": int(px), "nano": int(round((px - int(px)) * 1e9)), "currency": "rub"},
                        "createDate": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(s.get("created") or time.time()))
                                      + f".{int((s.get('created') or 0) % 1 * 1e6):06d}Z",
                        "status": "STOP_ORDER_STATUS_ACTIVE"})
        return out

    async def orders(self, *, strict=False):
        """GetOrders биржи: активные (неисполненные, не снятые) заявки."""
        if self.orders_fail:
            if strict:
                raise RuntimeError("GetOrders: 503 (тест)")
            return []
        return [{"orderId": oid, "figi": o["figi"], "executionReportStatus": "EXECUTION_REPORT_STATUS_NEW",
                 "lotsRequested": str(o["lots"]), "lotsExecuted": str(int(self.partial.get(oid) or 0))}
                for oid, o in self._orders.items()
                if not o.get("done") and not o.get("cancelled") and oid not in self._applied and oid not in self.cancelled]

    async def portfolio(self):
        if self.pf_error:
            return {"cash": None, "positions": [], "error": self.pf_error}
        if self.pf_positions is not None:
            poss = list(self.pf_positions)
        else:
            poss = [{"figi": FIGI, "qty": self.net, "avg": self.avg}] if self.net else []
        return {"mode": "real", "cash": self.pf_cash, "positions": poss}

    async def flat_all(self, positions=None, figi=None, lot=1, **kwargs):
        self.flat_calls.append((figi, lot))
        self.flat_kwargs.append(dict(kwargs))
        if kwargs.get("on_check") is not None:
            kwargs["on_check"]()                  # гейт персиста — как у настоящего брокера, до любого I/O
        return {"ok": True, "closed": 1, "stops": 1, "orders": 0, "warnings": [], "note": None}

    # ── сводка для отчёта ──
    def orders_text(self) -> list[str]:
        return [f"{x['tag']} {'BUY' if x['direction'] == BUY else 'SELL'} {x['lots']}"
                + (f"@{_f(x['price']):g}" if x.get("price") is not None else "@рынок") for x in self.placed]


class FakeTinkoff:
    """Лента и счёт без сети: цена (None — нет котировок), стакан (fail — не отдаётся).
    v5.4.4: почему цены нет (price_err → price_error), проверка токена (access → check_access; None — полный доступ),
    эпоха токена (epoch — новый токен в «Ключах»), счёт/портфель не читаются (accs_fail / pf_fail), ГО фьючерса."""
    price: float | None = PRICE0
    on = True
    book_fail = False
    positions: list = []
    price_err: dict | None = None
    access: dict | None = None
    epoch = 0
    checks = 0
    accs_fail = False
    pf_fail = False

    @classmethod
    def enabled(cls):
        return cls.on

    @classmethod
    async def accounts(cls):
        return None if cls.accs_fail else [{"id": "acc-1"}]

    @classmethod
    async def portfolio(cls, acc):
        return None if cls.pf_fail else {"positions": list(cls.positions), "total": DEPOSIT}

    @classmethod
    def price_error(cls, figi):
        return dict(cls.price_err) if (cls.price is None and cls.price_err) else None

    @classmethod
    async def check_access(cls, timeout=15.0):
        cls.checks += 1
        return dict(cls.access) if cls.access else {"ok": True, "trade": True, "kind": "ok", "access": "FULL_ACCESS",
                                                     "reason": "токен принят: полный доступ"}

    @classmethod
    def token_epoch(cls):
        return cls.epoch

    @classmethod
    async def futures_margin(cls, figi):
        return {"margin_buy": GO, "margin_sell": GO, "min_price_increment": 0.01, "min_price_increment_amount": 0.01}

    @classmethod
    async def resolve(cls, ticker, ac):
        return {"figi": FIGI, "lot": 1}

    @classmethod
    async def close_price(cls, figi):
        return None

    @classmethod
    async def last_price(cls, figi):
        return {"price": cls.price}

    @classmethod
    async def orderbook(cls, figi, depth=10):
        if cls.book_fail or cls.price is None:
            raise RuntimeError("стакан недоступен (тест)")
        return book(cls.price)


class FakeAI:
    """Скриптованный ИИ: ответы по маршрутам, счётчик вызовов по маршруту и модели, «молчание»."""
    PRO_ROUTES = ("mission_analysis", "mission_critique", "mission_verdict", "mission_exec", "mission_review")
    SILENT = "SILENT"

    def __init__(self):
        self.answers: dict[str, list] = {}    # route → очередь ответов
        self.calls: list[tuple[str, str]] = []  # (модель, маршрут)
        self.last_user: dict[str, str] = {}
        self.silent_sleep = 30.0
        self.errors: list[tuple[str, str]] = []   # note_error (панель проблем)
        self.gate: asyncio.Event | None = None    # задан → PRO-стадии совета ждут gate.set() (сцена проверяет промежуточное)
        self.now_msk_str = _real_ai_v5.now_msk_str
        self.fmt_ts = _real_ai_v5.fmt_ts

    def reset(self):
        self.answers.clear()
        self.calls.clear()
        self.last_user.clear()
        self.errors.clear()
        self.gate = None

    @staticmethod
    def money_model() -> str:
        return _real_ai_v5.money_model()

    def note_error(self, text: str, route: str = "") -> None:
        self.errors.append((route, text))

    def queue(self, route: str, *answers) -> None:
        self.answers.setdefault(route, []).extend(answers)

    def _next(self, route: str, default):
        q = self.answers.get(route) or []
        return q.pop(0) if q else default

    def count(self, route: str) -> int:
        return sum(1 for _, r in self.calls if r == route)

    def summary(self) -> dict:
        routes: dict[str, int] = {}
        for _, r in self.calls:
            routes[r] = routes.get(r, 0) + 1
        return {"FLASH": sum(1 for mdl, _ in self.calls if mdl == "FLASH"),
                "PRO": sum(1 for mdl, _ in self.calls if mdl == "PRO"), "routes": routes}

    async def _silent(self):
        await asyncio.sleep(self.silent_sleep)
        raise asyncio.TimeoutError()

    async def pro_stream(self, system, user, *, on_think=None, on_text=None, route="pro"):
        if self.gate is not None:
            await self.gate.wait()
        self.calls.append(("PRO", route))
        self.last_user[route] = user
        if on_think:
            await on_think("думаю…")
        if on_text:
            await on_text("текст " + route)
        return "текст " + route

    async def pro_json(self, system, user, *, route="pro", max_tokens=None):
        self.calls.append(("PRO", route))
        self.last_user[route] = user
        if route == "mission_exec":
            a = self._next(route, {"do": "BUY", "entry": None, "take": 110.0, "invalidation": 98.0,
                                   "why": "стенд: приказ по умолчанию"})
        else:
            a = self._next(route, {"choice": "ЖДЁМ", "why": "стенд: ответ по умолчанию", "note": "держим"})
        if a == self.SILENT:                  # PRO молчит: как истёкший wait_for в _review
            raise asyncio.TimeoutError()
        return a

    async def _money(self, route: str):
        """Узлы у денег: ответ по маршруту (умолчания безопасны), «молчание» — сон дольше таймаута узла."""
        if route == "mission_guard":
            a = self._next(route, {"decision": "СЛИТЬ", "why": "стенд: ответа у троса не задано"})
        elif route == "mission_take":         # v5.3 W2: мягкий тейк — по умолчанию фиксация
            a = self._next(route, {"decision": "ЗАФИКСИРОВАТЬ", "why": "стенд: ответа у тейка не задано"})
        elif route == "event_triage":         # v5.3 W2: триаж события — по умолчанию {} → СЕЙЧАС (PRO как раньше)
            a = self._next(route, {})
        elif route == "mission_entry":        # v5.4.1: проверка входа у двери — по умолчанию ВОЙТИ
            a = self._next(route, {"decision": "ВОЙТИ", "why": "стенд: вход по умолчанию"})
        elif route == "mission_profit":       # v5.4.1: мысль о прибыли — по умолчанию ДЕРЖАТЬ
            a = self._next(route, {"decision": "ДЕРЖАТЬ", "why": "стенд: держим по умолчанию"})
        else:
            a = self._next(route, {})
        if a == self.SILENT:                  # ИИ у денег молчит — дольше таймаута узла
            await self._silent()
        return a

    async def money_json(self, system, user, *, route, max_tokens=None):
        """v5.4.1: узлы у денег (трос, тейк, триаж, проверка входа, мысль о прибыли) — модель по PYTHIA_MONEY_MODEL."""
        self.calls.append((MM(), route))
        self.last_user[route] = user
        return await self._money(route)

    async def flash_json(self, system, user, *, think=False, route="flash", max_tokens=None):
        self.calls.append(("FLASH", route))
        self.last_user[route] = user
        if route == "scout":
            return self._next(route, {"requests": []})
        return await self._money(route)

    async def flash_text(self, system, user, *a, **k):
        route = k.get("route") or "flash"
        self.calls.append(("FLASH", route))
        self.last_user[route] = user
        if route == "shrink":
            lim = int(user.split("ЛИМИТ: ")[1].split(" ")[0])
            body = user.split("\n\nТЕКСТ:\n", 1)[1]
            return body[:lim]
        if route == "explain":                # толмач: текст по событиям промпта — виден в отчёте
            ev = user.split("═══ ЧТО ПРОИЗОШЛО ═══", 1)[1].split("═══", 1)[0].strip().splitlines()
            return "Толмач: " + " | ".join(l.split(" · ", 1)[-1] for l in ev if l.strip())
        if route == "memory":
            return "ПАМЯТЬ: " + " ".join(user.split("═══ НАКОПИЛОСЬ С ТЕХ ПОР ═══", 1)[1].split())[:400]
        return "сжато"

    def __getattr__(self, name):
        """v5.4.2: разбор слова решения (decision_raw/decision_of, словари узлов) — настоящий: чистые функции без сети."""
        if name.startswith(("decision_", "SYN_")) or name.endswith("_table") or name == "DECISION_KEYS":
            return getattr(_real_ai_v5, name)
        raise AttributeError(name)


class FakeClock:
    """Рыночные часы: открыто / закрыто (выходной) / клиринг — по флагам сцены."""
    open = True
    session = "основная"
    next_in = 4 * 3600
    calls = 0

    @staticmethod
    def enabled():
        return True

    @classmethod
    async def status(cls, t, ac, iid=None):
        cls.calls += 1
        closed_reason = "клиринг" if cls.session == "клиринг" else "выходной — торгов нет"
        return {"open": cls.open, "reason": "торги идут" if cls.open else closed_reason,
                "session": "основная" if cls.open else cls.session,
                "next_open_ts": None if cls.open else time.time() + cls.next_in,
                "next_open_in_s": None if cls.open else cls.next_in,
                "next_open_msk": None if cls.open else ("14:05 МСК" if cls.session == "клиринг" else "10:00 МСК"),
                "source": "tinkoff", "ts": time.time()}

    @staticmethod
    def describe(st):
        if (st or {}).get("open"):
            return "рынок открыт: торги идут"
        return f"рынок закрыт до {(st or {}).get('next_open_msk') or '?'} ({(st or {}).get('reason')}) — вход возможен только с открытия"


class FakeWatch:
    """Дозор: серьёзные заметки, которые сцена кладёт руками."""
    notes: list[dict] = []

    @classmethod
    def serious_since(cls, ts):
        return [{"id": i, "ts": n["ts"], "seen": 0, "data": n} for i, n in enumerate(cls.notes)
                if float(n.get("ts") or 0) > float(ts or 0)]

    @classmethod
    def add(cls, text: str, severity: int = 90, ts: float | None = None) -> None:
        cls.notes.append({"ts": ts or time.time(), "severity": severity, "note": text, "affected": [TICKER]})


class StubCouncil:
    @staticmethod
    def latest():
        return {"data": {"summary": {"regime": "risk-on"}}}

    @staticmethod
    def summary_text(s):
        return "итог совета: risk-on"

    @staticmethod
    async def present_frame(kind, payload):
        return {"headline": "рамка " + kind, "frame": "…", "highlights": []}


class StubNews:
    items: list[dict] = [{"id": "n1", "title": "новость по инструменту", "ts": time.time()}]

    @classmethod
    def news_for_ticker(cls, t, name, days=3, limit=20):
        return list(cls.items)

    @staticmethod
    def fresh_since(ts):
        return []

    @staticmethod
    def render(items, limit=None, rich=False):
        return "\n".join(f"[{i['id']}] {i['title']}" for i in (items if limit is None else items[:limit]))

    @staticmethod
    async def enrich_ticker(run_id, ticker, name, asset_class, days=3):
        return {"new": 0, "relevant": 0, "characterized": 0}


class StubScan:
    MIN_TICKS_AGG = 10
    puncture: dict | None = None                  # v5.3 W3: прокол (puncture_first) — задаётся сценой

    @staticmethod
    async def start(code, ticker, asset_class, minutes):
        return {"ok": True, "minutes": minutes, "code": code}

    @staticmethod
    def status(code):
        out = {"running": True, "ticks": 120, "elapsed_min": 6.0, "until": time.time() + 600,
               "consensus": {"up_share": 0.6, "dn_share": 0.3, "side": "вверх", "now_side": "вверх",
                             "streak": {"win": 20, "up": 12, "dn": 6}, "aggressor_mean": 0.58, "agree": True}}
        if StubScan.puncture:
            out["puncture_first"] = dict(StubScan.puncture)
            out["punctures"] = {"up": [], "down": [], "bin_step": 0.5}
            out["hawkes"] = {"n": 0.71}
            out["tension"] = {"word": "натяжение РАСТЁТ"}
        return out

    @staticmethod
    def stop(code):
        return {"ok": True, "note": "скан остановлен"}

    @staticmethod
    def render_for_ai(agg):
        return "СКАНЕР СТАКАНА: тянут вверх 60%"

    @staticmethod
    def brief(agg):
        return {"running": True, "ticks": 120, "side": "вверх", "up_share": 0.6, "dn_share": 0.3}


class FakeCorrelate:
    items = [{"code": "VTBR", "name": "ВТБ", "asset_class": "share", "kind": "сектор: банки и финансы",
              "rho": 0.81, "lead": 0, "n": 59, "rho_lag": None, "move_1d": 1.2, "move_5d": -0.5,
              "price": 95.5, "source": "tinkoff"}]
    text = staticmethod(correlate.text)
    scout_requests = staticmethod(correlate.scout_requests)

    @classmethod
    async def partners(cls, ticker, ac, days=60, *, force=False):
        return list(cls.items)

    @staticmethod
    def explain(ticker):
        return "стенд"


class FakeMoex:
    @staticmethod
    async def last_price(code, ac):
        return {"price": 2850.5, "change_pct": -0.4, "stale": False, "quote_time": "12:00"}

    @staticmethod
    async def candles(code, ac, interval, days):
        return []


async def fake_build(t, ac, full=True):
    return {"text": "ДОСЬЕ " + t, "price": FakeTinkoff.price or PRICE0, "astro": "", "astro_line": "", "aether": "",
            "kuramoto": None, "kuramoto_text": "", "bifurcation": None, "bif_text": "",
            "wyckoff_text": "ВАЙКОФФ D1: стенд", "xray_text": "", "oracle_text": "", "calendar_text": "",
            "sync_text": "", "reactor_text": "", "weather_text": "", "scan_text": "", "errors": []}


async def fake_light(t, figi, ac):
    return {"text": f"{t}: цена {FakeTinkoff.price}", "price": FakeTinkoff.price, "book": None}


async def fake_prepare(self):
    """prepare() без сети: контракт фьючерса, депозит, killswitch; позиция со счёта — по FakeTinkoff."""
    self.figi, self.asset_class = FIGI, "futures"
    self.go_per_lot, self.tick_size, self.point_value = GO, 0.01, 1.0
    self.deposit = self.deposit_override or DEPOSIT
    self.session_risk = trader_risk.SessionRisk(self.deposit)
    self._sr_day = self._msk_day()
    for x in FakeTinkoff.positions:
        if x.get("figi") == FIGI and abs(x.get("qty") or 0) >= 1 and self.adopt_account:
            self._absorb_account(int(x["qty"]), float(x.get("avg") or 0), None, "на счёте при старте")
    self._prepared = True
    if self._close_pending and not self.position:
        self._close_pending = None
    return True


async def _fake_actx(max_age=0):
    return {}


fake_ai = FakeAI()
_installed = False


def install() -> None:
    """Подменить всё внешнее фейками (один раз на процесс). Ничего в сеть, база — временная."""
    global _installed
    if _installed:
        return
    _installed = True
    store_v5.DB_PATH = Path(tempfile.mkdtemp(prefix="pythia_scen_")) / "scen.db"
    store_v5._schema()
    config.DATA_DIR = Path(tempfile.mkdtemp(prefix="pythia_scen_data_"))   # стенд не читает data/ (state-файл владельца)
    g = vars(mission)
    g["ai_v5"] = fake_ai
    g["market_clock"] = FakeClock
    g["council"], g["newsflow"], g["watch"] = StubCouncil, StubNews, FakeWatch
    g["tinkoff"], g["maya_scan"], g["correlate"] = FakeTinkoff, StubScan, FakeCorrelate
    g["_make_broker"] = FakeBroker
    ai_pilot.market_clock = FakeClock
    ai_pilot.tinkoff = FakeTinkoff
    market_ctx.build, market_ctx.light = fake_build, fake_light
    mission.MissionPilot.prepare = fake_prepare
    _init0 = mission.MissionPilot.__init__

    def _init_no_state(self, *a, **k):            # сцены не трогают data/aipilot_state.json
        _init0(self, *a, **k)
        self._state_path = None
    mission.MissionPilot.__init__ = _init_no_state
    compress.ai_v5 = fake_ai
    explain.ai_v5 = fake_ai
    explain.GATHER_SEC, explain.MIN_GAP_SEC = 0.02, 0.05
    scout.ai_v5, scout.tinkoff, scout.moex, scout.correlate = fake_ai, FakeTinkoff, FakeMoex, FakeCorrelate
    try:
        from . import astro as _astro
        _astro.acontext = _fake_actx
    except Exception:                             # noqa: BLE001 — небо в перепроверке не обязательно
        pass


# ══════════════════════════════════════════════════════════════════════════════
# Сцена: миссия + пилот на фейках, ручные тики
# ══════════════════════════════════════════════════════════════════════════════
class Scene:
    """Одна биржевая ситуация: свой брокер, свой пилот, свой счётчик ИИ; в конце — сводка."""

    def __init__(self, name: str, title: str, play: str = "auto"):
        self.name, self.title, self.play = name, title, play
        self.m: mission.Mission | None = None
        self.p: mission.MissionPilot | None = None
        self.broker = FakeBroker()
        self._saved: dict[str, Any] = {}
        self.note = ""

    async def __aenter__(self) -> "Scene":
        install()
        # ревью 5.4.2: ручки «свободного пилота» — на умолчаниях на время сцены (возврат в __aexit__): настройки
        # владельца (data/config_user.json, окружение) не роняют стенд
        config.pin_free_pilot_defaults(lambda k, v: self.patch(config, k, v))
        fake_ai.reset()
        FakeTinkoff.price, FakeTinkoff.book_fail, FakeTinkoff.positions = PRICE0, False, []
        FakeTinkoff.price_err = FakeTinkoff.access = None       # v5.4.4: связь и токен — в порядке
        FakeTinkoff.epoch = FakeTinkoff.checks = 0
        FakeTinkoff.accs_fail = FakeTinkoff.pf_fail = False
        FakeClock.open, FakeClock.session, FakeClock.calls = True, "основная", 0
        FakeWatch.notes = []
        StubScan.puncture = None
        mission._M.clear()
        mission._panic_refused.clear()            # W4: «повторное нажатие ПАНИКИ = force» не переживает сцену
        explain.reset()
        with store_v5._c() as c:                  # временная база стенда: каждая сцена с чистого листа
            c.execute("DELETE FROM trades")
            c.execute("DELETE FROM missions")
        m = mission.Mission(TICKER, "Тестовый фьючерс", "futures", self.play, DEPOSIT)
        m.run_id = bus.start_run("mission", {"ticker": TICKER, "play": self.play, "scenario": self.name})
        m.council_ts = time.time() - 4000         # окно совета открыто (PYTHIA_COUNCIL_GAP_SEC)
        m.exec = {"do": "BUY", "entry": None, "entry_kind": "сейчас", "take": 110.0, "invalidation": 98.0,
                  "why": "стенд", "plan": "", "confidence": 60, "news_ids": [], "levels": [], "time_note": ""}
        m.exec_ts = time.time()
        mission._M[TICKER] = m
        p = mission.MissionPilot(TICKER, deposit=DEPOSIT, broker=self.broker, mission=m)
        mission._bind_pilot(m, p)
        m.pilot = p
        await p.prepare()
        m.task = asyncio.get_running_loop().create_future()   # «пилот жив» без петли: тики — руками
        self.m, self.p = m, p
        return self

    async def __aexit__(self, et, ev, tb):
        try:
            await self.settle_ai()
        finally:
            for k, v in self._saved.items():
                mod, attr = k
                setattr(mod, attr, v)
            self._saved.clear()
            m = self.m
            if m and m.task and not m.task.done():
                m.task.set_result(None)
            if m and m.run_id:
                try:
                    bus.end_run(m.run_id)
                except Exception:                 # noqa: BLE001
                    pass
            explain.reset()
            mission._M.clear()
        return False

    # ── подмена констант на время сцены ──
    def patch(self, mod, attr: str, value) -> None:
        self._saved.setdefault((mod, attr), getattr(mod, attr))
        setattr(mod, attr, value)

    # ── действия ──
    def ex(self, do="BUY", entry=None, take=110.0, inv=98.0, why="стенд") -> dict:
        return {"exec": {"do": do, "entry": entry, "take": take, "invalidation": inv, "why": why}}

    async def tick(self, price: float, with_book: bool = True, n: int = 1) -> None:
        FakeTinkoff.price = price
        for _ in range(n):
            await self.p.tick(price, book(price) if with_book else None)
            await self.wait_gate()                # 5.4.1: вход идёт через проверку у двери фоном — дождаться ответа

    async def wait_gate(self, n: int = 300) -> None:
        """5.4.1: PRO у двери думает фоном (_gate_bg) — дождаться ответа и того, что он породил (заявку входа)."""
        p = self.p
        plan = p.plan
        if plan is not None and plan.get("gate_busy"):
            await self.settle(lambda: not plan.get("gate_busy"), n)
        gt = p._gate_task
        if gt and not gt.done():
            try:
                await asyncio.wait_for(asyncio.shield(gt), 5)
            except Exception:                     # noqa: BLE001
                pass

    async def wait_profit(self, n: int = 300) -> None:
        """5.4.1: мысль о прибыли (_profit_bg) — дождаться ответа PRO и всего, что он породил."""
        p = self.p
        pos = p.position
        if pos is not None and pos.get("profit_busy"):
            await self.settle(lambda: not pos.get("profit_busy"), n)
        pt = p._profit_task
        if pt and not pt.done():
            try:
                await asyncio.wait_for(asyncio.shield(pt), 5)
            except Exception:                     # noqa: BLE001
                pass

    async def open_position(self, side="long", price=PRICE0, inv=98.0, take=110.0, age_s: float = 0.0) -> dict:
        """Позиция настоящим путём: приказ → проверка у двери (PRO: ВОЙТИ по умолчанию) → агрессивная лимитка →
        FILL → трос: на бирже при PYTHIA_EXCHANGE_STOP, иначе (умолчание 5.4.1) только в программе."""
        do = "BUY" if side == "long" else "SELL"
        assert self.p.adopt_forecast(self.ex(do, None, take, inv)), self.p.last_action
        await self.tick(price)
        await self.tick(price)
        pos = self.p.position
        assert pos and pos["side"] == side, self.p.last_action
        assert pos.get("stop_id") or not self.broker.stop_ok or not config.PYTHIA_EXCHANGE_STOP, self.p.last_action
        if age_s:
            pos["opened_ts"] -= age_s
        return pos

    async def settle(self, cond: Callable[[], bool], n: int = 300, dt: float = 0.01) -> bool:
        for _ in range(n):
            if cond():
                return True
            await asyncio.sleep(dt)
        return cond()

    async def wait_guard(self, n: int = 300) -> None:
        """Дождаться ответа FLASH у троса (задача _guard_bg) и всего, что он породил."""
        p = self.p
        await self.settle(lambda: not (p.position and p.position.get("guard_busy")), n)
        gt = p._guard_task
        if gt and not gt.done():
            try:
                await asyncio.wait_for(asyncio.shield(gt), 5)
            except Exception:                     # noqa: BLE001
                pass

    async def wait_triage(self, n: int = 300) -> None:
        """Дождаться FLASH-триажа события (задача _triage_bg) — резкий ход поднимает его фоном."""
        p = self.p
        await self.settle(lambda: not (p._triage_task and not p._triage_task.done()), n)

    async def wait_council(self, n: int = 600) -> None:
        p = self.p
        await self.settle(lambda: not p._reanalyzing, n)

    async def settle_ai(self) -> None:
        """Толмач и память: дождаться фоновых задач, дожать очередь (объяснение — в отчёт)."""
        m = self.m
        if not m:
            return
        st = explain._state(m)
        for _ in range(200):
            t, mt = st.get("task"), st.get("mem_task")
            busy = (t and not t.done()) or (mt and not mt.done()) or st["queue"]
            if not busy:
                break
            await asyncio.sleep(0.01)
        if st["queue"]:
            await explain.flush(m)

    # ── сводка ──
    def xevents(self, kind: str | None = None) -> list[dict]:
        return [e for x in (self.m.explain if self.m else []) for e in x.get("events") or []
                if kind is None or e.get("kind") == kind]

    def last_explain(self) -> str:
        return str(self.m.explain[-1].get("text") or "") if self.m and self.m.explain else ""

    def last_explain_title(self) -> str:
        return str(self.m.explain[-1].get("title") or "") if self.m and self.m.explain else ""

    def result(self, ok: bool, error: str = "") -> dict:
        return {"name": self.name, "title": self.title, "ok": ok, "error": error, "note": self.note,
                "orders": self.broker.orders_text(), "stops": len(self.broker.stops),
                "ai": fake_ai.summary(), "last_action": self.p.last_action if self.p else "",
                "explain": self.last_explain(), "explain_title": self.last_explain_title(),
                "trades": store_v5.trades_summary(TICKER).get("count", 0)}


# ══════════════════════════════════════════════════════════════════════════════
# Сценарии
# ══════════════════════════════════════════════════════════════════════════════
async def s01_gap_hard(sc: Scene) -> None:
    """Гэп ценой сразу за аварийный трос — закрытие без вопроса FLASH."""
    sc.patch(config, "PYTHIA_EXCHANGE_STOP", True)     # сцена про трос НА БИРЖЕ (умолчание 5.4.1 — только в программе)
    pos = await sc.open_position("long", inv=98.0)
    hard, sid = pos["hard_stop"], pos["stop_id"]
    assert hard and hard < 98.0 and sc.broker.stops[-1]["stop"] == hard, "на бирже лежит трос дальше триггера"
    await sc.tick(hard - 1.5)                     # гэп: цена сразу за тросом
    assert sc.p.position is None and sc.p.pnls and sc.p.pnls[-1] < 0, sc.p.last_action
    assert fake_ai.count("mission_guard") == 0, "FLASH у троса не спрашивали — за тросом слив без вопросов"
    closes = [x for x in sc.broker.placed if x["tag"] == "aip-close"]
    assert len(closes) == 1 and closes[0]["lots"] == 8 and closes[0]["direction"] == SELL and closes[0]["price"] is None
    assert sc.broker.stop_cancels == [sid], "трос снят перед рыночным закрытием"
    await sc.settle_ai()
    assert any("Позиция закрыта" in e["title"] for e in sc.xevents("close")), sc.m.explain
    assert store_v5.trades(TICKER)[0]["why"].startswith("аварийный трос")
    assert sc.m.handoffs and "после закрытия" in sc.m.handoffs[-1]["reason"] and sc.m.handoffs[-1]["kind"] == "pilot"
    sc.note = f"трос {hard:g}, закрыто рыночным, P/L {sc.p.pnls[-1]:+.0f}"


async def s02_gap_trigger(sc: Scene) -> None:
    """Гэп за триггер, но не за трос — FLASH спрашивают; ЖДАТЬ → совет без очереди → CLOSE (а во второй
    сцене — держать с новыми уровнями)."""
    pos = await sc.open_position("long", inv=98.0)
    hard = pos["hard_stop"]
    sc.p._last_reanalyze_ts = time.time()         # советы «только что» — трос обязан пройти без очереди
    sc.m.council_ts = time.time()
    fake_ai.queue("mission_guard", {"decision": "ЖДАТЬ", "why": "ложный прокол, стакан держит",
                                    "hold_until_price": 97.2, "hold_minutes": 5})
    fake_ai.queue("mission_exec", {"do": "CLOSE", "why": "картина сломалась — выходим"})
    await sc.tick(97.6)                           # за триггером 98, выше троса
    assert pos.get("guard_busy") or sc.p.guards, sc.p.last_action
    await sc.wait_guard()
    assert sc.p.guards[-1]["decision"] == "ЖДАТЬ" and pos["holds"] == 1 and pos["invalidation"] == 97.2, sc.p.guards
    assert pos["hard_stop"] == hard, "трос за FLASH не двигается"
    assert sc.m.handoffs[-1]["kind"] == "stop" and not sc.m.handoffs[-1]["deferred"]
    await sc.wait_council()
    assert fake_ai.count("mission_exec") == 1 and fake_ai.count("mission_analysis") == 1, fake_ai.summary()
    assert "ОТКРЫТАЯ ПОЗИЦИЯ" in fake_ai.last_user["mission_exec"], "совет видит позицию"
    assert sc.m.exec["do"] == "CLOSE" and sc.p._close_pending, (sc.m.exec, sc.p.last_action)
    await sc.tick(97.6)
    assert sc.p.position is None and "приказ совета: закрыть" in sc.p.last_action, sc.p.last_action
    assert fake_ai.count("mission_guard") == 1
    await sc.settle_ai()
    assert any(f"{MM()} у троса: ЖДАТЬ" in e["title"] for e in sc.xevents("guard"))
    assert any("CLOSE" in e["title"] for e in sc.xevents("council")), [e["title"] for e in sc.xevents()]
    # вторая сцена: совет говорит держать (ревью 5.4.2: HOLD — без добора, новые уровни) — позиция цела
    fake_ai.queue("mission_guard", {"decision": "ЖДАТЬ", "why": "прокол", "hold_minutes": 5})
    fake_ai.queue("mission_exec", {"do": "HOLD", "entry": None, "take": 112.0, "invalidation": 96.0, "why": "держать"})
    sc.p.pnls, sc.p.session_risk = [], trader_risk.SessionRisk(DEPOSIT)
    pos2 = await sc.open_position("long", inv=98.0)
    await sc.tick(97.6)
    await sc.wait_guard()
    await sc.wait_council()
    assert sc.p.position is pos2 and pos2["invalidation"] == 96.0 and pos2["take"] == 112.0, pos2
    assert pos2["holds"] == 0, "новый приказ совета — счётчик «ждать» заново"
    await sc.tick(97.6)
    assert sc.p.position is pos2 and sc.p.state == "В_ПОЗИЦИИ", sc.p.last_action
    assert sc.m.exec["do"] == "HOLD", sc.m.exec
    sc.note = "FLASH ЖДАТЬ → совет: CLOSE закрыл; HOLD — позиция держится с уровнями совета"


async def s03_dead_market(sc: Scene) -> None:
    """Нет котировок / стакан протух (мёртвый рынок) — входов нет, ложных стопов нет, позиция цела."""
    sc.patch(config, "PYTHIA_EXCHANGE_STOP", True)     # сцена про трос НА БИРЖЕ (умолчание 5.4.1 — только в программе)
    pos = await sc.open_position("long", inv=98.0)
    n_orders = len(sc.broker.placed)
    # 1) нет котировок: настоящая петля run() не тикает без цены (позиция и трос нетронуты)
    sc.patch(ai_pilot, "TICK_SEC", 0.01)
    FakeTinkoff.price = None
    sc.m.task.set_result(None)
    task = asyncio.get_running_loop().create_task(sc.p.run())
    sc.m.task = task
    n_ticks = sc.p._tick_n
    await asyncio.sleep(0.15)
    assert sc.p._tick_n == n_ticks and sc.p.position is pos and len(sc.broker.placed) == n_orders, "без котировок тиков нет"
    # 2) цена есть, стакан не отдаётся: план BUY «сейчас» не исполняется — «рынок мёртв»
    sc.p.plan = None
    FakeTinkoff.book_fail = True
    FakeTinkoff.price = 100.0
    sc.p._book_ts = time.time() - ai_pilot.BOOK_FRESH_SEC - 1     # стакан протух
    sc.p.plan = {"side": "long", "entry": None, "kind": "сейчас", "take": 110.0, "invalidation": 98.0,
                 "why": "добор", "ts": time.time()}
    await asyncio.sleep(0.15)
    sc.p.stop()
    await asyncio.wait_for(task, 5)
    sc.m.task = asyncio.get_running_loop().create_future()
    assert sc.p._tick_n > n_ticks and "рынок мёртв" in sc.p.last_action, sc.p.last_action
    assert sc.p.pending is None and len(sc.broker.placed) == n_orders and sc.p.position is pos
    # 3) перепроверка при мёртвом рынке откладывается, PRO не дёргают
    sc.p.review_ts = 0.0
    await sc.tick(100.0, with_book=False)
    assert "перепроверка отложена" in sc.p.last_action and fake_ai.count("mission_review") == 0, sc.p.last_action
    assert sc.p.review_ts > time.time() + 500
    assert fake_ai.count("mission_guard") == 0 and sc.p.position is pos and pos["stop_id"], "ложных стопов нет"
    sc.note = "без цены петля стоит; протухший стакан — входов нет; перепроверка отложена"


async def s04_market_closed(sc: Scene) -> None:
    """Рынок закрылся в позиции — стопор; открылся — сверка и перепроверка с накопленными новостями."""
    pos = await sc.open_position("long", inv=98.0)
    sc.p.review_ts = time.time() + 1800
    FakeClock.open = False
    await sc.tick(99.0)
    assert sc.p.state == "РЫНОК_ЗАКРЫТ" and mission.status(TICKER)["phase"] == "closed", sc.p.state
    assert sc.p.review_ts >= time.time() + FakeClock.next_in, "перепроверка ждёт открытия"
    n_st, n_o = len(sc.broker.stops), len(sc.broker.placed)
    await sc.tick(97.0)                           # за триггером при закрытом рынке — FLASH не спрашивают
    await asyncio.sleep(0.05)
    assert sc.p.position is pos and not pos.get("guard_busy") and fake_ai.count("mission_guard") == 0
    assert "под аварийным тросом" in sc.p.last_action and len(sc.broker.stops) == n_st and len(sc.broker.placed) == n_o
    await mission.on_serious_news({"note": "ЦБ поднял ставку до 21%", "severity": 90})
    assert sc.m.handoffs[-1]["deferred"] and "рынок закрыт" in sc.p.last_action, sc.p.last_action
    FakeWatch.add("Санкции на крупнейший банк")   # серьёзное за ночь — дозор
    await sc.settle_ai()
    assert any(e["title"].startswith("Рынок закрыт") for e in sc.xevents("market"))
    sc.p._closed_since = time.time() - 8 * 3600
    FakeClock.open = True
    await sc.tick(100.5)                          # открытие после ночи
    assert sc.p.state == "В_ПОЗИЦИИ" and sc.p.position is pos and sc.p._closed_since == 0.0, sc.p.state
    rr = sc.p._review_reason or ""
    assert "рынок открылся" in rr and "ЦБ" in rr and "Санкции" in rr, rr
    assert sc.p.review_ts <= time.time() + ai_pilot.OPEN_REVIEW_GRACE_SEC + 1 and sc.m.handoffs[-1]["kind"] == "open"
    # перепроверка: PRO видит повод с накопленными новостями
    sc.p.review_ts = 0.0
    fake_ai.queue("mission_review", {"choice": "ЖДЁМ", "why": "после ночи спокойно", "note": "держим"})
    await sc.tick(100.5)
    assert await sc.settle(lambda: fake_ai.count("mission_review") == 1 and not sc.p._review_busy)
    ur = fake_ai.last_user["mission_review"]
    pv = ur.split("ПОВОД ПЕРЕПРОВЕРКИ (внеплановая): ", 1)[1].split("\n", 1)[0]
    assert "рынок открылся" in pv and "ЦБ поднял ставку" in pv and "Санкции" in pv and "закрыт был 8 ч" in pv, pv
    assert sc.p._review_reason is None and sc.p.position is pos
    # короткий клиринг: перепроверку не уносит к завтрашнему открытию
    sc.p.review_ts = time.time() + 1500
    FakeClock.open, FakeClock.session = False, "клиринг"
    await sc.tick(100.5)
    assert sc.p.state == "РЫНОК_ЗАКРЫТ" and "14:05 МСК" in sc.p.last_action, sc.p.last_action
    sc.p._closed_since = time.time() - 300
    FakeClock.open, FakeClock.session = True, "основная"
    await sc.tick(100.5)
    assert sc.p.state == "В_ПОЗИЦИИ" and time.time() + 1000 < sc.p.review_ts <= time.time() + 1500
    assert fake_ai.count("mission_review") == 1, "клиринг PRO не дёргает"
    await sc.settle_ai()
    assert any(e["title"] == "Рынок открылся" for e in sc.xevents("market"))
    sc.note = "закрыто → стопор, повод копится; открылось → сверка, PRO с новостями; клиринг тихо"


async def s05_partial_then_30042(sc: Scene) -> None:
    """Частичное исполнение, затем отказ биржи 30042 (текст дошёл до last_action и толмача), бэкофф и
    серия отказов → план снят."""
    sc.patch(config, "PYTHIA_EXCHANGE_STOP", True)     # сцена про трос НА БИРЖЕ (умолчание 5.4.1 — только в программе)
    b = sc.broker
    b.fill_next = False
    assert sc.p.adopt_forecast(sc.ex("BUY"))
    await sc.tick(100.0)
    assert sc.p.pending and sc.p.pending["lots"] == 8
    b.partial[sc.p.pending["order_id"]] = 3       # налили 3 из 8
    sc.p.pending["ts"] -= ai_pilot.ENTRY_TTL_SEC + 1
    await sc.tick(100.0)                          # срок лимитки вышел: снята, частичка — позиция
    pos = sc.p.position
    assert pos and pos["lots"] == 3 and sc.p.plan is None and sc.broker.stops[-1]["lots"] == 3, sc.p.last_action
    assert b.cancelled, "остаток заявки снят"
    # добор по приказу PRO: биржа отбивает с текстом 30042
    b.fill_next = True
    b.place_error = "30042: недостаточно средств/обеспечения для сделки"
    fake_ai.queue("mission_review", {"choice": "ДОБРАТЬ", "why": "тянут вверх", "invalidation": 98.0, "take": 110.0})
    await sc.p._review(100.0)
    assert sc.p.plan and sc.p.plan["side"] == "long"
    await sc.tick(100.0)
    assert sc.p._entry_fail == 1 and "30042" in sc.p.last_action and "отбит" in sc.p.last_action, sc.p.last_action
    assert sc.p.no_entry_until > time.time(), "бэкофф после отказа"
    await sc.tick(100.0)
    assert "бэкофф" in sc.p.last_action and sc.p._entry_fail == 1, sc.p.last_action
    await sc.settle_ai()
    xr = [e for e in sc.xevents("refusal")]
    assert xr and xr[-1]["title"] == "Биржа отбила заявку входа" and "30042" in xr[-1]["detail"], xr
    assert "попытка" not in xr[-1]["detail"], "счётчик попыток вычищен — одно объяснение на серию"
    for _ in range(ai_pilot.ENTRY_FAIL_MAX - 1):
        sc.p.no_entry_until = 0.0
        await sc.tick(100.0)
    assert sc.p.plan is None and sc.p._entry_fail == ai_pilot.ENTRY_FAIL_MAX, (sc.p.plan, sc.p._entry_fail)
    assert sc.p.position is pos and pos["lots"] == 3, "частичка цела"
    assert any("серия отказов" in h["reason"] and h["kind"] == "pilot" for h in sc.m.handoffs), sc.m.handoffs
    assert len([x for x in b.placed]) == 1, "отбитые заявки на биржу не легли"
    await sc.tick(100.0)
    assert sc.p.state == "В_ПОЗИЦИИ", sc.p.state
    sc.note = "3 из 8 налито; ДОБРАТЬ → 30042 в last_action и у толмача, бэкофф, серия → план снят"


async def s06_shock_move(sc: Scene) -> None:
    """Резкий ход +2% — одна внеплановая перепроверка с пейсингом; второй такой же ход не плодит вторую."""
    sc.patch(config, "PYTHIA_PROFIT_THINK", False)   # 5.4.1: рывок в плюсе идёт в мысль о прибыли (сцена 38); здесь — триаж как раньше
    pos = await sc.open_position("long", inv=98.0, take=120.0, age_s=1300)   # тишина после входа прошла
    t0 = time.time()
    sc.p._px_hist = [(t0 - 400, 100.0), (t0 - 300, 100.0), (t0 - 200, 100.0), (t0 - 100, 100.0)]
    sc.p.review_ts, sc.p._last_review_ts = t0 + 1800, t0     # PRO только что ответил
    n_h = len(sc.m.handoffs)
    await sc.tick(102.0)
    await sc.wait_triage()                        # резкий ход при позиции — сначала триаж FLASH; без ответа ({}) → СЕЙЧАС → PRO как раньше
    assert fake_ai.count("event_triage") == 1 and sc.p.triages[-1]["urgency"] == "СЕЙЧАС", (fake_ai.summary(), sc.p.last_action)
    assert len(sc.m.handoffs) == n_h + 1 and sc.m.handoffs[-1]["kind"] == "shock" and "+2.00%" in sc.m.handoffs[-1]["reason"]
    assert sc.m.handoffs[-1]["triage"].startswith("СЕЙЧАС") and not sc.m.handoffs[-1]["deferred"]
    assert sc.p._review_pulled and time.time() + mission.EVENT_MIN_GAP_SEC - 5 <= sc.p.review_ts <= time.time() + mission.EVENT_MIN_GAP_SEC + 1
    await sc.tick(102.2)
    assert len(sc.m.handoffs) == n_h + 1, "тот же ход второй раз — не повод"
    sc.p.review_ts = 0.0
    fake_ai.queue("mission_review", {"choice": "ЖДЁМ", "why": "импульс, держим", "invalidation": 99.5, "take": 120.0})
    await sc.tick(102.2)
    assert await sc.settle(lambda: fake_ai.count("mission_review") == 1 and not sc.p._review_busy)
    assert "резкий ход: +2.00%" in fake_ai.last_user["mission_review"]
    assert pos["invalidation"] == 99.5 and sc.p._last_event_review_ts > 0
    # второй такой же ход сразу после ответа PRO — повод есть, но перепроверка не раньше PYTHIA_EVENT_COOL_SEC
    t1 = time.time()
    sc.p._px_hist = [(t1 - 400, 102.0), (t1 - 300, 102.0), (t1 - 200, 102.0), (t1 - 100, 102.0)]
    sc.p._last_shock_ts = 0.0
    sc.p.review_ts = t1 + 1800
    await sc.tick(104.2)
    await sc.wait_triage()
    assert sc.m.handoffs[-1]["kind"] == "shock" and "+2.16%" in sc.m.handoffs[-1]["reason"], sc.m.handoffs[-1]
    assert sc.p.review_ts >= time.time() + float(config.PYTHIA_EVENT_COOL_SEC) - 10, "пейсинг событий"
    assert fake_ai.count("mission_review") == 1 and sc.p.position is pos
    sc.note = "один ход — триаж FLASH → одна перепроверка PRO; повтор хода — в очередь с пейсингом"


async def s07_take_hit(sc: Scene) -> None:
    """Тейк достигнут — мягкий тейк (W2): FLASH за секунды решает; ЗАФИКСИРОВАТЬ → закрытие рыночным с
    причиной FLASH, толмач и память; выключен конфигом — закрытие без вопросов, как раньше."""
    pos = await sc.open_position("long", inv=98.0, take=103.0)
    entry = pos["entry"]
    fake_ai.queue("mission_take", {"decision": "ЗАФИКСИРОВАТЬ", "why": "цель взята, стакан над 103 редеет",
                                   "lock_price": None, "tp_next": None, "note": "в кассу"})
    await sc.tick(103.4)
    assert sc.p.position is pos and pos.get("guard_busy") and pos.get("guard_side") == "take", sc.p.last_action
    assert f"спрашиваю {MM()}: зафиксировать" in sc.p.last_action and sc.p.status()["take_guard"]["busy"]
    assert not sc.p.status()["guard"]["busy"], "это тейк, не трос"
    await sc.wait_guard()
    assert sc.p.position is None and "ПОБЕДА: тейк" in store_v5.trades(TICKER)[0]["why"], sc.p.last_action
    assert "цель взята" in store_v5.trades(TICKER)[0]["why"]
    assert fake_ai.count("mission_take") == 1 and fake_ai.count("mission_guard") == 0 and fake_ai.count("mission_review") == 0
    ut = fake_ai.last_user["mission_take"]
    for piece in ("ТЕЙК: long, цена 103.4 выше цели 103", "ХОД ЦЕНЫ", "ЖИВОЙ РЫНОК", "ПЛАН И ПРОШЛЫЕ РЕШЕНИЯ",
                  "СВЯЗАННЫЕ БУМАГИ", "ИТОГ ОБЩЕГО СОВЕТА", "СВЕЖИЕ НОВОСТИ", "Зафиксировать сейчас или подержать"):
        assert piece in ut, (piece, ut[:800])
    assert sc.m.sizes["take"]["prompt"] == len(ut) and sc.m.sizes["take"]["answer"] > 0
    g = sc.p.guards[-1]
    assert g["side"] == "take" and g["decision"] == "ЗАФИКСИРОВАТЬ" and g["take"] == 103.0, g
    close = [x for x in sc.broker.placed if x["tag"] == "aip-close"]
    assert len(close) == 1 and close[0]["lots"] == 8 and close[0]["price"] is None
    assert abs(sc.p.pnls[-1] - (103.4 - entry) * 8) < 1e-6
    after = float(config.PYTHIA_AFTER_CLOSE_SEC)
    assert time.time() + after - 10 <= sc.p.review_ts <= time.time() + after + 1, "после закрытия — остыть, потом PRO"
    assert sc.m.handoffs[-1]["kind"] == "pilot" and "после закрытия" in sc.m.handoffs[-1]["reason"]
    await sc.settle_ai()
    assert any(f"{MM()} у тейка: ЗАФИКСИРОВАТЬ" in e["title"] for e in sc.xevents("take")), [e["title"] for e in sc.xevents()]
    assert any("Позиция закрыта" in e["title"] for e in sc.xevents("close"))
    assert sc.m.memory.startswith("ПАМЯТЬ"), "закрытие — узел памяти"
    # мягкий тейк выключен конфигом → закрытие без вопросов, как раньше
    sc.patch(config, "PYTHIA_SOFT_TAKE", False)
    sc.p.pnls, sc.p.session_risk = [], trader_risk.SessionRisk(DEPOSIT)
    pos2 = await sc.open_position("long", inv=98.0, take=103.0)
    await sc.tick(103.4)
    assert sc.p.position is None and fake_ai.count("mission_take") == 1 and "всё в кассу" in store_v5.trades(TICKER)[0]["why"]
    assert pos2 is not sc.p.position and sc.p.status()["soft_take"] is False
    sc.note = f"тейк 103 → FLASH ЗАФИКСИРОВАТЬ → закрыто рыночным, P/L {sc.p.pnls[-1] if sc.p.pnls else 0:+.0f}; выкл → как раньше"


async def s08_news_storm(sc: Scene) -> None:
    """Шквал из 5 серьёзных новостей за 10 минут — одна перепроверка, поводы склеены."""
    pos = await sc.open_position("long", inv=98.0, age_s=1300)
    sc.p.review_ts, sc.p._last_review_ts = time.time() + 1800, time.time()
    n_h = len(sc.m.handoffs)
    for i in range(1, 6):
        await mission.on_serious_news({"note": f"Новость {i}: важное событие номер {i}", "severity": 90})
    assert len(sc.m.handoffs) == n_h + 5 and all(h["kind"] == "news" for h in sc.m.handoffs[-5:])
    assert time.time() + mission.EVENT_MIN_GAP_SEC - 5 <= sc.p.review_ts <= time.time() + mission.EVENT_MIN_GAP_SEC + 1, \
        "первая новость подняла перепроверку (не раньше 3 мин после ответа PRO)"
    assert sc.p._review_reason.count("Новость") == 5, sc.p._review_reason
    sc.p.review_ts = 0.0
    fake_ai.queue("mission_review", {"choice": "ЖДЁМ", "why": "шум", "note": "держим"})
    await sc.tick(100.0)
    assert await sc.settle(lambda: fake_ai.count("mission_review") == 1 and not sc.p._review_busy)
    ur = fake_ai.last_user["mission_review"]
    for i in range(1, 6):
        assert f"Новость {i}" in ur, i
    assert ur.count("ПОВОД ПЕРЕПРОВЕРКИ") == 1 and sc.p._review_reason is None
    sc.p.review_ts = 0.0
    await sc.tick(100.0)                          # плановая — без повода, PRO по расписанию
    assert await sc.settle(lambda: fake_ai.count("mission_review") == 2 and not sc.p._review_busy)
    assert "ПОВОД ПЕРЕПРОВЕРКИ" not in fake_ai.last_user["mission_review"] and sc.p.position is pos
    sc.note = "5 новостей → 5 поводов в хронике, 1 перепроверка PRO со всеми"


async def s09_restart(sc: Scene) -> None:
    """Рестарт сервера в позиции — подхват из state-файла с тросом, guard_next и holds."""
    sc.patch(config, "PYTHIA_EXCHANGE_STOP", True)     # сцена про трос НА БИРЖЕ (умолчание 5.4.1 — только в программе)
    pos = await sc.open_position("long", inv=98.0)
    hard, sid = pos["hard_stop"], pos["stop_id"]
    pos["holds"], pos["guard_next"] = 1, time.time() + 300
    path = Path(tempfile.mkdtemp(prefix="pythia_scen_")) / "aipilot_state.json"
    sc.p._state_path = path
    sc.p._puncture_seen[("вверх", 200, "угроза")] = time.time() - 60   # фаза 3: память прокола — в секции pilot
    sc.p._last_puncture_ts = time.time() - 60
    sc.p._save_state()
    rec = json.loads(path.read_text())
    assert rec["figi"] == FIGI and rec["position"]["hard_stop"] == hard and rec["position"]["holds"] == 1
    assert rec["pilot"]["puncture_seen"][0][:3] == ["вверх", 200, "угроза"] and rec["pilot"]["last_puncture_ts"] > 0, rec["pilot"]
    # «новый сервер»: новый пилот той же миссии, тот же брокер (на счёте 8 лотов)
    p2 = mission.MissionPilot(TICKER, deposit=DEPOSIT, broker=sc.broker, mission=sc.m)
    mission._bind_pilot(sc.m, p2)
    await p2.prepare()
    p2._state_path = path
    p2._restore_state(sc.broker.net)
    sc.m.pilot, sc.p = p2, p2
    q = p2.position
    assert q and q["side"] == "long" and q["lots"] == 8 and q["hard_stop"] == hard and q["stop_id"] == sid, q
    assert q["holds"] == 1 and q.get("guard_next") and q["guard_next"] - time.time() > 250
    assert not q.get("restop") and p2.state == "В_ПОЗИЦИИ" and "РЕСТАРТ" in p2.last_action
    assert ("вверх", 200, "угроза") in p2._puncture_seen and p2._last_puncture_ts > 0, "память прокола пережила рестарт"
    n_st = len(sc.broker.stops)
    p2._tick_n = ai_pilot.RECONCILE_EVERY - 1
    await sc.tick(100.0)                          # сверка со счётом сходится — трос не перевыставляется
    assert p2.position is q and len(sc.broker.stops) == n_st and not sc.broker.stop_cancels[1:]
    await sc.tick(97.5)                           # за триггером, но срок «ждать» FLASH не вышел — тихо
    await asyncio.sleep(0.05)
    assert p2.position is q and fake_ai.count("mission_guard") == 0 and not q.get("guard_busy")
    q["guard_next"] = 0.0
    fake_ai.queue("mission_guard", {"decision": "СЛИТЬ", "why": "после рестарта картина хуже"})
    await sc.tick(97.5)
    await sc.wait_guard()
    assert p2.position is None and f"{MM()} решил слить" in store_v5.trades(TICKER)[0]["why"], p2.last_action
    assert p2.last_action.startswith(f"ЗАКРЫЛ ВСЁ (мягкий стоп: {MM()} решил слить") and "P/L" in p2.last_action, p2.last_action
    assert "после закрытия" in p2.last_action and p2.last_action.index("ЗАКРЫЛ") < p2.last_action.index("после закрытия"), p2.last_action
    rec2 = json.loads(path.read_text())               # позиции нет, но память прокола в файле осталась (секция pilot)
    assert rec2["position"] is None and rec2["pilot"]["puncture_seen"], rec2
    assert rec2["pilot"]["gates"] and rec2["pilot"]["gates"][-1]["decision"] == "ВОЙТИ", "5.4.1: проверки входа тоже в секции pilot"
    p2._puncture_seen.clear(); p2._last_puncture_ts = 0.0
    p2.gates.clear(); p2.profits.clear()
    p2._save_state()
    assert not path.exists(), "state-файл стёрт: ни позиции, ни памяти"
    sc.note = "трос, holds=1, guard_next, память прокола пережили рестарт; сверка без перестановки троса; причина закрытия первой"


async def s10_owner_hands(sc: Scene) -> None:
    """Владелец докупил / продал часть / перевернул руками — принято без стопа-сироты и без фантома
    при лаге портфеля."""
    sc.patch(config, "PYTHIA_EXCHANGE_STOP", True)     # сцена про трос НА БИРЖЕ (умолчание 5.4.1 — только в программе)
    b = sc.broker
    pos = await sc.open_position("long", inv=98.0)
    sid0 = pos["stop_id"]
    # 11 лотов × ГО 12 000 × MAINT_FRAC 0.9 = 118 800 > депозит 100 000: иначе маржевой дозор ужмёт позицию раньше,
    # чем сцена проверит «докупил → веду всё» (W1: прежний assert ловил артефакт порядка, а не волю владельца)
    sc.p.deposit = 150000.0
    b.pf_positions = [{"figi": FIGI, "qty": 11, "avg": 100.2}]     # докупил 3
    sc.p._tick_n = ai_pilot.RECONCILE_EVERY - 1
    await sc.tick(100.3)
    assert pos["lots"] == 11 and abs(pos["entry"] - 100.2) < 1e-9 and "докупил 3" in sc.p.last_action, sc.p.last_action
    await sc.tick(100.3)                          # restop → трос на 11, старый снят
    assert b.stops[-1]["lots"] == 11 and sid0 in b.stop_cancels
    n_tr = store_v5.trades_summary(TICKER)["count"]
    b.pf_positions = [{"figi": FIGI, "qty": 6, "avg": 100.2}]      # продал 5
    sc.p._tick_n = ai_pilot.RECONCILE_EVERY - 1
    await sc.tick(100.4)
    assert pos["lots"] == 6 and store_v5.trades_summary(TICKER)["count"] == n_tr + 1, sc.p.last_action
    await sc.tick(100.4)
    assert b.stops[-1]["lots"] == 6
    sid1 = pos["stop_id"]
    b.pf_positions = [{"figi": FIGI, "qty": -4, "avg": 100.4}]     # перевернул в шорт
    sc.p._tick_n = ai_pilot.RECONCILE_EVERY - 1
    await sc.tick(100.4)
    q = sc.p.position
    assert q and q["side"] == "short" and q["lots"] == 4 and q["stop_id"] == sid1 and q.get("restop"), q
    await sc.tick(100.4)
    assert sid1 in b.stop_cancels and b.stops[-1]["direction"] == BUY and b.stops[-1]["lots"] == 4, "старый трос не сирота"
    placed_stop_ids = {f"S-{i + 1}" for i in range(len(b.stops))}
    assert placed_stop_ids - set(b.stop_cancels) == {q["stop_id"]}, "на бирже ровно один живой трос"
    # лаг портфеля после своего закрытия: лоты ещё «лежат» — фантома нет
    b.pf_positions = [{"figi": FIGI, "qty": -4, "avg": 100.4}]
    assert q.get("levels_placeholder") and q.get("take") is None, "принятая руками позиция — уровни временные"
    await sc.p._close_all(99.0, "решение перепроверки: закрыть", reanalyze=False)
    assert sc.p.position is None and sc.p._closed_ts > 0, sc.p.last_action
    n_pnl, n_st = len(sc.p.pnls), len(b.stops)
    sc.p._tick_n = ai_pilot.RECONCILE_EVERY - 1
    await sc.tick(90.0)
    assert sc.p.position is None and "жду подтверждения" in sc.p.last_action, sc.p.last_action
    b.pf_positions = [{"figi": FIGI, "qty": 0}]
    sc.p._tick_n = ai_pilot.RECONCILE_EVERY - 1
    await sc.tick(90.0)
    await sc.tick(90.0)
    assert sc.p.position is None and len(sc.p.pnls) == n_pnl and len(b.stops) == n_st, "фантома нет"
    sc.note = "докупил → 11, продал → 6 (P/L в журнал), перевернул → short 4, лаг портфеля без фантома"


async def s11_close_in_flight(sc: Scene) -> None:
    """CLOSE совета при заявке входа в полёте — частичка закрыта, остаток снят."""
    b = sc.broker
    b.fill_next = False
    assert sc.p.adopt_forecast(sc.ex("BUY"))
    await sc.tick(100.0)
    assert sc.p.pending
    b.partial[sc.p.pending["order_id"]] = 3
    assert sc.p.adopt_forecast({"exec": {"do": "CLOSE", "why": "картина сломалась"}})
    assert sc.p._cancel_entry and sc.p._close_pending and "снимаю заявку" in sc.p.last_action
    await sc.tick(100.0)                          # заявка снята, частичка 3 лот → позиция
    assert sc.p.position and sc.p.position["lots"] == 3 and b.cancelled, sc.p.last_action
    await sc.tick(100.0)                          # CLOSE закрывает частичку
    assert sc.p.position is None and "приказ совета: закрыть" in sc.p.last_action
    closes = [x for x in b.placed if x["tag"] == "aip-close"]
    assert len(closes) == 1 and closes[0]["lots"] == 3 and closes[0]["direction"] == SELL
    assert sc.p._close_pending is None and sc.p.state == "ЖДУ_ПЛАН"
    tr = store_v5.trades(TICKER)[0]
    assert tr["lots"] == 3 and "приказ совета: закрыть" in tr["why"], tr
    sc.note = "заявка 8 → снята, налитые 3 закрыты приказом CLOSE, журнал: 3 лот"


async def s12_killswitch(sc: Scene) -> None:
    """Дневной лимит killswitch — всё стоит до нового дня (приказы и события не принимаются)."""
    pos = await sc.open_position("long", inv=98.0)
    sc.p.session_risk.locked, sc.p.session_risk.reason = True, "дневной лимит −6% (тест)"
    await sc.tick(99.0)
    assert sc.p.position is None and sc.p.state == "СТОП" and "killswitch" in sc.p.last_action, sc.p.last_action
    n_o = len(sc.broker.placed)
    assert sc.p.adopt_forecast(sc.ex("BUY")) is False and "killswitch" in sc.p.last_action
    sc.p.review_ts = 0.0
    t0 = time.time()
    sc.p._px_hist = [(t0 - 400, 99.0), (t0 - 300, 99.0), (t0 - 200, 99.0), (t0 - 100, 99.0)]
    await sc.tick(101.5)                          # резкий ход и плановая перепроверка — стоим
    assert fake_ai.count("mission_review") == 0 and fake_ai.count("mission_guard") == 0
    assert not any(h["kind"] == "shock" for h in sc.m.handoffs) and len(sc.broker.placed) == n_o
    assert mission.status(TICKER)["phase"] == "stopped" and "ЗАБЛОКИРОВАН" in sc.p._situation_text(101.5)
    sc.p._sr_day -= 1                             # новый торговый день МСК
    sc.p.review_ts = time.time() + 1800
    await sc.tick(101.5)
    assert sc.p.state == "ЖДУ_ПЛАН" and not sc.p.session_risk.locked and sc.p.review_ts <= time.time() + 300
    assert sc.p.adopt_forecast(sc.ex("BUY")) is True
    sc.note = "лимит дня → закрыто, СТОП, ни приказов, ни PRO; новый день — снова в строю"


async def s13_flash_silent(sc: Scene) -> None:
    """FLASH у троса молчит (таймаут) — стоп по правилу, объяснение толмача."""
    sc.patch(ai_pilot, "GUARD_TIMEOUT", 0.3)
    pos = await sc.open_position("long", inv=98.0)
    fake_ai.queue("mission_guard", FakeAI.SILENT)
    await sc.tick(97.6)
    assert pos.get("guard_busy"), sc.p.last_action
    await sc.wait_guard()
    assert sc.p.position is None and "стоп по правилу" in store_v5.trades(TICKER)[0]["why"], sc.p.last_action
    g = sc.p.guards[-1]
    # ревью 5.4.2: выход по правилу — действие кода, не решение ИИ «СЛИТЬ»
    assert g["decision"] == "НЕТ_ОТВЕТА" and g["silent"] and g["source"] == "код" and g["applied"] == "по правилу: СЛИТЬ" \
        and f"{MM()} не ответил" in g["why"], g
    assert fake_ai.count("mission_guard") == 1
    await sc.settle_ai()
    assert any(f"{MM()} у троса: не ответил — слито по правилу" in e["title"] and "не ответил" in e["detail"]
               for e in sc.xevents("guard")), sc.xevents()
    assert any("Позиция закрыта" in e["title"] for e in sc.xevents("close"))
    tw = store_v5.trades(TICKER)[0]["why"]
    assert tw.startswith(f"мягкий стоп по правилу: {MM()} не ответил") and "решил слить" not in tw, tw
    assert "ответа не было — выход по правилу" in sc.p._guards_text() and "трос: НЕТ_ОТВЕТА" not in sc.p._guards_text()
    sc.note = "таймаут FLASH → слив по правилу (запись кода, не решение ИИ), толмач объяснил"


async def s14_pro_silent(sc: Scene) -> None:
    """PRO на перепроверке молчит — позиция держится, повод не теряется (доходит до следующей)."""
    pos = await sc.open_position("long", inv=98.0, age_s=1300)
    await mission.on_serious_news({"note": "Отчёт хуже ожиданий", "severity": 90})
    assert sc.p._review_reason and "Отчёт" in sc.p._review_reason
    fake_ai.queue("mission_review", FakeAI.SILENT)
    sc.p.review_ts = 0.0
    await sc.tick(100.0)
    assert await sc.settle(lambda: fake_ai.count("mission_review") == 1 and not sc.p._review_busy)
    assert sc.p.position is pos and sc.p.state == "В_ПОЗИЦИИ", "молчание PRO — позиция цела"
    assert sc.p._review_reason and "Отчёт" in sc.p._review_reason, "повод не потерян"
    assert not sc.m.reviews, "молчание — не решение"
    fake_ai.queue("mission_review", {"choice": "ЖДЁМ", "why": "отчёт уже в цене", "note": "держим"})
    sc.p.review_ts = 0.0
    await sc.tick(100.0)
    assert await sc.settle(lambda: fake_ai.count("mission_review") == 2 and not sc.p._review_busy)
    assert "Отчёт хуже ожиданий" in fake_ai.last_user["mission_review"] and sc.m.reviews[-1]["choice"] == "ЖДЁМ"
    assert sc.p._review_reason is None and sc.p.position is pos
    sc.note = "PRO молчал → позиция держится; повод дошёл до следующей перепроверки"


async def s15_stop_refused(sc: Scene) -> None:
    """Биржа отбивает выставление стоп-заявки — виртуальный стоп, повтор, толмач."""
    sc.patch(config, "PYTHIA_EXCHANGE_STOP", True)     # сцена про трос НА БИРЖЕ (умолчание 5.4.1 — только в программе)
    b = sc.broker
    b.stop_ok = False
    assert sc.p.adopt_forecast(sc.ex("BUY", inv=98.0))
    await sc.tick(100.0)
    await sc.tick(100.0)
    pos = sc.p.position
    assert pos and pos["lots"] == 8 and not pos.get("stop_id") and "ТРОС НЕ ВСТАЛ" in sc.p.last_action, sc.p.last_action
    assert pos.get("restop") and pos.get("restop_after", 0) > time.time(), "повтор назначен с бэкоффом"
    assert not b.stops
    await sc.settle_ai()
    xr = [e for e in sc.xevents("refusal")]
    assert xr and "стоп-заявку" in xr[-1]["title"] and "30079" in xr[-1]["detail"], xr
    await sc.tick(100.0)                          # бэкофф не вышел — биржу не штурмуем
    assert not b.stops and pos.get("restop")
    pos["restop_after"] = 0.0
    await sc.tick(100.0)                          # повтор: снова отбито → снова ждём
    assert not b.stops and pos.get("restop") and pos.get("restop_after", 0) > time.time()
    b.stop_ok = True
    pos["restop_after"] = 0.0
    await sc.tick(100.0)                          # биржа ожила — трос встал
    assert pos.get("stop_id") == "S-1" and b.stops[-1]["stop"] == pos["hard_stop"] and not pos.get("restop")
    # пока троса на бирже не было, виртуальный стоп всё равно держит: за тросом — закрытие
    b.stop_ok = False
    sc.p.pnls, sc.p.session_risk = [], trader_risk.SessionRisk(DEPOSIT)
    await sc.tick(pos["take"] + 0.5)              # освободить сцену тейком
    pos2 = await sc.open_position("long", inv=98.0)
    assert not pos2.get("stop_id")
    await sc.tick(pos2["hard_stop"] - 1.0)
    assert sc.p.position is None and "аварийный трос" in store_v5.trades(TICKER)[0]["why"] and fake_ai.count("mission_guard") == 0
    sc.note = "трос не встал → виртуальный стоп, повтор с бэкоффом, толмач; биржа ожила → трос лёг"


async def s16_flip_quiet(sc: Scene) -> None:
    """Переворот по свежему совету при позиции моложе тишины — переворота нет, но выход исполнен (v5.4.2: совет сказал
    другую сторону — позиция закрыта без переворота, вход в другую сторону решит PRO); позже — переворот исполнен."""
    sc.patch(config, "PYTHIA_EXCHANGE_STOP", True)     # сцена про трос НА БИРЖЕ (умолчание 5.4.1 — только в программе)
    pos = await sc.open_position("long", inv=98.0)
    assert sc.p.adopt_forecast(sc.ex("SELL", None, 90.0, 102.0)) is False
    assert sc.p.position is pos and "переворот отклонён" in sc.p.last_action and sc.p.plan is None
    assert sc.p._close_pending and "закрываю без переворота" in sc.p.last_action, sc.p.last_action
    assert sc.p.review_ts <= time.time() + 300, "перепроверка через 5 мин решит сама"
    assert "закрыта без переворота" in (sc.p._review_reason or ""), sc.p._review_reason
    n_o = len(sc.broker.placed)
    await sc.tick(100.0)                          # выход: закрыть лонг, шорт не открывать
    assert sc.p.position is None and len(sc.broker.placed) == n_o + 1 and sc.broker.placed[-1]["tag"] == "aip-close"
    await sc.tick(100.0)
    assert sc.p.position is None and sc.p.pending is None and len(sc.broker.placed) == n_o + 1, "переворота нет"
    pos = await sc.open_position("long", inv=98.0)
    pos["opened_ts"] -= float(config.PYTHIA_FLIP_QUIET_SEC) + 100
    assert sc.p.adopt_forecast(sc.ex("SELL", None, 90.0, 102.0)) is True and sc.p.plan["side"] == "short"
    await sc.tick(100.0)                          # флип: закрыть лонг
    assert sc.p.position is None and "флип" in sc.p.last_action
    await sc.tick(100.0)                          # вход в шорт
    await sc.tick(100.0)
    q = sc.p.position
    assert q and q["side"] == "short" and q["lots"] == 8 and q["hard_stop"] > 102.0 and q["take"] == 90.0, q
    tags = [x["tag"] for x in sc.broker.placed]
    assert tags == ["aip-entry", "aip-close", "aip-entry", "aip-close", "aip-entry"], tags
    assert sc.broker.stops[-1]["direction"] == BUY
    # переворот по совету, позванному мягким стопом, — без тишины
    q["opened_ts"] = time.time()
    sc.p._council_kind = "stop"
    assert sc.p.adopt_forecast(sc.ex("BUY", None, 110.0, 98.0)) is True and sc.p.plan["side"] == "long"
    sc.note = "молодая позиция — переворота нет, выход по слову совета исполнен; после тишины — закрыт лонг, открыт шорт"


async def s17_topup_zero(sc: Scene) -> None:
    """Добор по приказу, когда биржа даёт 0 лотов — честный отказ без входа."""
    b = sc.broker
    b.mx = {"buy": 8, "sell": 8}
    sc.p.deposit_override = None
    pos = await sc.open_position("long", inv=98.0)
    assert sc.p._sized_by_broker and pos["lots"] == 8
    b.mx = {"buy": 0, "sell": 16}
    sc.p._mx = None
    pos["topup_left"] = 0
    n_o = len(b.placed)
    fake_ai.queue("mission_review", {"choice": "ДОБРАТЬ", "why": "ещё", "invalidation": 98.0, "take": 110.0})
    await sc.p._review(100.0)
    assert sc.p.plan and sc.p.plan["side"] == "long" and "ДОБРАТЬ → long" in sc.p.last_action
    await sc.tick(100.0)
    assert sc.p.plan is None and "добор невозможен" in sc.p.last_action, sc.p.last_action
    assert len(b.placed) == n_o and sc.p.pending is None and pos["lots"] == 8
    # тот же приказ от совета (BUY «сейчас» той же стороны) — добор по свежему приказу тоже честно 0
    assert sc.p.adopt_forecast(sc.ex("BUY", inv=97.5)) and pos["invalidation"] == 97.5
    pos["last_fill_ts"], sc.p._tick_n = 0.0, 5
    await sc.tick(100.0)
    assert len(b.placed) == n_o and pos["topup_left"] == 0, (b.placed, pos)
    assert "биржа даёт" in sc.p._account_line() and "max_buy" in json.dumps(sc.p.status()["account"])
    sc.note = "GetMaxLots buy=0 → «добор невозможен», ордеров нет, позиция 8"


async def s18_double_close(sc: Scene) -> None:
    """Две задачи закрывают одновременно (FLASH «слить» и петля за тросом) — один ордер."""
    b = sc.broker
    b.close_delay = 0.2
    pos = await sc.open_position("long", inv=98.0)
    fake_ai.queue("mission_guard", {"decision": "СЛИТЬ", "why": "поток продавцов"})
    await sc.tick(97.6)                           # FLASH → СЛИТЬ → _close_all ждёт биржу
    await sc.settle(lambda: bool(pos.get("closing")), n=100)
    assert sc.p.position is pos and pos.get("closing"), sc.p.last_action
    await sc.tick(pos["hard_stop"] - 1.0)         # петля за тросом: второй ордер не шлётся
    assert "второй ордер не шлю" in sc.p.last_action, sc.p.last_action
    await sc.wait_guard()
    await sc.settle(lambda: sc.p.position is None, n=100)
    assert sc.p.position is None
    closes = [x for x in b.placed if x["tag"] == "aip-close"]
    assert len(closes) == 1 and len(sc.p.pnls) == 1 and store_v5.trades_summary(TICKER)["count"] == 1
    assert b.net == 0, "на счёте ровно ноль — голого разворота нет"
    sc.note = "FLASH СЛИТЬ + трос в одном тике → один aip-close, один P/L"


async def s19_take_hold_council(sc: Scene) -> None:
    """Мягкий тейк: FLASH ПОДЕРЖАТЬ → прибыль заперта триггером (не ниже входа + 50 % хода), трос биржи
    переставлен за ним, тейк отодвинут на tp_next, задача Совету без очереди → совет держит с новыми
    уровнями; во второй сцене — CLOSE; предел «подержать» → фиксация без вопроса."""
    sc.patch(config, "PYTHIA_EXCHANGE_STOP", True)     # сцена про трос НА БИРЖЕ (умолчание 5.4.1 — только в программе)
    pos = await sc.open_position("long", inv=98.0, take=103.0)
    entry, sid0 = pos["entry"], pos["stop_id"]
    sc.p._last_reanalyze_ts = time.time()         # советы «только что» — тейк обязан пройти без очереди
    sc.m.council_ts = time.time()
    fake_ai.queue("mission_take", {"decision": "ПОДЕРЖАТЬ", "why": "импульс не выдохся, лента за нас",
                                   "lock_price": 100.5, "tp_next": 106.0, "hold_minutes": 10})
    fake_ai.queue("mission_exec", {"do": "HOLD", "entry": None, "take": 107.0, "invalidation": 101.0, "why": "держать выше"})
    fake_ai.gate = asyncio.Event()                # совет придержан: сначала проверяем, что сделал FLASH
    await sc.tick(103.4)
    await sc.wait_guard()
    floor = entry + (103.0 - entry) * 0.5
    assert sc.p.position is pos and pos["take_holds"] == 1, sc.p.last_action
    assert abs(pos["invalidation"] - floor) <= 0.0051 and pos["inv0"] == pos["invalidation"], "lock ниже пола → пол (вход + 50 % хода, шаг цены)"
    assert pos["hard_stop"] < floor and pos["hard_stop"] > entry, "трос биржи за новым триггером, но выше входа"
    assert pos["take"] == 106.0 and pos.get("restop") and "ПОДЕРЖАТЬ" in sc.p.last_action, pos
    g = sc.p.guards[-1]
    assert g["side"] == "take" and g["decision"] == "ПОДЕРЖАТЬ" and abs(g["lock_price"] - floor) <= 0.0051 and g["tp_next"] == 106.0
    assert sc.m.handoffs[-1]["kind"] == "take" and not sc.m.handoffs[-1]["deferred"] and "мягкий тейк" in sc.m.handoffs[-1]["reason"]
    assert sc.p._reanalyzing and "ПОДЕРЖАТЬ" in sc.p.last_action, sc.p.last_action
    await sc.tick(103.4)                          # пока совет думает: трос биржи уже переставлен за запертую прибыль
    assert sid0 in sc.broker.stop_cancels and sc.broker.stops[-1]["stop"] == pos["hard_stop"] and pos["hard_stop"] > entry
    sid1 = pos["stop_id"]
    fake_ai.gate.set()
    await sc.wait_council()
    assert fake_ai.count("mission_exec") == 1 and "ОТКРЫТАЯ ПОЗИЦИЯ" in fake_ai.last_user["mission_exec"], fake_ai.summary()
    assert sc.p.position is pos and pos["invalidation"] == 101.0 and pos["take"] == 107.0, "совет держит с новыми уровнями"
    assert pos["take_holds"] == 0, "новый приказ совета — счётчик «подержать» заново"
    await sc.tick(103.4)                          # трос биржи переставлен за уровень совета
    assert sid1 in sc.broker.stop_cancels and sc.broker.stops[-1]["stop"] == pos["hard_stop"] and pos["hard_stop"] < 101.0
    assert sc.p.position is pos and sc.p.state == "В_ПОЗИЦИИ" and fake_ai.count("mission_take") == 1
    await sc.settle_ai()
    assert any(f"{MM()} у тейка: ПОДЕРЖАТЬ" in e["title"] and "заперта" in e["detail"] for e in sc.xevents("take")), sc.xevents()
    # вторая сцена: ПОДЕРЖАТЬ → совет говорит CLOSE → закрыто в плюс
    fake_ai.queue("mission_take", {"decision": "ПОДЕРЖАТЬ", "why": "ещё растёт", "tp_next": 112.0})
    fake_ai.queue("mission_exec", {"do": "CLOSE", "why": "цель взята — в кассу"})
    fake_ai.gate = asyncio.Event()
    await sc.tick(107.5)
    await sc.wait_guard()
    assert sc.p.position is pos and pos["take"] == 112.0 and pos["take_holds"] == 1, pos
    fake_ai.gate.set()
    await sc.wait_council()
    assert sc.m.exec["do"] == "CLOSE" and sc.p._close_pending
    await sc.tick(107.5)
    assert sc.p.position is None and sc.p.pnls[-1] > 0 and "приказ совета: закрыть" in store_v5.trades(TICKER)[0]["why"]
    # предел «подержать» (PYTHIA_SOFT_TAKE_MAX_HOLDS=2): третий раз FLASH не спрашивают — фиксация
    sc.p.pnls, sc.p.session_risk = [], trader_risk.SessionRisk(DEPOSIT)
    pos3 = await sc.open_position("long", inv=98.0, take=103.0)
    pos3["take_holds"] = 2
    n_take = fake_ai.count("mission_take")
    await sc.tick(103.4)
    assert sc.p.position is None and fake_ai.count("mission_take") == n_take and "предел" in store_v5.trades(TICKER)[0]["why"]
    sc.note = "ПОДЕРЖАТЬ → триггер на полу прибыли, трос за ним, тейк 106 → совет HOLD держит; CLOSE закрыл в плюс; предел"


async def s20_take_flash_silent(sc: Scene) -> None:
    """FLASH у тейка молчит (таймаут) — фиксация по правилу, толмач объяснил."""
    sc.patch(ai_pilot, "TAKE_TIMEOUT", 0.3)
    pos = await sc.open_position("long", inv=98.0, take=103.0)
    fake_ai.queue("mission_take", FakeAI.SILENT)
    await sc.tick(103.4)
    assert pos.get("guard_busy") and pos.get("guard_side") == "take", sc.p.last_action
    await sc.wait_guard()
    assert sc.p.position is None and "фиксация по правилу" in store_v5.trades(TICKER)[0]["why"], sc.p.last_action
    assert sc.p.pnls[-1] > 0 and "ПОБЕДА: тейк" in store_v5.trades(TICKER)[0]["why"]
    g = sc.p.guards[-1]
    assert g["side"] == "take" and g["decision"] == "НЕТ_ОТВЕТА" and g["silent"] and g["source"] == "код" \
        and g["applied"] == "по правилу: ЗАФИКСИРОВАТЬ" and f"{MM()} не ответил" in g["why"], g
    assert fake_ai.count("mission_take") == 1 and fake_ai.count("mission_review") == 0
    await sc.settle_ai()
    assert any(f"{MM()} у тейка: не ответил — зафиксировано по правилу" in e["title"] and "не ответил" in e["detail"]
               for e in sc.xevents("take")), sc.xevents()
    assert any("Позиция закрыта" in e["title"] for e in sc.xevents("close"))
    sc.note = "таймаут FLASH у тейка → фиксация по правилу (запись кода), прибыль в кассе, толмач объяснил"


async def s21_take_lock_pullback(sc: Scene) -> None:
    """ПОДЕРЖАТЬ у тейка, затем откат: прибыль заперта — у нового триггера FLASH у троса СЛИТЬ → закрыто в
    плюс; гэп за новый трос биржи — закрыто в плюс без вопросов."""
    sc.patch(config, "PYTHIA_EXCHANGE_STOP", True)     # сцена про трос НА БИРЖЕ (умолчание 5.4.1 — только в программе)
    pos = await sc.open_position("long", inv=98.0, take=103.0)
    entry = pos["entry"]
    sc.p._last_reanalyze_ts = time.time()
    sc.m.council_ts = time.time()
    fake_ai.queue("mission_take", {"decision": "ПОДЕРЖАТЬ", "why": "разгон", "lock_price": 102.0, "tp_next": None})
    fake_ai.queue("mission_exec", {"do": "BUY", "entry": None, "take": None, "invalidation": 102.0, "why": "трейлим"})
    fake_ai.gate = asyncio.Event()
    await sc.tick(103.4)
    await sc.wait_guard()
    assert sc.p.position is pos and pos["invalidation"] == 102.0 and pos["take"] is None, "lock выше пола — берём его; тейк снят"
    assert pos["hard_stop"] > entry and pos["hard_stop"] < 102.0, "трос биржи за триггером, выше входа: прибыль заперта"
    fake_ai.gate.set()
    await sc.wait_council()
    assert sc.p.position is pos and pos["invalidation"] == 102.0 and pos["take"] is None
    await sc.tick(103.0)                          # трос переставлен
    hard = pos["hard_stop"]
    assert sc.broker.stops[-1]["stop"] == hard and pos["stop_id"]
    # откат к запертой прибыли → FLASH у троса (стоп, не тейк) → СЛИТЬ → закрыто в плюс
    fake_ai.queue("mission_guard", {"decision": "СЛИТЬ", "why": "откат с объёмом — забираем"})
    await sc.tick(101.9)
    assert pos.get("guard_busy") and pos.get("guard_side") != "take" and "за триггером" in sc.p.last_action, sc.p.last_action
    await sc.wait_guard()
    assert sc.p.position is None and sc.p.pnls[-1] > 0, (sc.p.last_action, sc.p.pnls)
    tr = store_v5.trades(TICKER)[0]
    assert f"{MM()} решил слить" in tr["why"] and tr["pnl"] > 0 and abs(tr["pnl"] - (101.9 - entry) * 8) < 1e-6, tr
    assert fake_ai.count("mission_take") == 1 and fake_ai.count("mission_guard") == 1
    # вторая сцена: гэп сразу за новый трос биржи — закрытие без вопросов, тоже в плюс
    sc.p.pnls, sc.p.session_risk = [], trader_risk.SessionRisk(DEPOSIT)
    pos2 = await sc.open_position("long", inv=98.0, take=103.0)
    fake_ai.queue("mission_take", {"decision": "ПОДЕРЖАТЬ", "why": "ещё", "lock_price": 102.5})
    fake_ai.queue("mission_exec", {"do": "BUY", "entry": None, "take": None, "invalidation": 102.5, "why": "трейлим"})
    await sc.tick(103.4)
    await sc.wait_guard()
    await sc.wait_council()
    assert pos2["invalidation"] == 102.5, pos2
    hard2 = pos2["hard_stop"]
    assert hard2 > pos2["entry"]
    await sc.tick(hard2 - 0.5)
    assert sc.p.position is None and sc.p.pnls[-1] > 0 and "аварийный трос" in store_v5.trades(TICKER)[0]["why"]
    assert fake_ai.count("mission_guard") == 1, "за тросом — без вопросов"
    sc.note = "прибыль заперта триггером 102: откат → FLASH у троса СЛИТЬ в плюс; гэп за трос → в плюс без вопросов"


async def s22_event_triage(sc: Scene) -> None:
    """Триаж событий FLASH при позиции: резкий ход → ПЛАНОВО (PRO не дёргают, повод к плановой); САМ с
    «подтянуть_трос» → триггер ближе без PRO; FLASH молчит → PRO как раньше; серьёзная новость → СЕЙЧАС →
    PRO с пейсингом; выключен конфигом → сразу PRO; в тишине после входа триажа нет."""
    sc.patch(config, "PYTHIA_PROFIT_THINK", False)   # 5.4.1: рывок в плюсе идёт в мысль о прибыли (сцена 38); здесь — триаж как раньше
    sc.patch(config, "PYTHIA_EXCHANGE_STOP", True)     # сцена про трос НА БИРЖЕ (умолчание 5.4.1 — только в программе)
    sc.patch(mission, "EVENT_TRIAGE_TIMEOUT", 0.3)
    pos = await sc.open_position("long", inv=98.0, take=120.0, age_s=1300)
    t0 = time.time()
    sc.p.review_ts, sc.p._last_review_ts = t0 + 1800, t0
    n_h = len(sc.m.handoffs)
    # 1) ПЛАНОВО: ход в свою сторону — PRO не нужен, повод копится
    sc.p._px_hist = [(t0 - 400, 100.0), (t0 - 300, 100.0), (t0 - 200, 100.0), (t0 - 100, 100.0)]
    fake_ai.queue("event_triage", {"urgency": "ПЛАНОВО", "action": None, "why": "ход в нашу сторону, в русле плана"})
    await sc.tick(102.0)
    await sc.wait_triage()
    assert fake_ai.count("event_triage") == 1, fake_ai.summary()
    ut = fake_ai.last_user["event_triage"]
    for piece in ("СОБЫТИЕ: резкий ход: +2.00%", "ХОД ЦЕНЫ", "ПЛАН И УРОВНИ", "триггер (мягкий стоп) 98.0", "СЕЙЧАС, ПЛАНОВО или САМ?"):
        assert piece in ut, (piece, ut[:600])
    assert sc.p.triages[-1]["urgency"] == "ПЛАНОВО" and sc.p.review_ts >= t0 + 1790, "PRO не подтянут"
    h = sc.m.handoffs[-1]
    assert len(sc.m.handoffs) == n_h + 1 and h["kind"] == "shock" and h["deferred"] and h["triage"].startswith("ПЛАНОВО"), h
    assert "резкий ход" in (sc.p._review_reason or "") and "ПЛАНОВО" in sc.p.last_action and "плановой" in sc.p.last_action
    assert sc.m.sizes["triage"]["prompt"] == len(ut) and fake_ai.count("mission_review") == 0
    # 2) САМ: подтянуть трос — триггер ближе к цене, трос биржи на месте, PRO не нужен
    t1 = time.time()
    sc.p._px_hist = [(t1 - 400, 102.0), (t1 - 300, 102.0), (t1 - 200, 102.0), (t1 - 100, 102.0)]
    sc.p._last_shock_ts = 0.0
    hard0, sid0 = pos["hard_stop"], pos["stop_id"]
    fake_ai.queue("event_triage", {"urgency": "САМ", "action": "подтянуть_трос", "trigger": 101.0, "why": "прибыль есть — подтянем"})
    await sc.tick(104.2)
    await sc.wait_triage()
    tr = sc.p.triages[-1]
    assert tr["urgency"] == "САМ" and tr["action"] == "подтянуть_трос" and tr["trigger"] == 101.0 and "подтянут" in tr["done"], tr
    assert pos["invalidation"] == 101.0 and pos["inv0"] == 98.0 and pos["hard_stop"] == hard0 and pos["stop_id"] == sid0, pos
    assert sc.m.handoffs[-1]["deferred"] and "САМ" in sc.m.handoffs[-1]["triage"] and sc.p.review_ts >= t0 + 1790
    assert fake_ai.count("mission_review") == 0 and fake_ai.count("event_triage") == 2
    await sc.settle_ai()
    assert any(e["title"].startswith(f"Триаж {MM()}") for e in sc.xevents("triage")), [e["title"] for e in sc.xevents()]
    # 2б) САМ с уровнем не между триггером и ценой → триггер не тронут
    t2 = time.time()
    sc.p._px_hist = [(t2 - 400, 104.0), (t2 - 300, 104.0), (t2 - 200, 104.0), (t2 - 100, 104.0)]
    sc.p._last_shock_ts = 0.0
    fake_ai.queue("event_triage", {"urgency": "САМ", "action": "подтянуть_трос", "trigger": 107.0, "why": "ошибся стороной"})
    await sc.tick(106.3)
    await sc.wait_triage()
    assert pos["invalidation"] == 101.0 and "не тронут" in sc.p.triages[-1]["done"], sc.p.triages[-1]
    # 3) FLASH молчит → PRO как раньше (внеплановая перепроверка подтянута)
    t3 = time.time()
    sc.p._px_hist = [(t3 - 400, 106.0), (t3 - 300, 106.0), (t3 - 200, 106.0), (t3 - 100, 106.0)]
    sc.p._last_shock_ts, sc.p._last_event_review_ts = 0.0, 0.0
    fake_ai.queue("event_triage", FakeAI.SILENT)
    await sc.tick(108.5)
    await sc.wait_triage()
    assert sc.p.triages[-1]["urgency"] == "СЕЙЧАС" and "не ответил" in sc.p.triages[-1]["why"]
    assert sc.p._review_pulled and sc.p.review_ts <= time.time() + mission.EVENT_MIN_GAP_SEC + 1, "PRO как раньше"
    assert not sc.m.handoffs[-1]["deferred"]
    # 4) серьёзная новость → СЕЙЧАС (триаж дождались внутри on_serious_news)
    sc.p.review_ts, sc.p._review_pulled = time.time() + 1800, False
    fake_ai.queue("event_triage", {"urgency": "СЕЙЧАС", "action": None, "why": "новость ломает идею"})
    await mission.on_serious_news({"note": "Отчёт хуже ожиданий", "severity": 90})
    assert not (sc.p._triage_task and not sc.p._triage_task.done()) and sc.p.triages[-1]["urgency"] == "СЕЙЧАС"
    assert sc.m.handoffs[-1]["kind"] == "news" and sc.m.handoffs[-1]["triage"].startswith("СЕЙЧАС: новость ломает")
    assert sc.p.review_ts <= time.time() + float(config.PYTHIA_EVENT_COOL_SEC) + 1 and "Отчёт" in sc.p._review_reason
    # 5) перепроверка PRO видит все поводы и вердикты триажа
    sc.p.review_ts = 0.0
    fake_ai.queue("mission_review", {"choice": "ЖДЁМ", "why": "держим", "note": "ок"})
    await sc.tick(108.5)
    assert await sc.settle(lambda: fake_ai.count("mission_review") == 1 and not sc.p._review_busy)
    ur = fake_ai.last_user["mission_review"]
    assert "резкий ход: +2.00%" in ur and "Отчёт хуже ожиданий" in ur and sc.p._review_reason is None
    # 6) триаж выключен конфигом → сразу PRO; при позиции моложе тишины — триажа тоже нет
    sc.patch(config, "PYTHIA_EVENT_TRIAGE", False)
    n_tr = fake_ai.count("event_triage")
    sc.p.review_ts, sc.p._last_review_ts, sc.p._last_event_review_ts = time.time() + 1800, 0.0, 0.0
    await mission.on_serious_news({"note": "Ещё новость", "severity": 90})
    assert fake_ai.count("event_triage") == n_tr and sc.m.handoffs[-1]["kind"] == "news" and not sc.m.handoffs[-1].get("triage")
    sc.patch(config, "PYTHIA_EVENT_TRIAGE", True)
    pos["opened_ts"] = time.time()
    # v5.4.2: молодая позиция — событие всё равно идёт к триажу: СЕЙЧАС/ПЛАНОВО решает ИИ, а не тишина кода
    sc.p.review_ts = time.time() + 1800
    fake_ai.queue("event_triage", {"urgency": "ПЛАНОВО", "action": None, "why": "позиция молодая, трос рядом — подождёт"})
    await mission.on_serious_news({"note": "Молодая позиция", "severity": 90})
    assert fake_ai.count("event_triage") == n_tr + 1 and sc.p.triages[-1]["urgency"] == "ПЛАНОВО", sc.p.triages[-1:]
    assert sc.m.handoffs[-1]["deferred"] and "ПЛАНОВО" in sc.p.last_action and sc.p.review_ts >= time.time() + 1790, sc.p.last_action
    assert sc.p.position is pos
    sc.note = ("ПЛАНОВО → PRO не дёргают; САМ → триггер 98→101 без PRO; молчание → PRO; новость СЕЙЧАС → PRO; "
               "молодая позиция → тоже триаж")


def _punc(side: str, lo: float, hi: float, pers: float) -> dict:
    return {"side": side, "p_lo": lo, "p_hi": hi, "persistence": pers, "depth_mean": 0.9, "ticks": int(120 * pers)}


async def s23_puncture_plan(sc: Scene) -> None:
    """Прокол сканера без позиции, с планом входа (засада long у 99): прокол вниз — «предупреждение», прокол вверх —
    «вход»; в обоих случаях сразу внеплановая перепроверка PRO без триажа (позиции нет), блок ПРОКОЛ СКАНЕРА первым
    в ситуации; пейсинг PYTHIA_PUNCTURE_COOL_SEC между проколами; PRO входит по проколу вверх; тот же прокол при
    позиции — уже «подтверждение», под пейсингом молчит."""
    sc.patch(mission, "PUNCTURE_CHECK_SEC", 0.0)
    assert sc.p.adopt_forecast(sc.ex("BUY", entry=99.0, take=110.0, inv=97.0)), sc.p.last_action
    await sc.tick(101.0)
    assert sc.p.position is None and sc.p.plan and sc.p.plan["side"] == "long", sc.p.last_action
    t0 = time.time()
    sc.p.review_ts, sc.p._last_review_ts, sc.p._last_event_review_ts = t0 + 1800, 0.0, 0.0
    # 1) прокол против плана → «предупреждение», PRO разбужен сразу (триажа нет — позиции нет): перепроверка
    #    стартует тем же тиком — ответ PRO ставим в очередь заранее
    fake_ai.queue("mission_review", {"choice": "ЖДЁМ", "why": "полоса ниже засады — ждём", "note": "держим"})
    StubScan.puncture = _punc("вниз", 97.9, 98.3, 0.7)
    await sc.tick(101.0)
    pu = sc.p.puncture
    assert pu and pu["role"] == "предупреждение" and not pu["in_pos"] and "PRO решит" in pu["state"], pu
    h = sc.m.handoffs[-1]
    assert h["kind"] == "puncture" and not h["deferred"] and not h.get("triage") and fake_ai.count("event_triage") == 0, h
    assert "прокол сканера вниз 70 % (предупреждение" in h["reason"], h
    assert await sc.settle(lambda: fake_ai.count("mission_review") == 1 and not sc.p._review_busy)
    ur = fake_ai.last_user["mission_review"]
    assert ur.index("ПРОКОЛ СКАНЕРА: сторона ВНИЗ, стойкость 70 %") < ur.index("Цена сейчас"), ur[:300]
    assert "мы вне рынка, план/приказ long — прокол против плана — возможный довод против входа" in ur and "полоса 97.9–98.3" in ur, ur[:600]
    assert not sc.p.puncture["pending"] and sc.p.puncture["state"].startswith("PRO решил: ЖДЁМ")
    # 2) прокол в сторону плана — под пейсингом молчит (честная причина), после окна → «вход» → PRO входит сейчас
    StubScan.puncture = _punc("вверх", 101.4, 101.9, 0.78)
    await sc.tick(101.0)
    assert "пейсинг" in (sc.p._puncture_now or {}).get("state", ""), sc.p._puncture_now
    n_h = len(sc.m.handoffs)
    assert sc.p.puncture["side"] == "вниз" and fake_ai.count("mission_review") == 1
    sc.p._last_puncture_ts = 0.0
    sc.p.review_ts, sc.p._last_review_ts, sc.p._last_event_review_ts = time.time() + 1800, 0.0, 0.0
    fake_ai.queue("mission_review", {"choice": "КУПИТЬ_СЕЙЧАС", "why": "прокол вверх 78 % — входим", "entry": None,
                                     "invalidation": 99.5, "take": 110.0})
    await sc.tick(101.0)
    pu = sc.p.puncture
    assert pu["role"] == "вход" and pu["side"] == "вверх" and "PRO решит" in pu["state"] and len(sc.m.handoffs) == n_h + 1, pu
    assert sc.m.handoffs[-1]["kind"] == "puncture" and not sc.m.handoffs[-1]["deferred"]
    assert await sc.settle(lambda: fake_ai.count("mission_review") == 2 and not sc.p._review_busy)
    ur = fake_ai.last_user["mission_review"]
    assert ur.index("ПРОКОЛ СКАНЕРА: сторона ВВЕРХ, стойкость 78 %") < ur.index("Цена сейчас") and "возможный момент входа" in ur
    assert "ПОВОД ПЕРЕПРОВЕРКИ (внеплановая): прокол сканера вверх 78 % (вход" in ur, ur[-600:]
    assert sc.p.puncture["state"].startswith("PRO решил: КУПИТЬ")   # метка модели (ревью 5.4.3)
    await sc.tick(101.0)
    await sc.tick(101.0)
    assert sc.p.position and sc.p.position["side"] == "long", sc.p.last_action
    # 3) та же полоса при позиции — роль уже «подтверждение», но пейсинг: молчим с причиной, ИИ не тревожим
    await sc.tick(101.0)
    pn = sc.p._puncture_now or {}
    assert pn.get("role") == "подтверждение" and "пейсинг" in pn.get("state", "") and len(sc.m.handoffs) == n_h + 1, pn
    assert fake_ai.count("event_triage") == 0 and fake_ai.count("mission_review") == 2
    await sc.settle_ai()
    assert any(e["title"].startswith("Сканер видит прокол") for e in sc.xevents("puncture")), [e["title"] for e in sc.xevents()]
    sc.note = "прокол вниз → предупреждение → PRO ЖДЁМ; прокол вверх → вход → PRO КУПИТЬ_СЕЙЧАС → позиция; пейсинг честный"


async def s24_puncture_against_position(sc: Scene) -> None:
    """Прокол против зрелой позиции: «угроза» → FLASH-триаж (СЕЙЧАС) → PRO разбужен, блок ПРОКОЛ СКАНЕРА первым и у
    триажа, и у PRO; тот же прокол второй раз — дедуп (ни триажа, ни повода); другая полоса после пейсинга → триаж САМ
    подтягивает трос; прокол за нас → «подтверждение» → ПЛАНОВО (повод копится); PRO видит все поводы."""
    sc.patch(mission, "PUNCTURE_CHECK_SEC", 0.0)
    sc.patch(mission, "EVENT_TRIAGE_TIMEOUT", 0.3)
    pos = await sc.open_position("long", inv=97.0, take=120.0, age_s=1300)
    t0 = time.time()
    sc.p.review_ts, sc.p._last_review_ts, sc.p._last_event_review_ts = t0 + 1800, 0.0, 0.0
    n_h = len(sc.m.handoffs)
    # 1) угроза → триаж СЕЙЧАС → PRO (перепроверка стартует следующим тиком — ответ PRO в очередь заранее)
    StubScan.puncture = _punc("вниз", 97.2, 97.6, 0.78)
    fake_ai.queue("event_triage", {"urgency": "СЕЙЧАС", "action": None, "why": "полоса ведёт прямо к тросу"})
    fake_ai.queue("mission_review", {"choice": "ЖДЁМ", "why": "полоса 97.2–97.6 — трос 97 рядом, но держим", "note": "ок"})
    await sc.tick(100.0)
    await sc.wait_triage()
    pu = sc.p.puncture
    assert pu and pu["role"] == "угроза" and pu["in_pos"] and pu["pending"] and pu["triage"].startswith("СЕЙЧАС"), pu
    ut = fake_ai.last_user["event_triage"]
    assert ut.index("ПРОКОЛ СКАНЕРА: сторона ВНИЗ, стойкость 78 %") < ut.index("Цена сейчас"), ut[:300]
    assert "мы в позиции long — прокол против нас — возможная угроза" in ut and "СОБЫТИЕ: прокол сканера вниз 78 % (угроза" in ut
    h = sc.m.handoffs[-1]
    assert len(sc.m.handoffs) == n_h + 1 and h["kind"] == "puncture" and not h["deferred"] and h["triage"].startswith("СЕЙЧАС"), h
    assert sc.p._review_pulled and sc.p.review_ts <= time.time() + mission.EVENT_MIN_GAP_SEC + 1 and "PRO решит" in pu["state"]
    # 2) тот же прокол ещё раз — дедуп: ни триажа, ни нового повода; этим же тиком стартует разбуженный PRO
    await sc.tick(100.0)
    await sc.wait_triage()
    assert fake_ai.count("event_triage") == 1 and len(sc.m.handoffs) == n_h + 1 and sc.p._puncture_now is sc.p.puncture
    # 3) PRO: блок первым, решил ЖДЁМ — блок снят
    assert await sc.settle(lambda: fake_ai.count("mission_review") == 1 and not sc.p._review_busy)
    ur = fake_ai.last_user["mission_review"]
    assert ur.index("ПРОКОЛ СКАНЕРА") < ur.index("Цена сейчас") and "прокол сканера вниз 78 % (угроза" in ur
    assert not sc.p.puncture["pending"] and sc.p.puncture["state"].startswith("PRO решил: ДЕРЖАТЬ")   # в позиции — метка модели
    # 4) другая полоса (после пейсинга) → триаж САМ подтянуть трос — без PRO, состояние прокола «подтянут»
    StubScan.puncture = _punc("вниз", 98.4, 98.8, 0.66)
    sc.p._last_puncture_ts = 0.0
    sc.p.review_ts, sc.p._last_review_ts, sc.p._review_pulled = time.time() + 1800, 0.0, False
    fake_ai.queue("event_triage", {"urgency": "САМ", "action": "подтянуть_трос", "trigger": 98.0, "why": "подтянем к полосе"})
    await sc.tick(100.0)
    await sc.wait_triage()
    pu = sc.p.puncture
    assert pu["p_lo"] == 98.4 and pu["triage"].startswith("САМ") and "подтянут" in pu["state"] and pos["invalidation"] == 98.0, pu
    assert sc.m.handoffs[-1]["deferred"] and sc.p.review_ts >= time.time() + 1790 and fake_ai.count("mission_review") == 1
    # 5) прокол за нас → «подтверждение» → ПЛАНОВО: повод копится, PRO не тянем
    StubScan.puncture = _punc("вверх", 101.5, 102.0, 0.9)
    sc.p._last_puncture_ts = 0.0
    fake_ai.queue("event_triage", {"urgency": "ПЛАНОВО", "action": None, "why": "в нашу сторону — подождёт плановой"})
    await sc.tick(100.0)
    await sc.wait_triage()
    pu = sc.p.puncture
    assert pu["role"] == "подтверждение" and pu["triage"].startswith("ПЛАНОВО") and "плановой" in pu["state"], pu
    assert sc.m.handoffs[-1]["deferred"] and "прокол сканера вверх 90 %" in (sc.p._review_reason or "")
    # 6) плановая перепроверка PRO видит блок первым и все поводы
    sc.p.review_ts = 0.0
    fake_ai.queue("mission_review", {"choice": "ЖДЁМ", "why": "держим", "note": "ок"})
    await sc.tick(100.0)
    assert await sc.settle(lambda: fake_ai.count("mission_review") == 2 and not sc.p._review_busy)
    ur = fake_ai.last_user["mission_review"]
    assert ur.startswith("ОБЪЕКТ") and ur.index("ПРОКОЛ СКАНЕРА: сторона ВВЕРХ, стойкость 90 %") < ur.index("Цена сейчас")
    assert "прокол сканера вниз 66 %" in ur and "прокол сканера вверх 90 %" in ur, ur[-800:]
    await sc.settle_ai()
    assert sum(1 for e in sc.xevents("puncture")) >= 2, [e["title"] for e in sc.xevents()]
    sc.note = "угроза → триаж СЕЙЧАС → PRO; повтор — дедуп; САМ подтянул трос 97→98; за нас → ПЛАНОВО; PRO видел всё"


async def s25_puncture_below_threshold(sc: Scene) -> None:
    """Прокол ниже порога (стойкость 50 % < PYTHIA_PUNCTURE_MIN 60 %) — тишина с честной причиной; без позиции и
    плана (v5.4.2) — «вне рынка»: возможный вход в сторону прокола → PRO сразу (против режима игры — тишина);
    выключено конфигом — тишина; порог опущен — прокол становится поводом."""
    sc.patch(mission, "PUNCTURE_CHECK_SEC", 0.0)
    pos = await sc.open_position("long", inv=97.0, take=120.0, age_s=1300)
    n_h = len(sc.m.handoffs)
    StubScan.puncture = _punc("вниз", 97.2, 97.6, 0.5)
    await sc.tick(100.0)
    await sc.wait_triage()
    pn = sc.p._puncture_now or {}
    assert sc.p.puncture is None and "ниже порога 60 %" in pn.get("state", "") and pn.get("role") == "угроза", pn
    assert len(sc.m.handoffs) == n_h and fake_ai.count("event_triage") == 0 and fake_ai.count("mission_review") == 0
    # v5.4.2: позиции и плана нет → «вне рынка»: возможный вход в сторону прокола — PRO сразу (без триажа, позиции нет)
    sc.p.position, sc.p.plan = None, None
    sc.p.review_ts, sc.p._last_review_ts, sc.p._last_event_review_ts = time.time() + 1800, 0.0, 0.0
    sc.patch(sc.m, "play", "long")                # режим long: прокол вниз — вход в шорт закрыт владельцем → тишина
    StubScan.puncture = _punc("вниз", 97.2, 97.6, 0.9)
    await sc.tick(100.0)
    pn = sc.p._puncture_now or {}
    assert sc.p.puncture is None and "режим игры long" in pn.get("state", "") and pn.get("role") == "вне рынка", pn
    assert len(sc.m.handoffs) == n_h
    sc.patch(sc.m, "play", "auto")
    fake_ai.queue("mission_review", {"choice": "ЖДЁМ", "why": "полоса вниз, но лента двусторонняя", "note": "ок"})
    await sc.tick(100.0)
    pu = sc.p.puncture or {}
    assert pu.get("role") == "вне рынка" and pu.get("our_side") is None and len(sc.m.handoffs) == n_h + 1, pu
    assert sc.m.handoffs[-1]["kind"] == "puncture" and not sc.m.handoffs[-1]["deferred"] and fake_ai.count("event_triage") == 0
    assert "мы вне рынка без плана — прокол вне рынка — возможный момент входа" in pu.get("text", ""), pu.get("text")
    assert await sc.settle(lambda: fake_ai.count("mission_review") == 1 and not sc.p._review_busy)
    assert fake_ai.last_user["mission_review"].index("ПРОКОЛ СКАНЕРА") < fake_ai.last_user["mission_review"].index("Цена сейчас")
    sc.p._last_puncture_ts, sc.p.puncture = 0.0, None   # пейсинг проколов — дальше проверяется порог
    n_h = len(sc.m.handoffs)
    # выключено конфигом → тишина
    sc.p.position = pos
    sc.patch(config, "PYTHIA_PUNCTURE", False)
    await sc.tick(100.0)
    assert sc.p.puncture is None and len(sc.m.handoffs) == n_h and sc.p.status()["puncture_min"] is None
    sc.patch(config, "PYTHIA_PUNCTURE", True)
    # порог опущен до 40 % → прокол 50 % становится поводом (триаж по умолчанию → СЕЙЧАС → PRO)
    sc.patch(config, "PYTHIA_PUNCTURE_MIN", 0.4)
    sc.patch(mission, "EVENT_TRIAGE_TIMEOUT", 0.3)
    StubScan.puncture = _punc("вниз", 97.2, 97.6, 0.5)
    await sc.tick(100.0)
    await sc.wait_triage()
    assert sc.p.puncture and sc.p.puncture["persistence"] == 0.5 and len(sc.m.handoffs) == n_h + 1
    assert sc.m.handoffs[-1]["kind"] == "puncture" and fake_ai.count("event_triage") == 1
    assert sc.p.status()["puncture_min"] == 0.4 and "ПРОКОЛ СКАНЕРА" in fake_ai.last_user["event_triage"]
    sc.note = "50 % < 60 % → тишина; без позиции и плана → «вне рынка» → PRO (против режима — тишина); выкл → тишина; порог 40 % → повод"


async def s26_stop_request_stale(sc: Scene) -> None:
    """Запрос стопа завис > 5 мин (ответ биржи потерян): судьбу решает СПИСОК стопов биржи, а не вечное ожидание —
    гэп за трос закрывает; список недоступен → честно ждём, но ПАНИКА (повторная = force) закрывает и stop() завершает
    петлю."""
    sc.patch(config, "PYTHIA_EXCHANGE_STOP", True)     # сцена про трос НА БИРЖЕ (умолчание 5.4.1 — только в программе)
    b = sc.broker
    pos = await sc.open_position("long", inv=98.0)
    hard, sid0 = pos["hard_stop"], pos["stop_id"]

    def hang(p: dict, age: float, uncertain: bool = True) -> dict:
        """Как после потерянного ответа PostStopOrder при переносе троса: старый снят, новый — неизвестно."""
        b.stop_status[p["stop_id"]] = "cancelled"
        p["stop_id"] = None
        p["stop_request"] = {"request_id": f"lost-{int(age)}", "lots": p["lots"], "direction": SELL,
                             "price": sc.p._snap(hard), "uncertain": uncertain, "created_ts": time.time() - age}
        p["restop_after"] = 0.0
        return p["stop_request"]

    # A) биржа запрос НЕ приняла (в списке стопов его нет): запрос закрыт, трос ставится заново новым UUID, а гэп за
    #    трос в тот же тик закрывает позицию (виртуальный стоп работает и при висящем запросе)
    hang(pos, 360)
    n_st = len(b.stops)
    await sc.tick(hard - 1.5)
    assert sc.p.position is None, sc.p.last_action
    assert len(b.stops) == n_st + 1 and b.stops[-1]["stop"] == hard, "трос перевыставлен новым запросом"
    assert f"S-{n_st + 1}" in b.stop_cancels, "новый трос снят перед рыночным закрытием"
    closes = [x for x in b.placed if x["tag"] == "aip-close"]
    assert len(closes) == 1 and closes[0]["lots"] == 8 and sc.p.last_action.startswith("ЗАКРЫЛ ВСЁ (аварийный трос"), sc.p.last_action
    await sc.wait_council()
    # B) биржа запрос ПРИНЯЛА (стоп лежит в списке): он признан своим, второго стопа нет, гэп закрывает
    sc.p.pnls, sc.p.session_risk = [], trader_risk.SessionRisk(DEPOSIT)
    pos = await sc.open_position("long", inv=98.0)
    hang(pos, 400)
    b.stops.append({"figi": FIGI, "direction": SELL, "lots": 8, "stop": sc.p._snap(pos["hard_stop"]), "created": time.time() - 395})
    lost_sid = f"S-{len(b.stops)}"
    b.stop_status[lost_sid] = "active"
    n_st = len(b.stops)
    await sc.tick(pos["hard_stop"] - 1.5)
    assert sc.p.position is None and len(b.stops) == n_st and lost_sid in b.stop_cancels, (sc.p.last_action, b.stop_cancels)
    await sc.wait_council()
    # C) список стопов недоступен: за тросом закрытие честно отложено (причина названа), ПАНИКА ×3 упирается,
    #    повторная ПАНИКА (mission.panic) = force → закрыто; stop() после этого завершает петлю
    sc.p.pnls, sc.p.session_risk = [], trader_risk.SessionRisk(DEPOSIT)
    pos = await sc.open_position("long", inv=98.0)
    hang(pos, 360)
    b.stops_fail = True
    n_o = len(b.placed)
    await sc.tick(pos["hard_stop"] - 1.5)
    assert sc.p.position is pos and len(b.placed) == n_o, sc.p.last_action
    assert "список стопов биржи недоступен" in sc.p.last_action, sc.p.last_action
    assert pos.get("stop_request") and pos.get("close_fail"), pos
    sc.p.panic()
    for i in range(3):
        await sc.tick(90.0)
    assert sc.p.position is pos and len(b.placed) == n_o, sc.p.last_action
    assert "ПАНИКА владельца" in sc.p.last_action and "force" in sc.p.last_action and "отбито биржей" not in sc.p.last_action, sc.p.last_action
    assert sc.p.panic_blocked and sc.p.status()["panic_blocked"]
    r = await mission.panic(TICKER)                 # владелец жмёт ещё раз: пилот упёрся → force
    assert r["ok"] and r["panic"] == [TICKER] and "force" in sc.m.note, (r, sc.m.note)
    assert sc.p.panic_force
    await sc.tick(90.0)
    assert sc.p.position is None and len(b.placed) == n_o + 1, sc.p.last_action
    assert sc.p.state == "СТОП" and "ЗАКРЫЛ ВСЁ (ПАНИКА владельца)" in sc.p.last_action, sc.p.last_action
    sc.p.stop()
    cont = bool(sc.p.pending or any((sc.p.position or {}).get(k) for k in ("exit_order", "stop_request"))
                or (sc.p.panic_flag and sc.p.position))
    assert not cont, "петля обязана завершиться: ни запроса стопа, ни позиции"
    b.stops_fail = False
    sc.note = "запрос > 5 мин: нет в списке → новый UUID и гэп закрыл; есть в списке → свой; список недоступен → паника ×3, повтор = force"


async def s27_cancel_stop_gone(sc: Scene) -> None:
    """CancelStopOrder «не подтверждена», а стопа на бирже уже нет (снят владельцем / истёк / исполнен): список стопов
    подтверждает отмену — трос переставлен, закрытие прошло; исполненный трос учтён без рыночного ордера."""
    sc.patch(config, "PYTHIA_EXCHANGE_STOP", True)     # сцена про трос НА БИРЖЕ (умолчание 5.4.1 — только в программе)
    b = sc.broker
    pos = await sc.open_position("long", inv=98.0)
    sid0 = pos["stop_id"]
    b.cancel_stop_ok, b.cancel_stop_error = False, "50006: stop order not found (тест)"
    # 1) владелец снял трос руками → перенос троса: отмена подтверждена списком, новый трос лёг
    b.stop_status[sid0] = "cancelled"
    pos["restop"] = True
    await sc.tick(100.0)
    assert sid0 in b.stop_cancels and pos["stop_id"] == "S-2" and not pos.get("restop"), (pos, b.stop_cancels)
    assert len(b.stops) == 2 and sc.p.position is pos and pos["lots"] == 8
    # 2) закрытие при «неподтверждённой» отмене, стопа на бирже нет: закрытие прошло одним ордером
    b.stop_status["S-2"] = "cancelled"
    assert await sc.p._close_all(100.5, "приказ совета: закрыть", reanalyze=False)
    assert sc.p.position is None and len([x for x in b.placed if x["tag"] == "aip-close"]) == 1, sc.p.last_action
    # 3) трос ИСПОЛНИЛСЯ на бирже (счёт пуст), отмена «не подтверждена»: рыночный ордер не шлётся, закрытие учтено по тросу
    pos2 = await sc.open_position("long", inv=98.0)
    b.stop_status[pos2["stop_id"]] = "executed"
    b.net = 0                                       # биржа продала 8 по тросу
    n_o, n_tr = len(b.placed), store_v5.trades_summary(TICKER)["count"]
    await sc.tick(pos2["hard_stop"] - 0.5)
    assert sc.p.position is None and len(b.placed) == n_o, sc.p.last_action
    assert store_v5.trades_summary(TICKER)["count"] == n_tr + 1 and abs(sc.p.pnls[-1] - (pos2["hard_stop"] - pos2["entry"]) * 8) < 1e-6
    await sc.wait_council()
    # 4) то же при переносе троса (restop): стопа нет и счёт пуст → закрытие вне петли учтено, нового троса-сироты нет
    sc.p.pnls, sc.p.session_risk = [], trader_risk.SessionRisk(DEPOSIT)
    pos3 = await sc.open_position("long", inv=98.0)
    b.stop_status[pos3["stop_id"]] = "executed"
    b.net = 0
    n_st, n_o, n_tr = len(b.stops), len(b.placed), store_v5.trades_summary(TICKER)["count"]
    pos3["restop"] = True
    await sc.tick(100.0)
    assert sc.p.position is None and len(b.stops) == n_st and len(b.placed) == n_o, sc.p.last_action
    assert store_v5.trades_summary(TICKER)["count"] == n_tr + 1, "закрытие вне петли — в журнале"
    assert abs(sc.p.pnls[-1] - (pos3["hard_stop"] - pos3["entry"]) * 8) < 1e-6, sc.p.pnls
    assert "внешнее закрытие" in sc.p.last_action or "закрыла биржа/владелец" in sc.p.last_action, sc.p.last_action
    await sc.wait_council()
    sc.note = "стопа нет в списке → отмена подтверждена: трос переставлен, закрытие прошло; исполнен → учтено без ордера"


async def s28_unreadable_store_panic(sc: Scene) -> None:
    """База миссий / state-файл не читаются: ПАНИКА с живым пилотом всё равно закрывает (через брокера, честная
    пометка); без пилота — отказ с «повтори/force», повторное нажатие или force закрывает по счёту напрямую."""
    b = sc.broker
    sc.patch(mission, "_make_broker", lambda: b)      # закрытие по счёту идёт через брокера сцены: видны flat_calls/flat_kwargs
    pos = await sc.open_position("long", inv=98.0)

    def broken_all():
        raise OSError("database is locked (тест)")
    sc.patch(store_v5, "mission_all", broken_all)
    r = await mission.panic(TICKER)
    assert r["ok"] and r["panic"] == [TICKER] and "база миссий не читается" in r["note"], r
    assert sc.p.panic_flag and sc.m.phase == "panic"
    await sc.tick(100.0)
    assert sc.p.position is None and sc.p.state == "СТОП", sc.p.last_action
    assert len([x for x in b.placed if x["tag"] == "aip-close"]) == 1
    # без пилота: первая ПАНИКА честно отказывает (базу не прочитать) и не шлёт ничего; повтор = force → по счёту
    sc.m.task.set_result(None)
    sc.p.panic_flag = False
    r2 = await mission.panic(TICKER)
    assert not r2["ok"] and r2["failed"] == [TICKER] and "повторная ПАНИКА" in r2["note"] and not b.flat_calls, r2
    r3 = await mission.panic(TICKER)
    assert r3["ok"] and r3["panic"] == [TICKER] and r3["force"] and "закрыто по счёту" in r3["note"], r3
    assert b.flat_calls == [(FIGI, 1)] and b.flat_kwargs[-1]["force"] is True and "закрыто по счёту" in sc.m.note
    assert not sc.m.panic_force, "force одноразовый: сторож дальше не форсирует"
    # state-файл пилота не читается (мусор): без пилота — отказ с причиной, force закрывает по счёту (заявки/стопы снимутся там)
    setattr(store_v5, "mission_all", sc._saved.pop((store_v5, "mission_all")))
    mission._panic_refused.clear()
    tmp = Path(tempfile.mkdtemp(prefix="pythia_scen_"))
    (tmp / "aipilot_state.json").write_text("{мусор", encoding="utf-8")
    sc.patch(config, "DATA_DIR", tmp)
    r4 = await mission.panic(TICKER)
    assert not r4["ok"] and "state-файл пилота не читается" in r4["note"] and "force" in r4["note"], r4
    r5 = await mission.panic(TICKER, force=True)
    assert r5["ok"] and r5["force"] and "закрыто по счёту" in r5["note"], r5
    assert len(b.flat_calls) == 2 and b.flat_kwargs[-1]["force"] is True
    sc.m.task = asyncio.get_running_loop().create_future()
    sc.note = "база не читается: пилот закрыл через брокера; без пилота — отказ, повтор/force закрыл по счёту; state-мусор → force"


async def s29_panic_stops_unavailable(sc: Scene) -> None:
    """ПАНИКА при недоступном GetStopOrders: пилот — отмена троса не подтверждена, список недоступен → ×3 честно
    упирается, повторная ПАНИКА = force закрывает; без пилота — настоящий Broker.flat_all не молчит (ошибка с текстом,
    ничего не тронуто), force снимает заявки по figi (GetOrders → CancelOrder) и закрывает всё равно."""
    sc.patch(config, "PYTHIA_EXCHANGE_STOP", True)     # сцена про трос НА БИРЖЕ (умолчание 5.4.1 — только в программе)
    b = sc.broker
    pos = await sc.open_position("long", inv=98.0)
    b.cancel_stop_ok, b.stops_fail = False, True
    n_o = len(b.placed)
    r = await mission.panic(TICKER)
    assert r["ok"] and sc.p.panic_flag and not sc.p.panic_force
    for _ in range(3):
        await sc.tick(99.0)
    assert sc.p.position is pos and len(b.placed) == n_o and "force" in sc.p.last_action, sc.p.last_action
    r = await mission.panic(TICKER)
    assert r["ok"] and sc.p.panic_force, r
    await sc.tick(99.0)
    assert sc.p.position is None and len(b.placed) == n_o + 1 and sc.p.state == "СТОП", sc.p.last_action
    b.cancel_stop_ok, b.stops_fail = True, False
    # без пилота — настоящий брокер: GetStopOrders падает
    sc.m.task.set_result(None)
    sc.p.panic_flag = False
    calls: list[str] = []

    async def poster(path, body, timeout=15.0):
        name = path.rsplit("/", 1)[-1]
        calls.append(name)
        if name == "GetStopOrders":
            raise RuntimeError("503 GetStopOrders недоступен (тест)")
        if name == "GetOrders":
            return {"orders": [{"orderId": "o-entry", "figi": FIGI, "executionReportStatus": "EXECUTION_REPORT_STATUS_NEW"}]}
        if name == "GetPortfolio":
            return {"totalAmountCurrencies": {"units": 50000}, "positions": [
                {"figi": FIGI, "instrumentUid": FIGI, "instrumentType": "futures", "quantity": {"units": 8}}]}
        if name == "PostOrder":
            return {"orderId": "x-1", "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL", "lotsExecuted": 8}
        return {}
    sc.patch(trader_broker, "_audit", lambda *a, **k: None)
    real = trader_broker.Broker("dry", account_id="acc-1", poster=poster)
    real.mode = "real"
    sc.patch(mission, "_make_broker", lambda: real)
    r1 = await mission.panic(TICKER)
    assert not r1["ok"] and r1["failed"] == [TICKER] and "защитные стопы" in r1["note"] and "force" in r1["note"], r1
    assert "PostOrder" not in calls and "CancelOrder" not in calls, calls
    calls.clear()
    r2 = await mission.panic(TICKER)                # повтор = force
    assert r2["ok"] and r2["force"] and "закрыто по счёту" in r2["note"] and "не сверены" in r2["note"], r2
    assert calls.index("CancelOrder") < calls.index("PostOrder") and "GetOrders" in calls, calls
    assert not mission._M[TICKER].panic_orders
    sc.m.task = asyncio.get_running_loop().create_future()
    sc.note = "пилот: список недоступен → ×3 честно, повтор=force закрыл; без пилота: отказ с текстом, force снял заявку и закрыл"


async def s30_owner_sells_during_exit(sc: Scene) -> None:
    """Рыночная заявка выхода принята, но висит (NEW); владелец продал руками — наша заявка снята биржей: остаток
    сверяется со счётом, второй продажи нет, фантома нет; частичка учитывается один раз."""
    b = sc.broker
    pos = await sc.open_position("long", inv=98.0)
    b.market_pending = True
    assert not await sc.p._close_all(101.0, "приказ совета: закрыть", reanalyze=False)
    order = pos.get("exit_order")
    assert order and sc.p.position is pos and pos["lots"] == 8 and "жду исполнения" in sc.p.last_action, sc.p.last_action
    oid = order["order_id"]
    await sc.tick(101.0)
    closes = [x for x in b.placed if x["tag"] == "aip-close"]
    assert len(closes) == 1 and pos.get("exit_order") is order, "повторной заявки нет, пока первая жива"
    b.net = 0                                       # владелец продал 8 руками
    b._orders[oid]["cancelled"] = True              # биржа сняла нашу заявку: CANCELLED, exec 0
    await sc.tick(101.0)
    await sc.tick(101.0)
    assert sc.p.position is None, sc.p.last_action
    assert len([x for x in b.placed if x["tag"] == "aip-close"]) == 1, "второй продажи нет — голого шорта не будет"
    assert len(sc.p.pnls) == 1 and abs(sc.p.pnls[0] - (101.0 - pos["entry"]) * 8) < 1e-6, sc.p.pnls
    assert store_v5.trades_summary(TICKER)["count"] == 1 and b.net == 0
    sc.p._tick_n = ai_pilot.RECONCILE_EVERY - 1
    await sc.tick(101.0)
    assert sc.p.position is None, "фантома нет"
    await sc.wait_council()
    # частичка: биржа налила 3, владелец продал остальные 5, заявка снята — 3 и 5 учтены по одному разу, ордер один
    sc.p.pnls, sc.p.session_risk = [], trader_risk.SessionRisk(DEPOSIT)
    pos2 = await sc.open_position("long", inv=98.0)
    n_o = len(b.placed)
    assert not await sc.p._close_all(102.0, "приказ совета: закрыть", reanalyze=False)
    oid2 = pos2["exit_order"]["order_id"]
    b.partial[oid2] = 3
    await sc.tick(102.0)
    assert sc.p.position is pos2 and pos2["lots"] == 5 and len(sc.p.pnls) == 1, (pos2, sc.p.pnls)
    b.net = 0
    b._orders[oid2]["cancelled"] = True
    await sc.tick(102.0)
    await sc.tick(102.0)
    assert sc.p.position is None and len(b.placed) == n_o + 1 and len(sc.p.pnls) == 2, (sc.p.last_action, sc.p.pnls)
    assert abs(sum(sc.p.pnls) - (102.0 - pos2["entry"]) * 8) < 1e-6 and b.net == 0
    b.market_pending = False
    sc.note = "выход NEW, владелец продал → заявка снята: остаток по счёту 0, один ордер, P/L один раз; частичка 3+5"


async def s31_panic_does_not_stick(sc: Scene) -> None:
    """ПАНИКА закрыла позицию, а в state-файле осталась память проколов сканера (секция pilot живёт часами) → новый
    пилот того же тикера (ПРОДОЛЖИТЬ / новая миссия) стартует БЕЗ паники и торгует, а не рождается в СТОП;
    ПАНИКА новому пилоту работает как обычно."""
    b = sc.broker
    path = Path(tempfile.mkdtemp(prefix="pythia_scen_")) / "aipilot_state.json"
    sc.p._state_path = path
    sc.p._puncture_seen[("вниз", 195, "угроза")] = time.time()      # секция pilot непуста → файл не стирается
    await sc.open_position("long", inv=98.0)
    r = await mission.panic(TICKER)
    assert r["ok"] and r["panic"] == [TICKER]
    await sc.tick(100.0)
    assert sc.p.position is None and sc.p.state == "СТОП" and "ПАНИКА владельца" in sc.p.last_action, sc.p.last_action
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["position"] is None and not rec.get("pending") and rec.get("pilot"), rec
    assert not rec.get("panic") and not rec.get("panic_force"), "паника — свойство позиции/заявки, а не инструмента"
    # владелец: ПРОДОЛЖИТЬ / новая миссия по тикеру → новый пилот, тот же файл, позиции и заявок нет
    sc.m.task.set_result(None)
    p2 = mission.MissionPilot(TICKER, deposit=DEPOSIT, broker=b, mission=sc.m)
    p2._state_path = path
    mission._bind_pilot(sc.m, p2)
    sc.m.pilot, sc.p = p2, p2
    await p2.prepare()
    p2._restore_state(0)
    assert not p2.panic_flag and not p2.panic_force and p2._close_pending is None and p2.position is None, p2.last_action
    assert ("вниз", 195, "угроза") in p2._puncture_seen, "память проколов подхвачена — она и держала файл"
    sc.m.auto_resume, sc.m.phase = True, "idle"
    sc.m.task = asyncio.get_running_loop().create_future()
    n_e = len([x for x in b.placed if x["tag"] == "aip-entry"])
    assert p2.adopt_forecast(sc.ex("BUY", None, 110.0, 98.0)), p2.last_action
    await sc.tick(100.0)
    await sc.tick(100.0)
    assert p2.position and p2.position["lots"] == 8 and p2.state == "В_ПОЗИЦИИ", (p2.state, p2.last_action)
    assert len([x for x in b.placed if x["tag"] == "aip-entry"]) == n_e + 1, "новый пилот торгует"
    # …и ПАНИКА новому пилоту работает (флаг не потерян в другую сторону), файл снова без panic
    p2.panic()
    await sc.tick(100.0)
    assert p2.position is None and p2.state == "СТОП", p2.last_action
    rec2 = json.loads(path.read_text(encoding="utf-8"))
    assert rec2["position"] is None and not rec2.get("panic") and rec2.get("pilot"), rec2
    sc.note = "паника → файл с памятью проколов без panic; новый пилот того же тикера торгует; паника ему — работает"


async def s32_gate_wait(sc: Scene) -> None:
    """Проверка входа у двери (5.4.1): приказ «сейчас» → PRO у двери говорит ЖДАТЬ с уровнем (откат 99.2) и поправленными
    стопом/тейком → план обновлён, входа нет; цена дошла до уровня → v5.4.2: уровень назвал сам PRO по живому рынку (план
    gate_level) и решение свежее → вход без второго вопроса; протухло → вторая проверка → ВОЙТИ → вход по текущей цене;
    записи gates, статус entry_gate, шина и толмач; выключено конфигом → вход сразу без вопросов."""
    b = sc.broker
    assert sc.p.adopt_forecast(sc.ex("BUY", None, 110.0, 98.0)), sc.p.last_action
    fake_ai.queue("mission_entry", {"decision": "ЖДАТЬ", "why": "лента продавцов, лучше взять на откате к 99.2",
                                    "entry": 99.2, "entry_kind": "откат", "invalidation": 97.5, "take": 108.0, "council": False})
    await sc.tick(100.0)                          # тик: PRO у двери; Scene.tick дожидается его ответа
    assert fake_ai.count("mission_entry") == 1 and sc.p.pending is None and not b.placed, sc.p.last_action
    plan = sc.p.plan
    assert plan and plan["entry"] == 99.2 and plan["kind"] == "откат" and plan["invalidation"] == 97.5 and plan["take"] == 108.0, plan
    assert sc.p.state == "ЗАСАДА" and "ЖДАТЬ" in sc.p.last_action and "новый уровень: откат @99.2" in sc.p.last_action, sc.p.last_action
    g = sc.p.gates[-1]
    assert g["decision"] == "ЖДАТЬ" and g["entry"] == 99.2 and g["entry_kind"] == "откат" and g["plan_ts"] == plan["ts"] and g["model"] == MM(), g
    st = sc.p.status()
    assert st["entry_gate"]["decision"] == "ЖДАТЬ" and st["entry_gate"]["entry"] == 99.2 and st["entry_gate"]["busy"] is False \
        and st["entry_gate"]["checks"] == 1 and st["entry_gate"]["next_in_s"] is None, st["entry_gate"]
    assert st["gates"][-1]["why"].startswith("лента продавцов") and st["entry_check"] is True and st["money_model"] == MM().lower()
    assert mission.status(TICKER)["phase"] == "armed"
    ue = fake_ai.last_user["mission_entry"]
    for piece in ("ПРИКАЗ И ПЛАН", "ПЛАН ПИЛОТА", "long сейчас", "ЖИВОЙ РЫНОК", "ИТОГ ОБЩЕГО СОВЕТА", "СВЕЖИЕ НОВОСТИ",
                  "СКАНЕР СТАКАНА", "СВЯЗАННЫЕ БУМАГИ", "Войти сейчас, отменить или ждать уровня/срока"):
        assert piece in ue, (piece, ue[:900])
    assert sc.m.sizes["entry"]["prompt"] == len(ue) and sc.m.sizes["entry"]["answer"] > 0
    await sc.tick(99.8)                           # выше уровня — ждём, PRO не дёргаем
    assert fake_ai.count("mission_entry") == 1 and sc.p.pending is None and "засада" in sc.p.last_action, sc.p.last_action
    assert "ПРОВЕРКА ВХОДА:" in sc.p._situation_text(99.8) and "ЖДАТЬ — лента продавцов" in sc.p._situation_text(99.8)
    await sc.tick(99.22)                         # уровень достигнут → v5.4.2: уровень назвал PRO у двери (gate_level) и он свеж → вход без второго вопроса
    assert fake_ai.count("mission_entry") == 1 and sc.p.pending and sc.p.pending["side"] == "long", sc.p.last_action
    assert "бью агрессивной лимиткой" in sc.p.last_action and plan["src"] == "gate_level", (sc.p.last_action, plan)
    assert "ЖДАТЬ — лента продавцов" in sc.p._gates_text(plan), "ответ двери — в истории плана"
    await sc.tick(99.22)                          # FILL
    pos = sc.p.position
    assert pos and pos["lots"] == 8 and pos["invalidation"] == 97.5 and pos["take"] == 108.0 and sc.p.plan is None, pos
    assert sc.p.gates[-1]["decision"] == "ЖДАТЬ" and sc.p.status()["entry_gate"] is None
    await sc.settle_ai()
    titles = [e["title"] for e in sc.xevents()]
    assert any(t == f"{MM()} у двери: ЖДАТЬ" for t in titles) and any(t == f"Вход по свежему решению {MM()}" for t in titles), titles
    # решение протухло (старше PYTHIA_ENTRY_FRESH_SEC) → у уровня дверь спрашивается снова, как в 5.4.1; её прошлые ответы — в промпте
    await sc.p._close_all(99.5, "тест: освободить-0", reanalyze=False)
    sc.p.pnls, sc.p.session_risk = [], trader_risk.SessionRisk(DEPOSIT)
    assert sc.p.adopt_forecast(sc.ex("BUY", None, 110.0, 98.0)), sc.p.last_action
    fake_ai.queue("mission_entry", {"decision": "ЖДАТЬ", "why": "лента продавцов, лучше взять на откате к 99.2",
                                    "entry": 99.2, "entry_kind": "откат"})
    await sc.tick(100.0)
    sc.p.plan["snap_ts"] -= float(config.PYTHIA_ENTRY_FRESH_SEC) + 1
    fake_ai.queue("mission_entry", {"decision": "ВОЙТИ", "why": "откат к 99.2 выкупают, лента за нас"})
    await sc.tick(99.22)
    assert fake_ai.count("mission_entry") == 3 and sc.p.pending and sc.p.pending["side"] == "long", sc.p.last_action
    assert "ПРОШЛЫЕ ОТВЕТЫ У ДВЕРИ" in fake_ai.last_user["mission_entry"] and "ЖДАТЬ — лента продавцов" in fake_ai.last_user["mission_entry"]
    assert "ВОЙТИ" in sc.p.last_action and "бью агрессивной лимиткой" in sc.p.last_action, sc.p.last_action
    await sc.tick(99.22)
    assert sc.p.position and sc.p.gates[-1]["decision"] == "ВОЙТИ" and "бью" in sc.p.gates[-1]["applied"]
    # выключено конфигом → вход сразу, PRO у двери не спрашивают
    sc.patch(config, "PYTHIA_ENTRY_CHECK", False)
    await sc.p._close_all(99.5, "тест: освободить", reanalyze=False)
    sc.p.pnls, sc.p.session_risk = [], trader_risk.SessionRisk(DEPOSIT)
    n_e = fake_ai.count("mission_entry")
    assert sc.p.adopt_forecast(sc.ex("BUY"))
    await sc.tick(100.0)
    assert sc.p.pending and fake_ai.count("mission_entry") == n_e and sc.p.status()["entry_check"] is False, sc.p.last_action
    sc.note = ("ЖДАТЬ → уровень 99.2, стоп 97.5, тейк 108; у уровня (свежий, назвал PRO) вход без второго вопроса; "
               "протух → вторая проверка ВОЙТИ; выкл → сразу")


async def s33_gate_cancel(sc: Scene) -> None:
    """PRO у двери: ОТМЕНИТЬ — план снят, ЖДУ_ПЛАН (фаза idle), повод дежурному PRO (kind pilot; v5.4.2: вне рынка без
    плана — перепроверка через EVENT_MIN_GAP_SEC, а не плановая);
    с council=true — полный совет без очереди (handoff kind entry), совет даёт BUY → снова через проверку у двери → вход."""
    b = sc.broker
    sc.p.review_ts = time.time() + 1800
    assert sc.p.adopt_forecast(sc.ex("BUY", None, 110.0, 98.0))
    fake_ai.queue("mission_entry", {"decision": "ОТМЕНИТЬ", "why": "идея отыграна: цель уже рядом, стакан пуст", "council": False})
    n_h = len(sc.m.handoffs)
    await sc.tick(100.0)
    assert sc.p.plan is None and sc.p.state == "ЖДУ_ПЛАН" and sc.p.pending is None and not b.placed, sc.p.last_action
    assert sc.p.gates[-1]["decision"] == "ОТМЕНИТЬ" and "план снят" in sc.p.gates[-1]["applied"], sc.p.gates[-1]
    assert len(sc.m.handoffs) == n_h + 1 and sc.m.handoffs[-1]["kind"] == "pilot" and not sc.m.handoffs[-1]["deferred"], sc.m.handoffs[-1]
    assert "вход отменён" in sc.m.handoffs[-1]["reason"] and "отыграна" in (sc.p._review_reason or "")
    assert sc.p.review_ts <= time.time() + mission.EVENT_MIN_GAP_SEC + 1, "v5.4.2: вне рынка без плана — PRO решает скоро"
    assert mission.status(TICKER)["phase"] == "idle" and sc.p.status()["entry_gate"] is None and sc.p.status()["gates"][-1]["decision"] == "ОТМЕНИТЬ"
    assert fake_ai.count("mission_review") == 0 and fake_ai.count("mission_exec") == 0
    # council=true → полный совет без очереди → BUY → проверка у двери (ВОЙТИ по умолчанию) → вход
    sc.p._last_reanalyze_ts = time.time()
    sc.m.council_ts = time.time()
    assert sc.p.adopt_forecast(sc.ex("BUY", None, 110.0, 98.0))
    fake_ai.queue("mission_entry", {"decision": "ОТМЕНИТЬ", "why": "картина спорная — нужен совет", "council": True})
    fake_ai.queue("mission_exec", {"do": "BUY", "entry": None, "take": 109.0, "invalidation": 97.0, "why": "совет: входим"})
    await sc.tick(100.0)
    assert sc.m.handoffs[-1]["kind"] == "entry" and not sc.m.handoffs[-1]["deferred"], sc.m.handoffs[-1]
    await sc.wait_council()                       # совет на фейках успевает пройти, пока Scene.tick ждёт ответ у двери
    assert fake_ai.count("mission_exec") == 1 and sc.p.plan and sc.p.plan["invalidation"] == 97.0, (sc.p.plan, sc.p.last_action)
    await sc.tick(100.0)                          # проверка у двери по новому приказу → ВОЙТИ (по умолчанию) → заявка
    await sc.tick(100.0)
    assert sc.p.position and sc.p.position["invalidation"] == 97.0 and fake_ai.count("mission_entry") == 3, sc.p.last_action
    await sc.settle_ai()
    assert any(t == f"{MM()} у двери: ОТМЕНИТЬ" for t in [e["title"] for e in sc.xevents()]), [e["title"] for e in sc.xevents()]
    sc.note = "ОТМЕНИТЬ → ЖДУ_ПЛАН, повод к плановой; council=true → совет BUY → у двери ВОЙТИ → позиция"


async def s34_gate_silent(sc: Scene) -> None:
    """PRO у двери молчит (таймаут попытки PYTHIA_ENTRY_TIMEOUT_SEC) → v5.4.2: это не решение ИИ — запись кода НЕТ_ОТВЕТА
    (не «ЖДАТЬ»), входа и отмены нет, ошибка в панель проблем; 5.4.4 (воля владельца 30.09: PRO — раз в 30 мин и по
    рыночным триггерам): без быстрого повтора — у двери спросим снова после ответа дежурного PRO на перепроверке; до него
    тик PRO не спрашивает и не пишет «ответ ЖДАТЬ» (5.4.3; было «велел ждать»); перепроверка ответила → вопрос → ВОЙТИ →
    вход. Непонятный ответ — НЕ_РАЗОБРАН, так же; молчания подряд считаются, исполнения по молчанию нет."""
    sc.patch(config, "PYTHIA_ENTRY_TIMEOUT_SEC", 1)
    sc.patch(fake_ai, "silent_sleep", 0.2)        # фейк «не ответил за срок попытки» — TimeoutError, как у ai_v5
    assert sc.p.adopt_forecast(sc.ex("BUY"))
    fake_ai.queue("mission_entry", FakeAI.SILENT)
    await sc.tick(100.0)
    assert fake_ai.count("mission_entry") == 1 and sc.p.pending is None and sc.p.plan, sc.p.last_action
    g = sc.p.gates[-1]
    assert g["decision"] == "НЕТ_ОТВЕТА" and g["silent"] and g["source"] == "код" and "не ответил у двери" in g["why"] \
        and "таймаут" in g["why"] and "решения не было" in g["why"], g
    assert not sc.p.plan.get("gate_after") and sc.p.plan.get("gate_review_ts") and sc.p.state == "ЗАСАДА", \
        (sc.p.plan, sc.p.state)
    assert fake_ai.errors and fake_ai.errors[-1][0] == "mission_entry", fake_ai.errors
    st = sc.p.status()["entry_gate"]
    assert st["busy"] is False and st["next_in_s"] is None and st["wait_review"] is True \
        and st["decision"] == "НЕТ_ОТВЕТА", st
    await sc.tick(100.0)                          # перепроверка не отвечала — не спрашиваем
    assert fake_ai.count("mission_entry") == 1 and "не ответил у двери" in sc.p.last_action \
        and "ответ ЖДАТЬ" not in sc.p.last_action and "велел" not in sc.p.last_action, sc.p.last_action
    assert "ЖДАТЬ" not in sc.p._gates_text(sc.p.plan) and "решения не было" in sc.p._situation_text(100.0)
    sc.p._last_review_ts = time.time() + 1.0      # дежурный PRO ответил на перепроверке
    fake_ai.queue("mission_entry", {"decision": "ВОЙТИ", "why": "стакан ожил"})
    await sc.tick(100.0)
    assert fake_ai.count("mission_entry") == 2 and sc.p.pending, sc.p.last_action
    assert "не ответил у двери" in fake_ai.last_user["mission_entry"], "прошлое молчание — в истории как «ответа не было»"
    await sc.tick(100.0)
    assert sc.p.position and sc.p.position["lots"] == 8
    # непонятный ответ после молчания — НЕ_РАЗОБРАН, счёт подряд: 2; входа нет, повтор через срок
    await sc.p._close_all(100.0, "тест: освободить", reanalyze=False)
    sc.p.pnls, sc.p.session_risk = [], trader_risk.SessionRisk(DEPOSIT)
    assert sc.p.adopt_forecast(sc.ex("BUY"))
    fake_ai.queue("mission_entry", FakeAI.SILENT, {"decision": "МОЖЕТ БЫТЬ", "why": "?"})
    await sc.tick(100.0)
    sc.p._last_review_ts = time.time() + 2.0
    await sc.tick(100.0)
    g = sc.p.gates[-1]
    assert sc.p.pending is None and g["silent"] and g["decision"] == "НЕ_РАЗОБРАН" and "непонятно" in g["why"] \
        and sc.p.plan.get("gate_review_ts") and sc.p.plan["gate_silent"] == 2, g
    assert "дверь молчит 2 раз подряд" in g["applied"] and sc.p.position is None, g
    # V: стоп-кран сессии сработал, ПОКА PRO думал у двери (закрытие с убытком в другой задаче) → ВОЙТИ не исполняется
    sc.p.plan = None
    assert sc.p.adopt_forecast(sc.ex("BUY"))
    n_e = len(sc.broker.placed)
    _orig = fake_ai.money_json

    async def _lock_while_thinking(system, user, *, route, max_tokens=None):
        if route == "mission_entry":
            sc.p.session_risk.lock_structural("тест: стоп-кран сессии")
        return await _orig(system, user, route=route, max_tokens=max_tokens)
    fake_ai.money_json = _lock_while_thinking
    try:
        fake_ai.queue("mission_entry", {"decision": "ВОЙТИ", "why": "стакан ожил"})
        await sc.tick(100.0)
    finally:
        del fake_ai.money_json
    assert len(sc.broker.placed) == n_e and sc.p.pending is None and "стоп-кран" in sc.p.gates[-1]["applied"], (sc.broker.placed[n_e:], sc.p.gates[-1])
    assert not sc.p.plan.get("approved_until"), "стоп-кран — не временный запрет: одобрение не хранится"
    sc.p.session_risk = trader_risk.SessionRisk(DEPOSIT)
    sc.note = ("молчание → НЕТ_ОТВЕТА (не ЖДАТЬ), повтор после ответа перепроверки → ВОЙТИ → позиция; непонятный ответ — "
               "НЕ_РАЗОБРАН, счёт подряд; стоп-кран во время раздумий — входа нет")

async def s35_profit_exit(sc: Scene) -> None:
    """Мысль о прибыли (5.4.1): пройдено ≥ 60 % хода от входа до тейка → PRO думает; ВЫЙТИ → закрыто в плюс по рынку,
    журнал, обычный ход после закрытия (остыть, дежурный PRO); ниже порога — вопросов нет; без тейка — плюс ≥ 1 % от
    входа; ДЕРЖАТЬ → пейсинг PYTHIA_PROFIT_THINK_COOL_SEC."""
    pos = await sc.open_position("long", inv=98.0, take=110.0)
    entry = pos["entry"]
    await sc.tick(entry + 5.0)                    # ≈50 % хода — ниже порога 60 %
    assert fake_ai.count("mission_profit") == 0 and not pos.get("profit_busy")
    fake_ai.queue("mission_profit", {"decision": "ВЫЙТИ", "why": "рывок выдохся: стакан над 106 тонкий, лента продавцов", "note": "в кассу"})
    await sc.tick(entry + 6.2)                    # >60 % хода → мысль о прибыли
    assert pos.get("profit_busy") or fake_ai.count("mission_profit") == 1, sc.p.last_action
    await sc.wait_profit()
    assert sc.p.position is None and sc.p.pnls[-1] > 0, (sc.p.last_action, sc.p.pnls)
    tr = store_v5.trades(TICKER)[0]
    assert "мысль о прибыли" in tr["why"] and f"{MM()} решил выйти" in tr["why"] and tr["pnl"] > 0, tr
    x = sc.p.profits[-1]
    share = f"пройдено {6.2 / (110.0 - entry) * 100:.0f} % хода"
    assert x["decision"] == "ВЫЙТИ" and share in x["reason"] and x["model"] == MM() and x["floating"] > 0, x
    up = fake_ai.last_user["mission_profit"]
    for piece in ("ПРИБЫЛЬ: " + share, "ХОД ЦЕНЫ", "ЖИВОЙ РЫНОК", "ПЛАН И ПРОШЛЫЕ РЕШЕНИЯ", "УРОВНИ ПОЗИЦИИ", "ИТОГ ОБЩЕГО СОВЕТА",
                  "СВЕЖИЕ НОВОСТИ", "Выйти, выйти и перезайти, держать или звать совет"):
        assert piece in up, (piece, up[:900])
    assert sc.m.sizes["profit"]["prompt"] == len(up) and sc.m.sizes["profit"]["answer"] > 0
    assert sc.p.last_action.startswith("ЗАКРЫЛ ВСЁ (мысль о прибыли") and "после закрытия" in sc.p.last_action, sc.p.last_action
    after = float(config.PYTHIA_AFTER_CLOSE_SEC)
    assert time.time() + after - 10 <= sc.p.review_ts <= time.time() + after + 1 and sc.m.handoffs[-1]["kind"] == "pilot"
    assert sc.p.status()["profits"][-1]["decision"] == "ВЫЙТИ" and sc.p.status()["profit"] is None
    await sc.settle_ai()
    assert any(e["title"] == "Мысль о прибыли: ВЫЙТИ" for e in sc.xevents()), [e["title"] for e in sc.xevents()]
    assert any("Позиция закрыта" in e["title"] for e in sc.xevents("close"))
    # без тейка: плюс ≥ PYTHIA_PROFIT_THINK_MIN_PCT % от входа — тоже повод; ДЕРЖАТЬ → пейсинг PROFIT_THINK_COOL_SEC
    sc.p.pnls, sc.p.session_risk = [], trader_risk.SessionRisk(DEPOSIT)
    pos2 = await sc.open_position("long", inv=98.0, take=None)
    fake_ai.queue("mission_profit", {"decision": "ДЕРЖАТЬ", "why": "ход жив"})
    await sc.tick(round(pos2["entry"] * 1.005, 2))
    assert fake_ai.count("mission_profit") == 1, "плюс 0.5 % < 1 % — не повод"
    await sc.tick(round(pos2["entry"] * 1.012, 2))
    await sc.wait_profit()
    assert fake_ai.count("mission_profit") == 2 and sc.p.position is pos2 and sc.p.profits[-1]["decision"] == "ДЕРЖАТЬ", sc.p.profits[-1]
    assert "тейка нет, плюс" in sc.p.profits[-1]["reason"] and pos2.get("profit_next", 0) > time.time() + 800
    await sc.tick(round(pos2["entry"] * 1.02, 2))
    assert fake_ai.count("mission_profit") == 2, "пейсинг: не чаще PYTHIA_PROFIT_THINK_COOL_SEC"
    stp = sc.p.status()["profit"]
    assert stp["next_in_s"] > 800 and stp["busy"] is False and stp["progress_pct"] is None and stp["gain_pct"] >= 1.9, stp
    sc.note = f"62 % хода → {MM()} ВЫЙТИ → закрыто в плюс, журнал, остыть; без тейка плюс 1.2 % → ДЕРЖАТЬ, пейсинг"


async def s36_profit_reenter(sc: Scene) -> None:
    """ВЫЙТИ_И_ПЕРЕЗАЙТИ: закрыто в плюс, план той же стороны у уровня (откат 103 ниже цены; стоп из позиции, тейк из ответа);
    v5.4.2: план несёт src «profit» — цена дошла, решение свежее → вход без второго вопроса у двери → новая позиция.
    Уровень не с той стороны → как ВЫЙТИ."""
    pos = await sc.open_position("long", inv=98.0, take=110.0)
    n_e = fake_ai.count("mission_entry")          # проверка у двери при открытии позиции
    fake_ai.queue("mission_profit", {"decision": "ВЫЙТИ_И_ПЕРЕЗАЙТИ", "why": "рывок на пустом стакане — забрать и перезайти на откате",
                                     "reentry": 103.0, "reentry_kind": "откат", "take": 112.0})
    await sc.tick(106.5)
    await sc.wait_profit()
    assert sc.p.position is None and sc.p.pnls[-1] > 0, (sc.p.last_action, sc.p.pnls)
    plan = sc.p.plan
    assert plan and plan["side"] == "long" and plan["entry"] == 103.0 and plan["kind"] == "откат" and plan["take"] == 112.0, plan
    assert plan["invalidation"] == 98.0 and sc.p.state == "ЗАСАДА" and "план перезайти" in sc.p.last_action, sc.p.last_action
    assert sc.p.last_action.startswith("ЗАКРЫЛ ВСЁ (мысль о прибыли") and "выйти и перезайти" in store_v5.trades(TICKER)[0]["why"]
    x = sc.p.profits[-1]
    assert x["decision"] == "ВЫЙТИ_И_ПЕРЕЗАЙТИ" and x["reentry"] == 103.0 and "план перезайти: откат @103" in x["applied"], x
    assert mission.status(TICKER)["phase"] == "armed" and sc.p.status()["entry_gate"]["checks"] == 0
    assert plan["src"] == "profit" and plan["snap_ts"] and "у уровня вход по этому решению" in sc.p.last_action, (plan, sc.p.last_action)
    await sc.tick(104.0)                          # выше уровня — ждём
    assert sc.p.pending is None and fake_ai.count("mission_entry") == n_e
    await sc.tick(103.03)                         # уровень → решение PRO о перезаходе свежее → вход без второго вопроса
    assert fake_ai.count("mission_entry") == n_e and sc.p.pending, sc.p.last_action
    await sc.tick(103.03)
    assert sc.p.position and sc.p.position["take"] == 112.0 and sc.p.position["invalidation"] == 98.0 and sc.p.plan is None, sc.p.position
    # уровень перезахода не с той стороны (откат выше цены) → как ВЫЙТИ: закрыто, плана нет, обычный ход после закрытия
    fake_ai.queue("mission_profit", {"decision": "ВЫЙТИ_И_ПЕРЕЗАЙТИ", "why": "ещё раз", "reentry": 111.0, "reentry_kind": "откат"})
    sc.p.position["profit_next"] = 0.0
    await sc.tick(108.5)
    await sc.wait_profit()
    assert sc.p.position is None and sc.p.plan is None and "перезайти негде" in store_v5.trades(TICKER)[0]["why"], sc.p.last_action
    assert "не с той стороны" in sc.p.profits[-1]["applied"] and sc.m.handoffs[-1]["kind"] == "pilot", sc.p.profits[-1]
    sc.note = "ВЫЙТИ_И_ПЕРЕЗАЙТИ → закрыто в плюс, план откат @103 → у уровня вход по свежему решению → позиция; кривой уровень → как ВЫЙТИ"


async def s37_profit_council(sc: Scene) -> None:
    """СОВЕТ из мысли о прибыли: триггер подтянут к lock_price (прибыль заперта, трос от него не ниже входа), тейк → цель,
    полный совет без очереди (handoff kind profit) → совет HOLD держит с новыми уровнями; ДЕРЖАТЬ с lock ниже входа —
    не принят; молчание PRO → v5.4.2: НЕТ_ОТВЕТА (запись кода, не «ДЕРЖАТЬ» за ИИ), позиция как есть, ошибка в панель;
    5.4.4 — без быстрого повтора: следующая мысль не раньше PYTHIA_PROFIT_THINK_COOL_SEC."""
    pos = await sc.open_position("long", inv=98.0, take=110.0)
    entry = pos["entry"]
    sc.p._last_reanalyze_ts = time.time()         # советы «только что» — мысль обязана пройти без очереди
    sc.m.council_ts = time.time()
    fake_ai.queue("mission_profit", {"decision": "СОВЕТ", "why": "картина спорная: рывок на новости, объёмы падают",
                                     "lock_price": 104.0, "take": 111.0})
    fake_ai.queue("mission_exec", {"do": "HOLD", "entry": None, "take": 112.0, "invalidation": 105.0, "why": "держать выше"})
    fake_ai.gate = asyncio.Event()
    await sc.tick(106.5)
    await sc.wait_profit()
    assert sc.p.position is pos and pos["invalidation"] == 104.0 and pos["inv0"] == 104.0 and pos["take"] == 111.0, pos
    assert pos["profit_lock"] and pos["hard_stop"] >= entry and pos["hard_stop"] < 104.0, pos
    assert sc.m.handoffs[-1]["kind"] == "profit" and not sc.m.handoffs[-1]["deferred"] and sc.p._reanalyzing, sc.m.handoffs[-1]
    x = sc.p.profits[-1]
    assert x["decision"] == "СОВЕТ" and x["lock_price"] == 104.0 and "прибыль заперта триггером 104" in x["applied"] \
        and "цель 110 → 111" in x["applied"] and "зову полный совет" in x["applied"], x
    assert sc.p.status()["profit"]["locked"] is True
    fake_ai.gate.set()
    await sc.wait_council()
    assert fake_ai.count("mission_exec") == 1 and "ОТКРЫТАЯ ПОЗИЦИЯ" in fake_ai.last_user["mission_exec"], fake_ai.summary()
    assert sc.p.position is pos and pos["invalidation"] == 105.0 and pos["take"] == 112.0 and not pos.get("profit_lock"), pos
    assert "мысль о прибыли" in str(bus.run(sc.m.run_id)["meta"].get("reason"))
    await sc.settle_ai()
    assert any(e["title"] == "Мысль о прибыли: СОВЕТ" for e in sc.xevents()), [e["title"] for e in sc.xevents()]
    # ДЕРЖАТЬ с lock_price ниже входа — не принят, уровни прежние
    pos["profit_next"] = 0.0
    fake_ai.queue("mission_profit", {"decision": "ДЕРЖАТЬ", "why": "ход жив", "lock_price": 99.0})
    await sc.tick(109.0)
    await sc.wait_profit()
    assert pos["invalidation"] == 105.0 and "не принят" in sc.p.profits[-1]["applied"], sc.p.profits[-1]
    # v5.4.2: молчание → не «ДЕРЖАТЬ» за ИИ: запись кода НЕТ_ОТВЕТА, позиция как есть; 5.4.4 — без быстрого повтора
    sc.patch(config, "PYTHIA_PROFIT_TIMEOUT_SEC", 1)
    sc.patch(fake_ai, "silent_sleep", 0.2)        # фейк «не ответил за срок попытки» — TimeoutError, как у ai_v5
    pos["profit_next"] = 0.0
    fake_ai.queue("mission_profit", FakeAI.SILENT)
    await sc.tick(109.2)
    await sc.wait_profit()
    x = sc.p.profits[-1]
    assert x["decision"] == "НЕТ_ОТВЕТА" and x["silent"] and x["source"] == "код" and "не ответил" in x["why"] \
        and "решения не было" in x["why"] and sc.p.position is pos and pos["invalidation"] == 105.0, x
    cool = float(config.PYTHIA_PROFIT_THINK_COOL_SEC)
    assert pos["profit_next"] - time.time() >= cool - 10, "5.4.4: без быстрого повтора — полный пейсинг мысли"
    assert fake_ai.errors and fake_ai.errors[-1][0] == "mission_profit", fake_ai.errors
    assert "ДЕРЖАТЬ" not in sc.p._profits_text(pos).splitlines()[-1], "в промпт молчание идёт как «ответа не было»"
    sc.note = ("СОВЕТ → триггер 104 (трос не ниже входа), цель 111, совет HOLD 105/112; lock ниже входа не принят; "
               "молчание → НЕТ_ОТВЕТА, без быстрого повтора")


async def s38_shock_profit(sc: Scene) -> None:
    """Резкий ход В НАШУ сторону при плавающем плюсе → мысль о прибыли вместо триажа (один ход — одна мысль, PRO дежурный
    не разбужен); ход против позиции → триаж как раньше; в пейсинге мысли → триаж; выключено конфигом → триаж."""
    pos = await sc.open_position("long", inv=98.0, take=120.0, age_s=1300)
    t0 = time.time()
    sc.p._px_hist = [(t0 - 400, 100.0), (t0 - 300, 100.0), (t0 - 200, 100.0), (t0 - 100, 100.0)]
    sc.p.review_ts, sc.p._last_review_ts = t0 + 1800, t0
    n_h = len(sc.m.handoffs)
    fake_ai.queue("mission_profit", {"decision": "ДЕРЖАТЬ", "why": "рывок с объёмом — держим", "lock_price": 101.0})
    await sc.tick(102.0)                          # +2 % за 6 мин в нашу сторону, плюс есть
    await sc.wait_profit()
    assert fake_ai.count("mission_profit") == 1 and fake_ai.count("event_triage") == 0 and fake_ai.count("mission_review") == 0, fake_ai.summary()
    x = sc.p.profits[-1]
    assert x["decision"] == "ДЕРЖАТЬ" and "рывок в нашу сторону +2.00%" in x["reason"] and pos["invalidation"] == 101.0, x
    assert len(sc.m.handoffs) == n_h and sc.p.review_ts >= t0 + 1790, "дежурный PRO не разбужен: думала мысль о прибыли"
    assert "ПРИБЫЛЬ: рывок в нашу сторону" in fake_ai.last_user["mission_profit"]
    await sc.tick(102.2)
    assert fake_ai.count("mission_profit") == 1, "тот же ход второй раз — не повод"
    # ход ПРОТИВ позиции → триаж как раньше
    t1 = time.time()
    sc.p._px_hist = [(t1 - 400, 104.0), (t1 - 300, 104.0), (t1 - 200, 104.0), (t1 - 100, 104.0)]
    sc.p._last_shock_ts = 0.0
    fake_ai.queue("event_triage", {"urgency": "ПЛАНОВО", "action": None, "why": "откат в русле"})
    await sc.tick(101.9)                          # −2 % против нас
    await sc.wait_triage()
    assert fake_ai.count("event_triage") == 1 and fake_ai.count("mission_profit") == 1 and sc.m.handoffs[-1]["kind"] == "shock"
    # в пейсинге мысли (только что думала) рывок в плюс → триаж как раньше
    t2 = time.time()
    sc.p._px_hist = [(t2 - 400, 102.0), (t2 - 300, 102.0), (t2 - 200, 102.0), (t2 - 100, 102.0)]
    sc.p._last_shock_ts = 0.0
    fake_ai.queue("event_triage", {"urgency": "ПЛАНОВО", "action": None, "why": "в русле"})
    await sc.tick(104.5)
    await sc.wait_triage()
    assert fake_ai.count("event_triage") == 2 and fake_ai.count("mission_profit") == 1
    # выключено конфигом → рывок в плюс идёт в триаж
    sc.patch(config, "PYTHIA_PROFIT_THINK", False)
    pos["profit_next"] = 0.0
    t3 = time.time()
    sc.p._px_hist = [(t3 - 400, 104.0), (t3 - 300, 104.0), (t3 - 200, 104.0), (t3 - 100, 104.0)]
    sc.p._last_shock_ts = 0.0
    fake_ai.queue("event_triage", {"urgency": "ПЛАНОВО", "action": None, "why": "в русле"})
    await sc.tick(106.5)
    await sc.wait_triage()
    assert fake_ai.count("event_triage") == 3 and fake_ai.count("mission_profit") == 1 and sc.p.status()["profit_think"] is False
    sc.note = "рывок +2 % в плюсе → мысль о прибыли (ДЕРЖАТЬ, триггер 101), триаж не звался; против/в пейсинге/выкл → триаж"


async def s39_council_wait(sc: Scene) -> None:
    """Приказ совета WAIT (5.4.1): пилот без плана (ЖДУ_ПЛАН, фаза idle), входа нет, m.exec хранится (wait_for), перепроверка
    по расписанию, толмач; v5.4.2: WAIT показан ИИ как прошлое мнение совета, а не запрет; дежурный PRO решает
    КУПИТЬ_СЕЙЧАС по живому рынку → план (src review) → свежее решение → вход без второго вопроса у двери."""
    sc.p.plan = None
    sc.p._last_reanalyze_ts = time.time() - 4000
    fake_ai.queue("mission_exec", {"do": "WAIT", "wait_for": "закрепление выше 101 на объёме", "why": "перевеса нет: стакан двусторонний",
                                   "plan": "ждём", "confidence": 35, "levels": [101.0]})
    r = await mission.council_again(TICKER, "стенд: совет", wait=True)
    assert r["ok"], r
    assert sc.m.exec["do"] == "WAIT" and sc.m.exec["wait_for"] == "закрепление выше 101 на объёме" and sc.m.exec["invalidation"] is None, sc.m.exec
    assert sc.p.plan is None and sc.p.state == "ЖДУ_ПЛАН" and "вне рынка — ждал: закрепление выше 101" in sc.p.last_action, sc.p.last_action
    assert "перевеса нет" not in sc.p.last_action, "5.4.2: текст пилота нейтрален"
    assert mission.status(TICKER)["phase"] == "idle" and mission.status(TICKER)["exec"]["do"] == "WAIT"
    # v5.4.3: ориентир совета — не условие; цена тогда и ход с тех пор; уровень будильника кода — числом
    assert "WAIT — вне рынка. Ориентир совета (не условие): закрепление выше 101" in mission._exec_text(sc.m)
    assert "Будильник кода у уровней 101" in mission._exec_text(sc.m) and "(прошлое мнение): план — ждём" in mission._exec_text(sc.m)
    await sc.tick(100.0, n=2)                     # первый тик — стакан появился, второй — рынок жив
    # v5.4.2: WAIT — не сон на PYTHIA_REVIEW_SEC: дежурный PRO не реже раза в PYTHIA_WAIT_REVIEW_SEC
    assert sc.p.review_ts <= sc.m.exec_ts + float(config.PYTHIA_WAIT_REVIEW_SEC) + 1, sc.p.review_ts - time.time()
    assert sc.p.review_ts > time.time() + float(config.PYTHIA_WAIT_REVIEW_SEC) - 60
    # …и уровень приказа (101) под наблюдением: цена прошла его → внеплановая перепроверка (один уровень — один повод)
    fake_ai.queue("mission_review", {"choice": "ЖДЁМ", "why": "пробой без объёма", "note": "ждём"})
    await sc.tick(101.5)
    assert sc.m.handoffs[-1]["kind"] == "wait_level" and "прошла уровень 101" in sc.m.handoffs[-1]["reason"], sc.m.handoffs[-1]
    assert not sc.m.handoffs[-1]["deferred"], "перепроверка поднята сразу (тем же тиком)"
    assert sc.p.pending is None and not sc.broker.placed and fake_ai.count("mission_entry") == 0, "WAIT — входа нет"
    assert await sc.settle(lambda: fake_ai.count("mission_review") == 1 and not sc.p._review_busy)
    assert "прошла уровень 101 из приказа совета" in fake_ai.last_user["mission_review"]
    n_h = len(sc.m.handoffs)
    await sc.tick(100.5)
    await sc.tick(101.6)
    assert len(sc.m.handoffs) == n_h, "тот же уровень второй раз не будит"
    sit = sc.p._situation_text(101.5)
    assert "ПРИКАЗ СОВЕТА (" in sit and "прошлое мнение, не запрет" in sit and "закрепление выше 101" not in sit, sit
    await sc.settle_ai()
    assert any(e["title"].startswith("Совет решил ждать (WAIT)") for e in sc.xevents("council")), [e["title"] for e in sc.xevents()]
    # дежурный PRO: КУПИТЬ_СЕЙЧАС → план по живому рынку → вход без второго вопроса у двери → позиция
    sc.p.review_ts = 0.0
    fake_ai.queue("mission_review", {"choice": "КУПИТЬ_СЕЙЧАС", "why": "закрепились выше 101", "invalidation": 99.0, "take": 108.0})
    await sc.tick(101.5)
    assert await sc.settle(lambda: fake_ai.count("mission_review") == 2 and not sc.p._review_busy)
    ur = fake_ai.last_user["mission_review"]
    assert "Ориентир совета (не условие): закрепление выше 101" in ur and sc.p.plan and sc.p.plan["side"] == "long", sc.p.last_action
    assert sc.p.plan["src"] == "review" and sc.p.plan["snap_price"] == 101.5, sc.p.plan
    await sc.tick(101.5)                          # свежее решение PRO → заявка без вопроса у двери
    assert fake_ai.count("mission_entry") == 0 and sc.p.pending, sc.p.last_action
    await sc.tick(101.5)
    assert sc.p.position and sc.p.position["invalidation"] == 99.0 and mission.status(TICKER)["phase"] == "in_position"
    await sc.settle_ai()
    assert any(e["title"] == f"Вход по свежему решению {MM()}" for e in sc.xevents()), [e["title"] for e in sc.xevents()]
    sc.note = ("WAIT → ЖДУ_ПЛАН/idle, входа нет, WAIT — прошлое мнение в ситуации, ритм WAIT, уровень 101 пройден → PRO; "
               "PRO КУПИТЬ → вход по свежему решению без второго вопроса у двери → позиция")


async def s40_program_stops(sc: Scene) -> None:
    """Стопы только в программе (PYTHIA_EXCHANGE_STOP=0, умолчание 5.4.1): ни одной стоп-заявки на бирже, трос виртуальный —
    гэп за него закрывает по рынку без вопросов и без снятия стопов; триггер и PRO у троса работают как раньше; флаг
    включили в бою → трос встал на биржу на следующем restop; выключили → снят один раз; рестарт со старым stop_id → снят."""
    b = sc.broker
    assert config.PYTHIA_EXCHANGE_STOP is False, "умолчание 5.4.1 — стопы только в программе"
    pos = await sc.open_position("long", inv=98.0)
    assert not b.stops and pos.get("stop_id") is None and not pos.get("stop_request"), pos
    assert "трос в программе @" in sc.p.last_action and "стопов на бирже нет" in sc.p.last_action, sc.p.last_action
    assert sc.p.status()["exchange_stop"] is False and pos["hard_stop"] < 98.0
    await sc.tick(100.0)                          # restop-тики ничего не ставят
    assert not b.stops
    fake_ai.queue("mission_guard", {"decision": "ЖДАТЬ", "why": "ложный прокол", "hold_until_price": 97.0})
    await sc.tick(97.6)                           # триггер → PRO у троса — как раньше
    await sc.wait_guard()
    assert sc.p.position is pos and pos["invalidation"] == 97.0 and fake_ai.count("mission_guard") == 1, sc.p.last_action
    await sc.wait_council()
    n_o = len(b.placed)
    await sc.tick(pos["hard_stop"] - 1.0)         # гэп за виртуальный трос → закрытие по рынку без вопросов
    assert sc.p.position is None and len(b.placed) == n_o + 1 and b.placed[-1]["tag"] == "aip-close" and b.placed[-1]["price"] is None, sc.p.last_action
    assert not b.stop_cancels and not b.stops and "аварийный трос" in store_v5.trades(TICKER)[0]["why"] and fake_ai.count("mission_guard") == 1
    await sc.wait_council()
    # флаг включили в бою → трос на бирже как в 5.4.0; выключили → снят один раз; включили → снова лежит
    sc.p.pnls, sc.p.session_risk = [], trader_risk.SessionRisk(DEPOSIT)
    sc.patch(config, "PYTHIA_EXCHANGE_STOP", True)
    pos2 = await sc.open_position("long", inv=98.0)
    sid = pos2["stop_id"]
    assert sid and len(b.stops) == 1 and b.stops[-1]["stop"] == pos2["hard_stop"] and sc.p.status()["exchange_stop"] is True
    config.PYTHIA_EXCHANGE_STOP = False           # переключили в панели (config.set_many → живое значение) — тик сам снимает стоп
    await sc.tick(100.0)
    assert pos2["stop_id"] is None and b.stop_cancels == [sid] and len(b.stops) == 1 and not pos2.get("restop"), (pos2, b.stop_cancels)
    await sc.tick(100.0)
    assert b.stop_cancels == [sid], "снят один раз"
    config.PYTHIA_EXCHANGE_STOP = True            # включили — трос ложится на биржу ближайшим тиком, без другого повода
    await sc.tick(100.0)
    assert pos2["stop_id"] == "S-2" and len(b.stops) == 2, pos2
    config.PYTHIA_EXCHANGE_STOP = False
    # рестарт со старым state (stop_id в файле) при флаге 0 → стоп снят один раз, трос виртуальный
    path = Path(tempfile.mkdtemp(prefix="pythia_scen_")) / "aipilot_state.json"
    sc.p._state_path = path
    sc.p._save_state()
    sc.p._state_path = None
    sc.m.task.set_result(None)
    p2 = mission.MissionPilot(TICKER, deposit=DEPOSIT, broker=b, mission=sc.m)
    mission._bind_pilot(sc.m, p2)
    await p2.prepare()
    p2._state_path = path
    p2._restore_state(b.net)
    p2._state_path = None
    sc.m.pilot, sc.p = p2, p2
    sc.m.task = asyncio.get_running_loop().create_future()
    q = p2.position
    assert q and q["stop_id"] == "S-2" and q.get("restop") and "трос S-2" in p2.last_action, (q, p2.last_action)
    await sc.tick(100.0)
    assert q["stop_id"] is None and "S-2" in b.stop_cancels and len(b.stops) == 2 and not q.get("restop"), (q, b.stop_cancels)
    assert q["hard_stop"] == pos2["hard_stop"] and p2.status()["exchange_stop"] is False
    # V: висящий запрос стопа (state 5.4.0 / флаг выключили в бою) при флаге 0 — тот же UUID НЕ повторяем (это был бы
    # PostStopOrder): судьбу решает список стопов биржи; стопа там нет → запрос закрыт, трос виртуальный, на бирже пусто
    n_s = len(b.stops)
    q["stop_request"] = {"request_id": "v-0000-0000-0000", "lots": int(q["lots"]), "direction": SELL, "price": q["hard_stop"],
                         "uncertain": True, "created_ts": time.time() - 5}
    q["restop"] = True
    await sc.tick(100.0)
    assert len(b.stops) == n_s and not q.get("stop_request") and q.get("stop_id") is None and not q.get("restop"), (b.stops[n_s:], q)
    sc.note = "ни одной стоп-заявки; гэп за виртуальный трос → рынок; флаг в бою: 1 → трос лёг, 0 → снят один раз; рестарт → старый стоп снят; висящий запрос стопа не повторён"


async def s41_council_hold(sc: Scene) -> None:
    """Совет «держать» (ревью 5.4.2): позиция по размеру биржи, у тейка PRO ПОДЕРЖАТЬ → совет → приказ HOLD — стоп и тейк
    позиции обновлены, добора нет (биржа снова даёт лоты — aip-topup не шлётся), дверь не спрашивается; раньше «держать»
    шифровалось как BUY той же стороны, и код добирал до максимума без вопроса."""
    b = sc.broker
    b.mx = {"buy": 3, "sell": 3}
    sc.p.deposit_override = None
    pos = await sc.open_position("long", inv=98.0, take=103.0)
    assert sc.p._sized_by_broker and pos["lots"] == 3, pos
    # ревью 5.4.2 (финал): остаток авто-добора самого входа (topup_left) не обнуляется руками — его снимает HOLD
    b.mx = {"buy": 4, "sell": 4}                  # биржа снова даёт лоты
    sc.p._mx = None
    sc.p._last_reanalyze_ts = time.time()         # тейк зовёт совет без очереди
    sc.m.council_ts = time.time()
    n_entry = fake_ai.count("mission_entry")
    fake_ai.queue("mission_take", {"decision": "ПОДЕРЖАТЬ", "why": "импульс жив, лента за нас", "tp_next": 106.0})
    fake_ai.queue("mission_exec", {"do": "ДЕРЖАТЬ", "entry": None, "take": 107.0, "invalidation": 101.0,
                                   "why": "держать, стоп подтянуть к 101"})
    await sc.tick(103.4)
    await sc.wait_guard()
    await sc.wait_council()
    assert sc.m.exec["do"] == "HOLD" and sc.m.exec["entry"] is None and sc.m.exec["invalidation"] == 101.0, sc.m.exec
    assert sc.p.position is pos and pos["invalidation"] == 101.0 and pos["take"] == 107.0 and pos["lots"] == 3, pos
    assert not pos.get("topup_left") and sc.p.plan is None and "без добора" in sc.p.last_action, sc.p.last_action
    n_o = len(b.placed)
    pos["last_fill_ts"] = 0.0
    await sc.tick(103.5, n=13)                    # тик §2 проверяет добор раз в 6 тиков — окна прошли
    assert len(b.placed) == n_o and not [o for o in b.placed if o["tag"] == "aip-topup"] and pos["lots"] == 3, b.placed
    assert fake_ai.count("mission_entry") == n_entry, "HOLD — не вход и не добор: дверь не спрашивается"
    assert mission.status(TICKER)["phase"] == "in_position" and "HOLD — держать позицию как есть" in mission._exec_text(sc.m)
    await sc.settle_ai()
    assert any("Совет решил держать позицию (HOLD" in e["title"] for e in sc.xevents("council")), [e["title"] for e in sc.xevents()]
    sc.note = "ПОДЕРЖАТЬ → совет HOLD: стоп 101 / тейк 107, 3 лота как были — биржа давала 4, добора нет"


async def s42_wait_alarm(sc: Scene) -> None:
    """Будильник ЖДЁМ (5.4.3): вне рынка дежурный PRO ответил ЖДЁМ и назвал уровень 101 выше цены — код уровень не
    выбрасывает, а ставит будильник; цена прошла 101 → внеплановая перепроверка с поводом «прошла уровень …, который
    ты назвал в ЖДЁМ» (один раз) → PRO КУПИТЬ_СЕЙЧАС → вход по свежему решению; ответ снимает будильник."""
    sc.p.plan = None
    wait, err = mission._validate_exec({"do": "WAIT", "wait_for": "нужен объём покупателя", "why": "стенд: вне рынка",
                                        "plan": "ждём"}, "auto", 100.0)
    assert wait and not err and wait["levels"] == [], (wait, err)
    assert sc.p.adopt_forecast({"exec": wait}) and sc.p.plan is None
    sc.m.exec, sc.m.exec_ts = wait, time.time()
    await sc.tick(100.0, n=2)                     # стакан появился, рынок жив
    # 1) плановая перепроверка: ЖДЁМ с уровнем 101 → будильник (не план, не вход)
    sc.p.review_ts = 0.0
    fake_ai.queue("mission_review", {"choice": "ЖДЁМ", "why": "пробоя 101 нет — жду", "entry": 101.0, "entry_kind": "прорыв",
                                     "note": "уровень 101"})
    await sc.tick(100.0)
    assert await sc.settle(lambda: fake_ai.count("mission_review") == 1 and not sc.p._review_busy)
    wk = sc.p._wake
    assert wk and wk["level"] == 101.0 and wk["dir"] == "up" and wk["ref"] == 100.0 and not wk["fired"], wk
    assert sc.p.plan is None and sc.p.pending is None and sc.m.reviews[-1]["wake"] == 101.0, sc.p.plan
    assert "будильник у 101" in sc.p.last_action and sc.p.status()["wake"]["level"] == 101.0, sc.p.last_action
    assert "БУДИЛЬНИК: уровень 101 из твоего ЖДЁМ" in sc.p._situation_text(100.2)
    await sc.tick(100.6)                          # под уровнем — тишина
    assert not [h for h in sc.m.handoffs if h.get("kind") == "wait_level"], sc.m.handoffs
    # 2) цена прошла 101 (PRO ответил давно — пейсинг EVENT_MIN_GAP_SEC позади) → внеплановая перепроверка
    sc.p._last_review_ts -= mission.EVENT_MIN_GAP_SEC + 60
    fake_ai.queue("mission_review", {"choice": "КУПИТЬ_СЕЙЧАС", "why": "пробой 101 с объёмом", "invalidation": 100.2,
                                     "take": 104.0})
    await sc.tick(101.3)
    h = sc.m.handoffs[-1]
    assert h["kind"] == "wait_level" and "прошла уровень 101, который ты назвал в ЖДЁМ" in h["reason"] and not h["deferred"], h
    assert await sc.settle(lambda: fake_ai.count("mission_review") == 2 and not sc.p._review_busy)
    ur = fake_ai.last_user["mission_review"]
    assert "ПОВОД ПЕРЕПРОВЕРКИ (внеплановая): цена 101.3 прошла уровень 101, который ты назвал в ЖДЁМ" in ur, ur[:1500]
    assert "Прошлая перепроверка (0 мин назад, цена 100): ЖДЁМ — пробоя 101 нет — жду; с тех пор 101.3 (+1.30 %)" in ur, ur[:1500]
    assert sc.p._wake is None, "ответ перепроверки снимает будильник"
    assert sc.p.plan and sc.p.plan["side"] == "long" and sc.p.plan["src"] == "review", sc.p.plan
    n_h = len(sc.m.handoffs)
    await sc.tick(101.3)                          # свежее решение PRO → заявка без второго вопроса у двери
    await sc.tick(101.3)
    assert sc.p.position and sc.p.position["side"] == "long" and fake_ai.count("mission_entry") == 0, sc.p.last_action
    assert len([x for x in sc.m.handoffs[n_h:] if x.get("kind") == "wait_level"]) == 0, "будильник сработал один раз"
    await sc.settle_ai()
    rv = [e for e in sc.xevents("review") if e["title"] == "Перепроверка: ЖДЁМ"]
    assert rv and "будильник у 101" in rv[-1]["detail"] and "вход 101" not in rv[-1]["detail"], rv
    sc.note = "ЖДЁМ с уровнем 101 → будильник; цена 101.3 → PRO по поводу «прошла уровень» → КУПИТЬ → вход; будильник снят"


async def s43_wait_alarm_kept(sc: Scene) -> None:
    """Ревью 5.4.3 (D1/D2): будильник ЖДЁМ снимается только явным решением. ЖДЁМ с уровнем 101 → плановая перепроверка
    отвечает ЖДЁМ без entry («будильник стоит — ждём его») — будильник остаётся; цена прошла 101, пока PRO думал над
    плановой, — повод сразу после его ответа (а не молча); PRO по поводу снова ЖДЁМ у 101 («вынос без объёма») — цена
    пилит уровень ±0.05 %, тот же уровень в окне PYTHIA_EVENT_COOL_SEC не будит (в ситуации «будильник на 101 уже
    срабатывал»); ответ КУПИТЬ снимает будильник."""
    sc.p.plan = None
    wait, err = mission._validate_exec({"do": "WAIT", "wait_for": "нужен объём покупателя", "why": "стенд: вне рынка",
                                        "plan": "ждём"}, "auto", 100.0)
    assert wait and not err and wait["levels"] == [], (wait, err)
    assert sc.p.adopt_forecast({"exec": wait}) and sc.p.plan is None
    sc.m.exec, sc.m.exec_ts = wait, time.time()
    await sc.tick(100.0, n=2)                     # стакан появился, рынок жив

    def wakes() -> list:
        return [h for h in sc.m.handoffs if h.get("kind") == "wait_level"]

    # 1) ЖДЁМ с уровнем 101 → будильник
    sc.p.review_ts = 0.0
    fake_ai.queue("mission_review", {"choice": "ЖДЁМ", "why": "пробоя 101 нет — жду", "entry": 101.0})
    await sc.tick(100.0)
    assert await sc.settle(lambda: fake_ai.count("mission_review") == 1 and not sc.p._review_busy)
    wk = sc.p._wake
    assert wk and wk["level"] == 101.0 and wk["dir"] == "up" and not wk["fired"], wk
    # 2) плановая перепроверка: ЖДЁМ без entry, а цена прошла 101, пока PRO думал → будильник цел и будит после ответа
    orig = fake_ai.pro_json

    async def thinking(system, user, *, route="pro", max_tokens=None):
        if route == "mission_review":             # пока PRO думает, рынок идёт за уровень
            FakeTinkoff.price = 101.3
            sc.p.prices.append(101.3)
        return await orig(system, user, route=route, max_tokens=max_tokens)
    sc.patch(fake_ai, "pro_json", thinking)
    sc.p.review_ts = 0.0
    fake_ai.queue("mission_review", {"choice": "ЖДЁМ", "why": "будильник на 101 стоит — ждём его"})
    await sc.tick(100.4)
    assert await sc.settle(lambda: fake_ai.count("mission_review") == 2 and not sc.p._review_busy)
    fake_ai.pro_json = orig
    ur = fake_ai.last_user["mission_review"]
    assert "БУДИЛЬНИК: уровень 101 из твоего ЖДЁМ" in ur and "при проходе цены код разбудит" in ur, ur[:1500]
    assert sc.m.reviews[-1]["wake_kept"] and len(wakes()) == 1, (sc.m.reviews[-1], sc.m.handoffs)
    assert "(пока ты думал над прошлым ответом)" in wakes()[0]["reason"] and sc.p._wake["fired"], wakes()
    # 3) PRO по поводу: снова ЖДЁМ у 101 → пила у уровня в окне PYTHIA_EVENT_COOL_SEC PRO не будит
    sc.p._last_review_ts -= mission.EVENT_MIN_GAP_SEC + 60
    sc.p.review_ts = 0.0
    fake_ai.queue("mission_review", {"choice": "ЖДЁМ", "why": "у 101 без объёма — вынос", "entry": 101.0})
    await sc.tick(101.3)
    assert await sc.settle(lambda: fake_ai.count("mission_review") == 3 and not sc.p._review_busy)
    ur = fake_ai.last_user["mission_review"]
    assert "ПОВОД ПЕРЕПРОВЕРКИ (внеплановая): цена 101.3 прошла уровень 101" in ur and "сработал в" in ur, ur[:1500]
    wk = sc.p._wake
    assert wk and wk["level"] == 101.0 and wk["dir"] == "down" and not wk["fired"], wk
    assert "будильник на 101 уже срабатывал в" in sc.p._situation_text(101.3)
    sc.p._last_review_ts -= mission.EVENT_MIN_GAP_SEC + 60
    for px in (100.95, 101.05, 100.94, 101.06):
        await sc.tick(px)
    assert len(wakes()) == 1 and fake_ai.count("mission_review") == 3, "пинг-понга у уровня нет"
    # 4) ответ КУПИТЬ снимает будильник
    sc.p.review_ts = 0.0
    fake_ai.queue("mission_review", {"choice": "КУПИТЬ", "why": "поглощение у 101", "invalidation": 100.3, "take": 104.0})
    await sc.tick(100.95)
    assert await sc.settle(lambda: fake_ai.count("mission_review") == 4 and not sc.p._review_busy)
    assert sc.p._wake is None and (sc.p.plan or sc.p.position or sc.p.pending), sc.p.last_action
    sc.note = ("ЖДЁМ у 101 → плановая без entry будильник не сняла; проход за раздумья → повод сразу; пила у 101 — "
               "PRO один раз; КУПИТЬ снял будильник")


async def s44_conditional_enter(sc: Scene) -> None:
    """5.4.4 (C5): приказ совета «сейчас» → PRO у двери отвечает ВОЙТИ, но с условием: «войти на откате к 99.3» (entry
    99.3, entry_kind откат) при цене 100 — это ожидание уровня, а не вход по текущей цене: засада переставлена на 99.3
    (план gate_level), заявки нет; цена дошла до уровня — вход без второго вопроса у двери (уровень назвал сам PRO, решение
    свежее). Уровень у самой цены (не дальше max(0.1 %, 2 шагов цены)) — обычный ВОЙТИ по текущей цене."""
    b = sc.broker
    assert sc.p.adopt_forecast(sc.ex("BUY", None, 110.0, 98.0)), sc.p.last_action
    fake_ai.queue("mission_entry", {"decision": "ВОЙТИ", "entry": 99.3, "entry_kind": "откат",
                                    "why": "войти на откате к 99.3 — там лимитный покупатель"})
    await sc.tick(100.0)
    assert fake_ai.count("mission_entry") == 1 and sc.p.pending is None and not b.placed, sc.p.last_action
    plan = sc.p.plan
    assert plan and plan["entry"] == 99.3 and plan["kind"] == "откат" and plan["src"] == "gate_level", plan
    g = sc.p.gates[-1]
    assert g["decision"] == "ВОЙТИ" and "условный ВОЙТИ: уровень откат @99.3 далеко от цены 100" in g["applied"], g
    assert "ВОЙТИ с условием" in sc.p.last_action and sc.p.state == "ЗАСАДА", (sc.p.last_action, sc.p.state)
    await sc.tick(99.7)                           # выше уровня — ждём, PRO у двери не дёргаем
    assert fake_ai.count("mission_entry") == 1 and sc.p.pending is None, sc.p.last_action
    await sc.tick(99.32)                          # уровень → вход по решению PRO без второго вопроса
    assert fake_ai.count("mission_entry") == 1 and sc.p.pending and sc.p.pending["side"] == "long", sc.p.last_action
    await sc.tick(99.32)
    assert sc.p.position and sc.p.position["entry"] < 99.5, sc.p.position
    # уровень у самой цены — обычный ВОЙТИ по текущей цене
    await sc.p._close_all(99.4, "тест: освободить", reanalyze=False)
    sc.p.pnls, sc.p.session_risk = [], trader_risk.SessionRisk(DEPOSIT)
    assert sc.p.adopt_forecast(sc.ex("BUY", None, 110.0, 98.0)), sc.p.last_action
    fake_ai.queue("mission_entry", {"decision": "ВОЙТИ", "entry": 99.95, "entry_kind": "откат",
                                    "why": "откат уже здесь, лента выкупает"})
    await sc.tick(100.0)
    assert sc.p.pending and "условный" not in sc.p.gates[-1]["applied"], (sc.p.last_action, sc.p.gates[-1])
    await sc.settle_ai()
    assert any(e["title"] == f"{MM()} у двери: ВОЙТИ" for e in sc.xevents()), [e["title"] for e in sc.xevents()]
    sc.note = "ВОЙТИ «на откате к 99.3» при 100 → засада у 99.3, не рыночный вход → у уровня вход без второго вопроса"


async def s45_drift_once(sc: Scene) -> None:
    """5.4.4 (D2): тренд, приказ совета «сейчас»; пока PRO думает у двери, цена каждый раз проходит 1.2 % в сторону
    сделки. Первый ВОЙТИ — один переспрос с пометкой кода о дрейфе (не засада по старой цене и не молча выброшенный
    ответ); второй ВОЙТИ дан уже после пометки — исполняется, раз цена не за тейком/стопом и (тейк − цена)/(цена − стоп)
    ≥ 1: заявка без третьего вопроса. Тот же тренд у близкого тейка: отношение < 1 — «ход отыгран: вход не исполнен», план
    снят, повод — к плановой перепроверке (PRO не дёргаем раньше)."""
    orig = fake_ai.money_json

    async def trending(system, user, *, route, max_tokens=None):
        if route == "mission_entry":              # пока PRO думает у двери, тренд уносит цену на +1.2 %
            px = round(sc.p.prices[-1] * 1.012, 4)
            FakeTinkoff.price = px
            sc.p.prices.append(px)
        return await orig(system, user, route=route, max_tokens=max_tokens)
    sc.patch(fake_ai, "money_json", trending)
    assert sc.p.adopt_forecast(sc.ex("BUY", None, 110.0, 97.0)), sc.p.last_action
    fake_ai.queue("mission_entry", {"decision": "ВОЙТИ", "why": "импульс, покупатель жмёт"},
                  {"decision": "ВОЙТИ", "why": "импульс жив и после дрейфа"})
    await sc.tick(100.0)                          # 1-й вопрос: ВОЙТИ, а цена уже 101.2 → один переспрос
    assert fake_ai.count("mission_entry") == 1 and not sc.broker.placed, sc.p.last_action
    assert "ВОЙТИ — переспрашиваю из-за дрейфа (1 раз)" in sc.p.last_action and sc.p.plan["drift_asked"] == 1
    await sc.tick(sc.p.prices[-1])                # 2-й вопрос с пометкой кода: ВОЙТИ, цена ещё +1.2 % → по отношению
    assert fake_ai.count("mission_entry") == 2 and sc.p.pending, sc.p.last_action
    assert "ПОМЕТКА КОДА: с момента решения цена прошла 1.20 % в сторону сделки" in fake_ai.last_user["mission_entry"]
    g = sc.p.gates[-1]
    assert "ВОЙТИ исполнен после дрейфа" in g["applied"] and "(тейк−цена)/(цена−стоп) = 1.4" in g["applied"], g
    await sc.tick(sc.p.prices[-1])
    assert sc.p.position and fake_ai.count("mission_entry") == 2, sc.p.last_action
    # тейк близко: второй ВОЙТИ после пометки — отношение < 1 → ход отыгран, план снят, повод к плановой
    await sc.p._close_all(sc.p.prices[-1], "тест: освободить", reanalyze=False)
    sc.p.pnls, sc.p.session_risk = [], trader_risk.SessionRisk(DEPOSIT)
    px0 = sc.p.prices[-1]
    assert sc.p.adopt_forecast(sc.ex("BUY", None, round(px0 * 1.035, 2), round(px0 * 0.97, 2))), sc.p.last_action
    rv = sc.p.review_ts                           # плановая перепроверка по приказу — её «ход отыгран» не подтягивает
    fake_ai.queue("mission_entry", {"decision": "ВОЙТИ", "why": "идём"}, {"decision": "ВОЙТИ", "why": "всё ещё идём"})
    await sc.tick(px0)
    await sc.tick(sc.p.prices[-1])
    assert fake_ai.count("mission_entry") == 4 and sc.p.plan is None and sc.p.pending is None, sc.p.last_action
    g = sc.p.gates[-1]
    assert "ход отыгран: вход не исполнен" in g["applied"] and "(тейк−цена)/(цена−стоп) = 0." in g["applied"], g
    assert "ход отыгран" in (sc.p._review_reason or "") and sc.p.review_ts == rv, "повод к плановой — PRO раньше не зовём"
    await sc.settle_ai()
    assert any(e["title"] == "Ход отыгран: вход не исполнен" for e in sc.xevents()), [e["title"] for e in sc.xevents()]
    sc.note = ("тренд +1.2 % за каждую мысль двери: ВОЙТИ → один переспрос → ВОЙТИ исполнен (отношение 1.4); "
               "близкий тейк → «ход отыгран», план снят, повод к плановой")


# ── v5.4.4 «связь с брокером»: жалоба владельца 30.09.2026 — токен отозван, пилот «работал без перерыву, как минимум так
#    показывал», вход и закрытие «написаны, но не исполнены» ─────────────────────────────────────────────────────────
_AUTH_REASON = "токен Т-Банка не принят (40003) — выпусти новый с полным доступом и вставь в «Ключи»"


def _health_now(market_open: bool = True) -> list[dict]:
    """Панель проблем по живому снимку миссии (api_v5.health_block; ключи — «есть»)."""
    from . import api_v5
    hb = api_v5.health_block(keys={"deepseek": True, "tinkoff": True, "dry": False}, mission_snap=mission.snapshot(),
                             market={"open": market_open, "enabled": True})
    return [p for p in hb["problems"] if p.get("kind") in ("pilot", "data")]


async def s44_token_revoked(sc: Scene) -> None:
    """Токен Т-Банка отозван (401/40003 на всё) — пилот не тикает и говорит это честно: НЕТ_ДОСТУПА, фаза no_access
    (не «В позиции»), позиция названа без защиты, PRO не зовётся ни по перепроверке, ни у троса; панель проблем — err
    с причиной, без «рынок мёртв»; новый токен — связь вернулась сама, сверка со счётом, пилот снова ведёт."""
    pos = await sc.open_position("long", inv=98.0)
    p = sc.p
    p.loop_alive = True                            # петля «жива»: шаги — руками (_loop_step), как в run()
    seen: list[tuple[bool, dict]] = []
    p._on_feed_change = lambda ok, info: seen.append((ok, info))
    FakeTinkoff.price = None                       # цены нет: GetLastPrices → 401
    FakeTinkoff.price_err = {"ts": time.time(), "path": "GetLastPrices", "status": 401, "code": "40003", "kind": "auth",
                             "reason": _AUTH_REASON, "text": "Tinkoff 401 · 40003: Authentication token is missing or invalid"}
    FakeTinkoff.access = {"ok": False, "trade": False, "kind": "auth", "reason": _AUTH_REASON}
    n_orders, ai0 = len(sc.broker.placed), dict(fake_ai.summary()["routes"])
    slow = await p._loop_step()
    assert slow and p.feed["ok"] is False and p.feed["kind"] == "auth" and p.state == "НЕТ_ДОСТУПА", (p.feed, p.state)
    assert FakeTinkoff.checks == 1, "проверка брокера (GetAccounts) уточнила причину сразу"
    la = p.last_action
    assert "НЕТ ДОСТУПА" in la and "40003" in la and "без защиты" in la and "long 8 лот" in la, la
    assert seen and seen[0][0] is False and seen[0][1]["kind"] == "auth", seen
    st = mission.status(TICKER)
    assert st["phase"] == "no_access" and st["pilot"]["ticking"] is False and st["pilot"]["feed"]["kind"] == "auth", st["phase"]
    assert p.broker_ok() is False
    # плановая перепроверка «пора», цена улетела бы за триггер — но без связи ни тика, ни PRO, ни заявок
    p.review_ts = 0.0
    for _ in range(5):
        await p._loop_step()
    assert FakeTinkoff.checks == 1, "проверка брокера — не чаще раза в FEED_PROBE_SEC"
    assert p.last_action == la, "причина записана один раз — не переписывается каждый шаг"
    assert dict(fake_ai.summary()["routes"]) == ai0 and len(sc.broker.placed) == n_orders, fake_ai.summary()
    probs = _health_now()
    txt = " | ".join(x["text"] for x in probs)
    assert any(x["level"] == "err" and x["key"] == "pilot:feed" for x in probs), probs
    assert "нет доступа" in txt and "БЕЗ ЗАЩИТЫ" in txt and "мёртв" not in txt and "старой цене" not in txt, txt
    r = await mission.resume(TICKER)               # «ПРОДОЛЖИТЬ» у зомби — честно, а не «пилот уже работает»
    assert not r["ok"] and "не работает" in r["note"] and "40003" in r["note"], r
    # новый токен в «Ключах»: эпоха растёт → проверка сразу; цена пошла — связь восстановлена, сверка со счётом
    FakeTinkoff.epoch += 1
    FakeTinkoff.access, FakeTinkoff.price_err = None, None
    FakeTinkoff.price = 100.4
    await p._loop_step()
    assert p.feed["ok"] is True and seen[-1][0] is True and seen[-1][1]["was"] == "auth", (p.feed, seen)
    assert p.position is pos and p.state == "В_ПОЗИЦИИ" and p.ticking(), (p.state, p.last_action)
    assert mission.status(TICKER)["phase"] == "in_position"
    sc.note = ("401 на всё → НЕТ_ДОСТУПА (no_access), «без защиты» в last_action один раз, 0 вызовов PRO и 0 заявок, "
               "панель err без «рынок мёртв»; новый токен → связь вернулась, пилот ведёт")


async def s45_orders_rights(sc: Scene) -> None:
    """Чтение работает, заявки отбиваются 403/40002 (токен только для чтения): одна попытка входа, сразу НЕТ_ДОСТУПА —
    без лесенки попыток, без петли PRO у двери и перепроверки; новый токен с полным доступом — вход исполнен; закрытие,
    отбитое 30042, повторяется с паузой, а не каждый тик, и доходит до конца."""
    b, p = sc.broker, sc.p
    p.loop_alive = True
    b.place_error = "Tinkoff 403 · 40002: Insufficient privileges [PostOrder]"
    FakeTinkoff.access = {"ok": True, "trade": False, "kind": "rights", "access": "READ_ONLY",
                          "reason": "токен принят, но только для чтения — пилот не сможет торговать"}
    assert p.adopt_forecast(sc.ex("BUY"))
    await sc.tick(100.0)                           # дверь (PRO: ВОЙТИ) → заявка → 403
    assert b._n == 1 and p.feed["ok"] is False and p.feed["kind"] == "rights" and p.state == "НЕТ_ДОСТУПА", (p.feed, p.state)
    br = p.status()["broker_refusal"]
    assert br["what"] == "entry" and br["code"] == "40002" and br["kind"] == "rights" and p._entry_fail == 0, br
    n_entry = fake_ai.count("mission_entry")
    p.review_ts = 0.0
    p._feed_probe_ts -= ai_pilot.FEED_PROBE_SEC    # прошло FEED_PROBE_SEC: следующий шаг спросит брокера (GetAccounts)
    for _ in range(8):                             # цена идёт, а торговать нечем: ни тика решений, ни PRO, ни заявок
        await p._loop_step()
    assert FakeTinkoff.checks == 1, "проверка брокера — один раз за FEED_PROBE_SEC"
    assert b._n == 1 and fake_ai.count("mission_entry") == n_entry and fake_ai.count("mission_review") == 0, fake_ai.summary()
    assert not any("серия отказов" in str(h.get("reason")) for h in sc.m.handoffs), sc.m.handoffs
    assert p.plan is not None, "приказ ждёт доступа, а не сброшен лесенкой отказов"
    assert "только для чтения" in p.feed["reason"] and mission.status(TICKER)["phase"] == "no_access", p.feed
    # владелец вставил токен с полным доступом: проверка сразу (новая эпоха), доступ есть — вход исполнен
    b.place_error = None
    FakeTinkoff.access = None
    FakeTinkoff.epoch += 1
    await p._loop_step()
    await sc.wait_gate()
    await sc.tick(100.0)
    assert p.feed["ok"] and p.position and p.position["lots"] == 8 and p.broker_refusal is None, (p.feed, p.last_action)
    # закрытие отбито 30042: пауза 2→4→… с, а не заявка каждый тик; причина видна; после паузы — закрыто
    b.place_error = "30042: недостаточно средств/обеспечения для сделки"
    assert p.adopt_forecast(sc.ex("CLOSE"))
    await sc.tick(100.0)
    n = b._n
    assert p.position and p.position.get("close_refusals") == 1 and p.status()["broker_refusal"]["what"] == "close"
    for _ in range(6):
        await sc.tick(100.0)
    assert b._n == n and "повтор через" in p.last_action and "30042" in p.last_action, p.last_action
    b.place_error = None
    p.position["close_retry_at"] = time.time() - 1      # пауза вышла
    await sc.tick(100.0)
    assert p.position is None and p.broker_refusal is None, p.last_action
    sc.note = ("403/40002: 1 заявка, 1 вопрос у двери, 0 перепроверок — НЕТ_ДОСТУПА до нового токена; полный доступ → "
               "вход; 30042 на закрытии — пауза, не каждый тик")


async def s46_prepare_unread_portfolio(sc: Scene) -> None:
    """prepare() без портфеля (GetPortfolio не ответил) не стирает state-файл: позиция из файла поднимается «сверить со
    счётом», входов и доборов нет до сверки; первая удачная сверка подтверждает. Счёт не прочитан (токен) — пилот не
    стартует, причина — в ошибке миссии, файл цел."""
    path = Path(tempfile.mkdtemp(prefix="pythia_scen_")) / "aipilot_state.json"
    rec = {"figi": FIGI, "base": TICKER, "ts": time.time(), "account_id": "acc-1", "mode": "real", "pending": None,
           "foreign_lots": 0, "close_pending": None,
           "position": {"side": "long", "entry": 100.0, "lots": 3, "take": 110.0, "invalidation": 98.0, "inv0": 98.0,
                        "hard_stop": 96.53, "opened_ts": time.time() - 3600, "stop_id": None, "holds": 0}}
    path.write_text(json.dumps(rec), encoding="utf-8")

    def fresh_pilot() -> mission.MissionPilot:
        q = mission.MissionPilot(TICKER, deposit=DEPOSIT, broker=sc.broker, mission=sc.m)
        mission._bind_pilot(sc.m, q)
        q._state_path = path
        return q

    FakeTinkoff.pf_fail = True                     # счёт есть, портфель не прочитан
    p2 = fresh_pilot()
    assert await ai_pilot.AIPilot.prepare(p2) is True and path.exists(), p2.last_action
    q = p2.position
    assert q and q["lots"] == 3 and q.get("unverified") and p2._acct_unverified, q
    assert "СВЕРИТЬ СО СЧЁТОМ" in p2.last_action and "портфель при старте не прочитан" in p2.last_action, p2.last_action
    assert p2.status()["account_unverified"] is True
    sc.m.pilot, sc.p = p2, p2
    p2.adopt_forecast(sc.ex("BUY", entry=99.9))     # добор той же стороны у уровня — ждёт сверки
    sc.broker.pf_error = "GetPortfolio: 500 (тест)"   # портфель брокера всё ещё не читается — сверка откладывается
    FakeTinkoff.pf_fail = False
    p2._tick_n = 1
    await sc.tick(99.95)
    assert not sc.broker.placed and "ждёт сверки со счётом" in p2.last_action, p2.last_action
    assert p2._acct_unverified and p2.position.get("unverified") and path.exists()
    sc.broker.pf_error = None
    sc.broker.pf_positions = [{"figi": FIGI, "qty": 3, "avg": 100.0}]    # сверка: на счёте те же 3 лота
    p2._tick_n = 7
    await sc.tick(99.95)
    assert not p2._acct_unverified and not p2.position.get("unverified"), p2.last_action
    assert path.exists() and p2.position["lots"] == 3
    # счёт не прочитан вовсе (токен): пилот не стартует, причина наружу, state-файл цел
    FakeTinkoff.accs_fail = True
    p3 = fresh_pilot()
    sc.m.pilot = p3
    assert await ai_pilot.AIPilot.prepare(p3) is False and path.exists()
    assert "счёт Т-Банка не прочитан" in (p3.prepare_error or ""), p3.prepare_error
    mission._pilot_prepared(sc.m, p3, False, p3.prepare_error)
    assert sc.m.error.startswith("пилот не стартовал") and sc.m.phase == "error", (sc.m.error, sc.m.phase)
    sc.m.pilot, sc.m.error, sc.m.phase = p2, None, "in_position"
    sc.p = p2
    sc.note = "портфель не прочитан → позиция из файла «сверить со счётом», входов нет до сверки, сверка подтвердила; счёт не прочитан → не стартовал с причиной, файл цел"


SCENARIOS: list[tuple[str, Callable[[Scene], Awaitable[None]]]] = [
    ("гэп_за_трос", s01_gap_hard), ("гэп_за_триггер", s02_gap_trigger), ("мёртвый_рынок", s03_dead_market),
    ("рынок_закрыт", s04_market_closed), ("частичка_30042", s05_partial_then_30042),
    ("резкий_ход", s06_shock_move), ("тейк", s07_take_hit), ("шквал_новостей", s08_news_storm),
    ("рестарт", s09_restart), ("руки_владельца", s10_owner_hands), ("close_в_полёте", s11_close_in_flight),
    ("killswitch", s12_killswitch), ("flash_молчит", s13_flash_silent), ("pro_молчит", s14_pro_silent),
    ("стоп_отбит", s15_stop_refused), ("переворот_тишина", s16_flip_quiet), ("добор_ноль", s17_topup_zero),
    ("двойное_закрытие", s18_double_close),
    ("тейк_подержать", s19_take_hold_council), ("тейк_flash_молчит", s20_take_flash_silent),
    ("тейк_откат", s21_take_lock_pullback), ("триаж_событий", s22_event_triage),
    ("прокол_план", s23_puncture_plan), ("прокол_против_позиции", s24_puncture_against_position),
    ("прокол_ниже_порога", s25_puncture_below_threshold),
    ("стоп_запрос_завис", s26_stop_request_stale), ("стопа_нет_в_списке", s27_cancel_stop_gone),
    ("база_не_читается", s28_unreadable_store_panic), ("паника_без_списка_стопов", s29_panic_stops_unavailable),
    ("владелец_продал_в_выходе", s30_owner_sells_during_exit), ("паника_не_липнет", s31_panic_does_not_stick),
    # v5.4.1 «ТРЕЗВЫЙ ПИЛОТ»: проверка входа у двери, мысль о прибыли, рывок в плюсе, приказ WAIT, стопы в программе
    ("вход_проверка_ждать", s32_gate_wait), ("вход_проверка_отмена", s33_gate_cancel), ("вход_pro_молчит", s34_gate_silent),
    ("прибыль_выйти", s35_profit_exit), ("прибыль_перезайти", s36_profit_reenter), ("прибыль_совет", s37_profit_council),
    ("рывок_в_плюсе", s38_shock_profit), ("совет_вне_рынка", s39_council_wait), ("стопы_в_программе", s40_program_stops),
    # ревью 5.4.2: совет «держать» — HOLD без добора
    ("совет_держать", s41_council_hold),
    # v5.4.3 «решительный пилот»: уровень из ЖДЁМ — будильник, а не молча выброшенное число
    ("ждём_будильник", s42_wait_alarm),
    # ревью 5.4.3: будильник снимается только явным решением, проход за раздумья — повод, без пинг-понга у уровня
    ("будильник_держит", s43_wait_alarm_kept),
    # 5.4.4 (X2): условный ВОЙТИ у двери — засада у уровня; дрейф — не больше одного переспроса, второй ВОЙТИ исполняется
    ("вход_условный", s44_conditional_enter), ("дрейф_один_переспрос", s45_drift_once),
    # v5.4.4 «связь с брокером»: токен отозван — честно и без ИИ; заявки 40002 — без петли PRO; портфель не прочитан ≠ 0 лотов
    ("токен_отозван", s44_token_revoked), ("заявки_40002", s45_orders_rights),
    ("портфель_не_прочитан", s46_prepare_unread_portfolio),
]


def _title(fn) -> str:
    return " ".join((fn.__doc__ or fn.__name__).split()).split(" TODO")[0].strip()


async def run_one(name: str, fn, play: str = "auto") -> dict:
    async with Scene(name, _title(fn), play) as sc:
        try:
            await fn(sc)
            return sc.result(True)
        except Exception as e:                    # noqa: BLE001 — сценарий упал: честно в отчёт
            fr = [f for f in traceback.extract_tb(e.__traceback__) if f.filename.endswith("scenarios.py")]
            where = f" [строка {fr[-1].lineno}: {fr[-1].line}]" if fr else ""
            err = f"{type(e).__name__}: {str(e)[:300]}{where}"
            log.warning("сценарий %s: %s", name, err)
            return sc.result(False, err)


async def run_all(only: list[str] | None = None) -> dict:
    """Все сценарии по очереди → {"ok", "passed", "total", "scenarios": [...], "table": str}."""
    out = []
    for name, fn in SCENARIOS:
        if only and name not in only:
            continue
        out.append(await run_one(name, fn))
    passed = sum(1 for r in out if r["ok"])
    return {"ok": passed == len(out), "passed": passed, "total": len(out), "scenarios": out, "table": table(out)}


def table(rows: list[dict]) -> str:
    """Текстовая таблица по сценариям: итог, вызовы ИИ (FLASH/PRO по маршрутам), ордера и стопы,
    сделки в журнале, последнее действие пилота, объяснение толмача, итог сцены."""
    pad = " " * 6
    L = [f"{'№':>2} {'сценарий':<18} {'итог':<5} {'FLASH':>5} {'PRO':>4}  маршруты ИИ"]
    L.append("-" * 100)
    for i, r in enumerate(rows, 1):
        ai = r["ai"]
        routes = ", ".join(f"{k.replace('mission_', '')}×{v}" for k, v in sorted(ai["routes"].items())) or "—"
        L.append(f"{i:>2} {r['name']:<18} {'OK' if r['ok'] else 'FAIL':<5} {ai['FLASH']:>5} {ai['PRO']:>4}  {routes}")
        L.append(f"{pad}{r['title'][:110]}")
        L.append(f"{pad}ордера: {', '.join(r['orders']) or 'нет'} · стопов {r['stops']} · сделок в журнале {r['trades']}")
        L.append(f"{pad}пилот: {str(r['last_action'])[:110]}")
        if r.get("explain"):
            L.append(f"{pad}толмач [{r.get('explain_title', '')[:60]}]: {str(r['explain'])[:110]}")
        if r.get("note"):
            L.append(f"{pad}итог: {r['note'][:110]}")
        if r.get("error"):
            L.append(f"{pad}ОШИБКА: {r['error']}")
    return "\n".join(L)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO if os.getenv("PYTHIA_SCEN_VERBOSE") else logging.ERROR,
                        format="%(levelname)s %(name)s: %(message)s")
    only = [a for a in os.sys.argv[1:] if a] or None
    rep = asyncio.run(run_all(only))
    print(rep["table"])
    bad = [r for r in rep["scenarios"] if not r["ok"]]
    assert rep["total"] == (len(only) if only else len(SCENARIOS)), rep["total"]
    assert not bad, "провалены: " + ", ".join(f"{r['name']} ({r['error']})" for r in bad)
    for r in rep["scenarios"]:
        assert isinstance(r["ai"]["FLASH"], int) and isinstance(r["ai"]["PRO"], int) and isinstance(r["orders"], list)
    json.dumps(rep)                                    # отчёт сериализуем (для API/панели)
    print(f"scenarios self-test OK: {rep['passed']}/{rep['total']} сценариев биржевых ситуаций зелёные "
          "(гэп за трос/триггер, мёртвый рынок, стопор и открытие рынка, частичка + 30042, резкий ход с триажем, "
          "мягкий тейк (зафиксировать), шквал новостей, рестарт, руки владельца, CLOSE в полёте, killswitch, молчание "
          "FLASH/PRO, отбитый стоп, переворот и тишина, добор при нуле, двойное закрытие; W2: тейк подержать → совет, "
          "FLASH у тейка молчит, запертая прибыль при откате, триаж событий ПЛАНОВО/САМ/молчит/СЕЙЧАС; W3: прокол сканера "
          "в сторону плана → PRO с блоком ПРОКОЛ СКАНЕРА, прокол против позиции → триаж и дедуп, ниже порога — тишина; "
          "W4: запрос стопа завис → список стопов решает, стопа нет в списке → отмена подтверждена, база/state не читаются → "
          "паника всё равно, GetStopOrders недоступен → force, продажа владельца во время выхода, паника не липнет к тикеру; "
          "v5.4.1: PRO у двери ЖДАТЬ/ОТМЕНИТЬ/молчит, мысль о прибыли ВЫЙТИ/ПЕРЕЗАЙТИ/СОВЕТ, рывок в плюсе, приказ WAIT, "
          "стопы только в программе; ревью 5.4.2: совет «держать» — HOLD без добора, молчание у троса/тейка — запись кода; "
          "v5.4.3: ЖДЁМ с уровнем — будильник, проход цены → PRO решает заново; ревью 5.4.3: ЖДЁМ без entry будильник "
          "не снимает, проход за раздумья — повод, пила у уровня не будит повторно; 5.4.4: условный ВОЙТИ у двери — "
          "засада у уровня, дрейф — один переспрос, второй ВОЙТИ по отношению исполнен или «ход отыгран»; "
          "v5.4.4: токен отозван — НЕТ_ДОСТУПА без "
          "ИИ и заявок, новый токен — связь сама; заявки 40002 — одна попытка, без петли PRO; закрытие 30042 — с паузой; "
          "портфель не прочитан ≠ 0 лотов — state-файл цел)")
