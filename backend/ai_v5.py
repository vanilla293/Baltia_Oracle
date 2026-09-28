# -*- coding: utf-8 -*-
"""ПИФИЯ v5 — тонкие помощники над шлюзом DeepSeek (backend/ai.py).

Две модели: FLASH (быстро/дёшево, механика) и PRO (думает, совет).
Каждый вызов — НОВЫЙ чат: system + user, без истории. Ключ — из пула
аккаунтов DeepSeek (панель), иначе любой ключ инструмента.
"""
from __future__ import annotations

import asyncio
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from . import ai, config

MSK = timezone(timedelta(hours=3))
_WD = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def _flash() -> str:
    return config.DEEPSEEK_MODEL_FAST


def _pro() -> str:
    return config.DEEPSEEK_MODEL


FLASH = _flash()
PRO = _pro()


def key() -> str:
    """Ключ DeepSeek: пул аккаунтов → любой ключ инструмента → ошибка."""
    k = config.pick_pool_key()
    if k:
        return k
    for v in (config.INSTRUMENT_KEYS or {}).values():
        if v:
            return v
    raise RuntimeError("нет ключа DeepSeek — введи его в панели (🔑 Ключи)")


def has_key() -> bool:
    try:
        key()
        return True
    except RuntimeError:
        return False


def _skew_s() -> float:
    """Сдвиг локальных часов к серверу биржи (market_clock.sync); без модуля/синка — 0.
    Единая база времени: «Сейчас» в промптах и метки fmt_ts живут в одном времени."""
    try:
        from . import market_clock as _mc
        return float((_mc.skew() or {}).get("skew_s") or 0.0)
    except Exception:            # noqa: BLE001
        return 0.0


def now_msk() -> datetime:
    return datetime.fromtimestamp(time.time() + _skew_s(), MSK)


def now_msk_str(dt: datetime | None = None) -> str:
    d = (dt or now_msk()).astimezone(MSK)
    return f"{d:%d.%m.%Y %H:%M} МСК, {_WD[d.weekday()]}"


def fmt_ts(ts: float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(float(ts) + _skew_s(), MSK).strftime("%d.%m %H:%M")


SEM_FLASH = asyncio.Semaphore(max(1, int(getattr(config, "PYTHIA_FLASH_PAR", 6))))
# 24.09.2026: узлы у денег не стоят в общей очереди с разметкой новостей — иначе таймер wait_for тикал
# в очереди и «решал таймер, а не FLASH» (воспроизведено проверяющим). Своя очередь на 3.
SEM_MONEY = asyncio.Semaphore(3)
MONEY_ROUTES = frozenset({"mission_guard", "mission_take", "event_triage", "mission_entry", "mission_profit",
                          "mission_exec", "mission_review", "summary"})


def _sem_for(route: str) -> asyncio.Semaphore:
    return SEM_MONEY if route in MONEY_ROUTES else SEM_FLASH


_REPAIR_SYS = ("Тебе дан текст, который должен быть одним JSON-объектом, но он сломан "
               "(обрезан, лишние кавычки, текст вокруг). Верни ТОЛЬКО исправленный JSON-объект: "
               "синтаксис починить, содержимое не менять, обрезанные элементы закрыть.")


async def _json_with_repair(system: str, user: str, *, model: str, think: bool,
                            route: str, max_tokens: int | None, attempt_timeout: float | None = None) -> Any:
    """JSON-стадия (24.09.2026).
    • деньги (MONEY_ROUTES): на любую беду — пустой ответ (модель ушла в размышления) или кривой JSON — ровно ОДИН
      повтор той же моделью с тем же размышлением; снова беда → исключение, для вызывающего это «молчание» → его
      безопасный ответ по правилу. Без размышления не переспрашиваем и чужим FLASH не «чиним»: решение о позиции
      принимает думающая модель или никто (воля владельца). Ответ повтора тоже проверяется на маркер —
      черновик из размышления решением не становится (находка проверяющего);
    • механика: повтор без размышления, затем дешёвая починка FLASH (весь текст, без среза) с записью в панель проблем."""
    lg = logging.getLogger("pythia.ai_v5")

    async def _ask(rt: str) -> str:
        # v5.4.2: у каждой попытки свой срок (attempt_timeout) — повтор после пустого/кривого ответа не умирает внутри
        # чужого таймера вызывающего; таймаут попытки — исключение сразу, без повтора (ещё один такой же долгий
        # вызов вызывающий всё равно не дождётся).
        coro = ai.ask(system, user, model=model, json_mode=True, thinking=think,
                      max_tokens=max_tokens, route=rt, api_key=key())
        if attempt_timeout:
            try:
                return await asyncio.wait_for(coro, float(attempt_timeout))
            except asyncio.TimeoutError:
                raise asyncio.TimeoutError(f"[{rt}] модель не ответила за {int(attempt_timeout)} с") from None
        return await coro

    raw = await _ask(route)
    if route in MONEY_ROUTES:
        for attempt in (1, 2):
            if raw.startswith(ai.THINK_MARK):
                why = "модель ушла в размышления без ответа"
            else:
                try:
                    return ai._extract_json(raw)
                except Exception as e:                   # noqa: BLE001
                    why = f"JSON не разобрался ({str(e)[:60]})"
            if attempt == 2:
                ai.note_error(f"{why} и после повтора — решение по правилу", route)
                raise RuntimeError(f"[{route}] {why} и после повтора — решение по правилу")
            lg.warning("[%s] %s — один повтор с размышлением", route, why)
            raw = await _ask(route + "_retry")
    if think and raw.startswith(ai.THINK_MARK):
        lg.warning("[%s] модель ушла в размышления без ответа — повтор без thinking", route)
        raw = await ai.ask(system, user, model=model, json_mode=True, thinking=False,
                           max_tokens=max_tokens, route=route + "_nothink", api_key=key())
    try:
        return ai._extract_json(raw)
    except Exception as e:       # noqa: BLE001
        lg.warning("[%s] JSON не разобрался (%s): %r…", route, str(e)[:60], raw[:160])
    fixed = await ai.ask(_REPAIR_SYS, raw[:400_000], model=_flash(), json_mode=True,
                         thinking=False, max_tokens=max_tokens, route=route + "_repair",
                         api_key=key())
    ai.note_error("JSON чинился вторым вызовом FLASH — содержимое могло измениться", route + "_repair")
    return ai._extract_json(fixed)


# think=True — размышление НЕ глушим; reasoning_effort не шлём (режима «auto» у DeepSeek нет: пусто = умолчание API
# = high, как в первой ПИФИИ 4.5.4); max_tokens шлём явно = максимум модели 393 216 (без него сервер режет 64K
# вместе с размышлением). think=False только там, где ИИ не судит о рынке: починка JSON и health. Воля владельца 24.09.2026.
async def flash_json(system: str, user: str, *, think: bool = True,
                     route: str = "flash", max_tokens: int | None = None,
                     attempt_timeout: float | None = None) -> Any:
    async with _sem_for(route):
        return await _json_with_repair(system, user, model=_flash(), think=think,
                                       route=route, max_tokens=max_tokens, attempt_timeout=attempt_timeout)


async def flash_text(system: str, user: str, *, think: bool = True,
                     route: str = "flash", max_tokens: int | None = None) -> str:
    async with _sem_for(route):
        return await ai.ask(system, user, model=_flash(), thinking=think,
                            max_tokens=max_tokens, route=route, api_key=key())


async def pro_json(system: str, user: str, *, route: str = "pro",
                   max_tokens: int | None = None, attempt_timeout: float | None = None) -> Any:
    return await _json_with_repair(system, user, model=_pro(), think=True,
                                   route=route, max_tokens=max_tokens, attempt_timeout=attempt_timeout)


def money_model() -> str:
    """v5.4.1: модель узлов у денег (трос, тейк, триаж, проверка входа, мысль о прибыли) — PYTHIA_MONEY_MODEL:
    pro (умолчание, воля владельца: «пусть думает долго, но решает тот, кто должен») или flash (как в 5.3)."""
    return "flash" if str(getattr(config, "PYTHIA_MONEY_MODEL", "pro") or "pro").strip().lower() == "flash" else "pro"


async def money_json(system: str, user: str, *, route: str, max_tokens: int | None = None,
                     attempt_timeout: float | None = None) -> Any:
    """Узел у денег (route ∈ MONEY_ROUTES): PRO или FLASH по money_model(), всегда с размышлением, ровно один
    повтор той же моделью, без починки чужой моделью и без повтора без размышления (см. _json_with_repair).
    v5.4.2: attempt_timeout — срок каждой попытки (таймаут → исключение без повтора; повтор после пустого или
    кривого ответа получает свой срок, а не остаток чужого)."""
    extra = {"attempt_timeout": attempt_timeout} if attempt_timeout else {}   # подменам в тестах ключ не навязываем
    if money_model() == "pro":
        return await pro_json(system, user, route=route, max_tokens=max_tokens, **extra)
    return await flash_json(system, user, think=True, route=route, max_tokens=max_tokens, **extra)


# ── v5.4.2: разбор слова решения из JSON-ответа ИИ — один на все узлы у денег ──────────────────────────────
# До 5.4.2 каждый узел искал подстроки («КУП», «ВОЙ», «ЖД»…) и всё непонятое молча превращал в ЖДЁМ / ЖДАТЬ /
# ДЕРЖАТЬ: «BUY», «ВОЙТИ», «CLOSE», «ЗАФИКСИРОВАТЬ» у перепроверки и мысли о прибыли становились ожиданием, а
# «НЕ ВХОДИТЬ» у двери — входом. Здесь: целые слова (Ё=Е, регистр, латиница, знаки и markdown не мешают), ключ
# decision|choice|do|action, отрицание в начале и два разных решения → None. None — «решения нет»: вызывающий
# пишет «ответ не разобран» и переспрашивает, а не выбирает за ИИ.
DECISION_KEYS = ("decision", "choice", "do", "action", "решение", "выбор")
_NEGATIONS = frozenset({"НЕ", "НЕТ", "NOT", "NO", "DONT", "DO_NOT", "НЕЛЬЗЯ"})

SYN_BUY = ("КУПИТЬ", "КУПИТЬ_СЕЙЧАС", "КУПИТЬ_НА_ОТКАТЕ", "КУПИТЬ_НА_ПРОБОЕ", "КУПИТЬ_НА_ПРОРЫВЕ", "ПОКУПКА", "ПОКУПАТЬ",
           "ПОКУПАЕМ", "ПОКУПАЮ", "ЛОНГ", "В_ЛОНГ", "BUY", "BUY_NOW", "LONG", "GO_LONG")
SYN_SELL = ("ПРОДАТЬ", "ПРОДАТЬ_СЕЙЧАС", "ПРОДАТЬ_НА_ОТКАТЕ", "ПРОДАТЬ_НА_ПРОБОЕ", "ПРОДАТЬ_НА_ПРОРЫВЕ", "ПРОДАЖА",
            "ПРОДАВАТЬ", "ПРОДАЕМ", "ПРОДАЮ", "ШОРТ", "В_ШОРТ", "SELL", "SELL_NOW", "SHORT", "GO_SHORT")
SYN_ENTER = ("ВОЙТИ", "ВОЙТИ_СЕЙЧАС", "ВХОД", "ВХОДИМ", "ВХОДИТЬ", "ВХОЖУ", "ЗАЙТИ", "ENTER", "ENTRY", "GO", "YES", "ДА")
SYN_WAIT = ("ЖДАТЬ", "ЖДЕМ", "ЖДУ", "ПОДОЖДАТЬ", "ОЖИДАТЬ", "ВНЕ_РЫНКА", "WAIT", "STAY_OUT", "NO_TRADE", "NONE", "PASS")
SYN_HOLD = ("ДЕРЖАТЬ", "ДЕРЖИМ", "ДЕРЖУ", "ПОДЕРЖАТЬ", "ОСТАВИТЬ", "HOLD", "KEEP")
SYN_CLOSE = ("ЗАКРЫТЬ", "ЗАКРЫВАЕМ", "ЗАКРЫВАТЬ", "ВЫЙТИ", "ВЫХОД", "ВЫХОДИМ", "СЛИТЬ", "СЛИВАЕМ", "ЗАФИКСИРОВАТЬ",
             "ФИКСИРОВАТЬ", "ФИКСИРУЕМ", "ЗАБРАТЬ", "ЗАБРАТЬ_ПРИБЫЛЬ", "ФИКСИРОВАТЬ_ПРИБЫЛЬ", "ЗАФИКСИРОВАТЬ_ПРИБЫЛЬ",
             "CLOSE", "EXIT", "FLAT", "TAKE_PROFIT", "CLOSE_ALL")
SYN_CANCEL = ("ОТМЕНИТЬ", "ОТМЕНА", "ОТМЕНЯЕМ", "ОТКАЗ", "ОТКАЗАТЬСЯ", "CANCEL", "SKIP", "ABORT")
SYN_ADD = ("ДОБРАТЬ", "ДОБОР", "ДОКУПИТЬ", "УСИЛИТЬ", "НАРАСТИТЬ", "ADD", "TOPUP", "TOP_UP", "SCALE_IN")
SYN_FLIP = ("ПЕРЕВЕРНУТЬ", "ПЕРЕВОРОТ", "РАЗВЕРНУТЬ", "РАЗВОРОТ_ПОЗИЦИИ", "FLIP", "REVERSE")
SYN_COUNCIL = ("НОВЫЙ_АНАЛИЗ", "СОВЕТ", "НОВЫЙ_СОВЕТ", "ПЕРЕАНАЛИЗ", "COUNCIL", "REANALYZE", "NEW_ANALYSIS")
SYN_REENTER = ("ВЫЙТИ_И_ПЕРЕЗАЙТИ", "ВЫЙТИ_ПЕРЕЗАЙТИ", "ПЕРЕЗАЙТИ", "ПЕРЕЗАХОД", "REENTER", "REENTRY", "RE_ENTER",
               "RE_ENTRY", "EXIT_AND_REENTER")


def _norm_word(x: Any) -> str:
    """«Ждём!» → «ЖДЕМ», «buy-now» → «BUY_NOW», «**ВОЙТИ**» → «ВОЙТИ»: верхний регистр, Ё=Е, всё, что не буква и не
    цифра, — разделитель слов (подчёркивание тоже)."""
    import re
    s = str(x or "").upper().replace("Ё", "Е")
    return " ".join(re.sub(r"[^0-9A-ZА-Я]+", " ", s).split())


def decision_raw(obj: Any, keys: tuple[str, ...] = DECISION_KEYS) -> str:
    """Сырое слово решения из ответа: строка как есть или первый непустой ключ из keys (без учёта регистра)."""
    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, dict):
        low = {str(k).strip().lower(): v for k, v in obj.items()}
        for k in keys:
            v = low.get(k)
            if v not in (None, "") and not isinstance(v, (dict, list)):
                return str(v).strip()
    return ""


def decision_of(raw: Any, table: dict[str, tuple[str, ...]]) -> str | None:
    """Каноническое решение узла или None. table: {токен узла: синонимы} (токен сам себе синоним).
    Сначала весь ответ целиком, потом первые 3 / 2 / 1 слова; попадание в два разных решения → None;
    «НЕ …» в начале и «… или …» → None (не угадываем ни в сторону входа, ни в сторону ожидания)."""
    words = _norm_word(raw).split()
    if not words or words[0] in _NEGATIONS or "ИЛИ" in words or "OR" in words:
        return None                        # «НЕ ВХОДИТЬ», «ВОЙТИ или ЖДАТЬ» — решения нет, переспросить
    idx: dict[str, set[str]] = {}
    for tok, syns in table.items():
        for w in (tok, *syns):
            idx.setdefault("_".join(_norm_word(w).split()), set()).add(tok)
    for n in (len(words), 3, 2, 1):
        if n > len(words):
            continue
        hit = idx.get("_".join(words[:n]))
        if hit:
            return next(iter(hit)) if len(hit) == 1 else None
    return None


async def pro_text(system: str, user: str, *, route: str = "pro",
                   max_tokens: int | None = None) -> str:
    return await ai.ask(system, user, model=_pro(), thinking=True,
                        max_tokens=max_tokens, route=route, api_key=key())


async def pro_stream(system: str, user: str, *,
                     on_think: Callable[[str], Awaitable[None]] | None = None,
                     on_text: Callable[[str], Awaitable[None]] | None = None,
                     route: str = "pro") -> str:
    async def _t(delta: str, _full):
        if on_think:
            await on_think(delta)

    async def _x(delta: str, _full):
        if on_text:
            await on_text(delta)

    return await ai.stream(system, user, on_think=_t if on_think else None,
                           on_text=_x if on_text else None, model=_pro(),
                           thinking=True, route=route, api_key=key())


def last_error() -> dict | None:
    """v5.3 фаза 3 (W1), панель проблем: последняя ошибка DeepSeek после всех повторов —
    {"ts","route","code","text"} | None; поверх — "stale": True, если позже был успешный ответ
    (ошибка старая, ИИ снова отвечает)."""
    try:
        err = ai.last_error()
    except Exception:            # noqa: BLE001
        return None
    if not err:
        return None
    try:
        err["stale"] = bool(ai.last_ok_ts() and ai.last_ok_ts() > float(err.get("ts") or 0))
    except Exception:            # noqa: BLE001
        err["stale"] = False
    return err


def note_error(text: str, route: str = "") -> None:
    """Ошибка ИИ вне ai.ask (таймаут ожидания у вызывающего, ответ не JSON) — в ту же память."""
    try:
        ai.note_error(str(text), route)
    except Exception:            # noqa: BLE001
        pass


def usage() -> dict:
    """Расход DeepSeek с запуска: итог (prompt/completion/total/calls) + по маршрутам и по моделям
    (v5.3 W2, панель фазы 3): {"by_route": {route: {calls, prompt, completion, model}},
    "by_model": {"FLASH"|"PRO"|имя: {calls, prompt, completion}}}."""
    out = dict(ai.tokens())
    try:
        by_route = ai.tokens_by_route()
    except Exception:            # noqa: BLE001
        by_route = {}
    by_model: dict[str, dict] = {}
    for rec in by_route.values():
        mdl = rec.get("model") or ""
        name = "FLASH" if mdl == _flash() else "PRO" if mdl == _pro() else (mdl or "?")
        b = by_model.setdefault(name, {"calls": 0, "prompt": 0, "completion": 0})
        b["calls"] += int(rec.get("calls") or 0)
        b["prompt"] += int(rec.get("prompt") or 0)
        b["completion"] += int(rec.get("completion") or 0)
    out["by_route"] = by_route
    out["by_model"] = by_model
    return out


if __name__ == "__main__":
    async def _t():
        calls = []

        async def fake_ask(system, user, **kw):
            calls.append(kw.get("route"))
            if kw.get("route") == "x":
                return '{"a": [1, 2'          # обрезано
            if kw.get("route") == "y":
                return "[⚠ модель ушла в размышления — ниже финальная часть]\nбла"
            return '{"a": [1, 2]}'

        real = ai.ask
        ai.ask = fake_ask
        try:
            key.__globals__["config"].set_deepseek_keys(["sk-test"])
            out = await flash_json("s", "u", route="x")
            assert out == {"a": [1, 2]} and calls == ["x", "x_repair"], (out, calls)
            calls.clear()
            out = await flash_json("s", "u", think=True, route="y")
            assert out == {"a": [1, 2]} and calls == ["y", "y_nothink"], (out, calls)
        finally:
            ai.ask = real
            key.__globals__["config"].wipe_keys() if False else None

    import asyncio as _a
    import tempfile as _tf
    from pathlib import Path as _P
    _cfg = key.__globals__["config"]
    _cfg_keys = _cfg.deepseek_keys()
    _real_cfg_path = _cfg.USER_CFG_PATH
    _cfg.USER_CFG_PATH = _P(_tf.mkdtemp(prefix="pythia_ai_v5_")) / "config_user.json"   # self-тест не трогает data/
    try:
        _a.run(_t())
        _cfg.set_deepseek_keys(_cfg_keys)   # вернуть как было (в памяти; файл — временный)
        assert _cfg.USER_CFG_PATH.exists() and not str(_cfg.USER_CFG_PATH).startswith(str(_cfg.DATA_DIR)), _cfg.USER_CFG_PATH
    finally:
        _cfg.USER_CFG_PATH = _real_cfg_path
    assert _cfg.deepseek_keys() == _cfg_keys, "ключи владельца в памяти — как были"
    # память ошибок (v5.3 фаза 3): note_error → last_error со stale по последнему успеху
    ai._last_err, ai._last_ok_ts = None, 0.0
    assert last_error() is None
    note_error("PRO промолчал (таймаут 300 с)", "mission_review")
    _le = last_error()
    assert _le and _le["route"] == "mission_review" and "таймаут" in _le["text"] and _le["stale"] is False, _le
    ai._add_usage(None, "x", _flash())            # ответ пришёл позже ошибки → ошибка старая
    assert last_error()["stale"] is True
    ai._last_err, ai._last_ok_ts = None, 0.0
    s = now_msk_str()
    assert "МСК" in s and any(w in s for w in _WD), s
    assert fmt_ts(None) == "—"
    # учёт по маршрутам и моделям (v5.3 W2): ai._add_usage(route, model) → usage()["by_route"/"by_model"]
    class _U:
        prompt_tokens, completion_tokens, total_tokens = 100, 20, 120
    _saved_tok, _saved_routes = dict(ai._tok), ai.tokens_by_route()
    ai.reset_tokens()
    ai._add_usage(_U(), "mission_take", _flash())
    ai._add_usage(_U(), "mission_take", _flash())
    ai._add_usage(_U(), "mission_review", _pro())
    u = usage()
    assert u["calls"] == 3 and u["prompt"] == 300 and u["by_route"]["mission_take"]["calls"] == 2, u
    assert u["by_route"]["mission_review"]["model"] == _pro() and u["by_model"]["FLASH"]["prompt"] == 200
    assert u["by_model"]["PRO"] == {"calls": 1, "prompt": 100, "completion": 20}, u["by_model"]
    ai.reset_tokens()
    ai._tok.update(_saved_tok)
    for _r, _v in _saved_routes.items():
        ai._tok_routes[_r] = dict(_v)
    # v5.4.2: срок попытки — таймаут сразу исключением, без повтора; пустой ответ → повтор со своим сроком
    async def _t_att():
        seen = []

        async def slow_ask(system, user, **kw):
            seen.append(kw.get("route"))
            if kw.get("route") == "mission_entry":
                await asyncio.sleep(0.2)
                return "[⚠ модель ушла в размышления — ниже финальная часть]\nчерновик"
            return '{"decision": "ВОЙТИ"}'

        real_ask = ai.ask
        ai.ask = slow_ask
        try:
            got = await _json_with_repair("s", "u", model="m", think=True, route="mission_entry", max_tokens=None,
                                          attempt_timeout=0.5)
            assert got == {"decision": "ВОЙТИ"} and seen == ["mission_entry", "mission_entry_retry"], seen
            seen.clear()
            try:
                await _json_with_repair("s", "u", model="m", think=True, route="mission_entry", max_tokens=None,
                                        attempt_timeout=0.05)
                raise AssertionError("таймаут попытки должен быть исключением")
            except asyncio.TimeoutError as e:
                assert "не ответила за" in str(e) and seen == ["mission_entry"], (e, seen)
        finally:
            ai.ask = real_ask
    asyncio.run(_t_att())
    # v5.4.2: разбор слова решения — целые слова, синонимы, отрицание и «или» → None (решения нет, переспросить)
    _door = {"ВОЙТИ": SYN_ENTER + SYN_BUY, "ЖДАТЬ": SYN_WAIT, "ОТМЕНИТЬ": SYN_CANCEL}
    for _raw, _want in (("ВОЙТИ", "ВОЙТИ"), ("**войти**", "ВОЙТИ"), ("BUY", "ВОЙТИ"), ("ENTRY", "ВОЙТИ"),
                        ("Ждём!", "ЖДАТЬ"), ("ЖДАТЬ, хотя можно и войти", "ЖДАТЬ"), ("ЖДАТЬ ВХОДА НА ОТКАТЕ", "ЖДАТЬ"),
                        ("ОТКАЗ", "ОТМЕНИТЬ"), ("НЕ ВХОДИТЬ", None), ("ВОЙТИ или ЖДАТЬ", None), ("", None), ("?!", None)):
        assert decision_of(_raw, _door) == _want, (_raw, decision_of(_raw, _door), _want)
    _rev = {"КУПИТЬ_СЕЙЧАС": SYN_BUY, "ПРОДАТЬ_СЕЙЧАС": SYN_SELL, "ЖДЁМ": SYN_WAIT + SYN_HOLD, "НОВЫЙ_АНАЛИЗ": SYN_COUNCIL}
    assert decision_of("КУПИТЬ_СЕЙЧАС — анализ подтверждён", _rev) == "КУПИТЬ_СЕЙЧАС", "не совет вместо входа"
    assert decision_of("ЖДЁМ (не покупать до 101)", _rev) == "ЖДЁМ" and decision_of("не покупать", _rev) is None
    assert decision_of("ЖДЕМ", _rev) == "ЖДЁМ" and decision_of("новый анализ", _rev) == "НОВЫЙ_АНАЛИЗ"
    assert decision_raw({"Decision": "buy"}) == "buy" and decision_raw({"choice": "", "do": "SELL"}) == "SELL"
    assert decision_raw({}) == "" and decision_raw("ВОЙТИ ") == "ВОЙТИ" and decision_raw({"choice": {"x": 1}}) == ""
    print("ai_v5 self-test OK:", s)
