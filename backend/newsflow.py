# -*- coding: utf-8 -*-
"""ПИФИЯ v5 — этап 1, механика новостей (FLASH).

RSS → хранилище → отбор биржевого пачками по TRIAGE_BATCH=50 (остаток — последняя
неровная пачка: 130 → 50, 50, 30) → разметка каждой отобранной новости отдельным
FLASH-чатом с ПОЛНЫМ текстом → группировка в потоки → раскладка выбора совета по
тикерам каталога.
v5.1: плюс ЦЕЛЕВЫЕ новости Google News по инструментам (`collect_targeted` —
по всему каталогу для совета, `enrich_ticker` — по одному тикеру для миссии;
выключатель config.PYTHIA_GNEWS). Ножницами входы ИИ не режем: `render(items)`
отдаёт все строки, лимит — только по просьбе (панель/логи); выше разумных пределов
блоки сжимает FLASH (`compress.fit/shrink` в совете и дозоре). Разметка несёт
ключевые характеристики (`key_facts` — ≤4 факта с числами/датами/именами, `gist`
— только факты): `one_line_rich` показывает их у важных новостей, так PRO видит
всю ленту сжато, а суть важного — без простыни.
Все сетевые/ИИ-сбои ловятся по слоям: одна плохая новость или пачка не роняет
этап. Короткий id новости = первые 6 символов sha1 (`sid`), ИИ видит и
возвращает только его — код мапит обратно.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time

from . import ai, ai_v5, bus, compress, config, instruments, news, prompts_council as P, store_v5

log = logging.getLogger("pythia.newsflow")

TRIAGE_BATCH = 50          # отбор пачками по 50; остаток — последняя неровная пачка
GROUP_LIMIT = 120          # больше → группируем пачками и сливаем
GROUP_BATCH = 100
TARGET_TIMEOUT = 40.0      # с на Google News по одному инструменту
TARGET_PAR = 5             # одновременных запросов к Google News (весь каталог разом → 429)
# группировка/слияние идут БЕЗ размышления: с thinking flash тратил весь лимит на
# reasoning и отдавал пустой ответ (живой прогон 18.09.2026); это механика, не совет

# Коды каталога Пифии ↔ как их называют биржа и ИИ. Совет пишет «MIX», «Si», «RTS» —
# приводим к кодам каталога, иначе карточка/миссия не найдут инструмент.
TICKER_ALIASES = {
    "MIX": "MX", "MXI": "MX", "IMOEX": "MX", "MOEXIDX": "MX", "ИНДЕКС": "MX",
    "RTS": "RI", "RTSI": "RI",
    "USDRUB": "SI", "USDRUB_TOM": "SI", "USD/RUB": "SI", "ДОЛЛАР": "SI",
    "EURRUB": "EU", "EUR/RUB": "EU", "CNYRUB": "CR", "CNY/RUB": "CR", "ЮАНЬ": "CR",
    "BRENT": "BR", "НЕФТЬ": "BR", "GOLD": "GD", "ЗОЛОТО": "GD", "SILV": "SV", "SILVER": "SV",
    "СЕРЕБРО": "SV", "NATGAS": "NG", "ГАЗ": "NG", "TCS": "T", "TCSG": "T", "YNDX": "YDEX",
}
TICKER_HINTS = {
    "MX": "фьючерс на индекс МосБиржи (биржевой код MIX)",
    "RI": "фьючерс на индекс РТС (RTS)", "SI": "фьючерс USD/RUB (Si)",
    "EU": "фьючерс EUR/RUB (Eu)", "CR": "фьючерс CNY/RUB (CNY)",
    "BR": "фьючерс Brent", "NG": "фьючерс природный газ Henry Hub",
    "TTF": "фьючерс газ TTF (Европа)", "GD": "фьючерс золото", "SV": "фьючерс серебро",
}


def norm_ticker(t) -> str:
    """Синоним биржи/ИИ → код каталога (MIX→MX, Si→SI, RTS→RI…)."""
    t = str(t or "").upper().strip().strip("[]").replace(" ", "")
    return TICKER_ALIASES.get(t, t)


def display_name(code: str, name: str = "") -> str:
    """Имя для промптов: у фьючерсов — с пометкой «фьючерс» и биржевым кодом."""
    return TICKER_HINTS.get((code or "").upper(), name or code)

_KINDS = ("факт", "мнение", "слух", "прогноз", "реклама")
_HORIZONS = ("часы", "дни", "недели", "месяцы")
_EFFECTS = ("вверх", "вниз", "нейтрально", "неясно")
_AI_KEYS = ("gist", "one_liner", "tone", "honesty", "hype", "importance", "novelty", "kind",
            "horizon", "expected_effect", "effect_strength", "manipulation_risk", "actors",
            "assets", "sectors", "tags", "why_market", "key_facts")
RICH_IMPORTANCE = 60       # one_line_rich: факты и суть показываем от этой важности…
RICH_EFFECT = 50           # …или от этой силы ожидаемого эффекта
KEY_FACTS_MAX = 4          # ключевых фактов в разметке
KEY_FACT_LEN = 90          # симв. на один факт

# ключевые слова инструментов для news_for_ticker (фьючерсы и курируемые акции)
_TICKER_KW: dict[str, list[str]] = {
    "BR": ["нефть", "нефт", "brent", "брент", "опек", "opec", "urals", "баррел"],
    "NG": ["газ", "henry hub", "спг", "lng"],
    "TTF": ["газ", "ttf", "спг", "европ"],
    "SI": ["доллар", "рубл", "курс", "usd", "валют"],
    "EU": ["евро", "рубл", "курс", "eur"],
    "CR": ["юан", "cny", "рубл", "курс"],
    "GD": ["золот", "gold"],
    "SV": ["серебр", "silver"],
    "MX": ["индекс", "мосбирж", "imoex", "рынок акций"],
    "RI": ["индекс", "ртс", "rts", "мосбирж"],
    "SBER": ["сбер"], "GAZP": ["газпром"], "LKOH": ["лукойл"], "ROSN": ["роснефт"],
    "NVTK": ["новатэк", "новатек"], "GMKN": ["норникел", "норильск"], "TATN": ["татнефт"],
    "PLZL": ["полюс"], "YDEX": ["яндекс"], "T": ["т-технолог", "тинькофф", "т-банк"],
    "VTBR": ["втб"], "MOEX": ["мосбирж", "московская бирж"], "OZON": ["ozon", "озон"],
    "MGNT": ["магнит"], "CHMF": ["северстал"], "ALRS": ["алроса"],
}


# ── помощники ─────────────────────────────────────────────────────────────
def sid(full_id: str) -> str:
    return (full_id or "")[:6]


def scope_of(run_id: str) -> str:
    return (run_id or "daily").split("-", 1)[0] or "daily"


def _hum(e: Exception) -> str:
    try:
        return ai.humanize_error(e)
    except Exception:            # noqa: BLE001
        return str(e)[:200]


def _int(v, lo: int, hi: int, default: int = 0) -> int:
    try:
        x = int(round(float(str(v).replace("%", "").replace("+", "").strip())))
    except Exception:            # noqa: BLE001
        return default
    return max(lo, min(hi, x))


def _enum(v, allowed: tuple, default: str) -> str:
    s = str(v or "").strip().lower()
    for a in allowed:
        if s.startswith(a[:4]):
            return a
    return default


def _strs(v, limit: int = 8, up: bool = False) -> list[str]:
    if isinstance(v, str):
        v = [x for x in re.split(r"[,;]", v)]
    out = []
    for x in (v or [])[:limit]:
        s = str(x).strip()
        if s:
            out.append(s.upper() if up and s.isascii() else s)
    return out


def _facts(v) -> list[str]:
    """key_facts: только список строк, ≤KEY_FACTS_MAX штук по ≤KEY_FACT_LEN симв.;
    строка вместо списка, None, мусор → []."""
    if not isinstance(v, (list, tuple)):
        return []
    out: list[str] = []
    for x in v:
        if isinstance(x, (dict, list, tuple)):
            continue
        s = re.sub(r"\s+", " ", str(x if x is not None else "")).strip(" ;·")
        if s:
            out.append(s[:KEY_FACT_LEN])
        if len(out) >= KEY_FACTS_MAX:
            break
    return out


def normalize_ai(data: dict) -> dict:
    """Ответ FLASH → строго ключи §2.3, числа int в диапазонах, строки — из допустимых,
    key_facts — список ≤4 коротких фактов (v5.1)."""
    d = data if isinstance(data, dict) else {}
    return {
        "gist": str(d.get("gist") or "")[:600],
        "one_liner": str(d.get("one_liner") or d.get("gist") or "")[:120],
        "tone": _int(d.get("tone"), -100, 100),
        "honesty": _int(d.get("honesty"), 0, 100, 50),
        "hype": _int(d.get("hype"), 0, 100),
        "importance": _int(d.get("importance"), 0, 100),
        "novelty": _int(d.get("novelty"), 0, 100, 50),
        "kind": _enum(d.get("kind"), _KINDS, "мнение"),
        "horizon": _enum(d.get("horizon"), _HORIZONS, "дни"),
        "expected_effect": _enum(d.get("expected_effect"), _EFFECTS, "неясно"),
        "effect_strength": _int(d.get("effect_strength"), 0, 100),
        "manipulation_risk": _int(d.get("manipulation_risk"), 0, 100),
        "actors": _strs(d.get("actors")),
        "assets": _strs(d.get("assets"), up=True),
        "sectors": _strs(d.get("sectors")),
        "tags": _strs(d.get("tags")),
        "why_market": str(d.get("why_market") or "")[:300],
        "key_facts": _facts(d.get("key_facts")),
    }


def one_line(item: dict) -> str:
    """`[a1b2c3] 18.09 14:20 · rbc · <one_liner> · тон +40 · честн 70 · разд 30 · важн 65 ·
    нов 50 · эффект вверх/40 · факт · часы · SBER,банки`; без разметки — заголовком."""
    head = f"[{sid(item.get('id'))}] {ai_v5.fmt_ts(item.get('ts'))} · {item.get('source') or '—'}"
    a = item.get("ai")
    if not a:
        return f"{head} · {(item.get('title') or '')[:140]}"
    who = ",".join((a.get("assets") or [])[:4] + (a.get("sectors") or [])[:2]) or "—"
    return (f"{head} · {a.get('one_liner') or (item.get('title') or '')[:120]}"
            f" · тон {a.get('tone', 0):+d} · честн {a.get('honesty', 0)} · разд {a.get('hype', 0)}"
            f" · важн {a.get('importance', 0)} · нов {a.get('novelty', 0)}"
            f" · эффект {a.get('expected_effect', 'неясно')}/{a.get('effect_strength', 0)}"
            f" · {a.get('kind', '—')} · {a.get('horizon', '—')} · {who}")


def is_rich(item: dict) -> bool:
    """Важная новость: importance ≥ RICH_IMPORTANCE или effect_strength ≥ RICH_EFFECT."""
    a = item.get("ai") or {}
    return (_int(a.get("importance"), 0, 100) >= RICH_IMPORTANCE
            or _int(a.get("effect_strength"), 0, 100) >= RICH_EFFECT)


def one_line_rich(item: dict) -> str:
    """one_line + у ВАЖНОЙ новости (is_rich) вторая строка с отступом:
    `    факты: f1; f2 · суть: <gist>` — ключевые характеристики для PRO, чтобы он
    видел сжатую суть, а не простыню. Неважная или без разметки — просто one_line."""
    line = one_line(item)
    a = item.get("ai")
    if not a or not is_rich(item):
        return line
    parts = []
    facts = [str(f).strip() for f in (a.get("key_facts") or []) if str(f).strip()]
    if facts:
        parts.append("факты: " + "; ".join(facts[:KEY_FACTS_MAX]))
    gist = re.sub(r"\s+", " ", str(a.get("gist") or "")).strip()
    if gist and gist != (a.get("one_liner") or ""):
        parts.append("суть: " + gist)
    return line + ("\n    " + " · ".join(parts) if parts else "")


def render(items: list[dict], limit: int | None = None, rich: bool = False) -> str:
    """Строки для промптов. limit=None (по умолчанию) — ВСЕ новости, без обрезки;
    число — только первые limit (для панели/логов, не для ИИ). rich=True —
    one_line_rich: у важных новостей вторая строка «факты: … · суть: …» (вход PRO:
    аналитик совета, новые новости пересчёта/взгляда, дозор)."""
    src = list(items or [])
    if limit is not None:
        src = src[:max(0, int(limit))]
    fn = one_line_rich if rich else one_line
    return "\n".join(fn(x) for x in src)


def _raw_line(item: dict) -> str:
    text = re.sub(r"\s+", " ", item.get("summary") or "").strip()[:200]
    line = (f"[{sid(item['id'])}] {ai_v5.fmt_ts(item.get('ts'))} · {item.get('source') or '—'}"
            f" · {(item.get('title') or '').strip()[:200]}")
    return f"{line} — {text}" if text else line


# ── сбор ──────────────────────────────────────────────────────────────────
def _to_rows(items) -> list[dict]:
    """news.News (или dict) → строки для store: to_dict() + ts; битые элементы пропускаются."""
    rows = []
    for n in items or []:
        try:
            d = n.to_dict() if hasattr(n, "to_dict") else dict(n)
            d["ts"] = float(getattr(n, "ts", 0) or d.get("ts") or 0)
            rows.append(d)
        except Exception:        # noqa: BLE001
            continue
    return rows


async def collect(days: float, force: bool = True) -> int:
    """RSS → хранилище. Возврат: новых. Сеть упала → 0 и лог (не исключение)."""
    try:
        items = await news.fetch_news(force=force, days=max(1, int(math.ceil(days))))
    except Exception as e:       # noqa: BLE001
        log.warning("collect: RSS недоступен: %s", str(e)[:120])
        return 0
    rows = _to_rows(items)
    try:
        new = store_v5.news_upsert_raw(rows)
    except Exception as e:       # noqa: BLE001
        log.warning("collect: запись в базу: %s", str(e)[:120])
        return 0
    log.info("collect: %d из ленты, новых %d", len(rows), new)
    return new


# ── целевые новости Google News (совет: весь каталог; миссия: один тикер) ──
def catalog_targets() -> list[tuple[str, str, str]]:
    """(code, name, asset_class) по instruments.CATALOG — цели Google News для совета."""
    out: list[tuple[str, str, str]] = []
    for it in getattr(instruments, "CATALOG", None) or []:
        try:
            code = str(it.get("code") or "").upper().strip()
            if code:
                out.append((code, str(it.get("name") or code), str(it.get("asset_class") or "share")))
        except Exception:        # noqa: BLE001
            continue
    return out


async def collect_targeted(targets, days: float = 3) -> list[str]:
    """Google News по инструментам (news.fetch_for_ticker: запрос по тикеру и имени,
    у фьючерсов — только по имени базового актива, 15-мин кэш) → хранилище.
    targets: [(code, name, asset_class)]. Все цели параллельно, каждая под своим
    таймаутом TARGET_TIMEOUT; дедуп по ссылке между целями. Возврат: id НОВЫХ строк
    (по ним миссия отбирает и размечает только новое). Сеть упала / база не
    пишет → [] и лог, не исключение."""
    win = max(1, int(math.ceil(float(days or 1))))
    tg: list[tuple[str, str, str]] = []
    for t in targets or []:
        try:
            code, name, ac = (list(t) + ["", ""])[:3]
            code = str(code or "").upper().strip()
            if code:
                tg.append((code, str(name or code), str(ac or "share")))
        except Exception:        # noqa: BLE001
            continue
    if not tg:
        return []

    sem = asyncio.Semaphore(max(1, int(TARGET_PAR)))

    async def _one(code: str, name: str, ac: str) -> list:
        try:
            async with sem:
                res = await asyncio.wait_for(
                    news.fetch_for_ticker(code, name, days=win, asset_class=ac or "share"),
                    timeout=TARGET_TIMEOUT)
        except asyncio.TimeoutError:
            log.info("collect_targeted: %s: Google News не ответил за %.0f с", code, TARGET_TIMEOUT)
            return []
        except Exception as e:   # noqa: BLE001
            log.info("collect_targeted: %s: %s", code, str(e)[:120])
            return []
        items = res[0] if isinstance(res, tuple) else res      # (items, note) или просто список
        return list(items or [])

    try:
        results = await asyncio.gather(*(_one(*t) for t in tg))
    except Exception as e:       # noqa: BLE001
        log.warning("collect_targeted: %s", str(e)[:120])
        return []
    rows: list[dict] = []
    seen: set[str] = set()
    cut = time.time() - (win + 1) * 86400.0      # «архивные» новости за 90 дн (fetch_for_ticker при пустом окне) не берём
    for items in results:
        for d in _to_rows(items):
            key = (d.get("link") or "").strip() or (d.get("title") or "").strip().lower()
            if not key or key in seen:
                continue
            try:
                if d.get("ts") is not None and float(d["ts"]) < cut:
                    continue
            except (TypeError, ValueError):
                pass
            seen.add(key)
            rows.append(d)
    if not rows:
        log.info("collect_targeted: целей %d, Google News пуст", len(tg))
        return []
    try:
        new_ids = store_v5.news_upsert_raw_ids(rows)
    except Exception as e:       # noqa: BLE001
        log.warning("collect_targeted: запись в базу: %s", str(e)[:120])
        return []
    log.info("collect_targeted: целей %d, из Google News %d, новых %d", len(tg), len(rows), len(new_ids))
    return new_ids


async def enrich_ticker(run_id: str, ticker: str, name: str, asset_class: str, days: float = 3) -> dict:
    """Миссия: целевые Google News по инструменту → отбор → разметка ТОЛЬКО новых
    (всё окно не трогаем). Стадия «news» в шине (start/done/error, ticker=…).
    Идемпотентно: повтор без новых → нули. Любой сбой ловится: стадия получает
    «error» с detail, возврат нули — миссию не роняем. PYTHIA_GNEWS выключен →
    сразу нули без сети. Возврат: {"new","relevant","characterized"}."""
    scope = scope_of(run_id)
    out = {"new": 0, "relevant": 0, "characterized": 0}
    t = str(ticker or "").upper().strip()
    if not getattr(config, "PYTHIA_GNEWS", True):
        try:
            await bus.stage(scope, run_id, "news", "done", ticker=t, n=0,
                            detail="Google News по инструментам выключен (PYTHIA_GNEWS=0)")
        except Exception:        # noqa: BLE001
            pass
        return out
    try:
        await bus.stage(scope, run_id, "news", "start", ticker=t,
                        detail=f"Google News по {t} ({name or t}) за {max(1, int(math.ceil(float(days or 1))))} дн")
        new_ids = await collect_targeted([(t, name or t, asset_class or "share")], days)
        out["new"] = len(new_ids)
        if new_ids:
            await triage(run_id, days, items=store_v5.news_by_ids(new_ids))
            rel = [x for x in store_v5.news_by_ids(new_ids) if x.get("relevant") == 1]
            out["relevant"] = len(rel)
            out["characterized"] = await characterize_all(run_id, days, items=[x for x in rel if not x.get("ai")])
        await bus.stage(scope, run_id, "news", "done", ticker=t, n=out["new"],
                        detail=(f"Google News по {t}: новых {out['new']}, биржевых {out['relevant']}, "
                                f"размечено {out['characterized']}") if out["new"] else
                               f"Google News по {t}: нового нет (всё уже в базе)")
        return out
    except asyncio.CancelledError:
        # v5.4.4 (отчёт проверяющего A2): срок сбора у вызывающего вышел (wait_for) или задачу сняли — CancelledError
        # не Exception: без этой ветки стадия «news» висела в start. Закрыть «error» и пробросить отмену дальше
        try:
            await bus.stage(scope, run_id, "news", "error", ticker=t,
                            detail=f"Google News по {t}: сбор прерван (срок вышел или задачу сняли) — решаем без него")
        except Exception:        # noqa: BLE001
            pass
        raise
    except Exception as e:       # noqa: BLE001
        msg = _hum(e)
        log.warning("enrich_ticker %s: %s", t, msg)
        try:
            await bus.stage(scope, run_id, "news", "error", ticker=t, detail=msg)
        except Exception:        # noqa: BLE001
            pass
        return {"new": 0, "relevant": 0, "characterized": 0}


enrich_ticker.closes_stage_on_cancel = True   # v5.4.4: миссия не дублирует «error» стадии при отмене (_enrich_news)


# ── отбор ─────────────────────────────────────────────────────────────────
async def _triage_batch(batch: list[dict]) -> tuple[list[str], list[str]]:
    by_sid = {sid(x["id"]): x["id"] for x in batch}
    lines = "\n".join(_raw_line(x) for x in batch)
    s, u = P.triage(lines, len(batch))
    obj = await ai_v5.flash_json(s, u, route="triage")
    keep_raw = (obj or {}).get("keep") if isinstance(obj, dict) else obj
    keep = set()
    for k in keep_raw or []:
        k = str(k).strip().strip("[]")
        if k in by_sid:
            keep.add(by_sid[k])
        elif len(k) > 6 and k[:6] in by_sid:
            keep.add(by_sid[k[:6]])
    keep_ids = [x["id"] for x in batch if x["id"] in keep]
    drop_ids = [x["id"] for x in batch if x["id"] not in keep]
    return keep_ids, drop_ids


async def triage(run_id: str, days: float, items: list[dict] | None = None) -> int:
    """FLASH пачками по TRIAGE_BATCH=50 (остаток — последняя неровная пачка: 130 → 50,
    50, 30), все пачки параллельно под SEM_FLASH: {"keep":[sid]}. Возврат: отмечено
    relevant=1. Сбой одной пачки → стадия «error» с detail, остальные идут.
    items — ограничить набором (дозор); иначе все неотобранные окна."""
    scope = scope_of(run_id)
    todo = items if items is not None else store_v5.news_untriaged(days)
    todo = [x for x in todo if x.get("id")]
    total = len(todo)
    batches = [todo[i:i + TRIAGE_BATCH] for i in range(0, total, TRIAGE_BATCH)]
    sizes = ", ".join(str(len(b)) for b in batches[:8]) + (", …" if len(batches) > 8 else "")
    await bus.stage(scope, run_id, "triage", "start", n=0, total=total,
                    detail=f"отбор биржевого: {total} новостей"
                           + (f", пачек по {TRIAGE_BATCH}: {len(batches)} ({sizes})" if batches else ""))
    if not todo:
        await bus.stage(scope, run_id, "triage", "done", n=0, total=0, detail="нечего отбирать")
        return 0
    done_n = 0
    kept = 0
    errors = 0

    async def _one(b):
        nonlocal done_n, kept, errors
        try:
            keep_ids, drop_ids = await _triage_batch(b)
            store_v5.news_set_relevant(keep_ids, 1)
            store_v5.news_set_relevant(drop_ids, 0)
            kept += len(keep_ids)
        except Exception as e:   # noqa: BLE001
            errors += 1
            log.warning("triage: пачка %d шт. не отобрана: %s", len(b), _hum(e))
            await bus.stage(scope, run_id, "triage", "error",
                            detail=f"пачка {len(b)} шт.: {_hum(e)}")
        done_n += len(b)
        await bus.stage(scope, run_id, "triage", "progress", n=done_n, total=total)

    await asyncio.gather(*(_one(b) for b in batches))
    await bus.stage(scope, run_id, "triage", "done", n=total, total=total,
                    detail=f"биржевого: {kept} из {total}" + (f", сбоев пачек {errors}" if errors else ""))
    return kept


# ── разметка ──────────────────────────────────────────────────────────────
async def characterize_one(item: dict) -> dict:
    s, u = P.characterize(item)
    obj = await ai_v5.flash_json(s, u, route="characterize")
    data = normalize_ai(obj)
    if not data["one_liner"]:
        data["one_liner"] = (item.get("title") or "")[:120]
    store_v5.news_ai_put(item["id"], data)
    return data


async def characterize_all(run_id: str, days: float, items: list[dict] | None = None) -> int:
    """Каждая релевантная неразмеченная — отдельный FLASH-чат (новый чат, полный текст
    новости), все параллельно под ai_v5.SEM_FLASH (PYTHIA_FLASH_PAR); шина шлёт progress
    каждые 5 новостей / раз в секунду; сбой одной новости — лог, остальные идут.
    Идемпотентно. Возврат: размечено."""
    scope = scope_of(run_id)
    todo = items if items is not None else store_v5.news_relevant_uncharacterized(days)
    todo = [x for x in todo if x.get("id")]
    total = len(todo)
    await bus.stage(scope, run_id, "characterize", "start", n=0, total=total,
                    detail=f"разметка: {total} новостей")
    if not todo:
        await bus.stage(scope, run_id, "characterize", "done", n=0, total=0, detail="всё размечено")
        return 0
    done_n = 0
    ok = 0
    last_emit = [0.0]

    async def _one(it):
        nonlocal done_n, ok
        try:
            await characterize_one(it)
            ok += 1
        except Exception as e:   # noqa: BLE001
            log.info("characterize: %s пропущена: %s", sid(it["id"]), _hum(e))
        done_n += 1
        now = time.time()
        if done_n == total or done_n % 5 == 0 or now - last_emit[0] >= 1.0:
            last_emit[0] = now
            await bus.stage(scope, run_id, "characterize", "progress", n=done_n, total=total)

    await asyncio.gather(*(_one(it) for it in todo))
    await bus.stage(scope, run_id, "characterize", "done", n=total, total=total,
                    detail=f"размечено {ok} из {total}")
    return ok


# ── группировка ───────────────────────────────────────────────────────────
def _clean_streams(obj, known: set[str]) -> dict:
    streams_out = []
    used: set[str] = set()
    src = obj if isinstance(obj, dict) else {}
    for st in src.get("streams") or []:
        if not isinstance(st, dict):
            continue
        ids = []
        for i in st.get("ids") or []:
            k = sid(str(i).strip().strip("[]"))
            if k in known and k not in ids:
                ids.append(k)
        if not ids:
            continue
        used.update(ids)
        streams_out.append({
            "tag": re.sub(r"\s+", "_", str(st.get("tag") or "поток").strip())[:40],
            "title": str(st.get("title") or st.get("tag") or "поток")[:80],
            "gist": str(st.get("gist") or "")[:800],
            "ids": ids,
            "net_tone": _int(st.get("net_tone"), -100, 100),
            "heat": _int(st.get("heat"), 0, 100),
            "assets": _strs(st.get("assets"), up=True),
        })
    orphans = []
    for i in src.get("orphans") or []:
        k = sid(str(i).strip().strip("[]"))
        if k in known and k not in used and k not in orphans:
            orphans.append(k)
    for k in known:              # всё, что ИИ потерял — в одиночки
        if k not in used and k not in orphans:
            orphans.append(k)
    streams_out.sort(key=lambda s: -s["heat"])
    return {"streams": streams_out, "orphans": orphans}


def _merge_apply(parts: list[dict], mapping) -> dict:
    """Склейка потоков частей по карте ИИ: id — объединение (код), тексты — из карты.
    Потоки без пары остаются как есть; orphans объединяются."""
    by_code: dict[str, dict] = {}
    for pi, part in enumerate(parts, 1):
        for si, st in enumerate(part.get("streams") or [], 1):
            by_code[f"P{pi}S{si}"] = st
    used: set[str] = set()
    merged = []
    for g in ((mapping or {}).get("merged") or []) if isinstance(mapping, dict) else []:
        if not isinstance(g, dict):
            continue
        codes = [str(c).upper().strip() for c in (g.get("from") or [])]
        codes = [c for c in codes if c in by_code and c not in used]
        if len(codes) < 2:
            continue
        ids: list[str] = []
        assets: list[str] = []
        for c in codes:
            used.add(c)
            for i in by_code[c].get("ids") or []:
                if i not in ids:
                    ids.append(i)
            for a in by_code[c].get("assets") or []:
                if a not in assets:
                    assets.append(a)
        first = by_code[codes[0]]
        merged.append({
            "tag": str(g.get("tag") or first.get("tag") or "поток"),
            "title": str(g.get("title") or first.get("title") or "поток"),
            "gist": str(g.get("gist") or first.get("gist") or ""),
            "ids": ids,
            "net_tone": g.get("net_tone", first.get("net_tone", 0)),
            "heat": g.get("heat", first.get("heat", 0)),
            "assets": _strs(g.get("assets"), up=True) or assets,
        })
    for code, st in by_code.items():
        if code not in used:
            merged.append(dict(st))
    orphans: list[str] = []
    for part in parts:
        for i in part.get("orphans") or []:
            if i not in orphans:
                orphans.append(i)
    return {"streams": merged, "orphans": orphans}


def _parts_text(parts: list[dict]) -> tuple[str, int]:
    lines, n = [], 0
    for pi, part in enumerate(parts, 1):
        for si, st in enumerate(part.get("streams") or [], 1):
            n += 1
            lines.append(f"P{pi}S{si} · {st.get('title')} [{st.get('tag')}] · накал {st.get('heat')}"
                         f" · тон {st.get('net_tone')} · {', '.join((st.get('assets') or [])[:5]) or '—'}"
                         f" · {len(st.get('ids') or [])} нов.\n    {str(st.get('gist') or '')[:400]}")
    return "\n".join(lines), n


async def _merge_parts(parts: list[dict]) -> dict:
    """Слияние частей: ИИ даёт только карту одинаковых тем (маленький ответ), склеивает код.
    Сбой карты → части просто складываются (ничего не теряется)."""
    text, n = _parts_text(parts)
    try:
        s, u = P.group_merge(text, n)
        mapping = await ai_v5.flash_json(s, u, route="group_merge")
    except Exception as e:       # noqa: BLE001
        log.warning("group_merge: карта не получена (%s) — части складываю как есть", _hum(e))
        mapping = {}
    return _merge_apply(parts, mapping)


async def group(run_id: str, days: float) -> dict:
    """FLASH (думает сам, auto): потоки/теги §2.4. Провал ИИ → все новости в orphans (этап не падает)."""
    scope = scope_of(run_id)
    items = store_v5.news_window(days)
    items = [x for x in items if x.get("ai")]
    known = {sid(x["id"]) for x in items}
    await bus.stage(scope, run_id, "group", "start", n=0, total=len(items),
                    detail=f"группировка {len(items)} новостей")
    if not items:
        await bus.stage(scope, run_id, "group", "done", detail="нет размеченных новостей")
        return {"streams": [], "orphans": []}
    try:
        if len(items) <= GROUP_LIMIT:
            s, u = P.group(render(items, limit=len(items)), len(items))
            obj = await ai_v5.flash_json(s, u, route="group")
        else:
            parts = []
            batches = [items[i:i + GROUP_BATCH] for i in range(0, len(items), GROUP_BATCH)]

            async def _part(b):
                s, u = P.group(render(b, limit=len(b)), len(b))
                return await ai_v5.flash_json(s, u, route="group")

            res = await asyncio.gather(*(_part(b) for b in batches), return_exceptions=True)
            for r in res:
                if isinstance(r, Exception):
                    log.warning("group: пачка не сгруппирована: %s", _hum(r))
                else:
                    parts.append(_clean_streams(r, known))
            if not parts:
                raise RuntimeError("ни одна пачка не сгруппирована")
            if len(parts) == 1:
                obj = parts[0]
            else:
                obj = await _merge_parts(parts)
        out = _clean_streams(obj, known)
    except Exception as e:       # noqa: BLE001
        log.warning("group: %s", _hum(e))
        await bus.stage(scope, run_id, "group", "error", detail=_hum(e))
        out = {"streams": [], "orphans": sorted(known)}
    await bus.stage(scope, run_id, "group", "done", n=len(out["streams"]), total=len(items),
                    detail=f"потоков {len(out['streams'])}, одиночек {len(out['orphans'])}")
    return out


# ── выборки ───────────────────────────────────────────────────────────────
def _kw(ticker: str, name: str) -> list[str]:
    t = (ticker or "").upper().strip()
    kws = list(_TICKER_KW.get(t, []))
    nm = re.sub(r"[()«»\"—–-]", " ", (name or "").lower())
    for w in re.split(r"\s+", nm):
        w = w.strip(" .,")
        if len(w) >= 4 and w not in ("пао", "оао", "акции", "фьючерс", "индекс", "россии"):
            kws.append(w)
    if t and t.lower() not in kws and len(t) >= 3:
        kws.append(t.lower())
    return list(dict.fromkeys(kws))


def _match(item: dict, ticker: str, kws: list[str]) -> bool:
    a = item.get("ai") or {}
    assets = [str(x).upper() for x in (a.get("assets") or [])]
    if ticker in assets:
        return True
    hay = " ".join([*(a.get("assets") or []), *(a.get("tags") or []), *(a.get("sectors") or []),
                    item.get("title") or "", a.get("one_liner") or ""]).lower()
    return any(k in hay for k in kws)


def _score(item: dict, days: float) -> float:
    a = item.get("ai") or {}
    imp = float(a.get("importance") or 30)
    age_d = max(0.0, (time.time() - float(item.get("ts") or 0)) / 86400)
    fresh = max(0.1, 1.0 - age_d / max(1.0, float(days) + 0.5))
    return imp * fresh


def news_for_ticker(ticker: str, name: str, days: float = 3, limit: int | None = None) -> list[dict]:
    """Новости инструмента: по assets/tags/sectors/заголовку + news_ids его карточки из
    последнего совета (в т.ч. целевые Google News, раз они легли в ту же базу).
    Сортировка: важность × свежесть. limit=None (по умолчанию) — все (v5.1, для ИИ)."""
    t = (ticker or "").upper().strip()
    kws = _kw(t, name)
    try:
        items = store_v5.news_window(days)
    except Exception as e:       # noqa: BLE001
        log.info("news_for_ticker: база: %s", str(e)[:100])
        items = []
    found = {x["id"]: x for x in items if _match(x, t, kws)}
    try:
        row = store_v5.council_latest(None)
        for c in ((row or {}).get("data") or {}).get("cards") or []:
            if str(c.get("ticker") or "").upper() == t:
                extra = [i for i in (c.get("news_ids") or []) if i not in found]
                for x in store_v5.news_by_ids(extra):
                    found[x["id"]] = x
                break
    except Exception as e:       # noqa: BLE001
        log.info("news_for_ticker: карточка совета: %s", str(e)[:100])
    out = sorted(found.values(), key=lambda x: -_score(x, days))
    return out if limit is None else out[:max(0, int(limit))]


def fresh_since(ts: float) -> list[dict]:
    try:
        return [x for x in store_v5.news_since(float(ts or 0)) if x.get("ai")]
    except Exception as e:       # noqa: BLE001
        log.info("fresh_since: %s", str(e)[:100])
        return []


# ── раскладка выбора совета по тикерам ────────────────────────────────────
def _catalog_text(limit: int = 300) -> str:
    lines = []
    try:
        for grp, its in instruments.all_grouped().items():
            lines.append(f"## {grp}")
            for it in its:
                lines.append(f"{it['code']} — {display_name(it['code'], it['name'])}")
                if len(lines) >= limit:
                    return "\n".join(lines)
    except Exception as e:       # noqa: BLE001
        log.info("distribute: каталог: %s", str(e)[:100])
        for it in instruments.CATALOG:
            lines.append(f"{it['code']} — {it['name']}")
    return "\n".join(lines[:limit])


def _picks_text(summary: dict) -> str:
    lines = []
    for p in summary.get("picks") or []:
        lines.append(f"ВХОД {p.get('ticker')} {p.get('side')} {p.get('conviction', '?')}% "
                     f"{p.get('horizon') or ''} — {p.get('why') or ''} | триггер: {p.get('trigger') or '—'}"
                     f" | риск: {p.get('risk') or '—'}")
    for p in summary.get("avoid") or []:
        lines.append(f"ИЗБЕГАТЬ {p.get('ticker')} — {p.get('why') or ''}")
    for p in summary.get("watch") or []:
        lines.append(f"СМОТРЕТЬ {p.get('ticker')} — {p.get('why') or ''}")
    return "\n".join(lines) or "—"


def _avg(vals: list) -> int:
    vals = [v for v in vals if isinstance(v, (int, float))]
    return int(round(sum(vals) / len(vals))) if vals else 0


def _card(pick: dict, news_items: list[dict], note: str) -> dict:
    t = str(pick.get("ticker") or "").upper()
    inst = instruments.get(t)
    ais = [x.get("ai") or {} for x in news_items]
    return {
        "ticker": t,
        "name": inst["name"] if inst else t,
        "asset_class": inst["asset_class"] if inst else instruments.guess_asset_class(t),
        "side": pick.get("side"), "conviction": pick.get("conviction"),
        "horizon": pick.get("horizon"), "why": pick.get("why"), "trigger": pick.get("trigger"),
        "risk": pick.get("risk"), "note": (note or "")[:300],
        "news_ids": [x["id"] for x in news_items], "news": news_items,
        "tone_avg": _avg([a.get("tone") for a in ais]),
        "honesty_avg": _avg([a.get("honesty") for a in ais]),
        "hype_avg": _avg([a.get("hype") for a in ais]),
        "importance_max": max([int(a.get("importance") or 0) for a in ais] or [0]),
        "news_count": len(news_items),
    }


async def distribute(run_id: str, summary: dict, days: float) -> list[dict]:
    """FLASH: выбор совета + каталог + новости окна → карточки тикеров §2.6."""
    scope = scope_of(run_id)
    picks = [p for p in (summary or {}).get("picks") or [] if p.get("ticker")]
    for p in picks:
        p["ticker"] = norm_ticker(p["ticker"])
    await bus.stage(scope, run_id, "distribute", "start", n=0, total=len(picks),
                    detail=f"раскладка {len(picks)} идей по тикерам")
    if not picks:
        await bus.stage(scope, run_id, "distribute", "done", detail="входов нет")
        return []
    items = [x for x in store_v5.news_window(days) if x.get("ai")]
    by_flash: dict[str, dict] = {}
    try:
        lines = render(items, limit=None)
        try:                                         # лента выше предела блока → FLASH ужимает (id сохраняются)
            lines = await compress.shrink(lines, None, "новости для раскладки")
        except Exception as e:   # noqa: BLE001
            log.info("distribute: сжатие ленты: %s — как есть", str(e)[:80])
        s, u = P.distribute(_picks_text(summary), _catalog_text(), lines)
        obj = await ai_v5.flash_json(s, u, route="distribute")
        for c in (obj or {}).get("cards") or []:
            if isinstance(c, dict) and c.get("ticker"):
                by_flash[norm_ticker(c["ticker"])] = c
    except Exception as e:       # noqa: BLE001
        log.warning("distribute: FLASH: %s — карточки без прикрепления", _hum(e))
        await bus.stage(scope, run_id, "distribute", "error", detail=_hum(e))
    cards = []
    for p in picks:
        t = str(p["ticker"]).upper()
        fc = by_flash.get(t) or by_flash.get(norm_ticker(t)) or {}
        ids = list(p.get("news_ids") or []) + [str(i) for i in (fc.get("news_ids") or [])]
        try:
            news_items = store_v5.news_by_ids(list(dict.fromkeys(ids)))[:20] if ids else []
        except Exception as e:   # noqa: BLE001
            log.info("distribute: news_by_ids: %s", str(e)[:100])
            news_items = []
        cards.append(_card(p, news_items, str(fc.get("note") or "")))
    await bus.stage(scope, run_id, "distribute", "done", n=len(cards), total=len(picks),
                    detail=", ".join(f"{c['ticker']} {c['side']}" for c in cards)[:300])
    return cards


# ── self-test ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    store_v5.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
    store_v5._schema()

    class FakeAI:
        FLASH = "flash"
        PRO = "pro"
        SEM_FLASH = asyncio.Semaphore(4)
        now_msk_str = staticmethod(ai_v5.now_msk_str)
        fmt_ts = staticmethod(ai_v5.fmt_ts)
        calls: list = []
        batches: list = []        # размеры пачек отбора («Новостей в пачке: N»)
        fail_title = ""           # разметка этой новости падает (сбой одной не роняет этап)

        async def flash_json(self, system, user, *, think=False, route="flash", max_tokens=None):
            self.calls.append(route)
            if route == "triage":
                self.batches.append(int(re.search(r"Новостей в пачке: (\d+)", user).group(1)))
                ids = re.findall(r"^\[(\w{6})\]", user, flags=re.M)
                return {"keep": [i for i in ids if "спорт" not in user.split(f"[{i}]")[1].split("\n")[0]]}
            if route == "characterize" and self.fail_title and self.fail_title in user:
                raise RuntimeError("сеть")
            if route == "characterize" and "Новость " in user:          # сценарий «200 новостей»
                n = int(re.search(r"Заголовок: Новость (\d+) ", user).group(1))
                rich = n % 2 == 0
                facts = "строка вместо списка" if n % 3 == 0 else \
                    [f"факт {k} · {'д' * 120}" if k == 1 else f"факт {k} = {n}" for k in range(1, 7)]
                return {"gist": f"Суть новости {n}: прибыль {n} млрд.", "one_liner": f"Новость {n}: {n} млрд",
                        "tone": 10, "honesty": 70, "hype": 10, "importance": 90 if rich else 30,
                        "novelty": 50, "kind": "факт", "horizon": "дни", "expected_effect": "вверх",
                        "effect_strength": 20 if rich else 10, "manipulation_risk": 5, "actors": ["Сбер"],
                        "assets": ["SBER"], "sectors": ["банки"], "tags": ["отчёт"], "why_market": "w",
                        "key_facts": facts}
            if route == "characterize":
                bad = "ЦБ" in user
                return {"gist": "суть", "one_liner": "ставка ЦБ вверх" if bad else "Сбер дивиденды",
                        "tone": "-40" if bad else 55, "honesty": 80, "hype": 120, "importance": 90,
                        "novelty": 40, "kind": "факт", "horizon": "дни", "expected_effect": "вниз" if bad else "вверх",
                        "effect_strength": 50, "manipulation_risk": 5, "actors": ["ЦБ"],
                        "assets": ["sber", "рубль"] if not bad else ["рубль", "индекс"], "sectors": ["банки"],
                        "tags": ["ставка"], "why_market": "потому",
                        "key_facts": ["ставка 21%", "заседание 24.10"] if bad else "нет списка"}
            if route == "group":
                ids = re.findall(r"^\[(\w{6})\]", user, flags=re.M)
                return {"streams": [{"tag": "ставка", "title": "ЦБ", "gist": "g", "ids": ids[:1] + ["zzzzzz"],
                                     "net_tone": -30, "heat": 80, "assets": ["SBER"]},
                                    {"tag": "пусто", "title": "x", "gist": "", "ids": ["nonono"]}],
                        "orphans": ids[1:]}
            if route == "distribute":
                ids = re.findall(r"^\[(\w{6})\]", user, flags=re.M)
                return {"cards": [{"ticker": "sber", "news_ids": ids, "note": "ок"}]}
            return {}

    fake = FakeAI()
    ai_v5 = fake  # noqa: F811

    class N:
        def __init__(self, title, summary, ts):
            self.source, self.title, self.summary, self.link = "rbc", title, summary, "http://x/" + title
            self.published, self.ts = "", ts

        def to_dict(self):
            return {"source": self.source, "title": self.title, "summary": self.summary,
                    "link": self.link, "published": self.published}

    now = time.time()

    async def fake_fetch(*, force=False, days=None):
        return [N("Сбербанк объявил дивиденды", "рекордная прибыль", now - 3600),
                N("ЦБ поднял ставку", "неожиданно", now - 7200),
                N("Футбол: спорт матч", "гол", now - 100)]

    news.fetch_news = fake_fetch

    async def main():
        run_id = bus.start_run("daily", {})
        assert await collect(3) == 3
        assert await triage(run_id, 3) == 2
        assert store_v5.news_stats(3)["relevant"] == 2
        assert await characterize_all(run_id, 3) == 2
        assert await characterize_all(run_id, 3) == 0          # идемпотентно
        w = store_v5.news_window(3)
        cb = next(x for x in w if "ЦБ" in x["title"])
        assert cb["ai"]["tone"] == -40 and cb["ai"]["hype"] == 100 and cb["ai"]["kind"] == "факт"
        sb = next(x for x in w if "Сбербанк" in x["title"])
        assert sb["ai"]["assets"] == ["SBER", "рубль"], sb["ai"]["assets"]
        assert sb["ai"]["key_facts"] == [] and cb["ai"]["key_facts"] == ["ставка 21%", "заседание 24.10"]
        line = one_line(sb)
        assert line.startswith(f"[{sid(sb['id'])}]") and "тон +55" in line and "эффект вверх/50" in line, line
        # one_line_rich: важн 90 → вторая строка с фактами и сутью; one_line не меняется
        rl = one_line_rich(cb)
        assert rl.startswith(one_line(cb) + "\n    факты: ставка 21%; заседание 24.10 · суть: суть"), rl
        assert one_line_rich(sb) == one_line(sb) + "\n    суть: суть" and "факты:" not in one_line_rich(sb)
        assert one_line_rich({"id": "abcdefgh", "title": "t"}) == one_line({"id": "abcdefgh", "title": "t"})
        assert render(w, rich=True).count("\n    ") == 2 and render(w) == render(w, rich=False)
        assert render(w, 1).count("\n") == 0 and one_line({"id": "abcdefgh", "title": "t"}).endswith("· t")
        assert render(w) == render(w, None) and render(w).count("\n") == len(w) - 1   # по умолчанию — все
        assert render(w, 0) == "" and render([]) == ""
        g = await group(run_id, 3)
        assert len(g["streams"]) == 1 and g["streams"][0]["ids"] == [sid(w[0]["id"])], g
        assert set(g["orphans"]) == {sid(w[1]["id"])}, g
        # news_for_ticker: по assets и по ключевым словам
        got = news_for_ticker("SBER", "Сбербанк")
        assert [x["id"] for x in got] == [sb["id"]], got
        assert news_for_ticker("SI", "USD/RUB") and news_for_ticker("MX", "Индекс МосБиржи")
        assert not news_for_ticker("PLZL", "Полюс")
        assert len(fresh_since(now - 10)) == 2 and not fresh_since(now + 10)
        summary = {"picks": [{"ticker": "sber", "side": "long", "conviction": 70, "horizon": "день",
                              "why": "w", "trigger": "t", "risk": "r", "news_ids": [sid(sb["id"])]},
                             {"ticker": "XXXX", "side": "short", "conviction": 40, "news_ids": []}],
                   "avoid": [{"ticker": "GAZP", "why": "нет"}]}
        cards = await distribute(run_id, summary, 3)
        assert [c["ticker"] for c in cards] == ["SBER", "XXXX"]
        c0 = cards[0]
        assert c0["name"] == "Сбербанк" and c0["asset_class"] == "share" and c0["news_count"] == 2
        assert c0["note"] == "ок" and c0["importance_max"] == 90 and c0["hype_avg"] == 100
        assert cards[1]["asset_class"] == "share" and cards[1]["news_count"] == 0
        # карточка совета подтягивает новости в news_for_ticker
        store_v5.council_put(run_id, "daily", {"cards": cards})
        assert len(news_for_ticker("SBER", "Сбербанк")) == 2
        # сбой RSS → 0
        async def boom(**kw):
            raise RuntimeError("net")
        news.fetch_news = boom
        assert await collect(3) == 0
        bus.end_run(run_id)

        # ── целевые новости Google News: совет (весь каталог) и миссия (один тикер) ──
        global TARGET_TIMEOUT
        events: list[dict] = []

        async def sink(ev):
            events.append(ev)

        bus.set_sink(sink)
        tcalls: list = []
        tfeed = [N("Сбербанк объявил дивиденды", "рекордная прибыль", now - 3600),   # уже лежит из RSS
                 N("Сбербанк: целевая новость", "gnews", now - 1800),
                 N("Сбербанк: ещё одна целевая", "gnews", now - 1900)]

        async def fake_tk(ticker, name, *, days=14, fallback_days=90, limit=22, asset_class="share"):
            tcalls.append((ticker, name, days, asset_class))
            return list(tfeed)

        news.fetch_for_ticker = fake_tk
        cat = catalog_targets()
        assert cat[0] == ("BR", "Brent — нефть", "futures") and ("SBER", "Сбербанк", "share") in cat, cat[:3]
        new_ids = await collect_targeted([("SBER", "Сбербанк", "share"), ("sber", "Сбер Банк")], 2.5)
        assert len(new_ids) == 2 and all(len(i) == 40 for i in new_ids), new_ids   # дубль RSS и дубль между целями — нет
        assert tcalls[0] == ("SBER", "Сбербанк", 3, "share") and tcalls[1][:2] == ("SBER", "Сбер Банк"), tcalls
        assert await collect_targeted([("SBER", "Сбербанк", "share")], 3) == []    # повтор — новых нет
        assert await collect_targeted([], 3) == [] and await collect_targeted([("", "x")], 3) == []
        assert all(x["relevant"] is None for x in store_v5.news_by_ids(new_ids))    # только пишет, не отбирает
        # enrich_ticker: стадия news start/done, отбор и разметка ТОЛЬКО новых
        tfeed.append(N("Сбербанк: третья целевая", "g", now - 100))
        tfeed.append(N("Сбербанк: спорт-спонсорство матча", "g", now - 50))       # отбор её отбросит
        run2 = bus.start_run("mission", {"ticker": "SBER"})
        r = await enrich_ticker(run2, "SBER", "Сбербанк", "share", 3)
        assert r == {"new": 2, "relevant": 1, "characterized": 1}, r
        st_news = [e for e in events if e.get("run_id") == run2 and e["stage"] == "news"]
        assert [(e["status"], e["ticker"], e["scope"]) for e in st_news] == \
            [("start", "SBER", "mission"), ("done", "SBER", "mission")], st_news
        assert "новых 2, биржевых 1, размечено 1" in st_news[-1]["detail"], st_news[-1]
        assert store_v5.news_stats(3)["untriaged"] == 2                              # старые целевые не тронуты
        # повтор: всё уже в базе → нули, relevant считает только новые
        assert await enrich_ticker(run2, "SBER", "Сбербанк", "share", 3) == {"new": 0, "relevant": 0, "characterized": 0}
        # выключатель: без сети и без вызова фейка
        n_calls = len(tcalls)
        config.PYTHIA_GNEWS = False
        assert await enrich_ticker(run2, "GAZP", "Газпром", "share") == {"new": 0, "relevant": 0, "characterized": 0}
        assert len(tcalls) == n_calls
        config.PYTHIA_GNEWS = True
        # Google News упал → [] без исключения, миссия живёт (стадия done, не error)
        async def boom_tk(*a, **k):
            raise RuntimeError("net")
        news.fetch_for_ticker = boom_tk
        assert await collect_targeted(cat, 3) == []
        assert await enrich_ticker(run2, "SBER", "Сбербанк", "share") == {"new": 0, "relevant": 0, "characterized": 0}
        assert [e for e in events if e.get("run_id") == run2 and e["stage"] == "news"][-1]["status"] == "done"
        # Google News молчит дольше таймаута → [] (не висим); в ленте есть новое — без таймаута оно бы легло
        tfeed.append(N("Сбербанк: четвёртая", "g", now - 10))

        async def slow_tk(*a, **k):
            await asyncio.sleep(0.5)
            return list(tfeed)
        news.fetch_for_ticker = slow_tk
        TARGET_TIMEOUT = 0.05
        t_slow = time.time()
        assert await collect_targeted([("SBER", "Сбербанк", "share")], 3) == []
        assert time.time() - t_slow < 0.4 and store_v5.news_stats(3)["raw"] == 7, store_v5.news_stats(3)   # 3 RSS + 2 + 2; «четвёртая» не легла
        # сбой базы внутри enrich_ticker → стадия error, нули, без исключения
        news.fetch_for_ticker = fake_tk
        real_by_ids = store_v5.news_by_ids

        def bad_by_ids(ids):
            raise RuntimeError("база")
        store_v5.news_by_ids = bad_by_ids
        assert await enrich_ticker(run2, "SBER", "Сбербанк", "share") == {"new": 0, "relevant": 0, "characterized": 0}
        assert [e for e in events if e.get("run_id") == run2 and e["stage"] == "news"][-1]["status"] == "error"
        store_v5.news_by_ids = real_by_ids
        # v5.4.4 (A2): срок вызывающего вышел (wait_for отменяет задачу) — стадия «error», не висит в start; отмена дальше
        async def hang_tk(*a, **k):
            await asyncio.sleep(5)
            return []
        news.fetch_for_ticker = hang_tk
        n_ev = len(events)
        try:
            await asyncio.wait_for(enrich_ticker(run2, "SBER", "Сбербанк", "share"), 0.05)
            raise AssertionError("срок вышел — TimeoutError у вызывающего")
        except asyncio.TimeoutError:
            pass
        st_c = [e for e in events[n_ev:] if e.get("run_id") == run2 and e["stage"] == "news"]
        assert [e["status"] for e in st_c] == ["start", "error"] and "сбор прерван" in st_c[-1]["detail"], st_c
        assert enrich_ticker.closes_stage_on_cancel is True
        news.fetch_for_ticker = fake_tk
        bus.end_run(run2)

        # ── v5.1: 200 новостей — отбор пачками по 50, разметка каждой отдельным вызовом ──
        import hashlib
        store_v5.DB_PATH = Path(tempfile.mkdtemp()) / "t200.db"
        store_v5._schema()
        news.fetch_for_ticker = fake_tk
        fake.calls.clear()
        fake.batches.clear()
        events.clear()
        big = [N(f"Новость {i} про Сбер" + (" спорт" if i % 10 == 9 else ""), f"текст {i} " * 30, now - i * 30)
               for i in range(200)]

        async def fetch200(*, force=False, days=None):
            return list(big)

        news.fetch_news = fetch200
        run3 = bus.start_run("daily", {})
        assert await collect(3) == 200
        assert await triage(run3, 3) == 180
        assert fake.calls.count("triage") == 4 and fake.batches == [50, 50, 50, 50], fake.batches
        st_tr = [e for e in events if e.get("run_id") == run3 and e["stage"] == "triage"]
        assert "200 новостей, пачек по 50: 4 (50, 50, 50, 50)" in st_tr[0]["detail"], st_tr[0]
        assert [e["n"] for e in st_tr if e["status"] == "progress"][-1] == 200 and st_tr[-1]["status"] == "done"
        assert "биржевого: 180 из 200" in st_tr[-1]["detail"], st_tr[-1]
        # остаток — последняя неровная пачка: 130 → 50, 50, 30
        fake.batches.clear()
        syn = [{"id": hashlib.sha1(f"s{i}".encode()).hexdigest(), "title": f"т{i}", "ts": now, "source": "x"}
               for i in range(130)]
        await triage(run3, 3, items=syn)
        assert fake.batches == [50, 50, 30], fake.batches
        # разметка: каждая из 180 — отдельный вызов FLASH; сбой одной → лог, этап done
        fake.fail_title = "Заголовок: Новость 7 про"
        n_before = fake.calls.count("characterize")
        assert await characterize_all(run3, 3) == 179
        assert fake.calls.count("characterize") - n_before == 180
        st_ch = [e for e in events if e.get("run_id") == run3 and e["stage"] == "characterize"]
        assert st_ch[-1]["status"] == "done" and "размечено 179 из 180" in st_ch[-1]["detail"], st_ch[-1]
        assert len([e for e in st_ch if e["status"] == "progress"]) >= 36            # прогресс каждые 5
        assert not [e for e in st_ch if e["status"] == "error"]
        fake.fail_title = ""
        assert await characterize_all(run3, 3) == 1                                  # добираем упавшую
        w200 = [x for x in store_v5.news_window(3) if x.get("ai")]
        assert len(w200) == 180
        by_n = {int(re.search(r"Новость (\d+) ", x["title"]).group(1)): x for x in w200}
        # key_facts нормализованы: строка → [], 6 элементов → 4, факт ≤ 90 симв.
        assert by_n[3]["ai"]["key_facts"] == [] and by_n[6]["ai"]["key_facts"] == []
        kf = by_n[2]["ai"]["key_facts"]
        assert len(kf) == 4 and len(kf[0]) == 90 and kf[1] == "факт 2 = 2" and kf[3] == "факт 4 = 2", kf
        # one_line_rich: «факты:» только у важных (важн 90), у неважных — одна строка
        r2, r1, r6 = one_line_rich(by_n[2]), one_line_rich(by_n[1]), one_line_rich(by_n[6])
        assert "\n    факты: " in r2 and " · суть: Суть новости 2: прибыль 2 млрд." in r2 and r2.count("\n") == 1, r2
        assert r1 == one_line(by_n[1]) and "\n" not in r1 and by_n[1]["ai"]["importance"] == 30
        assert "факты:" not in r6 and r6.endswith("\n    суть: Суть новости 6: прибыль 6 млрд."), r6
        full = render(w200, rich=True)
        assert all(f"[{sid(x['id'])}]" in full for x in w200)
        n_rich = sum(1 for n in by_n if n % 2 == 0)
        assert full.count("\n    ") == n_rich and full.count("факты: ") == sum(1 for n in by_n if n % 2 == 0 and n % 3), n_rich
        assert render(w200).count("\n") == 179 and "факты:" not in render(w200)
        bus.end_run(run3)

    asyncio.run(main())
    print("newsflow self-test OK: 200 новостей — отбор 4×50 (130 → 50/50/30), разметка по одной, key_facts, rich")
