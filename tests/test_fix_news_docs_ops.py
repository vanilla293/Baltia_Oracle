"""Регрессии группы news-docs-ops: общие сроки сетевых шагов новостей, страж read_url от DNS rebinding
и NAT64, быстрый /news, подрезка get_news под лимит результата, интересы в дайджесте, файлы запуска."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import time
from pathlib import Path

import httpcore
import httpx
import pytest
import pytest_asyncio

from oracle.agent import Agent
from oracle.llm import LLMError
from oracle.services import news as ns
from oracle.services.news import NewsService
from oracle.tools import base as tb
from oracle.tools import news as tn

from conftest import FakeLLM, with_cfg

ROOT = Path(__file__).resolve().parent.parent

RSS = ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Fast</title>'
       "<link>https://f.test/</link><description>d</description>"
       "<item><title>Быстрая новость</title><link>https://f.test/1</link><description>ok</description>"
       "<pubDate>Mon, 28 Sep 2026 05:00:00 +0000</pubDate></item></channel></rss>").encode()


def trickle(chunks: int = 40, pause: float = 0.2, piece: bytes = b"x"):
    """Тело, которое сервер отдаёт по кусочку: каждое чтение укладывается в таймаут httpx, а целиком — нет."""
    async def gen():
        for _ in range(chunks):
            await asyncio.sleep(pause)
            yield piece
    return gen()


async def call(ctx, name: str, /, **args) -> dict:
    return json.loads(await tb.dispatch(name, args, ctx))


# ── F1: общие сроки ──────────────────────────────────────────────────────────
class _Slowpoke:
    """Живой HTTP-сервер на 127.0.0.1: /fast — лента сразу, /slow — по байту в 0.3 с (read-таймаут не сработает)."""

    def __init__(self) -> None:
        self.tasks: set[asyncio.Task] = set()
        self.server: asyncio.AbstractServer | None = None
        self.port = 0

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.tasks.add(asyncio.current_task())
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            path = head.split(b" ", 2)[1]
            if path == b"/fast":
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/rss+xml\r\nContent-Length: "
                             + str(len(RSS)).encode() + b"\r\n\r\n" + RSS)
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/rss+xml\r\nContent-Length: 1000\r\n\r\n")
            for _ in range(1000):
                writer.write(b" ")
                await writer.drain()
                await asyncio.sleep(0.3)
        except (asyncio.CancelledError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    async def __aenter__(self) -> "_Slowpoke":
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        for t in list(self.tasks):
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.server.close()
        await self.server.wait_closed()


async def test_refresh_bounded_by_feed_deadline_on_real_trickling_server(cfg, db, clock, monkeypatch):
    monkeypatch.setattr(ns, "FEED_DEADLINE", 1.0)
    async with _Slowpoke() as srv:
        base = f"http://127.0.0.1:{srv.port}"
        # тот же таймаут, что у клиента по умолчанию: 15 с на одно чтение — байт каждые 0.3 с его не трогает
        http = httpx.AsyncClient(timeout=15.0, trust_env=False, follow_redirects=True)
        s = NewsService(with_cfg(cfg, news_feeds=(base + "/slow", base + "/fast")), db, http=http)
        try:
            t0 = time.monotonic()
            new = await s.refresh(force=True)
            took = time.monotonic() - t0
        finally:
            await http.aclose()
    assert took < 4.0, took                       # без общего срока — 300 с (1000 байт по 0.3 с)
    assert new == 1                               # быстрая лента не пострадала от медленной
    assert await db.scalar("SELECT COUNT(*) FROM news_items WHERE url='https://f.test/1'") == 1


async def test_feed_size_cap(cfg, db, clock, monkeypatch):
    monkeypatch.setattr(ns, "FEED_CAP_BYTES", 10_000)
    pulled = {"n": 0}

    async def endless():
        for _ in range(1000):
            pulled["n"] += 1
            yield b"x" * 1000

    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=endless())))
    s = NewsService(with_cfg(cfg, news_feeds=("https://big.test/rss",)), db, http=http)
    try:
        assert await s._fetch_feed("https://big.test/rss") is None
        assert pulled["n"] <= 12                    # дальше лимита не качали
    finally:
        await http.aclose()


@pytest.fixture
def dns(monkeypatch):
    """Подменённый резолвер: имя → список ответов по очереди (последний повторяется)."""
    table: dict[str, list[str]] = {"example.com": ["93.184.216.34"]}
    seen: list[str] = []

    async def getaddrinfo(self, host, port, *args, **kwargs):
        seen.append(host)
        answers = table.get(host)
        if not answers:
            raise socket.gaierror(-2, "Name or service not known")
        ip = answers.pop(0) if len(answers) > 1 else answers[0]
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", getaddrinfo)
    return table, seen


async def test_read_url_bounded_by_deadline(cfg, db, dns, monkeypatch):
    monkeypatch.setattr(ns, "READ_DEADLINE", 0.5)
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(
        200, headers={"content-type": "text/plain"}, content=trickle(100, 0.2))))
    s = NewsService(cfg, db, http=http)
    try:
        t0 = time.monotonic()
        with pytest.raises(ValueError, match="слишком долго"):
            await s.read_url("https://example.com/drip")
        assert time.monotonic() - t0 < 2.0
    finally:
        await http.aclose()


async def test_read_url_refuses_event_stream(cfg, db, dns):
    pulled = {"n": 0}

    async def sse():
        while True:
            pulled["n"] += 1
            yield b": ping\n\n"
            await asyncio.sleep(0.1)

    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=sse())))
    s = NewsService(cfg, db, http=http)
    try:
        with pytest.raises(ValueError, match="не текстовая"):
            await s.read_url("https://example.com/live")
        assert pulled["n"] == 0
    finally:
        await http.aclose()


async def test_weather_bounded_by_deadline(cfg, db, monkeypatch):
    monkeypatch.setattr(ns, "WEATHER_DEADLINE", 0.3)

    async def slow(req):
        return httpx.Response(200, headers={"content-type": "application/json"}, content=trickle(50, 0.2, b" "))

    http = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    s = NewsService(with_cfg(cfg, weather_lat=54.7, weather_lon=20.5, weather_city="К"), db, http=http)
    try:
        t0 = time.monotonic()
        assert await s.weather() is None
        assert time.monotonic() - t0 < 2.0
    finally:
        await http.aclose()


def _dripping_news(ctx, db, monkeypatch) -> httpx.AsyncClient:
    monkeypatch.setattr(ns, "FEED_DEADLINE", 0.5)
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=trickle(200, 0.2))))
    ctx.cfg = with_cfg(ctx.cfg, news_feeds=("https://drip.test/rss",))
    ctx.services.news = NewsService(ctx.cfg, db, http=http)
    return http


async def test_get_news_tool_bounded(ctx, db, monkeypatch):
    http = _dripping_news(ctx, db, monkeypatch)
    try:
        t0 = time.monotonic()
        r = await call(ctx, "get_news")
        assert time.monotonic() - t0 < 3.0
        assert r["ok"] and r["count"] == 0 and "не ответили" in r["note"]
    finally:
        await http.aclose()


async def test_slow_feed_does_not_block_next_message(ctx, db, monkeypatch):
    """Ход с get_news держит блокировку агента; второе сообщение ждёт не дольше срока ленты."""
    http = _dripping_news(ctx, db, monkeypatch)
    ctx.llm = FakeLLM([{"name": "get_news", "args": {}}, "в лентах тихо"], default="привет")
    agent = Agent(ctx)
    try:
        t0 = time.monotonic()
        first = asyncio.create_task(agent.handle("что в новостях?"))
        await asyncio.sleep(0.1)
        second = asyncio.create_task(agent.handle("привет"))
        r1, r2 = await asyncio.wait_for(asyncio.gather(first, second), 10)
        took = time.monotonic() - t0
    finally:
        await http.aclose()
    assert took < 5.0, took              # без срока — 40 с (200 байт по 0.2 с)
    assert r1.text and r2.text


# ── F2: DNS rebinding и NAT64 ────────────────────────────────────────────────
@pytest.mark.parametrize("addr, bad", [
    ("64:ff9b::a9fe:a9fe", True),        # NAT64 → 169.254.169.254
    ("64:ff9b::7f00:1", True),           # NAT64 → 127.0.0.1
    ("64:ff9b::c0a8:101", True),         # NAT64 → 192.168.1.1
    ("64:ff9b::5db8:d822", False),       # NAT64 → публичный 93.184.216.34
    ("64:ff9b:1::7f00:1", True),         # локальный NAT64
    ("2002:7f00:1::1", True),            # 6to4 с 127.0.0.1 внутри
    ("::7f00:1", True),                  # IPv4-совместимый ::127.0.0.1
    ("::ffff:5db8:d822", False),         # ::ffff:93.184.216.34
    ("::ffff:a00:5", True),              # ::ffff:10.0.0.5
    ("93.184.216.34", False),
    ("2606:4700:4700::1111", False),
])
def test_bad_ip_unwraps_ipv4_inside_ipv6(addr, bad):
    assert ns._bad_ip(ipaddress.ip_address(addr)) is bad


class _Recorder(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.hosts: list[str] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.hosts.append(host)
        raise httpcore.ConnectError("записал и хватит")

    async def connect_unix_socket(self, *a, **kw):
        raise httpcore.ConnectError("нет")

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


async def test_guarded_backend_connects_to_the_vetted_ip(dns):
    table, _ = dns
    table["news.test"] = ["93.184.216.35"]
    table["rebind.test"] = ["93.184.216.34", "127.0.0.1"]
    rec = _Recorder()
    g = ns._GuardedBackend(rec)
    with pytest.raises(httpcore.ConnectError, match="записал"):
        await g.connect_tcp("news.test", 443)
    assert rec.hosts == ["93.184.216.35"]         # соединяемся на IP, а не на имя — второго резолва нет
    await asyncio.get_running_loop().getaddrinfo("rebind.test", 80)   # это «съел» _check_url
    with pytest.raises(ns._BlockedAddress):
        await g.connect_tcp("rebind.test", 80)
    with pytest.raises(ns._BlockedAddress):
        await g.connect_tcp("64:ff9b::a9fe:a9fe", 80)
    with pytest.raises(ns._BlockedAddress):
        await g.connect_unix_socket("/var/run/docker.sock")
    assert rec.hosts == ["93.184.216.35"]


async def test_read_url_dns_rebinding_blocked_on_real_server(cfg, db, dns):
    """Проверка видит публичный адрес, а в момент соединения DNS уже отдаёт 127.0.0.1 — страница не читается."""
    hits: list[bytes] = []

    async def admin(reader, writer):
        hits.append(await reader.readuntil(b"\r\n\r\n"))
        body = b"INTERNAL ADMIN PANEL secret=hunter2"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: "
                     + str(len(body)).encode() + b"\r\n\r\n" + body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(admin, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    table, seen = dns
    table["rebind.test"] = ["93.184.216.34", "127.0.0.1"]
    s = NewsService(cfg, db)          # свой клиент — как в работе
    try:
        assert isinstance(s._page_http._transport._pool._network_backend, ns._GuardedBackend)
        assert s._page_http is not s.http
        with pytest.raises(ValueError, match="внутренние"):
            await s.read_url(f"http://rebind.test:{port}/")
        assert hits == [] and seen.count("rebind.test") == 2
    finally:
        await s.aclose()
        server.close()
        await server.wait_closed()
    assert s.http.is_closed and s._page_http.is_closed


async def test_peer_check_blocks_internal_peer():
    class Stream:
        def __init__(self, addr):
            self.addr = addr

        def get_extra_info(self, name):
            return self.addr if name == "server_addr" else None

    req = httpx.Request("GET", "https://example.com/")
    assert ns._peer_is_bad(httpx.Response(200, request=req, extensions={"network_stream": Stream(("127.0.0.1", 80))}))
    assert not ns._peer_is_bad(httpx.Response(200, request=req,
                                              extensions={"network_stream": Stream(("93.184.216.34", 80))}))
    assert not ns._peer_is_bad(httpx.Response(200, request=req))          # MockTransport — сведений нет


# ── F3/F5: /news быстрый ─────────────────────────────────────────────────────
@pytest_asyncio.fixture
async def nctx(ctx, db, clock):
    rss = ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>A</title>'
           "<link>https://a.test/</link><description>d</description>"
           "<item><title>Тегеран отверг ультиматум</title><link>https://a.test/1</link>"
           "<description>Иран</description><pubDate>Mon, 28 Sep 2026 05:00:00 +0000</pubDate></item>"
           "</channel></rss>").encode()
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=rss)))
    ctx.cfg = with_cfg(ctx.cfg, news_feeds=("https://a.test/rss",))
    ctx.services.news = NewsService(ctx.cfg, db, http=http)
    yield ctx
    await http.aclose()


async def test_news_digest_fast_is_one_fast_call(nctx):
    nctx.llm = FakeLLM(["**Сюжет**\nМой взгляд: так."])
    assert (await tn.news_digest(nctx, "", deep=False)).startswith("**Сюжет**")
    assert len(nctx.llm.calls) == 1
    c = nctx.llm.calls[0]
    assert c["deep"] is False and c["timeout"] == nctx.cfg.llm_fast_timeout


async def test_news_digest_fast_failure_gives_headlines_without_retry(nctx):
    def fail(messages, kw):
        raise LLMError("модель не ответила вовремя (таймаут)")

    nctx.llm = FakeLLM([fail, "не должно понадобиться"])
    text = await tn.news_digest(nctx, deep=False)
    assert "таймаут" in text and "• Тегеран отверг ультиматум" in text
    assert [c["deep"] for c in nctx.llm.calls] == [False]


async def test_news_digest_default_stays_deep_for_schedule(nctx):
    nctx.llm = FakeLLM(["разбор"])
    assert await tn.news_digest(nctx) == "разбор"
    assert nctx.llm.calls[0]["deep"] is True


# ── F4: get_news влезает в лимит результата ──────────────────────────────────
async def test_get_news_fits_result_cap(ctx, db, clock, monkeypatch):
    from oracle import timeutil
    from datetime import timedelta
    for k in range(60):
        await db.execute(
            "INSERT INTO news_items(url, title, summary, source, published_at, fetched_at) VALUES(?,?,?,?,?,?)",
            (f"https://src{k % 6}.test/news/2026/09/28/very-long-slug-{k}-" + "x" * 40,
             f"Заголовок новости номер {k}: " + "очень длинный заголовок " * 4,
             "Подробная выжимка новости. " * 30, f"Источник {k % 6}",
             timeutil.iso(clock.now - timedelta(minutes=k)), timeutil.iso(clock.now)))
    ctx.services.news = NewsService(ctx.cfg, db, http=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    for limit in (30, 60):
        raw = await tb.dispatch("get_news", {"limit": limit}, ctx)
        assert len(raw) <= tb.MAX_RESULT_CHARS
        r = json.loads(raw)                                   # целый JSON, а не обрывок
        assert r["note"] == tn.ANALYST_NOTE                   # указание «как разбирать» на месте
        assert r["count"] == len(r["items"]) > 10
        assert r["count"] + r.get("omitted", 0) == limit
        assert r.get("omitted", 0) == 0 or "не влезло" in r["omitted_note"]
    await ctx.services.news.http.aclose()


# ── F7: интересы в дайджесте ─────────────────────────────────────────────────
async def test_digest_interests_prefer_work_plan_preference(nctx):
    old, fresh = "2026-08-01T00:00:00+00:00", "2026-09-27T00:00:00+00:00"
    await nctx.db.execute("INSERT INTO facts(content, category, created_at, updated_at) VALUES(?,?,?,?)",
                          ("Следит за рынком недвижимости Калининграда", "work", old, old))
    for k in range(40):     # свежее, но не про интересы: дети, спорт
        await nctx.db.execute("INSERT INTO facts(content, category, created_at, updated_at) VALUES(?,?,?,?)",
                              (f"Бытовая подробность №{k}", "person" if k % 2 else "health", fresh, fresh))
    nctx.llm = FakeLLM(["разбор"])
    await tn.news_digest(nctx, deep=False)
    user = nctx.llm.calls[0]["messages"][1]["content"]
    assert "Следит за рынком недвижимости Калининграда" in user
    assert user.count("\n- ") <= tn.INTERESTS_MAX


# ── F9/F11/F12: файлы запуска ────────────────────────────────────────────────
def test_compose_pins_data_paths_to_volume():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env = text.split("environment:", 1)[1].split("volumes:", 1)[0]
    assert re.search(r"^\s+DATA_DIR: /app/data$", env, re.M)
    assert re.search(r'^\s+DB_PATH: ""$', env, re.M)
    assert re.search(r'^\s+USERBOT_SESSION: ""$', env, re.M)
    assert "- ./data:/app/data" in text


def test_start_bat_rebuilds_half_made_venv():
    raw = (ROOT / "start.bat").read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n")           # CRLF, иначе cmd.exe путает метки
    text = raw.decode("utf-8")
    assert '".venv\\Scripts\\python.exe" -m pip --version' in text
    tail = text.split("%PY% -m venv .venv", 1)[1].split("goto venv_fail", 1)[0]
    assert 'rmdir /s /q ".venv"' in tail                      # недоделанное окружение не остаётся


def test_readme_restore_drops_wal():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    restore = text.split("**Восстановление из копии:**", 1)[1][:1200]
    assert "oracle.db-wal" in restore and "oracle.db-shm" in restore
    assert "звонит, пока" not in text
