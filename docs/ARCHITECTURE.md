# Архитектура Baltia Oracle

Личный ИИ-помощник в Telegram. Один владелец (`OWNER_ID`), DeepSeek через OpenAI-совместимый API,
SQLite, голос (Whisper), напоминания, календарь, дни рождения, идеи, проекты, новости, память,
собственные позиции бота, дневник и инициатива. Опционально — userbot (Telethon) для чтения своих
чатов и черновиков ответов.

Код и докстринги — по-русски, в стиле ядра (`oracle/db.py`, `oracle/llm.py`, `oracle/tools/base.py`).
Python ≥ 3.10, всё асинхронное. Никаких глобальных синглтонов, кроме реестра инструментов.

## Поток сообщения

```
Telegram (aiogram 3) ── голос ──► STT (Groq / OpenAI / локальный faster-whisper) ──► текст
        │                                                                              │
        └── текст ─────────────────────────────────────────────────────────────────────┤
                                                                                       ▼
                                Agent.handle(text) ── persona.build_system(память, позиции, дневник, конспект, будильники)
                                        │  история (последние HISTORY_MESSAGES реплик)
                                        ▼
                                LLM.complete(tools=…)  ◄──► tools.dispatch(...)  (до LLM_MAX_STEPS шагов)
                                        │
                                        ▼
                     ответ текстом (+ голосом через edge-tts) + вложения из ctx.outbox (файлы, кнопки)

Scheduler (тик 15 с): напоминания, «долбилки», отложенные, follow-up'ы бота,
утренняя сводка, дни рождения, дайджест новостей, ночная рефлексия, сворачивание старого диалога.
```

## Ядро (готово, менять только осознанно)

| Файл | Что даёт |
|---|---|
| `oracle/config.py` | `Settings` (frozen dataclass) и `load()`; `cfg.tz` → `ZoneInfo` (кривой TIMEZONE → UTC, `cfg.tz_ok` = False; принимает и `MSK`, `UTC+3`); `cfg.problems()` (включает `cfg.warnings` — что в .env не понято); `cfg.turn_budget(deep)` — секунд на весь ход агента (`LLM_TURN_BUDGET` / `LLM_DEEP_TURN_BUDGET`); `cfg.owner_gender` (m/f) |
| `oracle/timeutil.py` | `now_utc()`, `now_local(tz)`, `iso(dt)`, `from_iso(s)`, `naive_str(dt)`, `parse_local(s, tz)`, `normalize_rrule(r)`, `next_occurrence(rrule, local_start, tz, after=None, inclusive=False)`, `fmt_local(dt_utc, tz)`, `fmt_now_for_prompt(tz)`, `describe_rrule(r)`, `weekday_name(d)`, `month_gen(m)`, `set_clock(fn)` (тесты) |
| `oracle/db.py` | `DB`: `execute` (INSERT → lastrowid, иначе rowcount), `fetchall`, `fetchone`, `scalar`, `transaction()`, `kv_get/kv_set/kv_delete` (JSON), `index_put(kind, ref_id, body)`, `index_delete`, `search(kind, query, limit) -> [ids]`, `add_message(role, content, via)`, `recent_messages(limit)`, `backup(dest) -> int` (VACUUM INTO, файл 0600); `words(s)` / `search_stems(word)` — разбор текста для поиска; вся схема таблиц; миграции — по `PRAGMA user_version` (`SCHEMA_VERSION` = 2); после аварийной остановки (непустой `-wal`) `open()` делает `PRAGMA quick_check` и при порче падает понятной ошибкой (сам `-wal` не удаляет) |
| `oracle/llm.py` | `LLM.complete(messages, tools=, deep=, json_mode=, max_tokens=, temperature=, timeout=, tool_choice=, model=, allow_empty=False) -> LLMResponse`; `ask(system, user, deep=, json_mode=)`; `ask_json(...)`; `LLMError(message, *, status, fatal, kind)` (kind: model / context / filtered / timeout / ""); `error_kind(status, body)`; `human_error(status, body, exc, *, local=)`; `LLMResponse.to_message()` (с `reasoning_content` для tool_calls); `extract_json(text)` |
| `oracle/tools/base.py` | `@tool(name, description, properties, required, enabled=, keep="head"\|"tail")` (keep — какой конец длинного списка в результате сохранить; tail — история чата); `fit_list(items, budget, keep)`; `REGISTRY`; `schemas(cfg)`; `dispatch(name, args_json, ctx) -> str` (сетевым `NET_TOOLS` — срок `NET_TOOL_DEADLINE` = 90 с); `ToolContext(cfg, db, llm, services, outbox)`; `Services(notifier, scheduler, userbot, news, agent, tts).spawn(coro)`; `OutItem(kind, text, data, filename, buttons)`; `Notifier` (протокол); `Buttons = list[list[tuple[str, str]]]` |
| `oracle/persona.py` | `PERSONA`, `build_system(name=, owner_name=, now=, facts=, opinions=, journal=, summary=, nags=, deep=, extra=, owner_gender=)` |
| `tests/conftest.py` | фикстуры `clock` (понедельник 28.09.2026 09:00 МСК), `cfg`, `db`, `fake_llm` (`FakeLLM(script)`), `notifier` (`FakeNotifier`), `ctx` |

Правила времени: в базе — UTC ISO (`timeutil.iso`). Модель передаёт локальное `"YYYY-MM-DD HH:MM"`,
инструмент разбирает `timeutil.parse_local(s, ctx.tz)`. Повторы: `local_start` (наивное локальное) + `rrule`
+ `tz`, следующее срабатывание — `timeutil.next_occurrence`. Показ времени — `timeutil.fmt_local`.

Правила инструментов:
- обработчик `async def name(ctx: ToolContext, *, arg1, arg2=None) -> dict`; успех — `{"ok": True, ...}`;
  ожидаемая ошибка данных — `raise ValueError("человеческий текст")` (dispatch отдаст модели `{"ok": false, "error": ...}`);
- id сущностей в ответах модели — всегда числом, чтобы она могла на них сослаться;
- описания инструментов — по-русски, коротко, с форматом аргументов («время: YYYY-MM-DD HH:MM, локальное»);
- всё, что надо показать владельцу помимо ответа модели (файл, кнопки), — `ctx.outbox.append(OutItem(...))`;
- фоновая долгая работа — `ctx.services.spawn(coro)`, итог — `ctx.services.notifier.send(...)`;
- инструменты модуля регистрируются при импорте модуля; `oracle/tools/__init__.py: load_all()` импортирует все.

## Формат callback_data кнопок (≤ 64 байт, разбирает bot/handlers.py)

| callback | что делает | кто строит |
|---|---|---|
| `rem:done:<id>` | отметить напоминание; у будильника с задачкой, пока он долбит или отложен «💤», — сначала задачка | scheduler |
| `rem:snz:<id>:<min>` | отложить на min минут | scheduler |
| `rem:del:<id>` | отменить напоминание; звонящий будильник с задачкой не отменяется — вместо этого задачка (в /reminders у него и кнопки нет) | /reminders |
| `wake:ans:<id>:<n>` | ответ на задачку будильника: kv `challenge:<id>` = `{"q","a","o"}` (голое число от старых версий тоже принимается); нерешённая задачка на повторный «Встал» присылается та же, после неверного ответа — новая | bot |
| `bday:regen:<id>` | другой вариант поздравления | birthdays / scheduler |
| `bday:send:<id>[:<n>]` | отправить поздравление через userbot: текст варианта n — kv `bday_greeting:<id>:<n>`; старая кнопка без n — только если последний текст (`bday_greeting:<id>`) в том же сообщении; раз в год на ДР (`bday_sent:<id>:<year>`); получатель — строго по @username, без угадывания; после отправки «Поздравить…» снимается | birthdays |
| `idea:deep:<id>` | додумать идею глубоко (в фоне) | ideas / bot |
| `draft:new:<chat_id>` | предложить ответ в чат | userbot-уведомление |
| `draft:send:<id>` / `draft:regen:<id>` / `draft:drop:<id>` | черновик ответа | tg_chats |
| `fact:del:<id>` | забыть факт | /memory |

## Модули

### tools/reminders.py — напоминания
Публичные функции (их зовут scheduler, bot, другие инструменты):
```python
async def create_reminder(ctx, *, text: str, when: str | datetime, rrule: str | None = None,
                          kind: str = "reminder", nag: bool | None = None, challenge: bool | None = None,
                          nag_interval_min: int | None = None, ref_type: str | None = None,
                          ref_id: int | None = None) -> dict   # строка reminders + "when_local", "repeat"
async def get_reminder(db, rid: int) -> dict | None
async def list_active(db, limit: int = 50) -> list[dict]      # status='active', по next_at (NULL в конце), вкл. долбящие
async def cancel_reminder(db, rid: int) -> bool
async def ack_reminder(db, rid: int) -> dict | None           # стоп долбёжки; нет будущих срабатываний → status='done'
async def snooze_reminder(db, rid: int, minutes: int) -> dict | None   # nag_active=0, snooze_at=now+min
async def cancel_by_ref(db, ref_type: str, ref_id: int) -> int
def render_reminder(row: dict, tz) -> str                     # "#12 · пн 29.09 07:30 · каждый день · Подъём 🔁долбит"
```
`kind`: reminder | wake | event | task | followup. `wake` ⇒ `nag=1`, `challenge=cfg.wake_challenge` по умолчанию,
`nag_interval_min` = cfg.nag_interval_min, `nag_max` = cfg.nag_max. Однократное в прошлом — ValueError
(«это время уже прошло: …»). `next_at` = `next_occurrence(rrule, local_start, tz, inclusive=True)`.
`create_reminder` принимает ещё `tz=` (пояс серии) и `nag_max=`. `ringing_challenge(row)` — будильник с задачкой
звонит (долбит или отложен «💤»): такой трогается только задачкой.
Инструменты: `create_reminder(text, when, repeat?, nag?, kind? enum[reminder,wake])` (с первым будильником —
`note` про «Не беспокоить»), `list_reminders()`, `update_reminder(id, text?, when?, repeat?, nag?, only_next?)`
(`only_next` — пропустить или перенести одно ближайшее срабатывание повторяющегося, серия остаётся),
`cancel_reminder(id)` (вся серия), `ack_reminder(id)`, `snooze_reminder(id, minutes?)` (голосом — как кнопка «💤»:
только то, что звонит, отложено или сработало не больше 2 ч назад; сработавшее разовое тоже можно, отменённое — нет;
будильник с задачкой — не дальше `WAKE_SNOOZE_MAX` = 30 мин; повторяющееся — не дальше следующего срабатывания,
это уже пропуск через `only_next`). `only_next` у разового без `when` и `update_reminder` без изменений — ошибка,
а не молчаливый ok. cancel / ack / перенос звонящего будильника с задачкой — отказ («пусть решит задачку кнопкой»).

### tools/calendar.py — календарь
```python
async def add_event(ctx, *, title, start, end=None, all_day=False, location="", notes="", repeat=None,
                    remind_before_min: int | None = 30) -> dict
async def events_between(db, tz, start_utc: datetime, end_utc: datetime) -> list[dict]
    # раскрывает повторы; у каждого элемента "occurs_at" (UTC ISO) и "occurs_local" (строка)
def build_ics(events: list[dict], tz_name: str) -> bytes      # VCALENDAR, повторы — RRULE с TZID
```
Напоминание о событии — строка reminders `kind='event', ref_type='event', ref_id`, `local_start = start - remind_before`,
тот же rrule; для all_day — в 09:00 дня события (минус remind_before, если он ≥ 1440 — за сутки и т.п.).
Отмена события → `cancel_by_ref`. Инструменты: `add_event`, `list_events(from?, to?)` (по умолчанию 7 дней),
`update_event(id, …)`, `cancel_event(id)`, `export_calendar(days_ahead=90)` → `OutItem(kind="file", filename="calendar.ics")`,
`get_agenda(date? | from?, to?)` — повестка одним вызовом (события, напоминания и будильники с раскрытыми
повторами — каждое срабатывание в окне, не раньше `next_at`; ДР, задачи со сроком — напоминания о задачах и событиях
отдельно не дублируются; через `brief.gather`), не больше 31 дня. `update_event` после уже сработавшего напоминания
«в момент начала» не ставит, если начало не сдвинули.

### tools/birthdays.py — дни рождения
```python
def next_birthday(month: int, day: int, today: date) -> date   # 29.02 в невисокосный → 28.02
async def upcoming(db, tz, days: int = 30) -> list[dict]       # + "next_date", "days_left", "turns" (или None)
async def generate_greeting(ctx, bday: dict, style: str = "", *, avoid: str = "") -> str   # LLM, живое, без штампов
async def regenerate_greeting(ctx, bid, style: str = "") -> tuple[dict, str]   # новый вариант; dict с "greeting_variant"
async def store_greeting(db, bid, text) -> int                  # вариант → kv, → его номер n
def variant_key(bid, n) -> str                                  # "bday_greeting:<id>:<n>"
async def birthday_jobs(ctx, *, failures: list[int] | None = None) -> int
    # ежедневно (cfg.birthday_time): предупреждение за remind_days_before, в сам день — поздравление
    # с кнопками и «Поздравить…»; отметки last_*_year — только после отправки; не ушло — id в failures
```
В день ДР: `notifier.send("🎂 Сегодня день рождения: …\n\n<поздравление>", buttons=[[("🔁 Другой вариант", "bday:regen:<id>")], (+ [("📨 Отправить @user", "bday:send:<id>:<n>")] если userbot готов и есть tg_username)])`
и напоминание «Поздравить с днём рождения: …» (`ref_type='birthday'`, долбит раз в час с 9:00 до 22:00 — последний
нажим и «сдаюсь» тоже не позже `NAG_UNTIL`; после «💤» — снова только до 22:00; проверка после 20:00 — одно
напоминание без долбёжки), которое снимается «Готово» или отправкой поздравления кнопкой (отправил накануне или до
утренней проверки — утром ни второго поздравления, ни «Поздравить…»). Не поздравили в сам день (бот лежал) — назавтра, с пометкой «вчера»
(`GRACE_DAYS` = 1). Каждый вариант текста — kv `bday_greeting:<id>:<n>` (счётчик `bday_gen:<id>`, хранятся
последние 20), последний — ещё и `bday_greeting:<id>`; `bday_greeting_for:<id>` — к какому ДР (дата) он написан:
повтор после сбоя его переиспользует. Инструменты: `add_birthday(name, date "DD.MM" | "DD.MM.YYYY" | "YYYY-MM-DD" | «14 марта», relation?, notes?, tg_username?, remind_days_before?)`
(тот же человек с той же датой — обновление; `note` говорит модели, писать ли поздравление сейчас — только если ДР
сегодня/завтра, был вчера (запоздалое) или он просит), `list_birthdays(days_ahead?)`, `update_birthday(id, …)`, `delete_birthday(id)`,
`birthday_greeting(id, style?)` (при userbot — ещё сообщение «текст + 📨 Отправить» в outbox).

### tools/ideas.py — идеи
Инструменты: `save_idea(title, content, evaluation, score, tags?)` (описание требует: сначала честная оценка),
`find_ideas(query, limit=8)` (поиск `db.search("idea", …)`, добивка свежими, если мало), `get_idea(id)`,
`update_idea(id, title?, content?, status?, tags?, note?)` (note дописывается в content с датой),
`list_ideas(status?, limit=15)`, `delete_idea(id)`, `deep_think_idea(id)` — фон: `deep_evaluate`.
```python
async def deep_evaluate(ctx, idea_id: int) -> str   # LLM deep=True: разбор + оценка; пишет deep_evaluation, score;
                                                    # шлёт владельцу «🧠 Додумал идею #id …»; статус thinking → прежний
async def recover_interrupted(db) -> list[dict]     # при старте (app.py): разборы, прерванные перезапуском, —
                                                    # вернуть прежний статус; → [{id, title, status}]
```
Индекс: `db.index_put("idea", id, title + content + tags + evaluation)`.

### tools/projects.py — проекты и задачи
Инструменты: `create_project(name, description?, goal?)`, `list_projects(status?)`, `get_project(project)` (id или имя) → проект + задачи,
`update_project(project, …, status?)`, `add_task(text, project?, due?, priority?, remind?)` (due+remind → reminders kind=task),
`update_task(id, status?, text?, due?, priority?)`, `list_tasks(project?, status?, due_within_days?)`.
```python
async def resolve_project(db, ref: str | int) -> dict | None
async def due_tasks(db, tz, days: int = 1) -> list[dict]     # просроченные + со сроком в ближайшие days
```

### tools/memory.py — память, позиции, дневник, follow-up
Инструменты: `remember(content, category?)`, `recall(query, limit?)` (факты, позиции и `conversation` — конспекты
старых разговоров), `forget(fact_id)`, `list_facts(category?)`,
`set_opinion(topic, stance, reasons, confidence, opinion_id?, why_changed?)`, `get_opinions(query?)`,
`schedule_followup(when, about)` (→ reminders kind=followup).
```python
async def add_fact(db, content: str, category: str = "general", source: str = "chat") -> tuple[int, bool]  # (id, создан ли); дубли не плодит
async def add_fact_ex(db, content, category="general", source="chat") -> dict   # {"id", "created", "replaced"}
async def match_opinion(db, topic, opinion_id=None) -> dict | None   # та же позиция (по id или теме)
async def similar_opinions(db, topic, *, exclude_id=None, limit=3) -> list[dict]
async def facts_for_prompt(db, query: str = "", limit: int = 40) -> list[dict]
async def upsert_opinion(db, *, topic, stance, reasons="", confidence=60, opinion_id=None, why_changed="") -> dict
async def opinions_for_prompt(db, query: str = "", limit: int = 8) -> list[dict]
async def add_journal(db, content: str) -> int
async def latest_journal(db) -> dict | None
```

### services/news.py + tools/news.py — новости, веб, погода
```python
class NewsService:
    def __init__(self, cfg, db, http: httpx.AsyncClient | None = None)
    async def refresh(self, force: bool = False) -> int           # RSS параллельно, не чаще раза в 15 мин
    async def latest(self, hours: int = 24, query: str = "", limit: int = 40) -> list[dict]
    async def web_search(self, query: str, max_results: int = 8, news: bool = False) -> list[dict]  # ddgs, в потоке
    async def read_url(self, url: str, max_chars: int = 12000) -> dict   # {"url","title","text"}
    async def weather(self, city: str = "", days: int = 1) -> dict | None   # Open-Meteo, без ключа
    async def aclose(self)
async def news_digest(ctx, topic: str = "", *, deep: bool = True) -> str   # tools/news.py: дайджест «правды»
                                                       # deep=True — рассылка в фоне; /news — deep по /mode
async def weather_line(ctx) -> str | None              # одна строка погоды для утренней сводки
```
Инструменты: `get_news(topic?, hours?, limit?)`, `web_search(query)`, `read_url(url)`, `get_weather(city?, days?)`.
Сервис берётся из `ctx.services.news` (если None — создаётся на лету).

Сроки: таймаут httpx — на одно чтение, поэтому у каждого сетевого шага есть общий срок: лента — 20 с
(`FEED_DEADLINE`, тело ≤ 5 МБ), страница со всеми редиректами — 30 с (`READ_DEADLINE`), погода — 20 с на запрос.
Не уложились — лента пропускается, погода `None`, `read_url` — ValueError («грузится слишком долго»).

SSRF: `read_url` ходит через отдельный клиент (`_page_client`): имя резолвится в сетевом слое httpcore
(`_GuardedBackend`), внутренние адреса (включая NAT64 `64:ff9b::/96` с внутренним IPv4 внутри) отсекаются,
соединение идёт ровно на проверенный IP — DNS rebinding не проходит. Прокси из окружения этот клиент
не берёт. `_check_url` остаётся предпроверкой с понятными текстами ошибок.

### services/scheduler.py + services/brief.py
```python
class Scheduler:
    def __init__(self, ctx: ToolContext, tick_seconds: float = 15.0)
    def start(self) / async def stop(self)
    async def tick(self) -> None       # один проход (тесты гоняют его с подменёнными часами)
async def morning_brief(ctx, *, mode="morning") -> str   # brief.py: события дня, напоминания, ДР (3 дня), задачи,
                                       # погода, повестка из дневника; mode="now" — на остаток дня, без «доброго утра»
async def today_brief(ctx) -> str      # /today: morning_brief(mode="now")
async def gather(ctx, start=None, end=None, *, extras=None) -> dict   # данные повестки за период (и для get_agenda)
```
Срабатывание: сообщение + кнопки `rem:done/rem:snz`; запись в диалог `db.add_message("event", "Сработало напоминание #id: …", "system")`;
повтор → следующий `next_at`, иначе `next_at=NULL` (+ `status='done'`, если не долбит); nag → `nag_active=1`,
`nag_next_at`; эскалация фразами `NAG_LINES`; после `nag_max` — «сдаюсь». followup → `ctx.services.agent.proactive(...)`.
Ежедневные задачи — по `cfg.*_time`, раз в локальные сутки, каждая своей задачей. kv `job:<имя>` ставится только
после успешной доставки; не вышло — повтор по `JOB_RETRY` (5, 10, 20, 30 мин) внутри окна догона `CATCH_UP` = 3 ч
(дни рождения догоняются весь день). Сетевые ошибки Telegram (TelegramNetworkError / ServerError / RetryAfter)
повторяются без счёта; постоянные — после ~30 мин (`SEND_GIVE_UP`) сдаёмся: запись в диалог, а в kv `undelivered` —
что не дошло, об этом скажем при первой удачной отправке. Состояние напоминаний обновляется условно (UPDATE … WHERE
по прежнему состоянию): кнопка владельца между чтением и записью не затирается. Сводка и дайджест пишутся в разговор
событием целиком (до 5000 знаков), чтобы «подробнее про пятый сюжет» было понятно; ссылки из лент дайджеста
запоминаются как доверенные (kv `guard_urls`) — read_url их откроет. Длинный текст не дошёл целиком — повтор шлёт
с первого недошедшего куска (`BotNotifier.send(progress=…)`), дошедшие не задваиваются. Отложенное «💤», проспанное
вместе с ботом (или без связи), — одно сообщение «пропустил…» без новой долбёжки, как и в `fire()`; «сдаюсь» после
обрыва связи говорит «не было связи», а не «был выключен».

### services/stt.py, services/tts.py — голос
```python
class STTError(Exception)
class STT:
    def __init__(self, cfg, http: httpx.AsyncClient | None = None)
    provider: str   # groq | openai | local | off (auto выбирает)
    def available(self) -> bool
    async def transcribe(self, data: bytes, filename: str = "voice.ogg", mime: str = "audio/ogg") -> str
class TTS:
    def __init__(self, cfg)
    def available(self) -> bool
    async def synth(self, text: str) -> bytes     # mp3 (edge-tts)
def clean_for_speech(text: str) -> str
```

### services/userbot.py, tools/tg_chats.py, userbot_login.py — свои чаты (опционально)
```python
class Userbot:
    def __init__(self, cfg, db, notifier=None)
    ready: bool
    async def start(self) / async def stop(self)
    async def dialogs(self, limit: int = 20, unread_only: bool = False) -> list[dict]
    async def resolve(self, query: str | int) -> tuple[int, str]    # ValueError, если не нашёл/неоднозначно
    async def history(self, chat: int, limit: int = 30) -> list[dict]   # {"out","sender","text","date"}
    async def style_samples(self, chat: int | None = None, limit: int = 25) -> list[str]
    async def send(self, chat: int, text: str) -> bool
async def make_draft(ctx, chat: str | int, instruction: str = "") -> dict   # tools/tg_chats.py
```
Инструменты (только при `USERBOT_ENABLED` и `ready`): `tg_list_chats`, `tg_read_chat(chat, limit?)` (`keep="tail"`:
не влезло — выкидываются самые старые сообщения), `tg_draft_reply(chat, instruction?)`.
**Отправка — только кнопкой владельца** (`draft:send:<id>`). Инструмента «отправить» у модели нет.
Уведомление о новом личном сообщении (USERBOT_NOTIFY) пишется в разговор событием «Владельцу пришло личное
сообщение от «X» (chat_id N); что там — tg_read_chat…» — без самого текста: написать может кто угодно, а история
идёт в ход владельца без пометки «чужой текст»; прочитать — `tg_read_chat` (чужой текст, ход «заражён»). Кнопка —
`draft:new:<chat_id>`. Telethon потерял связь насовсем (`client.disconnected`) — `ready=False`, «network: …»
в /status, `app.keep_userbot` подключает заново.

### tools/settings.py — настройки голосом
`set_voice_replies(mode: off|mirror|always)` (kv `tts_mode`, как /voice; `note`, если озвучка недоступна),
`set_thinking_mode(mode: fast|deep)` (kv `mode`, как /mode, со следующего сообщения),
`reset_conversation()` (как /reset: реплики помечаются свёрнутыми; память и позиции остаются).
Все три — только по прямой просьбе (в «запачканном» чужим текстом ходе — `GATED_TOOLS`; там же удаление, отмена,
`update_*` включая `update_task`/`update_project`, `ack_reminder`, запись в память и follow-up'ы). read_url открывает
только ссылки владельца (в т.ч. «lenta.ru» без схемы) и пришедшие из поиска, новостей, страниц и его чатов — в этом
ходе или раньше (kv `guard_urls`); ссылки из хранилищ (задачи, идеи…) и эхо ссылок, которые модель сама вписала
(целиком или обрезанные), — нет.

### agent.py — мозг
```python
@dataclass
class AgentReply:
    text: str
    outbox: list[OutItem]
    deep: bool = False
    error: str | None = None
class Agent:
    def __init__(self, ctx: ToolContext)
    busy: bool                                            # идёт ход (владельца или инициатива бота)
    async def handle(self, text: str, *, via: str = "text", deep: bool | None = None,
                     received: datetime | None = None, queue_notice: bool = True) -> AgentReply
        # весь ход — не дольше cfg.turn_budget(deep); модель молчит > 20 с в быстром режиме — через notifier
        # «⏳ Отвечаю дольше обычного…». received — время прихода (от TurnOrder): отстояло > 60 с — модели
        # «ОЧЕРЕДЬ: пришло в HH:MM…», «через N минут» считается от него. Очередь сообщений владельца — в
        # TurnOrder (там и «⏳ Ещё дорешиваю прошлое сообщение…»); здесь — только если ход держит proactive
    async def proactive(self, trigger: str) -> str        # бот пишет первым (follow-up, событие); текст пишется
                                                          # в диалог до того, как scheduler его отправит
    async def summarize_old(self) -> bool                 # свернуть старые реплики в конспект
    async def reflect(self) -> str | None                 # ночная рефлексия: дневник, факты, позиции, follow-up'ы
    async def reset_context(self) -> int                  # /reset
```
Текст владельца приходит к агенту с пометками (их ставит bot/handlers.py): пересланное —
«[Переслано от <кто>, <когда>. Это чужие слова, не владельца: …]» (своё старое — «[Владелец переслал своё же старое
сообщение …]»), ответ (reply) — «[Ответ на твоё сообщение: «…»]», «[Ответ на сообщение: «…»]» или «[Ответ на
уведомление о личном сообщении (chat_id N; черновик ответа — tg_draft_reply): «…»]», скрытые ссылки —
«[ссылки в сообщении: …]». PERSONA и REFLECT_SYSTEM велят не приписывать пересланное владельцу.

### bot/ — Telegram (aiogram 3)
`bot/render.py` (`md_to_html`, `split_message`), `bot/keyboards.py` (`build(buttons)`), `bot/notifier.py`
(`BotNotifier(bot, chat_id)`), `bot/handlers.py` (команды, текст, голос, кнопки),
`oracle/app.py` (`main()` — сборка всего), `oracle/__main__.py`.

`bot/middleware.py`: `OwnerOnly` пропускает владельца только в личке (в группе бот молчит, кнопкам — «Нет доступа»);
`TurnOrder` — реплики доходят до агента в порядке прихода (голосовое не обгоняется текстом, отправленным следом;
команды без агента свой билет отпускают, /reset — ждёт очереди); билет помнит время прихода (`turn_arrived`, его
получает `Agent.handle(received=…)`), ждёт дольше 3 с — «печатает…» и «⏳ Ещё дорешиваю прошлое сообщение — это в
очереди…»; ответ ушёл текстом — очередь свободна (`finish_turn`), озвучка следующих не держит. Команды не
срабатывают из пересланного (и из подписи к пересланному фото). `Inflight` — обновления в работе, их дожидается
остановка (не успели — «повтори сообщение», не дольше 5 с на отправку).
Отредактированное сообщение заново не выполняется — бот отвечает, что правку не применяет.

Запуск (`app.main`): конфиг (предупреждения из `cfg.warnings`, кривой TIMEZONE — в лог) → база (не открылась —
понятный текст и код 1) → инструменты → `ideas.recover_interrupted` → «кто я» у Telegram с повторами (нет сети —
ждём) → агент и планировщик → меню команд → polling. Userbot подключается в фоне с повторами. Остановка:
ответы в работе (до 12 с) → планировщик → userbot → фоновые задачи → клиенты → база.

Команды: `/start /help /today /reminders /calendar /ics /birthdays /ideas /idea N /projects /news [тема]
/memory /forget N /opinions /journal /mode /deep <вопрос> /voice /reset /backup /chats /status`.
