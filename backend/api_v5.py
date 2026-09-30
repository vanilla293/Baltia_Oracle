# -*- coding: utf-8 -*-
"""ПИФИЯ v5 — общий API: состояние, ключи, снимки прогонов.

Роутеры этапов подключаются здесь же (api_council, api_mission) — каждый в
try/except, чтобы сервер поднимался даже без одного из них.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import os
import re
import sys
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from . import ai, ai_v5, astro, bus, config, store_v5, tinkoff

try:                                                    # фаза 4 · W2: Telegram-бот (ключи, панель проблем)
    from . import telegram
except Exception:                                       # noqa: BLE001
    telegram = None

log = logging.getLogger("pythia.api_v5")
router = APIRouter()


def _mod(name: str):
    try:
        return importlib.import_module(f"backend.{name}")
    except Exception:            # noqa: BLE001
        try:
            return importlib.import_module(f".{name}", package=__package__)
        except Exception as e:   # noqa: BLE001
            log.debug("модуль %s недоступен: %s", name, str(e)[:80])
            return None


def _safe(fn, default=None):
    try:
        return fn()
    except Exception as e:       # noqa: BLE001
        log.info("state: %s", str(e)[:100])
        return default


def _mask(s: str) -> str:
    if not s:
        return ""
    return s[:4] + "…" + s[-3:] if len(s) > 10 else "•••"


_astro_task = None               # фоновый пересчёт неба (не чаще одного за раз)
_mkt_task = None                 # фоновый добор рыночных часов на холодном кэше


def _astro_line() -> str:
    """Строка неба для панели без блокировки: кэш как есть; протух или пуст —
    пересчёт уходит в фон (astro.acontext в потоке), ответ не ждёт секунды CPU."""
    global _astro_task
    try:
        ctx, age = astro.peek_context()
        stale = ctx is None or age is None or age >= float(getattr(astro, "ASTRO_CACHE_SEC", 600))
        if stale and (_astro_task is None or _astro_task.done()):
            try:
                _astro_task = asyncio.get_running_loop().create_task(astro.acontext())
            except RuntimeError:                     # вне event loop (тесты) — считаем прямо
                ctx = astro.cached_context()
        return astro.short_line(ctx) if ctx else ""
    except Exception:            # noqa: BLE001
        return ""


def _token_check() -> dict | None:
    """v5.4.4: что известно о токене Т-Банка (проверка GetAccounts при сохранении / отказы 401–403 после неё):
    {ok, trade, kind, reason, access, ts} без секретов; ничего не известно — None. Токен «задан» ≠ «рабочий»."""
    fn = getattr(tinkoff, "token_state", None)
    try:
        st = fn() if callable(fn) else None
    except Exception:            # noqa: BLE001
        st = None
    if not isinstance(st, dict):
        return None
    return {k: st.get(k) for k in ("ok", "trade", "kind", "reason", "access", "ts")}


def keys_status() -> dict:
    pool = config.deepseek_keys()
    inst = list((config.INSTRUMENT_KEYS or {}).values())
    return {"deepseek": bool(pool or inst), "deepseek_accounts": len(pool),
            "deepseek_mask": [_mask(k) for k in pool] or [_mask(k) for k in inst[:1]],
            "tinkoff": tinkoff.enabled(), "tinkoff_mask": _mask(config.TINKOFF_TOKEN),
            "tinkoff_check": _token_check(),
            "dry": os.getenv("PYTHIA_DRY", "") == "1",
            # фаза 4 · W2: Telegram — только маски, ни токена, ни chat_id целиком
            "telegram": bool(telegram and telegram.token()), "telegram_mask": _mask(telegram.token()) if telegram else "",
            "telegram_chat": telegram.chat_mask() if telegram else "", "telegram_bound": bool(telegram and telegram.bound()),
            "telegram_on": bool(telegram and telegram.enabled())}


# ── v5.4.1 «трезвый пилот»: настройки поведения пилота для панели (переключатели в «Ключах») ─────────────
SETTINGS_DEFAULT = {"exchange_stop": False, "money_model": "pro", "entry_check": True, "profit_think": True,
                    "profit_think_pct": 60.0}


def settings_block() -> dict:
    """Настройки 5.4.1 из config.* (живые после config.set_many): exchange_stop — аварийный трос на бирже
    (PYTHIA_EXCHANGE_STOP; False — стопы только в программе), money_model — кто решает у денег (PYTHIA_MONEY_MODEL
    pro|flash), entry_check — проверка входа у двери (PYTHIA_ENTRY_CHECK), profit_think / profit_think_pct — мысль о
    прибыли и её порог (PYTHIA_PROFIT_THINK, PYTHIA_PROFIT_THINK_PCT). Сбой чтения → умолчания 5.4.1."""
    try:
        mm = str(getattr(config, "PYTHIA_MONEY_MODEL", "pro") or "pro").strip().lower()
        return {"exchange_stop": bool(getattr(config, "PYTHIA_EXCHANGE_STOP", False)),
                "money_model": "flash" if mm == "flash" else "pro",
                "entry_check": bool(getattr(config, "PYTHIA_ENTRY_CHECK", True)),
                "profit_think": bool(getattr(config, "PYTHIA_PROFIT_THINK", True)),
                "profit_think_pct": float(getattr(config, "PYTHIA_PROFIT_THINK_PCT", 60.0))}
    except Exception as e:   # noqa: BLE001
        log.info("settings: %s", str(e)[:80])
        return dict(SETTINGS_DEFAULT)


async def _market_state(mission_snap: dict) -> dict:
    """Рыночные часы для панели: по активной миссии (тикер/класс), иначе по SBER; сбой — честный note."""
    mc = _mod("market_clock")
    if not mc:
        return {"open": None, "reason": "модуль часов недоступен", "next_open_ts": None, "session": None, "skew_s": 0.0}
    try:
        act = (mission_snap or {}).get("active")
        ms = ((mission_snap or {}).get("missions") or {}).get(act) or {}
        return await asyncio.wait_for(mc.snapshot(act or None, ms.get("asset_class")), 6)
    except asyncio.TimeoutError as e:
        # холодный кэш (TradingSchedules + GetTradingStatus) — не держим панель: добираем фоном,
        # следующий опрос (5 с) возьмёт из кэша; сейчас — прошлый статус честно
        global _mkt_task
        if _mkt_task is None or _mkt_task.done():
            _mkt_task = asyncio.get_running_loop().create_task(mc.snapshot(act or None, ms.get("asset_class")))
        e = RuntimeError("биржа отвечает медленно — статус добирается фоном")
        last = None
        try:
            last = mc.last()
        except Exception:    # noqa: BLE001
            pass
        return {"open": (last or {}).get("open"), "reason": f"часы споткнулись: {str(e)[:80]}",
                "next_open_ts": (last or {}).get("next_open_ts"), "next_open_msk": (last or {}).get("next_open_msk"),
                "session": (last or {}).get("session"), "skew_s": (mc.skew() or {}).get("skew_s", 0.0),
                "text": mc.describe(last) if last else "рынок: статус неизвестен"}
    except Exception as e:   # noqa: BLE001
        last = None
        try:
            last = mc.last()
        except Exception:    # noqa: BLE001
            pass
        return {"open": (last or {}).get("open"), "reason": f"часы споткнулись: {str(e)[:80]}",
                "next_open_ts": (last or {}).get("next_open_ts"), "next_open_msk": (last or {}).get("next_open_msk"),
                "session": (last or {}).get("session"), "skew_s": (mc.skew() or {}).get("skew_s", 0.0),
                "text": mc.describe(last) if last else "рынок: статус неизвестен"}


# ── v5.3 фаза 3 (W1): панель проблем — блок "health" в /api/v5/state ─────────────────────────
PRICE_STALE_SEC = 60.0        # цена миссии старше при открытом рынке → «протухшие данные»
TICK_STALE_SEC = 20.0         # v5.4.4: пилот «не тикает» — не раньше N с после последнего тика (или запуска)
_LEVEL_ORDER = {"err": 0, "warn": 1, "info": 2}


def _prob(kind: str, level: str, text: str, ts: float | None = None, key: str | None = None) -> dict:
    """Проблема панели; key (v5.4.4) — вид проблемы для Telegram (одна и та же — не чаще раза в 10 мин), по умолчанию
    kind: у пилота их несколько разных (связь, не тикает, отказ биржи, killswitch) — ключ не даёт им глушить друг друга."""
    return {"kind": kind, "level": level, "text": text, "ts": ts, "key": key or kind}


_WHAT_RU = {"entry": "вход", "topup": "добор", "close": "закрытие", "stop": "стоп-заявку"}
_SRC_RU = {"market": "котировки", "orders": "заявки", "account": "счёт"}
_TK_HINT = {"auth": " — токен не принят: выпусти новый с полным доступом и вставь в «Ключи»",
            "rights": " — у токена нет прав: нужен токен с полным доступом к этому счёту («Ключи»)",
            "cert": " — TLS: нужны CA Минцифры (data/russian_trusted.pem), см. журнал сервера"}


def _ws_clients() -> int | None:
    """Сколько вкладок держат /ws/events (server.HUB); сервера в процессе нет (тесты) → None."""
    try:
        srv = sys.modules.get("backend.server") or sys.modules.get("server")
        hub = getattr(srv, "HUB", None)
        return int(hub.count) if hub is not None else None
    except Exception:        # noqa: BLE001
        return None


def _astro_mode() -> str | None:
    """Режим неба по кэшу без расчёта: "precise" | "lite" | None (ещё не считалось)."""
    try:
        ctx, _age = astro.peek_context()
        return str(ctx.get("mode")) if isinstance(ctx, dict) and ctx.get("mode") else None
    except Exception:        # noqa: BLE001
        return None


def _pilot_problems(ms: dict, market: dict | None, now: float) -> tuple[list[dict], float | None, float | None]:
    """Проблемы пилота активной миссии + свежесть данных. Возвращает (проблемы, price_ts, tick_ts)."""
    out: list[dict] = []
    t = str(ms.get("ticker") or "")
    pst = ms.get("pilot") if isinstance(ms.get("pilot"), dict) else {}
    if ms.get("error"):
        out.append(_prob("pilot", "err", f"Миссия {t}: {str(ms['error'])[:160]} — смотри журнал сервера; "
                                         f"«продолжить» поднимет пилот заново", ms.get("exec_ts")))
    if pst.get("error"):
        out.append(_prob("pilot", "err", f"Пилот {t} не отдал состояние: {str(pst['error'])[:120]}", now))
    pos = pst.get("position") if isinstance(pst.get("position"), dict) else {}
    feed = pst.get("feed") if isinstance(pst.get("feed"), dict) else {}
    feed_down = bool(feed) and feed.get("ok") is False
    if feed_down:
        # v5.4.4: связи с брокером нет — пилот не тикает, решений и заявок нет; позиция без защиты программы
        fk = str(feed.get("kind") or "")
        since = feed.get("since")
        mins = int((now - float(since)) // 60) if since else 0
        pos_t = (f"; позиция {pos.get('side')} {pos.get('lots')} лот БЕЗ ЗАЩИТЫ программы (стопов на бирже нет) — закрой "
                 f"в приложении брокера" if pos and not pos.get("stop_id") else
                 f"; позиция {pos.get('side')} {pos.get('lots')} лот: программа её не ведёт" if pos else "")
        cause = str(feed.get("cause") or feed.get("reason") or fk)
        out.append(_prob("pilot", "err" if fk in ("auth", "rights", "cert") or pos else "warn",
                         f"Пилот {t}: {'нет доступа к брокеру' if fk in ('auth', 'rights') else 'нет связи с брокером'} "
                         f"{mins} мин — {cause[:180]}{pos_t}", since or now, key="pilot:feed"))
    elif (ms.get("live") and pst.get("loop_alive") is True and pst.get("ticking") is False
          and feed.get("kind") != "closed"                 # рынок закрыт и цены нет — ночь, не авария
          and now - float(pst.get("last_tick_ts") or pst.get("tick_ts") or pst.get("started_ts") or now) > TICK_STALE_SEC):
        last = pst.get("last_tick_ts") or pst.get("tick_ts")
        age = int(now - float(last)) if last else None
        out.append(_prob("pilot", "err", f"Пилот {t} не тикает" + (f" {age} с" if age is not None else " с запуска") +
                         " — петля жива, но тиков нет: смотри журнал сервера; «стоп» и «продолжить» перезапустят пилот",
                         now, key="pilot:ticking"))
    elif (ms.get("live") and pst and pst.get("loop_alive") is False and not ms.get("error")
          and pst.get("started_ts") and now - float(pst["started_ts"]) > 90):
        out.append(_prob("pilot", "warn", f"Пилот {t} готовится {int(now - float(pst['started_ts']))} с и ещё не тикает "
                                          "(счёт, портфель, контракт у брокера)", now, key="pilot:ticking"))
    br = pst.get("broker_refusal") if isinstance(pst.get("broker_refusal"), dict) else {}
    if br and br.get("kind") not in ("auth", "rights") and now - float(br.get("ts") or 0) < 1800:
        what = str(br.get("what") or "")
        out.append(_prob("pilot", "err" if what in ("close", "stop") else "warn",
                         f"Биржа отбила {_WHAT_RU.get(what, what)} {t}: {str(br.get('text') or '')[:160]}"
                         + (f" ({br.get('count')} подряд)" if int(br.get("count") or 0) > 1 else "")
                         + (" — повторяю с паузой, не каждый тик" if what in ("close", "stop") else ""),
                         br.get("ts"), key=f"pilot:refusal:{what}"))
    if pos.get("close_fail"):
        out.append(_prob("pilot", "err", f"Закрытие позиции {t} не завершено ({str(pos['close_fail'])[:100]}) — "
                                         f"повторяю с паузой (до 60 с); не закрывается — закрой руками в терминале брокера",
                         now, key="pilot:close"))
    if pos.get("stop_err") and not pos.get("stop_id"):
        out.append(_prob("pilot", "err", f"Трос {t} на бирже не встал: {str(pos['stop_err'])[:100]} — держу виртуальный "
                                         f"стоп (за уровнем закрою сам), повтор каждые {int(getattr(_mod('ai_pilot'), 'STOP_RETRY_SEC', 30))} с; "
                                         f"проверь стоп-заявки у брокера", now, key="pilot:stop"))
    sr = pst.get("killswitch") if isinstance(pst.get("killswitch"), dict) else {}
    if sr.get("locked"):
        out.append(_prob("pilot", "err", f"Killswitch {t}: {str(sr.get('reason') or 'дневной лимит убытка')[:100]} — "
                                         f"торговля остановлена до нового торгового дня (МСК); приказы не принимаются",
                         now, key="pilot:killswitch"))
    ef = int(pst.get("entry_fail") or 0)
    left = pst.get("no_entry_in_s")
    if ef > 0 and left:
        out.append(_prob("pilot", "warn", f"Биржа отбивает заявки {t} ({ef} подряд) — бэкофф ещё {int(left)} с, "
                                          f"после {int(getattr(_mod('ai_pilot'), 'ENTRY_FAIL_MAX', 5))} отказов план снимается; "
                                          f"причина — в «последнем действии» пилота", now, key="pilot:backoff"))
    open_ = None if not isinstance(market, dict) else market.get("open")
    price_ts = pst.get("price_ts")
    tick_ts = pst.get("tick_ts")
    # v5.4.4: «рынок мёртв» / «решает по старой цене» — только когда связь есть и пилот тикает; без связи (выше) пилот не
    # решает вовсе, и причина — связь, а не рынок
    link_ok = not feed_down and pst.get("ticking") is not False
    if ms.get("live") and pst and open_ is not False and link_ok:
        age = (now - float(price_ts)) if price_ts else None
        if age is not None and age > PRICE_STALE_SEC:
            out.append(_prob("data", "warn", f"Цена {t} не обновлялась {int(age)} с при открытом рынке — котировки "
                                             f"Tinkoff не идут (сеть, токен, лимит запросов?)", price_ts))
        elif pst.get("market_alive") is False and (pos or pst.get("plan")):
            ba = pst.get("book_age_s")
            out.append(_prob("data", "warn", f"Стакан {t} пуст или протух" + (f" ({int(ba)} с)" if ba else "") +
                                             " — рынок «мёртв» для пилота: входов нет, перепроверка отложена", now))
    return out, price_ts, tick_ts


def health_block(*, keys: dict | None = None, mission_snap: dict | None = None,
                 market: dict | None = None, now: float | None = None) -> dict:
    """Блок "health" для панели: что сломано и что делать. Каждый источник — в try/except: сбой одного
    не роняет ни блок, ни state. Форма: {"problems": [{"kind": "keys|deepseek|tinkoff|telegram|market|ws|astro|pilot|data",
    "level": "err|warn|info", "text": "…человеку…", "ts"}], "last_ai_error", "last_broker_error", "astro_mode",
    "ws_clients", "state_age_s"} — problems отсортированы err → warn → info."""
    now = now or time.time()
    problems: list[dict] = []
    mock_ai = os.getenv("PYTHIA_MOCK_AI", "") == "1"
    mock_tk = os.getenv("PYTHIA_MOCK_TINKOFF", "") == "1"
    # 1. ключи
    try:
        ks = keys if keys is not None else keys_status()
        if mock_ai:
            problems.append(_prob("keys", "info", "Мок ИИ (PYTHIA_MOCK_AI=1): ответы DeepSeek подделаны — демо без ключей и сети", None))
        elif not ks.get("deepseek"):
            problems.append(_prob("keys", "err", "Нет ключа DeepSeek — совет, перепроверки, толмач и чат молчат. "
                                                 "Нажми «Ключи» в шапке и вставь ключ sk-… с platform.deepseek.com", None))
        if mock_tk:
            problems.append(_prob("keys", "info", "Мок биржи (PYTHIA_MOCK_TINKOFF=1): котировки и заявки поддельные", None))
        elif not ks.get("tinkoff"):
            problems.append(_prob("keys", "warn", "Нет токена Tinkoff — нет котировок, стакана, сканера и заявок: "
                                                  "«Ключи» → токен Tinkoff Invest (для наблюдения хватает read-only)", None))
        if ks.get("dry"):
            problems.append(_prob("keys", "info", "Сухой прогон (PYTHIA_DRY=1): заявки пишутся только в data/trader_audit.jsonl, "
                                                  "на биржу не уходят", None))
    except Exception as e:   # noqa: BLE001
        log.info("health: ключи: %s", str(e)[:80])
    # 2. DeepSeek — последняя ошибка после всех повторов (актуальна, пока ИИ не ответил снова)
    last_ai = None
    try:
        last_ai = ai_v5.last_error()
        last_ai = last_ai if isinstance(last_ai, dict) else None
        if last_ai and not last_ai.get("stale"):
            code = last_ai.get("code")
            lvl = "err" if code in (401, 402, 403) else "warn"
            problems.append(_prob("deepseek", lvl, f"DeepSeek ({last_ai.get('route') or '?'}, {ai_v5.fmt_ts(last_ai.get('ts'))}): "
                                                   f"{last_ai.get('text')}", last_ai.get("ts")))
    except Exception as e:   # noqa: BLE001
        log.info("health: deepseek: %s", str(e)[:80])
    # 3. Tinkoff — последняя ошибка API брокера по ИСТОЧНИКАМ (v5.4.4: котировки / заявки / счёт): ошибка актуальна, пока
    #    ЭТОТ ЖЕ источник не ответил снова — удачная цена не прячет отказ заявок и счёта. Токен/права/TLS — err
    last_tk = None
    try:
        last_tk = tinkoff.last_error()
        last_tk = last_tk if isinstance(last_tk, dict) else None
        if last_tk:
            src0 = last_tk.get("source")
            try:
                ok_ts = float((tinkoff.last_ok_ts(src0) if src0 else tinkoff.last_ok_ts()) or 0.0)
            except TypeError:                              # старая подпись last_ok_ts() без источника (фейки)
                ok_ts = float(tinkoff.last_ok_ts() or 0.0)
            last_tk["stale"] = bool(ok_ts and ok_ts > float(last_tk.get("ts") or 0))
        by_src = {}
        fn_err = getattr(tinkoff, "errors", None)
        try:
            by_src = fn_err() if callable(fn_err) else {}
        except Exception:    # noqa: BLE001
            by_src = {}
        rows = [r for r in (by_src or {}).values() if isinstance(r, dict)] or ([last_tk] if last_tk else [])
        for rec in sorted(rows, key=lambda r: float(r.get("ts") or 0), reverse=True):
            if rec.get("stale"):
                continue
            txt = str(rec.get("text") or "")
            kind = str(rec.get("kind") or "")
            if not kind:                                  # старая запись без вида — по тексту
                fn_cl = getattr(tinkoff, "classify", None)
                try:
                    kind = fn_cl(txt)[0] if callable(fn_cl) else ""
                except Exception:    # noqa: BLE001
                    kind = ""
            hint = _TK_HINT.get(kind, "")
            if not hint and ("RESOURCE_EXHAUSTED" in txt or "429" in txt or "80002" in txt):
                hint = " — биржа ограничила частоту запросов, подожди минуту"
            if hint and ("«Ключи»" in txt or "CA Минцифры" in txt or "подожди минуту" in txt):
                hint = ""                                 # подсказка уже в тексте (humanize_api_error)
            src = rec.get("source")
            problems.append(_prob("tinkoff", "err" if kind in ("auth", "rights", "cert") else "warn",
                                  f"Tinkoff{' · ' + _SRC_RU[src] if src in _SRC_RU else ''} ({rec.get('path') or '?'}, "
                                  f"{ai_v5.fmt_ts(rec.get('ts'))}): {txt}{hint}", rec.get("ts"),
                                  key=f"tinkoff:{src or 'api'}"))
    except Exception as e:   # noqa: BLE001
        log.info("health: tinkoff: %s", str(e)[:80])
    # 3б. Telegram (фаза 4 · W2): токен есть, но чат не привязан / бот отбит (401, 403, сеть) — пока не ответил снова
    try:
        if telegram is not None and (keys or {}).get("telegram"):
            if not telegram.bound():
                problems.append(_prob("telegram", "info", "Telegram: бот не привязан к чату — напиши боту /start, он ответит "
                                                          "твоим chat_id; впиши его в «Ключи»", None))
            te = telegram.last_error()
            if te and not te.get("stale"):
                problems.append(_prob("telegram", "warn", f"Telegram ({te.get('method') or '?'}, {ai_v5.fmt_ts(te.get('ts'))}): "
                                                          f"{te.get('text')}", te.get("ts")))
    except Exception as e:   # noqa: BLE001
        log.info("health: telegram: %s", str(e)[:80])
    # 4. рынок (market_clock)
    try:
        if isinstance(market, dict) and market.get("enabled", True):
            reason = str(market.get("reason") or "")
            if reason.startswith("часы споткнулись"):
                problems.append(_prob("market", "warn", f"Рыночные часы споткнулись: {reason[17:].strip(' :')[:120]} — статус биржи "
                                                        f"по прошлому снимку или статическому расписанию", market.get("ts")))
            if market.get("open") is False:
                nxt = market.get("next_open_msk")
                rope = "биржи" if getattr(config, "PYTHIA_EXCHANGE_STOP", False) else "программы (стопа на бирже нет)"   # 5.4.1
                problems.append(_prob("market", "info", "Рынок закрыт" + (f" до {nxt}" if nxt else "") +
                                      f" ({reason or 'торгов нет'}) — пилот в стопоре: входов и заявок нет, позиция под тросом {rope}, "
                                      f"поводы копятся к открытию", market.get("ts")))
    except Exception as e:   # noqa: BLE001
        log.info("health: рынок: %s", str(e)[:80])
    # 5. небо
    astro_mode = _astro_mode()
    if astro_mode == "lite":
        problems.append(_prob("astro", "info", "Небо в lite-режиме: эфемерид skyfield нет — Луна по формулам, окна и затмения из "
                                               "таблиц 2026; для точного неба поставь skyfield и эфемериды (README)", None))
    # 6–7. пилот активной миссии и свежесть данных
    price_ts = tick_ts = None
    try:
        snap = mission_snap or {}
        act = snap.get("active")
        ms = ((snap.get("missions") or {}).get(act) or {}) if act else {}
        if ms:
            pp, price_ts, tick_ts = _pilot_problems(ms, market, now)
            problems.extend(pp)
    except Exception as e:   # noqa: BLE001
        log.info("health: пилот: %s", str(e)[:80])
    problems.sort(key=lambda x: _LEVEL_ORDER.get(x.get("level"), 9))
    ws = _ws_clients()
    return {"problems": problems[:20], "last_ai_error": last_ai, "last_broker_error": last_tk, "astro_mode": astro_mode,
            "ws_clients": ws, "state_age_s": (round(max(0.0, now - float(tick_ts)), 1) if tick_ts else None)}


@router.get("/api/v5/state")
async def api_state():
    council = _mod("council")
    mission = _mod("mission")
    ks = _safe(keys_status, {"deepseek": False, "tinkoff": False, "dry": True, "deepseek_accounts": 0,
                             "deepseek_mask": [], "tinkoff_mask": ""})   # сломанный config_user.json не роняет state
    mission_snap = _safe(mission.snapshot, {}) if mission else {}
    market = await _market_state(mission_snap)
    return {
        "app": {"version": config.APP_VERSION, "codename": config.APP_CODENAME,
                "name": config.APP_NAME, "dry": ks["dry"], "tinkoff": ks["tinkoff"],
                "deepseek": ks["deepseek"], "model": config.DEEPSEEK_MODEL,
                "model_fast": config.DEEPSEEK_MODEL_FAST,
                "days": getattr(config, "PYTHIA_V5_DAYS", 3),
                "watch_sec": getattr(config, "PYTHIA_WATCH_SEC", 900)},
        "keys": ks,
        "clock": {"msk": ai_v5.now_msk_str(), "ts": time.time()},
        "astro": {"line": _astro_line()},
        "tokens": _safe(ai_v5.usage, ai.tokens()),   # v5.3 W2: + by_route / by_model (панель фазы 3)
        "running": bus.running(),
        "recent_runs": bus.recent(8),
        "council": _safe(council.snapshot, {}) if council else {},
        "watch": {"notes": _safe(lambda: store_v5.watch_list(10), []),
                  "unseen": _safe(store_v5.watch_unseen_count, 0)},
        "mission": mission_snap,
        "market": market,
        "trades": _safe(lambda: _mod("ledger").trades_summary(), {}),   # фаза 4 W1: + gross/fee/net/est/mode/by_day
        "settings": settings_block(),   # v5.4.1: переключатели панели «Ключи» (трос на бирже, узлы у денег) + проверка входа, мысль о прибыли
        # v5.3 фаза 3 (W1): панель проблем — сбои источников не роняют state
        "health": _safe(lambda: health_block(keys=ks, mission_snap=mission_snap, market=market),
                        {"problems": [], "last_ai_error": None, "last_broker_error": None, "astro_mode": None,
                         "ws_clients": None, "state_age_s": None}),
    }


@router.get("/api/v5/run")
async def api_run(run_id: str):
    r = bus.run(run_id)
    if not r:
        return JSONResponse({"error": "нет такого прогона"}, status_code=404)
    return r


@router.get("/api/v5/keys")
async def api_keys_get():
    return {**keys_status(), "settings": settings_block()}   # v5.4.1: + настройки поведения пилота


@router.post("/api/v5/keys")
async def api_keys_set(payload: dict):
    payload = payload or {}
    ds = payload.get("deepseek")
    tk = payload.get("tinkoff")
    changed = []
    updates = {}
    if ds is not None:
        if isinstance(ds, str):
            ds = [k for k in ds.replace(",", " ").replace(";", " ").split() if k]
        if not isinstance(ds, (list, tuple)):
            return JSONResponse({"error": "deepseek: строка или список ключей"}, status_code=400)
        bad = [k for k in ds if not isinstance(k, str) or not k.isascii() or len(k) > 512
               or any(ord(ch) == 127 for ch in k)]
        if bad:
            return JSONResponse({"error": "ключ DeepSeek содержит недопустимые символы — "
                                          "вставь заново (латиница, обычно sk-…)"}, status_code=400)
        ds = list(dict.fromkeys("".join(ch for ch in k if ord(ch) > 32) for k in ds))
        ds = [k for k in ds if k]
        if ds:
            updates["DEEPSEEK_KEYS"] = ds
            changed.append("deepseek")
    if tk is not None:
        if not isinstance(tk, str):
            return JSONResponse({"error": "токен Tinkoff — строка"}, status_code=400)
        tk = "".join(ch for ch in tk if ord(ch) > 32)
        if tk and (not tk.isascii() or len(tk) > 512 or "\x7f" in tk):
            return JSONResponse({"error": "токен Tinkoff содержит недопустимые символы"},
                                status_code=400)
        if tk:
            updates["TINKOFF_TOKEN"] = tk
            changed.append("tinkoff")
    # фаза 4 · W2: Telegram — токен бота (BotFather) и chat_id владельца
    tg_tok, tg_chat = payload.get("telegram_token"), payload.get("telegram_chat")
    if tg_tok is not None:
        if not isinstance(tg_tok, str):
            return JSONResponse({"error": "токен Telegram — строка"}, status_code=400)
        tg_tok = "".join(ch for ch in tg_tok if ord(ch) > 32)
        if tg_tok and (not tg_tok.isascii() or len(tg_tok) > 128 or ":" not in tg_tok or "\x7f" in tg_tok):
            return JSONResponse({"error": "токен Telegram-бота выглядит как 123456789:AAAA… — вставь из BotFather целиком"},
                                status_code=400)
        if tg_tok:
            updates["TG_BOT_TOKEN"] = tg_tok
            changed.append("telegram_token")
    if tg_chat is not None:
        tg_chat = "".join(ch for ch in str(tg_chat) if ord(ch) > 32)
        if tg_chat and not re.fullmatch(r"-?\d{1,20}", tg_chat):
            return JSONResponse({"error": "chat_id Telegram — число (бот сообщает его в ответ на /start)"}, status_code=400)
        if tg_chat:
            updates["TG_CHAT_ID"] = tg_chat
            changed.append("telegram_chat")
    # v5.4.1 «трезвый пилот»: переключатели панели — трос на бирже (PYTHIA_EXCHANGE_STOP) и модель узлов у денег
    # (PYTHIA_MONEY_MODEL). Проверяются вместе со всем запросом и пишутся той же записью set_many; False хранится как
    # "0" явно (set_many выкидывает только None/"", а config.get_bool("0") даёт False) — иначе выключить было бы нельзя.
    ex_stop, mm = payload.get("exchange_stop"), payload.get("money_model")
    if ex_stop is not None:
        if not isinstance(ex_stop, bool):
            return JSONResponse({"error": "exchange_stop — true (трос на бирже) или false (стопы только в программе)"},
                                status_code=400)
        updates["PYTHIA_EXCHANGE_STOP"] = "1" if ex_stop else "0"
        changed.append("exchange_stop")
    if mm is not None:
        mm_s = mm.strip().lower() if isinstance(mm, str) else None
        if mm_s not in ("pro", "flash"):
            return JSONResponse({"error": "money_model — \"pro\" или \"flash\" (кто решает у троса, тейка, триажа, двери и о прибыли)"},
                                status_code=400)
        updates["PYTHIA_MONEY_MODEL"] = mm_s
        changed.append("money_model")
    # Проверяем весь запрос до первой записи: ошибка одного поля не меняет остальные ключи.
    # Пул DeepSeek — через set_deepseek_keys (демо-режим PYTHIA_MOCK_AI перехватывает её и держит ключи
    # в памяти, не трогая data/config_user.json), остальное — одной записью set_many.
    ds_keys = updates.pop("DEEPSEEK_KEYS", None)
    if ds_keys:
        config.set_deepseek_keys(ds_keys)
    if updates:
        config.set_many(updates)
    tk_check = None
    if "deepseek" in changed or "tinkoff" in changed:
        ai.reset_client()
        try:
            await tinkoff.aclose()
        except Exception:        # noqa: BLE001
            pass
    if "tinkoff" in changed:
        # v5.4.4: новый токен — прошлые ошибки в прошлое (панель не держит старый 401) и сразу проверка: GetAccounts +
        # уровень доступа (полный / только чтение); пилот без связи видит новую эпоху токена и проверяет брокера сразу
        try:
            fn_reset = getattr(tinkoff, "reset_errors", None)
            if callable(fn_reset):
                fn_reset()
            fn_chk = getattr(tinkoff, "check_access", None)
            if callable(fn_chk) and os.getenv("PYTHIA_MOCK_TINKOFF", "") != "1":
                tk_check = await asyncio.wait_for(fn_chk(), 20)
        except Exception as e:   # noqa: BLE001
            tk_check = {"ok": False, "kind": "network", "reason": f"проверка токена не завершилась: {str(e)[:100]}"}
    if changed and telegram is not None and ("telegram_token" in changed or "telegram_chat" in changed):
        try:
            await telegram.aclose()
        except Exception:        # noqa: BLE001
            pass
    out = {"ok": True, "changed": changed, **keys_status(), "settings": settings_block()}   # v5.4.1: + настройки
    if tk_check is not None:                 # v5.4.4: итог проверки нового токена — сразу в ответ «Ключей»
        out["tinkoff_check"] = {k: tk_check.get(k) for k in ("ok", "trade", "kind", "reason", "access", "ts")}
    return out


@router.delete("/api/v5/keys")
async def api_keys_wipe(what: str = ""):
    """Стереть ключи. ?what=telegram — только токен бота и chat_id (фаза 4 · W2); без — всё, включая Telegram."""
    if (what or "").strip().lower() == "telegram":
        config.set_many({"TG_BOT_TOKEN": None, "TG_CHAT_ID": None})
        return {"ok": True, "wiped": ["telegram"], **keys_status()}
    config.wipe_keys()
    ai.reset_client()
    return {"ok": True, "wiped": ["all"], **keys_status()}


@router.get("/api/v5/telegram")
async def api_telegram_status():
    """Состояние бота для панели (фаза 4 · W2): {enabled, token, token_mask, bound, chat_mask, polling, queued,
    sent, last_ok, last_error, daily}; без модуля — {enabled: false}."""
    if telegram is None:
        return {"enabled": False, "token": False, "bound": False, "note": "модуль telegram недоступен"}
    return telegram.status()


@router.post("/api/v5/telegram/test")
async def api_telegram_test():
    """Проверить бота: getMe + тестовое сообщение в привязанный чат → {ok, name, note, sent}."""
    if telegram is None:
        return {"ok": False, "note": "модуль telegram недоступен"}
    r = await telegram.check_token()
    r["sent"] = False
    if r.get("ok") and telegram.bound():
        telegram.send("✅ ПИФИЯ на связи: бот привязан, уведомления пойдут сюда. /help — команды")
        r["sent"] = await telegram.flush(1) > 0
        r["note"] = "бот отвечает, сообщение ушло" if r["sent"] else "бот отвечает, но сообщение не ушло: " + \
                    str((telegram.last_error() or {}).get("text") or "см. панель проблем")
    elif r.get("ok"):
        r["note"] = "бот отвечает; чат не привязан — напиши боту /start и впиши chat_id"
    return r


@router.get("/api/v5/health")
async def api_health():
    if not ai_v5.has_key():
        return {"ok": False, "error": "ключ DeepSeek не задан"}
    return await ai.health()


if __name__ == "__main__":
    # self-тест панели проблем: все источники — фейки, сеть и data/ не трогаются
    import tempfile
    from pathlib import Path as _P

    store_v5.DB_PATH = _P(tempfile.mkdtemp(prefix="pythia_api_v5_")) / "t.db"   # noqa: F811
    now = time.time()
    _saved = (ai_v5.last_error, tinkoff.last_error, tinkoff.last_ok_ts, astro.peek_context, ai.last_error)
    for k in ("PYTHIA_MOCK_AI", "PYTHIA_MOCK_TINKOFF", "PYTHIA_DRY"):
        os.environ.pop(k, None)
    try:
        # 1) всё чисто: нет проблем, нет ошибок, небо ещё не считалось
        ai_v5.last_error = lambda: None
        tinkoff.last_error = lambda: None
        tinkoff.last_ok_ts = lambda: 0.0
        astro.peek_context = lambda: (None, None)
        ks_ok = {"deepseek": True, "tinkoff": True, "dry": False}
        h = health_block(keys=ks_ok, mission_snap={}, market={"open": True, "reason": "торги идут", "enabled": True}, now=now)
        assert h["problems"] == [] and h["last_ai_error"] is None and h["last_broker_error"] is None, h
        assert h["astro_mode"] is None and h["state_age_s"] is None and h["ws_clients"] is None, h
        # 2) ключей нет + сухой прогон: err (DeepSeek), warn (Tinkoff), info (dry) — порядок err → warn → info
        h = health_block(keys={"deepseek": False, "tinkoff": False, "dry": True}, mission_snap={}, market=None, now=now)
        kinds = [(x["kind"], x["level"]) for x in h["problems"]]
        assert kinds == [("keys", "err"), ("keys", "warn"), ("keys", "info")], kinds
        assert "Ключи" in h["problems"][0]["text"] and "DeepSeek" in h["problems"][0]["text"]
        assert "Tinkoff" in h["problems"][1]["text"] and "trader_audit" in h["problems"][2]["text"]
        # 3) ошибки DeepSeek/Tinkoff: свежие → проблемы (401 — err, таймаут — warn); после успеха — только в last_*_error
        ai_v5.last_error = lambda: {"ts": now - 30, "route": "mission_review", "code": 401,
                                    "text": "ключ DeepSeek не принят (401)", "stale": False}
        tinkoff.last_error = lambda: {"ts": now - 20, "path": "PostOrder", "code": "30042", "text": "30042: недостаточно средств"}
        tinkoff.last_ok_ts = lambda: now - 100
        h = health_block(keys=ks_ok, mission_snap={}, market=None, now=now)
        kinds = [(x["kind"], x["level"]) for x in h["problems"]]
        assert kinds == [("deepseek", "err"), ("tinkoff", "warn")], kinds
        assert "mission_review" in h["problems"][0]["text"] and "401" in h["problems"][0]["text"] and h["problems"][0]["ts"] == now - 30
        assert "PostOrder" in h["problems"][1]["text"] and "30042" in h["problems"][1]["text"]
        assert h["last_ai_error"]["code"] == 401 and h["last_broker_error"]["code"] == "30042" and h["last_broker_error"]["stale"] is False
        ai_v5.last_error = lambda: {"ts": now - 30, "route": "chat", "code": None, "text": "DeepSeek не ответил (таймаут)", "stale": False}
        tinkoff.last_ok_ts = lambda: now - 5                     # биржа уже ответила после ошибки → ошибка старая
        h = health_block(keys=ks_ok, mission_snap={}, market=None, now=now)
        kinds = [(x["kind"], x["level"]) for x in h["problems"]]
        assert kinds == [("deepseek", "warn")], kinds
        assert h["last_broker_error"]["stale"] is True and h["last_broker_error"]["text"].startswith("30042")
        ai_v5.last_error = lambda: {"ts": now - 30, "route": "chat", "code": 429, "text": "лимит (429)", "stale": True}
        h = health_block(keys=ks_ok, mission_snap={}, market=None, now=now)
        assert h["problems"] == [] and h["last_ai_error"]["stale"] is True, h
        ai_v5.last_error = lambda: None
        tinkoff.last_error = lambda: None
        # 4) рынок закрыт → info с временем открытия; часы споткнулись → warn; часы выключены → тишина
        h = health_block(keys=ks_ok, mission_snap={}, now=now,
                         market={"open": False, "reason": "выходной — торгов нет", "next_open_msk": "пн 10:00", "enabled": True})
        assert [(x["kind"], x["level"]) for x in h["problems"]] == [("market", "info")] and "до пн 10:00" in h["problems"][0]["text"]
        h = health_block(keys=ks_ok, mission_snap={}, now=now, market={"open": True, "reason": "часы споткнулись: TimeoutError", "enabled": True})
        assert [(x["kind"], x["level"]) for x in h["problems"]] == [("market", "warn")] and "TimeoutError" in h["problems"][0]["text"]
        h = health_block(keys=ks_ok, mission_snap={}, now=now, market={"open": False, "reason": "x", "enabled": False})
        assert h["problems"] == []
        # 5) небо: lite → info, precise → тишина, сбой peek_context → astro_mode None и ничего не падает
        astro.peek_context = lambda: ({"mode": "lite"}, 5.0)
        h = health_block(keys=ks_ok, mission_snap={}, market=None, now=now)
        assert h["astro_mode"] == "lite" and [x["kind"] for x in h["problems"]] == ["astro"] and "skyfield" in h["problems"][0]["text"]
        astro.peek_context = lambda: ({"mode": "precise"}, 5.0)
        assert health_block(keys=ks_ok, mission_snap={}, market=None, now=now)["problems"] == []

        def _boom():
            raise RuntimeError("небо упало")
        astro.peek_context = _boom
        assert health_block(keys=ks_ok, mission_snap={}, market=None, now=now)["astro_mode"] is None
        # 6) пилот: трос не встал, закрытие отбито, killswitch, бэкофф отказов; протухшая цена при открытом рынке; state_age_s
        pst = {"position": {"side": "long", "lots": 8, "stop_err": "30079: инструмент недоступен для торгов", "stop_id": None,
                            "close_fail": "закрытие отбито"},
               "killswitch": {"locked": True, "reason": "дневной лимит −3 000 ₽"}, "entry_fail": 3, "no_entry_in_s": 12,
               "price_ts": now - 200, "tick_ts": now - 7, "market_alive": True, "plan": None}
        snap = {"active": "SBER", "missions": {"SBER": {"ticker": "SBER", "live": True, "pilot": pst, "error": None}}}
        h = health_block(keys=ks_ok, mission_snap=snap, market={"open": True, "reason": "торги идут", "enabled": True}, now=now)
        kinds = [(x["kind"], x["level"]) for x in h["problems"]]
        assert kinds == [("pilot", "err"), ("pilot", "err"), ("pilot", "err"), ("pilot", "warn"), ("data", "warn")], kinds
        txt = " | ".join(x["text"] for x in h["problems"])
        assert "30079" in txt and "закрой руками" in txt and "Killswitch SBER" in txt and "3 подряд" in txt and "200 с" in txt, txt
        assert h["state_age_s"] == 7.0, h["state_age_s"]
        #    рынок закрыт → протухшая цена не проблема; стакан протух при открытом рынке в позиции → warn «мёртв»
        h = health_block(keys=ks_ok, mission_snap=snap, market={"open": False, "reason": "ночь", "enabled": True}, now=now)
        assert not [x for x in h["problems"] if x["kind"] == "data"], h["problems"]
        pst2 = {"position": {"side": "long", "lots": 8, "stop_id": "S1"}, "killswitch": {"locked": False}, "entry_fail": 0,
                "no_entry_in_s": None, "price_ts": now - 3, "tick_ts": now - 1, "market_alive": False, "book_age_s": 400.0}
        snap2 = {"active": "SBER", "missions": {"SBER": {"ticker": "SBER", "live": True, "pilot": pst2, "error": None}}}
        h = health_block(keys=ks_ok, mission_snap=snap2, market={"open": True, "enabled": True}, now=now)
        assert [(x["kind"], x["level"]) for x in h["problems"]] == [("data", "warn")] and "400 с" in h["problems"][0]["text"], h
        #    миссия с ошибкой (пилот упал) → err; сломанный снимок миссии → блок не падает
        snap3 = {"active": "SBER", "missions": {"SBER": {"ticker": "SBER", "live": False, "pilot": None, "error": "пилот упал: KeyError"}}}
        h = health_block(keys=ks_ok, mission_snap=snap3, market=None, now=now)
        assert [(x["kind"], x["level"]) for x in h["problems"]] == [("pilot", "err")] and "KeyError" in h["problems"][0]["text"]
        h = health_block(keys=ks_ok, mission_snap={"active": "X", "missions": "мусор"}, market="мусор", now=now)
        assert isinstance(h["problems"], list)
        # 7) мок-режим: ключи не требуются — info вместо err
        os.environ["PYTHIA_MOCK_AI"] = "1"
        os.environ["PYTHIA_MOCK_TINKOFF"] = "1"
        h = health_block(keys={"deepseek": False, "tinkoff": False, "dry": False}, mission_snap={}, market=None, now=now)
        assert [(x["kind"], x["level"]) for x in h["problems"]] == [("keys", "info"), ("keys", "info")], h["problems"]
        os.environ.pop("PYTHIA_MOCK_AI"); os.environ.pop("PYTHIA_MOCK_TINKOFF")
        # 8) /api/v5/state отдаёт health и не падает, даже если health_block сломан; часы — фейк (без сети)
        astro.peek_context = lambda: ({"mode": "precise"}, 5.0)

        async def _fake_market(snap):
            return {"open": True, "reason": "торги идут", "enabled": True}
        _mk0 = globals()["_market_state"]
        globals()["_market_state"] = _fake_market
        try:
            st = asyncio.run(api_state())
            assert "health" in st and isinstance(st["health"]["problems"], list) and st["market"]["open"] is True
            assert st["health"]["astro_mode"] == "precise"
            _hb0 = globals()["health_block"]

            def _hb_boom(**kw):
                raise RuntimeError("health сломан")
            globals()["health_block"] = _hb_boom
            st = asyncio.run(api_state())
            assert st["health"] == {"problems": [], "last_ai_error": None, "last_broker_error": None, "astro_mode": None,
                                    "ws_clients": None, "state_age_s": None}, st["health"]
            globals()["health_block"] = _hb0
        finally:
            globals()["_market_state"] = _mk0
        # 9) v5.4.4: источники Tinkoff (котировки/заявки/счёт) — удачная цена не прячет отказ заявок; токен/права — err
        #    с подсказкой; пилот без связи — err «нет доступа» с позицией без защиты, без ложных «рынок мёртв» / «старая
        #    цена»; петля жива, но не тикает — err; отказ биржи по закрытию — err с паузой; ключ проблемы для Telegram
        astro.peek_context = lambda: ({"mode": "precise"}, 5.0)
        _errs0 = getattr(tinkoff, "errors", None)
        tinkoff.last_error = lambda: {"ts": now - 5, "path": "GetLastPrices", "source": "market", "kind": "network",
                                      "text": "таймаут", "stale": True}
        tinkoff.last_ok_ts = lambda source=None: now
        tinkoff.errors = lambda: {
            "market": {"ts": now - 50, "path": "GetLastPrices", "source": "market", "kind": "network", "text": "таймаут",
                       "stale": True},
            "orders": {"ts": now - 20, "path": "PostOrder", "source": "orders", "kind": "rights", "code": "40002",
                       "text": "Tinkoff 403 · 40002: Insufficient privileges [PostOrder]", "stale": False},
            "account": {"ts": now - 10, "path": "GetAccounts", "source": "account", "kind": "auth", "code": "40003",
                        "text": "Tinkoff 401 · 40003: Authentication token is missing or invalid [GetAccounts]", "stale": False}}
        h = health_block(keys=ks_ok, mission_snap={}, market={"open": True, "enabled": True}, now=now)
        tk = [x for x in h["problems"] if x["kind"] == "tinkoff"]
        assert [(x["level"], x["key"]) for x in tk] == [("err", "tinkoff:account"), ("err", "tinkoff:orders")], tk
        assert "токен не принят" in tk[0]["text"] and "«Ключи»" in tk[0]["text"] and "· счёт" in tk[0]["text"], tk[0]
        assert "нет прав" in tk[1]["text"] and "40002" in tk[1]["text"] and "· заявки" in tk[1]["text"], tk[1]
        tinkoff.errors = lambda: {}
        tinkoff.last_error = lambda: None
        pst9 = {"position": {"side": "long", "lots": 3, "stop_id": None}, "killswitch": {"locked": False}, "entry_fail": 0,
                "price_ts": now - 400, "tick_ts": now - 400, "market_alive": False, "book_age_s": 500.0,
                "loop_alive": True, "ticking": False, "last_tick_ts": now - 400,
                "feed": {"ok": False, "kind": "auth", "reason": "токен Т-Банка не принят (40003) — выпусти новый",
                         "since": now - 300}, "broker_refusal": None}
        snap9 = {"active": "AFLT", "missions": {"AFLT": {"ticker": "AFLT", "live": True, "pilot": pst9, "error": None}}}
        h = health_block(keys=ks_ok, mission_snap=snap9, market={"open": True, "enabled": True}, now=now)
        txt9 = " | ".join(x["text"] for x in h["problems"])
        assert [(x["kind"], x["level"], x["key"]) for x in h["problems"]] == [("pilot", "err", "pilot:feed")], h["problems"]
        assert "нет доступа" in txt9 and "5 мин" in txt9 and "БЕЗ ЗАЩИТЫ" in txt9 and "long 3 лот" in txt9, txt9
        assert "мёртв" not in txt9 and "старой цене" not in txt9, txt9
        pst9b = dict(pst9, feed={"ok": True, "kind": "ok", "reason": "", "since": None}, ticking=False)
        snap9b = {"active": "AFLT", "missions": {"AFLT": {"ticker": "AFLT", "live": True, "pilot": pst9b, "error": None}}}
        h = health_block(keys=ks_ok, mission_snap=snap9b, market={"open": True, "enabled": True}, now=now)
        assert [(x["key"], x["level"]) for x in h["problems"]] == [("pilot:ticking", "err")], h["problems"]
        assert "не тикает 400 с" in h["problems"][0]["text"], h["problems"][0]
        pst9c = dict(pst9b, ticking=True, price_ts=now - 1, market_alive=True,
                     broker_refusal={"what": "close", "code": "30042", "text": "недостаточно средств (30042)", "count": 3,
                                     "ts": now - 30, "kind": "other"},
                     position={"side": "long", "lots": 3, "stop_id": None, "close_fail": "приказ совета: закрыть"})
        snap9c = {"active": "AFLT", "missions": {"AFLT": {"ticker": "AFLT", "live": True, "pilot": pst9c, "error": None}}}
        h = health_block(keys=ks_ok, mission_snap=snap9c, market={"open": True, "enabled": True}, now=now)
        keys9 = [(x["key"], x["level"]) for x in h["problems"]]
        assert keys9 == [("pilot:refusal:close", "err"), ("pilot:close", "err")], keys9
        assert "3 подряд" in h["problems"][0]["text"] and "с паузой" in h["problems"][1]["text"], h["problems"]
        # «Ключи»: новый токен — прошлые ошибки сброшены, проверка GetAccounts в ответе (фейки, без сети и без записи ключей)
        _sm, _chk, _rst, _acl = config.set_many, getattr(tinkoff, "check_access", None), getattr(tinkoff, "reset_errors", None), tinkoff.aclose
        seen9: list = []
        config.set_many = lambda upd: seen9.append(("set", sorted(upd)))

        async def _fake_check(timeout=15.0):
            seen9.append(("check",))
            return {"ok": True, "trade": False, "kind": "rights", "reason": "токен принят, но только для чтения",
                    "access": "READ_ONLY", "ts": now}

        async def _fake_aclose():
            seen9.append(("aclose",))
        tinkoff.check_access, tinkoff.reset_errors, tinkoff.aclose = _fake_check, (lambda: seen9.append(("reset",))), _fake_aclose
        try:
            r9 = asyncio.run(api_keys_set({"tinkoff": "t.fake-token-for-selftest"}))
        finally:
            config.set_many, tinkoff.aclose = _sm, _acl
            if _chk is not None:
                tinkoff.check_access = _chk
            if _rst is not None:
                tinkoff.reset_errors = _rst
        assert r9["ok"] and "tinkoff" in r9["changed"] and r9["tinkoff_check"]["access"] == "READ_ONLY", r9
        assert r9["tinkoff_check"]["ok"] and r9["tinkoff_check"]["trade"] is False, r9["tinkoff_check"]
        assert [x[0] for x in seen9] == ["set", "aclose", "reset", "check"], seen9
        if _errs0 is not None:
            tinkoff.errors = _errs0
    finally:
        ai_v5.last_error, tinkoff.last_error, tinkoff.last_ok_ts, astro.peek_context, ai.last_error = _saved
    print("api_v5 self-test OK: health — ключи (err/warn/info, мок), ошибки DeepSeek/Tinkoff со старением, рынок закрыт/часы, "
          "небо lite/precise/сбой, пилот (трос, закрытие, killswitch, бэкофф), протухшая цена/стакан, state_age_s, "
          "мусорный снимок и сломанный блок не роняют /api/v5/state; v5.4.4: Tinkoff по источникам (котировки/заявки/счёт), "
          "токен/права — err с подсказкой, пилот без связи — err без «рынок мёртв», не тикает — err, отказ закрытия — err, "
          "новый токен в «Ключах» — сброс ошибок и проверка GetAccounts")
