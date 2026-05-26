"""
Runtime path resolution for RIFEwarp.

The application is self-contained in a single directory with this layout:

    <INSTALL_DIR>/
        VERSION
        launch.sh
        app/                  (Python sources — main_window, ui/, core/)
            ui/...
            core/paths.py     (this file)
        rife/                 (RIFE batch script + models)
            rife_batch.py
            models/
        venv/                 (Python virtualenv)
            bin/python3

This module finds <INSTALL_DIR> at runtime so the codebase has no
hardcoded paths. Resolution order:

  1. The RIFEWARP_INSTALL_DIR environment variable, if set.
     (launch.sh exports this — explicit, fastest, wins if present.)
  2. Walk up from this file's location (.../app/core/paths.py)
     to find the directory that contains "rife/" and "app/" siblings.

If neither resolves, falls back to two levels up from this file —
which gives the correct answer for the standard install layout.
"""
from __future__ import annotations
import os
import sys
from functools import lru_cache


@lru_cache(maxsize=1)
def get_install_dir() -> str:
    """Return the absolute path to the RIFEwarp install root.

    Cached for the life of the process — the install dir doesn't move
    while the app is running.
    """
    # 1. Explicit override via env var.
    env = os.environ.get("RIFEWARP_INSTALL_DIR")
    if env:
        env = os.path.abspath(os.path.expanduser(env))
        if os.path.isdir(env):
            return env

    # 2. Walk up from this file. This file lives at
    #    <INSTALL_DIR>/app/core/paths.py — so the install root is two
    #    levels up. Verify by checking for sibling 'rife/' and 'app/'
    #    directories before accepting.
    here = os.path.dirname(os.path.abspath(__file__))     # .../app/core
    parent = os.path.dirname(here)                        # .../app
    grandparent = os.path.dirname(parent)                 # .../<install>

    candidate = grandparent
    if os.path.isdir(os.path.join(candidate, "rife")) \
            and os.path.isdir(os.path.join(candidate, "app")):
        return candidate

    # 3. Last-resort fallback — return grandparent anyway. Callers
    #    use os.path.isdir on subpaths and degrade gracefully when
    #    things aren't where they expect.
    return grandparent


def get_rife_dir() -> str:
    """Path to the RIFE script + models directory."""
    return os.path.join(get_install_dir(), "rife")


def get_models_dir() -> str:
    """Path to the RIFE models directory."""
    return os.path.join(get_rife_dir(), "models")


def get_venv_python() -> str:
    """Path to the venv's python3 binary, or the running interpreter as fallback.

    Returns a string suitable for use as the python_bin in subprocess calls.
    """
    install = get_install_dir()
    candidates = [
        os.path.join(install, "venv", "bin", "python3"),
        os.path.join(install, "venv", "bin", "python"),
        # Windows-style fallback in case anyone ever runs on Windows.
        os.path.join(install, "venv", "Scripts", "python.exe"),
        sys.executable,
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return "python3"


def get_batch_script() -> str:
    """Path to rife_batch.py."""
    return os.path.join(get_rife_dir(), "rife_batch.py")
