import { launch, BOOKS, SHOTS, URL0, wait } from './lib.mjs';
import fs from 'node:fs';
const axeSrc = fs.readFileSync(process.env.AXE_JS || './node_modules/axe-core/axe.min.js', 'utf8');
const run = async (page, label) => {
  await page.evaluate(axeSrc);
  const r = await page.evaluate(async () => { const res = await axe.run(document, { resultTypes: ['violations'] }); return res.violations.map((v) => ({ id: v.id, impact: v.impact, n: v.nodes.length, help: v.help, sample: v.nodes[0].html.slice(0, 160), fix: v.nodes[0].failureSummary?.slice(0, 200) })); });
  console.log(`== ${label}: ${r.length} violation types`);
  for (const v of r) console.log(JSON.stringify(v));
};
for (const scheme of ['light', 'dark']) {
  const { browser, page, logs } = await launch({ width: 390, height: 780, scheme });
  // fake speechSynthesis whose utterances never finish, so the read-aloud bar stays up for scanning
  await page.addInitScript(() => {
    class U { constructor(t) { this.text = t; } }
    Object.defineProperty(window, 'speechSynthesis', { value: { getVoices: () => [], speak() {}, cancel() {}, pause() {}, resume() {}, addEventListener() {}, removeEventListener() {} }, configurable: true });
    window.SpeechSynthesisUtterance = U;
    // fake on-device Translator so the 翻译 button / popover can be scanned
    window.Translator = { availability: async () => 'available', create: async () => ({ translate: async (t) => '【译】' + t }) };
    window.LanguageDetector = { create: async () => ({ detect: async () => [{ detectedLanguage: 'en', confidence: 0.9 }] }) };
  });
  await page.goto(URL0);
  await page.waitForSelector('.shelf-head');
  await page.setInputFiles('input[type=file]', [BOOKS + '/sample.epub', BOOKS + '/notes.md']);
  await page.waitForFunction(() => document.querySelectorAll('.book').length >= 2, null, { timeout: 30000 });
  await wait(800);
  await run(page, `shelf ${scheme}`);
  await page.locator('.book .book-open').first().click();
  await page.waitForSelector('.content p'); await wait(800);
  await page.evaluate(() => window.__pyreader.reader.setBars(true)); await wait(400);
  await run(page, `reader ${scheme} (bars on)`);
  // select text -> floating 翻译 button -> translation popover
  await page.evaluate(() => window.__pyreader.reader.setBars(false)); await wait(300);
  const sel = await page.evaluate(() => { const p = [...document.querySelectorAll('.content p')].find((x) => x.textContent.trim().length >= 5); if (!p) return ''; const r = document.createRange(); r.selectNodeContents(p); const s = getSelection(); s.removeAllRanges(); s.addRange(r); return r.toString(); });
  await wait(600);
  console.log('translate button visible:', await page.evaluate(() => { const b = document.querySelector('.tr-btn'); return !!b && !b.hidden; }), JSON.stringify(sel));
  await run(page, `reader with translate button ${scheme}`);
  await page.locator('.tr-btn').click(); await wait(500);
  console.log('translate popover visible:', await page.evaluate(() => { const b = document.querySelector('.tr-pop'); return !!b && !b.hidden && /【译】/.test(b.textContent); }));
  await page.screenshot({ path: SHOTS + `/a11y-translate-pop-${scheme}.png` });
  await run(page, `reader with translate popover ${scheme}`);
  await page.keyboard.press('Escape'); await wait(300);
  await page.evaluate(() => window.__pyreader.reader.setBars(true)); await wait(300);
  await page.evaluate(() => window.__pyreader.reader.openSettings()); await wait(500);
  await run(page, `settings sheet ${scheme}`);
  await page.keyboard.press('Escape'); await wait(300);
  await page.evaluate(() => window.__pyreader.reader.setBars(true));
  await page.keyboard.press('r'); await wait(500);
  console.log('tts bar visible:', await page.evaluate(() => { const b = document.querySelector('.tts-bar'); return !!b && getComputedStyle(b).display !== 'none'; }));
  await run(page, `reading, tts bar on ${scheme}`);
  // back on the shelf with the "继续阅读" card showing (the first scan above ran before any book was opened);
  // merely opening a book no longer offers it, so move the position first (the first book may be a single page)
  await page.evaluate(() => window.__pyreader.reader.seekPercent(40)); await wait(1500);
  await page.evaluate(() => window.__pyreader.reader.ctx.goShelf()); await wait(800);
  console.log('continue card visible:', await page.evaluate(() => { const c = document.querySelector('.continue'); return !!c && c.getBoundingClientRect().height > 0; }));
  await run(page, `shelf with continue card ${scheme}`);
  await page.screenshot({ path: SHOTS + `/a11y-continue-${scheme}.png` });
  await browser.close();
}
