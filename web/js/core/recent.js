// Shelf helpers for "continue reading": pick the book to resume and word "when" in Chinese.

export const DONE_PERCENT = 97;   // from here a book counts as finished (same threshold as the shelf's 读完 filter)
export const DWELL_MS = 30_000;   // a book kept open this long (tab visible) counts as read even if no page was turned

/**
 * When the book was last really read (ms, 0 = never): the newest of the saved progress stamp (page turn / scroll /
 * read-aloud advance), the "kept open past DWELL_MS" stamp and a progress stamp another device pushed. Merely opening
 * a book is none of these, so it never competes for "continue reading". `openedAt` (written by older builds on every
 * open) is deliberately ignored.
 */
export function touchedAt({ lastRead = 0, readAt = 0 } = {}, remoteUpdatedMs = 0) {
  return Math.max(lastRead || 0, readAt || 0, remoteUpdatedMs || 0);
}

/**
 * The entry to resume: the most recently read book (see touchedAt) that is not hidden, not still uploading and
 * not finished. `entries[i].touched` is a ms timestamp (0 = never read). Ties keep the earlier entry.
 */
export function continueCandidate(entries) {
  let best = null;
  for (const e of entries) {
    if (e.hidden || e.uploading) continue;
    const t = e.touched || 0;
    if (t <= 0 || e.percent >= DONE_PERCENT) continue;
    if (!best || t > best.touched) best = e;
  }
  return best;
}

const pad = (n) => String(n).padStart(2, '0');

/** "刚刚" / "5 分钟前" / "3 小时前" / "昨天" / "6 天前" / "2026-10-03" */
export function ago(ts, now = Date.now()) {
  const s = Math.max(0, Math.round((now - ts) / 1000));
  if (s < 60) return '刚刚';
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} 分钟前`;
  const hr = Math.floor(m / 60);
  if (hr < 24) return `${hr} 小时前`;
  const d = Math.floor(hr / 24);
  if (d === 1) return '昨天';
  if (d < 30) return `${d} 天前`;
  const dt = new Date(ts);
  return `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}`;
}
