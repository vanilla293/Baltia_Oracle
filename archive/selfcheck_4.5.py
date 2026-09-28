# -*- coding: utf-8 -*-
"""САМОПРОВЕРКА ПИФИИ: все внешние запросы (Tinkoff, MOEX, ЦБ, погода,
новости, DeepSeek) — успешные пути И пути ошибок.

Запуск (из папки проекта, с активным .venv):
  TINKOFF_TOKEN=t.…  DEEPSEEK_API_KEY=sk-…  python selfcheck.py
Без DEEPSEEK_API_KEY блок DeepSeek пропускается, остальное проверяется."""
import asyncio, os, sys, time, traceback

# токен Tinkoff берётся из окружения: set TINKOFF_TOKEN=t.…
os.environ["WIPE_KEYS_ON_START"] = "0"   # тест модулей, не сервера
DS_KEY = os.getenv("DEEPSEEK_API_KEY", "")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backend import tinkoff, moex, instruments, news, weather, underlying, ai, wyckoff, astro, aether, memory, pipeline, reactor_sky, maya, maya_scan

PASS, FAIL = [], []

def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗ FAIL'} {name}" + (f" — {extra}" if extra else ""))

async def t_aether():
    print("═══ ЭФИР (REAL SKY, офлайн-математика) ═══")
    import numpy as _np
    n = 256; t = _np.arange(n)
    z = aether._analytic(_np.cos(2*_np.pi*8*t/n))
    core = slice(n // 8, -n // 8)   # v3.1: точность 1e-9 была артефактом кольца
    ok("Гильберт H[cos]=sin (ядро)", bool(_np.allclose(
        z.imag[core], _np.sin(2*_np.pi*8*t/n)[core], atol=0.02)))
    tt9 = _np.arange(64, dtype=float)
    ph9 = _np.unwrap(_np.angle(aether._analytic(_np.sin(2*_np.pi*tt9/16.0+0.3))))
    ok("Гильберт: наклон фазы правого края честен (AR-хвост v3.1)",
       bool(abs(float(ph9[-1]-ph9[-2]) - 2*_np.pi/16) < 0.05))
    pl = aether.plates_of(list(range(1, 40)))
    ok("плиты: порядок", bool(pl and pl["floor"] < pl["mid"] < pl["ceil"]))
    ok("плиты: мало точек → None", aether.plates_of([1, 2]) is None)
    # ── v3.9 «Эфир на физике»: вес — фотометрия, частоты — dλ/dt ──
    ok("вес: Луна на среднем расстоянии → W_flux = 1",
       abs(float(aether.w_flux("Луна", 1.0)) - 1.0) < 1e-12)
    ok("вес: монотонность по диску (ближе/больше → громче)",
       float(aether.w_flux("Луна", 1.2)) > float(aether.w_flux("Луна", 1.0))
       > float(aether.w_flux("Луна", 0.8)))
    ok("вес: монотонность по альбедо (Венера ярче Марса при том же диске)",
       float(aether.w_flux("Венера", 0.5)) > float(aether.w_flux("Марс", 0.5)))
    ok("вес: Солнце — по апертуре (albedo:=1, честный прокси в note)",
       aether.ALBEDO_G["Солнце"] == 1.0)
    ok("Курамото: ω станции → 0 и монотонен по скорости",
       aether._omega_of(0.0) == 0.0
       and aether._omega_of(13.2) > aether._omega_of(1.0) > 0.0)
    _ts9 = _np.arange(0, 49) * 1800.0
    _lam9 = {nm: _np.full(49, 200.0) for nm in aether._BODY_ORDER}
    _lam9["Луна"] = 138.0 + 0.5 * _np.arange(49)
    _tr9 = {"ts": _ts9, "lam": _lam9,
            "E": {nm: _np.ones(49) for nm in aether._BODY_ORDER}}
    _dw9 = aether.dirac_windows(_tr9, 108, top=100)
    _l60 = [w for w in _dw9 if "Луна" in w["names"] and w["aspect"] == "секстиль"]
    ok("Дирак: σ — кинематика (24°/сут → 3 мин → кламп снизу 15), не нота band",
       bool(_l60) and _l60[0]["sigma_min"] == 15)
    _r9 = aether.kuramoto_r(_tr9, 0)
    ok("Курамото: r в диапазоне и детерминизм байт-в-байт",
       0.0 <= _r9 <= 1.0 and _r9 == aether.kuramoto_r(_tr9, 0))
    g = aether.genesis_for("BRU6")
    ok("генезис BR из реестра", g["key"] == "BR" and not g["fallback"])
    ok("неизвестный тикер → честный фолбэк", aether.genesis_for("XXXX")["fallback"])
    ctx = aether.compute_context("BR-TEST", None)
    if ctx.get("mode") == "precise":
        ok("контекст без свечей (небесная часть)", "wave" in ctx and ctx["wave"]["psi"])
        from datetime import datetime as _dt, timezone as _tz
        _tr = aether.transit_series(_dt(2026, 8, 4, tzinfo=_tz.utc), 24.0, 1.0)
        ok("серии v3.9 несут vel (°/сут): Луна ~13, детерминизм эфемерид",
           bool(_tr) and "vel" in _tr
           and 10.0 < float(_np.abs(_tr["vel"]["Луна"]).mean()) < 16.0)
        _chr = _np.minimum(aether.CHRONO_CAP, 1.0 + 1.0 / (
            _np.abs(_tr["vel"]["Луна"]) + aether.EPS_CHRONO))
        ok("физика E: Луна ≈ 1 (E/K_chrono = d_ap³), диск Луны ≫ Юпитера",
           0.5 < float((_tr["E"]["Луна"] / _chr).mean()) < 2.0
           and float(_tr["E"]["Луна"].mean()) > 50 * float(_tr["E"]["Юпитер"].mean()))
        txt = aether.render_for_ai(ctx)
        ok("блок для ИИ собран", "ЭФИРНЫЙ СЛОЙ" in txt and "18+" in txt)
        low = txt.lower()
        ok("запрет торговых слов в блоке", all(b not in low for b in
           ("покупай", "продавай", "лонг", "шорт")))
    else:
        ok("нет .bsp → честный unavailable", ctx.get("mode") == "unavailable",
           ctx.get("reason", ""))


async def t_reactor_maya():
    print("═══ ⚛ РЕАКТОР НЕБА + МАЙЯ (канон 05.08, офлайн) ═══")
    # Z-калибровка (единый источник — aether.impedance, реюз в реакторе)
    ok("Z(θ): калибровка журнала", reactor_sky.impedance(0.0) < 1e-9
       and abs(reactor_sky.impedance(90.0) - 1.30) < 0.05
       and abs(reactor_sky.impedance(120.0) - 0.69) < 0.05
       and reactor_sky.impedance(120.0) < reactor_sky.impedance(90.0))
    ok("Z(θ) — реюз aether.impedance (не копия)",
       reactor_sky.impedance is aether.impedance)
    # хроно-удар: кламп станции и монотонность
    ok("хроно-удар: станция → кэп ×50", reactor_sky.k_chrono(0.0) == 50.0)
    ok("хроно-удар: монотонность по |v|",
       reactor_sky.k_chrono(0.01) > reactor_sky.k_chrono(0.1) > reactor_sky.k_chrono(13.2))
    # КАМ: Φ-канал у золотого сечения, мост у 3/2
    kφ = reactor_sky.kam_of(reactor_sky.PHI)
    k32 = reactor_sky.kam_of(1.5)
    ok("КАМ: φ → Φ-канал, 3/2 → рациональный мост",
       bool(kφ and kφ["phi"]) and bool(k32 and k32["rational"]))
    ok("КАМ: вырожденные входы → None",
       reactor_sky.kam_of(0.0) is None and reactor_sky.kam_of(float("nan")) is None)
    # контекст реактора жив (эфемериды есть → панель; нет → честный отказ)
    r_ctx = reactor_sky.reactor_context("BRU6")
    if r_ctx.get("mode") == "precise":
        ok("reactor_context: 10 тел, K в клампе",
           len(r_ctx["bodies"]) == 10
           and all(1.0 <= b["k_chrono"] <= 50.0 for b in r_ctx["bodies"]))
        txt = reactor_sky.render_for_ai(r_ctx)
        ok("панель реактора: рамка и запреты", "⚛ РЕАКТОР" in txt and "18+" in txt
           and all(b not in txt.lower() for b in ("покупай", "продавай", "лонг", "шорт")))
        ok("панель реактора: токен-бюджет", len(txt) < 4500, f"{len(txt)} символов")
    else:
        ok("reactor_context: нет .bsp → честный unavailable",
           r_ctx.get("mode") == "unavailable", r_ctx.get("reason", ""))
    # МАЙЯ: синтетический стакан-литерал — вакуум/стена/тяга там, где выложены
    bids_s = [{"p": round(100.0 - 0.1 * i, 1), "q": 1000 if i == 5 else 100}
              for i in range(50)]
    asks_s = [{"p": round(100.1 + 0.1 * i, 1), "q": 1 if 10 <= i <= 14 else 100}
              for i in range(50)]
    mm = maya.analyze({"best_bid": 100.0, "best_ask": 100.1,
                       "levels_bid": bids_s, "levels_ask": asks_s},
                      {"aggressor_ratio": 0.62})
    ok("майя: вакуум найден там, где выложен разрыв",
       mm["available"] and abs(mm["vacuum_up"]["p_from"] - 101.1) < 1e-9
       and mm["vacuum_up"]["depth"] > 0.9)
    ok("майя: стена найдена там, где выложена",
       mm["wall_bid"] and abs(mm["wall_bid"]["p"] - 99.5) < 1e-9)
    ok("майя: тяга вверх (имплозия в разрежение ask) + агрессор согласен",
       mm["pull"]["side"] == "вверх" and mm["aggressor"]["with_pull"] is True)
    ok("майя: нет стакана → честный отказ (NO DUMMIES)",
       maya.analyze(None)["available"] is False)
    mtxt = maya.render_for_ai(mm)
    ok("майя: рендер с рамкой ⚫/18+", "18+" in mtxt and "⚫" in mtxt)
    # сканер: агрегаты на синтетических снимках (без сети)
    from collections import deque as _dq
    _sn = []
    for _i in range(30):
        _sn.append({"ts": 1.0 + _i, "bid": 100.0, "ask": 100.1,
                    "pull": ("вверх" if _i % 5 < 3 else "вниз"),
                    "s_up": .6, "s_dn": .2,
                    "vu": {"p_from": 101.1, "p_to": 101.5, "depth": .9},
                    "vd": {"p_from": 98.0, "p_to": 98.4, "depth": .2},
                    "wa": {"p": 102.0, "q": 500, "mult": 5.0},
                    "wb": {"p": round(99.5 - .01 * _i, 2), "q": 900, "mult": 6.0},
                    "ar": .62, "imb": .1})
    _st = {"snaps": _dq(_sn), "errors": 0, "started": 1.0,
           "until": 3600.0, "task": None, "ether": None}
    _agg = maya_scan.aggregate(_st)
    ok("майя-сканер: прокол там, где вакуум держат",
       bool(_agg.get("punctures")) and abs(_agg["punctures"]["up"][0]["p_lo"] - 101.1) < 1e-9)
    ok("майя-сканер: консенсус тяги вверх + агрессор согласен",
       (_agg.get("consensus") or {}).get("now_side") == "вверх"
       and (_agg.get("consensus") or {}).get("agree") is True)
    ok("майя-сканер: стена-призрак ловится",
       bool((_agg.get("wall_bid") or {}).get("ghost")))
    ok("майя-сканер: статус без запуска — честный",
       maya_scan.status("НЕТ-ТАКОГО").get("running") is False)


async def t_tinkoff():
    print("═══ TINKOFF (все эндпоинты) ═══")
    if not tinkoff.enabled():
        print("  (пропуск: не задан TINKOFF_TOKEN — честный пропуск, не падение)")
        return
    inst = await tinkoff.resolve("SBER", "share")
    ok("resolve share SBER", bool(inst and inst.get("figi")), f"figi={inst.get('figi') if inst else None}")
    figi, uid = inst["figi"], inst.get("uid")
    auid = inst.get("assetUid")

    lp = await tinkoff.last_price(figi)
    ok("GetLastPrices", bool(lp and lp.get("price", 0) > 0), f"price={lp.get('price') if lp else None}")
    cp = await tinkoff.close_price(figi)
    ok("GetClosePrices", isinstance(cp, float) and cp > 0, f"close={cp}")
    cp2 = await tinkoff.close_price(figi)   # из кэша (60с)
    ok("GetClosePrices cache", cp2 == cp)
    cd = await tinkoff.candles(figi, "1d", 400)
    ok("GetCandles 1d/400", len(cd) >= 200, f"{len(cd)} свечей (нужно ≥200 для EMA200)")
    ci = await tinkoff.candles(figi, "5m", 3)
    ok("GetCandles 5m/3", len(ci) > 0, f"{len(ci)} свечей")
    ob = await tinkoff.orderbook(figi, depth=50)
    ok("GetOrderBook", ob is None or ("best_bid" in ob), "нет стакана (вечер/выходной)" if ob is None else f"bid={ob['best_bid']}")
    tp = await tinkoff.last_trades(figi, minutes=60)
    ok("GetLastTrades", tp is None or "count" in tp, f"count={tp.get('count') if tp else None}")
    ts = await tinkoff.trading_status(figi)
    ok("GetTradingStatus", ts is None or "status" in ts, f"status={ts.get('status') if ts else None}")
    te = await tinkoff.tech(uid, "RSI", 14, "1h", 20)
    ok("GetTechAnalysis RSI", te is None or isinstance(te, list), f"{len(te) if te else 0} точек")
    cons = await tinkoff.consensus(uid)
    ok("GetForecastBy (консенсус)", cons is None or "reco" in cons, f"reco={cons.get('reco') if cons else 'нет'}")
    dv = await tinkoff.next_dividend(uid)
    ok("GetDividends (ближайший)", dv is None or "record_date" in dv, f"{dv.get('record_date','')[:10] if dv else 'нет объявленных'}")
    dvs = await tinkoff.dividends(figi)
    ok("GetDividends (история)", dvs is None or isinstance(dvs, list), f"{len(dvs) if dvs else 0} записей")
    if auid:
        fu = await tinkoff.fundamentals(auid)
        ok("GetAssetFundamentals", fu is None or "pe_ttm" in fu or len(fu) > 0, f"{len(fu) if fu else 0} полей")

    # метрики из свечей
    dm = tinkoff.daily_metrics(cd)
    ok("daily_metrics EMA200 (исправление)", dm.get("ema200") is not None, f"ema200={dm.get('ema200')}")
    ok("daily_metrics RSI/ATR", dm.get("rsi14") is not None and dm.get("atr14") is not None,
       f"rsi={dm.get('rsi14')} atr={dm.get('atr14')}")
    im = tinkoff.intraday_metrics(ci)
    ok("intraday_metrics VWAP", "vwap" in im, f"vwap={im.get('vwap')}")

    # фьючерс: авто-ролловер + ГО
    fin = await tinkoff.resolve("BR", "futures")
    ok("resolve futures BR (авто-ролловер)", bool(fin and fin.get("ticker")), f"→{fin.get('ticker') if fin else None} exp={str(fin.get('expirationDate',''))[:10] if fin else ''}")
    if fin:
        mg = await tinkoff.futures_margin(fin.get("figi") or fin.get("uid"))
        ok("GetFuturesMargin", mg is None or "margin_buy" in mg, f"ГО={mg.get('margin_buy') if mg else None}")
    cur = await tinkoff.resolve("CNYRUB_TOM", "currency")
    ok("resolve currency (не падает)", True, f"{'найден' if cur else 'нет — допустимо'}")

    # ПУТИ ОШИБОК
    bad = await tinkoff.resolve("НЕСУЩЕСТВУЕТ99", "share")
    ok("resolve мусорный тикер → None без падения", bad is None)
    lp_bad = await tinkoff.last_price("не-figi-мусор")
    ok("last_price мусорный figi → None без падения", lp_bad is None)
    d = await tinkoff.collect_dossier("SBER", "share")
    ok("collect_dossier SBER полное", bool(d.get("price")) and bool(d.get("daily_metrics")),
       f"блоков={len([k for k in d if d.get(k)])} ошибок={len(d.get('errors',[]))}")
    d2 = await tinkoff.collect_dossier("ЗЗЗ111", "share")
    ok("collect_dossier мусор → errors, без падения", "инструмент не найден в Tinkoff" in " ".join(d2.get("errors", [])))

async def t_moex():
    print("═══ MOEX ISS (фолбэк) ═══")
    lp = await moex.last_price("SBER", "share")
    ok("last_price акция", bool(lp and lp.get("price")), f"price={lp.get('price') if lp else None}")
    cd = await moex.candles("SBER", "share", "1d", 30)
    ok("candles акция 1d", len(cd) > 5, f"{len(cd)} свечей")
    ci = await moex.candles("SBER", "share", "10m", 2)
    ok("candles акция 10m", isinstance(ci, list), f"{len(ci)} свечей")
    sec = await moex.resolve_futures("BR")
    ok("resolve_futures BR → живой контракт", bool(sec), f"→{sec}")
    sec2 = await moex.resolve_futures("SBER")           # алиас SBRF
    ok("resolve_futures алиас SBER→SBRF", bool(sec2), f"→{sec2}")
    if sec:
        fcd = await moex.candles(sec, "futures", "1d", 10)
        ok("candles фьючерса", isinstance(fcd, list), f"{len(fcd)} свечей")
        flp = await moex.last_price("BR", "futures")    # авто-резолв внутри
        ok("last_price фьючерс через базовый код", flp is None or flp.get("price"), f"price={flp.get('price') if flp else 'нет (вечер?)'}")
    bad = await moex.last_price("МУСОР999", "share")
    ok("last_price мусор → None без падения", bad is None)
    bad2 = await moex.candles("МУСОР999", "share", "1d", 5)
    ok("candles мусор → [] без падения", bad2 == [])
    # вселенная
    uni = await instruments.fetch_universe()
    ok("fetch_universe (акции+фьючерсы)", len(uni) > 300, f"{len(uni)} инструментов")
    ok("instruments.get SBER", instruments.get("SBER") is not None)
    ok("instruments.get мусор → None", instruments.get("ЙЦУ12") is None)
    ok("guess_asset_class BRQ6 → futures", instruments.guess_asset_class("BRQ6") == "futures")
    ok("guess_asset_class LKOH → share", instruments.guess_asset_class("LKOH") == "share")

async def t_underlying():
    print("═══ ПЕРВОИСТОЧНИК (базовый актив) ═══")
    r = await underlying._cbr_rate("USD")
    ok("курс ЦБ USD", r is None or (50 < r < 300), f"{r}")
    px, cnd = await underlying._moex_index("IMOEX")
    ok("индекс IMOEX", px is None or px > 500, f"{px}, свечей={len(cnd)}")
    d = await tinkoff.collect_dossier("BR", "futures")
    u = await underlying.collect(d, "BR")
    ok("underlying BR (сырьё → пропуск без падения)", u is None, "нефть: биржевого спота нет — корректный пропуск")
    d2 = await tinkoff.collect_dossier("SI", "futures")
    u2 = await underlying.collect(d2, "SI")
    ok("underlying SI (USD/RUB через ЦБ)", u2 is None or "basis_pct" in u2,
       f"базис={u2.get('basis_pct') if u2 else 'нет'}% {u2.get('state','') if u2 else ''}")

async def t_news_weather():
    print("═══ НОВОСТИ + ПОГОДА ═══")
    t0 = time.time()
    items = await news.fetch_news()
    ok("fetch_news", len(items) > 50, f"{len(items)} новостей за {time.time()-t0:.1f}с")
    t0 = time.time()
    items2 = await news.fetch_news()
    ok("fetch_news кэш", (time.time() - t0) < 0.5, f"повтор {time.time()-t0:.2f}с")
    tk_items, note = await news.fetch_for_ticker("SBER", "Сбербанк", asset_class="share")
    ok("fetch_for_ticker SBER", isinstance(tk_items, list), f"{len(tk_items)} целевых ({note})")
    w = await weather.fetch_weather([{"name": "Амстердам", "lat": 52.37, "lon": 4.9}])
    ok("weather open-meteo", len(w) == 1 and w[0].get("temp_now") is not None, f"t={w[0].get('temp_now') if w else '?'}°C")
    w2 = await weather.fetch_weather([])
    ok("weather пустой список → []", w2 == [])

async def t_ai():
    print("═══ DEEPSEEK ═══")
    if not DS_KEY:
        print("  (пропуск: не задан DEEPSEEK_API_KEY в окружении)")
        return
    out = await ai.ask("Ты эхо.", "Ответь одним словом: готов", thinking=False,
                       max_tokens=20, route="check", api_key=DS_KEY)
    ok("ask (flash-путь)", "готов" in out.lower() or len(out) > 0, f"'{out[:40]}'")
    j = await ai.ask_json("Верни строго JSON {\"x\": 1}.", "Дай JSON.", thinking=False,
                          max_tokens=100, route="check_json", api_key=DS_KEY)
    ok("ask_json", isinstance(j, dict) and j.get("x") == 1, f"{j}")
    chunks = []
    async def on_text(d, _): chunks.append(d)
    st = await ai.stream("Ты эхо.", "Посчитай от 1 до 5 через запятую.",
                         on_text=on_text, thinking=False, max_tokens=60,
                         route="check_stream", api_key=DS_KEY)
    ok("stream + дельты", len(chunks) >= 1 and len(st) > 0, f"{len(chunks)} дельт, '{st[:30]}'")
    # ошибки
    try:
        await ai.ask("Эхо.", "хх", thinking=False, max_tokens=10, route="bad", api_key="sk-невалидный000")
        ok("невалидный ключ → исключение", False)
    except Exception as e:
        msg = ai.humanize_error(e)
        ok("невалидный ключ → человеческая ошибка", "ключ" in msg.lower() or "401" in str(e), f"'{msg[:60]}'")
    ok("_extract_json из ```fence```", ai._extract_json('```json\n{"a":2}\n```') == {"a": 2})
    ok("_extract_json из текста", ai._extract_json('мусор {"b":3} хвост') == {"b": 3})

async def t_local():
    print("═══ ЛОКАЛЬНЫЕ РАСЧЁТЫ ═══")
    if tinkoff.enabled():
        cd = await tinkoff.candles((await tinkoff.resolve("SBER", "share"))["figi"], "1d", 400)
        wy = wyckoff.analyze(cd, "1d")
        ok("wyckoff.analyze", wy is None or ("phase" in wy and "bias" in wy),
           f"{wy.get('read')} {wy.get('phase')} bias={wy.get('bias')}" if wy else "мало данных")
    else:
        print("  (wyckoff по свечам: пропуск — нет TINKOFF_TOKEN; астро-часть идёт дальше)")
    ctx = await astro.acontext()
    ok("astro.acontext", bool(ctx) and "moon" in ctx, f"mode={ctx.get('mode')}")
    line = astro.short_line(ctx)
    ok("astro.short_line", isinstance(line, str) and len(line) > 3, f"'{line[:50]}'")
    # ── НАСТОЯЩЕЕ 3D: раньше здесь не проверялось положение тел вообще, и движок
    #    мог назвать Солнце Стрельцом — ни один assert бы не упал ──
    P = ctx.get("planets") or []
    if ctx.get("mode") == "precise":
        ok("astro: широта не выброшена",
           bool(P) and all(p.get("lat") is not None for p in P),
           f"β есть у {sum(1 for p in P if p.get('lat') is not None)}/{len(P)} тел")
        ok("astro: реальное созвездие у всех тел",
           bool(P) and all(p.get("con") for p in P),
           ", ".join(f"{p['name']}={p.get('con')}" for p in P[:3]))
        moved = [p["name"] for p in P if p.get("beta_matters")]
        mism = [p["name"] for p in P if p.get("con") and p["con"] != p.get("sign")]
        ok("astro: 3D включилось (сетка ≠ небо)", len(mism) > 0,
           f"сетка врёт у {len(mism)}/{len(P)}; широта увела: {moved or '—'}")
        ok("astro: β Плутона/Луны не ноль",
           all(abs((next((p for p in P if p['name'] == n), {}) or {}).get("lat") or 0) > 0.05
               for n in ("Плутон", "Луна")),
           "широта живая, а не заглушка")
        sky = ctx.get("sky") or {}
        ok("astro: расхождение сетки измерено",
           sky.get("drift_deg") is not None and 23.0 <= sky["drift_deg"] <= 26.0,
           f"тропика отстала на {sky.get('drift_deg')}°")
        hr = (ctx.get("houses") or {}).get("real") or {}
        ok("astro: дома по реальным границам", hr.get("n_houses") == 13,
           f"домов {hr.get('n_houses')} (не 12), сумма ширин {hr.get('total_width_deg')}°")
        ok("astro: равные дома остались проекцией",
           bool((ctx["houses"].get("equal") or {}).get("note")) and len(ctx["houses"]["cusps"]) == 12,
           "🟡 ключ equal на месте, старый cusps цел")
        A = ctx.get("aspects") or []
        ok("astro: sep3d рядом с орбом (канон цел)",
           bool(A) and all("orb" in a and "sep3d" in a for a in A),
           f"аспектов {len(A)}, истинный угол посчитан")
        txt = astro.render_for_ai(ctx)
        ok("astro: в тексте для ИИ небо первым",
           "🔵" in txt and "🟡" in txt and "⚫" in txt and "ОТСТАЛА ОТ НЕБА" in txt,
           "рамка и строка про расхождение на месте")
        ok("astro: ни одного поля None без note",
           all(p.get("con") or p.get("note") for p in P),
           "NO DUMMIES соблюдён")
    # память
    await memory.save_analysis({"ticker": "TST1", "name": "тест", "asset_class": "share",
                                "dossier": {"x": 1}, "news": [], "tags": [], "background": "",
                                "analyst": "а", "critic": "б", "verdict": "в",
                                "forecast": {"action": "FLAT"}, "thinking": {"analyst": "мысль"}})
    rec = await memory.get_analysis("TST1")
    ok("memory save/get", rec is not None and rec["verdict"] == "в" and rec["thinking"]["analyst"] == "мысль")
    lst = await memory.list_analyses()
    ok("memory list", any(a["ticker"] == "TST1" for a in lst))
    await memory.add_chat("TST1", "user", "привет")
    ch = await memory.get_chat("TST1")
    ok("memory chat", len(ch) == 1 and ch[0]["content"] == "привет")
    await memory.hard_reset()
    ok("memory hard_reset", await memory.get_analysis("TST1") is None)
    # санитайзер прогноза
    f = pipeline._sanitize_forecast({"action": "long", "conviction": "77,5", "risk_pct": 250,
                                     "forecast_1h": {"direction": "UP", "target": "105,3", "probability": 61.7},
                                     "support": ["100,1", None, "abc", 99], "voice_line": "тест"}, 104.0)
    ok("_sanitize_forecast типы", f["action"] == "LONG" and f["conviction"] == 77 and f["risk_pct"] == 100
       and f["forecast_1h"]["target"] == 105.3 and f["support"] == [100.1, 99.0], str({k: f[k] for k in ('action','conviction','risk_pct')}))

async def t_oracle_core():
    """ОРАКУЛ-ЯДРО v4.0: двигатель бифуркаций + свод голосов + лестница."""
    print("═══ ОРАКУЛ-ЯДРО (бифуркации · свод · лестница, офлайн) ═══")
    import numpy as _np
    from backend import bifurcation, leverage, oracle
    i = _np.arange(400, dtype=float)
    dt = (425.0 - i)
    bubble = 100.0 * _np.exp(0.05 - 0.004 * dt ** 0.45
                             * (1.0 + 0.05 * _np.cos(8.0 * _np.log(dt))))
    f = bifurcation.lppls(bubble)
    ok("LPPLS Сорнетта находит синтетическую сингулярность",
       bool(f and f["qualified"] and abs(f["tc_bars_ahead"] - 25) <= 15))
    bc = bifurcation.bifurcation_context(bubble)
    ok("свод бифуркаций даёт давление и предсказуемость",
       bc["summary"]["pressure"] is not None
       and bc["summary"]["predictability"] is not None)
    ok("NO DUMMIES: короткий ряд → «нет данных»",
       bifurcation.bifurcation_context([1.0, 2.0])["summary"]["regime"] == "нет данных")
    v = oracle.verdict({"xray": {"available": True, "confidence": 0.7,
                                 "metrics": {"cvd": 300, "obi": 0.4}},
                        "trend": "up", "hurst": 0.62})
    ok("Оракул сводит голоса в вероятность", v["dir"] == "long" and v["p_long"] > 0.6)
    ok("режим «край−середина» маршрутизирует",
       oracle.verdict({"wave": {"psi_now": 0.5}})["mode"] == "mean_revert")
    # «Хищник»: машина состояний — сингулярность перехватывает парламент
    v_ov = oracle.verdict({
        "xray": {"available": True, "confidence": 0.7,
                 "metrics": {"cvd": 300, "obi": 0.4}}, "trend": "up",
        "bif": {"summary": {"direction": -0.7, "pressure": 0.8,
                            "predictability": 0.6, "window_open": True,
                            "regime": "БИФУРКАЦИЯ ОТКРЫТА"}}})
    ok("МАШИНА СОСТОЯНИЙ: сингулярность перехватывает парламент",
       v_ov["state"] == oracle.ST_SINGULARITY and v_ov["override"]
       and v_ov["dir"] == "short")
    ok("фронтраннинг отменяет фейд (Ψ→ликвидность)",
       oracle.verdict({"wave": {"psi_now": 0.5},
                       "xray": {"available": True, "confidence": 0.6,
                                "metrics": {"cvd": 900, "obi": 0.3,
                                            "vpin": 0.65}}})["mode"] == "momentum")
    from backend import hawkes as _hw
    ok("Хоукс: лавины → ветвление высокое, ровный поток → 0",
       (_hw.branching(([0] * 9 + [40]) * 12) or {}).get("n", 0) > 0.8
       and (_hw.branching([4] * 100) or {}).get("n") == 0.0)
    from backend import bif_calendar as _bc
    _per = _bc.load_periods()
    ok("календарь: язык Матьё n=2 Луны ловится на месячном цикле",
       any(t["body"] == "Луна" and t["n"] == 2
           for t in _bc.mathieu_tongues(29.5, 1440.0, _per)))
    lad_hi = leverage.ladder(0.9, 0.7)
    ok("лестница: убеждённость → потолок", lad_hi["frac"] == 1.0)
    ok("лестница: killswitch → ноль",
       leverage.ladder(0.9, 0.7, locked=True)["frac"] == 0.0)
    ok("окно бифуркации → пол 0.9",
       leverage.ladder(0.55, 0.5, window_open=True)["frac"] >= 0.9)
    ok("губернатор Ляпунова режет аппаратно",
       leverage.ladder(0.9, 0.8, lyap_horizon=2.0)["frac"]
       < leverage.ladder(0.9, 0.8)["frac"])
    ok("tail-кэп Мандельброта: α=2 → доля ≤ 0.2, Келли отключён",
       leverage.ladder(0.9, 0.8, window_open=True, tail_alpha=2.0)["frac"] <= 0.2
       and leverage.ladder(0.8, 0.6, p_dir=0.9, rr=3.0,
                           tail_alpha=2.2)["kelly"] is None)
    from backend import trader_risk as _tr
    _sr = _tr.SessionRisk(100000, day_loss_frac=0.06)
    _sr.set_env(atr_frac=0.008)
    ok("killswitch динамический: ATR×2 расширяет лимит до 12%, кэп 15%",
       abs(_sr.day_limit_frac() - 0.12) < 1e-9
       and _tr.HARD_DAY_CAP == 0.15)
    ok("структурный слом блокирует немедленно",
       _tr.SessionRisk(1000).lock_structural("тест")["locked"] is True)
    # «Сырая дата»: WS-поток, EM-Хоукс, модуляторы, Снайпер, Курамото-гейт
    from backend import stream as _str
    _s = _str.TickStream("T")
    _s.handle({"trade": {"direction": "TRADE_DIRECTION_BUY",
                         "price": {"units": 90, "nano": 0}, "quantity": "3",
                         "time": None}})               # None → текущее время
    _t0, _px, _qty, _side = _s.trades[0]
    ok("поток: сырой тик в кольцо с миллисекундами, ценой и стороной",
       len(_s.trades) == 1 and _px == 90.0 and _qty == 3.0 and _side == 1.0
       and _s.cvd(60.0) == 3.0)
    _casc = []
    for _k in range(80):
        _b = _k * 1200.0
        _casc += [_b, _b + 40.0, _b + 85.0, _b + 110.0]
    _em = _hw.branching_times(_casc)
    _reg = _hw.branching_times([i * 250.0 for i in range(240)])
    ok("EM-Хоукс по сырым таймстампам: каскад→потомки, решётка→докритично",
       _em and _reg and _em["n"] > _reg["n"] + 0.25 and _em["half_life_ms"] < 600)
    ok("фазовый модулятор: все фазы в J2000 совпадают → R=1, пороги делятся",
       abs(_bc.phase_modulator(_bc.EPOCH_MS / 1000.0, {})["sens_mult"]
           - (1.0 + _bc.SENS_GAIN)) < 1e-9)
    from backend import execution as _ex
    _bk = {"best_bid": 99.5, "best_ask": 100.5,
           "levels_ask": [{"p": 100.5, "q": 40}], "levels_bid": [{"p": 99.5, "q": 40}]}
    _psing = _ex.plan_entry("long", 30, 100.0, _bk, state="СИНГУЛЯРНОСТЬ")
    _ppar = _ex.plan_entry("long", 30, 100.0, _bk, state="ПАРЛАМЕНТ")
    ok("Снайпер: сингулярность бьёт рыночным, парламент — тенью у спреда; "
       "сумма траншей = плану",
       _psing["tranches"][0]["kind"] == "market"
       and _ppar["tranches"][0]["kind"] == "shadow"
       and sum(t["lots"] for t in _psing["tranches"]) == 30)
    from backend import kuramoto as _ku
    ok("Курамото-гейт: рассинхрон блокирует парламент, сингулярность — нет",
       _ku.gate("long", "ПАРЛАМЕНТ", {"r": 0.2})["blocked"] is True
       and _ku.gate("long", "СИНГУЛЯРНОСТЬ", {"r": 0.2})["blocked"] is False)
    # ПРОЕКТ PREDATOR: Пылесос (запись), Патологоанатом (пробои), Снайпер-солитон
    from backend import recorder as _rec, patterns as _pat, execution as _ex2
    from backend.lab import market_sim as _sim
    _s2 = _rec.TickRecorder(["T"], db_path=":memory:"); _s2._open_db()
    _tsr = _str.TickStream("T")
    _tsr.on_trade = lambda t, p, q, s, d: _s2._sink_trade("T", t, p, q, s, d)
    _tsr.handle({"trade": {"direction": "TRADE_DIRECTION_BUY",
                           "price": {"units": 90, "nano": 0}, "quantity": "3",
                           "time": None}})
    ok("Пылесос: кадр Tinkoff → SQLite (сырьё для Патологоанатома)",
       _s2.flush() == 1 and _s2._db.execute(
           "SELECT qty FROM trades").fetchone()[0] == 3.0)
    ok("симулятор рынка: кадры Tinkoff-формата, детерминизм по сиду",
       len(_sim.frames(7, 1)) > 200 and _sim.frames(7, 1)[:10] == _sim.frames(7, 1)[:10])
    ok("солитон-выход: у стены фиксируем ДО бетона",
       _ex2.soliton_exit("long", 100.0, 100.9, 101.0,
                         [0.001, 0.003, 0.006, 0.008, 0.009])["exit"] is True)


async def main():
    for fn in (t_aether, t_reactor_maya, t_oracle_core, t_tinkoff, t_moex,
               t_underlying, t_news_weather, t_ai, t_local):
        try:
            await fn()
        except Exception:
            FAIL.append(fn.__name__ + " (упал целиком)")
            traceback.print_exc()
    for mod in (tinkoff, moex, instruments, news, weather, underlying):
        await mod.aclose()
    print("\n" + "═" * 60)
    print(f"ИТОГ: ✓ {len(PASS)} прошло | ✗ {len(FAIL)} упало")
    if FAIL:
        print("УПАВШИЕ:"); [print("  ✗", f) for f in FAIL]
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
