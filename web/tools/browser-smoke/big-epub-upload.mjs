import { launch, BOOKS, SHOTS, SP, URL0, DEVDAV, wait } from './lib.mjs';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
let fails = 0;
const ok = (cond, msg, extra = '') => { console.log(`${cond ? 'PASS' : 'FAIL'}  ${msg} ${extra}`); if (!cond) fails++; };

// ------------------------------------------------ large EPUBs: import/open time, UI responsiveness
{
  const { browser, page, logs } = await launch({ width: 900, height: 700 });
  await page.goto(URL0);
  await page.waitForSelector('.shelf-head');
  await page.evaluate(() => { window.__maxGap = 0; let last = performance.now(); setInterval(() => { const n = performance.now(); window.__maxGap = Math.max(window.__maxGap, n - last); last = n; }, 20); });
  for (const f of ['big.epub', 'huge1.epub']) {
    const size = fs.statSync(BOOKS + '/' + f).size;
    await page.evaluate(() => { window.__maxGap = 0; });
    const t0 = Date.now();
    const before = await page.locator('.book').count();
    await page.setInputFiles('input[type=file]', [BOOKS + '/' + f]);
    await page.waitForFunction((n) => document.querySelectorAll('.book').length > n, before, { timeout: 120000 });
    const imp = Date.now() - t0;
    const gap = await page.evaluate(() => window.__maxGap);
    await wait(300);
    await page.evaluate(() => { window.__maxGap = 0; });
    const t1 = Date.now();
    await page.locator('.book .book-open').first().click();
    await page.waitForSelector('.content p', { timeout: 120000 });
    await page.waitForFunction(() => window.__pyreader.reader.pg.pageCount >= 1 && !document.querySelector('.loading:not([hidden])'), null, { timeout: 120000 });
    const open = Date.now() - t1;
    const st = await page.evaluate(() => { const r = window.__pyreader.reader; return { secs: r.book.sections.length, total: r.pg.total, pages: r.pg.pageCount }; });
    console.log(`${f} (${(size / 1048576).toFixed(1)} MB): import ${imp} ms (max main-thread gap ${gap.toFixed(0)} ms), open ${open} ms (gap ${(await page.evaluate(() => window.__maxGap)).toFixed(0)} ms)`, JSON.stringify(st));
    const t2 = Date.now();
    await page.keyboard.press('ArrowRight'); await wait(50);
    await page.evaluate(() => window.__pyreader.reader.seekPercent(55));
    console.log('  seek+page turn ms', Date.now() - t2, 'pct', (await page.evaluate(() => window.__pyreader.reader.percent())).toFixed(1));
    await page.setViewportSize({ width: 500, height: 800 });
    const t3 = Date.now();
    await page.waitForFunction(() => window.__pyreader.reader.pg.pw === 500, null, { timeout: 30000 });
    await wait(200);
    console.log('  resize relayout ms ~', Date.now() - t3, 'pct', (await page.evaluate(() => window.__pyreader.reader.percent())).toFixed(1));
    await page.setViewportSize({ width: 900, height: 700 });
    await page.evaluate(() => window.__pyreader.reader.ctx.goShelf()); await wait(400);
    await page.evaluate(async () => { const { removeBook, allBooks } = await import('./js/library.js'); for (const b of await allBooks()) await removeBook(b.id); await window.__pyreader.shelf.render(); }); await wait(300);
  }
  console.log('logs', logs);
  await browser.close();
}

// ------------------------------------------------ drag & drop import, prefers-color-scheme, upload via UI, Edge
{
  const proc = spawn('python', ['-I', DEVDAV, '--auth', 'gary:pw'], { stdio: ['ignore', 'pipe', 'inherit'] });
  const base = await new Promise((res) => { let b = ''; proc.stdout.on('data', (d) => { b += d; const m = /READY (\S+)/.exec(b); if (m) res(m[1]); }); });
  const auth = 'Basic ' + Buffer.from('gary:pw').toString('base64');
  const { browser, page, logs } = await launch({ width: 900, height: 700, scheme: 'dark' });
  await page.goto(URL0);
  await page.waitForSelector('.shelf-head');
  const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  ok(bg === 'rgb(18, 19, 22)', 'prefers-color-scheme: dark -> dark shelf', bg);
  // drag and drop
  await page.evaluate(async () => {
    const b64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
    const dt = new DataTransfer();
    dt.items.add(new File(['# 拖拽导入\n\n正文'], 'dropped.md', { type: 'text/markdown' }));
    for (const type of ['dragenter', 'dragover', 'drop']) window.dispatchEvent(new DragEvent(type, { dataTransfer: dt, bubbles: true, cancelable: true }));
  });
  await page.waitForFunction(() => document.querySelectorAll('.book').length >= 1, null, { timeout: 10000 });
  ok((await page.textContent('.book .t')).includes('拖拽导入'), 'drag-drop import works');
  // enable sync + upload that book via UI menu
  await page.evaluate(async (base) => { await window.__pyreader.syncMgr.saveConfig({ enabled: true, url: base, user: 'gary', password: 'pw', syncUser: 'gary', deviceId: 'web-up0001', deviceName: 'Up Test' }); }, base);
  await page.evaluate(() => window.__pyreader.shelf.render()); await wait(500);
  await page.locator('.book .more').first().click();
  await page.click('.panel >> text=上传到云端书库…');
  await page.fill('.panel input[aria-label="云端文件名"]', 'up-from-web.md');
  await page.click('.panel button:has-text("上传")'); await wait(1500);
  const up = await fetch(base + '/library/up-from-web.md', { headers: { Authorization: auth } });
  ok(up.status === 200 && (await up.text()).includes('正文'), 'UI upload created the file on the server');
  // second upload of the same name must be refused
  await page.locator('.book:not(.cloud) .more').first().click();
  await page.click('.panel >> text=上传到云端书库…');
  await page.fill('.panel input[aria-label="云端文件名"]', 'up-from-web.md');
  await page.click('.panel button:has-text("上传")'); await wait(1200);
  ok((await page.locator('.panel .hint').last().textContent()).includes('已有同名'), 'duplicate upload refused with message', await page.locator('.panel .hint').last().textContent());
  const dbg = await (await fetch(new URL(base).origin + '/__debug/log')).json();
  const mv = dbg.headers.find(([m]) => m === 'MOVE');
  ok(mv && mv[2].Overwrite === 'F', 'browser MOVE carried Overwrite: F', JSON.stringify(mv && mv[2]));
  ok(!dbg.methods.some(([m]) => m === 'DELETE'), 'no DELETE');
  console.log('logs', logs);
  await browser.close();
  proc.kill();
}
console.log(fails ? `\n${fails} FAILURES` : '\nALL OK');
process.exit(fails ? 1 : 0);
