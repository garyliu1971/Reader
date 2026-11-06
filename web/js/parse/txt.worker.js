import { parseTxt } from './txtparse.js';

self.onmessage = (e) => {
  try {
    const r = parseTxt(new Uint8Array(e.data.buf));
    self.postMessage({ ok: true, ...r });
  } catch (err) {
    self.postMessage({ ok: false, error: String(err && err.message || err) });
  }
};
