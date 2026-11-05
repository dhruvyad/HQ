// The floating panel: a pill that opens into what needs you, then expands to decide.
// app.js owns the data, the item pane and its actions. FloatPanel.swift only moves the
// window, so every rule lives here where Node can test it.
const native = (message) => window.webkit?.messageHandlers?.hq?.postMessage(message);

// Every open design decision, so `hq float lab` can flip each one live (float-lab.js).
const feel = {pill: 'count', size: 32, targets: 'all', ghost: 'pill', backdrop: 'dim', glide: 300, threshold: 5, order: 'lead', away: 'collapse', after: 'next', arrival: 'pulse', width: 480, show: 100000};

// Where the pill can live: [across, down], each the start, center or end of the free screen.
const DOCKS = {
  'top-left': ['start', 'start'], top: ['center', 'start'], 'top-right': ['end', 'start'],
  left: ['start', 'center'], right: ['end', 'center'],
  'bottom-left': ['start', 'end'], bottom: ['center', 'end'], 'bottom-right': ['end', 'end'],
};
const TARGETS = {all: Object.keys(DOCKS), flow: ['top-left', 'left', 'right', 'bottom'], corners: ['top-left', 'top-right', 'bottom-left', 'bottom-right']};
const DEFAULT_DOCK = 'top-left';
const EDGE = 10;
// A maximized window keeps its close, minimize and zoom buttons in this corner.
const TRAFFIC_LIGHTS = 74;
const LIST_WIDTH = 380, LIST_MAX = 520, ITEM_MIN = 240, ITEM_MAX = 760;
const PEEK_FOR = 5000, BUMP_FOR = 700, CONTROLS = 'button, a, input, textarea, select, summary';

let floatOpen = false, dock = DEFAULT_DOCK, display = null, drag = null, placed = '';
let known = null, peek = null, swallowClick = false;

// Geometry, in display-local points with the origin at the display's top-left.
function dockRect(requested, size, area) {
  const name = DOCKS[requested] ? requested : DEFAULT_DOCK;
  const [across, down] = DOCKS[name];
  const width = Math.min(size.width, area.width - 2 * EDGE), height = Math.min(size.height, area.height - 2 * EDGE);
  const start = area.x + EDGE + (name === 'top-left' ? TRAFFIC_LIGHTS : 0);
  const end = area.x + area.width - width - EDGE;
  const x = across === 'start' ? Math.min(start, end) : across === 'center' ? area.x + (area.width - width) / 2 : end;
  const y = down === 'start' ? area.y + EDGE : down === 'center' ? area.y + (area.height - height) / 2 : area.y + area.height - height - EDGE;
  return {x: Math.round(x), y: Math.round(y), width, height};
}
function nearestDock(x, y, rects) {
  let best = null, closest = Infinity;
  for (const [name, r] of Object.entries(rects)) {
    const dx = r.x + r.width / 2 - x, dy = r.y + r.height / 2 - y, distance = dx * dx + dy * dy;
    if (distance < closest) {closest = distance; best = name;}
  }
  return best;
}
const dockRects = (size, area, names = TARGETS[feel.targets] || TARGETS.all) => Object.fromEntries(names.map((name) => [name, dockRect(name, size, area)]));
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
function arrivals(items, seenIds) {return seenIds ? items.filter((i) => !seenIds.has(i.id)) : [];}
const surfaceMode = (open, item) => (open ? (item ? 'item' : 'list') : 'pill');
// Where to go after acting on an item: null is the list, undefined means stay.
function afterAct(after, next, replied) {
  if (after === 'stay' && replied) return undefined;
  return after === 'list' ? null : next;
}

function pillHTML(items, live = liveHealthy) {
  const count = items.length;
  const label = esc(peek || items[0]?.title || '');
  const mark = '<span class="wordmark">HQ</span>';
  const badge = count ? `<span class="badge">${count}</span>` : '';
  const off = live ? '' : '<span class="dot err"></span>';
  if (peek) return `${off}${badge || mark}<span class="f-peek">${label}</span>`;
  if (feel.pill === 'number') return `${off}${badge || mark}`;
  if (feel.pill === 'title' && count) return `${off}${badge}<span class="f-peek">${label}</span>`;
  return `${off}${mark}${badge}`;
}
function pillLabel(items, live = liveHealthy) {
  const n = items.length;
  const needs = n ? `${n} ${n === 1 ? 'item needs' : 'items need'} you` : 'Nothing needs you';
  return `HQ. ${needs}.${live ? '' : ' Reconnecting.'}`;
}
function floatListHTML(items, live = liveHealthy) {
  const head = `<header class="f-head"><h1>Needs you</h1>${items.length ? `<span class="f-count">${items.length}</span>` : ''}${live ? '' : '<span class="f-off"><span class="dot err"></span>Reconnecting…</span>'}
      <a class="icon-btn" href="/" target="_blank" rel="noreferrer" aria-label="Open the dashboard" title="Open the dashboard">${icon('out')}</a>
      <button class="icon-btn" type="button" data-collapse aria-label="Collapse" title="Collapse">${icon('x')}</button></header>`;
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
  noteArrivals(items);
  const scrolls = ['.f-rows', '.d-body'].map((sel) => [sel, document.querySelector(sel)?.scrollTop]);
  const pill = $('#pill');
  setHTML(pill, pillHTML(items, live));
  pill.setAttribute('aria-label', pillLabel(items, live));
  pill.title = 'Click to open. Drag to move.';
  setHTML($('#main'), floatListHTML(items, live));
  const detail = $('#detail'), active = document.activeElement;
  // Typing in the item pane wins over a live refresh; drafts are kept either way.
  if (!(background && detail.contains(active) && active.matches('textarea, input, select'))) renderDetail();
  scrolls.forEach(([sel, top]) => top && document.querySelector(sel) && (document.querySelector(sel).scrollTop = top));
  document.body.dataset.mode = surfaceMode(floatOpen, route.item);
  document.documentElement.style.setProperty('--f-width', `${route.item ? feel.width : LIST_WIDTH}px`);
  if (route.item && !detail.hidden) bindDetail(detail);
  layout();
}
function noteArrivals(items) {
  if (!snapshotSignature) return;
  const fresh = arrivals(items, known);
  known = new Set(items.map((i) => i.id));
  if (!fresh.length || floatOpen) return;
  if (feel.arrival === 'pulse') {
    $('#pill').classList.add('bump');
    setTimeout(() => $('#pill').classList.remove('bump'), BUMP_FOR);
  } else if (feel.arrival === 'title') {
    peek = fresh[0].title;
    clearTimeout(noteArrivals.timer);
    noteArrivals.timer = setTimeout(() => {peek = null; render();}, PEEK_FOR);
  }
}

// Window
function pillSize() {
  const r = $('#pill').getBoundingClientRect();
  return {width: Math.ceil(r.width), height: Math.ceil(r.height)};
}
function surfaceSize() {
  const mode = document.body.dataset.mode;
  if (mode === 'pill') return pillSize();
  if (mode === 'list') {
    const head = $('.f-head')?.offsetHeight || 0, rows = $('.f-rows-in')?.offsetHeight || 0;
    return {width: LIST_WIDTH, height: Math.min(LIST_MAX, head + rows + 2)};
  }
  const last = $('.d-body')?.lastElementChild;
  const natural = last ? last.offsetTop + last.offsetHeight + 28 : ITEM_MIN;
  return {width: feel.width, height: Math.max(ITEM_MIN, Math.min(ITEM_MAX, natural + 2))};
}
function placeWindow(rect, flags) {
  const message = {type: 'place', ...rect, ...flags}, signature = JSON.stringify(message);
  if (signature === placed) return;
  placed = signature;
  native(message);
}
function layout() {
  if (drag || !display || (!snapshotSignature && !layout.ready)) return;
  document.documentElement.style.setProperty('--f-origin', growFrom(dock));
  placeWindow(dockRect(dock, surfaceSize(), display.visible), {key: document.body.dataset.mode !== 'pill', shadow: true});
}
function layoutSoon() {
  if (layoutSoon.pending) return;
  layoutSoon.pending = true;
  requestAnimationFrame(() => {layoutSoon.pending = false; layout();});
}
function setOpen(open) {
  floatOpen = open;
  render();
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
  drag = {phase: 'dragging', size: pillSize(), target: dock, rects: {}};
  swallowClick = true;
  document.body.dataset.drag = 'on';
  $('#drag').hidden = false;
  const ghost = $('#ghost');
  ghost.className = `f-ghost ${feel.ghost === 'outline' ? 'outline' : ''}`;
  ghost.innerHTML = $('#pill').innerHTML;
  native({type: 'drag', on: true});
  document.addEventListener('mousemove', dragMoved);
  document.addEventListener('mouseup', dragDropped);
  coverDisplay();
  dragMoved(e);
}
// Also runs when the cursor reaches another display, so the targets move with it.
function coverDisplay() {
  if (!display || !drag) return;
  placeWindow({x: 0, y: 0, width: display.width, height: display.height}, {key: true, shadow: false});
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
  moveGhost(at.x - drag.size.width / 2, at.y - drag.size.height / 2);
  const target = nearestDock(at.x, at.y, drag.rects);
  if (target && target !== drag.target) {
    drag.target = target;
    markTarget();
  }
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
  layout();
}

// From the native window
function receive(message) {
  if (message.type === 'start') {
    if (DOCKS[message.dock]) dock = message.dock;
    layout();
  } else if (message.type === 'display') {
    display = message;
    if (drag?.phase === 'dragging') coverDisplay();
    else layout();
  } else if (message.type === 'cancelDrag') endDrag();
  else if (message.type === 'saved') saved(message.dock, message.ok);
  else if (message.type === 'resigned') {
    if (floatOpen && !route.item && !drag && feel.away === 'collapse') setOpen(false);
  } else if (message.type === 'hover') {
    // A panel that is not key never gets :hover from WebKit, so the window reports it.
    document.body.toggleAttribute('data-hover', message.on);
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
  document.body.dataset.backdrop = feel.backdrop;
}
// Links in the item pane point at the dashboard's routes, which live in the browser.
function openDashboard(hash) {window.open(`/${hash}`, '_blank', 'noreferrer');}

function bootFloat() {
  paint = paintFloat;
  moveOn = (next, replied) => {
    const target = afterAct(feel.after, next, replied);
    if (target !== undefined) go({item: target});
  };
  window.hqFloat = {receive};
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
    if (e.target.closest('#pill')) setOpen(true);
    else if (e.target.closest('[data-collapse]')) setOpen(false);
    else if (row) openItem(row.dataset.item);
    else if (inner) {
      e.preventDefault();
      openDashboard(inner.dataset.href || inner.getAttribute('href'));
    }
  }, true);
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (drag) endDrag();
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
  // The window hugs its content, so anything that changes its size has to re-place it.
  new ResizeObserver(layoutSoon).observe($('#pill'));
  new MutationObserver(layoutSoon).observe($('#box'), {childList: true, subtree: true, attributes: true, attributeFilter: ['style', 'open', 'hidden']});
  document.addEventListener('input', layoutSoon);
  document.addEventListener('toggle', layoutSoon, true);
  document.fonts?.ready.then(layoutSoon);
  window.addEventListener('resize', layoutSoon);
  setTimeout(() => {layout.ready = true; layout();}, 1500);
  boot();
  native({type: 'ready'});
}
bootFloat();
