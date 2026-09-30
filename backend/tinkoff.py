"""ОРАКУЛ // ПИФИЯ — глубокий сбор данных из Tinkoff Invest API (REST).

Цель: вытащить МАКСИМУМ по инструменту и упаковать в одно «досье» для ИИ.
Что собираем:
  • метаданные (lot, валюта, сектор, экспирация фьючерса, базовый актив, ГО);
  • реальную цену (GetLastPrices) — без 15-мин лага MOEX;
  • торговый статус;
  • свечи трёх горизонтов (5м/2дня, час/14дней, день/полгода);
  • стакан (GetOrderBook depth=50): стены, дисбаланс, спред, лимиты;
  • ленту сделок (GetLastTrades): агрессор buy/sell, дельта, крупные принты;
  • теханализ Tinkoff (RSI/MACD/EMA/BB);
  • дивиденды (для акций);
  • и десятки производных метрик, посчитанных локально из свечей/стакана/ленты.

Никогда не бросает наружу: при сбое любого блока пишет error и идёт дальше.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import ssl
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import config

logger = logging.getLogger("pythia.tinkoff")

# v5.4.4: два домена API Т-Банка — основной (новый бренд) и запасной (прежний). На запасной переключаемся, только
# когда запрос ТОЧНО не ушёл (соединение, DNS, TLS-рукопожатие, прокси); чтение — ещё при таймауте/обрыве чтения и
# 5xx. Заявки, стопы и отмены (WRITE_METHODS) при потерянном ответе или 5xx на другой домен НЕ повторяются: второй
# ордер хуже паузы — судьбу первой выясняет вызывающий по тому же UUID. 4xx не переключают (ответ сервера — ответ).
# Рабочий адрес запоминается (BASE). PYTHIA_TINKOFF_BASE (окружение / config_user.json) — один адрес, без запасного.
BASES = ("https://invest-public-api.tbank.ru/rest", "https://invest-public-api.tinkoff.ru/rest")
BASE = BASES[0]
WRITE_METHODS = frozenset({"PostOrder", "PostStopOrder", "CancelOrder", "CancelStopOrder", "ReplaceOrder",
                           "PostOrderAsync", "PostSandboxOrder", "CancelSandboxOrder", "ReplaceSandboxOrder",
                           "OpenSandboxAccount", "CloseSandboxAccount", "SandboxPayIn"})
MD = "tinkoff.public.invest.api.contract.v1.MarketDataService"
INS = "tinkoff.public.invest.api.contract.v1.InstrumentsService"

# ── кэши ──────────────────────────────────────────────────────────────
_index_cache: dict[str, tuple[float, dict]] = {}   # kind -> (ts, {ticker:row})
_INDEX_TTL = 3600
_price_cache: dict[str, tuple[float, float]] = {}
_resolve_cache: dict[str, tuple[float, dict]] = {}  # "share:SBER" -> (ts, instrument row)
_RESOLVE_TTL = 6 * 3600.0  # фьючерс экспирирует — без TTL резолвер вечно
                           # держал бы мёртвый контракт до рестарта сервера


def enabled() -> bool:
    return bool(config.TINKOFF_TOKEN)


def _q(q) -> float:
    """Quotation/MoneyValue -> float (units + nano/1e9)."""
    if q is None:
        return 0.0
    if isinstance(q, dict):
        try:
            units = float(q.get("units", 0) or 0)
        except (ValueError, TypeError):
            units = 0.0
        try:
            nano = float(q.get("nano", 0) or 0)
        except (ValueError, TypeError):
            nano = 0.0
        return units + nano / 1e9
    try:
        return float(q)
    except (ValueError, TypeError):
        return 0.0


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── ОБЩИЙ HTTP-КЛИЕНТ ─────────────────────────────────────────────────
# Раньше КАЖДЫЙ запрос (цена, свечи, стакан, лента…) открывал НОВЫЙ
# httpx.AsyncClient — то есть новое TCP+TLS-соединение. Живой тикер цены бьёт
# по API каждые 3с несколькими вызовами; за долгую сессию это копило сотни
# TIME_WAIT-сокетов (особенно на Windows с узким диапазоном портов) и добавляло
# ~200-300мс TLS-хендшейка на КАЖДЫЙ вызов. Итог — «чем дальше, тем медленнее».
# Теперь один пул соединений с keep-alive живёт весь процесс и переиспользуется.
_http: httpx.AsyncClient | None = None
_http_lock = asyncio.Lock()


_ca_checked = False

# Публичные CA Минцифры РФ с официального CDN Госуслуг (gosuslugi.ru/crt).
# Сам CDN отдаётся по ОБЫЧНОМУ глобально доверенному TLS — качаем безопасно.
RUS_CA_URLS = (
    "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt",
    "https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt",
)


async def _ensure_russian_ca() -> None:
    """Автолечение CERTIFICATE_VERIFY_FAILED: API Тинькофф подписан корнем
    Минцифры, которого нет в certifi. Если data/russian_trusted.pem ещё нет —
    скачиваем оба сертификата с CDN Госуслуг и складываем рядом. Один раз за
    процесс; не вышло → честный WARNING с ручным рецептом (NO DUMMIES,
    verify НЕ отключаем никогда)."""
    global _ca_checked
    if _ca_checked:
        return
    _ca_checked = True
    pem = config.DATA_DIR / "russian_trusted.pem"
    try:
        if pem.exists() and pem.stat().st_size > 1000:
            return
    except OSError:
        pass
    parts = []
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as c:
            for url in RUS_CA_URLS:
                r = await c.get(url)
                r.raise_for_status()
                t = r.text.strip()
                if "BEGIN CERTIFICATE" not in t:
                    raise ValueError(f"ответ не PEM: {url}")
                parts.append(t)
        pem.parent.mkdir(parents=True, exist_ok=True)
        pem.write_text("\n".join(parts) + "\n", encoding="ascii")
        logger.info("CA Минцифры скачаны → %s (это чинит доверие к API Тинькофф)", pem)
    except Exception as e:                                 # noqa: BLE001
        logger.warning(
            "CA Минцифры скачать не удалось (%s). Запросы к Тинькофф будут "
            "падать CERTIFICATE_VERIFY_FAILED. Ручной способ: на gosuslugi.ru/crt "
            "скачай «Корневой» и «Выпускающий» сертификаты в формате PEM, склей "
            "содержимое обоих в один файл и сохрани как %s, потом перезапусти. "
            "Если и после этого падает — антивирус перехватывает HTTPS "
            "(Касперский/Dr.Web): выключи «проверку защищённых соединений» "
            "для python.exe.", str(e)[:120], pem)


async def _client() -> httpx.AsyncClient:
    global _http
    h = _http
    if h is not None and not h.is_closed:
        return h
    async with _http_lock:
        if _http is None or _http.is_closed:
            await _ensure_russian_ca()          # сначала доверие, потом клиент
            _http = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0),
                limits=httpx.Limits(max_keepalive_connections=20,
                                    max_connections=40,
                                    keepalive_expiry=60.0),
                headers={"Content-Type": "application/json"},
                verify=_ssl_context(),
            )
        return _http


def _ssl_context():
    """TLS-доверие для Тинькофф Инвест API. С 2022 его сертификат подписан
    корнем Минцифры РФ (Russian Trusted Root CA), которого НЕТ в certifi —
    без него httpx падает CERTIFICATE_VERIFY_FAILED, resolve → None, и
    стакан «молча не работает» (симптом: MOEX и эфемериды живут, стакан нет).
    Здесь: стандартное доверие certifi + публичные CA Минцифры из
    data/russian_trusted.pem — ТОЛЬКО для клиента этого модуля.
    Файла нет / сломан → честно остаёмся на certifi (лог скажет причину)."""
    import ssl
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:                                  # noqa: BLE001
        ctx = ssl.create_default_context()
    try:
        pem = config.DATA_DIR / "russian_trusted.pem"
        if pem.exists():
            ctx.load_verify_locations(cafile=str(pem))
    except Exception as e:                             # noqa: BLE001
        logger.warning("russian_trusted.pem не подгружен: %s", e)
    return ctx


async def aclose() -> None:
    """Аккуратно закрыть пул (вызывается на остановке сервера)."""
    global _http
    h, _http = _http, None
    if h is not None and not h.is_closed:
        try:
            await h.aclose()
        except Exception:
            pass


# v5.3 фаза 3 (W1): память последней ошибки API Тинькофф для панели проблем (health): отказ биржи
# (TinkoffError с кодом 30042…) или сеть; последний успешный ответ — чтобы понять, актуальна ли ошибка.
# v5.4.4: ошибка помнится ещё и по ИСТОЧНИКУ — рынок (котировки, стакан, инструменты), заявки (заявки, стопы, лимиты),
# счёт (счета, портфель, операции, маржа): успешная цена не прячет отказ заявок/счёта (владелец видел «всё хорошо»,
# пока пилот не мог ни купить, ни продать). В записи — HTTP-статус, код Т-Банка (40003, а не gRPC «16»), вид ошибки
# (classify: auth/rights/cert/network/other) и текст владельцу с подсказкой.
_last_err: dict | None = None
_last_ok_ts: float = 0.0
_src_err: dict[str, dict] = {}               # источник → последняя ошибка
_src_ok: dict[str, float] = {}               # источник → последний успешный ответ
_price_err: dict[str, dict] = {}             # instrument_id → последний сбой GetLastPrices (сброс успехом)
_access_last: dict | None = None             # итог последней проверки токена (check_access)
_token_epoch = 0                             # растёт при смене токена в «Ключах» (reset_errors)
_warned: dict[str, float] = {}               # ключ → когда писали в лог (одна строка в минуту на сбой)
SOURCES = ("market", "orders", "account")
KINDS = ("auth", "rights", "cert", "network", "other")
_ACCOUNT_METHODS = frozenset({"GetAccounts", "GetPortfolio", "GetOperationsByCursor", "GetOperations", "GetInfo",
                              "GetMarginAttributes", "GetUserTariff", "GetPositions", "GetWithdrawLimits",
                              "GetSandboxAccounts", "GetSandboxPortfolio", "GetSandboxPositions",
                              "OpenSandboxAccount", "SandboxPayIn", "CloseSandboxAccount"})


def _method(path: str) -> str:
    return (path or "").rsplit("/", 1)[-1]


def _source(path: str) -> str:
    """Источник запроса для панели проблем: orders (OrdersService/StopOrdersService, заявки песочницы), account
    (счета, портфель, операции, маржа), market (котировки, стакан, свечи, инструменты, расписания)."""
    m = _method(path)
    if m in _ACCOUNT_METHODS or "UsersService" in (path or "") or "OperationsService" in (path or ""):
        return "account"
    if ("OrdersService" in (path or "") or m in WRITE_METHODS or m in ("GetOrderState", "GetOrders", "GetMaxLots",
                                                                        "GetStopOrders", "GetSandboxOrderState",
                                                                        "GetSandboxOrders", "GetSandboxMaxLots")):
        return "orders"
    return "market"


def _warn_once(key: str, msg: str, *args, every: float = 60.0) -> None:
    """Предупреждение в лог не чаще раза в every с на ключ (петля пилота опрашивает цену каждые секунды)."""
    now = time.time()
    if now - _warned.get(key, 0.0) >= every:
        _warned[key] = now
        logger.warning(msg, *args)


def note_error(e: "Exception | str", path: str = "") -> dict:
    """Запомнить ошибку запроса: {"ts","path","source","status","code","grpc","kind","reason","text"} (копия)."""
    global _last_err
    status = e.status if isinstance(e, TinkoffError) else None
    tcode = str(getattr(e, "message", "") or "").strip() if isinstance(e, TinkoffError) else ""
    grpc = str(getattr(e, "code", "") or "").strip() if isinstance(e, TinkoffError) else ""
    kind, reason = classify(e)
    text = humanize_api_error(e) if isinstance(e, Exception) else str(e)
    rec = {"ts": time.time(), "path": _method(path)[:60], "source": _source(path), "status": status,
           "code": (tcode if tcode.isdigit() else grpc) or None, "grpc": grpc or None,
           "kind": kind, "reason": reason, "text": text[:240]}
    _last_err = rec
    _src_err[rec["source"]] = rec
    return dict(rec)


def last_error() -> dict | None:
    """Последняя ошибка Tinkoff (любой источник): {"ts","path","source","status","code","grpc","kind","reason","text"}
    | None (копия)."""
    return dict(_last_err) if _last_err else None


def last_ok_ts(source: str | None = None) -> float:
    """Последний успешный ответ: всего API или одного источника (market | orders | account)."""
    if source:
        return float(_src_ok.get(source) or 0.0)
    return _last_ok_ts


def errors() -> dict[str, dict]:
    """v5.4.4: последние ошибки по источникам {source: {...запись note_error..., "stale": bool}}; stale — после
    ошибки ЭТОТ ЖЕ источник уже ответил успешно (успех рынка не прячет отказ заявок или счёта)."""
    out = {}
    for src, rec in list(_src_err.items()):
        r = dict(rec)
        r["stale"] = bool(_src_ok.get(src, 0.0) > float(r.get("ts") or 0.0))
        out[src] = r
    return out


def price_error(instrument_id: str) -> dict | None:
    """Почему последняя GetLastPrices по инструменту не дала цену ({kind, reason, text, ts, status, code}); цена
    пришла → None. Петля пилота по ней отличает «токен не принят» от «цены просто нет»."""
    r = _price_err.get(instrument_id)
    return dict(r) if r else None


def failure_text(default: str = "", max_age: float = 120.0) -> str:
    """Причина свежей ошибки Tinkoff владельцу (для текстов «счёт не прочитан: …»); свежей нет — default."""
    r = _last_err
    if r and time.time() - float(r.get("ts") or 0.0) <= max_age:
        return str(r.get("reason") or r.get("text") or default)
    return default


def reset_errors() -> None:
    """Новый токен в «Ключах»: прошлые ошибки и проверки — в прошлое (иначе панель показывала бы старый 401, а пилот
    ждал бы до 30 с своей проверки); эпоха токена растёт — пилот проверяет брокера сразу."""
    global _last_err, _access_last, _token_epoch
    _last_err = None
    _src_err.clear()
    _price_err.clear()
    _access_last = None
    _token_epoch += 1


def token_epoch() -> int:
    return _token_epoch


class TinkoffError(RuntimeError):
    """Ошибка API Тинькофф человеческим текстом: код (30042…), сообщение и описание из тела
    ответа, а не «400 Bad Request for url …». Именно этот текст видит владелец в панели
    («вход отбит: …»), поэтому он обязан говорить, ЧТО именно отбила биржа.
    v5.4.4: в тексте — код Т-Банка (40003, 30042), а не gRPC-номер (16): «Tinkoff 401 · 40003: …»."""

    def __init__(self, status: int, code: str = "", message: str = "", description: str = "",
                 path: str = ""):
        self.status, self.code, self.message, self.description, self.path = (
            status, code, message, description, path)
        tcode = str(message or "").strip()
        num = tcode if tcode.isdigit() else ""
        if description:
            text = description + (f" ({message})" if message and message != description and not num else "")
        else:
            text = message if (message and not num) else ""
        text = text or f"HTTP {status}"
        method = path.rsplit("/", 1)[-1] if path else ""
        head = f"Tinkoff {status}" + (f" · {num}" if num else (f" {code}" if code else ""))
        super().__init__(f"{head}: {text}" + (f" [{method}]" if method else ""))


_ERR_HINTS = {
    "30042": "недостаточно средств/обеспечения для сделки",
    "30079": "инструмент недоступен для торгов по API",
    "30080": "ошибка параметров заявки",
    "30049": "ошибка метода — проверь параметры",
    "30051": "инструмент не торгуется (сессия закрыта?)",
    "30052": "инструмент недоступен для маржинальной торговли",
    "30059": "заявка уже в терминальном состоянии",
    "30081": "счёт закрыт или недоступен",
    "30083": "выставление заявок по API запрещено для этого счёта",
    "30092": "заявка отклонена: цена вне допустимого коридора",
    "30099": "цена не кратна шагу цены",
    "40002": "у токена нет прав на эту операцию (только чтение или не к этому счёту)",
    "40003": "токен не принят (отозван или истёк)",
    "40004": "заявки с этого токена недоступны (нужен токен с правом торговли)",
    "80002": "лимит запросов к API исчерпан — повтор через минуту",
}


def _int(x) -> int | None:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def classify(e) -> tuple[str, str]:
    """Ошибка Tinkoff → (вид, причина владельцу по-русски). Виды: auth (401/40003 — токен не принят), rights
    (403/40002/40004 — нет прав на торговлю/счёт), cert (TLS: сертификат не принят), network (сеть, DNS, таймаут,
    5xx, лимит запросов), other (отказ по существу: 30042 и т. п.). Принимает исключение, запись note_error
    (dict) или текст ошибки (результат брокера {"error": …} — строка)."""
    status, tcode, grpc, text = None, "", "", ""
    if isinstance(e, TinkoffError):
        status, tcode, grpc, text = e.status, str(e.message or "").strip(), str(e.code or "").strip(), str(e)
    elif isinstance(e, dict):
        if e.get("kind") in KINDS and e.get("reason"):
            return str(e["kind"]), str(e["reason"])
        status, tcode = _int(e.get("status")), str(e.get("code") or "").strip()
        grpc, text = str(e.get("grpc") or "").strip(), str(e.get("text") or e.get("error") or "")
    elif isinstance(e, BaseException):
        text = f"{type(e).__name__}: {e}"
    else:
        text = str(e or "")
    low = text.lower()
    code = tcode if tcode.isdigit() else ""
    if not code:
        mc = re.search(r"\b(400\d\d|300\d\d|800\d\d|700\d\d|500\d\d)\b", text)
        code = mc.group(1) if mc else ""
    if not status:
        ms = re.search(r"(?:tinkoff|т-банк|http)\s+(\d{3})\b", low)
        status = int(ms.group(1)) if ms else None
    tag = code or (str(status) if status else "")
    if "token не установлен" in low or "токен не задан" in low:
        return "auth", "токен Т-Банка не задан — вставь его в «Ключи»"
    if status == 401 or code == "40003" or grpc in ("16", "UNAUTHENTICATED") or "unauthenticated" in low:
        return "auth", (f"токен Т-Банка не принят ({tag or 'UNAUTHENTICATED'}) — выпусти новый с полным доступом и "
                        "вставь в «Ключи»")
    if status == 403 or code in ("40002", "40004") or grpc in ("7", "PERMISSION_DENIED") or "permission_denied" in low:
        return "rights", (f"у токена нет прав на торговлю/счёт ({tag or 'PERMISSION_DENIED'}) — выпусти в приложении "
                          "Т-Банка токен с полным доступом к этому счёту и вставь в «Ключи»")
    if (isinstance(e, ssl.SSLError) or "certificate" in low or "certificate_verify" in low
            or ("ssl" in low and "handshake" in low)):
        return "cert", ("TLS: сертификат Т-Банка не принят — нужны CA Минцифры (data/russian_trusted.pem подтягивается "
                        "сам при старте, см. лог); антивирус с проверкой HTTPS тоже ломает TLS")
    if status == 429 or code == "80002" or grpc in ("8", "RESOURCE_EXHAUSTED") or "resource_exhausted" in low:
        return "network", "Т-Банк ограничил частоту запросов (80002/429) — пауза, повтор через минуту"
    if (status and status >= 500) or grpc in ("13", "14", "4", "INTERNAL", "UNAVAILABLE", "DEADLINE_EXCEEDED"):
        return "network", f"Т-Банк временно недоступен ({tag or grpc or '5xx'}) — повтор позже"
    net_words = ("timed out", "timeout", "connect", "name or service", "getaddrinfo", "nodename", "network",
                 "temporary failure in name resolution", "no route", "connection reset", "remote protocol",
                 "server disconnected", "сети нет", "таймаут", "соединени")
    if isinstance(e, (httpx.TransportError, OSError, asyncio.TimeoutError)) or any(w in low for w in net_words):
        short = (str(e) if isinstance(e, BaseException) else text).strip() or type(e).__name__
        return "network", f"нет связи с Т-Банком (сеть, DNS или таймаут): {short[:90]}"
    return "other", (text or "отказ Т-Банка без описания")[:200]


def humanize_api_error(e: Exception) -> str:
    """Любое исключение запроса → короткий русский текст для панели и логов (с кодом Т-Банка и подсказкой)."""
    if isinstance(e, TinkoffError):
        kind, reason = classify(e)
        base = str(e)
        if kind in ("auth", "rights"):
            return f"{base} — {reason}"
        hint = _ERR_HINTS.get(str(e.message or "").strip()) or _ERR_HINTS.get(str(e.code))
        return base + (f" — {hint}" if hint and hint not in base else "")
    raw = str(e) or type(e).__name__
    kind, reason = classify(e)
    if kind in ("cert", "network", "auth", "rights"):
        return reason
    return raw[:200]


# ── проверка токена (v5.4.4): GetAccounts + уровень доступа счёта ─────────────────────────────────────────────
_ACCESS_RU = {"FULL_ACCESS": "полный доступ", "READ_ONLY": "только чтение", "NO_ACCESS": "нет доступа"}


def _access_of(acc: dict | None) -> str | None:
    lvl = str((acc or {}).get("accessLevel") or "").replace("ACCOUNT_ACCESS_LEVEL_", "").strip().upper()
    return lvl if lvl and lvl != "UNSPECIFIED" else None


async def check_access(timeout: float = 15.0) -> dict:
    """Токен принят и что он может: GetAccounts (только чтение). {"ok" — токен принят (чтение работает), "trade" —
    FULL_ACCESS к первому счёту (им торгует пилот; None — брокер уровень не сообщил), "kind" ok|auth|rights|cert|
    network|other, "reason" — владельцу, "access", "accounts", "account", "ts"}. Помнится (token_state)."""
    global _access_last
    now = time.time()
    if not enabled():
        res = {"ok": False, "trade": False, "kind": "auth", "reason": "токен Т-Банка не задан — вставь его в «Ключи»",
               "access": None, "accounts": 0, "account": None, "ts": now}
        _access_last = res
        return dict(res)
    try:
        d = await asyncio.wait_for(_post(f"{USERS}/GetAccounts", {}), timeout)
    except Exception as e:                                 # noqa: BLE001
        if isinstance(e, asyncio.TimeoutError):
            note_error(e, f"{USERS}/GetAccounts")
        kind, reason = classify(e)
        res = {"ok": False, "trade": False, "kind": kind, "reason": reason, "access": None, "accounts": 0,
               "account": None, "ts": now}
        _access_last = res
        return dict(res)
    accs = [a for a in ((d or {}).get("accounts") or []) if isinstance(a, dict)]
    first = accs[0] if accs else {}
    access = _access_of(first)
    name = str(first.get("name") or first.get("id") or "")[:40]
    res = {"accounts": len(accs), "account": first.get("id"), "access": access, "ts": now}
    if not accs:
        res.update(ok=False, trade=False, kind="rights",
                   reason="токен принят, но счетов по нему не видно — выпусти токен с доступом к счёту и вставь в «Ключи»")
    elif access == "FULL_ACCESS":
        res.update(ok=True, trade=True, kind="ok", reason=f"токен принят: полный доступ (счёт «{name}»)")
    elif access == "READ_ONLY":
        res.update(ok=True, trade=False, kind="rights",
                   reason=f"токен принят, но только для чтения (счёт «{name}») — пилот не сможет торговать: выпусти "
                          "токен с полным доступом и вставь в «Ключи»")
    elif access == "NO_ACCESS":
        res.update(ok=False, trade=False, kind="rights",
                   reason=f"у токена нет доступа к счёту «{name}» — выпусти токен с полным доступом к нему")
    else:
        res.update(ok=True, trade=None, kind="ok", reason="токен принят; уровень доступа счёта брокер не сообщил")
    _access_last = res
    return dict(res)


def token_state() -> dict | None:
    """Что известно о токене: итог последней проверки (check_access), а свежий отказ 401/403 после неё главнее
    ({"ok": False, "kind": "auth"|"rights", "source": "error"}). Ни проверки, ни отказа — None."""
    a = dict(_access_last) if _access_last else None
    bad = None
    for src, rec in _src_err.items():
        if rec.get("kind") in ("auth", "rights") and not (_src_ok.get(src, 0.0) > float(rec.get("ts") or 0.0)):
            worse = bad is not None and rec["kind"] == "auth" and bad.get("kind") != "auth"   # 401 тяжелее 403
            if bad is None or worse or (rec["kind"] == bad.get("kind")
                                        and float(rec.get("ts") or 0.0) > float(bad.get("ts") or 0.0)):
                bad = rec
    if bad and (a is None or float(bad.get("ts") or 0.0) > float(a.get("ts") or 0.0)):
        return {"ok": bad["kind"] != "auth", "trade": False, "kind": bad["kind"], "reason": bad.get("reason"),
                "access": (a or {}).get("access"), "ts": bad.get("ts"), "source": "error"}
    return a


_server_date: tuple[float, float] | None = None   # (Date сервера биржи, локальное время приёма)


def _remember_date(r) -> None:
    """Заголовок Date любого ответа биржи — рыночным часам (market_clock): локальные часы
    машины могут отставать на часы, «сейчас» для биржи берём по серверу."""
    global _server_date
    try:
        d = r.headers.get("date")
        if d:
            from email.utils import parsedate_to_datetime
            _server_date = (parsedate_to_datetime(d).timestamp(), time.time())
    except Exception:                                      # noqa: BLE001
        pass


def server_date() -> tuple[float, float] | None:
    """(epoch по серверу биржи, локальное время приёма) последнего ответа или None."""
    return _server_date


def bases() -> list[str]:
    """Адреса API по порядку попыток: PYTHIA_TINKOFF_BASE (окружение / config_user.json) — только он, без запасного;
    иначе рабочий (последний ответивший) первым, затем второй домен."""
    one = ""
    try:
        getter = getattr(config, "get", None)
        one = str((getter("PYTHIA_TINKOFF_BASE", "") if callable(getter) else "") or "").strip().rstrip("/")
    except Exception:                                      # noqa: BLE001
        one = ""
    if one:
        return [one]
    cur = BASE if BASE in BASES else BASES[0]
    return [cur] + [b for b in BASES if b != cur]


def _host(url: str) -> str:
    return (url or "").split("://", 1)[-1].split("/", 1)[0]


def _not_sent(e: BaseException) -> bool:
    """Запрос ТОЧНО не ушёл к API: соединение не установлено (отказ, DNS, таймаут соединения, TLS-рукопожатие —
    httpx отдаёт его как ConnectError), прокси не пустил, пул соединений занят."""
    return isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError))


def _read_retryable(e: BaseException) -> bool:
    """Для чтения можно и при потерянном ответе: таймаут/обрыв чтения, сломанный протокол."""
    return isinstance(e, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError))


async def _post(path: str, body: dict, timeout: float = 15.0) -> dict:
    if not config.TINKOFF_TOKEN:
        raise RuntimeError("Tinkoff token не установлен")
    headers = {"Authorization": f"Bearer {config.TINKOFF_TOKEN}"}
    global _last_ok_ts, BASE
    method = _method(path)
    write = method in WRITE_METHODS
    urls = bases()
    cl = await _client()
    for i, base in enumerate(urls):
        more = i + 1 < len(urls)
        try:
            r = await cl.post(f"{base}/{path}", headers=headers, json=body, timeout=timeout)
        except Exception as e:                             # noqa: BLE001  (сеть/таймаут — тоже в память)
            if more and (_not_sent(e) or (not write and _read_retryable(e))):
                _warn_once(f"base:{base}:{type(e).__name__}", "Tinkoff %s: %s не ответил (%s) — пробую %s",
                           method, _host(base), type(e).__name__, _host(urls[i + 1]))
                continue
            note_error(e, path)
            raise
        _remember_date(r)
        if r.status_code >= 500 and not write and more:    # чтение: сервер одного домена лежит — второй
            _warn_once(f"base:{base}:{r.status_code}", "Tinkoff %s: %s ответил %d — пробую %s",
                       method, _host(base), r.status_code, _host(urls[i + 1]))
            continue
        if r.status_code >= 400:
            code = msg = desc = ""
            try:
                j = r.json()
                if isinstance(j, dict):
                    code = str(j.get("code") or "")
                    msg = str(j.get("message") or "")
                    desc = str(j.get("description") or "")
            except Exception:                              # noqa: BLE001
                desc = (r.text or "")[:200]
            err = TinkoffError(r.status_code, code, msg, desc, path)
            note_error(err, path)
            raise err
        now = time.time()
        _last_ok_ts = now
        _src_ok[_source(path)] = now
        if base in BASES and base != BASE:
            logger.warning("Tinkoff: рабочий адрес API теперь %s", _host(base))
            BASE = base                                    # запомнить: следующие запросы — сразу сюда
        return r.json()
    raise RuntimeError("Tinkoff: нет адреса API")          # сюда не доходим: последний адрес либо ответил, либо поднял


# ──────────────────────────────────────────────────────────────────────
# Резолвер тикер → инструмент (share/future/currency)
# ──────────────────────────────────────────────────────────────────────

async def _load_index(kind: str) -> dict:
    """kind: 'shares' | 'futures' | 'currencies'. {TICKER: row}."""
    now = time.time()
    c = _index_cache.get(kind)
    if c and now - c[0] < _INDEX_TTL:
        return c[1]
    method = {"shares": "Shares", "futures": "Futures",
              "currencies": "Currencies", "etfs": "Etfs"}.get(kind, "Shares")
    try:
        data = await _post(f"{INS}/{method}",
                           {"instrumentStatus": "INSTRUMENT_STATUS_BASE"}, timeout=25.0)
    except Exception as e:
        logger.warning("load_index %s failed: %s", kind, str(e)[:120])
        return c[1] if c else {}
    idx: dict[str, dict] = {}
    for row in data.get("instruments", []):
        t = (row.get("ticker") or "").upper().strip()
        if t:
            idx.setdefault(t, row)
    _index_cache[kind] = (now, idx)
    return idx


async def prewarm() -> None:
    """Прогреть индексы инструментов (акции/фьючерсы/валюты) в фоне при старте.
    Раньше ПЕРВЫЙ разбор акции тянул ~3МБ каталог Shares синхронно (+4-5с к
    самому первому объекту). Теперь это делается заранее, ещё до первого клика.

    Последовательно, а не разом: три больших (~3МБ) запроса одновременно
    Tinkoff иногда отбивает 504 — грузим по одному, это фон, спешить некуда."""
    if not enabled():
        return
    for kind in ("shares", "futures", "currencies"):
        try:
            await _load_index(kind)
        except Exception:
            pass


async def resolve(ticker: str, asset_class: str) -> dict | None:
    """Вернуть полный instrument-row Tinkoff по тикеру.

    Для фьючерсов: если задан базовый код (BR/NG/SI...) — найдём ближайший
    активный контракт по префиксу тикера и сроку экспирации (авто-роллове́р).
    """
    t = (ticker or "").upper().strip()
    if not t:
        return None
    key = f"{asset_class}:{t}"
    hit = _resolve_cache.get(key)
    if hit and time.time() - hit[0] < _RESOLVE_TTL:
        return hit[1]

    if asset_class == "futures":
        idx = await _load_index("futures")
        # точное совпадение
        if t in idx:
            _resolve_cache[key] = (time.time(), idx[t])
            return idx[t]
        # поиск ближайшего активного контракта по префиксу
        cands = []
        for tk, row in idx.items():
            base = (row.get("basicAsset") or "").upper()
            if tk.startswith(t) or base == t or tk.startswith(t[:2]):
                exp = row.get("expirationDate") or ""
                cands.append((exp, tk, row))
        # только будущие экспирации, ближайшая первой
        now_iso = _iso(datetime.now(timezone.utc))
        future = [c for c in cands if c[0] and c[0] > now_iso]
        future.sort(key=lambda x: x[0])
        chosen = future[0][2] if future else (cands[0][2] if cands else None)
        if chosen:
            _resolve_cache[key] = (time.time(), chosen)
        return chosen

    if asset_class == "currency":
        idx = await _load_index("currencies")
        row = idx.get(t)
        if row:
            _resolve_cache[key] = (time.time(), row)
        return row

    # shares (по умолчанию)
    idx = await _load_index("shares")
    row = idx.get(t)
    if row:
        _resolve_cache[key] = (time.time(), row)
    return row


# ──────────────────────────────────────────────────────────────────────
# Точечные запросы рыночных данных
# ──────────────────────────────────────────────────────────────────────

async def last_price(instrument_id: str) -> dict | None:
    now = time.time()
    ttl = config.TINKOFF_PRICE_CACHE_SEC
    c = _price_cache.get(instrument_id)
    if c and now - c[0] < ttl:
        return {"price": c[1], "ts": c[0], "cached": True}
    try:
        d = await _post(f"{MD}/GetLastPrices", {"instrumentId": [instrument_id]})
    except Exception as e:
        # _post уже запомнил ошибку этого запроса (ts ≥ начала вызова); нет — запомнить здесь (токена нет и т. п.)
        rec = (dict(_last_err) if (_last_err and float(_last_err.get("ts") or 0.0) >= now)
               else note_error(e, f"{MD}/GetLastPrices"))
        _price_err[instrument_id] = rec
        _warn_once(f"lp:{instrument_id}:{rec.get('kind')}", "GetLastPrices failed: %s", str(e)[:100])
        return None
    rows = d.get("lastPrices") or []
    px = _q(rows[0].get("price")) if rows else 0.0
    if px <= 0:
        _price_err[instrument_id] = {"ts": now, "kind": "no_price", "status": None, "code": None,
                                     "reason": "Т-Банк ответил без цены по инструменту (ошибки нет)",
                                     "text": "GetLastPrices: пустой ответ"}
        return None
    _price_err.pop(instrument_id, None)
    _price_cache[instrument_id] = (now, px)
    return {"price": px, "ts": now, "quote_time": rows[0].get("time")}


_close_cache: dict[str, tuple[float, float]] = {}
_CLOSE_TTL = 60.0   # цена закрытия прошлой сессии статична внутри дня —
                    # живой тикер дёргал её каждые 3с без нужды


async def close_price(instrument_id: str) -> float | None:
    now = time.time()
    c = _close_cache.get(instrument_id)
    if c and now - c[0] < _CLOSE_TTL:
        return c[1]
    try:
        d = await _post(f"{MD}/GetClosePrices",
                        {"instruments": [{"instrumentId": instrument_id}]})
    except Exception:
        return None
    rows = d.get("closePrices") or []
    if not rows:
        return None
    px = _q(rows[0].get("price")) or None
    if px:
        _close_cache[instrument_id] = (now, px)
    return px


_INTERVAL = {
    "1m": "CANDLE_INTERVAL_1_MIN", "5m": "CANDLE_INTERVAL_5_MIN",
    "15m": "CANDLE_INTERVAL_15_MIN", "30m": "CANDLE_INTERVAL_30_MIN",
    "1h": "CANDLE_INTERVAL_HOUR", "4h": "CANDLE_INTERVAL_4_HOUR",
    "1d": "CANDLE_INTERVAL_DAY", "1w": "CANDLE_INTERVAL_WEEK",
}


async def candles(instrument_id: str, interval: str, days_back: int) -> list[dict]:
    """Свечи. Возвращает [{t,o,h,l,c,v}] в хронологическом порядке."""
    iv = _INTERVAL.get(interval, "CANDLE_INTERVAL_DAY")
    to = datetime.now(timezone.utc)
    frm = to - timedelta(days=days_back)
    try:
        d = await _post(f"{MD}/GetCandles", {
            "instrumentId": instrument_id, "from": _iso(frm), "to": _iso(to),
            "interval": iv,
        }, timeout=25.0)
    except Exception as e:
        logger.warning("GetCandles %s %s failed: %s", instrument_id, interval, str(e)[:100])
        return []
    out = []
    for c in d.get("candles", []):
        out.append({
            "t": c.get("time"),
            "o": round(_q(c.get("open")), 6),
            "h": round(_q(c.get("high")), 6),
            "l": round(_q(c.get("low")), 6),
            "c": round(_q(c.get("close")), 6),
            "v": int(c.get("volume", 0) or 0),
            "complete": c.get("isComplete", True),
        })
    return out


async def orderbook(instrument_id: str, depth: int = 50) -> dict | None:
    try:
        d = await _post(f"{MD}/GetOrderBook",
                        {"instrumentId": instrument_id, "depth": depth})
    except Exception as e:
        _warn_once(f"ob:{instrument_id}", "GetOrderBook failed: %s", str(e)[:100])
        return None
    bids = [(_q(b.get("price")), int(b.get("quantity", 0) or 0))
            for b in d.get("bids", [])]
    asks = [(_q(a.get("price")), int(a.get("quantity", 0) or 0))
            for a in d.get("asks", [])]
    if not bids and not asks:
        return None
    bid_vol = sum(q for _, q in bids)
    ask_vol = sum(q for _, q in asks)
    tot = bid_vol + ask_vol
    imb = round((bid_vol - ask_vol) / tot, 3) if tot else 0.0
    best_bid = bids[0][0] if bids else 0.0
    best_ask = asks[0][0] if asks else 0.0
    spread = round(best_ask - best_bid, 6) if (best_bid and best_ask) else None
    mid = (best_bid + best_ask) / 2 if (best_bid and best_ask) else 0.0
    spread_bps = round(spread / mid * 1e4, 2) if (spread and mid) else None
    big_bid = max(bids, key=lambda x: x[1]) if bids else None
    big_ask = max(asks, key=lambda x: x[1]) if asks else None
    return {
        "best_bid": best_bid, "best_ask": best_ask, "spread": spread,
        "spread_bps": spread_bps, "bid_vol": bid_vol, "ask_vol": ask_vol,
        "imbalance": imb,
        "top_bids": [{"p": p, "q": q} for p, q in bids[:10]],
        "top_asks": [{"p": p, "q": q} for p, q in asks[:10]],
        # v3.7 (канон 05.08, слой Майи): ПОЛНЫЕ уровни стакана — раньше 50
        # запрошенных уровней выбрасывались, вакуум ликвидности было не из
        # чего считать. Аддитивно: старые ключи не тронуты.
        "levels_bid": [{"p": p, "q": q} for p, q in bids],
        "levels_ask": [{"p": p, "q": q} for p, q in asks],
        "wall_bid": {"p": big_bid[0], "q": big_bid[1]} if big_bid else None,
        "wall_ask": {"p": big_ask[0], "q": big_ask[1]} if big_ask else None,
        "limit_up": _q(d.get("limitUp")) or None,
        "limit_down": _q(d.get("limitDown")) or None,
        "last_price": _q(d.get("lastPrice")) or None,
        "close_price": _q(d.get("closePrice")) or None,
    }


async def last_trades(instrument_id: str, minutes: int = 60) -> dict | None:
    to = datetime.now(timezone.utc)
    frm = to - timedelta(minutes=minutes)
    try:
        d = await _post(f"{MD}/GetLastTrades",
                        {"instrumentId": instrument_id, "from": _iso(frm), "to": _iso(to)})
    except Exception as e:
        logger.warning("GetLastTrades failed: %s", str(e)[:100])
        return None
    trades = d.get("trades", [])
    if not trades:
        return {"count": 0}
    buy_v = sell_v = 0
    buy_n = sell_n = 0
    prints = []
    for tr in trades:
        q = int(tr.get("quantity", 0) or 0)
        p = _q(tr.get("price"))
        direction = tr.get("direction", "")
        if direction == "TRADE_DIRECTION_BUY":
            buy_v += q
            buy_n += 1
        elif direction == "TRADE_DIRECTION_SELL":
            sell_v += q
            sell_n += 1
        prints.append((q, p, direction))
    tot = buy_v + sell_v
    delta = buy_v - sell_v
    prints.sort(reverse=True)
    big = [{"q": q, "p": p, "side": "BUY" if "BUY" in d_ else "SELL"}
           for q, p, d_ in prints[:5]]
    return {
        "count": len(trades), "window_min": minutes,
        "buy_vol": buy_v, "sell_vol": sell_v, "delta": delta,
        "aggressor_ratio": round(buy_v / tot, 3) if tot else None,
        "buy_trades": buy_n, "sell_trades": sell_n,
        "avg_trade_size": round(sum(q for q, _, _ in prints) / len(prints), 1),
        "largest_prints": big,
    }


async def trading_status(instrument_id: str) -> dict | None:
    try:
        d = await _post(f"{MD}/GetTradingStatus", {"instrumentId": instrument_id})
    except Exception:
        return None
    return {
        "status": d.get("tradingStatus"),
        "limit_order": d.get("limitOrderAvailableFlag"),
        "market_order": d.get("marketOrderAvailableFlag"),
        "api_available": d.get("apiTradeAvailableFlag"),
    }


def _ts(v) -> float | None:
    """RFC3339 Tinkoff → epoch; пусто → None."""
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


async def trading_schedules(exchange: str, frm: datetime, to: datetime) -> list[dict] | None:
    """InstrumentsService/TradingSchedules: дни биржи (MOEX | FORTS | …) с окнами торгов —
    для рыночных часов. Каждый день: {date, is_trading_day, start, end, evening_start,
    evening_end, clearing_start, clearing_end, premarket_start, premarket_end,
    opening_auction_start, closing_auction_end} (времена — epoch или None). Сбой → None."""
    try:
        d = await _post(f"{INS}/TradingSchedules",
                        {"exchange": exchange, "from": _iso(frm), "to": _iso(to)})
    except Exception as e:                                 # noqa: BLE001
        logger.info("trading_schedules %s: %s", exchange, str(e)[:100])
        return None
    out: list[dict] = []
    for ex in d.get("exchanges", []) or []:
        for day in ex.get("days", []) or []:
            out.append({
                "exchange": ex.get("exchange"), "date": (day.get("date") or "")[:10],
                "is_trading_day": bool(day.get("isTradingDay")),
                "start": _ts(day.get("startTime")), "end": _ts(day.get("endTime")),
                "evening_start": _ts(day.get("eveningStartTime")), "evening_end": _ts(day.get("eveningEndTime")),
                "clearing_start": _ts(day.get("clearingStartTime")), "clearing_end": _ts(day.get("clearingEndTime")),
                "premarket_start": _ts(day.get("premarketStartTime")), "premarket_end": _ts(day.get("premarketEndTime")),
                "opening_auction_start": _ts(day.get("openingAuctionStartTime")),
                "closing_auction_end": _ts(day.get("closingAuctionEndTime")),
            })
    return out


_TECH = {
    "RSI": "INDICATOR_TYPE_RSI", "MACD": "INDICATOR_TYPE_MACD",
    "EMA": "INDICATOR_TYPE_EMA", "SMA": "INDICATOR_TYPE_SMA",
    "BB": "INDICATOR_TYPE_BB",
}


_IND_INTERVAL = {  # у GetTechAnalysis СВОЙ enum интервалов (не CANDLE_INTERVAL_*)
    "1m": "INDICATOR_INTERVAL_ONE_MINUTE", "5m": "INDICATOR_INTERVAL_FIVE_MINUTES",
    "15m": "INDICATOR_INTERVAL_FIFTEEN_MINUTES", "1h": "INDICATOR_INTERVAL_ONE_HOUR",
    "1d": "INDICATOR_INTERVAL_ONE_DAY",
}


async def consensus(instrument_uid: str) -> dict | None:
    """Консенсус аналитиков Tinkoff: рекомендация, целевая цена, апсайд,
    таргеты инвестдомов. То самое «ключевое, что пишут» — в цифрах."""
    try:
        r = await _post("tinkoff.public.invest.api.contract.v1."
                        "InstrumentsService/GetForecastBy",
                        {"instrumentId": instrument_uid})
    except Exception as e:
        logger.warning("consensus: %s", str(e)[:90])
        return None
    cons = r.get("consensus") or {}
    if not cons:
        return None
    reco_map = {"RECOMMENDATION_BUY": "ПОКУПАТЬ", "RECOMMENDATION_HOLD": "ДЕРЖАТЬ",
                "RECOMMENDATION_SELL": "ПРОДАВАТЬ"}
    houses = []
    for t in (r.get("targets") or []):
        tp = _q(t.get("targetPrice"))
        if tp:
            houses.append({"company": t.get("company"),
                           "reco": reco_map.get(t.get("recommendation"),
                                                t.get("recommendation")),
                           "target": tp})
    houses.sort(key=lambda x: -(x["target"] or 0))
    return {
        "reco": reco_map.get(cons.get("recommendation"), cons.get("recommendation")),
        "target": _q(cons.get("consensus")),
        "current": _q(cons.get("currentPrice")),
        "upside_pct": _q(cons.get("priceChangeRel")),
        "houses_count": len(houses),
        "houses_top": houses[:4],
    }


async def next_dividend(instrument_uid: str) -> dict | None:
    """Ближайший ОБЪЯВЛЕННЫЙ дивиденд (отсечка в будущем): сумма, доходность,
    дата отсечки и дней до неё. Критично для гэпа и базиса фьючерса."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    try:
        r = await _post("tinkoff.public.invest.api.contract.v1."
                        "InstrumentsService/GetDividends",
                        {"instrumentId": instrument_uid,
                         "from": _iso(now),
                         "to": _iso(now + timedelta(days=365))})
    except Exception as e:
        logger.warning("next_dividend: %s", str(e)[:90])
        return None
    best = None
    today = _iso(now)[:10]
    for d in (r.get("dividends") or []):
        rec = d.get("recordDate") or ""
        if not rec or rec[:10] < today:   # API иногда отдаёт прошедшие — фильтруем сами
            continue
        if best is None or rec < best.get("record_date", "9999"):
            best = {"value": _q(d.get("dividendNet")),
                    "currency": (d.get("dividendNet") or {}).get("currency"),
                    "record_date": rec,
                    "yield_pct": _q(d.get("yieldValue"))}
    if best:
        try:
            rd = datetime.fromisoformat(best["record_date"].replace("Z", "+00:00"))
            best["days_to_record"] = max(0, (rd - now).days)
        except Exception:
            best["days_to_record"] = None
    return best


async def tech(instrument_uid: str, indicator: str, length: int = 14,
               interval: str = "1h", days_back: int = 30) -> list[dict] | None:
    itype = _TECH.get(indicator)
    if not itype:
        return None
    iv = _IND_INTERVAL.get(interval, "INDICATOR_INTERVAL_ONE_HOUR")
    to = datetime.now(timezone.utc)
    frm = to - timedelta(days=days_back)
    body = {
        "indicatorType": itype, "instrumentUid": instrument_uid,
        "from": _iso(frm), "to": _iso(to), "interval": iv,
        "typeOfPrice": "TYPE_OF_PRICE_CLOSE", "length": length,
    }
    if indicator == "MACD":
        body.update({"smoothing": {"fastLength": 12, "slowLength": 26,
                                   "signalSmoothing": 9}})
    if indicator == "BB":
        body.update({"deviation": {"deviationMultiplier": {"units": 2, "nano": 0}}})
    try:
        d = await _post(f"{MD}/GetTechAnalysis", body)
    except Exception:
        return None
    out = []
    for r in d.get("technicalIndicators", []):
        item = {"t": r.get("timestamp")}
        for k in ("signal", "macd", "middleBand", "upperBand", "lowerBand"):
            if k in r:
                item[k] = round(_q(r[k]), 4)
        out.append(item)
    return out[-3:] if out else None


async def futures_margin(instrument_id: str) -> dict | None:
    try:
        d = await _post(f"{INS}/GetFuturesMargin", {"instrumentId": instrument_id})
    except Exception:
        return None
    return {
        "margin_buy": round(_q(d.get("initialMarginOnBuy")), 2),
        "margin_sell": round(_q(d.get("initialMarginOnSell")), 2),
        "min_price_increment": _q(d.get("minPriceIncrement")),
        "min_price_increment_amount": _q(d.get("minPriceIncrementAmount")),
    }


USERS = "tinkoff.public.invest.api.contract.v1.UsersService"
OPS = "tinkoff.public.invest.api.contract.v1.OperationsService"


async def accounts() -> list[dict] | None:
    """Список счетов владельца (GetAccounts). Нужен, чтобы бот знал, НА КАКОМ
    счёте и СКОЛЬКО денег. Только чтение. Нет токена → None.
    v5.4.4: None — счёт НЕ ПРОЧИТАН (причина в last_error()/failure_text()), [] — счетов нет; access — уровень
    доступа токена к счёту (FULL_ACCESS | READ_ONLY | NO_ACCESS | None)."""
    if not enabled():
        return None
    try:
        d = await _post(f"{USERS}/GetAccounts", {})
    except Exception:                                       # noqa: BLE001
        return None
    out = []
    for a in d.get("accounts", []):
        out.append({"id": a.get("id"), "name": a.get("name"),
                    "type": a.get("type"), "status": a.get("status"), "access": _access_of(a)})
    return out


async def portfolio(account_id: str) -> dict | None:
    """Портфель счёта (GetPortfolio): бот ВИДИТ реальный баланс — сколько
    свободных рублей, стоимость позиций, ГО. Это ответ на «шарит ли он про
    сумму денег»: да, размер позиции считается от РЕАЛЬНОГО депозита, а не
    от выдуманного числа (NO DUMMIES). Только чтение. Нет данных → None."""
    if not (enabled() and account_id):
        return None
    try:
        d = await _post(f"{OPS}/GetPortfolio",
                        {"accountId": account_id, "currency": "RUB"})
    except Exception:                                       # noqa: BLE001
        return None
    total = _q(d.get("totalAmountPortfolio"))
    free = 0.0
    for m in (d.get("totalAmountCurrencies"), d.get("totalAmountMoney")):
        if isinstance(m, dict):
            free += _q(m)
    # свободные рубли из позиций-валют
    for p in d.get("positions", []):
        if p.get("instrumentType") == "currency" and (p.get("figi", "").startswith("RUB")
                                                       or p.get("ticker") == "RUB"):
            free = _q(p.get("quantity"))
    return {
        "total_rub": round(total, 2),
        "free_rub": round(free, 2),
        "expected_yield_pct": round(_q(d.get("expectedYield")), 2),
        "positions": [{"figi": p.get("figi"), "uid": p.get("instrumentUid"),
                       "type": p.get("instrumentType"),
                       "qty": _q(p.get("quantity")),
                       "avg": _q(p.get("averagePositionPrice")),
                       "cur_price": _q(p.get("currentPrice")),
                       "yield": _q(p.get("expectedYield")),
                       "blocked": bool(p.get("blocked"))}
                      for p in d.get("positions", [])],
        "note": "реальный баланс счёта — размер позиции считается от него",
    }


def _i64(v) -> int:
    """int64 в REST приходит строкой ("12") — в число; мусор → 0."""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


async def max_lots(account_id: str, instrument_id: str, price: float | None = None) -> dict | None:
    """OrdersService/GetMaxLots — СКОЛЬКО ЛОТОВ БИРЖА ДАЁТ КУПИТЬ/ПРОДАТЬ прямо сейчас с учётом
    денег, уже открытых позиций и маржинального плеча (buyMarginLimits / sellMarginLimits).
    Это и есть «на максималку с максимальной маржой» — считает брокер, не мы.
    {"buy": N, "sell": M, "buy_cash": N0, "sell_cash": M0, "buy_money": ₽, "currency": "rub"};
    нет данных → None (сайзер честно откатится на локальный расчёт)."""
    if not (enabled() and account_id and instrument_id):
        return None
    body: dict = {"accountId": account_id, "instrumentId": instrument_id}
    if price and price > 0:
        units = int(price)
        body["price"] = {"units": units, "nano": int(round((price - units) * 1e9))}
    try:
        d = await _post("tinkoff.public.invest.api.contract.v1.OrdersService/GetMaxLots", body)
    except Exception as e:                                 # noqa: BLE001
        logger.warning("GetMaxLots: %s", humanize_api_error(e)[:160])
        return None

    def _lots(block, key):
        b = d.get(block) or {}
        return _i64(b.get(key, b.get(key.replace("Lots", "_lots").lower(), 0)))

    buy_cash = _lots("buyLimits", "buyMaxLots")
    buy_mrg = _lots("buyMarginLimits", "buyMaxLots")
    sell_cash = _lots("sellLimits", "sellMaxLots")
    sell_mrg = _lots("sellMarginLimits", "sellMaxLots")
    bm = (d.get("buyLimits") or {}).get("buyMoneyAmount")
    return {"buy": max(buy_cash, buy_mrg), "sell": max(sell_cash, sell_mrg),
            "buy_cash": buy_cash, "sell_cash": sell_cash,
            "buy_market": _lots("buyLimits", "buyMaxMarketLots") or None,
            "buy_money": round(_q(bm), 2) if isinstance(bm, dict) else None,
            "currency": d.get("currency"), "ts": time.time()}


async def margin_attributes(account_id: str) -> dict | None:
    """UsersService/GetMarginAttributes — ликвидный портфель, начальная/минимальная маржа,
    уровень достаточности средств, недостающие средства. Нет маржинальной торговли на счёте →
    API отвечает ошибкой, тогда None (сайзер и дозор работают по деньгам)."""
    if not (enabled() and account_id):
        return None
    try:
        d = await _post(f"{USERS}/GetMarginAttributes", {"accountId": account_id})
    except Exception as e:                                 # noqa: BLE001
        logger.info("GetMarginAttributes: %s", humanize_api_error(e)[:160])
        return None
    return {"liquid": round(_q(d.get("liquidPortfolio")), 2),
            "starting_margin": round(_q(d.get("startingMargin")), 2),
            "minimal_margin": round(_q(d.get("minimalMargin")), 2),
            "sufficiency": round(_q(d.get("fundsSufficiencyLevel")), 3) or None,
            "missing": round(_q(d.get("amountOfMissingFunds")), 2),
            "corrected_margin": round(_q(d.get("correctedMargin")), 2),
            "ts": time.time()}


# ── операции счёта (v5.3 фаза 4 · W1: журнал по операциям брокера) ────────────────────────
_OP_KIND = {
    "OPERATION_TYPE_BUY": "buy", "OPERATION_TYPE_BUY_CARD": "buy", "OPERATION_TYPE_BUY_MARGIN": "buy",
    "OPERATION_TYPE_SELL": "sell", "OPERATION_TYPE_SELL_MARGIN": "sell",
    "OPERATION_TYPE_BROKER_FEE": "fee",          # комиссия сделки (дочерняя к buy/sell, дублирует поле commission)
    "OPERATION_TYPE_MARGIN_FEE": "margin",       # плата за маржинальную позицию — суточная, не сделки
    "OPERATION_TYPE_ACCRUING_VARMARGIN": "varmargin", "OPERATION_TYPE_WRITING_OFF_VARMARGIN": "varmargin",
}                                                # остальное (ввод/вывод, сервисные платы, купоны…) → other
OPS_PAGE = 1000          # предел одной страницы GetOperationsByCursor
OPS_MAX_PAGES = 30       # страховка от вечного курсора


def _norm_operation(it: dict) -> dict | None:
    """Одна операция REST (GetOperationsByCursor.items[]) → {id, ts, kind buy|sell|fee|margin|varmargin|other, type, state, figi,
    uid, qty, price, payment, fee, trades:[{ts, qty, price}], name, parent}. Нет id или даты → None.
    qty — исполненное количество в штуках (quantityDone, иначе quantity); price — цена операции;
    payment — деньги со знаком (покупка < 0, продажа > 0), fee — комиссия операции (модуль)."""
    if not isinstance(it, dict):
        return None
    oid = str(it.get("id") or "").strip()
    ts = _ts(it.get("date"))
    if not oid or ts is None:
        return None
    typ = str(it.get("type") or "")
    kind = _OP_KIND.get(typ, "other")
    q_done = _i64(it.get("quantityDone"))
    qty = q_done or _i64(it.get("quantity"))
    trades = []
    for tr in ((it.get("tradesInfo") or {}).get("trades") or []):
        if not isinstance(tr, dict):
            continue
        tts = _ts(tr.get("date")) or ts
        tq = _i64(tr.get("quantity"))
        if tq > 0:
            trades.append({"ts": tts, "qty": tq, "price": round(_q(tr.get("price")), 6)})
    return {"id": oid, "ts": ts, "kind": kind, "type": typ,
            "state": str(it.get("state") or ""),
            "figi": it.get("figi") or None, "uid": it.get("instrumentUid") or None,
            "qty": qty, "price": round(_q(it.get("price")), 6),
            "payment": round(_q(it.get("payment")), 4),
            "fee": round(abs(_q(it.get("commission"))), 4),
            "trades": trades, "name": str(it.get("name") or it.get("description") or "")[:80],
            "parent": it.get("parentOperationId") or None}


async def operations(account_id: str, from_ts: float, to_ts: float,
                     instrument_id: str | None = None) -> list[dict] | None:
    """OperationsService/GetOperationsByCursor — ИСПОЛНЕННЫЕ операции счёта за окно (курсор, страницы) →
    список нормализованных (см. _norm_operation) по возрастанию времени. Только чтение.
    Нет токена/счёта, сбой сети или API → None (причина в last_error()). Кэша нет: журнал сам хранит операции."""
    if not (enabled() and account_id):
        return None
    frm = datetime.fromtimestamp(float(from_ts), tz=timezone.utc)
    to = datetime.fromtimestamp(float(to_ts), tz=timezone.utc)
    out: dict[str, dict] = {}
    cursor = ""
    try:
        for _ in range(OPS_MAX_PAGES):
            body: dict = {"accountId": account_id, "from": _iso(frm), "to": _iso(to),
                          "cursor": cursor, "limit": OPS_PAGE, "state": "OPERATION_STATE_EXECUTED",
                          "withoutCommissions": False, "withoutTrades": False, "withoutOvernights": True}
            if instrument_id:
                body["instrumentId"] = instrument_id
            d = await _post(f"{OPS}/GetOperationsByCursor", body, timeout=30.0)
            for it in (d.get("items") or []):
                op = _norm_operation(it)
                if op and op["state"] in ("", "OPERATION_STATE_EXECUTED") and op["id"] not in out:
                    out[op["id"]] = op                     # дубль между страницами — первый (полный) остаётся
            cursor = str(d.get("nextCursor") or "")
            if not d.get("hasNext") or not cursor:
                break
    except Exception as e:                                 # noqa: BLE001
        logger.warning("GetOperationsByCursor: %s", humanize_api_error(e)[:160])
        return None
    return sorted(out.values(), key=lambda o: (o["ts"], o["id"]))


async def dividends(instrument_id: str) -> list[dict] | None:
    to = datetime.now(timezone.utc) + timedelta(days=180)
    frm = datetime.now(timezone.utc) - timedelta(days=400)
    try:
        d = await _post(f"{INS}/GetDividends",
                        {"instrumentId": instrument_id, "from": _iso(frm), "to": _iso(to)})
    except Exception:
        return None
    out = []
    for dv in d.get("dividends", []):
        out.append({
            "value": round(_q(dv.get("dividendNet")), 4),
            "record_date": dv.get("recordDate"),
            "payment_date": dv.get("paymentDate"),
            "yield": round(_q(dv.get("yieldValue")), 3),
        })
    return out[-6:] if out else None


async def fundamentals(asset_uid: str) -> dict | None:
    """Фундаментальные показатели акции (P/E, капитализация, дивдоходность,
    рентабельность и т.д.) через GetAssetFundamentals. Нужен asset_uid."""
    if not asset_uid:
        return None
    try:
        d = await _post(f"{INS}/GetAssetFundamentals", {"assets": [asset_uid]})
    except Exception:
        return None
    arr = d.get("fundamentals") or []
    if not arr:
        return None
    f = arr[0]

    def num(*keys):
        for k in keys:
            v = f.get(k)
            if v not in (None, "", 0, 0.0):
                return v
        return None

    out = {
        "market_cap": num("marketCapitalization"),
        "pe_ttm": num("peRatioTtm", "peRatio"),
        "pb": num("priceToBookTtm", "pbRatio"),
        "ps_ttm": num("priceToSalesTtm"),
        "ev_ebitda": num("evToEbitdaMrq", "evToEbitda"),
        "roe": num("roe"),
        "roa": num("roa"),
        "eps_ttm": num("epsTtm", "dilutedEpsTtm"),
        "revenue_ttm": num("revenueTtm"),
        "ebitda_ttm": num("ebitdaTtm"),
        "net_income_ttm": num("netIncomeTtm"),
        "div_yield_ttm": num("dividendYieldDailyTtm"),
        "div_rate_ttm": num("dividendRateTtm"),
        "beta": num("beta"),
        "free_float": num("freeFloat"),
        "high_52w": num("highPriceLast52Weeks"),
        "low_52w": num("lowPriceLast52Weeks"),
        "shares_out": num("sharesOutstanding"),
        "currency": f.get("currency"),
    }
    out = {k: v for k, v in out.items() if v is not None}
    return out or None


# ──────────────────────────────────────────────────────────────────────
# Локальные индикаторы из свечей
# ──────────────────────────────────────────────────────────────────────

def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return round(e, 6)


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0)
        losses += max(-ch, 0)
    avg_g = gains / period
    avg_l = losses / period
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(ch, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-ch, 0)) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - 100 / (1 + rs), 2)


def _atr(cnd: list[dict], period: int = 14) -> float | None:
    if len(cnd) < period + 1:
        return None
    trs = []
    for i in range(1, len(cnd)):
        h, l, pc = cnd[i]["h"], cnd[i]["l"], cnd[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return round(sum(trs[-period:]) / period, 6)


def _vol_annualized(closes: list[float], window: int = 20) -> float | None:
    if len(closes) < window + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(len(closes) - window, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return round(math.sqrt(var) * math.sqrt(252) * 100, 2)


def daily_metrics(daily: list[dict]) -> dict:
    if not daily:
        return {}
    closes = [c["c"] for c in daily]
    last = closes[-1]
    prev = closes[-2] if len(closes) > 1 else last
    hi20 = max(c["h"] for c in daily[-20:]) if len(daily) >= 1 else None
    lo20 = min(c["l"] for c in daily[-20:]) if len(daily) >= 1 else None
    hi52 = max(c["h"] for c in daily) if daily else None
    lo52 = min(c["l"] for c in daily) if daily else None
    avg_vol20 = round(sum(c["v"] for c in daily[-20:]) / min(20, len(daily)), 0)
    return {
        "last_close": last,
        "prev_close": prev,
        "day_change_pct": round((last - prev) / prev * 100, 2) if prev else None,
        "ema9": _ema(closes, 9), "ema21": _ema(closes, 21),
        "ema50": _ema(closes, 50), "ema200": _ema(closes, 200),
        "rsi14": _rsi(closes, 14), "atr14": _atr(daily, 14),
        "vol_annual_pct": _vol_annualized(closes, 20),
        "high_20d": round(hi20, 6) if hi20 else None,
        "low_20d": round(lo20, 6) if lo20 else None,
        "high_range": round(hi52, 6) if hi52 else None,
        "low_range": round(lo52, 6) if lo52 else None,
        "dist_to_high20_pct": round((hi20 - last) / last * 100, 2) if (hi20 and last) else None,
        "dist_to_low20_pct": round((last - lo20) / last * 100, 2) if (lo20 and last) else None,
        "avg_volume_20d": avg_vol20,
        "above_ema50": (last > _ema(closes, 50)) if _ema(closes, 50) else None,
        "above_ema200": (last > _ema(closes, 200)) if _ema(closes, 200) else None,
    }


def intraday_metrics(intra: list[dict]) -> dict:
    if not intra:
        return {}
    # «сегодня» = свечи последней календарной даты
    last_date = (intra[-1]["t"] or "")[:10]
    today = [c for c in intra if (c["t"] or "")[:10] == last_date]
    if not today:
        today = intra[-78:]
    pv = sum(((c["h"] + c["l"] + c["c"]) / 3) * c["v"] for c in today)
    vv = sum(c["v"] for c in today)
    vwap = round(pv / vv, 6) if vv else None
    o = today[0]["o"]
    last = today[-1]["c"]
    hi = max(c["h"] for c in today)
    lo = min(c["l"] for c in today)
    return {
        "session_date": last_date,
        "session_open": o, "session_last": last,
        "session_high": hi, "session_low": lo,
        "session_range_pct": round((hi - lo) / o * 100, 2) if o else None,
        "session_change_pct": round((last - o) / o * 100, 2) if o else None,
        "vwap": vwap,
        "vs_vwap_pct": round((last - vwap) / vwap * 100, 2) if vwap else None,
        "session_volume": vv,
        "n_bars": len(today),
    }


# ──────────────────────────────────────────────────────────────────────
# ГЛАВНОЕ: собрать полное досье
# ──────────────────────────────────────────────────────────────────────

async def collect_dossier(ticker: str, asset_class: str) -> dict:
    """Полное досье инструмента из Tinkoff. asset_class: share|futures|currency."""
    dossier: dict = {"ticker": ticker, "asset_class": asset_class,
                     "source": "tinkoff", "collected_at": _iso(datetime.now(timezone.utc)),
                     "errors": []}
    inst = None
    try:
        inst = await resolve(ticker, asset_class)
    except Exception as e:
        dossier["errors"].append(f"resolve: {str(e)[:80]}")
    if not inst:
        dossier["errors"].append("инструмент не найден в Tinkoff")
        return dossier

    figi = inst.get("figi") or inst.get("uid")
    uid = inst.get("uid") or figi
    dossier["instrument"] = {
        "name": inst.get("name"),
        "ticker": inst.get("ticker"),
        "figi": figi, "uid": uid,
        "class_code": inst.get("classCode"),
        "currency": inst.get("currency"),
        "lot": inst.get("lot"),
        "sector": inst.get("sector"),
        "exchange": inst.get("exchange"),
        "country": inst.get("countryOfRisk"),
        "expiration": inst.get("expirationDate"),
        "basic_asset": inst.get("basicAsset"),
        "basic_asset_size": _q(inst.get("basicAssetSize")) or None,
        "min_price_increment": _q(inst.get("minPriceIncrement")) or None,
        "nominal": _q(inst.get("nominal")) or None,
        "first_trade": inst.get("first1minCandleDate"),
        "asset_uid": inst.get("assetUid") or inst.get("asset_uid"),
    }

    # Параллельные блоки
    tasks = {
        "last": last_price(figi),
        "status": trading_status(figi),
        "ob": orderbook(figi, depth=50),
        "tape": last_trades(figi, minutes=90),
        "intraday": candles(figi, "5m", 3),
        "hourly": candles(figi, "1h", 14),
        # 200 календарных дней ≈ 138 торговых — этого НЕ хватало на EMA200
        # (нужно ≥200 сессий), и ema200/above_ema200 были вечно None. 400 дн ≈
        # 275 сессий — EMA200 наконец считается.
        "daily": candles(figi, "1d", 400),
    }
    if uid:
        tasks["rsi_tech"] = tech(uid, "RSI", 14, "1h", 20)
        tasks["macd_tech"] = tech(uid, "MACD", 12, "1h", 30)
    if asset_class == "futures":
        tasks["margin"] = futures_margin(figi)
    if asset_class == "share":
        tasks["dividends"] = dividends(figi)
        _auid = inst.get("assetUid") or inst.get("asset_uid")
        if _auid:
            tasks["fundamentals"] = fundamentals(_auid)

    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    res = dict(zip(tasks.keys(), results))

    def ok(key):
        v = res.get(key)
        if isinstance(v, Exception):
            dossier["errors"].append(f"{key}: {str(v)[:60]}")
            return None
        return v

    last = ok("last")
    dossier["price"] = last
    _fund = ok("fundamentals")
    if _fund:
        dossier["fundamentals"] = _fund
    dossier["trading_status"] = ok("status")
    dossier["orderbook"] = ok("ob")
    dossier["tape"] = ok("tape")

    intra = ok("intraday") or []
    hourly = ok("hourly") or []
    daily = ok("daily") or []
    dossier["candles_intraday_5m"] = intra[-120:]   # ~2 дня 5-мин
    dossier["candles_hourly"] = hourly[-80:]
    dossier["candles_daily_tail"] = daily[-60:]     # для ИИ — хвост
    dossier["daily_metrics"] = daily_metrics(daily)
    dossier["intraday_metrics"] = intraday_metrics(intra)

    tech_block = {}
    rsi_t = ok("rsi_tech")
    macd_t = ok("macd_tech")
    if rsi_t:
        tech_block["rsi_tinkoff"] = rsi_t
    if macd_t:
        tech_block["macd_tinkoff"] = macd_t
    if tech_block:
        dossier["tinkoff_tech"] = tech_block

    if asset_class == "futures":
        dossier["margin"] = ok("margin")
    if asset_class == "share":
        dv = ok("dividends")
        if dv:
            dossier["dividends"] = dv

    return dossier


# ══════════════════════════════════════════════════════════════════════════════
# Self-тест (v5.3 фаза 4 · W1): нормализация GetOperationsByCursor на фейке _post — без сети и токена
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import asyncio as _aio

    _real_post, _real_enabled = _post, enabled
    _calls: list[dict] = []
    _PAGES = {
        "": {"hasNext": True, "nextCursor": "c2", "items": [
            {"id": "op-buy", "type": "OPERATION_TYPE_BUY", "state": "OPERATION_STATE_EXECUTED",
             "date": "2026-09-21T07:00:00Z", "figi": "BBG004730N88", "instrumentUid": "uid-sber",
             "quantity": "20", "quantityDone": "20", "price": {"units": "300", "nano": 500000000},
             "payment": {"units": "-6010", "nano": 0, "currency": "rub"},
             "commission": {"units": "-3", "nano": -5000000, "currency": "rub"},
             "tradesInfo": {"trades": [{"num": "1", "date": "2026-09-21T07:00:00Z", "quantity": "10",
                                        "price": {"units": "300", "nano": 0}},
                                       {"num": "2", "date": "2026-09-21T07:00:01Z", "quantity": "10",
                                        "price": {"units": "301", "nano": 0}}]}},
            {"id": "op-fee", "type": "OPERATION_TYPE_BROKER_FEE", "state": "OPERATION_STATE_EXECUTED",
             "date": "2026-09-21T07:00:02Z", "figi": "BBG004730N88", "parentOperationId": "op-buy",
             "payment": {"units": "-3", "nano": -5000000}},
            {"id": "op-cancelled", "type": "OPERATION_TYPE_SELL", "state": "OPERATION_STATE_CANCELED",
             "date": "2026-09-21T07:05:00Z", "figi": "BBG004730N88", "quantity": "20"},
            {"type": "OPERATION_TYPE_SELL", "date": "2026-09-21T07:05:00Z"},              # без id → мимо
        ]},
        "c2": {"hasNext": False, "nextCursor": "", "items": [
            {"id": "op-sell", "type": "OPERATION_TYPE_SELL", "state": "OPERATION_STATE_EXECUTED",
             "date": "2026-09-21T08:00:00Z", "figi": "BBG004730N88", "instrumentUid": "uid-sber",
             "quantity": "20", "price": {"units": "302", "nano": 0},
             "payment": {"units": "6040", "nano": 0}, "commission": {"units": "-3", "nano": -20000000}},
            {"id": "op-buy", "type": "OPERATION_TYPE_BUY", "state": "OPERATION_STATE_EXECUTED",   # дубль между страницами
             "date": "2026-09-21T07:00:00Z", "figi": "BBG004730N88", "quantity": "20"},
            {"id": "op-in", "type": "OPERATION_TYPE_INPUT", "state": "OPERATION_STATE_EXECUTED",
             "date": "2026-09-20T10:00:00Z", "payment": {"units": "100000", "nano": 0}},
        ]},
    }

    async def _fake_post(path, body, timeout=15.0):
        _calls.append({"path": path, "body": body})
        assert path.endswith("OperationsService/GetOperationsByCursor"), path
        assert body["state"] == "OPERATION_STATE_EXECUTED" and body["limit"] == OPS_PAGE
        return _PAGES[body["cursor"]]

    async def _main():
        global _post, enabled
        _post, enabled = _fake_post, (lambda: True)
        ops = await operations("acc-1", 1_758_000_000, 1_758_500_000)
        assert ops is not None and len(_calls) == 2 and _calls[1]["body"]["cursor"] == "c2", _calls
        ids = [o["id"] for o in ops]
        assert ids == ["op-in", "op-buy", "op-fee", "op-sell"], ids        # по времени, без отменённой и без дубля
        b = ops[1]
        assert b["kind"] == "buy" and b["qty"] == 20 and abs(b["price"] - 300.5) < 1e-9 and b["payment"] == -6010.0
        assert abs(b["fee"] - 3.005) < 1e-9 and b["figi"] == "BBG004730N88" and b["uid"] == "uid-sber"
        assert [t["qty"] for t in b["trades"]] == [10, 10] and b["trades"][1]["price"] == 301.0
        f = ops[2]
        assert f["kind"] == "fee" and f["parent"] == "op-buy" and abs(f["payment"] + 3.005) < 1e-9
        s = ops[3]
        assert s["kind"] == "sell" and s["payment"] == 6040.0 and abs(s["fee"] - 3.02) < 1e-9 and s["trades"] == []
        assert ops[0]["kind"] == "other" and ops[0]["type"] == "OPERATION_TYPE_INPUT"
        # фильтр по инструменту уходит в тело; окно — RFC3339
        _calls.clear()
        await operations("acc-1", 1_758_000_000, 1_758_500_000, instrument_id="uid-sber")
        assert _calls[0]["body"]["instrumentId"] == "uid-sber" and _calls[0]["body"]["from"].endswith("Z")
        # сбой сети → None (не исключение)
        async def _boom(path, body, timeout=15.0):
            raise RuntimeError("сети нет")
        _post = _boom
        assert await operations("acc-1", 0, 1) is None
        # без токена → None и ни одного запроса
        enabled = lambda: False                                            # noqa: E731
        _calls.clear()
        assert await operations("acc-1", 0, 1) is None and not _calls
        _post, enabled = _real_post, _real_enabled
        assert _norm_operation({"id": "x"}) is None and _norm_operation("мусор") is None

    _aio.run(_main())

    # ── v5.4.4: два домена, безопасное переключение, коды 40003/40002, источники ошибок, проверка токена ──────────
    async def _domains():
        global _http, BASE, _last_ok_ts
        import os as _os
        _tok, _http0 = config.TINKOFF_TOKEN, _http
        _env0 = _os.environ.pop("PYTHIA_TINKOFF_BASE", None)
        config.TINKOFF_TOKEN = "t.selftest-fake"            # в сеть не уходит: транспорт — MockTransport
        hits: list[tuple[str, str]] = []
        plan: dict = {}                                     # (host, method) → "connect" | "read" | "tls" | int статус

        def handler(req: httpx.Request) -> httpx.Response:
            host, method = req.url.host, req.url.path.rsplit("/", 1)[-1]
            hits.append((host, method))
            act = plan.get((host, method), plan.get((host, "*")))
            if act == "connect":
                raise httpx.ConnectError("Name or service not known", request=req)
            if act == "tls":
                raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed", request=req)
            if act == "read":
                raise httpx.ReadTimeout("read timed out", request=req)
            if isinstance(act, int):
                body = {"code": 16, "message": "40003", "description": "Authentication token is missing or invalid"} \
                    if act == 401 else {"code": 7, "message": "40002", "description": "Insufficient privileges"} \
                    if act == 403 else {"code": 13, "message": "70001", "description": "Internal error"}
                return httpx.Response(act, json=body)
            if method == "GetAccounts":
                return httpx.Response(200, json={"accounts": [{"id": "acc-1", "name": "Брокерский",
                                                               "status": "ACCOUNT_STATUS_OPEN",
                                                               "accessLevel": plan.get("access", "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS")}]})
            if method == "GetLastPrices":
                return httpx.Response(200, json={"lastPrices": [{"price": {"units": 100, "nano": 0}}]})
            return httpx.Response(200, json={"orderId": "EX-1"})

        _http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        TB, TK = _host(BASES[0]), _host(BASES[1])
        try:
            assert BASES[0].startswith("https://invest-public-api.tbank.ru") and "tinkoff.ru" in BASES[1]
            BASE = BASES[0]
            # 1) чтение: соединение не установлено → запасной домен; рабочий адрес запоминается
            plan.clear(); plan[(TB, "*")] = "connect"
            d = await _post(f"{MD}/GetLastPrices", {"instrumentId": ["F"]})
            assert d["lastPrices"] and [h for h, _ in hits] == [TB, TK] and BASE == BASES[1], (hits, BASE)
            hits.clear(); plan.clear()
            await _post(f"{MD}/GetLastPrices", {"instrumentId": ["F"]})
            assert [h for h, _ in hits] == [TK], hits           # сразу рабочий
            # 2) заявка: соединение не установлено (запрос точно не ушёл) → второй домен можно
            BASE = BASES[0]; hits.clear(); plan.clear(); plan[(TB, "*")] = "tls"
            await _post("tinkoff.public.invest.api.contract.v1.OrdersService/PostOrder", {"orderId": "u1"})
            assert [h for h, _ in hits] == [TB, TK], hits
            # 3) заявка: ответ потерян (таймаут чтения) → НЕ повторять на другом домене
            BASE = BASES[0]; hits.clear(); plan.clear(); plan[(TB, "*")] = "read"
            try:
                await _post("tinkoff.public.invest.api.contract.v1.OrdersService/PostOrder", {"orderId": "u2"})
                raise AssertionError("потерянный ответ заявки обязан подняться")
            except httpx.ReadTimeout:
                pass
            assert [h for h, _ in hits] == [TB] and BASE == BASES[0], hits
            #    …а чтение при таймауте чтения — на запасной
            hits.clear()
            await _post(f"{MD}/GetLastPrices", {"instrumentId": ["F"]})
            assert [h for h, _ in hits] == [TB, TK], hits
            # 4) 5xx: чтение — на запасной, заявка/отмена — нет (ни PostOrder, ни CancelOrder, ни стопы)
            BASE = BASES[0]; hits.clear(); plan.clear(); plan[(TB, "*")] = 500
            await _post(f"{MD}/GetLastPrices", {"instrumentId": ["F"]})
            assert [h for h, _ in hits] == [TB, TK], hits
            for m_ in ("OrdersService/CancelOrder", "StopOrdersService/PostStopOrder", "StopOrdersService/CancelStopOrder"):
                BASE = BASES[0]; hits.clear()
                try:
                    await _post(f"tinkoff.public.invest.api.contract.v1.{m_}", {})
                    raise AssertionError(m_)
                except TinkoffError as e:
                    assert e.status == 500
                assert [h for h, _ in hits] == [TB], (m_, hits)
            # 5) 4xx не переключают; текст — с кодом Т-Банка 40003 (не gRPC 16) и подсказкой; вид — auth
            BASE = BASES[0]; hits.clear(); plan.clear(); plan[(TB, "*")] = 401
            try:
                await _post(f"{USERS}/GetAccounts", {})
                raise AssertionError("401 обязан подняться")
            except TinkoffError as e:
                assert "401 · 40003" in str(e) and "16" not in str(e).split("[")[0], str(e)
                assert "токен Т-Банка не принят" in humanize_api_error(e) and "«Ключи»" in humanize_api_error(e)
            assert [h for h, _ in hits] == [TB], hits
            le = last_error()
            assert le["code"] == "40003" and le["status"] == 401 and le["kind"] == "auth" and le["source"] == "account", le
            # 6) источники: успех рынка не прячет отказ заявок; успех того же источника — прячет
            plan.clear(); plan[(TB, "PostOrder")] = 403
            try:
                await _post("tinkoff.public.invest.api.contract.v1.OrdersService/PostOrder", {"orderId": "u3"})
            except TinkoffError:
                pass
            await _post(f"{MD}/GetLastPrices", {"instrumentId": ["F"]})
            er = errors()
            assert er["orders"]["kind"] == "rights" and er["orders"]["code"] == "40002" and not er["orders"]["stale"], er
            assert er["account"]["kind"] == "auth" and not er["account"]["stale"]
            ts = token_state()
            assert ts and ts["ok"] is False and ts["kind"] in ("auth", "rights"), ts
            # 7) проверка токена: FULL_ACCESS → trade; READ_ONLY → принят, но без торговли; 401 → auth
            plan.clear()
            r = await check_access()
            assert r["ok"] and r["trade"] is True and r["access"] == "FULL_ACCESS" and "полный доступ" in r["reason"], r
            assert errors()["account"]["stale"] is True, errors()     # успех счёта снял «свежесть» его 401
            plan["access"] = "ACCOUNT_ACCESS_LEVEL_READ_ONLY"
            r = await check_access()
            assert r["ok"] and r["trade"] is False and r["kind"] == "rights" and "только для чтения" in r["reason"], r
            plan.clear(); plan[(TB, "GetAccounts")] = 401; plan[(TK, "GetAccounts")] = 401
            r = await check_access()
            assert not r["ok"] and r["kind"] == "auth" and "40003" in r["reason"], r
            accs = await accounts()
            assert accs is None and "токен Т-Банка не принят" in failure_text("x")
            plan.clear()
            accs = await accounts()
            assert accs and accs[0]["access"] == "FULL_ACCESS", accs
            # 8) цена: сбой по токену помнится по инструменту и снимается ценой
            _price_cache.clear(); plan.clear(); plan[(TB, "GetLastPrices")] = 401; plan[(TK, "GetLastPrices")] = 401
            BASE = BASES[0]
            assert await last_price("FIGI-X") is None
            pe = price_error("FIGI-X")
            assert pe and pe["kind"] == "auth", pe
            plan.clear(); _price_cache.clear()
            assert (await last_price("FIGI-X"))["price"] == 100.0 and price_error("FIGI-X") is None
            # 9) смена токена: ошибки в прошлое, эпоха растёт
            ep = token_epoch()
            reset_errors()
            assert last_error() is None and errors() == {} and token_state() is None and token_epoch() == ep + 1
            # 10) PYTHIA_TINKOFF_BASE — один адрес, без запасного
            _os.environ["PYTHIA_TINKOFF_BASE"] = "https://example-proxy.local/rest/"
            assert bases() == ["https://example-proxy.local/rest"], bases()
            _os.environ.pop("PYTHIA_TINKOFF_BASE", None)
            assert bases()[0] in BASES and len(bases()) == 2
            # 11) классификация без HTTP: строки брокера, TLS, сеть, прочее
            assert classify("Tinkoff 403 · 40002: Insufficient privileges [PostOrder]")[0] == "rights"
            assert classify("Tinkoff 401 16: Authentication token is missing or invalid (40003) [PostOrder]")[0] == "auth"
            assert classify(httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]"))[0] == "cert"
            assert classify(httpx.ConnectTimeout("connect timeout"))[0] == "network"
            assert classify(TinkoffError(400, "3", "30042", "Not enough assets"))[0] == "other"
            assert classify(TinkoffError(429, "8", "80002", "limit"))[0] == "network"
        finally:
            await _http.aclose()
            _http = _http0
            config.TINKOFF_TOKEN = _tok
            BASE = BASES[0]
            reset_errors()
            if _env0 is not None:
                _os.environ["PYTHIA_TINKOFF_BASE"] = _env0

    _aio.run(_domains())
    print("tinkoff self-test OK (operations; v5.4.4: tbank.ru основной / tinkoff.ru запасной — переключение только когда "
          "запрос точно не ушёл, чтение ещё при таймауте и 5xx, заявки/отмены/стопы при потерянном ответе и 5xx не "
          "повторяются, 4xx не переключают, рабочий адрес помнится, PYTHIA_TINKOFF_BASE — один адрес; коды 40003/40002 "
          "с подсказкой; ошибки по источникам рынок/заявки/счёт; проверка токена FULL_ACCESS/READ_ONLY/401)")
