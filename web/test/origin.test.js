// Same-origin check behind the CORS wording (settings hint and network error message).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { isCrossOrigin, networkHint, corsNote } from '../js/core/origin.js';

const PAGE = 'https://gary.example.com';

test('same scheme+host+port is same-origin, whatever the path', () => {
  assert.equal(isCrossOrigin('https://gary.example.com/dav', PAGE), false);
  assert.equal(isCrossOrigin('https://gary.example.com:443/dav/', PAGE), false);
  assert.equal(isCrossOrigin('https://GARY.example.com/dav', PAGE), false);
  assert.equal(isCrossOrigin('/dav', PAGE), false);   // relative to the page
});

test('other host, port or scheme is cross-origin', () => {
  assert.equal(isCrossOrigin('https://dav.example.com/dav', PAGE), true);
  assert.equal(isCrossOrigin('https://gary.example.com:8443/dav', PAGE), true);
  assert.equal(isCrossOrigin('http://gary.example.com/dav', PAGE), true);
});

test('cannot decide (garbage URL, opaque or missing page origin) -> treat as cross-origin', () => {
  assert.equal(isCrossOrigin('http://[bad', PAGE), true);
  assert.equal(isCrossOrigin('https://gary.example.com/dav', 'null'), true);
  assert.equal(isCrossOrigin('https://gary.example.com/dav', undefined), true);
});

test('network error hint mentions CORS only when the server is on another origin', () => {
  assert.match(networkHint('https://dav.example.com/dav', PAGE), /CORS/);
  const same = networkHint('https://gary.example.com/dav', PAGE);
  assert.ok(!/CORS 配置/.test(same));
  assert.match(same, /同源/);
});

test('settings note: generic when empty, requirement when cross-origin, reassurance when same-origin', () => {
  assert.match(corsNote('', PAGE), /不在同一个网址/);
  assert.match(corsNote('https://dav.example.com/dav', PAGE), /不同源，必须允许跨域/);
  assert.match(corsNote('https://gary.example.com/dav', PAGE), /同源，无需配置 CORS/);
});
