// Select text -> 翻译 button -> popover (Translator API replaced by a fake that prefixes 【译】). Real browser; see lib.mjs.
import { launch, BOOKS, SHOTS, URL0, wait } from './lib.mjs';
import fs from 'node:fs';
fs.mkdirSync(SHOTS, { recursive: true });
let fails = 0;
const ok = (cond, msg, extra = '') => { console.log(`${cond ? 'PASS' : 'FAIL'}  ${msg} ${extra}`); if (!cond) fails++; };

const EN = SHOTS + '/en-book.txt';
fs.writeFileSync(EN, 'Chapter 1\n\n' + 'The quick brown fox jumps over the lazy dog. '.repeat(60) + '\n\n안녕하세요. 이것은 시험입니다.\n\n' + 'It was a bright cold day in April. '.repeat(80), 'utf8');

const { browser, page, logs } = await launch({ width: 900, height: 700 });
await page.addInitScript(() => {
  window.__trCreate = 0;
  window.Translator = {
    availability: async () => 'available',
    create: async (o) => {
      if (window.__failNext) { window.__failNext = false; throw new Error('fake fail'); }
      if (window.__notAllowedOnce) { window.__notAllowedOnce = false; const e = new Error('needs user activation'); e.name = 'NotAllowedError'; throw e; }
      window.__trCreate++;
      if (o.monitor) o.monitor({ addEventListener: (_, cb) => { cb({ loaded: 0.5 }); cb({ loaded: 1 }); } });
      return { translate: async (t) => (window.__longTr ? '【译】' + t.repeat(30) : '【译】' + t) };
    },
  };
  window.LanguageDetector = { create: async () => ({ detect: async (t) => [{ detectedLanguage: /[\uac00-\ud7af]/.test(t) ? 'ko' : 'en', confidence: 0.9 }] }) };
});
await page.goto(URL0);
await page.waitForSelector('.shelf-head');
await page.setInputFiles('input[type=file]', [EN, BOOKS + '/long.txt']);
await page.waitForFunction(() => document.querySelectorAll('.book').length >= 2, null, { timeout: 30000 });
await wait(500);

const selectIn = (paraIndex, a, b) => page.evaluate(([i, a, b]) => {
  const ps = [...document.querySelectorAll('.content p')];
  const p = ps[i] || ps[0];
  const node = document.createTreeWalker(p, NodeFilter.SHOW_TEXT).nextNode();
  const r = document.createRange(); r.setStart(node, a); r.setEnd(node, Math.min(b, node.nodeValue.length));
  const s = getSelection(); s.removeAllRanges(); s.addRange(r);
  return r.toString();
}, [paraIndex, a, b]);
const btn = () => page.evaluate(() => { const b = document.querySelector('.tr-btn'); if (!b || b.hidden) return null; const r = b.getBoundingClientRect(); return { x: r.left, y: r.top, w: r.width, h: r.height, vw: innerWidth, vh: innerHeight }; });
const pop = () => page.evaluate(() => { const p = document.querySelector('.tr-pop'); if (!p || p.hidden) return null; const r = p.getBoundingClientRect(); return { head: p.querySelector('.tr-head').textContent, body: p.querySelector('.tr-body').textContent, l: r.left, t: r.top, r: r.right, b: r.bottom, vw: innerWidth, vh: innerHeight }; });
const open = async (title) => { await page.locator('.book .t', { hasText: title }).first().click(); await page.waitForSelector('.content p', { timeout: 20000 }); await wait(900); };

await open('en-book');

// ---- selecting shows the button, near the selection and inside the viewport
const picked = await selectIn(0, 0, 40);
await wait(700);
let b = await btn();
console.log('button', JSON.stringify(b), JSON.stringify(picked));
ok(b && b.x >= 0 && b.x + b.w <= b.vw && b.y >= 0 && b.y + b.h <= b.vh, 'translate button appears inside the viewport');
await page.screenshot({ path: SHOTS + '/translate-btn.png' });

// ---- click -> popover with the translation and direction
await page.locator('.tr-btn').click(); await wait(600);
let p = await pop();
console.log('popover', JSON.stringify(p));
ok(p && p.body.startsWith('【译】The quick brown fox'), 'popover shows the translation', p && p.body);
ok(p && p.head === '英语 → 中文', 'direction shown (英语 → 中文)', p && p.head);
ok(p && p.l >= 0 && p.r <= p.vw && p.t >= 0 && p.b <= p.vh, 'popover is fully on screen');
ok((await btn()) === null, 'button is replaced by the popover');
await page.screenshot({ path: SHOTS + '/translate-pop.png' });

// ---- Escape closes; a second selection reuses the translator (no new create)
const created = await page.evaluate(() => window.__trCreate);
await page.keyboard.press('Escape'); await wait(300);
ok((await pop()) === null, 'Escape closes the popover');
await selectIn(0, 50, 90); await wait(700);
await page.locator('.tr-btn').click(); await wait(500);
ok((await page.evaluate(() => window.__trCreate)) === created, 'same direction reuses the translator', `creates=${created}`);

// ---- turning the page hides it
await page.keyboard.press('ArrowRight'); await wait(500);
ok((await pop()) === null && (await btn()) === null, 'turning the page hides the popover');

// ---- collapsing the selection hides the button
await selectIn(0, 0, 30); await wait(700);
ok((await btn()) !== null, 'button shown again for a new selection');
await page.evaluate(() => getSelection().removeAllRanges()); await wait(700);
ok((await btn()) === null, 'clearing the selection hides the button');

// ---- text outside the book (toolbar title) never offers translation
await page.keyboard.press('m'); await wait(300);
await page.evaluate(() => { const t = document.querySelector('.topbar .title'); const r = document.createRange(); r.selectNodeContents(t); const s = getSelection(); s.removeAllRanges(); s.addRange(r); });
await wait(700);
ok((await btn()) === null, 'selection outside the page text does not show the button');
await page.keyboard.press('m'); await wait(200);

// ---- target = English never maps English onto itself
await page.evaluate(() => window.__pyreader.reader.setSetting('trTarget', 'en'));
await selectIn(0, 0, 40); await wait(700);
await page.locator('.tr-btn').click(); await wait(500);
p = await pop();
ok(p && p.head === '英语 → 中文', 'English text with target English still goes to Chinese', p && p.head);
await page.keyboard.press('Escape');
await page.evaluate(() => window.__pyreader.reader.setSetting('trTarget', 'zh'));

// ---- engine failure is shown, not swallowed (Korean paragraph = new direction, creation fails once)
await page.evaluate(() => { window.__failNext = true; });
const ko = await page.evaluate(() => { const ps = [...document.querySelectorAll('.content p')]; return ps.findIndex((x) => /안녕/.test(x.textContent)); });
ok(ko >= 0, 'test book has a Korean paragraph', `index=${ko}`);
if (ko >= 0) {
  // jump to its page first
  await page.evaluate(() => { const r = window.__pyreader.reader; const i = r.pg.fullText().indexOf('안녕'); r.pg.gotoOffset(i); });
  await wait(500);
  await selectIn(ko, 0, 10); await wait(700);
  await page.locator('.tr-btn').click(); await wait(600);
  p = await pop();
  ok(p && p.head === '无法翻译' && /翻译器启动失败/.test(p.body), 'a failing translator shows an error in the popover', p && `${p.head}: ${p.body}`);
  await page.keyboard.press('Escape');
}

// ---- NotAllowedError (browser wants a fresh click to download a model): message says so, clicking 翻译 again works
if (ko >= 0) {
  await page.evaluate(() => { window.__notAllowedOnce = true; });
  await selectIn(ko, 0, 10); await wait(700);
  await page.locator('.tr-btn').click(); await wait(600);
  p = await pop();
  ok(p && /再点一次/.test(p.body), 'NotAllowedError: popover asks for another click', p && `${p.head}: ${p.body}`);
  b = await btn();
  ok(b !== null, 'NotAllowedError: the 翻译 button is available again', JSON.stringify(b));
  if (b) {
    await page.locator('.tr-btn').click(); await wait(600);
    p = await pop();
    ok(p && p.head === '韩语 → 中文' && p.body.startsWith('【译】안녕'), 'NotAllowedError: second click translates', p && `${p.head}: ${p.body}`);
  }
  await page.keyboard.press('Escape'); await wait(200);
}

// ---- mouse double-click vs single click (needs a fresh page of the English text)
const pgState = () => page.evaluate(() => { const r = window.__pyreader.reader; return { page: r.pg.page, sel: getSelection().toString(), bars: r.barsOn() }; });
await page.evaluate(() => { const r = window.__pyreader.reader; r.pg.gotoOffset(Math.floor(r.pg.fullText().length / 3)); getSelection().removeAllRanges(); });   // mid-book, so a left tap WOULD turn back
await wait(700);
// a word on the current page whose centre lies in the left (or right) 30% of the reading frame
const wordAt = (zone) => page.evaluate((zone) => {
  const f = window.__pyreader.reader.pg.frame.getBoundingClientRect();
  const lo = zone === 'left' ? f.left + 0.06 * f.width : f.left + 0.74 * f.width, hi = zone === 'left' ? f.left + 0.24 * f.width : f.left + 0.94 * f.width;
  const w = document.createTreeWalker(document.querySelector('.content'), NodeFilter.SHOW_TEXT);
  for (let n; (n = w.nextNode());) {
    const re = /[A-Za-z]{5,}/g; let m;
    while ((m = re.exec(n.nodeValue))) {
      const r = document.createRange(); r.setStart(n, m.index); r.setEnd(n, m.index + m[0].length);
      const rc = r.getClientRects()[0];
      if (!rc || rc.width < 20) continue;
      const cx = rc.left + rc.width / 2, cy = rc.top + rc.height / 2;
      if (cx > lo && cx < hi && cy > f.top + 60 && cy < f.bottom - 60 && rc.left >= lo - 5 && rc.right <= hi + 5) return { x: cx, y: cy, word: m[0] };
    }
  }
  return null;
}, zone);
let st = await pgState();
ok(!st.bars, 'toolbars are hidden before the click tests');
ok(st.page > 0, 'not on the first page (a left tap would really turn back)', `page=${st.page}`);
const lw = await wordAt('left');
ok(lw !== null, 'found a word in the left 30% zone', JSON.stringify(lw));
if (lw) {
  const before = st.page;
  await page.mouse.dblclick(lw.x, lw.y); await wait(900);
  st = await pgState(); b = await btn();
  ok(st.page === before, 'mouse double-click in the left zone does not turn the page', `page ${before} -> ${st.page}`);
  ok(st.sel.trim().length > 0, 'double-click selected the word', JSON.stringify(st.sel));
  ok(b !== null, 'double-click on a word shows the 翻译 button');
  // two clicks 80 ms apart behave the same
  await page.evaluate(() => getSelection().removeAllRanges()); await wait(700);
  await page.mouse.move(lw.x, lw.y);
  await page.mouse.down({ clickCount: 1 }); await page.mouse.up({ clickCount: 1 }); await wait(80);
  await page.mouse.down({ clickCount: 2 }); await page.mouse.up({ clickCount: 2 }); await wait(900);   // 2nd press of a real double-click
  st = await pgState();
  ok(st.page === before && st.sel.trim().length > 0, 'two quick clicks 80 ms apart: no page turn, word selected', `page ${before} -> ${st.page} sel=${JSON.stringify(st.sel)}`);
}
await page.evaluate(() => getSelection().removeAllRanges()); await wait(700);
const rw = await wordAt('right');
ok(rw !== null, 'found a word in the right 30% zone');
if (rw) {
  const before = (await pgState()).page;
  await page.mouse.click(rw.x, rw.y); await wait(400);
  const after = await pgState();
  ok(after.page > before, 'single mouse click in the right 30% zone still turns the page', `page ${before} -> ${after.page}`);
}

// ---- Space on the focused 关闭 button closes the popover and does not turn the page
await page.evaluate(() => { const r = window.__pyreader.reader; r.pg.gotoOffset(0); getSelection().removeAllRanges(); }); await wait(700);
await selectIn(0, 0, 40); await wait(700);
await page.locator('.tr-btn').click(); await wait(600);
ok((await pop()) !== null, 'popover open for the keyboard test');
const pg0 = (await pgState()).page;
await page.focus('.tr-pop .tr-actions .btn:last-child');
ok((await page.evaluate(() => document.activeElement.textContent)) === '关闭', 'focus is on 关闭');
await page.keyboard.press('Space'); await wait(500);
ok((await pop()) === null, 'Space on 关闭 closes the popover');
ok((await pgState()).page === pg0, 'Space on 关闭 did not turn the page', `page ${pg0} -> ${(await pgState()).page}`);
// Enter on 复制译文 activates it and does not turn the page either
await selectIn(0, 0, 40); await wait(700);
await page.locator('.tr-btn').click(); await wait(600);
await page.focus('.tr-pop .tr-actions .btn:first-child');
await page.keyboard.press('Enter'); await wait(500);
ok((await pgState()).page === pg0, 'Enter on 复制译文 did not turn the page');
await page.keyboard.press('Escape'); await wait(300);

// ---- a very long translation near the bottom of the viewport stays on screen, buttons reachable
await page.evaluate(() => { window.__longTr = true; const r = window.__pyreader.reader; r.pg.gotoOffset(0); getSelection().removeAllRanges(); }); await wait(700);
const lastLine = await page.evaluate(() => {   // select ~70 characters at the end of the current page
  const r = window.__pyreader.reader; const e = r.pg.pageRange()[1]; const rg = r.pg.rangeFor(e - 90, e - 20);
  const s = getSelection(); s.removeAllRanges(); s.addRange(rg);
  const rc = rg.getClientRects(); return { bottom: rc[rc.length - 1].bottom, vh: innerHeight, text: rg.toString() };
});
console.log('long-translation selection', JSON.stringify(lastLine));
await wait(700);
await page.locator('.tr-btn').click(); await wait(700);
const geo = () => page.evaluate(() => {
  const pp = document.querySelector('.tr-pop'); if (!pp || pp.hidden) return null;
  const r = pp.getBoundingClientRect();
  const bs = [...pp.querySelectorAll('.tr-actions .btn')].map((x) => { const q = x.getBoundingClientRect(); return { t: x.textContent, top: q.top, bottom: q.bottom, left: q.left, right: q.right }; });
  return { top: r.top, bottom: r.bottom, vh: innerHeight, vw: innerWidth, bs, bodyLen: pp.querySelector('.tr-body').textContent.length };
});
const g = await geo();
console.log('long popover', JSON.stringify(g));
ok(g && g.bodyLen > 1500, 'the fake long translation is long', g && g.bodyLen);
ok(g && g.top >= 0 && g.bottom <= g.vh, 'long popover: bottom edge inside the viewport', g && `top=${g.top} bottom=${g.bottom} vh=${g.vh}`);
ok(g && g.bs.length === 2 && g.bs.every((x) => x.top >= 0 && x.bottom <= g.vh && x.left >= 0 && x.right <= g.vw), 'long popover: 复制译文 / 关闭 inside the viewport', g && JSON.stringify(g.bs));
ok(g && g.bs.every((x) => x.top >= g.top - 1 && x.bottom <= g.bottom + 1), 'long popover: the buttons are visible inside the popover (not scrolled out of it)', g && JSON.stringify([g.top, g.bottom, g.bs.map((x) => [x.top, x.bottom])]));
await page.screenshot({ path: SHOTS + '/translate-long.png' });

// ---- resizing the viewport while the popover is open hides it
await page.setViewportSize({ width: 800, height: 640 }); await wait(900);
ok((await pop()) === null && (await btn()) === null, 'resizing the viewport hides the open popover');
await page.setViewportSize({ width: 900, height: 700 }); await wait(700);
await page.evaluate(() => { window.__longTr = false; getSelection().removeAllRanges(); });

// ---- Chinese text goes to English
await page.evaluate(() => window.__pyreader.reader.ctx.goShelf()); await wait(600);
await open('long');
await selectIn(0, 0, 12); await wait(700);
await page.locator('.tr-btn').click(); await wait(500);
p = await pop();
ok(p && p.head === '中文 → 英语' && p.body.startsWith('【译】'), 'Chinese selection is translated to English', p && p.head);
await page.keyboard.press('Escape');

// ---- continuous-scroll mode: scrolling must not leave the button / popover floating over unrelated text
await page.evaluate(() => window.__pyreader.reader.ctx.goShelf()); await wait(600);
await open('en-book');
await page.evaluate(() => window.__pyreader.reader.setSetting('mode', 'scroll')); await wait(700);
await page.evaluate(() => { window.__pyreader.reader.pg.frame.scrollTop = 0; }); await wait(500);
await selectIn(0, 0, 40); await wait(700);
ok((await btn()) !== null, 'scroll mode: button shown for a selection');
await page.evaluate(() => { const f = window.__pyreader.reader.pg.frame; f.scrollTop += 150; }); await wait(500);
ok((await btn()) === null && (await pop()) === null, 'scroll mode: scrolling hides the button');
await selectIn(0, 700, 740); await wait(700);
await page.locator('.tr-btn').click(); await wait(500);
ok((await pop()) !== null, 'scroll mode: popover opens');
await page.evaluate(() => { const f = window.__pyreader.reader.pg.frame; f.scrollTop += 150; }); await wait(500);
ok((await pop()) === null, 'scroll mode: scrolling closes the popover');
await page.evaluate(() => window.__pyreader.reader.setSetting('mode', 'paged')); await wait(700);

// ---- settings panel
await page.keyboard.press('m'); await wait(300);
await page.locator('.topbar button[aria-label="阅读设置"]').click(); await wait(500);
ok(await page.locator('.panel').getByText('划词翻译：翻译成').count() >= 1, 'settings: translate target control');
await page.keyboard.press('Escape'); await wait(300);

// ---- a browser without the API: no button, settings explain why
const ctx2 = await browser.newContext({ viewport: { width: 900, height: 700 } });
const p2 = await ctx2.newPage();
await p2.addInitScript(() => { Object.defineProperty(self, 'Translator', { value: undefined, configurable: true }); });
await p2.goto(URL0);
await p2.waitForSelector('.shelf-head');
await p2.setInputFiles('input[type=file]', [EN]);
await p2.waitForFunction(() => document.querySelectorAll('.book').length >= 1, null, { timeout: 30000 });
await wait(400);
await p2.locator('.book .t', { hasText: 'en-book' }).first().click();
await p2.waitForSelector('.content p', { timeout: 20000 }); await wait(800);
await p2.evaluate(() => { const p = document.querySelector('.content p'); const n = document.createTreeWalker(p, NodeFilter.SHOW_TEXT).nextNode(); const r = document.createRange(); r.setStart(n, 0); r.setEnd(n, 5); const s = getSelection(); s.removeAllRanges(); s.addRange(r); });
await wait(700);
ok(await p2.evaluate(() => { const b = document.querySelector('.tr-btn'); return !b || b.hidden; }), 'unsupported browser: no translate button');
await p2.keyboard.press('m'); await wait(300);
await p2.locator('.topbar button[aria-label="阅读设置"]').click(); await wait(500);
ok(await p2.locator('.panel').getByText('需要桌面版 Chrome 或 Edge').count() >= 1, 'unsupported browser: settings say why');
await ctx2.close();

console.log('console logs:', logs);
console.log(fails ? `\n${fails} FAILURES` : '\nALL OK');
await browser.close();
process.exit(fails ? 1 : 0);
