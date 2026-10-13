// Pure rendering/draft unit checks; no browser access or page execution.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = {};
const fields = [
  {id: 'answer', name: 'text', value: 'Unsent decision'},
  {id: 'agent-question', value: 'Unsent question', selectionStart: 3, selectionEnd: 8,
    focus() {this.focused = true;}, setSelectionRange(a,b) {this.selectionStart=a; this.selectionEnd=b;}},
  {id: 'move-owner', name: 'owner', value: 'external', onchange() {this.changed = true;}},
  {name: 'waiting_on', value: 'Vendor'}
];
for (const el of fields) el.dataset = {dirty: 'true'};
const details = {open: true};
const panel = {scrollTop: 150};
const choice = {dataset: {choice: 'Unsent decision'}, classList: {toggle(name, selected) {this.selected=selected;}}};
elements['meta[name="hq-token"]'] = {content: 'test'};
elements['#detail-dialog'] = panel;
elements['#detail-content details'] = details;
elements['#detail-content'] = {querySelectorAll(selector) {return selector === '[data-choice]' ? [choice] : fields;}};
const context = vm.createContext({URL, document: {
  querySelector(selector) {return elements[selector] || fields.find(el => '#'+el.id === selector);},
  getElementById(id) {return fields.find(el => el.id === id);}, activeElement: fields[1]
}});
const source = fs.readFileSync('hq/static/app.js', 'utf8');
vm.runInContext(source.slice(0, source.indexOf('function closeSidebar()')), context);
const run = code => vm.runInContext(code, context);
const question = {id: 'q', author: 'human', text: '<script>bad()</script>', state: 'pending', answered: false};
const item = {status: 'open', owner: 'human', kind: 'question', asking: true, bucket: 'waiting', comments: [question]};
context.item = item;
let html = run('conversationHTML(item)');
assert.ok(html.includes('Ask agent'));
assert.ok(html.includes('Queued for agent'));
assert.ok(html.includes('&lt;script&gt;'));
assert.ok(!html.includes('<script>'));
assert.ok(html.includes('Cancel question'));
assert.equal(run("inView(item, 'needs')"), false);
assert.equal(run("inView(item, 'pending')"), true);
assert.equal(run('itemLabel(item)'), 'Waiting on agent');
item.comments.push({id: 'a', author: 'agent', parent_id: 'q', text: '**Short answer** [bad](https://user:secret@example.com)'});
question.answered = true;
item.asking = false;
html = run('conversationHTML(item)');
assert.ok(html.includes('<strong>Short answer</strong>'));
assert.ok(!html.includes('href="https://user:secret'));
assert.ok(!html.includes('Cancel question'));
assert.equal(run("inView(item, 'needs')"), true);
assert.equal(run("inView(item, 'pending')"), false);
question.answered = false;
question.state = 'failed'; question.error = '<b>Connection lost</b>';
html = run('conversationHTML(item)');
assert.ok(html.includes('Retry delivery'));
assert.ok(html.includes('&lt;b&gt;Connection lost&lt;/b&gt;'));
item.status = 'done';
assert.ok(!run('conversationHTML(item)').includes('id="agent-question-form"'));
// Incoming replies must not erase unsent questions, decisions, move fields or cursor position.
fields.push({id: 'unchanged', name: 'untouched', value: 'Old server value', dataset: {}});
const draft = run('detailDraft()');
for (const el of fields) el.value = '';
details.open = false; panel.scrollTop = 0;
context.draft = draft;
run('restoreDetailDraft(draft)');
assert.equal(fields[0].value, 'Unsent decision');
assert.equal(fields[1].value, 'Unsent question');
assert.equal(fields[1].selectionStart, 3);
assert.equal(fields[1].selectionEnd, 8);
assert.ok(fields[1].focused);
assert.equal(fields[2].value, 'external');
assert.ok(fields[2].changed);
assert.equal(fields[3].value, 'Vendor');
assert.equal(fields[4].value, ''); // Untouched fields accept the refreshed server value.
assert.ok(choice.classList.selected);
assert.ok(details.open);
assert.equal(panel.scrollTop, 150);
// Similar titles, harnesses, and tags must never combine separate sessions.
context.queue = {
  sessions: [
    {id: 'codex:a', name: 'Shared <project>', provider: 'codex'},
    {id: 'codex:b', name: 'Shared <project>', provider: 'codex'},
    {id: 'claude:c', name: 'Shared <project>', provider: 'claude-code'},
    {id: 'codex:done', name: 'Completed conversation', provider: 'codex'},
    {id: 'claude:empty', name: 'Discovered empty chat', provider: 'claude-code'}
  ],
  items: [
    {id: 'a1', session_id: 'codex:a', session_name: 'Shared <project>', provider: 'codex', title: 'Human action', owner: 'human', kind: 'todo', status: 'open', bucket: 'needs', tags: ['shared'], updated: 20},
    {id: 'a2', session_id: 'codex:a', session_name: 'Shared <project>', provider: 'codex', title: 'Agent action', owner: 'agent', kind: 'todo', status: 'open', bucket: 'pending', tags: ['other'], updated: 21},
    {id: 'b1', session_id: 'codex:b', session_name: 'Shared <project>', provider: 'codex', title: 'Saved answer', owner: 'human', kind: 'question', status: 'answered', bucket: 'waiting', delivery: 'pending', tags: ['shared'], updated: 19},
    {id: 'c1', session_id: 'claude:c', session_name: 'Shared <project>', provider: 'claude-code', title: 'Outside dependency', owner: 'external', kind: 'blocker', waiting_on: 'Vendor', status: 'open', bucket: 'waiting', tags: ['shared'], updated: 18},
    {id: 'd1', session_id: 'codex:done', session_name: 'Completed conversation', provider: 'codex', title: 'Finished work', owner: 'agent', kind: 'todo', status: 'done', bucket: 'done', tags: [], updated: 30}
  ]
};
run("state = queue; filter = {view: 'all', sessionId: null, search: ''}");
assert.deepEqual(Array.from(run('conversationEntries().map(s => s.id)')), ['codex:a', 'codex:b', 'claude:c']);
assert.equal(run('conversationEntries()[0].open'), 2);
assert.equal(run('scoped().length'), 5);
run("filter.sessionId = 'codex:a'");
assert.deepEqual(Array.from(run('scoped().map(i => i.id)')), ['a1', 'a2']);
assert.equal(run("scoped().filter(i => inView(i, 'needs')).length"), 1);
assert.equal(run("scoped().filter(i => inView(i, 'pending')).length"), 1);
html = run('conversationNavHTML(conversationEntries()[0])');
assert.ok(html.includes('data-session="codex:a"'));
assert.ok(html.includes('Shared &lt;project&gt;'));
assert.ok(html.includes('aria-pressed="true"'));
assert.ok(html.includes('Codex'));
assert.ok(!run('itemHTML(queue.items[0])').includes('class="tag"'));
assert.ok(run('tagHTML(queue.items[0].tags)').includes('shared')); // Context remains available in details.

// The selected conversation stays put when an incoming update completes its last item.
for (const i of context.queue.items.filter(i => i.session_id === 'codex:a')) {i.status = 'done'; i.bucket = 'done';}
assert.equal(run("conversationEntries().find(s => s.id === 'codex:a').open"), 0);
run('filter.sessionId = null');
assert.ok(!Array.from(run('conversationEntries().map(s => s.id)')).includes('codex:a'));
run("filter.view = 'done'");
assert.deepEqual(Array.from(run('conversationEntries().map(s => s.id)')), ['codex:done', 'codex:a']);
assert.ok(!Array.from(run('conversationEntries().map(s => s.id)')).includes('claude:empty'));

// Rendered tab counts and headings follow the exact selected session, including saved replies.
const tabs = ['all', 'needs', 'pending', 'waiting', 'later', 'done'].map(view => ({
  dataset: {view}, classList: {toggle() {}}, hasAttribute() {return true;}, setAttribute() {}
}));
context.document.querySelectorAll = () => tabs;
for (const selector of ['#needs-count', '#pending-count', '#waiting-count', '#later-count', '#done-count', '#all-items-count', '#scope', '#all-count', '#all-conversations', '#conversations-nav', '#items']) {
  elements[selector] = {textContent: '', innerHTML: '', classList: {toggle() {}}, setAttribute() {}, querySelectorAll() {return [];}};
}
run("filter.view = 'all'; filter.sessionId = 'codex:b'; render()");
assert.equal(elements['#scope'].textContent, 'Shared <project>');
assert.equal(elements['#all-items-count'].textContent, 1);
assert.equal(elements['#needs-count'].textContent, 0);
assert.equal(elements['#pending-count'].textContent, 1);
assert.equal(elements['#waiting-count'].textContent, 1);
assert.ok(elements['#items'].innerHTML.includes('Saved answer'));
assert.ok(!elements['#items'].innerHTML.includes('Outside dependency'));
run("filter.sessionId = 'codex:a'; render()");
assert.equal(elements['#all-items-count'].textContent, 0);
assert.equal(elements['#done-count'].textContent, 2);
assert.ok(elements['#conversations-nav'].innerHTML.includes('data-session="codex:a"'));
run("filter.sessionId = 'missing'; render()");
assert.equal(run('filter.sessionId'), null);
assert.equal(elements['#scope'].textContent, 'All conversations');
assert.equal(elements['#all-items-count'].textContent, 2);
run("filter.view = 'done'; render()");
assert.equal(elements['#all-count'].textContent, 3);
assert.ok(elements['#conversations-nav'].innerHTML.includes('Completed conversation'));
assert.ok(!elements['#conversations-nav'].innerHTML.includes('Discovered empty chat'));
console.log('UI conversation isolation, navigation, ownership, rendering, and draft checks passed');
