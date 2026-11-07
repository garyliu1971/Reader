// Per-device online read-aloud config (token included) in IndexedDB. Kept in its own key so the reader's display-settings
// writes (which rewrite 'reader.settings' whole) can never clobber it. Sync cache so the reader can read it inside a tap handler.
import { kvGet, kvSet } from './db.js';
import { normalizeCloud } from '../core/cloudtts.js';

let cache = normalizeCloud(null);
const listeners = new Set();
const notify = () => { for (const fn of [...listeners]) { try { fn(cache); } catch { /* a bad listener must not break saving */ } } };
export const getCloudCfg = () => cache;
/** call fn(cfg) after every load/save; returns the unsubscribe function */
export function onCloudCfgChange(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export async function loadCloudCfg() { try { cache = normalizeCloud(await kvGet('tts.cloud', null)); } catch { /* keep defaults */ } notify(); return cache; }
export async function saveCloudCfg(patch) {
  cache = normalizeCloud({ ...cache, ...patch });
  notify();
  try { await kvSet('tts.cloud', cache); } catch { /* memory only */ }
  return cache;
}
