# -*- coding: utf-8 -*-
"""ПИФИЯ v5 — ЖУРНАЛ ПО ОПЕРАЦИЯМ БРОКЕРА (v5.3 фаза 4 · W1).

Пилот пишет сделку в момент закрытия по цене тика — без комиссий и без настоящих цен исполнения
(«прибыль ничего не показывало, и не чистыми»). Этот слой читает ИСПОЛНЕННЫЕ операции счёта
(tinkoff.operations → GetOperationsByCursor), складывает их в таблицу `ops` (идемпотентно) и сверяет
с журналом:
  · сделке пилота по инструменту и окну времени (opened_ts − 60 с … closed_ts + 120 с) подбираются
    исполнения → entry_real / exit_real (VWAP), fee (комиссии входа и выхода), pnl_gross по реальным
    ценам (для акций — по деньгам операций), pnl_net = pnl_gross − fee, source «broker»;
  · сделки, которых в журнале нет (закрытие при упавшем сервере, руками владельца), собираются из
    операций в круги «из нуля в ноль» и добавляются с why «по операциям брокера»;
  · сделки без операций (сухой прогон, мок без операций) остаются оценочными: fee_est = notional ×
    PYTHIA_FEE_PCT (по умолчанию 0,05 % за сторону), честно помечены est.
Режимы (mode()): «broker» — настоящий токен и не PYTHIA_DRY; «mock» — фейковые операции демо
(PYTHIA_MOCK_TINKOFF=1); «est» — операций нет, всё оценка. Ничего не выдумываем: нет операций →
оценка с пометкой, сбой биржи → note. Только чтение счёта.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from . import config, store_v5, tinkoff

log = logging.getLogger("pythia.ledger")

WIN_BEFORE = 60.0            # окно подбора исполнений: до открытия сделки, с
WIN_AFTER = 120.0            # …и после закрытия (операции появляются не мгновенно), с
SYNC_DELAY_SEC = 20.0        # пауза после закрытия перед сверкой
LOOP_LIVE_SEC = 300          # фон: раз в 5 мин при живой миссии/позиции
LOOP_IDLE_SEC = 3600         # …и раз в час иначе
LOOP_FIRST_SEC = 45          # первый проход после старта сервера
SYNC_BACK_DAYS = 30          # первая выгрузка: 30 дней назад
RECON_BACK_DAYS = 60         # сверяем сделки журнала не старше
MSK = timezone(timedelta(hours=3))
KV_SYNC, KV_ACC, KV_VER = "ledger_last_sync", "ledger_account", "ledger_ops_ver"
OPS_VER = 2                  # версия нормализации операций: изменилась → одно полное перечитывание окна (виды в ops)
SOURCES_REAL = ("broker", "mock")
_TASKS: set[asyncio.Task] = set()


def fee_pct() -> float:
    """PYTHIA_FEE_PCT — оценка комиссии за сторону, % от оборота (по умолчанию 0,05)."""
    return max(0.0, config.get_float("PYTHIA_FEE_PCT", 0.05))


def mode() -> str:
    """broker — настоящие операции (токен есть, не PYTHIA_DRY); mock — фейковые операции демо; est — оценка."""
    if os.getenv("PYTHIA_MOCK_TINKOFF") == "1":
        return "mock"
    if getattr(config, "TINKOFF_TOKEN", "") and os.getenv("PYTHIA_DRY") != "1":
        return "broker"
    return "est"


def _f(x, d=None):
    try:
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


# ── оценка ─────────────────────────────────────────────────────────────────────
def _units(tr: dict) -> tuple[int, float]:
    """(штук в сделке, рублей на штуку за пункт цены). Пилот пишет lot (штук в лоте) и point_value
    (₽ за пункт на лот); без них — 1 штука на лот и 1 ₽ за пункт (акции)."""
    data = tr.get("data") or tr                     # запись пилота (lot/point_value сверху) или строка базы (json)
    lots = max(1, int(_f(tr.get("lots"), 1) or 1))
    lot = max(1, int(_f(data.get("lot"), 1) or 1))
    pv = _f(data.get("point_value"), None)
    uv = (pv / lot) if (pv and lot) else 1.0
    return lots * lot, uv


def estimate(tr: dict) -> float | None:
    """fee_est = оборот входа и выхода × PYTHIA_FEE_PCT — оценка, не факт. Нет цен → None."""
    entry, exit_px = _f(tr.get("entry")), _f(tr.get("exit_px"))
    if entry is None or exit_px is None:
        return None
    qty, uv = _units(tr)
    return round((abs(entry) + abs(exit_px)) * qty * uv * fee_pct() / 100.0, 2)


# ── счёт и выгрузка ────────────────────────────────────────────────────────────
async def account_id() -> str | None:
    """Первый счёт владельца (как у пилота); при сбое — последний известный из kv."""
    try:
        accs = await tinkoff.accounts() or []
    except Exception:                                # noqa: BLE001
        accs = []
    acc = accs[0].get("id") if accs and isinstance(accs[0], dict) else None
    if acc:
        try:
            store_v5.kv_set(KV_ACC, acc)
        except Exception:                            # noqa: BLE001
            pass
        return acc
    return store_v5.kv_get(KV_ACC) or None


async def sync(account: str | None = None, since_ts: float | None = None) -> dict:
    """Операции счёта → таблица ops (идемпотентно). {ok, mode, read, added, since, note}."""
    md = mode()
    if md == "est":
        return {"ok": False, "mode": md, "read": 0, "added": 0,
                "note": "операций брокера нет (нет токена Tinkoff или сухой прогон) — журнал оценочный"}
    acc = account or await account_id()
    if not acc:
        return {"ok": False, "mode": md, "read": 0, "added": 0, "note": "счёт Tinkoff не найден"}
    now = time.time()
    last = _f(store_v5.kv_get(KV_SYNC), 0.0) or 0.0
    if store_v5.kv_get(KV_VER) != OPS_VER:
        last = 0.0                                   # нормализация поменялась — полное окно, виды лежащих обновятся
    frm = since_ts or (max(last - 86400.0, now - SYNC_BACK_DAYS * 86400.0) if last else now - SYNC_BACK_DAYS * 86400.0)
    try:
        ops = await tinkoff.operations(acc, frm, now + 60.0)
    except Exception as e:                           # noqa: BLE001
        ops = None
        tinkoff.note_error(e, "GetOperationsByCursor")
    if ops is None:
        err = tinkoff.last_error() or {}
        return {"ok": False, "mode": md, "read": 0, "added": 0, "since": frm,
                "note": "операции не прочитаны: " + str(err.get("text") or "сбой Tinkoff")[:160]}
    added = store_v5.ops_upsert(ops)
    store_v5.kv_set(KV_SYNC, now)
    store_v5.kv_set(KV_VER, OPS_VER)
    return {"ok": True, "mode": md, "read": len(ops), "added": added, "since": frm,
            "note": f"операций прочитано {len(ops)}, новых {added}"}


# ── сверка ─────────────────────────────────────────────────────────────────────
def _vwap(fills: list[tuple[float, int]]) -> float | None:
    q = sum(n for _, n in fills)
    return round(sum(p * n for p, n in fills) / q, 6) if q > 0 else None


def _fills(op: dict) -> list[tuple[float, int]]:
    """Исполнения операции (цена, штук): по tradesInfo, иначе одной строкой по цене операции."""
    out = [(_f(t.get("price"), 0.0) or 0.0, int(_f(t.get("qty"), 0) or 0)) for t in (op.get("trades") or [])]
    out = [(p, n) for p, n in out if n > 0 and p > 0]
    if not out and (op.get("qty") or 0) > 0 and (_f(op.get("price"), 0.0) or 0.0) > 0:
        out = [(float(op["price"]), int(op["qty"]))]
    return out


def _slice_operation(op: dict, offset: int, count: int) -> dict:
    """Allocate a contiguous quantity of fills and its share of cash/commission."""
    all_fills = _fills(op)
    total = sum(n for _, n in all_fills)
    selected, skip, left = [], offset, count
    for price, qty in all_fills:
        before = min(skip, qty)
        skip -= before
        take = min(left, qty - before)
        if take:
            selected.append((price, take))
            left -= take
        if not left:
            break
    value = sum(p * n for p, n in selected)
    whole = sum(p * n for p, n in all_fills)
    return {**op, "qty": count, "price": _vwap(selected),
            "trades": [{"price": p, "qty": n} for p, n in selected],
            "payment": float(op.get("payment") or 0) * value / whole if whole else 0,
            "fee": float(op.get("fee") or 0) * count / total if total else 0,
            "_offset": op.get("_offset", 0) + offset,
            "_fraction": op.get("_fraction", 1.0) * count / total if total else 0}


def _take(ops: list[dict], limit: int | None, allocated: dict | None = None) -> tuple[list[dict], list[tuple[float, int]]]:
    """Consume only the unused quantities; one operation may serve several partial exits."""
    taken, fills, got = [], [], 0
    for op in ops:
        if limit is not None and got >= limit:
            break
        total = sum(n for _, n in _fills(op))
        cursor = 0
        used_ranges = sorted((allocated or {}).get(op["id"], []))
        # End sentinel also yields the final unused range.
        for start, count in used_ranges + [(total, 0)]:
            start, count = min(total, max(0, int(start))), max(0, int(count))
            if start > cursor:
                n = min(start - cursor, limit - got if limit is not None else total)
                if n > 0:
                    part = _slice_operation(op, cursor, n)
                    taken.append(part)
                    fills.extend(_fills(part))
                    got += n
            cursor = max(cursor, start + count)
            if limit is not None and got >= limit:
                break
    return taken, fills


def _allocations(matched: list[dict], extra: list[dict]) -> dict:
    out = {"fills": {}, "fees": {}}
    for op in matched:
        out["fills"].setdefault(op["id"], []).append([op.get("_offset", 0), int(op["qty"])])
    for op in extra:
        out["fees"][op["id"]] = out["fees"].get(op["id"], 0.0) + op.get(
            "_fee_amount", abs(float(op.get("payment") or 0.0)) or float(op.get("fee") or 0.0))
    return out


def _reserve(allocation: dict, fills: dict, fees: dict) -> None:
    for key, ranges in allocation.get("fills", {}).items():
        fills.setdefault(key, []).extend(ranges)
    for key, amount in allocation.get("fees", {}).items():
        fees[key] = fees.get(key, 0.0) + amount


def _fees_for(matched: list[dict], fee_ops: list[dict], w0: float, w1: float, used: set,
              allocated: dict | None = None) -> tuple[float, list[dict]]:
    """Комиссии сделки: поле commission операций + отдельные операции BROKER_FEE (дочерние по parent или
    в окне без родителя); дочерняя не считается дважды, если родитель уже нёс commission."""
    ids = {o["id"] for o in matched}
    fee = sum(float(o.get("fee") or 0.0) for o in matched)
    extra = []
    for f in fee_ops:
        total_fee = abs(float(f.get("payment") or 0.0)) or float(f.get("fee") or 0.0)
        remaining = max(0.0, total_fee - (allocated or {}).get(f["id"], 0.0))
        if f["id"] in used or remaining <= 1e-9 or not (w0 <= float(f["ts"]) <= w1):
            continue
        par = f.get("parent")
        if par:
            if par not in ids:
                continue
            parents = [o for o in matched if o["id"] == par]
            if any(float(p.get("fee") or 0.0) > 0 for p in parents):
                continue
            amount = min(remaining, total_fee * sum(p.get("_fraction", 1.0) for p in parents))
        else:
            amount = remaining
        fee += amount
        extra.append({**f, "_fee_amount": amount})
    return round(fee, 4), extra


def _gross(side: str, entries: list[dict], exits: list[dict], e_vwap, x_vwap, qty: int, uv: float,
           entry_tick, exit_tick) -> tuple[float | None, str]:
    """Брутто по реальным ценам: акции с деньгами в операциях и обе стороны в наличии — сумма payment
    (точно); иначе по VWAP (недостающая сторона — цена тика пилота, честно «частично»)."""
    sgn = 1.0 if side == "long" else -1.0
    both = bool(entries) and bool(exits)
    pays = [float(o.get("payment") or 0.0) for o in entries + exits]
    e_qty = sum(n for op in entries for _, n in _fills(op))
    x_qty = sum(n for op in exits for _, n in _fills(op))
    if both and e_qty == x_qty == qty and all(abs(p) > 0 for p in pays):
        return round(sum(pays), 2), "payments"
    e = e_vwap if e_vwap is not None else _f(entry_tick)
    x = x_vwap if x_vwap is not None else _f(exit_tick)
    if e is None or x is None or qty <= 0:
        return None, "нет цен"
    return round((x - e) * sgn * qty * uv, 2), ("vwap" if both else "partial")


async def _keys(ticker: str, trades: list[dict]) -> list[str]:
    """figi/uid инструмента: из сделок пилота, иначе резолвер брокера (в моке — MOCK-<тикер>)."""
    keys = {t.get("figi") for t in trades if t.get("figi")}
    keys |= {(t.get("data") or {}).get("figi") for t in trades if (t.get("data") or {}).get("figi")}
    if not keys:
        try:
            from . import instruments
            it = instruments.get(ticker) or {}
            ac = it.get("asset_class") or instruments.guess_asset_class(ticker)
            inst = await tinkoff.resolve(ticker, ac) or {}
            keys |= {inst.get("figi"), inst.get("uid")}
        except Exception as e:                       # noqa: BLE001
            log.info("ledger %s: инструмент не разрешён: %s", ticker, str(e)[:80])
    return [k for k in keys if k]


def _horizon(ticker: str, trades: list[dict]) -> float | None:
    """С какого момента операции без сделки в журнале — сделки бота/владельца по этой миссии:
    самая ранняя сделка журнала или старт миссии; ничего нет → None (чужие круги не трогаем)."""
    hs = [_f(t.get("opened_ts")) or _f(t.get("closed_ts")) for t in trades]
    try:
        m = store_v5.mission_get(ticker) or {}
        if _f(m.get("started_ts")):
            hs.append(float(m["started_ts"]))
    except Exception:                                # noqa: BLE001
        pass
    hs = [h for h in hs if h]
    return (min(hs) - WIN_BEFORE) if hs else None


def _round_trips(ops: list[dict], fee_ops: list[dict], used: set, horizon: float | None,
                 lot: int, uv: float, allocated_fees: dict | None = None) -> list[dict]:
    """Незадействованные покупки/продажи после горизонта → круги «из нуля в ноль» → записи сделок."""
    if horizon is None:
        return []
    out, cur, pos = [], [], 0
    elig = [op for op in ops if op["id"] not in used and float(op["ts"]) >= horizon
            and op["kind"] in ("buy", "sell") and int(op.get("qty") or 0) > 0]
    for i, op in enumerate(elig):
        n = int(op.get("qty") or 0)
        cur.append(op)
        pos += n if op["kind"] == "buy" else -n
        if pos != 0:
            continue
        side = "long" if cur[0]["kind"] == "buy" else "short"
        ent_dir = "buy" if side == "long" else "sell"
        entries = [o for o in cur if o["kind"] == ent_dir]
        exits = [o for o in cur if o["kind"] != ent_dir]
        _, ef = _take(entries, None)
        _, xf = _take(exits, None)
        e_vwap, x_vwap = _vwap(ef), _vwap(xf)
        qty = sum(n_ for _, n_ in ef)
        w0, w1 = float(cur[0]["ts"]) - 5.0, float(cur[-1]["ts"]) + WIN_AFTER
        if i + 1 < len(elig):                        # комиссии соседнего круга — не наши
            w1 = min(w1, float(elig[i + 1]["ts"]) - 0.001)
        fee, extra = _fees_for(cur, fee_ops, w0, w1, used, allocated_fees)
        gross, how = _gross(side, entries, exits, e_vwap, x_vwap, qty, uv, None, None)
        if gross is None:
            cur, pos = [], 0
            continue
        ids = [o["id"] for o in cur + extra]
        used.update(ids)
        out.append({"side": side, "lots": max(1, qty // max(1, lot)), "entry": e_vwap, "exit_px": x_vwap,
                    "pnl": gross, "opened_ts": float(cur[0]["ts"]), "closed_ts": float(cur[-1]["ts"]),
                    "why": "по операциям брокера", "entry_real": e_vwap, "exit_real": x_vwap,
                    "fee": fee, "pnl_gross": gross, "pnl_net": round(gross - fee, 2), "ops": ids,
                    "ops_alloc": _allocations(cur, extra), "how": how})
        cur = []
    return out


async def reconcile_ticker(ticker: str, apply: bool = True) -> dict:
    """Сверка одного инструмента: {ticker, matched, partial, added, est, keys, note}. apply=False — только счёт."""
    t = (ticker or "").upper().strip()
    md = mode()
    now = time.time()
    trades = store_v5.trades_full(t, since_ts=now - RECON_BACK_DAYS * 86400.0)
    res = {"ticker": t, "matched": 0, "partial": 0, "added": 0, "est": 0, "keys": [], "note": ""}
    keys = await _keys(t, trades) if md != "est" else []
    res["keys"] = keys
    ops = store_v5.ops_list(keys, kinds=("buy", "sell", "fee")) if keys else []
    fills = [o for o in ops if o["kind"] in ("buy", "sell")]
    fee_ops = [o for o in ops if o["kind"] == "fee"]
    used: set = set()
    blocked: set = set()  # Legacy rows without quantity allocations own their complete operation.
    allocated, allocated_fees = {}, {}
    for tr in trades:
        if tr.get("source") == "partial":
            continue                                 # повторная сверка вправе собрать обе стороны заново
        used.update(tr.get("ops") or [])
        if tr.get("ops_alloc"):
            _reserve(tr["ops_alloc"], allocated, allocated_fees)
        else:
            blocked.update(tr.get("ops") or [])
    src = "mock" if md == "mock" else "broker"
    for tr in trades:
        if tr.get("source") in SOURCES_REAL and tr.get("synced_ts"):
            continue                                 # уже сверена по операциям
        closed = _f(tr.get("closed_ts")) or now
        opened = _f(tr.get("opened_ts")) or (closed - 3600.0)
        w0, w1 = opened - WIN_BEFORE, closed + WIN_AFTER
        qty, uv = _units(tr)
        data = tr.get("data") or {}
        lot = max(1, int(_f(data.get("lot"), 1) or 1))
        side = tr.get("side") or "long"
        ent_dir = "buy" if side == "long" else "sell"
        cand = [o for o in fills if o["id"] not in blocked and w0 <= float(o["ts"]) <= w1]
        entries, ef = _take([o for o in cand if o["kind"] == ent_dir and float(o["ts"]) <= closed + 1.0], qty, allocated)
        t_ent = float(entries[0]["ts"]) if entries else w0           # выход — только после первого входа
        exits, xf = _take([o for o in cand if o["kind"] != ent_dir and float(o["ts"]) >= t_ent], qty, allocated)
        if not entries and not exits:
            fe = estimate(tr)
            res["est"] += 1
            if apply and (tr.get("fee_est") is None) and fe is not None:
                store_v5.trade_update(tr["id"], {"fee_est": fe, "source": tr.get("source") or "est"})
            continue
        e_vwap, x_vwap = _vwap(ef), _vwap(xf)
        matched = entries + exits
        fee, extra = _fees_for(matched, fee_ops, w0, w1, blocked, allocated_fees)
        e_qty, x_qty = sum(n for _, n in ef), sum(n for _, n in xf)
        q_real = min(qty, e_qty or qty, x_qty or qty)
        gross, how = _gross(side, entries, exits, e_vwap, x_vwap, q_real, uv, tr.get("entry"), tr.get("exit_px"))
        if gross is None:
            res["est"] += 1
            continue
        # Both sides may arrive in batches. Until all units match, future syncs
        # must be allowed to pick up the missing fills instead of freezing P/L.
        full = e_qty == x_qty == qty
        ids = list(dict.fromkeys(o["id"] for o in matched + extra))
        allocation = _allocations(matched, extra)
        _reserve(allocation, allocated, allocated_fees)
        used.update(ids)
        res["matched" if full else "partial"] += 1
        if apply:
            store_v5.trade_update(tr["id"], {
                "entry_real": e_vwap, "exit_real": x_vwap, "fee": fee, "pnl_gross": gross,
                "pnl_net": round(gross - fee, 2), "source": src if full else "partial",
                "synced_ts": now, "ops": ids, "ops_alloc": allocation,
                "figi": tr.get("figi") or (matched[0].get("figi") if matched else None),
                "fee_est": tr.get("fee_est") if tr.get("fee_est") is not None else estimate(tr)})
        _ = lot
    # круги без сделки в журнале
    lot0 = 1
    uv0 = 1.0
    for tr in reversed(trades):
        d = tr.get("data") or {}
        if d.get("lot"):
            lot0 = max(1, int(_f(d.get("lot"), 1) or 1))
            uv0 = _units(tr)[1]
            break
    remaining, _ = _take([op for op in fills if op["id"] not in blocked], None, allocated)
    for rec in _round_trips(remaining, fee_ops, blocked.copy(), _horizon(t, trades), lot0, uv0, allocated_fees):
        res["added"] += 1
        if apply:
            rec.update({"ticker": t, "mode": "dry" if md == "mock" else "real", "source": src,
                        "figi": keys[0] if keys else None, "synced_ts": now})
            rec.pop("how", None)
            try:
                store_v5.trade_add(rec)
            except Exception as e:                   # noqa: BLE001
                log.warning("ledger %s: сделка из операций не записалась: %s", t, str(e)[:100])
    if md == "est":
        res["note"] = "операций брокера нет — оценка (fee_est = оборот × PYTHIA_FEE_PCT)"
    elif not keys:
        res["note"] = "инструмент не разрешён у брокера — сделки остаются оценочными"
    else:
        res["note"] = (f"сверено {res['matched']}, частично {res['partial']}, добавлено из операций {res['added']}, "
                       f"оценочных {res['est']}")
    return res


def _tickers() -> list[str]:
    out: set[str] = set()
    try:
        out |= {t["ticker"] for t in store_v5.trades(None, 1000) if t.get("ticker")}
    except Exception:                                # noqa: BLE001
        pass
    try:
        out |= set(store_v5.mission_all().keys())
    except Exception:                                # noqa: BLE001
        pass
    return sorted(out)


async def reconcile(ticker: str | None = None, apply: bool = True) -> dict:
    """Сверка журнала с операциями: один инструмент или все (сделки журнала ∪ миссии).
    {ok, mode, matched, partial, added, est, tickers:{T: …}, note}."""
    tickers = [ticker.upper().strip()] if ticker else _tickers()
    res = {"ok": True, "mode": mode(), "matched": 0, "partial": 0, "added": 0, "est": 0, "tickers": {}, "note": ""}
    for t in tickers:
        try:
            r = await reconcile_ticker(t, apply)
        except Exception as e:                       # noqa: BLE001
            log.warning("ledger: сверка %s: %s", t, str(e)[:120])
            r = {"ticker": t, "error": str(e)[:120]}
        res["tickers"][t] = r
        for k in ("matched", "partial", "added", "est"):
            res[k] += int(r.get(k) or 0)
    if res["mode"] == "est":
        res["note"] = "операций брокера нет — все сделки оценочные"
    else:
        res["note"] = (f"сверено {res['matched']}, частично {res['partial']}, добавлено {res['added']}, "
                       f"оценочных {res['est']}")
    return res


async def sync_and_reconcile(ticker: str | None = None) -> dict:
    """Ручная/фоновая сверка: выгрузка операций → сверка. {ok, sync:{…}, reconcile:{…}, note}."""
    s = await sync()
    r = await reconcile(ticker)
    return {"ok": bool(s.get("ok")), "mode": mode(), "sync": s, "reconcile": r,     # est → ok False и note
            "note": (s.get("note") or "") + " · " + (r.get("note") or ""),
            "last_sync": _f(store_v5.kv_get(KV_SYNC))}


# ── сводки ─────────────────────────────────────────────────────────────────────
def trades_summary(ticker: str | None = None) -> dict:
    """store_v5.trades_summary + режим журнала и время последней выгрузки."""
    s = store_v5.trades_summary(ticker)
    s["mode"] = mode()
    s["last_sync"] = _f(store_v5.kv_get(KV_SYNC))
    return s


def day_bounds(day: str | None = None) -> tuple[str, float, float]:
    """(YYYY-MM-DD по МСК, начало, конец) — день журнала по московскому времени."""
    if day:
        d = datetime.strptime(day.strip()[:10], "%Y-%m-%d").replace(tzinfo=MSK)
    else:
        d = datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0)
    return d.strftime("%Y-%m-%d"), d.timestamp(), (d + timedelta(days=1)).timestamp()


def _brief(t: dict) -> dict:
    return {k: t.get(k) for k in ("id", "ticker", "side", "lots", "entry", "exit_px", "entry_real", "exit_real",
                                  "pnl", "pnl_gross", "fee", "net", "est", "source", "why", "opened_ts",
                                  "closed_ts", "mode")}


async def day_summary(day: str | None = None) -> dict:
    """Итоги дня (для экрана и Telegram): сделки дня, нетто/брутто/комиссии, лучшая/худшая, сессионный P/L
    живого пилота, деньги на счёте. Нет данных → None и note, ничего не выдумывается."""
    try:
        d, t0, t1 = day_bounds(day)
    except ValueError:
        return {"ok": False, "note": "дата — YYYY-MM-DD"}
    items = store_v5.trades_between(t0, t1)
    net = round(sum(float(t.get("net") or 0.0) for t in items), 2)
    gross = round(sum(float(t.get("pnl_gross") or 0.0) for t in items), 2)
    fee = round(sum(float(t.get("fee") or 0.0) for t in items), 2)
    best = max(items, key=lambda t: float(t.get("net") or 0.0)) if items else None
    worst = min(items, key=lambda t: float(t.get("net") or 0.0)) if items else None
    session = None
    pilot_ticker = None
    try:
        from . import mission as _mission
        m = _mission._active()
        if m is not None and m.pilot is not None:
            st = m.pilot.status()
            session = _f(st.get("session_pnl"))
            pilot_ticker = m.ticker
    except Exception:                                # noqa: BLE001
        session = None
    account = {"total": None, "cash": None, "note": "нет токена Tinkoff — денег на счёте не видно"}
    if tinkoff.enabled():
        try:
            acc = await account_id()
            pf = await tinkoff.portfolio(acc) if acc else None
            if pf:
                account = {"total": pf.get("total_rub"), "cash": pf.get("free_rub"),
                           "note": "по портфелю брокера" + (" (мок)" if mode() == "mock" else "")}
            else:
                account["note"] = "портфель не прочитан"
        except Exception as e:                       # noqa: BLE001
            account["note"] = f"портфель не прочитан: {str(e)[:80]}"
    margin_fee = None                                # плата за маржу за день — из операций (не сделки)
    if mode() != "est":
        try:
            margin_fee = store_v5.ops_stats(t0, t1).get("margin")
        except Exception:                            # noqa: BLE001
            margin_fee = None
    notes = []
    if not items:
        notes.append("сделок за день нет")
    if any(t.get("est") for t in items):
        notes.append("часть сделок оценочные (комиссия по PYTHIA_FEE_PCT, без операций брокера)")
    return {"ok": True, "day": d, "from_ts": t0, "to_ts": t1, "count": len(items), "net": net, "gross": gross,
            "fee": fee, "wins": sum(1 for t in items if float(t.get("net") or 0) > 0),
            "losses": sum(1 for t in items if float(t.get("net") or 0) < 0),
            "best": _brief(best) if best else None, "worst": _brief(worst) if worst else None,
            "trades": [_brief(t) for t in items], "est": any(t.get("est") for t in items),
            "margin_fee": margin_fee, "session_pnl": session, "pilot_ticker": pilot_ticker, "account": account,
            "mode": mode(), "last_sync": _f(store_v5.kv_get(KV_SYNC)),
            "note": "; ".join(notes) or "по журналу сделок"}


# ── фон ────────────────────────────────────────────────────────────────────────
def schedule_after_close(ticker: str, delay: float = SYNC_DELAY_SEC, live: bool = False) -> bool:
    """После записи сделки пилотом: через ~20 с выгрузить операции и сверить этот инструмент.
    live — сделка прошла через настоящий боевой Broker (иначе в режиме broker сверять нечего: фейки
    self-тестов не торгуют на счёте). В режиме est — ничего (оценка); без живого цикла — False."""
    md = mode()
    if md == "est" or (md == "broker" and not live):
        return False

    async def _job():
        try:
            t_job = time.time()
            await asyncio.sleep(delay)
            r = await sync_and_reconcile(ticker)
            log.info("ledger %s: %s", ticker, r.get("note"))
            await announce(ticker, since_ts=t_job, result=r)
        except asyncio.CancelledError:
            raise
        except Exception as e:                       # noqa: BLE001
            log.warning("ledger %s: сверка после закрытия: %s", ticker, str(e)[:120])
    try:
        task = asyncio.get_running_loop().create_task(_job())
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
        return True
    except RuntimeError:
        return False


async def shutdown_tasks() -> None:
    tasks = list(_TASKS)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _TASKS.difference_update(tasks)


def confirmed_since(ticker: str, since_ts: float) -> dict | None:
    """Последняя сделка инструмента, подтверждённая операциями (source broker|mock) после since_ts, иначе None."""
    try:
        for t in store_v5.trades(ticker, 5):
            if t.get("source") in SOURCES_REAL and float(t.get("synced_ts") or 0) >= float(since_ts):
                return t
    except Exception as e:                           # noqa: BLE001
        log.info("ledger %s: сделка после сверки не прочитана: %s", ticker, str(e)[:80])
    return None


async def announce(ticker: str, since_ts: float, result: dict | None = None) -> bool:
    """Фаза 4 · W2: сверка после закрытия прошла → событие шины scope "ledger" (Telegram шлёт «чистыми»);
    подтверждённой сделки нет — тишина. Никогда не роняет вызывающего."""
    try:
        tr = confirmed_since(ticker, since_ts)
        if not tr:
            return False
        from . import bus
        await bus.stage("ledger", f"ledger-{ticker}", "reconcile", "done", ticker=ticker,
                        detail=f"сверено с брокером: нетто {float(tr.get('net') or 0):+.2f}",
                        data={"trade": tr, "note": (result or {}).get("note")})
        return True
    except Exception as e:                           # noqa: BLE001
        log.info("ledger %s: событие сверки не ушло: %s", ticker, str(e)[:80])
        return False


def _live() -> bool:
    """Живая миссия или позиция → частый ритм."""
    try:
        from . import mission as _mission
        m = _mission._active()
        if m is None:
            return False
        p = m.pilot
        return bool(m.pilot_alive() or (p is not None and p.position))
    except Exception:                                # noqa: BLE001
        return False


async def loop() -> None:
    """Фоновая сверка: раз в 5 мин при живой миссии/позиции, раз в час иначе; только при операциях
    (broker/mock) — в est спит и честно ничего не делает."""
    await asyncio.sleep(LOOP_FIRST_SEC)
    while True:
        try:
            if mode() != "est":
                r = await sync_and_reconcile()
                log.info("ledger: %s", r.get("note"))
            await asyncio.sleep(LOOP_LIVE_SEC if _live() else LOOP_IDLE_SEC)
        except asyncio.CancelledError:
            raise
        except Exception as e:                       # noqa: BLE001
            log.warning("ledger: фон споткнулся: %s", str(e)[:120])
            await asyncio.sleep(60)


# ══════════════════════════════════════════════════════════════════════════════
# Self-тест: временная база, фейковые операции — VWAP, комиссии, нетто, недостающая сделка, дубли, оценка
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    store_v5.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
    store_v5._schema()
    os.environ.pop("PYTHIA_MOCK_TINKOFF", None)
    os.environ["PYTHIA_DRY"] = "0"
    config.TINKOFF_TOKEN = "t.fake-token-for-self-test"          # только в памяти процесса
    assert mode() == "broker"
    T0 = float(int(time.time()) - 3 * 86400)                     # три дня назад: сделки в окне сверки

    FAKE_OPS = [
        # сделка 1 (журнал): long 2 лота × 10 шт SBER, вход двумя исполнениями, выход одним; комиссии в commission
        {"id": "b1", "ts": T0 + 5, "kind": "buy", "figi": "F-SBER", "uid": "U-SBER", "qty": 20, "price": 300.5,
         "payment": -6010.0, "fee": 3.0, "trades": [{"ts": T0 + 5, "qty": 10, "price": 300.0},
                                                    {"ts": T0 + 6, "qty": 10, "price": 301.0}]},
        {"id": "s1", "ts": T0 + 600, "kind": "sell", "figi": "F-SBER", "uid": "U-SBER", "qty": 20, "price": 304.0,
         "payment": 6080.0, "fee": 3.04, "trades": []},
        # дочерняя BROKER_FEE у родителя, который УЖЕ нёс commission — не удваивается
        {"id": "f1", "ts": T0 + 601, "kind": "fee", "figi": "F-SBER", "uid": "U-SBER", "qty": 0, "price": 0.0,
         "payment": -3.04, "fee": 0.0, "parent": "s1"},
        # сделка 2 (журнал): short 1 лот, комиссии только отдельными BROKER_FEE без родителя
        {"id": "s2", "ts": T0 + 4000, "kind": "sell", "figi": "F-SBER", "uid": "U-SBER", "qty": 10, "price": 310.0,
         "payment": 3100.0, "fee": 0.0, "trades": []},
        {"id": "f2", "ts": T0 + 4001, "kind": "fee", "figi": "F-SBER", "uid": "U-SBER", "qty": 0, "price": 0.0,
         "payment": -1.55, "fee": 0.0},
        {"id": "b2", "ts": T0 + 4300, "kind": "buy", "figi": "F-SBER", "uid": "U-SBER", "qty": 10, "price": 312.0,
         "payment": -3120.0, "fee": 0.0, "trades": []},
        {"id": "f3", "ts": T0 + 4301, "kind": "fee", "figi": "F-SBER", "uid": "U-SBER", "qty": 0, "price": 0.0,
         "payment": -1.56, "fee": 0.0},
        # круг без сделки в журнале (сервер лежал): long 10 шт 320 → 325
        {"id": "b3", "ts": T0 + 9000, "kind": "buy", "figi": "F-SBER", "uid": "U-SBER", "qty": 10, "price": 320.0,
         "payment": -3200.0, "fee": 1.6, "trades": []},
        {"id": "s3", "ts": T0 + 9500, "kind": "sell", "figi": "F-SBER", "uid": "U-SBER", "qty": 10, "price": 325.0,
         "payment": 3250.0, "fee": 1.63, "trades": []},
        # открытая позиция (не круг) — сделкой не становится
        {"id": "b4", "ts": T0 + 12000, "kind": "buy", "figi": "F-SBER", "uid": "U-SBER", "qty": 10, "price": 330.0,
         "payment": -3300.0, "fee": 1.65, "trades": []},
        # чужой инструмент — не трогаем
        {"id": "g1", "ts": T0 + 100, "kind": "buy", "figi": "F-GAZP", "uid": "U-GAZP", "qty": 5, "price": 130.0,
         "payment": -650.0, "fee": 0.3, "trades": []},
        {"id": "in1", "ts": T0, "kind": "other", "figi": None, "uid": None, "qty": 0, "price": 0.0,
         "payment": 50000.0, "fee": 0.0, "trades": []},
    ]
    calls: list = []

    async def fake_ops(account, frm, to, instrument_id=None):
        calls.append((account, frm, to))
        return [dict(o) for o in FAKE_OPS if frm <= o["ts"] <= to]

    async def fake_accounts():
        return [{"id": "acc-1"}]

    async def fake_portfolio(acc):
        return {"total_rub": 123456.0, "free_rub": 1000.0}

    async def fake_resolve(ticker, asset_class):
        return {"figi": f"F-{ticker}", "uid": f"U-{ticker}"} if ticker == "GAZP" else None

    async def no_net(*a, **k):
        raise AssertionError("self-тест не ходит в сеть")

    tinkoff.operations, tinkoff.accounts, tinkoff.portfolio = fake_ops, fake_accounts, fake_portfolio
    tinkoff.resolve, tinkoff._post = fake_resolve, no_net
    tinkoff.enabled = lambda: True

    async def main():
        # сделки пилота по цене тика (как MissionPilot._journal): figi/lot/point_value в json
        t1 = store_v5.trade_add({"ticker": "SBER", "side": "long", "lots": 2, "entry": 300.4, "exit_px": 303.9,
                                 "pnl": 70.0, "opened_ts": T0 + 4, "closed_ts": T0 + 599, "why": "тейк", "mode": "real",
                                 "figi": "F-SBER", "lot": 10, "point_value": 10.0})
        t2 = store_v5.trade_add({"ticker": "SBER", "side": "short", "lots": 1, "entry": 310.0, "exit_px": 312.5,
                                 "pnl": -25.0, "opened_ts": T0 + 3999, "closed_ts": T0 + 4299, "why": "трос", "mode": "real",
                                 "figi": "F-SBER", "lot": 10, "point_value": 10.0})
        # оценочная сделка без операций (dry), далеко от окон
        t3 = store_v5.trade_add({"ticker": "SBER", "side": "long", "lots": 1, "entry": 300.0, "exit_px": 301.0,
                                 "pnl": 10.0, "opened_ts": T0 + 20000, "closed_ts": T0 + 20100, "why": "dry", "mode": "dry",
                                 "figi": "F-SBER", "lot": 10, "point_value": 10.0})
        assert abs(estimate(store_v5.trades_full("SBER")[2]) - 3.0) < 1e-9        # (300+301)×10×0,05 %
        s = await sync(since_ts=T0 - 10)
        assert s["ok"] and s["read"] == len(FAKE_OPS) and s["added"] == len(FAKE_OPS) and calls[0][0] == "acc-1", s
        s2 = await sync(since_ts=T0 - 10)
        assert s2["ok"] and s2["added"] == 0 and store_v5.ops_stats()["count"] == len(FAKE_OPS)
        assert store_v5.kv_get(KV_VER) == OPS_VER
        s3 = await sync()                                   # версия совпадает — окно от последней выгрузки минус сутки
        assert s3["ok"] and s3["since"] > T0 and s3["read"] == 0, s3
        store_v5.kv_set(KV_VER, 1)
        s4 = await sync()                                   # версия старая — полное окно 30 дней, всё перечитано
        assert s4["ok"] and s4["read"] == len(FAKE_OPS) and s4["added"] == 0 and store_v5.kv_get(KV_VER) == OPS_VER, s4
        r = await reconcile("SBER")
        assert r["ok"] and r["matched"] == 2 and r["added"] == 1 and r["est"] == 1 and r["partial"] == 0, r
        rows = {x["id"]: x for x in store_v5.trades_full("SBER")}
        a = rows[t1]
        assert a["entry_real"] == 300.5 and a["exit_real"] == 304.0 and a["source"] == "broker" and not a["est"], a
        assert a["pnl_gross"] == 70.0 and abs(a["fee"] - 6.04) < 1e-9 and abs(a["pnl_net"] - 63.96) < 1e-9, a
        assert sorted(a["ops"]) == ["b1", "s1"] and a["fee_est"] is not None, a
        b = rows[t2]
        assert b["entry_real"] == 310.0 and b["exit_real"] == 312.0 and b["pnl_gross"] == -20.0, b
        assert abs(b["fee"] - 3.11) < 1e-9 and abs(b["pnl_net"] + 23.11) < 1e-9 and sorted(b["ops"]) == ["b2", "f2", "f3", "s2"], b
        c = rows[t3]
        assert c["est"] and c["source"] == "est" and c["fee_est"] == 3.0 and c["net"] == 7.0 and c["pnl_net"] is None, c
        added = [x for x in rows.values() if x["why"] == "по операциям брокера"]
        assert len(added) == 1
        d = added[0]
        assert d["side"] == "long" and d["lots"] == 1 and d["entry"] == 320.0 and d["exit_px"] == 325.0, d
        assert d["pnl_gross"] == 50.0 and abs(d["fee"] - 3.23) < 1e-9 and abs(d["pnl_net"] - 46.77) < 1e-9 and d["mode"] == "real", d
        assert sorted(d["ops"]) == ["b3", "s3"] and d["source"] == "broker" and d["figi"] == "F-SBER"
        assert not any(x["why"] == "по операциям брокера" and "b4" in x["ops"] for x in rows.values())
        # повтор — ничего не плодится и не меняется
        r2 = await reconcile("SBER")
        assert r2["added"] == 0 and r2["matched"] == 0 and r2["est"] == 1, r2
        assert len(store_v5.trades_full("SBER")) == 4
        # чужой инструмент (GAZP) без сделок и миссии — кругов нет (горизонта нет)
        rg = await reconcile_ticker("GAZP")
        assert rg["added"] == 0 and rg["est"] == 0, rg
        # сводка: нетто/брутто/комиссии, est только из-за dry, по дням
        sm = trades_summary("SBER")
        assert sm["count"] == 4 and sm["confirmed"] == 3 and sm["est"] and sm["mode"] == "broker" and sm["last_sync"], sm
        assert abs(sm["gross"] - (70 - 20 + 50 + 10)) < 1e-6 and abs(sm["fee"] - (6.04 + 3.11 + 3.23 + 3.0)) < 1e-6, sm
        assert abs(sm["net"] - (63.96 - 23.11 + 46.77 + 7.0)) < 1e-6 and sm["pnl"] == sm["net"] and sm["wins"] == 3, sm
        assert sm["by_day"] and sum(x["count"] for x in sm["by_day"]) == 4
        # итоги дня по МСК
        day = datetime.fromtimestamp(T0 + 599, MSK).strftime("%Y-%m-%d")
        ds = await day_summary(day)
        assert ds["ok"] and ds["day"] == day and ds["count"] >= 1 and ds["account"]["total"] == 123456.0, ds
        assert ds["best"] and ds["worst"] and ds["best"]["net"] >= ds["worst"]["net"] and ds["mode"] == "broker"
        assert ds["from_ts"] <= T0 + 599 < ds["to_ts"] and ds["session_pnl"] is None and ds["margin_fee"] == 0.0
        bad = await day_summary("вчера")
        assert bad["ok"] is False
        # оценочный режим: sync честно отказывает, reconcile всё считает оценкой, хук не ставится
        config.TINKOFF_TOKEN = ""
        assert mode() == "est" and schedule_after_close("SBER") is False
        se = await sync()
        assert se["ok"] is False and "оценоч" in se["note"]
        re_ = await reconcile("SBER")
        assert re_["mode"] == "est" and re_["est"] == 1 and re_["matched"] == 0 and re_["added"] == 0
        assert trades_summary()["mode"] == "est"
        # сбой чтения операций → ok False и note, база не тронута
        config.TINKOFF_TOKEN = "t.fake"
        async def boom(*a, **k):
            return None
        tinkoff.operations = boom
        sb = await sync(since_ts=T0)
        assert sb["ok"] is False and "не прочитаны" in sb["note"] and store_v5.ops_stats()["count"] == len(FAKE_OPS)
        # хук после закрытия: задача ставится в живом цикле (не ждём 20 с — delay 0)
        tinkoff.operations = fake_ops
        assert schedule_after_close("SBER", delay=0.0) is False and schedule_after_close("SBER", delay=0.0, live=True) is True
        await asyncio.sleep(0.05)
        assert calls[-1][0] == "acc-1"
        # фаза 4 · W2: после сверки — событие шины scope "ledger" с подтверждённой сделкой (Telegram шлёт «чистыми»)
        from . import bus as _bus
        got: list[dict] = []

        async def sink(ev):
            got.append(ev)
        _bus.set_sink(sink)
        first = min(float(t.get("synced_ts") or 0) for t in store_v5.trades("SBER", 10) if t.get("source") == "broker")
        tr = confirmed_since("SBER", first)
        assert tr and tr["source"] == "broker" and tr["net"] is not None
        assert confirmed_since("SBER", time.time() + 10) is None and confirmed_since("GAZP", 0) is None
        assert await announce("SBER", since_ts=first, result={"note": "ок"}) is True
        assert got[-1]["scope"] == "ledger" and got[-1]["stage"] == "reconcile" and got[-1]["status"] == "done" \
            and got[-1]["ticker"] == "SBER" and got[-1]["data"]["trade"]["source"] == "broker" \
            and "нетто" in got[-1]["detail"], got[-1]
        assert await announce("SBER", since_ts=time.time() + 10) is False and len(got) == 1   # нечего объявлять — тишина
        _bus.set_sink(None)

    asyncio.run(main())
    print("ledger self-test OK (+ событие сверки для Telegram)")
