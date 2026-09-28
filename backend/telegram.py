# -*- coding: utf-8 -*-
"""ПИФИЯ v5 — Telegram-бот владельца (фаза 4 · W2).

Владелец вписывает токен бота (BotFather) и chat_id в панели «Ключи» — бот присылает уведомления
(приказ совета, вход/добор/закрытие сделки с числами, решения ИИ у троса и тейка (PRO или FLASH по настройке), объяснения толмача,
перепроверки PRO, итог совета, сверка сделки с брокером «чистыми», проблемы) и итоги дня (18:55 и 23:55 МСК,
если биржа в этот день торговала; утром — «рынок открылся, позиция такая-то»). Через бота видно всё:
/status /pos /trades /day /explain /health /council; управление (/stop, /panic) — только с подтверждением «да»
в течение 60 с. Чужой чат получает свой chat_id и просьбу впиcать его в панели — и ничего больше.

Сеть — httpx (тот же клиент, что у tinkoff.py); ошибки (401/429/сеть) идут в лог и в `last_error()` (панель
проблем, kind "telegram"), ничего не роняют. Очередь: 1 сообщение/с, дедуп повторов за 60 с, дробление длинных;
события одного узла копятся 5 с и уходят одним сообщением. Токен в лог, ответ и вывод не попадает.
Self-тест: `python3 -m backend.telegram` — HTTP подменён фейком, миссия/журнал — фейки, data/ не трогается.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import re
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import httpx

from . import config

log = logging.getLogger("pythia.telegram")
logging.getLogger("httpx").setLevel(logging.WARNING)      # URL запроса содержит токен — не в INFO-лог

API = "https://api.telegram.org"
MSK = timezone(timedelta(hours=3))
RATE_SEC = 1.0              # не чаще одного сообщения в секунду
DEDUP_SEC = 60.0            # одинаковый текст в один чат — раз в минуту
WINDOW_SEC = 5.0            # события одного узла копятся столько секунд и уходят одним сообщением
HEALTH_GAP_SEC = 600.0      # одна проблема (по виду) — не чаще раза в 10 мин
CONFIRM_SEC = 60.0          # «да» на /stop и /panic действует столько секунд
POLL_TIMEOUT = 25           # long polling getUpdates, с
PART_LIMIT = 3800           # Telegram: ≤ 4096 символов на сообщение; режем по строкам с запасом
MAX_TRIES = 3               # попыток отправить одно сообщение при сетевом сбое / 429
DAILY_SLOTS = ((18, 55), (23, 55))   # итоги дня по МСК
DAILY_LATE_MIN = 30         # слот ещё действует столько минут после времени (рестарт сервера в 23:57 — итоги уйдут)
KV_DAILY = "tg_daily_done"

_Q: deque = deque()          # очередь на отправку: {"chat","text","silent","tries"}
_wake: asyncio.Event | None = None
_recent: dict[str, float] = {}         # дедуп: sha1(chat+text) → ts
_batch: dict[str, dict] = {}           # узел → {"lines": [...], "ts", "silent", "task"}
_notified: dict[str, float] = {}       # вид проблемы → ts последнего уведомления
_confirm: dict | None = None           # ожидание «да»: {"action","ticker","ts"}
_offset = 0                            # getUpdates offset
_paused_until = 0.0                    # после 401 — пауза сети
_last_err: dict | None = None
_last_ok_ts = 0.0
_sent = 0
_polling = False
_market_prev: bool | None = None       # последний известный статус биржи (None — ещё не смотрели)
_daily_done: dict[str, dict] | None = None   # {"YYYY-MM-DD": {"18:55": count}}
_http: httpx.AsyncClient | None = None
_lock = asyncio.Lock()


# ── конфиг ────────────────────────────────────────────────────────────────────
def token() -> str:
    return str(getattr(config, "TG_BOT_TOKEN", "") or config.get("TG_BOT_TOKEN", "")).strip()


def chat_id() -> str:
    return str(getattr(config, "TG_CHAT_ID", "") or config.get("TG_CHAT_ID", "")).strip()


def enabled() -> bool:
    """Бот включён: PYTHIA_TG (1) и есть токен."""
    return bool(getattr(config, "PYTHIA_TG", True)) and bool(token())


def bound() -> bool:
    return enabled() and bool(chat_id())


def daily_enabled() -> bool:
    return bool(getattr(config, "PYTHIA_TG_DAILY", True))


def mask(s: str) -> str:
    s = str(s or "")
    if not s:
        return ""
    return s[:4] + "…" + s[-3:] if len(s) > 10 else ("•••" if len(s) > 3 else s)


def chat_mask(s: str | None = None) -> str:
    s = str(chat_id() if s is None else s)
    if not s:
        return ""
    return s[:2] + "…" + s[-2:] if len(s) > 5 else "•••"


def status() -> dict:
    """Для панели: без токена и без chat_id полностью."""
    return {"enabled": enabled(), "token": bool(token()), "token_mask": mask(token()), "bound": bound(),
            "chat_mask": chat_mask(), "polling": _polling, "queued": len(_Q), "sent": _sent,
            "last_ok": _last_ok_ts or None, "last_error": last_error(), "daily": daily_enabled()}


# ── ошибки ────────────────────────────────────────────────────────────────────
class TgError(RuntimeError):
    def __init__(self, code: int, text: str = "", retry_after: float | None = None):
        super().__init__(f"{code} {text}".strip())
        self.code, self.text, self.retry_after = code, text, retry_after


def _scrub(s: str) -> str:
    """Токен не должен попасть ни в лог, ни в панель."""
    t = token()
    return (s or "").replace(t, "•••") if t else (s or "")


def note_error(method: str, code: int | str | None, text: str) -> dict:
    global _last_err
    _last_err = {"ts": time.time(), "method": method, "code": code, "text": _scrub(str(text))[:200]}
    log.warning("Telegram %s: %s %s", method, code or "", _last_err["text"])
    return dict(_last_err)


def last_error() -> dict | None:
    """Последняя ошибка Telegram {"ts","method","code","text","stale"} | None; stale — после неё уже был успех."""
    if not _last_err:
        return None
    d = dict(_last_err)
    d["stale"] = bool(_last_ok_ts and _last_ok_ts > float(d.get("ts") or 0))
    return d


# ── сеть ──────────────────────────────────────────────────────────────────────
async def _client() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=httpx.Timeout(20.0, read=POLL_TIMEOUT + 15.0),
                                  limits=httpx.Limits(max_keepalive_connections=2, max_connections=4))
    return _http


async def aclose() -> None:
    global _http
    if _http is not None and not _http.is_closed:
        try:
            await _http.aclose()
        except Exception:                                  # noqa: BLE001
            pass
    _http = None


async def _post(method: str, body: dict, timeout: float | None = None) -> Any:
    """POST к Bot API → result; не ok → TgError (401 токен, 429 retry_after, 409 второй getUpdates …).
    Self-тест подменяет эту функцию фейком."""
    tok = token()
    if not tok:
        raise TgError(0, "нет токена")
    cl = await _client()
    r = await cl.post(f"{API}/bot{tok}/{method}", json=body, timeout=timeout or 20.0)
    try:
        j = r.json() if r.content else {}
    except Exception:                                      # noqa: BLE001
        j = {}
    if r.status_code != 200 or not isinstance(j, dict) or not j.get("ok"):
        desc = (j or {}).get("description") if isinstance(j, dict) else None
        ra = ((j or {}).get("parameters") or {}).get("retry_after") if isinstance(j, dict) else None
        raise TgError(int(r.status_code), str(desc or r.text[:200] or "пустой ответ"), retry_after=ra)
    return j.get("result")


async def check_token() -> dict:
    """getMe — жив ли токен (кнопка «Проверить» в панели): {ok, name, note}."""
    if not token():
        return {"ok": False, "name": None, "note": "токена бота нет"}
    try:
        me = await _post("getMe", {}, timeout=10.0)
        _ok()
        return {"ok": True, "name": (me or {}).get("username"), "note": "бот отвечает"}
    except TgError as e:
        note = ("токен бота не принят (401) — вставь из BotFather заново" if e.code in (401, 404)
                else f"{e.code} {e.text[:120]}")
        note_error("getMe", e.code, note)
        return {"ok": False, "name": None, "note": note}
    except Exception as e:                                 # noqa: BLE001
        note_error("getMe", None, f"сеть: {str(e)[:120]}")
        return {"ok": False, "name": None, "note": f"сеть: {str(e)[:120]}"}


def _ok() -> None:
    global _last_ok_ts
    _last_ok_ts = time.time()


# ── форматирование ────────────────────────────────────────────────────────────
def esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def _num(x: Any, digits: int = 2, sign: bool = False) -> str:
    """Число человеку: 1 234,5 (без хвоста нулей); None → «—»."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    s = f"{v:+,.{digits}f}" if sign else f"{v:,.{digits}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s.replace(",", " ").replace(".", ",")


def _rub(x: Any) -> str:
    return "—" if x is None else _num(x, 2, sign=True) + " ₽"


def _px(x: Any) -> str:
    return "—" if x is None else _num(x, 4)


def _hhmm(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts), MSK).strftime("%d.%m %H:%M")
    except (TypeError, ValueError, OSError):
        return "—"


_SIDE = {"long": "лонг", "short": "шорт"}
_PHASE = {"council": "совет думает", "entering": "вхожу", "in_position": "в позиции", "armed": "засада",
          "idle": "жду план", "closed": "рынок закрыт", "stopped": "остановлена", "panic": "ПАНИКА",
          "error": "ошибка", "done": "завершена", "waiting": "жду"}


def _side(s: Any) -> str:
    return _SIDE.get(str(s or ""), str(s or "—"))


def trade_line(t: dict, *, confirmed: bool | None = None) -> str:
    """«сделка такая-то»: сторона, лоты, вход → выход (реальные цены, если есть), нетто, комиссия, причина, оценка."""
    est = bool(t.get("est")) if confirmed is None else not confirmed
    e = t.get("entry_real") if t.get("entry_real") is not None else t.get("entry")
    x = t.get("exit_real") if t.get("exit_real") is not None else t.get("exit_px")
    net = t.get("net")
    if net is None:
        net = t.get("pnl_net") if t.get("pnl_net") is not None else t.get("pnl")
    fee = t.get("fee") if t.get("fee") is not None else t.get("fee_est")
    s = (f"{esc(t.get('ticker') or '?')} {_side(t.get('side'))} {t.get('lots') or '?'} лот, "
         f"вход {_px(e)} → выход {_px(x)}, <b>{_rub(net)}</b>")
    if fee:
        s += f" (комиссия {_num(fee)})"
    s += " — оценка, брокер не подтвердил" if est else " — по операциям брокера"
    if t.get("why"):
        s += f"; {esc(str(t['why'])[:160])}"
    return s


def _pos_line(pst: dict | None) -> str:
    pos = (pst or {}).get("position") if isinstance(pst, dict) else None
    if not pos:
        return "вне рынка"
    s = f"{_side(pos.get('side'))} {pos.get('lots') or '?'} лот @{_px(pos.get('entry'))}"
    parts = [f"триггер {_px(pos.get('invalidation'))}" if pos.get("invalidation") is not None else "",
             f"трос {_px(pos.get('hard_stop'))}" if pos.get("hard_stop") is not None else "",
             f"тейк {_px(pos.get('take'))}" if pos.get("take") is not None else ""]
    parts = [p for p in parts if p]
    return s + (" · " + " · ".join(parts) if parts else "")


def _market_line(mk: Any) -> str:
    if not isinstance(mk, dict):
        return "статус неизвестен"
    if mk.get("text"):
        return str(mk["text"])
    if mk.get("open"):
        return "открыт" + (f": {mk.get('reason')}" if mk.get("reason") else "")
    nxt = mk.get("next_open_msk")
    return "закрыт" + (f" до {nxt}" if nxt else "") + (f" ({mk.get('reason')})" if mk.get("reason") else "")


def _active(snap: dict | None) -> tuple[str | None, dict]:
    snap = snap or {}
    act = snap.get("active")
    ms = ((snap.get("missions") or {}).get(act) or {}) if act else {}
    return (act or None), (ms if isinstance(ms, dict) else {})


def fmt_day(d: dict) -> str:
    """Итоги дня из ledger.day_summary — одним сообщением."""
    if not d or not d.get("ok"):
        return "Итоги дня: " + esc((d or {}).get("note") or "нет данных")
    md = {"broker": "по операциям брокера", "mock": "мок", "est": "оценка, без операций брокера"}.get(d.get("mode"), "")
    out = [f"📊 <b>Итоги дня {esc(d.get('day'))}</b>" + (f" ({md})" if md else "")]
    n = int(d.get("count") or 0)
    if n:
        out.append(f"Сделок: {n} (в плюс {d.get('wins') or 0}, в минус {d.get('losses') or 0})")
        fees = f"комиссии {_num(d.get('fee'))}"
        if d.get("margin_fee"):
            fees += f", маржа {_num(d.get('margin_fee'))}"
        out.append(f"Нетто: <b>{_rub(d.get('net'))}</b> · брутто {_rub(d.get('gross'))} · {fees}")
        b, w = d.get("best"), d.get("worst")
        if b and (n > 1 or w is None):
            out.append("Лучшая: " + trade_line(b))
        if w and n > 1:
            out.append("Худшая: " + trade_line(w))
        if n == 1 and b:
            out[-1] = "Сделка: " + trade_line(b)
    else:
        out.append("Сделок за день нет")
    if d.get("session_pnl") is not None:
        out.append(f"Сессия пилота {esc(d.get('pilot_ticker') or '')}: {_rub(d.get('session_pnl'))}")
    acc = d.get("account") or {}
    if acc.get("total") is not None or acc.get("cash") is not None:
        out.append(f"Счёт: {_num(acc.get('total'))} ₽, свободно {_num(acc.get('cash'))} ₽ ({esc(acc.get('note') or '')})")
    elif acc.get("note"):
        out.append("Счёт: " + esc(acc["note"]))
    if d.get("est"):
        out.append("<i>часть сделок — оценка (комиссия по PYTHIA_FEE_PCT, брокер не подтвердил)</i>")
    return "\n".join(out)


# ── очередь отправки ──────────────────────────────────────────────────────────
def split(text: str, limit: int = PART_LIMIT) -> list[str]:
    """Длинный текст — на части по строкам, каждая ≤ limit (строка длиннее — режется жёстко)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            parts.append(cur)
            cur = line
        else:
            cur = line if not cur else cur + "\n" + line
    if cur:
        parts.append(cur)
    return parts


def _dedup(chat: str, text: str) -> bool:
    now = time.time()
    for k, ts in list(_recent.items()):
        if now - ts > DEDUP_SEC:
            _recent.pop(k, None)
    key = hashlib.sha1((chat + "\n" + text).encode("utf-8")).hexdigest()
    if key in _recent:
        return True
    _recent[key] = now
    return False


def send(text: str, silent: bool = False, chat: str | None = None) -> int:
    """Поставить сообщение в очередь (HTML). Возвращает число частей в очереди (0 — выключено/не привязан/повтор).
    Сеть — в flush()/фоне; отсюда ничего не ждёт."""
    if not enabled():
        return 0
    chat = str(chat or chat_id() or "")
    if not chat:
        log.info("Telegram: чат не привязан — сообщение пропущено: %s", (text or "")[:80].replace("\n", " "))
        return 0
    text = (text or "").strip()
    if not text or _dedup(chat, text):
        return 0
    parts = split(text)
    for p in parts:
        _Q.append({"chat": chat, "text": p, "silent": bool(silent), "tries": 0})
    if _wake is not None:
        _wake.set()
    return len(parts)


_TAG_RE = re.compile(r"</?(b|i|code|pre|u|s|a)( [^>]*)?>")


async def _send_one(item: dict) -> str:
    """Одна отправка: "sent" — ушло, "drop" — выброшено (401/403/предел попыток), "retry" — позже (сеть/429)."""
    global _sent, _paused_until
    body = {"chat_id": item["chat"], "text": item["text"], "parse_mode": "HTML",
            "disable_web_page_preview": True, "disable_notification": bool(item.get("silent"))}
    if item.get("plain"):
        body.pop("parse_mode", None)
        body["text"] = html.unescape(_TAG_RE.sub("", item["text"]))
    try:
        await _post("sendMessage", body)
        _ok()
        _sent += 1
        return "sent"
    except TgError as e:
        item["tries"] += 1
        if e.code == 429:
            note_error("sendMessage", 429, f"лимит Telegram, подожду {e.retry_after or 5} с")
            item["wait"] = min(30.0, float(e.retry_after or 5))
            return "drop" if item["tries"] >= MAX_TRIES else "retry"
        if e.code == 400 and "parse" in e.text.lower() and not item.get("plain"):
            item["plain"] = True                            # HTML не разобрался — тот же текст без разметки
            item["wait"] = 0.0
            return "retry"
        if e.code in (401, 403, 404):
            _paused_until = time.time() + 60.0
            note_error("sendMessage", e.code, ("токен бота не принят — впиши в «Ключи» заново" if e.code in (401, 404)
                                               else f"бот заблокирован в чате или чат не найден: {e.text}"))
            return "drop"                                   # такое сообщение не доставить никогда
        note_error("sendMessage", e.code, e.text)
        return "drop" if item["tries"] >= MAX_TRIES else "retry"
    except Exception as e:                                 # noqa: BLE001
        item["tries"] += 1
        note_error("sendMessage", None, f"сеть: {str(e)[:120]}")
        item["wait"] = 5.0
        return "drop" if item["tries"] >= MAX_TRIES else "retry"


async def flush(limit: int | None = None) -> int:
    """Выслать очередь (не чаще 1/с). Возвращает число отправленных. Сеть упала — остаток ждёт следующего раза."""
    n = 0
    async with _lock:
        while _Q and (limit is None or n < limit):
            if time.time() < _paused_until:
                break
            item = _Q[0]
            r = await _send_one(item)
            if r == "sent":
                _Q.popleft()
                n += 1
                await asyncio.sleep(RATE_SEC)
            elif r == "drop":
                _Q.popleft()
            else:
                w = item.pop("wait", None)
                if w is not None:                           # 429 / сеть / без разметки — повтор в этом же проходе
                    if w:
                        await asyncio.sleep(w)
                    continue
                break
    return n


# ── объединение событий одного узла (окно 5 с) ────────────────────────────────
def notify(key: str, line: str, silent: bool = False) -> None:
    """Строка события узла key: копится WINDOW_SEC и уходит одним сообщением с остальными строками узла."""
    if not bound():
        return
    b = _batch.get(key)
    if b is None:
        b = _batch[key] = {"lines": [], "ts": time.time(), "silent": silent, "task": None}
    if line not in b["lines"]:
        b["lines"].append(line)
    b["silent"] = b["silent"] and silent
    if b["task"] is None:
        try:
            b["task"] = asyncio.get_running_loop().create_task(_batch_later(key))
        except RuntimeError:                                # вне цикла (self-тест) — flush_batches() вручную
            b["task"] = None


async def _batch_later(key: str) -> None:
    try:
        await asyncio.sleep(WINDOW_SEC)
    except asyncio.CancelledError:
        raise
    _flush_batch(key)


def _flush_batch(key: str) -> int:
    b = _batch.pop(key, None)
    if not b or not b["lines"]:
        return 0
    return send("\n".join(b["lines"]), silent=b["silent"])


async def flush_batches() -> int:
    """Все накопленные узлы — в очередь сейчас (тесты, выключение)."""
    n = 0
    for key in list(_batch):
        b = _batch.get(key)
        if b and b.get("task"):
            b["task"].cancel()
        n += _flush_batch(key)
    return n


def problem(kind: str, text: str, silent: bool = False) -> bool:
    """Проблема (health / ошибка стадии): один вид — не чаще раза в HEALTH_GAP_SEC."""
    now = time.time()
    if now - _notified.get(kind, 0.0) < HEALTH_GAP_SEC:
        return False
    if not bound():
        return False
    _notified[kind] = now
    send("⚠️ " + text, silent=silent)
    return True


# ── события шины ──────────────────────────────────────────────────────────────
_PILOT_WORDS = ("ВОШЁЛ", "ДОБРАЛ", "ПРИНЯЛ", "РЕСТАРТ", "закрыт", "отбит", "CLOSE", "заявк", "трос", "стоп")
_SCOPE_RU = {"mission": "миссия", "daily": "совет", "update": "совет (новости)", "human": "взгляд человека",
             "watch": "дозор", "ledger": "журнал"}


async def on_event(ev: dict) -> None:
    """Слушатель шины (server.py: telegram.wrap_sink(HUB.broadcast)). Быстрый: сеть — не здесь."""
    if not isinstance(ev, dict) or ev.get("type") != "v5" or not bound():
        return
    scope, stage, st = str(ev.get("scope") or ""), str(ev.get("stage") or ""), str(ev.get("status") or "")
    t = str(ev.get("ticker") or "")
    detail = str(ev.get("detail") or "").strip()
    data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
    node = f"{scope}:{t}:{stage}"
    try:
        if st == "error" and scope in ("mission", "daily", "update", "human", "watch"):
            if stage == "explain":                          # толмач промолчал — не событие для владельца
                return
            problem(f"{scope}:{stage}", f"<b>{esc(_SCOPE_RU.get(scope, scope))} {esc(t)}</b> · {esc(stage)}: "
                                        f"{esc(detail or ev.get('error') or 'ошибка')}")
            return
        if scope == "mission":
            if stage == "exec" and st == "done":
                notify(node, f"📜 <b>Приказ {esc(t)}</b>: {esc(detail)}")
            elif stage == "pilot" and st == "progress":
                if data.get("closed_ts") is not None and "pnl" in data:      # сделка закрыта (по тику, до сверки)
                    notify(node, "✅ <b>Сделка закрыта</b>: " + trade_line(data, confirmed=False))
                elif data.get("fill"):
                    notify(node, f"🟢 <b>{esc(t)}</b>: {esc(detail)}")
                elif detail and any(w in detail for w in _PILOT_WORDS):
                    notify(node, f"🤖 {esc(t)}: {esc(detail)}")
            elif stage == "guard" and st == "done":
                notify(node, f"🛡 <b>{esc(t)}</b> {esc(detail)}")
            elif stage == "take" and st == "done":
                notify(node, f"🎯 <b>{esc(t)}</b> {esc(detail)}")
            elif stage == "review" and st == "done":
                notify(node, f"🔁 <b>Перепроверка PRO {esc(t)}</b>: {esc(detail)}")
            elif stage == "explain" and st == "done" and data.get("text"):
                notify(node, f"🗣 <b>{esc(data.get('title') or 'Толмач')}</b>\n{esc(data['text'])}")
            elif stage == "summary" and st == "done":
                notify(node, f"🏛 <b>Совет по {esc(t)}</b>: {esc(detail)}")
            elif stage == "pilot" and st == "done" and detail:
                notify(node, f"⏹ <b>{esc(t)}</b>: пилот остановился — {esc(detail)}")
        elif scope in ("daily", "update", "human") and stage == "summary" and st == "done":
            notify(node, f"📰 <b>{esc(_SCOPE_RU.get(scope, scope)).capitalize()}</b>: {esc(detail)}")
        elif scope == "ledger" and stage == "reconcile" and st == "done" and data.get("trade"):
            notify(node, "💰 <b>Сверено с брокером</b>: " + trade_line(data["trade"], confirmed=True))
    except Exception as e:                                 # noqa: BLE001
        log.info("Telegram: событие %s/%s не разобрано: %s", scope, stage, str(e)[:100])


def wrap_sink(inner: Callable[[dict], Awaitable[None]] | None) -> Callable[[dict], Awaitable[None]]:
    """Обёртка для bus.set_sink: сначала фронт (HUB.broadcast), потом бот; сбой одного не трогает другого."""
    async def _sink(ev: dict) -> None:
        if inner is not None:
            try:
                await inner(ev)
            except Exception as e:                         # noqa: BLE001
                log.info("шина → фронт: %s", str(e)[:80])
        try:
            await on_event(ev)
        except Exception as e:                             # noqa: BLE001
            log.info("шина → Telegram: %s", str(e)[:80])
    return _sink


# ── источники данных (подменяются в self-тесте) ───────────────────────────────
def _snapshot() -> dict:
    from . import mission
    return mission.snapshot()


async def _day(day: str | None) -> dict:
    from . import ledger
    return await ledger.day_summary(day)


def _trades(n: int = 10) -> list[dict]:
    from . import store_v5
    return store_v5.trades(None, n)


def _trades_summary() -> dict:
    from . import ledger
    return ledger.trades_summary()


def _health() -> list[dict]:
    from . import api_v5
    mk = None
    try:
        from . import market_clock
        mk = market_clock.last()
    except Exception:                                      # noqa: BLE001
        mk = None
    return list((api_v5.health_block(mission_snap=_snapshot(), market=mk) or {}).get("problems") or [])


def _council() -> dict:
    from . import council
    return council.snapshot()


async def _stop(ticker: str) -> dict:
    from . import mission
    return await mission.stop(ticker)


async def _panic(ticker: str | None) -> dict:
    from . import mission
    return await mission.panic(ticker)


async def _market(ticker: str | None) -> dict | None:
    from . import market_clock
    if not market_clock.enabled():
        return None
    return await market_clock.snapshot(ticker or None)


def _trading_day(d: date) -> bool:
    if os.getenv("PYTHIA_MOCK_TINKOFF") == "1":
        return True
    try:
        from . import market_clock
        return bool(market_clock._is_trading_day(d))
    except Exception:                                      # noqa: BLE001
        return d.weekday() < 5


def _now_msk() -> datetime:
    return datetime.now(MSK)


# ── команды ───────────────────────────────────────────────────────────────────
HELP = ("<b>Команды</b>\n"
        "/status — миссия, фаза, позиция, P/L, триггер · трос · тейк, рынок, следующая перепроверка\n"
        "/pos — позиция и счёт по инструменту\n"
        "/trades — последние 10 сделок, чистыми\n"
        "/day [ГГГГ-ММ-ДД] — итоги дня (сегодня по МСК)\n"
        "/explain — последние 3 объяснения толмача\n"
        "/health — что сломано и что делать\n"
        "/council — итог последнего совета\n"
        "/stop — остановить пилот (позиция под тросом биржи) — с подтверждением «да»\n"
        "/panic — закрыть всё и встать — с подтверждением «да»\n"
        "/help — это сообщение")


def _reply_status() -> str:
    snap = _snapshot()
    act, ms = _active(snap)
    if not act:
        try:
            c = _council() or {}
            d = c.get("daily") or {}
            s = (d.get("summary") or {})
            cl = f"\nПоследний совет ({_hhmm(d.get('ts'))}): {esc(s.get('regime') or '—')}" if d else ""
        except Exception:                                  # noqa: BLE001
            cl = ""
        return "Миссии нет — пилот не ведёт инструмент." + cl
    pst = ms.get("pilot") if isinstance(ms.get("pilot"), dict) else {}
    out = [f"<b>{esc(act)}</b> {esc(ms.get('name') or '')} · {_PHASE.get(str(ms.get('phase')), esc(ms.get('phase')))}"
           + (" · живая" if ms.get("live") else " · пилот не идёт")]
    if ms.get("price") is not None:
        out.append(f"Цена: {_px(ms.get('price'))}")
    out.append("Позиция: " + _pos_line(pst))
    if not pst.get("position") and isinstance(pst.get("plan"), dict):
        p = pst["plan"]
        out.append(f"План: {_side(p.get('side'))} вход {_px(p.get('entry')) if p.get('entry') is not None else 'сейчас'}"
                   f" · триггер {_px(p.get('invalidation'))} · тейк {_px(p.get('take'))}")
    if pst:
        out.append(f"P/L сессии: <b>{_rub(pst.get('session_pnl'))}</b> за {pst.get('trades') or 0} сделок")
    tr = ms.get("trades") if isinstance(ms.get("trades"), dict) else None
    if tr and tr.get("count"):
        out.append(f"Журнал {esc(act)}: {tr.get('count')} сделок, чистыми {_rub(tr.get('net', tr.get('pnl')))}"
                   + (" (есть оценочные)" if tr.get("est") else ""))
    out.append("Рынок: " + esc(_market_line(ms.get("market"))))
    if pst.get("next_review_in_s") is not None and ms.get("live"):
        s = int(pst["next_review_in_s"])
        out.append(f"Следующая перепроверка через {s // 60} мин {s % 60:02d} с"
                   + (f" ({esc(pst.get('review_reason'))})" if pst.get("review_reason") else ""))
    if pst.get("last_action"):
        out.append("Последнее: " + esc(str(pst["last_action"])[:300]))
    if ms.get("error"):
        out.append("⚠️ " + esc(ms["error"]))
    return "\n".join(out)


def _reply_pos() -> str:
    act, ms = _active(_snapshot())
    if not act:
        return "Миссии нет — позиции пилота нет."
    pst = ms.get("pilot") if isinstance(ms.get("pilot"), dict) else {}
    out = [f"<b>{esc(act)}</b>: " + _pos_line(pst)]
    pos = pst.get("position") if isinstance(pst.get("position"), dict) else None
    if pos:
        if pos.get("opened_ts"):
            out.append(f"Открыта {_hhmm(pos['opened_ts'])}")
        if ms.get("price") is not None and pos.get("entry") is not None:
            try:
                sgn = 1.0 if pos.get("side") == "long" else -1.0
                move = (float(ms["price"]) - float(pos["entry"])) * sgn
                out.append(f"Ход от входа: {_num(move, 4, sign=True)} пунктов (цена {_px(ms['price'])})")
            except (TypeError, ValueError):
                pass
        if pos.get("stop_id"):
            out.append("Трос стоит на бирже")
        elif pos.get("stop_err"):
            out.append("⚠️ трос на бирже не встал: " + esc(str(pos["stop_err"])[:120]))
    if pst.get("account"):
        a = pst["account"]
        if isinstance(a, dict):
            out.append("Счёт: " + esc("; ".join(f"{k} {v}" for k, v in list(a.items())[:6])))
    if ms.get("account_pos"):
        out.append("На счёте: " + esc(ms["account_pos"]))
    if pst.get("foreign_lots"):
        out.append(f"Чужие лоты: {pst['foreign_lots']}")
    return "\n".join(out)


def _reply_trades() -> str:
    items = _trades(10) or []
    if not items:
        return "Сделок в журнале нет."
    out = ["<b>Последние сделки</b> (чистыми)"]
    for t in items:
        out.append(f"{_hhmm(t.get('closed_ts'))} · " + trade_line(t))
    try:
        s = _trades_summary() or {}
        if s.get("count"):
            out.append(f"Всего {s['count']} сделок: нетто <b>{_rub(s.get('net'))}</b>, комиссии {_num(s.get('fee'))}"
                       + (", часть — оценка" if s.get("est") else ""))
    except Exception:                                      # noqa: BLE001
        pass
    return "\n".join(out)


async def _reply_day(arg: str) -> str:
    day = (arg or "").strip() or None
    if day and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return "Дата — ГГГГ-ММ-ДД, например /day 2026-09-23"
    try:
        return fmt_day(await _day(day))
    except Exception as e:                                 # noqa: BLE001
        return "Итоги дня не собрались: " + esc(str(e)[:120])


def _reply_explain() -> str:
    act, ms = _active(_snapshot())
    if not act:
        return "Миссии нет — толмачу нечего объяснять."
    items = [x for x in (ms.get("explain") or []) if isinstance(x, dict)][-3:]
    if not items:
        return f"{esc(act)}: объяснений толмача пока нет."
    return "\n\n".join(f"🗣 <b>{_hhmm(x.get('ts'))} · {esc(x.get('title') or '')}</b>\n{esc(x.get('text') or '')}"
                       for x in items)


def _reply_health() -> str:
    try:
        probs = _health() or []
    except Exception as e:                                 # noqa: BLE001
        return "Панель проблем не собралась: " + esc(str(e)[:120])
    if not probs:
        return "✅ Проблем нет."
    icon = {"err": "🔴", "warn": "🟠", "info": "ℹ️"}
    return "\n".join(f"{icon.get(p.get('level'), '•')} {esc(p.get('text'))}" for p in probs[:12])


def _reply_council() -> str:
    try:
        c = _council() or {}
    except Exception as e:                                 # noqa: BLE001
        return "Совет не прочитан: " + esc(str(e)[:120])
    kind = c.get("latest_kind") or "daily"
    d = c.get(kind) or c.get("daily")
    if not d:
        return "Совета ещё не было."
    s = d.get("summary") or {}
    fr = d.get("frame") or {}
    out = [f"🏛 <b>Совет</b> ({esc(_SCOPE_RU.get(kind, kind))}, {_hhmm(d.get('ts'))})"]
    if fr.get("headline"):
        out.append(esc(fr["headline"]))
    if s.get("regime"):
        out.append("Режим: " + esc(s["regime"]))
    picks = s.get("picks") or []
    if picks:
        out.append("Входы: " + "; ".join(f"{esc(p.get('ticker'))} {_side(p.get('side'))}"
                                         + (f" ({p.get('conviction')})" if p.get("conviction") is not None else "")
                                         for p in picks[:8]))
    else:
        out.append("Входов нет")
    if d.get("reason"):
        out.append("Повод: " + esc(str(d["reason"])[:160]))
    return "\n".join(out)


def _ask_confirm(action: str) -> str:
    global _confirm
    act, _ = _active(_snapshot())
    if action == "stop" and not act:
        return "Миссии нет — останавливать нечего."
    _confirm = {"action": action, "ticker": act, "ts": time.time()}
    log.info("Telegram: запрошено подтверждение %s %s", action, act or "(все)")
    if action == "panic":
        return (f"🛑 <b>ПАНИКА</b>: закрыть всё по {esc(act) if act else 'всем миссиям'} и встать? "
                f"Ответь «да» в течение {int(CONFIRM_SEC)} с.")
    return (f"⏹ Остановить пилот {esc(act)}? Позиция останется под тросом биржи. "
            f"Ответь «да» в течение {int(CONFIRM_SEC)} с.")


async def _do_confirm(yes: bool) -> str:
    global _confirm
    c, _confirm = _confirm, None
    if not c:
        return "Подтверждать нечего."
    if time.time() - float(c.get("ts") or 0) > CONFIRM_SEC:
        log.info("Telegram: подтверждение %s просрочено", c["action"])
        return f"Срок вышел — {c['action'] == 'panic' and 'паника' or 'стоп'} не выполнен, повтори команду."
    if not yes:
        log.info("Telegram: %s отменён владельцем", c["action"])
        return "Отменено."
    try:
        r = await (_panic(c.get("ticker")) if c["action"] == "panic" else _stop(str(c.get("ticker") or "")))
    except Exception as e:                                 # noqa: BLE001
        log.warning("Telegram: %s не выполнен: %s", c["action"], str(e)[:120])
        return f"Не выполнено: {esc(str(e)[:150])}"
    log.warning("Telegram: владелец подтвердил %s %s → %s", c["action"], c.get("ticker") or "(все)",
                str((r or {}).get("note") or r)[:160])
    ok = bool((r or {}).get("ok"))
    return ("✅ " if ok else "⚠️ ") + esc((r or {}).get("note") or ("выполнено" if ok else "не вышло"))


_YES = ("да", "yes", "y", "д", "ок", "ok", "подтверждаю")
_NO = ("нет", "no", "n", "отмена", "cancel", "стоп-отмена")


async def handle_command(text: str, chat: str, user: str = "") -> str | None:
    """Ответ на текст из чата (None — молчать). Чужой чат: только свой chat_id и просьба впиcать его."""
    text = (text or "").strip()
    if not text:
        return None
    chat = str(chat)
    cmd, _, arg = text.partition(" ")
    cmd = cmd.lower().split("@", 1)[0]
    own = chat_id()
    if not own or chat != own:
        log.info("Telegram: чужой чат %s (%s): %s", chat_mask(chat), user or "?", text[:60])
        who = "не привязан" if not own else "чужой чат"
        return (f"Этот чат {who}. Твой chat_id: <code>{esc(chat)}</code> — впиши его в панели «Ключи» "
                f"(поле «Telegram chat_id»), и бот начнёт присылать уведомления и отвечать на команды.")
    log.info("Telegram: команда %s%s", cmd, (" " + arg[:40]) if arg else "")
    if cmd in _YES or cmd in _NO:
        return await _do_confirm(cmd in _YES)
    if cmd == "/start":
        return (f"Привязан ✅ chat_id <code>{esc(chat)}</code>. Бот присылает сделки, решения ИИ, объяснения толмача "
                f"и итоги дня.\n\n{HELP}")
    if cmd == "/help":
        return HELP
    try:
        if cmd == "/status":
            return _reply_status()
        if cmd == "/pos":
            return _reply_pos()
        if cmd == "/trades":
            return _reply_trades()
        if cmd == "/day":
            return await _reply_day(arg)
        if cmd == "/explain":
            return _reply_explain()
        if cmd == "/health":
            return _reply_health()
        if cmd == "/council":
            return _reply_council()
        if cmd in ("/stop", "/panic"):
            return _ask_confirm(cmd[1:])
    except Exception as e:                                 # noqa: BLE001
        log.warning("Telegram: команда %s споткнулась: %s", cmd, str(e)[:120])
        return f"Не вышло: {esc(str(e)[:150])}"
    if cmd.startswith("/"):
        return "Не знаю такой команды. /help"
    return None


async def handle_update(upd: dict) -> None:
    msg = (upd or {}).get("message") or (upd or {}).get("edited_message")
    if not isinstance(msg, dict):
        return
    chat = (msg.get("chat") or {}).get("id")
    text = msg.get("text")
    if chat is None or not isinstance(text, str):
        return
    user = str((msg.get("from") or {}).get("username") or (msg.get("from") or {}).get("first_name") or "")
    reply = await handle_command(text, str(chat), user)
    if reply:
        send(reply, chat=str(chat))


# ── фон: long polling, отправка, планировщик ──────────────────────────────────
async def poll_once() -> int:
    """Один getUpdates (long polling до POLL_TIMEOUT с). Возвращает число обработанных обновлений."""
    global _offset, _paused_until
    try:
        res = await _post("getUpdates", {"offset": _offset, "timeout": POLL_TIMEOUT, "allowed_updates": ["message"]},
                          timeout=POLL_TIMEOUT + 10.0)
    except TgError as e:
        if e.code == 401 or e.code == 404:
            note_error("getUpdates", e.code, "токен бота не принят — впиши в «Ключи» заново")
            _paused_until = time.time() + 60.0
        elif e.code == 409:
            note_error("getUpdates", 409, "этот токен уже опрашивает другой процесс (второй сервер?) — жду")
            _paused_until = time.time() + 15.0
        elif e.code == 429:
            note_error("getUpdates", 429, "лимит Telegram")
            _paused_until = time.time() + min(30.0, float(e.retry_after or 5))
        else:
            note_error("getUpdates", e.code, e.text)
            _paused_until = time.time() + 5.0
        return 0
    except Exception as e:                                 # noqa: BLE001
        note_error("getUpdates", None, f"сеть: {str(e)[:120]}")
        _paused_until = time.time() + 5.0
        return 0
    _ok()
    n = 0
    for upd in res or []:
        try:
            _offset = max(_offset, int(upd.get("update_id", 0)) + 1)
            await handle_update(upd)
            n += 1
        except Exception as e:                             # noqa: BLE001
            log.info("Telegram: обновление не разобрано: %s", str(e)[:100])
    return n


async def _poll_loop() -> None:
    global _polling
    while True:
        try:
            if not enabled():
                _polling = False
                await asyncio.sleep(5)
                continue
            if time.time() < _paused_until:
                await asyncio.sleep(min(5.0, max(0.5, _paused_until - time.time())))
                continue
            _polling = True
            await poll_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:                             # noqa: BLE001
            log.warning("Telegram: опрос споткнулся: %s", str(e)[:120])
            await asyncio.sleep(5)


async def _pump_loop() -> None:
    global _wake
    if _wake is None:
        _wake = asyncio.Event()
    while True:
        try:
            if _Q and enabled() and time.time() >= _paused_until:
                await flush()
            _wake.clear()
            try:
                await asyncio.wait_for(_wake.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            raise
        except Exception as e:                             # noqa: BLE001
            log.warning("Telegram: отправка споткнулась: %s", str(e)[:120])
            await asyncio.sleep(5)


def _daily_load() -> dict:
    global _daily_done
    if _daily_done is None:
        try:
            from . import store_v5
            v = store_v5.kv_get(KV_DAILY)
            _daily_done = dict(v) if isinstance(v, dict) else {}
        except Exception:                                  # noqa: BLE001
            _daily_done = {}
    return _daily_done


def _daily_save() -> None:
    try:
        from . import store_v5
        d = _daily_load()
        for k in sorted(d)[:-7]:                           # неделя истории хватит
            d.pop(k, None)
        store_v5.kv_set(KV_DAILY, d)
    except Exception as e:                                 # noqa: BLE001
        log.info("Telegram: отметка итогов дня не сохранилась: %s", str(e)[:80])


async def daily_tick(now: datetime | None = None) -> bool:
    """Итоги дня: в слоты 18:55 и 23:55 МСК (до +30 мин), если биржа в этот день торговала; второй слот молчит,
    когда после первого ничего не прибавилось и пилот не жив. True — отправлено."""
    if not (bound() and daily_enabled()):
        return False
    now = now or _now_msk()
    if not _trading_day(now.date()):
        return False
    day = now.strftime("%Y-%m-%d")
    done = _daily_load().setdefault(day, {})
    cur = now.hour * 60 + now.minute
    for i, (h, m) in enumerate(DAILY_SLOTS):
        slot = h * 60 + m
        key = f"{h:02d}:{m:02d}"
        if key in done or not (slot <= cur < slot + DAILY_LATE_MIN):
            continue
        d = await _day(day)
        cnt = int((d or {}).get("count") or 0)
        prev = None
        if i > 0:
            pk = f"{DAILY_SLOTS[i - 1][0]:02d}:{DAILY_SLOTS[i - 1][1]:02d}"
            prev = done.get(pk)
        done[key] = cnt
        _daily_save()
        if prev is not None and prev == cnt and (d or {}).get("session_pnl") is None:
            log.info("Telegram: итоги %s %s — без изменений с прошлого слота, молчу", day, key)
            return False
        send(fmt_day(d))
        log.info("Telegram: итоги дня %s (%s) отправлены", day, key)
        return True
    return False


async def market_tick(open_now: bool | None, snap: dict | None = None) -> bool:
    """Переход биржи закрыто → открыто / открыто → закрыто: одно сообщение с позицией. Первый замер — молча."""
    global _market_prev
    if open_now is None:
        return False
    prev, _market_prev = _market_prev, bool(open_now)
    if prev is None or prev == bool(open_now) or not bound():
        return False
    act, ms = _active(snap if snap is not None else _snapshot())
    pst = ms.get("pilot") if isinstance(ms.get("pilot"), dict) else {}
    pos = ("позиция " + esc(act) + ": " + _pos_line(pst)) if act else "миссии нет"
    if open_now:
        send(f"🔔 <b>Рынок открылся</b> — {pos}")
    else:
        send(f"🌙 <b>Рынок закрылся</b> — {pos}" + (" (под тросом биржи)" if pst.get("position") else ""), silent=True)
    return True


def health_tick(problems: list[dict] | None) -> int:
    """Проблемы уровня err из панели — по виду не чаще раза в 10 мин."""
    n = 0
    for p in problems or []:
        if not isinstance(p, dict) or p.get("level") != "err":
            continue
        if problem(f"health:{p.get('kind')}", esc(p.get("text") or "")):
            n += 1
    return n


async def _sched_loop() -> None:
    last_health = 0.0
    last_market = 0.0
    while True:
        try:
            await asyncio.sleep(30)
            if not bound():
                continue
            now = time.time()
            if now - last_health >= 60:
                last_health = now
                try:
                    health_tick(_health())
                except Exception as e:                     # noqa: BLE001
                    log.info("Telegram: health не прочитан: %s", str(e)[:80])
            if now - last_market >= 60:
                last_market = now
                try:
                    snap = _snapshot()
                    act, ms = _active(snap)
                    mk = ms.get("market") if act and isinstance(ms.get("market"), dict) else None
                    if mk is None:
                        mk = await _market(act)
                    await market_tick(None if not isinstance(mk, dict) else mk.get("open"), snap)
                except Exception as e:                     # noqa: BLE001
                    log.info("Telegram: рынок не прочитан: %s", str(e)[:80])
            await daily_tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:                             # noqa: BLE001
            log.warning("Telegram: планировщик споткнулся: %s", str(e)[:120])
            await asyncio.sleep(30)


async def loop() -> None:
    """Фон из server.py: опрос команд, отправка очереди, планировщик. Без токена спит и ждёт, когда впишут."""
    global _wake
    if _wake is None:
        _wake = asyncio.Event()
    log.info("Telegram: %s", ("бот включён, чат " + (chat_mask() or "не привязан — напиши боту /start"))
             if enabled() else "токена нет — бот спит; впиши токен BotFather в «Ключи»")
    await asyncio.gather(_poll_loop(), _pump_loop(), _sched_loop())


# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    from . import store_v5

    store_v5.DB_PATH = Path(tempfile.mkdtemp(prefix="pythia_tg_")) / "t.db"
    store_v5._schema()
    RATE_SEC = 0.0                                          # noqa: F811 — тест не ждёт секунды между сообщениями
    WINDOW_SEC = 0.05                                       # noqa: F811
    config.TG_BOT_TOKEN = "123456:fake-token-for-self-test"  # только в памяти процесса, config_user.json не трогаем
    config.TG_CHAT_ID = "4242"
    config.PYTHIA_TG = True
    config.PYTHIA_TG_DAILY = True
    os.environ.pop("PYTHIA_MOCK_TINKOFF", None)

    SENT: list[dict] = []
    UPDATES: list[dict] = []
    FAIL: dict = {"code": None, "n": 0}

    async def fake_post(method, body, timeout=None):
        if method == "sendMessage":
            if FAIL["n"] > 0:
                FAIL["n"] -= 1
                raise TgError(FAIL["code"], "fake", retry_after=0)
            assert "fake-token" not in body.get("text", ""), "токен утёк в текст"
            SENT.append(dict(body))
            return {"message_id": len(SENT)}
        if method == "getUpdates":
            out, UPDATES[:] = list(UPDATES), []
            return out
        if method == "getMe":
            return {"username": "pythia_test_bot"}
        raise AssertionError(method)

    _post = fake_post                                       # noqa: F811

    # фейковые источники
    POS = {"side": "long", "lots": 2, "entry": 300.5, "invalidation": 297.0, "hard_stop": 292.5, "take": 310.0,
           "opened_ts": time.time() - 900, "stop_id": "S1"}
    SNAP = {"active": "SBER", "missions": {"SBER": {
        "ticker": "SBER", "name": "Сбербанк", "phase": "in_position", "live": True, "price": 303.2,
        "pilot": {"position": POS, "session_pnl": 54.0, "trades": 1, "next_review_in_s": 725, "review_reason": "план",
                  "last_action": "ВОШЁЛ: long 2 лот @300.5", "account": {"lots": 2}},
        "trades": {"count": 1, "net": 48.5, "est": True},
        "market": {"open": True, "reason": "торги идут", "text": "рынок открыт: торги идут (основная сессия)"},
        "explain": [{"ts": time.time() - 100, "title": "Вход исполнен", "text": "Вошли лонгом по плану совета."},
                    {"ts": time.time() - 50, "title": "Перепроверка", "text": "PRO держит позицию."}],
        "error": None}}}
    TRADES = [{"id": 2, "ticker": "SBER", "side": "short", "lots": 1, "entry": 310.0, "exit_px": 312.0, "pnl": -20.0,
               "entry_real": 310.0, "exit_real": 312.0, "fee": 3.11, "net": -23.11, "pnl_gross": -20.0, "est": False,
               "source": "broker", "why": "триггер", "closed_ts": time.time() - 3000},
              {"id": 1, "ticker": "SBER", "side": "long", "lots": 2, "entry": 300.0, "exit_px": 304.0, "pnl": 80.0,
               "entry_real": None, "exit_real": None, "fee": 6.04, "fee_est": 6.04, "net": 73.96, "pnl_gross": 80.0,
               "est": True, "source": "est", "why": "тейк", "closed_ts": time.time() - 7000}]
    DAY = {"ok": True, "day": "2026-09-23", "count": 2, "net": 50.85, "gross": 60.0, "fee": 9.15, "wins": 1, "losses": 1,
           "best": TRADES[1], "worst": TRADES[0], "trades": TRADES, "est": True, "margin_fee": 1.2,
           "session_pnl": 54.0, "pilot_ticker": "SBER", "account": {"total": 10500.0, "cash": 4000.0, "note": "по портфелю брокера"},
           "mode": "broker", "last_sync": time.time(), "note": "часть сделок оценочные"}
    CALLS: list = []

    async def fake_day(day):
        CALLS.append(("day", day))
        return DAY if day in (None, "2026-09-23") else {"ok": True, "day": day, "count": 0, "trades": [], "account": {"note": "нет"}}

    async def fake_panic(t):
        CALLS.append(("panic", t))
        return {"ok": True, "note": "ПАНИКА: закрываю всё и встаю"}

    async def fake_stop(t):
        CALLS.append(("stop", t))
        return {"ok": True, "note": "пилот остановлен"}

    _snapshot = lambda: SNAP                                # noqa: E731, F811
    _day = fake_day                                         # noqa: F811
    _trades = lambda n=10: TRADES[:n]                       # noqa: E731, F811
    _trades_summary = lambda: {"count": 2, "net": 50.85, "fee": 9.15, "est": True}   # noqa: E731, F811
    _health = lambda: [{"kind": "tinkoff", "level": "err", "text": "Tinkoff: токен не принят"},   # noqa: E731, F811
                       {"kind": "market", "level": "info", "text": "Рынок закрыт"}]
    _council = lambda: {"latest_kind": "daily", "daily": {"ts": time.time() - 600, "reason": "утро",   # noqa: E731, F811
                                                          "summary": {"regime": "боковик", "picks": [{"ticker": "SBER", "side": "long", "conviction": 7}]},
                                                          "frame": {"headline": "Рынок ждёт ЦБ"}}}
    _panic = fake_panic                                     # noqa: F811
    _stop = fake_stop                                       # noqa: F811
    _trading_day = lambda d: True                           # noqa: E731, F811

    def upd(text, chat="4242", uid=None):
        upd.n += 1
        return {"update_id": uid or upd.n, "message": {"text": text, "chat": {"id": int(chat)}, "from": {"username": "oleg"}}}
    upd.n = 100

    async def main():
        global _confirm, _market_prev
        assert enabled() and bound() and status()["token_mask"].endswith("est") and "fake" not in status()["token_mask"]
        assert status()["chat_mask"] == "•••" and chat_mask("123456789") == "12…89"
        assert mask("") == "" and "fake-token" not in mask(token())
        # 1) чужой чат → свой chat_id и просьба впиcать; /start владельца — привязан + справка
        r = await handle_command("/start", "777", "кто-то")
        assert "не привязан" not in r and "чужой" in r and "<code>777</code>" in r, r
        r = await handle_command("/start", "4242")
        assert "Привязан" in r and "/status" in r
        saved = config.TG_CHAT_ID
        config.TG_CHAT_ID = ""
        r = await handle_command("/status", "4242")
        assert "не привязан" in r and "4242" in r
        config.TG_CHAT_ID = saved
        # 2) команды
        r = await handle_command("/status@pythia_bot", "4242")
        assert "SBER" in r and "в позиции" in r and "лонг 2 лот @300,5" in r and "триггер 297" in r and "трос 292,5" in r \
            and "тейк 310" in r and "+54 ₽" in r and "12 мин 05 с" in r and "рынок открыт" in r, r
        r = await handle_command("/pos", "4242")
        assert "лонг 2 лот" in r and "Трос стоит на бирже" in r and "+2,7 пунктов" in r, r
        r = await handle_command("/trades", "4242")
        assert "Последние сделки" in r and "-23,11 ₽" in r and "по операциям брокера" in r and "+73,96 ₽" in r \
            and "оценка, брокер не подтвердил" in r and "Всего 2 сделок" in r, r
        r = await handle_command("/day", "4242")
        assert "Итоги дня 2026-09-23" in r and "Сделок: 2" in r and "+50,85 ₽" in r and "маржа 1,2" in r \
            and "Лучшая" in r and "Худшая" in r and "Сессия пилота SBER: +54 ₽" in r and "10 500 ₽" in r and "оценка" in r, r
        assert CALLS[-1] == ("day", None)
        r = await handle_command("/day 2026-09-22", "4242")
        assert "Сделок за день нет" in r and CALLS[-1] == ("day", "2026-09-22")
        r = await handle_command("/day вчера", "4242")
        assert "ГГГГ-ММ-ДД" in r
        r = await handle_command("/explain", "4242")
        assert "Вход исполнен" in r and "PRO держит" in r
        r = await handle_command("/health", "4242")
        assert "🔴" in r and "Tinkoff" in r and "ℹ️" in r
        r = await handle_command("/council", "4242")
        assert "боковик" in r and "SBER лонг (7)" in r and "Рынок ждёт ЦБ" in r
        assert await handle_command("/help", "4242") == HELP
        assert "Не знаю" in await handle_command("/foo", "4242")
        assert await handle_command("просто текст", "4242") is None
        assert "нечего" in await handle_command("да", "4242")
        # 3) подтверждения: /panic → «да» → выполнено; /stop → «нет» → отмена; просрочка → не выполнено
        r = await handle_command("/panic", "4242")
        assert "ПАНИКА" in r and _confirm and _confirm["action"] == "panic"
        r = await handle_command("Да", "4242")
        assert r.startswith("✅") and CALLS[-1] == ("panic", "SBER") and _confirm is None, r
        r = await handle_command("/stop", "4242")
        assert "Остановить пилот SBER" in r
        assert await handle_command("нет", "4242") == "Отменено." and CALLS[-1] == ("panic", "SBER")
        await handle_command("/stop", "4242")
        _confirm["ts"] -= CONFIRM_SEC + 1
        r = await handle_command("да", "4242")
        assert "Срок вышел" in r and CALLS[-1] == ("panic", "SBER")
        await handle_command("/stop", "4242")
        assert (await handle_command("да", "4242")).startswith("✅") and CALLS[-1] == ("stop", "SBER")
        # 4) очередь: дедуп, дробление, скорость, чужой чат получает ответ напрямую
        SENT.clear()
        assert send("привет") == 1 and send("привет") == 0 and send("привет", chat="777") == 1
        big = "\n".join(f"строка {i} " + "x" * 90 for i in range(120))
        parts = split(big)
        assert len(parts) >= 3 and all(len(p) <= PART_LIMIT for p in parts) and "".join(parts).replace("\n", "") == big.replace("\n", "")
        assert split("y" * 9000) == ["y" * PART_LIMIT, "y" * PART_LIMIT, "y" * (9000 - 2 * PART_LIMIT)]
        assert send(big) == len(parts)
        n = await flush()
        assert n == 2 + len(parts) and not _Q and SENT[0]["chat_id"] == "4242" and SENT[1]["chat_id"] == "777"
        assert SENT[0]["parse_mode"] == "HTML" and SENT[0]["disable_notification"] is False
        # 5) сбои: 429 → повтор и доставка; 401 → выброшено, пауза, last_error в панель; сеть → повтор до предела
        SENT.clear()
        FAIL.update(code=429, n=1)
        send("после лимита")
        assert await flush() == 1 and SENT[-1]["text"] == "после лимита" and last_error()["code"] == 429 and last_error()["stale"]
        FAIL.update(code=401, n=1)
        send("токен мёртв")
        t0 = time.time()
        assert await flush() == 0 and not _Q and last_error()["code"] == 401 and "не принят" in last_error()["text"] \
            and not last_error()["stale"] and _paused_until > t0
        globals()["_paused_until"] = 0.0
        FAIL.update(code=400, n=1)                          # 400 «can't parse» → тот же текст без разметки
        orig = fake_post

        async def post_400(method, body, timeout=None):
            if method == "sendMessage" and FAIL["n"] > 0:
                FAIL["n"] -= 1
                raise TgError(400, "Bad Request: can't parse entities")
            return await orig(method, body, timeout)
        globals()["_post"] = post_400
        send("<b>жирно</b> &amp; так")
        assert await flush() == 1 and SENT[-1]["text"] == "жирно & так" and "parse_mode" not in SENT[-1]
        globals()["_post"] = orig
        # 6) события шины: приказ, сделка (по тику), исполнение, трос, толмач, перепроверка, совет, сверка, ошибка
        SENT.clear()
        _recent.clear()
        evs = [
            {"type": "v5", "scope": "mission", "stage": "exec", "status": "done", "ticker": "SBER",
             "detail": "BUY вход 300 тейк 310 стоп 297 · почему: пробой"},
            {"type": "v5", "scope": "mission", "stage": "exec", "status": "start", "ticker": "SBER", "detail": "шифровальщик"},
            {"type": "v5", "scope": "mission", "stage": "pilot", "status": "progress", "ticker": "SBER",
             "detail": "ВОШЁЛ: long 2 лот @300.5", "data": {"fill": {"lots": 2}}},
            {"type": "v5", "scope": "mission", "stage": "pilot", "status": "progress", "ticker": "SBER",
             "detail": "передаю задачу Совету: новость"},
            {"type": "v5", "scope": "mission", "stage": "pilot", "status": "progress", "ticker": "SBER",
             "detail": "сделка закрыта (тейк): P/L +80", "data": {"ticker": "SBER", "side": "long", "lots": 2, "entry": 300.5,
                                                                  "exit_px": 304.5, "pnl": 80.0, "closed_ts": time.time(),
                                                                  "why": "тейк", "fee_est": 6.05, "source": "est"}},
            {"type": "v5", "scope": "mission", "stage": "guard", "status": "done", "ticker": "SBER",
             "detail": "FLASH у троса: ЖДАТЬ — откат на объёме", "data": {}},
            {"type": "v5", "scope": "mission", "stage": "explain", "status": "done", "ticker": "SBER",
             "detail": "Вход исполнен", "data": {"title": "Вход исполнен", "text": "Вошли по плану <совета>."}},
            {"type": "v5", "scope": "mission", "stage": "explain", "status": "error", "ticker": "SBER", "detail": "толмач не ответил"},
            {"type": "v5", "scope": "mission", "stage": "review", "status": "done", "ticker": "SBER", "detail": "ЖДАТЬ: тренд цел"},
            {"type": "v5", "scope": "daily", "stage": "summary", "status": "done", "detail": "боковик; входов 1: SBER long"},
            {"type": "v5", "scope": "ledger", "stage": "reconcile", "status": "done", "ticker": "SBER",
             "data": {"trade": {"ticker": "SBER", "side": "long", "lots": 2, "entry": 300.5, "exit_px": 304.5, "entry_real": 300.6,
                                "exit_real": 304.4, "pnl": 80.0, "pnl_gross": 76.0, "fee": 6.05, "net": 69.95, "est": False,
                                "source": "broker", "why": "тейк"}}},
            {"type": "v5", "scope": "mission", "stage": "council", "status": "error", "ticker": "SBER", "detail": "PRO молчит"},
            {"type": "v5", "scope": "mission", "stage": "council", "status": "error", "ticker": "SBER", "detail": "PRO молчит снова"},
            {"type": "text", "scope": "mission", "stage": "analysis", "delta": "abc"},
        ]
        for e in evs:
            await on_event(e)
        assert len(_batch) == 7, sorted(_batch)             # exec, pilot, guard, explain, review, daily, ledger — по узлам
        await asyncio.sleep(WINDOW_SEC * 3)                 # окно вышло — узлы ушли в очередь сами
        assert not _batch, sorted(_batch)
        await flush()
        texts = [s["text"] for s in SENT]
        assert texts[0].startswith("⚠️") and "PRO молчит" in texts[0] and "снова" not in texts[0]   # ошибка — сразу, повтор — молчит
        joined = "\n".join(texts)
        assert "📜 <b>Приказ SBER</b>: BUY вход 300" in joined and "шифровальщик" not in joined
        assert "🟢 <b>SBER</b>: ВОШЁЛ: long 2 лот @300.5" in joined and "передаю задачу" not in joined
        assert "✅ <b>Сделка закрыта</b>: SBER лонг 2 лот, вход 300,5 → выход 304,5, <b>+80 ₽</b> (комиссия 6,05) — оценка" in joined
        assert "🛡 <b>SBER</b> FLASH у троса" in joined and "🗣 <b>Вход исполнен</b>\nВошли по плану &lt;совета&gt;." in joined
        assert "толмач не ответил" not in joined and "🔁 <b>Перепроверка PRO SBER</b>: ЖДАТЬ" in joined
        assert "📰 <b>Совет</b>: боковик" in joined
        assert "💰 <b>Сверено с брокером</b>: SBER лонг 2 лот, вход 300,6 → выход 304,4, <b>+69,95 ₽</b> (комиссия 6,05) — по операциям брокера" in joined
        pilot_msgs = [x for x in texts if "ВОШЁЛ" in x]
        assert len(pilot_msgs) == 1 and "Сделка закрыта" in pilot_msgs[0]   # два события узла pilot — одно сообщение
        # 7) health: err — раз в 10 мин на вид, info — никогда
        SENT.clear()
        assert health_tick(_health()) == 1 and health_tick(_health()) == 0
        _notified["health:tinkoff"] -= HEALTH_GAP_SEC + 1
        assert health_tick(_health()) == 1
        await flush()
        assert len(SENT) == 1 and "Tinkoff" in SENT[0]["text"]   # второй — тот же текст в ту же минуту, дедуп
        # 8) итоги дня по слотам МСК: 18:55 → отправлено; повтор — нет; 23:55 без новых сделок и без пилота — молчит;
        #    с живым пилотом — уходит; выходной — нет
        SENT.clear(); _recent.clear(); CALLS.clear()
        d = datetime(2026, 9, 23, 18, 57, tzinfo=MSK)
        assert await daily_tick(d) is True and CALLS[-1] == ("day", "2026-09-23")
        assert await daily_tick(d) is False and await daily_tick(d.replace(hour=12)) is False
        assert await daily_tick(d.replace(hour=19, minute=40)) is False      # слот прошёл
        assert store_v5.kv_get(KV_DAILY)["2026-09-23"]["18:55"] == 2
        DAY["session_pnl"] = None
        assert await daily_tick(d.replace(hour=23, minute=56)) is False      # без изменений — молчим
        DAY["session_pnl"] = 54.0
        globals()["_daily_done"]["2026-09-23"].pop("23:55")
        _recent.clear()                                     # в тесте текст итогов один и тот же — дедуп мешал бы
        assert await daily_tick(d.replace(hour=23, minute=56)) is True
        _recent.clear()
        assert await daily_tick(datetime(2026, 9, 24, 18, 55, tzinfo=MSK)) is True
        globals()["_trading_day"] = lambda dd: False
        assert await daily_tick(datetime(2026, 9, 25, 18, 55, tzinfo=MSK)) is False
        globals()["_trading_day"] = lambda dd: True
        config.PYTHIA_TG_DAILY = False
        assert await daily_tick(datetime(2026, 9, 26, 18, 55, tzinfo=MSK)) is False
        config.PYTHIA_TG_DAILY = True
        await flush()
        assert len(SENT) == 3 and all("Итоги дня" in s["text"] for s in SENT)
        # 9) рынок: первый замер молчит, переходы — сообщение с позицией
        SENT.clear(); _recent.clear()
        assert await market_tick(False) is False and await market_tick(False) is False
        assert await market_tick(True) is True and await market_tick(True) is False and await market_tick(False) is True
        assert await market_tick(None) is False
        await flush()
        assert "🔔 <b>Рынок открылся</b> — позиция SBER: лонг 2 лот" in SENT[0]["text"] and "Рынок закрылся" in SENT[1]["text"] \
            and SENT[1]["disable_notification"] is True
        # 10) long polling: ответы на команды из обновлений, offset растёт, чужой чат отвечает в свой чат
        SENT.clear(); _recent.clear()
        UPDATES.extend([upd("/status"), upd("/help", chat="999"), {"update_id": 5000, "channel_post": {"text": "x"}}])
        assert await poll_once() == 3 and globals()["_offset"] == 5001
        await flush()
        assert SENT[0]["chat_id"] == "4242" and "SBER" in SENT[0]["text"]
        assert SENT[1]["chat_id"] == "999" and "<code>999</code>" in SENT[1]["text"]
        assert await poll_once() == 0
        # 11) выключено / не привязан — ничего не шлётся, on_event молчит
        config.PYTHIA_TG = False
        assert not enabled() and send("x") == 0
        config.PYTHIA_TG = True
        config.TG_CHAT_ID = ""
        assert send("x") == 0 and not bound()
        await on_event(evs[0])
        assert not _batch
        config.TG_CHAT_ID = "4242"
        assert (await check_token())["ok"] and (await check_token())["name"] == "pythia_test_bot"
        # 12) обёртка sink: фронт получает всё, бот — только своё; сбой фронта не мешает боту
        got = []

        async def inner(ev):
            got.append(ev)
            raise RuntimeError("фронт упал")
        sink = wrap_sink(inner)
        await sink(evs[0])
        await sink({"type": "text", "delta": "x"})
        assert len(got) == 2 and len(_batch) == 1
        await flush_batches()
        assert not _batch and _Q
        _Q.clear()

    asyncio.run(main())
    print("telegram self-test OK: привязка чата, команды (/status /pos /trades /day /explain /health /council), "
          "подтверждение /stop и /panic, очередь (дедуп, дробление, 429/401/400), события шины по узлам, "
          "health раз в 10 мин, итоги дня по слотам МСК, рынок открылся/закрылся, long polling, обёртка sink")
