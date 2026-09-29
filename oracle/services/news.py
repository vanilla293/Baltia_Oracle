"""Новости, веб-поиск, чтение страниц и погода.

• RSS/Atom из `cfg.news_feeds` — параллельно, не чаще раза в 15 минут, в таблицу `news_items`
  (url уникален — дублей нет), старше 7 дней — вычищается. Ленты подобраны из разных лагерей,
  поэтому `latest` балансирует источники: одно издание не забьёт собой всю выдачу.
• Веб-поиск — ddgs (DuckDuckGo и компания), синхронный, поэтому в отдельном потоке.
• `read_url` — страница → читаемый текст (stdlib html.parser) с защитой от SSRF:
  внутренние адреса (localhost, 10.x, 192.168.x, 169.254.x, NAT64 64:ff9b::…) не открываем, в том числе
  после редиректа. Страницы читает отдельный клиент, у которого имя резолвится в момент соединения и
  соединение идёт ровно на проверенный IP: DNS rebinding (сначала публичный адрес, потом 127.0.0.1) не проходит.
• Погода — Open-Meteo, без ключа: геокодинг города → прогноз, коды WMO → русские слова.

Сеть никогда не роняет бота: ошибки лент пишутся в лог и пропускаются, поиск и погода отдают
пусто/None. Исключение — `read_url`: там ValueError с человеческим текстом (его увидит модель).

У каждого сетевого шага — общий срок (FEED_DEADLINE, READ_DEADLINE, WEATHER_DEADLINE): таймаут httpx
считается на одно чтение, и сервер, отдающий по байту в 5 секунд, иначе держал бы ход агента минутами.
"""
from __future__ import annotations

import asyncio
import html
import io
import ipaddress
import logging
import re
import socket
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpcore
import httpx

from .. import timeutil
from ..db import normalize_text, stem

log = logging.getLogger("oracle.news")

USER_AGENT = "Mozilla/5.0 (compatible; BaltiaOracle/0.1)"
REFRESH_EVERY = timedelta(minutes=15)
RETRY_AFTER_FAIL = timedelta(minutes=3)     # все ленты упали — повторить можно раньше
KEEP_DAYS = 7
SUMMARY_MAX = 600
READ_CAP_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
FEED_CAP_BYTES = 5 * 1024 * 1024
FEED_DEADLINE = 20.0         # секунд на одну ленту целиком (все ленты качаются параллельно)
READ_DEADLINE = 30.0         # секунд на страницу целиком, со всеми редиректами
WEATHER_DEADLINE = 20.0      # секунд на один запрос погоды
BLOCKED = "внутренние адреса не открываю"
KV_LAST_REFRESH = "news:last_refresh"

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# ── мелочи ───────────────────────────────────────────────────────────────────
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v ]+")


def strip_html(s: Any) -> str:
    """HTML-фрагмент → плоский текст: теги прочь, сущности раскрыты, пробелы схлопнуты."""
    t = str(s or "")
    t = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", t)
    t = _TAG.sub(" ", t)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def cut(s: str, n: int) -> str:
    """Обрезать до n символов по границе слова, с «…»."""
    s = s or ""
    if n <= 0:
        return ""
    if len(s) <= n:
        return s
    head = s[: max(1, n - 1)]
    sp = head.rfind(" ")
    if sp > n * 0.6:
        head = head[:sp]
    return head.rstrip(" ,;:.—-") + "…"


def _as_int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def _domain(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        host = ""
    return host[4:] if host.startswith("www.") else host


# слова, которые не несут темы («новости про Иран» → ищем только «Иран»)
_STOP = {
    "новост", "новости", "новость", "про", "что", "как", "для", "это", "все", "всё", "был", "была",
    "было", "или", "при", "над", "под", "без", "его", "ее", "её", "они", "там", "тут", "так",
    "сегодня", "вчера", "сейчас", "последн", "свежи", "свеж", "главн", "мир", "мире",
    "the", "and", "for", "news", "about", "with", "latest", "today",
}
_WORD = re.compile(r"[0-9a-zа-я]+", re.I)


def query_stems(query: str) -> list[str]:
    """Запрос → основы слов для поиска по префиксу (стоп-слова и огрызки выкинуты)."""
    out: list[str] = []
    for w in _WORD.findall(normalize_text(query)):
        if len(w) < 3 or w in _STOP:
            continue
        s = stem(w)
        if len(s) < 3 or s in _STOP or s in out:
            continue
        out.append(s)
    return out


def _matches(text: str, stems: list[str]) -> bool:
    t = normalize_text(text)
    return any(re.search(r"(?<![0-9a-zа-я])" + re.escape(s), t) for s in stems)


# ── погода: коды WMO ─────────────────────────────────────────────────────────
WMO = {
    0: "ясно", 1: "преимущественно ясно", 2: "облачно", 3: "пасмурно",
    45: "туман", 48: "туман с изморозью",
    51: "лёгкая морось", 53: "морось", 55: "сильная морось", 56: "ледяная морось", 57: "ледяная морось",
    61: "небольшой дождь", 63: "дождь", 65: "сильный дождь", 66: "ледяной дождь", 67: "ледяной дождь",
    71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежная крупа",
    80: "небольшой ливень", 81: "ливень", 82: "сильный ливень",
    85: "снегопад", 86: "сильный снегопад",
    95: "гроза", 96: "гроза с градом", 99: "гроза с сильным градом",
}


def wmo_desc(code: Any) -> str:
    try:
        return WMO.get(int(code), "")
    except (TypeError, ValueError):
        return ""


def is_wet(code: Any) -> bool:
    """Код WMO — осадки (морось, дождь, снег, ливень, гроза)."""
    try:
        return int(code) >= 51
    except (TypeError, ValueError):
        return False


def _num(v: Any, digits: int | None = 0) -> int | float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if digits == 0:
        return int(round(x))
    return round(x, digits)


# ── HTML → текст ─────────────────────────────────────────────────────────────
_SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form",
         "template", "iframe", "button", "select", "canvas", "object"}
_BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article",
          "main", "ul", "ol", "table", "blockquote", "pre", "hr", "dd", "dt", "figcaption", "td", "th"}
_MAIN = {"article", "main"}


class _TextExtractor(HTMLParser):
    """Читаемый текст страницы: без скриптов, меню, подвалов; абзацы — переводами строк.
    Текст внутри <article>/<main> копится отдельно — если его достаточно, берём только его."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.main_parts: list[str] = []
        self.title = ""
        self._title_buf: list[str] | None = None
        self._skip = 0
        self._main = 0

    def _emit(self, s: str) -> None:
        self.parts.append(s)
        if self._main:
            self.main_parts.append(s)

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP:
            self._skip += 1
            return
        if tag == "title" and not self._skip and not self.title:
            self._title_buf = []
            return
        if tag in _MAIN:
            self._main += 1
        if self._skip:
            return
        if tag == "li":
            self._emit("\n• ")
        elif tag in _BLOCK:
            self._emit("\n")
        elif tag in {"td", "th"}:
            self._emit(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if tag == "title" and self._title_buf is not None:
            self.title = re.sub(r"\s+", " ", "".join(self._title_buf)).strip()
            self._title_buf = None
            return
        if not self._skip and tag in _BLOCK:
            self._emit("\n")
        if tag in _MAIN:
            self._main = max(0, self._main - 1)

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP:        # <svg/> — ни открывать, ни закрывать нечего
            return
        if not self._skip and tag in _BLOCK:
            self._emit("\n")

    def handle_data(self, data: str) -> None:
        if self._title_buf is not None:
            self._title_buf.append(data)
            return
        if not self._skip:
            self._emit(data)


def _collapse(text: str) -> str:
    lines = [_WS.sub(" ", ln).strip() for ln in text.split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln in ("", "•"):
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(ln)
    return "\n".join(out).strip()


def html_to_text(markup: str) -> tuple[str, str]:
    """HTML → (заголовок, текст)."""
    p = _TextExtractor()
    try:
        p.feed(markup)
        p.close()
    except Exception as e:  # html.parser терпим, но на мусоре бывает всякое
        log.debug("html parse: %s", e)
    full = _collapse("".join(p.parts))
    main = _collapse("".join(p.main_parts))
    text = main if len(main) >= 400 else full
    return p.title, text          # сущности уже раскрыты парсером (convert_charrefs)


_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.I)


def _decode(data: bytes, charset: str | None) -> str:
    enc = charset
    if not enc:
        m = _META_CHARSET.search(data[:4096])
        enc = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    try:
        return data.decode(enc, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


_HTML_TYPES = ("text/html", "application/xhtml+xml")
_TEXT_TYPES = ("application/json", "application/xml", "application/rss+xml", "application/atom+xml",
               "application/ld+json", "application/javascript")


_STREAM_TYPES = ("text/event-stream",)       # бесконечный поток — не страница


def _is_textual(ctype: str) -> bool:
    if ctype in _STREAM_TYPES:
        return False
    return ctype.startswith("text/") or ctype in _HTML_TYPES or ctype in _TEXT_TYPES


# ── SSRF ─────────────────────────────────────────────────────────────────────
_NAT64 = ipaddress.ip_network("64:ff9b::/96")            # NAT64: в последних 32 битах — IPv4
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")    # локальный NAT64 — только внутренние сети
_V4_COMPAT = ipaddress.ip_network("::/96")               # устаревшие «IPv4-совместимые» ::a.b.c.d


def _bad_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Внутренний адрес? IPv6-обёртки IPv4 (::ffff:…, NAT64 64:ff9b::…, 6to4 2002:…) разворачиваем:
    решает вложенный IPv4 — иначе 64:ff9b::a9fe:a9fe (= 169.254.169.254) прошёл бы как «глобальный»."""
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return _bad_ip(ip.ipv4_mapped)
        if ip in _NAT64_LOCAL or ip in _V4_COMPAT:
            return True
        inner = ip.sixtofour or (ipaddress.IPv4Address(int(ip) & 0xFFFF_FFFF) if ip in _NAT64 else None)
        if inner is not None and _bad_ip(inner):
            return True
    return (not ip.is_global) or ip.is_multicast


class _BlockedAddress(httpcore.ConnectError):
    """Соединение не открыто: адрес сайта в момент соединения оказался внутренним."""


class _GuardedBackend(httpcore.AsyncNetworkBackend):
    """Сетевой слой клиента страниц: имя резолвится здесь, внутренние адреса отсекаются, и соединение
    идёт ровно на проверенный IP — второго резолва, который DNS мог бы подменить (rebinding), нет.
    SNI, проверка сертификата и заголовок Host остаются по имени сайта (их ставит httpcore)."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend) -> None:
        self._inner = inner

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                          local_address: str | None = None, socket_options: Any = None):
        try:
            ips = [ipaddress.ip_address(host.strip("[]"))]
        except ValueError:
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except (OSError, UnicodeError) as e:
                raise httpcore.ConnectError(f"не нашёл сайт {host}: {e}") from None
            ips = []
            for info in infos:
                try:
                    ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
                except (ValueError, IndexError, TypeError):
                    raise _BlockedAddress(BLOCKED) from None
                if ip not in ips:
                    ips.append(ip)
        if not ips or any(_bad_ip(ip) for ip in ips):
            raise _BlockedAddress(BLOCKED)
        ips.sort(key=lambda ip: ip.version)        # сначала IPv4: сломанный IPv6 не съест срок страницы
        last: Exception | None = None
        for ip in ips[:4]:          # адрес не отвечает — пробуем следующий, как сделал бы браузер
            try:
                return await self._inner.connect_tcp(str(ip), port, timeout=timeout, local_address=local_address,
                                                     socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as e:
                last = e
        if isinstance(last, OSError) or last is None:
            raise httpcore.ConnectError(f"не соединился с {host}: {last}") from last
        raise last

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options: Any = None):
        raise _BlockedAddress(BLOCKED)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _page_client() -> httpx.AsyncClient:
    """Клиент для read_url: сетевой страж на соединении, без прокси из окружения (прокси резолвил бы
    имя сам, мимо стража) и без автоматических редиректов (их проверяет read_url)."""
    transport = httpx.AsyncHTTPTransport()
    pool = getattr(transport, "_pool", None)
    inner = getattr(pool, "_network_backend", None)
    if inner is not None:
        pool._network_backend = _GuardedBackend(inner)
    else:   # другая версия httpx — остаётся проверка адреса собеседника после соединения
        log.warning("сетевой страж read_url не встал (httpx %s) — проверяю адрес после соединения",
                    httpx.__version__)
    return httpx.AsyncClient(transport=transport, trust_env=False, timeout=15.0,
                             headers={"User-Agent": USER_AGENT})


def _peer_is_bad(r: httpx.Response) -> bool:
    """Второй рубеж: с каким IP реально соединились (нет сведений — MockTransport — не мешаем)."""
    stream = r.extensions.get("network_stream")
    if stream is None:
        return False
    try:
        peer = stream.get_extra_info("server_addr")
        return bool(peer) and _bad_ip(ipaddress.ip_address(str(peer[0]).split("%")[0]))
    except (ValueError, TypeError, IndexError, AttributeError):
        return False


def _blocked(e: BaseException) -> bool:
    """Ошибка httpx выросла из отказа стража (httpx заворачивает исключения httpcore в свои)?"""
    seen = 0
    while e is not None and seen < 5:
        if isinstance(e, _BlockedAddress):
            return True
        e = e.__cause__ or e.__context__
        seen += 1
    return False


class NewsService:
    """Новости (RSS), веб-поиск, чтение ссылок, погода. Один на процесс (ctx.services.news)."""

    def __init__(self, cfg, db, http: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.db = db
        self._own_http = http is None
        self.http = http or httpx.AsyncClient(
            follow_redirects=True, timeout=15.0, headers={"User-Agent": USER_AGENT})
        # страницы по ссылкам от модели — только через клиент со стражем (подсунутый клиент — тестам)
        self._page_http = _page_client() if http is None else http
        self._refresh_lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._own_http:
            await self.http.aclose()
            if self._page_http is not self.http:
                await self._page_http.aclose()

    # ── RSS ──
    async def refresh(self, force: bool = False) -> int:
        """Скачать все ленты (не чаще раза в 15 мин, если не force) → число новых новостей."""
        async with self._refresh_lock:
            now = timeutil.now_utc()
            if not force:
                last = await self.db.kv_get(KV_LAST_REFRESH)
                try:
                    last_dt = timeutil.from_iso(last) if isinstance(last, str) else None
                except ValueError:
                    last_dt = None
                if last_dt is not None and now - last_dt < REFRESH_EVERY:
                    return 0
            feeds = [f for f in (self.cfg.news_feeds or ()) if isinstance(f, str) and f.strip()]
            results = await asyncio.gather(*(self._fetch_feed(u.strip()) for u in feeds),
                                           return_exceptions=True)
            rows: list[tuple] = []
            ok = 0
            for url, res in zip(feeds, results):
                if isinstance(res, BaseException):
                    log.warning("лента %s: %s", url, res)
                    continue
                if res is None:
                    continue
                ok += 1
                rows.extend(res)
            try:
                new = await self._store(rows, now)
                mark = now - REFRESH_EVERY + RETRY_AFTER_FAIL if feeds and not ok else now
                await self.db.kv_set(KV_LAST_REFRESH, timeutil.iso(mark))
            except Exception:   # база не дала записать — новости подождут, бот живёт
                log.exception("новости не сохранились")
                return 0
            log.info("новости: лент %d/%d, новых %d", ok, len(feeds), new)
            return new

    async def _get_capped(self, url: str) -> bytes:
        """Тело ленты, но не больше FEED_CAP_BYTES (больше — ValueError)."""
        async with self.http.stream("GET", url) as r:
            r.raise_for_status()
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > FEED_CAP_BYTES:
                    raise ValueError(f"лента больше {FEED_CAP_BYTES // (1024 * 1024)} МБ")
            return bytes(buf)

    async def _fetch_feed(self, url: str) -> list[tuple] | None:
        """Одна лента → строки (url, title, summary, source, published_at). None — не ответила
        (в том числе не уложилась в FEED_DEADLINE: медленная лента не держит остальные)."""
        try:
            data = await asyncio.wait_for(self._get_capped(url), FEED_DEADLINE)
        except Exception as e:
            log.warning("лента %s не ответила: %s", url,
                        "не уложилась в срок" if isinstance(e, asyncio.TimeoutError) else (str(e) or type(e).__name__))
            return None
        try:
            import feedparser
            parsed = await asyncio.to_thread(feedparser.parse, io.BytesIO(data))
        except Exception as e:
            log.warning("лента %s не разобралась: %s", url, e)
            return None
        entries = parsed.get("entries") or []
        if not entries and parsed.get("bozo"):
            log.warning("лента %s: пусто (%s)", url, parsed.get("bozo_exception"))
            return None
        feed_title = strip_html((parsed.get("feed") or {}).get("title") or "")
        source = cut(feed_title, 80) or _domain(url) or url
        now = timeutil.now_utc()
        out = []
        for e in entries:
            try:
                row = self._entry_row(e, source, now)
            except Exception as ex:  # одна кривая запись не должна топить ленту
                log.debug("запись ленты %s: %s", url, ex)
                continue
            if row:
                out.append(row)
        return out

    @staticmethod
    def _entry_row(e: Any, source: str, now: datetime) -> tuple | None:
        link = str(e.get("link") or "").strip()
        if not link or not link.lower().startswith(("http://", "https://")):
            return None
        title = cut(strip_html(e.get("title") or ""), 300)
        summary = e.get("summary") or e.get("description") or ""
        if not summary and e.get("content"):
            try:
                summary = e["content"][0].get("value") or ""
            except (IndexError, KeyError, TypeError, AttributeError):
                summary = ""
        summary = cut(strip_html(summary), SUMMARY_MAX)
        if not title:
            if not summary:
                return None
            title = cut(summary, 120)
        pub = None
        for key in ("published_parsed", "updated_parsed"):
            tp = e.get(key)
            if tp:
                try:
                    pub = datetime(*tp[:6], tzinfo=timeutil.UTC)
                    break
                except (TypeError, ValueError):
                    continue
        if pub is None or pub > now + timedelta(hours=1):   # нет даты или «из будущего» — считаем свежей
            pub = now
        return (link, title, summary, source, timeutil.iso(pub))

    async def _store(self, rows: list[tuple], now: datetime) -> int:
        fetched = timeutil.iso(now)
        cutoff = timeutil.iso(now - timedelta(days=KEEP_DAYS))
        new = 0
        async with self.db.transaction() as c:
            for (url, title, summary, source, pub) in rows:
                cur = await c.execute(
                    "INSERT OR IGNORE INTO news_items(url, title, summary, source, published_at, fetched_at) "
                    "VALUES(?,?,?,?,?,?)", (url, title, summary, source, pub, fetched))
                new += max(0, int(cur.rowcount or 0))
            await c.execute("DELETE FROM news_items WHERE fetched_at < ?", (cutoff,))
        return new

    async def latest(self, hours: int = 24, query: str = "", limit: int = 40) -> list[dict]:
        """Свежие новости за `hours` часов, новые первыми; с query — только по теме (основы слов).
        Источники чередуются: одно издание не забьёт собой всю выдачу."""
        hours = _as_int(hours, 24, 1, 24 * KEEP_DAYS)
        limit = _as_int(limit, 40, 1, 500)
        since = timeutil.iso(timeutil.now_utc() - timedelta(hours=hours))
        rows = await self.db.fetchall(
            "SELECT id, url, title, summary, source, published_at, fetched_at, "
            "COALESCE(published_at, fetched_at) AS ts FROM news_items "
            "WHERE COALESCE(published_at, fetched_at) >= ? ORDER BY ts DESC, id DESC LIMIT 5000", (since,))
        stems = query_stems(query)      # одни стоп-слова в запросе — считаем, что темы нет
        if stems:
            rows = [r for r in rows if _matches(f"{r['title']} {r['summary']}", stems)]
        # round-robin по источникам (порядок источников — по их самой свежей новости)
        by_src: dict[str, list[dict]] = {}
        for r in rows:
            by_src.setdefault(r["source"] or "?", []).append(r)
        queues = list(by_src.values())
        picked: list[dict] = []
        i = 0
        while len(picked) < limit:
            took = False
            for q in queues:
                if i < len(q) and len(picked) < limit:
                    picked.append(q[i])
                    took = True
            if not took:
                break
            i += 1
        picked.sort(key=lambda r: (r["ts"], r["id"]), reverse=True)
        tz = self.cfg.tz
        return [{"source": r["source"], "title": r["title"], "summary": r["summary"], "url": r["url"],
                 "published_at": r["ts"], "time_local": timeutil.fmt_local(r["ts"], tz)} for r in picked]

    # ── веб-поиск ──
    async def web_search(self, query: str, max_results: int = 8, news: bool = False) -> list[dict]:
        """Поиск в вебе (ddgs). Выключен в настройках, ошибка, пусто — []."""
        if not self.cfg.web_search:
            return []
        q = str(query or "").strip()
        if not q:
            return []
        n = _as_int(max_results, 8, 1, 25)

        def run() -> list[dict]:
            from ddgs import DDGS
            with DDGS(timeout=10) as d:
                if news:
                    return d.news(q, region="ru-ru", max_results=n, timelimit="w")
                return d.text(q, region="ru-ru", max_results=n)

        try:
            raw = await asyncio.wait_for(asyncio.to_thread(run), timeout=40)
        except Exception as e:
            log.warning("веб-поиск «%s» не удался: %s", q, e)
            return []
        out: list[dict] = []
        seen: set[str] = set()
        for r in raw or []:
            if not isinstance(r, dict):
                continue
            url = str(r.get("url") or r.get("href") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            item = {"title": cut(strip_html(r.get("title") or ""), 300) or _domain(url),
                    "url": url,
                    "snippet": cut(strip_html(r.get("body") or r.get("snippet") or ""), 500)}
            if r.get("date"):
                item["date"] = str(r["date"])
            if r.get("source"):
                item["source"] = strip_html(r["source"])
            out.append(item)
            if len(out) >= n:
                break
        return out

    # ── чтение страницы ──
    async def _check_url(self, url: str) -> None:
        """SSRF-страж: только http(s) и только публичные адреса. Иначе ValueError."""
        try:
            p = urlparse(url)
            port = p.port
        except ValueError:
            raise ValueError(f"кривая ссылка: {url}") from None
        if p.scheme not in ("http", "https"):
            raise ValueError("открываю только ссылки http:// и https://")
        host = (p.hostname or "").strip().rstrip(".").lower()
        if not host:
            raise ValueError(f"в ссылке нет адреса сайта: {url}")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
            raise ValueError(BLOCKED)
        try:
            literal = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            literal = None
        if literal is not None:
            if _bad_ip(literal):
                raise ValueError(BLOCKED)
            return
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except (OSError, UnicodeError) as e:
            raise ValueError(f"не нашёл сайт {host}: {e}") from None
        if not infos:
            raise ValueError(f"не нашёл сайт {host}")
        for info in infos:
            try:
                ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
            except (ValueError, IndexError, TypeError):
                raise ValueError(f"не понял адрес сайта {host}") from None
            if _bad_ip(ip):
                raise ValueError(BLOCKED)

    async def _fetch_page(self, url: str) -> tuple[bytes, str, str | None, str]:
        """Скачать страницу, сверяя каждый редирект со SSRF-стражем → (тело, тип, кодировка, итоговый url)."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            await self._check_url(current)
            try:
                async with self._page_http.stream("GET", current, follow_redirects=False) as r:
                    if _peer_is_bad(r):
                        raise ValueError(BLOCKED)
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        current = urljoin(current, r.headers["location"])
                        continue
                    if r.status_code >= 400:
                        raise ValueError(f"страница не открылась: HTTP {r.status_code}")
                    ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
                    if ctype and not _is_textual(ctype):     # pdf, картинка, архив, поток — даже не качаем
                        raise ValueError("это не текстовая страница")
                    charset = r.charset_encoding
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) >= READ_CAP_BYTES:
                            del buf[READ_CAP_BYTES:]
                            break
                    return bytes(buf), ctype, charset, str(r.url)
            except httpx.HTTPError as e:
                if _blocked(e):
                    raise ValueError(BLOCKED) from None
                raise ValueError(f"не смог открыть страницу: {type(e).__name__}") from None
        raise ValueError("слишком много редиректов")

    async def read_url(self, url: str, max_chars: int = 12000) -> dict:
        """Открыть страницу → {"url", "title", "text"}. Внутренние адреса, не-текст и страница,
        не отдавшаяся за READ_DEADLINE, — ValueError."""
        u = str(url or "").strip()
        if not u:
            raise ValueError("пустая ссылка")
        if "://" not in u and re.match(r"^[\w.-]+\.[a-z]{2,}(?:[:/?#]|$)", u, re.I):
            u = "https://" + u      # «meduza.io/news/…» без схемы
        max_chars = _as_int(max_chars, 12000, 200, 200_000)
        try:
            data, ctype, charset, final_url = await asyncio.wait_for(self._fetch_page(u), READ_DEADLINE)
        except asyncio.TimeoutError:
            raise ValueError(f"страница грузится слишком долго (дольше {int(READ_DEADLINE)} с) — бросил") from None
        is_html = ctype in _HTML_TYPES
        if not ctype:   # без типа — нюхаем
            head = data[:1024].lstrip().lower()
            is_html = b"<html" in head or head.startswith(b"<!doctype html")
            if not is_html and (not data or b"\x00" in data[:1024]):
                raise ValueError("это не текстовая страница")
        raw = _decode(data, charset)
        if is_html:     # до 2 МБ разметки — разбираем в потоке, чтобы не держать цикл событий
            title, text = await asyncio.to_thread(html_to_text, raw)
        else:
            title, text = "", _collapse(raw)
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "…[обрезано]"
        return {"url": final_url, "title": title, "text": text}

    # ── погода ──
    async def _get(self, url: str, params: dict) -> httpx.Response:
        """GET с общим сроком на весь ответ (таймаут httpx — на каждое чтение, а не на всё)."""
        return await asyncio.wait_for(self.http.get(url, params=params), WEATHER_DEADLINE)

    async def _geocode(self, name: str) -> tuple[float, float, str] | None:
        key = "weather:geo:" + normalize_text(name).strip()
        cached = await self.db.kv_get(key)
        if isinstance(cached, dict) and "lat" in cached and "lon" in cached:
            return float(cached["lat"]), float(cached["lon"]), str(cached.get("name") or name)
        r = await self._get(GEOCODE_URL, {"name": name, "count": 1, "language": "ru", "format": "json"})
        r.raise_for_status()
        res = (r.json() or {}).get("results") or []
        if not res:
            return None
        g = res[0]
        lat, lon = float(g["latitude"]), float(g["longitude"])
        label = str(g.get("name") or name)
        await self.db.kv_set(key, {"lat": lat, "lon": lon, "name": label})
        return lat, lon, label

    async def weather(self, city: str = "", days: int = 1) -> dict | None:
        """Погода (Open-Meteo): сейчас + прогноз на days (1..7) дней. Не настроено/не ответило — None."""
        days = _as_int(days, 1, 1, 7)
        city = str(city or "").strip()
        cfg = self.cfg
        try:
            if not city and cfg.weather_lat is not None and cfg.weather_lon is not None:
                lat, lon, label = float(cfg.weather_lat), float(cfg.weather_lon), cfg.weather_city or ""
            else:
                name = city or cfg.weather_city
                if not name:
                    return None
                geo = await self._geocode(name)
                if geo is None:
                    log.info("погода: город «%s» не найден", name)
                    return None
                lat, lon, label = geo
            r = await self._get(FORECAST_URL, {
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,"
                         "precipitation_probability_max,weather_code",
                "timezone": "auto", "forecast_days": days, "wind_speed_unit": "ms"})
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("погода не получена: %s", e)
            return None
        return self._parse_weather(data, label, days)

    @staticmethod
    def _parse_weather(data: Any, label: str, days: int) -> dict | None:
        if not isinstance(data, dict):
            return None
        cur = data.get("current") or {}
        now = {"temp": _num(cur.get("temperature_2m")), "desc": wmo_desc(cur.get("weather_code")),
               "wind": _num(cur.get("wind_speed_10m"))}
        daily = data.get("daily") or {}

        def at(key: str, i: int) -> Any:
            arr = daily.get(key) or []
            return arr[i] if i < len(arr) else None

        out_days = []
        for i, d in enumerate((daily.get("time") or [])[:days]):
            code = at("weather_code", i)
            out_days.append({"date": d, "min": _num(at("temperature_2m_min", i)),
                             "max": _num(at("temperature_2m_max", i)),
                             "precip": _num(at("precipitation_sum", i), 1),
                             "precip_prob": _num(at("precipitation_probability_max", i)),
                             "desc": wmo_desc(code), "wet": is_wet(code)})
        if now["temp"] is None and not out_days:
            return None
        return {"city": label, "now": now, "days": out_days}
