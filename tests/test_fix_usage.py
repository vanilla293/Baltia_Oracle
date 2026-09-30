"""Регрессия A30: usage.balance() при нескольких ключах.

Раньше balance() смотрел только api_keys[0]. Если первый ключ «умер» (401/402) и клиент ушёл на
запасной (llm.py), дашборд показывал баланс мёртвого ключа — владельцу казалось, что бот сломан.
Теперь balance() опрашивает каждый ключ, помечает его и на верхнем уровне показывает живой/рабочий.

Сеть — только httpx.MockTransport. Ключи в лог не попадают.
"""
from __future__ import annotations

import logging

import httpx

from oracle.usage import balance
from tests.conftest import with_cfg


def _dispatch(table: dict[str, object]):
    """MockTransport-обработчик: по ключу из заголовка Authorization отдаёт свой ответ.

    Значение в table — либо httpx.Response, либо исключение (будет поднято), либо callable(req)→Response.
    Порядковые запросы фиксируются в возвращённом списке `seen` (ключ каждого запроса)."""
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        auth = req.headers.get("Authorization", "")
        key = auth.removeprefix("Bearer ")
        seen.append(key)
        item = table[key]
        if isinstance(item, httpx.RequestError):        # прикрепляем реальный request (как в httpx)
            raise item.__class__(str(item), request=req)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(req)
        return item

    return handler, seen


def _resp(available: bool, total: float, currency: str = "USD") -> httpx.Response:
    return httpx.Response(200, json={
        "is_available": available,
        "balance_infos": [{"currency": currency, "total_balance": f"{total}"}],
    })


# ── один ключ: прежний контракт не изменился (обратная совместимость) ──────────
async def test_balance_single_key_unchanged_shape(cfg):
    handler, seen = _dispatch({"sk-test": _resp(True, 12.5)})
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        res = await balance(cfg, http=http)          # cfg — один ключ sk-test
    finally:
        await http.aclose()
    # ровно прежняя форма: ни keys, ни live_index не добавляем
    assert res == {"is_available": True, "balances": [{"currency": "USD", "total": 12.5}]}
    assert seen == ["sk-test"]                        # ровно один запрос


# ── два ключа: мёртвый первый, живой запасной — верхний уровень берёт рабочий ──
async def test_balance_two_keys_dead_first_surfaces_working_key(cfg):
    c = with_cfg(cfg, llm_api_keys=("dead", "good"))
    handler, seen = _dispatch({"dead": httpx.Response(402), "good": _resp(True, 42.0)})
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        res = await balance(c, http=http)
    finally:
        await http.aclose()

    assert seen == ["dead", "good"]                   # опрошены оба ключа, каждый своим ключом
    # верхний уровень (его читает дашборд) — уже НЕ мёртвый ключ, а рабочий запасной
    assert res["is_available"] is True
    assert res["balances"] == [{"currency": "USD", "total": 42.0}]
    # разбивка по ключам: у мёртвого — None, у живого — баланс; оба помечены индексом
    assert res["keys"] == [
        {"index": 1, "live": False, "is_available": None, "balances": None},
        {"index": 2, "live": False, "is_available": True,
         "balances": [{"currency": "USD", "total": 42.0}]},
    ]
    assert res["live_index"] is None                  # живой ключ не сообщён извне


# ── два ключа + известный живой индекс (LLM._key_idx): помечаем и берём именно его ─
async def test_balance_two_keys_live_index_marks_and_prefers(cfg):
    c = with_cfg(cfg, llm_api_keys=("first", "second"))
    # оба доступны, но с разными остатками — проверяем, что берём именно живой (второй)
    handler, _ = _dispatch({"first": _resp(True, 5.0), "second": _resp(True, 99.0)})
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        res = await balance(c, http=http, live_index=1)   # живой — второй ключ
    finally:
        await http.aclose()

    assert res["live_index"] == 2
    assert [k["live"] for k in res["keys"]] == [False, True]
    # верхний уровень — остаток именно живого (второго) ключа, а не первого
    assert res["balances"] == [{"currency": "USD", "total": 99.0}]


async def test_balance_live_index_dead_falls_back_but_keeps_label(cfg):
    # живой по индексу ключ недоступен — верхний уровень падает на первый рабочий,
    # но пометка live остаётся на реально живом (диагностика: «живой №1 не отвечает»)
    c = with_cfg(cfg, llm_api_keys=("dead", "good"))
    handler, _ = _dispatch({"dead": httpx.ConnectError("нет сети"), "good": _resp(True, 7.0)})
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        res = await balance(c, http=http, live_index=0)
    finally:
        await http.aclose()
    assert res["live_index"] == 1
    assert res["keys"][0] == {"index": 1, "live": True, "is_available": None, "balances": None}
    assert res["balances"] == [{"currency": "USD", "total": 7.0}]   # показан рабочий запасной


# ── деградация по каждому ключу независимо ────────────────────────────────────
async def test_balance_two_keys_per_key_error_degrades_to_none(cfg):
    c = with_cfg(cfg, llm_api_keys=("k1", "k2"))
    # k1 — битый JSON, k2 — сеть упала: оба → None, но это не роняет вызов целиком
    handler, seen = _dispatch({
        "k1": httpx.Response(200, content=b"not json"),
        "k2": httpx.ConnectError("boom"),
    })
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        res = await balance(c, http=http)
    finally:
        await http.aclose()
    assert seen == ["k1", "k2"]
    assert res["keys"] == [
        {"index": 1, "live": False, "is_available": None, "balances": None},
        {"index": 2, "live": False, "is_available": None, "balances": None},
    ]
    # ни один ключ не ответил — верхний уровень «пусто», но структура сохранена
    assert res["is_available"] is False and res["balances"] == []


async def test_balance_two_keys_first_available_wins_when_no_live_index(cfg):
    # оба ключа рабочие, живой неизвестен → верхний уровень берёт ПЕРВЫЙ доступный
    c = with_cfg(cfg, llm_api_keys=("a", "b"))
    handler, _ = _dispatch({"a": _resp(True, 3.0), "b": _resp(True, 8.0)})
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        res = await balance(c, http=http)
    finally:
        await http.aclose()
    assert res["balances"] == [{"currency": "USD", "total": 3.0}]


# ── ключи не попадают в лог ни при успехе, ни при ошибке ──────────────────────
async def test_balance_never_logs_keys(cfg, caplog):
    c = with_cfg(cfg, llm_api_keys=("secretkey1", "secretkey2"))
    handler, _ = _dispatch({
        "secretkey1": httpx.Response(401),
        "secretkey2": _resp(True, 1.0),
    })
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.DEBUG, logger="oracle.usage"):
        try:
            await balance(c, http=http)
        finally:
            await http.aclose()
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "secretkey1" not in blob and "secretkey2" not in blob
    # но диагностика по номеру ключа при этом есть
    assert "ключ №1" in blob


# ── не-DeepSeek и отсутствие ключей: как и раньше, без запросов ────────────────
async def test_balance_multikey_non_deepseek_no_request(cfg):
    c = with_cfg(cfg, llm_base_url="http://localhost:11434", llm_api_keys=("a", "b"))

    def boom(req: httpx.Request) -> httpx.Response:
        raise AssertionError("не должно быть запроса к не-DeepSeek")

    http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    try:
        assert await balance(c, http=http) is None
    finally:
        await http.aclose()
