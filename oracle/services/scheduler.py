"""Планировщик: один тик раз в 15 с.

За тик по порядку:
  1) сработавшие напоминания (next_at ≤ сейчас): сообщение с кнопками, запись в диалог,
     следующий next_at по повтору (пропущенные за время простоя — одним сообщением);
     follow-up'ы бота — не сырым текстом, а через agent.proactive в фоне;
  2) отложенные («💤») — повтор без изменения расписания (проспанное вместе с ботом — одно «пропустил»,
     без новой долбёжки);
  3) «долбилки» — каждые nag_interval_min, фразы с эскалацией, после nag_max — «сдаюсь»
     («Поздравить…» с ДР — только до 22:00, и после «💤» тоже);
  4) ежедневные задачи по местному времени (сводка, дни рождения, дайджест, рефлексия) —
     раз в сутки, в фоне. kv `job:<имя>` ставится только после того, как задача сделана: не дошло
     (сеть, перезапуск) — повтор через JOB_RETRY, пока не выйдет окно догона CATCH_UP (3 ч; дни
     рождения догоняются весь день); сгенерированный текст при повторе не пишется заново;
  5) уборка: сворачивание старого диалога (agent.summarize_old), не чаще раза в 10 мин.

Тик не падает: каждый шаг и каждая строка — в своём try/except. Не отправилось сообщение —
состояние не двигаем и пробуем на следующем тике. Нет связи с Telegram (сеть, таймаут, 5xx) —
ждём сколько угодно и отправляем с пометкой «пропустил», когда связь вернётся (остаток тика после
первой такой ошибки не тратим). Telegram отказывает всерьёз (бот заблокирован, чат не найден, кривое
сообщение) — пробуем SEND_GIVE_UP, потом двигаем состояние, пишем в диалог «не смог доставить» и
сообщаем владельцу при первой удачной отправке.
Состояние после отправки пишется, только если строку за это время не тронули (отмена, отметка, перенос).
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

from .. import persona, timeutil
from ..tools.base import Buttons, ToolContext

log = logging.getLogger("oracle.scheduler")

UTC = timeutil.UTC
LATE_AFTER = timedelta(minutes=10)      # опоздали больше — «пропустил, пока был выключен»
CATCH_UP = timedelta(hours=3)           # ежедневную задачу догоняем не позже, чем через 3 ч
STALE_NAG = timedelta(hours=1)          # долбёжка, просроченная на час (бот лежал), — уже не к месту
STALE_WAKE = timedelta(hours=1)         # будильник, опоздавший на час, не долбит
SEND_TIMEOUT = 60.0                     # секунд на одну отправку
SEND_GIVE_UP = timedelta(minutes=30)    # столько пробуем переотправить при отказе Telegram, потом двигаем
JOB_RETRY = (timedelta(minutes=5), timedelta(minutes=10), timedelta(minutes=20), timedelta(minutes=30))
FOLLOWUP_RETRY = (15, 60, 300, 300, 300, 300, 300, 300)   # паузы (с) между попытками follow-up'а, ≈ 30 мин
UNDELIVERED_KEY = "undelivered"         # kv: что не смогли доставить — скажем при первой удачной отправке
UNDELIVERED_MAX = 10
SUMMARY_EVERY = timedelta(minutes=10)
BATCH = 50                              # строк на шаг за тик (остальное — на следующем)
ERR_MAX = 300
RECORD_MAX = 5000                       # показанное владельцу — в разговор не длиннее (= handlers.RECORD_MAX)

JOBS = (("morning", "morning_brief_time"), ("birthdays", "birthday_time"),
        ("news", "news_digest_time"), ("reflection", "reflection_time"))

FOLLOWUP_TRIGGER = "Ты сам поставил себе вернуться к теме: {text}"
GIVE_UP = "Всё, сдаюсь. «{text}» — отметь, когда сделаешь."
STALE_GIVE_UP = "Пока я был выключен, долбить было некому. «{text}» — отметь, когда сделаешь."
STALE_GIVE_UP_NET = "Пока не было связи с Telegram, долбить не получалось. «{text}» — отметь, когда сделаешь."
WAKE_GIVE_UP = "Всё, сдаюсь — похоже, проспал."
STALE_WAKE_GIVE_UP = "Пока я был выключен, будить было некому — надеюсь, встал сам."
STALE_WAKE_GIVE_UP_NET = "Пока не было связи с Telegram, разбудить не получалось — надеюсь, встал сам."
GAVE_UP = "gave_up"     # _deliver: так и не доставили — состояние двигаем, но в диалог пишем «не смог»

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
    "Глаза открыты? Теперь ноги на пол.",
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


# OWNER_GENDER=f: те же фразы в женском роде
_FEMININE = {
    "Вчерашний ты поставил этот будильник. Не подводи его.": "Вчерашняя ты поставила этот будильник. Не подводи её.",
    "Вставай, чёрт возьми. Ты же сам просил разбудить.": "Вставай, чёрт возьми. Ты же сама просила разбудить.",
    "Я на тебя рассчитываю. И вчерашний ты — тоже.": "Я на тебя рассчитываю. И вчерашняя ты — тоже.",
    WAKE_GIVE_UP: "Всё, сдаюсь — похоже, проспала.",
    STALE_WAKE_GIVE_UP: "Пока я был выключен, будить было некому — надеюсь, встала сама.",
    STALE_WAKE_GIVE_UP_NET: "Пока не было связи с Telegram, разбудить не получалось — надеюсь, встала сама.",
}
WAKE_LINES_F = tuple(_FEMININE.get(x, x) for x in WAKE_LINES)
WAKE_FIRST_NOT_MORNING = "Пора вставать."       # первая фраза, если будит не утром (дневной сон)


def gendered(text: str, female: bool) -> str:
    """Фраза о владельце в его роде (OWNER_GENDER)."""
    return _FEMININE.get(text, text) if female else text


# ── тексты и кнопки ──────────────────────────────────────────────────────────
def nag_line(count: int, nag_max: int, wake: bool = False, female: bool = False, morning: bool = True) -> str:
    """Фраза для count-й долбёжки из nag_max: эскалация растянута на весь диапазон —
    первая всегда самая вежливая, последняя — самая настырная. morning=False — будильник не утром:
    без «Доброе утро»."""
    lines = (WAKE_LINES_F if female else WAKE_LINES) if wake else NAG_LINES
    n = len(lines)
    count, nag_max = max(1, int(count)), max(1, int(nag_max))
    i = 0 if nag_max <= 1 else max(0, min(n - 1, round((min(count, nag_max) - 1) * (n - 1) / (nag_max - 1))))
    if wake and i == 0 and not morning:
        return WAKE_FIRST_NOT_MORNING
    return lines[i]


def is_morning(local: datetime) -> bool:
    """Утро (4–12 ч): только тогда «Доброе утро»."""
    return 4 <= local.hour < 12


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


def reminder_buttons(row: dict, female: bool = False) -> Buttons | None:
    """Кнопки под напоминанием: готово / отложить (у follow-up'а кнопок нет). female — «Встала»."""
    rid = int(row["id"])
    kind = row.get("kind") or "reminder"
    if kind == "followup":
        return None
    if kind == "wake":
        return [[("✅ Встала" if female else "✅ Встал", f"rem:done:{rid}"), ("💤 5 мин", f"rem:snz:{rid}:5")]]
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


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _int(v: Any, default: int) -> int:
    try:
        return int(v) if v is not None and not isinstance(v, bool) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _err(e: BaseException) -> str:
    s = str(e).strip() or type(e).__name__
    return s if len(s) <= ERR_MAX else s[: ERR_MAX - 1] + "…"


# сеть/сервер Telegram недоступны — стоит повторить позже, а не сдаваться. Классы aiogram и aiohttp
# узнаём по имени: services/ не импортирует aiogram. TelegramEntityTooLarge — потомок TelegramNetworkError,
# но он навсегда.
_TRANSIENT = {"TransientSendError", "TelegramNetworkError", "TelegramServerError", "TelegramRetryAfter",
              "ClientError", "ServerDisconnectedError"}
_PERMANENT = {"TelegramEntityTooLarge"}


def is_transient(e: BaseException) -> bool:
    """Временная ли ошибка отправки (нет сети, таймаут, 5xx, флуд-контроль)."""
    names = {c.__name__ for c in type(e).__mro__}
    if names & _PERMANENT:
        return False
    if names & _TRANSIENT or isinstance(e, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    return isinstance(e, OSError) and not isinstance(e, (PermissionError, FileNotFoundError))


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
        self._outage: tuple[datetime, datetime | None] | None = None   # последний обрыв связи: (с, по)
        self._offline = False                              # в этом тике сеть уже падала — остальное потом
        self._job_tasks: dict[str, asyncio.Task] = {}
        self._job_retry_at: dict[str, datetime] = {}
        self._job_fails: dict[str, int] = {}
        self._job_text: dict[tuple[str, date], str] = {}   # готовый текст сводки/дайджеста — для повтора
        self._job_sent: dict[tuple[str, date], list[int]] = {}   # …и сколько его кусков уже дошло
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
            self._offline = False
            for step in (self._due, self._snoozed, self._nags, self._daily, self._housekeeping):
                try:
                    await step(now)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("планировщик: шаг %s упал", step.__name__)

    # ── отправка ──
    async def _deliver(self, key: tuple[str, int], text: str, buttons: Buttons | None,
                       now: datetime) -> bool | str:
        """Отправить владельцу. True — ушло; False — не вышло, состояние не двигать (повторим на
        следующем тике). Нет связи — ждём сколько угодно; Telegram отказывает дольше SEND_GIVE_UP —
        сдаёмся (GAVE_UP): состояние двигаем, чтобы не застрять навсегда, а владельцу скажем потом."""
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
            if is_transient(e):
                self._offline = True
                if self._outage is None or self._outage[1] is not None:
                    self._outage = (now, None)
                log.warning("нет связи с Telegram (%s) — %s:%s отправлю, когда появится", _err(e), key[0], key[1])
                return False
            since = self._fail_since.setdefault(key, now)
            if now - since < SEND_GIVE_UP:
                log.warning("не смог отправить %s:%s (%s) — повторю", key[0], key[1], _err(e))
                return False
            log.error("не могу отправить %s:%s уже %s (%s) — пропускаю", key[0], key[1], now - since, _err(e))
            self._fail_since.pop(key, None)
            await self._note_undelivered(text)
            return GAVE_UP
        self._fail_since.pop(key, None)
        if self._outage is not None and self._outage[1] is None:
            self._outage = (self._outage[0], now)          # связь вернулась
        await self._flush_undelivered(notifier)
        return True

    def _offline_at(self, occurred: datetime) -> bool:
        """Момент попал в обрыв связи с Telegram (бот при этом работал)?"""
        o = self._outage
        return o is not None and o[0] - timedelta(minutes=1) <= occurred and (o[1] is None or occurred <= o[1])

    def _missed_why(self, occurred: datetime) -> str:
        """Почему опоздали: бот лежал или не было связи с Telegram (вхождение попало в обрыв)."""
        return "пока не было связи" if self._offline_at(occurred) else "пока был выключен"

    async def _note_undelivered(self, text: str) -> None:
        try:
            items = await self.ctx.db.kv_get(UNDELIVERED_KEY) or []
            first = next((ln.strip() for ln in str(text).splitlines() if ln.strip()), "")
            items = (list(items) if isinstance(items, list) else []) + [first[:150]]
            await self.ctx.db.kv_set(UNDELIVERED_KEY, items[-UNDELIVERED_MAX:])
        except Exception:
            log.exception("не записал недоставленное")

    async def _flush_undelivered(self, notifier: Any) -> None:
        """Связь с владельцем есть — сказать, что раньше не дошло (один раз)."""
        try:
            items = await self.ctx.db.kv_get(UNDELIVERED_KEY)
            if not items:
                return
            await self.ctx.db.kv_delete(UNDELIVERED_KEY)
            lines = "\n".join(f"• {x}" for x in items if x)
            await asyncio.wait_for(notifier.send(f"⚠️ Раньше Telegram не принимал мои сообщения — "
                                                 f"не дошло:\n{lines}"), SEND_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("не сообщил о недоставленном: %s", _err(e))

    async def _send_once(self, text: str, buttons: Buttons | None = None,
                         progress: list[int] | None = None) -> bool:
        """Отправка из фоновой задачи, одна попытка → ушло ли (нет notifier — «ушло», делать нечего).
        progress — [сколько кусков длинного текста уже дошло]: повтор шлёт только недошедшие, а не весь
        дайджест заново (если notifier так умеет — BotNotifier.resumable)."""
        notifier = self.ctx.services.notifier
        if notifier is None:
            log.info("нет notifier — сообщение не отправлено: %.80s", text)
            return True
        kw: dict[str, Any] = {"progress": progress} \
            if progress is not None and getattr(notifier, "resumable", False) else {}
        try:
            await asyncio.wait_for(notifier.send(text, buttons=buttons, **kw), SEND_TIMEOUT)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            done = f" (дошло кусков: {progress[0]})" if kw and progress and progress[0] else ""
            log.warning("не смог отправить сообщение%s: %s", done, _err(e))
            return False

    async def _send_bg(self, text: str, buttons: Buttons | None = None) -> bool:
        """Отправка из фоновой задачи с повторами (FOLLOWUP_RETRY, ≈ SEND_GIVE_UP) → ушло ли."""
        progress = [0]
        if await self._send_once(text, buttons, progress):
            return True
        for pause in FOLLOWUP_RETRY:
            await asyncio.sleep(pause)
            if await self._send_once(text, buttons, progress):
                return True
        log.error("так и не отправил сообщение: %.80s", text)
        return False

    async def _record(self, text: str) -> None:
        try:
            await self.ctx.db.add_message("event", text, "system")
        except Exception:
            log.exception("не записал событие в диалог")

    def _female(self) -> bool:
        return persona.is_female(getattr(self.ctx.cfg, "owner_gender", "m"))

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
            if self._offline:           # сети нет — остальное на следующем тике, а не по минуте на строку
                break
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

    async def _unchanged(self, rid: int, col: str, value: Any) -> bool:
        """Строка всё ещё та, что прочитали в начале тика (не отменили, не перенесли, не отложили)."""
        return bool(await self.ctx.db.scalar(
            f"SELECT 1 FROM reminders WHERE id=? AND status='active' AND {col} IS ?", (rid, value)))

    async def fire(self, row: dict, now: datetime | None = None) -> bool:
        """Сработать напоминанию: сообщение (или follow-up через агента), следующий next_at,
        долбёжка, запись в диалог. False — отправка не удалась, повторим на следующем тике.
        Пока шла отправка, строку отменили/перенесли — её новое состояние не затираем."""
        now = now or timeutil.now_utc()
        rid = int(row["id"])
        kind = row.get("kind") or "reminder"
        tz = _zone(row.get("tz"), self.ctx.tz)
        text = str(row.get("text") or "").strip() or "(без текста)"
        rule = row.get("rrule") or None
        seen_next = row.get("next_at")
        due = timeutil.from_iso(seen_next) or now
        occurred = last_occurrence(rule, row.get("local_start"), tz, now, due)
        late = now - occurred > LATE_AFTER

        nxt: datetime | None = None
        if rule:
            try:
                nxt = next_after(rule, row.get("local_start"), tz, now)
            except Exception as e:   # битое правило в базе — срабатывает последний раз
                log.warning("напоминание #%s: правило %r не считается (%s) — больше не повторяю", rid, rule, e)
        nag = bool(row.get("nag")) and kind != "followup"
        interval, _ = self._nag_params(row)
        # долбить о вхождении, проспанном вместе с ботом (или без связи), поздно: одно сообщение, без долбёжки
        stale = STALE_WAKE if kind == "wake" else max(STALE_NAG, timedelta(minutes=3 * interval))
        if nag and now - occurred > stale:
            nag = False
        status = row.get("status") or "active"
        if nxt is None and not nag:
            status = "done"
        params = (timeutil.iso(now), timeutil.iso(nxt) if nxt else None, int(nag),
                  timeutil.iso(now + timedelta(minutes=interval)) if nag else None, status, rid, seen_next)
        update = ("UPDATE reminders SET fire_count=fire_count+1, last_fired_at=?, next_at=?, snooze_at=NULL, "
                  "nag_active=?, nag_count=0, nag_next_at=?, status=? "
                  "WHERE id=? AND status='active' AND next_at IS ?")
        if not await self._unchanged(rid, "next_at", seen_next):
            return True     # отменили или перенесли, пока отправлялись строки раньше в этом тике

        agent = self.ctx.services.agent
        if kind == "followup" and agent is not None:
            if await self.ctx.db.execute(update, params):
                self.ctx.services.spawn(self._followup(rid, text), name=f"followup:{rid}")
            return True

        msg = fire_text(row)
        if late:
            msg = f"(пропустил, {self._missed_why(occurred)} — было на {timeutil.fmt_local(occurred, tz)}) {msg}"
        sent = await self._deliver(("fire", rid), msg, reminder_buttons(row, self._female()), now)
        if not sent:
            return False
        if not await self.ctx.db.execute(update, params):
            log.info("напоминание #%s изменили во время отправки — его состояние не трогаю", rid)
            return True
        if sent == GAVE_UP:
            await self._record(f"Не смог доставить напоминание #{rid} (Telegram не принимал): {text}")
        elif kind == "followup":
            await self._record(f"Бот сам вернулся к теме (follow-up #{rid}): {text}")
        else:
            await self._record(f"Сработало напоминание #{rid}: {text}")
        return True

    async def _followup(self, rid: int, text: str) -> None:
        """Фон: агент сам пишет по теме follow-up'а; не вышло — короткое напоминание.
        Сеть моргнула — повторяем (FOLLOWUP_RETRY), а не теряем."""
        agent = self.ctx.services.agent
        msg = ""
        try:
            res = await agent.proactive(FOLLOWUP_TRIGGER.format(text=text))
            msg = str(getattr(res, "text", res) or "").strip()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("follow-up #%s: агент не ответил: %s", rid, _err(e))
        fallback = not msg
        if fallback:
            msg = f"🔔 Хотел вернуться к теме: {text}"
        if await self._send_bg(msg) and fallback:
            await self._record(f"Бот сам вернулся к теме (follow-up #{rid}): {text}")

    # ── 2) отложенные ──
    async def _snoozed(self, now: datetime) -> None:
        rows = await self.ctx.db.fetchall(
            "SELECT * FROM reminders WHERE status='active' AND snooze_at IS NOT NULL AND snooze_at <= ? "
            "ORDER BY snooze_at, id LIMIT ?", (timeutil.iso(now), BATCH))
        for row in rows:
            if self._offline:
                break
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

    def _nag_until(self, row: dict) -> datetime | None:
        """Долбёжке «Поздравить…» (ref_type='birthday') — до NAG_UNTIL того дня; остальным — без срока."""
        if row.get("ref_type") != "birthday":
            return None
        try:
            from ..tools import birthdays
            tz = _zone(row.get("tz"), self.ctx.tz)
            return datetime.combine(_start_naive(row["local_start"]).date(), birthdays.NAG_UNTIL, tzinfo=tz)
        except Exception as e:
            log.debug("напоминание #%s: срок долбёжки не посчитан: %s", row.get("id"), e)
            return None

    async def _refire(self, row: dict, now: datetime) -> bool:
        """Повтор отложенного: как срабатывание, но расписание (next_at/rrule) не трогаем.
        Проспали отложенное вместе с ботом (или без связи) — как и в fire(): одно сообщение «пропустил»,
        без новой долбёжки."""
        rid = int(row["id"])
        kind = row.get("kind") or "reminder"
        tz = _zone(row.get("tz"), self.ctx.tz)
        text = str(row.get("text") or "").strip() or "(без текста)"
        nag = bool(row.get("nag")) and kind != "followup"
        interval, nag_max = self._nag_params(row)
        seen = row.get("snooze_at")
        planned = timeutil.from_iso(seen) or now
        stale = STALE_WAKE if kind == "wake" else max(STALE_NAG, timedelta(minutes=3 * interval))
        if nag and now - planned > stale:
            nag = False
        until = self._nag_until(row) if nag else None
        if until is not None:
            # «Поздравить…» после «💤» — снова только до вечера: последний нажим и «сдаюсь» не позже until
            nag_max = int((until - now) / timedelta(minutes=interval)) - 1
            if nag_max < 1:
                nag = False
        # разовое без долбёжки после повтора закрывается — иначе висело бы «активным» без срабатываний
        status = "done" if not nag and not row.get("next_at") else (row.get("status") or "active")
        update = ("UPDATE reminders SET snooze_at=NULL, last_fired_at=?, nag_active=?, nag_count=0, "
                  "nag_next_at=?, nag_max=COALESCE(?, nag_max), status=? "
                  "WHERE id=? AND status='active' AND snooze_at IS ?")
        params = (timeutil.iso(now), int(nag), timeutil.iso(now + timedelta(minutes=interval)) if nag else None,
                  nag_max if until is not None and nag else None, status, rid, seen)
        if not await self._unchanged(rid, "snooze_at", seen):
            return True
        if kind == "followup" and self.ctx.services.agent is not None:
            if await self.ctx.db.execute(update, params):
                self.ctx.services.spawn(self._followup(rid, text), name=f"followup:{rid}")
            return True
        msg = "💤→ " + fire_text(row)
        if now - planned > LATE_AFTER:
            msg = f"(пропустил, {self._missed_why(planned)} — было на {timeutil.fmt_local(planned, tz)}) {msg}"
        sent = await self._deliver(("snooze", rid), msg, reminder_buttons(row, self._female()), now)
        if not sent:
            return False
        if not await self.ctx.db.execute(update, params):
            return True
        if sent == GAVE_UP:
            await self._record(f"Не смог доставить отложенное напоминание #{rid} (Telegram не принимал): {text}")
        else:
            await self._record(f"Сработало отложенное напоминание #{rid}: {text}")
        return True

    # ── 3) долбёжка ──
    async def _nags(self, now: datetime) -> None:
        rows = await self.ctx.db.fetchall(
            "SELECT * FROM reminders WHERE status='active' AND nag_active=1 AND nag_next_at IS NOT NULL "
            "AND nag_next_at <= ? ORDER BY nag_next_at, id LIMIT ?", (timeutil.iso(now), BATCH))
        for row in rows:
            if self._offline:
                break
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
        wake = row.get("kind") == "wake"
        female = self._female()
        interval, nag_max = self._nag_params(row)
        count = _int(row.get("nag_count"), 0) + 1
        planned = timeutil.from_iso(row.get("nag_next_at")) or now
        stale = now - planned > max(STALE_NAG, timedelta(minutes=3 * interval))
        until = self._nag_until(row)
        # «сдаюсь» запланирован ровно на until, а каждый нажим отсчитывается от фактического тика — секунды
        # опоздания копятся: ему — допуск LATE_AFTER, иначе обещанное «сдамся» тихо пропало бы
        give_up = count > nag_max
        if until is not None and (planned > (until + LATE_AFTER if give_up else until) or now > until + LATE_AFTER):
            # «Поздравить…» — только до вечера: на ночь глядя не долбим и не «сдаёмся», просто тихо заканчиваем
            await self.ctx.db.execute(
                "UPDATE reminders SET nag_active=0, nag_next_at=NULL, "
                "status=CASE WHEN next_at IS NULL THEN 'done' ELSE status END "
                "WHERE id=? AND status='active' AND nag_active=1", (rid,))
            return True
        if count > nag_max or stale:
            offline = self._offline_at(planned)
            if wake:
                if stale and count <= nag_max:
                    msg = STALE_WAKE_GIVE_UP_NET if offline else STALE_WAKE_GIVE_UP
                else:
                    msg = WAKE_GIVE_UP
                msg = gendered(msg, female)
                if row.get("next_at"):
                    msg += f" Следующий будильник — {timeutil.fmt_local(row['next_at'], self.ctx.tz)}."
            else:
                stale_msg = STALE_GIVE_UP_NET if offline else STALE_GIVE_UP
                msg = (stale_msg if stale and count <= nag_max else GIVE_UP).format(text=text)
            if not await self._deliver(("nag", rid), msg, None, now):
                return False
            await self.ctx.db.execute(
                "UPDATE reminders SET nag_active=0, nag_next_at=NULL, nag_count=?, "
                "status=CASE WHEN next_at IS NULL THEN 'done' ELSE status END "
                "WHERE id=? AND status='active' AND nag_active=1",
                (min(count, nag_max), rid))
            return True
        line = nag_line(count, nag_max, wake=wake, female=female,
                        morning=is_morning(now.astimezone(self.ctx.tz)))
        msg = f"{line}\n⏰ {text}  (#{rid}, {count}/{nag_max})"
        if not await self._deliver(("nag", rid), msg, reminder_buttons(row, self._female()), now):
            return False
        await self.ctx.db.execute(
            "UPDATE reminders SET nag_count=?, nag_next_at=? WHERE id=? AND nag_active=1",
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
            task = self._job_tasks.get(name)
            if task is not None and not task.done():
                continue        # ещё идёт (модель пишет поздравление дольше тика) — не задваиваем
            # дни рождения догоняем весь свой день: поздравление, потерянное в 9 утра, нужно и в 15:00
            if not (name == "birthdays" and day == today) and now - at > CATCH_UP:
                await self.ctx.db.kv_set(key, day.isoformat())
                self._job_retry_at.pop(name, None)
                self._job_fails.pop(name, None)
                log.info("ежедневная задача %s за %s пропущена: не вышло за %s", name, day, CATCH_UP)
                continue
            retry = self._job_retry_at.get(name)
            if retry is not None and now < retry:
                continue
            log.info("ежедневная задача %s за %s", name, day)
            self._job_tasks[name] = self.ctx.services.spawn(self._run_daily(name, day), name=f"job:{name}")

    async def _run_daily(self, name: str, day: date) -> None:
        """Фон: задача; сделана — отметка в kv, нет — повтор через JOB_RETRY (отметки нет, так что и
        перезапуск посреди дела не теряет задачу)."""
        try:
            ok = await self._job(name, day)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ежедневная задача %s упала", name)
            ok = False
        if ok:
            await self.ctx.db.kv_set(job_key(name), day.isoformat())
            self._job_retry_at.pop(name, None)
            self._job_fails.pop(name, None)
            return
        n = self._job_fails[name] = self._job_fails.get(name, 0) + 1
        pause = JOB_RETRY[min(n, len(JOB_RETRY)) - 1]
        self._job_retry_at[name] = timeutil.now_utc() + pause
        log.warning("ежедневная задача %s за %s не доставлена — повтор через %s", name, day, pause)

    async def run_job(self, name: str) -> bool:
        """Выполнить ежедневную задачу сразу (без отметки в kv) — для команд бота и тестов."""
        return await self._job(name)

    async def _text_for(self, name: str, day: date, make: Any) -> str:
        """Текст сводки/дайджеста за day: готовый от прошлой (недоставленной) попытки или новый."""
        text = self._job_text.get((name, day))
        if text is None:
            text = str(await make() or "").strip()
            self._job_text = {k: v for k, v in self._job_text.items() if k[1] == day}
            self._job_sent = {k: v for k, v in self._job_sent.items() if k[1] == day}
            self._job_text[(name, day)] = text
            self._job_sent[(name, day)] = [0]
        return text

    def _job_done(self, name: str, day: date) -> None:
        self._job_text.pop((name, day), None)
        self._job_sent.pop((name, day), None)

    async def _job(self, name: str, day: date | None = None) -> bool:
        """Ежедневная задача. → True — сделана (или делать нечего, или собрать не вышло — об этом
        сказано владельцу); False — не доставлена, повторить позже."""
        ctx = self.ctx.child()
        day = day or ctx.now_local().date()
        try:
            if name == "morning":
                from . import brief
                text = await self._text_for(name, day, lambda: brief.morning_brief(ctx))
                if text:
                    if not await self._send_once(text, progress=self._job_sent.setdefault((name, day), [0])):
                        return False
                    await self._record(f"Утренняя сводка владельцу:\n{_clip(text, RECORD_MAX)}")
                self._job_done(name, day)
            elif name == "birthdays":
                from ..tools import birthdays
                failed: list[int] = []
                n = await birthdays.birthday_jobs(ctx, failures=failed)
                log.info("дни рождения: отправлено %s, не ушло %s", n, len(failed))
                return not failed
            elif name == "news":
                from ..tools import news
                text = await self._text_for(name, day, lambda: news.news_digest(ctx))
                if text:
                    # не дошёл второй кусок — повтор шлёт со второго, а не весь дайджест заново
                    if not await self._send_once("🗞 " + text, progress=self._job_sent.setdefault((name, day), [0])):
                        return False
                    # целиком (до RECORD_MAX): «подробнее про пятый сюжет» — агент должен видеть пятый
                    await self._record(f"Дайджест новостей владельцу:\n{_clip(text, RECORD_MAX)}")
                self._job_done(name, day)
            elif name == "reflection":
                agent = ctx.services.agent
                if agent is None:
                    log.info("рефлексия: агента нет — пропускаю")
                    return True
                await agent.reflect()
            else:
                log.warning("неизвестная ежедневная задача %s", name)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("ежедневная задача %s упала", name)
            if name == "morning":
                await self._send_once(f"не собрал сводку: {_err(e)}")
            elif name == "news":
                await self._send_once(f"не собрал сводку новостей: {_err(e)}")
        return True

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
