"""Markdown модели → HTML Telegram, нарезка длинных ответов, плоский текст для запасного пути.

Telegram понимает лишь несколько тегов (b, i, u, s, code, pre, a, blockquote) и отвергает
сообщение целиком, если теги кривые. Поэтому: код, ссылки, адреса и @упоминания прячутся
в заглушки, всё остальное экранируется, разметка превращается в теги только парами и только
на границах слов (snake_case и URL не трогаются), а итог проверяется — если теги всё же
разъехались, уходит просто экранированный текст.

`split_message` режет исходный markdown (а не HTML): по пустым строкам, потом по строкам,
по концам предложений, по пробелам и лишь в крайнем случае — посередине. Блок кода,
разрезанный между сообщениями, закрывается в конце одного куска и открывается в начале
следующего — каждый кусок самодостаточен.
"""
from __future__ import annotations

import re
from typing import Callable

ALLOWED_TAGS = frozenset({"b", "i", "u", "s", "code", "pre", "a", "blockquote"})
LIMIT = 3500                     # запас до 4096: эмодзи в UTF-16 занимают по 2 единицы

# ── экранирование ────────────────────────────────────────────────────────────
def escape(s: str) -> str:
    """& < > → сущности (для текста внутри HTML Telegram)."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attr(s: str) -> str:
    return escape(s).replace('"', "&quot;")


# ── заглушки: то, что нельзя трогать разметкой ───────────────────────────────
_PH_A, _PH_B = "", ""
_PH = re.compile("(\\d+)")


class _Store:
    """Прячет готовые куски (HTML или сырой текст) за заглушки и возвращает их на место."""

    def __init__(self) -> None:
        self.items: list[str] = []

    def put(self, value: str) -> str:
        self.items.append(value)
        return f"{_PH_A}{len(self.items) - 1}{_PH_B}"

    def restore(self, s: str) -> str:
        for _ in range(4):            # заглушка может сидеть внутри другой (код в тексте ссылки)
            if _PH_A not in s:
                break
            s = _PH.sub(lambda m: self.items[int(m.group(1))] if int(m.group(1)) < len(self.items) else "", s)
        return s.replace(_PH_A, "").replace(_PH_B, "")


# ── блоки кода ───────────────────────────────────────────────────────────────
_FENCE_OPEN = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*([\w+#.-]*)[^`\n]*$")
_FENCE_CLOSE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*$")
_LANG_OK = re.compile(r"^[A-Za-z0-9_+#.-]{1,32}$")


def _fence_open(line: str) -> tuple[str, str] | None:
    """Строка открывает блок кода → (маркер, язык), иначе None."""
    m = _FENCE_OPEN.match(line)
    if not m:
        return None
    lang = m.group(2) if _LANG_OK.match(m.group(2) or "") else ""
    return m.group(1), lang


def _fence_closes(line: str, marker: str) -> bool:
    m = _FENCE_CLOSE.match(line)
    return bool(m and m.group(1)[0] == marker[0] and len(m.group(1)) >= len(marker))


def _pre(code: str, lang: str) -> str:
    if not code.strip():
        return ""                 # пустой блок Telegram не нужен
    body = escape(code)
    if lang:
        return f'<pre><code class="language-{_attr(lang)}">{body}</code></pre>'
    return f"<pre>{body}</pre>"


def _extract_fences(text: str, store: _Store, render: Callable[[str, str], str]) -> str:
    """Блоки ``` / ~~~ → заглушка на отдельной строке. Незакрытый блок идёт до конца текста."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        op = _fence_open(lines[i])
        if op is None:
            out.append(lines[i])
            i += 1
            continue
        marker, lang = op
        j = i + 1
        while j < len(lines) and not _fence_closes(lines[j], marker):
            j += 1
        out.append(store.put(render("\n".join(lines[i + 1:j]), lang)))
        i = j + 1
    return "\n".join(out)


# ── строчные элементы ────────────────────────────────────────────────────────
_INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_URL_BODY = r"(?:https?|tg)://[^\s()<>\[\]]*(?:\([^\s()<>]*\)[^\s()<>\[\]]*)*"
_LINK = re.compile(r"\[([^\[\]\n]+)\]\(\s*<?(" + _URL_BODY + r")>?\s*(?:\"[^\"\n]*\")?\s*\)")
_AUTOLINK = re.compile(r"<((?:https?|tg)://[^\s<>]+)>")
_BARE_URL = re.compile(r"(?:https?|tg)://[^\s<>\"'`]+")
_MENTION = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{3,32}\b")
_URL_TAIL = ".,;:!?…»\"'"


def _trim_url(url: str) -> tuple[str, str]:
    """Хвостовую пунктуацию — из адреса в текст; «)» остаётся, если внутри есть парная «(»."""
    tail = ""
    while url:
        c = url[-1]
        if c in _URL_TAIL or (c == ")" and url.count("(") < url.count(")")):
            tail = c + tail
            url = url[:-1]
            continue
        break
    return url, tail


def _code_span(m: re.Match) -> str | None:
    body = m.group(2)
    if len(body) >= 2 and body[0] == " " and body[-1] == " " and body.strip():
        body = body[1:-1]
    return body if body else None


def _protect_inline(s: str, store: _Store, *, html: bool) -> str:
    """Код, ссылки, адреса и упоминания → заглушки. html=False — для плоского текста."""
    def code(m: re.Match) -> str:
        body = _code_span(m)
        if body is None:
            return m.group(0)
        return store.put(f"<code>{escape(body)}</code>" if html else body)

    def link(m: re.Match) -> str:
        text, url = m.group(1), m.group(2)
        if html:
            return store.put(f'<a href="{_attr(url)}">{_inline(escape(text))}</a>')
        return store.put(f"{text} ({url})")

    def auto(m: re.Match) -> str:
        return store.put(escape(m.group(1)) if html else m.group(1))

    def bare(m: re.Match) -> str:
        url, tail = _trim_url(m.group(0))
        if not url:
            return m.group(0)
        return store.put(escape(url) if html else url) + tail

    def mention(m: re.Match) -> str:
        return store.put(m.group(0))

    s = _INLINE_CODE.sub(code, s)
    s = _LINK.sub(link, s)
    s = _AUTOLINK.sub(auto, s)
    s = _BARE_URL.sub(bare, s)
    return _MENTION.sub(mention, s)


_EMPHASIS = (
    ("b", re.compile(r"(?<![\w*\\])\*\*(?=\S)(.+?)(?<=\S)\*\*(?![\w*])")),
    ("b", re.compile(r"(?<![\w_\\])__(?=\S)(.+?)(?<=\S)__(?![\w_])")),
    ("s", re.compile(r"(?<![\w~\\])~~(?=\S)(.+?)(?<=\S)~~(?![\w~])")),
    ("i", re.compile(r"(?<![\w*\\])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])")),
    ("i", re.compile(r"(?<![\w_\\])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])")),
)


def _inline(s: str) -> str:
    """**жирный**, __жирный__, ~~зачёркнутый~~, *курсив*, _курсив_ — только парами и на границах слов.
    Пара, внутри которой теги не сбалансированы, остаётся буквальным текстом."""
    for tag, rx in _EMPHASIS:
        def repl(m: re.Match, tag: str = tag) -> str:
            inner = m.group(1)
            if not _balanced(inner):
                return m.group(0)
            return f"<{tag}>{inner}</{tag}>"
        s = rx.sub(repl, s)
    return s


def _strip_emphasis(s: str) -> str:
    for _tag, rx in _EMPHASIS:
        s = rx.sub(lambda m: m.group(1), s)
    return s


# ── строки и блоки ───────────────────────────────────────────────────────────
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_QUOTE = re.compile(r"^[ \t]{0,3}>[ \t]?(.*)$")
_QUOTE_MORE = re.compile(r"^(?:>[ \t]?)+")
_BULLET = re.compile(r"^([ \t]*)[-*+][ \t]+(.*)$")
_HR = re.compile(r"^[ \t]{0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
HR_LINE = "———"                 # не длиннее «---»: видимый текст не растёт


def _line_html(line: str) -> str:
    if _HR.match(line):
        return HR_LINE
    m = _HEADING.match(line)
    if m:
        head = re.sub(r"\*\*|__", "", m.group(1))       # жирный в жирном заголовке не нужен
        return f"<b>{_inline(escape(head))}</b>"
    m = _BULLET.match(line)
    if m:
        return f"{m.group(1)}• {_inline(escape(m.group(2)))}"
    return _inline(escape(line))


def _line_plain(line: str) -> str:
    if _HR.match(line):
        return HR_LINE
    m = _HEADING.match(line)
    if m:
        return _strip_emphasis(m.group(1))
    m = _BULLET.match(line)
    if m:
        return f"{m.group(1)}• {_strip_emphasis(m.group(2))}"
    return _strip_emphasis(line)


# ── проверка результата ──────────────────────────────────────────────────────
_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)((?:\s[^<>]*)?)>")
_OK_ATTR = {"a": re.compile(r'^\s+href="[^"<>]*"$'), "code": re.compile(r'^\s+class="language-[^"<>]*"$')}


def _balanced(fragment: str) -> bool:
    """Теги во фрагменте парные и правильно вложены (для решения «оборачивать ли пару»)."""
    stack: list[str] = []
    for m in _TAG.finditer(fragment):
        name = m.group(2).lower()
        if m.group(1):
            if not stack or stack.pop() != name:
                return False
        else:
            stack.append(name)
    return not stack


def well_formed(s: str) -> bool:
    """Годится ли HTML для Telegram: только разрешённые теги и атрибуты, всё закрыто по порядку,
    внутри code/pre нет других тегов (кроме code в pre), blockquote и a не вложены сами в себя,
    вне тегов нет голых < и >."""
    stack: list[str] = []
    for m in _TAG.finditer(s):
        closing, name, attrs = bool(m.group(1)), m.group(2).lower(), m.group(3)
        if name not in ALLOWED_TAGS:
            return False
        if closing:
            if attrs.strip() or not stack or stack.pop() != name:
                return False
            continue
        if attrs and not (name in _OK_ATTR and _OK_ATTR[name].match(attrs)):
            return False
        if name == "a" and not attrs:
            return False
        if stack and stack[-1] == "code":
            return False
        if stack and stack[-1] == "pre" and name != "code":
            return False
        if name in stack and name in ("a", "blockquote", "pre", "code"):
            return False
        stack.append(name)
    if stack:
        return False
    rest = _TAG.sub("", s)
    return "<" not in rest and ">" not in rest


# ── главное ──────────────────────────────────────────────────────────────────
def md_to_html(text: str) -> str:
    """Markdown модели → HTML Telegram (b, i, s, code, pre, a, blockquote). Всегда корректный:
    если что-то пошло не так — просто экранированный текст."""
    src = (text or "").replace(_PH_A, "").replace(_PH_B, "")
    if not src:
        return ""
    try:
        store = _Store()
        s = _extract_fences(src, store, _pre)
        s = _protect_inline(s, store, html=True)
        out: list[str] = []
        quote: list[str] = []

        def flush() -> None:
            if quote:
                body = "\n".join(quote).strip("\n")
                out.append(f"<blockquote>{body}</blockquote>" if body.strip() else "")
                quote.clear()

        for line in s.split("\n"):
            m = _QUOTE.match(line)
            if m:
                quote.append(_line_html(_QUOTE_MORE.sub("", m.group(1))))
                continue
            flush()
            out.append(_line_html(line))
        flush()
        html = store.restore("\n".join(out))
    except Exception:  # разметка — не повод не ответить
        return escape(src)
    return html if well_formed(html) else escape(src)


def plain(text: str) -> str:
    """Markdown → простой текст без маркеров (запасной путь, когда Telegram не принял HTML)."""
    src = (text or "").replace(_PH_A, "").replace(_PH_B, "")
    if not src:
        return ""
    store = _Store()
    s = _extract_fences(src, store, lambda code, _lang: code)
    s = _protect_inline(s, store, html=False)
    out: list[str] = []
    for line in s.split("\n"):
        m = _QUOTE.match(line)
        if m:
            out.append("│ " + _line_plain(_QUOTE_MORE.sub("", m.group(1))))
        else:
            out.append(_line_plain(line))
    return store.restore("\n".join(out))


# ── нарезка ──────────────────────────────────────────────────────────────────
_SENT_END = re.compile(r"([.!?…][)\]»\"']*)(\s+)")


def _best_cut(s: str, budget: int) -> tuple[int, int]:
    """Где резать строку длиннее budget → (конец куска, начало следующего).
    Приоритет: пустая строка → перевод строки → конец предложения → пробел → посередине.
    Сначала ищем точку не ближе четверти бюджета от начала (чтобы не плодить огрызки)."""
    for min_pos in (max(1, budget // 4), 1):
        i = s.rfind("\n\n", 0, budget + 2)
        if i >= min_pos:
            return i, i + 2
        i = s.rfind("\n", 0, budget + 1)
        if i >= min_pos:
            return i, i + 1
        best = None
        for m in _SENT_END.finditer(s, 0, budget + 1):
            if m.end(1) > budget:
                break
            if m.end(1) >= min_pos:
                best = (m.end(1), m.end())
        if best:
            return best
        i = s.rfind(" ", 0, budget + 1)
        if i >= min_pos:
            return i, i + 1
    return budget, budget


def _cut_pieces(text: str, budget: int) -> list[str]:
    pieces: list[str] = []
    rest = text
    while len(rest) > budget:
        end, nxt = _best_cut(rest, budget)
        pieces.append(rest[:end].rstrip())
        rest = rest[nxt:]
        rest = rest.lstrip("\n") if end != nxt else rest
    pieces.append(rest)
    return pieces


def _fence_state(text: str, state: tuple[str, str] | None) -> tuple[str, str] | None:
    """Открыт ли блок кода после text, если до него было state."""
    for line in text.split("\n"):
        if state is None:
            state = _fence_open(line)
        elif _fence_closes(line, state[0]):
            state = None
    return state


def _balance_fences(pieces: list[str]) -> list[str]:
    out: list[str] = []
    state: tuple[str, str] | None = None
    for p in pieces:
        prefix = ""
        if state is not None:
            first, _, remainder = p.partition("\n")
            if _fence_closes(first, state[0]):          # кусок начинается с закрытия — просто выкидываем
                p, state = remainder, None
            else:
                prefix = f"{state[0]}{state[1]}\n"
        end_state = _fence_state(p, state)
        body = prefix + p
        if end_state is not None:
            body = body.rstrip("\n") + "\n" + end_state[0]
        state = end_state
        out.append(body)
    return out


def split_message(text: str, limit: int = LIMIT) -> list[str]:
    """Длинный markdown → куски не длиннее limit. Пустой текст → []."""
    text = (text or "").rstrip().lstrip("\n")
    if not text.strip():
        return []
    limit = max(1, int(limit))
    if len(text) <= limit:
        return [text]
    openers = [len(op[0]) + len(op[1]) for line in text.split("\n") if (op := _fence_open(line))]
    reserve = 2 * (max(openers) + 1) if openers else 0
    if reserve and limit - reserve < max(20, limit // 4):   # крошечный лимит — без переоткрытия блоков
        reserve = 0
    pieces = _cut_pieces(text, limit - reserve)
    if reserve:
        pieces = _balance_fences(pieces)
    out: list[str] = []
    for p in pieces:
        while len(p) > limit:                   # страховка: вырожденные случаи
            out.append(p[:limit])
            p = p[limit:]
        if _has_content(p):
            out.append(p)
    return out


def utf16_len(s: str) -> int:
    """Длина так, как её считает Telegram (UTF-16: эмодзи — 2 единицы)."""
    return len(s.encode("utf-16-le")) // 2


def _has_content(chunk: str) -> bool:
    """Есть ли в куске что-то кроме пробелов и строк-ограждений кода (пустой блок слать незачем)."""
    return any(line.strip() and not (_fence_open(line) or _FENCE_CLOSE.match(line))
               for line in chunk.split("\n"))
