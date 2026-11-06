import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseMultistatus, DavError, hostIsPrivate, checkServerUrl, acceptableName, unescapeXml } from '../js/sync/propfind.js';

const ms = (responses, ns = 'd') => `<?xml version="1.0"?><${ns}:multistatus xmlns:${ns}="DAV:">${responses}</${ns}:multistatus>`;
const dir = (href) => `<d:response><d:href>${href}</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>`;
const file = (href, size = 5, extra = '') => `<d:response><d:href>${href}</d:href><d:propstat><d:prop><d:resourcetype/><d:getcontentlength>${size}</d:getcontentlength><d:getlastmodified>Wed, 15 Nov 2023 08:12:31 GMT</d:getlastmodified><d:getetag>"abc"</d:getetag>${extra}</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>`;

test('direct children only, self skipped, percent-decoding, sizes/etag/mtime', () => {
  const xml = ms(dir('/dav/library/') + file('/dav/library/%E4%B8%89%E4%BD%93.epub', 123) + dir('/dav/library/sub/') + file('/dav/library/a%20b.txt', 7));
  const r = parseMultistatus(xml, '/dav/library');
  assert.deepEqual(r.map((e) => e.name), ['三体.epub', 'sub', 'a b.txt']);
  assert.equal(r[0].size, 123);
  assert.equal(r[0].isDir, false);
  assert.equal(r[1].isDir, true);
  assert.equal(r[0].etag, '"abc"');
  assert.equal(r[0].mtime, Date.parse('Wed, 15 Nov 2023 08:12:31 GMT') / 1000);
});

test('absolute hrefs, other prefixes and default-namespace XML are handled', () => {
  const xml = `<multistatus xmlns="DAV:"><response><href>http://host:8080/dav/library/x.txt</href><propstat><prop><getcontentlength>9</getcontentlength></prop><status>HTTP/1.1 200 OK</status></propstat></response></multistatus>`;
  const r = parseMultistatus(xml, '/dav/library');
  assert.deepEqual(r.map((e) => [e.name, e.size]), [['x.txt', 9]]);
  const x2 = ms(file('/dav/library/y.txt'), 'D').replace(/<d:/g, '<D:').replace(/<\/d:/g, '</D:');
  assert.equal(parseMultistatus(x2, '/dav/library')[0].name, 'y.txt');
});

test('traversal, other directories, nested paths and junk names are dropped', () => {
  const xml = ms(
    file('/dav/library/../secret.txt') + file('/dav/other/a.txt') + file('/dav/library/sub/deep.txt') +
    file('/dav/library/./x.txt') + file('/dav/library/') + file('/dav/library/%2e%2e') + file('/dav/library/ok.txt')
    + file('/dav/library/%ZZbad.txt') + file('http://evil.example/dav/library/evil.txt'),
  );
  const names = parseMultistatus(xml, '/dav/library').map((e) => e.name);
  assert.deepEqual(names, ['ok.txt', 'evil.txt']);   // evil.example path is the same path: origin is the fetch layer's job
});

test('non-200 propstat is ignored; size must be digits', () => {
  const xml = ms(`<d:response><d:href>/dav/library/a.txt</d:href><d:propstat><d:prop><d:getcontentlength>99</d:getcontentlength></d:prop><d:status>HTTP/1.1 404 Not Found</d:status></d:propstat></d:response>`
    + file('/dav/library/b.txt', '12abc'));
  const r = parseMultistatus(xml, '/dav/library');
  assert.equal(r[0].size, 0);
  assert.equal(r[1].size, 0);
});

test('DTD / entity declarations are refused (billion laughs, XXE)', () => {
  const lol = `<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>` + ms(file('/dav/library/a.txt'));
  assert.throws(() => parseMultistatus(lol, '/dav/library'), DavError);
  const xxe = `<!doctype x [<!ENTITY e SYSTEM "file:///etc/passwd">]>` + ms(file('/dav/library/&e;.txt'));
  assert.throws(() => parseMultistatus(xxe, '/dav/library'), /unsafe/);
  assert.throws(() => parseMultistatus('<!ENTITY a "b">' + ms(''), '/dav/library'), /unsafe/);
  // predefined and numeric references still decode, nothing else expands
  assert.equal(unescapeXml('a&amp;b&lt;&#x4e2d;&#20013;&bogus;&#0;&#xD800;'), 'a&b<中中&bogus;');
  const r = parseMultistatus(ms(file('/dav/library/a&amp;b.txt')), '/dav/library');
  assert.equal(r[0].name, 'a&b.txt');
});

test('oversized, non-text and non-multistatus input is rejected; comments cannot smuggle entries', () => {
  assert.throws(() => parseMultistatus('x'.repeat(100), '/a', { maxBytes: 50 }), /too large/);
  assert.throws(() => parseMultistatus(null, '/a'), DavError);
  assert.throws(() => parseMultistatus('<html>nope</html>', '/dav'), /bad multistatus/);
  const hidden = ms('<!--' + file('/dav/library/ghost.txt') + '-->' + file('/dav/library/real.txt'));
  assert.deepEqual(parseMultistatus(hidden, '/dav/library').map((e) => e.name), ['real.txt']);
  const many = ms(Array.from({ length: 20001 }, (_, i) => file(`/dav/library/f${i}.txt`)).join(''));
  assert.throws(() => parseMultistatus(many, '/dav/library', { maxBytes: 1e9 }), /too many/);
  // pathological input must stay fast (no catastrophic backtracking)
  const t0 = performance.now();
  parseMultistatus(ms('<d:response>'.repeat(5000) + file('/dav/library/a.txt')), '/dav/library');
  assert.ok(performance.now() - t0 < 1500, `took ${performance.now() - t0}ms`);
});

test('acceptableName filters hidden, lock files, .part and unsafe names', () => {
  for (const n of ['.hidden', '~$x.docx', 'a.epub.part', '..', 'a:b', 'con.txt', 'x\u202e.txt']) assert.equal(acceptableName(n), false, n);
  for (const n of ['a.epub', '三体.epub', 'x y.txt']) assert.equal(acceptableName(n), true, n);
});

test('http:// only for private/local hosts (credentials stay off the public internet)', () => {
  for (const h of ['localhost', '127.0.0.1', '192.168.1.5', '10.0.0.1', '172.16.3.4', '100.64.1.1', 'nas', 'box.local', '[::1]', '169.254.1.1']) {
    assert.equal(hostIsPrivate(h), true, h);
  }
  for (const h of ['example.com', '8.8.8.8', '172.32.0.1', '100.128.0.1', '']) assert.equal(hostIsPrivate(h), false, h);
  assert.equal(checkServerUrl('https://dav.example.com/dav/').url, 'https://dav.example.com/dav');
  assert.equal(checkServerUrl('http://dav.example.com/dav').ok, false);
  assert.equal(checkServerUrl('http://192.168.1.2:8080/dav').ok, true);
  assert.equal(checkServerUrl('ftp://x/y').ok, false);
  assert.equal(checkServerUrl('https://user:pw@x.com/').ok, false);
  assert.equal(checkServerUrl('not a url').ok, false);
});
