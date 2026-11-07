// PDF support (one page per screen; canvas render + an invisible, selectable pdf.js TextLayer over it). pdf.js is loaded lazily on first use.
import { fitScale, layerBox, itemsToText } from './pdftext.js';
let pdfjsPromise = null;
export function getPdfjs() {
  if (!pdfjsPromise) {
    pdfjsPromise = import('../../vendor/pdf.min.js').then((m) => {
      m.GlobalWorkerOptions.workerSrc = new URL('../../vendor/pdf.worker.min.js', import.meta.url).href;
      return m;
    });
  }
  return pdfjsPromise;
}

export async function openPdf(blob) {
  const pdfjs = await getPdfjs();
  const data = new Uint8Array(await blob.arrayBuffer());
  let doc;
  try {
    doc = await pdfjs.getDocument({ data, isEvalSupported: false, enableXfa: false }).promise;
  } catch (e) {
    throw new Error('无法打开 PDF：' + (e && e.message ? e.message : e));
  }
  let title = '';
  let author = '';
  try {
    const m = await doc.getMetadata();
    title = (m.info && m.info.Title) || '';
    author = (m.info && m.info.Author) || '';
  } catch { /* optional */ }
  const outline = [];
  try {
    const ol = await doc.getOutline();
    const walk = async (items, level) => {
      for (const it of items || []) {
        let page = null;
        try {
          let dest = it.dest;
          if (typeof dest === 'string') dest = await doc.getDestination(dest);
          if (Array.isArray(dest) && dest[0]) page = await doc.getPageIndex(dest[0]);
        } catch { /* unresolved destination */ }
        if (page !== null) outline.push({ title: (it.title || '').trim(), s: page, id: '', level });
        if (it.items && outline.length < 2000) await walk(it.items, level + 1);
      }
    };
    await walk(ol, 0);
  } catch { /* no outline */ }
  const n = doc.numPages;
  const sections = Array.from({ length: n }, (_, i) => ({ title: `第 ${i + 1} 页` }));
  let task = null;
  let tl = null;   // current TextLayer
  let seq = 0;     // newest render wins
  return {
    format: 'pdf', title, author, sections, toc: outline.length ? outline : [], lens: Array(n).fill(1), pageCount: n,
    /** render page i into `canvas`, fitting (w,h) CSS pixels; later calls cancel earlier ones.
     *  With `textDiv` an invisible selectable text layer is built over the canvas (same CSS size). */
    async render(i, canvas, w, h, textDiv) {
      const my = ++seq;
      if (task) { try { task.cancel(); } catch { /* ignore */ } }
      if (tl) { try { tl.cancel(); } catch { /* ignore */ } tl = null; }
      const page = await doc.getPage(i + 1);
      if (my !== seq) return;
      const v1 = page.getViewport({ scale: 1 });
      const scale = fitScale(v1.width, v1.height, w, h);
      const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
      const vp = page.getViewport({ scale: scale * dpr });
      canvas.width = Math.floor(vp.width);
      canvas.height = Math.floor(vp.height);
      canvas.style.width = Math.floor(vp.width / dpr) + 'px';
      canvas.style.height = Math.floor(vp.height / dpr) + 'px';
      if (textDiv) textDiv.replaceChildren();
      task = page.render({ canvasContext: canvas.getContext('2d'), viewport: vp });
      try { await task.promise; } catch (e) { if (!(e && e.name === 'RenderingCancelledException')) throw e; return; }
      if (my !== seq || !textDiv) return;
      try {
        const box = layerBox(v1.width, v1.height, scale);
        textDiv.style.width = Math.floor(vp.width / dpr) + 'px';
        textDiv.style.height = Math.floor(vp.height / dpr) + 'px';
        textDiv.style.setProperty('--scale-factor', String(box.scaleFactor));
        const layer = new pdfjs.TextLayer({ textContentSource: page.streamTextContent(), container: textDiv, viewport: page.getViewport({ scale }) });
        tl = layer;
        await layer.render();
      } catch (e) { if (!(e && e.name === 'AbortException')) console.warn('pdf text layer failed', e); }   // the page stays readable without it
    },
    /** plain text of page i (for future search / read-aloud) */
    async pageText(i) {
      const page = await doc.getPage(i + 1);
      const tc = await page.getTextContent();
      return itemsToText(tc.items);
    },
    async cover(maxW = 240) {
      const page = await doc.getPage(1);
      const v1 = page.getViewport({ scale: 1 });
      const vp = page.getViewport({ scale: maxW / v1.width });
      const c = document.createElement('canvas');
      c.width = Math.floor(vp.width); c.height = Math.floor(vp.height);
      await page.render({ canvasContext: c.getContext('2d'), viewport: vp }).promise;
      return new Promise((res) => c.toBlob((b) => res(b), 'image/jpeg', 0.8));
    },
    async loadSection() { throw new Error('PDF has no reflow sections'); },
    async sectionText() { return ''; },
    resolveHref() { return null; },
    close() { try { doc.destroy(); } catch { /* ignore */ } },
  };
}
