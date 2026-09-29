"""Файлы: чтение папок в рамках корней, поиск, чтение текста vs бинарника, запрет выхода за
пределы, запись только в рабочую папку, отправка файла, заметки и зеркало идей в markdown.

Никакой сети: всё на временных папках tmp_path. Настройки files_roots / files_workspace
подставляются во frozen-Settings через object.__setattr__ (после интеграции это станет
обычным dataclasses.replace)."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from oracle.tools import base as tb
from oracle.tools import files as fs
from oracle.tools.base import Services, ToolContext


# ── помощники ────────────────────────────────────────────────────────────────
def _mkcfg(base, roots, workspace):
    """Копия cfg с подставленными files_roots / files_workspace (обходим frozen)."""
    c = replace(base)
    object.__setattr__(c, "files_roots", tuple(Path(r) for r in roots))
    object.__setattr__(c, "files_workspace", Path(workspace))
    return c


def _ctx(cfg, db, fake_llm, notifier, roots, workspace):
    c = _mkcfg(cfg, roots, workspace)
    return ToolContext(cfg=c, db=db, llm=fake_llm, services=Services(notifier=notifier))


@pytest.fixture
def env(cfg, db, fake_llm, notifier, clock, tmp_path):
    """Готовое окружение: корень (root), рабочая папка (ws=root/Oracle) и ctx с ними."""
    root = tmp_path / "root"
    root.mkdir()
    ws = root / "Oracle"
    ctx = _ctx(cfg, db, fake_llm, notifier, (root,), ws)
    return SimpleNamespace(ctx=ctx, root=root, ws=ws, notifier=notifier, tmp=tmp_path)


async def call(ctx, name, /, **args) -> dict:
    return json.loads(await tb.dispatch(name, args, ctx))


async def ok(ctx, name, /, **args) -> dict:
    r = await call(ctx, name, **args)
    assert r.get("ok") is True, r
    return r


async def fail(ctx, name, /, **args) -> dict:
    r = await call(ctx, name, **args)
    assert r.get("ok") is False, r
    return r


# ── реестр и диспетчеризация ─────────────────────────────────────────────────
def test_tools_registered():
    for name in ("fs_list", "fs_find", "fs_read", "fs_info", "fs_send", "fs_write", "save_note"):
        assert name in tb.REGISTRY, name
        t = tb.REGISTRY[name]
        assert t.description and "properties" in t.parameters


async def test_dispatch_clean(env):
    (env.root / "readme.txt").write_text("привет", encoding="utf-8")
    r = await ok(env.ctx, "fs_list", path=str(env.root))
    assert r["path"] == str(env.root)
    # неизвестный аргумент отбрасывается диспетчером, вызов не падает
    r2 = await ok(env.ctx, "fs_list", path=str(env.root), bogus=123)
    assert r2["count"] == r["count"]


# ── fs_list ──────────────────────────────────────────────────────────────────
async def test_fs_list_entries_and_hidden(env):
    (env.root / "a.txt").write_text("aaa", encoding="utf-8")
    (env.root / "b.log").write_text("bbbb", encoding="utf-8")
    (env.root / ".secret").write_text("x", encoding="utf-8")
    (env.root / "sub").mkdir()

    r = await ok(env.ctx, "fs_list", path=str(env.root))
    names = [e["name"] for e in r["entries"]]
    assert ".secret" not in names                     # скрытые прячем по умолчанию
    assert names[0] == "sub"                          # папки первыми
    assert set(names) == {"sub", "a.txt", "b.log"}
    e = next(x for x in r["entries"] if x["name"] == "a.txt")
    assert e["kind"] == "file" and e["size"] == 3 and e["modified"]
    assert next(x for x in r["entries"] if x["name"] == "sub")["kind"] == "dir"

    r2 = await ok(env.ctx, "fs_list", path=str(env.root), show_hidden=True)
    assert ".secret" in [e["name"] for e in r2["entries"]]


async def test_fs_list_default_is_first_root(env):
    (env.root / "here.txt").write_text("1", encoding="utf-8")
    r = await ok(env.ctx, "fs_list")                  # path по умолчанию — первый корень
    assert r["path"] == str(env.root.resolve())
    assert "here.txt" in [e["name"] for e in r["entries"]]


async def test_fs_list_cap_500(env):
    big = env.root / "many"
    big.mkdir()
    for i in range(510):
        (big / f"f{i:04d}.txt").write_text("x", encoding="utf-8")
    r = await ok(env.ctx, "fs_list", path=str(big))
    assert r["count"] == fs.LIST_CAP == 500
    assert r["total"] == 510
    assert "note" in r


async def test_fs_list_not_a_dir(env):
    p = env.root / "file.txt"
    p.write_text("hi", encoding="utf-8")
    r = await fail(env.ctx, "fs_list", path=str(p))
    assert "не папка" in r["error"]


# ── fs_find ──────────────────────────────────────────────────────────────────
async def test_fs_find_substring_recursive(env):
    (env.root / "docs").mkdir()
    (env.root / "docs" / "Резюме_2026.txt").write_text("cv", encoding="utf-8")
    (env.root / "otchet.md").write_text("x", encoding="utf-8")
    (env.root / "photo.png").write_bytes(b"\x89PNG")
    r = await ok(env.ctx, "fs_find", query="резюме", path=str(env.root))
    assert r["count"] == 1
    assert r["matches"][0]["name"] == "Резюме_2026.txt"
    assert Path(r["matches"][0]["path"]).exists()     # путь можно передать дальше


async def test_fs_find_glob(env):
    (env.root / "a.pdf").write_bytes(b"%PDF-1.4")
    (env.root / "b.pdf").write_bytes(b"%PDF-1.4")
    (env.root / "c.txt").write_text("x", encoding="utf-8")
    r = await ok(env.ctx, "fs_find", query="*.pdf", path=str(env.root))
    assert {m["name"] for m in r["matches"]} == {"a.pdf", "b.pdf"}


async def test_fs_find_truncation_note(env):
    d = env.root / "logs"
    d.mkdir()
    for i in range(60):
        (d / f"log{i:03d}.txt").write_text("x", encoding="utf-8")
    r = await ok(env.ctx, "fs_find", query="log", path=str(d), limit=50)
    assert r["count"] == 50
    assert r.get("truncated") is True and "note" in r


async def test_fs_find_nothing(env):
    (env.root / "a.txt").write_text("x", encoding="utf-8")
    r = await ok(env.ctx, "fs_find", query="нетуничего", path=str(env.root))
    assert r["count"] == 0 and "note" in r


async def test_fs_find_max_dirs_bounded(env, monkeypatch):
    monkeypatch.setattr(fs, "FIND_MAX_DIRS", 3)
    # дерево глубже трёх папок — обход прервётся с пометкой
    d = env.root
    for i in range(6):
        d = d / f"d{i}"
        d.mkdir()
    (d / "target.txt").write_text("x", encoding="utf-8")
    r = await ok(env.ctx, "fs_find", query="target", path=str(env.root))
    assert r.get("truncated") is True


# ── fs_read ──────────────────────────────────────────────────────────────────
async def test_fs_read_text(env):
    p = env.root / "note.md"
    p.write_text("# Заголовок\nтекст", encoding="utf-8")
    r = await ok(env.ctx, "fs_read", path=str(p))
    assert r["text"] == "# Заголовок\nтекст"
    assert r["chars"] == len("# Заголовок\nтекст")


async def test_fs_read_cp1251(env):
    p = env.root / "win.txt"
    p.write_bytes("Привет из cp1251".encode("cp1251"))
    r = await ok(env.ctx, "fs_read", path=str(p))
    assert "Привет" in r["text"]


async def test_fs_read_binary_refusal(env):
    p = env.root / "blob.bin"
    p.write_bytes(b"MZ\x00\x00\x90\x00binary\x00stuff")
    r = await fail(env.ctx, "fs_read", path=str(p))
    assert "не текстовый" in r["error"] or "двоичн" in r["error"]


async def test_fs_read_office_refusal(env):
    p = env.root / "doc.docx"
    p.write_bytes(b"PK\x03\x04 zip-based office")
    r = await fail(env.ctx, "fs_read", path=str(p))
    assert "офисн" in r["error"] and "fs_send" in r["error"]


async def test_fs_read_truncation(env):
    p = env.root / "long.txt"
    p.write_text("я" * 5000, encoding="utf-8")
    r = await ok(env.ctx, "fs_read", path=str(p), max_chars=1000)
    assert r["chars"] == 1000
    assert r.get("truncated") is True and "note" in r


async def test_fs_read_missing(env):
    r = await fail(env.ctx, "fs_read", path=str(env.root / "нет.txt"))
    assert "нет" in r["error"].lower()


# ── запрет выхода за пределы корней ──────────────────────────────────────────
async def test_escape_dotdot_denied(env):
    outside = env.tmp / "outside_secret.txt"
    outside.write_text("СЕКРЕТ", encoding="utf-8")
    # ../outside_secret.txt относительно корня выводит наружу
    r = await fail(env.ctx, "fs_read", path="../outside_secret.txt")
    assert "вне разрешённых папок" in r["error"]
    assert "СЕКРЕТ" not in r["error"]


async def test_escape_absolute_outside_denied(env):
    outside = env.tmp / "elsewhere"
    outside.mkdir()
    (outside / "x.txt").write_text("x", encoding="utf-8")
    r = await fail(env.ctx, "fs_list", path=str(outside))
    assert "вне разрешённых папок" in r["error"]


async def test_escape_symlink_out_denied(env):
    secret = env.tmp / "vault"
    secret.mkdir()
    (secret / "passwords.txt").write_text("hunter2", encoding="utf-8")
    link = env.root / "shortcut"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("симлинки недоступны")
    # прямое чтение через симлинк наружу — отказ
    r = await fail(env.ctx, "fs_read", path="shortcut/passwords.txt")
    assert "вне разрешённых папок" in r["error"]
    # и в листинге корня такой симлинк не показывается
    r2 = await ok(env.ctx, "fs_list", path=str(env.root))
    assert "shortcut" not in [e["name"] for e in r2["entries"]]


async def test_symlink_out_skipped_in_find(env):
    secret = env.tmp / "vault2"
    secret.mkdir()
    (secret / "hidden_target.txt").write_text("x", encoding="utf-8")
    try:
        (env.root / "lnk").symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("симлинки недоступны")
    r = await ok(env.ctx, "fs_find", query="hidden_target", path=str(env.root))
    assert r["count"] == 0                             # находка за пределами корня отброшена


# ── fs_write: только в рабочую папку ──────────────────────────────────────────
async def test_fs_write_creates_in_workspace(env):
    r = await ok(env.ctx, "fs_write", path="sub/dir/hello.txt", content="привет")
    written = env.ws / "sub" / "dir" / "hello.txt"
    assert written.exists() and written.read_text(encoding="utf-8") == "привет"
    assert r["path"] == str(written)


async def test_fs_write_append(env):
    await ok(env.ctx, "fs_write", path="a.txt", content="раз\n")
    r = await ok(env.ctx, "fs_write", path="a.txt", content="два\n", append=True)
    assert r["appended"] is True
    assert (env.ws / "a.txt").read_text(encoding="utf-8") == "раз\nдва\n"


async def test_fs_write_outside_workspace_denied(env):
    # корень доступен для чтения, но писать в него нельзя (не рабочая папка)
    r = await fail(env.ctx, "fs_write", path=str(env.root / "x.txt"), content="нет")
    assert "рабочую папку" in r["error"]
    assert not (env.root / "x.txt").exists()


async def test_fs_write_escape_denied(env):
    r = await fail(env.ctx, "fs_write", path="../../escape.txt", content="нет")
    assert "рабочую папку" in r["error"]


# ── fs_send ──────────────────────────────────────────────────────────────────
async def test_fs_send_attaches_outitem(env):
    p = env.root / "report.pdf"
    p.write_bytes(b"%PDF-1.4 data")
    r = await ok(env.ctx, "fs_send", path=str(p), caption="вот отчёт")
    assert r["filename"] == "report.pdf" and r["size"] == len(b"%PDF-1.4 data")
    assert len(env.ctx.outbox) == 1
    item = env.ctx.outbox[0]
    assert item.kind == "file" and item.filename == "report.pdf"
    assert item.data == b"%PDF-1.4 data" and item.text == "вот отчёт"


async def test_fs_send_size_limit(env):
    p = env.root / "huge.bin"
    with open(p, "wb") as f:                            # разрежённый файл нужного размера
        f.truncate(fs.SEND_MAX + 1)
    r = await fail(env.ctx, "fs_send", path=str(p))
    assert "слишком большой" in r["error"]
    assert env.ctx.outbox == []


async def test_fs_send_outside_denied(env):
    outside = env.tmp / "out.bin"
    outside.write_bytes(b"x")
    r = await fail(env.ctx, "fs_send", path=str(outside))
    assert "вне разрешённых папок" in r["error"]


# ── save_note ────────────────────────────────────────────────────────────────
async def test_save_note_markdown(env):
    r = await ok(env.ctx, "save_note", title="Список покупок", text="молоко\nхлеб",
                 tags="дом, быт")
    path = Path(r["path"])
    assert path.parent == env.ws / "notes"
    assert path.name == "список-покупок.md"
    body = path.read_text(encoding="utf-8")
    assert body.startswith("# Список покупок")
    assert "#дом #быт" in body and "молоко" in body
    assert r["created"] is True


async def test_save_note_appends_same_title(env):
    await ok(env.ctx, "save_note", title="Дневник", text="запись раз")
    r = await ok(env.ctx, "save_note", title="Дневник", text="запись два")
    assert r["created"] is False
    body = (env.ws / "notes" / "дневник.md").read_text(encoding="utf-8")
    assert "запись раз" in body and "запись два" in body
    assert body.count("# Дневник") == 1               # заголовок один


async def test_save_note_then_send(env):
    r = await ok(env.ctx, "save_note", title="Заметка", text="текст")
    # путь заметки можно отдать в fs_send (лежит в рабочей папке — в корнях чтения)
    s = await ok(env.ctx, "fs_send", path=r["path"])
    assert s["filename"] == "заметка.md"


async def test_save_note_empty_rejected(env):
    await fail(env.ctx, "save_note", title="  ", text="есть текст")
    await fail(env.ctx, "save_note", title="Ок", text="   ")


# ── mirror_idea ──────────────────────────────────────────────────────────────
async def test_mirror_idea_writes_markdown(env):
    idea = {"id": 7, "title": "Кофейня у вокзала", "score": 6, "status": "new",
            "tags": "общепит", "evaluation": "Трафик есть, маржа тонкая.",
            "deep_evaluation": "Проверить поток тележкой.", "content": "Кофе навынос для пассажиров."}
    p = await fs.mirror_idea(env.ctx, idea)
    assert p is not None and p.exists()
    assert p == env.ws / "ideas" / "7-кофейня-у-вокзала.md"
    body = p.read_text(encoding="utf-8")
    assert "# Кофейня у вокзала" in body
    assert "**Оценка:** 6/10" in body
    assert "Трафик есть" in body and "Проверить поток" in body
    assert "Кофе навынос" in body


async def test_mirror_idea_dedup_on_title_change(env):
    await fs.mirror_idea(env.ctx, {"id": 3, "title": "Старое имя", "content": "текст"})
    p = await fs.mirror_idea(env.ctx, {"id": 3, "title": "Новое имя", "content": "текст"})
    files = sorted(x.name for x in (env.ws / "ideas").glob("3-*.md"))
    assert files == ["3-новое-имя.md"]                 # старый файл убран, дублей нет
    assert p.name == "3-новое-имя.md"


async def test_mirror_idea_safe_on_bad_input(env):
    assert await fs.mirror_idea(env.ctx, {}) is None            # нет id
    assert await fs.mirror_idea(env.ctx, {"id": None}) is None
    assert await fs.mirror_idea(env.ctx, "не словарь") is None  # type: ignore[arg-type]


async def test_mirror_idea_never_raises(cfg, db, fake_llm, notifier, clock, tmp_path):
    # рабочая папка «под файлом» — mkdir невозможен; зеркало обязано вернуть None, не бросив
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    ctx = _ctx(cfg, db, fake_llm, notifier, (tmp_path,), blocker / "ws")
    assert await fs.mirror_idea(ctx, {"id": 1, "title": "Идея", "content": "x"}) is None


# ── корень и рабочая папка совпадают (как в дефолтной конфигурации допустимо) ──
async def test_root_equals_workspace(cfg, db, fake_llm, notifier, clock, tmp_path):
    ctx = _ctx(cfg, db, fake_llm, notifier, (tmp_path,), tmp_path)
    await ok(ctx, "fs_write", path="notes/x.md", content="ок")
    r = await ok(ctx, "fs_list", path=str(tmp_path))
    assert "notes" in [e["name"] for e in r["entries"]]
    rd = await ok(ctx, "fs_read", path="notes/x.md")
    assert rd["text"] == "ок"
