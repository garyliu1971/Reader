// Desktop keys a book read from an ordinary folder as local:sha1(name+size); web keys library downloads remote:<rel>.
// Progress must flow both ways through the alias.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { aliasIds, localBookId, remoteBookId } from '../js/core/bookid.js';
import { SyncEngine, validateConfig } from '../js/sync/sync.js';

const meta = { id: remoteBookId('小说/庆余年.txt'), source: 'remote', name: '庆余年.txt', size: 12345 };

test('aliasIds: remote library book aliases to the Qt local id', () => {
  assert.deepEqual(aliasIds(meta), [localBookId('庆余年.txt', 12345)]);
  assert.deepEqual(aliasIds({ ...meta, source: 'local', id: localBookId('庆余年.txt', 12345) }), []);
  assert.deepEqual(aliasIds(null), []);
});

function fakeEngine() {
  const files = new Map();
  const eng = new SyncEngine(validateConfig({ url: 'http://x.test/dav', syncUser: 'gary', deviceId: 'web-abc123' }).cfg,
    { kv: { get: async (k, d) => d, set: async () => {} } });
  eng._client = {
    skewMs: 0, url: (p) => p, mkdirs: async () => {},
    get: async (p, o = {}) => { if (!files.has(p)) { if ((o.okCodes || []).includes(404)) return { status: 404, body: new Uint8Array() }; throw new Error('404'); } return { status: 200, body: new TextEncoder().encode(files.get(p)) }; },
    put: async (p, b) => { files.set(p, typeof b === 'string' ? b : new TextDecoder().decode(b)); return { status: 201 }; },
  };
  return { eng, files };
}

test('web pushes progress under both ids and pulls the newest across aliases', async () => {
  const { eng, files } = fakeEngine();
  const al = aliasIds(meta);
  const r = await eng.pushBookProgress({ bid: meta.id, percent: 40, updated: 1000, aliases: al });
  assert.equal(r.status, 'written');
  assert.equal(files.size, 2);
  // desktop (alias id only) wrote something newer
  await eng.pushBookProgress({ bid: al[0], percent: 77, updated: 2000 });
  const got = await eng.pullBookProgress(meta.id, al);
  assert.equal(got.percent, 77);
  assert.equal(got.updated, 2000);
  // without aliases the old behaviour (the bug) would miss it
  assert.equal((await eng.pullBookProgress(meta.id)).percent, 40);
});
