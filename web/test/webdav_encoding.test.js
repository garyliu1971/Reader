// Content-Length counts compressed bytes when the server gzips (Caddy/SFTPGo do); fetch hands us the decoded stream.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DavClient } from '../js/sync/webdav.js';

const body = new TextEncoder().encode('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"></d:multistatus>');
const resp = (headers) => () => Promise.resolve(new Response(body, { status: 207, headers }));
const client = (headers) => new DavClient({ baseUrl: 'https://dav.example', fetchImpl: resp(headers) });

test('encoded response: Content-Length of the compressed body is not a truncation signal', async () => {
  const r = await client({ 'content-encoding': 'gzip', 'content-length': '12' }).request('PROPFIND', '', { okCodes: [207] });
  assert.equal(r.body.length, body.length);
});

test('identity response still detects a short body', async () => {
  await assert.rejects(client({ 'content-length': String(body.length + 50) }).request('PROPFIND', '', { okCodes: [207] }), /truncated response/);
});

test('encoded response still honours maxBytes on the decoded size', async () => {
  await assert.rejects(client({ 'content-encoding': 'gzip', 'content-length': '12' }).request('PROPFIND', '', { okCodes: [207], maxBytes: 10 }), /too large/);
});
