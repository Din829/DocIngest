# Build the DocIngest GUI into a double-click Windows exe (onedir + zip).
#
# Pipeline (each stage skippable for fast iteration):
#   1. Stage assets    — copy LibreOffice / ffmpeg / ffprobe / poppler /
#                        exiftool from THIS machine into packaging/build/bins
#   2. Stage models    — pre-download docling layout/tableformer/rapidocr
#                        models into packaging/build/models (offline exe)
#   3. PyInstaller     — Python side ONLY → packaging/build/pyi_dist
#   3.5 Assemble       — robocopy /MIR pyi_dist + bins + models → dist.
#                        INCREMENTAL: unchanged files are skipped, so a
#                        code-only rebuild never re-copies the ~1.7GB assets.
#   4. Smoke gate      — packaging/smoke_test.py runs the dependency matrix
#                        INSIDE the built exe; non-zero exit fails the build
#   5. Zip             — Compress-Archive for distribution
#
# Prerequisites: this machine must have the full dev setup working
# (install_python_deps.ps1 + install_system_deps.ps1 + verify_deps.py green),
# plus `pip install pyinstaller`.
#
# Usage:
#   .\packaging\build_exe.ps1                 # full pipeline (first build)
#   .\packaging\build_exe.ps1 -SkipAssets -SkipModels -SkipZip
#                                             # DAILY LOOP: source changed →
#                                             # rebuild python + incremental
#                                             # assemble + smoke gate
#   .\packaging\build_exe.ps1 -SkipZip        # iterate without the slow zip

param(
    [switch]$SkipAssets,
    [switch]$SkipModels,
    [switch]$SkipBuild,
    [switch]$SkipSmoke,
    [switch]$SkipZip
)
$ErrorActionPreference = "Stop"

$PackagingDir = $PSScriptRoot
$RepoRoot = Split-Path -Parent $PackagingDir
$BuildDir = Join-Path $PackagingDir "build"
$BinsDir = Join-Path $BuildDir "bins"
$ModelsDir = Join-Path $BuildDir "models"
$DistDir = Join-Path $PackagingDir "dist"

function Find-Binary([string]$Name) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

# ---------------------------------------------------------------------------
# Stage 1: system binaries from this machine
# ---------------------------------------------------------------------------
if (-not $SkipAssets) {
    Write-Host "=== Stage 1: staging system binaries ==="
    New-Item -ItemType Directory -Force $BinsDir | Out-Null

    # LibreOffice — copy the whole installed tree (program/ + share/ are both
    # required; soffice refuses to start without share/). ~1GB.
    $loRoot = Join-Path $env:ProgramFiles "LibreOffice"
    if (-not (Test-Path (Join-Path $loRoot "program\soffice.exe"))) {
        throw "LibreOffice not found at $loRoot - install it first (install_system_deps.ps1)."
    }
    Write-Host "  LibreOffice: $loRoot -> bins\LibreOffice (robocopy, ~1GB)"
    robocopy $loRoot (Join-Path $BinsDir "LibreOffice") /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy LibreOffice failed ($LASTEXITCODE)" }

    # ffmpeg + ffprobe — single static exes on PATH (winget/gyan builds).
    foreach ($tool in "ffmpeg", "ffprobe") {
        $src = Find-Binary $tool
        if ($src) {
            Copy-Item $src (Join-Path $BinsDir "$tool.exe") -Force
            Write-Host "  ${tool}: $src"
        } else {
            Write-Warning "$tool not found on PATH - exe will degrade (no $tool)."
        }
    }

    # poppler — pdf2image needs the whole bin dir (pdftoppm + its DLLs).
    $pdftoppm = Find-Binary "pdftoppm"
    if ($pdftoppm) {
        $popplerBin = Split-Path -Parent $pdftoppm
        Write-Host "  poppler: $popplerBin -> bins\poppler"
        robocopy $popplerBin (Join-Path $BinsDir "poppler") /E /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "robocopy poppler failed" }
    } else {
        Write-Warning "poppler (pdftoppm) not found - fast PDF rendering degrades to pymupdf."
    }

    # exiftool — optional single exe.
    $exiftool = Find-Binary "exiftool"
    if ($exiftool) {
        Copy-Item $exiftool (Join-Path $BinsDir "exiftool.exe") -Force
        Write-Host "  exiftool: $exiftool"
    } else {
        Write-Warning "exiftool not found - metadata hook degrades (optional feature)."
    }

    # Window icon: Doclogo.png -> .ico (PyInstaller wants .ico).
    $py = @"
from PIL import Image
from pathlib import Path
img = Image.open(r'$RepoRoot\Doclogo.png').convert('RGBA')
img.save(r'$BuildDir\docingest.ico', sizes=[(16,16),(32,32),(48,48),(256,256)])
print('icon written')
"@
    python -c $py
}

# ---------------------------------------------------------------------------
# Stage 2: docling models (the offline-exe requirement)
# ---------------------------------------------------------------------------
if (-not $SkipModels) {
    Write-Host "=== Stage 2: pre-downloading docling models ==="
    New-Item -ItemType Directory -Force $ModelsDir | Out-Null
    $py = @"
from pathlib import Path
from docling.utils.model_downloader import download_models
# layout + tableformer: the PDF pipeline core. rapidocr: do_ocr defaults ON.
# code_formula / picture_classifier: their pipeline switches default OFF in
# DocIngest, so they are not shipped.
download_models(
    output_dir=Path(r'$ModelsDir'),
    with_layout=True, with_tableformer=True, with_rapidocr=True,
    with_code_formula=False, with_picture_classifier=False,
    progress=True,
)
print('models staged')
"@
    python -c $py
    if ($LASTEXITCODE -ne 0) { throw "model download failed" }
}

# ---------------------------------------------------------------------------
# Stage 3: PyInstaller — Python side only, into an intermediate dir.
# Final dist is ASSEMBLED in 3.5 so the big assets never pass through
# PyInstaller (its --noconfirm wipes its own output every run; keeping
# assets out of that path is what makes rebuilds incremental).
# ---------------------------------------------------------------------------
$PyiDist = Join-Path $BuildDir "pyi_dist"
if (-not $SkipBuild) {
    Write-Host "=== Stage 3: PyInstaller (python side -> pyi_dist) ==="
    python -m PyInstaller --noconfirm `
        --distpath $PyiDist `
        --workpath (Join-Path $BuildDir "pyi") `
        (Join-Path $PackagingDir "docingest_gui.spec")
    if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed" }

    # VC-runtime fix (root cause found via the smoke gate): some dependency
    # ships OLD msvcp140/vcruntime140 (14.36) which PyInstaller drops into
    # _internal\ — but torch's c10.dll and onnxruntime need a newer runtime
    # (their DLL init fails with [WinError 1114] under 14.36). On a normal
    # install they load the up-to-date System32 copy; inside the bundle the
    # _internal copy wins. Fix: overwrite with this machine's System32
    # version (= the version the source tree was verified against).
    $InternalDir = Join-Path (Join-Path $PyiDist "DocIngest") "_internal"
    foreach ($dll in "msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll") {
        $sysDll = "C:\Windows\System32\$dll"
        $bundled = Join-Path $InternalDir $dll
        if ((Test-Path $sysDll) -and (Test-Path $bundled)) {
            $sysVer = [version](Get-Item $sysDll).VersionInfo.FileVersion
            $bunVer = [version](Get-Item $bundled).VersionInfo.FileVersion
            if ($sysVer -gt $bunVer) {
                Copy-Item $sysDll $bundled -Force
                Write-Host "  vc-runtime fix: $dll $bunVer -> $sysVer"
            }
        }
    }
}

# ---------------------------------------------------------------------------
# Stage 3.5: Assemble dist (always runs; robocopy /MIR = incremental).
#   python side : pyi_dist/DocIngest -> dist/DocIngest   (assets excluded)
#   binaries    : build/bins         -> dist/.../_internal/_bundled_bin
#   models      : build/models       -> dist/.../_internal/_bundled_models
# _internal because onedir's sys._MEIPASS points there — identical runtime
# layout to shipping these as spec datas, minus the per-build re-copy.
# ---------------------------------------------------------------------------
Write-Host "=== Stage 3.5: assemble dist (incremental) ==="
$AppDir = Join-Path $DistDir "DocIngest"
$Internal = Join-Path $AppDir "_internal"
if (-not (Test-Path (Join-Path $PyiDist "DocIngest"))) {
    throw "pyi_dist missing - run without -SkipBuild at least once."
}
robocopy (Join-Path $PyiDist "DocIngest") $AppDir /MIR /XD "_bundled_bin" "_bundled_models" /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "assemble: python-side sync failed ($LASTEXITCODE)" }
robocopy $BinsDir (Join-Path $Internal "_bundled_bin") /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "assemble: bins sync failed ($LASTEXITCODE)" }
robocopy $ModelsDir (Join-Path $Internal "_bundled_models") /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "assemble: models sync failed ($LASTEXITCODE)" }
$global:LASTEXITCODE = 0

# ---------------------------------------------------------------------------
# Stage 4: smoke gate (the completeness guarantee)
# ---------------------------------------------------------------------------
if (-not $SkipSmoke) {
    Write-Host "=== Stage 4: smoke gate ==="
    python (Join-Path $PackagingDir "smoke_test.py") (Join-Path $DistDir "DocIngest")
    if ($LASTEXITCODE -ne 0) { throw "SMOKE GATE FAILED - the exe is incomplete, do NOT ship it." }
}

# ---------------------------------------------------------------------------
# Stage 5: zip for distribution
# ---------------------------------------------------------------------------
if (-not $SkipZip) {
    Write-Host "=== Stage 5: zip ==="
    $zip = Join-Path $DistDir "DocIngest-win64.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path (Join-Path $DistDir "DocIngest") -DestinationPath $zip
    Write-Host "  -> $zip"
}

Write-Host ""
Write-Host "Build complete: $DistDir\DocIngest\DocIngest.exe"
Write-Host "Final check before shipping: run the Windows Sandbox offline test"
Write-Host "(packaging\sandbox_test.wsb) - see packaging\README.md."
