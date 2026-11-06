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
  await page.evaluate(() => window.__pyreader.reader.openSettings()); await wait(500);
  await run(page, `settings sheet ${scheme}`);
  await browser.close();
}
