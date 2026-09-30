# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — ИИ-ПИЛОТ (спринт 4.5: «ИИ рулит, код исполняет»).

Прямой заказ владельца (11.08.2026, личный тест на свои деньги, 18+):
  · огромный анализ → шифровщик отдаёт ИСПОЛНЯЕМЫЙ приказ (exec-блок:
    BUY/SELL, уровень засады, тейк, invalidation). ФЛЕТ ЗАПРЕЩЁН — играем
    агрессивно, «лучше переждать, чем недождать», но в сделку заходим ТОЧНО;
  · код исполняет НА МАКСИМУМ: фьючерс — депозит/ГО (ГО и есть плечо; шорт
    сайзится по ГО продажи), акция — плечо по риск-ставкам Тинькофф
    dlong/dshort из каталога (нет ставок → честно на свои);
  · «мы не ставим лимиток на биржу» — уровень входа (засада) живёт ВНУТРИ
    Пифии; имба-момент (цена в ARM_TICKS тиках) → агрессивная лимитка в
    спред (рыночные входы запрещены — «Абсолют»); не льётся → перевзводы →
    маркет-эйбл лимит по best («точно заходим», но это лимитка, не маркет);
  · при победе (тейк) продаём ВСЁ — и огромный анализ С ЧИСТОГО ЛИСТА;
  · каждые 30 минут ПЕРЕПРОВЕРКА: свежие новости + прошлый вердикт +
    текущая ситуация → ЖДЁМ / ЗАКРЫТЬ / КУПИТЬ_СЕЙЧАС / ПРОДАТЬ_СЕЙЧАС /
    НОВЫЙ_АНАЛИЗ. Решение исполняется кодом немедленно.

ЗАКАЛКА ВЕЕРОМ АГЕНТОВ (11.08.2026, 18 агентов, 90 находок, 7 критических
подтверждено скептиками) — что закрыто по их находкам:
  · маржевой дозор согласован с «максимумом»: порог MAINT_FRAC·ГО и
    ЧАСТИЧНОЕ ужатие до вмещающегося объёма (эталон autopilot), а не
    закрытие всего при −0.04% депозита;
  · двойное закрытие убито: результат снятия троса проверяется; трос не
    снялся (уже исполнен на бирже) → сверка с реальной позицией и закрытие
    ТОЛЬКО фактического остатка — голый разворот невозможен;
  · результат закрывающего ордера проверяется: биржа отбила → позиция
    ОСТАЁТСЯ в учёте и закрытие повторяется, а не исчезает с бумаги;
  · ПАНИКА/killswitch/новый план при заявке в полёте забирают ЧАСТИЧКУ
    (налитые лоты становятся позицией и закрываются/ведутся, не теряются);
  · заявка входа несёт СВОИ тейк/стоп (снимок плана) — гонка «новый план
    поверх летящей заявки» не приклеивает чужие уровни;
  · вход в мёртвую идею запрещён: цена уже за invalidation → входа нет,
    сразу свежий разбор; REJECTED-штурм биржи остановлен бэкоффом;
  · рыночный гейт: пустой/протухший стакан (ночь, аукцион, планка) —
    ни входов, ни перепроверок по замороженной цене;
  · один депозит = ОДИН пилот (двойной счёт денег при пакете тикеров
    отрезан на сервере); конфликт со старой оракульской петлёй закрыт;
  · рестарт: своя позиция сохраняется в data/aipilot_state.json и
    подхватывается после перезапуска (иначе она навсегда «ручная»);
  · killswitch дневной: новый торговый день (МСК) вне позиции — счётчик
    заново, СТОП снимается; цены лимиток/стопов снапятся к шагу цены;
    отказ постановки троса виден владельцу, а не проглатывается.

ТОРМОЗА НЕ ОБСУЖДАЮТСЯ: killswitch SessionRisk, маржевой дозор, биржевой
трос по invalidation, ПАНИКА, изоляция ручных лотов. TIME_KILLSWITCH 600с
здесь осознанно НЕ действует (горизонт — часы; его роль играют 30-мин цикл
и трос).

ЧЕСТНЫЕ ОГРАНИЧЕНИЯ (не скрыто, а сказано): комиссии и проскальзывание в
P/L не моделируются (тариф счёта неизвестен — нулём не врём, суммы это
занижает); P/L рыночного закрытия оценивается по последней цене тика, не по
факту исполнения; экспирация фьючерса в позиции не автоматизирована —
трос и перепроверка остаются единственной защитой, перенос контракта
делает новый разбор.

⚫ Реальные деньги на автомате по решению ИИ — осознанный выбор владельца
(«если сольёт — окей»). Измеренная точность ИИ-голоса на истории 44–49%.
Не сигнал, не обещание прибыли; рынок не предсказуем. Плечо 18+.

Self-тест (без сети): python3 -m backend.ai_pilot
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import uuid

try:
    from . import tinkoff, trader_broker, trader_risk
except ImportError:                                        # запуск как скрипт
    import tinkoff, trader_broker, trader_risk             # noqa: E401
try:                                                       # рыночные часы: биржа закрыта → стопор
    from . import market_clock
except ImportError:
    try:
        import market_clock                                # noqa: E401
    except ImportError:
        market_clock = None

log = logging.getLogger("pythia.ai_pilot")

TICK_SEC = 1.5                 # шаг петли
REVIEW_SEC = float(os.getenv("PYTHIA_AIP_REVIEW_SEC", "1800"))   # 30 мин
ARM_TICKS = float(os.getenv("PYTHIA_AIP_ARM_TICKS", "5"))  # имба-момент: цена
                               # в N шагах цены от засады → бьём (владелец:
                               # «ещё 4-5 пунктов — ставим покупку/продажу»)
ARM_FRAC = 0.0012              # фолбэк, если шаг цены неизвестен: 0.12% цены
RESERVE_FRAC = float(os.getenv("PYTHIA_AIP_RESERVE", "0.02"))
                               # 2% депозита не закладываем в ГО (физика ГО)
MAINT_FRAC = float(os.getenv("PYTHIA_AIP_MAINT", "0.90"))
                               # дозор: капитал < 90% начального ГО → ужатие.
                               # Согласовано с резервом: при 0.98·ГО на входе
                               # буфер ≈ 12% депозита (≈1.2% цены при плече 10),
                               # т.е. стоп ИИ (invalidation) срабатывает РАНЬШЕ
                               # дозора — дозор страховка, не главный стоп
ENTRY_TTL_SEC = 45.0           # лимитка входа живёт столько — дальше перевзвод
RUN_FRAC = 0.0015              # цена убежала от заявки на 0.15% → перевзвод
MAX_REPRICINGS = 3             # перевзводы агрессивной тени; дальше — маркет-
                               # эйбл лимит по best противоположной стороны
ENTRY_FAIL_MAX = 5             # подряд отказов биржи → план сброшен, реанализ
EMERGENCY_STOP_FRAC = 0.006    # перепроверка без invalidation → аварийный стоп
                               # 0.6% (объявленная политика режима)
RECONCILE_EVERY = 40           # тиков между сверками с биржей (~60с)
ADOPT_COOL_SEC = 180.0         # после собственного закрытия «позиция на счёте» принимается только
                               # при двух сверках подряд (лаг портфеля брокера — не фантом)
BOOK_FRESH_SEC = 180.0         # стакан старше — рынок «мёртв»: ни входов,
                               # ни перепроверок по замороженной картинке
# DRIFT_FRAC (решение протухло: цена ушла от снимка) — из конфига, см. ниже после _setting (v5.4.2)
PLAN_TTL_SEC = float(os.getenv("PYTHIA_AIP_PLAN_TTL", "5400"))
                               # СРОК ЖИЗНИ ПРИКАЗА (1.5 ч): пилот работает
                               # весь день, и засада, выставленная утром, к
                               # вечеру говорит о другом рынке. Протух — не
                               # входим по старой цене, просим свежий разбор
REANALYZE_GAP_SEC = float(os.getenv("PYTHIA_AIP_REANALYZE_GAP", "600"))
                               # не чаще одного огромного анализа в 10 мин:
                               # он стоит пять стадий ИИ, а день длинный
# ── v5.2 «ВЕСЬ СЧЁТ» ──────────────────────────────────────────────────────
MX_TTL_SEC = 20.0              # кэш «сколько даёт биржа» (GetMaxLots), с
TOPUP_GAP_SEC = 8.0            # добор после исполнения — не раньше чем через N с
GUARD_TIMEOUT = 600.0          # мягкий стоп: ИИ у денег (PRO, v5.4.1) обязан ответить за N с, иначе стоп по правилу
TAKE_TIMEOUT = 600.0           # мягкий тейк (v5.3 W2): ответ за N с, иначе фиксация по правилу
# ENTRY_TIMEOUT / PROFIT_TIMEOUT (проверка входа у двери / мысль о прибыли) — из конфига, см. ниже после _setting
GATES_KEEP = 20                # сколько проверок входа помним (в status — последние 10)
PROFITS_KEEP = 20              # сколько мыслей о прибыли помним (в status — последние 10)
GATE_WAIT_MIN_SEC = 60.0       # ЖДАТЬ у двери со сроком: не меньше минуты …
GATE_WAIT_MAX_SEC = 4 * 3600.0 # … и не дольше 4 ч (дальше — план протухнет сам, PLAN_TTL_SEC)
PILOT_EXTRA_TTL_SEC = 24 * 3600.0   # записи gates/profits в секции pilot state-файла живут сутки
STOP_RETRY_SEC = 30.0          # биржа отбила стоп-заявку → повтор не чаще N с (виртуальный стоп держит)
STOP_REQUEST_MAX_AGE_SEC = 300.0   # запрос стопа старше — тот же UUID не повторяем (срок хранения ключа биржей
                               # не обещан): судьбу решает СПИСОК СТОПОВ биржи (GetStopOrders), не вечное ожидание
STOP_LISTING_SKEW_SEC = 5.0    # допуск часов при сверке createDate стопа с моментом запроса
ORDER_REQUEST_MAX_AGE_SEC = trader_broker.ORDER_REQUEST_MAX_AGE_SEC   # (90 с) заявка по request-UUID «не найдена»
                               # биржей дольше — до биржи не дошла (фантом): терминал с 0 исполнений, не вечное ожидание
PANIC_PENDING_DROP_ATTEMPTS = 3   # ПАНИКА force: неразрешимая заявка входа снимается из учёта после N попыток (UUID в лог)
PANIC_STOP_SWEEP_TICKS = 3     # ПАНИКА: отмена троса не подтверждается N тиков → снять ВСЕ стопы по инструменту
                               # через список биржи и закрывать (виртуальный стоп/паника обязаны исполняться)
ADOPT_STOP_PCT = 2.0           # принятая со счёта позиция без уровней: временный триггер N % от входа
GUARDS_KEEP = 20               # сколько решений у троса помним
# ревью 5.4.2 (финал): флаг «решения ИИ не было — страховка кода» в ответе троса/тейка — приватные ключи (ИИ свои поля
# с такими именами не протаскивает: наследник вычищает их из ответа, strip_rule_keys); «rule» ИИ — его текст, не флаг
RULE_KEY, RULE_DETAIL, RULE_RAW = "_rule", "_detail", "_raw"
# ── РЫНОЧНЫЕ ЧАСЫ (воля владельца: «биржа закрыта — программа стопорится») ──
OPEN_REVIEW_GRACE_SEC = 90.0   # перепроверка не раньше чем через N с после открытия (стакан наполнится)
CLOSED_LONG_SEC = 1800.0       # закрыт дольше N с (ночь, выходной) → на открытии сразу перепроверка
                               # «накопились новости/события»; короткий клиринг — просто продолжаем
CLOSED_RECONCILE_EVERY = 4     # тиков закрытого рынка между сверками со счётом (шаг PYTHIA_CLOSED_TICK_SEC)


def _setting(name: str, default):
    """Ключ конфига v5 (config_user.json / окружение) с дефолтом; без модуля config — дефолт."""
    try:
        try:
            from . import config as _c
        except ImportError:
            import config as _c
        v = getattr(_c, name, default)
        return default if v is None else v
    except Exception:                                        # noqa: BLE001
        return default


# ── v5.4.2 «СВОБОДНЫЙ ПИЛОТ»: сроки PRO у денег и допуск ухода цены — из конфига, живьём ──────────────────────────
# 600 с из 5.4.1 резали треть ответов PRO (он думает ~10 мин), и код писал за ИИ «ЖДАТЬ»/«ДЕРЖАТЬ». Имена констант
# оставлены для совместимости — это снимок конфига при импорте; живое значение (панель меняет конфиг без рестарта) —
# entry_timeout() / profit_timeout() / drift_frac(). Константу, подменённую стендом или тестом (monkeypatch), функции
# уважают: подмена главнее конфига. Трос и тейк (GUARD_TIMEOUT / TAKE_TIMEOUT) — как в 5.4.1, 600 с.
ENTRY_TIMEOUT = float(_setting("PYTHIA_ENTRY_TIMEOUT_SEC", 1200))    # проверка входа у двери: срок ответа PRO, с
PROFIT_TIMEOUT = float(_setting("PYTHIA_PROFIT_TIMEOUT_SEC", 1200))  # мысль о прибыли: срок ответа PRO, с
DRIFT_FRAC = float(_setting("PYTHIA_ENTRY_DRIFT_PCT", 1.0)) / 100.0  # решение протухло: цена ушла от снимка дальше
_LIVE_SNAPSHOT = {"ENTRY_TIMEOUT": ENTRY_TIMEOUT, "PROFIT_TIMEOUT": PROFIT_TIMEOUT, "DRIFT_FRAC": DRIFT_FRAC}


def _live(name: str, key: str, default: float, scale: float = 1.0) -> float:
    """Живое значение настройки key (× scale); константа name, подменённая после импорта, — главнее конфига."""
    cur = globals().get(name)
    if cur != _LIVE_SNAPSHOT.get(name):                      # стенд/тест подменил константу модуля
        return float(cur)
    try:
        return float(_setting(key, default)) * scale
    except (TypeError, ValueError):
        return float(cur)


def entry_timeout() -> float:
    """Сколько ждать ответа PRO у двери, с (PYTHIA_ENTRY_TIMEOUT_SEC, живьём)."""
    return _live("ENTRY_TIMEOUT", "PYTHIA_ENTRY_TIMEOUT_SEC", 1200.0)


def profit_timeout() -> float:
    """Сколько ждать ответа PRO в мысли о прибыли, с (PYTHIA_PROFIT_TIMEOUT_SEC, живьём)."""
    return _live("PROFIT_TIMEOUT", "PYTHIA_PROFIT_TIMEOUT_SEC", 1200.0)


def drift_frac() -> float:
    """Допуск ухода цены хуже снимка решения, доля (PYTHIA_ENTRY_DRIFT_PCT / 100, живьём)."""
    return _live("DRIFT_FRAC", "PYTHIA_ENTRY_DRIFT_PCT", 1.0, 0.01)


def wait_review_sec() -> float:
    """Приказ совета WAIT без позиции и плана: через сколько секунд дежурный PRO смотрит заново
    (PYTHIA_WAIT_REVIEW_SEC, живьём; не дольше плановой REVIEW_SEC)."""
    try:
        sec = float(_setting("PYTHIA_WAIT_REVIEW_SEC", 1800))
    except (TypeError, ValueError):
        sec = 1800.0
    return max(1.0, min(sec, REVIEW_SEC))

FRAME = ("⚫ ИИ-пилот: реальные деньги на максимум по решению ИИ — личный тест "
         "владельца. Тормоза: killswitch, маржевой дозор, биржевой трос, "
         "ПАНИКА, изоляция ручных лотов. Не сигнал; рынок не предсказуем. 18+")

BUY, SELL = trader_broker.BUY, trader_broker.SELL


def strip_rule_keys(obj: dict) -> dict:
    """Ответ ИИ у троса/тейка без приватных ключей флага кода (RULE_KEY/RULE_DETAIL/RULE_RAW): флаг ставит только код."""
    return {k: v for k, v in obj.items() if k not in (RULE_KEY, RULE_DETAIL, RULE_RAW)}


def _f(x, d=0.0):
    try:
        if isinstance(x, str):
            x = x.replace(",", ".").strip()
        v = float(x)
        return v if math.isfinite(v) else d
    except (TypeError, ValueError):
        return d


def _q(v) -> float:
    """Quotation Тинькофф {units, nano} → float; число → как есть."""
    if isinstance(v, dict):
        return _f(v.get("units")) + _f(v.get("nano")) / 1e9
    return _f(v)


class AIPilot:
    """Один инструмент — одна петля. ИИ решает, код исполняет на максимум.

    v5.2 «ВЕСЬ СЧЁТ»: всё, что лежит на счёте по figi, — позиция бота (докупил владелец →
    позиция выросла, продал → уменьшилась); размер входа считает БИРЖА (GetMaxLots с
    маржой), после входа пилот добирает, пока биржа даёт; приказ CLOSE закрывает и ждёт;
    мягкий стоп (наследник с SOFT_STOP=True): у триггера спрашивают FLASH — слить или
    ждать и передать Совету, — а на бирже лежит аварийный трос дальше триггера.
    v5.3 W2 «МЯГКИЙ ТЕЙК»: у тейка тоже не закрываем вслепую — FLASH за секунды решает, зафиксировать
    или подержать (триггер подтягивается к цене, прибыль заперта) и передать задачу Совету."""

    SOFT_STOP = False              # базовый пилот: стоп есть стоп; MissionPilot спрашивает FLASH

    def __init__(self, base: str, deposit: float | None = None,
                 broker: trader_broker.Broker | None = None):
        self.base = (base or "").upper()
        self.name = self.base
        self.deposit_override = deposit
        self.broker = broker or trader_broker.Broker("real")
        self.asset_class = "share"
        self.figi = None
        self.tick_size = 0.0
        self.point_value = 1.0
        self.go_per_lot: float | None = None   # фьючерс: ГО покупки
        self.go_sell: float | None = None      # фьючерс: ГО продажи (шорт)
        self.dlong = 0.0                       # акция: риск-ставка лонга
        self.dshort = 0.0                      # акция: риск-ставка шорта
        self.deposit = 0.0
        self._live_cash: float | None = None   # свежие деньги счёта (сверка)
        self.session_risk: trader_risk.SessionRisk | None = None
        self._sr_day: int | None = None        # МСК-день killswitch (сброс)
        # план шифровщика: {side, entry|None, take|None, invalidation, why}
        self.plan: dict | None = None
        self.position: dict | None = None      # {side, entry, lots, take,
                                               #  invalidation, opened_ts,
                                               #  stop_id, close_fail?}
        self.pending: dict | None = None       # заявка входа в полёте
                                               # (несёт СВОИ take/invalidation)
        self._pending_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._cancel_entry = False             # снять заявку ближайшим тиком
        self.foreign_lots = 0                  # ручные лоты владельца (только при выключенном
                                               # PYTHIA_ADOPT_ACCOUNT — иначе весь счёт наш)
        self.adopt_account = bool(_setting("PYTHIA_ADOPT_ACCOUNT", True))
        self.account: dict = {}                # свободно/ликвидно/сколько даёт биржа — для панели
        self._mx: dict | None = None           # кэш GetMaxLots {"buy","sell","ts","price"}
        self._sized_by_broker = False          # размер последнего входа считала биржа
        self._close_pending: str | None = None # приказ CLOSE: закрыть ближайшим тиком
        self._prepared = False                 # prepare() отработал (позиция со счёта принята)
        self._pending_hold: dict | None = None # ревью 5.4.2: HOLD до prepare() — {take, inv, ts}: уровни совета к позиции
                                               # со счёта (_absorb_account); снимается в конце prepare() и любым приказом
        self._closed_ts = 0.0                  # когда сами закрыли/ужали позицию (лаг портфеля)
        self._adopt_seen: tuple | None = None  # что видели на прошлой сверке после закрытия
        self._guard_task = None                # мягкий стоп: ИИ у троса/тейка думает
        self._gate_task = None                 # v5.4.1: PRO проверяет вход у двери
        self._profit_task = None               # v5.4.1: PRO думает о прибыли
        self._background_tasks: set[asyncio.Task] = set()
        self.guards: list[dict] = []           # решения у троса (последние GUARDS_KEEP)
        self.gates: list[dict] = []            # v5.4.1: проверки входа у двери (последние GATES_KEEP, персистятся)
        self.profits: list[dict] = []          # v5.4.1: мысли о прибыли (последние PROFITS_KEEP, персистятся)
        self.market: dict | None = None        # рыночные часы: последний статус биржи (None — часы выключены)
        self._market_open_prev: bool | None = None   # был ли рынок открыт на прошлом тике
        self._closed_since = 0.0               # с какого момента рынок закрыт (для повода на открытии)
        self._opened_ts = 0.0                  # v5.4.2: когда рынок открылся (запас стакану не отменяет ритм WAIT)
        self._review_saved: float | None = None  # плановая перепроверка до закрытия (короткий клиринг → вернуть)
        self.pnls: list[float] = []
        self.prices: list[float] = []
        self.last_book: dict | None = None
        self._book_ts = 0.0
        self.state = "ЖДУ_ПЛАН"
        self.last_action = "—"
        self.review_ts = time.time() + REVIEW_SEC
        self._wait_order = False               # v5.4.2: последний приказ совета — WAIT (шаг перепроверки вне рынка короче)
        self.last_review: dict | None = None
        self._review_busy = False
        self._reanalyzing = False
        self.reanalyze_cb = None               # async () -> None (сервер)
        self._ai_ask_json = None               # инъекция ИИ для self-теста
        self.no_entry_until = 0.0              # бэкофф после отказов биржи
        self._entry_fail = 0
        self.analyses = 0                      # огромных разборов за смену
        self._last_reanalyze_ts = 0.0          # пейсинг дорогих разборов
        self._reanalyze_pending = None         # отложенная просьба о разборе
        self.stopping = False
        self.panic_flag = False
        self.panic_force = False               # ПАНИКА «без проверки»: закрывать, даже если стопы биржи не сверить
        self.panic_blocked = False             # ПАНИКА упёрлась (стопы не сверить) — повторное нажатие = force
        self._state_note: str | None = None    # честная пометка о state-файле (отложен: другой счёт/режим)
        self._tick_n = 0
        self._stray_warned = False
        self.started_ts = time.time()
        self._last_tick_ts = 0.0               # v5.3 фаза 3: когда петля последний раз тикала (панель: state_age_s)
        self._state_path = None                # файл выживания позиции
        try:
            try:
                from . import config as _cfg
            except ImportError:
                import config as _cfg
            self._state_path = _cfg.DATA_DIR / "aipilot_state.json"
        except Exception:                                    # noqa: BLE001
            pass

    # ── выживание позиции при рестарте ──────────────────────────────────────
    def _state_extra(self) -> dict:
        """v5.3 фаза 3: секция «pilot» state-файла — что пилот обязан помнить через рестарт помимо позиции
        (наследник: дедуп и пейсинг прокола сканера). v5.4.1: последние проверки входа (gates) и мысли о
        прибыли (profits) — не старше PILOT_EXTRA_TTL_SEC. Пусто → секции нет; нет и позиции → файла нет."""
        out: dict = {}
        now = time.time()
        for key, items in (("gates", self.gates), ("profits", self.profits)):
            keep = [r for r in items[-10:] if isinstance(r, dict) and now - _f(r.get("ts")) <= PILOT_EXTRA_TTL_SEC]
            if keep:
                out[key] = keep
        return out

    def _restore_extra(self, extra: dict) -> None:
        """Подхват секции «pilot» при рестарте (тот же figi); наследник дополняет своим."""
        now = time.time()
        for key, cap in (("gates", GATES_KEEP), ("profits", PROFITS_KEEP)):
            rows = (extra or {}).get(key)
            if not isinstance(rows, list):
                continue
            keep = [dict(r) for r in rows if isinstance(r, dict) and -60.0 <= now - _f(r.get("ts")) <= PILOT_EXTRA_TTL_SEC]
            if keep:
                setattr(self, key, (getattr(self, key) + keep)[-cap:])

    def _save_state(self) -> bool:
        if not self._state_path:
            return True
        try:
            try:
                extra = self._state_extra() or {}
            except Exception as e:                           # noqa: BLE001
                log.info("state-файл: секция pilot не собралась: %s", str(e)[:80])
                extra = {}
            if self.position or self.pending or extra:
                rec = {"figi": self.figi, "base": self.base,
                       "ts": time.time(),
                       "account_id": getattr(self.broker, "account_id", None), "mode": self.broker.mode,
                       "pending": self.pending,
                       "foreign_lots": self.foreign_lots,
                       "close_pending": self._close_pending,
                       "position": ({k: self.position.get(k) for k in
                                     ("side", "entry", "lots", "take",
                                      "invalidation", "opened_ts", "stop_id",
                                      # v5.2: триггер ИИ, аварийный трос, счётчик «ждать», временные уровни, добор
                                      "inv0", "hard_stop", "holds", "guard_last", "guard_next",
                                      "levels_placeholder", "topup_left",
                                      # v5.3 W2: мягкий тейк — сколько раз «подержали», срок следующего вопроса
                                      "take_holds", "take_next", "exit_order", "close_fail",
                                      "close_reanalyze", "exit_pnl_total", "risk_fed", "stop_request",
                                      "restop", "restop_after",
                                      # W4: сверка остатка со счётом перед повторной заявкой / новым тросом
                                      "exit_check", "exit_check_strict", "verify_lots",
                                      # v5.4.1: мысль о прибыли — пейсинг, запертая прибыль (трос не ниже входа)
                                      "profit_next", "profit_last", "profit_lock",
                                      # ревью 5.4.3: триггер до запирания прибыли (повтор старого стопа её не отдаёт)
                                      "lock_from")} if self.position else None)}
                if self.position or self.pending:
                    # ПАНИКА — свойство ПОЗИЦИИ/ЗАЯВКИ, которые она закрывает, а не инструмента: без них флаг в файл
                    # не пишется (иначе секция pilot держит файл часами, и следующая миссия по тикеру рождалась бы в СТОП)
                    rec["panic"] = self.panic_flag
                    rec["panic_force"] = self.panic_force
                if extra:
                    rec["pilot"] = extra                     # v5.3 фаза 3: дедуп/пейсинг прокола и т. п.
                tmp = self._state_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
                tmp.replace(self._state_path)
            elif self._state_path.exists():
                self._state_path.unlink()
            return True
        except Exception as e:                               # noqa: BLE001
            log.warning("state-файл не записался: %s", str(e)[:80])
            return False

    def _restore_state(self, portfolio_qty: int) -> None:
        """Рестарт сервера с открытой позицией: подхватить СВОЮ позицию из
        state-файла (иначе она навсегда попала бы в «ручные» и осталась без
        ведения). Подхват только при совпадении figi и знака на счёте."""
        if not (self._state_path and self._state_path.exists()):
            return
        try:
            try:
                state_text = self._state_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                state_text = self._state_path.read_text(encoding="cp1251")
            rec = json.loads(state_text)
        except Exception:                                    # noqa: BLE001
            return
        if (rec or {}).get("figi") == self.figi and isinstance(rec.get("pilot"), dict):
            try:                                             # v5.3 фаза 3: секция pilot — раньше позиции (файл может быть стёрт ниже)
                self._restore_extra(rec["pilot"])
            except Exception as e:                           # noqa: BLE001
                log.info("state-файл: секция pilot не подхватилась: %s", str(e)[:80])
        p = (rec or {}).get("position") or {}
        if rec.get("figi") != self.figi:
            return
        if rec.get("pending") or p.get("exit_order") or p.get("stop_request"):
            account = getattr(self.broker, "account_id", None)
            if ((rec.get("account_id") and account and rec["account_id"] != account) or
                    (rec.get("mode") and rec["mode"] != self.broker.mode)):
                # Сохранённая заявка другого счёта/режима: проверить её этим брокером нельзя. Раньше здесь был
                # RuntimeError → пилот падал → сторож поднимал → падал снова (цикл). Теперь: файл ОТЛОЖЕН
                # (.mismatch-<время>), честная пометка владельцу, пилот стартует чистым
                self._defer_state_file(rec, account)
                return
        saved_pending = rec.get("pending")
        pending_ok = bool(isinstance(saved_pending, dict) and saved_pending.get("order_id") and saved_pending.get("lots"))
        if pending_ok:
            self.pending = dict(saved_pending)
            self._cancel_entry = True  # first settle orders from the previous process
        # ПАНИКА/CLOSE наследуются только вместе с тем, что они закрывают (№1): файл без позиции и заявок (осталась
        # секция pilot — память проколов) не имеет права рождать новый пилот в СТОП «ПАНИКА владельца»
        owned = bool(pending_ok or p.get("lots") or p.get("exit_order") or p.get("stop_request"))
        if owned:
            self.panic_flag = self.panic_flag or bool(rec.get("panic"))
            self.panic_force = self.panic_force or bool(rec.get("panic_force"))
            self._close_pending = self._close_pending or rec.get("close_pending")
        if self.pending:
            self.foreign_lots = int(rec.get("foreign_lots") or 0)
        if not p.get("lots"):
            return
        owned_order = bool(self.pending or p.get("exit_order") or p.get("stop_request"))
        want = int(p["lots"]) * (1 if p.get("side") == "long" else -1)
        if portfolio_qty * want <= 0 and not owned_order:
            try:
                self._state_path.unlink()
            except Exception:                                # noqa: BLE001
                pass
            return
        # While our order is unresolved the portfolio may already contain a
        # partial fill or still lag behind it. Its cumulative status owns the
        # delta; adopting the snapshot here would account the fill twice.
        own = int(p["lots"]) if owned_order else min(abs(portfolio_qty), int(p["lots"]))
        self.position = {"side": p["side"], "entry": _f(p.get("entry")),
                         "lots": own, "take": p.get("take"),
                         "invalidation": p.get("invalidation"),
                         "opened_ts": _f(p.get("opened_ts")) or time.time(),
                         "stop_id": p.get("stop_id"), "floating": 0.0,
                         "inv0": p.get("inv0") or p.get("invalidation"),
                         "holds": int(p.get("holds") or 0),
                         "topup_left": int(p.get("topup_left") or 0)}
        for key in ("exit_order", "close_fail", "close_reanalyze", "exit_pnl_total", "risk_fed", "stop_request", "restop",
                    "restop_after", "exit_check", "exit_check_strict", "verify_lots"):
            if p.get(key) is not None:
                self.position[key] = p[key]
        if p.get("levels_placeholder"):
            self.position["levels_placeholder"] = True
        if _f(p.get("guard_last")) > 0:
            self.position["guard_last"] = _f(p.get("guard_last"))
        if _f(p.get("guard_next")) > time.time():
            self.position["guard_next"] = _f(p.get("guard_next"))   # срок «ждать», названный FLASH
        if int(p.get("take_holds") or 0) > 0:
            self.position["take_holds"] = int(p.get("take_holds") or 0)   # v5.3 W2: «подержали» у тейка
        if _f(p.get("take_next")) > time.time():
            self.position["take_next"] = _f(p.get("take_next"))
        if _f(p.get("profit_next")) > time.time():      # v5.4.1: пейсинг мысли о прибыли переживает рестарт
            self.position["profit_next"] = _f(p.get("profit_next"))
        if _f(p.get("profit_last")) > 0:
            self.position["profit_last"] = _f(p.get("profit_last"))
        if _f(p.get("lock_from")) > 0:                  # ревью 5.4.3: триггер до запирания прибыли
            self.position["lock_from"] = _f(p.get("lock_from"))
        if p.get("profit_lock"):                         # прибыль заперта PRO: трос не ниже входа
            self.position["profit_lock"] = True
        # v5.2: аварийный трос считается заново от триггера (старый state-файл его не знал);
        # трос на бирже стоял бы на самом триггере — перевыставить ближайшим тиком
        had_hard = _f(p.get("hard_stop")) > 0
        self._set_levels(self.position, None, None)
        if self._soft_stop_on() and not had_hard and self.position.get("hard_stop"):
            self.position["restop"] = True
        if not self.position.get("stop_id") and not self.position.get("exit_order"):
            self.position["restop"] = True  # a known rejected stop must still be retried after restart
        if self.position.get("stop_id") and not self._exchange_stop_on():
            self.position["restop"] = True  # v5.4.1: стопы только в программе — старый стоп биржи снять один раз
        self.foreign_lots = (int(rec.get("foreign_lots") or 0) if owned_order else
                             portfolio_qty - (own if p["side"] == "long" else -own))
        self.state = "В_ПОЗИЦИИ"
        self.last_action = (f"РЕСТАРТ: подхватил свою позицию {p['side']} "
                            f"{own} лот @{p.get('entry')} (трос {p.get('stop_id') or 'в программе'})")
        log.warning("ИИ-пилот %s: %s", self.base, self.last_action)

    def _defer_state_file(self, rec: dict, account) -> None:
        """State-файл относится к другому счёту/режиму: переименовать в .mismatch-<время>, сказать владельцу,
        стартовать чистым. Не исключение: цикл «упал → сторож поднял → упал» хуже честной пометки."""
        p = (rec or {}).get("position") or {}
        what = ("заявка входа" if rec.get("pending") else "заявка выхода" if p.get("exit_order") else "запрос стопа")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        where = ""
        try:
            target = self._state_path.with_name(self._state_path.name + f".mismatch-{stamp}")
            self._state_path.replace(target)
            where = f"отложен в {target.name}"
        except Exception as e:                               # noqa: BLE001
            where = f"отложить не удалось ({str(e)[:60]}) — файл оставлен, но не подхвачен"
        self._state_note = (f"⚠ state-файл: {what} относится к другому счёту/режиму (в файле счёт {rec.get('account_id')}, "
                            f"режим {rec.get('mode')}; сейчас счёт {account}, режим {self.broker.mode}) — {where}; "
                            "стартую чистым, сохранённую заявку проверь у брокера вручную")
        self.last_action = self._state_note
        log.error("ИИ-пилот %s: %s", self.base, self._state_note)

    # ── подготовка: контракт, депозит, изоляция, восстановление ─────────────
    async def prepare(self) -> bool:
        accs = await tinkoff.accounts() or []
        acc = accs[0]["id"] if accs else None
        pf = await tinkoff.portfolio(acc) if acc else None
        if acc:
            self.broker.account_id = acc
        mg = None
        try:                                   # маржинальные атрибуты: ликвидный портфель, достаточность
            fn = getattr(tinkoff, "margin_attributes", None)
            mg = await fn(acc) if (fn and acc) else None
        except Exception:                                    # noqa: BLE001
            mg = None
        dep = self.deposit_override
        if dep is None:
            # свободные деньги, не стоимость всего портфеля: максимум считается
            # от денег, которыми реально можно обеспечить ГО (находка веера).
            # v5.2: счёт целиком в бумагах (free_rub = 0) — берём ликвидный портфель
            # (маржа) или стоимость портфеля: полное управление счётом не имеет права
            # отказаться стартовать, размер всё равно считает биржа
            dep = (pf or {}).get("free_rub")
            if not dep or _f(dep) <= 0:
                dep = (mg or {}).get("liquid") or (pf or {}).get("total_rub")
                if dep:
                    log.warning("free_rub пуст — беру ликвидный портфель/стоимость %.0f "
                                "(счёт в бумагах, размер считает биржа)", _f(dep))
        self.deposit = _f(dep)
        self.account = {"free": _f((pf or {}).get("free_rub")) if pf else None,
                        "total": _f((pf or {}).get("total_rub")) if pf else None,
                        "liquid": (mg or {}).get("liquid"), "sufficiency": (mg or {}).get("sufficiency"),
                        "missing": (mg or {}).get("missing"), "ts": time.time()}
        if self.deposit <= 0 and getattr(self.broker, "mode", "dry") != "dry":
            log.error("ИИ-пилот %s: депозит не определён (%.2f) — не стартую "
                      "(сайзинг «на максимум» без денег невозможен)",
                      self.base, self.deposit)
            return False
        try:
            try:
                from . import instruments as _ins
            except ImportError:
                import instruments as _ins
            it = _ins.get(self.base) or {}
            self.asset_class = it.get("asset_class") or self.asset_class
            self.name = it.get("name") or self.base
        except Exception:                                    # noqa: BLE001
            pass
        inst = await tinkoff.resolve(self.base, self.asset_class)
        if not inst:
            log.error("ИИ-пилот: инструмент %s (%s) не найден",
                      self.base, self.asset_class)
            return False
        self.figi = inst.get("figi") or inst.get("uid")
        if self.asset_class == "futures":
            mg_f = await tinkoff.futures_margin(self.figi) or {}
            self.go_per_lot = mg_f.get("margin_buy")
            self.go_sell = mg_f.get("margin_sell")
            inc = mg_f.get("min_price_increment")
            amt = mg_f.get("min_price_increment_amount")
            self.point_value = (amt / inc) if inc else 1.0
            self.tick_size = float(inc or 0.0)
            if not self.go_per_lot or self.go_per_lot <= 0:
                log.error("ИИ-пилот %s: ГО фьючерса не получено — сайзинг "
                          "«на максимум» невозможен, не стартую", self.base)
                return False
        else:
            lot = int(inst.get("lot") or 1)
            self.point_value = float(lot)
            self.go_per_lot = None                 # акция: per считается живьём
            # шаг цены акции — из каталога (иначе лимитки некратны шагу)
            self.tick_size = _q(inst.get("minPriceIncrement"))
            # ПЛЕЧО ПО ТИНЬКОФФ (заказ владельца «макс по плечу, если можно»):
            # риск-ставки dlong/dshort из каталога — обеспечение доли позиции.
            # Нет ставок (не маржинальная бумага) → честно на свои (ставка 1).
            self.dlong = _q(inst.get("dlong"))
            self.dshort = _q(inst.get("dshort"))
        # что уже лежит на счёте по этому figi
        qty0, avg0 = 0, 0.0
        for p_ in (pf or {}).get("positions", []):
            if (p_.get("figi") == self.figi or (p_.get("uid") and p_.get("uid") == self.figi)) \
                    and abs(p_.get("qty") or 0) >= 1:
                q = p_.get("qty") or 0
                units = abs(q)
                lots = (int(units) if self.asset_class == "futures"
                        else max(1, int(units / max(1.0, self.point_value))))
                qty0 = lots if q > 0 else -lots
                avg0 = _f(p_.get("avg"))
                break
        # …своя позиция из прошлой жизни (state-файл, рестарт) подхватывается первой
        self._restore_state(qty0)
        if self.adopt_account and not self.pending and not any((self.position or {}).get(key) for key in ("exit_order", "stop_request")):
            # ВЕСЬ СЧЁТ ПО ИНСТРУМЕНТУ (v5.2): что лежит на счёте — позиция бота, владелец
            # докупил или открыл руками — веду и это; ИИ не думает «сколько куплено»
            self._absorb_account(qty0, avg0, None, "на счёте при старте")
        elif not self.position and not self.pending and qty0:
            # старая изоляция: всё чужое по figi — ручное, не наше
            self.foreign_lots = qty0
            log.info("ИИ-пилот: ИЗОЛЯЦИЯ ручных лотов владельца %+d (%s)",
                     self.foreign_lots, self.base)
        hold = self._pending_hold
        self._pending_hold = None              # ревью 5.4.2: HOLD до подготовки живёт только до её конца
        if hold and self.position:
            # позиция не принята со счёта, а подхвачена из state-файла (рестарт): HOLD к ней — как обычно
            self._adopt_hold({"invalidation": hold.get("inv"), "take": hold.get("take")})
        elif hold:
            # держать нечего (счёт пуст): дежурный PRO решит скоро, а не через плановые полчаса
            self.review_ts = min(self.review_ts, time.time() + 300)
            self.last_action = "приказ HOLD, а позиции на счёте нет — держать нечего; дежурный PRO решит через 5 мин"
            log.warning("ИИ-пилот %s: %s", self.base, self.last_action)
        if self._state_note and self.last_action != self._state_note:
            self.last_action = f"{self.last_action} · {self._state_note}"
        self.session_risk = trader_risk.SessionRisk(max(1.0, self.deposit))
        self._sr_day = self._msk_day()
        self._prepared = True
        if self._close_pending and not self.position and not self.pending:
            self._close_pending = None         # CLOSE до подготовки, а закрывать нечего
        log.info("ИИ-пилот готов: %s figi=%s ГО=%s/%s dlong=%.2f депозит=%.0f "
                 "режим=%s весь_счёт=%s", self.base, self.figi, self.go_per_lot,
                 self.go_sell, self.dlong, self.deposit, self.broker.mode, self.adopt_account)
        return True

    # ── весь счёт по инструменту: позиция бота = позиция на счёте (v5.2) ────
    def _realize(self, pos: dict, lots: int, px: float, why: str, *, risk: bool = True) -> float:
        """Зафиксировать P/L части позиции (владелец продал/перевернул руками) —
        в killswitch и журнал; честно: по цене тика, не по факту исполнения.
        v5.4.2: risk=False — кусок выхода (исполнение частями, остаток ушёл со счёта перед закрытием): в журнал
        (self.pnls) сразу, а killswitch увидит итог круга один раз — _risk_round из _finish_closed / _reduce."""
        sgn = 1.0 if pos["side"] == "long" else -1.0
        pnl = (px - pos["entry"]) * sgn * int(lots) * self.point_value if px else 0.0
        self.pnls.append(pnl)
        if risk and self.session_risk:
            self.session_risk.record(pnl)
        log.warning("ИИ-пилот %s: %s — %d лот, P/L ≈%+.0f", self.base, why, lots, pnl)
        return pnl

    def _risk_round(self, pos: dict, pnl: float = 0.0) -> None:
        """v5.4.2: killswitch (серия убытков, дневной лимит) видит СДЕЛКУ, а не кусок исполнения: один выход,
        исполненный тремя частями, — одна запись серии, а не «три убытка подряд — стоп сессии». Сюда приходит итог
        круга (закрытие) или одного ужатия: куски выхода, ещё не отданные killswitch'у (exit_pnl_total − risk_fed),
        плюс pnl этого события (внешнее закрытие по сверке, переворот руками)."""
        total = _f(pos.get("exit_pnl_total"))
        unfed = total - _f(pos.get("risk_fed"))
        pos["risk_fed"] = total
        if self.session_risk:
            self.session_risk.record(unfed + pnl)

    def _absorb_account(self, real: int, avg: float | None, price: float | None, why: str) -> bool:
        """Счёт — истина. Всё по figi становится позицией бота: владелец докупил → больше,
        продал → меньше, открыл руками → принята (уровни временные, PRO назовёт свои),
        перевернул → перевёрнута. Внешнее закрытие (real=0) — дело _reconcile. Возврат:
        что-то изменилось?"""
        own = 0
        if self.position:
            own = self.position["lots"] if self.position["side"] == "long" else -self.position["lots"]
        self.foreign_lots = 0
        if real == own or (own and real == 0):
            return False
        px = price if price and price > 0 else (self.prices[-1] if self.prices else 0.0)
        old_stop = None
        if own and real * own < 0:             # владелец перевернул руками: старую фиксируем
            pos = self.position
            flip_pnl = self._realize(pos, pos["lots"], px, "владелец перевернул позицию руками", risk=False)
            self._risk_round(pos, flip_pnl)    # круг старой позиции — одна запись killswitch (v5.4.2)
            old_stop = pos.get("stop_id")      # её трос на бирже снимет restop новой позиции
                                               # (сирота-стоп иначе удвоил бы новую сторону)
            self.position = None
            self._drop_topup_plan()            # ревью 5.4.2: план ДОБОРА к старой стороне — не вердикт переворота
        if self.position:                      # та же сторона, другой объём
            pos = self.position
            new = abs(real)
            if new > pos["lots"]:
                add = new - pos["lots"]
                # средняя цена: биржа знает среднюю всей позиции — берём её, иначе взвешиваем
                pos["entry"] = _f(avg) if _f(avg) > 0 else (
                    (pos["entry"] * pos["lots"] + px * add) / new if px else pos["entry"])
                self.last_action = (f"владелец докупил {add} лот — веду всё вместе: {pos['side']} "
                                    f"{new} лот @{pos['entry']:g} ({why})")
            else:
                cut = pos["lots"] - new
                self._realize(pos, cut, px, "владелец продал часть руками")
                self.last_action = f"владелец продал {cut} лот — осталось {pos['side']} {new} лот ({why})"
            pos["lots"] = new
            pos["restop"] = True
            pos["topup_left"] = 0
            self._save_state()
            log.warning("ИИ-пилот %s: %s", self.base, self.last_action)
            return True
        # позиции не было — принимаем со счёта
        side = "long" if real > 0 else "short"
        entry = _f(avg) if _f(avg) > 0 else px
        pos = {"side": side, "entry": float(entry or 0.0), "lots": abs(real), "take": None,
               "invalidation": None, "opened_ts": time.time(), "stop_id": old_stop, "floating": 0.0,
               "adopted": True}
        plan = self.plan
        hold, self._pending_hold = self._pending_hold, None
        hold_bad, hold_ok = "", ""
        if plan and plan.get("side") == side:  # приказ той же стороны: его уровни — уровни позиции
            self._set_levels(pos, plan.get("take"), plan.get("invalidation"))
        elif hold:                             # ревью 5.4.2: HOLD пришёл до подготовки — уровни совета к этой позиции
            hold_bad, hold_ok = self._hold_levels(pos, hold)
        if not _f(pos.get("invalidation")) and entry and entry > 0:
            # временный триггер, пока ИИ не назвал свои уровни; PRO позовём скоро
            inv = entry * (1 - ADOPT_STOP_PCT / 100 if side == "long" else 1 + ADOPT_STOP_PCT / 100)
            self._set_levels(pos, None, round(inv, 6))
            pos["levels_placeholder"] = True
            self.review_ts = min(self.review_ts, time.time() + 90)
        if hold_bad:
            # уровень HOLD не подошёл к стороне позиции — временный вместо него; причина — дежурному PRO в повод
            self.review_ts = min(self.review_ts, time.time() + 90)
            rr = f"уровни приказа HOLD к позиции со счёта {side} не приняты ({hold_bad}) — назови свои"
            if hasattr(self, "_review_reason"):          # наследник (MissionPilot) копит поводы перепроверки
                prev = getattr(self, "_review_reason", None)
                self._review_reason = f"{prev}; {rr}" if prev and rr not in prev else (prev or rr)
        pos["restop"] = True
        self.position = pos
        self.state = "В_ПОЗИЦИИ"
        self._save_state()
        self._mx = None
        self.last_action = (f"ПРИНЯЛ позицию со счёта: {side} {abs(real)} лот @{pos['entry']:g} ({why})"
                            + (f", уровни приказа HOLD: {hold_ok}" if hold_ok else "")
                            + (", уровни временные — ждёт PRO" if pos.get("levels_placeholder") else "")
                            + (f" (HOLD не принят: {hold_bad})" if hold_bad else ""))
        log.warning("ИИ-пилот %s: %s", self.base, self.last_action)
        return True

    def _hold_levels(self, pos: dict, hold: dict) -> tuple[str, str]:
        """Ревью 5.4.2: приказ HOLD пришёл до prepare() — его стоп/тейк к позиции, принятой со счёта. Сторону совет знал
        по счёту, но цену при подготовке пилот ещё не видел: проверка — согласованность уровней для стороны позиции
        (_plan_valid, как у HOLD в позиции), без вето по живой цене (пройденный стоп — триггер, вопрос у троса).
        Не подошёл уровень — на его месте временный (стоп ADOPT_STOP_PCT от входа / тейка нет).
        Возврат: (причина отказа или '', что принято или '')."""
        side = pos["side"]
        inv = _f(hold.get("inv")) if hold.get("inv") is not None else 0.0
        take = _f(hold.get("take")) if hold.get("take") is not None else 0.0
        inv = inv if inv > 0 else None
        take = take if take > 0 else None
        entry = _f(pos.get("entry"))
        bad: list[str] = []
        if inv is not None:
            why = self._plan_valid(side, None, take, inv)
            if why:
                bad.append(f"стоп {inv:g} / тейк {take:g}: {why}" if take is not None else f"стоп {inv:g}: {why}")
                inv = take = None
        if inv is None and take is not None:
            ph = entry * (1 - ADOPT_STOP_PCT / 100 if side == "long" else 1 + ADOPT_STOP_PCT / 100) if entry > 0 else 0.0
            why = self._plan_valid(side, None, take, ph) if ph > 0 else "нет цены входа для временного стопа"
            if why:
                bad.append(f"тейк {take:g}: {why}")
                take = None
        ok = ", ".join(x for x in (f"стоп {inv:g}" if inv is not None else "",
                                    f"тейк {take:g}" if take is not None else "") if x)
        if ok:
            self._set_levels(pos, take, inv)
        if bad:
            log.warning("ИИ-пилот %s: уровни HOLD к позиции со счёта не приняты: %s", self.base, "; ".join(bad))
        return "; ".join(bad), ok

    def _soft_stop_on(self) -> bool:
        return bool(self.SOFT_STOP) and bool(_setting("PYTHIA_SOFT_STOP", True))

    def _soft_take_on(self) -> bool:
        """Мягкий тейк (v5.3 W2): только у пилота с ИИ у денег (SOFT_STOP) и при PYTHIA_SOFT_TAKE."""
        return bool(self.SOFT_STOP) and bool(_setting("PYTHIA_SOFT_TAKE", True))

    @staticmethod
    def _exchange_stop_on() -> bool:
        """v5.4.1 «стопы только в программе»: PYTHIA_EXCHANGE_STOP=1 — аварийный трос лежит на бирже (как в
        5.2–5.4.0); 0 (умолчание) — стоп-заявок на бирже нет, трос виртуальный: за ним тик закрывает по рынку.
        Читается живьём — переключение в панели отрабатывает на следующем restop/тике в обе стороны."""
        return bool(_setting("PYTHIA_EXCHANGE_STOP", False))

    @staticmethod
    def _money_name() -> str:
        """Имя модели узлов у денег для текстов панели/логов: «PRO» (умолчание 5.4.1) или «FLASH»
        (PYTHIA_MONEY_MODEL, ai_v5.money_model()). Без ai_v5 — «PRO»."""
        try:
            try:
                from . import ai_v5 as _a
            except ImportError:
                import ai_v5 as _a                       # noqa: E401
            return str(_a.money_model() or "pro").upper()
        except Exception:                                # noqa: BLE001
            return "PRO"

    def _hard_name(self) -> str:
        """«аварийный трос биржи» / «аварийный трос в программе» — как есть на самом деле."""
        return "аварийный трос биржи" if self._exchange_stop_on() else "аварийный трос в программе"

    def _hard_of(self, pos: dict) -> float | None:
        """Аварийный трос: на PYTHIA_HARD_STOP_PCT % дальше «настоящего» стопа ИИ (inv0).
        На бирже (PYTHIA_EXCHANGE_STOP=1) или только в программе; за ним закрываем без вопросов.
        Без мягкого стопа — None."""
        base = _f(pos.get("inv0")) or _f(pos.get("invalidation"))
        if base <= 0 or not self._soft_stop_on():
            return None
        pct = float(_setting("PYTHIA_HARD_STOP_PCT", 1.5)) / 100.0
        hard = base * (1 - pct) if pos.get("side") == "long" else base * (1 + pct)
        # v5.3 W2 (проверяющий): после «подержать» у тейка прибыль заперта — трос биржи не уходит за вход
        # (малая цель: вход + 50 % хода − 1.5 % оказывался ниже входа, и гэп за трос давал убыток).
        # v5.4.1: то же после «ДЕРЖАТЬ с lock_price» мысли о прибыли (profit_lock)
        entry = _f(pos.get("entry"))
        if (int(pos.get("take_holds") or 0) > 0 or pos.get("profit_lock")) and entry > 0:
            if pos.get("side") == "long" and entry < base:
                hard = max(hard, entry)
            elif pos.get("side") != "long" and entry > base:
                hard = min(hard, entry)
        return round(self._snap(hard), 10)

    def _set_levels(self, pos: dict, take, inv, *, inv0: bool = True) -> None:
        """Уровни позиции: тейк, триггер (invalidation) и от него — аварийный трос.
        inv0=True — это стоп ИИ (совет/PRO): трос считается от него; False — FLASH
        подвинул только триггер, трос остаётся где был."""
        if take is not None:
            pos["take"] = _f(take) if _f(take) > 0 else None
        if inv is not None and _f(inv) > 0:
            pos["invalidation"] = round(_f(inv), 6)
            if inv0 or not _f(pos.get("inv0")):
                pos["inv0"] = pos["invalidation"]
        pos["hard_stop"] = self._hard_of(pos)

    def _stop_price(self, pos: dict) -> float:
        """Уровень аварийного троса (на бирже при PYTHIA_EXCHANGE_STOP, иначе виртуальный):
        при мягком стопе — трос дальше триггера, иначе — сам стоп."""
        hard = _f(pos.get("hard_stop"))
        if self._soft_stop_on() and hard > 0:
            return hard
        return _f(pos.get("invalidation"))

    # ── деньги ──────────────────────────────────────────────────────────────
    @staticmethod
    def _msk_day() -> int:
        return int((time.time() + 3 * 3600) // 86400)

    def _free_cash(self) -> float:
        """Свободные деньги: свежие со сверки, иначе депозит + реализованное."""
        if self._live_cash is not None:
            return self._live_cash
        return self.deposit + sum(self.pnls)

    def per_lot(self, price: float, side: str) -> float:
        """Обеспечение одного лота: фьючерс — ГО стороны; акция — стоимость
        лота × риск-ставка Тинькофф (плечо), нет ставки → на свои."""
        if self.asset_class == "futures":
            if side == "short" and _f(self.go_sell) > 0:
                return _f(self.go_sell)
            return _f(self.go_per_lot)
        notional = _f(price) * max(1.0, _f(self.point_value, 1.0))
        d = self.dlong if side == "long" else self.dshort
        return notional * (d if 0.0 < d <= 1.0 else 1.0)

    def max_lots(self, price: float, side: str | None = None) -> int:
        """Сколько лотов брать: СКОЛЬКО ДАЁТ БИРЖА (GetMaxLots с маржой, кэш _mx) — это и есть
        «на максималку»; без ответа биржи (dry/sandbox/отказ) — локальный расчёт от денег и ГО.
        Ограничение владельца (deposit при запуске) режет сверху."""
        side = side or (self.plan or {}).get("side") \
            or (self.position or {}).get("side") or "long"
        cap = self._cap_lots(price, side)
        mx = self._mx
        if mx and time.time() - mx["ts"] <= MX_TTL_SEC * 3:
            n = int(mx["buy"] if side == "long" else mx["sell"])
            self._sized_by_broker = True
            return max(0, min(n, cap) if cap is not None else n)
        self._sized_by_broker = False
        per = self.per_lot(price, side)
        if per <= 0:
            return 0
        free = self._free_cash()
        free -= abs(self.foreign_lots) * self.per_lot(price, "long")
        pos = self.position
        if pos and pos.get("side") == side:    # добор: обеспечение уже набранного занято
            free -= int(pos.get("lots") or 0) * per
        lots = max(0, int(free * (1.0 - RESERVE_FRAC) // per))
        return min(lots, cap) if cap is not None else lots

    def _cap_lots(self, price: float, side: str) -> int | None:
        """Владелец ограничил сумму при запуске (deposit) → потолок лотов; при позиции той же
        стороны — за вычетом уже набранного. None — ограничения нет (весь счёт)."""
        dep = _f(self.deposit_override)
        if dep <= 0:
            return None
        per = self.per_lot(price, side)
        if per <= 0:
            return None
        cap = int(dep * (1.0 - RESERVE_FRAC) // per)
        pos = self.position
        if pos and pos.get("side") == side:
            cap -= int(pos.get("lots") or 0)
        return max(0, cap)

    async def _refresh_max(self, price: float, force: bool = False) -> None:
        """Спросить биржу, сколько даёт (GetMaxLots); кэш MX_TTL_SEC. dry/sandbox или
        отказ → кэша нет, сайзер честно считает сам."""
        fn = getattr(self.broker, "max_lots", None)
        if not fn or not self.figi:
            return
        if not force and self._mx and time.time() - self._mx["ts"] < MX_TTL_SEC:
            return
        try:
            r = await fn(self.figi, price)
        except Exception as e:                               # noqa: BLE001
            r = None
            log.info("ИИ-пилот %s: GetMaxLots: %s", self.base, str(e)[:80])
        if isinstance(r, dict):
            self._mx = {"buy": int(r.get("buy") or 0), "sell": int(r.get("sell") or 0),
                        "ts": time.time(), "price": price, "buy_money": r.get("buy_money")}
            self.account.update({"max_buy": self._mx["buy"], "max_sell": self._mx["sell"],
                                 "mx_ts": self._mx["ts"]})
        elif self._mx and time.time() - self._mx["ts"] > MX_TTL_SEC * 3:
            self._mx = None

    async def _refresh_account(self, price: float) -> None:
        """Маржинальные атрибуты счёта (ликвидный портфель, недостающие средства) и «сколько
        даёт биржа» — раз в сверку; для дозора, сайзера и панели."""
        fn = getattr(self.broker, "margin", None)
        if fn:
            try:
                mg = await fn()
            except Exception:                                # noqa: BLE001
                mg = None
            if isinstance(mg, dict):
                self.account.update({"liquid": mg.get("liquid"), "sufficiency": mg.get("sufficiency"),
                                     "missing": mg.get("missing"), "ts": time.time()})
        if self.plan or self.position:
            await self._refresh_max(price)

    def _account_line(self) -> str:
        acc = self.account or {}
        if not acc:
            return ""
        parts = []
        if acc.get("free") is not None:
            parts.append(f"свободно {_f(acc['free']):.0f} ₽")
        if acc.get("liquid") is not None:
            parts.append(f"ликвидный портфель {_f(acc['liquid']):.0f} ₽")
        if acc.get("max_buy") is not None:
            parts.append(f"биржа даёт купить {acc.get('max_buy')} / продать {acc.get('max_sell')} лот")
        if _f(acc.get("missing")) > 0:
            parts.append(f"НЕ ХВАТАЕТ {_f(acc['missing']):.0f} ₽ обеспечения")
        return ("Счёт: " + ", ".join(parts)) if parts else ""

    def _snap(self, px: float) -> float:
        """Цена — на решётку шага (иначе биржа отбивает некратную лимитку)."""
        t = self.tick_size
        if t and t > 0 and px > 0:
            return round(round(px / t) * t, 10)
        return round(px, 10)

    # ── валидация приказа (стороны уровней — вторая линия после санитайзера) ─
    @staticmethod
    def _plan_valid(side: str, entry, take, inv) -> str | None:
        """None = ок, иначе причина отказа. Для long: inv ниже входа/тейка,
        take выше; short — зеркально. Мусорный приказ не исполняется."""
        inv = _f(inv)
        if inv <= 0:
            return "invalidation не задан или ≤0 — позиции без стопа не бывает"
        e = _f(entry) if entry is not None else None
        t = _f(take) if take is not None else None
        if e is not None and e <= 0:
            return "entry ≤ 0 — мусорная цена"
        if side == "long":
            if e is not None and inv >= e:
                return f"BUY: стоп {inv} НЕ НИЖЕ входа {e}"
            if t is not None and e is not None and t <= e:
                return f"BUY: тейк {t} не выше входа {e}"
            if t is not None and t <= inv:
                return f"BUY: тейк {t} не выше стопа {inv}"
        else:
            if e is not None and inv <= e:
                return f"SELL: стоп {inv} НЕ ВЫШЕ входа {e}"
            if t is not None and e is not None and t >= e:
                return f"SELL: тейк {t} не ниже входа {e}"
            if t is not None and t >= inv:
                return f"SELL: тейк {t} не ниже стопа {inv}"
        return None

    # ── приказ шифровщика (после огромного анализа) ─────────────────────────
    def adopt_forecast(self, forecast: dict | None) -> bool:
        """Свежий прогноз конвейера → боевой план. Возврат: план принят?"""
        self._reanalyzing = False
        self._pending_hold = None              # ревью 5.4.2: HOLD до подготовки — только пока не пришёл новый приказ
        if self.session_risk and self.session_risk.locked:
            self.last_action = ("killswitch заблокирован — свежий приказ НЕ "
                                "принят; сброс — новым торговым днём")
            log.warning("ИИ-пилот %s: %s", self.base, self.last_action)
            return False
        ex = (forecast or {}).get("exec") if isinstance(forecast, dict) else None
        do = str((ex or {}).get("do") or "").upper().strip() if isinstance(ex, dict) else ""
        self._wait_order = False               # v5.4.2: действующий приказ совета — WAIT? (шаг плановой перепроверки)
        if do in ("WAIT", "ЖДАТЬ", "HOLD_FLAT"):
            # приказ WAIT (v5.4.1): вход не сейчас — плана нет, стоим вне рынка; дежурный PRO
            # вернётся к этому на перепроверке (и может войти сам). При позиции WAIT отсекает
            # валидатор миссии (_validate_exec); здесь — только на всякий случай: позицию не трогаем
            self.plan = None
            if self.pending:
                self._cancel_entry = True
            wait_for = str(ex.get("wait_for") or ex.get("why") or "").strip()[:200]
            now = time.time()
            if not self.position:
                self.state = "ЖДУ_ПЛАН"
                # v5.4.2: вне рынка по WAIT дежурный PRO смотрит заново через PYTHIA_WAIT_REVIEW_SEC (умолчание 30 мин —
                # базовый ритм; раньше будят триггеры: уровни WAIT, прокол, поводы, события); перепроверку, которую уже
                # подтянули раньше (повод, событие), не отодвигаем
                wait_sec = wait_review_sec()
                self.review_ts = min(self.review_ts, now + wait_sec) if self.review_ts > now else now + wait_sec
                self._wait_order = True
            else:
                self.review_ts = now + REVIEW_SEC
            mins = max(0, int((self.review_ts - now + 59) // 60))
            self.last_action = (f"совет: вне рынка — ждал: {wait_for or 'условие не названо'}; "
                                f"перепроверка через {mins} мин")
            log.info("ИИ-пилот %s: %s", self.base, self.last_action)
            return True
        if do == "HOLD":
            return self._adopt_hold(ex)
        if do in ("CLOSE", "ЗАКРЫТЬ", "FLAT", "EXIT"):
            # приказ CLOSE (v5.2): закрыть позицию и стоять вне рынка до следующего решения
            self.plan = None
            if self.pending:
                self._cancel_entry = True
            why_c = "приказ совета: закрыть позицию" + (
                f" — {str(ex.get('why'))[:120]}" if ex.get("why") else "")
            if self.position:
                self._close_pending = why_c
                self.last_action = "приказ CLOSE принят — закрываю позицию ближайшим тиком"
            elif self.pending:
                # заявка входа в полёте: снимаем (_cancel_entry выше), частичка станет позицией — и закроется
                self._close_pending = why_c
                self.state = "ЖДУ_ПЛАН"
                self.last_action = "приказ CLOSE принят — снимаю заявку входа, исполненную часть закрою"
            elif self.adopt_account and not self._prepared:
                # приказ пришёл до prepare(): позицию со счёта приму при подготовке — и закрою
                self._close_pending = why_c
                self.state = "ЖДУ_ПЛАН"
                self.last_action = "приказ CLOSE принят — позицию со счёта закрою, как только приму её"
            else:
                self._close_pending = None
                self.state = "ЖДУ_ПЛАН"
                self.last_action = "приказ CLOSE: позиции нет — стою вне рынка до следующего решения"
            self.review_ts = time.time() + REVIEW_SEC
            log.info("ИИ-пилот %s: %s", self.base, self.last_action)
            return True
        if not isinstance(ex, dict) or ex.get("invalidation") is None:
            self.last_action = ("шифровщик не дал исполняемый exec-блок — "
                                "сделки нет, жду следующий разбор (NO DUMMIES)")
            if not self.position:
                self.state = "ЖДУ_ПЛАН"
            # без приказа не спим 30 минут: перепроверка через 5 минут сама
            # решит, просить ли новый разбор
            self.review_ts = min(self.review_ts, time.time() + 300)
            log.warning("ИИ-пилот %s: %s", self.base, self.last_action)
            return False
        side = "long" if str(ex.get("do")).upper() == "BUY" else "short"
        bad = self._plan_valid(side, ex.get("entry"), ex.get("take"),
                               ex.get("invalidation"))
        if not bad:
            self._close_pending = None         # свежий приказ стороны важнее старого CLOSE
        if bad:
            self.last_action = f"приказ отклонён ({bad}) — жду вменяемый"
            self.review_ts = min(self.review_ts, time.time() + 300)
            log.warning("ИИ-пилот %s: %s", self.base, self.last_action)
            return False
        # позиция ТОЙ ЖЕ стороны: свежий разбор обновляет стоп/тейк позиции
        # (иначе дорогой прогон ИИ ничего не менял — находка веера)
        if self.position and self.position["side"] == side:
            self._set_levels(self.position, ex.get("take"), ex.get("invalidation"))
            self.position["restop"] = True     # перевыставить трос тиком
            self.position.pop("levels_placeholder", None)
            self.position["holds"] = 0
            self.position["take_holds"] = 0        # v5.3 W2: новый приказ — счётчик «подержать» у тейка заново
            self.position.pop("take_next", None)
            self.position.pop("profit_lock", None)  # v5.4.1: стоп совета главнее запертой прибыли
            self.position["hard_stop"] = self._hard_of(self.position)
            self.plan = None
            self._save_state()
            self.last_action = (f"свежий вердикт той же стороны: обновил "
                                f"стоп {self.position['invalidation']} / "
                                f"тейк {self.position['take']}")
            log.info("ИИ-пилот %s: %s", self.base, self.last_action)
            # добор до максимума по свежему приказу «сейчас»: биржа даёт ещё — берём
            if ex.get("entry") is None and self._sized_by_broker:
                self.position["topup_left"] = int(_setting("PYTHIA_TOPUP_MAX", 2))
                self.position["last_fill_ts"] = 0.0
            return True
        self.plan = {"side": side, "entry": ex.get("entry"),
                     "take": ex.get("take"),
                     "invalidation": _f(ex.get("invalidation")),
                     "why": str(ex.get("why") or "")[:300], "ts": time.time()}
        if self.pending:                       # заявка старого плана в полёте
            self._cancel_entry = True          # → снять ближайшим тиком
        if not self.position:
            self.state = "ЗАСАДА" if self.plan["entry"] is not None else "ВХОЖУ"
        self.review_ts = time.time() + REVIEW_SEC
        self._entry_fail = 0
        self.last_action = (f"план принят: {side} "
                            + (f"засада @{self.plan['entry']}"
                               if self.plan["entry"] is not None else "вход сразу")
                            + f", тейк {self.plan['take']}, "
                              f"стоп {self.plan['invalidation']}")
        log.info("ИИ-пилот %s: %s (%s)", self.base, self.last_action,
                 self.plan["why"])
        return True

    def _adopt_hold(self, ex: dict) -> bool:
        """v5.4.2 (ревью): приказ HOLD — совет сказал «держать». Позиция как есть, БЕЗ добора: обновляются только
        стоп/тейк (null — прежний уровень); счётчики и сбросы — как у свежего вердикта той же стороны, но topup_left
        HOLD обнуляет (остаток авто-добора прошлого входа снят, заявка добора в полёте снимается ближайшим тиком):
        добор до максимума — только явный BUY/SELL той же стороны. Позиции нет — держать нечего.
        Ревью 5.4.2 (финал): проверка уровней — только согласованность стопа и тейка для стороны позиции (_plan_valid,
        как у BUY/SELL той же стороны); живая цена приказ не отклоняет: стоп, который цена уже прошла, становится
        триггером (ближайший тик спросит у троса), пройденный тейк — вопрос мягкого тейка. Стоп прежний (null или то же
        число), а цена уже за ним — счётчик «ждать» у троса не сбрасывается (PYTHIA_SOFT_STOP_MAX_HOLDS ограничивает круг
        «трос ждёт → совет HOLD → трос ждёт»). До prepare() уровни запоминаются (_pending_hold) и ложатся на позицию,
        принятую со счёта (_absorb_account → _hold_levels)."""
        pos = self.position
        now = time.time()
        inv = _f(ex.get("invalidation")) if ex.get("invalidation") not in (None, "", "null") else 0.0
        take = _f(ex.get("take")) if ex.get("take") not in (None, "", "null") else 0.0
        inv = inv if inv > 0 else None
        take = take if take > 0 else None
        if not pos:
            if self.adopt_account and not self._prepared:
                # приказ пришёл до prepare(): позицию со счёта приму при подготовке — с уровнями совета (стороны
                # проверю по ней); не подошли — временные до дежурного PRO
                self._pending_hold = {"take": take, "inv": inv, "ts": now}
                self.plan = None
                self._close_pending = None     # свежий приказ «держать» важнее старого CLOSE
                lv = ", ".join(x for x in (f"стоп {inv:g}" if inv is not None else "",
                                           f"тейк {take:g}" if take is not None else "") if x)
                self.last_action = ("приказ HOLD принят — позицию со счёта приму при подготовке"
                                    + (f", уровни совета ({lv}) — к ней после проверки сторон" if lv else
                                       ", уровни назовёт дежурный PRO"))
                log.info("ИИ-пилот %s: %s", self.base, self.last_action)
                return True
            if not self.plan and not self.pending:
                self.state = "ЖДУ_ПЛАН"
            self.review_ts = min(self.review_ts, now + 300)
            self.last_action = "приказ HOLD, а позиции уже нет — держать нечего; дежурный PRO решит через 5 мин"
            log.warning("ИИ-пилот %s: %s", self.base, self.last_action)
            return False
        side = pos["side"]
        inv_eff = inv if inv is not None else pos.get("invalidation")
        take_eff = take if take is not None else pos.get("take")
        bad = self._plan_valid(side, None, take_eff, inv_eff) if _f(inv_eff) > 0 else None
        if bad:
            self.last_action = f"приказ HOLD отклонён ({bad}) — позиция как есть, уровни прежние"
            self.review_ts = min(self.review_ts, now + 300)
            log.warning("ИИ-пилот %s: %s", self.base, self.last_action)
            return False
        cur = self.prices[-1] if self.prices else 0.0
        old_inv = _f(pos.get("invalidation"))
        # стоп прежний (null или то же число), а цена уже за ним: трос спрашивает (или спросит) — счётчик «ждать» живёт
        past_stop = ((inv is None or abs(inv - old_inv) <= 1e-9) and cur > 0 and old_inv > 0
                     and ((side == "long" and cur <= old_inv) or (side == "short" and cur >= old_inv)))
        # то же у тейка: тейк прежний, а цена уже у него — счётчик «подержать» живёт (PYTHIA_SOFT_TAKE_MAX_HOLDS
        # ограничивает круг «тейк держит → совет HOLD → тейк держит»)
        old_take = _f(pos.get("take"))
        past_take = ((take is None or abs(take - old_take) <= 1e-9) and cur > 0 and old_take > 0
                     and ((side == "long" and cur >= old_take) or (side == "short" and cur <= old_take)))
        self._close_pending = None             # свежий приказ «держать» важнее старого CLOSE
        locked = int(pos.get("take_holds") or 0) > 0 or bool(pos.get("profit_lock"))
        self._set_levels(pos, take, inv)       # None — прежний уровень
        pos["restop"] = True                   # перевыставить трос тиком
        if inv is not None:
            pos.pop("levels_placeholder", None)
            pos.pop("profit_lock", None)       # стоп совета главнее запертой прибыли (как у вердикта той же стороны)
        elif locked:
            pos["profit_lock"] = True          # стоп прежний = запертая прибыль: трос не уходит за вход
        if not past_stop:
            pos["holds"] = 0
        if not past_take:
            pos["take_holds"] = 0              # новый приказ — счётчик «подержать» у тейка заново
            pos.pop("take_next", None)
        pos["topup_left"] = 0                  # «держать» — не добор: остаток авто-добора прошлого входа снят
        if self.pending and self.pending.get("topup"):
            self._cancel_entry = True          # заявка добора в полёте — снять ближайшим тиком (частичка — в позицию)
        pos["hard_stop"] = self._hard_of(pos)
        self.plan = None                       # план добора / переворота снят: совет сказал держать как есть
        self._save_state()
        crossed = ""
        if cur > 0:
            n_inv, n_take = _f(pos.get("invalidation")), _f(pos.get("take"))
            if n_inv > 0 and ((side == "long" and cur <= n_inv) or (side == "short" and cur >= n_inv)):
                crossed = f" — цена {cur:g} уже за стопом: ближайший тик спросит у троса"
            elif n_take > 0 and ((side == "long" and cur >= n_take) or (side == "short" and cur <= n_take)):
                crossed = f" — цена {cur:g} уже у тейка: ближайший тик спросит у тейка"
        self.last_action = (f"приказ HOLD: держу {side} {pos['lots']} лот как есть, без добора — стоп "
                            f"{pos['invalidation']}{'' if inv is not None else ' (прежний)'} / тейк "
                            f"{pos['take']}{'' if take is not None else ' (прежний)'}" + crossed)
        log.info("ИИ-пилот %s: %s", self.base, self.last_action)
        return True

    # ── рыночный гейт: стакан жив и двусторонний? ───────────────────────────
    def _market_alive(self) -> bool:
        b = self.last_book or {}
        if not (_f(b.get("best_bid")) > 0 and _f(b.get("best_ask")) > 0):
            return False
        return (time.time() - self._book_ts) <= BOOK_FRESH_SEC

    def _at_limit(self, side: str, price: float) -> bool:
        """Планка: цена у лимита сессии — вход в лок запрещён."""
        b = self.last_book or {}
        up, dn = _f(b.get("limit_up")), _f(b.get("limit_down"))
        if side == "long" and up > 0 and price >= up * 0.999:
            return True
        if side == "short" and dn > 0 and price <= dn * 1.001:
            return True
        return False

    # ── рыночные часы: биржа закрыта → стопор ────────────────────────────────
    async def _market_check(self) -> dict | None:
        """Статус торгов (market_clock, кэш 60 с). None — часы выключены (PYTHIA_MARKET_CLOCK=0)
        или модуля нет: считаем рынок открытым, как раньше."""
        if market_clock is None:
            return None
        try:
            if not market_clock.enabled():
                self.market = None
                return None
            st = await market_clock.status(self.base, self.asset_class, self.figi)
        except Exception as e:                               # noqa: BLE001
            log.info("ИИ-пилот %s: рыночные часы споткнулись: %s", self.base, str(e)[:80])
            return self.market                               # прошлый статус, если был
        if isinstance(st, dict):
            self.market = st
        return self.market

    def _market_closed(self) -> bool:
        return bool(self.market) and not self.market.get("open")

    def _market_line(self) -> str:
        if market_clock is None or not self.market:
            return ""
        try:
            return market_clock.describe(self.market)
        except Exception:                                    # noqa: BLE001
            return ""

    def _resting_state(self) -> str:
        """Состояние по факту (после открытия рынка): позиция / засада / вход / жду план."""
        if self.position:
            return "В_ПОЗИЦИИ"
        if self.plan:
            return "ЗАСАДА" if self.plan.get("entry") is not None else "ВХОЖУ"
        return "ЖДУ_ПЛАН"

    async def _closed_tick(self, price: float, mk: dict, first: bool) -> None:
        """Рынок закрыт: входов, доборов, перевзводов нет; заявка входа снимается (частичка — в учёт);
        позиция остаётся под аварийным тросом биржи, FLASH у триггера не спрашиваем; перепроверка
        ждёт открытия; сверка со счётом реже. Паника/killswitch отработали раньше (0б)."""
        now = time.time()
        if first:
            self._closed_since = now
            self._review_saved = self.review_ts
            log.warning("ИИ-пилот %s: %s", self.base, self._market_line() or "рынок закрыт")
        if self.pending:
            part, po = await self._cancel_pending()
            if part > 0 and po:
                await self._absorb_fill(part, po)
            if not self.pending:
                log.info("ИИ-пилот %s: рынок закрыт — заявка входа снята", self.base)
        if self.position:
            pos = self.position
            sgn = 1.0 if pos["side"] == "long" else -1.0
            pos["floating"] = round((price - pos["entry"]) * sgn * pos["lots"] * self.point_value, 2)
        self.state = "РЫНОК_ЗАКРЫТ"
        line = self._market_line() or "рынок закрыт"
        tail = " — входов, доборов и заявок нет"
        if self.pending:
            tail = " — подтверждение отмены заявки ещё не получено, повторяю опрос"
        if self.position:
            hard = _f(self.position.get("hard_stop"))
            where = ("под аварийным тросом биржи" if self._exchange_stop_on() else
                     "под аварийным тросом в программе (стопов на бирже нет)")
            tail += (f"; позиция {self.position['side']} {self.position['lots']} лот {where}"
                     + (f" @{hard:g}" if hard > 0 else "") + ", плавающий "
                     f"{_f(self.position.get('floating')):+.0f}")
        elif self.plan:
            tail += "; приказ жду исполнить с открытия"
        self.last_action = line + tail
        # перепроверка и вопросы ИИ — к открытию (+ запас), время открытия неизвестно → отложить
        nxt = mk.get("next_open_in_s")
        due = now + float(nxt) + OPEN_REVIEW_GRACE_SEC if nxt is not None else now + 600.0
        if self.review_ts < due:
            self.review_ts = due
        if self._tick_n % CLOSED_RECONCILE_EVERY == 0:
            await self._reconcile(price)

    async def _on_market_open(self, price: float) -> None:
        """Рынок открылся: сразу сверка со счётом; закрыт был долго (ночь/выходной) → перепроверка
        с поводом «рынок открылся: накопились новости/события» (наследник собирает заметки дозора)."""
        closed_for = time.time() - self._closed_since if self._closed_since else 0.0
        self._opened_ts = time.time()
        await self._reconcile(price)
        if self.state == "РЫНОК_ЗАКРЫТ":
            self.state = self._resting_state()
        self.last_action = "рынок открылся — сверка со счётом сделана"
        log.warning("ИИ-пилот %s: рынок открылся (закрыт был %d мин) — сверка со счётом", self.base, int(closed_for // 60))
        if closed_for >= CLOSED_LONG_SEC or not self._closed_since:
            self._market_open_review(closed_for)
        elif self._review_saved is not None:             # короткий клиринг: плановая перепроверка как была
            self.review_ts = max(time.time() + OPEN_REVIEW_GRACE_SEC, min(self.review_ts, self._review_saved))
        self._closed_since = 0.0
        self._review_saved = None

    def _market_open_review(self, closed_for: float) -> None:
        """Базовый пилот: перепроверка сразу после открытия (запас OPEN_REVIEW_GRACE_SEC)."""
        self.review_ts = min(self.review_ts, time.time() + OPEN_REVIEW_GRACE_SEC)
        self.last_action = "рынок открылся: накопились новости/события — перепроверка после открытия"

    # ── петля ───────────────────────────────────────────────────────────────
    async def tick(self, price: float, book: dict | None = None) -> None:
        if price and price > 0:
            self.prices.append(float(price))
            if len(self.prices) > 2400:
                del self.prices[:1200]
        if book:
            self.last_book = book
            if _f(book.get("best_bid")) > 0 and _f(book.get("best_ask")) > 0:
                self._book_ts = time.time()
        self._tick_n += 1
        now = time.time()
        self._last_tick_ts = now

        # 0а. новый торговый день (МСК) вне позиции → killswitch заново
        #     (иначе «дневной −6%» копится за всю жизнь пилота — находка веера)
        day = self._msk_day()
        if (self._sr_day is not None and day != self._sr_day
                and not self.position and not self.pending):
            self._sr_day = day
            eq = max(1.0, self._free_cash())
            self.session_risk = trader_risk.SessionRisk(eq)
            self.pnls = []
            if self.state == "СТОП" and not self.panic_flag:
                self.state = "ЖДУ_ПЛАН"
                self.review_ts = min(self.review_ts, time.time() + 300)
            log.info("ИИ-пилот %s: новый торговый день — killswitch заново "
                     "(база %.0f)", self.base, eq)

        # 0б. ТОРМОЗА: killswitch/ПАНИКА выше любых планов
        if self.panic_flag or (self.session_risk and self.session_risk.locked):
            why = ("ПАНИКА владельца" if self.panic_flag else
                   "killswitch: " + str((self.session_risk.state() or {})
                                        .get("reason")))
            part, po = await self._cancel_pending()
            if part > 0 and po:                # частичка — под учёт и закрытие
                await self._absorb_fill(part, po, place_stop=False)
            self.plan = None
            if self.pending and self.panic_flag:
                # фантомная/неразрешимая заявка входа (№2): N попыток честно, дальше — упёрлись (повтор = force);
                # с force — снимаем её из учёта (UUID в лог для сверки владельцем) и закрываем позицию
                n = self._panic_pending_fails = int(getattr(self, "_panic_pending_fails", 0)) + 1
                po = self.pending
                rid = po.get("request_id") or po.get("order_id")
                if self.panic_force and n > PANIC_PENDING_DROP_ATTEMPTS:
                    log.error("ИИ-пилот %s: ПАНИКА force — заявка входа %s (%s %d лот) не разрешается %d попыток, снимаю "
                              "из учёта; сверь её у брокера по UUID", self.base, rid, po.get("side"), int(po.get("lots") or 0), n)
                    self.pending = None
                    self._cancel_entry = False
                    self._save_state()
                elif n >= PANIC_PENDING_DROP_ATTEMPTS:
                    self.panic_blocked = True
                    self.last_action = (f"{why} — заявка входа {rid} не разрешается (биржа её не подтверждает и не "
                                        f"отвергает, попытка {n}); повторная ПАНИКА (force) снимет её из учёта и закроет позицию")
                    return
                else:
                    self.last_action = (f"{why} — жду подтверждения отмены заявки входа (попытка {n}/{PANIC_PENDING_DROP_ATTEMPTS}, "
                                        "дальше — повторная ПАНИКА снимет её по force)")
                    return
            elif self.pending:
                self.last_action = why + " — жду подтверждения отмены заявки входа"
                return
            else:
                self._panic_pending_fails = 0
            if self.position:
                ok = await self._close_all(price, why, reanalyze=False)
                if not ok:
                    # причину назвал _close_all (отмена троса не подтверждена / выход ждёт исполнения /
                    # биржа отбила) — не подменяем её «закрытие отбито биржей» (W4, п. 3)
                    detail = str(self.last_action or "").strip()
                    if not detail or detail.startswith(why):
                        detail = "закрытие не завершено, повторяю каждый тик"
                    self.last_action = f"{why} — {detail}"
                    return                     # позиция в учёте, добьём дальше
            self.state = "СТОП"
            head = self.last_action if str(self.last_action or "").startswith("ЗАКРЫЛ ВСЁ") else why
            tail = " · торговля остановлена"    # причина и P/L закрытия остаются первыми; хвост один раз
            self.last_action = head if head.endswith(tail) else head + tail
            return

        # An accepted market exit can remain NEW or partially filled. Settle
        # its identity before any stop replacement, top-up, or fresh decision.
        # W4 (п. 2): неподтверждённый запрос стопа НЕ выключает виртуальный стоп — после попытки урегулировать
        # тик идёт дальше к тросу/триггеру/тейку (закрытие само доведёт запрос через _cancel_position_stop:
        # повтор того же UUID безопасен); замирают только свежие решения (входы, добор, перепроверка)
        unsettled = False
        if (self.position or {}).get("stop_request") and not self.position.get("exit_order"):
            pos = self.position
            if self._close_pending or pos.get("close_fail"):
                why = self._close_pending or pos["close_fail"]
                self._close_pending = None
                await self._close_all(price, why, reanalyze=pos.get("close_reanalyze", False))
                return
            if now >= _f(pos.get("restop_after")):
                await self._replace_stop(pos)
            unsettled = bool(self.position is pos and pos.get("stop_request"))
            if self.position is not pos or pos.get("exit_order"):
                return
        exit_order = (self.position or {}).get("exit_order")
        if exit_order:
            pos = self.position
            if self._close_pending or pos.get("close_fail") or exit_order.get("kind") == "close":
                why = self._close_pending or pos.get("close_fail") or exit_order.get("why") or "закрытие"
                self._close_pending = None
                await self._close_all(price, why, reanalyze=pos.get("close_reanalyze", False))
            else:
                await self._reduce(pos, exit_order["lots"], price, exit_order.get("why") or "ужатие")
            return

        # 0в. РЫНОЧНЫЕ ЧАСЫ: биржа закрыта → стопор (входов, доборов, перевзводов, вопросов ИИ нет;
        #     позиция под тросом биржи); открылась → сверка и перепроверка с накопленными поводами
        mk = await self._market_check()
        was_open = self._market_open_prev
        is_open = mk is None or bool(mk.get("open"))
        self._market_open_prev = is_open
        if not is_open:
            await self._closed_tick(price, mk, first=(was_open is not False))
            return
        if was_open is False:
            await self._on_market_open(price)

        # 1. заявка входа в полёте
        if self.pending:
            await self._pending_tick(price, book)
            return

        # 2. позиция: приказ CLOSE, недобитое закрытие, трос, дозор, тейк, флип, добор
        crossed = False
        if self.position:
            pos = self.position
            if self._close_pending:            # приказ CLOSE: закрыть и стоять вне рынка
                why, self._close_pending = self._close_pending, None
                self.plan = None
                await self._close_all(price, why, reanalyze=False)
                return
            if pos.get("close_fail"):          # биржа отбила закрытие — добить
                await self._close_all(price, pos["close_fail"],
                                      reanalyze=pos.get("close_reanalyze", True))
                return
            # v5.4.1 (V): переключили «Трос на бирже» в бою — привести стоп к флагу сейчас, а не при следующем поводе:
            # включили → трос встаёт на биржу (только у боевого брокера: dry/sandbox отвечают virtual без stopOrderId —
            # иначе запрос уходил бы каждый тик), выключили → снимается один раз (_replace_stop_once)
            if not unsettled and not pos.get("restop") and now >= _f(pos.get("restop_after")):
                ex_on = self._exchange_stop_on()
                if (ex_on and getattr(self.broker, "mode", "real") == "real" and not pos.get("stop_id")
                        and not pos.get("stop_request") and self._stop_price(pos) > 0):
                    pos["restop"] = True
                elif not ex_on and pos.get("stop_id"):
                    pos["restop"] = True
            if pos.get("restop") and now >= _f(pos.get("restop_after")) and not unsettled:   # свежий вердикт →
                await self._replace_stop(pos)          # перенос троса; отбитый трос → повтор после бэкоффа
                if self.position is not pos:           # W4: стопа на бирже нет и счёт пуст → сверка закрыла позицию
                    return
            sgn = 1.0 if pos["side"] == "long" else -1.0
            floating = (price - pos["entry"]) * sgn * pos["lots"] * self.point_value
            pos["floating"] = round(floating, 2)
            # аварийный трос (лежит на бирже, дальше стопа ИИ) — за ним без вопросов,
            # раньше любого дозора: биржа его уже исполнила бы
            inv = _f(pos.get("invalidation"))
            hard = _f(pos.get("hard_stop"))
            is_long = pos["side"] == "long"
            if hard > 0 and ((is_long and price <= hard) or (not is_long and price >= hard)):
                await self._close_all(price, f"аварийный трос @{hard:g}")
                return
            # маржевой дозор: капитал не тянет ГО → УЖАТЬСЯ до вмещающегося
            # (закрытие всего при −0.04% депозита убито — находка веера).
            # v5.2: размер считала биржа → судья только её маржинальные атрибуты
            # (недостающие средства), а не наша прикидка по деньгам
            per = self.per_lot(price, pos["side"])
            if self._sized_by_broker or self.account.get("liquid") is not None:
                miss = _f(self.account.get("missing"))
                if miss > 0 and per > 0:
                    cut = int(miss // per) + 1
                    fit = max(0, pos["lots"] - cut)
                    self.account["missing"] = 0.0          # до свежей сверки
                    if fit <= 0:
                        await self._close_all(price, f"маржевой дозор: брокеру не хватает {miss:.0f} ₽")
                    else:
                        await self._reduce(pos, pos["lots"] - fit, price,
                                           f"маржевой дозор: брокеру не хватает {miss:.0f} ₽ — ужатие до {fit} лот")
                    return
            else:
                equity = self._free_cash() + floating
                need = (pos["lots"] + abs(self.foreign_lots)) * per
                if per > 0 and equity < need * MAINT_FRAC:
                    fit = max(0, int(max(0.0, equity) * (1.0 - RESERVE_FRAC) // per)
                              - abs(self.foreign_lots))
                    if fit <= 0:
                        await self._close_all(price, "маржевой дозор: капитал "
                                              f"{equity:.0f} не тянет ГО {need:.0f}")
                    else:
                        await self._reduce(pos, pos["lots"] - fit, price,
                                           f"маржевой дозор: ужатие до {fit} лот")
                    return
            # триггер (invalidation): жёсткий стоп → закрыть; мягкий (v5.2) → FLASH за секунды
            # решает: слить или ждать и передать задачу Совету (_guard_bg); ждать можно не
            # больше PYTHIA_SOFT_STOP_MAX_HOLDS раз, потом закрываем сами
            crossed = inv > 0 and ((is_long and price <= inv) or (not is_long and price >= inv))
            if crossed:
                if not self._soft_stop_on():
                    await self._close_all(price, f"аварийный стоп @{inv:g}")
                    return
                max_holds = int(_setting("PYTHIA_SOFT_STOP_MAX_HOLDS", 3))
                if max_holds and int(pos.get("holds") or 0) >= max_holds:
                    await self._close_all(price, f"мягкий стоп: {self._money_name()} ждал {pos.get('holds')} раз — предел, закрываю")
                    return
                if not pos.get("guard_busy") and now >= _f(pos.get("guard_next")):
                    pos["guard_busy"] = True
                    pos["guard_price"] = price
                    self.last_action = (f"цена {price:g} за триггером {inv:g} — спрашиваю {self._money_name()}: "
                                        "слить или ждать и передать Совету")
                    log.info("ИИ-пилот %s: %s", self.base, self.last_action)
                    self._guard_task = self._spawn_background(self._guard_bg(price, pos))
            elif pos.get("holds") and now - _f(pos.get("guard_last")) > float(_setting("PYTHIA_SOFT_STOP_GRACE_SEC", 180)):
                pos["holds"] = 0               # цена вернулась за триггер — счётчик «ждать» заново
            # ПОБЕДА: тейк достигнут → продаём ВСЁ (на макс); мягкий тейк (v5.3 W2) → FLASH за секунды
            # решает: зафиксировать сейчас или подержать (триггер подтягивается, прибыль заперта) и
            # передать задачу Совету (_take_bg); подержать можно не больше PYTHIA_SOFT_TAKE_MAX_HOLDS раз
            take = _f(pos.get("take"))
            take_hit = take > 0 and ((is_long and price >= take) or (not is_long and price <= take))
            if take_hit and not crossed:
                if not self._soft_take_on():
                    await self._close_all(price, f"ПОБЕДА: тейк @{take:g} — всё в кассу")
                    return
                max_h = int(_setting("PYTHIA_SOFT_TAKE_MAX_HOLDS", 2))
                if max_h and int(pos.get("take_holds") or 0) >= max_h:
                    await self._close_all(price, f"ПОБЕДА: тейк @{take:g} — {self._money_name()} держал {pos.get('take_holds')} раз, "
                                                 "предел — фиксирую")
                    return
                if not pos.get("guard_busy") and now >= _f(pos.get("take_next")):
                    pos["guard_busy"] = True
                    pos["guard_side"] = "take"
                    pos["guard_price"] = price
                    self.last_action = (f"цена {price:g} у тейка {take:g} — спрашиваю {self._money_name()}: зафиксировать "
                                        "сейчас или подержать и передать Совету")
                    log.info("ИИ-пилот %s: %s", self.base, self.last_action)
                    self._guard_task = self._spawn_background(self._take_bg(price, pos))
            # флип: свежий план в другую сторону → сначала закрыть. Ревью 5.4.2: план ДОБОРА (src topup) к позиции
            # другой стороны (владелец перевернул руками, пока план ждал) — не вердикт переворота: снимается
            if self.plan and self.plan["side"] != pos["side"]:
                if self.plan.get("src") == "topup":
                    log.info("ИИ-пилот %s: план добора %s к позиции %s — не переворот, снят",
                             self.base, self.plan["side"], pos["side"])
                    self.plan = None
                else:
                    await self._close_all(price, "флип по новому вердикту",
                                          reanalyze=False)
                    return
            if not pos.get("guard_busy") and not crossed:
                self.state = "В_ПОЗИЦИИ"
                self.last_action = (f"в позиции {pos['side']} {pos['lots']} лот "
                                    f"@{pos['entry']:g}, плавающий {floating:+.0f}"
                                    + (f" — ⚠ трос на бирже не подтверждён ({pos.get('stop_err') or 'жду ответа биржи'}), "
                                       "держу виртуальный стоп" if unsettled else "")
                                    + (f" · {self._money_name()} думает о прибыли" if pos.get("profit_busy") else ""))
            # v5.4.1 «мысль о прибыли»: в плюсе ИИ у денег думает сам, не дожидаясь тейка (наследник);
            # не при пересечении троса/тейка, не пока идёт вопрос у троса/тейка, не при заявке/неурегулированном стопе
            if (not crossed and not take_hit and not pos.get("guard_busy") and not unsettled
                    and not self.pending and self._market_alive()):
                try:
                    self._profit_watch(price, pos)
                except Exception as e:                   # noqa: BLE001
                    log.info("ИИ-пилот %s: мысль о прибыли споткнулась: %s", self.base, str(e)[:80])
                if self.position is not pos:
                    return
            # добор после входа (v5.2): биржа даёт ещё лотов (округление, освободившаяся
            # маржа) — берём, пока даёт, не больше PYTHIA_TOPUP_MAX раз
            if (self._sized_by_broker and not self.plan and int(pos.get("topup_left") or 0) > 0
                    and now - _f(pos.get("last_fill_ts")) >= TOPUP_GAP_SEC and self._tick_n % 6 == 0
                    and self._market_alive() and not pos.get("guard_busy") and not crossed and not unsettled):
                await self._topup(price, book)
                if self.pending:
                    return

        # 3. засада: ждём имба-момент (только на живом рынке); при позиции той же стороны —
        #    это добор по приказу (сейчас / у уровня / на пробитии)
        if self.plan and self.state != "ПЕРЕАНАЛИЗ" and not self._reanalyzing and not unsettled \
                and (not self.position or self.plan["side"] == self.position["side"]):
            if time.time() < self.no_entry_until:
                self.last_action = "бэкофф после отказов биржи — жду"
            elif not self._market_alive():
                self.state = "ЗАСАДА" if (self.plan.get("entry") is not None and not self.position) \
                    else self.state
                self.last_action = ("рынок мёртв (стакан пуст/протух — ночь, "
                                    "аукцион?) — входов нет, жду живой стакан")
            elif self._at_limit(self.plan["side"], price):
                self.last_action = "цена у планки — вход в лок запрещён"
            elif time.time() - _f(self.plan.get("ts")) > PLAN_TTL_SEC:
                # ПРИКАЗ ПРОТУХ (пилот живёт весь день): утренняя засада к
                # обеду говорит о другом рынке — входим не по ней, а по
                # свежему разбору
                age_m = int((time.time() - _f(self.plan.get("ts"))) / 60)
                self.plan = None
                if not self.position:
                    self.state = "ЖДУ_ПЛАН"
                self.last_action = (f"приказ протух ({age_m} мин, предел "
                                    f"{int(PLAN_TTL_SEC/60)}) — прошу свежий "
                                    f"разбор, по старой цене не захожу")
                log.info("ИИ-пилот %s: %s", self.base, self.last_action)
                self._fire_reanalyze("приказ протух")
            else:
                inv = _f(self.plan.get("invalidation"))
                # v5.4.2: план «прорыв» до пробития уровня ещё не в рынке — классика «BUY stop 101, стоп 100.5 при цене
                # 100»: invalidation вступает только после пробития (откат / сейчас — как было)
                unbroken = self._breakout_unbroken(price)
                dead = not unbroken and ((self.plan["side"] == "long" and price <= inv)
                                         or (self.plan["side"] == "short" and price >= inv))
                if dead and self.position:
                    # добор при цене ЗА стопом — усиление мёртвой идеи, пока FLASH решает у троса:
                    # план снимаем, позицию ведут трос/триггер (v5.2, проверяющий)
                    self.plan = None
                    self.last_action = (f"добор отменён: цена {price:g} уже за invalidation "
                                        f"{inv:g} — позицию ведёт трос")
                    log.warning("ИИ-пилот %s: %s", self.base, self.last_action)
                elif dead:
                    # цена уже ЗА стопом — вход в мёртвую идею запрещён
                    self.plan = None
                    self.state = "ЖДУ_ПЛАН"
                    self.last_action = (f"цена {price:g} уже за invalidation "
                                        f"{inv:g} — идея мертва ДО входа, "
                                        "прошу свежий разбор")
                    log.warning("ИИ-пилот %s: %s", self.base, self.last_action)
                    self._fire_reanalyze("идея мертва до входа")
                else:
                    lvl = self.plan.get("entry")
                    if lvl is None:
                        # v5.4.1: перед КАЖДОЙ заявкой входа по плану — проверка у двери (наследник спрашивает
                        # PRO по живому рынку фоном и входит сам по его «ВОЙТИ»; база — вход сразу)
                        if await self._entry_gate(price, book):
                            await self._enter(price, book)
                    else:
                        lvl = _f(lvl)
                        if self._entry_ready(price, lvl):
                            if await self._entry_gate(price, book):
                                await self._enter(price, book)
                        elif not self.position:
                            self.state = "ЗАСАДА"
                            self.last_action = self._entry_wait_text(price, lvl)
                        else:
                            self.last_action = "добор: " + self._entry_wait_text(price, lvl)

        # 3б. отложенный пейсингом разбор: как только окно открылось —
        #     просим его сами (иначе пилот встал бы колом до перепроверки)
        if (self._reanalyze_pending and not self._reanalyzing
                and not self.position and not self.pending
                and time.time() - self._last_reanalyze_ts >= REANALYZE_GAP_SEC):
            self._fire_reanalyze(self._reanalyze_pending)

        # 4. сверка с биржей: всегда (не только при позиции — иначе слепые
        #    окна изоляции; находка веера) + свежие деньги/ГО
        if self._tick_n % RECONCILE_EVERY == 0:
            await self._reconcile(price)

        # 5. 30-минутная перепроверка — только на живом рынке и не во время
        #    переанализа (решение по замороженной цене — деньги на ветер)
        if (time.time() >= self.review_ts and not self._review_busy
                and self.state != "СТОП" and not self._reanalyzing and not unsettled):
            if not self._market_alive():
                self.review_ts = time.time() + 600     # рынок мёртв — отложили
                self.last_action = "перепроверка отложена: рынок мёртв"
            else:
                self.review_ts = time.time() + self._review_gap()
                self._spawn_background(self._review_bg(price))

    def _review_gap(self) -> float:
        """Шаг плановой перепроверки: REVIEW_SEC; v5.4.2 — последний приказ совета WAIT, а позиции, плана и заявки
        нет: не реже PYTHIA_WAIT_REVIEW_SEC (умолчание = REVIEW_SEC, 30 мин; вне рынка PRO будят и триггеры)."""
        if getattr(self, "_wait_order", False) and not (self.position or self.plan or self.pending):
            return wait_review_sec()
        return REVIEW_SEC

    def _breakout_unbroken(self, price: float) -> bool:
        """v5.4.2: план «прорыв» (kind от наследника; вход ЗА ценой по ходу сделки: BUY выше, SELL ниже), уровень
        которого цена ещё ни разу не пробила? Пока нет — идея не в рынке, и invalidation (стоп под уровнем пробоя)
        её не хоронит. Пробитие помечается в плане (crossed): откат обратно за invalidation после пробоя — идея
        мертва, как у любого плана. Не «прорыв» или нет уровня — False (проверка как была).
        Ревью 5.4.2: пометка привязана к уровню (plan["crossed"] = пробитый уровень): дверь (ЖДАТЬ с новым уровнем) или
        перепроверка сменили уровень — пробитие старого уровня новый не хоронит, пока цена не пройдёт новый.
        Старая пометка True (state до правки) — пробитие текущего уровня, как было."""
        plan = self.plan or {}
        lvl = plan.get("entry")
        if plan.get("kind") != "прорыв" or lvl is None or not price:
            return False
        lvl = _f(lvl)
        if (price >= lvl) if plan.get("side") == "long" else (price <= lvl):
            plan["crossed"] = lvl
        c = plan.get("crossed")
        if c is None or c is False:
            return True
        if c is True:
            return False
        return abs(_f(c) - lvl) > max(1e-9, abs(lvl) * 1e-9)

    # ── момент входа по уровню (наследник добавляет свои виды входа, напр. прорыв) ─
    def _entry_ready(self, price: float, lvl: float) -> bool:
        """Имба-момент засады: цена в ARM_TICKS шагах от уровня (или уже за ним —
        лучше уровня). Наследник переопределяет для других видов входа."""
        arm = (ARM_TICKS * self.tick_size if self.tick_size > 0
               else price * ARM_FRAC)
        return ((price <= lvl + arm) if self.plan["side"] == "long"
                else (price >= lvl - arm))

    def _entry_wait_text(self, price: float, lvl: float) -> str:
        # v5.4.3: взведённый вход — решение принято, не «жду»
        return (f"вход взведён: засада {self.plan['side']} @{lvl:g} (цена {price:g}, "
                "вход в имба-момент)")

    # ── v5.4.1: хуки трезвого пилота (база — как раньше; наследник MissionPilot спрашивает PRO) ─────────
    async def _entry_gate(self, price: float, book: dict | None) -> bool:
        """Проверка входа у двери: зовётся в tick §3 перед каждой заявкой входа по плану (сейчас / откат /
        прорыв / добор по решению PRO / переворот; авто-добор _topup — нет). База: True — вход сразу.
        MissionPilot при PYTHIA_ENTRY_CHECK спрашивает PRO фоном и возвращает False: входит он сам по «ВОЙТИ»."""
        return True

    def _profit_watch(self, price: float, pos: dict) -> None:
        """Мысль о прибыли: зовётся в tick §2 в позиции после проверок троса/тейка (не при их пересечении, не
        пока идёт вопрос у троса/тейка, не при заявке). База: ничего. MissionPilot в плюсе спрашивает PRO."""
        return None

    # ── вход: агрессивная лимитка в спред (рыночные входы запрещены) ────────
    def _aggr_price(self, side: str, price: float, book: dict | None) -> float:
        bb = _f((book or {}).get("best_bid"))
        ba = _f((book or {}).get("best_ask"))
        tick = self.tick_size if self.tick_size > 0 else max(price * 1e-5, 1e-6)
        if bb > 0 and ba > bb:
            if side == "long":
                px = min(bb + tick, ba - tick)
                px = px if px > bb else bb
            else:
                px = max(ba - tick, bb + tick)
                px = px if px < ba else ba
        else:
            px = price
        return self._snap(px)

    async def _post_owned_order(self, order: dict, direction: str,
                                price: float | None, tag: str) -> dict:
        """Persist a request identity before I/O; retain it if the response is lost."""
        request_id = str(uuid.uuid4())
        order.update(order_id=request_id, request_id=request_id, id_type="request", request_ts=time.time())
        unpersisted = False
        if not self._save_state():
            if not (self.panic_flag and tag == "aip-close"):
                return {"ok": False, "note": "заявка не отправлена: состояние не удалось сохранить"}
            # ПАНИКА: закрыть важнее, чем сохранить намерение (W4, п. 6). Постоянная ошибка диска не имеет права
            # запереть паническое закрытие; при потере ответа биржи заявку сверит владелец по UUID из лога
            unpersisted = True
            order["unpersisted"] = True
            log.error("ИИ-пилот %s: state-файл не записался, но это закрытие по ПАНИКЕ — отправляю БЕЗ персиста "
                      "(UUID заявки %s: при потере ответа сверь её у брокера)", self.base, request_id)
        kwargs = {"request_id": request_id} if isinstance(self.broker, trader_broker.Broker) else {}
        result = await self.broker.place(self.figi, direction, order["lots"], price=price, tag=tag, **kwargs)
        if unpersisted:
            result["persist_note"] = "закрытие по ПАНИКЕ отправлено без сохранения состояния (state-файл не пишется)"
        if result.get("ok") or result.get("uncertain"):
            order["order_id"] = result.get("order_id") or request_id
            order["id_type"] = result.get("id_type") or ("request" if result.get("uncertain") else "exchange")
            status = str(result.get("status") or "")
            known_full = result.get("filled") or status == "FILL" or status.endswith("_FILL")
            order["seen_exec_lots"] = (int(order["lots"]) if known_full else
                                      max(0, min(int(order["lots"]), int(_f(result.get("exec_lots"))))))
            self._save_state()
        return result

    @staticmethod
    def _request_vanished(order: dict, *results: dict) -> bool:
        """Фантом (№2): заявка известна только по request-UUID, а биржа отвечает «не найдена» (not_found) дольше
        ORDER_REQUEST_MAX_AGE_SEC — запрос до биржи не дошёл; терминал с 0 исполнений. Раньше срока — ещё неизвестно
        (репликация у брокера), с биржевым id — никогда (биржа его выдала, значит заявка была)."""
        if order.get("id_type") == "exchange":
            return False
        now = time.time()
        if not any(isinstance(r, dict) and r.get("not_found") for r in results):
            order.pop("not_found_since", None)   # любой другой ответ (NEW/5xx/…) обнуляет наблюдение «не найдена»
            return False
        # V (2-й проход, e): один транзиентный 404 после долгих 5xx на старом запросе — ещё не фантом: «не найдена»
        # обязана держаться ORDER_REQUEST_MAX_AGE_SEC подряд (not_found_since живёт в заявке и переживает рестарт)
        since = _f(order.get("not_found_since"))
        if since <= 0:
            order["not_found_since"] = since = now
        born = _f(order.get("request_ts")) or _f(order.get("ts"))
        return born > 0 and now - born > ORDER_REQUEST_MAX_AGE_SEC and now - since >= ORDER_REQUEST_MAX_AGE_SEC

    async def _owned_order_state(self, order: dict) -> dict:
        kwargs = ({"request_id": True} if order.get("id_type") == "request"
                  and isinstance(self.broker, trader_broker.Broker) else {})
        state = await self.broker.order_state(order["order_id"], **kwargs)
        if state.get("ok") and state.get("order_id") and order.get("id_type") == "request":
            order["order_id"] = state["order_id"]
            order["id_type"] = "exchange"
            self._save_state()
        return state

    async def _cancel_owned_order(self, order: dict) -> dict:
        kwargs = ({"request_id": True} if order.get("id_type") == "request"
                  and isinstance(self.broker, trader_broker.Broker) else {})
        return await self.broker.cancel(order["order_id"], **kwargs)

    async def _enter(self, price: float, book: dict | None,
                     attempts: int = 0) -> None:
        if not self.plan:
            return
        side = self.plan["side"]
        await self._refresh_max(price)         # сколько даёт биржа прямо сейчас (с маржой)
        lots = self.max_lots(price, side)
        topup = bool(self.position and self.position.get("side") == side)
        if lots <= 0:
            if topup:
                self.plan = None
                self.last_action = ("добор невозможен: биржа больше не даёт лотов — "
                                    "позиция уже на максимуме")
                log.info("ИИ-пилот %s: %s", self.base, self.last_action)
                return
            self.last_action = ("депозит не тянет ни лота — входа нет "
                                "(честно, без пыли)")
            self.plan = None
            self.state = "ЖДУ_ПЛАН"
            log.warning("ИИ-пилот %s: %s", self.base, self.last_action)
            return
        px = self._aggr_price(side, price, book)
        self.pending = {"side": side, "lots": lots,
                        "price": px, "ts": time.time(), "attempts": attempts,
                        "take": self.plan.get("take"), "invalidation": self.plan.get("invalidation"),
                        "why": self.plan.get("why", ""), "topup": topup}
        r = await self._post_owned_order(self.pending, BUY if side == "long" else SELL, px, "aip-entry")
        if not r.get("ok") and not r.get("uncertain"):
            self.pending = None
            self._save_state()
            self._entry_fail += 1
            self.no_entry_until = time.time() + min(120.0, 2.0 * 2 ** self._entry_fail)
            self._mx = None                    # биржа не приняла — пересчитать «сколько даёт»
            self.last_action = (f"{'добор' if topup else 'вход'} {'BUY' if side == 'long' else 'SELL'} {lots} лот "
                                f"отбит: {r.get('error') or r.get('note')} — бэкофф, попытка {self._entry_fail}")
            if self._entry_fail >= ENTRY_FAIL_MAX:
                self.plan = None
                self.state = "ЖДУ_ПЛАН"
                self.last_action = ("биржа стабильно отбивает вход — план "
                                    "сброшен, прошу свежий разбор")
                self._fire_reanalyze("вход невозможен: серия отказов биржи")
            return
        # заявка несёт СВОИ уровни (снимок плана): новый план поверх летящей
        # заявки больше не приклеивает чужие тейк/стоп (находка веера)
        if not topup:
            self.state = "ВХОЖУ"
        self.last_action = (f"{'добор: ' if topup else ''}бью агрессивной лимиткой: {side} {lots} лот @{px:g}"
                            + (" (размер дала биржа)" if self._sized_by_broker else "")
                            + (f" (перевзвод №{attempts})" if attempts else ""))
        log.info("ИИ-пилот %s: %s", self.base, self.last_action)

    async def _topup(self, price: float, book: dict | None) -> None:
        """Добор после входа: биржа даёт ещё лотов (округление, освободившаяся маржа) —
        агрессивная лимитка на всё, что даёт; не больше PYTHIA_TOPUP_MAX раз на вход."""
        pos = self.position
        if not pos:
            return
        pos["topup_left"] = int(pos.get("topup_left") or 0) - 1
        await self._refresh_max(price, force=True)
        n = self.max_lots(price, pos["side"])
        if n <= 0:
            pos["topup_left"] = 0
            return
        px = self._aggr_price(pos["side"], price, book)
        self.pending = {"side": pos["side"], "lots": n, "price": px,
                        "ts": time.time(), "attempts": 0, "take": pos.get("take"),
                        "invalidation": pos.get("invalidation"), "why": "добор до максимума", "topup": True}
        r = await self._post_owned_order(self.pending, BUY if pos["side"] == "long" else SELL, px, "aip-topup")
        if not r.get("ok") and not r.get("uncertain"):
            self.pending = None
            self._save_state()
            pos["topup_left"] = 0
            self._mx = None
            self.last_action = f"добор {n} лот отбит: {r.get('error') or r.get('note')}"
            log.info("ИИ-пилот %s: %s", self.base, self.last_action)
            return
        self.last_action = f"добор до максимума: ещё {n} лот @{px:g} (биржа даёт)"
        log.info("ИИ-пилот %s: %s", self.base, self.last_action)

    async def _order_part(self, po: dict) -> tuple[int, bool]:
        """(исполнено лотов, полностью?). Опрос судьбы заявки."""
        st = await self._owned_order_state(po)
        if st.get("filled"):
            return int(po["lots"]), True
        try:
            part = max(0, min(int(po["lots"]), int(st.get("exec_lots") or 0)))
        except (TypeError, ValueError, OverflowError):
            part = 0
        part = max(part, int(po.get("seen_exec_lots") or 0))
        po["seen_exec_lots"] = part
        self._save_state()
        return part, part >= int(po["lots"])

    async def _cancel_pending(self) -> tuple[int, dict | None]:
        async with self._pending_lock:
            return await self._cancel_pending_once()

    async def _cancel_pending_once(self) -> tuple[int, dict | None]:
        """Снять заявку входа; вернуть (налитые лоты, снимок заявки).
        После cancel — ПОВТОРНЫЙ опрос: между снятием и опросом могли долить
        (гонка «частичка после cancel» — находка веера)."""
        po = self.pending
        if not po:
            self._cancel_entry = False
            return 0, None
        part, full = await self._order_part(po)
        if full:
            self.pending = None
            self._cancel_entry = False
            return int(po["lots"]), po
        cancel = await self._cancel_owned_order(po)
        st = await self._owned_order_state(po)
        full2 = bool(st.get("filled"))
        vanished = self._request_vanished(po, st, cancel)
        terminal = str(st.get("status") or "").endswith(("REJECTED", "CANCELLED")) or vanished
        try:
            part2 = max(0, min(int(po["lots"]), int(st.get("exec_lots") or 0)))
        except (TypeError, ValueError, OverflowError):
            part2 = 0
        po["seen_exec_lots"] = max(part, part2)
        self._save_state()
        if vanished:
            log.error("ИИ-пилот %s: заявка входа %s не найдена биржей дольше %d с — до биржи не дошла, снимаю из учёта "
                      "(сверь по UUID у брокера)", self.base, po.get("request_id") or po.get("order_id"),
                      int(ORDER_REQUEST_MAX_AGE_SEC))
        # Even a successful cancel does not establish the final filled quantity
        # when the following state request failed. Retain ownership and retry.
        fresh_not_found = bool((st.get("not_found") or cancel.get("not_found")) and not vanished)
        if not full2 and not terminal and (not cancel.get("ok") or not st.get("ok") or fresh_not_found):
            self._cancel_entry = True
            self.last_action = (f"биржа не находит заявку по UUID — жду: снимаю из учёта, только если «не найдена» держится "
                                f"{int(ORDER_REQUEST_MAX_AGE_SEC)} с подряд у запроса старше того же срока (репликация у брокера?)"
                                if fresh_not_found
                                else "судьба заявки после отмены неизвестна — сохраняю её и повторяю опрос")
            return 0, None
        self.pending = None
        self._cancel_entry = False
        if not part and not part2 and not full2:
            self._save_state()
        return (int(po["lots"]) if full2 else max(part, part2)), po

    async def _pending_tick(self, price: float, book: dict | None) -> None:
        async with self._pending_lock:
            if self.pending:
                await self._pending_tick_once(price, book)

    async def _pending_tick_once(self, price: float, book: dict | None) -> None:
        po = self.pending
        # новый план/паника потребовали снять заявку — частичку в работу
        if self._cancel_entry:
            part, po2 = await self._cancel_pending_once()
            if part > 0 and po2:
                await self._absorb_fill(part, po2)
            elif self._close_pending and not self.position and not self.pending:
                self._close_pending = None     # CLOSE снял заявку, исполнения не было — закрывать нечего
            return
        st = await self._owned_order_state(po)
        s = str(st.get("status") or "")
        if st.get("filled"):
            self.pending = None
            await self._absorb_fill(po["lots"], po)
            return
        vanished = self._request_vanished(po, st)            # фантом: до биржи не дошла — как отказ, без исполнений;
        if vanished:                                         # свежий «не найдена» — ещё неизвестность (репликация), ждём
            log.error("ИИ-пилот %s: заявка %s не найдена биржей дольше %d с — считаю не дошедшей (сверь по UUID)",
                      self.base, po.get("request_id") or po.get("order_id"), int(ORDER_REQUEST_MAX_AGE_SEC))
        s = "NOT_FOUND" if vanished else ("" if st.get("not_found") else s)
        part = 0
        ex = st.get("exec_lots")
        if ex is not None:
            try:
                part = max(0, min(int(po["lots"]), int(ex)))
            except (TypeError, ValueError, OverflowError):
                part = 0
        part = max(part, int(po.get("seen_exec_lots") or 0))
        po["seen_exec_lots"] = part
        self._save_state()
        if part >= int(po["lots"]):
            self.pending = None
            await self._absorb_fill(part, po)
            return
        if s.endswith("REJECTED") or s.endswith("CANCELLED") or vanished:
            self.pending = None
            if part > 0:
                await self._absorb_fill(part, po)
                return
            self._save_state()
            # отказ биржи: бэкофф и счётчик (штурм каждые 1.5с убит)
            self._entry_fail += 1
            self.no_entry_until = time.time() + min(120.0, 2.0 * 2 ** self._entry_fail)
            if self._entry_fail >= ENTRY_FAIL_MAX:
                self.plan = None
                self.state = "ЖДУ_ПЛАН"
                self.last_action = (f"биржа отбивает заявки ({s}) — план "
                                    "сброшен, прошу свежий разбор")
                self._fire_reanalyze("серия отказов биржи")
                return
            self.state = "ЗАСАДА" if (self.plan or {}).get("entry") is not None \
                else "ЖДУ_ПЛАН"
            self.last_action = f"заявку отбила биржа ({s}) — бэкофф и перевзвод"
            return
        ran = abs(price - po["price"]) / po["price"] if po["price"] else 0.0
        if time.time() - po["ts"] > ENTRY_TTL_SEC or ran > RUN_FRAC:
            part2, cancelled = await self._cancel_pending_once()
            if cancelled is None:
                return
            part = max(part, part2)
            if part > 0:                       # частичку берём в работу
                await self._absorb_fill(part, po)
                return
            att = po["attempts"] + 1
            if att <= MAX_REPRICINGS:
                await self._enter(price, book, attempts=att)
                return
            # финальная эскалация «мы точно заходим»: МАРКЕТ-ЭЙБЛ ЛИМИТ по
            # best противоположной стороны (лимитка, не рыночный)
            bb = _f((book or {}).get("best_bid"))
            ba = _f((book or {}).get("best_ask"))
            cross = ba if po["side"] == "long" else bb
            if cross > 0:
                lots = self.max_lots(price, po["side"])
                if lots > 0:
                    cross = self._snap(cross)
                    self.pending = {"side": po["side"], "lots": lots,
                                        "price": cross, "ts": time.time(),
                                        "attempts": att,
                                        "take": po.get("take"),
                                        "invalidation": po.get("invalidation"),
                                        "why": po.get("why", "")}
                    r = await self._post_owned_order(
                        self.pending, BUY if po["side"] == "long" else SELL, cross, "aip-cross")
                    if r.get("ok") or r.get("uncertain"):
                        self.last_action = (f"эскалация: лимит по best "
                                            f"{cross:g} — точно заходим")
                        return
                    self.pending = None
                    self._save_state()
            self.state = "ЗАСАДА" if (self.plan or {}).get("entry") is not None \
                else "ЖДУ_ПЛАН"
            self.last_action = ("ликвидности нет даже по best — отступил, "
                                "вход взведён заново (имба-момент)")

    async def _absorb_fill(self, lots: int, po: dict,
                           place_stop: bool = True) -> None:
        """Исполненные лоты заявки → управляемая позиция. Уровни — ИЗ ЗАЯВКИ
        (снимок плана на момент выставления), не из текущего plan."""
        side, px = po["side"], po["price"]
        if self.position and self.position["side"] == side:
            p = self.position                  # доусреднение частичек / добор
            tot = p["lots"] + int(lots)
            p["entry"] = (p["entry"] * p["lots"] + px * int(lots)) / tot
            p["lots"] = tot
            p["restop"] = True                 # трос → на суммарный объём
            if not po.get("topup"):            # свежий приказ: его уровни главнее временных
                self._set_levels(p, po.get("take"), po.get("invalidation"))
                p.pop("levels_placeholder", None)
                p["topup_left"] = int(_setting("PYTHIA_TOPUP_MAX", 2))
            p["last_fill_ts"] = time.time()
        else:
            self.position = {"side": side, "entry": float(px), "lots": int(lots),
                             "take": None, "invalidation": None,
                             "opened_ts": time.time(), "stop_id": None,
                             "floating": 0.0, "last_fill_ts": time.time(),
                             "topup_left": int(_setting("PYTHIA_TOPUP_MAX", 2)) if self._sized_by_broker else 0}
            self._set_levels(self.position, po.get("take"), po.get("invalidation"))
        # план потреблён, ТОЛЬКО если это его сторона (частичка старой заявки
        # при новом противоположном плане не должна съесть новый план)
        if self.plan and self.plan["side"] == side:
            self.plan = None
        self._entry_fail = 0
        if not self.position:
            return
        pos = self.position
        self._save_state()  # persist the fill before waiting on stop placement
        if place_stop:
            await self._replace_stop(pos)
        if not self.plan:
            self.state = "В_ПОЗИЦИИ"
        self.review_ts = time.time() + REVIEW_SEC
        self._mx = None                        # лоты заняты — «сколько даёт биржа» спросим заново
        self._save_state()
        trig = (f" (триггер {self._money_name()} @{pos.get('invalidation')})"
                if self._soft_stop_on() and pos.get("hard_stop") else "")
        if not self._exchange_stop_on() and not pos.get("stop_id") and not pos.get("stop_request"):
            troos = f"трос в программе @{self._stop_price(pos):g}{trig}, стопов на бирже нет"   # v5.4.1
        else:
            troos = ((f"трос @{self._stop_price(pos):g}" + trig)
                     if pos.get("stop_id") else ("⚠ ПОДТВЕРЖДЕНИЕ ТРОСА ОЖИДАЕТСЯ" if pos.get("stop_request") else
                                                  "⚠ ТРОС НЕ ВСТАЛ — держу виртуальный стоп"))
        self.last_action = (f"{'ДОБРАЛ' if po.get('topup') else 'ВОШЁЛ'}: {side} {pos['lots']} лот @{pos['entry']:g}, "
                            f"{troos}, тейк {pos.get('take')}")
        log.info("ИИ-пилот %s: %s", self.base, self.last_action)

    async def _replace_stop(self, pos: dict) -> None:
        async with self._stop_lock:
            if self.position is not pos or pos.get("exit_order") or pos.get("close_fail"):
                return
            await self._replace_stop_once(pos)

    async def _replace_stop_once(self, pos: dict) -> None:
        """Биржевой трос: перевыставить на актуальные объём/уровень.
        Отказ не глотается: stop_id=None и честное предупреждение.
        v5.4.1 «стопы только в программе» (PYTHIA_EXCHANGE_STOP=0, умолчание): на биржу ничего не ставим;
        старый стоп (state-файл прошлой версии / флаг переключили в бою) снимается ОДИН раз с тем же
        подтверждением, что и при переносе (CancelStopOrder, иначе список стопов биржи — _stop_gone);
        висящий запрос стопа сначала урегулируется (найден → снимается, нет — закрыт). Трос дальше виртуальный."""
        pos.pop("restop", None)
        exchange = self._exchange_stop_on()
        if pos.get("stop_request"):
            if exchange:
                settled = await self._settle_stop_request(pos)
            else:
                # V 5.4.1: стопы только в программе — тот же UUID НЕ повторяем (повтор = PostStopOrder при выключенном
                # флаге): судьбу висящего запроса решает только список стопов биржи; найден → снимается ниже, нет → закрыт
                settled = await self._resolve_stop_request(
                    pos, pos["stop_request"], "стопы только в программе (PYTHIA_EXCHANGE_STOP=0) — запрос стопа не повторяю",
                    backoff=0.0)
                if settled.get("absent") and self.position is pos:
                    self.last_action = ("запрос стопа закрыт: списка стопов биржи наш трос не содержит — стопы только в "
                                        "программе, трос виртуальный")
            if self.position is not pos or pos.get("stop_request"):
                return                         # судьба неясна (или позиция сменилась) — ждём честно
            if exchange and (not settled.get("absent") or time.time() < _f(pos.get("restop_after"))):
                return                         # стоп принят/найден или бэкофф после отказа; absent → ниже
            pos.pop("restop", None)            # запрос закрыт: стопа на бирже нет — ставим заново (или, без флага,
            pos.pop("restop_after", None)      # снимаем найденный ниже)
        verify = bool(pos.pop("verify_lots", False))   # прошлый раз остаток по счёту не узнали — сверить до троса
        if pos.get("stop_id"):
            cancelled = await self.broker.cancel_stop(pos["stop_id"])
            if not cancelled.get("ok"):
                # An old stop may still be active. A second one can reverse the
                # account after both trigger, so keep the identifier until known.
                # W4 (A7): «не подтверждена» ≠ «жив»: список стопов биржи решает — стопа там нет (исполнен /
                # снят владельцем / истёк) → отмена подтверждена; остаток позиции сверяем со счётом ДО нового троса
                gone, err = await self._stop_gone(pos["stop_id"])
                if not gone:
                    pos["stop_err"] = str(cancelled.get("error") or cancelled.get("note") or "отмена стопа не подтверждена")[:100]
                    if err:
                        pos["stop_err"] = (pos["stop_err"] + f"; список стопов: {err}")[:160]
                    pos["restop"] = True
                    pos["restop_after"] = time.time() + STOP_RETRY_SEC
                    self._save_state()
                    return
                log.warning("ИИ-пилот %s: стопа %s на бирже уже нет — отмена подтверждена списком стопов",
                            self.base, pos["stop_id"])
                pos["stop_id"] = None
                self._save_state()
                verify = True
            else:
                if not exchange:
                    log.warning("ИИ-пилот %s: стоп-заявка %s снята с биржи — стопы только в программе "
                                "(PYTHIA_EXCHANGE_STOP=0), трос виртуальный @%s", self.base, pos["stop_id"],
                                f"{self._stop_price(pos):g}")
                pos["stop_id"] = None
        if verify:
            # стопа на бирже нет: он мог ИСПОЛНИТЬСЯ — новый трос на пустом счёте открыл бы обратную позицию.
            # Остаток — по счёту; счёт не отвечает → трос откладывается (verify_lots помнит, что сверка нужна)
            price = self.prices[-1] if self.prices else _f(pos.get("entry"))
            real = await self._real_own_lots(pos)
            if real is None:
                pos["verify_lots"] = True
                pos["stop_err"] = "стопа на бирже нет, остаток позиции на счёте не узнать — трос после сверки"
                pos["restop"] = True
                pos["restop_after"] = time.time() + STOP_RETRY_SEC
                self._save_state()
                return
            if real != pos["lots"]:            # стоп исполнился (или владелец продал): счёт — истина
                await self._reconcile(price)
                if self.position is not pos or pos.get("exit_order"):
                    return
        if self.position is not pos or pos.get("exit_order") or pos.get("close_fail"):
            return
        if pos.get("hard_stop") is None and self._soft_stop_on():
            pos["hard_stop"] = self._hard_of(pos)
        if not exchange:
            # стопы только в программе: на бирже пусто, трос виртуальный — tick закроет за ним по рынку
            pos.pop("stop_request", None)
            pos.pop("stop_err", None)
            pos.pop("restop_after", None)
            self._save_state()
            return
        inv = self._stop_price(pos)
        if inv <= 0:
            return
        pos["stop_request"] = {"request_id": str(uuid.uuid4()), "lots": int(pos["lots"]),
                               "direction": SELL if pos["side"] == "long" else BUY,
                               "price": self._snap(inv), "uncertain": False, "created_ts": time.time()}
        await self._settle_stop_request(pos)

    # ── список стопов биржи как разрешитель (W4, п. 1): «неизвестно» не значит «навсегда» ─────
    @staticmethod
    def _money(v) -> float:
        """Quotation/MoneyValue {units, nano} или число → float."""
        if isinstance(v, dict):
            return _f(v.get("units")) + _f(v.get("nano")) / 1e9
        return _f(v)

    @staticmethod
    def _ts_of(v) -> float | None:
        """createDate биржи (RFC3339) или число → epoch; не разобрали → None."""
        if v is None or v == "":
            return None
        if isinstance(v, (int, float)):
            return float(v)
        try:
            from datetime import datetime
            s = str(v).strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            if "." in s:                                     # наносекунды биржи → микросекунды Python
                head, tail = s.split(".", 1)
                cut = max(tail.find("+"), tail.find("-"))    # смещение зоны (дата уже в head)
                digits, tz = (tail[:cut], tail[cut:]) if cut >= 0 else (tail, "")
                s = f"{head}.{digits[:6]}{tz}"
            return datetime.fromisoformat(s).timestamp()
        except Exception:                                    # noqa: BLE001
            return None

    @staticmethod
    def _stop_active(so: dict) -> bool:
        st = str(so.get("status") or "")
        return not st or st.endswith(("_ACTIVE", "_UNSPECIFIED"))

    async def _list_figi_stops(self) -> tuple[list | None, str | None]:
        """Активные стопы по инструменту из списка биржи (GetStopOrders, strict).
        (список, None) — листинг успешен; (None, причина) — брокер не отдаёт список / запрос упал."""
        fn = getattr(self.broker, "stop_orders", None)
        if fn is None:
            return None, "брокер не отдаёт список стопов"
        try:
            stops = await fn(strict=True)
        except Exception as e:                               # noqa: BLE001
            return None, trader_broker._err_text(e)
        mine = [so for so in (stops or []) if isinstance(so, dict)
                and (so.get("figi") == self.figi or (so.get("instrumentUid") and so.get("instrumentUid") == self.figi))
                and self._stop_active(so)]
        return mine, None

    async def _find_stop_request(self, request: dict) -> tuple[str | None, str | None]:
        """Есть ли на бирже стоп по нашему запросу: тот же UUID (orderRequestId, если биржа отдаёт), иначе то же
        направление / объём / цена, созданный не раньше запроса (допуск STOP_LISTING_SKEW_SEC).
        (stopOrderId, None) — найден; (None, None) — листинг успешен, стопа нет; (None, причина) — не узнать."""
        stops, err = await self._list_figi_stops()
        if stops is None:
            return None, err
        want_dir = trader_broker.STOP_SELL if request.get("direction") == SELL else trader_broker.STOP_BUY
        want_px = _f(request.get("price"))
        tol = max(_f(self.tick_size) * 0.5, abs(want_px) * 1e-9)
        created_req = _f(request.get("created_ts"))
        for so in stops:
            sid = str(so.get("stopOrderId") or "")
            if not sid:
                continue
            rid = so.get("orderRequestId")
            if rid and rid == request.get("request_id"):
                return sid, None
            if str(so.get("direction") or "") != want_dir:
                continue
            if int(_f(so.get("lotsRequested", so.get("lots")))) != int(request.get("lots") or 0):
                continue
            if abs(self._money(so.get("stopPrice", so.get("stop"))) - want_px) > tol:
                continue
            created = self._ts_of(so.get("createDate"))
            if created is not None and created_req > 0 and created < created_req - STOP_LISTING_SKEW_SEC:
                continue
            return sid, None
        return None, None

    async def _stop_gone(self, stop_id: str) -> tuple[bool | None, str | None]:
        """Стоп stop_id в активных стопах биржи? (True — его там НЕТ: отмена подтверждена; False — жив;
        None — не узнать, причина во втором элементе)."""
        stops, err = await self._list_figi_stops()
        if stops is None:
            return None, err
        return all(str(so.get("stopOrderId") or "") != str(stop_id) for so in stops), None

    def _adopt_stop(self, pos: dict, request: dict, stop_id) -> None:
        pos["stop_id"] = stop_id
        pos.pop("stop_request", None)
        pos.pop("stop_err", None)
        pos.pop("restop_after", None)
        if request.get("lots") != pos["lots"] or request.get("price") != self._snap(self._stop_price(pos)):
            pos["restop"] = True  # settle the old intent before applying a newer level/quantity

    async def _resolve_stop_request(self, pos: dict, request: dict, reason: str, backoff: float) -> dict:
        """Запрос стопа, который нельзя повторять тем же UUID (старше STOP_REQUEST_MAX_AGE_SEC) или который биржа
        отбила на повторе, — разрешается по списку стопов: найден → это наш трос (stopOrderId принят);
        листинг успешен, стопа нет → запрос биржей не принят, закрыт, трос ставится заново НОВЫМ UUID;
        листинг упал → ждать дальше, честно назвав причину (не вечно: следующая попытка через STOP_RETRY_SEC)."""
        found, err = await self._find_stop_request(request)
        if found:
            self._adopt_stop(pos, request, found)
            self.last_action = f"трос подтверждён списком стопов биржи ({found}): {reason}"
            log.warning("ИИ-пилот %s: %s", self.base, self.last_action)
            self._save_state()
            return {"ok": True, "stop_order_id": found, "listed": True}
        if err is None:
            pos.pop("stop_request", None)
            pos["stop_id"] = None
            pos["stop_err"] = f"{reason} — стопа на бирже нет, запрос закрыт"[:100]
            pos["restop"] = True
            pos["restop_after"] = time.time() + backoff
            self.last_action = (f"{reason}; списка стопов биржи наш трос не содержит — запрос закрыт, "
                                + ("ставлю трос заново новым UUID" if backoff <= 0 else
                                   f"ставлю заново новым UUID через {int(backoff)} с") + " (виртуальный стоп держит)")
            log.warning("ИИ-пилот %s: %s", self.base, self.last_action)
            self._save_state()
            return {"ok": True, "absent": True, "note": self.last_action}
        self.last_action = (f"статус прежнего стопа неизвестен — повтор заблокирован ({reason}), список стопов биржи "
                            f"недоступен: {err}; сверю снова через {int(STOP_RETRY_SEC)} с (виртуальный стоп держит)")
        pos["stop_err"] = f"список стопов недоступен: {err}"[:100]
        pos["restop"] = True
        pos["restop_after"] = time.time() + STOP_RETRY_SEC
        log.warning("ИИ-пилот %s: %s", self.base, self.last_action)
        self._save_state()
        return {"ok": False, "uncertain": True, "note": self.last_action}

    async def _sweep_figi_stops(self, pos: dict) -> dict:
        """ПАНИКА (W4, п. 3): снять ВСЕ активные стопы по инструменту через список биржи. ok=True — список получен
        и в нём не осталось стопов по figi (что было — снято, что не снялось — уже не активно)."""
        stops, err = await self._list_figi_stops()
        if stops is None:
            return {"ok": False, "error": err}
        cancelled, failed = 0, []
        for so in stops:
            sid = str(so.get("stopOrderId") or "")
            r = await self.broker.cancel_stop(sid) if sid else {"ok": False}
            if r.get("ok"):
                cancelled += 1
            else:
                failed.append(sid)
        if failed:                                           # не снялись: живы ли ещё? (исполнены/сняты — не помеха)
            stops2, err2 = await self._list_figi_stops()
            if stops2 is None:
                return {"ok": False, "cancelled": cancelled, "error": f"стопы {', '.join(failed)} не сняты; список: {err2}"}
            alive = [str(so.get("stopOrderId") or "") for so in stops2]
            if alive:
                return {"ok": False, "cancelled": cancelled, "error": f"стопы {', '.join(alive)} на бирже живы, снять не удалось"}
        pos["stop_id"] = None
        pos.pop("stop_request", None)
        pos.pop("stop_err", None)
        self._save_state()
        log.warning("ИИ-пилот %s: ПАНИКА — по %s снято стопов: %d (список биржи чист)", self.base, self.figi, cancelled)
        return {"ok": True, "cancelled": cancelled}

    async def _verify_remainder(self, pos: dict, est_px: float, why: str, reanalyze: bool, *,
                                at_stop: bool, strict: bool) -> str:
        """Перед рыночным закрытием, когда трос мог исполниться (стопа на бирже уже нет) или прошлая заявка
        выхода кончилась без исполнения, сверить остаток со счётом — иначе второй ордер = голый разворот.
        «closed» — на счёте ноль, позиция закрыта по учёту; «proceed» — закрывать остаток; «defer» — счёт
        не отвечает (strict: не рискуем)."""
        real = await self._real_own_lots(pos)
        if real is None:
            if not strict or self.panic_force:
                return "proceed"
            # №4: сверка обязана дожить до следующего закрытия — стоп уже снят из учёта (gone), и без флага следующий
            # тик отправил бы рыночную заявку на все лоты вслепую (счёт пуст → голый разворот)
            pos["exit_check"] = True
            pos["exit_check_strict"] = True
            self.last_action = "остаток позиции на счёте не узнать (портфель не отвечает) — закрытие отложено до следующего тика"
            self._save_state()
            return "defer"
        if real < pos["lots"]:
            cut = pos["lots"] - real
            px = (self._stop_price(pos) if at_stop else 0.0) or est_px
            pnl = self._realize(pos, cut, px, f"{why}: {cut} лот ушли со счёта без моей заявки (трос биржи / владелец)",
                                risk=False)    # кусок выхода: killswitch увидит итог круга (_finish_closed)
            pos["exit_pnl_total"] = _f(pos.get("exit_pnl_total")) + pnl
            pos["lots"] = real
            if real == 0:
                self._finish_closed(pos, why, reanalyze)
                return "closed"
            self._save_state()
        return "proceed"

    async def _settle_stop_request(self, pos: dict) -> dict:
        """Replay only the same documented idempotency UUID and immutable stop body.
        W4: запрос не висит вечно — старый (> STOP_REQUEST_MAX_AGE_SEC) и отбитый на повторе разрешаются по
        списку стопов биржи (_resolve_stop_request). Возврат: {"ok": True, "stop_order_id"} — трос принят/найден;
        {"ok": True, "absent": True} — запрос закрыт, стопа на бирже нет (ставить заново новым UUID);
        {"ok": False, ...} — неясно, ждать."""
        request = pos["stop_request"]
        was_uncertain = bool(request.get("uncertain"))
        age = time.time() - _f(request.get("created_ts"))
        # The stop API documents idempotency but does not promise indefinite
        # key retention. Never replay an old/undated request after a long outage.
        if was_uncertain and (not request.get("created_ts") or age < 0 or age > STOP_REQUEST_MAX_AGE_SEC):
            return await self._resolve_stop_request(
                pos, request, f"запросу стопа {int(age // 60) if request.get('created_ts') and age > 0 else '?'} мин "
                              f"(> {int(STOP_REQUEST_MAX_AGE_SEC // 60)}) — тот же UUID не повторяю", backoff=0.0)
        request["uncertain"] = True  # cancellation/restart during the following I/O is ambiguous
        if not self._save_state():
            pos["restop"] = True
            pos["restop_after"] = time.time() + STOP_RETRY_SEC
            return {"ok": False, "uncertain": True, "note": "состояние стопа не сохранено"}
        kwargs = {"request_id": request["request_id"]} if isinstance(self.broker, trader_broker.Broker) else {}
        rs = await self.broker.place_stop(self.figi, request["direction"], request["lots"],
                                           request["price"], tag="aip-tros", **kwargs)
        if rs.get("ok"):
            self._adopt_stop(pos, request, rs.get("stop_order_id"))
        else:
            # биржа отбила трос: позиция ведётся виртуальным стопом (тик закроет за уровнем), а
            # выставление ПОВТОРЯЕТСЯ через STOP_RETRY_SEC — иначе позиция навсегда без троса на бирже
            pos["stop_id"] = None
            if not rs.get("uncertain") and was_uncertain:
                # W4: определённый отказ на ПОВТОРЕ неопределённого запроса — биржа этот UUID не приняла: запрос
                # закрыт (раньше он оставался висеть). Но сначала список стопов: вдруг первая попытка легла
                return await self._resolve_stop_request(
                    pos, request, f"биржа отбила повтор запроса стопа: {str(rs.get('error') or rs.get('note') or 'без причины')[:80]}",
                    backoff=STOP_RETRY_SEC)
            if not rs.get("uncertain"):
                pos.pop("stop_request", None)
            pos["stop_err"] = str(rs.get("error") or rs.get("note") or "биржа отбила")[:100]
            pos["restop"] = True
            pos["restop_after"] = time.time() + STOP_RETRY_SEC
            if pos.get("stop_request"):
                self.last_action = "подтверждение биржевого стопа не получено — восстанавливаю тот же запрос"
                log.warning("ИИ-пилот %s: статус троса неизвестен (%s); проверяю тот же UUID через %d с",
                            self.base, pos["stop_err"], int(STOP_RETRY_SEC))
            else:
                log.warning("ИИ-пилот %s: ⚠ биржевой трос НЕ встал (%s) — только "
                            "виртуальный стоп, повтор через %d с", self.base,
                            pos["stop_err"], int(STOP_RETRY_SEC))
        self._save_state()
        return rs

    async def _cancel_position_stop(self, pos: dict) -> dict:
        async with self._stop_lock:
            if pos.get("stop_request"):
                if self._exchange_stop_on():
                    resolved = await self._settle_stop_request(pos)
                else:                          # V 5.4.1: без флага тот же UUID не повторяем — только список стопов биржи
                    resolved = await self._resolve_stop_request(
                        pos, pos["stop_request"], "стопы только в программе (PYTHIA_EXCHANGE_STOP=0) — запрос стопа не повторяю",
                        backoff=0.0)
                if not resolved.get("ok"):
                    return resolved
            if not pos.get("stop_id"):
                return {"ok": True}
            result = await self.broker.cancel_stop(pos["stop_id"])
            if result.get("ok"):
                pos["stop_id"] = None
                self._save_state()
                return result
            # W4 (A7/A10): отмена не подтверждена ≠ стоп жив — список стопов биржи: нашего stop_id там нет
            # (исполнен / снят владельцем / истёк) → отмена подтверждена; остаток позиции сверит вызывающий
            gone, err = await self._stop_gone(pos["stop_id"])
            if gone:
                log.warning("ИИ-пилот %s: стопа %s на бирже уже нет — отмена подтверждена списком стопов",
                            self.base, pos["stop_id"])
                pos["stop_id"] = None
                self._save_state()
                return {"ok": True, "gone": True}
            if err:
                result = dict(result, note=f"{result.get('error') or result.get('note') or 'отмена не подтверждена'}; список стопов: {err}")
            return result

    # ── реальный остаток своих лотов на бирже (для аккуратного закрытия) ────
    async def _real_own_lots(self, pos: dict) -> int | None:
        """Сколько НАШИХ лотов реально осталось на счёте (None = не узнали)."""
        fn = getattr(self.broker, "portfolio", None)
        if fn is None:
            return None
        pf = await fn()
        if not isinstance(pf, dict) or pf.get("error") is not None:
            return None
        if pf.get("mode") == "dry":
            return pos["lots"]                 # dry: биржи нет, верим учёту
        real = 0
        for p_ in pf.get("positions") or []:
            if p_.get("figi") == self.figi:
                q = _f(p_.get("qty"))
                real = (int(q) if self.asset_class == "futures"
                        else int(q / max(1.0, self.point_value)))
                break
        sgn = 1 if pos["side"] == "long" else -1
        own_dir = (real - self.foreign_lots) * sgn
        return max(0, min(pos["lots"], own_dir))

    # ── закрытие всего (победа/стоп/решение ИИ) ─────────────────────────────
    async def _close_all(self, price: float, why: str,
                         reanalyze: bool = True) -> bool:
        if self.pending:
            part, order = await self._cancel_pending()
            if self.pending:
                self._close_pending = why
                self._save_state()
                return False
            if part and order:
                await self._absorb_fill(part, order, place_stop=False)
        pos = self.position
        if not pos:
            return True
        if pos.get("closing"):
            # закрытие уже идёт в другой задаче (FLASH у троса / перепроверка), а петля тикнула
            # за трос: второй рыночный ордер = голый разворот (проверяющий v5.2)
            self.last_action = f"закрытие уже идёт — второй ордер не шлю ({why})"
            return False
        pos["closing"] = True
        try:
            return await self._close_all_once(pos, price, why, reanalyze)
        finally:
            pos.pop("closing", None)

    async def _close_all_once(self, pos: dict, price: float, why: str,
                              reanalyze: bool) -> bool:
        gt = self._guard_task
        if gt and not gt.done() and gt is not asyncio.current_task():
            gt.cancel()                        # ИИ ещё думал у троса/тейка — уже не о чем
        pt = self._profit_task
        if pt and not pt.done() and pt is not asyncio.current_task():
            pt.cancel()                        # v5.4.1: мысль о прибыли о закрываемой позиции тоже не нужна
        pos["guard_busy"] = False
        pos["profit_busy"] = False
        pos["close_fail"] = why
        pos["close_reanalyze"] = reanalyze
        est_px = price if price and price > 0 else _f(pos.get("entry"))
        if pos.get("exit_order"):
            terminal = await self._poll_exit_order(pos, est_px)
            if pos["lots"] == 0 and terminal:
                self._finish_closed(pos, why, reanalyze)
                return True
            self._save_state()
            return False  # a terminal partial order permits a retry on the next tick
        # An unsuccessful cancellation does not prove the stop has executed.
        # Never place another close while that stop can still fire as well.
        # Wait for any in-flight restop before reading/cancelling its identifier.
        if pos.get("stop_id") or pos.get("stop_request") or self._stop_lock.locked():
            rc = await self._cancel_position_stop(pos)
            if rc.get("ok"):
                pos.pop("panic_stop_fails", None)
                self.panic_blocked = False
                if rc.get("gone"):             # стопа на бирже уже нет: мог исполниться — остаток по счёту
                    verdict = await self._verify_remainder(pos, est_px, why, reanalyze, at_stop=True, strict=True)
                    if verdict != "proceed":
                        return verdict == "closed"
            else:
                real = await self._real_own_lots(pos)
                if real == 0 and not pos.get("stop_request"):
                    pnl = self._realize(pos, pos["lots"], self._stop_price(pos) or est_px, why, risk=False)
                    pos["exit_pnl_total"] = _f(pos.get("exit_pnl_total")) + pnl
                    pos["lots"] = 0
                    self._finish_closed(pos, why, reanalyze)
                    return True
                reason = str(rc.get("note") or rc.get("error") or "отмена троса не подтверждена")[:140]
                if not self.panic_flag:
                    self.last_action = f"отмена троса не подтверждена ({reason}) — повторное закрытие пока не отправляю"
                    self._save_state()
                    return False
                # ПАНИКА (W4, п. 3): не «повторяю каждый тик» вечно — N тиков, затем ВСЕ стопы по инструменту
                # снимаются через список биржи и позиция закрывается; список недоступен → честно: нужен force
                n = pos["panic_stop_fails"] = int(pos.get("panic_stop_fails") or 0) + 1
                if n < PANIC_STOP_SWEEP_TICKS and not self.panic_force:
                    self.last_action = (f"отмена троса не подтверждена ({reason}) — попытка {n}/{PANIC_STOP_SWEEP_TICKS}, "
                                        "затем снимаю все стопы по инструменту через список биржи и закрываю")
                    self._save_state()
                    return False
                sweep = await self._sweep_figi_stops(pos)
                if not sweep.get("ok") and not self.panic_force:
                    self.panic_blocked = True
                    self.last_action = (f"отмена троса не подтверждена ({reason}); снять стопы по инструменту не удалось: "
                                        f"{sweep.get('error')} — закрытие без проверки стопов только повторной ПАНИКОЙ (force)")
                    self._save_state()
                    return False
                if not sweep.get("ok"):
                    log.error("ИИ-пилот %s: ПАНИКА force — стопы по %s не сверены (%s), закрываю по воле владельца; "
                              "проверь стопы у брокера", self.base, self.figi, sweep.get("error"))
                    pos["stop_id"] = None
                    pos.pop("stop_request", None)
                    pos["stop_err"] = f"ПАНИКА force: стопы не сверены ({str(sweep.get('error'))[:60]})"[:100]
                self.panic_blocked = False
                verdict = await self._verify_remainder(pos, est_px, why, reanalyze, at_stop=True, strict=True)
                if verdict != "proceed":
                    return verdict == "closed"
        if pos.pop("exit_check", None):
            # прошлая заявка выхода кончилась без полного исполнения (снята биржей): остаток — по счёту, а не по
            # памяти, иначе продажа владельца руками за это время + наша повторная = голый разворот;
            # strict — сверка отложена после исчезнувшего троса (№4): без счёта закрывать нельзя
            strict = bool(pos.pop("exit_check_strict", False))
            verdict = await self._verify_remainder(pos, est_px, why, reanalyze, at_stop=strict, strict=strict)
            if verdict != "proceed":
                return verdict == "closed"
        pos["exit_order"] = {"kind": "close", "lots": int(pos["lots"]), "accounted": 0,
                             "price": est_px, "why": why, "reanalyze": reanalyze}
        result = await self._post_owned_order(pos["exit_order"], SELL if pos["side"] == "long" else BUY,
                                              None, "aip-close")
        if not result.get("ok") and not result.get("uncertain"):
            pos.pop("exit_order", None)
            self.last_action = f"закрытие отбито биржей ({result.get('error') or result.get('note')}) — повторю тиком"
            self._save_state()
            return False
        terminal = await self._poll_exit_order(pos, est_px)
        if pos["lots"] == 0 and terminal:
            self._finish_closed(pos, why, reanalyze)
            return True
        return False

    async def _poll_exit_order(self, pos: dict, price: float) -> bool:
        """Apply only the new cumulative fills; NEW/unknown retains order ownership."""
        order = pos["exit_order"]
        state = await self._owned_order_state(order)
        vanished = self._request_vanished(order, state)
        if vanished:
            log.error("ИИ-пилот %s: заявка выхода %s не найдена биржей дольше %d с — до биржи не дошла; остаток сверю "
                      "со счётом и закрою заново", self.base, order.get("request_id") or order.get("order_id"),
                      int(ORDER_REQUEST_MAX_AGE_SEC))
        total = int(order["lots"])
        cumulative = total if state.get("filled") else max(0, min(total, int(_f(state.get("exec_lots")))))
        cumulative = max(cumulative, int(order.get("seen_exec_lots") or 0))
        order["seen_exec_lots"] = cumulative
        delta = min(int(pos["lots"]), max(0, cumulative - int(order.get("accounted") or 0)))
        if delta:
            # v5.4.2: кусок исполнения — в журнал сразу, в killswitch итогом круга/ужатия (_risk_round), не куском
            pnl = self._realize(pos, delta, price, order.get("why") or "исполнение выхода", risk=False)
            pos["exit_pnl_total"] = _f(pos.get("exit_pnl_total")) + pnl
            pos["lots"] -= delta
            self._closed_ts = time.time()
            self._mx = None
        order["accounted"] = cumulative
        terminal = (cumulative >= total or bool(state.get("ok")) and
                    str(state.get("status") or "").endswith(("REJECTED", "CANCELLED")) or vanished)
        if terminal:
            pos.pop("exit_order", None)
            if pos["lots"] > 0 and order.get("kind") == "close":
                pos["exit_check"] = True       # остаток перед повторной заявкой сверить со счётом (W4)
        self.last_action = (f"выход: подтверждено {cumulative}/{total} лот; " +
                            ("заявка завершена" if terminal else "жду исполнения, повторную заявку не отправляю")
                            + (" (⚠ заявка отправлена без персиста: state-файл не пишется)" if order.get("unpersisted") else ""))
        self._save_state()
        return terminal

    def _drop_topup_plan(self) -> None:
        """Ревью 5.4.2: позиция закрыта — план ДОБОРА к ней (src topup от наследника) не вход: снимается."""
        if self.plan and self.plan.get("src") == "topup":
            log.info("ИИ-пилот %s: позиция закрыта — план добора снят", self.base)
            self.plan = None

    def _finish_closed(self, pos: dict, why: str, reanalyze: bool) -> None:
        pnl = _f(pos.get("exit_pnl_total"))
        self._risk_round(pos)                  # v5.4.2: круг закрыт — killswitch видит его итог один раз
        self.position = None
        self._drop_topup_plan()
        self._mx = None                        # позиции нет — «сколько даёт биржа» заново (иначе флип берёт sell с закрытием лонга)
        self._closed_ts = time.time()          # лаг портфеля: позицию «со счёта» примем не раньше двух сверок
        self._save_state()
        self.state = "ЖДУ_ПЛАН"
        self.last_action = f"ЗАКРЫЛ ВСЁ ({why}): P/L {pnl:+.0f}"
        log.info("ИИ-пилот %s: %s", self.base, self.last_action)
        if reanalyze and not (self.session_risk and self.session_risk.locked):
            self._fire_reanalyze("после закрытия — огромный анализ с нуля")

    async def _reduce(self, pos: dict, excess: int, price: float,
                      why: str) -> None:
        """Частичное ужатие позиции (дозор): закрыть excess лотов, трос — на
        остаток. P/L срезанной части — в killswitch."""
        if pos.get("closing") or self.position is not pos:
            return
        pos["closing"] = True
        try:
            order = pos.get("exit_order")
            if order and order.get("kind") != "reduce":
                return
            if not order:
                excess = max(0, min(int(excess), pos["lots"]))
                if excess <= 0:
                    return
                if pos.get("stop_id") or pos.get("stop_request") or self._stop_lock.locked():
                    cancelled = await self._cancel_position_stop(pos)
                    if not cancelled.get("ok"):
                        self.last_action = "ужатие ждёт подтверждения отмены старого троса"
                        return
                pos["exit_order"] = {"kind": "reduce", "lots": excess, "accounted": 0,
                                     "price": price, "why": why}
                result = await self._post_owned_order(pos["exit_order"], SELL if pos["side"] == "long" else BUY,
                                                      None, "aip-reduce")
                if not result.get("ok") and not result.get("uncertain"):
                    pos.pop("exit_order", None)
                    pos["restop"] = True
                    self.last_action = f"ужатие отбито биржей — повторю тиком ({why})"
                    self._save_state()
                    return
            order = pos.get("exit_order")
            terminal = await self._poll_exit_order(pos, price)
            if terminal:
                if pos["lots"] <= 0:
                    self._finish_closed(pos, why, False)
                else:
                    if int((order or {}).get("accounted") or 0) > 0:
                        self._risk_round(pos)  # v5.4.2: одно ужатие — одна запись killswitch (не по кускам)
                    if not pos.get("close_fail"):
                        await self._replace_stop(pos)
        finally:
            pos.pop("closing", None)

    # ── сверка с биржей: изоляция, внешние закрытия, свежие деньги/ГО ───────
    async def _reconcile(self, price: float) -> None:
        if self.pending or any((self.position or {}).get(key) for key in ("exit_order", "stop_request")):
            return  # broker order status owns these deltas; portfolio snapshots may lag
        pf = await self.broker.portfolio()
        if pf.get("mode") == "dry" or pf.get("error"):
            return
        cash = pf.get("cash")
        if cash is not None:
            self._live_cash = _f(cash)         # свежие деньги (вармаржа и пр.)
            self.account["free"] = self._live_cash
        if pf.get("total"):
            self.account["total"] = _f(pf.get("total"))
        try:
            await self._refresh_account(price) # маржа счёта и «сколько даёт биржа»
        except Exception as e:                                   # noqa: BLE001
            log.info("ИИ-пилот %s: атрибуты счёта: %s", self.base, str(e)[:80])
        if self.asset_class == "futures" and self._tick_n % (RECONCILE_EVERY * 5) == 0:
            try:                               # ГО биржа меняет — обновляем
                mg = await tinkoff.futures_margin(self.figi) or {}
                if _f(mg.get("margin_buy")) > 0:
                    self.go_per_lot = mg.get("margin_buy")
                    self.go_sell = mg.get("margin_sell")
            except Exception:                                # noqa: BLE001
                pass
        real, avg = 0, 0.0
        for p_ in pf.get("positions") or []:
            if p_.get("figi") == self.figi or (p_.get("uid") and p_.get("uid") == self.figi):
                qty = _f(p_.get("qty"))
                real = (int(qty) if self.asset_class == "futures"
                        else int(qty / max(1.0, self.point_value)))
                avg = _f(p_.get("avg"))
                break
        own = 0
        if self.position:
            own = (self.position["lots"] if self.position["side"] == "long"
                   else -self.position["lots"])
        if real == own + self.foreign_lots:
            return
        if self.adopt_account and not (own and real == 0):
            # ВЕСЬ СЧЁТ (v5.2): расхождение = владелец докупил/продал/открыл/перевернул руками —
            # принимаем как свою позицию (P/L проданной части — в killswitch и журнал).
            # Сразу после СВОЕГО закрытия портфель брокера может отставать: лоты ещё «лежат» —
            # это не позиция владельца, а лаг; принимаем только при двух сверках подряд
            if own == 0 and real != 0 and time.time() - self._closed_ts < ADOPT_COOL_SEC:
                seen, self._adopt_seen = self._adopt_seen, (real, avg)
                if not seen or seen[0] != real:
                    self.last_action = (f"на счёте {real:+d} лот после собственного закрытия — "
                                        "жду подтверждения следующей сверкой (лаг портфеля?)")
                    log.info("ИИ-пилот %s: %s", self.base, self.last_action)
                    return
            self._adopt_seen = None
            self._absorb_account(real, avg, price, "сверка с биржей")
            return
        if own == 0 and self.foreign_lots == 0 and real != 0:
            if not self._stray_warned:
                self._stray_warned = True
                log.warning("ИИ-пилот %s: на счёте %+d лот, которых пилот не "
                            "знает — считаю ручными владельца, НЕ трогаю "
                            "(изоляция)", self.base, real)
            self.foreign_lots = real
            return
        sgn = 1 if own > 0 else (-1 if own < 0 else 0)
        new_own = min(abs(own), max(0, real * sgn)) * sgn if sgn else 0
        self.foreign_lots = real - new_own
        if own and not new_own:
            # позицию закрыло что-то вне петли: биржевой трос или владелец.
            # Стоп-сироту снять ОБЯЗАТЕЛЬНО (иначе позже откроет позицию)
            pos, self.position = self.position, None
            self._drop_topup_plan()
            self._mx = None                # позиции нет — «сколько даёт биржа» считать заново
            if pos.get("stop_id"):
                await self.broker.cancel_stop(pos["stop_id"])
            sgn_f = 1.0 if pos["side"] == "long" else -1.0
            est = (self._stop_price(pos) or _f(pos.get("invalidation"))) or price
            pnl = (est - pos["entry"]) * sgn_f * pos["lots"] * self.point_value
            self.pnls.append(pnl)
            self._risk_round(pos, pnl)         # v5.4.2: круг — одна запись (с кусками выхода, если были)
            self._save_state()
            self.state = "ЖДУ_ПЛАН"
            self.last_action = (f"позицию закрыла биржа/владелец (трос?): "
                                f"P/L ≈{pnl:+.0f} по уровню стопа/цене")
            log.warning("ИИ-пилот %s: %s", self.base, self.last_action)
            if not (self.session_risk and self.session_risk.locked):
                self._fire_reanalyze("внешнее закрытие — свежий разбор")
        elif new_own != own and self.position:
            cut = abs(own) - abs(new_own)
            sgn_f = 1.0 if self.position["side"] == "long" else -1.0
            pnl = (price - self.position["entry"]) * sgn_f * cut * self.point_value
            self.pnls.append(pnl)              # срез виден killswitch'у
            if self.session_risk:
                self.session_risk.record(pnl)
            self.position["lots"] = abs(new_own)
            self.position["restop"] = True     # трос — на новый объём
            self._save_state()
            self.last_action = (f"сверка: моих лотов теперь {abs(new_own)} "
                                f"(срез {cut}, P/L ≈{pnl:+.0f})")

    # ── 30-минутная перепроверка ────────────────────────────────────────────
    async def _gather_news(self) -> str:
        try:
            from . import news as news_mod
        except ImportError:
            import news as news_mod
        blocks = []
        try:
            items, note = await asyncio.wait_for(
                news_mod.fetch_for_ticker(self.base, self.name,
                                          asset_class=self.asset_class),
                timeout=30)
            if items:
                blocks.append(f"— по компании ({note}):\n"
                              + news_mod.render_for_ai(items, limit=15))
        except Exception as e:                               # noqa: BLE001
            log.info("новости компании молчат: %s", str(e)[:60])
        try:
            common = await asyncio.wait_for(news_mod.fetch_news(days=1),
                                            timeout=30)
            if common:
                blocks.append("— общий поток (сутки):\n"
                              + news_mod.render_for_ai(common, limit=25))
        except Exception as e:                               # noqa: BLE001
            log.info("общий поток молчит: %s", str(e)[:60])
        return "\n\n".join(blocks)

    async def _last_verdict(self) -> str:
        try:
            try:
                from . import memory
            except ImportError:
                import memory
            rec = await memory.get_analysis(self.base)
            return (rec or {}).get("verdict") or ""
        except Exception:                                    # noqa: BLE001
            return ""

    def _situation_text(self, price: float) -> str:
        L = [f"Цена сейчас: {price:g}"]
        if len(self.prices) > 2:
            n30 = int(1800 / TICK_SEC)
            base = self.prices[-n30] if len(self.prices) >= n30 else self.prices[0]
            if base > 0:
                L.append(f"Ход за окно наблюдения: {(price/base-1)*100:+.2f}%")
        b = self.last_book or {}
        if b.get("best_bid") and b.get("best_ask"):
            L.append(f"Стакан: bid {b['best_bid']} / ask {b['best_ask']}"
                     + (f", дисбаланс {b.get('imbalance')}"
                        if b.get("imbalance") is not None else ""))
        if self.position:
            p = self.position
            held = int((time.time() - p["opened_ts"]) / 60)
            L.append(f"ПОЗИЦИЯ: {p['side']} {p['lots']} лот @{p['entry']:g}, "
                     f"в рынке {held} мин, плавающий P/L {p.get('floating', 0):+.0f} ₽, "
                     f"трос @{p.get('invalidation')}, тейк {p.get('take')}")
        elif self.pending:
            L.append(f"ЗАЯВКА ВХОДА В ПОЛЁТЕ: {self.pending['side']} "
                     f"{self.pending['lots']} лот @{self.pending['price']}")
        elif self.plan:
            L.append(f"ВХОД ВЗВЕДЁН (засада): {self.plan['side']} @{self.plan.get('entry')} "
                     f"(вход в имба-момент), стоп {self.plan['invalidation']}")
        else:
            L.append("Позиции нет, засады нет — полностью вне рынка")
        L.append(f"Депозит {self.deposit:.0f} ₽, результат сессии "
                 f"{sum(self.pnls):+.0f} ₽ за {len(self.pnls)} сделок")
        acc_line = self._account_line()
        if acc_line:
            L.append(acc_line)
        if self.last_review:
            age = int((time.time() - self.last_review["ts"]) / 60)
            L.append(f"Прошлая перепроверка ({age} мин назад): "
                     f"{self.last_review['choice']} — {self.last_review['why']}")
        sr = self.session_risk.state() if self.session_risk else {}
        if sr:
            L.append(f"Killswitch: {'ЗАБЛОКИРОВАН' if sr.get('locked') else 'ок'}"
                     f" (дневной лимит {sr.get('day_loss_limit')})")
        return "\n".join(L)

    async def _review_bg(self, price: float) -> None:
        self._review_busy = True
        try:
            await self._review(price)
        except Exception as e:                               # noqa: BLE001
            log.warning("перепроверка споткнулась: %s", str(e)[:120])
        finally:
            self._review_busy = False

    @staticmethod
    def _parse_choice(raw: str, in_pos: bool, side: str | None = None) -> str | None:
        """Разбор решения перепроверки (v5.4.2): целые слова через ai_v5.decision_of по таблице
        ai_v5.review_table(in_pos, side) — «ЗАКРЫТЬ ВСЁ», «КУПИТЬ СЕЙЧАС!», BUY/LONG/SELL/SHORT, CLOSE/EXIT/
        ЗАФИКСИРОВАТЬ, ё/е, регистр. В позиции: ДОБРАТЬ и ПЕРЕВЕРНУТЬ; «купить» при лонге — добор, «продать» при
        лонге — закрыть (сторона позиции — side). Возврат: канонический токен или None — решения нет (пусто,
        непонятно, «НЕ …», «… или …»): код не выбирает за ИИ ни ЖДЁМ, ни вход — вызывающий переспрашивает."""
        try:
            from . import ai_v5 as _a
        except ImportError:
            import ai_v5 as _a                               # noqa: E401
        return _a.decision_of(raw, _a.review_table(in_pos, side))

    async def _review(self, price: float) -> None:
        try:
            from . import prompts
        except ImportError:
            import prompts
        ask_json = self._ai_ask_json
        if ask_json is None:
            try:
                from . import ai as ai_mod
            except ImportError:
                import ai as ai_mod
            ask_json = ai_mod.ask_json
        in_pos = self.position is not None
        news_txt, verdict = await asyncio.gather(self._gather_news(),
                                                 self._last_verdict())
        if not news_txt.strip():
            news_txt = ("(новости недоступны: сбой сбора — НЕ выдумывай их, "
                        "решай по цене, стакану и вердикту)")
        obj = await asyncio.wait_for(ask_json(
            prompts.REVIEW_SYS,
            prompts.review_user(self.base, self.name,
                                self._situation_text(price), verdict,
                                news_txt, in_pos),
            route="aip_review"), timeout=300)
        if not isinstance(obj, dict):
            log.warning("перепроверка: ИИ не дал JSON — держу как есть")
            return
        # путь 4.x (PYTHIA_LEGACY_PILOT): поведение как было — непонятый ответ держит всё как есть (ЖДЁМ);
        # миссия v5 (MissionPilot) разбирает сама и None не подменяет
        choice = self._parse_choice(str(obj.get("choice") or ""), in_pos,
                                    (self.position or {}).get("side")) or "ЖДЁМ"
        why = str(obj.get("why") or "")[:300]
        self.last_review = {"choice": choice, "why": why, "ts": time.time()}
        log.info("ИИ-пилот %s: перепроверка → %s (%s)", self.base, choice, why)
        cur = self.prices[-1] if self.prices else price
        if choice in ("ДОБРАТЬ", "ПЕРЕВЕРНУТЬ") and self.position:
            pos = self.position
            side = pos["side"] if choice == "ДОБРАТЬ" else ("short" if pos["side"] == "long" else "long")
            inv = _f(obj.get("invalidation")) if obj.get("invalidation") is not None else 0.0
            if choice == "ДОБРАТЬ" and not (inv > 0):
                inv = _f(pos.get("invalidation"))
            good = inv > 0 and ((side == "long" and inv < cur) or (side == "short" and inv > cur))
            if not good:
                inv = cur * (1 - EMERGENCY_STOP_FRAC if side == "long" else 1 + EMERGENCY_STOP_FRAC)
            take = _f(obj.get("take")) if obj.get("take") is not None else (pos.get("take") if choice == "ДОБРАТЬ" else None)
            self.plan = {"side": side, "entry": None, "take": take, "invalidation": round(inv, 6),
                         "why": "перепроверка: " + why, "ts": time.time()}
            self.last_action = f"перепроверка: {choice} → {side} на максимум"
            return
        # решение по протухшему снимку не исполняется: цена ушла >1% от
        # снимка, по которому думал ИИ → честный свежий разбор (находка веера)
        drift = abs(cur - price) / price if price else 0.0
        if choice in ("КУПИТЬ_СЕЙЧАС", "ПРОДАТЬ_СЕЙЧАС") and drift > drift_frac():
            log.warning("перепроверка протухла: цена ушла %.2f%% за время "
                        "раздумий — вместо входа прошу свежий разбор",
                        drift * 100)
            self._fire_reanalyze("решение перепроверки протухло")
            return
        if choice == "ЗАКРЫТЬ" and self.position:
            await self._close_all(cur, "решение перепроверки: закрыть")
        elif choice in ("КУПИТЬ_СЕЙЧАС", "ПРОДАТЬ_СЕЙЧАС") and not self.position \
                and not self._reanalyzing:
            side = "long" if choice == "КУПИТЬ_СЕЙЧАС" else "short"
            inv = obj.get("invalidation")
            inv = _f(inv) if inv is not None else 0.0
            # стоп не с той стороны/нулевой → аварийная политика режима
            good = (inv > 0 and ((side == "long" and inv < cur)
                                 or (side == "short" and inv > cur)))
            if not good:
                inv = cur * (1 - EMERGENCY_STOP_FRAC if side == "long"
                             else 1 + EMERGENCY_STOP_FRAC)
                log.warning("перепроверка без валидного invalidation — "
                            "аварийный стоп %.6g (политика %.1f%%)", inv,
                            EMERGENCY_STOP_FRAC * 100)
            take = obj.get("take")
            take = _f(take) if take is not None else None
            if take is not None and not ((side == "long" and take > cur)
                                         or (side == "short" and take < cur)):
                log.warning("тейк %s не с той стороны от цены %s — снят", take, cur)
                take = None
            self.plan = {"side": side, "entry": None, "take": take,
                         "invalidation": round(inv, 6),
                         "why": "перепроверка: " + why, "ts": time.time()}
            if self.pending:                   # старая заявка — снять тиком
                self._cancel_entry = True
            self.state = "ВХОЖУ"
        elif choice == "НОВЫЙ_АНАЛИЗ":
            self._fire_reanalyze("перепроверка потребовала свежий разбор")
        # ЖДЁМ — осознанное решение: держим как есть

    # ── огромный анализ с нуля (сервер вешает колбэк) ───────────────────────
    def _fire_reanalyze(self, why: str, force: bool = False) -> None:
        if self._reanalyzing:
            return
        if not self.reanalyze_cb:
            log.warning("ИИ-пилот %s: %s — но конвейер не подключён "
                        "(reanalyze_cb не задан)", self.base, why)
            return
        # ПЕЙСИНГ ДНЕВНОЙ РАБОТЫ: огромный анализ — пять стадий ИИ, это дорого
        # и долго. Пилот работает ВЕСЬ ДЕНЬ, поэтому просить новый разбор чаще
        # REANALYZE_GAP_SEC нельзя: иначе серия неудач (биржа отбивает, приказ
        # не собрался) сожгла бы ключ за час. Не отменяем — ОТКЛАДЫВАЕМ.
        # force=True (мягкий стоп: позиция за триггером, FLASH ждёт Совет) — без очереди.
        wait = REANALYZE_GAP_SEC - (time.time() - self._last_reanalyze_ts)
        if self._last_reanalyze_ts and wait > 0 and not force:
            self._reanalyze_pending = why
            self.last_action = (f"{why} — новый разбор через {int(wait)}с "
                                f"(пейсинг: не чаще раза в "
                                f"{int(REANALYZE_GAP_SEC/60)} мин)")
            return
        self._reanalyze_pending = None
        self._last_reanalyze_ts = time.time()
        self.analyses += 1
        self._reanalyzing = True
        if not self.position:
            self.plan = None
            self.state = "ПЕРЕАНАЛИЗ"
        log.info("ИИ-пилот %s: %s — запускаю конвейер", self.base, why)

        async def _run():
            try:
                await self.reanalyze_cb()
            except Exception as e:                           # noqa: BLE001
                log.warning("переанализ упал: %s", str(e)[:120])
            finally:
                if self._reanalyzing:          # колбэк не донёс adopt_forecast
                    self._reanalyzing = False
                    if self.state == "ПЕРЕАНАЛИЗ":
                        self.state = "ЖДУ_ПЛАН"
                        # без свежего приказа — ранняя перепроверка решит сама
                        self.review_ts = min(self.review_ts, time.time() + 300)

        self._spawn_background(_run())

    # ── мягкий стоп (v5.2): у триггера спрашиваем FLASH ──────────────────────
    async def _stop_guard(self, price: float, pos: dict) -> dict:
        """Базовый пилот FLASH не зовёт: стоп есть стоп. Наследник (MissionPilot)
        собирает максимум данных и задаёт один свободный вопрос."""
        return {"decision": "СЛИТЬ", "why": "базовый пилот: стоп есть стоп"}

    def _guard_handoff(self, why: str) -> None:
        """ИИ у троса решил ждать → задача уходит Совету (наследник зовёт полный совет без очереди)."""
        self._fire_reanalyze(f"мягкий стоп: {self._money_name()} решил ждать — " + why, force=True)

    async def _guard_bg(self, price: float, pos: dict) -> None:
        try:
            r = await asyncio.wait_for(self._stop_guard(price, pos), GUARD_TIMEOUT)
        except asyncio.CancelledError:
            pos["guard_busy"] = False
            raise
        except Exception as e:                               # noqa: BLE001
            # v5.4.2 (ревью): молчание — не решение ИИ; выход по правилу остаётся страховкой, но пишется как действие кода
            r = {"decision": "СЛИТЬ", RULE_KEY: "НЕТ_ОТВЕТА", RULE_DETAIL: str(e)[:80] or type(e).__name__}
        finally:
            pos["guard_busy"] = False
        try:
            await self._apply_guard(r if isinstance(r, dict) else {}, price, pos)
        except Exception as e:                               # noqa: BLE001
            log.warning("ИИ-пилот %s: решение у троса не применилось: %s", self.base, str(e)[:120])

    def _rule_rec(self, rec: dict, r: dict, action: str) -> str:
        """v5.4.2 (ревью): у троса/тейка решения ИИ не было (r[RULE_KEY]: НЕТ_ОТВЕТА — таймаут/сбой, НЕ_РАЗОБРАН — слово
        не разобрано) — страховка кода (action: СЛИТЬ / ЗАФИКСИРОВАТЬ) пишется как действие кода: decision = rule,
        silent, source «код», сырой ответ; слова ИИ при непонятном ответе — в note. Возврат: «что случилось» для причины.
        Ревью 5.4.2 (финал): флаг кода — приватные ключи (RULE_KEY/RULE_DETAIL/RULE_RAW), а не «rule»/«detail»/«raw»:
        поле «rule» в ответе ИИ («ЖДАТЬ» + своё правило) не превращает его решение в выход по правилу."""
        mm = self._money_name()
        rule = str(r.get(RULE_KEY) or "НЕТ_ОТВЕТА")
        det = str(r.get(RULE_DETAIL) or "")[:80]
        what = (f"{mm} не ответил" if rule == "НЕТ_ОТВЕТА" else "ответ не разобран") + (f" ({det})" if det else "")
        rec.update(decision=rule, silent=True, source="код", raw=(str(r.get(RULE_RAW) or "")[:80] or None),
                   why=f"{what} — ответа не было, выход по правилу", applied=f"по правилу: {action}",
                   note=str(r.get("why") or "")[:300])
        return what

    async def _apply_guard(self, r: dict, price: float, pos: dict) -> None:
        """Ответ у троса (мягкий стоп). СЛИТЬ / нет решения ИИ → закрыть (второе — по правилу, запись кода).
        ЖДАТЬ (ревью 5.4.3, D5 — поведение однозначно): hold_until_price принимается, только если лежит строго между
        аварийным тросом и ценой (лонг: трос < X < цена; шорт: цена < X < трос) — это новый триггер (трос не
        двигается), и когда цена его пройдёт, дежурного спросят снова сразу, без срока; не принят (нет числа, не в
        коридоре) — триггер прежний, запись и last_action пишут «не принят», и пока цена за триггером, вопрос повторится
        через hold_minutes (1–60 мин; нет — PYTHIA_SOFT_STOP_GRACE_SEC). Каждое ЖДАТЬ — +1 к holds; holds ≥
        PYTHIA_SOFT_STOP_MAX_HOLDS → за триггером слив по правилу без вопроса; за аварийным тросом — слив без вопроса
        всегда. Цена вернулась за триггер дольше PYTHIA_SOFT_STOP_GRACE_SEC — holds заново (AIPilot.tick)."""
        if self.position is not pos or not self.position:
            return                             # позиция уже закрыта/сменилась
        raw = str(r.get("decision") or "").upper().replace("Ё", "Е")
        rule = bool(r.get(RULE_KEY))           # v5.4.2: решения ИИ не было — выход по правилу (запись кода)
        hold = ("ЖД" in raw or "ДЕРЖ" in raw or "HOLD" in raw or "WAIT" in raw) and "СЛИ" not in raw and not rule
        why = str(r.get("why") or "")[:300]
        cur = self.prices[-1] if self.prices else price
        mm = self._money_name()
        rec = {"ts": time.time(), "side": "stop", "decision": "ЖДАТЬ" if hold else "СЛИТЬ", "why": why,
               "note": str(r.get("note") or "")[:300], "price": cur,
               "trigger": pos.get("invalidation"), "hold_until": _f(r.get("hold_until_price")) or None,
               "hold_minutes": _f(r.get("hold_minutes")) or None, "model": mm, "silent": False, "source": "ИИ",
               "pos_side": pos.get("side")}                # ревью 5.4.3: сторона позиции — память «за лонг/шорт»
        what = self._rule_rec(rec, r, "СЛИТЬ") if rule else ""
        if rule:
            rec["hold_until"] = rec["hold_minutes"] = None
        self.guards.append(rec)
        del self.guards[:-GUARDS_KEEP]
        pos["guard_last"] = time.time()
        if rule:
            await self._close_all(cur, f"мягкий стоп по правилу: {what}")
            return
        if not hold:
            await self._close_all(cur, f"мягкий стоп: {mm} решил слить — " + (why or "без объяснений"))
            return
        pos["holds"] = int(pos.get("holds") or 0) + 1
        is_long = pos["side"] == "long"
        hard = _f(pos.get("hard_stop"))
        new_trig = _f(r.get("hold_until_price"))
        moved = ""
        ok_trig = new_trig > 0 and ((is_long and new_trig < cur) or (not is_long and new_trig > cur)) \
            and (hard <= 0 or (is_long and new_trig > hard) or (not is_long and new_trig < hard))
        grace = float(_setting("PYTHIA_SOFT_STOP_GRACE_SEC", 180))
        hm = _f(r.get("hold_minutes"))
        if hm > 0:
            grace = max(60.0, min(hm * 60.0, 3600.0))
        if ok_trig:
            # ревью 5.4.3 (D5): новый триггер — у него вопрос сразу, как цена его пройдёт (срок hold_minutes — только
            # при прежнем триггере: цена и так за ним)
            self._set_levels(pos, None, new_trig, inv0=False)
            moved = f", новый триггер {new_trig:g} — у него спрошу снова"
            rec["hold_minutes"] = None
            pos["guard_next"] = 0.0
            nxt = "следующий вопрос — когда цена пройдёт новый триггер"
        else:
            if new_trig > 0:                   # ревью 5.4.3: не в коридоре «трос < X < цена» — не принят, не молча
                where = (f"не {'ниже' if is_long else 'выше'} цены {cur:g}"
                         if not ((is_long and new_trig < cur) or (not is_long and new_trig > cur))
                         else f"за аварийным тросом {hard:g}")
                rec["hold_until"] = None
                rec["hold_until_ai"] = new_trig
                rec["hold_note"] = f"hold_until_price {new_trig:g} не принят: {where}; триггер прежний {_f(pos.get('invalidation')):g}"
                moved = f"; {rec['hold_note']}"
            pos["guard_next"] = time.time() + grace
            nxt = f"следующий вопрос через {int(grace // 60)} мин, если цена за триггером"
        self._save_state()
        self.last_action = (f"мягкий стоп: {mm} решил ЖДАТЬ ({why or 'без объяснений'}){moved}, "
                            f"{nxt} — передаю задачу Совету")
        log.warning("ИИ-пилот %s: %s", self.base, self.last_action)
        try:
            self._guard_handoff(why)
        except Exception as e:                               # noqa: BLE001
            log.warning("ИИ-пилот %s: передача Совету не удалась: %s", self.base, str(e)[:120])

    # ── мягкий тейк (v5.3 W2): у тейка спрашиваем FLASH ─────────────────────
    async def _take_guard(self, price: float, pos: dict) -> dict:
        """Базовый пилот FLASH не зовёт: тейк есть тейк — фиксируем. Наследник (MissionPilot)
        собирает те же данные, что у троса, и задаёт один свободный вопрос."""
        return {"decision": "ЗАФИКСИРОВАТЬ", "why": "базовый пилот: тейк есть тейк"}

    def _take_handoff(self, why: str) -> None:
        """ИИ у тейка решил подержать → задача Совету без очереди (наследник зовёт полный совет)."""
        self._fire_reanalyze(f"мягкий тейк: {self._money_name()} решил подержать — " + why, force=True)

    def _lock_floor(self, pos: dict) -> float:
        """Ниже какого уровня прибыль у тейка не отпускаем: вход + PYTHIA_TAKE_LOCK_PCT % хода до тейка."""
        entry, take = _f(pos.get("entry")), _f(pos.get("take"))
        pct = float(_setting("PYTHIA_TAKE_LOCK_PCT", 50.0)) / 100.0
        if entry <= 0 or take <= 0:
            return 0.0
        return round(self._snap(entry + (take - entry) * pct), 10)

    async def _take_bg(self, price: float, pos: dict) -> None:
        try:
            r = await asyncio.wait_for(self._take_guard(price, pos), TAKE_TIMEOUT)
        except asyncio.CancelledError:
            pos["guard_busy"] = False
            pos.pop("guard_side", None)
            raise
        except Exception as e:                               # noqa: BLE001
            # v5.4.2 (ревью): молчание — не решение ИИ; фиксация по правилу — страховка кода, так и пишется
            r = {"decision": "ЗАФИКСИРОВАТЬ", RULE_KEY: "НЕТ_ОТВЕТА", RULE_DETAIL: str(e)[:80] or type(e).__name__}
        finally:
            pos["guard_busy"] = False
            pos.pop("guard_side", None)
        try:
            await self._apply_take(r if isinstance(r, dict) else {}, price, pos)
        except Exception as e:                               # noqa: BLE001
            log.warning("ИИ-пилот %s: решение у тейка не применилось: %s", self.base, str(e)[:120])

    async def _apply_take(self, r: dict, price: float, pos: dict) -> None:
        """Ответ FLASH у тейка. ЗАФИКСИРОВАТЬ → закрыть всё (как раньше). ПОДЕРЖАТЬ → триггер подтягивается к
        lock_price (не ниже входа + доля прибыли), трос биржи переставляется за ним, тейк снимается или
        отодвигается на tp_next, задача Совету без очереди. Уровень не на безопасной стороне цены → фиксация."""
        if self.position is not pos or not self.position:
            return                             # позиция уже закрыта/сменилась
        raw = str(r.get("decision") or "").upper().replace("Ё", "Е")
        rule = bool(r.get(RULE_KEY))           # v5.4.2: решения ИИ не было — фиксация по правилу (запись кода)
        hold = (("ДЕРЖ" in raw or "ЖД" in raw or "HOLD" in raw or "WAIT" in raw)
                and "ФИКС" not in raw and "ЗАКР" not in raw and "СЛИ" not in raw and not rule)
        why = str(r.get("why") or "")[:300]
        cur = self.prices[-1] if self.prices else price
        take = _f(pos.get("take"))
        is_long = pos["side"] == "long"
        lock_ai = _f(r.get("lock_price"))
        tp_next = _f(r.get("tp_next"))
        mm = self._money_name()
        rec = {"ts": time.time(), "side": "take", "decision": "ПОДЕРЖАТЬ" if hold else "ЗАФИКСИРОВАТЬ", "why": why,
               "note": str(r.get("note") or "")[:300], "price": cur, "take": take or None,
               "lock_price": lock_ai or None, "tp_next": tp_next or None,
               "hold_minutes": _f(r.get("hold_minutes")) or None, "model": mm, "silent": False, "source": "ИИ",
               "pos_side": pos.get("side")}                # ревью 5.4.3: сторона позиции — память «за лонг/шорт»
        what = self._rule_rec(rec, r, "ЗАФИКСИРОВАТЬ") if rule else ""
        if rule:
            rec["lock_price"] = rec["tp_next"] = rec["hold_minutes"] = None
        self.guards.append(rec)
        del self.guards[:-GUARDS_KEEP]
        pos["take_last"] = time.time()
        if rule:
            await self._close_all(cur, f"ПОБЕДА: тейк @{take:g} — фиксация по правилу: {what}")
            return
        if not hold:
            await self._close_all(cur, f"ПОБЕДА: тейк @{take:g} — {mm}: зафиксировать — " + (why or "без объяснений"))
            return
        # ПОДЕРЖАТЬ: прибыль запираем триггером — не ниже входа + PYTHIA_TAKE_LOCK_PCT % хода до тейка
        floor = self._lock_floor(pos)
        lock = lock_ai if lock_ai > 0 else floor
        if floor > 0:
            lock = max(lock, floor) if is_long else min(lock, floor)
        safe = lock > 0 and ((is_long and lock < cur) or (not is_long and lock > cur))
        if not safe and floor > 0 and ((is_long and floor < cur) or (not is_long and floor > cur)):
            lock, safe = floor, True
        if not safe:
            await self._close_all(cur, f"ПОБЕДА: тейк @{take:g} — откат уже съел запас, {mm} хотел подержать "
                                       f"({why or 'без объяснений'}) — фиксирую")
            return
        rec["lock_price"] = lock
        if not (int(pos.get("take_holds") or 0) > 0 or pos.get("profit_lock")):
            pos["lock_from"] = _f(pos.get("invalidation")) or None   # ревью 5.4.3: триггер до запирания прибыли
        pos["take_holds"] = int(pos.get("take_holds") or 0) + 1
        self._set_levels(pos, None, lock, inv0=True)      # триггер = запертая прибыль; трос считается от него
        pos["restop"] = True                               # трос биржи — за новым триггером, ближайшим тиком
        pos["restop_after"] = 0.0
        pos["holds"] = 0                                   # новый триггер — счётчик «ждать» у троса заново
        tp_ok = tp_next > 0 and ((is_long and tp_next > cur) or (not is_long and tp_next < cur))
        pos["take"] = round(tp_next, 6) if tp_ok else None
        rec["tp_next"] = pos["take"]
        grace = float(_setting("PYTHIA_SOFT_STOP_GRACE_SEC", 180))
        hm = _f(r.get("hold_minutes"))
        if hm > 0:
            grace = max(60.0, min(hm * 60.0, 3600.0))
        pos["take_next"] = time.time() + grace
        self._save_state()
        self.last_action = (f"мягкий тейк: {mm} решил ПОДЕРЖАТЬ ({why or 'без объяснений'}) — прибыль заперта "
                            f"триггером {lock:g} ({self._hard_name()} {_f(pos.get('hard_stop')):g}), тейк "
                            + (f"→ {pos['take']:g}" if pos["take"] else "снят") + " — передаю задачу Совету")
        log.warning("ИИ-пилот %s: %s", self.base, self.last_action)
        try:
            self._take_handoff(why)
        except Exception as e:                               # noqa: BLE001
            log.warning("ИИ-пилот %s: передача Совету не удалась: %s", self.base, str(e)[:120])

    # ── статус/управление ───────────────────────────────────────────────────
    def status(self) -> dict:
        sr = self.session_risk.state() if self.session_risk else {}
        return {"base": self.base, "mode": getattr(self.broker, "mode", "dry"),
                "pilot": "ИИ-ПИЛОТ", "state": self.state,
                "plan": self.plan, "position": self.position,
                "pending": bool(self.pending),
                "deposit": self.deposit, "go_per_lot": self.go_per_lot,
                "session_pnl": round(sum(self.pnls), 2),
                "trades": len(self.pnls),
                # смена: пилот держится весь день — видно, сколько отработал
                "uptime_h": round((time.time() - self.started_ts) / 3600.0, 1),
                "analyses": self.analyses,
                "foreign_lots": self.foreign_lots or None,
                "account": dict(self.account) if self.account else None,
                "adopt_account": self.adopt_account,
                "sized_by_broker": self._sized_by_broker,
                "soft_stop": self._soft_stop_on(),
                "exchange_stop": self._exchange_stop_on(),   # v5.4.1: трос на бирже (1) или только в программе (0)
                "hard_stop": (self.position or {}).get("hard_stop"),
                # v5.4.1: проверка входа у двери (последняя по текущему плану) и мысль о прибыли
                "entry_gate": self._entry_gate_status(),
                "gates": self.gates[-10:],
                "profit": self._profit_status(),
                "profits": self.profits[-10:],
                "guard": ({"busy": bool(self.position.get("guard_busy")) and self.position.get("guard_side") != "take",
                           "holds": int(self.position.get("holds") or 0),
                           "next_in_s": max(0, int(_f(self.position.get("guard_next")) - time.time()))
                           if self.position.get("guard_next") else None,
                           "placeholder": bool(self.position.get("levels_placeholder"))}
                          if self.position else None),
                "guards": self.guards[-10:],
                # v5.3 W2: мягкий тейк — FLASH думает у тейка / сколько раз «подержали» / срок следующего вопроса
                "soft_take": self._soft_take_on(),
                "take_guard": ({"busy": bool(self.position.get("guard_busy")) and self.position.get("guard_side") == "take",
                                "holds": int(self.position.get("take_holds") or 0),
                                "max_holds": int(_setting("PYTHIA_SOFT_TAKE_MAX_HOLDS", 2)),
                                "lock_pct": float(_setting("PYTHIA_TAKE_LOCK_PCT", 50.0)),
                                "next_in_s": max(0, int(_f(self.position.get("take_next")) - time.time()))
                                if self.position.get("take_next") else None}
                               if self.position else None),
                "last_action": self.last_action,
                "last_review": self.last_review,
                "next_review_in_s": max(0, int(self.review_ts - time.time())),
                "market_alive": self._market_alive(),
                # v5.3 фаза 3 (панель проблем): отказы биржи и бэкофф, свежесть петли и стакана
                "entry_fail": int(self._entry_fail or 0),
                "no_entry_in_s": max(0, int(_f(getattr(self, "no_entry_until", 0.0)) - time.time())) or None,
                "tick_ts": self._last_tick_ts or None,
                "book_age_s": (round(time.time() - self._book_ts, 1) if self._book_ts else None),
                "market": self.market,                  # рыночные часы: статус биржи (None — выключены)
                "market_closed_since": self._closed_since or None,
                # W4: неподтверждённый трос / ПАНИКА упёрлась (нужен force) / state-файл отложен
                "stop_request": bool((self.position or {}).get("stop_request")),
                "stop_err": (self.position or {}).get("stop_err"),
                "panic_blocked": self.panic_blocked or None,
                "panic_force": self.panic_force or None,
                "state_note": self._state_note,
                "killswitch": sr, "frame": FRAME}

    def _plan_gates(self, plan: dict | None) -> list[dict]:
        """Проверки у двери по ЭТОМУ плану (plan_ts — план, о котором спросили). Ревью 5.4.2 (финал): ответ «не
        применено» (план сменился или обновлён свежим решением, пока дверь думала) — про прежний план, не в счёт."""
        if not plan:
            return []
        ts = plan.get("ts")
        return [g for g in self.gates
                if g.get("plan_ts") == ts and "не применено" not in str(g.get("applied") or "")]

    def _entry_gate_status(self) -> dict | None:
        """v5.4.1: {busy, next_in_s, decision, why, ts, entry, entry_kind} по текущему плану; плана нет → None."""
        plan = self.plan
        if not plan:
            return None
        mine = self._plan_gates(plan)
        last = mine[-1] if mine else None
        after = _f(plan.get("gate_after"))
        return {"busy": bool(plan.get("gate_busy")),
                "next_in_s": max(0, int(after - time.time())) if after > time.time() else None,
                "decision": (last or {}).get("decision"), "why": (last or {}).get("why"),
                "ts": (last or {}).get("ts"), "entry": plan.get("entry"), "entry_kind": plan.get("kind"),
                "checks": len(mine)}

    def _profit_status(self) -> dict | None:
        """v5.4.1: {busy, next_in_s, threshold_pct, progress_pct, gain_pct} по открытой позиции; нет → None."""
        pos = self.position
        if not pos:
            return None
        entry, take = _f(pos.get("entry")), _f(pos.get("take"))
        cur = self.prices[-1] if self.prices else entry
        sgn = 1.0 if pos.get("side") == "long" else -1.0
        progress = None
        if entry > 0 and take > 0 and abs(take - entry) > 1e-12:
            progress = round((cur - entry) * sgn / abs(take - entry) * 100.0, 1)
        nxt = _f(pos.get("profit_next"))
        return {"busy": bool(pos.get("profit_busy")),
                "next_in_s": max(0, int(nxt - time.time())) if nxt > time.time() else None,
                "threshold_pct": float(_setting("PYTHIA_PROFIT_THINK_PCT", 60.0)),
                "min_pct": float(_setting("PYTHIA_PROFIT_THINK_MIN_PCT", 1.0)),
                "progress_pct": progress,
                "gain_pct": round((cur / entry - 1.0) * 100.0 * sgn, 2) if entry > 0 else None,
                "locked": bool(pos.get("profit_lock"))}

    def panic(self, force: bool = False) -> None:
        """ПАНИКА: закрыть всё и встать. force=True — закрывать, даже если стопы биржи не сверить (список
        стопов недоступен): воля владельца важнее проверки; о непроверенных стопах он предупреждён."""
        self.panic_flag = True
        if force:
            self.panic_force = True
        if self._prepared or self.position or self.pending:
            self._save_state()

    def _spawn_background(self, coroutine) -> asyncio.Task:
        """Own child work so a stopped pilot cannot later apply an AI reply."""
        task = asyncio.get_running_loop().create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        if self.stopping:
            task.cancel()
        return task

    async def _cancel_background(self) -> None:
        current = asyncio.current_task()
        tasks = {task for task in self._background_tasks if task is not current}
        if self._guard_task and self._guard_task is not current:
            tasks.add(self._guard_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.difference_update(tasks)

    def stop(self) -> None:
        self.stopping = True
        if self.pending:
            self._cancel_entry = True
        if self._prepared or self.position or self.pending:
            self._save_state()
        for task in tuple(self._background_tasks):
            if not task.done():
                task.cancel()

    async def run(self) -> None:
        try:
            await self._run_loop()
        finally:
            # Cancellation used to bypass all cleanup below the while loop.
            # Wait until children release clients and stop mutating this pilot.
            await self._cancel_background()

    async def _run_loop(self) -> None:
        ok = await self.prepare()
        if not ok:
            return
        # v5.3 фаза 3 (проверяющий): ПАНИКА, а следом СТОП в тот же тик — стоп не отменяет панику: петля живёт,
        # пока тормоза (0б) не закроют позицию/заявку, иначе «закрыть всё» молча терялось, позиция оставалась
        while (not self.stopping or self.pending or any((self.position or {}).get(key) for key in ("exit_order", "stop_request")) or
               (self.panic_flag and self.position)):
            try:
                lp = await tinkoff.last_price(self.figi)
                px = _f((lp or {}).get("price"))
                book = None
                if not self._market_closed():                # закрытый рынок: стакан мёртв, не дёргаем
                    try:
                        book = await tinkoff.orderbook(self.figi, depth=10)
                    except Exception:                        # noqa: BLE001
                        pass
                # ПАНИКА/killswitch обязаны исполняться и БЕЗ свежей цены
                if px <= 0 and (self.panic_flag or self.pending or any((self.position or {}).get(key) for key in ("exit_order", "stop_request")) or
                                (self.session_risk and self.session_risk.locked)):
                    px = (self.prices[-1] if self.prices
                          else _f((self.position or {}).get("entry")) or _f((self.pending or {}).get("price")))
                if px > 0:
                    await self.tick(px, book)
            except Exception as e:                           # noqa: BLE001
                log.warning("тик ИИ-пилота споткнулся: %s", str(e)[:120])
            if self._market_closed():                        # рынок закрыт → петля тикает реже
                if self.stopping or self.panic_flag:
                    # Settlement may outlive stop/panic while the exchange is
                    # closed. Still yield and pace requests instead of spinning.
                    await asyncio.sleep(TICK_SEC)
                else:
                    left = float(_setting("PYTHIA_CLOSED_TICK_SEC", 30))   # (кусками: паника/стоп ловятся сразу)
                    while left > 0 and not (self.stopping or self.panic_flag):
                        await asyncio.sleep(min(TICK_SEC, left))
                        left -= TICK_SEC
            else:
                await asyncio.sleep(TICK_SEC)
        # аккуратный выход: заявку снять (частичка — в учёт), позицию НЕ
        # закрываем сами (решение владельца), трос остаётся на бирже
        gt = self._guard_task
        if gt and not gt.done():
            gt.cancel()
        part, po = await self._cancel_pending()
        if part > 0 and po:
            await self._absorb_fill(part, po)
        if self.position:
            if not self.position.get("stop_id") and not self.position.get("exit_order"):
                await self._replace_stop(self.position)
            if self.position.get("stop_id"):
                log.warning("ИИ-пилот %s остановлен: позиция %s %d лот ОСТАЁТСЯ "
                            "под биржевым тросом — веди сам или включи пилот",
                            self.base, self.position["side"], self.position["lots"])
            else:
                # v5.4.1: стопы только в программе — без пилота позиция не защищена ничем (осознанный выбор владельца)
                log.warning("ИИ-пилот %s остановлен: позиция %s %d лот ОСТАЁТСЯ БЕЗ ЗАЩИТЫ — стопов на бирже нет "
                            "(PYTHIA_EXCHANGE_STOP=0), веди сам или включи пилот",
                            self.base, self.position["side"], self.position["lots"])


# ══════════════════════════════════════════════════════════════════════════
# Self-тест: вся машина без сети (фейковый брокер + фейковый ИИ)
# ══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":

    class FakeBroker:
        """Мини-биржа: заявки, судьба, стопы, портфель. Ничего в сеть."""
        mode = "real"

        def __init__(self):
            self.placed, self.stops, self.cancelled = [], [], []
            self.stop_cancels = []
            self.fill_next = True            # place → сразу FILL?
            self.place_ok = True             # биржа принимает заявки?
            self.cancel_stop_ok = True       # трос снимается?
            self.partial: dict = {}          # order_id → exec_lots
            self.pf_positions = []           # портфель для сверок
            self.pf_cash = None
            self._n = 0

        async def place(self, figi, direction, lots, price=None, tag=""):
            if not self.place_ok:
                return {"ok": False, "error": "биржа закрыта (тест)"}
            self._n += 1
            oid = f"F-{self._n}"
            self.placed.append({"order_id": oid, "figi": figi,
                                "direction": direction, "lots": lots,
                                "price": price, "tag": tag})
            return {"ok": True, "order_id": oid}

        async def order_state(self, oid):
            if oid in self.partial:
                return {"ok": True, "filled": False, "status": "PARTIAL",
                        "exec_lots": self.partial[oid]}
            # This fixture controls entry latency; market exits explicitly fill.
            market = any(order["order_id"] == oid and order["price"] is None for order in self.placed)
            filled = self.fill_next or market
            return {"ok": True, "filled": filled, "status": "FILL" if filled else "NEW"}

        async def cancel(self, oid):
            self.cancelled.append(oid)
            return {"ok": True}

        async def place_stop(self, figi, direction, lots, stop_price, tag=""):
            self.stops.append({"figi": figi, "direction": direction,
                               "lots": lots, "stop": stop_price})
            return {"ok": True, "stop_order_id": f"S-{len(self.stops)}"}

        async def cancel_stop(self, sid):
            self.stop_cancels.append(sid)
            if not self.cancel_stop_ok:
                return {"ok": False, "error": "stop already executed"}
            return {"ok": True}

        async def portfolio(self):
            return {"mode": "real", "cash": self.pf_cash,
                    "positions": self.pf_positions}

    def mk(dep=100000.0, go=12000.0, tick=1.0, go_sell=None, cls=None):
        """Реалистичный фьючерс: ГО ≈ 13% номинала 90000 (плечо ~7.5).
        cls — класс пилота (по умолчанию базовый AIPilot; SoftPilot — для мягкого стопа)."""
        p = (cls or AIPilot)("TEST", deposit=dep, broker=FakeBroker())
        p.figi, p.asset_class = "FIGI-T", "futures"
        p.go_per_lot, p.go_sell = go, go_sell
        p.tick_size, p.point_value = tick, 1.0
        p.deposit = dep
        p.session_risk = trader_risk.SessionRisk(dep)
        p._sr_day = p._msk_day()
        p._state_path = None                 # тестам файл не нужен
        return p

    BOOK = {"best_bid": 89999.0, "best_ask": 90001.0}

    class FakeClock:
        """Рыночные часы без сети: открыто/закрыто по флагу."""
        open = True
        next_in = 4 * 3600
        calls = 0

        @staticmethod
        def enabled():
            return True

        @classmethod
        async def status(cls, t, ac, iid=None):
            cls.calls += 1
            return {"open": cls.open, "reason": "торги идут" if cls.open else "выходной — торгов нет",
                    "session": "основная" if cls.open else "выходной",
                    "next_open_ts": None if cls.open else time.time() + cls.next_in,
                    "next_open_in_s": None if cls.open else cls.next_in,
                    "next_open_msk": None if cls.open else "10:00 МСК", "source": "tinkoff", "ts": time.time()}

        @staticmethod
        def describe(st):
            return ("рынок открыт: торги идут" if (st or {}).get("open") else
                    "рынок закрыт до 10:00 МСК (выходной — торгов нет) — вход возможен только с открытия")

    globals()["market_clock"] = FakeClock

    def ex(do="BUY", entry=None, take=None, inv=None, why="т"):
        return {"exec": {"do": do, "entry": entry, "take": take,
                         "invalidation": inv, "why": why}}

    async def main():
        try:
            from . import config as _cfg0
        except ImportError:
            import config as _cfg0
        # ревью 5.4.2: ручки «свободного пилота» — на умолчаниях (data/config_user.json и окружение владельца не роняют тест)
        _cfg0.pin_free_pilot_defaults()
        # блоки 1–29 писались под трос НА БИРЖЕ (5.2–5.4.0); умолчание 5.4.1 — стопы только в программе (блок 30)
        _cfg0.PYTHIA_EXCHANGE_STOP = True
        # 1) приказ принимается; без exec / с мусорными сторонами — отказ
        p = mk()
        assert p.adopt_forecast(ex("BUY", entry=89500.0, take=91000.0,
                                   inv=89000.0)) and p.state == "ЗАСАДА"
        p1 = mk()
        assert p1.adopt_forecast({"action": "LONG"}) is False
        assert p1.adopt_forecast(ex("BUY", entry=90000, inv=91000)) is False, \
            "BUY со стопом ВЫШЕ входа обязан быть отвергнут"
        assert p1.adopt_forecast(ex("SELL", entry=90000, inv=89000)) is False, \
            "SELL со стопом НИЖЕ входа обязан быть отвергнут"
        assert p1.adopt_forecast(ex("BUY", entry=90000, inv=-5)) is False
        assert p1.adopt_forecast(ex("BUY", entry=90000, take=89000,
                                    inv=88000)) is False   # тейк ниже входа

        # 2) максимум: фьючерс int(98000//12000)=8; шорт — по ГО продажи;
        #    изоляция режет ГО ручных лотов
        assert p.max_lots(90000.0, "long") == 8
        p2 = mk(go=12000.0, go_sell=14000.0)
        assert p2.max_lots(90000.0, "short") == 7           # 98000//14000
        p.foreign_lots = 2
        assert p.max_lots(90000.0, "long") == 6
        p.foreign_lots = 0

        # 2а) акция: плечо по dlong/dshort; нет ставок → на свои
        s = mk()
        s.asset_class, s.go_per_lot, s.point_value = "share", None, 10.0
        s.dlong, s.dshort = 0.25, 0.5
        assert s.max_lots(100.0, "long") == 392   # 98000//(100·10·0.25)
        assert s.max_lots(100.0, "short") == 196
        s.dlong = 0.0                              # не маржинальная
        assert s.max_lots(100.0, "long") == 98    # честно на свои

        # 3) рыночный гейт: мёртвый стакан → входа нет; живой → засада бьёт
        await p.tick(90020.0, None)               # стакана не было вовсе
        assert p.pending is None and "мёртв" in p.last_action
        await p.tick(95000.0, BOOK)               # далеко от засады
        assert p.pending is None and p.state == "ЗАСАДА"
        await p.tick(89504.0, BOOK)               # 89504 ≤ 89500+5·1 → бьём
        assert p.pending is not None
        e = p.broker.placed[-1]
        assert e["price"] is not None and BOOK["best_bid"] <= e["price"] <= BOOK["best_ask"]
        assert e["lots"] == 8 and e["direction"] == BUY
        assert p.pending["take"] == 91000.0       # заявка несёт СВОИ уровни

        # 4) исполнение → позиция, трос на бирже (снап к шагу), план потреблён
        await p.tick(89600.0, BOOK)
        assert p.position and p.position["lots"] == 8 and p.plan is None
        assert p.broker.stops[-1] == {"figi": "FIGI-T", "direction": SELL,
                                      "lots": 8, "stop": 89000.0}

        # 5) ПОБЕДА: тейк → закрыто всё рыночным, P/L учтён, реанализ пошёл
        fired = []

        async def cb():
            fired.append(1)

        p.reanalyze_cb = cb
        entry_px = p.position["entry"]
        await p.tick(91005.0, BOOK)
        assert p.position is None
        close = p.broker.placed[-1]
        assert close["price"] is None and close["direction"] == SELL and close["lots"] == 8
        assert abs(p.pnls[-1] - (91005.0 - entry_px) * 8) < 1e-6
        await asyncio.sleep(0)
        assert fired, "после победы обязан стартовать свежий разбор"

        # 6) МАРЖЕВОЙ ДОЗОР (находка веера): позиция на макс НЕ гибнет от
        #    шума; глубокая просадка → ЧАСТИЧНОЕ ужатие, трос на остаток
        p5 = mk()
        p5.adopt_forecast(ex("BUY", inv=1.0))     # стоп далеко: тестируем дозор
        await p5.tick(90000.0, BOOK)
        await p5.tick(90000.0, BOOK)
        assert p5.position and p5.position["lots"] == 8
        ep = p5.position["entry"]                 # 90000 (внутрь спреда)
        # мелкий минус (−0.1% цены) дозор НЕ трогает (раньше убивал бы всё)
        await p5.tick(ep - 90.0, BOOK)
        assert p5.position and p5.position["lots"] == 8, \
            "дозор не должен стрелять на шуме — находка веера"
        # глубже: equity=100000+(88000−ep)·8=84000 < 0.9·96000=86400 → ужатие
        await p5.tick(88000.0, BOOK)
        assert p5.position and p5.position["lots"] == 6, p5.position
        red = [x for x in p5.broker.placed if x["tag"] == "aip-reduce"]
        assert red and red[-1]["lots"] == 2 and red[-1]["price"] is None
        await p5.tick(88000.0, BOOK)              # restop отработал
        assert p5.broker.stops[-1]["lots"] == 6

        # 7) ДВОЙНОЕ ЗАКРЫТИЕ УБИТО: трос не снялся (уже исполнен на бирже),
        #    портфель пуст → рыночный НЕ шлётся, голого шорта нет
        p6 = mk()
        p6.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0))
        await p6.tick(90000.0, BOOK)
        await p6.tick(90000.0, BOOK)
        assert p6.position and p6.position.get("stop_id")
        p6.broker.cancel_stop_ok = False          # стоп уже исполнился
        p6.broker.pf_positions = [{"figi": "FIGI-T", "qty": 0}]   # биржа пуста
        n_orders = len(p6.broker.placed)
        await p6.tick(88900.0, BOOK)              # цена за стопом → закрытие
        assert p6.position is None
        assert len(p6.broker.placed) == n_orders, \
            "рыночный ордер НЕ должен уходить: биржевой трос уже всё закрыл"
        assert abs(p6.pnls[-1] - (89000.0 - p6.broker.stops[-1]["stop"]
                                  * 0 - (90000.0)) * 8) < 1e9  # pnl по стопу:
        assert abs(p6.pnls[-1] - (89000.0 - 90000.0) * 8) < 1e-6

        # 8) отказ биржи на закрытии: позиция НЕ исчезает из учёта, добивается
        p7 = mk()
        p7.adopt_forecast(ex("BUY", take=91000.0, inv=89000.0))
        await p7.tick(90000.0, BOOK)
        await p7.tick(90000.0, BOOK)
        p7.broker.place_ok = False                # биржа отбивает всё
        await p7.tick(91500.0, BOOK)              # тейк, но закрыть нельзя
        assert p7.position is not None and p7.position.get("close_fail")
        p7.broker.place_ok = True
        await p7.tick(91500.0, BOOK)              # добили
        assert p7.position is None and p7.pnls

        # 9) ПАНИКА при частично налитой заявке: частичка закрывается, не
        #    теряется (находка веера)
        p8 = mk()
        p8.broker.fill_next = False
        p8.adopt_forecast(ex("BUY", inv=89000.0))
        await p8.tick(90000.0, BOOK)
        oid = p8.pending["order_id"]
        p8.broker.partial[oid] = 3                # налили 3 из 8
        p8.panic()
        await p8.tick(90000.0, BOOK)
        assert p8.state == "СТОП" and p8.position is None
        closes = [x for x in p8.broker.placed if x["tag"] == "aip-close"]
        assert closes and closes[-1]["lots"] == 3 and closes[-1]["direction"] == SELL, \
            "частичка обязана быть закрыта, а не потеряна"

        # 10) новый план поверх летящей заявки: заявка снимается, частичка
        #     живёт со СТАРЫМИ уровнями, потом флип по новому плану
        p9 = mk()
        p9.broker.fill_next = False
        p9.adopt_forecast(ex("BUY", take=91000.0, inv=89000.0))
        await p9.tick(90000.0, BOOK)
        p9.broker.partial[p9.pending["order_id"]] = 5
        assert p9.adopt_forecast(ex("SELL", take=88000.0, inv=91000.0))
        assert p9._cancel_entry
        await p9.tick(90000.0, BOOK)              # снятие + частичка в позицию
        assert p9.position and p9.position["side"] == "long" \
            and p9.position["lots"] == 5
        assert p9.position["take"] == 91000.0, "уровни — из СНИМКА заявки"
        assert p9.plan and p9.plan["side"] == "short", "новый план жив"
        p9.broker.fill_next = True
        await p9.tick(90000.0, BOOK)              # флип: закрыли лонг
        assert p9.position is None and "флип" in p9.last_action
        await p9.tick(90000.0, BOOK)              # вошли в шорт
        await p9.tick(90000.0, BOOK)
        assert p9.position and p9.position["side"] == "short"

        # 11) вход в мёртвую идею запрещён: цена уже за invalidation
        pa = mk()
        fired2 = []

        async def cb2():
            fired2.append(1)

        pa.reanalyze_cb = cb2
        pa.adopt_forecast(ex("BUY", entry=89500.0, inv=89000.0))
        await pa.tick(88500.0, BOOK)              # цена ниже стопа
        assert pa.pending is None and pa.plan is None
        await asyncio.sleep(0)
        assert fired2, "мёртвая идея → свежий разбор"

        # 12) REJECTED-штурм убит: бэкофф, после серии — план сброшен
        pb = mk()
        pb.adopt_forecast(ex("BUY", inv=89000.0))
        pb.broker.place_ok = False
        for _ in range(ENTRY_FAIL_MAX):
            pb.no_entry_until = 0.0               # тест: бэкофф скручен
            await pb.tick(90000.0, BOOK)
        assert pb.plan is None and "сброшен" in pb.last_action

        # 13) эскалация «точно заходим»: лимит по best ask, рыночных входов нет
        pc = mk()
        pc.broker.fill_next = False
        pc.adopt_forecast(ex("BUY", inv=89000.0))
        await pc.tick(90000.0, BOOK)
        for _ in range(MAX_REPRICINGS + 1):
            assert pc.pending
            pc.pending["ts"] -= (ENTRY_TTL_SEC + 1)
            await pc.tick(90000.0, BOOK)
        last = pc.broker.placed[-1]
        assert last["price"] == BOOK["best_ask"]
        entries = [x for x in pc.broker.placed if x["tag"] in ("aip-entry", "aip-cross")]
        assert all(x["price"] is not None for x in entries), \
            "рыночных ВХОДОВ быть не должно (закон «Абсолюта»)"

        # 14) перепроверка: нестрогий разбор решений + аварийный стоп при
        #     кривом invalidation + дрейф цены отменяет вход
        pd = mk()
        pd.adopt_forecast(ex("SELL", take=88000.0, inv=91000.0))
        await pd.tick(90000.0, BOOK)
        await pd.tick(90000.0, BOOK)
        assert pd.position and pd.position["side"] == "short"

        async def none_news():
            return ""

        async def none_verdict():
            return "вердикт"

        pd._gather_news = none_news
        pd._last_verdict = none_verdict
        answers = [{"choice": "ЖДЁМ", "why": "план жив"}]

        async def fake_ask(*a, **k):
            return answers[-1]

        pd._ai_ask_json = fake_ask
        await pd._review(90000.0)
        assert pd.position is not None
        fired3 = []

        async def cb3():
            fired3.append(1)

        pd.reanalyze_cb = cb3
        answers.append({"choice": "ЗАКРЫТЬ ВСЁ И ЖДАТЬ", "why": "слом"})
        await pd._review(90000.0)                 # нестрогая формулировка
        assert pd.position is None
        await asyncio.sleep(0)
        assert fired3
        pd._reanalyzing = False
        pd.state = "ЖДУ_ПЛАН"
        answers.append({"choice": "КУПИТЬ СЕЙЧАС!", "why": "разворот",
                        "invalidation": 95000.0})   # стоп ВЫШЕ цены — кривой
        await pd._review(90000.0)
        assert pd.plan and pd.plan["side"] == "long"
        assert abs(pd.plan["invalidation"] - 90000.0 * (1 - EMERGENCY_STOP_FRAC)) < 1e-3
        # дрейф: ИИ думал при 90000, цена уже 91000 (>1%) → вход отменён
        pd.plan = None
        pd._reanalyzing = False
        pd.prices.append(91000.0)
        fired3.clear()
        answers.append({"choice": "КУПИТЬ_СЕЙЧАС", "why": "поехали",
                        "invalidation": 89000.0})
        await pd._review(90000.0)
        assert pd.plan is None, "по протухшему решению не входим"
        await asyncio.sleep(0)
        #    ПЕЙСИНГ ДНЕВНОЙ СМЕНЫ: разбор только что был — второй подряд не
        #    жжём, просьба ОТКЛАДЫВАЕТСЯ (не теряется)
        assert not fired3 and pd._reanalyze_pending, \
            "второй разбор внутри окна пейсинга обязан быть отложен"
        #    окно открылось → пилот сам добирает отложенный разбор
        pd._last_reanalyze_ts -= (REANALYZE_GAP_SEC + 1)
        await pd.tick(91000.0, BOOK)
        await asyncio.sleep(0)
        assert fired3 and pd._reanalyze_pending is None, \
            "как только окно открылось — отложенный разбор обязан уйти"

        # 15) killswitch: закрыл, СТОП, приказы не принимаются; новый день —
        #     сброс и снова в строю
        pe = mk()
        pe.adopt_forecast(ex("BUY", inv=89000.0))
        await pe.tick(90000.0, BOOK)
        await pe.tick(90000.0, BOOK)
        assert pe.position
        pe.session_risk.locked = True
        pe.session_risk.reason = "тест-блок"
        await pe.tick(90000.0, BOOK)
        assert pe.position is None and pe.state == "СТОП"
        assert pe.adopt_forecast(ex("BUY", inv=89000.0)) is False, \
            "в killswitch свежий приказ не принимается"
        pe._sr_day -= 1                           # «наступил» новый день МСК
        await pe.tick(90000.0, BOOK)
        assert pe.state == "ЖДУ_ПЛАН" and not pe.session_risk.locked

        # 16) сверка: внешнее закрытие снимает стоп-сироту и пишет P/L
        pf_ = mk()
        pf_.adopt_forecast(ex("BUY", inv=89000.0))
        await pf_.tick(90000.0, BOOK)
        await pf_.tick(90000.0, BOOK)
        assert pf_.position and pf_.position.get("stop_id")
        pf_.broker.pf_positions = [{"figi": "FIGI-T", "qty": 0}]
        pf_.broker.pf_cash = 97000.0
        pf_._tick_n = RECONCILE_EVERY - 1
        await pf_.tick(90000.0, BOOK)             # сверка сработала
        assert pf_.position is None
        assert pf_.broker.stop_cancels, "стоп-сирота обязан быть снят"
        assert pf_._live_cash == 97000.0 and pf_.pnls

        # 17) обновление позиции свежим вердиктом той же стороны
        pg = mk()
        pg.adopt_forecast(ex("BUY", inv=89000.0, take=91000.0))
        await pg.tick(90000.0, BOOK)
        await pg.tick(90000.0, BOOK)
        assert pg.adopt_forecast(ex("BUY", inv=89500.0, take=92000.0))
        assert pg.position["invalidation"] == 89500.0 and pg.position["take"] == 92000.0
        await pg.tick(90000.0, BOOK)              # restop
        assert pg.broker.stops[-1]["stop"] == 89500.0

        # 18) снап цены к шагу: кривой уровень не уходит на биржу как есть
        ph = mk(tick=0.5)
        assert ph._snap(90000.37) in (90000.0, 90000.5)

        # 19) ДНЕВНАЯ СМЕНА: приказ протухает — по утренней засаде вечером
        #     не заходим, просим свежий разбор (пилот работает весь день)
        pi = mk()
        fired4 = []

        async def cb4():
            fired4.append(1)

        pi.reanalyze_cb = cb4
        pi.adopt_forecast(ex("BUY", entry=89500.0, take=91000.0, inv=89000.0))
        assert pi.state == "ЗАСАДА"
        pi.plan["ts"] -= (PLAN_TTL_SEC + 1)       # «прошло полдня»
        await pi.tick(89504.0, BOOK)              # цена у засады, но приказ стар
        assert pi.pending is None, "по протухшему приказу вход запрещён"
        assert pi.plan is None and pi.state in ("ЖДУ_ПЛАН", "ПЕРЕАНАЛИЗ")
        await asyncio.sleep(0)
        assert fired4, "протухший приказ → свежий разбор"
        assert pi.analyses == 1                   # счётчик смены живой

        # 20) рамка честности
        st = pg.status()
        assert "18+" in st["frame"] and st["pilot"] == "ИИ-ПИЛОТ"
        assert st["adopt_account"] is True and st["soft_stop"] is False and st["guards"] == []

        # ══ v5.2 «ВЕСЬ СЧЁТ» ══
        # 21) размер считает БИРЖА: GetMaxLots даёт 15 (локально было бы 8) → вход 15;
        #     после исполнения — добор, пока биржа даёт (2, потом 0), не больше TOPUP_MAX раз
        class MaxBroker(FakeBroker):
            def __init__(self):
                super().__init__()
                self.mx = {"buy": 15, "sell": 12}
                self.mx_calls = 0

            async def max_lots(self, figi, price=None):
                self.mx_calls += 1
                return dict(self.mx)

        pm = mk(); pm.broker = MaxBroker(); pm.deposit_override = None   # весь счёт, без ограничения владельца
        assert pm.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0))
        await pm.tick(90000.0, BOOK)
        assert pm.pending and pm.pending["lots"] == 15 and pm._sized_by_broker, pm.pending
        assert "размер дала биржа" in pm.last_action
        await pm.tick(90000.0, BOOK)                # FILL → позиция 15, добор разрешён
        assert pm.position and pm.position["lots"] == 15 and pm.position["topup_left"] == 2
        assert pm._mx is None, "после исполнения кэш GetMaxLots сброшен — перед добором биржу спросят заново"
        pm.broker.mx = {"buy": 2, "sell": 0}
        pm.position["last_fill_ts"] -= TOPUP_GAP_SEC + 1
        pm._tick_n = 5
        await pm.tick(90000.0, BOOK)                # тик кратен 6 → добор 2 лота
        assert pm.pending and pm.pending.get("topup") and pm.pending["lots"] == 2, pm.pending
        tp = [x for x in pm.broker.placed if x["tag"] == "aip-topup"]
        assert tp and tp[-1]["lots"] == 2 and tp[-1]["price"] is not None
        await pm.tick(90000.0, BOOK)                # FILL добора
        assert pm.position["lots"] == 17 and pm.position["topup_left"] == 1 and "ДОБРАЛ" in pm.last_action
        assert pm.broker.stops[-1]["lots"] == 17, "трос — на весь объём"
        pm.broker.mx = {"buy": 0, "sell": 0}
        pm.position["last_fill_ts"] -= TOPUP_GAP_SEC + 1
        pm._tick_n = 11
        await pm.tick(90000.0, BOOK)
        assert pm.pending is None and pm.position["topup_left"] == 0, "биржа не даёт — добор закончен"
        #     ревью 5.4.2: приказ HOLD — держать как есть БЕЗ добора (биржа снова даёт 4 — не берём), уровни обновлены;
        #     null — прежний уровень; уровень не с той стороны — приказ отклонён, позиция не тронута
        pm.broker.mx = {"buy": 4, "sell": 0}
        n_pl = len(pm.broker.placed)
        assert pm.adopt_forecast({"exec": {"do": "HOLD", "entry": None, "take": 96000.0, "invalidation": 89500.0}})
        assert pm.position["invalidation"] == 89500.0 and pm.position["take"] == 96000.0 and pm.plan is None
        assert pm.position["topup_left"] == 0 and "без добора" in pm.last_action, pm.last_action
        pm.position["last_fill_ts"] -= TOPUP_GAP_SEC + 1
        pm._tick_n = 17
        await pm.tick(90000.0, BOOK)
        assert len(pm.broker.placed) == n_pl and pm.pending is None and pm.position["lots"] == 17, "HOLD — не добор"
        assert pm.adopt_forecast({"exec": {"do": "HOLD", "take": None, "invalidation": None}})
        assert pm.position["invalidation"] == 89500.0 and pm.position["take"] == 96000.0 and "(прежний)" in pm.last_action
        assert not pm.adopt_forecast({"exec": {"do": "HOLD", "invalidation": 96500.0}}), "стоп выше тейка 96000 у лонга"
        assert pm.position["invalidation"] == 89500.0 and "HOLD отклонён" in pm.last_action, pm.last_action
        #     ревью 5.4.2 (финал): остаток авто-добора прошлого входа HOLD снимает, заявку добора в полёте — снимает тиком
        pm.position["topup_left"] = 2
        pm.pending = {"side": "long", "lots": 4, "price": 90010.0, "ts": time.time(), "attempts": 0, "topup": True,
                      "take": 96000.0, "invalidation": 89500.0, "why": "добор до максимума"}
        pm._cancel_entry = False
        assert pm.adopt_forecast({"exec": {"do": "HOLD", "take": None, "invalidation": None}})
        assert pm.position["topup_left"] == 0 and pm._cancel_entry, "HOLD — не добор: остаток и заявка добора сняты"
        pm.pending, pm._cancel_entry = None, False
        #     живая цена HOLD не отклоняет: стоп, который цена уже прошла (совет думал по 90000, цена 89400), — триггер
        pm.prices.append(89400.0)
        assert pm.adopt_forecast({"exec": {"do": "HOLD", "take": None, "invalidation": 89450.0}}), pm.last_action
        assert pm.position["invalidation"] == 89450.0 and "уже за стопом" in pm.last_action, pm.last_action
        await pm.tick(89400.0, BOOK)                  # базовый пилот: стоп есть стоп — закрыл по триггеру
        assert pm.position is None and "аварийный стоп @89450" in pm.last_action, pm.last_action
        p_nh = mk()
        p_nh._prepared = True                        # до prepare() HOLD ждёт позицию со счёта — здесь её уже нет
        assert not p_nh.adopt_forecast({"exec": {"do": "HOLD", "invalidation": 89000.0}}) and "позиции уже нет" in p_nh.last_action
        #     HOLD до prepare(): уровни совета — к позиции со счёта после проверки сторон (не временные 2 %)
        p_h = mk()
        assert p_h.adopt_forecast({"exec": {"do": "HOLD", "take": 95000.0, "invalidation": 89000.0}}) and p_h._pending_hold
        assert p_h._absorb_account(3, 90000.0, None, "на счёте при старте") and p_h._pending_hold is None
        assert p_h.position["invalidation"] == 89000.0 and p_h.position["take"] == 95000.0, p_h.position
        assert not p_h.position.get("levels_placeholder") and "уровни приказа HOLD: стоп 89000, тейк 95000" in p_h.last_action
        #     …не та сторона (на счёте шорт, уровни лонга) — временный стоп и без тейка, причина в last_action
        p_h2 = mk()
        assert p_h2.adopt_forecast({"exec": {"do": "HOLD", "take": 95000.0, "invalidation": 89000.0}})
        assert p_h2._absorb_account(-3, 90000.0, None, "на счёте при старте")
        assert p_h2.position["levels_placeholder"] and p_h2.position["take"] is None, p_h2.position
        assert abs(p_h2.position["invalidation"] - 90000 * (1 + ADOPT_STOP_PCT / 100)) < 1e-6 and "HOLD не принят" in p_h2.last_action
        #     …только тейк: временный стоп, тейк совета
        p_h3 = mk()
        assert p_h3.adopt_forecast({"exec": {"do": "HOLD", "take": 95000.0, "invalidation": None}})
        assert p_h3._absorb_account(3, 90000.0, None, "на счёте при старте")
        assert p_h3.position["take"] == 95000.0 and p_h3.position["levels_placeholder"], p_h3.position
        #     …новый приказ до подготовки снимает HOLD: уровни — его
        p_h4 = mk()
        assert p_h4.adopt_forecast({"exec": {"do": "HOLD", "take": 95000.0, "invalidation": 89000.0}})
        assert p_h4.adopt_forecast({"exec": {"do": "CLOSE"}}) and p_h4._pending_hold is None
        #     ограничение владельца (deposit при запуске) режет сверху даже число биржи
        pcap = mk(); pcap.broker = MaxBroker(); pcap.deposit_override = 30000.0
        pcap.adopt_forecast(ex("BUY", inv=89000.0))
        await pcap.tick(90000.0, BOOK)
        assert pcap.pending and pcap.pending["lots"] == int(30000 * (1 - RESERVE_FRAC) // 12000) == 2, pcap.pending

        # 22) ВЕСЬ СЧЁТ: позиция на счёте принимается как своя (уровни временные, PRO скоро);
        #     владелец докупил → больше, продал → меньше, перевернул → перевёрнута
        pa2 = mk()
        assert pa2._absorb_account(5, 90000.0, 90000.0, "тест") and pa2.position
        assert pa2.position["side"] == "long" and pa2.position["lots"] == 5 and pa2.position["adopted"]
        assert pa2.position["levels_placeholder"] and abs(pa2.position["invalidation"] - 90000 * (1 - ADOPT_STOP_PCT / 100)) < 1e-6
        assert pa2.review_ts <= time.time() + 91 and "ПРИНЯЛ позицию со счёта" in pa2.last_action
        pa2.broker.pf_positions = [{"figi": "FIGI-T", "qty": 8, "avg": 90100.0}]   # владелец докупил 3
        pa2._tick_n = RECONCILE_EVERY - 1
        await pa2.tick(90200.0, BOOK)
        assert pa2.position["lots"] == 8 and abs(pa2.position["entry"] - 90100.0) < 1e-6, pa2.position
        assert "докупил 3" in pa2.last_action and pa2.foreign_lots == 0
        await pa2.tick(90200.0, BOOK)               # restop → трос на 8
        assert pa2.broker.stops[-1]["lots"] == 8
        pa2.broker.pf_positions = [{"figi": "FIGI-T", "qty": 6, "avg": 90100.0}]   # владелец продал 2
        n_pnl = len(pa2.pnls)
        pa2._tick_n = RECONCILE_EVERY - 1
        await pa2.tick(90300.0, BOOK)
        assert pa2.position["lots"] == 6 and len(pa2.pnls) == n_pnl + 1 and abs(pa2.pnls[-1] - (90300 - 90100) * 2) < 1e-6
        pa2.broker.pf_positions = [{"figi": "FIGI-T", "qty": -4, "avg": 90300.0}]  # владелец перевернул
        pa2._tick_n = RECONCILE_EVERY - 1
        await pa2.tick(90300.0, BOOK)
        assert pa2.position and pa2.position["side"] == "short" and pa2.position["lots"] == 4, pa2.position
        pa2.broker.pf_positions = [{"figi": "FIGI-T", "qty": 0}]                    # закрыл руками
        pa2._tick_n = RECONCILE_EVERY - 1
        await pa2.tick(90300.0, BOOK)
        assert pa2.position is None and "закрыла биржа/владелец" in pa2.last_action
        #     приказ той же стороны при принятой позиции — его уровни становятся уровнями позиции
        pa3 = mk(); pa3.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0))
        pa3._absorb_account(3, 90000.0, None, "старт")
        assert pa3.position["take"] == 95000.0 and pa3.position["invalidation"] == 89000.0 and not pa3.position.get("levels_placeholder")
        assert pa3.plan and pa3.plan["side"] == "long", "план остаётся — добор по нему"
        await pa3.tick(90000.0, BOOK)               # план «сейчас» той же стороны → добор (98000 − 3·ГО)//ГО = 5
        assert pa3.pending and pa3.pending.get("topup") and pa3.pending["lots"] == 5, pa3.pending
        await pa3.tick(90000.0, BOOK)
        assert pa3.position["lots"] == 8 and pa3.plan is None

        # 23) приказ CLOSE: закрыть позицию и стоять вне рынка
        pc2 = mk(); pc2.adopt_forecast(ex("BUY", inv=89000.0)); await pc2.tick(90000.0, BOOK); await pc2.tick(90000.0, BOOK)
        assert pc2.position
        assert pc2.adopt_forecast({"exec": {"do": "CLOSE", "why": "картина сломалась", "invalidation": 1}}) is True
        assert pc2._close_pending and pc2.plan is None
        await pc2.tick(90000.0, BOOK)
        assert pc2.position is None and "приказ совета: закрыть" in pc2.last_action and pc2.state == "ЖДУ_ПЛАН"
        # CLOSE до prepare(): позиция со счёта появится при подготовке — приказ ждёт её; после подготовки без позиции — вне рынка
        assert pc2.adopt_forecast({"exec": {"do": "CLOSE"}}) is True and "закрою, как только приму" in pc2.last_action
        assert pc2._close_pending and pc2.plan is None
        pc2._prepared = True
        assert pc2.adopt_forecast({"exec": {"do": "CLOSE"}}) is True and "стою вне рынка" in pc2.last_action
        assert pc2._close_pending is None
        pc2._prepared = False
        pc2.adopt_forecast({"exec": {"do": "CLOSE"}})
        assert pc2._close_pending
        assert pc2.adopt_forecast(ex("BUY", inv=89000.0)) is True and pc2._close_pending is None, "свежий BUY снимает старый CLOSE"

        # 24) разбор решений в позиции: ДОБРАТЬ / ПЕРЕВЕРНУТЬ / купить при лонге = добор, продать при лонге = закрыть
        assert AIPilot._parse_choice("ДОБРАТЬ", True, "long") == "ДОБРАТЬ"
        assert AIPilot._parse_choice("КУПИТЬ ещё", True, "long") == "ДОБРАТЬ"
        assert AIPilot._parse_choice("ПРОДАТЬ", True, "long") == "ЗАКРЫТЬ"
        assert AIPilot._parse_choice("продать ещё", True, "short") == "ДОБРАТЬ"
        assert AIPilot._parse_choice("ПЕРЕВЕРНУТЬ в шорт", True, "long") == "ПЕРЕВЕРНУТЬ"
        assert AIPilot._parse_choice("СЛИТЬ", True, "long") == "ЗАКРЫТЬ" and AIPilot._parse_choice("ЗАКРЫТЬ ВСЁ", True) == "ЗАКРЫТЬ"
        assert AIPilot._parse_choice("КУПИТЬ_СЕЙЧАС", False) == "КУПИТЬ_СЕЙЧАС"
        #     v5.4.2: латиница и синонимы — решения, а не молчаливый ЖДЁМ; пусто / непонятно / «НЕ …» / «… или …» → None
        for raw_c, in_p, side_c, want in (("BUY", False, None, "КУПИТЬ_СЕЙЧАС"), ("long", False, None, "КУПИТЬ_СЕЙЧАС"),
                                          ("SELL", False, None, "ПРОДАТЬ_СЕЙЧАС"), ("ШОРТ", False, None, "ПРОДАТЬ_СЕЙЧАС"),
                                          ("ЖДЁМ", False, None, "ЖДЁМ"), ("WAIT", False, None, "ЖДЁМ"),
                                          ("CLOSE", True, "long", "ЗАКРЫТЬ"), ("EXIT", True, "short", "ЗАКРЫТЬ"),
                                          ("ЗАФИКСИРОВАТЬ", True, "long", "ЗАКРЫТЬ"), ("SELL", True, "long", "ЗАКРЫТЬ"),
                                          ("BUY", True, "short", "ЗАКРЫТЬ"), ("СОВЕТ", False, None, "НОВЫЙ_АНАЛИЗ"),
                                          ("", False, None, None), ("", True, "long", None), ("ВОЙТИ", False, None, None),
                                          ("НЕ ПОКУПАТЬ, ЖДЁМ", False, None, "ЖДЁМ"), ("ДЕРЖАТЬ, не закрывать", True, "long", "ЖДЁМ"),
                                          ("КУПИТЬ или ЖДАТЬ", False, None, None),
                                          # v5.4.4 (разбор слов, ai_v5): «не закрывать» в позиции — держать
                                          ("НЕ ЗАКРЫВАТЬ", True, "long", "ЖДЁМ")):
            got = AIPilot._parse_choice(raw_c, in_p, side_c)
            assert got == want, (raw_c, in_p, side_c, got, want)
        assert AIPilot._parse_choice("КУПИТЬ", True, None) is None, "в позиции без стороны «купить» не угадываем"

        # 25) МЯГКИЙ СТОП: у триггера спрашивают FLASH; на бирже — аварийный трос дальше стопа;
        #     ЖДАТЬ → новый триггер, задача Совету; за аварийным тросом — закрытие без вопросов;
        #     СЛИТЬ → закрытие; предел «ждать» → закрытие
        class SoftPilot(AIPilot):
            SOFT_STOP = True
            answers: list = []
            asked: list = []
            handoffs: list = []

            async def _stop_guard(self, price, pos):
                self.asked.append(price)
                return self.answers.pop(0) if self.answers else {"decision": "СЛИТЬ", "why": "нет ответа"}

            def _guard_handoff(self, why):
                self.handoffs.append(why)

        async def settle_guard(pl, n=50):
            for _ in range(n):
                if not pl.position or not pl.position.get("guard_busy"):
                    return
                await asyncio.sleep(0.01)

        ps = SoftPilot("TEST", deposit=100000.0, broker=FakeBroker())
        ps.figi, ps.asset_class, ps.go_per_lot, ps.tick_size, ps.point_value = "FIGI-T", "futures", 12000.0, 1.0, 1.0
        ps.deposit = 100000.0; ps.session_risk = trader_risk.SessionRisk(100000.0); ps._sr_day = ps._msk_day(); ps._state_path = None
        ps.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0))
        await ps.tick(90000.0, BOOK); await ps.tick(90000.0, BOOK)
        pos = ps.position
        hard = round(89000.0 * (1 - 1.5 / 100))
        assert pos and pos["invalidation"] == 89000.0 and pos["inv0"] == 89000.0 and abs(pos["hard_stop"] - hard) < 1.0, pos
        assert abs(ps.broker.stops[-1]["stop"] - hard) < 1.0, "на бирже лежит аварийный трос, не триггер"
        assert ps.status()["soft_stop"] and abs(ps.status()["hard_stop"] - hard) < 1.0
        SoftPilot.answers[:] = [{"decision": "ЖДАТЬ", "why": "прокол стопа, лента за нас", "hold_until_price": 88400.0, "hold_minutes": 5}]
        await ps.tick(88950.0, BOOK)                # цена за триггером → вопрос FLASH
        assert pos.get("guard_busy") or SoftPilot.asked, "FLASH обязан быть спрошен"
        await settle_guard(ps)
        assert SoftPilot.asked == [88950.0] and ps.position is pos, "ЖДАТЬ — позиция жива"
        assert pos["holds"] == 1 and pos["invalidation"] == 88400.0 and pos["inv0"] == 89000.0, pos
        assert abs(pos["hard_stop"] - hard) < 1.0, "аварийный трос не двигается за FLASH"
        assert SoftPilot.handoffs and "лента за нас" in SoftPilot.handoffs[-1] and ps.guards[-1]["decision"] == "ЖДАТЬ"
        # ревью 5.4.3 (D5): принятый hold_until_price — новый триггер, вопрос у него сразу (срок hold_minutes не ждём)
        assert pos["guard_next"] == 0.0 and "у него спрошу снова" in ps.last_action and "ЖДАТЬ" in ps.last_action
        assert ps.guards[-1]["hold_until"] == 88400.0 and ps.guards[-1]["hold_minutes"] is None
        assert ps.guards[-1]["pos_side"] == "long", "сторона позиции — в записи ответа (память «за лонг»)"
        await ps.tick(88950.0, BOOK)                # выше нового триггера — тихо, без вопросов
        assert len(SoftPilot.asked) == 1 and ps.position is pos
        SoftPilot.answers[:] = [{"decision": "ЖДАТЬ", "why": "ещё терпим", "hold_until_price": 99999.0, "hold_minutes": 4}]
        await ps.tick(88390.0, BOOK)                # ниже нового триггера — вопрос сразу
        await settle_guard(ps)
        assert len(SoftPilot.asked) == 2 and pos["holds"] == 2 and ps.position is pos
        g2 = ps.guards[-1]                          # 99999 выше цены — не принят, не молча; триггер прежний, срок 4 мин
        assert g2["hold_until"] is None and g2["hold_until_ai"] == 99999.0 and "не принят" in g2["hold_note"], g2
        assert pos["invalidation"] == 88400.0 and "не принят" in ps.last_action and 230 < pos["guard_next"] - time.time() <= 240
        await ps.tick(88390.0, BOOK)                # за прежним триггером, срок не вышел — тихо
        assert len(SoftPilot.asked) == 2 and ps.position is pos
        await ps.tick(hard - 5.0, BOOK)             # за аварийным тросом — закрыть без вопросов
        assert ps.position is None and "аварийный трос" in ps.last_action and len(SoftPilot.asked) == 2
        def fresh(pl):   # новая сцена: killswitch и P/L сессии заново (иначе −18% дня всё запрёт)
            pl.pnls = []; pl.session_risk = trader_risk.SessionRisk(100000.0); pl._live_cash = None
        #     СЛИТЬ — закрытие сразу
        fresh(ps)
        ps.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0)); await ps.tick(90000.0, BOOK); await ps.tick(90000.0, BOOK)
        SoftPilot.answers[:] = [{"decision": "СЛИТЬ", "why": "пробой с объёмом"}]
        await ps.tick(88900.0, BOOK); await settle_guard(ps)
        assert ps.position is None and "решил слить" in ps.last_action and ps.guards[-1]["decision"] == "СЛИТЬ"
        #     FLASH молчит/падает → стоп по правилу
        fresh(ps)
        ps.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0)); await ps.tick(90000.0, BOOK); await ps.tick(90000.0, BOOK)
        async def boom(price, pos):
            raise RuntimeError("сеть")
        ps._stop_guard = boom
        await ps.tick(88900.0, BOOK); await settle_guard(ps)
        assert ps.position is None and "мягкий стоп по правилу: " in ps.last_action and "решил слить" not in ps.last_action
        g_ = ps.guards[-1]                   # ревью 5.4.2: страховка кода — не решение ИИ «СЛИТЬ»
        assert g_["decision"] == "НЕТ_ОТВЕТА" and g_["silent"] and g_["source"] == "код" \
            and g_["applied"] == "по правилу: СЛИТЬ" and "ответа не было" in g_["why"], g_
        #     предел «ждать»: PYTHIA_SOFT_STOP_MAX_HOLDS=3 → четвёртый раз не спрашиваем, закрываем
        ps._stop_guard = SoftPilot._stop_guard.__get__(ps, SoftPilot)
        fresh(ps)
        ps.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0)); await ps.tick(90000.0, BOOK); await ps.tick(90000.0, BOOK)
        pos = ps.position; pos["holds"] = 3
        await ps.tick(88900.0, BOOK)
        assert ps.position is None and "предел" in ps.last_action
        #     ревью 5.4.2 (финал): круг «трос ждёт → совет HOLD со стопом прежним → трос ждёт» не обходит предел: цена
        #     уже за стопом — HOLD без нового стопа счётчик «ждать» не сбрасывает
        fresh(ps)
        ps.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0)); await ps.tick(90000.0, BOOK); await ps.tick(90000.0, BOOK)
        pos = ps.position
        SoftPilot.answers[:] = [{"decision": "ЖДАТЬ", "why": "терпим"}]
        await ps.tick(88900.0, BOOK); await settle_guard(ps)
        assert ps.position is pos and pos["holds"] == 1, pos
        assert ps.adopt_forecast({"exec": {"do": "HOLD", "take": None, "invalidation": None}}) and pos["holds"] == 1, pos
        pos["holds"] = 3
        assert ps.adopt_forecast({"exec": {"do": "HOLD", "take": None, "invalidation": None}}) and pos["holds"] == 3
        await ps.tick(88900.0, BOOK)
        assert ps.position is None and "предел" in ps.last_action, ps.last_action
        #     ревью 5.4.2 (финал): поле «rule» в ответе ИИ — его текст, не флаг кода: «ЖДАТЬ» + своё правило — ждать
        fresh(ps)
        ps.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0)); await ps.tick(90000.0, BOOK); await ps.tick(90000.0, BOOK)
        pos = ps.position
        SoftPilot.answers[:] = [{"decision": "ЖДАТЬ", "why": "вынос стопов", "rule": "держу, пока 88500 не пробит",
                                 "detail": "лента", "raw": "ЖДАТЬ"}]
        await ps.tick(88900.0, BOOK); await settle_guard(ps)
        assert ps.position is pos and pos["holds"] == 1 and ps.guards[-1]["decision"] == "ЖДАТЬ" \
            and ps.guards[-1]["source"] == "ИИ", (ps.last_action, ps.guards[-1])
        await ps.tick(hard - 5.0, BOOK)             # за аварийным тросом — закрыть (сцена чистая для следующего блока)
        assert ps.position is None
        #     мягкий стоп выключен конфигом → жёсткий стоп, трос на самом стопе
        _saved = _setting("PYTHIA_SOFT_STOP", True)
        try:
            try:
                from . import config as _cfgm
            except ImportError:
                import config as _cfgm
            _cfgm.PYTHIA_SOFT_STOP = False
            fresh(ps)
            ps.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0)); await ps.tick(90000.0, BOOK); await ps.tick(90000.0, BOOK)
            assert ps.broker.stops[-1]["stop"] == 89000.0 and ps.position.get("hard_stop") is None
            await ps.tick(88900.0, BOOK)
            assert ps.position is None and "аварийный стоп" in ps.last_action
        finally:
            _cfgm.PYTHIA_SOFT_STOP = _saved

        # 29) МЯГКИЙ ТЕЙК (v5.3 W2): у тейка спрашивают FLASH; ПОДЕРЖАТЬ → триггер подтягивается к
        #     lock_price (не ниже входа + PYTHIA_TAKE_LOCK_PCT % хода до тейка), трос биржи за ним, тейк
        #     отодвинут на tp_next или снят, задача Совету; откат к триггеру → FLASH у троса, прибыль заперта;
        #     ЗАФИКСИРОВАТЬ → закрытие «ПОБЕДА: тейк»; FLASH молчит/падает → фиксация по правилу; предел
        #     «подержать» → фиксация; базовый пилот и выключенный конфиг → тейк закрывает без вопросов
        class TakePilot(SoftPilot):
            take_answers: list = []
            take_asked: list = []
            take_handoffs: list = []

            async def _take_guard(self, price, pos):
                self.take_asked.append(price)
                a = self.take_answers.pop(0) if self.take_answers else {"decision": "ЗАФИКСИРОВАТЬ", "why": "нет ответа"}
                if a == "boom":
                    raise RuntimeError("сеть")
                return a

            def _take_handoff(self, why):
                self.take_handoffs.append(why)

        async def settle_take(pl, n=50):
            for _ in range(n):
                if not pl.position or not pl.position.get("guard_busy"):
                    return
                await asyncio.sleep(0.01)

        def open_t(pl):
            fresh(pl)
            pl.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0))

        pt = TakePilot("TEST", deposit=100000.0, broker=FakeBroker())
        pt.figi, pt.asset_class, pt.go_per_lot, pt.tick_size, pt.point_value = "FIGI-T", "futures", 12000.0, 1.0, 1.0
        pt.deposit = 100000.0; pt.session_risk = trader_risk.SessionRisk(100000.0); pt._sr_day = pt._msk_day(); pt._state_path = None
        open_t(pt); await pt.tick(90000.0, BOOK); await pt.tick(90000.0, BOOK)
        pos = pt.position
        assert pos and pos["take"] == 95000.0 and pt.status()["soft_take"] and pt.status()["take_guard"]["holds"] == 0
        TakePilot.take_answers[:] = [{"decision": "ПОДЕРЖАТЬ", "why": "импульс не выдохся, лента за нас",
                                      "lock_price": 91000.0, "tp_next": 97000.0, "hold_minutes": 10}]
        await pt.tick(95100.0, BOOK)                # цена у тейка → вопрос FLASH, позиция не закрыта вслепую
        assert pt.position is pos and (pos.get("guard_busy") or TakePilot.take_asked), pt.last_action
        st_t = pt.status()
        assert st_t["take_guard"]["busy"] and not st_t["guard"]["busy"] or not pos.get("guard_busy"), st_t["take_guard"]
        await settle_take(pt)
        assert TakePilot.take_asked == [95100.0] and pt.position is pos, "ПОДЕРЖАТЬ — позиция жива"
        floor = 90000.0 + (95000.0 - 90000.0) * 0.5   # вход + 50 % хода до тейка
        assert pos["take_holds"] == 1 and pos["invalidation"] == floor and pos["inv0"] == floor, pos
        assert abs(pos["hard_stop"] - round(floor * (1 - 1.5 / 100))) < 1.0 and pos["hard_stop"] > 90000.0, "трос за триггером, выше входа"
        assert pos["take"] == 97000.0 and pos.get("restop") and pos["take_next"] - time.time() > 500, pos
        g = pt.guards[-1]
        assert g["side"] == "take" and g["decision"] == "ПОДЕРЖАТЬ" and g["lock_price"] == floor and g["tp_next"] == 97000.0, g
        assert TakePilot.take_handoffs and "импульс" in TakePilot.take_handoffs[-1] and "ПОДЕРЖАТЬ" in pt.last_action
        assert pt.status()["take_guard"]["holds"] == 1 and pt.status()["take_guard"]["next_in_s"] > 500
        await pt.tick(95100.0, BOOK)                # тик: трос биржи переставлен за новый триггер
        assert abs(pt.broker.stops[-1]["stop"] - pos["hard_stop"]) < 1.0 and not pos.get("restop") and pt.position is pos
        assert len(TakePilot.take_asked) == 1, "тейк отодвинут — у 95 100 вопросов больше нет"
        SoftPilot.answers[:] = [{"decision": "СЛИТЬ", "why": "откат с объёмом"}]
        await pt.tick(92400.0, BOOK); await settle_guard(pt)   # откат к запертой прибыли → FLASH у троса → слить в плюс
        assert pt.position is None and pt.pnls[-1] > 0 and "решил слить" in pt.last_action, (pt.last_action, pt.pnls)
        #     ЗАФИКСИРОВАТЬ → закрытие «ПОБЕДА: тейк» с причиной FLASH
        open_t(pt); await pt.tick(90000.0, BOOK); await pt.tick(90000.0, BOOK)
        TakePilot.take_answers[:] = [{"decision": "ЗАФИКСИРОВАТЬ", "why": "цель взята, стакан редеет"}]
        await pt.tick(95100.0, BOOK); await settle_take(pt)
        assert pt.position is None and "ПОБЕДА: тейк" in pt.last_action and "цель взята" in pt.last_action, pt.last_action
        assert pt.guards[-1]["side"] == "take" and pt.guards[-1]["decision"] == "ЗАФИКСИРОВАТЬ" and pt.pnls[-1] > 0
        #     FLASH молчит/падает → фиксация по правилу
        open_t(pt); await pt.tick(90000.0, BOOK); await pt.tick(90000.0, BOOK)
        TakePilot.take_answers[:] = ["boom"]
        await pt.tick(95100.0, BOOK); await settle_take(pt)
        assert pt.position is None and "фиксация по правилу" in pt.last_action and pt.guards[-1]["decision"] == "НЕТ_ОТВЕТА"
        assert pt.guards[-1]["silent"] and pt.guards[-1]["source"] == "код" \
            and pt.guards[-1]["applied"] == "по правилу: ЗАФИКСИРОВАТЬ", pt.guards[-1]
        #     предел «подержать» (PYTHIA_SOFT_TAKE_MAX_HOLDS=2) → третий раз не спрашиваем, фиксируем
        open_t(pt); await pt.tick(90000.0, BOOK); await pt.tick(90000.0, BOOK)
        pt.position["take_holds"] = 2
        n_asked = len(TakePilot.take_asked)
        await pt.tick(95100.0, BOOK)
        assert pt.position is None and "предел" in pt.last_action and len(TakePilot.take_asked) == n_asked, pt.last_action
        #     ПОДЕРЖАТЬ без tp_next и с lock_price ниже пола → тейк снят, триггер на полу (входа + 50 %)
        open_t(pt); await pt.tick(90000.0, BOOK); await pt.tick(90000.0, BOOK)
        pos = pt.position
        TakePilot.take_answers[:] = [{"decision": "подержать ещё", "why": "растёт", "lock_price": 90100.0}]
        await pt.tick(95100.0, BOOK); await settle_take(pt)
        assert pt.position is pos and pos["take"] is None and pos["invalidation"] == floor and pos["take_holds"] == 1, pos
        await pt.tick(95100.0, BOOK)
        assert pt.position is pos and len(TakePilot.take_asked) == n_asked + 1, "тейка нет — вопросов у тейка нет"
        pt.adopt_forecast(ex("BUY", take=99000.0, inv=93000.0))   # свежий приказ той же стороны — счётчик заново
        assert pos["take_holds"] == 0 and pos["take"] == 99000.0 and "take_next" not in pos
        await pt._close_all(95100.0, "тест: освободить-тейк", reanalyze=False)
        #     базовый пилот: тейк = закрытие без вопросов, soft_take выключен
        pb = mk(); pb.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0))
        await pb.tick(90000.0, BOOK); await pb.tick(90000.0, BOOK); await pb.tick(95100.0, BOOK)
        assert pb.position is None and "ПОБЕДА: тейк" in pb.last_action and pb.status()["soft_take"] is False and pb.status()["take_guard"] is None
        #     мягкий тейк выключен конфигом → как раньше, FLASH не спрашивают
        try:
            _cfgm.PYTHIA_SOFT_TAKE = False
            open_t(pt); await pt.tick(90000.0, BOOK); await pt.tick(90000.0, BOOK)
            n_asked = len(TakePilot.take_asked)
            await pt.tick(95100.0, BOOK)
            assert pt.position is None and "всё в кассу" in pt.last_action and len(TakePilot.take_asked) == n_asked
        finally:
            _cfgm.PYTHIA_SOFT_TAKE = True
        #     проверяющий фазы 2: малая цель (+2 % < 2×HARD_STOP_PCT) — после ПОДЕРЖАТЬ трос биржи НЕ ниже входа
        #     (прибыль заперта: худший исход — в ноль), FLASH у троса не уводит триггер под вход
        fresh(pt); pt.adopt_forecast(ex("BUY", take=91800.0, inv=88200.0))
        await pt.tick(90000.0, BOOK); await pt.tick(90000.0, BOOK)
        pos = pt.position
        TakePilot.take_answers[:] = [{"decision": "ПОДЕРЖАТЬ", "why": "разгон", "lock_price": 90100.0, "tp_next": 93000.0}]
        await pt.tick(91900.0, BOOK); await settle_take(pt)
        assert pt.position is pos and pos["invalidation"] == 90900.0 and pos["hard_stop"] == 90000.0, pos
        await pt.tick(91900.0, BOOK)
        assert pt.broker.stops[-1]["stop"] == 90000.0, "трос биржи на входе, не ниже"
        SoftPilot.answers[:] = [{"decision": "ЖДАТЬ", "why": "отскочит", "hold_until_price": 89600.0}]
        await pt.tick(90800.0, BOOK); await settle_guard(pt)
        assert pt.position is pos and pos["invalidation"] == 90900.0 and pos["hard_stop"] == 90000.0, "триггер под вход не ушёл"
        await pt.tick(89990.0, BOOK)
        assert pt.position is None and pt.pnls[-1] >= (89990.0 - 90000.0) * 8 and "аварийный трос @90000" in pt.last_action, (pt.last_action, pt.pnls[-1])   # только гэп 10 п., не −3 792
        pos = None

        # 26) РЕСТАРТ С ОТКРЫТОЙ ПОЗИЦИЕЙ (мягкий стоп): state-файл несёт триггер ИИ (inv0),
        #     аварийный трос (hard_stop) и счётчик «ждать»; новый пилот подхватывает позицию
        #     со счёта той же стороны с ТЕМ ЖЕ тросом — перевыставлять его не надо
        import tempfile
        from pathlib import Path
        pr = mk(cls=SoftPilot)
        pr.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0))
        await pr.tick(90000.0, BOOK); await pr.tick(90000.0, BOOK)
        assert pr.position and pr.position["lots"] == 8 and abs(pr.position["hard_stop"] - hard) < 1.0
        pr._state_path = Path(tempfile.mkdtemp()) / "s.json"
        pr._save_state()
        rec = json.loads(pr._state_path.read_text())
        assert rec["figi"] == "FIGI-T" and rec["position"]["lots"] == 8, rec
        assert rec["position"]["inv0"] == 89000.0 and abs(rec["position"]["hard_stop"] - hard) < 1.0, rec["position"]
        #     у нетронутой позиции счётчика «ждать» ещё нет (в файле null) — при подхвате это 0
        assert not rec["position"]["holds"] and rec["position"]["stop_id"] == pr.position["stop_id"], rec["position"]
        p2 = mk(cls=SoftPilot); p2._state_path = pr._state_path
        p2._restore_state(8)                        # на счёте 8 лотов той же стороны
        assert p2.position and p2.position["side"] == "long" and p2.position["lots"] == 8, p2.position
        assert p2.position["hard_stop"] == pr.position["hard_stop"], "трос обязан пережить рестарт как есть"
        assert p2.position["inv0"] == 89000.0 and p2.position["invalidation"] == 89000.0 and p2.position["holds"] == 0
        assert p2.position["stop_id"] == pr.position["stop_id"] and not p2.position.get("restop"), \
            "трос уже лежал на бирже — перевыставлять нечего"
        assert p2.state == "В_ПОЗИЦИИ" and p2.foreign_lots == 0 and "РЕСТАРТ" in p2.last_action
        #     старый state-файл (до v5.2): без inv0/hard_stop — трос пересчитывается от триггера,
        #     а биржевой стоп, стоявший на самом триггере, переезжает на трос ближайшим тиком
        old = {"figi": "FIGI-T", "base": "TEST", "ts": time.time(),
               "position": {"side": "long", "entry": 90000.0, "lots": 8, "take": 95000.0,
                            "invalidation": 89000.0, "opened_ts": time.time() - 600, "stop_id": "S-old"}}
        p3 = mk(cls=SoftPilot); p3._state_path = pr._state_path
        p3._state_path.write_text(json.dumps(old, ensure_ascii=False))
        p3._restore_state(8)
        pos = p3.position
        assert pos and pos["lots"] == 8 and pos["inv0"] == 89000.0 and pos["invalidation"] == 89000.0, pos
        assert pos["hard_stop"] == p3._hard_of(pos) and abs(pos["hard_stop"] - hard) < 1.0, pos
        assert pos["restop"] is True and pos["holds"] == 0 and pos["stop_id"] == "S-old", pos
        await p3.tick(90000.0, BOOK)                # restop → старый стоп снят, на бирже трос
        assert p3.broker.stop_cancels == ["S-old"] and abs(p3.broker.stops[-1]["stop"] - hard) < 1.0, p3.broker.stops
        assert p3.position is pos and pos["stop_id"] and not pos.get("restop")
        #     знак на счёте не совпал (позиции уже нет) — файл стирается, позиция не подхватывается
        pr._save_state()
        p4 = mk(cls=SoftPilot); p4._state_path = pr._state_path
        p4._restore_state(-3)
        assert p4.position is None and p4.state == "ЖДУ_ПЛАН" and not pr._state_path.exists()

        # 27) ПРОВЕРЯЮЩИЙ v5.2: (а) владелец перевернул руками → старый трос снят, не сирота;
        #     (б) добор по приказу при цене за стопом отменён; (в) закрытие в двух задачах — один ордер
        pf1 = mk(); pf1.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0))
        await pf1.tick(90000.0, BOOK); await pf1.tick(90000.0, BOOK)
        old_sid = pf1.position["stop_id"]
        pf1.broker.pf_positions = [{"figi": "FIGI-T", "qty": -4, "avg": 90300.0}]
        pf1._tick_n = RECONCILE_EVERY - 1
        await pf1.tick(90300.0, BOOK)
        assert pf1.position["side"] == "short" and pf1.position["stop_id"] == old_sid and pf1.position.get("restop")
        await pf1.tick(90300.0, BOOK)                # restop: старый SELL-стоп снят, BUY-стоп на шорт
        assert old_sid in pf1.broker.stop_cancels and pf1.broker.stops[-1]["direction"] == BUY \
            and pf1.broker.stops[-1]["lots"] == 4, (pf1.broker.stop_cancels, pf1.broker.stops)
        pf2 = mk(cls=SoftPilot); SoftPilot.answers[:] = [{"decision": "ЖДАТЬ", "why": "терпим"}]
        pf2.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0))
        await pf2.tick(90000.0, BOOK); await pf2.tick(90000.0, BOOK)
        pf2.plan = {"side": "long", "entry": None, "take": 95000.0, "invalidation": 89000.0, "why": "ДОБРАТЬ", "ts": time.time()}
        await pf2.tick(88950.0, BOOK)
        assert pf2.pending is None and pf2.plan is None and "добор отменён" in pf2.last_action, pf2.last_action
        await settle_guard(pf2)                      # FLASH: ЖДАТЬ — позиция жива, добора нет
        assert pf2.pending is None and pf2.position and pf2.position["lots"] == 8 and pf2.state != "ЖДУ_ПЛАН"

        class SlowBroker(FakeBroker):
            async def place(self, figi, direction, lots, price=None, tag=""):
                r = await super().place(figi, direction, lots, price, tag)
                if tag == "aip-close":
                    await asyncio.sleep(0.2)
                return r
        pf3 = mk(cls=SoftPilot); pf3.broker = SlowBroker()
        SoftPilot.answers[:] = [{"decision": "СЛИТЬ", "why": "пробой"}]
        pf3.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0))
        await pf3.tick(90000.0, BOOK); await pf3.tick(90000.0, BOOK)
        hard3 = pf3.position["hard_stop"]
        await pf3.tick(88900.0, BOOK)                # FLASH → СЛИТЬ → _close_all ждёт биржу
        await asyncio.sleep(0.05)
        assert pf3.position and pf3.position.get("closing"), "закрытие идёт"
        await pf3.tick(hard3 - 5.0, BOOK)            # петля за тросом: второй ордер не шлётся
        assert "второй ордер не шлю" in pf3.last_action, pf3.last_action
        await asyncio.sleep(0.4)
        assert pf3.position is None and len([x for x in pf3.broker.placed if x["tag"] == "aip-close"]) == 1
        assert len(pf3.pnls) == 1

            # 28) ПРОВЕРЯЮЩИЙ v5.2, вторая волна: (а) кэш GetMaxLots протухает после закрытия — флип берёт
        #     свежий ответ биржи (15, а не 30 с закрытием лонга); после исполнения кэш тоже сброшен
        px1 = mk(); px1.broker = MaxBroker(); px1.deposit_override = None
        px1.broker.mx = {"buy": 15, "sell": 30}
        px1.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0))
        await px1.tick(90000.0, BOOK); await px1.tick(90000.0, BOOK)
        assert px1.position and px1.position["lots"] == 15 and px1._mx is None, "после исполнения кэш сброшен"
        px1.broker.mx = {"buy": 15, "sell": 15}
        assert px1.adopt_forecast(ex("SELL", take=85000.0, inv=91000.0))
        await px1.tick(90000.0, BOOK)                                  # флип: закрыть лонг
        assert px1.position is None and px1._mx is None and px1._closed_ts > 0
        await px1.tick(90000.0, BOOK)                                  # вход в шорт — по свежему ответу биржи
        assert px1.pending and px1.pending["lots"] == 15, px1.pending
        #     (б) CLOSE при заявке входа в полёте: частичка станет позицией — и закроется; без частички — нечего закрывать
        px2 = mk(); px2._prepared = True
        px2.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0))
        px2.broker.fill_next = False
        await px2.tick(90000.0, BOOK)
        assert px2.pending
        px2.broker.partial[px2.pending["order_id"]] = 3
        assert px2.adopt_forecast({"exec": {"do": "CLOSE", "why": "картина сломалась"}})
        assert px2._cancel_entry and px2._close_pending and "снимаю заявку" in px2.last_action
        px2.broker.fill_next = True
        await px2.tick(90000.0, BOOK)                                  # заявка снята, частичка 3 лот → позиция
        assert px2.position and px2.position["lots"] == 3, px2.position
        await px2.tick(90000.0, BOOK)                                  # CLOSE закрывает её
        assert px2.position is None and px2._close_pending is None and "приказ совета: закрыть" in px2.last_action
        px3 = mk(); px3._prepared = True
        px3.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0))
        px3.broker.fill_next = False
        await px3.tick(90000.0, BOOK)
        assert px3.adopt_forecast({"exec": {"do": "CLOSE"}}) and px3._close_pending
        await px3.tick(90000.0, BOOK)                                  # снята без исполнения (fill_next=False)
        assert px3.pending is None and px3.position is None and px3._close_pending is None
        #     (в) лаг портфеля после собственного закрытия: «лоты ещё лежат» — не фантом, а ожидание второй сверки;
        #         портфель опустел → ничего не принято, стопа-сироты и фантомного P/L нет;
        #         владелец правда купил (две сверки подряд) → принято; без своего закрытия — принимаем сразу
        px4 = mk(); px4.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0))
        await px4.tick(90000.0, BOOK); await px4.tick(90000.0, BOOK)
        px4.broker.pf_positions = [{"figi": "FIGI-T", "qty": 8, "avg": 90000.0}]
        px4._tick_n = RECONCILE_EVERY - 1
        await px4.tick(90000.0, BOOK)                                  # сверка: сходится
        assert px4.position
        await px4.tick(95100.0, BOOK)                                  # тейк → закрыто
        assert px4.position is None and px4._closed_ts > 0
        n_pnl, n_st = len(px4.pnls), len(px4.broker.stops)
        px4._tick_n = RECONCILE_EVERY - 1
        await px4.tick(95100.0, BOOK)                                  # портфель отстаёт: 8 лот ещё «лежат»
        assert px4.position is None and "жду подтверждения" in px4.last_action, px4.last_action
        px4.broker.pf_positions = [{"figi": "FIGI-T", "qty": 0}]
        px4._tick_n = RECONCILE_EVERY - 1
        await px4.tick(95100.0, BOOK)                                  # портфель опустел — фантома не было
        await px4.tick(95100.0, BOOK)
        assert px4.position is None and len(px4.pnls) == n_pnl and len(px4.broker.stops) == n_st and not px4.session_risk.locked
        px4.broker.pf_positions = [{"figi": "FIGI-T", "qty": 5, "avg": 95000.0}]
        px4._tick_n = RECONCILE_EVERY - 1
        await px4.tick(95100.0, BOOK)                                  # первая сверка — ждём
        assert px4.position is None
        px4._tick_n = RECONCILE_EVERY - 1
        await px4.tick(95100.0, BOOK)                                  # вторая с теми же лотами — владелец правда купил
        assert px4.position and px4.position["lots"] == 5 and px4.position["side"] == "long", px4.position
        px5 = mk(); px5.broker.pf_positions = [{"figi": "FIGI-T", "qty": 4, "avg": 90000.0}]
        px5._tick_n = RECONCILE_EVERY - 1
        await px5.tick(90000.0, BOOK)                                  # без своего закрытия — принимаем сразу
        assert px5.position and px5.position["lots"] == 4
        #     (г) срок «ждать», названный FLASH (guard_next), переживает рестарт
        import tempfile as _tf
        from pathlib import Path as _P
        pg = mk(cls=SoftPilot); pg.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0))
        await pg.tick(90000.0, BOOK); await pg.tick(90000.0, BOOK)
        pg.position["guard_next"] = time.time() + 300; pg.position["holds"] = 1
        pg._state_path = _P(_tf.mkdtemp()) / "g.json"; pg._save_state()
        pg2 = mk(cls=SoftPilot); pg2._state_path = pg._state_path
        pg2._restore_state(8)
        assert pg2.position and pg2.position.get("guard_next") and pg2.position["guard_next"] - time.time() > 250
        assert pg2.position["holds"] == 1 and pg2.position["hard_stop"] == pg.position["hard_stop"]

        # 29) РЫНОЧНЫЕ ЧАСЫ (воля владельца: «биржа закрыта — стопорится»): закрыто → входа нет,
        #     заявка в полёте снята (план жив), перепроверка ждёт открытия, позиция за триггером не
        #     закрывается и FLASH не спрашивают (трос на бирже); открылось после ночи → сверка,
        #     состояние по факту и перепроверка сразу; короткий клиринг — просто продолжаем; часы
        #     выключены — как раньше
        FakeClock.open = False
        pc = mk(); pc.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0))
        pc.review_ts = time.time() + 60
        await pc.tick(90000.0, BOOK)
        assert pc.state == "РЫНОК_ЗАКРЫТ" and not pc.pending and not pc.broker.placed and pc.plan, pc.last_action
        assert "рынок закрыт до 10:00 МСК" in pc.last_action and "с открытия" in pc.last_action, pc.last_action
        assert pc.review_ts >= time.time() + FakeClock.next_in + OPEN_REVIEW_GRACE_SEC - 2, "перепроверка — к открытию"
        assert pc.status()["market"]["open"] is False and pc.status()["market_closed_since"]
        FakeClock.open = True
        pp = mk(); pp.broker.fill_next = False; pp.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0))
        await pp.tick(90000.0, BOOK)
        assert pp.pending, "заявка в полёте"
        FakeClock.open = False
        await pp.tick(90000.0, BOOK)
        assert not pp.pending and pp.broker.cancelled and pp.plan and pp.state == "РЫНОК_ЗАКРЫТ", "закрытие снимает заявку, план жив"
        FakeClock.open = True
        pz = mk(cls=SoftPilot); pz.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0))
        await pz.tick(90000.0, BOOK); await pz.tick(90000.0, BOOK)
        assert pz.position and pz.position.get("hard_stop"), "позиция с тросом"
        n_stops, n_asked = len(pz.broker.stops), len(SoftPilot.asked)
        FakeClock.open = False
        await pz.tick(88500.0, BOOK)                     # цена за триггером при закрытом рынке
        await asyncio.sleep(0.05)
        assert pz.position and not pz.position.get("guard_busy") and len(SoftPilot.asked) == n_asked, "FLASH у троса не спрашивают"
        assert pz.state == "РЫНОК_ЗАКРЫТ" and len(pz.broker.stops) == n_stops and "под аварийным тросом" in pz.last_action, pz.last_action
        pz.broker.pf_positions = [{"figi": "FIGI-T", "qty": pz.position["lots"], "avg": 90000.0}]
        pz._closed_since = time.time() - 8 * 3600; pz.review_ts = time.time() + 3600
        FakeClock.open = True
        await pz.tick(90500.0, BOOK)                     # открытие после ночи
        assert pz.state == "В_ПОЗИЦИИ" and pz.position and pz._closed_since == 0.0, (pz.state, pz.last_action)
        assert pz.review_ts <= time.time() + OPEN_REVIEW_GRACE_SEC + 1, "перепроверка сразу после открытия"
        pz.review_ts = time.time() + 3600                # плановая перепроверка через час
        FakeClock.open = False
        await pz.tick(90500.0, BOOK)                     # клиринг: перепроверка отодвинута к «открытию» (4 ч)
        assert pz.review_ts >= time.time() + FakeClock.next_in
        pz._closed_since = time.time() - 300
        FakeClock.open = True
        await pz.tick(90500.0, BOOK)                     # короткий клиринг кончился
        assert pz.state == "В_ПОЗИЦИИ" and time.time() + 3000 < pz.review_ts <= time.time() + 3600, \
            "клиринг не дёргает PRO, но и плановую перепроверку не уносит к завтрашнему открытию"
        FakeClock.open = False
        _en = FakeClock.enabled
        FakeClock.enabled = staticmethod(lambda: False)
        po = mk(); po.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0))
        await po.tick(90000.0, BOOK)
        assert po.state != "РЫНОК_ЗАКРЫТ" and po.status()["market"] is None and po.broker.placed, "часы выключены — как раньше"
        FakeClock.enabled = _en
        FakeClock.open = True

        # 30) v5.4.1 «ТРЕЗВЫЙ ПИЛОТ»: стопы только в программе (PYTHIA_EXCHANGE_STOP=0 — умолчание): на бирже ни
        #     одной стоп-заявки, трос виртуальный (за ним закрытие по рынку без вопросов), триггер работает как
        #     раньше; старый stop_id снимается ОДИН раз с подтверждением; переключение флага в бою — в обе стороны;
        #     приказ WAIT — без плана, вне рынка; хуки: базовый _entry_gate → вход сразу, наследник «нет» → входа нет,
        #     авто-добор без проверки; _profit_watch зовётся в позиции (не при тросе/тейке/занятости);
        #     gates/profits переживают рестарт в секции pilot (протухшие отбрасываются)
        _cfg0.PYTHIA_EXCHANGE_STOP = False
        pv = mk(cls=SoftPilot)
        SoftPilot.answers[:] = []
        pv.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0))
        await pv.tick(90000.0, BOOK); await pv.tick(90000.0, BOOK)
        pos = pv.position
        assert pos and pos["lots"] == 8 and not pv.broker.stops and pos.get("stop_id") is None and not pos.get("stop_request"), pos
        assert abs(pos["hard_stop"] - hard) < 1.0 and "трос в программе @" in pv.last_action \
            and "стопов на бирже нет" in pv.last_action and "триггер PRO @89000" in pv.last_action, pv.last_action
        assert pv.status()["exchange_stop"] is False and pv.status()["hard_stop"] == pos["hard_stop"]
        assert pv.status()["profit"]["busy"] is False and pv.status()["profit"]["threshold_pct"] == 60.0 and pv.status()["entry_gate"] is None
        await pv.tick(90000.0, BOOK)                 # restop — на биржу ничего не ставит
        assert not pv.broker.stops and not pos.get("restop")
        SoftPilot.answers[:] = [{"decision": "ЖДАТЬ", "why": "терпим", "hold_until_price": 88500.0}]
        await pv.tick(88950.0, BOOK); await settle_guard(pv)     # триггер и вопрос у троса — как раньше
        assert pv.position is pos and pos["holds"] == 1 and pos["invalidation"] == 88500.0 and abs(pos["hard_stop"] - hard) < 1.0
        n_o = len(pv.broker.placed)
        await pv.tick(hard - 5.0, BOOK)              # гэп за виртуальный трос → закрытие по рынку без вопросов
        assert pv.position is None and "аварийный трос" in pv.last_action and len(pv.broker.placed) == n_o + 1, pv.last_action
        assert pv.broker.placed[-1]["tag"] == "aip-close" and pv.broker.placed[-1]["price"] is None
        assert not pv.broker.stop_cancels and not pv.broker.stops, "стопов не было — снимать нечего"
        #     флаг переключили в бою: трос был на бирже → снят один раз; включили обратно → снова на бирже;
        #     отмена не подтверждена и списка стопов нет → повтор через STOP_RETRY_SEC, stop_id хранится
        fresh(pv)
        _cfg0.PYTHIA_EXCHANGE_STOP = True
        pv.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0)); await pv.tick(90000.0, BOOK); await pv.tick(90000.0, BOOK)
        pos = pv.position
        sid = pos["stop_id"]
        assert sid and len(pv.broker.stops) == 1 and pv.status()["exchange_stop"] is True
        _cfg0.PYTHIA_EXCHANGE_STOP = False
        pos["restop"] = True
        await pv.tick(90000.0, BOOK)
        assert pos["stop_id"] is None and pv.broker.stop_cancels == [sid] and len(pv.broker.stops) == 1 and not pos.get("restop"), pos
        await pv.tick(90000.0, BOOK)
        assert pv.broker.stop_cancels == [sid], "снят один раз, не каждый тик"
        _cfg0.PYTHIA_EXCHANGE_STOP = True
        pos["restop"] = True
        await pv.tick(90000.0, BOOK)
        assert pos["stop_id"] == "S-2" and len(pv.broker.stops) == 2 and abs(pv.broker.stops[-1]["stop"] - hard) < 1.0, pos
        _cfg0.PYTHIA_EXCHANGE_STOP = False
        pv.broker.cancel_stop_ok = False
        pos["restop"] = True
        await pv.tick(90000.0, BOOK)
        assert pos["stop_id"] == "S-2" and pos.get("restop") and pos.get("restop_after", 0) > time.time(), pos
        pv.broker.cancel_stop_ok = True
        pos["restop_after"] = 0.0
        await pv.tick(90000.0, BOOK)
        assert pos["stop_id"] is None and not pos.get("restop") and pv.broker.stop_cancels[-1] == "S-2"
        n_c = len(pv.broker.stop_cancels)
        await pv._close_all(90000.0, "тест: освободить-30", reanalyze=False)
        assert pv.position is None and len(pv.broker.stop_cancels) == n_c, "закрытие без стопа — снимать нечего"
        #     приказ WAIT: плана нет, вне рынка, входа нет; v5.4.2 — перепроверка через PYTHIA_WAIT_REVIEW_SEC, а не
        #     PYTHIA_REVIEW_SEC вслепую; раньше подтянутая перепроверка остаётся; текст без «перевеса нет»
        pw = mk()
        assert pw.adopt_forecast({"exec": {"do": "WAIT", "wait_for": "закрепление выше 90500", "why": "т"}}) is True
        w_sec = wait_review_sec()
        assert w_sec <= REVIEW_SEC and abs(pw.review_ts - time.time() - w_sec) < 5, (pw.review_ts - time.time(), w_sec)
        assert pw.plan is None and pw.state == "ЖДУ_ПЛАН" and "вне рынка — ждал: закрепление выше 90500" in pw.last_action
        assert f"перепроверка через {int((w_sec + 59) // 60)} мин" in pw.last_action and "перевеса нет" not in pw.last_action
        await pw.tick(90000.0, BOOK)
        assert pw.pending is None and not pw.broker.placed, "WAIT — входа нет"
        pw.review_ts = time.time() + 60                 # повод уже подтянул перепроверку — WAIT её не отодвигает
        assert pw.adopt_forecast({"exec": {"do": "WAIT"}}) and pw.review_ts - time.time() < 61
        assert "ждал: условие не названо" in pw.last_action and "перевеса нет" not in pw.last_action, pw.last_action
        pw.review_ts = time.time() - 5                  # срок прошёл — WAIT ставит полный шаг вне рынка
        assert pw.adopt_forecast({"exec": {"do": "WAIT"}}) and abs(pw.review_ts - time.time() - w_sec) < 5

        async def _no_review(price):
            return None
        pw._review_bg = _no_review
        pw.review_ts = 0.0                              # плановая пришла: следующий шаг вне рынка по WAIT — тоже короткий
        await pw.tick(90000.0, BOOK)
        assert abs(pw.review_ts - time.time() - w_sec) < 5, pw.review_ts - time.time()
        assert pw.adopt_forecast(ex("BUY", inv=89000.0)) and pw._review_gap() == REVIEW_SEC, "не WAIT — шаг прежний"
        #     v5.4.2: сроки у двери / в мысли о прибыли и допуск дрейфа — из конфига живьём; подмена константы главнее
        #     (сравнение — с закреплёнными умолчаниями конфига, не со снимком модуля: окружение владельца, например
        #     PYTHIA_ENTRY_TIMEOUT_SEC=1800, попало в снимок при импорте, а живое значение — закреплённое, ревью 5.4.2)
        _pin = _cfg0.FREE_PILOT_DEFAULTS
        assert entry_timeout() == float(_setting("PYTHIA_ENTRY_TIMEOUT_SEC", 0)) == float(_pin["PYTHIA_ENTRY_TIMEOUT_SEC"])
        assert profit_timeout() == float(_setting("PYTHIA_PROFIT_TIMEOUT_SEC", 0)) == float(_pin["PYTHIA_PROFIT_TIMEOUT_SEC"])
        assert abs(drift_frac() - float(_pin["PYTHIA_ENTRY_DRIFT_PCT"]) / 100) < 1e-12
        _saved_et = globals()["ENTRY_TIMEOUT"]
        globals()["ENTRY_TIMEOUT"] = 0.3
        try:
            assert entry_timeout() == 0.3, "подмена стенда главнее конфига"
        finally:
            globals()["ENTRY_TIMEOUT"] = _saved_et
        #     v5.4.2: killswitch видит круг, а не кусок: убыточный выход тремя частями — одна запись серии
        pk = mk()
        pk.adopt_forecast(ex("BUY", inv=89000.0)); await pk.tick(90000.0, BOOK); await pk.tick(90000.0, BOOK)
        assert pk.position and pk.position["lots"] >= 3, pk.position
        n_lots, n_pnl = pk.position["lots"], len(pk.pnls)
        oid_k = f"F-{pk.broker._n + 1}"
        pk.broker.partial[oid_k] = 1
        await pk._close_all(89900.0, "тест: выход частями", reanalyze=False)
        assert pk.position and pk.position["lots"] == n_lots - 1 and pk.session_risk.streak == 0
        pk.broker.partial[oid_k] = 2
        await pk.tick(89900.0, BOOK)
        del pk.broker.partial[oid_k]
        await pk.tick(89900.0, BOOK)
        assert pk.position is None and len(pk.pnls) == n_pnl + 3, (pk.pnls, pk.last_action)
        assert pk.session_risk.streak == 1 and not pk.session_risk.locked, pk.session_risk.state()
        assert abs(pk.session_risk.pnl - sum(pk.pnls[n_pnl:])) < 1e-6
        #     v5.4.2: план «прорыв» (вход на пробитии 90500, стоп 90200) при цене 90000 — за стопом, но уровень не
        #     пробит: не «мертва до входа»; пробил и откатился за стоп — мертва
        class BreakPilot(AIPilot):
            def _entry_ready(self, price, lvl):
                return False                  # подтверждения пробоя в тесте не ждём — входа нет

        pbk = mk(cls=BreakPilot)
        fired_b = []

        async def cb_b():
            fired_b.append(1)

        pbk.reanalyze_cb = cb_b
        assert pbk.adopt_forecast(ex("BUY", entry=90500.0, take=92000.0, inv=90200.0))
        pbk.plan["kind"] = "прорыв"
        await pbk.tick(90000.0, BOOK)
        assert pbk.plan and "мертва" not in pbk.last_action and not fired_b, pbk.last_action
        await pbk.tick(90510.0, BOOK)
        assert pbk.plan and pbk.plan.get("crossed") == 90500.0, "пометка привязана к пробитому уровню"
        #     ревью 5.4.2: уровень сменили (дверь ЖДАТЬ с новым прорывом) — пробитие старого уровня новый не хоронит
        pbk.plan["entry"], pbk.plan["invalidation"] = 90800.0, 90600.0
        await pbk.tick(90550.0, BOOK)
        assert pbk.plan and "мертва" not in pbk.last_action and not fired_b, pbk.last_action
        pbk.plan["entry"], pbk.plan["invalidation"] = 90500.0, 90200.0
        await pbk.tick(90100.0, BOOK)
        await asyncio.sleep(0)
        assert pbk.plan is None and "мертва ДО входа" in pbk.last_action and fired_b
        #     хуки: базовый _entry_gate → вход сразу; наследник, ответивший «нет», — входа нет; авто-добор без проверки
        class GatePilot(AIPilot):
            allow = False
            asked: list = []

            async def _entry_gate(self, price, book):
                self.asked.append(price)
                return self.allow

        pg_ = mk(cls=GatePilot); pg_.broker = MaxBroker(); pg_.deposit_override = None
        pg_.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0))
        await pg_.tick(90000.0, BOOK)
        assert GatePilot.asked == [90000.0] and pg_.pending is None and not pg_.broker.placed, "у двери «нет» — входа нет"
        GatePilot.allow = True
        await pg_.tick(90000.0, BOOK)
        assert pg_.pending and len(GatePilot.asked) == 2 and pg_.pending["lots"] == 15
        await pg_.tick(90000.0, BOOK)                # FILL
        assert pg_.position and pg_.position["topup_left"] == 2
        pg_.broker.mx = {"buy": 2, "sell": 0}
        pg_.position["last_fill_ts"] -= TOPUP_GAP_SEC + 1
        pg_._tick_n = 5
        await pg_.tick(90000.0, BOOK)                # авто-добор по округлению биржи — без проверки у двери
        assert pg_.pending and pg_.pending.get("topup") and len(GatePilot.asked) == 2, "авто-добор _topup без проверки"
        await pg_.tick(90000.0, BOOK)
        pg_.plan = {"side": "long", "entry": None, "kind": "сейчас", "take": 95000.0, "invalidation": 89000.0,
                    "why": "ДОБРАТЬ", "ts": time.time()}
        GatePilot.allow = False
        await pg_.tick(90000.0, BOOK)                # добор по решению PRO — через проверку
        assert len(GatePilot.asked) == 3 and pg_.pending is None
        assert pg_.status()["entry_gate"] and pg_.status()["entry_gate"]["busy"] is False and pg_.status()["entry_gate"]["checks"] == 0
        #     _profit_watch: зовётся в позиции на живом рынке; не при вопросе у троса/тейка, не при тейке/тросе
        class ProfitPilot(AIPilot):
            seen: list = []

            def _profit_watch(self, price, pos):
                self.seen.append(price)

        pp_ = mk(cls=ProfitPilot); pp_.adopt_forecast(ex("BUY", take=95000.0, inv=89000.0))
        await pp_.tick(90000.0, BOOK); await pp_.tick(90000.0, BOOK)
        assert pp_.position and ProfitPilot.seen == []
        await pp_.tick(91000.0, BOOK)
        assert ProfitPilot.seen == [91000.0] and pp_.status()["profit"]["progress_pct"] == 20.0 and pp_.status()["profit"]["gain_pct"] > 1.0
        pp_.position["guard_busy"] = True
        await pp_.tick(91500.0, BOOK)
        assert ProfitPilot.seen == [91000.0], "пока ИИ у троса/тейка думает — мысли нет"
        pp_.position["guard_busy"] = False
        await pp_.tick(95100.0, BOOK)                # тейк базового пилота → закрытие, без мысли
        assert pp_.position is None and ProfitPilot.seen == [91000.0]
        #     gates/profits — в секции pilot state-файла (последние 10, протухшие — вон), позиции нет → файл живёт ими
        pr2 = mk(cls=SoftPilot)
        pr2.gates.append({"ts": time.time(), "decision": "ЖДАТЬ", "why": "тест", "plan_ts": 1.0})
        pr2.profits.append({"ts": time.time() - PILOT_EXTRA_TTL_SEC - 10, "decision": "ДЕРЖАТЬ", "why": "протухла"})
        pr2.profits.append({"ts": time.time(), "decision": "ДЕРЖАТЬ", "why": "тест"})
        pr2._state_path = Path(tempfile.mkdtemp()) / "x.json"
        assert pr2._save_state() and pr2._state_path.exists()
        rec = json.loads(pr2._state_path.read_text(encoding="utf-8"))
        assert rec["position"] is None and rec["pilot"]["gates"][0]["decision"] == "ЖДАТЬ" and len(rec["pilot"]["profits"]) == 1, rec
        pr3 = mk(cls=SoftPilot); pr3._state_path = pr2._state_path
        pr3._restore_state(0)
        assert pr3.gates[-1]["why"] == "тест" and len(pr3.profits) == 1 and pr3.profits[0]["why"] == "тест" and pr3.position is None
        assert pr3.status()["gates"][-1]["decision"] == "ЖДАТЬ" and pr3.status()["profits"][-1]["decision"] == "ДЕРЖАТЬ"
        _cfg0.PYTHIA_EXCHANGE_STOP = False

    print("ai_pilot self-test OK (закалка веером): валидация приказа "
              "(стороны стоп/тейк), макс с плечом акций (dlong/dshort) и "
              "шорт-ГО, рыночный гейт, дозор ужимает (не убивает всё), "
              "двойное закрытие убито, отбитое закрытие добивается, паника "
              "забирает частичку, снимок уровней в заявке, мёртвая идея не "
              "покупается, REJECTED-бэкофф, эскалация без маркета, нестрогий "
              "разбор решений + дрейф, killswitch дневной, стоп-сирота "
              "снимается, свежий вердикт обновляет позицию; v5.2: размер даёт биржа + добор, "
              "весь счёт — позиция бота (докупил/продал/перевернул/закрыл), приказ CLOSE, "
              "ДОБРАТЬ/ПЕРЕВЕРНУТЬ, мягкий стоп (ИИ у троса ждать/слить, аварийный трос, предел), мягкий тейк "
              "(подержать/зафиксировать, запертая прибыль, предел); "
              "проверяющий: трос не сирота при перевороте руками, добор за стопом отменён, одно закрытие на две задачи; "
              "рыночные часы: закрыто → стопор (заявка снята, трос лежит, PRO ждёт открытия), открылось → сверка и перепроверка; "
              "v5.4.1: стопы только в программе (ни одной стоп-заявки, виртуальный трос закрывает по рынку, старый стоп снят "
              "один раз, переключение в бою), приказ WAIT, хуки проверки входа и мысли о прибыли, персист gates/profits; "
              "v5.4.2: разбор перепроверки без молчаливого ЖДЁМ (None — решения нет), WAIT → перепроверка через "
              "PYTHIA_WAIT_REVIEW_SEC, killswitch по кругу (не по куску исполнения), «прорыв» не мёртв до пробития, "
              "сроки у денег и дрейф из конфига живьём")

    asyncio.run(main())
