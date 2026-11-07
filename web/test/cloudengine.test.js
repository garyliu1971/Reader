import { test } from 'node:test';
import assert from 'node:assert/strict';
import { CloudEngine, CloudApi } from '../js/reader/cloudengine.js';
import { TtsReader } from '../js/reader/tts.js';

// ---- stub server (a fetch stand-in implementing the /tts/v1 contract) and fake <audio>
function stubServer({ status = 200, body = null, token = 'tok', delay = 0 } = {}) {
  const s = { calls: [], status, body, token, delay, aborted: 0 };
  s.fetch = (url, init) => new Promise((resolve, reject) => {
    const req = { url, method: init.method, auth: init.headers.Authorization, body: init.body ? JSON.parse(init.body) : null };
    s.calls.push(req);
    if (init.signal) init.signal.addEventListener('abort', () => { s.aborted++; reject(Object.assign(new Error('aborted'), { name: 'AbortError' })); });
    setTimeout(() => {
      const st = req.auth === 'Bearer ' + s.token ? s.status : 401;
      resolve({
        ok: st >= 200 && st < 300, status: st,
        json: async () => (s.body ?? { error: 'x' }),
        blob: async () => ({ size: 10, text: req.body && req.body.text }),
      });
    }, s.delay);
  });
  return s;
}

function fakeAudio({ blockAutoplay = false, neverStart = false, playMs = 5 } = {}) {
  const a = {
    plays: [], pauses: 0, src: '', playbackRate: 1, defaultPlaybackRate: 1, paused: true, blockAutoplay, neverStart, started: 0,
    play() {
      this.paused = false;
      if (this.blockAutoplay) return Promise.reject(Object.assign(new Error('blocked'), { name: 'NotAllowedError' }));
      const src = this.src;
      if (!src.startsWith('blob:')) return Promise.resolve();   // the silent primer
      this.plays.push({ src, rate: this.playbackRate });
      if (!this.neverStart) setTimeout(() => { if (!this.paused && this.src === src) { this.onplaying && this.onplaying(); setTimeout(() => this.onended && this.onended(), playMs); } }, 1);
      return Promise.resolve();
    },
    pause() { this.paused = true; this.pauses++; },
  };
  return a;
}
const urls = { createObjectURL: (b) => 'blob:' + b.text, revokeObjectURL() {} };
const cloudCfg = { enabled: true, consent: true, token: 'tok', voice: '', voices: [] };
const mkEngine = (srv, audio, extra = {}) => new CloudEngine({ audio, urls, online: () => true, api: new CloudApi({ fetchFn: srv.fetch, getCfg: () => cloudCfg, baseHref: 'https://h.example/reader/index.html' }), ...extra });
const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));
const live = () => true;

test('speak: POST contract (url, bearer, body), plays through the one audio element at playbackRate', async () => {
  const srv = stubServer(); const audio = fakeAudio(); const e = mkEngine(srv, audio);
  const r = await e.speak('你好。', live, { cloud: cloudCfg, ttsRate: 1.5 });
  assert.equal(r, 'end');
  assert.equal(srv.calls[0].url, 'https://h.example/tts/v1/speak');
  assert.equal(srv.calls[0].method, 'POST');
  assert.equal(srv.calls[0].auth, 'Bearer tok');
  assert.deepEqual(srv.calls[0].body, { text: '你好。', voice: 'zh-CN-XiaoxiaoNeural', rate: 0 });
  assert.equal(audio.plays[0].rate, 1.5);
});

test('prime plays a silent clip synchronously (autoplay unlock)', () => {
  const audio = fakeAudio(); const e = mkEngine(stubServer(), audio);
  e.prime();
  assert.ok(audio.src.startsWith('data:audio/wav'));
  assert.equal(audio.paused, false);
});

test('prefetches up to 3 sentences ahead, once each; stop aborts in-flight fetches', async () => {
  const srv = stubServer({ delay: 20 }); const audio = fakeAudio(); const e = mkEngine(srv, audio);
  const texts = ['甲。', '乙。', '丙。', '丁。', '戊。'];
  e.prefetch(texts, { cloud: cloudCfg });
  assert.deepEqual(srv.calls.map((c) => c.body.text), ['甲。', '乙。', '丙。', '丁。']);
  const p = e.speak('甲。', live, { cloud: cloudCfg, ttsRate: 1 });
  assert.equal(srv.calls.length, 4, 'speak reuses the prefetched request');
  e.cancel();
  assert.equal(await p, 'cancel');
  assert.equal(srv.aborted, 4);
});

test('errors: 401 auth, 429 quota (with retryAfter), 502 error, network offline, empty-ish 413', async () => {
  const run = async (opts, fetchOverride) => {
    const srv = stubServer(opts); const e = mkEngine(srv, fakeAudio());
    if (fetchOverride) e.api.fetchFn = fetchOverride;
    const r = await e.speak('x。', live, { cloud: cloudCfg, ttsRate: 1 });
    return [r, e.lastError];
  };
  let [r, err] = await run({ token: 'other' });
  assert.equal(r, 'auth'); assert.match(err.message, /令牌/);
  [r, err] = await run({ status: 429, body: { error: 'quota', retryAfter: 120 } });
  assert.equal(r, 'quota'); assert.equal(err.retryAfter, 120);
  [r] = await run({ status: 502 });
  assert.equal(r, 'error');
  [r] = await run({ status: 413 });
  assert.equal(r, 'toolong');
  [r, err] = await run({}, () => Promise.reject(new TypeError('Failed to fetch')));
  assert.equal(r, 'offline');
});

test('autoplay blocked or audio that never starts -> nostart', async () => {
  const srv = stubServer();
  assert.equal(await mkEngine(srv, fakeAudio({ blockAutoplay: true })).speak('甲。', live, { cloud: cloudCfg, ttsRate: 1 }), 'nostart');
  const e = mkEngine(srv, fakeAudio({ neverStart: true }), { startTimeoutMs: 40 });
  assert.equal(await e.speak('乙。', live, { cloud: cloudCfg, ttsRate: 1 }), 'nostart');
});

test('pause before the audio arrives holds playback until resume', async () => {
  const srv = stubServer({ delay: 20 }); const audio = fakeAudio(); const e = mkEngine(srv, audio);
  const p = e.speak('甲。', live, { cloud: cloudCfg, ttsRate: 1 });
  e.pause();
  await tick(60);
  assert.equal(audio.plays.length, 0);
  e.resume();
  assert.equal(await p, 'end');
  assert.equal(audio.plays.length, 1);
});

// ---- TtsReader with the cloud engine: fallback rules
function fakeSynth() {
  const s = { spoken: [], getVoices: () => [{ voiceURI: 'v', name: 'V', lang: 'zh-CN', localService: true }], queue: null,
    speak(u) { s.spoken.push(u.text); setTimeout(() => u.onend && u.onend(), 0); }, cancel() {}, pause() {}, resume() {} };
  return s;
}
class Utter { constructor(t) { this.text = t; } }
function mkReader(srv, { cfg = cloudCfg, audio = fakeAudio(), noSynth = false } = {}) {
  const log = { notices: [], failed: [], finished: 0, reveal: [] };
  const synth = noSynth ? null : fakeSynth();
  const cloud = mkEngine(srv, audio);
  const host = {
    text: () => '甲。乙。丙。丁。', breaks: () => null, reveal: (a, b) => log.reveal.push([a, b]), nextSection: async () => false,
    changed() {}, finished: () => { log.finished++; }, failed: (m) => log.failed.push(m), notice: (m) => log.notices.push(m),
    settings: () => ({ ttsRate: 1, ttsVoice: '', cloud: cfg }),
  };
  return { r: new TtsReader({ synth, Utter: noSynth ? null : Utter, host, cloud }), synth, audio, log, cloud };
}

test('reader + cloud: reads the whole text through <audio>, browser synth untouched', async () => {
  const srv = stubServer(); const { r, synth, audio, log } = mkReader(srv);
  r.start(0);
  await tick(150);
  assert.equal(log.finished, 1);
  assert.equal(audio.plays.length, 4);
  assert.equal(synth.spoken.length, 0);
  assert.equal(srv.calls.length, 4, 'each sentence fetched exactly once (prefetch reused)');
});

test('reader + cloud: cloud disabled (or no consent) -> browser engine, no network, no hint when voices exist', async () => {
  const srv = stubServer(); const { r, synth, log } = mkReader(srv, { cfg: { ...cloudCfg, enabled: false } });
  r.start(0);
  await tick(60);
  assert.equal(synth.spoken.length, 4);
  assert.equal(srv.calls.length, 0);
  assert.equal(log.notices.length, 0);
});

test('reader + cloud: quota exhausted -> notice + automatic fallback to browser voice from the same sentence', async () => {
  const srv = stubServer({ status: 429, body: { error: 'quota', retryAfter: 7200 } });
  const { r, synth, log } = mkReader(srv);
  r.start(0);
  await tick(100);
  assert.equal(log.notices.length, 1);
  assert.match(log.notices[0], /额度已用完.*2 小时/);
  assert.deepEqual(synth.spoken, ['甲。', '乙。', '丙。', '丁。']);
  assert.equal(log.finished, 1);
  assert.equal(log.failed.length, 0);
});

test('reader + cloud: bad token stops with a clear message, no fallback', async () => {
  const srv = stubServer({ token: 'other' });
  const { r, synth, log } = mkReader(srv);
  r.start(0);
  await tick(60);
  assert.equal(r.state, 'idle');
  assert.match(log.failed[0], /令牌/);
  assert.equal(synth.spoken.length, 0);
});

test('reader + cloud: repeated 502 falls back to the browser after 3 failures; network down falls back at once', async () => {
  const a = mkReader(stubServer({ status: 502 }));
  a.r.start(0);
  await tick(150);
  assert.equal(a.log.notices.length, 1);
  assert.match(a.log.notices[0], /已切换回浏览器朗读/);
  assert.ok(a.synth.spoken.length >= 1 && a.log.finished === 1);
  const b = mkReader(stubServer());
  b.cloud.api.fetchFn = () => Promise.reject(new TypeError('net'));
  b.r.start(0);
  await tick(100);
  assert.match(b.log.notices[0], /网络不可用/);
  assert.equal(b.synth.spoken.length, 4);
});

test('reader + cloud: autoplay blocked falls back; stop aborts and nothing more is fetched', async () => {
  const a = mkReader(stubServer(), { audio: fakeAudio({ blockAutoplay: true }) });
  a.r.start(0);
  await tick(100);
  assert.match(a.log.notices[0], /无法播放音频/);
  assert.equal(a.synth.spoken.length, 4);
  const srv = stubServer({ delay: 30 }); const b = mkReader(srv);
  b.r.start(0);
  await tick(5);
  b.r.stop();
  await tick(80);
  assert.equal(b.audio.plays.length, 0);
  assert.equal(srv.aborted, srv.calls.length, 'every in-flight request was aborted');
});

test('reader without speechSynthesis: cloud works; when cloud is off it explains where to enable it', async () => {
  const on = mkReader(stubServer(), { noSynth: true });
  on.r.start(0);
  await tick(200);
  assert.equal(on.log.finished, 1);
  const off = mkReader(stubServer(), { noSynth: true, cfg: { ...cloudCfg, enabled: false } });
  off.r.start(0);
  assert.match(off.log.failed[0], /在线朗读/);
  assert.equal(off.r.state, 'idle');
});

test('suggestion: empty getVoices() shows the cloud hint once (non-blocking notice)', async () => {
  const { r, synth, log } = mkReader(stubServer(), { cfg: { ...cloudCfg, enabled: false } });
  synth.getVoices = () => [];
  r.start(0); await tick(60); r.start(0); await tick(60);
  assert.equal(log.notices.length, 1);
  assert.match(log.notices[0], /没有可用的朗读语音/);
  assert.equal(log.failed.length, 0);
});
