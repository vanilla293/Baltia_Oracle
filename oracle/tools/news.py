"""Инструменты новостей, веба и погоды + дайджест «правды» и строка погоды для утренней сводки.

Новости модель получает сырьём (заголовок, источник, время, выжимка) вместе с напоминанием,
как их разбирать: факт / заявление / интерпретация, подача разных лагерей, умолчания.
`news_digest` — готовый разбор для рассылки по расписанию и команды /news.
"""
from __future__ import annotations

import logging
from typing import Any

from .. import timeutil
from .base import ToolContext, tool

log = logging.getLogger("oracle.tools.news")

ANALYST_NOTE = ("Разбери как аналитик: факт (кто подтверждает) / заявление (кто и зачем) / интерпретация; "
                "сравни подачу разных лагерей; скажи, о чём молчат. Не пересказывай пропаганду.")
WEATHER_NOT_SET = "погода не настроена: задай WEATHER_CITY в .env или скажи город"
EMPTY_DIGEST = "Лента пустая — источники не ответили. Попробуй позже."
WEB_NEWS_MAX = 8
READ_MAX_CHARS = 8000


def _svc(ctx: ToolContext):
    """Сервис новостей из ctx.services (или новый — и остаётся там же)."""
    svc = getattr(ctx.services, "news", None)
    if svc is None:
        from ..services.news import NewsService
        svc = NewsService(ctx.cfg, ctx.db)
        ctx.services.news = svc
    return svc


def _as_int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def _cut(s: str, n: int) -> str:
    from ..services.news import cut
    return cut(s or "", n)


def _web_time(date: Any, tz) -> str:
    s = str(date or "").strip()
    if not s:
        return ""
    try:
        return timeutil.fmt_local(timeutil.from_iso(s.replace("Z", "+00:00")), tz)
    except (ValueError, TypeError):
        return s[:25]


def _web_items(results: list[dict], tz) -> list[dict]:
    out = []
    for r in results:
        src = str(r.get("source") or "").strip()
        out.append({"source": f"web · {src}" if src else "web", "title": r.get("title") or "",
                    "time": _web_time(r.get("date"), tz), "summary": _cut(r.get("snippet") or "", 300),
                    "url": r.get("url") or ""})
    return out


async def _collect(ctx: ToolContext, topic: str, hours: int, limit: int) -> list[dict]:
    """Ленты (обновить + свежие) и, если есть тема, веб-новости по ней. Элементы в формате инструмента."""
    svc = _svc(ctx)
    try:
        await svc.refresh()
    except Exception as e:   # refresh сам не падает, но страхуемся
        log.warning("обновление лент: %s", e)
    try:
        rows = await svc.latest(hours=hours, query=topic, limit=limit)
    except Exception as e:
        log.warning("чтение новостей: %s", e)
        rows = []
    items = [{"source": r["source"], "title": r["title"], "time": r.get("time_local") or "",
              "summary": _cut(r.get("summary") or "", 300), "url": r["url"]} for r in rows]
    if topic and ctx.cfg.web_search:
        try:
            web = await svc.web_search(topic, max_results=WEB_NEWS_MAX, news=True)
        except Exception as e:
            log.warning("веб-новости: %s", e)
            web = []
        seen = {i["url"] for i in items}
        items.extend(w for w in _web_items(web, ctx.tz) if w["url"] and w["url"] not in seen)
    return items


# ── инструменты ──────────────────────────────────────────────────────────────
@tool(
    "get_news",
    "Свежие новости из RSS-лент разных лагерей (российские госСМИ, независимые, зарубежные). "
    "Без темы — общая лента за период; с темой — только по теме (плюс веб-новости). "
    "Вызывай на «что нового», «что там с …», и прежде чем судить о свежих событиях. "
    "Результат — сырьё: разбирай как аналитик (факт / заявление / интерпретация), не пересказывай пропаганду.",
    {
        "topic": {"type": "string",
                  "description": "тема или ключевые слова: «Иран», «ставка ЦБ», «Калининград»; пусто — все новости"},
        "hours": {"type": "integer", "description": "за сколько последних часов, 1–168; по умолчанию 24"},
        "limit": {"type": "integer", "description": "сколько новостей вернуть, до 60; по умолчанию 30"},
    },
)
async def get_news(ctx: ToolContext, *, topic: str | None = None, hours: int | None = 24,
                   limit: int | None = 30) -> dict:
    topic = str(topic or "").strip()
    hours = _as_int(hours, 24, 1, 168)
    limit = _as_int(limit, 30, 1, 60)
    items = await _collect(ctx, topic, hours, limit)
    if not items:
        if topic:
            note = (f"По теме «{topic}» за {hours} ч в лентах ничего. Ленты могли не ответить — "
                    "попробуй web_search или тему шире. Не выдумывай новости.")
        else:
            note = "Ленты не ответили — новостей нет. Скажи об этом прямо и не выдумывай; можно попробовать web_search."
        return {"ok": True, "count": 0, "items": [], "note": note}
    return {"ok": True, "count": len(items), "items": items, "note": ANALYST_NOTE}


@tool(
    "web_search",
    "Поиск в интернете. Для всего, что могло устареть или чего нет в памяти: факты, цены, люди, компании, "
    "события, курсы, расписания. news=true — только свежие новостные публикации. "
    "Отдаёт заголовок, ссылку и сниппет; страницу целиком читай через read_url.",
    {
        "query": {"type": "string", "description": "поисковый запрос, как в поисковике"},
        "news": {"type": "boolean", "description": "true — искать среди новостей за последнюю неделю"},
        "max_results": {"type": "integer", "description": "сколько результатов, 1–10; по умолчанию 8"},
    },
    required=["query"],
    enabled=lambda cfg: bool(cfg.web_search),
)
async def web_search(ctx: ToolContext, *, query: str, news: bool | None = False,
                     max_results: int | None = 8) -> dict:
    q = str(query or "").strip()
    if not q:
        raise ValueError("пустой поисковый запрос")
    n = _as_int(max_results, 8, 1, 10)
    results = await _svc(ctx).web_search(q, max_results=n, news=bool(news))
    if not results:
        return {"ok": True, "count": 0, "results": [],
                "note": "поиск ничего не дал или недоступен — не выдумывай, скажи как есть"}
    for r in results:
        if r.get("date"):
            r["time"] = _web_time(r["date"], ctx.tz)
    return {"ok": True, "count": len(results), "results": results}


@tool(
    "read_url",
    "Открыть ссылку и прочитать текст страницы (статья, пост, документ). Только публичные http(s)-адреса; "
    "отдаёт заголовок и до 8000 символов текста. Используй, когда он кидает ссылку или после web_search, "
    "чтобы проверить подробности по первоисточнику.",
    {"url": {"type": "string", "description": "полная ссылка: https://…"}},
    required=["url"],
)
async def read_url(ctx: ToolContext, *, url: str) -> dict:
    page = await _svc(ctx).read_url(str(url or ""), max_chars=READ_MAX_CHARS)
    text = page.get("text") or ""
    if not text.strip():
        raise ValueError("на странице нет читаемого текста (возможно, она собирается скриптами)")
    return {"ok": True, "url": page.get("url"), "title": page.get("title") or "", "text": text,
            "truncated": text.endswith("…[обрезано]")}


@tool(
    "get_weather",
    "Погода: сейчас и прогноз на 1–7 дней (Open-Meteo). Без города — там, где живёт владелец (из настроек). "
    "Температура в °C, ветер в м/с, осадки в мм, вероятность осадков в %.",
    {
        "city": {"type": "string", "description": "город, например «Калининград» или «Berlin»; пусто — домашний"},
        "days": {"type": "integer", "description": "на сколько дней прогноз, 1–7; по умолчанию 1 (сегодня)"},
    },
)
async def get_weather(ctx: ToolContext, *, city: str | None = None, days: int | None = 1) -> dict:
    city = str(city or "").strip()
    w = await _svc(ctx).weather(city=city, days=_as_int(days, 1, 1, 7))
    if not w:
        if city:
            return {"ok": False, "error": f"не нашёл погоду для «{city}» — проверь название города или попробуй позже"}
        return {"ok": False, "error": WEATHER_NOT_SET}
    return {"ok": True, **w}


# ── дайджест ─────────────────────────────────────────────────────────────────
DIGEST_SYSTEM = """\
Ты — {name}, личный аналитик {owner}. Пишешь дайджест новостей своим голосом: прямо, живо, без канцелярита и без воды. Ты не пресс-служба и не пересказчик лент — ты разбираешься, что на самом деле произошло.

ПРАВИЛА ПРАВДЫ
- Работаешь ТОЛЬКО с новостями из списка. Никаких фактов, цифр, цитат, имён и ссылок, которых там нет. Чего нет в списке — того не знаешь.
- Факт — то, что подтверждают несколько независимых источников из разных лагерей. Сообщение одной ленты — это заявление, так и пиши: «по данным ТАСС», «утверждает Минобороны», «пишет Meduza».
- Разделяй: факт / заявление (кто сказал и зачем ему это) / интерпретация.
- Сравнивай подачу: российские госСМИ, независимые русскоязычные, зарубежные. Называй приёмы любой стороны: умолчание, эмоциональные ярлыки, анонимные «источники», «эксперты считают», подмена масштаба.
- Ничью пропаганду не пересказывай как истину.

ЧТО ВЫБРАТЬ
5–7 сюжетов, которые важнее всего — для мира и лично для него (учитывай его интересы). Одна история из многих заметок — один сюжет. Криминал, светскую хронику и мелочь пропускай, если это его не касается.

ФОРМАТ СЮЖЕТА
**Заголовок в одну строку**
Факты: что подтверждено и кем.
Кто что говорит: как подают разные лагеря.
Чего не говорят / где манипуляция: умолчания, передёргивания (нечего сказать — пропусти строку).
Тебе это важно, потому что… — только если это правда его касается.

В КОНЦЕ
Мой взгляд: 2–3 предложения твоего собственного мнения о главном — определённо, без «с одной стороны, с другой стороны».

Ссылки — изредка, на ключевые материалы, только из списка, в виде [источник](url). Telegram: короткие абзацы, без таблиц и без заголовков с решётками. Эмодзи не нужны. Без вступлений вроде «Вот дайджест».\
"""


def _format_items(items: list[dict]) -> str:
    lines = []
    for i, it in enumerate(items, 1):
        head = f"[{i}] {it['source']}" + (f" · {it['time']}" if it.get("time") else "")
        block = [head, it["title"]]
        if it.get("summary"):
            block.append(it["summary"])
        block.append(it["url"])
        lines.append("\n".join(block))
    return "\n\n".join(lines)


def _plain_headlines(items: list[dict], err: str) -> str:
    lines = [f"Модель сейчас не отвечает ({err}), так что без разбора — заголовки как есть:", ""]
    lines += [f"• {it['title']} — {it['source']}" for it in items[:15]]
    return "\n".join(lines)


async def news_digest(ctx: ToolContext, topic: str = "") -> str:
    """Дайджест «правды» за сутки: 5–7 сюжетов, факт/заявление/манипуляция, в конце — мнение бота."""
    from ..llm import LLMError

    topic = str(topic or "").strip()
    items = await _collect(ctx, topic, 24, 80)
    if not items:
        return EMPTY_DIGEST
    interests: list[str] = []
    try:
        from .memory import facts_for_prompt
        facts = await facts_for_prompt(ctx.db, topic, limit=25)
        interests = [f"- {f['content']}" for f in facts if f.get("content")]
    except Exception as e:   # модуля памяти может не быть — дайджест и без неё полезен
        log.debug("факты для дайджеста: %s", e)

    cfg = ctx.cfg
    owner = (cfg.owner_name or "").strip()
    system = DIGEST_SYSTEM.format(name=cfg.bot_name, owner=f"владельца ({owner})" if owner else "владельца")
    if topic:
        system += (f"\n\nТЕМА: «{topic}». Бери только сюжеты по теме; если их мало — пусть будет меньше, "
                   "посторонним не добивай.")
    sources = sorted({it["source"] for it in items})
    parts = [f"Сейчас: {timeutil.fmt_now_for_prompt(ctx.tz)}"]
    if topic:
        parts.append(f"Тема: {topic}")
    parts.append("ЕГО ИНТЕРЕСЫ И КОНТЕКСТ (из памяти):\n" + ("\n".join(interests) if interests else "- пока ничего не известно"))
    parts.append(f"НОВОСТИ ЗА СУТКИ ({len(items)} шт., источники: {', '.join(sources)}):\n\n{_format_items(items)}")
    user = "\n\n".join(parts)

    try:
        text = await ctx.llm.ask(system, user, deep=True, timeout=cfg.llm_deep_timeout)
    except LLMError as e:
        log.warning("дайджест в глубоком режиме не вышел (%s) — пробую быстрый", e)
        try:
            text = await ctx.llm.ask(system, user, deep=False, timeout=cfg.llm_fast_timeout)
        except LLMError as e2:
            log.warning("дайджест не вышел: %s", e2)
            return _plain_headlines(items, str(e2))
    text = (text or "").strip()
    return text or _plain_headlines(items, "пустой ответ")


# ── погода одной строкой ─────────────────────────────────────────────────────
def _signed(v: Any) -> str:
    if v is None:
        return "?"
    n = int(v)
    return f"+{n}" if n > 0 else str(n)


def _deg(v: Any) -> str:
    return _signed(v) + "°"


def format_weather_line(w: dict) -> str:
    """'Калининград: сейчас +12°, облачно; днём +9…+14°, дождь 60%'."""
    head = w.get("city") or "Погода"
    now = w.get("now") or {}
    parts = []
    if now.get("temp") is not None:
        cur = f"сейчас {_deg(now['temp'])}"
        if now.get("desc"):
            cur += f", {now['desc']}"
        parts.append(cur)
    days = w.get("days") or []
    if days:
        d = days[0]
        day = f"днём {_signed(d.get('min'))}…{_deg(d.get('max'))}"
        desc = d.get("desc") or ""
        prob = d.get("precip_prob")
        if prob is not None and prob >= 20:
            desc = f"{desc} {prob}%" if d.get("wet") and desc else (f"{desc}, осадки {prob}%" if desc else f"осадки {prob}%")
        if desc:
            day += f", {desc}"
        parts.append(day)
    wind = now.get("wind")
    if wind is not None and wind >= 10:
        parts.append(f"ветер {wind} м/с")
    return (f"{head}: " + "; ".join(parts)) if parts else ""


async def weather_line(ctx: ToolContext) -> str | None:
    """Одна строка погоды для утренней сводки; None — погода не настроена или не ответила."""
    try:
        w = await _svc(ctx).weather(days=1)
    except Exception as e:
        log.warning("погода для сводки: %s", e)
        return None
    if not w:
        return None
    return format_weather_line(w) or None
