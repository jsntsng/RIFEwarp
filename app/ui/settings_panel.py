"""
Settings panel — clean, fully wired up.
Auto-detects Python bin from venv. No dead UI.
"""
from __future__ import annotations
import os
import sys
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QLabel, QCheckBox,
    QGroupBox, QLineEdit, QPushButton, QHBoxLayout,
    QFileDialog, QComboBox, QSlider, QDoubleSpinBox
)
from PyQt6.QtCore import Qt, pyqtSignal

TT = "QToolTip{background:#1e1e22;color:#ffffff;border:1px solid #4a9eff;font-family:monospace;font-size:10px;}"
SCALE_VALUES = [0.25, 0.5, 1.0, 2.0]
SCALE_LABELS = ["0.25×  (very low VRAM)", "0.5×  (4K recommended)", "1.0×  (default)", "2.0×  (fine detail)"]


class SettingsPanel(QWidget):
    settingsChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(TT)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(8, 8, 8, 8)

        # ── Model ─────────────────────────────────────────────────────────────
        model_box = QGroupBox("MODEL")
        model_layout = QVBoxLayout(model_box)
        model_layout.setSpacing(4)

        model_layout.addWidget(QLabel("Active model"))
        model_row = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setMinimumWidth(160)
        self.model_combo.setToolTip(
            "Select which RIFE model to use for interpolation.\n"
            "Models are auto-detected from train_log* folders in your RIFE directory.\n"
            "Newer models (4.x) produce significantly better results than 3.x.")
        btn_scan = QPushButton("↺")
        btn_scan.setFixedWidth(28)
        btn_scan.setToolTip("Rescan RIFE directory for available models.")
        btn_scan.clicked.connect(self._scan_models)
        model_row.addWidget(self.model_combo)
        model_row.addWidget(btn_scan)
        model_layout.addLayout(model_row)

        self.model_path_label = QLabel("")
        self.model_path_label.setStyleSheet(
            "color:#6a6a72; font-size:9px; font-family:monospace;")
        self.model_path_label.setWordWrap(True)
        model_layout.addWidget(self.model_path_label)

        model_layout.addWidget(QLabel("Custom model dir"))
        custom_row = QHBoxLayout()
        self.model_dir = QLineEdit()
        self.model_dir.setText("")
        self.model_dir.setPlaceholderText("leave blank to use model above")
        self.model_dir.setToolTip(
            "Override the model directory manually.\n"
            "Leave blank to use the model selected above.\n"
            "Use this to point to a model in a non-standard location.")
        btn_model = QPushButton("…")
        btn_model.setFixedWidth(28)
        btn_model.setToolTip("Browse for a custom model directory.")
        btn_model.clicked.connect(self._browse_model)
        custom_row.addWidget(self.model_dir)
        custom_row.addWidget(btn_model)
        model_layout.addLayout(custom_row)

        self.model_combo.currentIndexChanged.connect(self._on_model_selected)
        layout.addWidget(model_box)

        # ── Processing ────────────────────────────────────────────────────────
        proc_box = QGroupBox("PROCESSING")
        proc_layout = QVBoxLayout(proc_box)
        proc_layout.setSpacing(6)

        self.use_fp16 = QCheckBox("FP16 Half Precision")
        self.use_fp16.setChecked(True)
        self.use_fp16.setToolTip(
            "Use 16-bit floating point instead of 32-bit.\n"
            "Reduces VRAM usage by ~50%. Recommended for 3080 Ti and similar.\n"
            "Minimal quality difference on live action footage.")

        self.use_tile = QCheckBox("Tile Processing")
        self.use_tile.setChecked(False)
        self.use_tile.setToolTip(
            "Process frames in tiles instead of whole-frame.\n"
            "Use if you get CUDA out of memory errors on large frames.\n"
            "Slower but allows rendering at resolutions that would otherwise OOM.")

        self.preserve_alpha = QCheckBox("Preserve Alpha Channel")
        self.preserve_alpha.setChecked(True)
        self.preserve_alpha.setToolTip(
            "Copy the alpha channel from the source frame to the output.\n"
            "RIFE does not interpolate alpha — it is copied from the first source frame.\n"
            "Enable if your EXRs have alpha channels you need to preserve.")

        self.use_ensemble = QCheckBox("Ensemble Mode")
        self.use_ensemble.setChecked(False)
        self.use_ensemble.setToolTip(
            "Average forward and backward flow predictions for cleaner results.\n"
            "Produces better output on difficult motion — hair, fast action, fine detail.\n"
            "Approximately 2× slower. Recommended for final renders.")

        for cb in [self.use_fp16, self.use_tile, self.preserve_alpha, self.use_ensemble]:
            proc_layout.addWidget(cb)

        # Scale
        scale_lbl = QLabel("Scale")
        scale_lbl.setToolTip(
            "Controls the resolution at which optical flow is computed.\n"
            "1.0 = full resolution (best quality, most VRAM).\n"
            "0.5 = recommended for 4K frames.\n"
            "0.25 = use only if getting OOM on very large frames.")
        proc_layout.addWidget(scale_lbl)

        scale_row = QHBoxLayout()
        self.scale_slider = QSlider(Qt.Orientation.Horizontal)
        self.scale_slider.setRange(0, 3)
        self.scale_slider.setValue(2)  # default 1.0
        self.scale_slider.setToolTip(
            "Controls the resolution at which optical flow is computed.\n"
            "1.0 = full resolution (best quality, most VRAM).\n"
            "0.5 = recommended for 4K frames.\n"
            "0.25 = use only if getting OOM on very large frames.")
        self.scale_label = QLabel("1.0×")
        self.scale_label.setFixedWidth(48)
        self.scale_label.setStyleSheet("color:#6a6a72; font-size:10px;")
        self.scale_slider.valueChanged.connect(self._on_scale)
        scale_row.addWidget(self.scale_slider)
        scale_row.addWidget(self.scale_label)
        proc_layout.addLayout(scale_row)

        # Scene cut threshold
        sct_lbl = QLabel("Scene cut threshold")
        sct_lbl.setToolTip(
            "Skip RIFE interpolation when two source frames are too different.\n"
            "Prevents blending across hard cuts, flashes, or large jumps.\n"
            "0.0 = disabled (always interpolate).\n"
            "0.3–0.5 = recommended for most footage with cuts.\n"
            "Higher = more sensitive (more frames skipped).")
        proc_layout.addWidget(sct_lbl)

        sct_row = QHBoxLayout()
        self.scene_cut_spin = QDoubleSpinBox()
        self.scene_cut_spin.setRange(0.0, 1.0)
        self.scene_cut_spin.setSingleStep(0.05)
        self.scene_cut_spin.setValue(0.0)
        self.scene_cut_spin.setDecimals(2)
        self.scene_cut_spin.setFixedWidth(70)
        self.scene_cut_spin.setToolTip(
            "Skip RIFE interpolation when two source frames are too different.\n"
            "Prevents blending across hard cuts, flashes, or large jumps.\n"
            "0.0 = disabled (always interpolate).\n"
            "0.3–0.5 = recommended for most footage with cuts.\n"
            "Higher = more sensitive (more frames skipped).")
        self.scene_cut_label = QLabel("(0.0 = off)")
        self.scene_cut_label.setStyleSheet("color:#6a6a72; font-size:10px;")
        self.scene_cut_spin.valueChanged.connect(self._on_scene_cut)
        sct_row.addWidget(self.scene_cut_spin)
        sct_row.addWidget(self.scene_cut_label)
        sct_row.addStretch()
        proc_layout.addLayout(sct_row)

        layout.addWidget(proc_box)
        layout.addStretch()

        self._scan_models()

    # ── Scale / scene cut ─────────────────────────────────────────────────────

    def _on_scale(self, v):
        self.scale_label.setText(f"{SCALE_VALUES[v]}×")

    def _on_scene_cut(self, v):
        if v <= 0.0:
            self.scene_cut_label.setText("(0.0 = off)")
        elif v < 0.3:
            self.scene_cut_label.setText("(low sensitivity)")
        elif v < 0.6:
            self.scene_cut_label.setText("(recommended)")
        else:
            self.scene_cut_label.setText("(high sensitivity)")

    # ── Model scanning ────────────────────────────────────────────────────────

    def _rife_dir(self):
        """RIFE directory — resolved at runtime from the install root."""
        from core.paths import get_rife_dir
        d = get_rife_dir()
        return d if os.path.isdir(d) else os.path.expanduser("~")

    def _models_dir(self):
        """Returns the models/ subdirectory."""
        return os.path.join(self._rife_dir(), "models")

    def _scan_models(self):
        """Scan models/ directory for named model folders containing flownet.pkl."""
        models_dir = self._models_dir()
        found = []
        if os.path.isdir(models_dir):
            for name in sorted(os.listdir(models_dir), reverse=True):
                path = os.path.join(models_dir, name)
                if os.path.isdir(path):
                    if os.path.exists(os.path.join(path, "flownet.pkl")):
                        found.append((name, path))
        # Also check legacy train_log location
        rife_dir = self._rife_dir()
        legacy = os.path.join(rife_dir, "train_log")
        if os.path.isdir(legacy) and os.path.exists(os.path.join(legacy, "flownet.pkl")):
            found.append(("train_log (legacy)", legacy))

        current = self.model_combo.currentText()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for name, path in found:
            self.model_combo.addItem(name, path)
        self.model_combo.blockSignals(False)

        idx = self.model_combo.findText(current)
        if idx >= 0:
            self.model_combo.setCurrentIndex(idx)
        elif self.model_combo.count() > 0:
            self.model_combo.setCurrentIndex(0)

        self._on_model_selected()

    def _on_model_selected(self):
        if self.model_combo.count() == 0:
            self.model_path_label.setText("No models found — check RIFE directory")
            return
        path = self.model_combo.currentData()
        if path:
            self.model_path_label.setText(path)
        # Never auto-fill custom model dir — user must explicitly set it

    def _browse_model(self):
        d = QFileDialog.getExistingDirectory(
            self, "Select Model Directory", os.path.expanduser("~"))
        if d:
            self.model_dir.setText(d)

    # ── Auto-detect Python ────────────────────────────────────────────────────

    def _auto_python(self):
        """Find the venv Python next to the install directory."""
        from core.paths import get_venv_python
        return get_venv_python()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_rife_script(self):
        """rife_batch.py is found relative to rife dir."""
        rife_dir = self._rife_dir()
        return os.path.join(rife_dir, "inference_img.py")

    def get_model_dir(self):
        custom = self.model_dir.text().strip()
        if custom:
            return custom
        if self.model_combo.count() > 0:
            return self.model_combo.currentData() or ""
        return ""

    def get_python_bin(self):
        return self._auto_python()

    def get_model_name(self):
        return self.model_combo.currentText()

    def get_scale(self):
        return SCALE_VALUES[self.scale_slider.value()]

    def get_use_fp16(self):        return self.use_fp16.isChecked()
    def get_use_tile(self):        return self.use_tile.isChecked()
    def get_preserve_alpha(self):  return self.preserve_alpha.isChecked()
    def get_use_ensemble(self):    return self.use_ensemble.isChecked()
    def get_scene_cut(self):       return self.scene_cut_spin.value()
