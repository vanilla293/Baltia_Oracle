"""Живучесть одного экземпляра: замок на папку, поиск чужих держателей токена, буфер лога.

Больная точка v3: у владельца бывает запущена СТАРАЯ копия бота с тем же токеном (первый архив
без OWNER_ID, старый проект «Пифия», второе окно, другой компьютер). Telegram отдаёт каждое
сообщение только одному опрашивающему — ответы прыгают между «ок» и «не настроен».

Лечим без убийства чужих процессов: (1) замок на ЭТУ папку не даёт запустить вторую копию отсюда;
(2) поиск (только чтение!) кто ещё держит этот токен — свои процессы `-m oracle` и «Пифию»
(`uvicorn backend.server:app`); (3) прямое сообщение владельцу, какое окно закрыть, и баннер на
дашборде. Ничего не завершаем — только читаем и подсказываем.

Модуль лёгкий: psutil импортируется лениво (без него поиск просто пуст), секреты в лог не попадают
(всё проходит через `oracle.bot.render.redact`).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import socket
import time
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from .bot.render import redact

log = logging.getLogger("oracle.runtime")

CONFLICT_WINDOW = 120.0     # конфликт считается «активным», если был не раньше стольких секунд назад
LOCK_NAME = "oracle.lock"   # имя файла-замка в папке данных
MAX_HOLDERS = 20            # больше держателей в выдачу не кладём — незачем

_psutil_warned = False      # «psutil не установлен» пишем в лог один раз на процесс


# ── буфер лога для дашборда ────────────────────────────────────────────────────
def _level_no(level: int | str) -> int:
    """Имя уровня («INFO») или число → число. Непонятное → INFO."""
    if isinstance(level, int):
        return level
    return getattr(logging, str(level).upper(), logging.INFO)


class LogBuffer(logging.Handler):
    """Последние N записей лога в памяти — для панели «журнал» на дашборде.

    Каждая запись получает строго возрастающий целочисленный id (дашборд опрашивает `after_id`,
    чтобы дотянуть только новое). Текст сообщения проходит через `redact`, поэтому токен бота и
    ключи в буфер не попадают. `emit` не бросает исключений — сломанная запись просто теряется.
    """

    def __init__(self, capacity: int = 3000, secrets: tuple[str, ...] | list[str] = ()) -> None:
        super().__init__()
        self._buf: deque[dict[str, Any]] = deque(maxlen=max(1, int(capacity)))
        self._secrets = tuple(s for s in secrets if s)
        self._id = 0
        self._lock = Lock()

    def set_secrets(self, secrets: tuple[str, ...] | list[str]) -> None:
        """Обновить список секретов (ключи узнаём после чтения .env)."""
        with self._lock:
            self._secrets = tuple(s for s in secrets if s)

    def _render(self, record: logging.LogRecord) -> str:
        """Только текст сообщения (+ трассировка исключения), без времени/уровня/имени —
        их отдаём отдельными полями. От внешнего форматтера намеренно не зависим."""
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(getattr(record, "msg", ""))
        if record.exc_info:
            try:
                msg = f"{msg}\n{logging.Formatter().formatException(record.exc_info)}"
            except Exception:
                pass
        return msg

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = redact(self._render(record), self._secrets)
            with self._lock:
                self._id += 1
                self._buf.append({
                    "id": self._id,
                    "ts": float(getattr(record, "created", time.time())),
                    "levelno": int(record.levelno),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": message,
                })
        except Exception:
            # обработчик лога не имеет права ронять приложение
            pass

    @staticmethod
    def _public(r: dict[str, Any]) -> dict[str, Any]:
        return {"id": r["id"], "ts": r["ts"], "level": r["level"],
                "logger": r["logger"], "message": r["message"]}

    def records(self, after_id: int = 0, min_level: int | str = "INFO",
                limit: int = 500) -> list[dict[str, Any]]:
        """Записи с id > after_id и уровнем ≥ min_level, по возрастанию id. Если их больше limit —
        отдаём последние limit (хвост), чтобы клиент двигал after_id вперёд."""
        minno = _level_no(min_level)
        after = int(after_id or 0)
        with self._lock:
            items = [r for r in self._buf if r["id"] > after and r["levelno"] >= minno]
        if limit and len(items) > limit:
            items = items[-int(limit):]
        return [self._public(r) for r in items]

    def errors(self, limit: int = 50) -> list[dict[str, Any]]:
        """Только предупреждения и ошибки (WARNING+)."""
        return self.records(after_id=0, min_level=logging.WARNING, limit=limit)


# ── замок на папку ─────────────────────────────────────────────────────────────
def _pid_alive(pid: int) -> bool:
    """Жив ли процесс с таким pid. psutil, если есть; иначе `os.kill(pid, 0)`."""
    try:
        import psutil
        return bool(psutil.pid_exists(int(pid)))
    except Exception:
        pass
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True     # процесс есть, просто чужой
    except Exception:
        return True
    return True


class InstanceLock:
    """Замок `data_dir/oracle.lock` = JSON {pid, started_at, host}. Не даёт запустить вторую копию
    ИЗ ЭТОЙ ЖЕ папки. Замок мёртвого процесса (pid не жив) забираем молча. На ошибках ввода-вывода
    не падаем — считаем, что заняли (лучше работать, чем не стартовать), и пишем в лог один раз.
    Годится как контекст-менеджер: `with InstanceLock(dir) as holder: ...` (holder=None — заняли мы).
    """

    def __init__(self, data_dir: str | os.PathLike, *, pid: int | None = None,
                 host: str | None = None, clock: Callable[[], float] | None = None,
                 is_alive: Callable[[int], bool] | None = None) -> None:
        self.dir = Path(data_dir)
        self.path = self.dir / LOCK_NAME
        self.pid = int(pid) if pid is not None else os.getpid()
        self.host = host if host is not None else _hostname()
        self._clock = clock or time.time
        self._is_alive = is_alive or _pid_alive
        self._held = False
        self._io_logged = False

    def _log_io(self, what: str) -> None:
        if not self._io_logged:
            self._io_logged = True
            log.warning("замок %s: не смог %s (%s) — работаю без него", self.path, what, self.dir)

    def _read(self) -> dict[str, Any] | None:
        try:
            if not self.path.is_file():
                return None
            data = json.loads(self.path.read_text("utf-8"))
        except Exception:
            self._log_io("прочитать")
            return None
        return data if isinstance(data, dict) and "pid" in data else None

    def _write(self) -> bool:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            payload = {"pid": self.pid, "started_at": float(self._clock()), "host": self.host}
            self.path.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
            return True
        except Exception:
            self._log_io("записать")
            return False

    def acquire(self) -> dict[str, Any] | None:
        """None — замок наш. Иначе dict живого чужого держателя (наш замок берём, мёртвый забираем)."""
        holder = self._read()
        if holder is not None:
            try:
                hpid = int(holder.get("pid"))
            except Exception:
                hpid = None
            if hpid is not None and hpid != self.pid and self._is_alive(hpid):
                return holder                       # чужой и живой — уступаем
            # либо наш собственный, либо замок мёртвого процесса — забираем себе
        if self._write():
            self._held = True
        return None

    def release(self) -> None:
        """Снять замок, если он наш. Чужой не трогаем. На ошибках не падаем."""
        try:
            holder = self._read()
            if holder is not None and int(holder.get("pid", -1)) == self.pid:
                self.path.unlink(missing_ok=True)
        except Exception:
            self._log_io("снять")
        finally:
            self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def __enter__(self) -> dict[str, Any] | None:
        return self.acquire()

    def __exit__(self, *exc: Any) -> bool:
        self.release()
        return False


# ── состояние процесса ──────────────────────────────────────────────────────────
class Runtime:
    """Живое состояние процесса, которое заполняет остальной код и читает дашборд/уведомления.

    Часы (`clock`) вынесены наружу ради детерминированных тестов.
    """

    def __init__(self, *, clock: Callable[[], float] | None = None,
                 started_at: float | None = None) -> None:
        self._clock = clock or time.time
        self.started_at: float = float(started_at) if started_at is not None else self._clock()
        self.env_path: Path | None = None
        self.telegram: dict[str, Any] | None = None          # {ok, username, id, error}
        self.conflict_at: float | None = None
        self.conflict_count: int = 0
        self.last_update_at: float | None = None
        self.twins: list[dict[str, Any]] = []                # результат последнего сканирования
        self.restart_reason: str | None = None
        self.single_instance_ok: bool = True
        self._restart = asyncio.Event()

    # — отметки, которые ставит остальной код —
    def note_conflict(self) -> None:
        """Telegram ответил Conflict на getUpdates — токен опрашивает кто-то ещё."""
        self.conflict_at = self._clock()
        self.conflict_count += 1

    def note_update(self) -> None:
        """Пришло обновление от Telegram — значит, сейчас опрашиваем мы."""
        self.last_update_at = self._clock()

    def note_telegram(self, info: dict[str, Any] | None) -> None:
        """Итог getMe: {ok, username, id} или {ok: False, error: ...}."""
        self.telegram = dict(info) if info else None

    def set_twins(self, twins: list[dict[str, Any]] | None) -> None:
        self.twins = list(twins or [])

    def set_env_path(self, path: str | os.PathLike | None) -> None:
        self.env_path = Path(path) if path else None

    # — перезапуск (дашборд просит перечитать настройки) —
    def request_restart(self, reason: str) -> None:
        """Идемпотентно: первую причину запоминаем, событие взводим один раз."""
        if not self._restart.is_set():
            self.restart_reason = reason
            self._restart.set()

    def restart_requested(self) -> bool:
        return self._restart.is_set()

    async def wait_restart(self) -> None:
        await self._restart.wait()

    # — снимок для дашборда/уведомлений —
    def status(self) -> dict[str, Any]:
        now = self._clock()
        conflict = {
            "ago_sec": (now - self.conflict_at) if self.conflict_at is not None else None,
            "count": self.conflict_count,
            "active": bool(self.conflict_at is not None and now - self.conflict_at < CONFLICT_WINDOW),
        }
        return {
            "uptime_sec": now - self.started_at,
            "telegram": self.telegram,
            "conflict": conflict,
            "last_update_ago_sec": (now - self.last_update_at) if self.last_update_at is not None else None,
            "twins": self.twins,
            "restart_reason": self.restart_reason,
            "single_instance_ok": self.single_instance_ok,
        }


# ── кто ещё держит токен (только чтение) ────────────────────────────────────────
def _hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "?"


def _load_psutil() -> Any:
    """psutil или None. «Нет psutil» пишем в лог один раз."""
    global _psutil_warned
    try:
        import psutil
        return psutil
    except Exception:
        if not _psutil_warned:
            _psutil_warned = True
            log.warning("psutil не установлен — не могу проверить, кто ещё опрашивает этот токен")
        return None


def _related_pids(self_pid: int, psutil_mod: Any) -> set[int]:
    """Свой pid + родитель + все потомки — их из выдачи исключаем."""
    excl = {int(self_pid)}
    if psutil_mod is None:
        return excl
    try:
        p = psutil_mod.Process(int(self_pid))
        try:
            excl.add(int(p.ppid()))
        except Exception:
            pass
        try:
            for c in p.children(recursive=True):
                excl.add(int(c.pid))
        except Exception:
            pass
    except Exception:
        pass
    return excl


def _proc_cmdline(proc: Any) -> list[str]:
    try:
        return [str(a) for a in (proc.cmdline() or [])]
    except Exception:
        return []


def _proc_cwd(proc: Any) -> str:
    try:
        return str(proc.cwd() or "")
    except Exception:
        return ""


def _proc_exe(proc: Any) -> str:
    try:
        return str(proc.exe() or "")
    except Exception:
        return ""


def _venv_parent(exe: str) -> str:
    """Из `<folder>/.venv/.../python` (или Windows `<folder>\\.venv\\Scripts\\python.exe`)
    вытащить `<folder>` — папку над .venv."""
    if not exe:
        return ""
    norm = exe.replace("\\", "/")
    low = norm.lower()
    for marker in ("/.venv/", "/venv/"):
        i = low.find(marker)
        if i > 0:
            return str(Path(norm[:i]))
    return ""


def _is_oracle_cmd(cmdline: list[str]) -> bool:
    """Процесс запущен как `python -m oracle` или скриптом `.../oracle/__main__.py`."""
    for i, arg in enumerate(cmdline):
        if arg == "-m" and i + 1 < len(cmdline):
            mod = cmdline[i + 1]
            if mod == "oracle" or mod.startswith("oracle."):
                return True
        a = arg.replace("\\", "/")
        if a.endswith("oracle/__main__.py"):
            return True
    return False


def _is_pythia_cmd(cmdline: list[str]) -> bool:
    """uvicorn поднимает Пифию строкой `backend.server:app`."""
    return any("backend.server:app" in a for a in cmdline)


def _oracle_token(folder: str) -> str:
    """BOT_TOKEN из .env указанной папки (через штатный разбор конфига). Пусто — не нашли."""
    if not folder:
        return ""
    try:
        from .config import _read_env
        return str(_read_env(Path(folder) / ".env").get("BOT_TOKEN", "") or "")
    except Exception:
        return ""


def _pythia_token(folder: str) -> str:
    """TG_BOT_TOKEN из <folder>/data/config_user.json. Пусто — файла нет или нет ключа."""
    if not folder:
        return ""
    path = Path(folder) / "data" / "config_user.json"
    try:
        if not path.is_file():
            return ""
        data = json.loads(path.read_text("utf-8"))
        return str(data.get("TG_BOT_TOKEN", "") or "") if isinstance(data, dict) else ""
    except Exception:
        return ""


def find_holders(token: str, *, self_pid: int | None = None,
                 clock: Callable[[], float] | None = None,
                 process_iter: Callable[[], Any] | None = None) -> list[dict[str, Any]]:
    """Кто ещё в системе может опрашивать этот токен. ТОЛЬКО ЧТЕНИЕ, ничего не завершаем.

    Возвращает список {pid, kind, folder, same_token, detail} (не больше MAX_HOLDERS). `kind` —
    "oracle" (наш бот в другой папке) или "pythia" (проект «Пифия» на uvicorn). `same_token` —
    держит ли он именно НАШ токен (его токен читается из .env / config_user.json той папки; сам
    токен в результат не кладём). Свой процесс, родителя и потомков исключаем. Без psutil — [].

    `process_iter` — для тестов: функция без аргументов, отдающая процессоподобные объекты
    (.pid, .cmdline(), .cwd(), .exe()). По умолчанию берётся psutil.process_iter.
    """
    _ = clock  # часы приняты ради единообразия контракта; в чистом чтении не нужны
    self_pid = int(self_pid) if self_pid is not None else os.getpid()

    psutil_mod: Any = None
    try:
        import psutil as psutil_mod  # для классов исключений и поиска родни
    except Exception:
        psutil_mod = None

    if process_iter is None:
        if psutil_mod is None:
            _load_psutil()      # один раз предупредит про отсутствие psutil
            return []

        def process_iter() -> Any:  # type: ignore[misc]
            return psutil_mod.process_iter(["pid", "name"])

    skip_exc: tuple[type[BaseException], ...] = (Exception,)
    if psutil_mod is not None:
        skip_exc = (getattr(psutil_mod, "AccessDenied", Exception),
                    getattr(psutil_mod, "NoSuchProcess", Exception),
                    getattr(psutil_mod, "ZombieProcess", Exception), Exception)

    exclude = _related_pids(self_pid, psutil_mod)
    token = str(token or "")

    out: list[dict[str, Any]] = []
    seen_folders: set[tuple[str, str]] = set()
    try:
        procs = process_iter()
    except Exception:
        log.warning("не смог перечислить процессы", exc_info=True)
        return []

    for proc in procs:
        try:
            pid = int(proc.pid)
            if pid in exclude:
                continue
            cmdline = _proc_cmdline(proc)
            if not cmdline:
                continue

            if _is_oracle_cmd(cmdline):
                kind = "oracle"
                folder = _proc_cwd(proc) or _venv_parent(_proc_exe(proc))
                other = _oracle_token(folder)
                detail = "Baltia Oracle (python -m oracle)"
            elif _is_pythia_cmd(cmdline):
                kind = "pythia"
                folder = _proc_cwd(proc) or _venv_parent(_proc_exe(proc))
                other = _pythia_token(folder)
                detail = "Pythia (uvicorn backend.server:app)"
            else:
                continue

            key = (kind, folder)
            if key in seen_folders:     # тот же twin в двух видах (лаунчер + дочерний) — один раз
                continue
            seen_folders.add(key)

            out.append({
                "pid": pid,
                "kind": kind,
                "folder": folder,
                "same_token": bool(token) and other == token,
                "detail": detail,
            })
            if len(out) >= MAX_HOLDERS:
                break
        except skip_exc:
            continue
    return out


def conflict_hint(holders: list[dict[str, Any]]) -> str:
    """Короткая подсказка владельцу: какое окно/приложение закрыть, чтобы токен освободился.
    Если среди найденных никто не держит НАШ токен — "" (значит, опрашивают, скорее всего, с
    другого компьютера; общий текст про это даёт `owner_warning`)."""
    same = [h for h in (holders or []) if h.get("same_token")]
    if not same:
        return ""
    lines = ["С этим ботом сейчас работает ещё одна программа — из-за неё часть сообщений теряется."]
    for h in same:
        folder = h.get("folder") or "?"
        if h.get("kind") == "pythia":
            lines.append(f"• «Пифия» в папке {folder}: убери там токен "
                         f"(data/config_user.json → TG_BOT_TOKEN) или заведи для меня отдельного "
                         f"бота у @BotFather.")
        else:
            lines.append(f"• Оракул в папке {folder}: закрой то окно или останови там процесс.")
    lines.append("Меня закрывать не нужно — как только закроешь остальные, я снова буду один.")
    return "\n".join(lines)


def owner_warning(holders: list[dict[str, Any]]) -> str:
    """Готовый текст прямого предупреждения владельцу. Нашли местного виновника — назовём его;
    не нашли — скажем, что токен, похоже, опрашивают с другого компьютера."""
    hint = conflict_hint(holders)
    if hint:
        return hint
    return ("Кто-то ещё опрашивает этого бота — похоже, с другого компьютера. Из-за этого часть "
            "сообщений и кнопок уходит туда, а не мне. Проверь, не запущен ли бот где-то ещё, "
            "или смени токен у @BotFather (/revoke) и впиши новый в BOT_TOKEN.")


async def warn_owner(notifier: Any, holders: list[dict[str, Any]]) -> bool:
    """Отправить владельцу прямое предупреждение о конфликте. Не роняет вызывающего; True — ушло."""
    if notifier is None:
        return False
    try:
        await notifier.send(owner_warning(holders))
        return True
    except Exception:
        log.warning("не смог предупредить владельца о конфликте токена", exc_info=True)
        return False


# ── запись настроек в .env (для панели) ──────────────────────────────────────────
_ENV_LINE = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=)(.*)$")


def _env_value(v: str) -> str:
    """Значение для строки .env: если есть пробел/кавычка/решётка — берём в двойные кавычки."""
    s = str(v)
    if s and (s != s.strip() or any(c in s for c in " #\"'") ):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def update_env(path: str | os.PathLike, updates: dict[str, str | None]) -> dict[str, Any]:
    """Аккуратно записать изменения в файл .env, сохранив комментарии и порядок строк.

    updates: {ENV_NAME: значение} — заменить/добавить строку; {ENV_NAME: None} — удалить строку.
    Существующие строки правятся на месте, новые дописываются в конец. Комментарии и пустые
    строки не трогаются. Файла нет — создаётся. → {ok, changed:[…], path}. Не роняет вызывающего:
    ошибка ввода-вывода возвращается как {ok: False, error}. Секреты в лог не пишутся.
    """
    updates = {k: v for k, v in (updates or {}).items() if k}
    p = Path(path)
    changed: list[str] = []
    try:
        try:
            text = p.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            text = ""
        except UnicodeDecodeError:
            text = p.read_text(encoding="cp1251", errors="replace")
        lines = text.splitlines()
        seen: set[str] = set()
        out: list[str] = []
        for line in lines:
            m = _ENV_LINE.match(line)
            if m and m.group(2) in updates:
                key = m.group(2)
                seen.add(key)
                val = updates[key]
                if val is None:
                    changed.append(key)
                    continue                      # строку удаляем
                new_line = f"{m.group(1)}{key}={_env_value(val)}"
                if new_line != line:
                    changed.append(key)
                out.append(new_line)
            else:
                out.append(line)
        # новые ключи (которых в файле не было) — в конец
        added = [(k, v) for k, v in updates.items() if k not in seen and v is not None]
        if added:
            if out and out[-1].strip():
                out.append("")
            for k, v in added:
                out.append(f"{k}={_env_value(v)}")
                changed.append(k)
        new_text = "\n".join(out)
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(new_text, encoding="utf-8")
        with contextlib.suppress(OSError):
            if os.name == "posix":
                os.chmod(p, 0o600)                # там бывают токен и ключи
        return {"ok": True, "changed": changed, "path": str(p)}
    except Exception as e:
        log.warning("не смог записать %s: %s", p, type(e).__name__)
        return {"ok": False, "error": f"{type(e).__name__}", "changed": changed, "path": str(p)}
