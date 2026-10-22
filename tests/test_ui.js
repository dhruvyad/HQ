// Pure rendering, state and draft checks for the dashboard; no browser or page execution.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const elements = {'meta[name="hq-token"]': {content: 'test'}};
const context = vm.createContext({URL, URLSearchParams, document: {
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
assert.ok(html.indexOf('Working') < html.indexOf('data-session="codex:a"'));
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
assert.ok(html.includes('More context') && html.includes('Original paragraph'));
assert.ok(html.includes('Looks right') && !html.includes('id="reply"')); // the reply box waits for Something was off
assert.ok(html.includes('Hard to read?'));
run("askOpen.add('r'); openMove = 'off'");
html = run("detailHTML(model.items.find(i => i.id === 'r'))");
assert.ok(html.includes('Unsent question'));
assert.ok(html.includes("Didn&#39;t see: Second")); // what wasn't ticked, ready to send
html = run("detailHTML(model.items.find(i => i.id === 'a2'))");
assert.ok(!html.includes('Hard to read?')); // agents' own work isn't sent back to them
console.log('UI lanes, grouping, conversation isolation, presence, stale-update, escaping and draft checks passed');
