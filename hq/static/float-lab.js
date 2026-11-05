// The design lab for the floating panel, after NoClick's DashboardLab: a bench of presets
// and axes, with the real native panel as the stage. Flip an axis and the real window
// changes now, so drag, focus and always-on-top are never simulated. Every pick and note
// goes back as one batch. To run a new round, edit AXES and the matching `feel` in float.js.
const AXES = [
  {id: 'pill', ask: 'What should the pill show?', options: [
    {value: 'count', label: 'HQ and a count', why: 'Compact. You open it to see what is waiting.', default: true},
    {value: 'title', label: 'Count and latest title', why: 'Says what is new without a click, but takes up more of the screen.'},
    {value: 'number', label: 'Just the count', why: 'Smallest, and easiest to overlook.'},
  ]},
  {id: 'size', ask: 'How tall is the pill?', options: [
    {value: 28, label: '28', why: 'Fits inside a title bar.'},
    {value: 32, label: '32', why: 'Easy to grab, still quiet.', default: true},
    {value: 36, label: '36', why: 'Easiest target, most present.'},
  ]},
  {id: 'targets', ask: 'Where can you drop it?', options: [
    {value: 'all', label: 'Eight places', why: 'Corners, edge centers, top and bottom.', default: true},
    {value: 'flow', label: 'OpenFlow’s, plus top left', why: 'Bottom, left and right, plus the top left you asked for.'},
    {value: 'corners', label: 'Corners only', why: 'Fewest choices, and never over the middle of a window.'},
  ]},
  {id: 'ghost', ask: 'What follows the cursor while dragging?', options: [
    {value: 'pill', label: 'The pill', why: 'Feels like picking it up.', default: true},
    {value: 'outline', label: 'An outline', why: 'Lets you see what is under it.'},
  ]},
  {id: 'backdrop', ask: 'Dim the screen while dragging?', options: [
    {value: 'dim', label: 'Dim', why: 'As OpenFlow does. Targets stand out on any wallpaper.', default: true},
    {value: 'none', label: 'No dim', why: 'Less of an event for a small move.'},
  ]},
  {id: 'glide', ask: 'How long does it glide into place?', options: [
    {value: 180, label: '180 ms', why: 'Snappy.'},
    {value: 300, label: '300 ms', why: 'OpenFlow’s timing.', default: true},
    {value: 450, label: '450 ms', why: 'Deliberate, and starts to feel slow.'},
  ]},
  {id: 'threshold', ask: 'How far before a click becomes a drag?', options: [
    {value: 3, label: '3 px', why: 'Drag starts instantly; a shaky click may move it.'},
    {value: 5, label: '5 px', why: 'OpenFlow’s threshold.', default: true},
    {value: 8, label: '8 px', why: 'Safest for clicks; drag starts a beat late.'},
  ]},
  {id: 'order', ask: 'What order is the list in?', options: [
    {value: 'lead', label: 'Pinned and due first, then newest', why: 'Latest, without burying what is urgent.', default: true},
    {value: 'newest', label: 'Newest only', why: 'A plain feed. Pinned and due items can sink.'},
    {value: 'urgent', label: 'Most urgent', why: 'The dashboard’s order, by the rank agents assign.'},
  ]},
  {id: 'away', ask: 'What happens when you click somewhere else?', options: [
    {value: 'collapse', label: 'The list closes', why: 'Behaves like a menu. An open item always stays.', default: true},
    {value: 'stay', label: 'The list stays', why: 'Stays open until you close it, and stays over your work.'},
  ]},
  {id: 'after', ask: 'After you answer an item?', options: [
    {value: 'next', label: 'Next item', why: 'Fastest through a pile. Delivery shows only as a toast.', default: true},
    {value: 'list', label: 'Back to the list', why: 'You choose what to do next.'},
    {value: 'stay', label: 'Stay on it', why: 'Shows the delivery state, as the dashboard does.'},
  ]},
  {id: 'arrival', ask: 'When something new arrives?', options: [
    {value: 'pulse', label: 'The count pulses', why: 'Noticeable without words.', default: true},
    {value: 'title', label: 'Show its title briefly', why: 'Tells you what it is, for five seconds.'},
    {value: 'count', label: 'Count only', why: 'Quietest, and easiest to miss.'},
  ]},
  {id: 'width', ask: 'How wide is the expanded item?', options: [
    {value: 420, label: '420', why: 'Covers less of your screen.'},
    {value: 480, label: '480', why: 'Close to the dashboard’s 500 px pane.', default: true},
    {value: 560, label: '560', why: 'Longer lines; fewer scrolls.'},
  ]},
  // Sample data, not a decision, so it stays out of the picks.
  {id: 'show', fixture: true, ask: 'Try it with how many items?', options: [
    {value: 100000, label: 'Many', why: 'A long list that scrolls.', default: true},
    {value: 3, label: 'A few', why: 'A short list that hugs its rows.'},
    {value: 0, label: 'None', why: 'The empty state.'},
  ]},
];
const LAB_SESSION = 'lab:fixtures'; // floating.py seeds it; the lab has no other conversation.
const PRESETS = [
  {name: 'Recommended', why: 'Each axis at its starting point.', set: {}},
  {name: 'OpenFlow feel', why: 'Same drag as the flow bar, with the top left added.', set: {targets: 'flow', ghost: 'pill', backdrop: 'dim', glide: 300, threshold: 5}},
  {name: 'Quiet', why: 'Least on your screen, least motion.', set: {pill: 'number', size: 28, targets: 'corners', backdrop: 'none', glide: 180, arrival: 'count'}},
  {name: 'Fast triage', why: 'Tells you what is new and keeps you moving.', set: {pill: 'title', order: 'newest', after: 'next', arrival: 'title', width: 420}},
];

const defaults = () => Object.fromEntries(AXES.map((a) => [a.id, a.options.find((o) => o.default).value]));
const presetValues = (preset) => ({...defaults(), ...preset.set});
const optionOf = (axis, value) => axis.options.find((o) => o.value === value);
function picksText(picked, notes, overall) {
  const base = defaults(), lines = ['Floating window lab picks'];
  for (const axis of AXES.filter((a) => !a.fixture)) {
    const choice = optionOf(axis, picked[axis.id]), note = (notes[axis.id] || '').trim();
    lines.push(`- ${axis.ask} ${choice.label}${picked[axis.id] === base[axis.id] ? '' : ' (changed)'}${note ? `. Note: ${note}` : ''}`);
  }
  if (overall.trim()) lines.push('', `Overall: ${overall.trim()}`);
  return lines.join('\n');
}

function bootLab() {
  const native = (message) => window.webkit?.messageHandlers?.hq?.postMessage(message);
  const token = document.querySelector('meta[name="hq-token"]').content;
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'})[c]);
  const api = (path, body) => fetch(`/api/${path}`, {method: 'POST', headers: {'X-HQ-Token': token, 'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const picked = defaults(), notes = {};
  let overall = '';
  const send = (id) => native({type: 'feel', key: id, value: picked[id]});
  const changed = () => AXES.filter((a) => !a.fixture && picked[a.id] !== defaults()[a.id]).length;
  function paint() {
    const live = document.activeElement;
    const focus = live?.dataset?.note, caret = live?.selectionStart;
    document.getElementById('lab').innerHTML = `<h1>Floating window lab</h1>
      <p class="lab-sub">Flip anything here and the real window changes now. Drag it, click it, answer things. This is sample data, so nothing reaches a real agent.</p>
      <div class="lab-group">Presets</div>
      <div class="lab-presets">${PRESETS.map((p, n) => `<button class="lab-preset" data-preset="${n}" aria-pressed="${AXES.every((a) => a.fixture || presetValues(p)[a.id] === picked[a.id])}"><strong>${esc(p.name)}</strong><span>${esc(p.why)}</span></button>`).join('')}</div>
      <div class="lab-group">One at a time</div>
      ${AXES.map((a) => `<section class="lab-axis"><div class="lab-q">${esc(a.ask)}</div>
        <div class="lab-seg" role="radiogroup" aria-label="${esc(a.ask)}">${a.options.map((o) => `<button role="radio" data-axis="${a.id}" data-value="${esc(JSON.stringify(o.value))}" aria-checked="${o.value === picked[a.id]}">${esc(o.label)}</button>`).join('')}</div>
        <p class="lab-why">${esc(optionOf(a, picked[a.id]).why)}</p>
        ${a.fixture ? '' : `<input class="field lab-note" data-note="${a.id}" placeholder="Note (optional)" aria-label="Note on: ${esc(a.ask)}" value="${esc(notes[a.id] || '')}">`}</section>`).join('')}
      <div class="lab-group">Try an arrival</div>
      <button class="pill outline sm lab-arrive" id="lab-arrive" type="button">Add a new item</button>
      <div class="lab-group">Anything else</div>
      <textarea class="field lab-all" id="lab-all" placeholder="Overall notes for this round">${esc(overall)}</textarea>`;
    document.getElementById('lab-count').textContent = changed() ? `${changed()} changed from the starting point` : 'All at the starting point';
    if (focus) {
      const el = document.querySelector(`[data-note="${focus}"]`);
      el?.focus();
      el?.setSelectionRange(caret, caret);
    }
  }
  document.addEventListener('click', async (e) => {
    const option = e.target.closest('[data-axis]'), preset = e.target.closest('[data-preset]');
    if (option) {
      picked[option.dataset.axis] = JSON.parse(option.dataset.value);
      send(option.dataset.axis);
      paint();
    } else if (preset) {
      const values = presetValues(PRESETS[Number(preset.dataset.preset)]);
      for (const a of AXES.filter((x) => !x.fixture)) {
        picked[a.id] = values[a.id];
        send(a.id);
      }
      paint();
    } else if (e.target.closest('#lab-arrive')) {
      await api('items', {session_id: LAB_SESSION, title: `New decision at ${new Date().toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'})}`, kind: 'question', owner: 'human', options: ['Yes', 'No']});
    } else if (e.target.closest('#lab-send')) {
      native({type: 'picks', text: picksText(picked, notes, overall)});
    }
  });
  document.addEventListener('input', (e) => {
    if (e.target.dataset?.note) notes[e.target.dataset.note] = e.target.value;
    if (e.target.id === 'lab-all') overall = e.target.value;
  });
  paint();
  for (const a of AXES) send(a.id);
}
bootLab();
