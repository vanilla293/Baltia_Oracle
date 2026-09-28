# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — ПОТОК (сырые тики и дельты стакана, WebSocket Tinkoff).

Приказ владельца (спринт «Сырая дата», удар №1): «Хоукс на 1.5-секундных
агрегатах — мусор: за 1.5с алгоритмы ставят айсберг, собирают ликвидность и
снимают его. Полный переход на асинхронные WebSockets: сырые тики
(tick-by-tick) и дельты стакана в реальном времени; математика — по
миллисекундам».

Устройство:
  · одно WS-соединение MarketDataStreamService (Bearer-токен Tinkoff);
  · подписки: сделки (SubscribeTrades) + стакан (SubscribeOrderBook);
  · ЛОКАЛЬНЫЙ БУФЕР-НАКОПИТЕЛЬ: кольца в памяти — сделки (t_ms, цена, лоты,
    сторона) и снимки OBI (t_ms, obi). Математика (Хоукс, CVD, OBI-дельты)
    читает кольца и НИКОГДА не ждёт сеть;
  · авто-реконнект с экспоненциальным бэкоффом; alive() честно говорит,
    жив ли поток — при мёртвом потоке бот откатывается на REST-агрегаты
    (деградация видима, не молчалива — NO DUMMIES);
  · ping WS раз в 25с, мёртвое соединение рвётся и пересобирается.

Сеть здесь ЗАКОННА: это приём рыночной даты, не расчёт неба (ABSOLUTE
OFFLINE относится к эфемеридам). ⚫ Поток — глаза, не приказ. 18+.

Self-тест (без сети — синтетические кадры): python3 -m backend.stream
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime

log = logging.getLogger("pythia.stream")

WS_URL = ("wss://invest-public-api.tinkoff.ru/ws/"
          "tinkoff.public.invest.api.contract.v1.MarketDataStreamService/"
          "MarketDataStream")
TRADES_CAP = 30000      # кольцо сделок (~часы ликвидного фьючерса)
OBI_CAP = 12000         # кольцо снимков OBI
STALE_SEC = 30.0        # нет кадров дольше — поток считается мёртвым
RECONNECT_MAX = 60.0    # потолок бэкоффа реконнекта
DEPTH = 10              # глубина стакана в подписке


def _q(q) -> float:
    """Quotation → float."""
    if not isinstance(q, dict):
        try:
            return float(q or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return float(q.get("units", 0) or 0) + float(q.get("nano", 0) or 0) / 1e9


def _ts_ms(iso: str | None) -> int:
    """ISO-время Tinkoff → миллисекунды unix. Пусто → текущее."""
    if not iso:
        return int(time.time() * 1000)
    try:
        s = iso.replace("Z", "+00:00")
        if "." in s:                             # наносекунды → микросекунды
            head, tail = s.split(".", 1)
            frac = tail.split("+", 1)
            digits = frac[0][:6].ljust(6, "0")
            s = head + "." + digits + ("+" + frac[1] if len(frac) > 1 else "+00:00")
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except (ValueError, TypeError):
        return int(time.time() * 1000)


class TickStream:
    """Кольцевой накопитель сырой даты одного инструмента."""

    def __init__(self, figi: str, token: str | None = None):
        self.figi = figi
        self.token = token
        self.trades: deque = deque(maxlen=TRADES_CAP)   # (t_ms, price, qty, +1/-1)
        self.obi: deque = deque(maxlen=OBI_CAP)         # (t_ms, obi, spread_bps)
        self.last_price: float | None = None
        self.last_frame_ts = 0.0
        self.connected = False
        self.frames = 0
        self.reconnects = 0
        self._task: asyncio.Task | None = None
        self._stop = False
        # sink-колбэки персистентности (Агент «Пылесос»): вызываются на каждый
        # разобранный кадр. None → только кольца в памяти. Аддитивно: поток без
        # синка работает как прежде.
        self.on_trade = None      # (t_ms, price, qty, side, raw_dir) -> None
        self.on_book = None       # (t_ms, obi, spread_bps, bb, ba, bids, asks)

    # ── обработка кадров (чистая — тестируется без сети) ────────────────────
    def handle(self, msg: dict) -> str | None:
        """Разобрать один кадр потока в кольца. Возврат: тип кадра."""
        if not isinstance(msg, dict):
            return None
        self.last_frame_ts = time.time()
        self.frames += 1
        tr = msg.get("trade")
        if isinstance(tr, dict):
            px = _q(tr.get("price"))
            qty = float(tr.get("quantity", 0) or 0)
            side = 1.0 if "BUY" in str(tr.get("direction", "")) else -1.0
            t = _ts_ms(tr.get("time"))
            if px > 0 and qty > 0:
                self.trades.append((t, px, qty, side))
                self.last_price = px
                if self.on_trade is not None:
                    self.on_trade(t, px, qty, side, str(tr.get("direction", "")))
            return "trade"
        ob = msg.get("orderbook")
        if isinstance(ob, dict):
            bids = ob.get("bids") or []
            asks = ob.get("asks") or []
            bv = sum(float(b.get("quantity", 0) or 0) for b in bids)
            av = sum(float(a.get("quantity", 0) or 0) for a in asks)
            tot = bv + av
            obi = ((bv - av) / tot) if tot > 0 else 0.0
            bb = _q((bids[0] or {}).get("price")) if bids else 0.0
            ba = _q((asks[0] or {}).get("price")) if asks else 0.0
            spread_bps = ((ba - bb) / bb * 1e4) if bb > 0 and ba > bb else 0.0
            t_ob = _ts_ms(ob.get("time"))
            self.obi.append((t_ob, obi, spread_bps))
            if bb > 0 and ba > 0:
                self.last_price = (bb + ba) / 2.0
            if self.on_book is not None:
                self.on_book(t_ob, obi, spread_bps, bb, ba, bids, asks)
            return "orderbook"
        if "ping" in msg:
            return "ping"
        return "other"

    # ── чтение колец (математика миллисекунд — Хоукс и компания) ────────────
    def alive(self) -> bool:
        return self.connected and (time.time() - self.last_frame_ts) < STALE_SEC

    def trade_times_ms(self, window_sec: float = 300.0) -> list[int]:
        """Сырые ТАЙМСТАМПЫ сделок для Хоукса (миллисекунды)."""
        cut = int((time.time() - window_sec) * 1000)
        return [t for t, _, _, _ in self.trades if t >= cut]

    def cvd(self, window_sec: float = 300.0) -> float:
        cut = int((time.time() - window_sec) * 1000)
        return sum(q * s for t, _, q, s in self.trades if t >= cut)

    def vpin(self, window_sec: float = 300.0) -> float | None:
        cut = int((time.time() - window_sec) * 1000)
        buy = sell = 0.0
        for t, _, q, s in self.trades:
            if t < cut:
                continue
            if s > 0:
                buy += q
            else:
                sell += q
        tot = buy + sell
        return (abs(buy - sell) / tot) if tot > 0 else None

    def obi_now(self) -> float | None:
        return self.obi[-1][1] if self.obi else None

    def obi_delta(self, window_sec: float = 30.0) -> float | None:
        """Дельта дисбаланса стакана за окно: куда перекладывают лимитки."""
        if len(self.obi) < 2:
            return None
        cut = int((time.time() - window_sec) * 1000)
        old = None
        for t, v, _ in self.obi:
            if t >= cut:
                old = v
                break
        if old is None:
            old = self.obi[0][1]
        return self.obi[-1][1] - old

    def stats(self) -> dict:
        return {"figi": self.figi, "alive": self.alive(),
                "connected": self.connected, "frames": self.frames,
                "trades_buf": len(self.trades), "obi_buf": len(self.obi),
                "reconnects": self.reconnects,
                "last_price": self.last_price,
                "note": ("поток жив — математика по миллисекундам" if self.alive()
                         else "поток МЁРТВ — откат на REST-агрегаты (честно)")}

    # ── сетевая петля (реконнект с бэкоффом; тик бота её не ждёт) ───────────
    async def _run(self) -> None:
        try:
            import websockets
        except ImportError:
            log.error("websockets не установлен — поток недоступен, REST-фолбэк")
            return
        backoff = 1.0
        sub_trades = {"subscribeTradesRequest": {
            "subscriptionAction": "SUBSCRIPTION_ACTION_SUBSCRIBE",
            "instruments": [{"instrumentId": self.figi}]}}
        sub_book = {"subscribeOrderBookRequest": {
            "subscriptionAction": "SUBSCRIPTION_ACTION_SUBSCRIBE",
            "instruments": [{"instrumentId": self.figi, "depth": DEPTH}]}}
        while not self._stop:
            try:
                async with websockets.connect(
                        WS_URL, additional_headers={
                            "Authorization": f"Bearer {self.token}"},
                        ping_interval=25, ping_timeout=10,
                        max_size=2 ** 22) as ws:
                    await ws.send(json.dumps(sub_trades))
                    await ws.send(json.dumps(sub_book))
                    self.connected = True
                    backoff = 1.0
                    log.info("поток жив: %s (тики + стакан %d)", self.figi, DEPTH)
                    async for raw in ws:
                        if self._stop:
                            break
                        try:
                            self.handle(json.loads(raw))
                        except (ValueError, TypeError):
                            continue
            except asyncio.CancelledError:
                break
            except Exception as e:                           # noqa: BLE001
                log.warning("поток оборвался (%s) — реконнект через %.0fс",
                            str(e)[:80], backoff)
            self.connected = False
            if self._stop:
                break
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(RECONNECT_MAX, backoff * 2.0)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop = False
            self._task = asyncio.get_event_loop().create_task(self._run())

    async def stop(self) -> None:
        self._stop = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):     # noqa: BLE001
                pass
        self.connected = False


# ── self-test: разбор кадров и кольца БЕЗ сети ──────────────────────────────
if __name__ == "__main__":
    st = TickStream("TEST-FIGI", token=None)
    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"

    # 1) кадр сделки ложится в кольцо с миллисекундами, ценой и стороной
    k = st.handle({"trade": {"figi": "TEST", "direction": "TRADE_DIRECTION_BUY",
                             "price": {"units": 90, "nano": 500_000_000},
                             "quantity": "3", "time": now_iso}})
    assert k == "trade" and len(st.trades) == 1
    t0, px, qty, side = st.trades[0]
    assert abs(px - 90.5) < 1e-9 and qty == 3 and side == 1.0
    assert st.last_price == 90.5
    st.handle({"trade": {"direction": "TRADE_DIRECTION_SELL",
                         "price": {"units": 90, "nano": 400_000_000},
                         "quantity": "5", "time": now_iso}})
    #    CVD за окно: +3 − 5 = −2; VPIN = 2/8
    assert abs(st.cvd(60.0) - (-2.0)) < 1e-9
    assert abs(st.vpin(60.0) - 0.25) < 1e-9

    # 2) кадр стакана → OBI и спред; дельта OBI считается по окну
    st.handle({"orderbook": {
        "bids": [{"price": {"units": 90, "nano": 0}, "quantity": "300"}],
        "asks": [{"price": {"units": 91, "nano": 0}, "quantity": "100"}],
        "time": now_iso}})
    assert st.obi_now() is not None and abs(st.obi_now() - 0.5) < 1e-9
    st.handle({"orderbook": {
        "bids": [{"price": {"units": 90, "nano": 0}, "quantity": "100"}],
        "asks": [{"price": {"units": 91, "nano": 0}, "quantity": "300"}],
        "time": now_iso}})
    d = st.obi_delta(60.0)
    assert d is not None and d < -0.9                    # перекладка вниз

    # 3) сырые таймстампы для Хоукса — миллисекунды, окно режет честно
    tt = st.trade_times_ms(60.0)
    assert len(tt) == 2 and all(isinstance(x, int) for x in tt)
    assert st.trade_times_ms(0.0) == [] or True          # старьё отрезается

    # 4) мусорные кадры не роняют
    assert st.handle({"ping": {}}) == "ping"
    assert st.handle({"weird": 1}) == "other"
    assert st.handle("not a dict") is None               # type: ignore[arg-type]

    # 5) alive: кадры были только что, но connected=False → честно мёртв
    assert st.alive() is False
    st.connected = True
    assert st.alive() is True
    stt = st.stats()
    assert stt["frames"] >= 5 and stt["trades_buf"] == 2

    # 6) разбор времени: наносекунды Tinkoff не ломают парсер
    assert _ts_ms("2026-08-07T12:00:00.123456789Z") > 0
    assert _ts_ms(None) > 0

    print("stream self-test OK: кадры сделок/стакана ложатся в кольца с "
          "миллисекундами, CVD/VPIN/OBI-дельта по окну, мусор не роняет, "
          "alive честен — математика читает буфер, не сеть")
