// Service-worker update decisions (pure; the DOM/service-worker plumbing is in ui/update.js, the worker side in sw.js).
//
// Protocol (page <-> worker; sw.js is a classic script and repeats these constants, test/swupdate.test.js pins them):
//   worker -> page  {type: MSG_PING} + a MessagePort: "does this page understand the update prompt?"; a page that does
//                   answers {capable: true} on the port. A page from before this feature (<= SW v7) cannot answer.
//   page -> worker  {type: MSG_SKIP}: the user clicked 刷新, activate now.
// A new worker waits (no automatic skipWaiting) only when EVERY open window answered the ping; with any silent (old)
// window it activates at once, exactly like the pre-prompt workers did, because that old page has no way to ask.

export const MSG_PING = 'pyreader:ping';
export const MSG_SKIP = 'pyreader:skip-waiting';
export const PING_TIMEOUT_MS = 500;
export const CHECK_MIN_GAP_MS = 5 * 60 * 1000;      // visibility/online triggered update() calls are throttled to this
export const RELOAD_GUARD_MS = 10 * 1000;           // never reload twice within this (loop guard)

/**
 * What should the page do right now?
 * @param {{hadController:boolean, hasWaiting:boolean, controllerSwapped:boolean, requested:boolean, reloaded:boolean}} s
 *   hadController   page was already controlled by a worker when it started (false on the very first visit / hard reload)
 *   hasWaiting      registration.waiting is set (an installed worker is waiting for permission to activate)
 *   controllerSwapped  a `controllerchange` happened after the page started
 *   requested       the user already clicked 刷新
 *   reloaded        this page already issued a reload
 * @returns {'none'|'prompt'|'reload'}
 */
export function updateAction({ hadController, hasWaiting, controllerSwapped, requested, reloaded }) {
  if (reloaded || !hadController) return 'none';      // first install: clients.claim() fires controllerchange, nothing stale to replace
  if (requested && controllerSwapped) return 'reload';
  if (hasWaiting || controllerSwapped) return 'prompt'; // swapped without a click: another tab/old worker activated it, this page's JS is stale
  return 'none';
}

/** throttle for registration.update() calls */
export function shouldCheck(now, lastCheck, minGap = CHECK_MIN_GAP_MS) {
  return !lastCheck || now - lastCheck >= minGap || now < lastCheck;
}

/** loop guard: may we reload now, given when we last reloaded for an update (ms epoch, 0/undefined = never)? */
export function reloadAllowed(now, lastReload, guard = RELOAD_GUARD_MS) {
  return !lastReload || now < lastReload || now - lastReload >= guard;
}

