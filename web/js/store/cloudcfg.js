// Per-device online read-aloud config (token included) in IndexedDB. Kept in its own key so the reader's display-settings
// writes (which rewrite 'reader.settings' whole) can never clobber it. Sync cache so the reader can read it inside a tap handler.
import { kvGet, kvSet } from './db.js';
import { normalizeCloud } from '../core/cloudtts.js';

let cache = normalizeCloud(null);
export const getCloudCfg = () => cache;
export async function loadCloudCfg() { try { cache = normalizeCloud(await kvGet('tts.cloud', null)); } catch { /* keep defaults */ } return cache; }
export async function saveCloudCfg(patch) {
  cache = normalizeCloud({ ...cache, ...patch });
  try { await kvSet('tts.cloud', cache); } catch { /* memory only */ }
  return cache;
}
