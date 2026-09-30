// Run with: node --test tests/test_frontend.cjs
// Exercise the shipped inline JavaScript with controlled, out-of-order responses.
// No browser, server, broker, AI provider, or network connection is used.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '../frontend/index.html'), 'utf8');
const section = (start, end) => {
  const a = html.indexOf(start), b = html.indexOf(end, a + start.length);
  assert.ok(a >= 0 && b > a, `Source boundaries found: ${start}`);
  return html.slice(a, b);
};
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; };
function element() {
  const classes = new Set();
  return {value: '', textContent: '', innerHTML: '', style: {}, dataset: {}, disabled: false, attrs: {},
    classList: {add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x), toggle: (x, on) => on ? classes.add(x) : classes.delete(x)},
    setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; }, dispatchEvent() {},
  };
}
function context(extra = {}) {
  const els = new Map(), get = id => { if (!els.has(id)) els.set(id, element()); return els.get(id); };
  const ctx = vm.createContext({
    console, setTimeout, clearTimeout, Event: class {},
    $: get, $$: () => [], document: {addEventListener() {}, body: element()},
    S: {mission: {ticker: 'SBER'}, screen: 'mission'},
    num: v => v == null || v === '' || isNaN(+v) ? null : +v,
    esc: String, toast() {}, LS: {get: (_key, value) => value}, DEMO: false,
    ...extra,
  });
  return {ctx, get, run: src => vm.runInContext(src, ctx)};
}

// ═══ 5.4.1 (логика 5.4.0 в index.html): весь скрипт панели разбирается; параллельные полные обновления состояния —
// один запрос с восстановлением после сбоя; поздний опрос миссии не затирает более свежий снимок того же тикера.
const scriptBlock = (() => {   // единственный встроенный <script> без src — вся логика панели
  const re = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g; const found = []; let m;
  while ((m = re.exec(html))) found.push(m[1]);
  assert.equal(found.length, 1, 'ровно один встроенный <script> без src');
  return found[0];
})();
test('whole application script parses before any browser interaction', () => {
  assert.doesNotThrow(() => new vm.Script(scriptBlock, {filename: 'index.html#script'}));
});

test('concurrent full-state refreshes share one request and recover after failure', async () => {
  const pending = [], applied = [];
  const c = context({api: () => { const d = deferred(); pending.push(d); return d.promise; },
    applyState: (state, missionContext) => applied.push({state, missionContext})});
  c.run(section('let _stateRefresh = null;', 'const ROUTE_RU ='));
  const first = c.run('refreshState()'), repeat = c.run('refreshState()');
  assert.equal(first, repeat);
  await Promise.resolve();
  assert.equal(pending.length, 1);
  pending[0].resolve(null); await first;
  c.ctx.S.mission.revision = 2;
  const retry = c.run('refreshState()'); await Promise.resolve();
  pending[1].resolve({version: 2}); await retry;
  assert.equal(applied.length, 1);
  assert.equal(applied[0].state.version, 2);
  assert.equal(applied[0].missionContext.revision, 2);
});

test('late mission poll cannot overwrite a newer full-state snapshot of the same ticker', async () => {
  const d = deferred(); let applied = 0;
  const c = context({api: () => d.promise, applyMissionStatus: () => ++applied});
  c.run(section('let _missionPoll = null;', 'function applyMissionStatus'));
  const old = c.run('pollMission()');
  c.ctx.S.mission.revision = 1;
  c.ctx.S.mission.status = {phase: 'stopped'};
  d.resolve({ticker: 'SBER', phase: 'in_position'}); await old;
  assert.equal(applied, 0);
  assert.equal(c.ctx.S.mission.status.phase, 'stopped');
});

const candleSource = section('let _candlePending = null;', 'function onCross(');
function candleContext() {
  const pending = [], writes = [], series = {setData: rows => writes.push(rows)};
  const c = context({
    api: url => { const d = deferred(); pending.push({url, ...d}); return d.promise; },
    TZ_SHIFT: () => 10800, IV_DAYS: {'15m': 5, '1h': 20}, chartTheme: () => ({}), volData: () => [],
    updateLegend() {}, applyMarkers() {}, applyPriceLines() {}, padRight() {}, fmtNum: String, fmtK: String,
    S: {series, vol: {setData() {}}, emas: {}, chartTicker: 'SBER', interval: '15m',
      showVol: false, showEma: false, mission: {}, chart: {timeScale: () => ({fitContent() {}})}},
  });
  c.run(candleSource); return {...c, pending, writes};
}
const candle = (close = 101) => ({t: '2026-09-24T10:00:00Z', o: 100, h: 110, l: 90, c: close, v: 5});

test('late candle response cannot overwrite the selected interval', async () => {
  const c = candleContext();
  const old = c.run('loadCandles(true)');
  c.ctx.S.interval = '1h';
  const current = c.run('loadCandles(true)');
  c.pending[1].resolve({candles: [candle(105)], source: 'fixture'}); await current;
  c.pending[0].resolve({candles: [candle(99)], source: 'fixture'}); await old;
  assert.equal(c.ctx.S.lastBar.close, 105);
  assert.match(c.get('#chart-note').textContent, /1h/);
  assert.equal(c.ctx.S.candleKey, 'SBER:1h');
  assert.equal(c.get('#chart').style.opacity, '');
});

test('slow candle requests survive repeated polling and preserve a requested fit', async () => {
  const c = candleContext(), load = c.run('loadCandles(false)');
  await c.run('loadCandles(false)'); await c.run('loadCandles(true)'); await c.run('loadCandles(false)');
  assert.equal(c.pending.length, 1);
  c.pending[0].resolve({candles: [candle(105)], source: 'fixture'}); await load;
  assert.equal(c.ctx.S.lastBar.close, 105); assert.equal(c.ctx.S.needFit, true);
  const next = c.run('loadCandles(false)'); assert.equal(c.pending.length, 2);
  c.pending[1].resolve({candles: [candle(106)], source: 'fixture'}); await next;
  assert.equal(c.ctx.S.lastBar.close, 106);
});

test('invalid OHLC rows do not crash or reach the chart; duplicate bars use the latest value', async () => {
  const c = candleContext();
  const normalized = c.run(`cleanCandles(${JSON.stringify([
    candle(101), {...candle(), h: 'invalid'}, {...candle(), c: null},
    {...candle(), h: 80}, {...candle(), t: 'invalid'}, candle(106),
  ])})`);
  assert.equal(normalized.length, 1); assert.equal(normalized[0].close, 106);
  const load = c.run('loadCandles(true)');
  c.pending[0].resolve({candles: [{...candle(), l: 'bad'}]}); await load;
  assert.equal(c.ctx.S.bars.length, 0);
  assert.match(c.get('#chart-note').textContent, /некорректные данные/);
});

test('status polling ignores the previous instrument and coalesces overlapping requests', async () => {
  const pending = [], applied = [];
  const c = context({api: () => { const d = deferred(); pending.push(d); return d.promise; }, applyMissionStatus: st => applied.push(st)});
  c.run(section('let _missionPoll = null;', 'function applyMissionStatus('));
  const old = c.run('pollMission()'); await c.run('pollMission()');
  assert.equal(pending.length, 1);
  c.ctx.S.mission.ticker = 'GAZP'; const current = c.run('pollMission()');
  pending[1].resolve({ticker: 'GAZP', phase: 'armed'}); await current;
  pending[0].resolve({ticker: 'SBER', phase: 'stopped'}); await old;
  assert.deepEqual(applied, [{ticker: 'GAZP', phase: 'armed'}]);
});

test('a late price for another instrument cannot alter current price or candle', async () => {
  const d = deferred(); let calls = 0;
  const c = context({api: () => { ++calls; return d.promise; }, bPxFlash() { throw Error('stale quote rendered'); }});
  c.run(section('let _pricePoll = null;', '/* ───────── экран 4: журнал'));
  const load = c.run('tickPrice()'); await c.run('tickPrice()');
  assert.equal(calls, 1);
  c.ctx.S.mission.ticker = 'GAZP'; d.resolve({price: 100}); await load;
});

test('late trades from a previous mission do not replace its successor', async () => {
  const d = deferred(); let calls = 0;
  const c = context({api: () => { ++calls; return d.promise; }, fmtPnl() { throw Error('stale trades rendered'); }});
  c.run(section('let _tradesPending = null;', 'function tradesTable('));
  const load = c.run("loadMissionTrades('SBER')");
  await c.run("loadMissionTrades('SBER')"); assert.equal(calls, 1);
  c.ctx.S.mission.ticker = 'GAZP'; d.resolve({trades: [{id: 1}]}); await load;
  assert.equal(c.get('#m-trades-sum').textContent, '');
});

function searchContext() {
  const pending = [], c = context({api: url => { const d = deferred(); pending.push({url, ...d}); return d.promise; }});
  c.run(section('let _srchT = null,', "$('#btn-launcher-toggle').onclick"));
  return {...c, pending};
}
test('search results follow the current query even when responses arrive out of order', async () => {
  const c = searchContext();
  c.get('#tk-search').value = 'SB'; const old = c.run("searchTk('SB')");
  c.get('#tk-search').value = 'GA'; const current = c.run("searchTk('GA')");
  c.pending[1].resolve({items: [{code: 'GAZP', name: 'Газпром'}]}); await current;
  c.pending[0].resolve({items: [{code: 'SBER', name: 'Сбербанк'}]}); await old;
  assert.match(c.get('#tk-dd').innerHTML, /GAZP/);
  assert.doesNotMatch(c.get('#tk-dd').innerHTML, /SBER/);
});
test('closing search prevents a pending result from reopening the dropdown', async () => {
  const c = searchContext(); c.get('#tk-search').value = 'SB';
  const load = c.run("searchTk('SB')"); c.run('closeTkDD()');
  c.pending[0].resolve({items: [{code: 'SBER'}]}); await load;
  assert.equal(c.get('#tk-dd').classList.contains('hidden'), true);
  assert.equal(c.get('#tk-search').attrs['aria-expanded'], 'false');
});
test('failed catalog load is retryable and is not cached as an empty catalog', async () => {
  const c = searchContext(); let load = c.run('showGroups()');
  c.pending[0].resolve(null); await load;
  assert.equal(c.ctx.S.instruments, undefined);
  assert.match(c.get('#tk-dd').innerHTML, /недоступен/);
  load = c.run('showGroups()'); c.pending[1].resolve({groups: {Акции: [{code: 'SBER'}]}}); await load;
  assert.match(c.get('#tk-dd').innerHTML, /SBER/);
});

test('rejected mission launch preserves the active mission', async () => {
  const c = context({call: async () => ({ok: false, note: 'busy'}),
    renderMission() { throw Error('rejected launch rendered'); }});
  c.ctx.S.mission.status = {phase: 'armed'};
  c.get('#tk-search').value = 'GAZP';
  c.run(section("$('#btn-launch').onclick = async () =>", 'let _missionPoll = null;'));
  await c.get('#btn-launch').onclick();
  assert.equal(c.ctx.S.mission.ticker, 'SBER');
  assert.equal(c.ctx.S.mission.status.phase, 'armed');
  assert.equal(c.get('#btn-launch').disabled, false);
});

function chatContext(api) {
  const c = context({api, fmtDT: String, confirmBox: async () => true, call: async () => null});
  c.run(section('const Chat = {', 'function chatMd(') + '\nthis.Chat = Chat;');
  const bubbles = [];
  Object.assign(c.ctx.Chat, {bubble: (_cls, text) => { bubbles.push(text); return element(); }, status() {}, scroll() {}});
  return {...c, bubbles, chat: c.ctx.Chat};
}
test('repeated Enter cannot submit twice while the first POST is pending', async () => {
  const d = deferred(); let calls = 0;
  const c = chatContext(() => { ++calls; return d.promise; });
  c.get('#chat-in').value = 'first'; const send = c.chat.send();
  c.get('#chat-in').value = 'second'; await c.chat.send();
  assert.equal(calls, 1); assert.equal(c.get('#chat-in').value, 'second');
  d.resolve({ok: true, run_id: 'r1', model: 'pro'}); await send;
  assert.equal(c.chat.run, 'r1'); assert.equal(c.chat.sending, false);
});
test('a failed chat send preserves both the question and a new draft, including HTTP busy', async () => {
  const d = deferred(), c = chatContext(() => d.promise);
  c.get('#chat-in').value = 'question'; const send = c.chat.send();
  c.get('#chat-in').value = 'new draft';
  d.resolve({ok: false, busy: true, note: 'busy'}); await send;
  assert.equal(c.get('#chat-in').value, 'question\n\nnew draft');
  assert.equal(c.chat.sending, false);
});
test('failed history deletion preserves the visible history', async () => {
  const c = chatContext(async () => ({})); let renders = 0;
  c.chat.render = () => { ++renders; }; await c.chat.clearHistory();
  assert.equal(renders, 0); assert.equal(c.chat.clearing, false);
});
test('history arriving after a send cannot erase the new conversation', async () => {
  const d = deferred(), c = chatContext(() => d.promise); let renders = 0;
  c.chat.render = () => { ++renders; }; const load = c.chat.load();
  ++c.chat.revision; d.resolve({items: []}); await load;
  assert.equal(renders, 0);
});
test('final chat snapshot uses the completed run text, even after the next answer starts', async () => {
  const d = deferred(), c = chatContext(() => d.promise), bubble = element(), meta = element();
  bubble.querySelector = () => meta;
  c.chat.cur = bubble; c.chat.curText = 'short'; c.chat.run = 'old';
  let restored; c.chat.setText = (_bubble, text) => { restored = text; };
  c.chat.finish('old'); c.chat.curText = 'new answer '.repeat(100);
  d.resolve({texts: {answer: 'short completed answer'}, meta: {model: 'pro'}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(restored, 'short completed answer');
});

// ═══ 5.4.4 «честная панель» (воля владельца: «если реально не запущен пилот — то не мигает, если запущен — то работает»):
// одно правило pilotRun(status) для точки фазы, пилюли «Пилот», полосы степпера и мини-шапки; события пилота из шины —
// в хронику, не в вечный progress; плашки связи/отказа/маржи; метки решений перепроверки; «нет тиков» вместо молчания.
const NOW = 1_800_000_000;
const HM = new Intl.DateTimeFormat('ru-RU', {timeZone: 'Europe/Moscow', hour: '2-digit', minute: '2-digit'});
function honestContext(extra = {}) {
  const c = context({fmtNum: String, fmtInt: String, fmtLeft: m => Math.round(m) + ' мин', _hm: HM,
    ST: {mission: {}}, renderStepper() {}, pollMission() {}, ...extra});
  c.run(section('const DEC_RU =', 'const mmOf ='));
  c.run(section('// ═══ 5.4.4 «честная панель»', 'const HANDOFF_KIND = {'));
  return c;
}
const armed = (pilot, extra) => ({ticker: 'AFLT', phase: 'armed', live: true, pilot: {state: 'ЗАСАДА', mode: 'real', ...pilot}, ...extra});

test('only a really ticking pilot animates; stopped, absent, dead-feed and closed-market pilots stay still', () => {
  const c = honestContext(), run = st => c.ctx.pilotRun(st, NOW);
  let r = run(armed({ticking: true, loop_alive: true, feed: {ok: true, kind: 'ok'}}));
  assert.equal(r.on, true); assert.equal(r.kind, 'ticking'); assert.equal(c.ctx.phaseStill(r), '');
  r = run(armed({ticking: false, loop_alive: true, last_tick_ts: NOW - 700, feed: {ok: false, kind: 'auth', reason: 'токен отозван (401)'}}));
  assert.equal(r.on, false); assert.equal(r.level, 'err'); assert.equal(r.text, 'пилот не тикает: токен отозван (401)');
  assert.equal(c.ctx.phaseStill(r), ' still halt');
  r = run(armed({ticking: false, loop_alive: true, last_tick_ts: NOW - 300, feed: {ok: true, kind: 'ok'}}));
  assert.equal(r.on, false); assert.equal(r.level, 'warn'); assert.equal(r.text, 'пилот не тикает: нет тиков 5 мин');
  assert.equal(c.ctx.phaseStill(r), ' still stale');
  assert.equal(run(armed({ticking: true, loop_alive: false})).on, false);   // петля мертва — флаг ticking не спасает
  assert.equal(run(null).kind, 'none');
  assert.equal(run(armed(null, {pilot: null})).kind, 'none');
  for (const phase of ['stopped', 'panic', 'error']) assert.equal(run(armed({ticking: true}, {phase})).on, false);
  assert.equal(run(armed({ticking: true}, {live: false})).on, false);   // фаза от прошлого, задачи пилота нет
  const closed = run(armed({ticking: true}, {phase: 'closed'}));
  assert.equal(closed.kind, 'closed'); assert.equal(closed.on, false); assert.equal(c.ctx.phaseStill(closed), ' still');
  assert.equal(run({phase: 'council', pilot: null}).on, true);   // идёт совет — живой прогон
  // старый бэкенд без ticking: по tick_ts; без единого тика дольше пары минут — «не тикает»
  assert.equal(run(armed({tick_ts: NOW - 5})).on, true);
  r = run(armed({tick_ts: NOW - 600})); assert.equal(r.on, false); assert.equal(r.legacy, true);
  r = run(armed({tick_ts: null, uptime_h: 1.2})); assert.equal(r.on, false); assert.equal(r.text, 'пилот не тикает: нет тиков — ни одного с запуска');
});

test('Pilot pill follows status: heartbeat while ticking, PRO minutes while reviewing, still/halt when not ticking', () => {
  const c = honestContext(), M = c.ctx.ST.mission, ms = NOW * 1000;
  const settle = (st, live = false) => c.ctx.stepperSettle(st, c.ctx.pilotRun(st, NOW), live, NOW);
  Object.assign(M, {news: {status: 'start', at: ms - 30000}, review: {status: 'start', at: ms - 30000}, profit: {status: 'start', at: ms - 30000},
    guard: {status: 'start', at: ms - 2000}, pilot: {status: 'progress', at: ms - 30000}});
  const st = armed({ticking: true, review_busy: false, profit: {busy: false}, guard: {busy: false}});
  assert.equal(settle(st), true);
  assert.equal(M.news.status, 'done');     // отчёт 2, A2: CancelledError — ни done, ни error; совет не идёт — стадия закрыта
  assert.equal(M.review.status, 'done');   // A4: review_busy false — перепроверка не идёт
  assert.equal(M.profit.status, 'done');   // A3: ранний return — profit.busy false
  assert.equal(M.guard.status, 'start');   // событие свежее 8 с — статус мог его ещё не увидеть
  assert.equal(M.pilot.status, 'tick'); assert.equal(M.pilot.note, 'работает');   // A1: не вечный progress
  settle(armed({ticking: true, review_busy: true, review_started_ts: NOW - 180}));
  assert.equal(M.pilot.status, 'progress'); assert.equal(M.pilot.note, 'PRO думает 3 мин');
  settle(armed({ticking: false, feed: {ok: false, kind: 'auth', reason: 'токен отозван'}}));
  assert.equal(M.pilot.status, 'halt'); assert.equal(M.pilot.note, 'не тикает');
  settle(armed({ticking: false, last_tick_ts: NOW - 100}));
  assert.equal(M.pilot.status, 'still');
  settle(armed({ticking: true}, {phase: 'stopped'}));
  assert.equal(M.pilot.status, ''); assert.equal(M.pilot.note, 'остановлен');
  assert.equal(settle(armed({ticking: true}, {phase: 'stopped'})), false);   // без изменений — без перерисовки
  // идёт совет: пилюлю и стадии ведут события прогона
  M.news = {status: 'start', at: ms - 30000}; M.pilot = {status: 'start', at: ms};
  settle({phase: 'council', pilot: null}, true);
  assert.equal(M.news.status, 'start'); assert.equal(M.pilot.status, 'start');
});

test('stepper bar flows only while something runs; a ticking pilot counts as reached and shows its note', () => {
  const host = element(), det = element();
  const c = context({ST: {mission: {}}, STEPS: {mission: ['exec', 'pilot']}, STAGE_RU: {exec: 'Приказ', pilot: 'Пилот'}, bScrollTo() {},
    $: id => id === '#stepper-mission' ? host : id === '#detail-mission' ? det : element()});
  c.run(section('function counterText(stage, c) {', "document.addEventListener('click', e => {   // действия из блока ошибки стадии"));
  c.run(section('function put(sel, html) {', '/* ───────── навигация'));
  c.ctx.ST.mission = {exec: {status: 'done'}, pilot: {status: 'tick', note: 'работает', tip: 'пилот работает'}};
  c.run("renderStepper('mission')");
  assert.doesNotMatch(host.innerHTML, /progress live/); assert.match(host.innerHTML, /width:100%/);
  assert.match(host.innerHTML, /class="stg tick"/); assert.match(host.innerHTML, /· работает/);
  c.ctx.ST.mission.pilot = {status: 'progress', note: 'PRO думает 3 мин'};
  c.run("renderStepper('mission')");
  assert.match(host.innerHTML, /progress live/);
  c.ctx.ST.mission.pilot = {status: 'halt', note: 'не тикает'};
  c.run("renderStepper('mission')");
  assert.doesNotMatch(host.innerHTML, /progress live/); assert.match(host.innerHTML, /class="d">!</);
  assert.doesNotMatch(det.innerHTML, /stg-err/);   // не тикает — не «ошибка стадии» с кнопками пересмотра
});

test('pilot bus events after the council go to the chronicle and never start a new run or pin the pill', () => {
  const renders = [];
  const c = honestContext({renderStepper: k => renders.push(k), SCOPE_KEY: {mission: 'mission'}, STAGE_RU: {}, CHAIR_STAGES: ['analysis'],
    CH: {mission: {live: false, stages: {}}}, Chat: {onStage() {}}, Xp: {onStage() {}},
    mkChairs() { throw Error('pilot event started a new run'); }, setSub() { throw Error('pilot event lit the tab'); },
    renderChairs() {}, updateChair() {}, loadRun() {}, refreshState() {}, pilotCountdown() {}});
  c.run(section('function onStage(ev) {', 'function onText(ev) {'));
  Object.assign(c.ctx.S, {runs: {mission: 'r1'}}); c.ctx.S.mission.ticker = 'SBER';
  c.ctx.ST.mission = {pilot: {status: 'tick', note: 'работает'}};
  const ev = o => c.ctx.onStage({type: 'v5', scope: 'mission', ticker: 'SBER', ...o});
  ev({run_id: 'r1', stage: 'pilot', status: 'progress', detail: 'ВОШЁЛ: лонг 10 лот @284.6', data: {fill: {new: true}}});
  ev({run_id: 'r1', stage: 'pilot', status: 'progress', detail: 'ВОШЁЛ: лонг 10 лот @284.6'});   // повтор — одна запись
  assert.equal(c.ctx.ST.mission.pilot.status, 'tick');
  assert.equal(c.ctx.S.mission.pev.length, 1); assert.equal(c.ctx.S.mission.pev[0].fill, true);
  assert.equal(c.ctx.ST.mission.pilot.detail, 'ВОШЁЛ: лонг 10 лот @284.6');
  c.ctx.S.mission.reviewAt = Date.now() + 1800e3;   // review_ts переставлен при старте перепроверки — это следующая плановая, не текущая
  ev({run_id: 'r0', stage: 'review', status: 'start', detail: 'перепроверка'});   // run_id прошлого совета — не новый прогон
  assert.equal(c.ctx.ST.mission.review.status, 'start'); assert.equal(c.ctx.ST.mission.pilot.status, 'tick');
  assert.ok(c.ctx.S.mission.reviewBusyAt > 0); assert.equal(c.ctx.S.mission.reviewAt, null);   // отсчёт — «PRO перепроверяет… N»
  ev({run_id: 'r0', stage: 'review', status: 'done', detail: 'ДЕРЖАТЬ: тест'});
  assert.equal(c.ctx.S.mission.reviewBusyAt, null);
  ev({run_id: 'r0', stage: 'pilot', status: 'progress', ticker: 'GAZP', detail: 'чужой тикер'});
  assert.equal(c.ctx.S.mission.pev.length, 1);
  assert.ok(renders.length >= 2);
});

test('chronicle: a level/wake or market-open cue is not «to the council»; unknown kinds are a neutral cue', () => {
  const c = context();
  c.run(section('const HANDOFF_KIND = {', 'function pilotLines(st) {'));
  assert.equal(c.run('HANDOFF_KIND.wait_level[1]'), 'уровень → PRO');
  assert.equal(c.run('HANDOFF_KIND.open[1]'), 'открытие → PRO');
  assert.equal(c.run("(HANDOFF_KIND['новый_вид'] || HANDOFF_OTHER)[1]"), 'повод');
  assert.equal(c.run('HANDOFF_KIND.council[1]'), 'совету');
  const html = section('const evAll = [', '].sort((a, b) => (b.ts || 0) - (a.ts || 0));');
  assert.match(html, /HANDOFF_KIND\[h\.kind\] \|\| HANDOFF_OTHER/);
  assert.doesNotMatch(html, /HANDOFF_KIND\[h\.kind\] \|\| HANDOFF_KIND\.council/);
});

test('review decisions show the label the model saw: ambush, HOLD in position, silence in words', () => {
  const c = honestContext(), L = r => c.ctx.revLabel(r);
  assert.equal(L({choice: 'КУПИТЬ_СЕЙЧАС', label: 'КУПИТЬ — засада откат @296.6'}), 'КУПИТЬ — засада откат @296.6');
  assert.equal(L({choice: 'ЖДЁМ', label: 'ДЕРЖАТЬ', in_pos: true}), 'ДЕРЖАТЬ');
  assert.equal(L({choice: 'ЖДЁМ', in_pos: true}), 'ДЕРЖАТЬ');   // старый бэкенд без label
  assert.equal(L({choice: 'ЖДЁМ', in_pos: false}), 'ЖДЁМ');
  assert.equal(L({choice: 'КУПИТЬ_СЕЙЧАС'}), 'КУПИТЬ');
  assert.equal(L({choice: 'ПРОДАТЬ_СЕЙЧАС', entry: 301.5, entry_kind: 'прорыв'}), 'ПРОДАТЬ — засада прорыв @301.5');
  assert.equal(L({choice: 'КУПИТЬ_СЕЙЧАС', entry: 301.5, entry_kind: 'сейчас'}), 'КУПИТЬ');
  assert.match(L({choice: 'НЕТ_ОТВЕТА'}), /решения не было/);
  assert.match(L({choice: 'ЖДЁМ', label: 'НЕ_РАЗОБРАН'}), /решения не было/);
});

test('pilot card alerts: dead broker link red, flaky link amber, exchange refusal, exhausted margin, wake alarm', () => {
  const c = honestContext();
  const A = pilot => { const st = armed(pilot); return c.ctx.pilotAlerts(st, c.ctx.pilotRun(st, NOW), NOW); };
  let a = A({ticking: false, feed: {ok: false, kind: 'auth', reason: 'токен отозван (401)'}});
  assert.equal(a.length, 1); assert.equal(a[0].level, 'err'); assert.equal(a[0].text, 'Т-Банк: токен отозван (401)');
  a = A({ticking: true, feed: {ok: false, kind: 'network', reason: 'таймаут чтения'}});
  assert.equal(a[0].level, 'warn'); assert.equal(a[0].text, 'Т-Банк: таймаут чтения');
  a = A({ticking: true, feed: {ok: true, kind: 'ok'}, broker_refusal: {what: 'вход', code: 30042, text: 'недостаточно средств', count: 3, ts: NOW - 60},
    account_limits: {buy_lots: 0, sell_lots: 195, note: ''}});
  const ref = a.find(x => x.kind === 'refusal'), mar = a.find(x => x.kind === 'margin');
  assert.equal(ref.text, `Биржа отклонила вход: недостаточно средств (3 раза, ${HM.format(new Date((NOW - 60) * 1000))})`); assert.equal(ref.level, 'err');
  assert.equal(mar.text, 'Покупка недоступна: маржа исчерпана · продажа до 195 лот');
  assert.equal(A({ticking: true, feed: {ok: true}, broker_refusal: null, account_limits: {buy_lots: 12, sell_lots: 40}}).length, 0);
  a = A({ticking: false, last_tick_ts: NOW - 200, feed: {ok: true}});
  assert.equal(a.length, 1); assert.equal(a[0].kind, 'tick'); assert.match(a[0].text, /^пилот не тикает: нет тиков/);
  assert.equal(c.ctx.nRaz(1), '1 раз'); assert.equal(c.ctx.nRaz(22), '22 раза'); assert.equal(c.ctx.nRaz(12), '12 раз');
  // «Tinkoff» в шапке — по связи живого пилота, а не «токен задан» (отчёт 1: зелёный при отозванном токене)
  const tk = feed => c.ctx.tkLink({app: {tinkoff: true}, mission: {missions: {AFLT: armed({feed})}}});
  assert.equal(tk({ok: false, kind: 'auth', reason: 'токен отозван'}).cls, 'bad');
  assert.equal(tk({ok: false, kind: 'network', reason: 'таймаут'}).cls, 'off');
  assert.equal(tk({ok: true, kind: 'ok'}).cls, 'ok');
  assert.equal(tk({ok: false, kind: 'closed'}).cls, 'ok');   // рынок закрыт — не поломка связи
  assert.equal(c.ctx.tkLink({app: {tinkoff: false}}).cls, 'bad');
  assert.match(c.ctx.wakeHtml({level: 296.6, dir: 'up', why: 'пробой 296.6', ref: 295, ts: NOW - 60, fired: null}), /будильник у 296\.6 \(вверх\)/);
  assert.match(c.ctx.wakeHtml({level: 290, dir: 'down', fired: NOW}), /будильник у 290 \(вниз\) · сработал/);
  assert.equal(c.ctx.wakeHtml(null), '');
});

test('problems panel says «нет тиков» instead of silence and does not repeat what the server said', () => {
  const c = honestContext(), mem = {};
  const ms = {ticker: 'AFLT', phase: 'in_position', live: true, pilot: {state: 'В_ПОЗИЦИИ', mode: 'real', price_ts: null, tick_ts: null, uptime_h: 0}};
  assert.equal(c.ctx.pilotProblems(ms, {open: true}, [], NOW, mem).length, 0);   // только что запущен — не сразу
  const later = c.ctx.pilotProblems(ms, {open: true}, [], NOW + 40, mem);
  const ids = list => list.map(x => x.id).join(',');   // массивы из vm-контекста — сравниваем строкой
  assert.equal(ids(later), 'price'); assert.match(later[0].text, /^Пилот AFLT: нет тиков/);
  assert.equal(c.ctx.pilotProblems(ms, {open: false}, [], NOW + 40, {}).length, 0);   // рынок закрыт — не тревожим
  const dead = {...ms, pilot: {...ms.pilot, ticking: false, loop_alive: true, feed: {ok: false, kind: 'auth', reason: 'токен отозван (401)'}}};
  let pp = c.ctx.pilotProblems(dead, {open: true}, [], NOW + 40, {});
  assert.equal(ids(pp), 'pl:feed'); assert.equal(pp[0].level, 'err'); assert.equal(pp[0].act, 'keys');
  pp = c.ctx.pilotProblems(dead, {open: true}, [{kind: 'tinkoff', level: 'err', text: 'Tinkoff: токен отозван'}], NOW + 40, {});
  assert.equal(pp.length, 0);
  const stale = {...ms, pilot: {...ms.pilot, ticking: false, loop_alive: true, last_tick_ts: NOW - 400, price_ts: NOW - 400, uptime_h: 2}};
  pp = c.ctx.pilotProblems(stale, {open: true}, [], NOW, {});
  assert.equal(ids(pp), 'pl:tick');   // «не тикает» — одна запись, без второй «цена не обновлялась»
  assert.equal(c.ctx.pilotProblems(stale, {open: true}, [{kind: 'pilot', level: 'warn', text: 'Пилот AFLT не тикает 6 мин'}], NOW, {}).length, 0);
});
