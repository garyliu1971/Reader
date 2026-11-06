// Service worker: app shell cached for offline use (stale-while-revalidate for same-origin GET).
// Only requests under this worker's own directory are handled. Everything else (cross-origin WebDAV, or a WebDAV
// server on the SAME origin: /library, /users, ...) is never touched, so authenticated responses and progress JSON
// are never cached or served stale. Bump CACHE when shipping a new version.
const CACHE = 'pyreader-web-v7';
const SCOPE_PATH = new URL('./', self.location).pathname;
const SHELL = [
  './', 'index.html', 'manifest.webmanifest', 'css/app.css',
  'icons/icon.svg', 'icons/icon-192.png', 'icons/icon-512.png', 'icons/maskable-512.png',
  'js/app.js', 'js/library.js',
  'js/core/anchor.js', 'js/core/bookid.js', 'js/core/chapters.js', 'js/core/names.js', 'js/core/progress.js', 'js/core/sha1.js',
  'js/core/recent.js', 'js/core/shelfmerge.js', 'js/core/textenc.js',
  'js/parse/epub.js', 'js/parse/mobi.js', 'js/parse/mobicheck.js', 'js/parse/pdf.js', 'js/parse/sanitize.js', 'js/parse/textbooks.js', 'js/parse/txt.worker.js', 'js/parse/txtparse.js',
  'js/reader/paginator.js', 'js/reader/reader.js', 'js/reader/tts.js', 'js/reader/translate.js',
  'js/store/db.js', 'js/store/settings.js',
  'js/sync/manager.js', 'js/sync/propfind.js', 'js/sync/sync.js', 'js/sync/webdav.js',
  'js/ui/dom.js', 'js/ui/icons.js', 'js/ui/settings.js', 'js/ui/shelf.js',
  'vendor/jszip.min.js', 'vendor/marked.esm.js', 'vendor/foliate-mobi.js',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => Promise.all(SHELL.map((u) => c.add(new Request(u, { cache: 'reload' })).catch(() => {})))).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin || !url.pathname.startsWith(SCOPE_PATH)) return;
  e.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const hit = await cache.match(req, { ignoreSearch: true });
    const net = fetch(req).then((r) => { if (r && r.ok && r.type === 'basic') cache.put(req, r.clone()); return r; }).catch(() => null);
    if (hit) { net.catch(() => {}); return hit; }
    const r = await net;
    if (r) return r;
    if (req.mode === 'navigate') { const idx = await cache.match('index.html'); if (idx) return idx; }
    return new Response('offline', { status: 503, statusText: 'offline' });
  })());
});
