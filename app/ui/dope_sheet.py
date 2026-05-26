"""
Dope sheet — drag tracked by object identity, selection preserved after drag.
"""
from __future__ import annotations
import math
from typing import Optional, Set

from PyQt6.QtWidgets import QWidget, QSizePolicy, QHBoxLayout, QVBoxLayout, QLabel, QLineEdit, QPushButton, QFrame
from PyQt6.QtCore import Qt, QPointF, QRectF, pyqtSignal
from PyQt6.QtGui import (
    QPainter, QPen, QBrush, QColor, QPolygonF,
    QFont, QFontMetrics, QMouseEvent
)

from core.timewarp import TimewarpCurve, Keypoint, InterpMode

C_BG       = QColor("#0e0e0f")
C_TRACK    = QColor("#161618")
C_GRID     = QColor("#1e1e21")
C_GRID2    = QColor("#2a2a2e")
C_KP       = QColor("#4a9eff")
C_KP_SEL   = QColor("#ffffff")
C_KP_MULTI = QColor("#f5a623")
C_KP_HOVER = QColor("#80c4ff")
C_KP_ANCH  = QColor("#9b7cf4")
C_LABEL    = QColor("#6a6a72")
C_LABEL2   = QColor("#a0a0aa")
C_AMBER    = QColor("#ffb830")
C_RB       = QColor(74, 158, 255, 35)
C_RB_B     = QColor("#4a9eff")

PAD_L   = 54
PAD_R   = 18
PAD_T   = 44
PAD_B   = 24
TRACK_H = 40
DIAMOND = 7
SNAP_PX = 12
SNAP_PH = 8
# Vertical gap between the frame-number label row and the diamond row,
# and where the playhead line stops so it never slices through the numbers.
LABEL_GAP   = 16
PLAYHEAD_TOP_INSET = 6

TARGET_NONE     = "none"
TARGET_KP       = "kp"
TARGET_PLAYHEAD = "playhead"


class DopeSheetCanvas(QWidget):
    curveChanged    = pyqtSignal()
    playheadMoved   = pyqtSignal(int)
    selectionChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.curve: TimewarpCurve = TimewarpCurve()

        self._selected_frames: Set[float] = set()
        self._hover_idx: Optional[int]    = None

        # Drag tracked by object identity
        self._drag_target    = TARGET_NONE
        self._drag_start_x: float = 0.0
        self._dragged_kps: list   = []
        self._drag_start_vals: dict = {}
        self._anchor_ids: set = set()
        self._drag_playhead   = False
        self._pre_drag_snapshot = None
        # Undo/redo delegated to curve editor via callbacks set by main_window
        self._undo_cb = None
        self._redo_cb = None
        self._snapshot_cb = None

        # Rubber-band
        self._rb_start: Optional[QPointF] = None
        self._rb_end:   Optional[QPointF] = None
        self._rb_active = False

        self._playhead_frame: Optional[int] = None
        self._view_min: Optional[float] = None
        self._view_max: Optional[float] = None

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setMinimumSize(300, PAD_T + TRACK_H + PAD_B + 20)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # ── Layout ────────────────────────────────────────────────────────────────

    def _plot_rect(self) -> QRectF:
        return QRectF(PAD_L, PAD_T,
                      self.width() - PAD_L - PAD_R, TRACK_H)

    def _out_total(self) -> float:
        kps = self.curve.sorted_keypoints()
        return kps[-1].out_frame if kps else 1.0

    def _view_range(self):
        lo = self._view_min if self._view_min is not None else 0.0
        hi = self._view_max if self._view_max is not None else self._out_total()
        return lo, max(hi, lo + 1.0)

    def _to_x(self, out_f: float) -> float:
        r       = self._plot_rect()
        lo, hi  = self._view_range()
        span    = hi - lo
        if span == 0: return r.left()
        return r.left() + ((out_f - lo) / span) * r.width()

    def _from_x(self, px: float) -> float:
        r       = self._plot_rect()
        lo, hi  = self._view_range()
        span    = hi - lo
        fx      = (px - r.left()) / r.width()
        return max(0.0, min(self._out_total(), lo + fx * span))

    def _track_y(self) -> float:
        return self._plot_rect().center().y()

    # ── Hit testing ───────────────────────────────────────────────────────────

    def _hit_test(self, pos: QPointF):
        r  = self._plot_rect()
        ty = self._track_y()

        if self._playhead_frame is not None:
            ph_rel = float(self._playhead_frame - self.curve.out_start)
            px     = self._to_x(ph_rel)
            if abs(pos.x() - px) < SNAP_PH:
                return TARGET_PLAYHEAD, None

        kps = self.curve.sorted_keypoints()
        for i, kp in enumerate(kps):
            x = self._to_x(kp.out_frame)
            if math.hypot(pos.x() - x, pos.y() - ty) < SNAP_PX:
                return TARGET_KP, i

        return TARGET_NONE, None

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        r    = self._plot_rect()
        ty   = self._track_y()

        p.fillRect(0, 0, w, h, C_BG)
        p.fillRect(QRectF(r.left(), r.top(), r.width(), r.height()), C_TRACK)

        p.setPen(QPen(C_GRID, 0.5))
        for i in range(11):
            x = r.left() + (i/10) * r.width()
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        p.setPen(QPen(C_GRID2, 0.75))
        for i in range(5):
            x = r.left() + (i/4) * r.width()
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        p.setPen(QPen(C_GRID2, 1.0))
        p.drawLine(QPointF(r.left(), ty), QPointF(r.right(), ty))

        font = QFont("Monospace", 9)
        p.setFont(font)
        fm   = QFontMetrics(font)
        ot   = self._out_total()
        p.setPen(QPen(C_LABEL, 1))
        for i in range(5):
            t  = i / 4
            x  = r.left() + t * r.width()
            lb = f"{int(t * ot + self.curve.out_start)}"
            lw = fm.horizontalAdvance(lb)
            p.drawText(QPointF(x - lw/2, r.bottom() + 16), lb)
        p.save()
        p.translate(10, ty + 4)
        p.rotate(-90)
        p.drawText(QPointF(-20, 0), "KEYS")
        p.restore()

        if self._playhead_frame is not None:
            ph_rel = float(self._playhead_frame - self.curve.out_start)
            px     = self._to_x(ph_rel)
            p.setPen(QPen(C_AMBER, 2.0))
            # Start the line just above the diamond row (not up through the
            # frame-number labels), so the playhead never covers the numbers.
            p.drawLine(QPointF(px, r.top()-PLAYHEAD_TOP_INSET), QPointF(px, r.bottom()+8))
            tri = QPolygonF([
                QPointF(px,   r.top()-PLAYHEAD_TOP_INSET),
                QPointF(px-5, r.top()-PLAYHEAD_TOP_INSET-8),
                QPointF(px+5, r.top()-PLAYHEAD_TOP_INSET-8),
            ])
            p.setBrush(QBrush(C_AMBER))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPolygon(tri)

        kps = self.curve.sorted_keypoints()
        n   = len(kps)
        for i, kp in enumerate(kps):
            x         = self._to_x(kp.out_frame)
            is_sel    = kp.out_frame in self._selected_frames
            is_hover  = (i == self._hover_idx)
            is_anchor = (i == 0 or i == n - 1)
            is_multi  = len(self._selected_frames) > 1 and is_sel
            d         = DIAMOND + (2 if (is_sel or is_hover) else 0)

            diamond = QPolygonF([
                QPointF(x,     ty - d),
                QPointF(x + d, ty    ),
                QPointF(x,     ty + d),
                QPointF(x - d, ty    ),
            ])

            if is_sel and is_multi:
                p.setBrush(QBrush(QColor("#3a2a0a")))
                p.setPen(QPen(C_KP_MULTI, 2.0))
            elif is_sel:
                p.setBrush(QBrush(QColor("#2a3a5a")))
                p.setPen(QPen(C_KP_SEL, 2.0))
            elif is_hover:
                p.setBrush(QBrush(QColor("#1a2535")))
                p.setPen(QPen(C_KP_HOVER, 1.5))
            elif is_anchor:
                p.setBrush(QBrush(QColor("#1a1a2a")))
                p.setPen(QPen(C_KP_ANCH, 1.5))
            else:
                p.setBrush(QBrush(QColor("#0e1a2a")))
                p.setPen(QPen(C_KP, 1.5))
            p.drawPolygon(diamond)

            # Inner glyph for interp mode — same scheme as the curve editor.
            # Smooth = small dot (default), Linear = small triangle, Constant = small square.
            # Outer diamond is the hit target and stays unchanged.
            inner_color = C_KP_SEL if is_sel else (
                C_KP_ANCH if is_anchor else C_KP)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(inner_color))
            if kp.interp == InterpMode.LINEAR:
                s = 2.5
                tri = QPolygonF([
                    QPointF(x,       ty - s),
                    QPointF(x + s,   ty + s * 0.75),
                    QPointF(x - s,   ty + s * 0.75),
                ])
                p.drawPolygon(tri)
            elif kp.interp == InterpMode.CONSTANT:
                s = 2.0
                p.drawRect(QRectF(x - s, ty - s, s * 2, s * 2))
            else:
                p.drawEllipse(QPointF(x, ty), 2, 2)

            out_abs = self.curve.out_start + kp.out_frame
            in_abs  = self.curve.in_start  + kp.in_frame
            out_str = f"{out_abs:.2f}" if out_abs != round(out_abs) else str(int(out_abs))
            in_str  = f"{in_abs:.2f}"  if in_abs  != round(in_abs)  else str(int(in_abs))
            p.setFont(font)
            if is_sel and len(self._selected_frames) == 1:
                p.setPen(QPen(C_KP_SEL, 1))
                lbl1 = f"f{out_str}"
                lw1  = fm.horizontalAdvance(lbl1)
                lx   = max(r.left(), min(r.right()-lw1, x - lw1/2))
                p.drawText(QPointF(lx, r.top() - DIAMOND - LABEL_GAP - 10), lbl1)
                p.setPen(QPen(C_AMBER, 1))
                lbl2 = f"\u2192f{in_str}"
                lw2  = fm.horizontalAdvance(lbl2)
                lx2  = max(r.left(), min(r.right()-lw2, x - lw2/2))
                p.drawText(QPointF(lx2, r.top() - DIAMOND - LABEL_GAP + 2), lbl2)
            else:
                p.setPen(QPen(C_KP_MULTI if is_multi else
                              C_LABEL2   if (is_hover or is_sel) else C_LABEL, 1))
                lbl = f"{out_str}"
                lw  = fm.horizontalAdvance(lbl)
                lx  = max(r.left(), min(r.right()-lw, x - lw/2))
                p.drawText(QPointF(lx, r.top() - DIAMOND - LABEL_GAP), lbl)

        if self._rb_active and self._rb_start and self._rb_end:
            rb = QRectF(self._rb_start, self._rb_end).normalized()
            p.setPen(QPen(C_RB_B, 1.0, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(rb)

        p.end()

    # ── Undo/Redo — delegated to curve editor's stack ────────────────────────

    def _push_undo(self):
        if self._snapshot_cb:
            self._snapshot_cb()

    def undo(self):
        if self._undo_cb:
            self._undo_cb()
            self._selected_frames = set()
            self.update()

    def redo(self):
        if self._redo_cb:
            self._redo_cb()
            self._selected_frames = set()
            self.update()

    # ── Mouse ─────────────────────────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos   = event.position()
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        target, idx = self._hit_test(pos)

        if target == TARGET_PLAYHEAD:
            self._drag_playhead = True

        elif target == TARGET_KP and idx is not None:
            kps      = self.curve.sorted_keypoints()
            n        = len(kps)
            kp_frame = kps[idx].out_frame

            if shift:
                if kp_frame in self._selected_frames:
                    self._selected_frames.discard(kp_frame)
                else:
                    self._selected_frames.add(kp_frame)
            else:
                if kp_frame not in self._selected_frames:
                    self._selected_frames = {kp_frame}

            # Set up drag by object identity
            self._drag_target   = TARGET_KP
            self._drag_start_x  = pos.x()
            self._dragged_kps   = [kp for kp in kps
                                   if kp.out_frame in self._selected_frames]
            self._drag_start_vals = {
                id(kp): kp.out_frame
                for kp in self._dragged_kps
            }
            self._anchor_ids = {id(kps[0]), id(kps[n-1])}
            import copy
            self._pre_drag_snapshot = copy.deepcopy(self.curve.keypoints)

        else:
            if not shift:
                self._selected_frames = set()
            self._rb_start  = pos
            self._rb_end    = pos
            self._rb_active = True

        self.selectionChanged.emit()
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent):
        pos = event.position()

        if self._drag_playhead:
            out_f = self._from_x(pos.x())
            frame = int(round(out_f)) + self.curve.out_start
            frame = max(self.curve.out_start,
                        min(self.curve.out_start + int(self._out_total()), frame))
            self._playhead_frame = frame
            self.playheadMoved.emit(frame)
            self.update()
            return

        if self._rb_active:
            self._rb_end = pos
            rb  = QRectF(self._rb_start, self._rb_end).normalized()
            kps = self.curve.sorted_keypoints()
            self._selected_frames = set()
            for kp in kps:
                x = self._to_x(kp.out_frame)
                if rb.left() <= x <= rb.right():
                    self._selected_frames.add(kp.out_frame)
            self.update()
            return

        if self._drag_target == TARGET_KP and self._dragged_kps:
            dx        = pos.x() - self._drag_start_x
            r         = self._plot_rect()
            ot        = self._out_total()
            lo, hi    = self._view_range()
            view_span = hi - lo
            d_out     = (dx / r.width()) * view_span

            new_frames = set()
            for kp in self._dragged_kps:
                if id(kp) in self._anchor_ids:
                    new_frames.add(kp.out_frame)
                    continue
                start_out = self._drag_start_vals[id(kp)]
                new_out   = round(start_out + d_out)
                new_out   = max(1, min(int(ot) - 1, new_out))
                kp.out_frame = float(new_out)
                new_frames.add(float(new_out))

            self.curve.keypoints.sort(key=lambda k: k.out_frame)
            self._selected_frames = new_frames
            self.curveChanged.emit()
            self.update()
            return

        old = self._hover_idx
        _, self._hover_idx = self._hit_test(pos)
        if old != self._hover_idx:
            self.update()

    def mouseReleaseEvent(self, event):
        if self._pre_drag_snapshot is not None:
            if self._snapshot_cb:
                self._snapshot_cb(self._pre_drag_snapshot)
            self._pre_drag_snapshot = None
        self._drag_target    = TARGET_NONE
        self._dragged_kps    = []
        self._drag_start_vals = {}
        self._anchor_ids     = set()
        self._drag_playhead  = False
        self._rb_active      = False
        self._rb_start       = None
        self._rb_end         = None
        # Selection intentionally preserved
        self.selectionChanged.emit()

    def fit_view(self):
        """Fit selected keyframes if any, otherwise fit all."""
        kps = self.curve.sorted_keypoints()
        if not kps:
            return
        sel     = [kp for kp in kps if kp.out_frame in self._selected_frames]
        targets = sel if sel else kps
        out_vals = [kp.out_frame for kp in targets]
        out_min, out_max = min(out_vals), max(out_vals)
        span = max(out_max - out_min, 1.0)
        # Store as view range override — we'll use _view_min/_view_max
        self._view_min = max(0, out_min - span * 0.15)
        self._view_max = out_max + span * 0.15
        self.update()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_F:
            self.fit_view(); return
        if event.key() == Qt.Key.Key_Z and (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.undo(); return
        if event.key() == Qt.Key.Key_Y and (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.redo(); return
        if event.key() == Qt.Key.Key_Delete:
            kps = self.curve.sorted_keypoints()
            n   = len(kps)
            to_remove = [
                i for i, kp in enumerate(kps)
                if kp.out_frame in self._selected_frames
                and i != 0 and i != n - 1
                and len(self.curve.keypoints) > 2
            ]
            if to_remove:
                self._push_undo()
            for i in sorted(to_remove, reverse=True):
                self.curve.remove_keypoint(i)
            self._selected_frames = set()
            self.curveChanged.emit()
            self.update()
        elif event.key() == Qt.Key.Key_A:
            kps = self.curve.sorted_keypoints()
            self._selected_frames = {kp.out_frame for kp in kps}
            self._view_min = None
            self._view_max = None
            self.update()
        super().keyPressEvent(event)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_curve(self, curve: TimewarpCurve):
        self.curve             = curve
        self._selected_frames  = set()
        self._hover_idx        = None
        self._drag_target      = TARGET_NONE
        self._dragged_kps      = []
        self._drag_start_vals  = {}
        self._view_min         = None
        self._view_max         = None
        self.update()

    def refresh(self):
        self.update()

    def set_playhead(self, out_frame: int):
        self._playhead_frame = out_frame
        self.update()


class DopeSheet(QWidget):
    """DopeSheetCanvas wrapped with an info bar (in/out fields, add/delete buttons)."""
    curveChanged  = pyqtSignal()
    playheadMoved = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Match settings_panel tooltip styling for consistency
        self.setStyleSheet(
            "QToolTip{background:#1e1e22;color:#ffffff;"
            "border:1px solid #4a9eff;font-family:monospace;font-size:10px;}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Info bar
        info_bar = QWidget()
        info_bar.setFixedHeight(28)
        info_bar.setStyleSheet("background:#161618; border-bottom:1px solid #2a2a2e;")
        ib = QHBoxLayout(info_bar)
        ib.setContentsMargins(8, 2, 8, 2)
        ib.setSpacing(8)

        ib.addStretch()   # leading stretch — centers the control cluster

        self._kp_label = QLabel("0 keys")
        self._kp_label.setStyleSheet("color:#6a6a72; font-size:10px; min-width:40px;")
        ib.addWidget(self._kp_label)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("color:#2a2a2e; margin:0 6px;")
        ib.addWidget(sep)

        lbl_in = QLabel("X")
        lbl_in.setStyleSheet("color:#6a6a72; font-size:10px;")
        lbl_in.setToolTip("X \u2014 output (timeline) frame")
        ib.addWidget(lbl_in)

        self._in_input = QLineEdit()
        self._in_input.setFixedWidth(54)
        self._in_input.setStyleSheet(
            "background:#1e1e21; border:1px solid #3a3a40; color:#e8e8ec;"
            "font-family:monospace; font-size:10px; padding:1px 4px;")
        self._in_input.setPlaceholderText("--")
        self._in_input.setReadOnly(True)
        self._in_input.setToolTip(
            "Output (timeline) frame for the selected keyframe.\n"
            "Read-only here — edit values in the curve editor's info bar.\n"
            "Empty when nothing is selected or multiple keyframes are selected.")
        ib.addWidget(self._in_input)

        arr = QLabel("\u00b7")
        arr.setStyleSheet("color:#3a3a40; font-size:10px;")
        ib.addWidget(arr)

        lbl_out = QLabel("Y")
        lbl_out.setStyleSheet("color:#6a6a72; font-size:10px;")
        lbl_out.setToolTip("Y \u2014 source (in) frame")
        ib.addWidget(lbl_out)

        self._out_input = QLineEdit()
        self._out_input.setFixedWidth(54)
        self._out_input.setStyleSheet(
            "background:#1e1e21; border:1px solid #3a3a40; color:#e8e8ec;"
            "font-family:monospace; font-size:10px; padding:1px 4px;")
        self._out_input.setPlaceholderText("--")
        self._out_input.setReadOnly(True)
        self._out_input.setToolTip(
            "Source (in) frame for the selected keyframe.\n"
            "Read-only here — edit values in the curve editor's info bar.\n"
            "Empty when nothing is selected or multiple keyframes are selected.")
        ib.addWidget(self._out_input)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.VLine)
        sep2.setStyleSheet("color:#2a2a2e; margin:0 6px;")
        ib.addWidget(sep2)

        BTN_ADD = (
            "QPushButton{background:#1a2a1a;border:1px solid #3ecf6e;"
            "color:#3ecf6e;font-family:monospace;font-size:13px;padding:0 6px;}"
            "QPushButton:hover{background:#243a24;}"
        )
        BTN_DEL = (
            "QPushButton{background:#2a1a1a;border:1px solid #e04a4a;"
            "color:#e04a4a;font-family:monospace;font-size:13px;padding:0 6px;}"
            "QPushButton:hover{background:#3a2424;}"
        )

        self._btn_add = QPushButton("+")
        self._btn_add.setFixedHeight(22); self._btn_add.setFixedWidth(28)
        self._btn_add.setStyleSheet(BTN_ADD)
        self._btn_add.setToolTip(
            "Add a keyframe at the current playhead position.\n"
            "Shortcut: I\n"
            "The Y value is taken from the existing curve at that X.")
        ib.addWidget(self._btn_add)

        self._btn_del = QPushButton("×")
        self._btn_del.setFixedHeight(22); self._btn_del.setFixedWidth(28)
        self._btn_del.setStyleSheet(BTN_DEL)
        self._btn_del.setToolTip(
            "Delete the selected keyframe(s).\n"
            "The first and last keyframes cannot be deleted —\n"
            "they define the curve's working range.\n"
            "Shortcut: Del")
        ib.addWidget(self._btn_del)

        ib.addStretch()   # trailing stretch — balances the leading one to center

        layout.addWidget(info_bar)

        # Canvas
        self.canvas = DopeSheetCanvas()
        self.canvas.curveChanged.connect(self._on_canvas_changed)
        self.canvas.selectionChanged.connect(self._update_info_bar)
        self.canvas.playheadMoved.connect(self.playheadMoved)
        layout.addWidget(self.canvas, stretch=1)

        # Wire add/delete buttons
        self._btn_add.clicked.connect(self._add_at_playhead)
        self._btn_del.clicked.connect(self._delete_selected)

    def _on_canvas_changed(self):
        self._update_info_bar()
        self.curveChanged.emit()

    def _update_info_bar(self):
        curve = self.canvas.curve
        kps   = curve.sorted_keypoints()
        n     = len(kps)
        self._kp_label.setText(f"{n} key{'s' if n != 1 else ''}")
        sel   = self.canvas._selected_frames
        if len(sel) == 1:
            frame = next(iter(sel))
            kp    = next((k for k in kps if k.out_frame == frame), None)
            if kp:
                out_val = curve.out_start + kp.out_frame
                out_str = f"{out_val:.2f}" if out_val != round(out_val) else str(int(out_val))
                in_val  = curve.in_start  + kp.in_frame
                in_str  = f"{in_val:.2f}"  if in_val  != round(in_val)  else str(int(in_val))
                self._in_input.setText(out_str)
                self._out_input.setText(in_str)
                return
        self._in_input.setText("")
        self._out_input.setText("")

    def _add_at_playhead(self):
        ph = self.canvas._playhead_frame
        if ph is not None:
            rel    = float(ph - self.canvas.curve.out_start)
            in_val = self.canvas.curve.evaluate(rel)
            self.canvas._push_undo()
            self.canvas.curve.add_keypoint(rel, in_val)
            self.canvas._selected_frames = {round(rel, 2)}
            self.canvas.curveChanged.emit()
            self.canvas.update()
            self._update_info_bar()

    def _delete_selected(self):
        canvas = self.canvas
        kps    = canvas.curve.sorted_keypoints()
        n      = len(kps)
        to_remove = [
            i for i, kp in enumerate(kps)
            if kp.out_frame in canvas._selected_frames
            and i != 0 and i != n - 1
            and len(canvas.curve.keypoints) > 2
        ]
        if to_remove:
            canvas._push_undo()
        for i in sorted(to_remove, reverse=True):
            canvas.curve.remove_keypoint(i)
        canvas._selected_frames = set()
        canvas.curveChanged.emit()
        canvas.update()
        self._update_info_bar()

    # ── Proxy API — forward to canvas ─────────────────────────────────────────

    @property
    def curve(self):
        return self.canvas.curve

    @property
    def _selected_frames(self):
        return self.canvas._selected_frames

    @_selected_frames.setter
    def _selected_frames(self, v):
        self.canvas._selected_frames = v

    @property
    def _undo_cb(self):
        return self.canvas._undo_cb

    @_undo_cb.setter
    def _undo_cb(self, v):
        self.canvas._undo_cb = v

    @property
    def _redo_cb(self):
        return self.canvas._redo_cb

    @_redo_cb.setter
    def _redo_cb(self, v):
        self.canvas._redo_cb = v

    @property
    def _snapshot_cb(self):
        return self.canvas._snapshot_cb

    @_snapshot_cb.setter
    def _snapshot_cb(self, v):
        self.canvas._snapshot_cb = v

    def set_curve(self, curve):
        self.canvas.set_curve(curve)
        self._update_info_bar()

    def set_playhead(self, out_frame: int):
        self.canvas.set_playhead(out_frame)

    def refresh(self):
        self.canvas.refresh()
        self._update_info_bar()

    def update(self):
        self.canvas.update()
        self._update_info_bar()
        super().update()
