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
// Each action can have several keys. These work from any app.
const SHORTCUTS = {
  toggle: {name: 'Open or minimize the panel', keys: [{keyCode: 4, modifiers: CONTROL_OPTION, label: '⌃⌥H'}]},
  next: {name: 'Next item', keys: [{keyCode: 38, modifiers: CONTROL_OPTION, label: '⌃⌥J'}]},
  previous: {name: 'Previous item', keys: [{keyCode: 40, modifiers: CONTROL_OPTION, label: '⌃⌥K'}]},
  dashboard: {name: 'Open the dashboard', keys: [{keyCode: 2, modifiers: CONTROL_OPTION, label: '⌃⌥D'}]},
  customize: {name: 'Customize the pill', keys: []},
};
// Keys inside the open panel, separate from the hotkeys that work from any app. The defaults are
// what already worked: Enter opens as → does, and Escape goes back as ← does.
const PANEL_KEYS = {
  previous: {name: 'Previous', keys: ['ArrowUp']},
  next: {name: 'Next', keys: ['ArrowDown']},
  open: {name: 'Open', keys: ['ArrowRight', 'Enter']},
  back: {name: 'Back', keys: ['ArrowLeft', 'Escape']},
};
const KEYS_PER_ACTION = 8;
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
// Recording is {action, index}: index is the chip being re-recorded, or null for a new one.
let shortcuts = cleanShortcuts(), notify = cleanNotify(), recording = null, refused = [], keyConflict = null;
let panelKeys = cleanPanelKeys(), recordingPanel = null, cursor = null, sizes = cleanSizes(), resizing = null;
let lastItems = null, lastUsage = null, lastStuck = null, pending = [], notice = null;
let listView = 'needs', agentOpen = null, agentsAll = false, listMotion = null, fallbackIndex = null, pendingScroll = null;
const agentMemory = new Map();

// A global key is its key code and modifiers; its label is only how it reads.
function hotkeyId(key) {return `${key.keyCode}:${key.modifiers}`;}
function sameHotkey(a, b) {return hotkeyId(a) === hotkeyId(b);}
// Saved keys, as lists. One key from before becomes a list of one, and null (none) an empty list;
// nothing saved means the defaults. Junk and repeats go, and a key stays on its first action only.
function cleanShortcuts(saved) {
  const valid = (key) => Number.isInteger(key?.keyCode) && Number.isInteger(key?.modifiers) && typeof key?.label === 'string' && key.label.length <= 40;
  const taken = new Set();
  return Object.fromEntries(Object.entries(SHORTCUTS).map(([action, entry]) => {
    const chosen = saved?.[action];
    const list = chosen === undefined ? entry.keys : chosen === null ? [] : Array.isArray(chosen) ? chosen : valid(chosen) ? [chosen] : entry.keys;
    const keys = list.filter(valid).map(({keyCode, modifiers, label}) => ({keyCode, modifiers, label}))
      .filter((key) => !taken.has(hotkeyId(key)) && Boolean(taken.add(hotkeyId(key))));
    return [action, keys.slice(0, KEYS_PER_ACTION)];
  }));
}
// A key as one string: modifiers in a fixed order, then the key, such as Alt+ArrowLeft or k.
function keyCombo(e) {
  const key = e.key?.length === 1 ? e.key.toLowerCase() : e.key;
  return [e.ctrlKey && 'Ctrl', e.altKey && 'Alt', e.shiftKey && key.length > 1 && 'Shift', e.metaKey && 'Meta', key].filter(Boolean).join('+');
}
const KEY_LABELS = {ArrowUp: '↑', ArrowDown: '↓', ArrowLeft: '←', ArrowRight: '→', Enter: '↵', Escape: 'Esc', Tab: '⇥', ' ': 'Space', Ctrl: '⌃', Alt: '⌥', Shift: '⇧', Meta: '⌘'};
const keyLabel = (combo) => combo.split('+').map((part) => KEY_LABELS[part] || part.toUpperCase()).join('');
// The same for keys inside the panel. One key from before also had Enter (Open) or Escape (Back)
// working beside it, so those join it.
function cleanPanelKeys(saved) {
  const valid = (key) => typeof key === 'string' && key.length > 0 && key.length <= 40;
  const taken = new Set();
  return Object.fromEntries(Object.entries(PANEL_KEYS).map(([action, entry]) => {
    const chosen = saved?.[action];
    let list = chosen === undefined ? entry.keys : chosen === null ? [] : Array.isArray(chosen) ? chosen : valid(chosen) ? [chosen] : entry.keys;
    // One key or none from before: Enter (Open) and Escape (Back) always worked beside it, so they stay.
    if ((typeof chosen === 'string' || chosen === null) && (action === 'open' || action === 'back')) list = [...list, action === 'open' ? 'Enter' : 'Escape'];
    return [action, list.filter(valid).filter((key) => !taken.has(key) && Boolean(taken.add(key))).slice(0, KEYS_PER_ACTION)];
  }));
}
// What a key does in the open panel, wherever focus is not in a text box.
function panelAction(combo) {
  return Object.keys(panelKeys).find((action) => panelKeys[action].includes(combo)) || null;
}
// Which action holds a key already.
function keyOwner(chosen, key, same = (a, b) => a === b) {
  for (const [action, keys] of Object.entries(chosen)) {
    const at = keys.findIndex((k) => same(k, key));
    if (at >= 0) return {action, at};
  }
  return null;
}
// A key onto an action: in place of the chip being re-recorded, or after the others. Taken from
// any other action that had it, so no key ever does two things.
function placeKey(chosen, action, index, key, same = (a, b) => a === b) {
  const was = Number.isInteger(index) ? chosen[action][index] : null;
  const next = Object.fromEntries(Object.entries(chosen).map(([name, keys]) => [name, keys.filter((k) => !same(k, key))]));
  const at = was ? next[action].findIndex((k) => same(k, was)) : -1;
  if (at >= 0) next[action][at] = key;
  else next[action].push(key);
  next[action] = next[action].slice(0, KEYS_PER_ACTION);
  return next;
}
const dropKey = (chosen, action, index) => ({...chosen, [action]: chosen[action].filter((_, n) => n !== index)});
// Back to an action's own defaults, taking them from wherever else they went.
function resetKeys(chosen, action, defaults, same = (a, b) => a === b) {
  const wanted = defaults[action].keys;
  return Object.fromEntries(Object.entries(chosen).map(([name, keys]) => [name, name === action ? [...wanted] : keys.filter((k) => !wanted.some((w) => same(w, k)))]));
}
const keysAreDefault = (chosen, action, defaults, same = (a, b) => a === b) =>
  chosen[action].length === defaults[action].keys.length && chosen[action].every((k, n) => same(k, defaults[action].keys[n]));
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
// Bindings the window should hold, one per key, in the shape it registers.
function hotkeyList(chosen) {
  return Object.entries(chosen).flatMap(([action, keys]) => keys.map((key) => ({action, keyCode: key.keyCode, modifiers: key.modifiers})));
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
// A change mark's dot sits inside the circle, before the number, so the number still rolls and
// the dot follows the circle when the spotlight scales it.
function circleHTML(r, style = feel.style, mark = null) {
  const empty = r.value === null, text = empty ? '–' : shortCount(r.value);
  const classes = ['f-circle', r.gauge && !empty ? 'gauge' : '', empty ? 'none' : '', r.loud && style === 'attention' ? 'loud' : '',
    r.live ? 'live' : '', r.low ? 'low' : '', r.stale ? 'stale' : '', text.length > 2 ? 'wide' : ''].filter(Boolean).join(' ');
  const ring = r.gauge && !empty ? `<svg viewBox="0 0 36 36" aria-hidden="true"><circle class="track" cx="18" cy="18" r="16" pathLength="100"/><circle class="fill" cx="18" cy="18" r="16" pathLength="100" stroke-dasharray="${Number(r.value)} 100"/></svg>` : '';
  const dot = mark?.kind === 'dot' ? `<span class="f-mark-dot ${esc(mark.tone)}"></span>` : '';
  return `<span class="${classes}">${ring}${dot}<span>${esc(text)}</span></span>`;
}
function pillHTML(items, live = liveHealthy, marked = {}) {
  const rows = circles.map((id) => {
    const r = METRICS[id].read(items);
    return `<span class="f-row" data-metric="${id}">${circleHTML(r, feel.style, marked[id])}<span class="f-tag">${esc(r.tag)}</span></span>`;
  }).join('');
  return `${live ? '' : '<span class="f-row f-offline"><span class="dot err"></span><span class="f-tag">Reconnecting…</span></span>'}${rows}`;
}
function pillLabel(items, live = liveHealthy) {
  const now = Date.now() / 1000;
  return `HQ. ${circles.map((id) => {
    const changed = markHistory(marks.log, id, now);
    return `${METRICS[id].read(items).tag}${changed ? `, ${changed.summary}` : ''}`;
  }).join('. ')}.${live ? '' : ' Reconnecting.'}`;
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
// A row of keys: the action, then its keys as chips. Clicking a chip records it again, its ×
// (on hover or focus) or Delete removes it, the round + records another, and Reset brings back
// the defaults. With no keys, a dashed Add. A key another action has asks before it moves.
function keyRowHTML(kind, action, name, keys, defaults, same) {
  const global = kind === 'global', attr = global ? 'data-record' : 'data-record-panel', live = global ? recording : recordingPanel;
  const label = (key) => (global ? key.label : keyLabel(key));
  const now = live?.action === action ? live : null;
  const chips = keys.map((key, n) => {
    const on = now?.index === n, bad = global && (refused.includes(hotkeyId(key)) || refused.includes(action));
    return `<span class="f-chip ${bad ? 'bad' : ''}"><button type="button" class="f-cap ${on ? 'live' : ''}" ${attr}="${action}" data-index="${n}" aria-label="${esc(`${label(key)} for ${name}${bad ? ', in use by another app' : ''}: click to change, Delete to remove`)}"${bad ? ' title="In use by another app"' : ''}>${esc(on ? 'Press keys…' : label(key))}</button><button type="button" class="f-chip-x" tabindex="-1" data-drop-key="${kind}" data-action="${action}" data-index="${n}" aria-label="${esc(`Remove ${label(key)} from ${name}`)}">${icon('x', 'sm')}</button></span>`;
  }).join('');
  // A key macOS refused is outlined on its chip and named under the row, so the chips stay compact.
  const refusedKeys = global ? keys.filter((key) => refused.includes(hotkeyId(key)) || refused.includes(action)) : [];
  const taken = refusedKeys.length ? `<small class="f-key-bad">${esc(refusedKeys.map(label).join(', '))} ${refusedKeys.length === 1 ? 'is' : 'are'} in use by another app</small>` : '';
  const adding = now && now.index === null;
  const more = keys.length
    ? `<button type="button" class="icon-btn f-plus ${adding ? 'live' : ''}" ${attr}="${action}" data-index="new" aria-label="${esc(`Add another key for ${name}`)}" title="Add another key">${adding ? '<span class="f-plus-live">Press keys…</span>' : icon('plus', 'sm')}</button>`
    : `<button type="button" class="f-cap empty ${adding ? 'live' : ''}" ${attr}="${action}" data-index="new" aria-label="${esc(`Add a key for ${name}`)}">${adding ? 'Press keys…' : 'Add'}</button>`;
  const reset = keysAreDefault(global ? shortcuts : panelKeys, action, defaults, same) ? '' : `<button type="button" class="f-reset" data-reset-key="${kind}" data-action="${action}">Reset</button>`;
  const c = keyConflict?.kind === kind && keyConflict.action === action ? keyConflict : null;
  const note = c ? `<div class="f-conflict" role="alert"><span>${esc(label(c.key))} is used by ${esc(defaults[c.from].name)}. Move it here?</span><button type="button" class="f-reset" data-move-key>Move</button><button type="button" class="f-reset" data-move-cancel>Cancel</button></div>` : '';
  return `<div class="f-key"><span class="f-key-name">${esc(name)}${reset}</span><span class="f-chips">${chips}${more}</span>${taken}${note}</div>`;
}
function shortcutsHTML() {
  const rows = Object.entries(SHORTCUTS).map(([action, entry]) => keyRowHTML('global', action, entry.name, shortcuts[action], SHORTCUTS, sameHotkey)).join('');
  return `<div class="f-section">Keyboard shortcuts</div>${rows}<p class="f-hint">These work from any app. Modifiers alone work too: press ⌃ and left ⌥ together, then let go. While recording, Delete clears a key and Escape cancels.</p>`;
}
function panelKeysHTML() {
  const rows = Object.entries(PANEL_KEYS).map(([action, entry]) => keyRowHTML('panel', action, entry.name, panelKeys[action], PANEL_KEYS)).join('');
  return `<div class="f-section">Keys inside the panel</div>${rows}<p class="f-hint">While the panel is open. While recording, Delete clears a key and Escape cancels.</p>`;
}
// Recording finished with a key: onto its action, unless another action has it, which asks first.
function recordedKey(kind, at, key) {
  const global = kind === 'global', chosen = global ? shortcuts : panelKeys, same = global ? sameHotkey : undefined;
  const owner = keyOwner(chosen, key, same);
  if (owner && owner.action !== at.action) {
    keyConflict = {kind, action: at.action, index: at.index, key, from: owner.action};
    return;
  }
  keyConflict = null;
  if (global) shortcuts = placeKey(shortcuts, at.action, at.index, key, sameHotkey);
  else panelKeys = placeKey(panelKeys, at.action, at.index, key);
  saveSettings();
}
function moveKey() {
  const c = keyConflict;
  if (!c) return;
  keyConflict = null;
  if (c.kind === 'global') shortcuts = placeKey(shortcuts, c.action, c.index, c.key, sameHotkey);
  else panelKeys = placeKey(panelKeys, c.action, c.index, c.key);
  saveSettings();
  if (c.kind === 'global') applyShortcuts();
  render();
}
function dropChip(kind, action, index) {
  if (kind === 'global') {shortcuts = dropKey(shortcuts, action, index); applyShortcuts();}
  else panelKeys = dropKey(panelKeys, action, index);
  keyConflict = null;
  saveSettings();
  render();
  // Focus stays in the row: on the chip that took its place, or the + or Add.
  const row = [...document.querySelectorAll(`[${kind === 'global' ? 'data-record' : 'data-record-panel'}="${action}"]`)];
  (row[Math.min(index, row.length - 1)] || row.at(-1))?.focus();
}
function floatListHTML(items, live = liveHealthy) {
  const f = needsFilter(), stuck = stuckItems().length;
  const head = needsHeadHTML(f, items, {extra: offlineNote(live), more: moreButton, elsewhere: elsewhereHTML()});
  const rows = needsBodyHTML(f, items, (i) => rowHTML(i, {where: f.group !== 'project', age: true, kind: true, source: true}),
    (narrowed) => (narrowed ? needsEmpty(true) : `<div class="f-empty">${EMPTY.you[0]}</div>`));
  const quiet = stuck ? `<button class="f-quiet f-stuck" type="button" data-view="stuck">${stuckWords(stuck)}</button>` : '';
  return `${head}<div class="f-rows scroll"><div class="f-rows-in">${rows}${quiet}</div></div>`;
}
// Not acted on is a view of your own decisions, so it is the view menu's last entry. Agents is
// a list of its own, reached only from its circle on the pill.
function elsewhereHTML() {
  const stuck = stuckItems().length;
  return stuck ? `<div class="p-rule"></div><button class="g-opt" type="button" data-view="stuck"><span class="tick"></span><span class="o-text">Not acted on</span><span class="n">${stuck}</span></button>` : '';
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
// Agents stands on its own: the needs circle opens Needs you and the agents circle opens this,
// so the header is just its name and how many are running.
function agentsTitle() {
  const running = model.agents.filter((a) => agentState(a) === 'active').length;
  return `<span class="f-headline"><h1>Agents</h1>${running ? `<span class="f-count">${running} running</span>` : ''}</span>`;
}
const offlineNote = (live) => (live ? '' : '<span class="f-off"><span class="dot err"></span>Reconnecting…</span>');
const moreButton = `<button class="icon-btn f-push" type="button" data-more aria-label="More" title="More">${icon('more')}</button>`;
// Agents, by the folder each one works in. Click one for its page.
function agentsPanelHTML(items, live = liveHealthy) {
  const view = agentFolders(state.sessions, model.items), now = Date.now() / 1000;
  const shown = agentOpen && view.all.find((a) => a.id === agentOpen);
  if (shown) return `<header class="f-head"><button class="f-return" type="button" data-back>${icon('back', 'sm')}Agents</button>${offlineNote(live)}${moreButton}</header><div class="f-rows scroll"><div class="f-rows-in">${agentPageHTML(shown, now)}</div></div>`;
  heldStill();
  const v = unactedAgents(state.sessions, model.items, now);
  // The whole row opens the agent; its count, above that button, opens the sheet instead.
  const agent = (a, side = agentSideHTML(a, v.held.get(a.id)), meta = `${esc(a.harness)} · ${agentMeta(a, now)}`) =>
    `<div class="f-agent" data-agent="${esc(a.id)}"><button class="f-hit" type="button" aria-label="${esc(a.name)}"></button>${agentDot(a)}<span class="a-main"><span class="a-name">${esc(a.name)}</span><span class="a-meta">${meta}</span></span><span class="a-side">${side}</span></div>`;
  const folder = (f) => `<div class="f-folder"><div class="f-top"><span class="f-name">${esc(f.name)}</span>${f.running ? '<span class="f-run">running</span>' : ''}</div>${f.agents.map((a) => agent(a)).join('')}</div>`;
  let body;
  if (agentsShow === 'unacted') {
    body = v.rows.length ? `<p class="ag-sum">${unactedSummary(v)}</p>${v.folders.map((f) => `<div class="f-folder"><div class="f-top"><span class="f-name">${esc(f.name)}</span></div>${f.rows.map((r) => agent(r.a, unactedSide(r, v), `${esc(r.a.harness)} · ${esc(lastRan(r.a))}`)).join('')}</div>`).join('')}`
      : `<div class="f-empty ag-empty">${UNACTED_EMPTY}</div>`;
  } else {
    const quiet = view.quiet.length ? (agentsAll ? view.quiet.map(folder).join('')
      : `<button class="f-quiet" type="button" data-agents-all>${view.quietCount} more, in ${view.quiet.length} ${view.quiet.length === 1 ? 'folder' : 'folders'}</button>`) : '';
    body = (view.groups.map(folder).join('') || '<div class="f-empty">No agent has run in the last day</div>') + quiet;
  }
  const held = heldSheet && v.held.get(heldSheet), who = held && view.all.find((a) => a.id === heldSheet);
  const shut = who ? ' inert' : '';
  return `<header class="f-head">${agentsTitle()}${offlineNote(live)}${moreButton}</header><div class="ag-bar"${shut}>${agentsShowHTML(agentsShow, v)}</div><div class="f-rows scroll"${shut}><div class="f-rows-in">${body}</div></div>${who ? heldSheetHTML(who, held, {closing: heldClosing, note: heldNote}) : ''}`;
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
// Usage: the Claude or Codex circle on the pill opens a card for that tool, with every account
// HQ keeps for it, how much each has left and when it comes back, and one tap to use another;
// the other tool waits below as one line. Switching swaps the tool's login, as /login does, so a
// notice says what moved and offers to switch back. Adding never switches.
const USAGE_TOOLS = {claude: {name: 'Claude', app: 'Claude Code'}, codex: {name: 'Codex', app: 'Codex'}};
const USAGE_HOUR = 3600, USAGE_DAY = 86400;
const usageView = {tool: 'claude', data: null, live: false, loading: null, flow: null, toast: null, undo: null, toastFor: null, timer: null, seen: ''};
// The accounts API, or, where it is missing, today's single reading for each tool as the account
// signed in now, so the view never comes up empty.
function usageModel(raw, usage = state.usage) {
  const windows = (u) => (u?.windows || []).filter((w) => Number.isFinite(w.left)).map((w) => ({name: w.name, left: w.left, resetsAt: w.resets_at || null}));
  if (raw?.claude && raw?.codex) {
    const tool = (t) => ({active: t.active || null, adding: t.adding || null, running: Number(t.running) || 0,
      accounts: (t.accounts || []).map((a) => ({key: a.key, email: a.email || '', plan: a.plan || '', active: Boolean(a.active), saved: a.saved !== false,
        state: ['ok', 'expired', 'needs sign-in'].includes(a.state) ? a.state : 'ok', windows: windows(a.usage), asOf: a.usage?.as_of ?? null}))});
    return {live: true, claude: tool(raw.claude), codex: tool(raw.codex)};
  }
  const one = (name) => {
    const u = usage?.[name], w = windows(u);
    return {active: w.length ? 'current' : null, adding: null, running: 0,
      accounts: w.length ? [{key: 'current', email: '', plan: '', active: true, saved: false, state: 'ok', windows: w, asOf: u.as_of ?? null}] : []};
  };
  return {live: false, claude: one('claude'), codex: one('codex')};
}
const usageFresh = (a, now = Date.now() / 1000) => a.asOf !== null && now - a.asOf < USAGE_DAY && a.windows.length > 0;
const usageStale = (a, now = Date.now() / 1000) => a.state === 'expired' || (usageFresh(a, now) && now - a.asOf >= USAGE_HOUR);
const usageTightest = (a) => (usageFresh(a) ? a.windows.reduce((m, w) => (!m || w.left < m.left ? w : m), null) : null);
function untilWords(seconds) {
  const s = Math.max(0, Math.round(seconds)), d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${String(m).padStart(2, '0')}m` : `${m}m ${String(s % 60).padStart(2, '0')}s`;
}
const usageUntil = (at) => `<span class="u-until" data-until="${at}">in ${untilWords(at - Date.now() / 1000)}</span>`;
const usagePct = (left) => (left <= USAGE_LOW ? `<span class="bad">${left}% left</span>` : `${left}% left`);
function usageRing(left, {cls = '', dim = false} = {}) {
  const empty = left === null || left === undefined, low = !empty && left <= USAGE_LOW;
  return `<span class="u-ring ${cls} ${low ? 'low' : ''} ${dim ? 'dim' : ''} ${empty ? 'u-none' : ''}" aria-label="${empty ? 'No reading' : `${left}% left`}"><svg viewBox="0 0 36 36" aria-hidden="true"><circle class="track" cx="18" cy="18" r="16" pathLength="100"/>${!empty && left > 0 ? `<circle class="fill" cx="18" cy="18" r="16" pathLength="100" stroke-dasharray="${left} 100"/>` : ''}</svg><b>${empty ? '–' : left}</b></span>`;
}
const usageWho = (a) => `<span class="u-who"><span class="u-email" title="${esc(a.email || 'Signed in now')}">${esc(a.email || 'Signed in now')}</span>${a.plan ? `<span class="u-plan">${esc(a.plan)}</span>` : ''}</span>`;
function usageLines(a, now = Date.now() / 1000) {
  if (a.state === 'needs sign-in') return '<span class="u-line"><span class="bad">Needs sign-in</span> · its saved login lapsed</span>';
  if (!usageFresh(a, now)) return '<span class="u-line old">No reading in the last day</span>';
  return a.windows.map((w) => `<span class="u-line"><span class="u-part">${esc(w.name)} ${usagePct(w.left)}</span>${w.resetsAt ? ` · <span class="u-part">back ${usageUntil(w.resetsAt)}</span>` : ''}</span>`).join('')
    + (usageStale(a, now) ? `<span class="u-line old">As of ${ago(a.asOf)}</span>` : '');
}
// What a switch reaches, said before you make it.
const usageReach = (tool) => (tool === 'claude' ? 'Every Claude session moves to the one you pick.' : 'Open Codex sessions keep the old account until restarted.');
const usageActive = (t) => t.accounts.find((a) => a.active) || null;
// The add or sign-in step, in place.
function usageAddHTML(tool, f, data) {
  const t = data[tool], who = f.key && t.accounts.find((a) => a.key === f.key), now = usageActive(t), app = USAGE_TOOLS[tool].app;
  const go = '<div class="u-send"><button class="ghost-pill" type="button" data-u="cancel">Cancel</button><button class="u-wide" type="button" data-u="signin">Sign in with your browser</button></div>';
  if (f.step === 'waiting') {
    const where = f.job?.terminal ? 'the Terminal window HQ opened' : 'the browser tab HQ opened';
    return `<div class="u-sheet" data-u-sheet="add"><div class="u-title"><span class="u-spin" aria-hidden="true"></span>Waiting for you to sign in…</div><p>Finish in ${where}${who ? `, as ${esc(who.email)}` : ''}.</p><div class="u-send"><button class="ghost-pill" type="button" data-u="cancel">Cancel</button></div></div>`;
  }
  if (f.step === 'failed') {
    return `<div class="u-sheet" data-u-sheet="add"><div class="u-title">The sign-in didn't finish</div><p>${esc(f.job?.message || 'HQ could not add the account.')}</p><div class="u-send"><button class="ghost-pill" type="button" data-u="cancel">Close</button><button class="u-wide" type="button" data-u="signin">Try again</button></div></div>`;
  }
  if (who) return `<div class="u-sheet" data-u-sheet="add"><div class="u-title">Sign ${esc(who.email)} in again</div><p>Its saved login lapsed.${now && now !== who ? ` ${app} stays signed in as ${esc(now.email)}.` : ''}</p>${go}</div>`;
  return `<div class="u-sheet" data-u-sheet="add"><div class="u-title">Add a ${USAGE_TOOLS[tool].name} account</div><p>Nothing changes for ${app} until you switch to it.</p>${go}</div>`;
}
function usageRemoveHTML(a) {
  return `<div class="u-sheet" data-u-sheet="remove"><div class="u-title">Remove ${esc(a.email)} from HQ?</div><p>HQ deletes the login it stored for it. You stay signed in wherever else you use it.</p><div class="u-send"><button class="ghost-pill" type="button" data-u="cancel">Cancel</button><button class="u-wide" type="button" data-u="remove" data-key="${esc(a.key)}">Remove</button></div></div>`;
}
// B: the tool you clicked as a card; the other tool as one line under it.
function usageCardHTML(data, tool, f = null) {
  const t = data[tool], other = tool === 'claude' ? 'codex' : 'claude', o = data[other], active = usageActive(t);
  const rows = t.accounts.map((a) => {
    const w = usageTightest(a), id = esc(a.key);
    const side = a.active ? `<span class="u-tick">${icon('check', 'sm')}Active</span>`
      : a.state === 'needs sign-in' ? `<button class="u-btn" type="button" data-u="again" data-key="${id}">Sign in again</button>`
      : data.live ? `<button class="u-btn" type="button" data-u="use" data-key="${id}">Use</button>` : '';
    const remove = !a.active && a.saved && data.live ? `<button class="icon-btn u-x" type="button" data-u="ask-remove" data-key="${id}" aria-label="Remove ${esc(a.email)} from HQ" title="Remove from HQ">${icon('x', 'sm')}</button>` : '';
    const sheet = f?.key === a.key ? (f.kind === 'remove' ? usageRemoveHTML(a) : usageAddHTML(tool, f, data)) : '';
    return `<div class="u-acct" data-acct="${id}"><div class="u-row">${usageRing(w ? w.left : null, {dim: usageStale(a)})}<div class="u-mid">${usageWho(a)}${usageLines(a)}</div><div class="u-side">${side}${remove}</div></div>${sheet}</div>`;
  }).join('') || `<p class="u-empty">${USAGE_TOOLS[tool].app} isn't signed in on this Mac.</p>`;
  const adding = f?.kind === 'add' && !f.key && f.tool === tool;
  const add = !data.live ? '' : adding ? usageAddHTML(tool, f, data)
    : `<button class="u-add" type="button" data-u="add" data-tool="${tool}">${icon('plus', 'sm')}Add a ${USAGE_TOOLS[tool].name} account</button>`;
  const card = `<section class="u-card" aria-label="${USAGE_TOOLS[tool].name}"><div class="u-card-h"><h2>${USAGE_TOOLS[tool].name}</h2><span class="u-using">${active ? `uses ${esc(active.email || 'the signed-in account')}` : 'not signed in'}</span></div>
    ${data.live ? `<p class="u-note">${usageReach(tool)}</p>` : ''}${rows}${add}</section>`;
  const theirs = usageActive(o), tw = theirs && usageTightest(theirs);
  const mini = `<button class="u-mini" type="button" data-u="tool" data-tool="${other}">${usageRing(tw ? tw.left : null)}<span class="u-mid"><span class="u-mini-h">${USAGE_TOOLS[other].name}</span><span class="u-using">${theirs ? `uses ${esc(theirs.email || 'the signed-in account')}` : 'not signed in'}${data.live ? ` · ${plural(o.accounts.length, 'account', 'accounts')}` : ''}</span></span></button>`;
  return card + mini;
}
// The notice: what happened in two lines, an address giving way in the middle of its name.
function usageToastHTML(t, undo) {
  const at = t.email.lastIndexOf('@'), name = at > 0 ? t.email.slice(0, at) : t.email, domain = at > 0 ? t.email.slice(at) : '';
  return `<div class="u-veil" aria-hidden="true"></div><div class="u-toast" role="status"><span class="u-t-text"><span class="u-sr">${esc(t.lead + t.email)}.</span><strong class="u-t-title" aria-hidden="true" title="${esc(t.email)}"><span class="u-t-lead">${esc(t.lead)}</span><span class="u-t-local" data-full="${esc(name)}">${esc(name)}</span><span class="u-t-domain">${esc(domain)}</span></strong>${t.body?.length ? `<span class="u-t-body">${esc(t.body[0])}</span>` : ''}</span>${undo ? `<button class="u-btn" type="button" data-u="undo">${esc(undo.label)}</button>` : ''}</div>`;
}
// The second line after a switch, as whole sentences, longest first; the notice keeps the longest
// that fits on its line, so it is never cut mid-sentence. Claude's is the short form; Codex's
// warning loses the words the first line already says, then shortens step by step.
function switchedBody(tool, reply) {
  if (tool === 'claude') return ['Every Claude session moves to it.'];
  if (!reply?.warning) return reply?.note ? [String(reply.note)] : [];
  const said = String(reply.warning).replace(/^Switched to \S+\.\s+/, '');
  const n = Number(said.match(/\d+/)?.[0]) || null, them = n === 1 ? 'session' : 'sessions';
  const shorter = n ? [`Restart ${n} open Codex ${them} to use it.`, `Restart ${n} Codex ${them} to use it.`] : [];
  return [...new Set([said, ...shorter, 'Restart Codex to use it.'])];
}// Reading and acting. Every request carries the token; the accounts API may be missing (an
// older HQ, or the lab), and then the view shows today's readings and offers no actions.
async function loadAccounts() {
  if (usageView.loading) return usageView.loading;
  usageView.loading = api('accounts').then((raw) => {usageView.raw = raw;}).catch(() => {usageView.raw = null;})
    .finally(() => {usageView.loading = null; if (listView === 'usage') render();});
  return usageView.loading;
}
// /api/state carries a summary every 15s; when it changes, the open view reads the details again.
function followAccounts() {
  const seen = JSON.stringify(state.accounts || null);
  if (seen === usageView.seen) return;
  usageView.seen = seen;
  if (floatOpen && listView === 'usage') loadAccounts();
}
function usageData() {return usageModel(usageView.raw, state.usage);}
function usageNotice(note) {
  clearTimeout(usageView.timer);
  usageView.toast = {lead: note.lead, email: note.email, body: [].concat(note.body || [])};
  usageView.undo = note.undo || null;
  render();
  usageView.timer = setTimeout(dismissUsageNotice, 9000);
}
function dismissUsageNotice() {
  clearTimeout(usageView.timer);
  if (!usageView.toast) return false;
  usageView.toast = usageView.undo = null;
  render();
  return true;
}
async function useAccount(tool, key) {
  const before = usageActive(usageData()[tool]);
  try {
    const reply = await api(`accounts/${tool}/${key}/use`, {});
    await loadAccounts();
    if (reply.already) return render();
    usageNotice({lead: 'Switched to ', email: reply.account?.email || '', body: switchedBody(tool, reply),
      // HQ saves the login it switches away from, so switching back needs no sign-in.
      undo: before && before.key !== key ? {kind: 'use', tool, key: before.key, label: 'Switch back'} : null});
  } catch (error) {
    toast(error.message);
  }
}
async function removeAccount(tool, key) {
  try {
    const reply = await api(`accounts/${tool}/${key}/remove`, {});
    usageView.flow = null;
    await loadAccounts();
    usageNotice({lead: 'Removed ', email: reply.email || '', body: 'HQ deleted the login it stored for it.'});
  } catch (error) {
    toast(error.message);
  }
}
// Adding is a sign-in that HQ keeps; nothing changes for the tool until you switch to it.
async function startSignIn() {
  const f = usageView.flow;
  if (!f || f.kind !== 'add') return;
  try {
    f.job = await api(`accounts/${f.tool}/add`, {});
    f.step = 'waiting';
    render();
    pollSignIn(f);
  } catch (error) {
    f.step = 'failed';
    f.job = {message: error.message};
    render();
  }
}
async function pollSignIn(f) {
  while (usageView.flow === f && f.step === 'waiting' && f.job?.id) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    if (usageView.flow !== f) return;
    let job;
    try {
      job = await api(`accounts/jobs/${f.job.id}`);
    } catch {
      continue;
    }
    f.job = job;
    if (job.state === 'added') {
      usageView.flow = null;
      await loadAccounts();
      const a = job.account;
      return usageNotice({lead: 'Added ', email: a?.email || '', body: 'Nothing changes until you switch.',
        undo: a && !a.active ? {kind: 'use', tool: f.tool, key: a.key, label: 'Switch to it'} : null});
    }
    if (job.state === 'failed') {f.step = 'failed'; return render();}
    if (job.state === 'cancelled') {usageView.flow = null; return render();}
    render();
  }
}
async function cancelUsageFlow() {
  const f = usageView.flow;
  usageView.flow = null;
  render();
  if (f?.kind === 'add' && f.step === 'waiting' && f.job?.id) {
    try {await api(`accounts/jobs/${f.job.id}/cancel`, {});} catch {/* it may have just finished */}
  }
}
function usageClick(e) {
  const el = e.target.closest('[data-u]');
  if (!el || !el.closest('#main')) return false;
  const act = el.dataset.u, key = el.dataset.key, tool = usageView.tool;
  if (act === 'tool') {usageView.tool = el.dataset.tool; usageView.flow = null; saveTab(); render();}
  else if (act === 'use') useAccount(tool, key);
  else if (act === 'add') {usageView.flow = {kind: 'add', tool, key: null, step: 'intro'}; render();}
  else if (act === 'again') {usageView.flow = {kind: 'add', tool, key, step: 'intro'}; render();}
  else if (act === 'signin') startSignIn();
  else if (act === 'ask-remove') {usageView.flow = usageView.flow?.kind === 'remove' && usageView.flow.key === key ? null : {kind: 'remove', tool, key}; render();}
  else if (act === 'remove') removeAccount(tool, key);
  else if (act === 'cancel') cancelUsageFlow();
  else if (act === 'undo') {const u = usageView.undo; dismissUsageNotice(); if (u?.kind === 'use') useAccount(u.tool, u.key);}
  else if (act === 'close') setOpen(false);
  else return false;
  e.preventDefault();
  e.stopPropagation();
  return true;
}
// Escape steps back one thing at a time: the notice, then an open step, then the panel.
function usageEscape() {
  if (dismissUsageNotice()) return true;
  if (usageView.flow) {cancelUsageFlow(); return true;}
  return false;
}
// The Usage view in the panel: its header with a corner close, the card, and the notice.
function usagePanelHTML(live = liveHealthy) {
  const head = `<header class="f-head"><span class="f-headline"><h1>Usage</h1></span>${offlineNote(live)}<button class="icon-btn f-push" type="button" data-u="close" aria-label="Back to the pill" title="Close  Esc">${icon('x')}</button></header>`;
  const toast = usageView.toast ? usageToastHTML(usageView.toast, usageView.undo) : '';
  return `${head}<div class="f-rows scroll"><div class="f-rows-in u-body">${usageCardHTML(usageData(), usageView.tool, usageView.flow)}</div></div>${toast}`;
}
// A long address gives way in the middle of its name: the longest start that fits, then "…",
// set right against the domain.
function fitAddress(el) {
  if (!el) return;
  const full = el.dataset.full || '';
  el.textContent = full;
  if (el.scrollWidth <= el.clientWidth + 0.5) return;
  let lo = 1, hi = full.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    el.textContent = `${full.slice(0, mid)}…`;
    if (el.scrollWidth <= el.clientWidth + 0.5) lo = mid; else hi = mid - 1;
  }
  el.textContent = `${full.slice(0, lo).replace(/[.\-_]$/, '')}…`;
}
// After a paint: the notice's address fitted, its veil sized, and a new notice rising in.
function afterUsage() {
  const note = $('#main .u-toast'), veil = $('#main .u-veil');
  if (!note) {usageView.shown = null; return;}
  fitAddress($('#main .u-t-local'));
  // The longest whole sentence that fits on the second line.
  const body = $('#main .u-t-body'), forms = usageView.toast?.body || [];
  for (let n = 1; body && n < forms.length && body.scrollWidth > body.clientWidth + 0.5; n += 1) body.textContent = forms[n];
  veil.style.height = `${8 + note.offsetHeight + 24}px`;
  if (usageView.shown !== usageView.toast && !calm()) {
    note.animate([{opacity: 0, transform: 'translateY(14px) scale(.97)'}, {opacity: 1, transform: 'none'}], {duration: 460, easing: spring('--f-spring')});
    veil.animate([{opacity: 0}, {opacity: 1}], {duration: 300, easing: 'ease-out'});
  }
  usageView.shown = usageView.toast;
}
// Countdowns tick while the view is open; nothing else repaints for them.
function tickUsage() {
  if (!floatOpen || listView !== 'usage') return;
  const now = Date.now() / 1000;
  document.querySelectorAll('#main .u-until[data-until]').forEach((el) => {el.textContent = `in ${untilWords(Number(el.dataset.until) - now)}`;});
}

// Each tab keeps where you were in it: what it had open, its highlight and its scroll. Closing
// the panel keeps it, and so does a restart, since only ids and positions are stored.
const TABS_KEY = 'hq-float-tabs', TAB_NAMES = ['needs', 'agents', 'stuck', 'usage'];
const blankTab = () => ({item: null, agent: null, cursor: null, index: 0, scroll: 0, body: 0, itemTab: null, all: false, filter: blankNeeds('none'), show: 'all', sheet: null, tool: 'claude'});
const idOrNull = (v) => (typeof v === 'string' && v.length > 0 && v.length <= 200 ? v : null);
const place = (v) => (Number.isFinite(v) && v >= 0 ? Math.min(Math.round(v), 1e6) : 0);
function cleanTabs(saved) {
  const tabs = Object.fromEntries(TAB_NAMES.map((name) => {
    const t = saved?.tabs?.[name];
    if (!t || typeof t !== 'object') return [name, blankTab()];
    return [name, {item: idOrNull(t.item), agent: idOrNull(t.agent), cursor: idOrNull(t.cursor), index: place(t.index), scroll: place(t.scroll),
      body: place(t.body), itemTab: ['steps', 'why', 'context', 'ask'].includes(t.itemTab) ? t.itemTab : null, all: t.all === true,
      filter: cleanNeeds(t.filter, 'none'), show: t.show === 'unacted' ? 'unacted' : 'all', sheet: idOrNull(t.sheet), tool: t.tool === 'codex' ? 'codex' : 'claude'}];
  }));
  return {view: TAB_NAMES.includes(saved?.view) ? saved.view : 'needs', tabs};
}
let tabState = cleanTabs(remember(TABS_KEY));
listView = tabState.view;
needsFilter = () => tabState.tabs.needs.filter;
needsBase = () => attention();
saveNeeds = () => {saveTab();};
saveAgentsShow = () => {saveTab();};
openHeldItem = (id) => openItem(id);
openHeldPage = (agent) => showList('agents', agent);
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
    all: listView === 'agents' && agentsAll, show: listView === 'agents' ? agentsShow : t.show, sheet: listView === 'agents' ? heldSheet : null, tool: listView === 'usage' ? usageView.tool : t.tool || 'claude'});
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
  // Agents comes back on the list you chose, with its sheet if one was open.
  if (view === 'agents') agentsShow = t.show || 'all';
  heldSheet = view === 'agents' ? t.sheet : null;
  heldClosing = null;
  // Usage comes back on the tool you left, and reads its accounts again.
  if (view === 'usage') {usageView.tool = t.tool || 'claude'; usageView.flow = null; loadAccounts();}
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
  return {needs: 'needs', decisions: 'needs', due: 'needs', agents: 'agents', stuck: 'stuck', claude: 'usage', codex: 'usage'}[metric] || null;
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
  if (!route.item && listView === 'agents' && heldKey(e)) {e.stopImmediatePropagation(); return;}
  if (!route.item && listView === 'agents' && (heldSheet || e.target.closest?.('[data-held], .ag-show'))) return;
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
  const at = recordingPanel;
  recordingPanel = null;
  if (e.key === 'Escape') return render();
  if (['Backspace', 'Delete'].includes(e.key)) {
    if (Number.isInteger(at.index)) {panelKeys = dropKey(panelKeys, at.action, at.index); saveSettings();}
    return render();
  }
  recordedKey('panel', at, keyCombo(e));
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
  // Open, or just closing, you have seen everything up to now.
  if (floatOpen || (lastMode && lastMode !== 'pill')) marks.seen = Date.now() / 1000;
  const mode = surfaceMode(floatOpen, route.item, customizing);
  const kind = motionKind(lastMode, mode, lastItem, route.item, visibleIds(), flown);
  flown = false;
  const outgoing = (kind === 'next' || kind === 'prev') && !$('#detail').hidden ? keepCard() : null;
  const scrolls = ['.f-rows', '.d-body'].map((sel) => [sel, document.querySelector(sel)?.scrollTop]);
  const pill = $('#pill');
  setHTML($('#rail'), pillHTML(items, live, markMap()));
  rollChanged(items);
  pill.setAttribute('aria-label', pillLabel(items, live));
  const tops = !listMotion && listView === 'agents' ? agentTops() : null;
  const finding = document.activeElement?.matches?.('[data-g-q]') ? [document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;
  const sheet = keepSheet();
  setHTML($('#main'), customizing ? settingsHTML(items) : listView === 'agents' ? agentsPanelHTML(items, live) : listView === 'usage' ? usagePanelHTML(live) : listView === 'stuck' ? stuckPanelHTML(live) : floatListHTML(items, live));
  sheet();
  if (listView === 'usage') afterUsage();
  followAccounts();
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
  recordMarks(events);
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

// Change marks: what the Island said, left behind once it folds. A signed badge stays beside
// the circle for two minutes, a burst netted into one number; then a dot stays on the circle
// until you open the panel. Hovering a circle lists its last few changes. Amber is more needing
// you, green fewer. Badges live in their own layer, so the capsule never moves or grows.
const MARK_BADGE = 120, MARK_FADE = 30, MARK_HISTORY = 600, MARK_KEEP = 40;
// The tone when it went up, and when it went down: for usage, down is the worse way.
const MARK_TONES = {needs: ['more', 'fewer'], stuck: ['more', 'fewer'], agents: ['same', 'same'], claude: ['fewer', 'more'], codex: ['fewer', 'more']};
const MARK_WORDS = {needs: ['added', 'done'], stuck: ['not acted on', 'acted on'], agents: ['started', 'stopped'], claude: ['back', 'used'], codex: ['back', 'used']};
const markUnit = (metric) => (metric === 'claude' || metric === 'codex' ? '%' : '');
const markTone = (metric, n) => (n === 0 ? 'same' : (MARK_TONES[metric] || ['same', 'same'])[n > 0 ? 0 : 1]);
const signedDelta = (n, unit = '') => `${n > 0 ? '+' : n < 0 ? '−' : '±'}${Math.abs(n)}${unit}`;
const marks = {log: [], seen: 0};
function recordMarks(events, now = Date.now() / 1000) {
  for (const e of events) {
    if (MARK_TONES[e.metric] && e.delta) marks.log.push({metric: e.metric, delta: e.delta, label: e.title, group: e.group, t: now});
  }
  marks.log = marks.log.filter((e) => now - e.t <= MARK_HISTORY).slice(-MARK_KEEP);
}
// One circle at rest: a badge while its last run of changes (each within two minutes of the
// one before) is under two minutes old, fading over the last thirty seconds; then a dot.
// Nothing from before you last looked counts.
function markOf(log, metric, seen, now, wanted = () => true) {
  const events = log.filter((e) => e.metric === metric && e.t > seen && e.t <= now && wanted(e));
  if (!events.length) return null;
  const age = now - events.at(-1).t;
  if (age < MARK_BADGE) {
    let start = events.length - 1;
    while (start > 0 && events[start].t - events[start - 1].t <= MARK_BADGE) start -= 1;
    const n = events.slice(start).reduce((sum, e) => sum + e.delta, 0);
    const opacity = age < MARK_BADGE - MARK_FADE ? 1 : Math.max(0, (MARK_BADGE - age) / MARK_FADE);
    return {kind: 'badge', n, tone: markTone(metric, n), text: signedDelta(n, markUnit(metric)), opacity};
  }
  const n = events.reduce((sum, e) => sum + e.delta, 0);
  return {kind: 'dot', n, tone: markTone(metric, n)};
}
const clockOf = (t) => new Date(t * 1000).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
// "2 added, 1 done since 3:42", then the last three with their times.
function markHistory(log, metric, now) {
  const recent = log.filter((e) => e.metric === metric && e.t <= now && now - e.t <= MARK_HISTORY);
  if (!recent.length || !MARK_WORDS[metric]) return null;
  const up = recent.filter((e) => e.delta > 0).reduce((n, e) => n + e.delta, 0);
  const down = recent.filter((e) => e.delta < 0).reduce((n, e) => n - e.delta, 0);
  const [rise, fall] = MARK_WORDS[metric], unit = markUnit(metric);
  const words = [up && `${up}${unit} ${rise}`, down && `${down}${unit} ${fall}`].filter(Boolean);
  return {summary: `${words.join(', ')} since ${clockOf(recent[0].t)}`,
    list: recent.slice(-3).reverse().map((e) => ({text: signedDelta(e.delta, unit), tone: markTone(metric, e.delta), label: e.label, at: clockOf(e.t)}))};
}
// Marks follow Notify me about: a kind of change you turned off leaves nothing behind either.
function markMap(now = Date.now() / 1000) {
  if (floatOpen) return {};
  const wanted = (e) => Boolean(notify[e.group]);
  return Object.fromEntries(circles.map((id) => [id, markOf(marks.log, id, marks.seen, now, wanted)]).filter(([, m]) => m));
}
// Badges sit beside their circle, away from the screen edge, in a layer that moves with the
// pill. They wait out the Island card, a drag and the spotlight, which would cover them.
function placeMarks(map = markMap()) {
  const layer = $('#marks');
  if (!layer) return;
  const rail = $('#rail'), quiet = floatOpen || notice || pending.length || drag || spot.on || document.body.dataset.mode !== 'pill';
  if (layer.dataset.side !== spotSide(dock)) layer.dataset.side = spotSide(dock);
  // Where each circle rests: its place in the rail, plus the pill's border and padding. The
  // rail is centred in the pill, so while the pill springs, its own offset says nothing yet.
  // Nothing here is measured unless a badge shows, so opening the panel pays nothing for it.
  const pad = pillPad(feel.size), border = 1;
  const shown = new Set();
  if (!quiet) for (const row of document.querySelectorAll('#rail .f-row[data-metric]')) {
    const id = row.dataset.metric, m = map[id];
    if (m?.kind !== 'badge') continue;
    shown.add(id);
    const c = row.querySelector('.f-circle');
    let badge = layer.querySelector(`.f-mark[data-metric="${id}"]`);
    if (!badge) {
      badge = document.createElement('span');
      badge.dataset.metric = id;
      layer.append(badge);
      if (!calm()) badge.animate([{opacity: 0, transform: 'scale(.6)'}, {opacity: 1, transform: 'none'}], {duration: 320, easing: spring('--f-soft')});
    }
    badge.className = `f-mark ${m.tone}`;
    badge.textContent = m.text;
    badge.style.setProperty('--x', `${c.offsetLeft - rail.offsetLeft + border + pad + c.offsetWidth / 2}px`);
    badge.style.setProperty('--y', `${c.offsetTop - rail.offsetTop + border + pad + c.offsetHeight / 2}px`);
    badge.style.opacity = calm() ? (m.opacity > 0 ? 1 : 0) : m.opacity.toFixed(3);
  }
  layer.querySelectorAll('.f-mark').forEach((badge) => {if (!shown.has(badge.dataset.metric)) badge.remove();});
  // A badge fades and then becomes a dot, so keep ticking while one is up.
  clearTimeout(placeMarks.timer);
  if (Object.values(map).some((m) => m.kind === 'badge')) placeMarks.timer = setTimeout(tickMarks, 1000);
}
function tickMarks() {
  const map = markMap();
  setHTML($('#rail'), pillHTML(attention(), liveHealthy || !snapshotSignature, map));
  if (spot.on) markSpot();
  placeMarks(map);
}
// What hovering a circle says: its label, and what changed lately.
// The agents circle also says who is running, most recently started first, five at most; with
// none, the last one to finish. Its change history follows under a hairline.
const SPOT_AGENTS = 5;
const runFor = (s) => {const m = Math.max(1, Math.floor(s / 60)); return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`;};
function runningHTML(now = Date.now() / 1000) {
  const agents = state.sessions.map((s) => agentOf(s, model.items));
  const running = agents.filter((a) => a.state === 'active').sort((a, b) => (b.since || 0) - (a.since || 0));
  if (!running.length) {
    const last = agents.filter((a) => a.last).sort((a, b) => b.last - a.last)[0];
    return last ? `<span class="f-spot-sum">${esc(last.name)} finished ${ago(last.last)}</span>` : '';
  }
  // The name takes the room; the right side stays short ("Claude · 12m", "↻" in the background),
  // with the full words for VoiceOver.
  const row = (a) => {
    const time = a.since ? runFor(now - a.since) : '', tool = a.harness.replace(/ (Code|Desktop)$/, '');
    const said = `${a.name}, ${a.harness}, ${a.background ? 'in the background' : 'running'}${time ? ` ${time}` : ''}`;
    return `<span class="f-spot-agent" role="listitem" aria-label="${esc(said)}"><span class="dot active" aria-hidden="true"></span><span class="n" aria-hidden="true">${esc(a.name)}</span><small aria-hidden="true">${esc(tool)}${time || a.background ? ` · ${a.background ? '↻ ' : ''}${time}` : ''}</small></span>`;
  };
  return running.slice(0, SPOT_AGENTS).map(row).join('') + (running.length > SPOT_AGENTS ? `<span class="f-spot-more">+${running.length - SPOT_AGENTS} more</span>` : '');
}
function spotHTML(id) {
  const none = id === 'agents' && !model.agents.some((a) => agentState(a) === 'active');
  const tag = esc(none ? 'No agents running' : METRICS[id].read(attention()).tag), changed = markHistory(marks.log, id, Date.now() / 1000);
  const extra = id === 'agents' ? runningHTML() : '';
  const history = changed ? `<span class="f-spot-sum">${esc(changed.summary)}</span>${changed.list.map((e) => `<span class="f-spot-row"><em class="${e.tone}">${esc(e.text)}</em><span>${esc(e.label)}</span><small>${esc(e.at)}</small></span>`).join('')}` : '';
  if (!extra && !history) return tag;
  return `<b>${tag}</b>${extra}${extra && history ? '<span class="f-spot-rule" aria-hidden="true"></span>' : ''}${history}`;
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
    if (listView === 'agents' && heldSheet && $('#main .held-sheet')) return {width: chosen.width, height: chosen.height};
    const above = [...($('#main')?.children || [])].filter((el) => !el.matches('.f-rows, .g-pop, .held-scrim, .held-sheet, .u-toast, .u-veil')).reduce((n, el) => n + el.offsetHeight, 0);
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
  // The rect is local to its display, so two displays of one size share it: which display
  // it is belongs in the signature too, or moving to a twin monitor would never be sent.
  const message = {type: 'place', ...rect, ...flags}, signature = JSON.stringify([message, display?.x, display?.y]);
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
  if ($('#marks')) setRect($('#marks'), pill);
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
  placeMarks();
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
  if (usageView.timer) clearTimeout(usageView.timer);
  usageView.toast = usageView.undo = null;
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
  const area = display?.visible || {x: 0, y: 0, width: innerWidth, height: innerHeight};
  // Beside the pill and never over it; a tall label slides along that side to stay on screen.
  let x = side === 'right' ? p.right + 6 : side === 'left' ? p.left - 6 - w : c.left + c.width / 2 - w / 2;
  let y = side === 'above' ? p.top - 6 - h : side === 'below' ? p.bottom + 6 : c.top + c.height / 2 - h / 2;
  if (side === 'left' || side === 'right') y = clamp(y, area.y + 4, area.y + area.height - 4 - h);
  else x = clamp(x, area.x + 4, area.x + area.width - 4 - w);
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
    label.innerHTML = spotHTML(rows[index].dataset.metric);
    el.classList.toggle('log', label.children.length > 0);
    label.querySelectorAll('.f-spot-agent').forEach((row) => {const n = row.querySelector('.n'); row.classList.toggle('two', n.scrollWidth > n.clientWidth + 0.5);});
    el.classList.toggle('instant', first);
    if (!first) label.animate([{opacity: 0, transform: 'translateY(5px)'}, {opacity: 1, transform: 'none'}], {duration: 240, easing: 'ease-out'});
  }
  spot.on = true;
  el.classList.toggle('on', spotLabelled(floatOpen));
  markSpot();
  placeSpot();
  if (first) {
    requestAnimationFrame(() => el.classList.remove('instant'));
    placeMarks();
  }
}
function hideSpot() {
  if (!spot.on) return;
  spot.on = false;
  spot.index = -1;
  $('#spot').classList.remove('on');
  markSpot();
  placeMarks();
}

// Tooltips. macOS draws a page's native title tooltips in a window of its own, below the
// panel's, so they would show behind it; the panel draws its own. A title becomes data-tip the
// moment the pointer reaches it, before the native one could appear. Two spaces separate the
// words from a key hint: "Fold all groups  ⌥←".
const TIP_DELAY = 400, TIP_WARM = 900;
const tip = {target: null, timer: null, shown: false, hiddenAt: -Infinity};
function tipParts(text) {
  const [label, key = ''] = String(text || '').split(/\s{2,}|\t/);
  return {label: label.trim(), key: key.trim()};
}
function tipTarget(el) {
  const target = el?.closest?.('[data-tip], [title]');
  if (!target || target.closest('#pill, #tip')) return null;
  if (target.hasAttribute('title')) {
    const text = target.getAttribute('title');
    target.removeAttribute('title');
    target.dataset.tip = text;
    // A control named only by its tooltip keeps that name for VoiceOver.
    if (!target.hasAttribute('aria-label') && !target.textContent.trim()) target.setAttribute('aria-label', tipParts(text).label);
  }
  return target.dataset.tip ? target : null;
}
// Below the control; above it when there is no room below; beside it when neither fits; always
// inside the visible area, which the panel's window covers, so it may reach past the box.
function placeTip(r, size, area, gap = 6) {
  const inside = (y) => y >= area.y + 4 && y + size.height <= area.y + area.height - 4;
  let x = r.left + r.width / 2 - size.width / 2, y = r.bottom + gap;
  if (!inside(y)) y = r.top - gap - size.height;
  if (!inside(y)) {
    y = r.top + r.height / 2 - size.height / 2;
    x = r.right + gap + size.width <= area.x + area.width - 4 ? r.right + gap : r.left - gap - size.width;
  }
  return {x: Math.round(clamp(x, area.x + 4, area.x + area.width - 4 - size.width)), y: Math.round(clamp(y, area.y + 4, area.y + area.height - 4 - size.height))};
}
function tipHover(el) {
  const target = tipTarget(el);
  if (target === tip.target) return;
  hideTip();
  if (!target) return;
  tip.target = target;
  // macOS-style: once one has shown, the next comes at once.
  tip.timer = setTimeout(showTip, performance.now() - tip.hiddenAt < TIP_WARM ? 0 : TIP_DELAY);
}
function showTip() {
  const target = tip.target, el = $('#tip');
  if (!el || !target?.isConnected || drag) return hideTip();
  const {label, key} = tipParts(target.dataset.tip);
  el.innerHTML = `<span>${esc(label)}</span>${key ? `<kbd>${esc(key)}</kbd>` : ''}`;
  el.hidden = false;
  const area = display?.visible || {x: 0, y: 0, width: innerWidth, height: innerHeight};
  const at = placeTip(target.getBoundingClientRect(), {width: el.offsetWidth, height: el.offsetHeight}, area);
  el.style.transform = `translate(${at.x}px, ${at.y}px)`;
  el.classList.add('on');
  tip.shown = true;
}
function hideTip() {
  clearTimeout(tip.timer);
  if (tip.shown) tip.hiddenAt = performance.now();
  tip.shown = false;
  tip.target = null;
  const el = $('#tip');
  if (el && !el.hidden) {el.classList.remove('on'); el.hidden = true;}
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
// The window covers its whole display from the display's corner, so the page's own coordinates
// are the display's. Not screenX: WebKit flips it against the window's screen rather than the
// main one, which put the ghost off an external display.
const pointer = (e) => ({x: e.clientX, y: e.clientY});
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

// The cursor moved to another display: the pill appears in its place there with a short fade,
// rather than springing across from where it sat on the last one.
const ARRIVE_MS = 180;
function arrive() {
  const pill = $('#pill'), box = $('#box');
  pill.classList.add('instant');
  box.classList.add('instant');
  layout();
  pill.getBoundingClientRect();
  if (!calm()) pill.animate([{opacity: 0, scale: '.97'}, {opacity: 1, scale: '1'}], {duration: ARRIVE_MS, easing: 'ease-out'});
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
    const arriving = message.changed && display && !drag;
    display = message;
    if (drag?.phase === 'dragging') coverDisplay();
    else if (arriving) arrive();
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
    const at = recording?.action === message.action ? recording : {action: message.action, index: null};
    recording = null;
    if (message.cleared && Number.isInteger(at.index)) {shortcuts = dropKey(shortcuts, at.action, at.index); saveSettings();}
    else if (message.key) recordedKey('global', at, message.key);
    // Recording stood every shortcut down; this puts them back, with whatever changed.
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
    if (!message.on) {hideSpot(); hideTip(); sendCursor('default');}
  } else if (message.type === 'pointer') {
    if (hovering) spotlight(message.x, message.y);
    cursorAt(message.x, message.y);
    tipHover(document.elementFromPoint(message.x, message.y));
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
  setInterval(tickUsage, 1000);
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
    if (e.target.closest('#main') && (heldClick(e) || usageClick(e))) return;
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
      if (target === 'usage' && USAGE_TOOLS[metric]) {
        if (floatOpen && listView === 'usage' && usageView.tool !== metric) {usageView.tool = metric; usageView.flow = null; saveTab(); return render();}
        tabState.tabs.usage.tool = metric;
      }
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
      recording = {action: record.dataset.record, index: record.dataset.index === 'new' || record.dataset.index === undefined ? null : Number(record.dataset.index)};
      keyConflict = null;
      native({type: 'recordKey', action: recording.action});
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
    else if (e.target.closest('[data-record-panel]')) {
      const el = e.target.closest('[data-record-panel]');
      recordingPanel = {action: el.dataset.recordPanel, index: el.dataset.index === 'new' ? null : Number(el.dataset.index)};
      keyConflict = null;
      render();
    } else if (e.target.closest('[data-drop-key]')) {
      const el = e.target.closest('[data-drop-key]');
      dropChip(el.dataset.dropKey, el.dataset.action, Number(el.dataset.index));
    } else if (e.target.closest('[data-reset-key]')) {
      const el = e.target.closest('[data-reset-key]');
      if (el.dataset.resetKey === 'global') {shortcuts = resetKeys(shortcuts, el.dataset.action, SHORTCUTS, sameHotkey); applyShortcuts();}
      else panelKeys = resetKeys(panelKeys, el.dataset.action, PANEL_KEYS);
      keyConflict = null;
      saveSettings();
      render();
    } else if (e.target.closest('[data-move-key]')) moveKey();
    else if (e.target.closest('[data-move-cancel]')) {keyConflict = null; render();}
    else if (e.target.closest('[data-agents-all]')) {agentsAll = true; render();}
    else if (e.target.closest('[data-agent-dashboard]')) openDashboard(href({page: 'agent', agent: e.target.closest('[data-agent-dashboard]').dataset.agentDashboard, lane: 'you', item: null}));
    else if (row) openItem(row.dataset.item);
    else if (inner) {
      e.preventDefault();
      openDashboard(inner.dataset.href || inner.getAttribute('href'));
    }
  }, true);
  document.addEventListener('keydown', (e) => {
    // Delete with a key chip focused removes that key.
    if (customizing && !recording && !recordingPanel && ['Backspace', 'Delete'].includes(e.key)) {
      const chip = e.target.closest?.('.f-cap[data-index]:not([data-index="new"])');
      if (chip) {
        e.preventDefault();
        e.stopImmediatePropagation();
        dropChip(chip.dataset.record ? 'global' : 'panel', chip.dataset.record || chip.dataset.recordPanel, Number(chip.dataset.index));
        return;
      }
    }
    if (e.key !== 'Escape' || recordingPanel) return;
    if (drag) cancelDrag();
    else if (customizing && keyConflict) {keyConflict = null; render();}
    else if (customizing) {customizing = false; render();}
    else if (floatOpen && !route.item && listView === 'agents' && heldEscape()) {/* the note or the sheet took it */}
    else if (floatOpen && !route.item && listView === 'usage' && usageEscape()) {/* the notice or a step took it */}
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
  document.addEventListener('mousemove', (e) => {delete document.body.dataset.keys; cursorAt(e.clientX, e.clientY); tipHover(e.target);});
  // A tooltip goes as soon as you act or move the content under it.
  for (const type of ['pointerdown', 'keydown', 'scroll', 'wheel']) document.addEventListener(type, hideTip, true);
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
  document.addEventListener('input', (e) => {heldInput(e); layoutSoon();});
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
