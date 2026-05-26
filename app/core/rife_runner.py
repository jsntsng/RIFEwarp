"""
RIFE runner — uses rife_batch.py to load the model ONCE per job
and process all interpolated frames in a single subprocess call.
Integer frames (exact copies) are handled directly without RIFE.
"""
from __future__ import annotations
import os
import math
import shutil
import json
import subprocess
import tempfile
from typing import List, Tuple, Optional

from PyQt6.QtCore import QThread, QObject, pyqtSignal


class RifeJobSignals(QObject):
    progress   = pyqtSignal(int, int)
    log        = pyqtSignal(str)
    finished   = pyqtSignal(bool, str)
    frame_done = pyqtSignal(int, str)


class RifeRunner(QThread):
    def __init__(self, rife_script, frame_list, in_dir, in_prefix, in_padding,
                 in_ext, out_dir, out_prefix, out_padding, out_ext,
                 model_dir=None, python_bin="python3",
                 use_fp16=True, use_tile=False, preserve_alpha=True,
                 use_ensemble=False, scale=1.0, scene_cut=0.0,
                 parent=None):
        super().__init__(parent)
        self.rife_script    = rife_script
        self.frame_list     = frame_list
        self.in_dir         = in_dir
        self.in_prefix      = in_prefix
        self.in_padding     = in_padding
        self.in_ext         = in_ext
        self.out_dir        = out_dir
        self.out_prefix     = out_prefix
        self.out_padding    = out_padding
        self.out_ext        = out_ext
        self.model_dir      = model_dir
        self.python_bin     = python_bin
        self.use_fp16       = use_fp16
        self.use_tile       = use_tile
        self.preserve_alpha = preserve_alpha
        self.use_ensemble  = use_ensemble
        self.scale         = scale
        self.scene_cut     = scene_cut
        self.signals        = RifeJobSignals()
        self._abort         = False

    def abort(self):
        self._abort = True

    def _in_path(self, frame_int):
        return os.path.join(
            self.in_dir,
            f"{self.in_prefix}{frame_int:0{self.in_padding}d}{self.in_ext}"
        )

    def _out_path(self, frame_abs):
        return os.path.join(
            self.out_dir,
            f"{self.out_prefix}{frame_abs:0{self.out_padding}d}{self.out_ext}"
        )

    def _batch_script(self):
        """Find rife_batch.py in the rife directory."""
        from core.paths import get_batch_script, get_rife_dir
        candidate = get_batch_script()
        if os.path.exists(candidate):
            return candidate
        # Fall back next to rife_script (the original behavior)
        script_dir = os.path.dirname(os.path.abspath(self.rife_script))
        return os.path.join(script_dir, "rife_batch.py")

    def _model_dir(self):
        from core.paths import get_rife_dir
        if self.model_dir and os.path.isdir(self.model_dir):
            return self.model_dir
        rife_dir   = get_rife_dir()
        # Check models/ subdirectory for any model
        models_dir = os.path.join(rife_dir, "models")
        if os.path.isdir(models_dir):
            entries = sorted(os.listdir(models_dir), reverse=True)
            for name in entries:
                p = os.path.join(models_dir, name)
                if os.path.isdir(p) and os.path.exists(os.path.join(p, "flownet.pkl")):
                    return p
        # Legacy train_log
        legacy = os.path.join(rife_dir, "train_log")
        if os.path.isdir(legacy):
            return legacy
        return None

    def run(self):
        os.makedirs(self.out_dir, exist_ok=True)
        total    = len(self.frame_list)
        done     = 0
        rife_dir = os.path.dirname(os.path.abspath(self.rife_script))
        model_dir = self._model_dir()
        batch_script = self._batch_script()

        self.signals.log.emit(f"Starting: {total} frames")
        self.signals.log.emit(f"Batch:    {batch_script}")
        self.signals.log.emit(f"Python:   {self.python_bin}")
        self.signals.log.emit(f"Model:    {model_dir or 'NOT FOUND'}")
        self.signals.log.emit(f"Output:   {self.out_dir}")

        if not model_dir:
            self.signals.finished.emit(False,
                "Model directory not found. Copy train_log to the RIFE directory.")
            return

        if not os.path.exists(batch_script):
            self.signals.finished.emit(False,
                f"rife_batch.py not found at {batch_script}\n"
                f"Copy rife_batch.py to {rife_dir}/")
            return

        # ── Step 1: Handle integer frames (straight copies) ───────────────
        rife_tasks = []
        for out_frame_abs, in_frame_float in self.frame_list:
            if self._abort:
                self.signals.finished.emit(False, "Aborted.")
                return

            floor_f = int(math.floor(in_frame_float))
            ceil_f  = int(math.ceil(in_frame_float))
            t       = in_frame_float - floor_f
            out_path = self._out_path(out_frame_abs)

            if abs(t) < 1e-4 or floor_f == ceil_f:
                # Integer frame — copy directly
                src = self._in_path(floor_f)
                if os.path.exists(src):
                    shutil.copy2(src, out_path)
                    self.signals.log.emit(
                        f"COPY  {os.path.basename(src)} -> {os.path.basename(out_path)}")
                    done += 1
                    self.signals.progress.emit(done, total)
                    self.signals.frame_done.emit(out_frame_abs, out_path)
                else:
                    self.signals.log.emit(f"WARN  missing: {src}")
                    done += 1
                    self.signals.progress.emit(done, total)
            else:
                # Needs RIFE interpolation
                floor_path = self._in_path(floor_f)
                ceil_path  = self._in_path(ceil_f)
                if not os.path.exists(floor_path):
                    self.signals.log.emit(f"WARN  missing: {floor_path}")
                    done += 1
                    self.signals.progress.emit(done, total)
                    continue
                if not os.path.exists(ceil_path):
                    self.signals.log.emit(f"WARN  missing: {ceil_path}")
                    done += 1
                    self.signals.progress.emit(done, total)
                    continue
                rife_tasks.append({
                    "img0":  floor_path,
                    "img1":  ceil_path,
                    "ratio": t,
                    "out":   out_path,
                    "frame": out_frame_abs,
                })

        if not rife_tasks:
            self.signals.finished.emit(True,
                f"Done. {total} frames written to {self.out_dir}")
            return

        # ── Step 2: Write task file and run rife_batch.py once ────────────
        self.signals.log.emit(
            f"RIFE  {len(rife_tasks)} frames to interpolate (model loads once)")

        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False) as tf:
            json.dump(rife_tasks, tf)
            task_file = tf.name

        try:
            env = os.environ.copy()
            env["OPENCV_IO_ENABLE_OPENEXR"] = "1"

            cmd = [
                self.python_bin,
                batch_script,
                "--tasks", task_file,
                "--model", model_dir,
            ]
            if self.use_fp16:
                cmd.append("--fp16")
            if self.use_tile:
                cmd += ["--tile"]
            if self.preserve_alpha:
                cmd.append("--preserve-alpha")
            if self.use_ensemble:
                cmd.append("--ensemble")
            if self.scale != 1.0:
                cmd += ["--scale", str(self.scale)]
            if self.scene_cut > 0.0:
                cmd += ["--scene-cut", str(self.scene_cut)]

            proc = subprocess.Popen(
                cmd,
                cwd=rife_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )

            # Read output line by line so we can update progress live
            for line in proc.stdout:
                if self._abort:
                    proc.kill()
                    self.signals.finished.emit(False, "Aborted.")
                    return

                line = line.rstrip()
                if not line:
                    continue

                if line.startswith("DONE "):
                    frame_num = int(line.split()[1])
                    out_path  = self._out_path(frame_num)
                    done += 1
                    self.signals.progress.emit(done, total)
                    self.signals.frame_done.emit(frame_num, out_path)
                    self.signals.log.emit(
                        f"RIFE  f{frame_num:04d} -> {os.path.basename(out_path)}")
                elif line.startswith("FINISHED "):
                    pass  # handled below
                elif line.startswith("ERROR "):
                    self.signals.log.emit(f"ERR   {line}")
                else:
                    self.signals.log.emit(f"      {line}")

            proc.wait()

            if proc.returncode != 0:
                self.signals.finished.emit(False,
                    f"rife_batch.py exited with code {proc.returncode}")
                return

        finally:
            try:
                os.unlink(task_file)
            except Exception:
                pass

        self.signals.finished.emit(True,
            f"Done. {total} frames written to {self.out_dir}")
