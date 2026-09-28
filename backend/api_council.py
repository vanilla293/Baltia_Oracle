# -*- coding: utf-8 -*-
"""ПИФИЯ v5 — API совета и дозора (§5 контракта). Подключается в api_v5/server.

Долгие прогоны — фоном (asyncio.create_task), один живой прогон на scope:
если уже идёт — 409. Ответ фонового запуска: {"ok": true, "run_id": "…"}.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from . import bus, council, newsflow, store_v5, watch

log = logging.getLogger("pythia.api_council")
router = APIRouter()

MAX_TEXT = 40_000
_TASKS: set[asyncio.Task] = set()
_TASK_SCOPES: dict[asyncio.Task, str] = {}


def _err(msg: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=code)


def _spawn(coro, name: str, scope: str = "") -> asyncio.Task:
    """Фоновая задача с логом исключения (прогон сам пишет ошибку в store/bus)."""
    task = asyncio.create_task(coro, name=name)
    _TASKS.add(task)
    _TASK_SCOPES[task] = scope

    def _done(t: asyncio.Task):
        _TASKS.discard(t)
        _TASK_SCOPES.pop(t, None)
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            return
        if exc:
            log.warning("%s: %s", name, str(exc)[:200])

    task.add_done_callback(_done)
    return task


async def _wait_run(scope: str, tries: int = 25) -> str | None:
    """Прогон создаёт run_id синхронно на первом шаге — ждём его появления в bus."""
    for _ in range(tries):
        a = bus.active(scope)
        if a:
            return a["run_id"]
        await asyncio.sleep(0.02)
    return None


def _busy() -> JSONResponse | None:
    # create_task ещё не исполнил start_run: резервируем запуск до первого await.
    if _pending("daily") or _pending("human"):
        return _err("совет уже запускается или идёт — дождись конца", 409)
    b = council._busy()
    return _err(b, 409) if b else None


def _pending(scope: str) -> bool:
    return any(sc == scope and not task.done() for task, sc in _TASK_SCOPES.items())


# ── совет ─────────────────────────────────────────────────────────────────
@router.post("/api/v5/council/daily")
async def api_daily(payload: dict | None = None):
    payload = payload or {}
    b = _busy()
    if b:
        return b
    days = payload.get("days")
    if days is not None:
        try:
            days = max(0.5, min(float(days), 14.0))
        except Exception:        # noqa: BLE001
            return _err("days: число дней 0.5..14")
    reason = str(payload.get("reason") or "по кнопке")[:200]
    task = _spawn(council.daily(days, reason), "совет daily", "daily")
    run_id = await _wait_run("daily")
    if task.done() and task.exception():
        return _err(str(task.exception())[:300], 500)
    return {"ok": True, "run_id": run_id, "days": days}


@router.post("/api/v5/council/rerun")
async def api_rerun(payload: dict | None = None):
    payload = payload or {}
    b = _busy()
    if b:
        return b
    reason = str(payload.get("reason") or "пересчёт по новым новостям")[:200]
    ids = [str(i) for i in (payload.get("ids") or []) if str(i).strip()]
    task = _spawn(council.update_with_news(ids, reason), "совет update", "daily")
    run_id = await _wait_run("daily")
    if task.done() and task.exception():
        return _err(str(task.exception())[:300], 500)
    return {"ok": True, "run_id": run_id}


@router.post("/api/v5/human")
async def api_human(payload: dict | None = None):
    payload = payload or {}
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return _err("text: нужен непустой текст")
    if len(text) > MAX_TEXT:
        return _err(f"text: слишком длинный ({len(text)} > {MAX_TEXT} символов)")
    b = _busy()
    if b:
        return b
    task = _spawn(council.human(text.strip()), "взгляд человека", "human")
    run_id = await _wait_run("human")
    if task.done() and task.exception():
        return _err(str(task.exception())[:300], 500)
    return {"ok": True, "run_id": run_id}


@router.get("/api/v5/council/latest")
async def api_latest(kind: str = "any"):
    k = None if kind in ("", "any", None) else kind
    if k and k not in ("daily", "human", "update"):
        return _err("kind: daily|human|update|any")
    row = store_v5.council_latest(k)
    if not row:
        return _err("совета ещё не было", 404)
    return row


@router.get("/api/v5/council/get")
async def api_get(run_id: str):
    row = store_v5.council_get((run_id or "").strip()) if run_id else None
    if not row:
        return _err("нет такого совета", 404)
    return row


@router.get("/api/v5/council/list")
async def api_list(limit: int = 20):
    return {"runs": store_v5.council_list(max(1, min(int(limit), 100)))}


# ── новости ───────────────────────────────────────────────────────────────
@router.get("/api/v5/news")
async def api_news(days: float = 3, relevant: int = 1, limit: int = 1000):
    days = max(0.1, min(float(days), 30.0))
    items = store_v5.news_window(days, relevant_only=bool(int(relevant)))
    items = items[: max(1, min(int(limit), 5000))]
    for x in items:
        x["sid"] = newsflow.sid(x["id"])
    return {"items": items, "stats": store_v5.news_stats(days)}


@router.get("/api/v5/news/item")
async def api_news_item(id: str):
    rows = store_v5.news_by_ids([id.strip()]) if id and id.strip() else []
    if not rows:
        return _err("нет такой новости", 404)
    x = rows[0]
    x["sid"] = newsflow.sid(x["id"])
    return x


@router.get("/api/v5/news/ticker")
async def api_news_ticker(ticker: str, name: str = "", days: float = 3, limit: int = 20):
    items = newsflow.news_for_ticker(ticker, name or ticker, days=days, limit=max(1, min(int(limit), 100)))
    for x in items:
        x["sid"] = newsflow.sid(x["id"])
    return {"items": items}


# ── дозор ─────────────────────────────────────────────────────────────────
@router.get("/api/v5/watch")
async def api_watch(limit: int = 30, unseen: int = 0):
    notes = store_v5.watch_list(max(1, min(int(limit), 200)), unseen_only=bool(int(unseen)))
    return {"notes": notes, "unseen": store_v5.watch_unseen_count(), "active": bus.active("watch")}


@router.post("/api/v5/watch/seen")
async def api_watch_seen(payload: dict | None = None):
    ids = []
    for i in (payload or {}).get("ids") or []:
        try:
            ids.append(int(i))
        except Exception:        # noqa: BLE001
            continue
    if not ids and (payload or {}).get("all"):
        ids = [n["id"] for n in store_v5.watch_list(500, unseen_only=True)]
    store_v5.watch_mark_seen(ids)
    return {"ok": True, "marked": len(ids), "unseen": store_v5.watch_unseen_count()}


@router.post("/api/v5/watch/tick")
async def api_watch_tick():
    if _pending("watch") or bus.active("watch"):
        return _err("дозор уже идёт", 409)
    b = _busy()
    if b:
        return _err("идёт совет — дозор подождёт его конца", 409)
    task = _spawn(watch.tick(force=True), "дозор по кнопке", "watch")
    run_id = await _wait_run("watch")
    if task.done() and task.exception():
        return _err(str(task.exception())[:300], 500)
    return {"ok": True, "run_id": run_id}


# ── self-test ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import tempfile
    import time
    from pathlib import Path

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    store_v5.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
    store_v5._schema()

    calls: list = []

    async def fake_daily(days=None, reason="по кнопке"):
        rid = bus.start_run("daily", {"days": days, "reason": reason})
        calls.append(("daily", days, reason))
        await asyncio.sleep(0.05)
        store_v5.council_put(rid, "daily", {"summary": {"regime": "risk-on", "picks": [{"ticker": "SBER"}]}})
        bus.end_run(rid)
        return {}

    async def fake_human(text):
        rid = bus.start_run("human", {})
        calls.append(("human", text))
        await asyncio.sleep(0.05)
        bus.end_run(rid)
        return {}

    async def fake_update(ids, reason):
        rid = bus.start_run("daily", {"kind": "update"})
        calls.append(("update", ids, reason))
        await asyncio.sleep(0.02)
        bus.end_run(rid, error="нечего")
        raise RuntimeError("нечего")

    async def fake_tick(force=False):
        rid = bus.start_run("watch", {})
        calls.append(("tick", force))
        await asyncio.sleep(0.02)
        bus.end_run(rid)
        return None

    council.daily = fake_daily
    council.human = fake_human
    council.update_with_news = fake_update
    watch.tick = fake_tick

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        r = c.post("/api/v5/council/daily", json={"days": 2, "reason": "тест"})
        assert r.status_code == 200 and r.json()["ok"] and r.json()["run_id"].startswith("daily-"), r.text
        assert c.post("/api/v5/council/daily", json={}).status_code == 409
        assert c.post("/api/v5/human", json={"text": "x"}).status_code == 409
        assert c.post("/api/v5/watch/tick").status_code == 409
        time.sleep(0.15)
        assert calls[0] == ("daily", 2.0, "тест") and bus.active("daily") is None
        assert c.get("/api/v5/council/latest").json()["kind"] == "daily"
        assert c.get("/api/v5/council/latest?kind=human").status_code == 404
        assert c.get("/api/v5/council/latest?kind=xx").status_code == 400
        assert c.get("/api/v5/council/list").json()["runs"][0]["picks"] == ["SBER"]
        rid0 = c.get("/api/v5/council/list").json()["runs"][0]["run_id"]
        assert c.get(f"/api/v5/council/get?run_id={rid0}").json()["data"]["summary"]["regime"] == "risk-on"
        assert c.get("/api/v5/council/get?run_id=nope").status_code == 404
        assert c.post("/api/v5/human", json={"text": "   "}).status_code == 400
        assert c.post("/api/v5/human", json={"text": "x" * (MAX_TEXT + 1)}).status_code == 400
        r = c.post("/api/v5/human", json={"text": " мой взгляд "})
        assert r.status_code == 200 and r.json()["run_id"].startswith("human-")
        time.sleep(0.15)
        assert ("human", "мой взгляд") in calls
        r = c.post("/api/v5/council/rerun", json={"reason": "r"})
        assert r.status_code == 200 and r.json()["run_id"].startswith("daily-")
        time.sleep(0.1)
        assert ("update", [], "r") in calls and bus.active("daily") is None
        # новости и дозор
        store_v5.news_upsert_raw([{"source": "s", "title": "t", "link": "http://a", "ts": time.time()}])
        nid = store_v5.news_untriaged(1)[0]["id"]
        store_v5.news_set_relevant([nid], 1)
        j = c.get("/api/v5/news?days=1&relevant=1&limit=10").json()
        assert j["items"][0]["sid"] == nid[:6] and j["stats"]["relevant"] == 1
        assert c.get(f"/api/v5/news/item?id={nid[:6]}").json()["id"] == nid
        assert c.get("/api/v5/news/item?id=nope").status_code == 404
        assert c.get("/api/v5/news/ticker?ticker=SBER").json()["items"] == []
        wid = store_v5.watch_add({"severity": 80, "note": "n"})
        j = c.get("/api/v5/watch").json()
        assert j["unseen"] == 1 and j["notes"][0]["id"] == wid
        assert c.post("/api/v5/watch/seen", json={"ids": [wid, "x"]}).json()["unseen"] == 0
        r = c.post("/api/v5/watch/tick")
        assert r.status_code == 200 and r.json()["run_id"].startswith("watch-")
        time.sleep(0.1)
        assert ("tick", True) in calls
    print("api_council self-test OK")
