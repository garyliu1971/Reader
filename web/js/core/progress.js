// Per-book progress records, wire format of pyreader/sync.py:
//   {"book_id","offset","percent"(0..100),"chapter","updated"(unix SECONDS, float),"device"}
// Rules: strictly newer `updated` wins; percent is portable, offset is NOT (Qt: code-point index into its own text).

export const FUTURE_LIMIT_S = 24 * 3600;

const isNum = (x) => typeof x === 'number' && Number.isFinite(x);

/**
 * `offset` is a code-point index into the *desktop app's own* extracted text, which the web reader cannot know.
 * The desktop (sync.resolve_offset) trusts any 0 <= offset < text_len, so writing 0 would send it to the start of the
 * book. We therefore write an out-of-range value on purpose: the desktop then falls back to `percent`.
 */
export const OFFSET_UNKNOWN = 1_000_000_000_000;

export function makeRecord({ bid, offset = OFFSET_UNKNOWN, percent, chapter = '', updated, device }) {
  return {
    book_id: bid,
    offset: Math.max(0, Math.trunc(offset) || 0),
    percent: Math.round(Math.min(100, Math.max(0, percent)) * 1000) / 1000,
    chapter: String(chapter || '').slice(0, 256),
    updated,
    device,
  };
}

/** Newer `updated` wins; on a tie the local one is kept (same as Python merge_progress). */
export function mergeProgress(local, remote) {
  if (!remote) return local || null;
  if (!local) return remote;
  return Number(remote.updated) > Number(local.updated) ? remote : local;
}

/**
 * Parse + validate a progress file body fetched from the server (untrusted).
 * book_id inside the body must equal the expected id. percent/updated must be finite; updated more than 24h ahead
 * of `nowSec` is a bad record. Returns {percent, updated, device, chapter} or null.
 */
export function parseProgressBody(text, bid, nowSec = Date.now() / 1000) {
  let d;
  try { d = JSON.parse(text); } catch { return null; }
  if (!d || typeof d !== 'object' || Array.isArray(d) || d.book_id !== bid) return null;
  const { percent, updated } = d;
  if (!isNum(percent) || !isNum(updated) || percent < 0 || updated <= 0 || updated > nowSec + FUTURE_LIMIT_S) return null;
  const clean = (v, n) => (typeof v === 'string' ? v.replace(/[\u0000-\u001f\u007f]/g, '').slice(0, n) : '');
  return { percent: Math.min(100, percent), updated, device: clean(d.device, 64), chapter: clean(d.chapter, 256) };
}

/**
 * Timestamp (seconds) to write. `skewMs` = serverTime - localTime when the server Date header was readable
 * (then the server clock is the reference); never more than 2 s ahead of the reference clock.
 */
export function stampNow(nowMs = Date.now(), skewMs = 0) {
  return (nowMs + (Number.isFinite(skewMs) ? skewMs : 0)) / 1000;
}

/**
 * Decide what to do with a remote progress for an open book.
 * @returns 'adopt' (remote is newer: offer/perform jump), 'keep' (local at least as new) or 'none'
 */
export function decideRemote(local, remote) {
  if (!remote) return 'none';
  if (!local) return 'adopt';
  return Number(remote.updated) > Number(local.updated) ? 'adopt' : 'keep';
}
