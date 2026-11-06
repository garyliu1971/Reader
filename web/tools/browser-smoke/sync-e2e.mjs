import { launch, BOOKS, SHOTS, SP, URL0, DEVDAV, wait } from './lib.mjs';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
let fails = 0;
const ok = (cond, msg, extra = '') => { console.log(`${cond ? 'PASS' : 'FAIL'}  ${msg} ${extra}`); if (!cond) fails++; };

// ---- devdav with auth + seeded library
const seed = SP + '/seed'; fs.rmSync(seed, { recursive: true, force: true }); fs.mkdirSync(seed, { recursive: true });
fs.copyFileSync(BOOKS + '/sample.epub', seed + '/sample.epub'); fs.copyFileSync(BOOKS + '/notes.md', seed + '/notes.md'); fs.copyFileSync(BOOKS + '/long-gbk.txt', seed + '/long-gbk.txt');
const proc = spawn('python', ['-I', DEVDAV, '--auth', 'gary:pw', '--seed', seed], { stdio: ['ignore', 'pipe', 'inherit'] });
const base = await new Promise((res) => { let b = ''; proc.stdout.on('data', (d) => { b += d; const m = /READY (\S+)/.exec(b); if (m) res(m[1]); }); });
console.log('devdav at', base);
const auth = 'Basic ' + Buffer.from('gary:pw').toString('base64');
const dav = (p, init = {}) => fetch(base + '/' + p, { ...init, headers: { Authorization: auth, ...(init.headers || {}) } });

const { browser, context, page, logs } = await launch({ width: 900, height: 700 });
const reqs = [];
page.on('request', (r) => { if (r.url().startsWith(base)) reqs.push({ m: r.method(), u: r.url().slice(base.length), h: r.headers() }); });
await page.goto(URL0);
await page.waitForSelector('.shelf-head');

// ---- settings UI: public http is refused with a clear warning
await page.click('button[aria-label=设置]'); await page.waitForSelector('.panel');
await page.fill('input[aria-label="WebDAV 地址"]', 'http://dav.example.com/dav'); await wait(200);
ok(await page.locator('.panel .warn').nth(1).isVisible().catch(() => false), 'http:// public host shows a warning');
await page.check('#sync-enabled');
await page.click('text=保存并测试连接'); await wait(500);
ok(/https|明文/.test(await page.locator('.panel [role=status]').first().textContent()), 'http:// public host is refused on save');
ok(!(await page.evaluate(() => window.__pyreader.syncMgr.enabled())), 'sync stays off after refused save');

// ---- real config against devdav (cross-origin, Authorization header => real CORS preflight)
await page.fill('input[aria-label="WebDAV 地址"]', base);
await page.fill('input[aria-label="用户名"]', 'gary');
await page.fill('input[aria-label="密码"]', 'pw');
await page.fill('input[aria-label="同步用户名"]', 'gary');
await page.fill('input[aria-label="设备名称"]', 'E2E Browser');
await page.fill('input[aria-label="设备 ID"]', 'web-e2e001');
await page.click('text=保存并测试连接');
await page.waitForFunction(() => /同步完成|失败/.test(document.querySelector('.panel [role=status]')?.textContent || ''), null, { timeout: 20000 });
const msg = await page.locator('.panel [role=status]').first().textContent();
ok(/同步完成：3/.test(msg), 'settings: connected, 3 cloud books', msg);
const dbg = await (await fetch(new URL(base).origin + '/__debug/log')).json();
const pre = dbg.headers.filter(([m]) => m === 'OPTIONS');
ok(pre.length > 0 && pre.every(([, , h]) => !h.Authorization), 'browser sent OPTIONS preflights, none carrying Authorization', `n=${pre.length} ${JSON.stringify(pre[0] && pre[0][2])}`);
await page.keyboard.press('Escape'); await wait(500);
await page.screenshot({ path: SHOTS + '/shelf-cloud.png' });
const cloud = await page.$$eval('.book.cloud .t', (e) => e.map((x) => x.textContent));
ok(cloud.length === 3, 'shelf shows cloud books', JSON.stringify(cloud));
ok(await page.evaluate(() => !JSON.stringify(localStorage).includes('pw') && !location.href.includes('pw')), 'credentials not in localStorage/URL');
const stored = await page.evaluate(async () => (await window.__pyreader.syncMgr.cfg).password);
ok(stored === 'pw', 'credentials kept in IndexedDB-backed config only');

// ---- download + open + progress push
await page.locator('.book.cloud .book-open', { hasText: 'sample' }).first().click();
await page.waitForSelector('.content p', { timeout: 30000 }); await wait(800);
const id = await page.evaluate(() => window.__pyreader.reader.meta.id);
ok(id === 'remote:sample.epub', 'downloaded book id is remote:<rel>', id);
await page.keyboard.press('ArrowRight'); await page.keyboard.press('ArrowRight'); await wait(300);
await page.evaluate(() => window.__pyreader.reader.chapter(1)); await wait(800);
await page.evaluate(async () => { await window.__pyreader.reader.saveNow(); window.__pyreader.syncMgr.flush(); });
await wait(1800);
const sha = await page.evaluate(async () => { const { progressFileName } = await import('./js/core/bookid.js'); return progressFileName('remote:sample.epub'); });
const pr = await (await dav('users/gary/progress/' + sha)).json();
console.log('remote progress file:', JSON.stringify(pr));
ok(pr.book_id === 'remote:sample.epub' && pr.percent > 5 && pr.offset >= 1e9 && pr.device === 'E2E Browser', 'per-book progress written to the server in Qt format');
ok(Math.abs(pr.updated - Date.now() / 1000) < 60, 'updated is unix seconds, not in the future');
const dev = await (await dav('users/gary/shelf/web-e2e001.json')).json();
ok(dev.progress['remote:sample.epub'] && dev.device_id === 'web-e2e001' && dev.version === 1, 'device file written with progress digest');
console.log('device file keys', Object.keys(dev));

// ---- desktop reads further elsewhere (newer record) -> banner on reopen
const newer = { book_id: 'remote:sample.epub', offset: 4321, percent: 80, chapter: '第十章', updated: Date.now() / 1000 + 5, device: 'Desktop' };
await dav('users/gary/progress/' + sha, { method: 'PUT', body: JSON.stringify(newer) });
await page.evaluate(() => window.__pyreader.reader.ctx.goShelf()); await wait(600);
await page.locator('.book:not(.cloud) .book-open', { hasText: '测试之书' }).first().click();
await page.waitForSelector('.banner', { timeout: 10000 });
ok((await page.textContent('.banner')).includes('Desktop') && (await page.textContent('.banner')).includes('80.0%'), 'banner offers the newer desktop position');
await page.click('.banner >> text=跳转'); await wait(900);
const pctNow = await page.evaluate(() => window.__pyreader.reader.percent());
ok(Math.abs(pctNow - 80) < 6, 'jump to remote percent', pctNow.toFixed(1));
// adopting must not write back
await wait(6000);
const after = await (await dav('users/gary/progress/' + sha)).json();
ok(after.device === 'Desktop' && after.percent === 80, 'newer desktop record was not regressed by the web app');

// ---- peers: a Qt device file marks a book as favourite and shows progress
const qt = { version: 1, device_id: 'qt-pc', hostname: 'My Desktop', user: 'gary', entries: { 'remote:notes.md': { fav: [true, Date.now()] } },
  progress: { 'remote:notes.md': { percent: 42, updated: Date.now() / 1000 - 30 } } };
await dav('users/gary/shelf/qt-pc.json', { method: 'PUT', body: JSON.stringify(qt) });
await page.evaluate(() => window.__pyreader.reader.ctx.goShelf()); await wait(600);
await page.click('button[aria-label=立即同步]'); await wait(1800);
const favs = await page.$$eval('.book .favmark', (e) => e.length);
const notesPct = await page.locator('.book', { hasText: 'notes' }).first().locator('.pct').textContent();
ok(favs === 1, 'favourite from the desktop device file shows on the shelf');
ok(/42.0%/.test(notesPct) && /My Desktop/.test(notesPct), 'peer progress digest shown with desktop name', notesPct);
await page.screenshot({ path: SHOTS + '/shelf-synced.png' });
// toggling hidden/fav writes only our own file
await page.locator('.book', { hasText: 'notes' }).first().locator('.more').click();
await page.click('.panel >> text=取消收藏'); await wait(300);
await page.evaluate(() => window.__pyreader.syncMgr.pushDeviceNow()); await wait(800);
const dev2 = await (await dav('users/gary/shelf/web-e2e001.json')).json();
ok(dev2.entries['remote:notes.md'].fav[0] === false && dev2.entries['remote:notes.md'].fav[1] > qt.entries['remote:notes.md'].fav[1], 'unfav written to OWN device file with a newer ts');
const qtAfter = await (await dav('users/gary/shelf/qt-pc.json')).json();
ok(JSON.stringify(qtAfter) === JSON.stringify(qt), "peer's device file untouched");
// no DELETE ever
ok(!reqs.some((r) => r.m === 'DELETE'), 'no DELETE request was ever sent');
console.log('methods used:', [...new Set(reqs.map((r) => r.m))].join(','));

// ---- delete local copy keeps remote
await page.locator('.book:not(.cloud)', { hasText: '测试之书' }).first().locator('.more').click();
await page.click('.panel >> text=删除本地副本'); await page.click('.panel >> text=删除'); await wait(800);
ok((await dav('library/sample.epub')).status === 200, 'deleting the local copy leaves the cloud file');
ok(!reqs.some((r) => r.m === 'DELETE'), 'still no DELETE');

// ---- offline PWA
await page.evaluate(() => navigator.serviceWorker.ready.then(() => true));
await wait(1500);
const swState = await page.evaluate(async () => { const keys = await caches.keys(); const c = keys.length ? await (await caches.open(keys[0])).keys() : []; return { keys, n: c.length, ctrl: !!navigator.serviceWorker.controller }; });
console.log('sw', JSON.stringify(swState));
await page.reload(); await wait(1500);   // now controlled
await context.setOffline(true);
await page.reload(); await page.waitForSelector('.shelf-head', { timeout: 10000 });
ok(await page.evaluate(() => !!document.querySelector('.shelf-head h1')), 'offline: app shell loads from the service worker cache');
await context.setOffline(false);

console.log('logs', logs.filter((l) => !/Failed to load resource|ERR_INTERNET_DISCONNECTED/.test(l)));
console.log(fails ? `\n${fails} FAILURES` : '\nALL OK');
await browser.close(); proc.kill();
process.exit(fails ? 1 : 0);
