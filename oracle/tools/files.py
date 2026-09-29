"""Файлы владельца: бот смотрит в его папки и держит заметки и идеи настоящими файлами.

Владелец просил — «пусть лазит по папкам». Здесь инструменты для этого, но с жёсткими рамками:

  • читать (`fs_list`, `fs_find`, `fs_read`, `fs_info`, `fs_send`) можно только внутри
    `files_roots` (по умолчанию — домашняя папка) или внутри рабочей папки `files_workspace`;
  • писать (`fs_write`, `save_note`, `mirror_idea`) можно только внутри `files_workspace`
    (по умолчанию ~/Oracle) — она создаётся при первой записи;
  • путь проверяется ПОСЛЕ `resolve()`: «..», абсолютный путь наружу и симлинк, ведущий за
    пределы папок, — всё это отклоняется понятной ошибкой; наружу бот не ходит никогда.

Бинарные файлы не читаются (картинки, архивы, exe), офисные форматы (pdf/docx/xlsx) — пока
тоже: их можно только отправить файлом (`fs_send`). Обход дерева в `fs_find` ограничен по числу
папок и времени, чтобы огромное дерево не подвесило бота.
"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .base import OutItem, ToolContext, tool

log = logging.getLogger("oracle.tools.files")

# ── ограничения ────────────────────────────────────────────────────────────────
LIST_CAP = 500                       # сколько записей папки максимум отдаём
FIND_CAP = 50                        # находок в fs_find по умолчанию
FIND_MAX_DIRS = 4000                 # сколько папок максимум обходим
FIND_MAX_SECONDS = 3.0               # и сколько секунд
SEND_MAX = 45 * 1024 * 1024          # 45 МБ — предел вложения в Telegram (лимит бота ~50)
READ_MAX_CHARS = 8000                # знаков текста по умолчанию в fs_read
READ_CHARS_HARD = 200_000            # верхний предел max_chars
TEXT_READ_BYTES = 5 * 1024 * 1024    # больше этого текстовый файл целиком не читаем
BINARY_SNIFF = 8192                  # по скольким первым байтам решаем, бинарный ли файл
NOTE_MAX = 20_000                    # длина текста заметки/записи
WRITE_MAX = 2 * 1024 * 1024          # предел содержимого fs_write

# офисные форматы: не текст, читать их пока не умеем — только отправить файлом
OFFICE_EXT = frozenset({".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                        ".odt", ".ods", ".odp", ".pages", ".numbers", ".key"})
OFFICE_NOTE = ("не умею читать бинарные офисные форматы (pdf, docx, xlsx и т.п.) — "
               "могу отправить этот файл тебе целиком через fs_send")
BINARY_NOTE = ("это не текстовый файл (похоже на двоичный) — прочитать не могу; "
               "если нужно, отправлю файлом через fs_send")

_SLUG_DROP = re.compile(r"[^\w\s-]", re.U)
_SLUG_DASH = re.compile(r"[\s_]+", re.U)


# ── настройки и корни ───────────────────────────────────────────────────────────
def _roots(cfg: Any) -> list[Path]:
    """Папки, в которых боту разрешено ЧИТАТЬ (files_roots; по умолчанию — домашняя)."""
    roots = getattr(cfg, "files_roots", None) or (Path.home(),)
    out: list[Path] = []
    for r in roots:
        try:
            out.append(Path(r).expanduser())
        except (TypeError, ValueError):
            continue
    return out or [Path.home()]


def _workspace(cfg: Any) -> Path:
    """Папка, в которую боту разрешено ПИСАТЬ (files_workspace; по умолчанию ~/Oracle)."""
    ws = getattr(cfg, "files_workspace", None) or (Path.home() / "Oracle")
    return Path(ws).expanduser()


def _read_bases(cfg: Any) -> list[Path]:
    """Все базы, внутри которых чтение разрешено: корни + рабочая папка (без дублей)."""
    bases = [_safe_resolve(r) for r in _roots(cfg)]
    ws = _safe_resolve(_workspace(cfg))
    if not any(_within(ws, b) for b in bases):
        bases.append(ws)
    return bases


def _safe_resolve(p: Path) -> Path:
    """resolve() без падений: несуществующий хвост разрешается как есть, симлинки — по цели."""
    try:
        return p.resolve()
    except (OSError, RuntimeError, ValueError):
        return p.absolute()


def _within(path: Path, base: Path) -> bool:
    """path совпадает с base или лежит внутри него (обе стороны уже resolve-нуты)."""
    try:
        return path == base or path.is_relative_to(base)
    except (ValueError, OSError):
        return False


def _deny_read(raw: str, cfg: Any) -> ValueError:
    where = ", ".join(str(b) for b in _read_bases(cfg))
    return ValueError(f"путь «{raw}» вне разрешённых папок — смотреть я могу только в: {where}")


def _resolve_read(cfg: Any, path: Any) -> Path:
    """Путь для чтения → абсолютный resolve-нутый путь внутри разрешённых папок (иначе ValueError).

    Относительный путь считается от первого корня. resolve() проходит по симлинкам, поэтому
    ссылка наружу или «..» дадут путь за пределами баз — и будут отклонены."""
    raw = str(path if path is not None else ".").strip() or "."
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = _roots(cfg)[0] / p
    real = _safe_resolve(p)
    if not any(_within(real, b) for b in _read_bases(cfg)):
        raise _deny_read(raw, cfg)
    return real


def _resolve_write(cfg: Any, path: Any) -> Path:
    """Путь для записи → абсолютный путь внутри рабочей папки (иначе ValueError)."""
    raw = str(path or "").strip()
    if not raw:
        raise ValueError("не указан путь файла для записи")
    p = Path(raw).expanduser()
    ws = _workspace(cfg)
    if not p.is_absolute():
        p = ws / p
    real = _safe_resolve(p)
    ws_real = _safe_resolve(ws)
    if not _within(real, ws_real):
        raise ValueError(f"писать я могу только в рабочую папку {ws_real} — путь «{raw}» вне её")
    return real


# ── мелочи ───────────────────────────────────────────────────────────────────
def _tz(cfg: Any) -> Any:
    return getattr(cfg, "tz", None)


def _fmt_mtime(ts: float, tz: Any) -> str:
    try:
        dt = datetime.fromtimestamp(ts, tz) if tz else datetime.fromtimestamp(ts)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def _hsize(n: int) -> str:
    """Человекочитаемый размер: 1536 → '1.5 КБ', 900 → '900 Б'."""
    x = float(max(0, n))
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if x < 1024 or unit == "ТБ":
            return f"{int(x)} {unit}" if unit == "Б" else f"{x:.1f} {unit}"
        x /= 1024
    return f"{x:.1f} ТБ"


def _slug(text: Any, maxlen: int = 50) -> str:
    t = str(text or "").strip().lower().replace("ё", "е")
    t = _SLUG_DROP.sub("", t)
    t = _SLUG_DASH.sub("-", t)
    t = re.sub(r"-{2,}", "-", t).strip("-")
    return t[:maxlen].strip("-")


def _kind(p: Path) -> str:
    try:
        if p.is_symlink() and not p.exists():
            return "symlink"
        if p.is_dir():
            return "dir"
        if p.is_file():
            return "file"
    except OSError:
        return "other"
    return "other"


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "on", "да", "y"}


def _as_int(v: Any, default: int) -> int:
    try:
        if v is None or isinstance(v, bool):
            return default
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default


def _ctrl_ratio_ok(s: str) -> bool:
    """Мало ли управляющих символов в начале — иначе это не текст (замаскированный бинарник)."""
    head = s[:BINARY_SNIFF]
    if not head:
        return True
    ctrl = sum(1 for ch in head if ord(ch) < 9 or 13 < ord(ch) < 32)
    return ctrl / len(head) <= 0.15


def _decode_text(data: bytes, *, may_truncate: bool = False) -> str | None:
    """Байты → текст или None, если это двоичный файл. Пробуем utf-8 (с запасом на обрыв
    многобайтового символа в конце, если читали не целиком) и cp1251 (частая русская кодировка)."""
    if b"\x00" in data[:BINARY_SNIFF]:
        return None
    cuts = (0, 1, 2, 3) if may_truncate else (0,)
    for cut in cuts:
        chunk = data[: len(data) - cut] if cut else data
        try:
            s = chunk.decode("utf-8")
        except UnicodeDecodeError:
            continue
        return s if _ctrl_ratio_ok(s) else None
    try:
        s = data.decode("cp1251")
    except UnicodeDecodeError:
        return None
    return s if _ctrl_ratio_ok(s) else None


def _entry(p: Path, tz: Any) -> dict:
    try:
        st = p.stat()
        size = st.st_size
        mtime = st.st_mtime
    except OSError:
        size, mtime = 0, 0.0
    return {"name": p.name, "kind": "dir" if p.is_dir() else "file",
            "size": int(size), "modified": _fmt_mtime(mtime, tz), "path": str(p)}


# ── обработчики (синхронная работа с ФС — в потоке) ──────────────────────────────
def _do_list(base: Path, show_hidden: bool, tz: Any, bases: list[Path]) -> dict:
    if not base.exists():
        raise ValueError(f"папки нет: {base}")
    if not base.is_dir():
        raise ValueError(f"это не папка, а файл: {base} — читай через fs_read или отправь fs_send")
    entries: list[dict] = []
    total = 0
    try:
        with os.scandir(base) as it:
            for de in it:
                name = de.name
                if not show_hidden and name.startswith("."):
                    continue
                child = Path(de.path)
                # симлинк, ведущий за пределы разрешённых папок, — пропускаем (наружу не показываем)
                if de.is_symlink() and not any(_within(_safe_resolve(child), b) for b in bases):
                    continue
                total += 1
                if len(entries) < LIST_CAP:
                    entries.append(_entry(child, tz))
    except OSError as e:
        raise ValueError(f"не смог прочитать папку {base}: {e}") from None
    entries.sort(key=lambda e: (e["kind"] != "dir", e["name"].lower()))
    out: dict[str, Any] = {"ok": True, "path": str(base), "count": len(entries), "total": total,
                           "entries": entries}
    if total > len(entries):
        out["note"] = f"показал первые {len(entries)} из {total} — уточни папку или поищи через fs_find"
    return out


def _do_find(base: Path, query: str, limit: int, roots: list[Path], show_hidden: bool, tz: Any) -> dict:
    q = str(query or "").strip()
    if not q:
        raise ValueError("что искать? пустой запрос")
    if not base.exists() or not base.is_dir():
        raise ValueError(f"негде искать — нет папки: {base}")
    is_glob = any(ch in q for ch in "*?[")
    ql = q.lower()
    matches: list[dict] = []
    dirs_seen = 0
    truncated = False
    reason = ""
    start = time.monotonic()
    stack: list[Path] = [base]
    while stack:
        cur = stack.pop()
        dirs_seen += 1
        if dirs_seen > FIND_MAX_DIRS:
            truncated, reason = True, "просмотрел слишком много папок"
            break
        if time.monotonic() - start > FIND_MAX_SECONDS:
            truncated, reason = True, "поиск занял слишком долго"
            break
        try:
            with os.scandir(cur) as it:
                for de in it:
                    name = de.name
                    if not show_hidden and name.startswith("."):
                        continue
                    try:
                        is_dir = de.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if is_dir:
                        stack.append(Path(de.path))
                        continue
                    hit = (fnmatch.fnmatch(name.lower(), ql) if is_glob else ql in name.lower())
                    if not hit:
                        continue
                    child = Path(de.path)
                    # находка должна остаться в пределах корней (симлинк наружу — мимо)
                    if de.is_symlink() and not any(_within(_safe_resolve(child), _safe_resolve(r))
                                                   for r in roots):
                        continue
                    matches.append(_entry(child, tz))
                    if len(matches) >= limit:
                        truncated, reason = True, "нашёл больше, чем просил — показал первые"
                        break
        except OSError:
            continue
        if len(matches) >= limit:
            break
    out: dict[str, Any] = {"ok": True, "query": q, "path": str(base), "count": len(matches),
                           "matches": matches}
    if truncated:
        out["truncated"] = True
        out["note"] = f"{reason}; уточни запрос или папку"
    elif not matches:
        out["note"] = "ничего не нашёл — проверь имя или папку"
    return out


def _do_read(path: Path, max_chars: int) -> dict:
    if not path.exists():
        raise ValueError(f"файла нет: {path}")
    if path.is_dir():
        raise ValueError(f"это папка, а не файл: {path} — смотри содержимое через fs_list")
    if path.suffix.lower() in OFFICE_EXT:
        raise ValueError(OFFICE_NOTE)
    try:
        size = path.stat().st_size
    except OSError as e:
        raise ValueError(f"не смог прочитать файл: {e}") from None
    to_read = min(size, TEXT_READ_BYTES)
    partial = size > TEXT_READ_BYTES
    try:
        with open(path, "rb") as f:
            data = f.read(to_read)
    except OSError as e:
        raise ValueError(f"не смог прочитать файл: {e}") from None
    text = _decode_text(data, may_truncate=partial)
    if text is None:
        raise ValueError(BINARY_NOTE)
    truncated = partial
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    out: dict[str, Any] = {"ok": True, "path": str(path), "size": int(size),
                           "chars": len(text), "text": text}
    if truncated:
        out["truncated"] = True
        out["note"] = "показал не весь файл — при необходимости попроси ещё через max_chars или fs_send"
    return out


def _do_info(path: Path, tz: Any) -> dict:
    if not path.exists() and not path.is_symlink():
        raise ValueError(f"нет такого пути: {path}")
    try:
        st = path.stat()
    except OSError as e:
        raise ValueError(f"не смог получить сведения: {e}") from None
    return {"ok": True, "path": str(path), "name": path.name, "kind": _kind(path),
            "is_symlink": path.is_symlink(), "size": int(st.st_size), "size_h": _hsize(st.st_size),
            "modified": _fmt_mtime(st.st_mtime, tz), "ext": path.suffix.lower()}


def _do_read_bytes(path: Path) -> bytes:
    if not path.exists():
        raise ValueError(f"файла нет: {path}")
    if not path.is_file():
        raise ValueError(f"это не файл: {path}")
    try:
        size = path.stat().st_size
    except OSError as e:
        raise ValueError(f"не смог прочитать файл: {e}") from None
    if size > SEND_MAX:
        raise ValueError(f"файл слишком большой для отправки ({_hsize(size)}) — предел {_hsize(SEND_MAX)}")
    try:
        return path.read_bytes()
    except OSError as e:
        raise ValueError(f"не смог прочитать файл: {e}") from None


def _do_write(path: Path, content: str, append: bool) -> dict:
    data = str(content if content is not None else "")
    if len(data.encode("utf-8", "replace")) > WRITE_MAX:
        raise ValueError(f"слишком длинное содержимое — предел {_hsize(WRITE_MAX)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    mode = "a" if append else "w"
    try:
        with open(path, mode, encoding="utf-8") as f:
            f.write(data)
    except OSError as e:
        raise ValueError(f"не смог записать файл: {e}") from None
    return {"ok": True, "path": str(path), "bytes": len(data.encode("utf-8", "replace")),
            "appended": bool(append), "existed": existed}


def _do_save_note(notes_dir: Path, title: str, text: str, tags: list[str], stamp: str) -> dict:
    notes_dir.mkdir(parents=True, exist_ok=True)
    slug = _slug(title) or "note"
    path = notes_dir / f"{slug}.md"
    created = not path.exists()
    parts: list[str] = []
    if created:
        parts.append(f"# {title.strip()}\n")
        if tags:
            parts.append(" ".join(f"#{t}" for t in tags) + "\n")
    parts.append(f"\n## {stamp}\n")
    if tags and not created:
        parts.append(" ".join(f"#{t}" for t in tags) + "\n")
    parts.append(f"\n{text.strip()}\n")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("".join(parts))
    except OSError as e:
        raise ValueError(f"не смог сохранить заметку: {e}") from None
    return {"ok": True, "path": str(path), "title": title.strip(), "created": created,
            "note": "заметка сохранена в рабочей папке; отправить файлом — fs_send с этим путём"}


def _clean_tags(v: Any) -> list[str]:
    if not v:
        return []
    items = [str(x) for x in v] if isinstance(v, (list, tuple, set)) else re.split(r"[,;\n]", str(v))
    out: list[str] = []
    for t in items:
        t = _slug(t, 40)
        if t and t not in out:
            out.append(t)
    return out[:10]


# ── инструменты для модели ───────────────────────────────────────────────────
@tool("fs_list",
      "Посмотреть содержимое папки владельца: файлы и подпапки с размером и датой изменения. "
      "path — путь к папке (по умолчанию домашняя, «.»). Смотреть можно только внутри разрешённых "
      "папок. Скрытые файлы (с точки) по умолчанию не показываются. Пути в ответе можно передавать "
      "дальше в fs_read, fs_find, fs_send.",
      {"path": {"type": "string", "description": "путь к папке, по умолчанию домашняя («.»)"},
       "show_hidden": {"type": "boolean", "description": "показывать скрытые файлы (с точки), по умолчанию нет"}},
      keep="head")
async def t_fs_list(ctx: ToolContext, *, path: str = ".", show_hidden: Any = False) -> dict:
    base = _resolve_read(ctx.cfg, path)
    return await asyncio.to_thread(_do_list, base, _as_bool(show_hidden), _tz(ctx.cfg),
                                   _read_bases(ctx.cfg))


@tool("fs_find",
      "Найти файлы по имени в папках владельца. query — часть имени (без учёта регистра) или маска "
      "вроде «*.pdf», «отчет*». path — где искать (по умолчанию домашняя папка). Обход ограничен по "
      "времени и числу папок, поэтому в огромном дереве покажет часть с пометкой. Полный путь находки "
      "передавай в fs_read или fs_send.",
      {"query": {"type": "string", "description": "часть имени файла или маска: «*.pdf», «резюме»"},
       "path": {"type": "string", "description": "папка для поиска, по умолчанию домашняя"},
       "limit": {"type": "integer", "description": "сколько находок максимум, по умолчанию 50"},
       "show_hidden": {"type": "boolean", "description": "искать и в скрытых, по умолчанию нет"}},
      required=["query"], keep="head")
async def t_fs_find(ctx: ToolContext, *, query: str, path: str = ".", limit: Any = None,
                    show_hidden: Any = False) -> dict:
    base = _resolve_read(ctx.cfg, path)
    lim = max(1, min(500, _as_int(limit, FIND_CAP)))
    return await asyncio.to_thread(_do_find, base, query, lim, _roots(ctx.cfg),
                                   _as_bool(show_hidden), _tz(ctx.cfg))


@tool("fs_read",
      "Прочитать текстовый файл владельца (код, заметки, txt, md, csv, json и т.п.). path — путь к "
      "файлу. max_chars — сколько знаков вернуть (по умолчанию 8000; большой файл вернётся началом с "
      "пометкой). Картинки, архивы и офисные форматы (pdf, docx, xlsx) прочитать нельзя — их можно "
      "только отправить владельцу через fs_send.",
      {"path": {"type": "string", "description": "путь к текстовому файлу"},
       "max_chars": {"type": "integer", "description": "сколько знаков вернуть, по умолчанию 8000"}},
      required=["path"])
async def t_fs_read(ctx: ToolContext, *, path: str, max_chars: Any = None) -> dict:
    real = _resolve_read(ctx.cfg, path)
    mc = max(100, min(READ_CHARS_HARD, _as_int(max_chars, READ_MAX_CHARS)))
    return await asyncio.to_thread(_do_read, real, mc)


@tool("fs_info",
      "Сведения о файле или папке: тип, размер, когда изменён. path — путь.",
      {"path": {"type": "string", "description": "путь к файлу или папке"}},
      required=["path"])
async def t_fs_info(ctx: ToolContext, *, path: str) -> dict:
    real = _resolve_read(ctx.cfg, path)
    return await asyncio.to_thread(_do_info, real, _tz(ctx.cfg))


@tool("fs_send",
      "Отправить файл владельцу в Telegram (когда он просит «пришли мне этот файл» или файл нужен "
      "целиком: pdf, картинка, архив, документ). path — путь к файлу. Файлы больше 45 МБ отправить "
      "нельзя.",
      {"path": {"type": "string", "description": "путь к файлу для отправки"},
       "caption": {"type": "string", "description": "подпись к файлу (необязательно)"}},
      required=["path"])
async def t_fs_send(ctx: ToolContext, *, path: str, caption: str = "") -> dict:
    real = _resolve_read(ctx.cfg, path)
    data = await asyncio.to_thread(_do_read_bytes, real)
    cap = str(caption or "").strip()
    ctx.outbox.append(OutItem(kind="file", filename=real.name, data=data, text=cap))
    return {"ok": True, "sent": True, "path": str(real), "filename": real.name,
            "size": len(data), "size_h": _hsize(len(data)),
            "note": "файл отправлен владельцу"}


@tool("fs_write",
      "Записать текстовый файл. Писать можно ТОЛЬКО в рабочую папку бота (files_workspace, по "
      "умолчанию ~/Oracle) — за её пределы записать нельзя. path — путь (относительный считается от "
      "рабочей папки), нужные подпапки создаются сами. append=true — дописать в конец, иначе перезаписать.",
      {"path": {"type": "string", "description": "путь файла в рабочей папке"},
       "content": {"type": "string", "description": "что записать"},
       "append": {"type": "boolean", "description": "дописать в конец (иначе перезаписать)"}},
      required=["path", "content"])
async def t_fs_write(ctx: ToolContext, *, path: str, content: str, append: Any = False) -> dict:
    real = _resolve_write(ctx.cfg, path)
    return await asyncio.to_thread(_do_write, real, content, _as_bool(append))


@tool("save_note",
      "Сохранить заметку владельца настоящим markdown-файлом в рабочей папке (notes/). Вызывай, когда "
      "он просит «запиши», «сохрани заметку», «сделай список» — то, что он захочет потом открыть файлом. "
      "Одноимённые заметки дописываются в тот же файл с датой. title — заголовок, text — содержимое, "
      "tags — теги через запятую (необязательно). Путь можно потом отправить через fs_send.",
      {"title": {"type": "string", "description": "заголовок заметки"},
       "text": {"type": "string", "description": "текст заметки"},
       "tags": {"type": "string", "description": "теги через запятую (необязательно)"}},
      required=["title", "text"])
async def t_save_note(ctx: ToolContext, *, title: str, text: str, tags: Any = "") -> dict:
    t = str(title or "").strip()
    body = str(text or "").strip()
    if not t:
        raise ValueError("у заметки нет заголовка")
    if not body:
        raise ValueError("заметка пустая — нечего сохранять")
    if len(body) > NOTE_MAX:
        body = body[: NOTE_MAX - 1].rstrip() + "…"
    notes_dir = _workspace(ctx.cfg) / "notes"
    stamp = ctx.now_local().strftime("%Y-%m-%d %H:%M")
    return await asyncio.to_thread(_do_save_note, notes_dir, t, body, _clean_tags(tags), stamp)


# ── зеркало идей в markdown (зовёт ideas.py после сохранения / глубокого разбора) ──
def _idea_markdown(idea: dict) -> str:
    title = str(idea.get("title") or "Идея").strip()
    parts = [f"# {title}", ""]
    score = idea.get("score")
    if score is not None:
        parts += [f"**Оценка:** {score}/10", ""]
    status = idea.get("status")
    if status:
        parts += [f"**Статус:** {status}", ""]
    tags = idea.get("tags")
    if tags:
        parts += [f"**Теги:** {tags}", ""]
    ev = str(idea.get("evaluation") or "").strip()
    if ev:
        parts += ["## Оценка", "", ev, ""]
    deep = str(idea.get("deep_evaluation") or "").strip()
    if deep:
        parts += ["## Глубокий разбор", "", deep, ""]
    content = str(idea.get("content") or "").strip()
    if content:
        parts += ["## Идея", "", content, ""]
    return "\n".join(parts).rstrip() + "\n"


def _do_mirror_idea(ideas_dir: Path, idea: dict) -> Path:
    ideas_dir.mkdir(parents=True, exist_ok=True)
    iid = idea.get("id")
    slug = _slug(idea.get("title")) or "idea"
    path = ideas_dir / f"{iid}-{slug}.md"
    # прежний файл этой идеи мог называться иначе (заголовок сменился) — уберём дубли по id
    try:
        for old in ideas_dir.glob(f"{iid}-*.md"):
            if old != path:
                old.unlink()
    except OSError:
        pass
    path.write_text(_idea_markdown(idea), encoding="utf-8")
    return path


async def mirror_idea(ctx: ToolContext, idea: dict) -> Path | None:
    """Отразить идею markdown-файлом files_workspace/ideas/<id>-<slug>.md (заголовок, оценка,
    глубокий разбор, текст). Зовётся из ideas.py после сохранения и глубокого разбора. Никогда не
    бросает исключение в вызывающего — на любой сбой просто возвращает None."""
    try:
        if not isinstance(idea, dict) or idea.get("id") is None:
            return None
        ideas_dir = _workspace(ctx.cfg) / "ideas"
        return await asyncio.to_thread(_do_mirror_idea, ideas_dir, dict(idea))
    except Exception:  # зеркало не должно ронять сохранение идеи
        log.warning("не смог отразить идею #%s в файл", idea.get("id") if isinstance(idea, dict) else "?",
                    exc_info=True)
        return None
