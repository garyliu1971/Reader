// Sync interop against tests/fakedav.py (the Qt tests' fake WebDAV server) via web/tools/devdav.py (adds CORS).
// Python is also used as an oracle: files written by the JS engine must be accepted by pyreader's own validators.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { join } from 'node:path';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { SyncEngine, validateConfig } from '../js/sync/sync.js';
import { DavClient } from '../js/sync/webdav.js';
import { progressPath } from '../js/core/bookid.js';
import { golden, repoRoot, here } from './helpers.js';

const py = spawnSync('python', ['--version']);
const HAVE_PY = py.status === 0;
const opts = { skip: HAVE_PY ? false : 'python not available' };

let proc, base;
const AUTH = 'gary:pässwörd';   // non-ASCII password: Basic auth must be UTF-8
const memKv = () => { const m = new Map(); return { get: async (k, d) => (m.has(k) ? structuredClone(m.get(k)) : d), set: async (k, v) => { m.set(k, structuredClone(v)); } }; };
const raw = () => new DavClient({ baseUrl: base, user: 'gary', password: 'pässwörd' });
const mk = async (extra = {}) => {
  const v = validateConfig({ url: base, user: 'gary', password: 'pässwörd', syncUser: 'gary', deviceId: 'web-abc123', deviceName: 'Test Web', ...extra });
  assert.ok(v.ok, v.errors.join());
  return new SyncEngine(v.cfg, { kv: memKv() }).init();
};
const text = (u8) => new TextDecoder().decode(u8);
function pyEval(code, stdin) {
  const r = spawnSync('python', ['-I', '-c', code], { input: stdin, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONIOENCODING: 'utf-8' }, cwd: repoRoot, encoding: 'utf8' });
  assert.equal(r.status, 0, r.stderr);
  return r.stdout.trim();
}

before(async () => {
  if (!HAVE_PY) return;
  const seed = mkdtempSync(join(tmpdir(), 'pyr-seed-'));
  writeFileSync(join(seed, 'hello.txt'), '第一章 你好\n\n正文。\n');
  writeFileSync(join(seed, 'two.md'), '# T\n');
  writeFileSync(join(seed, 'skip.exe'), 'x');
  writeFileSync(join(seed, '.hidden.epub'), 'x');
  writeFileSync(join(seed, 'x.epub.part'), 'x');
  proc = spawn('python', ['-I', join(here, '..', 'tools', 'devdav.py'), '--auth', AUTH, '--seed', seed], { stdio: ['ignore', 'pipe', 'inherit'], env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } });
  base = await new Promise((resolve, reject) => {
    let buf = '';
    proc.stdout.on('data', (d) => { buf += d; const m = /READY (\S+)/.exec(buf); if (m) resolve(m[1]); });
    proc.on('exit', (c) => reject(new Error('devdav exited ' + c)));
    const t = setTimeout(() => reject(new Error('devdav start timeout')), 15000); t.unref();
  });
});
after(() => { if (proc) proc.kill(); });

test('CORS preflight succeeds without credentials and echoes the origin (never *)', opts, async () => {
  const r = await fetch(base + '/library', { method: 'OPTIONS', headers: { Origin: 'https://app.example', 'Access-Control-Request-Method': 'PROPFIND', 'Access-Control-Request-Headers': 'authorization,depth,content-type' } });
  assert.equal(r.status, 204);
  assert.equal(r.headers.get('access-control-allow-origin'), 'https://app.example');
  for (const m of ['OPTIONS', 'GET', 'PUT', 'PROPFIND', 'MKCOL', 'MOVE']) assert.ok(r.headers.get('access-control-allow-methods').includes(m), m);
  for (const h of ['Authorization', 'Depth', 'Content-Type', 'Destination', 'Overwrite', 'If-None-Match']) assert.ok(r.headers.get('access-control-allow-headers').includes(h), h);
});

test('connection test: wrong password is a clear 401, right one works', opts, async () => {
  const bad = new SyncEngine(validateConfig({ url: base, user: 'gary', password: 'nope', syncUser: 'gary', deviceId: 'web-aaaaaa' }).cfg, { kv: memKv() });
  await assert.rejects(() => bad.test(), (e) => e.status === 401);
  const e = await mk();
  const r = await e.test();
  assert.equal(r.ok, true);
  assert.equal(r.libraryExists, true);
});

test('plain http to a public host with credentials is refused; invalid names are rejected by validateConfig', opts, () => {
  assert.throws(() => new DavClient({ baseUrl: 'http://dav.example.com/dav', user: 'a', password: 'b' }), /plain http/);
  const v = validateConfig({ url: 'http://dav.example.com/dav', syncUser: '../x', deviceId: 'a b' });
  assert.equal(v.ok, false);
  assert.equal(v.errors.length, 3);
  assert.match(validateConfig({ url: base }).cfg.deviceId, /^web-[0-9a-f]{6}$/);
  assert.equal(validateConfig({ url: base }).cfg.syncUser, 'default');
});

test('library listing: filters hidden/.part/unsupported, marks fresh files as unstable', opts, async () => {
  const e = await mk();
  const l = await e.listLibrary();
  assert.deepEqual(l.files.map((f) => f.rel), ['hello.txt', 'two.md']);
  assert.equal(l.unstable.size, 0);
  assert.equal(l.rootMissing, false);
  assert.ok(l.skipped >= 3);
  // size changed since the previous round => still being uploaded
  const l2 = await e.listLibrary({ prevSizes: { 'hello.txt': 1 } });
  assert.deepEqual([...l2.unstable], ['hello.txt']);
  const bytes = await e.downloadBook('hello.txt', l.files[0].size);
  assert.equal(text(bytes), '第一章 你好\n\n正文。\n');
  await assert.rejects(() => e.downloadBook('hello.txt', l.files[0].size + 1), /size mismatch/);
  await assert.rejects(() => e.downloadBook('../x.txt', 0), /illegal/);
  await assert.rejects(() => e.downloadBook('.hidden.epub', 0), /illegal/);
});

test('upload: .part + MOVE with Overwrite: F, never overwrites an existing book', opts, async () => {
  const e = await mk();
  const data = new TextEncoder().encode('new book content');
  const r = await e.uploadBook({ name: 'up 新书.txt', folder: '', data, size: data.length });
  assert.equal(r.status, 'ok', r.message);
  assert.equal(r.method, 'move');
  assert.equal(text((await raw().get('library/up 新书.txt')).body), 'new book content');
  // same name again (also differently normalized/cased): refused, bytes untouched
  const other = new TextEncoder().encode('OVERWRITE ATTEMPT');
  const r2 = await e.uploadBook({ name: 'UP 新书.TXT', folder: '', data: other, size: other.length });
  assert.equal(r2.status, 'exists');
  const r3 = await e.uploadBook({ name: 'hello.txt', folder: '', data: other, size: other.length });
  assert.equal(r3.status, 'exists');
  assert.equal(text((await raw().get('library/hello.txt')).body), '第一章 你好\n\n正文。\n');
  assert.equal((await e.uploadBook({ name: 'bad.exe', data: other, size: 3 })).status, 'error');
  assert.equal((await e.uploadBook({ name: '.dot.txt', data: other, size: 3 })).status, 'error');
  assert.equal((await e.uploadBook({ name: 'a.txt', folder: '../x', data: other, size: 3 })).status, 'error');
  const sub = await e.uploadBook({ name: 'in.md', folder: '子 目录', data: other, size: other.length });
  assert.equal(sub.status, 'ok', sub.message);
  // what the server saw: MOVE carried Overwrite: F, the part PUT was conditional, no DELETE was ever sent
  const log = await (await fetch(new URL(base).origin + '/__debug/log', { headers: { Authorization: 'Basic ' + Buffer.from(AUTH).toString('base64') } })).json();
  const mv = log.headers.find(([m, p]) => m === 'MOVE' && p.endsWith('.up 新书.txt.part'));
  assert.ok(mv, 'MOVE seen');
  assert.equal(mv[2].Overwrite, 'F');
  assert.equal(mv[2]['If-None-Match'], '*');
  const putPart = log.headers.find(([m, p]) => m === 'PUT' && p.endsWith('.up 新书.txt.part'));
  assert.equal(putPart[2]['If-None-Match'], '*');
  assert.equal(log.methods.some(([m]) => m === 'DELETE'), false);
});

test('device file written by JS passes the Python validator; peers written by Python are read back', opts, async () => {
  // a "Qt" peer file produced by the real Python to_wire
  const peerWire = JSON.stringify(golden.wire);
  const peerDev = 'qt-pc';
  const c = raw();
  await c.mkdirs('users/gary/shelf');
  await c.put(`users/gary/shelf/${peerDev}.json`, peerWire);

  const e = await mk();
  const r = await e.pullPeers();
  assert.deepEqual(r.fetched, [peerDev]);
  assert.equal(e.digest()['remote:a.epub'].percent, 61.25);
  assert.equal(e.digest()['remote:a.epub'].device, 'My Phone');       // hostname label
  assert.equal(e.mergedView().entries['remote:a.epub'].fav[0], true);
  const again = await e.pullPeers();                                  // cursor: unchanged peer is not re-fetched (racy window aside)
  assert.equal(again.failed.length + again.corrupt.length, 0);

  // our own state: favourite + progress, then push
  e.setFlag('remote:hello.txt', 'fav', true);
  e.recordProgress('remote:hello.txt', 42.5, Date.now() / 1000);
  e.recordProgress('local:' + 'a'.repeat(40), 7, Date.now() / 1000 - 10);
  assert.equal(e.dirty(), true);
  assert.equal(await e.pushDevice(), 'pushed');
  assert.equal(e.dirty(), false);
  assert.equal(await e.pushDevice(), 'unchanged');
  const body = (await c.get('users/gary/shelf/web-abc123.json')).body;
  assert.ok(body.length < 1024 * 1024);

  const out = pyEval(`
import sys, json
sys.path.insert(0, '.')
from pyreader import shelf as sh
raw = json.loads(sys.stdin.buffer.read().decode('utf-8'))
clean, issues = sh.validate_overlay(raw, device_id='web-abc123')
print(json.dumps({'issues': issues, 'fav': sh.field_value(clean, 'remote:hello.txt', 'fav'), 'prog': clean['progress']['remote:hello.txt']['percent'], 'n': len(clean['progress']), 'host': clean.get('hostname'), 'keys': sorted(raw)}))
`, Buffer.from(body));
  const parsed = JSON.parse(out);
  assert.deepEqual(parsed.issues, []);
  assert.equal(parsed.fav, true);
  assert.equal(parsed.prog, 42.5);
  assert.equal(parsed.n, 2);
  assert.equal(parsed.host, 'Test Web');
  assert.deepEqual(parsed.keys, ['device_id', 'entries', 'hostname', 'progress', 'user', 'version']);

  // a second browser with the same device id (cloned) adopts what the server has instead of clobbering it
  const clone = await mk();
  const r2 = await clone.pullPeers({ force: true });
  assert.equal(r2.ownAdopted, true);
  assert.equal(clone.own.progress['remote:hello.txt'].percent, 42.5);
});

test('per-book progress: Python-compatible record, never regresses a newer remote one', opts, async () => {
  const e = await mk();
  const bid = 'remote:hello.txt';
  const r = await e.pushBookProgress({ bid, percent: 33.3, chapter: '第一章', offset: 0 });
  assert.equal(r.status, 'written');
  const fileBody = (await raw().get(progressPath('gary', bid))).body;
  const out = pyEval(`
import sys, json
sys.path.insert(0, '.')
from pyreader import sync as s
d = json.loads(sys.stdin.buffer.read().decode('utf-8'))
print(json.dumps({'path': s.progress_path('gary', 'remote:hello.txt'), 'ok': isinstance(d, dict) and 'offset' in d and d.get('book_id') == 'remote:hello.txt', 'keys': sorted(d), 'pct': d['percent'], 'upd': d['updated']}))
`, Buffer.from(fileBody));
  const p = JSON.parse(out);
  assert.equal(p.path, progressPath('gary', bid));
  assert.equal(p.ok, true);
  assert.deepEqual(p.keys, ['book_id', 'chapter', 'device', 'offset', 'percent', 'updated']);
  assert.equal(p.pct, 33.3);
  assert.ok(Math.abs(p.upd - Date.now() / 1000) < 30);

  // a desktop wrote a newer record in the meantime
  const newer = { book_id: bid, offset: 5000, percent: 80, chapter: 'x', updated: Date.now() / 1000 + 30, device: 'desktop' };
  await raw().put(progressPath('gary', bid), JSON.stringify(newer));
  const r2 = await e.pushBookProgress({ bid, percent: 10 });
  assert.equal(r2.status, 'remote-newer');
  assert.equal(r2.remote.percent, 80);
  assert.equal(JSON.parse(text((await raw().get(progressPath('gary', bid))).body)).percent, 80);
  // pull sees it; a corrupt/foreign record is ignored
  assert.equal((await e.pullBookProgress(bid)).percent, 80);
  await raw().put(progressPath('gary', bid), JSON.stringify({ ...newer, book_id: 'remote:other' }));
  assert.equal(await e.pullBookProgress(bid), null);
  assert.equal(await e.pullBookProgress('remote:never-existed.txt'), null);
});
