"""Идеи: сохранение с оценкой, поиск по-русски, заметки и статусы, глубокий разбор в фоне."""
from __future__ import annotations

import json
import sys
import pytest

from oracle import timeutil
from oracle.llm import LLMError
from oracle.tools import base as tb
from oracle.tools import ideas as ide
from oracle.tools import memory as mem

REM = "oracle.tools.reminders"

DEEP_TEXT = """1. Суть: кофе навынос для пассажиров электричек у вокзала.
2. Боль: утренняя спешка, реальная, но за неё платят копейки.
5. Что убьёт: аренда, сетевые конкуренты, сезонность.
8. **Оценка: 6/10** — трафик есть, маржа тонкая.
9. **Вердикт:** докрутить — сначала проверить трафик кофе-тележкой."""


async def call(ctx, tool_name: str, /, **args) -> dict:
    return json.loads(await tb.dispatch(tool_name, args, ctx))


async def ok(ctx, tool_name: str, /, **args) -> dict:
    r = await call(ctx, tool_name, **args)
    assert r.get("ok") is True, r
    return r


async def save_coffee(ctx, **kw) -> dict:
    args = dict(title="Кофейня у вокзала", content="Кофе навынос для пассажиров электричек, утром и вечером.",
                evaluation="Трафик есть, но аренда съест маржу. Первый шаг — посчитать поток.", score=5,
                tags=["Общепит", "#офлайн"])
    args.update(kw)
    return await ok(ctx, "save_idea", **args)


# ── помощники ────────────────────────────────────────────────────────────────
def test_clean_helpers():
    assert ide.clean_score(7) == 7
    assert ide.clean_score("7/10") == 7
    assert ide.clean_score("6,5") == 7
    assert ide.clean_score(15) == 10
    assert ide.clean_score(0) == 1
    assert ide.clean_score(-3) == 1
    for bad in ("много", None, True, "7 баллов"):
        with pytest.raises(ValueError):
            ide.clean_score(bad)
    assert ide.clean_tags(["Общепит", "#офлайн", "общепит", " ", "Кофе  Навынос"]) == "общепит, офлайн, кофе навынос"
    assert ide.clean_tags("бизнес; Кофе,бизнес") == "бизнес, кофе"
    assert ide.clean_tags(None) == "" and ide.clean_tags("none") == ""
    assert ide.clean_status("в работу") == "in_work"
    assert ide.clean_status("In-Work") == "in_work"
    assert ide.clean_status("в стол") == "parked"
    assert ide.clean_status("все", allow_all=True) == "all"
    with pytest.raises(ValueError):
        ide.clean_status("летит")
    with pytest.raises(ValueError):
        ide.clean_status("all")


def test_parse_score_and_verdict():
    assert ide.parse_score(DEEP_TEXT) == 6
    assert ide.parse_score("Оценка — 7 из 10") == 7
    assert ide.parse_score("оценку ставлю 4,5/10") == 5
    assert ide.parse_score("вначале думал 8/10, итог: Оценка: 3/10") == 3
    assert ide.parse_score("итог: 7/10") == 7                     # без слова «оценка» — последнее N/10
    assert ide.parse_score("Оценка: 15/10") is None
    assert ide.parse_score("без чисел") is None
    assert ide.parse_score("") is None
    assert ide.parse_verdict(DEEP_TEXT) == "докрутить — сначала проверить трафик кофе-тележкой"
    assert ide.parse_verdict("Вердикт: в стол.") == "в стол"
    assert ide.parse_verdict("нет вердикта") is None


# ── сохранение и поиск ───────────────────────────────────────────────────────
async def test_save_idea(ctx):
    r = await save_coffee(ctx)
    assert r["title"] == "Кофейня у вокзала" and r["score"] == 5 and r["tags"] == "общепит, офлайн"
    assert "similar" not in r
    row = await ide.load_idea(ctx.db, r["id"])
    assert row["status"] == "new" and row["evaluation"].startswith("Трафик есть")
    # кнопка «додумать» для владельца
    assert len(ctx.outbox) == 1 and ctx.outbox[0].buttons == [[("🧠 Додумать глубоко", f"idea:deep:{r['id']}")]]
    assert f"#{r['id']}" in ctx.outbox[0].text
    # индекс: название, текст, теги, оценка
    for q in ("кофейни", "электричка", "общепит", "маржа"):
        assert await ctx.db.search("idea", q) == [r["id"]], q
    # похожая идея — подсказка
    r2 = await save_coffee(ctx, title="Кофейня на колёсах у вокзала", score="12", tags="кофе")
    assert r2["score"] == 10 and [s["id"] for s in r2["similar"]] == [r["id"]]


async def test_save_idea_validation(ctx):
    for args in (dict(title=" ", content="x", evaluation="e", score=5),
                 dict(title="t", content="", evaluation="e", score=5)):
        bad = await call(ctx, "save_idea", **args)
        assert bad["ok"] is False
    bad = await call(ctx, "save_idea", title="Т", content="c", evaluation="e", score="отлично")
    assert bad["ok"] is False and "1 до 10" in bad["error"]
    bad = await call(ctx, "save_idea", title="Т", content="c", evaluation="e")
    assert bad["ok"] is False and "score" in bad["error"]
    long = await ok(ctx, "save_idea", title="Д" * 500, content="c", evaluation="e", score=4)
    assert len(long["title"]) == ide.TITLE_MAX
    assert await ctx.db.scalar("SELECT COUNT(*) FROM ideas") == 1


async def test_find_ideas_morphology(ctx):
    coffee = await save_coffee(ctx)
    gym = await ok(ctx, "save_idea", title="Приложение для учёта тренировок", content="Трекер подходов в зале",
                   evaluation="Рынок забит", score=3, tags=["приложение"])
    r = await ok(ctx, "find_ideas", query="найди мою идею про кофейню")
    assert r["items"][0]["id"] == coffee["id"] and r["items"][0]["match"] is True
    first = r["items"][0]
    assert first["created"] == "28.09.2026" and first["status"] == "new" and first["score"] == 5
    assert first["tags"] == "общепит, офлайн" and first["snippet"].startswith("Кофе навынос")
    assert "get_idea" in r["note"]
    # меньше трёх совпадений — добиваем свежими, помечая match=false
    assert {i["id"]: i["match"] for i in r["items"]} == {coffee["id"]: True, gym["id"]: False}
    r = await ok(ctx, "find_ideas", query="тренировки")
    assert r["items"][0]["id"] == gym["id"] and r["items"][0]["match"] is True


async def test_find_ideas_fallback_recent(ctx, clock):
    ids = []
    for i in range(12):
        clock.advance(minutes=1)
        ids.append((await ok(ctx, "save_idea", title=f"Идея номер {i}", content=f"Текст {i}",
                             evaluation="ок", score=5))["id"])
    r = await ok(ctx, "find_ideas", query="квантовый компьютер", limit=5)
    assert [i["id"] for i in r["items"]] == list(reversed(ids))[:5]
    assert all(i["match"] is False for i in r["items"]) and "точных совпадений нет" in r["note"]
    clock.advance(minutes=1)
    await ok(ctx, "update_idea", id=ids[0], content="x" * 500)
    r = await ok(ctx, "find_ideas", query="квант")
    assert len(r["items"]) == 8                                   # limit по умолчанию
    assert r["items"][0]["id"] == ids[0]                          # обновлённая — самая свежая
    assert len(r["items"][0]["snippet"]) == 200


async def test_find_ideas_empty_db(ctx):
    r = await ok(ctx, "find_ideas", query="кофейня")
    assert r["items"] == [] and "не сохранено" in r["note"]


# ── карточка, правка, список, удаление ───────────────────────────────────────
async def test_get_idea(ctx):
    r = await save_coffee(ctx)
    g = await ok(ctx, "get_idea", id=f"#{r['id']}")
    assert g["content"].startswith("Кофе навынос") and g["evaluation"].startswith("Трафик")
    assert g["deep_evaluation"] == "" and "deep_think_idea" in g["note"]
    assert g["status_ru"] == "новая" and g["created"] == "28.09.2026"
    bad = await call(ctx, "get_idea", id=999)
    assert bad["ok"] is False and "#999" in bad["error"]
    bad = await call(ctx, "get_idea", id="кофейня")
    assert bad["ok"] is False and "числом" in bad["error"]


async def test_update_idea_note_status_tags_reindex(ctx, clock):
    r = await save_coffee(ctx)
    clock.advance(days=2)
    u = await ok(ctx, "update_idea", id=r["id"], note="Можно продавать ещё и выпечку", status="в работу",
                 tags=["кофе", "Выпечка"])
    assert u["status"] == "in_work" and u["tags"] == "кофе, выпечка"
    assert set(u["changed"]) == {"note", "status", "tags"}
    row = await ide.load_idea(ctx.db, r["id"])
    assert row["content"].endswith("\n\n[30.09.2026] Можно продавать ещё и выпечку")
    assert row["content"].startswith("Кофе навынос")
    assert row["updated_at"] > row["created_at"]
    assert await ctx.db.search("idea", "выпечкой") == [r["id"]]
    assert await ctx.db.search("idea", "общепит") == []            # старые теги из индекса ушли
    u = await ok(ctx, "update_idea", id=r["id"], title="Кофе-тележка на перроне", score=7)
    assert u["title"] == "Кофе-тележка на перроне" and u["score"] == 7
    assert await ctx.db.search("idea", "тележку") == [r["id"]]
    u = await ok(ctx, "update_idea", id=r["id"], content="Новый текст целиком", tags=[])
    row = await ide.load_idea(ctx.db, r["id"])
    assert row["content"] == "Новый текст целиком" and row["tags"] == ""
    # пустые поля — «не менять»; совсем ничего — ошибка
    bad = await call(ctx, "update_idea", id=r["id"], title="", note="  ")
    assert bad["ok"] is False and "нечего менять" in bad["error"]
    bad = await call(ctx, "update_idea", id=r["id"], status="летает")
    assert bad["ok"] is False and "in_work" in bad["error"]
    bad = await call(ctx, "update_idea", id=12345, status="done")
    assert bad["ok"] is False


async def test_list_and_delete(ctx, clock):
    a = await save_coffee(ctx)
    clock.advance(minutes=1)
    b = await ok(ctx, "save_idea", title="Подкаст про историю", content="Раз в неделю", evaluation="ок", score=4)
    clock.advance(minutes=1)
    await ok(ctx, "update_idea", id=a["id"], status="parked")
    r = await ok(ctx, "list_ideas")
    assert [i["id"] for i in r["items"]] == [a["id"], b["id"]]          # свежие правки сверху
    assert r["counts"] == {"parked": 1, "new": 1} and "snippet" not in r["items"][0]
    r = await ok(ctx, "list_ideas", status="new")
    assert [i["id"] for i in r["items"]] == [b["id"]]
    r = await ok(ctx, "list_ideas", status="done")
    assert r["items"] == [] and "done" in r["note"]
    r = await ok(ctx, "list_ideas", status="all", limit=1)
    assert len(r["items"]) == 1
    d = await ok(ctx, "delete_idea", id=a["id"])
    assert d["deleted"] and d["title"] == "Кофейня у вокзала"
    assert await ide.load_idea(ctx.db, a["id"]) is None
    assert await ctx.db.search("idea", "кофейня") == []
    bad = await call(ctx, "delete_idea", id=a["id"])
    assert bad["ok"] is False


async def test_delete_idea_without_reminders_module(ctx, monkeypatch):
    a = await save_coffee(ctx)
    monkeypatch.setitem(sys.modules, REM, None)
    assert (await ok(ctx, "delete_idea", id=a["id"]))["deleted"]


def test_render_helpers(clock):
    row = {"id": 3, "title": "Кофейня", "score": 6, "status": "in_work", "tags": "общепит",
           "content": "Текст", "evaluation": "Норм", "deep_evaluation": "Разбор",
           "created_at": "2026-09-01T10:00:00+00:00"}
    assert ide.idea_line(row) == "#3 · Кофейня · 6/10 · в работе · общепит"
    card = ide.idea_card(row, timeutil.UTC)
    assert "🧠 Глубокий разбор:" in card and "Записана 01.09.2026" in card
    assert "без оценки" in ide.idea_line({**row, "score": None, "tags": ""})


# ── глубокий разбор ──────────────────────────────────────────────────────────
async def test_deep_evaluate_success(ctx, fake_llm, notifier):
    await mem.add_fact(ctx.db, "Работал бариста три года", "work")
    await mem.add_fact(ctx.db, "Живёт в Твери, рядом с вокзалом", "general")
    r = await save_coffee(ctx)
    fake_llm.script = [DEEP_TEXT]
    text = await ide.deep_evaluate(ctx, r["id"])
    assert text == DEEP_TEXT.strip()
    call_ = fake_llm.calls[0]
    assert call_["deep"] is True
    system, user = call_["messages"][0]["content"], call_["messages"][1]["content"]
    assert "Оценка: N/10" in system and "в стол" in system and "по памяти" in system
    assert "Кофейня у вокзала" in user and "Кофе навынос" in user
    assert "Работал бариста три года" in user                        # персонализация фактами
    assert "Трафик есть, но аренда" in user and "(5/10)" in user
    assert "Автор идеи" not in user and "{" not in system
    row = await ide.load_idea(ctx.db, r["id"])
    assert row["score"] == 6 and row["deep_evaluation"] == DEEP_TEXT.strip() and row["status"] == "new"
    assert await ctx.db.kv_get(ide.DEEP_KEY.format(r["id"])) is None
    assert notifier.texts()[0].startswith(f"🧠 Додумал идею #{r['id']} «Кофейня у вокзала»\n\n1. Суть")
    ev = (await ctx.db.recent_messages(5))[-1]
    assert ev["role"] == "event" and ev["via"] == "system"
    assert ev["content"].startswith(f"Бот додумал идею #{r['id']} «Кофейня у вокзала», итог: оценка 6/10")
    assert "докрутить" in ev["content"]


async def test_deep_evaluate_mentions_owner_name(ctx, fake_llm):
    from dataclasses import replace
    ctx.cfg = replace(ctx.cfg, owner_name="Саша")
    r = await save_coffee(ctx)
    fake_llm.script = ["Оценка: 5/10"]
    await ide.deep_evaluate(ctx, r["id"])
    assert "Автор идеи: Саша" in fake_llm.calls[0]["messages"][1]["content"]


async def test_deep_evaluate_status_thinking_during_and_restored(ctx, notifier):
    r = await save_coffee(ctx)
    await ok(ctx, "update_idea", id=r["id"], status="parked")
    seen = {}

    class SpyLLM:
        async def ask(self, system, user, **kw):
            seen["status"] = await ctx.db.scalar("SELECT status FROM ideas WHERE id=?", (r["id"],))
            seen["kw"] = kw
            return "Разбор без явной оценки."

    ctx.llm = SpyLLM()
    await ide.deep_evaluate(ctx, r["id"])
    assert seen["status"] == "thinking" and seen["kw"] == {"deep": True}
    row = await ide.load_idea(ctx.db, r["id"])
    assert row["status"] == "parked" and row["score"] == 5            # оценки нет — прежняя
    ev = (await ctx.db.recent_messages(5))[-1]
    assert "оценка не выставлена" in ev["content"]


async def test_deep_evaluate_status_changed_meanwhile_kept(ctx):
    r = await save_coffee(ctx)

    class ChangingLLM:
        async def ask(self, system, user, **kw):
            await ctx.db.execute("UPDATE ideas SET status='done' WHERE id=?", (r["id"],))
            return "Оценка: 8/10"

    ctx.llm = ChangingLLM()
    await ide.deep_evaluate(ctx, r["id"])
    row = await ide.load_idea(ctx.db, r["id"])
    assert row["status"] == "done" and row["score"] == 8


async def test_deep_evaluate_llm_error(ctx, fake_llm, notifier):
    r = await save_coffee(ctx)

    def boom(messages, kw):
        raise LLMError("модель не ответила вовремя (таймаут)")

    fake_llm.script = [boom]
    out = await ide.deep_evaluate(ctx, r["id"])
    assert out == "не смог додумать: модель не ответила вовремя (таймаут)"
    row = await ide.load_idea(ctx.db, r["id"])
    assert row["status"] == "new" and row["deep_evaluation"] == "" and row["score"] == 5
    assert "не смог додумать" in notifier.texts()[0].lower() and "таймаут" in notifier.texts()[0]
    assert await ctx.db.kv_get(ide.DEEP_KEY.format(r["id"])) is None


async def test_deep_evaluate_unexpected_error_and_no_notifier(ctx, fake_llm):
    r = await save_coffee(ctx)
    ctx.services.notifier = None

    def boom(messages, kw):
        raise RuntimeError("сломалось")

    fake_llm.script = [boom]
    out = await ide.deep_evaluate(ctx, r["id"])
    assert "не смог додумать" in out and "RuntimeError" in out
    assert (await ide.load_idea(ctx.db, r["id"]))["status"] == "new"
    assert await ide.deep_evaluate(ctx, 999) == "идеи #999 нет"
    assert "числом" in await ide.deep_evaluate(ctx, "abc")


async def test_deep_evaluate_recovers_after_crash(ctx, fake_llm, clock):
    """Прошлый разбор упал на полпути (перезапуск): статус застрял в thinking, пометка осталась."""
    r = await save_coffee(ctx)
    await ok(ctx, "update_idea", id=r["id"], status="in_work")
    await ide._begin_deep(ctx.db, await ide.load_idea(ctx.db, r["id"]))
    assert (await ide.load_idea(ctx.db, r["id"]))["status"] == "thinking"
    clock.advance(hours=2)
    assert await ide.deep_busy(ctx, r["id"]) is False                 # пометка протухла
    fake_llm.script = ["Оценка: 4/10"]
    await ide.deep_evaluate(ctx, r["id"])
    assert (await ide.load_idea(ctx.db, r["id"]))["status"] == "in_work"


async def test_deep_think_idea_tool_spawns(ctx, fake_llm, notifier):
    r = await save_coffee(ctx)
    fake_llm.script = [DEEP_TEXT]
    res = await ok(ctx, "deep_think_idea", id=r["id"])
    assert res["started"] is True and "Думаю в фоне" in res["note"] and "одной фразой" in res["note"]
    assert (await ide.load_idea(ctx.db, r["id"]))["status"] == "thinking"
    again = await ok(ctx, "deep_think_idea", id=r["id"])                # второй раз не запускаем
    assert again["started"] is False and "Уже думаю" in again["note"]
    await ctx.services.drain()
    row = await ide.load_idea(ctx.db, r["id"])
    assert row["status"] == "new" and row["score"] == 6 and row["deep_evaluation"]
    assert len(fake_llm.calls) == 1
    assert [t for t in notifier.texts() if t.startswith("🧠 Додумал идею")]
    assert ctx.outbox[-1].buttons                                   # outbox основного контекста не тронут фоном
    bad = await call(ctx, "deep_think_idea", id=404)
    assert bad["ok"] is False and "#404" in bad["error"]


async def test_deep_busy_stale_threshold(ctx, clock):
    r = await save_coffee(ctx)
    assert await ide.deep_busy(ctx, r["id"]) is False
    await ide._begin_deep(ctx.db, await ide.load_idea(ctx.db, r["id"]))
    assert await ide.deep_busy(ctx, r["id"]) is True
    clock.advance(seconds=1000)
    assert await ide.deep_busy(ctx, r["id"]) is True
    clock.advance(seconds=1000)                                     # 2000 с > 2·600+120
    assert await ide.deep_busy(ctx, r["id"]) is False
    assert await ide.deep_busy(ctx, 999) is False


def test_tools_registered():
    names = {"save_idea", "find_ideas", "get_idea", "update_idea", "list_ideas", "delete_idea", "deep_think_idea"}
    assert names <= set(tb.REGISTRY)
    d = tb.REGISTRY["save_idea"].description
    assert "сначала честно" in d.lower() and "4–6" in d and "1–10" in d
    assert "после того, как инструмент вернул ok" in d        # разбор — в итоговом ответе, а не до вызова
