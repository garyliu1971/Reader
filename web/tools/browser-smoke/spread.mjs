// Two-page spread + line-width cap. Real browser; see lib.mjs for setup.
import { launch, BOOKS, SHOTS, URL0, wait } from './lib.mjs';
import fs from 'node:fs';
fs.mkdirSync(SHOTS, { recursive: true });
let fails = 0;
const ok = (cond, msg, extra = '') => { console.log(`${cond ? 'PASS' : 'FAIL'}  ${msg} ${extra}`); if (!cond) fails++; };

const { browser, page, logs } = await launch({ width: 1400, height: 800 });
await page.goto(URL0);
await page.waitForSelector('.shelf-head');
await page.setInputFiles('input[type=file]', [BOOKS + '/sample.epub', BOOKS + '/long.txt']);
await page.waitForFunction(() => document.querySelectorAll('.book').length >= 2, null, { timeout: 30000 });
await wait(500);

const R = () => page.evaluate(() => {
  const r = window.__pyreader.reader, pg = r.pg;
  const [a, b] = pg.pageRange();
  const cr = pg.content.getBoundingClientRect(), fr = pg.frame.getBoundingClientRect();
  return { s: r.s, page: pg.page, cols: pg.cols, pageCount: pg.pageCount, anchor: pg.anchor, range: [a, b], pw: pg.pw, fw: pg.fw, mx: pg.mx,
    tw: pg.content.clientWidth, fs: r.settings.fontSize, label: r.posEl.textContent, mode: pg.mode,
    // how many distinct columns are visible inside the frame: x of the first char on the left and right page
    left: pg.charRect(a) && Math.round(pg.charRect(a).left - fr.left),
    pct: r.percent(), frameLeft: fr.left, contentLeft: cr.left };
});
const set = (k, v) => page.evaluate(([k, v]) => window.__pyreader.reader.setSetting(k, v), [k, v]);

await page.locator('.book .t', { hasText: '测试之书' }).first().click();
await page.waitForSelector('.content p', { timeout: 20000 });
await wait(800);

// ---- 1400 wide: spread, even page index, range covers two pages
let st = await R();
console.log('1400', JSON.stringify(st));
ok(st.cols === 2 && st.pw === 700, 'wide window -> 2 columns of half width', `cols=${st.cols} pw=${st.pw}`);
ok(st.page % 2 === 0, 'page is the left page of a spread');
ok(/^\d+-\d+\/\d+ /.test(st.label) || st.pageCount === 1, 'status shows "a-b/n"', st.label);
ok(st.tw <= Math.round(40 * st.fs) + 1, 'text column within line-width cap', `tw=${st.tw}`);
await page.screenshot({ path: SHOTS + '/spread-1400.png' });

// one key press advances by a whole spread, never an odd page
const p0 = st.page;
await page.keyboard.press('ArrowRight'); await wait(400);
st = await R();
ok(st.page === p0 + 2 && st.page % 2 === 0, 'next turns a whole spread', `page ${p0} -> ${st.page}`);
ok(st.range[0] <= st.anchor && st.anchor < st.range[1], 'anchor inside current spread');
const keep = { anchor: st.anchor, s: st.s };
await page.keyboard.press('ArrowLeft'); await wait(400);
st = await R();
ok(st.page === p0, 'prev goes back one spread', `page=${st.page}`);
await page.keyboard.press('ArrowRight'); await wait(400);

// ---- shrinking below the threshold: single page, anchor preserved; widening restores the spread
await page.setViewportSize({ width: 900, height: 800 }); await wait(600);
let a = await R();
ok(a.cols === 1 && a.pw === a.fw, '900 wide -> single page', `cols=${a.cols}`);
ok(a.anchor === keep.anchor && a.s === keep.s && a.range[0] <= keep.anchor && keep.anchor < a.range[1], 'anchor preserved spread -> single', JSON.stringify(a.range));
await page.setViewportSize({ width: 1400, height: 800 }); await wait(600);
a = await R();
ok(a.cols === 2 && a.range[0] <= keep.anchor && keep.anchor < a.range[1], 'anchor preserved single -> spread', JSON.stringify(a.range));
ok(a.page % 2 === 0, 'page re-aligned to spread start');

// ---- spread off
await set('spread', false); await wait(500);
a = await R();
ok(a.cols === 1 && a.pw === a.fw, 'spread disabled -> single page even when wide', `cols=${a.cols}`);
ok(a.tw <= Math.round(40 * a.fs) + 1 && a.mx > 28, 'single wide page: text capped and centred by larger margins', `tw=${a.tw} mx=${a.mx}`);
await page.screenshot({ path: SHOTS + '/single-1400-capped.png' });
await set('lineWidth', 0); await wait(400);
a = await R();
ok(a.tw >= a.fw - 2 * 28 - 1, 'lineWidth 0 -> full width', `tw=${a.tw}`);
await set('lineWidth', 40); await set('spread', true); await wait(500);

// ---- last spread: end detection and percent
await page.evaluate(() => window.__pyreader.reader.showSection(0, { end: true })); await wait(600);
a = await R();
ok(a.page + a.cols >= a.pageCount, 'gotoEnd shows the last spread', `page=${a.page}/${a.pageCount}`);
const r = await page.evaluate(() => window.__pyreader.reader.pg.next());
ok(r === 'end', 'next on the last spread reports end (moves to next chapter)');

// ---- scroll mode is never a spread; txt book
await set('mode', 'scroll'); await wait(500);
a = await R();
ok(a.cols === 1 && a.mode === 'scroll', 'scroll mode: single column');
await page.screenshot({ path: SHOTS + '/scroll-1400.png' });
await set('mode', 'paged'); await wait(500);

// ---- mouse wheel / tap zones on a spread
await page.evaluate(() => window.__pyreader.reader.showSection(1, { start: true })); await wait(600);
const before = await R();
await page.mouse.click(1400 - 30, 400); await wait(500);   // right tap zone
let after = await R();
ok(after.page === before.page + 2, 'right-edge tap turns a spread', `${before.page} -> ${after.page}`);
await page.mouse.click(30, 400); await wait(500);          // left tap zone
after = await R();
ok(after.page === before.page, 'left-edge tap goes back a spread');

// ---- long book: many spreads in a row never land on an odd page, anchor only moves forward, end is reached
await page.evaluate(() => window.__pyreader.reader.ctx.goShelf()); await wait(500);
await page.locator('.book .t', { hasText: 'long' }).first().click();
await page.waitForSelector('.content p', { timeout: 20000 });
await wait(800);
let prevAnchor = -1, prevS = -1, oddPages = 0, backwards = 0, turns = 0;
for (let i = 0; i < 12; i++) {
  const x = await R();
  if (x.page % 2 !== 0) oddPages++;
  if (x.s === prevS && x.anchor < prevAnchor) backwards++;
  prevAnchor = x.anchor; prevS = x.s; turns++;
  await page.keyboard.press('ArrowRight'); await wait(250);
}
ok(oddPages === 0 && backwards === 0, `long book: ${turns} spread turns stay aligned and move forward`, `odd=${oddPages} back=${backwards}`);
await page.evaluate(() => window.__pyreader.reader.showSection(0, { end: true })); await wait(600);
a = await R();
ok(a.page + a.cols >= a.pageCount && a.page % 2 === 0, 'long book: last spread of a chapter', `page=${a.page}/${a.pageCount}`);

// ---- settings persisted through normalize
const norm = await page.evaluate(() => { const s = window.__pyreader.reader.settings; return { spread: s.spread, lw: s.lineWidth }; });
ok(norm.spread === true && norm.lw === 40, 'settings hold spread/lineWidth', JSON.stringify(norm));

console.log('console logs:', logs);
console.log(fails ? `\n${fails} FAILURES` : '\nALL OK');
await browser.close();
process.exit(fails ? 1 : 0);
