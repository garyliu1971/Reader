// In-book search bar: Ctrl+F / "/", n / total, Enter / Shift+Enter, jump + highlight in paged / scroll / two-page spread,
// result list, PDF message, IME Enter, long-task check on a 13 MB book, phone layout. Real browser; see lib.mjs for setup.
import { launch, BOOKS, SHOTS, URL0, wait } from './lib.mjs';
import fs from 'node:fs';
fs.mkdirSync(SHOTS, { recursive: true });
let fails = 0;
const ok = (cond, msg, extra = '') => { console.log(`${cond ? 'PASS' : 'FAIL'}  ${msg} ${extra}`); if (!cond) fails++; };

// ---- a book whose hits we know: 30 chapters of filler, markers planted in chapters 3, 10 and 25
const CN = '一二三四五六七八九';
const cnum = (n) => (n < 10 ? CN[n - 1] : n === 10 ? '十' : n < 20 ? '十' + CN[n - 11] : n % 10 === 0 ? CN[Math.floor(n / 10) - 1] + '十' : CN[Math.floor(n / 10) - 1] + '十' + CN[(n % 10) - 1]);
const SENT = ['夜色渐深，远处的灯火在雾气里晕开。', '他停下脚步，回头望了一眼来时的路。', '风从窗缝里钻进来，带着一丝凉意。', '桌上的茶早已凉透，却没有人想起去换。', '这座城市从不等待任何人，只是默默地向前流淌。'];
const plant = { 3: { 5: ' Zebra crossing ', 8: '斑马线', 15: ' the ZEBRA runs ' }, 10: { 2: ' zebra ', 3: ' (a+b)*c ', 4: ' a.b ', 5: ' axb ' }, 25: { 1: ' zebra ', 10: '斑马线', 19: ' zebra and zebra ' } };
let txt = '搜索测试\n\n作者：无名\n\n';
for (let c = 1; c <= 30; c++) {
  txt += `第${cnum(c)}章 第${c}个故事\n\n`;
  for (let p = 1; p <= 20; p++) {
    let line = '';
    for (let i = 0; i < 6; i++) line += SENT[(c * 7 + p * 3 + i) % SENT.length];
    const m = plant[c] && plant[c][p];
    txt += '　　' + (m ? line.slice(0, 20) + m + line.slice(20) : line) + '\n';
  }
}
fs.writeFileSync(BOOKS + '/search-book.txt', txt);
const ZEBRA = 6;

const { browser, page, logs } = await launch({ width: 900, height: 700 });
await page.goto(URL0);
await page.waitForSelector('.shelf-head');
await page.setInputFiles('input[type=file]', [BOOKS + '/search-book.txt', BOOKS + '/sample.pdf', BOOKS + '/sample.epub']);
await page.waitForFunction(() => document.querySelectorAll('.book').length >= 3, null, { timeout: 30000 });
await wait(500);

const openBook = async (title) => {
  await page.evaluate(() => { history.replaceState(null, '', '#/'); });
  await page.locator('.book .t', { hasText: title }).first().click();
  await page.waitForSelector('.content p, .frame.pdf canvas', { timeout: 20000 });
  await wait(700);
};
const countText = () => page.textContent('.find-count');
const settled = async (re = /^\d+ \/ \d+\+?$/) => {
  await page.waitForFunction((src) => new RegExp(src).test(document.querySelector('.find-count').textContent), re.source, { timeout: 15000 });
  await wait(450);   // navigation + repaint
};
// active highlight: its text and whether it is really on screen (paged mode hides other pages by translating them out of the frame)
const active = () => page.evaluate(() => {
  const hl = CSS.highlights.get('search-active');
  const r = hl ? [...hl][0] : null;
  if (!r) return null;
  const fr = document.querySelector('.frame').getBoundingClientRect();
  const rects = [...r.getClientRects()];
  const vis = rects.length > 0 && rects.every((b) => b.left >= fr.left - 1 && b.right <= fr.right + 1 && b.top >= fr.top - 1 && b.bottom <= fr.bottom + 1);
  const rd = window.__pyreader.reader;
  return { text: r.toString(), vis, s: rd.s, n: CSS.highlights.get('search') ? [...CSS.highlights.get('search')].length : 0 };
});
const find = () => page.evaluate(() => {
  const r = window.__pyreader.reader;
  return { open: r.find.isOpen, finding: r.root.classList.contains('finding'), focused: document.activeElement === r.find.input, hl: !!(CSS.highlights.get('search') || CSS.highlights.get('search-active')), prevDis: r.find.btnPrev.disabled, nextDis: r.find.btnNext.disabled };
});
const typeQuery = async (q) => { await page.fill('.find-input', q); await page.dispatchEvent('.find-input', 'input'); };

// ================================================================ desktop, paged
await openBook('search-book');
await page.keyboard.press('Control+f'); await wait(500);
let f = await find();
ok(f.open && f.finding && f.focused, 'Ctrl+F opens the bar and focuses the input', JSON.stringify(f));
ok(await page.evaluate(() => document.querySelector('.topbar').getBoundingClientRect().bottom <= 1), 'toolbar is out of the way while searching');
await typeQuery('zebra'); await settled();
ok((await countText()) === `1 / ${ZEBRA}`, 'first hit after the reader, "1 / 6"', await countText());
let a = await active();
ok(a && a.text === 'Zebra' && a.vis && a.s === 3, 'active hit highlighted, on screen, in chapter 3 = section 3, after the front matter (case-insensitive)', JSON.stringify(a));
ok(a && a.n >= 1, 'the other hits of the page are highlighted too', `n=${a && a.n}`);
await page.screenshot({ path: SHOTS + '/search-1.png' });

await page.keyboard.press('Enter'); await settled();
a = await active();
ok((await countText()) === `2 / ${ZEBRA}` && a && a.text === 'ZEBRA' && a.vis, 'Enter -> next hit "2 / 6"', `${await countText()} ${a && a.text}`);
await page.keyboard.press('Enter'); await settled();
a = await active();
ok((await countText()) === `3 / ${ZEBRA}` && a && a.s === 10 && a.vis, 'Enter -> next section, jumps there and highlights', `${await countText()} s=${a && a.s}`);
await page.keyboard.press('Shift+Enter'); await settled();
a = await active();
ok((await countText()) === `2 / ${ZEBRA}` && a && a.s === 3 && a.text === 'ZEBRA' && a.vis, 'Shift+Enter -> previous hit, back to chapter 3', `${await countText()} s=${a && a.s}`);
// walk the whole ring: every hit is reached, visible, and the sequence wraps
const seen = [];
for (let i = 0; i < ZEBRA; i++) {
  await page.keyboard.press('Enter'); await settled();
  a = await active();
  seen.push(`${(await countText()).split(' ')[0]}${a && a.vis ? '' : '!'}`);
}
ok(seen.join(',') === '3,4,5,6,1,2', 'six Enters walk 3..6 then wrap through 1 to 2, all visible', seen.join(','));
await page.locator('.findbar button[aria-label^="上一个"]').click(); await settled();
ok((await countText()) === `1 / ${ZEBRA}`, 'prev button', await countText());
await page.locator('.findbar button[aria-label^="上一个"]').click(); await settled();
a = await active();
ok((await countText()) === `${ZEBRA} / ${ZEBRA}` && a && a.vis && a.s === 25, 'prev from the first hit wraps to the last', `${await countText()} s=${a && a.s}`);
await page.locator('.findbar button[aria-label^="下一个"]').click(); await settled();
ok((await countText()) === `1 / ${ZEBRA}`, 'next button wraps to the first', await countText());
await page.keyboard.press('F3'); await settled();
ok((await countText()) === `2 / ${ZEBRA}`, 'F3 = next', await countText());

// ---- page turn keeps the highlights while the bar is open; focus on the page, arrows turn pages
await page.evaluate(() => window.__pyreader.reader.frame.focus());
const before = await page.evaluate(() => window.__pyreader.reader.pg.page);
await page.keyboard.press('ArrowRight'); await wait(500);
ok(await page.evaluate((b) => window.__pyreader.reader.pg.page !== b, before) && (await find()).open, 'page turn works with the bar open');
ok(await page.evaluate(() => !!CSS.highlights.get('search-active')), 'active highlight survives a page turn');

// ---- CJK and literal characters
await typeQuery('斑马线'); await settled();
a = await active();
ok((await countText()) === '1 / 2' || (await countText()) === '2 / 2', 'CJK query: 2 hits', await countText());
ok(a && a.text === '斑马线' && a.vis, 'CJK hit highlighted and visible', JSON.stringify(a));
await typeQuery('(a+b)*c'); await settled();
ok((await countText()) === '1 / 1', 'regex characters are literal', await countText());
await typeQuery('a.b'); await settled();
ok((await countText()) === '1 / 1', '"a.b" does not match "axb"', await countText());
await typeQuery('ZEBRA   crossing'); await settled();
ok((await countText()) === '1 / 1', 'query whitespace is flexible, case ignored', await countText());
await typeQuery('不存在的词语xyz'); await wait(900);
f = await find();
ok((await countText()) === '没有找到' && f.prevDis && f.nextDis && !f.hl, 'no hit: message, buttons disabled, no highlights', JSON.stringify(f));
await page.keyboard.press('Enter'); await wait(300);
ok((await countText()) === '没有找到', 'Enter with no hits is harmless');
await typeQuery(''); await wait(500);
ok((await countText()) === '', 'empty query clears the counter');

// ---- IME: an Enter that only commits composition must not step
await typeQuery('zebra'); await settled();
const c0 = await countText();
await page.evaluate(() => { document.querySelector('.find-input').dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true, cancelable: true })); });
await wait(400);
ok((await countText()) === c0, 'Enter during IME composition does not step', `${c0} -> ${await countText()}`);

// ---- result list
await page.locator('.findbar button[aria-label="结果列表"]').click(); await wait(800);
const items = await page.locator('.panel .list li').count();
ok(items === ZEBRA, 'list shows every hit', `n=${items}`);
ok(await page.locator('.panel .list li mark').first().textContent() === 'Zebra', 'list marks the hit text');
await page.locator('.panel .list li button').nth(4).click(); await settled();
a = await active();
ok((await countText()) === `5 / ${ZEBRA}` && a && a.vis && a.s === 25, 'picking a list entry jumps there and keeps the bar usable', `${await countText()} s=${a && a.s}`);
await page.keyboard.press('Escape'); await wait(300);   // focus is on the list button (the panel closed): Esc still closes the bar

// ---- Escape closes the bar and clears every highlight
f = await find();
ok(!f.open && !f.finding && !f.hl, 'Escape closes the bar and clears the highlights', JSON.stringify(f));
// reopen: query kept, hits painted again
await page.keyboard.press('/'); await wait(500);
f = await find();
ok(f.open && f.focused && (await page.inputValue('.find-input')) === 'zebra' && f.hl, '"/" reopens with the last query and repaints', JSON.stringify(f));
await page.keyboard.press('Escape'); await wait(300);

// ---- scroll mode
await page.evaluate(() => window.__pyreader.reader.setSetting('mode', 'scroll')); await wait(600);
await page.evaluate(() => window.__pyreader.reader.showSection(0, { start: true })); await wait(500);
await page.keyboard.press('Control+f'); await wait(300);
await typeQuery('zebra'); await settled();
a = await active();
ok(a && a.vis && a.s === 3, 'scroll mode: first hit scrolled into view', JSON.stringify(a));
await page.keyboard.press('Enter'); await settled();
a = await active();
ok((await countText()) === `2 / ${ZEBRA}` && a && a.vis, 'scroll mode: next hit visible', `${await countText()} ${JSON.stringify(a)}`);
await page.keyboard.press('Enter'); await settled();
a = await active();
ok((await countText()) === `3 / ${ZEBRA}` && a && a.vis && a.s === 10, 'scroll mode: next section', `${await countText()} ${JSON.stringify(a)}`);
await page.screenshot({ path: SHOTS + '/search-scroll.png' });
await page.keyboard.press('Escape'); await wait(300);
await page.evaluate(() => window.__pyreader.reader.setSetting('mode', 'paged')); await wait(600);

// ---- two-page spread
await page.setViewportSize({ width: 1400, height: 800 }); await wait(700);
await page.evaluate(() => window.__pyreader.reader.showSection(0, { start: true })); await wait(500);
ok(await page.evaluate(() => window.__pyreader.reader.pg.cols === 2), 'spread layout active at 1400px');
await page.keyboard.press('Control+f'); await wait(300);
await typeQuery('zebra'); await settled();
const spreadSeen = [];
for (let i = 0; i < ZEBRA; i++) {
  a = await active();
  spreadSeen.push(a && a.vis ? 'v' : 'x');
  await page.keyboard.press('Enter'); await settled();
}
ok(spreadSeen.join('') === 'vvvvvv', 'spread: every hit visible on the left or right page', spreadSeen.join(''));
await page.screenshot({ path: SHOTS + '/search-spread.png' });
await page.keyboard.press('Escape'); await wait(300);
await page.setViewportSize({ width: 900, height: 700 }); await wait(500);

// ---- two jumps into a section that is still loading: the later one wins, nothing lands on the earlier hit
await page.keyboard.press('Control+f'); await wait(300);
await page.evaluate(() => window.__pyreader.reader.showSection(0, { start: true })); await wait(500);
const race = await page.evaluate(async () => {
  const r = window.__pyreader.reader;
  const orig = r.pg.setContent.bind(r.pg);
  r.pg.setContent = async (...a) => { await new Promise((res) => setTimeout(res, 150)); return orig(...a); };   // a slow section (many images)
  const p1 = r.gotoTarget(10, { offset: 0 });
  for (let i = 0; i < 400 && !(r.s === 10 && r.contentS !== 10); i++) await new Promise((res) => setTimeout(res, 0));   // r.s is already 10 while its text is still being laid out
  const during = r.s === 10 && r.contentS !== 10;
  const p2 = r.gotoTarget(10, { offset: 1900 });   // same section, issued while the first load is pending
  await Promise.all([p1, p2]);
  r.pg.setContent = orig;
  const [a, b] = r.pg.pageRange();
  return { during, s: r.s, cs: r.contentS, a, b, ok: 1900 >= a && 1900 < Math.max(b, a + 1) };
});
ok(race.during && race.ok && race.s === 10 && race.cs === 10, 'two jumps into a loading section: the later offset is the one shown', JSON.stringify(race));

// ---- a pasted chapter as query: capped, still finishes; screen-reader live region carries only results
await typeQuery('字'.repeat(50000)); await wait(1500);
const longq = await page.evaluate(() => { const f = window.__pyreader.reader.find; return { len: f.input.value.length, done: f.done, text: f.count.textContent }; });
ok(longq.len <= 200 && longq.done && longq.text === '没有找到', 'very long query: input capped, search finishes', JSON.stringify(longq));
await typeQuery('zebra'); await settled();
const liveT = await page.evaluate(() => { const f = window.__pyreader.reader.find; return { live: f.live.textContent, vis: f.count.textContent, lr: f.count.getAttribute('role'), aria: f.count.getAttribute('aria-hidden') }; });
ok(liveT.live === liveT.vis && /^\d+ \/ \d+$/.test(liveT.live) && !liveT.lr, 'live region mirrors the final result only', JSON.stringify(liveT));
await page.keyboard.press('Escape'); await wait(300);

// ---- EPUB
await page.evaluate(() => { history.replaceState(null, '', '#/'); window.dispatchEvent(new PopStateEvent('popstate')); }); await wait(400);
await openBook('测试之书');
await page.keyboard.press('Control+f'); await wait(300);
await typeQuery('许多年以后'); await settled();
a = await active();
const total = Number((await countText()).split('/')[1]);
ok(total > 20 && a && a.text === '许多年以后' && a.vis, 'EPUB: many hits, first one highlighted and visible', `${await countText()} ${JSON.stringify(a)}`);
await page.keyboard.press('Escape'); await wait(300);

// ---- PDF: no text layer -> a clear message, no bar, browser Ctrl+F left alone
await page.evaluate(() => { history.replaceState(null, '', '#/'); window.dispatchEvent(new PopStateEvent('popstate')); }); await wait(400);
await openBook('sample');
ok(await page.evaluate(() => window.__pyreader.reader.pdf === true), 'PDF opened');
ok((await page.getAttribute('.topbar button[aria-label="搜索全文"]', 'aria-disabled')) === 'true', 'search button is marked unavailable for PDF');
await page.keyboard.press('/'); await wait(400);
ok(await page.locator('.toast', { hasText: '没有文字层' }).count() >= 1 && !(await find()).open, '"/" on a PDF explains why there is no search');
const prevented = await page.evaluate(() => { const e = new KeyboardEvent('keydown', { key: 'f', ctrlKey: true, bubbles: true, cancelable: true }); document.dispatchEvent(e); return e.defaultPrevented; });
ok(!prevented, 'Ctrl+F on a PDF keeps the browser default');
await page.evaluate(() => { history.replaceState(null, '', '#/'); window.dispatchEvent(new PopStateEvent('popstate')); }); await wait(400);

// ---- 13 MB book: the UI must stay responsive while scanning
await page.setInputFiles('input[type=file]', [BOOKS + '/big.txt']);
await page.waitForFunction(() => document.querySelectorAll('.book').length >= 4, null, { timeout: 60000 });
await wait(800);
await openBook('big');
await page.evaluate(() => { window.__long = []; new PerformanceObserver((l) => { for (const e of l.getEntries()) window.__long.push(Math.round(e.duration)); }).observe({ entryTypes: ['longtask'] }); });
await page.keyboard.press('Control+f'); await wait(300);
const t0 = Date.now();
await typeQuery('没有人想起去换'); await settled(/^\d+ \/ \d+\+?$/);
const ms = Date.now() - t0;
await page.waitForFunction(() => window.__pyreader.reader.find.done, null, { timeout: 60000 });
const full = Date.now() - t0;
const longs = await page.evaluate(() => window.__long);
console.log(`big.txt: first hit shown after ${ms} ms, whole book scanned after ${full} ms, count "${await countText()}", long tasks:`, longs.join(',') || 'none');
ok(Math.max(0, ...longs) < 250, 'no main-thread task above 250 ms while scanning 13 MB', `max=${Math.max(0, ...longs)}`);
ok(/^\d+ \/ \d+\+?$/.test(await countText()), 'big book: n / total shown', await countText());
// worst case: nothing matches, so every section is scanned
await typeQuery('不存在的词xyz'); 
const t1 = Date.now();
await page.waitForFunction(() => window.__pyreader.reader.find.done && window.__pyreader.reader.find.count.textContent === '没有找到', null, { timeout: 60000 });
console.log(`big.txt: full scan without a hit took ${Date.now() - t1} ms (incl. 220 ms debounce), long tasks:`, (await page.evaluate(() => window.__long)).join(',') || 'none');
ok(Math.max(0, ...(await page.evaluate(() => window.__long))) < 250, 'no main-thread task above 250 ms during a full no-hit scan');
await page.keyboard.press('Escape'); await wait(300);
await page.evaluate(() => { history.replaceState(null, '', '#/'); window.dispatchEvent(new PopStateEvent('popstate')); }); await wait(400);

console.log('console logs (desktop):', logs);
await browser.close();

// ================================================================ phone, touch, 390px
{
  const { browser: b2, page: p2, logs: logs2 } = await launch({ width: 390, height: 780, touch: true, ctx: { isMobile: true, deviceScaleFactor: 2 } });
  await p2.goto(URL0);
  await p2.waitForSelector('.shelf-head');
  await p2.setInputFiles('input[type=file]', [BOOKS + '/search-book.txt']);
  await p2.waitForFunction(() => document.querySelectorAll('.book').length >= 1, null, { timeout: 30000 });
  await wait(500);
  await p2.locator('.book .t', { hasText: 'search-book' }).first().click();
  await p2.waitForSelector('.content p', { timeout: 20000 });
  await wait(900);
  const fr = await p2.locator('.frame').boundingBox();
  await p2.touchscreen.tap(fr.x + fr.width * 0.5, fr.y + fr.height * 0.5); await wait(500);
  await p2.locator('.topbar button[aria-label="搜索全文"]').tap(); await wait(600);
  const lay = await p2.evaluate(() => {
    const bar = document.querySelector('.findbar').getBoundingClientRect(), st = document.querySelector('.stage').getBoundingClientRect();
    const inp = document.querySelector('.find-input').getBoundingClientRect();
    const btns = [...document.querySelectorAll('.findbar .icon-btn')].map((b) => { const r = b.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height), r.left >= 0 && r.right <= innerWidth]; });
    return { barBottom: Math.round(bar.bottom), stageTop: Math.round(st.top), inputW: Math.round(inp.width), inputIn: inp.left >= 0 && inp.right <= innerWidth, btns, noHScroll: document.documentElement.scrollWidth <= innerWidth + 1, fontPx: getComputedStyle(document.querySelector('.find-input')).fontSize };
  });
  console.log('phone layout', JSON.stringify(lay));
  ok(lay.stageTop >= lay.barBottom, 'phone: page starts below the find bar (nothing hidden under it)', `${lay.stageTop} >= ${lay.barBottom}`);
  ok(lay.inputIn && lay.inputW >= 200 && lay.btns.every(([w, h, inside]) => w >= 40 && h >= 40 && inside) && lay.noHScroll, 'phone: input and buttons fit in 390px, touch targets >= 40px', JSON.stringify(lay.btns));
  ok(parseFloat(lay.fontPx) >= 16, 'phone: input font >= 16px (no iOS zoom)', lay.fontPx);
  await p2.fill('.find-input', 'zebra'); await p2.dispatchEvent('.find-input', 'input');
  await p2.waitForFunction(() => /^\d+ \/ \d+/.test(document.querySelector('.find-count').textContent), null, { timeout: 15000 }); await wait(500);
  let pa = await p2.evaluate(() => { const hl = CSS.highlights.get('search-active'); const r = hl && [...hl][0]; const fr = document.querySelector('.frame').getBoundingClientRect(); const b = r && r.getBoundingClientRect(); return r ? { t: r.toString(), vis: b.left >= fr.left - 1 && b.right <= fr.right + 1 && b.top >= fr.top - 1 && b.bottom <= fr.bottom + 1 } : null; });
  ok(pa && pa.vis, 'phone: first hit visible', JSON.stringify(pa));
  await p2.screenshot({ path: SHOTS + '/search-phone.png' });
  await p2.locator('.findbar button[aria-label^="下一个"]').tap(); await wait(600);
  const cnt = await p2.textContent('.find-count');
  pa = await p2.evaluate(() => { const hl = CSS.highlights.get('search-active'); const r = hl && [...hl][0]; const fr = document.querySelector('.frame').getBoundingClientRect(); const b = r && r.getBoundingClientRect(); return r ? { vis: b.left >= fr.left - 1 && b.right <= fr.right + 1 && b.top >= fr.top - 1 && b.bottom <= fr.bottom + 1 } : null; });
  ok(cnt === `2 / ${ZEBRA}` && pa && pa.vis, 'phone: next button (tap) moves to hit 2, visible', cnt);
  // a tap on the middle of the page while searching must not bring the toolbar over the find bar
  await p2.touchscreen.tap(fr.x + fr.width * 0.5, fr.y + fr.height * 0.5); await wait(500);
  ok(await p2.evaluate(() => document.querySelector('.topbar').getBoundingClientRect().bottom <= 1 && document.querySelector('.findbar').offsetHeight > 0), 'phone: toolbar stays hidden under the find bar');
  await p2.locator('.find-close').tap(); await wait(500);
  ok(await p2.evaluate(() => !window.__pyreader.reader.find.isOpen && !CSS.highlights.get('search-active') && !CSS.highlights.get('search')), 'phone: close button clears the highlights');
  console.log('console logs (phone):', logs2);
  await b2.close();
  if (logs2.some((l) => l.startsWith('[pageerror]'))) { ok(false, 'phone: no page errors', logs2.join('|')); }
}

ok(!logs.some((l) => l.startsWith('[pageerror]')), 'no page errors on desktop', logs.filter((l) => l.startsWith('[pageerror]')).join('|'));
console.log(fails ? `\n${fails} FAILURES` : '\nALL OK');
process.exit(fails ? 1 : 0);
