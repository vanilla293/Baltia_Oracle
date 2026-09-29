"""Одна самодостаточная HTML-страница панели (встроенные CSS и JS, без внешних CDN — работает
офлайн). Тёмная, чистая, адаптивная. Вкладки: Обзор, Диалог, Память, Позиции, Дневник,
Напоминания, Идеи, Проекты, Новости, Расходы, Логи, Настройки.

Токен доступа страница берёт из своего же адреса (?t=…) и подставляет во все запросы. JS сам
опрашивает /api/status раз в 3 с (баннер конфликта, карточки) и данные открытой вкладки — раз в 5 с.

Строку `PAGE` отдаёт `oracle.dashboard.server` на GET /. Здесь только вёрстка — никаких секретов.
"""
from __future__ import annotations

# Цвета взяты из проверенной палитры (тёмная поверхность): синий-акцент #3987e5,
# красный-тревога #e66767, зелёный-ок #199e70 — контраст на тёмном фоне выверен.
PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Оракул — панель</title>
<style>
  :root{
    --bg:#111110; --surface:#1a1a19; --surface-2:#232320; --line:#333330;
    --ink:#ffffff; --ink-2:#c3c2b7; --muted:#8a897f;
    --accent:#3987e5; --accent-2:#256abf; --ok:#199e70; --warn:#c98500; --bad:#e66767;
    --radius:12px; --gap:16px;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0}
  body{background:var(--bg);color:var(--ink);
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    -webkit-font-smoothing:antialiased}
  a{color:var(--accent);text-decoration:none}
  a:hover{text-decoration:underline}
  header{position:sticky;top:0;z-index:20;background:rgba(17,17,16,.92);
    backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
  .bar{display:flex;align-items:center;gap:12px;padding:12px 16px;max-width:1100px;margin:0 auto}
  .brand{font-weight:700;font-size:17px;letter-spacing:.2px}
  .dot{width:9px;height:9px;border-radius:50%;background:var(--muted);display:inline-block;
    margin-right:6px;vertical-align:middle}
  .dot.ok{background:var(--ok)} .dot.bad{background:var(--bad)} .dot.warn{background:var(--warn)}
  .spacer{flex:1}
  .pill{font-size:12px;color:var(--ink-2);background:var(--surface-2);border:1px solid var(--line);
    padding:4px 9px;border-radius:999px;white-space:nowrap}
  nav{max-width:1100px;margin:0 auto;padding:0 8px;display:flex;gap:2px;overflow-x:auto;
    scrollbar-width:thin}
  nav button{appearance:none;background:none;border:none;color:var(--ink-2);cursor:pointer;
    padding:11px 13px;font-size:14px;border-bottom:2px solid transparent;white-space:nowrap}
  nav button:hover{color:var(--ink)}
  nav button.active{color:var(--ink);border-bottom-color:var(--accent)}
  main{max-width:1100px;margin:0 auto;padding:var(--gap)}
  .banner{display:none;margin:0 0 var(--gap);padding:14px 16px;border-radius:var(--radius);
    background:rgba(230,103,103,.12);border:1px solid var(--bad);color:#ffd9d9}
  .banner.show{display:block}
  .banner h3{margin:0 0 6px;font-size:15px;color:var(--bad)}
  .banner pre{margin:6px 0 10px;white-space:pre-wrap;font:inherit;color:#ffe3e3}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:var(--gap)}
  .card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
    padding:16px}
  .card .k{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px}
  .card .v{font-size:26px;font-weight:700;margin-top:6px;line-height:1.15;word-break:break-word}
  .card .sub{font-size:13px;color:var(--ink-2);margin-top:4px}
  .list{display:flex;flex-direction:column;gap:10px}
  .item{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
    padding:12px 14px}
  .item .top{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
  .item .title{font-weight:600}
  .item .meta{font-size:12px;color:var(--muted)}
  .item .body{margin-top:6px;color:var(--ink-2);white-space:pre-wrap;word-break:break-word}
  .tag{font-size:11px;color:var(--ink-2);background:var(--surface-2);border:1px solid var(--line);
    padding:2px 8px;border-radius:999px}
  .msg{display:flex;flex-direction:column;gap:2px}
  .msg .who{font-size:12px;color:var(--muted)}
  .msg.user{align-items:flex-end}
  .bubble{max-width:80%;padding:9px 12px;border-radius:14px;background:var(--surface);
    border:1px solid var(--line);white-space:pre-wrap;word-break:break-word}
  .msg.user .bubble{background:var(--accent-2);border-color:var(--accent-2);color:#fff}
  table{width:100%;border-collapse:collapse;font-size:14px}
  th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
  th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.5px}
  td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
  .chart{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
    padding:16px;margin-bottom:var(--gap);overflow-x:auto}
  .chart h3{margin:0 0 12px;font-size:14px;color:var(--ink-2)}
  .logs{background:#0d0d0c;border:1px solid var(--line);border-radius:var(--radius);
    padding:12px;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    max-height:64vh;overflow:auto}
  .log{white-space:pre-wrap;word-break:break-word;padding:1px 0}
  .log .t{color:var(--muted)}
  .log .lv{font-weight:700;margin:0 6px}
  .log.INFO .lv{color:var(--accent)} .log.DEBUG .lv{color:var(--muted)}
  .log.WARNING .lv{color:var(--warn)} .log.ERROR .lv{color:var(--bad)}
  .log.CRITICAL .lv{color:var(--bad)}
  .toolbar{display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
  select,input,button.btn{font:inherit;color:var(--ink);background:var(--surface-2);
    border:1px solid var(--line);border-radius:8px;padding:8px 10px}
  input[type=checkbox]{width:18px;height:18px;accent-color:var(--accent);padding:0}
  button.btn{cursor:pointer}
  button.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
  button.btn.primary:hover{background:var(--accent-2)}
  button.btn:hover{border-color:var(--accent)}
  form.settings{display:flex;flex-direction:column;gap:22px}
  fieldset{border:1px solid var(--line);border-radius:var(--radius);padding:16px;margin:0}
  legend{padding:0 8px;color:var(--ink);font-weight:700;font-size:15px}
  .field{display:grid;grid-template-columns:minmax(200px,300px) 1fr;gap:12px;align-items:start;
    padding:12px 0;border-bottom:1px solid var(--line)}
  .field:last-child{border-bottom:none}
  .field .lab{font-weight:600}
  .field .help{font-size:12px;color:var(--muted);margin-top:3px}
  .field .in input,.field .in select{width:100%;max-width:460px}
  .field .err{color:var(--bad);font-size:12px;margin-top:4px;display:none}
  .field.bad .err{display:block}
  .field.bad .in input,.field.bad .in select{border-color:var(--bad)}
  .saverow{display:flex;gap:12px;align-items:center;position:sticky;bottom:0;
    background:rgba(17,17,16,.95);padding:14px 0;border-top:1px solid var(--line)}
  .note{font-size:13px;color:var(--ink-2)}
  .note.ok{color:var(--ok)} .note.warn{color:var(--warn)}
  .empty{color:var(--muted);padding:24px 4px;text-align:center}
  .muted{color:var(--muted)}
  @media(max-width:640px){
    .field{grid-template-columns:1fr}
    .card .v{font-size:22px}
  }
</style>
</head>
<body>
<header>
  <div class="bar">
    <span class="brand">🔮 <span id="botName">Оракул</span></span>
    <span class="pill"><span id="tgDot" class="dot"></span><span id="tgText">Telegram…</span></span>
    <span class="spacer"></span>
    <span class="pill" id="uptimePill">аптайм —</span>
    <span class="pill" id="costPill">сегодня —</span>
  </div>
  <nav id="tabs"></nav>
</header>
<main>
  <div class="banner" id="conflict">
    <h3>⚠️ Кто-то ещё опрашивает этого бота</h3>
    <pre id="conflictHint"></pre>
    <button class="btn primary" id="conflictBtn">Я закрыл, проверить</button>
  </div>
  <div id="view"></div>
</main>
<script>
"use strict";
const T = new URLSearchParams(location.search).get("t") || "";
const $ = (s, r) => (r || document).querySelector(s);
const el = (tag, attrs, kids) => {
  const n = document.createElement(tag);
  if (attrs) for (const k in attrs) {
    if (k === "class") n.className = attrs[k];
    else if (k === "html") n.innerHTML = attrs[k];
    else if (k === "text") n.textContent = attrs[k];
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), attrs[k]);
    else n.setAttribute(k, attrs[k]);
  }
  for (const c of ([]).concat(kids || [])) {
    if (c == null) continue;
    n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return n;
};
const esc = (s) => String(s == null ? "" : s);
const withT = (u) => u + (u.includes("?") ? "&" : "?") + "t=" + encodeURIComponent(T);

async function api(path){
  const r = await fetch(withT(path), {headers:{"X-Dash-Token":T}});
  if(!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
async function post(path, body){
  const r = await fetch(withT(path), {method:"POST",
    headers:{"Content-Type":"application/json","X-Dash-Token":T}, body:JSON.stringify(body||{})});
  return r.json().catch(()=>({ok:false,error:"плохой ответ"}));
}

// ── форматирование ─────────────────────────────────────────────────────────────
function fmtDur(sec){
  sec = Math.max(0, Math.floor(Number(sec)||0));
  const d=Math.floor(sec/86400), h=Math.floor(sec%86400/3600), m=Math.floor(sec%3600/60);
  if(d) return d+" д "+h+" ч";
  if(h) return h+" ч "+m+" мин";
  if(m) return m+" мин";
  return sec+" с";
}
function fmtMoney(x){
  const n=Number(x); if(!isFinite(n)) return "—";
  return "$"+n.toFixed(n<1?4:2);
}
function fmtTs(ts){
  if(ts==null) return "";
  let d;
  if(typeof ts==="number") d=new Date(ts*1000);
  else d=new Date(ts);
  if(isNaN(d)) return esc(ts);
  return d.toLocaleString("ru-RU",{day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"});
}
function pick(o){ for(let i=1;i<arguments.length;i++){ const k=arguments[i];
  if(o && o[k]!=null && o[k]!=="") return o[k];} return ""; }
function asList(d){
  if(Array.isArray(d)) return d;
  if(d && Array.isArray(d.items)) return d.items;
  if(d && typeof d==="object"){ for(const k in d){ if(Array.isArray(d[k])) return d[k]; } }
  return [];
}
function emptyBox(txt){ return el("div",{class:"empty",text:txt||"Пока пусто"}); }

// ── статус (шапка, обзор, баннер конфликта) ─────────────────────────────────────
let LAST_STATUS = null;
function applyStatus(s){
  LAST_STATUS = s;
  $("#botName").textContent = pick(s,"bot_name") || "Оракул";
  const tg = s.telegram || {};
  const dot = $("#tgDot"), txt = $("#tgText");
  if(tg.ok){ dot.className="dot ok";
    txt.textContent = "@"+(tg.username||"bot"); }
  else { dot.className="dot bad";
    txt.textContent = tg.error ? "Telegram: ошибка" : "Telegram —"; }
  $("#uptimePill").textContent = "аптайм " + fmtDur(s.uptime_sec);
  const cost = s.cost || {};
  $("#costPill").textContent = "сегодня " + (cost.today!=null?fmtMoney(cost.today):"—");

  const c = s.conflict || {};
  const banner = $("#conflict");
  if(c.active){
    banner.classList.add("show");
    $("#conflictHint").textContent = c.hint || "Часть сообщений уходит другой копии бота.";
  } else banner.classList.remove("show");

  if(current==="overview") renderOverview(s);
}
$("#conflictBtn").addEventListener("click", async (e)=>{
  e.target.disabled=true; e.target.textContent="Проверяю…";
  try{ await post("/api/action",{name:"clear_conflict"}); await refreshStatus(); }
  finally{ e.target.disabled=false; e.target.textContent="Я закрыл, проверить"; }
});
async function refreshStatus(){ try{ applyStatus(await api("/api/status")); }catch(e){} }

function statCard(k,v,sub){
  return el("div",{class:"card"},[el("div",{class:"k",text:k}),el("div",{class:"v",text:v}),
    sub?el("div",{class:"sub",text:sub}):null]);
}
function renderOverview(s){
  const v=$("#view"); v.innerHTML="";
  const tg=s.telegram||{}, cost=s.cost||{}, model=s.model||{}, bal=s.balance||null;
  const g=el("div",{class:"grid"});
  g.appendChild(statCard("Telegram", tg.ok?("@"+(tg.username||"bot")):"не в сети",
    tg.ok?("id "+(tg.id||"—")):(tg.error||"нет связи")));
  g.appendChild(statCard("Аптайм", fmtDur(s.uptime_sec),
    s.last_update_ago_sec!=null?("обновление "+fmtDur(s.last_update_ago_sec)+" назад"):""));
  g.appendChild(statCard("Модель", pick(model,"fast")||"—",
    model.deep?("глубокая: "+model.deep):""));
  g.appendChild(statCard("Расходы сегодня", cost.today!=null?fmtMoney(cost.today):"—",
    "модель DeepSeek"));
  g.appendChild(statCard("За 30 дней", cost.month!=null?fmtMoney(cost.month):"—",
    cost.calls!=null?(cost.calls+" вызовов"):""));
  if(bal){
    const b=(bal.balances&&bal.balances[0])||{};
    g.appendChild(statCard("Баланс DeepSeek", b.total!=null?fmtMoney(b.total):"—",
      bal.is_available?"счёт активен":"пополни счёт"));
  }
  const lock = s.single_instance_ok!==false;
  g.appendChild(statCard("Одна копия", lock?"да ✓":"замок не взят",
    lock?"эта папка — единственная":"проверь другие окна"));
  v.appendChild(g);

  const tw=asList(s.twins);
  if(tw.length){
    const box=el("div",{class:"card",style:"margin-top:16px"},[el("div",{class:"k",
      text:"Кто ещё найден в системе"})]);
    const tbl=el("table"); tbl.appendChild(el("tr",null,[el("th",{text:"pid"}),
      el("th",{text:"что"}),el("th",{text:"папка"}),el("th",{text:"наш токен"})]));
    tw.forEach(h=>tbl.appendChild(el("tr",null,[el("td",{text:esc(h.pid)}),
      el("td",{text:esc(h.detail||h.kind)}),el("td",{text:esc(h.folder||"?")}),
      el("td",{text:h.same_token?"да ⚠️":"нет"})])));
    box.appendChild(tbl); v.appendChild(box);
  }
}

// ── универсальный список ─────────────────────────────────────────────────────────
function itemCard(title, meta, body, tags){
  const top=el("div",{class:"top"},[title?el("span",{class:"title",text:title}):null,
    meta?el("span",{class:"meta",text:meta}):null]);
  (tags||[]).forEach(t=>top.appendChild(el("span",{class:"tag",text:t})));
  const kids=[top];
  if(body) kids.push(el("div",{class:"body",text:body}));
  return el("div",{class:"item"},kids);
}
function renderList(data, mapper, emptyTxt){
  const v=$("#view"); v.innerHTML="";
  const rows=asList(data);
  if(!rows.length){ v.appendChild(emptyBox(emptyTxt)); return; }
  const list=el("div",{class:"list"});
  rows.forEach(r=>{ try{ list.appendChild(mapper(r)); }catch(e){
    list.appendChild(itemCard("", "", JSON.stringify(r))); } });
  v.appendChild(list);
}

// ── вкладки ──────────────────────────────────────────────────────────────────────
const TABS=[
  {id:"overview", title:"Обзор", load:async()=>{ renderOverview(LAST_STATUS||await api("/api/status")); }},
  {id:"dialog", title:"Диалог", load:async()=>{
    const d=await api("/api/dialog?limit=60"); const v=$("#view"); v.innerHTML="";
    const rows=asList(d); if(!rows.length){ v.appendChild(emptyBox("Диалога пока нет")); return; }
    const box=el("div",{class:"list"});
    rows.forEach(m=>{ const role=(m.role||m.via||"").toLowerCase();
      const mine=role==="user"||role==="owner"||role==="me";
      const who=mine?"ты":(m.role==="assistant"?"Оракул":esc(m.role||"—"));
      box.appendChild(el("div",{class:"msg "+(mine?"user":"bot")},[
        el("div",{class:"who",text:who+(m.ts?(" · "+fmtTs(m.ts)):"")}),
        el("div",{class:"bubble",text:esc(pick(m,"content","text","message"))})]));
    });
    v.appendChild(box);
  }},
  {id:"memory", title:"Память", load:async()=>renderList(await api("/api/memory"),
    f=>itemCard(esc(pick(f,"content","text")), f.category?("["+f.category+"]"):"", "",
      f.id?["#"+f.id]:[]), "Фактов пока нет")},
  {id:"opinions", title:"Позиции", load:async()=>renderList(await api("/api/opinions"),
    o=>itemCard(esc(pick(o,"topic","title")),
      o.confidence!=null?("уверенность "+o.confidence+"%"):"",
      esc(pick(o,"stance","opinion"))+(o.reasons?("\\n"+o.reasons):"")),
    "Своих позиций пока нет")},
  {id:"journal", title:"Дневник", load:async()=>renderList(await api("/api/journal"),
    j=>itemCard("", fmtTs(pick(j,"ts","created_at","at")), esc(pick(j,"content","text"))),
    "Дневник пуст")},
  {id:"reminders", title:"Напоминания", load:async()=>renderList(await api("/api/reminders"),
    r=>itemCard(esc(pick(r,"text","title")),
      [pick(r,"when_local","when","next_local"), r.repeat].filter(Boolean).join(" · "),
      "", r.kind?[r.kind]:[]), "Напоминаний нет")},
  {id:"ideas", title:"Идеи", load:async()=>renderList(await api("/api/ideas"),
    i=>itemCard(esc(pick(i,"title","content")),
      i.score!=null?("оценка "+i.score):"", esc(i.title?pick(i,"content",""):""),
      [i.status].filter(Boolean)), "Идей пока нет")},
  {id:"projects", title:"Проекты", load:async()=>renderList(await api("/api/projects"),
    p=>{ const tasks=asList(p.tasks);
      return itemCard(esc(pick(p,"name","title")), esc(pick(p,"goal","description")),
        tasks.length?(tasks.map(t=>"• "+esc(pick(t,"text","title"))+
          (t.status?(" ("+t.status+")"):"")).join("\\n")):"",
        [p.status].filter(Boolean)); }, "Проектов пока нет")},
  {id:"news", title:"Новости", load:async()=>{
    const d=await api("/api/news"); const v=$("#view"); v.innerHTML="";
    const rows=asList(d); if(!rows.length){ v.appendChild(emptyBox("Новостей пока нет")); return; }
    const box=el("div",{class:"list"});
    rows.forEach(n=>{ const url=pick(n,"url","link");
      const t=el("div",{class:"top"},[
        url?el("a",{href:url,target:"_blank",class:"title",text:esc(pick(n,"title","headline"))})
           :el("span",{class:"title",text:esc(pick(n,"title","headline"))}),
        el("span",{class:"meta",text:[pick(n,"source","feed"),fmtTs(pick(n,"ts","published","date"))]
          .filter(Boolean).join(" · ")})]);
      const kids=[t]; const sum=pick(n,"summary","text");
      if(sum) kids.push(el("div",{class:"body",text:esc(sum)}));
      box.appendChild(el("div",{class:"item"},kids)); });
    v.appendChild(box);
  }},
  {id:"usage", title:"Расходы", load:async()=>renderUsage(await api("/api/usage?days=30"))},
  {id:"logs", title:"Логи", load:loadLogs},
  {id:"settings", title:"Настройки", load:loadSettings},
];

// ── расходы: заголовки + столбики по дням (SVG) + таблицы ─────────────────────────
function renderUsage(u){
  const v=$("#view"); v.innerHTML="";
  u=u||{};
  const g=el("div",{class:"grid"});
  g.appendChild(statCard("Всего за "+((u.days)||30)+" дн", fmtMoney(u.total_cost),
    (u.total_calls||0)+" вызовов"));
  g.appendChild(statCard("Токены запроса", (u.prompt_tokens||0).toLocaleString("ru-RU"), ""));
  g.appendChild(statCard("Токены ответа", (u.completion_tokens||0).toLocaleString("ru-RU"), ""));
  v.appendChild(g);

  const days=asList(u.by_day);
  const chart=el("div",{class:"chart"},[el("h3",{text:"Расходы по дням, $"})]);
  chart.appendChild(days.length?barChart(days):emptyBox("Пока нет данных за период"));
  v.appendChild(chart);

  const routes=asList(u.by_route);
  if(routes.length){
    const box=el("div",{class:"chart"},[el("h3",{text:"По назначению вызова"})]);
    const tbl=el("table"); tbl.appendChild(el("tr",null,[el("th",{text:"маршрут"}),
      el("th",{class:"num",text:"вызовы"}),el("th",{class:"num",text:"$"})]));
    routes.forEach(r=>tbl.appendChild(el("tr",null,[el("td",{text:esc(r.route)}),
      el("td",{class:"num",text:esc(r.calls)}),el("td",{class:"num",text:fmtMoney(r.cost)})])));
    box.appendChild(tbl); v.appendChild(box);
  }
  const models=asList(u.by_model);
  if(models.length){
    const box=el("div",{class:"chart"},[el("h3",{text:"По моделям"})]);
    const tbl=el("table"); tbl.appendChild(el("tr",null,[el("th",{text:"модель"}),
      el("th",{class:"num",text:"вызовы"}),el("th",{class:"num",text:"$"})]));
    models.forEach(r=>tbl.appendChild(el("tr",null,[el("td",{text:esc(r.model)}),
      el("td",{class:"num",text:esc(r.calls)}),el("td",{class:"num",text:fmtMoney(r.cost)})])));
    box.appendChild(tbl); v.appendChild(box);
  }
}
function barChart(days){
  const NS="http://www.w3.org/2000/svg";
  const W=Math.max(320, days.length*38+40), H=180, pad=28, base=H-22;
  const max=Math.max.apply(null, days.map(d=>Number(d.cost)||0).concat([0.000001]));
  const bw=Math.min(30, (W-pad*2)/days.length-6);
  const svg=document.createElementNS(NS,"svg");
  svg.setAttribute("viewBox","0 0 "+W+" "+H); svg.setAttribute("width","100%");
  svg.style.maxWidth=W+"px"; svg.style.display="block";
  const mk=(t,a,kids)=>{ const n=document.createElementNS(NS,t);
    for(const k in a) n.setAttribute(k,a[k]);
    (kids||[]).forEach(c=>n.appendChild(c)); return n; };
  // ось-основание
  svg.appendChild(mk("line",{x1:pad-6,y1:base,x2:W-6,y2:base,stroke:"#333330","stroke-width":1}));
  days.forEach((d,i)=>{
    const val=Number(d.cost)||0;
    const h=Math.max(2, Math.round((val/max)*(base-16)));
    const x=pad+i*((W-pad*2)/days.length)+((W-pad*2)/days.length-bw)/2;
    const y=base-h;
    const rect=mk("rect",{x:x, y:y, width:bw, height:h, rx:4, fill:"#3987e5"});
    rect.appendChild(mk("title",{},[document.createTextNode(
      (d.day||"")+": "+fmtMoney(val)+" · "+(d.calls||0)+" выз.")]));
    svg.appendChild(rect);
    if(days.length<=16 || i%Math.ceil(days.length/16)===0){
      const lbl=mk("text",{x:x+bw/2, y:H-6, "text-anchor":"middle", "font-size":"9",
        fill:"#8a897f"}); lbl.textContent=String(d.day||"").slice(5); svg.appendChild(lbl);
    }
  });
  return svg;
}

// ── логи ──────────────────────────────────────────────────────────────────────────
let logAfter=0, logLevel="INFO", logBox=null;
async function loadLogs(){
  const v=$("#view"); v.innerHTML=""; logAfter=0;
  const bar=el("div",{class:"toolbar"},[el("span",{class:"muted",text:"Уровень:"})]);
  const sel=el("select",{onchange:()=>{ logLevel=sel.value; loadLogs(); }});
  ["DEBUG","INFO","WARNING","ERROR"].forEach(l=>{
    const o=el("option",{value:l,text:l}); if(l===logLevel)o.selected=true; sel.appendChild(o); });
  bar.appendChild(sel);
  bar.appendChild(el("span",{class:"muted",text:"обновляется само"}));
  v.appendChild(bar);
  logBox=el("div",{class:"logs"}); v.appendChild(logBox);
  await pullLogs();
}
async function pullLogs(){
  if(!logBox) return;
  let d;
  try{ d=await api("/api/logs?after_id="+logAfter+"&level="+encodeURIComponent(logLevel)); }
  catch(e){ return; }
  const rows=asList(d);
  const atBottom = logBox.scrollTop+logBox.clientHeight >= logBox.scrollHeight-40;
  rows.forEach(r=>{
    logAfter=Math.max(logAfter, r.id||0);
    logBox.appendChild(el("div",{class:"log "+esc(r.level)},[
      el("span",{class:"t",text:fmtTs(r.ts)}),
      el("span",{class:"lv",text:esc(r.level)}),
      el("span",{text:esc(r.logger?(r.logger+": "):"")+esc(r.message)})]));
  });
  if(!logBox.childElementCount) logBox.appendChild(emptyBox("Записей нет"));
  if(atBottom) logBox.scrollTop=logBox.scrollHeight;
}

// ── настройки ───────────────────────────────────────────────────────────────────
let SETTINGS_META=null;
async function loadSettings(){
  const v=$("#view"); v.innerHTML="";
  const data=await api("/api/settings");
  SETTINGS_META=data;
  const values=data.values||{};
  const secsData=data.sections|| groupFlat(data.settings||[]);
  const form=el("form",{class:"settings", onsubmit:(e)=>{e.preventDefault();saveSettings();}});
  secsData.forEach(sec=>{
    const fs=el("fieldset",null,[el("legend",{text:sec.name})]);
    (sec.settings||[]).forEach(s=>fs.appendChild(fieldRow(s, values[s.key])));
    form.appendChild(fs);
  });
  const saveRow=el("div",{class:"saverow"},[
    el("button",{class:"btn primary",type:"submit",text:"Сохранить"}),
    el("span",{class:"note",id:"saveNote"})]);
  form.appendChild(saveRow);
  v.appendChild(form);
}
function groupFlat(list){
  const order=[], map={};
  list.forEach(s=>{ const n=s.section||"Прочее"; if(!map[n]){map[n]=[];order.push(n);}
    map[n].push(s); });
  return order.map(n=>({name:n, settings:map[n]}));
}
function fieldRow(s, cur){
  const id="f_"+s.key;
  let input;
  if(s.type==="bool"){
    input=el("input",{type:"checkbox",id:id});
    input.checked = String(cur)==="1" || cur===true;
  } else if(s.type==="choice"){
    input=el("select",{id:id});
    if(!s.choices||!s.choices.includes(cur)) input.appendChild(el("option",{value:"",text:"—"}));
    (s.choices||[]).forEach(c=>{ const o=el("option",{value:c,text:c});
      if(String(cur)===c)o.selected=true; input.appendChild(o); });
  } else {
    const t = s.type==="secret" ? "password" : (s.type==="int"||s.type==="float") ? "text" : "text";
    input=el("input",{type:t,id:id,value:cur!=null?String(cur):"",autocomplete:"off",spellcheck:"false"});
    if(s.type==="secret") input.placeholder = cur ? "оставь как есть, чтобы не менять" : "не задано";
  }
  input.dataset.key=s.key; input.dataset.type=s.type;
  const right=el("div",{class:"in"},[input, el("div",{class:"err"}),
    s.help?el("div",{class:"help",text:s.help}):null]);
  return el("div",{class:"field",id:"row_"+s.key},[
    el("div",null,[el("div",{class:"lab",text:s.title||s.key}),
      el("div",{class:"help",text:s.key})]), right]);
}
function collectForm(){
  const out={};
  document.querySelectorAll("[data-key]").forEach(inp=>{
    const k=inp.dataset.key, ty=inp.dataset.type;
    if(ty==="bool") out[k]= inp.checked ? "1":"0";
    else out[k]= inp.value;
  });
  return out;
}
async function saveSettings(){
  const note=$("#saveNote"); note.className="note"; note.textContent="Сохраняю…";
  document.querySelectorAll(".field.bad").forEach(f=>f.classList.remove("bad"));
  const res=await post("/api/settings", collectForm());
  const errs=res.errors||{};
  Object.keys(errs).forEach(k=>{ const row=$("#row_"+k); if(row){ row.classList.add("bad");
    const e=$(".err",row); if(e)e.textContent=errs[k]; } });
  if(Object.keys(errs).length){
    note.className="note warn";
    note.textContent="Не сохранил: проверь поля, отмеченные красным.";
    return;
  }
  const n=(res.changed||[]).length;
  if(res.restart_needed){
    note.className="note warn";
    note.textContent = n ? ("Сохранено ("+n+"). Нужен перезапуск, чтобы применилось.")
                         : "Изменений нет.";
    if(n) addRestartBtn();
  } else {
    note.className="note ok";
    note.textContent = n ? ("Сохранено ("+n+"), применено на лету.") : "Изменений нет.";
  }
}
function addRestartBtn(){
  if($("#restartBtn")) return;
  const row=$(".saverow");
  row.appendChild(el("button",{class:"btn",id:"restartBtn",type:"button",text:"Перезапустить бота",
    onclick:async(e)=>{ e.target.disabled=true; e.target.textContent="Перезапускаю…";
      await post("/api/action",{name:"restart"}); }}));
}

// ── навигация и опрос ─────────────────────────────────────────────────────────────
let current="overview", tabTimer=null;
function buildTabs(){
  const nav=$("#tabs");
  TABS.forEach(t=>nav.appendChild(el("button",{text:t.title, "data-id":t.id,
    onclick:()=>go(t.id)})));
}
async function go(id){
  current=id;
  document.querySelectorAll("#tabs button").forEach(b=>
    b.classList.toggle("active", b.getAttribute("data-id")===id));
  const tab=TABS.find(t=>t.id===id); if(!tab) return;
  $("#view").innerHTML='<div class="empty">Загрузка…</div>';
  try{ await tab.load(); }
  catch(e){ $("#view").innerHTML=""; $("#view").appendChild(emptyBox("Не удалось загрузить: "+e.message)); }
}
function tabTick(){
  const tab=TABS.find(t=>t.id===current); if(!tab) return;
  if(current==="logs"){ pullLogs(); return; }
  if(current==="settings") return;          // форму не перетираем во время правки
  tab.load().catch(()=>{});
}

buildTabs();
go("overview");
refreshStatus();
setInterval(refreshStatus, 3000);
setInterval(tabTick, 5000);
</script>
</body>
</html>
"""
