const $ = (selector) => document.querySelector(selector);
const token = $('meta[name="hq-token"]').content;
let state = {sessions: [], items: []}, filter = {view: 'needs', session: null, tag: null, harness: null, search: ''};
let selectedId = null, toastTimer, loading = false, showAllSessions = false, snapshotSignature = '';
const providers = {codex: ['Cx', 'Codex'], 'claude-code': ['Cl', 'Claude Code'], 'claude-desktop': ['Cl', 'Claude Desktop'], chatgpt: ['G', 'ChatGPT'], other: ['A', 'Other']};
const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const tagHTML = (tags) => tags.map(t => `<span class="tag">${escapeHTML(t)}</span>`).join('');
const harnessHTML = (provider) => `<span class="harness-badge">${(providers[provider] || providers.other)[1]}</span>`;
const iconPaths = {arrow: '<path d="M5 12h14M15 8l4 4-4 4"/>', pause: '<path d="M9 6v12M15 6v12"/>', question: '<path d="M9 8a3 3 0 016 0c0 2-3 2-3 4M12 16h.01"/>', check: '<path d="M5 12l4 4L19 6"/>', chevron: '<path d="M9 5l7 7-7 7"/>'};
const icon = (name) => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${iconPaths[name] || iconPaths.arrow}</svg>`;
function relative(timestamp) {const delta = Math.max(0, (Date.now()/1000 - timestamp)); return delta < 60 ? 'Just now' : delta < 3600 ? `${Math.floor(delta/60)}m ago` : delta < 86400 ? `${Math.floor(delta/3600)}h ago` : `${Math.floor(delta/86400)}d ago`;}
function toast(message) {$('#toast').textContent = message; $('#toast').classList.add('visible'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').classList.remove('visible'), 4000);}
async function api(path, body) {const response = await fetch(`/api/${path}`, {method: body === undefined ? 'GET' : 'POST', headers: {'X-HQ-Token': token, 'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)}); const data = await response.json(); if (!response.ok) throw new Error(data.error || 'HQ could not save this change'); return data;}
function scoped() {return state.items.filter(i => (!filter.session || i.session_id === filter.session) && (!filter.tag || i.tags.includes(filter.tag)) && (!filter.harness || i.provider === filter.harness));}
function inView(item, view) {return view === 'needs' ? item.status === 'open' && item.kind !== 'todo' : view === 'pending' ? item.status === 'open' && item.kind === 'todo' : item.status === (view === 'done' ? 'done' : 'answered');}
function deliveryText(item) {return ({pending: 'Queued', sending: 'Sending…', sent: 'Sent · unconfirmed', received: 'Received', failed: 'Delivery failed'})[item.delivery] || 'Saved';}
function setView(view) {filter.view = view; render();}
function render() {
  const items = scoped();
  ['needs','pending','answered'].forEach(view => $(`#${view}-count`).textContent = items.filter(i => inView(i, view)).length);
  document.querySelectorAll('[data-view]').forEach(el => {const active = el.dataset.view === filter.view; el.classList.toggle('active', active); if (el.hasAttribute('role')) el.setAttribute('aria-selected', active);});
  const session = state.sessions.find(s => s.id === filter.session);
  $('#scope').textContent = session ? session.name : [filter.harness && providers[filter.harness][1], filter.tag].filter(Boolean).join(' / ') || 'Inbox';
  $('#all-count').textContent = state.items.filter(i => i.status === 'open').length;
  $('#all-work').classList.toggle('active', !filter.session && !filter.tag && !filter.harness);
  const tags = [...new Set(state.items.flatMap(i => i.tags))].sort();
  $('#tags-nav').innerHTML = tags.length ? tags.map(t => `<button class="nav-item ${filter.tag === t ? 'active' : ''}" data-tag="${escapeHTML(t)}"><span class="tag-dot"></span>${escapeHTML(t)}<span class="nav-number">${state.items.filter(i => i.tags.includes(t) && i.status === 'open').length}</span></button>`).join('') : '<div class="sidebar-empty">No tags</div>';
  const harnesses = Object.keys(providers).filter(p => state.sessions.some(s => s.provider === p));
  $('#harnesses-nav').innerHTML = harnesses.map(p => `<button class="nav-item ${filter.harness === p ? 'active' : ''}" data-harness="${p}"><span class="harness-indicator">${providers[p][0]}</span>${providers[p][1]}<span class="nav-number">${state.sessions.filter(s => s.provider === p).length}</span></button>`).join('') || '<div class="sidebar-empty">No agents</div>';
  const harnessSessions = state.sessions.filter(s => !filter.harness || s.provider === filter.harness);
  $('#session-total').textContent = harnessSessions.length;
  const sessionSearch = $('#session-search').value.toLowerCase();
  const sessions = [...harnessSessions].sort((a,b) => Number(b.connection !== 'offline') - Number(a.connection !== 'offline') || state.items.filter(i => i.session_id === b.id && i.status === 'open').length - state.items.filter(i => i.session_id === a.id && i.status === 'open').length);
  const listed = sessions.filter(s => (showAllSessions || sessionSearch || filter.session === s.id || s.connection !== 'offline' || state.items.some(i => i.session_id === s.id && i.status === 'open')) && `${s.name} ${s.provider} ${(providers[s.provider] || providers.other)[1]}`.toLowerCase().includes(sessionSearch));
  $('#sessions-nav').innerHTML = listed.map(s => `<button class="nav-item session-item ${filter.session === s.id ? 'active' : ''}" data-session="${escapeHTML(s.id)}" title="${escapeHTML(s.name)}"><span class="provider-symbol">${(providers[s.provider] || providers.other)[0]}</span><span class="session-text"><span class="session-name">${escapeHTML(s.name)}</span>${harnessHTML(s.provider)}<span class="session-meta">${state.items.filter(i => i.session_id === s.id && i.status === 'open').length} pending · ${s.connection === 'offline' ? 'Not connected' : s.connection === 'waiting' ? 'Waiting for you' : 'Connected'}</span></span><span class="session-state ${s.connection !== 'offline' ? 'live' : ''}"></span></button>`).join('') || '<div class="sidebar-empty">No conversations</div>';
  $('#browse-sessions').textContent = showAllSessions ? 'Show active' : `View all (${harnessSessions.length})`;
  const visible = items.filter(i => inView(i, filter.view) && `${i.title} ${i.detail} ${i.tags.join(' ')} ${i.session_name}`.toLowerCase().includes(filter.search)).sort((a,b) => (a.kind === 'blocker' ? -1 : 0) - (b.kind === 'blocker' ? -1 : 0) || b.created-a.created);
  $('#items').innerHTML = visible.length ? visible.map(i => `<button class="item" data-item="${escapeHTML(i.id)}"><span class="item-icon ${i.status === 'open' ? i.kind : i.status}">${icon(i.status === 'answered' || i.status === 'done' ? 'check' : i.kind === 'todo' ? 'arrow' : i.kind === 'blocker' ? 'pause' : 'question')}</span><span class="item-body"><span class="item-title">${escapeHTML(i.title)}</span>${i.detail ? `<span class="item-preview">${escapeHTML(i.detail)}</span>` : ''}<span class="item-context">${harnessHTML(i.provider)}<span class="item-session">${escapeHTML(i.session_name)}</span>${i.tags.length ? '<span class="context-divider">/</span>' : ''}${tagHTML(i.tags)}</span></span><span class="item-side"><span class="kind-label ${i.kind === 'blocker' && i.status === 'open' ? 'blocked' : ''}">${i.status === 'answered' ? escapeHTML(deliveryText(i)) : i.kind === 'blocker' ? 'Blocked' : i.kind === 'question' ? 'Needs input' : 'Task'}</span>${relative(i.created)}</span><span class="item-arrow">${icon('chevron')}</span></button>`).join('') : `<div class="empty"><div class="empty-symbol">${icon(state.items.length ? 'check' : 'arrow')}</div><h3>${filter.search ? 'No matches' : state.items.length ? ({needs:'No blockers',pending:'No pending tasks',answered:'No replies',done:'No completed tasks'})[filter.view] : 'Connect your first agent'}</h3>${!state.items.length ? '<button class="button" id="empty-connect">Connect an agent</button>' : ''}</div>`;
  $('#tags-nav').querySelectorAll('[data-tag]').forEach(el => el.onclick = () => {filter.tag = filter.tag === el.dataset.tag ? null : el.dataset.tag; filter.session = null; render(); closeSidebar();});
  $('#harnesses-nav').querySelectorAll('[data-harness]').forEach(el => el.onclick = () => {filter.harness = filter.harness === el.dataset.harness ? null : el.dataset.harness; filter.session = null; render(); closeSidebar();});
  $('#sessions-nav').querySelectorAll('[data-session]').forEach(el => el.onclick = () => {filter.session = el.dataset.session; filter.tag = null; render(); closeSidebar();});
  $('#items').querySelectorAll('[data-item]').forEach(el => el.onclick = () => openDetail(el.dataset.item));
  if ($('#empty-connect')) $('#empty-connect').onclick = () => $('#connect-dialog').showModal();
}
function openDetail(id) {
  selectedId = id;
  const item = state.items.find(i => i.id === id); if (!item) return;
  const session = state.sessions.find(s => s.id === item.session_id);
  const response = item.status === 'open' && item.kind !== 'todo';
  $('#detail-content').innerHTML = `<div class="eyebrow">${item.kind === 'blocker' ? 'BLOCKER' : item.kind === 'question' ? 'QUESTION' : 'TASK'}</div><h2>${escapeHTML(item.title)}</h2><div class="detail-meta">${harnessHTML(session?.provider || item.provider)}<span>${escapeHTML(item.session_name)}</span>${tagHTML(item.tags)}</div>${item.detail ? `<div class="detail-context"><p>${escapeHTML(item.detail)}</p></div>` : ''}${item.answer !== null ? `<div class="detail-answer"><span>YOUR RESPONSE</span><p>${escapeHTML(item.answer)}</p></div><div id="delivery-status" class="delivery ${item.delivery === 'failed' ? 'error' : ''}">${escapeHTML(deliveryText(item))}${item.delivery_error ? `<br>${escapeHTML(item.delivery_error)}` : ''}</div>${item.delivery === 'failed' ? '<button id="retry-response" class="button">Retry delivery</button>' : ''}` : ''}${response ? `<form class="response-form" id="response-form">${item.options.length ? `<div class="choices">${item.options.map(o => `<button type="button" class="choice" data-choice="${escapeHTML(o)}">${escapeHTML(o)}</button>`).join('')}</div>` : ''}<label for="answer">Your response</label><textarea id="answer" name="text" rows="4" required maxlength="20000" placeholder="Your reply…"></textarea><div class="response-actions"><small>${session && (session.connection === 'waiting' || session.connection === 'channel' || session.route === 'codex') ? 'Auto-delivery' : 'Queued for agent'}</small><button class="button primary" type="submit">Send reply</button></div><p class="form-error" aria-live="polite"></p></form>` : ''}${(item.status === 'open' && item.kind === 'todo') || (item.status === 'answered' && item.delivery === 'received') ? '<div class="response-actions"><button class="button" id="resolve-item">Mark completed ✓</button></div>' : ''}`;
  if (!$('#detail-dialog').open) $('#detail-dialog').showModal();
  $('#detail-content').querySelectorAll('[data-choice]').forEach(el => el.onclick = () => {$('#answer').value = el.dataset.choice; $('#detail-content').querySelectorAll('.choice').forEach(c => c.classList.toggle('selected', c === el));});
  if ($('#response-form')) $('#response-form').onsubmit = async event => {event.preventDefault(); const button = event.target.querySelector('[type="submit"]'); button.disabled = true; try {await api(`items/${id}/reply`, {text: $('#answer').value}); await refresh(); openDetail(id); toast('Reply saved');} catch(error) {event.target.querySelector('.form-error').textContent = error.message;} finally {button.disabled = false;}};
  if ($('#resolve-item')) $('#resolve-item').onclick = async () => {try {await api(`items/${id}/resolve`, {}); $('#detail-dialog').close(); await refresh(); toast('Item completed');} catch(error) {toast(error.message);}};
  if ($('#retry-response')) $('#retry-response').onclick = async () => {try {await api(`items/${id}/retry`, {}); await refresh(); openDetail(id);} catch(error) {toast(error.message);}};
}
async function refresh() {
  if (loading) return;
  loading = true;
  try {
    const next = await api('state');
    const signature = JSON.stringify({items: next.items, sessions: next.sessions.map(s => [s.id, s.name, s.provider, s.connection, s.route])});
    state = next;
    if (signature !== snapshotSignature) {
      snapshotSignature = signature;
      const focused = document.activeElement;
      const focusItem = focused?.dataset.item;
      render();
      if (focusItem) [...$('#items').querySelectorAll('[data-item]')].find(el => el.dataset.item === focusItem)?.focus({preventScroll: true});
      if ($('#detail-dialog').open && selectedId && (!$('#response-form') || state.items.find(i => i.id === selectedId)?.status !== 'open')) openDetail(selectedId);
    }
    $('#service-status').hidden = true;
  } catch(error) {$('#service-status').textContent = 'Offline'; $('#service-status').hidden = false;}
  finally {loading = false;}
}
function closeSidebar() { $('.sidebar').classList.remove('visible'); $('#mobile-menu').setAttribute('aria-expanded', 'false'); }
document.querySelectorAll('[data-view]').forEach(el => el.onclick = () => setView(el.dataset.view));
$('#all-work').onclick = () => {filter.session = null; filter.tag = null; filter.harness = null; render(); closeSidebar();};
$('#browse-sessions').onclick = () => {showAllSessions = !showAllSessions; render();};
$('#mobile-menu').onclick = () => {const visible = $('.sidebar').classList.toggle('visible'); $('#mobile-menu').setAttribute('aria-expanded', String(visible));};
document.addEventListener('click', event => {if ($('.sidebar').classList.contains('visible') && !event.target.closest('.sidebar') && !event.target.closest('#mobile-menu')) closeSidebar();});
document.addEventListener('keydown', event => {if (event.key === 'Escape') closeSidebar();});
$('#search').oninput = event => {filter.search = event.target.value.toLowerCase(); render();};
$('#session-search').oninput = render;
$('#connections').onclick = () => $('#connect-dialog').showModal();
$('#add-button').onclick = () => {$('#session-select').innerHTML = state.sessions.map(s => `<option value="${escapeHTML(s.id)}">${escapeHTML(s.name)}</option>`).join(''); if (!state.sessions.length) return $('#connect-dialog').showModal(); $('#add-dialog').showModal();};
$('#add-form').onsubmit = async event => {event.preventDefault(); const form = event.target, fields = Object.fromEntries(new FormData(form)), button = form.querySelector('[type="submit"]'); fields.tags = fields.tags.split(',').map(t => t.trim()).filter(Boolean); button.disabled = true; try {await api('items', fields); filter.view = fields.kind === 'todo' ? 'pending' : 'needs'; $('#add-dialog').close(); form.reset(); await refresh(); toast('Item added');} catch(error) {form.querySelector('.form-error').textContent = error.message;} finally {button.disabled = false;}};
refresh(); setInterval(refresh, 2000);
