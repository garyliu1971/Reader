// Service worker: app shell cached for offline use (stale-while-revalidate for same-origin GET).
// Only requests under this worker's own directory are handled. Everything else (cross-origin WebDAV, or a WebDAV
// server on the SAME origin: /library, /users, ...) is never touched, so authenticated responses and progress JSON
// are never cached or served stale (the /tts/ API sits beside the app dir and is skipped explicitly, GET or not). Bump CACHE when shipping a new version.
//
// Updates: a new worker normally WAITS after install (the open page keeps its own cache generation, so lazily imported
// modules never come from a different version than the running JS) and activates only when the page asks for it
// (MSG_SKIP, the 刷新 button; see js/ui/update.js). Pages from before this prompt existed (<= v7) cannot ask, so at
// install the worker pings every open app window (same origin AND under SCOPE_PATH; other tabs of a shared origin such
// as the WebDAV host's own pages are ignored); if any one does not answer it activates immediately, as v7 always did.
// While a newer worker is installing/waiting this worker stops writing revalidated responses into its cache, so the
// running page keeps getting its own generation's bytes (a lazy import never mixes in the new deploy).
// Constants repeat js/core/swupdate.js (a classic worker cannot import it); test/swupdate.test.js keeps them equal.
const CACHE = 'pyreader-web-v11';
const MSG_PING = 'pyreader:ping';
const MSG_SKIP = 'pyreader:skip-waiting';
const PING_TIMEOUT_MS = 2000;
const SCOPE_PATH = new URL('./', self.location).pathname;
const SHELL = [
  './', 'index.html', 'manifest.webmanifest', 'css/app.css',
  'icons/icon.svg', 'icons/icon-192.png', 'icons/icon-512.png', 'icons/maskable-512.png',
  'js/app.js', 'js/library.js',
  'js/core/anchor.js', 'js/core/bookid.js', 'js/core/chapters.js', 'js/core/cloudtts.js', 'js/core/names.js', 'js/core/origin.js', 'js/core/progress.js', 'js/core/sha1.js',
  'js/core/recent.js', 'js/core/search.js', 'js/core/shelfmerge.js', 'js/core/sleeptimer.js', 'js/core/swupdate.js', 'js/core/textenc.js',
  'js/parse/epub.js', 'js/parse/mobi.js', 'js/parse/mobicheck.js', 'js/parse/pdf.js', 'js/parse/pdftext.js', 'js/parse/sanitize.js', 'js/parse/textbooks.js', 'js/parse/txt.worker.js', 'js/parse/txtparse.js',
  'js/reader/cloudengine.js', 'js/reader/find.js', 'js/reader/paginator.js', 'js/reader/reader.js', 'js/reader/tts.js', 'js/reader/translate.js',
  'js/store/cloudcfg.js', 'js/store/db.js', 'js/store/settings.js',
  'js/sync/manager.js', 'js/sync/propfind.js', 'js/sync/sync.js', 'js/sync/webdav.js',
  'js/ui/dom.js', 'js/ui/icons.js', 'js/ui/settings.js', 'js/ui/shelf.js', 'js/ui/update.js',
  'vendor/jszip.min.js', 'vendor/marked.esm.js', 'vendor/foliate-mobi.js',
];

// true when every open app window answered the ping with {capable: true} (no windows at all counts as yes)
async function allWindowsCanPrompt() {
  const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  const wins = all.filter((w) => { try { return new URL(w.url).pathname.startsWith(SCOPE_PATH); } catch { return false; } });
  const answers = await Promise.all(wins.map((w) => new Promise((resolve) => {
    const ch = new MessageChannel();
    const t = setTimeout(() => resolve(false), PING_TIMEOUT_MS);
    ch.port1.onmessage = (ev) => { clearTimeout(t); resolve(!!(ev.data && ev.data.capable)); };
    try { w.postMessage({ type: MSG_PING }, [ch.port2]); } catch { clearTimeout(t); resolve(false); }
  })));
  return answers.every(Boolean);
}

self.addEventListener('install', (e) => {
  e.waitUntil((async () => {
    const c = await caches.open(CACHE);
    await Promise.all(SHELL.map((u) => c.add(new Request(u, { cache: 'reload' })).catch(() => {})));
    if (!(await allWindowsCanPrompt())) await self.skipWaiting();
  })());
});

self.addEventListener('message', (e) => {
  if (e.data && e.data.type === MSG_SKIP) self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin || !url.pathname.startsWith(SCOPE_PATH)) return;
  if (url.pathname.includes('/tts/')) return;   // online read-aloud API (authenticated audio): never cached, even if the app is served from the site root
  e.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const hit = await cache.match(req, { ignoreSearch: true });
    const net = fetch(req).then((r) => {
      // a newer worker installing/waiting means the network now serves the NEXT version: keep it out of this generation
      if (r && r.ok && r.type === 'basic' && !self.registration.installing && !self.registration.waiting) cache.put(req, r.clone());
      return r;
    }).catch(() => null);
    if (hit) { net.catch(() => {}); return hit; }
    const r = await net;
    if (r) return r;
    if (req.mode === 'navigate') { const idx = await cache.match('index.html'); if (idx) return idx; }
    return new Response('offline', { status: 503, statusText: 'offline' });
  })());
});
