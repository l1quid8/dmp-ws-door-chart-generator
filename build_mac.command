#!/bin/bash
# Build the macOS .app for "C1 DMP Toolkit".
# Double-click this file in Finder, or run it from a terminal.
# No prior setup needed beyond Python 3.13 + Homebrew OCR tools.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

BUILD_HOME="$HOME/.dmp-doorchart"
VENV="$BUILD_HOME/venv"
APP_NAME="C1 DMP Toolkit"

echo "==> Project:    $PROJECT_DIR"
echo "==> Build home: $BUILD_HOME  (local, never synced)"
mkdir -p "$BUILD_HOME"

# 1. Python 3.13 -------------------------------------------------------------
PYTHON="$(command -v python3.13 || true)"
if [ -z "$PYTHON" ]; then
    echo "ERROR: python3.13 not found." >&2
    echo "       Install it with:  brew install python@3.13" >&2
    exit 1
fi

# 2. OCR tools (tesseract + ghostscript) -------------------------------------
for tool in tesseract gs; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "ERROR: '$tool' not found." >&2
        echo "       Install OCR tools with:  brew install tesseract ghostscript" >&2
        exit 1
    fi
done

# 3. Build virtualenv --------------------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
    echo "==> Creating build venv..."
    "$PYTHON" -m venv "$VENV"
fi

# 4. Dependencies ------------------------------------------------------------
echo "==> Installing dependencies..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r requirements.txt

# 5. PyInstaller build -------------------------------------------------------
echo "==> Building (this takes a few minutes)..."
"$VENV/bin/pyinstaller" --noconfirm \
    --distpath "$BUILD_HOME/dist" \
    --workpath "$BUILD_HOME/build" \
    dmp_doorchart.spec

# 6. Install to ~/Applications ----------------------------------------------
DEST="$HOME/Applications"
mkdir -p "$DEST"
rm -rf "$DEST/$APP_NAME.app"
cp -R "$BUILD_HOME/dist/$APP_NAME.app" "$DEST/"

echo ""
echo "==> Done. App installed at:"
echo "    $DEST/$APP_NAME.app"
