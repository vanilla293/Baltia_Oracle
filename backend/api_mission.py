# -*- coding: utf-8 -*-
"""ПИФИЯ v5 — API миссии (§4 контракта). Подключается в api_v5 через
include_router; сам ничего не считает — зовёт mission.* и store_v5.

POST /api/v5/mission/start     {ticker, play, deposit?}   → mission.start
POST /api/v5/mission/stop      {ticker}                   → пилот стоп (позиция под тросом)
POST /api/v5/mission/resume    {ticker}                   → поднять пилот с сохранённым планом/позицией
POST /api/v5/mission/panic     {ticker?, force?}          → закрыть всё и встать (force — по счёту без проверок)
POST /api/v5/mission/reanalyze {ticker, reason?}          → council_again
GET  /api/v5/mission/status?ticker=                       → status | {"active": false}
GET  /api/v5/mission/explain?ticker=                      → лента толмача и память миссии (v5.3)
GET  /api/v5/trades?ticker=&limit=                        → {"trades":[…],"summary":{…}}  (фаза 4 W1: нетто/комиссии/est)
GET  /api/v5/trades/day?date=YYYY-MM-DD                   → ledger.day_summary (итоги дня; без date — сегодня по МСК)
POST /api/v5/trades/sync       {ticker?}                  → ledger.sync_and_reconcile (ручная сверка с операциями)
"""
from __future__ import annotations

import logging
import math

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from . import ledger, mission, store_v5

log = logging.getLogger("pythia.api_mission")
router = APIRouter()


def _ticker(payload: dict | None) -> str:
    return str((payload or {}).get("ticker") or (payload or {}).get("code") or "").upper().strip()


def _err(msg: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg, "note": msg}, status_code=code)


@router.post("/api/v5/mission/start")
async def api_mission_start(payload: dict):
    payload = payload or {}
    t = _ticker(payload)
    if not t:
        return _err("нужен ticker")
    play = str(payload.get("play") or "auto").lower().strip()
    if play not in mission.PLAY:
        return _err(f"режим игры {play!r} неизвестен — нужен один из {', '.join(mission.PLAY)}")
    dep = payload.get("deposit")
    try:
        if isinstance(dep, bool):
            return _err("deposit — число")
        dep = float(dep) if dep not in (None, "", 0, "0") else None
        if dep is not None and (not math.isfinite(dep) or dep <= 0):
            return _err("deposit должен быть конечным числом > 0")
    except (TypeError, ValueError, OverflowError):
        return _err("deposit — число")
    try:
        r = await mission.start(t, play, deposit=dep, reason=str(payload.get("reason") or "по кнопке"))
    except ValueError as e:
        return _err(str(e))
    except Exception as e:                           # noqa: BLE001
        log.warning("mission/start %s: %s", t, str(e)[:120])
        return _err(f"миссия не стартовала: {str(e)[:150]}", 500)
    if not r.get("ok"):
        return JSONResponse(r, status_code=409)
    return r


@router.post("/api/v5/mission/stop")
async def api_mission_stop(payload: dict):
    t = _ticker(payload)
    if not t:
        return _err("нужен ticker")
    return await mission.stop(t)


@router.post("/api/v5/mission/resume")
async def api_mission_resume(payload: dict):
    t = _ticker(payload)
    if not t:
        return _err("нужен ticker")
    try:
        r = await mission.resume(t)
    except RuntimeError as e:
        return _err(str(e)[:200], 503)
    return r if r.get("ok") else JSONResponse(r, status_code=409)


@router.post("/api/v5/mission/panic")
async def api_mission_panic(payload: dict | None = None):
    t = _ticker(payload) or None
    force = bool((payload or {}).get("force"))          # W4: закрыть по счёту без проверки сохранённой заявки/стопов
    try:
        return await mission.panic(t, force=force)
    except RuntimeError as e:
        return _err(str(e)[:200], 503)


@router.post("/api/v5/mission/reanalyze")
async def api_mission_reanalyze(payload: dict):
    t = _ticker(payload)
    if not t:
        return _err("нужен ticker")
    reason = str((payload or {}).get("reason") or "пересмотр по кнопке")
    try:
        r = await mission.council_again(t, reason)
    except RuntimeError as e:
        return _err(str(e)[:200], 503)
    return r if r.get("ok") else JSONResponse(r, status_code=409)


@router.get("/api/v5/mission/status")
async def api_mission_status(ticker: str = ""):
    t = (ticker or "").upper().strip()
    if not t:
        snap = mission.snapshot()
        act = snap.get("active")
        return {"active": act or False, **snap}
    st = mission.status(t)
    if not st:
        return {"active": False, "ticker": t}
    return st


@router.get("/api/v5/mission/explain")
async def api_mission_explain(ticker: str = ""):
    """Толмач (v5.3): объяснения владельцу по ключевым узлам и память миссии
    {ok, ticker, active, items:[{ts, kind, title, text, refs, ok, model}], memory:{text, ts, n, limit}, enabled, model, pending}."""
    t = (ticker or "").upper().strip()
    if not t:
        snap = mission.snapshot()
        t = snap.get("active") or ""
        if not t:
            return {"ok": False, "ticker": None, "active": False, "items": [], "memory": None, "note": "активной миссии нет"}
    return mission.explain_list(t)


@router.get("/api/v5/trades")
async def api_trades(ticker: str = "", limit: int = 200):
    t = (ticker or "").upper().strip() or None
    limit = max(1, min(int(limit or 200), 1000))
    return {"trades": store_v5.trades(t, limit), "summary": ledger.trades_summary(t)}


@router.get("/api/v5/trades/day")
async def api_trades_day(date: str = ""):
    """Итоги дня (фаза 4 · W1): {ok, day, count, net, gross, fee, wins, losses, best, worst, trades, est,
    session_pnl, pilot_ticker, account:{total, cash, note}, mode, last_sync, note}; кривая дата → 400."""
    r = await ledger.day_summary(date.strip() or None)
    return r if r.get("ok") else _err(r.get("note") or "дата — YYYY-MM-DD")


@router.post("/api/v5/trades/sync")
async def api_trades_sync(payload: dict | None = None):
    """Ручная сверка журнала с операциями брокера: {ok, mode, sync, reconcile, note, last_sync}.
    Без операций (est) — ok False и честный note, не ошибка HTTP."""
    t = _ticker(payload) or None
    try:
        return await ledger.sync_and_reconcile(t)
    except Exception as e:                           # noqa: BLE001
        log.warning("trades/sync: %s", str(e)[:120])
        return _err(f"сверка не удалась: {str(e)[:150]}", 500)


# ── сканер стакана («8 ч» из 4.x) ─────────────────────────────────────────────
@router.post("/api/v5/mission/scan")
async def api_scan_start(payload: dict | None = None):
    """Запуск/продление онлайн-скана стакана по тикеру: {ticker, minutes?} (до 480)."""
    payload = payload or {}
    t = _ticker(payload)
    if not t:
        return _err("нужен ticker")
    mins = payload.get("minutes")
    try:
        mins = int(mins) if mins not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        return _err("minutes — число")
    r = await mission.scan_start(t, mins)
    return r if r.get("ok") else JSONResponse(r, status_code=409)


@router.delete("/api/v5/mission/scan")
async def api_scan_stop(ticker: str = ""):
    t = (ticker or "").upper().strip()
    if not t:
        return _err("нужен ticker")
    return mission.scan_stop(t)


@router.get("/api/v5/mission/scan")
async def api_scan_status(ticker: str = ""):
    t = (ticker or "").upper().strip()
    if not t:
        return _err("нужен ticker")
    return mission.scan_status(t)


if __name__ == "__main__":
    import asyncio

    paths = {r.path for r in router.routes}
    for p in ("/api/v5/mission/start", "/api/v5/mission/stop", "/api/v5/mission/resume",
              "/api/v5/mission/panic", "/api/v5/mission/reanalyze", "/api/v5/mission/status",
              "/api/v5/mission/explain", "/api/v5/trades", "/api/v5/trades/day", "/api/v5/trades/sync",
              "/api/v5/mission/scan"):
        assert p in paths, p
    # фаза 4 · W1: журнал — на временной базе, без сети (токен владельца в config не трогаем: ledger в режиме est)
    import os
    import tempfile
    import time
    from pathlib import Path
    from . import tinkoff
    store_v5.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
    store_v5._schema()
    os.environ["PYTHIA_DRY"] = "1"
    os.environ.pop("PYTHIA_MOCK_TINKOFF", None)
    tinkoff.enabled = lambda: False

    async def _no_net(*a, **k):
        raise AssertionError("self-тест не ходит в сеть")
    tinkoff._post = tinkoff.accounts = tinkoff.operations = tinkoff.portfolio = _no_net
    assert ledger.mode() == "est"
    scan_methods = {m for r in router.routes if getattr(r, "path", "") == "/api/v5/mission/scan"
                    for m in getattr(r, "methods", ())}
    assert scan_methods == {"POST", "DELETE", "GET"}, scan_methods

    async def main():
        r = await api_mission_start({"ticker": "SBER", "play": "flat"})
        assert r.status_code == 400
        r = await api_mission_start({"ticker": "", "play": "long"})
        assert r.status_code == 400
        r = await api_mission_start({"ticker": "SBER", "play": "long", "deposit": "abc"})
        assert r.status_code == 400
        r = await api_mission_stop({"ticker": "NOPE"})
        assert r["ok"] is False
        r = await api_mission_status("NOPE")
        assert r["active"] is False
        r = await api_mission_status("")
        assert "missions" in r
        r = await api_mission_explain("NOPE")
        assert r["ok"] is False and r["items"] == [] and "нет" in r["note"], r
        r = await api_mission_explain("")
        assert r["ok"] is False and r["active"] is False and "items" in r
        r = await api_trades("", 5)
        assert "trades" in r and "summary" in r and "count" in r["summary"]
        assert r["summary"]["mode"] == "est" and "net" in r["summary"] and "by_day" in r["summary"], r["summary"]
        # день: сделка сегодня (по МСК) → итоги дня с нетто по оценке; кривая дата → 400; без операций sync честен
        store_v5.trade_add({"ticker": "SBER", "side": "long", "lots": 1, "entry": 300.0, "exit_px": 302.0, "pnl": 20.0,
                            "opened_ts": time.time() - 600, "closed_ts": time.time() - 1, "why": "тейк", "mode": "dry",
                            "fee_est": 0.3})
        d = await api_trades_day("")
        assert d["ok"] and d["count"] == 1 and d["net"] == 19.7 and d["gross"] == 20.0 and d["fee"] == 0.3 and d["est"], d
        assert d["best"]["id"] == d["worst"]["id"] and d["account"]["total"] is None and "Tinkoff" in d["account"]["note"]
        assert d["trades"][0]["ticker"] == "SBER" and d["mode"] == "est" and d["day"] == ledger.day_bounds()[0]
        d2 = await api_trades_day("2001-01-01")
        assert d2["ok"] and d2["count"] == 0 and d2["day"] == "2001-01-01" and "нет" in d2["note"]
        d3 = await api_trades_day("вчера")
        assert d3.status_code == 400
        sy = await api_trades_sync({"ticker": "SBER"})
        assert sy["ok"] is False and sy["mode"] == "est" and "оценоч" in sy["note"] and sy["reconcile"]["est"] == 1, sy
        r = await api_trades("SBER", 5)
        assert r["trades"][0]["net"] == 19.7 and r["trades"][0]["est"] and r["summary"]["est"] is True
        r = await api_scan_start({"ticker": ""})
        assert r.status_code == 400
        r = await api_scan_start({"ticker": "SBER", "minutes": "abc"})
        assert r.status_code == 400
        r = await api_scan_status("NOPE")
        assert r["running"] is False
        r = await api_scan_stop("")
        assert r.status_code == 400

    asyncio.run(main())
    print("api_mission self-test OK")
