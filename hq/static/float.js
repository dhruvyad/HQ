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
// The box can be resized by its edges away from the pill, down to these and up to the screen.
const MIN_SIZE = {list: {width: 300, height: 200}, item: {width: 380, height: ITEM_MIN}}, GRIP = 6;
const CONTROLS = 'button, a, input, textarea, select, summary';
// The pill's size: each circle's diameter, from which its padding, gaps and numbers follow.
const PILL_SIZES = [['Small', 24], ['Medium', 28], ['Large', 34]], PILL_MIN = 22, PILL_MAX = 40;
function cleanPillSize(saved) {
  const n = Math.round(Number(saved));
  return Number.isFinite(n) && saved !== null && saved !== '' && typeof saved !== 'boolean' ? Math.max(PILL_MIN, Math.min(PILL_MAX, n)) : 28;
}
// Padding, gaps and border around the circles, so the capsule keeps its proportions at any size.
const pillPad = (size) => Math.max(3, Math.round(size / 7));
const pillChrome = (size) => 2 * pillPad(size) + 2;
const CARD = {width: 308, height: 62};
// The open box sits beside the pill, which stays put so answered items can fly home to it.
// Opening and closing are quick: a spring with no long tail, about 200ms from input to settled.
const SNAP = {stiffness: 800, damping: 46, tolerance: 0.01};
const GAP = 8, STACK = 22, BOX_MAX = 840, FOLD_MS = 220, FLY_MS = 560;
const BURST_WINDOW = 350, FOLD_FOR = 640, ROLL_AFTER = 260, HOLD_TICK = 100;
// Agents flip between busy and idle on every turn, so a change has to hold before it is news.
const AGENT_START_HOLD = 3, AGENT_STOP_HOLD = 8;
const CONTROL_OPTION = 6144;
const SHORTCUTS = {
  toggle: {name: 'Open or minimize the panel', key: {keyCode: 4, modifiers: CONTROL_OPTION, label: '⌃⌥H'}},
  next: {name: 'Next item', key: {keyCode: 38, modifiers: CONTROL_OPTION, label: '⌃⌥J'}},
  previous: {name: 'Previous item', key: {keyCode: 40, modifiers: CONTROL_OPTION, label: '⌃⌥K'}},
  dashboard: {name: 'Open the dashboard', key: {keyCode: 2, modifiers: CONTROL_OPTION, label: '⌃⌥D'}},
  customize: {name: 'Customize the pill', key: null},
};
// Keys inside the open panel, separate from the hotkeys that work from any app. Enter also
// opens and Escape also goes back; these four can be changed or cleared.
const PANEL_KEYS = {
  previous: {name: 'Previous', key: 'ArrowUp'},
  next: {name: 'Next', key: 'ArrowDown'},
  open: {name: 'Open', key: 'ArrowRight'},
  back: {name: 'Back', key: 'ArrowLeft'},
};
const NOTIFY = {items: 'Items added or removed', agents: 'Agents starting or stopping', usage: 'Usage running low', stuck: 'Decisions an agent did not act on'};
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
  stuck: {name: 'Decisions not acted on', read: () => {
    const n = stuckItems().length;
    return {value: n, tag: n ? stuckWords(n) : 'Every decision acted on'};
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
let panelKeys = cleanPanelKeys(), recordingPanel = null, cursor = null, sizes = cleanSizes(), resizing = null;
let lastItems = null, lastUsage = null, lastStuck = null, pending = [], notice = null;
let listView = 'needs', agentOpen = null, agentsAll = false, listMotion = null, fallbackIndex = null, pendingScroll = null;
const agentMemory = new Map();

function cleanShortcuts(saved) {
  const valid = (key) => Number.isInteger(key?.keyCode) && Number.isInteger(key?.modifiers) && typeof key?.label === 'string';
  return Object.fromEntries(Object.entries(SHORTCUTS).map(([action, entry]) => {
    const chosen = saved?.[action];
    return [action, chosen === null ? null : valid(chosen) ? chosen : entry.key];
  }));
}
// A key as one string: modifiers in a fixed order, then the key, such as Alt+ArrowLeft or k.
function keyCombo(e) {
  const key = e.key?.length === 1 ? e.key.toLowerCase() : e.key;
  return [e.ctrlKey && 'Ctrl', e.altKey && 'Alt', e.shiftKey && key.length > 1 && 'Shift', e.metaKey && 'Meta', key].filter(Boolean).join('+');
}
const KEY_LABELS = {ArrowUp: '↑', ArrowDown: '↓', ArrowLeft: '←', ArrowRight: '→', Enter: '↵', Escape: 'Esc', Tab: '⇥', ' ': 'Space', Ctrl: '⌃', Alt: '⌥', Shift: '⇧', Meta: '⌘'};
const keyLabel = (combo) => combo.split('+').map((part) => KEY_LABELS[part] || part.toUpperCase()).join('');
function cleanPanelKeys(saved) {
  const valid = (key) => typeof key === 'string' && key.length > 0 && key.length <= 40;
  return Object.fromEntries(Object.entries(PANEL_KEYS).map(([action, entry]) => {
    const chosen = saved?.[action];
    return [action, chosen === null ? null : valid(chosen) ? chosen : entry.key];
  }));
}
// What a key does in the open panel, wherever focus is not in a text box.
function panelAction(combo, mode) {
  const bound = Object.keys(panelKeys).find((action) => panelKeys[action] === combo);
  if (bound) return bound;
  if (combo === 'Enter' && mode === 'list') return 'open';
  if (combo === 'Escape' && mode === 'item') return 'back';
  return null;
}
// Sizes you chose by dragging an edge, one for the list and one for an item. Unset means the
// default; a height is how tall the box may grow, and it still hugs shorter content.
function cleanSizes(saved) {
  const valid = (s) => Number.isFinite(s?.width) && Number.isFinite(s?.height) && s.width > 0 && s.height > 0 && s.width < 20000 && s.height < 20000;
  return {list: valid(saved?.list) ? {width: Math.round(saved.list.width), height: Math.round(saved.list.height)} : null,
          item: valid(saved?.item) ? {width: Math.round(saved.item.width), height: Math.round(saved.item.height)} : null};
}
const sizeView = (mode) => (mode === 'item' ? 'item' : 'list');
// The width and the most height a view may have, from your size or the defaults.
function chosenSize(view, saved = sizes) {
  const fallback = view === 'item' ? {width: feel.width, height: ITEM_MAX} : {width: LIST_WIDTH, height: LIST_MAX};
  return saved[view] || fallback;
}
// Which edges resize: those away from the pill. On an axis where the box is centred on the
// pill, both edges move together.
function freeEdges(name) {
  const [across, down] = DOCKS[name] || DOCKS[DEFAULT_DOCK];
  if (across === 'center') return {x: ['left', 'right'], y: [down === 'end' ? 'top' : 'bottom'], centeredX: true, centeredY: false};
  return {x: [across === 'start' ? 'right' : 'left'], y: down === 'start' ? ['bottom'] : down === 'end' ? ['top'] : ['top', 'bottom'],
    centeredX: false, centeredY: down === 'center'};
}
// The largest box that still fits on the screen beside the pill.
function sizeLimits(name, view, pill, area) {
  const [across] = DOCKS[name] || DOCKS[DEFAULT_DOCK], min = MIN_SIZE[view];
  const width = across === 'center' ? area.width - 2 * EDGE : area.width - 2 * EDGE - pill.width - GAP;
  const height = (across === 'center' ? area.height - pill.height - GAP : area.height) - EDGE - BOTTOM_EDGE - (view === 'item' ? STACK : 0);
  return {min, max: {width: Math.max(min.width, width), height: Math.max(min.height, height)}};
}
// Dragging an edge by (dx, dy) from where it started. A centred axis grows on both sides,
// so the edge you hold stays under the cursor.
function resized(start, edge, dx, dy, name, limits) {
  const free = freeEdges(name);
  let {width, height} = start;
  if (edge.includes('right')) width = start.width + dx * (free.centeredX ? 2 : 1);
  if (edge.includes('left')) width = start.width - dx * (free.centeredX ? 2 : 1);
  if (edge.includes('bottom')) height = start.height + dy * (free.centeredY ? 2 : 1);
  if (edge.includes('top')) height = start.height - dy * (free.centeredY ? 2 : 1);
  return {width: Math.round(clamp(width, limits.min.width, limits.max.width)), height: Math.round(clamp(height, limits.min.height, limits.max.height))};
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
  const why = latest && settleLabel(latest) ? settleLabel(latest)
    : !latest || latest.status === 'done' ? 'Done'
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
// A spring sampled into CSS linear(), so it runs on the compositor. The tolerance says how
// close counts as there: a looser one ends the curve before a long, invisible tail.
function springSamples({stiffness, damping, tolerance = 0.002}) {
  const step = 1 / 120, points = [];
  let x = 0, v = 0, still = 0;
  for (let t = 0; t < 3 && still < 12; t += step) {
    v += (-stiffness * (x - 1) - damping * v) * step;
    x += v * step;
    points.push(x);
    still = Math.abs(x - 1) < tolerance && Math.abs(v) < tolerance * 10 ? still + 1 : 0;
  }
  return points.slice(0, Math.max(1, points.length - (tolerance > 0.002 ? 11 : 0)));
}
function springCurve(spring) {
  const points = springSamples(spring), every = Math.max(1, Math.floor(points.length / 44));
  return `linear(0, ${points.filter((_, n) => n % every === 0).map((p) => p.toFixed(3)).join(', ')}, 1)`;
}
// How long a spring's curve lasts, so the transition runs at its real speed.
const springMs = (spring) => Math.round(springSamples(spring).length / 120 * 1000);

// Geometry, in display-local points with the origin at the display's top-left.
function dockRect(requested, size, area) {
  const name = DOCKS[requested] ? requested : DEFAULT_DOCK;
  const [across, down] = DOCKS[name];
  const width = Math.min(size.width, area.width - 2 * EDGE), height = Math.min(size.height, area.height - EDGE - BOTTOM_EDGE);
  const x = across === 'start' ? area.x + EDGE : across === 'center' ? area.x + (area.width - width) / 2 : area.x + area.width - width - EDGE;
  const y = down === 'start' ? area.y + EDGE : down === 'center' ? area.y + (area.height - height) / 2 : area.y + area.height - height - BOTTOM_EDGE;
  return {x: Math.round(x), y: Math.round(y), width, height};
}
// The window covers the whole display, always, so nothing ever resizes or moves it: the pill
// springs, the box folds and a drop lands all in one page, on the compositor. Clicks still
// pass through everywhere but the pill and the box.
const roomOf = (d) => ({x: 0, y: 0, width: d.width, height: d.height});
const inflateRect = (r, by) => ({x: r.x - by, y: r.y - by, width: r.width + 2 * by, height: r.height + 2 * by});
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
// The window reports the pointer over the open box too; only the pill itself, give or take a
// few pixels, lights a circle.
const SPOT_SLACK = 6;
function overPill(pill, box, x, y) {
  const inside = (r, pad = 0) => Boolean(r) && x >= r.left - pad && x <= r.right + pad && y >= r.top - pad && y <= r.bottom + pad;
  return inside(pill, SPOT_SLACK) && !inside(box);
}
// While the box is open its header sits beside the pill, so the label would cover it: the
// circle still steps forward, but says nothing.
const spotLabelled = (open) => !open;
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
    <div class="f-rows scroll"><div class="f-rows-in f-settings"><div class="f-capsule f-preview" aria-hidden="true"><span class="f-rail">${pillHTML(items, true)}</span></div><div class="f-set-list">${pillSizeHTML()}${rows}${notifyHTML()}${shortcutsHTML()}${panelKeysHTML()}</div></div></div>`;
}
function pillSizeHTML() {
  return `<div class="f-section">Size</div><div class="f-sizes" role="radiogroup" aria-label="Pill size">${PILL_SIZES.map(([name, px]) => `<button type="button" role="radio" data-pill-size="${px}" aria-checked="${feel.size === px}">${name}</button>`).join('')}</div>`;
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
function panelKeysHTML() {
  const rows = Object.entries(PANEL_KEYS).map(([action, entry]) => {
    const key = panelKeys[action], live = recordingPanel === action;
    const label = live ? 'Press a key…' : key ? keyLabel(key) : 'Add';
    return `<div class="f-key"><span>${esc(entry.name)}</span><button type="button" class="f-cap ${live ? 'live' : ''} ${key ? '' : 'empty'}" data-record-panel="${action}" aria-label="Change the key for ${esc(entry.name)}">${esc(label)}</button></div>`;
  }).join('');
  const changed = Object.entries(PANEL_KEYS).some(([action, entry]) => panelKeys[action] !== entry.key);
  return `<div class="f-section">Keys inside the panel</div>${rows}<p class="f-hint">While the panel is open. Enter also opens and Escape also goes back. While recording, Delete clears a key and Escape cancels.${changed ? ' <button type="button" class="f-reset" data-reset-panel-keys>Reset to the arrows</button>' : ''}</p>`;
}
function floatListHTML(items, live = liveHealthy) {
  const f = needsFilter(), stuck = stuckItems().length;
  const head = needsHeadHTML(f, items, {extra: offlineNote(live), more: moreButton, elsewhere: elsewhereHTML()});
  const rows = needsBodyHTML(f, items, (i) => rowHTML(i, {where: f.group !== 'project', age: true, kind: true}),
    (narrowed) => (narrowed ? needsEmpty(true) : `<div class="f-empty">${EMPTY.you[0]}</div>`));
  const quiet = stuck ? `<button class="f-quiet f-stuck" type="button" data-view="stuck">${stuckWords(stuck)}</button>` : '';
  return `${head}<div class="f-rows scroll"><div class="f-rows-in">${rows}${quiet}</div></div>`;
}
// The panel's other lists, at the foot of the view menu, since its header has no room for tabs.
function elsewhereHTML() {
  const running = model.agents.filter((a) => agentState(a) === 'active').length, stuck = stuckItems().length;
  return `<div class="p-label">Elsewhere</div><button class="g-opt" type="button" data-view="agents"><span class="tick"></span><span class="o-text">Agents</span><span class="n">${running ? `${running} running` : ''}</span></button>${stuck ? `<button class="g-opt" type="button" data-view="stuck"><span class="tick"></span><span class="o-text">Not acted on</span><span class="n">${stuck}</span></button>` : ''}`;
}

// Decisions an agent has not acted on, oldest first: a quiet view under Needs you.
function stuckPanelHTML(live = liveHealthy) {
  const items = stuckItems();
  const rows = items.map((i) => rowHTML(i, {stuck: true})).join('');
  return `<header class="f-head"><button class="f-return" type="button" data-view="needs">${icon('back', 'sm')}Needs you</button><span class="f-title">Not acted on</span>${offlineNote(live)}${moreButton}</header>
    <div class="f-rows scroll"><div class="f-rows-in">${rows || '<div class="f-empty">Every decision has been acted on</div>'}</div></div>`;
}
const stuckWords = (n) => `${n} ${n === 1 ? 'decision' : 'decisions'} not acted on`;
// A decision turning stuck is news once, when it turns.
function stuckChanges(before, after) {
  const was = new Set(before.map((i) => i.id));
  return after.filter((i) => !was.has(i.id)).map((i) => ({group: 'stuck', metric: 'stuck', delta: 1, plural: 'decisions not acted on', item: i.id,
    title: `${{stopped: 'An agent stopped before acting on', unheard: 'Not heard in a day', unclosed: 'Heard but never closed'}[i.followup.stuck] || 'Not acted on'}: ${i.title}`,
    sub: i.project_name, dot: 'bad'}));
}
// The two lists share one header: Needs you, and Agents beside it.
function listTabs(items) {
  const running = model.agents.filter((a) => agentState(a) === 'active').length;
  return `<div class="f-tabs" role="tablist" aria-label="Lists"><button class="f-tab" type="button" role="tab" data-view="needs" aria-selected="${listView === 'needs'}">Needs you${items.length ? `<small>${items.length}</small>` : ''}</button><button class="f-tab" type="button" role="tab" data-view="agents" aria-selected="${listView === 'agents'}">Agents${running ? `<small>${running} running</small>` : ''}</button></div>`;
}
const offlineNote = (live) => (live ? '' : '<span class="f-off"><span class="dot err"></span>Reconnecting…</span>');
const moreButton = `<button class="icon-btn f-push" type="button" data-more aria-label="More" title="More">${icon('more')}</button>`;
// Agents, by the folder each one works in. Click one for its page.
function agentsPanelHTML(items, live = liveHealthy) {
  const view = agentFolders(state.sessions, model.items), now = Date.now() / 1000;
  const shown = agentOpen && view.all.find((a) => a.id === agentOpen);
  if (shown) return `<header class="f-head"><button class="f-return" type="button" data-back>${icon('back', 'sm')}Agents</button>${offlineNote(live)}${moreButton}</header><div class="f-rows scroll"><div class="f-rows-in">${agentPageHTML(shown, now)}</div></div>`;
  const agent = (a) => `<button class="f-agent" type="button" data-agent="${esc(a.id)}">${agentDot(a)}<span class="a-main"><span class="a-name">${esc(a.name)}</span><span class="a-meta">${esc(a.harness)} · ${agentMeta(a, now)}</span></span><span class="a-side">${agentSide(a)}</span></button>`;
  const folder = (f) => `<div class="f-folder"><div class="f-top"><span class="f-name">${esc(f.name)}</span>${f.running ? '<span class="f-run">running</span>' : ''}</div>${f.agents.map(agent).join('')}</div>`;
  const quiet = view.quiet.length ? (agentsAll ? view.quiet.map(folder).join('')
    : `<button class="f-quiet" type="button" data-agents-all>${view.quietCount} more, in ${view.quiet.length} ${view.quiet.length === 1 ? 'folder' : 'folders'}</button>`) : '';
  const body = view.groups.map(folder).join('') || '<div class="f-empty">No agent has run in the last day</div>';
  return `<header class="f-head">${listTabs(items)}${offlineNote(live)}${moreButton}</header><div class="f-rows scroll"><div class="f-rows-in">${body}${quiet}</div></div>`;
}
// One agent: what it is doing, where, and its open items, which open as usual.
function agentPageHTML(a, now) {
  const items = sortLane(model.items.filter((i) => i.session_id === a.id && i.lane !== 'done'), 'you');
  const lane = (key, label) => {
    const list = items.filter((i) => i.lane === key);
    return `<div class="f-lane"><div class="f-lane-h">${label}<span class="c">${list.length}</span></div>${list.map((i) => rowHTML(i, {age: true, kind: true})).join('') || '<div class="f-none">None</div>'}</div>`;
  };
  const streams = [...new Set(items.map((i) => i.workstream_name))];
  return `<div class="f-agent-page"><h2 class="f-agent-name">${esc(a.name)}</h2><div class="f-agent-state">${agentDot(a)}<span>${agentStateLine(a, now)}</span></div>
    <dl class="f-facts"><dt>Harness</dt><dd>${esc(a.harness)}</dd><dt>Folder</dt><dd>${esc(folderName(a.folder))}</dd>
      ${a.state !== 'active' && a.last ? `<dt>Last running</dt><dd>${ago(a.last)}</dd>` : ''}
      ${streams.length ? `<dt>Working on</dt><dd>${esc(streams.join(', '))}</dd>` : ''}</dl>
    ${lane('you', 'Needs you')}${lane('agents', 'With the agent')}${lane('waiting', 'Waiting on someone')}
    <button class="ghost-pill f-wide" type="button" data-agent-dashboard="${esc(a.id)}">Open in the dashboard</button></div>`;
}
// Each tab keeps where you were in it: what it had open, its highlight and its scroll. Closing
// the panel keeps it, and so does a restart, since only ids and positions are stored.
const TABS_KEY = 'hq-float-tabs', TAB_NAMES = ['needs', 'agents', 'stuck'];
const blankTab = () => ({item: null, agent: null, cursor: null, index: 0, scroll: 0, body: 0, itemTab: null, all: false, filter: blankNeeds('none')});
const idOrNull = (v) => (typeof v === 'string' && v.length > 0 && v.length <= 200 ? v : null);
const place = (v) => (Number.isFinite(v) && v >= 0 ? Math.min(Math.round(v), 1e6) : 0);
function cleanTabs(saved) {
  const tabs = Object.fromEntries(TAB_NAMES.map((name) => {
    const t = saved?.tabs?.[name];
    if (!t || typeof t !== 'object') return [name, blankTab()];
    return [name, {item: idOrNull(t.item), agent: idOrNull(t.agent), cursor: idOrNull(t.cursor), index: place(t.index), scroll: place(t.scroll),
      body: place(t.body), itemTab: ['steps', 'why', 'context', 'ask'].includes(t.itemTab) ? t.itemTab : null, all: t.all === true,
      filter: cleanNeeds(t.filter, 'none')}];
  }));
  return {view: TAB_NAMES.includes(saved?.view) ? saved.view : 'needs', tabs};
}
let tabState = cleanTabs(remember(TABS_KEY));
listView = tabState.view;
needsFilter = () => tabState.tabs.needs.filter;
needsBase = () => attention();
saveNeeds = () => {saveTab();};
// Whether what a tab had open is still there to show.
function stillOpen(view, id, agent) {
  const i = model?.items.find((x) => x.id === id);
  if (!i) return false;
  if (view === 'stuck') return isStuck(i);
  if (view === 'agents') return i.session_id === agent && i.lane !== 'done';
  return attention().some((x) => x.id === id);
}
function saveTab() {
  const t = tabState.tabs[listView];
  if (!t || !floatOpen || customizing) return;
  const rows = cursorRows(), on = route.item || cursor;
  Object.assign(t, {item: route.item || null, agent: listView === 'agents' ? agentOpen : null, cursor,
    index: Math.max(0, rows.findIndex((r) => rowKey(r) === on)), scroll: place($('.f-rows')?.scrollTop),
    body: route.item ? place($('.d-body')?.scrollTop) : 0, itemTab: route.item ? draftOf(route.item).tab || null : null,
    all: listView === 'agents' && agentsAll});
  tabState.view = listView;
  const kept = {view: tabState.view, tabs: Object.fromEntries(Object.entries(tabState.tabs).map(([name, x]) => [name, {...x, filter: keptNeeds(x.filter || blankNeeds('none'))}]))};
  const json = JSON.stringify(kept);
  if (json !== saveTab.last) {saveTab.last = json; remember(TABS_KEY, kept);}
}
// Back into a tab as you left it. Something answered, closed or gone in the meantime shows
// its list instead, on the row that took its place.
function restoreTab(view) {
  const t = tabState.tabs[view] || blankTab();
  listView = view;
  agentOpen = view === 'agents' && t.agent && state.sessions.some((x) => x.id === t.agent) ? t.agent : null;
  agentsAll = view === 'agents' && t.all;
  const item = t.item && stillOpen(view, t.item, agentOpen) ? t.item : null;
  if (item && t.itemTab && !draftOf(item).tab) setDraft(item, {tab: t.itemTab});
  // A stale item stays the highlight; it is gone from the list, so the row now at its place lights.
  cursor = item || t.item || t.cursor;
  fallbackIndex = t.index;
  pendingScroll = {list: t.scroll, body: item ? t.body : 0};
  return item;
}
// The item the panel shows, changed without a navigation, so it paints once.
function setRouteItem(item) {
  if ((route.item || null) === (item || null)) return;
  route = {...route, item: item || null};
  try {history.replaceState(null, '', href(route));} catch {}
}
// Opening the panel goes back to a tab as you left it: the last one, or the circle's own.
function openTab(view = tabState.view, item) {
  dropNotices();
  floatOpen = true;
  customizing = false;
  if (item !== undefined) {
    listView = view;
    agentOpen = null;
    cursor = item;
  }
  setRouteItem(item !== undefined ? item : restoreTab(view));
  render();
}
// Which tab a click on the pill means: a notice's own, a circle's own, or, for the capsule
// itself, wherever you were.
function pillTarget(metric, noticeGroup) {
  if (noticeGroup) return noticeGroup === 'agents' ? 'agents' : 'needs';
  return {needs: 'needs', decisions: 'needs', due: 'needs', agents: 'agents', stuck: 'stuck'}[metric] || null;
}
// The highlighted row in whichever list is open: the first on open, the item you came back from.
const cursorRows = () => [...document.querySelectorAll('#main [data-item], #main [data-agent]')];
const rowKey = (row) => row.dataset.item || row.dataset.agent;
function markCursor(scroll = false) {
  const rows = cursorRows();
  if (!rows.length) return;
  const row = rows.find((r) => rowKey(r) === cursor) || rows[Math.min(fallbackIndex ?? 0, rows.length - 1)];
  fallbackIndex = null;
  cursor = rowKey(row);
  rows.forEach((r) => r.classList.toggle('hl', r === row));
  if (scroll) row.scrollIntoView({block: 'nearest'});
}
function moveCursor(delta) {
  const rows = cursorRows(), at = rows.findIndex((r) => rowKey(r) === cursor);
  if (!rows.length) return;
  cursor = rowKey(rows[Math.max(0, Math.min(rows.length - 1, (at < 0 ? 0 : at + delta)))]);
  markCursor(true);
}
function openCursor() {
  const row = cursorRows().find((r) => rowKey(r) === cursor);
  if (!row) return;
  if (row.dataset.agent) showList('agents', row.dataset.agent);
  else openItem(row.dataset.item);
}
// Back to the list you came from, with what you were on still highlighted.
function backToList() {
  if (route.item) {
    cursor = route.item;
    return go({item: null});
  }
  if (agentOpen) {
    cursor = agentOpen;
    return showList('agents');
  }
  if (listView === 'stuck') return showList('needs');
}
function panelKey(e) {
  if (!floatOpen || customizing || drag || recording || recordingPanel || e.defaultPrevented) return;
  // While typing, the arrows and Enter belong to the text; Escape first leaves the box.
  if (!route.item && listView === 'needs' && needsKey(e)) {e.preventDefault(); e.stopImmediatePropagation(); return;}
  if (e.target.matches?.('input, textarea, select, [contenteditable="true"]')) {
    if (e.key !== 'Escape') return;
    e.preventDefault();
    e.stopImmediatePropagation();
    e.target.blur();
    return;
  }
  const mode = route.item ? 'item' : 'list', action = panelAction(keyCombo(e), mode);
  if (!action) return;
  if (mode === 'item') {
    if (action === 'previous' || action === 'next') step(action === 'next' ? 1 : -1);
    else if (action === 'back') backToList();
    else return;
  } else if (action === 'previous' || action === 'next') moveCursor(action === 'next' ? 1 : -1);
  else if (action === 'open') openCursor();
  else if (action === 'back' && (agentOpen || listView === 'stuck')) backToList();
  else return;
  e.preventDefault();
  e.stopImmediatePropagation();
  // While the keys drive, a row under a resting cursor should not look highlighted too.
  document.body.dataset.keys = '';
}
// Recording a key for the panel: the next key pressed, Delete to clear, Escape to cancel.
function recordPanelKey(e) {
  if (!recordingPanel) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  if (['Shift', 'Control', 'Alt', 'Meta'].includes(e.key)) return;
  const action = recordingPanel;
  recordingPanel = null;
  if (e.key === 'Escape') return render();
  const combo = ['Backspace', 'Delete'].includes(e.key) ? null : keyCombo(e);
  // One key does one thing: taking it from another action leaves that one unset.
  for (const other of Object.keys(panelKeys)) if (combo && other !== action && panelKeys[other] === combo) panelKeys[other] = null;
  panelKeys = {...panelKeys, [action]: combo};
  saveSettings();
  render();
}
// Moving between lists slides the way the header reads: Agents and an agent's page sit to the right.
function showList(view, agent = null) {
  const depth = (v, a) => (v === 'needs' ? 0 : a ? 2 : 1);  // Not acted on and Agents both sit one step in
  const from = depth(listView, agentOpen);
  saveTab();
  // Another tab comes back as you left it; within a tab, this opens an agent or steps back.
  let item = null;
  if (view !== listView) item = restoreTab(view);
  else {
    agentOpen = agent;
    if (agent) {cursor = null; pendingScroll = {list: 0, body: 0};}
  }
  listMotion = depth(listView, agentOpen) > from ? 'push' : depth(listView, agentOpen) < from ? 'pop' : null;
  customizing = false;
  setRouteItem(item);
  render();
}
function slideList(motion) {
  if (!motion || calm()) return;
  $('#main').animate([{opacity: 0, transform: `translateX(${motion === 'push' ? 24 : -24}px)`}, {opacity: 1, transform: 'none'}], {duration: 320, easing: spring('--f-soft')});
}
// While the list stays put, agents that move between folders glide to their new place.
const agentTops = () => new Map([...document.querySelectorAll('#main [data-agent]')].map((el) => [el.dataset.agent, el.getBoundingClientRect().top]));
function glideAgents(before) {
  if (!before || calm()) return;
  for (const el of document.querySelectorAll('#main [data-agent]')) {
    const was = before.get(el.dataset.agent), at = el.getBoundingClientRect().top;
    if (was === undefined) el.animate([{opacity: 0, transform: 'translateY(6px)'}, {opacity: 1, transform: 'none'}], {duration: 380, easing: spring('--f-soft')});
    else if (Math.abs(was - at) > 1) el.animate([{transform: `translateY(${was - at}px)`}, {transform: 'none'}], {duration: 520, easing: spring('--f-soft')});
  }
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
  const tops = !listMotion && listView === 'agents' ? agentTops() : null;
  const finding = document.activeElement?.matches?.('[data-g-q]') ? [document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;
  setHTML($('#main'), customizing ? settingsHTML(items) : listView === 'agents' ? agentsPanelHTML(items, live) : listView === 'stuck' ? stuckPanelHTML(live) : floatListHTML(items, live));
  if (finding) {const q = $('#main [data-g-q]'); q?.focus({preventScroll: true}); q?.setSelectionRange(...finding);}
  if (mode === 'list') {markCursor(); slideList(listMotion); glideAgents(tops);}
  listMotion = null;
  const detail = $('#detail'), active = document.activeElement;
  // Typing in the item pane wins over a live refresh; drafts are kept either way.
  if (!(background && detail.contains(active) && active.matches('textarea, input, select'))) renderDetail();
  if (pendingScroll && mode !== 'pill') {
    const rows = $('.f-rows'), body = $('.d-body');
    if (rows) rows.scrollTop = pendingScroll.list;
    if (body && pendingScroll.body) body.scrollTop = pendingScroll.body;
    pendingScroll = null;
  } else scrolls.forEach(([sel, top]) => top && document.querySelector(sel) && (document.querySelector(sel).scrollTop = top));
  document.body.dataset.mode = mode;
  // Content keeps its final width while the box springs, so it never reflows mid-motion.
  document.documentElement.style.setProperty('--f-content', `${mode === 'settings' ? LIST_WIDTH : chosenSize(sizeView(mode)).width}px`);
  $('#box').classList.toggle('bare', mode === 'item');
  stackBehind(mode);
  syncPill();
  if (route.item && !detail.hidden) bindDetail(detail);
  layout();
  moveDeck(kind, outgoing);
  lastMode = mode;
  lastItem = route.item;
  if (mode === 'list' || mode === 'item') saveTab();
}
function noteChanges(items) {
  if (!snapshotSignature) return;
  if (lastItems) enqueue(itemChanges(lastItems, items, model.items));
  if (lastUsage) enqueue(usageChanges(lastUsage, state.usage));
  const stuck = stuckItems();
  if (lastStuck) enqueue(stuckChanges(lastStuck, stuck));
  lastItems = items;
  lastUsage = state.usage;
  lastStuck = stuck;
}
function watchAgents() {
  if (!liveHealthy || !model) return;
  const agents = state.sessions.map((s) => ({id: s.id, name: s.name, harness: harness(s.provider), state: agentState(s)}));
  // The first reading is only the baseline: agents already running at launch were not just started.
  const events = trackAgents(agentMemory, agents, Date.now() / 1000, Boolean(watchAgents.settled));
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
  const chrome = pillChrome(feel.size);
  return layoutFor(dock) === 'horizontal' ? {width: rail.offsetWidth + chrome, height: feel.size + chrome}
    : {width: feel.size + chrome, height: rail.offsetHeight + chrome};
}
function setRect(el, r) {
  Object.assign(el.style, {left: `${r.x}px`, top: `${r.y}px`, width: `${r.width}px`, height: `${r.height}px`});
}
function boxSize(mode = document.body.dataset.mode) {
  if (mode === 'settings') {
    const head = $('.f-head')?.offsetHeight || 0, rows = $('.f-rows-in')?.offsetHeight || 0;
    return {width: LIST_WIDTH, height: Math.min(SETTINGS_MAX, head + rows + 2)};
  }
  const chosen = chosenSize(sizeView(mode));
  if (mode === 'list') {
    const above = [...($('#main')?.children || [])].filter((el) => !el.matches('.f-rows, .g-pop')).reduce((n, el) => n + el.offsetHeight, 0);
    const rows = $('.f-rows-in')?.offsetHeight || 0;
    return {width: chosen.width, height: Math.min(chosen.height, above + rows + 2)};
  }
  return {width: chosen.width, height: Math.max(ITEM_MIN, Math.min(chosen.height, itemHeight() + 2)) + STACK};
}
// An item is as tall as its head, tabs and dock plus the open tab's content. Within one item
// the box only grows, so switching tabs or folding the answer never makes it jump.
let tallest = {id: null, height: 0};
function itemHeight() {
  const detail = $('#detail'), body = $('.d-body'), inner = $('.d-panel-in');
  if (!body || !inner) return ITEM_MIN;
  const pad = parseFloat(getComputedStyle(body).paddingTop) + parseFloat(getComputedStyle(body).paddingBottom);
  const natural = [...detail.children].reduce((n, el) => n + (el === body ? inner.offsetHeight + pad : el.offsetHeight), 0);
  if (tallest.id !== route.item) tallest = {id: route.item, height: 0};
  tallest.height = Math.max(tallest.height, natural);
  return tallest.height;
}
function geometry(mode = document.body.dataset.mode) {
  const area = display.visible, rest = dockRect(dock, pillSize(), area);
  const pill = mode === 'pill' && notice && !notice.folding ? dockRect(dock, CARD, area) : rest;
  return {room: roomOf(display), rest, pill, box: mode === 'pill' ? null : boxRect(dock, rest, boxSize(mode), area)};
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
    layout.closing = setTimeout(() => {box.hidden = true; box.classList.remove('closing');}, calm() ? 0 : FOLD_MS);
    // The pill takes the box in.
    if (!calm()) $('#pill').animate([{transform: 'scale(1)'}, {transform: 'scale(1.07)'}, {transform: 'scale(1)'}], {duration: 240, delay: 110, easing: 'ease-out'});
  }
  if (spot.on) placeSpot();
  placeGrips(g.box && mode !== 'settings' && !box.classList.contains('closing') ? localRect(g.box, g.room) : null, mode);
  const hits = [pill, g.box && inflateRect(localRect(g.box, g.room), GRIP)].filter(Boolean);
  placeWindow({...g.room, hit: hits[hits.length - 1], hits}, {key: mode !== 'pill', shadow: false});
}
// Resize grips: one per free edge and corner, drawn over the box's border.
const gripEdges = (name) => {
  const free = freeEdges(name);
  return [...free.x, ...free.y, ...free.y.flatMap((v) => free.x.map((h) => `${v}-${h}`))];
};
// macOS only shows a page's cursor for the active app, and the panel's app never is. So the
// page says which cursor it wants and the window sets it, only when that changes.
const CURSORS = {left: 'ew', right: 'ew', top: 'ns', bottom: 'ns', 'top-left': 'nwse', 'bottom-right': 'nwse', 'top-right': 'nesw', 'bottom-left': 'nesw'};
const gripCursor = (edge) => CURSORS[edge] || 'default';
let cursorSent = 'default|';
function sendCursor(kind, edge = '') {
  const key = `${kind}|${edge}`;
  if (key === cursorSent) return;
  cursorSent = key;
  native({type: 'cursor', kind, edge});
}
function cursorAt(x, y) {
  if (resizing) return sendCursor(gripCursor(resizing.edge), resizing.edge);
  if (drag) return sendCursor('grabbing');
  const grip = document.elementFromPoint?.(x, y)?.closest?.('.f-grip');
  sendCursor(grip ? gripCursor(grip.dataset.edge) : 'default', grip?.dataset.edge || '');
}
function placeGrips(rect, mode) {
  let grips = $('#grips');
  if (!grips) {
    grips = document.createElement('div');
    grips.id = 'grips';
    grips.className = 'f-grips';
    document.body.append(grips);
  }
  grips.hidden = !rect;
  if (!rect) return sendCursor('default');
  const card = mode === 'item' ? {...rect, height: rect.height - STACK} : rect;
  setRect(grips, inflateRect(card, GRIP));
  const edges = gripEdges(dock);
  const signature = edges.join(' ');
  if (grips.dataset.edges !== signature) {
    grips.dataset.edges = signature;
    grips.innerHTML = edges.map((edge) => `<div class="f-grip" data-edge="${edge}" title="Drag to resize, double-click to reset"></div>`).join('');
  }
}
function startResize(e) {
  const grip = e.target.closest('.f-grip');
  if (!grip || e.button !== 0) return;
  e.preventDefault();
  e.stopPropagation();
  const mode = document.body.dataset.mode, view = sizeView(mode), box = $('#box').getBoundingClientRect();
  const height = mode === 'item' ? box.height - STACK : box.height;
  const area = display?.visible || {x: 0, y: 0, width: innerWidth, height: innerHeight};
  resizing = {edge: grip.dataset.edge, view, x: e.clientX, y: e.clientY, start: {width: box.width, height},
    limits: sizeLimits(dock, view, pillSize(), area)};
  grip.setPointerCapture?.(e.pointerId);
  document.body.dataset.resizing = grip.dataset.edge;
  sendCursor(gripCursor(grip.dataset.edge), grip.dataset.edge);
  $('#box').classList.add('instant');
}
function moveResize(e) {
  if (!resizing) return;
  const next = resized(resizing.start, resizing.edge, e.clientX - resizing.x, e.clientY - resizing.y, dock, resizing.limits);
  sizes = {...sizes, [resizing.view]: next};
  // An item's height hugs its content at the new width, so measure it afresh.
  tallest = {id: null, height: 0};
  document.documentElement.style.setProperty('--f-content', `${next.width}px`);
  layoutSoon();
}
function endResize(e) {
  if (!resizing) return;
  resizing = null;
  cursorAt(e?.clientX ?? -1, e?.clientY ?? -1);
  delete document.body.dataset.resizing;
  saveSettings();
  requestAnimationFrame(() => {if (!resizing) $('#box').classList.remove('instant');});
}
// Double-clicking an edge puts that view back to its default size.
function resetSize(e) {
  if (!e.target.closest('.f-grip')) return;
  const view = sizeView(document.body.dataset.mode);
  sizes = {...sizes, [view]: null};
  tallest = {id: null, height: 0};
  document.documentElement.style.setProperty('--f-content', `${chosenSize(view).width}px`);
  saveSettings();
  layout();
}
function layoutSoon() {
  if (layoutSoon.pending) return;
  layoutSoon.pending = true;
  requestAnimationFrame(() => {layoutSoon.pending = false; layout();});
}
function setOpen(open) {
  if (open) return openTab();
  // Closing keeps everything: each tab is as you left it next time.
  saveTab();
  floatOpen = false;
  customizing = false;
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
  const box = $('#box');
  if (!overPill($('#pill').getBoundingClientRect(), box.hidden ? null : box.getBoundingClientRect(), x, y)) return hideSpot();
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
  el.classList.toggle('on', spotLabelled(floatOpen));
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
  native({type: 'settings', settings: {circles, notify, shortcuts, panelKeys, sizes, pillSize: feel.size}});
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
    if (listView === 'agents' && !agentOpen) listView = 'needs';
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
  sendCursor('grabbing');
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
  const whole = roomOf(display);
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
// Escape, or the system taking the drag away: the ghost glides home, then swaps the same way.
function cancelDrag() {
  if (!drag) return;
  const home = drag.rects[dock];
  if (drag.phase !== 'dragging' || !home || calm()) return endDrag();
  drag.phase = 'gliding';
  drag.target = dock;
  markTarget();
  $('#ghost').dataset.layout = layoutFor(dock);
  $('#ghost').classList.add('gliding');
  requestAnimationFrame(() => moveGhost(home.x, home.y));
  drag.timer = setTimeout(endDrag, feel.glide);
}
function endDrag() {
  if (!drag) return;
  clearTimeout(drag.timer);
  drag = null;
  document.removeEventListener('mousemove', dragMoved);
  document.removeEventListener('mouseup', dragDropped);
  // The pill takes the ghost's place in the same paint, already in its new dock and shape:
  // no transition from where it was before the drag, and the window itself never moves.
  const pill = $('#pill'), box = $('#box');
  pill.classList.add('instant');
  box.classList.add('instant');
  syncPill();
  layout();
  pill.getBoundingClientRect();
  delete document.body.dataset.drag;
  $('#drag').hidden = true;
  $('#ghost').classList.remove('gliding');
  sendCursor('default');
  native({type: 'drag', on: false});
  requestAnimationFrame(() => requestAnimationFrame(() => {
    pill.classList.remove('instant');
    if (!box.classList.contains('folded')) box.classList.remove('instant');
  }));
}

// From the native window
function receive(message) {
  if (message.type === 'start') {
    if (DOCKS[message.dock]) dock = message.dock;
    circles = cleanCircles(message.settings?.circles);
    notify = cleanNotify(message.settings?.notify);
    shortcuts = cleanShortcuts(message.settings?.shortcuts);
    panelKeys = cleanPanelKeys(message.settings?.panelKeys);
    feel.size = cleanPillSize(message.settings?.pillSize);
    applyFeel();
    sizes = cleanSizes(message.settings?.sizes);
    applyShortcuts();
    render();
  } else if (message.type === 'display') {
    display = message;
    if (drag?.phase === 'dragging') coverDisplay();
    else layout();
  } else if (message.type === 'cancelDrag') cancelDrag();
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
  // Files dragged from Finder: the window says where they are, then hands them over on a drop.
  else if (message.type === 'fileDrag') markDrop(message.off ? null : document.elementFromPoint(message.x, message.y));
  else if (message.type === 'files') dropFilesAt(document.elementFromPoint(message.x, message.y), nativeFiles(message.files));
  else if (message.type === 'hover') {
    // A panel that is not key never gets :hover or mouse moves from WebKit, so the window reports both.
    document.body.toggleAttribute('data-hover', message.on);
    hovering = message.on;
    if (!message.on) {hideSpot(); sendCursor('default');}
  } else if (message.type === 'pointer') {
    if (hovering) spotlight(message.x, message.y);
    cursorAt(message.x, message.y);
  } else if (message.type === 'feel' && message.key in feel) {
    feel[message.key] = message.value;
    applyFeel();
    render();
  }
}
function applyFeel() {
  const root = document.documentElement;
  root.style.setProperty('--f-size', `${feel.size}px`);
  root.style.setProperty('--f-pad', `${pillPad(feel.size)}px`);
  root.style.setProperty('--f-glide', `${feel.glide}ms`);
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
  document.documentElement.style.setProperty('--f-snap', springCurve(SNAP));
  document.documentElement.style.setProperty('--f-snap-ms', `${springMs(SNAP)}ms`);
  applyFeel();
  document.addEventListener('mousedown', (e) => {
    swallowClick = false;
    if (e.target.closest('#pill') || (e.target.closest('.f-head, .g-head, .d-bar') && !e.target.closest(CONTROLS))) armDrag(e);
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
      // A circle opens its own tab, as you left it; the capsule itself, wherever you were. A
      // notice opens what it is about. Clicking the tab that is already open closes it.
      const item = notice?.item, rows = [...document.querySelectorAll('#rail .f-row[data-metric]')];
      const metric = e.target.closest('[data-metric]')?.dataset.metric || (spot.on ? rows[spot.index]?.dataset.metric : null);
      const target = pillTarget(metric, notice?.group);
      if (floatOpen && (!target || target === listView)) return setOpen(false);
      if (floatOpen) return showList(target);
      // An item that is no longer in Needs you, such as a stuck decision, still opens.
      if (notice && item && model.items.some((i) => i.id === item)) return openTab('needs', item);
      openTab(target || tabState.view);
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
    else if (e.target.closest('[data-view]')) showList(e.target.closest('[data-view]').dataset.view);
    else if (e.target.closest('[data-agent]')) showList('agents', e.target.closest('[data-agent]').dataset.agent);
    else if (e.target.closest('[data-back]')) backToList();
    else if (e.target.closest('[data-pill-size]')) {
      feel.size = cleanPillSize(e.target.closest('[data-pill-size]').dataset.pillSize);
      applyFeel();
      saveSettings();
      render();
    }
    else if (e.target.closest('[data-record-panel]')) {recordingPanel = e.target.closest('[data-record-panel]').dataset.recordPanel; render();}
    else if (e.target.closest('[data-reset-panel-keys]')) {panelKeys = cleanPanelKeys(); saveSettings(); render();}
    else if (e.target.closest('[data-agents-all]')) {agentsAll = true; render();}
    else if (e.target.closest('[data-agent-dashboard]')) openDashboard(href({page: 'agent', agent: e.target.closest('[data-agent-dashboard]').dataset.agentDashboard, lane: 'you', item: null}));
    else if (row) openItem(row.dataset.item);
    else if (inner) {
      e.preventDefault();
      openDashboard(inner.dataset.href || inner.getAttribute('href'));
    }
  }, true);
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (drag) cancelDrag();
    else if (customizing) {customizing = false; render();}
    else if (floatOpen && !route.item && listView === 'needs' && needsEscape(e)) {/* the search or a menu took it */}
    else if (floatOpen && !route.item && (agentOpen || listView === 'stuck')) backToList();
    else if (floatOpen && !route.item) setOpen(false);
    else return;
    e.stopImmediatePropagation();
    e.preventDefault();
  }, true);
  document.addEventListener('keydown', recordPanelKey, true);
  document.addEventListener('pointerdown', startResize, true);
  document.addEventListener('pointermove', moveResize);
  document.addEventListener('mousemove', (e) => {delete document.body.dataset.keys; cursorAt(e.clientX, e.clientY);});
  document.addEventListener('pointerup', endResize);
  document.addEventListener('pointercancel', endResize);
  document.addEventListener('dblclick', resetSize);
  // Before the dashboard's own keys, which boot() adds after this.
  document.addEventListener('keydown', panelKey);
  document.addEventListener('contextmenu', (e) => {
    if (!e.target.closest('#pill, .f-head, .g-head, .d-bar')) return;
    e.preventDefault();
    native({type: 'menu'});
  });
  // The surface hugs its content, so anything that changes its size has to re-place the window.
  new ResizeObserver(layoutSoon).observe($('#rail'));
  new MutationObserver(layoutSoon).observe($('#box'), {childList: true, subtree: true, attributes: true, attributeFilter: ['style', 'open', 'hidden']});
  document.addEventListener('input', layoutSoon);
  // The answer folds in place; once it settles, the box may need to grow to hold it.
  $('#box').addEventListener('transitionend', (e) => {if (e.target.classList?.contains('d-fold')) layoutSoon();});
  // Scrolling is remembered too, once it settles.
  $('#box').addEventListener('scroll', () => {clearTimeout(saveTab.timer); saveTab.timer = setTimeout(saveTab, 250);}, true);
  document.addEventListener('toggle', layoutSoon, true);
  document.fonts?.ready.then(layoutSoon);
  window.addEventListener('resize', layoutSoon);
  setTimeout(() => {layout.ready = true; layout();}, 1500);
  setInterval(watchAgents, 1000);
  boot();
  native({type: 'ready'});
}
bootFloat();
