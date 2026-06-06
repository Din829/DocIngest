# Vendored third-party assets — provenance & licenses

All of `web/vendor/` is third-party code/fonts copied in so the GUI runs
**fully offline** (DocIngest ships as an exe; data must not leave the machine).
Nothing here loads from a CDN at runtime. Vendored on **2026-06-06**.

License texts are under `licenses/`. To update any asset: re-download the same
pinned version, replace the file, and bump the version + date here.

## JavaScript libraries

| File | Library | Version | License | Source |
|---|---|---|---|---|
| `lucide.min.js` | Lucide (icons) | 1.17.0 | ISC | npm `lucide` UMD (`dist/umd/lucide.min.js`) |
| `marked.umd.js` | marked (Markdown→HTML) | 18.0.5 | MIT | npm `marked` (`lib/marked.umd.js`) |
| `purify.min.js` | DOMPurify (HTML sanitizer) | 3.4.8 | Apache-2.0 OR MPL-2.0 | npm `dompurify` (`dist/purify.min.js`) |

- Globals exposed (what `app.js` uses): `window.lucide.createIcons()`,
  `window.marked.parse()`, `window.DOMPurify.sanitize()`.
- **Why full lucide, not a subset**: the app references ~37 icons, 14 of them
  in dynamic JS maps (format icons, processing-state icons, chevrons) — a hand
  subset is easy to under-count and silently blanks an icon. The full UMD is
  393KB, negligible next to the fonts. `createIcons()` with no args renders all
  `data-lucide` elements (its default `icons` arg is the full bundled set).

## Fonts (`fonts/`)

Full (un-subsetted) **woff2**, converted from Google Fonts TTF with fontTools
4.56 (`flavor='woff2'`). Single complete file per weight — NOT the
unicode-range-sharded form Google serves browsers — so no missing-glyph risk in
the Markdown preview. ~9.3MB total (vs 22.8MB as TTF). @font-face rules in
`fonts/fonts.css`.

| Files | Family | Weights | License | Copyright |
|---|---|---|---|---|
| `noto-sans-jp-*.woff2` | Noto Sans JP | 400/500/600 | SIL OFL 1.1 | © Google / Noto Project |
| `noto-serif-jp-500.woff2` | Noto Serif JP | 500 | SIL OFL 1.1 | © Google / Noto Project |
| `newsreader-*.woff2` | Newsreader | 400/500 | SIL OFL 1.1 | © Production Type |
| `ibm-plex-mono-*.woff2` | IBM Plex Mono | 400/500 | SIL OFL 1.1 | © IBM Corp. |

OFL 1.1 text: `licenses/OFL-1.1.txt` (the template form; each font ships its own
copyright line above — keep these in any redistribution).

## Note on the original code comments

`index.html` previously said fonts/icons/marked/dompurify load "from CDN in dev,
vendored at packaging." That's now done up-front: they are vendored here and
referenced by local relative path, so dev and packaged builds both run offline.
