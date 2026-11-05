// A small headless Chrome driver: real input events, screenshots and a timed screencast.
import {spawn} from 'node:child_process';
import {mkdtempSync, rmSync, writeFileSync, mkdirSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';

const chrome = process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export async function open(url, {serveAt = null, serveBody = null, width = 1280, height = 800, scale = 2} = {}) {
  const profile = mkdtempSync(join(tmpdir(), 'hq-media-chrome-'));
  const proc = spawn(chrome, ['--headless=new', '--no-first-run', '--no-default-browser-check', '--hide-scrollbars',
    '--force-color-profile=srgb', `--user-data-dir=${profile}`, `--window-size=${width},${height}`, '--remote-debugging-port=0',
    'about:blank'], {stdio: ['ignore', 'ignore', 'pipe']});
  const ws = await new Promise((resolve, reject) => {
    let log = '';
    proc.stderr.on('data', (chunk) => {
      log += chunk;
      const match = log.match(/DevTools listening on (ws:\/\/\S+)/);
      if (match) resolve(match[1]);
    });
    setTimeout(() => reject(new Error('Chrome did not start')), 15000);
  });
  const port = new URL(ws).port;
  let target;
  for (let n = 0; n < 40 && !target; n += 1) {
    target = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((t) => t.type === 'page');
    if (!target) await sleep(100);
  }
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve) => {socket.onopen = resolve;});
  let id = 0;
  const waiting = new Map(), listeners = new Map(), errors = [];
  socket.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.id && waiting.has(data.id)) {
      waiting.get(data.id)(data.result ?? data);
      waiting.delete(data.id);
    }
    if (data.method === 'Runtime.exceptionThrown') errors.push(data.params.exceptionDetails.exception?.description || data.params.exceptionDetails.text);
    (listeners.get(data.method) || []).forEach((fn) => fn(data.params));
  };
  const send = (method, params = {}) => new Promise((resolve) => {
    id += 1;
    waiting.set(id, resolve);
    socket.send(JSON.stringify({id, method, params}));
  });
  const on = (method, fn) => listeners.set(method, [...(listeners.get(method) || []), fn]);
  // Every frame's default context, newest last, so the stage can run code inside /float (CSP forbids eval there).
  const contexts = [];
  on('Runtime.executionContextCreated', (p) => p.context.auxData?.isDefault && contexts.push(p.context));
  await send('Runtime.enable');
  await send('Page.enable');
  await send('Emulation.setDeviceMetricsOverride', {width, height, deviceScaleFactor: scale, mobile: false});
  // Serve a local file at a URL on the page's own origin, and let that page frame /float.
  // HQ forbids framing (frame-ancestors 'none'); this capture browser alone relaxes it to 'self'.
  const fake = new Map();
  on('Fetch.requestPaused', async (p) => {
    const url = p.request.url.split('#')[0];
    if (fake.has(url)) {
      return send('Fetch.fulfillRequest', {requestId: p.requestId, responseCode: 200,
        responseHeaders: [{name: 'Content-Type', value: 'text/html; charset=utf-8'}], body: Buffer.from(fake.get(url)).toString('base64')});
    }
    if (p.responseStatusCode) {
      const {body, base64Encoded} = await send('Fetch.getResponseBody', {requestId: p.requestId});
      const responseHeaders = p.responseHeaders.map((h) => (h.name.toLowerCase() === 'content-security-policy'
        ? {name: h.name, value: h.value.replace("frame-ancestors 'none'", "frame-ancestors 'self'")} : h));
      return send('Fetch.fulfillRequest', {requestId: p.requestId, responseCode: p.responseStatusCode, responseHeaders,
        body: base64Encoded ? body : Buffer.from(body).toString('base64')});
    }
    return send('Fetch.continueRequest', {requestId: p.requestId});
  });
  const serve = async (at, body) => {
    fake.set(at, body);
    const origin = new URL(at).origin;
    await send('Fetch.enable', {patterns: [...[...fake.keys()].map((urlPattern) => ({urlPattern})),
      {urlPattern: `${origin}/float*`, requestStage: 'Response'}]});
  };
  const goto = async (to) => {
    const loaded = new Promise((resolve) => on('Page.loadEventFired', resolve));
    await send('Page.navigate', {url: to});
    await loaded;
  };
  if (serveAt) await serve(serveAt, serveBody);
  if (url) await goto(url);
  const evaluate = async (expression) => {
    const out = await send('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
    if (out.exceptionDetails) throw new Error(out.exceptionDetails.exception?.description || out.exceptionDetails.text);
    return out.result?.value;
  };
  const evaluateIn = async (origin, expression) => {
    const context = [...contexts].reverse().find((c) => c.origin === origin && c.auxData.frameId !== contexts[0]?.auxData.frameId);
    const out = await send('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true, contextId: context.id});
    if (out.exceptionDetails) throw new Error(out.exceptionDetails.exception?.description || out.exceptionDetails.text);
    return out.result?.value;
  };
  const shot = async (path, clip) => {
    const {data} = await send('Page.captureScreenshot', {format: 'png', ...(clip ? {clip: {...clip, scale: 1}} : {})});
    writeFileSync(path, Buffer.from(data, 'base64'));
  };
  const mouse = (type, x, y, extra = {}) => send('Input.dispatchMouseEvent', {type, x, y, ...extra});
  // Frames arrive only when something changes, each with its time, so the encoder can space them.
  let frames = null;
  on('Page.screencastFrame', (p) => {
    send('Page.screencastFrameAck', {sessionId: p.sessionId});
    if (frames) frames.list.push({t: p.metadata.timestamp, data: p.data});
  });
  const record = async (dir) => {
    mkdirSync(dir, {recursive: true});
    frames = {dir, list: []};
    await send('Page.startScreencast', {format: 'png', everyNthFrame: 1});
  };
  const stop = async () => {
    await send('Page.stopScreencast');
    const {dir, list} = frames;
    frames = null;
    list.forEach((f, n) => writeFileSync(join(dir, `f${String(n).padStart(5, '0')}.png`), Buffer.from(f.data, 'base64')));
    writeFileSync(join(dir, 'times.json'), JSON.stringify(list.map((f) => f.t)));
    return list.length;
  };
  const close = () => {
    socket.close();
    proc.kill();
    setTimeout(() => rmSync(profile, {recursive: true, force: true}), 500);
  };
  return {send, evaluate, evaluateIn, shot, mouse, record, stop, close, errors};
}
