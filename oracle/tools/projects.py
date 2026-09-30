"""Проекты и задачи: большие дела с целью, задачи со сроками и приоритетами.

Срок задачи (`tasks.due_at`) — UTC ISO. Напоминание о сроке — строка reminders `kind='task'`,
`ref_type='task'`, `ref_id=<id задачи>`; модуль reminders импортируется лениво.
Владелец явно отказался от напоминания (remind=false) — отметка в kv `task_noremind:<id>`,
чтобы смена срока или текста не вернула его обратно.

Срок одной датой («к пятнице», «сегодня») — это «до конца дня»: due_at = 23:59 местного, а
напоминание — утром в 09:00 (или в ближайший час, если утро уже прошло).
"""
from __future__ import annotations

import importlib
import logging
import re
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import timeutil
from ..db import normalize_text, search_stems, words
from .base import ToolContext, tool

log = logging.getLogger("oracle.tools.projects")

PROJECT_STATUSES = ("active", "paused", "done", "dropped")
TASK_STATUSES = ("todo", "doing", "done", "dropped")
OPEN_STATUSES = ("todo", "doing")

_PROJECT_ALIASES = {
    "активный": "active", "активен": "active", "в работе": "active", "открыт": "active",
    "пауза": "paused", "на паузе": "paused", "приостановлен": "paused", "отложен": "paused",
    "готово": "done", "готов": "done", "завершен": "done", "закрыт": "done", "сделан": "done",
    "completed": "done", "брошен": "dropped", "заброшен": "dropped", "отменен": "dropped",
    "cancelled": "dropped", "canceled": "dropped",
}
_TASK_ALIASES = {
    "new": "todo", "open": "todo", "новая": "todo", "сделать": "todo", "открыть": "todo",
    "in_progress": "doing", "progress": "doing", "в работе": "doing", "делаю": "doing",
    "complete": "done", "completed": "done", "готово": "done", "сделано": "done", "выполнено": "done",
    "cancelled": "dropped", "canceled": "dropped", "отменена": "dropped", "отмена": "dropped",
    "брошена": "dropped",
}
_PRIORITY_WORDS = {"высокий": 1, "high": 1, "срочно": 1, "обычный": 2, "normal": 2, "средний": 2,
                   "низкий": 3, "low": 3}
_EMPTY = {"", "none", "null", "нет", "-", "—", "без срока"}
_CLOSED_SHOWN = 15
_LIST_LIMIT = 60
END_OF_DAY = time(23, 59)          # срок одной датой — до конца дня
DAY_REMIND = time(9, 0)            # …а напоминание о нём — утром


# ── мелочи ───────────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return timeutil.iso(timeutil.now_utc())


def _as_id(v: Any, what: str = "id") -> int:
    try:
        if isinstance(v, bool):
            raise TypeError
        return int(str(v).strip().lstrip("#"))
    except (TypeError, ValueError):
        raise ValueError(f"{what} должен быть числом, а пришло «{v}»") from None


def _as_bool(v: Any, default: bool | None = None) -> bool | None:
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    s = normalize_text(str(v)).strip()
    if s in {"1", "true", "yes", "да", "on", "y"}:
        return True
    if s in {"0", "false", "no", "нет", "off", "n"}:
        return False
    return default


def _project_status(v: Any, *, allow_all: bool = False) -> str:
    s = normalize_text(str(v or "")).strip()
    s = _PROJECT_ALIASES.get(s, s)
    ok = PROJECT_STATUSES + (("all",) if allow_all else ())
    if s not in ok:
        raise ValueError(f"статус проекта «{v}» не бывает — нужен один из: {', '.join(ok)}")
    return s


def _task_status(v: Any) -> str:
    s = normalize_text(str(v or "")).strip()
    s = _TASK_ALIASES.get(s, s)
    if s not in TASK_STATUSES:
        raise ValueError(f"статус задачи «{v}» не бывает — нужен один из: {', '.join(TASK_STATUSES)}")
    return s


def _priority(v: Any, default: int = 2) -> int:
    if v is None or v == "":
        return default
    if isinstance(v, str) and normalize_text(v).strip() in _PRIORITY_WORDS:
        return _PRIORITY_WORDS[normalize_text(v).strip()]
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        raise ValueError("priority — 1 (высокий), 2 (обычный) или 3 (низкий)") from None
    return max(1, min(3, n))


def _clean_text(v: Any, what: str, limit: int = 500) -> str:
    s = re.sub(r"\s+", " ", str(v or "")).strip()
    if not s:
        raise ValueError(f"пустое {what}")
    return s[:limit]


def _parse_due(v: Any, tz: ZoneInfo) -> datetime | None:
    """Срок от модели → aware UTC или None («снять срок»). Только дата → до конца дня (23:59)."""
    if v is None:
        return None
    s = str(v).strip()
    if normalize_text(s) in _EMPTY:
        return None
    return timeutil.parse_local(s, tz, default_time=END_OF_DAY).astimezone(timeutil.UTC)


def _remind_at(due: datetime, tz: ZoneInfo) -> datetime | None:
    """Когда напомнить о задаче со сроком `due` (aware UTC). Срок «до конца дня» (23:59) — утром
    в 09:00, а если утро прошло — в ближайший час того же дня; иначе — в сам срок. None — уже поздно."""
    now = timeutil.now_utc()
    local = due.astimezone(tz)
    if local.time().replace(second=0, microsecond=0) != END_OF_DAY:
        return due if due > now else None
    at = datetime.combine(local.date(), DAY_REMIND, tzinfo=tz)
    if at <= now:
        at = (now.astimezone(tz) + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return at if at < due else None


def _is_open(t: dict) -> bool:
    return t.get("status") in OPEN_STATUSES


def _task_sort_key(t: dict) -> tuple:
    """Открытые сверху, дальше по приоритету, по сроку (без срока — в конце)."""
    return (0 if _is_open(t) else 1, int(t.get("priority") or 2),
            t.get("due_at") is None, t.get("due_at") or "", int(t["id"]))


def _task_public(t: dict, tz: ZoneInfo, now: datetime, *, with_project: bool = True) -> dict:
    due = timeutil.from_iso(t.get("due_at"))
    out = {"id": t["id"], "text": t["text"], "status": t["status"], "priority": t["priority"],
           "due": timeutil.fmt_local(due, tz) if due else None,
           "overdue": bool(due and _is_open(t) and due < now)}
    if with_project:
        out["project_id"] = t.get("project_id")
        out["project"] = t.get("project_name")
    if t.get("status") == "done" and t.get("done_at"):
        out["done"] = timeutil.fmt_local(t["done_at"], tz)
    return out


# ── проекты ──────────────────────────────────────────────────────────────────
async def _get_project(db, pid: int) -> dict | None:
    return await db.fetchone("SELECT * FROM projects WHERE id=?", (int(pid),))


def _prefer(rows: list[dict]) -> dict:
    """Из нескольких кандидатов — активный и самый свежий."""
    return max(rows, key=lambda r: (r["status"] == "active", r["updated_at"] or "", r["id"]))


async def resolve_project(db, ref: str | int | None) -> dict | None:
    """Проект по id («3», «#3», 3), точному имени (без учёта регистра), поиску по индексу
    или по части имени. Не нашёл → None."""
    if ref is None or isinstance(ref, bool):
        return None
    if isinstance(ref, (int, float)):
        return await _get_project(db, int(ref))
    s = str(ref).strip().strip("«»\"'“”").strip()
    if not s:
        return None
    m = re.fullmatch(r"#?\s*(\d+)", s)
    if m:
        row = await _get_project(db, int(m.group(1)))
        if row:
            return row
    rows = await db.fetchall("SELECT * FROM projects")
    if not rows:
        return None
    key = normalize_text(s)
    bare = re.sub(r"^проект\w*\s+", "", key).strip() or key       # «проект Кофейня» → «кофейня»
    exact = [r for r in rows if normalize_text(r["name"]).strip() in (key, bare)]
    if exact:
        return _prefer(exact)
    by_id = {r["id"]: r for r in rows}
    # поиск — OR по префиксам основ: одно общее слово («Ремонт машины» → «Ремонт кухни») или предлог
    # («по» → «бег по утрам») дали бы чужой проект. Кандидат годится, только если в его имени, описании
    # или цели есть КАЖДОЕ значимое слово запроса (≥ 3 букв)
    terms = [search_stems(w) for w in words(bare) if len(w) >= 3]
    if terms:
        for pid in await db.search("project", bare, 3):
            if pid in by_id and _covers(by_id[pid], terms):
                return by_id[pid]
    part = [r for r in rows
            if bare in normalize_text(r["name"])
            or (len(r["name"].strip()) >= 3 and normalize_text(r["name"]).strip() in key)]
    return _prefer(part) if part else None


def _covers(row: dict, terms: list[list[str]]) -> bool:
    """Каждое слово запроса (его основы) — префикс какого-то слова проекта (имя, описание, цель)."""
    ws = words(" ".join(str(row.get(k) or "") for k in ("name", "description", "goal")))
    return all(any(w.startswith(t) for w in ws for t in variants) for variants in terms)


async def _need_project(db, ref: Any) -> dict:
    p = await resolve_project(db, ref)
    if p is None:
        raise ValueError(f"не нашёл проект «{ref}» — list_projects покажет, какие есть")
    return p


async def _index_project_tx(db, c, p: dict) -> None:
    # проект и его текст в поиске пишем одной транзакцией вызывающего (A33): убьют процесс между
    # записями — индекс не разъедется
    await db.index_put_tx(c, "project", int(p["id"]),
                          "\n".join(x for x in (p["name"], p.get("description") or "", p.get("goal") or "") if x))


async def _active_duplicate(db, name: str, exclude_id: int | None = None) -> dict | None:
    for r in await db.fetchall("SELECT * FROM projects WHERE status='active'"):
        if r["id"] != exclude_id and normalize_text(r["name"]).strip() == normalize_text(name).strip():
            return r
    return None


async def _touch_project(db, pid: Any) -> None:
    if pid:
        await db.execute("UPDATE projects SET updated_at=? WHERE id=?", (_now_iso(), int(pid)))


# ── напоминания о сроках (модуль reminders — лениво) ─────────────────────────
def _reminders_module():
    try:
        return importlib.import_module("oracle.tools.reminders")
    except ImportError:
        log.warning("модуль напоминаний недоступен")
        return None


async def _cancel_task_reminders(ctx: ToolContext, tid: int) -> int:
    rem = _reminders_module()
    if rem is None:
        return 0
    try:
        return int(await rem.cancel_by_ref(ctx.db, "task", int(tid)) or 0)
    except Exception:
        log.exception("не смог снять напоминания задачи #%s", tid)
        return 0


async def _create_task_reminder(ctx: ToolContext, task: dict, project_name: str | None) -> tuple[int | None, str]:
    """Напоминание на срок задачи → (id напоминания | None, примечание для модели)."""
    due = timeutil.from_iso(task.get("due_at"))
    if due is None:
        return None, ""
    if due <= timeutil.now_utc():
        return None, "срок уже прошёл — задачу записал, напоминание не ставил"
    at = _remind_at(due, ctx.tz)
    if at is None:
        return None, "срок — сегодня до конца дня, напоминать уже поздно — задачу записал без напоминания"
    rem = _reminders_module()
    if rem is None:
        return None, "напоминания сейчас недоступны — задачу записал без напоминания"
    text = f"Задача: {task['text']}" + (f" [{project_name}]" if project_name else "")
    when = at.astimezone(ctx.tz).strftime("%Y-%m-%d %H:%M")
    try:
        r = await rem.create_reminder(ctx, text=text, when=when, kind="task",
                                      ref_type="task", ref_id=int(task["id"]))
    except ValueError as e:
        return None, f"напоминание не поставил: {e}"
    except Exception as e:   # задача уже записана — не роняем инструмент, иначе модель её задвоит
        log.exception("напоминание для задачи #%s", task.get("id"))
        return None, f"напоминание не поставил: {type(e).__name__}"
    rid = r.get("id") if isinstance(r, dict) else None
    return (int(rid) if rid is not None else None), ""


def _noremind_key(tid: int) -> str:
    return f"task_noremind:{int(tid)}"


async def _sync_task_reminder(ctx: ToolContext, task: dict) -> tuple[int | None, str]:
    """Снять старые напоминания задачи и, если она открыта, со сроком в будущем и владелец
    не отказывался от напоминания, — поставить новое."""
    await _cancel_task_reminders(ctx, task["id"])
    if not _is_open(task) or not task.get("due_at"):
        return None, ""
    if await ctx.db.kv_get(_noremind_key(task["id"]), False):
        return None, ""
    return await _create_task_reminder(ctx, task, task.get("project_name"))


async def _get_task(db, tid: int) -> dict | None:
    return await db.fetchone(
        "SELECT t.*, p.name AS project_name FROM tasks t LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.id=?", (int(tid),))


# ── публичное ────────────────────────────────────────────────────────────────
async def due_tasks(db, tz: ZoneInfo, days: int = 1) -> list[dict]:
    """Открытые задачи со сроком: просроченные + в ближайшие `days` дней, по сроку.

    К строке добавляются "project" (имя или None), "overdue", "due_local". Задачи закрытых
    и брошенных проектов не показываются.
    """
    now = timeutil.now_utc()
    limit = timeutil.iso(now + timedelta(days=max(0, float(days or 0))))
    rows = await db.fetchall(
        "SELECT t.*, p.name AS project FROM tasks t LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.status IN ('todo','doing') AND t.due_at IS NOT NULL AND t.due_at <= ? "
        "AND (p.id IS NULL OR p.status NOT IN ('done','dropped')) "
        "ORDER BY t.due_at, t.priority, t.id", (limit,))
    for r in rows:
        due = timeutil.from_iso(r["due_at"])
        r["overdue"] = bool(due and due < now)
        r["due_local"] = timeutil.fmt_local(due, tz)
    return rows


# ── инструменты: проекты ─────────────────────────────────────────────────────
@tool("create_project",
      "Завести проект — большое дело с целью и задачами. name — короткое узнаваемое название; "
      "description — о чём проект; goal — к чему идём (лучше измеримо и со сроком). "
      "Задачи добавляются потом через add_task.",
      {"name": {"type": "string", "description": "короткое название: «Кофейня», «Ремонт кухни»"},
       "description": {"type": "string", "description": "о чём проект, контекст"},
       "goal": {"type": "string", "description": "цель: что считаем результатом"}},
      required=["name"])
async def t_create_project(ctx: ToolContext, *, name: str, description: str | None = None,
                           goal: str | None = None) -> dict:
    nm = _clean_text(name, "название проекта", 120)
    dup = await _active_duplicate(ctx.db, nm)
    if dup:
        raise ValueError(f"проект «{dup['name']}» уже есть (#{dup['id']}) — дополни его через "
                         f"update_project или add_task, а не заводи второй")
    now = _now_iso()
    async with ctx.db.transaction() as c:          # проект и его строка в поиске — одной транзакцией (A33)
        cur = await c.execute(
            "INSERT INTO projects(name, description, goal, status, created_at, updated_at) "
            "VALUES(?,?,?,'active',?,?)", (nm, (description or "").strip(), (goal or "").strip(), now, now))
        pid = int(cur.lastrowid or 0)
        p = await _get_project(ctx.db, pid)
        await _index_project_tx(ctx.db, c, p)
    out = {"ok": True, "id": pid, "name": nm, "status": "active"}
    if not (goal or "").strip():
        out["hint"] = "цели нет — спроси, что считать результатом"
    out["next"] = "предложи 2–3 первых конкретных шага (add_task) — проект без следующего шага стоит"
    return out


@tool("list_projects",
      "Проекты с числом открытых/закрытых задач, ближайшим сроком и сколько дней без движения (idle_days). "
      "status: active (по умолчанию) | paused | done | dropped | all.",
      {"status": {"type": "string", "enum": ["active", "paused", "done", "dropped", "all"]}})
async def t_list_projects(ctx: ToolContext, *, status: str | None = None) -> dict:
    st = _project_status(status or "active", allow_all=True)
    where, params = ("", ()) if st == "all" else ("WHERE p.status=?", (st,))
    rows = await ctx.db.fetchall(
        "SELECT p.*, "
        "(SELECT COUNT(*) FROM tasks t WHERE t.project_id=p.id AND t.status IN ('todo','doing')) AS open_tasks, "
        "(SELECT COUNT(*) FROM tasks t WHERE t.project_id=p.id AND t.status='done') AS done_tasks, "
        "(SELECT MIN(t.due_at) FROM tasks t WHERE t.project_id=p.id AND t.status IN ('todo','doing') "
        " AND t.due_at IS NOT NULL) AS next_due_at "
        f"FROM projects p {where} "
        "ORDER BY CASE p.status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 WHEN 'done' THEN 2 ELSE 3 END, "
        "p.updated_at DESC, p.id DESC", params)
    now = timeutil.now_utc()
    items = []
    for r in rows:
        upd = timeutil.from_iso(r["updated_at"])
        items.append({
            "id": r["id"], "name": r["name"], "status": r["status"], "goal": r["goal"],
            "open_tasks": r["open_tasks"], "done_tasks": r["done_tasks"],
            "next_due": timeutil.fmt_local(r["next_due_at"], ctx.tz) if r["next_due_at"] else None,
            "idle_days": (now - upd).days if upd else None,
        })
    out: dict = {"ok": True, "count": len(items), "items": items}
    if not items:
        out["note"] = "проектов нет" if st in ("active", "all") else f"проектов со статусом {st} нет"
    return out


@tool("get_project",
      "Проект целиком: описание, цель, статус, задачи (открытые сверху, по приоритету и сроку) и статистика. "
      "project — id или название (можно часть названия).",
      {"project": {"type": "string", "description": "id или название проекта"}},
      required=["project"])
async def t_get_project(ctx: ToolContext, *, project: Any) -> dict:
    p = await _need_project(ctx.db, project)
    tasks = await ctx.db.fetchall("SELECT * FROM tasks WHERE project_id=?", (p["id"],))
    now = timeutil.now_utc()
    tasks.sort(key=_task_sort_key)
    open_t = [t for t in tasks if _is_open(t)]
    closed = sorted((t for t in tasks if not _is_open(t)),
                    key=lambda t: t.get("done_at") or t.get("created_at") or "", reverse=True)
    shown = open_t + closed[:_CLOSED_SHOWN]
    count = {s: sum(1 for t in tasks if t["status"] == s) for s in TASK_STATUSES}
    overdue = sum(1 for t in open_t if t.get("due_at") and timeutil.from_iso(t["due_at"]) < now)
    base = len(tasks) - count["dropped"]
    stats = {"total": len(tasks), "open": len(open_t), "doing": count["doing"], "done": count["done"],
             "dropped": count["dropped"], "overdue": overdue,
             "progress_pct": round(100 * count["done"] / base) if base else 0}
    out = {"ok": True, "id": p["id"], "name": p["name"], "description": p["description"], "goal": p["goal"],
           "status": p["status"], "created": timeutil.fmt_local(p["created_at"], ctx.tz),
           "updated": timeutil.fmt_local(p["updated_at"], ctx.tz),
           "tasks": [_task_public(t, ctx.tz, now, with_project=False) for t in shown], "stats": stats}
    if len(closed) > _CLOSED_SHOWN:
        out["closed_hidden"] = len(closed) - _CLOSED_SHOWN
    if p["status"] == "active" and not open_t:
        out["hint"] = "открытых задач нет — какой следующий шаг?"
    return out


@tool("update_project",
      "Изменить проект: название, описание, цель, статус (active | paused | done | dropped). "
      "project — id или название. Передавай только то, что меняется.",
      {"project": {"type": "string", "description": "id или название проекта"},
       "name": {"type": "string"},
       "description": {"type": "string"},
       "goal": {"type": "string"},
       "status": {"type": "string", "enum": list(PROJECT_STATUSES)}},
      required=["project"])
async def t_update_project(ctx: ToolContext, *, project: Any, name: str | None = None,
                           description: str | None = None, goal: str | None = None,
                           status: str | None = None) -> dict:
    p = await _need_project(ctx.db, project)
    sets: dict[str, Any] = {}
    if name is not None and str(name).strip():
        nm = _clean_text(name, "название проекта", 120)
        if nm != p["name"]:
            dup = await _active_duplicate(ctx.db, nm, exclude_id=p["id"])
            if dup:
                raise ValueError(f"проект «{dup['name']}» уже есть (#{dup['id']})")
            sets["name"] = nm
    if description is not None:
        sets["description"] = str(description).strip()
    if goal is not None:
        sets["goal"] = str(goal).strip()
    if status is not None and str(status).strip():
        st = _project_status(status)
        if st == "active" and p["status"] != "active":
            dup = await _active_duplicate(ctx.db, sets.get("name", p["name"]), exclude_id=p["id"])
            if dup:
                raise ValueError(f"активный проект «{dup['name']}» уже есть (#{dup['id']})")
        sets["status"] = st
    if not sets:
        raise ValueError("нечего менять — передай хотя бы одно поле")
    sets["updated_at"] = _now_iso()
    cols = ", ".join(f"{k}=?" for k in sets)
    async with ctx.db.transaction() as c:          # изменения проекта и переиндексация — одной транзакцией (A33)
        await c.execute(f"UPDATE projects SET {cols} WHERE id=?", (*sets.values(), p["id"]))
        p = await _get_project(ctx.db, p["id"])
        await _index_project_tx(ctx.db, c, p)
    out = {"ok": True, "id": p["id"], "name": p["name"], "status": p["status"], "goal": p["goal"],
           "description": p["description"]}
    if p["status"] in ("done", "dropped"):
        n = await ctx.db.scalar(
            "SELECT COUNT(*) FROM tasks WHERE project_id=? AND status IN ('todo','doing')", (p["id"],))
        if n:
            out["note"] = f"в проекте осталось открытых задач: {n} — закрыть их (update_task) или оставить?"
    return out


# ── инструменты: задачи ──────────────────────────────────────────────────────
@tool("add_task",
      "Добавить задачу — в проект или без него. due — срок, локальное «YYYY-MM-DD HH:MM» "
      "(или «YYYY-MM-DD» — тогда до конца дня, а напомню утром или в ближайший час). Со сроком по умолчанию "
      "ставится напоминание на это время (remind=false — без него). "
      "priority: 1 — высокий, 2 — обычный (по умолчанию), 3 — низкий.",
      {"text": {"type": "string", "description": "что сделать — конкретным действием"},
       "project": {"type": "string", "description": "id или название проекта (необязательно)"},
       "due": {"type": "string", "description": "срок: YYYY-MM-DD HH:MM, локальное"},
       "priority": {"type": "integer", "enum": [1, 2, 3]},
       "remind": {"type": "boolean", "description": "напомнить в срок (по умолчанию да, если срок есть)"}},
      required=["text"])
async def t_add_task(ctx: ToolContext, *, text: str, project: Any = None, due: str | None = None,
                     priority: Any = None, remind: Any = None) -> dict:
    tx = _clean_text(text, "описание задачи")
    p = None
    if project is not None and normalize_text(str(project)).strip() not in _EMPTY:
        p = await resolve_project(ctx.db, project)
        if p is None:
            raise ValueError(f"нет проекта «{project}» — создай его (create_project) или добавь задачу без проекта")
    due_utc = _parse_due(due, ctx.tz)
    prio = _priority(priority)
    want_remind = _as_bool(remind, True)
    tid = await ctx.db.execute(
        "INSERT INTO tasks(project_id, text, status, priority, due_at, created_at) VALUES(?,?,'todo',?,?,?)",
        (p["id"] if p else None, tx, prio, timeutil.iso(due_utc) if due_utc else None, _now_iso()))
    if p:
        await _touch_project(ctx.db, p["id"])
    task = await _get_task(ctx.db, tid)
    out = {"ok": True, "id": tid, "text": tx, "project_id": p["id"] if p else None,
           "project": p["name"] if p else None, "priority": prio, "status": "todo",
           "due": timeutil.fmt_local(due_utc, ctx.tz) if due_utc else None, "reminder_id": None}
    if due_utc is not None:
        if want_remind:
            rid, note = await _create_task_reminder(ctx, task, p["name"] if p else None)
            out["reminder_id"] = rid
            if note:
                out["note"] = note
        else:
            await ctx.db.kv_set(_noremind_key(tid), True)
            out["note"] = "без напоминания — как просил"
    return out


@tool("update_task",
      "Изменить задачу по id: status (todo | doing | done | dropped), текст, срок (YYYY-MM-DD HH:MM; "
      "пустая строка — снять срок), приоритет 1..3, проект (id/название; «нет» — убрать из проекта), "
      "remind (напоминать ли в срок). done и dropped снимают напоминание.",
      {"id": {"type": "integer"},
       "status": {"type": "string", "enum": list(TASK_STATUSES)},
       "text": {"type": "string"},
       "due": {"type": "string", "description": "новый срок YYYY-MM-DD HH:MM или пустая строка"},
       "priority": {"type": "integer", "enum": [1, 2, 3]},
       "project": {"type": "string", "description": "перенести в проект (id или название)"},
       "remind": {"type": "boolean"}},
      required=["id"])
async def t_update_task(ctx: ToolContext, *, id: Any, status: str | None = None, text: str | None = None,
                        due: str | None = None, priority: Any = None, project: Any = None,
                        remind: Any = None) -> dict:
    tid = _as_id(id, "id задачи")
    t = await _get_task(ctx.db, tid)
    if t is None:
        raise ValueError(f"нет задачи #{tid} — посмотри list_tasks")
    sets: dict[str, Any] = {}
    resync = False
    if status is not None and str(status).strip():
        st = _task_status(status)
        if st != t["status"]:
            sets["status"] = st
            sets["done_at"] = _now_iso() if st == "done" else None
            resync = True
    if text is not None and str(text).strip():
        tx = _clean_text(text, "описание задачи")
        if tx != t["text"]:
            sets["text"] = tx
            resync = True
    if due is not None:
        d = _parse_due(due, ctx.tz)
        d_iso = timeutil.iso(d) if d else None
        if d_iso != t["due_at"]:
            sets["due_at"] = d_iso
            resync = True
    if priority is not None and priority != "":
        sets["priority"] = _priority(priority)
    if project is not None:
        if normalize_text(str(project)).strip() in _EMPTY | {"0"}:
            new_pid = None
        else:
            new_pid = (await _need_project(ctx.db, project))["id"]
        if new_pid != t["project_id"]:
            sets["project_id"] = new_pid
            resync = True
    rem_flag = _as_bool(remind)
    if rem_flag is not None:
        await ctx.db.kv_set(_noremind_key(tid), not rem_flag)
        resync = True
    if not sets and rem_flag is None:
        raise ValueError("нечего менять — передай хотя бы одно поле")
    if sets:
        cols = ", ".join(f"{k}=?" for k in sets)
        await ctx.db.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*sets.values(), tid))
    t = await _get_task(ctx.db, tid)
    await _touch_project(ctx.db, t["project_id"])
    out = {"ok": True, **_task_public(t, ctx.tz, timeutil.now_utc())}
    if resync:
        rid, note = await _sync_task_reminder(ctx, t)
        out["reminder_id"] = rid
        if note:
            out["note"] = note
    return out


@tool("list_tasks",
      "Задачи: по проекту (id или название) или все. status: open (по умолчанию: todo+doing) | done | "
      "dropped | all. due_within_days — только со сроком в ближайшие N дней, включая просроченные.",
      {"project": {"type": "string", "description": "id или название проекта (необязательно)"},
       "status": {"type": "string", "enum": ["open", "todo", "doing", "done", "dropped", "all"]},
       "due_within_days": {"type": "integer", "description": "только со сроком не позже чем через N дней"}})
async def t_list_tasks(ctx: ToolContext, *, project: Any = None, status: str | None = None,
                       due_within_days: Any = None) -> dict:
    st = normalize_text(str(status or "open")).strip()
    if st in ("open", "открытые", "active"):
        statuses: tuple[str, ...] = OPEN_STATUSES
    elif st == "all":
        statuses = TASK_STATUSES
    else:
        statuses = (_task_status(st),)
    where = [f"t.status IN ({','.join('?' * len(statuses))})"]
    params: list[Any] = list(statuses)
    p = None
    if project is not None and normalize_text(str(project)).strip() not in _EMPTY:
        p = await _need_project(ctx.db, project)
        where.append("t.project_id=?")
        params.append(p["id"])
    if due_within_days is not None and due_within_days != "":
        try:
            n = float(due_within_days)
        except (TypeError, ValueError):
            raise ValueError("due_within_days — число дней") from None
        where.append("t.due_at IS NOT NULL AND t.due_at <= ?")
        params.append(timeutil.iso(timeutil.now_utc() + timedelta(days=max(0.0, n))))
    rows = await ctx.db.fetchall(
        "SELECT t.*, p.name AS project_name FROM tasks t LEFT JOIN projects p ON p.id=t.project_id "
        f"WHERE {' AND '.join(where)}", params)
    rows.sort(key=_task_sort_key)
    now = timeutil.now_utc()
    items = [_task_public(t, ctx.tz, now) for t in rows[:_LIST_LIMIT]]
    out: dict = {"ok": True, "count": len(rows), "items": items}
    if p:
        out["project"] = {"id": p["id"], "name": p["name"]}
    if len(rows) > _LIST_LIMIT:
        out["more"] = len(rows) - _LIST_LIMIT
    if not rows:
        out["note"] = "задач нет"
    return out
