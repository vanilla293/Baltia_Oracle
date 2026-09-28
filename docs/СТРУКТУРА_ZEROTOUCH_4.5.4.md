# АРХИВ `pythia_4.5.4_zerotouch.zip` — из чего он состоит (инструкция для нового агента)

Это описание исходного архива, который владелец передал агенту 18.09.2026 как
отправную точку для v5. Только устройство, без оценок. Если тебе дали один этот
архив без репозитория — здесь всё, что нужно, чтобы понять его за один проход.

## 1. Что это

**ОРАКУЛ // ПИФИЯ 4.5.4 «zero-touch»** — локальный торговый терминал для Мосбиржи.
Один процесс на Python (FastAPI + WebSocket) отдаёт тёмный однофайловый
интерфейс, собирает досье инструмента (Tinkoff Invest API, фолбэк MOEX ISS),
новости (RSS), погоду, небо (эфемериды JPL), гоняет всё через DeepSeek
(конвейер «распределитель → фон → аналитик → критик → синтез → шифровка») и
исполняет сделки через Tinkoff двумя машинами на выбор: **автопилот**
(оракульская петля по стакану и ленте) и **ИИ-пилот** (приказ шифровщика →
вход на максимум → 30-минутные перепроверки). Название «zero-touch» — режим
«ввёл ключи — работает само». Версия в `backend/config.py`: `APP_VERSION = "4.5.4"`,
порт 8799.

Размер: 154 файла, ~3.5 МБ распакованных (из них `NEXT_CHAT.md` 376 КБ,
`frontend/index.html` 261 КБ, `backend/aether.py` 190 КБ, `lightweight-charts.js`
164 КБ). Внутри есть `__pycache__` (мусор сборки, игнорировать).

## 2. Три файла с искажёнными именами

В архиве три файла с именами вида `#U2568#U0427…`: имена в кириллице были
закодированы cp866 → UTF-8 при упаковке. Раскодировка: заменить `#UXXXX` на
символ Unicode, затем `.encode("cp866").decode("utf-8")`. Настоящие имена:

| В архиве (начало)                         | Настоящее имя               | Что это |
|-------------------------------------------|-----------------------------|---------|
| `#U2568#U0427#U2568#U0420#U2568#U042f#U2568#U0433#U2568#U0431#U2568#U042a.txt` | `ЗАПУСК.txt` | инструкция запуска 3.17 «Хищник» (проверка, панель, ключи, сухой прогон, боевой, управление, что внутри решения) |
| `…#U042a_#U2568#U0428#U2568#U0428_#U2568#U042f…txt` | `ЗАПУСК_ИИ_ПИЛОТ.txt` | инструкция запуска ИИ-пилота 4.5.2: тумблер в панели, полный анализ, смена на весь день, сухой прогон, управление по API |
| `…#U0420_#U2568#U042f#U2568#U0430#U2568#U042e…md` | `ЗАЩИТА_ПРОЕКТА.md` | документ для защиты проекта: результаты честного перебора 384 комбинаций × 3 горизонта на данных MOEX, что показывать и что нет |

## 3. Дерево архива

```
pythia_4.5.4_zerotouch/
├─ start.bat, start.sh          запуск: venv → pip → fetch_data → uvicorn backend.server:app :8799
├─ requirements.txt             fastapi, uvicorn, httpx, feedparser, openai, pydantic, websockets, wsproto, python-dotenv, skyfield
├─ build_release.sh             сборщик релизного zip
├─ selfcheck.py                 самопроверка: компиляция, self-тесты модулей, импорт сервера
├─ README.md (130 КБ)           changelog v2.0 → v4.3.0 + быстрый старт, эфемериды, ключи, конвейер, структура
├─ CLAUDE.md (14 КБ)            инструкция агенту эпохи REAL SKY (движок неба, из которого перенесён эфир): законы школы, одна ветка, NEXT_CHAT, чек-лист
├─ NEXT_CHAT.md (376 КБ)        журнал всех сессий 07.2026–08.2026 (см. §8)
├─ BACKTEST_PROTOCOL.md         протокол бэктестов для лаборатории (нули, held-out, что считается результатом)
├─ docs_AETHER_ROADMAP.md       дорожная карта эфирного слоя
├─ docs_FORMULY_EFIRA_v3_0_0.html  формулы эфира v3.0.0 (страница)
├─ docs_KAK_USTROENO_v3.html    «как устроено» 3.x (страница)
├─ ЗАПУСК.txt, ЗАПУСК_ИИ_ПИЛОТ.txt, ЗАЩИТА_ПРОЕКТА.md   (см. §2)
├─ backend/                     53 модуля + lab/ (36 стендов) — §5, §6
│  ├─ TRADER_README.md          описание торговых модулей (брокер, риск, стратегия, исполнение)
│  └─ data/                     пусто; сюда fetch_data кладёт de440s.bsp
├─ frontend/
│  ├─ index.html (261 КБ)       тёмный «терминал спецслужбы», один файл — §7
│  └─ static/                   lightweight-charts.js 4.2.3, icon.svg, icon-180.png, manifest.webmanifest
└─ data/
   ├─ config_user.json          `{}` (ключи стёрты)
   ├─ pythia.db                 SQLite разборов и чата (пустая схема)
   ├─ trader_audit.jsonl        несколько строк аудита dry-ордеров
   ├─ russian_trusted.pem       CA Минцифры для Tinkoff
   └─ mathieu_periods.json      периоды для стендов Матьё
```

## 4. Как запускается и как работает

**Запуск:** `start.bat` / `start.sh` → `.venv` → `pip install -r requirements.txt` →
`python -m backend.fetch_data` (качает `backend/data/de440s.bsp`, ~32 МБ) →
`uvicorn backend.server:app --host 0.0.0.0 --port 8799`. Панель на
`http://localhost:8799`, с телефона в той же сети — адрес из лога.

**Ключи** вводятся в панели (модалка 🔑): ключ DeepSeek **на каждый инструмент**
(`INSTRUMENT_KEYS`, тикер → ключ) или пул аккаунтов (`DEEPSEEK_KEYS`), токен
Tinkoff. Хранятся в `data/config_user.json` (0600). В 4.5.4 по умолчанию
`RESET_ON_START=1`, `WIPE_KEYS_ON_START=1`, `WIPE_ON_EXIT=1`: при каждом старте и
выходе база разборов и ключи стираются («чистый старт»).

**Конвейер полного анализа** (`pipeline.run_pipeline`, кнопка «ЗАПУСТИТЬ ПОЛНЫЙ
АНАЛИЗ», стрим по `WS /ws/analyze` и `WS /ws/events`):
0. СБОР — `collect_dossier` (Tinkoff: цена, статус, стакан 50 уровней, лента за
   90 мин, свечи 5м/1ч/1д, RSI/MACD, ГО/дивиденды/фундаментал; фолбэк MOEX) +
   Вайкофф + Майя + эфир + первоисточник фьючерса; новости RSS за `NEWS_DAYS`
   (2) суток; погода для BR/NG/TTF; астро-карта дня.
1. РАСПРЕДЕЛИТЕЛЬ — DeepSeek раскидывает новости по объектам с выжимками (JSON).
2. ФОН — общий рыночный фон (свободный текст).
3. АНАЛИТИК — думающая модель (`deepseek-v4-pro`, thinking, effort high) по
   досье + новости + фон + погода + астро + эфир + Вайкофф (свободный текст, стрим
   размышления и ответа).
4. КРИТИК — красная команда по анализу.
5. СИНТЕЗ — большой вердикт.
6. ШИФРОВКА — быстрая модель (`deepseek-v4-flash`) → строгий JSON прогноза
   (action LONG/SHORT/FLAT, conviction, forecast_1h/24h/day, уровни, entry,
   invalidation, риски, voice_line); при включённом ИИ-пилоте — ещё блок `exec`
   (do BUY/SELL, entry-засада, take, invalidation, why), который валидирует
   `_sanitize_forecast`.
Результат сохраняется в `data/pythia.db` (`analyses`), чат по объекту — `chat`.

**После разбора — одна из двух торговых машин:**
- Тумблер «⛊ ИИ-ПИЛОТ» выключен (по умолчанию) → **автопилот** (`autopilot.py`):
  петля ~1.5 с: стакан + лента → микроструктура (OBI/CVD/VPIN/Хёрст/Хоукс) →
  ИИ-шифратор раз в час → оракул-свод (машина состояний хаос / сингулярность /
  порог / парламент) → размер (лестница плеча, риск) → лимитная тактика → брокер.
  Первый час после открытия — наблюдение (`PYTHIA_OBSERVE_MIN`).
- Тумблер включён (`config PYTHIA_AI_PILOT=1`, включается только с localhost) →
  **ИИ-пилот** (`ai_pilot.py`): приказ `exec` → засада (цена в 5 тиках → агрессивная
  лимитка в спред, рыночных входов нет) → вход на максимум (фьючерс — депозит/ГО,
  акция — по ставкам риска dlong/dshort) → трос stop-market на бирже → каждые 30
  мин перепроверка (`REVIEW_SYS`: ЖДЁМ / ЗАКРЫТЬ / КУПИТЬ_СЕЙЧАС / ПРОДАТЬ_СЕЙЧАС /
  НОВЫЙ_АНАЛИЗ) → победа/стоп → полный анализ с нуля. Приказ протухает через
  1.5 ч, повторный анализ не чаще раза в 10 мин, сторож раз в минуту поднимает
  пилот после падения, позиция переживает рестарт через `data/aipilot_state.json`.
- Общие тормоза: дневной killswitch (`SessionRisk`), маржевой дозор, ПАНИКА
  (закрыть всё по рынку), изоляция ручных лотов, один депозит = один пилот.
- Брокер `trader_broker.Broker`: режимы `dry` (ордера только в
  `data/trader_audit.jsonl`), `sandbox`, `real` (нужен токен; `PYTHIA_DRY=1`
  принудительно глушит real).

**Переменные окружения 4.5.4:** `PORT`, `PYTHIA_DRY`, `PYTHIA_AI_PILOT`,
`PYTHIA_AUTOSTART` (0 — не поднимать петлю после разбора), `PYTHIA_OBSERVE_MIN`,
`PYTHIA_AIP_REVIEW_SEC` (1800), `PYTHIA_AIP_ARM_TICKS` (5), `PYTHIA_AIP_RESERVE`
(0.02), `PYTHIA_AIP_MAINT` (0.90), `PYTHIA_AIP_PLAN_TTL` (5400),
`PYTHIA_AIP_REANALYZE_GAP` (600), `PYTHIA_EPHEMERIS` (путь к .bsp),
`TRADER_ARM_REAL`/`TRADER_ARM_TOKEN` (старый двойной рубильник 3.17, в 4.5 не
нужен), `RS_NO_BROWSER`. Через `config_user.json`/env: `DEEPSEEK_MODEL`,
`DEEPSEEK_MODEL_FAST`, `DEEPSEEK_BASE_URL`, `AI_REASONING_EFFORT`, `AI_THINKING`,
`AI_MAX_TOKENS`, `NEWS_THINKING`, `NEWS_DAYS`, `NEWS_CACHE_SEC`, `ANALYSIS_TTL_SEC`,
`TINKOFF_TOKEN`, `TINKOFF_PRICE_CACHE_SEC`, `HOST`, `EVENTS_IDLE_SEC`.

**HTTP/WS API `server.py`:** ключи `POST/GET /api/instrument_key`, `POST /api/keys`,
`POST/GET /api/deepseek_keys`; каталог `GET /api/instruments`,
`/api/instruments/search?q=`; `GET /api/modes`, `/api/serverinfo`, `/api/astro`,
`/api/ether?code=&interval=`, `/api/health`, `/api/health/deepseek`,
`GET/POST /api/config`; рынок `GET /api/price?code=`, `/api/candles?code=&interval=&days=`,
`/api/orderbook`, `/api/maya`, `/api/tape`, `/api/select_instrument`,
`/api/directive`, `/api/maya/scan` (POST/GET/DELETE); автопилот
`POST /api/auto/start|stop|panic`, `GET /api/auto/status`; ИИ-пилот
`POST /api/aipilot/enable` (`{on, code}`), `GET /api/aipilot/status`,
`POST /api/aipilot/stop`; разборы `GET /api/analyses`, `/api/running`,
`/api/analysis?code=`, `/api/live?code=`; чат `GET /api/chat/history`,
`POST /api/chat/clear`; `WS /ws/analyze` (запуск конвейера `{codes:[…], mode}`),
`WS /ws/chat`, `WS /ws/events` (шина всех вкладок, ping/ack); `GET /` — панель.

## 5. `backend/` — модули по группам

**ИИ.** `ai.py` — шлюз DeepSeek (SDK openai): `ask`, `ask_json`, `stream`, `chat`,
`health`, ретраи, переключение ключей пула, thinking V4, извлечение JSON, хвост
размышления при пустом ответе. `prompts.py` — доктрина `CORE_DOCTRINE`, персона
«Хищник», промпты распределителя, фона, аналитика, критика, синтеза, шифровщика
(`CODER_SYS` + `CODER_EXEC_ADDON`), перепроверки (`REVIEW_SYS`), чата, `_clip`.
`pipeline.py` — конвейер (см. §4), `collect_dossier`, `render_dossier`,
`_sanitize_forecast`, реестр живых разборов и живой снимок. `memory.py` —
SQLite `data/pythia.db` (`analyses`, `chat`).

**Данные биржи и внешние источники.** `tinkoff.py` — REST Tinkoff Invest:
резолв инструмента, цена, свечи, стакан, лента, статус, консенсус, дивиденды,
фундаментал, тех-индикаторы, ГО фьючерса, счета, портфель, метрики, досье, CA
Минцифры. `moex.py` — MOEX ISS без токена (свечи, цена, дата выпуска, резолв
фьючерса). `moex_farm.py` — фарм реальных данных ISS в базу. `instruments.py` —
курируемый каталог (BR, NG, TTF, SI, EU, CR, GD, SV, MX, RI, SBER, GAZP, LKOH,
ROSN, NVTK, GMKN, TATN, PLZL, YDEX, T, VTBR, MOEX, OZON, MGNT, CHMF, ALRS) +
вся биржа через ISS. `instrument_select.py` — какой контракт фьючерса брать.
`underlying.py` — базовый актив фьючерса. `news.py` — RSS-агрегатор (~20 лент,
`fetch_news`, `fetch_for_ticker`, `render_for_ai`). `weather.py` — open-meteo.
`fetch_data.py` — докачка эфемерид. `config.py` — конфиг, секреты, пул ключей,
changelog в комментариях.

**Анализ рынка.** `wyckoff.py` (структура и VSA), `maya.py` (вакуум стакана),
`maya_scan.py` (наблюдение за стаканом), `microstructure.py` (OBI, CVD, VPIN,
отмены, Хёрст, актор в стакане), `hawkes.py` (критичность потока сделок),
`stream.py` (WebSocket Tinkoff, тики и дельты), `recorder.py` (запись потока в
SQLite), `tagger.py` (теги макро-состояния), `patterns.py` (поиск паттернов в
записях), `bifurcation.py` (Хёрст, Пригожин, EWS, LPPLS, cusp, Ляпунов,
детерминизм, хвост Хилла, `bifurcation_context`), `bif_calendar.py`
(бифуркационный календарь Матьё на эфемеридах), `kuramoto.py` (фазовая сцепка
поводырей, гейт), `oracle.py` (свод голосов как машина состояний), `decision.py`
(единая структура решения), `directive.py` (вердикт направления на час),
`predator.py` (оркестратор четырёх агентов Пылесос/Физик/Патологоанатом/Снайпер).

**Небо и эфир.** `astro.py` (суточная карта по эфемеридам: тела, созвездия,
аспекты, станции, ретро-Меркурий, затмения, фаза Луны; `render_for_ai`,
`short_line`), `realsky3d.py` (88 созвездий IAU, дома), `reactor_sky.py`
(спецификация дня для ИИ), `aether.py` (волна Ψ инструмента, сцепка с ценой,
плиты, потенциал, `compute_context`, `chart_payload`), `aether_deep.py`,
`aether_resonance.py` (Гильберт, интермодуляция, Адлер), `gates.py` (створы —
окна фазы впереди).

**Торговля.** `trader_broker.py` (единственный шлюз ордеров, аудит),
`trader_risk.py` (размер по риску и плечу, `SessionRisk`), `trader_strategy.py`
(машина состояний Фармилы), `execution.py` (нарезка, теневая лимитка, ловля
сброса, выходы), `limits.py` (лимитная тактика), `leverage.py` (лестница плеча),
`arming.py` (взведение по двум ключам), `autopilot.py` (оракульская петля),
`ai_pilot.py` (ИИ-пилот). `TRADER_README.md` — их описание.

**Сервер.** `server.py` — FastAPI, все эндпоинты §4, шина `_Hub`, реестры
`_AUTO` (автопилоты) и `_AIPILOT` (ИИ-пилоты), тумблер режима, сторож, прогревы
на старте, чистый старт/выход.

Каждый модуль имеет self-тест `python -m backend.<модуль>` (фейковый брокер и
ИИ, без сети); `selfcheck.py` в корне гоняет их пакетом.

## 6. `backend/lab/` — стенды (не входят в рантайм)

Эфир и небо: `market_ether_lab.py`, `market_ether_lab2.py`, `psi_regime.py`,
`psi_structure.py`, `psi_split.py`, `psi_shift_null.py`, `plv_null.py`,
`e_surrogate.py`, `e_vs_angles.py`, `fake_sky.py`, `fake_sky_native.py`,
`genesis_lab.py`, `sky_feats.py`, `weight_perm.py`, `ar_nulls.py`, `vol_deep.py`,
`why_oil.py`. Частотный анализ: `morlet.py`, `fast_planets_cwt.py`,
`stochastic_resonance.py`, `mathieu.py`, `mathieu_scan.py`. Нефть и макро:
`oil_fetch.py`, `oil_frs.py`, `macro_calendar.py`. Торговая логика:
`entry_probe.py`, `entry_probe2.py`, `grid_horizons.py`, `grid_search_honest.py`,
`grid_search_v2.py`, `grid_sim_test.py`, `txt_backtester.py`, `calibration.py`,
`forward_test.py`, `pattern_probe.py`, `real_analyze.py`, `rnd_micro.py`,
`market_sim.py`, `predator_bench.py`. Каждый — отдельный скрипт с описанием
гипотезы и протокола в докстринге; результаты записаны в `NEXT_CHAT.md`.

## 7. `frontend/index.html` — тёмный терминал

Один файл (~261 КБ), минифицированный местами; правится только точечным
`str.replace`. Раскладка: шапка (афоризм, персона, астро-строка, эфир, поле,
заряд толпы, легенда, часы МСК с отсчётом до события биржи, токены, ключи);
слева каталог инструментов с поиском и пакетным выбором; центр — полоса
«⛊ ИИ-ПИЛОТ» (тумблер, состояние, P/L, план, до перепроверки, killswitch,
СТОП/ПАНИКА), карточка автопилота, конвейер стадий, интервалы, график
lightweight-charts с уровнями прогноза и слоями эфира, вкладки ДОСЬЕ / АНАЛИЗ /
КРИТИКА / ФОН / МЫШЛЕНИЕ, скачать TXT; справа вкладки ПРОГНОЗ / НОВОСТИ / АСТРО /
ВАКУУМ / ЧАТ; модалки ключей, журнала, будильников, горячих клавиш; мобильная
навигация; полноэкранный график; загрузочный экран. Связь: `WS /ws/analyze`
(запуск), `WS /ws/events` (стрим всех вкладок, ack каждые 200 кадров, ping 25 с),
опрос `/api/price` каждые 3 с, `/api/running`, `/api/aipilot/status`,
`/api/auto/status`, `/api/analysis`, `/api/live`. Есть светлая тема как
переключатель. Иконки и manifest — PWA на телефон.

## 8. Документы

- `README.md` — changelog по версиям (v2.0 → v4.3.0 «PREDATOR»), затем разделы
  «Быстрый старт», «Эфемериды», «Настройка ключей», «Как это работает
  (конвейер)», «Структура», «Замечания» (в файле два экземпляра этих разделов
  от разных версий).
- `NEXT_CHAT.md` — журнал сессий, свежее сверху: «СМЕНА» 4.5.3, «ТУМБЛЕР» 4.5.2,
  «ИИ-ПИЛОТ» 4.5.0/4.5.1 (веер агентов, 90 находок), «ZERO-TOUCH» и «АБСОЛЮТ»
  4.4.0, «PREDATOR» 4.3.0, «СЫРАЯ ДАТА» 4.2.0, «ХИЩНИК» 4.1.0, «ОРАКУЛ-ЯДРО»
  4.0.0, «РЕАКТОР», сессии эфира и REAL SKY (3D-небо, диф-прогоны), «ДОМИНУС»
  (ресторанная ветка того же движка), «НЕЛИНЕЙНАЯ МАТЕМАТИКА», «ЭФИР РАЗОБРАН ДО
  ДНА», далее сессии v17.x движка неба; в конце — бот-эра, спасённый бэклог,
  законы проекта, состояние репозитория, натал владельца для расчётов.
- `CLAUDE.md` — правила агента эпохи REAL SKY (одна ветка, читать NEXT_CHAT,
  законы школы: аксиома мгновенности, честная рамка 🔵🟡⚫, ABSOLUTE OFFLINE, NO
  DUMMIES, аддитивность, единые константы; грабли; чек-лист «готово»).
  Упоминаемые там `kernel.py`, `core.py`, `templates/v2/`, `lavka/` — из движка
  неба, в архиве Пифии их нет.
- `BACKTEST_PROTOCOL.md`, `docs_AETHER_ROADMAP.md`, `docs_FORMULY_EFIRA_v3_0_0.html`,
  `docs_KAK_USTROENO_v3.html`, `ЗАПУСК.txt`, `ЗАПУСК_ИИ_ПИЛОТ.txt`,
  `ЗАЩИТА_ПРОЕКТА.md` — см. §2 и §3.

## 9. Что из этого живёт в v5

В репозитории v5 весь `backend/` 4.5.4 оставлен как библиотека (данные биржи,
небо, эфир, Курамото, бифуркации, брокер, риск, `AIPilot`), `lab/` — без
изменений, старый `frontend/index.html` заменён новым, старые документы и
скрипты вынесены в `archive/docs_4.5/`, сам архив лежит в
`archive/pythia_4.5.4_zerotouch.zip`. Карта v5 — `docs/СТРУКТУРА_ПРОЕКТА.md`.
