"""Учёт расходов на DeepSeek: сколько токенов и денег ушло — по фичам и по дням.

Каждый вызов модели (через хук `LLM.on_usage`) пишется строкой в таблицу `llm_usage`:
момент, дата (UTC), модель, «маршрут» (за что был вызов: chat, deep, digest…), режим,
токены и стоимость в долларах. Цену считаем по документированному прайсу DeepSeek
(`PRICES`, цена за 1 млн токенов), с раздельным тарифом на попадание в кэш и промах и
с учётом «пикового» окна (дороже в будни ночью и утром по UTC).

Отсюда дашборд и `/status` берут:
  • `record(...)`  — записать один вызов и вернуть его стоимость;
  • `summary(...)` — сводку за N дней (по маршрутам, дням и моделям);
  • `balance(...)` — живой остаток на счёте DeepSeek (read-only, ключи не логируются;
                    при нескольких ключах опрашивает каждый и помечает «живой»).

Маршрут вызова помечается контекстным менеджером `route("deep")`: агент оборачивает
им свои этапы, а `record` без явного `route` берёт текущий из контекста.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Iterator

import httpx

from .timeutil import UTC, iso, now_utc

log = logging.getLogger("oracle.usage")

# ── прайс DeepSeek ────────────────────────────────────────────────────────────
# Цена за 1 000 000 токенов в USD, кортеж (off-peak, peak).
#   flash = deepseek-flash (быстрый режим), pro = deepseek-v4-pro (глубокий).
#   input_miss — токены запроса, которых не было в кэше; input_hit — попадание в кэш
#   (в разы дешевле); output — токены ответа.
# Источник: документированный прайс DeepSeek (off-peak / peak, пик — будни ночь+утро UTC),
# переданный в задании этого модуля. Меняется у провайдера — правится здесь.
PRICES: dict[str, dict[str, tuple[float, float]]] = {
    "deepseek-flash": {
        "input_miss": (0.15, 0.30),
        "input_hit": (0.003, 0.006),
        "output": (0.60, 1.20),
    },
    "deepseek-v4-pro": {
        "input_miss": (0.66, 1.32),
        "input_hit": (0.022, 0.044),
        "output": (1.98, 3.96),
    },
}

_PRO = "deepseek-v4-pro"
_FLASH = "deepseek-flash"

# ── схема таблицы ─────────────────────────────────────────────────────────────
# Интегратор добавит USAGE_SCHEMA в общий SCHEMA (oracle/db.py). До этого record()
# создаёт таблицу лениво (CREATE TABLE IF NOT EXISTS), так что модуль работает и в одиночку.
USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,                     -- UTC ISO момент вызова
    day               TEXT NOT NULL,                     -- UTC дата 'YYYY-MM-DD'
    model             TEXT NOT NULL DEFAULT '',
    route             TEXT NOT NULL DEFAULT 'chat',      -- за что вызов: chat|deep|digest|…
    deep              INTEGER NOT NULL DEFAULT 0,        -- 1 — глубокий режим (размышление)
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,        -- всего токенов запроса (hit+miss)
    completion_tokens INTEGER NOT NULL DEFAULT 0,        -- токенов ответа
    cached_tokens     INTEGER NOT NULL DEFAULT 0,        -- из prompt_tokens попало в кэш
    cost_usd          REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_llm_usage_day ON llm_usage(day);
"""

_INSERT = (
    "INSERT INTO llm_usage(ts, day, model, route, deep, prompt_tokens, completion_tokens, "
    "cached_tokens, cost_usd) VALUES(?,?,?,?,?,?,?,?,?)"
)

# известные маршруты (за что бывает вызов модели) — для дашборда/справки
ROUTES = ("chat", "deep", "digest", "reflect", "idea", "brief", "draft", "summary")

# ── маршрут вызова ────────────────────────────────────────────────────────────
current_route: ContextVar[str] = ContextVar("oracle_current_route", default="chat")


@contextmanager
def route(name: str) -> Iterator[None]:
    """Пометить, за что идут вызовы модели внутри блока: `with route("deep"): ...`.
    Вложенные блоки восстанавливают внешний маршрут на выходе."""
    token = current_route.set(name or "chat")
    try:
        yield
    finally:
        current_route.reset(token)


# ── тарификация ───────────────────────────────────────────────────────────────
def is_peak(at: datetime) -> bool:
    """Пиковое окно DeepSeek: будни (Пн–Пт), 00:00–04:00 и 06:00–10:00 UTC. Иначе — off-peak."""
    u = at.astimezone(UTC) if at.tzinfo else at.replace(tzinfo=UTC)
    if u.weekday() >= 5:                 # суббота, воскресенье — всегда off-peak
        return False
    h = u.hour
    return (0 <= h < 4) or (6 <= h < 10)


def price_key(model: str, deep: bool = False) -> str:
    """Строка модели → ключ в PRICES. Незнакомую относим к pro/flash по флагу deep."""
    m = (model or "").strip().lower()
    if m in PRICES:
        return m
    if "pro" in m:
        return _PRO
    if "flash" in m or "chat" in m:
        return _FLASH
    return _PRO if deep else _FLASH


def split_tokens(usage: dict) -> tuple[int, int, int]:
    """(попало_в_кэш, промах, токены_ответа) из usage модели.

    Если полей кэша нет — весь запрос считаем промахом (дороже, честнее к владельцу).
    Если дано только одно из полей — второе добираем из prompt_tokens.
    """
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    hit_raw = usage.get("prompt_cache_hit_tokens")
    miss_raw = usage.get("prompt_cache_miss_tokens")
    if hit_raw is None and miss_raw is None:
        return 0, prompt, completion
    hit = int(hit_raw or 0)
    miss = int(miss_raw or 0)
    if hit_raw is not None and miss_raw is None:
        miss = max(prompt - hit, 0)
    elif miss_raw is not None and hit_raw is None:
        hit = max(prompt - miss, 0)
    return hit, miss, completion


def cost(model: str, usage: dict, *, peak: bool, deep: bool = False) -> float:
    """Стоимость одного вызова в USD по PRICES. peak — брать пиковый тариф."""
    hit, miss, completion = split_tokens(usage)
    p = PRICES[price_key(model, deep)]
    i = 1 if peak else 0
    total = miss * p["input_miss"][i] + hit * p["input_hit"][i] + completion * p["output"][i]
    return total / 1_000_000


# ── запись вызова ─────────────────────────────────────────────────────────────
async def _ensure_table(db: Any) -> None:
    """Создать таблицу и индекс, если их ещё нет (до интеграции в db.py)."""
    for stmt in USAGE_SCHEMA.split(";"):
        s = stmt.strip()
        if s:
            await db.execute(s)


def _missing_table(exc: Exception) -> bool:
    return "no such table" in str(exc).lower()


async def record(db: Any, *, model: str, deep: bool = False, route: str | None = None,
                 usage: dict, at: datetime | None = None,
                 peak_pricing: bool | None = None, cfg: Any = None) -> float:
    """Записать один вызов модели и вернуть его стоимость в USD.

    model  — имя модели (LLMResponse.model); deep — был ли глубокий режим;
    route  — за что вызов (см. ROUTES); None → текущий из `route(...)`;
    usage  — dict из ответа модели (prompt_tokens, completion_tokens,
             prompt_cache_hit_tokens / prompt_cache_miss_tokens);
    at     — момент вызова (по умолчанию сейчас), он же решает пик/офф-пик и дату;
    peak_pricing — брать ли пиковый тариф в пиковом окне; None → cfg.usage_peak_pricing
             (по умолчанию True).

    Таблицу создаёт лениво, если её ещё нет. Хранит момент и дату в UTC.
    """
    at = at or now_utc()
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    u = at.astimezone(UTC)
    if peak_pricing is None:
        peak_pricing = bool(getattr(cfg, "usage_peak_pricing", True)) if cfg is not None else True
    peak = bool(peak_pricing) and is_peak(u)

    hit, miss, completion = split_tokens(usage)
    prompt_total = int(usage.get("prompt_tokens") or 0) or (hit + miss)
    c = cost(model, usage, peak=peak, deep=deep)
    r = route if route is not None else current_route.get()

    params = (iso(u), u.strftime("%Y-%m-%d"), model or "", r or "chat", 1 if deep else 0,
              prompt_total, completion, hit, c)
    try:
        await db.execute(_INSERT, params)
    except Exception as e:                      # таблицы ещё нет — создаём и повторяем один раз
        if not _missing_table(e):
            raise
        await _ensure_table(db)
        await db.execute(_INSERT, params)
    return c


# ── сводка ────────────────────────────────────────────────────────────────────
async def _fetchall(db: Any, sql: str, params: tuple) -> list[dict]:
    """fetchall, но если таблицы ещё нет — создать её и вернуть пусто (свежая база)."""
    try:
        return await db.fetchall(sql, params)
    except Exception as e:
        if not _missing_table(e):
            raise
        await _ensure_table(db)
        return await db.fetchall(sql, params)


async def summary(db: Any, days: int = 30) -> dict:
    """Сводка расходов за последние `days` дней (включая сегодня, по UTC-датам).

    → {total_cost, total_calls, prompt_tokens, completion_tokens,
       by_route: [{route, calls, cost}], by_day: [{day, cost, calls}],
       by_model: [{model, calls, cost}], days, since}
    Списки: маршруты и модели — по убыванию расходов, дни — по возрастанию даты.
    """
    days = max(int(days), 1)
    since = (now_utc().date() - timedelta(days=days - 1)).isoformat()

    tot = await _fetchall(
        db, "SELECT COUNT(*) AS calls, COALESCE(SUM(cost_usd),0) AS cost, "
            "COALESCE(SUM(prompt_tokens),0) AS pt, COALESCE(SUM(completion_tokens),0) AS ct "
            "FROM llm_usage WHERE day >= ?", (since,))
    t = tot[0] if tot else {"calls": 0, "cost": 0.0, "pt": 0, "ct": 0}

    by_route = await _fetchall(
        db, "SELECT route, COUNT(*) AS calls, COALESCE(SUM(cost_usd),0) AS cost "
            "FROM llm_usage WHERE day >= ? GROUP BY route ORDER BY cost DESC, calls DESC", (since,))
    by_day = await _fetchall(
        db, "SELECT day, COALESCE(SUM(cost_usd),0) AS cost, COUNT(*) AS calls "
            "FROM llm_usage WHERE day >= ? GROUP BY day ORDER BY day ASC", (since,))
    by_model = await _fetchall(
        db, "SELECT model, COUNT(*) AS calls, COALESCE(SUM(cost_usd),0) AS cost "
            "FROM llm_usage WHERE day >= ? GROUP BY model ORDER BY cost DESC, calls DESC", (since,))

    return {
        "total_cost": float(t["cost"] or 0.0),
        "total_calls": int(t["calls"] or 0),
        "prompt_tokens": int(t["pt"] or 0),
        "completion_tokens": int(t["ct"] or 0),
        "by_route": [{"route": r["route"], "calls": int(r["calls"]), "cost": float(r["cost"])}
                     for r in by_route],
        "by_day": [{"day": r["day"], "cost": float(r["cost"]), "calls": int(r["calls"])}
                   for r in by_day],
        "by_model": [{"model": r["model"], "calls": int(r["calls"]), "cost": float(r["cost"])}
                     for r in by_model],
        "days": days,
        "since": since,
    }


# ── живой баланс DeepSeek ─────────────────────────────────────────────────────
def _to_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


async def _key_balance(client: httpx.AsyncClient, url: str, key: str,
                       label: str = "") -> dict | None:
    """Остаток по одному ключу: {is_available, balances: [{currency, total}]} или None при ошибке.

    Ключ уходит только в заголовок запроса; в лог — никогда (лишь порядковый номер, если задан).
    """
    where = f" ({label})" if label else ""
    try:
        r = await client.get(url, headers={"Authorization": f"Bearer {key}",
                                           "Accept": "application/json"})
        if r.status_code != 200:
            log.warning("баланс DeepSeek%s: HTTP %s", where, r.status_code)
            return None
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("баланс DeepSeek%s недоступен: %s", where, type(e).__name__)   # ключ не логируем
        return None
    infos = data.get("balance_infos") or []
    balances = [{"currency": str(b.get("currency") or ""), "total": _to_float(b.get("total_balance"))}
                for b in infos]
    return {"is_available": bool(data.get("is_available")), "balances": balances}


async def balance(cfg: Any, http: httpx.AsyncClient | None = None, *,
                  live_index: int | None = None) -> dict | None:
    """Остаток на счёте DeepSeek (read-only, ключи не логируются).

    Один ключ → прежний контракт: {is_available, balances: [{currency, total}]} или None.

    Несколько ключей (DEEPSEEK_API_KEY=k1,k2 — клиент уходит на запасной при 401/402): опрашиваем
    каждый и добавляем разбивку `keys` [{index, live, is_available, balances}] с пометкой «живого».
    Ошибка по конкретному ключу → его is_available/balances = None (остальные не страдают).
    Верхний уровень (is_available/balances — его читает дашборд) показывает баланс живого ключа,
    если он известен (live_index — это LLM._key_idx, см. cross_group), иначе первого доступного,
    иначе первого ответившего — так на панели не всплывёт «ноль» мёртвого запасного ключа.

    Работает только для DeepSeek (cfg.is_deepseek) и при наличии хотя бы одного ключа.
    Клиент можно подставить (тесты: MockTransport).
    """
    if not getattr(cfg, "is_deepseek", False):
        return None
    keys = tuple(getattr(cfg, "api_keys", ()) or ())
    if not keys:
        return None
    base = str(getattr(cfg, "llm_base_url", "https://api.deepseek.com")).rstrip("/")
    url = f"{base}/user/balance"

    own = http is None
    client = http or httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    try:
        if len(keys) == 1:                       # один ключ — прежний контракт без разбивки
            return await _key_balance(client, url, keys[0])
        per_key = [await _key_balance(client, url, k, f"ключ №{i + 1}")
                   for i, k in enumerate(keys)]
    finally:
        if own:
            await client.aclose()

    live = live_index % len(keys) if isinstance(live_index, int) else None
    entries = [{"index": i + 1, "live": i == live,
                "is_available": (b["is_available"] if b else None),
                "balances": (b["balances"] if b else None)}
               for i, b in enumerate(per_key)]
    # верхний уровень (совместимость с дашбордом): живой ключ, иначе первый с доступным счётом,
    # иначе первый ответивший — чтобы не показать остаток мёртвого/пустого ключа
    primary = per_key[live] if (live is not None and per_key[live]) else None
    primary = primary or next((b for b in per_key if b and b["is_available"]), None)
    primary = primary or next((b for b in per_key if b), None)
    primary = primary or {"is_available": False, "balances": []}
    return {"is_available": primary["is_available"], "balances": primary["balances"],
            "keys": entries, "live_index": (live + 1) if live is not None else None}


# ── хук для интегратора ───────────────────────────────────────────────────────
def make_hook(db: Any, cfg: Any = None, *,
              on_error: Callable[[Exception], None] | None = None) -> Callable[[dict], Awaitable[None]]:
    """Готовый безопасный колбэк для `LLM.on_usage`: любой сбой учёта не ломает ответ.

    Интегратор: `llm.on_usage = usage.make_hook(db, cfg)`. Ожидает dict вида
    {"model": str, "deep": bool, "usage": dict, "route": str|None, "at": datetime|None}.
    """
    async def hook(info: dict) -> None:
        try:
            await record(db, model=info.get("model", ""), deep=bool(info.get("deep")),
                         route=info.get("route"), usage=info.get("usage") or {},
                         at=info.get("at"), cfg=cfg)
        except Exception as e:                  # noqa: BLE001 — учёт не должен ронять диалог
            if on_error is not None:
                on_error(e)
            else:
                log.warning("учёт расхода не записан: %r", e)

    return hook
