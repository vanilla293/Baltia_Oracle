"""Планировщик: один тик раз в 15 с.

За тик по порядку:
  1) сработавшие напоминания (next_at ≤ сейчас): сообщение с кнопками, запись в диалог,
     следующий next_at по повтору (пропущенные за время простоя — одним сообщением);
     follow-up'ы бота — не сырым текстом, а через agent.proactive в фоне;
  2) отложенные («💤») — повтор без изменения расписания;
  3) «долбилки» — каждые nag_interval_min, фразы с эскалацией, после nag_max — «сдаюсь»;
  4) ежедневные задачи по местному времени (сводка, дни рождения, дайджест, рефлексия) —
     раз в сутки (kv `job:<имя>`), с догоном в течение 3 ч; тяжёлое — в фоне;
  5) уборка: сворачивание старого диалога (agent.summarize_old), не чаще раза в 10 мин.

Тик не падает: каждый шаг и каждая строка — в своём try/except. Не отправилось сообщение —
состояние не двигаем и пробуем на следующем тике (но не дольше SEND_GIVE_UP, чтобы не застрять).
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

from .. import timeutil
from ..tools.base import Buttons, ToolContext

log = logging.getLogger("oracle.scheduler")

UTC = timeutil.UTC
LATE_AFTER = timedelta(minutes=10)      # опоздали больше — «пропустил, пока был выключен»
CATCH_UP = timedelta(hours=3)           # ежедневную задачу догоняем не позже, чем через 3 ч
STALE_NAG = timedelta(hours=1)          # долбёжка, просроченная на час (бот лежал), — уже не к месту
STALE_WAKE = timedelta(hours=1)         # будильник, опоздавший на час, не долбит
SEND_TIMEOUT = 60.0                     # секунд на одну отправку
SEND_GIVE_UP = timedelta(minutes=30)    # столько пробуем переотправить, потом двигаем состояние
SUMMARY_EVERY = timedelta(minutes=10)
BATCH = 50                              # строк на шаг за тик (остальное — на следующем)
ERR_MAX = 300

JOBS = (("morning", "morning_brief_time"), ("birthdays", "birthday_time"),
        ("news", "news_digest_time"), ("reflection", "reflection_time"))

FOLLOWUP_TRIGGER = "Ты сам поставил себе вернуться к теме: {text}"
GIVE_UP = "Всё, сдаюсь. «{text}» — отметь, когда сделаешь."
STALE_GIVE_UP = "Пока я был выключен, долбить было некому. «{text}» — отметь, когда сделаешь."

# эскалация: от вежливого к настырному; последние — с перчиком, но без наездов на него самого
NAG_LINES = (
    "Напоминаю.",
    "Ещё раз, на всякий случай.",
    "Висит неотмеченным. Я вижу.",
    "Не хочу быть занудой, но придётся.",
    "Это всё ещё актуально.",
    "Я не отстану, ты же знаешь.",
    "Сделай — и нажми «Готово». Больше ничего не прошу.",
    "Напоминание, которое нельзя смахнуть, — это я.",
    "Можно отложить кнопкой. Можно сделать. Молчать — нельзя.",
    "Я терпеливый. Но не бесконечно.",
    "Давай уже, а? Там дел на пять минут.",
    "Знаю, ты сейчас думаешь «потом». Не потом.",
    "Серьёзно, сколько можно откладывать.",
    "Я уже повторяюсь. Дело — тоже никуда не делось.",
    "Блин, ну сделай ты это.",
    "Это не спам, это твоё же дело.",
    "Чёрт возьми, я не просто так пишу.",
    "Да ёлки-палки. Одна кнопка.",
    "Задолбался напоминать. Но продолжаю.",
    "Последние заходы. Потом сдамся — и это будет на твоей совести.",
)

WAKE_LINES = (
    "Доброе утро. Пора вставать.",
    "Подъём, подъём.",
    "Ну давай, вставай.",
    "Глаза открыл? Теперь ноги на пол.",
    "Будильник, который нельзя выключить, — это я.",
    "«Ещё пять минут» — самая популярная утренняя ложь.",
    "Я не отстану.",
    "Одеяло — не аргумент.",
    "Кофе сам себя не сварит.",
    "Вставай, день сам себя не проживёт.",
    "Я могу так хоть до обеда. У меня нет кнопки «сон».",
    "Серьёзно, хватит валяться, блин.",
    "Вчерашний ты поставил этот будильник. Не подводи его.",
    "Всё ещё лежишь? Кнопку-то не нажали — я вижу.",
    "Мир уже проснулся. Догоняй.",
    "Вставай, чёрт возьми. Ты же сам просил разбудить.",
    "Подъём! Это не просьба.",
    "Да вставай ты уже, ёлки-палки.",
    "Я на тебя рассчитываю. И вчерашний ты — тоже.",
    "Последнее предупреждение. Потом сдамся — и проспишь ты, а не я.",
)


# ── тексты и кнопки ──────────────────────────────────────────────────────────
def nag_line(count: int, nag_max: int, wake: bool = False) -> str:
    """Фраза для count-й долбёжки из nag_max: эскалация растянута на весь диапазон —
    первая всегда самая вежливая, последняя — самая настырная."""
    lines = WAKE_LINES if wake else NAG_LINES
    n = len(lines)
    count, nag_max = max(1, int(count)), max(1, int(nag_max))
    if nag_max <= 1:
        return lines[0]
    i = round((min(count, nag_max) - 1) * (n - 1) / (nag_max - 1))
    return lines[max(0, min(n - 1, i))]


def fire_text(row: dict) -> str:
    """Текст срабатывания по виду напоминания."""
    text = str(row.get("text") or "").strip() or "(без текста)"
    kind = row.get("kind") or "reminder"
    if kind == "wake":
        return f"⏰ ПОДЪЁМ! {text}"
    if kind == "event":
        return f"📅 {text}"
    if kind == "followup":
        return f"🔔 {text}"
    return f"⏰ {text}"


def reminder_buttons(row: dict) -> Buttons | None:
    """Кнопки под напоминанием: готово / отложить (у follow-up'а кнопок нет)."""
    rid = int(row["id"])
    kind = row.get("kind") or "reminder"
    if kind == "followup":
        return None
    if kind == "wake":
        return [[("✅ Встал", f"rem:done:{rid}"), ("💤 5 мин", f"rem:snz:{rid}:5")]]
    return [[("✅ Готово", f"rem:done:{rid}"), ("💤 10 мин", f"rem:snz:{rid}:10"),
             ("💤 1 час", f"rem:snz:{rid}:60")]]


def job_key(name: str) -> str:
    return f"job:{name}"


def parse_hhmm(v: Any) -> time | None:
    """'08:00' → time(8, 0); пусто/кривое → None (задача выключена)."""
    m = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", str(v or ""))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return time(h, mi) if h < 24 and mi < 60 else None


def _zone(tz: Any, fallback: ZoneInfo) -> ZoneInfo:
    if isinstance(tz, ZoneInfo):
        return tz
    try:
        return ZoneInfo(str(tz)) if tz else fallback
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return fallback


def _int(v: Any, default: int) -> int:
    try:
        return int(v) if v is not None and not isinstance(v, bool) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _err(e: BaseException) -> str:
    s = str(e).strip() or type(e).__name__
    return s if len(s) <= ERR_MAX else s[: ERR_MAX - 1] + "…"


# ── повторы: быстрый пересчёт ────────────────────────────────────────────────
_PERIOD = {"MINUTELY": timedelta(minutes=1), "HOURLY": timedelta(hours=1),
           "DAILY": timedelta(days=1), "WEEKLY": timedelta(weeks=1)}


def _ff_start(rule: str, start: datetime, target: datetime) -> datetime:
    """dateutil перебирает вхождения от самого DTSTART: у «каждые 5 минут» полугодовой давности —
    десятки тысяч шагов на каждом срабатывании. Сдвигаем DTSTART к target на целое число периодов:
    сетка вхождений та же (для правил без COUNT/BYSETPOS)."""
    parts = dict(p.split("=", 1) for p in rule.upper().split(";") if "=" in p)
    per = _PERIOD.get(parts.get("FREQ", ""))
    if per is None or "COUNT" in parts or "BYSETPOS" in parts:
        return start
    try:
        step = per * max(1, int(parts.get("INTERVAL", "1")))
    except ValueError:
        return start
    k = (target - start) // step - 1
    return start + step * k if k > 0 else start


def _start_naive(local_start: Any) -> datetime:
    d = local_start if isinstance(local_start, datetime) else datetime.fromisoformat(str(local_start))
    return d.replace(tzinfo=None)


def next_after(rule: str, local_start: Any, tz: ZoneInfo, after: datetime) -> datetime | None:
    """Следующее вхождение повтора строго после `after` (UTC) → UTC или None (повтор кончился)."""
    start = _start_naive(local_start)
    target = after.astimezone(tz).replace(tzinfo=None)
    return timeutil.next_occurrence(rule, _ff_start(rule, start, target), tz, after=after)


def last_occurrence(rule: str | None, local_start: Any, tz: ZoneInfo, now: datetime,
                    due: datetime) -> datetime:
    """Последнее плановое вхождение ≤ now (UTC), но не раньше due; для разового — сам due.
    Нужно, чтобы «пропустил» считалось от свежего вхождения, а не от первого пропущенного."""
    if not rule:
        return due
    try:
        start = _start_naive(local_start)
        target = now.astimezone(tz).replace(tzinfo=None, microsecond=0)
        rr = rrulestr(timeutil.normalize_rrule(rule), dtstart=_ff_start(rule, start, target))
        prev = rr.before(target, inc=True)
        best = due
        # в «повторённый» час перехода на зимнее наивное сравнение ошибается — дошагиваем вперёд
        for _ in range(500):
            if prev is None:
                break
            cand = timeutil.localize(prev, tz).astimezone(UTC)
            if cand > now:
                break
            best = max(best, cand)
            prev = rr.after(prev, inc=False)
        return best
    except Exception as e:   # кривое правило — не беда, считаем от due
        log.debug("last_occurrence(%r): %s", rule, e)
    return due


# ── планировщик ──────────────────────────────────────────────────────────────
class Scheduler:
    """Фоновый цикл напоминаний и ежедневных задач. `tick()` — один проход (тесты зовут его сами)."""

    def __init__(self, ctx: ToolContext, tick_seconds: float = 15.0):
        self.ctx = ctx
        self.tick_seconds = max(0.01, float(tick_seconds))
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._warned_no_notifier = False
        self._warned_times: set[str] = set()
        self._fail_since: dict[tuple[str, int], datetime] = {}
        self._last_summary: datetime | None = None
        self._summary_task: asyncio.Task | None = None
        self.ticks = 0

    # ── жизненный цикл ──
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Запустить цикл (нужен работающий event loop). Повторный вызов — ничего не делает."""
        self.ctx.services.scheduler = self
        if self.running:
            return
        self._task = asyncio.get_running_loop().create_task(self._loop(), name="oracle-scheduler")

    async def stop(self, timeout: float = 10.0) -> None:
        """Остановить цикл: дождаться конца текущего тика (до timeout), потом отменить."""
        task, self._task = self._task, None
        if task is None:
            return
        locked = False
        if not task.done():
            try:
                await asyncio.wait_for(self._lock.acquire(), timeout)
                locked = True
            except asyncio.TimeoutError:
                log.warning("планировщик: тик не закончился за %.0f с — прерываю", timeout)
        try:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("планировщик завершился с ошибкой")
        finally:
            if locked:
                self._lock.release()

    async def _loop(self) -> None:
        log.info("планировщик запущен (тик %.0f с)", self.tick_seconds)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("тик планировщика упал")
            await asyncio.sleep(self.tick_seconds)

    async def tick(self) -> None:
        """Один проход: напоминания → отложенные → долбёжка → ежедневные задачи → уборка."""
        async with self._lock:
            now = timeutil.now_utc()
            self.ticks += 1
            for step in (self._due, self._snoozed, self._nags, self._daily, self._housekeeping):
                try:
                    await step(now)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("планировщик: шаг %s упал", step.__name__)

    # ── отправка ──
    async def _deliver(self, key: tuple[str, int], text: str, buttons: Buttons | None,
                       now: datetime) -> bool:
        """Отправить владельцу. False — не вышло, состояние не двигать (повторим на следующем тике).
        Не выходит дольше SEND_GIVE_UP — сдаёмся и двигаем (True), чтобы не застрять навсегда."""
        notifier = self.ctx.services.notifier
        if notifier is None:
            if not self._warned_no_notifier:
                log.warning("планировщик: нет notifier — сообщения не отправляются, состояние двигается")
                self._warned_no_notifier = True
            return True
        try:
            await asyncio.wait_for(notifier.send(text, buttons=buttons), SEND_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            since = self._fail_since.setdefault(key, now)
            if now - since < SEND_GIVE_UP:
                log.warning("не смог отправить %s:%s (%s) — повторю", key[0], key[1], _err(e))
                return False
            log.error("не могу отправить %s:%s уже %s — пропускаю", key[0], key[1], now - since)
            self._fail_since.pop(key, None)
            return True
        self._fail_since.pop(key, None)
        return True

    async def _send_bg(self, text: str, buttons: Buttons | None = None) -> None:
        """Отправка из фоновой задачи: без повторов, ошибки — в лог."""
        notifier = self.ctx.services.notifier
        if notifier is None:
            log.info("нет notifier — сообщение не отправлено: %.80s", text)
            return
        try:
            await asyncio.wait_for(notifier.send(text, buttons=buttons), SEND_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("не смог отправить сообщение: %s", _err(e))

    async def _record(self, text: str) -> None:
        try:
            await self.ctx.db.add_message("event", text, "system")
        except Exception:
            log.exception("не записал событие в диалог")

    def _nag_params(self, row: dict) -> tuple[int, int]:
        cfg = self.ctx.cfg
        interval = max(1, _int(row.get("nag_interval_min"), cfg.nag_interval_min))
        nag_max = max(1, _int(row.get("nag_max"), cfg.nag_max))
        return interval, nag_max

    # ── 1) срабатывания ──
    async def _due(self, now: datetime) -> None:
        rows = await self.ctx.db.fetchall(
            "SELECT * FROM reminders WHERE status='active' AND next_at IS NOT NULL AND next_at <= ? "
            "ORDER BY next_at, id LIMIT ?", (timeutil.iso(now), BATCH))
        for row in rows:
            try:
                await self.fire(row, now)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("напоминание #%s: срабатывание упало", row.get("id"))
                await self._unstick(row)

    async def _unstick(self, row: dict) -> None:
        """Срабатывание упало посреди дела — снять next_at, чтобы строка не долбила каждый тик."""
        try:
            await self.ctx.db.execute(
                "UPDATE reminders SET next_at=NULL, status=CASE WHEN nag_active=1 THEN status ELSE 'done' END "
                "WHERE id=?", (row["id"],))
        except Exception:
            log.exception("напоминание #%s: не смог снять", row.get("id"))

    async def fire(self, row: dict, now: datetime | None = None) -> bool:
        """Сработать напоминанию: сообщение (или follow-up через агента), следующий next_at,
        долбёжка, запись в диалог. False — отправка не удалась, повторим на следующем тике."""
        now = now or timeutil.now_utc()
        rid = int(row["id"])
        kind = row.get("kind") or "reminder"
        tz = _zone(row.get("tz"), self.ctx.tz)
        text = str(row.get("text") or "").strip() or "(без текста)"
        rule = row.get("rrule") or None
        due = timeutil.from_iso(row.get("next_at")) or now
        occurred = last_occurrence(rule, row.get("local_start"), tz, now, due)
        late = now - occurred > LATE_AFTER

        nxt: datetime | None = None
        if rule:
            try:
                nxt = next_after(rule, row.get("local_start"), tz, now)
            except Exception as e:   # битое правило в базе — срабатывает последний раз
                log.warning("напоминание #%s: правило %r не считается (%s) — больше не повторяю", rid, rule, e)
        nag = bool(row.get("nag")) and kind != "followup"
        if nag and kind == "wake" and now - occurred > STALE_WAKE:
            nag = False   # будильник, проспанный вместе с ботом, долбить поздно
        interval, _ = self._nag_params(row)
        status = row.get("status") or "active"
        if nxt is None and not nag:
            status = "done"
        params = (timeutil.iso(now), timeutil.iso(nxt) if nxt else None, int(nag),
                  timeutil.iso(now + timedelta(minutes=interval)) if nag else None, status, rid)
        update = ("UPDATE reminders SET fire_count=fire_count+1, last_fired_at=?, next_at=?, snooze_at=NULL, "
                  "nag_active=?, nag_count=0, nag_next_at=?, status=? WHERE id=?")

        agent = self.ctx.services.agent
        if kind == "followup" and agent is not None:
            await self.ctx.db.execute(update, params)
            self.ctx.services.spawn(self._followup(rid, text), name=f"followup:{rid}")
            return True

        msg = fire_text(row)
        if late:
            msg = f"(пропустил, пока был выключен — было на {timeutil.fmt_local(occurred, tz)}) {msg}"
        if not await self._deliver(("fire", rid), msg, reminder_buttons(row), now):
            return False
        await self.ctx.db.execute(update, params)
        if kind == "followup":
            await self._record(f"Бот сам вернулся к теме (follow-up #{rid}): {text}")
        else:
            await self._record(f"Сработало напоминание #{rid}: {text}")
        return True

    async def _followup(self, rid: int, text: str) -> None:
        """Фон: агент сам пишет по теме follow-up'а; не вышло — короткое напоминание."""
        agent = self.ctx.services.agent
        msg = ""
        try:
            res = await agent.proactive(FOLLOWUP_TRIGGER.format(text=text))
            msg = str(getattr(res, "text", res) or "").strip()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("follow-up #%s: агент не ответил: %s", rid, _err(e))
        if not msg:
            msg = f"🔔 Хотел вернуться к теме: {text}"
            await self._record(f"Бот сам вернулся к теме (follow-up #{rid}): {text}")
        await self._send_bg(msg)

    # ── 2) отложенные ──
    async def _snoozed(self, now: datetime) -> None:
        rows = await self.ctx.db.fetchall(
            "SELECT * FROM reminders WHERE status='active' AND snooze_at IS NOT NULL AND snooze_at <= ? "
            "ORDER BY snooze_at, id LIMIT ?", (timeutil.iso(now), BATCH))
        for row in rows:
            try:
                await self._refire(row, now)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("напоминание #%s: повтор отложенного упал", row.get("id"))
                try:
                    await self.ctx.db.execute("UPDATE reminders SET snooze_at=NULL WHERE id=?", (row["id"],))
                except Exception:
                    log.exception("напоминание #%s: не снял snooze_at", row.get("id"))

    async def _refire(self, row: dict, now: datetime) -> bool:
        """Повтор отложенного: как срабатывание, но расписание (next_at/rrule) не трогаем."""
        rid = int(row["id"])
        kind = row.get("kind") or "reminder"
        text = str(row.get("text") or "").strip() or "(без текста)"
        nag = bool(row.get("nag")) and kind != "followup"
        interval, _ = self._nag_params(row)
        # разовое без долбёжки после повтора закрывается — иначе висело бы «активным» без срабатываний
        status = "done" if not nag and not row.get("next_at") else (row.get("status") or "active")
        update = ("UPDATE reminders SET snooze_at=NULL, last_fired_at=?, nag_active=?, nag_count=0, "
                  "nag_next_at=?, status=? WHERE id=?")
        params = (timeutil.iso(now), int(nag), timeutil.iso(now + timedelta(minutes=interval)) if nag else None,
                  status, rid)
        if kind == "followup" and self.ctx.services.agent is not None:
            await self.ctx.db.execute(update, params)
            self.ctx.services.spawn(self._followup(rid, text), name=f"followup:{rid}")
            return True
        if not await self._deliver(("snooze", rid), "💤→ " + fire_text(row), reminder_buttons(row), now):
            return False
        await self.ctx.db.execute(update, params)
        await self._record(f"Сработало отложенное напоминание #{rid}: {text}")
        return True

    # ── 3) долбёжка ──
    async def _nags(self, now: datetime) -> None:
        rows = await self.ctx.db.fetchall(
            "SELECT * FROM reminders WHERE status='active' AND nag_active=1 AND nag_next_at IS NOT NULL "
            "AND nag_next_at <= ? ORDER BY nag_next_at, id LIMIT ?", (timeutil.iso(now), BATCH))
        for row in rows:
            try:
                await self._nag(row, now)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("напоминание #%s: долбёжка упала", row.get("id"))
                try:
                    await self.ctx.db.execute(
                        "UPDATE reminders SET nag_active=0, nag_next_at=NULL WHERE id=?", (row["id"],))
                except Exception:
                    log.exception("напоминание #%s: не снял долбёжку", row.get("id"))

    async def _nag(self, row: dict, now: datetime) -> bool:
        rid = int(row["id"])
        text = str(row.get("text") or "").strip() or "(без текста)"
        interval, nag_max = self._nag_params(row)
        count = _int(row.get("nag_count"), 0) + 1
        planned = timeutil.from_iso(row.get("nag_next_at")) or now
        stale = now - planned > max(STALE_NAG, timedelta(minutes=3 * interval))
        if count > nag_max or stale:
            msg = (STALE_GIVE_UP if stale and count <= nag_max else GIVE_UP).format(text=text)
            if not await self._deliver(("nag", rid), msg, None, now):
                return False
            await self.ctx.db.execute(
                "UPDATE reminders SET nag_active=0, nag_next_at=NULL, nag_count=?, "
                "status=CASE WHEN next_at IS NULL THEN 'done' ELSE status END WHERE id=?",
                (min(count, nag_max), rid))
            return True
        line = nag_line(count, nag_max, wake=row.get("kind") == "wake")
        msg = f"{line}\n⏰ {text}  (#{rid}, {count}/{nag_max})"
        if not await self._deliver(("nag", rid), msg, reminder_buttons(row), now):
            return False
        await self.ctx.db.execute(
            "UPDATE reminders SET nag_count=?, nag_next_at=? WHERE id=?",
            (count, timeutil.iso(now + timedelta(minutes=interval)), rid))
        return True

    # ── 4) ежедневные задачи ──
    def _job_time(self, name: str, attr: str) -> time | None:
        raw = str(getattr(self.ctx.cfg, attr, "") or "").strip()
        if not raw:
            return None
        t = parse_hhmm(raw)
        if t is None and attr not in self._warned_times:
            log.warning("планировщик: %s=%r — не время HH:MM, задача %s выключена", attr, raw, name)
            self._warned_times.add(attr)
        return t

    async def _daily(self, now: datetime) -> None:
        tz = self.ctx.tz
        today = now.astimezone(tz).date()
        for name, attr in JOBS:
            t = self._job_time(name, attr)
            if t is None:
                continue
            day = today
            at = datetime.combine(day, t).replace(tzinfo=tz).astimezone(UTC)
            if now < at:        # сегодняшнее время ещё не пришло — может, догоняем вчерашнее (через полночь)
                day = today - timedelta(days=1)
                at = datetime.combine(day, t).replace(tzinfo=tz).astimezone(UTC)
            key = job_key(name)
            if await self.ctx.db.kv_get(key) == day.isoformat():
                continue
            await self.ctx.db.kv_set(key, day.isoformat())      # отметка ДО запуска: не задвоится
            if now - at > CATCH_UP:
                log.info("ежедневная задача %s за %s пропущена: бот лежал дольше %s", name, day, CATCH_UP)
                continue
            log.info("ежедневная задача %s за %s", name, day)
            self.ctx.services.spawn(self._job(name), name=f"job:{name}")

    async def run_job(self, name: str) -> None:
        """Выполнить ежедневную задачу сразу (без отметки в kv) — для команд бота и тестов."""
        await self._job(name)

    async def _job(self, name: str) -> None:
        ctx = self.ctx.child()
        try:
            if name == "morning":
                from . import brief
                text = await brief.morning_brief(ctx)
                if text.strip():
                    await self._send_bg(text)
                    await self._record(f"Утренняя сводка владельцу:\n{text[:2000]}")
            elif name == "birthdays":
                from ..tools import birthdays
                n = await birthdays.birthday_jobs(ctx)
                log.info("дни рождения: отправлено %s", n)
            elif name == "news":
                from ..tools import news
                text = await news.news_digest(ctx)
                if text.strip():
                    await self._send_bg("🗞 " + text.strip())
                    short = text.strip()[:1500] + ("…" if len(text.strip()) > 1500 else "")
                    await self._record(f"Дайджест новостей владельцу:\n{short}")
            elif name == "reflection":
                agent = ctx.services.agent
                if agent is None:
                    log.info("рефлексия: агента нет — пропускаю")
                    return
                await agent.reflect()
            else:
                log.warning("неизвестная ежедневная задача %s", name)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("ежедневная задача %s упала", name)
            if name == "morning":
                await self._send_bg(f"не собрал сводку: {_err(e)}")
            elif name == "news":
                await self._send_bg(f"не собрал сводку новостей: {_err(e)}")

    # ── 5) уборка ──
    async def _housekeeping(self, now: datetime) -> None:
        agent = self.ctx.services.agent
        if agent is None or not hasattr(agent, "summarize_old"):
            return
        if self._summary_task is not None and not self._summary_task.done():
            return
        if self._last_summary is not None and now - self._last_summary < SUMMARY_EVERY:
            return
        cfg = self.ctx.cfg
        n = _int(await self.ctx.db.scalar("SELECT COUNT(*) FROM messages WHERE summarized=0"), 0)
        if n <= cfg.history_messages + cfg.summary_chunk:
            return
        self._last_summary = now
        self._summary_task = self.ctx.services.spawn(self._summarize(agent), name="summarize")

    async def _summarize(self, agent: Any) -> None:
        try:
            await agent.summarize_old()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("сворачивание диалога не вышло: %s", _err(e))
