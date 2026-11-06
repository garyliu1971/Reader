// MOBI / AZW3 smoke: import, open, TOC, inline image, filepos link, search, position restore, DRM / truncated / junk rejection.
// Needs the books made by tools/make_test_books.py (sample.mobi, long.mobi, notoc.mobi, sample.azw3, drm.mobi, truncated.mobi).
// MOBI_REAL=<path to a real .mobi> additionally opens that book (never commit it).
import { launch, BOOKS, SHOTS, URL0, wait } from './lib.mjs';
import fs from 'node:fs';
import crypto from 'node:crypto';
fs.mkdirSync(SHOTS, { recursive: true });
let fails = 0;
const ok = (cond, msg, extra = '') => { console.log(`${cond ? 'PASS' : 'FAIL'}  ${msg} ${extra}`); if (!cond) fails++; };

const { browser, page, logs } = await launch({ width: 900, height: 700 });
const requests = [];
page.on('request', (r) => requests.push(r.url()));
await page.goto(URL0);
await page.waitForSelector('.shelf-head');

const R = () => page.evaluate(() => {
  const r = window.__pyreader.reader, pg = r.pg;
  const [a, b] = pg.pageRange();
  return { s: r.s, page: pg.page, pageCount: pg.pageCount, total: pg.total, anchor: pg.anchor, range: [a, b], pct: r.percent(),
    at: pg.textAt(pg.anchor, 12), nSections: r.book.sections.length, toc: r.book.toc.map((t) => ({ title: t.title, s: t.s, id: t.id, level: t.level })),
    secTitles: r.book.sections.map((s) => s.title), format: r.book.format, title: r.book.title, author: r.book.author };
});
const toasts = () => page.evaluate(() => Array.from(document.querySelectorAll('.toast')).map((t) => t.textContent));
const bookCount = () => page.locator('.book').count();
const importFiles = async (files) => { await page.setInputFiles('input[type=file]', files); };
const openBook = async (title) => {
  await page.evaluate(() => { history.replaceState(null, '', '#/'); });
  await page.locator('.book .t', { hasText: title }).first().click();
  await page.waitForSelector('.content p', { timeout: 20000 });
  await wait(700);
};
const toShelf = async () => { await page.evaluate(() => window.__pyreader.reader.ctx.goShelf()); await wait(500); };

// ---------------------------------------------------------------- import sample.mobi
const t0 = Date.now();
await importFiles([BOOKS + '/sample.mobi']);
await page.waitForFunction(() => document.querySelectorAll('.book').length >= 1, null, { timeout: 30000 });
await wait(800);
console.log('import ms', Date.now() - t0);
ok((await bookCount()) === 1, 'sample.mobi appears on the shelf');
ok((await page.locator('.book .t').first().textContent()) === '测试之书', 'title from MOBI header/EXTH');
ok((await page.locator('.book .a').first().textContent()) === '测试作者', 'author from EXTH');
ok((await page.locator('.book .fmt').first().textContent()) === 'mobi', 'format badge shows mobi');
ok(await page.evaluate(() => { const i = document.querySelector('.book .cover img'); return !!i && i.complete && i.naturalWidth > 0; }), 'cover image (EXTH cover offset) rendered on the shelf');
await page.screenshot({ path: SHOTS + '/mobi-shelf.png' });

// ---------------------------------------------------------------- open
await openBook('测试之书');
let st = await R();
console.log('mobi opened', JSON.stringify({ s: st.s, pages: st.pageCount, total: st.total, nSections: st.nSections, secTitles: st.secTitles, toc: st.toc }));
ok(st.format === 'mobi' && st.title === '测试之书' && st.author === '测试作者', 'book object: format/title/author');
ok(await page.evaluate(() => document.querySelector('.content').textContent.includes('测试之书')), 'text is readable (title page)');
ok(!(await page.evaluate(() => document.querySelector('.content').textContent.includes('�'))), 'no replacement characters');
ok(st.nSections === 6, 'six sections (title page, TOC page, four chapters)', `n=${st.nSections}`);
ok(st.toc.length === 4 && st.toc[0].title.startsWith('第一章') && st.toc[3].title.startsWith('第四章'), 'book TOC (from the in-book TOC page) has the four chapters', JSON.stringify(st.toc.map((t) => t.title)));
ok(!(await page.evaluate(() => !!document.querySelector('.content script, .content style, .content [onerror], .content a[href^="javascript"]'))), 'no script/onerror/javascript: in rendered book');
await page.screenshot({ path: SHOTS + '/mobi-open.png' });

// ---- TOC overlay: click chapter 3
await page.keyboard.press('t'); await wait(500);
const items = await page.locator('.panel .list li').count();
ok(items === 4, 'TOC panel lists 4 entries', `n=${items}`);
await page.locator('.panel .list li').nth(2).locator('button').click(); await wait(800);
st = await R();
ok(st.s === 4 && /^第三章/.test(await page.evaluate(() => document.querySelector('.content h2')?.textContent || '')), 'TOC click jumps to chapter 3', `s=${st.s}`);

// ---- inline image (chapter 2 = section 3)
await page.evaluate(() => window.__pyreader.reader.showSection(3, { start: true })); await wait(800);
const img = await page.evaluate(async () => {
  const i = document.querySelector('.content img');
  if (!i) return null;
  await i.decode().catch(() => {});
  return { w: i.naturalWidth, src: i.src.slice(0, 5), alt: i.alt, nImgs: document.querySelectorAll('.content img').length };
});
ok(img && img.w === 400 && img.src === 'blob:', 'inline image (recindex) loaded from a blob: URL', JSON.stringify(img));

// ---- filepos link in chapter 1 (section 2) -> the marked paragraph in chapter 3
await page.evaluate(() => window.__pyreader.reader.showSection(2, { start: true })); await wait(600);
const hrefs = await page.evaluate(() => Array.from(document.querySelectorAll('.content a[data-href]')).map((a) => a.getAttribute('data-href')));
ok(hrefs.length === 1 && /^#filepos\d{10}$/.test(hrefs[0]), 'only the filepos link survives (javascript: href dropped)', JSON.stringify(hrefs));
const link = page.locator('.content a[data-href]').first();
for (let i = 0; i < 12; i++) {
  const box = await link.boundingBox();
  const fr = await page.locator('.frame').boundingBox();
  if (box && box.x >= fr.x && box.x < fr.x + fr.width) break;
  await page.keyboard.press('ArrowRight'); await wait(250);
}
await link.click(); await wait(900);
st = await R();
ok(st.s === 4 && st.at.startsWith('这里是被链接'), 'filepos link jumps to the target paragraph in chapter 3', `s=${st.s} at=${st.at}`);

// ---- search
await page.keyboard.press('/'); await wait(400);
await page.fill('.panel input[type=search]', '许多年以后'); await wait(1500);
const nHits = await page.locator('.panel .list li').count();
ok(nHits > 10, 'search finds the word in the book', `n=${nHits}`);
await page.locator('.panel .list li button').nth(8).click(); await wait(800);
ok(await page.evaluate(() => !!(CSS.highlights && CSS.highlights.get('search'))), 'search hit highlighted');

// ---- position restore (persisted layout path), then after a full reload
await page.evaluate(() => window.__pyreader.reader.showSection(4, { offset: 600 })); await wait(600);
await page.keyboard.press('ArrowRight'); await wait(300);
await wait(1200);
const saved = await page.evaluate(async () => { const r = window.__pyreader.reader; await r.saveNow(); return r.lastSaved; });
await toShelf();
await openBook('测试之书');
st = await R();
ok(st.s === saved.anchor.s && Math.abs(st.anchor - saved.anchor.o) <= 3, 'close + reopen restores the position', `s=${st.s} anchor=${st.anchor} saved=${JSON.stringify(saved.anchor)}`);
await toShelf();
await page.reload(); await page.waitForSelector('.shelf-head'); await wait(800);
await openBook('测试之书');
st = await R();
ok(st.s === saved.anchor.s && Math.abs(st.anchor - saved.anchor.o) <= 3, 'position also restored after a page reload', `s=${st.s} anchor=${st.anchor}`);
ok(st.toc.length === 4, 'TOC restored from the persisted layout', `n=${st.toc.length}`);
await toShelf();

// ---------------------------------------------------------------- the remote <img> in the book must never be requested
ok(!requests.some((u) => u.includes('127.0.0.1:9')), 'remote image in the book was never requested');

// ---------------------------------------------------------------- .azw3 extension (same MOBI6 bytes)
await importFiles([BOOKS + '/sample.azw3']);
await wait(1500);
ok((await bookCount()) === 2, 'sample.azw3 imported as a second book');
ok((await page.locator('.book .fmt').allTextContents()).every((f) => f === 'mobi'), 'all MOBI-family files show format mobi');

// ---------------------------------------------------------------- long.mobi: one big section -> parts, TOC into the parts
await importFiles([BOOKS + '/long.mobi']);
await page.waitForFunction(() => Array.from(document.querySelectorAll('.book .t')).some((e) => e.textContent === '长篇之书'), null, { timeout: 30000 });
await wait(500);
await openBook('长篇之书');
st = await R();
console.log('long.mobi', JSON.stringify({ n: st.nSections, toc: st.toc }));
ok(st.nSections >= 6, 'oversized section is split into parts', `n=${st.nSections}`);
ok(st.toc.length === 6 && new Set(st.toc.map((t) => t.s)).size === 6, 'six volume TOC entries map to six different sections/parts', JSON.stringify(st.toc.map((t) => t.s)));
ok(await page.evaluate(() => window.__pyreader.reader.book.lens.every((n) => n <= 60000)), 'every part is bounded');
await page.keyboard.press('t'); await wait(500);
await page.locator('.panel .list li').nth(4).locator('button').click(); await wait(900);
st = await R();
ok(st.at.startsWith('第五卷'), 'TOC click lands on volume five inside the big section', `at=${st.at} s=${st.s}`);
await toShelf();

// ---------------------------------------------------------------- notoc.mobi: fallback TOC
await importFiles([BOOKS + '/notoc.mobi']);
await page.waitForFunction(() => Array.from(document.querySelectorAll('.book .t')).some((e) => e.textContent === '无目录之书'), null, { timeout: 30000 });
await wait(500);
await openBook('无目录之书');
st = await R();
ok(st.toc.length === 3 && st.toc.map((t) => t.title).join('|') === '开篇1|开篇2|开篇3', 'no TOC in the book: one entry per section titled from its first short line', JSON.stringify(st.toc.map((t) => t.title)));
await toShelf();

// ---------------------------------------------------------------- rejections: no book is added, a friendly toast is shown
const before = await bookCount();
const rejected = async (file, pattern, label) => {
  const t = Date.now();
  await importFiles([file]);
  let tx = [];
  for (let i = 0; i < 40; i++) { tx = await toasts(); if (tx.some((x) => pattern.test(x))) break; await wait(150); }
  ok(tx.some((x) => pattern.test(x)), `${label}: friendly toast`, JSON.stringify(tx));
  ok(Date.now() - t < 8000, `${label}: no hang`, `${Date.now() - t} ms`);
  ok((await bookCount()) === before, `${label}: nothing added to the shelf`);
  await page.evaluate(() => document.querySelectorAll('.toast').forEach((t) => t.remove()));
};
await rejected(BOOKS + '/drm.mobi', /DRM/, 'drm.mobi');
await rejected(BOOKS + '/truncated.mobi', /不完整/, 'truncated.mobi');
await rejected({ name: 'junk.mobi', mimeType: 'application/octet-stream', buffer: crypto.randomBytes(20000) }, /不是有效的 MOBI/, 'junk.mobi');
await rejected({ name: 'tiny.azw3', mimeType: 'application/octet-stream', buffer: Buffer.from('BOOKMOBI') }, /MOBI/, 'tiny.azw3');
const kfx = Buffer.alloc(4096); kfx.write('CONT');
await rejected({ name: 'x.azw', mimeType: 'application/octet-stream', buffer: kfx }, /KFX/, 'KFX container');
await rejected({ name: 'x.pdf.azw', mimeType: 'application/octet-stream', buffer: Buffer.from('%PDF-1.4 not a book') }, /MOBI|AZW/, 'pdf bytes under .azw');

// ---------------------------------------------------------------- real book (optional)
if (process.env.MOBI_REAL) {
  const real = process.env.MOBI_REAL;
  const errsBefore = logs.length;
  const name = real.split(/[\\/]/).pop().replace(/\.[^.]+$/, '');
  const t1 = Date.now();
  await importFiles([real]);
  await page.waitForFunction((n) => Array.from(document.querySelectorAll('.book .t')).some((e) => e.textContent.includes(n.slice(0, 3))), name.replace(/^\(.\)/, ''), { timeout: 60000 }).catch(() => {});
  await wait(1000);
  console.log('real import ms', Date.now() - t1, 'shelf titles:', await page.locator('.book .t').allTextContents());
  const title = (await page.locator('.book .t').allTextContents()).find((x) => x.includes('失明')) || (await page.locator('.book .t').last().textContent());
  await openBook(title);
  st = await R();
  const txt = await page.evaluate(() => document.querySelector('.content').textContent);
  console.log('real', JSON.stringify({ title: st.title, author: st.author, sections: st.nSections, toc: st.toc.length, first: txt.slice(0, 40) }));
  ok(st.toc.length >= 20, 'real book: 20+ TOC entries', `n=${st.toc.length}`);
  ok(!txt.includes('�') && /[一-鿿]/.test(txt), 'real book: Chinese text, no U+FFFD');
  // walk every section's text for U+FFFD
  const bad = await page.evaluate(async () => { const b = window.__pyreader.reader.book; let n = 0; for (let i = 0; i < b.sections.length; i++) if ((await b.sectionText(i)).includes('�')) n++; return n; });
  ok(bad === 0, 'real book: no U+FFFD in any section', `bad=${bad}`);
  // every TOC entry resolves to a section; click the 10th
  await page.keyboard.press('t'); await wait(500);
  await page.locator('.panel .list li').nth(9).locator('button').click(); await wait(900);
  const s10 = await R();
  ok(s10.s === st.toc[9].s, 'real book: TOC click navigates', `s=${s10.s} want=${st.toc[9].s}`);
  await page.screenshot({ path: SHOTS + '/mobi-real.png' });
  const newErrs = logs.slice(errsBefore).filter((l) => /^\[(error|pageerror)\]/.test(l));
  ok(newErrs.length === 0, 'real book: no console errors', JSON.stringify(newErrs));
}

const hard = logs.filter((l) => /^\[pageerror\]/.test(l));
ok(hard.length === 0, 'no uncaught page errors', JSON.stringify(hard));
console.log('console logs:', logs);
console.log(fails ? `\n${fails} FAILURES` : '\nALL OK');
await browser.close();
process.exit(fails ? 1 : 0);
