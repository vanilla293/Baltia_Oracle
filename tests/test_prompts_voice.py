# -*- coding: utf-8 -*-
"""v5.4.1 «ТРЕЗВЫЙ ПИЛОТ» (24.09.2026, W1): голос первой ПИФИИ 4.5.4 в промптах.

Что закреплено: PERSONA + DOCTRINE (prompts_mission.VOICE) стоят в system всех думающих и денежных узлов миссии
(анализ, критика, вердикт, приказ, перепроверка, трос, тейк, триаж, проверка входа, мысль о прибыли), совета
(аналитик daily/update/human, критик, председатель, итог) и чата; механика — без доктрины; подталкиваний
«флета нет» / «главное войти, а не оттягивать» нет; приказ допускает WAIT с wait_for; system ≤ 20 строк; слово
«json» в каждой JSON-стадии; время МСК всегда дано; entry_check/profit_think отдают (system, user) с нужными
блоками; ai_v5.money_json выбирает PRO/FLASH по PYTHIA_MONEY_MODEL; MONEY_ROUTES знает mission_entry/mission_profit.
v5.4.2 «СВОБОДНЫЙ ПИЛОТ» (воля владельца 28.09.2026: «ждём, ждём, ждём — не входит и не выходит»): пункт 7 доктрины —
свобода решения (все ходы равноправны, у каждого своя цена); BANNED ловит подталкивания в ОБЕ стороны — и «главное
войти», и «вне рынка — не трусость» / «только с перевесом» / «сомнение — ждать» / «не входить вслепую»; шифровальщик
переводит вердикт как есть; критик ищет и слабый вход, и упущенный ход; совет — входов столько, сколько даёт картина.
v5.4.3 «РЕШИТЕЛЬНЫЙ ПИЛОТ» (воля владельца 30.09.2026: «прорыва нет, сидим ждём» — надоело): лёгкий крен к действию
через цену ожидания, засаду у уровня, порядок вариантов и требование числа (не голым «будь агрессивнее»): доктрина —
спор сигналов не повод стоять, у ожидания тоже цена, ожидаемый итог лучших ходов; «ждёшь пробоя» — засада, а не
ЖДЁМ/WAIT; ожидание и удержание в списках последними; метки перепроверки без «_СЕЙЧАС» (канон кода прежний).
Без сети: ИИ не зовётся (money_json — на подменённых pro_json/flash_json)."""
import asyncio

import pytest

from backend import ai_v5, api_chat, config, explain, scout
from backend import prompts_council as pc
from backend import prompts_mission as pm

TIME = "18.09.2026 14:35 МСК, четверг"
CTX = {"time_msk": TIME, "price": 285.4, "asset_class": "share", "dossier": "ИНСТРУМЕНТ: Сбер", "council": "risk-on",
       "news": "[a1b2c3] 18.09 14:20 · rbc · тест", "wyckoff": "ВАЙКОФФ D1: НАКОПЛЕНИЕ", "scan": "СКАНЕР: тянут вверх 78%",
       "reason": "тест", "position": "ОТКРЫТАЯ ПОЗИЦИЯ: long 10 лот @279"}
KW = dict(situation="Цена 285.4, позиция long 10 @279", light="стакан: плита на покупку 285.0", news="[a1b2c3] новость",
          council_text="итог совета", scan="СКАНЕР", scout="MX 2 791", partners="- VTBR: ρ=+0.81", memory="ПАМЯТЬ", time_msk=TIME)


def _mission_nodes() -> dict[str, tuple[str, str]]:
    return {
        "analysis": pm.analysis("SBER", "Сбербанк", "long", CTX),
        "critique": pm.critique("SBER", "Сбербанк", "long", "анализ…", CTX),
        "verdict": pm.verdict("SBER", "Сбербанк", "auto", "анализ…", "критика…", CTX),
        "exec_order": pm.exec_order("SBER", "Сбербанк", "auto", 285.4, "вердикт…", CTX),
        "review": pm.review("SBER", "Сбербанк", "auto", situation="Цена 285.4", light="стакан", council_text="итог",
                            prev_exec="WAIT — ждём 283", news="", watch="", astro_line="☽", in_pos=False, time_msk=TIME),
        "stop_guard": pm.stop_guard("SBER", "Сбербанк", "long", trigger="long, цена 281.1 ниже 281.2", history="285 → 281",
                                    plan="BUY 285 стоп 281.2", hard_stop="Аварийный трос @277.", holds_left="Ждать можно ещё 2 раза.", **KW),
        "take_guard": pm.take_guard("SBER", "Сбербанк", "long", take="long, цена 291.2 выше цели 291", history="291 → 291.2",
                                    plan="BUY тейк 291", lock_rule="Прибыль ниже 287.8 не отпускаем. ", holds_left="ещё 2 раза", **KW),
        "event_triage": pm.event_triage("SBER", "Сбербанк", "auto", event="резкий ход +2.0%", situation="Цена 291, позиция long",
                                        history="285 → 291", light="стакан", plan="BUY тейк 295", news="", memory="ПАМЯТЬ",
                                        time_msk=TIME, review_in="через 22 мин", last_review="ЖДЁМ"),
        "entry_check": pm.entry_check("SBER", "Сбербанк", "long", plan="BUY сейчас, стоп 281, тейк 291", history="285.0 → 285.4",
                                      guards="10:40 трос: ЖДАТЬ", checks="14:20 ЖДАТЬ @285", **KW),
        "profit_think": pm.profit_think("SBER", "Сбербанк", "long", profit="пройдено 64 % хода до тейка 291", history="279 → 285.4",
                                        plan="BUY тейк 291 стоп 276", guards="", thoughts="14:00 ДЕРЖАТЬ — ход жив", **KW),
    }


def _council_nodes() -> dict[str, tuple[str, str]]:
    return {
        "analysis_daily": pc.analysis_daily(3, "лента 120", "SBER 285.4", "☽ Луна", "потоки…", extra="IMOEX 2 791"),
        "analysis_update": pc.analysis_update("итог", "[n1] новость", "SBER 285.4", "☽"),
        "analysis_human": pc.analysis_human("итог", {"theses": []}, "SBER 285.4", "☽", "свежие"),
        "critique": pc.critique("анализ", "потоки кратко"),
        "verdict": pc.verdict("анализ", "критика", "потоки", "☽", human=True),
        "summary": pc.summary("вердикт", human=True, codes_text="SBER, BR"),
    }


def _council_mechanics() -> dict[str, tuple[str, str]]:
    return {
        "triage": pc.triage("[a1b2c3] x", 1), "characterize": pc.characterize({"title": "t", "summary": "s"}),
        "group": pc.group("[a1b2c3] x", 1), "group_merge": pc.group_merge("P1S1 …", 1),
        "distribute": pc.distribute("p", "c", "n"), "human_compress": pc.human_compress("текст"),
        "present_frame": pc.present_frame("mission", "данные"), "impact": pc.impact("итог", "[n1] новость"),
    }


MISSION = _mission_nodes()
COUNCIL = _council_nodes()
MECHANICS = _council_mechanics()
CHAT = api_chat.prompt("что по SBER, держим?", {"mission": "Миссия SBER: в позиции long 8 @299", "light": "SBER: цена 300.5"}, "pro")
JSON_MISSION = ("exec_order", "review", "stop_guard", "take_guard", "event_triage", "entry_check", "profit_think")
JSON_COUNCIL = ("summary", "triage", "characterize", "group", "group_merge", "distribute", "human_compress", "present_frame", "impact")


# ── доктрина и персона: константы ────────────────────────────────────────────
def test_doctrine_is_compact_and_says_the_right_things():
    assert 6 <= len(pm.DOCTRINE.splitlines()) <= 9 and 1 <= len(pm.PERSONA.splitlines()) <= 2
    assert pm.VOICE == f"{pm.PERSONA}\n{pm.DOCTRINE}" and len(pm.VOICE.splitlines()) <= 11
    for piece in ("кухню рынка", "НАБИРАЕТ", "РАЗДАЁТ", "ВЫНОСИТ стопы", "Конкретика", "таймингом", "Сценарии", "отменой",
                  "Холодный расчёт", "решителен", "честен", "асимметрия", "рынок ошибается", "стоять вне рынка",
                  "ни один ход не выбор по умолчанию", "лишний вход", "пропущенный ход"):
        assert piece in pm.DOCTRINE, piece
    # v5.4.2: п.7 не оправдывает одно ожидание и не делает вход «дороже» пропуска
    for absent in ("трусост", "хуже пропущенного", "Нет перевеса — вне рынка"):
        assert absent not in pm.DOCTRINE, absent
    assert "стратег" in pm.PERSONA and "двигает рынок" in pm.PERSONA
    # пункты 4/5/5a/5b доктрины 4.5.4 (как читать небо, Вайкофф, первоисточник, эфир) не перенесены: слои идут данными
    for absent in ("ГАНН", "Ганну", "IAU", "PLV", "тропическ", "ОБЯЗАН опереться"):
        assert absent not in pm.DOCTRINE, absent
    assert not any(b in pm.VOICE.lower() for b in pm.BANNED)


# ── миссия: все думающие и денежные узлы ─────────────────────────────────────
@pytest.mark.parametrize("name", sorted(MISSION))
def test_mission_node_voice_lines_time_json(name):
    s, u = MISSION[name]
    assert pm.VOICE in s and pm.DOCTRINE in s and pm.PERSONA in s and pm.FREEDOM in s, name
    assert pm.system_lines(s) <= pm.SYSTEM_MAX_LINES == 20, (name, pm.system_lines(s))
    assert "МСК" in s and "МСК" in u, (name, "время МСК всегда дано")
    assert not any(b in s.lower() for b in pm.BANNED), (name, "подталкивание к входу или к ожиданию")
    assert "SBER" in u and "Сбербанк" in s
    if name in JSON_MISSION:
        assert "json" in s.lower() and "строго один JSON-объект" in s, (name, "правило JSON-режима DeepSeek")
    else:
        assert "JSON" not in s, (name, "думающая стадия — свободный текст")
    role = s.splitlines()[0]
    assert "FLASH" not in role, (name, "роль не привязана к FLASH: модель у денег — PYTHIA_MONEY_MODEL")


def test_banned_catches_nudges_both_ways():
    """v5.4.2: BANNED — подталкивания и к входу (5.1–5.4.0), и к ожиданию (5.4.1); сравнение по system.lower()."""
    assert all(b == b.lower() for b in pm.BANNED)
    for nudge in ("флета нет", "главное войти", "а не оттягивать",              # к входу
                  "не трусость", "хуже пропущенного", "только с перевесом",    # к ожиданию
                  "сомнение — ждать", "вслепую", "не лезем", "не от скуки"):
        assert nudge in pm.BANNED, nudge


def test_exec_order_allows_wait_as_decision():
    s, _ = MISSION["exec_order"]
    assert '"do":"BUY|SELL|WAIT|HOLD|CLOSE"' in pm.EXEC_SCHEMA and '"wait_for"' in pm.EXEC_SCHEMA
    assert '"invalidation":число|null' in pm.EXEC_SCHEMA, "при WAIT стопа нет"
    assert "wait_for" in pm.EXEC_RULE and "дежурный PRO вернётся" in pm.EXEC_RULE
    # v5.4.2: шифровальщик переводит вердикт как есть в обе стороны и сам решение не пересматривает
    assert "Переводи решение вердикта как есть" in pm.EXEC_RULE and "вердикт BUY/SELL" in pm.EXEC_RULE
    assert "трусост" not in pm.EXEC_RULE and "наугад" not in pm.EXEC_RULE
    assert "«держать» — HOLD" in pm.EXEC_RULE and "без добора" in pm.EXEC_RULE, "держать ≠ добор до максимума"
    assert "при WAIT — null" in s and "после проверки у двери" in s and pm.EXEC_SCHEMA in s
    assert "флета нет" not in s.lower() and "немедленно и на максимум" not in s


def test_verdict_and_review_treat_all_moves_as_equal():
    sv, _ = MISSION["verdict"]
    assert "вне рынка" in sv and "по тому, что сильнее в данных" in sv and "на откате" in sv and "на пробитии уровня" in sv
    assert "держать" in sv and "перевернуть" in sv and "добрать" in sv
    assert "только с перевесом" not in sv and "весомых новых основаниях" not in sv, "у статус-кво нет форы"
    sr, ur = MISSION["review"]
    assert "Приказ совета WAIT" in sr and "wait_for" in sr and "ориентир, а не условие" in sr and "у двери" in sr
    assert "ни один choice не выбор по умолчанию" in sr and "не якорь" in sr and "порядок ничего не значит" in sr
    assert "WAIT — ждём 283" in ur, "перепроверка видит приказ WAIT"
    assert pm.review_options("auto", False).split(" | ")[0] != "ЖДЁМ" and pm.review_options("auto", True).split(" | ")[0] != "ДЕРЖАТЬ"
    sc, _ = MISSION["critique"]
    assert "в обе стороны" in sc and "Перестраховка" in sc and "упустил ли вход или выход" in sc


def test_decisive_tilt_543():
    """v5.4.3 «РЕШИТЕЛЬНЫЙ ПИЛОТ»: лёгкий крен к действию задан ценой ожидания, засадой у уровня, порядком вариантов и
    требованием числа — не голым «будь агрессивнее»; invalidation для входа по-прежнему обязателен; канон кода прежний."""
    # доктрина и персона: спор сигналов — не повод стоять, у ожидания своя цена, ожидаемый итог, засада, боковик — рынок
    for piece in ("не повод стоять", "у ожидания тоже", "ожидаемый итог", "засаду у уровня", "боковик (диапазон — тоже рынок",
                  "небольшой перекос — тоже перекос", "а не откладывай словами"):
        assert piece in pm.DOCTRINE, piece
    assert "пока толпа ждёт подтверждения" in pm.PERSONA and len(pm.PERSONA.splitlines()) == 1
    assert len(pm.DOCTRINE.splitlines()) == 8, "заголовок + 7 пунктов, один пункт — одна строка"
    systems = {**{k: v[0] for k, v in MISSION.items()}, **{k: v[0] for k, v in COUNCIL.items()}, "chat": CHAT[0]}
    for name, s in systems.items():
        low = s.lower()
        assert "агрессивн" not in low and "будь решительным" not in low, (name, "голый приказ решительности")
    # перепроверка: ждать пробоя — засада, ЖДЁМ с числом, описание рынка — не решение, entry при ЖДЁМ — будильник
    sr, _ = MISSION["review"]
    for piece in ("это засада", "а не ЖДЁМ", "описание рынка, а не решение", "будильник", "почему не засада",
                  "уровень числом или время МСК", "в диапазоне играют от границ", "invalidation обязателен",
                  "а не потому, что картина неясна", "на ближайшие 30 мин", "трос и тейк встанут туда"):
        assert piece in sr, piece
    assert "КУПИТЬ_СЕЙЧАС" not in sr and "ПРОДАТЬ_СЕЙЧАС" not in sr
    # списки choice: ожидание / удержание последними во всех режимах, «_СЕЙЧАС» в метках нет, всё разбирается в канон
    for play in ("long", "short", "mixed", "auto"):
        for in_pos, side in ((False, None), (True, "long"), (True, "short")):
            opts = pm.review_options(play, in_pos, side).split(" | ")
            assert opts[-1] == ("ДЕРЖАТЬ" if in_pos else "ЖДЁМ"), (play, in_pos, opts)
            assert not any("_СЕЙЧАС" in o for o in opts), opts
            table = ai_v5.review_table(in_pos, side)
            for o in opts:
                assert ai_v5.decision_of(o, table) in table, (play, in_pos, side, o)
    assert ai_v5.decision_of("КУПИТЬ", ai_v5.review_table(False)) == "КУПИТЬ_СЕЙЧАС"
    assert ai_v5.decision_of("ПРОДАТЬ", ai_v5.review_table(False)) == "ПРОДАТЬ_СЕЙЧАС"
    assert ai_v5.decision_of("ДЕРЖАТЬ", ai_v5.review_table(True, "long")) == "ЖДЁМ"
    assert ai_v5.decision_of("ЖДЁМ", ai_v5.review_table(False)) == "ЖДЁМ"
    assert ai_v5.decision_of("КУПИТЬ", ai_v5.review_table(True, "long")) == "ДОБРАТЬ", "в позиции КУПИТЬ у лонга — добор"
    # схемы двери и прибыли: ожидание / удержание не первыми
    door = pm.ENTRY_SCHEMA.split('"decision":"')[1].split('"')[0].split("|")
    prof = pm.PROFIT_SCHEMA.split('"decision":"')[1].split('"')[0].split("|")
    assert door[0] == "ВОЙТИ" and door[-1] == "ЖДАТЬ" and prof[0] != "ДЕРЖАТЬ" and prof[0] == "ВЫЙТИ", (door, prof)
    se, ue = MISSION["entry_check"]
    assert "что изменилось после приказа" in se and "ход до этой точки пройдёт без нас" in se
    assert "Войти сейчас, отменить или ждать уровня/срока?" in ue
    sp, up = MISSION["profit_think"]
    assert "назови lock_price" in sp and "а не просто спорная картина" in sp and "ни один ответ не выбор по умолчанию" in sp
    assert "Выйти, выйти и перезайти, держать или звать совет?" in up
    # приказ: «войти, если пробьёт X» — BUY/SELL с засадой, а не WAIT; WAIT — с числом; invalidation для входа обязателен
    assert "а не WAIT" in pm.EXEC_RULE and "засаду пилот исполнит сам" in pm.EXEC_RULE
    assert "уровень числом и/или время МСК" in pm.EXEC_RULE and "«подтверждение» — только с числом" in pm.EXEC_RULE
    assert "при проходе которых разбудить дежурного PRO" in pm.EXEC_RULE and '"levels":[число]' in pm.EXEC_SCHEMA
    assert "что изменит решение" in pm.EXEC_SCHEMA and "чего ждём" not in pm.EXEC_SCHEMA and "что отменяет идею" in pm.EXEC_SCHEMA
    assert "invalidation обязателен для BUY/SELL" in MISSION["exec_order"][0]
    # вердикт, анализ и критика миссии: ждать пробоя — вход на пробитии; «вне рынка» — с числом; упущенный ход в %
    sv = MISSION["verdict"][0]
    assert "пилот исполнит его сам, а не «вне рынка»" in sv and "какое число изменит решение" in sv
    assert "что отменяет план" in sv and "чего ждёшь" not in sv and "подтверждение, время" not in sv
    assert "ждёшь пробоя или отката — дай уровень входа, стоп и цель" in MISSION["analysis"][0]
    sc = MISSION["critique"][0]
    for piece in ("с равным усердием", "засадой у уровня", "боковик без игры от границ", "сколько % стоил бы упущенный ход",
                  "как рынок уйдёт без нас и где был бы вход"):
        assert piece in sc, piece
    # трос, тейк, триаж: ожидание не бесплатно
    sg = MISSION["stop_guard"][0]
    assert "докажи числами" in sg and "иначе это надежда, а не картина" in sg and "назови hold_until_price" in sg
    assert "можешь назвать новый уровень" not in sg
    assert "lock_price обязателен" in MISSION["take_guard"][0]
    st = MISSION["event_triage"][0]
    assert "уже в цене" not in st and "Не уверен, нужен ли PRO, — СЕЙЧАС" in st
    # «нет данных» по слою — не повод не решать (миссия у денег и совет)
    for name in ("analysis", "verdict", "review", "stop_guard", "take_guard", "entry_check", "profit_think", "event_triage"):
        s = MISSION[name][0]
        assert "нет данных по слою — скажи и решай по тому, что есть" in s and "нет данных — так и скажи" not in s, name
    for name in ("analysis_daily", "analysis_update", "analysis_human", "critique", "verdict"):
        s = COUNCIL[name][0]
        assert "нет данных по слою — скажи и решай по тому, что есть" in s and "так и скажи" not in s, name
    # совет: идея со стороной и уровнем — вход с триггером, а не «смотреть»; итог кладёт её в picks
    svc, ssc, scc = COUNCIL["verdict"][0], COUNCIL["summary"][0], COUNCIL["critique"][0]
    assert "только идеи без стороны" in svc and "это вход с триггером, а не «смотреть»" in svc and "уровень входа числом" in svc
    assert "даже если вердикт назвал её «смотреть»" in ssc and "в обе стороны с равным усердием" in scc and "«ждать пробоя»" in scc
    # прокол: одна оговорка-критерий у всех ролей, без голого «или ложный прокол»
    assert all(pm.PUNCTURE_FALSE in v for v in pm.PUNCTURE_ROLES.values())
    assert not any("или ложный прокол" in v for v in pm.PUNCTURE_ROLES.values())
    assert "засада за полосой опоздает" in pm.PUNCTURE_ROLES["вне рынка"]


def test_guards_ask_by_picture_not_by_rule():
    sg, _ = MISSION["stop_guard"]
    assert "дежурный миссии у троса" in sg and "слить" in sg.lower() and "Совету" in sg and pm.GUARD_SCHEMA in sg
    assert "решаешь ты" in sg and "Оба ответа равноправны" in sg
    assert "вынос стопов" in sg and "Аварийный трос" in sg and "ещё 2 раза" in sg
    st, _ = MISSION["take_guard"]
    assert "дежурный миссии у тейка" in st and "зафиксировать" in st.lower() and pm.TAKE_SCHEMA in st and "287.8" in st
    assert "решаешь ты" in st
    se, ue = MISSION["event_triage"]
    assert pm.TRIAGE_SCHEMA in se and "СЕЙЧАС" in se and "ПЛАНОВО" in se and "САМ" in se and "через 22 мин" in se
    assert "за секунды" not in se, "триаж на PRO: обещаний «за секунды» нет"
    assert "пора забирать прибыль" in se, "рывок в нашу сторону тоже может звать PRO — за выходом"
    assert "СКАНЕР" not in ue and "РАЗВЕДК" not in ue, "триаж короткий: без сканера и разведки"


def test_entry_check_blocks_and_schema():
    s, u = MISSION["entry_check"]
    assert pm.ENTRY_SCHEMA in s and '"decision":"ВОЙТИ|ОТМЕНИТЬ|ЖДАТЬ"' in pm.ENTRY_SCHEMA
    for field in ('"entry"', '"entry_kind":"сейчас|откат|прорыв"|null', '"wait_minutes"', '"invalidation"', '"take"', '"council":true|false', '"note"'):
        assert field in pm.ENTRY_SCHEMA, field
    assert "у самой двери" in s and "сверь его с живым рынком" in s and "ни один исход не выбор по умолчанию" in s and "ОТМЕНИТЬ" in s
    assert "анализ заново не пересобирай" in s and "не входить вслепую" not in s and "сомнение — ЖДАТЬ" not in s
    assert "Совет уже решил войти" not in s and "приказ совета или дежурного PRO" in s, "решение могло прийти и от перепроверки"
    for piece in (f"ВРЕМЯ: {TIME}", "═══ СИТУАЦИЯ ═══", "Цена 285.4, позиция long 10 @279", "═══ ПРИКАЗ И ПЛАН", "BUY сейчас, стоп 281",
                  "═══ ХОД ЦЕНЫ", "285.0 → 285.4", "═══ ЖИВОЙ РЫНОК ═══", "плита на покупку", "═══ ПРОШЛЫЕ ОТВЕТЫ У ДВЕРИ", "14:20 ЖДАТЬ @285",
                  "не обязательство",
                  "═══ ПРОШЛЫЕ ОТВЕТЫ У ТРОСА И ТЕЙКА ═══", "10:40 трос: ЖДАТЬ", "═══ ПАМЯТЬ МИССИИ", "═══ СКАНЕР СТАКАНА ═══",
                  "═══ ДАННЫЕ РАЗВЕДКИ ═══", "MX 2 791", "═══ СВЯЗАННЫЕ БУМАГИ", "ρ=+0.81", "═══ ИТОГ ОБЩЕГО СОВЕТА ═══",
                  "═══ СВЕЖИЕ НОВОСТИ (время МСК) ═══", "[a1b2c3]", "Войти сейчас, отменить или ждать уровня/срока?"):
        assert piece in u, piece
    assert u.index("═══ СИТУАЦИЯ ═══") < u.index("═══ ПРИКАЗ И ПЛАН") < u.index("═══ ХОД ЦЕНЫ") < u.index("═══ СВЕЖИЕ НОВОСТИ")
    _, u0 = pm.entry_check("SBER", "Сбербанк", "long", situation="", plan="", history="", light="", news="", council_text="")
    assert "(нет)" in u0 and "═══ ПРИКАЗ И ПЛАН" not in u0 and "ВРЕМЯ: ?" in u0, "пустые блоки пропущены, ничего не выдумано"


def test_profit_think_blocks_and_schema():
    s, u = MISSION["profit_think"]
    assert pm.PROFIT_SCHEMA in s and '"decision":"ВЫЙТИ|ВЫЙТИ_И_ПЕРЕЗАЙТИ|ДЕРЖАТЬ|СОВЕТ"' in pm.PROFIT_SCHEMA
    for field in ('"lock_price"', '"take"', '"reentry"', '"reentry_kind":"откат|прорыв"|null', '"note"'):
        assert field in pm.PROFIT_SCHEMA, field
    assert "позиция в плюсе" in s and "пройдено 64 %" in s and "может продолжиться, а может выдохнуться" in s and "ВЫЙТИ_И_ПЕРЕЗАЙТИ" in s
    assert "рывок может быть последним" not in s, "v5.4.2: без подталкивания и к выходу"
    for piece in (f"ВРЕМЯ: {TIME}", "ПРИБЫЛЬ: пройдено 64 % хода до тейка 291", "═══ СИТУАЦИЯ ═══", "═══ ХОД ЦЕНЫ", "279 → 285.4",
                  "═══ ЖИВОЙ РЫНОК ═══", "═══ ПЛАН И ПРОШЛЫЕ РЕШЕНИЯ ═══", "BUY тейк 291 стоп 276", "═══ ПРОШЛЫЕ МЫСЛИ О ПРИБЫЛИ ═══",
                  "14:00 ДЕРЖАТЬ", "═══ ПАМЯТЬ МИССИИ", "═══ СКАНЕР СТАКАНА ═══", "═══ ДАННЫЕ РАЗВЕДКИ ═══", "═══ СВЯЗАННЫЕ БУМАГИ",
                  "═══ ИТОГ ОБЩЕГО СОВЕТА ═══", "═══ СВЕЖИЕ НОВОСТИ (время МСК) ═══", "Выйти, выйти и перезайти, держать или звать совет?"):
        assert piece in u, piece
    assert "ПРОШЛЫЕ ОТВЕТЫ У ТРОСА И ТЕЙКА" not in u, "пустой блок guards пропущен"


# ── совет и чат ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", sorted(COUNCIL))
def test_council_chair_has_voice(name):
    s, u = COUNCIL[name]
    assert pm.VOICE in s and pm.DOCTRINE in s, name
    assert pm.system_lines(s) <= pm.SYSTEM_MAX_LINES and "сейчас:" in u and "МСК" in u, name
    assert not any(b in s.lower() for b in pm.BANNED), name
    if name in JSON_COUNCIL:
        assert "json" in s.lower() and "строго один JSON-объект" in s, name


def test_council_is_not_a_funnel():
    sv = COUNCIL["verdict"][0]
    assert "ни одного, один или несколько" in sv and "трусост" not in sv and "только с перевесом" not in sv
    assert "ближайшую торговую сессию" in sv and "входить сегодня" not in sv, "совет ночью и в выходной не пустеет"
    ss = COUNCIL["summary"][0]
    assert "picks пустой" in ss and "не выбрасывай" in ss and "без перевеса" not in ss, "итог переводит вердикт как есть"
    assert "список пуст — скажи почему" in COUNCIL["analysis_daily"][0]
    sc = COUNCIL["critique"][0]
    assert "в обе стороны" in sc and "Упущенное" in sc and "по упущенному — добавить" in sc
    assert "ПОЛНЫМ текущим списком входов" in COUNCIL["analysis_update"][0]
    si = MECHANICS["impact"][0]
    assert "новая возможность" in si and "шанс" in si, "дозор будит миссию и на шанс, не только на угрозу"


@pytest.mark.parametrize("name", sorted(MECHANICS))
def test_mechanics_have_no_doctrine_but_json_word(name):
    s, u = MECHANICS[name]
    assert pm.DOCTRINE not in s and pm.PERSONA not in s and "ДОКТРИНА" not in s, (name, "механика — без доктрины")
    assert pm.system_lines(s) <= pm.SYSTEM_MAX_LINES and "МСК" in u, name
    assert "json" in s.lower() and "строго один JSON-объект" in s, name


def test_explain_and_scout_have_no_doctrine():
    s, _ = explain.prompt({"ticker": "SBER", "name": "Сбер", "time_msk": TIME, "price": 285.4}, [], [])
    assert "ДОКТРИНА" not in s and pm.PERSONA not in s and "МСК" in s
    s2, _ = scout._plan_prompt("mission", "SBER 285.4", "SBER")
    assert "ДОКТРИНА" not in s2 and pm.PERSONA not in s2


def test_chat_has_voice_and_fits():
    s, u = CHAT
    assert pm.VOICE in s and "ДОКТРИНА" in s and pm.system_lines(s) <= pm.SYSTEM_MAX_LINES and "МСК" in s
    assert "ВОПРОС ВЛАДЕЛЬЦА: что по SBER, держим?" in u and "PRO (думающая модель)" in s
    assert "FLASH (быстрая модель)" in api_chat.prompt("q", {}, "flash")[0]
    assert not any(b in s.lower() for b in pm.BANNED)


# ── модель узлов у денег ─────────────────────────────────────────────────────
def test_money_routes_know_entry_and_profit():
    assert {"mission_entry", "mission_profit", "mission_guard", "mission_take", "event_triage"} <= ai_v5.MONEY_ROUTES
    assert ai_v5._sem_for("mission_entry") is ai_v5.SEM_MONEY and ai_v5._sem_for("mission_profit") is ai_v5.SEM_MONEY


def test_money_json_picks_model_by_setting(monkeypatch):
    calls = []

    async def fake_pro(system, user, *, route="pro", max_tokens=None):
        calls.append(("pro", route, max_tokens))
        return {"decision": "ВОЙТИ", "model": "pro"}

    async def fake_flash(system, user, *, think=True, route="flash", max_tokens=None):
        calls.append(("flash", route, think))
        return {"decision": "ДЕРЖАТЬ", "model": "flash"}

    monkeypatch.setattr(ai_v5, "pro_json", fake_pro)
    monkeypatch.setattr(ai_v5, "flash_json", fake_flash)
    monkeypatch.setattr(config, "PYTHIA_MONEY_MODEL", "pro")
    assert ai_v5.money_model() == "pro"
    assert asyncio.run(ai_v5.money_json("s", "u", route="mission_entry"))["model"] == "pro"
    monkeypatch.setattr(config, "PYTHIA_MONEY_MODEL", "flash")
    assert ai_v5.money_model() == "flash"
    assert asyncio.run(ai_v5.money_json("s", "u", route="mission_profit", max_tokens=None))["model"] == "flash"
    monkeypatch.setattr(config, "PYTHIA_MONEY_MODEL", "нечто")     # неизвестное значение → PRO (безопасное умолчание)
    assert ai_v5.money_model() == "pro"
    assert asyncio.run(ai_v5.money_json("s", "u", route="mission_guard"))["model"] == "pro"
    assert calls == [("pro", "mission_entry", None), ("flash", "mission_profit", True), ("pro", "mission_guard", None)]
    assert calls[1][2] is True, "FLASH у денег — всегда с размышлением"
