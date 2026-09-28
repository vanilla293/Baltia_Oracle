# -*- coding: utf-8 -*-
"""ПИФИЯ v5 — дозор новостей (фон, каждые config.PYTHIA_WATCH_SEC).

tick: свежие новости (1 день) → FLASH отбор только новых → FLASH разметка →
FLASH(think) «меняет ли это что-то радикально» → заметка в store.
v5.1: дозорный видит новые новости все, важные — со строкой «факты: … · суть: …»
(`newsflow.render(rich=True)`); блоки итог/новости идут через `compress.fit` —
выше разумных пределов FLASH сжимает, ниже — как есть; стадия «impact» несёт
размер промпта в символах.
Серьёзная (severity ≥ PYTHIA_WATCH_SERIOUS) → mission.on_serious_news(note).
Пока идёт совет (daily/human) — тик пропускается, чтобы не конкурировать за
ключ. Пустой прогон (нет новых релевантных) заметку не создаёт.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import time

from . import ai, ai_v5, bus, compress, config, council, newsflow, prompts_council as P, store_v5

log = logging.getLogger("pythia.watch")

_last_purge = 0.0
_KV_LAST = "watch_last_end"
IMPACT_TIMEOUT = 480.0        # v5.3 W2: FLASH(think) дозора обязан ответить за N с — иначе тик дозора падает честно,
                              # а не висит до клиентского таймаута (600 с × 3 попытки) и не держит следующие проходы


def _cutoff(start: float) -> float:
    """Новые = добавленные после: старт тика−1с, конца прошлого тика, последнего совета
    (что совет уже видел — не «новое»)."""
    cut = start - 1
    try:
        cut = max(cut, float(store_v5.kv_get(_KV_LAST, 0) or 0))
    except Exception:            # noqa: BLE001
        pass
    try:
        lt = council.latest()
        if lt:
            cut = max(cut, float(lt.get("ts") or 0))
    except Exception:            # noqa: BLE001
        pass
    return cut


def _mark_end() -> None:
    try:
        store_v5.kv_set(_KV_LAST, time.time())
    except Exception:            # noqa: BLE001
        pass


def _hum(e: Exception) -> str:
    try:
        return ai.humanize_error(e)
    except Exception:            # noqa: BLE001
        return str(e)[:200]


def _serious_level() -> int:
    return int(getattr(config, "PYTHIA_WATCH_SERIOUS", 75))


def _norm_impact(obj, new_items: list[dict]) -> dict:
    d = obj if isinstance(obj, dict) else {}
    sev = newsflow._int(d.get("severity"), 0, 100)
    changes = d.get("changes")
    if isinstance(changes, str):
        changes = changes.strip().lower() in ("true", "да", "1", "yes")
    rerun = d.get("recommend_rerun")
    if isinstance(rerun, str):
        rerun = rerun.strip().lower() in ("true", "да", "1", "yes")
    note = str(d.get("note") or "").strip()
    if not note:
        note = "Новые новости: " + "; ".join(
            (x.get("ai") or {}).get("one_liner") or x.get("title") or "" for x in new_items[:3])
    return {"changes": bool(changes) if changes is not None else sev >= 50, "severity": sev,
            "note": note[:1200], "affected": newsflow._strs(d.get("affected"), limit=12, up=True),
            "recommend_rerun": bool(rerun) if rerun is not None else sev >= _serious_level()}


async def impact(run_id: str, new_items: list[dict]) -> dict:
    """FLASH(think=True): итог совета + новые новости (все; важные — с фактами) →
    {"changes","severity","note","affected","recommend_rerun"}. Блоки через compress.fit;
    стадия «impact» start — размеры промпта в символах."""
    try:
        prev = council.summary_text(council.latest())
    except Exception as e:       # noqa: BLE001
        log.info("impact: итог совета: %s", str(e)[:100])
        prev = "совета ещё не было"
    blocks = await compress.fit({"итог": prev, "новости": newsflow.render(new_items, limit=None, rich=True)})
    s, u = P.impact(blocks["итог"], blocks["новости"])
    try:
        await bus.stage(newsflow.scope_of(run_id), run_id, "impact", "start", n=len(new_items),
                        detail=council._sizes(f"дозор ({len(new_items)} новых)", blocks, u))
    except Exception:            # noqa: BLE001
        pass
    obj = await asyncio.wait_for(ai_v5.flash_json(s, u, think=True, route="impact"), IMPACT_TIMEOUT)
    return _norm_impact(obj, new_items)


async def _notify_mission(note: dict) -> None:
    try:
        mission = importlib.import_module("backend.mission")
    except Exception:            # noqa: BLE001
        try:
            mission = importlib.import_module(".mission", package=__package__)
        except Exception as e:   # noqa: BLE001
            log.info("mission недоступна — серьёзная новость не передана: %s", str(e)[:80])
            return
    try:
        await mission.on_serious_news(note)
    except Exception as e:       # noqa: BLE001
        log.warning("mission.on_serious_news: %s", str(e)[:120])


async def tick(force: bool = False) -> dict | None:
    """Один проход дозора. Возврат: заметка или None (нечего/пропущено)."""
    if bus.active("watch"):
        return None
    if council._busy():
        log.info("дозор: совет идёт — тик пропущен")
        return None
    scope = "watch"
    run_id = bus.start_run(scope, {"force": force})
    t0 = _cutoff(time.time())
    try:
        await bus.stage(scope, run_id, "collect", "start", detail="свежие ленты")
        n_new = await newsflow.collect(1)
        try:
            new_items = store_v5.news_since(t0, relevant_only=False)
        except Exception as e:   # noqa: BLE001
            log.warning("дозор: news_since: %s", str(e)[:100])
            new_items = []
        await bus.stage(scope, run_id, "collect", "done", n=len(new_items), detail=f"новых {len(new_items)}")
        if not new_items:
            _mark_end()
            bus.end_run(run_id)
            return None
        await newsflow.triage(run_id, 1, items=[x for x in new_items if x.get("relevant") is None])
        ids = [x["id"] for x in new_items]
        rel = [x for x in store_v5.news_by_ids(ids) if x.get("relevant") == 1]
        await newsflow.characterize_all(run_id, 1, items=[x for x in rel if not x.get("ai")])
        rel_items = [x for x in store_v5.news_by_ids([x["id"] for x in rel]) if x.get("ai")]
        if not rel_items:
            await bus.stage(scope, run_id, "impact", "done", n=0, detail="биржевого среди новых нет")
            _mark_end()
            bus.end_run(run_id)
            return None

        imp = await impact(run_id, rel_items)          # стадия «impact» start с размерами — внутри
        note = {"ts": time.time(), "n_new": max(n_new, len(new_items)), "n_relevant": len(rel_items),
                "ids": [x["id"] for x in rel_items], "run_id": run_id, **imp, "items": rel_items}
        try:
            note["id"] = store_v5.watch_add(note)
        except Exception as e:   # noqa: BLE001
            log.warning("дозор: watch_add: %s", str(e)[:100])
        await bus.stage(scope, run_id, "impact", "done", n=len(rel_items),
                        detail=f"серьёзность {imp['severity']}: {imp['note'][:200]}",
                        data={k: v for k, v in note.items() if k != "items"})
        if imp["severity"] >= _serious_level():
            log.info("дозор: серьёзная новость (%d) → миссия", imp["severity"])
            await _notify_mission(note)
        _mark_end()
        bus.end_run(run_id)
        return note
    except asyncio.CancelledError:
        bus.end_run(run_id, status="cancelled")
        raise
    except Exception as e:       # noqa: BLE001
        msg = _hum(e)
        log.warning("дозор: %s", msg)
        cur = (bus.run(run_id) or {}).get("stage") or "collect"
        await bus.stage(scope, run_id, cur, "error", detail=msg)
        bus.end_run(run_id, error=msg)
        return None


def serious_since(ts: float) -> list[dict]:
    """Заметки с severity ≥ порога после ts (новые первыми)."""
    lvl = _serious_level()
    try:
        rows = store_v5.watch_list(100)
    except Exception as e:       # noqa: BLE001
        log.info("serious_since: %s", str(e)[:100])
        return []
    out = []
    for r in rows:
        d = r.get("data") or {}
        if float(r.get("ts") or 0) > float(ts or 0) and int(d.get("severity") or 0) >= lvl:
            out.append({"id": r.get("id"), "ts": r.get("ts"), "seen": r.get("seen"),
                        **{k: v for k, v in d.items() if k != "items"}})
    return out


async def loop() -> None:
    """Бесконечно: первый tick сразу, дальше каждые PYTHIA_WATCH_SEC. Нет ключа — тихо ждём."""
    global _last_purge
    while True:
        try:
            if not ai_v5.has_key():
                await asyncio.sleep(60)
                continue
            if bus.active("watch") is None:
                await tick()
            if time.time() - _last_purge > 6 * 3600:
                _last_purge = time.time()
                try:
                    store_v5.news_purge(14)
                except Exception:    # noqa: BLE001
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as e:   # noqa: BLE001
            log.warning("дозор: цикл: %s", str(e)[:120])
        await asyncio.sleep(max(120, int(getattr(config, "PYTHIA_WATCH_SEC", 900))))


# ── self-test ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import re
    import sys
    import tempfile
    import types
    from pathlib import Path

    store_v5.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
    store_v5._schema()

    class FakeAI:
        SEM_FLASH = asyncio.Semaphore(4)
        now_msk_str = staticmethod(ai_v5.now_msk_str)
        fmt_ts = staticmethod(ai_v5.fmt_ts)
        severity = 90

        def has_key(self):
            return True

        async def flash_json(self, system, user, *, think=False, route="flash", max_tokens=None):
            ids = re.findall(r"^\[(\w{6})\]", user, flags=re.M)
            if route == "triage":
                return {"keep": [i for i in ids
                                 if "спорт" not in user.split(f"[{i}]")[1].split("\n")[0].lower()]}
            if route == "characterize":
                return {"one_liner": "ЦБ поднял ставку до 25%", "tone": -70, "honesty": 90, "hype": 10,
                        "importance": 95, "assets": ["рубль", "SBER"], "kind": "факт", "horizon": "дни",
                        "expected_effect": "вниз", "effect_strength": 80, "gist": "Ставка 25% с 20.09.",
                        "key_facts": ["ставка 25%", "с 20.09", "ЦБ"]}
            if route == "impact":
                assert think and "ИТОГ СОВЕТА" in user and "НОВЫЕ НОВОСТИ" in user
                self.impact_user = user
                return {"changes": "true", "severity": str(self.severity), "note": "всё меняется",
                        "affected": ["sber", "рубль"], "recommend_rerun": True}
            return {}

        async def flash_text(self, system, user, **kw):        # compress.shrink: «сжимает» к лимиту
            lim = int(user.split("ЛИМИТ: ")[1].split(" ")[0])
            return user.split("\n\nТЕКСТ:\n", 1)[1][:lim]

    fake = FakeAI()
    ai_v5 = fake  # noqa: F811
    newsflow.ai_v5 = fake
    compress.ai_v5 = fake
    events: list[dict] = []

    async def sink(ev):
        events.append(ev)

    bus.set_sink(sink)

    got_serious: list[dict] = []
    mm = types.ModuleType("backend.mission")

    async def on_serious_news(note):
        got_serious.append(note)

    mm.on_serious_news = on_serious_news
    sys.modules["backend.mission"] = mm

    feed: list = []

    class N:
        def __init__(self, title, ts):
            self.source, self.title, self.summary, self.link = "tass", title, "т", "http://x/" + title
            self.published, self.ts = "", ts

        def to_dict(self):
            return {"source": self.source, "title": self.title, "summary": self.summary,
                    "link": self.link, "published": self.published}

    async def fake_fetch(*, force=False, days=None):
        return list(feed)

    newsflow.news.fetch_news = fake_fetch

    async def main():
        # пустой прогон — заметки нет
        assert await tick() is None and store_v5.watch_unseen_count() == 0
        # только мусор — заметки нет, но новость отобрана как нерелевантная
        feed.append(N("Спорт: матч", time.time()))
        assert await tick() is None
        assert store_v5.news_stats(1)["untriaged"] == 0 and store_v5.news_stats(1)["relevant"] == 0
        # серьёзная новость → заметка + миссия
        feed.append(N("ЦБ поднял ставку", time.time()))
        note = await tick(force=True)
        assert note and note["severity"] == 90 and note["changes"] is True and note["n_relevant"] == 1
        assert note["affected"] == ["SBER", "рубль"] and note["items"][0]["ai"]["tone"] == -70
        assert store_v5.watch_unseen_count() == 1 and len(got_serious) == 1 and got_serious[0]["id"] == note["id"]
        # дозорный видит важную новость со строкой «факты: … · суть: …», стадия impact — размеры промпта
        assert "\n    факты: ставка 25%; с 20.09; ЦБ · суть: Ставка 25% с 20.09." in fake.impact_user, fake.impact_user
        st_imp = [e for e in events if e.get("run_id") == note["run_id"] and e["stage"] == "impact"]
        assert st_imp[0]["status"] == "start" and "симв." in st_imp[0]["detail"] and "новости" in st_imp[0]["detail"], st_imp
        assert st_imp[-1]["status"] == "done"
        # огромный итог совета → блок ужат FLASH (без «обрезано»), дозор не падает
        real_latest = council.latest
        council.latest = lambda: {"data": {"summary": {"regime": "risk-on", "summary": "с " * 50_000}}}
        feed.append(N("ЦБ: ещё одно решение", time.time()))
        saved_lim, config.PYTHIA_CTX_LIMIT = config.PYTHIA_CTX_LIMIT, 60_000   # порог на время проверки: боевой 200 000 (24.09.2026)
        try:
            n_big = await tick()
        finally:
            config.PYTHIA_CTX_LIMIT = saved_lim
        council.latest = real_latest
        assert n_big and "обрезано" not in fake.impact_user
        prev_block = fake.impact_user.split("ИТОГ СОВЕТА:\n", 1)[1].split("\n\nНОВЫЕ НОВОСТИ:")[0]
        assert len(prev_block) <= 60_000 and prev_block.startswith("Режим: risk-on"), len(prev_block)
        got_serious.clear()
        assert serious_since(0)[0]["severity"] == 90 and "items" not in serious_since(0)[0]
        assert not serious_since(time.time() + 1) and bus.active("watch") is None
        # повторный тик: новых нет → None, миссию не дёргаем
        assert await tick() is None and len(got_serious) == 0
        # ниже порога → заметка есть, миссию не зовём
        fake.severity = 40
        feed.append(N("Сбер отчитался", time.time()))
        n2 = await tick()
        assert n2 and n2["severity"] == 40 and len(got_serious) == 0 and store_v5.watch_unseen_count() == 3
        assert len(serious_since(0)) == 2
        # совет идёт → тик пропущен
        rid = bus.start_run("daily")
        feed.append(N("ЦБ ещё", time.time()))
        assert await tick() is None
        bus.end_run(rid)
        # сбой ИИ в impact → без заметки, прогон закрыт с ошибкой
        async def boom(*a, **k):
            raise RuntimeError("сеть")
        feed.append(N("Нефть растёт", time.time()))
        fake.flash_json = boom
        assert await tick() is None and bus.active("watch") is None
        assert store_v5.watch_unseen_count() == 3

    asyncio.run(main())
    print("watch self-test OK")
