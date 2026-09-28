"""ОРАКУЛ // ПИФИЯ — агрегатор новостей (RSS).

Тянет ленты за NEWS_DAYS суток, чистит, дедуплицирует кросс-источниково.
Отдаёт сырой массив новостей — далее ИИ-РАСПРЕДЕЛИТЕЛЬ сам решает,
какую куда привязать и какую выжимку дать.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

from . import config

logger = logging.getLogger("pythia.news")

RSS_FEEDS = {
    # российские
    "rbc_economics": "https://rssexport.rbc.ru/rbcnews/news/30/full.rss",
    "rbc_quote": "https://rssexport.rbc.ru/rbcnews/news/15/full.rss",
    "interfax": "https://www.interfax.ru/rss.asp",
    "kommersant": "https://www.kommersant.ru/RSS/news.xml",
    "tass_econ": "https://tass.ru/rss/economics.xml",
    "vedomosti": "https://www.vedomosti.ru/rss/news",
    "finam": "https://www.finam.ru/analysis/conews/rsspoint/",
    "prime_g": "https://news.google.com/rss/search?q=site:1prime.ru+когда:2д&hl=ru&gl=RU&ceid=RU:ru",
    "ria_econ_g": "https://news.google.com/rss/search?q=site:ria.ru+экономика+OR+рынок+когда:2д&hl=ru&gl=RU&ceid=RU:ru",
    "smartlab_g": "https://news.google.com/rss/search?q=site:smart-lab.ru+когда:2д&hl=ru&gl=RU&ceid=RU:ru",
    # международные
    "bbc_business": "https://feeds.bbci.co.uk/news/business/rss.xml",
    "cnbc": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147",
    "marketwatch": "https://feeds.marketwatch.com/marketwatch/topstories/",
    "reuters_g": "https://news.google.com/rss/search?q=site:reuters.com+markets+OR+economy&hl=en-US",
    "bloomberg_g": "https://news.google.com/rss/search?q=site:bloomberg.com+markets&hl=en-US",
    # тематические через Google News (надёжно отдают 200)
    "g_ru_market": "https://news.google.com/rss/search?q=Мосбиржа+OR+акции+OR+рубль+OR+ЦБ+когда:2д&hl=ru&gl=RU&ceid=RU:ru",
    "g_oilgas": "https://news.google.com/rss/search?q=нефть+OR+газ+OR+Brent+OR+ОПЕК+OR+LNG+когда:2д&hl=ru&gl=RU&ceid=RU:ru",
    "g_natgas": "https://news.google.com/rss/search?q=natural+gas+OR+TTF+OR+Henry+Hub+OR+LNG+when:2d&hl=en-US",
    "g_metals": "https://news.google.com/rss/search?q=золото+OR+gold+OR+silver+OR+ФРС+OR+Fed+когда:2д&hl=ru&gl=RU&ceid=RU:ru",
}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
_HEADERS = {"User-Agent": _UA,
            "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
            "Accept-Language": "ru,en;q=0.8"}

_cache: dict[int, tuple[float, list]] = {}   # days -> (ts, items): раньше кэш
_lock = asyncio.Lock()                       # был один на любую глубину — смена
                                             # NEWS_DAYS отдавала окно не той длины


# Общий keep-alive клиент для всех RSS-запросов: раньше и fetch_news, и каждый
# fetch_for_ticker поднимали новый httpx.AsyncClient (новые TLS-сокеты на разбор).
_http: httpx.AsyncClient | None = None
_http_lock = asyncio.Lock()


async def _client() -> httpx.AsyncClient:
    global _http
    h = _http
    if h is not None and not h.is_closed:
        return h
    async with _http_lock:
        if _http is None or _http.is_closed:
            _http = httpx.AsyncClient(
                headers=_HEADERS, follow_redirects=True,
                timeout=httpx.Timeout(15.0, connect=8.0),
                limits=httpx.Limits(max_keepalive_connections=10,
                                    max_connections=20, keepalive_expiry=60.0))
        return _http


async def aclose() -> None:
    global _http
    h, _http = _http, None
    if h is not None and not h.is_closed:
        try:
            await h.aclose()
        except Exception:
            pass


def _feeds(days: int) -> dict:
    """Ленты с подстановкой реальной глубины в Google-News запросы
    (раньше «когда:2д» было зашито намертво при любом NEWS_DAYS)."""
    d = max(1, min(int(days or 2), 30))
    return {name: url.replace("когда:2д", f"когда:{d}д").replace("when:2d", f"when:{d}d")
            for name, url in RSS_FEEDS.items()}


@dataclass
class News:
    source: str
    title: str
    summary: str
    link: str
    published: str
    ts: float = 0.0

    def to_dict(self) -> dict:
        return {"source": self.source, "title": self.title,
                "summary": self.summary, "link": self.link,
                "published": self.published}


def _clean(html: str) -> str:
    txt = re.sub(r"<[^>]+>", " ", html or "")
    txt = re.sub(r"&[a-z]+;", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def _parse_dt(entry) -> tuple[str, float]:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                dt = datetime(*st[:6], tzinfo=timezone.utc)
                return dt.isoformat(), dt.timestamp()
            except Exception:
                pass
    now = datetime.now(timezone.utc)
    return now.isoformat(), now.timestamp()


async def _fetch(name: str, url: str, client: httpx.AsyncClient,
                 cutoff_ts: float) -> list[News]:
    try:
        r = await client.get(url)
        if r.status_code != 200:
            return []
        feed = feedparser.parse(r.content)
    except Exception as e:
        logger.warning("feed %s failed: %s", name, str(e)[:80])
        return []
    out = []
    for e in feed.entries[:60]:
        iso, ts = _parse_dt(e)
        if ts < cutoff_ts:
            continue
        title = _clean(e.get("title", ""))
        summary = _clean(e.get("summary", "") or e.get("description", ""))
        if not title:
            continue
        out.append(News(source=name, title=title, summary=summary[:600],
                        link=e.get("link", ""), published=iso, ts=ts))
    return out


def _dedupe(items: list[News]) -> list[News]:
    seen: dict[str, News] = {}
    for it in items:
        # ключ — нормализованные первые слова заголовка
        key = re.sub(r"[^a-zа-я0-9 ]", "", it.title.lower())
        key = " ".join(key.split()[:8])
        if key in seen:
            # оставляем более свежую
            if it.ts > seen[key].ts:
                seen[key] = it
        else:
            seen[key] = it
    return sorted(seen.values(), key=lambda x: x.ts, reverse=True)


async def fetch_news(*, force: bool = False, days: int | None = None) -> list[News]:
    days = max(1, min(int(days or config.NEWS_DAYS), 30))
    ttl = config.NEWS_CACHE_SEC
    hit = _cache.get(days)
    if not force and hit and (time.time() - hit[0]) < ttl:
        return list(hit[1])
    async with _lock:
        hit = _cache.get(days)
        if not force and hit and (time.time() - hit[0]) < ttl:
            return list(hit[1])
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
        cl = await _client()
        tasks = [_fetch(n, u, cl, cutoff) for n, u in _feeds(days).items()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        items: list[News] = []
        ok = 0
        for res in results:
            if isinstance(res, list):
                items.extend(res)
                if res:
                    ok += 1
        items = _dedupe(items)
        logger.info("news: %d items from %d/%d feeds", len(items), ok, len(RSS_FEEDS))
        if ok >= len(RSS_FEEDS) // 3:
            _cache[days] = (time.time(), items)
        return items


_tk_cache: dict[str, tuple[float, list]] = {}
_TK_TTL = 900.0


def _short_name(name: str) -> str:
    """«Сбер Банк — обыкн.» → «Сбер Банк»: чистим хвосты для поискового запроса."""
    n = (name or "").split("—")[0].split("(")[0].strip()
    for suf in (" ао", " ап", " обыкн.", " прив."):
        if n.lower().endswith(suf):
            n = n[: -len(suf)].strip()
    return n or name


async def fetch_for_ticker(ticker: str, name: str, *, days: int = 14,
                           fallback_days: int = 90, limit: int = 22,
                           asset_class: str = "share") -> tuple[list[News], str]:
    """ЦЕЛЕВЫЕ новости по компании: Google News по тикеру и названию за `days`.
    Пусто за окно → расширяем до `fallback_days` и берём последние 3
    (пометка «архив»). Возвращает (items, window_note).
    Для фьючерсов запрос по коду контракта (BRQ6, NGN6…) — шум: Google News
    таких кодов не знает, ищем только по имени базового актива."""
    import time as _t
    key = f"{ticker}:{days}"
    hit = _tk_cache.get(key)
    if hit and _t.time() - hit[0] < _TK_TTL:
        return list(hit[1][0]), hit[1][1]

    async def _grab(win: int) -> list[News]:
        from urllib.parse import quote
        q_name = _short_name(name)
        feeds = {}
        if asset_class == "share":
            feeds[f"g-{ticker}"] = ("https://news.google.com/rss/search?q="
                                    f"{ticker}%20MOEX%20when:{win}d&hl=ru&gl=RU&ceid=RU:ru")
        if q_name and q_name.upper() != ticker.upper():
            feeds[f"g-{q_name[:12]}"] = ("https://news.google.com/rss/search?q="
                                         f"%22{quote(q_name)}%22%20when:{win}d&hl=ru&gl=RU&ceid=RU:ru")
        if not feeds:   # фьючерс, у которого имя совпало с кодом — ищем как есть
            feeds[f"g-{ticker}"] = ("https://news.google.com/rss/search?q="
                                    f"{quote(ticker)}%20when:{win}d&hl=ru&gl=RU&ceid=RU:ru")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=win)).timestamp()
        cl = await _client()
        res = await asyncio.gather(*[_fetch(n, u, cl, cutoff)
                                     for n, u in feeds.items()],
                                   return_exceptions=True)
        items: list[News] = []
        for r in res:
            if isinstance(r, list):
                items.extend(r)
        return _dedupe(items)

    items = await _grab(days)
    note = f"окно {days} дн"
    if not items:
        older = await _grab(fallback_days)
        older.sort(key=lambda x: x.ts, reverse=True)
        items = older[:3]
        note = (f"за {days} дн новостей НЕТ — показаны последние доступные "
                f"(до {fallback_days} дн, «архив»)") if items else                f"новостей по компании нет даже за {fallback_days} дн"
    items.sort(key=lambda x: x.ts, reverse=True)
    items = items[:limit]
    _tk_cache[key] = (_t.time(), (items, note))
    logger.info("news[%s]: %d целевых (%s)", ticker, len(items), note)
    return items, note


def render_for_ai(items: list[News], limit: int = 90) -> str:
    """Компактный нумерованный список новостей для подачи ИИ-распределителю."""
    lines = []
    for i, n in enumerate(items[:limit], 1):
        when = n.published[:16].replace("T", " ")
        body = (n.summary[:240] + "…") if len(n.summary) > 240 else n.summary
        lines.append(f"[N{i}] ({when} · {n.source}) {n.title}"
                     + (f" — {body}" if body else ""))
    return "\n".join(lines)
