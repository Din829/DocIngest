# DocIngest exe packaging (Windows, double-click distribution)

Turns the GUI into `dist/DocIngest/DocIngest.exe` — a folder the end user
unzips and double-clicks. **Everything is inside**: Python runtime, all pip
packages, LibreOffice, ffmpeg/ffprobe, poppler, exiftool, and docling's ML
models (offline parsing, no first-run download).

Performance contract: **onedir, no onefile, no UPX** — the exe runs the same
bytecode and native DLLs as a local install; only cold start differs (a few
seconds of first disk read). See `docs/GUI/GUI_DESIGN.md` packaging section
for the design background (incl. the `0xC0000142` SetDllDirectory fix).

## Build

Prerequisites — the build machine IS the dev machine: full install green
(`verify_deps.py`), LibreOffice/ffmpeg/poppler on it, plus:

```powershell
pip install pyinstaller
```

Then one command:

```powershell
.\packaging\build_exe.ps1            # stages assets → models → build → smoke gate → zip
```

| Flag | Use when |
|---|---|
| `-SkipAssets` | bins/ already staged (iterating on code only) |
| `-SkipModels` | models/ already staged |
| `-SkipBuild`  | re-running only the smoke gate |
| `-SkipSmoke`  | NEVER for a shipping build |
| `-SkipZip`    | iterating (zip of ~2GB is slow) |

Output: `packaging/dist/DocIngest/` (~2GB) and `DocIngest-win64.zip`.

## Daily loop: source changed → repackage

```powershell
.\packaging\build_exe.ps1 -SkipAssets -SkipModels -SkipZip
```

This is fast by construction: PyInstaller rebuilds ONLY the Python side
(assets are not spec datas), then the assemble stage robocopy-mirrors
python + bins + models into dist — robocopy skips unchanged files, so the
~1.7GB of LibreOffice/models is synced in seconds after the first build.
Re-stage assets (drop a skip flag) only when you upgrade LibreOffice/ffmpeg
(`-SkipModels` stays) or bump docling and its models (`-SkipAssets` stays).

## How completeness is guaranteed (three gates)

1. **Greedy collection** — `docingest_gui.spec` uses `collect_all` on every
   dynamic-import-heavy package (docling family, litellm, tiktoken, magika,
   rapidocr, pywebview, ...). Over-collection is deliberate.
2. **In-bundle smoke matrix** (build stage 4, automatic) —
   `DocIngest.exe --smoke` runs INSIDE the frozen bundle: critical imports,
   bundled-binary env injection, web assets, doctor, and a real end-to-end
   ingest of generated samples in every offline format (pdf/docx/pptx/xlsx/
   png/md/zip) with Vision disabled. Any miss = non-zero exit = build fails.
   Report: `smoke.log` (relayed to the build console).
3. **Offline sandbox acceptance** (manual, before shipping) — double-click
   `packaging/sandbox_test.wsb`: a throwaway Windows Sandbox with **no
   network and no Python**. Run the smoke matrix + open the window there.
   Passing offline in a bare sandbox = the bundle is truly self-contained.

## Configuring API keys on a target machine

The exe ships with NO keys (verified: the build contains no .env and no key
material). End users have two equivalent options:

1. **GUI settings screen** (環境チェック) — enter keys once; they persist in
   that machine's `~/.docingest/config.yaml`.
2. **A `.env` file next to `DocIngest.exe`** — same format as the dev .env
   (`GEMINI_API_KEY=...`). Loaded explicitly at startup regardless of how
   the exe is launched (double-click, shortcut, another tool).

## What is intentionally NOT bundled

| Thing | Why |
|---|---|
| Node.js (yt-dlp's JS runtime) | URL ingestion needs internet anyway; yt-dlp itself (pip) IS bundled and covers most sites without node |
| Vision / ASR API keys | user-provided, entered in the GUI settings screen |
| WebView2 Runtime | ships with Win10/11; on stripped images the exe shows a message box linking the Microsoft installer (gui_app.py fallback) |
| GraphRAG embedding models | `[graph-local]` is a 2GB opt-in; graph build in the exe uses API embeddings |

## Troubleshooting

- **Smoke gate fails with `import X` FAIL** → add `X` to `collect_all` (or
  `hiddenimports`) in `docingest_gui.spec`, rebuild with `-SkipAssets -SkipModels`.
- **soffice crashes 0xC0000142 in the exe** → should not happen (fixed in
  `binary_finder.run_soffice_convert`); if it reappears, check that fix is
  still on the soffice call path.
- **torch c10.dll / onnxruntime fail with [WinError 1114]** → stale
  msvcp140/vcruntime140 (≤14.36) in `_internal\`; build_exe.ps1 stage 3
  overwrites them with the System32 version automatically. If it reappears,
  check the build machine's VC redist is current.
- **Window doesn't open on a target machine** → likely missing WebView2; the
  message box links the Evergreen installer.
- **AV quarantines the exe** → unsigned binaries trip SmartScreen; code-sign
  for wide distribution, or whitelist for internal use.
