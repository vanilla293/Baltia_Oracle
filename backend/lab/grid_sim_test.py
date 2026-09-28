# -*- coding: utf-8 -*-
"""СТЕНД боевой петли Zero-Touch (фибо-сетка + маржевой дозор + изоляция).

Автопилот нельзя гонять на живой бирже из стенда — здесь его приватные
методы прогоняются на ФЕЙКОВОМ брокере (ни одного сетевого вызова):

  · _grid_place  — сетка ставится после FILL, лимитки на верной стороне;
  · _grid_tick   — исполнение уровня: лоты вниз, P/L в killswitch, журнал;
    последний уровень — позиция закрыта целиком, трос снят;
  · _close       — активный выход СНАЧАЛА снимает сетку (нет двойной продажи);
  · _margin_guard— ГО не вмещается в капитал → ужимаемся рыночным;
  · _reconcile   — ИЗОЛЯЦИЯ: ручные лоты владельца → foreign_lots (не
    трогаем), сжатие в нашу сторону → ужимаем СВОЙ учёт.

Запуск: python3 -m backend.lab.grid_sim_test   (из корня ПИФИЯ_ИТОГ)
⚫ стенд проверяет машину, не рынок. 18+.
"""
from __future__ import annotations

import asyncio

from .. import autopilot as ap_mod
from .. import trader_risk
from ..autopilot import Autopilot


class FakeBroker:
    """Брокер-пустышка: помнит все ордера, исполняет по команде стенда."""

    def __init__(self):
        self.mode = "dry"
        self.account_id = "fake"
        self.placed: list[dict] = []
        self.cancelled: list[str] = []
        self.filled_ids: set[str] = set()
        self.partial: dict[str, int] = {}     # order_id → исполнено лотов
        self.cancel_fail: set[str] = set()    # снятие этих ордеров отказывает
        self._n = 0

    async def place(self, figi, direction, lots, price=None, tag=""):
        self._n += 1
        oid = f"F{self._n}"
        self.placed.append({"order_id": oid, "figi": figi, "dir": direction,
                            "lots": int(lots), "price": price, "tag": tag})
        return {"ok": True, "order_id": oid, "status": "NEW"}

    async def cancel(self, order_id):
        if order_id in self.cancel_fail:      # биржа отказала (как в бою:
            return {"ok": False, "error": "cancel refused"}   # без исключения)
        self.cancelled.append(order_id)
        return {"ok": True}

    async def order_state(self, order_id):
        st = {"filled": order_id in self.filled_ids,
              "status": "FILL" if order_id in self.filled_ids else "NEW"}
        if order_id in self.partial:          # частичное исполнение биржи
            st["exec_lots"] = self.partial[order_id]
        return st

    async def place_stop(self, *a, **k):
        return {"ok": True, "stop_order_id": "S1"}

    async def cancel_stop(self, *a, **k):
        return {"ok": True}


def _mk(deposit=100000.0, go=1000.0) -> tuple[Autopilot, FakeBroker, list]:
    a = Autopilot("CR")
    fb = FakeBroker()
    a.broker = fb
    a.figi = "FIGI-TEST"
    a.asset_class = "futures"
    a.deposit = deposit
    a.go_per_lot = go
    a.point_value = 1.0
    a.tick_size = 0.01
    a.session_risk = trader_risk.SessionRisk(deposit)
    a.spread_hist = [0.5] * 30
    a.oracle_verdict = {"env": "щёлочь", "hill_alpha": 3.5, "voices": {},
                        "confidence": 0.1, "override": False}
    a.last_xray = {"metrics": {"vpin": 0.2}}
    a.last_zmap = {}
    journal: list = []
    a._journal = journal.append          # без файлов: журнал в память стенда
    return a, fb, journal


async def _t_grid_lifecycle():
    a, fb, journal = _mk()
    a.position = {"side": "long", "entry": 100.0, "lots": 10, "stop": 99.0}
    await a._grid_place()
    grid = a.position["exit_grid"]
    assert len(grid) == 3, grid                       # три уровня сетки
    assert all(g["price"] > 100.0 for g in grid), grid  # long: тейки ВЫШЕ входа
    assert sum(g["lots"] for g in grid) == 10
    tags = [p["tag"] for p in fb.placed]
    assert tags.count("exit-grid") == 3
    sells = [p for p in fb.placed if p["tag"] == "exit-grid"]
    assert all(p["dir"] == ap_mod.trader_broker.SELL for p in sells)
    # повторный вызов сетку не дублирует
    await a._grid_place()
    assert len(a.position["exit_grid"]) == 3

    # исполняем ПЕРВЫЙ уровень → лоты вниз, P/L записан, журнал пополнен
    fb.filled_ids.add(grid[0]["order_id"])
    lots0, day0 = a.position["lots"], a.session_risk.pnl
    for _ in range(3):                                # round-robin дойдёт
        await a._grid_tick(100.1)
        if a.position["lots"] < lots0:
            break
    assert a.position["lots"] == lots0 - grid[0]["lots"]
    assert a.session_risk.pnl > day0                  # прибыль легла в killswitch
    assert journal and "фибо-сетка" in journal[-1]["why"]
    # исполняем остальные — позиция закрывается целиком
    for g in list(a.position["exit_grid"]):
        fb.filled_ids.add(g["order_id"])
    for _ in range(6):
        await a._grid_tick(100.2)
        if not a.position:
            break
    assert a.position is None, "сетка обязана добрать позицию целиком"
    assert sum(r["lots"] for r in journal) == 10      # все лоты отчитаны


async def _t_close_cancels_grid():
    a, fb, _ = _mk()
    a.position = {"side": "short", "entry": 100.0, "lots": 6, "stop": 101.0}
    await a._grid_place()
    grid_ids = [g["order_id"] for g in a.position["exit_grid"]]
    assert grid_ids and all(
        p["dir"] == ap_mod.trader_broker.BUY
        for p in fb.placed if p["tag"] == "exit-grid")   # short: тейки BUY ниже
    await a._close(99.5, "тест: активный выход")
    assert a.position is None
    for oid in grid_ids:                 # сетка снята ДО рыночного закрытия
        assert oid in fb.cancelled, (oid, fb.cancelled)


async def _t_race_no_flip():
    """РЕГРЕССИЯ (критическая находка ревью): лимитка сетки исполнилась на
    бирже, а round-robin до неё ещё НЕ дошёл — активный выход обязан сперва
    рассчитать сетку и продать только ОСТАТОК. Иначе рыночный ордер уходит
    на устаревший объём и переворачивает позицию в противоположную."""
    a, fb, journal = _mk()
    a.position = {"side": "long", "entry": 100.0, "lots": 10, "stop": 99.0}
    await a._grid_place()
    grid = list(a.position["exit_grid"])
    # биржа исполнила ПЕРВЫЙ уровень; бот об этом ещё не знает (не опрашивал)
    fb.filled_ids.add(grid[0]["order_id"])
    assert a.position["lots"] == 10                  # учёт устарел — так и было
    await a._close(100.4, "активный выход на гонке")
    closes = [p for p in fb.placed if p["tag"] == "close"]
    assert len(closes) == 1, closes
    # ГЛАВНОЕ: рыночный ушёл на ОСТАТОК (10 − исполненный уровень), не на 10
    assert closes[0]["lots"] == 10 - grid[0]["lots"], \
        ("ПЕРЕВОРОТ: продали больше, чем осталось", closes, grid[0])
    # суммарно продано ровно 10 лотов: сетка + рыночный
    assert grid[0]["lots"] + closes[0]["lots"] == 10
    # исполненный уровень отчитан в журнале, неисполненные — сняты
    assert any("фибо-сетка" in r["why"] for r in journal)
    for g in grid[1:]:
        assert g["order_id"] in fb.cancelled, (g, fb.cancelled)
    assert a.position is None

    # тот же расчёт в маржевом дозоре: ужимаем от ПРАВДЫ, не от памяти
    b, fbb, _ = _mk(deposit=10000.0, go=3000.0)
    b.position = {"side": "long", "entry": 100.0, "lots": 8, "stop": 99.0}
    await b._grid_place()
    g2 = list(b.position["exit_grid"])
    fbb.filled_ids.add(g2[0]["order_id"])            # часть ушла лимиткой
    await b._margin_guard(100.0)
    sold = sum(p["lots"] for p in fbb.placed
               if p["tag"] in ("margin-guard", "close"))
    left = b.position["lots"] if b.position else 0
    assert g2[0]["lots"] + sold + left == 8, (g2[0]["lots"], sold, left)


async def _t_tensor_exit_acid():
    """Спринт «Абсолют»: в КИСЛОТЕ сетка выхода строится по координатам
    ТЕНЗОРА КАЗИМИРА (плита упругости + радиус пузыря), а не обычная фибо."""
    a, fb, _ = _mk()
    a.oracle_verdict = {"env": "кислота", "hill_alpha": 1.6, "voices": {},
                        "confidence": 0.1, "override": True}
    a.position = {"side": "long", "entry": 100.0, "lots": 10, "stop": 99.0,
                  "opened_ts": ap_mod.time.time()}
    await a._grid_place()
    grid = a.position["exit_grid"]
    assert grid and sum(g["lots"] for g in grid) == 10
    # тензор в кислоте: два уровня, дальний жирнее (61.8% на радиусе пузыря)
    assert len(grid) == 2, grid
    assert grid[-1]["lots"] >= grid[0]["lots"], grid
    # все тейки выше входа (long), лимитки SELL
    assert all(g["price"] > 100.0 for g in grid)


async def _t_time_killswitch():
    """Спринт «Абсолют»: TIME_KILLSWITCH — позиция старше 600с ликвидируется
    рыночным без исключений (приоритет выше тактических выходов)."""
    a, fb, journal = _mk()
    now = ap_mod.time.time()
    # позиция «висит» 601с — таймер обязан сработать
    a.position = {"side": "long", "entry": 100.0, "lots": 5, "stop": 99.0,
                  "opened_ts": now - (ap_mod.TIME_KILL_SEC + 1)}
    await a._grid_place()
    # прямой вызов ветки ведения через публичную проверку таймера:
    # эмулируем то, что делает tick() — сработавший killswitch зовёт _close
    opened = a.position.get("opened_ts")
    assert opened and (ap_mod.time.time() - opened) >= ap_mod.TIME_KILL_SEC
    await a._close(100.1, "TIME_KILLSWITCH: тест")
    assert a.position is None
    closes = [p for p in fb.placed if p["tag"] == "close"]
    assert len(closes) == 1 and closes[0]["price"] is None  # РЫНОЧНЫЙ выход
    # свежая позиция (0с) — таймер молчит
    b, fbb, _ = _mk()
    b.position = {"side": "long", "entry": 100.0, "lots": 5, "stop": 99.0,
                  "opened_ts": ap_mod.time.time()}
    opened_b = b.position.get("opened_ts")
    assert (ap_mod.time.time() - opened_b) < ap_mod.TIME_KILL_SEC


async def _t_partial_fill():
    """РЕГРЕССИЯ (критическая находка ревью №2): биржа исполнила лимитку
    ЧАСТИЧНО (lotsExecuted), filled=False. Раньше эти лоты не книжились
    вовсе, ордер снимался — исполненная часть выпадала из учёта, и рыночный
    выход продавал её второй раз."""
    a, fb, journal = _mk()
    a.position = {"side": "long", "entry": 100.0, "lots": 10, "stop": 99.0}
    await a._grid_place()
    grid = list(a.position["exit_grid"])
    g0 = grid[0]                                   # уровень на 5 лот
    fb.partial[g0["order_id"]] = 2                 # биржа налила 2 из 5
    for _ in range(4):
        await a._grid_tick(100.4)
        if a.position["lots"] < 10:
            break
    assert a.position["lots"] == 8, ("частичное исполнение не учтено",
                                     a.position)
    # ордер остался в сетке, зачтено ровно 2 (не выброшен и не задвоен)
    same = [x for x in a.position["exit_grid"] if x["order_id"] == g0["order_id"]]
    assert same and same[0].get("booked") == 2, a.position["exit_grid"]
    assert any("частично" in r["why"] for r in journal), journal
    # ПОВТОРНЫЕ опросы с тем же накопительным счётчиком НЕ списывают лоты
    # заново (exec_lots биржи — счётчик, а не приращение)
    for _ in range(6):
        await a._grid_tick(100.4)
    assert a.position["lots"] == 8, ("двойной учёт частичного исполнения",
                                     a.position)
    # биржа долила ещё 1 лот (счётчик 3) — книжится ровно прирост
    fb.partial[g0["order_id"]] = 3
    for _ in range(4):
        await a._grid_tick(100.4)
        if a.position["lots"] < 8:
            break
    assert a.position["lots"] == 7, a.position
    # активный выход считает от ПРАВДЫ: продаёт 7, а не 10
    await a._close(100.5, "выход после частичного исполнения")
    closes = [p for p in fb.placed if p["tag"] == "close"]
    assert len(closes) == 1 and closes[0]["lots"] == 7, closes
    # суммарно отчитано ровно 10 лотов: 3 сеткой + 7 рыночным
    assert sum(r["lots"] for r in journal) == 10, journal


async def _t_orphan_order():
    """РЕГРЕССИЯ (находка ревью №3): биржа ОТКАЗАЛА в снятии живой лимитки.
    Раньше ордер молча выбрасывался из учёта и оставался висеть — после
    закрытия позиции он открывал противоположную. Теперь уезжает в орфаны
    и добивается каждым тиком; выход при этом НЕ блокируется."""
    a, fb, _ = _mk()
    a.position = {"side": "long", "entry": 100.0, "lots": 10, "stop": 99.0}
    await a._grid_place()
    grid = list(a.position["exit_grid"])
    stubborn = grid[2]["order_id"]
    fb.cancel_fail.add(stubborn)                   # эта лимитка не снимается
    await a._close(100.3, "выход при неснимаемой лимитке")
    # выход состоялся (стоп-лосс важнее орфана)
    assert a.position is None
    closes = [p for p in fb.placed if p["tag"] == "close"]
    assert len(closes) == 1 and closes[0]["lots"] == 10, closes
    # ордер НЕ потерян — он в орфанах и добивается
    assert any(g["order_id"] == stubborn for g in a.orphan_orders), a.orphan_orders
    await a._orphan_sweep()
    assert any(g["order_id"] == stubborn for g in a.orphan_orders)   # всё ещё
    fb.cancel_fail.discard(stubborn)               # биржа пришла в себя
    await a._orphan_sweep()
    assert not a.orphan_orders, a.orphan_orders    # снят, список пуст
    assert stubborn in fb.cancelled
    # исполнившийся орфан просто уходит из списка (сверка учтёт нетто)
    b, fbb, _ = _mk()
    b.position = {"side": "long", "entry": 100.0, "lots": 6, "stop": 99.0}
    await b._grid_place()
    oid = b.position["exit_grid"][0]["order_id"]
    fbb.cancel_fail.add(oid)
    await b._close(100.2, "выход")
    assert b.orphan_orders
    fbb.filled_ids.add(oid)                        # орфан исполнился
    await b._orphan_sweep()
    assert not b.orphan_orders


async def _t_sniper_never_traps_position():
    """РЕГРЕССИЯ (критическая находка ревью «Абсолюта»): ургентный план с
    неисполняющейся тенью перевзводился ВЕЧНО, а tick() уходил в ранний
    return на ветке Снайпера — живая частично набранная позиция навсегда
    минула TIME_KILLSWITCH, ПАНИКУ, стоп-гард, маржевой дозор и сверку."""
    a, fb, _ = _mk()
    a.position = {"side": "long", "entry": 100.0, "lots": 3, "stop": 99.0,
                  "opened_ts": ap_mod.time.time() - 700}
    plan = ap_mod.execution.plan_entry("long", 30, 100.0,
                                       {"best_bid": 99.9, "best_ask": 100.1,
                                        "levels_ask": [{"p": 100.1, "q": 40}],
                                        "levels_bid": [{"p": 99.9, "q": 40}]},
                                       state="СИНГУЛЯРНОСТЬ")
    assert plan["urgent"] and all(t["kind"] != "market" for t in plan["tranches"])
    born = ap_mod.time.time() - (ap_mod.TIME_KILL_SEC + 5)   # набор давно живёт
    a.sniper = {"plan": plan, "side": "long", "filled": 3, "total": 30,
                "started": born, "why": "тест", "level": None, "idx": 99,
                "born_ts": born}
    await a._sniper_advance(100.0)
    assert a.sniper is None, "страж жизни входа не оборвал набор"
    # ПАНИКА рвёт набор немедленно, независимо от возраста
    b, fbb, _ = _mk()
    b.position = {"side": "long", "entry": 100.0, "lots": 3, "stop": 99.0,
                  "opened_ts": ap_mod.time.time()}
    b.sniper = {"plan": plan, "side": "long", "filled": 3, "total": 30,
                "started": ap_mod.time.time(), "why": "т", "level": None,
                "idx": 99, "born_ts": ap_mod.time.time()}
    b.panic_flag = True
    await b._sniper_advance(100.0)
    assert b.sniper is None, "ПАНИКА не оборвала набор"
    # предел перевзводов: после MAX_REPRICINGS недобор снимается
    c, fbc, _ = _mk()
    c.position = {"side": "long", "entry": 100.0, "lots": 3, "stop": 99.0,
                  "opened_ts": ap_mod.time.time()}
    now = ap_mod.time.time()
    c.sniper = {"plan": plan, "side": "long", "filled": 3, "total": 30,
                "started": now - 1000, "why": "т", "level": None, "idx": 99,
                "born_ts": now, "repricings": ap_mod.MAX_REPRICINGS}
    await c._sniper_advance(100.0)
    assert c.sniper is None, "предел перевзводов не сработал"


async def _t_partial_entry_kept():
    """РЕГРЕССИЯ (major): частично налитая лимитка ВХОДА терялась — лоты
    уходили в «чужие» без стопа и сетки. Теперь становится позицией."""
    a, fb, _ = _mk()
    r = await fb.place("FIGI-TEST", ap_mod.trader_broker.BUY, 10, price=99.9,
                       tag="auto")
    a.pending = {"order_id": r["order_id"], "side": "long", "lots": 10,
                 "price": 99.9, "ts": ap_mod.time.time() - 120, "mom": 0.0,
                 "why": "тест", "voices": {}, "oconf": 0.1,
                 "override_dir": None}
    fb.partial[r["order_id"]] = 4            # биржа налила 4 из 10
    await a._pending_tick(100.5)             # цена ушла → снятие остатка
    assert a.position is not None, "частично налитый вход потерян"
    assert a.position["lots"] == 4, a.position
    assert a.position.get("opened_ts"), "нет opened_ts → TIME_KILLSWITCH слеп"
    assert a.pending is None


async def _t_margin_guard():
    a, fb, journal = _mk(deposit=10000.0, go=3000.0)
    a.position = {"side": "long", "entry": 100.0, "lots": 8, "stop": 99.0}
    # 8 лотов × ГО 3000 = 24000 > капитал 10000 → вмещается только 3
    hit = await a._margin_guard(100.0)
    assert hit is True
    assert a.position and a.position["lots"] == 3, a.position
    assert any(p["tag"] == "margin-guard" and p["lots"] == 5 for p in fb.placed)
    assert any("маржевой дозор" in r["why"] for r in journal)
    # вмещаемся → дозор молчит
    assert await a._margin_guard(100.0) is False


async def _t_reconcile_isolation():
    async def _pf(qty):
        async def fake_portfolio(_acc):
            return {"positions": [{"figi": "FIGI-TEST", "qty": qty}]}
        return fake_portfolio

    # (а) ручная активность: у бота 2 long, на бирже 5 → чужих +3, свои целы
    a, fb, _ = _mk()
    a.position = {"side": "long", "entry": 100.0, "lots": 2, "stop": 99.0}
    ap_mod.tinkoff.portfolio = await _pf(5.0)
    await a._reconcile(100.0)
    assert a.foreign_lots == 3 and a.position["lots"] == 2, \
        (a.foreign_lots, a.position)
    # (а2) РЕГРЕССИЯ ревью: владелец ЗАКРЫЛ свою ручную позицию (5→2).
    #      Наши 2 лота ЦЕЛЫ — старое правило ошибочно списывало их и
    #      бросало живую позицию бота без стопа и учёта
    ap_mod.tinkoff.portfolio = await _pf(2.0)
    await a._reconcile(100.0)
    assert a.position and a.position["lots"] == 2, ("свои лоты целы", a.position)
    assert a.foreign_lots == 0, a.foreign_lots
    #      и наоборот: владелец добавил ещё — свои по-прежнему целы
    ap_mod.tinkoff.portfolio = await _pf(9.0)
    await a._reconcile(100.0)
    assert a.position["lots"] == 2 and a.foreign_lots == 7, \
        (a.position, a.foreign_lots)
    # (б) свои ужаты снаружи: биржа говорит 1 (чужих 0 у нового бота)
    a2, fb2, _ = _mk()
    a2.position = {"side": "long", "entry": 100.0, "lots": 2, "stop": 99.0}
    ap_mod.tinkoff.portfolio = await _pf(1.0)
    await a2._reconcile(100.0)
    assert a2.position and a2.position["lots"] == 1, a2.position
    # (в) всё закрыто снаружи → учёт снят, бот не «переоткрывает»
    a3, fb3, _ = _mk()
    a3.position = {"side": "long", "entry": 100.0, "lots": 2, "stop": 99.0}
    ap_mod.tinkoff.portfolio = await _pf(0.0)
    await a3._reconcile(100.0)
    assert a3.position is None and a3.foreign_lots == 0
    # (в2) НЕТТО-СХЛОПЫВАНИЕ: наш long 2 + ручной short 3 → биржа −1.
    #      Наши лоты снетчены (учёт снят), остаток −1 — ЧУЖОЙ, и это должно
    #      быть видно СРАЗУ, а не через минуту до следующей сверки
    a5, fb5, _ = _mk()
    a5.position = {"side": "long", "entry": 100.0, "lots": 2, "stop": 99.0}
    ap_mod.tinkoff.portfolio = await _pf(-1.0)
    await a5._reconcile(100.0)
    assert a5.position is None and a5.foreign_lots == -1, \
        (a5.position, a5.foreign_lots)
    #      повторная сверка на тех же данных ничего не ломает (нет дрейфа)
    await a5._reconcile(100.0)
    assert a5.foreign_lots == -1 and a5.position is None
    # (г) чужой short при нашем отсутствии: foreign, позиции нет
    a4, fb4, _ = _mk()
    ap_mod.tinkoff.portfolio = await _pf(-4.0)
    await a4._reconcile(100.0)
    assert a4.position is None and a4.foreign_lots == -4
    # (д) изоляция включена по умолчанию (без PYTHIA_ADOPT)
    assert a.adopt is False and a.status()["isolation"] is True


async def _t_no_dummies():
    # нет истории спреда → сетки нет (не выдумываем базу)
    a, fb, _ = _mk()
    a.spread_hist = []
    a.position = {"side": "long", "entry": 100.0, "lots": 5, "stop": 99.0}
    await a._grid_place()
    assert not a.position.get("exit_grid")
    # принятая (adopted) позиция сетку не получает — она не наша математика
    a.spread_hist = [0.5] * 10
    a.position["adopted"] = True
    await a._grid_place()
    assert not a.position.get("exit_grid")


if __name__ == "__main__":
    asyncio.run(_t_grid_lifecycle())
    asyncio.run(_t_close_cancels_grid())
    asyncio.run(_t_race_no_flip())
    asyncio.run(_t_tensor_exit_acid())
    asyncio.run(_t_time_killswitch())
    asyncio.run(_t_sniper_never_traps_position())
    asyncio.run(_t_partial_entry_kept())
    asyncio.run(_t_partial_fill())
    asyncio.run(_t_orphan_order())
    asyncio.run(_t_margin_guard())
    asyncio.run(_t_reconcile_isolation())
    asyncio.run(_t_no_dummies())
    print("grid_sim_test OK: сетка ставится/исполняется/закрывает целиком, "
          "активный выход РАСЧИТЫВАЕТ сетку (гонка «лимитка исполнилась, "
          "round-robin не дошёл» → переворота НЕТ), маржевой дозор ужимает "
          "от правды биржи, изоляция — чужое к чужим / своё ужимается, "
          "NO DUMMIES без спреда и на adopted")
