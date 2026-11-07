// Sleep timer for read-aloud: pure logic, no DOM and no real timers (the reader polls `check` with the clock).
// A timer is either {kind: 'time', endAt} (stop at that clock time) or {kind: 'chapter'} (stop when the current chapter
// has been read to its end). `null` = no timer.

export const SLEEP_MINUTES = [15, 30, 45, 60];

/** timer for a menu choice: a number of minutes from SLEEP_MINUTES, or 'chapter'; null for anything else */
export function makeTimer(choice, now) {
  if (choice === 'chapter') return { kind: 'chapter' };
  const m = Number(choice);
  if (!SLEEP_MINUTES.includes(m)) return null;
  return { kind: 'time', endAt: now + m * 60000 };
}

/** milliseconds left (never negative); null for no timer or the chapter kind (no clock involved) */
export function remainingMs(t, now) {
  return t && t.kind === 'time' ? Math.max(0, t.endAt - now) : null;
}

/** true once a time timer has run out. The chapter kind is ended by the reader itself, never by the clock. */
export function isDue(t, now) {
  return !!t && t.kind === 'time' && now >= t.endAt;
}

/** "mm:ss", or "h:mm:ss" from one hour up; rounds UP so the display never shows 00:00 before the timer fires */
export function formatRemaining(ms) {
  const total = Math.max(0, Math.ceil(ms / 1000));
  const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
  const two = (n) => String(n).padStart(2, '0');
  return h ? `${h}:${two(m)}:${two(s)}` : `${two(m)}:${two(s)}`;
}

/** short label for the TTS bar button: the countdown, "本章" for end-of-chapter, "定时" for none */
export function timerLabel(t, now) {
  if (!t) return '定时';
  if (t.kind === 'chapter') return '本章末';
  return formatRemaining(remainingMs(t, now));
}

/** menu choice that is active right now (to mark it), or null. Minutes are not recoverable once counting, so only 'chapter'. */
export const activeChoice = (t) => (t && t.kind === 'chapter' ? 'chapter' : null);
