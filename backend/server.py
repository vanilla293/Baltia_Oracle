"""ОРАКУЛ // ПИФИЯ — сервер.

FastAPI + WebSocket. Отдаёт фронт (терминал спецслужбы), real-time котировки,
свечи (Tinkoff → MOEX фолбэк), стакан, ленту, сохранённые разборы, чат с памятью
и стрим полного аналитического конвейера по WebSocket.

Запуск:  uvicorn backend.server:app --host 0.0.0.0 --port 8799
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import (aether, ai, api_v5, astro, bus, config, decision as _decision,
               directive as _directive, instrument_select as _iselect,
               instruments, maya, maya_scan, memory, microstructure, moex,
               pipeline, prompts, reactor_sky, tinkoff, wyckoff)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pythia.server")


class _QuietTicks(logging.Filter):
    """Живой тикер цены опрашивает /api/price каждые 3с — глушим этот шум в
    access-логе, иначе кажется, что конвейер «фармит биржу» на вердикте."""
    _NOISY = ("/api/price", "/api/astro", "/api/ether", "/api/health",
              "/api/v5/state", "/api/v5/run", "/api/v5/mission/status")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(p in msg for p in self._NOISY)


logging.getLogger("uvicorn.access").addFilter(_QuietTicks())

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _background(coro, name: str) -> asyncio.Task:
    """Держим фоновые циклы до завершения и останавливаем их перед HTTP-пулами."""
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)

    def _done(t: asyncio.Task) -> None:
        _BACKGROUND_TASKS.discard(t)
        if not t.cancelled() and t.exception():
            logger.warning("фон %s: %s", name, str(t.exception())[:120])

    task.add_done_callback(_done)
    return task


async def _stop_background() -> None:
    while _BACKGROUND_TASKS:
        tasks = list(_BACKGROUND_TASKS)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        _BACKGROUND_TASKS.difference_update(tasks)


async def _stop_owned_work() -> None:
    """Join user-launched work before closing the clients it still uses."""
    from . import api_chat, api_council, explain, ledger, mission

    await mission.shutdown_tasks()
    tasks = set(api_council._TASKS)
    tasks.update(task for task in (api_v5._astro_task, api_v5._mkt_task) if task)
    chat_task = api_chat._STATE.get("task")
    if chat_task:
        tasks.add(chat_task)
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    # A task cancelled before its first instruction has no finally block.
    rid = api_chat._STATE.get("run_id")
    if rid and (bus.run(rid) or {}).get("ended") is None:
        bus.end_run(rid, "cancelled")

    # Direct emergency closes own their broker requests. Let their bounded
    # reconciliation finish and persist its outcome before tearing down HTTP.
    panic_tasks = [m.panic_task for m in list(mission._M.values())
                   if m.panic_task and not m.panic_task.done()]
    if panic_tasks:
        await asyncio.gather(*panic_tasks, return_exceptions=True)

    tasks = {m.task for m in list(mission._M.values()) if m.task}
    tasks.update(st["task"] for st in list(maya_scan._SCANS.values()) if st.get("task"))
    tasks.update(st["task"] for registry in (_AUTO, _AIPILOT)
                 for st in list(registry.values()) if st.get("task"))
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    # Legacy pilots own a raw market-data stream outside their main loop.
    streams = [getattr(st.get("ap"), "stream", None) for st in list(_AUTO.values())]
    await asyncio.gather(*(stream.stop() for stream in streams if stream), return_exceptions=True)
    await explain.shutdown_tasks()
    await ledger.shutdown_tasks()

app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
    allow_headers=["*"], allow_credentials=False,
)
app.include_router(api_v5.router)
for _name in ("api_council", "api_mission", "api_chat"):   # этапы v5 и чат — каждый сам по себе
    try:
        _m = __import__(f"backend.{_name}", fromlist=["router"])
        app.include_router(_m.router)
    except Exception as _e:                            # noqa: BLE001
        logger.warning("роутер %s не подключён: %s", _name, str(_e)[:120])


@app.middleware("http")
async def _no_cache(request, call_next):
    """Индекс и все /api/* — всегда свежие: браузерный кэш старого фронта
    и устаревшего /api/config ломал цикл ввода ключей (модалка по кругу)."""
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.startswith("/api"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    elif p.startswith("/static") or p == "/favicon.ico":
        # статика ревалидируется (ETag → дешёвый 304): после обновления
        # версии браузер не имеет права показывать старый скрипт из кэша
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.on_event("startup")
async def _startup():
    if getattr(config, "RESET_ON_START", True):
        try:
            await memory.hard_reset()
            logger.info("Чистый старт: архив разборов и чат обнулены")
        except Exception as e:
            logger.warning("reset_on_start не выполнен: %s", str(e)[:80])
    if getattr(config, "WIPE_KEYS_ON_START", True):
        try:
            config.wipe_keys()
            ai.reset_client()
            logger.info("Чистый старт: ключи DeepSeek и токен Tinkoff обнулены — "
                        "введи их заново в интерфейсе (так задумано)")
        except Exception as e:
            logger.warning("wipe_keys_on_start не выполнен: %s", str(e)[:80])
    if os.getenv("PYTHIA_MOCK_AI") == "1":          # демонстрация без сети и ключей
        from . import mock_ai
        mock_ai.install()
    logger.info("%s v%s — порт %s | tinkoff=%s | ключей DeepSeek: %d",
                config.APP_NAME, config.APP_VERSION, config.PORT,
                "ON" if tinkoff.enabled() else "OFF", len(config.INSTRUMENT_KEYS))
    ips = _lan_ips()
    logger.info("Локально:           http://localhost:%s", config.PORT)
    for ip in ips:
        logger.info("В этой же сети WiFi: http://%s:%s", ip, config.PORT)
    if ips:
        logger.info("↑ открой этот адрес на другом устройстве в той же сети — работает на всех сразу")
    _bg = _background
    _bg(instruments.fetch_universe(), "каталог")   # прогреть каталог всей биржи в фоне
    _bg(astro.acontext(), "астро")                 # прогреть астро-карту (первый расчёт тяжёлый)
    if tinkoff.enabled():
        _bg(tinkoff.prewarm(), "индексы Tinkoff")  # прогреть каталог Tinkoff (первый разбор акции)
    if os.getenv("PYTHIA_LEGACY_PILOT") == "1":
        # сторож ИИ-пилота 4.x: поднимал пилот по data/aipilot_state.json мимо миссии v5 —
        # два пилота на одном счёте дрались за позицию; в v5 позицию ведёт сторож миссии
        _bg(_aipilot_watchdog(), "сторож ИИ-пилота 4.x")
    # ── v5 «СОВЕТ»: шина событий, дозор новостей, сторож миссии ──
    bus.set_sink(HUB.broadcast)
    try:                                              # v5.3 фаза 4 (W2): Telegram-бот — слушает шину, опрашивает команды
        from . import telegram as _telegram
        bus.set_sink(_telegram.wrap_sink(HUB.broadcast))
        _bg(_telegram.loop(), "Telegram-бот")         # без токена спит и ждёт, когда впишут в «Ключи»
    except Exception as e:                            # noqa: BLE001
        logger.warning("Telegram-бот не запущен: %s", str(e)[:120])
    try:
        from . import watch as _watch
        _bg(_watch.loop(), "дозор новостей")
    except Exception as e:                            # noqa: BLE001
        logger.warning("дозор не запущен: %s", str(e)[:120])
    try:
        from . import mission as _mission
        _bg(_mission.watchdog(), "сторож миссии")
    except Exception as e:                            # noqa: BLE001
        logger.warning("сторож миссии не запущен: %s", str(e)[:120])
    try:                                              # v5.3 фаза 4 (W1): журнал по операциям брокера — фоновая сверка
        from . import ledger as _ledger
        if _ledger.mode() != "est":
            _bg(_ledger.loop(), "журнал по операциям")
        else:
            logger.info("журнал по операциям: операций брокера нет (нет токена или PYTHIA_DRY) — сделки оценочные")
    except Exception as e:                            # noqa: BLE001
        logger.warning("журнал по операциям не запущен: %s", str(e)[:120])


@app.on_event("shutdown")
async def _shutdown():
    await _stop_background()
    await _stop_owned_work()
    # закрываем ВСЕ общие пулы HTTP-соединений, чтобы не оставлять висящих
    # keep-alive сокетов при перезапуске
    from . import news, weather, underlying
    try:
        from . import telegram as _telegram
        await _telegram.flush_batches()               # накопленные узлы — в очередь (доставит, если успеет)
        await _telegram.aclose()
    except Exception:
        pass
    for mod in (ai, tinkoff, moex, instruments, news, weather, underlying):
        try:
            await mod.aclose()
        except Exception:
            pass
    # Выключение = зачистка: пока программа работает — кэш и ключи живут,
    # после выхода на диске не остаётся ни ключей, ни накопленной базы.
    # (Стирание на старте страхует жёсткое убийство процесса без этого хука.)
    if getattr(config, "WIPE_ON_EXIT", True):
        try:
            config.wipe_keys()
            await memory.hard_reset()
            logger.info("Выключение: ключи и база стёрты — чистый лист к следующему запуску")
        except Exception as e:
            logger.warning("wipe_on_exit не выполнен: %s", str(e)[:80])


def _lan_ips() -> list[str]:
    import socket
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0]); s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if ":" not in ip and not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    return sorted(ips)


# ──────────────────────────────────────────────────────────────────────
# Котировка (Tinkoff → MOEX), общий помощник
# ──────────────────────────────────────────────────────────────────────

async def _quote(code: str) -> dict:
    it = instruments.get(code)
    if not it:
        return {"error": f"неизвестный инструмент: {code}"}
    ticker, ac, name = it["code"], it["asset_class"], it["name"]
    out = {"code": ticker, "name": name, "asset_class": ac, "source": None}

    if tinkoff.enabled():
        try:
            inst = await tinkoff.resolve(ticker, ac)
        except Exception as e:
            inst = None
            out["resolve_error"] = str(e)[:80]
        if inst:
            figi = inst.get("figi") or inst.get("uid")
            last_t = asyncio.create_task(tinkoff.last_price(figi))
            prev_t = asyncio.create_task(tinkoff.close_price(figi))
            ob_t = asyncio.create_task(tinkoff.orderbook(figi, depth=10))
            day_t = asyncio.create_task(tinkoff.candles(figi, "1d", 2))
            last, prev, ob, day = await asyncio.gather(
                last_t, prev_t, ob_t, day_t, return_exceptions=True)
            last = last if isinstance(last, dict) else None
            prev = prev if isinstance(prev, (int, float)) else None
            ob = ob if isinstance(ob, dict) else None
            day = day if isinstance(day, list) else []
            price = (last or {}).get("price")
            if price:
                out["source"] = "tinkoff"
                out["price"] = price
                out["resolved_ticker"] = inst.get("ticker")
                out["currency"] = inst.get("currency")
                out["expiration"] = inst.get("expirationDate")
                if prev and prev > 0:
                    out["prev_close"] = prev
                    out["change_pct"] = round((price - prev) / prev * 100, 2)
                    out["change_abs"] = round(price - prev, 6)
                if day:
                    out["volume_today"] = day[-1].get("v")
                    out["session_open"] = day[-1].get("o")
                    out["session_high"] = day[-1].get("h")
                    out["session_low"] = day[-1].get("l")
                if ob:
                    out["bid"] = ob.get("best_bid")
                    out["ask"] = ob.get("best_ask")
                    out["spread_bps"] = ob.get("spread_bps")
                    out["imbalance"] = ob.get("imbalance")
                return out

    # фолбэк MOEX
    try:
        mp = await moex.last_price(ticker, ac)
    except Exception as e:
        mp = None
        out["moex_error"] = str(e)[:80]
    if mp:
        out["source"] = "moex"
        out["price"] = mp.get("price")
        out["change_pct"] = mp.get("change_pct")
        out["volume_today"] = mp.get("volume_today")
        out["value_today"] = mp.get("value_today")
        out["bid"] = mp.get("bid")
        out["ask"] = mp.get("offer")
        out["quote_time"] = mp.get("quote_time")
        return out

    out["error"] = "котировка недоступна (нет Tinkoff-токена и MOEX не отдал)"
    return out


async def _candles(code: str, interval: str, days: int) -> dict:
    it = instruments.get(code)
    if not it:
        return {"error": f"неизвестный инструмент: {code}", "candles": []}
    ticker, ac = it["code"], it["asset_class"]
    rows: list[dict] = []
    src = None
    if tinkoff.enabled():
        try:
            inst = await tinkoff.resolve(ticker, ac)
            if inst:
                figi = inst.get("figi") or inst.get("uid")
                rows = await tinkoff.candles(figi, interval, days)
                if rows:
                    src = "tinkoff"
        except Exception as e:
            logger.warning("tinkoff candles %s: %s", code, str(e)[:80])
    if not rows:
        try:
            rows = await moex.candles(ticker, ac, interval, days)
            if rows:
                src = "moex"
        except Exception as e:
            logger.warning("moex candles %s: %s", code, str(e)[:80])
    return {"code": ticker, "interval": interval, "source": src,
            "candles": rows}


# ──────────────────────────────────────────────────────────────────────
# REST
# ──────────────────────────────────────────────────────────────────────

@app.post("/api/instrument_key")
async def api_instrument_key(payload: dict):
    ticker = (payload or {}).get("ticker") or (payload or {}).get("code")
    key = (payload or {}).get("key") or (payload or {}).get("deepseek_api_key")
    if not ticker:
        return JSONResponse({"error": "не указан инструмент"}, status_code=400)
    key = (key or "").strip()
    if len(key) > 512:
        return JSONResponse({"error": "ключ подозрительно длинный (>512) — вставлен не тот текст"},
                            status_code=400)
    if key and (not key.isascii() or any(ord(ch) < 33 for ch in key)):
        return JSONResponse({"error": "недопустимые символы в ключе — "
                             "вставь ключ DeepSeek заново (латиница, обычно sk-…)"},
                            status_code=400)
    config.set_instrument_key(ticker, key or None)
    ai.reset_client()
    return {"ok": True, "ticker": ticker.upper(),
            "has_key": config.has_key_for(ticker)}


@app.get("/api/instrument_key")
async def api_instrument_key_get(ticker: str = ""):
    return {"ticker": ticker.upper(),
            "has_key": config.has_key_for(ticker)}


@app.post("/api/keys")
async def api_instrument_keys_bulk(payload: dict):
    """Пакетный ввод ключей: {"keys": {"BRQ6": "sk-…", "NGN6": "sk-…"}}.
    Пустые значения пропускаются. Возвращает, у кого ключ теперь есть."""
    keys = (payload or {}).get("keys") or {}
    if not isinstance(keys, dict):
        return JSONResponse({"error": "keys: ожидается объект тикер→ключ"}, status_code=400)
    if len(keys) > 100:
        return JSONResponse({"error": "слишком много ключей за раз (максимум 100)"}, status_code=400)
    saved, rejected = [], []
    for tk, k in keys.items():
        # табы/переносы/пробелы из копипасты вычищаем сами — это не повод отказать
        k = "".join(ch for ch in (str(k) if k is not None else "") if ord(ch) > 32)
        if not tk or not k:
            continue
        if len(k) > 512 or not k.isascii():
            rejected.append(tk.upper())   # кириллица/юникод — это не ключ
            continue
        config.set_instrument_key(tk, k)
        saved.append(tk.upper())
    if saved:
        ai.reset_client()
    out = {"ok": True, "saved": saved,
           "has_key": {t.upper(): config.has_key_for(t) for t in keys}}
    if rejected:
        out["rejected"] = rejected
        out["error"] = ("недопустимые символы в ключе: " + ", ".join(rejected)
                        + " — вставь ключ DeepSeek заново (латиница, обычно sk-…)")
    return out


@app.post("/api/deepseek_keys")
async def api_deepseek_keys(payload: dict):
    """Пул аккаунтов DeepSeek — «работа на несколько аккаунтов разом»: нагрузка
    раскидывается по кругу, при лимите/нуле баланса на одном идёт авто-переход на
    другой. Принимает {"keys": ["sk-…", "sk-…"]} или строку (ключи через
    пробел/запятую/перенос). Персональные ключи инструментов важнее пула."""
    keys = (payload or {}).get("keys")
    if isinstance(keys, str):
        keys = [k for k in keys.replace(",", " ").replace(";", " ").split() if k]
    if keys is None or not isinstance(keys, (list, tuple)):
        return JSONResponse({"error": "keys: список ключей или строка"}, status_code=400)
    if len(keys) > 20:
        return JSONResponse({"error": "слишком много аккаунтов (максимум 20)"}, status_code=400)
    config.set_deepseek_keys(list(keys))
    ai.reset_client()
    return {"ok": True, "accounts": len(config.deepseek_keys())}


@app.get("/api/deepseek_keys")
async def api_deepseek_keys_get():
    return {"accounts": len(config.deepseek_keys())}


@app.get("/api/instruments")
async def api_instruments():
    # раньше первый вход ждал полной выгрузки вселенной MOEX (до ~50с при
    # лежащем ISS) и терминал «висел на загрузке». Теперь ждём максимум 6с,
    # дальше отдаём курируемый каталог; вселенная дольётся следующим заходом.
    try:
        await asyncio.wait_for(instruments.fetch_universe(), timeout=6.0)
    except Exception:
        pass
    return {"groups": instruments.all_grouped()}


@app.get("/api/instruments/search")
async def api_instruments_search(q: str = "", limit: int = 40):
    """ПОИСК ПО ВСЕЙ БИРЖЕ — включая КАТАЛОГ ТИНЬКОФФ (жалоба владельца:
    «почему не подтянул по классике, чтобы я мог выбрать другие с того же
    Тинькофф, BMU6 к примеру»).

    Слева в панели рисуется каталог MOEX ISS, и если ISS не ответил за 6 с
    (или его режет сеть), владелец видел только курируемый список и не мог
    добраться до остальных контрактов. Здесь ищем в трёх местах: курируемый
    каталог → вселенная MOEX → индексы Тинькофф (акции/фьючерсы/валюты, тот
    самый брокер, чей ключ уже введён). Просроченные фьючерсы отсеиваем.
    Ничего не нашли — фронт всё равно даст разобрать код напрямую: конвейер
    умеет работать по произвольному тикеру."""
    from datetime import datetime, timezone
    qq = (q or "").strip().upper()
    if len(qq) < 1:
        return {"q": q, "items": []}
    limit = max(1, min(200, int(limit or 40)))
    out: list[dict] = []
    seen: set[str] = set()

    def add(code: str, name: str, ac: str, src: str) -> None:
        c = (code or "").upper().strip()
        if not c or c in seen:
            return
        seen.add(c)
        out.append({"code": c, "name": name or c, "asset_class": ac,
                    "source": src})

    # 1) то, что уже знает панель (курируемый каталог + вселенная MOEX)
    try:
        for grp, items in instruments.all_grouped().items():
            for it in items:
                if qq in it["code"].upper() or qq in (it.get("name") or "").upper():
                    add(it["code"], it.get("name"), it.get("asset_class"), "moex")
    except Exception as e:                                   # noqa: BLE001
        logger.warning("поиск по каталогу MOEX: %s", str(e)[:90])

    # 2) каталог БРОКЕРА (Тинькофф) — то, о чём просил владелец
    if tinkoff.enabled():
        now_iso = datetime.now(timezone.utc).isoformat()
        for kind, ac in (("futures", "futures"), ("shares", "share"),
                         ("currencies", "currency")):
            try:
                idx = await tinkoff._load_index(kind)
            except Exception as e:                           # noqa: BLE001
                logger.warning("индекс Тинькофф %s: %s", kind, str(e)[:80])
                continue
            for tk, row in (idx or {}).items():
                nm = row.get("name") or tk
                if qq not in tk.upper() and qq not in nm.upper():
                    continue
                if kind == "futures":                # истёкшие контракты — мимо
                    exp = row.get("expirationDate") or ""
                    if exp and exp < now_iso:
                        continue
                add(tk, nm, ac, "tinkoff")
    # точное совпадение — первым, дальше по длине кода (короткие важнее)
    out.sort(key=lambda x: (x["code"] != qq, not x["code"].startswith(qq),
                            len(x["code"]), x["code"]))
    return {"q": q, "items": out[:limit],
            "tinkoff": tinkoff.enabled(), "total": len(out)}


@app.get("/api/modes")
async def api_modes():
    out = [{"id": k, "name": v["name"], "icon": v["icon"],
            "tagline": v["tagline"]} for k, v in prompts.MODES.items()]
    return {"modes": out, "default": prompts.DEFAULT_MODE}


@app.get("/api/serverinfo")
async def api_serverinfo():
    return {"port": config.PORT, "lan_ips": _lan_ips(),
            "urls": [f"http://{ip}:{config.PORT}" for ip in _lan_ips()]}


@app.get("/api/astro")
async def api_astro():
    try:
        ctx = await astro.acontext()
        return {"astro": ctx, "line": astro.short_line(ctx),
                "text": astro.render_for_ai(ctx)}
    except Exception as e:
        return {"astro": None, "error": str(e)[:120]}


# корзина ρ рынка: фазовая сетка смежных активов (теневой вход в один актив
# деформирует фазы остальных — считаем когерентность часовых закрытий)
_RHO_BASKET = (("BR", "futures"), ("GOLD", "futures"), ("Si", "futures"),
               ("SBER", "share"))
_rho_cache: dict = {"ts": 0.0, "series": {}}


async def _rho_series() -> dict:
    import time as _t
    if _t.time() - _rho_cache["ts"] < 300.0 and _rho_cache["series"]:
        return _rho_cache["series"]
    out: dict = {}
    for root, ac in _RHO_BASKET:
        try:
            rows = await moex.candles(root, ac, "1h", 7)
            closes = [r.get("c") for r in rows if r.get("c")]
            if len(closes) >= 48:
                out[root] = closes
        except Exception:
            continue
    _rho_cache["ts"] = _t.time()
    _rho_cache["series"] = out
    return out


@app.get("/api/ether")
async def api_ether(code: str, interval: str = "1h"):
    """Эфирный слой REAL SKY: волна генезиса × реальные свечи. «Дневной» слот —
    свечи АКТИВНОГО таймфрейма (5м/15м/1ч...): энергия, плиты и окна живут в
    том масштабе, который открыт на графике. Ошибки полем error, не 500."""
    code = (code or "").upper()[:32]
    if interval not in _INTERVALS_OK:
        interval = "1h"
    try:
        _, _, dd = aether.tf_params(interval)
        it = instruments.get(code) or {}
        day, week, month, idate = await asyncio.gather(
            _candles(code, interval, dd), _candles(code, "1h", 7),
            _candles(code, "1d", 60),
            moex.issue_date(it.get("code") or code))
        candles = {"day": day.get("candles") or [],
                   "week": week.get("candles") or [],
                   "month": month.get("candles") or []}
        ctx = await aether.acontext(code, candles, interval=interval,
                                    genesis_date=idate)
        payload = aether.chart_payload(ctx)
        text = aether.render_for_ai(ctx)
        try:
            basket = dict(await _rho_series())
            wk = [r.get("c") for r in (candles.get("week") or [])
                  if isinstance(r, dict) and r.get("c")]
            if len(wk) >= 48:
                # актив замещает свой корень в корзине (BRU6 ~ BR): дубль
                # одной и той же серии искусственно поднимал бы r (веер v3.0)
                root = "".join(ch for ch in code if ch.isalpha()).upper()
                for bk in list(basket):
                    if root.startswith(bk.upper()) or bk.upper().startswith(root[:2]):
                        basket.pop(bk, None)
                basket[code] = wk
            mr = aether.market_coherence(basket)
            if mr:
                payload["market_r"] = mr
                text += (f"\nρ РЫНКА: {mr['r']} — {mr['word']} "
                         f"(корзина: {', '.join(mr['assets'])}; {mr['note']})")
        except Exception:
            pass
        return {"code": code, "ether": payload,
                "line": aether.short_line(ctx),
                "text": text}
    except Exception as e:
        return {"ether": None, "error": str(e)[:120]}


@app.get("/api/health")
async def api_health():
    return {
        "ok": True,
        "app": config.APP_NAME,
        "version": config.APP_VERSION,
        "deepseek": bool(config.INSTRUMENT_KEYS),
        "deepseek_keys": len(config.INSTRUMENT_KEYS),
        "tinkoff": tinkoff.enabled(),
        "model": config.DEEPSEEK_MODEL,
        "model_fast": config.DEEPSEEK_MODEL_FAST,
        "news_days": config.NEWS_DAYS,
    }


@app.get("/api/health/deepseek")
async def api_health_deepseek():
    return await ai.health()


@app.get("/api/config")
async def api_config_get():
    return config.public_snapshot()


@app.post("/api/config")
async def api_config_set(payload: dict):
    # DEEPSEEK_BASE_URL намеренно НЕ меняется по сети: сервер работает в LAN,
    # и подмена base_url увела бы все ключи DeepSeek на чужой хост при
    # следующем разборе. Менять только env/config_user.json на самой машине.
    allow = {
        "DEEPSEEK_MODEL",
        "DEEPSEEK_MODEL_FAST", "TINKOFF_TOKEN", "NEWS_DAYS",
        "AI_MAX_TOKENS", "AI_REASONING_EFFORT", "AI_REASONING_EFFORT_MAX",
        "EVENTS_IDLE_SEC",
    }
    alias = {
        "tinkoff_token": "TINKOFF_TOKEN",
        "model": "DEEPSEEK_MODEL",
        "model_fast": "DEEPSEEK_MODEL_FAST",
        "news_days": "NEWS_DAYS",
    }
    updates = {}
    for k, v in (payload or {}).items():
        key = alias.get(k, k)
        if key in allow:
            updates[key] = v
    if not updates:
        return JSONResponse({"error": "нет допустимых полей"}, status_code=400)
    config.set_many(updates)
    ai.reset_client()
    return config.public_snapshot()


@app.get("/api/price")
async def api_price(code: str):
    return await _quote(code)


_INTERVALS_OK = {"1m", "5m", "10m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"}


@app.get("/api/candles")
async def api_candles(code: str, interval: str = "15m", days: int = 7):
    if interval not in _INTERVALS_OK:
        interval = "15m"
    days = max(1, min(days, 365))
    return await _candles(code, interval, days)


@app.get("/api/orderbook")
async def api_orderbook(code: str):
    it = instruments.get(code)
    if not it or not tinkoff.enabled():
        return {"orderbook": None,
                "note": "стакан только при заданном Tinkoff-токене"}
    try:
        inst = await tinkoff.resolve(it["code"], it["asset_class"])
        figi = inst.get("figi") or inst.get("uid")
        ob = await tinkoff.orderbook(figi, depth=50)
        return {"code": it["code"], "orderbook": ob}
    except Exception as e:
        return {"orderbook": None, "error": str(e)[:120]}


@app.get("/api/maya")
async def api_maya(code: str):
    """Мгновенный разбор стакана: вакуум/сингулярности/тяга. Каждый обрыв
    цепочки — ЧЕСТНАЯ причина словами (NO DUMMIES), не тихий провал."""
    it = instruments.get(code)
    if not it:
        return {"maya": None, "note": f"объект {code!r} не найден в реестре"}
    if not tinkoff.enabled():
        return {"maya": None,
                "note": "нет Tinkoff-токена — стакан недоступен (введи ключ TK)"}
    try:
        inst = await tinkoff.resolve(it["code"], it["asset_class"])
        if not inst:
            return {"maya": None,
                    "note": ("инструмент не разрешился в Tinkoff (сеть/выходной/"
                             "нет такого контракта) — стакан недоступен")}
        figi = inst.get("figi") or inst.get("uid")
        ob = await tinkoff.orderbook(figi, depth=50)
        if not ob:
            return {"code": it["code"], "maya": None,
                    "note": ("биржа не отдала стакан (вечер/выходной/аукцион) — "
                             "вакуум честно не считается")}
        tape = await tinkoff.last_trades(figi, minutes=15)
        return {"code": it["code"], "maya": maya.analyze(ob, tape)}
    except Exception as e:
        return {"maya": None, "error": str(e)[:160],
                "note": "ошибка запроса к Tinkoff — подробность в error"}


# ══════════════════════════════════════════════════════════════════════════
# АВТОПИЛОТ: разбор закончился → торговая петля стартует САМА (без кнопок)
# ══════════════════════════════════════════════════════════════════════════
_AUTO: dict[str, dict] = {}          # code -> {"ap": Autopilot, "task": Task}


def _auto_enabled() -> bool:
    """Автостарт после разбора включён? Выключить: PYTHIA_AUTOSTART=0."""
    return os.getenv("PYTHIA_AUTOSTART", "1") != "0"


async def _auto_start(code: str, reason: str = "ручной запуск",
                      accumulate: bool = False) -> dict:
    """Поднять петлю по объекту. Идемпотентно: вторая петля не плодится.
    accumulate=True — петля копит данные, но реальные входы пока закрыты
    (пока ИИ думает); снимается _auto_arm(code) по готовности разбора."""
    code = (code or "").upper().strip()
    if not code:
        return {"ok": False, "note": "пустой код"}
    # ЗАПРЕТ ДВОЕВЛАСТИЯ — ПЕРВЫМ делом, до идемпотентной ветки (веер, major:
    # раньше уже накапливавшая петля проскакивала сюда по ветке `already` и
    # кнопка «ЗАПУСТИТЬ» из ВАКУУМа выводила её в БОЙ рядом с ИИ-пилотом —
    # две торговые машины на одном figi и одном депозите).
    aip = _AIPILOT.get(code)
    if _ai_pilot_enabled() or (aip and not aip["task"].done()):
        cur0 = _AUTO.get(code)
        if cur0 and not cur0["task"].done():
            cur0["ap"].stop()                        # копившую петлю гасим
        return {"ok": False, "note": "включён ИИ-ПИЛОТ — оракульская петля "
                                     "не поднимается (двоевластие запрещено)"}
    cur = _AUTO.get(code)
    if cur and not cur["task"].done():
        if not accumulate:
            cur["ap"].analysis_ready = True          # разбор готов → боевой
        return {"ok": True, "already": True, "status": cur["ap"].status()}
    if not tinkoff.enabled():
        return {"ok": False, "note": "нет токена Tinkoff — введи ключ в интерфейсе"}
    try:
        from . import autopilot as _ap_mod
        ap = _ap_mod.Autopilot(code)
        ap.analysis_ready = not accumulate           # копим, пока думает
        task = asyncio.create_task(ap.run())

        def _done(t: asyncio.Task) -> None:
            if not t.cancelled() and t.exception():
                logger.error("автопилот %s упал: %s", code, str(t.exception())[:200])

        task.add_done_callback(_done)
        _AUTO[code] = {"ap": ap, "task": task}
        logger.info("АВТОПИЛОТ %s запущен (%s), режим %s — торгует без кнопок",
                    code, reason, ap.mode["mode"])
        return {"ok": True, "started": True, "status": ap.status()}
    except Exception as e:                                   # noqa: BLE001
        logger.error("автопилот %s не стартовал: %s", code, str(e)[:200])
        return {"ok": False, "note": f"не стартовал: {str(e)[:150]}"}


@app.post("/api/auto/start")
async def api_auto_start(payload: dict):
    return await _auto_start((payload or {}).get("code", ""), "кнопка панели")


@app.post("/api/auto/stop")
async def api_auto_stop(payload: dict):
    code = ((payload or {}).get("code") or "").upper().strip()
    cur = _AUTO.get(code)
    if not cur:
        return {"ok": False, "note": "петля по этому объекту не запущена"}
    cur["ap"].stop()
    return {"ok": True, "note": f"{code}: петля останавливается"}


@app.post("/api/auto/panic")
async def api_auto_panic(payload: dict):
    """ПАНИКА: закрыть позиции и остановить петли (все или по объекту)."""
    code = ((payload or {}).get("code") or "").upper().strip()
    targets = [code] if code else list(set(_AUTO) | set(_AIPILOT))
    hit = []
    for c in targets:
        cur = _AUTO.get(c)
        if cur:
            cur["ap"].panic()
            hit.append(c)
        aip = _AIPILOT.get(c)          # ПАНИКА накрывает и ИИ-пилот
        if aip:
            aip["pilot"].panic()
            if c not in hit:
                hit.append(c)
    logger.warning("ПАНИКА владельца: %s", hit or "нечего закрывать")
    return {"ok": True, "panicked": hit,
            "note": "позиции закрываются на ближайшем тике"}


@app.get("/api/auto/status")
async def api_auto_status(code: str = ""):
    if code:
        cur = _AUTO.get(code.upper().strip())
        if not cur:
            return {"running": False, "autostart": _auto_enabled()}
        return {"running": not cur["task"].done(), "autostart": _auto_enabled(),
                "status": cur["ap"].status()}
    return {"autostart": _auto_enabled(),
            "loops": {c: {"running": v["task"].done() is False,
                          "status": v["ap"].status()} for c, v in _AUTO.items()}}


# ══════════════════════════════════════════════════════════════════════════
# ИИ-ПИЛОТ (PYTHIA_AI_PILOT=1): ИИ решает — код исполняет на максимум.
# Вместо оракульской петли: exec-приказ шифровщика → засада → вход на все →
# 30-мин перепроверки → при закрытии новый огромный анализ С НУЛЯ.
# ══════════════════════════════════════════════════════════════════════════
_AIPILOT: dict[str, dict] = {}       # code -> {"pilot": AIPilot, "task": Task}


def _ai_pilot_enabled() -> bool:
    """Режим ИИ-пилота включён? Рубильник — ТУМБЛЕР В ПАНЕЛИ (config_user.json,
    ключ PYTHIA_AI_PILOT), с откатом на переменную окружения PYTHIA_AI_PILOT=1.

    Раньше читалось ТОЛЬКО окружение — владелец запускал start.bat, режим
    молча оставался выключенным, и после разбора поднималась старая
    оракульская петля (её видно в панели «ВАКУУМ»). Теперь режим виден и
    переключается кнопкой, а окружение осталось как запасной путь."""
    return config.get_bool("PYTHIA_AI_PILOT", False)


async def _aipilot_on_analysis(code: str) -> dict:
    """Разбор готов → поднять ИИ-пилот (или скормить живому свежий приказ).
    Идемпотентно: вторая петля не плодится."""
    code = (code or "").upper().strip()
    if not code:
        return {"ok": False, "note": "пустой код"}
    # мёртвые пилоты выметаем: иначе они вечно висят в реестре и (а) блокируют
    # правило «один депозит = один пилот», (б) держат тумблер в панели
    # залипшим в «ВЫКЛ» (виджет считал режим по наличию записи) — веер, major
    for c_dead in [c for c, v in _AIPILOT.items() if v["task"].done()]:
        _AIPILOT.pop(c_dead, None)
    rec = await memory.get_analysis(code)
    forecast = (rec or {}).get("forecast")
    cur = _AIPILOT.get(code)
    if cur and not cur["task"].done():
        ok = cur["pilot"].adopt_forecast(forecast)
        st = cur["pilot"].status()
        return {"ok": ok, "already": True, "status": st,
                # без note владельцу показывали «не стартовал: None» (веер)
                "note": (f"{code}: пилот уже работает — "
                         + str(st.get("last_action") or "приказ принят"))}
    # ОДИН ДЕПОЗИТ = ОДИН ПИЛОТ (закалка веером): два пилота, каждый «на
    # максимум» от всего счёта — двойной счёт денег и мгновенный маржин-колл.
    for c2, v2 in _AIPILOT.items():
        if c2 != code and not v2["task"].done():
            note = (f"ИИ-пилот уже ведёт {c2} на максимум — второй пилот на "
                    f"тот же депозит не поднимаю (один депозит = один пилот). "
                    f"Останови {c2} через /api/aipilot/stop")
            logger.warning("ИИ-пилот %s: %s", code, note)
            return {"ok": False, "note": note}
    # конфликт со старой оракульской петлёй на том же объекте — гасим её
    old = _AUTO.get(code)
    if old and not old["task"].done():
        old["ap"].stop()
        logger.info("ИИ-пилот %s: остановил оракульскую петлю (двоевластие "
                    "на одном figi запрещено)", code)
    if not tinkoff.enabled():
        return {"ok": False, "note": "нет токена Tinkoff — введи ключ в интерфейсе"}
    try:
        from . import ai_pilot as _aip_mod
        pilot = _aip_mod.AIPilot(code)

        async def _reanalyze() -> None:
            # огромный анализ С ЧИСТОГО ЛИСТА: конвейер собирает всё заново,
            # прошлый разбор думающим стадиям НЕ подаётся («как в первый раз»)
            if not _ai_pilot_enabled():
                # тумблер выключили, пока пилот работал: позицию он доведёт,
                # но НОВЫХ кругов «разбор → вход на максимум» не будет —
                # иначе выключенный режим жёг деньги и токены вечно (веер)
                pilot.plan = None
                pilot.last_action = ("режим ИИ-пилота выключен тумблером — "
                                     "новых входов не будет; позицию довожу "
                                     "по тросу/тейку, дальше стоп")
                logger.warning("ИИ-пилот %s: режим выключен — переанализ "
                               "отменён, новых входов нет", code)
                if not pilot.position and not pilot.pending:
                    pilot.stop()
                return
            async def to_hub(ev: dict) -> None:
                ev.setdefault("launch_id", f"aip-{code}")
                await HUB.broadcast(ev)
            res = await pipeline.run_pipeline([code], to_hub)
            f = (((res or {}).get("results") or {}).get(code) or {}).get("forecast")
            pilot.adopt_forecast(f)

        pilot.reanalyze_cb = _reanalyze
        pilot.adopt_forecast(forecast)
        task = asyncio.create_task(pilot.run())

        def _done(t: asyncio.Task) -> None:
            if not t.cancelled() and t.exception():
                logger.error("ИИ-пилот %s упал: %s", code,
                             str(t.exception())[:200])

        task.add_done_callback(_done)
        _AIPILOT[code] = {"pilot": pilot, "task": task}
        logger.info("ИИ-ПИЛОТ %s запущен: ИИ решает, код исполняет на максимум",
                    code)
        return {"ok": True, "started": True, "status": pilot.status()}
    except Exception as e:                                   # noqa: BLE001
        logger.error("ИИ-пилот %s не стартовал: %s", code, str(e)[:200])
        return {"ok": False, "note": f"не стартовал: {str(e)[:150]}"}


AIP_FRESH_H = 2.0        # приказ старше — протух: на максимум по вчерашней
                         # цене не заходим (веер, major)


@app.post("/api/aipilot/enable")
async def api_aipilot_enable(payload: dict, request: Request):
    """ТУМБЛЕР РЕЖИМА из панели (без правки окружения и перезапуска).

    on=true  → следующий разбор поднимет ИИ-пилот (шифровщик получит боевой
               аддон и обязан выдать exec-приказ);
    on=false → будущие разборы снова ведёт оракульская петля. УЖЕ ЗАПУЩЕННЫЕ
               пилоты НЕ убиваются молча (у них может быть открытая позиция):
               гаси их явно — СТОП ПИЛОТА или ПАНИКА."""
    raw = (payload or {}).get("on")
    if isinstance(raw, str):                  # "0"/"false" — это ВЫКЛ, а не
        raw = raw.strip().lower()             # «непустая строка = истина»
        on = raw in ("1", "true", "yes", "on", "да")
        if raw not in ("1", "0", "true", "false", "yes", "no", "on", "off", "да", "нет"):
            return JSONResponse({"ok": False, "note": f"непонятное on={raw!r}"},
                                status_code=400)
    elif isinstance(raw, bool):
        on = raw
    elif isinstance(raw, (int, float)):
        on = bool(raw)
    else:                                     # поля нет вовсе — раньше это
        return JSONResponse(                  # МОЛЧА выключало режим (веер)
            {"ok": False, "note": "нужно поле on: true|false"}, status_code=400)
    code = ((payload or {}).get("code") or "").upper().strip()
    live = [c for c, v in _AIPILOT.items() if not v["task"].done()]

    # ── ВКЛЮЧЕНИЕ — только с самой машины (веер, critical) ────────────────
    # Сервер слушает 0.0.0.0 и приглашает открыть панель с телефона в той же
    # сети — удобно для НАБЛЮДЕНИЯ, но включать боевой автомат «на максимум с
    # плечом» должен тот, кто сидит за машиной. Направление «выключить» и
    # ПАНИКА остаются доступны отовсюду: тормозить можно всегда.
    host = (request.client.host if request and request.client else "") or ""
    local = host in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")
    if on and not local:
        note = (f"включение ИИ-пилота разрешено только с самой машины "
                f"(запрос с {host}). Открой панель на компьютере, где "
                f"запущена Пифия, — или задай PYTHIA_AI_PILOT=1 в окружении. "
                f"Выключение и ПАНИКА работают с любого устройства.")
        logger.warning("ОТКАЗ: попытка включить ИИ-пилот с %s", host)
        return JSONResponse({"ok": False, "enabled": _ai_pilot_enabled(),
                             "note": note}, status_code=403)

    config.set_many({"PYTHIA_AI_PILOT": "1" if on else "0"})
    if not on:
        note = "ИИ-пилот ВЫКЛЮЧЕН: следующий разбор поведёт обычная петля"
        if live:
            note += (f". ⚠ Уже работают пилоты: {', '.join(live)} — они "
                     f"доводят свои позиции и БОЛЬШЕ НЕ ОТКРЫВАЮТ новых; "
                     f"чтобы закрыть сейчас — СТОП ПИЛОТА или ПАНИКА")
        logger.warning("режим ИИ-пилота выключен из панели (живых пилотов: %d)",
                       len(live))
        return {"ok": True, "enabled": False, "note": note}

    # включили: если по объекту УЖЕ есть СВЕЖИЙ разбор с боевым приказом —
    # поднимаем пилот сразу, не заставляя гонять конвейер второй раз
    note = "ИИ-пилот ВКЛЮЧЁН: запусти полный анализ — дальше он всё сделает сам"
    started, ok = None, True
    if code:
        rec = await memory.get_analysis(code)
        fc = (rec or {}).get("forecast")
        fc = fc if isinstance(fc, dict) else {}
        age_h = ((time.time() - float((rec or {}).get("created_at") or 0)) / 3600.0
                 if rec else 1e9)
        if fc.get("exec") and age_h <= AIP_FRESH_H:
            r = await _aipilot_on_analysis(code)
            ok = bool(r.get("ok"))
            started = code if ok else None
            note = (f"ИИ-пилот ВКЛЮЧЁН и поднят по {code} — приказ из "
                    f"последнего разбора принят" if ok else
                    f"ИИ-пилот включён, но по {code} не стартовал: "
                    f"{r.get('note') or 'причина не указана'}")
        elif fc.get("exec"):
            note = (f"ИИ-пилот ВКЛЮЧЁН. Приказ по {code} протух "
                    f"({age_h:.1f} ч назад, предел {AIP_FRESH_H:g} ч) — по "
                    f"старой цене на максимум не заходим. Запусти полный анализ")
        elif fc.get("exec_error"):
            note = (f"ИИ-пилот ВКЛЮЧЁН. В разборе {code} приказ БЫЛ, но его "
                    f"отклонил санитайзер ({fc['exec_error']}). Запусти "
                    f"полный анализ заново")
        elif rec:
            note = (f"ИИ-пилот ВКЛЮЧЁН. Прошлый разбор {code} сделан в обычном "
                    f"режиме — боевого приказа в нём нет. Запусти полный "
                    f"анализ заново: шифровщик выдаст приказ, и пилот войдёт")
    logger.warning("режим ИИ-пилота ВКЛЮЧЁН из панели (%s)", note)
    return {"ok": ok, "enabled": True, "started": started, "note": note}


@app.get("/api/aipilot/status")
async def api_aipilot_status(code: str = ""):
    if code:
        cur = _AIPILOT.get(code.upper().strip())
        if not cur:
            return {"running": False, "enabled": _ai_pilot_enabled()}
        return {"running": not cur["task"].done(),
                "enabled": _ai_pilot_enabled(),
                "status": cur["pilot"].status()}
    return {"enabled": _ai_pilot_enabled(),
            "pilots": {c: {"running": v["task"].done() is False,
                           "status": v["pilot"].status()}
                       for c, v in _AIPILOT.items()}}


async def _aipilot_watchdog() -> None:
    """СМЕНА НА ВЕСЬ ДЕНЬ: пилот должен пережить и падение своей задачи, и
    перезапуск сервера. Раз в минуту: если режим включён, ключ Тинькофф на
    месте, а в data/aipilot_state.json лежит НАША позиция без живого пилота —
    поднимаем его заново (он сам подхватит позицию через _restore_state).

    Ключи на старте стираются (чистый старт — так задумано), поэтому сразу
    после перезапуска сторож ждёт, пока владелец введёт токен."""
    import json as _json
    while True:
        try:
            await asyncio.sleep(60)
            for c_dead in [c for c, v in _AIPILOT.items() if v["task"].done()]:
                _AIPILOT.pop(c_dead, None)
            if not (_ai_pilot_enabled() and tinkoff.enabled()):
                continue
            if any(not v["task"].done() for v in _AIPILOT.values()):
                continue                       # пилот уже работает
            p = config.DATA_DIR / "aipilot_state.json"
            if not p.exists():
                continue
            rec = _json.loads(p.read_text(encoding="utf-8"))
            code = (rec.get("base") or "").upper().strip()
            pos = rec.get("position") or {}
            if not code or not pos.get("lots"):
                continue
            logger.warning("СТОРОЖ: брошенная позиция %s %s лот без пилота — "
                           "поднимаю смену заново", code, pos.get("lots"))
            r = await _aipilot_on_analysis(code)
            await HUB.broadcast({"type": "auto", "ticker": code,
                                 "ok": bool(r.get("ok")),
                                 "detail": (f"{code}: сторож вернул ИИ-пилот к "
                                            f"брошенной позиции"
                                            if r.get("ok") else
                                            f"{code}: сторож не смог поднять "
                                            f"пилот — {r.get('note')}")})
        except asyncio.CancelledError:
            raise
        except Exception as e:                               # noqa: BLE001
            logger.warning("сторож ИИ-пилота споткнулся: %s", str(e)[:120])


@app.post("/api/aipilot/stop")
async def api_aipilot_stop(payload: dict):
    code = ((payload or {}).get("code") or "").upper().strip()
    cur = _AIPILOT.get(code)
    if not cur:
        return {"ok": False, "note": "ИИ-пилот по этому объекту не запущен"}
    cur["pilot"].stop()
    pos = cur["pilot"].position
    note = f"{code}: ИИ-пилот останавливается"
    if pos:
        note += (f". ⚠ ПОЗИЦИЯ {pos['side']} {pos['lots']} лот ОСТАЁТСЯ "
                 f"открытой под биржевым тросом — веди сам или дай ПАНИКУ")
    return {"ok": True, "note": note}


@app.get("/api/select_instrument")
async def api_select_instrument(deposit: float = 8000.0, bases: str = "Si,CR"):
    """Какой фьючерс брать под депозит: берёт ближние 2 контракта каждой базы
    (Si, CR), обогащает ГО/ценой/дневным размахом и прогоняет через селектор.
    Отвечает владельцу: Si или CR, сколько лотов влезает, что с макс плечом.
    Нужен токен; биржа закрыта → скорость по последним дневным свечам."""
    if not tinkoff.enabled():
        return {"result": None, "note": "нет Tinkoff-токена — живые фьючерсы недоступны"}
    from datetime import datetime, timezone
    prefixes = [b.strip().upper() for b in (bases or "").split(",") if b.strip()]
    try:
        idx = await tinkoff._load_index("futures")
    except Exception:                                       # noqa: BLE001
        return {"result": None, "note": "индекс фьючерсов не загрузился"}
    now = datetime.now(timezone.utc)
    by_base: dict[str, list] = {}
    for tk, row in idx.items():
        base = next((p for p in prefixes if tk.upper().startswith(p.upper())), None)
        if not base:
            continue
        exp = row.get("expirationDate") or ""
        try:
            ed = (datetime.fromisoformat(exp.replace("Z", "+00:00")) - now).days
        except Exception:                                   # noqa: BLE001
            continue
        if ed < 0:
            continue
        by_base.setdefault(base, []).append((ed, tk, row))

    cands = []
    for base, lst in by_base.items():
        lst.sort(key=lambda x: x[0])
        for ed, tk, row in lst[:2]:                         # ближние 2 экспирации
            figi = row.get("figi") or row.get("uid")
            if not figi:
                continue
            try:
                lp = await tinkoff.last_price(figi)
                mg = await tinkoff.futures_margin(figi)
                cnd = await tinkoff.candles(figi, "day", 3)
            except Exception:                               # noqa: BLE001
                lp, mg, cnd = None, None, []
            price = (lp or {}).get("price")
            go = (mg or {}).get("margin_buy")
            inc = (mg or {}).get("min_price_increment") or 0
            amt = (mg or {}).get("min_price_increment_amount") or 0
            pv = (amt / inc) if inc else 1.0
            drp, vol = 0.0, 0
            if cnd:
                last = cnd[-1]
                if last.get("c"):
                    drp = (last["h"] - last["l"]) / last["c"] * 100.0
                vol = int(last.get("v", 0))
            if go is None or not price:                     # без ГО/цены не судим (NO DUMMIES)
                continue
            cands.append({"code": tk, "base": base, "price": price,
                          "margin_per_lot_rub": go, "spread_bps": 2.0,
                          "day_range_pct": round(drp, 3), "volume": vol,
                          "expiry_days": ed, "point_value_rub": pv})

    if not cands:
        return {"result": None, "note": "живых контрактов Si/CR с ГО и ценой не "
                "найдено (биржа закрыта / нет данных) — попробуй в торговые часы"}

    res = _iselect.choose(deposit, cands)
    ch = res.get("chosen")
    if ch and ch.get("margin_per_lot_rub"):
        go = float(ch["margin_per_lot_rub"]); price = float(ch["price"])
        pv = next((c["point_value_rub"] for c in cands if c["code"] == ch["code"]), 1.0)
        lots_max = int(deposit // go) if go > 0 else 0
        free = deposit - lots_max * go
        liq_pts = (free / (lots_max * pv)) if lots_max * pv > 0 else None
        res["leverage"] = {
            "lots_max_leverage": lots_max,
            "liq_pct_at_max": (round(liq_pts / price * 100.0, 3) if liq_pts and price else None),
            "warn": (f"МАКС ПЛЕЧО = {lots_max} лот(ов); ликвидация уже при "
                     f"~{round((free/(lots_max*pv))/price*100,2) if lots_max*pv>0 and price else '?'}% "
                     "хода против. Один резкий тик — стоп-аут депозита. Твой выбор, не совет"),
        }
    return {"deposit": deposit, "result": res}


@app.get("/api/directive")
async def api_directive(code: str):
    """Вердикт Аналитика: LONG/SHORT/FLAT + зона входа/стоп из живого стакана,
    режим по Пригожину/Курамото. ЭТО АНАЛИЗ, НЕ автоторговля — курок за
    человеком. Недостающий источник просто не голосует (NO DUMMIES)."""
    it = instruments.get(code)
    if not it:
        return {"directive": None, "note": f"объект {code!r} не найден"}
    # 1) живой стакан → Майя (главный голос); молчит без токена
    maya_now = None
    if tinkoff.enabled():
        try:
            inst = await tinkoff.resolve(it["code"], it["asset_class"])
            if inst:
                figi = inst.get("figi") or inst.get("uid")
                ob = await tinkoff.orderbook(figi, depth=50)
                tape = await tinkoff.last_trades(figi, minutes=15)
                if ob:
                    maya_now = maya.analyze(ob, tape)
                    try:                                    # рентген: кто в стакане
                        maya_now["_xray"] = microstructure.xray_live(ob, tape)
                    except Exception:                       # noqa: BLE001
                        pass
        except Exception:                                   # noqa: BLE001
            maya_now = None
    # 2) макро-наклон неба (офлайн, всегда) + 3) наклон волны Ψ
    rc = wave_slope = None
    try:
        rc = await asyncio.to_thread(reactor_sky.reactor_context, code)
    except Exception:                                       # noqa: BLE001
        rc = None
    try:
        ctx = await asyncio.to_thread(aether.compute_context, code, None)
        psi = ((ctx or {}).get("wave") or {}).get("psi") or []
        if len(psi) >= 4:
            wave_slope = float(psi[-1]) - float(psi[-4])    # наклон хвоста волны
    except Exception:                                       # noqa: BLE001
        wave_slope = None
    d = _directive.directive(maya_now, rc, wave_slope)
    d["has_orderbook"] = bool(maya_now and maya_now.get("available"))
    if maya_now and maya_now.get("_xray", {}).get("available"):
        xr = maya_now["_xray"]
        d["xray"] = {"actor": xr.get("actor"), "posture": xr.get("posture"),
                     "signals": xr.get("signals"), "metrics": xr.get("metrics")}
        # свод выбирает стратегию под «кто в стакане» (полный размер — на исполнении с балансом)
        try:
            st = _decision.pick_strategy(xr.get("actor") or "", d.get("dir"),
                                         xr.get("metrics") or {})
            d["strategy"] = {"name": st["name"], "plan": st["plan"], "side": st["side"]}
        except Exception:                                   # noqa: BLE001
            pass
    if not d["has_orderbook"]:
        d["note"] = ("живого стакана нет (нет Tinkoff-токена или биржа закрыта) — "
                     "вердикт только по небу/волне, слабее")
    return {"code": it["code"], "directive": d}


@app.post("/api/maya/scan")
async def api_maya_scan_start(payload: dict):
    """Старт длительного онлайн-скана стакана (тик ~3с, до 8 часов)."""
    code = str(payload.get("code") or "")
    minutes = payload.get("minutes") or 15
    it = instruments.get(code)
    if not it:
        return {"ok": False, "note": f"объект {code} не найден"}
    return await maya_scan.start(code, it["code"], it["asset_class"], minutes)


@app.get("/api/maya/scan")
async def api_maya_scan_status(code: str):
    """Живой отчёт скана: тяга-консенсус, проколы, стены-призраки, натяжение."""
    return maya_scan.status(code)


@app.delete("/api/maya/scan")
async def api_maya_scan_stop(code: str):
    return maya_scan.stop(code)


@app.get("/api/tape")
async def api_tape(code: str, minutes: int = 60):
    it = instruments.get(code)
    if not it or not tinkoff.enabled():
        return {"tape": None, "note": "лента только при заданном Tinkoff-токене"}
    try:
        inst = await tinkoff.resolve(it["code"], it["asset_class"])
        figi = inst.get("figi") or inst.get("uid")
        tp = await tinkoff.last_trades(figi, minutes=minutes)
        return {"code": it["code"], "tape": tp}
    except Exception as e:
        return {"tape": None, "error": str(e)[:120]}


@app.get("/api/analyses")
async def api_analyses():
    return {"analyses": await memory.list_analyses()}


@app.get("/api/running")
async def api_running():
    """Живые разборы (тикер → стадия/сколько идёт). Источник истины для
    восстановления фронта после смерти сокета или перезагрузки страницы."""
    return {"running": pipeline.running_now()}


@app.get("/api/analysis")
async def api_analysis(code: str):
    rec = await memory.get_analysis(code)
    if not rec:
        return JSONResponse({"error": "разбор не найден"}, status_code=404)
    # отдаём всё, кроме тяжёлого досье целиком — рендерим хвост для UI
    dossier = rec.get("dossier") or {}
    rec_out = {
        "ticker": rec.get("ticker"), "name": rec.get("name"),
        "asset_class": rec.get("asset_class"),
        "created_at": rec.get("created_at"),
        "background": rec.get("background"),
        "analyst": rec.get("analyst"),
        "critic": rec.get("critic"),
        "verdict": rec.get("verdict"),
        "forecast": rec.get("forecast"),
        "news": rec.get("news"),
        "tags": rec.get("tags") or [],
        "wyckoff": dossier.get("wyckoff") or None,
        "aether": rec.get("aether") or None,
        "thinking": rec.get("thinking") or None,
        "underlying": dossier.get("underlying") or None,
        "consensus": dossier.get("consensus") or None,
        "dividend_next": dossier.get("dividend_next") or None,
        "price": (dossier.get("price") or {}).get("price"),
        "dossier_text": pipeline.render_dossier(dossier, full=False)
        if dossier else "",
    }
    return rec_out


@app.get("/api/chat/history")
async def api_chat_history(code: str):
    it = instruments.get(code)
    tk = it["code"] if it else code.upper()
    return {"history": await memory.get_chat(tk, limit=60)}


@app.post("/api/chat/clear")
async def api_chat_clear(payload: dict):
    code = (payload or {}).get("code", "")
    it = instruments.get(code)
    tk = it["code"] if it else code.upper()
    await memory.clear_chat(tk)
    return {"ok": True}


# ──────────────────────────────────────────────────────────────────────
# WebSocket: полный конвейер анализа
# ──────────────────────────────────────────────────────────────────────


# ── шина событий: мультипользовательский режим ────────────────────────
class _Hub:
    """Все открытые вкладки подписаны на /ws/events и получают события ВСЕХ
    разборов. У каждого зрителя — своя очередь и своя задача-отправитель:
    конвейер кладёт событие в очереди и НЕ ждёт сеть вообще. Медленный или
    зависший зритель не тормозит трансляцию ни на миллисекунду; безнадёжно
    отставший (очередь переполнена) отцепляется."""

    LAG_AFTER = 800     # столько кадров без подтверждения = зритель захлебнулся
    WINDOW = 400        # больше стольких неподтверждённых в трубу не льём (v5: фронт ack каждые 100)

    def __init__(self) -> None:
        self._clients: dict[WebSocket, dict] = {}

    def add(self, ws: WebSocket) -> None:
        q: asyncio.Queue = asyncio.Queue(maxsize=4000)
        gate = asyncio.Event()
        gate.set()
        c = {"q": q, "unack": 0, "lag": False, "gate": gate}
        c["task"] = asyncio.create_task(self._writer(ws, c))
        self._clients[ws] = c

    def ack(self, ws: WebSocket) -> None:
        c = self._clients.get(ws)
        if c:
            c["unack"] = 0
            c["force"] = False
            c["gate"].set()             # окно открыто — писатель продолжает
            if c["lag"]:
                c["lag"] = False        # догнал — снова живые дельты

    def remove(self, ws: WebSocket) -> None:
        c = self._clients.pop(ws, None)
        if c:
            c["task"].cancel()

    @property
    def count(self) -> int:
        return len(self._clients)

    async def _writer(self, ws: WebSocket, c: dict) -> None:
        q = c["q"]
        try:
            while True:
                ev = await q.get()
                if c["unack"] >= self.WINDOW and not c.get("force"):
                    c["gate"].clear()              # труба полна: ждём ack,
                    try:                           # чтобы live_gap не тонул
                        await asyncio.wait_for(c["gate"].wait(), timeout=5)
                    except asyncio.TimeoutError:
                        c["force"] = True          # клиент жив, но нем (нет ack):
                        logger.info("шина: зритель без ack — только служебные")
                if c.get("force") and ev.get("type") in ("think", "text"):
                    continue                       # дельты вернутся после ack
                await asyncio.wait_for(ws.send_json(ev), timeout=10)
                if not c.get("force"):
                    c["unack"] += 1
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.info("шина: зритель отпал (%s)", repr(e)[:80])
            self.remove(ws)          # сокет умер/завис — зритель отцеплен

    @staticmethod
    def _shed(q: asyncio.Queue) -> None:
        """Аварийный сброс отстающему: из его очереди выкидываются только
        дельты стрима (think/text), служебное (стадии/итоги/чат) остаётся,
        плюс метка live_gap — фронт по ней пересинхронизируется снимком
        /api/live, где живой буфер всегда полный. Никто не отцепляется."""
        keep = [e for e in q._queue if e.get("type") not in ("think", "text")]
        q._queue.clear()
        for e in keep:
            q._queue.append(e)
        q._queue.append({"type": "live_gap"})

    async def broadcast(self, ev: dict) -> None:
        for ws, c in list(self._clients.items()):
            q = c["q"]
            if not c["lag"] and c["unack"] + q.qsize() >= self.LAG_AFTER:
                self._shed(q)              # захлебнулся: дельты — в снимок,
                c["lag"] = True            # дальше кормим только служебным
                logger.info("шина: зритель отстал — режим догоняющего (до ack)")
            if c["lag"] and ev.get("type") in ("think", "text"):
                continue                   # живые дельты вернутся после ack
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:      # совсем клинический случай
                pass


HUB = _Hub()


@app.websocket("/ws/events")
async def ws_events(ws: WebSocket):
    """Подписка зрителя: сразу отдаём снимок идущих разборов, дальше — поток."""
    await ws.accept()
    HUB.add(ws)
    try:
        await ws.send_json({"type": "hello", "version": config.APP_VERSION,
                            "clients": HUB.count,
                            "running": pipeline.running_now(),
                            "v5": bus.running()})
        while True:   # фронт шлёт ping каждые 25с и ack каждые 200 кадров;
            # молчание дольше лимита = мёртвая вкладка
            idle = max(5, config.get_int("EVENTS_IDLE_SEC", 90))
            msg = await asyncio.wait_for(ws.receive_text(), timeout=idle)
            if msg.startswith("ack"):
                HUB.ack(ws)
    except asyncio.TimeoutError:
        logger.info("шина: зритель молчал дольше лимита — отцеплен")
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.info("шина: /ws/events завершён (%s)", repr(e)[:80])
    finally:
        HUB.remove(ws)
        try:
            await ws.close()
        except Exception:
            pass


@app.get("/api/live")
async def api_live(code: str):
    """Живой снимок идущего разбора — для зрителя, подключившегося посередине."""
    snap = pipeline.live_snapshot((code or "")[:32])
    if not snap:
        return JSONResponse({"error": "не идёт"}, status_code=404)
    return snap


@app.websocket("/ws/analyze")
async def ws_analyze(ws: WebSocket):
    await ws.accept()
    try:
        try:  # клиент, который подключился и молчит, не держит сокет вечно
            req = await asyncio.wait_for(ws.receive_json(), timeout=30)
        except asyncio.TimeoutError:
            await ws.close()
            return
        if not isinstance(req, dict):
            await ws.send_json({"type": "error", "detail": "ожидается JSON-объект {codes:[...]}"})
            await ws.close()
            return
        codes = req.get("codes") or req.get("tickers") or []
        if isinstance(codes, str):
            codes = [codes]
        if not isinstance(codes, (list, tuple)):
            codes = [codes]
        codes = list(dict.fromkeys(str(c).upper().strip()
                                   for c in codes if str(c).strip()))[:20]
        mode = req.get("mode") or prompts.DEFAULT_MODE
        if not codes:
            await ws.send_json({"type": "error", "detail": "не выбран инструмент"})
            await ws.close()
            return

        send_lock = asyncio.Lock()
        state = {"dead": False}

        async def emit(ev: dict):
            if state["dead"]:
                return
            async with send_lock:
                try:
                    # уснувший телефон держит TCP открытым, но не читает: send
                    # виснет на полном буфере и раньше МОРОЗИЛ весь конвейер.
                    await asyncio.wait_for(ws.send_json(ev), timeout=10)
                except Exception:
                    state["dead"] = True    # клиент ушёл/завис — разборы доходят до базы в фоне
                    logger.info("ws_analyze: клиент недоступен, конвейер продолжает в фоне")

        # у КАЖДОГО объекта должен быть свой ключ; без ключа — пропуск,
        # остальные из пакета разбираются как ни в чём не бывало
        missing_src = list(codes)
        missing = [c for c in codes if not config.has_key_for(c)]
        for c in missing:
            await emit({"type": "skip", "ticker": c, "reason": "no_key",
                        "detail": f"{c}: нет ключа DeepSeek — пропущен"})
        codes = [c for c in codes if c not in missing]
        if not codes and not any(c in pipeline.RUNNING for c in missing_src):
            await emit({"type": "error",
                        "detail": "ни у одного объекта нет ключа DeepSeek — "
                                  "введи ключи и запусти снова"})
            await ws.close()
            return

        # занятые тикеры отбиваем здесь же, напрямую пускателю
        busy = [c for c in codes if c in pipeline.RUNNING]
        for c in busy:
            await emit({"type": "skip", "ticker": c, "reason": "busy",
                        "detail": f"{c}: уже разбирается — второй конвейер не запускаю"})
        codes = [c for c in codes if c not in busy]

        import uuid
        lid = uuid.uuid4().hex[:10]

        async def to_hub(ev: dict):
            ev["launch_id"] = lid
            await HUB.broadcast(ev)

        if codes:
            # ПОКА ИИ ДУМАЕТ — петля уже КОПИТ данные (accumulate): стакан,
            # рентген, история цен набираются, но реальные входы закрыты.
            # В режиме ИИ-ПИЛОТА оракульская петля не поднимается вовсе:
            # решает ИИ, исполняет ai_pilot (см. _observe ниже).
            # РЕЖИМ ФИКСИРУЕТСЯ НА СТАРТЕ РАЗБОРА (веер, critical): щёлкнув
            # тумблер в середине прогона, владелец раньше получал разрыв —
            # шифровщик работал по одному режиму, а петлю поднимали по
            # другому (оракульскую гасили, пилот оставался без приказа).
            pilot_mode = _ai_pilot_enabled()
            if _auto_enabled() and not pilot_mode:
                for c in codes:
                    r0 = await _auto_start(c, "накопление во время разбора",
                                           accumulate=True)
                    await HUB.broadcast({
                        "type": "auto", "ticker": c, "launch_id": lid,
                        "ok": bool(r0.get("ok")),
                        "detail": (f"{c}: автопилот копит данные, пока ИИ думает"
                                   if r0.get("ok") else
                                   f"{c}: накопление не стартовало — {r0.get('note')}")})

            # События конвейера идут В ШИНУ: их видят ВСЕ подключённые,
            # включая самого пускателя (он тоже слушает /ws/events).
            # Сокет запуска — только для подтверждения и точечных skip.
            task = _background(pipeline.run_pipeline(codes, to_hub, mode=mode), "legacy pipeline")
            started_codes = list(codes)

            def _observe(t: asyncio.Task) -> None:
                if t.cancelled():
                    return
                e = t.exception()
                if e:
                    logger.error("фоновый конвейер: %s", str(e)[:200])
                    return
                # РАЗБОР ЗАКОНЧИЛСЯ → СРАЗУ ТОРГОВАЯ ПЕТЛЯ (без второго терминала
                # и без кнопок одобрения — прямое требование владельца).
                if not _auto_enabled() and not _ai_pilot_enabled():
                    # у ИИ-пилота свой рубильник: PYTHIA_AUTOSTART=0 не должен
                    # делать режим недостижимым (находка веера)
                    logger.info("автостарт выключен (PYTHIA_AUTOSTART=0)")
                    return

                async def _kick() -> None:
                    for c in started_codes:
                        if pilot_mode:      # тот же режим, что видел шифровщик
                            # ИИ-ПИЛОТ: exec-приказ шифровщика → вход на макс
                            r = await _aipilot_on_analysis(c)
                            st = r.get("status") or {}
                            await HUB.broadcast({
                                "type": "auto", "ticker": c, "launch_id": lid,
                                "ok": bool(r.get("ok")),
                                "detail": (f"{c}: РАЗБОР ГОТОВ → ИИ-ПИЛОТ "
                                           f"({st.get('state')}) — "
                                           f"{st.get('last_action')}"
                                           if r.get("ok") else
                                           f"{c}: ИИ-пилот не принял приказ — "
                                           f"{r.get('note') or (st or {}).get('last_action')}")})
                            continue
                        # разбор готов → снимаем гейт: петля (уже копившая) идёт в бой
                        r = await _auto_start(c, "разбор завершён")
                        st = r.get("status") or {}
                        real = st.get("trades_real")
                        await HUB.broadcast({
                            "type": "auto", "ticker": c, "launch_id": lid,
                            "ok": bool(r.get("ok")),
                            "detail": (f"{c}: РАЗБОР ГОТОВ → автопилот "
                                       + ("БОЕВОЙ, торгует" if real else
                                          "сухой прогон (ордера в лог)")
                                       if r.get("ok") else
                                       f"{c}: автопилот не стартовал — {r.get('note')}")})

                _background(_kick(), "legacy pilot launch")

            task.add_done_callback(_observe)
        await emit({"type": "launched", "launch_id": lid, "tickers": codes,
                    "watch_busy": busy})
        await ws.close()
        return
    except WebSocketDisconnect:
        logger.info("ws_analyze: клиент отключился")
    except Exception as e:
        logger.exception("ws_analyze error")
        try:
            await ws.send_json({"type": "error", "detail": str(e)[:200]})
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────
# WebSocket: чат с памятью по инструменту
# ──────────────────────────────────────────────────────────────────────

@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            req = await ws.receive_json()
            if not isinstance(req, dict):
                continue
            code = (str(req.get("code") or "")).strip()
            cid = str(req.get("cid") or "")[:24]     # id вкладки-автора (для фильтра эха)
            user_msg = (str(req.get("message") or "")).strip()
            if len(user_msg) > 8000:      # мегабайтная вставка не должна раздувать промпт и базу
                user_msg = user_msg[:8000] + "\n[…обрезано: сообщение длиннее 8000 символов]"
            mode = req.get("mode") or prompts.DEFAULT_MODE
            if not user_msg:
                continue
            it = instruments.get(code)
            tk = it["code"] if it else code.upper()
            name = it["name"] if it else tk

            if not config.has_key_for(tk):
                await ws.send_json({"type": "error",
                                    "detail": "не задан ключ DeepSeek для этого инструмента"})
                continue

            try:
                astro_text = astro.render_for_ai(await astro.acontext())
            except Exception:
                astro_text = ""

            rec = await memory.get_analysis(tk)
            if rec:
                dossier = rec.get("dossier") or {}
                dossier_text = pipeline.render_dossier(dossier, full=False)
                verdict = rec.get("verdict") or ""
                news_text = pipeline.render_news_for_ticker(rec.get("news") or [])
                wyckoff_text = wyckoff.render_for_ai(dossier.get("wyckoff") or {})
            else:
                dossier_text = "(разбор ещё не запускался — данных по объекту нет)"
                verdict = ""
                news_text = ""
                wyckoff_text = ""

            sys = prompts.chat_system(tk, name, dossier_text, verdict, news_text,
                                      mode=mode, astro_text=astro_text,
                                      wyckoff_text=wyckoff_text)
            history = await memory.get_chat(tk, limit=40)
            messages = [{"role": "system", "content": sys}]
            for h in history:
                messages.append({"role": h["role"], "content": h["content"]})
            messages.append({"role": "user", "content": user_msg})

            await memory.add_chat(tk, "user", user_msg)
            await HUB.broadcast({"type": "chat_msg", "code": tk, "role": "user",
                                 "content": user_msg, "cid": cid})
            chat_state = {"dead": False}

            async def _safe_send(ev: dict) -> None:
                """Уснувший телефон рвёт сокет мид-стрим. Раньше это роняло
                весь ai.chat и ответ терялся. Теперь: сокет умер — стрим
                молча доживает, ответ уходит в базу, оператор увидит его
                в истории при следующем входе."""
                if chat_state["dead"]:
                    return
                try:
                    await asyncio.wait_for(ws.send_json(ev), timeout=10)
                except Exception:
                    chat_state["dead"] = True

            await _safe_send({"type": "start", "ticker": tk})

            async def on_think(delta, _full):
                await _safe_send({"type": "think", "delta": delta})

            async def on_text(delta, _full):
                await _safe_send({"type": "text", "delta": delta})

            try:
                answer = await ai.chat(messages, on_think=on_think,
                                       on_text=on_text, thinking=True,
                                       effort=config.AI_REASONING_EFFORT,
                                       route="chat",   # без max_tokens: чат сам решает объём
                                       api_key=config.key_for(tk))
            except Exception as e:
                answer = f"[ошибка: {ai.humanize_error(e)[:160]}]"
                await _safe_send({"type": "text", "delta": answer})
            if answer and not answer.startswith("[ошибка:"):
                await memory.add_chat(tk, "assistant", answer)
                await HUB.broadcast({"type": "chat_msg", "code": tk,
                                     "role": "assistant", "content": answer,
                                     "cid": cid})
            await _safe_send({"type": "done", "ticker": tk})
            if chat_state["dead"]:
                break   # сокет мёртв — выходим из цикла приёма
    except WebSocketDisconnect:
        logger.info("ws_chat: клиент отключился")
    except Exception as e:
        logger.exception("ws_chat error")
        try:
            await ws.send_json({"type": "error", "detail": str(e)[:200]})
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────
# Фронт
# ──────────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    f = FRONTEND / "index.html"
    if f.exists():
        return FileResponse(str(f))
    return PlainTextResponse("frontend/index.html не найден", status_code=404)


@app.get("/favicon.ico")
async def favicon():
    # браузер просит /favicon.ico независимо от <link rel="icon"> —
    # без маршрута в консоли висел 404 при каждом старте
    f = FRONTEND / "static" / "icon.svg"
    if f.exists():
        return FileResponse(str(f), media_type="image/svg+xml")
    return PlainTextResponse("", status_code=204)


if (FRONTEND / "static").exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND / "static")),
              name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.server:app", host=config.HOST, port=config.PORT,
                reload=False)
