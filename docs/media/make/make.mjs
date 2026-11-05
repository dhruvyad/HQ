// Regenerates the README's pictures from the real UI, on made-up data, in headless Chrome.
//
//   node docs/media/make/make.mjs                 every picture
//   node docs/media/make/make.mjs hero drag       just these
//
// Needs Google Chrome (or CHROME=/path/to/chrome) and ffmpeg. Each take runs its own HQ on a
// throwaway inbox and a free port, so it never touches your inbox, your screen or a real agent.
// loop.svg is drawn by hand; edit it directly.
import {execFileSync, spawn} from 'node:child_process';
import {mkdtempSync, readFileSync, rmSync, statSync, writeFileSync} from 'node:fs';
import {createServer} from 'node:net';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {open, sleep} from './chrome.mjs';

const HERE = dirname(fileURLToPath(import.meta.url)), ROOT = resolve(HERE, '../../..'), OUT = resolve(HERE, '..');
const CIRCLES = ['needs', 'agents', 'claude', 'codex'];

// A throwaway HQ: its own inbox, seeded with fixtures.py, on a free port, with no live workers.
async function startHQ() {
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
async function stage(hq, {width, height, app, dock = 'top-left', scale = 2}) {
  const at = `${hq.origin}/__stage`;
  const page = await open(at, {width, height, scale, serveAt: at, serveBody: readFileSync(join(HERE, 'stage.html'), 'utf8')});
  if (app) await page.evaluate(`fillApp(${JSON.stringify(app)})`);
  // The page reads the pointer in screen points, so place the display where the viewport is.
  await page.mouse('mouseMoved', 5, 5);
  const offset = await page.evaluate('stage.offset');
  const display = {x: offset.x, y: offset.y, width, height, visible: {x: 0, y: 0, width, height}};
  await page.evaluate(`boot('${hq.origin}/float', ${JSON.stringify(display)}, '${dock}', ${JSON.stringify({circles: CIRCLES})}).then(() => stage.booted = true); 1`);
  for (let n = 0; !(await page.evaluate('stage.booted')); n += 1) {
    if (n > 60) throw new Error('The panel did not start');
    await sleep(100);
  }
  const s = {page, at: {x: width + 60, y: height + 60}, pressed: false};
  const inFrame = (expression) => page.evaluate(`(() => {const document = frame.contentDocument; return (${expression});})()`);
  s.until = async (expression, ms = 12000) => {
    const start = Date.now();
    while (!(await inFrame(expression))) {
      if (Date.now() - start > ms) throw new Error(`Timed out waiting for ${expression}`);
      await sleep(40);
    }
  };
  s.inFrame = inFrame;
  s.cursor = async (x, y) => {
    s.at = {x, y};
    await page.evaluate(`cursorTo(${x}, ${y}); pointerAt(${x}, ${y})`);
    await page.mouse('mouseMoved', x, y, s.pressed ? {button: 'left', buttons: 1} : {});
  };
  // Eased like a hand: quick in the middle, careful at the end.
  s.glide = async (x, y, ms = 600) => {
    const from = {...s.at}, start = Date.now();
    for (;;) {
      const t = Math.min(1, (Date.now() - start) / ms), e = t < 0.5 ? 4 * t ** 3 : 1 - (-2 * t + 2) ** 3 / 2;
      await s.cursor(Math.round(from.x + (x - from.x) * e), Math.round(from.y + (y - from.y) * e));
      if (t === 1) return;
      await sleep(14);
    }
  };
  s.press = async () => {
    s.pressed = true;
    await page.evaluate('cursorDown(true)');
    await page.mouse('mousePressed', s.at.x, s.at.y, {button: 'left', buttons: 1, clickCount: 1});
  };
  s.release = async () => {
    s.pressed = false;
    await page.mouse('mouseReleased', s.at.x, s.at.y, {button: 'left', buttons: 0, clickCount: 1});
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
  // Let startup settle: the panel announces agents that were already running a few seconds
  // after it starts, so wait for that card to come and go before recording.
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

// Screencast frames arrive only when something changes, each with its time; space them evenly.
function gif(frames, out, width, fps = 25) {
  const times = JSON.parse(readFileSync(join(frames, 'times.json'), 'utf8'));
  const lines = times.flatMap((t, n) => [`file '${join(frames, `f${String(n).padStart(5, '0')}.png`)}'`,
    `duration ${((times[n + 1] ?? t + 0.04) - t).toFixed(4)}`]);
  lines.push(lines[lines.length - 2]);
  writeFileSync(join(frames, 'list.txt'), `${lines.join('\n')}\n`);
  // One palette for the whole clip and only changed rectangles re-dithered, so the still
  // background stays identical frame to frame and the file stays small.
  const graph = `fps=${fps},scale=${width}:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];`
    + '[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle';
  execFileSync('ffmpeg', ['-loglevel', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', join(frames, 'list.txt'),
    '-vf', graph, '-loop', '0', join(OUT, out)]);
}

async function take(name, size, scene, out, width) {
  const hq = await startHQ(), frames = mkdtempSync(join(tmpdir(), 'hq-media-frames-'));
  let s;
  try {
    s = await stage(hq, size);
    await s.settle();
    await scene(s, hq, () => s.page.record(frames));
    await s.page.stop();
    if (s.page.errors.length) throw new Error(`${name}: ${s.page.errors.join('; ')}`);
    gif(frames, out, width);
  } finally {
    s?.page.close();
    hq.stop();
    rmSync(frames, {recursive: true, force: true});
  }
}

const TAKES = {
  // A decision arrives, you open it from the pill, answer, and it flies home into the count.
  hero: () => take('hero', {width: 800, height: 620, app: {x: 210, y: 52, width: 640, height: 600}}, async (s, hq, record) => {
    await s.glide(680, 530, 10);
    await record();
    await sleep(900);
    hq.fixtures('add');
    await sleep(1100);
    await s.glide(430, 300, 900);
    await s.quiet();
    await s.glideTo('#pill', 700, 0, -40);
    await s.click();
    await s.until(`document.body.dataset.mode === 'list'`);
    // Move off the pill at once, or its label sits over the list's header.
    await s.glide(s.at.x + 40, s.at.y + 70, 250);
    const row = await s.inFrame(`[...document.querySelectorAll('#main [data-item]')].find((r) => r.textContent.includes('Name the new')).dataset.item`);
    await s.glideTo(`#main [data-item="${row}"]`, 650, -60);
    await sleep(150);
    await s.click();
    await s.until(`document.body.dataset.mode === 'item'`);
    await sleep(900);
    await s.glideTo('#detail [data-choice="Reports"]', 650, -40);
    await sleep(150);
    await s.click();
    await sleep(500);
    await s.glideTo('#detail .send', 600);
    await sleep(150);
    await s.click();
    await sleep(1550);
    await s.glideTo('#pill', 650, 0, -40);
    await sleep(200);
    await s.click();
    await sleep(900);
    await s.glide(680, 530, 900);
    await sleep(1200);
  }, 'hero.gif', 800),

  // Hovering names each circle.
  glance: () => take('glance', {width: 480, height: 170, app: {x: 250, y: 26, width: 300, height: 200}}, async (s, hq, record) => {
    await s.glide(410, 130, 10);
    await record();
    await sleep(600);
    const rows = await s.inFrame(`[...document.querySelectorAll('#rail .f-row[data-metric]')].map((r) => r.dataset.metric)`);
    for (const [n, metric] of rows.entries()) {
      await s.glideTo(`#rail [data-metric="${metric}"] .f-circle`, n ? 380 : 800, 7, 6);
      await sleep(1100);
    }
    await s.glide(410, 130, 800);
    await sleep(700);
  }, 'glance.gif', 960),

  // An item arrives, then an agent finishes one: the capsule says so and folds back.
  notify: () => take('notify', {width: 420, height: 150, app: {x: 200, y: 24, width: 300, height: 200}}, async (s, hq, record) => {
    await record();
    await sleep(1000);
    hq.fixtures('add');
    await sleep(500);
    await s.quiet();
    await sleep(600);
    hq.fixtures('resolve', 'Renew the example.com');
    await sleep(500);
    await s.quiet();
    await sleep(600);
  }, 'notify.gif', 840),

  // Drag it to the bottom, where it lies flat, and back to the corner.
  drag: () => take('drag', {width: 800, height: 500, app: {x: 150, y: 40, width: 600, height: 420}}, async (s, hq, record) => {
    await s.glide(640, 360, 10);
    await record();
    await sleep(700);
    await s.glideTo('#pill', 800);
    await sleep(250);
    await s.press();
    await s.glide(s.at.x + 12, s.at.y + 10, 120);
    await s.glide(340, 380, 900);
    await s.glide(400, 470, 600);
    await sleep(350);
    await s.release();
    await sleep(700);
    await s.glide(520, 350, 600);
    await sleep(700);
    await s.glideTo('#pill', 700);
    await sleep(250);
    await s.press();
    await s.glide(s.at.x - 12, s.at.y - 10, 120);
    await s.glide(140, 120, 900);
    await s.glide(40, 40, 500);
    await sleep(350);
    await s.release();
    await sleep(700);
    await s.glide(640, 360, 900);
    await sleep(900);
  }, 'drag.gif', 800),

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
      await s.page.shot(join(OUT, 'settings.png'), {x: 0, y: 0, width: Math.ceil(box.x + box.width + 16), height: Math.ceil(box.y + box.height + 16)});
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

const wanted = process.argv.slice(2).length ? process.argv.slice(2) : Object.keys(TAKES);
for (const name of wanted) {
  if (!TAKES[name]) throw new Error(`No picture called ${name}. Try: ${Object.keys(TAKES).join(', ')}`);
  const started = Date.now();
  await TAKES[name]();
  const file = {hero: 'hero.gif', glance: 'glance.gif', notify: 'notify.gif', drag: 'drag.gif', settings: 'settings.png', dashboard: 'dashboard.png'}[name];
  console.log(`${file}  ${(statSync(join(OUT, file)).size / 1e6).toFixed(2)} MB  ${((Date.now() - started) / 1000).toFixed(0)} s`);
}
