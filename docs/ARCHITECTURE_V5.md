# ПИФИЯ v5 «СОВЕТ» — контракт архитектуры

Этот документ — единый контракт для всех, кто пишет код v5 (бэкенд-новости,
бэкенд-миссия, фронт). Всё, что здесь названо, обязано существовать ровно с
такими именами, сигнатурами и формами JSON. Что не названо — на усмотрение.

## 0. Идея в одном абзаце

Две модели DeepSeek: **FLASH** (`config.DEEPSEEK_MODEL_FAST`, дёшево, для
механики: отбор, разметка, группировка, сжатие, раскладка) и **PRO**
(`config.DEEPSEEK_MODEL`, думает, для совета: анализ → критика → вердикт → итог).
**Каждый вызов ИИ — новый чат** (system + user, без истории). Контекст передаётся
явно и компактно; большие тексты сжимает `compress.shrink` без потери нитей.
Промпты — **свободные, точные, короткие** (см. §7).

Этапы:
1. **Новости → Совет** (`newsflow` + `council`): RSS за 3 дня → FLASH отбирает
   биржевое → FLASH размечает каждую новость в новом чате (тон, честность,
   раздутость, важность…) → FLASH группирует в потоки/теги → PRO совет
   (анализ, критика, вердикт, итог-JSON со списком «куда входить сегодня») →
   FLASH раскладывает выбор по тикерам каталога и прикрепляет новости.
2. **Взгляд человека** (`council.human`): текст человека → FLASH сжимает и
   сортирует (ничего не теряя, правит орфографию) → PRO совет заново, но на
   вход даётся ИТОГ первого этапа (не вся простыня) + сжатый взгляд →
   итог-JSON v2 + FLASH «рамка» для человека (к чему пришли и почему).
3. **Дозор** (`watch`): фоном каждые N минут — свежие новости → FLASH отбор →
   FLASH разметка с нуля → FLASH «меняет ли это что-то радикально» → заметка;
   серьёзная (severity ≥ порога) → полный пересмотр миссии.
4. **Миссия** (`mission`): человек выбирает тикер и способ игры (лонг / шорт /
   смешанно / пусть решает ИИ) → PRO совет по инструменту (досье биржи + итог
   совета + прикреплённые новости + астро + Курамото/бифуркации) → exec-приказ
   (вход сейчас / на откате к уровню / на пробитии уровня, тейк, стоп) → исполняет
   `ai_pilot.AIPilot` (наследник `MissionPilot`): вход на максимум с плечом, трос;
   дежурный PRO раз в 30 мин думает и решает (ждать / войти / закрыть / звать
   совет), внепланово — по резкому ходу цены или серьёзной новости; полный
   совет зовёт сам PRO (НОВЫЙ_АНАЛИЗ). Человек видит график, вход/выход, P/L,
   журнал сделок, может остановить/продолжить/паника/пересмотр.

## 1. Общие модули (уже написаны — использовать, не переписывать)

### `backend/ai_v5.py`
```python
FLASH: str; PRO: str                       # имена моделей (из config)
def key() -> str                           # ключ DeepSeek: пул → любой ключ инструмента → RuntimeError
def has_key() -> bool
def now_msk() -> datetime                  # aware, UTC+3
def now_msk_str() -> str                   # "18.09.2026 14:35 МСК, четверг"
async def flash_json(system, user, *, think=True, route="flash", max_tokens=None) -> Any
async def flash_text(system, user, *, think=True, route="flash", max_tokens=None) -> str
async def pro_json(system, user, *, route="pro", max_tokens=None) -> Any
async def pro_text(system, user, *, route="pro", max_tokens=None) -> str
async def pro_stream(system, user, *, on_think=None, on_text=None, route="pro") -> str
   # on_think(delta) / on_text(delta) — async колбэки на дельты
def usage() -> dict                        # {"prompt","completion","total","calls"}
SEM_FLASH: asyncio.Semaphore               # параллелизм flash (config.PYTHIA_FLASH_PAR)
```
`*_json` возвращают распарсенный объект (json_mode + `ai._extract_json`).
Ошибки ключа/сети — исключения `Exception` с человеческим текстом через
`ai.humanize_error(e)`; стадии обязаны ловить и слать `bus.stage(... "error")`.

### `backend/bus.py` — события и реестр живых прогонов
```python
def set_sink(coro_fn)                                   # сервер: HUB.broadcast
async def emit(ev: dict)
def start_run(scope: str, meta: dict | None = None) -> str   # run_id
async def stage(scope, run_id, stage, status, **kw)     # {"type":"v5", ...}
async def text(scope, run_id, stage, delta, kind="text") # {"type": kind("text"|"think"), ...}
def end_run(run_id, status="done", error: str | None = None)
def run(run_id) -> dict | None                          # снимок: {run_id,scope,meta,stage,status,started,ended,error,texts:{stage: str},thinks:{stage:str},counters:{...}}
def running() -> dict                                   # run_id -> краткий снимок без текстов
def active(scope) -> dict | None                        # живой прогон этого scope (или None)
```
Форма события стадии (все поля кроме type/scope/run_id/stage/status — опциональны):
```json
{"type":"v5","scope":"daily|human|watch|mission","run_id":"…","stage":"collect|triage|characterize|group|snapshot|analysis|critique|verdict|summary|distribute|compress|present|impact|dossier|exec|pilot|review",
 "status":"start|progress|done|error","detail":"строка для человека","n":12,"total":40,"ticker":"SBER","data":{}}
```
Событие стрима текста: `{"type":"text"|"think","scope","run_id","stage","delta":"…"}`.
`bus.text` сам копит полный текст в `run(run_id)["texts"|"thinks"][stage]` — фронт,
открытый посередине, забирает снимок `GET /api/v5/run?run_id=`.

### `backend/store_v5.py` — SQLite `data/pythia_v5.db` (переживает перезапуск)
Старт (`_init`, ревью 24.09.2026 + доработка): ошибка открытия базы НЕ стирает файлы. Настоящее повреждение файла
(«file is not a database», «malformed») — база и уцелевшие хвосты -wal/-shm/-journal (негодные SQLite убирает сам при
открытии) переименовываются в `.broken-<время>` (`_set_aside`, хвосты первыми) и начинается новая, приложение поднимается; блокировка другим экземпляром, полный диск, права, сбой
миграции — `RuntimeError` с понятным текстом, файлы на месте. `news_upsert_raw_ids` — одна атомарная вставка
`ON CONFLICT(id) DO NOTHING` (дубль не сбрасывает разметку и `relevant`). То же правило старта — в `memory.py` (архив 4.x).
```python
# новости
def news_upsert_raw(items: list[dict]) -> int          # {id?,source,title,summary,link,published,ts}; id = sha1(link or title); возврат: новых
def news_untriaged(days: float) -> list[dict]
def news_set_relevant(ids: list[str], flag: int) -> None
def news_relevant_uncharacterized(days: float) -> list[dict]
def news_ai_put(id: str, data: dict) -> None
def news_ai_get(ids: list[str]) -> dict[str, dict]
def news_window(days: float, relevant_only=True, with_ai=True) -> list[dict]   # raw ∪ ai, ts desc
def news_by_ids(ids: list[str]) -> list[dict]
def news_since(ts: float, relevant_only=True) -> list[dict]
def news_stats(days: float) -> dict                    # {"raw","relevant","characterized","untriaged"}
# советы
def council_put(run_id, kind, data: dict, status="done") -> None
def council_latest(kind: str | None = None) -> dict | None   # {"run_id","kind","ts","status","data"}
def council_list(limit=20) -> list[dict]               # без data-тела (кратко)
# дозор
def watch_add(data: dict) -> int
def watch_list(limit=30, unseen_only=False) -> list[dict]  # {"id","ts","seen","data"}
def watch_mark_seen(ids: list[int]) -> None
def watch_unseen_count() -> int
# миссии и сделки
def mission_put(ticker, data: dict); def mission_get(ticker) -> dict | None; def mission_all() -> dict; def mission_del(ticker)
def news_upsert_raw_ids(items: list[dict]) -> list[str]   # id ДОБАВЛЕННЫХ строк (news_upsert_raw = len(...))
def trade_add(rec: dict) -> int   # {"ticker","side","lots","entry","exit_px","pnl","opened_ts","closed_ts","why","mode",
                                  #  + фаза 4 W1: "figi","lot","point_value","fee_est","source"} (pnl — по цене тика, оценка)
def trade_update(tid, fields: dict) -> bool         # фаза 4 W1: сверка ledger — entry_real/exit_real/fee/pnl_gross/pnl_net/source/synced_ts/ops
def trades(ticker: str | None = None, limit=200) -> list[dict]   # + "figi","entry_real","exit_real","fee","fee_est","pnl_gross","pnl_net",
                                                                  #   "net" (нетто: pnl_net или pnl − fee_est), "source" est|broker|partial|mock, "est" bool, "synced_ts"
def trades_full(ticker=None, since_ts=None, limit=1000) -> list[dict]   # + "data" (json) и "ops" (id операций), по возрастанию closed_ts
def trades_between(from_ts, to_ts, ticker=None) -> list[dict]           # закрытые в окне (итоги дня)
def trades_summary(ticker: str | None = None) -> dict   # {"count","pnl" (= net),"gross","fee","net","wins","losses" (по нетто),
                                                        #  "est": bool (есть неподтверждённые),"confirmed": n,"by_day":[{"day" МСК,"count","net","gross","fee"}]}
# операции брокера (фаза 4 W1): таблица ops (id PK, ts, kind buy|sell|fee|margin|varmargin|other, figi, uid, qty, price, payment, fee, raw json)
def ops_upsert(items: list[dict]) -> int            # идемпотентно (id PK; вид уже лежащей обновляется)
def ops_list(keys=None, from_ts=None, to_ts=None, kinds=None) -> list[dict]   # keys — figi ИЛИ uid
def ops_stats(from_ts=None, to_ts=None) -> dict     # {"count","by_kind","fee" (комиссии сделок без дублей),"margin","last_ts"}
def kv_get(key, default=None); def kv_set(key, value)
```
Запись новости после слияния (`news_window`): все сырые поля + `"ai": {...}`
(разметка, см. §2.3) + `"relevant": 0|1`.

### `backend/compress.py`
```python
def ctx_limit() -> int; def prompt_soft() -> int; def prompt_cap() -> int   # config.PYTHIA_CTX_LIMIT / _PROMPT_SOFT / _PROMPT_CAP
async def shrink(text: str, limit: int | None = None, label: str = "", passes: int = 2) -> str
   # limit=None → PYTHIA_CTX_LIMIT (60 000; 5.4.1 вернул пределы 5.1–5.3.2). ≤limit → как есть; выше — FLASH сжимает «без
   # потери ни одной нити»; текст больше SINGLE_MAX (90 000) идёт ПО ЧАСТЯМ (куски ≤ CHUNK
   # 45 000, каждый к своей доле лимита, склейка, при нужде второй проход); провал ИИ → clip.
async def fit(blocks: dict[str, str], total: int | None = None, per_block: int | None = None) -> dict
   # блоки одного промпта: каждый выше per_block (= CTX_LIMIT) ужимается shrink; пока сумма
   # выше total (= PROMPT_SOFT 300 000) — самый большой блок ужимается ещё; ниже пределов
   # ничего не трогается. Зовётся ДО сборки промпта (совет, миссия, перепроверка).
def clip(text: str, limit: int, label: str = "") -> str   # голова+хвост (prompts._clip), только при провале ИИ
def cap(text: str, label: str = "промпт") -> str           # жёсткий потолок PYTHIA_PROMPT_CAP (авария)
```
v5.1: окно DeepSeek V4 — 1M токенов, но на половине окна ответ хуже, чем на чистом
листе. Поэтому входы ИИ не режутся ножницами, а сжимаются FLASH выше разумных
пределов: блок 60 000 симв. (≈ 20–25 тыс. токенов), весь промпт 300 000 симв. (≈ 100–120 тыс. токенов),
жёсткий потолок 450 000 (5.4.1: пределы ×3 из 5.3.3 давали промпты по 400–600 тыс. симв., модель тонула в слоях —
возвращены значения 5.1–5.3.2). Ниже пределов кресла, новости, досье, астро идут целиком.
Стадии в шине несут размеры промптов («аналитик: промпт 84 512 симв. — …»).

### Конфиг (`config._refresh`, читаются как `config.X`)
`PYTHIA_V5_DAYS=3`, `PYTHIA_WATCH_SEC=900`, `PYTHIA_WATCH_SERIOUS=75`,
`PYTHIA_FLASH_PAR=6`, `PYTHIA_CTX_LIMIT=60000`, `PYTHIA_PROMPT_SOFT=300000`,
`PYTHIA_PROMPT_CAP=450000`, `PYTHIA_REVIEW_SEC=1800`, `PYTHIA_GNEWS=1` (Google News
по инструментам), `PYTHIA_SCAN_MIN=480` (сканер стакана в миссии, минут, 5…480).
5.4.1 «трезвый пилот»: `PYTHIA_EXCHANGE_STOP=0` (стопы только в программе — стоп-заявок на бирже нет, трос виртуальный;
1 — трос на бирже, как в 5.4.0), `PYTHIA_MONEY_MODEL=pro` (pro|flash — модель узлов у денег: трос, тейк, триаж, проверка
входа, мысль о прибыли; `ai_v5.money_model()`/`money_json(system, user, route=…)`), `PYTHIA_ENTRY_CHECK=1` и
`PYTHIA_ENTRY_CHECK_COOL_SEC=300` (проверка входа у двери), `PYTHIA_PROFIT_THINK=1`, `PYTHIA_PROFIT_THINK_PCT=60`,
`PYTHIA_PROFIT_THINK_MIN_PCT=1.0`, `PYTHIA_PROFIT_THINK_COOL_SEC=900` (мысль о прибыли). Читать как `config.X` /
`ai_pilot._setting("X", default)` — живые после `config.set_many` (панель «Ключи» → `POST /api/v5/keys`).

## 2. Этап 1 — `backend/newsflow.py` (сборщик A)

```python
async def collect(days: float, force=True) -> int                # RSS → store; возврат: новых
def catalog_targets() -> list[tuple[str, str, str]]              # (code, name, asset_class) по instruments.CATALOG
async def collect_targeted(targets, days=3) -> list[str]         # Google News по инструментам (news.fetch_for_ticker, параллельно,
                                                                 # TARGET_TIMEOUT=40 с на цель, дедуп по ссылке) → store; id НОВЫХ
async def enrich_ticker(run_id, ticker, name, asset_class, days=3) -> dict   # миссия: целевые новости по тикеру → отбор и
                                                                 # разметка ТОЛЬКО новых; стадия «news»; {"new","relevant","characterized"}
async def triage(run_id, days, items=None) -> int                # FLASH пачками по 50 (остаток — последняя неровная): {"keep":[ids]}; возврат: отмечено relevant
async def characterize_all(run_id, days, items=None) -> int      # FLASH по одной в новом чате, параллельно SEM_FLASH; идемпотентно
async def group(run_id, days) -> dict                            # FLASH(think=True): потоки/теги; см. §2.4
def news_for_ticker(ticker: str, name: str, days=3, limit=40) -> list[dict]   # по assets/tags/sectors + карточки последнего совета
def fresh_since(ts: float) -> list[dict]                         # свежие релевантные размеченные
def render(items: list[dict], limit=None, rich=False) -> str     # строки для промптов; limit=None — ВСЕ (для ИИ), число — панель/логи;
                                                                 # rich=True — one_line_rich: у важных (importance ≥ 60 или effect ≥ 50) вторая строка «факты: …· суть: …»
def one_line_rich(item: dict) -> str
def one_line(item: dict) -> str
async def distribute(run_id, summary: dict, days) -> list[dict]  # FLASH: выбор совета → карточки тикеров §2.6
```

### 2.3 Разметка одной новости (`news_ai_put` json) — СТРОГО эти ключи
```json
{"gist":"2-3 предложения сути, только факты","one_liner":"≤120 символов, суть с главным числом",
 "key_facts":["≤4 коротких факта с числами, датами, именами"],
 "tone":-100..100,"honesty":0..100,"hype":0..100,"importance":0..100,"novelty":0..100,
 "kind":"факт|мнение|слух|прогноз|реклама","horizon":"часы|дни|недели|месяцы",
 "expected_effect":"вверх|вниз|нейтрально|неясно","effect_strength":0..100,
 "manipulation_risk":0..100,"actors":["ЦБ","Сбер"],"assets":["SBER","нефть","рубль","индекс"],
 "sectors":["банки"],"tags":["ставка","дивиденды"],"why_market":"1 фраза: при чём тут биржа"}
```
Числа — целые. Строка `render()` одной новости:
`[a1b2c3] 18.09 14:20 · rbc · <one_liner> · тон +40 · честн 70 · разд 30 · важн 65 · нов 50 · эффект вверх/40 · факт · часы · SBER,банки`
(id — первые 6 символов sha1; при выдаче в промпт используем короткий id, в JSON
ответы ИИ возвращают короткие id, код мапит обратно).

### 2.4 Группировка (результат `group`, кладётся в daily.data.streams)
```json
{"streams":[{"tag":"ставка_цб","title":"ЦБ и ставка","gist":"2-3 предложения","ids":["a1b2c3"],
             "net_tone":-30,"heat":80,"assets":["SBER","рубль"]}],"orphans":["…"]}
```
Больше 120 новостей → группировать пачками и слить второй FLASH-вызов.

### 2.5 Совет — `backend/council.py` (сборщик A)
```python
async def daily(days: float | None = None, reason: str = "по кнопке") -> dict   # весь этап 1; возврат: council_runs.data
async def update_with_news(new_ids: list[str], reason: str) -> dict            # пересчёт: прошлый итог + новые новости → анализ/критика/вердикт/итог
async def human(text: str) -> dict                                             # этап 2
def latest() -> dict | None                                                    # store.council_latest(None) — текущая истина (daily|human|update)
def summary_text(summary: dict) -> str                                          # компактный текст итога для промптов (≤ 2500 символов)
async def present_frame(kind: str, payload: dict) -> dict                      # FLASH: {"headline","frame","highlights":[{"ticker","side","line"}]}
def snapshot() -> dict                                                          # для /api/v5/state: {"daily":…,"human":…,"latest_kind","news_stats"}
async def market_snapshot() -> str                                              # цены каталога (moex.last_price / tinkoff) в строки "SBER 285.4 +1.2%"
```
Порядок `daily`: collect → triage → characterize → group → snapshot(цены) →
PRO analysis (stream) → PRO critique (stream) → PRO verdict (stream) → PRO
summary (json) → FLASH distribute → store.council_put(run_id,"daily",data).
Каждой стадии: `bus.stage(... start/done)`; текстовые стадии стримятся через
`bus.text` в stage `analysis|critique|verdict`.

**Итог совета (summary JSON — единая схема для daily/human/update):**
```json
{"regime":"risk-on|risk-off|смешанно","summary":"3-6 предложений",
 "picks":[{"ticker":"SBER","side":"long|short","conviction":0..100,"horizon":"часы|день|дни",
           "why":"1-2 предложения","trigger":"уровень/событие для входа","risk":"что ломает идею",
           "news_ids":["a1b2c3"]}],
 "avoid":[{"ticker":"…","why":"…"}],"watch":[{"ticker":"…","why":"…"}],
 "key_times":["14:00 МСК — …"],
 "human_alignment":[{"thesis":"…","verdict":"согласен|частично|нет","why":"…"}]   // только human
}
```
**Тело `council_runs.data`:**
```json
{"kind":"daily|human|update","reason":"…","ts":…,"days":3,"time_msk":"…",
 "stats":{"raw":…,"relevant":…,"characterized":…,"streams":…},
 "streams":{…§2.4…},"snapshot":"текст цен","astro_line":"…",
 "texts":{"analysis":"…","critique":"…","verdict":"…"},
 "summary":{…},"cards":[…§2.6…],"human":{"raw":"…","compressed":{…}},"frame":{…},
 "tokens":{"prompt":…,"completion":…}}
```
### 2.6 Карточки тикеров (`distribute`)
```json
[{"ticker":"SBER","name":"Сбербанк","asset_class":"share","side":"long","conviction":72,"horizon":"день",
  "why":"…","trigger":"…","risk":"…","note":"1 фраза FLASH",
  "news_ids":["…"],"news":[{…элементы news_window…}],
  "tone_avg":12,"honesty_avg":64,"hype_avg":41,"importance_max":80,"news_count":5}]
```
FLASH получает: picks + список тикеров каталога (`instruments.all_grouped()`, код+имя)
+ строки новостей окна (render) → возвращает `{"cards":[{"ticker","news_ids","note"}]}`;
агрегаты считает код из `news_ai`.

### 2.7 Этап 2 — `council.human(text)`
FLASH сжатие: `{"clean_text":"…","by_asset":{"SBER":["тезис"],"рынок":["…"]},
"theses":[{"asset":"…","claim":"…","direction":"вверх|вниз|нейтрально","confidence":"низкая|средняя|высокая"}],"questions":["…"]}`
(правило: ничего не терять, только сжимать и править орфографию/ясность).
Затем PRO: analysis(вход: `summary_text(latest daily)` + сжатый взгляд + астро + snapshot) →
critique → verdict → summary(JSON + human_alignment) → distribute → present_frame.

## 3. Дозор — `backend/watch.py` (сборщик A)
```python
async def loop() -> None                         # бесконечно, каждые config.PYTHIA_WATCH_SEC; сервер стартует в фоне
async def tick(force=False) -> dict | None       # один проход: collect(1д) → triage новых → characterize → impact
async def impact(run_id, new_items: list[dict]) -> dict   # FLASH(think=True): см. ниже
def serious_since(ts: float) -> list[dict]       # заметки с severity ≥ порога после ts
```
Заметка (`watch_notes.data`):
```json
{"ts":…,"n_new":7,"n_relevant":3,"ids":["…"],"changes":true,"severity":0..100,
 "note":"2-4 предложения что могло бы поменяться","affected":["SBER","рынок"],
 "recommend_rerun":true,"items":[…новости с ai…]}
```
При `severity ≥ config.PYTHIA_WATCH_SERIOUS` → `watch` зовёт
`mission.on_serious_news(note)` (импорт в try/except) — миссии сами решают,
делать ли полный пересмотр. Первый `tick` — сразу при старте цикла, затем по таймеру.
Пустой прогон (нет новых релевантных) заметку НЕ создаёт.

## 4. Миссия — `backend/mission.py` + `backend/market_ctx.py` (сборщик B)

### `market_ctx.py`
```python
async def build(ticker: str, asset_class: str, *, full=True) -> dict
   # {"dossier": dict(pipeline.collect_dossier), "text": str(render_dossier full, аварийный потолок DOSSIER_LIMIT=150 000), "price": float|None,
   #  "astro": str(astro.render_compact, откат render_for_ai), "astro_line": str, "aether": str|"",
   #  "kuramoto": dict|None, "kuramoto_text": str, "bifurcation": dict|None, "bif_text": str,
   #  v5.1 — слои 4.x (dict = сырые данные, *_text = готовый текст для ИИ, сбой → «слой недоступен: …»):
   #  "wyckoff_text" (D1/H1: read, фаза, bias, бокс лёд…крик, цена на % высоты, ДО ЛЬДА/ДО КРИКА в %, в цене и ATR → «ближе к …», события с ±% от цены),
   #  "xray", "xray_text" (microstructure.xray_live: OBI, CVD, VPIN, Хёрст, спред, Казимир, VWAP, классификатор),
   #  "reactor", "reactor_text" (reactor_sky: хроно-удар, Z(θ), Доплер, катализатор Курамото, окно Пригожина, КАМ),
   #  "calendar", "calendar_text" (bif_calendar + tagger: цикл, языки Матьё, окна, плотность среды),
   #  "sync_text" (aether_resonance.sync_rupture по поводырям), "weather_text" (weather по регионам инструмента),
   #  "oracle", "directive", "oracle_text" (свод машин 4.x: состояние, направление, P(вверх), голоса, зоны, калибровка),
   #  "scan", "scan_text" (maya_scan.status → render_for_ai, если тиков ≥ MIN_TICKS_AGG), "errors":[...]}
async def light(ticker, figi, asset_class) -> dict  # перепроверка: цена, стакан, лента, 5м-метрики + «Рентген»/«Майя» → {"text","price","book","xray","maya"}
def render_kuramoto(kur) -> str; def render_bifurcation(bif) -> str; def render_wyckoff(wy, price) -> str
```
Курамото: ряды закрытий (`moex.candles`/tinkoff 1h, 14 дней) для лидеров
`MX, BR, SI` + сам тикер → `kuramoto.graph({...})`; сбой → None + note.
Бифуркации: `bifurcation.bifurcation_context(closes)` по дневным/часовым.
Всё в try/except — миссия не падает без любого слоя. Тексты слоёв — числа и
факты; толкование школы — короткий абзац «как читают», без приказов.

Сканер стакана «8 ч» (`maya_scan`, из 4.x): миссия запускает его при старте и
`resume` на `PYTHIA_SCAN_MIN` минут (`maya_scan.start(ticker, ticker, asset_class, minutes)`),
останавливает на stop/panic; `maya_scan.render_for_ai(status)` — блок для промптов,
`maya_scan.brief(status)` — сводка для панели (`status()["scan"]`). Почему работает:
накопленный за часы дисбаланс и пустоты стакана плюс согласие агрессора ленты —
путь наименьшего сопротивления; усреднение по тысячам тиков даёт упреждающий
сигнал, потому он точнее при долгой работе и срабатывает с задержкой.

### Исполнение 5.4.0 — `trader_broker.py`, `ai_pilot.py`, `mission.py` (второй проход стороннего ревью + доработки W4)
`Broker.place(..., request_id=None)` → `{ok, order_id, request_id, id_type: exchange|request, status, exec_lots, uncertain}`
(orderId = UUID — ключ идемпотентности PostOrder; сеть/5xx/408/409 → `uncertain: True`, «ответ потерян ≠ отказ»);
`order_state/cancel(oid, request_id=True)` (ORDER_ID_TYPE_REQUEST); `place_stop` с UUID `orderId`; `stop_orders(strict)`
и `orders(strict)` бросают исключение вместо пустого списка; `flat_all(figi, lot, *, on_intent, on_check, force)`:
on_check (персист-гейт) → портфель → стопы strict → снятие активных заявок по figi → снятие стопов → рыночное
закрытие; ответ `{ok, orders, stops_unknown, warnings, note}`. `AIPilot`: state атомарный (tmp+replace) с `pending`,
`exit_order`, `stop_request`, `panic`, `panic_force`, `close_pending`, `foreign_lots`; заявка уходит только после
сохранения UUID (кроме панического закрытия — уходит и без персиста); `stop_request` урегулируется разрешителем
(`STOP_REQUEST_MAX_AGE_SEC` 300: старый/отбитый запрос сверяется со списком стопов биржи — найден → принят, нет →
новый UUID, список недоступен → ждём честно); тик не пропускает аварийный трос/триггер/тейк при висящем запросе;
паника: после `PANIC_STOP_SWEEP_TICKS` (3) неподтверждённых отмен — снятие всех стопов по figi через листинг,
`panic(force)` закрывает без проверки. `mission.panic(ticker, force=False)`: живые пилоты получают `panic()` ПЕРВЫМИ,
`_restore_pending_panics()` в try/except (ошибка базы — только для пути без пилота: честный отказ с note),
повторная ПАНИКА в `PANIC_REPEAT_SEC` (120 с) после отказа или пока закрытие «ждёт подтверждения» = force; `_restore_pending_panics` → RuntimeError только у
start (500 «миссия не стартовала…») и resume/reanalyze (503) — не у паники; `_state_file_position` unreadable → отказ start/resume, паника с force закрывает
по счёту; `auto_resume` (ручной стоп переживает рестарт, сторож поднимает только `settle_only`); `panic_task` под
shield; `_restore_state` при чужом счёте откладывает файл `.mismatch-<время>`.
Второй проход (V → W4): `ORDER_REQUEST_MAX_AGE_SEC` 90 — фантом: заявка по UUID-запросу, о которой биржа отвечает
«не найдена» (`_order_not_found`: 404 / 400 с кодом 50005/50007 или «not found»; 401/403/429/408/409 — нет), снимается
из учёта только если запрос старше порога И «не найдена» держится ≥ порога подряд (`not_found_since` в `pending` /
`exit_order` / `panic_orders`, переживает рестарт; любой иной ответ обнуляет наблюдение); `_not_sent` (ConnectError на
PostOrder) — определённый отказ, не uncertain; `order_state/cancel` отдают `not_found`; при панике после
`PANIC_PENDING_DROP_ATTEMPTS` (3) неразрешимая заявка снимается по force (UUID в лог); `_flat_account` под force
сбрасывает старые закрывающие заявки и закрывает по портфелю; `flat_all` после снятия хотя бы одной активной заявки
перечитывает портфель (без force нечитаемый портфель = отказ с причиной, с force — прежний снимок с предупреждением);
отложенная сверка остатка помнится (`exit_check` / `exit_check_strict`, персистятся) — следующий тик сверяет счёт, а не
шлёт рынок вслепую; одноразовый `m.panic_force` от второго нажатия при закрытии в полёте срабатывает на следующем тике
сторожа (≤ 60 с). После ПАНИКИ пилот стоит в фазе panic: новая миссия по тикеру — сначала СТОП (иначе 409 «уже идёт
миссия»). Сервер: `_stop_owned_work()` гасит
совет/миссии/пилоты/сканеры/чат/толмач/сверку до закрытия HTTP-клиентов, дожидаясь `panic_task`. Конфиг: атомарная
запись, битый JSON → RuntimeError с сохранением файла, пустой файл = нет настроек.

### `mission.py`
Фаза 3 (W1), поведение перепроверки: PRO промолчал (таймаут 1200 с / сеть / ответ не JSON) → `MissionPilot._review_silent`:
`review_ts` = min(…, now + `REVIEW_RETRY_SEC` 300) — ранняя повторная попытка, как при «шифровщик не дал exec»;
накопленный `_review_reason` не стирается, `last_action` «дежурный PRO промолчал (…) — повторю через N мин; повод
сохранён: …», стадия шины `review` error, толмач узел `review`, `ai_v5.note_error` (панель проблем). После закрытия
позиции причина остаётся первой в `last_action`: «ЗАКРЫЛ ВСЁ (ПОБЕДА: тейк … / мягкий стоп: FLASH решил слить … /
аварийно …): P/L … · после закрытия — дежурный PRO решит через N мин» (`_ask_review_now`, kind `pilot`).
```python
PLAY = ("long", "short", "mixed", "auto")
class MissionPilot(ai_pilot.AIPilot):    # переопределяет _review (PRO, богатый контекст), _gather_news (из store), _close_all/_absorb_fill (журнал сделок)
async def start(ticker: str, play: str, deposit: float | None = None, reason="запуск") -> dict   # {"ok","run_id","note"}
async def council_again(ticker: str, reason: str) -> dict       # полный разбор с нуля: analysis→critique→verdict→exec; adopt в пилот
async def stop(ticker) -> dict; async def resume(ticker) -> dict; async def panic(ticker) -> dict
def status(ticker) -> dict | None                                # см. ниже (+ "scan": maya_scan.brief|None)
def snapshot() -> dict                                           # {"active": ticker|None, "missions": {ticker: status}}
async def scan_start(ticker, minutes=None) -> dict; def scan_stop(ticker) -> dict; def scan_status(ticker) -> dict   # сканер стакана
async def on_serious_news(note: dict) -> None                    # серьёзная новость → пересмотр активной миссии (council_again)
async def watchdog() -> None                                     # раз в минуту: миссия жива? позиция без пилота → поднять
```
Порядок `start`: registry-проверка (один депозит = одна активная миссия) →
`bus.start_run("mission", {"ticker"})` → stage scan (сканер стакана, `maya_scan.start`;
при стопе пилота не гасится, только при панике или своей кнопкой) → stage dossier
(market_ctx.build) → stage news (`newsflow.enrich_ticker`, Google News по тикеру) →
PRO analysis(stream) → critique(stream) → verdict(stream) → exec(json) →
валидация сторон (как `pipeline._sanitize_forecast` для exec, плюс play-режим:
long→BUY, short→SELL, mixed/auto→любое) → FLASH `council.present_frame("mission", …)`
(try/except) → store.mission_put → `pilot.adopt_forecast({"exec": ex})` → задача `pilot.run()`.
Нет токена Tinkoff → совет и приказ считаются и показываются, пилот не стартует
(`note` объясняет). `PYTHIA_DRY=1` → брокер dry (ордера в лог).

**exec-приказ (JSON от PRO, после валидации):**
```json
{"do":"BUY|SELL|WAIT|CLOSE","entry":число|null,"entry_kind":"сейчас|откат|прорыв","take":число|null,
 "invalidation":число|null,"why":"1 фраза","plan":"3-6 строк: как ведём, где добавляем/выходим, чего ждём",
 "confidence":0..100,"news_ids":["…"],"levels":[число…],"time_note":"тайминг: когда ждать движения",
 "wait_for":"при WAIT: что должно случиться, чтобы войти; иначе \"\""}
```
5.4.1: `WAIT` — вне рынка (5.4.2: без оценок «перевеса нет» / «не трусость»; шифровальщик переводит вердикт как есть,
пустой `wait_for` → «условие входа не названо — реши по живой картине»): после `_validate_exec`
`{"do":"WAIT","entry":null,"entry_kind":"сейчас","take":null,"invalidation":null,"wait_for":str,"why","plan","confidence",
"news_ids","levels","time_note"}`, фаза миссии `idle` (пилот ЖДУ_ПЛАН), `m.exec` хранится, дежурный PRO вернётся к нему на
перепроверке; панель — «ВНЕ РЫНКА — ждём: wait_for» без уровней входа. `CLOSE` — только при позиции (v5.2).
Три вида входа (v5.1h): `сейчас` — `entry: null`, входим немедленно (5.4.1: после проверки входа у двери); `откат` — засада: уровень ближе к стопу (BUY ниже цены, SELL выше), вход, когда цена
подойдёт (`ARM_TICKS`) или уже лучше уровня; `прорыв` — уровень за ценой (BUY выше, SELL ниже),
вход, когда цена пройдёт уровень (два тика подряд за уровнем, `mission.BREAK_CONFIRM_TICKS`;
`MissionPilot._entry_ready` поверх хука `AIPilot._entry_ready`). Вид определяет геометрия
(`mission._entry_kind`: подсказка ИИ важна только без цены); уровень, равный цене, = «сейчас»;
для прорыва стоп обязан быть за текущей ценой (иначе идея была бы мертва до пробития — приказ
отклоняется, шифровальщик исправляет). План пилота несёт `kind`.

Промпты миссии (`prompts_mission`): system ≤ 20 строк (5.4.1: VOICE — голос 4.5.4, §7) + строка FREEDOM («слои даны
как данные, без указаний, что с ними делать: их вес — твой выбор»); user = шапка +
блоки в порядке `_CONTEXT_ORDER` (Вайкофф, досье, сканер, рентген, итог совета,
новости, дозор, погода, свод, Курамото, бифуркации, календарь, синхронизация,
реактор, небо, эфир, прошлый разбор). Блоки идут через `compress.fit` ДО сборки
промпта (ничего не режется ножницами, выше пределов — FLASH ужимает); критика
видит анализ, вердикт — анализ и критику (через shrink выше PYTHIA_CTX_LIMIT);
`status()["sizes"]` = {"analysis": {"prompt", "answer", "blocks": {имя: симв.}}, "critique",
"verdict", "exec", "review" (после перепроверки)}; `prompts_mission.CONTEXT_KEYS` — ключи
блоков в порядке промпта, `BLOCK_NAMES` — короткие имена для степпера; `mission.HEAD_RESERVE`
= 8 000 (fit держит SOFT − 8 000, запас на шапку и правила); жёсткий потолок — `PYTHIA_PROMPT_CAP`.
Стадии шины PRO/FLASH: «start» → `detail` = «кто: промпт N симв. — блок N, … · FLASH ужал: имя
было→стало», «done» → «ответ N симв.» (совет: «ответ N симв. (промпт M)»); `watch.impact`
шлёт `impact start` с размерами. Критик и вердикт миссии видят ОДНО и то же сжатие анализа.

**Перепроверка (`MissionPilot._review`, PRO, JSON):**
вход: ситуация пилота (`_situation_text`) + `market_ctx.light` + сканер стакана +
Вайкофф + `council.summary_text(latest)` + прошлый exec/план + свежие новости
(`newsflow.enrich_ticker` перед каждой перепроверкой, затем все свежие без среза)
(+ серьёзные заметки дозора) + астро-строка + время МСК →
`{"choice":"ЖДЁМ|ЗАКРЫТЬ|КУПИТЬ_СЕЙЧАС|ПРОДАТЬ_СЕЙЧАС|НОВЫЙ_АНАЛИЗ","why":"…","entry":число|null,"entry_kind":"сейчас|откат|прорыв","invalidation":число|null,"take":число|null,"note":"2-3 строки человеку"}`.
Исполнение (v5.1h): ЗАКРЫТЬ → закрыть всё; ЖДЁМ в позиции с новыми `invalidation`/`take` →
`_retune` передвигает трос и тейк (сторона проверяется, null/те же числа — ничего);
КУПИТЬ/ПРОДАТЬ → `_plan_from_review`: план сейчас / откат / прорыв (геометрия как у приказа);
дрейф решения НАПРАВЛЕННЫЙ — цена лучше снимка, по которому думал PRO, → входим (баг «войти
сейчас, а цена ещё лучше — не заходит» закрыт), цена ушла хуже снимка более чем на
`DRIFT_FRAC` (1%) → не гонимся, засада-откат по цене решения; без валидного stop — аварийный
стоп 0.6%; НОВЫЙ_АНАЛИЗ → полный совет (окно `PYTHIA_COUNCIL_GAP_SEC`). История перепроверок
копится в `mission.status()["reviews"]` (последние 20) и в store.

**Полный пересмотр после входа** (v5.1f, уточнено в v5.1h). Его зовут только `НОВЫЙ_АНАЛИЗ`
от дежурного PRO, кнопка ПЕРЕСМОТР и серьёзная новость при миссии без живого пилота.
Предохранители: пейсинг `REANALYZE_GAP_SEC` (10 мин) считается с ПЕРВОГО принятого плана
(`MissionPilot.adopt_forecast`); защита от переворота `PYTHIA_FLIP_QUIET_SEC` (1200 с) — вердикт
другой стороны при позиции моложе порога отклоняется, позиция держится, перепроверка через
5 мин; той же стороны — обновляет стоп/тейк. Совет и шифровальщик видят строку «ОТКРЫТАЯ
ПОЗИЦИЯ: …» (`_position_text`, блок ПРОШЛЫЙ РАЗБОР и блок ОТКРЫТАЯ ПОЗИЦИЯ в exec); вердикт
обязан сказать: держать / закрыть / перевернуть. Повод совета берётся у пилота
(`MissionPilot.council_reason`, колбэк `_bind_pilot`) — в стадиях и хронике виден настоящий повод.

**Ритм миссии (v5.1h — воля владельца: «PRO думает и решает каждые 30 мин, совет зовёт он»).**
Дежурный PRO думает и решает раз в `PYTHIA_REVIEW_SEC` (1800 с): ЖДЁМ / ЗАКРЫТЬ / КУПИТЬ /
ПРОДАТЬ / НОВЫЙ_АНАЛИЗ — между советами торгует он. Полный совет (три кресла + шифровальщик)
зовут только: старт миссии, кнопка ПЕРЕСМОТР, `НОВЫЙ_АНАЛИЗ` от PRO (по автоматическим поводам
не чаще `PYTHIA_COUNCIL_GAP_SEC`; окно закрыто → PRO получает строку «СОВЕТ: … просили полный
совет, окно откроется через N мин; до тех пор решай сам»), серьёзная новость без живого пилота.
Внеплановую перепроверку поднимают только события (`MissionPilot._ask_review`): резкий ход цены
(`_shock_watch` поверх `tick`: за окно `PYTHIA_SHOCK_WIN_SEC` цена ушла от края окна на
`PYTHIA_SHOCK_PCT` %; один ход — одна перепроверка) и серьёзная новость дозора (`on_serious_news`
при живом пилоте) — не чаще `PYTHIA_EVENT_COOL_SEC` после прошлой событийной и не раньше
`EVENT_MIN_GAP_SEC` (180 с) после любого ответа PRO; в тишине после входа (`PYTHIA_QUIET_SEC`)
события не приближают перепроверку, повод доходит до плановой — трос защищает. Поводы пилота
(«приказ протух», «идея мертва до входа», «после закрытия», «внешнее закрытие», «серия отказов
биржи», «вход невозможен») НЕ зовут совет и не дёргают PRO каждую минуту: повод копится к
ближайшей перепроверке («ПОВОД ПЕРЕПРОВЕРКИ (внеплановая): …» в ситуации), после закрытия
позиции — остыть `PYTHIA_AFTER_CLOSE_SEC` (900 с; 0 — ждать плановой). Тактик v5.1g (45 с / 2 мин /
бэкофф) больше нет. Ситуация PRO (`MissionPilot._situation_text`): ход за окно (мин/макс),
ход за ≈30 мин, стакан, позиция или план с видом входа (ВХОЖУ / ЗАСАДА (откат) / ЖДУ ПРОБИТИЯ,
возраст приказа и его срок), депозит и итог, прошлая перепроверка, давность последнего совета,
killswitch. В хронике поводы помечены `kind`: council | pilot | shock | news (+ `deferred` —
повод ждёт плановой перепроверки); `status()["pilot"]` несёт `review_reason`, `review_kind`,
`council_blocked`, `council_age_min`; план пилота — `kind`.

**v5.2 — весь счёт, размер от биржи, CLOSE, мягкий стоп, разведка** (воля владельца: «ИИ не думает,
сколько куплено; приказ купить/продать — код берёт на максимум с плечом; докупил руками — это тоже
его; вместо тупого стопа FLASH решает, слить или подождать и передать Совету»).
- *Весь счёт по инструменту* (`PYTHIA_ADOPT_ACCOUNT`, по умолчанию 1): при старте (`prepare`) и на
  каждой сверке (`_reconcile`) всё, что лежит на счёте по figi миссии, — позиция пилота
  (`AIPilot._absorb_account`): докупил владелец → лоты растут, вход = средняя брокера; продал → часть
  закрыта с P/L в журнал; перевернул → новая сторона; позиция без плана → принята с временными уровнями
  (`levels_placeholder`, триггер ±2 %) и ранней перепроверкой. Совет и шифровальщик видят «ОТКРЫТАЯ
  ПОЗИЦИЯ НА СЧЁТЕ …» ещё до пилота (`_account_position_text`, первый блок контекста `position`);
  `status()["account_pos"]`. Старая изоляция `foreign_lots` — только при `PYTHIA_ADOPT_ACCOUNT=0`.
- *Размер даёт биржа*: `tinkoff.max_lots` (OrdersService/GetMaxLots: buy/sell с маржой) →
  `Broker.max_lots` → `AIPilot._refresh_max` (кэш `MX_TTL_SEC`), `max_lots()` берёт ответ биржи; без
  него (dry/отказ) — локальный расчёт. `deposit` при запуске = только потолок владельца (`_cap_lots`),
  пусто — весь счёт. После входа ещё `PYTHIA_TOPUP_MAX` доборов, если биржа даёт (`_topup`, тег
  `aip-topup`). Маржевой дозор при размере от биржи — по `GetMarginAttributes` (`amountOfMissingFunds`),
  не по своей прикидке. `status()["pilot"]`: `account` ({free, liquid, missing, max_buy, max_sell}),
  `adopt_account`, `sized_by_broker`.
- *Приказ CLOSE*: `exec.do` ∈ BUY|SELL|CLOSE; CLOSE допустим только при позиции
  (`_validate_exec(in_pos=True)`) → `_close_pending`, закрытие ближайшим тиком, вне рынка до следующего
  решения; CLOSE до `prepare()` ждёт позицию со счёта; свежий BUY/SELL снимает его; `resume` CLOSE
  заново не принимает. Перепроверка: `ДОБРАТЬ` (план той же стороны «сейчас» → тик доберёт до максимума
  биржи) и `ПЕРЕВЕРНУТЬ` (закрыть и войти в другую сторону; против режима игры → ЖДЁМ); «купить» в
  лонге = ДОБРАТЬ, «продать» в лонге = ЗАКРЫТЬ (`_parse_choice`).
- *Мягкий стоп* (`PYTHIA_SOFT_STOP`, `MissionPilot.SOFT_STOP=True`): уровень «идея мертва» — триггер
  (`invalidation`, `inv0`), на бирже лежит аварийный трос на `PYTHIA_HARD_STOP_PCT` % дальше
  (`hard_stop`, `_hard_of`, `_stop_price`). Цена за триггером → `_guard_bg` → `MissionPilot._stop_guard`:
  живой рынок (`market_ctx.light`; FLASH — за секунды, PRO — минуты, трос страхует), ход цены, план и перепроверки, прошлые ответы у троса,
  сканер, свежая разведка (`scout.refetch`), совет, новости → `prompts_mission.stop_guard` →
  `ai_v5.money_json(…, route="mission_guard")` (5.4.1: PRO по умолчанию, `PYTHIA_MONEY_MODEL`; до 5.4.1 — `flash_json`) → `GUARD_SCHEMA` {decision: СЛИТЬ|ЖДАТЬ, why, hold_until_price,
  hold_minutes}. СЛИТЬ → закрыть; ЖДАТЬ → `holds`+1, новый триггер внутри троса, следующий вопрос через
  `hold_minutes` (`PYTHIA_SOFT_STOP_GRACE_SEC`), задача Совету без очереди и без окна
  `PYTHIA_COUNCIL_GAP_SEC` (`_guard_handoff`, handoff kind `stop`, повод «мягкий стоп: …»; переворот
  по такому совету без тишины `PYTHIA_FLIP_QUIET_SEC`). Больше `PYTHIA_SOFT_STOP_MAX_HOLDS` раз подряд
  ждать нельзя — закрытие; за тросом — закрытие без вопросов; модель молчит/падает → СЛИТЬ. Стадия шины
  `guard` (start/done); `status()["pilot"]`: `soft_stop`, `hard_stop`, `guard` {busy, holds, next_in_s,
  placeholder}, `guards[-10:]` (каждая запись с `side: "stop"|"take"`); `sizes["guard"]`.
- *Мягкий тейк* (фаза 2 · W2; `PYTHIA_SOFT_TAKE`=1, только у пилота с `SOFT_STOP`; воля владельца: «когда тейк
  срабатывает — FLASH очень быстро думает, закрывать или ждать; если ждать — передаёт Совету»): цена у
  `take` → `AIPilot.tick` → `_take_bg` (`TAKE_TIMEOUT` 45 с) → `MissionPilot._take_guard`: те же данные,
  что у троса (живой рынок, ход цены, план и перепроверки, прошлые ответы у тейка и троса, сканер,
  разведка, партнёры, совет, новости после совета, память) → `prompts_mission.take_guard` →
  `ai_v5.money_json(…, route="mission_take")` (5.4.1: PRO по умолчанию) → `TAKE_SCHEMA` {decision: ЗАФИКСИРОВАТЬ|ПОДЕРЖАТЬ, why, lock_price,
  tp_next, hold_minutes, note}. ЗАФИКСИРОВАТЬ → `_close_all("ПОБЕДА: тейк @… — FLASH: зафиксировать — …")`;
  ПОДЕРЖАТЬ → `_apply_take`: `take_holds`+1, триггер = `lock_price`, но не ниже `_lock_floor` (вход +
  `PYTHIA_TAKE_LOCK_PCT` % хода до тейка; лонг — ниже цены, шорт — зеркально; уровень не на безопасной
  стороне → фиксация), `_set_levels(inv0=True)` — аварийный трос считается от нового триггера (при
  `take_holds`>0 `_hard_of` не пускает его за вход: малая цель < 2×`PYTHIA_HARD_STOP_PCT` давала бы трос ниже
  входа и убыток на гэпе — правка проверяющего фазы 2) и
  переставляется на бирже ближайшим тиком (`restop`), `holds`=0, тейк → `tp_next` (на верной стороне) или
  `None` (снят — ведёт триггер), `take_next` = `hold_minutes`/`PYTHIA_SOFT_STOP_GRACE_SEC`, задача Совету
  без очереди (`_take_handoff` → `_fire_reanalyze(force=True)`, handoff kind `take`, `_council_kind="take"`
  → переворот по такому совету без тишины); совет уже идёт → перепроверка сразу после. Больше
  `PYTHIA_SOFT_TAKE_MAX_HOLDS` (2) раз подряд — фиксация без вопроса; модель молчит/падает →
  ЗАФИКСИРОВАТЬ; базовый `AIPilot` (`SOFT_STOP=False`) и `PYTHIA_SOFT_TAKE=0` — закрытие как раньше.
  Свежий приказ той же стороны сбрасывает `take_holds`/`take_next`; state-файл несёт их. Стадия шины `take`
  (start/done), толмач узел `take` («PRO/FLASH у тейка: ПОДЕРЖАТЬ/ЗАФИКСИРОВАТЬ» — по модели узла); `status()["pilot"]`:
  `soft_take`, `take_guard` {busy, holds, max_holds, lock_pct, next_in_s}; `sizes["take"]`; фронт — плитка
  троса «тейк · мягкий», бейдж «PRO у тейка» / «FLASH у тейка» (по `pilot.money_model`, 5.4.1), хроника «тейк: …».
- *Триаж событий* (фаза 2 · W2; `PYTHIA_EVENT_TRIAGE`=1; узел у денег — с 5.4.1 PRO по умолчанию, `PYTHIA_MONEY_MODEL`): резкий ход (`_shock_watch`) и серьёзная
  новость (`on_serious_news`) при открытой зрелой позиции (не в тишине `PYTHIA_QUIET_SEC`, рынок открыт, не
  СТОП) идут не сразу к PRO: `MissionPilot._ask_review` → `_triage_bg` (`EVENT_TRIAGE_TIMEOUT` 20 с) →
  `_event_triage` (короткий промпт: ситуация, ход цены, живой рынок ≤ 8 с, план и уровни, прошлая
  перепроверка, память, новости из хранилища — без сканера и разведки) → `prompts_mission.event_triage` →
  `ai_v5.money_json(…, route="event_triage")` (5.4.1: PRO по умолчанию) → `TRIAGE_SCHEMA` {urgency: СЕЙЧАС|ПЛАНОВО|САМ, action:
  null|"подтянуть_трос"|"снять_план", trigger, why} → `_apply_triage`: СЕЙЧАС (или сбой/молчание/непонятно)
  → `_ask_review_now` — PRO как раньше с пейсингом; ПЛАНОВО → повод копится к плановой (`review_ts` не
  тянется); САМ → «подтянуть_трос» (триггер между старым и ценой, `inv0` и трос биржи на месте) или
  «снять_план» (план добора той же стороны), повод к плановой. Фаза 3 (W1): ПЛАНОВО с action
  «подтянуть_трос» и безопасным уровнем (между триггером и ценой, в сторону прибыли) выполняется, как при
  САМ (FLASH вживую так и отвечает); «снять_план» — только у САМ. Один триаж за раз (второе событие во время
  триажа — сразу как раньше); `on_serious_news` дожидается триажа. Записи `pilot.triages` ({ts, kind,
  event, urgency, action, why, trigger, done}), поле `triage` у handoff, стадия шины `triage`
  (start/done), толмач узел `triage`, `sizes["triage"]`, `status()["pilot"]["triage_busy"]`.
- *Прокол сканера как повод* (фаза 2 · W3; `PYTHIA_PUNCTURE`=1, `PYTHIA_PUNCTURE_MIN`=0.6,
  `PYTHIA_PUNCTURE_COOL_SEC`=600; воля владельца: «прокол с таким-то процентом срабатывает часто: хотим зайти
  или уже в позиции — ИИ даётся „вот так и так“, и PRO думает»): `MissionPilot._puncture_watch` в `tick`
  (не чаще `PUNCTURE_CHECK_SEC` 5 с; `maya_scan.status()` кэширован) берёт `puncture_first` сканера
  (сторона = ведущая тяга, полоса `p_lo–p_hi`, `persistence`, глубина, тики) и роль по `_puncture_role`:
  позиция → «угроза» (против нас) / «подтверждение» (за нас); живой план входа без позиции → «вход» (в сторону
  плана) / «предупреждение» (против); ни того ни другого → сторона не важна. Стойкость ≥ порога, роль есть,
  ключ (сторона, бин полосы по `bin_step`, роль) не встречался `PUNCTURE_SEEN_SEC` 6 ч, пейсинг прошёл, пилот
  не занят (СТОП / совет / перепроверка / заявка в полёте), рынок жив и открыт → `prompts_mission.puncture_block`
  (числа сканера как есть: тяга за окно, серия тиков, агрессор и согласие, Хоукс, натяжение; «что это значит
  для нас» по `PUNCTURE_ROLES`; толкование школы отдельной фразой) → `pilot.puncture` {ts, side, persistence,
  p_lo, p_hi, depth, ticks, role, our_side, in_pos, price, text, pending, state, reason, triage?} →
  `_ask_review(kind="puncture")`: в зрелой позиции — триаж W2 (узел у денег; блок первым и в ситуации триажа; СЕЙЧАС →
  PRO, ПЛАНОВО → повод копится, САМ → подтянуть трос / снять план), вне рынка — `_ask_review_now` с пейсингом
  `PYTHIA_PUNCTURE_COOL_SEC` вместо `PYTHIA_EVENT_COOL_SEC`. `_situation_for_ai` ставит блок ПРОКОЛ СКАНЕРА
  первым в ситуацию перепроверки и триажа, пока `pending`; ответ PRO снимает `pending` и пишет `state`
  «PRO решил: …». Иначе — тишина с честной причиной в `pilot.puncture_now.state` (ниже порога / сторона не
  важна / уже был поводом / пейсинг / пилот занят / рынок). Дедуп `_puncture_seen` и пейсинг `_last_puncture_ts`
  переживают рестарт (фаза 3): секция `pilot` state-файла `data/aipilot_state.json` рядом с позицией
  (`AIPilot._state_extra/_restore_extra` — хуки, `MissionPilot` пишет `{"puncture_seen": [[сторона, бин, роль, ts]…],
  "last_puncture_ts"}`; файл живёт, пока есть позиция или память; протухшее при подхвате отбрасывается).
  Handoff kind `puncture` (с полем `triage`), стадия
  шины `puncture` (done, data — запись без текста), толмач узел `puncture` («Сканер видит прокол вверх 78 %»),
  `status()["pilot"]`: `puncture`, `puncture_now` (без text), `puncture_min` (None — выключено). Панель:
  блок «Прокол» в карточке сканера (сторона, стойкость, полоса, роль, порог, что делаем: PRO думает / PRO решил /
  триаж (PRO/FLASH по `money_model`) / подтянут трос / повод к плановой / тишина), хроника «прокол», толмач 🕳, стадия «Прокол
  сканера». Мок: стакан 50 уровней стоит на сетке 0.5 % медленной цены с полосами вакуума на фиксированных
  уровнях и сканер тикает раз в секунду — прокол в демо появляется за полминуты. Стенд: сценарии 23–25.
- *Разведка данных* (`backend/scout.py`, `PYTHIA_SCOUT`, стадия `scout`): перед аналитиком совета
  (daily/update/human), миссии и чата FLASH получает описание того, что увидит PRO (`_scout_brief`), и
  каталог (любые тикеры, фьючерсы по базовому коду, рынок в целом — фьючерс на индекс MX/RI, история
  1d/1h) → `plan()` → JSON запросов (≤ `PYTHIA_SCOUT_MAX`) → `fetch()` (Tinkoff → MOEX ISS для акций и
  сырьевых/валютных фьючерсов) → блок «ДАННЫЕ ПО ЗАПРОСУ РАЗВЕДКИ FLASH» у аналитика/критика/вердикта
  (совет: `extra=` в `prompts_council.analysis_*`, `data["scout"]`; миссия: `pctx["scout"]`,
  `m.scout_reqs`, `m.scout_text`, `status()["scout"]`). Перепроверка и трос берут те же запросы свежими
  (`_scout_fresh` → `refetch`, без нового вопроса FLASH). **Индексов MOEX ISS нет** (v5.3, воля владельца:
  «данные нетрезвые, отстают»): `IMOEX`/`RTSI`/`MIX`/`RTS` в запросах ИИ → `ALIASES` → фьючерс `MX`/`RI`
  (`asset_class` futures, только Tinkoff; без токена строка «нет данных — индексных данных нет»),
  отраслевые индексы ISS (`ISS_INDEXES`) → класс `index` и строка «индексы MOEX ISS не используются».
- *Связанные бумаги* (`backend/correlate.py`, `PYTHIA_PARTNERS`, стадия `partners` миссии между `dossier`
  и `news`): `partners(ticker, asset_class, days=60)` → до 6 бумаг `{code, name, asset_class, kind, rho,
  lead, n, rho_lag, move_1d, move_5d, price, source}` — вселенная `universe()` = соседи по сектору
  (`SECTORS`: банки и финансы, нефть и газ, металлы и добыча, ритейл, телеком, IT, энергетика, транспорт,
  застройщики, химия, холдинги; группы фьючерсов) + макро-набор `MACRO` (MX/RI индексы, Si/CR рубль,
  BR нефть, GD золото, NG газ; ближайшая серия через `tinkoff.resolve`); дневные свечи Tinkoff, без
  токена — `moex.candles` для акций и сырья, индексные фьючерсы — «нет данных»; `compute()` — ρ Пирсона
  по лог-доходностям общих дней (`MIN_POINTS` 20), лаг ±1 день (`lead` +1 партнёр опережает, −1 отстаёт,
  если `|ρ_lag| ≥ |ρ| + 0.15` и ≥ 0.30); отбор по `strength()` ≥ `MIN_RHO` 0.25, индексный фьючерс —
  всегда, если есть данные; кэш `store_v5.kv` `partners:<TICKER>` на `PARTNERS_TTL` 6 ч (`cached`,
  `reset_cache`), `explain(ticker)` — честная заметка (без данных: …). `text(items, ticker)` — блок
  «СВЯЗАННЫЕ БУМАГИ» (факты + один абзац «обычно читают так», без приказов). Миссия:
  `mission._partners_block` → `m.partners`, `m.partners_text`, `pctx["partners"]` (в `CONTEXT_KEYS` после
  `scout`), `MissionPilot._partners_fresh` → `prompts_mission.review(partners=…)` и
  `stop_guard(partners=…)`, `status()["partners"] {items, text, note}`; разведка —
  `scout.partner_requests(ticker)` (`correlate.scout_requests`: quote + history 5d по каждому партнёру,
  FLASH их не просит заново, свежие числа при каждом `refetch`); чат — `api_chat._partners_text` → блок
  «СВЯЗАННЫЕ БУМАГИ ИНСТРУМЕНТА МИССИИ».
- *Предохранители после проверки v5.2*: `_close_all` не шлёт второй ордер, пока закрытие идёт в другой
  задаче (`pos["closing"]`); переворот руками переносит старый `stop_id` и снимает его `restop`; добор
  по приказу при цене за invalidation отменяется; кэш GetMaxLots (`_mx`) сбрасывается после своего
  закрытия / исполнения / приёма со счёта; CLOSE при заявке входа в полёте закрывает исполненную
  частичку; после собственного закрытия позиция «со счёта» принимается только при двух сверках подряд
  (`ADOPT_COOL_SEC`, лаг портфеля); state-файл несёт `inv0`, `hard_stop`, `holds`, `guard_last`,
  `guard_next`, `levels_placeholder`, `topup_left`, при рестарте трос пересчитывается; биржа отбила
  стоп-заявку (`_replace_stop`) → позиция под виртуальным стопом (тик закроет за уровнем), `pos["stop_err"]`,
  повтор выставления через `ai_pilot.STOP_RETRY_SEC` (30 с, `restop` + `restop_after`), толмач — узел
  `refusal` «Биржа отбила стоп-заявку (трос)» (одно объяснение на серию). Стенд ситуаций —
  `backend/scenarios.py` (22 сцены поверх настоящих `Mission`/`MissionPilot` на фейках, таблица по сценариям).
  Совет миссии не стирает `sizes["guard"/"take"/"triage"]`; `watch.impact` — под `IMPACT_TIMEOUT` 480 с.
- *Паника без пилота*: `panic()` при остановленном пилоте закрывает позицию по инструменту прямо на
  счёте (`_flat_account` → `Broker.flat_all(figi, lot)`, снимает стоп-заявки).
- *Ошибки Tinkoff с текстом*: `tinkoff.TinkoffError` (код, message, description, подсказка `_ERR_HINTS`:
  30042 недостаточно средств…), `humanize_api_error`; `Broker` возвращает их в `note`.

**Толмач и память миссии** (`backend/explain.py`, v5.3; воля владельца: «отдельный FLASH описывает владельцу каждое
решение — непонятно, что происходит, после совета та же картинка»; «сжатие с каждым ключевым узлом»).
```python
KINDS = {council, entry, topup, review, guard, close, market, refusal, other}   # (значок, имя)
def note(m, kind, title, detail="", refs=None, *, ctx=None, persist=None) -> bool   # узел в очередь; дебаунс MIN_GAP_SEC=20, GATHER_SEC=1.5, дубль за DEDUP_SEC=600 — False
async def flush(m) -> dict | None       # всё из очереди → один вызов FLASH (route "explain"; PYTHIA_EXPLAIN_MODEL=pro → PRO) → запись
def prompt(ctx, events, prev) -> (system, user)          # system ≤ 12 строк; блоки ЧТО ПРОИЗОШЛО / СИТУАЦИЯ ПИЛОТА / ПРИКАЗ СОВЕТА / ПОСЛЕДНИЕ РЕШЕНИЯ / ПАМЯТЬ / СВЕЖИЕ НОВОСТИ / СВЯЗАННЫЕ БУМАГИ / ТВОИ ПРОШЛЫЕ ОБЪЯСНЕНИЯ
async def memorize(m, why, *, ctx=None, persist=None) -> str | None   # накопившееся → абзац ≤ MEMORY_LIMIT=1500 (route "memory"; длиннее — compress.shrink), успех → списки режутся
def memorize_bg(m, why, ...) -> bool; def items(m, n=40); def text(m, n=10); def status(m); def memory_status(m); def reset(ticker=None)
```
Запись толмача: `{"ts","kind","title","text","refs":{…},"events":[{"ts","kind","title","detail"}],"ok":bool,"model":"flash|pro"}`;
сбой ИИ → `ok:false`, `text` = «Толмач не ответил (…). По факту: …». Хранится в `m.explain` (последние 40, в
store через `_persist`), шина `bus.stage("mission", rid, "explain", start|done|error, detail, data=запись)`.
Узлы в `mission.py` (`_tolmach(m, kind, title, detail, refs)`): `_council` → `council` (+ `_memorize` «после
совета»); `MissionPilot._absorb_fill` → `entry` / `topup`; `_review` → `review` (+ раз в `PYTHIA_MEMORY_EVERY`
перепроверок `_memorize`); `_apply_guard` → `guard`; `_journal` → `close` (позиция закрыта → `_memorize`);
`_closed_tick(first)` / `_on_market_open` → `market`; `_enter` / `_pending_tick` (рост `_entry_fail`) и
`_close_all` с `close_fail` → `refusal` (счётчик попыток вычищен — один отказ, одно объяснение). Контекст
`_explain_ctx(m)`: ситуация пилота, приказ, последние решения, память, новости после совета / до совета
(`_split_news` по `m.council_ts`), связанные бумаги, сделки. Память: `m.memory`, `m.memory_ts`, `m.memory_n`,
`m.reviews_since_memory`; после успеха `reviews[-5:]`, `handoffs[-5:]`, `pilot.guards[-3:]`, `explain[-10:]`;
блок `pctx["memory"]` (`CONTEXT_KEYS` перед `prev`), `prompts_mission.review(memory=)`, `stop_guard(memory=)`,
чат — строка «ПАМЯТЬ МИССИИ» и три последних объяснения в блоке МИССИЯ И ПИЛОТ; `_gather_news`/`_news_quick`
при наличии памяти отдают только новости после совета (`_news_after_council`) с пометкой, что старые ужаты.
`status()`: `explain` (items), `memory` {text, ts, n, limit}, `exec_ts`; `explain_list(ticker)` →
`GET /api/v5/mission/explain`. Фронт: карточка «Что происходит» (`#m-explain`, `explainHtml`/`renderExplain`,
`Xp.onStage` — стадии `explain`/`memory` не идут в степпер), отметка «обновлено HH:MM» и подсветка изменившихся
полей приказа (`orderChanged`), сворачиваемая «Память миссии». Мок: route `explain` → живой текст по данным,
`memory` → абзац. Конфиг: `PYTHIA_EXPLAIN`, `PYTHIA_EXPLAIN_MODEL`, `PYTHIA_MEMORY_EVERY`.

**Рыночные часы** (`backend/market_clock.py`, `PYTHIA_MARKET_CLOCK`=1; воля владельца: «запустил — пришло
время, биржа закрыта — стопорится; новости в фоне копятся»).
- Время: `market_clock.now()` = локальные часы + сдвиг к серверу биржи (`sync()` раз в 10 мин по заголовку
  `Date` ответа Tinkoff — `tinkoff.server_date()`, без токена — MOEX ISS (только Date), без сети — локальные
  часы и предупреждение в лог); `skew()` для статуса. Логика «сейчас» для биржи берёт только его.
- Статус: `status(ticker, asset_class, instrument_id=None)` → `{"open", "reason", "next_open_ts",
  "next_open_in_s", "next_open_msk", "session": "утренняя|основная|вечерняя|выходная|клиринг|закрыто|выходной",
  "exchange", "sessions_today": [{from, to, session, open, why}…], "source": "tinkoff|schedule", "static",
  "ts", "note", "skew_s"}`.
  **Площадка инструмента** (W1 «часы по площадке»): `tinkoff.resolve` (кэш 6 ч) → поле `exchange` бумаги в
  верхнем регистре (SBER/GAZP — `MOEX_MRNG_EVNG_E_WKND_DLR`, SiZ6 — `FORTS_FUTURES_WEEKEND`; не резолвится —
  `MOEX`/`FORTS` по классу с пометкой) → `TradingSchedules` именно по ней (`tinkoff.trading_schedules`, кэш 6 ч
  по площадке; `from` — «сейчас» по часам биржи: раньше текущей даты API не принимает, ошибка 30003).
  **Откуда окна дня**: сделки с `opening_auction_start` + 10 мин (если аукцион есть; на выходных 09:50 → 10:00,
  брокерский `start` 02:00 не берём) или со `start`, до `end`; внутри дня — паузы по классу актива, которых
  в API нет: акции — аукцион открытия основной 09:50–10:00, аукцион закрытия 18:40–18:50 («закрытие
  основной»), пауза 18:50–19:05 перед вечерней; фьючерсы — клиринги 14:00–14:05 и 18:45–19:05; валюта — без
  пауз. Сессия по времени: утренняя (до 10:00), основная (10:00–18:50; фьючерсы до 18:45), вечерняя (после
  19:05), выходная (сб/вс). «Открыто сейчас» — по-прежнему `GetTradingStatus` (кэш 60 с; открыто =
  NORMAL_TRADING и лимитные заявки и API доступны); расписание — календарь, `next_open_*`, `sessions_today`,
  `describe`. Разошлись — статус главнее: биржа «открыто» при паузе по расписанию → открыто, note «верим
  бирже»; биржа «недоступны» в окне сделок → закрыто, reason «(по расписанию … — верим бирже)», next_open —
  следующее окно. Статус не получен, расписание есть → `source: "schedule"`, `static: false`, note честно.
  Без токена / без расписания — статический регламент по классу актива (акции 07:00–09:50 утренняя,
  10:00–18:40 основная, 19:05–23:50 вечерняя; фьючерсы 07:00–23:50 с клирингами; валюта 07:00–23:50;
  выходные — «зависит от бумаги, без расписания биржи считаем закрытым»; праздники) с note «расписание
  статическое», `static: true`, `exchange: null`.
  `is_open(...)`, `describe(st)` → «рынок закрыт до 07:00 МСК (ночь — торги ещё не начались; биржа: торги
  недоступны) — вход возможен только с открытия» / «рынок закрыт до 10:00 МСК (выходной: сделки с 10:00 после
  аукциона 09:50; …)» / «рынок закрыт до 07:00 МСК понедельника 28.09 (выходной — на этой площадке торгов
  нет)» / «рынок открыт: торги идут, утренняя сессия до 09:50». `snapshot()` → то же + `exchange`,
  `sessions_today`, `text` — в `/api/v5/state.market` (пилюля «Биржа»: площадка и окна сделок дня в подсказке).
- Стопор пилота (`AIPilot.tick`, шаг 0в после паники/killswitch): рынок закрыт → `_closed_tick`: состояние
  `РЫНОК_ЗАКРЫТ` (фаза `closed`), заявка входа снята (частичка — в учёт), план жив до открытия, позиция
  остаётся под аварийным тросом (биржи при `PYTHIA_EXCHANGE_STOP=1`, иначе виртуальным в программе) — узел у троса не спрашивают, тейк/флип/добор не трогаем,
  `review_ts` → открытие + `OPEN_REVIEW_GRACE_SEC` (90 с), сверка со счётом реже, петля тикает раз в
  `PYTHIA_CLOSED_TICK_SEC` (30 с, кусками — стоп/паника ловятся сразу), стакан не дёргаем. Открылось →
  `_on_market_open`: сразу сверка, состояние по факту (`_resting_state`), закрыт был ≥ `CLOSED_LONG_SEC`
  (30 мин) → `MissionPilot._market_open_review` → `_ask_review(kind="open")` без пейсинга и тишины с поводом
  «рынок открылся: накопились новости/события (закрыт был N ч)» + серьёзные заметки дозора за это время
  (`watch.serious_since`) + поводы, что копились; короткий клиринг — просто продолжаем. При закрытом рынке
  `_ask_review` ничего не приближает (note «рынок закрыт … повод дойдёт до перепроверки на открытии»,
  handoff `deferred`), `_shock_watch` молчит. Совет и приказ разрешены: `pctx["market"]` → строка `РЫНОК: …`
  в шапке аналитика/критика/вердикта/шифровальщика (`prompts_mission._head`), в ситуации перепроверки —
  `РЫНОК: …`. `status()["pilot"]`: `market`, `market_closed_since`; `mission.status()["market"]`;
  `GET /api/v5/state` → `market` {open, reason, next_open_ts, next_open_msk, next_open_in_s, session,
  exchange, sessions_today, source, note, skew_s, skew_source, enabled, text}. Мок (`PYTHIA_MOCK_TINKOFF=1`) — биржа всегда открыта.
Режим отказа (осознанный выбор): сбой Tinkoff при известном прошлом статусе — пилот держит прошлый статус (закрыто → стопор до следующего успешного ответа, кэш 60 с); статус неизвестен — считаем рынок открытым (без Tinkoff торговать всё равно нечем); часы без синка — локальное время с предупреждением в лог. Единая база времени: `ai_v5.now_msk`/`fmt_ts` берут сдвиг `market_clock.skew()` — «Сейчас» в промптах и метки событий живут в одном времени с биржей.

**v5.4.1 «трезвый пилот»** (воля владельца 24.09.2026; W2 — `ai_pilot.py`/`mission.py`, W1 — промпты, W3 — панель):
- *Стопы только в программе* (`PYTHIA_EXCHANGE_STOP`=0): стоп-заявок на бирже нет, трос `hard_stop` виртуальный —
  тик за ним закрывает по рынку без вопросов (`price <= hard` → `_close_all`); 1 — как в 5.4.0 (стоп на бирже дальше
  триггера). `status()["pilot"]["exchange_stop"]`; панель предупреждает: при падении программы позиция без защиты.
- *Узлы у денег — PRO* (`PYTHIA_MONEY_MODEL` pro|flash): трос (`mission_guard`), тейк (`mission_take`), триаж
  (`event_triage`), проверка входа (`mission_entry`), мысль о прибыли (`mission_profit`) идут через
  `ai_v5.money_json(system, user, route=…)`; таймауты GUARD/TAKE/ENTRY/PROFIT 600 с, TRIAGE 300 с; молчание →
  безопасное правило (СЛИТЬ / ЗАФИКСИРОВАТЬ / PRO как раньше / входа нет / ДЕРЖАТЬ). `status()["pilot"]["money_model"]`.
- *Проверка входа у двери* (`PYTHIA_ENTRY_CHECK`, `MissionPilot._entry_gate`): перед КАЖДОЙ заявкой входа по плану
  (сейчас / откат / прорыв; добор по решению PRO и переворот — тоже; авто-добор по округлению биржи `_topup` — нет)
  PRO смотрит живой рынок: `prompts_mission.entry_check(ticker, name, play, *, situation, plan, history, light, news,
  council_text, scan, scout, partners, memory, guards, time_msk, checks)` → `ENTRY_SCHEMA` {decision:
  ВОЙТИ|ЖДАТЬ|ОТМЕНИТЬ, why, entry, entry_kind, wait_minutes, invalidation, take, council, note}. ЖДАТЬ — новый уровень
  (`entry`+`entry_kind`) или срок (`wait_minutes`), можно поправить стоп/тейк; ОТМЕНИТЬ — план снят, `council=true` →
  полный совет (handoff kind `entry`); молчание/сбой → входа нет, повтор через `PYTHIA_ENTRY_CHECK_COOL_SEC`. Толмач
  узел `entry_check`. `status()["pilot"]`: `entry_gate` {busy, next_in_s, decision, why, ts, entry, entry_kind} | null
  (последняя проверка по текущему плану), `gates[-10:]` [{ts, decision, why, note, price, entry, entry_kind,
  wait_minutes, council}] (персистятся в state).
- *Мысль о прибыли* (`PYTHIA_PROFIT_THINK`, `MissionPilot._profit_watch`): в плюсе — пройдено ≥ `PYTHIA_PROFIT_THINK_PCT`
  % хода от входа до тейка (без тейка — плюс ≥ `PYTHIA_PROFIT_THINK_MIN_PCT` % от входа) или рывок в нашу сторону
  (`_shock_watch`, ход ≥ `PYTHIA_SHOCK_PCT`) при плавающем плюсе, не чаще `PYTHIA_PROFIT_THINK_COOL_SEC` →
  `prompts_mission.profit_think(ticker, name, play, *, situation, profit, history, light, plan, news, council_text, scan,
  scout, partners, memory, guards, time_msk, thoughts)` → `PROFIT_SCHEMA` {decision: ДЕРЖАТЬ|ВЫЙТИ|ВЫЙТИ_И_ПЕРЕЗАЙТИ|
  СОВЕТ, why, lock_price, take, reentry, reentry_kind, note}: ДЕРЖАТЬ (+`lock_price` → триггер, `take` → цель) / ВЫЙТИ /
  ВЫЙТИ_И_ПЕРЕЗАЙТИ (`reentry`, `reentry_kind` → план той же стороны, вход через проверку входа) / СОВЕТ (триггер к
  lock_price, полный совет без очереди, handoff kind `profit`). Толмач узел `profit`. `status()["pilot"]`: `profit`
  {busy, next_in_s, threshold_pct, progress_pct} | null, `profits[-10:]` [{ts, decision, why, note, price, floating,
  lock_price, take, reentry, reentry_kind}] (персистятся).
- *Приказ WAIT* — см. exec-приказ выше; *голос 4.5.4* и закон промптов (system ≤ 20 строк) — §7.

**`status(ticker)` (для фронта):**
```json
{"ticker":"SBER","name":"Сбербанк","asset_class":"share","play":"auto","started_ts":…,
 "run_id":"…","phase":"council|armed|entering|in_position|stopped|panic|idle|error|closed",   // closed — рынок закрыт (пилот стопорится)
 "market":{"open":bool,"reason":"…","next_open_ts":…,"next_open_msk":"10:00 МСК","session":"…","source":"tinkoff|schedule"}|null,
 "pilot":{…AIPilot.status()…},"exec":{…},"frame":{…},"texts":{"analysis":"…","critique":"…","verdict":"…"},
 "news":[…прикреплённые (news_window-элементы)…],"reviews":[{"ts","choice","why","note"}],
 "handoffs":[{"ts","reason","kind":"council|pilot|shock|news|stop|take|puncture|entry|profit","deferred":bool,"triage":"СЕЙЧАС|ПЛАНОВО|САМ …"|null}],   // поводы: совету / дежурному PRO / мягкий стоп / мягкий тейк; triage — вердикт триажа (W2); 5.4.1: entry — дверь → совет, profit — прибыль → совет
 "trades":{"count","pnl","net","gross","fee","wins","losses","est","confirmed","by_day","mode","last_sync"},   // фаза 4 W1: ledger.trades_summary
 "price":число|null,"note":"человеку","error":null,"exec_ts":…,
 "explain":[{"ts","kind","title","text","refs","ok","model"}],                  // v5.3: лента толмача (последние 40)
 "memory":{"text":"…","ts":…,"n":2,"limit":1500},                                // v5.3: память миссии одним абзацем
 "account_pos":"ОТКРЫТАЯ ПОЗИЦИЯ НА СЧЁТЕ: …"|null,           // v5.2: что лежит на счёте до пилота
 "scout":{"requests":[{"kind","code","name","days","interval","why"}],"text":"…"},   // v5.2: разведка FLASH
 "sizes":{"analysis|critique|verdict|exec|review|guard|take|triage":{"prompt","answer","blocks"}},
 "scan":{"running","ticks","elapsed_min","left_min","side","up_share","dn_share","now_side","streak","aggressor_mean","agree","puncture","tension","hawkes_n","note"}|null}
```

### `backend/api_mission.py` (сборщик B) — `router = APIRouter()`
```
POST /api/v5/mission/start    {ticker, play, deposit?}      → mission.start
POST /api/v5/mission/stop     {ticker}                       → пилот стоп (позиция остаётся под тросом)
POST /api/v5/mission/resume   {ticker}                       → поднять пилот заново с сохранённым планом/позицией
POST /api/v5/mission/panic    {ticker?, force?}              → закрыть всё и встать; если проверка невозможна (база/state/список
                                                               стопов) — честный отказ с note, повтор в 120 с или force закрывает напрямую
POST /api/v5/mission/reanalyze {ticker, reason?}             → council_again
GET  /api/v5/mission/status?ticker=                          → status | {"active": false}
GET  /api/v5/mission/explain?ticker=                         → mission.explain_list: {ok, ticker, active, items, memory, enabled, model, pending} (без ticker — активная миссия)
GET  /api/v5/trades?ticker=&limit=                           → {"trades":[…store_v5.trades…],"summary":{…ledger.trades_summary…}}
GET  /api/v5/trades/day?date=YYYY-MM-DD                      → ledger.day_summary (без date — сегодня по МСК; кривая дата → 400)
POST /api/v5/trades/sync      {ticker?}                      → ledger.sync_and_reconcile (ручная сверка; est → ok false + note, HTTP 200)
POST /api/v5/mission/scan     {ticker, minutes?}             → mission.scan_start (409 если не запустился: нет токена)
DELETE /api/v5/mission/scan?ticker=                          → mission.scan_stop
GET  /api/v5/mission/scan?ticker=                            → mission.scan_status (status + brief + text)
```

### `backend/ledger.py` (фаза 4 · W1) — журнал по операциям брокера, чистый результат, итоги дня
Пилот пишет сделку по цене тика (`MissionPilot._journal`: `pnl` без комиссий, + `figi`, `lot`, `point_value`,
`fee_est`, `source: "est"`) и ставит `ledger.schedule_after_close(ticker, live=боевой Broker)` — через ~20 с
выгрузка операций и сверка. Фон `ledger.loop()` в `server.py`: раз в 5 мин при живой миссии/позиции, раз в час иначе;
только в режимах broker/mock. Сеть: `tinkoff.operations(account_id, from_ts, to_ts, instrument_id=None)` —
`OperationsService/GetOperationsByCursor` (только EXECUTED, курсор/страницы, комиссии и tradesInfo) → список
`{id, ts, kind buy|sell|fee|margin|varmargin|other, type, state, figi, uid, qty (штук, quantityDone), price, payment
(со знаком), fee (|commission|), trades:[{ts, qty, price}], name, parent}`; сбой → `None` (причина в
`tinkoff.last_error()`).
```python
def mode() -> "broker"|"mock"|"est"      # токен и не PYTHIA_DRY | PYTHIA_MOCK_TINKOFF=1 | операций нет — всё оценка
def fee_pct() -> float                  # PYTHIA_FEE_PCT, % за сторону (0.05)
def estimate(trade) -> float | None     # fee_est = (|entry| + |exit|) × штук × ₽/пункт × pct/100
async def sync(account=None, since_ts=None) -> {"ok","mode","read","added","since","note"}   # операции → ops (30 дней при первой)
async def reconcile(ticker=None, apply=True) -> {"ok","mode","matched","partial","added","est","tickers":{T:{…}},"note"}
async def sync_and_reconcile(ticker=None) -> {"ok","mode","sync","reconcile","note","last_sync"}
def trades_summary(ticker=None) -> store_v5.trades_summary + {"mode","last_sync"}
async def day_summary(day: "YYYY-MM-DD"|None) -> {"ok","day","from_ts","to_ts","count","net","gross","fee","wins","losses",
        "best":{…brief…}|null,"worst":{…}|null,"trades":[{id,ticker,side,lots,entry,exit_px,entry_real,exit_real,pnl,pnl_gross,
        fee,net,est,source,why,opened_ts,closed_ts,mode}],"est":bool,"margin_fee":float|null,"session_pnl":float|null,
        "pilot_ticker","account":{"total","cash","note"},"mode","last_sync","note"}          # день — по МСК
def schedule_after_close(ticker, delay=20, live=False) -> bool
async def loop() -> None
```
Сверка (`reconcile_ticker`): инструмент — figi/uid из сделок пилота или `tinkoff.resolve`; для сделки окно
`opened_ts − 60 с … closed_ts + 120 с`, исполнения стороны входа/выхода по порядку до количества штук сделки
(переворот не съедает чужие) → `entry_real`/`exit_real` (VWAP по tradesInfo), `fee` = commission операций +
BROKER_FEE без родителя (дочерние с родителем, уже нёсшим commission, не удваиваются; MARGIN_FEE — суточная, к
сделке не относится), `pnl_gross` = сумма payment (акции, обе стороны) или по VWAP × штук × ₽/пункт, `pnl_net` =
gross − fee, `source` broker|mock (обе стороны) | partial (одна сторона, остальное — цена тика). Незадействованные
покупки/продажи после горизонта (самая ранняя сделка журнала или старт миссии) собираются в круги «из нуля в ноль»
→ `trade_add` с why «по операциям брокера» (открытый круг — не сделка); использованные id операций хранятся в
`trades.ops` — повтор ничего не плодит. Без операций — `fee_est`, `est: true`, честно «оценка».

## 5. API совета — `backend/api_council.py` (сборщик A) — `router = APIRouter()`
```
POST /api/v5/council/daily   {days?, reason?}     → фон; {"ok","run_id"} (409 если уже идёт или только запускается:
                                                     запуск резервируется до start_run (`_pending`); ошибка фоновой задачи → 500)
POST /api/v5/council/rerun   {reason?}            → update_with_news(новые relevant с последнего совета)
POST /api/v5/human           {text}               → фон; {"ok","run_id"} (409/500 — как у daily; rerun — так же)
GET  /api/v5/council/latest?kind=daily|human|update|any
GET  /api/v5/council/list
GET  /api/v5/news?days=3&relevant=1&limit=1000    → {"items":[…news_window…],"stats":{…}} (limit max 5000)
GET  /api/v5/news/item?id=
GET  /api/v5/watch?limit=30                       → {"notes":[…],"unseen":n}
POST /api/v5/watch/seen      {ids:[…]}
POST /api/v5/watch/tick                           → принудительный проход дозора; 409 если дозор идёт/резервирован
                                                     или идёт/запускается совет; ошибка фоновой задачи → 500
```
Фоновые задачи: `asyncio.create_task`, один живой прогон на scope (`bus.active`).

## 6. Общее API — `backend/api_v5.py` (написано)
```
GET  /api/v5/state      → {"app":{version,codename,dry,tinkoff,deepseek,model,model_fast},"clock":{"msk","ts"},
                           "astro":{"line"},"tokens":{prompt,completion,total,calls,"by_route":{route:{calls,prompt,completion,model}},"by_model":{"FLASH"|"PRO":{calls,prompt,completion}}},   // W2: ai_v5.usage()
                           "running":{…bus.running…},
                           "council":{…council.snapshot…},"watch":{"notes":[…10],"unseen":n},
                           "mission":{…mission.snapshot…},"market":{…market_clock.snapshot…},"trades":{…ledger.trades_summary: count,pnl,net,gross,fee,est,mode,by_day…},
                           "settings":{"exchange_stop":bool,"money_model":"pro|flash","entry_check":bool,"profit_think":bool,"profit_think_pct":float},   // v5.4.1: api_v5.settings_block() из config.*
                           "health":{                                                        // v5.3 фаза 3 (W1): панель проблем
                             "problems":[{"kind":"keys|deepseek|tinkoff|telegram|market|ws|astro|pilot|data","level":"err|warn|info",
                                          "text":"человеку: что случилось и что делать","ts":float|null}],   // ≤ 20, отсортированы err → warn → info
                             "last_ai_error":{"ts","route","code":int|null,"text","stale":bool}|null,      // ai_v5.last_error(): после всех повторов; stale — ИИ уже ответил позже
                             "last_broker_error":{"ts","path","code":str|null,"text","stale":bool}|null,   // tinkoff.last_error(): TinkoffError текстом (30042…) или сеть
                             "astro_mode":"precise"|"lite"|null,                                        // по кэшу astro.peek_context, без расчёта
                             "ws_clients":int|null,                                                     // вкладок на /ws/events (server.HUB.count)
                             "state_age_s":float|null}}                                                 // секунд с последнего тика пилота активной миссии
GET  /api/v5/run?run_id=  → bus.run(run_id) (с текстами)
POST /api/v5/keys  {deepseek?: str|list, tinkoff?: str, telegram_token?: str, telegram_chat?: str,   // W2: токен бота и chat_id
                    exchange_stop?: bool, money_model?: "pro"|"flash"}                          // v5.4.1: переключатели «Ключей» → PYTHIA_EXCHANGE_STOP "1"/"0" (False — явно "0"), PYTHIA_MONEY_MODEL; в changed — имена настроек; ответ + settings
                   // весь запрос проверяется до записи (ошибка любого поля → 400, ничего не записано; нестроковые токены → 400);
                   // пул DeepSeek → config.set_deepseek_keys (демо-режим держит его в памяти), остальное → одной записью set_many
DELETE /api/v5/keys[?what=telegram]   // без what — всё (и Telegram); what=telegram — только токен бота и chat_id
GET  /api/v5/keys  → {deepseek, deepseek_accounts, deepseek_mask, tinkoff, tinkoff_mask, dry,
                      telegram: bool(токен), telegram_mask, telegram_chat (маска), telegram_bound, telegram_on,   // только маски
                      settings: {exchange_stop, money_model, entry_check, profit_think, profit_think_pct}}       // v5.4.1
GET  /api/v5/health → ai.health()
GET  /api/v5/telegram → telegram.status(): {enabled, token, token_mask, bound, chat_mask, polling, queued, sent, last_ok, last_error, daily}
POST /api/v5/telegram/test → getMe + тестовое сообщение в привязанный чат: {ok, name, note, sent}
```
`health` собирает `api_v5.health_block(keys, mission_snap, market)`: каждый источник в try/except, сбой одного не
роняет ни блок, ни state (сломанный блок → пустые `problems`). Что попадает в `problems`: **keys** — нет ключа
DeepSeek (err: «нажми «Ключи»…»), нет токена Tinkoff (warn), сухой прогон / мок (info); **deepseek** — последняя
ошибка после всех повторов (`ai.note_error` в `ask/stream/chat` + `ai_v5.note_error` при таймауте/не-JSON у
вызывающего): 401/402/403 — err, 429/5xx/таймаут/сеть — warn, после успешного ответа (`ai.last_ok_ts`) — только в
`last_ai_error` со `stale`; **tinkoff** — последняя ошибка API брокера (`tinkoff.note_error` в `_post`), пока биржа
не ответила снова (warn, с подсказкой для 401/429); **market** — рынок закрыт (info, с временем открытия), часы
споткнулись (warn); **astro** — небо в lite-режиме (info); **pilot** — активная миссия: `error` миссии/пилота,
`position.close_fail`, `position.stop_err` без `stop_id`, `killswitch.locked` (err), бэкофф отказов биржи
`entry_fail`/`no_entry_in_s` (warn); **data** — при открытом рынке цена миссии старше `PRICE_STALE_SEC` (60 с) или
стакан протух (`market_alive` false в позиции/плане) — warn. Для этого `AIPilot.status()` отдаёт `entry_fail`,
`no_entry_in_s`, `tick_ts`, `book_age_s`, а `MissionPilot.status()` — `price_ts`; **telegram** (фаза 4 · W2) — токен
бота есть, но чат не привязан (info), бот отбит — 401/403/429/сеть по `telegram.last_error()`, пока не ответил снова (warn).

### `backend/telegram.py` (фаза 4 · W2) — Telegram-бот владельца
Воля владельца: «впишу бота — он присылает уведомления: сделка такая-то, расчётные данные, итоги за день; через бота я
могу всё видеть». Конфиг (config_user.json через `config.set_many`, в панель — только маски): `TG_BOT_TOKEN` (BotFather),
`TG_CHAT_ID` (чат владельца), `PYTHIA_TG` (1 — включён), `PYTHIA_TG_DAILY` (1 — итоги дня). Сеть — httpx, Bot API
`sendMessage` (HTML) и `getUpdates` (long polling 25 с, offset); токен ни в лог, ни в ответ не попадает (`_scrub`).
```python
def enabled() -> bool                # PYTHIA_TG и есть токен
def bound() -> bool                  # … и есть chat_id
def status() -> {"enabled","token","token_mask","bound","chat_mask","polling","queued","sent","last_ok","last_error","daily"}
def last_error() -> {"ts","method","code","text","stale"} | None     # панель проблем kind "telegram"
def send(text, silent=False, chat=None) -> int   # в очередь (HTML): дробление > 3800 симв. по строкам, дедуп текста 60 с; 0 — выключен/не привязан/повтор
async def flush(limit=None) -> int   # очередь → сеть, 1 сообщение/с; 429 → retry_after; 400 «can't parse» → тот же текст без разметки;
                                     # 401/403/404 → выброшено, пауза 60 с, last_error; сеть → до 3 попыток
def notify(key, line, silent=False)  # события одного узла копятся WINDOW_SEC (5 с) → одно сообщение
def problem(kind, text) -> bool      # проблема — не чаще раза в 10 мин на вид (HEALTH_GAP_SEC)
async def on_event(ev) -> None       # слушатель шины (быстрый, без сети)
def wrap_sink(inner) -> sink         # server.py: bus.set_sink(telegram.wrap_sink(HUB.broadcast))
async def handle_command(text, chat, user="") -> str | None
async def handle_update(upd) / poll_once() -> int
async def daily_tick(now_msk=None) -> bool     # 18:55 и 23:55 МСК (+30 мин), если биржа торговала; kv tg_daily_done
async def market_tick(open_now, snap=None) -> bool   # переход закрыто↔открыто → «рынок открылся/закрылся, позиция …»
def health_tick(problems) -> int     # err из api_v5.health_block → problem(kind)
async def check_token() -> {"ok","name","note"}    # getMe
async def loop() -> None             # server.py: gather(опрос команд, отправка очереди, планировщик 30 с); без токена спит
def trade_line(t, confirmed=None) -> str; def fmt_day(d) -> str      # формы «сделка такая-то» и «итоги дня»
```
**События шины → сообщения** (scope `mission`, по узлам `scope:ticker:stage`): `exec done` → «📜 Приказ T: …»;
`pilot progress` с `data` сделки (`closed_ts`+`pnl`, из `_journal`) → «✅ Сделка закрыта: сторона, лоты, вход → выход,
нетто, комиссия — оценка …»; `pilot progress` с `data.fill` (новое событие `MissionPilot._absorb_fill`: вход/добор
исполнен) → «🟢 T: ВОШЁЛ …»; прочие `pilot progress` — только со словами ВОШЁЛ/ДОБРАЛ/ПРИНЯЛ/РЕСТАРТ/закрыт/отбит/CLOSE/
заявк/трос/стоп (передачи задач Совету — тишина); `guard done` → «🛡 PRO у троса …» (модель узла — по `PYTHIA_MONEY_MODEL`); `take done` → «🎯 …»;
`review done` → «🔁 Перепроверка PRO …»; `explain done` (`data.text`) → «🗣 заголовок + текст толмача» (главное
человеческое сообщение; `explain error` — тишина); `summary done` scope mission → «🏛 Совет по T»; scope
`daily|update|human` `summary done` → «📰 Совет: …»; scope `ledger` `reconcile done` (новое: `ledger.announce` после
сверки в `schedule_after_close`, `data.trade` — сделка с source broker|mock) → «💰 Сверено с брокером: … нетто …»;
`status error` (mission/daily/update/human/watch, кроме explain) → «⚠️ …» не чаще раза в 10 мин на `scope:stage`.
Планировщик (раз в 30 с при привязке): `health_tick` (err из панели проблем, раз в 10 мин на вид), `market_tick`
(статус биржи из активной миссии или `market_clock.snapshot`; первый замер — молча), `daily_tick` (18:55 и 23:55 МСК,
если `market_clock._is_trading_day`; второй слот молчит, если сделок не прибавилось и пилот не жив).
**Команды** (только из чата `TG_CHAT_ID`; чужой чат и непривязанный `/start` получают «твой chat_id: N — впиши в
панели»): `/start` (привязан + справка), `/status` (миссия, фаза, цена, позиция: сторона/лоты/вход · триггер · трос ·
тейк, P/L сессии, журнал по тикеру, рынок, следующая перепроверка, последнее действие), `/pos` (позиция, ход от входа,
трос на бирже, счёт), `/trades` (последние 10, чистыми, источник), `/day [YYYY-MM-DD]` (`ledger.day_summary` →
`fmt_day`), `/explain` (последние 3 толмача), `/health` (панель проблем), `/council` (итог последнего совета: рамка,
режим, входы), `/help`; управление — `/stop` и `/panic` только через вопрос и ответ «да» в течение 60 с
(`mission.stop` / `mission.panic`; «нет» — отмена; просрочка — не выполняется); всё в лог `pythia.telegram`.
Данные бот берёт через подменяемые источники `_snapshot/_day/_trades/_trades_summary/_health/_council/_stop/_panic/
_market/_trading_day` — self-тест (`python3 -m backend.telegram`) гоняет всё на фейковом `_post` и фейках данных.

### `backend/api_chat.py` (v5.2) — `router = APIRouter()`, чат панели
```
POST   /api/v5/chat            {text, model?: "pro"|"flash"} → {"ok","run_id","model"}; 409 — ИИ ещё отвечает; 400 — пусто/нет ключа
GET    /api/v5/chat/history                                 → {"items":[{role,text,ts,model?,run_id?,error?,scout?}],"busy","run_id","model","default_model"}
DELETE /api/v5/chat/history                                 → {"ok": true}
GET    /api/v5/chat/status                                  → {"busy","run_id","model","default_model"}
```
Прогон — scope `chat` в шине: стадии `context` → `scout` → `answer` (дельты `text`/`think`) → `end`
(`bus.end_run`); полный текст — `GET /api/v5/run?run_id=` (`texts.answer`). Контекст: `mission.snapshot`
(позиция, план, триггер/трос, что даёт биржа, перепроверки, передачи, ответы у троса, разведка),
`council.latest` + `summary_text`, `market_ctx.light` по инструменту миссии, `store_v5.watch_list`,
`newsflow.fresh_since`/`news_for_ticker` (rich), небо (`api_v5._astro_line`), `scout.run("chat", …)`
под вопрос, блок ДИАЛОГ (последние 12 реплик из kv `chat_history`, хранится 60). Каждый вопрос — новый
чат ИИ; system ≤ 20 строк (5.4.1, с VOICE — §7); из чата приказов бирже нет.

Старые эндпоинты остаются: `GET /api/price?code=`, `GET /api/candles?code=&interval=&days=`,
`GET /api/instruments`, `GET /api/instruments/search?q=`, `GET /api/orderbook?code=`, `WS /ws/events`.
Фронт шлёт в `/ws/events` строку `ping` каждые 25 с и `ack` каждые 200 кадров.

## 7. Правила промптов (обязательны для всех)
- Русский. System-промпт ≤ 20 строк (5.4.1; было 12): роль в одной строке, голос (`prompts_mission.VOICE`),
  что дано, что вернуть. Без воды и дисклеймеров. Закон проверяют self-тесты `prompts_mission`, `prompts_council`,
  `api_chat` и `tests/test_prompts_voice.py` (`prompts_mission.system_lines(s) <= SYSTEM_MAX_LINES`).
- Голос 4.5.4 (5.4.1 «ТРЕЗВЫЙ ПИЛОТ», воля владельца: «раньше он был более трезвый и свободный»):
  `prompts_mission.PERSONA` (1 строка — стратег, который знает изнутри, как работают маркетмейкеры и фонды, и думает
  как тот, кто двигает рынок) + `DOCTRINE` (8 строк: 1 без цензуры и воды; 2 кухня рынка — кто НАБИРАЕТ, кто
  РАЗДАЁТ, где ВЫНОСЯТ стопы, толпа всегда поздно; 3 конкретика — каждый тезис с числом и таймингом по МСК;
  4 сценарии с триггерами и отменой — из них одно ясное решение; 5 холодный расчёт — решителен, где данные
  позволяют, честен, где сигналы спорят; 6 преимущество и асимметрия — где рынок ошибается и куда перекошены
  риск/прибыль; 7 (5.4.2) свобода решения — войти, добрать, держать, выйти, перевернуть, стоять вне рынка —
  равноправные ходы, у каждого своя цена) = `VOICE`. Стоит в system думающих и денежных узлов: миссия —
  `analysis/critique/verdict/exec_order/review/stop_guard/take_guard/event_triage/entry_check/profit_think`; совет —
  `analysis_daily/analysis_update/analysis_human/critique/verdict/summary`; чат — `api_chat.prompt`. Механика
  (`triage/characterize/group/group_merge/distribute/human_compress/present_frame/impact`, `scout`, `shrink`,
  `explain`, `memory`) — без доктрины. Пункты 4/5/5a/5b доктрины 4.5.4 (как читать небо, Вайкофф, первоисточник,
  эфир) не переносятся: слои идут данными (строка FREEDOM).
- Думающие стадии (анализ/критика/вердикт) — формат свободный, «по делу, с
  числами, уровнями и временем»; JSON-стадии — «верни строго один JSON-объект» (слово «json» обязательно в system:
  правило JSON-режима DeepSeek, иначе при явном потолке модель может лить пробелы до лимита за деньги).
- Каждый промпт получает текущее время МСК (`ai_v5.now_msk_str()`) и, где есть,
  тайминги новостей.
- Тон: точный и свободный, кратко, без морали. Подталкиваний нет ни к входу («главное войти верно, а не
  оттягивать», «флета нет», «то, что рынок реально даст в ближайший час»), ни к ожиданию (5.4.2: «не трусость»,
  «хуже пропущенного», «только с перевесом», «сомнение — ждать», «вслепую», «не лезем», «не от скуки», «наугад»,
  «долгая и дорогая») — `prompts_mission.BANNED` ловит обе стороны. Приказ совета: `do` ∈ BUY|SELL|WAIT|CLOSE —
  WAIT: вне рынка, `wait_for` — что должно случиться (уровень, подтверждение, время), `invalidation`/`entry` null;
  шифровальщик переводит решение вердикта как есть (BUY/SELL → BUY/SELL, «вне рынка» → WAIT) и после отказа кода
  исправляет уровни, а не решение. Перепроверка: приказ WAIT — ориентир, а не условие; каждое решение — заново по
  живой картине, прошлые ответы — данные, не якорь; choice равноправны, ЖДЁМ не первым в списке. Критик (миссии и
  совета) ищет ошибки в обе стороны — слабый вход и упущенный ход. Проверка входа у двери (`entry_check`): сверяет
  приказ с живым рынком, анализ заново не пересобирает; ВОЙТИ / ЖДАТЬ / ОТМЕНИТЬ равноправны; мысль о прибыли
  (`profit_think`): ДЕРЖАТЬ / ВЫЙТИ / ВЫЙТИ_И_ПЕРЕЗАЙТИ / СОВЕТ по картине, не по правилу. Совет: входов столько,
  сколько даёт картина (ни одного, один или несколько), список — на ближайшую торговую сессию и 1-3 дня; итог
  переводит вердикт 1:1; пересчёт видит прошлый итог и цены. Роли узлов у денег не привязаны к FLASH
  («дежурный миссии у троса / у тейка / на триаже / у двери»): модель — `ai_v5.money_model()` по
  `PYTHIA_MONEY_MODEL`.
- Ничего не выдумывать: нет данных → так и сказать; числа только из данных.
- Входы не режем ножницами (v5.1): ниже разумных пределов кресла, новости, досье,
  астро, слои идут целиком; выше — `compress.shrink`/`compress.fit` ужимают FLASH
  «без потери ни одной нити» (5.4.1: блок 60 000 симв., промпт 300 000, жёсткий потолок 450 000 — назад к
  5.1–5.3.2: в промптах ×3 модель тонула в слоях); стадии шины показывают размеры промптов; `clip` — только при
  провале ИИ, с пометкой.
- Новости: отбор пачками по 50 (остаток — последняя неровная пачка), разметка
  каждой новости отдельным вызовом FLASH по полному тексту; PRO видит сжатые
  строки с характеристиками, у важных — ещё «факты: … · суть: …» (`render(rich=True)`).
  Каждая модель видит своё: FLASH — полный текст одной новости, PRO — выжимку.
- Слои (небо, эфир, своды машин 4.x, сканер) — как данные: факты и числа;
  толкование школы — отдельным абзацем «обычно читают так», без директив
  «это разворот/входи»; вес слоя решает ИИ (строка FREEDOM в system).
- Отбор новостей: по суждению ИИ («что может двигать цены или нужно трейдеру,
  чтобы понимать день»), без квот и без «всё, что хоть как-то относится».
- Астро для ИИ — `astro.render_compact(ctx)`: 10 тел (созвездие IAU, λ, β,
  скорость, дом), Луна, аспекты, станции, ретро, лунации, затмения и окна ретро
  по эфемеридам (`_eclipses_calc`, `_retro_windows`, живут после 2026), глоссарий
  школы Ганна одним абзацем; без тропических знаков и рамок.
- ИИ без границ (воля владельца 24.09.2026, факты — `docs/DEEPSEEK_2026-09-24.md`): `reasoning_effort` не шлём
  (режима auto у API нет; пусто = умолчание сервера high, как в 4.5.4); `max_tokens` шлём явно = максимум
  модели 393 216 (`AI_MAX_TOKENS`; без него сервер режет 64K вместе с размышлением); FLASH по умолчанию
  `think=True`; `thinking=False` только у починки JSON и `health`. `ai_v5.MONEY_ROUTES` (guard/take/triage/
  entry/profit/exec/review/summary): пустой или кривой ответ → один повтор `<route>_retry` с размышлением, иначе
  исключение («молчание» → ответ по правилу), без `_nothink` и без FLASH-починки; своя очередь `SEM_MONEY`.
  5.4.1: узлы у денег (трос, тейк, триаж, проверка входа, мысль о прибыли) зовутся через `ai_v5.money_json(system,
  user, route=…)` — PRO или FLASH по `PYTHIA_MONEY_MODEL` (умолчание pro), таймауты 600/600/300/600/600 с, при
  молчании — безопасное правило. `ai.THINK_MARK` ставится всегда, когда content пуст; `finish_reason=length` и
  обрыв потока → `ai.note_error` + пометка в тексте. Модель FLASH — официальное имя `deepseek-flash`
  (V4.1-Flash); `ai._is_v4` узнаёт любое deepseek-* кроме legacy.

## 8. Фронт — `frontend/index.html` (сборщик C)
Один файл: разметка, стили и логика — прежний вид 5.3.3 с анимациями (воля владельца 24.09.2026; разложенный фронт
сторонней сборки в `static/terminal.js` / `terminal.css` / `workspace.css` панелью не используется, оставлен для сверки)
+ `static/lightweight-charts.js` (v4.2.3, уже лежит). Спокойный фон,
бумажные карточки, шрифт системный. Поток: 4 шага слева (Новости→Совет, Мой
взгляд, Миссия, Журнал), справа «Дозор». Стрим текстов совета через
`/ws/events` (`type: text|think` с `scope/run_id/stage`), снимок при открытии —
`/api/v5/state` + `/api/v5/run`. Подробности — в задании сборщика C.

**Проблемы и свежесть (v5.3 фаза 3, W2).** Единый центр проблем — индикатор `#ind-pb` в шапке и список
`#pb-pop`; объект `Pb` в `index.html`: `Pb.list()` = `state.health.problems` (§6) + локальные записи
`Pb.set(id, {kind, level, text, act})` / `Pb.clear(id)` (сервер недоступен `net`, поток `ws` с отсчётом, снимок
`stale` > 15 с при живом WS, цена миссии `data` > 60 с при открытом рынке, сканер `scan` без тиков > 45 с при
`running` и `ticks < 9600`, дозор `watch` без прохода дольше `2 × watch_sec` по `recent_runs`) + ошибки запросов
`Pb.apiFail(path, code, note)` (снимаются `Pb.apiOk(path)` при удачном ответе того же пути) + страховки, если
`health` пуст (ключи по `app.deepseek/tinkoff`, рынок по `market.open`). Порядок err → warn → info; действие по
`kind`: ключи → модалка «Ключи», пилот/данные/сканер → экран миссии, сеть → «Повторить», поток → переподключить,
дозор → «Дозор сейчас», запрос → «Скрыть». `api()` кладёт в исключение `status`, `path`, `note`, `net`, `timeout` (тихие опросы `quiet` — таймаут `API_POLL_TIMEOUT`
20 с через AbortController, иначе зависший запрос замораживал бы опрос; ревью 24.09.2026: опросы статуса/цены/свечей/сделок
идут по одному — `_missionPoll`/`_pricePoll`/`_candlePending`/`_tradesPending`, ответ другого тикера/интервала отбрасывается,
`cleanCandles` выкидывает битые OHLC и схлопывает дубли времени, поиск — `_srchId` + клавиатура ↑↓/Enter/Esc + ARIA combobox,
чат — `sending`/`clearing`/`revision`: двойной Enter не шлёт дважды, очистка — после успеха сервера, поздние события
завершённого прогона игнорируются); текст по
коду — `httpMsg()` (409 занято/конфликт, 400 сервер не принял, 404 нет адреса, 5xx упал). `toast(msg, 'err', 0,
key)` — липкий (кнопка «×»), дедуп по ключу/тексту, `toastClear(key)` снимает при «исправилось». Степпер:
стадия `error` → блок `.stg-err` в `#detail-<key>` с `detail` и кнопками `data-sact`
(council/human/reanalyze/resume). Свежесть: `freshHtml(ts, maxSec, {label, stale, badgeOnly})` →
`.fresh[data-ts][data-max]`, `freshTick()` раз в 30 с ставит `.stale` и бейдж; пороги `FRESH` (совет 6 ч,
приказ 2 ч, память сутки), перепроверка — `2 × reviewPeriod + 300`. Новый прогон: `newRun(key, host, prevKey)`
— подсветка `.newrun` и прокрутка на открытом экране, иначе `tabNew(key)` → бейдж «новое» на шаге/вкладке до
`show(key)`. Хроника пилота: `S.mission.evTab` (all/pro/flash/why), группировка соседних триажей, `evAll` «ещё
N». Кнопки: `busyBtn(btn, fn)` и `S.mission.acting` (кнопки пилота остаются запертыми и после перерисовки).
Новых эндпоинтов нет; Playwright-проверки — `pw_test.py` (мок).

**Красота и ясность (v5.3 фаза 3, W3).** Порядок блоков `#mission-body` = иерархия «что важно сейчас»:
`#m-head` → чипы `B_SPY` → `#m-now` → `#m-explain` → `#m-order` → `#m-frame` → `#m-pilot` → `#m-scan` →
`#m-partners` → `#m-chairs-h` + `#chairs-mission` → `#m-sizes` → график → сделки. `nowHtml(st, lines, phase)` —
каркас «Сейчас» (позиция: сторона, лоты, вход; уровни `[data-npct]` из `pilotLines` + `hard_stop`; отсчёт
`[data-cd]`; `marketBadge(mk)`; трос `ropeHtml` живёт здесь, а не в пилоте), `nowLive(st, px, fl)` — живые
числа без перерисовки (`[data-nfl]` P/L, `[data-nfls]` цена и % от входа, расстояния, `.near` ближе 0,35 %,
кайма `.long/.short/.council`); зовётся из `pilotLive` и `tickPrice` (`S.mission.fl`); `pilotCountdown`
обновляет и `#m-now [data-cd]` (`.now-cd.urgent` за 30 с). Из плиток пилота «Позиция» и «Плавающий P/L» убраны.
`chairsFold(st, live)`: кресла с текстом свёрнуты (`.closed`), пока есть объяснение толмача моложе
`FRESH.council` и совет не идёт; `S.mission.chairsOpen` (null — по умолчанию, true/false — руками через
`#m-chairs-all`, сбрасывается на новую миссию), клик по креслу ставит `dataset.user` и исключает его.
`partnersHtml(st)` — `status().partners.items` (`code, name, kind, rho, lead, n, move_1d, move_5d, price, source`)
строками `.pt` (полоса ρ от центра, `lead` ±1 → «опережает/отстаёт на день»), пусто — `partners.note` или
причина по `app.tinkoff`/фазе. Прокол: `PUNC_ROLE` (роль → цвет и толкование), `puncFlow(x)` → чипы
`.pf .st.gave/.flash/.pro/.quiet` из `pilot.puncture_now|puncture` (`state`, `triage`, `pending`, `reason`),
кольцо `ring(persistence)` цветом роли. Лента толмача: первая запись `.lead`, чип модели `.badge.pro/.flash`,
подпись вида `XP_RU`. Хроника: `HANDOFF_KIND` — PRO indigo, совет amber, сканер/триаж teal; решения FLASH у
троса/тейка — `badge flash` «FLASH · трос/тейк: …»; точка события `.ev.r-<цвет>`. Пузырь `#tok-pop`:
`tokensHtml(tok)` + `ROUTE_RU` (имя маршрута), доля FLASH/PRO по `tokens.by_model`, строки `tokens.by_route`
(модель → роль по `app.model_fast`/`app.model`); стоимость не выдумывается. Пустое состояние миссии —
`empty(...)` с кнопками `[data-go=council|launcher]`. CSS-блок «фаза 3 · W3» в `<style>`: `.now*`, `.pt*`,
`.punc .pf`, `.badge.flash/.pro/.council`, `.pop .rt`, табличные цифры, телефон (`.scan-grid`/`.order` в две
колонки, `.pt` в три ряда, кольцо прокола скрыто ≤ 480).

**Журнал чистыми, ключи Telegram, плитки (фаза 4 · W3).** `renderJournal()`: шесть плиток `#journal-tiles`
(сетка 3×2, шесть колонок только ≥ 1700 px, `.v.huge` через `clamp`): «Сделок» (боевых/сухих, сверено N),
«Чистыми» (`#jt-net`, бейдж брокер/оценка по `summary.est`, средняя · лучшая · худшая), «Брутто» (`#jt-gross`: по
ценам исполнения / по тику), «Комиссии» (`#jt-fee`, бейдж «оценка» при `est`, доля от |брутто|), «Win rate»
(кольцо, по нетто), «Профит-фактор» (`#jt-pf`, ср. победа / потеря); все числа — `tNet(t)` (= `net` сервера,
иначе `pnl − fee`). Свежесть: `#journal-fresh` «сверено HH:MM» из `summary.last_sync` (`fmtSyncAt`: сегодня по
МСК — время, иначе дата), «ещё не сверено» при режиме broker/mock без выгрузки, пусто в est. Блок «По дням»
(`#journal-days-card`): без прочих фильтров — `summary.by_day` сервера (все сделки, не только загруженные 200),
иначе — по видимым сделкам через `bDayKey(closed_ts)` (МСК = UTC+3); строка `tr[data-f="day:YYYY-MM-DD"]` →
общий перекрёстный фильтр (`S.jFilter.day`, `bJFilterApply(…, 'day')`, чип «день» в `#j-filters`, повторный клик
снимает). `tradesTable`: колонки Тикер · Сторона · Лоты · Вход · Выход · Брутто · Комиссия (`data-l="комиссия"`,
подпись «оценка» без операций) · Чистыми · Источник (`data-l="источник"`, бейдж брокер / мок-брокер / частично /
оценка с датой сверки в title) · Открыта · Закрыта · Почему · Режим; вход/выход — `entry_real`/`exit_real`, тик
подписью. Пустое состояние — «Сделок нет: журнал наполняется закрытиями пилота и операциями брокера». Карточка
«Сделки миссии» — чистыми, комиссии, «сверено HH:MM». `onStage`: scope `ledger` stage `reconcile` done →
`loadJournal()`/`loadMissionTrades()` на открытом экране и тост «Сверено с брокером: T сторона · чистыми …» (ключ
`ledger`). Плитки пилота: шестая «Сессия» (`#pt-session`: `pilot.session_pnl`, `pilot.trades` «сделок за смену»,
`killswitch.locked` → «стоп-кран» с причиной, иначе `day_loss_limit` «предел дня», `streak` → «потерь подряд»);
«Итог миссии» — чистыми с комиссиями (`status.trades` = `trades_summary(ticker)`), из «Депозит» предел дня убран;
CSS `#m-pilot .pilot-grid .tile:last-child:nth-child(3n+1|3n+2)` растягивает одинокую последнюю плитку на ряд
(7-я при «Счёт · биржа даёт»), на телефоне (2 колонки) — `:nth-child(odd):last-child`. Модал «Ключи»: поля `#k-tg`
(токен, `/^\d+:[A-Za-z0-9_-]{20,}$/`) и `#k-tg-chat` (`/^-?\d{1,20}$/`) с подсказкой `.k-hint` (BotFather → /start
боту → chat_id); `k-save` шлёт только заполненные поля (`telegram_token`, `telegram_chat`), имена изменений по-русски,
подсказка «напиши боту /start» пока `telegram_bound` false; статус `#k-tg-st` из `keys.telegram_mask/telegram_chat/
telegram_bound/telegram_on` (только маски); `#k-tg-test` → `POST /api/v5/telegram/test` → `@name: note`;
`#k-tg-unlink` → `confirmBox` → `DELETE /api/v5/keys?what=telegram`; «Стереть ключи» предупреждает и про Telegram.
Индикатор: `tgState()` → `{lvl off|wait|bad|ok, col, text}` по `state.keys` и `health.problems` kind `telegram`
(warn/err → «ошибка 401 — …», код из текста); пилюля `#ind-tg` в шапке (класс `wait`/`bad`/`ok`, текст в title;
на телефоне — точка `#md-tg` в `#ind-mini` и строка «Telegram» в пузыре состояния), строка «Telegram: …» в подвале
`Pb.html()`, `PB_IC/PB_RU/PB_ACT_BY_KIND.telegram` (действие «Ключи»). Демо-заглушка: `keys.telegram*`,
`/api/v5/telegram`, `/api/v5/telegram/test` (честная note «токена бота нет»). Playwright — `pw_test.py` (мок) +
`pw_w3.py` (плитки, дни, ключи, индикатор, 400 px, скриншоты).

**Трезвый пилот (v5.4.1, W3).** Логика 5.4.0 из `terminal.js` перенесена в `index.html`: `refreshState` склеивает
параллельные полные обновления в один запрос (`_stateRefresh`, после сбоя следующий вызов идёт заново) и передаёт в
`applyState(st, missionContext)` снимок `{ticker, revision}` — миссия из позднего снимка не затирает более свежий статус;
`S.mission.revision` растёт в `applyMissionStatus` и при запуске, `pollMission` сверяет ревизию до применения ответа; строка
«ПАНИКА упёрлась» в «Сейчас». Кто решает у денег — `mmOf(pilot, dflt)`/`mmCls` по `pilot.money_model` (без поля старые узлы
подписаны FLASH, как было; новые — PRO): бейджи «PRO у троса» / «PRO у тейка» / «PRO-триаж» в карточке пилота, `phaseLabel`
(«PRO у троса», «PRO у двери», «PRO о прибыли»), подсказки «Сейчас», `xpRu(kind, pilot)` у толмача (`XP_RU` +
`entry_check` «проверка входа», `profit` «мысль о прибыли»), `ROUTE_RU` + `mission_entry` «PRO у двери (проверка входа)»,
`mission_profit` «PRO о прибыли» (трос/тейк — «Страж у троса/тейка», модель видна в строке), `HANDOFF_KIND` + `entry`
(«дверь → совет») и `profit` («прибыль → совет»). Карточка пилота: бейджи «PRO проверяет вход…» (`entry_gate.busy`) и «PRO
думает о прибыли…» (`profit.busy`), строка последней проверки входа `[data-gate]` (решение, почему, уровень, следующий
взгляд) и последней мысли о прибыли `[data-profit]` (решение, почему, триггер/цель/перезайти, пройдено N % из порога),
хроника — записи `gates`/`profits` как ответы у троса (бейдж «PRO · вход: …» / «PRO · прибыль: …», разрезы PRO/FLASH по
модели, «совет» — ещё и в поводах). Трос: при `pilot.exchange_stop === false` — «трос (в программе)» в плитке, «Трос (в
программе)» в «Сейчас», `pilotLines().hardSoft` → подпись линии и легенды графика, бейдж `[data-exsoft]` «трос в программе» с
предупреждением «при падении программы позиция без защиты — включается в Ключах»; `true` — как было. Приказ `exec.do ===
'WAIT'` — карточка «ВНЕ РЫНКА — ждём: wait_for» без уровней входа (`orderHtml`, `execToText`, `sideRu('WAIT')` = «ВНЕ РЫНКА»),
в «Сейчас» — «вне рынка — ждём: …», фаза `idle` — «Жду план». Модал «Ключи»: блок `#k-settings` — чекбокс «Трос на бирже»
(`#k-exstop`, бейдж `#k-exstop-st`, подсказка с предупреждением) и сегмент «Узлы у денег» (`#k-money` PRO/FLASH);
`settingsForm(state.settings)` из `applyState`, `settingsSave(body, what)` → `POST /api/v5/keys {exchange_stop}` |
`{money_model}` → `settings` из ответа, `kMsg` «Сохранено: …», `refreshState()`; без `settings` в state — переключатели
заперты («сервер не сказал»). Демо `?demo=1`: `settings` в state, поля 5.4.1 в пилоте, переключатели в памяти.
`tests/test_frontend.cjs` ×18 (+ «весь скрипт разбирается», склейка `refreshState`, поздний `pollMission`);
Playwright — `pw_541.py` (подмена `status().pilot` полями контракта и `exec.do='WAIT'`, переключатели на моке, демо,
390/1440, обе темы, 0 ошибок консоли).
