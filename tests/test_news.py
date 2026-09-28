"""Новости: RSS-ленты, выборка с балансом источников, чтение страниц с SSRF-стражем, погода,
веб-поиск, инструменты и дайджест. Сеть — только httpx.MockTransport и подменённый getaddrinfo."""
from __future__ import annotations

import asyncio
import importlib
import json
import socket
import sys
import types
from datetime import datetime, timedelta
from typing import Callable

import httpx
import pytest
import pytest_asyncio

from oracle import timeutil
from oracle.llm import LLMError
from oracle.services import news as ns
from oracle.services.news import NewsService
from oracle.tools import base as tb
from oracle.tools import news as tn

from conftest import FakeLLM, with_cfg

RSS_URL = "https://a.test/rss"
ATOM_URL = "https://www.b.test/atom"
BROKEN_URL = "https://c.test/broken"

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Лента А</title><link>https://a.test/</link><description>test</description>
<item><title>Иран &amp; США: переговоры в Омане</title><link>https://a.test/1</link>
  <description>&lt;p&gt;Делегации &lt;b&gt;Ирана&lt;/b&gt; и США встретились &amp;laquo;без условий&amp;raquo;.&lt;/p&gt;&lt;script&gt;x()&lt;/script&gt;</description>
  <pubDate>Mon, 28 Sep 2026 05:00:00 +0000</pubDate></item>
<item><title>Курс рубля</title><link>https://a.test/2</link><description>Рубль укрепился к доллару</description>
  <pubDate>Mon, 28 Sep 2026 04:00:00 +0000</pubDate></item>
<item><title>Без ссылки</title><description>у этой записи нет link</description></item>
<item><title>Старое</title><link>https://a.test/old</link><description>три дня назад</description>
  <pubDate>Fri, 25 Sep 2026 04:00:00 +0000</pubDate></item>
</channel></rss>
"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><id>urn:b</id>
<entry><title type="html">Тегеран &lt;i&gt;отверг&lt;/i&gt; ультиматум</title><link href="https://b.test/x1"/>
  <id>x1</id><updated>2026-09-28T05:30:00Z</updated>
  <summary type="html">&lt;div&gt;Иранские власти&amp;nbsp;заявили о &amp;quot;провокации&amp;quot;&lt;/div&gt;</summary></entry>
<entry><title>Погода в Европе</title><link href="https://b.test/x2"/><id>x2</id>
  <updated>2026-09-27T20:00:00Z</updated><content type="html">&lt;p&gt;Шторм&lt;/p&gt;</content></entry>
</feed>
"""


def rss(title: str, items: list[tuple[str, str, str, str]]) -> bytes:
    """items: (title, link, description, pubDate RFC822 или '')."""
    body = "".join(
        f"<item><title>{t}</title><link>{l}</link><description>{d}</description>"
        + (f"<pubDate>{p}</pubDate>" if p else "") + "</item>"
        for t, l, d, p in items)
    return (f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{title}</title>'
            f"<link>https://x.test/</link><description>d</description>{body}</channel></rss>").encode()


class Router:
    """Маршруты MockTransport по URL (без query или целиком). Не найдено → 404."""

    def __init__(self) -> None:
        self.routes: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self.calls: list[httpx.Request] = []

    def add(self, url: str, body: bytes | str = b"", status: int = 200,
            headers: dict | None = None) -> None:
        data = body.encode() if isinstance(body, str) else body
        self.routes[url] = lambda req: httpx.Response(status, content=data, headers=headers or {})

    def fn(self, url: str, f: Callable[[httpx.Request], httpx.Response]) -> None:
        self.routes[url] = f

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        u = str(request.url)
        f = self.routes.get(u) or self.routes.get(u.split("?")[0])
        return f(request) if f else httpx.Response(404, content=b"nope")

    def hits(self, prefix: str) -> int:
        return sum(1 for r in self.calls if str(r.url).startswith(prefix))


@pytest.fixture
def router() -> Router:
    r = Router()
    r.add(RSS_URL, RSS, headers={"content-type": "application/rss+xml"})
    r.add(ATOM_URL, ATOM, headers={"content-type": "application/atom+xml"})
    r.add(BROKEN_URL, b"server error", status=500)
    return r


@pytest_asyncio.fixture
async def svc(cfg, db, router, clock):
    s = NewsService(with_cfg(cfg, news_feeds=(RSS_URL, ATOM_URL, BROKEN_URL)), db,
                    http=httpx.AsyncClient(transport=httpx.MockTransport(router)))
    yield s
    await s.http.aclose()


@pytest.fixture
def nctx(ctx, svc):
    ctx.cfg = svc.cfg
    ctx.services.news = svc
    return ctx


async def call(ctx, name: str, /, **args) -> dict:
    return json.loads(await tb.dispatch(name, args, ctx))


async def rows(db) -> dict[str, dict]:
    return {r["url"]: r for r in await db.fetchall("SELECT * FROM news_items")}


async def put(db, url: str, title: str, source: str, published: datetime | None,
              summary: str = "", fetched: datetime | None = None) -> None:
    await db.execute(
        "INSERT INTO news_items(url, title, summary, source, published_at, fetched_at) VALUES(?,?,?,?,?,?)",
        (url, title, summary, source, timeutil.iso(published) if published else None,
         timeutil.iso(fetched or timeutil.now_utc())))


# ── утилиты ──────────────────────────────────────────────────────────────────
def test_strip_html_and_cut():
    assert ns.strip_html("<p>Привет&nbsp;<b>мир</b> &amp; всё</p><script>evil()</script>") == "Привет мир & всё"
    assert ns.strip_html(None) == ""
    assert ns.cut("короткий", 50) == "короткий"
    s = ns.cut("слово " * 50, 40)
    assert len(s) <= 40 and s.endswith("…")


def test_query_stems():
    assert ns.query_stems("новости про Иран") == ["иран"]
    assert ns.query_stems("Ирана") == ["иран"]
    assert ns.query_stems("новости") == []
    assert ns.query_stems("") == []
    assert "рубл" in ns.query_stems("что с рублём")


def test_wmo_mapping():
    assert ns.wmo_desc(0) == "ясно"
    assert ns.wmo_desc(3) == "пасмурно"
    assert ns.wmo_desc(45) == "туман"
    assert ns.wmo_desc(63) == "дождь"
    assert ns.wmo_desc(73) == "снег"
    assert ns.wmo_desc(95) == "гроза"
    assert ns.wmo_desc(1234) == "" and ns.wmo_desc(None) == "" and ns.wmo_desc("x") == ""
    assert ns.is_wet(61) and not ns.is_wet(3) and not ns.is_wet(None)


# ── refresh ──────────────────────────────────────────────────────────────────
async def test_refresh_parses_rss_and_atom(svc, db):
    n = await svc.refresh()
    assert n == 5          # 3 из RSS (без ссылки — мимо) + 2 из Atom; сломанная лента пропущена
    r = await rows(db)
    a1 = r["https://a.test/1"]
    assert a1["title"] == "Иран & США: переговоры в Омане"
    assert a1["summary"] == "Делегации Ирана и США встретились «без условий»."
    assert a1["source"] == "Лента А"
    assert a1["published_at"] == "2026-09-28T05:00:00+00:00"
    b1 = r["https://b.test/x1"]
    assert b1["title"] == "Тегеран отверг ультиматум"
    assert b1["summary"] == 'Иранские власти заявили о "провокации"'
    assert b1["source"] == "b.test"          # у Atom нет заголовка — домен без www
    assert b1["published_at"] == "2026-09-28T05:30:00+00:00"
    assert r["https://b.test/x2"]["summary"] == "Шторм"
    assert all(x["fetched_at"] == "2026-09-28T06:00:00+00:00" for x in r.values())


async def test_refresh_dedup_and_throttle(svc, db, router, clock):
    assert await svc.refresh() == 5
    assert router.hits(RSS_URL) == 1
    assert await svc.refresh() == 0                      # 15 минут не прошло — даже не ходим
    assert router.hits(RSS_URL) == 1
    assert await svc.refresh(force=True) == 0            # сходили, но всё уже есть
    assert router.hits(RSS_URL) == 2
    clock.advance(minutes=16)
    router.add(RSS_URL, rss("Лента А", [("Новое", "https://a.test/new", "свежак", "Mon, 28 Sep 2026 06:10:00 +0000"),
                                        ("Курс рубля", "https://a.test/2", "повтор", "")]))
    assert await svc.refresh() == 1
    assert router.hits(RSS_URL) == 3
    assert await db.scalar("SELECT COUNT(*) FROM news_items") == 6
    assert await db.kv_get("news:last_refresh") == timeutil.iso(clock.now)


async def test_refresh_prunes_old(svc, db, clock):
    await put(db, "https://old.test/1", "древность", "X", clock.now - timedelta(days=9),
              fetched=clock.now - timedelta(days=8))
    await put(db, "https://old.test/2", "свежее", "X", clock.now - timedelta(days=5),
              fetched=clock.now - timedelta(days=6))
    await svc.refresh()
    r = await rows(db)
    assert "https://old.test/1" not in r and "https://old.test/2" in r


async def test_refresh_all_failed_retries_sooner(cfg, db, clock):
    tries = {"n": 0}

    def boom(request):
        tries["n"] += 1
        raise httpx.ConnectError("нет сети", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    s = NewsService(with_cfg(cfg, news_feeds=(RSS_URL, ATOM_URL)), db, http=http)
    try:
        assert await s.refresh() == 0 and tries["n"] == 2       # не падает
        clock.advance(minutes=1)
        assert await s.refresh() == 0 and tries["n"] == 2       # сразу не долбим
        clock.advance(minutes=3)                                # через 3+ минуты — пробуем снова, а не через 15
        assert await s.refresh() == 0 and tries["n"] == 4
    finally:
        await http.aclose()


async def test_refresh_garbage_and_odd_entries(cfg, db, clock):
    r = Router()
    r.add("https://g.test/junk", b"\x00\x01 garbage, not a feed")
    r.add("https://g.test/odd", rss("", [
        ("", "https://g.test/e1", "только описание без заголовка", ""),   # заголовок — из описания
        ("Будущее", "https://g.test/e2", "дата из будущего", "Wed, 30 Sep 2026 06:00:00 +0000"),
        ("Не http", "ftp://g.test/e3", "ссылка не та", ""),
        ("Длинное", "https://g.test/e4", "очень " * 300, "Mon, 28 Sep 2026 05:00:00 +0000"),
    ]))
    http = httpx.AsyncClient(transport=httpx.MockTransport(r))
    s = NewsService(with_cfg(cfg, news_feeds=("https://g.test/junk", "https://g.test/odd", "  ")), db, http=http)
    try:
        assert await s.refresh() == 3
        got = await rows(db)
        assert got["https://g.test/e1"]["title"].startswith("только описание")
        assert got["https://g.test/e1"]["source"] == "g.test"
        assert got["https://g.test/e1"]["published_at"] == timeutil.iso(clock.now)   # нет даты — «сейчас»
        assert got["https://g.test/e2"]["published_at"] == timeutil.iso(clock.now)   # из будущего — «сейчас»
        assert "ftp://g.test/e3" not in got
        assert len(got["https://g.test/e4"]["summary"]) <= 600
        assert got["https://g.test/e4"]["summary"].endswith("…")
    finally:
        await http.aclose()


async def test_own_client_defaults(cfg, db):
    s = NewsService(cfg, db)
    try:
        assert s.http.headers["user-agent"] == ns.USER_AGENT
        assert s.http.follow_redirects is True
        assert s.http.timeout.read == 15.0
    finally:
        await s.aclose()
    assert s.http.is_closed


async def test_aclose_keeps_foreign_client(cfg, db):
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    s = NewsService(cfg, db, http=http)
    await s.aclose()
    assert not http.is_closed
    await http.aclose()


# ── latest ───────────────────────────────────────────────────────────────────
async def test_latest_window_and_fields(svc, clock):
    await svc.refresh()
    items = await svc.latest(hours=24)
    urls = [i["url"] for i in items]
    assert "https://a.test/old" not in urls           # 25.09 — старше суток
    assert urls[0] == "https://b.test/x1"             # новые первыми: 05:30 > 05:00 > 04:00 > вчера 20:00
    assert urls == ["https://b.test/x1", "https://a.test/1", "https://a.test/2", "https://b.test/x2"]
    first = items[0]
    assert set(first) == {"source", "title", "summary", "url", "published_at", "time_local"}
    assert first["time_local"] == "пн 28.09 08:30"
    assert "https://a.test/old" in [i["url"] for i in await svc.latest(hours=96)]
    assert len(await svc.latest(hours=24, limit=2)) == 2


async def test_latest_query_by_russian_stems(svc):
    await svc.refresh()
    got = {i["url"] for i in await svc.latest(query="Ирана")}
    assert got == {"https://a.test/1", "https://b.test/x1"}        # «Иран», «Ирана», «Иранские»
    got = {i["url"] for i in await svc.latest(query="новости про рубль")}
    assert got == {"https://a.test/2"}                              # «рубль» ~ «рубля», «Рубль»
    assert await svc.latest(query="Антарктида") == []
    assert len(await svc.latest(query="новости")) == 4             # одни стоп-слова — без фильтра


async def test_latest_falls_back_to_fetched_at(svc, db, clock):
    await put(db, "https://n.test/1", "без даты", "N", None, fetched=clock.now - timedelta(hours=2))
    await put(db, "https://n.test/2", "без даты старое", "N", None, fetched=clock.now - timedelta(hours=30))
    urls = [i["url"] for i in await svc.latest(hours=24)]
    assert "https://n.test/1" in urls and "https://n.test/2" not in urls


async def test_latest_balances_sources(svc, db, clock):
    for k in range(30):     # ТАСС завалил ленту свежим
        await put(db, f"https://tass.test/{k}", f"ТАСС {k}", "ТАСС", clock.now - timedelta(minutes=k + 1))
    for k in range(3):
        await put(db, f"https://bbc.test/{k}", f"BBC {k}", "BBC", clock.now - timedelta(hours=5 + k))
    await put(db, "https://dw.test/0", "DW 0", "DW", clock.now - timedelta(hours=10))
    items = await svc.latest(hours=24, limit=9)
    by = [i["source"] for i in items]
    assert by.count("BBC") == 3 and by.count("DW") == 1 and by.count("ТАСС") == 5
    ts = [i["published_at"] for i in items]
    assert ts == sorted(ts, reverse=True)             # после балансировки — снова новые первыми
    assert items[0]["url"] == "https://tass.test/0"


async def test_latest_bad_args_do_not_crash(svc):
    await svc.refresh()
    assert isinstance(await svc.latest(hours="много", query=None, limit="x"), list)
    assert await svc.latest(hours=-5, limit=0) is not None


# ── read_url ─────────────────────────────────────────────────────────────────
ADDRS = {
    "example.com": "93.184.216.34",
    "news.test": "93.184.216.35",
    "intranet.test": "10.0.0.5",
    "evil.test": "127.0.0.1",
    "meta.test": "169.254.169.254",
    "v6.test": "::1",
    "mapped.test": "::ffff:192.168.1.1",
}


@pytest.fixture
def fake_dns(monkeypatch):
    seen: list[str] = []

    async def getaddrinfo(self, host, port, *args, **kwargs):
        seen.append(host)
        if host not in ADDRS:
            raise socket.gaierror(-2, "Name or service not known")
        ip = ADDRS[host]
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", getaddrinfo)
    return seen


PAGE = """<!doctype html><html><head><title>Статья &laquo;про&raquo; жизнь</title>
<style>body{color:red}</style><script>var x = "<p>не текст</p>";</script></head>
<body><header>Шапка сайта</header><nav><a href="/">Меню</a> <a href="/x">Раздел</a></nav>
<h1>Главное</h1><p>Первый&nbsp;абзац   с   пробелами.</p><p>Второй абзац.<br>Новая строка</p>
<ul><li>пункт один</li><li>пункт два</li></ul>
<svg><title>иконка</title><path d="M0"/></svg><noscript>включи JS</noscript>
<form><input name="q"><button>Найти</button></form>
<aside>Реклама</aside><footer>Подвал © 2026</footer></body></html>"""


async def test_read_url_extracts_text(svc, router, fake_dns):
    router.add("https://example.com/a", PAGE, headers={"content-type": "text/html; charset=utf-8"})
    r = await svc.read_url("https://example.com/a")
    assert r["title"] == "Статья «про» жизнь"
    assert r["url"] == "https://example.com/a"
    t = r["text"]
    assert "Первый абзац с пробелами." in t
    assert "Второй абзац.\nНовая строка" in t
    assert "• пункт один" in t and "• пункт два" in t
    assert t.index("Главное") < t.index("Первый")
    for junk in ("color:red", "не текст", "Шапка", "Меню", "иконка", "включи JS", "Найти", "Реклама", "Подвал"):
        assert junk not in t, junk
    assert "\n\n\n" not in t


async def test_read_url_prefers_article(svc, router, fake_dns):
    body = "<p>" + "Суть статьи. " * 60 + "</p>"
    page = (f"<html><head><title>T</title></head><body><div>Лента: ещё новости, подписка, куки</div>"
            f"<article><h1>Заголовок</h1>{body}</article><div>Похожие материалы</div></body></html>")
    router.add("https://example.com/art", page, headers={"content-type": "text/html"})
    t = (await svc.read_url("https://example.com/art"))["text"]
    assert t.startswith("Заголовок") and "Суть статьи." in t
    assert "подписка" not in t and "Похожие" not in t


async def test_read_url_charset_plain_and_cut(svc, router, fake_dns):
    router.add("https://example.com/cp", "<html><title>Привет</title><p>Кириллица в 1251</p></html>".encode("cp1251"),
               headers={"content-type": "text/html; charset=windows-1251"})
    r = await svc.read_url("https://example.com/cp")
    assert r["title"] == "Привет" and "Кириллица в 1251" in r["text"]
    router.add("https://example.com/meta", '<html><head><meta charset="windows-1251"><title>Мета</title></head></html>'.encode("cp1251"),
               headers={"content-type": "text/html"})
    assert (await svc.read_url("https://example.com/meta"))["title"] == "Мета"
    router.add("https://example.com/t.txt", "строка   1\n\n\n\nстрока 2", headers={"content-type": "text/plain"})
    r = await svc.read_url("https://example.com/t.txt")
    assert r["title"] == "" and r["text"] == "строка 1\n\nстрока 2"
    router.add("https://example.com/long", "а" * 5000, headers={"content-type": "text/plain"})
    r = await svc.read_url("https://example.com/long", max_chars=1000)
    assert r["text"].startswith("а" * 1000) and r["text"].endswith("…[обрезано]") and len(r["text"]) < 1020


async def test_read_url_without_scheme_and_without_ctype(svc, router, fake_dns):
    router.add("https://example.com/page", "<html><body><p>без схемы</p></body></html>")   # без content-type
    r = await svc.read_url("example.com/page")
    assert r["url"] == "https://example.com/page" and "без схемы" in r["text"]
    router.add("https://example.com/bin", b"\x89PNG\x00\x00\x00binary")
    with pytest.raises(ValueError, match="не текстовая"):
        await svc.read_url("https://example.com/bin")


async def test_read_url_refuses_non_text(svc, router, fake_dns):
    router.add("https://example.com/img.png", b"\x89PNG....", headers={"content-type": "image/png"})
    with pytest.raises(ValueError, match="не текстовая"):
        await svc.read_url("https://example.com/img.png")
    router.add("https://example.com/doc.pdf", b"%PDF-1.4", headers={"content-type": "application/pdf"})
    with pytest.raises(ValueError, match="не текстовая"):
        await svc.read_url("https://example.com/doc.pdf")


@pytest.mark.parametrize("url", [
    "http://localhost/admin", "http://LOCALHOST:8080/", "http://api.localhost/", "http://127.0.0.1/",
    "http://[::1]/", "http://169.254.169.254/latest/meta-data/", "http://10.1.2.3/", "http://192.168.0.1/",
    "http://0.0.0.0/", "http://evil.test/", "http://intranet.test/x", "http://meta.test/", "http://v6.test/",
    "http://mapped.test/", "http://printer.local/",
])
async def test_read_url_blocks_internal(svc, router, fake_dns, url):
    with pytest.raises(ValueError, match="внутренние"):
        await svc.read_url(url)
    assert router.calls == []            # ни одного запроса не ушло


@pytest.mark.parametrize("url, msg", [
    ("ftp://example.com/file", "http"), ("file:///etc/passwd", "http"), ("javascript:alert(1)", "http"),
    ("", "пустая"), ("https://", "нет адреса"), ("http://example.com:99999/", "кривая"),
    ("https://no-such-host.test/", "не нашёл"),
])
async def test_read_url_bad_urls(svc, router, fake_dns, url, msg):
    with pytest.raises(ValueError, match=msg):
        await svc.read_url(url)
    assert router.calls == []


async def test_read_url_redirects_checked(svc, router, fake_dns):
    router.add("https://example.com/r1", b"", status=302, headers={"location": "https://news.test/final"})
    router.add("https://news.test/final", "<p>дошли</p>", headers={"content-type": "text/html"})
    r = await svc.read_url("https://example.com/r1")
    assert r["url"] == "https://news.test/final" and "дошли" in r["text"]
    router.add("https://example.com/r2", b"", status=301, headers={"location": "http://127.0.0.1/admin"})
    with pytest.raises(ValueError, match="внутренние"):
        await svc.read_url("https://example.com/r2")
    assert router.hits("http://127.0.0.1") == 0
    router.add("https://example.com/r3", b"", status=302, headers={"location": "/r3"})
    with pytest.raises(ValueError, match="редирект"):
        await svc.read_url("https://example.com/r3")


async def test_read_url_http_errors(svc, router, fake_dns):
    router.add("https://example.com/404", b"nope", status=404)
    with pytest.raises(ValueError, match="HTTP 404"):
        await svc.read_url("https://example.com/404")

    def boom(request):
        raise httpx.ReadTimeout("медленно", request=request)

    router.fn("https://example.com/slow", boom)
    with pytest.raises(ValueError, match="не смог открыть"):
        await svc.read_url("https://example.com/slow")


async def test_read_url_size_cap(svc, router, fake_dns):
    pulled = {"n": 0}
    chunk = b"x" * 65536

    async def stream():
        for _ in range(100):        # 6.4 МБ
            pulled["n"] += 1
            yield chunk

    router.fn("https://example.com/huge", lambda req: httpx.Response(
        200, headers={"content-type": "text/plain"}, content=stream()))
    r = await svc.read_url("https://example.com/huge", max_chars=100)
    assert pulled["n"] <= 33                       # дальше 2 МБ не читали
    assert r["text"].endswith("…[обрезано]")


# ── погода ───────────────────────────────────────────────────────────────────
GEO = {"results": [{"name": "Калининград", "latitude": 54.71, "longitude": 20.51, "country": "Россия"}]}
FORECAST = {
    "current": {"time": "2026-09-28T09:00", "temperature_2m": 12.4, "weather_code": 2, "wind_speed_10m": 4.6},
    "daily": {"time": ["2026-09-28", "2026-09-29", "2026-09-30"],
              "temperature_2m_max": [14.4, 11.0, 9.6], "temperature_2m_min": [9.2, 6.5, -1.4],
              "precipitation_sum": [3.24, 0.0, 1.0], "precipitation_probability_max": [60, 5, 40],
              "weather_code": [63, 0, 71]},
}


@pytest.fixture
def weather_routes(router):
    router.add(ns.GEOCODE_URL, json.dumps(GEO), headers={"content-type": "application/json"})
    router.add(ns.FORECAST_URL, json.dumps(FORECAST), headers={"content-type": "application/json"})
    return router


async def test_weather_geocode_and_forecast(svc, weather_routes, db):
    svc.cfg = with_cfg(svc.cfg, weather_city="Калининград")
    w = await svc.weather(days=3)
    assert w == {
        "city": "Калининград",
        "now": {"temp": 12, "desc": "облачно", "wind": 5},
        "days": [
            {"date": "2026-09-28", "min": 9, "max": 14, "precip": 3.2, "precip_prob": 60, "desc": "дождь", "wet": True},
            {"date": "2026-09-29", "min": 6, "max": 11, "precip": 0.0, "precip_prob": 5, "desc": "ясно", "wet": False},
            {"date": "2026-09-30", "min": -1, "max": 10, "precip": 1.0, "precip_prob": 40,
             "desc": "небольшой снег", "wet": True},
        ],
    }
    geo_req = [r for r in weather_routes.calls if str(r.url).startswith(ns.GEOCODE_URL)][0]
    assert geo_req.url.params["name"] == "Калининград" and geo_req.url.params["language"] == "ru"
    assert geo_req.url.params["count"] == "1"
    fc = [r for r in weather_routes.calls if str(r.url).startswith(ns.FORECAST_URL)][0]
    p = fc.url.params
    assert p["latitude"] == "54.71" and p["longitude"] == "20.51"
    assert p["timezone"] == "auto" and p["forecast_days"] == "3"
    assert set(p["current"].split(",")) == {"temperature_2m", "weather_code", "wind_speed_10m"}
    assert set(p["daily"].split(",")) == {"temperature_2m_max", "temperature_2m_min", "precipitation_sum",
                                          "precipitation_probability_max", "weather_code"}
    # геокод запомнен — второй раз не спрашиваем
    await svc.weather()
    assert weather_routes.hits(ns.GEOCODE_URL) == 1 and weather_routes.hits(ns.FORECAST_URL) == 2


async def test_weather_lat_lon_and_explicit_city(svc, weather_routes):
    svc.cfg = with_cfg(svc.cfg, weather_lat=55.75, weather_lon=37.62, weather_city="Москва")
    w = await svc.weather()
    assert w["city"] == "Москва" and weather_routes.hits(ns.GEOCODE_URL) == 0
    assert weather_routes.calls[-1].url.params["latitude"] == "55.75"
    w = await svc.weather(city="Калининград", days=30)       # город явно — геокодим; дни ≤ 7
    assert w["city"] == "Калининград" and weather_routes.hits(ns.GEOCODE_URL) == 1
    assert weather_routes.calls[-1].url.params["forecast_days"] == "7"


async def test_weather_not_configured_or_failing(svc, router):
    assert await svc.weather() is None and router.calls == []
    router.add(ns.GEOCODE_URL, json.dumps({"generationtime_ms": 0.1}))       # город не найден
    assert await svc.weather(city="Нетакогоград") is None
    router.add(ns.GEOCODE_URL, json.dumps(GEO))
    router.add(ns.FORECAST_URL, b"oops", status=502)
    assert await svc.weather(city="Калининград") is None
    router.add(ns.FORECAST_URL, b"not json", headers={"content-type": "application/json"})
    assert await svc.weather(city="Калининград") is None


async def test_weather_line(nctx, weather_routes):
    nctx.cfg = nctx.services.news.cfg = with_cfg(nctx.cfg, weather_city="Калининград")
    assert await tn.weather_line(nctx) == "Калининград: сейчас +12°, облачно; днём +9…+14°, дождь 60%"


async def test_weather_line_variants(nctx):
    assert await tn.weather_line(nctx) is None                 # не настроено
    line = tn.format_weather_line({"city": "", "now": {"temp": -3, "desc": "ясно", "wind": 12},
                                   "days": [{"min": -7, "max": 0, "precip_prob": 30, "desc": "пасмурно",
                                             "wet": False}]})
    assert line == "Погода: сейчас -3°, ясно; днём -7…0°, пасмурно, осадки 30%; ветер 12 м/с"
    assert tn.format_weather_line({"city": "X", "now": {}, "days": []}) == ""


# ── веб-поиск ────────────────────────────────────────────────────────────────
async def test_web_search_disabled_returns_empty(svc, monkeypatch):
    assert svc.cfg.web_search is False
    monkeypatch.setitem(sys.modules, "ddgs", None)            # импорт упал бы — но до него не доходим
    assert await svc.web_search("что угодно") == []


def fake_ddgs(monkeypatch, *, text=None, news=None, exc: Exception | None = None) -> list[dict]:
    calls: list[dict] = []

    class DDGS:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def text(self, query, **kw):
            calls.append({"kind": "text", "query": query, **kw})
            if exc:
                raise exc
            return text or []

        def news(self, query, **kw):
            calls.append({"kind": "news", "query": query, **kw})
            if exc:
                raise exc
            return news or []

    monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=DDGS))
    return calls


async def test_web_search_normalizes(svc, monkeypatch):
    svc.cfg = with_cfg(svc.cfg, web_search=True)
    calls = fake_ddgs(monkeypatch, text=[
        {"title": "Python <b>docs</b>", "href": "https://docs.python.org/", "body": "Официальная &amp; документация"},
        {"title": "дубль", "href": "https://docs.python.org/", "body": "повтор"},
        {"title": "без ссылки", "body": "мимо"},
        "мусор",
    ], news=[{"date": "2026-09-28T05:00:00+00:00", "title": "Новость", "body": "текст", "url": "https://n.test/1",
              "image": "", "source": "Reuters"}])
    got = await svc.web_search("python", max_results=5)
    assert got == [{"title": "Python docs", "url": "https://docs.python.org/", "snippet": "Официальная & документация"}]
    assert calls[0]["kind"] == "text" and calls[0]["max_results"] == 5 and calls[0]["region"] == "ru-ru"
    got = await svc.web_search("иран", news=True)
    assert got == [{"title": "Новость", "url": "https://n.test/1", "snippet": "текст",
                    "date": "2026-09-28T05:00:00+00:00", "source": "Reuters"}]
    assert calls[1]["kind"] == "news"
    assert await svc.web_search("   ") == []


async def test_web_search_errors_return_empty(svc, monkeypatch):
    svc.cfg = with_cfg(svc.cfg, web_search=True)
    fake_ddgs(monkeypatch, exc=RuntimeError("No results found."))
    assert await svc.web_search("x") == []
    monkeypatch.setitem(sys.modules, "ddgs", None)            # библиотеки нет — тоже []
    assert await svc.web_search("x") == []


# ── инструменты ──────────────────────────────────────────────────────────────
async def test_get_news_tool(nctx):
    r = await call(nctx, "get_news")
    assert r["ok"] and r["count"] == 4 and r["note"] == tn.ANALYST_NOTE
    it = r["items"][0]
    assert set(it) == {"source", "title", "time", "summary", "url"}
    assert it["title"] == "Тегеран отверг ультиматум" and it["time"] == "пн 28.09 08:30"
    r = await call(nctx, "get_news", topic="Иран", hours=1000, limit="2")
    assert r["ok"] and r["count"] == 2 and all("иран" in (i["title"] + i["summary"]).lower() for i in r["items"])


async def test_get_news_summary_cut_and_empty(nctx, db, clock):
    await put(db, "https://l.test/1", "Длинная", "L", clock.now, summary="слово " * 100)
    r = await call(nctx, "get_news", limit=60)
    long = [i for i in r["items"] if i["url"] == "https://l.test/1"][0]
    assert len(long["summary"]) <= 300
    r = await call(nctx, "get_news", topic="Антарктида")
    assert r == {"ok": True, "count": 0, "items": [], "note": r["note"]} and "Антарктида" in r["note"]


async def test_get_news_feeds_silent(ctx, db, clock):
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(503)))
    ctx.cfg = with_cfg(ctx.cfg, news_feeds=(RSS_URL,))
    ctx.services.news = NewsService(ctx.cfg, db, http=http)
    try:
        r = await call(ctx, "get_news")
        assert r["ok"] and r["items"] == [] and "не ответили" in r["note"]
    finally:
        await http.aclose()


async def test_get_news_with_web(nctx, monkeypatch):
    nctx.cfg = nctx.services.news.cfg = with_cfg(nctx.cfg, web_search=True)
    seen = {}

    async def fake_search(query, max_results=8, news=False):
        seen.update(query=query, max_results=max_results, news=news)
        return [{"title": "Веб про Иран", "url": "https://w.test/1", "snippet": "с" * 500,
                 "date": "2026-09-28T04:30:00+00:00", "source": "Reuters"},
                {"title": "Дубль ленты", "url": "https://a.test/1", "snippet": ""},
                {"title": "Без источника", "url": "https://w.test/2", "snippet": "x"}]

    monkeypatch.setattr(nctx.services.news, "web_search", fake_search)
    r = await call(nctx, "get_news", topic="Иран")
    assert seen == {"query": "Иран", "max_results": 8, "news": True}
    web = [i for i in r["items"] if i["source"].startswith("web")]
    assert [w["url"] for w in web] == ["https://w.test/1", "https://w.test/2"]   # дубль ленты выкинут
    assert web[0]["source"] == "web · Reuters" and web[1]["source"] == "web"
    assert web[0]["time"] == "пн 28.09 07:30" and len(web[0]["summary"]) <= 300
    # без темы в веб не ходим
    seen.clear()
    await call(nctx, "get_news")
    assert seen == {}


async def test_web_search_tool(nctx, monkeypatch):
    r = await call(nctx, "web_search", query="x")
    assert r["ok"] is False and "нет такого инструмента" in r["error"]    # выключен в cfg
    assert "web_search" not in [s["function"]["name"] for s in tb.schemas(nctx.cfg)]
    nctx.cfg = nctx.services.news.cfg = with_cfg(nctx.cfg, web_search=True)
    assert "web_search" in [s["function"]["name"] for s in tb.schemas(nctx.cfg)]
    calls = fake_ddgs(monkeypatch, news=[{"date": "2026-09-28T05:00:00+00:00", "title": "Т", "body": "б",
                                          "url": "https://n.test/1", "source": "DW"}])
    r = await call(nctx, "web_search", query="курс", news=True, max_results=50)
    assert r["ok"] and r["count"] == 1 and r["results"][0]["time"] == "пн 28.09 08:00"
    assert calls[0]["max_results"] == 10
    r = await call(nctx, "web_search", query="пусто")
    assert r["ok"] and r["count"] == 0 and "note" in r
    r = await call(nctx, "web_search", query="")
    assert r["ok"] is False


async def test_read_url_tool(nctx, router, fake_dns):
    router.add("https://example.com/big", "<p>" + "текст " * 3000 + "</p>", headers={"content-type": "text/html"})
    r = await call(nctx, "read_url", url="https://example.com/big")
    assert r["ok"] and r["truncated"] is True and len(r["text"]) <= 8000 + 20
    r = await call(nctx, "read_url", url="http://127.0.0.1:8080/")
    assert r["ok"] is False and "внутренние" in r["error"]
    router.add("https://example.com/empty", "<html><script>app()</script></html>", headers={"content-type": "text/html"})
    r = await call(nctx, "read_url", url="https://example.com/empty")
    assert r["ok"] is False and "нет читаемого текста" in r["error"]
    r = await call(nctx, "read_url")
    assert r["ok"] is False and "url" in r["error"]


async def test_get_weather_tool(nctx, weather_routes, router):
    r = await call(nctx, "get_weather")
    assert r == {"ok": False, "error": tn.WEATHER_NOT_SET}
    r = await call(nctx, "get_weather", city="Калининград", days=2)
    assert r["ok"] and r["city"] == "Калининград" and r["now"]["temp"] == 12 and len(r["days"]) == 2
    router.add(ns.GEOCODE_URL, json.dumps({"results": []}))
    r = await call(nctx, "get_weather", city="Нетакогоград", days="abc")
    assert r["ok"] is False and "Нетакогоград" in r["error"]


async def test_svc_created_lazily(ctx):
    assert ctx.services.news is None
    s = tn._svc(ctx)
    try:
        assert isinstance(s, NewsService) and ctx.services.news is s and tn._svc(ctx) is s
    finally:
        await s.aclose()


# ── дайджест ─────────────────────────────────────────────────────────────────
async def test_news_digest_prompt_and_answer(nctx):
    nctx.llm = FakeLLM(["**Иран и США сели за стол**\nФакты: …\n\nМой взгляд: посмотрим."])
    text = await tn.news_digest(nctx)
    assert text.startswith("**Иран и США")
    call0 = nctx.llm.calls[0]
    assert call0["deep"] is True and call0["timeout"] == nctx.cfg.llm_deep_timeout
    system, user = call0["messages"][0]["content"], call0["messages"][1]["content"]
    for title in ("Тегеран отверг ультиматум", "Иран & США: переговоры в Омане", "Курс рубля", "Погода в Европе"):
        assert title in user
    assert "https://a.test/1" in user and "Лента А" in user and "b.test" in user
    assert "Старое" not in user                       # старше суток
    for must in ("Факты:", "Кто что говорит:", "Чего не говорят", "Мой взгляд:", "5–7"):
        assert must in system
    assert "ТЕМА" not in system


async def test_news_digest_topic_and_memory(nctx):
    mem = importlib.import_module("oracle.tools.memory")
    await mem.add_fact(nctx.db, "Интересуется Ближним Востоком и нефтью", "preference")
    nctx.llm = FakeLLM(["разбор"])
    assert await tn.news_digest(nctx, topic="Иран") == "разбор"
    system, user = (m["content"] for m in nctx.llm.calls[0]["messages"])
    assert "ТЕМА: «Иран»" in system
    assert "Ближним Востоком" in user
    assert "Тегеран отверг ультиматум" in user and "Курс рубля" not in user


async def test_news_digest_falls_back_to_fast(nctx):
    def deep_fails(messages, kw):
        raise LLMError("глубокая модель легла")

    nctx.llm = FakeLLM([deep_fails, "быстрый разбор"])
    assert await tn.news_digest(nctx) == "быстрый разбор"
    assert [c["deep"] for c in nctx.llm.calls] == [True, False]
    assert nctx.llm.calls[1]["timeout"] == nctx.cfg.llm_fast_timeout


async def test_news_digest_llm_down_gives_headlines(nctx):
    def fail(messages, kw):
        raise LLMError("нет связи")

    nctx.llm = FakeLLM([fail, fail])
    text = await tn.news_digest(nctx)
    assert "нет связи" in text and "• Тегеран отверг ультиматум — b.test" in text


async def test_news_digest_empty_feed(ctx, db):
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(500)))
    ctx.cfg = with_cfg(ctx.cfg, news_feeds=(RSS_URL,))
    ctx.services.news = NewsService(ctx.cfg, db, http=http)
    try:
        assert await tn.news_digest(ctx) == tn.EMPTY_DIGEST
        assert ctx.llm.calls == []
    finally:
        await http.aclose()
