// The Usage clips: several Claude and Codex accounts, switching with one tap, and adding one. Each is
// drawn from the real panel: make.mjs drives it through every state against a throwaway HQ with
// fake logins, snapshot.js draws each state as SVG, and this file animates between them with the
// panel's own timings. Every write Usage makes (switch, add, remove) is answered inside the page,
// so nothing ever reaches a login or the Keychain.
import {readFileSync, writeFileSync} from 'node:fs';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {sleep} from './chrome.mjs';
import {CURSOR, Derived, EASE, SHADOWS, Track, animate, backdrop, fontFace, svg, transform} from './vector.mjs';

const HERE = dirname(fileURLToPath(import.meta.url)), OUT = resolve(HERE, '..');
const SNAP = readFileSync(join(HERE, 'snapshot.js'), 'utf8');
const W = 450, APP = {x: 230, y: 40, width: 400, height: 600};
const FADE = 0.15;

// Stand in for the server's account writes, from a copy of what it listed.
const ANSWER = `(() => {
  const real = window.fetch;
  window.__posts = []; window.__job = null;
  const reply = (body, status = 200) => Promise.resolve(new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}}));
  let listing = null;
  window.fetch = async (url, init = {}) => {
    const u = String(url), path = u.replace(/^.*\\/api\\//, '');
    if (!u.includes('/api/accounts')) return real(url, init);
    if ((init.method || 'GET') === 'GET') {
      if (path === 'accounts') {
        if (!listing) listing = await (await real(url, init)).json();
        return reply(listing);
      }
      if (path.startsWith('accounts/jobs/')) return reply(window.__job);
    }
    window.__posts.push(path);
    const [, tool, key, verb] = path.split('/');
    if (verb === 'use') {
      const t = listing[tool], to = t.accounts.find((a) => a.key === key);
      t.accounts.forEach((a) => {a.active = a.key === key; a.saved = true;});
      t.active = key;
      return reply({tool, already: false, note: null, warning: null, verified: true, account: to, running: 2});
    }
    if (key === 'add') {
      window.__job = {id: 'job1', tool, state: 'waiting', message: '', account: null, terminal: true, started: Date.now() / 1000};
      return reply(window.__job);
    }
    if (path.endsWith('/cancel')) return reply({...window.__job, state: 'cancelled'});
    return reply({error: 'not in this recording'}, 400);
  };
  window.__added = (tool, account) => {listing[tool].accounts.push(account); window.__job = {...window.__job, state: 'added', account};};
})()`;

async function capture(s, origin) {
  const inF = (e) => s.page.evaluateIn(origin, e);
  const take = (sel, opts = {}) => inF(`__snap(${JSON.stringify(sel)}, ${JSON.stringify(opts)})`);
  return {
    box: await take('#box', {shell: true, exclude: ['.u-toast', '.u-veil', '.u-spin', '.f-grips']}),
    toast: await take('#main .u-toast'),
    veil: await take('#main .u-veil'),
    spin: await inF(`(() => {const r = document.querySelector('#main .u-spin')?.getBoundingClientRect(); return r ? {x: r.left, y: r.top, width: r.width} : null;})()`),
  };
}

// Builds one clip from its states: [{at, snap, cursor: [[t, x, y, ms]], clicks: [t]}].
function compose({name, title, desc, pill, states, loop}) {
  const heights = states.map((st) => st.snap.box.rect.height), H = Math.ceil(Math.max(...heights) + pill.rect.y + 14);
  const box = states[0].snap.box.rect, shell = states[0].snap.box.shell;
  const height = new Track(heights[0]), chars = {sans: pill.chars.sans, display: pill.chars.display};
  const layers = [], defs = [pill.defs];
  // Back to the first state's height as the loop comes round.
  const reset = loop - FADE - 0.3;
  const fadeTo = (track, t, v) => track.set(t, v, FADE, EASE.ease);
  states.forEach((st, i) => {
    const op = new Track(i === 0 ? 1 : 0);
    if (i > 0) fadeTo(op, st.at, 1);
    const next = states[i + 1];
    if (next) fadeTo(op, next.at, 0);
    else if (st.at < loop) fadeTo(op, reset, 0);
    if (i === 0) fadeTo(op, reset, 1);
    if (i > 0) height.set(st.at, st.snap.box.rect.height, 0.56, EASE.hug);
    for (const part of [st.snap.box, st.snap.toast, st.snap.veil].filter(Boolean)) {
      chars.sans += part.chars.sans;
      chars.display += part.chars.display;
      defs.push(part.defs);
    }
    let extra = '';
    if (st.snap.veil) {
      const v = new Track(0);
      v.set(st.at, 1, 0.3, EASE.out);
      extra += `<g opacity="0">${animate(v, 'opacity', loop, 0.004)}${st.snap.veil.svg}</g>`;
    }
    if (st.snap.toast) {
      // afterUsage: a new notice rises in, 460 ms on the panel's spring.
      const t = st.snap.toast.rect, cx = t.x + t.width / 2, cy = t.y + t.height / 2;
      const o = new Track(0), lift = new Track(14), scale = new Track(0.97);
      o.set(st.at, 1, 0.46, EASE.spring);
      lift.set(st.at, 0, 0.46, EASE.spring);
      scale.set(st.at, 1, 0.46, EASE.spring);
      extra += `<g opacity="0">${animate(o, 'opacity', loop, 0.004)}<g>${transform(new Derived([lift], (y) => [0, y]), 'translate', loop, 0.02)}
<g transform="translate(${cx} ${cy})"><g>${transform(scale, 'scale', loop, 0.001)}<g transform="translate(${-cx} ${-cy})">${st.snap.toast.svg}</g></g></g></g></g>`;
    }
    if (st.snap.spin) {
      // The spinner: a ring with a bright top quarter, one turn every 0.9 s.
      const {x, y, width} = st.snap.spin, c = [x + width / 2, y + width / 2], r = (width - 2) / 2;
      const arc = `M ${c[0] - r * Math.SQRT1_2} ${c[1] - r * Math.SQRT1_2} A ${r} ${r} 0 0 1 ${c[0] + r * Math.SQRT1_2} ${c[1] - r * Math.SQRT1_2}`;
      extra += `<circle cx="${c[0]}" cy="${c[1]}" r="${r}" fill="none" stroke="#fafafa" stroke-opacity="0.18" stroke-width="2"/>
<path d="${arc}" fill="none" stroke="#fafafa" stroke-width="2"><animateTransform attributeName="transform" type="rotate" from="0 ${c[0]} ${c[1]}" to="360 ${c[0]} ${c[1]}" dur="0.9s" repeatCount="indefinite"/></path>`;
    }
    layers.push(`<g opacity="${i === 0 ? 1 : 0}">${animate(op, 'opacity', loop, 0.004)}${st.snap.box.svg}${extra}</g>`);
  });
  height.set(reset, heights[0], 0.56, EASE.hug);
  // The cursor: glides like a hand, and presses on each click.
  const first = states[0].cursor?.[0] || [0, W + 40, H + 40];
  const cursor = new Track([first[1], first[2]]), press = new Track(1);
  for (const st of states) {
    for (const [t, x, y, ms] of st.cursor || []) cursor.set(t, [x, y], (ms || 0) / 1000, EASE.hand);
    for (const t of st.clicks || []) press.set(t, 0.86, 0.06, EASE.ease).set(t + 0.09, 1, 0.12, EASE.ease);
  }
  const back = backdrop(W, H, APP);
  const sh = /(rgba?\([^)]+\))\s+(-?[\d.]+)px\s+(-?[\d.]+)px\s+([\d.]+)px/.exec(shell.shadow || '');
  const shellDefs = `<clipPath id="${name}-box"><rect x="${box.x + 1}" y="${box.y + 1}" width="${box.width - 2}" height="${heights[0] - 2}" rx="${shell.radius - 1}">${animate(new Derived([height], (h) => h - 2), 'height', loop, 0.05)}</rect></clipPath>
<filter id="${name}-shadow" filterUnits="userSpaceOnUse" x="0" y="0" width="${W}" height="${H + 40}"><feDropShadow dx="${sh ? sh[2] : 0}" dy="${sh ? sh[3] : 10}" stdDeviation="${sh ? Number(sh[4]) / 2 : 13}" flood-color="#000" flood-opacity="0.42"/></filter>`;
  const shellRect = `<rect x="${box.x + 0.5}" y="${box.y + 0.5}" width="${box.width - 1}" height="${heights[0] - 1}" rx="${shell.radius - 0.5}" fill="${shell.background}" stroke="${shell.border}" stroke-width="${shell.borderWidth}" filter="url(#${name}-shadow)">${animate(new Derived([height], (h) => h - 1), 'height', loop, 0.05)}</rect>`;
  const body = `${back.body}
${pill.svg}
${shellRect}
<g clip-path="url(#${name}-box)">${layers.join('\n')}</g>
<g><g>${transform(new Derived([cursor], ([x, y]) => [x - 3, y - 2]), 'translate', loop, 0.05)}<g transform="translate(3 2)"><g>${transform(press, 'scale', loop, 0.002)}<g transform="translate(-3 -2)" filter="url(#cursorShadow)">${CURSOR}</g></g></g></g></g>`;
  const font = fontFace([chars.sans], chars.display);
  writeFileSync(join(OUT, name), svg(W, H, title, desc, font, `${back.defs}${SHADOWS(W, H)}${shellDefs}${defs.join('')}`, body));
  return name;
}

// Drives the panel: open Usage from a circle, then the scene's own steps.
export async function usageClips({startHQ, stage}) {
  const made = [];
  for (const scene of [accounts, switching, adding]) {
    const hq = await startHQ({accounts: true});
    let s;
    try {
      s = await stage(hq, {width: W, height: 720, app: APP});
      await s.settle();
      const inF = (e) => s.page.evaluateIn(hq.origin, e);
      await inF(SNAP);
      await inF(ANSWER);
      // Codex's own reading comes from its app-server, which this throwaway HOME has none of; use
      // the signed-in account's, which Usage lists anyway.
      await inF(`(async () => {const a = await (await fetch('/api/accounts', {headers: {'X-HQ-Token': token}})).json();
        const on = a.codex.accounts.find((x) => x.active); state.usage = {...state.usage, codex: on.usage}; render();})()`);
      await sleep(600);
      const pill = await inF(`__snap('#pill')`);
      made.push(await scene(s, hq, inF, pill));
      if (s.page.errors.length) throw new Error(`${scene.name}: ${s.page.errors.join('; ')}`);
    } finally {
      s?.page.close();
      hq.stop();
    }
  }
  return made;
}

const center = (r, dx = 0, dy = 0) => [Math.round(r.x + r.width / 2 + dx), Math.round(r.y + r.height / 2 + dy)];
async function settleOn(s, selector) {
  await s.glideTo(selector, 200);
  await sleep(250);
}

// (a) Every account, its rings and when each limit resets; Claude's card, then Codex's.
async function accounts(s, hq, inF, pill) {
  await s.glideTo('#rail [data-metric="claude"] .f-circle', 200);
  await s.click();
  await s.until(`document.body.dataset.mode !== 'pill'`);
  await s.glide(420, 700, 200);
  await sleep(1200);
  const claude = await capture(s, hq.origin), mini = await s.rect('#main .u-mini');
  await settleOn(s, '#main .u-mini');
  await s.click();
  await sleep(1300);
  await s.glide(420, 700, 200);
  await sleep(500);
  const codex = await capture(s, hq.origin), back = await s.rect('#main .u-mini');
  const rest = [420, 650];
  return compose({name: 'usage-accounts.svg', title: 'Usage: every account', loop: 9.2, pill,
    desc: 'The Usage view lists every Claude account with rings for what is left and when each limit resets, then every Codex account.',
    states: [
      {at: 0, snap: claude, cursor: [[0, ...rest], [0.8, ...center(mini, 60), 900]], clicks: [1.9]},
      {at: 2.0, snap: codex, cursor: [[3.1, ...rest, 700], [5.4, ...center(back, 60), 900]], clicks: [6.5]},
      {at: 6.6, snap: claude, cursor: [[7.3, ...rest, 800]]},
    ]});
}

// (b) One tap moves every Claude session to another account.
async function switching(s, hq, inF, pill) {
  await s.glideTo('#rail [data-metric="claude"] .f-circle', 200);
  await s.click();
  await s.until(`document.body.dataset.mode !== 'pill'`);
  await s.glide(420, 700, 200);
  await sleep(1200);
  const before = await capture(s, hq.origin);
  const use = await inF(`(() => {const b = [...document.querySelectorAll('#main [data-u="use"]')].find((x) => x.closest('.u-acct, .u-row')?.textContent.includes('personal@')); const r = b.getBoundingClientRect(); return {x: r.left, y: r.top, width: r.width, height: r.height, key: b.dataset.key};})()`);
  await s.glide(...center(use), 300);
  await s.click();
  await sleep(1400);
  await s.glide(420, 700, 200);
  await sleep(400);
  const after = await capture(s, hq.origin);
  if (!after.toast) throw new Error('No notice after switching');
  const rest = [420, 660];
  return compose({name: 'usage-switch.svg', title: 'Usage: switch accounts', loop: 7.6, pill,
    desc: 'One tap on Use switches Claude to another account, and a notice says every Claude session moves to it.',
    states: [
      {at: 0, snap: before, cursor: [[0, ...rest], [0.7, ...center(use), 1000]], clicks: [1.9]},
      {at: 2.0, snap: after, cursor: [[3.2, ...rest, 900]]},
    ]});
}

// (c) Adding an account: sign in in the browser, and it joins the list without switching.
async function adding(s, hq, inF, pill) {
  await s.glideTo('#rail [data-metric="codex"] .f-circle', 200);
  await s.click();
  await s.until(`document.body.dataset.mode !== 'pill'`);
  await s.glide(420, 700, 200);
  await sleep(1200);
  const start = await capture(s, hq.origin), add = await s.rect('#main [data-u="add"]');
  await s.glide(...center(add), 300);
  await s.click();
  await sleep(900);
  await s.glide(420, 700, 200);
  await sleep(300);
  const sheet = await capture(s, hq.origin), signin = await s.rect('#main [data-u="signin"]');
  await s.glide(...center(signin), 300);
  await s.click();
  await sleep(900);
  await s.glide(420, 700, 200);
  await sleep(300);
  const waiting = await capture(s, hq.origin);
  if (!waiting.spin) throw new Error('No spinner while waiting for the sign-in');
  await inF(`__added('codex', {key: 'clientkey', email: 'client@example.com', plan: 'Plus', org: '', active: false, saved: true, state: 'ok',
    usage: {windows: [{name: '5-hour', left: 100, resets_at: Date.now() / 1000 + 5 * 3600}, {name: 'weekly', left: 100, resets_at: Date.now() / 1000 + 7 * 86400}], as_of: Date.now() / 1000},
    added: Date.now() / 1000, last_used: null})`);
  await sleep(2200);
  const added = await capture(s, hq.origin);
  if (!added.toast) throw new Error('No notice after adding');
  const rest = [420, 660];
  return compose({name: 'usage-add.svg', title: 'Usage: add an account', loop: 10.4, pill,
    desc: 'Adding a Codex account: sign in with the browser while Usage waits, then it is added, with a button to switch to it.',
    states: [
      {at: 0, snap: start, cursor: [[0, ...rest], [0.6, ...center(add), 900]], clicks: [1.7]},
      {at: 1.8, snap: sheet, cursor: [[2.4, ...center(signin), 800]], clicks: [3.5]},
      {at: 3.6, snap: waiting, cursor: [[4.2, ...rest, 800]]},
      {at: 6.4, snap: added},
    ]});
}
