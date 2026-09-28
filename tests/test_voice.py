"""Голос: распознавание (Groq / OpenAI / локальный whisper / выключено), чистка текста и озвучка."""
from __future__ import annotations

import asyncio
import io
import sys
import types

import httpx
import pytest

from conftest import with_cfg
from oracle.services import stt as S
from oracle.services import tts as T
from oracle.services.stt import STT, STTError
from oracle.services.tts import TTS, TTSError, clean_for_speech


# ── выбор провайдера ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("want, groq, openai, local, expected", [
    ("auto", "gsk", "", False, "groq"),
    ("auto", "gsk", "sk", True, "groq"),
    ("auto", "", "sk", True, "openai"),
    ("auto", "", "", True, "local"),
    ("auto", "", "", False, "off"),
    ("groq", "gsk", "", False, "groq"),
    ("groq", "", "sk", True, "off"),          # явно выбран, но ключа нет — не подменяем молча
    ("openai", "", "sk", False, "openai"),
    ("openai", "gsk", "", False, "off"),
    ("local", "gsk", "", True, "local"),
    ("local", "gsk", "", False, "off"),
    ("off", "gsk", "sk", True, "off"),
    ("OFF", "gsk", "", False, "off"),
    ("whisper", "", "sk", False, "openai"),   # неизвестное значение — как auto
    ("", "gsk", "", False, "groq"),
])
def test_provider_matrix(cfg, monkeypatch, want, groq, openai, local, expected):
    monkeypatch.setattr(S, "local_available", lambda: local)
    stt = STT(with_cfg(cfg, stt_provider=want, groq_api_key=groq, openai_api_key=openai))
    assert stt.provider == expected
    assert stt.available() is (expected != "off")
    if expected == "off":
        assert stt.reason
    assert stt.describe()


def test_off_reason_names_missing_piece(cfg, monkeypatch):
    monkeypatch.setattr(S, "local_available", lambda: False)
    assert "GROQ_API_KEY" in STT(with_cfg(cfg, stt_provider="groq")).reason
    assert "OPENAI_API_KEY" in STT(with_cfg(cfg, stt_provider="openai")).reason
    assert "faster-whisper" in STT(with_cfg(cfg, stt_provider="local")).reason


def test_local_available_sees_injected_module(monkeypatch):
    monkeypatch.delitem(sys.modules, "faster_whisper", raising=False)
    fake = types.ModuleType("faster_whisper")          # без __spec__ — find_spec бы упал
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    assert S.local_available() is True
    monkeypatch.delitem(sys.modules, "faster_whisper")
    assert S._module_available("definitely_not_a_module_xyz") is False


# ── облако ───────────────────────────────────────────────────────────────────
def _stt(cfg, handler, **kw) -> STT:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return STT(with_cfg(cfg, **kw), http=http)


async def test_groq_request_shape(cfg):
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"text": "  Привет,   мир!  "})

    stt = _stt(cfg, handler, groq_api_key="gsk-test", openai_api_key="sk-other")
    assert stt.provider == "groq"
    text = await stt.transcribe(b"OggS-fake-audio", "voice.ogg", "audio/ogg")
    assert text == "Привет, мир!"
    req = seen[0]
    assert str(req.url) == S.GROQ_URL and req.method == "POST"
    assert req.headers["authorization"] == "Bearer gsk-test"
    assert req.headers["content-type"].startswith("multipart/form-data")
    body = req.content
    assert b'name="model"' in body and b"whisper-large-v3-turbo" in body
    assert b'name="language"' in body and b"\r\n\r\nru\r\n" in body
    assert b'name="response_format"' in body and b"\r\n\r\njson\r\n" in body
    assert b'name="temperature"' in body
    assert b'name="file"; filename="voice.ogg"' in body and b"audio/ogg" in body
    assert b"OggS-fake-audio" in body


async def test_openai_endpoint_and_model(cfg):
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"text": "ок"})

    stt = _stt(cfg, handler, openai_api_key="sk-oa", stt_language="")
    assert await stt.transcribe(b"x", "a.mp3", "audio/mpeg") == "ок"
    req = seen[0]
    assert str(req.url) == S.OPENAI_URL
    assert req.headers["authorization"] == "Bearer sk-oa"
    assert b"whisper-1" in req.content
    assert b'name="language"' not in req.content          # язык не задан — пусть определяет сам
    assert b'filename="a.mp3"' in req.content


@pytest.mark.parametrize("status, needle", [
    (401, "ключ распознавания речи не принят"),
    (403, "ключ распознавания речи не принят"),
    (413, "25 МБ"),
    (429, "лимит распознавания — подожди минуту"),
    (500, "сервис распознавания не отвечает"),
    (503, "сервис распознавания не отвечает"),
    (400, "не принял файл"),
])
async def test_http_error_mapping(cfg, status, needle):
    def handler(req):
        return httpx.Response(status, json={"error": {"message": "bad audio"}})

    stt = _stt(cfg, handler, groq_api_key="gsk")
    with pytest.raises(STTError) as ei:
        await stt.transcribe(b"x")
    assert needle in str(ei.value)
    if status == 401:
        assert "GROQ_API_KEY" in str(ei.value)
    if status == 400:
        assert "bad audio" in str(ei.value)


@pytest.mark.parametrize("exc", [httpx.ConnectError("boom"), httpx.ReadTimeout("slow")])
async def test_network_errors(cfg, exc):
    def handler(req):
        raise exc

    stt = _stt(cfg, handler, groq_api_key="gsk")
    with pytest.raises(STTError, match="не отвечает"):
        await stt.transcribe(b"x")


async def test_weird_response(cfg):
    stt = _stt(cfg, lambda req: httpx.Response(200, text="<html>"), groq_api_key="gsk")
    with pytest.raises(STTError, match="непонятный"):
        await stt.transcribe(b"x")
    stt = _stt(cfg, lambda req: httpx.Response(200, json={"nope": 1}), groq_api_key="gsk")
    with pytest.raises(STTError, match="непонятный"):
        await stt.transcribe(b"x")


@pytest.mark.parametrize("said", ["", "   ", "Продолжение следует...", "Субтитры сделал DimaTorzok",
                                  "Спасибо за просмотр!"])
async def test_nothing_recognized(cfg, said):
    stt = _stt(cfg, lambda req: httpx.Response(200, json={"text": said}), groq_api_key="gsk")
    with pytest.raises(STTError, match="не разобрал"):
        await stt.transcribe(b"x")


async def test_size_limits_before_network(cfg):
    def handler(req):
        raise AssertionError("в сеть ходить не должны")

    stt = _stt(cfg, handler, groq_api_key="gsk")
    with pytest.raises(STTError, match="пустой файл"):
        await stt.transcribe(b"")
    with pytest.raises(STTError, match="больше 25 МБ"):
        await stt.transcribe(b"0" * (S.MAX_BYTES + 1))


async def test_off_explains_how_to_enable(cfg, monkeypatch):
    monkeypatch.setattr(S, "local_available", lambda: False)
    stt = STT(with_cfg(cfg, stt_provider="auto"))
    assert stt.provider == "off" and not stt.available()
    with pytest.raises(STTError) as ei:
        await stt.transcribe(b"audio")
    msg = str(ei.value)
    assert "GROQ_API_KEY" in msg and "console.groq.com" in msg and "pip install faster-whisper" in msg


async def test_own_http_client_closed(cfg):
    stt = STT(with_cfg(cfg, groq_api_key="gsk"))
    http = await stt._client()
    assert not http.is_closed
    await stt.aclose()
    assert http.is_closed


# ── локальный whisper ────────────────────────────────────────────────────────
def _fake_whisper(monkeypatch, *, fail_load: bool = False, fail_run: bool = False) -> dict:
    box: dict = {"inits": [], "calls": []}

    class Seg:
        def __init__(self, text: str):
            self.text = text

    class WhisperModel:
        def __init__(self, name, **kw):
            if fail_load:
                raise RuntimeError("нет сети для скачивания")
            box["inits"].append((name, kw))

        def transcribe(self, audio, **kw):
            if fail_run:
                raise RuntimeError("битый файл")
            assert isinstance(audio, io.BytesIO)
            box["calls"].append((audio.read(), kw))

            def gen():   # как у faster-whisper: ленивый генератор
                yield Seg("  Привет, ")
                yield Seg(" это локальный ")
                yield Seg("whisper. ")
            return gen(), types.SimpleNamespace(language="ru")

    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = WhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    return box


async def test_local_transcribe_loads_model_once(cfg, monkeypatch):
    box = _fake_whisper(monkeypatch)
    stt = STT(with_cfg(cfg, stt_provider="auto", whisper_model="small", whisper_device="cpu",
                       whisper_compute_type="int8"))
    assert stt.provider == "local"
    assert "загрузится" in stt.describe()
    results = await asyncio.gather(stt.transcribe(b"aud1"), stt.transcribe(b"aud2"))
    assert results == ["Привет, это локальный whisper."] * 2
    assert len(box["inits"]) == 1                         # модель грузится один раз даже при гонке
    name, kw = box["inits"][0]
    assert name == "small"
    assert kw == {"device": "cpu", "compute_type": "int8", "download_root": str(cfg.data_dir / "whisper")}
    audio, kw = box["calls"][0]
    assert audio in (b"aud1", b"aud2")
    assert kw == {"language": "ru", "vad_filter": True, "beam_size": 5}
    assert "загружена" in stt.describe()


async def test_local_autodetect_language(cfg, monkeypatch):
    box = _fake_whisper(monkeypatch)
    stt = STT(with_cfg(cfg, stt_provider="local", stt_language=""))
    await stt.transcribe(b"x")
    assert box["calls"][0][1]["language"] is None


async def test_local_errors(cfg, monkeypatch):
    _fake_whisper(monkeypatch, fail_load=True)
    stt = STT(with_cfg(cfg, stt_provider="local"))
    with pytest.raises(STTError, match="не смог загрузить модель whisper"):
        await stt.transcribe(b"x")
    _fake_whisper(monkeypatch, fail_run=True)
    stt = STT(with_cfg(cfg, stt_provider="local"))
    with pytest.raises(STTError, match="не смог распознать"):
        await stt.transcribe(b"x")


async def test_local_package_vanished(cfg, monkeypatch):
    monkeypatch.setattr(S, "local_available", lambda: True)
    monkeypatch.setitem(sys.modules, "faster_whisper", None)   # import → ImportError
    stt = STT(with_cfg(cfg, stt_provider="local"))
    with pytest.raises(STTError, match="pip install faster-whisper"):
        await stt.transcribe(b"x")


# ── чистка текста для озвучки ────────────────────────────────────────────────
def test_clean_markdown_and_links():
    s = clean_for_speech("## План\n**Главное:** встать _рано_ и __бодро__, `make run`.\n"
                         "Читай [статью](https://example.com/a(b)) и вот https://x.ru/p?q=1\n"
                         "> мудрая цитата")
    assert s == "План. Главное: встать рано и бодро, make run. Читай статью. мудрая цитата."
    for junk in ("*", "_", "#", "`", "http", "](", ">"):
        assert junk not in s


def test_clean_code_blocks():
    s = clean_for_speech("Вот скрипт:\n```python\nprint('секрет')\nx = 1\n```\n```\nещё\n```\nЗапускай.")
    assert s == "Вот скрипт: Код в тексте. Запускай."
    assert "секрет" not in s
    assert clean_for_speech("Смотри: ```a = 1``` и всё") == "Смотри: код в тексте и всё."
    assert clean_for_speech("```\nнезакрытый\nкод") == "Код в тексте."


def test_clean_bullets_become_sentences():
    s = clean_for_speech("Список:\n- молоко\n* хлеб\n• яйца\n1. позвонить маме\n2) купить цветы!")
    assert s == "Список: молоко. хлеб. яйца. позвонить маме. купить цветы!"


def test_clean_emoji_symbols_and_ids():
    s = clean_for_speech("Готово ✅🔥 Будильник ⏰ на 7:00 → подъём 👍🏽🇷🇺\n"
                         "#12 · пн 29.09 07:30 · каждый день\n---\nМороз -5, файл snake_case_name.")
    assert s == ("Готово Будильник на 7:00 — подъём. номер 12, пн 29.09 07:30, каждый день. "
                 "Мороз -5, файл snake case name.")
    assert clean_for_speech("🔥🎉 👍 ✅") == ""
    assert clean_for_speech("") == "" and clean_for_speech(None) == ""   # type: ignore[arg-type]
    assert clean_for_speech("```\n```") == "Код в тексте."


def test_clean_table_and_html():
    s = clean_for_speech("| Что | Сколько |\n|---|:---:|\n| кофе | 3 |\n<b>важно</b>")
    assert s == "Что, Сколько. кофе, 3. важно."


def test_truncate_at_sentence_boundary():
    text = "Первое предложение тут. Второе предложение подлиннее будет. Третье уже не влезет никак."
    s = clean_for_speech(text, max_chars=80)
    assert s == "Первое предложение тут. Второе предложение подлиннее будет" + T.CUT_SUFFIX
    assert len(s) <= 80
    s = clean_for_speech(text, max_chars=70)          # хвост тоже в лимите — влезает только первое
    assert s == "Первое предложение тут" + T.CUT_SUFFIX
    assert clean_for_speech(text, max_chars=500) == text
    assert clean_for_speech(text, max_chars=0) == text               # 0 — без ограничения
    # одно огромное предложение — режем по слову
    s = clean_for_speech("слово " * 100, max_chars=80)
    assert s.endswith(T.CUT_SUFFIX) and len(s) <= 80 and "слов…" not in s
    # граница предложения слишком рано — тоже по слову, а не «Да.»
    s = clean_for_speech("Да. " + "очень длинное рассуждение " * 20, max_chars=100)
    assert len(s) <= 100 and s.startswith("Да. очень") and s.endswith(T.CUT_SUFFIX)


def test_truncate_default_limit():
    long = ("Это предложение про что-то важное. " * 100).strip()
    s = clean_for_speech(long)
    assert len(s) <= 1500 and s.endswith(T.CUT_SUFFIX)


# ── синтез ───────────────────────────────────────────────────────────────────
class FakeCommunicate:
    instances: list["FakeCommunicate"] = []
    chunks: list[dict] = []
    error: Exception | None = None

    def __init__(self, text, voice="en-US", **kw):
        self.text, self.voice = text, voice
        FakeCommunicate.instances.append(self)

    async def stream(self):
        for ch in FakeCommunicate.chunks:
            if FakeCommunicate.error is not None:
                raise FakeCommunicate.error
            yield ch


@pytest.fixture
def fake_edge(monkeypatch):
    import edge_tts
    FakeCommunicate.instances = []
    FakeCommunicate.chunks = [
        {"type": "audio", "data": b"ID3"},
        {"type": "SentenceBoundary", "offset": 1.0, "duration": 2.0, "text": "Привет"},
        {"type": "audio", "data": b"\xff\xfb-mp3"},
        {"type": "audio", "data": b""},
    ]
    FakeCommunicate.error = None
    monkeypatch.setattr(edge_tts, "Communicate", FakeCommunicate)
    return FakeCommunicate


async def test_synth_collects_audio_chunks(cfg, fake_edge):
    tts = TTS(with_cfg(cfg, tts_voice="ru-RU-SvetlanaNeural"))
    assert tts.available()
    audio = await tts.synth("**Привет!** Вот [ссылка](https://x.ru) 🔥")
    assert audio == b"ID3\xff\xfb-mp3"
    com = fake_edge.instances[0]
    assert com.text == "Привет! Вот ссылка." and com.voice == "ru-RU-SvetlanaNeural"


async def test_synth_respects_max_chars(cfg, fake_edge):
    tts = TTS(with_cfg(cfg, tts_max_chars=120))
    await tts.synth("Короткая фраза номер один. " * 30)
    sent = fake_edge.instances[0].text
    assert len(sent) <= 120 and sent.endswith(T.CUT_SUFFIX)


async def test_synth_nothing_to_say(cfg, fake_edge):
    with pytest.raises(ValueError, match="нечего озвучивать"):
        await TTS(cfg).synth("🔥🔥 👍")
    with pytest.raises(ValueError):
        await TTS(cfg).synth("")
    assert fake_edge.instances == []


async def test_synth_service_errors(cfg, fake_edge):
    fake_edge.error = RuntimeError("websocket closed")
    with pytest.raises(TTSError, match="озвучка не удалась"):
        await TTS(cfg).synth("Привет")
    fake_edge.error = None
    fake_edge.chunks = [{"type": "WordBoundary", "offset": 0.0, "duration": 1.0, "text": "x"}]
    with pytest.raises(TTSError, match="пустой звук"):
        await TTS(cfg).synth("Привет")
