// IndexedDB wrapper. Every access is wrapped: if IndexedDB is unavailable (private window, blocked storage) the app
// keeps working from an in-memory fallback for the session and `storageMode()` reports it so the UI can warn.

const DB_NAME = 'pyreader-web';
const DB_VERSION = 1;
const STORES = {
  books: { keyPath: 'id' },            // book metadata (no file bytes)
  files: { keyPath: 'id' },            // {id, blob}
  covers: { keyPath: 'id' },           // {id, blob}
  progress: { keyPath: 'bid' },        // {bid, anchor, percent, updated, chapter, device}
  bookmarks: { keyPath: 'id', autoIncrement: true, indexes: [['bid', 'bid']] },
  kv: { keyPath: 'key' },              // settings, sync config, overlay, cursors
};

let dbPromise = null;
let mode = 'indexeddb';
const mem = {};
for (const s of Object.keys(STORES)) mem[s] = new Map();
let memSeq = 1;

export function storageMode() { return mode; }

function open() {
  if (dbPromise) return dbPromise;
  dbPromise = new Promise((resolve) => {
    let req;
    try {
      if (typeof indexedDB === 'undefined') throw new Error('no indexedDB');
      req = indexedDB.open(DB_NAME, DB_VERSION);
    } catch { mode = 'memory'; resolve(null); return; }
    req.onupgradeneeded = () => {
      const db = req.result;
      for (const [name, def] of Object.entries(STORES)) {
        if (db.objectStoreNames.contains(name)) continue;
        const os = db.createObjectStore(name, { keyPath: def.keyPath, autoIncrement: !!def.autoIncrement });
        for (const [iname, kp] of def.indexes || []) os.createIndex(iname, kp);
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => { mode = 'memory'; resolve(null); };
    req.onblocked = () => { mode = 'memory'; resolve(null); };
  });
  return dbPromise;
}

function wrap(req) {
  return new Promise((resolve, reject) => { req.onsuccess = () => resolve(req.result); req.onerror = () => reject(req.error); });
}

async function run(store, rw, fn, memFn) {
  const db = await open();
  if (db) {
    try {
      const tx = db.transaction(store, rw ? 'readwrite' : 'readonly');
      const done = new Promise((res, rej) => { tx.oncomplete = () => res(); tx.onerror = () => rej(tx.error); tx.onabort = () => rej(tx.error); });
      const out = await fn(tx.objectStore(store));
      if (rw) await done;
      return out;
    } catch (e) {
      if (e && e.name === 'QuotaExceededError') throw e;   // surface to the caller (import UI shows it)
      console.warn('[db]', store, e);
      throw e;
    }
  }
  return memFn(mem[store]);
}

export const db = {
  get: (store, key) => run(store, false, (os) => wrap(os.get(key)), (m) => m.get(key)).catch(() => undefined),
  getAll: (store) => run(store, false, (os) => wrap(os.getAll()), (m) => [...m.values()]).catch(() => []),
  async put(store, value) {
    return run(store, true, (os) => wrap(os.put(value)), (m) => {
      const def = STORES[store];
      if (def.autoIncrement && value[def.keyPath] == null) value[def.keyPath] = memSeq++;
      m.set(value[def.keyPath], value);
      return value[def.keyPath];
    });
  },
  del: (store, key) => run(store, true, (os) => wrap(os.delete(key)), (m) => { m.delete(key); }).catch(() => undefined),
  byIndex: (store, index, key) => run(store, false, (os) => wrap(os.index(index).getAll(key)),
    (m) => [...m.values()].filter((v) => v[index] === key)).catch(() => []),
};

export async function kvGet(key, dflt = undefined) {
  const row = await db.get('kv', key);
  return row && 'value' in row ? row.value : dflt;
}
export async function kvSet(key, value) {
  try { await db.put('kv', { key, value }); return true; } catch { return false; }
}

/** Remove a book and everything stored for it (local copy only; remote data is never touched). */
export async function deleteBookData(id) {
  await db.del('files', id);
  await db.del('covers', id);
  await db.del('progress', id);
  for (const b of await db.byIndex('bookmarks', 'bid', id)) await db.del('bookmarks', b.id);
  await db.del('books', id);
}

export async function persistRequest() {
  try { return navigator.storage && navigator.storage.persist ? await navigator.storage.persist() : false; } catch { return false; }
}

export async function storageEstimate() {
  try { return navigator.storage && navigator.storage.estimate ? await navigator.storage.estimate() : null; } catch { return null; }
}
