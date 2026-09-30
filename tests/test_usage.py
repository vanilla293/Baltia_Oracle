"""Учёт расходов DeepSeek: тарификация, запись, сводка, баланс.

Сеть — только httpx.MockTransport. Никаких обращений к настоящему API.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from oracle import usage
from oracle.usage import (PRICES, balance, cost, current_route, is_peak, price_key,
                          record, route, split_tokens, summary)

UTC = timezone.utc


def dt(y=2026, m=9, d=28, hh=5, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


# ── тарификация: пиковое окно ───────────────────────────────────────────────
def test_is_peak_windows():
    # понедельник 2026-09-28
    assert is_peak(dt(hh=0, mm=0)) is True          # начало первого окна
    assert is_peak(dt(hh=3, mm=59)) is True
    assert is_peak(dt(hh=4, mm=0)) is False         # 04:00 — уже off-peak
    assert is_peak(dt(hh=5, mm=0)) is False         # между окнами
    assert is_peak(dt(hh=6, mm=0)) is True          # начало второго окна
    assert is_peak(dt(hh=9, mm=59)) is True
    assert is_peak(dt(hh=10, mm=0)) is False        # 10:00 — off-peak
    assert is_peak(dt(hh=12, mm=0)) is False


def test_is_peak_weekend_never():
    sat = dt(m=10, d=3, hh=7)     # суббота, время пикового окна
    sun = dt(m=10, d=4, hh=1)     # воскресенье, ночь
    assert sat.weekday() == 5 and sun.weekday() == 6
    assert is_peak(sat) is False
    assert is_peak(sun) is False


def test_is_peak_naive_treated_as_utc():
    naive = datetime(2026, 9, 28, 6, 30)   # без tz — считаем UTC
    assert is_peak(naive) is True


# ── тарификация: стоимость ──────────────────────────────────────────────────
def test_cost_flash_offpeak_and_peak():
    u = {"prompt_tokens": 3000, "prompt_cache_hit_tokens": 1000,
         "prompt_cache_miss_tokens": 2000, "completion_tokens": 500}
    # off-peak: 2000*0.15 + 1000*0.003 + 500*0.60 = 300 + 3 + 300 = 603 / 1e6
    assert cost("deepseek-flash", u, peak=False) == pytest.approx(603 / 1_000_000)
    # peak:     2000*0.30 + 1000*0.006 + 500*1.20 = 600 + 6 + 600 = 1206 / 1e6
    assert cost("deepseek-flash", u, peak=True) == pytest.approx(1206 / 1_000_000)


def test_cost_pro_offpeak_and_peak():
    u = {"prompt_tokens": 3000, "prompt_cache_hit_tokens": 1000,
         "prompt_cache_miss_tokens": 2000, "completion_tokens": 500}
    # off-peak: 2000*0.66 + 1000*0.022 + 500*1.98 = 1320 + 22 + 990 = 2332 / 1e6
    assert cost("deepseek-v4-pro", u, peak=False) == pytest.approx(2332 / 1_000_000)
    # peak:     2000*1.32 + 1000*0.044 + 500*3.96 = 2640 + 44 + 1980 = 4664 / 1e6
    assert cost("deepseek-v4-pro", u, peak=True) == pytest.approx(4664 / 1_000_000)


def test_cost_no_cache_fields_all_miss():
    u = {"prompt_tokens": 3000, "completion_tokens": 500}   # полей кэша нет — весь промпт по тарифу miss
    # 3000*0.15 + 500*0.60 = 450 + 300 = 750 / 1e6
    assert cost("deepseek-flash", u, peak=False) == pytest.approx(750 / 1_000_000)


def test_split_tokens_variants():
    assert split_tokens({"prompt_tokens": 1000, "completion_tokens": 100}) == (0, 1000, 100)
    assert split_tokens({"prompt_tokens": 1000, "prompt_cache_hit_tokens": 400,
                         "prompt_cache_miss_tokens": 600, "completion_tokens": 100}) == (400, 600, 100)
    # дано только одно поле — второе добираем из prompt_tokens
    assert split_tokens({"prompt_tokens": 1000, "prompt_cache_hit_tokens": 400}) == (400, 600, 0)
    assert split_tokens({"prompt_tokens": 1000, "prompt_cache_miss_tokens": 700}) == (300, 700, 0)
    assert split_tokens({}) == (0, 0, 0)


def test_price_key_resolution():
    assert price_key("deepseek-flash") == "deepseek-flash"
    assert price_key("deepseek-v4-pro") == "deepseek-v4-pro"
    assert price_key("DeepSeek-V4-Pro") == "deepseek-v4-pro"
    assert price_key("something-pro-x") == "deepseek-v4-pro"
    assert price_key("deepseek-chat") == "deepseek-flash"
    assert price_key("totally-unknown", deep=True) == "deepseek-v4-pro"
    assert price_key("totally-unknown", deep=False) == "deepseek-flash"


def test_prices_table_shape():
    for model, tariff in PRICES.items():
        assert set(tariff) == {"input_miss", "input_hit", "output"}
        for pair in tariff.values():
            assert len(pair) == 2 and pair[1] >= pair[0]   # peak не дешевле off-peak


# ── запись вызова ───────────────────────────────────────────────────────────
async def test_record_creates_table_lazily_on_fresh_db(db):
    # интегратор добавил llm_usage в схему db.open, поэтому в обычной базе таблица уже есть.
    # Чтобы проверить ленивое создание в record(), временно её убираем — record() создаст сам.
    await db.execute("DROP TABLE IF EXISTS llm_usage")
    exists = await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_usage'")
    assert exists == []

    c = await record(db, model="deepseek-flash", deep=False, route="chat",
                     usage={"prompt_tokens": 1000, "completion_tokens": 100},
                     at=dt(hh=5), peak_pricing=False)   # off-peak
    assert c == pytest.approx((1000 * 0.15 + 100 * 0.60) / 1_000_000)

    now_exists = await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_usage'")
    assert len(now_exists) == 1

    rows = await db.fetchall("SELECT * FROM llm_usage")
    assert len(rows) == 1
    row = rows[0]
    assert row["model"] == "deepseek-flash"
    assert row["route"] == "chat"
    assert row["deep"] == 0
    assert row["day"] == "2026-09-28"
    assert row["ts"].startswith("2026-09-28T05:00:00")
    assert row["prompt_tokens"] == 1000
    assert row["completion_tokens"] == 100
    assert row["cached_tokens"] == 0
    assert row["cost_usd"] == pytest.approx(c)


async def test_record_peak_vs_offpeak_by_injected_time(db):
    u = {"prompt_tokens": 2000, "prompt_cache_hit_tokens": 500,
         "prompt_cache_miss_tokens": 1500, "completion_tokens": 200}
    off = await record(db, model="deepseek-flash", route="chat", usage=u, at=dt(hh=5))   # off-peak окно
    peak = await record(db, model="deepseek-flash", route="chat", usage=u, at=dt(hh=6))  # пиковое окно
    assert peak > off
    assert off == pytest.approx(cost("deepseek-flash", u, peak=False))
    assert peak == pytest.approx(cost("deepseek-flash", u, peak=True))


async def test_record_peak_pricing_disabled_never_bills_peak(db):
    u = {"prompt_tokens": 2000, "prompt_cache_miss_tokens": 2000, "completion_tokens": 200}
    # момент внутри пикового окна, но usage_peak_pricing выключен → тариф off-peak
    c = await record(db, model="deepseek-flash", route="chat", usage=u,
                     at=dt(hh=6), peak_pricing=False)
    assert c == pytest.approx(cost("deepseek-flash", u, peak=False))


async def test_record_reads_peak_flag_from_cfg(db, cfg):
    from types import SimpleNamespace
    u = {"prompt_tokens": 1000, "prompt_cache_miss_tokens": 1000, "completion_tokens": 100}
    # поле usage_peak_pricing добавит интегратор; читаем его через getattr, поэтому в тесте — заглушка
    c_off = await record(db, model="deepseek-flash", route="chat", usage=u,
                         at=dt(hh=6), cfg=SimpleNamespace(usage_peak_pricing=False))
    assert c_off == pytest.approx(cost("deepseek-flash", u, peak=False))
    # по умолчанию (поля нет → True) в пиковом окне берём пик; реальный cfg поля ещё не имеет
    c_peak = await record(db, model="deepseek-flash", route="chat", usage=u, at=dt(hh=6), cfg=cfg)
    assert c_peak == pytest.approx(cost("deepseek-flash", u, peak=True))


async def test_record_route_from_contextmanager(db):
    u = {"prompt_tokens": 100, "completion_tokens": 10}
    # без явного route берём текущий из контекста
    with route("digest"):
        await record(db, model="deepseek-flash", usage=u, at=dt(hh=5))
        with route("deep"):
            await record(db, model="deepseek-v4-pro", deep=True, usage=u, at=dt(hh=5))
        # вложенный блок восстановил внешний маршрут
        assert current_route.get() == "digest"
    # вне блоков — маршрут по умолчанию
    await record(db, model="deepseek-flash", usage=u, at=dt(hh=5))
    # явный route перекрывает контекст
    with route("digest"):
        await record(db, model="deepseek-flash", route="brief", usage=u, at=dt(hh=5))

    routes = [r["route"] for r in await db.fetchall("SELECT route FROM llm_usage ORDER BY id")]
    assert routes == ["digest", "deep", "chat", "brief"]


# ── сводка ──────────────────────────────────────────────────────────────────
async def _seed(db):
    """A: flash/chat сегодня; B: pro/deep сегодня; C: flash/chat вчера; OLD: 2026-01-01 (вне окна)."""
    ua = {"prompt_tokens": 1000, "completion_tokens": 100}
    ub = {"prompt_tokens": 2000, "prompt_cache_hit_tokens": 500,
          "prompt_cache_miss_tokens": 1500, "completion_tokens": 200}
    uc = {"prompt_tokens": 500, "completion_tokens": 50}
    uo = {"prompt_tokens": 100, "completion_tokens": 10}
    # все off-peak, чтобы стоимость была детерминирована
    await record(db, model="deepseek-flash", route="chat", usage=ua, at=dt(d=28, hh=5), peak_pricing=False)
    await record(db, model="deepseek-v4-pro", deep=True, route="deep", usage=ub,
                 at=dt(d=28, hh=5), peak_pricing=False)
    await record(db, model="deepseek-flash", route="chat", usage=uc, at=dt(d=27, hh=5), peak_pricing=False)
    await record(db, model="deepseek-flash", route="chat", usage=uo, at=dt(m=1, d=1, hh=5), peak_pricing=False)
    return {
        "a": cost("deepseek-flash", ua, peak=False),
        "b": cost("deepseek-v4-pro", ub, peak=False),
        "c": cost("deepseek-flash", uc, peak=False),
        "o": cost("deepseek-flash", uo, peak=False),
    }


async def test_summary_aggregation(db, clock):
    # clock → «сейчас» = 2026-09-28 06:00 UTC (окно 30 дней покрывает 08-30…09-28)
    costs = await _seed(db)
    s = await summary(db, days=30)

    assert s["total_calls"] == 3                       # OLD за окном
    assert s["total_cost"] == pytest.approx(costs["a"] + costs["b"] + costs["c"])
    assert s["prompt_tokens"] == 1000 + 2000 + 500
    assert s["completion_tokens"] == 100 + 200 + 50
    assert s["since"] == "2026-08-30" and s["days"] == 30

    # по маршрутам — по убыванию расходов: deep (одна дорогая) выше chat
    assert [r["route"] for r in s["by_route"]] == ["deep", "chat"]
    chat = next(r for r in s["by_route"] if r["route"] == "chat")
    assert chat["calls"] == 2 and chat["cost"] == pytest.approx(costs["a"] + costs["c"])

    # по моделям — по убыванию расходов
    assert [r["model"] for r in s["by_model"]] == ["deepseek-v4-pro", "deepseek-flash"]

    # по дням — по возрастанию даты
    assert [r["day"] for r in s["by_day"]] == ["2026-09-27", "2026-09-28"]
    d28 = next(r for r in s["by_day"] if r["day"] == "2026-09-28")
    assert d28["calls"] == 2 and d28["cost"] == pytest.approx(costs["a"] + costs["b"])


async def test_summary_window_includes_old_when_wide(db, clock):
    costs = await _seed(db)
    wide = await summary(db, days=400)          # окно накрывает и 2026-01-01
    assert wide["total_calls"] == 4
    assert wide["total_cost"] == pytest.approx(sum(costs.values()))


async def test_summary_empty_fresh_db(db, clock):
    s = await summary(db, days=30)
    assert s["total_calls"] == 0
    assert s["total_cost"] == 0.0
    assert s["prompt_tokens"] == 0 and s["completion_tokens"] == 0
    assert s["by_route"] == [] and s["by_day"] == [] and s["by_model"] == []
    # таблица создана лениво — второй вызов тоже проходит
    assert (await summary(db))["total_calls"] == 0


# ── баланс DeepSeek ─────────────────────────────────────────────────────────
async def test_balance_ok(cfg):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("Authorization")
        return httpx.Response(200, json={
            "is_available": True,
            "balance_infos": [
                {"currency": "USD", "total_balance": "110.50",
                 "granted_balance": "10.00", "topped_up_balance": "100.50"},
                {"currency": "CNY", "total_balance": "0.00"},
            ],
        })

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        res = await balance(cfg, http=http)
    finally:
        await http.aclose()

    assert res == {"is_available": True,
                   "balances": [{"currency": "USD", "total": 110.5},
                                {"currency": "CNY", "total": 0.0}]}
    assert seen["url"] == "https://api.deepseek.com/user/balance"
    assert seen["auth"] == "Bearer sk-test"          # ключ уходит в заголовке, не в логах


async def test_balance_http_error_returns_none(cfg):
    for status in (401, 402, 500):
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status)))
        try:
            assert await balance(cfg, http=http) is None
        finally:
            await http.aclose()


async def test_balance_network_error_returns_none(cfg):
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет сети", request=req)

    http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    try:
        assert await balance(cfg, http=http) is None
    finally:
        await http.aclose()


async def test_balance_bad_json_returns_none(cfg):
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=b"not json")))
    try:
        assert await balance(cfg, http=http) is None
    finally:
        await http.aclose()


async def test_balance_non_deepseek_no_request(cfg):
    from tests.conftest import with_cfg

    def boom(req: httpx.Request) -> httpx.Response:
        raise AssertionError("не должно быть запроса к не-DeepSeek")

    http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    try:
        assert await balance(with_cfg(cfg, llm_base_url="http://localhost:11434"), http=http) is None
    finally:
        await http.aclose()


async def test_balance_no_keys_no_request(cfg):
    from tests.conftest import with_cfg

    def boom(req: httpx.Request) -> httpx.Response:
        raise AssertionError("не должно быть запроса без ключа")

    http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    try:
        assert await balance(with_cfg(cfg, llm_api_key="", llm_api_keys=()), http=http) is None
    finally:
        await http.aclose()


async def test_balance_respects_custom_deepseek_base_url(cfg):
    from tests.conftest import with_cfg
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"is_available": True, "balance_infos": []})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    c = with_cfg(cfg, llm_base_url="https://proxy.deepseek.com/v1")
    try:
        res = await balance(c, http=http)
    finally:
        await http.aclose()
    assert res == {"is_available": True, "balances": []}
    assert seen["url"] == "https://proxy.deepseek.com/v1/user/balance"


# ── безопасный хук для интегратора ──────────────────────────────────────────
async def test_make_hook_records_and_swallows_errors(db, cfg):
    hook = usage.make_hook(db, cfg)
    await hook({"model": "deepseek-flash", "deep": False, "route": "chat",
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
                "at": dt(hh=5)})
    rows = await db.fetchall("SELECT * FROM llm_usage")
    assert len(rows) == 1 and rows[0]["route"] == "chat"

    # сбой учёта не должен пробрасываться (диалог важнее статистики)
    errors: list[Exception] = []
    bad = usage.make_hook(object(), cfg, on_error=errors.append)   # у object нет .execute
    await bad({"model": "deepseek-flash", "usage": {}})
    assert len(errors) == 1
