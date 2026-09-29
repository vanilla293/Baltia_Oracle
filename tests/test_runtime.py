"""Живучесть одного экземпляра: буфер лога, замок на папку, состояние процесса, поиск чужих
держателей токена. Ни одного настоящего процесса и ни одного обращения в сеть — всё на фейках."""
from __future__ import annotations

import asyncio
import json
import logging
import sys

import pytest

from oracle import runtime as rt
from oracle.runtime import (InstanceLock, LogBuffer, Runtime, conflict_hint,
                            find_holders, owner_warning, warn_owner, _related_pids)


# ── LogBuffer ──────────────────────────────────────────────────────────────────
def _logger(name: str, buf: LogBuffer) -> logging.Logger:
    lg = logging.getLogger(name)
    lg.handlers.clear()
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    lg.addHandler(buf)
    return lg


def test_logbuffer_ids_strictly_increasing_and_levels():
    buf = LogBuffer(capacity=100)
    lg = _logger("test.lb.levels", buf)
    lg.debug("d"); lg.info("i"); lg.warning("w"); lg.error("e")

    info_up = buf.records(min_level="INFO")
    assert [r["message"] for r in info_up] == ["i", "w", "e"]          # DEBUG отсеян
    ids = [r["id"] for r in info_up]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)            # строго возрастают, без повторов

    warn_up = buf.records(min_level="WARNING")
    assert [r["message"] for r in warn_up] == ["w", "e"]
    assert [r["message"] for r in buf.errors()] == ["w", "e"]          # errors() = WARNING+

    r0 = info_up[0]
    assert set(r0) == {"id", "ts", "level", "logger", "message"}       # публичная форма без levelno
    assert isinstance(r0["ts"], float) and r0["level"] == "INFO" and r0["logger"] == "test.lb.levels"


def test_logbuffer_after_id_and_numeric_level():
    buf = LogBuffer()
    lg = _logger("test.lb.after", buf)
    lg.info("one"); lg.info("two"); lg.info("three")
    first_id = buf.records()[0]["id"]
    rest = buf.records(after_id=first_id)
    assert [r["message"] for r in rest] == ["two", "three"]
    assert [r["message"] for r in buf.records(min_level=logging.WARNING)] == []   # число тоже понимает


def test_logbuffer_redacts_token_and_secrets():
    secret = "supersecret_api_key_value"
    buf = LogBuffer(secrets=(secret,))
    lg = _logger("test.lb.redact", buf)
    token = "123456789:" + "A" * 35                                   # вид токена бота
    lg.info("token=%s key=%s", token, secret)
    msg = buf.records()[0]["message"]
    assert token not in msg and secret not in msg
    assert "<token>" in msg and "<secret>" in msg


def test_logbuffer_capacity_evicts_oldest_keeps_ids():
    buf = LogBuffer(capacity=2)
    lg = _logger("test.lb.cap", buf)
    lg.info("a"); lg.info("b"); lg.info("c")
    recs = buf.records()
    assert [r["message"] for r in recs] == ["b", "c"]                 # «a» вытеснено
    assert [r["id"] for r in recs] == [2, 3]                          # id не переиспользуются


def test_logbuffer_limit_returns_tail():
    buf = LogBuffer()
    lg = _logger("test.lb.limit", buf)
    for i in range(10):
        lg.info("m%d", i)
    recs = buf.records(limit=3)
    assert [r["message"] for r in recs] == ["m7", "m8", "m9"]


def test_logbuffer_emit_never_raises_on_bad_record():
    buf = LogBuffer()
    lg = _logger("test.lb.bad", buf)
    lg.info("%d", "не число")                                         # форматирование упало бы
    assert buf.records(), "запись всё равно сохранена, без исключения"


def test_logbuffer_set_secrets_updates():
    buf = LogBuffer()
    buf.set_secrets(("later_secret_value",))
    lg = _logger("test.lb.setsec", buf)
    lg.info("x=%s", "later_secret_value")
    assert "<secret>" in buf.records()[0]["message"]


# ── InstanceLock ────────────────────────────────────────────────────────────────
def test_instancelock_acquire_release_roundtrip(tmp_path):
    lock = InstanceLock(tmp_path, pid=111, host="h1", clock=lambda: 1000.0,
                        is_alive=lambda p: True)
    assert lock.acquire() is None and lock.held
    data = json.loads((tmp_path / "oracle.lock").read_text("utf-8"))
    assert data == {"pid": 111, "started_at": 1000.0, "host": "h1"}
    lock.release()
    assert not (tmp_path / "oracle.lock").exists() and not lock.held


def test_instancelock_detects_live_holder(tmp_path):
    InstanceLock(tmp_path, pid=111, is_alive=lambda p: True).acquire()
    holder = InstanceLock(tmp_path, pid=222, is_alive=lambda p: True).acquire()
    assert holder is not None and holder["pid"] == 111                # чужой и живой — уступаем


def test_instancelock_takes_over_stale(tmp_path):
    InstanceLock(tmp_path, pid=111, is_alive=lambda p: True).acquire()
    taker = InstanceLock(tmp_path, pid=222, is_alive=lambda p: False)  # прежний pid мёртв
    assert taker.acquire() is None and taker.held
    assert json.loads((tmp_path / "oracle.lock").read_text())["pid"] == 222


def test_instancelock_reacquire_own_pid(tmp_path):
    a = InstanceLock(tmp_path, pid=111, is_alive=lambda p: True)
    a.acquire()
    b = InstanceLock(tmp_path, pid=111, is_alive=lambda p: True)       # тот же pid — забираем без вопросов
    assert b.acquire() is None


def test_instancelock_release_does_not_touch_foreign(tmp_path):
    InstanceLock(tmp_path, pid=444, is_alive=lambda p: True).acquire()
    InstanceLock(tmp_path, pid=555, is_alive=lambda p: True).release()  # не наш замок — не трогаем
    assert json.loads((tmp_path / "oracle.lock").read_text())["pid"] == 444


def test_instancelock_context_manager(tmp_path):
    with InstanceLock(tmp_path, pid=111, is_alive=lambda p: True) as holder:
        assert holder is None
        assert (tmp_path / "oracle.lock").exists()
    assert not (tmp_path / "oracle.lock").exists()                     # вышли — сняли


def test_instancelock_survives_io_error(tmp_path):
    afile = tmp_path / "not_a_dir"
    afile.write_text("i am a file")                                    # папку не создать поверх файла
    lock = InstanceLock(afile, pid=111, is_alive=lambda p: True)
    assert lock.acquire() is None and not lock.held                    # не заняли, но и не упали


def test_instancelock_corrupt_lock_is_taken_over(tmp_path):
    (tmp_path / "oracle.lock").write_text("{ битый json")
    lock = InstanceLock(tmp_path, pid=111, is_alive=lambda p: True)
    assert lock.acquire() is None and lock.held


# ── Runtime ─────────────────────────────────────────────────────────────────────
class _Clock:
    def __init__(self, v: float) -> None:
        self.v = v

    def __call__(self) -> float:
        return self.v


def test_runtime_status_math():
    clk = _Clock(1000.0)
    r = Runtime(clock=clk, started_at=1000.0)
    st = r.status()
    assert st["uptime_sec"] == 0.0 and st["conflict"]["count"] == 0
    assert st["conflict"]["active"] is False and st["telegram"] is None
    assert st["single_instance_ok"] is True and st["restart_reason"] is None

    clk.v = 1005.0
    r.note_conflict()
    clk.v = 1010.0
    r.note_update()
    st = r.status()
    assert st["uptime_sec"] == 10.0
    assert st["conflict"] == {"ago_sec": 5.0, "count": 1, "active": True}
    assert st["last_update_ago_sec"] == 0.0

    clk.v = 1200.0                                                     # прошло больше окна в 120 с
    assert r.status()["conflict"]["active"] is False
    assert r.status()["conflict"]["count"] == 1

    r.note_telegram({"ok": True, "username": "bot", "id": 42})
    r.set_twins([{"pid": 7, "kind": "oracle"}])
    r.single_instance_ok = False
    st = r.status()
    assert st["telegram"]["username"] == "bot" and st["twins"][0]["pid"] == 7
    assert st["single_instance_ok"] is False
    json.dumps(st)                                                     # снимок сериализуем


def test_runtime_request_restart_idempotent():
    r = Runtime()
    assert not r.restart_requested()
    r.request_restart("первая причина")
    r.request_restart("вторая причина")                               # запоминается только первая
    assert r.restart_requested() and r.restart_reason == "первая причина"
    assert r.status()["restart_reason"] == "первая причина"


async def test_runtime_wait_restart_event():
    r = Runtime()
    waiter = asyncio.ensure_future(r.wait_restart())
    await asyncio.sleep(0)
    assert not waiter.done()
    r.request_restart("go")
    await asyncio.wait_for(waiter, timeout=1.0)


# ── find_holders ─────────────────────────────────────────────────────────────────
class FakeProc:
    """Процессоподобный объект для инъекции в find_holders (как у psutil.Process)."""

    def __init__(self, pid: int, cmdline: list[str], cwd: str = "",
                 exe: str = "", cwd_raises: bool = False) -> None:
        self.pid = pid
        self._cmdline = cmdline
        self._cwd = cwd
        self._exe = exe
        self._cwd_raises = cwd_raises

    def cmdline(self) -> list[str]:
        return list(self._cmdline)

    def cwd(self) -> str:
        if self._cwd_raises:
            raise PermissionError("нет доступа")
        return self._cwd

    def exe(self) -> str:
        return self._exe


TOKEN = "123456789:" + "A" * 35


def _iter(*procs: FakeProc):
    return lambda: list(procs)


def test_find_holders_oracle_same_token(tmp_path):
    (tmp_path / ".env").write_text(f"BOT_TOKEN={TOKEN}\nOWNER_ID=1\n")
    proc = FakeProc(pid=770001, cmdline=["/usr/bin/python", "-m", "oracle"], cwd=str(tmp_path))
    out = find_holders(TOKEN, self_pid=770000, process_iter=_iter(proc))
    assert len(out) == 1
    h = out[0]
    assert h["pid"] == 770001 and h["kind"] == "oracle"
    assert h["folder"] == str(tmp_path) and h["same_token"] is True
    assert h["detail"] and TOKEN not in json.dumps(out)               # токен в выдачу не попал


def test_find_holders_oracle_different_token(tmp_path):
    (tmp_path / ".env").write_text("BOT_TOKEN=999:OTHERtokenXXXXXXXXXXXXXXXXXXXXXXXXXXXX\n")
    proc = FakeProc(pid=770002, cmdline=["python", "-m", "oracle"], cwd=str(tmp_path))
    out = find_holders(TOKEN, self_pid=770000, process_iter=_iter(proc))
    assert out[0]["same_token"] is False


def test_find_holders_oracle_by_script_path(tmp_path):
    (tmp_path / ".env").write_text(f"BOT_TOKEN={TOKEN}\n")
    proc = FakeProc(pid=770003, cmdline=["python", str(tmp_path / "oracle" / "__main__.py")],
                    cwd=str(tmp_path))
    out = find_holders(TOKEN, self_pid=770000, process_iter=_iter(proc))
    assert out and out[0]["kind"] == "oracle" and out[0]["same_token"] is True


def test_find_holders_oracle_folder_from_venv(tmp_path):
    (tmp_path / ".env").write_text(f"BOT_TOKEN={TOKEN}\n")
    exe = str(tmp_path / ".venv" / "bin" / "python")
    proc = FakeProc(pid=770004, cmdline=[exe, "-m", "oracle"], exe=exe, cwd_raises=True)
    out = find_holders(TOKEN, self_pid=770000, process_iter=_iter(proc))
    assert out and out[0]["folder"] == str(tmp_path) and out[0]["same_token"] is True


def test_find_holders_pythia_reads_config_user(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config_user.json").write_text(json.dumps({"TG_BOT_TOKEN": TOKEN}))
    proc = FakeProc(pid=770005,
                    cmdline=["python", "-m", "uvicorn", "backend.server:app", "--port", "8799"],
                    cwd=str(tmp_path))
    out = find_holders(TOKEN, self_pid=770000, process_iter=_iter(proc))
    assert len(out) == 1
    assert out[0]["kind"] == "pythia" and out[0]["same_token"] is True
    assert out[0]["folder"] == str(tmp_path)


def test_find_holders_pythia_without_config(tmp_path):
    proc = FakeProc(pid=770006, cmdline=["uvicorn", "backend.server:app"], cwd=str(tmp_path))
    out = find_holders(TOKEN, self_pid=770000, process_iter=_iter(proc))
    assert out[0]["kind"] == "pythia" and out[0]["same_token"] is False


def test_find_holders_excludes_own_pid(tmp_path):
    (tmp_path / ".env").write_text(f"BOT_TOKEN={TOKEN}\n")
    me = FakeProc(pid=770000, cmdline=["python", "-m", "oracle"], cwd=str(tmp_path))
    other = FakeProc(pid=770001, cmdline=["python", "-m", "oracle"], cwd=str(tmp_path))
    out = find_holders(TOKEN, self_pid=770000, process_iter=_iter(me, other))
    assert [h["pid"] for h in out] == [770001]                        # свой pid отброшен


def test_find_holders_ignores_unrelated_and_bad_procs(tmp_path):
    good = FakeProc(pid=770007, cmdline=["python", "-m", "oracle"], cwd=str(tmp_path))
    unrelated = FakeProc(pid=770008, cmdline=["bash", "-c", "sleep 1"], cwd=str(tmp_path))

    class Boom:
        pid = 770009

        def cmdline(self):
            raise RuntimeError("сломан")

    (tmp_path / ".env").write_text(f"BOT_TOKEN={TOKEN}\n")
    out = find_holders(TOKEN, self_pid=770000, process_iter=_iter(good, unrelated, Boom()))
    assert [h["pid"] for h in out] == [770007]                        # только валидный держатель


def test_find_holders_dedupes_same_folder(tmp_path):
    (tmp_path / ".env").write_text(f"BOT_TOKEN={TOKEN}\n")
    launcher = FakeProc(pid=770010, cmdline=["python", "-m", "oracle"], cwd=str(tmp_path))
    child = FakeProc(pid=770011, cmdline=["python", "-m", "oracle"], cwd=str(tmp_path))
    out = find_holders(TOKEN, self_pid=770000, process_iter=_iter(launcher, child))
    assert len(out) == 1                                              # тот же twin — одна запись


def test_find_holders_no_psutil_returns_empty(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)                 # import psutil → ImportError
    rt._psutil_warned = False
    assert find_holders(TOKEN, self_pid=770000) == []


def test_find_holders_caps_at_20(tmp_path):
    (tmp_path / ".env").write_text(f"BOT_TOKEN={TOKEN}\n")
    procs = []
    for i in range(30):
        d = tmp_path / f"p{i}"
        d.mkdir()
        (d / ".env").write_text(f"BOT_TOKEN={TOKEN}\n")
        procs.append(FakeProc(pid=771000 + i, cmdline=["python", "-m", "oracle"], cwd=str(d)))
    out = find_holders(TOKEN, self_pid=770000, process_iter=lambda: procs)
    assert len(out) == 20


# ── _related_pids ─────────────────────────────────────────────────────────────────
class _FakeChild:
    def __init__(self, pid: int) -> None:
        self.pid = pid


class _FakeProcRel:
    def __init__(self, ppid: int, children: list[_FakeChild]) -> None:
        self._ppid = ppid
        self._children = children

    def ppid(self) -> int:
        return self._ppid

    def children(self, recursive: bool = False) -> list[_FakeChild]:
        return self._children


class _FakePsutil:
    def __init__(self, proc) -> None:
        self._proc = proc

    def Process(self, pid):
        if self._proc is None:
            raise RuntimeError("нет такого")
        return self._proc


def test_related_pids_collects_parent_and_children():
    ps = _FakePsutil(_FakeProcRel(ppid=10, children=[_FakeChild(20), _FakeChild(21)]))
    assert _related_pids(5, ps) == {5, 10, 20, 21}


def test_related_pids_without_psutil():
    assert _related_pids(5, None) == {5}


def test_related_pids_handles_process_error():
    assert _related_pids(5, _FakePsutil(None)) == {5}                 # Process() кинул → только свой pid


# ── conflict_hint / owner_warning / warn_owner ───────────────────────────────────
def test_conflict_hint_oracle_names_folder():
    holders = [{"pid": 1, "kind": "oracle", "folder": "/home/u/Baltia_Oracle",
                "same_token": True, "detail": "x"}]
    hint = conflict_hint(holders)
    assert "/home/u/Baltia_Oracle" in hint and "закрой" in hint.lower()


def test_conflict_hint_pythia_mentions_token_and_config():
    holders = [{"pid": 2, "kind": "pythia", "folder": "/home/u/pythia",
                "same_token": True, "detail": "x"}]
    hint = conflict_hint(holders)
    assert "Пифия" in hint and "/home/u/pythia" in hint
    assert "config_user.json" in hint or "TG_BOT_TOKEN" in hint


def test_conflict_hint_empty_when_no_same_token():
    assert conflict_hint([{"pid": 1, "kind": "oracle", "folder": "/x", "same_token": False}]) == ""
    assert conflict_hint([]) == ""


def test_owner_warning_falls_back_to_other_pc():
    text = owner_warning([{"pid": 1, "kind": "oracle", "folder": "/x", "same_token": False}])
    assert "компьютер" in text.lower()


def test_owner_warning_uses_hint_when_local_culprit():
    holders = [{"pid": 1, "kind": "oracle", "folder": "/home/u/Baltia_Oracle", "same_token": True}]
    assert owner_warning(holders) == conflict_hint(holders)


async def test_warn_owner_sends_via_notifier(notifier):
    holders = [{"pid": 1, "kind": "oracle", "folder": "/home/u/Baltia_Oracle", "same_token": True}]
    assert await warn_owner(notifier, holders) is True
    assert notifier.texts() and "Baltia_Oracle" in notifier.texts()[0]


async def test_warn_owner_without_notifier_is_safe():
    assert await warn_owner(None, []) is False


async def test_warn_owner_swallows_send_error():
    class Boom:
        async def send(self, *a, **k):
            raise RuntimeError("телега упала")

    assert await warn_owner(Boom(), []) is False                     # не пробрасывает исключение
