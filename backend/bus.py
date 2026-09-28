# -*- coding: utf-8 -*-
"""ПИФИЯ v5 — шина событий и реестр живых прогонов.

Сервер вешает sink (HUB.broadcast); модули шлют события стадий и дельты
текста. Реестр копит полный текст каждой стадии — вкладка, открытая
посередине, забирает снимок через /api/v5/run.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Awaitable, Callable

_sink: Callable[[dict], Awaitable[None]] | None = None
_RUNS: dict[str, dict] = {}
_KEEP = 40            # сколько завершённых прогонов держим в памяти
_TEXT_CAP = 1_500_000   # потолок текста одной стадии (24.09.2026: ответы до 393K токенов)


def set_sink(fn: Callable[[dict], Awaitable[None]] | None) -> None:
    global _sink
    _sink = fn


async def emit(ev: dict) -> None:
    if _sink is None:
        return
    try:
        await _sink(ev)
    except Exception:   # noqa: BLE001 — шина никогда не роняет стадию
        pass


def _gc() -> None:
    done = [k for k, v in _RUNS.items() if v.get("ended")]
    if len(done) > _KEEP:
        done.sort(key=lambda k: _RUNS[k].get("ended") or 0)
        for k in done[: len(done) - _KEEP]:
            _RUNS.pop(k, None)


def start_run(scope: str, meta: dict | None = None) -> str:
    run_id = f"{scope}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    _RUNS[run_id] = {"run_id": run_id, "scope": scope, "meta": dict(meta or {}),
                     "stage": "start", "status": "start", "started": time.time(),
                     "ended": None, "error": None, "detail": "",
                     "texts": {}, "thinks": {}, "counters": {}}
    _gc()
    return run_id


async def stage(scope: str, run_id: str, stage_name: str, status: str, **kw: Any) -> None:
    r = _RUNS.get(run_id)
    if r is not None and not r.get("ended"):         # поздние стадии (перепроверка миссии) не портят снимок завершённого прогона
        r["stage"] = stage_name
        r["status"] = status
        if "detail" in kw:
            r["detail"] = str(kw["detail"])[:400]
        if "n" in kw or "total" in kw:
            r["counters"][stage_name] = {"n": kw.get("n"), "total": kw.get("total")}
    ev = {"type": "v5", "scope": scope, "run_id": run_id,
          "stage": stage_name, "status": status}
    ev.update({k: v for k, v in kw.items() if v is not None})
    await emit(ev)


async def text(scope: str, run_id: str, stage_name: str, delta: str,
               kind: str = "text") -> None:
    if not delta:
        return
    r = _RUNS.get(run_id)
    if r is not None:
        bucket = r["texts"] if kind == "text" else r["thinks"]
        cur = bucket.get(stage_name, "")
        if len(cur) < _TEXT_CAP:
            bucket[stage_name] = cur + delta
    await emit({"type": kind if kind in ("text", "think") else "text",
                "scope": scope, "run_id": run_id, "stage": stage_name, "delta": delta})


def set_text(run_id: str, stage_name: str, full: str, kind: str = "text") -> None:
    """Положить готовый текст стадии целиком (без стрима)."""
    r = _RUNS.get(run_id)
    if r is not None:
        (r["texts"] if kind == "text" else r["thinks"])[stage_name] = (full or "")[:_TEXT_CAP]


def end_run(run_id: str, status: str = "done", error: str | None = None) -> None:
    r = _RUNS.get(run_id)
    if r is None:
        return
    r["status"] = status
    r["ended"] = time.time()
    if error:
        r["error"] = str(error)[:400]
        r["status"] = "error"
    _gc()
    # событие конца прогона — фронт узнаёт о завершении сразу, а не по опросу
    ev = {"type": "v5", "scope": r["scope"], "run_id": run_id, "stage": "end", "status": r["status"]}
    if r.get("error"):
        ev["error"] = r["error"]
    try:
        import asyncio
        asyncio.get_running_loop().create_task(emit(ev))
    except RuntimeError:      # вне цикла событий (self-тесты, синхронный код) — молча
        pass


def run(run_id: str) -> dict | None:
    r = _RUNS.get(run_id)
    return dict(r) if r else None


def _brief(r: dict) -> dict:
    return {k: r[k] for k in ("run_id", "scope", "meta", "stage", "status",
                              "started", "ended", "error", "detail", "counters")}


def running() -> dict:
    return {k: _brief(v) for k, v in _RUNS.items() if not v.get("ended")}


def recent(limit: int = 10) -> list[dict]:
    rs = sorted(_RUNS.values(), key=lambda v: v["started"], reverse=True)
    return [_brief(r) for r in rs[:limit]]


def active(scope: str) -> dict | None:
    for v in _RUNS.values():
        if v["scope"] == scope and not v.get("ended"):
            return _brief(v)
    return None


if __name__ == "__main__":
    import asyncio

    got = []

    async def sink(ev):
        got.append(ev)

    async def main():
        set_sink(sink)
        rid = start_run("daily", {"days": 3})
        assert active("daily")["run_id"] == rid
        await stage("daily", rid, "collect", "start", detail="rss")
        await text("daily", rid, "analysis", "abc")
        await text("daily", rid, "analysis", "def", kind="think")
        assert run(rid)["texts"]["analysis"] == "abc"
        assert run(rid)["thinks"]["analysis"] == "def"
        end_run(rid)
        assert active("daily") is None and not running()
        assert [e["type"] for e in got] == ["v5", "text", "think"]
        await asyncio.sleep(0)
        assert got[-1]["stage"] == "end" and got[-1]["status"] == "done" and got[-1]["run_id"] == rid
        rid2 = start_run("watch")
        end_run(rid2, error="сеть")
        await asyncio.sleep(0)
        assert got[-1]["stage"] == "end" and got[-1]["status"] == "error" and got[-1]["error"] == "сеть"

    asyncio.run(main())
    print("bus self-test OK")
