// HQ: what needs you, by project and workstream; agents second. A view onto the
// local database, kept current by an authenticated event stream.
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'})[c]);
const ICONS = {
  inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
  bot: '<path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2M20 14h2M15 13v2M9 13v2"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  plug: '<path d="M12 22v-5M9 8V2M15 8V2"/><path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/>',
  chev: '<path d="m9 18 6-6-6-6"/>',
  back: '<path d="m15 18-6-6 6-6"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  up: '<path d="m18 15-6-6-6 6"/>',
  down: '<path d="m6 9 6 6 6-6"/>',
  plus: '<path d="M5 12h14M12 5v14"/>',
  out: '<path d="M7 7h10v10M7 17 17 7"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  send: '<path d="m5 12 7-7 7 7M12 19V5"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  pin: '<path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/>',
};
const icon = (name, cls = '') => `<svg class="icon ${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;
const HARNESS = {'claude-code': 'Claude Code', 'claude-desktop': 'Claude Desktop', codex: 'Codex', chatgpt: 'ChatGPT'};
const LANES = [['you', 'Needs you'], ['agents', 'With agents'], ['waiting', 'Waiting'], ['later', 'Later'], ['done', 'Done']];
const ACTIVITY = {active: 'Active', waiting: 'Waiting on you', idle: 'Idle', offline: 'Offline', unknown: 'Unknown'};
const LIVE = new Set(['active', 'idle', 'waiting']);
const OTHER = 'other';
const EMPTY = {
  you: ['Nothing needs you', 'Decisions and actions only you can take land here as agents hit them.'],
  agents: ['No agent work', 'Work an agent has taken on, or is picking up after your reply, shows here.'],
  waiting: ['Nothing waiting', 'Items blocked on a person, team or service outside HQ show here.'],
  later: ['Nothing for later', 'Items deferred to a date come back to Needs you on that day.'],
  done: ['Nothing done yet', 'Completed items stay here for reference.'],
};
const HARD_TO_READ = 'This item is hard to read. Rewrite it with hq_update in the HQ format (hq_reporting_guide): the title is the one action, up to 5 steps carrying their link, copy text or checks, minutes, matters (why it matters) and needs_you (why you, not the agent).';
// Agents leave rank unset on about half their items; fall back to the guide's order.
const DEFAULT_RANK = {blocker: 100, question: 150, todo: 300};
const NEW_FOR = 24 * 3600;

let token = document.querySelector('meta[name="hq-token"]').content;
let state = {sessions: [], items: []}, model = null, snapshotSignature = '';
let liveHealthy = false, lastLive = 0, streamController = null, loading = null, booted = false;
const seenInstances = new Set();
const drafts = new Map();
let search = '', doneLimit = 40, sheetOpen = false, openMove = null, showQuiet = false;
const folded = new Set(remember('hq-folded') || []);
const seen = new Set(remember('hq-seen') || []);
let sortMode = remember('hq-sort') || 'urgent';
let route = parseRoute();

function remember(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(key));
    localStorage.setItem(key, JSON.stringify(value));
  } catch { return null; }
}
const today = () => new Date().toLocaleDateString('en-CA');
const agentState = (session) => (liveHealthy ? session?.activity?.state : null) || 'unknown';
const harness = (provider) => HARNESS[provider] || 'Agent';
const mark = (name) => (name.length <= 2 ? name : name[0]).toUpperCase();
function safeURL(value) {try {const url = new URL(value); return url.protocol === 'https:' && !url.username && !url.password;} catch {return false;}}
function ago(seconds) {
  const s = Math.max(0, Date.now() / 1000 - seconds);
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
function daysUntil(date) {return Math.round((new Date(`${date}T00:00:00`) - new Date(`${today()}T00:00:00`)) / 86400000);}
function fmtDate(value) {
  if (!value) return '';
  const d = typeof value === 'number' ? new Date(value * 1000) : new Date(`${value}T00:00:00`);
  return d.toLocaleDateString('en-US', {month: 'short', day: 'numeric'});
}
function dueLabel(i) {
  const d = daysUntil(i.due);
  if (d < 0) return `Overdue ${-d}d`;
  if (d === 0) return 'Due today';
  if (d === 1) return 'Due tomorrow';
  if (d < 7) return `Due ${new Date(`${i.due}T00:00:00`).toLocaleDateString('en-US', {weekday: 'short'})}`;
  return `Due ${fmtDate(i.due)}`;
}
const rankOf = (i) => (i.rank == null || i.rank >= 9999 ? DEFAULT_RANK[i.kind] ?? 300 : i.rank);
const dueSoon = (i) => Boolean(i.due) && i.lane !== 'done' && daysUntil(i.due) <= 2;
const isNew = (i) => i.lane !== 'done' && Date.now() / 1000 - i.created < NEW_FOR && !seen.has(i.id);
function inline(value) {
  const text = String(value ?? ''), pattern = /`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\((https:\/\/[^\s)]+)\)/g;
  let out = '', end = 0;
  for (const m of text.matchAll(pattern)) {
    out += esc(text.slice(end, m.index));
    out += m[1] ? `<code>${esc(m[1])}</code>` : m[2] ? `<strong>${esc(m[2])}</strong>` : safeURL(m[4]) ? `<a href="${esc(m[4])}" target="_blank" rel="noreferrer">${esc(m[3])}</a>` : esc(m[0]);
    end = m.index + m[0].length;
  }
  return out + esc(text.slice(end));
}
function blocks(text) {
  const out = [];
  let list = null;
  for (const line of String(text ?? '').split('\n')) {
    const t = line.trim();
    if (!t) {list = null; continue;}
    if (/^[-*•] /.test(t)) {
      if (!list) out.push((list = []));
      list.push(t.slice(2));
    } else {
      list = null;
      out.push(t);
    }
  }
  return out.map((b) => (Array.isArray(b) ? `<ul>${b.map((li) => `<li>${inline(li)}</li>`).join('')}</ul>` : `<p>${inline(b)}</p>`)).join('');
}
function toast(message) {
  const t = $('#toast');
  t.textContent = message;
  t.classList.add('on');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove('on'), 2600);
}

// Which list an item sits in, from the human's side.
function laneOf(item) {
  if (item.status === 'done') return 'done';
  if (item.bucket === 'later') return 'later';
  if (item.status === 'answered' || item.asking) return 'agents';
  if (item.owner === 'human') return 'you';
  if (item.owner === 'external') return 'waiting';
  return 'agents';
}
const count = (items, lane) => items.filter((i) => i.lane === lane).length;
function derive() {
  const sessions = new Map(state.sessions.map((s) => [s.id, s]));
  const items = state.items.map((i) => ({...i, lane: laneOf(i), session: sessions.get(i.session_id)}));
  const projects = new Map();
  for (const item of items) {
    if (!projects.has(item.project)) projects.set(item.project, {id: item.project, name: item.project_name, items: [], streams: new Map()});
    const p = projects.get(item.project);
    p.items.push(item);
    if (!p.streams.has(item.workstream)) p.streams.set(item.workstream, {id: item.workstream, project: p.id, name: item.workstream_name, items: []});
    p.streams.get(item.workstream).items.push(item);
  }
  for (const p of projects.values()) p.workstreams = [...p.streams.values()];
  // An agent appears while it has open work or is verifiably running.
  const agents = state.sessions.map((s) => ({...s, items: items.filter((i) => i.session_id === s.id)}))
    .filter((s) => s.items.some((i) => i.lane !== 'done') || LIVE.has(s.activity?.state) || s.id === route.agent);
  return {items, projects: [...projects.values()], agents};
}

// Routes live in the hash: #/, #/agents[/id], #/p/project[/workstream], plus ?lane=&item=.
function parseRoute(hash = globalThis.location?.hash || '') {
  const [path, query = ''] = hash.replace(/^#/, '').split('?');
  const parts = path.split('/').filter(Boolean).map(decodeURIComponent);
  const q = new URLSearchParams(query);
  const r = {page: 'needs', lane: q.get('lane') || 'you', item: q.get('item')};
  if (parts[0] === 'agents') Object.assign(r, {page: parts[1] ? 'agent' : 'agents', agent: parts[1]});
  else if (parts[0] === 'p' && parts[1]) Object.assign(r, {page: parts[2] ? 'workstream' : 'project', project: parts[1], workstream: parts[2]});
  return r;
}
function href(r) {
  const enc = encodeURIComponent;
  const path = {agents: '/agents', agent: `/agents/${enc(r.agent)}`, project: `/p/${enc(r.project)}`, workstream: `/p/${enc(r.project)}/${enc(r.workstream)}`}[r.page] || '/';
  const q = new URLSearchParams();
  if (r.lane && r.lane !== 'you' && r.page !== 'needs') q.set('lane', r.lane);
  if (r.item) q.set('item', r.item);
  return `#${path}${q.toString() ? `?${q}` : ''}`;
}
function go(next) {
  const target = href({...route, ...next});
  if (target === location.hash) return render();
  location.hash = target;
}

// Scope
function scopeItems(r = route) {
  if (r.page === 'project') return model.items.filter((i) => i.project === r.project);
  if (r.page === 'workstream') return model.items.filter((i) => i.project === r.project && i.workstream === r.workstream);
  if (r.page === 'agent') return model.items.filter((i) => i.session_id === r.agent);
  return model.items;
}
const newest = (lane) => sortMode === 'new' && lane !== 'done';
const leadOf = (i) => (i.pinned ? 0 : dueSoon(i) ? 1 : 2);
function sortLane(items, lane) {
  if (newest(lane)) return [...items].sort((a, b) => b.created - a.created);
  const by = {
    you: (a, b) => rankOf(a) - rankOf(b) || b.created - a.created,
    later: (a, b) => a.defer_until.localeCompare(b.defer_until),
  }[lane] || ((a, b) => (b.updated || b.created) - (a.updated || a.created));
  return [...items].sort((a, b) => leadOf(a) - leadOf(b) || (leadOf(a) === 1 ? a.due.localeCompare(b.due) : 0) || by(a, b));
}
// Pinned and due-soon items lead every list; the rest group by workstream.
function groups(items, level, {lead = true} = {}) {
  const out = [];
  let rest = items;
  for (const [key, label, test] of lead ? [['pinned', 'Pinned', (i) => i.pinned], ['due', 'Due soon', dueSoon]] : []) {
    const picked = rest.filter(test);
    if (!picked.length) continue;
    out.push({key, label, items: picked});
    rest = rest.filter((i) => !test(i));
  }
  if (level === 'none') return rest.length ? [...out, {key: '', label: out.length ? 'Everything else' : '', items: rest}] : out;
  const map = new Map();
  for (const item of rest) {
    const key = level === 'project' ? item.workstream : `${item.project}/${item.workstream}`;
    if (!map.has(key)) map.set(key, {key, project: item.project, workstream: item.workstream, items: []});
    map.get(key).items.push(item);
  }
  // Groups keep the order of their first item, so the most urgent workstream leads.
  return [...out, ...map.values()];
}
const projectOf = (id) => model.projects.find((p) => p.id === id);
const projectName = (id) => projectOf(id)?.name || id;
function workstreamName(project, id) {return projectOf(project)?.workstreams.find((w) => w.id === id)?.name || (id === OTHER ? 'Other' : id);}
function agentsOn(items) {
  const ids = new Set(items.filter((i) => i.lane !== 'done').map((i) => i.session_id));
  return model.agents.filter((a) => ids.has(a.id));
}

// Render. The floating panel (float.js) paints its own surface over the same state.
let paint = paintDashboard;
function render(options) {paint(options);}
function paintDashboard({background = false} = {}) {
  model = derive();
  // A deep link waits for the first snapshot before giving up on its item.
  if (snapshotSignature && route.item && !model.items.some((i) => i.id === route.item)) route.item = null;
  const scrolls = ['#page', '.sb-body', '.d-body'].map((sel) => [sel, document.querySelector(sel)?.scrollTop]);
  const active = document.activeElement, selection = active?.id === 'search' ? [active.selectionStart, active.selectionEnd] : null;
  $('#sidebar').innerHTML = sidebarHTML();
  $('#main').innerHTML = mainHTML();
  // Typing in the item pane wins over a live refresh; drafts are kept either way.
  const typing = background && $('#detail').contains(active) && active.matches('textarea, input, select');
  if (!typing) renderDetail();
  // Same list as before (a refresh, or an item opened): keep where the reader was.
  const scope = JSON.stringify([route.page, route.project, route.workstream, route.agent, route.lane, search]);
  if (scope === render.scope) scrolls.forEach(([sel, top]) => top && document.querySelector(sel) && (document.querySelector(sel).scrollTop = top));
  else if (scrolls[1][1]) $('.sb-body').scrollTop = scrolls[1][1];
  render.scope = scope;
  if (selection) {const el = $('#search'); el.focus(); el.setSelectionRange(...selection);}
  document.body.classList.toggle('detail-open', Boolean(route.item));
  $('#scrim').className = `scrim ${sheetOpen ? 'sheet' : route.item ? 'on' : ''}`;
  $('#sidebar').classList.toggle('on', sheetOpen);
  bind();
}

function sidebarHTML() {
  const you = count(model.items, 'you');
  const active = model.agents.filter((a) => ['active', 'waiting'].includes(agentState(a))).length;
  const here = (page) => (route.page === page && !search ? 'aria-current="page"' : '');
  const projects = model.projects.filter((p) => p.items.some((i) => i.lane !== 'done') || p.id === route.project).sort((a, b) => a.name.localeCompare(b.name));
  return `<div class="sb-head">
      <div class="sb-title"><span class="wordmark">HQ</span></div>
      <label class="search">${icon('search', 'sm')}<input id="search" type="search" placeholder="Search" value="${esc(search)}" aria-label="Search everything"><span class="kbd">⌘K</span></label>
    </div>
    <div class="sb-body scroll">
      <nav class="sb-nav" aria-label="Main">
        <a class="sb-item" href="#/" ${here('needs')}>${icon('inbox')}<span class="label">Needs you</span>${you ? `<span class="badge">${you}</span>` : ''}</a>
        <a class="sb-item" href="#/agents" ${['agents', 'agent'].includes(route.page) && !search ? 'aria-current="page"' : ''}>${icon('bot')}<span class="label">Agents</span>${active ? `<span class="sb-count">${active} active</span>` : ''}</a>
      </nav>
      <div class="sb-section">Projects</div>
      <nav class="sb-nav" aria-label="Projects">${projects.map(projectNavHTML).join('')}</nav>
    </div>
    <div class="sb-foot">
      <button class="sb-item" id="connect">${icon('plug')}<span class="label">Connect agents</span></button>
      <div class="live"><span class="dot ${liveHealthy ? 'active' : 'err'}"></span>${liveHealthy ? 'Live' : 'Reconnecting…'}</div>
    </div>`;
}
function projectNavHTML(p) {
  const streams = p.workstreams.filter((w) => w.items.some((i) => i.lane !== 'done') || (route.project === p.id && route.workstream === w.id));
  const foldable = streams.length > 1 || (streams.length === 1 && streams[0].id !== OTHER);
  const open = foldable && route.project === p.id && !folded.has(p.id);
  const current = route.page === 'project' && route.project === p.id && !search;
  const you = count(p.items, 'you');
  const fresh = !open && p.items.some((i) => i.lane === 'you' && isNew(i));
  const rows = open
    ? `<div class="ws-group" role="group" aria-label="${esc(p.name)} workstreams">${streams
        .sort((a, b) => (a.id === OTHER) - (b.id === OTHER) || a.name.localeCompare(b.name))
        .map((w) => {
          const n = count(w.items, 'you');
          const dot = w.items.some((i) => i.lane === 'you' && isNew(i));
          const on = route.page === 'workstream' && route.project === p.id && route.workstream === w.id && !search;
          return `<a class="ws-item" href="${esc(href({page: 'workstream', project: p.id, workstream: w.id, lane: 'you'}))}" ${on ? 'aria-current="page"' : ''}><span class="label">${esc(w.name)}</span>${dot ? '<span class="sb-new" title="New in the last day"></span>' : ''}${n ? `<span class="sb-count">${n}</span>` : ''}</a>`;
        })
        .join('')}</div>`
    : '';
  return `<div><button class="sb-item" data-project="${esc(p.id)}" ${current ? 'aria-current="page"' : ''} ${foldable ? `aria-expanded="${open}"` : ''}><span class="mark">${esc(mark(p.name))}</span><span class="label">${esc(p.name)}</span>${fresh ? '<span class="sb-new" title="New in the last day"></span>' : ''}${you ? `<span class="sb-count">${you}</span>` : ''}${foldable ? `<svg class="icon chev ${open ? 'open' : ''}" viewBox="0 0 24 24" aria-hidden="true">${ICONS.chev}</svg>` : ''}</button>${rows}</div>`;
}

function mainHTML() {
  const page = search ? searchPage() : {needs: needsPage, agents: agentsPage, agent: agentPage, project: projectPage, workstream: workstreamPage}[route.page]();
  return `<div class="phone-bar"><button class="icon-btn" id="menu" aria-label="Open menu">${icon('menu')}</button><span class="t">${esc(page.title)}</span>${page.actions ? newItemIcon : ''}</div>
    <div class="page scroll" id="page"><div class="page-inner">
      ${page.crumb ? `<nav class="crumb" aria-label="Breadcrumb"><button data-href="${esc(page.crumb.href)}">${esc(page.crumb.label)}</button>${icon('chev', 'sm')}</nav>` : ''}
      <header class="page-head"><div><h1>${esc(page.title)}</h1>${page.desc ? `<div class="desc">${page.desc}</div>` : ''}</div>${page.actions || ''}</header>
      ${page.body}
    </div></div>`;
}
const newItemPill = `<button class="pill outline sm" data-new>${icon('plus', 'sm')}New item</button>`;
const newItemIcon = `<button class="icon-btn" data-new aria-label="New item">${icon('plus')}</button>`;
function sortHTML(lane) {
  if (lane === 'done') return '';
  return `<div class="sort" role="group" aria-label="Order">${[['urgent', 'Most urgent'], ['new', 'Newest']].map(([id, label]) => `<button data-sort="${id}" aria-pressed="${sortMode === id}">${label}</button>`).join('')}</div>`;
}
const headActions = (lane) => `<div class="head-actions">${sortHTML(lane)}${newItemPill}</div>`;

function needsPage() {
  const items = sortLane(model.items.filter((i) => i.lane === 'you'), 'you');
  const decisions = items.filter((i) => i.kind === 'question').length;
  const minutes = items.reduce((n, i) => n + (i.minutes || 0), 0);
  const fresh = items.filter(isNew).length;
  const desc = items.length ? [`${items.length} items`, `${decisions} decisions`, fresh ? `${fresh} new` : '', minutes ? `about ${Math.max(1, Math.round(minutes / 60))} h of your time` : ''].filter(Boolean).join(' · ') : '';
  return {title: 'Needs you', desc: esc(desc), actions: headActions('you'), body: listHTML(items, 'you', 'all')};
}
function scopePage({title, crumb, items, level, desc}) {
  const lanes = LANES.map(([id, label]) => {
    const n = count(items, id);
    return `<button class="tab" role="tab" data-lane="${id}" aria-selected="${route.lane === id}">${label}${id !== 'done' && n ? `<span class="n">${n}</span>` : ''}</button>`;
  }).join('');
  const lane = sortLane(items.filter((i) => i.lane === route.lane), route.lane);
  return {title, crumb, desc, actions: headActions(route.lane), body: `<div class="tabs" role="tablist" aria-label="Status">${lanes}</div>${listHTML(lane, route.lane, level)}`};
}
function agentsDesc(items) {
  const agents = agentsOn(items);
  if (!agents.length) return '';
  const active = agents.filter((a) => agentState(a) === 'active');
  const shown = (active.length ? active : agents).slice(0, 2);
  const rest = agents.length - shown.length;
  return `${shown.map((a) => `<span class="who"><span class="dot ${agentState(a)}"></span>${esc(a.name)}</span>`).join('')}${rest ? `<span>+${rest} more ${rest === 1 ? 'agent' : 'agents'}</span>` : ''}`;
}
function projectPage() {
  const p = projectOf(route.project);
  if (!p) return {title: route.project, body: emptyHTML('you')};
  return scopePage({title: p.name, items: p.items, level: 'project', desc: agentsDesc(p.items)});
}
function workstreamPage() {
  const items = scopeItems();
  return scopePage({title: workstreamName(route.project, route.workstream), crumb: {label: projectName(route.project), href: href({page: 'project', project: route.project, lane: route.lane})}, items, level: 'none', desc: agentsDesc(items)});
}
// Agents by the folder they work in: running first, then waiting on you, then finished
// within the last day. Anything quieter folds away. A conversation is one agent, however
// many agents it runs in the background.
const AGENT_WINDOW = 24 * 3600;
// The folder's own name only, never the path to it.
const folderName = (cwd) => String(cwd || '').replace(/\/+$/, '').split('/').pop() || 'No folder';
// How long something has been going, as a clock: 4m 05s, then 1h 12m.
function ranFor(seconds) {
  const s = Math.max(0, Math.floor(seconds)), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${String(m).padStart(2, '0')}m` : `${m}m ${String(s % 60).padStart(2, '0')}s`;
}
function agentFolders(sessions, items, now = Date.now() / 1000) {
  const all = sessions.map((s) => {
    const mine = items.filter((i) => i.session_id === s.id && i.lane !== 'done'), st = agentState(s);
    return {id: s.id, name: s.name, harness: harness(s.provider), folder: s.cwd || '', state: st,
      since: s.activity?.since || null, last: s.last_active || null,
      background: st === 'active' && s.activity?.source === 'background',
      you: mine.filter((i) => i.lane === 'you').length, open: mine.length};
  });
  const recent = (a) => Boolean(a.last) && now - a.last < AGENT_WINDOW;
  const newest = (key) => (x, y) => (y[key] || 0) - (x[key] || 0);
  const running = all.filter((a) => a.state === 'active').sort(newest('since'));
  const waiting = all.filter((a) => a.state === 'waiting').sort(newest('since'));
  const finished = all.filter((a) => ['idle', 'offline'].includes(a.state) && recent(a)).sort(newest('last'));
  // No live signal is never shown as finished: only that it was seen running lately.
  const unsure = all.filter((a) => a.state === 'unknown' && recent(a)).sort(newest('last'));
  const shown = [...running, ...waiting, ...finished, ...unsure];
  const quiet = all.filter((a) => !shown.includes(a)).sort(newest('last'));
  // Folders keep the order of their first agent, so the folder with work running leads.
  const group = (list) => {
    const folders = new Map();
    for (const a of list) {
      if (!folders.has(a.folder)) folders.set(a.folder, {key: a.folder, name: folderName(a.folder), agents: [], running: false});
      const f = folders.get(a.folder);
      f.agents.push(a);
      f.running ||= a.state === 'active';
    }
    return [...folders.values()];
  };
  return {all, groups: group(shown), quiet: group(quiet), running: running.length, finished: finished.length, quietCount: quiet.length};
}
const sinceTick = (a, now) => (a.since ? `<span class="tick" data-since="${a.since}">${ranFor(now - a.since)}</span>` : '');
// The line under an agent's name.
function agentMeta(a, now = Date.now() / 1000) {
  if (a.state === 'active') return `running${a.since ? ` ${sinceTick(a, now)}` : ''}${a.background ? ' · in the background' : ''}`;
  if (a.state === 'waiting') return 'waiting on you';
  if (a.state === 'unknown') return a.last ? `no live signal · seen running ${ago(a.last)}` : 'no live signal';
  if (a.state === 'offline') return a.last ? `offline · finished ${ago(a.last)}` : 'offline';
  return a.last ? `finished ${ago(a.last)}` : 'idle';
}
// The same, said in full, at the top of one agent's page.
function agentStateLine(a, now = Date.now() / 1000) {
  if (a.state === 'active') return `${a.background ? 'Agents running in the background' : 'Running'}${a.since ? ` for ${sinceTick(a, now)}` : ''}`;
  if (a.state === 'waiting') return `Waiting on you${a.since ? ` for ${sinceTick(a, now)}` : ''}`;
  if (a.state === 'unknown') return a.last ? `No live signal · last seen running ${ago(a.last)}` : 'No live signal';
  if (a.state === 'offline') return a.last ? `Offline · finished ${ago(a.last)}` : 'Offline';
  return a.last ? `Finished ${ago(a.last)}` : 'Idle';
}
const agentSide = (a) => (a.you ? `<b>${a.you}</b> for you` : a.open ? `${a.open} open` : '');
const agentDot = (a) => `<span class="dot ${a.state}${a.state === 'active' ? ' pulse' : ''}" aria-hidden="true"></span>`;
function tickTimes(root = document) {
  const now = Date.now() / 1000;
  root.querySelectorAll('.tick[data-since]').forEach((el) => {el.textContent = ranFor(now - Number(el.dataset.since));});
}
function agentsPage() {
  const view = agentFolders(state.sessions, model.items), now = Date.now() / 1000;
  const row = (a) => `<a class="row agent-row" href="${esc(href({page: 'agent', agent: a.id, lane: 'you', item: null}))}" data-session="${esc(a.id)}">${agentDot(a)}<span class="a-main"><span class="a-name">${esc(a.name)}</span><span class="a-meta">${esc(a.harness)} · ${agentMeta(a, now)}</span></span><span class="a-side">${agentSide(a)}</span></a>`;
  const folder = (f) => `<section class="group folder"><div class="group-head"><span class="f-name">${esc(f.name)}</span>${f.running ? '<span class="n">running</span>' : ''}</div>${f.agents.map(row).join('')}</section>`;
  const fold = view.quiet.length ? (showQuiet ? view.quiet.map(folder).join('')
    : `<button class="fold" data-quiet>${view.quietCount} more, in ${view.quiet.length} ${view.quiet.length === 1 ? 'folder' : 'folders'}${icon('chev', 'sm')}</button>`) : '';
  const desc = [view.running ? `${view.running} running` : '', view.finished ? `${view.finished} finished in the last day` : ''].filter(Boolean).join(' · ');
  const body = view.groups.map(folder).join('') + fold;
  return {title: 'Agents', desc: esc(desc), body: body || `<div class="empty"><h3>No agents yet</h3><p>Connect an agent, then tell it to register this conversation in HQ.</p></div>`};
}
function agentPage() {
  const a = model.agents.find((s) => s.id === route.agent);
  if (!a) return {title: 'Agent', crumb: {label: 'Agents', href: '#/agents'}, body: `<div class="empty"><h3>This conversation isn't in HQ</h3><p>It may have been removed.</p></div>`};
  const where = (a.cwd || '').replace(/^\/Users\/[^/]+/, '~');
  const desc = `<span class="who"><span class="dot ${agentState(a)}"></span>${ACTIVITY[agentState(a)]}</span><span>${esc(harness(a.provider))}</span>${where ? `<span>${esc(where)}</span>` : ''}`;
  return scopePage({title: a.name, crumb: {label: 'Agents', href: '#/agents'}, items: scopeItems(), level: 'all', desc});
}
function searchPage() {
  const q = search.toLowerCase();
  const items = model.items.filter((i) => `${i.title} ${i.detail} ${i.waiting_on} ${i.tags.join(' ')} ${i.session_name} ${i.project_name} ${i.workstream_name} ${harness(i.provider)}`.toLowerCase().includes(q));
  const order = LANES.map(([id]) => id);
  const sorted = [...items].sort((a, b) => order.indexOf(a.lane) - order.indexOf(b.lane) || (b.updated || 0) - (a.updated || 0));
  return {title: 'Search', desc: esc(`${items.length} ${items.length === 1 ? 'item' : 'items'} match “${search}”`), body: sorted.length ? groupsHTML(sorted, 'all', {withLane: true, lead: false}) : `<div class="empty"><h3>No matches</h3><p>Search covers titles, context, tags, projects, workstreams and agents.</p></div>`};
}

function listHTML(items, lane, level) {
  if (!items.length) return emptyHTML(lane);
  let shown = items, more = '';
  if (lane === 'done' && items.length > doneLimit) {
    shown = items.slice(0, doneLimit);
    more = `<div class="more-row"><button class="pill ghost sm" data-more>Show ${items.length - doneLimit} more</button></div>`;
  }
  // Newest is a feed: one list, each row saying where it lives and when it arrived.
  if (newest(lane)) return `<section class="group">${shown.map((i) => rowHTML(i, {where: level !== 'none', age: true})).join('')}</section>${more}`;
  return groupsHTML(shown, level) + more;
}
function groupsHTML(items, level, {withLane = false, lead = true} = {}) {
  return groups(items, level, {lead}).map((g) => {
    const head = g.label !== undefined ? (g.label ? `<div class="group-head">${g.label}<span class="n">${g.items.length}</span></div>` : '') : level === 'none' ? '' : groupHeadHTML(g, level);
    return `<section class="group">${head}${g.items.map((i) => rowHTML(i, {withLane, where: Boolean(g.label) && level !== 'none'})).join('')}</section>`;
  }).join('');
}
function groupHeadHTML(g, level) {
  const target = href({page: 'workstream', project: g.project, workstream: g.workstream, lane: route.page === 'needs' ? 'you' : route.lane, item: null});
  const name = workstreamName(g.project, g.workstream);
  const lone = g.workstream === OTHER && (projectOf(g.project)?.workstreams.length || 0) <= 1;
  const label = level === 'project' ? esc(name) : `<span class="of">${esc(projectName(g.project))}</span>${lone ? '' : `<span class="of">›</span>${esc(name)}`}`;
  return `<div class="group-head"><button data-href="${esc(target)}">${label}</button><span class="n">${g.items.length}</span></div>`;
}
function rowHTML(i, opts = {}) {
  return `<button class="row" data-item="${esc(i.id)}" aria-current="${route.item === i.id}">${isNew(i) ? '<span class="new" title="New in the last day"></span>' : ''}<span class="title">${esc(i.title)}</span><span class="meta">${metaHTML(i, opts)}</span></button>`;
}
function whereLabel(i) {
  const lone = i.workstream === OTHER && (projectOf(i.project)?.workstreams.length || 0) <= 1;
  return lone ? i.project_name : `${i.project_name} › ${i.workstream_name}`;
}
function metaHTML(i, {withLane = false, where = false, age = false, kind = false} = {}) {
  const parts = [];
  if (age && i.delivery === 'failed') parts.push('<span class="overdue">Reply failed</span>');
  if (kind && i.kind === 'question') parts.push('Decision');
  if (kind && i.kind === 'blocker') parts.push('<span class="blocker">Blocker</span>');
  if (i.due && i.lane !== 'done') parts.push(`<span class="${daysUntil(i.due) < 0 ? 'overdue' : 'due'}">${dueLabel(i)}</span>`);
  if (withLane) parts.push(LANES.find(([id]) => id === i.lane)[1]);
  if (where) parts.push(esc(whereLabel(i)));
  if (age) parts.push(ago(i.created));
  else if (i.lane === 'you' && !where) {
    if (i.kind === 'question') parts.push('Decision');
    if (i.kind === 'blocker') parts.push('<span class="blocker">Blocker</span>');
    if (i.minutes) parts.push(`${i.minutes} min`);
  } else if (i.lane === 'agents' && !withLane && !where) {
    if (i.status === 'answered') parts.push(deliveryText(i));
    else if (i.asking) parts.push('Answering your question');
    else parts.push(`<span class="dot ${agentState(i.session)}" title="${esc(i.session_name)} · ${ACTIVITY[agentState(i.session)]}"></span>${ago(i.updated || i.created)}`);
  } else if (i.lane === 'waiting') parts.push(`On ${esc(i.waiting_on || 'someone')}`);
  else if (i.lane === 'later') parts.push(fmtDate(i.defer_until));
  else if (i.lane === 'done') parts.push(i.skipped ? `Skipped · ${fmtDate(i.updated)}` : fmtDate(i.updated));
  return parts.join('<span class="sep">·</span>');
}
function emptyHTML(lane) {
  const [title, text] = EMPTY[lane] || EMPTY.you;
  return `<div class="empty"><h3>${title}</h3><p>${text}</p></div>`;
}
function deliveryText(i) {
  return {pending: 'Reply queued', sending: 'Sending reply…', sent: 'Reply sent, not yet confirmed', received: 'Agent has your reply', failed: 'Reply failed'}[i.delivery] || 'Reply saved';
}

// Item pane
function laneLine(i) {
  if (i.lane === 'you') return [i.kind === 'question' ? 'Your decision' : i.kind === 'blocker' ? 'Blocking, needs you' : 'Your action', i.minutes ? `${i.minutes} min` : ''].filter(Boolean).join(' · ');
  if (i.lane === 'agents') return i.status === 'answered' ? `${deliveryText(i)} · agent next` : i.asking ? 'Agent is answering your question' : 'With the agent';
  if (i.lane === 'waiting') return `Waiting on ${i.waiting_on || 'someone'}`;
  if (i.lane === 'later') return `Back on ${fmtDate(i.defer_until)}`;
  return `${i.skipped ? 'Skipped' : 'Done'} ${fmtDate(i.updated)}`;
}
const stateLine = (i) => [i.pinned ? 'Pinned' : '', i.due && i.lane !== 'done' ? dueLabel(i) : '', laneLine(i)].filter(Boolean).join(' · ');
const isReadable = (item) => Boolean(item.matters || item.needs_you || item.steps?.some((step) => typeof step === 'object'));
const checksOf = (item) => (item.steps || []).flatMap((step, i) => (typeof step === 'object' && step.checks ? step.checks.map((text, j) => ({key: `${i}:${j}`, text})) : []));
const draftOf = (id) => drafts.get(id) || {};
function setDraft(id, patch) {drafts.set(id, {...draftOf(id), ...patch});}
function planHTML(item, draft = draftOf(item.id)) {
  const ticks = draft.checks || [], live = item.status === 'open';
  const steps = (item.steps || []).map((raw, n) => {
    const s = typeof raw === 'string' ? {text: raw} : raw;
    const link = s.link && safeURL(s.link.url) ? `<a class="pill outline sm" href="${esc(s.link.url)}" target="_blank" rel="noreferrer">${esc(s.link.label)}${icon('out', 'sm')}</a>` : '';
    const copy = s.copy ? `<div class="copy-line"><span>${esc(s.copy)}</span><button class="pill ghost sm" data-copy-step="${n}">${draft.copied === n ? `${icon('check', 'sm')}Copied` : 'Copy'}</button></div>` : '';
    const checks = s.checks ? `<div class="checks">${s.checks.map((c, k) => `<button type="button" class="check" role="checkbox" data-check="${n}:${k}" aria-checked="${ticks.includes(`${n}:${k}`)}" ${live ? '' : 'disabled'}><span class="box">${icon('check')}</span><span>${esc(c)}</span></button>`).join('')}</div>` : '';
    const tally = s.checks ? `<span class="tally">${ticks.filter((t) => t.startsWith(`${n}:`)).length} of ${s.checks.length}</span>` : '';
    return `<li class="pstep"><span class="num">${n + 1}</span><div><div class="phead"><span>${inline(s.text)}</span>${link || tally}</div>${copy}${checks}</div></li>`;
  }).join('');
  return steps ? `<ol class="plan">${steps}</ol>` : '';
}
function reasonsHTML(item) {
  const reason = (label, r) => (r ? `<section class="reason"><div class="d-label">${label}</div><p class="r-lead">${inline(r.lead)}</p>${r.points?.length ? `<ul>${r.points.map((t) => `<li>${inline(t)}</li>`).join('')}</ul>` : ''}</section>` : '');
  return item.matters || item.needs_you ? `<div class="reasons">${reason('Why it matters', item.matters)}${reason('Why you, not the agent', item.needs_you)}</div>` : '';
}
// The item pane: a fixed head and tabs, one scrolling tab panel, and the answer docked
// below it, so what you are asked to do is always in view.
const FOCUS_KEYS = ['id', 'data-tab', 'data-choice', 'data-note', 'data-wide', 'data-report', 'data-retry', 'data-check', 'data-copy-step', 'data-move', 'data-done'];
function focusKey(el) {
  const name = FOCUS_KEYS.find((n) => el.hasAttribute?.(n));
  return name ? {selector: `[${name}="${CSS.escape(el.getAttribute(name))}"]`, start: el.selectionStart, end: el.selectionEnd} : null;
}
function renderDetail() {
  const i = route.item && model.items.find((x) => x.id === route.item);
  const pane = $('#detail');
  pane.hidden = !i;
  if (!i) {renderDetail.shown = null; return (pane.innerHTML = '');}
  // A re-render of the same item and tab keeps your place and your focus.
  const shown = `${i.id}\n${tabOf(i)}`, body = pane.querySelector('.d-body');
  const top = renderDetail.shown === shown && body ? body.scrollTop : 0;
  const same = renderDetail.shown?.startsWith(`${i.id}\n`);
  const focus = same && pane.contains(document.activeElement) ? focusKey(document.activeElement) : null;
  pane.innerHTML = detailHTML(i);
  renderDetail.shown = shown;
  const again = pane.querySelector('.d-body');
  if (again) again.scrollTop = top;
  pane.querySelectorAll('.composer textarea').forEach(grow);
  const target = focus && pane.querySelector(focus.selector);
  if (target) {
    target.focus({preventScroll: true});
    if (focus.start != null && target.setSelectionRange) target.setSelectionRange(focus.start, focus.end);
  }
}
const optionsOf = (i) => (i.options || []).map((raw) => (typeof raw === 'string' ? {label: raw} : raw));
function tabsOf(i) {
  const steps = (i.steps || []).length, questions = (i.comments || []).filter((c) => c.author === 'human').length;
  return [
    ['steps', 'Steps', steps, steps || (i.paste || []).length || (i.link && safeURL(i.link.url))],
    ['why', 'Why', 0, i.recommendation || i.matters || i.needs_you],
    ['context', 'Context', 0, true],
    ['ask', 'Ask', questions, true],
  ].filter((t) => t[3]).map(([id, label, n]) => ({id, label, n}));
}
// The tab you chose for an item, while the page lives; otherwise Steps, else Why.
function tabOf(i, draft = draftOf(i.id)) {
  const tabs = tabsOf(i);
  return tabs.some((t) => t.id === draft.tab) ? draft.tab : tabs[0].id;
}
// free: no option picked, so the box is the answer. picked: an option, sent by the wide
// button. note: an option plus a note, which typed text always becomes.
function answerState(i, draft) {
  if (!draft.choice || !optionsOf(i).some((o) => o.label === draft.choice)) return 'free';
  return draft.note || (draft.text || '').trim() ? 'note' : 'picked';
}
const wideLabel = (choice, note) => `Send: ${choice}${note ? ' + note' : ''}`;
// What the agent receives: the option, the note, or both, as replies always have.
function replyText(i, draft, typed) {
  const state = answerState(i, draft);
  return [state === 'free' ? '' : draft.choice, state === 'picked' ? '' : typed.trim()].filter(Boolean).join(' — ');
}
// Escape in an empty note closes the note, not the item.
const noteEscape = (i, draft, typed) => answerState(i, draft) === 'note' && !typed.trim();
// Enter sends and Shift+Enter starts a new line; never mid-composition in an input method.
function composeKey(e) {
  if (e.key !== 'Enter' || e.isComposing || e.keyCode === 229) return null;
  return e.shiftKey || e.altKey ? 'newline' : 'send';
}
function replyPlaceholder(i, draft, state) {
  if (state === 'note') return `Add a note to “${draft.choice}”…`;
  if (optionsOf(i).length) return 'Or answer in your own words…';
  return i.owner === 'external' ? 'What came back?' : i.kind === 'todo' ? 'Tell the agent what you did…' : 'Reply to the agent…';
}
function detailHTML(i) {
  const visible = visibleIds(), at = visible.indexOf(i.id);
  const p = projectOf(i.project);
  const lone = i.workstream === OTHER && (p?.workstreams.length || 0) <= 1;
  const crumbs = `<button data-href="${esc(href({page: 'project', project: i.project, lane: i.lane, item: null}))}">${esc(i.project_name)}</button>${lone ? '' : ` › <button data-href="${esc(href({page: 'workstream', project: i.project, workstream: i.workstream, lane: i.lane, item: null}))}">${esc(i.workstream_name)}</button>`}`;
  const draft = draftOf(i.id), tab = tabOf(i, draft);
  const tabs = tabsOf(i).map((t) => `<button class="d-tab" role="tab" id="d-tab-${t.id}" data-tab="${t.id}" aria-selected="${t.id === tab}" aria-controls="d-panel" tabindex="${t.id === tab ? 0 : -1}">${t.label}${t.n ? `<span class="n">${t.n}</span>` : ''}</button>`).join('');
  return `<div class="d-bar"><div class="crumbs">${crumbs}</div>
      ${i.status === 'open' ? `<button class="icon-btn" data-pin aria-pressed="${Boolean(i.pinned)}" aria-label="${i.pinned ? 'Unpin' : 'Pin'}" title="${i.pinned ? 'Unpin' : 'Pin to the top'}">${icon('pin', i.pinned ? 'filled' : '')}</button>` : ''}
      <button class="icon-btn" data-step="-1" aria-label="Previous item" ${at <= 0 ? 'disabled' : ''}>${icon('up')}</button>
      <button class="icon-btn" data-step="1" aria-label="Next item" ${at < 0 || at >= visible.length - 1 ? 'disabled' : ''}>${icon('down')}</button>
      <button class="icon-btn" data-close aria-label="Close">${icon('x')}</button></div>
    <div class="d-head">
      <div class="d-state">${i.due && i.lane !== 'done' && daysUntil(i.due) < 0 ? `<span class="overdue">${esc(stateLine(i))}</span>` : esc(stateLine(i))}</div>
      <h2 class="d-title" id="detail-title">${esc(i.title)}</h2>
    </div>
    <div class="d-tabs" role="tablist" aria-label="About this item">${tabs}</div>
    <div class="d-body scroll" id="d-panel" role="tabpanel" aria-labelledby="d-tab-${tab}"><div class="d-panel-in">${panelHTML(i, draft, tab)}</div></div>
    ${dockHTML(i, draft)}`;
}
function panelHTML(i, draft, tab) {
  const recommended = i.recommendation ? `<div class="d-section"><div class="d-label">Recommended</div>${blocks(i.recommendation)}</div>` : '';
  if (tab === 'why') return recommended + reasonsHTML(i);
  if (tab === 'ask') return askHTML(i, draft);
  if (tab === 'context') return contextHTML(i);
  return `${planHTML(i, draft)}
    ${i.link && safeURL(i.link.url) ? `<div class="d-actions"><a class="pill outline" href="${esc(i.link.url)}" target="_blank" rel="noreferrer">${esc(i.link.label)}${icon('out', 'sm')}</a></div>` : ''}
    ${(i.paste || []).map((b, n) => `<section class="copy"><div class="copy-head"><span>${esc(b.label)}</span><button class="pill ghost sm" data-copy="${n}">Copy</button></div><pre class="${b.mono ? 'mono' : ''}">${esc(b.text)}</pre></section>`).join('')}
    ${recommended}`;
}
function contextHTML(i) {
  const parts = [
    i.detail ? `<div class="d-text">${blocks(i.detail)}</div>` : '',
    i.unlocks ? `<div class="d-section"><div class="d-label">Unlocks</div>${blocks(i.unlocks)}</div>` : '',
    i.agent_note ? `<div class="d-section"><div class="d-label">From the agent</div>${blocks(i.agent_note)}</div>` : '',
  ].join('');
  return `${parts || '<p class="d-quiet">No more context from the agent.</p>'}
    <dl class="from">
      <dt>Agent</dt><dd><span class="dot ${agentState(i.session)}"></span><a href="${esc(href({page: 'agent', agent: i.session_id, lane: 'you', item: null}))}">${esc(i.session_name)}</a><span>· ${esc(harness(i.provider))}</span></dd>
      ${i.source ? `<dt>Source</dt><dd>${esc(i.source)}</dd>` : ''}
      ${i.tags.length ? `<dt>Tags</dt><dd>${esc(i.tags.join(', '))}</dd>` : ''}
      <dt>Added</dt><dd>${fmtDate(i.created)} · ${ago(i.created)}</dd>
    </dl>
    ${i.status !== 'done' && i.owner !== 'agent' && !i.asking ? '<button class="hard-to-read" data-hard>Hard to read? Send it back to the agent</button>' : ''}`;
}
// One composer everywhere: the reply, a note on an option, and a question.
function composeHTML({id, draft, value, placeholder, label, send, ready}) {
  return `<div class="compose"><div class="composer"><textarea id="${id}" data-draft="${draft}" rows="1" maxlength="20000" placeholder="${esc(placeholder)}" aria-label="${label}" enterkeyhint="send">${esc(value)}</textarea><button class="send" type="submit" aria-label="${send}" ${ready ? '' : 'disabled'}>${icon('send')}</button></div></div>`;
}
const foldHTML = (name, open, inner) => `<div class="d-fold ${open ? 'open' : ''}" data-fold="${name}" ${open ? '' : 'inert'}><div class="d-fold-in">${inner}</div></div>`;
function dockHTML(i, draft) {
  const inner = `${sentHTML(i)}${answerHTML(i, draft)}${movesHTML(i)}`;
  return inner.trim() ? `<div class="d-dock">${inner}</div>` : '';
}
function sentHTML(i) {
  const failed = i.delivery === 'failed';
  const retry = failed ? '<div class="go-row"><button class="wide" type="button" data-retry><span>Retry delivery</span></button></div>' : '';
  if (i.skipped) return `<div class="win skipped ${failed ? 'failed' : ''}">${icon('x', 'sm')}<span><strong>You skipped this</strong> · ${esc(i.delivery === 'received' ? 'the agent knows to drop it' : failed ? `couldn't tell the agent${i.delivery_error ? `: ${i.delivery_error}` : ''}` : 'the agent will be told when it next checks in')}</span></div>${retry}`;
  if (i.answer) return `<div class="win ${failed ? 'failed' : ''}">${icon(failed ? 'x' : 'check', 'sm')}<span><strong>${esc(i.answer)}</strong> · ${esc(failed ? `${deliveryText(i)}${i.delivery_error ? `: ${i.delivery_error}` : ''}` : deliveryText(i))}</span></div>${retry}`;
  return '';
}
function answerHTML(i, draft) {
  if (i.status !== 'open' || i.owner === 'agent') return '';
  return isReadable(i) && checksOf(i).length ? reportHTML(i, draft) : respondHTML(i, draft);
}
function respondHTML(i, draft) {
  const options = optionsOf(i), state = answerState(i, draft), text = draft.text ?? '';
  const rows = options.map((o, n) => `<button type="button" class="choice" role="radio" data-choice="${esc(o.label)}" aria-checked="${draft.choice === o.label}"><span class="key">${n + 1}</span><span><span class="c-label">${esc(o.label)}</span>${o.recommended ? '<span class="c-rec">Recommended</span>' : ''}${o.note ? `<span class="c-note">${esc(o.note)}</span>` : ''}</span></button>`).join('');
  const go = `<div class="go-row"><button type="button" class="ghost-pill" data-note aria-pressed="${state === 'note'}">${state === 'note' ? 'Remove note' : `${icon('plus', 'sm')}Note`}</button><button type="submit" class="wide" data-wide><span>${esc(wideLabel(draft.choice, state === 'note' && text.trim()))}</span></button></div>`;
  return `<form class="respond" id="respond" data-state="${state}">
    ${options.length ? `<div class="choices" role="radiogroup" aria-label="Options">${rows}</div>` : ''}
    ${foldHTML('note', state !== 'picked', composeHTML({id: 'reply', draft: 'text', value: text, placeholder: replyPlaceholder(i, draft, state), label: state === 'note' ? 'Note' : 'Reply', send: 'Send', ready: Boolean(text.trim())}))}
    ${options.length ? foldHTML('go', state !== 'free', go) : ''}
    <p class="form-error" aria-live="polite"></p></form>`;
}
// Checks end in Looks right or Something was off, which swaps the row for the box, filled
// with what you didn't tick. Escape, before you change it, brings the row back.
function missedText(i, draft) {
  const checks = checksOf(i), ticks = draft.checks || [];
  const missed = checks.filter((c) => !ticks.includes(c.key)).map((c) => c.text);
  return missed.length && missed.length < checks.length ? `Didn't see: ${missed.join('; ')}` : '';
}
function reportHTML(i, draft) {
  const checks = checksOf(i), ticks = draft.checks || [];
  const ticked = checks.filter((c) => ticks.includes(c.key)).length;
  if (openMove !== 'off') return `<div class="respond" data-state="report"><div class="go-row"><button type="button" class="ghost-pill" data-report="off">Something was off</button><button type="button" class="wide" data-report="ok"><span>${esc(lookedRight(ticked, checks.length, true))}</span></button></div></div>`;
  const text = draft.text ?? missedText(i, draft);
  return `<form class="respond" id="respond" data-state="off">
    ${composeHTML({id: 'reply', draft: 'text', value: text, placeholder: 'What was off?', label: 'What was off', send: 'Send', ready: Boolean(text.trim())})}
    <p class="form-error" aria-live="polite"></p></form>`;
}
// Escape leaves Something was off while the box still holds only what was filled in for you.
const offEscape = (i, draft, typed) => !typed.trim() || typed === missedText(i, draft);
// The button's words, and the reply it sends: honest about checks left unticked.
function lookedRight(ticked, total, button = false) {
  if (ticked === total) return button ? 'Looks right' : `All ${total} looked right`;
  return button ? `Looks right (${ticked} of ${total} ticked)` : `Looked right (${ticked} of ${total} ticked)`;
}
function movesHTML(i) {
  if (i.status !== 'open') return i.status === 'answered' && i.delivery === 'received' && !i.asking ? '<div class="moves"><button class="pill outline sm" data-done>Mark done</button></div>' : '';
  const draft = draftOf(i.id);
  const quiet = [];
  if (i.lane === 'later') quiet.push('<button class="link" data-forward>Bring back now</button>');
  else quiet.push('<button class="link" data-move="later">Later</button>');
  quiet.push('<button class="link" data-move="due">Due date</button>');
  if (i.owner !== 'human') quiet.push('<button class="link" data-needs-me>Needs me now</button>');
  quiet.push('<button class="link" data-move="owner">Hand off</button>');
  quiet.push('<button class="link" data-move="skip">Skip</button>');
  // A decision is answered or skipped, not marked done; checks end in Looks right.
  if (!optionsOf(i).length && (!isReadable(i) || !checksOf(i).length)) quiet.push('<button class="link" data-done>Mark done</button>');
  const owner = draft.owner || i.owner;
  const form = openMove === 'skip' ? `<form class="inline-form" id="skip-form"><input class="field" name="reason" data-draft="skip" maxlength="2000" placeholder="Why skip? (optional)" value="${esc(draft.skip || '')}"><button class="pill primary sm">Skip it</button><button type="button" class="pill ghost sm" data-move="skip">Cancel</button><span class="hint">Closes it and tells the agent to drop it.</span></form>`
    : openMove === 'due' ? `<form class="inline-form" id="due-form"><input class="field" type="date" name="due" data-draft="due" required value="${esc(draft.due ?? i.due ?? '')}"><button class="pill primary sm">Set</button>${i.due ? '<button type="button" class="pill ghost sm" data-clear-due>Clear</button>' : ''}</form>`
    : openMove === 'later' ? `<form class="inline-form" id="later-form"><input class="field" type="date" name="defer_until" data-draft="later" min="${today()}" required value="${esc(draft.later ?? i.defer_until)}"><button class="pill primary sm">Snooze</button></form>`
    : openMove === 'owner' ? `<form class="inline-form" id="owner-form"><select class="field" name="owner" data-draft="owner">${[['human', 'Needs you'], ['agent', 'With the agent'], ['external', 'Waiting on someone']].map(([v, l]) => `<option value="${v}" ${owner === v ? 'selected' : ''}>${l}</option>`).join('')}</select><input class="field" name="waiting_on" data-draft="waiting" placeholder="Who or what" value="${esc(draft.waiting ?? i.waiting_on)}" ${owner === 'external' ? 'required' : 'hidden'}><button class="pill primary sm">Move</button></form>` : '';
  return `<div class="notnow"><span>Not now?</span>${quiet.join('')}</div>${form}<p class="form-error" id="move-error" aria-live="polite"></p>`;
}
// The Ask tab is the thread itself: questions to the agent and its answers, then the box.
function askHTML(i, draft) {
  const comments = i.comments || [];
  const questions = comments.filter((c) => c.author === 'human');
  const thread = questions.map((q) => {
    const answer = comments.find((c) => c.parent_id === q.id);
    const status = q.answered || answer ? '' : ({pending: 'queued for the agent', sending: 'sending…', sent: 'sent, not yet confirmed', received: 'waiting for a reply', failed: 'delivery failed', cancelled: 'cancelled'})[q.state] || '';
    const actions = !q.answered && !answer && q.state !== 'cancelled' ? `<div class="moves">${q.state === 'failed' ? `<button class="pill outline sm" data-comment-action="retry" data-comment-id="${esc(q.id)}">Retry delivery</button>` : ''}<button class="pill ghost sm" data-comment-action="cancel" data-comment-id="${esc(q.id)}">Cancel question</button></div>` : '';
    return `<div class="msg"><div class="who">You${status ? ` · ${esc(status)}` : ''}</div><p>${esc(q.text)}</p>${q.error ? `<p class="form-error">${esc(q.error)}</p>` : ''}${actions}</div>${answer ? `<div class="msg"><div class="who">Agent</div><p>${inline(answer.text)}</p></div>` : ''}`;
  }).join('');
  const box = i.status !== 'done' ? `<form id="ask-form" class="ask-form">${composeHTML({id: 'question', draft: 'question', value: draft.question || '', placeholder: 'Ask about this item…', label: 'Question', send: 'Ask', ready: Boolean((draft.question || '').trim())})}<p class="form-error" aria-live="polite"></p></form>` : '';
  return `<div class="ask"><p class="d-quiet">Questions wait here until the agent replies. Your answer below stays as it is.</p>${thread ? `<div class="thread">${thread}</div>` : ''}${box}</div>`;
}
function visibleIds() {return [...document.querySelectorAll('#main [data-item]')].map((el) => el.dataset.item);}
const nextVisible = (id) => {const ids = visibleIds(); return ids[ids.indexOf(id) + 1] || null;};
// After you act on an item. The dashboard keeps a reply on screen to show its
// delivery; the floating panel replaces this to move on to the next one.
let moveOn = (next, replied) => {if (!replied) go({item: next});};
function openItem(id) {
  openMove = null;
  if (!seen.has(id)) {
    seen.add(id);
    remember('hq-seen', [...seen].slice(-3000));
  }
  go({item: id});
}

// Data
async function api(path, body) {
  const response = await fetch(`/api/${path}`, {method: body === undefined ? 'GET' : 'POST', headers: {'X-HQ-Token': token, 'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'HQ could not save this change');
  return data;
}
function refresh(force = false) {
  if (loading) return force ? loading.then(() => refresh(true)) : loading;
  loading = api('state').then(applyState).catch(() => setLive(false)).finally(() => {loading = null;});
  return loading;
}
function setLive(healthy) {
  if (healthy) lastLive = Date.now();
  const changed = liveHealthy !== healthy;
  liveHealthy = healthy;
  if (changed && booted) render({background: true});
}
function applyState(next) {
  if (next.instance) {
    if (state.instance === next.instance && next.revision < state.revision) return;
    if (state.instance && state.instance !== next.instance && seenInstances.has(next.instance)) return;
    seenInstances.add(next.instance);
  }
  setLive(true);
  const signature = JSON.stringify({items: next.items, usage: next.usage, sessions: next.sessions.map((s) => [s.id, s.name, s.provider, s.connection, s.route, s.activity?.state, s.activity?.source])});
  state = next;
  if (signature !== snapshotSignature) {
    snapshotSignature = signature;
    if (booted) render({background: true});
  }
}
function streamEvent(frame) {
  const type = frame.split('\n').find((line) => line.startsWith('event:'))?.slice(6).trim();
  const data = frame.split('\n').filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trimStart()).join('\n');
  if (type === 'state') applyState(JSON.parse(data));
  else if (type === 'ping') setLive(JSON.parse(data).healthy !== false);
}
async function renewToken() {
  const response = await fetch('/', {cache: 'no-store'});
  if (!response.ok) throw new Error('HQ unavailable');
  const match = (await response.text()).match(/name="hq-token" content="([^"]+)"/);
  if (!match) throw new Error('HQ token unavailable');
  token = match[1];
}
async function watchState() {
  let delay = 300;
  while (true) {
    const controller = new AbortController();
    streamController = controller;
    try {
      const response = await fetch('/api/events', {headers: {'X-HQ-Token': token}, signal: controller.signal, cache: 'no-store'});
      if (response.status === 403) {await renewToken(); throw new Error('Reconnect with new token');}
      if (!response.ok || !response.body) throw new Error('HQ stream unavailable');
      const reader = response.body.getReader(), decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const {value, done} = await reader.read();
        if (done) throw new Error('HQ stream closed');
        buffer += decoder.decode(value, {stream: true});
        if (buffer.length > 8 * 1024 * 1024) throw new Error('HQ snapshot too large');
        let end;
        while ((end = buffer.indexOf('\n\n')) !== -1) {
          streamEvent(buffer.slice(0, end));
          buffer = buffer.slice(end + 2);
          delay = 300;
        }
      }
    } catch {
      controller.abort();
      setLive(false);
    }
    await new Promise((resolve) => setTimeout(resolve, delay));
    delay = Math.min(delay * 2, 5000);
  }
}
// One way to save: post, then refresh so the stream and the page agree.
async function act(request, message, errorTarget) {
  try {
    await request;
    await refresh(true);
    if (message) toast(message);
    return true;
  } catch (error) {
    if (errorTarget) errorTarget.textContent = error.message;
    else toast(error.message);
    return false;
  }
}
const update = (id, patch, message) => act(api(`items/${id}/update`, patch), message, $('#move-error'));

// Dialogs
function connectDialog() {
  const d = $('#dialog');
  d.innerHTML = `<button class="icon-btn close" data-dclose aria-label="Close">${icon('x')}</button><h2>Connect agents</h2><p class="sub">Agents report work here and pick up your replies.</p>
    <div class="step"><h3>Agent tools</h3><code>hq install all</code><small>Review Codex hooks, then reload tools or resume.</small></div>
    <div class="step"><h3>Codex desktop</h3><code>hq desktop install</code><small>Quit Codex, then open Codex + HQ from Applications. Use that launcher on future starts.</small></div>
    <div class="step"><h3>Terminal agents</h3><code>hq launch codex<br>hq launch claude-code</code><small>Claude asks you to enable its preview channel.</small></div>
    <div class="step"><h3>Then tell your agent</h3><code>Register this conversation in HQ.<br>Report pending work and ask for input here.</code><small>ChatGPT needs a private tunnel; see the <a href="https://github.com/dhruvyad/HQ#connections" target="_blank" rel="noreferrer">connection guide</a>.</small></div>`;
  d.showModal();
}
function newItemDialog() {
  const d = $('#dialog');
  if (!state.sessions.length) return connectDialog();
  const project = route.project || model.projects[0]?.id;
  d.innerHTML = `<button class="icon-btn close" data-dclose aria-label="Close">${icon('x')}</button><h2>New item</h2><p class="sub">Something for you or an agent to pick up.</p>
    <form class="form" id="new-form">
      <label>Title<input class="field" name="title" required maxlength="500" autofocus></label>
      <div class="two"><label>Project<select class="field" name="project" id="new-project">${model.projects.map((p) => `<option value="${esc(p.id)}" ${p.id === project ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select></label><label>Workstream<select class="field" name="workstream" id="new-ws"></select></label></div>
      <div class="two"><label>Next step<select class="field" name="owner" id="new-owner"><option value="human">Needs you</option><option value="agent">With an agent</option><option value="external">Waiting on someone</option></select></label><label>Agent<select class="field" name="session_id" id="new-agent"></select></label></div>
      <label id="new-waiting" hidden>Waiting on<input class="field" name="waiting_on" maxlength="200" placeholder="Person, team or service"></label>
      <label>Context<textarea class="field" name="detail" rows="3"></textarea></label>
      <div class="end"><button type="button" class="pill ghost" data-dclose>Cancel</button><button class="pill primary">Add item</button></div>
      <p class="form-error" aria-live="polite"></p>
    </form>`;
  const fill = () => {
    const p = $('#new-project').value;
    const streams = projectOf(p)?.workstreams || [];
    $('#new-ws').innerHTML = streams.map((w) => `<option value="${esc(w.id === OTHER ? '' : w.name)}" ${w.id === route.workstream ? 'selected' : ''}>${esc(w.name)}</option>`).join('') || '<option value="">Other</option>';
    const sessions = [...state.sessions].sort((a, b) => model.items.filter((i) => i.session_id === b.id && i.project === p).length - model.items.filter((i) => i.session_id === a.id && i.project === p).length);
    $('#new-agent').innerHTML = sessions.map((s) => `<option value="${esc(s.id)}">${esc(s.name)} · ${esc(harness(s.provider))}</option>`).join('');
  };
  $('#new-project').onchange = fill;
  $('#new-owner').onchange = () => {const external = $('#new-owner').value === 'external'; $('#new-waiting').hidden = !external; $('#new-waiting input').required = external;};
  fill();
  $('#new-form').onsubmit = async (e) => {
    e.preventDefault();
    const fields = Object.fromEntries(new FormData(e.target));
    try {
      const item = await api('items', {...fields, kind: 'todo'});
      d.close();
      await refresh(true);
      go({page: 'workstream', project: item.project, workstream: item.workstream, lane: laneOf(item), item: item.id});
      toast('Item added');
    } catch (error) {
      e.target.querySelector('.form-error').textContent = error.message;
    }
  };
  d.showModal();
}

// Events
function bind() {
  const main = $('#main'), side = $('#sidebar'), detail = $('#detail');
  main.querySelectorAll('[data-item]').forEach((el) => (el.onclick = () => openItem(el.dataset.item)));
  document.querySelectorAll('[data-href]').forEach((el) => (el.onclick = () => (location.hash = el.dataset.href)));
  main.querySelectorAll('[data-lane]').forEach((el) => (el.onclick = () => go({lane: el.dataset.lane, item: null})));
  main.querySelectorAll('[data-new]').forEach((el) => (el.onclick = newItemDialog));
  main.querySelectorAll('[data-sort]').forEach((el) => (el.onclick = () => {sortMode = el.dataset.sort; remember('hq-sort', sortMode); render();}));
  const quiet = main.querySelector('[data-quiet]');
  if (quiet) quiet.onclick = () => {showQuiet = true; render();};
  const more = main.querySelector('[data-more]');
  if (more) more.onclick = () => {doneLimit += 200; render();};
  const menu = $('#menu');
  if (menu) menu.onclick = () => {sheetOpen = true; render();};
  side.querySelectorAll('[data-project]').forEach((el) => (el.onclick = () => {
    const id = el.dataset.project;
    if (route.page === 'project' && route.project === id) {
      folded.has(id) ? folded.delete(id) : folded.add(id);
      remember('hq-folded', [...folded]);
      return render();
    }
    folded.delete(id);
    go({page: 'project', project: id, workstream: null, agent: null, lane: 'you', item: null});
  }));
  $('#connect').onclick = connectDialog;
  const input = $('#search');
  input.oninput = () => {search = input.value.trim(); render();};
  if (route.item && !detail.hidden) bindDetail(detail);
}
// One line is 42px: 20px of text and 22px of padding. It grows to six lines (142px), then scrolls.
function grow(textarea) {
  if (!textarea?.offsetParent) return;
  textarea.style.height = '42px';
  const natural = textarea.scrollHeight;
  textarea.style.height = `${Math.max(42, Math.min(natural, 142))}px`;
  textarea.classList.toggle('overflow', natural > 142);
  textarea.closest('.composer')?.classList.toggle('multi', natural > 44);
}
const stillMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
function setSendable(button, ready) {
  if (!button || button.disabled === !ready) return;
  button.disabled = !ready;
  if (!ready || stillMotion()) return;
  const bounce = getComputedStyle(document.documentElement).getPropertyValue('--f-spring').trim() || 'cubic-bezier(.34,1.56,.64,1)';
  button.animate([{transform: 'scale(.86)'}, {transform: 'scale(1)'}], {duration: 420, easing: bounce});
}
function setFold(fold, open) {
  if (!fold || fold.classList.contains('open') === open) return;
  fold.classList.toggle('open', open);
  fold.inert = !open;
}
// Picking, unpicking and adding a note change the answer in place, so the box can fold
// away and come back with your text and focus intact.
function syncRespond(form, i) {
  if (!form || !i || !['free', 'picked', 'note'].includes(form.dataset.state)) return;
  const draft = draftOf(i.id), state = answerState(i, draft), text = (draft.text || '').trim();
  form.dataset.state = state;
  form.querySelectorAll('[data-choice]').forEach((c) => c.setAttribute('aria-checked', String(c.dataset.choice === draft.choice)));
  setFold(form.querySelector('[data-fold="note"]'), state !== 'picked');
  setFold(form.querySelector('[data-fold="go"]'), state !== 'free');
  const reply = form.querySelector('#reply');
  reply.placeholder = replyPlaceholder(i, draft, state);
  reply.setAttribute('aria-label', state === 'note' ? 'Note' : 'Reply');
  setSendable(form.querySelector('.composer .send'), Boolean(text));
  const pill = form.querySelector('[data-note]');
  if (pill) {
    pill.innerHTML = state === 'note' ? 'Remove note' : `${icon('plus', 'sm')}Note`;
    pill.setAttribute('aria-pressed', String(state === 'note'));
  }
  const wide = form.querySelector('[data-wide] span');
  if (wide && draft.choice) wide.textContent = wideLabel(draft.choice, state === 'note' && text);
}
function bindDetail(detail) {
  const id = route.item, item = () => model.items.find((x) => x.id === id);
  const rerender = () => {renderDetail(); bindDetail(detail);};
  const respond = detail.querySelector('#respond'), reply = detail.querySelector('#reply');
  detail.querySelector('[data-close]').onclick = () => go({item: null});
  detail.querySelectorAll('[data-step]').forEach((el) => (el.onclick = () => step(Number(el.dataset.step))));
  const tabs = [...detail.querySelectorAll('[data-tab]')];
  const pick = (tab) => {setDraft(id, {tab: tab.dataset.tab}); rerender(); detail.querySelector(`[data-tab="${tab.dataset.tab}"]`)?.focus();};
  tabs.forEach((el, n) => {
    el.onclick = () => pick(el);
    el.onkeydown = (e) => {
      const to = {ArrowRight: n + 1, ArrowLeft: n - 1, Home: 0, End: tabs.length - 1}[e.key];
      if (to === undefined) return;
      e.preventDefault();
      pick(tabs[(to + tabs.length) % tabs.length]);
    };
  });
  detail.oninput = (e) => {
    const key = e.target.dataset?.draft;
    if (!key) return;
    setDraft(id, {[key]: e.target.value});
    if (e.target.matches('.composer textarea')) {
      grow(e.target);
      setSendable(e.target.closest('.composer').querySelector('.send'), Boolean(e.target.value.trim()));
    }
    if (key === 'text') syncRespond(respond, item());
    if (key === 'owner') {
      const waiting = detail.querySelector('#owner-form [name="waiting_on"]');
      waiting.hidden = e.target.value !== 'external';
      waiting.required = e.target.value === 'external';
    }
  };
  detail.querySelectorAll('.composer textarea').forEach((box) => (box.onkeydown = (e) => {
    if (composeKey(e) === 'send') {
      e.preventDefault();
      box.closest('form')?.requestSubmit();
    } else if (e.key === 'Escape' && box.id === 'reply' && noteEscape(item(), draftOf(id), box.value)) {
      // Escape closes an empty note first, and only then the item.
      e.preventDefault();
      e.stopPropagation();
      setDraft(id, {note: false});
      syncRespond(respond, item());
      detail.querySelector('[data-note]')?.focus();
    } else if (e.key === 'Escape' && respond?.dataset.state === 'off' && offEscape(item(), draftOf(id), box.value)) {
      e.preventDefault();
      e.stopPropagation();
      openMove = null;
      setDraft(id, {text: undefined});
      rerender();
      detail.querySelector('[data-report="off"]')?.focus();
    }
  }));
  // ⌘Enter sends from anywhere in the pane: the question if you are asking, else the answer.
  detail.onkeydown = (e) => {
    if (e.defaultPrevented || e.key !== 'Enter' || !(e.metaKey || e.ctrlKey)) return;
    const form = e.target.closest?.('#ask-form') || respond;
    if (form) {e.preventDefault(); form.requestSubmit();}
  };
  detail.querySelectorAll('[data-copy]').forEach((el) => (el.onclick = async () => {
    try {await navigator.clipboard.writeText(item().paste[Number(el.dataset.copy)].text); toast('Copied');} catch {toast('Copy unavailable; select the text instead');}
  }));
  detail.querySelectorAll('[data-copy-step]').forEach((el) => (el.onclick = async () => {
    const n = Number(el.dataset.copyStep);
    try {await navigator.clipboard.writeText(item().steps[n].copy);} catch {return toast('Copy unavailable; select the text instead');}
    setDraft(id, {copied: n});
    rerender();
  }));
  detail.querySelectorAll('[data-check]').forEach((el) => (el.onclick = () => {
    const key = el.dataset.check, ticks = draftOf(id).checks || [];
    setDraft(id, {checks: ticks.includes(key) ? ticks.filter((t) => t !== key) : [...ticks, key]});
    rerender();
  }));
  detail.querySelectorAll('[data-choice]').forEach((el) => (el.onclick = () => {
    const d = draftOf(id), choice = d.choice === el.dataset.choice ? null : el.dataset.choice;
    // Text you already typed becomes the note; unpicking turns it back into your answer.
    setDraft(id, {choice, note: Boolean(choice && (d.note || (d.text || '').trim()))});
    syncRespond(respond, item());
  }));
  const note = detail.querySelector('[data-note]');
  if (note) note.onclick = () => {
    const open = answerState(item(), draftOf(id)) === 'note';
    if (open) {
      setDraft(id, {note: false, text: ''});
      reply.value = '';
      grow(reply);
    } else setDraft(id, {note: true});
    syncRespond(respond, item());
    if (!open) reply.focus({preventScroll: true});
  };
  if (respond) respond.onsubmit = async (e) => {
    e.preventDefault();
    const text = replyText(item(), draftOf(id), reply.value);
    if (!text) return;
    const next = nextVisible(id);
    if (await act(api(`items/${id}/reply`, {text}), 'Reply saved for the agent', respond.querySelector('.form-error'))) {
      setDraft(id, {text: undefined, choice: undefined, note: undefined});
      openMove = null;
      // Sending from the box leaves focus in it, which holds off live refreshes; show the result now.
      if (route.item === id) rerender();
      moveOn(next, true);
    }
  };
  const ok = detail.querySelector('[data-report="ok"]');
  if (ok) ok.onclick = async () => {
    const next = nextVisible(id), checks = checksOf(item()), ticks = draftOf(id).checks || [];
    const text = lookedRight(checks.filter((c) => ticks.includes(c.key)).length, checks.length);
    if (await act(api(`items/${id}/reply`, {text}), 'Reply saved for the agent')) {
      openMove = null;
      if (route.item === id) rerender();
      moveOn(next, true);
    }
  };
  const off = detail.querySelector('[data-report="off"]');
  // Something was off puts the cursor at the end of what you missed, so Enter sends it.
  if (off) off.onclick = () => {
    openMove = 'off';
    rerender();
    const box = detail.querySelector('#reply');
    if (box) {box.focus({preventScroll: true}); box.setSelectionRange(box.value.length, box.value.length);}
  };
  detail.querySelectorAll('[data-done]').forEach((el) => (el.onclick = async () => {
    const next = nextVisible(id);
    if (await act(api(`items/${id}/resolve`, {}), 'Marked done')) moveOn(next, false);
  }));
  const pin = detail.querySelector('[data-pin]');
  if (pin) pin.onclick = () => update(id, {pinned: !item().pinned}, item().pinned ? 'Unpinned' : 'Pinned');
  const forward = detail.querySelector('[data-forward]');
  if (forward) forward.onclick = () => update(id, {defer_until: ''}, 'Brought back');
  const needsMe = detail.querySelector('[data-needs-me]');
  if (needsMe) needsMe.onclick = () => update(id, {owner: 'human', defer_until: ''}, 'Moved to Needs you');
  detail.querySelectorAll('[data-move]').forEach((el) => (el.onclick = () => {openMove = openMove === el.dataset.move ? null : el.dataset.move; rerender();}));
  const due = detail.querySelector('#due-form');
  if (due) {
    due.onsubmit = async (e) => {e.preventDefault(); if (await update(id, {due: new FormData(due).get('due')}, 'Due date set')) openMove = null;};
    const clear = due.querySelector('[data-clear-due]');
    if (clear) clear.onclick = async () => {if (await update(id, {due: ''}, 'Due date cleared')) openMove = null;};
  }
  const later = detail.querySelector('#later-form');
  if (later) later.onsubmit = async (e) => {e.preventDefault(); if (await update(id, {defer_until: new FormData(later).get('defer_until')}, 'Snoozed')) openMove = null;};
  const owner = detail.querySelector('#owner-form');
  if (owner) owner.onsubmit = async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(owner));
    if (await update(id, {owner: f.owner, waiting_on: f.owner === 'external' ? f.waiting_on : ''}, 'Moved')) {openMove = null; setDraft(id, {owner: undefined, waiting: undefined});}
  };
  const skip = detail.querySelector('#skip-form');
  if (skip) skip.onsubmit = async (e) => {
    e.preventDefault();
    const next = nextVisible(id);
    if (await act(api(`items/${id}/skip`, {reason: new FormData(skip).get('reason') || ''}), 'Skipped. The agent will be told to drop it', $('#move-error'))) {
      openMove = null;
      setDraft(id, {skip: undefined});
      moveOn(next, false);
    }
  };
  const retry = detail.querySelector('[data-retry]');
  if (retry) retry.onclick = () => act(api(`items/${id}/retry`, {}), 'Queued again');
  detail.querySelectorAll('[data-comment-action]').forEach((el) => (el.onclick = () => act(api(`comments/${el.dataset.commentId}/${el.dataset.commentAction}`, {}), el.dataset.commentAction === 'cancel' ? 'Question cancelled' : 'Queued again')));
  const ask = detail.querySelector('#ask-form');
  if (ask) ask.onsubmit = async (e) => {
    e.preventDefault();
    const text = ask.querySelector('textarea').value.trim();
    if (text && await act(api(`items/${id}/ask`, {text}), 'Question saved', ask.querySelector('.form-error'))) {
      setDraft(id, {question: ''});
      if (route.item === id) rerender();
    }
  };
  const hard = detail.querySelector('[data-hard]');
  if (hard) hard.onclick = () => act(api(`items/${id}/ask`, {text: HARD_TO_READ}), 'Sent back to the agent to rewrite');
}
function step(delta) {
  const ids = visibleIds(), at = ids.indexOf(route.item);
  const next = ids[Math.max(0, Math.min(ids.length - 1, (at < 0 ? -1 : at) + delta))];
  if (next) openItem(next);
  requestAnimationFrame(() => document.querySelector(`#main [data-item="${CSS.escape(next || '')}"]`)?.scrollIntoView({block: 'nearest'}));
}

function boot() {
  booted = true;
  window.addEventListener('hashchange', () => {
    const before = route;
    route = parseRoute();
    if (before.page !== route.page || before.project !== route.project || before.workstream !== route.workstream || before.lane !== route.lane) doneLimit = 40;
    sheetOpen = false;
    render();
  });
  document.addEventListener('keydown', (e) => {
    const typing = e.target.matches('input, textarea, select');
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {e.preventDefault(); $('#search')?.focus(); return;}
    if (e.key === 'Escape') {
      if ($('#dialog').open) return;
      if (typing && e.target.id === 'search' && search) {search = ''; render(); return;}
      if (sheetOpen) {sheetOpen = false; return render();}
      if (route.item) go({item: null});
      return;
    }
    if (typing || $('#dialog').open) return;
    const option = route.item && /^[1-9]$/.test(e.key) && !(e.metaKey || e.ctrlKey || e.altKey) && document.querySelectorAll('#detail [data-choice]')[Number(e.key) - 1];
    if (option) {e.preventDefault(); option.click(); return;}
    if (e.key === 'j' || e.key === 'ArrowDown') {e.preventDefault(); step(1);}
    if (e.key === 'k' || e.key === 'ArrowUp') {e.preventDefault(); step(-1);}
  });
  $('#scrim').onclick = () => {
    if (sheetOpen) {sheetOpen = false; return render();}
    go({item: null});
  };
  $('#dialog').addEventListener('click', (e) => {if (e.target.closest('[data-dclose]') || e.target === $('#dialog')) $('#dialog').close();});
  render();
  refresh();
  watchState();
  setInterval(() => {if (Date.now() - lastLive > 5000) {setLive(false); streamController?.abort(); refresh();}}, 1000);
  setInterval(() => tickTimes(), 1000);
  document.addEventListener('visibilitychange', () => {if (!document.hidden) {streamController?.abort(); refresh();}});
  window.addEventListener('offline', () => {streamController?.abort(); setLive(false);});
  window.addEventListener('online', () => {streamController?.abort(); refresh();});
}
if (document.body.dataset.surface !== 'float') boot();
