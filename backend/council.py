# -*- coding: utf-8 -*-
"""ПИФИЯ v5 — совет (этапы 1–2): анализ → критика → вердикт → итог → раскладка.

`daily`   — весь этап 1: новости → потоки → PRO-совет → карточки тикеров.
`update_with_news` — пересчёт: прошлый итог + новые новости → PRO-совет.
`human`   — этап 2: взгляд человека → FLASH сжатие → PRO-совет → рамка.
Один живой прогон совета на приложение (scope daily/human): пока идёт один —
второй отвечает «уже идёт». Любой сбой стадии → council_put(status="error"),
bus.end_run(error) и исключение наверх (API/фон логируют).
v5.1 «всё, но в разумных пределах»: кресла получают тексты друг друга целиком
(анализ → критик, анализ+критика → вердикт, вердикт → итог), новости окна — все
(важные — со строкой «факты: … · суть: …»), астро — полный блок; но окно DeepSeek
V4 на половине уже хуже чистого листа, поэтому перед каждым PRO-вызовом блоки
проходят `compress.fit` (блок выше PYTHIA_CTX_LIMIT = 200 000 симв. и сумма выше
PYTHIA_PROMPT_SOFT = 600 000 сжимаются FLASH без потери нитей; ниже пределов —
как есть, «обрезано» только при провале ИИ). Стадии шины несут размеры промптов
в символах — владелец видит в степпере, сколько ушло ИИ. Плюс целевые Google
News по каталогу в стадии collect (config.PYTHIA_GNEWS).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from . import (ai, ai_v5, astro, bus, compress, config, instruments, moex, newsflow,
               prompts_council as P, scout, store_v5)

log = logging.getLogger("pythia.council")

_SIDES = {"long": "long", "short": "short", "лонг": "long", "шорт": "short", "buy": "long",
          "sell": "short", "покупка": "long", "продажа": "short", "купить": "long", "продать": "short"}
_REGIMES = ("risk-on", "risk-off", "смешанно")
_SNAP_TEXT_CAP = 20_000


def _hum(e: Exception) -> str:
    try:
        return ai.humanize_error(e)
    except Exception:            # noqa: BLE001
        return str(e)[:200]


def _ctx_limit() -> int:
    return compress.ctx_limit()


def _n(x: int) -> str:
    """84512 → «84 512»."""
    return f"{int(x):,}".replace(",", " ")


def _sizes(who: str, blocks: dict[str, str], user: str | None = None) -> str:
    """Detail стадии «start»: «аналитик: промпт 84 512 симв. — новости 61 000, астро 3 300…».
    user — готовый user-текст (полный размер промпта); иначе сумма блоков."""
    total = len(user) if user is not None else sum(len(v or "") for v in blocks.values())
    parts = ", ".join(f"{k} {_n(len(v or ''))}" for k, v in blocks.items())
    return f"{who}: промпт {_n(total)} симв." + (f" — {parts}" if parts else "")


def _days(days) -> float:
    try:
        return float(days) if days else float(getattr(config, "PYTHIA_V5_DAYS", 3))
    except Exception:            # noqa: BLE001
        return 3.0


def _busy() -> str | None:
    for sc in ("daily", "human"):
        a = bus.active(sc)
        if a:
            return f"совет уже идёт ({a['run_id']}, стадия {a.get('stage')}) — дождись конца"
    return None


# ── снимки рынка и астро ──────────────────────────────────────────────────
async def market_snapshot() -> str:
    """Цены каталога строками `SBER Сбербанк 285.4 (+1.2%)`; 8 с на всё; пусто → «цены недоступны»."""
    cat = list(instruments.CATALOG)

    async def _one(it):
        return await moex.last_price(it["code"], it["asset_class"])

    try:
        res = await asyncio.wait_for(asyncio.gather(*(_one(it) for it in cat), return_exceptions=True),
                                     timeout=8)
    except Exception as e:       # noqa: BLE001
        log.info("snapshot: цены: %s", str(e)[:100])
        return "цены недоступны"
    lines = []
    for it, r in zip(cat, res):
        if not isinstance(r, dict) or not r.get("price"):
            continue
        px = float(r["price"])
        s = f"{it['code']} {newsflow.display_name(it['code'], it['name'])} {px:g}"
        ch = r.get("change_pct")
        if isinstance(ch, (int, float)):
            s += f" ({float(ch):+.1f}%)"
        if r.get("stale"):
            s += " закр."
        lines.append(s)
    return "\n".join(lines) or "цены недоступны"


async def _astro() -> tuple[str, str]:
    """(полный астро-блок без обрезки, короткая строка); любой сбой → «астро недоступно».
    Берём astro.render_compact (факты без воды), если модуль его даёт, иначе render_for_ai."""
    try:
        ctx = await astro.acontext()
        if not ctx:
            return "астро недоступно", ""
        fn = getattr(astro, "render_compact", None) or astro.render_for_ai
        try:
            full = (fn(ctx) or "").strip()
        except Exception as e:   # noqa: BLE001
            log.info("астро %s: %s — беру render_for_ai", getattr(fn, "__name__", "?"), str(e)[:100])
            full = (astro.render_for_ai(ctx) or "").strip()
        try:
            line = astro.short_line(ctx) or ""
        except Exception:        # noqa: BLE001
            line = ""
        return full or "астро недоступно", line
    except Exception as e:       # noqa: BLE001
        log.info("астро недоступно: %s", str(e)[:100])
        return "астро недоступно", ""


# ── тексты для промптов ───────────────────────────────────────────────────
def _pick_line(p: dict) -> str:
    return (f"{p.get('ticker')} {p.get('side')} {p.get('conviction', '?')}% {p.get('horizon') or ''}"
            f" — {p.get('why') or ''} | триггер: {p.get('trigger') or '—'} | риск: {p.get('risk') or '—'}")


def _age_text(ts) -> str:
    """Возраст совета: «40 мин назад» / «26 ч назад»."""
    try:
        age = max(0.0, time.time() - float(ts))
    except (TypeError, ValueError):
        return "возраст неизвестен"
    return f"{int(age // 60)} мин назад" if age < 3600 else f"{int(age // 3600)} ч назад"


def _row_head(meta: dict) -> str:
    """v5.4.2: шапка итога из строки council_latest (или data совета): вид, время МСК и возраст — вчерашний
    совет не читается как сегодняшний; совет общий по рынку, у миссии свой приказ."""
    kind = meta.get("kind") or "—"
    ts = meta.get("ts")
    when = f"от {ai_v5.fmt_ts(ts)} МСК ({_age_text(ts)})" if ts else "(время неизвестно)"
    return f"Совет {kind} {when} — общий по рынку"


def summary_text(summary: dict) -> str:
    """Текст итога совета целиком (без обрезки). Принимает summary, data совета или строку council_latest.
    v5.4.2: строка council_latest (или data с ts/kind) → первой строкой шапка «Совет {kind} от ДД.ММ ЧЧ:ММ МСК
    (N ч назад) — общий по рынку»; голый summary — без шапки, как раньше. Пустые picks — явная строка
    «Входов совет не назвал»; режим, подставленный по умолчанию (в JSON его не было), — «режим не распознан»."""
    s = summary or {}
    meta = None
    if isinstance(s, dict) and "data" in s and isinstance(s["data"], dict):
        meta = s if (s.get("ts") or s.get("kind")) else None
        s = s["data"]
    if isinstance(s, dict) and "summary" in s and isinstance(s["summary"], dict):
        if meta is None and (s.get("ts") or s.get("kind")):
            meta = s
        s = s["summary"]
    if not isinstance(s, dict) or not s:
        return "совета ещё не было"
    lines = [_row_head(meta)] if meta else []
    if "regime_raw" in s or not s.get("regime"):
        raw = str(s.get("regime_raw") or "").strip()
        lines.append("Режим: режим не распознан" + (f" (в ответе совета «{raw}»)" if raw else " (совет его не назвал)"))
    else:
        lines.append(f"Режим: {s.get('regime')}")
    if s.get("summary"):
        lines.append(str(s["summary"]))
    if s.get("picks"):
        lines.append("Входы:")
        lines += ["- " + _pick_line(p) for p in s["picks"]]
    else:
        lines.append("Входов совет не назвал")
    if s.get("avoid"):
        lines.append("Избегать: " + "; ".join(f"{a.get('ticker')} ({a.get('why') or ''})" for a in s["avoid"]))
    if s.get("watch"):
        lines.append("Смотреть: " + "; ".join(f"{a.get('ticker')} ({a.get('why') or ''})" for a in s["watch"]))
    if s.get("key_times"):
        lines.append("Ключевое время: " + "; ".join(str(x) for x in s["key_times"]))
    if s.get("human_alignment"):
        lines.append("По тезисам человека: " + "; ".join(
            f"{h.get('thesis')} → {h.get('verdict')} ({h.get('why') or ''})" for h in s["human_alignment"]))
    return "\n".join(lines)


def _streams_brief(streams: dict) -> str:
    out = []
    for st in (streams or {}).get("streams") or []:
        out.append(f"- [{st.get('tag')}] {st.get('title')}: {st.get('gist')} "
                   f"(накал {st.get('heat')}, тон {st.get('net_tone'):+d}, {len(st.get('ids') or [])} нов.)"
                   if isinstance(st.get("net_tone"), int) else
                   f"- [{st.get('tag')}] {st.get('title')}: {st.get('gist')}")
    n_orph = len((streams or {}).get("orphans") or [])
    if n_orph:
        out.append(f"- одиночных новостей вне потоков: {n_orph}")
    return "\n".join(out) or "потоков нет"


def _streams_full(streams: dict, items: list[dict]) -> str:
    """Вход аналитика: потоки с ВСЕМИ строками новостей; важные — one_line_rich
    (вторая строка «факты: … · суть: …»), так PRO видит ленту сжато, а суть важного — целиком."""
    by_sid = {newsflow.sid(x["id"]): x for x in items}
    parts = []
    for st in (streams or {}).get("streams") or []:
        head = (f"## {st.get('title')} [{st.get('tag')}] · накал {st.get('heat')} · тон {st.get('net_tone'):+d}"
                f" · {', '.join(st.get('assets') or []) or '—'}\n{st.get('gist') or ''}")
        rows = [newsflow.one_line_rich(by_sid[i]) for i in st.get("ids") or [] if i in by_sid]
        parts.append(head + "\n" + "\n".join(rows))
    orph = [newsflow.one_line_rich(by_sid[i]) for i in (streams or {}).get("orphans") or [] if i in by_sid]
    if orph:
        parts.append("## Вне потоков\n" + "\n".join(orph))
    if not parts:
        parts.append("## Все новости (потоки не собраны)\n" + newsflow.render(items, limit=None, rich=True))
    return "\n\n".join(parts)


async def _scout(scope: str, run_id: str, kind: str, brief: str) -> tuple[str, list]:
    """Разведка данных FLASH (v5.2) перед аналитиком: фьючерс на индекс сейчас и за неделю, соседи, курс, нефть —
    блок «ДАННЫЕ ПО ЗАПРОСУ РАЗВЕДКИ». Сбой → пусто, совет идёт без неё."""
    try:
        return await asyncio.wait_for(scout.run(scope, run_id, kind, brief), 120)
    except Exception as e:       # noqa: BLE001
        log.info("разведка совета: %s", _hum(e))
        return "", []


def _scout_brief(kind: str, stats: dict, streams: dict, snap: str, extra: str = "") -> str:
    st = (streams or {}).get("streams") or []
    lines = [f"Совет ({kind}): новостей к бирже {stats.get('relevant', 0)}, потоков {len(st)}.",
             "Темы: " + "; ".join(f"{x.get('title')} (накал {x.get('heat')})" for x in st[:12]) if st else "Потоков нет.",
             "Цены каталога уже есть: " + ", ".join((snap or "").splitlines()[:8]),
             "Астро-фон есть."]
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def _stats_text(stats: dict) -> str:
    return (f"сырых {stats.get('raw', 0)}, биржевых {stats.get('relevant', 0)}, "
            f"размечено {stats.get('characterized', 0)}, потоков {stats.get('streams', 0)}")


# ── валидация итога ───────────────────────────────────────────────────────
def _news_ids(ids) -> list[str]:
    ids = [str(i).strip().strip("[]") for i in (ids or []) if str(i).strip()]
    if not ids:
        return []
    try:
        return [x["id"] for x in store_v5.news_by_ids(ids)]
    except Exception:            # noqa: BLE001
        return []


def _tw(items, key="ticker") -> list[dict]:
    out = []
    for a in items or []:
        if isinstance(a, dict) and a.get(key):
            out.append({"ticker": newsflow.norm_ticker(a[key]), "why": str(a.get("why") or "")[:300]})
    return out


_SIDE_TABLE = {"long": ai_v5.SYN_BUY, "short": ai_v5.SYN_SELL}
_decision_of = ai_v5.decision_of          # чистая функция словаря; self-тест подменяет ai_v5 фейком — берём заранее


def _pick_side(raw) -> str | None:
    """Сторона пика терпимо: «long», «LONG », «buy_now», «ЛОНГ (покупка)» → long; не распознано → None."""
    s = str(raw or "").strip()
    side = _SIDES.get(s.lower())
    if side:
        return side
    try:
        return _decision_of(s, _SIDE_TABLE)
    except Exception:            # noqa: BLE001
        return None


def validate_summary(obj, human: bool = False, dropped: list | None = None) -> dict:
    """Итог совета по схеме §2.5. v5.4.2: сторона пика — терпимо (ai_v5.decision_of: long ← SYN_BUY,
    short ← SYN_SELL); пик без распознанной стороны не пропадает молча — строка «ТИКЕР «сторона»» идёт в
    dropped (вызывающий пишет её в стадию совета «пик без стороны: …»). Режима нет или он не из списка →
    «смешанно» и ключ regime_raw (что было в ответе) — summary_text скажет «режим не распознан»."""
    s = obj if isinstance(obj, dict) else {}
    regime = str(s.get("regime") or "").strip().lower()
    picks = []
    for p in s.get("picks") or []:
        if not isinstance(p, dict) or not p.get("ticker"):
            continue
        side = _pick_side(p.get("side"))
        if not side:
            if dropped is not None:
                dropped.append(f"{newsflow.norm_ticker(p['ticker'])} «{str(p.get('side') or '')[:40]}»")
            continue
        picks.append({
            "ticker": newsflow.norm_ticker(p["ticker"]), "side": side,
            "conviction": newsflow._int(p.get("conviction"), 0, 100, 50),
            "horizon": str(p.get("horizon") or "день")[:20],
            "why": str(p.get("why") or "")[:400], "trigger": str(p.get("trigger") or "")[:300],
            "risk": str(p.get("risk") or "")[:300], "news_ids": _news_ids(p.get("news_ids")),
        })
    out = {
        "regime": regime if regime in _REGIMES else "смешанно",
        "summary": str(s.get("summary") or "")[:2000],
        "picks": picks[:12], "avoid": _tw(s.get("avoid")), "watch": _tw(s.get("watch")),
        "key_times": [str(x)[:120] for x in (s.get("key_times") or []) if str(x).strip()][:12],
    }
    if regime not in _REGIMES:
        out["regime_raw"] = regime[:40]
    if human:
        out["human_alignment"] = [
            {"thesis": str(h.get("thesis") or "")[:300],
             "verdict": str(h.get("verdict") or "частично")[:20],
             "why": str(h.get("why") or "")[:300]}
            for h in (s.get("human_alignment") or []) if isinstance(h, dict)]
    return out


# ── стадии PRO ────────────────────────────────────────────────────────────
async def _stream_stage(scope: str, run_id: str, stage: str, system: str, user: str,
                        detail: str = "") -> str:
    """PRO-стадия со стримом. detail «start» — размеры промпта (см. _sizes), «done» — длина ответа."""
    await bus.stage(scope, run_id, stage, "start",
                    detail=detail or f"{stage}: промпт {_n(len(user))} симв. — PRO думает")

    async def on_text(d):
        await bus.text(scope, run_id, stage, d)

    async def on_think(d):
        await bus.text(scope, run_id, stage, d, kind="think")

    out = await ai_v5.pro_stream(system, user, on_think=on_think, on_text=on_text, route=stage)
    out = (out or "").strip()
    if not out:
        raise RuntimeError(f"{stage}: PRO вернул пустой ответ")
    bus.set_text(run_id, stage, out)
    await bus.stage(scope, run_id, stage, "done", detail=f"ответ {_n(len(out))} симв. (промпт {_n(len(user))})")
    return out


async def _deliberate(scope: str, run_id: str, analysis_su: tuple[str, str], streams_brief: str,
                      astro_line: str, human: bool = False, analysis_detail: str = "",
                      prices: str = "", prev_text: str = "") -> tuple[dict, dict]:
    """analysis → critique → verdict (stream) → summary (json). Возврат: (texts, summary).
    v5.4.2: критик и председатель видят цены (prices — снимок, что видел аналитик), председатель при
    пересчёте — итог прошлого совета (prev_text): живые входы не теряются молча.
    Кресла читают друг друга целиком, но в разумных пределах: перед каждым PRO-вызовом
    блоки идут через compress.fit/shrink — блок выше PYTHIA_CTX_LIMIT (200 000 симв.) или
    сумма выше PYTHIA_PROMPT_SOFT FLASH сжимает без потери нитей; ниже — как есть.
    analysis_detail — размеры блоков аналитика для стадии «start» (см. _sizes)."""
    texts: dict[str, str] = {}
    texts["analysis"] = await _stream_stage(scope, run_id, "analysis", *analysis_su, detail=analysis_detail)

    # критик: анализ (shrink по умолчанию = PYTHIA_CTX_LIMIT) + краткие потоки
    an = await compress.shrink(texts["analysis"], None, "анализ совета")
    cb = await compress.fit({"анализ": an, "потоки": streams_brief, "цены": prices or ""})
    an = cb["анализ"]
    s_, u_ = P.critique(an, cb["потоки"], prices=cb["цены"])
    texts["critique"] = await _stream_stage(scope, run_id, "critique", s_, u_, detail=_sizes("критик", cb, u_))

    # вердикт: анализ + критика + потоки под общим пределом
    vb = await compress.fit({"анализ": an, "критика": texts["critique"], "потоки": cb["потоки"],
                             "цены": cb["цены"], "прошлый итог": prev_text or ""})
    s_, u_ = P.verdict(vb["анализ"], vb["критика"], vb["потоки"], astro_line, human=human,
                       prev_summary=vb["прошлый итог"], prices=vb["цены"])
    texts["verdict"] = await _stream_stage(scope, run_id, "verdict", s_, u_, detail=_sizes("вердикт", vb, u_))

    # итог: вердикт целиком (shrink лишь выше PYTHIA_CTX_LIMIT)
    vd = await compress.shrink(texts["verdict"], None, "вердикт совета")
    codes = ", ".join(f"{it['code']} ({newsflow.display_name(it['code'], it['name'])})"
                      for it in instruments.CATALOG[:12]) + ", акции — тикер Мосбиржи (SBER, GAZP…)"
    s_, u_ = P.summary(vd, human=human, codes_text=codes)
    await bus.stage(scope, run_id, "summary", "start", detail=_sizes("итог в JSON", {"вердикт": vd}, u_))
    obj = await ai_v5.pro_json(s_, u_, route="summary")
    dropped: list[str] = []
    summary = validate_summary(obj, human=human, dropped=dropped)
    if dropped:                  # v5.4.2: пик без стороны не пропадает молча — заметка в стадии итога
        log.info("итог совета %s: пик без стороны: %s", run_id, "; ".join(dropped))
        await bus.stage(scope, run_id, "summary", "progress", n=len(summary["picks"]),
                        detail="пик без стороны: " + "; ".join(dropped))
    await bus.stage(scope, run_id, "summary", "done", n=len(summary["picks"]),
                    detail=f"{summary['regime']}; входов {len(summary['picks'])}: "
                           + ", ".join(f"{p['ticker']} {p['side']}" for p in summary["picks"])
                           + (f"; пик без стороны: {'; '.join(dropped)}" if dropped else ""),
                    data={"regime": summary["regime"],
                          "picks": [{k: p[k] for k in ("ticker", "side", "conviction")} for p in summary["picks"]]})
    return texts, summary


def _tokens_diff(before: dict) -> dict:
    try:
        now = ai_v5.usage() or {}
        return {"prompt": int(now.get("prompt", 0)) - int(before.get("prompt", 0)),
                "completion": int(now.get("completion", 0)) - int(before.get("completion", 0))}
    except Exception:            # noqa: BLE001
        return {}


def _usage() -> dict:
    try:
        return dict(ai_v5.usage() or {})
    except Exception:            # noqa: BLE001
        return {}


async def _fail(scope: str, run_id: str, kind: str, data: dict, e: Exception) -> None:
    msg = _hum(e)
    log.warning("%s %s: %s", kind, run_id, msg)
    data = dict(data)
    data["error"] = msg
    data["texts"] = dict((bus.run(run_id) or {}).get("texts") or {})
    try:
        store_v5.council_put(run_id, kind, data, status="error")
    except Exception as e2:      # noqa: BLE001
        log.warning("council_put(error): %s", str(e2)[:100])
    cur = (bus.run(run_id) or {}).get("stage") or "start"
    await bus.stage(scope, run_id, cur, "error", detail=msg)
    bus.end_run(run_id, error=msg)


def _cancelled(run_id: str, kind: str, data: dict) -> None:
    """CancelledError не является Exception: освобождаем прогон и сохраняем частичный текст."""
    data = dict(data, error="совет прерван",
                texts=dict((bus.run(run_id) or {}).get("texts") or {}))
    try:
        store_v5.council_put(run_id, kind, data, status="cancelled")
    except Exception as e:       # noqa: BLE001
        log.warning("council_put(cancelled): %s", str(e)[:100])
    finally:
        bus.end_run(run_id, status="cancelled")


# ── этап 1: daily ─────────────────────────────────────────────────────────
async def daily(days: float | None = None, reason: str = "по кнопке") -> dict:
    busy = _busy()
    if busy:
        raise RuntimeError(busy)
    days = _days(days)
    scope = "daily"
    run_id = bus.start_run(scope, {"days": days, "reason": reason})
    t0 = _usage()
    data: dict = {"kind": "daily", "reason": reason, "ts": time.time(), "days": days,
                  "time_msk": ai_v5.now_msk_str(), "run_id": run_id}
    try:
        await bus.stage(scope, run_id, "collect", "start", detail="читаю ленты")
        n_new = await newsflow.collect(days)
        await bus.stage(scope, run_id, "collect", "done", n=n_new, detail=f"новых новостей {n_new}")
        n_tk = 0
        if getattr(config, "PYTHIA_GNEWS", True):        # целевые Google News по всему каталогу
            targets = newsflow.catalog_targets()
            await bus.stage(scope, run_id, "collect", "start", n=n_new,
                            detail=f"Google News по {len(targets)} инструментам каталога")
            try:
                n_tk = len(await newsflow.collect_targeted(targets, days))
            except Exception as e:   # noqa: BLE001
                log.warning("daily: Google News по инструментам: %s", _hum(e))
                n_tk = 0
            await bus.stage(scope, run_id, "collect", "done", n=n_new + n_tk,
                            detail=f"новых новостей {n_new} + по инструментам {n_tk}")
        data["targeted_new"] = n_tk

        await newsflow.triage(run_id, days)
        await newsflow.characterize_all(run_id, days)
        streams = await newsflow.group(run_id, days)
        items = [x for x in store_v5.news_window(days) if x.get("ai")]
        if not items:
            raise RuntimeError("нет размеченных новостей за окно — проверь ленты и ключ DeepSeek")
        stats = store_v5.news_stats(days)
        stats["streams"] = len(streams.get("streams") or [])
        data.update({"stats": stats, "streams": streams})

        await bus.stage(scope, run_id, "snapshot", "start", detail="цены и астро")
        snap, (astro_full, astro_line) = await asyncio.gather(market_snapshot(), _astro())
        data.update({"snapshot": snap, "astro_line": astro_line})
        await bus.stage(scope, run_id, "snapshot", "done", detail=f"{snap.count(chr(10)) + 1} строк цен")

        # разведка данных (v5.2): FLASH просит недостающее — индексы, соседи, курсы
        sc_text, sc_reqs = await _scout(scope, run_id, "council", _scout_brief("daily", stats, streams, snap))
        data["scout"] = {"requests": sc_reqs, "text": sc_text}
        # аналитик: все новости окна (важные — с фактами), астро, цены, разведка — в разумных пределах
        blocks = await compress.fit({"новости": _streams_full(streams, items), "астро": astro_full,
                                     "цены": snap, "статистика": _stats_text(stats), "разведка": sc_text})
        su = P.analysis_daily(days, blocks["статистика"], blocks["цены"], blocks["астро"], blocks["новости"],
                              extra=blocks["разведка"])
        texts, summary = await _deliberate(scope, run_id, su, _streams_brief(streams), astro_line,
                                           analysis_detail=_sizes("аналитик", blocks, su[1]), prices=blocks["цены"])
        data.update({"texts": texts, "summary": summary})

        data["cards"] = await newsflow.distribute(run_id, summary, days)
        data["tokens"] = _tokens_diff(t0)
        store_v5.council_put(run_id, "daily", data)
        bus.end_run(run_id)
        return data
    except asyncio.CancelledError:
        _cancelled(run_id, "daily", data)
        raise
    except Exception as e:       # noqa: BLE001
        await _fail(scope, run_id, "daily", data, e)
        raise


# ── пересчёт по новым новостям ────────────────────────────────────────────
async def update_with_news(new_ids: list[str], reason: str) -> dict:
    busy = _busy()
    if busy:
        raise RuntimeError(busy)
    prev = latest()
    days = _days((prev or {}).get("data", {}).get("days"))
    scope = "daily"
    run_id = bus.start_run(scope, {"reason": reason, "kind": "update", "days": days})
    t0 = _usage()
    data: dict = {"kind": "update", "reason": reason, "ts": time.time(), "days": days,
                  "time_msk": ai_v5.now_msk_str(), "run_id": run_id, "base_run_id": (prev or {}).get("run_id")}
    try:
        await bus.stage(scope, run_id, "collect", "start", detail="новые новости")
        if new_ids:
            new_items = [x for x in store_v5.news_by_ids(list(new_ids)) if x.get("ai")]
        else:
            new_items = newsflow.fresh_since(float((prev or {}).get("ts") or 0))
        if not new_items:
            raise RuntimeError("новых размеченных новостей с последнего совета нет — пересчитывать нечего")
        await bus.stage(scope, run_id, "collect", "done", n=len(new_items), detail=f"новых {len(new_items)}")
        streams = ((prev or {}).get("data") or {}).get("streams") or {"streams": [], "orphans": []}
        stats = store_v5.news_stats(days)
        stats["streams"] = len(streams.get("streams") or [])
        data.update({"stats": stats, "streams": streams, "new_ids": [x["id"] for x in new_items]})

        await bus.stage(scope, run_id, "snapshot", "start", detail="цены и астро")
        snap, (astro_full, astro_line) = await asyncio.gather(market_snapshot(), _astro())
        data.update({"snapshot": snap, "astro_line": astro_line})
        await bus.stage(scope, run_id, "snapshot", "done")

        prev_text = summary_text(prev) if prev else "совета ещё не было"
        sc_text, sc_reqs = await _scout(scope, run_id, "council", _scout_brief("update", stats, streams, snap,
                                                                                f"Новых новостей {len(new_items)}. Прошлый итог: {prev_text[:300]}"))
        data["scout"] = {"requests": sc_reqs, "text": sc_text}
        blocks = await compress.fit({"итог": prev_text, "новости": newsflow.render(new_items, limit=None, rich=True),
                                     "цены": snap, "астро": astro_full, "разведка": sc_text})
        su = P.analysis_update(blocks["итог"], blocks["новости"], blocks["цены"], blocks["астро"],
                               extra=blocks["разведка"])
        brief = _streams_brief(streams) + "\nНОВЫЕ:\n" + newsflow.render(new_items, limit=None)
        texts, summary = await _deliberate(scope, run_id, su, brief, astro_line,
                                           analysis_detail=_sizes("аналитик (пересчёт)", blocks, su[1]),
                                           prices=blocks["цены"], prev_text=blocks["итог"])
        data.update({"texts": texts, "summary": summary})
        data["cards"] = await newsflow.distribute(run_id, summary, days)
        data["tokens"] = _tokens_diff(t0)
        store_v5.council_put(run_id, "update", data)
        bus.end_run(run_id)
        return data
    except asyncio.CancelledError:
        _cancelled(run_id, "update", data)
        raise
    except Exception as e:       # noqa: BLE001
        await _fail(scope, run_id, "update", data, e)
        raise


# ── этап 2: взгляд человека ───────────────────────────────────────────────
def _compressed_fallback(text: str) -> dict:
    """FLASH не разложил взгляд — текст человека уходит PRO как есть (он уже ≤ PYTHIA_CTX_LIMIT
    после shrink; clip тут — только страховка при провале сжатия)."""
    return {"clean_text": compress.clip(text, _ctx_limit(), "взгляд человека"), "by_asset": {},
            "theses": [], "questions": []}


async def human(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("пустой текст")
    busy = _busy()
    if busy:
        raise RuntimeError(busy)
    try:
        store_v5.kv_set("human_last", text)
    except Exception as e:       # noqa: BLE001
        log.info("kv human_last: %s", str(e)[:100])
    prev = latest()
    days = _days((prev or {}).get("data", {}).get("days"))
    scope = "human"
    run_id = bus.start_run(scope, {"reason": "взгляд человека", "chars": len(text), "days": days})
    t0 = _usage()
    data: dict = {"kind": "human", "reason": "взгляд человека", "ts": time.time(), "days": days,
                  "time_msk": ai_v5.now_msk_str(), "run_id": run_id, "base_run_id": (prev or {}).get("run_id"),
                  "human": {"raw": text, "compressed": {}}}
    try:
        await bus.stage(scope, run_id, "compress", "start", detail=f"сжимаю взгляд ({_n(len(text))} симв.)")
        src = await compress.shrink(text, None, "взгляд человека")      # выше PYTHIA_CTX_LIMIT — FLASH ужмёт
        try:
            obj = await ai_v5.flash_json(*P.human_compress(src), route="human_compress")
            comp = obj if isinstance(obj, dict) and obj.get("clean_text") else _compressed_fallback(src)
        except Exception as e:   # noqa: BLE001
            log.warning("human: сжатие: %s — беру текст как есть", _hum(e))
            await bus.stage(scope, run_id, "compress", "error", detail=_hum(e))
            comp = _compressed_fallback(src)
        data["human"]["compressed"] = comp
        await bus.stage(scope, run_id, "compress", "done", n=len(comp.get("theses") or []),
                        detail=f"тезисов {len(comp.get('theses') or [])}, вопросов {len(comp.get('questions') or [])}")

        streams = ((prev or {}).get("data") or {}).get("streams") or {"streams": [], "orphans": []}
        stats = store_v5.news_stats(days)
        stats["streams"] = len(streams.get("streams") or [])
        data.update({"stats": stats, "streams": streams})

        await bus.stage(scope, run_id, "snapshot", "start", detail="цены и астро")
        snap, (astro_full, astro_line) = await asyncio.gather(market_snapshot(), _astro())
        data.update({"snapshot": snap, "astro_line": astro_line})
        await bus.stage(scope, run_id, "snapshot", "done")

        prev_text = summary_text(prev) if prev else "совета ещё не было"
        fresh = newsflow.fresh_since(float((prev or {}).get("ts") or 0)) if prev else []
        sc_text, sc_reqs = await _scout(scope, run_id, "council", _scout_brief("human", stats, streams, snap,
                                                                                "Взгляд человека: " + str(comp.get("clean_text") or "")[:400]))
        data["scout"] = {"requests": sc_reqs, "text": sc_text}
        blocks = await compress.fit({"итог": prev_text, "цены": snap, "астро": astro_full,
                                     "свежие": newsflow.render(fresh, limit=None, rich=True), "разведка": sc_text})
        su = P.analysis_human(blocks["итог"], comp, blocks["цены"], blocks["астро"], blocks["свежие"],
                              extra=blocks["разведка"])
        texts, summary = await _deliberate(
            scope, run_id, su, _streams_brief(streams), astro_line, human=True,
            analysis_detail=_sizes("аналитик (взгляд)", {"взгляд": P._j(comp), **blocks}, su[1]),
            prices=blocks["цены"], prev_text=blocks["итог"])
        data.update({"texts": texts, "summary": summary})
        data["cards"] = await newsflow.distribute(run_id, summary, days)

        await bus.stage(scope, run_id, "present", "start", detail="рамка для человека")
        data["frame"] = await present_frame("human", {"summary": summary, "texts": texts,
                                                       "human": comp})
        await bus.stage(scope, run_id, "present", "done", detail=(data["frame"].get("headline") or "")[:200])
        data["tokens"] = _tokens_diff(t0)
        store_v5.council_put(run_id, "human", data)
        bus.end_run(run_id)
        return data
    except asyncio.CancelledError:
        _cancelled(run_id, "human", data)
        raise
    except Exception as e:       # noqa: BLE001
        await _fail(scope, run_id, "human", data, e)
        raise


# ── рамка ─────────────────────────────────────────────────────────────────
def _payload_blocks(payload: dict) -> dict[str, str]:
    """Данные рамки блоками {ИМЯ: текст}; без обрезки — пределы держит compress.fit."""
    blocks: dict[str, str] = {}
    for k, v in (payload or {}).items():
        if k == "summary" and isinstance(v, dict):
            blocks["ИТОГ"] = summary_text(v)
        elif k == "texts" and isinstance(v, dict):
            for st in ("verdict", "critique", "analysis"):
                if v.get(st):
                    blocks[st.upper()] = str(v[st])
        elif isinstance(v, str):
            blocks[k.upper()] = v
        else:
            try:
                blocks[k.upper()] = json.dumps(v, ensure_ascii=False)
            except Exception:    # noqa: BLE001
                blocks[k.upper()] = str(v)[:1000]
    return blocks


async def _payload_text(payload: dict) -> str:
    """Рамка — короткий пересказ: каждый блок ≤ PYTHIA_CTX_LIMIT, всё вместе ≤ 2×;
    выше — FLASH ужимает (не clip)."""
    blocks = await compress.fit(_payload_blocks(payload), total=_ctx_limit() * 2, per_block=_ctx_limit())
    return "\n\n".join(f"{k}:\n{v}" for k, v in blocks.items())


async def present_frame(kind: str, payload: dict) -> dict:
    """FLASH(think=True): {"headline","frame","highlights":[{"ticker","side","line"}]}. Сбой → {}."""
    try:
        obj = await ai_v5.flash_json(*P.present_frame(kind, await _payload_text(payload)), think=True,
                                     route="present")
        if not isinstance(obj, dict):
            return {}
        hl = [{"ticker": str(h.get("ticker") or "").upper(), "side": str(h.get("side") or "")[:10],
               "line": str(h.get("line") or "")[:300]}
              for h in (obj.get("highlights") or []) if isinstance(h, dict)]
        return {"headline": str(obj.get("headline") or "")[:200], "frame": str(obj.get("frame") or "")[:3000],
                "highlights": hl, "kind": kind, "ts": time.time()}
    except Exception as e:       # noqa: BLE001
        log.info("present_frame(%s): %s", kind, _hum(e))
        return {}


# ── снимки ────────────────────────────────────────────────────────────────
def latest() -> dict | None:
    try:
        return store_v5.council_latest(None)
    except Exception as e:       # noqa: BLE001
        log.info("latest: %s", str(e)[:100])
        return None


def _brief_row(row: dict | None) -> dict | None:
    if not row:
        return None
    d = row.get("data") or {}
    streams = d.get("streams") or {}
    return {
        "run_id": row.get("run_id"), "kind": row.get("kind"), "ts": row.get("ts"), "status": row.get("status"),
        "reason": d.get("reason"), "time_msk": d.get("time_msk"), "days": d.get("days"),
        "base_run_id": d.get("base_run_id"), "error": d.get("error"),
        "summary": d.get("summary") or {}, "cards": d.get("cards") or [], "stats": d.get("stats") or {},
        "streams": [{k: st.get(k) for k in ("tag", "title", "gist", "heat", "net_tone", "assets")}
                    | {"n": len(st.get("ids") or [])} for st in streams.get("streams") or []],
        "orphans": len(streams.get("orphans") or []),
        "frame": d.get("frame") or {}, "astro_line": d.get("astro_line"), "snapshot": d.get("snapshot"),
        "human": {"compressed": (d.get("human") or {}).get("compressed") or {}} if d.get("human") else None,
        "texts": {k: (v or "")[:_SNAP_TEXT_CAP] for k, v in (d.get("texts") or {}).items()},
        "tokens": d.get("tokens") or {},
    }


def snapshot() -> dict:
    out = {"daily": None, "human": None, "update": None, "latest_kind": None, "news_stats": {},
           "human_last": None, "busy": _busy()}
    days = _days(None)
    for kind in ("daily", "human", "update"):
        try:
            out[kind] = _brief_row(store_v5.council_latest(kind))
        except Exception as e:   # noqa: BLE001
            log.info("snapshot %s: %s", kind, str(e)[:100])
    lt = latest()
    out["latest_kind"] = (lt or {}).get("kind")
    out["latest_run_id"] = (lt or {}).get("run_id")
    try:
        out["news_stats"] = store_v5.news_stats(days)
    except Exception:            # noqa: BLE001
        pass
    try:
        out["human_last"] = store_v5.kv_get("human_last")
    except Exception:            # noqa: BLE001
        pass
    return out


# ── self-test ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import re
    import tempfile
    from pathlib import Path

    store_v5.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
    store_v5._schema()
    events: list[dict] = []

    async def sink(ev):
        events.append(ev)

    bus.set_sink(sink)

    class FakeAI:
        SEM_FLASH = asyncio.Semaphore(4)
        now_msk_str = staticmethod(ai_v5.now_msk_str)
        fmt_ts = staticmethod(ai_v5.fmt_ts)
        calls: list = []
        users: dict = {}          # route → user-промпт последнего PRO-вызова
        _u = {"prompt": 0, "completion": 0}
        big_analysis = 0          # >0 → PRO-аналитик отдаёт простыню такой длины (симв.)

        def usage(self):
            return dict(self._u)

        async def flash_json(self, system, user, *, think=False, route="flash", max_tokens=None):
            self.calls.append(route)
            ids = re.findall(r"^\[(\w{6})\]", user, flags=re.M)
            if route == "triage":
                return {"keep": ids}
            if route == "characterize":
                return {"gist": "g", "one_liner": "Сбер растёт", "tone": 30, "honesty": 70, "hype": 20,
                        "importance": 60, "novelty": 50, "kind": "факт", "horizon": "дни",
                        "expected_effect": "вверх", "effect_strength": 40, "manipulation_risk": 5,
                        "actors": [], "assets": ["SBER"], "sectors": ["банки"], "tags": ["дивиденды"],
                        "why_market": "w", "key_facts": ["прибыль 1,5 трлн", "дивиденд 35 ₽"]}
            if route == "group":
                return {"streams": [{"tag": "сбер", "title": "Сбер", "gist": "g", "ids": ids, "net_tone": 30,
                                     "heat": 70, "assets": ["SBER"]}], "orphans": []}
            if route == "distribute":
                return {"cards": [{"ticker": "SBER", "news_ids": ids[:2], "note": "n"}]}
            if route == "human_compress":
                return {"clean_text": "Сбер вверх", "by_asset": {"SBER": ["вверх"]},
                        "theses": [{"asset": "SBER", "claim": "вверх", "direction": "вверх", "confidence": "средняя"}],
                        "questions": ["когда?"]}
            if route == "present":
                return {"headline": "Сбер лонг", "frame": "потому", "highlights": [{"ticker": "sber", "side": "long", "line": "l"}]}
            if route == "scout":
                assert "ЧТО УЖЕ ПОЛУЧИТ PRO" in user and "новостей к бирже" in user
                return {"requests": [{"kind": "quote", "code": "нефть", "why": "экспортёры"},
                                     {"kind": "quote", "code": "IMOEX", "why": "рынок"}]}
            return {}

        async def flash_text(self, system, user, *, route="flash", **kw):   # compress.shrink → «сжимает» к лимиту
            self.calls.append(route)
            lim = int(user.split("ЛИМИТ: ")[1].split(" ")[0])
            return user.split("\n\nТЕКСТ:\n", 1)[1][:lim]

        async def pro_json(self, system, user, *, route="pro", max_tokens=None):
            self.calls.append(route)
            self.users[route] = user
            self._u["prompt"] += 100
            ids = re.findall(r"\[(\w{6})\]", user)
            s = {"regime": "RISK-ON", "summary": "s", "avoid": [{"ticker": "gazp", "why": "нет"}],
                 "watch": [], "key_times": ["14:00 МСК — ЦБ"],
                 "picks": [{"ticker": "sber", "side": "лонг", "conviction": "72%", "horizon": "день",
                            "why": "w", "trigger": "t", "risk": "r", "news_ids": ids[:1] + ["zzzzzz"]},
                           {"ticker": "GAZP", "side": "flat", "conviction": 10}]}
            if "human_alignment" in system:
                s["human_alignment"] = [{"thesis": "Сбер вверх", "verdict": "согласен", "why": "да"}]
            return s

        async def pro_stream(self, system, user, *, on_think=None, on_text=None, route="pro"):
            self.calls.append(route)
            self.users[route] = user
            assert "сейчас:" in user and "МСК" in user
            await on_think("думаю ")
            ids = re.findall(r"\[(\w{6})\]", user)           # id тянутся через анализ → вердикт
            txt = f"{route}: SBER лонг выше 300 в 14:00 МСК [{ids[0] if ids else 'x' * 6}]"
            txt += " " + f"{route}-длинно " * 3000               # ≈40 000 симв.: ниже PYTHIA_CTX_LIMIT — как есть
            if route == "analysis" and self.big_analysis:
                txt += "\n" + "\n".join(f"абзац {i} " + "а" * 200 for i in range(self.big_analysis // 210))
            for ch in (txt[:10], txt[10:]):
                await on_text(ch)
            return txt

    fake = FakeAI()
    ai_v5 = fake  # noqa: F811
    newsflow.ai_v5 = fake
    compress.ai_v5 = fake
    scout.ai_v5 = fake

    async def fake_scout_price(code, ac):
        assert ac != "index" and code not in ("MX", "RI"), "индексы через MOEX ISS не ходят (v5.3)"
        return {"price": 71.4, "change_pct": -1.8} if code == "BR" else None

    scout.moex.last_price = fake_scout_price
    scout.tinkoff.config.TINKOFF_TOKEN = ""
    config.PYTHIA_CTX_LIMIT = 60_000
    config.PYTHIA_PROMPT_SOFT = 300_000

    async def fake_snapshot():
        return "SBER Сбербанк 300 (+1.0%)"

    market_snapshot = fake_snapshot  # noqa: F811

    async def fake_astro():
        return "астро недоступно", ""

    _real_astro = _astro
    _astro = fake_astro  # noqa: F811

    async def fake_tk(ticker, name, *, days=14, fallback_days=90, limit=22, asset_class="share"):
        return [], "окно 3 дн"                                  # Google News пуст, в сеть не ходим

    newsflow.news.fetch_for_ticker = fake_tk

    now = time.time()

    class N:
        def __init__(self, i):
            self.source, self.title, self.summary = "rbc", f"Сбербанк новость {i}", "текст"
            self.link, self.published, self.ts = f"http://x/{i}", "", now - i * 600

        def to_dict(self):
            return {"source": self.source, "title": self.title, "summary": self.summary,
                    "link": self.link, "published": self.published}

    async def fake_fetch(*, force=False, days=None):
        return [N(i) for i in range(4)]

    newsflow.news.fetch_news = fake_fetch

    async def main():
        # daily от начала до конца
        d = await daily(3, "тест")
        row = store_v5.council_latest(None)
        assert row and row["kind"] == "daily" and row["run_id"] == d["run_id"]
        s = row["data"]["summary"]
        assert s["regime"] == "risk-on" and len(s["picks"]) == 1, s
        p = s["picks"][0]
        assert p["ticker"] == "SBER" and p["side"] == "long" and p["conviction"] == 72
        assert len(p["news_ids"]) == 1 and len(p["news_ids"][0]) == 40, p["news_ids"]
        assert s["avoid"] == [{"ticker": "GAZP", "why": "нет"}]
        cards = row["data"]["cards"]
        assert len(cards) == 1 and cards[0]["ticker"] == "SBER" and cards[0]["news_count"] == 2, cards
        assert cards[0]["name"] == "Сбербанк" and cards[0]["tone_avg"] == 30
        assert set(d["texts"]) == {"analysis", "critique", "verdict"} and d["texts"]["verdict"].startswith("verdict")
        assert d["stats"]["relevant"] == 4 and d["stats"]["streams"] == 1 and d["snapshot"].startswith("SBER")
        assert d["tokens"]["prompt"] == 100 and bus.active("daily") is None
        assert bus.run(d["run_id"])["texts"]["analysis"].startswith("analysis")
        assert bus.run(d["run_id"])["thinks"]["critique"] == "думаю "
        stages = [(e["stage"], e["status"]) for e in events if e["type"] == "v5"]
        for st in ("collect", "triage", "characterize", "group", "snapshot", "analysis", "critique",
                   "verdict", "summary", "distribute"):
            assert (st, "done") in stages, st
        assert [e["type"] for e in events if e["type"] in ("text", "think")][:2] == ["think", "text"]
        assert fake.calls.count("group") == 1 and fake.calls.count("characterize") == 4
        # v5.2 разведка: FLASH спрошен, данные дошли до аналитика блоком, стадия scout в шине, итог хранит запросы
        assert fake.calls.count("scout") == 1 and [r["code"] for r in d["scout"]["requests"]] == ["BR", "MX"], d["scout"]
        assert "ДОПОЛНИТЕЛЬНЫЕ ДАННЫЕ (разведка FLASH" in fake.users["analysis"] and "BR (Brent — нефть): 71.4 (-1.80% за день) · moex" in fake.users["analysis"]
        assert "MX (Индекс МосБиржи): нет данных — индексных данных нет: фьючерс на индекс только через Tinkoff, токена нет" in fake.users["analysis"]
        assert ("scout", "done") in stages, stages
        # стадия Google News по инструментам прошла (пусто, но без сети и без ошибки)
        col = [e for e in events if e["type"] == "v5" and e["stage"] == "collect" and e["status"] == "done"]
        assert col[-1]["detail"] == "новых новостей 4 + по инструментам 0" and d["targeted_new"] == 0, col
        # кресла читают друг друга целиком: критик — весь анализ, вердикт — анализ и критику, итог — весь вердикт
        assert len(d["texts"]["analysis"]) > 30_000
        assert d["texts"]["analysis"] in fake.users["critique"]
        assert d["texts"]["analysis"] in fake.users["verdict"] and d["texts"]["critique"] in fake.users["verdict"]
        assert d["texts"]["verdict"] in fake.users["summary"]
        assert "обрезано" not in fake.users["critique"] + fake.users["verdict"] + fake.users["summary"]
        assert "shrink" not in fake.calls, "ниже пределов ничего не сжимается"
        # аналитик видит важные новости со строкой «факты: … · суть: …»; стадии несут размеры
        assert "\n    факты: прибыль 1,5 трлн; дивиденд 35 ₽ · суть: g" in fake.users["analysis"]
        an_start = [e for e in events if e["type"] == "v5" and e["stage"] == "analysis" and e["status"] == "start"][-1]
        assert an_start["detail"].startswith("аналитик: промпт ") and "симв. — новости " in an_start["detail"] \
            and "астро " in an_start["detail"] and "цены " in an_start["detail"], an_start["detail"]
        assert int(an_start["detail"].split("промпт ")[1].split(" симв.")[0].replace(" ", "")) == len(fake.users["analysis"])
        for st in ("critique", "verdict", "summary"):
            ev = [e for e in events if e["type"] == "v5" and e["stage"] == st and e["status"] == "start"][-1]
            assert "симв." in ev["detail"] and "промпт" in ev["detail"], (st, ev["detail"])
        an_done = [e for e in events if e["type"] == "v5" and e["stage"] == "analysis" and e["status"] == "done"][-1]
        assert an_done["detail"].startswith(f"ответ {_n(len(d['texts']['analysis']))} симв."), an_done["detail"]
        # _astro: render_compact (если есть) без обрезки, иначе render_for_ai; сбой → «астро недоступно»
        async def fake_actx():
            return {"ok": 1}
        astro.acontext = fake_actx
        astro.render_for_ai = lambda ctx: "ПОЛНЫЙ " + "п" * 6000
        astro.short_line = lambda ctx: "луна растёт"
        astro.render_compact = lambda ctx: "КОМПАКТ " + "к" * 6000
        full_a, line_a = await _real_astro()
        assert full_a.startswith("КОМПАКТ") and len(full_a) > 6000 and line_a == "луна растёт"
        astro.render_compact = None
        full_a, _ = await _real_astro()
        assert full_a.startswith("ПОЛНЫЙ") and len(full_a) > 6000 and "обрезано" not in full_a
        async def bad_actx():
            raise RuntimeError("эфемериды")
        astro.acontext = bad_actx
        assert await _real_astro() == ("астро недоступно", "")
        # summary_text: по summary, по data, по строке council_latest
        st_ = summary_text(row)
        assert "SBER long 72% день" in st_ and "Избегать: GAZP" in st_ and len(st_) <= 8000
        # v5.4.2: строка council_latest → шапка с видом, временем МСК и возрастом; голый summary — как раньше
        head_, bare_ = st_.split("\n", 1)
        assert head_.startswith("Совет daily от ") and head_.endswith("МСК (0 мин назад) — общий по рынку"), head_
        assert summary_text(s) == bare_ and bare_.startswith("Режим: risk-on") and "Входов совет не назвал" not in bare_
        assert summary_text(row["data"]).startswith("Совет daily от ") and summary_text({}) == "совета ещё не было"
        # пик GAZP «flat» не пропал молча: заметка в стадии итога
        notes_ = [e for e in events if e["type"] == "v5" and e["stage"] == "summary" and e["status"] == "progress"]
        assert notes_ and notes_[0]["detail"] == "пик без стороны: GAZP «flat»", notes_
        sm_done = [e for e in events if e["type"] == "v5" and e["stage"] == "summary" and e["status"] == "done"][0]
        assert "пик без стороны: GAZP «flat»" in sm_done["detail"], sm_done["detail"]
        # стороны пиков — терпимо; спорное и отрицание — в dropped, не угадываем
        drop_: list = []
        vs_ = validate_summary({"regime": "risk-off", "picks": [
            {"ticker": "sber", "side": "buy_now"}, {"ticker": "gazp", "side": "ЛОНГ (покупка)"},
            {"ticker": "lkoh", "side": "LONG "}, {"ticker": "rosn", "side": "Шорт"}, {"ticker": "vtbr", "side": "продажа"},
            {"ticker": "mgnt", "side": "flat"}, {"ticker": "tatn", "side": "лонг или шорт"}, {"ticker": "plzl", "side": ""}]},
            dropped=drop_)
        assert [(p["ticker"], p["side"]) for p in vs_["picks"]] == [
            ("SBER", "long"), ("GAZP", "long"), ("LKOH", "long"), ("ROSN", "short"), ("VTBR", "short")], vs_["picks"]
        assert drop_ == ["MGNT «flat»", "TATN «лонг или шорт»", "PLZL «»"], drop_
        assert "regime_raw" not in vs_ and vs_["regime"] == "risk-off"
        assert validate_summary({"picks": [{"ticker": "x", "side": "flat"}]})["picks"] == []   # без dropped — как раньше
        # режим не распознан: по умолчанию «смешанно» + regime_raw; пустые picks — явной строкой
        v0 = validate_summary({})
        assert v0["regime"] == "смешанно" and v0["regime_raw"] == "" and v0["picks"] == []
        t0_ = summary_text(v0)
        assert t0_.startswith("Режим: режим не распознан (совет его не назвал)") and "Входов совет не назвал" in t0_, t0_
        vb_ = validate_summary({"regime": "Bullish", "picks": []})
        assert vb_["regime"] == "смешанно" and "«bullish»" in summary_text(vb_)
        old_ = {"kind": "update", "ts": time.time() - 26 * 3600, "run_id": "r0",
                "data": {"kind": "update", "summary": {"regime": "risk-off", "summary": "вне рынка", "picks": []}}}
        to_ = summary_text(old_)
        assert to_.startswith("Совет update от ") and "(26 ч назад) — общий по рынку" in to_.splitlines()[0], to_
        assert "Режим: risk-off" in to_ and "Входов совет не назвал" in to_ and "режим не распознан" not in to_
        assert summary_text({"data": {"summary": {"regime": "risk-on"}}}).startswith("Режим: risk-on"), "без ts/kind — без шапки"
        # human
        h = await human("  Сбер пойдёт вверх, ставка не помеха ")
        assert h["kind"] == "human" and h["frame"]["headline"] == "Сбер лонг"
        assert h["frame"]["highlights"][0]["ticker"] == "SBER"
        assert h["summary"]["human_alignment"][0]["verdict"] == "согласен"
        assert h["human"]["compressed"]["theses"][0]["asset"] == "SBER" and h["human"]["raw"].startswith("Сбер")
        assert store_v5.kv_get("human_last").startswith("Сбер") and latest()["kind"] == "human"
        # update_with_news: новых нет → ошибка со status=error
        try:
            await update_with_news([], "тест")
            raise AssertionError("ожидалась ошибка")
        except RuntimeError as e:
            assert "нечего" in str(e)
        assert store_v5.council_list(1)[0]["status"] == "error" and latest()["kind"] == "human"
        store_v5.news_upsert_raw([{"source": "s", "title": "Сбер ещё", "link": "http://n", "ts": time.time()}])
        rid = bus.start_run("watch")
        await newsflow.triage(rid, 1)
        await newsflow.characterize_all(rid, 1)
        bus.end_run(rid)
        u = await update_with_news([], "новые")
        assert u["kind"] == "update" and len(u["new_ids"]) == 1 and u["summary"]["picks"][0]["ticker"] == "SBER"
        assert latest()["kind"] == "update"
        # snapshot для /api/v5/state
        sn = snapshot()
        assert sn["latest_kind"] == "update" and sn["daily"]["summary"]["regime"] == "risk-on"
        assert sn["human"]["frame"]["headline"] == "Сбер лонг" and sn["human"]["human"]["compressed"]["clean_text"]
        assert sn["daily"]["streams"][0]["n"] == 4 and sn["news_stats"]["relevant"] == 5 and sn["human_last"]
        assert sn["daily"]["texts"]["verdict"].startswith("verdict") and sn["busy"] is None
        # ── v5.1 пределы: PRO-аналитик отдал 150 000 симв. → FLASH ужимает до PYTHIA_CTX_LIMIT ──
        fake.big_analysis = 150_000
        fake.calls.clear()
        d2 = await daily(3, "простыня")
        fake.big_analysis = 0
        assert len(d2["texts"]["analysis"]) >= 150_000, len(d2["texts"]["analysis"])
        assert "shrink" in fake.calls, "FLASH-сжатие должно было вызваться"
        an_block = fake.users["critique"].split("АНАЛИЗ:\n", 1)[1].split("\n\nПОТОКИ КРАТКО:")[0]
        assert len(an_block) <= 60_000 and "обрезано" not in an_block, len(an_block)
        assert an_block.startswith("analysis: SBER лонг выше 300"), an_block[:60]
        assert an_block in fake.users["verdict"] and "обрезано" not in fake.users["verdict"]
        assert d2["texts"]["verdict"] in fake.users["summary"]          # вердикт (≈40 000) — целиком
        assert d2["summary"]["picks"][0]["ticker"] == "SBER"
        # ── 200 новостей: аналитик видит все 200 sid и факты ──
        store_v5.DB_PATH = Path(tempfile.mkdtemp()) / "t200.db"
        store_v5._schema()
        fake.calls.clear()
        big = [N(i) for i in range(200)]

        async def fetch200(*, force=False, days=None):
            return list(big)

        newsflow.news.fetch_news = fetch200
        d3 = await daily(3, "200 новостей")
        assert fake.calls.count("triage") == 4 and fake.calls.count("characterize") == 200
        assert fake.calls.count("group") == 2 and fake.calls.count("group_merge") == 1
        assert d3["stats"]["relevant"] == 200 and d3["stats"]["streams"] == 2
        sids = [newsflow.sid(x["id"]) for x in store_v5.news_window(3)]
        assert len(sids) == 200 and all(f"[{sid_}]" in fake.users["analysis"] for sid_ in sids)
        assert fake.users["analysis"].count("факты: прибыль 1,5 трлн") == 200
        assert "shrink" not in fake.calls and "обрезано" not in fake.users["analysis"]
        an_start = [e for e in events if e["type"] == "v5" and e["stage"] == "analysis" and e["status"] == "start"][-1]
        assert "симв." in an_start["detail"] and "новости " in an_start["detail"], an_start["detail"]
        newsflow.news.fetch_news = fake_fetch
        # «уже идёт»
        rid = bus.start_run("daily")
        try:
            await daily()
            raise AssertionError("ожидалась ошибка «уже идёт»")
        except RuntimeError as e:
            assert "уже идёт" in str(e)
        bus.end_run(rid)
        # present_frame никогда не роняет
        async def boom(*a, **k):
            raise RuntimeError("сеть")
        fake.flash_json = boom
        assert await present_frame("mission", {"exec": {"do": "BUY"}, "text": "x"}) == {}
        # сбой PRO → status error + end_run(error) + raise
        fake.pro_stream = boom
        try:
            await daily(3, "сбой")
            raise AssertionError("ожидалась ошибка")
        except RuntimeError:
            pass
        last = store_v5.council_list(1)[0]
        assert last["status"] == "error" and bus.active("daily") is None
        assert bus.run(last["run_id"])["error"] == "сеть"

    asyncio.run(main())
    print("council self-test OK")
