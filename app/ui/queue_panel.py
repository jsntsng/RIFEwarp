"""Render queue panel."""
from __future__ import annotations
import os
import time
from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QProgressBar, QTextEdit, QSplitter, QHeaderView,
    QAbstractItemView, QFrame
)
from PyQt6.QtCore import Qt, pyqtSlot, QSize, QPointF, QRectF
from PyQt6.QtGui import QColor, QBrush, QPixmap, QPainter, QPen, QIcon

from core.rife_runner import RifeRunner
from core.timewarp import TimewarpCurve


TT = ("QToolTip{background:#1e1e22;color:#ffffff;"
      "border:1px solid #4a9eff;font-family:monospace;font-size:10px;}")


def _trash_icon(color: str = "#9a4a4a", px: int = 16) -> QIcon:
    """Draw a small trash-bin icon at the given color. Drawn rather than using a
    glyph/emoji so it renders consistently in the dark UI regardless of fonts."""
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(color)); pen.setWidthF(1.3)
    p.setPen(pen)
    w = px; h = px
    # Lid (horizontal line across the top) + small handle.
    lid_y = h * 0.30
    p.drawLine(QPointF(w*0.22, lid_y), QPointF(w*0.78, lid_y))
    p.drawLine(QPointF(w*0.40, lid_y), QPointF(w*0.40, h*0.20))
    p.drawLine(QPointF(w*0.60, lid_y), QPointF(w*0.60, h*0.20))
    p.drawLine(QPointF(w*0.40, h*0.20), QPointF(w*0.60, h*0.20))
    # Can body (tapered rectangle).
    p.drawLine(QPointF(w*0.28, lid_y), QPointF(w*0.33, h*0.82))
    p.drawLine(QPointF(w*0.72, lid_y), QPointF(w*0.67, h*0.82))
    p.drawLine(QPointF(w*0.33, h*0.82), QPointF(w*0.67, h*0.82))
    # Two vertical ribs.
    p.drawLine(QPointF(w*0.43, lid_y+h*0.10), QPointF(w*0.45, h*0.74))
    p.drawLine(QPointF(w*0.57, lid_y+h*0.10), QPointF(w*0.55, h*0.74))
    p.end()
    return QIcon(pm)


class JobStatus(Enum):
    READY   = "READY"
    RUNNING = "RUNNING"
    DONE    = "DONE"
    ERROR   = "ERROR"
    ABORTED = "ABORTED"


STATUS_COLORS = {
    JobStatus.READY:   "#6a6a72",
    JobStatus.RUNNING: "#4a9eff",
    JobStatus.DONE:    "#3ecf6e",
    JobStatus.ERROR:   "#e04a4a",
    JobStatus.ABORTED: "#f5a623",
}

COL_NAME=0; COL_IN=1; COL_OUT=2; COL_FRAMES=3; COL_SPEED=4
COL_MODEL=5; COL_STATUS=6; COL_ETA=7; COL_TOTAL=8; COL_PROGRESS=9; COL_ACTIONS=10; N_COLS=11
HEADERS = ["Shot","Input","Output","Frames","Avg Speed","Model","Status","ETA","Total Time","Progress",""]


@dataclass
class RetimeJob:
    name:           str
    in_dir:         str
    in_prefix:      str
    in_padding:     int
    in_ext:         str
    in_start:       int
    in_end:         int
    out_dir:        str
    out_prefix:     str
    out_padding:    int
    out_ext:        str
    out_start:      int
    curve:          TimewarpCurve
    rife_script:    str
    model_dir:      Optional[str]
    python_bin:     str
    model_name:     str
    # Settings with defaults
    scale:          float         = 1.0
    use_fp16:       bool          = True
    use_tile:       bool          = False
    preserve_alpha: bool          = True
    use_ensemble:   bool          = False
    scene_cut:      float         = 0.0
    # Optional per-job output frame range. None = full curve range.
    frame_range_str: str          = ""
    frame_range_set: Optional[set] = None
    status:         JobStatus     = JobStatus.READY
    progress:       int           = 0
    frames_done:    int           = 0
    frames_total:   int           = 0
    log_lines:      List[str]     = field(default_factory=list)
    runner:         Optional[RifeRunner] = None
    start_time:     Optional[float]      = None
    end_time:       Optional[float]      = None


def _fmt_time(secs: float) -> str:
    """Format seconds as Xm Ys or Xs."""
    secs = int(secs)
    if secs >= 3600:
        return f"{secs//3600}h {(secs%3600)//60}m"
    elif secs >= 60:
        return f"{secs//60}m {secs%60}s"
    return f"{secs}s"


class QueuePanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.jobs: List[RetimeJob] = []
        self._add_to_queue_cb = None   # set by main_window
        self.setStyleSheet(TT)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(0, 0, 0, 0)

        # Toolbar — fixed height, compact
        toolbar_widget = QWidget()
        toolbar_widget.setFixedHeight(32)
        toolbar_widget.setStyleSheet("background:#111113; border-bottom:1px solid #2a2a2e;")
        toolbar = QHBoxLayout(toolbar_widget)
        toolbar.setContentsMargins(6, 4, 6, 4)
        toolbar.setSpacing(6)

        BTN = ("QPushButton{font-family:monospace;font-size:10px;"
               "padding:2px 10px;height:22px;}")

        self.btn_add_queue = QPushButton("+ ADD TO QUEUE")
        self.btn_add_queue.clicked.connect(self._on_add_to_queue)
        self.btn_add_queue.setFixedHeight(22)
        self.btn_add_queue.setToolTip(
            "Add the current curve and settings as a new render job.\n"
            "Captures the curve, paths, model, and frame range as a snapshot —\n"
            "future changes to those settings do not affect this job.")
        self.btn_add_queue.setStyleSheet(BTN +
            "QPushButton{background:#1a2a3a;border:1px solid #4a9eff;color:#4a9eff;}"
            "QPushButton:hover{background:#2a3a2a;}")

        self.btn_run_all = QPushButton("▶ RUN ALL")
        self.btn_run_all.clicked.connect(self._run_next)
        self.btn_run_all.setFixedHeight(22)
        self.btn_run_all.setToolTip(
            "Start rendering all READY jobs in order, top to bottom.\n"
            "Jobs run one at a time. The next job starts when the current one finishes.\n"
            "Skips jobs that are already DONE, RUNNING, or ERROR.")
        self.btn_run_all.setStyleSheet(BTN +
            "QPushButton{background:#1a2a1a;border:1px solid #3ecf6e;color:#3ecf6e;}"
            "QPushButton:hover{background:#2a3a4a;}")

        self.btn_stop = QPushButton("■ STOP")
        self.btn_stop.clicked.connect(self._stop)
        self.btn_stop.setFixedHeight(22)
        self.btn_stop.setObjectName("dangerButton")
        self.btn_stop.setToolTip(
            "Abort the currently running render.\n"
            "Frames already written remain on disk.\n"
            "Job is marked ABORTED and the next READY job does not auto-start.")
        self.btn_stop.setStyleSheet(BTN +
            "QPushButton{background:#2a1a1a;border:1px solid #e04a4a;color:#e04a4a;}"
            "QPushButton:hover{background:#3a2a2a;}")

        self.btn_clear_done = QPushButton("CLEAR DONE")
        self.btn_clear_done.clicked.connect(self._clear_done)
        self.btn_clear_done.setFixedHeight(22)
        self.btn_clear_done.setToolTip(
            "Remove all DONE, ERROR, and ABORTED jobs from the queue.\n"
            "READY and RUNNING jobs are kept.\n"
            "Does not delete any rendered files from disk.")
        self.btn_clear_done.setStyleSheet(BTN +
            "QPushButton{background:#1e1e21;border:1px solid #2a2a2e;color:#6a6a72;}"
            "QPushButton:hover{background:#27272b;}")

        self.summary_label = QLabel("Queue empty")
        self.summary_label.setObjectName("statusLabel")
        self.summary_label.setStyleSheet("color:#3a3a42;font-family:monospace;font-size:10px;")

        toolbar.addWidget(self.btn_add_queue)
        toolbar.addWidget(self.btn_run_all)
        toolbar.addWidget(self.btn_stop)
        toolbar.addWidget(self.btn_clear_done)
        toolbar.addStretch()
        toolbar.addWidget(self.summary_label)
        layout.addWidget(toolbar_widget)

        # Horizontal splitter: table left, log panel right (resizable, log ~33%)
        from PyQt6.QtWidgets import QVBoxLayout as _VBL
        h_widget = QWidget()
        from PyQt6.QtWidgets import QHBoxLayout as _HBL
        h_layout = _HBL(h_widget)
        h_layout.setContentsMargins(0, 0, 0, 0)
        h_layout.setSpacing(0)

        h_splitter = QSplitter(Qt.Orientation.Horizontal)
        h_splitter.setHandleWidth(4)
        h_splitter.setStyleSheet("QSplitter::handle{background:#2a2a2e;}")

        self.table = QTableWidget(0, N_COLS)
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(COL_IN, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(COL_OUT, QHeaderView.ResizeMode.Stretch)
        # Data columns: fixed comfortable widths (not hugging text) so they get
        # breathing room — which also takes width away from the stretch trio,
        # keeping Shot/Input/Output from ballooning.
        fixed_widths = {
            COL_FRAMES: 96,
            COL_SPEED:  84,
            COL_MODEL:  72,
            COL_STATUS: 86,
            COL_ETA:    64,
            COL_TOTAL:  90,
        }
        for c, w in fixed_widths.items():
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(c, w)
        hh.setSectionResizeMode(COL_PROGRESS, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(COL_PROGRESS, 170)
        hh.setSectionResizeMode(COL_ACTIONS, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(COL_ACTIONS, 48)
        self.table.currentItemChanged.connect(
            lambda cur, prev: self._on_row_changed(self.table.currentRow()))
        h_splitter.addWidget(self.table)

        # Log panel
        log_widget = QWidget()
        log_vl = _VBL(log_widget)
        log_vl.setContentsMargins(0, 0, 0, 0)
        log_vl.setSpacing(0)

        self.log_header_label = QLabel("JOB LOG")
        self.log_header_label.setStyleSheet(
            "background:#111113; color:#3a3a42; font-family:monospace;"
            "font-size:9px; letter-spacing:1px; padding:4px 8px;"
            "border-left:1px solid #2a2a2e; border-bottom:1px solid #2a2a2e;")
        self.log_header_label.setFixedHeight(24)
        log_vl.addWidget(self.log_header_label)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setObjectName("logView")
        self.log_view.setPlaceholderText("Select a job to see its log…")
        self.log_view.setStyleSheet(
            "QTextEdit{background:#0e0e0f;color:#6a6a72;font-family:monospace;"
            "font-size:9px;border:none;border-left:1px solid #2a2a2e;padding:4px;}")
        log_vl.addWidget(self.log_view, stretch=1)

        h_splitter.addWidget(log_widget)
        # Set log panel to ~33% of total width
        h_splitter.setStretchFactor(0, 2)
        h_splitter.setStretchFactor(1, 1)
        h_layout.addWidget(h_splitter)
        layout.addWidget(h_widget)

    def _on_add_to_queue(self):
        if self._add_to_queue_cb:
            self._add_to_queue_cb()

    def add_job(self, job: RetimeJob):
        self.jobs.append(job)
        row = len(self.jobs) - 1
        self.table.insertRow(row)
        self._refresh_row(row)
        self._update_summary()

    def _refresh_row(self, row: int):
        job = self.jobs[row]
        kps = job.curve.sorted_keypoints()
        avg_speed = 0.0
        if len(kps) >= 2:
            total_in = kps[-1].in_frame - kps[0].in_frame
            total_out = kps[-1].out_frame - kps[0].out_frame
            if total_out > 0:
                avg_speed = total_in / total_out
        color = QColor(STATUS_COLORS[job.status])

        def cell(text, align=Qt.AlignmentFlag.AlignLeft):
            item = QTableWidgetItem(text)
            item.setTextAlignment(align | Qt.AlignmentFlag.AlignVCenter)
            return item

        self.table.setItem(row, COL_NAME,   cell(job.name))
        self.table.setItem(row, COL_IN,     cell(os.path.basename(job.in_dir.rstrip("/"))))
        self.table.setItem(row, COL_OUT,    cell(os.path.basename(job.out_dir.rstrip("/"))))
        # Show custom range if set, otherwise the full in_start–in_end
        if job.frame_range_set:
            n = len(job.frame_range_set)
            frames_text = f"{min(job.frame_range_set)}\u2013{max(job.frame_range_set)} ({n}f) *"
        else:
            frames_text = f"{job.in_start}\u2013{job.in_end}"
        frames_item = cell(frames_text, Qt.AlignmentFlag.AlignCenter)
        if job.frame_range_set:
            frames_item.setForeground(QBrush(QColor("#f5a623")))
            frames_item.setToolTip(f"Custom range: {job.frame_range_str}")
        self.table.setItem(row, COL_FRAMES, frames_item)
        self.table.setItem(row, COL_SPEED,  cell(f"{avg_speed*100:.0f}%", Qt.AlignmentFlag.AlignCenter))
        self.table.setItem(row, COL_MODEL,  cell(job.model_name))
        status_item = cell(job.status.value, Qt.AlignmentFlag.AlignCenter)
        status_item.setForeground(QBrush(color))
        self.table.setItem(row, COL_STATUS, status_item)

        # ETA — shown while running
        eta_text = "—"
        if job.status == JobStatus.RUNNING and job.start_time and job.frames_done > 0:
            elapsed  = time.time() - job.start_time
            rate     = job.frames_done / elapsed  # frames/sec
            remaining = max(0, job.frames_total - job.frames_done)
            secs     = remaining / rate if rate > 0 else 0
            eta_text = _fmt_time(secs)
        eta_item = cell(eta_text, Qt.AlignmentFlag.AlignCenter)
        if job.status == JobStatus.RUNNING:
            eta_item.setForeground(QBrush(QColor("#f5a623")))
        self.table.setItem(row, COL_ETA, eta_item)

        # TOTAL TIME — shown when done
        total_text = "—"
        if job.status == JobStatus.DONE and job.start_time and job.end_time:
            total_text = _fmt_time(job.end_time - job.start_time)
        total_item = cell(total_text, Qt.AlignmentFlag.AlignCenter)
        if job.status == JobStatus.DONE and total_text != "—":
            total_item.setForeground(QBrush(QColor("#3ecf6e")))
        self.table.setItem(row, COL_TOTAL, total_item)

        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(job.progress)
        bar.setTextVisible(True)
        bar.setFormat(f"{job.frames_done}/{job.frames_total}" if job.frames_total else "%p%")
        self.table.setCellWidget(row, COL_PROGRESS, bar)

        btn = QPushButton()
        btn.setIcon(_trash_icon("#8a8a92"))
        btn.setIconSize(QSize(16, 16))
        btn.setFixedWidth(28)
        btn.setStyleSheet(
            "QPushButton{background:#1e1e21;border:1px solid #2a2a2e;}"
            "QPushButton:hover{background:#3a2424;border:1px solid #e04a4a;}"
            "QPushButton:pressed{background:#161618;}" + TT)
        btn.setToolTip(
            "Remove this job from the queue.\n"
            "If the job is running, it is aborted first.\n"
            "Rendered files on disk are not deleted.")
        btn.clicked.connect(lambda _, r=row: self._remove_job(r))
        self.table.setCellWidget(row, COL_ACTIONS, btn)

    def _update_summary(self):
        total = len(self.jobs)
        running = sum(1 for j in self.jobs if j.status == JobStatus.RUNNING)
        done = sum(1 for j in self.jobs if j.status == JobStatus.DONE)
        self.summary_label.setText(f"{total} shots  ·  {running} running  ·  {done} done")

    def _remove_job(self, row: int):
        if row < len(self.jobs):
            job = self.jobs[row]
            if job.runner and job.runner.isRunning():
                job.runner.abort()
            self.jobs.pop(row)
            self.table.removeRow(row)
            self._update_summary()

    def _clear_done(self):
        for i in range(len(self.jobs) - 1, -1, -1):
            if self.jobs[i].status in (JobStatus.DONE, JobStatus.ERROR, JobStatus.ABORTED):
                self.jobs.pop(i)
                self.table.removeRow(i)
        self._update_summary()

    def _run_next(self):
        for i, job in enumerate(self.jobs):
            if job.status == JobStatus.READY:
                self._start_job(i)
                return

    def _start_job(self, row: int):
        job = self.jobs[row]
        # Always sync curve range from job's in_start before building frame list
        job.curve.set_range(job.in_start, job.in_end, job.in_start)
        frame_list = job.curve.build_frame_list()
        # Apply per-job custom output frame range filter if set
        if job.frame_range_set:
            requested = job.frame_range_set
            filtered = [(o, i) for (o, i) in frame_list if o in requested]
            missing = requested - {o for (o, _) in frame_list}
            if missing:
                # Frames in user's range but not produced by the curve —
                # log a warning but proceed with what we have
                m_sorted = sorted(missing)
                preview = ",".join(str(f) for f in m_sorted[:8])
                if len(m_sorted) > 8:
                    preview += f"...+{len(m_sorted)-8} more"
                job.log_lines.append(
                    f"WARN  {len(missing)} requested frame(s) not in curve range: {preview}")
            frame_list = filtered
        job.frames_total = len(frame_list)
        job.frames_done  = 0
        job.start_time   = time.time()
        job.end_time     = None
        if not frame_list:
            # Nothing to render — mark error and bail
            job.status = JobStatus.ERROR
            job.log_lines.append(
                "ERR   No frames to render. Check the frame range against the curve.")
            self._refresh_row(row)
            self._update_summary()
            return
        runner = RifeRunner(
            rife_script=job.rife_script, frame_list=frame_list,
            in_dir=job.in_dir, in_prefix=job.in_prefix, in_padding=job.in_padding,
            in_ext=job.in_ext, out_dir=job.out_dir, out_prefix=job.out_prefix,
            use_fp16=job.use_fp16, use_tile=job.use_tile,
            preserve_alpha=job.preserve_alpha,
            use_ensemble=job.use_ensemble, scale=job.scale,
            scene_cut=job.scene_cut,
            out_padding=job.out_padding, out_ext=job.out_ext,
            model_dir=job.model_dir, python_bin=job.python_bin,
        )
        job.runner = runner
        runner.signals.progress.connect(lambda d, t, r=row: self._on_progress(r, d, t))
        runner.signals.log.connect(lambda line, r=row: self._on_log(r, line))
        runner.signals.finished.connect(lambda ok, msg, r=row: self._on_finished(r, ok, msg))
        job.status = JobStatus.RUNNING
        self._refresh_row(row)
        self._update_summary()
        runner.start()

    def _on_progress(self, row, done, total):
        if row >= len(self.jobs): return
        job = self.jobs[row]
        job.frames_done  = done
        job.frames_total = total
        job.progress     = int(done / total * 100) if total else 0
        bar = self.table.cellWidget(row, COL_PROGRESS)
        if bar:
            bar.setValue(job.progress)
            bar.setFormat(f"{done}/{total}")
        # Update ETA — only after 10% of frames done to let rate stabilize
        eta_text = "—"
        if job.start_time and done > 0 and total > 0 and done >= max(1, total * 0.10):
            elapsed   = time.time() - job.start_time
            rate      = done / elapsed
            remaining = max(0, total - done)
            eta_text  = _fmt_time(remaining / rate) if rate > 0 else "—"
        eta_item = self.table.item(row, COL_ETA)
        if eta_item:
            eta_item.setText(eta_text)
        else:
            from PyQt6.QtWidgets import QTableWidgetItem
            from PyQt6.QtGui import QBrush, QColor
            item = QTableWidgetItem(eta_text)
            item.setTextAlignment(0x0004 | 0x0080)  # AlignHCenter | AlignVCenter
            item.setForeground(QBrush(QColor("#f5a623")))
            self.table.setItem(row, COL_ETA, item)

    def _on_log(self, row, line):
        if row >= len(self.jobs): return
        self.jobs[row].log_lines.append(line)
        if self.table.currentRow() == row:
            self.log_view.append(line)

    def _on_finished(self, row, ok, msg):
        if row >= len(self.jobs): return
        job = self.jobs[row]
        # Don't override ABORTED status set by _stop()
        if job.status != JobStatus.ABORTED:
            job.status = JobStatus.DONE if ok else JobStatus.ERROR
        job.end_time = time.time()
        job.log_lines.append(f"--- {msg} ---")
        if self.table.currentRow() == row:
            self.log_view.append(f"--- {msg} ---")
        self._refresh_row(row)
        self._update_summary()
        self._run_next()

    def _stop(self):
        for job in self.jobs:
            if job.runner and job.runner.isRunning():
                job.runner.abort()
                job.status = JobStatus.ABORTED

    def _on_row_changed(self, row: int):
        if row < 0 or row >= len(self.jobs):
            self.log_header_label.setText("JOB LOG")
            return
        self.log_header_label.setText(f"JOB LOG — {self.jobs[row].name}")
        self.log_view.clear()
        for line in self.jobs[row].log_lines:
            self.log_view.append(line)
