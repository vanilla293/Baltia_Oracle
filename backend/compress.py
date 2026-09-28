# -*- coding: utf-8 -*-
"""ПИФИЯ v5 — сжатие контекста без потери нитей.

v5.1: DeepSeek V4 держит окно 1 048 576 токенов, но уже на половине окна ответ
хуже, чем на чистом листе. Поэтому входы ИИ не режутся ножницами, а сжимаются
FLASH «без потери ни одной нити» — и только когда они выросли выше разумного:
  · `shrink(text, limit)` — один блок выше PYTHIA_CTX_LIMIT (200 000 симв.) FLASH
    ужимает; огромный текст идёт ПО ЧАСТЯМ (map: каждый кусок ≤ CHUNK сжимается к
    своей доле лимита, reduce: склейка, при нужде второй проход) — так лимит
    держится и на 500 000 символов, и ни один кусок не пропадает;
  · `fit(blocks, total)` — набор блоков одного промпта: пока сумма выше
    PYTHIA_PROMPT_SOFT (600 000 симв.), самый большой блок ужимается FLASH; ниже
    предела ничего не трогается — каждый ИИ видит своё целиком;
  · `cap(text)` — жёсткий потолок PYTHIA_PROMPT_CAP голова+хвост, только авария;
  · провал ИИ → честная обрезка головы+хвоста (`clip`), никогда не молча.
"""
from __future__ import annotations

import asyncio
import logging

from . import ai_v5, config
from .prompts import _clip as clip  # noqa: F401  (голова+хвост)

log = logging.getLogger("pythia.compress")

CHUNK = 45_000          # кусок для FLASH при сжатии по частям (симв.)
SINGLE_MAX = 90_000     # до этого объёма блок ужимается одним вызовом FLASH
MIN_TARGET = 1_500      # ниже этого блок FLASH не ужимаем — режем честно
FIT_FLOOR = 0.35        # fit не ужимает блок ниже 35% его длины за один проход

_SYS = ("Ты сжимаешь текст для передачи другому аналитику. Верни ТОЛЬКО сжатый "
        "текст, без вступлений. Правила: не потерять ни одной нити — все числа, "
        "уровни, тикеры, времена, тезисы и выводы остаются; убрать воду, повторы "
        "и общие слова; исправить орфографию; порядок мыслей сохранить. "
        "Короткие id в квадратных скобках в начале строк ([a1b2c3]) и заголовки блоков "
        "сохраняй дословно — по ним другой код ищет новости. "
        "Уложись в лимит символов, который дан.")


def ctx_limit() -> int:
    return int(getattr(config, "PYTHIA_CTX_LIMIT", 60_000))


def prompt_soft() -> int:
    return int(getattr(config, "PYTHIA_PROMPT_SOFT", 300_000))


def prompt_cap() -> int:
    return int(getattr(config, "PYTHIA_PROMPT_CAP", 450_000))


async def _flash_shrink(t: str, limit: int, label: str, part: str = "") -> str | None:
    """Один вызов FLASH: сжать t до limit. Возвращает ответ ИИ, даже если он длиннее
    лимита (следующий проход дожмёт); None — только если ИИ недоступен или ответ пуст."""
    tag = f" [{label}]" if label else ""
    try:
        out = await ai_v5.flash_text(
            _SYS, f"ЛИМИТ: {limit} символов (сейчас {len(t)}).\n"
                  f"{('ЧТО ЭТО: ' + label + (' — ' + part if part else '')) if label else ''}\n\nТЕКСТ:\n{t}",
            route="shrink")
    except Exception as e:   # noqa: BLE001
        log.info("shrink%s: ИИ недоступен (%s)", tag, str(e)[:80])
        return None
    out = (out or "").strip()
    if not out:
        return None
    if len(out) >= len(t):
        log.info("shrink%s: ИИ не сжал (%d → %d)", tag, len(t), len(out))
        return None
    if len(out) > limit:
        log.info("shrink%s: ИИ не уложился (%d > %d) — дожму следующим проходом", tag, len(out), limit)
    return out


def _split(t: str, size: int) -> list[str]:
    """Режем по границам абзацев/строк, куски ≈ size."""
    parts: list[str] = []
    cur = ""
    for para in t.split("\n"):
        if len(cur) + len(para) + 1 > size and cur:
            parts.append(cur)
            cur = para
        else:
            cur = f"{cur}\n{para}" if cur else para
        while len(cur) > size:                      # одна гигантская строка — режем по длине
            parts.append(cur[:size])
            cur = cur[size:]
    if cur:
        parts.append(cur)
    return parts


async def shrink(text: str, limit: int | None = None, label: str = "", passes: int = 3) -> str:
    """≤limit → как есть (limit по умолчанию = PYTHIA_CTX_LIMIT). Выше — FLASH сжимает без
    потери нитей: огромный текст по частям (каждый кусок ≤ CHUNK к своей доле лимита),
    склейка и следующий проход, пока не уложится (до passes). Провал куска → кусок как
    есть (ничего не пропадает); ИИ так и не уложился / недоступен → clip с пометкой."""
    limit = int(limit or ctx_limit())
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    if limit < MIN_TARGET:
        return clip(t, limit, label)
    tag = f" [{label}]" if label else ""
    cur = t
    for p in range(max(1, passes)):
        if len(cur) <= SINGLE_MAX:
            out = await _flash_shrink(cur, limit, label)
            if out is None:
                break                                # ИИ недоступен/не сжал — честные ножницы ниже
            cur = out
        else:
            parts = _split(cur, CHUNK)
            n = len(parts)
            share = max(MIN_TARGET, int(limit * 0.92 / n))
            async def _part(i: int, part: str):
                if len(part) <= share:               # кусок уже в своей доле — не трогаем и не зовём ИИ
                    return part
                return await _flash_shrink(part, share, label, f"часть {i + 1} из {n}")
            outs = await asyncio.gather(*(_part(i, part) for i, part in enumerate(parts)))
            if all(o is None for o in outs):
                break
            cur = "\n".join(o if o is not None else part for part, o in zip(parts, outs))
        if len(cur) <= limit:
            return cur
        log.info("shrink%s: после прохода %d ещё %d > %d", tag, p + 1, len(cur), limit)
    log.warning("shrink%s: ИИ не уложился в %d (осталось %d) — голова+хвост", tag, limit, len(cur))
    return clip(cur, limit, label)


async def fit(blocks: dict[str, str], total: int | None = None, per_block: int | None = None) -> dict[str, str]:
    """Блоки одного промпта {имя: текст}. Сначала каждый блок выше per_block
    (= PYTHIA_CTX_LIMIT) ужимается shrink; потом, пока сумма выше total
    (= PYTHIA_PROMPT_SOFT), самый большой блок ужимается ещё (не ниже FIT_FLOOR его
    длины за проход). Возвращает новый словарь; ничего ниже пределов не трогается."""
    total = int(total or prompt_soft())
    per_block = int(per_block or ctx_limit())
    out = {k: (v or "") for k, v in (blocks or {}).items()}
    big = [k for k, v in out.items() if len(v) > per_block]
    if big:
        res = await asyncio.gather(*(shrink(out[k], per_block, k) for k in big))
        for k, r in zip(big, res):
            out[k] = r
    for _ in range(12):
        size = sum(len(v) for v in out.values())
        if size <= total:
            break
        k = max(out, key=lambda x: len(out[x]))
        cur = len(out[k])
        target = max(MIN_TARGET, min(int(cur * (1 - FIT_FLOOR)), cur - (size - total)))
        if cur <= MIN_TARGET or target >= cur:
            log.warning("fit: не могу ужать дальше (%d > %d), самый большой блок %s = %d", size, total, k, cur)
            break
        log.info("fit: сумма %d > %d — ужимаю %s %d → %d", size, total, k, cur, target)
        out[k] = await shrink(out[k], target, k)
    return out


def cap(text: str, label: str = "промпт") -> str:
    """Жёсткий потолок user-текста одного вызова. Ниже потолка — как есть."""
    t = text or ""
    lim = prompt_cap()
    if len(t) <= lim:
        return t
    log.warning("cap: %s %d символов > потолка %d — голова+хвост", label, len(t), lim)
    return clip(t, lim, label)


if __name__ == "__main__":
    calls: list[tuple[int, int]] = []

    async def fake_flash(system, user, **kw):        # сжимает ровно к лимиту: первые N символов текста
        lim = int(user.split("ЛИМИТ: ")[1].split(" ")[0])
        body = user.split("\n\nТЕКСТ:\n", 1)[1]
        calls.append((len(body), lim))
        return body[:lim]

    ai_v5.flash_text = fake_flash

    async def main():
        s = "x" * 100
        assert await shrink(s, 200) == s
        config.PYTHIA_CTX_LIMIT = 60_000
        big = "y" * 5000
        assert await shrink(big) == big, "ниже PYTHIA_CTX_LIMIT ничего не сжимается"
        # один блок выше лимита → один вызов FLASH, без «обрезано»
        calls.clear()
        r = await shrink("z" * 70_000, 60_000, "тест")
        assert len(r) == 60_000 and "обрезано" not in r and len(calls) == 1 and calls[0] == (70_000, 60_000)
        # огромный текст → по частям: 500 000 симв. ≈ 12 кусков ≤ 45 000, каждый к своей доле
        calls.clear()
        text = "\n".join("абзац %d " % i + "к" * 990 for i in range(500))    # ≈ 500 000 симв.
        r = await shrink(text, 60_000, "простыня")
        assert len(r) <= 60_000 and "обрезано" not in r, len(r)
        n_parts = len(calls)
        assert 10 <= n_parts <= 14 and all(l <= 45_000 for l, _ in calls), (n_parts, calls[:3])
        assert all(t <= 60_000 * 0.92 / n_parts + 1 for _, t in calls)
        assert r.startswith("абзац 0"), "первый кусок сохранён"
        # fit: ниже пределов — ничего не трогает
        config.PYTHIA_PROMPT_SOFT = 300_000
        blocks = {"а": "а" * 1000, "б": "б" * 2000}
        assert await fit(blocks) == blocks
        # fit: один блок выше per_block → ужат; сумма выше total → самый большой ужат ещё
        calls.clear()
        blocks = {"досье": "д" * 150_000, "новости": "н" * 100_000, "небо": "с" * 3000}
        r = await fit(blocks, total=120_000, per_block=60_000)
        assert len(r["досье"]) <= 60_000 and len(r["новости"]) <= 60_000 and r["небо"] == "с" * 3000
        assert sum(len(v) for v in r.values()) <= 120_000, sum(len(v) for v in r.values())
        assert all("обрезано" not in v for v in r.values())
        # ИИ не уложился в долю куска (как живой FLASH: отдаёт ~70% куска) → второй проход, без ножниц
        calls.clear()

        async def loose(system, user, **kw):
            lim = int(user.split("ЛИМИТ: ")[1].split(" ")[0])
            body = user.split("\n\nТЕКСТ:\n", 1)[1]
            calls.append((len(body), lim))
            return body[:max(lim, int(len(body) * 0.7))]     # не меньше 70% входа
        ai_v5.flash_text = loose
        text2 = "\n".join("строка %d " % i + "м" * 200 for i in range(500))    # ≈ 105 000 симв. (как лента из 500 новостей)
        r = await shrink(text2, 60_000, "новости")
        assert len(r) <= 60_000 and "обрезано" not in r, (len(r), r[-120:])
        assert len(calls) >= 3 and r.startswith("строка 0"), calls   # 2 куска + второй проход; малый кусок без вызова
        # ИИ вообще не сжимает → после проходов честный clip с пометкой
        async def same(system, user, **kw):
            return user.split("\n\nТЕКСТ:\n", 1)[1]
        ai_v5.flash_text = same
        r = await shrink("q" * 70_000, 60_000, "тест")
        assert "обрезано" in r and len(r) <= 60_000 + 200
        ai_v5.flash_text = fake_flash
        # провал ИИ → честный clip с пометкой
        async def bad(system, user, **kw):
            raise RuntimeError("сеть")
        ai_v5.flash_text = bad
        r = await shrink("w" * 70_000, 60_000, "тест")
        assert len(r) <= 60_000 + 200 and "обрезано" in r
        c = clip("a" * 1000, 100)
        assert len(c) < 400 and "обрезано" in c
        assert cap("z" * 100) == "z" * 100
        config.PYTHIA_PROMPT_CAP = 20_000
        assert "обрезано" in cap("w" * 30_000)

    asyncio.run(main())
    print("compress self-test OK: shrink по частям, fit блоков, clip при провале")
