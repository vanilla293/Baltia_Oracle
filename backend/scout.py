# -*- coding: utf-8 -*-
"""ПИФИЯ v5.2 — РАЗВЕДКА ДАННЫХ (FLASH перед советом и миссией).

Идея владельца: прежде чем PRO сядет думать, пусть FLASH быстро пройдётся по тому,
что PRO получит, и спросит себя: каких данных не хватает? Рынок в целом — фьючерс на
индекс (MX/RI) у Tinkoff, курс доллара/юаня, нефть, золото, соседи по сектору, лидер
рынка. Индексы MOEX ISS (IMOEX, RTSI…) не используются вовсе — воля владельца: «данные
нетрезвые, отстают надолго»; акции без токена — MOEX ISS, индексные фьючерсы — только
Tinkoff (без токена честно «нет данных»). FLASH возвращает список запросов, код тянет
данные и отдаёт PRO блоком «ДАННЫЕ ПО ЗАПРОСУ РАЗВЕДКИ». Связанные бумаги (v5.3,
correlate.partners) добавляются к запросам сами: котировка и история 5 дней по каждому
партнёру — свежие числа при каждой перепроверке/чате через refetch. Никаких выдумок:
нет данных — строка честно говорит «нет данных».

    catalog_text()                       — что можно попросить (для промпта FLASH)
    async plan(kind, brief, ticker, partner_codes=()) — FLASH → [{"kind","code","days","interval","why"}]
    async partner_requests(ticker, asset_class=None) — quote + history 5d по связанным бумагам
    async fetch(requests)                — данные → текст блока
    async run(scope, run_id, kind, brief, ticker=None) -> (text, requests)
                                         — стадия шины «scout»: партнёры → план → сбор → текст;
                                           любой сбой → ("", []) — этап не падает
    async refetch(requests)              — те же запросы свежими данными (перепроверка, чат)

Self-тест (без сети): python3 -m backend.scout
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from . import ai_v5, bus, config, correlate, instruments, moex, tinkoff

log = logging.getLogger("pythia.scout")

PLAN_TIMEOUT = 120.0            # FLASH обязан ответить за N с
FETCH_TIMEOUT = 25.0           # весь сбор данных — не дольше N с
ONE_TIMEOUT = 10.0             # один запрос к бирже
MAX_DAYS = 90
KINDS = ("quote", "history")
PARTNERS_TIMEOUT = 40.0        # связанные бумаги (обычно из кэша correlate — мгновенно)

# индексы MOEX ISS — НЕ используются (воля владельца: данные отстают); код честно скажет об этом,
# а «рынок в целом» смотрим по фьючерсу на индекс у Tinkoff (MX — MIX, RI — RTS)
ISS_INDEXES = {
    "IMOEX": "Индекс МосБиржи", "IMOEX2": "Индекс МосБиржи (вечерняя сессия)", "RTSI": "Индекс РТС",
    "MOEXBC": "Индекс голубых фишек", "MOEXBMI": "Индекс широкого рынка", "RGBI": "Индекс гособлигаций RGBI",
    "MOEXOG": "Индекс нефти и газа", "MOEXFN": "Индекс финансов", "MOEXMM": "Индекс металлов и добычи",
    "MOEXEU": "Индекс электроэнергетики", "MOEXTL": "Индекс телекоммуникаций", "MOEXCN": "Индекс потребсектора",
    "MOEXIT": "Индекс информационных технологий", "MOEXRE": "Индекс строительных компаний",
    "MOEXTN": "Индекс транспорта", "MOEXCH": "Индекс химии и нефтехимии", "RVI": "Индекс волатильности RVI",
}
INDEX_FUTURES = {"MX": "фьючерс на индекс МосБиржи (MIX)", "MM": "фьючерс MXI (мини)", "RI": "фьючерс на индекс РТС (RTS)"}
# ходовые синонимы, которые ИИ называет словами; индекс → фьючерс на индекс
ALIASES = {
    "ИНДЕКС": "MX", "МОСБИРЖА": "MX", "IMOEX": "MX", "IMOEX2": "MX", "MOEX_INDEX": "MX", "MICEX": "MX",
    "MIX": "MX", "MXI": "MM", "RTS": "RI", "РТС": "RI", "RTSI": "RI", "RTS_FUT": "RI",
    "USDRUB": "SI", "USD/RUB": "SI", "ДОЛЛАР": "SI", "USD": "SI",
    "CNYRUB": "CR", "CNY/RUB": "CR", "ЮАНЬ": "CR", "CNY": "CR", "EURRUB": "EU", "EUR/RUB": "EU", "ЕВРО": "EU",
    "BRENT": "BR", "НЕФТЬ": "BR", "OIL": "BR", "GOLD": "GD", "ЗОЛОТО": "GD", "SILVER": "SV", "СЕРЕБРО": "SV",
    "NATGAS": "NG", "ГАЗ": "NG", "TCS": "T", "TCSG": "T", "YNDX": "YDEX",
}


def _f(x, d=None):
    try:
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


def _fmt(v) -> str:
    v = _f(v)
    if v is None:
        return "н/д"
    a = abs(v)
    if a >= 1000:
        return f"{v:,.1f}".replace(",", " ")
    if a >= 100:
        return f"{v:.2f}"
    return f"{v:.4g}"


def norm_code(code: str) -> tuple[str, str, str]:
    """(код, класс, имя): индекс → фьючерс на индекс (MX/RI, 'futures'); индексы MOEX ISS,
    у которых фьючерса нет, → 'index' (данных не будет — честная строка); каталог/вселенная →
    их класс, иначе догадка."""
    c = str(code or "").upper().strip().strip("[]").replace(" ", "")
    if not c:
        return "", "", ""
    c = ALIASES.get(c, c)
    if c in ISS_INDEXES:
        return c, "index", ISS_INDEXES[c]
    try:
        from . import newsflow as _nf
        c2 = _nf.norm_ticker(c)
        if c2:
            c = c2
    except Exception:                                    # noqa: BLE001
        pass
    if c in INDEX_FUTURES:
        it = instruments.get(c) or {}
        return c, "futures", it.get("name") or INDEX_FUTURES[c]
    it = instruments.get(c) or {}
    if it:
        return c, it.get("asset_class") or "share", it.get("name") or c
    return c, instruments.guess_asset_class(c), c


def catalog_text() -> str:
    """Что разведка умеет достать — коротко, для промпта FLASH."""
    cat = ", ".join(f"{it['code']} ({it['name']})" for it in instruments.CATALOG[:12])
    return ("Можно запросить (kind):\n"
            "- quote — цена сейчас и изменение за день: любой тикер Мосбиржи (акции: SBER, GAZP, LKOH… "
            "фьючерсы по базовому коду: BR нефть, SI доллар, CR юань, GD золото, NG газ; рынок в целом — "
            "фьючерс на индекс: MX (индекс МосБиржи, MIX), RI (индекс РТС)). Индексы MOEX ISS (IMOEX, RTSI, "
            "отраслевые) не используются — их данные отстают; вместо IMOEX проси MX;\n"
            "- history — закрытия за N дней (days 2…90, interval 1d; или часы: interval 1h, days 1…14): "
            "итоговое изменение, мин/макс, последние точки — чтобы видеть, где рынок был неделю или месяц назад.\n"
            f"Каталог кодов (часть): {cat}; акции — тикер Мосбиржи. Данные: Tinkoff (если есть токен), "
            "акции без токена — MOEX ISS.")


def _plan_prompt(kind: str, brief: str, ticker: str | None, partner_codes: tuple = ()) -> tuple[str, str]:
    what = {"council": "совет трейдеров по всему рынку (новости → анализ → критика → вердикт)",
            "mission": f"совет по инструменту {ticker or ''} и приказ пилоту",
            "review": f"перепроверка дежурного PRO по {ticker or ''}",
            "chat": "ответ на вопрос человека в чате панели"}.get(kind, kind)
    system = (
        "Ты — разведчик данных перед работой PRO-аналитика на Мосбирже. Сейчас "
        f"{ai_v5.now_msk_str()}.\n"
        f"Дальше будет: {what}. Ниже — краткое описание того, что PRO уже получит, и каталог данных, "
        "которые можно запросить дополнительно (индексы, соседние бумаги, курсы, сырьё, история цены).\n"
        "Подумай, каких данных не хватает, чтобы разбор был точнее: где рынок в целом (фьючерс на индекс MX "
        "сейчас и за неделю/месяц), сектор и соседи инструмента, курс и нефть для экспортёров, лидер сектора, — и "
        f"запроси только нужное, не больше {int(getattr(config, 'PYTHIA_SCOUT_MAX', 12))} запросов. "
        "Ничего не нужно — верни пустой список.\n"
        'Верни строго один JSON-объект: {"requests":[{"kind":"quote|history","code":"MX","days":10,'
        '"interval":"1d","why":"зачем"}],"note":"1 фраза"}')
    user = (f"сейчас: {ai_v5.now_msk_str()}\n\nЧТО УЖЕ ПОЛУЧИТ PRO:\n{(brief or '').strip() or '(описания нет)'}\n\n"
            + (f"СВЯЗАННЫЕ БУМАГИ уже добавлены автоматически (котировка и 5 дней): {', '.join(partner_codes)} — "
               "их заново не проси.\n\n" if partner_codes else "")
            + f"КАТАЛОГ ДАННЫХ:\n{catalog_text()}")
    return system, user


def _clean_requests(obj) -> list[dict]:
    reqs = (obj or {}).get("requests") if isinstance(obj, dict) else obj
    out: list[dict] = []
    seen: set[str] = set()
    limit = int(getattr(config, "PYTHIA_SCOUT_MAX", 12))
    for r in reqs or []:
        if not isinstance(r, dict):
            continue
        kind = str(r.get("kind") or "quote").lower().strip()
        if kind not in KINDS:
            kind = "history" if r.get("days") else "quote"
        code, ac, name = norm_code(str(r.get("code") or r.get("ticker") or ""))
        if not code:
            continue
        days = int(_f(r.get("days"), 10) or 10)
        interval = str(r.get("interval") or "1d").lower()
        if interval not in ("1d", "1h"):
            interval = "1d"
        days = max(2, min(days, MAX_DAYS if interval == "1d" else 14))
        key = f"{kind}:{code}:{interval if kind == 'history' else ''}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": kind, "code": code, "asset_class": ac, "name": name, "days": days,
                    "interval": interval, "why": str(r.get("why") or "")[:120]})
        if len(out) >= limit:
            break
    return out


async def plan(kind: str, brief: str, ticker: str | None = None,
               partner_codes: tuple = ()) -> list[dict] | None:
    """FLASH решает, что запросить. [] — ничего не нужно; None — FLASH не ответил
    (разведка молчит, этап живёт без неё)."""
    if not getattr(config, "PYTHIA_SCOUT", True):
        return []
    try:
        s, u = _plan_prompt(kind, brief, ticker, partner_codes)
        obj = await asyncio.wait_for(ai_v5.flash_json(s, u, route="scout"), PLAN_TIMEOUT)
        return _clean_requests(obj)
    except Exception as e:                                   # noqa: BLE001
        log.info("разведка: план не получен: %s", str(e)[:120])
        return None


async def partner_requests(ticker: str | None, asset_class: str | None = None) -> list[dict]:
    """Связанные бумаги (correlate, кэш 6 ч) → запросы quote + history 5d. Сбой → []."""
    if not ticker or not getattr(config, "PYTHIA_PARTNERS", True):
        return []
    try:
        ac = asset_class or norm_code(ticker)[1] or "share"
        items = await asyncio.wait_for(correlate.partners(ticker, ac), PARTNERS_TIMEOUT)
        return correlate.scout_requests(items)
    except Exception as e:                                   # noqa: BLE001
        log.info("разведка: связанные бумаги %s: %s", ticker, str(e)[:80])
        return []


def _merge(reqs: list[dict], extra: list[dict]) -> list[dict]:
    """Запросы FLASH + партнёры без дублей (ключ kind:code:interval)."""
    out: list[dict] = []
    seen: set[str] = set()
    for r in list(reqs or []) + list(extra or []):
        if not isinstance(r, dict) or not r.get("code"):
            continue
        key = f"{r.get('kind')}:{r['code']}:{r.get('interval') if r.get('kind') == 'history' else ''}"
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


# ── сбор ───────────────────────────────────────────────────────────────────────
def _no_data_reason(code: str, ac: str) -> str | None:
    """Почему данных не будет ещё до похода в сеть: индекс ISS или индексный фьючерс без токена."""
    if ac == "index":
        return "индексы MOEX ISS не используются (данные отстают) — смотри фьючерс MX/RI"
    if ac == "futures" and code in INDEX_FUTURES and not tinkoff.enabled():
        return "индексных данных нет: фьючерс на индекс только через Tinkoff, токена нет"
    return None


async def _figi(code: str, ac: str) -> str | None:
    if not tinkoff.enabled() or ac == "index":
        return None
    try:
        inst = await tinkoff.resolve(code, ac)
        return (inst or {}).get("figi") or (inst or {}).get("uid")
    except Exception:                                        # noqa: BLE001
        return None


async def _quote(code: str, ac: str) -> dict | None:
    """Цена сейчас и изменение за день: Tinkoff → MOEX ISS (акции, сырьё/валюта; индексы — нет)."""
    if _no_data_reason(code, ac):
        return None
    figi = await _figi(code, ac)
    if figi:
        try:
            lp, cp = await asyncio.gather(tinkoff.last_price(figi), tinkoff.close_price(figi),
                                          return_exceptions=True)
            px = _f((lp or {}).get("price")) if isinstance(lp, dict) else None
            if px:
                prev = _f(cp) if isinstance(cp, (int, float)) else None
                return {"price": px, "change_pct": round((px - prev) / prev * 100, 2) if prev else None,
                        "source": "tinkoff", "stale": False}
        except Exception:                                    # noqa: BLE001
            pass
    try:
        mp = await moex.last_price(code, ac)
    except Exception:                                        # noqa: BLE001
        mp = None
    if mp and _f(mp.get("price")):
        return {"price": _f(mp["price"]), "change_pct": _f(mp.get("change_pct")),
                "source": "moex", "stale": bool(mp.get("stale")), "time": mp.get("quote_time")}
    return None


async def _history(code: str, ac: str, interval: str, days: int) -> dict | None:
    """Закрытия: Tinkoff → MOEX ISS (индексы — нет). Итог: первое/последнее, мин/макс, точки."""
    rows: list[dict] = []
    if _no_data_reason(code, ac):
        return None
    figi = await _figi(code, ac)
    if figi:
        try:
            rows = await tinkoff.candles(figi, interval, days)
        except Exception:                                    # noqa: BLE001
            rows = []
    src = "tinkoff" if rows else ""
    if not rows:
        try:
            rows = await moex.candles(code, ac, interval, days)
            src = "moex" if rows else ""
        except Exception:                                    # noqa: BLE001
            rows = []
    closes = [(r.get("t"), _f(r.get("c"))) for r in rows if _f(r.get("c"))]
    if len(closes) < 2:
        return None
    vals = [c for _, c in closes]
    first, last = vals[0], vals[-1]
    hi, lo = max(vals), min(vals)
    n_pts = 6 if interval == "1d" else 8
    step = max(1, len(closes) // n_pts)
    pts = closes[::step][-n_pts:]
    if pts[-1] is not closes[-1]:
        pts = pts[:-1] + [closes[-1]] if len(pts) >= n_pts else pts + [closes[-1]]
    return {"first": first, "last": last, "hi": hi, "lo": lo, "n": len(closes),
            "change_pct": round((last - first) / first * 100, 2) if first else None,
            "from": closes[0][0], "to": closes[-1][0], "points": pts, "source": src}


def _short_t(t: str | None, interval: str) -> str:
    try:
        dt = datetime.fromisoformat(str(t).replace("Z", "+00:00")).astimezone(ai_v5.MSK)
        return dt.strftime("%d.%m %H:%M" if interval == "1h" else "%d.%m")
    except Exception:                                        # noqa: BLE001
        return str(t or "")[:10]


async def _one(req: dict) -> str:
    code, ac, name = req["code"], req.get("asset_class") or "share", req.get("name") or req["code"]
    label = f"{code} ({name})" if name and name != code else code
    try:
        reason = _no_data_reason(code, ac)
        if reason:
            return f"{label}: нет данных — {reason}"
        if req["kind"] == "quote":
            q = await asyncio.wait_for(_quote(code, ac), ONE_TIMEOUT)
            if not q:
                return f"{label}: нет данных (биржа не отдала котировку)"
            ch = q.get("change_pct")
            return (f"{label}: {_fmt(q['price'])}" + (f" ({ch:+.2f}% за день)" if isinstance(ch, (int, float)) else "")
                    + (" · закрытие" if q.get("stale") else "") + f" · {q.get('source')}")
        h = await asyncio.wait_for(_history(code, ac, req["interval"], req["days"]), ONE_TIMEOUT)
        if not h:
            return f"{label}: история за {req['days']} дн. недоступна"
        iv = req["interval"]
        pts = " → ".join(f"{_short_t(t, iv)} {_fmt(c)}" for t, c in h["points"])
        return (f"{label}, {req['days']} дн. ({'дни' if iv == '1d' else 'часы'}, {h['n']} точек): "
                f"{_fmt(h['first'])} → {_fmt(h['last'])} ({h['change_pct']:+.2f}%), мин {_fmt(h['lo'])}, "
                f"макс {_fmt(h['hi'])} · {pts} · {h['source']}")
    except asyncio.TimeoutError:
        return f"{label}: биржа не ответила вовремя"
    except Exception as e:                                   # noqa: BLE001
        return f"{label}: сбой ({str(e)[:60]})"


async def fetch(requests: list[dict]) -> str:
    """Собрать все запросы параллельно (общий предел FETCH_TIMEOUT) → строки блока."""
    reqs = [r for r in (requests or []) if isinstance(r, dict) and r.get("code")]
    if not reqs:
        return ""
    lines: list[str] = []
    try:
        res = await asyncio.wait_for(asyncio.gather(*(_one(r) for r in reqs), return_exceptions=True),
                                     FETCH_TIMEOUT)
    except asyncio.TimeoutError:
        return "разведка: биржа не ответила вовремя — данных нет"
    for r, x in zip(reqs, res):
        why = f" — {r['why']}" if r.get("why") else ""
        lines.append((str(x) if not isinstance(x, Exception) else f"{r['code']}: сбой") + why)
    n_p = sum(1 for r in reqs if str(r.get("why") or "").startswith("связанная бумага"))
    head = (f"Разведка FLASH ({ai_v5.now_msk_str()}): {len(reqs)} запрос(ов)"
            + (f", из них по связанным бумагам {n_p}" if n_p else ""))
    return head + "\n" + "\n".join("- " + ln for ln in lines)


async def refetch(requests: list[dict]) -> str:
    """Те же запросы, свежие числа (перепроверка, чат) — без нового вопроса FLASH."""
    try:
        return await fetch(requests)
    except Exception as e:                                   # noqa: BLE001
        log.info("разведка: повторный сбор: %s", str(e)[:100])
        return ""


async def run(scope: str, run_id: str | None, kind: str, brief: str,
              ticker: str | None = None) -> tuple[str, list[dict]]:
    """Стадия шины «scout»: связанные бумаги (correlate) → FLASH планирует → код собирает →
    текст блока для PRO. Любой сбой → ("", []) и стадия error; этап продолжается без разведки."""
    if not getattr(config, "PYTHIA_SCOUT", True):
        return "", []
    t0 = time.time()
    try:
        if run_id:
            await bus.stage(scope, run_id, "scout", "start", ticker=ticker,
                            detail="FLASH решает, каких данных не хватает (индексный фьючерс, соседи, курсы)")
        preqs = await partner_requests(ticker)
        pcodes = tuple(dict.fromkeys(r["code"] for r in preqs))
        reqs = await plan(kind, brief, ticker, pcodes)
        if reqs is None and not preqs:
            if run_id:
                await bus.stage(scope, run_id, "scout", "error", ticker=ticker,
                                detail="FLASH не ответил — этап идёт без разведки")
            return "", []
        reqs = _merge(reqs or [], preqs)
        if not reqs:
            if run_id:
                await bus.stage(scope, run_id, "scout", "done", ticker=ticker, n=0,
                                detail="FLASH: дополнительных данных не нужно")
            return "", []
        if run_id:
            await bus.stage(scope, run_id, "scout", "progress", ticker=ticker, n=len(reqs),
                            detail="запросы: " + ", ".join(
                                f"{r['code']}{' ' + str(r['days']) + 'д' if r['kind'] == 'history' else ''}"
                                for r in reqs if r not in preqs)
                            + (f" + связанные бумаги: {', '.join(pcodes)}" if pcodes else ""))
        text = await fetch(reqs)
        if run_id:
            await bus.stage(scope, run_id, "scout", "done", ticker=ticker, n=len(reqs),
                            detail=f"собрано {len(reqs)} за {time.time() - t0:.0f} с: " + ", ".join(
                                r["code"] for r in reqs), data={"requests": reqs})
        return text, reqs
    except Exception as e:                                   # noqa: BLE001
        log.warning("разведка (%s): %s", kind, str(e)[:120])
        if run_id:
            try:
                await bus.stage(scope, run_id, "scout", "error", ticker=ticker, detail=str(e)[:160])
            except Exception:                                # noqa: BLE001
                pass
        return "", []


# ── self-test ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json

    calls: list = []

    class FakeAI:
        now_msk_str = staticmethod(ai_v5.now_msk_str)
        MSK = ai_v5.MSK
        answer: dict = {"requests": [{"kind": "quote", "code": "imoex", "why": "рынок в целом"},
                                     {"kind": "history", "code": "Индекс", "days": 200, "why": "неделя"},
                                     {"kind": "quote", "code": "нефть"}, {"kind": "history", "code": "SBER", "days": 5,
                                                                          "interval": "1h"},
                                     {"kind": "quote", "code": ""}, {"kind": "quote", "code": "IMOEX"},
                                     {"kind": "quote", "code": "RGBI", "why": "ОФЗ"}],
                        "note": "ок"}
        last_user = ""

        async def flash_json(self, system, user, *, think=False, route="flash", max_tokens=None):
            calls.append(route)
            FakeAI.last_user = user
            assert "КАТАЛОГ ДАННЫХ" in user and "MX" in user and "ЧТО УЖЕ ПОЛУЧИТ PRO" in user
            assert "IMOEX" not in system and "не используются" in user, "промпт не зовёт индексы ISS"
            return dict(self.answer)

    ai_v5 = FakeAI()  # noqa: F811

    async def fake_moex_price(code, ac):
        assert ac != "index" and code not in ("MX", "RI", "MM"), (code, ac)   # индексы через ISS не ходят
        if code == "BR":
            return {"price": 71.4, "change_pct": -1.8, "stale": True}
        if code == "VTBR":
            return {"price": 95.5, "change_pct": 1.2, "stale": False}
        return None

    async def fake_moex_candles(code, ac, interval, days):
        assert ac != "index" and code not in ("MX", "RI", "MM"), (code, ac)
        if code == "SBER":
            return [{"t": f"2026-09-21T{h:02d}:00:00Z", "c": 285 + h * 0.1} for h in range(7, 19)]
        if code == "VTBR":
            return [{"t": f"2026-09-{d:02d}T07:00:00Z", "c": 90 + d * 0.5} for d in range(15, 22)]
        return []

    moex.last_price, moex.candles = fake_moex_price, fake_moex_candles
    tinkoff.config.TINKOFF_TOKEN = ""

    partner_calls: list = []

    async def fake_partners(ticker, ac, days=60, *, force=False):
        partner_calls.append((ticker, ac))
        if ticker == "SBER":
            return [{"code": "VTBR", "name": "ВТБ", "asset_class": "share", "rho": 0.81, "lead": 0}]
        return []

    correlate.partners = fake_partners

    events: list = []

    async def sink(ev):
        events.append(ev)

    bus.set_sink(sink)

    async def main():
        # ── совет (без тикера): партнёров нет, индексы ISS → фьючерс MX, RGBI → честно нет данных ──
        reqs = await plan("council", "новости 200 строк, цены каталога, астро", None)
        assert [r["code"] for r in reqs] == ["MX", "MX", "BR", "SBER", "RGBI"], reqs   # пустой и дубль выкинуты
        assert reqs[0]["asset_class"] == "futures" and reqs[0]["name"] == "Индекс МосБиржи", reqs[0]
        assert reqs[1]["kind"] == "history" and reqs[1]["days"] == MAX_DAYS, reqs[1]   # 200 → потолок
        assert reqs[2]["code"] == "BR" and reqs[2]["asset_class"] == "futures"
        assert reqs[3]["interval"] == "1h" and reqs[3]["days"] == 5
        assert reqs[4]["asset_class"] == "index", "индекс ISS без фьючерса помечен классом index"
        text = await fetch(reqs)
        assert "MX (Индекс МосБиржи): нет данных — индексных данных нет: фьючерс на индекс только через Tinkoff, токена нет — рынок в целом" in text, text
        assert "RGBI (Индекс гособлигаций RGBI): нет данных — индексы MOEX ISS не используются (данные отстают)" in text, text
        assert "BR (Brent — нефть): 71.4 (-1.80% за день) · закрытие · moex" in text, text
        assert "SBER (Сбербанк), 5 дн. (часы, 12 точек)" in text, text
        rid = bus.start_run("daily")
        t2, r2 = await run("daily", rid, "council", "кратко", None)
        assert t2 == text.split("\n", 1)[0][:9] + t2[9:] and r2 == reqs and calls == ["scout", "scout"]
        assert not partner_calls, "без тикера связанные бумаги не считаются"
        st = [(e["stage"], e["status"]) for e in events if e.get("type") == "v5"]
        assert ("scout", "start") in st and ("scout", "progress") in st and ("scout", "done") in st, st
        assert json.dumps(r2)
        # ── миссия (тикер SBER): партнёры добавлены сами — quote + history 5d, FLASH их не просит заново ──
        t3, r3 = await run("mission", rid, "mission", "SBER цена 285", "SBER")
        assert partner_calls == [("SBER", "share")]
        assert "СВЯЗАННЫЕ БУМАГИ уже добавлены автоматически (котировка и 5 дней): VTBR" in FakeAI.last_user
        pr = [r for r in r3 if r["code"] == "VTBR"]
        assert len(pr) == 2 and {r["kind"] for r in pr} == {"quote", "history"} and pr[1]["days"] == 5, pr
        assert "из них по связанным бумагам 2" in t3.splitlines()[0], t3.splitlines()[0]
        assert "VTBR (ВТБ): 95.5 (+1.20% за день) · moex — связанная бумага, ρ=+0.81" in t3, t3
        assert "VTBR (ВТБ), 5 дн. (дни, 7 точек): 97.5 → 100.50 (+3.08%)" in t3, t3
        prog = [e for e in events if e.get("stage") == "scout" and e.get("status") == "progress"][-1]
        assert "связанные бумаги: VTBR" in prog["detail"], prog["detail"]
        # FLASH молчит, но партнёры есть → блок только из партнёров, стадия done
        async def boom(*a, **k):
            raise RuntimeError("сеть")
        ai_v5.flash_json = boom
        t4, r4 = await run("mission", rid, "mission", "x", "SBER")
        assert [r["code"] for r in r4] == ["VTBR", "VTBR"] and "VTBR (ВТБ): 95.5" in t4, (r4, t4)
        assert events[-1]["stage"] == "scout" and events[-1]["status"] == "done"
        # FLASH молчит и партнёров нет → пусто, стадия error, ничего не падает
        assert await run("daily", rid, "council", "x") == ("", [])
        assert events[-1]["stage"] == "scout" and events[-1]["status"] == "error"
        # партнёры упали → разведка живёт без них
        async def pboom(*a, **k):
            raise RuntimeError("биржа")
        correlate.partners = pboom
        ai_v5.flash_json = FakeAI.flash_json.__get__(ai_v5, FakeAI)
        t5, r5 = await run("mission", rid, "mission", "x", "SBER")
        assert r5 == reqs and "VTBR" not in t5
        correlate.partners = fake_partners
        # выключено конфигом
        config.PYTHIA_SCOUT = False
        assert await plan("council", "x") == [] and await run("daily", rid, "council", "x") == ("", [])
        config.PYTHIA_SCOUT = True
        config.PYTHIA_PARTNERS = False
        assert await partner_requests("SBER") == []
        config.PYTHIA_PARTNERS = True
        # refetch тех же запросов
        assert "BR (Brent — нефть): 71.4" in await refetch(reqs)
        assert await fetch([]) == ""
        # нормализация кодов: индексы → фьючерсы на индекс, ISS-индексы без фьючерса → index
        assert norm_code("ртс")[0] == "RI" and norm_code("ртс")[1] == "futures"
        assert norm_code("IMOEX") == ("MX", "futures", "Индекс МосБиржи") and norm_code("MIX")[0] == "MX"
        assert norm_code("Si")[0] == "SI" and norm_code("sber")[1] == "share"
        assert norm_code("rgbi")[1] == "index" and norm_code("MOEXOG")[1] == "index" and norm_code("BRZ6")[1] == "futures"
        assert _merge([{"kind": "quote", "code": "MX"}], [{"kind": "quote", "code": "MX"}, {"kind": "history", "code": "MX", "interval": "1d"}]) \
            == [{"kind": "quote", "code": "MX"}, {"kind": "history", "code": "MX", "interval": "1d"}]
        bus.end_run(rid)

    asyncio.run(main())
    print("scout self-test OK: план FLASH (индексы ISS → фьючерс MX, дубли, пределы), связанные бумаги в запросах "
          "(quote + 5d, без дублей, FLASH их не просит), сбор quote/history (MOEX/Tinkoff), индексы ISS честно "
          "«нет данных», стадия шины, сбой → пусто, выкл. конфигом, refetch")
