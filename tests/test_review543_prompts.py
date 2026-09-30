# -*- coding: utf-8 -*-
"""Ревью 5.4.3 «решительный пилот», часть F2 — промпты и данные: промпт обещает только то, что делает код.

Воля владельца 30.09.2026: лёгкий крен ИИ к действию («прорыва нет — сидим ждём» надоело), без возврата «входит не
глядя». Ревью нашло, где текст промпта расходится с кодом, и где слои 4.x несут приказы в данных. Здесь закреплено:
- будильник ЖДЁМ (D1–D3): уровень — только в поле entry, без позиции и без взведённого входа; ЖДЁМ без entry оставляет
  несработавший будильник, другой entry переставляет; время будильник не ставит; сработавший уровень снова будит не
  раньше PYTHIA_EVENT_COOL_SEC (D2);
- ДЕРЖАТЬ в позиции (D6): новые invalidation/take или null, прежние числа не повторять;
- трос (D5, по коду F1): hold_until_price строго между аварийным тросом и ценой — новый триггер, у него вопрос сразу
  (hold_minutes не действует); не принят — триггер прежний, вопрос снова через hold_minutes (без него
  PYTHIA_SOFT_STOP_GRACE_SEC), до того держит аварийный трос; на лимите ожиданий — слив по правилу сразу; бремя чисел у
  обоих ответов;
- засада совета (D4): пилот взводит, у двери сверяет дежурный, живёт PLAN_TTL_SEC от приказа — числа из кода; засада
  перепроверки — правило свежести как есть;
- дверь: уровень и срок вместе — вход у уровня не раньше срока; прибыль: условие принятия lock_price; триаж: цена
  СЕЙЧАС — окно событий; прокол: «засада за полосой» — толкование школы;
- итог совета переводит вердикт 1:1 («смотреть» не становится входом);
- market_ctx: гейт Курамото, причина оракула и директива — фактом, правила машин 4.x — «обычно читают так».
Тексты сверяются с поведением кода на фейках (трос, дверь, прибыль, окно событий, срок засады). Ни сети, ни ключей,
ни data/."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend import ai_pilot, config, market_ctx, mission, trader_risk
from backend import prompts_council as pc
from backend import prompts_mission as pm

TIME = "30.09.2026 12:35 МСК, среда"
KW = dict(situation="Цена 100, позиции нет", light="стакан", news="", council_text="итог", time_msk=TIME)


def _review(in_pos=False, side=None, **kw):
    return pm.review("TEST", "Тест", "auto", situation="Цена 100", light="", council_text="", prev_exec="", news="",
                     watch="", astro_line="", in_pos=in_pos, side=side, time_msk=TIME, **kw)


def _systems() -> dict:
    ctx = {"time_msk": TIME, "price": 100.0}
    return {
        "verdict": pm.verdict("TEST", "Тест", "auto", "а", "к", ctx)[0],
        "exec_order": pm.exec_order("TEST", "Тест", "auto", 100.0, "в", ctx)[0],
        "review": _review()[0], "review_pos": _review(True, "long")[0],
        "stop_guard": pm.stop_guard("TEST", "Тест", "long", trigger="t", history="", plan="", **KW)[0],
        "entry_check": pm.entry_check("TEST", "Тест", "long", plan="BUY", history="", **KW)[0],
        "profit_think": pm.profit_think("TEST", "Тест", "long", profit="p", history="", plan="", **KW)[0],
        "event_triage": pm.event_triage("TEST", "Тест", "auto", event="e", situation="s", history="", light="",
                                        plan="", news="", time_msk=TIME)[0],
        "summary": pc.summary("вердикт")[0],
    }


# ── тексты: закон 3 держится во всех вариантах настроек ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("door", [True, False])
def test_all_changed_systems_fit_law3(monkeypatch, door):
    monkeypatch.setattr(config, "PYTHIA_ENTRY_CHECK", door, raising=False)
    for name, s in _systems().items():
        assert pm.system_lines(s) <= pm.SYSTEM_MAX_LINES, (name, pm.system_lines(s))
        assert not any(b in s.lower() for b in pm.BANNED), name
        assert "МСК" in s and pm.VOICE in s, name
        if name not in ("verdict",):
            assert "json" in s.lower() and "строго один JSON-объект" in s, name
        for promise in ("исполнит сам", "исполнит его сам", "за ним ты выходишь", "трос и тейк встанут туда",
                        "прибыль ниже него не отдаём", "стоит минуты", "даже если вердикт назвал её «смотреть»"):
            assert promise not in s, (name, promise)


# ── D1–D3: будильник ЖДЁМ ────────────────────────────────────────────────────────────────────────────────────────
def test_review_alarm_promise_is_what_code_does(monkeypatch):
    s, _ = _review()
    for piece in ("Уровень — в поле entry: без позиции и без взведённого входа это будильник, а не вход",
                  "ЖДЁМ без entry оставляет несработавший будильник", "другой entry его переставляет",
                  "вход и НОВЫЙ_АНАЛИЗ снимают", "время будильник не ставит — к нему вернёшься на плановом взгляде",
                  "сработавший уровень снова будит не раньше чем через 15 мин",
                  "в why — почему не засада и какое число (уровень или время МСК) изменит решение",
                  '"why":"1-3 предложения с числами; при ЖДЁМ — почему не засада и какое число изменит решение '
                  '(уровень будильника — в entry)"'):
        assert piece in s, piece
    # уровень больше не направляется в why (раньше: «в why назови уровень … entry — будильник» — модель писала уровень
    # в why, и будильника не было); плановый взгляд — тот же, что в первой строке
    assert "в why назови уровень" not in s and "при ЖДЁМ — уровень или время МСК" not in s
    assert "следующий плановый взгляд — через 30 мин" in s
    monkeypatch.setattr(config, "PYTHIA_EVENT_COOL_SEC", 600, raising=False)
    assert "снова будит не раньше чем через 10 мин" in _review()[0]
    monkeypatch.setattr(config, "PYTHIA_EVENT_COOL_SEC", 0, raising=False)
    s0 = _review()[0]
    assert "снова будит не раньше" not in s0 and "код разбудит тебя; ЖДЁМ без entry" in s0


# ── D6: ДЕРЖАТЬ в позиции ────────────────────────────────────────────────────────────────────────────────────────
def test_review_hold_asks_new_levels_or_null():
    s, _ = _review(True, "long")
    assert ("ДЕРЖАТЬ — держим; переставить трос или тейк — дай новые invalidation/take (у лонга стоп ниже цены, тейк "
            "выше, у шорта зеркально; не с той стороны код не примет — отказ увидишь в следующей ситуации), не меняешь "
            "— null: прежние числа не повторяй") in s
    assert "новые или прежние" not in s
    sv = pm.verdict("TEST", "Тест", "auto", "а", "к", {"time_msk": TIME})[0]
    assert "стоп и тейк числами, если переставляешь, иначе «прежние»" in sv and "новыми или прежними" not in sv
    se = pm.exec_order("TEST", "Тест", "auto", 100.0, "в", {"time_msk": TIME})[0]
    assert "invalidation/take — новые или null, если прежние" in se, "шифровальщик переводит «прежние» в null"


# ── D4: засада совета и перепроверки ─────────────────────────────────────────────────────────────────────────────
def test_council_ambush_formula_from_code(monkeypatch):
    amb = pm.council_ambush()
    assert amb == ("засаду пилот взведёт сам; перед заявкой дежурный сверит её с живым рынком у двери (ВОЙТИ / ЖДАТЬ / "
                   f"ОТМЕНИТЬ); невзятая засада снимается через {int(ai_pilot.PLAN_TTL_SEC // 60)} мин от приказа — "
                   "дальше решает дежурный PRO"), amb
    ctx = {"time_msk": TIME, "price": 100.0}
    sv = pm.verdict("TEST", "Тест", "auto", "а", "к", ctx)[0]
    se = pm.exec_order("TEST", "Тест", "auto", 100.0, "в", ctx)[0]
    assert f"а не «вне рынка»: {amb}; «вне рынка» — когда обе стороны хуже" in sv
    assert f"{pm.EXEC_RULE} З{amb[1:]}.\n" in se and "засаду пилот исполнит" not in se
    assert "entry X (засада), а не WAIT" in pm.EXEC_RULE and "у двери" not in pm.EXEC_RULE, "дверь — в формуле, не в правиле"
    monkeypatch.setattr(ai_pilot, "PLAN_TTL_SEC", 3600.0)              # срок — из кода, а не зашит в текст
    assert "снимается через 60 мин от приказа" in pm.council_ambush()
    assert "снимается через 60 мин от решения" in _review()[0]
    monkeypatch.setattr(config, "PYTHIA_ENTRY_CHECK", False, raising=False)
    off = pm.council_ambush()
    assert "у уровня пилот войдёт сам (проверка у двери выключена)" in off and "у двери (ВОЙТИ" not in off
    assert "вход — без проверки у двери: она выключена" in pm.exec_order("TEST", "Тест", "auto", 100.0, "в", ctx)[0]
    assert "дальше — план снят и вопрос снова к тебе; проверка у двери выключена" in pm.review_ambush()


def test_review_ambush_freshness_rule_as_is(monkeypatch):
    s = _review()[0]
    assert "засаду пилот взведёт сам (у уровня — по правилу свежести ниже)" in s
    assert ("вход из перепроверки пилот бьёт по твоему решению без второго вопроса у двери, пока решению не больше 20 мин "
            "и цена не ушла хуже снимка дальше 1 %; позже — сверка у двери; невзятая засада снимается через "
            f"{int(ai_pilot.PLAN_TTL_SEC // 60)} мин от решения") in s
    monkeypatch.setattr(config, "PYTHIA_ENTRY_FRESH_SEC", 600, raising=False)
    monkeypatch.setattr(config, "PYTHIA_ENTRY_DRIFT_PCT", 0.5, raising=False)
    assert "пока решению не больше 10 мин и цена не ушла хуже снимка дальше 0.5 %" in _review()[0]


# ── D5: трос ─────────────────────────────────────────────────────────────────────────────────────────────────────
def test_stop_guard_text_says_what_the_trigger_does(monkeypatch):
    """Текст троса = код F1 (AIPilot._apply_guard / tick): принятый hold_until_price — новый триггер, вопрос у него
    сразу, срок hold_minutes — только при прежнем триггере; лимит ожиданий — слив по правилу сразу."""
    s = _systems()["stop_guard"]
    for piece in ("Оба ответа равноправны", "бремя чисел у обоих: слом докажи числами (агрессор ленты, объём за "
                  "уровнем, нет возврата), вынос стопов — тоже числами (объём, лента, возврат за уровень), иначе это "
                  "надежда, а не картина",
                  "назови hold_until_price, и код сделает так: это новый триггер, если он строго между аварийным тросом "
                  "и ценой (у лонга трос < X < цена, у шорта цена < X < трос); цена его пройдёт — тебя спросят сразу, "
                  "hold_minutes тогда не действует",
                  "числа нет или оно вне этого коридора — триггер прежний (цена уже за ним), тебя спросят снова через "
                  "hold_minutes (1–60 мин; без него 3 мин), а до того позицию держит только аварийный трос",
                  "Каждое ЖДАТЬ идёт в лимит ожиданий: на лимите цена за триггером — слив по правилу сразу, без вопроса и "
                  "без срока; счёт заново, когда цена по безопасную сторону триггера, а с ответа прошло больше 3 мин"):
        assert piece in s, piece
    # старое обещание F2 («пока срок идёт, вопроса у триггера нет» и у нового триггера) код F1 не делает
    assert "пока срок идёт" not in s and "с безопасной стороны от цены и не за аварийным тросом" not in s
    monkeypatch.setattr(config, "PYTHIA_SOFT_STOP_GRACE_SEC", 300, raising=False)
    s5 = pm.stop_guard("TEST", "Тест", "long", trigger="t", history="", plan="", **KW)[0]
    assert "без него 5 мин" in s5 and "а с ответа прошло больше 5 мин" in s5


# ── дверь, прибыль, триаж, прокол ─────────────────────────────────────────────────────────────────────────────────
def test_door_wait_level_and_term(monkeypatch):
    """v5.4.4 (воля владельца «только триггеры и раз в 30 мин», код двери X2): ЖДАТЬ без уровня и срока, молчание и
    неразобранный ответ — следующий вопрос только после ответа дежурного PRO на перепроверке (было — через
    PYTHIA_ENTRY_CHECK_COOL_SEC); срок — не меньше 10 мин и не больше 3 на план; одобренный ВОЙТИ живёт до исполнения;
    условный ВОЙТИ — засада у уровня; дрейф — один переспрос."""
    s = _systems()["entry_check"]
    assert ("ЖДАТЬ — оставить деньги вне рынка до нового уровня (entry, entry_kind откат/прорыв), который цена вероятно "
            "даст по ленте (у него пилот войдёт без второго вопроса, пока ответу не больше 20 мин и цена не ушла от "
            "уровня хуже 1 %), и/или до срока (wait_minutes — не меньше 10 мин, не больше 3 сроков на план: до него ни "
            "входа, ни вопроса; с уровнем — вход у уровня не раньше срока); без того и другого (и если ответа нет или он "
            "не разобран) — следующий вопрос у двери только после ответа дежурного PRO на ближайшей перепроверке "
            "(плановой или по рыночному поводу); срок жизни засады (в блоке плана) ЖДАТЬ не продлевает") in s, s
    assert "вопрос снова через" not in s and "одобренный вход живёт до исполнения или снятия плана" in s
    assert "засада у уровня: вход у него без второго вопроса" in s and "второй ВОЙТИ исполняется, если" in s
    monkeypatch.setattr(config, "PYTHIA_ENTRY_CHECK_COOL_SEC", 600, raising=False)   # дверь эту настройку больше не читает
    assert pm.entry_check("TEST", "Тест", "long", plan="BUY", history="", **KW)[0] == s


def test_profit_lock_condition_in_text():
    s = _systems()["profit_think"]
    assert ("назови lock_price и при желании новую цель take (за ценой по ходу позиции); к lock_price код поднимет "
            "триггер, только если он между входом и ценой и лучше нынешнего триггера (иначе триггер не тронут — "
            "фактический в строке УРОВНИ ПОЗИЦИИ), у триггера — вопрос троса, а не выход сам собой") in s, s


def test_triage_cost_is_the_event_window(monkeypatch):
    s = _systems()["event_triage"]
    assert ("Не уверен, нужен ли PRO, — СЕЙЧАС: лишняя перепроверка стоит вызова PRO (минуты раздумий) и окна событий: "
            "следующее событие подтянет PRO не раньше чем через 15 мин после неё (прокол — через 10 мин), пропущенная — "
            "хода") in s, s
    monkeypatch.setattr(config, "PYTHIA_PUNCTURE_COOL_SEC", 900, raising=False)
    assert "(прокол" not in pm._triage_cost()
    monkeypatch.setattr(config, "PYTHIA_EVENT_COOL_SEC", 0, raising=False)
    monkeypatch.setattr(config, "PYTHIA_PUNCTURE_COOL_SEC", 0, raising=False)
    assert pm._triage_cost() == "лишняя перепроверка стоит вызова PRO (минуты раздумий)"


def test_puncture_interpretation_is_school():
    assert pm.PUNCTURE_ROLES["вне рынка"] == f"прокол вне рынка — возможный момент входа в сторону прокола {pm.PUNCTURE_FALSE}"
    assert pm.PUNCTURE_SCHOOL.startswith("Обычно читают так (толкование, не приказ):")
    assert "засада за полосой может не успеть исполниться" in pm.PUNCTURE_SCHOOL
    pb = pm.puncture_block("вверх", 0.8, 101.0, 101.4, role="вне рынка", price=100.0)
    role_line = [x for x in pb.splitlines() if x.startswith("Что это значит для нас")][0]
    assert "засада" not in role_line and pb.splitlines()[-1] == pm.PUNCTURE_SCHOOL, pb


# ── итог совета: перевод 1:1 ─────────────────────────────────────────────────────────────────────────────────────
def test_summary_translates_verdict_one_to_one():
    s = pc.summary("вердикт")[0]
    assert ("picks — входы вердикта (0–8 штук) ровно как в вердикте: идея, которой вердикт сам дал сторону, уровень (или "
            "событие) входа и то, что её ломает (стоп), — с её триггером и горизонтом; это перевод, не оценка") in s
    assert "то, что вердикт отнёс к «смотреть», идеи без стороны или без уровня входа — в watch" in s
    assert "Ничего не добавляй от себя" in s and "Своей оценки не добавляй" in s and "picks пустой" in s
    assert "даже если вердикт назвал её «смотреть»" not in s
    sv = pc.verdict("а", "к", "п", "☽")[0]
    assert "что смотреть — только идеи без стороны" in sv and "это вход с триггером, а не «смотреть»" in sv, \
        "классифицирует председатель, итог — переводит"


# ── market_ctx: слои 4.x — фактом, толкование — «обычно читают так» ─────────────────────────────────────────────────
@pytest.mark.parametrize("r,where", [(0.2, "ниже порога рассинхрона"), (0.6, "между порогами"),
                                     (0.9, "не ниже порога стадного захвата")])
def test_kuramoto_gate_is_fact_plus_school(r, where):
    t = market_ctx.render_kuramoto({"r": r, "r_min": 0.1, "n": 3, "bars": 300, "leader": "MX", "coupling": {"MX": 1.1},
                                    "word": "СТАДНЫЙ ЗАХВАТ: хор в фазе — каскады ходят широко"})
    fact, school = t.split("\n")
    assert where in fact and "СТАДНЫЙ ЗАХВАТ: хор в фазе." in fact and "каскады" not in fact, fact
    assert school.startswith("Обычно читают так (гейт 4.x — толкование, не приказ)"), school
    for bad in ("Гейт-смысл", "опасен", "разрешён", "доверять", "вход по"):
        assert bad not in t, bad


def test_oracle_and_directive_carry_no_orders():
    v = {"state": "У ПОРОГА", "dir": "flat", "p_long": 0.5, "confidence": 0.0, "mode": "momentum", "voices": {},
         "reason": "[У ПОРОГА] у порога, но среда — кислота (α=1.50≤2: дисперсия бесконечна): вход только по вектору "
                   "сингулярности, ждём"}
    dv = {"dir": "flat", "dir_word": "ВНЕ РЫНКА", "confidence": 0.0, "votes": {}, "sources": [],
          "regime": "бифуркация — среда готова к слому, читаем сигнал агрессивнее",
          "entry": "нет перевеса — вне рынка, ждём чистый сетап", "stop": "—", "invalidation": "—"}
    t = market_ctx.render_oracle(v, dv)
    assert "состояние машины: [У ПОРОГА] у порога, но среда — кислота (α=1.50≤2: дисперсия бесконечна)" in t, t
    assert "карта давлений 4.x («директива», не приказ): своего перекоса по голосам нет" in t and "режим бифуркация," in t
    assert t.splitlines()[-1] == market_ctx.ORACLE_SCHOOL
    for bad in ("ВНЕ РЫНКА", "вне рынка", "ждём", "наблюдаем", "вход только", "агрессивнее", "директива —", "зоны:"):
        assert bad not in t, (bad, t)
    assert market_ctx.oracle_reason_fact("[ХАОС] инфаркт спреда — вето всему, наблюдаем") == "[ХАОС] инфаркт спреда"
    assert market_ctx.oracle_reason_fact("[ПАРЛАМЕНТ] парламент ОТСТРАНЁН (α=1.9) — ждём вектор сингулярности") == \
        "[ПАРЛАМЕНТ] парламент ОТСТРАНЁН (α=1.9)"
    # сторона есть — «лонг», зоны стакана — цитатой школы, а не «зоны:» приказом
    t2 = market_ctx.render_oracle(dict(v, dir="long", reason="[ПАРЛАМЕНТ] парламент: P(вверх)=0.6 по 3 голосам"),
                                  dict(dv, dir="long", entry="вход у поддержки (вакуум под ценой 99.8)",
                                       stop="стоп под стеной bid 99.5"))
    assert "сторона голосов — лонг (вверх)" in t2 and ("школа обычно читает стакан так (толкование, не приказ): «вход у "
                                                       "поддержки (вакуум под ценой 99.8)»; «стоп под стеной bid 99.5»") in t2


def test_xray_posture_is_school_reading_not_quoted_order():
    """Поза классификатора микроструктуры 4.x («готовить вход», «вход … ДО тика», «не выходить», «ждать разворот»,
    «рыночные входы запрещены») — не цитатой в данные, а толкованием класса без повелительных слов."""
    import inspect
    import re
    from backend import microstructure
    actors = re.findall(r'actor = "([^"]+)"', inspect.getsource(microstructure.classify))
    postures = re.findall(r'posture = "([^"]+)"', inspect.getsource(microstructure.classify))
    assert len(actors) == len(postures) >= 7
    for a, pos in zip(actors, postures):
        n = market_ctx._posture_note({"signals": ["s"], "actor": a, "posture": pos})
        assert pos not in n and "«" not in n, (a, n)
        for bad in ("готовить", "ждать", "не выходить", "запрещен", "лимитки на", "фиксировать", "вход в сторону"):
            assert bad not in n, (bad, n)
        if a != "толпа-шум":
            assert n.startswith("школа микроструктуры обычно читает так (толкование, не приказ): "), n
    assert market_ctx._posture_note({"signals": [], "actor": "КИТ грузит айсберг"}) == \
        "явного давления крупного игрока классификатор не видит"


def test_real_oracle_reasons_lose_their_orders():
    """Все формы причины из backend/oracle.py (verdict: ХАОС / У ПОРОГА / ПАРЛАМЕНТ) — без хвоста-приказа."""
    import inspect
    from backend import oracle
    src = inspect.getsource(oracle.verdict)
    assert "ждём" in src and "наблюдаем" in src and "вход только по" in src, "формы причин оракула 4.x на месте"
    samples = ("[ХАОС] инфаркт спреда — вето всему, наблюдаем",
               "[У ПОРОГА] у порога, но среда — кислота (α=1.20≤2: дисперсия бесконечна, линейное усреднение голосов "
               "недействительно): вход только по вектору сингулярности, ждём",
               "[У ПОРОГА] у порога, сторона слома не названа — вход только по сильному согласию парламента (0.31)",
               "[У ПОРОГА] у порога: давление есть, сторона не названа, согласия нет — ждём",
               "[ПАРЛАМЕНТ] парламент ОТСТРАНЁН (α=2.40 тяжёлая И φ=0.950≥0.9 — критическое замедление: переход уже "
               "идёт) — ждём вектор сингулярности")
    for r in samples:
        f = market_ctx.oracle_reason_fact(r)
        for bad in ("ждём", "наблюдаем", "вход только", "вето"):
            assert bad not in f, (bad, f)
        assert f.startswith(r.split("]")[0] + "]"), f
    assert market_ctx.oracle_reason_fact(samples[2]).endswith("; согласие парламента 0.31")


# ── тексты сверены с кодом: пилот на фейках ──────────────────────────────────────────────────────────────────────
class Broker:
    mode = "real"

    def __init__(self):
        self.placed = []
        self._n = 0

    async def place(self, figi, direction, lots, price=None, tag="", **kw):
        self._n += 1
        self.placed.append({"order_id": f"F-{self._n}", "direction": direction, "lots": int(lots), "tag": tag})
        return {"ok": True, "order_id": f"F-{self._n}"}

    async def order_state(self, oid, **kw):
        return {"ok": True, "filled": True, "status": "FILL", "exec_lots": 0}

    async def cancel(self, oid, **kw):
        return {"ok": True}

    async def place_stop(self, figi, direction, lots, stop_price, tag="", **kw):
        return {"ok": True, "stop_order_id": "S-1"}

    async def cancel_stop(self, sid):
        return {"ok": True}

    async def stop_orders(self, *, strict=False):
        return []

    async def max_lots(self, figi, price=None):
        return None

    async def portfolio(self):
        return {"mode": "dry", "positions": []}


class FakeMoney:
    def __init__(self):
        self.answers: dict = {}
        self.calls: list = []

    def queue(self, route, *answers):
        self.answers.setdefault(route, []).extend(answers)

    def count(self, route):
        return sum(1 for r in self.calls if r == route)

    async def money_json(self, system, user, *, route, max_tokens=None, attempt_timeout=None):
        self.calls.append(route)
        q = self.answers.get(route) or []
        return q.pop(0) if q else {}

    def note_error(self, text, route=""):
        pass


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setattr(mission, "_M", {})
    monkeypatch.setattr(mission, "explain", None)
    monkeypatch.setattr(mission.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mission.store_v5, "mission_put", lambda ticker, data: None)
    monkeypatch.setattr(mission.store_v5, "trade_add", lambda rec: None)
    monkeypatch.setattr(mission.ledger, "schedule_after_close", Mock())
    monkeypatch.setattr(mission.ledger, "estimate", lambda rec: 0.0)
    monkeypatch.setattr(mission.bus, "stage", AsyncMock())
    monkeypatch.setattr(mission.market_ctx, "light", AsyncMock(return_value={"text": "живой рынок: тест"}))
    monkeypatch.setattr(mission, "_council_text", lambda: "итог совета: тест")
    monkeypatch.setattr(mission, "_scan_text", lambda m: "")
    monkeypatch.setattr(mission, "_mod", lambda name: None)
    for k, v in (("PYTHIA_PARTNERS", False), ("PYTHIA_EXCHANGE_STOP", False), ("PYTHIA_ENTRY_CHECK", True),
                 ("PYTHIA_PROFIT_THINK", False), ("PYTHIA_MONEY_MODEL", "pro"), ("PYTHIA_SOFT_STOP", True),
                 ("PYTHIA_SOFT_TAKE", True), ("PYTHIA_ENTRY_FRESH_SEC", 1200), ("PYTHIA_ENTRY_DRIFT_PCT", 1.0),
                 ("PYTHIA_ENTRY_CHECK_COOL_SEC", 300), ("PYTHIA_SOFT_STOP_MAX_HOLDS", 3),
                 ("PYTHIA_SOFT_STOP_GRACE_SEC", 180), ("PYTHIA_EVENT_COOL_SEC", 900), ("PYTHIA_PUNCTURE_COOL_SEC", 600)):
        monkeypatch.setattr(config, k, v, raising=False)
    monkeypatch.setattr(ai_pilot, "market_clock", None)             # часы выключены: рынок открыт
    f = FakeMoney()
    monkeypatch.setattr(mission.ai_v5, "money_json", f.money_json)
    monkeypatch.setattr(mission.ai_v5, "note_error", f.note_error)
    return f


def make_pilot():
    m = mission.Mission("TEST", "Тест", "futures", "auto", 100000.0)
    mission._M["TEST"] = m
    p = mission.MissionPilot("TEST", deposit=100000.0, broker=Broker(), mission=m)
    p.figi, p.asset_class = "F", "futures"
    p.go_per_lot, p.tick_size, p.point_value = 12000.0, 0.01, 1.0
    p.deposit = 100000.0
    p.session_risk = trader_risk.SessionRisk(100000.0)
    p._sr_day = p._msk_day()
    p._state_path = None
    p._prepared = True
    p.reanalyze_cb = AsyncMock()
    m.pilot = p
    m.task = SimpleNamespace(done=lambda: False)
    return m, p


def book(price):
    return {"best_bid": round(price - 0.1, 4), "best_ask": round(price + 0.1, 4)}


async def tick(p, price, n=1):
    for _ in range(n):
        await p.tick(price, book(price))
        for _ in range(300):
            if not [t for t in p._background_tasks if not t.done()]:
                break
            await asyncio.sleep(0.005)


def order(do="BUY", entry=None, take=110.0, inv=98.0):
    return {"exec": {"do": do, "entry": entry, "take": take, "invalidation": inv, "why": "совет: тест"}}


async def _open_long(p, fake, inv=98.0, take=110.0):
    assert p.adopt_forecast(order("BUY", None, take, inv))
    fake.queue("mission_entry", {"decision": "ВОЙТИ", "why": "ок"})
    await tick(p, 100.0, 2)
    assert p.position, p.last_action
    return p.position


def test_code_guard_hold_until_price_is_a_trigger_not_an_exit(fake):
    """D5 — то, что обещает промпт троса, делает код F1 (AIPilot._apply_guard / tick):
    · hold_until_price строго между аварийным тросом и ценой — новый триггер (трос на месте), hold_minutes не действует:
      цена прошла новый триггер — вопрос сразу;
    · число не принято (выше цены / за аварийным тросом) — триггер прежний, вопрос снова через hold_minutes, без него —
      PYTHIA_SOFT_STOP_GRACE_SEC; до срока за триггером ни вопроса, ни выхода (держит аварийный трос);
    · каждое ЖДАТЬ — +1 к holds; на лимите PYTHIA_SOFT_STOP_MAX_HOLDS за триггером — слив по правилу сразу, без вопроса
      и без срока; за аварийным тросом — слив без вопроса всегда."""
    async def scenario():
        m, p = make_pilot()
        pos = await _open_long(p, fake)                            # триггер 98, аварийный трос 98 − 1.5 %
        hard = float(pos["hard_stop"])
        assert 96.0 < hard < 97.0, pos
        fake.queue("mission_guard", {"decision": "ЖДАТЬ", "why": "вынос стопов: объём 3×, возврат", "hold_until_price": 97.0,
                                     "hold_minutes": 10})
        await tick(p, 97.9)                                        # за триггером 98 — вопрос у троса
        assert fake.count("mission_guard") == 1 and p.position and float(pos["invalidation"]) == 97.0, p.last_action
        assert float(pos["hard_stop"]) == hard, "аварийный трос на месте"
        assert pos["guard_next"] == 0.0 and pos["holds"] == 1, "новый триггер: срока нет, вопрос у него сразу"
        g = p.guards[-1]
        assert g["hold_until"] == 97.0 and g["hold_minutes"] is None and "у него спрошу снова" in p.last_action, g
        placed = len(p.broker.placed)
        # цена прошла новый триггер — вопрос сразу (hold_minutes 10 из прошлого ответа не действует)
        fake.queue("mission_guard", {"decision": "ЖДАТЬ", "why": "поглощение", "hold_until_price": 99.0, "hold_minutes": 5})
        await tick(p, 96.9)
        assert fake.count("mission_guard") == 2 and p.position and len(p.broker.placed) == placed, p.last_action
        g = p.guards[-1]
        assert float(pos["invalidation"]) == 97.0, "hold_until_price выше цены — не принят, триггер прежний"
        assert g["hold_until"] is None and g["hold_until_ai"] == 99.0 and "не принят" in g["hold_note"], g
        assert 295 <= pos["guard_next"] - time.time() <= 301 and pos["holds"] == 2, "прежний триггер — срок hold_minutes"
        await tick(p, 96.8)                                        # за триггером, до срока: ни вопроса, ни выхода
        assert fake.count("mission_guard") == 2 and p.position and len(p.broker.placed) == placed, p.last_action
        pos["guard_next"] = 0.0                                    # срок вышел — тот же вопрос снова
        fake.queue("mission_guard", {"decision": "ЖДАТЬ", "why": "стена bid 96.7", "hold_until_price": 96.0})
        await tick(p, 96.8)
        g = p.guards[-1]
        assert fake.count("mission_guard") == 3 and p.position and float(pos["invalidation"]) == 97.0, p.last_action
        assert "за аварийным тросом" in g["hold_note"], "96.0 за тросом — не принят"
        assert 170 <= pos["guard_next"] - time.time() <= 181, "без hold_minutes — PYTHIA_SOFT_STOP_GRACE_SEC"
        assert pos["holds"] == 3
        await tick(p, 96.8)                                        # лимит 3 из 3 — слив по правилу сразу, срок не ждём
        assert fake.count("mission_guard") == 3 and len(p.broker.placed) > placed, p.last_action
        # за аварийным тросом — слив без вопроса всегда
        m2, p2 = make_pilot()
        pos2 = await _open_long(p2, fake)
        n0, placed2 = fake.count("mission_guard"), len(p2.broker.placed)
        await tick(p2, round(float(pos2["hard_stop"]) - 0.1, 2))
        assert fake.count("mission_guard") == n0 and len(p2.broker.placed) > placed2, p2.last_action

    asyncio.run(scenario())


def test_code_door_wait_with_level_and_term(fake):
    """Дверь: ЖДАТЬ с уровнем и сроком — у уровня до срока ни входа, ни вопроса; после срока — вход у уровня без
    второго вопроса (свежий ответ двери); ЖДАТЬ без того и другого — вопрос снова через PYTHIA_ENTRY_CHECK_COOL_SEC."""
    async def scenario():
        m, p = make_pilot()
        assert p.adopt_forecast(order("BUY", None, 110.0, 98.0))
        assert p.plan["src"] == "council"
        fake.queue("mission_entry", {"decision": "ЖДАТЬ", "why": "лента продаёт 2:1", "entry": 99.5, "entry_kind": "откат",
                                     "wait_minutes": 10})
        await tick(p, 100.0)
        assert fake.count("mission_entry") == 1 and p.plan["entry"] == 99.5 and p.plan["kind"] == "откат", p.plan
        assert 590 <= p.plan["gate_after"] - time.time() <= 601
        await tick(p, 99.4, 2)                                    # уровень дан раньше срока — входа нет
        assert not p.broker.placed and fake.count("mission_entry") == 1 and not p.position, p.last_action
        p.plan["gate_after"] = 0.0                                 # срок вышел, уровень держится — вход без двери
        await tick(p, 99.4, 2)
        assert fake.count("mission_entry") == 1 and p.broker.placed, p.last_action
        # без уровня и срока — повтор через PYTHIA_ENTRY_CHECK_COOL_SEC
        m2, p2 = make_pilot()
        assert p2.adopt_forecast(order("BUY", None, 110.0, 98.0))
        fake.queue("mission_entry", {"decision": "ЖДАТЬ", "why": "спред 3×"})
        await tick(p2, 100.0)
        assert 290 <= p2.plan["gate_after"] - time.time() <= 301, p2.plan

    asyncio.run(scenario())


def test_code_council_ambush_expires_to_duty_pro(fake):
    """D4: приказ совета у уровня идёт через дверь (свежести у него нет), невзятая засада снимается через PLAN_TTL_SEC
    от приказа — повод дежурному PRO («приказ протух»), не полный совет."""
    async def scenario():
        m, p = make_pilot()
        assert p.adopt_forecast(order("BUY", 99.0, 110.0, 97.0))
        assert p.plan["src"] == "council" and p.plan["kind"] == "откат"
        await tick(p, 98.9)                                        # уровень взят — дверь спрашивает (совет — всегда)
        assert fake.count("mission_entry") == 1 and not p.broker.placed, p.last_action
        m2, p2 = make_pilot()
        assert p2.adopt_forecast(order("BUY", 99.0, 110.0, 97.0))
        p2.plan["ts"] = time.time() - ai_pilot.PLAN_TTL_SEC - 1
        await tick(p2, 100.0)
        assert p2.plan is None and "приказ протух" in (p2._review_reason or ""), (p2.last_action, p2._review_reason)
        p2.reanalyze_cb.assert_not_called()

    asyncio.run(scenario())


def test_code_profit_lock_acceptance(fake):
    """Прибыль: lock_price поднимает триггер, только если он между входом и ценой и лучше нынешнего триггера."""
    async def scenario():
        m, p = make_pilot()
        pos = await _open_long(p, fake)
        inv0 = float(pos["invalidation"])
        applied: list = []
        p._profit_lock(pos, 99.0, 0.0, 103.0, applied)             # ниже входа — не принят
        assert float(pos["invalidation"]) == inv0 and "не принят" in applied[-1], applied
        p._profit_lock(pos, 103.5, 0.0, 103.0, applied)            # выше цены — не принят
        assert float(pos["invalidation"]) == inv0 and "не принят" in applied[-1], applied
        p._profit_lock(pos, 101.5, 0.0, 103.0, applied)            # между входом и ценой, лучше триггера — принят
        assert float(pos["invalidation"]) == 101.5 and pos.get("profit_lock"), applied
        p._profit_lock(pos, 101.0, 0.0, 103.0, applied)            # не лучше нынешнего триггера — не принят
        assert float(pos["invalidation"]) == 101.5 and "не лучше триггера" in applied[-1], applied
        # следующая мысль видит, что код сделал с числом (промпт: «не принят — триггер не тронут»), а не «триггер → X»
        p.prices.append(103.0)
        await p._apply_profit({"decision": "ДЕРЖАТЬ", "why": "ход жив", "lock_price": 99.0}, 103.0, pos, "повод: тест")
        th = p._profits_text(pos)
        assert "[код: lock_price 99 не принят: не между входом" in th and "триггер → 99" not in th, th
        assert float(pos["invalidation"]) == 101.5

    asyncio.run(scenario())


def test_code_triage_now_spends_the_event_window(fake):
    """Триаж СЕЙЧАС: перепроверка подтягивается не раньше PYTHIA_EVENT_COOL_SEC после прошлой событийной (прокол —
    PYTHIA_PUNCTURE_COOL_SEC) — ровно цена, названная в промпте триажа."""
    async def scenario():
        m, p = make_pilot()
        await _open_long(p, fake)
        now = time.time()
        p.position["opened_ts"] = now - 3600                       # не молодая позиция
        p._last_review_ts = now - 1000
        p._last_event_review_ts = now - 60                         # событийная перепроверка минуту назад
        p.review_ts = now + 1800
        p._ask_review_now("резкий ход +1.2 %", kind="shock", triage={"urgency": "СЕЙЧАС"})
        assert 895 - 60 <= p.review_ts - now <= 901 - 60, p.review_ts - now
        p.review_ts = now + 1800
        p._ask_review_now("прокол вверх", kind="puncture", triage={"urgency": "СЕЙЧАС"})
        assert 595 - 60 <= p.review_ts - now <= 601 - 60, p.review_ts - now

    asyncio.run(scenario())
