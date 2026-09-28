"""Дни рождения: разбор дат, ближайшие даты, поздравления, ежедневная проверка, инструменты."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from oracle.llm import LLMError
from oracle.tools import base as tb
from oracle.tools import birthdays as bd
from oracle.tools.base import Services, ToolContext

from conftest import FakeLLM, FakeNotifier, with_cfg

TODAY = date(2026, 9, 28)   # clock: понедельник 28.09.2026 09:00 МСК


async def call(ctx, tool_name: str, /, **args) -> dict:
    return json.loads(await tb.dispatch(tool_name, args, ctx))


async def add(ctx, **args) -> dict:
    r = await call(ctx, "add_birthday", **args)
    assert r["ok"], r
    return r


# ── разбор даты ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw, expected", [
    ("14.03", (3, 14, None)),
    ("4.3", (3, 4, None)),
    ("14.03.1990", (3, 14, 1990)),
    ("1990-03-14", (3, 14, 1990)),
    ("1990-03-14T00:00:00", (3, 14, 1990)),
    ("14 марта", (3, 14, None)),
    ("14 марта 1990", (3, 14, 1990)),
    ("14 марта 1990 года", (3, 14, 1990)),
    ("31 декабря 1985 г.", (12, 31, 1985)),
    ("14 Март", (3, 14, None)),
    ("1 мая", (5, 1, None)),
    ("1-го мая", (5, 1, None)),
    ("май 3", (5, 3, None)),
    ("14 авг", (8, 14, None)),
    ("2 сент.", (9, 2, None)),
    ("  3 ОКТЯБРЯ ", (10, 3, None)),
    ("14.03.90", (3, 14, 1990)),
    ("05.01.05", (1, 5, 2005)),
    ("14/03/2001", (3, 14, 2001)),
    ("14 03 1990", (3, 14, 1990)),
    ("14.03 1990", (3, 14, 1990)),
    ("March 14, 1990", (3, 14, 1990)),
    ("29.02", (2, 29, None)),
    ("29.02.2000", (2, 29, 2000)),
    ("28.09.2026", (9, 28, 2026)),            # родился сегодня — можно
])
def test_parse_birth_date_ok(raw, expected):
    assert bd.parse_birth_date(raw, TODAY) == expected


@pytest.mark.parametrize("raw, fragment", [
    ("29.02.1990", "не високосный"),
    ("31.04", "такой даты нет"),
    ("30.02", "такой даты нет"),
    ("13.13", "месяца 13"),
    ("14.03.1899", "1900"),
    ("14.03.2030", "2026"),
    ("29.09.2026", "ещё не наступила"),        # завтрашняя дата в этом году
    ("", "пустая"),
    (None, "пустая"),
    ("завтра", "не понял"),
    ("abc 5", "не понял"),
])
def test_parse_birth_date_errors(raw, fragment):
    with pytest.raises(ValueError) as ei:
        bd.parse_birth_date(raw, TODAY)
    assert fragment in str(ei.value)


def test_human_date():
    assert bd.human_date(3, 14) == "14 марта"
    assert bd.human_date(3, 14, 1990) == "14 марта 1990"


# ── ближайшие даты ───────────────────────────────────────────────────────────
def test_next_birthday_basic():
    assert bd.next_birthday(9, 30, TODAY) == date(2026, 9, 30)
    assert bd.next_birthday(9, 28, TODAY) == TODAY                    # сегодня — это сегодня
    assert bd.next_birthday(9, 27, TODAY) == date(2027, 9, 27)
    assert bd.next_birthday(1, 1, TODAY) == date(2027, 1, 1)


def test_next_birthday_leap_day():
    assert bd.next_birthday(2, 29, TODAY) == date(2027, 2, 28)         # 2027 не високосный
    assert bd.next_birthday(2, 29, date(2027, 2, 28)) == date(2027, 2, 28)
    assert bd.next_birthday(2, 29, date(2027, 3, 1)) == date(2028, 2, 29)
    assert bd.next_birthday(2, 29, date(2028, 1, 10)) == date(2028, 2, 29)


async def test_upcoming_ordering_and_turns(ctx):
    await add(ctx, name="Иван", date="15.10.1980")
    await add(ctx, name="Маша", date="30.09.1996", relation="сестра")
    await add(ctx, name="Петя", date="28.09")
    await add(ctx, name="Оля", date="01.09.2000")
    await add(ctx, name="Лёша", date="29.02.1992")
    rows = await bd.upcoming(ctx.db, ctx.tz, 30)
    assert [r["name"] for r in rows] == ["Петя", "Маша", "Иван"]
    assert [r["days_left"] for r in rows] == [0, 2, 17]
    assert [r["turns"] for r in rows] == [None, 30, 46]
    assert rows[1]["next_date"] == "2026-09-30" and rows[1]["relation"] == "сестра"

    everything = await bd.upcoming(ctx.db, ctx.tz, 366)
    assert [r["name"] for r in everything] == ["Петя", "Маша", "Иван", "Лёша", "Оля"]
    lesha = everything[3]
    assert lesha["next_date"] == "2027-02-28" and lesha["turns"] == 35
    assert everything[-1]["days_left"] == 338 and everything[-1]["turns"] == 27

    assert [r["name"] for r in await bd.upcoming(ctx.db, ctx.tz, 0)] == ["Петя"]


async def test_upcoming_uses_local_date(ctx, clock):
    await add(ctx, name="Петя", date="29.09")
    clock.set(datetime(2026, 9, 28, 21, 30, tzinfo=timezone.utc))   # в Москве уже 29.09 00:30
    rows = await bd.upcoming(ctx.db, ctx.tz, 0)
    assert [r["name"] for r in rows] == ["Петя"]


async def test_upcoming_skips_broken_rows(ctx):
    await ctx.db.execute("INSERT INTO birthdays(name, month, day, created_at) VALUES('Кривой', 2, 31, 'x')")
    await add(ctx, name="Петя", date="28.09")
    assert [r["name"] for r in await bd.upcoming(ctx.db, ctx.tz, 366)] == ["Петя"]


# ── инструменты ──────────────────────────────────────────────────────────────
async def test_add_birthday_tool(ctx):
    r = await add(ctx, name="Маша", date="30 сентября 1996", relation="сестра",
                  notes="обожает горы", tg_username="@masha_k")
    assert r["date"] == "30 сентября 1996" and r["days_left"] == 2 and r["turns"] == 30
    assert r["weekday"] == "среда" and r["updated"] is False
    assert r["note"].startswith("Напиши поздравление")
    assert r["tg_username"] == "@masha_k"
    row = await bd.get_birthday(ctx.db, r["id"])
    assert (row["month"], row["day"], row["year"], row["tg_username"]) == (9, 30, 1996, "masha_k")
    assert row["remind_days_before"] == 1


async def test_add_birthday_duplicate_updates(ctx):
    r1 = await add(ctx, name="Маша", date="30.09", relation="сестра")
    r2 = await add(ctx, name="маша", date="30 сентября 1996", notes="горы")
    assert r2["id"] == r1["id"] and r2["updated"] is True
    rows = await ctx.db.fetchall("SELECT * FROM birthdays")
    assert len(rows) == 1
    assert rows[0]["year"] == 1996 and rows[0]["relation"] == "сестра" and rows[0]["notes"] == "горы"
    # то же имя, другая дата — новая запись, но с предупреждением
    r3 = await add(ctx, name="Маша", date="01.10")
    assert r3["id"] != r1["id"] and "warning" in r3


async def test_add_birthday_bad_input(ctx):
    r = await call(ctx, "add_birthday", name="Маша", date="31.02")
    assert r["ok"] is False and "такой даты нет" in r["error"]
    r = await call(ctx, "add_birthday", name="  ", date="14.03")
    assert r["ok"] is False and "имя" in r["error"]
    r = await call(ctx, "add_birthday", name="Маша", date="14.03", tg_username="+7 999 123")
    assert r["ok"] is False and "username" in r["error"]
    r = await call(ctx, "add_birthday", name="Маша", date="14.03", remind_days_before="много")
    assert r["ok"] is False
    r = await call(ctx, "add_birthday", name="Маша", date="14.03", remind_days_before=500)
    assert r["ok"] and (await bd.get_birthday(ctx.db, r["id"]))["remind_days_before"] == bd.MAX_REMIND_DAYS
    assert await ctx.db.scalar("SELECT COUNT(*) FROM birthdays") == 1


async def test_add_within_window_marks_prenotice(ctx, notifier):
    """Владелец сам только что сказал про ДР послезавтра — «через 2 дн.» ему не шлём."""
    r = await add(ctx, name="Маша", date="30.09", remind_days_before=3)
    row = await bd.get_birthday(ctx.db, r["id"])
    assert row["last_prenotice_year"] == 2026
    assert await bd.birthday_jobs(ctx) == 0 and notifier.sent == []


async def test_list_birthdays_tool(ctx):
    await add(ctx, name="Иван", date="15.10.1980", relation="коллега")
    await add(ctx, name="Петя", date="28.09")
    await add(ctx, name="Оля", date="01.09")
    r = await call(ctx, "list_birthdays")
    assert r["ok"] and r["count"] == 3
    assert [i["name"] for i in r["items"]] == ["Петя", "Иван", "Оля"]
    first = r["items"][0]
    assert set(first) >= {"id", "name", "date", "days_left", "turns", "relation"}
    assert r["items"][1]["turns"] == 46 and r["items"][1]["relation"] == "коллега"
    r = await call(ctx, "list_birthdays", days_ahead=20)
    assert [i["name"] for i in r["items"]] == ["Петя", "Иван"]
    r = await call(ctx, "list_birthdays", days_ahead="7")
    assert [i["name"] for i in r["items"]] == ["Петя"]
    r = await call(ctx, "list_birthdays", days_ahead="неделя")
    assert r["ok"] is False


async def test_list_birthdays_empty(ctx):
    r = await call(ctx, "list_birthdays")
    assert r["ok"] and r["count"] == 0 and "не записано" in r["note"]
    await add(ctx, name="Оля", date="01.09")
    r = await call(ctx, "list_birthdays", days_ahead=10)
    assert r["count"] == 0 and "всего записано: 1" in r["note"]


async def test_update_birthday(ctx):
    r = await add(ctx, name="Маша", date="30.09.1996", relation="сестра", tg_username="masha_k")
    bid = r["id"]
    await ctx.db.execute("UPDATE birthdays SET last_greeted_year=2026, last_prenotice_year=2026 WHERE id=?",
                         (bid,))
    u = await call(ctx, "update_birthday", id=bid, date="05.10", relation="", tg_username="",
                   notes="любит кофе")
    assert u["ok"] and u["date"] == "5 октября 1996" and u["relation"] == "" and "tg_username" not in u
    row = await bd.get_birthday(ctx.db, bid)
    assert (row["month"], row["day"], row["year"]) == (10, 5, 1996)
    assert row["last_greeted_year"] is None and row["last_prenotice_year"] is None
    assert row["notes"] == "любит кофе" and row["tg_username"] == ""
    u = await call(ctx, "update_birthday", id=str(bid), name="Мария")
    assert u["ok"] and u["name"] == "Мария"
    assert (await call(ctx, "update_birthday", id=bid))["ok"] is False             # нечего менять
    assert "нет дня рождения" in (await call(ctx, "update_birthday", id=999, name="x"))["error"]
    assert (await call(ctx, "update_birthday", id="abc", name="x"))["ok"] is False


async def test_delete_birthday(ctx):
    r = await add(ctx, name="Маша", date="30.09")
    d = await call(ctx, "delete_birthday", id=r["id"])
    assert d == {"ok": True, "deleted": r["id"], "name": "Маша"}
    assert await ctx.db.scalar("SELECT COUNT(*) FROM birthdays") == 0
    d = await call(ctx, "delete_birthday", id=r["id"])
    assert d["ok"] is False and "нет дня рождения" in d["error"]


# ── поздравление ─────────────────────────────────────────────────────────────
def capture(reply: str, box: list):
    def fn(messages, kw):
        box.append({"system": messages[0]["content"], "user": messages[1]["content"], **kw})
        return reply
    return fn



async def test_generate_greeting_prompt(ctx):
    r = await add(ctx, name="Маша", date="30.09.1996", relation="сестра", notes="обожает горы, была на Эльбрусе")
    now = ctx.now_utc().isoformat()
    await ctx.db.execute("INSERT INTO facts(content, category, created_at, updated_at) VALUES(?,?,?,?)",
                         ("Маша переехала в Казань", "person", now, now))
    await ctx.db.execute("INSERT INTO facts(content, category, created_at, updated_at) VALUES(?,?,?,?)",
                         ("любит чёрный кофе", "preference", now, now))
    box: list = []
    ctx.llm = FakeLLM([capture('«Маш, с днюхой! Горы подождут 🏔️🎉»', box)])
    b = (await bd.upcoming(ctx.db, ctx.tz, 5))[0]
    text = await bd.generate_greeting(ctx, b, style="покороче")
    assert text == "Маш, с днюхой! Горы подождут 🏔️"       # кавычки сняты, лишний эмодзи убран
    call_ = box[0]
    assert call_["temperature"] == 1.1 and call_["deep"] is False
    user, system = call_["user"], call_["system"]
    assert "Маша" in user and "сестра" in user and "Эльбрусе" in user and "30" in user
    assert "Казань" in user and "кофе" not in user            # факт про неё — да, чужой — нет
    assert "покороче" in user
    for cliche in ("счастья, здоровья, успехов", "пусть сбудутся все мечты",
                   "желаю всего самого наилучшего", "море позитива"):
        assert cliche in system
    assert "ОТ ИМЕНИ" in system and "эмодзи" in system.lower()
    assert r["id"] == b["id"]


async def test_generate_greeting_without_turns_key_and_owner_name(ctx):
    ctx.cfg = with_cfg(ctx.cfg, owner_name="Андрей")
    box: list = []
    ctx.llm = FakeLLM([capture("Петя, с днём рождения!", box)])
    row = await bd.get_birthday(ctx.db, (await add(ctx, name="Петя", date="28.09.1990"))["id"])
    assert await bd.generate_greeting(ctx, row) == "Петя, с днём рождения!"
    assert "Андрей" in box[0]["system"] and "Исполняется: 36" in box[0]["user"]
    assert "Кем приходится: не указано" in box[0]["user"]


async def test_generate_greeting_empty_is_error(ctx):
    ctx.llm = FakeLLM(['""'])
    b = await bd.get_birthday(ctx.db, (await add(ctx, name="Петя", date="28.09"))["id"])
    with pytest.raises(ValueError):
        await bd.generate_greeting(ctx, b)


def test_clean_greeting():
    assert bd.clean_greeting("Вот поздравление:\n\n\"Оля, с днём рождения!\"") == "Оля, с днём рождения!"
    assert bd.clean_greeting("  «Текст»  ") == "Текст"
    assert bd.clean_greeting("Раз 🎉 два 🎂 три 🥳!") == "Раз 🎉 два три!"
    assert bd.clean_greeting("А\n\n\n\nБ") == "А\n\nБ"
    assert bd.clean_greeting("") == ""


def test_fallback_greeting_by_relation():
    mom = bd.fallback_greeting({"name": "Мама", "relation": "мама"})
    colleague = bd.fallback_greeting({"name": "Игорь", "relation": "коллега"})
    friend = bd.fallback_greeting({"name": "Петя", "relation": "друг"})
    assert mom.startswith("Мама, с днём рождения!") and "Люблю" in mom
    assert "Работать" in colleague and "Обнимаю" not in colleague
    assert "Рад" in friend
    for t in (mom, colleague, friend):
        low = t.lower()
        assert all(c not in low for c in bd.BANNED_CLICHES)


async def test_birthday_greeting_tool(ctx):
    r = await add(ctx, name="Маша", date="30.09", tg_username="masha_k")
    box: list = []
    ctx.llm = FakeLLM([capture("Первый вариант", box), capture("Второй вариант", box)])
    g = await call(ctx, "birthday_greeting", id=r["id"])
    assert g == {"ok": True, "id": r["id"], "name": "Маша", "greeting": "Первый вариант"}
    assert await ctx.db.kv_get(f"bday_greeting:{r['id']}") == "Первый вариант"
    assert ctx.outbox == []                                  # userbot выключен — без кнопки «отправить»
    g = await call(ctx, "birthday_greeting", id=r["id"], style="смешнее")
    assert g["greeting"] == "Второй вариант"
    assert "Первый вариант" in box[1]["user"] and "смешнее" in box[1]["user"]   # просим не повторяться
    assert await ctx.db.kv_get(f"bday_greeting:{r['id']}") == "Второй вариант"


async def test_birthday_greeting_tool_userbot_button(ctx):
    ctx.cfg = with_cfg(ctx.cfg, userbot_enabled=True)
    ctx.services.userbot = SimpleNamespace(ready=True)
    ctx.llm = FakeLLM(["Текст"])
    r = await add(ctx, name="Маша", date="30.09", tg_username="masha_k")
    await call(ctx, "birthday_greeting", id=r["id"])
    assert len(ctx.outbox) == 1
    item = ctx.outbox[0]
    assert item.kind == "text" and "@masha_k" in item.text
    assert item.buttons == [[("📨 Отправить", f"bday:send:{r['id']}")]]


async def test_birthday_greeting_tool_errors(ctx):
    def boom(messages, kw):
        raise LLMError("сервер модели сбоит (503)")
    ctx.llm = FakeLLM([boom])
    r = await add(ctx, name="Маша", date="30.09")
    g = await call(ctx, "birthday_greeting", id=r["id"])
    assert g["ok"] is False and "напиши поздравление сам" in g["error"]
    g = await call(ctx, "birthday_greeting", id=777)
    assert g["ok"] is False and "нет дня рождения" in g["error"]


async def test_regenerate_greeting_helper(ctx):
    r = await add(ctx, name="Маша", date="30.09")
    await ctx.db.kv_set(f"bday_greeting:{r['id']}", "старый")
    box: list = []
    ctx.llm = FakeLLM([capture("новый", box)])
    b, text = await bd.regenerate_greeting(ctx, r["id"])
    assert b["name"] == "Маша" and text == "новый" and "старый" in box[0]["user"]


# ── ежедневная проверка ──────────────────────────────────────────────────────
async def insert(ctx, name, month, day, year=None, **kw) -> int:
    cols = {"name": name, "month": month, "day": day, "year": year, "created_at": "2026-01-01T00:00:00+00:00",
            **kw}
    return await ctx.db.execute(
        f"INSERT INTO birthdays({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})", tuple(cols.values()))


async def test_birthday_jobs_prenotice_and_greeting(ctx, notifier):
    tomorrow = await insert(ctx, "Петя", 9, 29, 1990, relation="друг")
    today = await insert(ctx, "Маша", 9, 28, 1996, relation="сестра", notes="любит горы")
    far = await insert(ctx, "Иван", 10, 15, 1980)
    ctx.llm = FakeLLM(["«Маш, с днём рождения! Горы ждут.»"])
    assert await bd.birthday_jobs(ctx) == 2
    greet, pre = notifier.sent[0], notifier.sent[1]              # ближайшие первыми: сегодня, потом завтра
    assert pre["text"].startswith("🎁 Завтра день рождения: Петя (друг) — исполнится 36")
    assert pre["buttons"] is None
    assert greet["text"].startswith("🎂 Сегодня день рождения: Маша (сестра) — исполняется 30")
    assert "Вот поздравление, можно переслать:\n\nМаш, с днём рождения! Горы ждут." in greet["text"]
    assert greet["buttons"] == [[("🔁 Другой вариант", f"bday:regen:{today}")]]
    assert await ctx.db.kv_get(f"bday_greeting:{today}") == "Маш, с днём рождения! Горы ждут."
    rows = {r["id"]: r for r in await ctx.db.fetchall("SELECT * FROM birthdays")}
    assert rows[tomorrow]["last_prenotice_year"] == 2026 and rows[tomorrow]["last_greeted_year"] is None
    assert rows[today]["last_greeted_year"] == 2026
    assert rows[far]["last_prenotice_year"] is None
    events = [m for m in await ctx.db.recent_messages(10) if m["role"] == "event"]
    assert events and "Сегодня ДР у Маша" in events[-1]["content"] and events[-1]["via"] == "system"
    # второй прогон в тот же день — тишина
    assert await bd.birthday_jobs(ctx) == 0 and len(notifier.sent) == 2


async def test_birthday_jobs_prenotice_once_per_year(ctx, notifier, clock):
    bid = await insert(ctx, "Иван", 10, 1, 1980, remind_days_before=3)   # 01.10 — через 3 дня
    assert await bd.birthday_jobs(ctx) == 1
    assert notifier.sent[0]["text"].startswith("🎁 Через 3 дн. день рождения: Иван — исполнится 46")
    assert "четверг, 1 октября" in notifier.sent[0]["text"]
    clock.advance(days=1)
    assert await bd.birthday_jobs(ctx) == 0                    # 2 дня — уже предупреждал в этом году
    clock.advance(days=2)                                      # сам день
    ctx.llm = FakeLLM(["Ваня, с днём рождения!"])
    assert await bd.birthday_jobs(ctx) == 1
    assert notifier.sent[-1]["text"].startswith("🎂 Сегодня")
    # через год — снова предупреждение и поздравление
    clock.set(datetime(2027, 9, 28, 6, 0, tzinfo=timezone.utc))
    assert await bd.birthday_jobs(ctx) == 1
    assert "исполнится 47" in notifier.sent[-1]["text"]
    row = await bd.get_birthday(ctx.db, bid)
    assert row["last_prenotice_year"] == 2027 and row["last_greeted_year"] == 2026


async def test_birthday_jobs_catch_up_missed_prenotice(ctx, notifier):
    """Бот лежал в день предупреждения — предупредит позже, но один раз."""
    await insert(ctx, "Иван", 9, 30, remind_days_before=5)
    assert await bd.birthday_jobs(ctx) == 1
    assert notifier.sent[0]["text"].startswith("🎁 Через 2 дн. день рождения: Иван.")
    assert await bd.birthday_jobs(ctx) == 0


async def test_birthday_jobs_zero_remind_days(ctx, notifier):
    await insert(ctx, "Иван", 9, 29, remind_days_before=0)
    assert await bd.birthday_jobs(ctx) == 0 and notifier.sent == []


async def test_birthday_jobs_userbot_send_button(ctx, notifier):
    bid = await insert(ctx, "Маша", 9, 28, tg_username="masha_k")
    other = await insert(ctx, "Петя", 9, 28)
    ctx.cfg = with_cfg(ctx.cfg, userbot_enabled=True)
    ctx.services.userbot = SimpleNamespace(ready=True)
    ctx.llm = FakeLLM(["Маша, поздравляю!", "Петя, поздравляю!"])
    assert await bd.birthday_jobs(ctx) == 2
    by_text = {s["text"].split("\n")[0]: s for s in notifier.sent}
    masha = next(s for k, s in by_text.items() if "Маша" in k)
    petya = next(s for k, s in by_text.items() if "Петя" in k)
    assert masha["buttons"] == [[("🔁 Другой вариант", f"bday:regen:{bid}")],
                                [("📨 Отправить @masha_k", f"bday:send:{bid}")]]
    assert petya["buttons"] == [[("🔁 Другой вариант", f"bday:regen:{other}")]]


async def test_birthday_jobs_userbot_not_ready(ctx, notifier):
    await insert(ctx, "Маша", 9, 28, tg_username="masha_k")
    ctx.cfg = with_cfg(ctx.cfg, userbot_enabled=True)
    ctx.services.userbot = SimpleNamespace(ready=False)
    ctx.llm = FakeLLM(["Текст"])
    await bd.birthday_jobs(ctx)
    assert len(notifier.sent[0]["buttons"]) == 1


async def test_birthday_jobs_userbot_disabled_in_cfg(ctx, notifier):
    await insert(ctx, "Маша", 9, 28, tg_username="masha_k")
    ctx.services.userbot = SimpleNamespace(ready=True)          # объект есть, но USERBOT_ENABLED выключен
    ctx.llm = FakeLLM(["Текст"])
    await bd.birthday_jobs(ctx)
    assert len(notifier.sent[0]["buttons"]) == 1


async def test_birthday_jobs_llm_error_fallback(ctx, notifier):
    def boom(messages, kw):
        raise LLMError("модель не ответила вовремя (таймаут)")
    bid = await insert(ctx, "Мама", 9, 28, relation="мама")
    ctx.llm = FakeLLM([boom])
    assert await bd.birthday_jobs(ctx) == 1
    text = notifier.sent[0]["text"]
    assert "Мама, с днём рождения!" in text and "заготовка" in text
    assert (await ctx.db.kv_get(f"bday_greeting:{bid}")).startswith("Мама, с днём рождения!")
    assert (await bd.get_birthday(ctx.db, bid))["last_greeted_year"] == 2026


async def test_birthday_jobs_without_notifier(cfg, db, clock):
    c = ToolContext(cfg=cfg, db=db, llm=FakeLLM(["x"]), services=Services(notifier=None))
    bid = await c.db.execute("INSERT INTO birthdays(name, month, day, created_at) VALUES('Маша', 9, 28, 'x')")
    await c.db.execute("INSERT INTO birthdays(name, month, day, created_at) VALUES('Петя', 9, 29, 'x')")
    assert await bd.birthday_jobs(c) == 0
    rows = await c.db.fetchall("SELECT * FROM birthdays")
    assert all(r["last_greeted_year"] is None and r["last_prenotice_year"] is None for r in rows)
    assert await c.db.kv_get(f"bday_greeting:{bid}") is None
    assert c.llm.calls == []


async def test_birthday_jobs_empty_db(ctx, notifier):
    assert await bd.birthday_jobs(ctx) == 0


async def test_birthday_jobs_send_failure_does_not_mark(ctx):
    class Broken(FakeNotifier):
        async def send(self, text, buttons=None, *, silent=False):
            raise RuntimeError("telegram down")
    ctx.services.notifier = Broken()
    bid = await insert(ctx, "Маша", 9, 28)
    ctx.llm = FakeLLM(["Текст"])
    assert await bd.birthday_jobs(ctx) == 0
    assert (await bd.get_birthday(ctx.db, bid))["last_greeted_year"] is None


def test_tools_registered():
    for name in ("add_birthday", "list_birthdays", "update_birthday", "delete_birthday", "birthday_greeting"):
        t = tb.REGISTRY[name]
        assert t.description and t.parameters["type"] == "object"
