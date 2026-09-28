"""Распознавание речи (голосовые → текст).

Провайдеры (`STT_PROVIDER`):
  • groq   — Whisper в облаке Groq, бесплатный ключ на console.groq.com (GROQ_API_KEY);
  • openai — Whisper в OpenAI (OPENAI_API_KEY);
  • local  — faster-whisper на этой машине (pip install faster-whisper), модель качается
             один раз в data/whisper;
  • off    — выключено;
  • auto   — groq, если есть ключ; иначе openai; иначе local, если faster-whisper стоит; иначе off.

Явно выбранный провайдер без того, что ему нужно (ключа, пакета), даёт `off` с причиной —
бот не молчит, а объясняет, что поправить. Все ошибки — `STTError` с человеческим текстом.
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import logging
import re
import sys
from typing import Any

import httpx

log = logging.getLogger("oracle.stt")

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions"
MAX_BYTES = 25 * 1024 * 1024          # предел Whisper API
TIMEOUT = 120.0
PROVIDERS = ("groq", "openai", "local", "off")

TOO_BIG = "файл больше 25 МБ — пришли короче"
NOT_RESPONDING = "сервис распознавания не отвечает"
HOW_TO_ENABLE = ("Включить: бесплатный ключ Groq на console.groq.com → GROQ_API_KEY в .env "
                 "(самое простое), или OPENAI_API_KEY, или локально: pip install faster-whisper "
                 "(STT_PROVIDER=local).")

# Whisper на тишине «слышит» титры из обучающих данных — такой «текст» считаем пустым
_HALLUCINATIONS = re.compile(
    r"^(?:продолжение следует|субтитры (?:сделал|создавал|делал)\b.*|редактор субтитров\b.*"
    r"|спасибо за просмотр|подписывайтесь на (?:наш )?канал|thanks for watching|you)[.!…\s]*$",
    re.I)


class STTError(Exception):
    """Ошибка распознавания человеческим текстом (её можно показать владельцу как есть)."""


def _module_available(name: str) -> bool:
    """Пакет можно импортировать (не импортируя его)."""
    if sys.modules.get(name) is not None:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def local_available() -> bool:
    return _module_available("faster_whisper")


def resolve_provider(cfg: Any) -> tuple[str, str]:
    """(провайдер, причина, если off). auto выбирает лучший доступный."""
    want = str(getattr(cfg, "stt_provider", "auto") or "auto").strip().lower()
    groq = bool(getattr(cfg, "groq_api_key", ""))
    openai = bool(getattr(cfg, "openai_api_key", ""))
    if want == "off":
        return "off", "распознавание голоса выключено (STT_PROVIDER=off)."
    if want == "groq":
        return ("groq", "") if groq else ("off", "STT_PROVIDER=groq, но нет GROQ_API_KEY.")
    if want == "openai":
        return ("openai", "") if openai else ("off", "STT_PROVIDER=openai, но нет OPENAI_API_KEY.")
    if want == "local":
        return ("local", "") if local_available() else \
            ("off", "STT_PROVIDER=local, но faster-whisper не установлен.")
    if want != "auto":
        log.warning("неизвестный STT_PROVIDER=%r — выбираю сам (auto)", want)
    if groq:
        return "groq", ""
    if openai:
        return "openai", ""
    if local_available():
        return "local", ""
    return "off", "распознавание голоса не настроено."


class STT:
    """Голос → текст через выбранного провайдера."""

    def __init__(self, cfg: Any, http: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._http = http
        self._own_http = http is None
        self.provider, self.reason = resolve_provider(cfg)
        self._model: Any = None                 # faster_whisper.WhisperModel, грузится один раз
        self._model_lock = asyncio.Lock()
        if self.provider == "off":
            log.info("STT выключен: %s", self.reason)
        else:
            log.info("STT: %s", self.describe())

    def available(self) -> bool:
        return self.provider != "off"

    def describe(self) -> str:
        """Одна строка для /status."""
        c = self.cfg
        if self.provider == "groq":
            return f"Groq ({c.groq_stt_model})"
        if self.provider == "openai":
            return f"OpenAI ({c.openai_stt_model})"
        if self.provider == "local":
            state = "загружена" if self._model is not None else "загрузится при первом голосовом"
            return f"локальный faster-whisper ({c.whisper_model}, {c.whisper_device}; {state})"
        return f"выключено — {self.reason}"

    async def aclose(self) -> None:
        if self._http is not None and self._own_http:
            await self._http.aclose()
            self._http = None

    # ── главное ──
    async def transcribe(self, data: bytes, filename: str = "voice.ogg", mime: str = "audio/ogg") -> str:
        """Байты аудио → распознанный текст (без пробелов по краям).
        Ничего не разобрал — STTError, а не пустая строка."""
        if not data:
            raise STTError("пустой файл")
        if len(data) > MAX_BYTES:
            raise STTError(TOO_BIG)
        if self.provider == "off":
            raise STTError(f"{self.reason} {HOW_TO_ENABLE}".strip())
        if self.provider == "local":
            text = await self._local(bytes(data))
        elif self.provider == "groq":
            text = await self._remote(GROQ_URL, self.cfg.groq_api_key, self.cfg.groq_stt_model,
                                      "GROQ_API_KEY", data, filename, mime)
        else:
            text = await self._remote(OPENAI_URL, self.cfg.openai_api_key, self.cfg.openai_stt_model,
                                      "OPENAI_API_KEY", data, filename, mime)
        text = " ".join((text or "").split())
        if not text or _HALLUCINATIONS.match(text):
            raise STTError("не разобрал ни слова — скажи ещё раз, погромче или ближе к микрофону")
        return text

    # ── облако (Groq / OpenAI: один и тот же протокол) ──
    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=TIMEOUT)
            self._own_http = True
        return self._http

    async def _remote(self, url: str, key: str, model: str, key_name: str,
                      data: bytes, filename: str, mime: str) -> str:
        form = {"model": model, "response_format": "json", "temperature": "0"}
        if self.cfg.stt_language:
            form["language"] = self.cfg.stt_language
        files = {"file": (filename or "voice.ogg", bytes(data), mime or "application/octet-stream")}
        http = await self._client()
        try:
            r = await http.post(url, data=form, files=files, timeout=TIMEOUT,
                                headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError as e:           # таймаут, нет сети, обрыв
            log.warning("STT %s: сеть: %r", self.provider, e)
            raise STTError(NOT_RESPONDING) from e
        if r.status_code != 200:
            raise STTError(self._http_error(r, key_name))
        try:
            body = r.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and isinstance(body.get("text"), str):
            return body["text"].strip()
        log.warning("STT %s: странный ответ: %s", self.provider, r.text[:300])
        raise STTError("сервис распознавания вернул непонятный ответ")

    def _http_error(self, r: httpx.Response, key_name: str) -> str:
        code = r.status_code
        log.warning("STT %s: HTTP %s: %s", self.provider, code, r.text[:300])
        if code in (401, 403):
            return f"ключ распознавания речи не принят ({code}) — проверь {key_name} в .env"
        if code == 413:
            return TOO_BIG
        if code == 429:
            return "лимит распознавания — подожди минуту"
        if code >= 500:
            return f"{NOT_RESPONDING} ({code})"
        detail = ""
        try:
            err = r.json().get("error")
            detail = err.get("message", "") if isinstance(err, dict) else str(err or "")
        except (ValueError, AttributeError):
            detail = r.text
        detail = " ".join(str(detail).split())[:200]
        return f"сервис распознавания не принял файл ({code})" + (f": {detail}" if detail else "")

    # ── локально (faster-whisper) ──
    async def _load_local(self) -> Any:
        async with self._model_lock:
            if self._model is not None:
                return self._model
            try:
                from faster_whisper import WhisperModel
            except ImportError as e:
                raise STTError("faster-whisper не установлен: pip install faster-whisper") from e
            c = self.cfg
            root = c.data_dir / "whisper"
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            log.info("гружу whisper «%s» (%s, %s)…", c.whisper_model, c.whisper_device, c.whisper_compute_type)
            try:
                self._model = await asyncio.to_thread(
                    WhisperModel, c.whisper_model, device=c.whisper_device,
                    compute_type=c.whisper_compute_type, download_root=str(root))
            except Exception as e:
                log.exception("whisper не загрузился")
                raise STTError(f"не смог загрузить модель whisper «{c.whisper_model}»: {e}") from e
            return self._model

    async def _local(self, data: bytes) -> str:
        model = await self._load_local()
        lang = self.cfg.stt_language or None

        def run() -> str:
            # segments — ленивый генератор: распознавание идёт при переборе, поэтому весь перебор здесь, в потоке
            segments, _info = model.transcribe(io.BytesIO(data), language=lang, vad_filter=True, beam_size=5)
            return " ".join(s.text.strip() for s in segments)

        try:
            return await asyncio.to_thread(run)
        except Exception as e:
            log.exception("локальное распознавание упало")
            raise STTError(f"локальный whisper не смог распознать запись: {type(e).__name__}") from e
