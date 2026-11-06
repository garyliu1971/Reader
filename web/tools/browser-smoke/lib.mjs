// Shared helpers for the optional real-browser smoke tests (not part of `node --test`).
// Setup (once, in any scratch directory):  npm i playwright-core axe-core
// Then:  python web/tools/make_test_books.py $SMOKE_TMP/books ; python -m http.server 8123 --directory web
//        node web/tools/browser-smoke/reader.mjs      (set CHROME_PATH if Chrome/Edge is not in the default location)
import { chromium } from 'playwright-core';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const SP = (process.env.SMOKE_TMP || path.join(os.tmpdir(), 'pyreader-web-smoke')).replaceAll('\\', '/');
export const BOOKS = SP + '/books';
export const SHOTS = SP + '/shots';
export const URL0 = process.env.APP_URL || 'http://127.0.0.1:8123/index.html';
export const DEVDAV = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'devdav.py');
const CHROME = process.env.CHROME_PATH || (process.platform === 'win32' ? 'C:/Program Files/Google/Chrome/Application/chrome.exe' : undefined);

export async function launch({ width = 900, height = 700, touch = false, scheme = 'light', ctx = {} } = {}) {
  const browser = await chromium.launch({ ...(CHROME ? { executablePath: CHROME } : { channel: 'chrome' }), headless: true, args: ['--no-sandbox'] });
  const context = await browser.newContext({ viewport: { width, height }, hasTouch: touch, isMobile: false, colorScheme: scheme, ...ctx });
  const page = await context.newPage();
  const logs = [];
  page.on('console', (m) => { if (['error', 'warning'].includes(m.type())) logs.push(`[${m.type()}] ${m.text()}`); });
  page.on('pageerror', (e) => logs.push('[pageerror] ' + e.message));
  return { browser, context, page, logs };
}

export const wait = (ms) => new Promise((r) => setTimeout(r, ms));
