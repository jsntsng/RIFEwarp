#!/usr/bin/env bash
# rife_retime setup script — Rocky Linux 9/10
# Installs everything into ~/rife_retime/ — no sudo required.
set -e

INSTALL_DIR="$HOME/rife_retime"
VENV_DIR="$INSTALL_DIR/venv"
APP_DIR="$INSTALL_DIR/app"
RIFE_DIR="$INSTALL_DIR/rife"
LAUNCHER="$INSTALL_DIR/launch.sh"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ""
echo "============================================="
echo "  RIFE Retime — Setup"
echo "  Install dir: $INSTALL_DIR"
echo "============================================="
echo ""

echo "[1/6] Creating install directory..."
mkdir -p "$INSTALL_DIR" "$APP_DIR"

echo "[2/6] Cloning ECCV2022-RIFE..."
if [ -d "$RIFE_DIR/.git" ]; then
    echo "      Already cloned — skipping."
else
    git clone https://github.com/hzwer/ECCV2022-RIFE.git "$RIFE_DIR"
fi

echo "[3/6] Creating Python venv..."
if [ -d "$VENV_DIR" ]; then
    echo "      Venv already exists — skipping."
else
    python3.11 -m venv "$VENV_DIR"
fi

VENV_PY="$VENV_DIR/bin/python3"
VENV_PIP="$VENV_DIR/bin/pip"
"$VENV_PIP" install --upgrade pip --quiet

echo "[4/6] Installing PyQt6..."
"$VENV_PIP" install PyQt6 --quiet
echo "      PyQt6 installed."

echo "[5/6] Installing RIFE + ML dependencies..."
"$VENV_PIP" install \
    torch torchvision \
    --index-url https://download.pytorch.org/whl/cu126 \
    --quiet
"$VENV_PIP" install numpy opencv-python-headless openimageio sk-video tqdm --quiet
echo "      PyTorch: $("$VENV_PY" -c 'import torch; print(torch.__version__)')"
echo "      CUDA available: $("$VENV_PY" -c 'import torch; print(torch.cuda.is_available())')"

echo "[6/6] Copying app source..."
cp -r "$SCRIPT_DIR/." "$APP_DIR/"

echo "Writing launcher..."
cat > "$LAUNCHER" << EOF
#!/usr/bin/env bash
INSTALL_DIR="\$HOME/rife_retime"
export RIFE_RETIME_SCRIPT="\$INSTALL_DIR/rife/inference_img.py"
export RIFE_RETIME_PYTHON="\$INSTALL_DIR/venv/bin/python3"
cd "\$INSTALL_DIR/app"
exec "\$INSTALL_DIR/venv/bin/python3" main.py "\$@"
EOF
chmod +x "$LAUNCHER"

mkdir -p "$HOME/bin"
ln -sf "$LAUNCHER" "$HOME/bin/rife-retime"

# PATH reminder
if [[ ":$PATH:" != *":$HOME/bin:"* ]]; then
    echo ""
    echo "  Add ~/bin to PATH — add this to ~/.bashrc:"
    echo "    export PATH=\"\$HOME/bin:\$PATH\""
    echo "  Then: source ~/.bashrc"
fi

echo ""
echo "============================================="
echo "  IMPORTANT: Download the pretrained model"
echo "============================================="
echo "  https://github.com/hzwer/ECCV2022-RIFE"
echo "  Unzip into: $RIFE_DIR/train_log/"
echo ""
echo "============================================="
echo "  Setup complete."
echo "============================================="
echo ""
echo "  Launch: rife-retime"
echo ""
