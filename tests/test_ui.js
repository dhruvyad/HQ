// Pure rendering, state and draft checks for the dashboard; no browser or page execution.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const elements = {'meta[name="hq-token"]': {content: 'test'}};
const context = vm.createContext({URL, URLSearchParams, document: {
  body: {dataset: {}},
  querySelector(selector) {return elements[selector] || null;},
  querySelectorAll() {return [];},
}});
const source = fs.readFileSync('hq/static/app.js', 'utf8');
vm.runInContext(source.slice(0, source.indexOf('function boot()')), context);
const run = (code) => vm.runInContext(code, context);
const now = Date.now() / 1000;
const item = (fields) => ({status: 'open', owner: 'human', kind: 'todo', bucket: 'needs', tags: [], options: [], steps: [], paste: [],
  comments: [], detail: '', created: now - 7200, updated: now - 7200, rank: 9999, project: 'noclick', project_name: 'NoClick',
  workstream: 'support', workstream_name: 'Support', session_id: 'codex:a', session_name: 'Shared <project>', provider: 'codex', ...fields});

context.queue = {
  instance: 'server-one', revision: 5,
  sessions: [
    {id: 'codex:a', name: 'Shared <project>', provider: 'codex', activity: {state: 'active'}},
    {id: 'codex:b', name: 'Shared <project>', provider: 'codex', activity: {state: 'idle'}},
    {id: 'claude:c', name: 'Shared <project>', provider: 'claude-code'},
    {id: 'claude:empty', name: 'Discovered empty chat', provider: 'claude-code'},
  ],
  items: [
    item({id: 'a1', title: 'Human <b>action</b>', rank: 300}),
    item({id: 'a2', title: 'Agent action', owner: 'agent', bucket: 'pending', workstream: 'billing', workstream_name: 'Billing and usage'}),
    item({id: 'b1', session_id: 'codex:b', title: 'Saved answer', kind: 'question', status: 'answered', bucket: 'waiting', delivery: 'pending', answer: 'Annual'}),
    item({id: 'c1', session_id: 'claude:c', title: 'Outside dependency', owner: 'external', waiting_on: 'Vendor', bucket: 'waiting', kind: 'blocker'}),
    item({id: 'd1', session_id: 'codex:a', title: 'Finished work', status: 'done', bucket: 'done', updated: now - 60}),
    item({id: 'h1', title: 'Restyle the inbox', project: 'hq', project_name: 'HQ', workstream: 'design', workstream_name: 'Design', kind: 'question', rank: 20, created: now - 600}),
    item({id: 'p1', title: 'Pinned one', pinned: true, created: now - 90000}),
    item({id: 'u1', title: 'Due today', due: run('today()'), created: now - 90000}),
    item({id: 'l1', title: 'Snoozed', bucket: 'later', defer_until: '2099-01-01'}),
    item({id: 'q1', title: 'Asked the agent', asking: true, comments: [{id: 'q', author: 'human', text: '<script>bad()</script>', state: 'pending', answered: false}]}),
  ],
};
run('applyState(queue); model = derive()');

// Lanes follow who must act next.
const lane = (id) => run(`model.items.find(i => i.id === '${id}').lane`);
assert.equal(lane('a1'), 'you');
assert.equal(lane('a2'), 'agents');
assert.equal(lane('b1'), 'agents'); // a saved reply waits on the agent
assert.equal(lane('c1'), 'waiting');
assert.equal(lane('d1'), 'done');
assert.equal(lane('l1'), 'later');
assert.equal(lane('q1'), 'agents'); // an open question leaves Needs you until answered

// Needs you: pinned and due-soon lead, then project › workstream groups, most urgent first.
let html = run('needsPage().body');
assert.ok(html.indexOf('Pinned') < html.indexOf('Due soon'));
assert.ok(html.indexOf('Due soon') < html.indexOf('Restyle the inbox'));
assert.ok(html.indexOf('Restyle the inbox') < html.indexOf('Human &lt;b&gt;action&lt;/b&gt;')); // rank 20 before 300
assert.ok(html.includes('<span class="of">HQ</span><span class="of">›</span>Design'));
assert.ok(!html.includes('<b>action</b>'));
assert.ok(!html.includes('Agent action') && !html.includes('Snoozed') && !html.includes('Asked the agent'));
run("sortMode = 'new'");
html = run('needsPage().body');
assert.ok(html.indexOf('Restyle the inbox') < html.indexOf('Pinned one')); // newest first, one feed
assert.ok(html.includes('HQ › Design') && html.includes('10m ago'));
run("sortMode = 'urgent'");

// Workstream pages count each lane for that scope only.
run("route = parseRoute('#/p/noclick/support?lane=waiting')");
const page = run('workstreamPage()');
assert.equal(page.title, 'Support');
assert.ok(page.body.includes('data-lane="you" aria-selected="false">Needs you<span class="n">3</span>'));
assert.ok(page.body.includes('Outside dependency') && !page.body.includes('Human &lt;b&gt;'));
assert.equal(run("href({page: 'workstream', project: 'noclick', workstream: 'support', lane: 'waiting', item: 'c1'})"), '#/p/noclick/support?lane=waiting&item=c1');

// Similar names, harnesses and tags never combine separate conversations.
run("route = parseRoute('#/agents')");
assert.deepEqual(Array.from(run('model.agents.map(a => a.id)')), ['codex:a', 'codex:b', 'claude:c']);
run("route = parseRoute('#/agents/codex%3Ab'); model = derive()");
assert.deepEqual(Array.from(run('scopeItems().map(i => i.id)')), ['b1']);
html = run('agentsPage().body');
assert.ok(html.includes('data-session="codex:a"') && html.includes('Shared &lt;project&gt;'));
assert.ok(html.includes('class="f-name">No folder<') && html.indexOf('f-name') < html.indexOf('data-session="codex:a"')); // grouped by folder
// The selected conversation stays visible when its last open item completes.
context.queue = {...context.queue, revision: 6, items: context.queue.items.map(i => i.session_id === 'codex:b' ? {...i, status: 'done', bucket: 'done'} : i),
  sessions: context.queue.sessions.map(s => s.id === 'codex:b' ? {...s, activity: {state: 'offline'}} : s)};
run('applyState(queue); model = derive()');
assert.ok(Array.from(run('model.agents.map(a => a.id)')).includes('codex:b'));
run("route = parseRoute('#/'); model = derive()");
assert.ok(!Array.from(run('model.agents.map(a => a.id)')).includes('codex:b'));
assert.ok(!Array.from(run('model.agents.map(a => a.id)')).includes('claude:empty'));

// Presence uses harness state, and becomes Unknown while the stream is down.
assert.equal(run("agentState(state.sessions[0])"), 'active');
run('liveHealthy = false');
assert.equal(run("agentState(state.sessions[0])"), 'unknown');
run('liveHealthy = true');

// Older or replayed snapshots never replace newer state.
context.older = {...context.queue, revision: 4, items: []};
run('applyState(older)');
assert.equal(run('state.revision'), 6);
context.restarted = {...context.queue, instance: 'server-two', revision: 1};
run('applyState(restarted)');
run('applyState(queue)');
assert.equal(run('state.instance'), 'server-two');
run('streamEvent(\'event: ping\\ndata: {"healthy":false}\')');
assert.equal(run('liveHealthy'), false);
run('streamEvent(\'event: ping\\ndata: {}\')');
assert.equal(run('liveHealthy'), true);

// The ask thread escapes text, refuses credentialed links and offers cancel only while pending.
run("model = derive()");
html = run("askHTML(model.items.find(i => i.id === 'q1'), {})");
assert.ok(html.includes('&lt;script&gt;') && !html.includes('<script>'));
assert.ok(html.includes('Cancel question') && html.includes('queued for the agent'));
context.answered = {...run("model.items.find(i => i.id === 'q1')"), comments: [
  {id: 'q', author: 'human', text: 'Why?', state: 'received', answered: true},
  {id: 'a', author: 'agent', parent_id: 'q', text: '**Short answer** [bad](https://user:secret@example.com)'}]};
html = run('askHTML(answered, {})');
assert.ok(html.includes('<strong>Short answer</strong>') && !html.includes('href="https://user:secret'));
assert.ok(!html.includes('Cancel question'));

// Readable items escape every agent-written field and refuse unsafe links.
context.readable = item({id: 'r', title: 'Send one message', minutes: 3,
  steps: [{text: 'Open <b>it</b>', link: {label: 'x', url: 'javascript:alert(1)'}}, {text: 'Send', copy: '<img onerror=x>'},
          {text: 'Check', checks: ['<script>a()</script>', 'Second']}, 'Plain step'],
  matters: {lead: '<i>lead</i>', points: ['**fact**']}, needs_you: {lead: 'Only you', points: []}, detail: 'Original paragraph'});
assert.equal(run('isReadable(readable)'), true);
assert.equal(run("isReadable({steps: ['a']})"), false);
const plan = run('planHTML(readable, {})');
assert.ok(!plan.includes('javascript:') && !plan.includes('<script>') && !plan.includes('<img') && !plan.includes('<b>'));
assert.ok(plan.includes('0 of 2') && plan.includes('Plain step'));
assert.equal((plan.match(/data-check=/g) || []).length, 2);
const reasons = run('reasonsHTML(readable)');
assert.ok(reasons.includes('Why it matters') && reasons.includes('Why you, not the agent'));
assert.ok(reasons.includes('<strong>fact</strong>') && !reasons.includes('<i>'));
assert.deepEqual(Array.from(run('checksOf(readable).map(c => c.key)')), ['2:0', '2:1']);

// Drafts survive live refreshes: ticks, replies, questions and move fields.
context.queue = {...context.queue, instance: 'server-two', revision: 9, items: [...context.queue.items, context.readable]};
run("applyState(queue); model = derive(); route = parseRoute('#/?item=r')");
run("setDraft('r', {checks: ['2:0'], question: 'Unsent question', due: '2026-11-02'}); openMove = 'due'");
context.queue = {...context.queue, revision: 10};
run('applyState(queue); model = derive()');
html = run("detailHTML(model.items.find(i => i.id === 'r'))");
assert.ok(html.includes('1 of 2') && html.includes('data-check="2:0" aria-checked="true"'));
assert.ok(html.includes('value="2026-11-02"'));
assert.ok(html.includes('Looks right') && !html.includes('id="reply"')); // the reply box waits for Something was off
// Checks end in a wide Looks right, honest about what is still unticked.
assert.ok(html.includes('data-report="ok"') && html.includes('Looks right (1 of 2 ticked)'));
assert.ok(html.includes('class="ghost-pill" data-report="off">Something was off'));
assert.equal(run('lookedRight(1, 2)'), 'Looked right (1 of 2 ticked)');
assert.equal(run('lookedRight(2, 2)'), 'All 2 looked right');
assert.equal(run('lookedRight(2, 2, true)'), 'Looks right');
// The background and where it came from live in Context.
assert.ok(!html.includes('Original paragraph'));
run("setDraft('r', {tab: 'context'})");
html = run("detailHTML(model.items.find(i => i.id === 'r'))");
assert.ok(html.includes('Original paragraph') && html.includes('Hard to read?'));
assert.ok(html.includes('data-tab="context" aria-selected="true"'));
run("setDraft('r', {tab: 'ask'}); openMove = 'off'");
html = run("detailHTML(model.items.find(i => i.id === 'r'))");
assert.ok(html.includes('Unsent question'));
assert.ok(html.includes("Didn&#39;t see: Second")); // what wasn't ticked, ready to send
// Something was off swaps the row for the box; Escape brings the row back until you change it.
assert.ok(!html.includes('data-report=') && html.includes('placeholder="What was off?"') && html.includes('data-state="off"'));
const offEsc = (typed) => run(`offEscape(model.items.find(i => i.id === 'r'), draftOf('r'), ${JSON.stringify(typed)})`);
assert.equal(offEsc("Didn't see: Second"), true);
assert.equal(offEsc(''), true);
assert.equal(offEsc("Didn't see: Second, and the preview was blank"), false);
run("setDraft('r', {tab: undefined}); openMove = null");
html = run("detailHTML(model.items.find(i => i.id === 'a2'))");
assert.ok(!html.includes('Hard to read?')); // agents' own work isn't sent back to them
// Skip sits with the other not-now choices and reads as its own outcome once taken.
assert.ok(run("movesHTML(model.items.find(i => i.id === 'a1'))").includes('data-move="skip"'));
run("openMove = 'skip'");
assert.ok(run("movesHTML(model.items.find(i => i.id === 'a1'))").includes('tells the agent to drop it'));
run("openMove = null");
context.skipped = item({id: 's1', status: 'done', bucket: 'done', skipped: true, answer: 'Skipped: …', delivery: 'received', updated: now});
context.skipped.lane = 'done';
assert.ok(run('metaHTML(skipped)').startsWith('Skipped'));
html = run('detailHTML(skipped)');
assert.ok(html.includes('You skipped this') && html.includes('the agent knows to drop it') && !html.includes('data-move="skip"'));

// The item page: tabs above, the answer docked below.
context.decide = item({id: 'dq', kind: 'question', title: 'Pick a date', options: [
  {label: 'Tuesday the 12th', note: 'Support gets a week', recommended: true}, {label: 'Thursday <14th>', note: 'More docs time'}, 'Later'],
  matters: {lead: 'Unblocks the release', points: []}, needs_you: {lead: 'Your call', points: []}, recommendation: 'Tuesday'});
context.plain = item({id: 'pl', title: 'Legacy item', detail: 'Only text'});
context.failed = item({id: 'fd', status: 'answered', bucket: 'waiting', delivery: 'failed', answer: 'Annual', delivery_error: 'Codex is not running'});
context.queue = {...context.queue, revision: 11, items: [...context.queue.items, context.decide, context.plain, context.failed]};
run('applyState(queue); model = derive()');
const pane = (id) => run(`detailHTML(model.items.find(i => i.id === '${id}'))`);
const tabIds = (id) => Array.from(run(`tabsOf(model.items.find(i => i.id === '${id}')).map(t => t.id)`));
// Steps leads when there are steps; without them Why, and a bare item opens on Context.
assert.deepEqual(tabIds('r'), ['steps', 'why', 'context', 'ask']);
assert.deepEqual(tabIds('dq'), ['why', 'context', 'ask']); // no steps, so no Steps tab
assert.deepEqual(tabIds('pl'), ['context', 'ask']);
assert.equal(run("tabOf(model.items.find(i => i.id === 'r'))"), 'steps');
assert.equal(run("tabOf(model.items.find(i => i.id === 'dq'))"), 'why');
assert.equal(run("tabOf(model.items.find(i => i.id === 'pl'))"), 'context');
assert.equal(run("tabOf(model.items.find(i => i.id === 'dq'), {tab: 'steps'})"), 'why'); // a tab it lacks falls back
html = pane('r');
assert.ok(html.includes('role="tablist"') && html.includes('data-tab="steps" aria-selected="true"') && html.includes('Steps<span class="n">4</span>'));
assert.ok(html.indexOf('class="d-head"') < html.indexOf('class="d-tabs"') && html.indexOf('class="d-body') < html.indexOf('class="d-dock"'));
assert.ok(pane('q1').includes('Ask<span class="n">1</span>'));
assert.ok(pane('pl').includes('Only text'));
// Nothing picked: the box is the answer, with its arrow, and the send row is folded away.
html = pane('dq');
assert.ok(html.includes('data-state="free"') && html.includes('placeholder="Or answer in your own words…"'));
assert.ok(html.includes('class="d-fold open" data-fold="note"') && html.includes('class="d-fold " data-fold="go" inert'));
assert.ok(html.includes('role="radio" data-choice="Thursday &lt;14th&gt;" aria-checked="false"') && !html.includes('<14th>'));
assert.ok(html.includes('aria-label="Send" disabled'));
// Picked: the box folds away and the wide button sends the option.
run("setDraft('dq', {choice: 'Tuesday the 12th'})");
html = pane('dq');
assert.ok(html.includes('data-state="picked"') && html.includes('class="d-fold " data-fold="note" inert') && html.includes('class="d-fold open" data-fold="go"'));
assert.ok(html.includes('<span>Send: Tuesday the 12th</span>') && html.includes('</svg>Note</button>'));
const reply = (draft, typed) => run(`replyText(model.items.find(i => i.id === 'dq'), ${JSON.stringify(draft)}, ${JSON.stringify(typed)})`);
assert.equal(reply({choice: 'Tuesday the 12th'}, ''), 'Tuesday the 12th');
// + Note opens the box above the row, with your text kept and the label saying so.
run("setDraft('dq', {note: true, text: 'after standup'})");
html = pane('dq');
assert.ok(html.includes('data-state="note"') && html.includes('class="d-fold open" data-fold="note"') && html.includes('class="d-fold open" data-fold="go"'));
assert.ok(html.includes('placeholder="Add a note to “Tuesday the 12th”…"') && html.includes('>after standup</textarea>'));
assert.ok(html.includes('aria-pressed="true">Remove note') && html.includes('<span>Send: Tuesday the 12th + note</span>'));
assert.equal(run("wideLabel('Later', '')"), 'Send: Later');
assert.equal(reply({choice: 'Tuesday the 12th', note: true}, ' after standup '), 'Tuesday the 12th — after standup'); // as replies always were
// Text typed before picking becomes the note; unpicking turns it back into the answer.
const state = (draft) => run(`answerState(model.items.find(i => i.id === 'dq'), ${JSON.stringify(draft)})`);
assert.equal(state({choice: 'Later', text: 'because'}), 'note');
assert.equal(state({choice: null, text: 'because'}), 'free');
assert.equal(reply({text: 'because'}, 'because'), 'because');
assert.equal(state({choice: 'Gone option'}), 'free'); // an option the agent removed is not sent
// Escape closes an empty note first; with text in it, Escape is left alone.
assert.equal(run("noteEscape(model.items.find(i => i.id === 'dq'), {choice: 'Later', note: true}, '  ')"), true);
assert.equal(run("noteEscape(model.items.find(i => i.id === 'dq'), {choice: 'Later', note: true}, 'kept')"), false);
assert.equal(run("noteEscape(model.items.find(i => i.id === 'dq'), {}, '')"), false);
assert.equal(state({choice: 'Later', note: false}), 'picked');
run("setDraft('dq', {choice: undefined, note: undefined, text: undefined})");
// Enter sends, Shift+Enter is a new line, and an input method's Enter is its own.
assert.equal(run("composeKey({key: 'Enter'})"), 'send');
assert.equal(run("composeKey({key: 'Enter', metaKey: true})"), 'send');
assert.equal(run("composeKey({key: 'Enter', shiftKey: true})"), 'newline');
assert.equal(run("composeKey({key: 'Enter', isComposing: true})"), null);
assert.equal(run("composeKey({key: 'Enter', keyCode: 229})"), null);
assert.equal(run("composeKey({key: 'a'})"), null);
// Free replies keep their own words, in a box with no footer or hint line below it.
html = pane('a1');
assert.ok(html.includes('placeholder="Tell the agent what you did…"') && !html.includes('compose-hint') && !html.includes('composer-foot'));
assert.ok(!html.includes('data-fold="go"'));
assert.ok(html.includes('data-done>Mark done') && !pane('dq').includes('data-done')); // decisions are answered or skipped
// A reply that failed to deliver offers the wide Retry.
html = pane('fd');
assert.ok(html.includes('<button class="go-wide" type="button" data-retry><span>Retry delivery</span>') && html.includes('Codex is not running'));
assert.ok(!html.includes('id="respond"'));

// The floating panel shares this state and reuses the item pane.
const floatSource = fs.readFileSync('hq/static/float.js', 'utf8');
vm.runInContext(floatSource.slice(0, floatSource.indexOf('function bootFloat()')), context);
const area = {x: 0, y: 25, width: 1440, height: 800}; // a menu bar on top, no Dock
const rect = (dock, size = {width: 72, height: 32}, free = area) => JSON.parse(run(`JSON.stringify(dockRect(${JSON.stringify(dock)}, ${JSON.stringify(size)}, ${JSON.stringify(free)}))`));

// Corners sit right against the free screen, and the bottom edge sits closest of all.
assert.deepEqual(rect('top-left'), {x: 10, y: 35, width: 72, height: 32});
assert.deepEqual(rect('top-right'), {x: 1358, y: 35, width: 72, height: 32});
assert.deepEqual(rect('top'), {x: 684, y: 35, width: 72, height: 32});
assert.deepEqual(rect('left'), {x: 10, y: 409, width: 72, height: 32});
assert.deepEqual(rect('right'), {x: 1358, y: 409, width: 72, height: 32});
assert.deepEqual(rect('bottom-left'), {x: 10, y: 789, width: 72, height: 32});
assert.deepEqual(rect('bottom-right'), {x: 1358, y: 789, width: 72, height: 32});
assert.deepEqual(rect('bottom'), {x: 684, y: 789, width: 72, height: 32});
// The box grows away from its dock and always stays on the free screen.
assert.deepEqual(rect('top-right', {width: 480, height: 600}), {x: 950, y: 35, width: 480, height: 600});
assert.deepEqual(rect('bottom-right', {width: 480, height: 600}), {x: 950, y: 221, width: 480, height: 600});
assert.deepEqual(rect('top-left', {width: 480, height: 5000}), {x: 10, y: 35, width: 480, height: 786});
assert.deepEqual(rect('top-left', {width: 500, height: 100}, {x: 0, y: 0, width: 520, height: 400}), {x: 10, y: 10, width: 500, height: 100});
// Docking follows the free area of a display, wherever that is.
assert.deepEqual(rect('left', {width: 72, height: 32}, {x: 80, y: 0, width: 1000, height: 700}), {x: 90, y: 334, width: 72, height: 32});
assert.deepEqual(rect('no such dock'), rect('top-left')); // an old saved name falls back
assert.equal(run("growFrom('top-left')"), 'left top');
// The capsule stands up at the sides and lies along an edge when centered on it.
for (const dock of ['top-left', 'top-right', 'left', 'right', 'bottom-left', 'bottom-right']) assert.equal(run(`layoutFor('${dock}')`), 'vertical', dock);
for (const dock of ['top', 'bottom']) assert.equal(run(`layoutFor('${dock}')`), 'horizontal', dock);
assert.equal(run("layoutFor('bottom', 'vertical')"), 'vertical'); // the lab can force either
assert.equal(run("layoutFor('left', 'horizontal')"), 'horizontal');
const measured = {width: 38, height: 102};
assert.deepEqual(JSON.parse(run(`JSON.stringify(capsuleSize(${JSON.stringify(measured)}, 'horizontal'))`)), {width: 102, height: 38});
assert.deepEqual(JSON.parse(run(`JSON.stringify(capsuleSize({width: 102, height: 38}, 'vertical'))`)), measured);
const zones = JSON.parse(run(`JSON.stringify(dockRects(${JSON.stringify(measured)}, ${JSON.stringify(area)}))`));
assert.deepEqual(zones.bottom, {x: 669, y: 783, width: 102, height: 38}); // lying down, 4 above the free edge
assert.deepEqual(zones['top-left'], {x: 10, y: 35, width: 38, height: 102}); // standing up, in the corner
assert.deepEqual(zones.top, {x: 669, y: 35, width: 102, height: 38});
assert.equal(run("growFrom('bottom')"), 'center bottom');

// A drop goes to the nearest target, measured to its center, as in OpenFlow.
const nearest = (x, y, set = 'all') => run(`nearestDock(${x}, ${y}, dockRects({width: 72, height: 32}, ${JSON.stringify(area)}, TARGETS[${JSON.stringify(set)}]))`);
assert.equal(nearest(5, 5), 'top-left');
assert.equal(nearest(1435, 5), 'top-right');
assert.equal(nearest(720, 40), 'top');
assert.equal(nearest(5, 425), 'left');
assert.equal(nearest(720, 820), 'bottom');
assert.equal(nearest(720, 820, 'corners'), 'bottom-left'); // equally far, so the first wins; only corners are offered
assert.equal(nearest(1430, 820, 'flow'), 'right'); // the corner it would prefer is not offered
assert.deepEqual(Array.from(run("Object.keys(dockRects({width: 72, height: 32}, area0 = {x: 0, y: 0, width: 800, height: 600}, TARGETS.flow))")), ['top-left', 'left', 'right', 'bottom']);

// The list is what needs you, and a reply that failed to deliver needs you again.
const floatItems = [
  item({id: 'f-old', title: 'Older action', created: now - 90000}),
  item({id: 'f-new', title: 'Newest <b>decision</b>', kind: 'question', created: now - 60}),
  item({id: 'f-pin', title: 'Pinned action', pinned: true, created: now - 200000}),
  item({id: 'f-due', title: 'Due today', due: run('today()'), created: now - 150000}),
  item({id: 'f-rank', title: 'Ranked first', rank: 10, created: now - 80000}),
  item({id: 'f-fail', title: 'Reply bounced', status: 'answered', bucket: 'waiting', delivery: 'failed', created: now - 300000}),
  item({id: 'f-sent', title: 'Reply fine', status: 'answered', bucket: 'waiting', delivery: 'pending'}),
  item({id: 'f-done', title: 'Old failure, finished', status: 'done', bucket: 'done', delivery: 'failed'}),
  item({id: 'f-skip', title: 'Skip not delivered', status: 'done', bucket: 'done', skipped: true, delivery: 'failed', created: now - 400000}),
  item({id: 'f-agent', title: 'Agent work', owner: 'agent', bucket: 'pending'}),
  item({id: 'f-later', title: 'Snoozed', bucket: 'later', defer_until: '2099-01-01'}),
  item({id: 'f-ask', title: 'Asking', asking: true}),
];
context.queue = {instance: 'server-float', revision: 1, sessions: context.queue.sessions, items: floatItems};
run('applyState(queue); model = derive()');
const listed = () => Array.from(run('attention().map(i => i.id)'));
assert.deepEqual(listed(), ['f-fail', 'f-skip', 'f-pin', 'f-due', 'f-new', 'f-rank', 'f-old']);
run("feel.order = 'newest'");
assert.deepEqual(listed(), ['f-fail', 'f-skip', 'f-new', 'f-rank', 'f-old', 'f-due', 'f-pin']);
run("feel.order = 'urgent'");
assert.deepEqual(listed(), ['f-fail', 'f-skip', 'f-pin', 'f-due', 'f-rank', 'f-new', 'f-old']);
run("feel.order = 'lead'; feel.show = 2");
assert.deepEqual(listed(), ['f-fail', 'f-skip']);
run('feel.show = 100000');
assert.ok(run('feel.show') >= 10000); // the real list is never cut short

html = run('floatListHTML(attention(), true)');
assert.ok(html.includes('Newest &lt;b&gt;decision&lt;/b&gt;') && !html.includes('<b>decision</b>'));
assert.ok(html.includes('data-item="f-new"') && html.includes('Decision') && html.includes('NoClick › Support'));
assert.ok(html.includes('Reply failed') && html.includes('data-view="needs" aria-selected="true">Needs you<small>7</small>') && html.includes('data-more'));
assert.ok(html.includes('data-view="agents" aria-selected="false">Agents<small>1 running</small>')); // the other list, beside it
assert.ok(!html.includes('data-collapse') && !html.includes('target="_blank"')); // one menu, not a cross and an arrow
assert.ok(!html.includes('Reconnecting'));
assert.ok(run('floatListHTML(attention(), false)').includes('Reconnecting'));
assert.ok(run('floatListHTML([], true)').includes('Nothing needs you'));

// Agents already running when the panel starts are its baseline, so nothing is announced.
run("agentMemory.clear(); watchAgents.settled = undefined; heard = []; realEnqueue = enqueue; enqueue = (events) => heard.push(...events)");
run('watchAgents()');
assert.equal(run("agentMemory.get('codex:a').running"), true); // known to be running already
run("watchAgents(); trackAgents(agentMemory, state.sessions.map((s) => ({id: s.id, name: s.name, harness: harness(s.provider), state: agentState(s)})), Date.now() / 1000 + 60).forEach((e) => heard.push(e))");
assert.equal(run('heard.length'), 0);
run('enqueue = realEnqueue; agentMemory.clear(); watchAgents.settled = undefined');
// The spotlight answers the pill alone: a few pixels of slack, never the open box.
const pillRect = {left: 10, right: 48, top: 35, bottom: 137}, boxRect = {left: 56, right: 436, top: 35, bottom: 400};
assert.equal(run(`overPill(${JSON.stringify(pillRect)}, null, 20, 60)`), true);
assert.equal(run(`overPill(${JSON.stringify(pillRect)}, null, 52, 60)`), true); // just past the edge
assert.equal(run(`overPill(${JSON.stringify(pillRect)}, ${JSON.stringify(boxRect)}, 200, 60)`), false); // in the list
assert.equal(run(`overPill(${JSON.stringify(pillRect)}, null, 200, 60)`), false); // far away, box or not
assert.equal(run(`overPill(${JSON.stringify(pillRect)}, ${JSON.stringify({...boxRect, left: 50})}, 52, 60)`), false); // the box wins where they meet
// With the box open the circle still steps forward, but its label would cover the header.
assert.equal(run('spotLabelled(false)'), true);
assert.equal(run('spotLabelled(true)'), false);

// Agents, by folder: running first, then waiting on you, then finished in the last day.
const clockNow = Math.floor(Date.now() / 1000); // ago() reads the real clock
context.agentSessions = [
  {id: 'c:old', name: 'Old <chat>', provider: 'claude-code', cwd: '/Users/someone/code/harbor', activity: {state: 'idle'}, last_active: clockNow - 3 * 86400},
  {id: 'c:done', name: 'Docs pass', provider: 'claude-code', cwd: '/Users/someone/code/harbor', activity: {state: 'idle'}, last_active: clockNow - 2 * 3600},
  {id: 'x:run', name: 'Release build', provider: 'codex', cwd: '/Users/someone/code/lantern/', activity: {state: 'active', source: 'codex-live', since: clockNow - 65}, last_active: clockNow},
  {id: 'c:bg', name: 'Research with subagents', provider: 'claude-code', cwd: '/Users/someone/code/harbor', activity: {state: 'active', source: 'background', since: clockNow - 3725}, last_active: clockNow},
  {id: 'c:wait', name: 'Asks you first', provider: 'claude-code', cwd: '/Users/someone/code/site', activity: {state: 'waiting', since: clockNow - 30}, last_active: clockNow - 40},
  {id: 'c:nosig', name: 'No signal', provider: 'claude-code', cwd: '/Users/someone/code/site', activity: {state: 'unknown'}, last_active: clockNow - 600},
  {id: 'c:nofolder', name: 'Desktop chat', provider: 'claude-desktop', activity: {state: 'idle'}, last_active: clockNow - 1200},
];
context.agentItems = [item({id: 'ai1', session_id: 'c:done', title: 'Check the docs'}), item({id: 'ai2', session_id: 'c:done', title: 'With it', owner: 'agent', bucket: 'pending'})].map((i) => ({...i, lane: run('laneOf')(i)}));
const folders = JSON.parse(run(`JSON.stringify(agentFolders(agentSessions, agentItems, ${clockNow}))`));
// Folders are named by their last part only, never the path to them, and the one with work running leads.
assert.deepEqual(folders.groups.map((f) => f.name), ['lantern', 'harbor', 'site', 'No folder']);
assert.ok(!JSON.stringify(folders.groups.map((f) => [f.name, f.agents.map((a) => a.name)])).includes('/Users'));
assert.deepEqual(folders.groups.map((f) => f.running), [true, true, false, false]);
// Within a folder, running comes before finished; anything quiet for over a day folds away.
assert.deepEqual(folders.groups[1].agents.map((a) => a.id), ['c:bg', 'c:done']);
assert.deepEqual(folders.quiet.map((f) => [f.name, f.agents.map((a) => a.id)]), [['harbor', ['c:old']]]);
assert.equal(folders.running, 2);
assert.equal(folders.finished, 2); // the docs pass and the desktop chat, not the one with no live signal
// A conversation with agents working in the background is one running agent, and says so.
const bg = folders.groups[1].agents[0];
assert.equal(folders.groups.flatMap((f) => f.agents).filter((a) => a.id === 'c:bg').length, 1);
assert.equal(bg.state, 'active');
assert.equal(bg.background, true);
const meta = (id) => run(`agentMeta(agentFolders(agentSessions, agentItems, ${clockNow}).all.find((a) => a.id === '${id}'), ${clockNow})`);
assert.equal(meta('c:bg'), `running <span class="tick" data-since="${clockNow - 3725}">1h 02m</span> · in the background`);
// Times: a running clock from when the state began, and how long ago a finished one last ran.
assert.equal(meta('x:run'), `running <span class="tick" data-since="${clockNow - 65}">1m 05s</span>`);
assert.equal(run('ranFor(0)'), '0m 00s');
assert.equal(run('ranFor(7200)'), '2h 00m');
assert.equal(meta('c:wait'), 'waiting on you');
assert.ok(meta('c:done').startsWith('finished ') && meta('c:done').endsWith('h ago'));
assert.ok(meta('c:nosig').startsWith('no live signal · seen running')); // never "finished"
assert.ok(run(`agentStateLine(agentFolders(agentSessions, agentItems, ${clockNow}).all.find((a) => a.id === 'c:bg'), ${clockNow})`).startsWith('Agents running in the background for'));
assert.equal(folders.all.find((a) => a.id === 'c:done').you, 1);
assert.equal(run("agentSide({you: 2, open: 3})"), '<b>2</b> for you');
assert.equal(run("agentSide({you: 0, open: 3})"), '3 open');
assert.equal(run("folderName('/Users/someone/code/HQ/')"), 'HQ');
assert.equal(run("folderName('')"), 'No folder');
// The panel shows the same folders, and an agent's own page lists its items to open.
run(`savedSessions = state.sessions; state.sessions = agentSessions; listView = 'agents'`);
html = run('agentsPanelHTML(attention(), true)');
assert.ok(html.includes('data-view="agents" aria-selected="true"') && html.includes('class="f-folder"') && html.includes('data-agent="c:bg"'));
assert.ok(html.includes('Old &lt;chat&gt;') === false && html.includes('data-agents-all>1 more, in 1 folder')); // quiet ones folded
assert.ok(!html.includes('/Users/someone'));
run("agentOpen = 'c:done'");
html = run('agentsPanelHTML(attention(), true)');
assert.ok(html.includes('data-back') && html.includes('Docs pass') && html.includes('<dt>Folder</dt><dd>harbor</dd>'));
run("agentOpen = 'c:gone'");
assert.ok(run('agentsPanelHTML(attention(), true)').includes('data-agent="c:bg"')); // a vanished agent falls back to the list
run(`state.sessions = savedSessions; listView = 'needs'; agentOpen = null`);

// Opening and closing are quick: a spring that settles in about 200ms, with no long tail.
assert.ok(run('springMs(SNAP)') >= 180 && run('springMs(SNAP)') <= 220, `snap spring lasts ${run('springMs(SNAP)')}ms`);
assert.ok(run('FOLD_MS') <= 240);
assert.ok(run('springMs({stiffness: 190, damping: 17})') > 600); // the springs left for other motion are slower

// Keys inside the open panel: arrows by default, Enter also opens, Escape also goes back.
const combo = (e) => run(`keyCombo(${JSON.stringify(e)})`);
assert.equal(combo({key: 'ArrowUp'}), 'ArrowUp');
assert.equal(combo({key: 'K', shiftKey: true}), 'k'); // a letter is itself, whatever shift does to it
assert.equal(combo({key: 'ArrowLeft', altKey: true}), 'Alt+ArrowLeft');
assert.equal(combo({key: 'Tab', shiftKey: true, metaKey: true}), 'Shift+Meta+Tab');
assert.equal(run("keyLabel('ArrowLeft')"), '←');
assert.equal(run("keyLabel('Alt+h')"), '⌥H');
const action = (key, mode) => run(`panelAction(${JSON.stringify(key)}, ${JSON.stringify(mode)})`);
assert.equal(action('ArrowUp', 'list'), 'previous');
assert.equal(action('ArrowDown', 'list'), 'next');
assert.equal(action('ArrowRight', 'list'), 'open');
assert.equal(action('Enter', 'list'), 'open');
assert.equal(action('Enter', 'item'), null); // in an item, Enter belongs to whatever has focus
assert.equal(action('ArrowLeft', 'item'), 'back');
assert.equal(action('Escape', 'item'), 'back');
assert.equal(action('Escape', 'list'), null); // the list's Escape collapses the panel, as before
assert.equal(action('ArrowDown', 'item'), 'next');
assert.deepEqual(JSON.parse(run("JSON.stringify(cleanPanelKeys({back: 'h', open: null, next: 42}))")), {previous: 'ArrowUp', next: 'ArrowDown', open: null, back: 'h'});
run("panelKeys = cleanPanelKeys({back: 'h'})");
assert.equal(action('h', 'item'), 'back');
assert.equal(action('ArrowLeft', 'item'), null); // rebound, so the old key does nothing
assert.equal(action('Escape', 'item'), 'back'); // Escape always goes back
run('panelKeys = cleanPanelKeys()');
// While typing, the arrows and Enter belong to the text, and Escape first leaves the box.
run(`floatOpen = true; customizing = false; route.item = 'a1'; stepped = []; went = [];
  realStep = step; step = (n) => stepped.push(n); realGo = go; go = (next) => went.push(next);
  keyEvent = (key, typing) => ({key, defaultPrevented: false, prevented: 0, stopped: 0, blurred: 0,
    preventDefault() {this.prevented += 1;}, stopImmediatePropagation() {this.stopped += 1;},
    target: {matches: () => typing, blur() {keyEventBlurred += 1;}}}); keyEventBlurred = 0;`);
run(`e1 = keyEvent('ArrowDown', true); panelKey(e1); e2 = keyEvent('Enter', true); panelKey(e2)`);
assert.equal(run('e1.prevented + e2.prevented + stepped.length + went.length'), 0); // caret moves, nothing else
run(`e3 = keyEvent('Escape', true); panelKey(e3)`);
assert.equal(run('keyEventBlurred'), 1);
assert.equal(run('went.length'), 0); // the first Escape only leaves the box
run(`e4 = keyEvent('Escape', false); panelKey(e4)`);
assert.deepEqual(JSON.parse(run('JSON.stringify(went)')), [{item: null}]); // the second goes back
assert.equal(run('cursor'), 'a1'); // with the item it came from highlighted
run(`e5 = keyEvent('ArrowDown', false); panelKey(e5); e6 = keyEvent('ArrowUp', false); panelKey(e6)`);
assert.deepEqual(Array.from(run('stepped')), [1, -1]); // up and down move between items in item view
run('step = realStep; go = realGo; floatOpen = false; route.item = null; cursor = null');

// Resizing: only the edges away from the pill, and both edges together where the box is centred.
const edges = (dock) => JSON.parse(run(`JSON.stringify(freeEdges('${dock}'))`));
assert.deepEqual(edges('top-left'), {x: ['right'], y: ['bottom'], centeredX: false, centeredY: false});
assert.deepEqual(edges('top-right'), {x: ['left'], y: ['bottom'], centeredX: false, centeredY: false});
assert.deepEqual(edges('bottom-left'), {x: ['right'], y: ['top'], centeredX: false, centeredY: false});
assert.deepEqual(edges('right'), {x: ['left'], y: ['top', 'bottom'], centeredX: false, centeredY: true});
assert.deepEqual(edges('bottom'), {x: ['left', 'right'], y: ['top'], centeredX: true, centeredY: false});
assert.deepEqual(edges('top'), {x: ['left', 'right'], y: ['bottom'], centeredX: true, centeredY: false});
const dhruv = {x: 0, y: 33, width: 1728, height: 994}, pillAtRight = {x: 1680, y: 480, width: 38, height: 102};
const limits = JSON.parse(run(`JSON.stringify(sizeLimits('right', 'list', ${JSON.stringify(pillAtRight)}, ${JSON.stringify(dhruv)}))`));
assert.deepEqual(limits, {min: {width: 300, height: 200}, max: {width: 1728 - 20 - 38 - 8, height: 994 - 14}});
const resize = (edge, dx, dy, dock = 'right', start = {width: 380, height: 520}) => JSON.parse(run(`JSON.stringify(resized(${JSON.stringify(start)}, '${edge}', ${dx}, ${dy}, '${dock}', ${JSON.stringify(limits)}))`));
assert.deepEqual(resize('left', -120, 0), {width: 500, height: 520}); // pulling the left edge left widens it
assert.deepEqual(resize('bottom', 0, 50), {width: 380, height: 620}); // centred on the pill, so it grows both ways
assert.deepEqual(resize('top-left', -20, -10), {width: 400, height: 540});
assert.deepEqual(resize('left', 500, 0), {width: 300, height: 520}); // never below the minimum
assert.deepEqual(resize('left', -5000, 0), {width: limits.max.width, height: 520}); // nor off the screen
assert.deepEqual(resize('right', 40, 30, 'top-left'), {width: 420, height: 520}); // a top-left box has no top edge to drag
assert.deepEqual(resize('bottom', 0, 30, 'top-left'), {width: 380, height: 550});
// Sizes are kept per view, and anything odd in settings falls back to the default.
assert.deepEqual(JSON.parse(run("JSON.stringify(cleanSizes({list: {width: 500.4, height: 700}, item: {width: 'wide'}}))")), {list: {width: 500, height: 700}, item: null});
assert.deepEqual(JSON.parse(run("JSON.stringify(chosenSize('list', cleanSizes()))")), {width: 380, height: 520});
assert.deepEqual(JSON.parse(run("JSON.stringify(chosenSize('item', cleanSizes({item: {width: 640, height: 900}})))")), {width: 640, height: 900});

// The pill: a black capsule of circles you choose, with no wordmark.
run("circles = ['needs', 'agents', 'claude']");
html = run('pillHTML(attention(), true)');
assert.equal((html.match(/class="f-circle/g) || []).length, 3);
assert.ok(!html.includes('wordmark') && !html.includes('>HQ<'));
assert.ok(html.indexOf('data-metric="needs"') < html.indexOf('data-metric="agents"'));
assert.ok(html.includes('<span class="f-circle loud"><span>7</span></span>'));
assert.ok(html.includes('7 need you'));
run("circles = ['agents', 'needs']");
assert.ok(run('pillHTML(attention(), true)').indexOf('data-metric="agents"') < run('pillHTML(attention(), true)').indexOf('data-metric="needs"')); // your order
run("feel.style = 'outline'");
assert.ok(!run('pillHTML(attention(), true)').includes('loud'));
run("feel.style = 'attention'; circles = ['needs']");
assert.ok(!run('pillHTML([], true)').includes('loud') && run('pillHTML([], true)').includes('<span>0</span>')); // nothing to act on
assert.ok(run('pillHTML(attention(), false)').includes('dot err') && run('pillHTML(attention(), false)').includes('Reconnecting'));
assert.ok(!run('pillHTML(attention(), true)').includes('dot err'));
assert.equal(run('pillLabel(attention(), true)'), 'HQ. 7 need you.');
assert.equal(run('pillLabel(attention().slice(0, 1), true)'), 'HQ. 1 needs you.');
assert.equal(run('pillLabel([], false)'), 'HQ. 0 need you. Reconnecting.');
assert.equal(run('shortCount(144)'), '144');
assert.equal(run('shortCount(1000)'), '1k+');
assert.ok(run("circleHTML({value: 144})").includes('wide'));
assert.ok(!run("circleHTML({value: 14})").includes('wide'));

// Agents running counts only what is verifiably active.
assert.equal(run("METRICS.agents.read([]).value"), run("model.agents.filter(a => agentState(a) === 'active').length"));
run("setLive(false)");
assert.equal(run("METRICS.agents.read([]).value"), 0); // offline, so nothing is verifiably running
run("setLive(true)");

// Usage is never invented: nothing connected shows an empty circle.
run("state.usage = {claude: null}");
assert.equal(run("METRICS.claude.read([]).value"), null);
assert.equal(run("METRICS.codex.read([]).value"), null);
html = run("circleHTML(METRICS.claude.read([]))");
assert.ok(html.includes('none') && html.includes('–') && !html.includes('<svg'));
assert.ok(run("METRICS.claude.read([]).tag").includes('not connected'));
// The tightest limit is the one that will stop you.
const resets = Math.round(now + 7200);
run(`state.usage = {claude: {as_of: Date.now() / 1000, windows: [{name: 'weekly', left: 81, resets_at: null}, {name: '5-hour', left: 62, resets_at: ${resets}}]}}`);
assert.equal(run("METRICS.claude.read([]).value"), 62);
assert.ok(run("METRICS.claude.read([]).tag").startsWith("62% of Claude's 5-hour limit left, resets "));
html = run("circleHTML(METRICS.claude.read([]))");
assert.ok(html.includes('stroke-dasharray="62 100"') && html.includes('gauge') && !html.includes('low') && !html.includes('stale'));
run("state.usage.claude.windows[1].left = 5");
assert.ok(run("circleHTML(METRICS.claude.read([]))").includes('low'));
run("state.usage.claude.windows[1].left = 62; state.usage.claude.as_of = Date.now() / 1000 - 7200");
assert.ok(run("circleHTML(METRICS.claude.read([]))").includes('stale'));
assert.ok(run("METRICS.claude.read([]).tag").includes('(as of 2h ago)'));
run("state.usage = {claude: {as_of: 1, windows: [{name: 'weekly', left: 'lots'}]}}");
assert.equal(run("METRICS.claude.read([]).value"), null); // a malformed number is not a number
run("delete state.usage");
assert.equal(run("METRICS.claude.read([]).value"), null);
// A usage change alone has to repaint the pill.
run("snapshotSignature = ''");
context.queue = {...context.queue, revision: context.queue.revision + 1, usage: {codex: {as_of: 1, windows: [{name: '5-hour', left: 40}]}}};
run('applyState(queue)');
assert.ok(run('snapshotSignature').includes('"left":40'));
run("state.usage = undefined");

// Choosing circles: one always stays, order is yours, and old saved names cannot break it.
const list = (code) => Array.from(run(code));
assert.deepEqual(list("cleanCircles(['needs', 'gone-metric', 'needs', 'codex'])"), ['needs', 'codex']);
assert.deepEqual(list('cleanCircles(undefined)'), list('DEFAULT_CIRCLES'));
assert.deepEqual(list("cleanCircles('needs')"), list('DEFAULT_CIRCLES'));
assert.deepEqual(list("cleanCircles([])"), list('DEFAULT_CIRCLES'));
assert.deepEqual(list("toggleCircle(['needs'], 'agents')"), ['needs', 'agents']);
assert.deepEqual(list("toggleCircle(['needs', 'agents'], 'needs')"), ['agents']);
assert.deepEqual(list("toggleCircle(['needs'], 'needs')"), ['needs']);
assert.deepEqual(list("moveCircle(['a', 'b', 'c'], 'c', -1)"), ['a', 'c', 'b']);
assert.deepEqual(list("moveCircle(['a', 'b', 'c'], 'a', -1)"), ['a', 'b', 'c']);
assert.deepEqual(list("moveCircle(['a', 'b', 'c'], 'c', 1)"), ['a', 'b', 'c']);
assert.deepEqual(list("moveCircle(['a'], 'gone', 1)"), ['a']);
run("circles = ['agents']");
html = run('settingsHTML(attention())');
for (const metric of ['needs', 'decisions', 'due', 'agents', 'claude', 'codex']) assert.ok(html.includes(`data-circle="${metric}"`), metric);
assert.ok(html.indexOf('data-circle="agents"') < html.indexOf('data-circle="needs"')); // what you chose comes first
assert.ok(/data-circle="agents" aria-checked="true" disabled/.test(html)); // the last circle cannot be removed
assert.ok(html.includes('Not connected') && html.includes('f-preview'));
run("circles = ['agents', 'needs']");
assert.ok(!/data-circle="agents" aria-checked="true" disabled/.test(run('settingsHTML(attention())')));
run("circles = ['needs', 'agents', 'codex']");
// Labels open away from the screen edge, and only where the circles would stay put.
assert.equal(run("labelSide('top-left')"), 'right');
assert.equal(run("labelSide('bottom-right')"), 'left');
assert.equal(run("labelSide('top')"), 'none');
assert.equal(run("labelSide('top-left', 'horizontal')"), 'none');
assert.equal(run("surfaceMode(true, null, true)"), 'settings');
assert.equal(run("surfaceMode(false, null, true)"), 'pill');

// Notifications say what changed, whoever did it.
const changes = (before, after, all = []) => JSON.parse(run(`JSON.stringify(itemChanges(${JSON.stringify(before)}, ${JSON.stringify(after)}, ${JSON.stringify(all)}))`));
const plainItem = (fields) => ({id: 'x', title: 'Title <b>', project_name: 'Harbor', kind: 'todo', lane: 'you', status: 'open', ...fields});
let event = changes([], [plainItem({id: 'n', kind: 'question'})])[0];
assert.deepEqual([event.title, event.sub, event.delta, event.item, event.group], ['Title <b>', 'New decision · Harbor', 1, 'n', 'items']);
assert.equal(changes([], [plainItem({kind: 'blocker'})])[0].sub, 'New blocker · Harbor');
assert.equal(changes([], [plainItem({})])[0].sub, 'New action · Harbor');
event = changes([], [plainItem({delivery: 'failed', status: 'answered'})])[0];
assert.equal(event.sub, 'Reply failed · Harbor');
assert.equal(event.dot, 'bad');
const gone = (latest) => changes([plainItem({})], [], latest ? [plainItem(latest)] : [])[0].sub;
assert.equal(gone(null), 'Done · Harbor');
assert.equal(gone({status: 'done'}), 'Done · Harbor');
assert.equal(gone({status: 'done', skipped: true}), 'Skipped · Harbor');
assert.equal(gone({status: 'answered'}), 'Reply saved · Harbor'); // saved, never received
assert.equal(gone({lane: 'later'}), 'Snoozed · Harbor');
assert.equal(gone({asking: true}), 'Asked the agent · Harbor');
assert.equal(gone({owner: 'agent'}), 'With the agent · Harbor');
assert.equal(gone({owner: 'external'}), 'Waiting on someone · Harbor');
assert.deepEqual(changes([plainItem({})], [plainItem({})]), []); // nothing changed, nothing to say

// An agent only counts once its new state has held, and unknown never reads as stopped.
const agent = (state) => [{id: 'a', name: 'Triage', harness: 'Codex', state}];
run('tracked = new Map()');
const track = (state, t, settled = true) => JSON.parse(run(`JSON.stringify(trackAgents(tracked, ${JSON.stringify(agent(state))}, ${t}, ${settled}))`));
assert.deepEqual(track('idle', 100, false), []); // the baseline is not news
assert.deepEqual(track('active', 101), []); // it just started; wait
assert.deepEqual(track('idle', 102), []); // a flicker between turns
assert.deepEqual(track('active', 103), []);
assert.deepEqual(track('active', 105), []); // 2 seconds is not enough
event = track('active', 106)[0];
assert.deepEqual([event.title, event.sub, event.dot, event.delta], ['Triage started', 'Codex', 'on', 1]);
assert.deepEqual(track('active', 107), []); // said once
assert.deepEqual(track('unknown', 500), []); // a lost connection says nothing
assert.deepEqual(track('idle', 501), []);
assert.deepEqual(track('unknown', 900), []);
assert.deepEqual(track('idle', 505), []);
event = track('idle', 510)[0];
assert.deepEqual([event.title, event.dot, event.delta], ['Triage stopped', 'off', -1]);
run('tracked = new Map()');
assert.deepEqual(track('active', 1, false), []); // already running at startup: baseline
run('tracked = new Map()');
assert.deepEqual(track('active', 1), []); // appears running after startup: it just started, and holds first
assert.equal(track('active', 5)[0].title, 'Triage started');

// Usage speaks only at the moment it crosses the line.
const usageAt = (left) => ({codex: {as_of: 1, windows: [{name: 'weekly', left, resets_at: null}]}});
const crossed = (a, b) => JSON.parse(run(`JSON.stringify(usageChanges(${JSON.stringify(a)}, ${JSON.stringify(b)}))`));
event = crossed(usageAt(11), usageAt(10))[0];
assert.deepEqual([event.title, event.sub, event.dot], ['Codex is nearly out', '10% of the weekly limit left', 'bad']);
assert.deepEqual(crossed(usageAt(10), usageAt(9)), []); // already said
assert.deepEqual(crossed(usageAt(50), usageAt(40)), []);
assert.deepEqual(crossed(null, usageAt(5)), []); // first sight is not a change
assert.deepEqual(crossed(usageAt(50), {codex: null}), []);

// A burst becomes one card.
const merged = (events) => JSON.parse(run(`JSON.stringify(coalesce(${JSON.stringify(events)}))`));
const one = {group: 'items', metric: 'needs', delta: 1, plural: 'new items', title: 'Only one', sub: 'New action · Harbor', item: 'i'};
assert.equal(merged([one]).title, 'Only one');
assert.equal(merged([one]).item, 'i'); // a single card opens its item
let burst = merged([one, one, {group: 'agents', metric: 'agents', delta: 1, plural: 'agents started'}]);
assert.equal(burst.title, '3 updates');
assert.equal(burst.sub, '2 new items · 1 agent started');
assert.equal(burst.delta, 2);
assert.equal(burst.item, undefined); // several things: it opens the list
assert.equal(merged([one, {...one, delta: -1, plural: 'items cleared'}]).sub, '1 new item · 1 item cleared');
assert.equal(merged([{...one, plural: 'failed replies'}, one]).sub, '1 failed reply · 1 new item');

// The curve is a real spring: it starts at rest, overshoots and settles.
const curve = run("springCurve({stiffness: 190, damping: 17})");
const points = curve.slice(7, -1).split(', ').map(Number);
assert.equal(points[0], 0);
assert.equal(points.at(-1), 1);
assert.ok(Math.max(...points) > 1.05 && Math.max(...points) < 1.3);

// The open box sits beside the pill, which stays put so answered items can fly home to it.
const js = (code) => JSON.parse(run(`JSON.stringify(${code})`));
const capsule = {width: 38, height: 102}, card = {width: 308, height: 62}, listBox = {width: 380, height: 520};
const restAt = (dock, size = capsule) => rect(dock, size);
const boxAt = (dock, size = listBox) => js(`boxRect('${dock}', ${JSON.stringify(restAt(dock, ['top', 'bottom'].includes(dock) ? {width: 102, height: 38} : capsule))}, ${JSON.stringify(size)}, ${JSON.stringify(area)})`);
assert.deepEqual(boxAt('top-left'), {x: 56, y: 35, width: 380, height: 520}); // right of the pill, tops aligned
assert.deepEqual(boxAt('top-right'), {x: 1004, y: 35, width: 380, height: 520}); // left of it
assert.deepEqual(boxAt('bottom-left'), {x: 56, y: 301, width: 380, height: 520}); // bottoms aligned
assert.equal(boxAt('left').y + 260, restAt('left').y + 51); // centered on a side pill
assert.deepEqual(boxAt('bottom'), {x: 530, y: 255, width: 380, height: 520}); // above a pill along the bottom
assert.deepEqual(boxAt('top'), {x: 530, y: 81, width: 380, height: 520}); // below one along the top
assert.deepEqual(boxAt('top-left', {width: 480, height: 5000}), {x: 56, y: 35, width: 480, height: 786}); // never off the screen
// The window is the whole display whatever the dock, so a drop never moves or resizes it:
// the pill, its card and every box are drawn in it at their display positions.
const inside = (outer, inner) => inner.x >= outer.x && inner.y >= outer.y && inner.x + inner.width <= outer.x + outer.width && inner.y + inner.height <= outer.y + outer.height;
const screenAt = {x: 0, y: 0, width: 1440, height: 825}, room = js(`roomOf(${JSON.stringify(screenAt)})`);
assert.deepEqual(room, {x: 0, y: 0, width: 1440, height: 825});
for (const dock of ['top-left', 'top', 'top-right', 'left', 'right', 'bottom-left', 'bottom', 'bottom-right']) {
  const lying = ['top', 'bottom'].includes(dock), rest = restAt(dock, lying ? {width: 102, height: 38} : capsule);
  assert.ok(inside(room, rest), `${dock}: the pill`);
  assert.deepEqual(js(`localRect(${JSON.stringify(rest)}, roomOf(${JSON.stringify(screenAt)}))`), rest, `${dock}: drawn where it is on screen`);
  assert.ok(inside(room, rect(dock, card)), `${dock}: the notification card`);
  for (const size of [listBox, {width: 480, height: 300}, {width: 480, height: 760}, {width: 380, height: 840}]) {
    assert.ok(inside(room, js(`boxRect('${dock}', ${JSON.stringify(rest)}, ${JSON.stringify(size)}, ${JSON.stringify(area)})`)), `${dock}: a ${size.width}x${size.height} box`);
  }
  // Where the ghost lands is where the pill is drawn, in the shape that dock gives it.
  const ghost = js(`dockRects(${JSON.stringify(capsule)}, ${JSON.stringify(area)})['${dock}']`);
  assert.deepEqual(ghost, rest, `${dock}: the ghost lands on the pill`);
}
assert.deepEqual(js('localRect({x: 50, y: 60, width: 5, height: 6}, {x: 40, y: 30})'), {x: 10, y: 30, width: 5, height: 6});

// How the panel moves: Fold springs open and closed, items are a deck, an answer flies home.
const motion = (...args) => run(`motionKind(${args.map((a) => JSON.stringify(a)).join(', ')})`);
assert.equal(motion('pill', 'list', null, null), 'open');
assert.equal(motion('pill', 'item', null, 'a'), 'open'); // a shortcut straight to an item still folds open
assert.equal(motion('item', 'pill', 'a', 'a'), 'close');
assert.equal(motion('list', 'item', null, 'a'), 'deal');
assert.equal(motion('item', 'list', 'a', null), 'gather');
assert.equal(motion('item', 'item', 'a', 'b', ['a', 'b', 'c']), 'next');
assert.equal(motion('item', 'item', 'c', 'b', ['a', 'b', 'c']), 'prev');
assert.equal(motion('item', 'item', 'a', 'b', ['a', 'b'], true), 'answered');
assert.equal(motion('item', 'item', 'gone', 'b', ['b']), 'next'); // the old one left the list
assert.equal(motion('item', 'item', 'a', 'a'), 'none');
assert.equal(motion('list', 'list', null, null), 'none');
assert.equal(motion('list', 'settings', null, null), 'none');

// Spotlight: the label opens away from the edge, and the circle nearest the cursor along the pill wins.
assert.equal(run("spotSide('top-left')"), 'right');
assert.equal(run("spotSide('right')"), 'left');
assert.equal(run("spotSide('bottom')"), 'above');
assert.equal(run("spotSide('top')"), 'below');
const centers = [{x: 19, y: 19}, {x: 19, y: 51}, {x: 19, y: 83}];
assert.equal(run(`nearestCircle(${JSON.stringify(centers)}, 19, 49, true)`), 1);
assert.equal(run(`nearestCircle(${JSON.stringify(centers)}, 300, 90, true)`), 2); // off to the side still picks by height
assert.equal(run(`nearestCircle(${JSON.stringify(centers.map((c) => ({x: c.y, y: c.x})))}, 80, 19, false)`), 2);
assert.equal(run('nearestCircle([], 1, 1, true)'), -1);
// The needs count holds while an answered card is still flying home.
run('flying = 1');
assert.equal(run('METRICS.needs.read([{}, {}]).value'), 3);
assert.equal(run('METRICS.needs.read([{}, {}]).tag'), '3 need you');
run('flying = 0');

// Shortcuts: the same key opens and closes, and what you set survives.
const shortcut = (saved) => JSON.parse(run(`JSON.stringify(cleanShortcuts(${JSON.stringify(saved ?? null)}))`));
assert.equal(shortcut().toggle.label, '⌃⌥H');
assert.equal(shortcut().customize, null); // nothing bound unless you ask
const mine = {keyCode: 49, modifiers: 2048, label: '⌥Space'};
assert.deepEqual(shortcut({toggle: mine}).toggle, mine);
assert.equal(shortcut({toggle: null}).toggle, null); // cleared stays cleared
assert.equal(shortcut({toggle: {keyCode: 'H'}}).toggle.label, '⌃⌥H'); // a broken value falls back
assert.ok(!('gone' in shortcut({gone: mine})));
assert.deepEqual(JSON.parse(run(`JSON.stringify(hotkeyList({toggle: ${JSON.stringify(mine)}, next: null}))`)), [{action: 'toggle', keyCode: 49, modifiers: 2048}]);
assert.equal(new Set(Object.values(shortcut()).filter(Boolean).map((k) => `${k.keyCode}:${k.modifiers}`)).size, 4); // no two defaults collide
assert.deepEqual(JSON.parse(run('JSON.stringify(cleanNotify({agents: false}))')), {stuck: true, items: true, agents: false, usage: true});
html = run('shortcutsHTML()');
assert.ok(html.includes('Open or minimize Needs you') && html.includes('⌃⌥H') && html.includes('data-record="toggle"') && html.includes('>Add<'));
run("recording = 'toggle'; refused = ['next']");
html = run('shortcutsHTML()');
assert.ok(html.includes('Press keys…') && html.includes('Another app is using this'));
run("recording = null; refused = []");
html = run('notifyHTML()');
assert.ok(html.includes('data-notify="agents"') && html.includes('aria-checked="true"'));
assert.ok(run('settingsHTML(attention())').includes('Keyboard shortcuts'));

// Which surface shows, and where you go after acting.
assert.equal(run('surfaceMode(false, null)'), 'pill');
assert.equal(run('surfaceMode(true, null)'), 'list');
assert.equal(run("surfaceMode(true, 'x')"), 'item');
assert.equal(run("afterAct('next', 'n2', true)"), 'n2');
assert.equal(run("afterAct('next', null, true)"), null); // the last one returns to the list
assert.equal(run("afterAct('list', 'n2', true)"), null);
assert.equal(run("afterAct('stay', 'n2', true)"), undefined);
assert.equal(run("afterAct('stay', 'n2', false)"), 'n2'); // done or skipped has nothing left to show

// Every lab axis drives something real, and its starting point is what ships.
const labContext = vm.createContext({});
const labSource = fs.readFileSync('hq/static/float-lab.js', 'utf8');
vm.runInContext(labSource.slice(0, labSource.indexOf('function bootLab()')), labContext);
const lab = (code) => vm.runInContext(code, labContext);
const feelNow = JSON.parse(run('JSON.stringify(feel)'));
assert.deepEqual(JSON.parse(lab('JSON.stringify(defaults())')), feelNow);
for (const axis of JSON.parse(lab('JSON.stringify(AXES)'))) {
  assert.equal(axis.options.filter((o) => o.default).length, 1, axis.id);
  assert.equal(new Set(axis.options.map((o) => o.value)).size, axis.options.length, axis.id);
  assert.ok(axis.options.every((o) => o.label && o.why), axis.id);
}
for (const preset of JSON.parse(lab('JSON.stringify(PRESETS)'))) {
  for (const [key, value] of Object.entries(preset.set)) {
    const axis = JSON.parse(lab('JSON.stringify(AXES)')).find((a) => a.id === key);
    assert.ok(axis?.options.some((o) => o.value === value), `${preset.name}: ${key}`);
  }
}
const picks = lab(`picksText({...defaults(), glide: 180, show: 3}, {glide: 'feels right', layout: '  '}, ' more contrast ')`);
assert.ok(picks.includes('How long does it glide into place? 180 ms (changed). Note: feels right'));
assert.ok(picks.includes('Which way does the pill run? Depends where it sits\n'));
assert.ok(picks.endsWith('Overall: more contrast'));
assert.ok(!picks.includes('how many items')); // sample size is not a decision

// The panel loads style.css too, so the dashboard must never style, on its own, a class the
// pill puts on its circles. A bare .wide once stretched a 3-digit count into a blob.
const dashboardCss = fs.readFileSync('hq/static/style.css', 'utf8');
for (const name of ['wide', 'loud', 'live', 'low', 'none', 'gauge', 'stale', 'dim', 'hot', 'noting', 'instant', 'flying']) {
  assert.ok(!new RegExp(`(^|[\\s,{}>+~(])\\.${name}(?![\\w-])`).test(dashboardCss), `style.css styles .${name}, which the pill also uses`);
}
// The panel's window covers the whole display, so its rows stay narrow without a width query.
const panelCss = fs.readFileSync('hq/static/float.css', 'utf8');
assert.ok(/:where\(html\.float\) \.row \{[^}]*flex-direction: column/.test(panelCss));

// A decision you replied to is not done until its agent hears it and acts.
const day = 86400, follow = (stage, extra = {}) => ({stage, stuck: null, adoptable: false, replied: now - 600, heard: stage === 'acknowledged' ? now - 300 : null, ...extra});
const decided = (fields) => item({status: 'answered', bucket: 'pending', kind: 'question', answer: 'Annual', delivery: 'sent', session_id: 'claude:r', ...fields});
context.queue = {instance: 'server-follow', revision: 1, sessions: [
  {id: 'claude:r', name: 'Pricing chat', provider: 'claude-code', external_id: 'abc-123', cwd: "/Users/someone/it's here", activity: {state: 'offline'}},
  {id: 'codex:r', name: 'Codex chat', provider: 'codex', external_id: 'thread-9', cwd: '/x', activity: {state: 'idle'}},
  {id: 'other:r', name: 'Desktop chat', provider: 'claude-desktop', activity: {state: 'unknown'}}], items: [
  decided({id: 'f1', title: 'Pick the plan', followup: follow('delivering')}),
  decided({id: 'f2', title: 'Pick the name', delivery: 'received', followup: follow('acknowledged')}),
  decided({id: 'f3', title: 'Stopped one', project: 'hq', project_name: 'HQ', followup: follow('delivering', {stuck: 'stopped', replied: now - 2 * day})}),
  decided({id: 'f4', title: 'Unheard one', session_id: 'codex:r', followup: follow('delivering', {stuck: 'unheard', replied: now - 3 * day})}),
  decided({id: 'f5', title: 'Unclosed one', session_id: 'other:r', delivery: 'received', followup: follow('acknowledged', {stuck: 'unclosed', replied: now - 9 * day, heard: now - 8 * day})}),
  decided({id: 'f6', title: 'Skipped one', skipped: true, settle: 'skip', answer: 'Skipped: Dhruv decided not to do this.', followup: follow('delivering')}),
  decided({id: 'f7', title: 'Handed off', handoff: true, followup: follow('delivering', {adoptable: true})}),
  decided({id: 'f8', title: 'Adopted', adopted_from: {session_id: 'claude:old', name: 'Old <chat>', at: now - 60}, delivery: 'received', followup: follow('acknowledged')}),
  item({id: 'f9', title: 'Closed by me', status: 'done', bucket: 'done', answer: 'Annual', delivery: 'withdrawn', closed_by: 'you', agent_note: 'Did it by hand'}),
  decided({id: 'f10', title: 'Marked done', settle: 'done', answer: 'Done: The user marked this done.', followup: follow('delivering')}),
]};
run('applyState(queue); model = derive()');
const decidedPane = (id) => run(`detailHTML(model.items.find((i) => i.id === '${id}'))`);
// Nothing reads a skip, a done or an answer as finished: they wait with the agent.
for (const id of ['f1', 'f6', 'f10']) assert.equal(run(`model.items.find((i) => i.id === '${id}').lane`), 'agents', id);
assert.ok(!run('attention().map((i) => i.id)').includes('f6')); // nor do they need you again
html = decidedPane('f1');
assert.ok(html.includes('<li class="done"><span class="mark">') && html.includes('You decided') && html.includes('<li class="now"><span class="mark"></span><span class="h-label">Agent heard'));
assert.ok(html.includes('Waiting for the agent to hear it') && html.includes('data-close-self>Close it myself') && html.includes('data-handoff>Give to another agent'));
assert.ok(html.includes("cd &#39;/Users/someone/it&#39;\\&#39;&#39;s here&#39; &amp;&amp; claude --resume abc-123")); // quoted for the shell
assert.ok(!html.includes('data-done')); // Mark done is gone; Close it myself takes its place
assert.ok(decidedPane('f2').includes('The agent has it, not closed yet') && decidedPane('f2').includes('<li class="done"><span class="mark"><svg'));
assert.ok(decidedPane('f3').includes('<p class="d-follow stuck">The agent stopped before acting on it</p>'));
assert.ok(decidedPane('f4').includes('Not heard in 3 days') && decidedPane('f4').includes('codex resume thread-9'));
assert.ok(decidedPane('f5').includes('Heard 8 days ago, never closed') && !decidedPane('f5').includes('Resume the conversation')); // no command we can vouch for
assert.ok(decidedPane('f6').includes('<strong>You skipped this</strong></span>') && decidedPane('f6').includes('You skipped it'));
assert.ok(decidedPane('f10').includes('You marked it done') && !decidedPane('f10').includes('Done: The user'));
assert.ok(decidedPane('f7').includes('Offered to the next agent in NoClick') && !decidedPane('f7').includes('data-handoff'));
assert.ok(decidedPane('f8').includes('Taken over from Old &lt;chat&gt;'));
html = decidedPane('f9');
assert.ok(html.includes('Withdrawn: you closed it') && !html.includes('class="decided"'));
run("setDraft('f9', {tab: 'context'})");
assert.ok(decidedPane('f9').includes('Your note') && !decidedPane('f9').includes('From the agent'));
run("openMove = 'close'");
html = decidedPane('f1');
assert.ok(html.includes('id="close-form"') && html.includes('data-optional') && html.includes('placeholder="Close it myself, with a note for the record (optional)"'));
run('openMove = null');
// Rows say where a decision stands.
assert.equal(run("followupShort(model.items.find((i) => i.id === 'f6'))"), 'Skipped · Not heard yet');
assert.equal(run("followupShort(model.items.find((i) => i.id === 'f2'))"), 'Agent has it');
assert.equal(run("followupShort(model.items.find((i) => i.id === 'f3'))"), 'Not acted on');
assert.ok(run("metaHTML(model.items.find((i) => i.id === 'f3'))").includes('<span class="overdue">Not acted on</span>'));
assert.equal(run("dayWords(86400 + 5)"), 'a day');
assert.equal(run("resumeCommand({provider: 'claude-code', external_id: 'u1'})"), 'claude --resume u1');
assert.equal(run("resumeCommand({provider: 'claude-desktop', external_id: 'u1'})"), '');
// Not acted on: every stuck decision, oldest first, by project.
assert.deepEqual(Array.from(run('stuckItems().map((i) => i.id)')), ['f5', 'f4', 'f3']);
run("route = parseRoute('#/stuck')");
assert.equal(run('route.page'), 'stuck');
assert.equal(run("href({page: 'stuck'})"), '#/stuck');
const stuckPageHTML = run('stuckPage().body');
assert.ok(stuckPageHTML.indexOf('NoClick') < stuckPageHTML.indexOf('>HQ<') && stuckPageHTML.includes('Heard 8 days ago, never closed'));
assert.ok(run('sidebarHTML()').includes('Not acted on</span><span class="sb-count stuck">3</span>'));
run("route = parseRoute('#/')");
// The panel: a quiet line under Needs you opens the same list.
html = run('floatListHTML(attention(), true)');
assert.ok(html.includes('data-view="stuck">3 decisions not acted on</button>'));
assert.ok(run('stuckPanelHTML(true)').includes('data-item="f5"') && run('stuckPanelHTML(true)').includes('data-view="needs"'));
// The pill can show it, off by default, and says when a decision turns stuck.
assert.ok(!run('DEFAULT_CIRCLES').includes('stuck'));
assert.equal(run('METRICS.stuck.read([]).value'), 3);
assert.ok(run('settingsHTML(attention())').includes('data-circle="stuck"'));
const turned = JSON.parse(run("JSON.stringify(stuckChanges([model.items.find((i) => i.id === 'f5')], stuckItems()))"));
assert.deepEqual(turned.map((e) => [e.item, e.group, e.title]), [['f4', 'stuck', 'Not heard in a day: Unheard one'], ['f3', 'stuck', 'An agent stopped before acting on: Stopped one']]);
assert.equal(run("cleanNotify({}).stuck"), true);
assert.ok(run("describeRemoved({title: 'x', project_name: 'P'}, model.items.find((i) => i.id === 'f6'))").sub.startsWith('Skipped'));
// Skip and Mark done say they still have to reach the agent.
assert.ok(source.includes("'Skipped · tells the agent'") && source.includes("'Marked done · tells the agent'") && source.includes('items/${id}/done'));

console.log('UI lanes, grouping, conversation isolation, presence, stale-update, escaping, draft, floating panel and lab checks passed');
