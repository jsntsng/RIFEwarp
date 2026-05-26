#!/usr/bin/env bash
# RIFEwarp launcher — derives the install directory from the script's
# own location, so the entire app folder can be moved or renamed
# without breaking. Exports RIFEWARP_INSTALL_DIR so app code can rely
# on it directly without re-deriving the path.

# Resolve install dir from the directory containing this script.
# Use readlink -f to resolve symlinks (the .desktop launcher might
# point through one).
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
INSTALL_DIR="$(dirname "$SCRIPT_PATH")"

export RIFEWARP_INSTALL_DIR="$INSTALL_DIR"
export RIFE_RETIME_SCRIPT="$INSTALL_DIR/rife/inference_img.py"
export RIFE_RETIME_PYTHON="$INSTALL_DIR/venv/bin/python3"

cd "$INSTALL_DIR/app"
exec "$INSTALL_DIR/venv/bin/python3" main.py "$@"
