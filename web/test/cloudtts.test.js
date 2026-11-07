import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { here } from './helpers.js';
import {
  normalizeCloud, cloudReady, endpoint, classifyStatus, curateVoices, pickCloudVoice, formatUsage, cloudHint, PrefetchQueue, clampPlaybackRate, FALLBACK_VOICES,
} from '../js/core/cloudtts.js';

test('config: defaults off, needs enabled + consent + token', () => {
  const c = normalizeCloud(null);
  assert.equal(c.enabled, false);
  assert.equal(cloudReady(c), false);
  assert.equal(cloudReady(normalizeCloud({ enabled: true, consent: true, token: '  abc ' })), true);
  assert.equal(normalizeCloud({ token: ' abc ' }).token, 'abc');
  assert.equal(cloudReady(normalizeCloud({ enabled: true, token: 'x' })), false, 'no consent');
  assert.equal(cloudReady(normalizeCloud({ enabled: true, consent: true })), false, 'no token');
  assert.deepEqual(normalizeCloud({ voices: [{ id: 1 }, null, { id: 'v', lang: 'zh-CN' }] }).voices.map((v) => v.id), ['v']);
});

test('endpoint resolves next to the app directory, same origin', () => {
  assert.equal(endpoint('speak', 'https://x.example/reader/index.html#/read/1'), 'https://x.example/tts/v1/speak');
  assert.equal(endpoint('voices', 'https://x.example/reader/'), 'https://x.example/tts/v1/voices');
  assert.equal(endpoint('usage', 'http://127.0.0.1:8123/index.html'), 'http://127.0.0.1:8123/tts/v1/usage');
});

test('classifyStatus maps the contract statuses', () => {
  assert.equal(classifyStatus(401).kind, 'auth');
  assert.equal(classifyStatus(413).kind, 'toolong');
  const q = classifyStatus(429, { error: 'quota', retryAfter: 3600 });
  assert.equal(q.kind, 'quota');
  assert.equal(q.retryAfter, 3600);
  assert.match(q.message, /60 分钟/);
  assert.equal(classifyStatus(429, null).retryAfter, null);
  assert.equal(classifyStatus(502).kind, 'upstream');
  assert.equal(classifyStatus(418).kind, 'http');
});

test('voices: curated zh first then en; pick follows text language and preference', () => {
  const all = [
    { id: 'en-US-AriaNeural', lang: 'en-US' }, { id: 'ja-JP-NanamiNeural', lang: 'ja-JP' },
    { id: 'zh-TW-HsiaoChenNeural', lang: 'zh-TW' }, { id: 'zh-CN-YunxiNeural', lang: 'zh-CN' }, { id: 'fr-FR-X', lang: 'fr-FR' }, { id: 'bad' },
  ];
  assert.deepEqual(curateVoices(all).map((v) => v.id), ['zh-CN-YunxiNeural', 'zh-TW-HsiaoChenNeural', 'en-US-AriaNeural']);
  assert.deepEqual(curateVoices(null), []);
  const vs = curateVoices(all);
  assert.equal(pickCloudVoice(vs, '你好', ''), 'zh-CN-YunxiNeural');
  assert.equal(pickCloudVoice(vs, 'hello', 'zh-CN-YunxiNeural'), 'en-US-AriaNeural', 'preferred voice of the other language is not forced');
  assert.equal(pickCloudVoice(vs, '你好', 'zh-TW-HsiaoChenNeural'), 'zh-TW-HsiaoChenNeural');
  assert.equal(pickCloudVoice([], '你好'), FALLBACK_VOICES[0].id);
});

test('usage formatting and rate clamp and hint rules', () => {
  assert.equal(formatUsage({ usedToday: 10, limitToday: 100, usedMonth: 5, limitMonth: 500, backend: 'azure' }), '今日 10 / 100 字；本月 5 / 500 字（azure）');
  assert.equal(formatUsage(null), '');
  assert.equal(clampPlaybackRate(9), 2.5);
  assert.equal(clampPlaybackRate('x'), 1);
  assert.match(cloudHint({ kind: 'novoices', cloudOn: false, alreadyShown: false }), /在线朗读/);
  assert.equal(cloudHint({ kind: 'novoices', cloudOn: true, alreadyShown: false }), null);
  assert.equal(cloudHint({ kind: 'nostart', cloudOn: false, alreadyShown: true }), null);
});

test('PrefetchQueue: starts the window once, aborts what fell out, retries failures', async () => {
  const calls = [];
  let fail = false;
  const q = new PrefetchQueue((text, signal) => new Promise((res, rej) => {
    calls.push({ text, signal });
    if (fail) { rej(new Error('boom')); return; }
    signal.addEventListener('abort', () => rej(Object.assign(new Error('aborted'), { name: 'AbortError' })));
    setTimeout(() => res(text), 5);
  }));
  q.want([{ key: 'a', text: 'a' }, { key: 'b', text: 'b' }, { key: 'c', text: 'c' }]);
  q.want([{ key: 'a', text: 'a' }, { key: 'b', text: 'b' }, { key: 'c', text: 'c' }]);
  assert.equal(calls.length, 3, 'no duplicate fetches');
  assert.equal(await q.get('a', 'a'), 'a');
  q.want([{ key: 'b', text: 'b' }, { key: 'c', text: 'c' }, { key: 'd', text: 'd' }]);
  assert.equal(calls.length, 4);
  assert.equal(calls[0].signal.aborted, true, 'a left the window (abort after completion is harmless)');
  q.clear();
  assert.equal(q.size, 0);
  assert.ok(calls.slice(1).every((c) => c.signal.aborted), 'clear aborts in-flight fetches');
  fail = true;
  await assert.rejects(q.get('z', 'z'));
  assert.equal(q.size, 0, 'a failed entry is dropped so it can be retried');
  fail = false;
  assert.equal(await q.get('z', 'z'), 'z');
});

test('sw.js: bumped to v11, never handles /tts/ requests, only its own dir', () => {
  const sw = readFileSync(join(here, '..', 'sw.js'), 'utf8');
  assert.match(sw, /const CACHE = 'pyreader-web-v11'/);
  assert.match(sw, /pathname\.startsWith\(SCOPE_PATH\)\) return;/);
  assert.match(sw, /pathname\.includes\('\/tts\/'\)\) return;/);
  assert.match(sw, /req\.method !== 'GET'\) return;/);
  // the guard sits before respondWith, so a /tts/ response can never reach cache.put
  assert.ok(sw.indexOf("includes('/tts/')") < sw.indexOf('e.respondWith'));
});
