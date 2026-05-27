#!/usr/bin/env bash
# Install DocIngest's system binary dependencies on Linux / macOS.
#
# DocIngest itself does not (and cannot) install system binaries via pip.
# This script picks the right package manager and installs:
#
#   Required:
#     libreoffice  — Office formats → Vision enrichment
#     ffmpeg       — audio/video processing (ffmpeg + ffprobe ship together)
#     poppler-utils — required by pdf2image's fast PDF→image path
#
#   Recommended (default profile installs these too):
#     exiftool     — file metadata hook
#     nodejs       — JS runtime for yt-dlp (YouTube etc.)
#     yt-dlp       — URL ingestion
#
#   --with-ocr (extra step):
#     pip install onnxruntime rapidocr, then warm the model
#     cache so non-root containers don't hit PermissionError at first PDF.
#
# Usage:
#   ./scripts/install_system_deps.sh                 # required + recommended
#   ./scripts/install_system_deps.sh --minimal       # required only
#   ./scripts/install_system_deps.sh --with-ocr      # also OCR stack
#   ./scripts/install_system_deps.sh --dry-run       # print commands, don't run
#
# Supported package managers (auto-detected):
#   apt (Debian/Ubuntu), dnf (Fedora/RHEL), brew (macOS).
# Other distros: edit _MANUAL_HINTS below or use your distro's package names.

set -euo pipefail

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------
PROFILE="default"
WITH_OCR=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --minimal)  PROFILE="minimal"; shift ;;
        --with-ocr) WITH_OCR=1; shift ;;
        --dry-run)  DRY_RUN=1; shift ;;
        -h|--help)
            sed -n '2,30p' "$0"
            exit 0
            ;;
        *)
            echo "error: unknown arg '$1'" >&2
            exit 2
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Detect package manager
# ---------------------------------------------------------------------------
PM=""
SUDO=""

detect_pm() {
    if [[ "$(uname -s)" == "Darwin" ]]; then
        if command -v brew >/dev/null 2>&1; then
            PM="brew"
            return
        fi
        echo "error: brew not found. Install Homebrew first: https://brew.sh" >&2
        exit 1
    fi

    if command -v apt-get >/dev/null 2>&1; then
        PM="apt"
    elif command -v dnf >/dev/null 2>&1; then
        PM="dnf"
    elif command -v yum >/dev/null 2>&1; then
        PM="yum"
    elif command -v pacman >/dev/null 2>&1; then
        PM="pacman"
    elif command -v zypper >/dev/null 2>&1; then
        PM="zypper"
    else
        echo "error: no supported package manager found." >&2
        echo "Manually install: libreoffice ffmpeg poppler-utils exiftool nodejs" >&2
        exit 1
    fi

    if [[ $EUID -ne 0 ]]; then
        SUDO="sudo"
    fi
}

detect_pm

# ---------------------------------------------------------------------------
# Package name maps per package manager
# (different distros use different names for the same thing)
# ---------------------------------------------------------------------------

pkg_name() {
    local logical="$1"
    case "$PM:$logical" in
        # apt (Debian/Ubuntu)
        apt:libreoffice)    echo "libreoffice" ;;
        apt:ffmpeg)         echo "ffmpeg" ;;
        apt:poppler)        echo "poppler-utils" ;;
        apt:exiftool)       echo "libimage-exiftool-perl" ;;
        apt:nodejs)         echo "nodejs" ;;

        # dnf / yum (Fedora/RHEL/CentOS)
        dnf:libreoffice|yum:libreoffice)  echo "libreoffice" ;;
        dnf:ffmpeg|yum:ffmpeg)            echo "ffmpeg" ;;
        dnf:poppler|yum:poppler)          echo "poppler-utils" ;;
        dnf:exiftool|yum:exiftool)        echo "perl-Image-ExifTool" ;;
        dnf:nodejs|yum:nodejs)            echo "nodejs" ;;

        # pacman (Arch)
        pacman:libreoffice) echo "libreoffice-fresh" ;;
        pacman:ffmpeg)      echo "ffmpeg" ;;
        pacman:poppler)     echo "poppler" ;;
        pacman:exiftool)    echo "perl-image-exiftool" ;;
        pacman:nodejs)      echo "nodejs" ;;

        # zypper (openSUSE)
        zypper:libreoffice) echo "libreoffice" ;;
        zypper:ffmpeg)      echo "ffmpeg" ;;
        zypper:poppler)     echo "poppler-tools" ;;
        zypper:exiftool)    echo "exiftool" ;;
        zypper:nodejs)      echo "nodejs" ;;

        # brew (macOS)
        brew:libreoffice)   echo "--cask libreoffice" ;;
        brew:ffmpeg)        echo "ffmpeg" ;;
        brew:poppler)       echo "poppler" ;;
        brew:exiftool)      echo "exiftool" ;;
        brew:nodejs)        echo "node" ;;

        *) echo "" ;;
    esac
}

install_cmd() {
    local pkgs="$1"
    case "$PM" in
        apt)     echo "$SUDO apt-get install -y --no-install-recommends $pkgs" ;;
        dnf)     echo "$SUDO dnf install -y $pkgs" ;;
        yum)     echo "$SUDO yum install -y $pkgs" ;;
        pacman)  echo "$SUDO pacman -S --needed --noconfirm $pkgs" ;;
        zypper)  echo "$SUDO zypper install -y $pkgs" ;;
        brew)    echo "brew install $pkgs" ;;
    esac
}

update_index_cmd() {
    case "$PM" in
        apt)  echo "$SUDO apt-get update" ;;
        *)    echo "" ;;
    esac
}

# ---------------------------------------------------------------------------
# Build the install list
# ---------------------------------------------------------------------------

REQUIRED_LOGICAL=(libreoffice ffmpeg poppler)
RECOMMENDED_LOGICAL=(exiftool nodejs)

LOGICAL=("${REQUIRED_LOGICAL[@]}")
if [[ "$PROFILE" == "default" ]]; then
    LOGICAL+=("${RECOMMENDED_LOGICAL[@]}")
fi

PKGS=""
for l in "${LOGICAL[@]}"; do
    name="$(pkg_name "$l")"
    if [[ -z "$name" ]]; then
        echo "warning: no $PM package name known for '$l', skipping" >&2
        continue
    fi
    PKGS="$PKGS $name"
done

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

echo "=== DocIngest system deps install ==="
echo "Package manager:  $PM"
echo "Profile:          $PROFILE"
echo "With OCR stack:   $([ $WITH_OCR -eq 1 ] && echo yes || echo no)"
echo "Logical packages: ${LOGICAL[*]}"
echo

UPDATE_CMD="$(update_index_cmd)"
INSTALL=$(install_cmd "$PKGS")

if [[ -n "$UPDATE_CMD" ]]; then
    echo ">> $UPDATE_CMD"
fi
echo ">> $INSTALL"
# yt-dlp via pip (more up-to-date than distro packages)
echo ">> pip install --upgrade yt-dlp"

if [[ $WITH_OCR -eq 1 ]]; then
    echo ">> pip install onnxruntime rapidocr"
    echo ">> python -c 'from rapidocr import RapidOCR; RapidOCR()'  # warm model cache"
fi

if [[ $DRY_RUN -eq 1 ]]; then
    echo
    echo "(dry-run: nothing installed)"
    exit 0
fi

echo
echo "--- Executing ---"
if [[ -n "$UPDATE_CMD" ]]; then
    eval "$UPDATE_CMD"
fi
eval "$INSTALL"
pip install --upgrade yt-dlp

if [[ $WITH_OCR -eq 1 ]]; then
    pip install onnxruntime rapidocr
    # Run as the same user that will later run docingest, so the model cache
    # lands in a writable location. In Dockerfiles, run this step BEFORE
    # switching to a non-root user.
    python -c "from rapidocr import RapidOCR; RapidOCR()" \
        || echo "warning: rapidocr model warmup failed; you may need to retry as the runtime user"
fi

echo
echo "✅ System deps installed. Next:"
echo "   python scripts/verify_deps.py        # confirm everything is reachable"
