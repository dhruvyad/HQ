// Draws the pill's clips as animated SVG, so the README shows them sharp at any zoom and in full
// colour, at a few kilobytes each. Every size, colour and baseline is the real panel's, measured
// from /float in headless Chrome, and every motion is its CSS: each transition's duration, delay
// and easing, with the springs sampled from float.js's own springCurve. The output is plain SVG
// with SMIL, so it plays inside an <img>, with no scripts. make.mjs runs this.
import {execFileSync} from 'node:child_process';
import {mkdtempSync, readFileSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {sleep} from './chrome.mjs';

const HERE = dirname(fileURLToPath(import.meta.url)), ROOT = resolve(HERE, '../../..'), OUT = resolve(HERE, '..');
const FPS = 60;

// Easing, as the panel's CSS defines it.
function springPoints(stiffness, damping) {
  // float.js springCurve, which becomes a CSS linear() with evenly spaced stops.
  const step = 1 / 120, points = [];
  let x = 0, v = 0, still = 0;
  for (let t = 0; t < 3 && still < 12; t += step) {
    v += (-stiffness * (x - 1) - damping * v) * step;
    x += v * step;
    points.push(x);
    still = Math.abs(x - 1) < 0.002 && Math.abs(v) < 0.02 ? still + 1 : 0;
  }
  const every = Math.max(1, Math.floor(points.length / 44));
  return [0, ...points.filter((_, n) => n % every === 0).map((p) => Number(p.toFixed(3))), 1];
}
const linearCurve = (stops) => (p) => {
  if (p <= 0) return 0;
  if (p >= 1) return 1;
  const at = p * (stops.length - 1), n = Math.floor(at);
  return stops[n] + (stops[n + 1] - stops[n]) * (at - n);
};
function bezier(x1, y1, x2, y2) {
  const curve = (a, b, t) => 3 * a * t * (1 - t) ** 2 + 3 * b * t * t * (1 - t) + t ** 3;
  return (p) => {
    if (p <= 0) return 0;
    if (p >= 1) return 1;
    let low = 0, high = 1, t = p;
    for (let n = 0; n < 40; n += 1) {
      t = (low + high) / 2;
      if (curve(x1, x2, t) < p) low = t;
      else high = t;
    }
    return curve(y1, y2, t);
  };
}
export const EASE = {
  spring: linearCurve(springPoints(190, 17)), soft: linearCurve(springPoints(170, 26)), hug: linearCurve(springPoints(240, 26)),
  ease: bezier(0.25, 0.1, 0.25, 1), out: bezier(0, 0, 0.58, 1), linear: (p) => p,
  // The capture's cursor: ease in and out, like a hand.
  hand: (t) => (t < 0.5 ? 4 * t ** 3 : 1 - (-2 * t + 2) ** 3 / 2),
};

// A property over time. set() starts a CSS transition from wherever the value is right then.
export class Track {
  constructor(value) {
    this.initial = value;
    this.moves = [];
  }
  valueAt(t) {
    let move = null;
    for (const m of this.moves) {
      if (m.change > t) break;
      move = m;
    }
    if (!move) return this.initial;
    if (t < move.begin) return move.from;
    if (!move.dur || t >= move.begin + move.dur) return move.to;
    return mix(move.from, move.to, move.ease((t - move.begin) / move.dur));
  }
  set(t, to, dur = 0, ease = EASE.linear, delay = 0) {
    // On the same 0.1 ms grid as the samples, so an instant change is never sampled early.
    const at = Number(t.toFixed(4));
    this.moves.push({change: at, begin: Number((at + delay).toFixed(4)), dur, ease, from: this.valueAt(at), to});
    return this;
  }
  times(loop) {
    const all = [0, loop];
    for (const m of this.moves) {
      all.push(m.change, m.begin);
      if (!m.dur) all.push(m.begin - 0.001);
      for (let t = m.begin; t < m.begin + m.dur; t += 1 / FPS) all.push(t);
      all.push(m.begin + m.dur);
    }
    return [...new Set(all.filter((t) => t >= 0 && t <= loop).map((t) => Number(t.toFixed(4))))].sort((a, b) => a - b);
  }
}
// A value worked out from other tracks, such as a center that follows a growing box.
export class Derived {
  constructor(tracks, fn) {
    this.tracks = tracks;
    this.fn = fn;
    this.moves = tracks.flatMap((t) => t.moves);
  }
  valueAt(t) {return this.fn(...this.tracks.map((track) => track.valueAt(t)));}
  times(loop) {return [...new Set(this.tracks.flatMap((t) => t.times(loop)))].sort((a, b) => a - b);}
}
const mix = (a, b, e) => (Array.isArray(a) ? a.map((v, n) => v + (b[n] - v) * e) : a + (b - a) * e);
const num = (v) => String(Number(v.toFixed(3)));
const show = (v) => (Array.isArray(v) ? v.map(num).join(' ') : num(v));

// Keep only the samples a straight line between neighbours cannot reproduce.
function simplify(points, eps) {
  const far = (a, b, p) => {
    const e = (p.t - a.t) / (b.t - a.t || 1), va = [a.v].flat(), vb = [b.v].flat(), vp = [p.v].flat();
    return va.some((v, n) => Math.abs(v + (vb[n] - v) * e - vp[n]) > eps);
  };
  const kept = [points[0]];
  let anchor = 0;
  for (let n = 2; n < points.length; n += 1) {
    if (points.slice(anchor + 1, n).some((p) => far(points[anchor], points[n], p))) {
      kept.push(points[n - 1]);
      anchor = n - 1;
    }
  }
  kept.push(points[points.length - 1]);
  return kept;
}
function smil(track, loop, element, eps = 0.01) {
  if (!track.moves.length) return '';
  const points = simplify(track.times(loop).map((t) => ({t, v: track.valueAt(t)})), eps);
  const keyTimes = points.map((p) => (p.t / loop).toFixed(6)).join(';'), values = points.map((p) => show(p.v)).join(';');
  return `<${element} dur="${loop}s" repeatCount="indefinite" calcMode="linear" keyTimes="${keyTimes}" values="${values}"/>`;
}
export const animate = (track, attr, loop, eps) => smil(track, loop, `animate attributeName="${attr}"`, eps);
export const transform = (track, type, loop, eps) => smil(track, loop, `animateTransform attributeName="transform" type="${type}"`, eps);

// Inter and Instrument Serif, cut down to the characters drawn, so text matches the panel without
// a font download. The image CSP GitHub serves SVGs with still allows a data: font.
function subset(file, text) {
  const dir = mkdtempSync(join(tmpdir(), 'hq-media-font-')), out = join(dir, 'font.woff2');
  try {
    const args = [join(ROOT, 'hq/static', file), `--text=${[...new Set(text)].join('') || ' '}`,
      '--layout-features=kern,tnum', '--flavor=woff2', `--output-file=${out}`];
    // fontTools with brotli, from whichever Python has it, or a throwaway one from uv.
    const tries = [[process.env.PYTHON || 'python3', '-m', 'fontTools.subset'], ['uvx', '--from', 'fonttools[woff]', 'pyftsubset']];
    const ok = tries.some(([command, ...rest]) => {
      try {
        execFileSync(command, [...rest, ...args], {stdio: 'ignore'});
        return true;
      } catch {
        return false;
      }
    });
    if (!ok) throw new Error('Subsetting fonts needs fontTools and brotli: pip install fonttools brotli, or install uv');
    return readFileSync(out).toString('base64');
  } finally {
    rmSync(dir, {recursive: true, force: true});
  }
}
export function fontFace(texts, display = '') {
  const sans = `@font-face {font-family: 'HQ Inter'; font-weight: 100 900; src: url(data:font/woff2;base64,${subset('inter-latin.woff2', [texts].flat().join(''))}) format('woff2');}`;
  return display ? `${sans}\n@font-face {font-family: 'HQ Serif'; src: url(data:font/woff2;base64,${subset('instrument-serif-latin.woff2', display)}) format('woff2');}` : sans;
}
const FONT = `'HQ Inter', 'Inter Variable', Inter, ui-sans-serif, system-ui, sans-serif`;

// The capture stage's fake desktop (stage.html): a neutral wallpaper and a faint editor window.
export function backdrop(W, H, app) {
  const a = 160 * Math.PI / 180, dx = Math.sin(a), dy = -Math.cos(a), length = Math.abs(W * dx) + Math.abs(H * dy);
  const glow = (id, cx, cy, rx, ry, color) => `<radialGradient id="${id}" gradientUnits="userSpaceOnUse" cx="${num(cx)}" cy="${num(cy)}" r="${num(rx)}" gradientTransform="translate(${num(cx)} ${num(cy)}) scale(1 ${num(ry / rx)}) translate(${num(-cx)} ${num(-cy)})"><stop offset="0" stop-color="${color}"/><stop offset="0.7" stop-color="${color}" stop-opacity="0"/></radialGradient>`;
  const inner = {x: app.x + 1, y: app.y + 1, w: app.width - 2, h: app.height - 2};
  const sideLines = [70, 55, 62, 48, 66, 40].map((w, n) => `<rect x="${inner.x + 16}" y="${inner.y + 31 + 18 + 25 * n}" width="${num(1.5 * w)}" height="7" rx="3.5" fill="#fff" fill-opacity="0.06"/>`).join('');
  const linesWidth = inner.w - 172 - 24;
  const lines = [62, 48, 71, 55, 66, 38, 74, 52, 60, 44, 69, 57, 35, 63, 50, 72, 46, 58].map((w, n) => {
    const left = [0, 0, 24, 24, 48, 24, 0, 0, 24][n % 9];
    return `<rect x="${num(inner.x + 172 + left)}" y="${inner.y + 52 + 22 * n}" width="${num(linesWidth * w / 100)}" height="7" rx="3.5" fill="#fff" fill-opacity="0.07"/>`;
  }).join('');
  return {
    defs: `<linearGradient id="wall" gradientUnits="userSpaceOnUse" x1="${num(W / 2 - dx * length / 2)}" y1="${num(H / 2 - dy * length / 2)}" x2="${num(W / 2 + dx * length / 2)}" y2="${num(H / 2 + dy * length / 2)}"><stop offset="0" stop-color="#171717"/><stop offset="1" stop-color="#0b0b0b"/></linearGradient>
${glow('glow', 0.22 * W, 0.08 * H, 0.75 * W, 0.85 * H, '#292929')}
<clipPath id="appClip"><rect x="${app.x}" y="${app.y}" width="${app.width}" height="${app.height}" rx="12"/></clipPath>
<filter id="appShadow" filterUnits="userSpaceOnUse" x="0" y="0" width="${W}" height="${H}"><feDropShadow dx="0" dy="20" stdDeviation="25" flood-color="#000" flood-opacity="0.45"/></filter>`,
    body: `<rect width="${W}" height="${H}" fill="url(#wall)"/><rect width="${W}" height="${H}" fill="url(#glow)"/>
<rect x="${app.x}" y="${app.y}" width="${app.width}" height="${app.height}" rx="12" fill="#161616" fill-opacity="0.92" filter="url(#appShadow)"/>
<g clip-path="url(#appClip)">
${[0, 1, 2].map((n) => `<circle cx="${inner.x + 17 + 17 * n}" cy="${inner.y + 15}" r="5" fill="#fff" fill-opacity="0.13"/>`).join('')}
<rect x="${inner.x}" y="${inner.y + 30}" width="${inner.w}" height="1" fill="#fff" fill-opacity="0.05"/>
<rect x="${inner.x + 150}" y="${inner.y + 31}" width="1" height="${inner.h}" fill="#fff" fill-opacity="0.05"/>
${sideLines}${lines}</g>
<rect x="${app.x + 0.5}" y="${app.y + 0.5}" width="${app.width - 1}" height="${app.height - 1}" rx="11.5" fill="none" stroke="#fff" stroke-opacity="0.07"/>`,
  };
}

// The panel's pieces, at the sizes measured from /float.
const PILL = {x: 10, y: 10, w: 38, h: 134, r: 19}, CARD = {w: 308, h: 62, r: 31};
const CIRCLES = [
  {metric: 'needs', kind: 'loud', value: '6', label: '6 need you', labelWidth: 92.08},
  {metric: 'agents', kind: 'live', value: '2', label: '2 agents running', labelWidth: 125.53},
  {metric: 'claude', kind: 'gauge', value: '62', label: "62% of Claude's 5-hour limit left", labelWidth: 217.91},
  {metric: 'codex', kind: 'gauge', value: '18', label: "18% of Codex's 5-hour limit left", labelWidth: 212.13},
].map((c, n) => ({...c, cx: 29, cy: 29 + 32 * n}));
const DIGIT_DROP = 4.367; // half the cap height of 12px Inter: the panel centers a number on its glyphs
function digits(text, kind, extra = '') {
  const color = kind === 'loud' ? '#18181b' : '#fafafa';
  return `<text x="0" y="${DIGIT_DROP}" text-anchor="middle" fill="${color}" font-size="12" font-weight="600" letter-spacing="-0.12" style="font-variant-numeric: tabular-nums"${extra}>${text}</text>`;
}
function circleShape(kind, value) {
  if (kind === 'loud') return '<circle r="14" fill="#fafafa"/>';
  if (kind === 'live') return '<circle r="13.25" fill="none" stroke="#4ade80" stroke-width="1.5"/>';
  const r = 16 * 28 / 36, w = 3 * 28 / 36;
  return `<circle r="${num(r)}" fill="none" stroke="#fafafa" stroke-opacity="0.16" stroke-width="${num(w)}"/>
<circle r="${num(r)}" fill="none" stroke="#fafafa" stroke-width="${num(w)}" stroke-linecap="round" pathLength="100" stroke-dasharray="${value} 100" transform="rotate(-90)"/>`;
}
export const SHADOWS = (W, H) => `<filter id="pillShadow" filterUnits="userSpaceOnUse" x="0" y="0" width="${W}" height="${H}"><feDropShadow dx="0" dy="6" stdDeviation="11" flood-color="#000" flood-opacity="0.38"/></filter>
<filter id="spotShadow" x="-30%" y="-150%" width="160%" height="400%"><feDropShadow dx="0" dy="6" stdDeviation="11" flood-color="#000" flood-opacity="0.35"/></filter>
<filter id="cursorShadow" x="-50%" y="-50%" width="200%" height="200%"><feDropShadow dx="0" dy="1" stdDeviation="0.75" flood-color="#000" flood-opacity="0.5"/></filter>`;
export const CURSOR = '<path d="M3 2 L3 17.5 L7 13.8 L9.8 20 L12.6 18.8 L9.9 12.7 L15.2 12.7 Z" fill="#000" stroke="#fff" stroke-width="1.4" stroke-linejoin="round"/>';

export function svg(W, H, title, desc, style, defs, body) {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-labelledby="title desc">
<title id="title">${title}</title>
<desc id="desc">${desc}</desc>
<style>${style}
text {-webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale;}
text:not([font-family]) {font-family: ${FONT};}</style>
<defs><clipPath id="frame"><rect width="${W}" height="${H}" rx="12"/></clipPath>${defs}</defs>
<g clip-path="url(#frame)">
${body}
</g>
</svg>
`;
}

// What the pill shows on hover and after a change, read from the real page: each circle's label,
// the Island card for a burst, the badge it leaves, the dot that follows and the history.
export async function capturePill({startHQ, stage}) {
  const SNAP = readFileSync(join(HERE, 'snapshot.js'), 'utf8');
  const hq = await startHQ();
  let s;
  try {
    s = await stage(hq, {width: 480, height: 200});
    await s.settle();
    const inF = (e) => s.page.evaluateIn(hq.origin, e);
    await inF(SNAP);
    const labels = [];
    for (const c of CIRCLES) {
      await s.glide(c.cx + 7, c.cy + 6, 250);
      await sleep(900);
      labels.push(await inF(`__snap('#spot', {shell: true})`));
    }
    await s.glide(470, 190, 250);
    await sleep(700);
    hq.fixtures('burst');
    await s.until(`document.querySelector('#pill').classList.contains('noting')`);
    await sleep(1200);
    const card = await inF(`({title: document.querySelector('.f-note-title').textContent, sub: document.querySelector('.f-note-sub').textContent,
      digits: [...document.querySelectorAll('#hero .f-num b')].map((b) => b.textContent)})`);
    await s.until(`!document.querySelector('#pill').classList.contains('noting')`);
    await sleep(1600);
    const badge = await inF(`__snap('#marks')`);
    // Two minutes on, the badge has become a dot.
    await inF(`(marks.log.forEach((e) => {e.t -= 125;}), tickMarks(), 1)`);
    await sleep(500);
    const dot = await inF(`__snap('.f-mark-dot')`);
    await s.glide(CIRCLES[0].cx + 7, CIRCLES[0].cy + 6, 250);
    await sleep(900);
    const history = await inF(`__snap('#spot', {shell: true})`);
    const to = await inF(`document.querySelector('#rail [data-metric="needs"] .f-circle > span:last-child').textContent`);
    if (s.page.errors.length) throw new Error(s.page.errors.join('; '));
    if (!badge?.svg || !dot?.svg) throw new Error('No badge or dot after the burst');
    return {labels, card: {...card, from: CIRCLES[0].value, to}, badge, dot, history};
  } finally {
    s?.page.close();
    hq.stop();
  }
}

// A label from the page: its own box (fill, hairline, radius, shadow) and what is in it.
function labelShell(snap) {
  const {rect: r, shell} = snap;
  return `<rect x="${num(r.x + 0.5)}" y="${num(r.y + 0.5)}" width="${num(r.width - 1)}" height="${num(r.height - 1)}" rx="${num(shell.radius - 0.5)}" fill="${shell.background}" stroke="${shell.border}" stroke-width="${shell.borderWidth}" filter="url(#spotShadow)"/>`;
}

// Hovering the pill: the circle under the cursor steps forward with its label, the others step
// back. The labels are the page's own, the agents one listing who is running.
function glance(labels) {
  const W = 480, H = 170, app = {x: 250, y: 26, width: 300, height: 200};
  const rest = [410, 130], script = [['wait', 0.6]];
  CIRCLES.forEach((c, n) => script.push(['glide', [c.cx + 7, c.cy + 6], n ? 0.38 : 0.8], ['wait', n === 1 ? 1.8 : 1.1]));
  script.push(['glide', rest, 0.8], ['wait', 0.7]);
  const loop = Number(script.reduce((sum, [, a, b]) => sum + (b ?? a), 0).toFixed(3));
  const cursor = new Track(rest);
  let t = 0;
  for (const [kind, a, b] of script) {
    if (kind === 'wait') {
      t += a;
      continue;
    }
    cursor.set(t, a, b, EASE.hand);
    t += b;
  }
  // The panel's spotlight, stepped the way the window reports the pointer.
  const rows = CIRCLES.map(() => ({opacity: new Track(1), scale: new Track(1)}));
  const shown = new Track(0), each = labels.map(() => ({on: new Track(0), shift: new Track(0), fade: new Track(0), lift: new Track(0)}));
  let index = -1;
  for (let step = 0; step <= loop * FPS; step += 1) {
    const now = step / FPS, [x, y] = cursor.valueAt(now);
    const inside = x >= PILL.x && x <= PILL.x + PILL.w && y >= PILL.y && y <= PILL.y + PILL.h;
    if (inside) {
      const nearest = CIRCLES.reduce((best, c, n) => (Math.abs(y - c.cy) < Math.abs(y - CIRCLES[best].cy) ? n : best), 0);
      if (nearest === index) continue;
      const l = each[nearest];
      l.on.set(now, 1);
      if (index < 0) {
        shown.set(now, 1, 0.2, EASE.ease);
        l.fade.set(now, 1);
      } else {
        // The box moves from where it was on the panel's spring; the new words fade up into it.
        each[index].on.set(now, 0);
        l.shift.set(now, labels[index].rect.y - labels[nearest].rect.y).set(now + 0.001, 0, 0.5, EASE.spring);
        l.fade.set(now, 0).set(now + 0.001, 1, 0.24, EASE.out);
        l.lift.set(now, 5).set(now + 0.001, 0, 0.24, EASE.out);
      }
      rows.forEach((row, n) => {
        row.opacity.set(now, n === nearest ? 1 : 0.32, 0.2, EASE.ease);
        row.scale.set(now, n === nearest ? 1.08 : 0.9, 0.45, EASE.spring);
      });
      index = nearest;
    } else if (index >= 0) {
      shown.set(now, 0, 0.2, EASE.ease);
      each[index].on.set(now + 0.2, 0);
      rows.forEach((row) => {
        row.opacity.set(now, 1, 0.2, EASE.ease);
        row.scale.set(now, 1, 0.45, EASE.spring);
      });
      index = -1;
    }
  }
  const back = backdrop(W, H, app);
  const circles = CIRCLES.map((c, n) => `<g transform="translate(${c.cx} ${c.cy})"><g>${transform(rows[n].scale, 'scale', loop, 0.002)}${animate(rows[n].opacity, 'opacity', loop, 0.004)}
${circleShape(c.kind, c.value)}${digits(c.value, c.kind)}</g></g>`).join('\n');
  const flat = (track) => new Derived([track], (v) => [0, v]);
  const boxes = labels.map((l, n) => `<g opacity="0">${animate(each[n].on, 'opacity', loop, 0.004)}<g>${transform(flat(each[n].shift), 'translate', loop, 0.02)}
${labelShell(l)}<g opacity="0">${animate(each[n].fade, 'opacity', loop, 0.004)}<g>${transform(flat(each[n].lift), 'translate', loop, 0.02)}${l.svg}</g></g></g></g>`).join('\n');
  const body = `${back.body}
<g filter="url(#pillShadow)"><rect x="${PILL.x + 0.5}" y="${PILL.y + 0.5}" width="${PILL.w - 1}" height="${PILL.h - 1}" rx="${PILL.r - 0.5}" fill="#000" stroke="#fafafa" stroke-opacity="0.16"/></g>
${circles}
<g opacity="0">${animate(shown, 'opacity', loop, 0.004)}
${boxes}</g>
<g><g>${transform(new Derived([cursor], ([x, y]) => [x - 3, y - 2]), 'translate', loop, 0.05)}<g filter="url(#cursorShadow)">${CURSOR}</g></g></g>`;
  return {name: 'glance.svg', texts: [...CIRCLES.map((c) => c.value), ...labels.map((l) => l.chars.sans)],
    build: (font) => svg(W, H, 'The HQ pill', 'Hovering each circle on the pill names it: what needs you, who is running and for how long, then the Claude and Codex usage left.',
      font, `${back.defs}${SHADOWS(W, H)}${labels.map((l) => l.defs).join('')}`, body)};
}

// The Island: the capsule becomes a card saying what changed and folds back, leaving a badge
// with the change beside the circle; a while later a dot, and hovering tells the history.
function notify({card, badge, dot, history}) {
  const W = 440, H = 150, app = {x: 220, y: 24, width: 300, height: 200};
  const DATA_TO_CARD = 0.35, ROLL_AFTER = 0.26, HOLD = 4, FOLD_FOR = 0.64;
  const data = 1.0, on = data + DATA_TO_CARD, off = on + HOLD, cleared = off + FOLD_FOR;
  // The badge lasts two minutes and the dot until you open the panel: shown here in seconds.
  const later = cleared + 2.8, hover = later + 1.4, leave = hover + 2.6, loop = Number((leave + 1.6).toFixed(3));
  const reset = loop - 0.45;
  const pill = {w: new Track(PILL.w), h: new Track(PILL.h), r: new Track(PILL.r)};
  const rail = {opacity: new Track(1), scale: new Track(1), blur: new Track(0)};
  const hero = {opacity: new Track(0), scale: new Track(0.55)};
  const note = {opacity: new Track(0), y: new Track(8)};
  const B = 8.734 * 1.4; // .f-num b moves 140% of its own (cap-trimmed) height
  const railFrom = {opacity: new Track(1), y: new Track(0)}, railTo = {opacity: new Track(0), y: new Track(0)};
  const heroFrom = {opacity: new Track(0), y: new Track(0)}, heroTo = {opacity: new Track(0), y: new Track(0)};
  const cardOn = new Track(0);
  // The data arrives: the rail repaints and its number rolls in.
  railFrom.opacity.set(data, 0);
  railTo.opacity.set(data, 0).set(data + 0.001, 1, 0.42, EASE.soft);
  railTo.y.set(data, 5.24).set(data + 0.001, 0, 0.42, EASE.soft);
  // runNotice: .noting on.
  pill.w.set(on, CARD.w, 0.68, EASE.spring);
  pill.h.set(on, CARD.h, 0.68, EASE.spring);
  pill.r.set(on, CARD.r, 0.5, EASE.ease);
  rail.opacity.set(on, 0, 0.24, EASE.ease);
  rail.scale.set(on, 0.7, 0.5, EASE.soft);
  rail.blur.set(on, 3, 0.24, EASE.ease);
  hero.opacity.set(on, 1, 0.2, EASE.ease, 0.1);
  hero.scale.set(on, 1.25, 0.55, EASE.spring, 0.1);
  note.opacity.set(on, 1, 0.3, EASE.ease, 0.16);
  note.y.set(on, 0, 0.55, EASE.soft, 0.12);
  cardOn.set(on, 1);
  heroFrom.opacity.set(on, 1);
  const roll = on + ROLL_AFTER;
  heroFrom.opacity.set(roll, 0, 0.26, EASE.ease);
  heroFrom.y.set(roll, -B, 0.42, EASE.soft);
  heroTo.y.set(roll, B).set(roll + 0.001, 0, 0.42, EASE.soft);
  heroTo.opacity.set(roll + 0.001, 1, 0.26, EASE.ease);
  // foldNotice: .noting off, and the card's contents clear once it has folded.
  pill.w.set(off, PILL.w, 0.68, EASE.spring);
  pill.h.set(off, PILL.h, 0.68, EASE.spring);
  pill.r.set(off, PILL.r, 0.5, EASE.ease);
  rail.opacity.set(off, 1, 0.24, EASE.ease, 0.22);
  rail.scale.set(off, 1, 0.5, EASE.soft, 0.22);
  rail.blur.set(off, 0, 0.24, EASE.ease, 0.22);
  hero.opacity.set(off, 0, 0.2, EASE.ease);
  hero.scale.set(off, 0.55, 0.55, EASE.spring);
  note.opacity.set(off, 0, 0.16, EASE.ease);
  note.y.set(off, 8, 0.3, EASE.ease);
  cardOn.set(cleared, 0);
  heroTo.opacity.set(cleared, 0);
  // placeMarks: once the card is gone the badge springs in beside the circle.
  const badgeOn = new Track(0), badgeScale = new Track(0.6), dotOn = new Track(0);
  badgeOn.set(cleared, 1, 0.32, EASE.soft);
  badgeScale.set(cleared, 1, 0.32, EASE.soft);
  badgeOn.set(later, 0, 0.5, EASE.ease);
  dotOn.set(later + 0.3, 1, 0.3, EASE.ease);
  // Hover: the circle steps forward and the label tells what changed and when.
  const rows = CIRCLES.map(() => ({opacity: new Track(1), scale: new Track(1)}));
  const label = new Track(0);
  label.set(hover, 1, 0.2, EASE.ease).set(leave, 0, 0.2, EASE.ease);
  rows.forEach((row, n) => {
    row.opacity.set(hover, n ? 0.32 : 1, 0.2, EASE.ease).set(leave, 1, 0.2, EASE.ease);
    row.scale.set(hover, n ? 0.9 : 1.08, 0.45, EASE.spring).set(leave, 1, 0.45, EASE.spring);
  });
  const cursor = new Track([W + 30, 120]);
  cursor.set(hover - 0.8, [CIRCLES[0].cx + 7, CIRCLES[0].cy + 6], 0.8, EASE.hand).set(leave, [W + 30, 120], 0.9, EASE.hand);
  // Round again: the count and the dot go back to how the clip began.
  dotOn.set(reset, 0, 0.3, EASE.ease);
  railTo.opacity.set(reset, 0, 0.3, EASE.ease);
  railFrom.opacity.set(reset, 1, 0.3, EASE.ease);

  const back = backdrop(W, H, app);
  const center = new Derived([pill.h], (h) => PILL.y + h / 2);
  const box = (inset) => [
    animate(new Derived([pill.w], (w) => w - 2 * inset), 'width', loop, 0.02),
    animate(new Derived([pill.h], (h) => h - 2 * inset), 'height', loop, 0.02),
    animate(new Derived([pill.r], (r) => r - inset), 'rx', loop, 0.01)].join('');
  const flat = (track) => new Derived([track], (v) => [0, v]);
  const numberLayer = (d, text, start = 0) => `<g opacity="${start}">${animate(d.opacity, 'opacity', loop, 0.004)}<g>${transform(flat(d.y), 'translate', loop, 0.02)}${digits(text, 'loud')}</g></g>`;
  const at = (n, inner) => `<g transform="translate(${CIRCLES[n].cx} ${CIRCLES[n].cy})"><g>${transform(rows[n].scale, 'scale', loop, 0.002)}${animate(rows[n].opacity, 'opacity', loop, 0.004)}${inner}</g></g>`;
  const railCircles = CIRCLES.map((c, n) => at(n, c.metric === 'needs'
    ? `${circleShape(c.kind, c.value)}${numberLayer(railFrom, card.from, 1)}${numberLayer(railTo, card.to)}`
    : `${circleShape(c.kind, c.value)}${digits(c.value, c.kind)}`)).join('\n');
  const dotAt = [CIRCLES[0].cx, CIRCLES[0].cy];
  const body = `${back.body}
<g filter="url(#pillShadow)"><rect x="${PILL.x + 0.5}" y="${PILL.y + 0.5}" width="${PILL.w - 1}" height="${PILL.h - 1}" rx="${PILL.r - 0.5}" fill="#000" stroke="#fafafa" stroke-opacity="0.16">${box(0.5)}</rect></g>
<g clip-path="url(#pillClip)">
<g transform="translate(29 77)"><g>${animate(rail.opacity, 'opacity', loop, 0.004)}${transform(rail.scale, 'scale', loop, 0.002)}<g filter="url(#railBlur)" transform="translate(-29 -77)">
${railCircles}
</g></g></g>
<g transform="translate(37 0)"><g opacity="0">${animate(hero.opacity, 'opacity', loop, 0.004)}${transform(flat(center), 'translate', loop, 0.02)}<g opacity="0">${animate(cardOn, 'opacity', loop, 0.004)}
<g><g>${transform(hero.scale, 'scale', loop, 0.002)}<circle r="14" fill="#fafafa"/>${numberLayer(heroFrom, card.from)}${numberLayer(heroTo, card.to)}</g></g></g></g></g>
<g transform="translate(69 0)"><g opacity="0">${animate(note.opacity, 'opacity', loop, 0.004)}${transform(new Derived([pill.h, note.y], (h, y) => [0, PILL.y + h / 2 + y]), 'translate', loop, 0.02)}
<g opacity="0">${animate(cardOn, 'opacity', loop, 0.004)}
<text x="0" y="-3.742" fill="#fafafa" font-size="13" font-weight="500">${card.title}</text>
<text x="0" y="13.148" fill="#a1a1aa" font-size="12">${card.sub}</text></g></g></g>
</g>
<!-- The dot sits over the circle's edge and past the capsule, as on the real pill: outside its clip. -->
<g opacity="0">${animate(dotOn, 'opacity', loop, 0.004)}<g transform="translate(${dotAt[0]} ${dotAt[1]})"><g>${transform(rows[0].scale, 'scale', loop, 0.002)}<g transform="translate(${-dotAt[0]} ${-dotAt[1]})">${dot.svg}</g></g></g></g>
<g opacity="0">${animate(badgeOn, 'opacity', loop, 0.004)}<g transform="translate(${num(badge.rect.x)} ${num(badge.rect.y + badge.rect.height / 2)})"><g>${transform(badgeScale, 'scale', loop, 0.002)}<g transform="translate(${num(-badge.rect.x)} ${num(-(badge.rect.y + badge.rect.height / 2))})">${badge.svg}</g></g></g></g>
<g opacity="0">${animate(label, 'opacity', loop, 0.004)}${labelShell(history)}${history.svg}</g>
<g><g>${transform(new Derived([cursor], ([x, y]) => [x - 3, y - 2]), 'translate', loop, 0.05)}<g filter="url(#cursorShadow)">${CURSOR}</g></g></g>`;
  const defs = `${back.defs}${SHADOWS(W, H)}${badge.defs}${dot.defs}${history.defs}
<clipPath id="pillClip"><rect x="${PILL.x + 1}" y="${PILL.y + 1}" width="${PILL.w - 2}" height="${PILL.h - 2}" rx="${PILL.r - 1}">${box(1)}</rect></clipPath>
<filter id="railBlur" x="-50%" y="-20%" width="200%" height="140%"><feGaussianBlur stdDeviation="0">${animate(rail.blur, 'stdDeviation', loop, 0.02)}</feGaussianBlur></filter>`;
  return {name: 'notify.svg', texts: [card.title, card.sub, card.from, card.to, badge.chars.sans, history.chars.sans, ...CIRCLES.map((c) => c.value)],
    build: (font) => svg(W, H, 'HQ notifications', 'Three new decisions: the pill opens into a card and folds back, leaving an amber +3 beside the count, later a dot, and hovering lists what changed.',
      font, defs, body)};
}

export async function drawAll(capture) {
  const got = await capture();
  const clips = [glance(got.labels), notify(got)];
  const font = fontFace(clips.flatMap((c) => c.texts));
  for (const clip of clips) writeFileSync(join(OUT, clip.name), clip.build(font));
  return clips.map((c) => c.name);
}
