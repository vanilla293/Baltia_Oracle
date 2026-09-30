# -*- coding: utf-8 -*-
"""ПИФИЯ v5.4.2 «СВОБОДНЫЙ ПИЛОТ»: контекст, который видит ИИ, — без подмены решений кодом.

Итог общего совета приходит в промпты миссии, дозора и чата с шапкой «Совет {вид} от … МСК (N ч назад) — общий по
рынку» (вчерашний «вне рынка» не читается как сегодняшний), пустые picks — явной строкой, режим по умолчанию —
«режим не распознан»; стороны пиков — терпимо, непонятная сторона — заметкой, не молча. Память миссии видит, что
дали решения ждать (цена при решении → сейчас, %), а молчание модели — «решения не было», не ЖДАТЬ. Чат читает приказ
WAIT как «совет ждал: …; уровни: …». Демо-мок нейтрален: ротация ответов, не «всегда ждём» и не «всегда входим».
Ни сети, ни ключей, ни data/ (базы — во временном каталоге)."""
import asyncio
import pathlib
import tempfile
import time
from types import SimpleNamespace

import pytest

from backend import api_chat, bus, council, explain, mission, mock_ai, store_v5, watch
from backend.prompts_mission import system_lines


@pytest.fixture()
def tmp_db(monkeypatch):
    monkeypatch.setattr(store_v5, "DB_PATH", pathlib.Path(tempfile.mkdtemp(prefix="pythia_ctx_")) / "t.db")
    store_v5._schema()
    yield


def _row(hours_ago: float, summary: dict, kind: str = "daily") -> dict:
    ts = time.time() - hours_ago * 3600
    return {"run_id": f"{kind}-1", "kind": kind, "ts": ts, "status": "done",
            "data": {"kind": kind, "ts": ts, "summary": summary}}


# ── 1. итог совета: шапка с возрастом, пустые входы, режим не распознан ──────────
def test_summary_text_row_header_age_and_empty_picks():
    row = _row(26, {"regime": "risk-off", "summary": "ставка давит", "picks": [],
                    "avoid": [{"ticker": "SBER", "why": "ставка"}]})
    t = council.summary_text(row)
    head = t.splitlines()[0]
    assert head.startswith("Совет daily от ") and head.endswith("МСК (26 ч назад) — общий по рынку"), head
    assert "Режим: risk-off" in t and "Входов совет не назвал" in t and "Избегать: SBER" in t
    # голый summary — без шапки, как раньше (кроме явной строки о пустых входах)
    bare = council.summary_text(row["data"]["summary"])
    assert bare.startswith("Режим: risk-off") and t.endswith(bare)
    # свежий совет — в минутах
    assert "(5 мин назад)" in council.summary_text(_row(5 / 60, {"regime": "risk-on", "picks": []}, "update"))


def test_summary_text_regime_fallback_marked():
    v = council.validate_summary({"summary": "без режима", "picks": []})
    assert v["regime"] == "смешанно" and "regime_raw" in v            # форма §2.5 цела, пометка — отдельным ключом
    t = council.summary_text(v)
    assert "режим не распознан" in t and "Режим: смешанно" not in t, t
    v2 = council.validate_summary({"regime": "бычий", "picks": []})
    assert "режим не распознан (в ответе совета «бычий»)" in council.summary_text(v2)
    v3 = council.validate_summary({"regime": "Risk-On", "picks": []})
    assert v3["regime"] == "risk-on" and "regime_raw" not in v3 and "режим не распознан" not in council.summary_text(v3)


# ── 2. стороны пиков — терпимо; непонятные — в заметку, не молча ─────────────────
def test_validate_summary_tolerant_sides(tmp_db):
    dropped: list = []
    s = council.validate_summary({"regime": "risk-on", "picks": [
        {"ticker": "SBER", "side": "buy_now"}, {"ticker": "GAZP", "side": "ЛОНГ (покупка)"},
        {"ticker": "LKOH", "side": "LONG "}, {"ticker": "BR", "side": "sell"}, {"ticker": "SI", "side": "шорт"},
        {"ticker": "MX", "side": "flat"}, {"ticker": "GD", "side": "не лонг"}]}, dropped=dropped)
    assert [(p["ticker"], p["side"]) for p in s["picks"]] == [
        ("SBER", "long"), ("GAZP", "long"), ("LKOH", "long"), ("BR", "short"), ("SI", "short")]
    assert dropped == ["MX «flat»", "GD «не лонг»"]


def test_deliberate_logs_dropped_pick_as_stage_note(tmp_db, monkeypatch):
    events: list = []

    async def sink(ev):
        events.append(ev)

    class FakeAI:
        async def pro_stream(self, system, user, *, on_think=None, on_text=None, route="pro"):
            return f"{route}: SBER лонг"

        async def pro_json(self, system, user, *, route="pro", max_tokens=None):
            return {"regime": "risk-on", "picks": [{"ticker": "SBER", "side": "Long"},
                                                    {"ticker": "GAZP", "side": "держать"}]}

        def usage(self):
            return {}

    monkeypatch.setattr(council, "ai_v5", FakeAI())
    monkeypatch.setattr(bus, "_sink", sink)
    rid = bus.start_run("daily")
    texts, summary = asyncio.run(council._deliberate("daily", rid, ("s", "u"), "потоки", ""))
    bus.end_run(rid)
    assert [p["ticker"] for p in summary["picks"]] == ["SBER"]
    notes = [e for e in events if e.get("stage") == "summary" and e.get("status") == "progress"]
    assert notes and notes[0]["detail"] == "пик без стороны: GAZP «держать»", notes


# ── 3. строка council_latest доходит до промптов миссии, дозора и чата ───────────
def test_mission_council_text_passes_row(monkeypatch):
    row = _row(3, {"regime": "смешанно", "summary": "вне рынка", "picks": []})
    monkeypatch.setattr(council, "latest", lambda: row)
    monkeypatch.setattr(mission, "council", council, raising=False)
    t = mission._council_text()
    assert t.startswith("Совет daily от ") and "(3 ч назад) — общий по рынку" in t and "Входов совет не назвал" in t, t


def test_watch_impact_prompt_carries_council_age(monkeypatch):
    row = _row(30, {"regime": "risk-off", "picks": []}, "update")
    monkeypatch.setattr(council, "latest", lambda: row)
    seen: dict = {}

    async def flash_json(system, user, *, think=False, route="flash", max_tokens=None):
        seen["user"] = user
        return {"changes": False, "severity": 20, "note": "шум", "affected": [], "recommend_rerun": False}

    monkeypatch.setattr(watch.ai_v5, "flash_json", flash_json)
    items = [{"id": "a1b2c3" + "0" * 34, "ts": time.time(), "source": "rbc", "title": "ЦБ сохранил ставку"}]
    out = asyncio.run(watch.impact("watch-1", items))
    assert out["severity"] == 20
    assert "Совет update от " in seen["user"] and "(30 ч назад) — общий по рынку" in seen["user"], seen["user"][:600]


def _chat_snap(exec_: dict, phase: str = "idle", reviews=None, guards=None) -> dict:
    return {"active": "SBER", "missions": {"SBER": {
        "ticker": "SBER", "name": "Сбербанк", "asset_class": "share", "play": "auto", "phase": phase,
        "price": 300.0, "started_ts": time.time(), "exec_ts": time.time(), "exec": exec_,
        "pilot": {"state": "ОЖИДАНИЕ", "last_action": "вне рынка", "position": None, "guards": guards or []},
        "reviews": reviews or []}}}


def test_chat_wait_order_and_silence(monkeypatch):
    snap = _chat_snap({"do": "WAIT", "entry": None, "entry_kind": "сейчас", "take": None, "invalidation": None,
                       "wait_for": "пробой 305 на объёме", "levels": [298, 305.0], "why": "коридор"},
                      reviews=[{"ts": time.time(), "choice": "НЕТ_ОТВЕТА", "why": "таймаут"},
                               {"ts": time.time(), "choice": "ЖДЁМ", "why": "коридор 298–305"}],
                      guards=[{"ts": time.time(), "decision": "ЖДАТЬ", "why": "ложный прокол"}])
    monkeypatch.setitem(api_chat._STUBS, "mission", SimpleNamespace(snapshot=lambda: snap))
    txt, meta = api_chat._mission_text()
    assert meta["ticker"] == "SBER"
    # ревью 5.4.3: подача WAIT как в миссии 5.4.3 — ориентир совета (не условие), уровни — будильник кода
    assert "фаза: вне рынка, плана нет" in txt
    assert ": WAIT — вне рынка. Ориентир совета (не условие): пробой 305 на объёме." in txt, txt
    assert "Будильник кода у уровней 298, 305 — проход цены будит дежурного PRO, это не вход." in txt, txt
    assert "совет ждал" not in txt and "Пилот: вне рынка, плана нет;" in txt, txt
    assert "вход сейчас" not in txt and "стоп None" not in txt and "тейк None" not in txt
    assert "Перепроверка" in txt and "ответа не было — решения не было" in txt and "ЖДЁМ — коридор 298–305" in txt
    assert "НЕТ_ОТВЕТА —" not in txt and "Ответ у троса" in txt and "FLASH у троса" not in txt
    # итог совета в чате — одна шапка от summary_text (строка council_latest)
    row = _row(2, {"regime": "risk-on", "picks": []})
    monkeypatch.setitem(api_chat._STUBS, "council", SimpleNamespace(latest=lambda: row,
                                                                    summary_text=council.summary_text))
    ct = api_chat._council_text()
    assert ct.startswith("Совет daily от ") and "(2 ч назад)" in ct and ct.count("Совет daily") == 1, ct


# ── 4. память миссии: что дало ожидание; молчание — не решение ────────────────────
def _mem_mission(now: float) -> SimpleNamespace:
    return SimpleNamespace(
        ticker="SBER", name="Сбербанк", memory="", memory_ts=None,
        reviews=[{"ts": now, "choice": "ЖДЁМ", "why": "коридор", "price": 100.0},
                 {"ts": now, "choice": "НЕТ_ОТВЕТА", "why": "таймаут 1200 с", "price": 100.5, "silent": True},
                 {"ts": now, "choice": "НЕ_РАЗОБРАН", "why": "«может быть»", "price": 100.6, "silent": True}],
        handoffs=[],
        pilot=SimpleNamespace(guards=[], profits=[],
                              gates=[{"ts": now, "decision": "ЖДАТЬ", "why": "откат к 99.7", "price": 100.0},
                                     {"ts": now, "decision": "ЖДАТЬ", "why": "PRO не ответил", "price": 100.2,
                                      "silent": True}]))


def test_memory_shows_what_waiting_gave_and_silence_is_not_wait():
    m = _mem_mission(time.time())
    acc = explain._accumulated(m, {"price": 101.4})
    # 5.4.3: ожидание вне рынка — в чью пользу: «лонг отсюда …, шорт …» (tests/test_decisive_door.py)
    assert "ЖДЁМ @100.0 → сейчас 101.4 (лонг отсюда +1.4 %, шорт -1.4 %) — коридор" in acc, acc
    assert "Проверки входа у двери: " in acc and "ЖДАТЬ @100.0 → сейчас 101.4 (+1.4 %) — откат к 99.7" in acc
    assert acc.count("(модель не ответила — решения не было)") == 2, acc
    assert "(ответ модели не разобран — решения не было)" in acc
    assert "НЕТ_ОТВЕТА" not in acc and "PRO не ответил" not in acc and "таймаут" not in acc
    s, _ = explain.memory_prompt({"ticker": "SBER", "name": "Сбербанк"}, "тест", acc, "")
    assert "что дали решения — вход, выход, отмена, ожидание, удержание: цена при решении и куда она ушла после" in s
    assert s.count("\n") < 12 and system_lines(s) <= 12


# ── 5. демо-мок нейтрален: ротация по номеру вызова ────────────────────────────────
def test_mock_demo_is_neutral(monkeypatch):
    monkeypatch.setattr(mock_ai, "_TURNS", {})
    monkeypatch.setattr(mock_ai, "_CALLS", {})
    rv_u = ("ДОПУСТИМЫЕ choice: КУПИТЬ_СЕЙЧАС | ЖДЁМ | ПРОДАТЬ_СЕЙЧАС | НОВЫЙ_АНАЛИЗ\n\n═══ СИТУАЦИЯ ПИЛОТА ═══\n"
            "Цена сейчас: 200\nПозиции нет, засады нет — полностью вне рынка")
    rv = [mock_ai._answer("mission_review", "s", rv_u, True) for _ in range(3)]
    assert [x["choice"] for x in rv] == ["ЖДЁМ", "КУПИТЬ_СЕЙЧАС", "НОВЫЙ_АНАЛИЗ"]
    assert abs(rv[1]["invalidation"] - 198.0) < 1e-6
    pos = [mock_ai._answer("mission_review", "s", "Цена сейчас: 210\nПОЗИЦИЯ: long 3 лот @200", True)["choice"]
           for _ in range(2)]
    assert sorted(pos) == ["ЖДЁМ", "ЗАКРЫТЬ"]
    door = [mock_ai._answer("mission_entry", "s", "Цена сейчас: 200", True)["decision"] for _ in range(3)]
    assert door == ["ВОЙТИ", "ЖДАТЬ", "ВОЙТИ"]
    prof = [mock_ai._answer("mission_profit", "s", "Цена сейчас: 210", True)["decision"] for _ in range(3)]
    assert prof == ["ДЕРЖАТЬ", "ВЫЙТИ", "ДЕРЖАТЬ"]
    ex = [mock_ai._answer("mission_exec", "… Цена 200.", "ОБЪЕКТ: SBER", True) for _ in range(3)]
    assert [x["do"] for x in ex] == ["BUY", "BUY", "WAIT"] and ex[2]["wait_for"] and ex[2]["levels"]
    for x in ex + rv:
        assert "перевес есть" not in x["why"] and "вход сейчас" not in x["why"]
