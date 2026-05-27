#!/usr/bin/env bash
# Install DocIngest's Python dependencies.
#
# This wraps `pip install -e .[...]` with a sane default set of extras so you
# don't have to remember which combo to install on a new machine.
#
# Usage:
#   ./scripts/install_python_deps.sh             # core + mcp,audio,nlp,graph
#   ./scripts/install_python_deps.sh --minimal   # core only
#   ./scripts/install_python_deps.sh --full      # also adds graph-local (~2GB torch)
#   ./scripts/install_python_deps.sh --no-graph  # drop graph layer
#   ./scripts/install_python_deps.sh --dry-run   # print the pip command, don't run
#
# Tip: also install these on top (not in pyproject):
#   pip install pdf2image PyExifTool
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

case "$PROFILE" in
    minimal)   EXTRAS="" ;;
    default)   EXTRAS="[mcp,audio,nlp,graph]" ;;
    no-graph)  EXTRAS="[mcp,audio,nlp]" ;;
    full)      EXTRAS="[mcp,audio,nlp,graph,graph-local]" ;;
esac

CMD="pip install -e \"${REPO_ROOT}${EXTRAS}\""
EXTRA_CMD="pip install pdf2image PyExifTool"

echo "=== DocIngest Python deps install (profile: $PROFILE) ==="
echo "Step 1: $CMD"
echo "Step 2 (lazy imports, not in pyproject): $EXTRA_CMD"

if [[ $DRY_RUN -eq 1 ]]; then
    echo "(dry-run: nothing installed)"
    exit 0
fi

# shellcheck disable=SC2086
eval $CMD
eval $EXTRA_CMD

echo
echo "✅ Python deps installed. Next:"
echo "   - Install system binaries:  scripts/install_system_deps.sh   (Linux/macOS)"
echo "                              scripts/install_system_deps.ps1   (Windows)"
echo "   - Verify everything:        python scripts/verify_deps.py"
