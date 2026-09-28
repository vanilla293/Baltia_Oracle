# -*- coding: utf-8 -*-
"""ПИФИЯ v5.2 — чат с ИИ (панель у правого края, §6 контракта).

Владелец задаёт любой вопрос; ИИ (PRO по умолчанию, FLASH — быстрый ответ) отвечает по
живому состоянию терминала: миссия и пилот (позиция на счёте, план, триггер и аварийный
трос, что даёт биржа), итог последнего совета, живой рынок по инструменту миссии, заметки
дозора, свежие новости, небо, разведка FLASH под вопрос (индексный фьючерс, соседи, курсы),
связанные бумаги инструмента миссии (v5.3, correlate: корреляции с сектором и макро), история
диалога. Каждый вопрос — новый чат ИИ (system + user), история идёт блоком «ДИАЛОГ».

Ответ стримится в шину: scope "chat", stage "answer" (type text / think); стадии
context → scout → answer → end (end шлёт bus.end_run). История — store_v5.kv
"chat_history" (последние HISTORY_KEEP реплик).

POST   /api/v5/chat            {text, model?: "pro"|"flash"} → {"ok","run_id","model"} · 409 — ИИ ещё отвечает
GET    /api/v5/chat/history                                 → {"items":[{role,text,ts,model?,run_id?,error?}], "model", "busy", "run_id"}
DELETE /api/v5/chat/history                                 → {"ok": true}
GET    /api/v5/chat/status                                  → {"busy", "run_id", "model"}
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import os
import time
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from . import ai_v5, bus, compress, config, store_v5
from .prompts_mission import SYSTEM_MAX_LINES, VOICE, system_lines

log = logging.getLogger("pythia.chat")
router = APIRouter()

KV_KEY = "chat_history"
HISTORY_KEEP = 60             # реплик в хранилище
DIALOG_LAST = 12              # реплик в промпт (блок «ДИАЛОГ»)
TEXT_MAX = 4000               # длина вопроса
CTX_TIMEOUT = 20.0            # живой рынок / новости — не дольше
SCOUT_TIMEOUT = 120.0          # разведка FLASH под вопрос
ANSWER_TIMEOUT = 1800.0       # ответ ИИ: 32K+ токенов с размышлением PRO ≈ до 18 мин (24.09.2026)
HEAD_RESERVE = 6_000          # запас под шапку промпта при compress.fit

_STATE: dict[str, Any] = {"task": None, "run_id": None, "model": None, "cleared_ts": 0.0}
_STUBS: dict[str, Any] = {}   # self-тест: подмены модулей по имени

_PHASE_RU = {"council": "совет думает", "armed": "засада", "entering": "вхожу", "in_position": "в позиции",
             "stopped": "пилот остановлен", "panic": "паника", "idle": "вне рынка / жду план", "error": "ошибка"}
_SILENT = ("НЕТ_ОТВЕТА", "НЕ_РАЗОБРАН")   # v5.4.2: сбой ИИ — «решения не было», не ЖДАТЬ


def _mod(name: str):
    if name in _STUBS:
        return _STUBS[name]
    try:
        return importlib.import_module(f"backend.{name}")
    except Exception:                                    # noqa: BLE001
        try:
            return importlib.import_module(f".{name}", package=__package__)
        except Exception as e:                           # noqa: BLE001
            log.info("модуль %s недоступен: %s", name, str(e)[:80])
            return None


def _f(x, d=None):
    try:
        if x is None or x == "":
            return d
        return float(x)
    except (TypeError, ValueError):
        return d


def _humanize(e: BaseException) -> str:
    if isinstance(e, asyncio.TimeoutError):
        return "ИИ не ответил вовремя"
    return (str(e)[:200] or type(e).__name__)


def busy() -> bool:
    t = _STATE.get("task")
    return bool(t) and not t.done()


# ── история ───────────────────────────────────────────────────────────────────
def history() -> list[dict]:
    try:
        h = store_v5.kv_get(KV_KEY, [])
    except Exception as e:                               # noqa: BLE001
        log.info("история чата недоступна: %s", str(e)[:80])
        return []
    return [x for x in h if isinstance(x, dict)] if isinstance(h, list) else []


def _save(h: list[dict]) -> None:
    try:
        store_v5.kv_set(KV_KEY, h[-HISTORY_KEEP:])
    except Exception as e:                               # noqa: BLE001
        log.warning("история чата не сохранилась: %s", str(e)[:80])


def clear_history() -> None:
    _save([])
    _STATE["cleared_ts"] = time.time()   # ответ прогона, начатого раньше очистки, в пустую историю не ложится


def _dialog_text(h: list[dict]) -> str:
    rows = []
    for x in h[-DIALOG_LAST:]:
        who = "Владелец" if x.get("role") == "user" else "ПИФИЯ"
        rows.append(f"{who} ({ai_v5.fmt_ts(x.get('ts'))}): {str(x.get('text') or '').strip()[:20000]}")
    return "\n".join(rows)


# ── контекст ──────────────────────────────────────────────────────────────────
def _said(rec: dict, key: str) -> str:
    """Решение узла строкой «ЖДЁМ — почему». Сбой ИИ (silent, НЕТ_ОТВЕТА, НЕ_РАЗОБРАН) — «ответа не было —
    решения не было», а не его ЖДАТЬ (v5.4.2)."""
    d = str(rec.get(key) or "")
    why = str(rec.get("why") or "")[:200]
    if rec.get("silent") or d.upper() in _SILENT:
        what = "ответ не разобран" if d.upper() == "НЕ_РАЗОБРАН" else "ответа не было"
        return f"{what} — решения не было" + (f" ({why})" if why else "")
    return f"{d} — {why}"


def _mission_text() -> tuple[str, dict]:
    """Состояние миссии и пилота одним блоком + мета (тикер, класс) для живого рынка."""
    ms = _mod("mission")
    meta: dict = {}
    if not ms:
        return "", meta
    try:
        snap = ms.snapshot() or {}
    except Exception as e:                               # noqa: BLE001
        return f"(миссия недоступна: {str(e)[:80]})", meta
    missions = snap.get("missions") or {}
    act = snap.get("active")
    st = missions.get(act) if act else None
    if not st:
        if missions:
            rows = [f"{t}: {_PHASE_RU.get(x.get('phase'), x.get('phase'))} — {str(x.get('note') or '')[:160]}"
                    for t, x in missions.items() if isinstance(x, dict)]
            return "Активной миссии нет. Прошлые: " + "; ".join(rows), meta
        return "Миссии нет: пилот не запущен, позиций терминал сейчас не ведёт", meta
    meta = {"ticker": st.get("ticker"), "name": st.get("name"), "asset_class": st.get("asset_class")}
    L = [f"Миссия {st.get('ticker')} ({st.get('name')}, {st.get('asset_class')}), режим игры {st.get('play')}, "
         f"фаза: {_PHASE_RU.get(st.get('phase'), st.get('phase'))}, цена {st.get('price')}, "
         f"запущена {ai_v5.fmt_ts(st.get('started_ts'))}"]
    if st.get("note"):
        L.append("Заметка: " + str(st["note"])[:300])
    if st.get("error"):
        L.append("Ошибка: " + str(st["error"])[:300])
    ex = st.get("exec") or {}
    if ex:
        do = str(ex.get("do") or "").upper()
        if do == "WAIT":                         # v5.4.2: WAIT — чего ждал совет и уровни, без «вход сейчас … стоп None»
            lv = ", ".join(f"{x:g}" if isinstance(x, (int, float)) else str(x) for x in (ex.get("levels") or [])) or "—"
            L.append(f"Приказ совета ({ai_v5.fmt_ts(st.get('exec_ts'))}): WAIT — совет ждал: "
                     f"{str(ex.get('wait_for') or '').strip()[:300] or '—'}; уровни: {lv}"
                     + (f"; почему: {str(ex['why'])[:300]}" if ex.get("why") else ""))
        elif do == "CLOSE":
            L.append(f"Приказ совета ({ai_v5.fmt_ts(st.get('exec_ts'))}): CLOSE — закрыть позицию"
                     + (f" — {str(ex['why'])[:300]}" if ex.get("why") else ""))
        else:
            L.append(f"Приказ совета: {ex.get('do')} вход {ex.get('entry') if ex.get('entry') is not None else 'сейчас'}"
                     f" ({ex.get('entry_kind') or '—'}) тейк {ex.get('take')} стоп {ex.get('invalidation')}"
                     f" — {str(ex.get('why') or '')[:300]}")
        if ex.get("plan"):
            L.append("План ведения: " + str(ex["plan"])[:600])
    if st.get("account_pos"):
        L.append(str(st["account_pos"])[:300])
    p = st.get("pilot") or {}
    if p:
        pos = p.get("position")
        if pos:
            L.append(f"ПОЗИЦИЯ ПИЛОТА: {pos.get('side')} {pos.get('lots')} лот @{pos.get('entry')}, "
                     f"плавающий P/L {pos.get('floating')} ₽, триггер (мягкий стоп) {pos.get('invalidation')}, "
                     f"аварийный трос биржи {pos.get('hard_stop')}, тейк {pos.get('take')}"
                     + (f"; у троса держал {pos.get('holds')} раз" if pos.get("holds") else "")
                     + ("; уровни временные — принята со счёта, ждёт PRO" if pos.get("levels_placeholder") else ""))
        else:
            pl = p.get("plan") or {}
            L.append("Позиции у пилота нет" + (f"; план: {pl.get('side')} вход "
                                                f"{pl.get('entry') if pl.get('entry') is not None else 'сейчас'} "
                                                f"стоп {pl.get('invalidation')} тейк {pl.get('take')}" if pl else ""))
        L.append(f"Пилот: {p.get('state')} — {str(p.get('last_action') or '')[:300]}; брокер {p.get('mode')}, "
                 f"весь счёт: {'да' if p.get('adopt_account') else 'нет'}, размер даёт биржа: "
                 f"{'да' if p.get('sized_by_broker') else 'нет'}, мягкий стоп: {'да' if p.get('soft_stop') else 'нет'}")
        acc = p.get("account") or {}
        if acc:
            names = {"free": "свободно ₽", "liquid": "ликвидный портфель ₽", "missing": "не хватает ₽",
                     "max_buy": "биржа даёт купить лотов", "max_sell": "биржа даёт продать лотов",
                     "sufficiency": "достаточность", "total": "всего ₽"}
            L.append("Счёт: " + ", ".join(f"{names.get(k, k)} {v}" for k, v in acc.items()
                                          if v is not None and k in names))
        ks = p.get("killswitch") or {}
        if ks:
            L.append(f"Killswitch: {'ЗАБЛОКИРОВАН — ' + str(ks.get('reason') or '') if ks.get('locked') else 'ок'}; "
                     f"P/L сессии {p.get('session_pnl')} ₽ за {p.get('trades')} сделок")
        if p.get("next_review_in_s") is not None:
            L.append(f"Следующая перепроверка дежурного PRO через {int(_f(p['next_review_in_s'], 0) // 60)} мин")
        for g in (p.get("guards") or [])[-3:]:
            L.append(f"Ответ у {'тейка' if g.get('side') == 'take' else 'троса'} {ai_v5.fmt_ts(g.get('ts'))}: "
                     f"{_said(g, 'decision')}")
    for r in (st.get("reviews") or [])[-5:]:
        L.append(f"Перепроверка {ai_v5.fmt_ts(r.get('ts'))}: {_said(r, 'choice')}")
    for h in (st.get("handoffs") or [])[-3:]:
        L.append(f"Передача {ai_v5.fmt_ts(h.get('ts'))} ({h.get('kind')}): {str(h.get('reason') or '')[:200]}")
    tr = st.get("trades") or {}
    if tr.get("count"):
        L.append(f"Сделок по миссии: {tr.get('count')}, P/L {tr.get('pnl')} ₽")
    mem = st.get("memory") if isinstance(st.get("memory"), dict) else None      # v5.3: память миссии одним абзацем
    if mem and str(mem.get("text") or "").strip():
        L.append(f"ПАМЯТЬ МИССИИ (прошлые этапы, сведено {ai_v5.fmt_ts(mem.get('ts'))}): {str(mem['text']).strip()[:2000]}")
    for x in (st.get("explain") or [])[-3:]:                                   # v5.3: последние объяснения толмача
        if isinstance(x, dict) and x.get("text"):
            L.append(f"Толмач {ai_v5.fmt_ts(x.get('ts'))} ({x.get('title')}): {str(x['text'])[:600]}")
    sc = st.get("scout") or {}
    if isinstance(sc, dict) and sc.get("text"):
        L.append("Разведка миссии (последняя):\n" + str(sc["text"])[:2000])
    return "\n".join(L), meta


def _council_text() -> str:
    """Итог общего совета. v5.4.2: summary_text получает строку council_latest — шапку с видом, временем МСК
    и возрастом пишет он сам («Совет daily от … (N ч назад) — общий по рынку»)."""
    c = _mod("council")
    if not c:
        return ""
    try:
        latest = c.latest()
        if not latest:
            return "совета ещё не было"
        return c.summary_text(latest) or ""
    except Exception as e:                               # noqa: BLE001
        return f"(итог совета недоступен: {str(e)[:80]})"


def _watch_text() -> str:
    try:
        notes = store_v5.watch_list(10) or []
    except Exception as e:                               # noqa: BLE001
        log.info("заметки дозора: %s", str(e)[:80])
        return ""
    L = []
    for n in notes:
        d = n.get("data") if isinstance(n, dict) and isinstance(n.get("data"), dict) else n
        if isinstance(d, dict):
            L.append(f"{ai_v5.fmt_ts(d.get('ts') or (n.get('ts') if isinstance(n, dict) else None))} · "
                     f"серьёзность {d.get('severity')} · {str(d.get('note') or '')[:300]} · "
                     f"{', '.join(str(a) for a in (d.get('affected') or []))}")
    return "\n".join(L)


def _news_text(meta: dict) -> str:
    nf = _mod("newsflow")
    if not nf:
        return ""
    out = []
    try:
        fresh = nf.fresh_since(time.time() - 6 * 3600) or []
        if fresh:
            out.append("— за последние 6 часов:\n" + nf.render(fresh[:40], rich=True))
    except Exception as e:                               # noqa: BLE001
        log.info("новости для чата: %s", str(e)[:80])
    if meta.get("ticker"):
        try:
            own = nf.news_for_ticker(meta["ticker"], meta.get("name") or meta["ticker"], days=3, limit=25) or []
            if own:
                out.append(f"— по {meta['ticker']}:\n" + nf.render(own, rich=True))
        except Exception as e:                           # noqa: BLE001
            log.info("новости по тикеру для чата: %s", str(e)[:80])
    return "\n\n".join(out)


async def _light_text(meta: dict) -> str:
    if not meta.get("ticker"):
        return ""
    mc = _mod("market_ctx")
    if not mc:
        return ""
    figi = None
    try:
        ms = _mod("mission")
        m = (getattr(ms, "_M", None) or {}).get(meta["ticker"]) if ms else None
        figi = getattr(getattr(m, "pilot", None), "figi", None)
    except Exception:                                    # noqa: BLE001
        figi = None
    try:
        lt = await asyncio.wait_for(mc.light(meta["ticker"], figi, meta.get("asset_class") or "share"), CTX_TIMEOUT)
        return (lt or {}).get("text") or ""
    except Exception as e:                               # noqa: BLE001
        return f"(живой рынок недоступен: {_humanize(e)[:80]})"


def _astro_text() -> str:
    a = _mod("api_v5")
    try:
        return a._astro_line() if a else ""
    except Exception:                                    # noqa: BLE001
        return ""


async def _scout_text(run_id: str, question: str, meta: dict, mission_line: str) -> tuple[str, list]:
    sc = _mod("scout")
    if not sc or not getattr(config, "PYTHIA_SCOUT", True):
        return "", []
    brief = (f"Вопрос владельца в чате: {question[:600]}\n"
             + (f"Миссия: {mission_line[:300]}\n" if mission_line else "")
             + "PRO получит: состояние миссии и пилота, итог совета, живой рынок, дозор, новости, небо.")
    try:
        return await asyncio.wait_for(sc.run("chat", run_id, "chat", brief, meta.get("ticker")), SCOUT_TIMEOUT)
    except Exception as e:                               # noqa: BLE001
        log.info("разведка для чата: %s", str(e)[:80])
        return "", []


async def _partners_text(meta: dict) -> str:
    """Связанные бумаги инструмента миссии (correlate, кэш 6 ч): блок «СВЯЗАННЫЕ БУМАГИ»; сбой → ''."""
    c = _mod("correlate")
    t = (meta or {}).get("ticker")
    if not c or not t or not getattr(config, "PYTHIA_PARTNERS", True):
        return ""
    try:
        items = await asyncio.wait_for(c.partners(t, meta.get("asset_class") or "share"), CTX_TIMEOUT)
        return c.text(items, t)
    except Exception as e:                               # noqa: BLE001
        log.info("связанные бумаги для чата: %s", str(e)[:80])
        return ""


async def _fit(blocks: dict[str, str]) -> dict[str, str]:
    src = {k: (v or "") for k, v in blocks.items()}
    try:
        total = max(10_000, compress.prompt_soft() - HEAD_RESERVE)
        return await compress.fit(src, total=total, per_block=compress.ctx_limit())
    except Exception as e:                               # noqa: BLE001
        log.warning("сжатие блоков чата не удалось (%s) — блоки уходят как есть", str(e)[:100])
        return src


# ── промпт ────────────────────────────────────────────────────────────────────
def _block(title: str, body: str) -> list[str]:
    body = (body or "").strip()
    return ["", f"═══ {title} ═══", body] if body else []


def prompt(question: str, blocks: dict[str, str], model: str) -> tuple[str, str]:
    """system ≤ 20 строк (v5.4.1: с голосом 4.5.4 — prompts_mission.VOICE), свободный ответ, числа только из данных."""
    system = (
        f"Ты — ПИФИЯ, ИИ терминала владельца: совет по рынку, миссия по инструменту, пилот на счёте "
        f"Tinkoff. Сейчас {ai_v5.now_msk_str()}.\n"
        f"{VOICE}\n"
        "Владелец задаёт любой вопрос в чате панели. Отвечай свободно и по делу, как коллега за соседним "
        "столом: коротко на короткий вопрос, подробно — если просят разбор; вскрывай кухню и манипуляции, где они есть.\n"
        "Ниже дано: состояние миссии и пилота (позиция, план, триггер мягкого стопа, аварийный трос, что "
        "даёт биржа, память миссии и объяснения толмача), итог совета, живой рынок, заметки дозора, свежие "
        "новости, небо, данные разведки под вопрос, связанные бумаги (корреляции), история диалога.\n"
        "Числа только из данных; чего нет — так и скажи, не выдумывай. Из чата приказы бирже не идут: если "
        "владелец хочет действие (закрыть, добрать, позвать совет) — скажи, какой кнопкой это делается "
        "(Миссия → ПАНИКА / ПЕРЕСМОТР / СТОП / ПРОДОЛЖИТЬ) и что бы ты советовал.\n"
        "Формат: живой текст без простыней заголовков; список — только когда перечисляешь; "
        f"отвечаешь как {'PRO (думающая модель)' if model == 'pro' else 'FLASH (быстрая модель)'}.")
    parts = [f"ВРЕМЯ: {ai_v5.now_msk_str()}"]
    parts += _block("ДИАЛОГ (последние реплики)", blocks.get("dialog", ""))
    parts += _block("МИССИЯ И ПИЛОТ", blocks.get("mission", ""))
    parts += _block("ИТОГ СОВЕТА", blocks.get("council", ""))
    parts += _block("ЖИВОЙ РЫНОК ПО ИНСТРУМЕНТУ МИССИИ", blocks.get("light", ""))
    parts += _block("ЗАМЕТКИ ДОЗОРА", blocks.get("watch", ""))
    parts += _block("НОВОСТИ (время МСК)", blocks.get("news", ""))
    parts += _block("НЕБО", blocks.get("astro", ""))
    parts += _block("ДАННЫЕ РАЗВЕДКИ ПОД ВОПРОС (индексный фьючерс, соседи, курсы)", blocks.get("scout", ""))
    parts += _block("СВЯЗАННЫЕ БУМАГИ ИНСТРУМЕНТА МИССИИ (корреляции)", blocks.get("partners", ""))
    parts += ["", f"ВОПРОС ВЛАДЕЛЬЦА: {question.strip()}", "Ответь свободно, по делу и по данным выше."]
    return system, "\n".join(parts)


# ── прогон ────────────────────────────────────────────────────────────────────
async def _run(run_id: str, text: str, model: str, asked_ts: float | None = None) -> None:
    t0 = time.time()
    asked_ts = asked_ts or t0                       # момент приёма вопроса: очистка истории позже него — ответ не пишем
    answer, err, reqs = "", None, []
    try:
        await bus.stage("chat", run_id, "context", "start", detail="собираю состояние терминала")
        mission_txt, meta = _mission_text()
        light_txt, news_txt, partners_txt = await asyncio.gather(
            _light_text(meta), asyncio.to_thread(_news_text, meta), _partners_text(meta))
        blocks = {"dialog": _dialog_text(history()[:-1]), "mission": mission_txt, "council": _council_text(),
                  "light": light_txt, "watch": _watch_text(), "news": news_txt, "astro": _astro_text(),
                  "partners": partners_txt}
        await bus.stage("chat", run_id, "context", "done",
                        detail="миссия " + (meta.get("ticker") or "нет") + f", новости {len(news_txt)} симв., "
                               f"рынок {len(light_txt)} симв.")
        scout_txt, reqs = await _scout_text(run_id, text, meta, mission_txt.splitlines()[0] if mission_txt else "")
        blocks["scout"] = scout_txt
        blocks = await _fit(blocks)
        s, u = prompt(text, blocks, model)
        await bus.stage("chat", run_id, "answer", "start",
                        detail=f"{'PRO' if model == 'pro' else 'FLASH'} отвечает · промпт {len(u):,} симв.".replace(",", " "))
        if model == "pro":
            async def on_think(d: str) -> None:
                await bus.text("chat", run_id, "answer", d, kind="think")

            async def on_text(d: str) -> None:
                await bus.text("chat", run_id, "answer", d)
            answer = await asyncio.wait_for(ai_v5.pro_stream(s, u, on_think=on_think, on_text=on_text, route="chat"),
                                            ANSWER_TIMEOUT)
        else:
            answer = await asyncio.wait_for(ai_v5.flash_text(s, u, route="chat"), ANSWER_TIMEOUT)
            await bus.text("chat", run_id, "answer", answer or "")
        answer = (answer or "").strip()
        if not answer:
            raise RuntimeError("ИИ вернул пустой ответ")
        bus.set_text(run_id, "answer", answer)
        await bus.stage("chat", run_id, "answer", "done", detail=f"ответ {len(answer)} симв. за {time.time() - t0:.0f} с")
    except asyncio.CancelledError:
        err = "прервано"
        raise
    except Exception as e:                               # noqa: BLE001
        err = _humanize(e)
        log.warning("чат: %s", err)
        if isinstance(e, asyncio.TimeoutError):          # 24.09.2026: накопленный по шине текст не теряем
            try:
                acc = (((bus.run(run_id) or {}).get("texts") or {}).get("answer") or "").strip()
            except Exception:                            # noqa: BLE001
                acc = ""
            if acc:
                answer = acc + "\n\n[⚠ ответ прерван по времени — выше накопленное]"
                bus.set_text(run_id, "answer", answer)
        try:
            await bus.stage("chat", run_id, "answer", "error", detail=err)
        except Exception:                                # noqa: BLE001
            pass
    finally:
        if _STATE.get("cleared_ts", 0.0) <= asked_ts:    # историю не стёрли, пока ИИ отвечал
            h = history()
            h.append({"role": "assistant", "text": answer or f"(ответа нет: {err})", "ts": time.time(), "model": model,
                      "run_id": run_id, "error": err, "scout": [r.get("code") for r in reqs if isinstance(r, dict)]})
            _save(h)
        bus.end_run(run_id, "error" if err else "done", err)


async def ask(text: str, model: str | None = None) -> dict:
    """Принять вопрос: история + фоновый прогон. Возврат сразу — ответ идёт по шине."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "note": "пустой вопрос"}
    text = text[:TEXT_MAX]
    if busy():
        return {"ok": False, "note": "ИИ ещё отвечает — дождись ответа", "run_id": _STATE.get("run_id"), "busy": True}
    if not (ai_v5.has_key() or os.getenv("PYTHIA_MOCK_AI") == "1"):
        return {"ok": False, "note": "нет ключа DeepSeek — введи его в панели (🔑 Ключи)"}
    m = str(model or getattr(config, "PYTHIA_CHAT_MODEL", "pro") or "pro").lower().strip()
    m = "flash" if m == "flash" else "pro"
    run_id = bus.start_run("chat", {"question": text[:200], "model": m})
    asked_ts = time.time()
    h = history()
    h.append({"role": "user", "text": text, "ts": time.time()})
    _save(h)
    _STATE.update(run_id=run_id, model=m, task=asyncio.get_running_loop().create_task(_run(run_id, text, m, asked_ts)))
    return {"ok": True, "run_id": run_id, "model": m}


def status() -> dict:
    return {"busy": busy(), "run_id": _STATE.get("run_id"), "model": _STATE.get("model"),
            "default_model": getattr(config, "PYTHIA_CHAT_MODEL", "pro")}


# ── API ───────────────────────────────────────────────────────────────────────
@router.post("/api/v5/chat")
async def api_chat_post(payload: dict | None = None):
    payload = payload or {}
    try:
        r = await ask(str(payload.get("text") or ""), payload.get("model"))
    except Exception as e:                               # noqa: BLE001
        log.warning("chat: %s", str(e)[:120])
        return JSONResponse({"ok": False, "note": f"чат не принял вопрос: {str(e)[:150]}"}, status_code=500)
    if not r.get("ok"):
        return JSONResponse(r, status_code=409 if r.get("busy") else 400)
    return r


@router.get("/api/v5/chat/history")
async def api_chat_history():
    return {"items": history(), **status()}


@router.delete("/api/v5/chat/history")
async def api_chat_clear():
    clear_history()
    return {"ok": True}


@router.get("/api/v5/chat/status")
async def api_chat_status():
    return status()


# ══════════════════════════════════════════════════════════════════════════════
# Self-тест: ИИ, шина и источники подменены
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace

    store_v5.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
    store_v5._schema()
    events: list[dict] = []

    async def sink(ev: dict) -> None:
        events.append(ev)
    bus.set_sink(sink)

    class FakeAI:
        now_msk_str = staticmethod(ai_v5.now_msk_str)
        fmt_ts = staticmethod(ai_v5.fmt_ts)
        MSK = ai_v5.MSK
        hold: asyncio.Event | None = None
        boom = False
        last: dict = {}

        @staticmethod
        def has_key():
            return True

        async def pro_stream(self, system, user, *, on_think=None, on_text=None, route="pro"):
            self.last = {"system": system, "user": user, "route": route}
            if self.boom:
                raise RuntimeError("DeepSeek 500")
            if on_think:
                await on_think("смотрю позицию… ")
            for d in ("Позиция ", "жива, ", "трос на месте."):
                if on_text:
                    await on_text(d)
            if self.hold is not None:
                await self.hold.wait()
            return "Позиция жива, трос на месте."

        async def flash_text(self, system, user, *, think=False, route="flash", max_tokens=None):
            self.last = {"system": system, "user": user, "route": route}
            return "быстро: всё ок"

    fake_ai = FakeAI()
    globals()["ai_v5"] = fake_ai
    compress.ai_v5 = fake_ai

    class StubMission:
        _M = {"SBER": SimpleNamespace(pilot=SimpleNamespace(figi="FIGI-S"))}
        snap: dict = {"active": "SBER", "missions": {"SBER": {
            "ticker": "SBER", "name": "Сбербанк", "asset_class": "share", "play": "long", "phase": "in_position",
            "price": 300.5, "started_ts": time.time() - 3600, "note": "пилот идёт", "error": None,
            "exec": {"do": "BUY", "entry": None, "entry_kind": "сейчас", "take": 310.0, "invalidation": 295.0,
                     "why": "откуп", "plan": "держим до 310"},
            "account_pos": None,
            "pilot": {"state": "В_ПОЗИЦИИ", "last_action": "в позиции long 8 лот", "mode": "real",
                      "adopt_account": True, "sized_by_broker": True, "soft_stop": True,
                      "position": {"side": "long", "lots": 8, "entry": 299.0, "floating": 1200.0,
                                   "invalidation": 295.0, "hard_stop": 290.6, "take": 310.0, "holds": 1},
                      "account": {"free": 12000.0, "liquid": 250000.0, "max_buy": 3, "max_sell": 11, "ts": 1},
                      "killswitch": {"locked": False}, "session_pnl": 150.0, "trades": 2, "next_review_in_s": 900,
                      "guards": [{"ts": time.time(), "decision": "ЖДАТЬ", "why": "ложный прокол"}]},
            "reviews": [{"ts": time.time(), "choice": "ЖДЁМ", "why": "план жив"}],
            "handoffs": [{"ts": time.time(), "kind": "stop", "reason": "мягкий стоп: FLASH решил ждать"}],
            "trades": {"count": 2, "pnl": 150.0}, "scout": {"text": "MX 2 850 (−0.4%)", "requests": []},
            "memory": {"text": "22.09 вошли long 299, трос держал; 23.09 тейк не дошёл", "ts": time.time(), "n": 2},
            "explain": [{"ts": time.time(), "kind": "entry", "title": "Вход исполнен", "text": "Пилот вошёл в лонг 8 лотов."}]}}}

        @classmethod
        def snapshot(cls):
            return cls.snap

    from . import council as _real_council

    class StubCouncil:
        @staticmethod
        def latest():
            return {"kind": "daily", "ts": time.time(), "data": {"summary": {"regime": "risk-on"}}}

        summary_text = staticmethod(_real_council.summary_text)     # v5.4.2: шапку с возрастом пишет совет

    class StubNews:
        @staticmethod
        def fresh_since(ts):
            return [{"id": "n1", "title": "ЦБ сохранил ставку"}]

        @staticmethod
        def news_for_ticker(t, name, days=3, limit=None):
            return [{"id": "n2", "title": "Сбер: дивиденды"}]

        @staticmethod
        def render(items, limit=None, rich=False):
            return "\n".join(f"[{i['id']}] {i['title']}" for i in items)

    class StubMarket:
        calls: list = []

        @staticmethod
        async def light(t, figi, ac):
            StubMarket.calls.append((t, figi, ac))
            return {"text": f"{t}: цена 300.5, стакан bid 300.4 / ask 300.6", "price": 300.5}

    class StubScout:
        briefs: list = []

        @staticmethod
        async def run(scope, run_id, kind, brief, ticker=None):
            StubScout.briefs.append((scope, kind, brief, ticker))
            await bus.stage(scope, run_id, "scout", "done", n=1, detail="MX")
            return "Разведка FLASH: MX 2 850.5 (-0.40%)", [{"kind": "quote", "code": "MX"}]

    class StubCorrelate:
        """Связанные бумаги без сети (настоящий correlate полез бы к бирже)."""
        calls: list = []

        @classmethod
        async def partners(cls, t, ac, days=60, *, force=False):
            cls.calls.append((t, ac))
            return [{"code": "VTBR", "name": "ВТБ", "asset_class": "share", "kind": "сектор: банки", "rho": 0.81,
                     "lead": 0, "n": 59, "move_1d": 1.2, "move_5d": -0.5, "price": 95.5, "source": "tinkoff"}]

        @staticmethod
        def text(items, t=""):
            return f"Связанные бумаги для {t}: VTBR ρ=+0.81"

    class StubApiV5:
        @staticmethod
        def _astro_line():
            return "☽ Луна в Тельце"

    store_v5.watch_add({"ts": time.time(), "severity": 80, "note": "санкции на банки", "affected": ["SBER"]})
    _STUBS.update(mission=StubMission, council=StubCouncil, newsflow=StubNews, market_ctx=StubMarket,
                  scout=StubScout, api_v5=StubApiV5, correlate=StubCorrelate)

    async def main():
        # ── PRO: полный контекст, стрим по шине, история ──
        r = await ask("что по SBER, держим?", "pro")
        assert r["ok"] and r["run_id"].startswith("chat-") and r["model"] == "pro", r
        assert busy() and status()["busy"] and status()["run_id"] == r["run_id"]
        r2 = await ask("ещё вопрос")
        assert not r2["ok"] and r2.get("busy") and "ещё отвечает" in r2["note"], r2
        await _STATE["task"]
        await asyncio.sleep(0.05)
        assert not busy()
        u, s = fake_ai.last["user"], fake_ai.last["system"]
        assert fake_ai.last["route"] == "chat" and system_lines(s) <= SYSTEM_MAX_LINES, system_lines(s)
        assert VOICE in s and "ДОКТРИНА" in s, "чат говорит голосом 4.5.4 (v5.4.1)"
        for piece in ("═══ МИССИЯ И ПИЛОТ ═══", "Миссия SBER (Сбербанк, share), режим игры long, фаза: в позиции",
                      "ПОЗИЦИЯ ПИЛОТА: long 8 лот @299.0", "триггер (мягкий стоп) 295.0", "аварийный трос биржи 290.6",
                      "; у троса держал 1 раз", "биржа даёт купить лотов 3", "Приказ совета: BUY вход сейчас",
                      "План ведения: держим до 310", "Ответ у троса ", "ЖДАТЬ — ложный прокол",
                      "Перепроверка", "Передача", "(stop)", "Сделок по миссии: 2", "Разведка миссии (последняя)",
                      "ПАМЯТЬ МИССИИ (прошлые этапы", "трос держал", "Толмач", "Вход исполнен): Пилот вошёл в лонг",
                      "═══ ИТОГ СОВЕТА ═══", "Совет daily от ", "МСК (0 мин назад) — общий по рынку", "Режим: risk-on",
                      "Входов совет не назвал",
                      "═══ ЖИВОЙ РЫНОК ПО ИНСТРУМЕНТУ МИССИИ ═══", "SBER: цена 300.5",
                      "═══ ЗАМЕТКИ ДОЗОРА ═══", "серьёзность 80 · санкции на банки · SBER",
                      "═══ НОВОСТИ (время МСК) ═══", "[n1] ЦБ сохранил ставку", "[n2] Сбер: дивиденды",
                      "═══ НЕБО ═══", "☽ Луна в Тельце",
                      "═══ ДАННЫЕ РАЗВЕДКИ ПОД ВОПРОС", "MX 2 850.5 (-0.40%)",
                      "═══ СВЯЗАННЫЕ БУМАГИ ИНСТРУМЕНТА МИССИИ (корреляции) ═══", "Связанные бумаги для SBER: VTBR ρ=+0.81",
                      "ВОПРОС ВЛАДЕЛЬЦА: что по SBER, держим?"):
            assert piece in u, (piece, u[:2500])
        assert "FLASH у троса" not in u and "Совет (daily" not in u, "метки 5.4.2: «у троса», шапка совета — одна"
        assert StubCorrelate.calls == [("SBER", "share")] and "связанные бумаги" in s, "чат видит связанные бумаги миссии"
        assert "память миссии" in s and "толмача" in s, "system упоминает память и толмача (v5.3)"
        assert "═══ ДИАЛОГ" not in u, "первый вопрос — диалога ещё нет"
        assert StubMarket.calls == [("SBER", "FIGI-S", "share")]
        assert StubScout.briefs and StubScout.briefs[-1][0] == "chat" and StubScout.briefs[-1][1] == "chat"
        assert "что по SBER" in StubScout.briefs[-1][2] and StubScout.briefs[-1][3] == "SBER"
        st = [e for e in events if e.get("run_id") == r["run_id"] and e.get("type") == "v5"]
        seq = [(e["stage"], e["status"]) for e in st]
        for want in (("context", "start"), ("context", "done"), ("scout", "done"), ("answer", "start"),
                     ("answer", "done"), ("end", "done")):
            assert want in seq, (want, seq)
        assert all(e["scope"] == "chat" for e in st)
        tx = [e for e in events if e.get("run_id") == r["run_id"] and e.get("type") in ("text", "think")]
        assert [e["type"] for e in tx] == ["think", "text", "text", "text"] and all(e["stage"] == "answer" for e in tx)
        assert "".join(e["delta"] for e in tx if e["type"] == "text") == "Позиция жива, трос на месте."
        assert bus.run(r["run_id"])["texts"]["answer"] == "Позиция жива, трос на месте."
        assert bus.run(r["run_id"])["status"] == "done" and bus.run(r["run_id"])["meta"]["model"] == "pro"
        h = history()
        assert len(h) == 2 and h[0]["role"] == "user" and h[1]["role"] == "assistant"
        assert h[1]["text"] == "Позиция жива, трос на месте." and h[1]["model"] == "pro" and h[1]["scout"] == ["MX"]
        assert h[1]["run_id"] == r["run_id"] and h[1]["error"] is None

        # ── FLASH: без стрима мыслей, ответ одной дельтой; диалог в промпте ──
        events.clear()
        r = await ask("а коротко?", "flash")
        assert r["ok"] and r["model"] == "flash"
        await _STATE["task"]
        await asyncio.sleep(0.05)
        u = fake_ai.last["user"]
        assert "═══ ДИАЛОГ (последние реплики) ═══" in u and "Владелец (" in u and "что по SBER, держим?" in u
        assert "ПИФИЯ (" in u and "Позиция жива, трос на месте." in u and "а коротко?" not in u.split("ВОПРОС ВЛАДЕЛЬЦА")[0]
        assert "FLASH (быстрая модель)" in fake_ai.last["system"]
        tx = [e for e in events if e.get("type") in ("text", "think")]
        assert len(tx) == 1 and tx[0]["type"] == "text" and tx[0]["delta"] == "быстро: всё ок"
        assert history()[-1]["text"] == "быстро: всё ок" and history()[-1]["model"] == "flash"

        # ── ошибка ИИ: стадия error, конец error, в истории честная заметка, busy снят ──
        events.clear()
        fake_ai.boom = True
        r = await ask("сломайся", "pro")
        await _STATE["task"]
        await asyncio.sleep(0.05)
        fake_ai.boom = False
        seq = [(e["stage"], e["status"]) for e in events if e.get("type") == "v5"]
        assert ("answer", "error") in seq and ("end", "error") in seq, seq
        assert bus.run(r["run_id"])["status"] == "error" and "DeepSeek 500" in bus.run(r["run_id"])["error"]
        last = history()[-1]
        assert last["role"] == "assistant" and last["error"] == "DeepSeek 500" and "ответа нет" in last["text"]
        assert not busy()

        # ── модель по умолчанию из config, пустой вопрос, потолок истории, очистка ──
        config.PYTHIA_CHAT_MODEL = "flash"
        r = await ask("x" * 5000)
        assert r["ok"] and r["model"] == "flash"
        await _STATE["task"]
        assert len(history()[-2]["text"]) == TEXT_MAX
        config.PYTHIA_CHAT_MODEL = "pro"
        assert not (await ask("   "))["ok"]
        _save([{"role": "user", "text": str(i), "ts": time.time()} for i in range(100)])
        assert len(history()) == HISTORY_KEEP
        assert _dialog_text(history()).count("\n") == DIALOG_LAST - 1
        clear_history()
        assert history() == []

        # ── v5.4.2: приказ WAIT — чего ждал совет и уровни; фаза idle — «вне рынка / жду план»;
        #    молчание ИИ — «ответа не было — решения не было», а не ЖДАТЬ ──
        snap_w = {"active": "SBER", "missions": {"SBER": {
            "ticker": "SBER", "name": "Сбербанк", "asset_class": "share", "play": "auto", "phase": "idle",
            "price": 300.5, "started_ts": time.time(), "exec_ts": time.time(),
            "exec": {"do": "WAIT", "entry": None, "entry_kind": "сейчас", "take": None, "invalidation": None,
                     "wait_for": "закрепление выше 305 на объёме", "levels": [300.0, 305.5], "why": "коридор"},
            "pilot": {"state": "ОЖИДАНИЕ", "last_action": "вне рынка", "position": None,
                      "guards": [{"ts": time.time(), "decision": "НЕТ_ОТВЕТА", "why": "таймаут", "silent": True}]},
            "reviews": [{"ts": time.time(), "choice": "НЕТ_ОТВЕТА", "why": "таймаут 1200 с"},
                        {"ts": time.time(), "choice": "НЕ_РАЗОБРАН", "why": "«может быть»"},
                        {"ts": time.time(), "choice": "ЖДЁМ", "why": "коридор 300–305"}]}}}
        StubMission.snap = snap_w
        txt, _ = _mission_text()
        for piece in ("фаза: вне рынка / жду план", "): WAIT — совет ждал: закрепление выше 305 на объёме; уровни: 300, 305.5",
                      "почему: коридор", "Ответ у троса", "ответа не было — решения не было (таймаут)",
                      "Перепроверка", "ответа не было — решения не было (таймаут 1200 с)",
                      "ответ не разобран — решения не было", "ЖДЁМ — коридор 300–305"):
            assert piece in txt, (piece, txt)
        assert "вход сейчас" not in txt and "стоп None" not in txt and "НЕТ_ОТВЕТА —" not in txt, txt
        snap_w["missions"]["SBER"]["exec"] = {"do": "CLOSE", "why": "слом", "invalidation": 1.0}
        txt, _ = _mission_text()
        assert "): CLOSE — закрыть позицию — слом" in txt and "вход сейчас" not in txt, txt

        # ── без миссии: честная строка; без ключа — отказ ──
        StubMission.snap = {"active": None, "missions": {}}
        txt, meta = _mission_text()
        assert txt.startswith("Миссии нет") and meta == {}
        StubMission.snap = {"active": None, "missions": {"GAZP": {"phase": "stopped", "note": "пилот стоп"}}}
        assert "Прошлые: GAZP: пилот остановлен — пилот стоп" in _mission_text()[0]
        assert await _light_text({}) == ""
        fake_ai.has_key = staticmethod(lambda: False)
        os.environ.pop("PYTHIA_MOCK_AI", None)
        assert "нет ключа" in (await ask("привет"))["note"]

        # ── API-обёртки ──
        fake_ai.has_key = staticmethod(lambda: True)
        fake_ai.hold = asyncio.Event()
        r = await api_chat_post({"text": "долгий вопрос", "model": "pro"})
        assert isinstance(r, dict) and r["ok"]
        resp = await api_chat_post({"text": "второй"})
        assert isinstance(resp, JSONResponse) and resp.status_code == 409
        resp = await api_chat_post({"text": ""})
        assert isinstance(resp, JSONResponse) and resp.status_code == 400
        assert (await api_chat_status())["busy"] and (await api_chat_history())["busy"]
        fake_ai.hold.set()
        await _STATE["task"]
        fake_ai.hold = None
        hh = await api_chat_history()
        assert not hh["busy"] and hh["items"][-1]["role"] == "assistant" and hh["default_model"] == "pro"
        assert (await api_chat_clear())["ok"] and (await api_chat_history())["items"] == []
        # очистка истории, пока ИИ отвечает: ответ прогона в пустую историю не ложится (одинокого assistant нет)
        fake_ai.hold = asyncio.Event()
        r = await ask("вопрос до очистки", "pro")
        assert r["ok"] and busy()
        clear_history()
        assert history() == []
        fake_ai.hold.set()
        await _STATE["task"]
        fake_ai.hold = None
        assert history() == [] and not busy() and bus.run(r["run_id"])["status"] == "done"

    asyncio.run(main())
    print("api_chat self-test OK: контекст (миссия/пилот/счёт/трос, совет, живой рынок, дозор, новости, небо, "
          "разведка под вопрос, диалог), PRO-стрим мыслей и текста по шине, FLASH одной дельтой, ошибка ИИ честно, "
          "занятость (409), история kv с потолком, очистка, без ключа отказ, API-обёртки")
