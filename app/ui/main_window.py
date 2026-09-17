"""MainWindow — assembles all panels."""
from __future__ import annotations
import os
import json
import copy

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QSplitter, QToolBar, QLabel, QPushButton,
    QStatusBar, QMessageBox, QTabWidget, QStackedWidget,
    QLineEdit, QSpinBox, QDoubleSpinBox, QTextEdit, QPlainTextEdit,
    QApplication, QComboBox,
)
from PyQt6.QtCore import Qt, QSettings, pyqtSignal
from PyQt6.QtGui import QAction, QKeySequence, QColor, QShortcut
from PyQt6.QtCore import QPointF

from core.timewarp import TimewarpCurve
from core.snapshot import SnapshotCollection, Snapshot
from core.project import save_project, load_project, collect_project, apply_project
from ui.curve_editor import CurveEditor
from ui.io_panel import IOPanel
from ui.settings_panel import SettingsPanel
from ui.queue_panel import QueuePanel, RetimeJob
from ui.sequence_viewer import SequenceViewer
from ui.dope_sheet import DopeSheet
from ui.snapshot_panel import SnapshotPanel


class MainWindow(QMainWindow):
    # Project-level frame/TC display mode. Both the viewer's dropdown and the
    # curve editor's dropdown read from / write to this single source of truth.
    # See set_display_mode() — emits on actual change only (deduped).
    displayModeChanged          = pyqtSignal(str)
    # Mirrors the viewer's "TC (metadata) available" probe result. Curve editor's
    # dropdown listens to this to enable/disable its TC (metadata) entry in
    # lockstep with the viewer's.
    tcMetadataAvailableChanged  = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        # Shared display mode state — initialised before any panel is built so
        # panels can read it during their own __init__.
        self._display_mode: str = "Frame"
        self._tc_metadata_available: bool = False
        try:
            import pathlib
            _ver = pathlib.Path(__file__).parent.parent.parent / "VERSION"
            _version = _ver.read_text().strip() if _ver.exists() else "dev"
        except Exception:
            _version = "dev"
        self._version = _version
        self.setWindowTitle(f"RIFEwarp v{_version}")
        self.setMinimumSize(1280, 800)
        self._current_project_path = None

        # Snapshots: start with a single default snapshot wrapping the curve
        # editor's initial curve, so the panel is never empty.
        self.snapshots = SnapshotCollection()
        self._suppress_scrubber_mirror = False

        self._build_menu()
        self._build_central()
        self._build_toolbar()
        self._build_statusbar()
        self._restore_geometry()
        self._detect_gpu()

        # Seed the snapshot collection with the curve_editor's initial curve so
        # the active snapshot and the canvas share the exact same object.
        initial_curve = self.curve_editor.curve
        initial = Snapshot(
            curve=initial_curve,
            in_point=self.viewer.scrubber.inPoint(),
            out_point=self.viewer.scrubber.outPoint(),
            name="default",
        )
        self.snapshots = SnapshotCollection(snapshots=[initial], active_id=initial.id)
        self.snapshot_panel.set_snapshots(self.snapshots)

        self.queue_panel._add_to_queue_cb = self._add_to_queue

        self.io_panel.sequenceChanged.connect(self._on_sequence_changed)
        self.curve_editor.curveChanged.connect(self._on_curve_changed)
        self.curve_editor.playheadMoved.connect(self._on_curve_playhead_moved)
        self.viewer.frameChanged.connect(self._on_viewer_frame_changed)
        self.viewer.cacheUpdated.connect(self.io_panel.update_cache_display)
        self.dope_sheet.curveChanged.connect(self._on_dope_changed)
        self.dope_sheet.playheadMoved.connect(self._on_dope_playhead_moved)

        # Mirror scrubber in/out into the active snapshot so they persist
        # across snapshot switches and project save.
        self.viewer.scrubber.inPointChanged.connect(self._mirror_in_point)
        self.viewer.scrubber.outPointChanged.connect(self._mirror_out_point)

        # Snapshot panel signals
        self.snapshot_panel.activeChanged.connect(self._on_active_snapshot_changed)

        # Shared display-mode wiring. Viewer and curve editor each have a
        # dropdown; both route their user-changes through MainWindow's
        # set_display_mode, and both subscribe to displayModeChanged to apply
        # the new mode locally. tcMetadataAvailableChanged fans out to the
        # curve editor's combo so its TC (metadata) entry mirrors the viewer's
        # enable/disable state.
        self.viewer.displayModeUserChanged.connect(self.set_display_mode)
        self.viewer.tcMetadataAvailableUserChanged.connect(
            self.set_tc_metadata_available)
        self.displayModeChanged.connect(self.viewer.apply_display_mode)
        self.displayModeChanged.connect(self.curve_editor.apply_display_mode)
        self.tcMetadataAvailableChanged.connect(
            self.curve_editor.apply_tc_metadata_available)
        # Click-on-disabled-mode feedback. Qt swallows clicks on disabled combo
        # items; both panels surface them as disabledModeClicked(text), which we
        # turn into a transient status-bar message.
        self.viewer.disabledModeClicked.connect(self._on_disabled_mode_clicked)
        self.curve_editor.disabledModeClicked.connect(self._on_disabled_mode_clicked)
        # Initial knob-formatter wiring (no signal needed — direct apply).
        self.curve_editor.apply_display_mode(self._display_mode)
        self.curve_editor.apply_tc_metadata_available(self._tc_metadata_available)

        # Wire dope sheet undo/redo through curve editor's stack
        canvas = self.curve_editor.canvas
        self.dope_sheet._undo_cb     = canvas.undo
        self.dope_sheet._redo_cb     = canvas.redo
        self.dope_sheet._snapshot_cb = self._dope_push_snapshot

        # Wire viewer range zoom to curve editor and dope sheet
        self.viewer.btn_range_zoom.clicked.connect(self._on_range_zoom_changed)

        # PageUp / PageDown — cycle snapshots when focus is in the snapshot
        # panel or curve editor area, but never while editing text.
        self._sc_next = QShortcut(QKeySequence(Qt.Key.Key_PageDown), self)
        self._sc_next.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._sc_next.activated.connect(lambda: self._cycle_snapshot(+1))
        self._sc_prev = QShortcut(QKeySequence(Qt.Key.Key_PageUp), self)
        self._sc_prev.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._sc_prev.activated.connect(lambda: self._cycle_snapshot(-1))

        self._on_sequence_changed()

    # ── Menu ──────────────────────────────────────────────────────────────────

    def _build_menu(self):
        mb = self.menuBar()

        file_menu = mb.addMenu("File")

        new_act = QAction("New Project", self)
        new_act.setShortcut("Ctrl+N")
        new_act.triggered.connect(self._new_project)
        file_menu.addAction(new_act)

        open_act = QAction("Open Project...", self)
        open_act.setShortcut(QKeySequence.StandardKey.Open)
        open_act.triggered.connect(self._open_project)
        file_menu.addAction(open_act)

        self._recent_menu = file_menu.addMenu("Open Recent")
        self._rebuild_recent_menu()

        file_menu.addSeparator()

        save_act = QAction("Save Project", self)
        save_act.setShortcut(QKeySequence.StandardKey.Save)
        save_act.triggered.connect(self._save_project)
        file_menu.addAction(save_act)

        saveas_act = QAction("Save Project As...", self)
        saveas_act.setShortcut("Ctrl+Shift+S")
        saveas_act.triggered.connect(self._save_project_as)
        file_menu.addAction(saveas_act)

        saveup_act = QAction("Save Up Version", self)
        saveup_act.setShortcut("Alt+Shift+S")
        saveup_act.triggered.connect(self._save_up_version)
        file_menu.addAction(saveup_act)

        file_menu.addSeparator()

        save_curve_act = QAction("Export Curve...", self)
        save_curve_act.triggered.connect(self._save_curve)
        file_menu.addAction(save_curve_act)

        load_curve_act = QAction("Import Curve...", self)
        load_curve_act.triggered.connect(self._load_curve)
        file_menu.addAction(load_curve_act)

        file_menu.addSeparator()

        quit_act = QAction("Quit", self)
        quit_act.setShortcut("Ctrl+Q")
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        # Edit menu
        edit_menu = mb.addMenu("Edit")

        undo_act = QAction("Undo", self)
        undo_act.setShortcut("Ctrl+Z")
        undo_act.triggered.connect(lambda: self.curve_editor.canvas.undo())
        edit_menu.addAction(undo_act)

        redo_act = QAction("Redo", self)
        redo_act.setShortcut("Ctrl+Y")
        redo_act.triggered.connect(lambda: self.curve_editor.canvas.redo())
        edit_menu.addAction(redo_act)

        edit_menu.addSeparator()

        sel_all_act = QAction("Select All Keyframes", self)
        sel_all_act.setShortcut("Ctrl+A")
        sel_all_act.triggered.connect(self._select_all_keyframes)
        edit_menu.addAction(sel_all_act)

        del_sel_act = QAction("Delete Selected Keyframes", self)
        del_sel_act.setShortcut("Delete")
        del_sel_act.triggered.connect(self._delete_selected_keyframes)
        edit_menu.addAction(del_sel_act)

        edit_menu.addSeparator()

        reset_linear_act = QAction("Reset Curve to Linear", self)
        reset_linear_act.triggered.connect(lambda: self._reset_curve(1.0))
        edit_menu.addAction(reset_linear_act)

        reset_default_act = QAction("Reset Curve to Default", self)
        reset_default_act.triggered.connect(lambda: self._reset_curve(1.0))
        edit_menu.addAction(reset_default_act)

        # Help menu
        help_menu = mb.addMenu("Help")
        about_act = QAction("About RIFEwarp", self)
        about_act.triggered.connect(self._show_about)
        help_menu.addAction(about_act)

    # ── Central ───────────────────────────────────────────────────────────────

    def _build_central(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        outer_v = QSplitter(Qt.Orientation.Vertical)

        h_splitter = QSplitter(Qt.Orientation.Horizontal)

        self.io_panel = IOPanel()
        self.io_panel.setMinimumWidth(180)
        h_splitter.addWidget(self.io_panel)

        self.viewer = SequenceViewer()
        self.viewer.setMinimumHeight(200)
        h_splitter.addWidget(self.viewer)

        self.settings_panel = SettingsPanel()
        self.settings_panel.setMinimumWidth(180)
        h_splitter.addWidget(self.settings_panel)

        h_splitter.setStretchFactor(0, 0)
        h_splitter.setStretchFactor(1, 1)
        h_splitter.setStretchFactor(2, 0)
        h_splitter.setSizes([420, 9999, 420])
        outer_v.addWidget(h_splitter)

        bottom_widget = QWidget()
        bottom_widget.setMinimumHeight(200)
        bottom_layout = QHBoxLayout(bottom_widget)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(0)

        btn_strip = QWidget()
        btn_strip.setFixedWidth(180)
        btn_strip.setStyleSheet(
            "background:#0e0e0f; border-right:1px solid #2a2a2e;")
        from PyQt6.QtWidgets import QVBoxLayout as VBL
        btn_strip_layout = VBL(btn_strip)
        btn_strip_layout.setContentsMargins(0, 4, 0, 4)
        btn_strip_layout.setSpacing(2)

        self._bottom_active_style = (
            "QPushButton{background:rgba(74,158,255,0.15);"
            "border:none;border-left:2px solid #4a9eff;"
            "color:#4a9eff;font-family:monospace;font-size:11pt;"
            "letter-spacing:1px;padding:10px 2px;}"
        )
        self._bottom_inactive_style = (
            "QPushButton{background:transparent;border:none;"
            "border-left:2px solid transparent;color:#4a4a52;"
            "font-family:monospace;font-size:11pt;letter-spacing:2px;"
            "padding:10px 2px;}"
            "QPushButton:hover{color:#a0a0aa;border-left:2px solid #3a3a40;}"
        )

        self._btn_curve = QPushButton("CURVE")
        self._btn_curve.setStyleSheet(self._bottom_active_style)
        self._btn_curve.clicked.connect(lambda: self._switch_bottom_panel(0))

        self._btn_queue = QPushButton("QUEUE")
        self._btn_queue.setStyleSheet(self._bottom_inactive_style)
        self._btn_queue.clicked.connect(lambda: self._switch_bottom_panel(1))

        self._bottom_btns = [self._btn_curve, self._btn_queue]
        btn_strip_layout.addWidget(self._btn_curve)
        btn_strip_layout.addWidget(self._btn_queue)
        btn_strip_layout.addStretch()
        bottom_layout.addWidget(btn_strip)

        self._bottom_stack = QStackedWidget()

        curve_tabs = QTabWidget()
        curve_tabs.setStyleSheet(
            "QTabWidget::pane{border:none;border-top:1px solid #2a2a2e;}"
            "QTabBar::tab{background:#161618;border:1px solid #2a2a2e;"
            "border-bottom:none;color:#6a6a72;font-family:monospace;"
            "font-size:9pt;letter-spacing:1px;padding:4px 14px;margin-right:2px;}"
            "QTabBar::tab:selected{background:#1e1e21;color:#e8e8ec;"
            "border-top:1px solid #4a9eff;}"
            "QTabBar::tab:hover{background:#1e1e21;color:#a0a0aa;}"
        )

        self.curve_editor = CurveEditor()
        self.curve_editor.setMinimumHeight(160)
        curve_tabs.addTab(self.curve_editor, "CURVE EDITOR")

        self.dope_sheet = DopeSheet()
        curve_tabs.addTab(self.dope_sheet, "DOPE SHEET")

        # Curve area + snapshot panel side by side. Snapshot panel sizing
        # mirrors the top-right settings panel (min 180px, initial 420px).
        self.snapshot_panel = SnapshotPanel()
        self.snapshot_panel.setMinimumWidth(180)
        curve_split = QSplitter(Qt.Orientation.Horizontal)
        curve_split.addWidget(curve_tabs)
        curve_split.addWidget(self.snapshot_panel)
        curve_split.setStretchFactor(0, 1)
        curve_split.setStretchFactor(1, 0)
        curve_split.setSizes([9999, 420])

        self._bottom_stack.addWidget(curve_split)

        self.queue_panel = QueuePanel()
        self._bottom_stack.addWidget(self.queue_panel)

        bottom_layout.addWidget(self._bottom_stack, stretch=1)
        outer_v.addWidget(bottom_widget)
        outer_v.setSizes([520, 280])
        root.addWidget(outer_v)

    # ── Toolbar ───────────────────────────────────────────────────────────────

    def _build_toolbar(self):
        tb = QToolBar("Main Toolbar")
        tb.setMovable(False)
        tb.setStyleSheet("QToolBar { spacing: 8px; padding: 4px 8px; }")
        self.addToolBar(tb)
        self.gpu_label = QLabel("  GPU: detecting...")
        tb.addWidget(self.gpu_label)
        self.vram_bar_widget = QWidget()
        self.vram_bar_widget.setFixedSize(120, 14)
        self.vram_bar_widget.setToolTip("VRAM usage")
        tb.addWidget(self.vram_bar_widget)
        self._vram_used  = 0
        self._vram_total = 1
        from PyQt6.QtCore import QTimer
        self._vram_timer = QTimer(self)
        self._vram_timer.timeout.connect(self._update_vram)
        self._vram_timer.start(3000)

    def _build_statusbar(self):
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready — load an input sequence to begin.")

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _switch_bottom_panel(self, index: int):
        self._bottom_stack.setCurrentIndex(index)
        for i, btn in enumerate(self._bottom_btns):
            btn.setStyleSheet(
                self._bottom_active_style if i == index
                else self._bottom_inactive_style
            )

    def _on_sequence_changed(self):
        in_start = self.io_panel.get_in_start()
        in_end   = self.io_panel.get_in_end()

        curve = self.curve_editor.curve
        range_changed = (curve.in_start != in_start or curve.in_end != in_end)

        # Update every snapshot's curve range metadata so the editor's x-axis
        # tracks the source sequence. Keypoints are deliberately untouched —
        # a sequence change must never destroy user curve data, even when the
        # new range is smaller and some keypoints end up off-canvas. They
        # persist in snap.curve.keypoints and reappear if the range widens.
        # Exception: a curve that's still the unedited default (created at
        # app init from the io_panel fallback range) is rebuilt as a fresh
        # 1:1 identity curve spanning the newly loaded sequence, instead of
        # keeping the stale default span.
        for snap in self.snapshots.snapshots:
            if snap.curve.is_untouched_default():
                snap.curve.reset_to_identity(in_start, in_end, in_start)
            else:
                snap.curve.set_range(in_start, in_end, in_start)

        if range_changed:
            self.curve_editor.canvas.reset_view()

        self.curve_editor.canvas.update()
        self._on_curve_changed()
        self.dope_sheet.set_curve(self.curve_editor.curve)

        self.viewer.set_source_sequence(
            directory=self.io_panel.get_in_dir(),
            prefix=self.io_panel.get_in_prefix(),
            padding=self.io_panel.get_in_padding(),
            ext=self.io_panel.get_in_ext(),
            start=in_start,
            end=in_end,
        )

    def _on_curve_changed(self):
        curve = self.curve_editor.curve
        n_out = curve.out_frame_count
        self.io_panel.update_out_count(n_out)

        kps = curve.sorted_keypoints()
        if kps:
            total_out = kps[-1].out_frame
            total_in  = kps[-1].in_frame - kps[0].in_frame
            if total_out > 0:
                avg = total_in / total_out * 100
                self.status_bar.showMessage(
                    f"Output: ~{n_out} frames  |  Avg speed: {avg:.0f}%  |  Keys: {len(kps)}")

        self.dope_sheet.refresh()
        kf_abs = [curve.out_start + int(kp.out_frame) for kp in kps]
        self.viewer.set_keyframes(kf_abs)
        frame_map = curve.build_frame_list()
        self.viewer.set_frame_map(frame_map)
        self.viewer.set_output_sequence(
            directory=self.io_panel.get_out_dir(),
            prefix=self.io_panel.get_out_prefix(),
            padding=self.io_panel.get_out_padding(),
            ext=self.io_panel.get_out_ext(),
            start=self.io_panel.get_out_start(),
            end=self.io_panel.get_out_start() + n_out - 1,
        )

    def _on_curve_playhead_moved(self, out_frame: int):
        self.viewer.go_to_out_frame(out_frame)

    def _on_viewer_frame_changed(self, out_frame: int):
        self.curve_editor.set_playhead(out_frame)
        self.curve_editor.canvas.update()
        self.dope_sheet.set_playhead(out_frame)

    def _on_dope_changed(self):
        self.curve_editor.canvas.update()
        self._on_curve_changed()

    def _on_dope_playhead_moved(self, out_frame: int):
        self.viewer.go_to_out_frame(out_frame)
        self.curve_editor.set_playhead(out_frame)

    def _add_to_queue(self):
        in_dir  = self.io_panel.get_in_dir()
        out_dir = self.io_panel.get_out_dir()
        if not in_dir:
            QMessageBox.warning(self, "Missing Input",
                "Please set an input sequence directory.")
            return
        if not out_dir:
            QMessageBox.warning(self, "Missing Output",
                "Please set an output directory.")
            return

        # Use output prefix as job name — no dialog needed
        name = self.io_panel.out_prefix.text().strip().rstrip(".").rstrip("_").rstrip("-") or \
               os.path.basename(out_dir.rstrip("/"))

        curve_copy = copy.deepcopy(self.curve_editor.curve)
        # Snapshot the frame range field — locked into the job at queue time
        range_text = self.io_panel.get_out_range_text()
        range_set  = self.io_panel.get_out_range_set()  # None if empty/invalid

        # Snapshot identity stamp — captured at queue time, frozen strings.
        # Defensive ""/None fallback in case the collection is empty (shouldn't
        # happen given the >=1 invariant, but cheap insurance).
        snap_name = ""
        snap_color = None
        if len(self.snapshots) > 0:
            active = self.snapshots.active()
            snap_name = active.name
            snap_color = active.color

        job = RetimeJob(
            name=name,
            in_dir=in_dir,
            in_prefix=self.io_panel.get_in_prefix(),
            in_padding=self.io_panel.get_in_padding(),
            in_ext=self.io_panel.get_in_ext(),
            in_start=self.io_panel.get_in_start(),
            in_end=self.io_panel.get_in_end(),
            out_dir=out_dir,
            out_prefix=self.io_panel.get_out_prefix(),
            out_padding=self.io_panel.get_out_padding(),
            out_ext=self.io_panel.get_out_ext(),
            out_start=self.io_panel.get_out_start(),
            curve=curve_copy,
            rife_script=self.settings_panel.get_rife_script(),
            model_dir=self.settings_panel.get_model_dir(),
            python_bin=self.settings_panel.get_python_bin(),
            scale=self.settings_panel.get_scale(),
            model_name=self.settings_panel.get_model_name(),
            use_fp16=self.settings_panel.get_use_fp16(),
            use_tile=self.settings_panel.get_use_tile(),
            preserve_alpha=self.settings_panel.get_preserve_alpha(),
            use_ensemble=self.settings_panel.get_use_ensemble(),
            scene_cut=self.settings_panel.get_scene_cut(),
            frame_range_str=range_text,
            frame_range_set=range_set,
            snapshot_name=snap_name,
            snapshot_color=snap_color,
        )
        self.queue_panel.add_job(job)
        self._switch_bottom_panel(1)
        self.status_bar.showMessage(
            f"Added '{name}' to queue. {len(self.queue_panel.jobs)} jobs total.")

    # ── Project ───────────────────────────────────────────────────────────────

    def _select_all_keyframes(self):
        kps = self.curve_editor.curve.sorted_keypoints()
        self.curve_editor.canvas._selected_frames = {kp.out_frame for kp in kps}
        self.curve_editor.canvas.update()
        self.dope_sheet._selected_frames = {kp.out_frame for kp in kps}
        self.dope_sheet.update()

    def _delete_selected_keyframes(self):
        canvas = self.curve_editor.canvas
        kps = self.curve_editor.curve.sorted_keypoints()
        n   = len(kps)
        to_remove = [
            i for i, kp in enumerate(kps)
            if kp.out_frame in canvas._selected_frames
            and i != 0 and i != n - 1
            and len(self.curve_editor.curve.keypoints) > 2
        ]
        if to_remove:
            canvas._push_undo()
        for i in sorted(to_remove, reverse=True):
            self.curve_editor.curve.remove_keypoint(i)
        canvas._selected_frames = set()
        self._on_curve_changed()
        canvas.update()
        self.dope_sheet.update()

    def _reset_curve(self, speed=1.0):
        self.curve_editor.canvas._reset(speed)
        self.dope_sheet.set_curve(self.curve_editor.curve)
        self._on_curve_changed()

    def _save_up_version(self):
        import re as _re
        if not self._current_project_path:
            self._save_project_as()
            return
        p         = self._current_project_path
        name      = os.path.splitext(os.path.basename(p))[0]
        directory = os.path.dirname(p)
        # Find last version token vNNNN and increment
        match = _re.search(r'v(\d+)', name)
        if match:
            num      = int(match.group(1))
            padding  = len(match.group(1))
            new_name = name[:match.start(1)] + f"{num+1:0{padding}d}" + name[match.end(1):]
        else:
            new_name = name + "_v0002"
        new_path = os.path.join(directory, new_name + ".rtp")
        self._write_project(new_path)

    def _update_prefix_from_project(self, path: str):
        """Set output prefix to project filename (without extension) + period."""
        name = os.path.splitext(os.path.basename(path))[0]
        self.io_panel.out_prefix.setText(name)

    def _on_range_zoom_changed(self):
        zoomed = self.viewer.btn_range_zoom._active
        in_pt  = self.viewer.scrubber.inPoint()
        out_pt = self.viewer.scrubber.outPoint()
        ce     = self.curve_editor.canvas
        ds     = self.dope_sheet.canvas

        if zoomed:
            # Convert absolute in/out to relative (curve space)
            in_start = self.io_panel.get_in_start()
            rel_in   = float(in_pt  - in_start)
            rel_out  = float(out_pt - in_start)
            out_total = ce._out_total() or 1.0
            in_total  = ce._in_total()  or 1.0
            pad_out   = (rel_out - rel_in) * 0.1

            # X: frame the requested out-range.
            ce._zoom_x = max(0.1, out_total / max(rel_out - rel_in + pad_out*2, 1.0))
            ce._origin = QPointF(max(0, rel_in - pad_out), 0)

            # Y: fit to the ACTUAL keypoints whose out_frame falls within the
            # zoomed range (plus the curve value at the range edges), rather than
            # assuming a 1:1 diagonal. This frames the keypoints vertically.
            kps = ce.curve.sorted_keypoints()
            in_vals = [kp.in_frame for kp in kps
                       if rel_in - pad_out <= kp.out_frame <= rel_out + pad_out]
            # Include the curve's value at the range edges so the visible curve
            # segment is fully framed even if no keypoint sits exactly there.
            try:
                in_vals.append(ce.curve.evaluate(max(0.0, rel_in)))
                in_vals.append(ce.curve.evaluate(rel_out))
            except Exception:
                pass

            if in_vals:
                y_min, y_max = min(in_vals), max(in_vals)
                y_span = max(y_max - y_min, 1.0)
                pad_y  = y_span * 0.15
                y_min -= pad_y; y_max += pad_y
                ce._zoom_y = max(0.1, min(200.0, in_total / max(y_max - y_min, 1.0)))
                ce._origin = QPointF(ce._origin.x(), y_min)
            else:
                # No keypoints in range — leave Y unzoomed.
                ce._zoom_y = 1.0
                ce._origin = QPointF(ce._origin.x(), 0.0)

            ds._view_min = rel_in  - pad_out
            ds._view_max = rel_out + pad_out
        else:
            ce._origin = QPointF(0.0, 0.0)
            ce._zoom_x = 1.0
            ce._zoom_y = 1.0
            ds._view_min = None
            ds._view_max = None

        ce.update()
        ds.update()

    def _dope_push_snapshot(self, snapshot=None):
        """Push a snapshot to the curve editor undo stack from the dope sheet."""
        import copy
        canvas = self.curve_editor.canvas
        state  = snapshot if snapshot is not None else copy.deepcopy(canvas.curve.keypoints)
        canvas._undo_stack.append(state)
        if len(canvas._undo_stack) > canvas._undo_max:
            canvas._undo_stack.pop(0)
        canvas._redo_stack.clear()

    # ── Display mode (shared between viewer and curve editor) ────────────────

    def display_mode(self) -> str:
        return self._display_mode

    def set_display_mode(self, mode: str):
        """Single source of truth. Dedup'd; emits only on actual change."""
        if mode not in ("Frame", "TC (metadata)", "TC (fps)"):
            return
        if mode == self._display_mode:
            return
        self._display_mode = mode
        self.displayModeChanged.emit(mode)

    def tc_metadata_available(self) -> bool:
        return self._tc_metadata_available

    def set_tc_metadata_available(self, available: bool):
        """Mirrors the viewer's TC-probe result. Dedup'd."""
        available = bool(available)
        if available == self._tc_metadata_available:
            return
        self._tc_metadata_available = available
        self.tcMetadataAvailableChanged.emit(available)

    def _on_disabled_mode_clicked(self, mode_text: str):
        """Either dropdown reported a click on a disabled item. Post a one-shot
        status-bar message explaining why it can't be selected right now."""
        if mode_text == "TC (metadata)":
            msg = ("TC (metadata) not available "
                   "— this sequence has no embedded timecode.")
        else:
            msg = f"{mode_text} is not available for this sequence."
        self.status_bar.showMessage(msg, 4000)

    # ── Snapshots ─────────────────────────────────────────────────────────────

    def set_snapshots(self, snaps: SnapshotCollection):
        """Replace the live snapshot collection (called by project load).
        Binds the panel and fans out to all dependent UI."""
        self.snapshots = snaps
        self.snapshot_panel.set_snapshots(snaps)
        self._bind_active_snapshot()

    def _bind_active_snapshot(self):
        """Point the curve editor, dope sheet, and viewer at the active
        snapshot's data. Resets undo (cross-snapshot undo is confusing)."""
        snap = self.snapshots.active()
        # Bind curve to editor and dope sheet — same TimewarpCurve instance.
        self.curve_editor.set_curve(snap.curve)
        self.dope_sheet.set_curve(snap.curve)
        canvas = self.curve_editor.canvas
        canvas._undo_stack.clear()
        canvas._redo_stack.clear()
        canvas.reset_view()
        # Restore scrubber in/out without re-mirroring back into the snapshot.
        self._suppress_scrubber_mirror = True
        try:
            self.viewer.scrubber.setInPoint(snap.in_point)
            self.viewer.scrubber.setOutPoint(snap.out_point)
        finally:
            self._suppress_scrubber_mirror = False
        # Refresh derived UI (output frame count, dope sheet, viewer frame map).
        self._on_curve_changed()

    def _on_active_snapshot_changed(self):
        self._bind_active_snapshot()

    def _mirror_in_point(self, v: int):
        if self._suppress_scrubber_mirror:
            return
        try:
            self.snapshots.active().in_point = int(v)
        except RuntimeError:
            pass

    def _mirror_out_point(self, v: int):
        if self._suppress_scrubber_mirror:
            return
        try:
            self.snapshots.active().out_point = int(v)
        except RuntimeError:
            pass

    def _cycle_snapshot(self, direction: int):
        """PageUp/PageDown handler. Bails out if the focus widget is a text-
        entry field, so typing in a QLineEdit / spin box doesn't cycle."""
        fw = QApplication.focusWidget()
        TEXT_TYPES = (QLineEdit, QSpinBox, QDoubleSpinBox,
                      QTextEdit, QPlainTextEdit)
        if isinstance(fw, TEXT_TYPES):
            return
        # An editable QComboBox embeds a QLineEdit; the isinstance check above
        # catches that QLineEdit when focus is on the combo's edit field.
        if isinstance(fw, QComboBox) and fw.isEditable():
            return
        # Limit positive scope to "inside snapshot panel or curve editor".
        if fw is not None:
            w = fw
            in_scope = False
            while w is not None:
                if w is self.snapshot_panel or w is self.curve_editor:
                    in_scope = True
                    break
                w = w.parentWidget()
            if not in_scope:
                return
        self.snapshot_panel.cycle(direction)

    def _show_about(self):
        QMessageBox.about(self, "About RIFEwarp",
            f"<b>RIFEwarp</b> v{self._version}<br><br>"
            "VFX frame interpolation and retiming tool.<br>"
            "Powered by ECCV2022-RIFE.")

    # ── Recent projects ───────────────────────────────────────────────────────

    _RECENT_MAX = 10
    _RECENT_KEY = "recent_projects"

    def _load_recent(self):
        import json, os
        cfg = os.path.expanduser("~/.config/RIFEwarp/recent.json")
        if os.path.exists(cfg):
            try:
                with open(cfg) as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def _save_recent(self, recent: list):
        import json, os
        cfg_dir = os.path.expanduser("~/.config/RIFEwarp")
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, "recent.json"), "w") as f:
            json.dump(recent, f, indent=2)

    def _add_to_recent(self, path: str):
        recent = self._load_recent()
        if path in recent:
            recent.remove(path)
        recent.insert(0, path)
        recent = [p for p in recent if os.path.exists(p)]
        recent = recent[:self._RECENT_MAX]
        self._save_recent(recent)
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self):
        self._recent_menu.clear()
        recent = [p for p in self._load_recent() if os.path.exists(p)]
        if not recent:
            empty = QAction("No recent projects", self)
            empty.setEnabled(False)
            self._recent_menu.addAction(empty)
            return
        for path in recent:
            name = os.path.basename(path)
            act  = QAction(name, self)
            act.setToolTip(path)
            act.triggered.connect(lambda checked=False, p=path: self._open_project_path(p))
            self._recent_menu.addAction(act)
        self._recent_menu.addSeparator()
        clear_act = QAction("Clear Recent", self)
        clear_act.triggered.connect(self._clear_recent)
        self._recent_menu.addAction(clear_act)

    def _clear_recent(self):
        self._save_recent([])
        self._rebuild_recent_menu()

    def _new_project(self):
        self._current_project_path = None
        self.setWindowTitle(f"RIFEwarp v{self._version}")
        # Clear I/O panel
        self.io_panel.in_dir.setText("")
        self.io_panel.out_dir.setText("")
        self.io_panel.out_prefix.setText("")
        self.io_panel._in_seq = None
        self.io_panel.in_range_label.setText("–")
        self.io_panel.in_status.setText("no sequence loaded")
        self.io_panel.in_status.setStyleSheet("color: #6a6a72;")
        self.io_panel.out_count_label.setText("~? frames")
        # Clear queue
        self.queue_panel.jobs.clear()
        self.queue_panel.table.setRowCount(0)
        self.queue_panel.log_view.clear()
        self.queue_panel._update_summary()
        # Reset snapshots to a single fresh default that points at the curve
        # editor's current curve object (which _on_sequence_changed will reset).
        default = Snapshot(
            curve=self.curve_editor.curve,
            in_point=self.viewer.scrubber.inPoint(),
            out_point=self.viewer.scrubber.outPoint(),
            name="default",
        )
        self.snapshots = SnapshotCollection(snapshots=[default], active_id=default.id)
        self.snapshot_panel.set_snapshots(self.snapshots)
        # Reset curve and viewer
        self._on_sequence_changed()

    def _open_project(self):
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Project", "", "RIFEwarp Project (*.rtp)")
        if path:
            self._open_project_path(path)

    def _open_project_path(self, path: str):
        try:
            data = load_project(path)
            # Clear queue BEFORE applying — mirrors _new_project. Without this,
            # the previous project's jobs survive into the loaded one (audit
            # Finding 2). Order matters: any signals fired during apply_project
            # must not see stale queue state.
            self.queue_panel.jobs.clear()
            self.queue_panel.table.setRowCount(0)
            self.queue_panel.log_view.clear()
            self.queue_panel._update_summary()
            apply_project(self, data)
            self._current_project_path = path
            self.setWindowTitle(f"RIFEwarp v{self._version} — {os.path.basename(path)}")
            self.status_bar.showMessage(f"Project loaded: {path}")
            self._update_prefix_from_project(path)
            self._add_to_recent(path)
        except Exception as e:
            QMessageBox.critical(self, "Load Failed", str(e))

    def _save_project(self):
        if self._current_project_path:
            self._write_project(self._current_project_path)
        else:
            self._save_project_as()

    def _save_project_as(self):
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Project As", "", "RIFEwarp Project (*.rtp)")
        if path:
            if not path.endswith(".rtp"):
                path += ".rtp"
            self._write_project(path)

    def _write_project(self, path: str):
        try:
            data = collect_project(self)
            save_project(path, data)
            self._current_project_path = path
            self.setWindowTitle(f"RIFEwarp v{self._version} — {os.path.basename(path)}")
            self.status_bar.showMessage(f"Project saved: {path}")
            self._update_prefix_from_project(path)
            self._add_to_recent(path)
        except Exception as e:
            QMessageBox.critical(self, "Save Failed", str(e))

    def _save_curve(self):
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Curve", "", "JSON (*.json)")
        if path:
            with open(path, "w") as f:
                json.dump(self.curve_editor.curve.to_dict(), f, indent=2)
            self.status_bar.showMessage(f"Curve exported to {path}")

    def _load_curve(self):
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Curve", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path) as f:
                d = json.load(f)
            curve = TimewarpCurve.from_dict(d)
            # Apply range from current sequence so curve fits
            in_start = self.io_panel.get_in_start()
            in_end   = self.io_panel.get_in_end()
            curve.set_range(in_start, in_end, in_start)
            # Replace the active snapshot's curve in place so the snapshot list
            # stays intact.
            self.snapshots.active().curve = curve
            self.curve_editor.set_curve(curve)
            self.dope_sheet.set_curve(curve)
            self.curve_editor.canvas.reset_view()
            self._on_curve_changed()
            self.status_bar.showMessage(f"Curve imported from {path}")
        except Exception as e:
            QMessageBox.critical(self, "Import Failed", str(e))

    # ── GPU ───────────────────────────────────────────────────────────────────

    def _detect_gpu(self):
        import subprocess
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3)
            if result.returncode == 0:
                line = result.stdout.strip().split("\n")[0]
                name, mem = line.split(",")
                self._vram_total = int(mem.strip())
                self.gpu_label.setText(
                    f"  GPU: {name.strip()} · {self._vram_total:,}MB")
                self.gpu_label.setStyleSheet("color: #3ecf6e;")
                self._update_vram()
                return
        except Exception:
            pass
        self.gpu_label.setText("  GPU: not detected")
        self.gpu_label.setStyleSheet("color: #f5a623;")

    def _update_vram(self):
        import subprocess
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                self._vram_used = int(result.stdout.strip().split("\n")[0].strip())
                pct = self._vram_used / max(1, self._vram_total)
                used_color = "#e04a4a" if pct > 0.85 else "#f5a623" if pct > 0.65 else "#3ecf6e"
                self.vram_bar_widget.setStyleSheet(
                    f"background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                    f"stop:0 {used_color},stop:{pct:.3f} {used_color},"
                    f"stop:{min(pct+0.001,1):.3f} #1e1e21,stop:1 #1e1e21);"
                    f"border:1px solid #2a2a2e;border-radius:2px;")
                self.vram_bar_widget.setToolTip(
                    f"VRAM: {self._vram_used:,} / {self._vram_total:,} MB  ({pct*100:.0f}%)")
        except Exception:
            pass

    def _restore_geometry(self):
        settings = QSettings("vfx-local", "RifeRetime")
        geom = settings.value("geometry")
        if geom:
            self.restoreGeometry(geom)

    def closeEvent(self, event):
        settings = QSettings("vfx-local", "RifeRetime")
        settings.setValue("geometry", self.saveGeometry())
        super().closeEvent(event)
