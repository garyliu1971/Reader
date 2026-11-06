// Service worker update prompt. Real Chromium; serves several builds of the app from one port and swaps which one is
// "deployed" while the page stays open. See lib.mjs for setup. Needs git (for the pre-prompt build) and tar.
//   APP_URL=http://127.0.0.1:8133/reader/index.html SMOKE_TMP=... node web/tools/browser-smoke/swupdate.mjs
// APP_URL must be in a sub-directory (like the real /reader/): the "other app on the same origin" case needs a page outside the scope.
import { launch, SP, SHOTS, URL0, wait } from './lib.mjs';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

fs.mkdirSync(SHOTS, { recursive: true });
let fails = 0;
const ok = (cond, msg, extra = '') => { console.log(`${cond ? 'PASS' : 'FAIL'}  ${msg} ${extra}`); if (!cond) fails++; };

const WEB = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const VER = SP + '/versions';
fs.rmSync(VER, { recursive: true, force: true });
fs.mkdirSync(VER, { recursive: true });

// A "build" = a copy of web/ with its own cache name (so sw.js bytes differ) and a marker the page can report.
function build(name, from) {
  const dir = `${VER}/${name}`;
  fs.cpSync(from, dir, { recursive: true, filter: (s) => !/node_modules/.test(s) });
  const sw = path.join(dir, 'sw.js');
  fs.writeFileSync(sw, fs.readFileSync(sw, 'utf8').replace(/const CACHE = '[^']*';/, `const CACHE = 'pyreader-web-${name}';`));
  const app = path.join(dir, 'js/app.js');
  fs.appendFileSync(app, `\nglobalThis.__BUILD = '${name}';\n`);
  return dir;
}
// the build as deployed before this feature: fork/main's web/ before the prompt landed (git archive of the base commit)
function legacyBuild(name) {
  const ROOT = path.join(WEB, '..');
  const base = execFileSync('git', ['-C', ROOT, 'merge-base', 'HEAD', 'fork/main'], { encoding: 'utf8' }).trim();
  const tar = `${VER}/${name}.tar`;
  fs.writeFileSync(tar, execFileSync('git', ['-C', ROOT, 'archive', '--format=tar', base, 'web'], { maxBuffer: 512 * 1024 * 1024 }));
  const x = `${VER}/${name}-x`;
  fs.mkdirSync(x);
  execFileSync('tar', ['-xf', `../${name}.tar`], { cwd: x });   // relative: GNU tar reads "C:" as a host
  return build(name, `${x}/web`);
}
for (const n of ['v1', 'v2', 'v3']) build(n, WEB);
legacyBuild('old');
if (/swupdate/.test(fs.readFileSync(`${VER}/old/js/app.js`, 'utf8'))) { console.log('FAIL  legacy build already contains the prompt (merge-base wrong?)'); process.exit(1); }

const BASE = new URL('./', URL0).pathname;   // e.g. /reader/
if (BASE === '/') { console.log('FAIL  APP_URL must be in a sub-directory, e.g. http://127.0.0.1:8133/reader/index.html'); process.exit(1); }
let current = 'v1';
let slowShell = 0;   // ms to hold back css/app.css: makes a worker's install last long enough to load a page in the middle of it
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.webmanifest': 'application/manifest+json', '.svg': 'image/svg+xml', '.png': 'image/png' };
const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://x');
  let p = decodeURIComponent(u.pathname);
  if (p.endsWith('/')) p += 'index.html';
  // a page of some OTHER app on the same origin (like the WebDAV host's web UI), outside the reader's scope
  if (p === '/other/index.html') { res.writeHead(200, { 'Content-Type': 'text/html' }); res.end('<!doctype html><title>other</title><p>not the reader'); return; }
  if (!p.startsWith(BASE)) { res.writeHead(404); res.end('nf'); return; }
  p = p.slice(BASE.length - 1);
  const f = path.join(VER, current, p);
  if (!f.startsWith(path.join(VER, current)) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) { res.writeHead(404); res.end('nf'); return; }
  const send = () => {
    res.writeHead(200, { 'Content-Type': MIME[path.extname(f)] || 'application/octet-stream', 'Cache-Control': 'no-cache' });
    fs.createReadStream(f).pipe(res);
  };
  if (slowShell && p.endsWith('/css/app.css')) setTimeout(send, slowShell); else send();
});
const port = Number(new URL(URL0).port);
await new Promise((r) => server.listen(port, '127.0.0.1', r));

const until = async (fn, ms = 15000, step = 150) => {
  const t0 = Date.now();
  for (;;) {
    try { const v = await fn(); if (v) return v; } catch { /* page navigating */ }
    if (Date.now() - t0 > ms) return null;
    await wait(step);
  }
};

try {
  // ============================================================ new -> new: prompt, no auto reload, click swaps versions
  const { browser, context, page, logs } = await launch({ width: 900, height: 700 });
  await page.goto(URL0);
  await page.waitForSelector('.shelf-head');
  ok(await until(() => page.evaluate(() => globalThis.__BUILD)) === 'v1', 'page runs build v1');
  ok(await until(() => page.evaluate(() => !!navigator.serviceWorker.controller)), 'service worker controls the page');
  await wait(800);
  ok(await page.locator('.update-bar').count() === 0, 'first install: no update prompt');
  ok(await page.evaluate(() => caches.keys()).then((k) => k.join() === 'pyreader-web-v1'), 'cache of v1 only');
  // an unrelated same-origin tab outside the app's scope must not count as a window that cannot show the prompt
  const other = await context.newPage();
  await other.goto(new URL('/other/index.html', URL0).href);
  await page.bringToFront();
  const navCount = () => page.evaluate(() => performance.getEntriesByType('navigation').length + ':' + performance.timeOrigin);
  const origin0 = await navCount();

  current = 'v2';                                                           // "deploy" v2
  // returning to the tab triggers a (throttled) check: pretend 6 minutes passed
  await page.evaluate(() => { const real = Date.now; Date.now = () => real() + 6 * 60 * 1000; document.dispatchEvent(new Event('visibilitychange')); });
  ok(await until(() => page.locator('.update-bar').count().then((n) => n === 1), 20000), 'deploying v2: "new version" bar appears after the visibility check');
  console.log('   bar text:', JSON.stringify(await page.locator('.update-bar').innerText()));
  ok((await page.locator('.update-bar').innerText()).includes('有新版本可用'), 'bar is in Chinese and says so');
  await page.screenshot({ path: SHOTS + '/swupdate-bar.png' });
  const w = await page.evaluate(async () => { const r = await navigator.serviceWorker.getRegistration(); return { waiting: !!r.waiting, active: r.active && r.active.state }; });
  ok(w.waiting && w.active === 'activated', 'new worker is installed and WAITING (did not skipWaiting by itself)', JSON.stringify(w));
  await wait(2500);
  ok(await page.evaluate(() => globalThis.__BUILD) === 'v1' && (await navCount()) === origin0, 'page not reloaded by itself; still running v1');
  ok(await page.evaluate(() => caches.keys()).then((k) => k.includes('pyreader-web-v1')), 'old cache generation still there while waiting (lazy imports stay consistent)');

  // while v2 waits the old worker must keep serving v1 bytes: no revalidation write of the new deploy into its cache
  const bytes = () => page.evaluate(async () => (await (await fetch('js/app.js')).text()).match(/__BUILD = '(\w+)'/)[1]);
  const seen = [await bytes()]; await wait(700); seen.push(await bytes()); await wait(700); seen.push(await bytes());
  ok(seen.every((b) => b === 'v1'), 'while a new worker waits, repeated loads of a module still return the running version', seen.join());

  // narrow phone: the pill keeps its full width and stays on screen; in the reader it steps aside for the controls
  await page.setViewportSize({ width: 360, height: 640 });
  const bb = await page.locator('.update-bar').boundingBox();
  ok(bb && bb.x >= 0 && bb.x + bb.width <= 360 && bb.height < 100, 'bar fits a 360px screen', JSON.stringify(bb));
  const vis = (cls) => page.evaluate((c) => {
    const r = document.getElementById('reader');
    r.hidden = false; r.className = 'view reader ' + c;
    const bar = document.querySelector('.update-bar'); const cs = getComputedStyle(bar);
    const out = { v: cs.visibility, b: parseFloat(cs.bottom) };
    r.hidden = true; r.className = 'view reader';
    return out;
  }, cls);
  const rb = await vis('bars-on'); const rt = await vis('tts-on'); const rn = await vis('');
  ok(rb.v === 'hidden' && rn.v === 'visible', 'reader controls shown: bar is hidden, not on top of them', JSON.stringify([rb, rn]));
  ok(rt.b > rn.b + 60, 'read-aloud pill showing: bar sits above it', JSON.stringify([rt, rn]));
  await page.setViewportSize({ width: 900, height: 700 });

  // dismiss: bar goes away, nothing else changes
  await page.locator('.update-bar button', { hasText: '稍后' }).click();
  ok(await page.locator('.update-bar').count() === 0 && await page.evaluate(() => globalThis.__BUILD) === 'v1', '稍后 dismisses the bar without reloading');

  // a repeated check does not resurrect a dismissed bar for the same update
  await page.evaluate(() => { const real = Date.now; Date.now = () => real() + 20 * 60 * 1000; document.dispatchEvent(new Event('visibilitychange')); });
  await wait(1500);
  ok(await page.locator('.update-bar').count() === 0, 'dismissed bar stays dismissed on the next check');

  // deploy v3 as well: still ONE waiting worker (v3 replaces v2); read-aloud active -> click asks first
  current = 'v3';
  await page.evaluate(() => { const real = Date.now; Date.now = () => real() + 40 * 60 * 1000; document.dispatchEvent(new Event('visibilitychange')); });
  await until(() => page.evaluate(async () => { const r = await navigator.serviceWorker.getRegistration(); return r.waiting && r.waiting.scriptURL; }));
  // the first tab dismissed its bar (per page session); a fresh tab shows it again
  const page2 = await context.newPage();
  await page2.goto(URL0);
  await page2.waitForSelector('.shelf-head');
  ok(await until(() => page2.evaluate(() => globalThis.__BUILD)), 'second tab loads');
  const b2 = await page2.evaluate(() => globalThis.__BUILD);
  console.log('   second tab runs', b2);
  // The second tab is controlled by the still-active v1 worker, so it serves v1 and sees v3 waiting.
  ok(await until(() => page2.locator('.update-bar').count().then((n) => n === 1), 15000), 'a page opened while a worker is waiting shows the bar at once');
  await page2.evaluate(() => { window.__pyreader.reader.tts = { state: 'playing', stop() {} }; });
  await page2.evaluate(() => { const b = [...document.querySelectorAll('.update-bar button')].find((x) => x.textContent === '刷新'); b.click(); b.click(); b.click(); });   // impatient taps
  ok(await until(() => page2.locator('.panel', { hasText: '正在朗读' }).count().then((n) => n === 1), 5000), 'while reading aloud, 刷新 asks for confirmation first');
  await wait(400);
  ok(await page2.locator('.panel', { hasText: '正在朗读' }).count() === 1, 'repeated taps do not stack a second confirmation');
  await page2.screenshot({ path: SHOTS + '/swupdate-tts-confirm.png' });
  await page2.locator('.panel button', { hasText: '取消' }).click();
  await wait(1500);
  ok(await page2.evaluate(() => globalThis.__BUILD) === b2 && await page2.locator('.update-bar').count() === 1, 'cancelling keeps reading: no reload, bar still there');
  ok(await page2.locator('.update-bar button', { hasText: '刷新' }).isEnabled(), 'cancelling re-enables 刷新');
  await page2.evaluate(() => { window.__pyreader.reader.tts = null; });

  // click for real: worker activates, page reloads into the new build, cache generations swapped
  await page2.locator('.update-bar button', { hasText: '刷新' }).click();
  ok(await until(() => page2.evaluate(() => globalThis.__BUILD === 'v3'), 20000), '刷新 -> page reloads into the new build (v3)');
  ok(await until(() => page2.evaluate(() => caches.keys()).then((k) => k.join() === 'pyreader-web-v3')), 'old cache generation deleted after the swap');
  ok(await page2.locator('.update-bar').count() === 0, 'no bar after the swap');
  const ctl = await page2.evaluate(() => navigator.serviceWorker.controller && navigator.serviceWorker.controller.state);
  ok(ctl === 'activated', 'new worker controls the page');
  // no reload loop: stays on v3, one navigation
  await wait(2500);
  ok(await page2.evaluate(() => globalThis.__BUILD) === 'v3' && await page2.locator('.update-bar').count() === 0, 'stable after the reload (no loop)');
  // the first tab (still the old JS) notices the swap and offers the reload instead of doing it
  ok(await until(() => page.locator('.update-bar').count().then((n) => n === 1), 5000), 'the other open tab is now stale: prompted again even though it had dismissed the bar');
  ok(await page.evaluate(() => globalThis.__BUILD) === 'v1', 'other tab still on v1 (not reloaded behind the reader\'s back)');
  const real = logs.filter((l) => !/favicon|Failed to load resource/.test(l));
  ok(real.length === 0, 'no console errors/warnings', JSON.stringify(real));
  await context.close(); await browser.close();

  // ============================================================ page opened while the new worker is ALREADY installing
  current = 'v1';
  const M = await launch({ width: 900, height: 700 });
  const mp = M.page;
  await mp.goto(URL0);
  await mp.waitForSelector('.shelf-head');
  ok(await until(() => mp.evaluate(() => !!navigator.serviceWorker.controller)), 'mid-install case: v1 controls the page');
  await wait(800);
  current = 'v2'; slowShell = 3000;                                           // deploy v2; its install now takes >3 s
  await mp.reload();                                                          // the navigation-time update check starts the install
  await mp.waitForSelector('.shelf-head');
  ok(await until(() => mp.evaluate(() => globalThis.__BUILD)) === 'v1', 'reloaded page still runs v1 (v2 not active yet)');
  ok(await until(() => mp.evaluate(async () => !!(await navigator.serviceWorker.getRegistration()).waiting), 20000), 'v2 finished installing in the background');
  ok(await until(() => mp.locator('.update-bar').count().then((n) => n === 1), 8000), 'bar shows for a worker that was already installing when the page started (no later trigger needed)');
  slowShell = 0;
  const mreal = M.logs.filter((l) => !/favicon|Failed to load resource/.test(l));
  ok(mreal.length === 0, 'no console errors in the mid-install case', JSON.stringify(mreal));
  await M.context.close(); await M.browser.close();

  // ============================================================ old (pre-prompt) page + new worker, and the reverse
  current = 'old';
  const L = await launch({ width: 900, height: 700 });
  const lp = L.page;
  await lp.goto(URL0);
  await lp.waitForSelector('.shelf-head');
  ok(await until(() => lp.evaluate(() => globalThis.__BUILD)) === 'old', 'legacy build loads');
  ok(await until(() => lp.evaluate(() => !!navigator.serviceWorker.controller)), 'legacy worker controls the page');
  ok(await lp.evaluate(() => caches.keys()).then((k) => k.join() === 'pyreader-web-old'), 'legacy cache present');

  current = 'v2';                                                           // deploy the new build over it
  // legacy page: the browser's own navigation-time update check is what finds it, so reload (the user's first reload)
  await lp.reload();
  await lp.waitForSelector('.shelf-head');
  const first = await lp.evaluate(() => globalThis.__BUILD);
  console.log('   first reload after deploy runs:', first);
  // the new worker cannot get an answer to its ping from the old page -> it must activate by itself
  const swapped = await until(() => lp.evaluate(() => caches.keys()).then((k) => k.join() === 'pyreader-web-v2'), 20000);
  ok(swapped, 'new worker activated on its own (old page cannot answer the ping, so no waiting forever)');
  await lp.reload();
  ok(await until(() => lp.evaluate(() => globalThis.__BUILD === 'v2')), 'second reload runs the new build (same as the old two-reload behaviour)');
  await wait(1000);
  ok(await lp.locator('.update-bar').count() === 0, 'upgraded page: no stray bar');
  const waiting = await lp.evaluate(async () => !!(await navigator.serviceWorker.getRegistration()).waiting);
  ok(!waiting, 'nothing left waiting');
  const lreal = L.logs.filter((l) => !/favicon|Failed to load resource/.test(l));
  ok(lreal.length === 0, 'no console errors during the legacy upgrade', JSON.stringify(lreal));
  await L.context.close(); await L.browser.close();
} finally {
  server.close();
}
console.log(fails ? `\n${fails} FAILED` : '\nall passed');
process.exit(fails ? 1 : 0);
