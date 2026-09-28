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
