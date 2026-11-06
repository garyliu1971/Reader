# Vendored third-party code

Downloaded once from jsDelivr (npm mirror) at pinned versions and committed unmodified. They are untrusted data
until reviewed: nothing here is executed outside the browser, and the app never loads code from a CDN at runtime.
To update: download the new version, re-check the license, update this table, bump `CACHE` in `../sw.js`.

| File | Package / version | Source URL | License | SHA-256 |
|---|---|---|---|---|
| `jszip.min.js` | jszip 3.10.1 | https://cdn.jsdelivr.net/npm/jszip@3.10.1/dist/jszip.min.js | MIT or GPL-3.0 (we use MIT) - `LICENSE.jszip` | `acc7e41455a80765b5fd9c7ee1b8078a6d160bbbca455aeae854de65c947d59e` |
| `marked.esm.js` | marked 15.0.12 | https://cdn.jsdelivr.net/npm/marked@15.0.12/lib/marked.esm.js | MIT - `LICENSE.marked` | `29e25cdf1a06075c90e9885af06ddd23fb22a5424ed23a61dda986c73a8af641` |
| `pdf.min.js` | pdfjs-dist 4.10.38 (`build/pdf.min.mjs`) | https://cdn.jsdelivr.net/npm/pdfjs-dist@4.10.38/build/pdf.min.mjs | Apache-2.0 - `LICENSE.pdfjs` | `27fc2a057a00f92a4334ad06e17dbd7259912954e9fb7f76400bcca5fd190a9c` |
| `pdf.worker.min.js` | pdfjs-dist 4.10.38 (`build/pdf.worker.min.mjs`) | https://cdn.jsdelivr.net/npm/pdfjs-dist@4.10.38/build/pdf.worker.min.mjs | Apache-2.0 | `1baa1844c89c80a5b2797c916e75ab29254be46d8e9cb53cb6364d7aad84be36` |
| `foliate-mobi.js` | foliate-js `mobi.js` @ 9a676796f728347b0127162079c13d23a0f7fee8 (unmodified) | https://github.com/johnfactotum/foliate-js/blob/9a676796f728347b0127162079c13d23a0f7fee8/mobi.js | MIT - `LICENSE.foliate` | `016526df13a9f11ee6671a8c74346e1cf41fe6bdfe50165fdcbf744f015694f3` |

Notes

* The two pdf.js files are ES modules; they were renamed from `.mjs` to `.js` (content unchanged) because several static
  hosts (Azure Blob static websites, some Windows registries behind `python -m http.server`) do not serve `.mjs` with a
  JavaScript MIME type, which makes the browser refuse to load modules.
* JSZip is loaded lazily as a classic script (global `JSZip`) the first time an EPUB is opened; pdf.js lazily on the first PDF;
  marked lazily on the first Markdown file. They are not needed for TXT reading.
* EPUB parsing uses JSZip directly instead of epub.js: we need our own pagination, text-offset anchors and TOC handling,
  and epub.js's iframe-per-chapter rendition cannot give stable offsets across reflows.
* `foliate-mobi.js` (MOBI/AZW3 parser) is lazily imported as an ES module on the first MOBI. It does NOT check DRM, truncation, KFX or Print Replica: `js/parse/mobicheck.js` does that before it is called, and its `section.load()` HTML is never displayed (only `createDocument()` through our sanitizer).
* Both ".min" files are exactly what the package publishes; we do not use pdf.js's viewer, text layer or annotation layer in v1.
