"""Общие фикстуры: временная база, фейковая модель, фейковый отправитель, подменённые часы."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable

import pytest
import pytest_asyncio

from oracle import timeutil
from oracle.config import Settings
from oracle.db import DB
from oracle.llm import LLMResponse, ToolCall
from oracle.tools.base import Services, ToolContext


class FakeLLM:
    """Модель по сценарию. `script` — список ответов по порядку: LLMResponse, str (текст),
    dict (tool_calls: {"name": ..., "args": {...}} или {"calls": [...]}) или callable(messages, kw) → любое из этого.
    Если сценарий кончился — отдаёт `default`. Все вызовы пишутся в `calls`."""

    def __init__(self, script: list[Any] | None = None, default: Any = "ок"):
        self.script = list(script or [])
        self.default = default
        self.calls: list[dict] = []

    def _to_resp(self, item: Any, messages: list[dict], kw: dict) -> LLMResponse:
        if callable(item) and not isinstance(item, LLMResponse):
            item = item(messages, kw)
        if isinstance(item, LLMResponse):
            return item
        if isinstance(item, str):
            return LLMResponse(content=item, finish_reason="stop")
        if isinstance(item, dict):
            calls = item.get("calls") or [item]
            return LLMResponse(
                content=item.get("content", ""),
                reasoning=item.get("reasoning", ""),
                tool_calls=[ToolCall(id=f"call_{i}", name=c["name"],
                                     arguments=json.dumps(c.get("args", {}), ensure_ascii=False))
                            for i, c in enumerate(calls)],
                finish_reason="tool_calls")
        raise TypeError(f"не понимаю элемент сценария: {item!r}")

    async def complete(self, messages: list[dict], **kw: Any) -> LLMResponse:
        self.calls.append({"messages": [dict(m) for m in messages], **kw})
        item = self.script.pop(0) if self.script else self.default
        return self._to_resp(item, messages, kw)

    async def ask(self, system: str, user: str, **kw: Any) -> str:
        r = await self.complete([{"role": "system", "content": system},
                                 {"role": "user", "content": user}], **kw)
        return r.content.strip()

    async def ask_json(self, system: str, user: str, **kw: Any) -> Any:
        from oracle.llm import extract_json
        return extract_json(await self.ask(system, user, json_mode=True, **kw))

    async def aclose(self) -> None:
        pass


class FakeNotifier:
    """Собирает всё, что бот отправил бы владельцу."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._mid = 100

    async def send(self, text: str, buttons=None, *, silent: bool = False) -> int:
        self._mid += 1
        self.sent.append({"kind": "text", "text": text, "buttons": buttons, "silent": silent, "id": self._mid})
        return self._mid

    async def send_file(self, data: bytes, filename: str, caption: str = "") -> None:
        self.sent.append({"kind": "file", "data": data, "filename": filename, "text": caption})

    async def send_voice(self, data: bytes, filename: str = "voice.mp3", caption: str = "") -> None:
        self.sent.append({"kind": "voice", "data": data, "filename": filename, "text": caption})

    def texts(self) -> list[str]:
        return [s["text"] for s in self.sent]


class Clock:
    """Управляемые часы: clock.set(datetime) / clock.advance(minutes=...)."""

    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def set(self, d: datetime) -> None:
        self.now = d if d.tzinfo else d.replace(tzinfo=timezone.utc)

    def advance(self, **kw: float) -> datetime:
        from datetime import timedelta
        self.now = self.now + timedelta(**kw)
        return self.now


@pytest.fixture
def clock():
    c = Clock(datetime(2026, 9, 28, 6, 0, tzinfo=timezone.utc))   # 09:00 по Москве, понедельник
    timeutil.set_clock(c)
    yield c
    timeutil.set_clock(None)


@pytest.fixture
def cfg(tmp_path) -> Settings:
    # files_workspace держим внутри tmp_path (и оканчивающимся на «Oracle»), чтобы зеркало идей
    # в тестах не писало в настоящую домашнюю папку, а само значение оставалось узнаваемым
    return Settings(bot_token="1:x", owner_id=42, llm_api_key="sk-test",
                    timezone="Europe/Moscow", data_dir=tmp_path, db_path=tmp_path / "t.db",
                    news_feeds=(), web_search=False,
                    files_roots=(tmp_path,), files_workspace=tmp_path / "Oracle")


@pytest_asyncio.fixture
async def db(tmp_path):
    d = await DB(tmp_path / "t.db").open()
    yield d
    await d.close()


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def ctx(cfg, db, fake_llm, notifier, clock) -> ToolContext:
    return ToolContext(cfg=cfg, db=db, llm=fake_llm, services=Services(notifier=notifier))


def with_cfg(cfg: Settings, **kw: Any) -> Settings:
    return replace(cfg, **kw)
