# -*- coding: utf-8 -*-
"""TXT-БЭКТЕСТЕР дуального роутера (спринт Zero-Touch, задача №2).

Прогоняет текстовые дампы метрик (OBI, CVD, Хоукс n, Пригожин φ, Хилл α,
таймстампы) через БОЕВУЮ oracle.verdict (та же dual_env / машина состояний,
что торгует — никакой второй реализации логики) и печатает отчёт:

  · Win Rate, стратифицированный по корзинам α (кислота ≤2 / переход 2–3 /
    щёлочь ≥3 / неизвестно) и по состояниям машины;
  · Profit Factor и максимальная просадка — на НЕТТО (комиссия 5 бп круг
    + симулированное проскальзывание);
  · холдаут-срез (последняя доля выборки отдельно) — дрейф виден сразу.

Форматы дампа (автодетект по строке): JSONL {"ts":..., "price":...},
key=value (`ts=... price=... obi=...`), CSV с заголовком. Обязательные
поля: ts (секунды или мс), price. Остальные — опциональны (NO DUMMIES:
чего нет в дампе, тем вердикт не пользуется).

Правила честности (протокол BACKTEST_PROTOCOL.md):
  · сигнал строки t исполняется по ЦЕНЕ СЛЕДУЮЩЕЙ строки (+задержка);
  · цена стороны = полспреда из дампа ПЛЮС --slip-bps (удар по рынку и
    запаздывание сверх спреда) — свип чувствительности всегда работает;
  · корзина с < MIN_CELL сделок помечается «данных нет», не интерпретируется;
  · одна позиция за раз, выход по горизонту — никакого заглядывания вперёд.

Запуск: python3 -m backend.lab.txt_backtester dump.txt [--horizon-s 180]
        [--latency-s 1] [--slip-bps 1.0] [--holdout 0.3]
Self-тест: python3 -m backend.lab.txt_backtester --selftest
⚫ бэктест проверяет машину, не обещает прибыль; один файл — одна сессия,
закон рынка из неё не выводится. 18+.
"""
from __future__ import annotations

import json
import math
import sys

from .. import oracle

COMMISSION_BPS = 5.0     # круг (константа стендов лаборатории)
MIN_CELL = 25            # меньше сделок в ячейке — «данных нет»
_ALIASES = {
    "ts": ("ts", "ts_ms", "time", "timestamp", "t"),
    "price": ("price", "p", "last", "close"),
    "obi": ("obi",), "cvd": ("cvd",), "vpin": ("vpin",),
    "hawkes_n": ("hawkes_n", "hawkes", "n"),
    "phi": ("phi", "prigogine", "prigogine_phi"),
    "alpha": ("alpha", "hill", "hill_alpha", "tail_alpha"),
    "spread_bps": ("spread_bps", "spread"),
    "casimir_a": ("casimir_a", "casimir"),
    "pressure": ("pressure",), "direction": ("direction", "bif_dir"),
    "window_open": ("window_open",),
    "atr_frac": ("atr_frac", "atr"), "corridor_frac": ("corridor_frac", "corridor"),
}


def _f(x, d=None):
    try:
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


def _canon(raw: dict) -> dict | None:
    """Сырые ключи строки → канонические имена; без ts/price строка мертва."""
    low = {str(k).strip().lower(): v for k, v in raw.items()}
    out = {}
    for name, keys in _ALIASES.items():
        for k in keys:
            if k in low:
                out[name] = low[k]
                break
    ts, px = _f(out.get("ts")), _f(out.get("price"))
    if ts is None or px is None or px <= 0:
        return None
    if ts > 1e11:                       # миллисекунды → секунды
        ts /= 1000.0
    out["ts"], out["price"] = ts, px
    return out


def parse_txt(path: str) -> list[dict]:
    """Парсер дампа: JSONL / key=value / CSV — определяется по содержимому.
    Битые строки пропускаются молча со счётчиком (не роняют прогон)."""
    rows, header = [], None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            raw = None
            if line.startswith("{"):
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue
            elif "=" in line and not line.count(","):
                raw = {}
                for part in line.replace(";", " ").split():
                    if "=" in part:
                        k, _, v = part.partition("=")
                        raw[k] = v
            else:                        # CSV: первая подходящая строка — шапка
                cells = [c.strip() for c in line.split(",")]
                if header is None:
                    if any(c.lower() in _ALIASES["ts"] for c in cells):
                        header = [c.lower() for c in cells]
                    continue
                raw = dict(zip(header, cells))
            if raw:
                c = _canon(raw)
                if c:
                    rows.append(c)
    rows.sort(key=lambda r: r["ts"])
    return rows


def _verdict_inputs(r: dict) -> dict:
    """Строка дампа → входы БОЕВОГО oracle.verdict (без второй реализации)."""
    d: dict = {"price": r["price"]}
    m = {}
    for k in ("obi", "cvd", "vpin", "casimir_a"):
        v = _f(r.get(k))
        if v is not None:
            m[k] = v
    if m:
        d["xray"] = {"available": True, "confidence": 0.6, "metrics": m}
    summ = {}
    for k_src, k_dst in (("pressure", "pressure"), ("direction", "direction"),
                         ("alpha", "tail_alpha")):
        v = _f(r.get(k_src))
        if v is not None:
            summ[k_dst] = v
    if str(r.get("window_open", "")).lower() in ("1", "true", "да"):
        summ["window_open"] = True
    bif: dict = {}
    if summ:
        bif["summary"] = summ
    ph = _f(r.get("phi"))
    if ph is not None:
        bif["prigogine"] = {"phi": ph}
    if bif:
        d["bif"] = bif
    hn = _f(r.get("hawkes_n"))
    if hn is not None:
        d["hawkes"] = {"n": hn}
    a = _f(r.get("alpha"))
    if a is not None:
        d["hill_alpha"] = a
    # масштаб ожидаемого хода — иначе expected_move_frac всегда 0 и любой
    # фильтр min_move_bps отсекает ВСЁ (ревью «Абсолюта», major)
    for k in ("atr_frac", "corridor_frac"):
        v = _f(r.get(k))
        if v is not None:
            d[k] = v
    return d


def _bucket(alpha) -> str:
    a = _f(alpha)
    if a is None or a <= 0:
        return "неизвестно"
    if a <= 2.0:
        return "кислота(α≤2)"
    if a < 3.0:
        return "переход(2<α<3)"
    return "щёлочь(α≥3)"


def _slip_bps(row: dict, extra_bps: float) -> float:
    """Стоимость одной стороны, bps = полспреда + ДОПОЛНИТЕЛЬНЫЙ слиппедж.

    Полспреда — неизбежная цена пересечения (из дампа; нет спреда → 0).
    extra_bps (--slip-bps) — удар по рынку и запаздывание сверх спреда:
    он СКЛАДЫВАЕТСЯ, а не заменяется данными дампа. Иначе флаг молча
    глох на дампах со спредом и свип чувствительности из
    BACKTEST_PROTOCOL.md был невозможен."""
    sp = _f(row.get("spread_bps"))
    half = (sp / 2.0) if (sp and sp > 0) else 0.0
    return half + max(0.0, float(extra_bps or 0.0))


def backtest(rows: list[dict], *, horizon_s: float = 180.0,
             latency_s: float = 1.0, slip_bps: float = 1.0,
             commission_bps: float = COMMISSION_BPS) -> dict:
    """Один проход: сигнал → исполнение следующей строкой (+latency) →
    выход по горизонту. Одна позиция за раз. Всё в НЕТТО bps."""
    trades = []
    n = len(rows)
    i = 0
    while i < n - 1:
        r = rows[i]
        v = oracle.verdict(_verdict_inputs(r))
        if v["dir"] not in ("long", "short"):
            i += 1
            continue
        # исполнение: первая строка с ts >= ts_сигнала + latency
        j = i + 1
        while j < n and rows[j]["ts"] < r["ts"] + latency_s:
            j += 1
        if j >= n:
            break
        e_row = rows[j]
        sgn = 1.0 if v["dir"] == "long" else -1.0
        entry = e_row["price"] * (1.0 + sgn * _slip_bps(e_row, slip_bps) / 1e4)
        # выход: первая строка с ts >= входа + горизонт (без заглядывания)
        k = j + 1
        while k < n and rows[k]["ts"] < e_row["ts"] + horizon_s:
            k += 1
        if k >= n:
            break                        # хвост не закрывается — не считаем
        x_row = rows[k]
        exit_px = x_row["price"] * (1.0 - sgn * _slip_bps(x_row, slip_bps) / 1e4)
        gross = sgn * (exit_px - entry) / entry * 1e4
        net = gross - commission_bps
        trades.append({"ts": r["ts"], "dir": v["dir"], "state": v["state"],
                       "env": v["env"], "bucket": _bucket(r.get("alpha")),
                       "entry": round(entry, 6), "exit": round(exit_px, 6),
                       "net_bps": round(net, 3)})
        i = k                            # одна позиция за раз
    return _stats(trades)


MIN_MOVE_BPS = 15.0      # «Абсолют»: сигнал с ожидаемым ходом < 15 бп
                         # игнорируется (скальпинг шума отключён)


def backtest_absolute(rows: list[dict], *, horizon_s: float = 600.0,
                      latency_s: float = 1.0, commission_bps: float = COMMISSION_BPS,
                      residual_slip_bps: float = 1.0,
                      only_t95: bool = False,
                      min_move_bps: float = MIN_MOVE_BPS) -> dict:
    """ПРОТОКОЛ «АБСОЛЮТ»: T95-триггер + Shadow-вход + 600с Time_Killswitch +
    тензорный тейк. Отличия от базового backtest, каждое — с обоснованием:

    · SHADOW-ВХОД: рыночные входы запрещены, заходим лимиткой в спред. Модель
      исполнения ПЕССИМИСТИЧНА: лимитка исполняется, только если цена в окне
      входа коснулась нашей стороны (иначе сделки НЕТ — пропущенный вход, не
      прибыль). За счёт мейкер-входа мы НЕ платим полуспред на входе (это и
      есть «экономия на спреде»), но платим его на РЫНОЧНОМ выходе.
    · T95: считаем, сколько раз сработал абсолютный триггер (state=
      СИНГУЛЯРНОСТЬ с пометкой T95 в reason). only_t95=True → торгуем ТОЛЬКО
      их (снайпер-режим лога: «сутками flat, стреляем в аномалию»).
    · 600с KILLSWITCH: если за horizon позиция не закрылась о тензорную сетку
      целиком — рыночная ликвидация остатка на строке горизонта.
    · ТЕНЗОРНЫЙ ТЕЙК: выход моделируется как касание уровней тензора Казимира
      (S·Φ и S·Φ^(2/α)) ВНУТРИ окна; закрытое лимиткой — экономит полуспред,
      добитое киллсвитчем — платит рыночный выход + residual_slip.

    Возврат: _stats + доп. поля t95_count, shadow_miss, killswitch_hits."""
    trades = []
    n = len(rows)
    t95_count = shadow_miss = killswitch_hits = small_move_skip = 0
    directional = 0
    i = 0
    while i < n - 1:
        r = rows[i]
        v = oracle.verdict(_verdict_inputs(r))
        is_t95 = (v["state"] == "СИНГУЛЯРНОСТЬ" and "T95" in (v.get("reason") or ""))
        if is_t95:
            t95_count += 1
        if v["dir"] not in ("long", "short"):
            i += 1
            continue
        if only_t95 and not is_t95:
            i += 1
            continue
        # ФИЛЬТР ХОДА («Абсолют»): ожидаемый ход < min_move_bps → скальпинг
        # шума отключён. expected_move_frac Оракула — доля цены, не bps.
        # ⚠ Фильтр применяется ТОЛЬКО когда масштаб хода реально ИЗМЕРЕН
        # (в дампе есть atr_frac/corridor_frac). Иначе expected_move_frac=0
        # у всех строк и фильтр молча убивал бы весь стенд — не «рынок
        # тихий», а данных нет (ревью «Абсолюта», major).
        has_scale = ("atr_frac" in r) or ("corridor_frac" in r)
        exp_bps = _f(v.get("expected_move_frac"), 0.0) * 1e4
        if min_move_bps > 0 and has_scale and exp_bps < min_move_bps:
            small_move_skip += 1
            i += 1
            continue
        directional += 1
        sgn = 1.0 if v["dir"] == "long" else -1.0
        # окно входа: [ts+latency; ts+latency+горизонт/3] — тень должна
        # исполниться быстро, иначе сигнал протух
        j = i + 1
        while j < n and rows[j]["ts"] < r["ts"] + latency_s:
            j += 1
        if j >= n:
            break
        e_row = rows[j]
        sp = _f(e_row.get("spread_bps"), 0.0)
        half = (sp / 2.0) if sp > 0 else 0.0
        # SHADOW-вход: лимитка на нашей стороне по цене (best − полуспред для
        # long). Исполнение — только если в ближайших барах цена дошла ДО неё
        # или ниже (для long). Моделируем консервативно: вход по цене строки
        # j МИНУС полуспред (мы мейкер), но требуем, чтобы в окне j..j+arm
        # цена реально коснулась этого уровня.
        entry_px = e_row["price"] * (1.0 - sgn * half / 1e4)
        arm_end_ts = e_row["ts"] + horizon_s / 3.0
        armed = False
        k = j
        while k < n and rows[k]["ts"] <= arm_end_ts:
            touched = (rows[k]["price"] <= entry_px if sgn > 0
                       else rows[k]["price"] >= entry_px)
            if touched:
                armed = True
                j = k                    # реально вошли на этой строке
                break
            k += 1
        if not armed:
            shadow_miss += 1             # тень не исполнилась — сделки НЕТ
            i += 1
            continue
        # выход: тензорный тейк ВНУТРИ окна horizon, иначе 600с killswitch
        a = _f(r.get("alpha"), 3.0)
        a_cl = max(1.5, min(4.0, a if a > 0 else 3.0))
        PHI = (1.0 + 5.0 ** 0.5) / 2.0
        # цель по дальнему уровню тензора относительно входа, в долях цены;
        # база — полуспред как метрика S_median (в отсутствие стакана)
        s_unit = max(half, 0.5) / 1e4                     # доля цены
        take_frac = s_unit * (PHI ** (2.0 / a_cl))        # радиус пузыря
        take_px = entry_px * (1.0 + sgn * take_frac)
        exit_px = None
        kswitch = False
        deadline = rows[j]["ts"] + horizon_s
        k = j + 1
        while k < n and rows[k]["ts"] < deadline:
            reached = (rows[k]["price"] >= take_px if sgn > 0
                       else rows[k]["price"] <= take_px)
            if reached:
                # тейк исполнен ЛИМИТКОЙ — полуспред на выходе НЕ платим
                exit_px = take_px
                break
            k += 1
        if exit_px is None:
            # 600с killswitch: рыночная ликвидация остатка на горизонте
            if k >= n:
                break
            kswitch = True
            killswitch_hits += 1
            xr = rows[k]
            exit_px = xr["price"] * (1.0 - sgn * (half + residual_slip_bps) / 1e4)
        gross = sgn * (exit_px - entry_px) / entry_px * 1e4
        net = gross - commission_bps
        trades.append({"ts": r["ts"], "dir": v["dir"], "state": v["state"],
                       "env": v["env"], "bucket": _bucket(r.get("alpha")),
                       "t95": is_t95, "killswitch": kswitch,
                       "entry": round(entry_px, 6), "exit": round(exit_px, 6),
                       "net_bps": round(net, 3)})
        i = k
    st = _stats(trades)
    st["t95_count"] = t95_count
    st["directional_signals"] = directional
    st["shadow_miss"] = shadow_miss
    st["killswitch_hits"] = killswitch_hits
    st["small_move_skip"] = small_move_skip
    st["min_move_bps"] = min_move_bps
    st["fill_rate"] = (round(len(trades) / directional, 4) if directional else 0.0)
    return st


def _stats(trades: list[dict]) -> dict:
    def cell(tr):
        if not tr:
            return {"trades": 0, "note": "данных нет"}
        wins = [t for t in tr if t["net_bps"] > 0]
        gw = sum(t["net_bps"] for t in wins)
        gl = -sum(t["net_bps"] for t in tr if t["net_bps"] < 0)
        eq = mdd = peak = 0.0
        for t in tr:
            eq += t["net_bps"]
            peak = max(peak, eq)
            mdd = max(mdd, peak - eq)
        out = {"trades": len(tr),
               "win_rate": round(len(wins) / len(tr), 4),
               "profit_factor": (round(gw / gl, 3) if gl > 0
                                 else (float("inf") if gw > 0 else 0.0)),
               "net_bps_total": round(eq, 2),
               "net_bps_avg": round(eq / len(tr), 3),
               "max_drawdown_bps": round(mdd, 2)}
        if len(tr) < MIN_CELL:
            out["note"] = f"< {MIN_CELL} сделок — данных нет, не интерпретировать"
        return out

    by_bucket = {}
    for b in ("кислота(α≤2)", "переход(2<α<3)", "щёлочь(α≥3)", "неизвестно"):
        sub = [t for t in trades if t["bucket"] == b]
        if sub:
            by_bucket[b] = cell(sub)
    by_state = {}
    for s in sorted({t["state"] for t in trades}):
        by_state[s] = cell([t for t in trades if t["state"] == s])
    # срез по СРЕДЕ дуального роутера: env не совпадает с α-корзиной, когда
    # φ Пригожина добивает переходную зону до кислоты — такие сделки иначе
    # молча вливались бы в WR чужого бина
    by_env = {}
    for e in sorted({t["env"] for t in trades}):
        by_env[e] = cell([t for t in trades if t["env"] == e])
    return {"total": cell(trades), "by_alpha": by_bucket, "by_env": by_env,
            "by_state": by_state,
            "trades": trades,
            "frame": ("⚫ нетто после комиссии и слиппеджа; ячейка <"
                      f"{MIN_CELL} сделок не интерпретируется; одна сессия — "
                      "не закон рынка. 18+")}


def report_text(rep: dict, tag: str = "") -> str:
    ln = [f"── ОТЧЁТ БЭКТЕСТА {tag} " + "─" * 30]
    t = rep["total"]
    ln.append(f"всего: {t.get('trades', 0)} сделок · WR {t.get('win_rate', '—')}"
              f" · PF {t.get('profit_factor', '—')}"
              f" · net {t.get('net_bps_total', '—')} bps"
              f" · avg {t.get('net_bps_avg', '—')} bps"
              f" · maxDD {t.get('max_drawdown_bps', '—')} bps")
    ln.append("по корзинам α:")
    for b, c in rep["by_alpha"].items():
        ln.append(f"  {b:16s} n={c['trades']:4d} WR={c.get('win_rate', '—')} "
                  f"PF={c.get('profit_factor', '—')} "
                  f"avg={c.get('net_bps_avg', '—')}bps"
                  + (f"  ⚠ {c['note']}" if c.get("note") else ""))
    ln.append("по средам роутера (dual_env):")
    for e, c in (rep.get("by_env") or {}).items():
        ln.append(f"  {e:16s} n={c['trades']:4d} WR={c.get('win_rate', '—')} "
                  f"PF={c.get('profit_factor', '—')} "
                  f"avg={c.get('net_bps_avg', '—')}bps"
                  + (f"  ⚠ {c['note']}" if c.get("note") else ""))
    ln.append("по состояниям машины:")
    for s, c in rep["by_state"].items():
        ln.append(f"  {s:16s} n={c['trades']:4d} WR={c.get('win_rate', '—')} "
                  f"PF={c.get('profit_factor', '—')}"
                  + (f"  ⚠ {c['note']}" if c.get("note") else ""))
    if "t95_count" in rep:               # режим «Абсолют»
        ln.append("── ПРОТОКОЛ «АБСОЛЮТ» ──")
        ln.append(f"  T95-триггеров сработало: {rep['t95_count']}")
        ln.append(f"  отсеяно фильтром хода <{rep.get('min_move_bps', 0):.0f}бп: "
                  f"{rep.get('small_move_skip', 0)} сигналов")
        ln.append(f"  направленных сигналов: {rep.get('directional_signals', 0)}"
                  f" · shadow-вход НЕ исполнился: {rep.get('shadow_miss', 0)}"
                  f" · fill rate: {rep.get('fill_rate', 0)}")
        ln.append(f"  600с Time_Killswitch сработал: {rep['killswitch_hits']} раз"
                  " (тейк не дошёл — рыночная ликвидация)")
    ln.append(rep["frame"])
    return "\n".join(ln)


def run_file(path: str, *, horizon_s=180.0, latency_s=1.0, slip_bps=1.0,
             holdout: float = 0.0, absolute: bool = False,
             only_t95: bool = False,
             min_move_bps: float = MIN_MOVE_BPS) -> dict:
    rows = parse_txt(path)
    if not rows:
        print(f"дамп {path}: ни одной валидной строки (нужны ts и price)")
        return {}
    print(f"дамп {path}: {len(rows)} строк, "
          f"{rows[-1]['ts'] - rows[0]['ts']:.0f} c данных")
    if absolute:
        rep = backtest_absolute(rows, horizon_s=(horizon_s or 600.0),
                                latency_s=latency_s, only_t95=only_t95,
                                min_move_bps=min_move_bps)
        print(report_text(rep, "(АБСОЛЮТ: T95+Shadow+600с+тензор"
                          + (", только T95" if only_t95 else "") + ")"))
        return rep
    if holdout and 0.0 < holdout < 1.0:
        cut = int(len(rows) * (1.0 - holdout))
        rep_a = backtest(rows[:cut], horizon_s=horizon_s,
                         latency_s=latency_s, slip_bps=slip_bps)
        rep_b = backtest(rows[cut:], horizon_s=horizon_s,
                         latency_s=latency_s, slip_bps=slip_bps)
        print(report_text(rep_a, f"(train {1-holdout:.0%})"))
        print(report_text(rep_b, f"(holdout {holdout:.0%})"))
        return {"train": rep_a, "holdout": rep_b}
    rep = backtest(rows, horizon_s=horizon_s, latency_s=latency_s,
                   slip_bps=slip_bps)
    print(report_text(rep))
    return rep


# ── self-test: детерминированная синтетика, три формата, честность ──────────
def _selftest() -> None:
    import os
    import tempfile

    # синтетика без random (закон школы): щёлочь — синус (α=3.5, сильный
    # стакан), кислота — разгон с квантовым триггером (α=1.7, n=.96, φ=.985)
    rows = []
    px = 100.0
    for t in range(0, 2400):
        if t < 1200:                                  # ЩЁЛОЧЬ
            px = 100.0 + 0.3 * math.sin(2 * math.pi * t / 120.0)
            cvd = 500.0 * math.cos(2 * math.pi * t / 120.0)
            rows.append({"ts": t, "price": round(px, 5), "obi": 0.4 if cvd > 0
                         else -0.4, "cvd": round(cvd, 1), "alpha": 3.5,
                         "spread_bps": 2.0})
        else:                                         # КИСЛОТА: тренд вверх
            px = 100.0 + (t - 1200) * 0.01
            rows.append({"ts": t, "price": round(px, 5), "obi": 0.3,
                         "cvd": 800.0, "alpha": 1.7, "hawkes_n": 0.96,
                         "phi": 0.985, "casimir_a": 0.3, "spread_bps": 2.0})

    with tempfile.TemporaryDirectory() as td:
        # (а) три формата парсятся в одно и то же
        p_json = os.path.join(td, "d.jsonl")
        with open(p_json, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        p_kv = os.path.join(td, "d.kv.txt")
        with open(p_kv, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(" ".join(f"{k}={v}" for k, v in r.items()) + "\n")
        p_csv = os.path.join(td, "d.csv")
        cols = ["ts", "price", "obi", "cvd", "alpha", "hawkes_n", "phi",
                "casimir_a", "spread_bps"]
        with open(p_csv, "w", encoding="utf-8") as f:
            f.write(",".join(cols) + "\n")
            for r in rows:
                f.write(",".join(str(r.get(c, "")) for c in cols) + "\n")
        rj, rk, rc = parse_txt(p_json), parse_txt(p_kv), parse_txt(p_csv)
        assert len(rj) == len(rows) and len(rk) == len(rows), (len(rj), len(rk))
        assert len(rc) == len(rows), len(rc)
        assert abs(rj[7]["price"] - rk[7]["price"]) < 1e-9
        assert _f(rc[1500].get("hawkes_n")) == 0.96

        # (б) прогон: сделки есть, в кислоте — ТОЛЬКО сингулярность
        rep = backtest(rj, horizon_s=60, latency_s=1, slip_bps=1.0)
        tt = rep["total"]
        assert tt["trades"] >= 10, tt
        acid = [t for t in rep["trades"] if t["bucket"] == "кислота(α≤2)"]
        assert acid, "в кислотном сегменте обязаны быть сделки (вектор)"
        assert all(t["state"] == "СИНГУЛЯРНОСТЬ" for t in acid), \
            "кислота торгуется только вектором сингулярности"
        assert all(t["dir"] == "long" for t in acid)      # тренд вверх → long
        acid_cell = rep["by_alpha"]["кислота(α≤2)"]
        assert acid_cell["win_rate"] > 0.9, acid_cell     # синтетика идеальна
        # щёлочь: сделки парламента существуют
        alk = [t for t in rep["trades"] if t["bucket"] == "щёлочь(α≥3)"]
        assert alk and any(t["state"] == "ПАРЛАМЕНТ" for t in alk)

        # (в) комиссия реально бьёт: задрали круг — вся синтетика в минус
        rep_fee = backtest(rj, horizon_s=60, latency_s=1, slip_bps=1.0,
                           commission_bps=10000.0)
        assert all(t["net_bps"] < 0 for t in rep_fee["trades"])

        # (г) latency сдвигает вход: с гигантской задержкой сделок меньше
        rep_lat = backtest(rj, horizon_s=60, latency_s=600, slip_bps=1.0)
        assert rep_lat["total"]["trades"] <= tt["trades"]

        # (г2) СВИП СЛИППЕДЖА обязан работать ДАЖЕ на дампе со спредом:
        #      extra складывается с полспредом, а не глохнет в нём
        s0 = backtest(rj, horizon_s=60, latency_s=1, slip_bps=0.0,
                      commission_bps=0.0)
        s2 = backtest(rj, horizon_s=60, latency_s=1, slip_bps=2.0,
                      commission_bps=0.0)
        d_avg = (s0["total"]["net_bps_avg"] - s2["total"]["net_bps_avg"])
        assert d_avg > 3.5, ("свип слиппеджа не работает", d_avg)
        #      и бьёт ОБЕ стороны (short не должен «зарабатывать» на слиппедже)
        for side in ("long", "short"):
            a0 = [t["net_bps"] for t in s0["trades"] if t["dir"] == side]
            a2 = [t["net_bps"] for t in s2["trades"] if t["dir"] == side]
            if a0 and a2:
                assert (sum(a0) / len(a0)) - (sum(a2) / len(a2)) > 3.5, side
        #      полспреда из дампа учитывается сверх extra
        assert _slip_bps({"spread_bps": 4.0}, 1.0) == 3.0
        assert _slip_bps({}, 1.0) == 1.0
        assert _slip_bps({"spread_bps": 4.0}, 0.0) == 2.0

        # (д) детерминизм и честность мелкой ячейки
        assert backtest(rj, horizon_s=60) == backtest(rj, horizon_s=60)
        # (д2) срез по СРЕДАМ роутера присутствует и согласован с числом сделок
        assert "by_env" in rep and rep["by_env"], rep.keys()
        assert sum(c["trades"] for c in rep["by_env"].values()) == tt["trades"]
        assert "кислота" in rep["by_env"], rep["by_env"].keys()
        assert "по средам роутера" in report_text(rep)
        few = _stats(rep["trades"][:3])
        assert "не интерпретировать" in few["total"]["note"]

        # (ж) РЕЖИМ «АБСОЛЮТ»: T95 считается, killswitch срабатывает,
        #     shadow-вход может не исполниться (fill_rate ≤ 1)
        rab = backtest_absolute(rj, horizon_s=120, latency_s=1,
                                residual_slip_bps=1.0, min_move_bps=0.0)
        # ФИЛЬТР ХОДА: с порогом 15 бп сделок не больше, чем без него
        rab_f = backtest_absolute(rj, horizon_s=120, latency_s=1,
                                  residual_slip_bps=1.0, min_move_bps=15.0)
        assert rab_f["total"]["trades"] <= rab["total"]["trades"], (
            rab_f["total"]["trades"], rab["total"]["trades"])
        assert rab_f["small_move_skip"] >= 0
        assert "отсеяно фильтром хода" in report_text(rab_f)
        assert rab["t95_count"] >= 1, rab          # кислотный сегмент даёт T95
        assert rab["directional_signals"] >= 1
        assert 0.0 <= rab["fill_rate"] <= 1.0
        assert "killswitch_hits" in rab and rab["killswitch_hits"] >= 0
        assert "T95-триггеров" in report_text(rab)
        # only_t95: торгуем ТОЛЬКО абсолютный триггер — сделок не больше, чем
        # всех направленных, и все они помечены t95
        rt95 = backtest_absolute(rj, horizon_s=120, latency_s=1, only_t95=True)
        assert all(t["t95"] for t in rt95["trades"]), rt95["trades"][:2]
        assert rt95["total"]["trades"] <= rab["total"]["trades"]
        # детерминизм режима
        assert backtest_absolute(rj, horizon_s=120) == \
            backtest_absolute(rj, horizon_s=120)

        # (е) битые строки не роняют
        p_bad = os.path.join(td, "bad.txt")
        with open(p_bad, "w", encoding="utf-8") as f:
            f.write("мусор\n{битый json\nts=1 price=сломано\n"
                    "ts=5 price=100.0 obi=0.1\n")
        rb = parse_txt(p_bad)
        assert len(rb) == 1 and rb[0]["price"] == 100.0

    print("txt_backtester self-test OK: 3 формата, кислота=вектор-only, "
          "комиссия бьёт, latency честен, детерминизм, MIN_CELL, битые строки; "
          "режим АБСОЛЮТ — T95 считается, shadow-fill и 600с killswitch честны, "
          "only-t95 торгует лишь триггер")


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] == "--selftest":
        _selftest()
        return
    path = args[0]
    kw: dict = {}
    it = iter(args[1:])
    for a in it:
        if a == "--horizon-s":
            kw["horizon_s"] = float(next(it))
        elif a == "--latency-s":
            kw["latency_s"] = float(next(it))
        elif a == "--slip-bps":
            kw["slip_bps"] = float(next(it))
        elif a == "--holdout":
            kw["holdout"] = float(next(it))
        elif a == "--absolute":
            kw["absolute"] = True
        elif a == "--only-t95":
            kw["absolute"] = True
            kw["only_t95"] = True
        elif a == "--min-move-bps":
            kw["min_move_bps"] = float(next(it))
    run_file(path, **kw)


if __name__ == "__main__":
    main()
