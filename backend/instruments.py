"""ОРАКУЛ // ПИФИЯ — каталог инструментов для выбора оператором.

Каждый инструмент: code (базовый тикер), name, asset_class, category,
weather (список регионов, где погода реально двигает спрос — только для
энергии/сырья; для акций/валют/индексов пусто).

Для фьючерсов code — базовый код (BR/NG/SI...); резолвер Tinkoff сам
подберёт ближайший активный контракт (авто-ролловер).
"""
from __future__ import annotations

# Погодные узлы (lat, lon) — там, где холод/жара двигают спрос на энергию.
W_EU = [
    {"name": "Амстердам (хаб TTF)", "lat": 52.37, "lon": 4.90},
    {"name": "Берлин",             "lat": 52.52, "lon": 13.40},
    {"name": "Франкфурт",          "lat": 50.11, "lon": 8.68},
]
W_US = [
    {"name": "Чикаго",             "lat": 41.88, "lon": -87.63},
    {"name": "Нью-Йорк",           "lat": 40.71, "lon": -74.01},
    {"name": "Хьюстон (Texas)",    "lat": 29.76, "lon": -95.37},
]
W_GULF = [
    {"name": "Мексиканский залив", "lat": 27.50, "lon": -90.00},
]

CATALOG: list[dict] = [
    # ── Энергия (фьючерсы FORTS) ──────────────────────────────────────
    {"code": "BR", "name": "Brent — нефть",        "asset_class": "futures",
     "category": "Энергия", "weather": W_GULF},
    {"code": "NG", "name": "Природный газ (Henry Hub)", "asset_class": "futures",
     "category": "Энергия", "weather": W_US},
    {"code": "TTF", "name": "Газ TTF (Европа)",    "asset_class": "futures",
     "category": "Энергия", "weather": W_EU},

    # ── Валюта (фьючерсы) ─────────────────────────────────────────────
    {"code": "SI", "name": "USD/RUB",              "asset_class": "futures",
     "category": "Валюта", "weather": []},
    {"code": "EU", "name": "EUR/RUB",              "asset_class": "futures",
     "category": "Валюта", "weather": []},
    {"code": "CR", "name": "CNY/RUB (юань)",       "asset_class": "futures",
     "category": "Валюта", "weather": []},

    # ── Металлы (фьючерсы) ────────────────────────────────────────────
    {"code": "GD", "name": "Золото",               "asset_class": "futures",
     "category": "Металлы", "weather": []},
    {"code": "SV", "name": "Серебро",              "asset_class": "futures",
     "category": "Металлы", "weather": []},

    # ── Индексы (фьючерсы) ────────────────────────────────────────────
    {"code": "MX", "name": "Индекс МосБиржи",      "asset_class": "futures",
     "category": "Индексы", "weather": []},
    {"code": "RI", "name": "Индекс РТС",           "asset_class": "futures",
     "category": "Индексы", "weather": []},

    # ── Акции РФ ──────────────────────────────────────────────────────
    {"code": "SBER", "name": "Сбербанк",           "asset_class": "share",
     "category": "Акции", "weather": []},
    {"code": "GAZP", "name": "Газпром",            "asset_class": "share",
     "category": "Акции", "weather": []},
    {"code": "LKOH", "name": "Лукойл",             "asset_class": "share",
     "category": "Акции", "weather": []},
    {"code": "ROSN", "name": "Роснефть",           "asset_class": "share",
     "category": "Акции", "weather": []},
    {"code": "NVTK", "name": "Новатэк",            "asset_class": "share",
     "category": "Акции", "weather": []},
    {"code": "GMKN", "name": "Норникель",          "asset_class": "share",
     "category": "Акции", "weather": []},
    {"code": "TATN", "name": "Татнефть",           "asset_class": "share",
     "category": "Акции", "weather": []},
    {"code": "PLZL", "name": "Полюс",              "asset_class": "share",
     "category": "Акции", "weather": []},
    {"code": "YDEX", "name": "Яндекс",             "asset_class": "share",
     "category": "Акции", "weather": []},
    {"code": "T",    "name": "Т-Технологии",       "asset_class": "share",
     "category": "Акции", "weather": []},
    {"code": "VTBR", "name": "ВТБ",                "asset_class": "share",
     "category": "Акции", "weather": []},
    {"code": "MOEX", "name": "Московская биржа",   "asset_class": "share",
     "category": "Акции", "weather": []},
    {"code": "OZON", "name": "Ozon",               "asset_class": "share",
     "category": "Акции", "weather": []},
    {"code": "MGNT", "name": "Магнит",             "asset_class": "share",
     "category": "Акции", "weather": []},
    {"code": "CHMF", "name": "Северсталь",         "asset_class": "share",
     "category": "Акции", "weather": []},
    {"code": "ALRS", "name": "Алроса",             "asset_class": "share",
     "category": "Акции", "weather": []},
]

import asyncio
import time
import logging
from datetime import date

import httpx

logger = logging.getLogger("pythia.instruments")

_CURATED_BY_CODE = {x["code"]: x for x in CATALOG}

# Динамическая вселенная всей биржи (акции TQBR + активные фьючерсы FORTS).
# Кэш в памяти, обновляется раз в 6 часов. Курируемые инструменты (с погодой,
# авто-ролловером по 2-буквенному коду) сохраняют свои метаданные.
_universe: list[dict] = []
_uni_by_code: dict[str, dict] = {}
_uni_ts: float = 0.0
_UNI_TTL = 6 * 3600


_http: "httpx.AsyncClient | None" = None
_http_lock = asyncio.Lock()


async def _client() -> "httpx.AsyncClient":
    global _http
    h = _http
    if h is not None and not h.is_closed:
        return h
    async with _http_lock:
        if _http is None or _http.is_closed:
            _http = httpx.AsyncClient(
                timeout=httpx.Timeout(25.0), follow_redirects=True,
                limits=httpx.Limits(max_keepalive_connections=4,
                                    max_connections=8, keepalive_expiry=60.0))
        return _http


async def aclose() -> None:
    global _http
    h, _http = _http, None
    if h is not None and not h.is_closed:
        try:
            await h.aclose()
        except Exception:
            pass


async def _iss(path: str, params: dict) -> dict | None:
    url = f"https://iss.moex.com/iss/{path}.json"
    try:
        cl = await _client()
        r = await cl.get(url, params=params)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning("universe ISS %s: %s", path, str(e)[:120])
        return None


_uni_lock = asyncio.Lock()
_uni_fail_ts: float = 0.0
_UNI_FAIL_COOLDOWN = 120.0


async def fetch_universe(force: bool = False) -> list[dict]:
    """Подтянуть ВСЮ биржу: акции TQBR + активные фьючерсы FORTS. Кэш 6ч."""
    global _universe, _uni_by_code, _uni_ts, _uni_fail_ts
    if _universe and not force and (time.time() - _uni_ts) < _UNI_TTL:
        return _universe
    # single-flight: старт греет вселенную в фоне, а первый /api/instruments
    # тоже её дёргает — без замка обе тянули полный каталог параллельно; при
    # недоступном ISS кэш не бился и каждый заход повторял загрузку заново.
    async with _uni_lock:
        if _universe and not force and (time.time() - _uni_ts) < _UNI_TTL:
            return _universe
        if not force and (time.time() - _uni_fail_ts) < _UNI_FAIL_COOLDOWN:
            return _universe   # недавняя неудача — отдаём что есть (курируемый каталог)
        return await _fetch_universe_inner()


async def _fetch_universe_inner() -> list[dict]:
    global _universe, _uni_by_code, _uni_ts, _uni_fail_ts
    items: list[dict] = []

    j = await _iss("engines/stock/markets/shares/boards/TQBR/securities",
                   {"iss.only": "securities", "securities.columns": "SECID,SHORTNAME"})
    if j:
        b = j.get("securities", {})
        cols = b.get("columns", [])
        if "SECID" in cols and "SHORTNAME" in cols:
            ci, ni = cols.index("SECID"), cols.index("SHORTNAME")
            for row in b.get("data", []):
                code = (row[ci] or "").strip()
                if code:
                    items.append({"code": code, "name": (row[ni] or code),
                                  "asset_class": "share", "category": "Акции", "weather": []})

    j = await _iss("engines/futures/markets/forts/securities",
                   {"iss.only": "securities",
                    "securities.columns": "SECID,SHORTNAME,LASTTRADEDATE"})
    if j:
        b = j.get("securities", {})
        cols = b.get("columns", [])
        if "SECID" in cols and "SHORTNAME" in cols:
            ci, ni = cols.index("SECID"), cols.index("SHORTNAME")
            li = cols.index("LASTTRADEDATE") if "LASTTRADEDATE" in cols else None
            today = date.today().isoformat()
            for row in b.get("data", []):
                code = (row[ci] or "").strip()
                if not code:
                    continue
                if li is not None and (row[li] or "") and row[li] < today:
                    continue
                items.append({"code": code, "name": (row[ni] or code),
                              "asset_class": "futures", "category": "Фьючерсы", "weather": []})

    if items:
        _universe = items
        _uni_ts = time.time()
        m = {it["code"]: it for it in items}
        m.update(_CURATED_BY_CODE)  # курируемые перекрывают (метаданные/погода)
        _uni_by_code = m
        logger.info("вселенная MOEX загружена: %d инструментов", len(items))
    else:
        _uni_fail_ts = time.time()   # ISS недоступен — кулдаун перед следующей попыткой
    return _universe


import re as _re
_FUT_RX = _re.compile(r"^[A-Z]{2}[FGHJKMNQUVXZ]\d$")   # BRQ6, SiU6, NGN6…


def guess_asset_class(code: str) -> str:
    """Код не найден в каталоге/вселенной (MOEX недоступен на старте и т.п.):
    угадываем класс, чтобы сбор данных пошёл по верной ветке, а не в «share»."""
    c = (code or "").upper().strip()
    if _FUT_RX.match(c) or len(c) <= 2:
        return "futures"
    return "share"


def get(code: str) -> dict | None:
    code = (code or "").upper().strip()
    it = _CURATED_BY_CODE.get(code)
    if it:
        return it
    return _uni_by_code.get(code)


def _pub(it: dict) -> dict:
    return {"code": it["code"], "name": it["name"],
            "asset_class": it["asset_class"], "has_weather": bool(it.get("weather"))}


_ORDER = ["Энергия", "Валюта", "Металлы", "Индексы", "Акции", "Фьючерсы"]


def all_grouped() -> dict:
    groups: dict[str, list] = {}
    seen: set[str] = set()
    for it in CATALOG:                       # курируемые первыми (спец-группы + топ-акции)
        groups.setdefault(it["category"], []).append(_pub(it))
        seen.add(it["code"])
    for it in _universe:                     # затем вся остальная биржа
        if it["code"] in seen:
            continue
        groups.setdefault(it["category"], []).append(_pub(it))
        seen.add(it["code"])
    ordered = {k: groups[k] for k in _ORDER if k in groups}
    for k, v in groups.items():
        if k not in ordered:
            ordered[k] = v
    return ordered
