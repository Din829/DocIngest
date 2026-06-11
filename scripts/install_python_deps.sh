#!/usr/bin/env bash
# Install DocIngest's Python dependencies.
#
# This wraps `pip install -e .[...]` with a sane default set of extras so you
# don't have to remember which combo to install on a new machine.
#
# Usage:
#   ./scripts/install_python_deps.sh             # core + mcp,audio,nlp,graph,gui
#   ./scripts/install_python_deps.sh --minimal   # core only
#   ./scripts/install_python_deps.sh --full      # also adds graph-local (~2GB torch)
#   ./scripts/install_python_deps.sh --no-graph  # drop graph layer
#   ./scripts/install_python_deps.sh --dry-run   # print the pip command, don't run
#
# Tip: also install these on top (not in pyproject):
#   pip install pdf2image PyExifTool magika
# They are imported lazily by DocIngest. Run scripts/verify_deps.py to see
# whether anything is missing.

set -euo pipefail

# Resolve repo root from this script's location so it works regardless of CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PROFILE="default"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --minimal)  PROFILE="minimal"; shift ;;
        --full)     PROFILE="full"; shift ;;
        --no-graph) PROFILE="no-graph"; shift ;;
        --dry-run)  DRY_RUN=1; shift ;;
        -h|--help)
            sed -n '2,18p' "$0"
            exit 0
            ;;
        *)
            echo "error: unknown arg '$1'" >&2
            exit 2
            ;;
    esac
done

# gui (pywebview, lightweight) is part of every non-minimal profile so the
# desktop GUI works out of the box after this script.
case "$PROFILE" in
    minimal)   EXTRAS="" ;;
    default)   EXTRAS="[mcp,audio,nlp,graph,gui]" ;;
    no-graph)  EXTRAS="[mcp,audio,nlp,gui]" ;;
    full)      EXTRAS="[mcp,audio,nlp,graph,graph-local,gui]" ;;
esac

# CPU-only torch — installed BEFORE docling. docling pulls torch transitively
# (its DocumentConverter imports torch at module load); the default Linux PyPI
# wheel is the ~5.6GB CUDA build, of which DocIngest uses NONE (CPU inference
# only). Forcing the CPU build here means the later `pip install -e .` sees
# torch already satisfied and won't drag the CUDA wheel. Verified: docling adds
# no nvidia/cuda packages on top of a pre-installed CPU torch.
TORCH_CMD="pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu"
CMD="pip install -e \"${REPO_ROOT}${EXTRAS}\""
EXTRA_CMD="pip install pdf2image PyExifTool magika"

echo "=== DocIngest Python deps install (profile: $PROFILE) ==="
echo "Step 1 (CPU-only torch, before docling): $TORCH_CMD"
echo "Step 2: $CMD"
echo "Step 3 (lazy imports, not in pyproject): $EXTRA_CMD"

if [[ $DRY_RUN -eq 1 ]]; then
    echo "(dry-run: nothing installed)"
    exit 0
fi

# Drop any pre-existing (possibly CUDA) torch first — pip won't swap an
# already-satisfied torch, so without this the CPU install below is a no-op
# on machines that already have the CUDA build.
pip uninstall -y torch torchvision 2>/dev/null || true

# shellcheck disable=SC2086
eval $TORCH_CMD
eval $CMD
eval $EXTRA_CMD

echo
echo "✅ Python deps installed. Next:"
echo "   - Install system binaries:  scripts/install_system_deps.sh   (Linux/macOS)"
echo "                              scripts/install_system_deps.ps1   (Windows)"
echo "   - Verify everything:        python scripts/verify_deps.py"
