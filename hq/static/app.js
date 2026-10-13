const $ = (selector) => document.querySelector(selector);
const token = $('meta[name="hq-token"]').content;
let state = {sessions: [], items: []}, filter = {view: 'all', tag: null, search: ''};
let selectedId = null, toastTimer, loading = null, snapshotSignature = '';
const providers = {codex: ['Cx', 'Codex'], 'claude-code': ['Cl', 'Claude Code'], 'claude-desktop': ['Cl', 'Claude Desktop'], chatgpt: ['G', 'ChatGPT'], other: ['A', 'Other']};
const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const tagHTML = (tags) => tags.map(t => `<span class="tag">${escapeHTML(t)}</span>`).join('');
const harnessHTML = (provider) => `<span class="harness-badge">${(providers[provider] || providers.other)[1]}</span>`;
function safeURL(value) {try {const url = new URL(value); return url.protocol === 'https:' && !url.username && !url.password;} catch {return false;}}
function inlineHTML(value) {
  const text = String(value ?? ''), pattern = /`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\((https:\/\/[^\s)]+)\)/g;
  let out = '', end = 0;
  for (const match of text.matchAll(pattern)) {
    out += escapeHTML(text.slice(end, match.index));
    out += match[1] ? `<code>${escapeHTML(match[1])}</code>` : match[2] ? `<strong>${escapeHTML(match[2])}</strong>` : safeURL(match[4]) ? `<a href="${escapeHTML(match[4])}" target="_blank" rel="noreferrer">${escapeHTML(match[3])}</a>` : escapeHTML(match[0]);
    end = match.index + match[0].length;
  }
  return out + escapeHTML(text.slice(end));
}
function readableHTML(item) {
  return `${item.unlocks ? `<div class="detail-unlocks"><span>Unlocks</span><p>${inlineHTML(item.unlocks)}</p></div>` : ''}${item.recommendation ? `<div class="recommendation"><span>Agent recommends</span><p>${inlineHTML(item.recommendation)}</p></div>` : ''}${item.steps?.length ? `<ol class="detail-steps">${item.steps.map(step => `<li>${inlineHTML(step)}</li>`).join('')}</ol>` : ''}${item.link && safeURL(item.link.url) ? `<a class="button primary action-link" href="${escapeHTML(item.link.url)}" target="_blank" rel="noreferrer">${escapeHTML(item.link.label)} ↗</a>` : ''}${(item.paste || []).map((block,index) => `<section class="copy-block"><div><span>${escapeHTML(block.label)}</span><button class="button" data-copy="${index}">Copy</button></div><pre class="${block.mono ? 'mono' : ''}">${escapeHTML(block.text)}</pre></section>`).join('')}${item.agent_note ? `<div class="agent-note"><span>From agent</span><p>${escapeHTML(item.agent_note)}</p></div>` : ''}`;
}
const iconPaths = {arrow: '<path d="M5 12h14M15 8l4 4-4 4"/>', pause: '<path d="M9 6v12M15 6v12"/>', question: '<path d="M9 8a3 3 0 016 0c0 2-3 2-3 4M12 16h.01"/>', check: '<path d="M5 12l4 4L19 6"/>', chevron: '<path d="M9 5l7 7-7 7"/>'};
const icon = (name) => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${iconPaths[name] || iconPaths.arrow}</svg>`;
function toast(message) {$('#toast').textContent = message; $('#toast').classList.add('visible'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').classList.remove('visible'), 4000);}
async function api(path, body) {const response = await fetch(`/api/${path}`, {method: body === undefined ? 'GET' : 'POST', headers: {'X-HQ-Token': token, 'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)}); const data = await response.json(); if (!response.ok) throw new Error(data.error || 'HQ could not save this change'); return data;}
function scoped() {return state.items.filter(i => !filter.tag || i.tags.includes(filter.tag));}
function inView(item, view) {
  if (view === 'all') return item.status !== 'done';
  if (view === 'needs') return item.status === 'open' && item.owner === 'human' && !item.asking;
  if (view === 'pending') return item.status !== 'done' && (item.owner !== 'human' || item.status === 'answered' || item.asking);
  return item.bucket === view;
}
function itemLabel(item) {
  if (item.asking) return 'Waiting on agent';
  if (item.status === 'answered') return `${deliveryText(item)} · agent next`;
  if (item.bucket === 'done') return 'Done';
  if (item.bucket === 'later') return `Later · ${item.defer_until}`;
  if (item.owner === 'external') return `Waiting on ${item.waiting_on}`;
  if (item.owner === 'agent') return 'With agent';
  return item.kind === 'question' ? 'Your decision' : item.kind === 'blocker' ? 'Needs you · blocker' : 'Your action';
}
function itemHTML(i) {
  const marker = i.status !== 'open' ? 'check' : i.bucket === 'waiting' || i.bucket === 'later' ? 'pause' : i.kind === 'question' ? 'question' : 'arrow';
  return `<button class="item" data-item="${escapeHTML(i.id)}"><span class="item-icon ${i.bucket === 'needs' && i.kind === 'blocker' ? 'blocker' : ''}">${icon(marker)}</span><span class="item-body"><span class="item-title">${escapeHTML(i.title)}</span><span class="item-context"><span class="item-status ${i.bucket === 'needs' ? 'needs' : ''}">${escapeHTML(itemLabel(i))}</span>${i.minutes ? `<span class="effort">${i.minutes} min</span>` : ''}${tagHTML(i.tags)}</span></span><span class="item-arrow">${icon('chevron')}</span></button>`;
}
function itemSummary(items) {return [['needs','for you'],['pending','with agent'],['waiting','waiting'],['later','later']].map(([bucket,label]) => {const n = items.filter(i => i.bucket === bucket).length; return n ? `${n} ${label}` : '';}).filter(Boolean).join(' · ');}
function groupHTML(items) {
  const groups = new Map();
  items.forEach(i => {if (!groups.has(i.session_id)) groups.set(i.session_id, []); groups.get(i.session_id).push(i);});
  return [...groups.values()].map(group => {
    const first = group[0], summary = itemSummary(group);
    return `<section class="conversation-group" aria-label="${escapeHTML(first.session_name)}"><div class="conversation-header"><div><strong>${escapeHTML(first.session_name)}</strong>${harnessHTML(first.provider)}</div><span>${escapeHTML(summary || `${group.length} ${group.length === 1 ? 'item' : 'items'}`)}</span></div>${group.map(itemHTML).join('')}</section>`;
  }).join('');
}
function deliveryText(item) {return ({pending: 'Queued', sending: 'Sending…', sent: 'Sent · unconfirmed', received: 'Received', failed: 'Delivery failed'})[item.delivery] || 'Saved';}
function setView(view) {filter.view = view; render();}
function render() {
  const items = scoped();
  ['needs','pending','waiting','later','done'].forEach(view => $(`#${view}-count`).textContent = items.filter(i => inView(i, view)).length);
  $('#all-items-count').textContent = items.filter(i => inView(i, 'all')).length;
  document.querySelectorAll('[data-view]').forEach(el => {const active = el.dataset.view === filter.view; el.classList.toggle('active', active); if (el.hasAttribute('role')) el.setAttribute('aria-selected', active);});
  $('#scope').textContent = filter.tag || 'All workstreams';
  $('#all-count').textContent = state.items.filter(i => i.status !== 'done').length;
  $('#all-work').classList.toggle('active', !filter.tag);
  const tags = [...new Set(state.items.flatMap(i => i.tags))].sort();
  $('#tags-nav').innerHTML = tags.length ? tags.map(t => `<button class="nav-item ${filter.tag === t ? 'active' : ''}" data-tag="${escapeHTML(t)}"><span class="tag-dot"></span>${escapeHTML(t)}<span class="nav-number">${state.items.filter(i => i.tags.includes(t) && i.status !== 'done').length}</span></button>`).join('') : '<div class="sidebar-empty">No tags</div>';
  const priority = {needs: 0, pending: 1, waiting: 2, later: 3, done: 4};
  const visible = items.filter(i => inView(i, filter.view) && `${i.title} ${i.detail} ${i.waiting_on} ${i.tags.join(' ')} ${i.session_name}`.toLowerCase().includes(filter.search)).sort((a,b) => priority[a.bucket] - priority[b.bucket] || a.rank-b.rank || b.created-a.created);
  $('#items').innerHTML = visible.length ? groupHTML(visible) : `<div class="empty"><div class="empty-symbol">${icon(state.items.length ? 'check' : 'arrow')}</div><h3>${filter.search ? 'No matches' : state.items.length ? ({all:'No open items',needs:'No human tasks pending',pending:'No agent work pending',waiting:'Nothing waiting',later:'Nothing for later',done:'No completed items'})[filter.view] : 'Connect your first agent'}</h3>${!state.items.length ? '<button class="button" id="empty-connect">Connect an agent</button>' : ''}</div>`;
  $('#tags-nav').querySelectorAll('[data-tag]').forEach(el => el.onclick = () => {filter.tag = filter.tag === el.dataset.tag ? null : el.dataset.tag; render(); closeSidebar();});
  $('#items').querySelectorAll('[data-item]').forEach(el => el.onclick = () => openDetail(el.dataset.item));
  if ($('#empty-connect')) $('#empty-connect').onclick = () => $('#connect-dialog').showModal();
}
function conversationHTML(item) {
  const messages = item.comments || [];
  const questionHTML = question => {
    const answer = messages.find(c => c.parent_id === question.id);
    const status = question.answered ? '' : ({pending: 'Queued for agent', sending: 'Sending…', sent: 'Sent · unconfirmed', received: 'Waiting for reply', failed: 'Delivery failed', cancelled: 'Cancelled'})[question.state] || '';
    return `<div class="thread-question"><div class="thread-message"><div class="thread-author">You${status ? `<span>${escapeHTML(status)}</span>` : ''}</div><p>${escapeHTML(question.text)}</p>${question.error ? `<p class="form-error">${escapeHTML(question.error)}</p>` : ''}${!question.answered && question.state !== 'cancelled' ? `<div class="thread-actions">${question.state === 'failed' ? `<button class="button" data-comment-action="retry" data-comment-id="${escapeHTML(question.id)}">Retry delivery</button>` : ''}<button class="button" data-comment-action="cancel" data-comment-id="${escapeHTML(question.id)}">Cancel question</button></div>` : ''}</div>${answer ? `<div class="thread-message agent-message"><div class="thread-author">Agent</div><p>${inlineHTML(answer.text)}</p></div>` : ''}</div>`;
  };
  return `<section class="item-conversation" aria-label="Clarification"><h3>Ask agent</h3><div class="comment-thread" aria-live="polite">${messages.filter(c => c.author === 'human').map(questionHTML).join('')}</div>${item.status !== 'done' ? `<form id="agent-question-form"><label class="sr-only" for="agent-question">Ask the agent about this item</label><textarea id="agent-question" rows="2" required maxlength="20000" placeholder="Ask about this item…"></textarea><div class="response-actions"><small>Saved here until the agent replies</small><button class="button" type="submit">Send question</button></div><p class="form-error" aria-live="polite"></p></form>` : ''}</section>`;
}
function detailDraft() {
  const fields = [...$('#detail-content').querySelectorAll('textarea, input, select')].filter(el => el.dataset.dirty === 'true').map(el => ({id: el.id, name: el.name, value: el.value}));
  const active = document.activeElement;
  return {fields, activeId: active?.id, start: active?.selectionStart, end: active?.selectionEnd,
    expanded: $('#detail-content details')?.open, scroll: $('#detail-dialog').scrollTop};
}
function restoreDetailDraft(draft) {
  for (const field of draft.fields) {
    const el = [...$('#detail-content').querySelectorAll('textarea, input, select')].find(el => field.id ? el.id === field.id : el.name === field.name);
    if (el) {el.value = field.value; el.dataset.dirty = 'true';}
  }
  if ($('#move-owner')) $('#move-owner').onchange();
  $('#detail-content').querySelectorAll('[data-choice]').forEach(el => el.classList.toggle('selected', el.dataset.choice === $('#answer')?.value));
  if ($('#detail-content details')) $('#detail-content details').open = draft.expanded;
  const active = draft.activeId && document.getElementById(draft.activeId);
  if (active) {active.focus({preventScroll: true}); if (typeof draft.start === 'number' && active.setSelectionRange && active.type !== 'date') active.setSelectionRange(draft.start, draft.end);}
  $('#detail-dialog').scrollTop = draft.scroll;
}
function openDetail(id) {
  selectedId = id;
  const item = state.items.find(i => i.id === id); if (!item) return;
  const session = state.sessions.find(s => s.id === item.session_id);
  const response = item.status === 'open' && item.owner !== 'agent';
  $('#detail-content').innerHTML = `<div class="eyebrow">${item.kind === 'blocker' ? 'BLOCKER' : item.kind === 'question' ? 'QUESTION' : 'TASK'}</div><h2>${escapeHTML(item.title)}</h2><div class="detail-meta">${harnessHTML(session?.provider || item.provider)}<span>${escapeHTML(item.session_name)}</span>${tagHTML(item.tags)}${item.minutes ? `<span>${item.minutes} min of yours</span>` : ''}</div><p class="detail-status">${escapeHTML(itemLabel(item))}</p>${item.detail ? `<div class="detail-context"><p>${inlineHTML(item.detail)}</p></div>` : ''}${readableHTML(item)}${item.answer !== null ? `<div class="detail-answer"><span>YOUR RESPONSE</span><p>${escapeHTML(item.answer)}</p></div><div id="delivery-status" class="delivery ${item.delivery === 'failed' ? 'error' : ''}">${escapeHTML(deliveryText(item))}${item.delivery_error ? `<br>${escapeHTML(item.delivery_error)}` : ''}</div>${item.delivery === 'failed' ? '<button id="retry-response" class="button">Retry delivery</button>' : ''}` : ''}${response ? `<form class="response-form" id="response-form">${item.options.length ? `<div class="choices">${item.options.map(raw => {const o = typeof raw === 'string' ? {label: raw} : raw; return `<button type="button" class="choice ${o.recommended ? 'recommended' : ''}" data-choice="${escapeHTML(o.label)}"><span>${escapeHTML(o.label)}${o.recommended ? '<small>Recommended</small>' : ''}</span>${o.note ? `<span class="choice-note">${escapeHTML(o.note)}</span>` : ''}</button>`;}).join('')}</div>` : ''}<label for="answer">${item.owner === 'external' ? 'What came back?' : item.kind === 'todo' ? 'Your update' : 'Your response'}</label><textarea id="answer" name="text" rows="4" required maxlength="20000" placeholder="Your reply…"></textarea><div class="response-actions"><small>${session && (session.connection === 'waiting' || session.connection === 'channel' || session.route === 'codex') ? 'Auto-delivery' : 'Queued for agent'}</small><button class="button primary" type="submit">${item.kind === 'todo' || item.owner === 'external' ? 'Send update' : 'Send reply'}</button></div><p class="form-error" aria-live="polite"></p></form>` : ''}${!item.asking && (item.status === 'open' || (item.status === 'answered' && item.delivery === 'received')) ? '<div class="response-actions"><button class="button" id="resolve-item">Mark completed ✓</button></div>' : ''}`;
  $('#detail-content').oninput = event => {if (event.target.matches('textarea, input, select')) event.target.dataset.dirty = 'true';};
  $('#detail-content').insertAdjacentHTML('beforeend', conversationHTML(item));
  if (item.status === 'open') {
    $('#detail-content').insertAdjacentHTML('beforeend', `<div class="item-moves">${item.bucket === 'later' ? '<button class="button" id="bring-forward">Bring forward</button>' : ''}${item.owner === 'external' ? '<button class="button" id="needs-me">Needs me now</button>' : ''}<details><summary>Move or defer</summary><form id="move-form"><label>Next step<select name="owner" id="move-owner">${[['human','Needs you'],['agent','With agent'],['external','Waiting on someone']].map(([value,label]) => `<option value="${value}" ${item.owner === value ? 'selected' : ''}>${label}</option>`).join('')}</select></label><label id="move-waiting-label" ${item.owner !== 'external' ? 'hidden' : ''}>Waiting on<input name="waiting_on" maxlength="200" value="${escapeHTML(item.waiting_on)}" ${item.owner === 'external' ? 'required' : ''}></label><label>Show later<input type="date" name="defer_until" value="${escapeHTML(item.defer_until)}"></label><button class="button" type="submit">Save</button><p class="form-error" aria-live="polite"></p></form></details></div>`);
    $('#move-owner').onchange = () => {const external = $('#move-owner').value === 'external'; $('#move-waiting-label').hidden = !external; $('#move-waiting-label input').required = external;};
    $('#move-form').onsubmit = async event => {event.preventDefault(); const button = event.target.querySelector('[type="submit"]'); button.disabled = true; try {await moveItem(id, Object.fromEntries(new FormData(event.target)));} catch (error) {event.target.querySelector('.form-error').textContent = error.message;} finally {button.disabled = false;}};
    if ($('#bring-forward')) $('#bring-forward').onclick = () => moveItem(id, {defer_until: ''}).catch(error => toast(error.message));
    if ($('#needs-me')) $('#needs-me').onclick = () => moveItem(id, {owner: 'human', defer_until: ''}).catch(error => toast(error.message));
  }
  $('#detail-content').querySelectorAll('[data-copy]').forEach(el => el.onclick = async () => {try {await navigator.clipboard.writeText(item.paste[Number(el.dataset.copy)].text); toast('Copied');} catch {toast('Copy unavailable; select the text to copy');}});
  if ($('#agent-question-form')) $('#agent-question-form').onsubmit = async event => {
    event.preventDefault(); const form = event.target, button = form.querySelector('[type="submit"]'), question = form.querySelector('textarea'); button.disabled = true;
    try {await api(`items/${id}/ask`, {text: question.value}); question.value = ''; await refresh(true); toast('Question saved');}
    catch (error) {form.querySelector('.form-error').textContent = error.message;} finally {button.disabled = false;}
  };
  $('#detail-content').querySelectorAll('[data-comment-action]').forEach(el => el.onclick = async () => {
    el.disabled = true;
    try {await api(`comments/${el.dataset.commentId}/${el.dataset.commentAction}`, {}); await refresh(true);}
    catch (error) {toast(error.message);} finally {el.disabled = false;}
  });
  if (item.source) $('#detail-content').insertAdjacentHTML('beforeend', `<p class="item-source">${escapeHTML(item.source)}</p>`);
  if (!$('#detail-dialog').open) $('#detail-dialog').showModal();
  $('#detail-content').querySelectorAll('[data-choice]').forEach(el => el.onclick = () => {$('#answer').value = el.dataset.choice; $('#answer').dataset.dirty = 'true'; $('#detail-content').querySelectorAll('.choice').forEach(c => c.classList.toggle('selected', c === el));});
  if ($('#response-form')) $('#response-form').onsubmit = async event => {event.preventDefault(); const button = event.target.querySelector('[type="submit"]'); button.disabled = true; try {await api(`items/${id}/reply`, {text: $('#answer').value}); await refresh(true); toast('Reply saved');} catch(error) {event.target.querySelector('.form-error').textContent = error.message;} finally {button.disabled = false;}};
  if ($('#resolve-item')) $('#resolve-item').onclick = async () => {try {await api(`items/${id}/resolve`, {}); $('#detail-dialog').close(); await refresh(true); toast('Item completed');} catch(error) {toast(error.message);}};
  if ($('#retry-response')) $('#retry-response').onclick = async () => {try {await api(`items/${id}/retry`, {}); await refresh(true);} catch(error) {toast(error.message);}};
}
async function moveItem(id, patch) {await api(`items/${id}/update`, patch); await refresh(true); toast('Item updated');}
function refresh(force = false) {
  if (loading) return force ? loading.then(() => refresh(true)) : loading;
  loading = refreshState().finally(() => {loading = null;});
  return loading;
}
async function refreshState() {
  try {
    const next = await api('state');
    const signature = JSON.stringify({items: next.items, sessions: next.sessions.map(s => [s.id, s.name, s.provider, s.connection, s.route])});
    const before = state.items.find(i => i.id === selectedId);
    const after = next.items.find(i => i.id === selectedId);
    const detailChanged = JSON.stringify(before) !== JSON.stringify(after);
    const draft = $('#detail-dialog').open && detailChanged ? detailDraft() : null;
    state = next;
    if (signature !== snapshotSignature) {
      snapshotSignature = signature;
      const focused = document.activeElement;
      const focusItem = focused?.dataset.item;
      render();
      if (focusItem) [...$('#items').querySelectorAll('[data-item]')].find(el => el.dataset.item === focusItem)?.focus({preventScroll: true});
      if (draft && after) {openDetail(selectedId); restoreDetailDraft(draft);}
    }
    $('#service-status').hidden = true;
  } catch(error) {$('#service-status').textContent = 'Offline'; $('#service-status').hidden = false;}
}
function closeSidebar() { $('.sidebar').classList.remove('visible'); $('#mobile-menu').setAttribute('aria-expanded', 'false'); }
document.querySelectorAll('[data-view]').forEach(el => el.onclick = () => setView(el.dataset.view));
$('#all-work').onclick = () => {filter.tag = null; render(); closeSidebar();};
$('#mobile-menu').onclick = () => {const visible = $('.sidebar').classList.toggle('visible'); $('#mobile-menu').setAttribute('aria-expanded', String(visible));};
document.addEventListener('click', event => {if ($('.sidebar').classList.contains('visible') && !event.target.closest('.sidebar') && !event.target.closest('#mobile-menu')) closeSidebar();});
document.addEventListener('keydown', event => {if (event.key === 'Escape') closeSidebar();});
$('#search').oninput = event => {filter.search = event.target.value.toLowerCase(); render();};
$('#connections').onclick = () => $('#connect-dialog').showModal();
$('#add-button').onclick = () => {$('#session-select').innerHTML = state.sessions.map(s => `<option value="${escapeHTML(s.id)}">${escapeHTML(s.name)}</option>`).join(''); if (!state.sessions.length) return $('#connect-dialog').showModal(); $('#add-dialog').showModal();};
$('#add-owner').onchange = () => {const external = $('#add-owner').value === 'external'; $('#add-waiting-label').hidden = !external; $('#add-waiting-label input').required = external;};
$('#add-form').onsubmit = async event => {event.preventDefault(); const form = event.target, fields = Object.fromEntries(new FormData(form)), button = form.querySelector('[type="submit"]'); fields.tags = fields.tags.split(',').map(t => t.trim()).filter(Boolean); button.disabled = true; try {await api('items', fields); filter.view = 'all'; $('#add-dialog').close(); form.reset(); $('#add-owner').onchange(); await refresh(true); toast('Item added');} catch(error) {form.querySelector('.form-error').textContent = error.message;} finally {button.disabled = false;}};
refresh(); setInterval(refresh, 2000);
