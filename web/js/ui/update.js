// Service worker registration + "new version available" prompt. Decisions live in core/swupdate.js, the worker side in
// sw.js. Never reloads by itself: a waiting worker only shows a small dismissible bar, and the reload happens after
// the user clicked 刷新 and the new worker took control (or at once if it already did, see updateAction).
import { h, confirmDialog } from './dom.js';
import { MSG_PING, MSG_SKIP, updateAction, shouldCheck, reloadAllowed } from '../core/swupdate.js';

const RELOAD_KEY = 'pyreader-sw-reload-at';
const SKIP_GIVE_UP_MS = 5000;

const guardGet = () => { try { return Number(sessionStorage.getItem(RELOAD_KEY)) || 0; } catch { return 0; } };
const guardSet = (t) => { try { sessionStorage.setItem(RELOAD_KEY, String(t)); } catch { /* storage may be blocked */ } };

/**
 * @param {{isBusy?:()=>string|null, prepareReload?:()=>Promise<void>, reload?:()=>void}} o
 *   isBusy         returns a short reason (e.g. '正在朗读') when reloading would interrupt something, else null
 *   prepareReload  save/flush whatever is pending before the page goes away
 */
export async function startUpdates({ isBusy = () => null, prepareReload = async () => {}, reload = () => location.reload() } = {}) {
  const sw = navigator.serviceWorker;
  const st = { hadController: !!sw.controller, hasWaiting: false, controllerSwapped: false, requested: false, reloaded: false };
  let bar = null;
  let dismissed = false;
  let accepting = false;   // 刷新 clicked and not yet resolved: a second click/Enter must not stack another confirm dialog
  let lastCheck = 0;
  const watched = new WeakSet();

  // The worker asks every open window at install time whether it can show this prompt (see sw.js).
  sw.addEventListener('message', (e) => {
    if (e.data && e.data.type === MSG_PING && e.ports && e.ports[0]) e.ports[0].postMessage({ capable: true });
  });

  const showBar = () => {
    if (bar || dismissed) return;
    const go = h('button', { class: 'btn small primary', type: 'button', onclick: () => accept() }, '刷新');
    bar = h('div', { class: 'update-bar', role: 'status', 'aria-live': 'polite' },
      h('span', { text: '有新版本可用' }), go,
      h('button', { class: 'btn small', type: 'button', onclick: () => { dismissed = true; hideBar(); } }, '稍后'));
    bar.go = go;
    document.body.append(bar);
  };
  const hideBar = () => { if (bar) { bar.remove(); bar = null; } };

  const evaluate = () => {
    const act = updateAction(st);
    if (act === 'prompt') showBar();
    else if (act === 'reload' && reloadAllowed(Date.now(), guardGet())) {
      st.reloaded = true;
      guardSet(Date.now());
      reload();
    }
  };

  async function accept() {
    if (accepting) return;
    accepting = true;
    const give = () => { accepting = false; if (bar) bar.go.disabled = false; };
    if (bar) bar.go.disabled = true;
    const why = isBusy();
    if (why && !(await confirmDialog(`${why}，现在刷新会中断。仍要刷新吗？`, { ok: '刷新' }))) return give();
    // the waiting worker may have been replaced/discarded since the bar was shown: nothing to activate, look again
    if (!reg.waiting && !st.controllerSwapped) {
      st.hasWaiting = false;
      hideBar();
      accepting = false;
      check(true);
      return;
    }
    try { await prepareReload(); } catch (e) { console.warn('[sw] prepare failed', e); }
    st.requested = true;
    if (reg.waiting) {
      reg.waiting.postMessage({ type: MSG_SKIP });
      // normally controllerchange follows within milliseconds; if not, say so instead of hanging
      setTimeout(() => {
        if (st.reloaded) return;
        st.requested = false;
        give();
        const t = bar && bar.querySelector('span');
        if (t) t.textContent = '更新没有生效，请关闭所有标签页后重新打开';
      }, SKIP_GIVE_UP_MS);
    }
    evaluate();   // already swapped by someone else: reloads now
  }

  sw.addEventListener('controllerchange', () => {
    if (!st.hadController) { st.hadController = true; return; }   // first install claiming this page: it is already current
    st.controllerSwapped = true;
    dismissed = false;   // this page's JS no longer matches what the worker serves: ask again
    evaluate();
  });

  const reg = await sw.register('sw.js');
  if (reg.active) st.hadController = true;   // also covers a hard reload (page uncontrolled although a worker exists)
  // Re-read the registration whenever a worker changes state. A worker the browser's own navigation-time check already
  // started installing (or finished installing) before this code ran fires no more updatefound for us, so look at
  // reg.installing/reg.waiting directly as well.
  const sync = () => {
    st.hasWaiting = !!reg.waiting;
    if (!st.hasWaiting && !st.requested && !st.controllerSwapped) hideBar();   // the waiting worker went away (replaced): nothing to offer
    evaluate();
  };
  const watch = (w) => {
    if (!w || watched.has(w)) return;
    watched.add(w);
    w.addEventListener('statechange', sync);
  };
  const adopt = () => { watch(reg.installing); watch(reg.waiting); sync(); };
  reg.addEventListener('updatefound', adopt);
  adopt();

  const check = (force = false) => {
    const now = Date.now();
    if (!force && !shouldCheck(now, lastCheck)) return;
    lastCheck = now;
    reg.update().then(adopt, () => {});   // offline / server hiccup: try again at the next trigger
  };
  check(true);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') { adopt(); check(); } });
  window.addEventListener('online', () => check());
  return { check };
}
