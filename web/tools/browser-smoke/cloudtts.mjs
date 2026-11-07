// Online read-aloud against a local STUB of the /tts/v1 API (never the real server): token header, prefetch, play through
// <audio>, settings UI, quota fallback. Starts its own static server for web/ + stub on port 8131 (override with STUB_PORT).
import { launch, BOOKS, SHOTS, wait } from './lib.mjs';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
fs.mkdirSync(SHOTS, { recursive: true });
let fails = 0;
const ok = (cond, msg, extra = '') => { console.log(`${cond ? 'PASS' : 'FAIL'}  ${msg} ${extra}`); if (!cond) fails++; };

const WEB = path.resolve(process.env.WEB_DIR || path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..'));
const PORT = Number(process.env.STUB_PORT || 8131);
const TOKEN = 'smoke-token';
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png', '.webmanifest': 'application/json' };
function wav(ms = 250) {   // silent 8 kHz mono 8-bit PCM
  const n = 8 * ms; const b = Buffer.alloc(44 + n, 0x80);
  b.write('RIFF', 0); b.writeUInt32LE(36 + n, 4); b.write('WAVEfmt ', 8); b.writeUInt32LE(16, 16); b.writeUInt16LE(1, 20); b.writeUInt16LE(1, 22);
  b.writeUInt32LE(8000, 24); b.writeUInt32LE(8000, 28); b.writeUInt16LE(1, 32); b.writeUInt16LE(8, 34); b.write('data', 36); b.writeUInt32LE(n, 40);
  return b;
}
const stub = { speak: [], mode: 'ok' };
const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://x');
  if (u.pathname.startsWith('/tts/v1/')) {
    if (req.headers.authorization !== 'Bearer ' + TOKEN) { res.writeHead(401).end(); return; }
    if (u.pathname.endsWith('/voices')) { res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify([{ id: 'zh-CN-YunxiNeural', name: '云希', lang: 'zh-CN', gender: 'Male' }, { id: 'en-US-AriaNeural', name: 'Aria', lang: 'en-US', gender: 'Female' }])); return; }
    if (u.pathname.endsWith('/usage')) { res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({ usedToday: 12, limitToday: 60000, usedMonth: 99, limitMonth: 450000, backend: 'stub' })); return; }
    let body = ''; req.on('data', (c) => { body += c; });
    req.on('end', () => {
      stub.speak.push(JSON.parse(body || '{}'));
      if (stub.mode === 'quota') { res.writeHead(429, { 'content-type': 'application/json' }).end(JSON.stringify({ error: 'quota', retryAfter: 3600 })); return; }
      res.writeHead(200, { 'content-type': 'audio/mpeg' }).end(wav());
    });
    return;
  }
  let p = path.join(WEB, decodeURIComponent(u.pathname === '/' ? '/index.html' : u.pathname));
  if (!p.startsWith(WEB) || !fs.existsSync(p) || fs.statSync(p).isDirectory()) { res.writeHead(404).end(); return; }
  res.writeHead(200, { 'content-type': MIME[path.extname(p)] || 'application/octet-stream' }).end(fs.readFileSync(p));
});
await new Promise((r) => server.listen(PORT, '127.0.0.1', r));
const APP = `http://127.0.0.1:${PORT}/index.html`;

const { browser, page } = await launch({ width: 900, height: 700 });
try {
  await page.addInitScript(() => {   // the browser engine is a fake, so a fallback is observable
    window.__spoken = [];
    class U { constructor(t) { this.text = t; } }
    let cur = null;
    Object.defineProperty(window, 'speechSynthesis', { configurable: true, value: {
      getVoices: () => [{ voiceURI: 'v', name: 'V', lang: 'zh-CN', localService: true }],
      speak(u) { cur = u; window.__spoken.push(u.text); setTimeout(() => { if (cur === u) { cur = null; u.onend && u.onend({}); } }, 30); },
      cancel() { const u = cur; cur = null; if (u && u.onerror) setTimeout(() => u.onerror({ error: 'canceled' }), 0); }, pause() {}, resume() {}, addEventListener() {}, removeEventListener() {},
    } });
    window.SpeechSynthesisUtterance = U;
  });
  await page.goto(APP);
  await page.waitForSelector('.shelf-head');
  await page.setInputFiles('input[type=file]', [BOOKS + '/long.txt']);
  await page.waitForFunction(() => document.querySelectorAll('.book').length >= 1, null, { timeout: 30000 });

  // ---- settings dialog: consent, token, voices, usage, test
  await page.locator('button[aria-label="设置"], .shelf-head button:has-text("设置")').first().click();
  await page.waitForSelector('#cloud-enabled');
  page.once('dialog', (d) => d.accept());
  await page.locator('input[aria-label="在线朗读令牌"]').fill(TOKEN);
  await page.locator('#cloud-enabled').check();
  await page.locator('button:has-text("同意并开启")').click();
  await wait(300);
  await page.locator('button:has-text("刷新音色")').click(); await wait(500);
  ok((await page.locator('select[aria-label="在线音色"] option').count()) === 3, 'voice list filled from /voices (auto + 2)');
  await page.locator('button:has-text("查看用量")').click(); await wait(500);
  ok((await page.locator('p[role=status]', { hasText: '用量' }).count()) >= 1, 'usage shown');
  const before = stub.speak.length;
  await page.getByRole('button', { name: '测试', exact: true }).click(); await wait(2500);
  ok(stub.speak.length === before + 1 && (await page.locator('p[role=status]', { hasText: '试听成功' }).count()) === 1, 'test button synthesizes and plays a sample');
  await page.keyboard.press('Escape');

  // ---- read with the cloud engine
  await page.locator('.book .t', { hasText: 'long' }).first().click();
  await page.waitForSelector('.content p', { timeout: 20000 });
  await wait(600);
  stub.speak.length = 0;
  await page.keyboard.press('r'); await wait(2500);
  const st = await page.evaluate(() => ({ state: window.__pyreader.reader.tts.state, spoken: window.__spoken.length }));
  ok(st.state === 'playing' && stub.speak.length >= 3, 'cloud reading requests sentences from the stub', `requests=${stub.speak.length}`);
  ok(st.spoken === 0, 'browser synth not used while cloud is on');
  ok(stub.speak.every((b) => b.text.length <= 200 && b.voice), 'each request <=200 chars with a voice');
  await page.keyboard.press('r'); await wait(300);

  // ---- quota -> automatic fallback to the browser voice
  stub.mode = 'quota';
  await page.keyboard.press('r'); await wait(1500);
  ok((await page.locator('.toast', { hasText: '额度已用完' }).count()) >= 1, 'quota toast shown');
  ok((await page.evaluate(() => window.__spoken.length)) >= 1, 'fell back to the browser voice');
  await page.screenshot({ path: SHOTS + '/cloudtts.png' });
} finally {
  await browser.close();
  server.close();
}
console.log(fails ? `${fails} FAILED` : 'ALL PASSED');
process.exit(fails ? 1 : 0);
