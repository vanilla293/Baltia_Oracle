# -*- coding: utf-8 -*-
"""v5.4.3 · стенд промптов (backend/prompt_bench.py): «как DeepSeek торгует сам».

Закреплено: 14 выдуманных ситуаций строят НАСТОЯЩИЕ промпты prompts_mission (перепроверка вне рынка и в позиции,
дверь, мысль о прибыли, вердикт) по закону 3 (голос 4.5.4, system ≤ 20 строк, без BANNED в обе стороны, «json» у
JSON-узлов, время МСК) и воспроизводимо; тексты, которые в бою собирает mission.py (ситуация, приказ, план и ответы у
двери, ход цены, новости, итог совета, сканер, prev совета), — из боевых сборщиков на сцене (настоящие Mission +
MissionPilot, замороженное время), и промпт стенда совпадает байт в байт с тем, что собирает настоящий узел
(MissionPilot._review / _entry_check / _profit_think) на тех же данных; история сцены — настоящими функциями 5.4.3
(после F1): ответ перепроверки — _review_answered / _review_record (запись с in_pos/pos_side, метка модели, будильник,
серия ЖДЁМ m.wait_streak), и запись сцены — та же, что пишет _review; находки стенда исправлены в бою (Вайкофф совета
с подписью и расстояниями от текущей цены, новость — один раз, возраст совета — от приказа); вызовы ИИ — те же
маршруты, что у mission.py (mission_review / mission_entry / mission_profit /
mission_verdict; у денег — срок попытки); слово решения — ai_v5.decision_of по словарям узлов, неразобранное и сбой —
silent («ответа не было»), стенд идёт дальше; отчёты (текст, markdown, JSON) содержат все ситуации; --dump пишет 28
файлов без ИИ; без ключа и без мока — код 2. Без сети: ai_v5.pro_json / money_json / pro_text подменены; в data/ ничего
не пишется (tmp_path)."""
import asyncio
import json

import pytest

from backend import ai_pilot, ai_v5, config, council, mission, prompt_bench as pb
from backend import prompts_mission as pm


@pytest.fixture
def fake(monkeypatch):
    f = pb.FakeAI(dict(pb.FAKE_ANSWERS))
    monkeypatch.setattr(ai_v5, "pro_json", f.pro_json)
    monkeypatch.setattr(ai_v5, "money_json", f.money_json)
    monkeypatch.setattr(ai_v5, "pro_text", f.pro_text)
    return f


def _data_snapshot() -> dict:
    d = config.DATA_DIR
    return {p.name: p.stat().st_mtime_ns for p in d.iterdir()} if d.exists() else {}


def test_fourteen_situations_cover_all_nodes():
    assert len(pb.SITUATIONS) == 14 and len(pb.BY_ID) == 14
    assert {s["node"] for s in pb.SITUATIONS} == set(pb.NODES)
    assert {s["expect"] for s in pb.SITUATIONS} == set(pb.EXPECTS)
    for sid in ("range_mid_wait", "range_low_buyer", "breakout_hold", "false_breakout", "trend_no_pullback",
                "news_shock", "dead_market", "long_resistance", "long_pressure", "short_add", "door_now",
                "door_breakout", "profit_fade", "verdict_split"):
        assert sid in pb.BY_ID, sid
    assert pb.BY_ID["long_resistance"]["side"] == "long" and pb.BY_ID["short_add"]["side"] == "short"


@pytest.mark.parametrize("sid", [s["id"] for s in pb.SITUATIONS])
def test_prompts_obey_law_3(sid):
    sit = pb.BY_ID[sid]
    system, user = pb.build(sit)
    low = system.lower()
    assert pm.VOICE in system and pm.FREEDOM in system
    assert pm.system_lines(system) <= pm.SYSTEM_MAX_LINES
    assert not [b for b in pm.BANNED if b in low]
    if sit["node"] in pb.JSON_NODES:
        assert "json" in low and "строго один JSON-объект" in system
    assert pb.msk(sit["time"]) in system and "SBER" in user and "обрезано" not in user


def test_prompts_are_reproducible_and_selfcheck_passes():
    assert [pb.build(s) for s in pb.SITUATIONS] == [pb.build(s) for s in pb.SITUATIONS]
    pb.check_prompts()


@pytest.mark.parametrize("sid,piece", pb.FORMAT_PIECES)
def test_blocks_in_combat_format(sid, piece):
    """Строки mission.py 5.4.3 (после F1 и находок стенда) в промптах стенда: цена ожидания (приказ WAIT — цена тогда →
    сейчас, ориентир — не условие), прошлая перепроверка с ценой решения и меткой модели (ДЕРЖАТЬ в позиции), «ЖДЁМ
    подряд» по счётчику серии (с какого времени, цена за серию), будильник, возраст совета от приказа, Вайкофф совета с
    подписью и расстояниями от текущей цены, новость по тикеру — один раз, взведённый вход, пройденный пробой, дверь без
    петли своих отговорок (ЖДАТЬ — факт с исходом по цене), повод перепроверки из наблюдателей тика, prev совета со
    свёрнутыми перепроверками."""
    system, user = pb.build(pb.BY_ID[sid])
    assert piece in user or piece in system, (sid, piece)


def test_blocks_other_sources_in_their_format():
    s1 = pb.BY_ID["range_mid_wait"]["args"]
    assert s1["prev_exec"].startswith("Приказ совета 30.09 09:40 (100 мин назад, цена тогда 299.8): WAIT — вне рынка. "
                                      "Ориентир совета (не условие): закрепление над 304")
    assert s1["light"].startswith("SBER: цена 300.0 (30.09.2026 11:20 МСК, среда)") and "Рентген: OBI" in s1["light"]
    assert "Майя: тяга нет (симметрия)" in s1["light"]
    assert s1["scan"].startswith("СКАНЕР СТАКАНА (онлайн, тик 3 с): 2360 тиков за 118 мин") and "Как читают" in s1["scan"]
    assert "ВАЙКОФФ D1 (250 баров)" in s1["wyckoff"] and "ВАЙКОФФ H1 (300 баров)" in s1["wyckoff"]
    assert "цена 299.8 на 54% высоты бокса" in s1["wyckoff"], "перепроверка видит Вайкоффа досье совета (m.layers)"
    assert s1["council_text"].startswith("Совет daily от 30.09 08:50 МСК (2 ч назад) — общий по рынку")
    assert s1["partners"].startswith("Связанные бумаги для SBER") and "Обычно читают так" in s1["partners"]
    # прокол сканера — первым в ситуации, повод — как у _puncture_watch
    s3 = pb.BY_ID["breakout_hold"]["args"]["situation"]
    assert s3.startswith("ПРОКОЛ СКАНЕРА: сторона ВВЕРХ") and "мы вне рынка без плана" in s3
    # позиция: плавающий P/L и аварийный трос — как считает пилот (PYTHIA_HARD_STOP_PCT сцены)
    assert "ПОЗИЦИЯ: short 30 лот @303, в рынке 70 мин, плавающий P/L +1050 ₽" in pb.BY_ID["short_add"]["args"]["situation"]
    assert "аварийный трос в программе @291.95, тейк 305.0" in pb.BY_ID["long_resistance"]["args"]["situation"]
    # у двери нет строки ПРОВЕРКА ВХОДА (её место — блок ПРОШЛЫЕ ОТВЕТЫ У ДВЕРИ), причина ЖДАТЬ — один раз
    _, ue = pb.build(pb.BY_ID["door_now"])
    assert "ПРОВЕРКА ВХОДА:" not in ue and "ПОМЕТКА" not in ue and ue.count(pb.DOOR_CHECK) == 1
    _, up = pb.build(pb.BY_ID["profit_fade"])
    assert "ПРИБЫЛЬ: пройдено 77 % хода от входа 298 до тейка 305" in up and "УРОВНИ ПОЗИЦИИ: вход 298" in up
    _, uv = pb.build(pb.BY_ID["verdict_split"])
    assert "═══ АНАЛИЗ ═══" in uv and "═══ КРИТИКА ═══" in uv and "спринг" in uv and "ПРОШЛЫЙ ПЛАН" in uv


@pytest.mark.parametrize("sid", [s["id"] for s in pb.SITUATIONS if s["node"] in pb.COMBAT_NODES])
def test_bench_prompt_equals_combat_node(sid):
    """Стенд = бой: настоящий узел mission.py (сбор данных, склейка ситуации и повода, промпт) в той же сцене собирает
    РОВНО промпт стенда — формат сборщиков и склейки не разъедется."""
    sit = pb.BY_ID[sid]
    system, user, route = pb.combat_prompt(sid)
    assert (system, user) == pb.build(sit)
    assert route == pb.ROUTE_OF[sit["node"]]


@pytest.mark.parametrize("sid,answer", pb.REVIEW_ANSWERS)
def test_scene_review_record_is_combat(sid, answer):
    """Запись перепроверки, которую сцена кладёт в историю, — та же, что пишет настоящий MissionPilot._review."""
    pb.check_review_record(sid, answer)


def test_scene_history_goes_through_combat_functions():
    """История сцен — настоящими функциями mission.py 5.4.3 (после F1): серия ЖДЁМ — счётчик m.wait_streak
    (_streak_after_review + мин/макс по тикам _flat_track), запись с in_pos/pos_side (_review_record), будильник ЖДЁМ
    без entry остаётся (_wake_after_review), метка модели ДЕРЖАТЬ в позиции."""
    sc = pb.SCENES["range_mid_wait"]()
    ws = sc.m.wait_streak
    assert ws["n"] == 2 and ws["ts"] == pb.ts("10:14") and ws["price"] == 302.2, ws
    assert (ws["lo"], ws["hi"]) == (299.4, 302.2), "цена за серию — с первого ЖДЁМ, по тикам"
    assert sc.m.reviews[-1]["in_pos"] is False and sc.m.reviews[-1]["pos_side"] is None
    assert sc.p._wake["level"] == 304.0 and sc.m.reviews[-1]["wake"] == 304.0
    dm = pb.SCENES["dead_market"]()
    assert dm.m.wait_streak["n"] == 6 and len(dm.m.reviews) == 6
    lp = pb.SCENES["long_pressure"]()
    rec = lp.m.reviews[-1]
    assert rec["in_pos"] is True and rec["pos_side"] == "long" and lp.m.wait_streak is None, rec
    assert lp.p.last_review["in_pos"] is True and mission._choice_label(lp.p.last_review) == "ДЕРЖАТЬ"
    # ЖДЁМ без entry оставляет несработавший будильник (ревью 5.4.3, D1) — сцена идёт через настоящую функцию
    sc2 = pb.SCENES["range_mid_wait"]()
    sc2.review("11:20", "ЖДЁМ", "без нового уровня", 300.0)
    assert sc2.p._wake["level"] == 304.0 and sc2.m.reviews[-1].get("wake_kept") is True
    assert sc2.m.wait_streak["n"] == 3


def test_bench_findings_are_fixed_in_combat_code():
    """Три находки стенда исправлены в боевом коде (промпт стенда = боевой узел байт в байт):
    (1) Вайкофф у перепроверки — подпись «на момент совета HH:MM, N мин назад, цена тогда P» и строка расстояний до
    краёв бокса от ТЕКУЩЕЙ цены; (2) свежая новость по тикеру — один раз; (3) возраст совета — от приказа."""
    s1 = pb.BY_ID["range_mid_wait"]["args"]
    assert s1["wyckoff_at"] == "на момент совета 09:22, 118 мин назад, цена тогда 299.8"
    assert s1["wyckoff"].startswith("СЕЙЧАС от цены 300 (края бокса — расчёт совета): D1 бокс 288.4 … 309.6")
    assert "цена 299.8 на 54% высоты бокса" in s1["wyckoff"], "слой совета — как был, под своей подписью"
    _, u1 = pb.build(pb.BY_ID["range_mid_wait"])
    assert "ВАЙКОФФ (на момент разбора)" not in u1 and "═══ ВАЙКОФФ (на момент совета 09:22" in u1
    sc = pb.SCENES["range_mid_wait"]()
    assert sc.m.wy_at["ts"] == pb.ts("09:22") and sc.m.wy_at["price"] == 299.8 and set(sc.m.wy_at["box"]) == {"D1", "H1"}
    # (2) ЦБ 10:40 — и в «свежих», и по тикеру: в промпте один раз
    _, un = pb.build(pb.BY_ID["news_shock"])
    assert un.count("[7c21e0]") == 1 and "— по инструменту (кроме свежих выше):" in un
    _, ul = pb.build(pb.BY_ID["long_pressure"])
    assert ul.count("[a4c9f2]") == 1 and "— по инструменту: только свежие выше" in ul
    # (3) door_now: совет 10:30–11:08, сейчас 11:20 — приказ 12 мин назад (раньше «50 мин назад»)
    sd = pb.BY_ID["door_now"]["args"]["situation"]
    assert "Последний полный совет: приказ пришёл 12 мин назад (совет шёл 38 мин, начат 10:30)" in sd
    assert "Последний полный совет: 50 мин назад" not in sd


def test_verdict_prev_and_news_from_combat_builders():
    sc = pb.SCENES["verdict_split"]()
    ctx = pb.BY_ID["verdict_split"]["args"]["ctx"]
    with sc.combat():
        assert ctx["prev"] == mission._prev_text(sc.m)
        assert ctx["council"] == mission._council_text()
        assert ctx["news"] == pb._drive(mission._news_for(sc.m))[1]
    assert ctx["reason"] == sc.m.handoffs[-1]["reason"] and ctx["reason"].startswith("перепроверка потребовала свежий разбор: ")
    assert "ЖДЁМ @298.4 → сейчас 299.2 (+0.27 %)" in ctx["prev"] and "Пилот: вне рынка" in ctx["prev"]


def test_scene_leaves_modules_as_they_were():
    """Сборка сцен и сверка с боем не оставляют подмен: время, ручки, соседние модули, ИИ — как были."""
    import time as _time
    knobs = {k: getattr(config, k, None) for k in pb.SCENE_KNOBS}
    names = ("newsflow", "watch", "council", "_scan_status_raw", "_persist", "explain", "_fit_blocks")
    before = {n: getattr(mission, n) for n in names}
    ai = (ai_v5.pro_json, ai_v5.money_json, ai_v5.now_msk_str, ai_v5._skew_s)
    pb.combat_prompt("door_now")
    pb.check_review_record(*pb.REVIEW_ANSWERS[0])
    pb.SCENES["news_shock"]()
    assert mission.time is _time and ai_pilot.time is _time and council.time is _time
    assert {k: getattr(config, k, None) for k in pb.SCENE_KNOBS} == knobs
    assert {n: getattr(mission, n) for n in names} == before
    assert (ai_v5.pro_json, ai_v5.money_json, ai_v5.now_msk_str, ai_v5._skew_s) == ai


def test_combat_check_writes_nothing_to_data():
    before = _data_snapshot()
    for s in pb.SITUATIONS:
        if s["node"] in pb.COMBAT_NODES:
            pb.combat_prompt(s["id"])
    for sid, answer in pb.REVIEW_ANSWERS:
        pb.check_review_record(sid, answer)
    assert _data_snapshot() == before


def test_kinds_and_routes_on_fake_ai(fake):
    before = _data_snapshot()
    rep = asyncio.run(pb.run(runs=1))
    got = {r["id"]: (r["answers"][0]["decision"], r["answers"][0]["kind"]) for r in rep["situations"]}
    assert got == pb.FAKE_WANT
    routes = {sid: route for sid, route, _ in fake.calls}
    assert {routes[s["id"]] for s in pb.SITUATIONS if s["node"].startswith("review")} == {"mission_review"}
    assert routes["door_now"] == "mission_entry" and routes["profit_fade"] == "mission_profit"
    assert routes["verdict_split"] == "mission_verdict"
    atts = {sid: a for sid, r, a in fake.calls if r in ("mission_entry", "mission_profit")}
    assert atts == {"door_now": float(config.PYTHIA_ENTRY_TIMEOUT_SEC), "door_breakout": float(config.PYTHIA_ENTRY_TIMEOUT_SEC),
                    "profit_fade": float(config.PYTHIA_PROFIT_TIMEOUT_SEC)}, "срок каждой попытки — как у боевого _money_call"
    t = rep["total"]
    assert t["n"] == 14 and t["silent"] == 1 and t["decided"] == 13
    assert abs(t["act_share"] + t["wait_share"] + t["council_share"] - 1.0) < 1e-6
    json.dumps(rep, ensure_ascii=False)
    assert _data_snapshot() == before


def test_failure_and_unparsed_are_silent_and_bench_goes_on(fake):
    fake.answers["door_now"] = RuntimeError("сеть упала")
    fake.answers["profit_fade"] = asyncio.TimeoutError()
    fake.answers["long_resistance"] = {"choice": "НЕ ВХОДИТЬ", "why": "?"}
    fake.answers["range_mid_wait"] = "не json"
    rep = asyncio.run(pb.run(ids=["door_now", "profit_fade", "long_resistance", "range_mid_wait", "short_add"], runs=2))
    r = {x["id"]: x for x in rep["situations"]}
    assert [a["decision"] for a in r["door_now"]["answers"]] == ["НЕТ_ОТВЕТА", "НЕТ_ОТВЕТА"]
    assert "сеть упала" in r["door_now"]["answers"][0]["why"] and r["door_now"]["act_share"] is None
    assert r["profit_fade"]["answers"][0]["why"] == "ответа не было: таймаут"
    assert r["long_resistance"]["answers"][0]["decision"] == "НЕ_РАЗОБРАН" and r["long_resistance"]["silent"] == 2
    assert r["range_mid_wait"]["answers"][0]["kind"] == "silent"
    assert [a["decision"] for a in r["short_add"]["answers"]] == ["ДОБРАТЬ", "ДОБРАТЬ"], "стенд идёт дальше"
    assert rep["total"]["silent"] == 8 and rep["total"]["decided"] == 2 and rep["total"]["act_share"] == 1.0


def test_select_filters_and_rejects_unknown():
    assert [s["id"] for s in pb.select(nodes=["entry"])] == ["door_now", "door_breakout"]
    assert [s["id"] for s in pb.select(["profit_fade"], ["profit"])] == ["profit_fade"]
    assert pb.select(["profit_fade"], ["entry"]) == []
    with pytest.raises(ValueError):
        pb.select(["нет_такой"])
    with pytest.raises(ValueError):
        pb.select(nodes=["triage"])


@pytest.mark.parametrize("text,in_pos,want", [
    ("Вердикт: BUY от 299.2, стоп 295.6", False, "BUY"),
    ("## ВЕРДИКТ\nВне рынка до закрепления над 300.5", False, "WAIT"),
    ("ВЕРДИКТ. SBER — long от 299.2", False, "BUY"),
    ("Решение: не покупать, ждать 300.5", False, "WAIT"),
    ("**Итог:** SELL от 300 к 296", False, "SELL"),
    ("1. Выход из бокса вверх — не сейчас\n2. Сторона: шорт от 300.4", False, "SELL"),
    ("Вердикт: держать лонг", False, "HOLD"),
    ("Вердикт: закрыть позицию", True, "CLOSE"),
    ("Разбор без решения", False, None),
])
def test_verdict_decision(text, in_pos, want):
    assert pb.verdict_decision(text, in_pos)[0] == want


def test_verdict_hold_flat_is_wait():
    sit = pb.BY_ID["verdict_split"]
    rec = pb.parse(sit, "Вердикт: держать, вне рынка")
    assert rec["decision"] == "HOLD" and rec["kind"] == "wait"
    think = ai_v5.ai.THINK_MARK + "\nчерновик: BUY"
    assert pb.parse(sit, think)["decision"] == "НЕ_РАЗОБРАН", "черновик размышления — не решение"


def test_reports_contain_all_ids(fake):
    rep = asyncio.run(pb.run(runs=1))
    txt, md = pb.report_text(rep), pb.report_md(rep)
    for s in pb.SITUATIONS:
        assert s["id"] in txt and s["id"] in md
    assert "ПО УЗЛАМ" in txt and "ВСЕГО" in txt and "доли — от ответов с решением" in txt
    assert md.startswith("# Стенд промптов") and "## Сводка" in md and "## Ответы по ситуациям" in md
    assert "стоп 295.6" in md, "уровни ответа в деталях"


def test_dump_writes_28_files_without_ai(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("dump не зовёт ИИ")
    monkeypatch.setattr(ai_v5, "pro_json", boom)
    monkeypatch.setattr(ai_v5, "money_json", boom)
    monkeypatch.setattr(ai_v5, "pro_text", boom)
    files = pb.dump(tmp_path / "d")
    assert len(files) == 28 and len(list((tmp_path / "d").iterdir())) == 28
    s, u = pb.build(pb.BY_ID["door_now"])
    assert (tmp_path / "d" / "door_now.system.txt").read_text(encoding="utf-8") == s
    assert (tmp_path / "d" / "door_now.user.txt").read_text(encoding="utf-8") == u
    assert pb.main(["--dump", str(tmp_path / "cli")]) == 0 and len(list((tmp_path / "cli").iterdir())) == 28
    assert pb.main(["--dump", str(tmp_path / "one"), "--only", "profit_fade"]) == 0
    assert sorted(p.name for p in (tmp_path / "one").iterdir()) == ["profit_fade.system.txt", "profit_fade.user.txt"]


def test_cli_without_key_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("PYTHIA_MOCK_AI", raising=False)
    monkeypatch.setattr(ai_v5, "has_key", lambda: False)
    assert pb.main(["--run"]) == 2
    assert "нет ключа DeepSeek: задай ключ в панели или PYTHIA_MOCK_AI=1 для сухого прогона" in capsys.readouterr().err
    assert pb.main(["--runs", "2", "--only", "door_now"]) == 2, "любой флаг прогона — прогон"
    assert pb.main(["--only", "нет_такой", "--run"]) == 2


def test_cli_run_writes_reports(fake, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("PYTHIA_MOCK_AI", raising=False)
    monkeypatch.setattr(ai_v5, "has_key", lambda: True)
    md, js = tmp_path / "r" / "bench.md", tmp_path / "r" / "bench.json"
    assert pb.main(["--runs", "2", "--only", "range_mid_wait,door_now,verdict_split", "--out", str(md),
                    "--json", str(js), "--par", "2"]) == 0
    out = capsys.readouterr().out
    assert "range_mid_wait" in out and "ВСЕГО" in out
    rep = json.loads(js.read_text(encoding="utf-8"))
    assert [r["id"] for r in rep["situations"]] == ["range_mid_wait", "door_now", "verdict_split"]
    assert all(len(r["answers"]) == 2 for r in rep["situations"]) and rep["meta"]["runs"] == 2
    assert "door_now" in md.read_text(encoding="utf-8")


def test_cli_no_args_is_selftest(capsys):
    assert pb.main([]) == 0
    assert "prompt_bench self-test OK" in capsys.readouterr().out
