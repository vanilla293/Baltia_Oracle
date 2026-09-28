# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — АВТОПИЛОТ (боевая петля: ключи ввёл — работает сразу).

По прямому заказу владельца: «ввёл ключи — заработал чётко и начал торговать,
без кнопок одобрить/неодобрить, прям сразу». Здесь НЕТ кнопок подтверждения:
режим определяется ключами (arming.resolve_mode), и в live ордера идут сами.

Петля (фон, тик ~1.5с, «работа кипела, не виснем»):
  1. снимок: стакан+лента → maya (тяга) + microstructure.xray (кто в стакане);
  2. история цен → Хёрст/VWAP/отскок (limits.catch_bounce);
  3. ИИ-шифратор: раз в час И при смене актора — новый вердикт DeepSeek;
     бот СХВАТЫВАЕТ новый результат и вписывает его в контекст решения;
  4. СВОД decision.decide(всё) → действие;
  5. вход по лимитной тактике (limits): лимит взводится ТОЛЬКО у точки,
     импульс убегает → ближайший; плиты/призраки учтены;
  6. в позиции: «играем ПОКА НЕ ПОВЕРНЁТСЯ» — выход не по фикс-цели, а по
     подтверждённому развороту (CVD-флип + актор разворота); стоп защищён от
     ложного выбивания (decision.stop_guard: неглубокий синтетический прокол
     НЕ выбивает);
  7. killswitch SessionRisk всегда; panic() — закрыть всё немедленно.

Наблюдение после открытия рынка (слова владельца: «сначала ждать часик,
наблюдать, просто считать»): первые OBSERVE_MIN минут после открытия — полный
расчёт БЕЗ ордеров, потом торгует. PYTHIA_OBSERVE_MIN=0 отключает.

Запуск (у владельца, на его машине):
    export TINKOFF_TOKEN=t.***          # КЛЮЧ 1 — руки
    export DEEPSEEK_API_KEY=sk-***      # КЛЮЧ 2 — мозг (+GO)
    export TRADER_ARM_REAL=1            # боевой рубильник брокера
    export TRADER_ARM_TOKEN=<фраза>     # и его пароль
    python3 -m backend.autopilot CR     # ← сразу работает, без кнопок

⚫ Автопилот в live — реальные деньги БЕЗ подтверждений: это осознанный выбор
владельца, сделанный вводом ключей и рубильника. НЕ обещание прибыли; рынок
не предсказуем; плечо 18+. Тормоза капитала: killswitch −6%/день и серия
убытков, инфаркт-guard спреда, panic().
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

try:
    from . import (arming, bif_calendar, bifurcation, decision, execution,
                   hawkes, instrument_select, kuramoto, leverage, limits,
                   maya, microstructure, oracle, stream, tinkoff,
                   trader_broker, trader_risk, wyckoff)
except ImportError:                                        # запуск как скрипт
    import arming, decision, instrument_select, limits, maya  # noqa: E401
    import microstructure, tinkoff, trader_broker, trader_risk  # noqa: E401
    import bif_calendar, bifurcation, hawkes, leverage, oracle, wyckoff  # noqa: E401
    import execution, kuramoto, stream  # noqa: E401

log = logging.getLogger("pythia.autopilot")

TICK_SEC = 1.5                # шаг петли: живо, но без душения API
AI_REFRESH_SEC = 3600         # ИИ-шифратор: раз в час (и при смене актора)
SLOW_REFRESH_SEC = 600        # медленный контур: свечи → бифуркации/VWAP/Вайкофф
CAT_STOP_EXTRA = 0.005        # катастрофический биржевой стоп глубже мягкого
                              # на 0.5% (потолок допуска stop_guard) — «Хищник» №4
MODEL_BREAK_ATRS = 2.0        # ход против вектора сингулярности ≥ 2 ATR =
                              # СТРУКТУРНЫЙ СЛОМ модели (killswitch по физике)
SNIPER_MIN_LOTS = 3           # объём ≥ — вход ведёт Снайпер («Сырая дата» №3)
MAX_REPRICINGS = 3            # предел перевзводов тени: дальше недобор
                              # снимается (иначе набор висит вечно и держит
                              # петлю на своей ветке — ревью «Абсолюта»)
TIME_KILL_SEC = 600.0         # TIME_KILLSWITCH («Абсолют»): сигнал живёт ≤10
                              # мин; дольше — горизонт Ляпунова сжигает вектор,
                              # рыночная ликвидация остатка без исключений
                              # (env PYTHIA_TIME_KILL_SEC переопределяет)
KURAMOTO_BASKET = ("Si", "CR", "BR", "GOLD")   # поводыри графа сцепки (№4)
# По требованию владельца: без часа ожидания. Копит данные, пока ИИ думает, и
# как ИИ дал результат — сразу боевой. Наблюдение по умолчанию 0 (можно вернуть
# PYTHIA_OBSERVE_MIN=N, если захочешь «посмотреть N минут»).
OBSERVE_MIN = float(os.getenv("PYTHIA_OBSERVE_MIN", "0"))
HIST_MAX = 600                # ~15 мин истории цен при тике 1.5с


class Autopilot:
    """Один инструмент (CR по умолчанию) — одна петля — один счёт."""

    def __init__(self, base: str = "CR", deposit: float | None = None):
        self.base = base
        self.deposit_override = deposit
        # КЛЮЧИ: сначала то, что владелец ввёл В ИНТЕРФЕЙСЕ (config), потом env.
        # Раньше читалось только окружение — поэтому «ключ ввёл, а бот не видит».
        tkn = self._cfg_tinkoff() or os.getenv("TINKOFF_TOKEN")
        dsk = self._cfg_deepseek() or os.getenv("DEEPSEEK_API_KEY")
        self.mode = arming.resolve_mode(tkn, dsk, go_live=bool(dsk))
        self.last_action = "—"
        self.started_ts = time.time()
        self._pulse = 0
        self._tick_n = 0
        self.pending: dict | None = None     # лимитка в полёте (ещё НЕ позиция)
        self.no_entry_until = 0.0            # пауза после сделки (анти-молотилка)
        # Боевой по умолчанию: есть токен Тинькофф → real (брокер сам откатит в
        # dry, если токена нет или стоит PYTHIA_DRY=1). Никаких рубильников в
        # конфиге — «ввёл ключ, сразу боевой».
        self.broker = trader_broker.Broker("real")
        # Накопление, пока ИИ думает: петля живёт и копит данные сразу, а РЕАЛЬНЫЕ
        # входы открываются только после готовности разбора (ставится сервером).
        self.analysis_ready = True   # сервер выставит False на время разбора
        self.session_risk: trader_risk.SessionRisk | None = None
        self.figi = None
        self.point_value = 1.0
        self.go_per_lot = None
        self.position: dict | None = None                    # {side, entry, lots, stop}
        self.prices: list[float] = []
        self.prev_bids = self.prev_asks = None
        self.ai_hint: dict = {}
        self.ai_ts = 0.0
        self.last_actor = ""
        self.observe_until = 0.0
        self.market_was_open = False
        self.panic_flag = False
        self.stopping = False                # раньше создавался только в run():
                                             # обращение до старта петли падало
                                             # AttributeError (латентный баг)
        # ── ОРАКУЛ-ЯДРО (аудит 06.08.2026: магистраль расчёт→решение) ──
        self.bif: dict | None = None         # двигатель бифуркаций (по свечам)
        self.reactor: dict | None = None     # реактор неба (нет эфемерид → None)
        self.wyckoff_bias: float | None = None   # Вайкофф дневок, −1..1
        self.session_vwap: float | None = None   # VWAP сессии из свечей
        self.atr_frac: float | None = None       # ATR(14)/цена по 15м свечам
        self.oracle_verdict: dict = {}       # последний вердикт Оракула
        self.last_ladder: dict = {}          # последняя ступень лестницы
        self.calib: dict = {}                # калибровка голосов из журнала
        self.pnls: list[float] = []          # P/L сделок сессии (губернатор)
        self.tape_hist: list[tuple] = []     # история ленты (объём, сделок)
        self.tape_incr: list[float] = []     # приросты числа сделок (Хоукс)
        self._tape_prev_cnt: float | None = None
        self.hawkes_now: dict | None = None  # ветвление потока (Хоукс)
        self.calendar: dict | None = None    # бифуркационный календарь (Матьё)
        self.stream: stream.TickStream | None = None   # сырые тики/стакан (WS)
        self.kuramoto: dict | None = None    # граф сцепки поводырей
        self.sniper: dict | None = None      # активный план Снайпера
        self.last_book: dict | None = None   # последний стакан (для нарезки)
        # ── Zero-Touch: фибо-сетка выхода + изоляция ручных позиций ──
        self.spread_hist: list[float] = []   # ширина спреда В ЦЕНЕ (медиана —
                                             # база сетки, execution.fib_unit)
        self.last_zmap: dict | None = None   # плиты (стена для среза сетки)
        self.last_xray: dict | None = None   # последний рентген (VPIN для базы)
        self.tick_size = 0.0                 # шаг цены (снап сетки), из margin
        self.orphan_orders: list[dict] = []  # лимитки сетки, которые не удалось
                                             # снять — добиваются каждый тик
        self._force_reconcile = False        # сверить с биржей ближайшим тиком
        # TIME_KILLSWITCH: срок жизни сигнала (env переопределяет для тестов)
        global TIME_KILL_SEC
        try:
            TIME_KILL_SEC = float(os.getenv("PYTHIA_TIME_KILL_SEC",
                                            TIME_KILL_SEC))
        except (TypeError, ValueError):
            pass
        self.foreign_lots = 0                # РУЧНЫЕ лоты владельца (изоляция):
                                             # бот их не открывал и не трогает
        # PYTHIA_ADOPT=1 возвращает старое поведение «принять позиции со
        # счёта как свои»; по умолчанию — ИЗОЛЯЦИЯ (спринт Zero-Touch)
        self.adopt = os.getenv("PYTHIA_ADOPT", "0") == "1"
        self._grid_poll_i = 0                # round-robin опрос лимиток сетки
        self._kur_figis: dict = {}           # кэш figi поводырей
        self._slow_ts = 0.0
        self._slow_busy = False
        self._ai_busy = False
        self._analysis_prev = True

    # ── ключи из интерфейса (как их сохранила панель) ───────────────────────
    @staticmethod
    def _cfg_tinkoff() -> str | None:
        try:
            try:
                from . import config as cfg
            except ImportError:
                import config as cfg
            return getattr(cfg, "TINKOFF_TOKEN", None) or None
        except Exception:                                    # noqa: BLE001
            return None

    @staticmethod
    def _cfg_deepseek() -> str | None:
        try:
            try:
                from . import config as cfg
            except ImportError:
                import config as cfg
            keys = cfg.deepseek_keys() if hasattr(cfg, "deepseek_keys") else []
            if keys:
                return keys[0]
            return getattr(cfg, "DEEPSEEK_API_KEY", None) or None
        except Exception:                                    # noqa: BLE001
            return None

    def status(self) -> dict:
        """Состояние для панели/эндпоинта: что делает прямо сейчас."""
        sr = self.session_risk.state() if self.session_risk else {}
        bmode = getattr(self.broker, "mode", "dry")
        return {
            "base": self.base, "mode": bmode,
            # правда о боевом — от БРОКЕРА (реально ли уходят ордера), не от arming
            "trades_real": bmode == "real",
            "broker_mode": bmode,
            "accumulating": not getattr(self, "analysis_ready", True),
            "pending": self.pending,
            "deposit": getattr(self, "deposit", None),
            "go_per_lot": self.go_per_lot,
            "position": self.position,
            "last_action": self.last_action,
            "observing": time.time() < self.observe_until,
            "observe_left_min": max(0, round((self.observe_until - time.time()) / 60)),
            "ai_bias": self.ai_hint.get("bias"),
            "killswitch": sr,
            "uptime_min": round((time.time() - self.started_ts) / 60, 1),
            "oracle": {k: self.oracle_verdict.get(k) for k in
                       ("state", "override", "dir", "p_long", "confidence",
                        "mode", "window_open", "predictability", "bif_regime",
                        "hawkes_n", "n_voices")}
            if self.oracle_verdict else None,
            "ladder": {k: self.last_ladder.get(k) for k in ("frac", "rung")}
            if self.last_ladder else None,
            "hw_stop": ((self.position or {}).get("hw_stop_level")
                        if self.position else None),
            "exit_grid": ([{"lots": g["lots"], "price": g["price"],
                            "level": g["level"]}
                           for g in (self.position or {}).get("exit_grid") or []]
                          or None),
            "isolation": not self.adopt,
            "foreign_lots": self.foreign_lots or None,
            "stream": (self.stream.stats() if self.stream else None),
            "kuramoto": ({"r": self.kuramoto.get("r"),
                          "leader": self.kuramoto.get("leader"),
                          "word": self.kuramoto.get("word")}
                         if self.kuramoto else None),
            "modulator": ((self.calendar or {}).get("modulator") or None),
            "sniper": ({"filled": self.sniper.get("filled"),
                        "total": self.sniper.get("total"),
                        "note": (self.sniper.get("plan") or {}).get("note")}
                       if self.sniper else None),
            "calendar": {"tongues": len((self.calendar or {}).get("tongues") or []),
                         "mathieu_unstable": bool((self.calendar or {})
                                                  .get("mathieu_unstable")),
                         "windows": (self.calendar or {}).get("windows")}
            if self.calendar else None,
            "wyckoff_bias": self.wyckoff_bias,
            "frame": "⚫ live = реальные деньги без подтверждений; тормоза: "
                     "killswitch, инфаркт-guard, ПАНИКА",
        }

    # ── подготовка: выбрать контракт, узнать баланс — секунды, не часы ──────
    async def prepare(self) -> bool:
        accs = await tinkoff.accounts() or []
        acc = accs[0]["id"] if accs else None
        pf = await tinkoff.portfolio(acc) if acc else None
        dep = self.deposit_override
        if dep is None:
            dep = (pf or {}).get("total_rub")
        self.deposit = float(dep or 0.0)
        if acc:
            self.broker.account_id = acc
        # класс актива: бот летит по ТОМУ, что разобрали — акция ИЛИ фьючерс.
        # Раньше жёстко "futures": по акции (SBER) петля не стартовала вовсе.
        asset_class = "futures"
        try:
            try:
                from . import instruments as _instruments
            except ImportError:
                import instruments as _instruments
            it = _instruments.get(self.base) or {}
            asset_class = it.get("asset_class") or asset_class
        except Exception:                                    # noqa: BLE001
            pass
        self.asset_class = asset_class
        inst = await tinkoff.resolve(self.base, asset_class)
        if not inst:
            log.error("инструмент %s (%s) не найден", self.base, asset_class)
            return False
        self.figi = inst.get("figi") or inst.get("uid")
        if asset_class == "futures":
            mg = await tinkoff.futures_margin(self.figi) or {}
            self.go_per_lot = mg.get("margin_buy")
            inc = mg.get("min_price_increment")
            amt = mg.get("min_price_increment_amount")
            self.point_value = (amt / inc) if inc else 1.0
            self.tick_size = float(inc or 0.0)   # снап фибо-сетки к шагу цены
        else:
            # акция: «ГО» лота = его стоимость (без плеча), пункт цены = лот штук
            lot = int(inst.get("lot") or 1)
            lp = await tinkoff.last_price(self.figi)
            px = (lp or {}).get("price") or 0.0
            self.go_per_lot = (px * lot) if px else None
            self.point_value = float(lot)
        # позиции, которые УЖЕ на счёте (спринт Zero-Touch — ИЗОЛЯЦИЯ по
        # умолчанию): бот управляет ТОЛЬКО тем, что открыл сам. Ручные лоты
        # владельца учитываются как foreign_lots и не трогаются — ни ведением,
        # ни стопами, ни ПАНИКОЙ этого инструмента. Старое поведение «принять
        # как свои» (журнал: «может продать, купить, что хочет») — за явным
        # рубильником PYTHIA_ADOPT=1. ⚠ Честно: биржа неттингует фьючерсы в
        # одну позицию на счёте, идеальной изоляции на ОДНОМ инструменте не
        # существует физически — бот изолирует УЧЁТ (open/close только своих
        # лотов), см. правило атрибуции в _reconcile.
        for p_ in (pf or {}).get("positions", []):
            if p_.get("figi") == self.figi and abs(p_.get("qty") or 0) >= 1:
                qty = p_.get("qty") or 0
                side = "long" if qty > 0 else "short"
                units = abs(qty)
                lots = (int(units) if self.asset_class == "futures"
                        else max(1, int(units / max(1.0, self.point_value))))
                if self.adopt:
                    px = float(p_.get("cur_price") or 0.0)
                    stop = (trader_risk.stop_from_atr(px, px * 0.002, side)
                            if px > 0 else 0.0)
                    self.position = {"side": side, "entry": px, "lots": lots,
                                     "stop": stop, "adopted": True,
                                     "opened_ts": time.time()}
                    self.last_action = (f"ПРИНЯЛ позицию со счёта: {side} {lots} "
                                        f"лот — теперь моя, веду по своду "
                                        f"(PYTHIA_ADOPT=1)")
                    log.info("ПРИНЯЛ позицию со счёта: %s %d лот (%s) — управляю",
                             side, lots, self.base)
                else:
                    self.foreign_lots = lots if side == "long" else -lots
                    self.last_action = (f"ИЗОЛЯЦИЯ: на счёте ручная позиция "
                                        f"{side} {lots} лот — НЕ трогаю, веду "
                                        f"только свои сделки")
                    log.info("ИЗОЛЯЦИЯ: ручные лоты владельца %+d (%s) — "
                             "не мои, не управляю", self.foreign_lots, self.base)
                break
        self.session_risk = trader_risk.SessionRisk(max(1.0, self.deposit))
        # калибровка голосов Оракула по журналу сделок (лечение «амнезии»:
        # trades.jsonl теперь ЧИТАЕТСЯ — бот помнит, какие голоса платили)
        try:
            try:
                from . import config as cfg
            except ImportError:
                import config as cfg
            rows = oracle.load_journal(cfg.DATA_DIR / "trades.jsonl")
            self.calib = oracle.calibrate(rows)
            if self.calib:
                log.info("калибровка голосов из журнала (%d сделок): %s",
                         len(rows), self.calib)
        except Exception:                                    # noqa: BLE001
            self.calib = {}
        # медленный контур сразу: свечи → бифуркации/VWAP/ATR/Вайкофф, небо
        await self._refresh_slow()
        log.info("автопилот готов: %s figi=%s ГО=%s депозит=%.0f режим=%s "
                 "бифуркации=%s", self.base, self.figi, self.go_per_lot,
                 self.deposit, self.mode["mode"],
                 ((self.bif or {}).get("summary") or {}).get("regime"))
        return True

    # ── МЕДЛЕННЫЙ КОНТУР: длинная память для Оракула (раз в ~10 минут) ──────
    async def _refresh_slow(self) -> None:
        """Свечи 15м/1д → двигатель бифуркаций, ATR-доля, VWAP сессии,
        Вайкофф-bias; реактор неба (эфемерид нет → честный None). Каждый слой
        в своей защите — сбой одного не гасит остальные (аддитивность)."""
        if self._slow_busy:
            return
        self._slow_busy = True
        try:
            cnd = await tinkoff.candles(self.figi, "15m", 7)
            closes = [c.get("c") for c in cnd if c.get("c")]
            if len(closes) >= 64:
                try:
                    self.bif = bifurcation.bifurcation_context(closes)
                except Exception as e:                       # noqa: BLE001
                    log.warning("бифуркации споткнулись: %s", str(e)[:80])
            if len(cnd) >= 15:
                try:                                         # ATR(14)/цена
                    trs = []
                    for a, b in zip(cnd[-15:], cnd[-14:]):
                        hi, lo = float(b["h"]), float(b["l"])
                        pc = float(a["c"])
                        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
                    last = float(cnd[-1]["c"]) or 1.0
                    self.atr_frac = (sum(trs) / len(trs)) / last
                except Exception:                            # noqa: BLE001
                    pass
                try:                                         # VWAP сессии (UTC-сутки)
                    day = (cnd[-1].get("t") or "")[:10]
                    pv = vv = 0.0
                    for c in cnd:
                        if (c.get("t") or "")[:10] != day:
                            continue
                        typ = (float(c["h"]) + float(c["l"]) + float(c["c"])) / 3.0
                        v = float(c.get("v") or 0)
                        pv += typ * v
                        vv += v
                    self.session_vwap = (pv / vv) if vv > 0 else None
                except Exception:                            # noqa: BLE001
                    pass
            d1 = []
            try:                                             # Вайкофф дневок
                d1 = await tinkoff.candles(self.figi, "1d", 400)
                if len(d1) >= 30:
                    rows = [{"o": c["o"], "h": c["h"], "l": c["l"],
                             "c": c["c"], "v": c["v"], "t": c["t"]} for c in d1]
                    wy = wyckoff.analyze(rows, tf="D1")
                    bias = wy.get("bias")
                    if bias is not None:
                        self.wyckoff_bias = max(-1.0, min(1.0, float(bias) / 30.0))
            except Exception as e:                           # noqa: BLE001
                log.warning("Вайкофф споткнулся: %s", str(e)[:80])
            if self.reactor is None:                         # небо: раз, не в петле
                try:
                    try:
                        from . import reactor_sky
                    except ImportError:
                        import reactor_sky
                    self.reactor = await asyncio.to_thread(
                        reactor_sky.reactor_context, self.base)
                except Exception as e:                       # noqa: BLE001
                    log.info("реактор неба недоступен (%s) — Оракул без неба",
                             str(e)[:60])
                    self.reactor = {"mode": "unavailable"}
            try:                # бифуркационный календарь (Матьё) — «Хищник» №4
                d1_closes = [c.get("c") for c in d1 if c.get("c")]
                self.calendar = bif_calendar.calendar(
                    d1_closes if len(d1_closes) >= 64 else None,
                    1440.0, reactor=self.reactor, now_ts=time.time())
            except Exception as e:                           # noqa: BLE001
                log.warning("календарь споткнулся: %s", str(e)[:80])
            try:                # граф Курамото поводырей («Сырая дата» №4)
                series = {}
                for tk in {self.base, *KURAMOTO_BASKET}:
                    figi = self._kur_figis.get(tk)
                    if figi is None:
                        inst = await tinkoff.resolve(tk, "futures")
                        figi = (inst or {}).get("figi") or (inst or {}).get("uid")
                        self._kur_figis[tk] = figi or ""
                    if not figi:
                        continue
                    cnd_k = await tinkoff.candles(figi, "15m", 7)
                    cl = [c.get("c") for c in cnd_k if c.get("c")]
                    if len(cl) >= 64:
                        series[tk] = cl
                self.kuramoto = kuramoto.graph(series)
                if self.kuramoto:
                    log.info("Курамото поводырей: r=%.2f вожак=%s",
                             self.kuramoto["r"], self.kuramoto["leader"])
            except Exception as e:                           # noqa: BLE001
                log.warning("Курамото споткнулся: %s", str(e)[:80])
        finally:
            self._slow_ts = time.time()
            self._slow_busy = False

    def _kur_blocked(self, side: str) -> bool:
        """ГЕЙТ ЛОВУШКИ («Сырая дата» №4): хор поводырей развалился — вход-
        одиночка блокируется. Сингулярность гейту не подчиняется."""
        kg = kuramoto.gate(side, (self.oracle_verdict or {}).get("state")
                           or "ПАРЛАМЕНТ", self.kuramoto)
        if kg.get("blocked"):
            self.last_action = "Курамото-гейт: " + kg["why"]
            return True
        return False

    def _poke_against(self) -> bool:
        """Прокол недавнего экстремума с возвратом (пища ветки Stop-Hunt —
        аудит: ветка была мертва, poke_against никогда не подавался)."""
        p = self.prices
        if len(p) < 140:
            return False
        recent, prior = p[-20:], p[-140:-20]
        lo_r, hi_r = min(recent), max(recent)
        lo_p, hi_p = min(prior), max(prior)
        last = p[-1]
        return bool((lo_r < lo_p and last > lo_p) or (hi_r > hi_p and last < hi_p))

    # ── ИИ-шифратор: бот схватывает новый вердикт и вписывает в контекст ────
    async def _refresh_ai_bg(self, xray: dict) -> None:
        """Фоновая обёртка refresh_ai: один полёт за раз, петля не ждёт."""
        self._ai_busy = True
        try:
            await self.refresh_ai(xray)
        finally:
            self._ai_busy = False

    async def refresh_ai(self, xray: dict, force: bool = False) -> None:
        now = time.time()
        actor = (xray or {}).get("actor") or ""
        if not force and now - self.ai_ts < AI_REFRESH_SEC and actor == self.last_actor:
            return
        self.last_actor = actor
        self.ai_ts = now
        try:                                                # best-effort, петлю не держим
            try:
                from . import ai as ai_mod
            except ImportError:
                import ai as ai_mod
            txt = await asyncio.wait_for(ai_mod.ask(
                "Ты шифратор направления для скальпинга. Отвечай строго: "
                "BIAS=LONG|SHORT|FLAT и одной строкой почему.",
                (f"Рынок {self.base}. Кто в стакане: {actor}. "
                 f"Метрики: {(xray or {}).get('metrics')}.")), timeout=45)
            up = (txt or "").upper()
            bias = ("long" if "LONG" in up else
                    "short" if "SHORT" in up else "flat")
            self.ai_hint = {"bias": bias, "note": (txt or "")[:200], "ts": now}
            log.info("ИИ вписал: %s", self.ai_hint["bias"])
        except Exception as e:                               # noqa: BLE001
            log.warning("ИИ молчит (%s) — работаем по своду без него", str(e)[:80])

    # ── тенденция из истории цен (фактор 1 защиты стопа) ────────────────────
    def _trend(self) -> str:
        """Куда всё идёт: сдвиг цены за ~минуту (40 тиков по 1.5с).
        «пока всё на падение» → down; рост → up; иначе flat."""
        if len(self.prices) < 40:
            return "flat"
        a, b = self.prices[-40], self.prices[-1]
        if a <= 0:
            return "flat"
        ch = (b - a) / a
        if ch > 0.0008:
            return "up"
        if ch < -0.0008:
            return "down"
        return "flat"

    # ── лимитка в полёте: позиция появляется ТОЛЬКО после исполнения ────────
    async def _pending_tick(self, price: float) -> None:
        po = self.pending
        st = await self.broker.order_state(po["order_id"])
        s = str(st.get("status") or "")
        if st.get("filled") and po.get("sniper"):
            # транш Снайпера: доусреднение позиции, не перезапись
            self.pending = None
            await self._sniper_fill(po["lots"], po["price"])
            return
        if st.get("filled"):
            self.pending = None
            e = po["price"]
            stop = trader_risk.stop_from_atr(
                e, max(e * 0.001, abs(po.get("mom") or 0.0) * e), po["side"])
            self.position = {"side": po["side"], "entry": e,
                             "lots": po["lots"], "stop": stop,
                             "voices": po.get("voices"),
                             "oconf": po.get("oconf"),
                             "opened_ts": time.time(),
                             "override_dir": po.get("override_dir")}
            await self._hw_stop_sync()               # биржевой трос сразу
            await self._grid_place()                 # фибо-сетка выхода сразу
            self.last_action = f"лимитка ИСПОЛНЕНА: {po['side']} {po['lots']} лот @{e}"
            log.info("ЛИМИТ ИСПОЛНЕН %s %d @%s: %s",
                     po["side"], po["lots"], e, po.get("why", ""))
            return
        # ЧАСТИЧНОЕ исполнение лимитки ВХОДА (ревью «Абсолюта», major):
        # раньше читался только filled — налитые лоты терялись, позиция
        # становилась «чужой» на сверке и жила без стопа и сетки.
        part = 0
        ex = st.get("exec_lots")
        if ex is not None:
            try:
                part = max(0, min(int(po["lots"]), int(ex)))
            except (TypeError, ValueError):
                part = 0
        if s.endswith("REJECTED") or s.endswith("CANCELLED"):
            self.pending = None
            if part > 0:
                await self._accept_partial_entry(po, part)
                return
            self.last_action = f"лимитку отбила биржа ({s}) — ищу вход заново"
            return
        ran = abs(price - po["price"]) / po["price"] if po["price"] else 0.0
        if time.time() - po["ts"] > 60 or ran > 0.0015:
            await self.broker.cancel(po["order_id"])
            self.pending = None
            if part > 0:                       # часть налили до снятия — берём
                await self._accept_partial_entry(po, part)
                return
            self.last_action = "лимитка не исполнилась / цена ушла — снял, ищу заново"
            return
        self.last_action = (f"жду исполнения лимитки @{po['price']} "
                            f"({int(time.time() - po['ts'])}с)")

    async def _accept_partial_entry(self, po: dict, lots: int) -> None:
        """Частично налитая лимитка ВХОДА → полноценная УПРАВЛЯЕМАЯ позиция
        на исполненный объём (стоп, биржевой трос, сетка выхода, таймер).
        Транш Снайпера идёт своим путём — там доусреднение."""
        if lots <= 0:
            return
        if po.get("sniper"):
            await self._sniper_fill(lots, po["price"])
            return
        e = po["price"]
        stop = trader_risk.stop_from_atr(
            e, max(e * 0.001, abs(po.get("mom") or 0.0) * e), po["side"])
        if self.position and self.position.get("side") == po["side"]:
            p = self.position                       # доусреднение своей же
            tot = p["lots"] + int(lots)
            p["entry"] = (p["entry"] * p["lots"] + e * int(lots)) / tot
            p["lots"] = tot
        else:
            self.position = {"side": po["side"], "entry": e, "lots": int(lots),
                             "stop": stop, "voices": po.get("voices"),
                             "oconf": po.get("oconf"),
                             "opened_ts": time.time(),
                             "override_dir": po.get("override_dir")}
        await self._hw_stop_sync()
        await self._grid_place()
        self.last_action = (f"лимитка исполнена ЧАСТИЧНО: {po['side']} "
                            f"{lots}/{po['lots']} лот @{e} — веду как позицию")
        log.info("ЛИМИТ ЧАСТИЧНО %s %d/%d @%s", po["side"], lots,
                 po["lots"], e)

    # ── сверка с биржей: правда всегда у неё, не у памяти бота ──────────────
    async def _reconcile(self, price: float) -> None:
        try:
            pf = await tinkoff.portfolio(self.broker.account_id)
        except Exception:                                    # noqa: BLE001
            return
        qty = 0.0
        for p_ in (pf or {}).get("positions", []):
            if p_.get("figi") == self.figi:
                qty = float(p_.get("qty") or 0)
        if self.asset_class != "futures" and self.point_value:
            qty = qty / max(1.0, self.point_value)
        have = 0
        if self.position:
            have = (self.position["lots"] if self.position["side"] == "long"
                    else -self.position["lots"])
        real = int(round(qty))
        own = int(have)
        if real == own + int(self.foreign_lots):
            return
        if self.adopt:
            # старое поведение (PYTHIA_ADOPT=1): правда биржи = наша позиция
            if real == 0:
                await self._grid_cancel()
                await self._hw_stop_cancel(self.position)   # сирота-трос не нужен
                self.position = None
                self.last_action = "сверка: на бирже позиции НЕТ — сбросил внутреннюю"
                log.warning("сверка: биржа=0, внутренняя=%s — принял правду биржи", have)
            else:
                side = "long" if real > 0 else "short"
                stop = ((self.position or {}).get("stop")
                        or (trader_risk.stop_from_atr(price, price * 0.002, side)
                            if price else 0.0))
                self.position = {"side": side,
                                 "entry": (self.position or {}).get("entry") or price,
                                 "lots": abs(real), "stop": stop,
                                 "adopted": True,
                                 "opened_ts": ((self.position or {})
                                               .get("opened_ts") or time.time())}
                self.last_action = f"сверка: биржа говорит {side} {abs(real)} лот — принял"
                log.warning("сверка: внутренняя=%s, биржа=%s — принял биржу", have, real)
            return
        # ИЗОЛЯЦИЯ (Zero-Touch). ⚠ Биржа неттингует фьючерсы в ОДНУ позицию
        # на счёте — идеальная изоляция на одном инструменте физически
        # невозможна. ПРАВИЛО АТРИБУЦИИ (уточнено ревью): наш учёт ограничен
        # ПРАВДОЙ БИРЖИ в нашу сторону —
        #     свои_лоты = min(наш учёт, max(0, нетто в нашу сторону)),
        #     чужие     = нетто − свои.
        # Два свойства, ради которых оно такое:
        #   · бот НИКОГДА не считает своих лотов больше, чем есть в нетто —
        #     иначе закрытие «своих» откроет противоположную позицию;
        #   · сокращение, ОБЪЯСНИМОЕ чужими лотами, к нашим НЕ относится:
        #     владелец закрыл свою ручную позицию — наши лоты целы (старое
        #     правило ошибочно списывало их и бросало живую позицию бота).
        sgn_own = 1 if own > 0 else (-1 if own < 0 else 0)
        if sgn_own:
            new_own = min(abs(own), max(0, int(real * sgn_own)))
            self.foreign_lots = real - new_own * sgn_own
            if new_own == abs(own):
                self.last_action = (f"сверка: мои {new_own} лот целы, ручных "
                                    f"{self.foreign_lots:+d} — не трогаю")
                return                       # наши целы, менять нечего
            if new_own == 0:
                await self._grid_cancel()
                await self._hw_stop_cancel(self.position)
                self.position = None
                # foreign_lots уже пересчитан правилом выше: весь остаток на
                # счёте теперь чужой (нетто мог схлопнуть наши лоты встречной
                # ручной сделкой владельца)
                self.last_action = ("сверка: МОИ лоты закрыты снаружи — учёт "
                                    f"снят, ручных лотов {self.foreign_lots:+d} "
                                    "— не трогаю")
                log.warning("сверка/изоляция: мои %s лот закрыты снаружи, "
                            "foreign=%+d", own, self.foreign_lots)
            else:
                self.position["lots"] = new_own
                await self._grid_cancel()        # сетка на старый размер неверна
                await self._hw_stop_sync()
                self.last_action = (f"сверка: мои лоты ужаты снаружи до "
                                    f"{new_own} — веду остаток, ручные не трогаю")
                log.warning("сверка/изоляция: мои %s → %s лот, foreign=%+d",
                            abs(own), new_own, self.foreign_lots)
        else:
            self.foreign_lots = real - own
            self.last_action = (f"сверка: ручная активность владельца — чужих "
                                f"лотов {self.foreign_lots:+d}, НЕ управляю ими")
            log.warning("сверка/изоляция: ручные лоты владельца %+d (мои %s)",
                        self.foreign_lots, own)

    # ── журнал сделок: разбор полётов по файлу, не по памяти ────────────────
    def _journal(self, rec: dict) -> None:
        try:
            import json
            try:
                from . import config as cfg
            except ImportError:
                import config as cfg
            p = cfg.DATA_DIR / "trades.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:                                    # noqa: BLE001
            pass

    # ── скальп по давлению: «игривый, разумно» (владелец) ───────────────────
    def _micro_pressure(self, xray: dict, m: dict | None) -> tuple[str | None, int, str]:
        """Миллиметрирование, когда чистого сетапа нет: 4 голоса давления —
        OBI (стены), CVD (реальные деньги), тяга вакуума (maya), тенденция.
        ≥3 голосов в одну сторону с перевесом ≥2 → малый вход в ту сторону.
        Это НЕ замена свода: размер меньше, стоп ближе, трейлинг тот же."""
        mx = (xray or {}).get("metrics") or {}
        up = dn = 0
        why = []
        obi = limits._f(mx.get("obi"))
        if obi > 0.12:
            up += 1; why.append(f"OBI {obi:+.2f}↑")
        elif obi < -0.12:
            dn += 1; why.append(f"OBI {obi:+.2f}↓")
        cvd = limits._f(mx.get("cvd"))
        if cvd > 0:
            up += 1; why.append("CVD↑")
        elif cvd < 0:
            dn += 1; why.append("CVD↓")
        pull = ((m or {}).get("pull") or {}).get("side")
        if pull == "вверх":
            up += 1; why.append("тяга↑")
        elif pull == "вниз":
            dn += 1; why.append("тяга↓")
        tr = self._trend()
        if tr == "up":
            up += 1; why.append("тренд↑")
        elif tr == "down":
            dn += 1; why.append("тренд↓")
        if up >= 3 and up - dn >= 2:
            return "long", up, " ".join(why)
        if dn >= 3 and dn - up >= 2:
            return "short", dn, " ".join(why)
        return None, 0, " ".join(why)

    # ── дрейф: ровное движение без рывков — «оч хороший момент» (владелец) ──
    def _drift(self) -> str | None:
        """Три отрезка по ~1 мин подряд в одну сторону + суммарный ход ≥0.12%
        + Хёрст>0.55 (персистентно, не пила) → едем с дрейфом."""
        p = self.prices
        if len(p) < 120 or not p[-120]:
            return None
        a, b, c, d = p[-120], p[-80], p[-40], p[-1]
        segs = [b - a, c - b, d - c]
        tot = (d - a) / a
        if abs(tot) < 0.0012:
            return None
        if microstructure.hurst(p[-120:]) <= 0.55:
            return None
        if all(s > 0 for s in segs) and tot > 0:
            return "long"
        if all(s < 0 for s in segs) and tot < 0:
            return "short"
        return None

    # ── один тик петли ──────────────────────────────────────────────────────
    async def tick(self) -> None:
        ob = await tinkoff.orderbook(self.figi, depth=50)
        tape = await tinkoff.last_trades(self.figi, minutes=15)
        st = await tinkoff.trading_status(self.figi)
        is_open = bool(st and (st.get("api_available") or st.get("market_order"))
                       and "NORMAL_TRADING" in str(st.get("status", "")))

        # «часик наблюдаем после открытия»: засекаем момент открытия
        if is_open and not self.market_was_open and OBSERVE_MIN > 0:
            self.observe_until = time.time() + OBSERVE_MIN * 60
            log.info("рынок открылся — наблюдаем %.0f мин (считаем, не торгуем)",
                     OBSERVE_MIN)
        self.market_was_open = is_open
        observing = time.time() < self.observe_until

        if not (ob and is_open):
            return                                           # закрыт/нет данных — ждём

        bb, ba = limits._f(ob.get("best_bid")), limits._f(ob.get("best_ask"))
        price = ((bb + ba) / 2 if bb and ba else limits._f(ob.get("last_price")))
        if price > 0:
            self.prices.append(price)
            del self.prices[:-HIST_MAX]

        self._tick_n += 1
        # новый расчёт Аналитика → новая макро-сессия: killswitch сбрасывается
        # (аудит: SessionRisk без сброса = блокировка навсегда)
        if self.analysis_ready and not self._analysis_prev and self.session_risk:
            self.session_risk.reset()
            log.info("новый расчёт Аналитика — killswitch сброшен")
        self._analysis_prev = self.analysis_ready
        # ордер в полёте: сперва решаем его судьбу; Снайпер двигает план следом
        if self.pending:
            await self._pending_tick(price)
            if self.sniper:
                await self._sniper_advance(price)
            # ЖИВАЯ ПОЗИЦИЯ ВСЕГДА ПРОХОДИТ ЗАЩИТУ (ревью «Абсолюта», crit):
            # ранний return на ветке набора обходил TIME_KILLSWITCH, ПАНИКУ,
            # стоп-гард, маржевой дозор и сверку — частично набранная позиция
            # оставалась голой, её держал только аппаратный трос.
            if not self.position:
                return
        elif self.sniper:
            await self._sniper_advance(price)
            if not self.position:
                return
        # сверка с биржей ~раз в минуту: правда у неё, не у памяти бота
        # орфаны (лимитки, которые не снялись) добиваются каждым тиком —
        # висящая на бирже лимитка после закрытия открывает противоположную
        await self._orphan_sweep()
        # Пока на бирже висит ПАССИВНАЯ сетка выхода, сверка идёт вчетверо
        # чаще: биржевой трос может закрыть позицию сам (флэш-ход, обрыв
        # связи), и тогда оставшиеся лимитки сетки откроют ПРОТИВОПОЛОЖНУЮ
        # позицию. Сверка их снимает — чем короче окно, тем меньше риск.
        _rec_every = (10 if ((self.position or {}).get("exit_grid")
                             or self.orphan_orders) else 40)
        if self.broker.mode == "real" and (self._force_reconcile
                                           or self._tick_n % _rec_every == 0):
            self._force_reconcile = False
            await self._reconcile(price)
        # медленный контур по расписанию — ФОНОМ, тик не ждёт
        if time.time() - self._slow_ts > SLOW_REFRESH_SEC and not self._slow_busy:
            asyncio.create_task(self._refresh_slow())

        # история ленты для норм абсорбции/истощения (объём и число сделок)
        t_vol = (limits._f((tape or {}).get("buy_vol"))
                 + limits._f((tape or {}).get("sell_vol")))
        t_cnt = limits._f((tape or {}).get("count"))
        self.tape_hist.append((t_vol, t_cnt))
        del self.tape_hist[:-240]
        vols = sorted(v for v, _ in self.tape_hist if v > 0)
        vol_norm = vols[len(vols) // 2] if vols else None      # медиана
        speed_peak = max((c for _, c in self.tape_hist), default=None)
        # ХОУКС («Сырая дата» №1): СЫРЫЕ ТАЙМСТАМПЫ из WS-потока (EM по
        # миллисекундам), поток мёртв → честный Фано-фолбэк на бинах
        if self._tape_prev_cnt is not None:
            self.tape_incr.append(max(0.0, t_cnt - self._tape_prev_cnt))
            del self.tape_incr[:-240]
        self._tape_prev_cnt = t_cnt
        raw_times = (self.stream.trade_times_ms(300.0)
                     if (self.stream and self.stream.alive()) else None)
        self.hawkes_now = hawkes.branching_best(raw_times,
                                                self.tape_incr[-120:])
        self.last_book = ob                          # стакан для нарезки Снайпера
        # медианный спред — база фибо-сетки выхода (в ЦЕНЕ, не bps)
        _bb = limits._f(ob.get("best_bid"))
        _ba = limits._f(ob.get("best_ask"))
        if _ba > _bb > 0:
            self.spread_hist.append(_ba - _bb)
            del self.spread_hist[:-240]

        m = maya.analyze(ob, tape)
        # живой рентген на ПОЛНОМ питании (аудит: 3 из 6 веток классификатора
        # были мертвы — Хёрст константа, абсорбция/истощение/VWAP не подавались)
        xray = microstructure.xray_live(
            ob, tape, prev_bids=self.prev_bids, prev_asks=self.prev_asks,
            prices=self.prices[-64:], vwap_now=self.session_vwap,
            vol_now=(t_vol if t_vol > 0 else None), vol_norm=vol_norm,
            speed_now=(t_cnt if t_cnt > 0 else None), speed_peak=speed_peak,
            poke_against=self._poke_against())
        self.last_xray = xray                # VPIN для базы фибо-сетки
        self.prev_bids = ob.get("levels_bid")
        self.prev_asks = ob.get("levels_ask")
        # ИИ-шифратор ФОНОМ: тик с открытой позицией не замирает на 45с
        # (аудит, причина №10: refresh_ai блокировал петлю)
        if not self._ai_busy:
            asyncio.create_task(self._refresh_ai_bg(xray))

        zmap = limits.zone_map(ob.get("levels_bid"), ob.get("levels_ask"), price)
        self.last_zmap = zmap                # плиты: стена для среза сетки
        bounce = limits.catch_bounce(self.prices[-120:])

        # ── ОРАКУЛ: все голоса → вероятность, режим, окно бифуркации ────────
        res_p = (zmap.get("resistance") or {}).get("p")
        sup_p = (zmap.get("support") or {}).get("p")
        corridor = ((res_p - sup_p) / price / 2
                    if (res_p and sup_p and price) else 0.0)
        verdict = oracle.verdict({
            "xray": xray, "maya": m, "bif": self.bif, "reactor": self.reactor,
            "trend": self._trend(), "drift": self._drift(),
            "bounce": bool(bounce.get("bounce")),
            "ai_bias": self.ai_hint.get("bias"),
            "wyckoff_bias": self.wyckoff_bias,
            "hurst": (xray.get("metrics") or {}).get("hurst"),
            "atr_frac": self.atr_frac, "corridor_frac": corridor,
            "hawkes": self.hawkes_now, "price": price,
            "plates": [p_ for p_ in (sup_p, res_p) if p_],
            "mathieu_unstable": bool((self.calendar or {}).get("mathieu_unstable")),
            "sens_mult": (((self.calendar or {}).get("modulator") or {})
                          .get("sens_mult")),
        }, self.calib)
        self.oracle_verdict = verdict
        bs_sum = ((self.bif or {}).get("summary") or {})
        # среда дня — в динамический killswitch («Хищник» №5): волатильный
        # день расширяет лимит, управляемый хаос удлиняет серию
        if self.session_risk:
            self.session_risk.set_env(
                atr_frac=self.atr_frac,
                managed_chaos=bool(verdict["window_open"]
                                   and verdict["state"] != oracle.ST_CHAOS))
        lad = leverage.ladder(
            verdict["confidence"], verdict["predictability"],
            window_open=verdict["window_open"],
            chaos=(verdict["state"] == oracle.ST_CHAOS),
            locked=bool(self.session_risk and not self.session_risk.can_trade()),
            dd=leverage.drawdown_frac(self.pnls, max(1.0, self.deposit or 1.0)),
            p_dir=(verdict["p_long"] if verdict["dir"] == "long"
                   else (1.0 - verdict["p_long"]) if verdict["dir"] == "short"
                   else None),
            rr=1.5,
            lyap_horizon=bs_sum.get("lyap_horizon"),
            tail_alpha=bs_sum.get("tail_alpha"))
        self.last_ladder = lad

        # ── в позиции: играем пока не повернётся + защита стопа ────────────
        if self.position:
            pos = self.position
            side = pos["side"]
            if pos.get("close_fail"):
                await self._close(price, "повтор закрытия после сбоя")
                return
            # фибо-сетка: опрос лимиток выхода (частичные фиксации лесенкой);
            # сетка могла добрать позицию целиком — тогда тик завершён
            await self._grid_tick(price)
            if not self.position:
                return
            # маржевой дозор: ГО не вмещается в капитал → ужаться, не ждать
            # маржин-колла брокера
            if await self._margin_guard(price):
                return
            if not self.position:
                return
            pos = self.position
            side = pos["side"]
            # TIME_KILLSWITCH («Абсолют»): жёсткий таймер жизни сигнала. В
            # квантовой среде горизонт Ляпунова сжигает вектор; если позиция
            # висит ≥ TIME_KILL_SEC и сетка не закрыла её целиком — рыночная
            # ликвидация остатка БЕЗ ИСКЛЮЧЕНИЙ (замер лога: 96→74→51% с
            # ростом удержания — держать дольше 10 мин статистически смерть).
            # Приоритет выше всех тактических выходов: время = враг.
            opened = pos.get("opened_ts")
            if opened and (time.time() - opened) >= TIME_KILL_SEC:
                await self._close(price, f"TIME_KILLSWITCH: сигнал прожил "
                                  f"{TIME_KILL_SEC:.0f}с — принудительная "
                                  "ликвидация (горизонт Ляпунова сжёг вектор)")
                return
            sgd = decision.stop_guard(side, price, pos.get("stop", 0), xray,
                                      trend=self._trend())
            if sgd["decision"] == "exit" or self.panic_flag:
                await self._close(price, sgd["reason"] if not self.panic_flag
                                  else "ПАНИКА владельца")
                if self.panic_flag:
                    self.stopping = True     # ПАНИКА = закрылись И ВСТАЛИ намертво
                return

            cvd = limits._f((xray.get("metrics") or {}).get("cvd"))
            actor = xray.get("actor") or ""
            trend = self._trend()
            entry = limits._f(pos.get("entry")) or price
            # ход в НАШУ пользу >0, против нас <0
            gain = ((price - entry) / entry if side == "long"
                    else (entry - price) / entry)
            # лучшая цена за жизнь позиции — для трейлинга прибыли
            pos["best"] = max(pos.get("best", gain), gain)
            # СТРУКТУРНЫЙ СЛОМ («Хищник» №5): вектор сингулярности был назван,
            # рынок ушёл против ≥ 2 ATR — ошиблась МОДЕЛЬ, а не среда шумнула:
            # закрываемся и killswitch намертво (независимо от процентов дня)
            if (pos.get("override_dir") and self.atr_frac
                    and gain <= -MODEL_BREAK_ATRS * self.atr_frac):
                self.session_risk.lock_structural(
                    f"вектор {pos['override_dir']}: {gain*100:.2f}% против "
                    f"при ATR {self.atr_frac*100:.2f}%")
                await self._close(price, "СТРУКТУРНЫЙ СЛОМ: вектор модели "
                                  "ошибся — выходим и стоим")
                return
            # безубыток: +0.2% взяли → стоп на вход, дальше игра на чужие
            if gain >= 0.002 and not pos.get("be"):
                pos["stop"] = entry
                pos["be"] = True
                self.last_action = f"безубыток: стоп подтянут ко входу {entry}"
                await self._hw_stop_sync()      # биржевой трос едет за стопом

            cvd_flip = ((side == "short" and cvd > 0) or
                        (side == "long" and cvd < 0))          # поток против нас
            trend_against = ((side == "short" and trend == "up") or
                             (side == "long" and trend == "down"))
            rev_actor = ("истощение" in actor or "ложный клевок" in actor
                         or "КИТ" in actor)

            # «повернулось» — ЛЮБОЕ из подтверждений, актора не ждём вечно:
            #  1) поток CVD против нас И тенденция развернулась против;
            #  2) актор разворота + CVD против (старое правило);
            #  3) сидим в минусе ≥0.35% и поток бьёт против — не ждём стопа.
            if cvd_flip and trend_against:
                await self._close(price, "повернулось: CVD-флип + тенденция против "
                                  "— не сидим, выходим")
                return
            if rev_actor and cvd_flip:
                await self._close(price, "повернулось: разворот подтверждён CVD+актором")
                return
            if gain <= -0.0035 and cvd_flip:
                await self._close(price, f"минус {gain*100:.2f}% и поток против — "
                                  "режем, не ждём стопа")
                return
            # СОЛИТОН-ВЫХОД (Агент 4 PREDATOR): импульс — уединённая волна,
            # выходим ДО стены ликвидности и ДО распада гребня, не на пике
            pos.setdefault("gains", []).append(gain)
            del pos["gains"][:-40]
            wall = ((zmap.get("resistance") or {}).get("p") if side == "long"
                    else (zmap.get("support") or {}).get("p"))
            sx = execution.soliton_exit(side, entry, price, wall, pos["gains"])
            if sx["exit"] and gain > 0:
                await self._close(price, "солитон: " + sx["reason"])
                return
            # трейлинг прибыли: взяли ≥0.3% и отдали от пика ≥0.12% → фиксируем
            if pos["best"] >= 0.003 and (pos["best"] - gain) >= 0.0012:
                await self._close(price, f"трейлинг: пик +{pos['best']*100:.2f}%, "
                                  f"откат — фиксируем +{gain*100:.2f}%")
                return
            self.last_action = (f"веду {side} {pos['lots']} лот · "
                                f"{gain*100:+.2f}% (пик {pos['best']*100:+.2f}%) · "
                                f"CVD {cvd:+.0f} {'против' if cvd_flip else 'за'}")
            return

        # ── вне позиции: вход только вне наблюдения и при живом killswitch ──
        if self.panic_flag:
            self.stopping = True             # ПАНИКА без позиции — просто встаём
            return
        # пока разбор ещё думает — КОПИМ данные (история, рентген уже набраны
        # выше), но реальные входы не открываем; как ИИ дал результат — торгуем
        if not self.analysis_ready:
            return
        if observing or not self.session_risk.can_trade():
            return
        if time.time() < self.no_entry_until:
            return                           # пауза после сделки — не молотим
        # ДУАЛЬНАЯ МЕХАНИКА: flat в кислоте (Хилл α<2) — это ВЕТО, а не «нет
        # мнения». Линейный мир отстранён целиком, и часовой ИИ-bias — тоже
        # голос парламента: фолбэком мимо вето он не ходит. Вход в кислоте —
        # только по вектору сингулярности (тогда dir != flat и мы сюда не
        # попадаем). Без этой ветки вето было декоративным: направлением
        # рулил самый слабый голос (W_AI=0.45) на ступени покоя лестницы.
        if verdict.get("env") == "кислота" and verdict["dir"] == "flat":
            self.last_action = ("кислота (α Хилла < 2): парламент отстранён, "
                                "вектора слома нет — наблюдаем, не входим")
            return
        # ПОЛНЫЙ контекст свода (аудит: раньше сюда доходил только стакан и
        # часовой ИИ-bias; эфир/реактор/бифуркации не доезжали ни одним числом)
        o_dir = verdict["dir"] if verdict["dir"] != "flat" \
            else self.ai_hint.get("bias", "flat")
        exp_move = max(corridor, verdict["expected_move_frac"])
        ctx = {
            "directive": {"dir": o_dir, "confidence": verdict["confidence"]},
            "xray": xray, "macro_dir": o_dir,
            "reactor": self.reactor, "oracle": verdict,
            "expected_move_frac": exp_move,
            "size_frac": lad["frac"],
            "deposit": self.deposit, "price": price,
            "go_per_lot": self.go_per_lot, "point_value": self.point_value,
        }
        dec = decision.decide(ctx)
        if dec.get("guard") == "spread_infarct":
            self.last_action = "инфаркт спреда — руки прочь, жду восстановления"
            return                           # ни свод, ни скальп в хаос не лезут
        if dec["action"] not in ("enter_long", "enter_short"):
            # фолбэк-входы сидят на ПОЛСТУПЕНИ лестницы Оракула (аудит,
            # причина №9: «фолбэк-входы жёстко 1 лот» — сила сигнала терялась)
            flots = (max(1, leverage.lots_from(self.deposit, lad["frac"] * 0.5,
                                               self.go_per_lot))
                     if self.go_per_lot else 1)
            # падение? любой серьёзный отскок ловим отдельной веткой
            if (bounce.get("bounce") and self.session_risk.can_trade()
                    and not self._kur_blocked("long")):
                await self._open("long", price, flots,
                                 sup_p, "ловим серьёзный отскок: " + bounce["reason"])
                return
            # дрейф вверх/вниз — ровное движение, едем с ним (момент владельца)
            drift = self._drift()
            if (drift and self.session_risk.can_trade()
                    and not self._kur_blocked(drift)):
                await self._open(drift, price, flots,
                                 sup_p if drift == "long" else res_p,
                                 "дрейф " + ("вверх" if drift == "long" else "вниз")
                                 + ": ровное персистентное движение — едем с ним")
                return
            # СКАЛЬП ПО ДАВЛЕНИЮ («игривый, разумно»): чистого сетапа нет, но
            # давление стакана+ленты+тяги+тренда в одну сторону → малый вход.
            mside, votes, mwhy = self._micro_pressure(xray, m)
            if (mside and self.session_risk.can_trade()
                    and not self._kur_blocked(mside)):
                # ход до противоположной стены должен перекрыть комиссию
                target = res_p if mside == "long" else sup_p
                move = (abs((target - price) / price) if target and price else 0.0)
                gate = decision.commission_gate(move)
                if gate["ok"]:
                    await self._open(mside, price, flots,
                                     sup_p if mside == "long" else res_p,
                                     f"скальп по давлению [{votes} голоса: {mwhy}], "
                                     f"до стены {move*100:.2f}%")
                    return
                self.last_action = (f"жив · скальп {mside} есть ({mwhy}), но до "
                                    f"стены {move*100:.2f}% — комиссия съест, жду")
                return
            # пульс: видно, что бот жив и ЧТО он видит (а не молчит часами)
            self._pulse += 1
            if self._pulse % 20 == 0:
                self.last_action = (f"жив · тик {self._pulse} · {price} · "
                                    f"{(xray.get('actor') or 'нет данных')} · "
                                    f"давление: {mwhy or 'ровно'} · жду сетап")
            return
        side = "long" if dec["action"] == "enter_long" else "short"
        if self._kur_blocked(side):
            return                          # ловушка: одиночка без хора
        level = sup_p if side == "long" else res_p
        await self._open(side, price, dec["lots"], level, dec["reason"], zmap)

    # ── СНАЙПЕР («Сырая дата» №3): умное исполнение крупного входа ─────────
    async def _sniper_fill(self, lots: int, px: float) -> None:
        """Транш исполнен: доусреднить/создать позицию, подвинуть трос."""
        sn = self.sniper
        if sn:
            sn["filled"] += int(lots)
        side = (sn or {}).get("side") or (self.position or {}).get("side")
        if not side:
            return
        vo = {k: v.get("s") for k, v in
              ((self.oracle_verdict.get("voices") or {})).items()}
        if self.position and self.position.get("side") == side:
            p = self.position
            tot = p["lots"] + int(lots)
            p["entry"] = (p["entry"] * p["lots"] + px * int(lots)) / tot
            p["lots"] = tot
        else:
            stop = trader_risk.stop_from_atr(
                px, max(px * 0.001, (self.atr_frac or 0.002) * px), side)
            self.position = {
                "side": side, "entry": px, "lots": int(lots), "stop": stop,
                "voices": vo, "oconf": self.oracle_verdict.get("confidence"),
                "opened_ts": time.time(),
                "override_dir": (side if self.oracle_verdict.get("override")
                                 else None)}
        self.last_action = (f"Снайпер: транш {lots} лот @{px} · набрано "
                            f"{(sn or {}).get('filled', '?')}"
                            f"/{(sn or {}).get('total', '?')}")
        log.info("СНАЙПЕР транш: %s %d лот @%.4f", side, lots, px)
        await self._hw_stop_sync()

    async def _sniper_advance(self, price: float) -> None:
        """Один шаг плана: эскалация по дедлайну, иначе следующий транш."""
        sn = self.sniper
        if not sn:
            return
        # СТРАЖ ЖИЗНИ ВХОДА (ревью «Абсолюта»): набор не может длиться вечно.
        # ПАНИКА/остановка рвут его сразу; иначе — предел TIME_KILL_SEC от
        # первой постановки плана. Без этого ургентный план с неисполняющейся
        # тенью перевзводился бесконечно и держал петлю на своей ветке.
        born = sn.setdefault("born_ts", time.time())
        too_old = (time.time() - born) >= TIME_KILL_SEC
        if self.panic_flag or self.stopping or too_old:
            if self.pending and self.pending.get("sniper"):
                try:
                    await self.broker.cancel(self.pending["order_id"])
                except Exception:                            # noqa: BLE001
                    pass
                self.pending = None
            self.sniper = None
            self.last_action = ("Снайпер: набор оборван ("
                                + ("ПАНИКА" if self.panic_flag else
                                   "остановка" if self.stopping else
                                   f"жизнь входа >{TIME_KILL_SEC:.0f}с")
                                + ") — остаток под защитой позиции")
            log.warning("СНАЙПЕР оборван: panic=%s stopping=%s age=%.0fс",
                        self.panic_flag, self.stopping, time.time() - born)
            if self.position:
                await self._grid_place()     # набранное — под сетку выхода
            return
        dirn = (trader_broker.BUY if sn["side"] == "long"
                else trader_broker.SELL)
        es = execution.escalate(sn["plan"], sn["started"], time.time(),
                                sn["filled"], sn["total"])
        if es["action"] == "done":
            self.last_action = (f"Снайпер: план набран "
                                f"({sn['filled']}/{sn['total']})")
            self.sniper = None
            await self._grid_place()     # позиция набрана — фибо-сетка выхода
            return
        if es["action"] == "reprice_rest":
            # «Абсолют»: рыночный ДОБОР ЗАПРЕЩЁН — перевзвод тени у свежего
            # best. Снимаем висящую тень, план перезапустится следующим
            # шагом с обновлённой ценой (idx откатывается на транш).
            if self.pending and self.pending.get("sniper"):
                await self.broker.cancel(self.pending["order_id"])
                self.pending = None
            # ПРЕДЕЛ ПЕРЕВЗВОДОВ (ревью, crit): бесконечный перевзвод держал
            # петлю на ветке Снайпера и обходил защиту позиции. Исчерпали
            # попытки — снимаем недобор и возвращаем управление.
            sn["repricings"] = int(sn.get("repricings", 0)) + 1
            if sn["repricings"] > MAX_REPRICINGS:
                self.sniper = None
                self.last_action = (f"Снайпер: {MAX_REPRICINGS} перевзводов "
                                    f"без исполнения — недобор снят, ведём "
                                    f"набранное")
                log.info("СНАЙПЕР: предел перевзводов — недобор %s лот снят",
                         es.get("left"))
                if self.position:
                    await self._grid_place()
                return
            sn["idx"] = max(0, sn["idx"] - 1)
            sn["started"] = time.time()          # новый отсчёт дедлайна
            self.last_action = "Снайпер: " + str(es.get("note"))
            log.info("СНАЙПЕР перевзвод %d/%d (рыночный добор запрещён): %s",
                     sn["repricings"], MAX_REPRICINGS, es.get("note"))
            return
        if es["action"] == "cancel_rest":
            if self.pending and self.pending.get("sniper"):
                await self.broker.cancel(self.pending["order_id"])
                self.pending = None
            self.last_action = "Снайпер: " + str(es.get("note"))
            self.sniper = None
            await self._grid_place()     # недобор снят — сетка на то, что есть
            return
        if self.pending:
            return                               # транш в полёте — ждём
        trs = sn["plan"]["tranches"]
        if sn["idx"] >= len(trs):
            return                               # всё подано — ждём/эскалация
        tr = trs[sn["idx"]]
        sn["idx"] += 1
        if tr["kind"] == "market":
            r = await self.broker.place(self.figi, dirn, tr["lots"],
                                        price=None, tag="sniper-mkt")
            if r.get("ok"):
                await self._sniper_fill(tr["lots"], price)
            return
        if tr["kind"] == "shadow":               # тень — на СВЕЖЕМ best
            bb = limits._f((self.last_book or {}).get("best_bid"))
            ba = limits._f((self.last_book or {}).get("best_ask"))
            px = (bb if sn["side"] == "long" else ba) or tr.get("price") or price
        else:                                    # капкан на уровне сброса
            px = tr.get("price") or price
        r = await self.broker.place(self.figi, dirn, tr["lots"], price=px,
                                    tag=f"sniper-{tr['kind']}")
        if not r.get("ok"):
            return
        st = str(r.get("status") or "")
        if (self.broker.mode == "dry" or st.endswith("_FILL") or st == "FILL"):
            await self._sniper_fill(tr["lots"], px)
            return
        self.pending = {"order_id": r.get("order_id"), "side": sn["side"],
                        "lots": tr["lots"], "price": px, "ts": time.time(),
                        "mom": 0.0, "why": sn["why"], "sniper": True,
                        "voices": {k: v.get("s") for k, v in
                                   ((self.oracle_verdict.get("voices")
                                     or {})).items()},
                        "oconf": self.oracle_verdict.get("confidence"),
                        "override_dir": (sn["side"]
                                         if self.oracle_verdict.get("override")
                                         else None)}
        self.last_action = (f"Снайпер: {tr['kind']} {tr['lots']} лот @{px} "
                            f"({sn['filled']}/{sn['total']})")

    # ── ФИБО-СЕТКА ВЫХОДА (Zero-Touch): пассивные тейки после FILL входа ────
    def _median_spread(self) -> float | None:
        s = sorted(x for x in self.spread_hist if x > 0)
        return s[len(s) // 2] if s else None

    async def _grid_place(self) -> None:
        """Ставит сетку лимиток выхода (execution.plan_exit_fib) на свежую
        СВОЮ позицию. База — медианный спред × Φ^(3/α)/√VPIN (fib_unit),
        спираль дышит средой Оракула, стена — ближайшая плита по ходу.
        Нет меры спреда → сетки нет (NO DUMMIES): активные выходы (солитон,
        трейлинг, стоп) ведут позицию как раньше — сетка аддитивна."""
        pos = self.position
        if not pos or pos.get("exit_grid") or pos.get("adopted"):
            return
        sm = self._median_spread()
        if not sm:
            return
        v = self.oracle_verdict or {}
        xm = (self.last_xray or {}).get("metrics") or {}
        zm = self.last_zmap or {}
        wall = ((zm.get("resistance") or {}).get("p") if pos["side"] == "long"
                else (zm.get("support") or {}).get("p"))
        # В КИСЛОТЕ (α≤2) — сетка по координатам ТЕНЗОРА КАЗИМИРА (граница
        # упругости + радиус пузыря); иначе обычная фибо-сетка. Спецификация
        # «Абсолют»: 38.2% на импульсе, 61.8% на дальнем уровне 4.
        if v.get("env") == "кислота":
            plan = execution.plan_exit_tensor(
                pos["side"], pos["entry"], pos["lots"], sm,
                alpha=v.get("hill_alpha"), env="кислота",
                tick_size=self.tick_size, wall=wall)
        else:
            unit = execution.fib_unit(sm, alpha=v.get("hill_alpha"),
                                      vpin=xm.get("vpin"))["unit"]
            plan = execution.plan_exit_fib(
                pos["side"], pos["entry"], pos["lots"], unit=unit,
                env=v.get("env", "неизвестно"), tick_size=self.tick_size,
                wall=wall)
        placed = []
        for o in plan["orders"]:
            r = await self.broker.place(
                self.figi,
                trader_broker.SELL if pos["side"] == "long" else trader_broker.BUY,
                o["lots"], price=o["price"], tag="exit-grid")
            if r.get("ok") and r.get("order_id"):
                placed.append({"order_id": r["order_id"], "lots": o["lots"],
                               "price": o["price"], "level": o["level"]})
            # не прошла — не беда: этот кусок закроют активные выходы
        if placed:
            pos["exit_grid"] = placed
            self.last_action = ("сетка выхода: "
                                + " · ".join(f"{g['lots']}л@{g['price']}"
                                             for g in placed))
            log.info("ФИБО-СЕТКА выставлена (%s): %s", plan["spiral"],
                     pos["exit_grid"])

    async def _grid_apply_fill(self, g: dict,
                               exec_lots: int | None = None) -> bool:
        """Учесть исполнение лимитки сетки (полное или ЧАСТИЧНОЕ по
        exec_lots): лоты вниз, P/L в killswitch и журнал, трос на остаток.
        True — позиция кончилась."""
        pos = self.position
        if not pos:
            return True
        res = execution.grid_fill_apply(pos.get("exit_grid") or [],
                                        g["order_id"], pos["lots"], exec_lots)
        pos["exit_grid"] = res["grid"]
        f = res["filled"]
        if not f:
            return False
        pnl = ((f["price"] - pos["entry"]) if pos["side"] == "long"
               else (pos["entry"] - f["price"])) * f["lots"] * self.point_value
        self.session_risk.record(pnl)
        self.pnls.append(pnl)
        self._journal({"ts": round(time.time(), 1), "side": pos["side"],
                       "lots": f["lots"], "entry": pos["entry"],
                       "exit": f["price"], "pnl": round(pnl, 2),
                       "why": ("фибо-сетка: уровень " + str(f["level"])
                               + (" (частично)" if res.get("partial") else "")),
                       "voices": pos.get("voices"),
                       "oracle_conf": pos.get("oconf")})
        if res["left_lots"] <= 0:
            await self._hw_stop_cancel(pos)
            self.position = None
            self.no_entry_until = time.time() + 20
            self.last_action = (f"сетка добрала позицию целиком (уровень "
                                f"{f['level']}, {pnl:+.0f}₽) — вышли лесенкой")
            log.info("ФИБО-СЕТКА: позиция закрыта целиком, pnl=%.1f₽", pnl)
            return True
        pos["lots"] = res["left_lots"]
        # исполнение сетки могло совпасть со срабатыванием ТРОСА на бирже
        # (тогда часть «зачтённых» лотов уже закрыл стоп, и P/L был бы
        # фантомным) — форсируем сверку с биржей ближайшим тиком
        self._force_reconcile = True
        await self._hw_stop_sync()                   # трос едет на остаток
        self.last_action = (f"сетка сняла {f['lots']} лот @{f['price']} "
                            f"({pnl:+.0f}₽), остаток {pos['lots']} лот")
        log.info("ФИБО-СЕТКА: %d лот @%.4f pnl=%.1f₽, остаток %d",
                 f["lots"], f["price"], pnl, pos["lots"])
        return False

    async def _grid_tick(self, price: float) -> None:
        """Ведение сетки: ОДНА лимитка за тик (round-robin — не душим API).
        Исполнение → частичная фиксация; позиция кончилась → выход лесенкой."""
        pos = self.position
        grid = (pos or {}).get("exit_grid") or []
        if not grid:
            return
        self._grid_poll_i %= len(grid)
        g = grid[self._grid_poll_i]
        self._grid_poll_i += 1
        st = await self.broker.order_state(g["order_id"])
        s = str(st.get("status") or "")
        if st.get("filled"):
            await self._grid_apply_fill(g)
            return
        ex = st.get("exec_lots")            # ЧАСТИЧНОЕ исполнение книжится
        if ex is not None:                  # сразу, не дожидаясь полного
            try:
                done = max(0, min(int(g["lots"]), int(ex)))
            except (TypeError, ValueError):
                done = 0
            if done:
                await self._grid_apply_fill(g, exec_lots=done)
                return
        if s.endswith("REJECTED") or s.endswith("CANCELLED"):
            pos["exit_grid"] = [x for x in (pos.get("exit_grid") or [])
                                if x["order_id"] != g["order_id"]]

    async def _grid_settle(self) -> bool:
        """РАСЧЁТ СЕТКИ ПЕРЕД РЫНОЧНЫМ ПРИКАЗОМ (закрывает гонку «лимитка
        исполнилась, но round-robin до неё ещё не дошёл»).

        Опрашивает ВСЕ лимитки сетки разом: исполненные — учитываются
        (лоты вниз), неисполненные — снимаются, и только после этого
        вызывающий шлёт рыночный ордер. Без этого _close/_margin_guard
        считали бы объём по УСТАРЕВШЕМУ pos['lots'] и продали бы больше,
        чем осталось на бирже, — переворот в противоположную сторону.

        Снятие, вернувшее отказ, трактуется как «могла исполниться»: ордер
        перепроверяется, и его лоты списываются. True — позиции больше нет."""
        pos = self.position
        if not pos:
            return True
        orphans: list[dict] = []
        for g in list(pos.get("exit_grid") or []):
            try:
                st = await self.broker.order_state(g["order_id"])
            except Exception:                                # noqa: BLE001
                st = {}
            filled = bool(st.get("filled"))
            # ЧАСТИЧНОЕ исполнение: filled=False, но часть лотов уже ушла —
            # книжим ровно её (иначе исполненная часть выпадет из учёта и
            # рыночный выход продаст её второй раз)
            ex = st.get("exec_lots")
            done = None
            if not filled and ex is not None:
                try:
                    done = max(0, min(int(g["lots"]), int(ex)))
                except (TypeError, ValueError):
                    done = None
            if filled:
                if await self._grid_apply_fill(g):
                    return True                              # позиция закрыта
                continue
            if done:
                if await self._grid_apply_fill(g, exec_lots=done):
                    return True
            # остаток лимитки снимаем; отказ снятия = ордер, возможно, ЖИВ
            try:
                r = await self.broker.cancel(g["order_id"])
                ok = (r is None) or bool(r.get("ok", True))
            except Exception:                                # noqa: BLE001
                ok = False
            if ok:
                pos = self.position
                if not pos:
                    return True
                pos["exit_grid"] = [x for x in (pos.get("exit_grid") or [])
                                    if x["order_id"] != g["order_id"]]
                continue
            # снять не удалось: могла исполниться между опросом и снятием —
            # перепроверяем у биржи
            try:
                st2 = await self.broker.order_state(g["order_id"])
            except Exception:                                # noqa: BLE001
                st2 = {}
            if st2.get("filled"):
                if await self._grid_apply_fill(g):
                    return True
                continue
            ex2 = st2.get("exec_lots")
            if ex2 is not None:
                try:
                    d2 = max(0, min(int(g["lots"]), int(ex2)))
                except (TypeError, ValueError):
                    d2 = 0
                if d2 and await self._grid_apply_fill(g, exec_lots=d2):
                    return True
            # ордер ЖИВ и не снялся — НЕ теряем его: орфан уезжает в отдельный
            # список, который добивается КАЖДЫЙ тик и переживает закрытие
            # позиции (выход при этом не блокируем: висящая лимитка опаснее
            # орфана только на бумаге, а несостоявшийся стоп-лосс — всегда)
            cur = self.position
            src = [x for x in ((cur or {}).get("exit_grid") or [])
                   if x["order_id"] == g["order_id"]]
            orphans.extend(src or [dict(g)])
            log.warning("сетка: лимитку %s снять не удалось — в орфаны, "
                        "добиваю каждый тик", g["order_id"])
        if orphans:
            self.orphan_orders.extend(orphans)
        if self.position:
            self.position["exit_grid"] = []
        return self.position is None

    async def _orphan_sweep(self) -> None:
        """Добивание неснятых лимиток сетки: живёт отдельно от позиции, чтобы
        орфан не остался на бирже после закрытия и не открыл противоположную
        позицию. Исполнившийся орфан ловит сверка (_reconcile)."""
        if not self.orphan_orders:
            return
        left = []
        for g in list(self.orphan_orders):
            try:
                st = await self.broker.order_state(g["order_id"])
            except Exception:                                # noqa: BLE001
                st = {}
            s = str(st.get("status") or "")
            if st.get("filled") or s.endswith("CANCELLED") or s.endswith("REJECTED"):
                continue                     # судьба решена — сверка учтёт
            try:
                r = await self.broker.cancel(g["order_id"])
                if (r is None) or bool(r.get("ok", True)):
                    continue                 # снят
            except Exception:                                # noqa: BLE001
                pass
            left.append(g)
        if len(left) != len(self.orphan_orders):
            log.info("орфаны сетки: осталось %d", len(left))
        self.orphan_orders = left

    async def _grid_cancel(self) -> None:
        """Совместимость: снять сетку без расчёта (используется там, где
        позиция уже снята с учёта). Рыночные приказы обязаны идти через
        _grid_settle, а не через этот метод."""
        pos = self.position
        for g in (pos or {}).get("exit_grid") or []:
            try:
                await self.broker.cancel(g["order_id"])
            except Exception:                                # noqa: BLE001
                pass
        if pos:
            pos["exit_grid"] = []

    async def _margin_guard(self, price: float) -> bool:
        """Маржевой дозор Zero-Touch: ГО позиции не должно превышать капитал
        сессии (депозит + P/L дня). Биржа подняла ГО или день съел капитал →
        ужимаемся рыночным до вмещающегося размера, НЕ дожидаясь маржин-колла
        брокера. Возврат True — позиция была ужата (тик завершать)."""
        pos = self.position
        if not (pos and self.go_per_lot):
            return False
        # капитал = депозит + РЕАЛИЗОВАННЫЙ P/L + ПЛАВАЮЩИЙ по открытой позиции:
        # дозор, считающий только реализованное, слепнет ровно там, где он
        # нужен — когда позиция в глубоком минусе и ГО уже не обеспечено
        float_pnl = (((price - pos["entry"]) if pos["side"] == "long"
                      else (pos["entry"] - price))
                     * pos["lots"] * self.point_value) if price else 0.0
        equity = max(1.0, (self.deposit or 0.0) + sum(self.pnls) + float_pnl)
        if pos["lots"] * self.go_per_lot <= equity:
            return False
        # расчёт сетки ДО рыночного ужатия: часть лотов могла уже уйти
        # лимиткой — иначе ужимаем по устаревшему объёму
        if await self._grid_settle():
            return True                          # сетка закрыла позицию сама
        pos = self.position
        if not pos:
            return True
        equity = max(1.0, (self.deposit or 0.0) + sum(self.pnls)
                     + (((price - pos["entry"]) if pos["side"] == "long"
                         else (pos["entry"] - price))
                        * pos["lots"] * self.point_value if price else 0.0))
        fit = max(0, int(equity / self.go_per_lot))
        excess = pos["lots"] - fit
        if excess <= 0:
            return False
        r = await self.broker.place(
            self.figi,
            trader_broker.SELL if pos["side"] == "long" else trader_broker.BUY,
            excess, tag="margin-guard")
        if not r.get("ok"):
            self.last_action = "маржевой дозор: ужать не вышло — повторю"
            return True
        pnl = ((price - pos["entry"]) if pos["side"] == "long"
               else (pos["entry"] - price)) * excess * self.point_value
        self.session_risk.record(pnl)
        self.pnls.append(pnl)
        self._journal({"ts": round(time.time(), 1), "side": pos["side"],
                       "lots": excess, "entry": pos["entry"], "exit": price,
                       "pnl": round(pnl, 2),
                       "why": f"маржевой дозор: ГО {pos['lots']} лот не "
                              f"вмещается в капитал {equity:.0f}₽"})
        if fit == 0:
            await self._hw_stop_cancel(pos)
            self.position = None
            self.last_action = "маржевой дозор: капитал не держит ни лота — закрылся"
        else:
            pos["lots"] = fit
            await self._hw_stop_sync()
            await self._grid_place()             # сетка на новый размер
            self.last_action = (f"маржевой дозор: ужался {excess} лот → "
                                f"{fit} (ГО не вмещалось)")
        log.warning("МАРЖЕВОЙ ДОЗОР: ужато %d лот, pnl=%.1f₽", excess, pnl)
        return True

    # ── АППАРАТНЫЙ СТОП («Хищник» №4): трос на сервере биржи ───────────────
    async def _hw_stop_sync(self) -> None:
        """Катастрофический stop-market НА БИРЖЕ: глубже мягкого стопа на
        CAT_STOP_EXTRA (потолок допуска stop_guard — школа ложных клевков
        не тронута: мягкий стоп ведёт бот, трос ловит смерть процесса,
        обрыв связи и флэш-ход). Переносится при движении мягкого стопа."""
        pos = self.position
        if not pos or not pos.get("stop") or not self.figi:
            return
        side = pos["side"]
        level = (pos["stop"] * (1.0 - CAT_STOP_EXTRA) if side == "long"
                 else pos["stop"] * (1.0 + CAT_STOP_EXTRA))
        old_id, old_lv = pos.get("hw_stop_id"), pos.get("hw_stop_level")
        if (old_lv and abs(level - old_lv) / old_lv < 0.001
                and pos.get("hw_stop_lots") == pos["lots"]):
            return                                   # трос уже там, объём тот же
        try:
            if old_id:
                await self.broker.cancel_stop(old_id)
                pos["hw_stop_id"] = None
            r = await self.broker.place_stop(
                self.figi,
                trader_broker.SELL if side == "long" else trader_broker.BUY,
                int(pos["lots"]), round(level, 6), tag="catastrophe")
            if r.get("ok"):
                pos["hw_stop_level"] = level
                pos["hw_stop_lots"] = pos["lots"]
                if not r.get("virtual"):
                    pos["hw_stop_id"] = r.get("stop_order_id")
                    log.info("биржевой трос: %s %s лот @%.4f (id=%s)",
                             side, pos["lots"], level, pos["hw_stop_id"])
        except Exception as e:                       # noqa: BLE001
            log.warning("трос не встал (%s) — стоп пока мягкий", str(e)[:80])

    async def _hw_stop_cancel(self, pos: dict | None) -> None:
        """Снять биржевой трос (позиция закрыта/сброшена)."""
        if pos and pos.get("hw_stop_id"):
            try:
                await self.broker.cancel_stop(pos["hw_stop_id"])
            except Exception:                        # noqa: BLE001
                pass
            pos["hw_stop_id"] = None

    # ── исполнение входа по лимитной тактике ───────────────────────────────
    async def _open(self, side: str, price: float, lots: int,
                    level: float | None, why: str, zmap: dict | None = None) -> None:
        mom = 0.0
        if len(self.prices) >= 8:
            mom = (self.prices[-1] - self.prices[-8]) / self.prices[-8]
        # крупный вход ведёт СНАЙПЕР («Сырая дата» №3): нарезка с участием,
        # тени у спреда, капкан на сбросе; рыночный всем объёмом — запрещён
        if int(lots) >= SNIPER_MIN_LOTS and self.last_book:
            plan = execution.plan_entry(
                side, int(lots), price, self.last_book,
                state=(self.oracle_verdict or {}).get("state") or "ПАРЛАМЕНТ",
                prices=self.prices[-240:])
            if plan["tranches"]:
                self.sniper = {"plan": plan, "side": side, "filled": 0,
                               "total": int(lots), "started": time.time(),
                               "why": why, "level": level, "idx": 0}
                log.info("СНАЙПЕР ведёт вход %s %d лот: %s",
                         side, lots, plan["note"])
                self.last_action = "Снайпер: " + plan["note"]
                await self._sniper_advance(price)
                return
        style = limits.entry_style(price, level or price, side, mom)
        if style["style"] == "limit":
            armed = limits.arm_limit(price, level or price, side)
            if not armed["arm"]:
                return                                       # заранее не ставим — ждём подхода
            oprice = armed["level"]                          # лимит у точки
        else:
            oprice = None                                    # ближайший = рыночный
        r = await self.broker.place(
            self.figi,
            trader_broker.BUY if side == "long" else trader_broker.SELL,
            lots, price=oprice, tag="auto")
        if not r.get("ok"):
            self.last_action = ("вход НЕ прошёл: "
                                + str(r.get("note") or r.get("error") or "?")[:80])
            return
        st = str(r.get("status") or "")
        vo = {k: v.get("s") for k, v in
              ((self.oracle_verdict.get("voices") or {})).items()}
        oconf = self.oracle_verdict.get("confidence")
        if (oprice is not None and self.broker.mode != "dry"
                and not (st.endswith("_FILL") or st == "FILL")):
            # лимит ПРИНЯТ биржей, но НЕ исполнен: позиции ещё НЕТ — ждём/снимем
            self.pending = {"order_id": r.get("order_id"), "side": side,
                            "lots": lots, "price": oprice, "ts": time.time(),
                            "mom": mom, "why": why,
                            "voices": vo, "oconf": oconf,
                            "override_dir": (side if self.oracle_verdict.get("override")
                                             else None)}
            self.last_action = f"лимитка ждёт @{oprice} ({side} {lots} лот)"
            return
        fill_px = oprice or price
        stop = trader_risk.stop_from_atr(
            fill_px, max(fill_px * 0.001, abs(mom) * fill_px), side)
        self.position = {"side": side, "entry": fill_px, "lots": lots,
                         "stop": stop, "voices": vo, "oconf": oconf,
                         "opened_ts": time.time(),
                         "override_dir": (side if self.oracle_verdict.get("override")
                                          else None)}
        await self._hw_stop_sync()                   # биржевой трос сразу
        await self._grid_place()                     # фибо-сетка выхода сразу
        self.last_action = (f"ВХОД {side} {lots} лот @{fill_px} · "
                            f"стоп {round(stop, 4)}")
        log.info("ВХОД %s %d лот @%s (%s): %s", side, lots, fill_px,
                 style["style"], why)

    async def _close(self, price: float, why: str) -> None:
        pos = self.position
        if not pos:
            return
        # СЕТКА РАСЧИТЫВАЕТСЯ ДО рыночного закрытия: снимаем неисполненные
        # лимитки И учитываем те, что уже исполнились (иначе рыночный ордер
        # уйдёт на устаревший объём и перевернёт позицию в противоположную)
        if await self._grid_settle():
            return                       # сетка уже закрыла позицию целиком
        pos = self.position
        if not pos:
            return
        r = await self.broker.place(
            self.figi,
            trader_broker.SELL if pos["side"] == "long" else trader_broker.BUY,
            pos["lots"], tag="close")
        if not r.get("ok"):
            # РЕАЛЬНАЯ позиция осталась на бирже — забывать её НЕЛЬЗЯ.
            pos["close_fail"] = pos.get("close_fail", 0) + 1
            self.last_action = ("ЗАКРЫТИЕ НЕ ПРОШЛО ("
                                + str(r.get("error") or r.get("note") or "?")[:60]
                                + f") — попытка {pos['close_fail']}, повторю")
            log.error("закрытие не прошло (попытка %s): %s", pos["close_fail"], r)
            return
        await self._hw_stop_cancel(pos)              # трос снят вместе с позицией
        self.position = None
        pnl = ((price - pos["entry"]) if pos["side"] == "long"
               else (pos["entry"] - price)) * pos["lots"] * self.point_value
        self.session_risk.record(pnl)
        self.pnls.append(pnl)                # губернатор просадки лестницы
        # пауза после сделки — не молотим комиссию; после минуса — длиннее
        self.no_entry_until = time.time() + (45 if pnl < 0 else 20)
        # журнал сделки С ГОЛОСАМИ Оракула на входе: по нему calibrate()
        # учится, какие голоса платят (аудит: «пишется и не читается»)
        self._journal({"ts": round(time.time(), 1), "side": pos["side"],
                       "lots": pos["lots"], "entry": pos["entry"],
                       "exit": price, "pnl": round(pnl, 2), "why": why,
                       "voices": pos.get("voices"),
                       "oracle_conf": pos.get("oconf")})
        self.last_action = f"ВЫХОД {pos['side']} {pos['lots']} лот, P/L {pnl:+.0f}₽ · {why}"
        log.info("ВЫХОД %s %d лот @%.2f pnl=%.1f₽: %s (ok=%s)",
                 pos["side"], pos["lots"], price, pnl, why, r.get("ok"))

    def panic(self) -> None:
        """Кнопка ПАНИКА: следующий тик закрывает всё немедленно."""
        self.panic_flag = True

    # ── фоновая петля: работа кипит, сбой тика не роняет ────────────────────
    async def run(self) -> None:
        if not await self.prepare():
            self.last_action = "не стартовал: контракт/данные недоступны"
            return
        # WS-поток сырых тиков и стакана («Сырая дата» №1): математика по
        # миллисекундам; поток умер → честный откат на REST-агрегаты
        tkn = self._cfg_tinkoff() or os.getenv("TINKOFF_TOKEN")
        if tkn and self.figi:
            self.stream = stream.TickStream(self.figi, tkn)
            self.stream.start()
            log.info("WS-поток запущен: сырые тики + дельты стакана")
        bmode = getattr(self.broker, "mode", "dry")
        log.info("ПЕТЛЯ ПОШЛА (%s): без кнопок, режим %s", self.base, bmode)
        self.last_action = ("петля пошла — "
                            + ("БОЕВОЙ, ордера реальные" if bmode == "real"
                               else f"режим {bmode} (ордера в лог)"))
        self.stopping = False
        while not getattr(self, "stopping", False):
            t0 = time.time()
            try:
                await self.tick()
            except Exception as e:                           # noqa: BLE001
                log.warning("тик споткнулся (%s) — петля живёт", str(e)[:100])
            await asyncio.sleep(max(0.2, TICK_SEC - (time.time() - t0)))
        if self.stream:
            await self.stream.stop()
        log.info("петля %s остановлена владельцем", self.base)
        self.last_action = "остановлен владельцем"

    def stop(self) -> None:
        """Мягкая остановка петли (кнопка СТОП в панели)."""
        self.stopping = True


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    base = sys.argv[1] if len(sys.argv) > 1 else "CR"
    ap = Autopilot(base)
    print(f"АВТОПИЛОТ {base}: режим {ap.mode['mode']} — {ap.mode['note']}")
    asyncio.run(ap.run())


if __name__ == "__main__":
    main()
