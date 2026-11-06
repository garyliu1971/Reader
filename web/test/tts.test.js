import { test } from 'node:test';
import assert from 'node:assert/strict';
import { splitSentences, pickVoice, TtsReader } from '../js/reader/tts.js';

const texts = (t, br) => splitSentences(t, br).map((x) => t.slice(x.a, x.b));

test('splits on CJK and Latin terminators, keeps closing quotes with their sentence', () => {
  assert.deepEqual(texts('他说：“你好。”然后离开了！真的吗？是的……好。'), ['他说：“你好。”', '然后离开了！', '真的吗？', '是的……', '好。']);
  assert.deepEqual(texts('Hello world. It is 3.14 today! Ok?'), ['Hello world.', 'It is 3.14 today!', 'Ok?']);
});

test('offsets are absolute and newline/blank handling drops empty pieces', () => {
  const t = '  第一句。\n\n 第二句\n——\n';
  const s = splitSentences(t);
  assert.deepEqual(s.map((x) => t.slice(x.a, x.b)), ['第一句。', '第二句']);
  assert.equal(s[0].a, 2);
});

test('a block boundary ends a sentence even without punctuation (headings)', () => {
  const t = '第一章 开始这是正文。';
  assert.deepEqual(texts(t, new Set([6])), ['第一章 开始', '这是正文。']);
  assert.deepEqual(texts(t), ['第一章 开始这是正文。']);
});

test('very long runs are cut at soft breaks and stay short', () => {
  const t = '甲'.repeat(100) + '，' + '乙'.repeat(150) + '。';
  const parts = texts(t);
  assert.ok(parts.length >= 2 && parts.every((p) => p.length <= 160), parts.map((p) => p.length).join(','));
  assert.equal(parts.join('').replace(/，/g, ''), t.replace(/，/g, ''));
});

test('pickVoice prefers the chosen voice, then natural/online voices of the right language', () => {
  const vs = [
    { voiceURI: 'a', name: 'Microsoft Huihui', lang: 'zh-CN', localService: true },
    { voiceURI: 'b', name: 'Microsoft Xiaoxiao Online (Natural)', lang: 'zh-CN', localService: false },
    { voiceURI: 'c', name: 'Samantha', lang: 'en-US', localService: true },
  ];
  assert.equal(pickVoice(vs, 'zh-CN').voiceURI, 'b');
  assert.equal(pickVoice(vs, 'zh-CN', 'a').voiceURI, 'a');
  assert.equal(pickVoice(vs, 'zh-CN', 'c').voiceURI, 'b', 'a voice of another language is never forced');
  assert.equal(pickVoice(vs, 'en-US').voiceURI, 'c');
  assert.equal(pickVoice(vs, 'ja-JP'), null);
  assert.equal(pickVoice([], 'zh-CN'), null);
});

// ---- engine with a fake synthesiser
function fakeSynth({ autoEnd = true, failWith = null } = {}) {
  const s = {
    spoken: [], cancels: 0, paused: false, queue: null,
    getVoices: () => [],
    speak(u) {
      s.spoken.push({ text: u.text, rate: u.rate, lang: u.lang });
      s.queue = u;
      if (failWith) setTimeout(() => u.onerror({ error: failWith }), 0);
      else if (autoEnd) setTimeout(() => { if (!s.paused && s.queue === u) u.onend(); }, 0);
    },
    cancel() { s.cancels++; const u = s.queue; s.queue = null; if (u && u.onerror) setTimeout(() => u.onerror({ error: 'canceled' }), 0); },
    pause() { s.paused = true; },
    resume() { s.paused = false; const u = s.queue; if (u) setTimeout(() => u.onend(), 0); },
  };
  return s;
}
class Utter { constructor(text) { this.text = text; } }
const tick = (ms = 20) => new Promise((r) => setTimeout(r, ms));

function mkReader(sections, synthOpts) {
  let sec = 0;
  const log = { reveal: [], states: [], finished: 0, failed: [], sections: [] };
  const synth = fakeSynth(synthOpts);
  const host = {
    text: () => sections[sec], breaks: () => null,
    reveal: (a, b) => log.reveal.push([sec, a, b]),
    nextSection: async () => { if (sec + 1 >= sections.length) return false; sec++; log.sections.push(sec); return true; },
    changed: (s) => log.states.push(s), finished: () => { log.finished++; }, failed: (m) => log.failed.push(m),
    settings: () => ({ ttsRate: 1.5, ttsVoice: '' }),
  };
  return { r: new TtsReader({ synth, Utter, host }), synth, log };
}

test('reads sentences in order, reveals each, continues into next sections, then finishes', async () => {
  const { r, synth, log } = mkReader(['甲。乙。', '丙。']);
  r.start(0);
  await tick(60);
  assert.deepEqual(synth.spoken.map((x) => x.text), ['甲。', '乙。', '丙。']);
  assert.deepEqual(log.reveal, [[0, 0, 2], [0, 2, 4], [1, 0, 2]]);
  assert.equal(log.finished, 1);
  assert.equal(r.state, 'idle');
  assert.equal(synth.spoken[0].rate, 1.5);
  assert.equal(synth.spoken[0].lang, 'zh-CN');
});

test('starts from the sentence containing the offset', async () => {
  const { r, synth } = mkReader(['甲。乙。丙。']);
  r.start(3);   // inside the second sentence
  await tick(40);
  assert.deepEqual(synth.spoken.map((x) => x.text), ['乙。', '丙。']);
});

test('stop cancels and nothing more is spoken; restart jumps', async () => {
  const { r, synth } = mkReader(['甲。乙。丙。丁。'], { autoEnd: false });
  r.start(0);
  await tick();
  assert.equal(synth.spoken.length, 1);
  r.stop();
  await tick();
  assert.equal(r.state, 'idle');
  assert.equal(synth.spoken.length, 1, 'no further sentence after stop');
  r.start(6);
  await tick();
  assert.equal(synth.spoken.at(-1).text, '丁。');
});

test('pause holds the queue, resume continues', async () => {
  const { r, synth } = mkReader(['甲。乙。']);
  r.start(0);
  r.pause();
  await tick(30);
  assert.equal(r.state, 'paused');
  assert.equal(synth.spoken.length, 1, 'held on the first sentence');
  r.resume();
  await tick(40);
  assert.deepEqual(synth.spoken.map((x) => x.text), ['甲。', '乙。']);
});

test('three consecutive synthesis errors stop with a message instead of racing through the book', async () => {
  const { r, synth, log } = mkReader(['甲。乙。丙。丁。戊。'], { failWith: 'synthesis-failed' });
  r.start(0);
  await tick(60);
  assert.equal(r.state, 'idle');
  assert.equal(log.failed.length, 1);
  assert.match(log.failed[0], /朗读失败/);
  assert.equal(synth.spoken.length, 3);
});

test('restart while idle does nothing', () => {
  const { r, synth } = mkReader(['甲。']);
  r.restart(0);
  assert.equal(synth.spoken.length, 0);
});

test('engine that accepts speak() but never starts: stops with a clear message', async () => {
  const { r, log } = mkReader(['甲。乙。丙。'], { autoEnd: false });
  r.startTimeoutMs = 30;
  r.start(0);
  await tick(120);
  assert.equal(r.state, 'idle');
  assert.equal(log.failed.length, 1);
  assert.match(log.failed[0], /没有声音输出/);
});
