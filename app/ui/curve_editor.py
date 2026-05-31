"""
Curve editor — no tangent handles, monotone cubic smooth.
Drag tracked by object identity — fixes stop-after-1-frame bug.
"""
from __future__ import annotations
import math
import copy
from typing import Optional, Set, List

from PyQt6.QtWidgets import (
    QWidget, QSizePolicy, QMenu, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QFrame, QPushButton, QComboBox, QMessageBox
)
from PyQt6.QtCore import Qt, QPointF, QRectF, pyqtSignal
from PyQt6.QtGui import (
    QPainter, QPen, QBrush, QColor, QPainterPath,
    QFont, QFontMetrics, QMouseEvent, QWheelEvent,
    QContextMenuEvent, QAction
)

from core.timewarp import (
    TimewarpCurve, InterpMode, Keypoint,
    INTERP_LABELS, INTERP_ORDER, SMOOTH_MODES, coerce_interp,
    _auto_tangents
)

C_BG           = QColor("#0e0e0f")
C_GRID         = QColor("#1a1a1e")
C_GRID3        = QColor("#2a2a2e")
C_DIAG         = QColor(74, 158, 255, 30)
C_CURVE        = QColor("#4a9eff")
C_CURVE_LIN    = QColor("#a0a0aa")
C_CURVE_CONST  = QColor("#e04a4a")
C_FILL         = QColor(74, 158, 255, 12)
C_FILL_LIN     = QColor(160, 160, 170, 8)
C_FILL_CONST   = QColor(224, 74, 74, 8)
C_KP           = QColor("#4a9eff")
C_KP_SEL       = QColor("#ffffff")
C_KP_HOVER     = QColor("#80c4ff")
C_KP_MULTI     = QColor("#f5a623")
C_TEXT         = QColor("#4a4a52")
C_LABEL        = QColor("#e8e8ec")
C_RUBBERBAND   = QColor(74, 158, 255, 35)
C_RUBBERBAND_B = QColor("#4a9eff")
C_PLAYHEAD     = QColor("#ffb830")

PAD     = {"l": 58, "r": 20, "t": 20, "b": 38}
KP_R    = 5
KP_R_HV = 7
SNAP_KP = 10
SNAP_PH = 6

TARGET_NONE     = None
TARGET_KP       = "kp"
TARGET_PLAYHEAD = "playhead"
TARGET_TAN_IN   = "tan_in"     # incoming-side tangent handle
TARGET_TAN_OUT  = "tan_out"    # outgoing-side tangent handle

C_HANDLE       = QColor("#f5a623")
C_HANDLE_LINE  = QColor(245, 166, 35, 140)
SNAP_HANDLE    = 9
HANDLE_PX      = 46            # default on-screen handle length (pixels)

C_CURVE_BEZ    = QColor("#8a7aff")
C_CURVE_NAT    = QColor("#3ec8a0")
INTERP_CURVE_COLOR = {
    InterpMode.HERMITE:  (C_CURVE,       C_FILL),
    InterpMode.BEZIER:   (C_CURVE_BEZ,   QColor(138, 122, 255, 12)),
    InterpMode.NATURAL:  (C_CURVE_NAT,   QColor(62, 200, 160, 12)),
    InterpMode.LINEAR:   (C_CURVE_LIN,   C_FILL_LIN),
    InterpMode.CONSTANT: (C_CURVE_CONST, C_FILL_CONST),
    InterpMode.SMOOTH:   (C_CURVE,       C_FILL),   # legacy alias
}
INTERP_KP_COLOR = {
    InterpMode.HERMITE:  C_KP,
    InterpMode.BEZIER:   C_CURVE_BEZ,
    InterpMode.NATURAL:  C_CURVE_NAT,
    InterpMode.LINEAR:   C_CURVE_LIN,
    InterpMode.CONSTANT: C_CURVE_CONST,
    InterpMode.SMOOTH:   C_KP,                       # legacy alias
}


class TangentIconButton(QPushButton):
    """QPushButton that paints a line-art icon instead of a text glyph.

    Accepts a `mode` string that dispatches to one of the icon-paint methods.
    The standard button chrome (background, border, hover/pressed) is rendered
    by `super().paintEvent` from the parent's QSS; the icon is drawn on top
    with QPainter.Antialiasing in a 24×24 design space centered in the button.
    """

    # Design space side length; all icon coordinates are in [0, DS)×[0, DS).
    DS = 24.0

    def __init__(self, mode: str, parent=None):
        super().__init__("", parent)
        self._mode = mode

    def paintEvent(self, event):
        # 1. Standard chrome from QSS (background, border, hover, pressed).
        super().paintEvent(event)

        # 2. Icon on top.
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Scale so DS units fit inside (w-2) × (h-2) — 1px margin each side.
        margin = 1.0
        scale = min(self.width() - 2 * margin, self.height() - 2 * margin) / self.DS
        # Translate so design origin (0, 0) maps to top-left of the scaled icon,
        # with the icon centred in the button.
        tx = (self.width()  - self.DS * scale) / 2.0
        ty = (self.height() - self.DS * scale) / 2.0

        p.save()
        p.translate(tx, ty)
        p.scale(scale, scale)

        # Derive stroke color from QSS palette text colour (matches +/×/↺).
        text_color = self.palette().color(self.foregroundRole())
        stroke = QPen(text_color)
        stroke.setWidthF(1.2)
        stroke.setCapStyle(Qt.PenCapStyle.RoundCap)
        stroke.setJoinStyle(Qt.PenJoinStyle.RoundJoin)

        if self._mode == "break":
            self._paint_break(p, stroke)
        elif self._mode == "auto":
            self._paint_auto(p, stroke)

        p.restore()
        p.end()

    def _paint_break(self, p: QPainter, stroke: QPen):
        """Dotted V with keypoint at (12,16), tips at (4,8) and (20,8)."""
        # Dotted lines for the V arms.
        dot_pen = QPen(stroke)
        dot_pen.setDashPattern([0, 3])
        dot_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(dot_pen)
        p.drawLine(QPointF(12, 16), QPointF(4, 8))
        p.drawLine(QPointF(12, 16), QPointF(20, 8))

        # Filled dots at keypoint and handle tips.
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(stroke.color()))
        p.drawEllipse(QPointF(12, 16), 1.4, 1.4)  # keypoint
        p.drawEllipse(QPointF(4, 8),   1.0, 1.0)  # left tip
        p.drawEllipse(QPointF(20, 8),  1.0, 1.0)  # right tip

    def _paint_auto(self, p: QPainter, stroke: QPen):
        """Smooth S-curve through keypoint (12,12); no handle dots."""
        # Bezier curve.
        path = QPainterPath()
        path.moveTo(4, 18)
        path.cubicTo(10, 18, 14, 6, 20, 6)
        stroke_curve = QPen(stroke)
        stroke_curve.setDashPattern([])   # solid
        p.setPen(stroke_curve)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

        # Keypoint dot.
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(stroke.color()))
        p.drawEllipse(QPointF(12, 12), 1.4, 1.4)


class YKnob(QWidget):
    """Flame-style horizontal-drag scrubber for the selected keypoint's Y
    value (in_frame). Renders the value as text via an externally-supplied
    formatter so the display mode (Frame / TC) is decided by the parent.

    Mouse model: press anchors a baseline value, mouse-move emits
    ``valueScrubbing(float)`` with the live value (no integer rounding) so the
    curve can repaint mid-drag, and release emits ``dragFinished(int)`` exactly
    once with the final rounded value. The parent is responsible for capturing
    one undo entry on ``dragStarted`` and committing per-keypoint deltas.

    Modifiers (held at drag start):
      plain  →  1 frame per pixel (Flame default).
      Shift  →  0.1 frame per pixel (fine).
      Ctrl   →  10 frames per pixel (coarse).
    """
    dragStarted     = pyqtSignal()
    valueScrubbing  = pyqtSignal(float)
    dragFinished    = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value: "float | None" = None         # None = no selection / mixed
        self._formatter = lambda v: ("--" if v is None else str(int(round(v))))
        self._dragging  = False
        self._press_x   = 0
        self._press_val = 0.0
        self._live_val  = 0.0
        self._scale     = 1.0                       # frames per pixel
        # Match Y line edit's height; width sized so a full TC string
        # ("00:00:00:00") fits without truncation. The brief asked for a
        # square — see deviations note — but TC text won't fit at 22 px.
        self.setFixedHeight(22)
        self.setFixedWidth(120)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.setMouseTracking(False)
        self.setToolTip(
            "Scrubber for the selected keyframe.\n"
            "Click-drag horizontally to change the source (in) frame.\n"
            "  plain  1 frame per pixel\n"
            "  Shift  0.1 frame per pixel\n"
            "  Ctrl   10 frames per pixel\n"
            "Updates live; one undo entry per drag.")

    # ── Public API ────────────────────────────────────────────────────────────

    def set_value(self, v):
        """Set the displayed value. None = no selection / mixed → shows '--'."""
        self._value = (None if v is None else float(v))
        self.update()

    def value(self):
        return self._value

    def set_formatter(self, fn):
        """Install a value→str formatter (Frame / TC / etc.). Triggers repaint."""
        self._formatter = fn
        self.update()

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Background + border — match the Y line edit's chrome.
        border = QColor("#4a9eff") if self._dragging else QColor("#3a3a40")
        p.setBrush(QColor("#1e1e21"))
        p.setPen(QPen(border, 1))
        from PyQt6.QtCore import QRectF as _QRectF
        p.drawRoundedRect(_QRectF(0.5, 0.5, self.width()-1, self.height()-1), 2.0, 2.0)
        # Text
        if self._dragging and self._value is not None:
            txt = self._formatter(self._live_val)
        else:
            txt = self._formatter(self._value)
        p.setPen(QColor("#e8e8ec") if self._value is not None else QColor("#6a6a72"))
        # Use the widget's own font (inherits the panel monospace).
        p.drawText(self.rect(),
                   int(Qt.AlignmentFlag.AlignCenter), txt)
        p.end()

    # ── Mouse model ───────────────────────────────────────────────────────────

    def mousePressEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton or self._value is None:
            return
        self._dragging  = True
        self._press_x   = int(ev.position().x())
        self._press_val = float(self._value)
        self._live_val  = self._press_val
        mods = ev.modifiers()
        if mods & Qt.KeyboardModifier.ShiftModifier:
            self._scale = 0.1
        elif mods & Qt.KeyboardModifier.ControlModifier:
            self._scale = 10.0
        else:
            self._scale = 1.0
        self.update()
        self.dragStarted.emit()

    def mouseMoveEvent(self, ev):
        if not self._dragging:
            return
        dx = int(ev.position().x()) - self._press_x
        self._live_val = self._press_val + dx * self._scale
        self.update()
        self.valueScrubbing.emit(self._live_val)

    def mouseReleaseEvent(self, ev):
        if not self._dragging or ev.button() != Qt.MouseButton.LeftButton:
            return
        self._dragging = False
        final = int(round(self._live_val))
        self._value = float(final)
        self.update()
        self.dragFinished.emit(final)


class CurveCanvas(QWidget):
    curveChanged  = pyqtSignal()
    playheadMoved = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.curve = TimewarpCurve()

        self._selected_frames: Set[float] = set()
        self._hover_idx    = None
        self._hover_target = TARGET_NONE

        # Drag state — keyed by object id() not out_frame
        self._drag_target   = TARGET_NONE
        self._drag_start_pos  = QPointF()
        self._dragged_kps: list = []          # list of actual Keypoint objects
        self._drag_start_vals: dict = {}      # id(kp) -> (start_out, start_in)
        self._drag_handle_idx = None          # keypoint index whose handle is dragging
        self._drag_axis_lock  = None          # None | 'x' | 'y' while Shift-constraining a kp drag
        self._anchor_ids: set = set()         # id(kp) for anchors — X locked
        self._pre_drag_snapshot = None
        # Live drag readout state — set during keypoint drag, used by paint
        self._drag_cursor_pos: Optional[QPointF] = None
        self._drag_free_y     = False

        self._rb_start  = None
        self._rb_end    = None
        self._rb_active = False

        self._playhead_frame: Optional[int] = None
        self._drag_playhead = False

        self._undo_stack: list = []
        self._redo_stack: list = []
        self._undo_max = 50

        self._origin  = QPointF(0.0, 0.0)
        self._zoom_x  = 1.0
        self._zoom_y  = 1.0
        self._pan_start        = None
        self._pan_origin_start = QPointF()

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setMinimumSize(300, 140)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # ── Undo/Redo ─────────────────────────────────────────────────────────────

    def _push_undo(self):
        state = copy.deepcopy(self.curve.keypoints)
        self._undo_stack.append(state)
        if len(self._undo_stack) > self._undo_max:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def undo(self):
        if not self._undo_stack:
            return
        self._redo_stack.append(copy.deepcopy(self.curve.keypoints))
        self.curve.keypoints = self._undo_stack.pop()
        self._selected_frames = set()
        self.curveChanged.emit()
        self.update()

    def redo(self):
        if not self._redo_stack:
            return
        self._undo_stack.append(copy.deepcopy(self.curve.keypoints))
        self.curve.keypoints = self._redo_stack.pop()
        self._selected_frames = set()
        self.curveChanged.emit()
        self.update()

    # ── Selection ─────────────────────────────────────────────────────────────

    def _single_selected_idx(self, kps):
        if len(self._selected_frames) != 1:
            return None
        frame = next(iter(self._selected_frames))
        for i, kp in enumerate(kps):
            if kp.out_frame == frame:
                return i
        return None

    # ── Viewport ──────────────────────────────────────────────────────────────

    def _plot_rect(self):
        return QRectF(PAD["l"], PAD["t"],
                      self.width()  - PAD["l"] - PAD["r"],
                      self.height() - PAD["t"] - PAD["b"])

    def _out_total(self):
        kps = self.curve.sorted_keypoints()
        return kps[-1].out_frame if kps else 1.0

    def _in_total(self):
        return max(1, self.curve.in_frame_count - 1)

    def _view_out_range(self):
        return self._origin.x(), self._origin.x() + self._out_total() / self._zoom_x

    def _view_in_range(self):
        return self._origin.y(), self._origin.y() + self._in_total() / self._zoom_y

    def _to_widget(self, out_f, in_f):
        r = self._plot_rect()
        out_lo, out_hi = self._view_out_range()
        in_lo,  in_hi  = self._view_in_range()
        out_span = out_hi - out_lo
        in_span  = in_hi  - in_lo
        if out_span == 0 or in_span == 0:
            return QPointF(r.left(), r.bottom())
        x = r.left() + ((out_f - out_lo) / out_span) * r.width()
        y = r.bottom() - ((in_f - in_lo) / in_span) * r.height()
        return QPointF(x, y)

    def _from_widget(self, px, py):
        r = self._plot_rect()
        out_lo, out_hi = self._view_out_range()
        in_lo,  in_hi  = self._view_in_range()
        fx = (px - r.left()) / r.width()
        fy = 1.0 - (py - r.top()) / r.height()
        return (out_lo + fx*(out_hi-out_lo),
                in_lo  + fy*(in_hi -in_lo))

    def _handle_points(self, kps, idx):
        """Return (in_pt, out_pt) widget positions of the tangent handles for
        keypoint idx, or (None, None) if it has no handles. Only Hermite/Bezier
        keypoints have handles; endpoints get only the side with an adjacent
        segment. Slope sets the angle; weight sets the length (the control-point
        distance, as a fraction of that side's segment out-span). Both come from
        the model so the drawn handle matches the rendered curve."""
        kp = kps[idx]
        mode = coerce_interp(kp.interp)
        if mode not in (InterpMode.HERMITE, InterpMode.BEZIER, InterpMode.NATURAL):
            return None, None

        kp_pt = self._to_widget(kp.out_frame, kp.in_frame)
        r = self._plot_rect()
        out_lo, out_hi = self._view_out_range()
        in_lo,  in_hi  = self._view_in_range()
        ospan = (out_hi-out_lo) or 1.0
        ispan = (in_hi -in_lo)  or 1.0
        sx =  r.width()  / ospan      # screen px per +1 out unit
        sy = -r.height() / ispan      # screen px per +1 in unit

        def handle_pt(slope, weight, seg_span, sign):
            d_out = weight * seg_span
            d_in  = slope * d_out
            return QPointF(kp_pt.x() + sign*d_out*sx,
                           kp_pt.y() + sign*d_in*sy)

        # For a Natural keypoint with no manual override, the handle should show
        # the globally-solved natural tangent (what the curve actually uses),
        # not the local auto-tangent.
        nat = None
        if mode == InterpMode.NATURAL:
            nat = self.curve._natural_tangents(kps)

        DEF_W = 1.0/3.0
        in_pt = out_pt = None
        if idx > 0:
            _m0, m1, _d, h_prev = _auto_tangents(kps, idx-1)
            default_in = nat[idx] if nat is not None else m1
            in_slope  = kp.in_tangent if kp.in_tangent is not None else default_in
            in_weight = kp.in_weight  if kp.in_weight  is not None else DEF_W
            in_pt = handle_pt(in_slope, in_weight, abs(h_prev), -1)
        if idx < len(kps)-1:
            m0, _m1, _d, h_next = _auto_tangents(kps, idx)
            default_out = nat[idx] if nat is not None else m0
            out_slope  = kp.out_tangent if kp.out_tangent is not None else default_out
            out_weight = kp.out_weight  if kp.out_weight  is not None else DEF_W
            out_pt = handle_pt(out_slope, out_weight, abs(h_next), +1)
        return in_pt, out_pt

    def reset_view(self):
        """Fit selected keyframes if any, otherwise fit all (default view)."""
        kps = self.curve.sorted_keypoints()
        sel = [kp for kp in kps if kp.out_frame in self._selected_frames]

        if not sel:
            # No selection — reset to default full view
            self._origin = QPointF(0.0, 0.0)
            self._zoom_x = 1.0
            self._zoom_y = 1.0
            self.update()
            return

        # Fit to selected keyframes
        out_vals = [kp.out_frame for kp in sel]
        in_vals  = [kp.in_frame  for kp in sel]
        out_min, out_max = min(out_vals), max(out_vals)
        in_min,  in_max  = min(in_vals),  max(in_vals)

        out_span = max(out_max - out_min, 1.0)
        in_span  = max(in_max  - in_min,  1.0)
        pad_out  = out_span * 0.2
        pad_in   = in_span  * 0.2
        out_min -= pad_out; out_max += pad_out
        in_min  -= pad_in;  in_max  += pad_in

        full_out = self._out_total() or 1.0
        full_in  = self._in_total()  or 1.0
        self._zoom_x = max(0.1, min(200.0, full_out / max(out_max - out_min, 1.0)))
        self._zoom_y = max(0.1, min(200.0, full_in  / max(in_max  - in_min,  1.0)))
        self._origin = QPointF(out_min, in_min)
        self.update()

    # ── Hit testing ───────────────────────────────────────────────────────────

    def _hit_test(self, pos):
        kps = self.curve.sorted_keypoints()

        # Tangent handles take priority, but only for a single selected
        # Hermite/Bezier keypoint (handles are only shown in that case).
        if len(self._selected_frames) == 1:
            sel_idx = next((i for i, kp in enumerate(kps)
                            if kp.out_frame in self._selected_frames), None)
            if sel_idx is not None:
                in_pt, out_pt = self._handle_points(kps, sel_idx)
                if in_pt is not None and \
                   math.hypot(pos.x()-in_pt.x(), pos.y()-in_pt.y()) < SNAP_HANDLE:
                    return sel_idx, TARGET_TAN_IN, None
                if out_pt is not None and \
                   math.hypot(pos.x()-out_pt.x(), pos.y()-out_pt.y()) < SNAP_HANDLE:
                    return sel_idx, TARGET_TAN_OUT, None

        if self._playhead_frame is not None:
            rel = self._playhead_frame - self.curve.out_start
            ph  = self._to_widget(rel, self.curve.evaluate(rel))
            r   = self._plot_rect()
            if abs(pos.x() - ph.x()) < SNAP_PH and r.top() <= pos.y() <= r.bottom():
                return None, TARGET_PLAYHEAD, None

        for i, kp in enumerate(kps):
            pt = self._to_widget(kp.out_frame, kp.in_frame)
            if math.hypot(pos.x()-pt.x(), pos.y()-pt.y()) < SNAP_KP:
                return i, TARGET_KP, kp.out_frame

        return None, TARGET_NONE, None

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self._plot_rect()
        p.fillRect(0, 0, self.width(), self.height(), C_BG)
        p.setClipRect(r.adjusted(-2, -2, 2, 2))

        self._draw_grid(p, r)

        p.setPen(QPen(C_DIAG, 1.0, Qt.PenStyle.DashLine))
        p.drawLine(self._to_widget(0, 0),
                   self._to_widget(self._out_total(), self._in_total()))

        self._draw_curve(p, r)

        if self._playhead_frame is not None:
            rel = self._playhead_frame - self.curve.out_start
            ph  = self._to_widget(rel, 0)
            p.setPen(QPen(C_PLAYHEAD, 2.0))
            p.drawLine(QPointF(ph.x(), r.top()), QPointF(ph.x(), r.bottom()))
            tri = QPainterPath()
            tri.moveTo(ph.x(), r.top()+8)
            tri.lineTo(ph.x()-5, r.top())
            tri.lineTo(ph.x()+5, r.top())
            tri.closeSubpath()
            p.fillPath(tri, QBrush(C_PLAYHEAD))

        p.setClipping(False)
        self._draw_labels(p, r)

        kps = self.curve.sorted_keypoints()
        for i in range(len(kps)):
            self._draw_keypoint(p, kps, i)
        # Tangent handles for a single selected Hermite/Bezier keypoint.
        self._draw_handles(p, kps)
        # Labels/tooltips on top so glyphs never obscure them.
        for i in range(len(kps)):
            self._draw_keypoint_label(p, kps, i)

        if self._rb_active and self._rb_start and self._rb_end:
            rb = QRectF(self._rb_start, self._rb_end).normalized()
            p.setPen(QPen(C_RUBBERBAND_B, 1.0, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(rb)

        # Floating readout during keypoint drag — centered above the dragged
        # keypoint (flips below near the top, clamps horizontally near edges).
        if (self._drag_target == TARGET_KP
                and self._dragged_kps
                and len(self._dragged_kps) == 1):
            kp = self._dragged_kps[0]
            out_abs = self.curve.out_start + kp.out_frame
            in_abs  = self.curve.in_start  + kp.in_frame
            out_str = f"{out_abs:.2f}" if out_abs != round(out_abs) else str(int(out_abs))
            in_str  = f"{in_abs:.2f}"  if in_abs  != round(in_abs)  else str(int(in_abs))
            label = f"{out_str} \u2192 {in_str}"
            if self._drag_free_y:
                label += "   (free)"
            kp_pt = self._to_widget(kp.out_frame, kp.in_frame)
            self._draw_value_box(p, kp_pt, label)

        p.end()

    def _draw_grid(self, p, r):
        out_lo, out_hi = self._view_out_range()
        in_lo,  in_hi  = self._view_in_range()

        def nice_step(span, target=10):
            raw = span / target
            if raw <= 0: return 1
            mag = 10**math.floor(math.log10(max(raw, 1e-9)))
            for m in (1, 2, 5, 10):
                if mag*m >= raw: return mag*m
            return mag*10

        step_out = nice_step(out_hi - out_lo)
        step_in  = nice_step(in_hi  - in_lo)

        p.setPen(QPen(C_GRID, 0.5))
        x = math.ceil(out_lo / step_out) * step_out
        while x <= out_hi + step_out:
            pt = self._to_widget(x, 0)
            p.drawLine(QPointF(pt.x(), r.top()), QPointF(pt.x(), r.bottom()))
            x += step_out
        y = math.ceil(in_lo / step_in) * step_in
        while y <= in_hi + step_in:
            pt = self._to_widget(0, y)
            p.drawLine(QPointF(r.left(), pt.y()), QPointF(r.right(), pt.y()))
            y += step_in

        p.setPen(QPen(C_GRID3, 0.75))
        x = math.ceil(out_lo / (step_out*5)) * step_out*5
        while x <= out_hi + step_out*5:
            pt = self._to_widget(x, 0)
            p.drawLine(QPointF(pt.x(), r.top()), QPointF(pt.x(), r.bottom()))
            x += step_out*5
        y = math.ceil(in_lo / (step_in*5)) * step_in*5
        while y <= in_hi + step_in*5:
            pt = self._to_widget(0, y)
            p.drawLine(QPointF(r.left(), pt.y()), QPointF(r.right(), pt.y()))
            y += step_in*5

    def _draw_labels(self, p, r):
        font = QFont("Monospace", 9)
        p.setFont(font)
        fm   = QFontMetrics(font)
        out_lo, out_hi = self._view_out_range()
        in_lo,  in_hi  = self._view_in_range()

        def nice_step(span, target=8):
            raw = span / target
            if raw <= 0: return 1
            mag = 10**math.floor(math.log10(max(raw, 1e-9)))
            for m in (1,2,5,10):
                if mag*m >= raw: return mag*m
            return mag*10

        step_out = nice_step(out_hi - out_lo)
        step_in  = nice_step(in_hi  - in_lo)

        p.setPen(QPen(C_TEXT, 1))
        x = math.ceil(out_lo / step_out) * step_out
        while x <= out_hi:
            pt = self._to_widget(x, 0)
            lb = f"{int(x + self.curve.out_start)}"
            lw = fm.horizontalAdvance(lb)
            p.drawText(QPointF(pt.x()-lw/2, r.bottom()+14), lb)
            x += step_out
        y = math.ceil(in_lo / step_in) * step_in
        while y <= in_hi:
            pt = self._to_widget(0, y)
            p.drawText(QPointF(2, pt.y()+4), f"{int(y + self.curve.in_start)}")
            y += step_in

        p.save()
        p.translate(10, r.center().y())
        p.rotate(-90)
        p.drawText(QPointF(-30, 0), "IN FRAME")
        p.restore()
        p.drawText(QPointF(r.center().x()-25, self.height()-4), "OUT FRAME")

    def _draw_handles(self, p, kps):
        if len(self._selected_frames) != 1:
            return
        idx = next((i for i, kp in enumerate(kps)
                    if kp.out_frame in self._selected_frames), None)
        if idx is None:
            return
        in_pt, out_pt = self._handle_points(kps, idx)
        if in_pt is None and out_pt is None:
            return
        kp     = kps[idx]
        kp_pt  = self._to_widget(kp.out_frame, kp.in_frame)
        broken = kp.broken

        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for hp, tgt in ((in_pt, TARGET_TAN_IN), (out_pt, TARGET_TAN_OUT)):
            if hp is None:
                continue
            hovered = (idx == self._hover_idx and self._hover_target == tgt)
            # Stem: thicker now; dotted when the keypoint's tangents are broken
            # (independent sides), solid when unified.
            stem = QPen(C_HANDLE_LINE, 2.0)
            if broken:
                stem.setStyle(Qt.PenStyle.DotLine)
            p.setPen(stem)
            p.drawLine(kp_pt, hp)
            overridden = (kp.in_tangent is not None) if tgt == TARGET_TAN_IN \
                         else (kp.out_tangent is not None)
            col = QColor("#e0743c") if broken else QColor(C_HANDLE)
            rr  = 5.0 if (hovered or overridden) else 4.0
            p.setPen(QPen(col, 1.6))
            p.setBrush(QBrush(col) if overridden else Qt.BrushStyle.NoBrush)
            p.drawEllipse(hp, rr, rr)
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _draw_keypoint(self, p, kps, i):
        kp       = kps[i]
        pt       = self._to_widget(kp.out_frame, kp.in_frame)
        is_sel   = kp.out_frame in self._selected_frames
        is_hover = (i == self._hover_idx and self._hover_target == TARGET_KP)
        is_multi = len(self._selected_frames) > 1 and is_sel
        radius   = KP_R_HV if (is_sel or is_hover) else KP_R
        kp_color = INTERP_KP_COLOR.get(kp.interp, C_KP)

        if is_sel and is_multi:
            p.setPen(QPen(C_KP_MULTI, 2.0))
            p.setBrush(QBrush(QColor("#3a2a0a")))
        elif is_sel:
            p.setPen(QPen(C_KP_SEL, 2.0))
            p.setBrush(QBrush(QColor("#1a1a2a")))
        elif is_hover:
            p.setPen(QPen(C_KP_HOVER, 1.5))
            p.setBrush(QBrush(QColor("#1a2030")))
        else:
            p.setPen(QPen(kp_color, 1.5))
            p.setBrush(QBrush(C_BG))
        p.drawEllipse(pt, radius, radius)
        p.setPen(Qt.PenStyle.NoPen)
        # Inner glyph indicates interp mode at a glance:
        #   Hermite = filled dot, Bezier = diamond,
        #   Linear  = triangle,   Constant = square.
        inner_color = C_KP_SEL if is_sel else kp_color
        p.setBrush(QBrush(inner_color))
        mode = coerce_interp(kp.interp)
        if mode == InterpMode.LINEAR:
            from PyQt6.QtGui import QPolygonF
            s = 2.5
            tri = QPolygonF([
                QPointF(pt.x(),       pt.y() - s),
                QPointF(pt.x() + s,   pt.y() + s * 0.75),
                QPointF(pt.x() - s,   pt.y() + s * 0.75),
            ])
            p.drawPolygon(tri)
        elif mode == InterpMode.CONSTANT:
            s = 2.0
            p.drawRect(QRectF(pt.x() - s, pt.y() - s, s * 2, s * 2))
        elif mode == InterpMode.BEZIER:
            # Diamond (rotated square) — evokes Bezier control points.
            from PyQt6.QtGui import QPolygonF
            s = 2.6
            dia = QPolygonF([
                QPointF(pt.x(),     pt.y() - s),
                QPointF(pt.x() + s, pt.y()),
                QPointF(pt.x(),     pt.y() + s),
                QPointF(pt.x() - s, pt.y()),
            ])
            p.drawPolygon(dia)
        elif mode == InterpMode.NATURAL:
            # Hollow ring — auto-solved smooth, distinct from Hermite's solid dot.
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(inner_color, 1.2))
            p.drawEllipse(pt, 2.4, 2.4)
            p.setPen(Qt.PenStyle.NoPen)
        else:
            # HERMITE (default) — filled dot
            p.drawEllipse(pt, 2, 2)

    def _draw_keypoint_label(self, p, kps, i):
        """Second pass: hover tooltip only (centered above the keypoint). The
        persistent selected-keypoint value label was removed — the selected
        keypoint's X/Y live in the top info bar instead. Runs after all glyphs
        so a later keypoint's glyph can't paint over an earlier label."""
        kp       = kps[i]
        is_sel   = kp.out_frame in self._selected_frames
        is_hover = (i == self._hover_idx and self._hover_target == TARGET_KP)

        if is_hover and not is_sel:
            pt      = self._to_widget(kp.out_frame, kp.in_frame)
            speed   = kp.speed_hint(kps[i-1] if i > 0 else None)
            iname   = INTERP_LABELS.get(kp.interp, "")
            out_abs = self.curve.out_start + kp.out_frame
            in_abs  = self.curve.in_start  + kp.in_frame
            out_str = f"{out_abs:.2f}" if out_abs != round(out_abs) else str(int(out_abs))
            in_str  = f"{in_abs:.2f}"  if in_abs  != round(in_abs)  else str(int(in_abs))
            # Two lines: frames on top, speed + interpolation below — keeps the
            # box narrow instead of one long row.
            self._draw_value_box(p, pt,
                [f"f{out_str} \u2192 f{in_str}", f"{speed*100:.0f}%  {iname}"])

    def _draw_value_box(self, p, kp_pt, label):
        """Draw a black readout box centered above a keypoint. Accepts a single
        string or a list of lines (stacked vertically). Flips below near the top
        edge; clamps horizontally to stay on-canvas. Shared by the hover tooltip
        and the live drag readout so they look identical."""
        lines = label if isinstance(label, (list, tuple)) else [label]
        font = QFont("Monospace", 9)
        p.setFont(font)
        fm   = QFontMetrics(font)
        line_h = 17
        pad_v  = 7
        lw   = max(fm.horizontalAdvance(s) for s in lines) + 12
        lh   = line_h * len(lines) + pad_v
        gap  = KP_R_HV + 6
        # Centered horizontally on the keypoint, sitting just above it.
        lx = kp_pt.x() - lw / 2.0
        ly = kp_pt.y() - lh - gap
        # Flip below if it would clip the top.
        rc = self._plot_rect()
        if ly < rc.top() + 2:
            ly = kp_pt.y() + gap
        # Clamp horizontally to stay fully on-canvas.
        lx = max(4, min(self.width() - lw - 4, lx))
        p.fillRect(QRectF(lx, ly, lw, lh), QColor(0, 0, 0, 220))
        p.setPen(QPen(C_LABEL, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        ty = ly + 13
        for s in lines:
            sw = fm.horizontalAdvance(s)
            p.drawText(QPointF(lx + (lw - sw) / 2.0, ty), s)
            ty += line_h

    def _draw_curve(self, p, r):
        kps = self.curve.sorted_keypoints()
        if len(kps) < 2:
            return
        for i in range(len(kps)-1):
            k0, k1    = kps[i], kps[i+1]
            mode      = coerce_interp(k0.interp)
            color, _fc = INTERP_CURVE_COLOR.get(mode, (C_CURVE, C_FILL))
            p0_w = self._to_widget(k0.out_frame, k0.in_frame)
            p1_w = self._to_widget(k1.out_frame, k1.in_frame)
            path = QPainterPath()
            path.moveTo(p0_w)
            if mode == InterpMode.CONSTANT:
                path.lineTo(QPointF(p1_w.x(), p0_w.y()))
                path.lineTo(p1_w)
            elif mode == InterpMode.LINEAR:
                path.lineTo(p1_w)
            else:
                steps = max(24, int(abs(k1.out_frame - k0.out_frame)))
                for s in range(1, steps+1):
                    frac  = s / steps
                    out_f = k0.out_frame + frac*(k1.out_frame - k0.out_frame)
                    in_f  = self.curve.evaluate(out_f)
                    path.lineTo(self._to_widget(out_f, in_f))
            # No fill under the curve — just the colored line (cleaner, matches
            # how Flame/Nuke draw timewarp curves).
            p.setPen(QPen(color, 2.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)

    # ── Mouse ─────────────────────────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent):
        pos   = event.position()
        mods  = event.modifiers()
        ctrl  = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        alt   = bool(mods & Qt.KeyboardModifier.AltModifier)

        if event.button() == Qt.MouseButton.MiddleButton:
            self._pan_start        = pos
            self._pan_origin_start = QPointF(self._origin)
            return

        if event.button() == Qt.MouseButton.LeftButton:
            idx, target, kp_frame = self._hit_test(pos)

            if target == TARGET_PLAYHEAD:
                self._drag_playhead = True
                return

            if target in (TARGET_TAN_IN, TARGET_TAN_OUT) and idx is not None:
                kps = self.curve.sorted_keypoints()
                self._drag_target     = target
                self._drag_handle_idx = idx
                self._drag_start_pos  = pos
                self._pre_drag_snapshot = copy.deepcopy(self.curve.keypoints)
                return

            if target == TARGET_KP and idx is not None:
                # Selection is the same with or without Shift (multi-select is
                # handled by rubber-band). Shift's only role on a keypoint is to
                # axis-lock the drag (handled in mouseMoveEvent). Clicking a
                # keypoint that isn't part of the current selection selects it.
                if kp_frame not in self._selected_frames:
                    self._selected_frames = {kp_frame}
                # Notify parent CurveEditor to update info bar
                if hasattr(self.parent(), '_update_info_bar'):
                    self.parent()._update_info_bar()

                # Set up drag using object identity
                kps = self.curve.sorted_keypoints()
                n   = len(kps)
                self._drag_target    = TARGET_KP
                self._drag_start_pos = pos
                self._dragged_kps    = [kp for kp in kps
                                        if kp.out_frame in self._selected_frames]
                self._drag_start_vals = {
                    id(kp): (kp.out_frame, kp.in_frame)
                    for kp in self._dragged_kps
                }
                # Mark anchors (first/last) — their X is locked
                self._anchor_ids = {id(kps[0]), id(kps[n-1])}
                self._pre_drag_snapshot = copy.deepcopy(self.curve.keypoints)

            else:
                if ctrl and alt:
                    rc = self._plot_rect()
                    if rc.contains(pos.x(), pos.y()):
                        out_f, _ = self._from_widget(pos.x(), pos.y())
                        out_f    = round(out_f, 2)
                        in_f     = self.curve.evaluate(out_f)
                        self._push_undo()
                        self.curve.add_keypoint(out_f, in_f)
                        self._selected_frames = {out_f}
                        self.curveChanged.emit()
                else:
                    if not shift:
                        self._selected_frames = set()
                    self._rb_start  = pos
                    self._rb_end    = pos
                    self._rb_active = True

            self.update()

    def mouseMoveEvent(self, event: QMouseEvent):
        pos  = event.position()
        mods = event.modifiers()

        if self._pan_start is not None:
            dx = pos.x() - self._pan_start.x()
            dy = pos.y() - self._pan_start.y()
            r  = self._plot_rect()
            out_lo, out_hi = self._view_out_range()
            in_lo,  in_hi  = self._view_in_range()
            self._origin = QPointF(
                self._pan_origin_start.x() - (dx/r.width())*(out_hi-out_lo),
                self._pan_origin_start.y() + (dy/r.height())*(in_hi-in_lo))
            self.update()
            return

        if self._drag_playhead:
            out_f, _ = self._from_widget(pos.x(), pos.y())
            frame = max(0, min(int(self._out_total()), round(out_f)))
            self._playhead_frame = frame
            self.playheadMoved.emit(self.curve.out_start + frame)
            self.update()
            return

        if self._rb_active:
            self._rb_end = pos
            rb  = QRectF(self._rb_start, self._rb_end).normalized()
            kps = self.curve.sorted_keypoints()
            self._selected_frames = set()
            for kp in kps:
                pt = self._to_widget(kp.out_frame, kp.in_frame)
                if rb.contains(pt):
                    self._selected_frames.add(kp.out_frame)
            self.update()
            return

        if self._drag_target in (TARGET_TAN_IN, TARGET_TAN_OUT):
            kps = self.curve.sorted_keypoints()
            idx = self._drag_handle_idx
            if idx is None or idx >= len(kps):
                return
            kp  = kps[idx]
            kp_pt = self._to_widget(kp.out_frame, kp.in_frame)
            r = self._plot_rect()
            out_lo, out_hi = self._view_out_range()
            in_lo,  in_hi  = self._view_in_range()
            ospan = (out_hi-out_lo) or 1.0
            ispan = (in_hi -in_lo)  or 1.0
            d_out = ((pos.x()-kp_pt.x()) / r.width())  * ospan
            d_in  = -((pos.y()-kp_pt.y()) / r.height()) * ispan
            if abs(d_out) < 1e-6:
                d_out = 1e-6 if d_out >= 0 else -1e-6
            slope = d_in / d_out

            # Weight = how far the handle is pulled, as a fraction of that side's
            # segment out-span. Clamped to a sane range so handles can't vanish
            # or shoot off-screen.
            if self._drag_target == TARGET_TAN_OUT and idx < len(kps)-1:
                _m, _m1, _dl, h = _auto_tangents(kps, idx)
                seg = abs(h) or 1.0
            elif self._drag_target == TARGET_TAN_IN and idx > 0:
                _m, _m1, _dl, h = _auto_tangents(kps, idx-1)
                seg = abs(h) or 1.0
            else:
                seg = 1.0
            weight = max(0.05, min(1.0, abs(d_out) / seg))

            if self._drag_target == TARGET_TAN_OUT:
                kp.out_tangent = slope
                kp.out_weight  = weight
                if not kp.broken:
                    kp.in_tangent = slope     # unify mirrors ANGLE only;
                    # length (weight) stays independent per Flame behaviour.
            else:
                kp.in_tangent = slope
                kp.in_weight  = weight
                if not kp.broken:
                    kp.out_tangent = slope

            self.curveChanged.emit()
            self.update()
            return

        if self._drag_target == TARGET_KP and self._dragged_kps:
            dx    = pos.x() - self._drag_start_pos.x()
            dy    = pos.y() - self._drag_start_pos.y()
            r     = self._plot_rect()
            out_lo, out_hi = self._view_out_range()
            in_lo,  in_hi  = self._view_in_range()
            d_out = (dx/r.width())   * (out_hi-out_lo)
            d_in  = -(dy/r.height()) * (in_hi-in_lo)

            # Shift = auto axis-lock: constrain the drag to a single axis,
            # whichever direction dominated at the start of the drag. Lock X
            # (move along time only, in_frame frozen) if the cursor moved more
            # horizontally; lock Y (move source only, out_frame frozen) if more
            # vertically. The lock is decided once and held for the whole drag.
            shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            if shift:
                if self._drag_axis_lock is None:
                    # Only commit to an axis once the drag has moved enough to
                    # have a clear dominant direction.
                    if abs(dx) > 3 or abs(dy) > 3:
                        self._drag_axis_lock = 'x' if abs(dx) >= abs(dy) else 'y'
            else:
                self._drag_axis_lock = None
            lock = self._drag_axis_lock

            new_frames = set()
            for kp in self._dragged_kps:
                s_out, s_in = self._drag_start_vals[id(kp)]
                # Y (in_frame) — fractional, quantized to 2dp; frozen if X-locked.
                if lock == 'x':
                    new_in = s_in
                else:
                    new_in = round((s_in + d_in) * 100.0) / 100.0
                new_in = max(0.0, min(float(self._in_total()), new_in))
                kp.in_frame = new_in
                # X (out_frame) — integer; frozen if Y-locked or this is an anchor.
                if id(kp) not in self._anchor_ids:
                    if lock == 'y':
                        new_out = round(s_out)
                    else:
                        new_out = round(s_out + d_out)
                    kp.out_frame = float(new_out)
                new_frames.add(kp.out_frame)

            # Stash cursor pos for the floating drag-readout tooltip.
            self._drag_cursor_pos = pos
            self._drag_free_y     = True

            self.curve.keypoints.sort(key=lambda k: k.out_frame)
            self._selected_frames = new_frames
            self.curveChanged.emit()
            self.update()
            return

        old_idx, old_tgt = self._hover_idx, self._hover_target
        idx, target, _   = self._hit_test(pos)
        self._hover_idx    = idx
        self._hover_target = target
        if old_idx != self._hover_idx or old_tgt != self._hover_target:
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._pre_drag_snapshot is not None:
            self._undo_stack.append(self._pre_drag_snapshot)
            if len(self._undo_stack) > self._undo_max:
                self._undo_stack.pop(0)
            self._redo_stack.clear()
            self._pre_drag_snapshot = None
        self._drag_target    = TARGET_NONE
        self._dragged_kps    = []
        self._drag_start_vals = {}
        self._drag_handle_idx = None
        self._drag_axis_lock  = None
        self._anchor_ids     = set()
        self._rb_active      = False
        self._rb_start       = None
        self._rb_end         = None
        self._drag_playhead  = False
        self._pan_start      = None
        # Clear live drag readout
        self._drag_cursor_pos = None
        self._drag_free_y     = False
        # Notify parent CurveEditor to update info bar after any selection change
        if hasattr(self.parent(), '_update_info_bar'):
            self.parent()._update_info_bar()
        self.update()

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        idx, target, kp_frame = self._hit_test(event.position())
        kps = self.curve.sorted_keypoints()
        if (target == TARGET_KP and idx is not None and
                len(self.curve.keypoints) > 2 and
                idx != 0 and idx != len(kps)-1):
            self._push_undo()
            self.curve.remove_keypoint(idx)
            self._selected_frames.discard(kp_frame)
            self.curveChanged.emit()
            self.update()

    def wheelEvent(self, event: QWheelEvent):
        delta  = event.angleDelta().y()
        pos    = event.position()
        r      = self._plot_rect()
        if not r.contains(pos.x(), pos.y()):
            return
        mods   = event.modifiers()
        factor = 1.15 if delta > 0 else 1/1.15
        out_f, in_f = self._from_widget(pos.x(), pos.y())
        if mods & Qt.KeyboardModifier.ShiftModifier:
            self._zoom_y = max(0.1, min(200.0, self._zoom_y * factor))
        elif mods & Qt.KeyboardModifier.ControlModifier:
            self._zoom_x = max(0.1, min(200.0, self._zoom_x * factor))
        else:
            self._zoom_x = max(0.1, min(200.0, self._zoom_x * factor))
            self._zoom_y = max(0.1, min(200.0, self._zoom_y * factor))
        new_out, new_in = self._from_widget(pos.x(), pos.y())
        self._origin = QPointF(self._origin.x() + (out_f-new_out),
                               self._origin.y() + (in_f-new_in))
        self.update()

    def contextMenuEvent(self, event: QContextMenuEvent):
        pos = QPointF(event.pos())
        idx, target, kp_frame = self._hit_test(pos)
        kps  = self.curve.sorted_keypoints()
        menu = QMenu(self)

        if target == TARGET_KP and idx is not None:
            kp = kps[idx]
            self._selected_frames = {kp_frame}
            self.update()
            for mode in INTERP_ORDER:
                act = QAction(INTERP_LABELS[mode], self)
                act.setCheckable(True)
                act.setChecked(coerce_interp(kp.interp) == mode)
                act.triggered.connect(
                    lambda checked=False, m=mode, i=idx: self._set_interp(i, m))
                menu.addAction(act)

            # Tangent actions — only meaningful for smooth (handle) modes.
            if coerce_interp(kp.interp) in (InterpMode.HERMITE, InterpMode.BEZIER, InterpMode.NATURAL):
                menu.addSeparator()
                if kp.broken:
                    ua = QAction("Unify Tangents", self)
                    ua.triggered.connect(lambda checked=False, i=idx: self._unify_tangents(i))
                    menu.addAction(ua)
                else:
                    ba = QAction("Break Tangents", self)
                    ba.triggered.connect(lambda checked=False, i=idx: self._break_tangents(i))
                    menu.addAction(ba)
                has_override = (kp.in_tangent is not None or kp.out_tangent is not None
                                or kp.in_weight is not None or kp.out_weight is not None)
                rta = QAction("Reset Tangents to Auto", self)
                rta.setEnabled(has_override or kp.broken)
                rta.triggered.connect(lambda checked=False, i=idx: self._reset_tangents(i))
                menu.addAction(rta)

            is_anchor = (idx == 0 or idx == len(kps)-1)
            if not is_anchor and len(self.curve.keypoints) > 2:
                menu.addSeparator()
                da = QAction("Delete keyframe  [Del]", self)
                da.triggered.connect(lambda: self._delete_kp(idx))
                menu.addAction(da)
        else:
            rc = self._plot_rect()
            if rc.contains(pos.x(), pos.y()):
                out_f, _ = self._from_widget(pos.x(), pos.y())
                add_cur = QAction(
                    f"Add keyframe at cursor  (f{int(round(out_f)+self.curve.out_start)})", self)
                add_cur.triggered.connect(lambda: self._add_at(round(out_f), self.curve.evaluate(round(out_f))))
                menu.addAction(add_cur)
                if self._playhead_frame is not None:
                    ph_out = self._playhead_frame - self.curve.out_start
                    add_ph = QAction(
                        f"Add keyframe at playhead  (f{self._playhead_frame})", self)
                    add_ph.triggered.connect(lambda: self._add_at(ph_out, self.curve.evaluate(ph_out)))
                    menu.addAction(add_ph)

        menu.addSeparator()
        vm  = menu.addMenu("View")
        ra  = QAction("Reset zoom/pan  [F]", self)
        ra.triggered.connect(self.reset_view)
        vm.addAction(ra)
        menu.exec(event.globalPos())

    def keyPressEvent(self, event):
        kps = self.curve.sorted_keypoints()
        if event.key() == Qt.Key.Key_Delete:
            to_remove = [
                i for i, kp in enumerate(kps)
                if kp.out_frame in self._selected_frames
                and i != 0 and i != len(kps)-1
                and len(self.curve.keypoints) > 2
            ]
            if to_remove:
                self._push_undo()
            for i in sorted(to_remove, reverse=True):
                self.curve.remove_keypoint(i)
            self._selected_frames = set()
            self.curveChanged.emit()
            self.update()
        elif event.key() == Qt.Key.Key_I:
            if self._playhead_frame is not None:
                ph = float(self._playhead_frame - self.curve.out_start)
                self._add_at(ph, ph)
        elif event.key() == Qt.Key.Key_Z and (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.undo()
        elif event.key() == Qt.Key.Key_Y and (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.redo()
        elif event.key() == Qt.Key.Key_F:
            self.reset_view()  # fits selected or all
        elif event.key() == Qt.Key.Key_A:
            self._selected_frames = {kp.out_frame for kp in kps}
            self.update()
        elif event.key() == Qt.Key.Key_B:
            self._break_selected()
        elif event.key() == Qt.Key.Key_U:
            self._auto_selected()
        super().keyPressEvent(event)

    def _selected_smooth_indices(self):
        kps = self.curve.sorted_keypoints()
        return [i for i, kp in enumerate(kps)
                if kp.out_frame in self._selected_frames
                and coerce_interp(kp.interp) in
                    (InterpMode.HERMITE, InterpMode.BEZIER, InterpMode.NATURAL)]

    def _break_selected(self):
        idxs = self._selected_smooth_indices()
        if not idxs: return
        self._push_undo()
        for i in idxs:
            self._break_tangents(i, push_undo=False)
        self.curveChanged.emit(); self.update()

    def _auto_selected(self):
        idxs = self._selected_smooth_indices()
        if not idxs: return
        self._push_undo()
        for i in idxs:
            self._reset_tangents(i, push_undo=False)
        self.curveChanged.emit(); self.update()

    def _add_at(self, out_f, in_f):
        self._push_undo()
        out_f = round(float(out_f), 2)
        self.curve.add_keypoint(out_f, round(float(in_f), 2))
        self._selected_frames = {out_f}
        self.curveChanged.emit()
        self.update()

    def add_keyframe_at_playhead(self):
        if self._playhead_frame is not None:
            ph     = float(self._playhead_frame - self.curve.out_start)
            in_val = self.curve.evaluate(ph)
            self._add_at(ph, in_val)

    def delete_keyframe_at_playhead(self):
        if self._playhead_frame is None:
            return
        ph_out = float(self._playhead_frame - self.curve.out_start)
        kps    = self.curve.sorted_keypoints()
        best_i, best_d = None, 999
        for i, kp in enumerate(kps):
            if i == 0 or i == len(kps)-1:
                continue
            d = abs(kp.out_frame - ph_out)
            if d < best_d:
                best_d = d; best_i = i
        if best_i is not None and best_d <= 2 and len(self.curve.keypoints) > 2:
            self._push_undo()
            self._selected_frames.discard(kps[best_i].out_frame)
            self.curve.remove_keypoint(best_i)
            self.curveChanged.emit()
            self.update()

    def _set_interp(self, idx, mode):
        self._push_undo()
        self.curve.set_interp(idx, mode)
        self.curveChanged.emit()
        self.update()

    def _break_tangents(self, idx, push_undo=True):
        kps = self.curve.sorted_keypoints()
        if idx >= len(kps): return
        kp = kps[idx]
        if push_undo: self._push_undo()
        # Materialize current slopes so breaking doesn't snap the curve — both
        # sides keep their present angle, then become independent.
        in_s, out_s = self._current_slopes(kps, idx)
        if kp.in_tangent  is None: kp.in_tangent  = in_s
        if kp.out_tangent is None: kp.out_tangent = out_s
        kp.broken = True
        if push_undo:
            self.curveChanged.emit(); self.update()

    def _unify_tangents(self, idx, push_undo=True):
        kps = self.curve.sorted_keypoints()
        if idx >= len(kps): return
        kp = kps[idx]
        if push_undo: self._push_undo()
        in_s, out_s = self._current_slopes(kps, idx)
        shared = (in_s + out_s) * 0.5 if (in_s is not None and out_s is not None) \
                 else (out_s if out_s is not None else in_s)
        if shared is not None:
            if kp.in_tangent  is not None or kp.out_tangent is not None:
                kp.in_tangent = kp.out_tangent = shared
        kp.broken = False
        if push_undo:
            self.curveChanged.emit(); self.update()

    def _reset_tangents(self, idx, push_undo=True):
        kps = self.curve.sorted_keypoints()
        if idx >= len(kps): return
        kp = kps[idx]
        if push_undo: self._push_undo()
        kp.in_tangent = kp.out_tangent = None
        kp.in_weight  = kp.out_weight  = None
        kp.broken = False
        if push_undo:
            self.curveChanged.emit(); self.update()

    def _current_slopes(self, kps, idx):
        """Return (in_slope, out_slope) currently in effect for keypoint idx —
        override if set, else the solved natural tangent (Natural mode), else the
        local auto slope of the adjacent segment, else None."""
        in_s = out_s = None
        kp = kps[idx]
        nat = None
        if coerce_interp(kp.interp) == InterpMode.NATURAL:
            nat = self.curve._natural_tangents(kps)
        if idx > 0:
            _m0, m1, _d, _h = _auto_tangents(kps, idx-1)
            default_in = nat[idx] if nat is not None else m1
            in_s = kp.in_tangent if kp.in_tangent is not None else default_in
        if idx < len(kps)-1:
            m0, _m1, _d, _h = _auto_tangents(kps, idx)
            default_out = nat[idx] if nat is not None else m0
            out_s = kp.out_tangent if kp.out_tangent is not None else default_out
        return in_s, out_s

    def _delete_kp(self, idx):
        kps = self.curve.sorted_keypoints()
        if idx < len(kps):
            self._push_undo()
            self._selected_frames.discard(kps[idx].out_frame)
        self.curve.remove_keypoint(idx)
        self.curveChanged.emit()
        self.update()

    def _reset(self, speed):
        self._push_undo()
        self.curve.reset(speed)
        self._selected_frames = set()
        self.curveChanged.emit()
        self.update()

    def set_playhead(self, out_frame: int):
        self._playhead_frame = out_frame
        self.update()


# ── CurveEditor: canvas + info bar ───────────────────────────────────────────

class CurveEditor(QWidget):
    curveChanged  = pyqtSignal()
    playheadMoved = pyqtSignal(int)
    # Fired when the user clicks a disabled item in the display-mode dropdown.
    # Qt swallows clicks on disabled items silently — we surface them as a
    # signal so MainWindow can post status-bar feedback.
    disabledModeClicked = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Knob drag state — initialised before any widget is built so handlers
        # can reference it safely even if a stray signal fires during init.
        self._y_knob: "YKnob | None" = None
        self._knob_drag_baselines: dict = {}
        self._knob_drag_baseval: "float | None" = None
        # Match settings_panel tooltip styling for consistency
        self.setStyleSheet(
            "QToolTip{background:#1e1e22;color:#ffffff;"
            "border:1px solid #4a9eff;font-family:monospace;font-size:9pt;}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        info_bar = QWidget()
        info_bar.setFixedHeight(28)
        info_bar.setStyleSheet("background:#161618; border-bottom:1px solid #2a2a2e;")
        ib = QHBoxLayout(info_bar)
        ib.setContentsMargins(8, 2, 8, 2)
        ib.setSpacing(8)

        # Layout: [keypoint group flush left] [stretch] [Interpolation] [stretch].
        # With the keypoint group on the left and nothing on the right, the two
        # equal-weight stretches land Interpolation closer to visual center.

        # ── Keypoint info group (flush left) ─────────────────────────────────

        self._kp_label = QLabel("2 keys")
        self._kp_label.setStyleSheet("color:#6a6a72; font-size:9pt; min-width:40px;")
        ib.addWidget(self._kp_label)

        lbl_in = QLabel("X")
        lbl_in.setStyleSheet("color:#6a6a72; font-size:9pt;")
        lbl_in.setToolTip("X \u2014 output (timeline) frame")
        ib.addWidget(lbl_in)

        self._in_input = QLineEdit()
        self._in_input.setFixedWidth(54)
        self._in_input.setStyleSheet(
            "background:#1e1e21; border:1px solid #3a3a40; color:#e8e8ec;"
            "font-family:monospace; font-size:9pt; padding:1px 4px;")
        self._in_input.setPlaceholderText("--")
        self._in_input.setToolTip(
            "Output (timeline) frame for the selected keyframe.\n"
            "X-axis position on the curve. Always a whole number.\n"
            "Press Enter to commit; click outside to cancel.\n"
            "Empty when nothing is selected or multiple keyframes are selected.")
        self._in_input.returnPressed.connect(self._on_in_input)
        ib.addWidget(self._in_input)

        arr = QLabel("\u00b7")
        arr.setStyleSheet("color:#3a3a40; font-size:9pt;")
        ib.addWidget(arr)

        lbl_out = QLabel("Y")
        lbl_out.setStyleSheet("color:#6a6a72; font-size:9pt;")
        lbl_out.setToolTip("Y \u2014 source (in) frame")
        ib.addWidget(lbl_out)

        self._out_input = QLineEdit()
        self._out_input.setFixedWidth(54)
        self._out_input.setStyleSheet(
            "background:#1e1e21; border:1px solid #3a3a40; color:#e8e8ec;"
            "font-family:monospace; font-size:9pt; padding:1px 4px;")
        self._out_input.setPlaceholderText("--")
        self._out_input.setToolTip(
            "Source (in) frame for the selected keyframe.\n"
            "Y-axis position on the curve. Supports fractional frames (e.g. 1144.49).\n"
            "Press Enter to commit; click outside to cancel.\n"
            "Empty when nothing is selected or multiple keyframes are selected.")
        self._out_input.returnPressed.connect(self._on_out_input)
        ib.addWidget(self._out_input)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.VLine)
        sep2.setStyleSheet("color:#2a2a2e; margin:0 6px;")
        ib.addWidget(sep2)

        BTN_ADD = (
            "QPushButton{background:#1a2a1a;border:1px solid #3ecf6e;"
            "color:#3ecf6e;font-family:monospace;font-size:11pt;padding:0 6px;}"
            "QPushButton:hover{background:#243a24;color:#5adf8e;}"
            "QPushButton:pressed{background:#161618;}"
            "QToolTip{background:#1e1e22;color:#ffffff;"
            "border:1px solid #4a9eff;font-family:monospace;font-size:9pt;}"
        )
        BTN_DEL = (
            "QPushButton{background:#2a1a1a;border:1px solid #e04a4a;"
            "color:#e04a4a;font-family:monospace;font-size:11pt;padding:0 6px;}"
            "QPushButton:hover{background:#3a2424;color:#ff6a6a;}"
            "QPushButton:pressed{background:#161618;}"
            "QToolTip{background:#1e1e22;color:#ffffff;"
            "border:1px solid #4a9eff;font-family:monospace;font-size:9pt;}"
        )
        # Neutral style -- Reset is neither additive nor destructive to keypoint
        # existence; it only resets tangents on the selected keyframe(s).
        BTN_RESET = (
            "QPushButton{background:#1e1e21;border:1px solid #3a3a40;"
            "color:#c8c8cc;font-family:monospace;font-size:11pt;padding:0 6px;}"
            "QPushButton:hover{background:#27272b;color:#e8e8ec;border-color:#4a4a52;}"
            "QPushButton:pressed{background:#161618;border-color:#4a9eff;}"
            "QToolTip{background:#1e1e22;color:#ffffff;"
            "border:1px solid #4a9eff;font-family:monospace;font-size:9pt;}"
        )

        self._btn_add = QPushButton("+")
        self._btn_add.setFixedHeight(22); self._btn_add.setFixedWidth(28)
        self._btn_add.setStyleSheet(BTN_ADD)
        self._btn_add.setToolTip(
            "Add a keyframe at the current playhead position.\n"
            "The Y value is taken from the existing curve at that X.\n"
            "Shortcut: I\n"
            "Alternative: Ctrl+Alt+click anywhere on the canvas.")
        self._btn_add.clicked.connect(lambda: self.canvas.add_keyframe_at_playhead())
        ib.addWidget(self._btn_add)

        self._btn_del = QPushButton("\u00d7")
        self._btn_del.setFixedHeight(22); self._btn_del.setFixedWidth(28)
        self._btn_del.setStyleSheet(BTN_DEL)
        self._btn_del.setToolTip(
            "Delete the keyframe at (or near) the playhead.\n"
            "The first and last keyframes cannot be deleted --\n"
            "they define the curve's working range.\n"
            "Alternative: select keyframes and press Del.")
        self._btn_del.clicked.connect(lambda: self.canvas.delete_keyframe_at_playhead())
        ib.addWidget(self._btn_del)

        self._btn_reset = QPushButton("\u21ba")
        self._btn_reset.setFixedHeight(22); self._btn_reset.setFixedWidth(28)
        self._btn_reset.setStyleSheet(BTN_RESET)
        self._btn_reset.setToolTip(
            "Reset the curve to default (identity).\n"
            "All keypoints will be deleted, leaving only the two endpoints.\n"
            "Affects the active snapshot only; use Ctrl+Z to undo.")
        self._btn_reset.clicked.connect(self._on_reset_curve_clicked)
        ib.addWidget(self._btn_reset)

        ib.addStretch()   # leading stretch

        # ── Centre cluster: display dropdown, Y scrubber knob, separator, Interp
        self.display_mode_combo = QComboBox()
        self.display_mode_combo.addItems(["Frame", "TC (metadata)", "TC (fps)"])
        self.display_mode_combo.setFixedWidth(120)
        self.display_mode_combo.setFixedHeight(22)
        self.display_mode_combo.setToolTip(
            "Frame-number display mode for the Y scrubber knob to the right.\n"
            "Shared with the viewer's display mode: changing one updates both.\n"
            "  Frame          plain frame numbers\n"
            "  TC (metadata)  timecode from the file's embedded SMPTE TC\n"
            "                 (disabled when the sequence has no TC)\n"
            "  TC (fps)       timecode computed from frame count and fps")
        self.display_mode_combo.currentIndexChanged.connect(
            self._on_display_mode_user_changed)
        # Hook the popup view so clicks on disabled items surface as feedback
        # (Qt swallows those clicks silently — see disabledModeClicked).
        self.display_mode_combo.view().viewport().installEventFilter(self)
        ib.addWidget(self.display_mode_combo)

        self._y_knob = YKnob()
        self._y_knob.dragStarted.connect(self._on_knob_drag_started)
        self._y_knob.valueScrubbing.connect(self._on_knob_scrubbing)
        self._y_knob.dragFinished.connect(self._on_knob_drag_finished)
        ib.addWidget(self._y_knob)

        sep_cluster = QFrame(); sep_cluster.setFrameShape(QFrame.Shape.VLine)
        sep_cluster.setStyleSheet("color:#2a2a2e; margin:0 6px;")
        ib.addWidget(sep_cluster)

        # ── Interpolation control (centered) ─────────────────────────────────
        lbl_interp = QLabel("Interpolation")
        lbl_interp.setStyleSheet("color:#6a6a72; font-size:9pt;")
        ib.addWidget(lbl_interp)

        self._interp_combo = QComboBox()
        # Editable with a read-only line edit: lets us DISPLAY arbitrary text
        # ("Mixed") without it being a row in the popup. The popup contains only
        # the five real modes; the user can't type (read-only) -- it's just a
        # display surface. Styling comes from the global stylesheet.
        self._interp_combo.setEditable(True)
        self._interp_combo.lineEdit().setReadOnly(True)
        self._interp_combo.lineEdit().setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._interp_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._interp_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        # Real selectable modes only (not the legacy SMOOTH alias). No "Mixed"
        # row -- "Mixed" is shown via the line edit text when needed.
        self._combo_modes = [InterpMode.CONSTANT, InterpMode.LINEAR,
                             InterpMode.HERMITE, InterpMode.BEZIER, InterpMode.NATURAL]
        for m in self._combo_modes:
            self._interp_combo.addItem(INTERP_LABELS[m])
        self._interp_combo.setToolTip(
            "Interpolation mode for the selected keyframe(s).\n"
            "Changing it applies to all selected keyframes.")
        self._interp_combo.activated.connect(self._on_interp_combo)
        self._interp_label = self._interp_combo   # back-compat references
        ib.addWidget(self._interp_combo)

        # ── Tangent operation buttons ─────────────────────────────────────────
        sep_tan = QFrame(); sep_tan.setFrameShape(QFrame.Shape.VLine)
        sep_tan.setStyleSheet("color:#2a2a2e; margin:0 6px;")
        ib.addWidget(sep_tan)

        self._btn_break = TangentIconButton("break")
        self._btn_break.setFixedHeight(22); self._btn_break.setFixedWidth(28)
        self._btn_break.setStyleSheet(BTN_RESET)  # neutral styling matches ↺
        self._btn_break.setToolTip("Break tangents (B)")
        self._btn_break.clicked.connect(lambda: self.canvas._break_selected())
        ib.addWidget(self._btn_break)

        self._btn_auto = TangentIconButton("auto")
        self._btn_auto.setFixedHeight(22); self._btn_auto.setFixedWidth(28)
        self._btn_auto.setStyleSheet(BTN_RESET)
        self._btn_auto.setToolTip("Auto tangents (U)")
        self._btn_auto.clicked.connect(lambda: self.canvas._auto_selected())
        ib.addWidget(self._btn_auto)

        ib.addStretch()   # trailing stretch

        layout.addWidget(info_bar)

        self.canvas = CurveCanvas()
        self.canvas.curveChanged.connect(self._on_canvas_changed)
        self.canvas.playheadMoved.connect(self.playheadMoved)
        layout.addWidget(self.canvas, stretch=1)

    def _on_canvas_changed(self):
        n = len(self.canvas.curve.keypoints)
        self._kp_label.setText(f"{n} key{'s' if n != 1 else ''}")
        self._update_info_bar()
        self.curveChanged.emit()

    @staticmethod
    def _kp_at(kps, frame, tol=0.05):
        """Find the keypoint whose out_frame matches `frame`.
        Uses nearest-within-tolerance instead of exact float equality —
        selection values and stored out_frames can differ by float dust or
        by rounding-precision mismatches (e.g. add path rounds to 1dp,
        the model rounds to 2dp), which made exact `==` silently miss."""
        if not kps:
            return None
        best = min(kps, key=lambda k: abs(k.out_frame - frame))
        return best if abs(best.out_frame - frame) <= tol else None

    def _set_combo_mode(self, mode_or_mixed):
        """Set the dropdown display without firing its activated signal.
        'mixed' -> show the word 'Mixed' as free text (no matching row);
        a mode -> select that row; None -> blank."""
        self._interp_combo.blockSignals(True)
        if mode_or_mixed == "mixed":
            self._interp_combo.setCurrentIndex(-1)
            self._interp_combo.lineEdit().setText("Mixed")
        elif mode_or_mixed is None:
            self._interp_combo.setCurrentIndex(-1)
            self._interp_combo.lineEdit().setText("")
        else:
            m = coerce_interp(mode_or_mixed)
            if m in self._combo_modes:
                idx = self._combo_modes.index(m)
                self._interp_combo.setCurrentIndex(idx)
                self._interp_combo.lineEdit().setText(INTERP_LABELS[m])
        self._interp_combo.blockSignals(False)

    def _update_info_bar(self):
        sel = self.canvas._selected_frames
        kps = self.canvas.curve.sorted_keypoints()
        # Knob's value is the in_frame (Y) of the selected kp, or None when
        # selection is empty / multi-mixed-Y. Skip refresh while a drag is in
        # flight so the knob's live readout isn't clobbered. Knob may not yet
        # exist during _build_ui — guard against that too.
        if self._y_knob is not None and not self._y_knob._dragging:
            self._refresh_knob_value(sel, kps)
        if len(sel) == 1:
            frame = next(iter(sel))
            kp = self._kp_at(kps, frame)
            if kp:
                # X field = out timeline (whole); Y field = source (fractional).
                out_val = self.canvas.curve.out_start + kp.out_frame
                out_str = f"{out_val:.2f}" if out_val != round(out_val) else str(int(out_val))
                in_val  = self.canvas.curve.in_start + kp.in_frame
                in_str  = f"{in_val:.2f}" if in_val != round(in_val) else str(int(in_val))
                self._in_input.setText(out_str)
                self._out_input.setText(in_str)
                self._set_combo_mode(kp.interp)
                return
        elif len(sel) > 1:
            # Show the shared mode, or "Mixed" when they differ.
            modes = {coerce_interp(kp.interp) for kp in kps if kp.out_frame in sel}
            self._in_input.setText("")
            self._out_input.setText("")
            self._set_combo_mode(next(iter(modes)) if len(modes) == 1 else "mixed")
            return
        self._in_input.setText("")
        self._out_input.setText("")
        self._set_combo_mode(None)

    def _refresh_knob_value(self, sel, kps):
        """Compute the knob's value from the current selection and push it.
        Single selection: absolute source frame of the kp.
        Multi with matching Y: absolute source frame (shared).
        Multi with mixed Y or no selection: None ('--')."""
        if not sel:
            self._y_knob.set_value(None)
            return
        selected_kps = [kp for kp in kps if kp.out_frame in sel]
        if not selected_kps:
            self._y_knob.set_value(None)
            return
        in_vals = {round(kp.in_frame, 4) for kp in selected_kps}
        if len(in_vals) != 1:
            self._y_knob.set_value(None)
            return
        # Knob displays absolute source frame (matches viewer / Y line edit).
        rel = next(iter(in_vals))
        self._y_knob.set_value(self.canvas.curve.in_start + rel)

    # ── Display mode + knob handlers ──────────────────────────────────────────

    def _resolve_main_window(self):
        """Walk parent chain looking for a MainWindow-like host with the
        shared display_mode signals. Returns None if not embedded yet."""
        w = self.parentWidget()
        while w is not None:
            if hasattr(w, "displayModeChanged") and hasattr(w, "set_display_mode"):
                return w
            w = w.parentWidget()
        return None

    def _on_display_mode_user_changed(self, _idx=None):
        """Curve-editor dropdown change → route to MainWindow's shared state."""
        text = self.display_mode_combo.currentText()
        mw = self._resolve_main_window()
        if mw is not None:
            mw.set_display_mode(text)
        # Even without an mw (tests), apply locally so the knob reformats.
        self.apply_display_mode(text)

    def apply_display_mode(self, mode: str):
        """Apply a display mode received from MainWindow. Syncs combo with
        blocked signals, then re-installs the knob formatter."""
        if self.display_mode_combo.currentText() != mode:
            self.display_mode_combo.blockSignals(True)
            self.display_mode_combo.setCurrentText(mode)
            self.display_mode_combo.blockSignals(False)
        self._install_knob_formatter(mode)

    def apply_tc_metadata_available(self, available: bool):
        """Enable/disable the TC (metadata) entry on this combo in lockstep
        with the viewer's combo. Selection fallback is handled by the viewer."""
        idx = self.display_mode_combo.findText("TC (metadata)")
        if idx < 0:
            return
        item = self.display_mode_combo.model().item(idx)
        if item is not None:
            item.setEnabled(bool(available))

    def eventFilter(self, obj, event):
        """Surface clicks on disabled display-mode items as a signal so the
        MainWindow can post status-bar feedback. Qt would otherwise swallow
        the click silently (no currentIndexChanged for disabled items)."""
        from PyQt6.QtCore import QEvent
        view = self.display_mode_combo.view()
        if obj is view.viewport() and event.type() == QEvent.Type.MouseButtonPress:
            idx = view.indexAt(event.position().toPoint())
            if idx.isValid():
                item = self.display_mode_combo.model().item(idx.row())
                if item is not None and not item.isEnabled():
                    self.disabledModeClicked.emit(item.text())
        return super().eventFilter(obj, event)

    def _install_knob_formatter(self, mode: str):
        """Build a formatter that reuses the viewer's display_frame() so TC
        formatting is single-sourced. The knob value IS the absolute source
        frame, so we pass is_src=True (matches viewer's source-frame TC path)."""
        mw = self._resolve_main_window()
        viewer = getattr(mw, "viewer", None) if mw else None
        if viewer is None or not hasattr(viewer, "display_frame"):
            self._y_knob.set_formatter(
                lambda v: ("--" if v is None else str(int(round(v)))))
            return
        def fmt(v):
            if v is None:
                return "--"
            return viewer.display_frame(int(round(v)), is_src=True)
        self._y_knob.set_formatter(fmt)

    # ── Knob drag handlers ────────────────────────────────────────────────────

    def _on_knob_drag_started(self):
        """Capture pre-drag state for undo and per-kp baseline values."""
        sel = self.canvas._selected_frames
        kps = self.canvas.curve.sorted_keypoints()
        self._knob_drag_baselines = {}  # id(kp) → in_frame at press
        self._knob_drag_baseval = None  # the knob's value at press (absolute)
        if not sel:
            return
        selected_kps = [kp for kp in kps if kp.out_frame in sel]
        in_vals = {round(kp.in_frame, 4) for kp in selected_kps}
        if not selected_kps or len(in_vals) != 1:
            return
        self.canvas._push_undo()
        self._knob_drag_baseval = (
            self.canvas.curve.in_start + next(iter(in_vals)))
        for kp in selected_kps:
            self._knob_drag_baselines[id(kp)] = kp.in_frame

    def _on_knob_scrubbing(self, live_value: float):
        """Apply (live_value - baseval) delta to each selected kp's in_frame."""
        if self._knob_drag_baseval is None or not self._knob_drag_baselines:
            return
        delta = live_value - self._knob_drag_baseval
        in_total = float(self.canvas._in_total())
        kps = self.canvas.curve.sorted_keypoints()
        for kp in kps:
            if id(kp) in self._knob_drag_baselines:
                base = self._knob_drag_baselines[id(kp)]
                new_in = max(0.0, min(in_total, base + delta))
                kp.in_frame = new_in
        self.canvas.curveChanged.emit()
        self.canvas.update()

    def _on_knob_drag_finished(self, final_value: int):
        """Round each selected kp's in_frame to an integer and commit."""
        if self._knob_drag_baseval is None or not self._knob_drag_baselines:
            return
        delta = float(final_value) - self._knob_drag_baseval
        in_total = float(self.canvas._in_total())
        kps = self.canvas.curve.sorted_keypoints()
        for kp in kps:
            if id(kp) in self._knob_drag_baselines:
                base = self._knob_drag_baselines[id(kp)]
                new_in = max(0.0, min(in_total, round(base + delta)))
                kp.in_frame = float(new_in)
        self._knob_drag_baselines = {}
        self._knob_drag_baseval = None
        self.canvas.curveChanged.emit()
        self.canvas.update()
        # Refresh info bar so the Y line edit text catches up.
        self._update_info_bar()

    def _on_interp_combo(self, index):
        """Apply the chosen mode to all selected keyframes."""
        if index < 0 or index >= len(self._combo_modes):
            return  # "Mixed" row or out of range — no-op
        mode = self._combo_modes[index]
        kps = self.canvas.curve.sorted_keypoints()
        idxs = [i for i, kp in enumerate(kps)
                if kp.out_frame in self.canvas._selected_frames]
        if not idxs:
            return
        self.canvas._push_undo()
        for i in idxs:
            self.canvas.curve.set_interp(i, mode)
        self.canvas.curveChanged.emit()
        self.canvas.update()
        self._update_info_bar()

    def _on_in_input(self):
        """in field = out timeline position (X axis). Whole numbers only."""
        sel = self.canvas._selected_frames
        if len(sel) != 1: return
        frame = next(iter(sel))
        kps = self.canvas.curve.sorted_keypoints()
        kp  = self._kp_at(kps, frame)
        if kp is None: return
        try:
            val = int(float(self._in_input.text()))
            rel = val - self.canvas.curve.out_start
            rel = max(0, min(int(self.canvas._out_total()), rel))
            idx = kps.index(kp)
            if idx != 0 and idx != len(kps)-1:
                self.canvas._push_undo()
                self.canvas._selected_frames.discard(kp.out_frame)
                kp.out_frame = float(rel)
                self.canvas._selected_frames.add(float(rel))
                self.canvas.curve.keypoints.sort(key=lambda k: k.out_frame)
            self.canvas.curveChanged.emit()
            self.canvas.update()
        except ValueError:
            pass

    def _on_out_input(self):
        """out field = source in frame (Y axis). Decimals allowed."""
        sel = self.canvas._selected_frames
        if len(sel) != 1: return
        frame = next(iter(sel))
        kps = self.canvas.curve.sorted_keypoints()
        kp  = self._kp_at(kps, frame)
        if kp is None: return
        try:
            val    = round(float(self._out_input.text()), 2)
            in_rel = val - self.canvas.curve.in_start
            in_rel = max(0.0, min(float(self.canvas._in_total()), in_rel))
            self.canvas._push_undo()
            kp.in_frame = in_rel
            self.canvas.curveChanged.emit()
            self.canvas.update()
        except ValueError:
            pass

    @property
    def curve(self):
        return self.canvas.curve

    def set_curve(self, curve):
        self.canvas.curve            = curve
        self.canvas._selected_frames = set()
        self._kp_label.setText(f"{len(curve.keypoints)} keys")
        self._update_info_bar()
        self.canvas.update()

    def set_mode(self, mode):
        pass

    def set_playhead(self, out_frame: int):
        self.canvas.set_playhead(out_frame)
        self._update_info_bar()

    def _reset(self, speed):
        self.canvas._reset(speed)

    def _on_reset_curve_clicked(self):
        """Reset button (↺) handler: modal-confirm full-curve reset.
        Acts on the active snapshot only; per-Model-A the canvas's curve IS
        the active snapshot's curve, so canvas._reset(1.0) is the right path
        (it pushes undo, calls curve.reset(speed), clears selection, emits
        curveChanged). Per-keypoint tangent reset stays on the right-click
        menu — see _reset_tangents / _auto_selected for that path."""
        box = QMessageBox(self)
        box.setWindowTitle("Reset curve?")
        box.setText("Reset curve to default? All keypoints will be deleted.")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setStandardButtons(
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Reset)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        # Relabel Reset button so the action verb matches the title.
        reset_btn = box.button(QMessageBox.StandardButton.Reset)
        if reset_btn is not None:
            reset_btn.setText("Reset")
        if box.exec() != QMessageBox.StandardButton.Reset:
            return
        self.canvas._reset(1.0)
        self._update_info_bar()
