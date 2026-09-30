# -*- coding: utf-8 -*-
"""ПИФИЯ 5.4.3 — три находки стенда промптов (backend/prompt_bench.py), исправленные в боевом коде mission.py:

1. Вайкофф у перепроверки устаревший: дежурный PRO видел слой времени совета (m.layers) с расстояниями от цены совета и
   без метки времени. Свежий расчёт по свечам у перепроверки не дёшев (живой снимок market_ctx.light свечей не несёт) —
   поэтому честная подпись «ВАЙКОФФ (на момент совета HH:MM, N мин назад, цена тогда P)» (m.wy_at из market_ctx.build:
   ts, price, wyckoff_box) и первой строкой — расстояния до краёв бокса от ТЕКУЩЕЙ цены (market_ctx.render_wyckoff_now:
   числа только из краёв бокса и цены).
2. Свежая новость по тикеру шла дважды — в «свежие с …» и «по инструменту»: теперь одна новость — один раз (перепроверка
   _gather_news и узлы у денег _news_quick).
3. «Последний полный совет: N мин назад» считалось от начала совета — модель видела «50 мин назад» при приказе 12 мин
   назад: теперь «приказ пришёл N мин назад (совет шёл M мин, начат HH:MM)»; совет без приказа — так и сказано.
На фейках, без сети, ключей и data/."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend import ai_pilot, config, market_ctx, mission, trader_risk
from backend import prompts_mission as pm


class Broker:
    mode = "dry"

    async def portfolio(self):
        return {"mode": "dry", "positions": []}


class FakeAI:
    def __init__(self):
        self.answers: dict = {}
        self.calls: list = []

    def queue(self, route, *answers):
        self.answers.setdefault(route, []).extend(answers)

    def last(self, route):
        return [(s, u) for r, s, u in self.calls if r == route][-1]

    async def pro_json(self, system, user, *, route="pro", max_tokens=None, attempt_timeout=None):
        self.calls.append((route, system, user))
        q = self.answers.get(route) or []
        return q.pop(0) if q else {}

    def note_error(self, text, route=""):
        pass


class News:
    """newsflow без базы: свежие с момента since и новости инструмента — из списков теста."""

    def __init__(self, fresh, own):
        self.fresh, self.own = fresh, own

    def fresh_since(self, since):
        return list(self.fresh)

    def news_for_ticker(self, ticker, name, days=3, limit=None):
        return list(self.own)

    @staticmethod
    def render(items, limit=None, rich=False):
        return "\n".join(f"[{x['id']}] {x['title']}" for x in items)


@pytest.fixture(autouse=True)
def fake(monkeypatch, tmp_path):
    monkeypatch.setattr(mission, "_M", {})
    monkeypatch.setattr(mission, "explain", None)
    monkeypatch.setattr(mission.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mission.store_v5, "mission_put", lambda ticker, data: None)
    monkeypatch.setattr(mission.bus, "stage", AsyncMock())
    monkeypatch.setattr(mission.market_ctx, "light", AsyncMock(return_value={"text": "живой рынок: тест"}))
    monkeypatch.setattr(mission, "_council_text", lambda: "итог совета: тест")
    monkeypatch.setattr(mission, "_scan_text", lambda m: "")
    monkeypatch.setattr(mission, "_mod", lambda name: None)
    try:
        from backend import astro
        monkeypatch.setattr(astro, "acontext", AsyncMock(return_value=None))
    except Exception:                                  # noqa: BLE001 — без неба перепроверка живёт
        pass
    monkeypatch.setattr(config, "PYTHIA_PARTNERS", False, raising=False)
    monkeypatch.setattr(ai_pilot, "market_clock", None)
    f = FakeAI()
    monkeypatch.setattr(mission.ai_v5, "pro_json", f.pro_json)
    monkeypatch.setattr(mission.ai_v5, "note_error", f.note_error)
    return f


def make_pilot():
    m = mission.Mission("TEST", "Тест", "futures", "auto", 100000.0)
    mission._M["TEST"] = m
    p = mission.MissionPilot("TEST", deposit=100000.0, broker=Broker(), mission=m)
    p.figi, p.asset_class = "F", "futures"
    p.go_per_lot, p.tick_size, p.point_value = 12000.0, 0.01, 1.0
    p.session_risk = trader_risk.SessionRisk(100000.0)
    p._state_path = None
    p._prepared = True
    p.reanalyze_cb = AsyncMock()
    m.pilot = p
    m.task = SimpleNamespace(done=lambda: False)
    return m, p


WY = {"hourly": {"bars": 300, "read": "ХАРАКТЕР НЕ ОПРЕДЕЛЁН", "phase": "B — построение причины", "bias": 3,
                 "box_low": 98.0, "box_high": 102.0, "box_atr": 4.0, "vol_character": "паритет", "events_all": []}}


# ── 1. Вайкофф у перепроверки: подпись времени и цены совета, расстояния от текущей цены ─────────────────────────────
def test_review_wyckoff_is_labelled_and_measured_from_the_live_price(fake, monkeypatch):
    async def scenario():
        m, p = make_pilot()
        t0 = time.time() - 1800
        ctx = {"ts": t0, "price": 99.0, "wyckoff_box": market_ctx.wyckoff_box(WY)}   # форма market_ctx.build
        m.layers = {"wyckoff": market_ctx.render_wyckoff(WY, 99.0)}
        m.wy_at = mission._wyckoff_at(ctx, t0 + 5)
        assert m.wy_at == {"ts": t0, "price": 99.0, "box": {"H1": {"lo": 98.0, "hi": 102.0, "atr": 4.0}}}
        p.prices.append(101.0)
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "середина"})
        await p._review(101.0)
        s, u = fake.last("mission_review")
        head = f"═══ ВАЙКОФФ (на момент совета {p._hhmm(t0)}, 30 мин назад, цена тогда 99) ═══\n"
        now_line = ("СЕЙЧАС от цены 101 (края бокса — расчёт совета): H1 бокс 98 … 102: цена на 75% высоты бокса; до льда "
                    "-2.97% (-3, ~3.0 ATR), до крика +0.99% (+1, ~1.0 ATR) → ближе к крику\n")
        assert head + now_line + "ВАЙКОФФ H1 (300 баров)" in u, u
        assert "цена 99 на 25% высоты бокса" in u, "слой совета — как был посчитан, под своей подписью"
        assert "ВАЙКОФФ (на момент разбора)" not in u and "Вайкофф на момент разбора" not in s
        assert "Вайкофф последнего совета (когда и при какой цене посчитан — в заголовке блока)" in s
        assert pm.system_lines(s) <= pm.SYSTEM_MAX_LINES
        # состояние до 5.4.3 (метки нет) — честно «время расчёта не сохранено», без выдуманных расстояний
        m.wy_at = None
        p.prices.append(101.0)
        fake.queue("mission_review", {"choice": "ЖДЁМ", "why": "середина"})
        await p._review(101.0)
        _, u2 = fake.last("mission_review")
        assert "═══ ВАЙКОФФ (расчёт последнего совета; время расчёта не сохранено) ═══\nВАЙКОФФ H1" in u2
        assert "СЕЙЧАС от цены" not in u2

    asyncio.run(scenario())


def test_wyckoff_mark_survives_restart(monkeypatch):
    saved = {}
    monkeypatch.setattr(mission.store_v5, "mission_put", lambda ticker, data: saved.update(data))
    monkeypatch.setattr(mission.store_v5, "mission_get", lambda ticker: dict(saved))
    m, _p = make_pilot()
    m.wy_at = {"ts": 1_790_000_000.0, "price": 99.0, "box": {"H1": {"lo": 98.0, "hi": 102.0, "atr": 4.0}}}
    assert mission._persist(m)
    assert mission._from_store("TEST").wy_at == m.wy_at
    assert mission._wyckoff_at({"price": 99.0, "ts": 5.0}) == {"ts": 5.0, "price": 99.0, "box": {}}
    assert mission._wyckoff_at({}) is None


# ── 2. одна новость — один раз ──────────────────────────────────────────────────────────────────────────────────────
def test_fresh_ticker_news_is_shown_once(fake, monkeypatch):
    now = time.time()
    fresh = {"id": "a1", "ts": now - 60, "title": "ЦБ снизил ставку"}
    old = {"id": "b2", "ts": now - 7200, "title": "Минфин разместит ОФЗ"}

    async def scenario(nf, want_own):
        monkeypatch.setattr(mission, "_mod", lambda name: nf if name == "newsflow" else None)
        m, p = make_pilot()
        p._last_review_ts = now - 600
        txt = await p._gather_news()
        assert txt.count("ЦБ снизил ставку") == 1 and ("— свежие с " in txt) == bool(nf.fresh), txt
        assert want_own in txt, txt
        q = p._news_quick()                          # трос / дверь / прибыль — то же правило
        assert q.count("ЦБ снизил ставку") == 1 and want_own in q, q

    asyncio.run(scenario(News([fresh], [fresh, old]), "— по инструменту (кроме свежих выше):\n[b2] Минфин разместит ОФЗ"))
    asyncio.run(scenario(News([fresh], [fresh]), "— по инструменту: только свежие выше"))
    asyncio.run(scenario(News([], [fresh, old]), "— по инструменту:\n[a1] ЦБ снизил ставку\n[b2] Минфин разместит ОФЗ"))
    # без id — ключ по заголовку и времени
    assert mission._news_not_shown([dict(fresh, id=None)], [dict(fresh, id=None)]) == ([], 1)


# ── 3. возраст совета — от приказа ──────────────────────────────────────────────────────────────────────────────────
def test_council_age_counts_from_the_order():
    async def scenario():
        m, p = make_pilot()
        now = time.time()
        m.council_ts, m.council_dur, m.exec_ts = now - 50 * 60, 38 * 60.0, now - 12 * 60   # совет 50 мин назад, 38 мин
        sit = p._situation_text(100.0)
        assert (f"Последний полный совет: приказ пришёл 12 мин назад (совет шёл 38 мин, начат {p._hhmm(m.council_ts)}); "
                "пока совет идёт, пилот не входит") in sit, sit
        assert "Последний полный совет: 50 мин назад" not in sit
        p._reanalyzing = True                                          # совет запрошен — прошлый так же честно
        sit = p._situation_text(100.0)
        assert (f"Полный совет запрошен, пилот ждёт его; прошлый — приказ пришёл 12 мин назад (совет шёл 38 мин, начат "
                f"{p._hhmm(m.council_ts)}); пока совет идёт") in sit, sit
        p._reanalyzing = False
        m.exec_ts = now - 3 * 3600                                     # совет упал: приказ старше совета
        sit = p._situation_text(100.0)
        assert f"Последний полный совет: начат {p._hhmm(m.council_ts)} (50 мин назад), шёл 38 мин, нового приказа не дал" \
            in sit, sit
        m.council_task = asyncio.get_running_loop().create_future()   # совет идёт — строка прежняя
        sit = p._situation_text(100.0)
        assert f"Полный совет идёт с {p._hhmm(m.council_ts)} (уже 50 мин); прошлый длился 38 мин" in sit, sit
        m.council_task.cancel()

    asyncio.run(scenario())
