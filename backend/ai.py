"""ОРАКУЛ // ПИФИЯ — шлюз к DeepSeek (умный режим).

V4 думает по умолчанию: enable не шлём, thinking.disabled — только где мышление не нужно;
reasoning_effort — только если задан явно (пусто = умолчание сервера high, режима auto нет);
max_tokens — явный максимум модели 393 216 (24.09.2026). На думающих стадиях формат НЕ навязываем
(json_mode=False), температуру в thinking-режиме API игнорирует. Финальная шифровка → json_mode.

Методы:
  ask(system, user, ...)              — один проход, вернуть текст
  ask_json(system, user, ...)         — один проход, распарсить JSON
  stream(system, user, on_think, on_text, ...) — стрим content + reasoning
  tokens()                            — счётчик токенов сессии
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable

import httpx
from openai import AsyncOpenAI

try:  # человеческие тексты ошибок API
    from openai import (APIStatusError, APIConnectionError,  # type: ignore
                        APITimeoutError)
except Exception:  # на случай другой версии openai
    APIStatusError = APIConnectionError = APITimeoutError = ()  # type: ignore


def _is_encoding_err(e: Exception, raw: str) -> bool:
    return isinstance(e, UnicodeEncodeError) or \
        "codec can't encode" in raw or "Illegal header value" in raw


def humanize_error(e: Exception) -> str:
    """Ошибка DeepSeek → понятный русский текст для оператора."""
    code = getattr(e, "status_code", None)
    raw = str(e)
    if _is_encoding_err(e, raw):
        return ("ключ содержит недопустимые символы (кириллица/юникод) — "
                "похоже, вставлен не тот текст. Введи ключ заново (🔑 в шапке)")
    if code is None:
        for c in (401, 402, 403, 429):
            if f" {c} " in f" {raw} " or f"Error code: {c}" in raw:
                code = c
                break
    if code == 401:
        return "ключ DeepSeek не принят (401): неверный или отозван — введи заново (🔑 в шапке)"
    if code in (402, 403):
        return "на ключе DeepSeek закончились средства или нет доступа (402) — пополни баланс на platform.deepseek.com"
    if code == 429:
        return "лимит запросов DeepSeek (429) — подожди минуту и повтори"
    if isinstance(e, APITimeoutError) or "timed out" in raw.lower() or "timeout" in raw.lower():
        return "DeepSeek не ответил (таймаут) — проверь сеть/доступ к api.deepseek.com"
    if isinstance(e, APIConnectionError) or "connect" in raw.lower():
        return "нет соединения с DeepSeek — проверь интернет/доступ к api.deepseek.com"
    return raw[:200]


def _retry_delay(e: Exception, attempt: int) -> float | None:
    """Сколько ждать перед повтором. None — не ретраить (реальный фатал).
    429/таймаут/сеть/5xx/пустой ответ — временные: DeepSeek под нагрузкой
    в конце длинного пакета делает ровно это."""
    raw = str(e)
    code = getattr(e, "status_code", None)
    if code is None:
        for c in (401, 429, 500, 502, 503, 504):
            if f"Error code: {c}" in raw or f" {c} " in f" {raw} ":
                code = c
                break
    if code == 429:
        try:
            ra = float(getattr(getattr(e, "response", None), "headers", {}).get("retry-after", 0))
        except Exception:
            ra = 0
        return max(6.0 * attempt, min(ra, 30.0)) if ra else 6.0 * attempt
    if code == 401:
        return 3.0 if attempt == 1 else None   # один повтор: единичный глюк ≠ мёртвый ключ
    if code is not None and code >= 500:
        return 5.0
    if isinstance(e, (APITimeoutError, APIConnectionError)) \
            or "timed out" in raw.lower() or "timeout" in raw.lower() or "connect" in raw.lower():
        # 24.09.2026: таймаут — один повтор (сетевой сбой), не три: длинную генерацию (32K+ токенов
        # ответа, десятки тысяч токенов размышления) по три раза не гоняем и не оплачиваем трижды
        return 4.0 if attempt == 1 else None
    if isinstance(e, ValueError) and "пустой ответ" in raw:
        return 2.0 if attempt == 1 else None
    return None


_TRIES = 3
# стрим: 600 с без единого чанка = завис (известный сбой DeepSeek посреди reasoning_content, #1608) → обрыв и повтор
STREAM_TIMEOUT = httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=60.0)


_TAIL_MARKERS = re.compile(
    r"(?:ВЕРДИКТ|ИТОГОВЫЙ|ИТОГ\b|ЗАКЛЮЧЕНИ|ВЫВОД|Итого|Final answer|Финал)",
    re.IGNORECASE)


THINK_MARK = "[⚠ модель ушла в размышления — ниже финальная часть]"


def _tail_of_thinking(think: str, limit: int = 40_000) -> str:
    """Модель ушла целиком в reasoning (content пуст): отдать финальную часть с выводами,
    ВСЕГДА с маркером THINK_MARK — по нему ai_v5 отличает «ответа нет» от ответа (раньше маркер
    ставился только при > 6000 симв., и короткое размышление уходило как ответ без пометки —
    черновик решения из размышления мог стать решением; найдено проверяющим 24.09.2026)."""
    t = (think or "").strip()
    if not t:
        return ""
    if len(t) <= limit:
        return THINK_MARK + "\n" + t
    last = None
    for m2 in _TAIL_MARKERS.finditer(t):
        last = m2
    if last and len(t) - last.start() >= 300:
        cut = t[last.start():]
        if len(cut) > limit:
            cut = cut[:limit] + "\n[…обрезано]"
        return THINK_MARK + "\n" + cut
    return THINK_MARK + "\n…" + t[-limit:]


def is_key_error(e: Exception) -> bool:
    """Ошибка уровня ключа (нет смысла продолжать стадии на этом ключе)."""
    code = getattr(e, "status_code", None)
    raw = str(e)
    if _is_encoding_err(e, raw):
        return True
    return code in (401, 402, 403) or any(
        f"Error code: {c}" in raw or f" {c} " in f" {raw} " for c in (401, 402, 403))

from . import config

logger = logging.getLogger("pythia.ai")

_clients: dict[str, AsyncOpenAI] = {}
_lock = asyncio.Lock()
_sem = asyncio.Semaphore(16)


def _ck(api_key: str | None = None) -> str:
    """Ключ кэша клиента: тот же, что строит _get_client (ключ@base_url)."""
    return f"{api_key or config.DEEPSEEK_API_KEY}@{config.DEEPSEEK_BASE_URL}"

_tok = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}
_tok_routes: dict[str, dict] = {}     # v5.3 W2: учёт по маршрутам {route: {calls, prompt, completion, model}}
# v5.3 фаза 3 (W1): память последней ошибки DeepSeek для панели проблем (health) — после всех повторов
# вызов упал (401/402/429/5xx/таймаут/сеть/пустой ответ); последний успешный ответ — чтобы понять,
# актуальна ли ошибка (успех позже ошибки → ИИ снова отвечает)
_last_err: dict | None = None
_last_ok_ts: float = 0.0


def note_error(e: "Exception | str", route: str = "") -> dict:
    """Запомнить ошибку ИИ человеческим текстом (humanize_error); текстом — для ошибок вне ask
    (таймаут ожидания у вызывающего, ответ не JSON). Возвращает запись."""
    global _last_err
    if isinstance(e, Exception):
        code = getattr(e, "status_code", None)
        text = humanize_error(e)
    else:
        code, text = None, str(e)
    _last_err = {"ts": time.time(), "route": route or "", "code": code, "text": (text or "")[:200]}
    return dict(_last_err)


def last_error() -> dict | None:
    """Последняя ошибка DeepSeek: {"ts","route","code","text"} | None (копия)."""
    return dict(_last_err) if _last_err else None


def last_ok_ts() -> float:
    """Когда DeepSeek последний раз ответил (0 — ещё ни разу с запуска)."""
    return _last_ok_ts


def reset_tokens() -> None:
    for k in _tok:
        _tok[k] = 0
    _tok_routes.clear()


def tokens() -> dict:
    return dict(_tok)


def tokens_by_route() -> dict:
    """Расход по маршрутам вызова: {route: {calls, prompt, completion, model}} (копия)."""
    return {r: dict(v) for r, v in _tok_routes.items()}


def _add_usage(usage, route: str = "", model: str = "") -> None:
    global _last_ok_ts
    _last_ok_ts = time.time()          # ответ пришёл — ИИ жив (usage может и не быть)
    if not usage:
        return
    pt = getattr(usage, "prompt_tokens", 0) or 0
    ct = getattr(usage, "completion_tokens", 0) or 0
    _tok["prompt"] += pt
    _tok["completion"] += ct
    _tok["total"] += getattr(usage, "total_tokens", 0) or 0
    _tok["calls"] += 1
    if route:
        r = _tok_routes.setdefault(route, {"calls": 0, "prompt": 0, "completion": 0, "model": model or ""})
        r["calls"] += 1
        r["prompt"] += pt
        r["completion"] += ct
        if model:
            r["model"] = model


async def _get_client(api_key: str | None = None) -> AsyncOpenAI:
    key = api_key or config.DEEPSEEK_API_KEY
    url = config.DEEPSEEK_BASE_URL
    if not key:
        raise RuntimeError("ключ DeepSeek не задан для этого объекта — "
                           "введи его при запуске разбора или кликом по 🔑 в шапке")
    ck = f"{key}@{url}"
    async with _lock:
        cl = _clients.get(ck)
        if cl is None:
            # max_retries=0: повторы живут ТОЛЬКО в нашем цикле (_TRIES). Раньше
            # SDK делал до 2 своих повторов ПОВЕРХ наших 3 — на 429/5xx это
            # множилось (до ~6 попыток с бэкоффом), и запрос надолго зависал.
            # 24.09.2026 «без границ»: read 3600 с — не-стримовый ответ на 32K+ токенов с размышлением
            # (DeepSeek держит соединение пустыми строками keep-alive, но 600 с — впритык);
            # стримы получают свой сторож простоя между чанками — STREAM_TIMEOUT в create()
            cl = AsyncOpenAI(api_key=key, base_url=url,
                             timeout=httpx.Timeout(connect=15.0, read=3600.0, write=60.0, pool=60.0),
                             max_retries=0)
            _clients[ck] = cl
        return cl


def _close_clients(clients: list) -> None:
    if not clients:
        return

    async def _close(cs):
        for c in cs:
            try:
                await c.close()
            except Exception:
                pass
    try:
        asyncio.get_running_loop().create_task(_close(clients))
    except RuntimeError:
        pass   # нет активного loop — соединения соберёт GC


async def aclose() -> None:
    """Close the remaining SDK clients after all callers have stopped."""
    async with _lock:
        clients = list(_clients.values())
        _clients.clear()
    await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)


def reset_client(key: str | None = None) -> None:
    """Сбросить кэш клиентов DeepSeek и аккуратно закрыть их httpx-пулы в фоне.

    key=None — сбросить ВСЕ (смена конфига/base_url, стирание ключей).
    key задан — выбросить ТОЛЬКО этот клиент. Важно на 401: DeepSeek под
    нагрузкой отдаёт единичные 401 («глюк, не мёртвый ключ»); раньше это чистило
    кэш ВСЕХ инструментов пакета, и здоровые прогретые клиенты соседей
    пересоздавались заново (лишние TLS-хендшейки прямо посреди разбора)."""
    if key is not None:
        cl = _clients.pop(key, None)
        _close_clients([cl] if cl is not None else [])
        return
    old = list(_clients.values())
    _clients.clear()
    _close_clients(old)


def _try_failover(e: Exception, api_key: str | None) -> str:
    """Если текущий аккаунт упёрся в лимит (429) / баланс (402) / ключ (401/403)
    И он из ПУЛА аккаунтов — вернуть другой здоровый аккаунт для повтора.
    Персональные ключи инструментов не подменяем. Возвращает '' — переключения нет."""
    code = getattr(e, "status_code", None)
    raw = str(e)
    is_acct = code in (401, 402, 403, 429) or any(
        f"Error code: {c}" in raw or f" {c} " in f" {raw} " for c in (401, 402, 403, 429))
    if not is_acct or not api_key:
        return ""
    return config.failover_key(api_key)


def _is_v4(model: str) -> bool:
    """Семейство V4+ (думает по умолчанию, reasoning_effort, thinking.disabled): deepseek-v4-*, deepseek-v4.1-*,
    официальное короткое имя deepseek-flash (V4.1-Flash с 10.09.2026) и любое будущее deepseek-*,
    кроме legacy deepseek-chat / -reasoner / v3 / r1 (выведены 24.07.2026)."""
    m = (model or "").lower()
    if not m.startswith("deepseek"):
        return False
    return not (m.endswith("-reasoner") or m == "deepseek-chat" or m.startswith(("deepseek-chat-", "deepseek-v3", "deepseek-r1")))


def _is_reasoner(model: str) -> bool:
    m = (model or "").lower()
    return m.endswith("-reasoner")


def _eff(effort: str) -> str | None:
    """Уровень размышления. Пусто/auto/default → None: reasoning_effort НЕ шлём — работает умолчание
    сервера DeepSeek (high); режима «auto» у API нет. Явные значения оператора уходят как есть:
    none (выключить размышление), low, high, max; medium/xhigh/minimal/ultra API сам сводит к low/high/max."""
    e = (effort or "").lower().strip()
    if e in ("", "auto", "default"):
        return None
    return e if e in ("none", "low", "medium", "high", "max", "minimal", "xhigh", "ultra") else None


def _build_kwargs(model: str, messages: list[dict], *, json_mode: bool,
                  thinking: bool, effort: str, temperature: float,
                  max_tokens: int) -> dict:
    kw: dict[str, Any] = {"model": model, "messages": messages}
    # max_tokens=0 → не шлём потолок вовсе: DeepSeek сам решает, сколько писать
    # (нормальные разборы и так завершаются по finish=stop задолго до лимитов)
    if max_tokens and int(max_tokens) > 0:
        kw["max_tokens"] = int(max_tokens)
    think_on = thinking and (_is_v4(model) or _is_reasoner(model))
    if _is_v4(model):
        # V4 думает ПО УМОЛЧАНИЮ — enable не шлём; reasoning_effort — только если оператор задал явно
        # (пусто = умолчание сервера high); глушим только там, где мышление не нужно (починка JSON, health)
        if think_on:
            eff = _eff(effort)
            if eff:                       # пусто → не шлём = умолчание сервера (high)
                kw["reasoning_effort"] = eff
        else:
            kw["extra_body"] = {"thinking": {"type": "disabled"}}
    if not think_on:
        kw["temperature"] = temperature
    if json_mode:
        # JSON-режим в thinking V4 поддерживается с мая 2026; в reasoner — нет.
        if not (_is_reasoner(model) and think_on):
            kw["response_format"] = {"type": "json_object"}
    return kw


async def ask(system: str, user: str, *, model: str | None = None,
              json_mode: bool = False, thinking: bool | None = None,
              effort: str | None = None, temperature: float = 1.0,
              max_tokens: int | None = None, route: str = "ask",
              api_key: str | None = None) -> str:
    """Один проход. Возвращает content (с фолбэком на reasoning_content)."""
    model = model or config.DEEPSEEK_MODEL
    thinking = config.AI_THINKING if thinking is None else thinking
    effort = effort or config.AI_REASONING_EFFORT
    max_tokens = max_tokens or config.AI_MAX_TOKENS
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": user}]
    kw = _build_kwargs(model, msgs, json_mode=json_mode, thinking=thinking,
                       effort=effort, temperature=temperature, max_tokens=max_tokens)
    last_err: Exception | None = None
    for attempt in range(1, _TRIES + 1):
        try:
            client = await _get_client(api_key)
            t0 = time.time()
            async with _sem:
                resp = await client.chat.completions.create(**kw)
            _add_usage(getattr(resp, "usage", None), route, model)
            if not getattr(resp, "choices", None):
                raise ValueError(f"пустой ответ ИИ (model={model}, route={route})")
            msg = resp.choices[0].message
            if getattr(resp.choices[0], "finish_reason", None) == "length":   # 24.09.2026: обрезку не молчим
                logger.warning("[%s] ответ обрезан по max_tokens=%s (finish_reason=length)", route, kw.get("max_tokens"))
                note_error(f"ответ обрезан по потолку {kw.get('max_tokens')} токенов (finish_reason=length)", route)
            content = (getattr(msg, "content", None) or "").strip()
            if not content:
                content = _tail_of_thinking(
                    (getattr(msg, "reasoning_content", None) or "").strip())
            logger.info("[%s] model=%s think=%s %.1fs out=%dch try=%d",
                        route, model, thinking, time.time() - t0, len(content), attempt)
            if not content:
                raise ValueError(f"пустой ответ ИИ (model={model}, route={route})")
            return content
        except Exception as e:
            last_err = e
            alt = _try_failover(e, api_key) if attempt < _TRIES else ""
            if alt:
                logger.info("[%s] аккаунт упёрся (%s) — переключаюсь на другой аккаунт",
                            route, str(e)[:60])
                api_key = alt
                await asyncio.sleep(1.0)
                continue
            delay = _retry_delay(e, attempt) if attempt < _TRIES else None
            if delay is None:
                note_error(e, route)
                raise
            logger.warning("[%s] попытка %d/%d не удалась (%s) — повтор через %.0fс",
                           route, attempt, _TRIES, str(e)[:90], delay)
            if getattr(e, "status_code", None) == 401 or "401" in str(e):
                reset_client(_ck(api_key))   # только клиент этого ключа, не весь пакет
            await asyncio.sleep(delay)
    raise last_err  # недостижимо


def _extract_json(text: str) -> Any:
    """Достать JSON из текста: code-fence, затем сбалансированный объект."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # сбалансированный поиск первого { ... } (quote-aware)
    start = text.find("{")
    if start < 0:
        start = text.find("[")
    if start < 0:
        raise ValueError("JSON не найден в ответе ИИ")
    depth = 0
    in_str = False
    esc = False
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    for i in range(start, len(text)):
        c = text[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("незакрытый JSON в ответе ИИ")


async def ask_json(system: str, user: str, *, model: str | None = None,
                   thinking: bool | None = None, effort: str | None = None,
                   max_tokens: int | None = None, route: str = "ask_json",
                   api_key: str | None = None) -> Any:
    """Один проход c json_mode. Возвращает распарсенный объект."""
    raw = await ask(system, user, model=model, json_mode=True,
                    thinking=thinking, effort=effort, max_tokens=max_tokens,
                    route=route, api_key=api_key)
    return _extract_json(raw)


async def stream(system: str, user: str, *,
                 on_think: Callable[[str, str], Awaitable[None]] | None = None,
                 on_text: Callable[[str, str], Awaitable[None]] | None = None,
                 on_retry: Callable[[], Awaitable[None]] | None = None,
                 model: str | None = None, thinking: bool | None = None,
                 effort: str | None = None, temperature: float = 1.0,
                 max_tokens: int | None = None, route: str = "stream",
                 api_key: str | None = None) -> str:
    """Стрим. on_think(delta, full) — поток размышления; on_text(delta, full) —
    поток ответа. Возвращает полный content."""
    model = model or config.DEEPSEEK_MODEL
    thinking = config.AI_THINKING if thinking is None else thinking
    effort = effort or config.AI_REASONING_EFFORT
    max_tokens = max_tokens or config.AI_MAX_TOKENS
    client = await _get_client(api_key)
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": user}]
    kw = _build_kwargs(model, msgs, json_mode=False, thinking=thinking,
                       effort=effort, temperature=temperature, max_tokens=max_tokens)
    kw["stream"] = True
    kw["stream_options"] = {"include_usage": True}
    think_buf: list[str] = []
    text_buf: list[str] = []
    fin: list = [None]                       # finish_reason последнего чанка
    for _attempt in range(1, _TRIES + 1):
        think_buf.clear(); text_buf.clear()
        try:
            async with _sem:
                st = await client.chat.completions.create(**kw, timeout=STREAM_TIMEOUT)
                async for chunk in st:
                    if getattr(chunk, "usage", None):
                        _add_usage(chunk.usage, route, model)
                    if not chunk.choices:
                        continue
                    if getattr(chunk.choices[0], "finish_reason", None):
                        fin[0] = chunk.choices[0].finish_reason
                    d = chunk.choices[0].delta
                    rd = getattr(d, "reasoning_content", None)
                    if rd:
                        think_buf.append(rd)
                        if on_think:
                            try:
                                # full=None: не собираем всю строку на каждом чанке
                                # (было O(n²) и морозило event loop); полный текст
                                # копит потребитель по дельтам
                                await on_think(rd, None)
                            except Exception:
                                pass
                    cd = getattr(d, "content", None)
                    if cd:
                        text_buf.append(cd)
                        if on_text:
                            try:
                                await on_text(cd, None)   # см. коммент выше про full=None
                            except Exception:
                                pass
            break   # стрим дошёл до конца
        except Exception as e:
            got = len("".join(text_buf))   # спасаем только реальный КОНТЕНТ,
            if got > 400:                  # сырое мышление вердиктом не становится
                logger.warning("[%s] стрим оборван на %d симв. контента (%s) — возвращаю накопленное",
                               route, got, str(e)[:90])
                note_error(f"поток оборван на {got} симв. — отдано накопленное ({str(e)[:60]})", route)
                text_buf.append("\n[⚠ поток оборван — ответ неполный]")
                break
            alt = _try_failover(e, api_key) if _attempt < _TRIES else ""
            if alt:
                logger.info("[%s] аккаунт упёрся (%s) — переключаюсь на другой аккаунт",
                            route, str(e)[:60])
                api_key = alt
                client = await _get_client(api_key)
                if on_retry and (think_buf or text_buf):
                    try:
                        await on_retry()
                    except Exception:
                        pass
                await asyncio.sleep(1.0)
                continue
            delay = _retry_delay(e, _attempt) if _attempt < _TRIES else None
            if delay is None:
                note_error(e, route)
                raise
            logger.warning("[%s] стрим: попытка %d/%d (%s) — повтор через %.0fс",
                           route, _attempt, _TRIES, str(e)[:90], delay)
            if getattr(e, "status_code", None) == 401 or "401" in str(e):
                reset_client(_ck(api_key))   # только клиент этого ключа, не весь пакет
            client = await _get_client(api_key)
            if on_retry and (think_buf or text_buf):
                try:
                    await on_retry()   # фронт очистит недостриженную стадию
                except Exception:
                    pass
            await asyncio.sleep(delay)
    if fin[0] == "length":                   # 24.09.2026: обрезку не молчим
        logger.warning("[%s] ответ обрезан по max_tokens=%s (finish_reason=length)", route, kw.get("max_tokens"))
        note_error(f"ответ обрезан по потолку {kw.get('max_tokens')} токенов (finish_reason=length)", route)
    out = "".join(text_buf).strip()
    if not out:                      # модель ушла целиком в reasoning — не теряем разбор,
        out = _tail_of_thinking("".join(think_buf).strip())  # но отдаём только выводы
    return out


async def chat(messages: list[dict], *, model: str | None = None,
               thinking: bool | None = None, effort: str | None = None,
               temperature: float = 1.0, max_tokens: int | None = None,
               on_think: Callable[[str, str], Awaitable[None]] | None = None,
               on_text: Callable[[str, str], Awaitable[None]] | None = None,
               route: str = "chat", api_key: str | None = None) -> str:
    """Многоходовой чат с памятью (полная история сообщений). Стримится."""
    model = model or config.DEEPSEEK_MODEL
    thinking = config.AI_THINKING if thinking is None else thinking
    effort = effort or config.AI_REASONING_EFFORT
    max_tokens = max_tokens or config.AI_MAX_TOKENS
    client = await _get_client(api_key)
    kw = _build_kwargs(model, messages, json_mode=False, thinking=thinking,
                       effort=effort, temperature=temperature, max_tokens=max_tokens)
    kw["stream"] = True
    kw["stream_options"] = {"include_usage": True}
    think_buf: list[str] = []
    text_buf: list[str] = []
    fin: list = [None]                       # finish_reason последнего чанка
    for _attempt in range(1, _TRIES + 1):
        think_buf.clear(); text_buf.clear()
        try:
            async with _sem:
                st = await client.chat.completions.create(**kw, timeout=STREAM_TIMEOUT)
                async for chunk in st:
                    if getattr(chunk, "usage", None):
                        _add_usage(chunk.usage, route, model)
                    if not chunk.choices:
                        continue
                    if getattr(chunk.choices[0], "finish_reason", None):
                        fin[0] = chunk.choices[0].finish_reason
                    d = chunk.choices[0].delta
                    rd = getattr(d, "reasoning_content", None)
                    if rd:
                        think_buf.append(rd)
                        if on_think:
                            try:
                                # full=None: не собираем всю строку на каждом чанке
                                # (было O(n²) и морозило event loop); полный текст
                                # копит потребитель по дельтам
                                await on_think(rd, None)
                            except Exception:
                                pass
                    cd = getattr(d, "content", None)
                    if cd:
                        text_buf.append(cd)
                        if on_text:
                            try:
                                await on_text(cd, None)   # см. коммент выше про full=None
                            except Exception:
                                pass
            break   # стрим дошёл до конца
        except Exception as e:
            got = len("".join(text_buf))
            if got > 200:   # часть ответа уже есть — не сжигаем, отдаём накопленное
                logger.warning("[%s] чат-стрим оборван на %d симв. — возвращаю накопленное",
                               route, got)
                note_error(f"поток оборван на {got} симв. — отдано накопленное ({str(e)[:60]})", route)
                text_buf.append("\n[⚠ поток оборван — ответ неполный]")
                break
            alt = _try_failover(e, api_key) if _attempt < _TRIES else ""
            if alt:
                logger.info("[%s] аккаунт упёрся (%s) — переключаюсь на другой аккаунт",
                            route, str(e)[:60])
                api_key = alt
                client = await _get_client(api_key)
                await asyncio.sleep(1.0)
                continue
            delay = _retry_delay(e, _attempt) if _attempt < _TRIES else None
            if delay is None:
                note_error(e, route)
                raise
            logger.warning("[%s] чат: попытка %d/%d (%s) — повтор через %.0fс",
                           route, _attempt, _TRIES, str(e)[:90], delay)
            if getattr(e, "status_code", None) == 401 or "401" in str(e):
                reset_client(_ck(api_key))   # только клиент этого ключа, не весь пакет
            client = await _get_client(api_key)
            await asyncio.sleep(delay)
    if fin[0] == "length":                   # 24.09.2026: обрезку не молчим
        logger.warning("[%s] ответ обрезан по max_tokens=%s (finish_reason=length)", route, kw.get("max_tokens"))
        note_error(f"ответ обрезан по потолку {kw.get('max_tokens')} токенов (finish_reason=length)", route)
    out = "".join(text_buf).strip()
    if not out:   # модель ушла целиком в reasoning — отдаём выводы, не пустоту
        out = _tail_of_thinking("".join(think_buf).strip())
    return out


async def health() -> dict:
    """Лёгкая проверка ключа: пул аккаунтов, иначе первый ключ инструмента."""
    pool = config.deepseek_keys()
    key = (pool[0] if pool else None) or config.DEEPSEEK_API_KEY \
        or next(iter(config.INSTRUMENT_KEYS.values()), None)
    if not key:
        return {"ok": False, "error": "ни одного ключа DeepSeek не задано"}
    try:
        out = await ask("Ты эхо.", "Ответь одним словом: готов", model=config.DEEPSEEK_MODEL_FAST,
                        thinking=False, max_tokens=20, route="health", api_key=key)   # FLASH: дешёвый пинг ключа
        return {"ok": True, "reply": out[:60]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
