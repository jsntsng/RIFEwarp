"""I/O panel — input/output sequence settings.
Frame range is auto-detected from disk, shown read-only.
No editable start/end spinboxes.
"""
from __future__ import annotations
import os
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QSpinBox, QGroupBox, QFileDialog, QFormLayout
)
from PyQt6.QtCore import pyqtSignal
from core.sequence import scan_sequence, SequenceInfo


# Match settings_panel tooltip styling for consistency
TT = ("QToolTip{background:#1e1e22;color:#ffffff;"
      "border:1px solid #4a9eff;font-family:monospace;font-size:9pt;}")


def parse_frame_range(text: str):
    """Parse a frame range expression into a sorted set of ints.

    Accepts:  "1001-1500"
              "1001,1003,1005-1010"
              "1001 - 1010, 1020, 1030-1035"

    Returns:  (frame_set, error_message)
              frame_set is None if text is empty or invalid.
              error_message is "" on success or empty input,
              otherwise a human-readable reason.
    """
    s = (text or "").strip()
    if not s:
        return None, ""

    frames = set()
    for piece in s.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "-" in piece:
            parts = [p.strip() for p in piece.split("-")]
            if len(parts) != 2 or not parts[0] or not parts[1]:
                return None, f"bad range: '{piece}'"
            try:
                a, b = int(parts[0]), int(parts[1])
            except ValueError:
                return None, f"non-numeric: '{piece}'"
            if a > b:
                a, b = b, a
            frames.update(range(a, b + 1))
        else:
            try:
                frames.add(int(piece))
            except ValueError:
                return None, f"non-numeric: '{piece}'"
    if not frames:
        return None, "empty"
    return frames, ""


def format_frame_range(frames):
    """Compact a set/iterable of frames into '1001-1010,1020,1030-1035'."""
    if not frames:
        return ""
    nums = sorted(set(int(f) for f in frames))
    out = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
        else:
            out.append(f"{start}-{prev}" if start != prev else f"{start}")
            start = prev = n
    out.append(f"{start}-{prev}" if start != prev else f"{start}")
    return ",".join(out)


class IOPanel(QWidget):
    sequenceChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._in_seq: SequenceInfo | None = None
        self.setStyleSheet(TT)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(8, 8, 8, 8)

        # ── Input ─────────────────────────────────────────────────────────────
        in_box = QGroupBox("INPUT SEQUENCE")
        in_layout = QVBoxLayout(in_box)
        in_layout.setSpacing(4)

        dir_row = QHBoxLayout()
        self.in_dir = QLineEdit()
        self.in_dir.setPlaceholderText("/shots/sh010/frames/")
        self.in_dir.setToolTip(
            "Path to the directory containing your source frame sequence.\n"
            "RIFEwarp auto-detects format, prefix, padding, and frame range.\n"
            "Press Enter or click outside the field to scan.")
        self.in_dir.editingFinished.connect(self._on_in_dir_changed)
        btn = QPushButton("…")
        btn.setFixedWidth(28)
        btn.setToolTip("Browse for the input sequence directory.")
        btn.clicked.connect(self._browse_input)
        dir_row.addWidget(self.in_dir)
        dir_row.addWidget(btn)
        in_layout.addLayout(dir_row)

        self.in_status = QLabel("no sequence loaded")
        self.in_status.setObjectName("statusLabel")
        self.in_status.setWordWrap(True)
        in_layout.addWidget(self.in_status)

        form = QFormLayout()
        form.setSpacing(4)

        self.in_fmt = QComboBox()
        self.in_fmt.addItems(["EXR", "PNG", "TIFF", "DPX"])
        self.in_fmt.setToolTip(
            "File format of the source frames.\n"
            "Auto-detected from the input directory when scanned.\n"
            "EXR recommended for VFX work (linear, 16-bit half float).")
        form.addRow("Format", self.in_fmt)

        self.in_padding = QSpinBox()
        self.in_padding.setRange(1, 8)
        self.in_padding.setValue(4)
        self.in_padding.setToolTip(
            "Frame number zero-padding width in the source filenames.\n"
            "Example: '1001' = 4, '00001' = 5.\n"
            "Auto-detected from the input directory.")
        form.addRow("Padding", self.in_padding)

        # Read-only working range display
        self.in_range_label = QLabel("–")
        self.in_range_label.setObjectName("tagLabel")
        form.addRow("Range", self.in_range_label)

        # RAM cache fill — driven by the viewer's background preloader.
        self._cache_label = QLabel("–")
        self._cache_label.setStyleSheet(
            "color:#3a3a42; font-family:monospace; font-size:8pt;")
        form.addRow("Cache", self._cache_label)

        in_layout.addLayout(form)
        layout.addWidget(in_box)

        # ── Output ────────────────────────────────────────────────────────────
        out_box = QGroupBox("OUTPUT SEQUENCE")
        out_layout = QVBoxLayout(out_box)
        out_layout.setSpacing(4)

        out_dir_row = QHBoxLayout()
        self.out_dir = QLineEdit()
        self.out_dir.setPlaceholderText("/shots/sh010/retime/")
        self.out_dir.setToolTip(
            "Parent directory for the rendered output.\n"
            "RIFEwarp creates a subdirectory inside this path named after the prefix.\n"
            "Full output path is shown in the blue label below the field.")
        self.out_dir.textChanged.connect(self._update_full_path)
        btn_out = QPushButton("…")
        btn_out.setFixedWidth(28)
        btn_out.setToolTip("Browse for the output parent directory.")
        btn_out.clicked.connect(self._browse_output)
        out_dir_row.addWidget(self.out_dir)
        out_dir_row.addWidget(btn_out)
        out_layout.addLayout(out_dir_row)

        self.full_path_label = QLabel("")
        self.full_path_label.setWordWrap(True)
        self.full_path_label.setStyleSheet(
            "color:#4a9eff; font-size:8pt; font-family:monospace;"
            "background:#111113; border-left:2px solid #4a9eff;"
            "padding:3px 6px; margin-top:1px;")
        out_layout.addWidget(self.full_path_label)

        out_form = QFormLayout()
        out_form.setSpacing(4)

        self.out_fmt = QComboBox()
        self.out_fmt.addItems(["EXR", "PNG", "TIFF", "DPX"])
        self.out_fmt.setToolTip(
            "File format for rendered output frames.\n"
            "EXR for VFX delivery (linear, 16-bit half float).\n"
            "PNG/TIFF for previews and compatibility.")
        self.out_fmt.currentTextChanged.connect(self._update_full_path)
        out_form.addRow("Format", self.out_fmt)

        self.out_prefix = QLineEdit("retime_")
        self.out_prefix.setToolTip(
            "Filename prefix for rendered frames.\n"
            "Also used as the job name in the render queue\n"
            "and as the name of the subdirectory inside the output parent.\n"
            "Tip: include a version token like 'v0001' to use Save Up Version.")
        self.out_prefix.textChanged.connect(self._update_full_path)
        out_form.addRow("Prefix", self.out_prefix)

        self.out_padding = QSpinBox()
        self.out_padding.setRange(1, 8)
        self.out_padding.setValue(4)
        self.out_padding.setToolTip(
            "Frame number zero-padding width for output filenames.\n"
            "Example: '1001' = 4, '00001' = 5.\n"
            "Typically matches the input padding.")
        out_form.addRow("Padding", self.out_padding)

        # Frame Range — optional override; empty = full curve range
        self.out_range = QLineEdit()
        self.out_range.setPlaceholderText("(full curve)  e.g. 1001-1500  or  1001,1003,1005-1010")
        self.out_range.textChanged.connect(self._on_out_range_changed)
        self.out_range.setToolTip(
            "Optional. Limit rendering to specific output frames.\n"
            "Empty = render the full curve range (default behavior).\n"
            "Captured per-job when added to the queue —\n"
            "changing this field after queueing does not affect existing jobs.\n"
            "Examples:\n"
            "  1001-1500              (a single range)\n"
            "  1001,1003,1005-1010    (individual frames + a range)\n"
            "  1500-1001              (reversed ranges are accepted)")
        out_form.addRow("Frame Range", self.out_range)

        self.out_range_status = QLabel("")
        self.out_range_status.setStyleSheet(
            "color:#6a6a72; font-family:monospace; font-size:8pt;")
        out_form.addRow("", self.out_range_status)

        self.out_count_label = QLabel("~? frames")
        self.out_count_label.setObjectName("tagLabel")
        out_form.addRow("Est. out", self.out_count_label)

        out_layout.addLayout(out_form)
        layout.addWidget(out_box)
        layout.addStretch()

    # ── Browsing ──────────────────────────────────────────────────────────────

    def _browse_input(self):
        d = QFileDialog.getExistingDirectory(
            self, "Select Input Directory",
            self.in_dir.text() or os.path.expanduser("~"))
        if d:
            self.in_dir.setText(d)
            self._on_in_dir_changed()

    def _update_full_path(self):
        base   = self.out_dir.text().strip()
        prefix = self.out_prefix.text().strip()
        ext    = "." + self.out_fmt.currentText().lower()
        padding = self.out_padding.value() if hasattr(self, 'out_padding') else 4
        if base and prefix:
            subdir   = os.path.join(base, prefix)
            pattern  = f"{prefix}.%0{padding}d{ext}"
            full     = os.path.join(subdir, pattern)
            self.full_path_label.setText(full)
        else:
            self.full_path_label.setText("")

    def _browse_output(self):
        d = QFileDialog.getExistingDirectory(
            self, "Select Output Directory",
            self.out_dir.text() or os.path.expanduser("~"))
        if d:
            self.out_dir.setText(d)
            self._update_full_path()

    def _on_in_dir_changed(self):
        d = self.in_dir.text().strip()
        if not d:
            return
        seq = scan_sequence(d)
        if seq:
            self._in_seq = seq
            self.in_padding.setValue(seq.padding)
            ext = seq.extension.lstrip(".").upper()
            idx = self.in_fmt.findText(ext)
            if idx >= 0:
                self.in_fmt.setCurrentIndex(idx)
            miss = f"  {len(seq.missing)} missing" if seq.missing else ""
            self.in_range_label.setText(
                f"{seq.first_frame} – {seq.last_frame}  ({seq.frame_count}f{miss})")
            self.in_status.setText(
                f"{seq.frame_count} frames  [{seq.first_frame}–{seq.last_frame}]{miss}")
            self.in_status.setStyleSheet("color: #3ecf6e;")
            # Prefix is set from project filename on save/load — don't override here
        else:
            self._in_seq = None
            self.in_range_label.setText("–")
            self.in_status.setText("No sequence found in directory.")
            self.in_status.setStyleSheet("color: #e04a4a;")
        self._update_full_path()
        self.sequenceChanged.emit()

    # ── Public API ────────────────────────────────────────────────────────────

    def _on_out_range_changed(self):
        """Live-validate the frame range field. Empty = no override."""
        text = self.out_range.text()
        frames, err = parse_frame_range(text)
        if not text.strip():
            # Empty — clear status and any error styling
            self.out_range.setStyleSheet("")
            self.out_range_status.setText("")
        elif err:
            # Invalid — flag with a red border
            self.out_range.setStyleSheet(
                "QLineEdit{border:1px solid #e04a4a;background:#2a1818;}")
            self.out_range_status.setText(err)
            self.out_range_status.setStyleSheet(
                "color:#e04a4a; font-family:monospace; font-size:8pt;")
        else:
            # Valid — show frame count and compact form
            self.out_range.setStyleSheet(
                "QLineEdit{border:1px solid #3ecf6e;}")
            n = len(frames)
            self.out_range_status.setText(
                f"{n} frame{'s' if n != 1 else ''}  "
                f"[{min(frames)}\u2013{max(frames)}]")
            self.out_range_status.setStyleSheet(
                "color:#3ecf6e; font-family:monospace; font-size:8pt;")

    def get_out_range_text(self) -> str:
        """Raw text of the frame range field, stripped."""
        return self.out_range.text().strip()

    def get_out_range_set(self):
        """Parsed frame set, or None if empty/invalid."""
        frames, err = parse_frame_range(self.out_range.text())
        return frames  # None if empty or if invalid

    def set_out_range_text(self, text: str):
        """Programmatically set the frame range (e.g. on project load)."""
        self.out_range.setText(text or "")

    def update_out_count(self, n_out: int):
        self.out_count_label.setText(f"~{n_out}f")

    def update_cache_display(self, n: int, total: int):
        """Viewer cache fill readout: green (full) / amber (partial) / dim (empty)."""
        if total <= 0:
            self._cache_label.setText("–")
            self._cache_label.setStyleSheet(
                "color:#3a3a42; font-family:monospace; font-size:8pt;")
            return
        pct   = int(n * 100 / total)
        color = "#3ecf6e" if n >= total else "#f5a623" if n > 0 else "#3a3a42"
        self._cache_label.setStyleSheet(
            f"color:{color}; font-family:monospace; font-size:8pt;")
        self._cache_label.setText(f"cached {n}/{total}  ({pct}%)")

    def has_sequence(self) -> bool:
        """True when a valid image sequence is currently loaded in the input directory."""
        return self._in_seq is not None

    def get_in_dir(self):     return self.in_dir.text().strip()
    def get_out_dir(self):
        base   = self.out_dir.text().strip()
        prefix = self.out_prefix.text().strip()
        if base and prefix:
            return os.path.join(base, prefix)
        return base
    def get_in_start(self):   return self._in_seq.first_frame if self._in_seq else 1001
    def get_in_end(self):     return self._in_seq.last_frame  if self._in_seq else 1072
    def get_in_ext(self):     return "." + self.in_fmt.currentText().lower()
    def get_out_ext(self):    return "." + self.out_fmt.currentText().lower()
    def get_in_prefix(self):  return self._in_seq.prefix if self._in_seq else ""
    def get_in_padding(self): return self.in_padding.value()
    def get_out_prefix(self):
        p = self.out_prefix.text()
        return p if p.endswith('.') else p + '.' if p else ''
    def get_out_padding(self):return self.out_padding.value()
    def get_out_start(self):  return self._in_seq.first_frame if self._in_seq else 1001
