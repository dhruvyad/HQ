// The floating panel: a pill that opens into what needs you, then expands to decide.
// app.js owns the data, the item pane and its actions. FloatPanel.swift only moves the
// window, so every rule lives here where Node can test it.
const native = (message) => window.webkit?.messageHandlers?.hq?.postMessage(message);

// Every open design decision, so `hq float lab` can flip each one live (float-lab.js).
const feel = {layout: 'auto', size: 28, style: 'attention', labels: 'spotlight', targets: 'all', ghost: 'pill', backdrop: 'dim', glide: 300, threshold: 5, order: 'lead', away: 'collapse', after: 'next', hold: 4000, width: 480, show: 100000};

// Where the pill can live: [across, down], each the start, center or end of the free screen.
const DOCKS = {
  'top-left': ['start', 'start'], top: ['center', 'start'], 'top-right': ['end', 'start'],
  left: ['start', 'center'], right: ['end', 'center'],
  'bottom-left': ['start', 'end'], bottom: ['center', 'end'], 'bottom-right': ['end', 'end'],
};
const TARGETS = {all: Object.keys(DOCKS), flow: ['top-left', 'left', 'right', 'bottom'], corners: ['top-left', 'top-right', 'bottom-left', 'bottom-right']};
const DEFAULT_DOCK = 'top-left';
const EDGE = 10, BOTTOM_EDGE = 4;
const LIST_WIDTH = 380, LIST_MAX = 520, SETTINGS_MAX = 840, ITEM_MIN = 240, ITEM_MAX = 760;
const CONTROLS = 'button, a, input, textarea, select, summary';
// The window around the pill stays this size, with room for its shadow, so the capsule can
// spring and morph on the compositor. Resizing a window mid-animation can only stutter.
const STAGE = {width: 420, height: 270}, MARGIN = 32, PILL_CHROME = 10;
const CARD = {width: 308, height: 62};
// The open box sits beside the pill, which stays put so answered items can fly home to it.
const GAP = 8, STACK = 22, BOX_MAX = 840, FOLD_MS = 620, FLY_MS = 560;
const BURST_WINDOW = 350, FOLD_FOR = 640, ROLL_AFTER = 260, HOLD_TICK = 100;
// Agents flip between busy and idle on every turn, so a change has to hold before it is news.
const AGENT_START_HOLD = 3, AGENT_STOP_HOLD = 8;
const CONTROL_OPTION = 6144;
const SHORTCUTS = {
  toggle: {name: 'Open or minimize Needs you', key: {keyCode: 4, modifiers: CONTROL_OPTION, label: '⌃⌥H'}},
  next: {name: 'Next item', key: {keyCode: 38, modifiers: CONTROL_OPTION, label: '⌃⌥J'}},
  previous: {name: 'Previous item', key: {keyCode: 40, modifiers: CONTROL_OPTION, label: '⌃⌥K'}},
  dashboard: {name: 'Open the dashboard', key: {keyCode: 2, modifiers: CONTROL_OPTION, label: '⌃⌥D'}},
  customize: {name: 'Customize the pill', key: null},
};
const NOTIFY = {items: 'Items added or removed', agents: 'Agents starting or stopping', usage: 'Usage running low'};
const USAGE_LOW = 10, USAGE_STALE = 3600;
ICONS.more = '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>';

// What a circle can show. Usage comes from the server and stays empty until a real
// source exists: a circle never guesses.
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
function readUsage(name, title) {
  const usage = state.usage?.[name];
  const windows = (usage?.windows || []).filter((w) => Number.isFinite(w.left));
  if (!windows.length) return {value: null, gauge: true, tag: `${title} usage not connected`};
  const tight = windows.reduce((a, b) => (b.left < a.left ? b : a));
  const stale = Date.now() / 1000 - usage.as_of > USAGE_STALE;
  const resets = tight.resets_at ? `, resets ${tight.resets_at - Date.now() / 1000 > 86400 ? fmtDate(tight.resets_at) : new Date(tight.resets_at * 1000).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'})}` : '';
  return {value: tight.left, gauge: true, low: tight.left <= USAGE_LOW, stale,
    tag: `${tight.left}% of ${title}'s ${tight.name} limit left${resets}${stale ? ` (as of ${ago(usage.as_of)})` : ''}`};
}
const METRICS = {
  needs: {name: 'Items that need you', read: (items) => {
    const n = items.length + flying;
    return {value: n, loud: n > 0, tag: `${n} ${n === 1 ? 'needs' : 'need'} you`};
  }},
  decisions: {name: 'Decisions waiting', read: (items) => {
    const n = items.filter((i) => i.kind === 'question' && i.lane === 'you').length;
    return {value: n, tag: plural(n, 'decision waiting', 'decisions waiting')};
  }},
  due: {name: 'Due soon', read: (items) => {
    const n = items.filter(dueSoon).length;
    return {value: n, tag: `${n} due soon`};
  }},
  agents: {name: 'Agents running', read: () => {
    const n = model.agents.filter((a) => agentState(a) === 'active').length;
    return {value: n, live: n > 0, tag: plural(n, 'agent running', 'agents running')};
  }},
  claude: {name: 'Claude usage left', read: () => readUsage('claude', 'Claude')},
  codex: {name: 'Codex usage left', read: () => readUsage('codex', 'Codex')},
};
const DEFAULT_CIRCLES = ['needs', 'agents', 'codex'];
// A saved list can name a metric from another version, so keep only what still exists.
function cleanCircles(saved) {
  const known = (Array.isArray(saved) ? saved : []).filter((id, n, all) => METRICS[id] && all.indexOf(id) === n);
  return known.length ? known : [...DEFAULT_CIRCLES];
}
// The pill always keeps one circle, or there would be nothing to grab.
function toggleCircle(list, id) {
  if (!list.includes(id)) return [...list, id];
  return list.length > 1 ? list.filter((other) => other !== id) : list;
}
function moveCircle(list, id, delta) {
  const from = list.indexOf(id), to = from + delta;
  if (from < 0 || to < 0 || to >= list.length) return list;
  const next = [...list];
  next.splice(from, 1);
  next.splice(to, 0, id);
  return next;
}

let floatOpen = false, dock = DEFAULT_DOCK, display = null, drag = null, placed = '';
let swallowClick = false, customizing = false, hovering = false, circles = [...DEFAULT_CIRCLES];
let flying = 0, holding = null, flown = false, lastMode = 'pill', lastItem = null, lastShown = null, lastValues = {};
let shortcuts = cleanShortcuts(), notify = cleanNotify(), recording = null, refused = [];
let lastItems = null, lastUsage = null, pending = [], notice = null;
const agentMemory = new Map();

function cleanShortcuts(saved) {
  const valid = (key) => Number.isInteger(key?.keyCode) && Number.isInteger(key?.modifiers) && typeof key?.label === 'string';
  return Object.fromEntries(Object.entries(SHORTCUTS).map(([action, entry]) => {
    const chosen = saved?.[action];
    return [action, chosen === null ? null : valid(chosen) ? chosen : entry.key];
  }));
}
function cleanNotify(saved) {
  return Object.fromEntries(Object.keys(NOTIFY).map((kind) => [kind, saved?.[kind] !== false]));
}
// Bindings the window should hold, in the shape it registers.
function hotkeyList(chosen) {
  return Object.entries(chosen).filter(([, key]) => key).map(([action, key]) => ({action, keyCode: key.keyCode, modifiers: key.modifiers}));
}

// What changed. These run on the snapshots, so they say what happened whoever did it.
function describeAdded(i) {
  if (i.delivery === 'failed') return {group: 'items', metric: 'needs', delta: 1, plural: 'failed replies', item: i.id, title: i.title, sub: `Reply failed · ${i.project_name}`, dot: 'bad'};
  const kind = {question: ['New decision', 'new decisions'], blocker: ['New blocker', 'new blockers']}[i.kind] || ['New action', 'new actions'];
  return {group: 'items', metric: 'needs', delta: 1, plural: 'new items', item: i.id, title: i.title, sub: `${kind[0]} · ${i.project_name}`};
}
function describeRemoved(i, latest) {
  const why = !latest || latest.status === 'done' ? (latest?.skipped ? 'Skipped' : 'Done')
    : latest.status === 'answered' ? 'Reply saved'
    : latest.lane === 'later' ? 'Snoozed'
    : latest.asking ? 'Asked the agent'
    : latest.owner === 'agent' ? 'With the agent'
    : latest.owner === 'external' ? 'Waiting on someone' : 'Moved';
  return {group: 'items', metric: 'needs', delta: -1, plural: 'items cleared', title: i.title, sub: `${why} · ${i.project_name}`};
}
function itemChanges(before, after, all = []) {
  const was = new Set(before.map((i) => i.id)), now = new Set(after.map((i) => i.id));
  const latest = new Map(all.map((i) => [i.id, i]));
  return [...after.filter((i) => !was.has(i.id)).map(describeAdded),
    ...before.filter((i) => !now.has(i.id)).map((i) => describeRemoved(i, latest.get(i.id)))];
}
// Unknown says nothing, so a lost connection can never read as every agent stopping.
function trackAgents(memory, agents, now, settled = true) {
  const events = [];
  for (const a of agents) {
    if (!['active', 'idle', 'waiting', 'offline'].includes(a.state)) continue;
    const running = a.state === 'active', seen = memory.get(a.id);
    if (!seen) {
      // A conversation that appears already running has just started; at startup it is only the baseline.
      memory.set(a.id, settled ? {running: false, candidate: running, since: now} : {running, candidate: running, since: now});
      continue;
    }
    if (running === seen.running || running !== seen.candidate) {
      seen.candidate = running;
      seen.since = now;
      continue;
    }
    if (now - seen.since < (running ? AGENT_START_HOLD : AGENT_STOP_HOLD)) continue;
    seen.running = running;
    events.push({group: 'agents', metric: 'agents', delta: running ? 1 : -1, plural: running ? 'agents started' : 'agents stopped',
      title: `${a.name} ${running ? 'started' : 'stopped'}`, sub: a.harness, dot: running ? 'on' : 'off'});
  }
  return events;
}
const tightest = (usage) => {
  const windows = (usage?.windows || []).filter((w) => Number.isFinite(w.left));
  return windows.length ? windows.reduce((a, b) => (b.left < a.left ? b : a)) : null;
};
// Only the moment it crosses, so it says this once rather than on every reading.
function usageChanges(before, after) {
  return [['claude', 'Claude'], ['codex', 'Codex']].flatMap(([id, title]) => {
    const was = tightest(before?.[id]), now = tightest(after?.[id]);
    if (!was || !now || was.left <= USAGE_LOW || now.left > USAGE_LOW) return [];
    return [{group: 'usage', metric: id, delta: now.left - was.left, plural: 'limits running low', title: `${title} is nearly out`, sub: `${now.left}% of the ${now.name} limit left`, dot: 'bad'}];
  });
}
// A burst becomes one card, so the pill never plays catch-up with a queue.
function coalesce(events) {
  if (events.length === 1) return events[0];
  const counts = new Map();
  for (const e of events) counts.set(e.plural, (counts.get(e.plural) || 0) + 1);
  const delta = events.filter((e) => e.metric === events[0].metric).reduce((sum, e) => sum + e.delta, 0);
  return {group: events[0].group, metric: events[0].metric, delta, title: `${events.length} updates`,
    sub: [...counts].map(([label, n]) => `${n} ${n === 1 ? label.replace(/ies(?= |$)/, 'y').replace(/s(?= |$)/, '') : label}`).join(' · ')};
}
// A real spring, sampled into CSS linear() so the compositor runs it.
function springCurve({stiffness, damping}) {
  const step = 1 / 120, points = [];
  let x = 0, v = 0, still = 0;
  for (let t = 0; t < 3 && still < 12; t += step) {
    v += (-stiffness * (x - 1) - damping * v) * step;
    x += v * step;
    points.push(x);
    still = Math.abs(x - 1) < 0.002 && Math.abs(v) < 0.02 ? still + 1 : 0;
  }
  const every = Math.max(1, Math.floor(points.length / 44));
  return `linear(0, ${points.filter((_, n) => n % every === 0).map((p) => p.toFixed(3)).join(', ')}, 1)`;
}

// Geometry, in display-local points with the origin at the display's top-left.
function dockRect(requested, size, area) {
  const name = DOCKS[requested] ? requested : DEFAULT_DOCK;
  const [across, down] = DOCKS[name];
  const width = Math.min(size.width, area.width - 2 * EDGE), height = Math.min(size.height, area.height - EDGE - BOTTOM_EDGE);
  const x = across === 'start' ? area.x + EDGE : across === 'center' ? area.x + (area.width - width) / 2 : area.x + area.width - width - EDGE;
  const y = down === 'start' ? area.y + EDGE : down === 'center' ? area.y + (area.height - height) / 2 : area.y + area.height - height - BOTTOM_EDGE;
  return {x: Math.round(x), y: Math.round(y), width, height};
}
function unionRect(a, b) {
  const x = Math.min(a.x, b.x), y = Math.min(a.y, b.y);
  return {x, y, width: Math.max(a.x + a.width, b.x + b.width) - x, height: Math.max(a.y + a.height, b.y + b.height) - y};
}
const inflate = (r, m = MARGIN) => ({x: r.x - m, y: r.y - m, width: r.width + 2 * m, height: r.height + 2 * m});
const localRect = (r, room) => ({x: r.x - room.x, y: r.y - room.y, width: r.width, height: r.height});
const clamp = (v, low, high) => Math.max(low, Math.min(high, v));
// Beside the pill and away from the screen edge; above or below it when it lies along an edge.
function boxRect(requested, pill, size, area) {
  const [across, down] = DOCKS[requested] || DOCKS[DEFAULT_DOCK];
  const width = Math.min(size.width, area.width - 2 * EDGE), height = Math.min(size.height, area.height - EDGE - BOTTOM_EDGE);
  let x, y;
  if (across === 'center') {
    x = pill.x + pill.width / 2 - width / 2;
    y = down === 'end' ? pill.y - GAP - height : pill.y + pill.height + GAP;
  } else {
    x = across === 'start' ? pill.x + pill.width + GAP : pill.x - GAP - width;
    y = down === 'start' ? pill.y : down === 'end' ? pill.y + pill.height - height : pill.y + pill.height / 2 - height / 2;
  }
  x = clamp(x, area.x + EDGE, area.x + area.width - EDGE - width);
  y = clamp(y, area.y + EDGE, area.y + area.height - BOTTOM_EDGE - height);
  return {x: Math.round(x), y: Math.round(y), width, height};
}
// Room for everything the pill can become at this dock, plus margin for shadows.
function roomFor(name, rest, area, wide = Math.max(LIST_WIDTH, feel.width)) {
  const stage = dockRect(name, STAGE, area), most = boxRect(name, rest, {width: wide, height: BOX_MAX}, area);
  return inflate(unionRect(unionRect(stage, rest), most));
}
// How the panel should move between two moments.
function motionKind(fromMode, toMode, fromItem, toItem, order = [], answered = false) {
  if (fromMode === 'pill' && toMode !== 'pill') return 'open';
  if (fromMode !== 'pill' && toMode === 'pill') return 'close';
  if (fromMode !== 'item' && toMode === 'item') return 'deal';
  if (fromMode === 'item' && toMode !== 'item') return 'gather';
  if (toMode !== 'item' || fromItem === toItem) return 'none';
  if (answered) return 'answered';
  const a = order.indexOf(fromItem), b = order.indexOf(toItem);
  return a >= 0 && b >= 0 && b < a ? 'prev' : 'next';
}
// Which way the spotlight label opens from the pill.
function spotSide(name, layout = layoutFor(name)) {
  if (layout === 'vertical') return labelSide(name, 'vertical') === 'left' ? 'left' : 'right';
  return (DOCKS[name] || DOCKS[DEFAULT_DOCK])[1] === 'end' ? 'above' : 'below';
}
// The circle nearest the cursor, measured along the pill.
function nearestCircle(centers, x, y, vertical) {
  let best = -1, closest = Infinity;
  centers.forEach((c, n) => {
    const d = Math.abs(vertical ? y - c.y : x - c.x);
    if (d < closest) {closest = d; best = n;}
  });
  return best;
}
function nearestDock(x, y, rects) {
  let best = null, closest = Infinity;
  for (const [name, r] of Object.entries(rects)) {
    const dx = r.x + r.width / 2 - x, dy = r.y + r.height / 2 - y, distance = dx * dx + dy * dy;
    if (distance < closest) {closest = distance; best = name;}
  }
  return best;
}
// Centered on an edge the capsule lies along that edge; everywhere else it stands up.
const layoutFor = (name, layout = feel.layout) => {
  if (layout !== 'auto') return layout;
  return (DOCKS[name] || DOCKS[DEFAULT_DOCK])[0] === 'center' ? 'horizontal' : 'vertical';
};
// The capsule is the same shape either way round, so one measurement sizes both.
function capsuleSize(size, layout) {
  const long = Math.max(size.width, size.height), short = Math.min(size.width, size.height);
  return layout === 'horizontal' ? {width: long, height: short} : {width: short, height: long};
}
const dockRects = (size, area, names = TARGETS[feel.targets] || TARGETS.all) => Object.fromEntries(names.map((name) => [name, dockRect(name, capsuleSize(size, layoutFor(name)), area)]));
const growFrom = (name) => {
  const [across, down] = DOCKS[name] || DOCKS[DEFAULT_DOCK];
  return `${{start: 'left', center: 'center', end: 'right'}[across]} ${{start: 'top', center: 'center', end: 'bottom'}[down]}`;
};

// What the panel shows. A failed delivery needs you even though the item left your lane.
function attention(items = model.items) {
  const order = {
    newest: (a, b) => b.created - a.created,
    lead: (a, b) => leadOf(a) - leadOf(b) || (leadOf(a) === 1 ? a.due.localeCompare(b.due) : 0) || b.created - a.created,
    urgent: (a, b) => leadOf(a) - leadOf(b) || rankOf(a) - rankOf(b) || b.created - a.created,
  }[feel.order];
  const failed = (i) => i.delivery === 'failed' && (i.status !== 'done' || Boolean(i.skipped));
  return items.filter((i) => i.lane === 'you' || failed(i)).sort((a, b) => failed(b) - failed(a) || order(a, b)).slice(0, feel.show);
}
const surfaceMode = (open, item, customize = false) => (!open ? 'pill' : customize ? 'settings' : item ? 'item' : 'list');
// Labels open away from the screen edge, so the circles under your cursor never move.
// A centered pill would shift as it grew, and a sideways one has no room for them.
function labelSide(name, layout = layoutFor(name)) {
  if (layout !== 'vertical') return 'none';
  return {start: 'right', center: 'none', end: 'left'}[(DOCKS[name] || DOCKS[DEFAULT_DOCK])[0]];
}
// Where to go after acting on an item: null is the list, undefined means stay.
function afterAct(after, next, replied) {
  if (after === 'stay' && replied) return undefined;
  return after === 'list' ? null : next;
}

function shortCount(n) {return n > 999 ? '1k+' : String(n);}
function circleHTML(r, style = feel.style) {
  const empty = r.value === null, text = empty ? '–' : shortCount(r.value);
  const classes = ['f-circle', r.gauge && !empty ? 'gauge' : '', empty ? 'none' : '', r.loud && style === 'attention' ? 'loud' : '',
    r.live ? 'live' : '', r.low ? 'low' : '', r.stale ? 'stale' : '', text.length > 2 ? 'wide' : ''].filter(Boolean).join(' ');
  const ring = r.gauge && !empty ? `<svg viewBox="0 0 36 36" aria-hidden="true"><circle class="track" cx="18" cy="18" r="16" pathLength="100"/><circle class="fill" cx="18" cy="18" r="16" pathLength="100" stroke-dasharray="${Number(r.value)} 100"/></svg>` : '';
  return `<span class="${classes}">${ring}<span>${esc(text)}</span></span>`;
}
function pillHTML(items, live = liveHealthy) {
  const rows = circles.map((id) => {
    const r = METRICS[id].read(items);
    return `<span class="f-row" data-metric="${id}">${circleHTML(r)}<span class="f-tag">${esc(r.tag)}</span></span>`;
  }).join('');
  return `${live ? '' : '<span class="f-row f-offline"><span class="dot err"></span><span class="f-tag">Reconnecting…</span></span>'}${rows}`;
}
function pillLabel(items, live = liveHealthy) {
  return `HQ. ${circles.map((id) => METRICS[id].read(items).tag).join('. ')}.${live ? '' : ' Reconnecting.'}`;
}
function settingsHTML(items) {
  const rows = [...circles, ...Object.keys(METRICS).filter((id) => !circles.includes(id))].map((id) => {
    const on = circles.includes(id), at = circles.indexOf(id), r = METRICS[id].read(items);
    const now = r.value === null ? 'Not connected' : r.gauge ? `${r.value}% left` : `${r.value} now`;
    const moves = on ? `<span class="f-moves"><button class="icon-btn" type="button" data-circle-move="-1" data-id="${id}" aria-label="Move ${esc(METRICS[id].name)} up" ${at === 0 ? 'disabled' : ''}>${icon('up')}</button><button class="icon-btn" type="button" data-circle-move="1" data-id="${id}" aria-label="Move ${esc(METRICS[id].name)} down" ${at === circles.length - 1 ? 'disabled' : ''}>${icon('down')}</button></span>` : '';
    return `<div class="f-set"><button type="button" class="check" role="checkbox" data-circle="${id}" aria-checked="${on}" ${on && circles.length === 1 ? 'disabled' : ''}><span class="box">${icon('check')}</span><span>${esc(METRICS[id].name)}<small>${esc(now)}</small></span></button>${moves}</div>`;
  }).join('');
  return `<header class="f-head"><h1>Pill</h1><button class="icon-btn f-push" type="button" data-customized aria-label="Done" title="Done">${icon('check')}</button></header>
    <div class="f-rows scroll"><div class="f-rows-in f-settings"><div class="f-capsule f-preview" aria-hidden="true"><span class="f-rail">${pillHTML(items, true)}</span></div><div class="f-set-list">${rows}${notifyHTML()}${shortcutsHTML()}</div></div></div>`;
}
function notifyHTML() {
  const rows = Object.entries(NOTIFY).map(([kind, name]) => `<div class="f-set"><button type="button" class="check" role="checkbox" data-notify="${kind}" aria-checked="${notify[kind]}"><span class="box">${icon('check')}</span><span>${esc(name)}</span></button></div>`).join('');
  return `<div class="f-section">Notify me about</div>${rows}`;
}
function shortcutsHTML() {
  const rows = Object.entries(SHORTCUTS).map(([action, entry]) => {
    const key = shortcuts[action], live = recording === action, failed = refused.includes(action);
    const label = live ? 'Press keys…' : key?.label || 'Add';
    return `<div class="f-key"><span>${esc(entry.name)}${failed ? '<small class="f-key-bad">Another app is using this</small>' : ''}</span><button type="button" class="f-cap ${live ? 'live' : ''} ${key ? '' : 'empty'}" data-record="${action}" aria-label="Change the shortcut for ${esc(entry.name)}">${esc(label)}</button></div>`;
  }).join('');
  return `<div class="f-section">Keyboard shortcuts</div>${rows}<p class="f-hint">These work from any app. While recording, Delete clears a shortcut and Escape cancels.</p>`;
}
function floatListHTML(items, live = liveHealthy) {
  const head = `<header class="f-head"><h1>Needs you</h1>${items.length ? `<span class="f-count">${items.length}</span>` : ''}${live ? '' : '<span class="f-off"><span class="dot err"></span>Reconnecting…</span>'}
      <button class="icon-btn f-push" type="button" data-more aria-label="More" title="More">${icon('more')}</button></header>`;
  const rows = items.map((i) => rowHTML(i, {where: true, age: true, kind: true})).join('');
  return `${head}<div class="f-rows scroll"><div class="f-rows-in">${rows || `<div class="f-empty">${EMPTY.you[0]}</div>`}</div></div>`;
}

// Paint
function setHTML(el, html) {
  if (el.hqPainted === html) return;
  el.innerHTML = html;
  el.hqPainted = html;
}
function paintFloat({background = false} = {}) {
  model = derive();
  if (snapshotSignature && route.item && !model.items.some((i) => i.id === route.item)) route.item = null;
  const items = attention(), live = liveHealthy || !snapshotSignature;
  holdAnswered(items);
  noteChanges(items);
  const mode = surfaceMode(floatOpen, route.item, customizing);
  const kind = motionKind(lastMode, mode, lastItem, route.item, visibleIds(), flown);
  flown = false;
  const outgoing = (kind === 'next' || kind === 'prev') && !$('#detail').hidden ? keepCard() : null;
  const scrolls = ['.f-rows', '.d-body'].map((sel) => [sel, document.querySelector(sel)?.scrollTop]);
  const pill = $('#pill');
  setHTML($('#rail'), pillHTML(items, live));
  rollChanged(items);
  pill.setAttribute('aria-label', pillLabel(items, live));
  pill.title = 'Click to open. Drag to move. Right-click for more.';
  setHTML($('#main'), customizing ? settingsHTML(items) : floatListHTML(items, live));
  const detail = $('#detail'), active = document.activeElement;
  // Typing in the item pane wins over a live refresh; drafts are kept either way.
  if (!(background && detail.contains(active) && active.matches('textarea, input, select'))) renderDetail();
  scrolls.forEach(([sel, top]) => top && document.querySelector(sel) && (document.querySelector(sel).scrollTop = top));
  document.body.dataset.mode = mode;
  // Content keeps its final width while the box springs, so it never reflows mid-motion.
  document.documentElement.style.setProperty('--f-content', `${mode === 'item' ? feel.width : LIST_WIDTH}px`);
  $('#box').classList.toggle('bare', mode === 'item');
  stackBehind(mode);
  syncPill();
  if (route.item && !detail.hidden) bindDetail(detail);
  layout();
  moveDeck(kind, outgoing);
  lastMode = mode;
  lastItem = route.item;
}
function noteChanges(items) {
  if (!snapshotSignature) return;
  if (lastItems) enqueue(itemChanges(lastItems, items, model.items));
  if (lastUsage) enqueue(usageChanges(lastUsage, state.usage));
  lastItems = items;
  lastUsage = state.usage;
}
function watchAgents() {
  if (!liveHealthy || !model) return;
  const agents = state.sessions.map((s) => ({id: s.id, name: s.name, harness: harness(s.provider), state: agentState(s)}));
  const events = trackAgents(agentMemory, agents, Date.now() / 1000, watchAgents.settled);
  watchAgents.settled = true;
  enqueue(events);
}

// Island: the capsule morphs into a card and folds back. The numbers on the pill are already
// true by then; this only says what changed.
function enqueue(events) {
  const wanted = events.filter((e) => notify[e.group]);
  if (!wanted.length || floatOpen) return;
  pending.push(...wanted);
  clearTimeout(enqueue.timer);
  enqueue.timer = setTimeout(runNotice, BURST_WINDOW);
}
function runNotice() {
  if (notice || !pending.length || floatOpen || drag) return;
  const event = coalesce(pending.splice(0));
  const items = attention(), reading = METRICS[event.metric].read(items);
  notice = {...event, reading, from: reading.value === null ? null : reading.value - (event.delta || 0), left: feel.hold};
  $('#hero').innerHTML = heroHTML(reading, notice.from);
  $('#note').innerHTML = `<span class="f-note-title">${event.dot ? `<span class="f-dot ${esc(event.dot)}"></span>` : ''}${esc(event.title)}</span><span class="f-note-sub">${esc(event.sub)}</span>`;
  $('#pill').classList.add('noting');
  syncPill();
  layout();
  notice.roll = setTimeout(() => rollHero(reading.value), ROLL_AFTER);
  // Hovering holds the card open, since you are reading it.
  notice.timer = setInterval(() => {
    if (!hovering) notice.left -= HOLD_TICK;
    if (notice.left <= 0) foldNotice();
  }, HOLD_TICK);
}
function heroHTML(reading, from) {
  const html = circleHTML({...reading, value: from ?? reading.value});
  return html.replace(/<span>([^<]*)<\/span><\/span>$/, '<span class="f-num"><b>$1</b></span></span>');
}
function rollHero(value) {
  const num = $('#hero .f-num');
  if (!num || value === null) return;
  const old = num.querySelector('b');
  if (old.textContent === shortCount(value)) return;
  const up = value > Number(old.textContent), fresh = document.createElement('b');
  fresh.textContent = shortCount(value);
  fresh.className = up ? 'from-below' : 'from-above';
  num.append(fresh);
  fresh.getBoundingClientRect();
  old.className = up ? 'to-above' : 'to-below';
  fresh.className = '';
  setTimeout(() => old.remove(), 450);
}
function foldNotice() {
  clearInterval(notice.timer);
  clearTimeout(notice.roll);
  notice.folding = true;
  $('#pill').classList.remove('noting');
  layout();
  notice.done = setTimeout(() => {
    notice = null;
    $('#hero').innerHTML = '';
    $('#note').innerHTML = '';
    layout();
    runNotice();
  }, FOLD_FOR);
}
// Opening or dragging takes over at once, and you can see everything from there anyway.
function dropNotices() {
  clearTimeout(enqueue.timer);
  pending = [];
  if (!notice) return;
  clearInterval(notice.timer);
  clearTimeout(notice.roll);
  clearTimeout(notice.done);
  notice = null;
  $('#pill').classList.remove('noting');
  $('#hero').innerHTML = '';
  $('#note').innerHTML = '';
}

// Window
function pillSize() {
  const rail = $('#rail');
  return layoutFor(dock) === 'horizontal' ? {width: rail.offsetWidth + PILL_CHROME, height: feel.size + PILL_CHROME}
    : {width: feel.size + PILL_CHROME, height: rail.offsetHeight + PILL_CHROME};
}
function setRect(el, r) {
  Object.assign(el.style, {left: `${r.x}px`, top: `${r.y}px`, width: `${r.width}px`, height: `${r.height}px`});
}
function boxSize(mode = document.body.dataset.mode) {
  if (mode === 'list' || mode === 'settings') {
    const head = $('.f-head')?.offsetHeight || 0, rows = $('.f-rows-in')?.offsetHeight || 0;
    return {width: LIST_WIDTH, height: Math.min(mode === 'settings' ? SETTINGS_MAX : LIST_MAX, head + rows + 2)};
  }
  const last = $('.d-body')?.lastElementChild;
  const natural = last ? last.offsetTop + last.offsetHeight + 28 : ITEM_MIN;
  return {width: feel.width, height: Math.max(ITEM_MIN, Math.min(ITEM_MAX, natural + 2)) + STACK};
}
function geometry(mode = document.body.dataset.mode) {
  const area = display.visible, rest = dockRect(dock, pillSize(), area);
  const pill = mode === 'pill' && notice && !notice.folding ? dockRect(dock, CARD, area) : rest;
  return {room: roomFor(dock, rest, area), rest, pill, box: mode === 'pill' ? null : boxRect(dock, rest, boxSize(mode), area)};
}
function placeWindow(rect, flags) {
  const message = {type: 'place', ...rect, ...flags}, signature = JSON.stringify(message);
  if (signature === placed) return;
  placed = signature;
  native(message);
}
function layout() {
  if (drag || !display || (!snapshotSignature && !layout.ready)) return;
  const mode = document.body.dataset.mode, box = $('#box');
  // A hidden box measures as nothing, so show it before asking how big its content is.
  const unfolding = mode !== 'pill' && (box.hidden || box.classList.contains('closing'));
  if (unfolding) {
    clearTimeout(layout.closing);
    box.classList.remove('closing');
    box.classList.add('instant', 'folded');
    box.hidden = false;
  }
  const g = geometry(mode), pill = localRect(g.pill, g.room);
  setRect($('#pill'), pill);
  if (g.box) {
    const target = localRect(g.box, g.room);
    setRect(box, target);
    // The pill stays put, so the box grows out of its center and shrinks back into it,
    // rather than becoming a second, black pill on top of the real one.
    box.style.setProperty('--f-from', `${Math.round(pill.x + pill.width / 2 - target.x)}px ${Math.round(pill.y + pill.height / 2 - target.y)}px`);
    if (unfolding) {
      box.getBoundingClientRect();
      box.classList.remove('instant', 'folded');
      box.classList.add('opening');
      clearTimeout(layout.opened);
      layout.opened = setTimeout(() => box.classList.remove('opening'), FOLD_MS);
    }
  } else if (!box.hidden && !box.classList.contains('closing')) {
    box.classList.add('closing');
    layout.closing = setTimeout(() => {box.hidden = true; box.classList.remove('closing');}, FOLD_MS);
    // The pill takes the box in.
    if (!calm()) $('#pill').animate([{transform: 'scale(1)'}, {transform: 'scale(1.07)'}, {transform: 'scale(1)'}], {duration: 380, delay: 230, easing: 'ease-out'});
  }
  if (spot.on) placeSpot();
  const hits = [pill, g.box && localRect(g.box, g.room)].filter(Boolean);
  placeWindow({...g.room, hit: hits[hits.length - 1], hits}, {key: mode !== 'pill', shadow: false});
}
function layoutSoon() {
  if (layoutSoon.pending) return;
  layoutSoon.pending = true;
  requestAnimationFrame(() => {layoutSoon.pending = false; layout();});
}
function setOpen(open) {
  if (open) dropNotices();
  floatOpen = open;
  if (!open) customizing = false;
  render();
}
function syncPill() {
  const [across, down] = DOCKS[dock] || DOCKS[DEFAULT_DOCK], body = document.body;
  body.dataset.side = labelSide(dock);
  body.dataset.across = across;
  body.dataset.down = down;
  $('#pill').dataset.layout = layoutFor(dock);
  if (spot.on) markSpot();
}

// Deck: items are cards in a stack. Next lifts the top card away, previous slides it back on.
const DEPTH = ['none', 'translateY(10px) scale(.96)', 'translateY(20px) scale(.92)'];
function stackBehind(mode) {
  const ids = visibleIds(), at = ids.indexOf(route.item), left = mode === 'item' && at >= 0 ? ids.length - at - 1 : 0;
  document.querySelectorAll('.f-back').forEach((card) => {card.hidden = Number(card.dataset.depth) > left;});
}
function keepCard() {
  const copy = $('#detail').cloneNode(true);
  copy.removeAttribute('id');
  copy.removeAttribute('aria-labelledby');
  copy.classList.add('f-leaving');
  copy.inert = true;
  copy.querySelectorAll('[id]').forEach((el) => el.removeAttribute('id'));
  return copy;
}
const calm = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
function spring(name) {return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || 'ease';}
function moveDeck(kind, outgoing) {
  if (calm()) return outgoing?.remove();
  const card = $('#detail'), soft = spring('--f-soft');
  if (kind === 'deal') {
    card.animate([{transform: 'translateY(34px) scale(.97)', opacity: 0}, {transform: 'none', opacity: 1}], {duration: 500, easing: soft, delay: 90, fill: 'backwards'});
    document.querySelectorAll('.f-back:not([hidden])').forEach((back) => back.animate(
      [{transform: `translateY(${34 + back.dataset.depth * 10}px) scale(.97)`, opacity: 0}, {transform: DEPTH[back.dataset.depth], opacity: 1}],
      {duration: 500, easing: soft, delay: 90 + back.dataset.depth * 45, fill: 'backwards'}));
  } else if (kind === 'gather') {
    $('#main').animate([{transform: 'translateY(-10px)', opacity: 0}, {transform: 'none', opacity: 1}], {duration: 360, easing: soft});
  } else if (kind === 'answered') {
    card.animate([{transform: 'translateY(14px)', opacity: 0}, {transform: 'none', opacity: 1}], {duration: 460, easing: soft, delay: 130, fill: 'backwards'});
  } else if (kind === 'next' || kind === 'prev') {
    if (outgoing) {
      $('#box').append(outgoing);
      const away = kind === 'next'
        ? [{transform: 'none', opacity: 1}, {transform: 'translate(-12px, -58px) rotate(-5deg) scale(.98)', opacity: 0}]
        : [{transform: 'none', opacity: 1}, {transform: DEPTH[1], opacity: 0}];
      outgoing.animate(away, {duration: kind === 'next' ? 360 : 220, easing: kind === 'next' ? 'cubic-bezier(.3,.7,.3,1)' : 'ease-out', fill: 'forwards'}).onfinish = () => outgoing.remove();
    }
    const arrive = kind === 'next' ? [{transform: DEPTH[1], opacity: 0.8}, {transform: 'none', opacity: 1}]
      : [{transform: 'translate(10px, -54px) rotate(4deg)', opacity: 0}, {transform: 'none', opacity: 1}];
    card.animate(arrive, {duration: 480, easing: soft});
  }
}

// Fly home: an answered card flies into the needs-you circle, and the count drops when it lands.
// The count holds from the moment the item leaves the list, so it never drops early.
function holdAnswered(items) {
  const answered = floatOpen && route.item && lastShown?.includes(route.item) && !items.some((i) => i.id === route.item);
  lastShown = items.map((i) => i.id);
  if (!answered || holding) return;
  holding = route.item;
  flying += 1;
  // If nothing moves on, such as Stay after answering, let the count settle anyway.
  holdAnswered.timer = setTimeout(() => {if (holding) land();}, 1500);
}
function flyHome() {
  if (!holding) return;
  clearTimeout(holdAnswered.timer);
  const card = $('#detail'), circle = $('#rail [data-metric="needs"] .f-circle') || $('#pill');
  if (card.hidden || calm()) return land();
  const from = card.getBoundingClientRect(), to = circle.getBoundingClientRect();
  const outer = document.createElement('div'), inner = document.createElement('div');
  outer.className = 'f-flight';
  inner.className = 'f-flight-card';
  Object.assign(inner.style, {width: `${from.width}px`, height: `${from.height}px`});
  inner.append(keepCard());
  outer.append(inner);
  document.body.append(outer);
  const dx = to.left + to.width / 2 - (from.left + from.width / 2), dy = to.top + to.height / 2 - (from.top + from.height / 2);
  const fit = Math.max(to.width / from.width, to.height / from.height);
  // Different easing on each axis bends the path into an arc.
  outer.animate([{transform: `translate(${from.left}px, ${from.top}px)`}, {transform: `translate(${from.left + dx}px, ${from.top}px)`}],
    {duration: FLY_MS, easing: 'cubic-bezier(.45,0,.7,.4)', fill: 'forwards'});
  const flight = inner.animate([{transform: 'translateY(0) scale(1)', borderRadius: '16px', opacity: 1},
    {transform: `translateY(${dy}px) scale(${fit})`, borderRadius: `${from.width}px`, opacity: 0.85}],
  {duration: FLY_MS, easing: 'cubic-bezier(.3,-.28,.5,.9)', fill: 'forwards'});
  flight.onfinish = flight.oncancel = () => {outer.remove(); land();};
}
function land() {
  clearTimeout(holdAnswered.timer);
  holding = null;
  flying = Math.max(0, flying - 1);
  render();
  const circle = $('#rail [data-metric="needs"] .f-circle');
  if (!circle || calm()) return;
  circle.animate([{transform: 'scale(1)'}, {transform: 'scale(1.26)'}, {transform: 'scale(1)'}], {duration: 460, easing: 'ease-out'});
  const ripple = document.createElement('span');
  ripple.className = 'f-ripple';
  circle.append(ripple);
  ripple.animate([{opacity: 0.7, transform: 'scale(.9)'}, {opacity: 0, transform: 'scale(2.1)'}], {duration: 620, easing: 'cubic-bezier(.16,1,.3,1)'}).onfinish = () => ripple.remove();
}
// Numbers roll rather than swap.
function rollChanged(items) {
  for (const id of circles) {
    const value = METRICS[id].read(items).value, before = lastValues[id];
    lastValues[id] = value;
    if (before === undefined || before === null || value === null || value === before || calm()) continue;
    const num = $(`#rail [data-metric="${id}"] .f-circle > span:last-child`);
    num?.animate([{transform: `translateY(${value > before ? 60 : -60}%)`, opacity: 0}, {transform: 'none', opacity: 1}], {duration: 420, easing: spring('--f-soft')});
  }
}

// Spotlight: the circle under the cursor steps forward with its label; the others step back.
const spot = {on: false, index: -1};
function markSpot() {
  document.querySelectorAll('#rail .f-row[data-metric]').forEach((row, n) => {
    row.classList.toggle('hot', spot.on && n === spot.index);
    row.classList.toggle('dim', spot.on && n !== spot.index);
  });
}
function placeSpot() {
  const row = [...document.querySelectorAll('#rail .f-row[data-metric]')][spot.index], el = $('#spot');
  if (!row) return;
  const c = row.querySelector('.f-circle').getBoundingClientRect(), p = $('#pill').getBoundingClientRect();
  const side = spotSide(dock), w = el.offsetWidth, h = el.offsetHeight;
  const x = side === 'right' ? p.right + 6 : side === 'left' ? p.left - 6 - w : c.left + c.width / 2 - w / 2;
  const y = side === 'above' ? p.top - 6 - h : side === 'below' ? p.bottom + 6 : c.top + c.height / 2 - h / 2;
  el.style.transform = `translate(${Math.round(x)}px, ${Math.round(y)}px)`;
}
function spotlight(x, y) {
  if (feel.labels !== 'spotlight' || notice || drag) return hideSpot();
  const rows = [...document.querySelectorAll('#rail .f-row[data-metric]')];
  const centers = rows.map((row) => {
    const r = row.querySelector('.f-circle').getBoundingClientRect();
    return {x: r.left + r.width / 2, y: r.top + r.height / 2};
  });
  const index = nearestCircle(centers, x, y, layoutFor(dock) === 'vertical');
  if (index < 0) return;
  const el = $('#spot'), first = !spot.on;
  if (index !== spot.index || first) {
    spot.index = index;
    const label = el.firstElementChild;
    label.textContent = METRICS[rows[index].dataset.metric].read(attention()).tag;
    el.classList.toggle('instant', first);
    if (!first) label.animate([{opacity: 0, transform: 'translateY(5px)'}, {opacity: 1, transform: 'none'}], {duration: 240, easing: 'ease-out'});
  }
  spot.on = true;
  el.classList.add('on');
  markSpot();
  placeSpot();
  if (first) requestAnimationFrame(() => el.classList.remove('instant'));
}
function hideSpot() {
  if (!spot.on) return;
  spot.on = false;
  spot.index = -1;
  $('#spot').classList.remove('on');
  markSpot();
}
function saveSettings() {
  native({type: 'settings', settings: {circles, notify, shortcuts}});
}
function setCircles(next) {
  circles = next;
  saveSettings();
  render();
}
function applyShortcuts() {
  refused = [];
  native({type: 'hotkeys', hotkeys: hotkeyList(shortcuts)});
}
function runShortcut(action) {
  if (action === 'toggle') return setOpen(!floatOpen);
  if (action === 'dashboard') return native({type: 'openDashboard'});
  if (action === 'customize') {
    customizing = true;
    dropNotices();
    floatOpen = true;
    if (route.item) return go({item: null});
    return render();
  }
  if (action === 'next' || action === 'previous') {
    customizing = false;
    if (!floatOpen) setOpen(true);
    return step(action === 'next' ? 1 : -1);
  }
}

// Drag, after OpenFlow's flow bar: past a small threshold the window grows to the whole
// display, a ghost follows the cursor, the nearest target lights up, and a drop glides in.
const pointer = (e) => (display ? {x: e.screenX - display.x, y: e.screenY - display.y} : {x: e.clientX, y: e.clientY});
function armDrag(e) {
  if (e.button !== 0 || drag) return;
  const startX = e.screenX, startY = e.screenY, limit = feel.threshold * feel.threshold;
  const stop = () => {
    document.removeEventListener('mousemove', move);
    document.removeEventListener('mouseup', stop);
  };
  const move = (m) => {
    const dx = m.screenX - startX, dy = m.screenY - startY;
    if (dx * dx + dy * dy <= limit) return;
    stop();
    beginDrag(m);
  };
  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', stop);
}
function beginDrag(e) {
  dropNotices();
  hideSpot();
  drag = {phase: 'dragging', size: pillSize(), target: dock, rects: {}};
  swallowClick = true;
  document.body.dataset.drag = 'on';
  $('#drag').hidden = false;
  const ghost = $('#ghost');
  ghost.className = `f-ghost f-capsule ${feel.ghost === 'outline' ? 'outline' : ''}`;
  ghost.innerHTML = `<span class="f-rail">${$('#rail').innerHTML}</span>`;
  native({type: 'drag', on: true});
  document.addEventListener('mousemove', dragMoved);
  document.addEventListener('mouseup', dragDropped);
  coverDisplay();
  dragMoved(e);
}
// Also runs when the cursor reaches another display, so the targets move with it.
function coverDisplay() {
  if (!display || !drag) return;
  const whole = {x: 0, y: 0, width: display.width, height: display.height};
  placeWindow({...whole, hit: whole, hits: [whole]}, {key: true, shadow: false});
  drag.rects = dockRects(drag.size, display.visible);
  const zones = $('#drag');
  zones.querySelectorAll('.f-drop').forEach((el) => el.remove());
  for (const [name, r] of Object.entries(drag.rects)) {
    const el = document.createElement('div');
    el.className = 'f-drop';
    el.dataset.dock = name;
    Object.assign(el.style, {left: `${r.x}px`, top: `${r.y}px`, width: `${r.width}px`, height: `${r.height}px`});
    zones.insertBefore(el, $('#ghost'));
  }
  markTarget();
}
function markTarget() {
  $('#drag').querySelectorAll('.f-drop').forEach((el) => el.classList.toggle('on', el.dataset.dock === drag.target));
}
function moveGhost(x, y) {
  const ghost = $('#ghost');
  ghost.style.setProperty('--x', `${x}px`);
  ghost.style.setProperty('--y', `${y}px`);
}
function dragMoved(e) {
  if (drag?.phase !== 'dragging') return;
  const at = pointer(e);
  const target = nearestDock(at.x, at.y, drag.rects);
  if (target && target !== drag.target) {
    drag.target = target;
    markTarget();
  }
  // The ghost lies the way the pill would if you dropped it here.
  const layout = layoutFor(drag.target), size = capsuleSize(drag.size, layout);
  $('#ghost').dataset.layout = layout;
  moveGhost(at.x - size.width / 2, at.y - size.height / 2);
}
function dragDropped() {
  if (drag?.phase !== 'dragging') return;
  const r = drag.rects[drag.target];
  if (!r) return endDrag();
  drag.phase = 'gliding';
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  $('#ghost').classList.add('gliding');
  requestAnimationFrame(() => moveGhost(r.x, r.y));
  drag.timer = setTimeout(() => {
    // The dock only changes once it is saved, as in OpenFlow.
    if (window.webkit?.messageHandlers?.hq) native({type: 'save', dock: drag.target});
    else saved(drag.target, true);
  }, reduced ? 0 : feel.glide);
}
function saved(name, ok) {
  if (!drag) return;
  if (ok && DOCKS[name]) dock = name;
  else toast('HQ could not save where you put it');
  endDrag();
}
function endDrag() {
  if (!drag) return;
  clearTimeout(drag.timer);
  drag = null;
  document.removeEventListener('mousemove', dragMoved);
  document.removeEventListener('mouseup', dragDropped);
  delete document.body.dataset.drag;
  $('#drag').hidden = true;
  $('#ghost').classList.remove('gliding');
  native({type: 'drag', on: false});
  syncPill();
  layout();
}

// From the native window
function receive(message) {
  if (message.type === 'start') {
    if (DOCKS[message.dock]) dock = message.dock;
    circles = cleanCircles(message.settings?.circles);
    notify = cleanNotify(message.settings?.notify);
    shortcuts = cleanShortcuts(message.settings?.shortcuts);
    applyShortcuts();
    render();
  } else if (message.type === 'display') {
    display = message;
    if (drag?.phase === 'dragging') coverDisplay();
    else layout();
  } else if (message.type === 'cancelDrag') endDrag();
  else if (message.type === 'saved') saved(message.dock, message.ok);
  else if (message.type === 'resigned') {
    if (floatOpen && !route.item && !drag && !recording && feel.away === 'collapse') setOpen(false);
  } else if (message.type === 'customize') runShortcut('customize');
  else if (message.type === 'hotkey') runShortcut(message.action);
  else if (message.type === 'hotkeysApplied') {
    refused = message.failed || [];
    if (customizing) render();
  } else if (message.type === 'keyRecorded') {
    recording = null;
    if (message.key || message.cleared) shortcuts = {...shortcuts, [message.action]: message.key || null};
    saveSettings();
    applyShortcuts();
    render();
  } else if (message.type === 'notice') enqueue([message.event]);
  else if (message.type === 'hover') {
    // A panel that is not key never gets :hover or mouse moves from WebKit, so the window reports both.
    document.body.toggleAttribute('data-hover', message.on);
    hovering = message.on;
    if (!message.on) hideSpot();
  } else if (message.type === 'pointer') {
    if (hovering) spotlight(message.x, message.y);
  } else if (message.type === 'feel' && message.key in feel) {
    feel[message.key] = message.value;
    applyFeel();
    render();
  }
}
function applyFeel() {
  const root = document.documentElement;
  root.style.setProperty('--f-size', `${feel.size}px`);
  root.style.setProperty('--f-glide', `${feel.glide}ms`);
  root.style.setProperty('--f-margin', `${MARGIN}px`);
  document.body.dataset.backdrop = feel.backdrop;
}
// Links in the item pane point at the dashboard's routes, which live in the browser.
function openDashboard(hash) {window.open(`/${hash}`, '_blank', 'noreferrer');}

function bootFloat() {
  paint = paintFloat;
  moveOn = (next, replied) => {
    const target = afterAct(feel.after, next, replied);
    if (target === undefined) return;
    flyHome();
    flown = true;
    go({item: target});
  };
  window.hqFloat = {receive};
  document.documentElement.style.setProperty('--f-spring', springCurve({stiffness: 190, damping: 17}));
  document.documentElement.style.setProperty('--f-soft', springCurve({stiffness: 170, damping: 26}));
  document.documentElement.style.setProperty('--f-hug', springCurve({stiffness: 240, damping: 26}));
  applyFeel();
  document.addEventListener('mousedown', (e) => {
    swallowClick = false;
    if (e.target.closest('#pill') || (e.target.closest('.f-head, .d-bar') && !e.target.closest(CONTROLS))) armDrag(e);
  });
  // Ending a drag on the pill must not also count as clicking it.
  document.addEventListener('click', (e) => {
    if (swallowClick) {
      swallowClick = false;
      e.preventDefault();
      e.stopPropagation();
      return;
    }
    const row = e.target.closest('#main [data-item]');
    const inner = e.target.closest('[data-href], #detail a[href^="#"]');
    const circle = e.target.closest('[data-circle]'), shift = e.target.closest('[data-circle-move]');
    const toggle = e.target.closest('[data-notify]'), record = e.target.closest('[data-record]');
    if (e.target.closest('#pill')) {
      const item = notice?.item;
      if (floatOpen) return setOpen(false);
      setOpen(true);
      if (item && model.items.some((i) => i.id === item)) openItem(item);
    } else if (e.target.closest('[data-more]')) native({type: 'menu'});
    else if (toggle) {
      notify = {...notify, [toggle.dataset.notify]: !notify[toggle.dataset.notify]};
      saveSettings();
      render();
    } else if (record) {
      recording = record.dataset.record;
      native({type: 'recordKey', action: recording});
      render();
    }
    else if (e.target.closest('[data-customized]')) setOpen(false);
    else if (circle && !circle.disabled) setCircles(toggleCircle(circles, circle.dataset.circle));
    else if (shift && !shift.disabled) setCircles(moveCircle(circles, shift.dataset.id, Number(shift.dataset.circleMove)));
    else if (row) openItem(row.dataset.item);
    else if (inner) {
      e.preventDefault();
      openDashboard(inner.dataset.href || inner.getAttribute('href'));
    }
  }, true);
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (drag) endDrag();
    else if (customizing) {customizing = false; render();}
    else if (floatOpen && !route.item) setOpen(false);
    else return;
    e.stopImmediatePropagation();
    e.preventDefault();
  }, true);
  document.addEventListener('contextmenu', (e) => {
    if (!e.target.closest('#pill, .f-head, .d-bar')) return;
    e.preventDefault();
    native({type: 'menu'});
  });
  // The surface hugs its content, so anything that changes its size has to re-place the window.
  new ResizeObserver(layoutSoon).observe($('#rail'));
  new MutationObserver(layoutSoon).observe($('#box'), {childList: true, subtree: true, attributes: true, attributeFilter: ['style', 'open', 'hidden']});
  document.addEventListener('input', layoutSoon);
  document.addEventListener('toggle', layoutSoon, true);
  document.fonts?.ready.then(layoutSoon);
  window.addEventListener('resize', layoutSoon);
  setTimeout(() => {layout.ready = true; layout();}, 1500);
  setInterval(watchAgents, 1000);
  boot();
  native({type: 'ready'});
}
bootFloat();
