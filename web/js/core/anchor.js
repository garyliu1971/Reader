// Reading position model. A position is never a page index: it is
//   {s: section index, o: text offset inside the section, f: fraction o/len at save time, c: a few chars of context}
// "text offset" = offset in the concatenation of the section's DOM text nodes (document order).
// Percent (0..100) is derived from per-section text lengths `lens`; it is the only thing shared with the Qt app.

export function cumulative(lens) {
  const cum = new Array(lens.length + 1);
  cum[0] = 0;
  for (let i = 0; i < lens.length; i++) cum[i + 1] = cum[i] + Math.max(0, lens[i] || 0);
  return cum;
}

const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));

/** (section, fraction inside section) -> percent 0..100 */
export function positionToPercent(lens, s, frac) {
  const cum = cumulative(lens);
  const total = cum[lens.length];
  if (!total) return lens.length > 1 ? clamp((s / (lens.length - 1)) * 100, 0, 100) : 0;
  const si = clamp(s, 0, lens.length - 1);
  return clamp(((cum[si] + clamp(frac, 0, 1) * (lens[si] || 0)) / total) * 100, 0, 100);
}

/** percent -> {s, f}. Binary search over cumulative lengths. */
export function percentToPosition(lens, percent) {
  const n = lens.length;
  if (!n) return { s: 0, f: 0 };
  const cum = cumulative(lens);
  const total = cum[n];
  const p = clamp(Number.isFinite(percent) ? percent : 0, 0, 100);
  if (!total) return { s: n > 1 ? Math.round((p / 100) * (n - 1)) : 0, f: 0 };
  const target = (p / 100) * total;
  let lo = 0, hi = n - 1;
  while (lo < hi) {   // last section whose start <= target
    const mid = (lo + hi + 1) >> 1;
    if (cum[mid] <= target) lo = mid; else hi = mid - 1;
  }
  // an empty section would divide by zero: step forward to the next non-empty one
  let s = lo;
  while (s < n - 1 && !lens[s]) s++;
  const len = lens[s] || 0;
  return { s, f: len ? clamp((target - cum[s]) / len, 0, 1) : 0 };
}

/** Make an anchor from a text offset inside a section's text. */
export function makeAnchor(s, offset, sectionText) {
  const len = sectionText.length;
  const o = clamp(offset, 0, len);
  return { s, o, f: len ? o / len : 0, c: sectionText.slice(o, o + 12) };
}

/**
 * Resolve an anchor against (possibly changed) section text: trust the offset if the context still matches there;
 * otherwise search for the context near it; otherwise fall back to the saved fraction.
 */
export function resolveOffset(anchor, sectionText) {
  const len = sectionText.length;
  const guess = clamp(Math.round(anchor.o ?? (anchor.f ?? 0) * len), 0, len);
  const ctx = anchor.c || '';
  if (!ctx || sectionText.startsWith(ctx, guess)) return guess;
  const window = 2000;
  const from = Math.max(0, guess - window);
  const hay = sectionText.slice(from, Math.min(len, guess + window + ctx.length));
  let best = -1;
  for (let i = hay.indexOf(ctx); i >= 0; i = hay.indexOf(ctx, i + 1)) {
    if (best < 0 || Math.abs(from + i - guess) < Math.abs(from + best - guess)) best = i;
  }
  if (best >= 0) return from + best;
  return clamp(Math.round((anchor.f ?? 0) * len), 0, len);
}
