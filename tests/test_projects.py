"""Проекты и задачи: CRUD, поиск проекта, сроки с напоминаниями, due_tasks."""
from __future__ import annotations

import importlib
import json
import sys
import types

import pytest

from oracle import timeutil
from oracle.tools import base as tb
from oracle.tools import projects as pr

REM = "oracle.tools.reminders"


async def call(ctx, tool_name: str, /, **args) -> dict:
    return json.loads(await tb.dispatch(tool_name, args, ctx))


async def ok(ctx, tool_name: str, /, **args) -> dict:
    r = await call(ctx, tool_name, **args)
    assert r.get("ok") is True, r
    return r


@pytest.fixture
def fake_rem(monkeypatch):
    """Подменный модуль напоминаний: пишет строки в reminders, как настоящий, и всё запоминает."""
    mod = types.ModuleType(REM)
    mod.created = []
    mod.cancelled = []
    mod.fail_with = None

    async def create_reminder(ctx, *, text, when, rrule=None, kind="reminder", nag=None, challenge=None,
                              nag_interval_min=None, ref_type=None, ref_id=None):
        if mod.fail_with:
            raise mod.fail_with
        local = timeutil.parse_local(when, ctx.tz) if isinstance(when, str) else when
        if local <= timeutil.now_utc():
            raise ValueError("это время уже прошло")
        rid = await ctx.db.execute(
            "INSERT INTO reminders(text, kind, local_start, tz, next_at, status, ref_type, ref_id, created_at) "
            "VALUES(?,?,?,?,?,'active',?,?,?)",
            (text, kind, timeutil.naive_str(local), ctx.tz.key, timeutil.iso(local), ref_type, ref_id,
             timeutil.iso(timeutil.now_utc())))
        mod.created.append({"id": rid, "text": text, "when": when, "kind": kind,
                            "ref_type": ref_type, "ref_id": ref_id})
        return {"id": rid, "text": text, "when_local": timeutil.fmt_local(local, ctx.tz)}

    async def cancel_by_ref(db, ref_type, ref_id):
        mod.cancelled.append((ref_type, ref_id))
        return await db.execute("UPDATE reminders SET status='cancelled' WHERE ref_type=? AND ref_id=? "
                                "AND status='active'", (ref_type, int(ref_id)))

    mod.create_reminder = create_reminder
    mod.cancel_by_ref = cancel_by_ref
    monkeypatch.setitem(sys.modules, REM, mod)
    return mod


async def active_reminders(db, tid: int) -> list[dict]:
    return await db.fetchall("SELECT * FROM reminders WHERE ref_type='task' AND ref_id=? AND status='active'",
                             (tid,))


# ── проекты ──────────────────────────────────────────────────────────────────
async def test_create_project_and_duplicate(ctx):
    r = await ok(ctx, "create_project", name="Кофейня", description="кофе навынос у вокзала",
                 goal="окупиться за год")
    assert r["id"] > 0 and r["name"] == "Кофейня" and r["status"] == "active" and "hint" not in r
    row = await ctx.db.fetchone("SELECT * FROM projects WHERE id=?", (r["id"],))
    assert row["description"] == "кофе навынос у вокзала" and row["goal"] == "окупиться за год"
    assert await ctx.db.search("project", "вокзал") == [r["id"]]
    d = await call(ctx, "create_project", name="  КОФЕЙНЯ ")
    assert d["ok"] is False and f"#{r['id']}" in d["error"]
    e = await call(ctx, "create_project", name="   ")
    assert e["ok"] is False
    r2 = await ok(ctx, "create_project", name="Ремонт")
    assert "hint" in r2                                   # без цели — подсказка спросить


async def test_duplicate_allowed_when_old_closed(ctx):
    r = await ok(ctx, "create_project", name="Кофейня")
    await ok(ctx, "update_project", project=r["id"], status="done")
    r2 = await ok(ctx, "create_project", name="кофейня")
    assert r2["id"] != r["id"]
    back = await call(ctx, "update_project", project=r["id"], status="active")
    assert back["ok"] is False and f"#{r2['id']}" in back["error"]
    # по имени находится активный
    assert (await pr.resolve_project(ctx.db, "Кофейня"))["id"] == r2["id"]


async def test_resolve_project(ctx):
    a = await ok(ctx, "create_project", name="Кофейня", goal="свой бизнес")
    b = await ok(ctx, "create_project", name="Ремонт кухни", description="плитка, фасады")
    db = ctx.db
    assert (await pr.resolve_project(db, a["id"]))["name"] == "Кофейня"
    assert (await pr.resolve_project(db, str(b["id"])))["name"] == "Ремонт кухни"
    assert (await pr.resolve_project(db, f"#{b['id']}"))["name"] == "Ремонт кухни"
    assert (await pr.resolve_project(db, "кофейня"))["id"] == a["id"]
    assert (await pr.resolve_project(db, "«Ремонт кухни»"))["id"] == b["id"]
    assert (await pr.resolve_project(db, "проект Кофейня"))["id"] == a["id"]
    assert (await pr.resolve_project(db, "кофейне"))["id"] == a["id"]           # по индексу, другая форма
    assert (await pr.resolve_project(db, "плитка"))["id"] == b["id"]            # по описанию
    assert (await pr.resolve_project(db, "кухн"))["id"] == b["id"]              # часть имени
    assert await pr.resolve_project(db, "космодром") is None
    assert await pr.resolve_project(db, 999) is None
    assert await pr.resolve_project(db, "") is None
    assert await pr.resolve_project(db, None) is None


async def test_list_projects(ctx, fake_rem, clock):
    a = await ok(ctx, "create_project", name="Кофейня", goal="окупиться")
    b = await ok(ctx, "create_project", name="Ремонт")
    await ok(ctx, "add_task", text="найти помещение", project="Кофейня", due="2026-10-05 12:00")
    await ok(ctx, "add_task", text="посчитать смету", project="кофейня", due="2026-10-01")
    t3 = await ok(ctx, "add_task", text="зарегистрировать ИП", project=a["id"])
    await ok(ctx, "update_task", id=t3["id"], status="done")
    await ok(ctx, "update_project", project=b["id"], status="paused")
    r = await ok(ctx, "list_projects")
    assert r["count"] == 1
    item = r["items"][0]
    assert item["id"] == a["id"] and item["open_tasks"] == 2 and item["done_tasks"] == 1
    assert item["next_due"] == "чт 01.10 23:59" and item["goal"] == "окупиться" and item["idle_days"] == 0
    r = await ok(ctx, "list_projects", status="all")
    assert [i["name"] for i in r["items"]] == ["Кофейня", "Ремонт"]
    assert r["items"][1]["next_due"] is None
    assert (await ok(ctx, "list_projects", status="paused"))["items"][0]["name"] == "Ремонт"
    empty = await ok(ctx, "list_projects", status="dropped")
    assert empty["count"] == 0 and "note" in empty
    bad = await call(ctx, "list_projects", status="спит")
    assert bad["ok"] is False and "active" in bad["error"]
    clock.advance(days=10)
    assert (await ok(ctx, "list_projects"))["items"][0]["idle_days"] == 10


async def test_get_project_tasks_and_stats(ctx, fake_rem):
    p = await ok(ctx, "create_project", name="Кофейня")
    ids = {}
    ids["A"] = (await ok(ctx, "add_task", text="A", project="Кофейня", due="2026-10-05 10:00"))["id"]
    ids["B"] = (await ok(ctx, "add_task", text="B", project="Кофейня", priority=1))["id"]
    ids["C"] = (await ok(ctx, "add_task", text="C", project="Кофейня", priority=1, due="2026-10-01"))["id"]
    ids["D"] = (await ok(ctx, "add_task", text="D", project="Кофейня"))["id"]
    ids["E"] = (await ok(ctx, "add_task", text="E", project="Кофейня"))["id"]
    ids["F"] = (await ok(ctx, "add_task", text="F", project="Кофейня", priority=3, due="2026-09-27 10:00"))["id"]
    ids["G"] = (await ok(ctx, "add_task", text="G", project="Кофейня"))["id"]
    await ok(ctx, "update_task", id=ids["E"], status="done")
    await ok(ctx, "update_task", id=ids["G"], status="dropped")
    await ok(ctx, "add_task", text="чужая", due="2026-09-29 10:00")
    r = await ok(ctx, "get_project", project="кофейня")
    assert r["id"] == p["id"] and r["status"] == "active"
    assert [t["text"] for t in r["tasks"]] == ["C", "B", "A", "D", "F", "E", "G"]
    f = next(t for t in r["tasks"] if t["text"] == "F")
    assert f["overdue"] is True and f["due"] == "вс 27.09 10:00" and f["priority"] == 3
    e = next(t for t in r["tasks"] if t["text"] == "E")
    assert e["status"] == "done" and e["overdue"] is False and "done" in e
    assert set(r["tasks"][0]) >= {"id", "text", "status", "priority", "due"}
    assert r["stats"] == {"total": 7, "open": 5, "doing": 0, "done": 1, "dropped": 1, "overdue": 1,
                          "progress_pct": 17}
    miss = await call(ctx, "get_project", project="космодром")
    assert miss["ok"] is False and "list_projects" in miss["error"]


async def test_get_project_hides_old_closed(ctx):
    p = await ok(ctx, "create_project", name="Большой")
    for i in range(pr._CLOSED_SHOWN + 3):
        t = await ok(ctx, "add_task", text=f"t{i}", project=p["id"])
        await ok(ctx, "update_task", id=t["id"], status="done")
    r = await ok(ctx, "get_project", project=p["id"])
    assert len(r["tasks"]) == pr._CLOSED_SHOWN and r["closed_hidden"] == 3
    assert r["stats"]["done"] == pr._CLOSED_SHOWN + 3 and r["stats"]["progress_pct"] == 100
    assert "hint" in r                                     # открытых нет — какой следующий шаг?


async def test_update_project(ctx):
    a = await ok(ctx, "create_project", name="Кофейня")
    b = await ok(ctx, "create_project", name="Ремонт")
    await ok(ctx, "add_task", text="t", project=a["id"])
    r = await ok(ctx, "update_project", project="кофейня", name="Кофейня у вокзала", goal="100 чашек в день")
    assert r["name"] == "Кофейня у вокзала" and r["goal"] == "100 чашек в день"
    assert await ctx.db.search("project", "вокзал") == [a["id"]]
    assert await ctx.db.search("project", "чашек") == [a["id"]]
    dup = await call(ctx, "update_project", project=b["id"], name="кофейня у вокзала")
    assert dup["ok"] is False and f"#{a['id']}" in dup["error"]
    bad = await call(ctx, "update_project", project=a["id"], status="спит")
    assert bad["ok"] is False and "paused" in bad["error"]
    assert (await call(ctx, "update_project", project=a["id"]))["ok"] is False
    assert (await call(ctx, "update_project", project="нет такого", status="done"))["ok"] is False
    done = await ok(ctx, "update_project", project=a["id"], status="готово")      # синоним
    assert done["status"] == "done" and "открытых задач: 1" in done["note"]
    r = await ok(ctx, "update_project", project=b["id"], status="paused", description="")
    assert r["status"] == "paused" and "note" not in r


# ── задачи и напоминания ─────────────────────────────────────────────────────
async def test_add_task_with_due_creates_reminder(ctx, fake_rem):
    await ok(ctx, "create_project", name="Кофейня")
    r = await ok(ctx, "add_task", text="Позвонить арендодателю", project="Кофейня",
                 due="2026-09-30 15:00", priority=1)
    assert r["due"] == "ср 30.09 15:00" and r["priority"] == 1 and r["project"] == "Кофейня"
    assert r["reminder_id"] is not None and "note" not in r
    assert fake_rem.created == [{"id": r["reminder_id"], "text": "Задача: Позвонить арендодателю [Кофейня]",
                                 "when": "2026-09-30 15:00", "kind": "task", "ref_type": "task",
                                 "ref_id": r["id"]}]
    row = await ctx.db.fetchone("SELECT * FROM tasks WHERE id=?", (r["id"],))
    assert row["due_at"] == "2026-09-30T12:00:00+00:00" and row["status"] == "todo"
    # без проекта — без скобок, только дата → срок до конца дня, напоминание утром в 09:00
    r2 = await ok(ctx, "add_task", text="Купить кофемолку", due="2026-10-02")
    assert fake_rem.created[-1]["text"] == "Задача: Купить кофемолку"
    assert fake_rem.created[-1]["when"] == "2026-10-02 09:00" and r2["project"] is None
    assert r2["due"] == "пт 02.10 23:59"


async def test_add_task_past_due_skips_reminder(ctx, fake_rem):
    r = await ok(ctx, "add_task", text="Сдать отчёт", due="2026-09-27 18:00")
    assert r["reminder_id"] is None and "прошёл" in r["note"]
    assert fake_rem.created == []
    assert await ctx.db.scalar("SELECT COUNT(*) FROM tasks") == 1


async def test_add_task_without_due_or_remind(ctx, fake_rem):
    r = await ok(ctx, "add_task", text="Подумать о названии")
    assert r["due"] is None and r["reminder_id"] is None and r["priority"] == 2
    r2 = await ok(ctx, "add_task", text="Тихая", due="2026-10-01 10:00", remind=False)
    assert r2["reminder_id"] is None and "без напоминания" in r2["note"]
    assert fake_rem.created == []
    assert await ctx.db.kv_get(f"task_noremind:{r2['id']}") is True
    r3 = await ok(ctx, "add_task", text="Тихая 2", due="2026-10-01 10:00", remind="нет")
    assert r3["reminder_id"] is None and fake_rem.created == []


async def test_add_task_bad_input(ctx, fake_rem):
    miss = await call(ctx, "add_task", text="x", project="космодром")
    assert miss["ok"] is False and "create_project" in miss["error"]
    assert (await call(ctx, "add_task", text="  "))["ok"] is False
    bad_due = await call(ctx, "add_task", text="x", due="когда-нибудь")
    assert bad_due["ok"] is False and "YYYY-MM-DD" in bad_due["error"]
    assert (await call(ctx, "add_task", text="x", priority="очень"))["ok"] is False
    assert await ctx.db.scalar("SELECT COUNT(*) FROM tasks") == 0
    assert (await ok(ctx, "add_task", text="x", priority=7))["priority"] == 3
    assert (await ok(ctx, "add_task", text="x", priority=0))["priority"] == 1
    assert (await ok(ctx, "add_task", text="x", priority="высокий"))["priority"] == 1
    assert (await ok(ctx, "add_task", text="x", project="нет"))["project"] is None


async def test_add_task_reminders_module_missing(ctx, monkeypatch):
    monkeypatch.setitem(sys.modules, REM, None)          # import → ImportError
    r = await ok(ctx, "add_task", text="x", due="2026-10-01 10:00")
    assert r["reminder_id"] is None and "недоступны" in r["note"]
    u = await ok(ctx, "update_task", id=r["id"], status="done")    # снять напоминание — тоже не падает
    assert u["status"] == "done"


async def test_add_task_reminder_failure_keeps_task(ctx, fake_rem):
    fake_rem.fail_with = RuntimeError("boom")
    r = await ok(ctx, "add_task", text="x", due="2026-10-01 10:00")
    assert r["reminder_id"] is None and "RuntimeError" in r["note"]
    fake_rem.fail_with = ValueError("это время уже прошло: …")
    r = await ok(ctx, "add_task", text="y", due="2026-10-01 10:00")
    assert "время уже прошло" in r["note"]
    assert await ctx.db.scalar("SELECT COUNT(*) FROM tasks") == 2


async def test_update_task_done_cancels_reminder(ctx, fake_rem, clock):
    r = await ok(ctx, "add_task", text="Сдать отчёт", due="2026-09-30 18:00")
    assert len(await active_reminders(ctx.db, r["id"])) == 1
    clock.advance(hours=1)
    u = await ok(ctx, "update_task", id=r["id"], status="done")
    assert u["status"] == "done" and u["reminder_id"] is None
    assert ("task", r["id"]) in fake_rem.cancelled
    assert await active_reminders(ctx.db, r["id"]) == []
    row = await ctx.db.fetchone("SELECT * FROM tasks WHERE id=?", (r["id"],))
    assert row["done_at"] == "2026-09-28T07:00:00+00:00"
    # открыли снова — срок в будущем, напоминание возвращается
    u = await ok(ctx, "update_task", id=r["id"], status="todo")
    assert u["reminder_id"] is not None and len(await active_reminders(ctx.db, r["id"])) == 1
    assert (await ctx.db.fetchone("SELECT done_at FROM tasks WHERE id=?", (r["id"],)))["done_at"] is None
    u = await ok(ctx, "update_task", id=r["id"], status="отменена")
    assert u["status"] == "dropped" and await active_reminders(ctx.db, r["id"]) == []


async def test_update_task_due_text_and_remind(ctx, fake_rem):
    await ok(ctx, "create_project", name="Кофейня")
    r = await ok(ctx, "add_task", text="Смета", project="Кофейня", due="2026-09-30 18:00")
    tid = r["id"]
    u = await ok(ctx, "update_task", id=tid, due="2026-10-03 11:00")
    assert u["due"] == "сб 03.10 11:00"
    rems = await active_reminders(ctx.db, tid)
    assert len(rems) == 1 and rems[0]["local_start"] == "2026-10-03T11:00:00"
    u = await ok(ctx, "update_task", id=tid, text="Смета на ремонт")
    rems = await active_reminders(ctx.db, tid)
    assert len(rems) == 1 and rems[0]["text"] == "Задача: Смета на ремонт [Кофейня]"
    n_created = len(fake_rem.created)
    await ok(ctx, "update_task", id=tid, priority=1)                 # приоритет напоминания не трогает
    assert len(fake_rem.created) == n_created
    u = await ok(ctx, "update_task", id=tid, due="")                 # снять срок
    assert u["due"] is None and await active_reminders(ctx.db, tid) == []
    u = await ok(ctx, "update_task", id=tid, due="2026-10-04 12:00", remind=False)
    assert u["reminder_id"] is None and await active_reminders(ctx.db, tid) == []
    u = await ok(ctx, "update_task", id=tid, due="2026-10-05 12:00")   # отказ от напоминания помнится
    assert await active_reminders(ctx.db, tid) == []
    u = await ok(ctx, "update_task", id=tid, remind=True)
    assert u["reminder_id"] is not None and len(await active_reminders(ctx.db, tid)) == 1
    u = await ok(ctx, "update_task", id=tid, due="2026-09-27 10:00")   # перенесли в прошлое
    assert "прошёл" in u["note"] and u["overdue"] is True and await active_reminders(ctx.db, tid) == []


async def test_update_task_project_and_errors(ctx, fake_rem):
    a = await ok(ctx, "create_project", name="Кофейня")
    r = await ok(ctx, "add_task", text="t")
    u = await ok(ctx, "update_task", id=r["id"], project="кофейня")
    assert u["project_id"] == a["id"] and u["project"] == "Кофейня"
    u = await ok(ctx, "update_task", id=r["id"], project="нет")
    assert u["project_id"] is None
    assert "нет задачи" in (await call(ctx, "update_task", id=999, status="done"))["error"]
    assert "числом" in (await call(ctx, "update_task", id="abc", status="done"))["error"]
    bad = await call(ctx, "update_task", id=r["id"], status="почти")
    assert bad["ok"] is False and "todo" in bad["error"]
    assert "нечего менять" in (await call(ctx, "update_task", id=r["id"]))["error"]
    assert (await call(ctx, "update_task", id=r["id"], project="космодром"))["ok"] is False
    u = await ok(ctx, "update_task", id=f"#{r['id']}", status="в работе")
    assert u["status"] == "doing"


async def test_list_tasks(ctx, fake_rem):
    await ok(ctx, "create_project", name="Кофейня")
    t1 = await ok(ctx, "add_task", text="срочно", project="Кофейня", priority=1, due="2026-09-29 10:00")
    await ok(ctx, "add_task", text="потом", project="Кофейня", due="2026-10-20 10:00")
    await ok(ctx, "add_task", text="без срока")
    t4 = await ok(ctx, "add_task", text="сделано", project="Кофейня")
    await ok(ctx, "update_task", id=t4["id"], status="done")
    await ok(ctx, "add_task", text="просрочено", due="2026-09-20 10:00", priority=3)
    r = await ok(ctx, "list_tasks")
    assert [i["text"] for i in r["items"]] == ["срочно", "потом", "без срока", "просрочено"]
    assert r["items"][0]["project"] == "Кофейня" and r["items"][3]["overdue"] is True
    r = await ok(ctx, "list_tasks", project="кофейня")
    assert [i["text"] for i in r["items"]] == ["срочно", "потом"] and r["project"]["name"] == "Кофейня"
    r = await ok(ctx, "list_tasks", status="done")
    assert [i["text"] for i in r["items"]] == ["сделано"]
    r = await ok(ctx, "list_tasks", status="all", project="Кофейня")
    assert [i["text"] for i in r["items"]] == ["срочно", "потом", "сделано"]
    r = await ok(ctx, "list_tasks", due_within_days=3)
    assert [i["text"] for i in r["items"]] == ["срочно", "просрочено"] and r["items"][0]["id"] == t1["id"]
    r = await ok(ctx, "list_tasks", due_within_days=0)
    assert [i["text"] for i in r["items"]] == ["просрочено"]
    assert (await call(ctx, "list_tasks", status="почти"))["ok"] is False
    assert (await call(ctx, "list_tasks", project="космодром"))["ok"] is False
    assert (await call(ctx, "list_tasks", due_within_days="скоро"))["ok"] is False
    empty = await ok(ctx, "list_tasks", status="dropped")
    assert empty["count"] == 0 and empty["note"] == "задач нет"


async def test_due_tasks(ctx, fake_rem):
    p = await ok(ctx, "create_project", name="Кофейня")
    dead = await ok(ctx, "create_project", name="Мёртвый")
    await ok(ctx, "add_task", text="просрочено", project="Кофейня", due="2026-09-27 10:00")
    await ok(ctx, "add_task", text="сегодня вечером", due="2026-09-28 18:00")
    await ok(ctx, "add_task", text="послезавтра", due="2026-09-30 10:00")
    await ok(ctx, "add_task", text="без срока")
    done = await ok(ctx, "add_task", text="давно сделано", due="2026-09-20 10:00")
    await ok(ctx, "update_task", id=done["id"], status="done")
    await ok(ctx, "add_task", text="из брошенного", project=dead["id"], due="2026-09-25 10:00")
    await ok(ctx, "update_project", project=dead["id"], status="dropped")
    rows = await pr.due_tasks(ctx.db, ctx.tz, 1)
    assert [r["text"] for r in rows] == ["просрочено", "сегодня вечером"]
    assert rows[0]["overdue"] is True and rows[0]["project"] == "Кофейня" and rows[0]["project_id"] == p["id"]
    assert rows[1]["overdue"] is False and rows[1]["project"] is None
    assert rows[1]["due_local"] == "пн 28.09 18:00"
    rows = await pr.due_tasks(ctx.db, ctx.tz, days=3)
    assert [r["text"] for r in rows] == ["просрочено", "сегодня вечером", "послезавтра"]
    assert [r["text"] for r in await pr.due_tasks(ctx.db, ctx.tz, 0)] == ["просрочено"]


async def test_real_reminders_integration(ctx):
    """С настоящим модулем напоминаний (если он уже есть)."""
    importlib.import_module(REM)
    r = await ok(ctx, "add_task", text="Сдать отчёт", due="2026-09-30 18:00")
    assert r["reminder_id"] is not None
    row = await ctx.db.fetchone("SELECT * FROM reminders WHERE id=?", (r["reminder_id"],))
    assert (row["kind"], row["ref_type"], row["ref_id"], row["status"]) == ("task", "task", r["id"], "active")
    assert row["local_start"] == "2026-09-30T18:00:00" and "Сдать отчёт" in row["text"]
    await ok(ctx, "update_task", id=r["id"], status="done")
    row = await ctx.db.fetchone("SELECT * FROM reminders WHERE id=?", (r["reminder_id"],))
    assert row["status"] == "cancelled"


def test_tools_registered():
    for name in ("create_project", "list_projects", "get_project", "update_project",
                 "add_task", "update_task", "list_tasks"):
        t = tb.REGISTRY[name]
        assert t.description and t.parameters["type"] == "object"
    assert tb.REGISTRY["add_task"].parameters["required"] == ["text"]
