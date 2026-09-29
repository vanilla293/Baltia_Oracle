"""Локальный веб-сервер панели: отдаёт страницу и небольшой JSON-API поверх «провайдера состояния».

Только на этой машине: сервер привязан к 127.0.0.1, а каждый запрос обязан нести случайный токен
запуска (?t=… или заголовок X-Dash-Token). Токен печатается в лог вместе с адресом — им и
открывается панель. Без токена страница отдаёт 403, API — 401.

`start_dashboard(state, host=…, port=…, open_browser=…)` поднимает сервер на своём runner/site,
не блокирует и возвращает объект с `.url`, `.token` и асинхронным `.stop()`.

`state` — утиный «провайдер состояния» (см. `DashboardState`): его асинхронные методы отдают
JSON-совместимые данные разделов и применяют изменения. Реальную реализацию поверх runtime/usage/db
даёт интегратор; здесь есть `FakeState` для тестов и как образец.
"""
from __future__ import annotations

import asyncio
import functools
import hmac
import json
import logging
import os
import secrets
import sys
import threading
import webbrowser
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from aiohttp import web

from .page import PAGE
from ..settings_schema import (masked_values, schema_public, sections, to_env_updates,
                               validate, get_setting)

log = logging.getLogger("oracle.dashboard")

_dumps = functools.partial(json.dumps, ensure_ascii=False, default=str)

# ключи хранилища приложения (web.AppKey — рекомендованный aiohttp способ)
STATE_KEY: "web.AppKey[Any]" = web.AppKey("state", object)
TOKEN_KEY: web.AppKey[str] = web.AppKey("token", str)

_FORBIDDEN_HTML = (
    "<!doctype html><meta charset='utf-8'>"
    "<body style='background:#111;color:#ccc;font-family:sans-serif;padding:40px'>"
    "<h2>403 — нужен токен доступа</h2>"
    "<p>Открой ссылку из лога целиком (в ней есть <code>?t=…</code>).</p></body>"
)


# ── контракт «провайдера состояния» ──────────────────────────────────────────────
@runtime_checkable
class DashboardState(Protocol):
    """Что панель ждёт от объекта `state`. Все методы асинхронные и возвращают JSON-совместимое.

    Интегратор реализует их поверх runtime/usage/db (см. отчёт билдера про список методов).
    """

    async def status(self) -> dict: ...
    async def dialog(self, limit: int = 50) -> Any: ...
    async def memory(self) -> Any: ...
    async def opinions(self) -> Any: ...
    async def journal(self) -> Any: ...
    async def reminders(self) -> Any: ...
    async def ideas(self) -> Any: ...
    async def projects(self) -> Any: ...
    async def news_recent(self) -> Any: ...
    async def usage(self, days: int = 30) -> Any: ...
    async def logs(self, after_id: int = 0, level: str = "INFO") -> Any: ...
    async def settings_get(self) -> Any: ...
    async def settings_set(self, form: dict) -> dict: ...
    async def action(self, name: str, params: dict | None = None) -> dict: ...


# ── маршрутизация GET /api/<section> → метод state ────────────────────────────────
def _qint(request: web.Request, name: str, default: int) -> int:
    try:
        return int(request.query.get(name, default))
    except (TypeError, ValueError):
        return default


# каждый элемент — корутина метода state (запрос нужен только для параметров из query)
_SECTIONS: dict[str, Callable[[Any, web.Request], Awaitable[Any]]] = {
    "status": lambda s, r: s.status(),
    "dialog": lambda s, r: s.dialog(_qint(r, "limit", 50)),
    "memory": lambda s, r: s.memory(),
    "opinions": lambda s, r: s.opinions(),
    "journal": lambda s, r: s.journal(),
    "reminders": lambda s, r: s.reminders(),
    "ideas": lambda s, r: s.ideas(),
    "projects": lambda s, r: s.projects(),
    "news": lambda s, r: s.news_recent(),
    "usage": lambda s, r: s.usage(_qint(r, "days", 30)),
    "logs": lambda s, r: s.logs(_qint(r, "after_id", 0), r.query.get("level", "INFO")),
    "settings": lambda s, r: s.settings_get(),
}


# ── проверка токена ───────────────────────────────────────────────────────────────
def _token_ok(request: web.Request, token: str) -> bool:
    got = request.query.get("t") or request.headers.get("X-Dash-Token") or ""
    return bool(got) and hmac.compare_digest(str(got), str(token))


def _auth_middleware(token: str):
    @web.middleware
    async def mw(request: web.Request, handler: Callable) -> web.StreamResponse:
        if not _token_ok(request, token):
            if request.path.startswith("/api/"):
                return web.json_response({"ok": False, "error": "нужен токен доступа"}, status=401)
            return web.Response(text=_FORBIDDEN_HTML, status=403, content_type="text/html")
        return await handler(request)
    return mw


# ── обработчики ───────────────────────────────────────────────────────────────────
async def _page(request: web.Request) -> web.Response:
    return web.Response(text=PAGE, content_type="text/html", charset="utf-8")


async def _api_get(request: web.Request) -> web.Response:
    state = request.app[STATE_KEY]
    section = request.match_info.get("section", "")
    getter = _SECTIONS.get(section)
    if getter is None:
        return web.json_response({"ok": False, "error": f"нет раздела «{section}»"}, status=404)
    try:
        data = await getter(state, request)
    except Exception as e:                       # раздел не должен ронять сервер
        log.warning("панель: раздел %s не отдал данные: %r", section, e)
        return web.json_response({"ok": False, "error": "ошибка данных"}, status=500)
    return web.json_response(data, dumps=_dumps)


async def _read_json(request: web.Request) -> Any:
    try:
        return await request.json()
    except Exception:
        return None


async def _api_settings(request: web.Request) -> web.Response:
    state = request.app[STATE_KEY]
    form = await _read_json(request)
    if not isinstance(form, dict):
        return web.json_response({"ok": False, "error": "жду объект настроек"}, status=400)
    try:
        result = await state.settings_set(form)
    except Exception as e:
        log.warning("панель: сохранение настроек упало: %r", e)
        return web.json_response({"ok": False, "error": "не смог сохранить"}, status=500)
    return web.json_response(result, dumps=_dumps)


async def _api_action(request: web.Request) -> web.Response:
    state = request.app[STATE_KEY]
    body = await _read_json(request)
    if not isinstance(body, dict):
        return web.json_response({"ok": False, "error": "жду объект действия"}, status=400)
    name = str(body.get("name") or "").strip()
    if not name:
        return web.json_response({"ok": False, "error": "не указано действие"}, status=400)
    params = body.get("params")
    params = params if isinstance(params, dict) else {}
    try:
        result = await state.action(name, params)
    except Exception as e:
        log.warning("панель: действие %s упало: %r", name, e)
        return web.json_response({"ok": False, "error": "действие не выполнено"}, status=500)
    return web.json_response(result if isinstance(result, dict) else {"ok": True, "result": result},
                             dumps=_dumps)


def create_app(state: DashboardState, *, token: str) -> web.Application:
    """Настроенное aiohttp-приложение панели (страница + API), защищённое токеном.
    Отдельно от `start_dashboard`, чтобы тесты поднимали его через TestServer без реального порта."""
    app = web.Application(middlewares=[_auth_middleware(token)])
    app[STATE_KEY] = state
    app[TOKEN_KEY] = token
    app.router.add_get("/", _page)
    app.router.add_get("/api/{section}", _api_get)
    app.router.add_post("/api/settings", _api_settings)
    app.router.add_post("/api/action", _api_action)
    return app


# ── запуск ────────────────────────────────────────────────────────────────────────
def _is_headless() -> bool:
    """Есть ли смысл открывать браузер. На Windows/macOS — да; на Linux нужен дисплей."""
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _safe_open(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass


class Dashboard:
    """Живой сервер панели. `.url` — адрес с токеном (печатается в лог), `.token` — сам токен,
    `.stop()` — корректно закрыть runner."""

    def __init__(self, *, url: str, token: str, host: str, port: int,
                 runner: web.AppRunner, app: web.Application) -> None:
        self.url = url
        self.token = token
        self.host = host
        self.port = port
        self._runner = runner
        self.app = app

    async def stop(self) -> None:
        try:
            await self._runner.cleanup()
        except Exception:
            log.warning("панель: не смог остановить сервер", exc_info=True)


async def start_dashboard(state: DashboardState, *, host: str = "127.0.0.1", port: int = 8765,
                          open_browser: bool = True) -> Dashboard:
    """Поднять локальную панель и сразу вернуть управление (не блокирует).

    host  — куда привязаться (по умолчанию только 127.0.0.1, локально);
    port  — порт (0 — любой свободный, реальный попадёт в .url);
    open_browser — открыть панель в браузере (в отдельном потоке, только если есть дисплей).
    """
    token = secrets.token_urlsafe(24)
    app = create_app(state, token=token)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    actual_port = port
    try:
        addrs = runner.addresses
        if addrs:
            actual_port = int(addrs[0][1])
    except Exception:
        pass

    show_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{show_host}:{actual_port}/?t={token}"
    dash = Dashboard(url=url, token=token, host=show_host, port=actual_port, runner=runner, app=app)

    if open_browser and not _is_headless():
        threading.Thread(target=_safe_open, args=(url,), daemon=True).start()

    log.info("панель открыта: %s", url)      # адрес с токеном — только в локальном логе
    return dash


# ── фейковое состояние для тестов и как образец реализации ────────────────────────
class FakeState:
    """Игрушечный `DashboardState` с заготовленными данными. Тесты подменяют поля и читают
    `actions` / `saved`. Настройки берёт из настоящей схемы, поэтому проверяет и её."""

    def __init__(self, cfg: Any = None) -> None:
        from ..config import Settings
        self.cfg = cfg or Settings(bot_token="123456:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                                   owner_id=42, llm_api_key="sk-abc0000000000000000000000000c4d5")
        self.actions: list[tuple[str, dict]] = []      # какие действия дошли до state
        self.saved: list[dict[str, Any]] = []          # какие изменения .env собрал settings_set
        self._status: dict[str, Any] = {
            "bot_name": "Оракул",
            "telegram": {"ok": True, "username": "baltia_oracle_bot", "id": 42},
            "conflict": {"active": False, "count": 0, "hint": ""},
            "uptime_sec": 3661.0,
            "last_update_ago_sec": 4.0,
            "model": {"fast": "deepseek-flash", "deep": "deepseek-v4-pro"},
            "cost": {"today": 0.0123, "month": 0.42, "calls": 128, "currency": "USD"},
            "balance": {"is_available": True, "balances": [{"currency": "USD", "total": 4.2}]},
            "twins": [],
            "single_instance_ok": True,
        }
        self._dialog = [{"role": "user", "content": "привет", "ts": 1_700_000_000},
                        {"role": "assistant", "content": "Привет! Чем помочь?", "ts": 1_700_000_005}]
        self._memory = [{"id": 1, "content": "Любит крепкий кофе", "category": "привычки"}]
        self._opinions = [{"id": 1, "topic": "утренние совещания", "stance": "лучше без них",
                           "reasons": "съедают фокус", "confidence": 70}]
        self._journal = [{"ts": 1_700_000_100, "content": "Спокойный день, разобрал идеи."}]
        self._reminders = [{"id": 5, "text": "Позвонить в банк", "when_local": "пн 29.09 12:00",
                            "repeat": "", "kind": "reminder"}]
        self._ideas = [{"id": 3, "title": "Мини-CRM для себя", "score": 7, "status": "new",
                        "content": "Хранить контакты и историю."}]
        self._projects = [{"id": 2, "name": "Дом", "goal": "ремонт кухни", "status": "active",
                           "tasks": [{"id": 9, "text": "выбрать плитку", "status": "todo"}]}]
        self._news = [{"title": "Событие дня", "source": "РБК", "ts": 1_700_000_200,
                       "url": "https://example.com/n", "summary": "коротко о главном"}]
        self._usage = {
            "total_cost": 0.42, "total_calls": 128, "prompt_tokens": 100000,
            "completion_tokens": 20000,
            "by_route": [{"route": "chat", "calls": 100, "cost": 0.30},
                         {"route": "deep", "calls": 28, "cost": 0.12}],
            "by_day": [{"day": "2026-09-28", "cost": 0.20, "calls": 60},
                       {"day": "2026-09-29", "cost": 0.22, "calls": 68}],
            "by_model": [{"model": "deepseek-flash", "calls": 100, "cost": 0.20},
                         {"model": "deepseek-v4-pro", "calls": 28, "cost": 0.22}],
            "days": 30, "since": "2026-08-31",
        }
        self._logs = [{"id": 1, "ts": 1_700_000_000.0, "level": "INFO",
                       "logger": "oracle.app", "message": "старт"},
                      {"id": 2, "ts": 1_700_000_010.0, "level": "WARNING",
                       "logger": "oracle.bot", "message": "Conflict от Telegram"}]

    async def status(self) -> dict:
        return self._status

    async def dialog(self, limit: int = 50) -> Any:
        return self._dialog[-limit:]

    async def memory(self) -> Any:
        return self._memory

    async def opinions(self) -> Any:
        return self._opinions

    async def journal(self) -> Any:
        return self._journal

    async def reminders(self) -> Any:
        return self._reminders

    async def ideas(self) -> Any:
        return self._ideas

    async def projects(self) -> Any:
        return self._projects

    async def news_recent(self) -> Any:
        return self._news

    async def usage(self, days: int = 30) -> Any:
        return dict(self._usage, days=days)

    async def logs(self, after_id: int = 0, level: str = "INFO") -> Any:
        import logging as _l
        minno = getattr(_l, str(level).upper(), _l.INFO)
        return [r for r in self._logs
                if r["id"] > int(after_id or 0)
                and getattr(_l, r["level"], _l.INFO) >= minno]

    async def settings_get(self) -> Any:
        return {"sections": sections(), "settings": schema_public(),
                "values": masked_values(self.cfg)}

    async def settings_set(self, form: dict) -> dict:
        errors: dict[str, str] = {}
        for key, raw in form.items():
            if get_setting(key) is None:
                continue
            ok, res = validate(key, raw)
            if not ok:
                errors[key] = res
        if errors:
            return {"ok": False, "changed": [], "restart_needed": False, "errors": errors}
        changed = to_env_updates(form, masked_values(self.cfg))
        self.saved.append(changed)
        return {"ok": True, "changed": list(changed.keys()),
                "restart_needed": bool(changed), "errors": {}}

    async def action(self, name: str, params: dict | None = None) -> dict:
        self.actions.append((name, dict(params or {})))
        return {"ok": True, "action": name}
