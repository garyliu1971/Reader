import { test } from 'node:test';
import assert from 'node:assert/strict';
import { normalizeCloud } from '../js/core/cloudtts.js';
import { engineStatus, statusText, cloudOptions, selectedValue, applyChoice, tokenIncomplete, cloudActive, CLOUD_PREFIX, HINT_INCOMPLETE, HINT_NO_ONLINE } from '../js/core/ttsux.js';

const ready = (x = {}) => normalizeCloud({ enabled: true, consent: true, token: 't', ...x });

test('engine defaults to cloud, only "browser" is kept', () => {
  assert.equal(normalizeCloud(null).engine, 'cloud');
  assert.equal(normalizeCloud({ engine: 'browser' }).engine, 'browser');
  assert.equal(normalizeCloud({ engine: 'x' }).engine, 'cloud');
});

test('status line: browser / cloud with voice name / incomplete token', () => {
  assert.equal(statusText(normalizeCloud(null)), '当前使用：浏览器语音');
  assert.equal(engineStatus(ready({ voice: 'zh-CN-YunxiNeural' })).text, '当前使用：在线语音（云希）');
  assert.equal(engineStatus(ready({ voice: '' })).text, '当前使用：在线语音（自动）');
  assert.equal(engineStatus(ready({ voices: [{ id: 'v1', name: '自定', lang: 'zh-CN' }], voice: 'v1' })).text, '当前使用：在线语音（自定）');
  assert.equal(engineStatus(ready({ voice: 'unknown-id' })).text, '当前使用：在线语音（unknown-id）');
  assert.equal(engineStatus(ready({ engine: 'browser' })).kind, 'browser');
  const inc = normalizeCloud({ token: 'abc' });
  assert.ok(tokenIncomplete(inc));
  assert.equal(statusText(inc), '当前使用：浏览器语音。' + HINT_INCOMPLETE);
  assert.equal(tokenIncomplete(normalizeCloud({ enabled: true, token: 'abc' })), true, 'consent missing');
  assert.equal(tokenIncomplete(ready()), false);
  assert.equal(tokenIncomplete(normalizeCloud({ enabled: true, consent: true })), false, 'no token: nothing to warn about');
});

test('online group only when ready; falls back to FALLBACK_VOICES', () => {
  assert.deepEqual(cloudOptions(normalizeCloud({ token: 'a' })), []);
  const o = cloudOptions(ready());
  assert.equal(o[0].value, CLOUD_PREFIX);
  assert.ok(o.length > 2 && o.every((x) => x.value.startsWith(CLOUD_PREFIX)));
  const custom = cloudOptions(ready({ voices: [{ id: 'a', name: 'A', lang: 'en-US', gender: 'Male' }] }));
  assert.deepEqual(custom.map((x) => x.value), ['cloud:', 'cloud:a']);
});

test('selected value follows the active engine', () => {
  assert.equal(selectedValue(ready({ voice: 'a' }), 'BV'), 'cloud:a');
  assert.equal(selectedValue(ready(), 'BV'), 'cloud:');
  assert.equal(selectedValue(ready({ engine: 'browser', voice: 'a' }), 'BV'), 'BV');
  assert.equal(selectedValue(normalizeCloud(null), ''), '');
  assert.ok(!cloudActive(ready({ engine: 'browser' })) && cloudActive(ready()));
});

test('choices: online voice sets cloud voice; browser voice switches engine; not ready -> hint', () => {
  assert.deepEqual(applyChoice('cloud:zh-CN-YunxiNeural', ready()), { cloudPatch: { voice: 'zh-CN-YunxiNeural', engine: 'cloud' }, ttsVoice: undefined, hint: '' });
  assert.equal(applyChoice('cloud:', ready({ engine: 'browser', voice: 'x' })).cloudPatch.voice, '');
  const b = applyChoice('BV', ready());
  assert.deepEqual(b, { cloudPatch: { engine: 'browser' }, ttsVoice: 'BV', hint: '' });
    assert.equal(applyChoice('', ready({ engine: 'browser' })).cloudPatch, null);
  const n1 = applyChoice('cloud:a', normalizeCloud({ token: 'x' }));
  assert.equal(n1.cloudPatch, null); assert.equal(n1.hint, HINT_INCOMPLETE);
  assert.equal(applyChoice('cloud:a', normalizeCloud(null)).hint, HINT_NO_ONLINE);
});
