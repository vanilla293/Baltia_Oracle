# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — БРОКЕР-ИСПОЛНИТЕЛЬ (руки Фармилы).

Money-touching спина всего торгового бота. Всё, что реально двигает деньги,
проходит ТОЛЬКО здесь — один узкий, проверяемый, залоггированный шлюз.

ТРИ РЕЖИМА (MODE), безопасность нарастает СНИЗУ ВВЕРХ:
  · "dry"     — СУХОЙ ПРОГОН (по умолчанию): ордер НЕ уходит в сеть, только
                считается и пишется в аудит-лог. Так стартует бот ВСЕГДА при
                первом запуске — баг на первой минуте не тронет счёт.
  · "sandbox" — ПЕСОЧНИЦА Tinkoff: виртуальные деньги, реальный рынок и стакан.
                Обкатка Хищника без риска для депозита.
  · "real"    — БОЕВОЙ счёт, реальные деньги. Вооружается ТОЛЬКО явно:
                env TRADER_ARM_REAL=1 И переданный arm_token (двойной рубильник).
                Иначе брокер честно откатывается в "dry" и пишет причину.

Инвариант: real НЕ включится молча ни при каком стечении обстоятельств —
нет двойного подтверждения → dry. NO DUMMIES: нет токена/счёта → честный отказ.
Каждый интент и результат ордера пишется в audit jsonl (кто/что/когда/цена/итог).
Сеть тут ЗАКОННА — это исполнение на бирже, не расчёт неба.
"""
from __future__ import annotations

import json
import math
import os
import time
import uuid
from pathlib import Path

import httpx

from . import config, tinkoff

SANDBOX = "tinkoff.public.invest.api.contract.v1.SandboxService"
ORDERS = "tinkoff.public.invest.api.contract.v1.OrdersService"
OPER = "tinkoff.public.invest.api.contract.v1.OperationsService"
STOPS = "tinkoff.public.invest.api.contract.v1.StopOrdersService"

MODES = ("dry", "sandbox", "real")
_AUDIT_PATH = config.DATA_DIR / "trader_audit.jsonl"
ORDER_REQUEST_MAX_AGE_SEC = 90.0   # заявка по request-UUID «не найдена» биржей дольше — до биржи не дошла (фантом):
                                   # пилот/паника считают её терминальной с 0 исполнений, а не ждут вечно

BUY, SELL = "ORDER_DIRECTION_BUY", "ORDER_DIRECTION_SELL"
MARKET, LIMIT = "ORDER_TYPE_MARKET", "ORDER_TYPE_LIMIT"
STOP_BUY = "STOP_ORDER_DIRECTION_BUY"
STOP_SELL = "STOP_ORDER_DIRECTION_SELL"


def _now() -> float:
    return time.time()


def _q_to_float(q) -> float:
    """Quotation/MoneyValue → float (units + nano/1e9)."""
    if not isinstance(q, dict):
        return 0.0
    return float(q.get("units", 0) or 0) + float(q.get("nano", 0) or 0) / 1e9


def _to_quotation(x: float) -> dict:
    """float → Quotation {units, nano}."""
    total = int(round(x * 1e9))
    units = abs(total) // 1_000_000_000 * (-1 if total < 0 else 1)
    nano = total - units * 1_000_000_000
    return {"units": units, "nano": nano}


def _order_numbers(lots, price=None) -> tuple[int, float | None] | None:
    """Reject malformed quantities/prices before audit, dry fill, or a broker call."""
    try:
        if isinstance(lots, bool) or isinstance(price, bool):
            return None
        count = int(lots)
        if count <= 0 or count >= 2 ** 63 or count != float(lots):
            return None
        px = None if price is None else float(price)
        if px is not None and (not math.isfinite(px) or px <= 0 or px >= 2 ** 63):
            return None
        return count, px
    except (TypeError, ValueError, OverflowError):
        return None


def _not_sent(e: Exception) -> bool:
    """Соединение не установлено (ConnectError/ConnectTimeout, DNS) — запрос до брокера не дошёл: определённый отказ,
    а не «ответ потерян». ReadTimeout/ReadError/5xx — по-прежнему неопределённость (запрос мог дойти)."""
    return isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout))


def _order_not_found(e: Exception) -> bool:
    """Биржа такой заявки не знает (по request-UUID): gRPC NOT_FOUND → HTTP 404, либо 400 с кодом/текстом «не найдена».
    Намеренно НЕ любой 4xx: 401/403 (токен), 429 (лимит), 408/409 — не про заявку, их нельзя принять за «нет заявки»."""
    if not isinstance(e, tinkoff.TinkoffError) or e.status not in (400, 404):
        return False
    if e.status == 404:
        return True
    low = f"{e.code} {e.message} {e.description}".lower()
    return "not found" in low or "не найден" in low or str(e.message or "").strip() in ("50005", "50007")


def _err_text(e: Exception) -> str:
    """Текст ошибки для панели: у Tinkoff — код и описание, не «400 Bad Request»."""
    try:
        fn = getattr(tinkoff, "humanize_api_error", None)
        return (fn(e) if fn else str(e))[:200]
    except Exception:                                       # noqa: BLE001
        return str(e)[:200]


def _audit(kind: str, payload: dict) -> None:
    """Каждое движение денег — в несбиваемый журнал (append-only)."""
    rec = {"ts": _now(), "kind": kind, **payload}
    try:
        with open(_AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:                                       # noqa: BLE001
        pass                                                # журнал не должен ронять торговлю


class Broker:
    """Единый шлюз ордеров. Инъекция _post позволяет тестировать без сети."""

    def __init__(self, mode: str = "dry", account_id: str | None = None,
                 arm_token: str | None = None, poster=None):
        self.requested_mode = mode if mode in MODES else "dry"
        self.mode = self._resolve_mode(self.requested_mode, arm_token)
        self.account_id = account_id
        self._post = poster or tinkoff._post          # инъекция для тестов
        self.blocked_reason: str | None = None
        _audit("broker_init", {"requested": self.requested_mode,
                               "effective": self.mode,
                               "account": account_id})

    # ── боевой режим ───────────────────────────────────────────────────────
    @staticmethod
    def _resolve_mode(mode: str, arm_token: str | None) -> str:
        """Боевой real разрешён, когда есть токен Тинькофф (право торговли).
        По прямому требованию владельца доп-рубильники в конфигах убраны: ввёл
        ключ — сразу боевой. Отключить боевой можно env PYTHIA_DRY=1 (страховка
        для теста). Нет токена → dry (торговать нечем)."""
        if mode != "real":
            return mode
        if os.getenv("PYTHIA_DRY", "") == "1":
            _audit("real_refused", {"note": "PYTHIA_DRY=1 — принудительный dry"})
            return "dry"
        if not tinkoff.enabled():
            _audit("real_refused", {"note": "нет токена Тинькофф — откат в dry"})
            return "dry"
        return "real"

    def is_live(self) -> bool:
        """Ордера реально уходят в сеть? (sandbox или real, не dry)."""
        return self.mode in ("sandbox", "real")

    def _svc(self) -> str:
        return SANDBOX if self.mode == "sandbox" else ORDERS

    # ── счёт ──────────────────────────────────────────────────────────────
    async def ensure_account(self, pay_in: float = 100000.0) -> dict:
        """Песочница: открыть счёт + пополнить виртуалом. dry/real: только чтение."""
        if self.mode == "dry":
            self.account_id = self.account_id or "DRY-ACCOUNT"
            return {"ok": True, "account": self.account_id, "mode": "dry"}
        if not tinkoff.enabled():
            self.blocked_reason = "нет TINKOFF_TOKEN"
            return {"ok": False, "note": self.blocked_reason}
        try:
            if self.mode == "sandbox":
                accs = await self._post(f"{SANDBOX}/GetSandboxAccounts", {})
                lst = accs.get("accounts") or []
                if lst:
                    self.account_id = lst[0]["id"]
                else:
                    opened = await self._post(f"{SANDBOX}/OpenSandboxAccount", {})
                    self.account_id = opened["accountId"]
                    await self._post(f"{SANDBOX}/SandboxPayIn",
                                     {"accountId": self.account_id,
                                      "amount": {"currency": "rub",
                                                 "units": int(pay_in), "nano": 0}})
                return {"ok": True, "account": self.account_id, "mode": "sandbox"}
            # real: счёт задаёт владелец, сами не открываем
            if not self.account_id:
                self.blocked_reason = "боевой accountId не задан"
                return {"ok": False, "note": self.blocked_reason}
            return {"ok": True, "account": self.account_id, "mode": "real"}
        except Exception as e:                              # noqa: BLE001
            self.blocked_reason = str(e)[:160]
            _audit("account_error", {"err": self.blocked_reason})
            return {"ok": False, "note": self.blocked_reason}

    async def portfolio(self) -> dict:
        """Свободные деньги, позиции, ГО. dry → пустой честный портфель."""
        if self.mode == "dry":
            return {"cash": None, "positions": [], "mode": "dry",
                    "note": "сухой прогон — реального портфеля нет"}
        try:
            if self.mode == "sandbox":
                p = await self._post(f"{SANDBOX}/GetSandboxPortfolio",
                                     {"accountId": self.account_id})
            else:
                p = await self._post(f"{OPER}/GetPortfolio",
                                     {"accountId": self.account_id})
            cash = _q_to_float(p.get("totalAmountCurrencies"))
            poss = [{"figi": x.get("figi"), "uid": x.get("instrumentUid"),
                     "type": x.get("instrumentType"),
                     "qty": _q_to_float(x.get("quantity")),
                     "avg": _q_to_float(x.get("averagePositionPrice")),
                     "cur_price": _q_to_float(x.get("currentPrice")),
                     "yield": _q_to_float(x.get("expectedYield"))}
                    for x in (p.get("positions") or [])]
            return {"cash": cash, "total": _q_to_float(p.get("totalAmountPortfolio")) or None,
                    "positions": poss, "mode": self.mode}
        except Exception as e:                              # noqa: BLE001
            return {"cash": None, "positions": [], "error": _err_text(e)}

    # ── СКОЛЬКО ДАЁТ БИРЖА (полное управление счётом, v5.2) ────────────────
    async def max_lots(self, figi: str, price: float | None = None) -> dict | None:
        """Максимум лотов на покупку/продажу прямо сейчас — считает БРОКЕР
        (OrdersService/GetMaxLots) с учётом денег, открытых позиций и маржинального
        плеча. Только боевой счёт: в dry/sandbox честно None (сайзер считает сам)."""
        if self.mode != "real" or not self.account_id or not figi:
            return None
        try:
            return await tinkoff.max_lots(self.account_id, figi, price)
        except Exception as e:                              # noqa: BLE001
            _audit("max_lots_error", {"err": _err_text(e)})
            return None

    async def margin(self) -> dict | None:
        """Маржинальные атрибуты счёта (ликвидный портфель, достаточность средств,
        недостающие средства) — для дозора и панели. Только боевой счёт."""
        if self.mode != "real" or not self.account_id:
            return None
        try:
            return await tinkoff.margin_attributes(self.account_id)
        except Exception as e:                              # noqa: BLE001
            _audit("margin_error", {"err": _err_text(e)})
            return None

    # ── ордер ─────────────────────────────────────────────────────────────
    async def place(self, figi: str, direction: str, lots: int,
                    price: float | None = None, tag: str = "", *,
                    request_id: str | None = None) -> dict:
        """Выставить ордер. lots — ЦЕЛОЕ число лотов (>0). price=None → рыночный.
        dry → только считаем и логируем, в сеть не уходим (NO DUMMIES)."""
        assert direction in (BUY, SELL), "direction"
        numbers = _order_numbers(lots, price)
        if numbers is None:
            return {"ok": False, "note": "некорректные лоты/цена — ордер не выставлен"}
        lots, price = numbers
        otype = MARKET if price is None else LIMIT
        order_id = request_id or str(uuid.uuid4())
        intent = {"figi": figi, "direction": direction, "lots": lots,
                  "price": price, "type": otype, "tag": tag,
                  "mode": self.mode, "order_id": order_id}
        _audit("order_intent", intent)
        if self.mode == "dry":
            return {"ok": True, "dry": True, "order_id": order_id, **intent,
                    "note": "сухой прогон — ордер посчитан, в сеть НЕ ушёл"}
        if not self.account_id:
            return {"ok": False, "note": "нет accountId — ордер не выставлен"}
        body = {"accountId": self.account_id, "instrumentId": figi,
                "quantity": lots, "direction": direction,
                "orderType": otype, "orderId": order_id}
        if price is not None:
            body["price"] = _to_quotation(price)
        try:
            r = await self._post(f"{self._svc()}/Post{'Sandbox' if self.mode=='sandbox' else ''}Order",
                                 body)
            status = str(r.get("executionReportStatus") or "")
            res = {**intent, "ok": not status.endswith("REJECTED"),
                   "order_id": r.get("orderId") or order_id,
                   "request_id": order_id, "id_type": "exchange" if r.get("orderId") else "request",
                   "status": r.get("executionReportStatus"),
                   "exec_lots": r.get("lotsExecuted")}
            _audit("order_done", res)
            return res
        except Exception as e:                              # noqa: BLE001
            # A lost response is not a rejected order. The caller must query the
            # same request UUID before it can decide whether a retry is safe.
            # W4 (№2): соединение не установлено → запрос не отправлен → определённый отказ, не фантом
            uncertain = (not isinstance(e, tinkoff.TinkoffError) or
                         e.status >= 500 or e.status in (408, 409)) and not _not_sent(e)
            res = {**intent, "ok": False, "error": _err_text(e),
                   "request_id": order_id, "id_type": "request", "uncertain": uncertain}
            _audit("order_error", res)
            return res

    async def order_state(self, order_id: str, *, request_id: bool = False) -> dict:
        """Судьба ордера: реально ли исполнен. Реальный трейдер НЕ считает
        лимитку исполненной по факту выставления. dry → исполнен сразу.
        W4 (№2): по request-UUID биржа отвечает «не найдена» → {"ok": True, "status": "NOT_FOUND", "not_found": True}:
        «биржа такой заявки не знает» отличимо от «сервис недоступен» (ok False) — решает вызывающий по возрасту."""
        if self.mode == "dry":
            return {"ok": True, "filled": True, "dry": True}
        if not self.account_id:
            return {"ok": False, "note": "нет accountId"}
        try:
            body = {"accountId": self.account_id, "orderId": order_id}
            if request_id:
                body["orderIdType"] = "ORDER_ID_TYPE_REQUEST"
            r = await self._post(
                f"{self._svc()}/Get{'Sandbox' if self.mode == 'sandbox' else ''}OrderState",
                body)
            st = str(r.get("executionReportStatus", ""))
            return {"ok": True, "status": st,
                    "order_id": r.get("orderId"),
                    "filled": st.endswith("_FILL") or st == "FILL",
                    "exec_lots": r.get("lotsExecuted")}
        except Exception as e:                              # noqa: BLE001
            if request_id and _order_not_found(e):
                return {"ok": True, "status": "NOT_FOUND", "not_found": True, "filled": False, "exec_lots": 0,
                        "error": _err_text(e)}
            return {"ok": False, "error": _err_text(e)}

    async def cancel(self, order_id: str, *, request_id: bool = False) -> dict:
        """Снять невыполненный лимит (цена ушла / таймаут)."""
        _audit("order_cancel", {"order_id": order_id, "mode": self.mode})
        if self.mode == "dry":
            return {"ok": True, "dry": True}
        if not self.account_id:
            return {"ok": False, "note": "нет accountId"}
        try:
            body = {"accountId": self.account_id, "orderId": order_id}
            if request_id:
                body["orderIdType"] = "ORDER_ID_TYPE_REQUEST"
            await self._post(
                f"{self._svc()}/Cancel{'Sandbox' if self.mode == 'sandbox' else ''}Order",
                body)
            return {"ok": True}
        except Exception as e:                              # noqa: BLE001
            if request_id and _order_not_found(e):
                return {"ok": True, "status": "NOT_FOUND", "not_found": True, "error": _err_text(e)}
            return {"ok": False, "error": _err_text(e)}

    # ── АППАРАТНЫЙ СТОП НА СЕРВЕРЕ БИРЖИ (спринт «Хищник», прокол №4) ──────
    async def place_stop(self, figi: str, direction: str, lots: int,
                         stop_price: float, tag: str = "hw-stop", *, request_id: str | None = None) -> dict:
        """Stop-market НА СЕРВЕРЕ БИРЖИ: триггер лежит у биржи ДО импульса,
        срабатывает даже если бот мёртв, пинг упал, контейнер убит.

        direction — направление ЗАКРЫВАЮЩЕЙ заявки (long закрывает SELL).
        Только real: StopOrdersService не существует в песочнице Tinkoff —
        dry/sandbox честно отвечают virtual=True (стоп остаётся у бота)."""
        assert direction in (BUY, SELL), "direction"
        numbers = _order_numbers(lots, stop_price)
        if numbers is None or numbers[1] is None:
            return {"ok": False, "note": "некорректные лоты/цена стопа"}
        lots, stop_price = numbers
        intent = {"figi": figi, "direction": direction, "lots": lots,
                  "stop_price": stop_price, "mode": self.mode, "tag": tag,
                  "request_id": request_id or str(uuid.uuid4())}
        _audit("stop_intent", intent)
        if self.mode != "real":
            return {"ok": True, "virtual": True, **intent,
                    "note": ("режим " + self.mode + ": биржевых стопов нет — "
                             "стоп виртуальный, у бота (честно)")}
        if not self.account_id:
            return {"ok": False, "note": "нет accountId — стоп не выставлен"}
        body = {"accountId": self.account_id, "instrumentId": figi,
                "orderId": intent["request_id"],
                "quantity": lots,
                "direction": STOP_BUY if direction == BUY else STOP_SELL,
                "stopPrice": _to_quotation(stop_price),
                "stopOrderType": "STOP_ORDER_TYPE_STOP_LOSS",
                "expirationType": "STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL",
                "exchangeOrderType": "EXCHANGE_ORDER_TYPE_MARKET"}
        try:
            r = await self._post(f"{STOPS}/PostStopOrder", body)
            res = {**intent, "ok": bool(r.get("stopOrderId")), "stop_order_id": r.get("stopOrderId"),
                   "uncertain": not bool(r.get("stopOrderId"))}
            _audit("stop_done", res)
            return res
        except Exception as e:                              # noqa: BLE001
            res = {**intent, "ok": False, "error": _err_text(e),
                   "uncertain": (not isinstance(e, tinkoff.TinkoffError) or
                                 e.status >= 500 or e.status in (408, 409)) and not _not_sent(e)}
            _audit("stop_error", res)
            return res

    async def cancel_stop(self, stop_order_id: str) -> dict:
        """Снять биржевой стоп (перенос уровня / закрытие позиции)."""
        _audit("stop_cancel", {"stop_order_id": stop_order_id, "mode": self.mode})
        if self.mode != "real":
            return {"ok": True, "virtual": True}
        if not self.account_id or not stop_order_id:
            return {"ok": False, "note": "нет accountId/stop_order_id"}
        try:
            await self._post(f"{STOPS}/CancelStopOrder",
                             {"accountId": self.account_id,
                              "stopOrderId": stop_order_id})
            return {"ok": True}
        except Exception as e:                              # noqa: BLE001
            return {"ok": False, "error": _err_text(e)}

    async def stop_orders(self, *, strict: bool = False) -> list:
        """Живые биржевые стопы счёта (сверка/чистка сирот). strict=True — ошибка запроса не глотается
        (вызывающий обязан знать, что список НЕ получен, а не «стопов нет»)."""
        if self.mode != "real" or not self.account_id:
            return []
        try:
            r = await self._post(f"{STOPS}/GetStopOrders",
                                 {"accountId": self.account_id})
            return r.get("stopOrders") or []
        except Exception:                                   # noqa: BLE001
            if strict:
                raise
            return []

    async def orders(self, *, strict: bool = False) -> list:
        """Активные (неисполненные) заявки счёта — OrdersService/GetOrders (песочница: GetSandboxOrders).
        Нужны панике: неизвестная заявка входа (без state-файла) снимается ДО закрытия позиции."""
        if self.mode == "dry" or not self.account_id:
            return []
        try:
            r = await self._post(f"{self._svc()}/Get{'Sandbox' if self.mode == 'sandbox' else ''}Orders",
                                 {"accountId": self.account_id})
            return r.get("orders") or []
        except Exception:                                   # noqa: BLE001
            if strict:
                raise
            return []

    async def flat_all(self, positions: list | None = None, figi: str | None = None,
                       lot: int = 1, *, on_intent=None, on_check=None, force: bool = False) -> dict:
        """ПАНИКА: закрыть позиции по рынку (все или только figi) и снять их биржевые
        стопы, чтобы сирота-стоп не открыл позицию заново. Killswitch зовёт это.
        lot — размер лота акции (портфель отдаёт штуки, заявка — лоты).
        Порядок (W4, п. 5): персист-гейт on_check ДО любого I/O (иначе «стопы сняты, закрытия нет») →
        список стопов (strict) → снятие активных ЗАЯВОК по figi (GetOrders → CancelOrder: неизвестная заявка входа
        без state-файла обезврежена) → снятие стопов → рыночное закрытие (on_intent — намерение до отправки).
        force=True — воля владельца: закрывать, даже если список стопов недоступен / стоп не снялся / намерение
        не сохранилось (сняв, что удалось; всё непроверенное названо в note)."""
        if self.mode == "dry":
            _audit("flat_all", {"mode": "dry", "figi": figi})
            return {"ok": True, "dry": True, "closed": 0}
        errors, warnings = [], []
        if on_check is not None:
            try:
                on_check()                                  # persist-success gate before any exchange I/O
            except Exception as e:                          # noqa: BLE001
                if not force:
                    return {"ok": False, "closed": 0, "pending": [],
                            "note": f"состояние аварийного закрытия не сохраняется ({str(e)[:120]}) — ничего не тронуто; "
                                    "повторная ПАНИКА с force закроет без сохранения"}
                warnings.append(f"состояние не сохраняется ({str(e)[:80]}) — закрываю по force без персиста")
        port = {"positions": positions} if positions is not None else await self.portfolio()
        if port.get("error"):
            return {"ok": False, "closed": 0, "pending": [], "note": port["error"]}
        closed, pending = 0, []
        n_stops, n_orders, blocked = 0, 0, set()
        # A protective stop and a market close must not both sell the position.
        # Confirm stop cancellation before submitting the closing order.
        stops_unknown = None
        try:
            stops = await self.stop_orders(strict=True)
        except Exception as e:                           # noqa: BLE001
            stops_unknown = _err_text(e)
            if not force:
                _audit("flat_all_refused", {"mode": self.mode, "figi": figi, "note": stops_unknown})
                return {"ok": False, "closed": 0, "pending": [], "stops_unknown": stops_unknown,
                        "note": f"не удалось проверить защитные стопы: {stops_unknown} — закрытие не отправлено; "
                                "повторная ПАНИКА с force закроет всё равно (сняв, что удалось)"}
            stops = []
            warnings.append(f"список стопов недоступен ({stops_unknown}) — закрываю по force, стопы не сверены: проверь их у брокера")
        # активные заявки по инструменту — снять ДО закрытия (заявка входа исполнилась бы уже после «всё закрыто»)
        try:
            orders = await self.orders(strict=True)
        except Exception as e:                           # noqa: BLE001
            orders = []
            warnings.append(f"список заявок недоступен ({_err_text(e)}) — активные заявки не сняты")
        touched = False
        for o in orders:
            of = o.get("figi") or o.get("instrumentUid")
            if figi and of != figi:
                continue
            st = str(o.get("executionReportStatus") or "")
            if st.endswith(("_FILL", "_REJECTED", "_CANCELLED")):
                continue
            oid = o.get("orderId") or ""
            touched = True
            r = await self.cancel(oid) if oid else {"ok": False, "error": "без orderId"}
            if r.get("ok"):
                n_orders += 1
            else:
                warnings.append(f"{of}: заявка {oid} не снята ({r.get('error') or r.get('note')})")
        if touched and positions is None:
            # V (2-й проход, c): активная заявка могла исполниться (частично) между снимком портфеля и CancelOrder
            # (рыночная в аукционе, «already executed» на отмене) — остаток закрываем ТОЛЬКО по свежему снимку,
            # иначе прежняя продажа + наша = голый разворот
            port2 = await self.portfolio()
            if port2.get("error"):
                if not force:
                    _audit("flat_all_refused", {"mode": self.mode, "figi": figi, "note": f"портфель после снятия заявок: {port2['error']}"})
                    return {"ok": False, "closed": 0, "pending": [], "orders": n_orders,
                            "note": f"после снятия заявок портфель не перечитан ({port2['error']}) — закрытие не отправлено "
                                    "(заявки по инструменту уже сняты); повторная ПАНИКА с force закроет по портфелю как есть"}
                warnings.append(f"портфель после снятия заявок не перечитан ({port2['error']}) — закрываю по прежнему снимку")
            else:
                port = port2
        for so in stops:
            sf = so.get("figi") or so.get("instrumentUid")
            if figi and sf != figi:
                continue
            cancelled = await self.cancel_stop(so.get("stopOrderId") or "")
            if cancelled.get("ok"):
                n_stops += 1
            elif force:
                warnings.append(f"{sf}: отмена защитного стопа {so.get('stopOrderId')} не подтверждена — закрываю по force")
            else:
                blocked.add(sf)
                errors.append(f"{sf}: отмена защитного стопа не подтверждена")
        for p in (port.get("positions") or []):
            qty = p.get("qty") or 0
            if abs(qty) < 1e-9 or not p.get("figi") or (p.get("type") == "currency"):
                continue
            if figi and p.get("figi") != figi and p.get("uid") != figi:
                continue
            if p.get("figi") in blocked or p.get("uid") in blocked:
                continue
            lots = int(abs(qty) / max(1, int(lot or 1))) if p.get("type") == "share" else int(abs(qty))
            if lots <= 0:
                continue
            d = SELL if qty > 0 else BUY
            request_id = str(uuid.uuid4())
            intent = {"order_id": request_id, "request_id": request_id, "id_type": "request",
                      "figi": p["figi"], "lots": lots, "exec_lots": 0, "ts": _now(),
                      "account_id": self.account_id, "mode": self.mode}
            if on_intent is not None:
                try:
                    on_intent(dict(intent))  # persist-success gate before any order I/O
                except Exception as e:                       # noqa: BLE001
                    if not force:
                        raise
                    warnings.append(f"{p['figi']}: намерение не сохранилось ({str(e)[:80]}) — заявка {request_id} по force без персиста")
            r = await self.place(p["figi"], d, lots, price=None, tag="PANIC", request_id=request_id)
            filled = r.get("filled") or str(r.get("status") or "") in (
                "FILL", "FILLED", "EXECUTION_REPORT_STATUS_FILL")
            if r.get("ok") and filled:
                closed += 1
            elif (r.get("ok") or r.get("uncertain")) and r.get("order_id"):
                pending.append({"order_id": r["order_id"], "request_id": r.get("request_id"),
                                "id_type": r.get("id_type") or "exchange", "figi": p["figi"],
                                "lots": lots, "exec_lots": r.get("exec_lots") or 0, "ts": intent["ts"],
                                "account_id": self.account_id, "mode": self.mode})
            else:
                errors.append(f"{p['figi']}: {r.get('error') or r.get('note')}")
        _audit("flat_all", {"mode": self.mode, "closed": closed, "stops": n_stops, "orders": n_orders, "figi": figi,
                            "force": force, "errors": errors, "warnings": warnings})
        return {"ok": not errors, "closed": closed, "stops": n_stops, "orders": n_orders, "pending": pending,
                "stops_unknown": stops_unknown, "warnings": warnings,
                "note": "; ".join(errors + warnings) if (errors or warnings) else None}


# ── self-test: вся логика без единого сетевого запроса (fake poster) ─────────
if __name__ == "__main__":
    import asyncio
    import tempfile
    from pathlib import Path as _P

    _AUDIT_PATH = _P(tempfile.mkdtemp(prefix="pythia_broker_")) / "trader_audit.jsonl"   # noqa: F811  self-тест не пишет в data/

    calls = []

    async def fake_post(path, body, timeout=15.0):
        calls.append((path, body))
        if path.endswith("GetSandboxAccounts"):
            return {"accounts": []}
        if path.endswith("OpenSandboxAccount"):
            return {"accountId": "SB-1"}
        if path.endswith("SandboxPayIn"):
            return {"balance": {"units": 100000}}
        if "Portfolio" in path:
            return {"totalAmountCurrencies": {"units": 98000, "nano": 0},
                    "positions": [{"figi": "SIU5", "quantity": {"units": 3},
                                   "averagePositionPrice": {"units": 90, "nano": 0},
                                   "expectedYield": {"units": 120}}]}
        if path.endswith("PostStopOrder"):
            return {"stopOrderId": "st-1", "orderRequestId": body.get("orderId")}
        if "Order" in path:
            return {"orderId": body.get("orderId"), "executionReportStatus": "FILL",
                    "lotsExecuted": body.get("quantity")}
        return {}

    async def main():
        # 1) DRY по умолчанию — ордер НЕ уходит в сеть
        b = Broker(mode="dry", poster=fake_post)
        assert b.mode == "dry" and not b.is_live()
        r = await b.place("SIU5", BUY, 2, tag="recon")
        assert r["ok"] and r["dry"] and not calls, "dry не должен слать в сеть"

        # 2) real БЕЗ токена Тинькофф → откат в dry (торговать нечем)
        _tok = tinkoff.config.TINKOFF_TOKEN
        tinkoff.config.TINKOFF_TOKEN = ""
        os.environ.pop("PYTHIA_DRY", None)
        b2 = Broker(mode="real", poster=fake_post)
        assert b2.mode == "dry", "real без токена обязан откатиться в dry"

        # 3) real С токеном → сразу боевой; PYTHIA_DRY=1 — принудительный dry
        tinkoff.config.TINKOFF_TOKEN = "t.test"
        b3 = Broker(mode="real", account_id="ACC", poster=fake_post)
        assert b3.mode == "real" and b3.is_live()
        os.environ["PYTHIA_DRY"] = "1"
        assert Broker(mode="real", poster=fake_post).mode == "dry"
        os.environ.pop("PYTHIA_DRY", None)

        # 3b) судьба ордера и снятие: dry исполнен сразу; real — по статусу биржи
        assert (await b.order_state("x"))["filled"] is True          # dry
        st3 = await b3.order_state("px-1")
        assert st3["ok"] and st3["filled"] is True                   # fake: FILL
        assert (await b3.cancel("px-1"))["ok"]
        tinkoff.config.TINKOFF_TOKEN = _tok

        # 4) sandbox: счёт открывается и пополняется, ордер уходит
        #    (токен ставим фейковый: self-тест не должен зависеть от окружения)
        calls.clear()
        tinkoff.config.TINKOFF_TOKEN = "t.test"
        sb = Broker(mode="sandbox", poster=fake_post)
        acc = await sb.ensure_account(100000)
        assert acc["ok"] and sb.account_id == "SB-1"
        assert any("OpenSandboxAccount" in p for p, _ in calls)
        assert any("SandboxPayIn" in p for p, _ in calls)
        pf = await sb.portfolio()
        assert abs(pf["cash"] - 98000) < 1e-6 and pf["positions"][0]["figi"] == "SIU5"
        od = await sb.place("SIU5", BUY, 3, tag="load")
        assert od["ok"] and any("PostSandboxOrder" in p for p, _ in calls)

        # 5) паника закрывает все позиции встречным направлением; W4: активные заявки по инструменту
        #    спрашиваются и снимаются ДО закрытия (GetSandboxOrders раньше PostSandboxOrder)
        calls.clear()
        res = await sb.flat_all([{"figi": "SIU5", "qty": 3}])
        assert res["closed"] == 1
        paths = [p.rsplit("/", 1)[-1] for p, _ in calls]
        assert paths.index("GetSandboxOrders") < paths.index("PostSandboxOrder"), paths
        close_body = [b for p, b in calls if p.endswith("PostSandboxOrder")][0]
        assert close_body["direction"] == SELL and close_body["quantity"] == 3
        # 5b) W4: GetStopOrders недоступен → без force ничего не трогаем (честный текст), с force закрываем
        tinkoff.config.TINKOFF_TOKEN = "t.test"
        seen = []

        async def flaky_post(path, body, timeout=15.0):
            seen.append(path.rsplit("/", 1)[-1])
            if path.endswith("GetStopOrders"):
                raise RuntimeError("GetStopOrders: 503 (тест)")
            if path.endswith("GetOrders"):
                return {"orders": [{"orderId": "o-entry", "figi": "SIU5", "executionReportStatus": "EXECUTION_REPORT_STATUS_NEW"}]}
            return await fake_post(path, body, timeout)
        brf = Broker(mode="real", account_id="ACC", poster=flaky_post)
        r0 = await brf.flat_all([{"figi": "SIU5", "qty": 3}])
        assert r0["ok"] is False and r0["closed"] == 0 and "защитные стопы" in r0["note"] and "force" in r0["note"], r0
        assert "PostOrder" not in seen and "CancelOrder" not in seen, seen
        seen.clear()
        r1 = await brf.flat_all([{"figi": "SIU5", "qty": 3}], force=True)
        assert r1["ok"] and r1["closed"] == 1 and r1["orders"] == 1 and r1["stops_unknown"], r1
        assert seen.index("CancelOrder") < seen.index("PostOrder") and "не сверены" in r1["note"], (seen, r1)
        tinkoff.config.TINKOFF_TOKEN = _tok

        # 6) lots<=0 не выставляется
        assert (await sb.place("SIU5", BUY, 0))["ok"] is False

        # 6b) АППАРАТНЫЙ СТОП (прокол №4): real шлёт PostStopOrder со
        #     stop-market; dry/sandbox честно говорят «виртуальный»
        calls.clear()
        tinkoff.config.TINKOFF_TOKEN = "t.test"
        br = Broker(mode="real", account_id="ACC", poster=fake_post)
        rs = await br.place_stop("SIU5", SELL, 3, 89000.0, tag="cat")
        assert rs["ok"], rs
        p_stop, b_stop = [c for c in calls if "PostStopOrder" in c[0]][0]
        assert b_stop["stopOrderType"] == "STOP_ORDER_TYPE_STOP_LOSS"
        assert b_stop["exchangeOrderType"] == "EXCHANGE_ORDER_TYPE_MARKET"
        assert b_stop["direction"] == STOP_SELL and b_stop["quantity"] == 3
        assert (await br.cancel_stop("st-1"))["ok"]
        assert any("CancelStopOrder" in p for p, _ in calls)
        dry_stop = await b.place_stop("SIU5", SELL, 1, 100.0)
        assert dry_stop["ok"] and dry_stop["virtual"], dry_stop
        assert (await b.place_stop("SIU5", SELL, 0, 100.0))["ok"] is False
        tinkoff.config.TINKOFF_TOKEN = _tok

        # 7) Quotation round-trip
        q = _to_quotation(90.755)
        assert q["units"] == 90 and abs(q["nano"] - 755_000_000) < 1000
        assert abs(_q_to_float(q) - 90.755) < 1e-6

        print("trader_broker self-test OK: dry не шлёт, real по токену (PYTHIA_DRY "
              "глушит), судьба ордера/снятие, песочница/ордер/паника/квотация — верны")

    asyncio.run(main())
