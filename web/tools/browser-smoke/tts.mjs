// Read aloud (speechSynthesis replaced by a fake that "speaks" each sentence in 30 ms) + first-open hint + TOC label.
import { launch, BOOKS, SHOTS, URL0, wait } from './lib.mjs';
import fs from 'node:fs';
fs.mkdirSync(SHOTS, { recursive: true });
let fails = 0;
const ok = (cond, msg, extra = '') => { console.log(`${cond ? 'PASS' : 'FAIL'}  ${msg} ${extra}`); if (!cond) fails++; };

const { browser, page, logs } = await launch({ width: 900, height: 700 });
await page.addInitScript(() => {
  const log = (window.__spoken = []);
  class U { constructor(t) { this.text = t; } }
  let cur = null, paused = false;
  const synth = {
    getVoices: () => [],
    speak(u) { cur = u; log.push(u.text); setTimeout(() => { if (!paused && cur === u) { cur = null; u.onend && u.onend({}); } }, 30); },
    cancel() { const u = cur; cur = null; if (u && u.onerror) setTimeout(() => u.onerror({ error: 'canceled' }), 0); },
    pause() { paused = true; },
    resume() { paused = false; const u = cur; if (u) setTimeout(() => { cur = null; u.onend && u.onend({}); }, 0); },
    addEventListener() {}, removeEventListener() {},
  };
  Object.defineProperty(window, 'speechSynthesis', { value: synth, configurable: true });
  window.SpeechSynthesisUtterance = U;
});
await page.goto(URL0);
await page.waitForSelector('.shelf-head');
await page.setInputFiles('input[type=file]', [BOOKS + '/sample.epub', BOOKS + '/long.txt']);
await page.waitForFunction(() => document.querySelectorAll('.book').length >= 2, null, { timeout: 30000 });
await wait(500);

const R = () => page.evaluate(() => {
  const r = window.__pyreader.reader, pg = r.pg;
  const hl = CSS.highlights && CSS.highlights.get('tts');
  const rg = hl ? [...hl][0] : null;
  const fr = pg.frame.getBoundingClientRect();
  const rr = rg ? rg.getBoundingClientRect() : null;
  return { s: r.s, page: pg.page, cols: pg.cols, state: r.tts.state, spoken: window.__spoken.length, last: window.__spoken.at(-1) || '',
    hlText: rg ? rg.toString() : null, hlVisible: rr ? rr.left >= fr.left - 1 && rr.right <= fr.right + 1 : null, barOn: r.root.classList.contains('tts-on'),
    range: pg.pageRange(), rate: r.settings.ttsRate };
});

await page.locator('.book .t', { hasText: 'long' }).first().click();
await page.waitForSelector('.content p', { timeout: 20000 });
await wait(800);

// ---- first-open hint and a labelled TOC button
ok(await page.locator('.toast', { hasText: '点击页面中间呼出菜单' }).count() === 1, 'first-open hint shown');
ok((await page.locator('.topbar .icon-btn.labelled .lbl').textContent()) === '目录', 'TOC button carries a text label');
ok(await page.locator('.topbar button[aria-label="朗读"]').count() === 1, 'read-aloud button in the toolbar');

// ---- start with R
await page.keyboard.press('r'); await wait(500);
let st = await R();
console.log('reading', JSON.stringify(st));
ok(st.state === 'playing' && st.barOn, 'R starts reading, control bar visible');
ok(st.spoken >= 3, 'sentences are spoken one by one', `spoken=${st.spoken}`);
ok(st.hlText && st.hlText === st.last, 'highlight equals the sentence being spoken', `${st.hlText} | ${st.last}`);
ok(st.hlVisible === true, 'highlighted sentence is inside the frame');
await page.screenshot({ path: SHOTS + '/tts-playing.png' });

// ---- auto page turn while reading
const p0 = st.page;
await page.waitForFunction((p) => window.__pyreader.reader.pg.page > p, p0, { timeout: 15000 });
st = await R();
ok(st.page > p0 && st.state === 'playing', 'page turns by itself while reading', `${p0} -> ${st.page}`);
ok(st.range[0] <= (await page.evaluate(() => window.__pyreader.reader.pg.anchor)) + 400, 'anchor follows the reading');

// ---- pause / resume
await page.locator('.tts-bar button[aria-label="暂停朗读"]').click(); await wait(200);
st = await R(); const n1 = st.spoken;
ok(st.state === 'paused', 'pause button pauses');
await wait(400);
ok((await R()).spoken <= n1 + 1, 'nothing new is spoken while paused');
await page.locator('.tts-bar button[aria-label="继续朗读"]').click(); await wait(500);
st = await R();
ok(st.state === 'playing' && st.spoken > n1, 'resume continues', `${n1} -> ${st.spoken}`);

// ---- manual page turn while reading restarts from the new page
await page.keyboard.press('ArrowRight'); await wait(500);
st = await R();
const pageText = await page.evaluate(() => { const pg = window.__pyreader.reader.pg; const [a, b] = pg.pageRange(); return pg.fullText().slice(a, b); });
ok(st.state === 'playing', 'still reading after a manual page turn');
ok(await page.evaluate(([t, a]) => { const x = window.__spoken.slice(-3); return x.some((s) => t.includes(s.slice(0, 6))); }, [pageText, 0]), 'reading resumed on the new page', st.last);

// ---- speed buttons
await page.locator('.tts-bar button[aria-label="加快语速"]').click(); await wait(200);
ok((await R()).rate === 1.1, 'speed + raises rate to 1.1');

// ---- stop
await page.keyboard.press('r'); await wait(300);
st = await R(); const n2 = st.spoken;
ok(st.state === 'idle' && !st.barOn && st.hlText === null, 'R stops: bar hidden, highlight cleared');
await wait(400);
ok((await R()).spoken === n2, 'nothing is spoken after stop');

// ---- settings panel has the read-aloud controls
await page.keyboard.press('m'); await wait(300);
await page.locator('.topbar button[aria-label="阅读设置"]').click(); await wait(500);
ok(await page.locator('.panel').getByText('朗读语速').count() >= 1 && await page.locator('.panel select[aria-label="朗读音色"]').count() === 1, 'settings: rate and voice controls');
await page.locator('.panel select[aria-label="朗读音色"]').scrollIntoViewIfNeeded(); await wait(300);
await page.screenshot({ path: SHOTS + '/tts-settings.png' });
await page.keyboard.press('Escape'); await wait(300);

// ---- spread: reading keeps the page aligned to a spread
await page.setViewportSize({ width: 1400, height: 800 }); await wait(600);
await page.keyboard.press('r'); await wait(2500);
st = await R();
ok(st.cols === 2 && st.page % 2 === 0 && st.state === 'playing', 'spread: reading turns whole spreads', `page=${st.page}`);
await page.screenshot({ path: SHOTS + '/tts-spread.png' });
await page.keyboard.press('r'); await wait(300);
await page.setViewportSize({ width: 900, height: 700 }); await wait(500);

// ---- crossing into the next chapter (epub)
await page.evaluate(() => window.__pyreader.reader.ctx.goShelf()); await wait(500);
await page.locator('.book .t', { hasText: '测试之书' }).first().click();
await page.waitForSelector('.content p', { timeout: 20000 }); await wait(800);
await page.evaluate(() => window.__pyreader.reader.showSection(0, { end: true })); await wait(600);
const s0 = (await R()).s;
await page.keyboard.press('r');
await page.waitForFunction((s) => window.__pyreader.reader.s > s, s0, { timeout: 15000 }).catch(() => {});
st = await R();
ok(st.s === s0 + 1 && st.state === 'playing', 'reading continues into the next chapter', `s ${s0} -> ${st.s}`);
await page.keyboard.press('r'); await wait(300);

// ---- leaving the book stops reading
await page.keyboard.press('r'); await wait(300);
await page.evaluate(() => window.__pyreader.reader.ctx.goShelf()); await wait(500);
ok(await page.evaluate(() => window.__pyreader.reader.tts.state) === 'idle', 'going back to the shelf stops reading');

console.log('console logs:', logs);
console.log(fails ? `\n${fails} FAILURES` : '\nALL OK');
await browser.close();
process.exit(fails ? 1 : 0);
