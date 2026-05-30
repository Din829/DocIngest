# Install DocIngest's Python dependencies (Windows / PowerShell).
# Mirrors scripts/install_python_deps.sh.
#
# Usage:
#   .\scripts\install_python_deps.ps1             # core + mcp,audio,nlp,graph
#   .\scripts\install_python_deps.ps1 -Minimal    # core only
#   .\scripts\install_python_deps.ps1 -Full       # also adds graph-local (~2GB)
#   .\scripts\install_python_deps.ps1 -NoGraph    # drop graph layer
#   .\scripts\install_python_deps.ps1 -DryRun     # print the commands, don't run
#
# Tip: also `pip install pdf2image PyExifTool` (lazy imports, not in pyproject).
# Run `python scripts\verify_deps.py` to see whether anything is missing.

param(
    [switch]$Minimal,
    [switch]$Full,
    [switch]$NoGraph,
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"

# Repo root = parent of this script's directory (works regardless of CWD).
$RepoRoot = Split-Path -Parent $PSScriptRoot

if     ($Minimal) { $Extras = "" }
elseif ($Full)    { $Extras = "[mcp,audio,nlp,graph,graph-local]" }
elseif ($NoGraph) { $Extras = "[mcp,audio,nlp]" }
else              { $Extras = "[mcp,audio,nlp,graph]" }

# CPU-only torch — installed BEFORE docling (same rationale as the .sh script:
# docling pulls torch transitively and would otherwise drag the ~5.6GB CUDA
# wheel; DocIngest is CPU-only). Windows PyPI torch is often CPU already, but
# --index-url makes it deterministic across every machine / cloud target.
$TorchCmd = "pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu"
$Cmd      = "pip install -e `"$RepoRoot$Extras`""
$ExtraCmd = "pip install pdf2image PyExifTool"

Write-Host "=== DocIngest Python deps install (Windows) ==="
Write-Host "Step 1 (CPU-only torch, before docling): $TorchCmd"
Write-Host "Step 2: $Cmd"
Write-Host "Step 3 (lazy imports, not in pyproject): $ExtraCmd"

if ($DryRun) {
    Write-Host "(dry-run: nothing installed)"
    exit 0
}

# Drop any pre-existing (possibly CUDA) torch first — pip won't swap an
# already-satisfied torch, so on machines with the CUDA build the CPU install
# below would otherwise be a no-op. Ignore failure when torch isn't installed.
pip uninstall -y torch torchvision 2>$null

Invoke-Expression $TorchCmd
Invoke-Expression $Cmd
Invoke-Expression $ExtraCmd

Write-Host ""
Write-Host "Python deps installed. Next:"
Write-Host "  - System binaries:  scripts\install_system_deps.ps1"
Write-Host "  - Verify:           python scripts\verify_deps.py"
