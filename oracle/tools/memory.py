"""Долговременная память: факты о владельце, собственные позиции бота, дневник, follow-up'ы.

Это ядро «свободного ИИ со своей точкой зрения»:
  • факты (`facts`) — что бот знает о владельце; дубли не плодятся: точный повтор только
    освежает запись, почти-повтор (та же мысль другими словами) заменяет старую формулировку —
    новая информация побеждает;
  • позиции (`opinions`) — мнения, которые бот сам сформулировал; смена позиции пишется в
    историю вместе с тем, что переубедило (why_changed); инструмент не даёт сменить позицию молча;
  • дневник (`journal`) — заметки ночной рефлексии, последняя идёт в системный промпт;
  • follow-up — бот ставит себе напоминание и потом пишет владельцу первым.

Поиск — общий индекс `search_index` (виды "fact" и "opinion").
"""
from __future__ import annotations

import importlib
import json
import logging
import re
from typing import Any

from .. import timeutil
from ..db import normalize_text, stem
from .base import ToolContext, tool

log = logging.getLogger("oracle.tools.memory")

CATEGORIES = ("general", "person", "preference", "plan", "work", "health", "other")
SOURCES = ("chat", "reflection", "manual")
FACT_MIN, FACT_MAX = 3, 500
JOURNAL_MAX = 4000
HISTORY_MAX = 20
NEAR_DUP = 0.75          # порог Жаккара по основам: почти-повтор факта
TOPIC_MATCH = 0.6        # порог Жаккара по основам: та же тема позиции

_CAT_ALIASES = {
    "общее": "general", "общая": "general", "разное": "general", "misc": "general",
    "человек": "person", "люди": "person", "people": "person", "persons": "person", "contact": "person",
    "семья": "person", "family": "person", "друзья": "person", "relationship": "person",
    "предпочтение": "preference", "предпочтения": "preference", "вкусы": "preference",
    "preferences": "preference", "likes": "preference", "like": "preference",
    "план": "plan", "планы": "plan", "plans": "plan", "цель": "plan", "цели": "plan", "goal": "plan",
    "goals": "plan",
    "работа": "work", "карьера": "work", "бизнес": "work", "job": "work", "career": "work",
    "business": "work",
    "здоровье": "health", "спорт": "health", "medical": "health",
    "другое": "other", "прочее": "other",
}
# русские подписи категорий — кладутся в индекс, чтобы recall("работа") находил факты о работе
_CAT_RU = {
    "general": "общее", "person": "человек люди", "preference": "предпочтения",
    "plan": "планы цели", "work": "работа", "health": "здоровье", "other": "другое",
}
# служебные слова — не учитываются при сравнении формулировок (отрицание тоже: «не любит кофе»
# против «любит кофе» — это одна и та же тема, и новая формулировка должна заменить старую)
_STOP = frozenset({
    "и", "в", "во", "на", "с", "со", "к", "ко", "у", "о", "об", "обо", "по", "за", "из", "от", "до",
    "а", "но", "же", "ли", "не", "ни", "что", "как", "это", "то", "бы", "для", "при", "про", "или",
    "the", "a", "an", "of", "to", "and", "or", "is",
})
_WORD = re.compile(r"[0-9a-zа-я]+", re.I)
_PUNCT = re.compile(r"[^\w\s]|_", re.U)
# мусорные слова из запросов «что ты помнишь про…», «найди…»
_FILLER = frozenset({
    "про", "о", "об", "обо", "насчет", "что", "ты", "я", "мне", "мой", "моя", "мое", "мои", "моего",
    "помнишь", "знаешь", "запомнил", "найди", "вспомни", "скажи", "там", "был", "была", "было", "были",
    "такое", "такой", "это", "всё", "все", "уже",
})


# ── мелочи ───────────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return timeutil.iso(timeutil.now_utc())


def _as_id(v: Any, what: str = "id") -> int:
    try:
        if isinstance(v, bool):
            raise TypeError
        return int(str(v).strip().lstrip("#№").strip())
    except (TypeError, ValueError):
        raise ValueError(f"{what} должен быть числом, а пришло «{v}»") from None


def _as_int(v: Any, default: int) -> int:
    try:
        if v is None or isinstance(v, bool):
            return default
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default


def norm(s: Any) -> str:
    """Для сравнения формулировок: нижний регистр, ё→е, без пунктуации, одиночные пробелы."""
    t = normalize_text(str(s or ""))
    t = _PUNCT.sub(" ", t)
    return " ".join(t.split())


def stems(s: Any) -> set[str]:
    """Множество основ значимых слов (служебные выкидываются; если остались одни они — берём все)."""
    words = _WORD.findall(normalize_text(str(s or "")))
    good = {stem(w) for w in words if w not in _STOP}
    return good or {stem(w) for w in words}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def clean_category(v: Any) -> str:
    """Категория от модели → одна из CATEGORIES (незнакомая → general)."""
    s = norm(v)
    if s in CATEGORIES:
        return s
    return _CAT_ALIASES.get(s, "general")


def _strict_category(v: Any) -> str | None:
    """Для фильтра: None/""/all → None; незнакомая → ValueError."""
    s = norm(v)
    if not s or s in {"all", "все", "любая", "any"}:
        return None
    if s in CATEGORIES:
        return s
    if s in _CAT_ALIASES:
        return _CAT_ALIASES[s]
    raise ValueError(f"категории «{v}» нет — бывают: {', '.join(CATEGORIES)}")


def clean_confidence(v: Any, default: int = 60) -> int:
    """Уверенность 0..100: 70, "70", "70%", 0.7 → 70; мусор → default."""
    if v is None or isinstance(v, bool):
        return default
    try:
        x = float(str(v).strip().rstrip("%").strip().replace(",", "."))
    except (TypeError, ValueError):
        return default
    if x != x:  # NaN
        return default
    if 0 < x < 1:           # модель прислала долю
        x *= 100
    return max(0, min(100, int(round(x))))


def _local_date(iso_s: str | None, tz) -> str | None:
    d = timeutil.from_iso(iso_s)
    return d.astimezone(tz).strftime("%d.%m.%Y") if d else None


def _strip_filler(query: str) -> str:
    words = [w for w in _WORD.findall(normalize_text(query)) if w not in _FILLER]
    return " ".join(words) if words else query


def _fact_body(content: str, category: str) -> str:
    return f"{content} {category} {_CAT_RU.get(category, '')}"


def _history(row: dict) -> list[dict]:
    try:
        h = json.loads(row.get("history") or "[]")
    except (TypeError, ValueError):
        return []
    return h if isinstance(h, list) else []


# ── факты ────────────────────────────────────────────────────────────────────
async def get_fact(db, fact_id: Any) -> dict | None:
    try:
        fid = int(fact_id)
    except (TypeError, ValueError):
        return None
    return await db.fetchone("SELECT * FROM facts WHERE id=?", (fid,))


async def add_fact(db, content: str, category: str = "general", source: str = "chat") -> tuple[int, bool]:
    """Запомнить факт → (id, создан ли). Дубли не плодит.

    Точный повтор (без учёта регистра, ё и пунктуации) — только освежает updated_at.
    Почти-повтор (Жаккар по основам ≥ 0.75 среди кандидатов поиска) — старая формулировка
    заменяется новой: новая информация побеждает.
    """
    text = " ".join(str(content or "").split())
    if len(text) < FACT_MIN:
        raise ValueError("факт пустой или слишком короткий — сформулируй, что именно запомнить")
    if len(text) > FACT_MAX:
        raise ValueError(f"факт длиннее {FACT_MAX} символов — сформулируй короче, одной мыслью")
    cat = clean_category(category)
    src = source if source in SOURCES else "chat"
    now = _now_iso()

    # 1) точный повтор
    key = norm(text)
    for r in await db.fetchall("SELECT id, content, category FROM facts"):
        if norm(r["content"]) == key:
            new_cat = cat if cat != "general" else r["category"]
            await db.execute("UPDATE facts SET updated_at=?, category=? WHERE id=?", (now, new_cat, r["id"]))
            if new_cat != r["category"]:
                await db.index_put("fact", r["id"], _fact_body(r["content"], new_cat))
            return int(r["id"]), False

    # 2) почти-повтор: та же мысль другими словами → новая формулировка вместо старой
    mine = stems(text)
    best: tuple[float, dict] | None = None
    for fid in await db.search("fact", text, 5):
        r = await get_fact(db, fid)
        if r is None:
            continue
        j = jaccard(mine, stems(r["content"]))
        if j >= NEAR_DUP and (best is None or j > best[0]):
            best = (j, r)
    if best is not None:
        r = best[1]
        new_cat = cat if cat != "general" else r["category"]
        await db.execute("UPDATE facts SET content=?, category=?, updated_at=? WHERE id=?",
                         (text, new_cat, now, r["id"]))
        await db.index_put("fact", r["id"], _fact_body(text, new_cat))
        log.debug("факт #%s переформулирован: %r → %r", r["id"], r["content"], text)
        return int(r["id"]), False

    # 3) новый
    fid = await db.execute(
        "INSERT INTO facts(content, category, source, created_at, updated_at) VALUES(?,?,?,?,?)",
        (text, cat, src, now, now))
    await db.index_put("fact", fid, _fact_body(text, cat))
    return fid, True


async def delete_fact(db, fact_id: Any) -> dict | None:
    """Забыть факт (и убрать из индекса). → удалённая строка или None, если не было."""
    row = await get_fact(db, fact_id)
    if row is None:
        return None
    await db.execute("DELETE FROM facts WHERE id=?", (row["id"],))
    await db.index_delete("fact", row["id"])
    return row


async def all_facts(db, category: str | None = None, limit: int = 200) -> list[dict]:
    """Факты (для /memory и list_facts): по категории, внутри — старые первыми."""
    lim = max(1, _as_int(limit, 200))
    if category:
        return await db.fetchall(
            "SELECT * FROM facts WHERE category=? ORDER BY id LIMIT ?", (category, lim))
    return await db.fetchall("SELECT * FROM facts ORDER BY category, id LIMIT ?", (lim,))


async def facts_for_prompt(db, query: str = "", limit: int = 40) -> list[dict]:
    """Факты для системного промпта. Влезают все — все (по категориям); иначе — найденные по
    запросу (до половины лимита) плюс недавно обновлённые. Элементы: id, content, category."""
    limit = _as_int(limit, 40)
    if limit <= 0:
        return []
    total = int(await db.scalar("SELECT COUNT(*) FROM facts") or 0)
    if total <= limit:
        return await db.fetchall("SELECT id, content, category FROM facts ORDER BY category, id")
    out: list[dict] = []
    seen: set[int] = set()
    q = str(query or "").strip()
    if q and limit // 2 > 0:
        for fid in await db.search("fact", q, limit // 2):
            if fid in seen:
                continue
            r = await db.fetchone("SELECT id, content, category FROM facts WHERE id=?", (fid,))
            if r:
                out.append(r)
                seen.add(fid)
    for r in await db.fetchall(
            "SELECT id, content, category FROM facts ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,)):
        if len(out) >= limit:
            break
        if r["id"] not in seen:
            out.append(r)
            seen.add(r["id"])
    return out[:limit]


# ── позиции бота ─────────────────────────────────────────────────────────────
async def get_opinion(db, opinion_id: Any) -> dict | None:
    try:
        oid = int(str(opinion_id).strip().lstrip("#"))
    except (TypeError, ValueError):
        return None
    return await db.fetchone("SELECT * FROM opinions WHERE id=?", (oid,))


async def find_opinion(db, topic: str, opinion_id: Any = None) -> dict | None:
    """Существующая позиция: по id, иначе по точной теме, иначе по похожей теме (Жаккар ≥ 0.6)."""
    if opinion_id not in (None, ""):
        row = await get_opinion(db, opinion_id)
        if row is not None:
            return row
    key = norm(topic)
    if not key:
        return None
    for r in await db.fetchall("SELECT * FROM opinions ORDER BY id"):
        if norm(r["topic"]) == key:
            return r
    mine = stems(topic)
    best: tuple[float, dict] | None = None
    for oid in await db.search("opinion", topic, 3):
        r = await get_opinion(db, oid)
        if r is None:
            continue
        j = jaccard(mine, stems(r["topic"]))
        if j >= TOPIC_MATCH and (best is None or j > best[0]):
            best = (j, r)
    return best[1] if best else None


async def upsert_opinion(db, *, topic: str, stance: str, reasons: str = "", confidence: Any = 60,
                         opinion_id: Any = None, why_changed: str = "") -> dict:
    """Записать/обновить позицию бота. Смена позиции (по смыслу формулировки) → прежняя уходит в
    историю {stance, reasons, confidence, why_changed, changed_at} (не больше 20 записей).
    Возвращает строку opinions + "changed", "created" (+ "previous_stance" при смене)."""
    topic_s = " ".join(str(topic or "").split())
    stance_s = str(stance or "").strip()
    if not topic_s:
        raise ValueError("пустая тема позиции")
    if not stance_s:
        raise ValueError("пустая позиция — сформулируй, что ты думаешь")
    topic_s, stance_s = topic_s[:200], stance_s[:2000]
    reasons_s = str(reasons or "").strip()[:3000]
    why = str(why_changed or "").strip()[:1000]
    conf = clean_confidence(confidence)
    now = _now_iso()

    by_id = await get_opinion(db, opinion_id) if opinion_id not in (None, "") else None
    row = by_id or await find_opinion(db, topic_s)
    if row is None:
        oid = await db.execute(
            "INSERT INTO opinions(topic, stance, reasons, confidence, history, created_at, updated_at) "
            "VALUES(?,?,?,?,'[]',?,?)", (topic_s, stance_s, reasons_s, conf, now, now))
        await db.index_put("opinion", oid, f"{topic_s} {stance_s}")
        out = await get_opinion(db, oid) or {}
        out.update(changed=False, created=True)
        return out

    new_topic = topic_s if by_id is not None else row["topic"]
    changed = norm(stance_s) != norm(row["stance"])
    history = _history(row)
    if changed:
        history.append({"stance": row["stance"], "reasons": row["reasons"], "confidence": row["confidence"],
                        "why_changed": why, "changed_at": now})
        history = history[-HISTORY_MAX:]
        new_reasons = reasons_s
    else:
        new_reasons = reasons_s or row["reasons"]
    await db.execute(
        "UPDATE opinions SET topic=?, stance=?, reasons=?, confidence=?, history=?, updated_at=? WHERE id=?",
        (new_topic, stance_s, new_reasons, conf, json.dumps(history, ensure_ascii=False), now, row["id"]))
    await db.index_put("opinion", row["id"], f"{new_topic} {stance_s}")
    out = await get_opinion(db, row["id"]) or {}
    out.update(changed=changed, created=False)
    if changed:
        out["previous_stance"] = row["stance"]
    return out


def _opinion_public(r: dict, *, short: bool = False) -> dict:
    h = _history(r)
    out = {"id": r["id"], "topic": r["topic"], "stance": r["stance"], "confidence": r["confidence"]}
    if not short:
        out["reasons"] = r.get("reasons") or ""
        out["changes"] = len(h)
        if h and isinstance(h[-1], dict) and h[-1].get("why_changed"):
            out["last_change"] = h[-1]["why_changed"]
    return out


async def opinions_for_prompt(db, query: str = "", limit: int = 8) -> list[dict]:
    """Позиции для промпта: сначала относящиеся к запросу, потом самые уверенные/свежие.
    Элементы: id, topic, stance, confidence, reasons."""
    limit = _as_int(limit, 8)
    if limit <= 0:
        return []
    out: list[dict] = []
    seen: set[int] = set()
    q = str(query or "").strip()
    if q:
        for oid in await db.search("opinion", q, limit):
            r = await db.fetchone(
                "SELECT id, topic, stance, confidence, reasons FROM opinions WHERE id=?", (oid,))
            if r and r["id"] not in seen:
                out.append(r)
                seen.add(r["id"])
    if len(out) < limit:
        for r in await db.fetchall(
                "SELECT id, topic, stance, confidence, reasons FROM opinions "
                "ORDER BY confidence DESC, updated_at DESC, id DESC LIMIT ?", (limit + len(seen),)):
            if len(out) >= limit:
                break
            if r["id"] not in seen:
                out.append(r)
                seen.add(r["id"])
    return out[:limit]


# ── дневник ──────────────────────────────────────────────────────────────────
async def add_journal(db, content: str) -> int:
    """Запись дневника (ночная рефлексия). Пусто → ValueError; длиннее 4000 — обрезается."""
    text = str(content or "").strip()
    if not text:
        raise ValueError("пустая запись дневника")
    if len(text) > JOURNAL_MAX:
        text = text[: JOURNAL_MAX - 1].rstrip() + "…"
    return await db.execute("INSERT INTO journal(content, created_at) VALUES(?,?)", (text, _now_iso()))


async def latest_journal(db) -> dict | None:
    return await db.fetchone("SELECT * FROM journal ORDER BY id DESC LIMIT 1")


# ── инструменты для модели ───────────────────────────────────────────────────
def _fact_public(r: dict, tz) -> dict:
    return {"id": r["id"], "content": r["content"], "category": r["category"],
            "updated": _local_date(r.get("updated_at"), tz)}


@tool("remember",
      "Запомнить устойчивый факт о владельце: люди (имя + кто это: «Маша — жена», «Петров — начальник»), "
      "даты, планы и цели, предпочтения, работа, здоровье, привычки. Одна мысль — один вызов, "
      "формулируй самодостаточно, в третьем лице («Не пьёт молоко — непереносимость лактозы»). "
      "Не для мелочей и сиюминутного («сейчас устал») и не для того, что уже есть в памяти. "
      "Если факт уточняет или опровергает прежний — просто запомни новую формулировку: "
      "почти-повтор заменит старую сам.",
      {"content": {"type": "string", "description": "факт одной фразой, до 500 символов"},
       "category": {"type": "string", "enum": list(CATEGORIES),
                    "description": "person — люди, preference — вкусы и предпочтения, plan — планы и цели, "
                                   "work — работа и дела, health — здоровье, general/other — прочее"}},
      required=["content"])
async def t_remember(ctx: ToolContext, *, content: str, category: str | None = None) -> dict:
    fid, created = await add_fact(ctx.db, content, category or "general", "chat")
    row = await get_fact(ctx.db, fid) or {}
    out = {"ok": True, "id": fid, "created": created, "content": row.get("content"),
           "category": row.get("category")}
    if not created:
        out["note"] = "такое уже было в памяти — обновил запись, новой не заводил"
    return out


@tool("recall",
      "Поискать в своей памяти: факты о владельце и свои позиции по теме. Зови, когда он ссылается "
      "на то, чего нет в контексте («помнишь, я говорил…», «как зовут моего…», «что ты думаешь про X» "
      "— а позиции перед глазами нет). Поиск по словам с учётом окончаний; query — 1–4 ключевых слова.",
      {"query": {"type": "string", "description": "ключевые слова: имя, тема, предмет"},
       "limit": {"type": "integer", "description": "сколько фактов максимум, по умолчанию 10"}},
      required=["query"])
async def t_recall(ctx: ToolContext, *, query: str, limit: Any = None) -> dict:
    lim = max(1, min(30, _as_int(limit, 10)))
    q = _strip_filler(str(query or ""))
    facts: list[dict] = []
    for fid in await ctx.db.search("fact", q, lim):
        r = await get_fact(ctx.db, fid)
        if r:
            facts.append(_fact_public(r, ctx.tz))
    opinions: list[dict] = []
    for oid in await ctx.db.search("opinion", q, 5):
        r = await get_opinion(ctx.db, oid)
        if r:
            opinions.append(_opinion_public(r))
    out: dict[str, Any] = {"ok": True, "query": query, "facts": facts, "opinions": opinions}
    if not facts and not opinions:
        out["note"] = "ничего не нашёл — так и скажи, не выдумывай; можно спросить его и запомнить"
    return out


@tool("forget",
      "Забыть факт о владельце по id (он попросил забыть, факт устарел или ошибочный). "
      "id видно в памяти в системном промпте (#id) или через recall / list_facts.",
      {"fact_id": {"type": "integer"}}, required=["fact_id"])
async def t_forget(ctx: ToolContext, *, fact_id: Any) -> dict:
    fid = _as_id(fact_id, "fact_id")
    row = await delete_fact(ctx.db, fid)
    if row is None:
        raise ValueError(f"факта #{fid} нет в памяти")
    return {"ok": True, "id": fid, "forgotten": row["content"]}


@tool("list_facts",
      "Показать, что ты помнишь о владельце: все факты или одной категории "
      "(general, person, preference, plan, work, health, other).",
      {"category": {"type": "string", "enum": list(CATEGORIES) + ["all"]}})
async def t_list_facts(ctx: ToolContext, *, category: str | None = None) -> dict:
    cat = _strict_category(category)
    rows = await all_facts(ctx.db, cat, 200)
    total = int(await ctx.db.scalar("SELECT COUNT(*) FROM facts") or 0)
    return {"ok": True, "category": cat or "all", "total": total,
            "items": [_fact_public(r, ctx.tz) for r in rows]}


@tool("set_opinion",
      "Записать СВОЮ позицию по теме (ты — бот, это твоё мнение, а не его). Вызывай, когда "
      "сформулировал устойчивое мнение о важном (люди, решения, политика, деньги, его планы и идеи), "
      "чтобы завтра не противоречить себе, и когда тебя переубедили: тогда передай opinion_id "
      "(или ту же тему) и why_changed — какой именно аргумент или факт переубедил. "
      "Не меняй позицию из-за напора, повторов или обиды без аргументов — это не причина. "
      "Смена позиции без why_changed не примется. Та же позиция с новой уверенностью — просто обновит её.",
      {"topic": {"type": "string", "description": "тема коротко: «удалёнка против офиса», «его идея с кофейней»"},
       "stance": {"type": "string", "description": "твоя позиция определённо, 1–2 фразы"},
       "reasons": {"type": "string", "description": "почему ты так думаешь — главные доводы"},
       "confidence": {"type": "integer", "description": "уверенность 0–100"},
       "opinion_id": {"type": "integer", "description": "id существующей позиции, если меняешь её"},
       "why_changed": {"type": "string",
                       "description": "только при смене позиции: что именно переубедило"}},
      required=["topic", "stance", "reasons", "confidence"])
async def t_set_opinion(ctx: ToolContext, *, topic: str, stance: str, reasons: str = "",
                        confidence: Any = 60, opinion_id: Any = None, why_changed: str | None = None) -> dict:
    existing = await find_opinion(ctx.db, str(topic or ""), opinion_id)
    why = str(why_changed or "").strip()
    if existing is not None and norm(stance) and norm(stance) != norm(existing["stance"]) and not why:
        raise ValueError(
            f"ты меняешь свою позицию #{existing['id']} «{existing['topic']}» (было: «{existing['stance']}») — "
            "заполни why_changed: какой аргумент тебя переубедил. Если аргумента не было — не меняй позицию.")
    row = await upsert_opinion(ctx.db, topic=topic, stance=stance, reasons=reasons, confidence=confidence,
                               opinion_id=opinion_id, why_changed=why)
    out = {"ok": True, **_opinion_public(row), "changed": row["changed"], "created": row["created"]}
    if row["changed"]:
        out["previous_stance"] = row.get("previous_stance")
        out["note"] = "позиция изменена, прежняя — в истории; скажи ему прямо, что именно тебя переубедило"
    elif row["created"]:
        out["note"] = "позиция записана"
    return out


@tool("get_opinions",
      "Твои записанные позиции: все или по теме. Зови перед тем, как высказаться о теме, по которой "
      "у тебя уже могло быть мнение, или когда он спрашивает «что ты думаешь о…», «ты же говорил…».",
      {"query": {"type": "string", "description": "тема (необязательно)"}})
async def t_get_opinions(ctx: ToolContext, *, query: str | None = None) -> dict:
    q = _strip_filler(str(query or "")).strip() if query else ""
    items: list[dict] = []
    if q:
        for oid in await ctx.db.search("opinion", q, 10):
            r = await get_opinion(ctx.db, oid)
            if r:
                items.append({**_opinion_public(r), "match": True})
        if items:
            return {"ok": True, "query": query, "items": items}
    rows = await ctx.db.fetchall(
        "SELECT * FROM opinions ORDER BY confidence DESC, updated_at DESC, id DESC LIMIT 30")
    items = [{**_opinion_public(r), **({"match": False} if q else {})} for r in rows]
    out: dict[str, Any] = {"ok": True, "query": query or "", "items": items}
    if q:
        out["note"] = ("по теме записанной позиции нет — ниже все остальные; если по смыслу ни одна "
                       "не подходит, у тебя пока нет позиции")
    elif not rows:
        out["note"] = "записанных позиций пока нет"
    return out


@tool("schedule_followup",
      "Поставить СЕБЕ напоминание вернуться к теме и написать владельцу первым: он что-то откладывает, "
      "ждёт результата (ответ по работе, анализы, решение), проект стоит, обещал сделать и молчит. "
      "В срок ты сам напишешь ему по этому поводу. when — локальное 'YYYY-MM-DD HH:MM' "
      "(выбирай разумно: через день-три, в дневное время). about — заметка себе: о чём спросить и зачем.",
      {"when": {"type": "string", "description": "локальное 'YYYY-MM-DD HH:MM'"},
       "about": {"type": "string",
                 "description": "о чём вернуться, с контекстом: «спросить, отправил ли резюме — тянет неделю»"}},
      required=["when", "about"])
async def t_schedule_followup(ctx: ToolContext, *, when: str, about: str) -> dict:
    text = " ".join(str(about or "").split())
    if not text:
        raise ValueError("о чём вернуться? about пустой")
    try:
        rem = importlib.import_module(f"{__package__}.reminders")
    except ImportError:
        raise ValueError("модуль напоминаний недоступен — follow-up сейчас не поставить") from None
    row = await rem.create_reminder(ctx, text=text, when=when, kind="followup", nag=False)
    return {"ok": True, "id": row["id"], "when": row.get("when_local"), "about": text}
