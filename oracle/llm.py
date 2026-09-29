"""Клиент LLM: DeepSeek (или любой OpenAI-совместимый API) напрямую через httpx.

Два режима:
  • быстрый (по умолчанию) — модель LLM_MODEL, размышление ВЫКЛЮЧЕНО (thinking: disabled):
    ответ за секунды. Это лечит «ждёт непонятно чего»: у DeepSeek V4 размышление включено
    по умолчанию и на каждый чих уходят минуты.
  • глубокий — LLM_MODEL_DEEP с размышлением (reasoning_effort = LLM_DEEP_EFFORT):
    для оценки идей, ночной рефлексии, /deep.

Инструменты (function calling): в режиме размышления DeepSeek требует возвращать
`reasoning_content` ассистентских сообщений с tool_calls во всех последующих запросах —
`LLMResponse.to_message()` кладёт его туда сам.

Таймаут не повторяется, если сервер уже принял запрос и молчит: второй раз он быстрее не ответит,
а владелец ждёт. Повторяются только дешёвые сбои до генерации (не дозвонились, 5xx, 429).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings, clean_effort

log = logging.getLogger("oracle.llm")


class LLMError(Exception):
    """Ошибка модели человеческим текстом. fatal — повторять бессмысленно (ключ, баланс).
    kind — что именно: "model" (модель не принята/нет такой), "context" (контекст переполнен),
    "filtered" (ответ отфильтровал провайдер), "timeout", "" — прочее."""

    def __init__(self, message: str, *, status: int | None = None, fatal: bool = False, kind: str = ""):
        super().__init__(message)
        self.status = status
        self.fatal = fatal
        self.kind = kind


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str          # JSON-строка, как пришла от модели


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning: str = ""
    finish_reason: str = ""
    usage: dict = field(default_factory=dict)
    model: str = ""

    def to_message(self) -> dict:
        """Ассистентское сообщение для продолжения диалога (с tool_calls и reasoning_content)."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in self.tool_calls]
            if self.reasoning:
                msg["reasoning_content"] = self.reasoning
        return msg


def _is_deepseek_model(model: str) -> bool:
    return "deepseek" in (model or "").lower()


_CONTEXT_WORDS = ("context length", "context_length", "maximum context", "context window", "too many tokens",
                  "prompt is too long", "reduce the length", "exceeds the model")
_NO_MODEL_WORDS = ("not exist", "does not exist", "not found", "model_not_found", "unknown model",
                   "no such model", "invalid model", "not a valid model", "unsupported model")


def error_kind(status: int | None, body: str = "") -> str:
    """По ответу API: "context" — контекст переполнен, "model" — такой модели нет/не принята, "" — прочее."""
    low = (body or "").lower()
    if status in (400, 413, 422) and any(w in low for w in _CONTEXT_WORDS):
        return "context"
    if status in (400, 404, 422) and "model" in low and any(w in low for w in _NO_MODEL_WORDS):
        return "model"
    return ""


def human_error(status: int | None, body: str = "", exc: Exception | None = None, *,
                local: bool = False) -> tuple[str, bool]:
    """(текст, fatal) по коду ответа/исключению. local — модель не DeepSeek (свой LLM_BASE_URL: Ollama,
    LM Studio…): нет связи — скорее всего, адрес, а не интернет."""
    b = (body or "")[:300]
    if status == 401:
        return "ключ LLM не принят (401) — проверь DEEPSEEK_API_KEY в .env", True
    if status == 402:
        return "на счёте DeepSeek кончились деньги (402) — пополни на platform.deepseek.com", True
    if status == 403:
        return "доступ к модели запрещён (403)", True
    kind = error_kind(status, body)
    if kind == "context":
        return ("контекст переполнен — разговор стал слишком длинным для модели: сделай /reset "
                "(свежие реплики свернутся, память и позиции останутся) или спроси короче"), True
    if kind == "model":
        return f"модель не принята API ({status}): {b} — проверь LLM_MODEL / LLM_MODEL_DEEP в .env", True
    if status == 422:
        return f"неверный параметр запроса (422): {b} — проверь настройки LLM_* в .env", True
    if status == 400:
        return f"запрос не принят API (400): {b}", True
    if status == 404:
        return f"адрес API не найден (404) — проверь LLM_BASE_URL в .env: {b}", True
    if status == 429:
        return "слишком много запросов к модели (429) — подожди минуту", False
    if status is not None and status >= 500:
        return f"сервер модели сбоит ({status})", False
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
        return "модель не ответила вовремя (таймаут)", False
    if isinstance(exc, httpx.TransportError):
        if local:
            return ("нет связи с сервером модели (LLM_BASE_URL) — проверь адрес и что сервер запущен; "
                    "Ollama/LM Studio из Docker на Linux — см. README"), False
        return "нет связи с сервером модели — проверь интернет", False
    return (f"ошибка модели: {b or exc}", False)


_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def extract_json(text: str) -> Any:
    """Достать JSON из ответа модели (голый, в ```json```, с мусором вокруг)."""
    t = (text or "").strip()
    if not t:
        raise ValueError("пустой ответ вместо JSON")
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    try:
        return json.loads(t)
    except ValueError:
        m = _JSON_BLOCK.search(t)
        if m:
            return json.loads(m.group(0))
        raise


class LLM:
    """Асинхронный клиент chat/completions."""

    RETRIES = 3

    def __init__(self, cfg: Settings, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._client = client
        self._own_client = client is None
        self.usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.cfg.llm_base_url,
                headers={"Authorization": f"Bearer {self.cfg.llm_api_key}",
                         "Content-Type": "application/json"},
                # read — между байтами; DeepSeek шлёт keep-alive, общий срок держит wait_for
                timeout=httpx.Timeout(connect=20.0, read=max(self.cfg.llm_deep_timeout, 120.0),
                                      write=60.0, pool=60.0))
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._own_client:
            await self._client.aclose()
        self._client = None

    # ── сборка запроса ──
    def build_payload(self, messages: list[dict], *, tools: list[dict] | None = None,
                      deep: bool = False, json_mode: bool = False,
                      max_tokens: int | None = None, temperature: float | None = None,
                      tool_choice: str | dict | None = None, model: str | None = None) -> dict:
        cfg = self.cfg
        model = model or (cfg.llm_model_deep if deep else cfg.llm_model)
        payload: dict[str, Any] = {"model": model, "messages": messages}
        payload["max_tokens"] = int(max_tokens or (cfg.llm_deep_max_tokens if deep else cfg.llm_fast_max_tokens))
        thinking = False
        if cfg.is_deepseek or _is_deepseek_model(model):
            if deep:
                thinking = True
                effort, _ = clean_effort(cfg.llm_deep_effort, fast=False)
                if effort not in {"auto", "default"}:
                    payload["reasoning_effort"] = effort
            else:
                effort, _ = clean_effort(cfg.llm_fast_thinking, fast=True)   # API примет только low|high|max
                if effort == "off":
                    payload["thinking"] = {"type": "disabled"}
                else:
                    thinking = True
                    payload["reasoning_effort"] = effort
        if not thinking:  # в режиме размышления температура игнорируется — не шлём
            payload["temperature"] = cfg.llm_temperature if temperature is None else temperature
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    # ── вызов ──
    async def complete(self, messages: list[dict], *, tools: list[dict] | None = None,
                       deep: bool = False, json_mode: bool = False,
                       max_tokens: int | None = None, temperature: float | None = None,
                       timeout: float | None = None, tool_choice: str | dict | None = None,
                       model: str | None = None, allow_empty: bool = False) -> LLMResponse:
        """Один ответ модели. Повторяет дешёвые сбои (5xx, 429, обрыв связи, таймаут соединения);
        таймаут генерации не повторяет. Пустой ответ без tool_calls — ошибка после повторов, если только
        не allow_empty (агент после успешных инструментов: пустой итог — не повод гонять модель ещё раз)."""
        if not self.cfg.llm_api_key:
            raise LLMError("нет ключа DeepSeek — впиши DEEPSEEK_API_KEY в .env", fatal=True)
        payload = self.build_payload(messages, tools=tools, deep=deep, json_mode=json_mode,
                                     max_tokens=max_tokens, temperature=temperature,
                                     tool_choice=tool_choice, model=model)
        limit = timeout or (self.cfg.llm_deep_timeout if deep else self.cfg.llm_fast_timeout)
        last: LLMError | None = None
        fell_back = False
        for attempt in range(1, self.RETRIES + 1):
            try:
                resp = await asyncio.wait_for(self._post(payload), timeout=limit)
            except LLMError as e:
                last = e
                # глубокая модель недоступна (сняли с API) — один раз уходим на быструю
                if e.kind == "model" and deep and not fell_back \
                        and payload["model"] != self.cfg.llm_model:
                    log.warning("глубокая модель %s не принята — переключаюсь на %s",
                                payload["model"], self.cfg.llm_model)
                    payload["model"] = self.cfg.llm_model
                    fell_back = True
                    continue
                if e.fatal or attempt == self.RETRIES:
                    raise
                await asyncio.sleep(self._backoff(e, attempt))
                continue
            except (asyncio.TimeoutError, httpx.TimeoutException) as e:
                msg, _ = human_error(None, exc=e)
                last = LLMError(msg, kind="timeout")
                # не дозвонились / пул занят / не отправили запрос — повтор дешёвый; а если сервер принял
                # запрос и молчит (занят), второй раз он быстрее не ответит — сразу ошибка, не держим владельца
                cheap = isinstance(e, (httpx.ConnectTimeout, httpx.PoolTimeout, httpx.WriteTimeout))
                if attempt >= 2 or not cheap:
                    raise last
                await asyncio.sleep(2.0)
                continue
            except httpx.TransportError as e:
                msg, _ = human_error(None, exc=e, local=not self.cfg.is_deepseek)
                last = LLMError(msg)
                if attempt == self.RETRIES:
                    raise last
                await asyncio.sleep(2.0 * attempt)
                continue
            if not resp.content.strip() and not resp.tool_calls:
                if resp.finish_reason == "content_filter":    # повтор даст то же самое
                    raise LLMError("провайдер модели отфильтровал ответ (content_filter) — это цензура на стороне "
                                   "DeepSeek, а не моя; переформулируй вопрос", kind="filtered")
                if allow_empty:
                    return resp
                if resp.finish_reason == "length":
                    raise LLMError("модель упёрлась в потолок токенов и не успела ответить — "
                                   "увеличь LLM_*_MAX_TOKENS или спроси короче")
                last = LLMError("модель вернула пустой ответ")
                if attempt == self.RETRIES:
                    raise last
                await asyncio.sleep(1.0)
                continue
            return resp
        raise last or LLMError("модель не ответила")

    @staticmethod
    def _backoff(e: LLMError, attempt: int) -> float:
        if e.status == 429:
            return 5.0 * attempt
        return 2.0 * attempt

    async def _post(self, payload: dict) -> LLMResponse:
        http = await self._http()
        try:
            r = await http.post("/chat/completions", json=payload)
        except httpx.TimeoutException:
            raise
        except httpx.TransportError:
            raise
        if r.status_code != 200:
            text, fatal = human_error(r.status_code, r.text)
            raise LLMError(text, status=r.status_code, fatal=fatal, kind=error_kind(r.status_code, r.text))
        try:
            data = r.json()
        except ValueError:
            raise LLMError("ответ сервера — не JSON", status=r.status_code)
        return self._parse(data, payload.get("model", ""))

    def _parse(self, data: dict, model: str) -> LLMResponse:
        choices = data.get("choices") or []
        if not choices:
            err = data.get("error") or {}
            raise LLMError(f"пустой ответ сервера: {err.get('message') or data}"[:300])
        ch = choices[0]
        msg = ch.get("message") or {}
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if not isinstance(args, str):
                args = json.dumps(args or {}, ensure_ascii=False)
            calls.append(ToolCall(id=tc.get("id") or f"call_{i}", name=fn.get("name") or "", arguments=args))
        usage = data.get("usage") or {}
        self.usage_total["calls"] += 1
        self.usage_total["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        self.usage_total["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        return LLMResponse(
            content=msg.get("content") or "",
            tool_calls=calls,
            reasoning=msg.get("reasoning_content") or "",
            finish_reason=ch.get("finish_reason") or "",
            usage=usage,
            model=data.get("model") or model,
        )

    # ── удобные обёртки ──
    async def ask(self, system: str, user: str, *, deep: bool = False, json_mode: bool = False,
                  max_tokens: int | None = None, temperature: float | None = None,
                  timeout: float | None = None) -> str:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        r = await self.complete(msgs, deep=deep, json_mode=json_mode, max_tokens=max_tokens,
                                temperature=temperature, timeout=timeout)
        return r.content.strip()

    async def ask_json(self, system: str, user: str, *, deep: bool = False,
                       max_tokens: int | None = None, temperature: float | None = 0.4,
                       timeout: float | None = None) -> Any:
        """Ответ-JSON. В промпте обязано быть слово «json» (требование JSON-режима DeepSeek)."""
        if "json" not in (system + user).lower():
            system = system + "\nОтвет — строго JSON-объект."
        raw = await self.ask(system, user, deep=deep, json_mode=True, max_tokens=max_tokens,
                             temperature=temperature, timeout=timeout)
        try:
            return extract_json(raw)
        except ValueError:
            # один повтор с напоминанием о формате
            raw = await self.ask(system + "\nВерни ТОЛЬКО валидный JSON без пояснений.", user,
                                 deep=deep, json_mode=True, max_tokens=max_tokens,
                                 temperature=temperature, timeout=timeout)
            return extract_json(raw)
