// PyReader Web entry: boot, routing (shelf <-> reader), sync scheduling, PWA install/service worker.
import { loadSettings, applyTheme } from './store/settings.js';
import { syncMgr } from './sync/manager.js';
import { ShelfView } from './ui/shelf.js';
import { ReaderView } from './reader/reader.js';
import { openSettingsDialog } from './ui/settings.js';
import { toast } from './ui/dom.js';
import { storageMode } from './store/db.js';
import { startUpdates } from './ui/update.js';

const shelfEl = document.getElementById('shelf');
const readerEl = document.getElementById('reader');
let deferredInstall = null;

const routeOf = () => {
  const m = /^#\/read\/(.+)$/.exec(location.hash);
  return m ? { name: 'read', id: decodeURIComponent(m[1]) } : { name: 'shelf' };
};

async function main() {
  const settings = await loadSettings();
  applyTheme(settings.theme);
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', async () => applyTheme((await loadSettings()).theme));
  await syncMgr.load();

  const shelf = new ShelfView(shelfEl, {
    open: (id) => { history.pushState({ fromShelf: true }, '', '#/read/' + encodeURIComponent(id)); route(); },
    openSettings: () => openSettingsDialog({ onChange: () => shelf.render() }),
    installPrompt: async () => { if (!deferredInstall) return; deferredInstall.prompt(); await deferredInstall.userChoice.catch(() => {}); deferredInstall = null; shelf.setInstallable(false); },
  });
  const reader = new ReaderView(readerEl, {
    goShelf: () => { if (history.state && history.state.fromShelf) history.back(); else { history.replaceState(null, '', '#/'); route(); } },
    onProgress: (info) => syncMgr.onProgress(info),
    fetchRemoteProgress: (bid) => syncMgr.fetchRemoteProgress(bid),
    now: () => Date.now(),
    skewMs: () => (syncMgr.engine ? syncMgr.engine.skewMs() : 0),
  });

  async function route() {
    const r = routeOf();
    if (r.name === 'read') {
      shelfEl.hidden = true;
      readerEl.hidden = false;
      await reader.open(r.id);
    } else {
      reader.close();
      syncMgr.flush();
      readerEl.hidden = true;
      shelfEl.hidden = false;
      shelf.render();
    }
  }
  window.addEventListener('popstate', route);

  window.addEventListener('beforeinstallprompt', (e) => { e.preventDefault(); deferredInstall = e; shelf.setInstallable(true); });
  window.addEventListener('appinstalled', () => { deferredInstall = null; shelf.setInstallable(false); });

  await route();
  if (storageMode() === 'memory') toast('浏览器禁用了本地存储：书籍只在本次会话有效', { ms: 8000 });

  // sync: at start, when the tab comes back, and a flush when it goes away
  const refresh = async () => { if (!syncMgr.enabled()) return; await syncMgr.syncNow({ quiet: true }); if (!shelfEl.hidden) shelf.render(); };
  refresh();
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') syncMgr.flush();
    else if (Date.now() - (syncMgr.status.at || 0) > 60000) refresh();
  });
  window.addEventListener('online', refresh);

  if ('serviceWorker' in navigator && (location.protocol === 'https:' || ['localhost', '127.0.0.1'].includes(location.hostname))) {
    startUpdates({
      isBusy: () => (reader.tts && reader.tts.state !== 'idle' ? '正在朗读' : shelf.importing ? '正在导入书籍' : null),
      prepareReload: async () => { await reader.saveNow(); syncMgr.flush(); await new Promise((r) => setTimeout(r, 400)); },
    }).catch((e) => console.warn('[sw]', e));
  }
  window.__pyreader = { shelf, reader, syncMgr };   // handy for debugging / smoke tests
}

main().catch((e) => {
  console.error(e);
  document.body.prepend(Object.assign(document.createElement('pre'), { textContent: '启动失败：' + (e && e.stack || e), style: 'padding:16px;white-space:pre-wrap' }));
});
