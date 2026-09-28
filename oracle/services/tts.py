"""Синтез речи (текст → mp3) через edge-tts и подготовка текста к озвучке.

Ответ модели написан для глаз: markdown, ссылки, эмодзи, списки, код. Вслух это звучит
как «звёздочка-звёздочка, эйч-ти-ти-пи». `clean_for_speech` превращает его в то, что
нормально слушать: разметка прочь, ссылки — только текст, код — «Код в тексте.»,
пункты списков — отдельные предложения, длинное — обрезается по концу предложения.
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import re
import sys
from typing import Any

log = logging.getLogger("oracle.tts")

CUT_SUFFIX = "… дальше — в тексте."
CODE_SAY = "Код в тексте."
SYNTH_TIMEOUT = 120.0


class TTSError(Exception):
    """Озвучка не удалась (сеть, сервис) — человеческим текстом."""


# ── чистка текста ────────────────────────────────────────────────────────────
_FENCE = re.compile(r"(?ms)^[ \t]*(```|~~~).*?(?:^[ \t]*\1[ \t]*$|\Z)")
_FENCE_INLINE = re.compile(r"```.*?```", re.S)
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\((?:[^()\s]|\([^()]*\))*\)")
_REF_LINK = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")
_URL = re.compile(r"(?:https?://|ftp://|www\.)[^\s<>()\[\]]+", re.I)
_HTML_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?/?>")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_QUOTE = re.compile(r"^\s*(?:>\s?)+")
_BULLET = re.compile(r"^\s*(?:[-*+•▪◦‣–—]|\d{1,3}[.)])\s+")
_HR = re.compile(r"^\s*(?:[-*_=]\s*){3,}$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
_ID = re.compile(r"(?<![\w&])#(\d+)")
_TERMINAL = ".!?…:;"

# эмодзи и пиктограммы (плюс модификаторы, из которых их собирают)
_EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"     # эмодзи, флаги, символы и пиктограммы
    "\U00002600-\U000027BF"     # разные символы и дингбаты (☀ ✅ ❤ ✂)
    "\U00002B00-\U00002BFF"     # звёзды и стрелки-эмодзи (⭐ ⬆)
    "\U00002300-\U000023FF"     # технические (⏰ ⌛ ⏳)
    "\U00002190-\U000021FF"     # стрелки
    "\U000025A0-\U000025FF"     # геометрические фигуры (▶ ◀ ■)
    "\U0000FE00-\U0000FE0F"     # вариационные селекторы
    "\U0000200D\U000020E3"      # склейка (ZWJ) и «клавиша»
    "\U000E0020-\U000E007F"     # теги флагов
    "\U00003030\U0000303D\U00003297\U00003299\U000000A9\U000000AE\U00002122"
    "]+")


def _strip_inline(s: str) -> str:
    """Разметка внутри строки: ссылки, код, жирный/курсив, html, эмодзи."""
    s = _IMAGE.sub(lambda m: m.group(1), s)
    s = _LINK.sub(lambda m: m.group(1), s)
    s = _REF_LINK.sub(lambda m: m.group(1), s)
    had_url = bool(_URL.search(s))
    s = _URL.sub("", s)
    s = _HTML_TAG.sub("", s)
    s = s.replace("`", "")
    s = re.sub(r"\*{1,3}|~~", "", s)
    s = re.sub(r"__(.+?)__", r"\1", s)
    s = re.sub(r"(?<=[^\W_])_(?=[^\W_])", " ", s)   # snake_case → «snake case»
    s = s.replace("_", "")
    s = re.sub(r"\s*(?:→|⇒|->|=>)\s*", " — ", s)     # «А → Б» читается как пауза
    s = s.replace("·", ",").replace("|", ",")
    s = _ID.sub(r"номер \1", s)
    s = _EMOJI.sub("", s)
    s = re.sub(r"[ \t ]+", " ", s)
    s = re.sub(r"\s+([,.!?…:;])", r"\1", s)
    s = re.sub(r"([,:;])(?:\s*[,:;])+", r"\1", s)    # «, ,» от выброшенных кусков
    s = re.sub(r"\(\s*\)", "", s)
    s = s.strip(" ,;")
    s = re.sub(r"^[—–]\s+|\s+[—–]$", "", s)
    if had_url:   # «читай тут: <ссылка>» / «статью и <ссылка>» — хвост без ссылки не нужен
        s = re.sub(r"(?:\s+(?:и|или|а|вот|тут|здесь|по ссылке))+\s*[:,—-]?\s*$", "", s, flags=re.I)
        s = s.rstrip(" ,;:—-")
    return s.strip(" ,;")


def _truncate(text: str, max_chars: int) -> str:
    """Обрезать до max_chars (вместе с хвостом) по концу предложения, иначе — по слову."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    budget = max_chars - len(CUT_SUFFIX)
    if budget < 20:
        return text[:max_chars].rstrip()
    head = text[: budget + 1]
    ends = [m.end() for m in re.finditer(r"[.!?…](?=\s)", head)]
    if ends and ends[-1] >= budget * 0.4:
        body = head[: ends[-1]]
    else:
        sp = head.rfind(" ")
        body = head[:sp] if sp > budget * 0.5 else head[:budget]
    body = body.rstrip().rstrip(".,;:—- ")
    return body + CUT_SUFFIX


def clean_for_speech(text: str, max_chars: int | None = 1500) -> str:
    """Текст ответа → текст для озвучки. Пусто на входе или одни эмодзи → ""."""
    if not text:
        return ""
    t = str(text).replace("\r\n", "\n").replace("\r", "\n")
    # блоки кода целиком не читаем
    t = _FENCE.sub(f"\n{CODE_SAY}\n", t)
    t = _FENCE_INLINE.sub(" код в тексте ", t)
    sentences: list[str] = []
    for raw in t.split("\n"):
        line = raw.rstrip()
        if not line.strip() or _HR.match(line) or _TABLE_SEP.match(line):
            continue
        h = _HEADING.match(line)
        if h:
            line = h.group(1)
        line = _QUOTE.sub("", line)
        line = _BULLET.sub("", line)
        line = _strip_inline(line)
        if not line or not re.search(r"\w", line):
            continue
        if line[-1] not in _TERMINAL:
            line += "."
        if line == CODE_SAY and sentences and sentences[-1] == CODE_SAY:
            continue                                  # два блока кода подряд — одна фраза
        sentences.append(line)
    out = " ".join(sentences)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return _truncate(out, int(max_chars or 0))


# ── синтез ───────────────────────────────────────────────────────────────────
def _edge_tts_available() -> bool:
    if sys.modules.get("edge_tts") is not None:
        return True
    try:
        return importlib.util.find_spec("edge_tts") is not None
    except (ImportError, ValueError):
        return False


class TTS:
    """Озвучка голосом Microsoft Edge (бесплатно, без ключа, нужен интернет)."""

    def __init__(self, cfg: Any):
        self.cfg = cfg

    def available(self) -> bool:
        return _edge_tts_available()

    async def synth(self, text: str) -> bytes:
        """Текст → mp3. Нечего озвучивать → ValueError; сервис не ответил → TTSError."""
        clean = clean_for_speech(text, max_chars=getattr(self.cfg, "tts_max_chars", 1500))
        if not clean:
            raise ValueError("нечего озвучивать: после чистки текста ничего не осталось")
        try:
            import edge_tts
        except ImportError as e:
            raise TTSError("edge-tts не установлен: pip install edge-tts") from e

        async def collect() -> bytes:
            buf = bytearray()
            com = edge_tts.Communicate(clean, self.cfg.tts_voice)
            async for chunk in com.stream():
                if chunk.get("type") == "audio" and chunk.get("data"):
                    buf += chunk["data"]
            return bytes(buf)

        try:
            audio = await asyncio.wait_for(collect(), timeout=SYNTH_TIMEOUT)
        except asyncio.TimeoutError as e:
            raise TTSError("озвучка не успела — сервис синтеза молчит") from e
        except Exception as e:     # aiohttp, NoAudioReceived, неверный голос…
            log.warning("edge-tts: %r", e)
            raise TTSError(f"озвучка не удалась: {type(e).__name__}") from e
        if not audio:
            raise TTSError("сервис синтеза вернул пустой звук")
        return audio
