"""Память: факты без дублей, позиции бота с историей, дневник, follow-up'ы, инструменты."""
from __future__ import annotations

import importlib
import json
import sys
import types
from zoneinfo import ZoneInfo

import pytest

from oracle import timeutil
from oracle.tools import base as tb
from oracle.tools import memory as mem

REM = "oracle.tools.reminders"


async def call(ctx, tool_name: str, /, **args) -> dict:
    return json.loads(await tb.dispatch(tool_name, args, ctx))


async def ok(ctx, tool_name: str, /, **args) -> dict:
    r = await call(ctx, tool_name, **args)
    assert r.get("ok") is True, r
    return r


async def count(db, table: str) -> int:
    return int(await db.scalar(f"SELECT COUNT(*) FROM {table}"))


# ── мелкие помощники ─────────────────────────────────────────────────────────
def test_norm_stems_jaccard():
    assert mem.norm("  Любит  КРЕПКИЙ кофё!!! ") == "любит крепкий кофе"
    assert mem.stems("Сестра Оля живёт в Риге") == {"сестр", "оля", "живет", "риг"}
    assert mem.stems("в и на") == {"в", "и", "на"}          # одни служебные — берём все
    assert mem.jaccard({"a", "b"}, {"a", "b", "c", "d"}) == 0.5
    assert mem.jaccard(set(), {"a"}) == 0.0


def test_clean_category_and_confidence():
    assert mem.clean_category("person") == "person"
    assert mem.clean_category("Люди") == "person"
    assert mem.clean_category("работа") == "work"
    assert mem.clean_category("что-то странное") == "general"
    assert mem.clean_category(None) == "general"
    assert mem.clean_confidence(150) == 100
    assert mem.clean_confidence(-5) == 0
    assert mem.clean_confidence("80%") == 80
    assert mem.clean_confidence(0.7) == 70
    assert mem.clean_confidence("много") == 60
    assert mem.clean_confidence(None, 40) == 40
    assert mem.clean_confidence(True) == 60


# ── факты ────────────────────────────────────────────────────────────────────
async def test_add_fact_basic_and_validation(db, clock):
    fid, created = await mem.add_fact(db, "  Жену зовут Маша  ", "person")
    assert created and fid > 0
    row = await mem.get_fact(db, fid)
    assert row["content"] == "Жену зовут Маша" and row["category"] == "person" and row["source"] == "chat"
    assert await db.search("fact", "жена") == [fid]
    assert await db.search("fact", "люди") == [fid]           # русская подпись категории в индексе
    with pytest.raises(ValueError):
        await mem.add_fact(db, "ок")
    with pytest.raises(ValueError):
        await mem.add_fact(db, "   ")
    with pytest.raises(ValueError):
        await mem.add_fact(db, "я" * 501)
    fid2, _ = await mem.add_fact(db, "Работает бэкенд-разработчиком", "непонятно", source="weird")
    row2 = await mem.get_fact(db, fid2)
    assert row2["category"] == "general" and row2["source"] == "chat"
    fid3, _ = await mem.add_fact(db, "Подмечено ночью: устаёт к пятнице", "health", source="reflection")
    assert (await mem.get_fact(db, fid3))["source"] == "reflection"


async def test_add_fact_exact_duplicate_touches(db, clock):
    fid, created = await mem.add_fact(db, "Любит крепкий кофе.")
    before = (await mem.get_fact(db, fid))["updated_at"]
    clock.advance(hours=3)
    fid2, created2 = await mem.add_fact(db, "любит крепкий КОФЁ", "preference")
    assert (fid2, created2) == (fid, False)
    row = await mem.get_fact(db, fid)
    assert await count(db, "facts") == 1
    assert row["updated_at"] > before
    assert row["content"] == "Любит крепкий кофе."          # формулировка не тронута
    assert row["category"] == "preference"                  # уточнили категорию
    # повтор с general не сбивает конкретную категорию
    await mem.add_fact(db, "Любит крепкий кофе")
    assert (await mem.get_fact(db, fid))["category"] == "preference"


async def test_add_fact_near_duplicate_replaces(db, clock):
    fid, _ = await mem.add_fact(db, "Любит крепкий кофе без сахара", "preference")
    clock.advance(minutes=5)
    fid2, created = await mem.add_fact(db, "Любит крепкий кофе без сахара по утрам")
    assert fid2 == fid and created is False
    row = await mem.get_fact(db, fid)
    assert row["content"] == "Любит крепкий кофе без сахара по утрам"   # новая формулировка победила
    assert row["category"] == "preference"
    assert await count(db, "facts") == 1
    assert await db.search("fact", "утром") == [fid]                    # переиндексировано
    # отрицание — та же тема, новая информация заменяет старую
    fid3, created3 = await mem.add_fact(db, "Не любит крепкий кофе без сахара по утрам")
    assert fid3 == fid and not created3
    assert (await mem.get_fact(db, fid))["content"].startswith("Не любит")


async def test_add_fact_different_facts_not_merged(db, clock):
    a, ca = await mem.add_fact(db, "Жену зовут Маша", "person")
    b, cb = await mem.add_fact(db, "Сына зовут Петя, ему 7 лет", "person")
    c, cc = await mem.add_fact(db, "Сестра Оля живёт в Риге", "person")
    d, cd = await mem.add_fact(db, "Сестра Оля переехала в Берлин", "person")
    assert ca and cb and cc and cd
    assert len({a, b, c, d}) == 4


async def test_facts_for_prompt_small_returns_all_sorted(db, clock):
    await mem.add_fact(db, "Работает в банке", "work")
    await mem.add_fact(db, "Жену зовут Маша", "person")
    await mem.add_fact(db, "Бегает по утрам", "health")
    rows = await mem.facts_for_prompt(db, "что угодно", 40)
    assert [r["category"] for r in rows] == ["health", "person", "work"]
    assert set(rows[0]) == {"id", "content", "category"}
    assert await mem.facts_for_prompt(db, "", 0) == []


async def test_facts_for_prompt_over_limit(db, clock):
    ids = []
    for i in range(30):
        fid, _ = await mem.add_fact(db, f"Факт номер {i}: любимое число {i * 7 + 1000}", "general")
        ids.append(fid)
        clock.advance(minutes=1)
    target, _ = await mem.add_fact(db, "Аллергия на арахис", "health")
    # освежим старый факт — он должен попасть в «недавние»
    clock.advance(minutes=1)
    await mem.add_fact(db, "Факт номер 0: любимое число 1000")
    # ещё свежие, чтобы арахис не был среди последних обновлённых
    for i in range(12):
        clock.advance(minutes=1)
        await mem.add_fact(db, f"Свежая заметка {i} про погоду {i * 13 + 500}", "other")
    rows = await mem.facts_for_prompt(db, "есть ли аллергия на арахис", 10)
    got = [r["id"] for r in rows]
    assert len(got) == 10 and len(set(got)) == 10
    assert got[0] == target                               # нашлось по запросу — первым
    rows2 = await mem.facts_for_prompt(db, "", 10)
    assert target not in [r["id"] for r in rows2]         # без запроса — только свежие
    recent = await db.fetchall("SELECT id FROM facts ORDER BY updated_at DESC, id DESC LIMIT 10")
    assert [r["id"] for r in rows2] == [r["id"] for r in recent]


async def test_delete_and_all_facts(db, clock):
    fid, _ = await mem.add_fact(db, "Не ест мясо", "preference")
    await mem.add_fact(db, "Работает в банке", "work")
    assert [r["content"] for r in await mem.all_facts(db, "work")] == ["Работает в банке"]
    assert len(await mem.all_facts(db)) == 2
    row = await mem.delete_fact(db, fid)
    assert row["content"] == "Не ест мясо"
    assert await mem.delete_fact(db, fid) is None
    assert await mem.delete_fact(db, "мусор") is None
    assert await db.search("fact", "мясо") == []


# ── позиции ──────────────────────────────────────────────────────────────────
async def test_opinion_create_update_history(db, clock):
    o = await mem.upsert_opinion(db, topic="Удалённая работа против офиса",
                                 stance="Удалёнка лучше для глубокой работы", reasons="меньше отвлечений",
                                 confidence=70)
    assert o["created"] and not o["changed"] and o["confidence"] == 70
    oid = o["id"]
    # та же позиция, другая уверенность → не смена
    o2 = await mem.upsert_opinion(db, topic="удалённая работа против офиса",
                                  stance="Удалёнка лучше для глубокой работы!", confidence=150)
    assert o2["id"] == oid and not o2["changed"] and not o2["created"]
    assert o2["confidence"] == 100 and o2["reasons"] == "меньше отвлечений"     # доводы сохранились
    assert json.loads(o2["history"]) == []
    # смена позиции → история
    clock.advance(days=1)
    o3 = await mem.upsert_opinion(db, topic="удалённая работа — против офиса", stance="Гибрид лучше чистой удалёнки",
                                  reasons="джунам нужен офис", confidence=65,
                                  why_changed="показал данные о росте джунов в офисе")
    assert o3["id"] == oid and o3["changed"] and not o3["created"]
    assert o3["previous_stance"] == "Удалёнка лучше для глубокой работы!"
    assert o3["topic"] == "Удалённая работа против офиса"          # без id тему не переписываем
    h = json.loads(o3["history"])
    assert len(h) == 1
    assert h[0]["stance"] == "Удалёнка лучше для глубокой работы!" and h[0]["confidence"] == 100
    assert h[0]["why_changed"] == "показал данные о росте джунов в офисе"
    assert h[0]["reasons"] == "меньше отвлечений" and h[0]["changed_at"]
    assert await count(db, "opinions") == 1
    assert await db.search("opinion", "гибрид") == [oid]


async def test_opinion_by_id_and_validation(db, clock):
    o = await mem.upsert_opinion(db, topic="Биткоин", stance="Спекуляция, а не деньги", confidence=55)
    o2 = await mem.upsert_opinion(db, topic="Криптовалюты как инвестиция", stance="Максимум 5% портфеля",
                                  opinion_id=o["id"], why_changed="уточнил")
    assert o2["id"] == o["id"] and o2["changed"] and o2["topic"] == "Криптовалюты как инвестиция"
    # несуществующий id — ищем по теме / создаём
    o3 = await mem.upsert_opinion(db, topic="Электромобили", stance="Будущее за ними", opinion_id=999)
    assert o3["created"] and o3["id"] != o["id"]
    o4 = await mem.upsert_opinion(db, topic="Электромобили", stance="Будущее за ними", opinion_id="мусор")
    assert o4["id"] == o3["id"] and not o4["created"]
    with pytest.raises(ValueError):
        await mem.upsert_opinion(db, topic="  ", stance="x")
    with pytest.raises(ValueError):
        await mem.upsert_opinion(db, topic="тема", stance="")
    o5 = await mem.upsert_opinion(db, topic="Что-то новое", stance="да", confidence="чушь")
    assert o5["confidence"] == 60


async def test_opinion_topic_matching_threshold(db, clock):
    a = await mem.upsert_opinion(db, topic="Криптовалюта", stance="Пузырь")
    b = await mem.upsert_opinion(db, topic="Криптовалюта как инвестиция на пенсию", stance="Плохая идея")
    assert b["created"] and b["id"] != a["id"]                     # похоже мало — отдельная тема
    c = await mem.upsert_opinion(db, topic="криптовалюты", stance="Пузырь")
    assert c["id"] == a["id"] and not c["changed"]                 # та же тема в другой форме слова


async def test_opinion_history_capped(db, clock):
    o = await mem.upsert_opinion(db, topic="Лучший язык", stance="вариант 0")
    for i in range(1, 26):
        clock.advance(minutes=1)
        o = await mem.upsert_opinion(db, topic="Лучший язык", stance=f"вариант {i}", why_changed=f"довод {i}")
    h = json.loads(o["history"])
    assert len(h) == mem.HISTORY_MAX
    assert h[-1]["stance"] == "вариант 24" and h[-1]["why_changed"] == "довод 25"
    assert h[0]["stance"] == "вариант 5"


async def test_opinions_for_prompt(db, clock):
    await mem.upsert_opinion(db, topic="Кофе", stance="Две чашки в день норм", confidence=90)
    await mem.upsert_opinion(db, topic="Политика", stance="Никому не верить на слово", confidence=80)
    low = await mem.upsert_opinion(db, topic="Бег по утрам", stance="Полезнее вечернего", confidence=30)
    for i in range(6):
        await mem.upsert_opinion(db, topic=f"Тема {i} разное", stance=f"мнение {i}", confidence=50)
    rows = await mem.opinions_for_prompt(db, "", 3)
    assert [r["topic"] for r in rows] == ["Кофе", "Политика", "Тема 5 разное"]
    rows = await mem.opinions_for_prompt(db, "бегать утром", 3)
    assert rows[0]["id"] == low["id"] and len(rows) == 3
    assert {"id", "topic", "stance", "confidence", "reasons"} <= set(rows[0])
    assert await mem.opinions_for_prompt(db, "x", 0) == []


# ── дневник ──────────────────────────────────────────────────────────────────
async def test_journal(db, clock):
    assert await mem.latest_journal(db) is None
    await mem.add_journal(db, "  Первая запись  ")
    jid = await mem.add_journal(db, "Он третий день откладывает звонок врачу — завтра спрошу.")
    j = await mem.latest_journal(db)
    assert j["id"] == jid and j["content"].startswith("Он третий день") and j["created_at"]
    long_id = await mem.add_journal(db, "а" * 5000)
    assert len((await mem.latest_journal(db))["content"]) == mem.JOURNAL_MAX and long_id > jid
    with pytest.raises(ValueError):
        await mem.add_journal(db, "   ")


# ── инструменты ──────────────────────────────────────────────────────────────
def test_tools_registered():
    names = {"remember", "recall", "forget", "list_facts", "set_opinion", "get_opinions", "schedule_followup"}
    assert names <= set(tb.REGISTRY)
    for n in names:
        assert tb.REGISTRY[n].description and len(tb.REGISTRY[n].description) > 40


async def test_tool_remember_recall_forget(ctx):
    r = await ok(ctx, "remember", content="Начальника зовут Сергей Петрович", category="person")
    assert r["created"] is True and r["category"] == "person"
    r2 = await ok(ctx, "remember", content="начальника зовут Сергей Петрович!")
    assert r2["id"] == r["id"] and r2["created"] is False and "note" in r2
    await ok(ctx, "remember", content="Мечтает переехать в Португалию", category="планы")
    await ok(ctx, "set_opinion", topic="Переезд в Португалию", stance="Сначала пожить там месяц",
             reasons="климат и налоги не всё", confidence=70)
    found = await ok(ctx, "recall", query="что ты помнишь про начальника")
    assert [f["id"] for f in found["facts"]] == [r["id"]]
    assert found["facts"][0]["updated"] == "28.09.2026"
    found = await ok(ctx, "recall", query="Португалия")
    assert len(found["facts"]) == 1 and found["facts"][0]["category"] == "plan"
    assert found["opinions"][0]["topic"] == "Переезд в Португалию"
    none = await ok(ctx, "recall", query="квантовая физика")
    assert none["facts"] == [] and none["opinions"] == [] and "ничего не нашёл" in none["note"]
    gone = await ok(ctx, "forget", fact_id=r["id"])
    assert gone["forgotten"] == "Начальника зовут Сергей Петрович"
    assert (await ok(ctx, "recall", query="начальник"))["facts"] == []
    bad = await call(ctx, "forget", fact_id=r["id"])
    assert bad["ok"] is False and "нет" in bad["error"]
    bad = await call(ctx, "forget", fact_id="abc")
    assert bad["ok"] is False
    bad = await call(ctx, "remember", content="ой")
    assert bad["ok"] is False and "коротк" in bad["error"]


async def test_tool_list_facts(ctx):
    await ok(ctx, "remember", content="Жену зовут Маша", category="person")
    await ok(ctx, "remember", content="Работает в банке", category="work")
    r = await ok(ctx, "list_facts")
    assert r["total"] == 2 and len(r["items"]) == 2
    r = await ok(ctx, "list_facts", category="work")
    assert [i["content"] for i in r["items"]] == ["Работает в банке"]
    r = await ok(ctx, "list_facts", category="люди")
    assert [i["content"] for i in r["items"]] == ["Жену зовут Маша"]
    assert (await ok(ctx, "list_facts", category="all"))["total"] == 2
    bad = await call(ctx, "list_facts", category="звёзды")
    assert bad["ok"] is False and "person" in bad["error"]


async def test_tool_set_opinion_requires_why_changed(ctx):
    r = await ok(ctx, "set_opinion", topic="Его идея с кофейней", stance="Не взлетит без трафика",
                 reasons="у вокзала аренда дорогая", confidence=75)
    assert r["created"] and r["note"] == "позиция записана"
    # напор без аргумента: смена без why_changed не примется
    bad = await call(ctx, "set_opinion", topic="Его идея с кофейней", stance="Взлетит",
                     reasons="он настаивает", confidence=60)
    assert bad["ok"] is False and "why_changed" in bad["error"] and f"#{r['id']}" in bad["error"]
    row = await mem.get_opinion(ctx.db, r["id"])
    assert row["stance"] == "Не взлетит без трафика"
    # та же позиция — можно без why_changed, уверенность меняется
    same = await ok(ctx, "set_opinion", topic="идея с кофейней", stance="Не взлетит без трафика",
                    reasons="аренда дорогая, трафик транзитный", confidence="90%", opinion_id=r["id"])
    assert same["changed"] is False and same["confidence"] == 90
    assert same["reasons"] == "аренда дорогая, трафик транзитный" and same["id"] == r["id"]
    assert (await mem.get_opinion(ctx.db, r["id"]))["topic"] == "идея с кофейней"   # явный id — тема переписана
    # с аргументом — меняется, история растёт
    ch = await ok(ctx, "set_opinion", topic="Его идея с кофейней", stance="Может взлететь",
                  reasons="договорился об аренде за полцены", confidence=55, opinion_id=r["id"],
                  why_changed="аренда вдвое дешевле рынка")
    assert ch["changed"] is True and ch["previous_stance"] == "Не взлетит без трафика"
    assert ch["changes"] == 1 and ch["last_change"] == "аренда вдвое дешевле рынка"
    missing = await call(ctx, "set_opinion", topic="x", stance="y")
    assert missing["ok"] is False and "не хватает" in missing["error"]


async def test_tool_get_opinions(ctx):
    empty = await ok(ctx, "get_opinions")
    assert empty["items"] == [] and "пока нет" in empty["note"]
    await ok(ctx, "set_opinion", topic="Криптовалюты", stance="Спекуляция", reasons="нет денежного потока",
             confidence=80)
    await ok(ctx, "set_opinion", topic="Утренний бег", stance="Полезен", reasons="бодрость", confidence=60)
    allr = await ok(ctx, "get_opinions")
    assert [i["topic"] for i in allr["items"]] == ["Криптовалюты", "Утренний бег"]
    assert allr["items"][0]["changes"] == 0 and allr["items"][0]["reasons"] == "нет денежного потока"
    hit = await ok(ctx, "get_opinions", query="что думаешь про криптовалюту")
    assert [i["topic"] for i in hit["items"]] == ["Криптовалюты"] and hit["items"][0]["match"] is True
    miss = await ok(ctx, "get_opinions", query="политика")
    assert len(miss["items"]) == 2 and all(i["match"] is False for i in miss["items"]) and "note" in miss


# ── follow-up ────────────────────────────────────────────────────────────────
async def test_schedule_followup_real_reminders(ctx):
    importlib.import_module(REM)
    r = await ok(ctx, "schedule_followup", when="2026-09-30 12:00",
                 about="Спросить, отправил ли резюме — тянет неделю")
    row = await ctx.db.fetchone("SELECT * FROM reminders WHERE id=?", (r["id"],))
    assert row["kind"] == "followup" and row["nag"] == 0 and row["status"] == "active"
    assert row["text"] == "Спросить, отправил ли резюме — тянет неделю"
    assert "30.09" in r["when"]
    past = await call(ctx, "schedule_followup", when="2026-09-01 12:00", about="старое")
    assert past["ok"] is False and "прошло" in past["error"]
    bad = await call(ctx, "schedule_followup", when="когда-нибудь", about="что-то")
    assert bad["ok"] is False


async def test_schedule_followup_fake_and_missing(ctx, monkeypatch):
    fake = types.ModuleType(REM)
    seen = {}

    async def create_reminder(c, *, text, when, rrule=None, kind="reminder", nag=None, **kw):
        seen.update(text=text, when=when, kind=kind, nag=nag)
        return {"id": 77, "when_local": "ср 30.09 12:00"}

    fake.create_reminder = create_reminder
    monkeypatch.setitem(sys.modules, REM, fake)
    r = await ok(ctx, "schedule_followup", when="2026-09-30 12:00", about="  проверить,   как проект  ")
    assert r == {"ok": True, "id": 77, "when": "ср 30.09 12:00", "about": "проверить, как проект"}
    assert seen == {"text": "проверить, как проект", "when": "2026-09-30 12:00", "kind": "followup", "nag": False}
    monkeypatch.setitem(sys.modules, REM, None)
    r = await call(ctx, "schedule_followup", when="2026-09-30 12:00", about="x y z")
    assert r["ok"] is False and "недоступен" in r["error"]


async def test_prompt_integration_with_persona(db, clock):
    """Факты и позиции в том виде, в каком их ждёт persona.build_system."""
    from oracle.persona import build_system
    await mem.add_fact(db, "Жену зовут Маша", "person")
    await mem.upsert_opinion(db, topic="Кофе", stance="Норм", confidence=70)
    s = build_system(name="Оракул", now=timeutil.fmt_now_for_prompt(ZoneInfo("Europe/Moscow")),
                     facts=await mem.facts_for_prompt(db), opinions=await mem.opinions_for_prompt(db),
                     journal=await mem.latest_journal(db))
    assert "[person] Жену зовут Маша" in s and "Кофе: Норм (уверенность 70%)" in s
