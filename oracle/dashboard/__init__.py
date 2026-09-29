"""Локальная веб-панель Оракула: красивая страница на 127.0.0.1, где видно всё и можно менять
настройки без правки файлов.

Публичное:
  • `start_dashboard(state, *, host, port, open_browser)` — поднять сервер, вернуть `Dashboard`
    (`.url`, `.token`, `await .stop()`);
  • `DashboardState` — протокол «провайдера состояния», который реализует интегратор;
  • `create_app`, `Dashboard`, `FakeState` — для интеграции и тестов;
  • `PAGE` — сама HTML-страница.
"""
from __future__ import annotations

from .page import PAGE
from .server import Dashboard, DashboardState, FakeState, create_app, start_dashboard

__all__ = [
    "start_dashboard",
    "Dashboard",
    "DashboardState",
    "FakeState",
    "create_app",
    "PAGE",
]
