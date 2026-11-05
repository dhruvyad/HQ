// Regenerates the README's pictures from the real UI, on made-up data, in headless Chrome.
//
//   node docs/media/make/make.mjs              every picture
//   node docs/media/make/make.mjs hero clips   just these: hero, clips, drag, settings, dashboard
//
// hero.webp and drag.webp are recorded at 2x; glance.svg and notify.svg are drawn by vector.mjs;
// settings.png and dashboard.png are 2x screenshots; loop.svg is drawn by hand. Needs Google
// Chrome (or CHROME=/path/to/chrome) and ffmpeg with libwebp. Each take runs its own HQ on a
// throwaway inbox and a free port, so it never touches your inbox, your screen or a real agent.
// HQ_MEDIA_KEEP=1 keeps a recording's raw frames.
import {execFileSync, spawn} from 'node:child_process';
import {mkdtempSync, readFileSync, rmSync, writeFileSync} from 'node:fs';
import {createServer} from 'node:net';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {open, sleep} from './chrome.mjs';
import {drawAll} from './vector.mjs';

const HERE = dirname(fileURLToPath(import.meta.url)), ROOT = resolve(HERE, '../../..'), OUT = resolve(HERE, '..');
const CIRCLES = ['needs', 'agents', 'claude', 'codex'];
const FPS = 30, SCALE = Number(process.env.HQ_MEDIA_SCALE || 2);

// A throwaway HQ: its own inbox, seeded with fixtures.py, on a free port, with no live workers.
export async function startHQ() {
  const home = mkdtempSync(join(tmpdir(), 'hq-media-'));
  const env = {...process.env, HQ_HOME: home, HQ_FLOAT_LAB: '1'};
  const fixtures = (...args) => execFileSync('python3', [join(HERE, 'fixtures.py'), ...args], {env});
  fixtures('seed');
  const port = await new Promise((done) => {
    const probe = createServer().listen(0, '127.0.0.1', () => {
      const {port: free} = probe.address();
      probe.close(() => done(free));
    });
  });
  const server = spawn('python3', [join(ROOT, 'bin/hq'), 'serve', '--port', String(port)], {env, stdio: 'ignore'});
  const origin = `http://127.0.0.1:${port}`;
  for (let n = 0; ; n += 1) {
    try {
      if ((await (await fetch(`${origin}/api/health`)).json()).app === 'hq') break;
    } catch {}
    if (n > 50) throw new Error('The throwaway HQ did not start');
    await sleep(100);
  }
  const stop = () => {
    server.kill();
    rmSync(home, {recursive: true, force: true});
  };
  return {origin, fixtures, stop};
}

// A fake display with the real /float page in a frame, a drawn cursor, and real input events.
// It is laid out in display points and scaled up whole, so frames come out at SCALE times the size.
export async function stage(hq, {width, height, app, dock = 'top-left'}) {
  const at = `${hq.origin}/__stage`;
  const page = await open(at, {width: width * SCALE, height: height * SCALE, scale: 1, serveAt: at,
    serveBody: readFileSync(join(HERE, 'stage.html'), 'utf8')});
  await page.evaluate(`setup(${width}, ${height}, ${SCALE})`);
  if (app) await page.evaluate(`fillApp(${JSON.stringify(app)})`);
  const display = {x: 0, y: 0, width, height, visible: {x: 0, y: 0, width, height}};
  await page.evaluate(`boot('${hq.origin}/float', ${JSON.stringify(display)}, '${dock}', ${JSON.stringify({circles: CIRCLES})}).then(() => stage.booted = true); 1`);
  for (let n = 0; !(await page.evaluate('stage.booted')); n += 1) {
    if (n > 60) throw new Error('The panel did not start');
    await sleep(100);
  }
  const s = {page, at: {x: width + 60, y: height + 60}, pressed: false, synthetic: false};
  const inFrame = (expression) => page.evaluate(`(() => {const document = frame.contentDocument; return (${expression});})()`);
  s.inFrame = inFrame;
  s.wait = sleep;
  s.until = async (expression, ms = 12000) => {
    const start = Date.now();
    while (!(await inFrame(expression))) {
      if (Date.now() - start > ms) throw new Error(`Timed out waiting for ${expression}`);
      await sleep(30);
    }
  };
  // Real events in viewport pixels, except in a drag, which reads screen points (see synth).
  const mouse = (type, buttons) => (s.synthetic
    ? page.evaluate(`synth('${{mouseMoved: 'mousemove', mousePressed: 'mousedown', mouseReleased: 'mouseup'}[type]}', ${s.at.x}, ${s.at.y}, ${buttons})`)
    : page.mouse(type, s.at.x * SCALE, s.at.y * SCALE, {button: type === 'mouseMoved' && !buttons ? 'none' : 'left', buttons, clickCount: type === 'mouseMoved' ? 0 : 1}));
  s.cursor = async (x, y) => {
    s.at = {x, y};
    await page.evaluate(`cursorTo(${x}, ${y}); pointerAt(${x}, ${y})`);
    await mouse('mouseMoved', s.pressed ? 1 : 0);
  };
  // Eased like a hand: quick in the middle, careful at the end.
  s.glide = async (x, y, ms = 600) => {
    const from = {...s.at}, start = Date.now();
    for (;;) {
      const t = Math.min(1, (Date.now() - start) / ms), e = t < 0.5 ? 4 * t ** 3 : 1 - (-2 * t + 2) ** 3 / 2;
      await s.cursor(Math.round(from.x + (x - from.x) * e), Math.round(from.y + (y - from.y) * e));
      if (t === 1) return;
      await sleep(12);
    }
  };
  s.press = async () => {
    s.pressed = true;
    await page.evaluate('cursorDown(true)');
    await mouse('mousePressed', 1);
  };
  s.release = async () => {
    s.pressed = false;
    await mouse('mouseReleased', 0);
    await page.evaluate('cursorDown(false)');
  };
  s.click = async () => {
    await s.press();
    await sleep(90);
    await s.release();
  };
  s.rect = (selector) => page.evaluate(`rectOf(${JSON.stringify(selector)})`);
  // Waits for it to stop moving, since clicking mid-spring lands somewhere else.
  s.glideTo = async (selector, ms, dx = 0, dy = 0) => {
    let r = null, last = '';
    for (let n = 0; n < 60; n += 1) {
      r = await s.rect(selector);
      if (r && JSON.stringify(r) === last) break;
      last = JSON.stringify(r);
      await sleep(60);
    }
    if (!r) throw new Error(`Nothing matches ${selector}`);
    await s.glide(Math.round(r.x + r.width / 2 + dx), Math.round(r.y + r.height / 2 + dy), ms);
  };
  // Let startup settle before recording: the first reading of agents, and any card it raised.
  s.settle = async () => {
    const quiet = `watchAgents.settled && [...agentMemory.values()].every((v) => v.running === v.candidate) && !notice && !pending.length`;
    for (let n = 0; !(await page.evaluateIn(hq.origin, quiet)); n += 1) {
      if (n > 200) throw new Error('The panel never settled');
      await sleep(100);
    }
    await sleep(900);
  };
  const noting = `document.querySelector('#pill').classList.contains('noting')`;
  s.quiet = async () => {
    await s.until(`!${noting}`);
    await sleep(700);
  };
  await s.cursor(s.at.x, s.at.y);
  return s;
}

// Screencast frames arrive with their times, about 60 a second; space them evenly and keep every
// pixel as animated WebP: full colour, so gradients never band. Lossy at q95 tracks the source
// closely; a clip that dims the whole screen trails under lossy blending, so that one is lossless.
function webp(frames, out, {lossless = false} = {}) {
  const {times, end} = JSON.parse(readFileSync(join(frames, 'times.json'), 'utf8'));
  const hold = end - times[times.length - 1];
  const lines = times.flatMap((t, n) => [`file '${join(frames, `f${String(n).padStart(5, '0')}.png`)}'`,
    `duration ${((times[n + 1] ?? t + Math.max(hold, 0.04)) - t).toFixed(4)}`]);
  lines.push(lines[lines.length - 2]);
  writeFileSync(join(frames, 'list.txt'), `${lines.join('\n')}\n`);
  const quality = lossless ? ['-lossless', '1', '-compression_level', '4'] : ['-lossless', '0', '-q:v', '95', '-compression_level', '6', '-preset', 'picture'];
  execFileSync('ffmpeg', ['-loglevel', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', join(frames, 'list.txt'),
    '-vf', `fps=${FPS}`, '-c:v', 'libwebp_anim', ...quality, '-loop', '0', join(OUT, out)]);
}

async function take(name, size, scene, out, encode = {}) {
  const hq = await startHQ(), frames = mkdtempSync(join(tmpdir(), 'hq-media-frames-'));
  let s;
  try {
    s = await stage(hq, size);
    await s.settle();
    await scene(s, hq, () => s.page.record(frames));
    await s.page.stop();
    if (s.page.errors.length) throw new Error(`${name}: ${s.page.errors.join('; ')}`);
    webp(frames, out, encode);
  } finally {
    s?.page.close();
    hq.stop();
    if (process.env.HQ_MEDIA_KEEP) console.log(`${name} frames kept in ${frames}`);
    else rmSync(frames, {recursive: true, force: true});
  }
}

const TAKES = {
  // A decision arrives, you open it from the pill, answer, and it flies home into the count.
  hero: () => take('hero', {width: 800, height: 620, app: {x: 210, y: 52, width: 640, height: 600}}, async (s, hq, record) => {
    await s.glide(680, 530, 10);
    await record();
    await s.wait(900);
    hq.fixtures('add');
    await s.wait(1100);
    await s.glide(430, 300, 900);
    await s.quiet();
    await s.glideTo('#pill', 700, 0, -40);
    await s.click();
    await s.until(`document.body.dataset.mode === 'list'`);
    // Move off the pill at once, or its label sits over the list's header.
    await s.glide(s.at.x + 40, s.at.y + 70, 250);
    const row = await s.inFrame(`[...document.querySelectorAll('#main [data-item]')].find((r) => r.textContent.includes('Name the new')).dataset.item`);
    await s.glideTo(`#main [data-item="${row}"]`, 650, -60);
    await s.wait(150);
    await s.click();
    await s.until(`document.body.dataset.mode === 'item'`);
    await s.wait(900);
    await s.glideTo('#detail [data-choice="Reports"]', 650, -40);
    await s.wait(150);
    await s.click();
    await s.wait(500);
    await s.glideTo('#detail .send', 600);
    await s.wait(150);
    await s.click();
    await s.wait(1550);
    await s.glideTo('#pill', 650, 0, -40);
    await s.wait(200);
    await s.click();
    await s.wait(900);
    await s.glide(680, 530, 900);
    await s.wait(1200);
  }, 'hero.webp'),

  // The pill's own clips, hover and notifications, drawn as animated SVG (vector.mjs).
  clips: async () => console.log(drawAll().join(', ')),

  // Drag it to the bottom, where it lies flat, and back to the corner.
  drag: () => take('drag', {width: 800, height: 500, app: {x: 150, y: 40, width: 600, height: 420}}, async (s, hq, record) => {
    s.synthetic = true;
    await s.glide(640, 360, 10);
    await record();
    await s.wait(700);
    await s.glideTo('#pill', 800);
    await s.wait(250);
    await s.press();
    await s.glide(s.at.x + 12, s.at.y + 10, 120);
    await s.glide(340, 380, 900);
    await s.glide(400, 470, 600);
    await s.wait(350);
    await s.release();
    await s.wait(700);
    await s.glide(520, 350, 600);
    await s.wait(700);
    await s.glideTo('#pill', 700);
    await s.wait(250);
    await s.press();
    await s.glide(s.at.x - 12, s.at.y - 10, 120);
    await s.glide(140, 120, 900);
    await s.glide(40, 40, 500);
    await s.wait(350);
    await s.release();
    await s.wait(700);
    await s.glide(640, 360, 900);
    await s.wait(900);
  }, 'drag.webp', {lossless: true}),

  // Choosing circles, notifications and shortcuts.
  settings: async () => {
    const hq = await startHQ();
    let s;
    try {
      s = await stage(hq, {width: 520, height: 900, app: {x: 260, y: 40, width: 400, height: 700}});
      await s.settle();
      await s.page.evaluate(`toPage({type: 'customize'})`);
      await s.until(`document.body.dataset.mode === 'settings'`);
      await sleep(1200);
      const box = await s.rect('#box');
      await s.page.shot(join(OUT, 'settings.png'), {x: 0, y: 0, width: Math.ceil(box.x + box.width + 16) * SCALE, height: Math.ceil(box.y + box.height + 16) * SCALE});
    } finally {
      s?.page.close();
      hq.stop();
    }
  },

  // The dashboard, with a decision open beside the list.
  dashboard: async () => {
    const hq = await startHQ(), raw = join(tmpdir(), `hq-media-dashboard-${process.pid}.png`);
    const page = await open(`${hq.origin}/`, {width: 1440, height: 900});
    try {
      await sleep(1500);
      const id = await page.evaluate(`state.items.find((i) => i.title.startsWith('Choose the launch')).id`);
      await page.evaluate(`location.hash = '#/?item=${id}'`);
      await sleep(1500);
      if (page.errors.length) throw new Error(page.errors.join('; '));
      await page.shot(raw);
      execFileSync('ffmpeg', ['-loglevel', 'error', '-y', '-i', raw, '-vf', 'scale=1600:-1:flags=lanczos', join(OUT, 'dashboard.png')]);
    } finally {
      page.close();
      hq.stop();
      rmSync(raw, {force: true});
    }
  },
};

async function main() {
  const wanted = process.argv.slice(2).length ? process.argv.slice(2) : Object.keys(TAKES);
  for (const name of wanted) {
    if (!TAKES[name]) throw new Error(`No picture called ${name}. Try: ${Object.keys(TAKES).join(', ')}`);
    const started = Date.now();
    await TAKES[name]();
    console.log(`${name}  ${((Date.now() - started) / 1000).toFixed(0)} s`);
  }
}
if (import.meta.url === pathToFileURL(process.argv[1]).href) await main();
