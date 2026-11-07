import { launch, BOOKS, SHOTS, URL0, wait } from './lib.mjs';
let fails = 0;
const ok = (cond, msg, extra = '') => { console.log(`${cond ? 'PASS' : 'FAIL'}  ${msg} ${extra}`); if (!cond) fails++; };

// ---------------------------------------------------------------- big txt + pdf (desktop)
{
  const { browser, page, logs } = await launch({ width: 900, height: 700 });
  await page.goto(URL0);
  await page.waitForSelector('.shelf-head');
  // main-thread responsiveness while importing 13 MB of text (worker parses)
  await page.evaluate(() => { window.__maxGap = 0; let last = performance.now(); setInterval(() => { const n = performance.now(); window.__maxGap = Math.max(window.__maxGap, n - last); last = n; }, 20); });
  const t0 = Date.now();
  await page.setInputFiles('input[type=file]', [BOOKS + '/big.txt', BOOKS + '/sample.pdf']);
  await page.waitForFunction(() => document.querySelectorAll('.book').length >= 2, null, { timeout: 120000 });
  console.log('import time ms', Date.now() - t0, 'max main-thread gap ms', await page.evaluate(() => window.__maxGap));
  await wait(500);
  console.log('max gap after settle', await page.evaluate(() => window.__maxGap));

  await page.evaluate(() => { window.__maxGap = 0; });
  const t1 = Date.now();
  await page.locator('.book .t', { hasText: 'big' }).first().click();
  await page.waitForSelector('.content p', { timeout: 60000 });
  console.log('open big.txt ms', Date.now() - t1, 'gap', await page.evaluate(() => window.__maxGap));
  const info = await page.evaluate(() => { const r = window.__pyreader.reader; return { secs: r.book.sections.length, toc: r.book.toc.length, total: r.pg.total, pages: r.pg.pageCount }; });
  console.log(JSON.stringify(info));
  ok(info.toc >= 399, 'big txt: 400 chapters detected', `toc=${info.toc}`);
  // jump to 63% and verify
  await page.evaluate(() => window.__pyreader.reader.seekPercent(63)); await wait(800);
  const pct = await page.evaluate(() => window.__pyreader.reader.percent());
  ok(Math.abs(pct - 63) < 1, 'big txt: seek 63%', `pct=${pct.toFixed(2)}`);
  const tSearch = Date.now();
  await page.keyboard.press('/'); await wait(300);
  await page.fill('.find-input', '这座城市从不等待'); await wait(300);
  await page.waitForFunction(() => /^\d+ \/ \d+\+?$/.test(document.querySelector('.find-count').textContent), null, { timeout: 60000 });
  console.log('search over 13MB ms', Date.now() - tSearch, await page.textContent('.find-count'));
  await page.keyboard.press('Escape'); await wait(300);

  // ---- PDF
  await page.evaluate(() => window.__pyreader.reader.ctx.goShelf()); await wait(500);
  await page.locator('.book .t', { hasText: /Test|sample/ }).first().click();
  await page.waitForSelector('.frame.pdf canvas', { timeout: 30000 }); await wait(1200);
  const pdf = await page.evaluate(() => { const r = window.__pyreader.reader; const c = r.pdfCanvas; return { n: r.book.pageCount, w: c.width, h: c.height, page: r.pdfPage, toc: r.book.toc.length }; });
  ok(pdf.n === 6 && pdf.w > 100 && pdf.toc === 2, 'pdf rendered', JSON.stringify(pdf));
  await page.keyboard.press('ArrowRight'); await wait(800);
  ok((await page.evaluate(() => window.__pyreader.reader.pdfPage)) === 1, 'pdf next page');
  await page.screenshot({ path: SHOTS + '/pdf.png' });
  await page.evaluate(() => window.__pyreader.reader.setSetting('theme', 'dark')); await wait(500);
  await page.screenshot({ path: SHOTS + '/pdf-dark.png' });
  console.log('logs', logs);
  await browser.close();
}

// ---------------------------------------------------------------- touch phone: tap zones, swipe, selection, bars
{
  const { browser, page, logs } = await launch({ width: 390, height: 780, touch: true, ctx: { isMobile: true, deviceScaleFactor: 2 } });
  await page.goto(URL0);
  await page.waitForSelector('.shelf-head');
  await page.setInputFiles('input[type=file]', [BOOKS + '/sample.epub']);
  await page.waitForFunction(() => document.querySelectorAll('.book').length >= 1, null, { timeout: 30000 });
  await page.locator('.book .t').first().tap();
  await page.waitForSelector('.content p'); await wait(800);
  const P = () => page.evaluate(() => { const pg = window.__pyreader.reader.pg; return { page: pg.page, n: pg.pageCount, bars: window.__pyreader.reader.barsOn() }; });
  let s = await P();
  console.log('phone', JSON.stringify(s));
  const fr = await page.locator('.frame').boundingBox();
  // right tap zone -> next
  await page.touchscreen.tap(fr.x + fr.width * 0.9, fr.y + fr.height * 0.5); await wait(400);
  ok((await P()).page === 1, 'tap right zone -> next page');
  await page.touchscreen.tap(fr.x + fr.width * 0.1, fr.y + fr.height * 0.5); await wait(400);
  ok((await P()).page === 0, 'tap left zone -> previous page');
  await page.touchscreen.tap(fr.x + fr.width * 0.5, fr.y + fr.height * 0.5); await wait(400);
  ok((await P()).bars === true, 'tap centre -> bars (settings/progress overlay)');
  await page.screenshot({ path: SHOTS + '/phone-bars.png' });
  await page.touchscreen.tap(fr.x + fr.width * 0.5, fr.y + fr.height * 0.5); await wait(400);
  ok((await P()).bars === false, 'tap centre again -> bars hidden');
  // swipe via CDP touch events
  const cdp = await page.context().newCDPSession(page);
  const swipe = async (x0, x1, y) => {
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: x0, y }] });
    for (let i = 1; i <= 6; i++) await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: x0 + ((x1 - x0) * i) / 6, y }] });
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  };
  await swipe(300, 80, 300); await wait(500);
  ok((await P()).page === 1, 'swipe left -> next page', JSON.stringify(await P()));
  await swipe(80, 300, 300); await wait(500);
  ok((await P()).page === 0, 'swipe right -> previous page');
  await page.screenshot({ path: SHOTS + '/phone-1.png' });
  console.log('logs', logs);
  await browser.close();
}

// ---------------------------------------------------------------- desktop: selection / copy works and does not turn pages
{
  const { browser, page, logs } = await launch({ width: 900, height: 700 });
  await page.goto(URL0);
  await page.waitForSelector('.shelf-head');
  await page.setInputFiles('input[type=file]', [BOOKS + '/sample.epub']);
  await page.waitForFunction(() => document.querySelectorAll('.book').length >= 1, null, { timeout: 30000 });
  await page.locator('.book .t').first().click();
  await page.waitForSelector('.content p'); await wait(800);
  const p0 = await page.evaluate(() => window.__pyreader.reader.pg.page);
  const para = page.locator('.content p').nth(2);
  const bb = await para.boundingBox();
  await page.mouse.move(bb.x + 40, bb.y + 8); await page.mouse.down(); await page.mouse.move(bb.x + 300, bb.y + 8, { steps: 6 }); await page.mouse.up(); await wait(300);
  const sel = await page.evaluate(() => window.getSelection().toString());
  ok(sel.length > 3, 'mouse drag selects text (selection not blocked)', JSON.stringify(sel));
  ok((await page.evaluate(() => window.__pyreader.reader.pg.page)) === p0, 'selecting did not turn the page');
  const userSelect = await page.evaluate(() => getComputedStyle(document.querySelector('.content')).userSelect);
  ok(userSelect === 'text', 'user-select: text');
  console.log('logs', logs);
  await browser.close();
}
console.log(fails ? `\n${fails} FAILURES` : '\nALL OK');
process.exit(fails ? 1 : 0);
