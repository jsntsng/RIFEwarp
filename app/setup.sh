#!/usr/bin/env bash
# RIFEwarp setup script — supports Rocky Linux 9/10 and Ubuntu 22.04/24.04
# Installs everything into ~/RIFEwarp/ — no sudo required.
set -e

INSTALL_DIR="$HOME/RIFEwarp"
VENV_DIR="$INSTALL_DIR/venv"
LAUNCHER="$INSTALL_DIR/launch.sh"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(dirname "$SCRIPT_DIR")"
RELEASE_TAG="v1.1.1"
RELEASE_BASE="https://github.com/jsntsng/RIFEwarp/releases/download/$RELEASE_TAG"
MODEL_VERSIONS=(4.9.2 4.18 4.22 4.25 4.26)

echo ""
echo "============================================="
echo "  RIFEwarp — Setup"
echo "  Install dir: $INSTALL_DIR"
echo "============================================="
echo ""

# ── [1/6] Prerequisites ────────────────────────────────────────────────────────
echo "[1/6] Checking prerequisites..."

# Detect package manager (apt checked first, then dnf)
if command -v apt-get &>/dev/null; then
    PKG_HINT="apt"
elif command -v dnf &>/dev/null; then
    PKG_HINT="dnf"
else
    PKG_HINT="unknown"
fi

_missing=()
for _cmd in python3.11 curl unzip; do
    command -v "$_cmd" &>/dev/null || _missing+=("$_cmd")
done
if [ "${#_missing[@]}" -gt 0 ]; then
    echo ""
    echo "  ERROR: Missing required tools: ${_missing[*]}"
    case "$PKG_HINT" in
        dnf)
            echo "  Install with: sudo dnf install ${_missing[*]}"
            ;;
        apt)
            echo "  Install with: sudo apt install ${_missing[*]}"
            if [[ " ${_missing[*]} " == *" python3.11 "* ]]; then
                echo ""
                echo "  python3.11 is not in the default Ubuntu repos — add the deadsnakes PPA first:"
                echo "    sudo add-apt-repository ppa:deadsnakes/ppa"
                echo "    sudo apt update"
                echo "    sudo apt install python3.11 python3.11-venv"
            fi
            ;;
    esac
    echo ""
    exit 1
fi
echo "      python3.11, curl, unzip — OK."

# venv/ensurepip capability (Debian/Ubuntu split it into python3.11-venv)
if ! python3.11 -m ensurepip --version >/dev/null 2>&1; then
    echo ""
    echo "  ERROR: python3.11 cannot create virtual environments (ensurepip unavailable)."
    case "$PKG_HINT" in
        apt)
            echo "  Install with: sudo apt install python3.11-venv"
            ;;
        dnf)
            echo "  This is unexpected on dnf systems — check your python3.11 installation."
            ;;
    esac
    echo ""
    exit 1
fi
echo "      python3.11 venv support — OK."

# Qt xcb runtime library (warn only — headless/CI installs are legitimate)
_have_xcb_cursor=0
if command -v ldconfig &>/dev/null && ldconfig -p 2>/dev/null | grep -q 'libxcb-cursor\.so\.0'; then
    _have_xcb_cursor=1
else
    for _dir in /usr/lib64 /usr/lib /usr/lib/x86_64-linux-gnu /usr/lib/aarch64-linux-gnu /lib64 /lib/x86_64-linux-gnu; do
        if [ -e "$_dir/libxcb-cursor.so.0" ]; then
            _have_xcb_cursor=1
            break
        fi
    done
fi
if [ "$_have_xcb_cursor" -eq 1 ]; then
    echo "      libxcb-cursor — OK."
else
    echo ""
    echo "  WARNING: libxcb-cursor.so.0 not found. The app may fail to launch with"
    echo "           \"could not load the Qt platform plugin xcb\"."
    case "$PKG_HINT" in
        apt) echo "           Install with: sudo apt install libxcb-cursor0" ;;
        dnf) echo "           Install with: sudo dnf install xcb-util-cursor" ;;
        *)   echo "           Install the libxcb-cursor package for your distro." ;;
    esac
fi
echo ""

# ── [2/6] Copy repo tree to install dir ───────────────────────────────────────
echo "[2/6] Copying app files to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
if command -v rsync &>/dev/null; then
    rsync -a \
        --exclude='venv/' \
        --exclude='.git/' \
        --exclude='__pycache__/' \
        --exclude='*.pkl' \
        --exclude='*.pyc' \
        "$SOURCE_ROOT/" "$INSTALL_DIR/"
else
    for _item in app rife VERSION launch.sh CHANGELOG.md README.md requirements.txt; do
        _src="$SOURCE_ROOT/$_item"
        [ -e "$_src" ] || continue
        if [ -d "$_src" ]; then
            cp -r "$_src" "$INSTALL_DIR/"
        else
            cp "$_src" "$INSTALL_DIR/"
        fi
    done
    find "$INSTALL_DIR" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
    find "$INSTALL_DIR" -name '*.pyc' -delete 2>/dev/null || true
fi
chmod +x "$LAUNCHER"
echo "      Done."
echo ""

# ── [3/6] Python venv ─────────────────────────────────────────────────────────
echo "[3/6] Creating Python venv..."
if [ -d "$VENV_DIR" ]; then
    echo "      Venv already exists — skipping."
else
    python3.11 -m venv "$VENV_DIR"
fi
echo ""

# ── [4/6] Install Python dependencies ─────────────────────────────────────────
echo "[4/6] Installing Python dependencies..."
VENV_PY="$VENV_DIR/bin/python3"
VENV_PIP="$VENV_DIR/bin/pip"
"$VENV_PIP" install --upgrade pip --quiet

echo "      PyQt6..."
"$VENV_PIP" install PyQt6 --quiet

echo "      PyTorch (CUDA 12.6)..."
"$VENV_PIP" install \
    torch torchvision \
    --index-url https://download.pytorch.org/whl/cu126 \
    --quiet

echo "      ML / image dependencies..."
"$VENV_PIP" install numpy opencv-python-headless openimageio sk-video tqdm --quiet

echo "      PyTorch: $("$VENV_PY" -c 'import torch; print(torch.__version__)')"
echo "      CUDA available: $("$VENV_PY" -c 'import torch; print(torch.cuda.is_available())')"
echo ""

# ── [5/6] Download RIFE model weights ─────────────────────────────────────────
echo "[5/6] Downloading RIFE model weights..."
MODELS_DIR="$INSTALL_DIR/rife/models"
_WGTS_TMP="$(mktemp -d)"
trap 'rm -rf "$_WGTS_TMP"' EXIT

for _VERSION in "${MODEL_VERSIONS[@]}"; do
    _dest="$MODELS_DIR/$_VERSION"
    _pkl="$_dest/flownet.pkl"

    if [ -f "$_pkl" ]; then
        echo "      $_VERSION — already present, skipping."
        continue
    fi

    mkdir -p "$_dest"
    _url="$RELEASE_BASE/flownet-${_VERSION}.zip"
    _tmpzip="$_WGTS_TMP/flownet-${_VERSION}.zip"

    echo "      $_VERSION — downloading..."
    if ! curl -L --fail --silent --show-error -o "$_tmpzip" "$_url"; then
        echo ""
        echo "  ERROR: Download failed for model $_VERSION"
        echo "         URL: $_url"
        echo ""
        exit 1
    fi

    if [ ! -s "$_tmpzip" ]; then
        echo ""
        echo "  ERROR: Downloaded archive is empty for model $_VERSION"
        echo "         URL: $_url"
        echo ""
        exit 1
    fi

    echo "      $_VERSION — extracting flownet.pkl..."
    if ! unzip -j -o -q "$_tmpzip" "*flownet.pkl" -d "$_dest"; then
        echo ""
        echo "  ERROR: Extraction failed for model $_VERSION"
        echo ""
        exit 1
    fi

    if [ ! -s "$_pkl" ]; then
        echo ""
        echo "  ERROR: flownet.pkl missing or empty after extraction for model $_VERSION"
        echo ""
        exit 1
    fi

    rm -f "$_tmpzip"
    echo "      $_VERSION — OK ($(du -sh "$_pkl" | cut -f1))."
done
echo ""

# ── [6/6] Shortcuts and desktop entry ─────────────────────────────────────────
echo "[6/6] Installing shortcuts..."

mkdir -p "$HOME/bin"
ln -sf "$LAUNCHER" "$HOME/bin/rifewarp"
echo "      ~/bin/rifewarp -> $LAUNCHER"

_DESKTOP_DIR="$HOME/.local/share/applications"
mkdir -p "$_DESKTOP_DIR"
cat > "$_DESKTOP_DIR/rifewarp.desktop" << DESKTOP_EOF
[Desktop Entry]
Name=RIFEwarp
Comment=RIFE-powered timewarp tool
Exec=$LAUNCHER
Terminal=false
Type=Application
Categories=Graphics;Video;
DESKTOP_EOF
echo "      $_DESKTOP_DIR/rifewarp.desktop"
echo ""

# PATH reminder
if [[ ":$PATH:" != *":$HOME/bin:"* ]]; then
    echo "  Add ~/bin to PATH — add this to ~/.bashrc:"
    echo "    export PATH=\"\$HOME/bin:\$PATH\""
    echo "  Then: source ~/.bashrc"
    echo ""
fi

echo "============================================="
echo "  Setup complete."
echo ""
echo "  Install dir:  $INSTALL_DIR"
echo "  Launch:       rifewarp   (or ~/bin/rifewarp)"
echo "  Desktop:      $_DESKTOP_DIR/rifewarp.desktop"
echo "============================================="
echo ""
