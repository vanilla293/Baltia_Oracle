"""Регрессии починки запуска и упаковки (группа startup).

Покрывает: снятие вебхука перед опросом (иначе бот «глухой» на 409), различение конфликта
«вебхук» и «второй поллер» в TwinHint, предупреждение о нечисловом OWNER_ID, закреплённые версии
зависимостей и отказ упаковщика класть в архив .env без OWNER_ID.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import logging
import re
from pathlib import Path

import pytest

from oracle import config
from oracle.app import TwinHint, clear_webhook

ROOT = Path(__file__).resolve().parent.parent


# ── A1: снятие вебхука перед опросом ────────────────────────────────────────────
class _Bot:
    def __init__(self, boom: bool = False) -> None:
        self.calls: list[dict] = []
        self.boom = boom

    async def delete_webhook(self, drop_pending_updates: bool = True) -> bool:
        self.calls.append({"drop_pending_updates": drop_pending_updates})
        if self.boom:
            raise RuntimeError("Telegram недоступен")
        return True


async def test_clear_webhook_called_without_dropping_updates():
    bot = _Bot()
    await clear_webhook(bot)
    assert bot.calls == [{"drop_pending_updates": False}]     # накопленные апдейты не теряем


async def test_clear_webhook_swallows_errors(caplog):
    bot = _Bot(boom=True)
    with caplog.at_level(logging.WARNING, logger="oracle"):
        await clear_webhook(bot)                              # ошибка не должна ронять запуск
    assert bot.calls and any("вебхук" in r.getMessage() for r in caplog.records)


def test_main_clears_webhook_before_polling():
    """Порядок в main: снять вебхук — строго до start_polling (иначе снятие бесполезно)."""
    src = (ROOT / "oracle" / "app.py").read_text(encoding="utf-8")
    assert "clear_webhook(bot)" in src
    assert src.index("clear_webhook(bot)") < src.index("dp.start_polling(")


# ── A5: конфликт вебхука vs второго поллера ─────────────────────────────────────
def _conflict_record(detail: str) -> logging.LogRecord:
    return logging.LogRecord("aiogram.dispatcher", logging.ERROR, __file__, 1,
                             "Failed to fetch updates - %s: %s", ("TelegramConflictError", detail), None)


def test_twin_hint_webhook_vs_second_copy(caplog):
    now = [1000.0]
    with caplog.at_level(logging.WARNING, logger="oracle"):
        f = TwinHint(clock=lambda: now[0])
        # текст ошибки вебхука: "...can't use getUpdates method while webhook is active..."
        assert f.filter(_conflict_record(
            "Conflict: can't use getUpdates method while webhook is active")) is False
        now[0] += 700
        f2 = TwinHint(clock=lambda: now[0])
        assert f2.filter(_conflict_record("terminated by other getUpdates request")) is False
    msgs = [r.getMessage() for r in caplog.records]
    webhook_hints = [m for m in msgs if "вебхук" in m]
    twin_hints = [m for m in msgs if "запущен дважды" in m]
    assert len(webhook_hints) == 1 and "deleteWebhook" in webhook_hints[0]
    assert len(twin_hints) == 1
    # подсказки не путаются: у вебхука нет «запущен дважды», у второй копии нет «вебхук»
    assert "запущен дважды" not in webhook_hints[0]
    assert "вебхук" not in twin_hints[0]


# ── A15: нечисловой OWNER_ID → предупреждение, а не молчаливый режим настройки ───
def _load(monkeypatch, tmp_path, owner: str | None):
    for k in ("BOT_TOKEN", "DEEPSEEK_API_KEY", "LLM_API_KEY", "OWNER_ID"):
        monkeypatch.delenv(k, raising=False)
    if owner is not None:
        monkeypatch.setenv("OWNER_ID", owner)
    return config.load(tmp_path / "none.env")     # реальный .env не читаем


def test_nonnumeric_owner_id_warns(monkeypatch, tmp_path):
    c = _load(monkeypatch, tmp_path, "@myusername")
    assert c.owner_id == 0
    warns = [w for w in c.warnings if "OWNER_ID" in w and "не число" in w]
    assert warns, c.warnings
    assert any("не число" in p for p in c.problems())


def test_display_name_owner_id_warns(monkeypatch, tmp_path):
    c = _load(monkeypatch, tmp_path, "Александр Иванов")
    assert c.owner_id == 0
    assert any("не число" in w for w in c.warnings)


def test_numeric_owner_id_no_warning(monkeypatch, tmp_path):
    c = _load(monkeypatch, tmp_path, "123456789")
    assert c.owner_id == 123456789
    assert not any("OWNER_ID" in w for w in c.warnings)


def test_mixed_owner_id_takes_number_no_warning(monkeypatch, tmp_path):
    c = _load(monkeypatch, tmp_path, "garbage, 456")       # число есть — не жалуемся
    assert c.owner_id == 456
    assert not any("не число" in w for w in c.warnings)


def test_empty_owner_id_no_nonnumber_warning(monkeypatch, tmp_path):
    c = _load(monkeypatch, tmp_path, None)                 # пусто — это не «не число», это просто не задан
    assert c.owner_id == 0
    assert not any("не число" in w for w in c.warnings)


def test_zero_owner_id_no_nonnumber_warning(monkeypatch, tmp_path):
    c = _load(monkeypatch, tmp_path, "0")                  # 0 — это число, отдельная жалоба не нужна
    assert c.owner_id == 0
    assert not any("не число" in w for w in c.warnings)


# ── A26: версии зависимостей закреплены и совпадают с установленными ─────────────
def _parse_requirements() -> dict[str, tuple[str, str]]:
    """{имя: (оператор, версия)} из requirements.txt (комментарии и пустые строки пропущены)."""
    out: dict[str, tuple[str, str]] = {}
    for raw in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*(==|>=|~=)\s*([A-Za-z0-9_.\-]+)", line)
        assert m, f"строка requirements без версии: {raw!r}"
        out[m.group(1).lower()] = (m.group(2), m.group(3))
    return out


def test_requirements_pinned_and_installed():
    reqs = _parse_requirements()
    # код-библиотеки закреплены точно (==) и стоят ровно этой версии — свежая установка воспроизводима
    exact = ["aiogram", "httpx", "httpcore", "aiohttp", "psutil", "aiosqlite", "feedparser",
             "python-dateutil", "python-dotenv", "edge-tts", "ddgs", "telethon"]
    for name in exact:
        assert name in reqs, f"{name} пропал из requirements.txt"
        op, ver = reqs[name]
        assert op == "==", f"{name} должен быть закреплён точно (==), а не {op}"
        installed = importlib.metadata.version(name)
        assert installed == ver, f"{name}: в requirements {ver}, в окружении {installed}"
    # база часовых поясов — с нижней границей (её обновлять можно и нужно), но не голая
    assert "tzdata" in reqs and reqs["tzdata"][0] in {">=", "~="}


# ── упаковщик: отказ класть в архив .env без OWNER_ID ───────────────────────────
def _pkg():
    spec = importlib.util.spec_from_file_location("oracle_package_script",
                                                  ROOT / "scripts" / "package.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_owner_id_from_env_text():
    pkg = _pkg()
    assert pkg.owner_id_from_env_text("BOT_TOKEN=x\nOWNER_ID=123456\n") == 123456
    assert pkg.owner_id_from_env_text("OWNER_ID=@name\n") == 0
    assert pkg.owner_id_from_env_text("OWNER_ID=\n") == 0
    assert pkg.owner_id_from_env_text("OWNER_ID=0\n") == 0
    assert pkg.owner_id_from_env_text("OWNER_ID=  foo, 456 \n") == 456
    assert pkg.owner_id_from_env_text("no owner here at all\n") == 0
    assert pkg.owner_id_from_env_text('OWNER_ID="789"\n') == 789


def test_ensure_env_shippable_refuses_empty_owner(tmp_path):
    pkg = _pkg()
    env = tmp_path / ".env"
    env.write_text("BOT_TOKEN=1234567890:AAE\nDEEPSEEK_API_KEY=sk-x\nOWNER_ID=\n", encoding="utf-8")
    with pytest.raises(pkg.PackageError):
        pkg.ensure_env_shippable(env)


def test_ensure_env_shippable_refuses_nonnumeric_owner(tmp_path):
    pkg = _pkg()
    env = tmp_path / ".env"
    env.write_text("BOT_TOKEN=1234567890:AAE\nOWNER_ID=@name\n", encoding="utf-8")
    with pytest.raises(pkg.PackageError):
        pkg.ensure_env_shippable(env)


def test_ensure_env_shippable_requires_token(tmp_path):
    pkg = _pkg()
    env = tmp_path / ".env"
    env.write_text("OWNER_ID=123\nBOT_TOKEN=\n", encoding="utf-8")
    with pytest.raises(pkg.PackageError):
        pkg.ensure_env_shippable(env)


def test_ensure_env_shippable_missing_file(tmp_path):
    pkg = _pkg()
    with pytest.raises(pkg.PackageError):
        pkg.ensure_env_shippable(tmp_path / "nope.env")


def test_ensure_env_shippable_ok(tmp_path):
    pkg = _pkg()
    env = tmp_path / ".env"
    env.write_text("BOT_TOKEN=1234567890:AAE\nOWNER_ID=987654321\n", encoding="utf-8")
    assert pkg.ensure_env_shippable(env) == 987654321


def test_build_archive_with_env_refuses_and_writes_nothing(tmp_path, monkeypatch):
    """--with-env при пустом OWNER_ID: PackageError ДО сборки, файл-архив не создаётся."""
    pkg = _pkg()
    fake_root = tmp_path / "repo"
    fake_root.mkdir()
    (fake_root / ".env").write_text("BOT_TOKEN=1234567890:AAE\nOWNER_ID=\n", encoding="utf-8")
    monkeypatch.setattr(pkg, "ROOT", fake_root)
    out = tmp_path / "dist" / "baltia_oracle.zip"
    with pytest.raises(pkg.PackageError):
        pkg.build_archive(out, with_env=True)
    assert not out.exists()
