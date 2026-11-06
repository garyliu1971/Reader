// Static-shell consistency: service worker precache list, manifest icons and index.html references must exist.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, existsSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { here } from './helpers.js';

const web = join(here, '..');
const walk = (d) => readdirSync(d).flatMap((n) => { const p = join(d, n); return statSync(p).isDirectory() ? walk(p) : [p]; });

test('every app module under js/ is in the service worker precache list (offline works)', () => {
  const sw = readFileSync(join(web, 'sw.js'), 'utf8');
  const listed = new Set([...sw.matchAll(/'((?:js|css|icons|vendor)\/[^']+|index\.html|manifest\.webmanifest)'/g)].map((m) => m[1]));
  for (const f of walk(join(web, 'js')).map((p) => relative(web, p).replaceAll('\\', '/'))) assert.ok(listed.has(f), `${f} missing from sw.js SHELL`);
  for (const f of listed) assert.ok(existsSync(join(web, f)), `${f} listed in sw.js but missing on disk`);
});

test('manifest icons exist and index.html assets resolve', () => {
  const m = JSON.parse(readFileSync(join(web, 'manifest.webmanifest'), 'utf8'));
  assert.ok(m.name && m.start_url && m.display === 'standalone');
  for (const i of m.icons) assert.ok(existsSync(join(web, i.src)), i.src);
  assert.ok(m.icons.some((i) => i.purpose === 'maskable') && m.icons.some((i) => i.sizes === '512x512'));
  const html = readFileSync(join(web, 'index.html'), 'utf8');
  for (const [, ref] of html.matchAll(/(?:src|href)="((?!#|https?:)[^"]+)"/g)) assert.ok(existsSync(join(web, ref)), ref);
  assert.match(html, /viewport-fit=cover/);
});

test('vendored libraries have recorded versions and licenses', () => {
  const readme = readFileSync(join(web, 'vendor', 'README.md'), 'utf8');
  for (const f of ['jszip.min.js', 'marked.esm.js', 'pdf.min.js', 'pdf.worker.min.js', 'LICENSE.jszip', 'LICENSE.marked', 'LICENSE.pdfjs']) {
    assert.ok(existsSync(join(web, 'vendor', f)), f);
    assert.ok(readme.includes(f), `${f} not documented in vendor/README.md`);
  }
});
