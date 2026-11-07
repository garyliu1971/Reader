// Online library (Gutenberg) against a local STUB of the /books/v1 API (never the real server): open from shelf + empty shelf,
// search, paging, add -> shelf -> reader, already-added state, 401 / 503 catalog_loading / 429 / offline, 390px layout, axe.
// Starts its own static server for web/ + stub on port 8132 (override STUB_PORT). Covers on gutenberg.org are intercepted in the browser.
// Setup like cloudtts.mjs: copy lib.mjs + this file into a scratch dir with `npm i playwright-core axe-core`, WEB_DIR=<repo>/web.
import { launch, BOOKS, SHOTS, wait } from './lib.mjs';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
fs.mkdirSync(SHOTS, { recursive: true });
let fails = 0;
const ok = (cond, msg, extra = '') => { console.log(`${cond ? 'PASS' : 'FAIL'}  ${msg} ${extra}`); if (!cond) fails++; };

const WEB = path.resolve(process.env.WEB_DIR || path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..'));
const PORT = Number(process.env.STUB_PORT || 8132);
const TOKEN = 'smoke-token';
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png', '.webmanifest': 'application/json' };
const EPUB = fs.readFileSync(BOOKS + '/sample.epub');
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==', 'base64');
const ALL = Array.from({ length: 30 }, (_, i) => ({ id: String(1000 + i), title: `Pride Tale ${i + 1}`, authors: ['Austen, Jane'], lang: 'en' }));
const json = (res, code, obj) => res.writeHead(code, { 'content-type': 'application/json' }).end(JSON.stringify(obj));
const stub = { mode: 'ok', searches: [], downloads: [], loadingLeft: 0 };
const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://x');
  if (u.pathname.startsWith('/books/v1/')) {
    if (req.headers.authorization !== 'Bearer ' + TOKEN) { res.writeHead(401).end(); return; }
    if (u.pathname === '/books/v1/search') {
      stub.searches.push(Object.fromEntries(u.searchParams));
      if (stub.mode === 'quota') { json(res, 429, { error: 'rate_limited', retryAfter: 120 }); return; }
      if (stub.loadingLeft > 0) { stub.loadingLeft--; json(res, 503, { error: 'catalog_loading', retryAfter: 1 }); return; }
      const q = (u.searchParams.get('q') || '').toLowerCase();
      const off = Number(u.searchParams.get('offset')), lim = Number(u.searchParams.get('limit'));
      const hits = q.includes('nothing') ? [] : ALL;
      json(res, 200, { total: hits.length, items: hits.slice(off, off + lim) });
      return;
    }
    const m = /^\/books\/v1\/download\/(\d+)$/.exec(u.pathname);
    if (m) { stub.downloads.push({ id: m[1], variant: u.searchParams.get('variant') }); res.writeHead(200, { 'content-type': 'application/epub+zip', 'content-length': EPUB.length }).end(EPUB); return; }
    res.writeHead(404).end(); return;
  }
  const p = path.join(WEB, decodeURIComponent(u.pathname === '/' ? '/index.html' : u.pathname));
  if (!p.startsWith(WEB) || !fs.existsSync(p) || fs.statSync(p).isDirectory()) { res.writeHead(404).end(); return; }
  res.writeHead(200, { 'content-type': MIME[path.extname(p)] || 'application/octet-stream' }).end(fs.readFileSync(p));
});
await new Promise((r) => server.listen(PORT, '127.0.0.1', r));
const APP = `http://127.0.0.1:${PORT}/index.html`;

const { browser, context, page } = await launch({ width: 900, height: 800 });
const coverReqs = [];
await context.route('https://www.gutenberg.org/**', (route) => {   // no real internet: id 1000 has a cover, the rest fail -> placeholder
  coverReqs.push(route.request().url());
  if (route.request().url().includes('/1000/')) route.fulfill({ status: 200, contentType: 'image/png', body: PNG }); else route.abort();
});
const dlg = page.locator('.ol-panel');
const search = async (q) => { await dlg.locator('input[type=search]').fill(q); await dlg.locator('button[type=submit]').click(); };
try {
  await page.goto(APP);
  await page.waitForSelector('.shelf-head');

  // ---- empty shelf: button in the empty state, and the one in the header
  await page.waitForSelector('.empty .online-lib-btn');
  ok((await page.locator('.empty .online-lib-btn').innerText()).includes('在线书库（Gutenberg）'), 'empty shelf shows the online library button');
  await page.locator('.empty .online-lib-btn').click();
  await dlg.waitFor(); await wait(150);
  ok((await dlg.getAttribute('role')) === 'dialog' && (await page.evaluate(() => document.activeElement.id)) === 'ol-q', 'dialog opens with focus in the search box');
  ok((await dlg.locator('select[aria-label="书籍语言"] option').count()) >= 5 && (await dlg.locator('select[aria-label="书籍语言"]').inputValue()) === 'en', 'language select: >=5 languages, default en');

  // ---- no token: no request, message points to settings
  await search('pride');
  await dlg.locator('.ol-state.auth').waitFor();
  ok((await dlg.locator('.ol-state.auth').innerText()).includes('在线朗读') && stub.searches.length === 0, 'missing token: message names the 在线朗读 token, no request sent');
  await page.keyboard.press('Escape'); await wait(300);
  ok((await page.locator('.ol-panel').count()) === 0, 'Escape closes the dialog');

  // ---- wrong token -> 401 from the stub
  await page.evaluate(() => import('./js/store/cloudcfg.js').then((m) => m.saveCloudCfg({ token: 'wrong' })));
  await page.locator('.shelf-add .online-lib-btn').click();
  await dlg.waitFor();
  await search('pride');
  await dlg.locator('.ol-state.auth').waitFor();
  ok(stub.searches.length === 0 && (await dlg.locator('.ol-state.auth').innerText()).includes('令牌'), 'wrong token: 401 state (server rejected before search handler)');
  ok((await dlg.locator('.ol-state.auth button', { hasText: '打开设置' }).count()) === 1, '401 state offers 打开设置');

  // ---- good token: results, cover, paging
  await page.evaluate((t) => import('./js/store/cloudcfg.js').then((m) => m.saveCloudCfg({ token: t })), TOKEN);
  await search('pride');
  await dlg.locator('.ol-row').first().waitFor();
  ok((await dlg.locator('.ol-row').count()) === 25, '25 results rendered');
  const s0 = stub.searches[stub.searches.length - 1];
  ok(s0.q === 'pride' && s0.lang === 'en' && s0.limit === '25' && s0.offset === '0', 'search request params', JSON.stringify(s0));
  const first = dlg.locator('.ol-row').first();
  ok((await first.innerText()).includes('Pride Tale 1') && (await first.innerText()).includes('Austen') && (await first.innerText()).includes('ID 1000'), 'row shows title, author, ID');
  await wait(400);
  ok((await first.locator('img').count()) === 1 && (await first.locator('img').getAttribute('referrerpolicy')) === 'no-referrer', 'cover img with no-referrer');
  ok((await dlg.locator('.ol-row').nth(1).locator('.ol-ph').count()) === 1, 'failed cover falls back to a placeholder');
  await dlg.locator('button', { hasText: '加载更多' }).click();
  await page.waitForFunction(() => document.querySelectorAll('.ol-row').length === 30);
  ok(stub.searches[stub.searches.length - 1].offset === '25' && (await dlg.locator('button', { hasText: '加载更多' }).count()) === 0, 'load more fetches offset 25; button gone at the end');

  // ---- axe on the open dialog
  const axePath = process.env.AXE_JS || './node_modules/axe-core/axe.min.js';
  if (fs.existsSync(axePath)) {
    await page.evaluate(fs.readFileSync(axePath, 'utf8'));
    const v = await page.evaluate(async () => (await axe.run(document, { resultTypes: ['violations'] })).violations.map((x) => `${x.id}(${x.impact}) ${x.nodes[0].html.slice(0, 120)}`));
    ok(v.length === 0, 'axe: no violations in the library dialog', v.join(' | '));
  } else console.log('SKIP  axe-core not installed');
  await page.screenshot({ path: SHOTS + '/library-light.png' });

  // ---- add the first book (default variant noimages)
  const addBtn = dlg.locator('.ol-row').first().locator('button', { hasText: '加入书架' });
  ok((await addBtn.getAttribute('aria-label')) === '加入书架：Pride Tale 1', 'add button has an accessible name');
  await addBtn.click();
  await dlg.locator('.ol-row').first().locator('.ol-added').waitFor({ timeout: 20000 });
  ok(stub.downloads.length === 1 && stub.downloads[0].id === '1000' && stub.downloads[0].variant === 'noimages', 'download uses variant=noimages by default', JSON.stringify(stub.downloads));
  ok((await dlg.locator('.ol-row').first().innerText()).includes('已添加'), 'row switches to 已添加');
  const rec = await page.evaluate(async () => { const { allBooks } = await import('./js/library.js'); return (await allBooks()).map((b) => ({ n: b.name, o: b.origin, f: b.format, c: b.comic })); });
  ok(rec.length === 1 && rec[0].o === 'gutenberg:1000' && rec[0].n === 'pg1000 - Pride Tale 1.epub' && rec[0].f === 'epub', 'stored through importBlob with origin + sanitised name', JSON.stringify(rec));

  // ---- with images variant for the second book
  await dlg.locator('#ol-images').check();
  await dlg.locator('.ol-row').nth(1).locator('button', { hasText: '加入书架' }).click();
  await dlg.locator('.ol-row').nth(1).locator('.ol-added').waitFor({ timeout: 20000 });
  ok(stub.downloads[1] && stub.downloads[1].variant === 'epub3', 'with-images option requests variant=epub3');

  // ---- shelf shows it and the reader opens it
  await page.keyboard.press('Escape'); await wait(400);
  ok((await page.locator('.book').count()) === 2, 'both books are on the shelf');
  await page.locator('.book .t', { hasText: /./ }).first().click();
  await page.waitForSelector('.content p', { timeout: 20000 });
  ok(true, 'book opens in the reader');
  await page.evaluate(() => history.back()); await page.waitForSelector('.shelf-head', { state: 'visible' });

  // ---- already-added state after reopening, and the dialog's own 打开 button
  await page.locator('.shelf-add .online-lib-btn').click();
  await dlg.waitFor();
  await search('pride');
  await dlg.locator('.ol-row').first().waitFor();
  await page.waitForFunction(() => document.querySelectorAll('.ol-row .ol-added').length === 2);
  ok((await dlg.locator('.ol-row .ol-added').count()) === 2 && (await dlg.locator('.ol-row', { hasText: 'Pride Tale 3' }).locator('button', { hasText: '加入书架' }).count()) === 1, 'already-added books show 已添加, others still addable');
  await dlg.locator('.ol-row').first().locator('button', { hasText: '打开' }).click();
  await page.waitForSelector('.content p', { timeout: 20000 }); await wait(400);
  ok((await page.locator('.ol-panel').count()) === 0, '打开 closes the dialog and opens the reader');
  await page.evaluate(() => history.back()); await page.waitForSelector('.shelf-head', { state: 'visible' });

  // ---- empty result
  await page.locator('.shelf-add .online-lib-btn').click(); await dlg.waitFor();
  await search('nothing here');
  await dlg.locator('.ol-msg').waitFor();
  ok((await dlg.locator('.ol-msg').innerText()).includes('没有找到'), 'no results state');

  // ---- 503 catalog_loading -> automatic retry
  stub.loadingLeft = 1;
  const n0 = stub.searches.length;
  await search('pride');
  await dlg.locator('.ol-state.loading').waitFor({ timeout: 3000 });
  ok((await dlg.locator('.ol-state.loading').innerText()).includes('自动重试'), 'catalog_loading state announces the automatic retry');
  await dlg.locator('.ol-row').first().waitFor({ timeout: 15000 });
  ok(stub.searches.length === n0 + 2 && (await dlg.locator('.ol-row').count()) === 25, 'auto retry succeeded without user action');

  // ---- 429
  stub.mode = 'quota';
  await search('pride 2');
  await dlg.locator('.ol-state.quota').waitFor();
  ok((await dlg.locator('.ol-state.quota').innerText()).includes('2 分钟'), '429 state shows the retry time');
  stub.mode = 'ok';
  await dlg.locator('.ol-state.quota button', { hasText: '重试' }).click();
  await dlg.locator('.ol-row').first().waitFor();
  ok(true, 'retry after 429 works');

  // ---- offline
  await context.setOffline(true);
  await search('pride 3');
  await dlg.locator('.ol-state.offline').waitFor();
  ok((await dlg.locator('.ol-state.offline').innerText()).includes('网络'), 'offline state');
  await context.setOffline(false);

  // ---- 390px phone width, dark
  await page.setViewportSize({ width: 390, height: 800 });
  await page.emulateMedia({ colorScheme: 'dark' });
  await search('pride');
  await dlg.locator('.ol-row').first().waitFor();
  const over = await page.evaluate(() => { const b = document.querySelector('.ol-panel .panel-body'); const p = document.querySelector('.ol-panel'); return { body: b.scrollWidth - b.clientWidth, doc: document.documentElement.scrollWidth - innerWidth, panel: p.getBoundingClientRect().width }; });
  ok(over.body <= 0 && over.doc <= 0, 'no horizontal overflow at 390px', JSON.stringify(over));
  const btnH = await dlg.locator('.ol-row').nth(2).locator('button').first().boundingBox();
  ok(btnH.height >= 32, 'touch target >= 32px', String(btnH.height));
  await page.screenshot({ path: SHOTS + '/library-390-dark.png' });
} finally {
  await browser.close();
  server.close();
}
console.log(fails ? `${fails} FAILED` : 'ALL PASSED');
process.exit(fails ? 1 : 0);
