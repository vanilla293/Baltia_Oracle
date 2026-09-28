# -*- coding: utf-8 -*-
"""ОРАКУЛ // ПИФИЯ — ВЗВЕДЕНИЕ (два ключа, мгновенный старт).

По словам владельца: «ввожу ключ — и робит всё сразу после ввода ВТОРОГО
ключа; и без этих 8 часов — запустил, он сразу начал».

Два ключа = два рубильника:
  · КЛЮЧ 1 — Tinkoff Invest API токен (РУКИ: право покупать/продавать);
  · КЛЮЧ 2 — DeepSeek API ключ (МОЗГ-шифратор) + факт его ввода = согласие GO.

Режимы (безопасная лесенка, но БЕЗ искусственных задержек):
  · dry     — токена нет: расчёты идут, ордеров нет (тест);
  · analyst — есть токен, нет второго ключа/GO: сигналы + ручной курок;
  · live    — оба ключа и GO: автомат работает СРАЗУ (после прогрева индексов
              ~секунды, не часы). Никаких «8 часов» — их и не было.

⚫ live = реальные деньги на автомате: это осознанный выбор владельца, курок и
ответственность за ним. Тормоз капитала — killswitch (−6%/день, серия убытков),
он в live всегда включён. Плечо 18+, рынок не предсказуем.
"""
from __future__ import annotations

MODE_DRY = "dry"
MODE_ANALYST = "analyst"
MODE_LIVE = "live"

# прогрев перед стартом — это СЕКУНДЫ (каталоги инструментов), не часы
WARMUP_NOTE = "прогрев индексов инструментов ~секунды, затем работа сразу"


def resolve_mode(tinkoff_token: str | None, deepseek_key: str | None,
                 go_live: bool) -> dict:
    """Определить режим по двум ключам. live — мгновенно, как только оба ключа
    и GO. go_live — единичное согласие владельца (факт ввода второго ключа в
    интерфейсе может его выставлять)."""
    has_t = bool(tinkoff_token and str(tinkoff_token).strip())
    has_d = bool(deepseek_key and str(deepseek_key).strip())
    if not has_t:
        return {"mode": MODE_DRY, "immediate": True, "trades_real": False,
                "keys": {"tinkoff": has_t, "deepseek": has_d, "go": bool(go_live)},
                "note": "нет Tinkoff-токена — расчёты без ордеров (dry). "
                        "Введи КЛЮЧ 1 (Tinkoff), чтобы дать руки"}
    if not (has_d and go_live):
        need = []
        if not has_d:
            need.append("КЛЮЧ 2 (DeepSeek)")
        if not go_live:
            need.append("GO (согласие на автомат)")
        return {"mode": MODE_ANALYST, "immediate": True, "trades_real": False,
                "keys": {"tinkoff": has_t, "deepseek": has_d, "go": bool(go_live)},
                "note": "есть руки (Tinkoff), автомат не взведён: сигналы + ручной "
                        "курок. Для боевого нужно: " + " и ".join(need)}
    return {"mode": MODE_LIVE, "immediate": True, "trades_real": True,
            "keys": {"tinkoff": has_t, "deepseek": has_d, "go": True},
            "killswitch": "включён (−6%/день, серия убытков)",
            "warmup": WARMUP_NOTE,
            "note": "ОБА КЛЮЧА + GO → автомат работает СРАЗУ (без 8 часов, "
                    "прогрев — секунды). ⚫ реальные деньги, курок был твой"}


# ── self-test ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # нет токена → dry, немедленно, без реальных сделок
    d = resolve_mode(None, "sk-deep", True)
    assert d["mode"] == MODE_DRY and d["trades_real"] is False and d["immediate"]

    # только Tinkoff → analyst (ручной курок)
    a = resolve_mode("t.abc", None, False)
    assert a["mode"] == MODE_ANALYST and a["trades_real"] is False
    assert "КЛЮЧ 2" in a["note"]

    # токен есть, второй ключ есть, но GO нет → всё ещё analyst
    a2 = resolve_mode("t.abc", "sk-deep", False)
    assert a2["mode"] == MODE_ANALYST and "GO" in a2["note"]

    # оба ключа + GO → live СРАЗУ, killswitch включён
    lv = resolve_mode("t.abc", "sk-deep", True)
    assert lv["mode"] == MODE_LIVE and lv["trades_real"] is True and lv["immediate"]
    assert "killswitch" in lv and "СРАЗУ" in lv["note"]

    # никаких 8 часов — прогрев это секунды
    assert "секунды" in WARMUP_NOTE and "часы" not in lv.get("warmup", "")

    print("arming self-test OK: два ключа = два рубильника; dry→analyst→live; "
          "live взводится СРАЗУ после второго ключа+GO; прогрев секунды, не 8 часов; "
          "killswitch в live всегда включён")
