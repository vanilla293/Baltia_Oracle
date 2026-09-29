"""Идеи владельца: честная оценка, поиск по смыслу, заметки, глубокий разбор в фоне.

Идея сохраняется вместе с быстрой оценкой бота (score 1–10, evaluation). Поиск — индекс
`search_index` вида "idea" (название + текст + теги + оценка); если совпадений мало, к ним
добавляются свежие идеи, чтобы модель выбрала подходящую по смыслу сама.

Глубокий разбор (`deep_evaluate`) идёт в фоне глубокой моделью: статус на время — 'thinking',
прежний запоминается в kv `idea_deep:<id>` вместе с меткой процесса, итог — владельцу
отдельным сообщением «🧠 Додумал идею #id …» и событием в диалог. Разбор прошлого процесса
(бот упал или перезапустился посреди) не считается идущим: `recover_interrupted` при старте
возвращает таким идеям прежний статус, а повторный запуск не ждёт протухания пометки.
"""
from __future__ import annotations

import importlib
import logging
import re
import uuid
from datetime import timedelta
from typing import Any

from .. import timeutil
from ..db import normalize_text
from ..llm import LLMError
from .base import Buttons, OutItem, ToolContext, tool

log = logging.getLogger("oracle.tools.ideas")

STATUSES = ("new", "thinking", "in_work", "parked", "dropped", "done")
STATUS_RU = {"new": "новая", "thinking": "обдумывается", "in_work": "в работе", "parked": "отложена",
             "dropped": "брошена", "done": "сделана"}
_STATUS_ALIASES = {
    "новая": "new", "новое": "new", "open": "new",
    "думаю": "thinking", "обдумываю": "thinking", "обдумывается": "thinking", "обдумать": "thinking",
    "подумать": "thinking",
    "в работе": "in_work", "в работу": "in_work", "делаю": "in_work", "делать": "in_work", "work": "in_work",
    "in work": "in_work", "in progress": "in_work", "in_progress": "in_work", "active": "in_work",
    "отложена": "parked", "отложено": "parked", "отложить": "parked", "в стол": "parked", "пауза": "parked",
    "на паузе": "parked", "paused": "parked", "later": "parked",
    "брошена": "dropped", "бросить": "dropped", "отказ": "dropped", "не делать": "dropped", "забыть": "dropped",
    "мертвая": "dropped", "cancelled": "dropped", "canceled": "dropped",
    "сделана": "done", "сделано": "done", "готово": "done", "реализована": "done", "запущена": "done",
    "completed": "done", "complete": "done",
}
TITLE_MAX = 200
CONTENT_MAX = 20_000
EVAL_MAX = 8_000
TAGS_MAX = 10
DEEP_KEY = "idea_deep:{}"
_BOOT = uuid.uuid4().hex         # метка этого процесса: разбор с чужой меткой — от прошлого запуска, он мёртв
_EMPTY = {"", "none", "null", "нет", "-", "—"}
_WORD = re.compile(r"[^\W_]+")   # буквы любых алфавитов и цифры: «Jānis» — одно слово
# мусор из запросов «найди мою идею про…»
_FILLER = frozenset({
    "идея", "идеи", "идею", "идей", "идеей", "идеям", "идеях", "мою", "мои", "моя", "мой", "моих",
    "про", "о", "об", "обо", "насчет", "найди", "найти", "покажи", "ту", "та", "которая", "которую",
    "была", "было", "был", "где", "что", "там", "какая", "какую", "с", "со", "я", "мне", "говорил",
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
        raise ValueError(f"{what} идеи должен быть числом, а пришло «{v}»") from None


def _as_int(v: Any, default: int) -> int:
    try:
        if v is None or isinstance(v, bool):
            return default
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default


def _given(v: Any) -> bool:
    """Модель передала значение (не None и не пустая строка)."""
    return v is not None and not (isinstance(v, str) and not v.strip())


def clean_status(v: Any, *, allow_all: bool = False) -> str:
    s = " ".join(normalize_text(str(v or "")).replace("-", " ").split())
    s = _STATUS_ALIASES.get(s, s.replace(" ", "_"))
    ok = STATUSES + (("all",) if allow_all else ())
    if s in {"все", "любой", "любые", "any"} and allow_all:
        s = "all"
    if s not in ok:
        raise ValueError(f"статуса «{v}» у идей нет — бывают: {', '.join(STATUSES)}")
    return s


def clean_score(v: Any) -> int:
    """Оценка 1..10: 7, "7", "7/10", 6.5 → 7 (вне диапазона — прижимается к краю)."""
    if isinstance(v, bool) or v is None:
        raise ValueError("оценка — число от 1 до 10")
    s = str(v).strip().replace(",", ".")
    m = re.match(r"^(-?\d+(?:\.\d+)?)\s*(?:/\s*10|из\s*10)?$", s)
    if not m:
        raise ValueError(f"оценка — число от 1 до 10, а пришло «{v}»")
    x = float(m.group(1))
    return max(1, min(10, int(x + 0.5) if x >= 0 else 1))


def clean_tags(v: Any) -> str:
    """Теги списком или строкой через запятую → 'тег1, тег2' (нижний регистр, без повторов и #)."""
    if v is None:
        return ""
    items = [str(x) for x in v] if isinstance(v, (list, tuple, set)) else re.split(r"[,;\n]", str(v))
    out: list[str] = []
    for t in items:
        t = " ".join(normalize_text(t).strip().lstrip("#").split())[:40]
        if t and t not in _EMPTY and t not in out:
            out.append(t)
    return ", ".join(out[:TAGS_MAX])


def _clean_text(v: Any, what: str, limit: int) -> str:
    s = str(v or "").strip()
    if not s:
        raise ValueError(f"пустое {what} идеи")
    return s[:limit]


def _local_date(iso_s: str | None, tz) -> str | None:
    d = timeutil.from_iso(iso_s)
    return d.astimezone(tz).strftime("%d.%m.%Y") if d else None


def _strip_filler(query: str) -> str:
    words = [w for w in _WORD.findall(normalize_text(query)) if w not in _FILLER]
    return " ".join(words) if words else query


def _snippet(s: str, n: int = 200) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def deep_buttons(idea_id: int) -> Buttons:
    return [[("🧠 Додумать глубоко", f"idea:deep:{int(idea_id)}")]]


def idea_line(row: dict, tz: Any = None) -> str:
    """'#3 · Кофейня у вокзала · 6/10 · в работе' — для списков (/ideas)."""
    score = f"{row['score']}/10" if row.get("score") is not None else "без оценки"
    s = f"#{row['id']} · {row['title']} · {score} · {STATUS_RU.get(row.get('status'), row.get('status'))}"
    if row.get("tags"):
        s += f" · {row['tags']}"
    return s


def idea_card(row: dict, tz: Any) -> str:
    """Полная карточка (/idea N): текст, оценка, глубокий разбор."""
    parts = [f"💡 {idea_line(row, tz)}", "", str(row.get("content") or "").strip()]
    if row.get("evaluation"):
        parts += ["", "Оценка: " + str(row["evaluation"]).strip()]
    if row.get("deep_evaluation"):
        parts += ["", "🧠 Глубокий разбор:", str(row["deep_evaluation"]).strip()]
    created = _local_date(row.get("created_at"), tz)
    if created:
        parts += ["", f"Записана {created}"]
    return "\n".join(parts)


# ── база ─────────────────────────────────────────────────────────────────────
async def load_idea(db, idea_id: Any) -> dict | None:
    try:
        iid = int(str(idea_id).strip().lstrip("#"))
    except (TypeError, ValueError):
        return None
    return await db.fetchone("SELECT * FROM ideas WHERE id=?", (iid,))


async def recent_ideas(db, status: str | None = None, limit: int = 15) -> list[dict]:
    lim = max(1, min(100, _as_int(limit, 15)))
    if status:
        return await db.fetchall("SELECT * FROM ideas WHERE status=? ORDER BY updated_at DESC, id DESC LIMIT ?",
                                 (status, lim))
    return await db.fetchall("SELECT * FROM ideas ORDER BY updated_at DESC, id DESC LIMIT ?", (lim,))


async def _reindex(db, row: dict) -> None:
    body = " ".join(str(row.get(k) or "") for k in ("title", "content", "tags", "evaluation"))
    await db.index_put("idea", row["id"], body)


async def _idea_or_fail(db, idea_id: Any) -> dict:
    iid = _as_id(idea_id)
    row = await load_idea(db, iid)
    if row is None:
        raise ValueError(f"идеи #{iid} нет — найди нужную через find_ideas или list_ideas")
    return row


async def _similar(db, row: dict, limit: int = 3) -> list[dict]:
    """Похожие по названию уже сохранённые идеи (чтобы не плодить одну и ту же)."""
    from .memory import jaccard, stems
    mine = stems(row["title"])
    out = []
    for iid in await db.search("idea", row["title"], 6):
        if iid == row["id"]:
            continue
        r = await load_idea(db, iid)
        if r and jaccard(mine, stems(r["title"])) >= 0.3:
            out.append({"id": r["id"], "title": r["title"], "score": r["score"], "status": r["status"]})
        if len(out) >= limit:
            break
    return out


# ── глубокий разбор ──────────────────────────────────────────────────────────
DEEP_SYSTEM = """\
Ты — жёсткий, но честный практик и инвестор: видел сотни проектов, сам запускал и хоронил свои. Ты искренне хочешь, чтобы у автора идеи всё получилось, — поэтому не льстишь: лесть стоит ему денег и лет. Но и не топишь ради позы: сначала пойми идею в её лучшем виде, потом бей по самому слабому месту.

Разбери идею по пунктам, коротко и по существу, без воды:
1. Суть — одной фразой: что это и для кого.
2. Чья боль и насколько она реальная — кто страдает или платит сейчас, как часто, чем решает сейчас. Отличай «было бы прикольно» от «за это платят».
3. Что уже существует — аналоги и конкуренты, которые знаешь. Честно оговорись: в сети ты не искал, это по памяти и могло устареть.
4. Сильные стороны — что реально играет за идею, включая его личные преимущества (навыки, связи, деньги, место), если они известны.
5. Что убьёт — топ-3 риска по убыванию опасности, конкретно: не «конкуренция», а «X уже делает это бесплатно».
6. Как проверить за 1–2 недели — самый дешёвый эксперимент, который может идею убить или подтвердить: что сделать, сколько стоит деньгами и часами, какой результат считать успехом.
7. Первый шаг завтра — одно конкретное действие на час-два.
8. Оценка — отдельной строкой ровно так: «Оценка: N/10», и одна-две фразы почему. Шкала честная: большинство идей — 4–6; 8+ только при подтверждённой боли, понятном пути к деньгам и его преимуществе; 1–3 — идея мертва или вредна.
9. Вердикт — отдельной строкой: «Вердикт: делать», «Вердикт: докрутить — что именно» или «Вердикт: в стол» — и почему.

Пиши живым русским, на «ты», как умный друг-практик, а не консультант. Формат — простой текст для Telegram: пункты с номерами, без таблиц и без заголовков через #. Цифры не выдумывай: рынок, цены и сроки — только как прикидку с пояснением, на чём она основана."""


def parse_score(text: str) -> int | None:
    """'Оценка: 6/10' (последняя такая строка) → 6; без явной оценки — последнее 'N/10'; нет → None."""
    t = str(text or "")
    found = re.findall(r"оценк[аиу][^\d\n]{0,20}?(\d{1,2}(?:[.,]\d+)?)\s*(?:/|из)\s*10", t, re.I)
    if not found:
        found = re.findall(r"(?<![\d.,])(\d{1,2}(?:[.,]\d+)?)\s*/\s*10(?!\d)", t)
    for raw in reversed(found):
        x = float(raw.replace(",", "."))
        if 0 < x <= 10:
            return max(1, min(10, int(x + 0.5)))
    return None


def parse_verdict(text: str) -> str | None:
    """Строка 'Вердикт: …' → её содержание (без markdown), не длиннее 200 символов."""
    found = re.findall(r"^[\s\d.)*_#-]*вердикт[\s*_:—–-]+(.+)$", str(text or ""), re.I | re.M)
    if not found:
        return None
    v = found[-1].replace("*", "").replace("_", " ").strip(" .")
    return _snippet(v, 200) or None


_STARTING: set[tuple[str, int]] = set()   # (база, идея), для которых разбор сейчас запускается (у каждого человека свои номера идей)


async def _begin_deep(db, row: dict) -> str:
    """Пометить идею «обдумывается», запомнив прежний статус. → прежний статус."""
    key = DEEP_KEY.format(row["id"])
    prev = row["status"]
    mark = await db.kv_get(key)
    if row["status"] == "thinking" and isinstance(mark, dict) and mark.get("prev") in STATUSES:
        prev = mark["prev"]          # разбор уже идёт или прошлый не вернул статус
    await db.kv_set(key, {"prev": prev, "at": _now_iso(), "boot": _BOOT})
    await db.execute("UPDATE ideas SET status='thinking' WHERE id=?", (row["id"],))
    return prev


async def _end_deep(db, idea_id: int, prev: str) -> None:
    """Вернуть прежний статус (если его не сменили, пока думали) и снять пометку."""
    await db.execute("UPDATE ideas SET status=? WHERE id=? AND status='thinking'", (prev, idea_id))
    await db.execute("DELETE FROM kv WHERE key=?", (DEEP_KEY.format(idea_id),))


async def recover_interrupted(db) -> list[dict]:
    """При старте: разборы, начатые прошлым процессом (упал, перезапустили), уже не идут — вернуть
    идеям прежний статус и снять пометки. → [{id, title, status}] восстановленных (можно сказать владельцу)."""
    out: list[dict] = []
    for r in await db.fetchall("SELECT key, value FROM kv WHERE key LIKE 'idea_deep:%'"):
        key = r["key"]
        try:
            iid = int(key.split(":", 1)[1])
        except (IndexError, ValueError):
            await db.kv_delete(key)
            continue
        mark = await db.kv_get(key)
        if isinstance(mark, dict) and mark.get("boot") == _BOOT:
            continue                                       # идёт в этом процессе
        prev = mark.get("prev") if isinstance(mark, dict) else None
        prev = prev if prev in STATUSES and prev != "thinking" else "new"
        n = await db.execute("UPDATE ideas SET status=? WHERE id=? AND status='thinking'", (prev, iid))
        await db.kv_delete(key)
        if n:
            row = await load_idea(db, iid)
            if row:
                out.append({"id": iid, "title": row["title"], "status": prev})
                log.info("идея #%s: разбор прервался перезапуском — статус возвращён в %s", iid, prev)
    return out


async def deep_busy(ctx: ToolContext, idea_id: Any) -> bool:
    """Идёт ли уже глубокий разбор этой идеи (пометка этого процесса, свежая, статус 'thinking')."""
    row = await load_idea(ctx.db, idea_id)
    if row is None or row["status"] != "thinking":
        return False
    mark = await ctx.db.kv_get(DEEP_KEY.format(row["id"]))
    if not isinstance(mark, dict) or mark.get("boot") != _BOOT:
        return False                 # пометки нет или она от прошлого запуска — там разбор уже умер
    try:
        at = timeutil.from_iso(mark.get("at"))
    except (TypeError, ValueError):
        return False
    if at is None:
        return False
    stale = max(1200.0, 2 * float(getattr(ctx.cfg, "llm_deep_timeout", 600.0) or 600.0) + 120.0)
    return timeutil.now_utc() - at < timedelta(seconds=stale)


async def _notify(ctx: ToolContext, text: str) -> None:
    notifier = getattr(ctx.services, "notifier", None)
    if notifier is None:
        return
    try:
        await notifier.send(text)
    except Exception:  # недоставка не должна ронять фоновую задачу
        log.exception("не смог отправить итог разбора идеи")


def _deep_user_prompt(ctx: ToolContext, row: dict, facts: list[dict]) -> str:
    parts = [f"Идея #{row['id']}: {row['title']}", "", "Описание:", str(row["content"]).strip()]
    if row.get("tags"):
        parts += ["", f"Теги: {row['tags']}"]
    if row.get("evaluation"):
        sc = f" ({row['score']}/10)" if row.get("score") is not None else ""
        parts += ["", f"Твоя первая быстрая оценка в разговоре{sc}:", str(row["evaluation"]).strip()]
    if row.get("deep_evaluation"):
        parts += ["", "Прошлый глубокий разбор (идея могла измениться — пересмотри честно, не повторяй по инерции):",
                  _snippet(row["deep_evaluation"], 3000)]
    if facts:
        parts += ["", "Что ты знаешь о владельце (используй, если относится к делу: навыки, деньги, время, "
                      "город, связи, здоровье):"]
        parts += [f"- [{f.get('category') or 'general'}] {f['content']}" for f in facts]
    owner = (getattr(ctx.cfg, "owner_name", "") or "").strip()
    if owner:
        parts += ["", f"Автор идеи: {owner}"]
    parts += ["", f"Сейчас: {timeutil.fmt_now_for_prompt(ctx.tz)}"]
    return "\n".join(parts)


async def deep_evaluate(ctx: ToolContext, idea_id: int) -> str:
    """Глубокий разбор идеи глубокой моделью. Пишет deep_evaluation и score (если нашлась
    «Оценка: N/10»), шлёт владельцу «🧠 Додумал идею #id …», кладёт событие в диалог.
    Статус на время — 'thinking', потом прежний. Ошибка модели → владельцу «не смог додумать»,
    возвращается текст ошибки. Никогда не бросает (кроме отмены задачи)."""
    try:
        iid = _as_id(idea_id)
    except ValueError as e:
        return str(e)
    db = ctx.db
    row = await load_idea(db, iid)
    if row is None:
        return f"идеи #{iid} нет"
    title = row["title"]
    prev = await _begin_deep(db, row)
    err: str | None = None
    text = ""
    try:
        facts: list[dict] = []
        try:
            from .memory import facts_for_prompt
            facts = await facts_for_prompt(db, f"{row['title']} {row['content']} {row.get('tags') or ''}", 25)
        except Exception:  # без памяти разбор всё равно возможен
            log.exception("не смог достать факты для разбора идеи #%s", iid)
        text = (await ctx.llm.ask(DEEP_SYSTEM, _deep_user_prompt(ctx, row, facts), deep=True) or "").strip()
        if not text:
            err = "модель вернула пустой разбор"
    except LLMError as e:
        err = str(e) or "модель не ответила"
    except Exception as e:
        log.exception("разбор идеи #%s упал", iid)
        err = f"внутренняя ошибка: {type(e).__name__}: {e}"
    finally:
        await _end_deep(db, iid, prev)

    if err is not None:
        await _notify(ctx, f"🧠 Не смог додумать идею #{iid} «{title}»: {err}")
        try:
            await db.add_message("event", f"Бот не смог додумать идею #{iid} «{title}»: {err}", "system")
        except Exception:
            log.exception("не смог записать событие")
        return f"не смог додумать: {err}"

    score = parse_score(text)
    verdict = parse_verdict(text)
    await db.execute("UPDATE ideas SET deep_evaluation=?, score=COALESCE(?, score), updated_at=? WHERE id=?",
                     (text, score, _now_iso(), iid))
    await _notify(ctx, f"🧠 Додумал идею #{iid} «{title}»\n\n{text}")
    summary = f"Бот додумал идею #{iid} «{title}», итог: оценка {score}/10" if score is not None \
        else f"Бот додумал идею #{iid} «{title}», итог: оценка не выставлена"
    if verdict:
        summary += f", вердикт: {verdict}"
    summary += ". Полный разбор ушёл владельцу отдельным сообщением (текст — get_idea)."
    await db.add_message("event", summary, "system")
    return text


# ── инструменты для модели ───────────────────────────────────────────────────
def _brief(row: dict, tz, *, match: bool | None = None) -> dict:
    out = {"id": row["id"], "title": row["title"], "score": row["score"], "status": row["status"],
           "tags": row["tags"], "created": _local_date(row.get("created_at"), tz),
           "snippet": _snippet(row["content"], 200)}
    if match is not None:
        out["match"] = match
    return out


@tool("save_idea",
      "Сохранить идею владельца (бизнес, проект, продукт, текст, что угодно). Сначала честно оцени "
      "её про себя — что в ней сильное, что её убьёт, какой первый шаг, оценка 1–10 (большинство идей 4–6, "
      "это нормально; 8+ — редкость) — и сохрани вместе с этой оценкой (evaluation, score). Сам разбор "
      "владельцу пиши в ответе после того, как инструмент вернул ok. "
      "content — сама идея его словами, подробно, ничего не теряя; evaluation — твоя оценка коротко.",
      {"title": {"type": "string", "description": "короткое название, 2–6 слов: «Кофейня у вокзала»"},
       "content": {"type": "string", "description": "суть идеи подробно, его словами"},
       "evaluation": {"type": "string",
                      "description": "твоя честная оценка: сильное, что убьёт, первый шаг — 2–6 фраз"},
       "score": {"type": "integer", "description": "оценка 1–10"},
       "tags": {"type": "array", "items": {"type": "string"},
                "description": "2–4 тега: сфера, тип (общепит, приложение, контент…)"}},
      required=["title", "content", "evaluation", "score"])
async def t_save_idea(ctx: ToolContext, *, title: str, content: str, evaluation: str = "",
                      score: Any = None, tags: Any = None) -> dict:
    title_s = " ".join(_clean_text(title, "название", TITLE_MAX * 2).split())[:TITLE_MAX]
    content_s = _clean_text(content, "описание", CONTENT_MAX)
    eval_s = str(evaluation or "").strip()[:EVAL_MAX]
    sc = clean_score(score)
    tags_s = clean_tags(tags)
    now = _now_iso()
    iid = await ctx.db.execute(
        "INSERT INTO ideas(title, content, evaluation, score, tags, status, created_at, updated_at) "
        "VALUES(?,?,?,?,?,'new',?,?)", (title_s, content_s, eval_s, sc, tags_s, now, now))
    row = await load_idea(ctx.db, iid) or {}
    await _reindex(ctx.db, row)
    ctx.outbox.append(OutItem(kind="text", text=f"💡 Идея #{iid} «{title_s}» сохранена · {sc}/10",
                              buttons=deep_buttons(iid)))
    out: dict[str, Any] = {"ok": True, "id": iid, "title": title_s, "score": sc, "tags": tags_s}
    similar = await _similar(ctx.db, row)
    if similar:
        out["similar"] = similar
        out["note"] = "похожие идеи уже есть — скажи ему, может, это развитие старой"
    return out


@tool("find_ideas",
      "Найти сохранённые идеи по словам («найди мою идею про кофейню» → query «кофейня»). "
      "Поиск учитывает окончания; если совпадений мало, добавляются свежие идеи с match=false — "
      "выбери подходящую по смыслу сама. Полный текст и разбор — get_idea(id).",
      {"query": {"type": "string", "description": "ключевые слова: тема, предмет, название"},
       "limit": {"type": "integer", "description": "сколько максимум, по умолчанию 8"}},
      required=["query"])
async def t_find_ideas(ctx: ToolContext, *, query: str, limit: Any = None) -> dict:
    lim = max(1, min(30, _as_int(limit, 8)))
    q = _strip_filler(str(query or ""))
    items: list[dict] = []
    seen: set[int] = set()
    for iid in await ctx.db.search("idea", q, lim):
        row = await load_idea(ctx.db, iid)
        if row and row["id"] not in seen:
            items.append(_brief(row, ctx.tz, match=True))
            seen.add(row["id"])
    if len(items) < 3:
        for row in await recent_ideas(ctx.db, None, lim + len(seen)):
            if len(items) >= lim:
                break
            if row["id"] not in seen:
                items.append(_brief(row, ctx.tz, match=False))
                seen.add(row["id"])
    out: dict[str, Any] = {"ok": True, "query": query, "items": items,
                           "note": "выбери подходящую по смыслу; полный текст — get_idea"}
    if not items:
        out["note"] = "идей пока не сохранено"
    elif not any(i["match"] for i in items):
        out["note"] = ("точных совпадений нет, ниже свежие идеи — выбери подходящую по смыслу "
                       "или честно скажи, что такой нет; полный текст — get_idea")
    return out


@tool("get_idea",
      "Полная карточка идеи по id: текст, теги, статус, твоя оценка и глубокий разбор (если был).",
      {"id": {"type": "integer"}}, required=["id"])
async def t_get_idea(ctx: ToolContext, *, id: Any) -> dict:
    row = await _idea_or_fail(ctx.db, id)
    out = {"ok": True, "id": row["id"], "title": row["title"], "content": row["content"],
           "evaluation": row["evaluation"], "deep_evaluation": row["deep_evaluation"], "score": row["score"],
           "tags": row["tags"], "status": row["status"], "status_ru": STATUS_RU.get(row["status"]),
           "created": _local_date(row.get("created_at"), ctx.tz),
           "updated": _local_date(row.get("updated_at"), ctx.tz)}
    if not row["deep_evaluation"]:
        out["note"] = "глубокого разбора ещё не было — можно предложить deep_think_idea"
    return out


@tool("update_idea",
      "Изменить идею по id. Передавай только то, что меняется: title, content (заменяет текст целиком), "
      "note — дописать к идее новую мысль или развитие (добавится в конец с датой; предпочтительнее, "
      "чем переписывать content), status (new — новая, thinking — обдумывает, in_work — взялся делать, "
      "parked — отложил, dropped — отказался, done — сделано), tags — новый полный список тегов, "
      "score — новая оценка 1–10, если после обсуждения ты её честно пересмотрел.",
      {"id": {"type": "integer"},
       "title": {"type": "string"},
       "content": {"type": "string"},
       "status": {"type": "string", "enum": list(STATUSES)},
       "tags": {"type": "array", "items": {"type": "string"}},
       "note": {"type": "string", "description": "что дописать к идее"},
       "score": {"type": "integer", "description": "новая оценка 1–10"}},
      required=["id"])
async def t_update_idea(ctx: ToolContext, *, id: Any, title: str | None = None, content: str | None = None,
                        status: str | None = None, tags: Any = None, note: str | None = None,
                        score: Any = None) -> dict:
    row = await _idea_or_fail(ctx.db, id)
    new = dict(row)
    changed: list[str] = []
    if _given(title):
        new["title"] = " ".join(str(title).split())[:TITLE_MAX]
        changed.append("title")
    if _given(content):
        new["content"] = str(content).strip()[:CONTENT_MAX]
        changed.append("content")
    if _given(note):
        day = ctx.now_local().strftime("%d.%m.%Y")
        new["content"] = f"{new['content'].rstrip()}\n\n[{day}] {str(note).strip()[:CONTENT_MAX]}"
        changed.append("note")
    if _given(status):
        new["status"] = clean_status(status)
        changed.append("status")
    if tags is not None and not (isinstance(tags, str) and not tags.strip()):
        new["tags"] = clean_tags(tags)
        changed.append("tags")
    if _given(score):
        new["score"] = clean_score(score)
        changed.append("score")
    if not changed:
        raise ValueError("нечего менять — передай хотя бы одно поле: title, content, note, status, tags, score")
    now = _now_iso()
    await ctx.db.execute(
        "UPDATE ideas SET title=?, content=?, status=?, tags=?, score=?, updated_at=? WHERE id=?",
        (new["title"], new["content"], new["status"], new["tags"], new["score"], now, row["id"]))
    if "status" in changed:          # статус сменили руками — прежний статус из пометки разбора устарел
        await ctx.db.execute("DELETE FROM kv WHERE key=?", (DEEP_KEY.format(row["id"]),))
    fresh = await load_idea(ctx.db, row["id"]) or new
    await _reindex(ctx.db, fresh)
    return {"ok": True, "id": fresh["id"], "title": fresh["title"], "status": fresh["status"],
            "tags": fresh["tags"], "score": fresh["score"], "changed": changed}


@tool("list_ideas",
      "Список идей, свежие сверху: id, название, оценка, статус. status — фильтр "
      "(new, thinking, in_work, parked, dropped, done); без него — все.",
      {"status": {"type": "string", "enum": list(STATUSES) + ["all"]},
       "limit": {"type": "integer", "description": "по умолчанию 15"}})
async def t_list_ideas(ctx: ToolContext, *, status: str | None = None, limit: Any = None) -> dict:
    st = clean_status(status, allow_all=True) if _given(status) else "all"
    rows = await recent_ideas(ctx.db, None if st == "all" else st, _as_int(limit, 15))
    counts = {r["status"]: r["n"] for r in await ctx.db.fetchall(
        "SELECT status, COUNT(*) AS n FROM ideas GROUP BY status")}
    items = [{k: v for k, v in _brief(r, ctx.tz).items() if k != "snippet"} for r in rows]
    out: dict[str, Any] = {"ok": True, "status": st, "items": items, "counts": counts}
    if not items:
        out["note"] = "идей нет" if st == "all" else f"идей со статусом {st} нет"
    return out


@tool("delete_idea",
      "Удалить идею насовсем по id — только если он прямо просит удалить. "
      "Если он просто передумал её делать — лучше update_idea со status=dropped (останется в архиве).",
      {"id": {"type": "integer"}}, required=["id"])
async def t_delete_idea(ctx: ToolContext, *, id: Any) -> dict:
    row = await _idea_or_fail(ctx.db, id)
    await ctx.db.execute("DELETE FROM ideas WHERE id=?", (row["id"],))
    await ctx.db.index_delete("idea", row["id"])
    await ctx.db.execute("DELETE FROM kv WHERE key=?", (DEEP_KEY.format(row["id"]),))
    try:
        rem = importlib.import_module(f"{__package__}.reminders")
        await rem.cancel_by_ref(ctx.db, "idea", row["id"])
    except ImportError:
        pass
    except Exception:
        log.exception("не смог снять напоминания идеи #%s", row["id"])
    return {"ok": True, "id": row["id"], "title": row["title"], "deleted": True}


@tool("deep_think_idea",
      "Додумать идею глубоко: в фоне, думающей моделью — рынок и аналоги, чья боль, топ-3 риска, "
      "как проверить за 1–2 недели и сколько стоит, первый шаг, оценка и вердикт. Займёт несколько минут, "
      "разбор придёт ему отдельным сообщением. Зови, когда он просит «додумай», «разбери серьёзно», "
      "«подумай глубже над идеей». Идея должна быть уже сохранена (save_idea).",
      {"id": {"type": "integer"}}, required=["id"])
async def t_deep_think_idea(ctx: ToolContext, *, id: Any) -> dict:
    row = await _idea_or_fail(ctx.db, id)
    busy = {"ok": True, "started": False, "id": row["id"], "title": row["title"],
            "note": "Уже думаю над ней — разбор придёт отдельным сообщением. Скажи ему об этом одной фразой."}
    # проверка и пометка — не атомарны (два await): два нажатия «Додумать» разом прошли бы оба
    key = (str(getattr(ctx.db, "path", "")), int(row["id"]))
    if key in _STARTING:
        return busy
    _STARTING.add(key)
    try:
        if await deep_busy(ctx, row["id"]):
            return busy
        await _begin_deep(ctx.db, row)       # сразу «обдумывается» — повторный вызов не запустит второй разбор
    finally:
        _STARTING.discard(key)
    ctx.services.spawn(deep_evaluate(ctx.child(), row["id"]), name="deep_idea")
    return {"ok": True, "started": True, "id": row["id"], "title": row["title"],
            "note": "Думаю в фоне — пришлю разбор отдельным сообщением. Скажи ему об этом одной фразой."}
