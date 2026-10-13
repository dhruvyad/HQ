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
console.log('UI rendering, pending ownership, and draft preservation checks passed');
