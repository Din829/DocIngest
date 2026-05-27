<#
.SYNOPSIS
    Install DocIngest's system binary dependencies on Windows.

.DESCRIPTION
    DocIngest cannot install system binaries via pip. This script wraps
    winget for the standard tools and handles poppler manually (no
    official winget package — downloaded from the maintained
    poppler-windows GitHub release).

    Required (always installed):
        LibreOffice  — Office formats → Vision enrichment
        ffmpeg       — audio/video processing (includes ffprobe)
        poppler      — pdf2image's fast PDF→image path

    Recommended (default profile installs these too):
        exiftool     — file metadata hook
        Node.js      — JS runtime for yt-dlp (YouTube etc.)
        yt-dlp       — URL ingestion (installed via pip, more up-to-date)

    -WithOcr (extra step):
        pip install onnxruntime rapidocr, then warm the model
        cache so non-admin runs don't hit PermissionError at first PDF.

.PARAMETER Minimal
    Install only the required set (skip exiftool / Node.js / yt-dlp).

.PARAMETER WithOcr
    Additionally install onnxruntime + rapidocr and warm
    the model cache.

.PARAMETER DryRun
    Print the commands that would run, don't execute them.

.EXAMPLE
    .\scripts\install_system_deps.ps1                 # required + recommended
    .\scripts\install_system_deps.ps1 -Minimal        # required only
    .\scripts\install_system_deps.ps1 -WithOcr        # plus OCR stack
    .\scripts\install_system_deps.ps1 -DryRun         # preview

.NOTES
    Requires Windows 10 1709+ (for winget). On older Windows, install
    each tool manually from the URLs printed by the script.
#>

[CmdletBinding()]
param(
    [switch]$Minimal,
    [switch]$WithOcr,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Invoke-OrPreview {
    param([string]$Description, [string]$Command)
    Write-Host ">> $Description"
    Write-Host "   $Command" -ForegroundColor DarkGray
    if (-not $DryRun) {
        # Use Invoke-Expression so the $Command string is evaluated as a real command line.
        Invoke-Expression $Command
        if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne $null) {
            Write-Host "   warning: command exited with code $LASTEXITCODE" -ForegroundColor Yellow
        }
    }
}

function Test-Winget {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "error: winget not found." -ForegroundColor Red
        Write-Host "  Install from Microsoft Store: https://aka.ms/getwinget" -ForegroundColor Yellow
        Write-Host "  Or manually install LibreOffice / ffmpeg / exiftool / Node.js." -ForegroundColor Yellow
        exit 1
    }
}

function Install-WingetPkg {
    param([string]$Id, [string]$DisplayName)
    Invoke-OrPreview -Description "winget: $DisplayName" `
        -Command "winget install --id $Id --silent --accept-source-agreements --accept-package-agreements"
}

# ---------------------------------------------------------------------------
# Poppler (no official winget package — manual install from GitHub release)
# ---------------------------------------------------------------------------

function Install-Poppler {
    $popplerRoot = Join-Path $env:LOCALAPPDATA "poppler"
    $popplerBin  = Join-Path $popplerRoot "Library\bin"

    if (Test-Path (Join-Path $popplerBin "pdftoppm.exe")) {
        Write-Host ">> poppler: already installed at $popplerBin"
        $env:Path = "$popplerBin;$env:Path"
        return
    }

    Write-Host ">> poppler: downloading from oschwartz10612/poppler-windows"

    if ($DryRun) {
        Write-Host "   (dry-run) would download latest release, extract to $popplerRoot, add bin to PATH" -ForegroundColor DarkGray
        return
    }

    # Resolve latest release via GitHub API
    try {
        $api = "https://api.github.com/repos/oschwartz10612/poppler-windows/releases/latest"
        $release = Invoke-RestMethod -Uri $api -UseBasicParsing
        $asset = $release.assets | Where-Object { $_.name -like "Release-*.zip" } | Select-Object -First 1
        if (-not $asset) {
            throw "no Release-*.zip asset found in latest release"
        }
        $url = $asset.browser_download_url
    } catch {
        Write-Host "   error: failed to query GitHub for poppler release: $_" -ForegroundColor Red
        Write-Host "   Fallback: manually download from https://github.com/oschwartz10612/poppler-windows/releases" -ForegroundColor Yellow
        return
    }

    $tmpZip = Join-Path $env:TEMP "poppler-windows.zip"
    Write-Host "   downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $tmpZip -UseBasicParsing

    # Clean slate so an old broken install doesn't shadow the new one
    if (Test-Path $popplerRoot) { Remove-Item -Recurse -Force $popplerRoot }
    New-Item -ItemType Directory -Force -Path $popplerRoot | Out-Null

    Expand-Archive -Path $tmpZip -DestinationPath $popplerRoot -Force
    Remove-Item $tmpZip

    # Find the actual bin/ inside the extracted tree (release zip nests one level)
    $foundBin = Get-ChildItem -Path $popplerRoot -Recurse -Filter "pdftoppm.exe" | Select-Object -First 1
    if (-not $foundBin) {
        Write-Host "   error: pdftoppm.exe not found after extraction" -ForegroundColor Red
        return
    }
    $popplerBin = $foundBin.DirectoryName
    Write-Host "   installed to $popplerBin"

    # Add to user PATH (persistent) AND current session PATH (so verify_deps sees it)
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -notlike "*$popplerBin*") {
        [Environment]::SetEnvironmentVariable("Path", "$userPath;$popplerBin", "User")
        Write-Host "   added to user PATH (new shells will pick it up)"
    }
    $env:Path = "$popplerBin;$env:Path"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

Write-Host "=== DocIngest system deps install (Windows) ==="
Write-Host "Profile:        $(if ($Minimal) {'minimal'} else {'default'})"
Write-Host "With OCR stack: $WithOcr"
Write-Host "Dry-run:        $DryRun"
Write-Host ""

Test-Winget

# Required
Install-WingetPkg -Id "TheDocumentFoundation.LibreOffice" -DisplayName "LibreOffice"
Install-WingetPkg -Id "Gyan.FFmpeg"                       -DisplayName "ffmpeg (includes ffprobe)"
Install-Poppler

# Recommended
if (-not $Minimal) {
    Install-WingetPkg -Id "OliverBetz.ExifTool" -DisplayName "ExifTool"
    Install-WingetPkg -Id "OpenJS.NodeJS"       -DisplayName "Node.js (yt-dlp JS runtime)"
    Invoke-OrPreview -Description "pip: yt-dlp" -Command "pip install --upgrade yt-dlp"
}

# OCR stack
if ($WithOcr) {
    Invoke-OrPreview -Description "pip: onnxruntime + rapidocr" `
        -Command "pip install onnxruntime rapidocr"
    # Warm the model cache. Failure here is non-fatal — caller can retry later.
    Invoke-OrPreview -Description "warm rapidocr model cache" `
        -Command "python -c `"from rapidocr import RapidOCR; RapidOCR()`""
}

Write-Host ""
Write-Host "✅ Done. Next:" -ForegroundColor Green
Write-Host "   python scripts\verify_deps.py        # confirm everything is reachable"
Write-Host ""
Write-Host "Note: if you opened this shell BEFORE Node.js / poppler were installed," -ForegroundColor Yellow
Write-Host "      open a NEW PowerShell so PATH picks up the new entries."          -ForegroundColor Yellow
