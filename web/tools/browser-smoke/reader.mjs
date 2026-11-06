import { launch, BOOKS, SHOTS, URL0, wait } from './lib.mjs';
import fs from 'node:fs';
fs.mkdirSync(SHOTS, { recursive: true });
let fails = 0;
const ok = (cond, msg, extra = '') => { console.log(`${cond ? 'PASS' : 'FAIL'}  ${msg} ${extra}`); if (!cond) fails++; };

const { browser, page, logs } = await launch({ width: 900, height: 700 });
await page.goto(URL0);
await page.waitForSelector('.shelf-head');
await page.setInputFiles('input[type=file]', [BOOKS + '/sample.epub', BOOKS + '/long.txt', BOOKS + '/long-gbk.txt']);
await page.waitForFunction(() => document.querySelectorAll('.book').length >= 3, null, { timeout: 30000 });
await wait(500);

const R = () => page.evaluate(() => {
  const r = window.__pyreader.reader, pg = r.pg;
  const [a, b] = pg.pageRange();
  return { s: r.s, page: pg.page, pageCount: pg.pageCount, total: pg.total, anchor: pg.anchor, range: [a, b], pw: pg.pw, ph: pg.ph, mx: pg.mx,
    ctx: pg.textAt(pg.anchor, 10), pct: r.percent(), mode: pg.mode, first: pg.textAt(pg.offsetAtPage(pg.page), 10) };
});
const openBook = async (title) => {
  await page.evaluate(() => { history.replaceState(null, '', '#/'); });
  await page.evaluate(() => window.__pyreader.shelf.ctx.open && 0);
  await page.locator('.book .t', { hasText: title }).first().click();
  await page.waitForSelector('.content p', { timeout: 20000 });
  await wait(600);
};

// ---------------------------------------------------------------- EPUB
await openBook('测试之书');
let st = await R();
console.log('epub initial', JSON.stringify(st));
ok(st.pageCount >= 2 && st.total > 500, 'epub chapter paginated into columns', `pages=${st.pageCount}`);
await page.screenshot({ path: SHOTS + '/epub-1.png' });
// script/style from the book must not leak
ok(!(await page.evaluate(() => !!document.querySelector('.content script, .content style'))), 'no script/style in rendered book');
ok((await page.evaluate(() => document.querySelector('.content').textContent.includes('alert'))) === false, 'script text not rendered');

// page turns with keyboard
await page.keyboard.press('ArrowRight'); await wait(300);
await page.keyboard.press('ArrowRight'); await wait(300);
st = await R();
ok(st.page === 2, 'two page turns -> page 2', `page=${st.page}`);
ok(st.range[0] <= st.anchor && st.anchor < st.range[1], 'anchor inside current page range');
const before = { s: st.s, anchor: st.anchor, ctx: st.ctx, pct: st.pct };
await page.screenshot({ path: SHOTS + '/epub-2.png' });

// ---- reflow on viewport change keeps position (phone size, then wide, then small)
for (const [w, h] of [[420, 800], [1200, 600], [360, 640], [900, 700]]) {
  await page.setViewportSize({ width: w, height: h });
  await wait(500);
  const a = await R();
  const inside = a.range[0] <= before.anchor && before.anchor < a.range[1];
  ok(a.anchor === before.anchor && a.s === before.s, `viewport ${w}x${h}: anchor preserved`, `anchor ${a.anchor} pages=${a.pageCount} page=${a.page} pw=${a.pw}`);
  ok(inside, `viewport ${w}x${h}: anchored text is on the visible page`, JSON.stringify(a.range));
  const noHScroll = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1);
  ok(noHScroll, `viewport ${w}x${h}: no horizontal page scroll`);
  if (w === 360) await page.screenshot({ path: SHOTS + '/epub-360.png' });
  if (w === 1200) await page.screenshot({ path: SHOTS + '/epub-1200.png' });
}

// ---- font size via Ctrl +
await page.keyboard.press('Control+='); await page.keyboard.press('Control+='); await page.keyboard.press('Control+='); await wait(500);
let a = await R();
ok((await page.evaluate(() => window.__pyreader.reader.settings.fontSize)) === 22, 'Ctrl+= x3 -> font 22');
ok(a.range[0] <= before.anchor && before.anchor < a.range[1] && a.anchor === before.anchor, 'font change keeps anchor on page', JSON.stringify(a.range));
await page.keyboard.press('Control+0'); await wait(400);

// ---- overlays must not change text width
const wBefore = await page.evaluate(() => window.__pyreader.reader.pg.pw);
await page.keyboard.press('t'); await wait(500);
const tocInfo = await page.evaluate(() => ({ cur: document.querySelector('.panel .list li[aria-current=true]')?.textContent, n: document.querySelectorAll('.panel .list li').length, pw: window.__pyreader.reader.pg.pw, tw: window.__pyreader.reader.pg.content.clientWidth }));
ok(tocInfo.n === 12 && /第.+章/.test(tocInfo.cur || ''), 'TOC lists 12 chapters, current highlighted', JSON.stringify(tocInfo));
ok(tocInfo.pw === wBefore, 'TOC drawer is an overlay: page width unchanged');
await page.screenshot({ path: SHOTS + '/toc.png' });
await page.locator('.panel .list li').nth(4).locator('button').click(); await wait(700);
st = await R();
ok(st.s === 4, 'TOC click jumps to chapter 5', `s=${st.s}`);


// ---- chapter nav, slider seek
await page.evaluate(() => window.__pyreader.reader.chapter(1)); await wait(500);
ok((await R()).s === 5, 'next chapter');
await page.evaluate(() => window.__pyreader.reader.seekPercent(50)); await wait(600);
st = await R();
ok(Math.abs(st.pct - 50) < 6, 'seek 50% lands near 50%', `pct=${st.pct.toFixed(1)} s=${st.s}`);

// ---- internal link in chapter 1 -> chapter 3 target
await page.evaluate(() => window.__pyreader.reader.showSection(0, { start: true })); await wait(500);
const link = page.locator('.content a[data-href]').first();
// the link is on a later page of chapter 1: page until visible
let found = false;
for (let i = 0; i < 12 && !found; i++) {
  const box = await link.boundingBox();
  const fr = await page.locator('.frame').boundingBox();
  if (box && box.x >= fr.x && box.x < fr.x + fr.width) found = true; else { await page.keyboard.press('ArrowRight'); await wait(250); }
}
await link.click(); await wait(800);
st = await R();
ok(st.s === 2, 'internal link navigates to target chapter', `s=${st.s}`);

// ---- search
await page.keyboard.press('/'); await wait(400);
await page.fill('.panel input[type=search]', '许多年以后'); await wait(1500);
const nHits = await page.locator('.panel .list li').count();
ok(nHits > 20, 'search returns many hits', `n=${nHits}`);
await page.locator('.panel .list li button').nth(30).click(); await wait(800);
ok(await page.evaluate(() => !!(CSS.highlights && CSS.highlights.get('search'))), 'search hit highlighted');
await page.screenshot({ path: SHOTS + '/search-hit.png' });

// ---- bookmarks
await page.keyboard.press('b'); await wait(500);
ok((await page.getAttribute('.topbar button[aria-pressed]', 'aria-pressed')) === 'true', 'bookmark added on this page');
await page.keyboard.press('ArrowRight'); await wait(400);
ok((await page.getAttribute('.topbar button[aria-pressed]', 'aria-pressed')) === 'false', 'next page has no bookmark');
await page.keyboard.press('ArrowLeft'); await wait(400);
ok((await page.getAttribute('.topbar button[aria-pressed]', 'aria-pressed')) === 'true', 'bookmark shows again on return');

// ---- dark theme: the page itself
await page.evaluate(() => window.__pyreader.reader.setSetting('theme', 'dark')); await wait(300);
const colors = await page.evaluate(() => ({ body: getComputedStyle(document.body).backgroundColor, reader: getComputedStyle(document.querySelector('.reader')).backgroundColor, text: getComputedStyle(document.querySelector('.content')).color }));
ok(colors.reader === 'rgb(18, 19, 22)' && colors.text === 'rgb(214, 214, 217)', 'dark theme: reader surface and text colours', JSON.stringify(colors));
await page.screenshot({ path: SHOTS + '/dark.png' });
await page.evaluate(() => window.__pyreader.reader.setSetting('theme', 'sepia')); await wait(200);
await page.screenshot({ path: SHOTS + '/sepia.png' });
await page.evaluate(() => window.__pyreader.reader.setSetting('theme', 'auto'));

// ---- progress persisted; reopen restores
await wait(1200);
const savedBefore = await page.evaluate(async () => { const r = window.__pyreader.reader; await r.saveNow(); return r.lastSaved; });
await page.evaluate(() => window.__pyreader.reader.ctx.goShelf()); await wait(500);
const pctShown = await page.locator('.book .pct').first().textContent();
console.log('shelf pct text:', pctShown);
await page.locator('.book .t', { hasText: '测试之书' }).first().click();
await page.waitForSelector('.content p'); await wait(800);
st = await R();
ok(st.s === savedBefore.anchor.s, 'reopen restores chapter', `s=${st.s}`);
ok(Math.abs(st.anchor - savedBefore.anchor.o) <= 3, 'reopen restores offset', `${st.anchor} vs ${savedBefore.anchor.o}`);

// ---------------------------------------------------------------- TXT (GBK)
await page.evaluate(() => window.__pyreader.reader.ctx.goShelf()); await wait(500);
await page.locator('.book .t', { hasText: 'long-gbk' }).first().click();
await page.waitForSelector('.content p'); await wait(800);
const txt = await page.evaluate(() => ({ h: document.querySelector('.content').textContent.slice(0, 80), toc: window.__pyreader.reader.book.toc.length, secs: window.__pyreader.reader.book.sections.length }));
ok(/第一章|测试长篇/.test(txt.h) && !txt.h.includes('�'), 'GBK txt decoded correctly', txt.h);
ok(txt.toc >= 55, 'TXT chapters detected', `toc=${txt.toc}`);
await page.screenshot({ path: SHOTS + '/txt-gbk.png' });
// scroll mode
await page.evaluate(() => window.__pyreader.reader.setSetting('mode', 'scroll')); await wait(600);
let sc = await R();
ok(sc.mode === 'scroll', 'continuous scroll mode');
await page.screenshot({ path: SHOTS + '/scroll.png' });
await page.evaluate(() => window.__pyreader.reader.setSetting('mode', 'paged')); await wait(600);
sc = await R();
ok(sc.mode === 'paged', 'back to paged mode');

console.log('console logs:', logs);
console.log(fails ? `\n${fails} FAILURES` : '\nALL OK');
await browser.close();
process.exit(fails ? 1 : 0);
