# DocIngest deployment scripts

Three layers, three scripts, one gate. Mix freely depending on the target.

| Layer | What it does | Files |
|---|---|---|
| **Python deps** | Install `pip` packages with the right extras combo | [`install_python_deps.sh`](install_python_deps.sh) |
| **System deps** | Install system binaries (LibreOffice, ffmpeg, poppler, ...) | [`install_system_deps.sh`](install_system_deps.sh) (Linux/macOS), [`install_system_deps.ps1`](install_system_deps.ps1) (Windows) |
| **Gate** | Verify everything is installed; exit non-zero if not | [`verify_deps.py`](verify_deps.py) |

For a full deployment template that wires all three together, see [`../Dockerfile.example`](../Dockerfile.example).

---

## 30-second migration / new-machine setup

**Linux / macOS:**
```bash
./scripts/install_python_deps.sh
./scripts/install_system_deps.sh
python scripts/verify_deps.py            # gate — non-zero if anything required is missing
```

**Windows:**
```powershell
.\scripts\install_python_deps.sh         # works in git-bash / WSL — or run the pip line manually
.\scripts\install_system_deps.ps1
python scripts\verify_deps.py
```

That's it. Three lines, full DocIngest install with every system binary the code actually uses.

---

## 5 common scenarios — pick the right combo

| Scenario | Python profile | System profile | Verify profile |
|---|---|---|---|
| **Dev machine, full features** | default | default | default |
| **Pure RAG, no media/URL/OCR** | `--no-graph` | `--minimal` | `--require office` |
| **Production container, all features** | default | default + `--with-ocr` | `--strict` |
| **GraphRAG with local embeddings (2GB torch)** | `--full` | default + `--with-ocr` | `--strict` |
| **CI: only verify, don't install** | (skip) | (skip) | `--minimal` or `--strict --json` |

---

## Script reference

### `install_python_deps.sh`

Wraps `pip install -e .[...]` with a sane default extras combo.

| Flag | What changes |
|---|---|
| (none) | core + `[mcp,audio,nlp,graph,gui]` + `pdf2image PyExifTool magika` |
| `--minimal` | core only (still adds `pdf2image PyExifTool magika` — lazy imports) |
| `--no-graph` | core + `[mcp,audio,nlp,gui]` (drops GraphRAG layer) |
| `--full` | core + everything + `[graph-local]` (+~2GB torch) |
| `--dry-run` | print the pip command, don't run |

Why `pdf2image`, `PyExifTool` and `magika` are always installed: the DocIngest source `import`s them lazily but they are **not declared in pyproject.toml** — installing them here closes the gap.

### `install_system_deps.sh` (Linux / macOS)

Auto-detects apt / dnf / yum / pacman / zypper / brew. Falls back to a clear error message on unsupported package managers.

| Flag | What changes |
|---|---|
| (none) | required (libreoffice, ffmpeg, poppler) + recommended (exiftool, nodejs, yt-dlp) |
| `--minimal` | required only |
| `--with-ocr` | + `pip install onnxruntime rapidocr` + warm the model cache |
| `--dry-run` | print every command, don't execute |

`--with-ocr` is the **§4.11 踩雷 fix**: rapidocr lazily downloads `.onnx` model files into its package directory at first use. In a non-root container that's a read-only path → `PermissionError` → silent PDF failure. Pre-downloading as root in this script (or in a Dockerfile RUN as root) sidesteps that.

### `install_system_deps.ps1` (Windows)

Uses `winget` for everything except **poppler** — there is no official winget package, so the script downloads the latest release from [oschwartz10612/poppler-windows](https://github.com/oschwartz10612/poppler-windows) and adds its `bin/` to your user PATH.

| Flag | What changes |
|---|---|
| (none) | required (LibreOffice, ffmpeg, poppler) + recommended (ExifTool, Node.js, yt-dlp) |
| `-Minimal` | required only |
| `-WithOcr` | + `pip install onnxruntime rapidocr` + warm the model cache |
| `-DryRun` | print every command, don't execute |

**After this script runs, open a new PowerShell** so PATH picks up the new entries (poppler / Node.js). Otherwise `verify_deps.py` may still report them as missing.

### `verify_deps.py`

The gate. Checks Python core + tools + Python extras + the five hidden deps the built-in `docingest doctor` misses (poppler, JS runtime, pdf2image, pyexiftool, pywebview).

| Flag | What changes |
|---|---|
| (none) | core + `office` + `media` groups required; rest = warning |
| `--minimal` | only the 14 core Python packages required |
| `--strict` | every feature group required (most paranoid) |
| `--require <a,b,c>` | custom required groups (see table below) |
| `--json` | machine-readable output (for CI) |
| `--no-warnings` | hide the warnings section (still shows errors) |

**Feature groups:**

| Group | What's in it |
|---|---|
| `office` | LibreOffice |
| `media` | ffmpeg + ffprobe |
| `pdf-fast` | poppler + pdf2image (fast PDF→image path) |
| `url` | yt-dlp + JS runtime (node/deno/bun) |
| `ocr` | onnxruntime + rapidocr |
| `metadata` | exiftool + pyexiftool |
| `mcp` | fastmcp |
| `audio` | dashscope (Qwen3-ASR) |
| `nlp` | sudachipy (Japanese keyword extraction) |
| `graph` | lightrag-hku + nest_asyncio |
| `detect` | magika (content-based file type detection) |
| `gui` | pywebview (desktop GUI shell) |

**Exit codes:**
- `0` — all required deps satisfied
- `1` — at least one required dep missing
- `2` — bad CLI arguments

---

## How is this different from `docingest doctor`?

| | `docingest doctor` | `verify_deps.py` |
|---|---|---|
| Pretty table output | ✅ | ❌ (plain text) |
| Returns non-zero when missing | **❌ always 0** | ✅ |
| Detects poppler | ❌ | ✅ |
| Detects JS runtime (yt-dlp) | ❌ | ✅ |
| Detects pdf2image / pyexiftool | ❌ | ✅ |
| Custom required-set | ❌ | ✅ (`--require`) |
| JSON output for CI | ❌ | ✅ |

Use `docingest doctor` for human inspection, `verify_deps.py` for automation.

---

## Troubleshooting

**`verify_deps.py` says `core pkg [X] missing` but I just installed DocIngest.**
You're probably running the script from a different Python environment than where you installed DocIngest. Run `which python && which pip` and confirm they're the same env.

**`install_system_deps.sh` exits with "no supported package manager found".**
Your distro isn't in the auto-detection list. The script prints the logical package names — install them manually with your distro's tool, then re-run `verify_deps.py`.

**Poppler still not detected after running `install_system_deps.ps1`.**
Open a new PowerShell window. PATH changes don't propagate to the shell that triggered them.

**Dockerfile build fails at `verify_deps.py`.**
This is the gate working as designed. Read the printed `MISSING` lines — each one tells you which package and how to install it.

**On a non-root Docker container, PDFs fail silently with "successful=1".**
You forgot to pre-download the rapidocr models. Re-build with `--build-arg INSTALL_OCR=1` (the default in `Dockerfile.example`) or add to your own Dockerfile (as root, before USER switch):
```dockerfile
RUN pip install onnxruntime rapidocr && \
    python -c "from rapidocr import RapidOCR; RapidOCR()"
```
