// "继续阅读" card and the 最近 filter on the shelf. Real browser; see lib.mjs for setup.
import { launch, BOOKS, SHOTS, URL0, wait } from './lib.mjs';
import fs from 'node:fs';
fs.mkdirSync(SHOTS, { recursive: true });
let fails = 0;
const ok = (cond, msg, extra = '') => { console.log(`${cond ? 'PASS' : 'FAIL'}  ${msg} ${extra}`); if (!cond) fails++; };

const { browser, page, logs } = await launch({ width: 900, height: 700 });
await page.goto(URL0);
await page.waitForSelector('.shelf-head');
await page.setInputFiles('input[type=file]', [BOOKS + '/sample.epub', BOOKS + '/long.txt']);
await page.waitForFunction(() => document.querySelectorAll('.book').length >= 2, null, { timeout: 30000 });
await wait(600);

const cont = () => page.evaluate(() => {
  const c = document.querySelector('.continue');
  return c ? { title: c.querySelector('.ct').textContent, sub: c.querySelector('.cs').textContent, kicker: c.querySelector('.kicker').textContent, label: c.getAttribute('aria-label') } : null;
});
const toShelf = async () => { await page.evaluate(() => window.__pyreader.reader.ctx.goShelf()); await wait(700); };
const openBook = async (title) => { await page.locator('.book .t', { hasText: title }).first().click(); await page.waitForSelector('.content p', { timeout: 20000 }); await wait(800); };
const rows = () => page.evaluate(async () => {   // raw IndexedDB state: what is stored locally, not what the shelf shows
  const { db } = await import('./js/store/db.js');
  const out = {};
  for (const b of await db.getAll('books')) out[b.title] = { lastRead: b.lastRead || 0, readAt: b.readAt || 0, percent: b.percent || 0, progress: !!(await db.get('progress', b.id)) };
  return out;
});
const pos = () => page.evaluate(() => { const r = window.__pyreader.reader; return { id: r.meta.id, s: r.s, anchor: r.pg.anchor, pct: r.percent() }; });

// ---- nothing opened yet: no card
ok((await cont()) === null, 'fresh shelf: no continue card');

// ---- read a bit of "long", return: the card appears for it
await openBook('long');
await page.keyboard.press('ArrowRight'); await wait(300);
await page.keyboard.press('ArrowRight'); await wait(300);
await page.keyboard.press('ArrowRight'); await wait(1500);
const before = await pos();
await toShelf();
let c = await cont();
console.log('card', JSON.stringify(c));
ok(c && c.title.includes('long') && c.kicker === '继续阅读' && c.label === '继续阅读', 'card shows the book just read', JSON.stringify(c));
ok(c && /读到|刚开始/.test(c.sub) && /刚刚|分钟前/.test(c.sub), 'card says how far and when', c && c.sub);
await page.screenshot({ path: SHOTS + '/continue-card.png' });

// ---- the card is a real button; clicking resumes at the saved spot
ok(await page.locator('.continue button.continue-open').count() === 1, 'card is one button (keyboard + screen reader)');
await page.locator('.continue button.continue-open').click();
await page.waitForSelector('.content p', { timeout: 20000 }); await wait(900);
const after = await pos();
ok(after.id === before.id && after.s === before.s && Math.abs(after.anchor - before.anchor) <= 3, 'click resumes the same book at the saved position', `${before.anchor} vs ${after.anchor}`);

// ... and merely reopening it (paged) does not re-stamp it either
const lrBefore = (await rows())['long'].lastRead;
await wait(2000);
ok((await rows())['long'].lastRead === lrBefore, 'paged: reopening a read book leaves lastRead untouched', `${lrBefore} vs ${(await rows())['long'].lastRead}`);

// ---- merely opening another book (no page turn, short stay) must NOT take over the card
await toShelf();
await openBook('测试之书'); await wait(1500);
const peek = await rows();
await page.evaluate(async () => {   // a row written by an older build, which stamped openedAt on every open
  const { db } = await import('./js/store/db.js');
  for (const b of await db.getAll('books')) if (b.title.includes('测试之书')) { b.openedAt = Date.now() + 60000; await db.put('books', b); }
});
await toShelf();
c = await cont();
ok(c && c.title.includes('long'), 'opened-but-not-read book does not displace the real last-read book', c && c.title);
ok(peek['测试之书'] && !peek['测试之书'].progress && !peek['测试之书'].lastRead && !peek['测试之书'].readAt, 'opening writes no progress record and no read stamp (nothing to sync)', JSON.stringify(peek['测试之书']));
await page.locator('.chip', { hasText: '最近' }).click(); await wait(400);
ok(await page.locator('.grid .book').count() === 1, '最近 filter lists only books that were really read');
await page.locator('.chip', { hasText: '全部' }).click(); await wait(300);

// ---- reading it (a page turn) makes it the card
await openBook('测试之书');
await page.keyboard.press('ArrowRight'); await wait(300);
await page.keyboard.press('ArrowRight'); await wait(1800);
await toShelf();
c = await cont();
ok(c && c.title.includes('测试之书'), 'after turning pages the book takes over the card', c && c.title);

// ---- 最近 chip: newest first, card stays; search hides the card
await page.locator('.chip', { hasText: '最近' }).click(); await wait(500);
const order = await page.locator('.grid .book .t').allTextContents();
ok(order.length === 2 && order[0].includes('测试之书') && order[1].includes('long'), '最近 filter lists touched books newest first', order.join(' | '));
ok((await cont()) !== null, 'card also shown on the 最近 filter');
await page.locator('.chip', { hasText: '全部' }).click(); await wait(300);
await page.fill('input[type=search]', 'long'); await wait(500);
ok((await cont()) === null, 'card hidden while searching');
await page.fill('input[type=search]', ''); await wait(400);
ok((await cont()) !== null, 'card back after clearing the search');
await page.locator('.chip', { hasText: '收藏' }).click(); await wait(400);
ok((await cont()) === null, 'card hidden on filters where it would mislead (收藏)');
await page.locator('.chip', { hasText: '全部' }).click(); await wait(300);

// ---- a finished book is not offered; the card falls back to the previous one
await openBook('测试之书');
await page.evaluate(() => window.__pyreader.reader.seekPercent(100)); await wait(1500);
await page.evaluate(() => window.__pyreader.reader.saveNow()); await wait(500);
const fin = await pos();
await toShelf();
c = await cont();
ok(fin.pct >= 97, 'epub reached the end', `pct=${fin.pct.toFixed(1)}`);
ok(c && c.title.includes('long'), 'finished book is skipped, card falls back to the other book', c && c.title);

// ---- scroll mode: restoring a saved offset queues a scroll event; it must not count as reading
await page.evaluate(() => window.__pyreader.reader.setSetting('mode', 'scroll')); await wait(600);
await openBook('long');
await page.evaluate(() => { window.__pyreader.reader.pg.frame.scrollTop = 3000; }); await wait(1800);
await page.evaluate(() => window.__pyreader.reader.saveNow()); await wait(500);
const scrolled = await page.evaluate(() => window.__pyreader.reader.pg.frame.scrollTop);
await toShelf();
await openBook('测试之书');
await page.evaluate(() => window.__pyreader.reader.seekPercent(50)); await wait(1800);
await page.evaluate(() => window.__pyreader.reader.saveNow()); await wait(500);
await toShelf();
const lrScroll = (await rows())['long'];
ok(scrolled > 1000 && (await cont()).title.includes('测试之书'), 'scroll mode setup: long scrolled, the other book is the card', `scrollTop=${scrolled}`);
await openBook('long');
const restored = await page.evaluate(() => window.__pyreader.reader.pg.frame.scrollTop);
await wait(2500);
await toShelf();
const lrScroll2 = (await rows())['long'];
ok(restored > 1000, 'scroll mode: saved offset restored on reopen', `scrollTop=${restored}`);
ok(lrScroll2.lastRead === lrScroll.lastRead && lrScroll2.percent === lrScroll.percent, 'scroll mode: reopening without reading leaves lastRead untouched', `${lrScroll.lastRead} vs ${lrScroll2.lastRead}`);
ok((await cont()).title.includes('测试之书'), 'scroll mode: reopened-unread book does not displace the card');
await openBook('long');   // real scrolling afterwards still saves
await page.evaluate(() => { window.__pyreader.reader.pg.frame.scrollTop += 800; }); await wait(1800);
ok((await rows())['long'].lastRead > lrScroll.lastRead, 'scroll mode: actually scrolling after reopening still saves progress');
await toShelf();
await page.evaluate(() => window.__pyreader.reader.setSetting('mode', 'paged')); await wait(400);

// ---- staying open past the dwell threshold counts as reading even without a page turn
await toShelf();
await page.setInputFiles('input[type=file]', [BOOKS + '/notes.md']);
await page.waitForFunction(() => document.querySelectorAll('.book').length >= 3, null, { timeout: 30000 });
await wait(600);
await openBook('笔记'); await wait(2000);
c = (await rows())['笔记'];
ok(c && !c.readAt && !c.lastRead && !c.progress, 'freshly opened third book: no stamp yet', JSON.stringify(c));
await wait(30000);   // DWELL_MS = 30 s
c = (await rows())['笔记'];
ok(c && c.readAt > 0 && !c.lastRead && !c.progress, 'kept open past the threshold: local read stamp only, no progress written', JSON.stringify(c));
await toShelf();
const dc = await cont();
ok(dc && dc.title.includes('笔记'), 'kept-open book becomes the card', dc && dc.title);

// ---- narrow phone width
await page.setViewportSize({ width: 360, height: 640 }); await wait(500);
const fit = await page.evaluate(() => { const b = document.querySelector('.continue-open').getBoundingClientRect(); return { r: b.right, w: innerWidth, noH: document.documentElement.scrollWidth <= innerWidth + 1 }; });
ok(fit.r <= fit.w && fit.noH, 'card fits a 360px screen without horizontal scroll', JSON.stringify(fit));
await page.screenshot({ path: SHOTS + '/continue-360.png' });

console.log('console logs:', logs);
console.log(fails ? `\n${fails} FAILURES` : '\nALL OK');
await browser.close();
process.exit(fails ? 1 : 0);
