"""
Sequence viewer with RAM cache, background preloader, Nuke-style
cache indicator on the scrubber, and viewport zoom/pan.

Pan:         Middle mouse drag
Zoom:        Alt + middle mouse drag  
Fit to view: F key or double-click
"""
from __future__ import annotations
import os
import math
import threading
import numpy as np
from typing import Optional, List, Tuple, Dict, Set

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QLineEdit, QFrame, QDoubleSpinBox, QComboBox, QSlider
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QRectF, QPointF
from PyQt6.QtGui import (
    QPainter, QImage, QPixmap, QColor, QPen, QFont,
    QBrush, QMouseEvent, QKeyEvent, QWheelEvent, QPolygonF, QFontMetrics
)

try:
    import OpenImageIO as oiio
    HAS_OIIO = True
except ImportError:
    HAS_OIIO = False

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False


# ── Image loading ─────────────────────────────────────────────────────────────

def _linear_to_srgb(arr):
    """Convert linear floating-point image data to sRGB-encoded values in [0,1].

    Uses the official sRGB transfer function:
        x <= 0.0031308:   12.92 * x
        x  > 0.0031308:   1.055 * x^(1/2.4) - 0.055

    Input is clipped to [0,1] first (negative or super-white values are not encodable).
    """
    arr = np.clip(arr, 0.0, 1.0)
    # Split into linear toe and gamma curve regions
    linear_region = arr <= 0.0031308
    out = np.empty_like(arr)
    out[linear_region]  = 12.92 * arr[linear_region]
    # Use np.power for the curve segment; values here are guaranteed >0 so no NaN risk
    curve = ~linear_region
    out[curve] = 1.055 * np.power(arr[curve], 1.0/2.4) - 0.055
    return out


def _apply_display(arr, exposure, gamma):
    """Standard display pipeline for the viewer.

    1. Apply exposure (in stops, multiplicative in linear space).
    2. Convert linear -> sRGB encoded values using the proper sRGB transfer function.
    3. Apply the user's additional gamma adjustment on top.

    With gamma=1.0, output matches a standard sRGB view transform exactly.
    With gamma!=1.0, it's an extra adjustment on top of that view —
    same mental model as Nuke's Viewer gain/gamma knobs.
    """
    arr = arr * (2.0 ** exposure)
    arr = _linear_to_srgb(arr)
    if gamma != 1.0:
        # Avoid division by zero; gamma slider min is 0.5 so this is just safety
        g = max(0.01, gamma)
        arr = np.power(np.clip(arr, 0.0, 1.0), 1.0 / g)
    return arr


def load_frame(path, exposure=0.0, gamma=1.0):
    if not os.path.exists(path):
        return None
    ext = os.path.splitext(path)[1].lower()
    if HAS_OIIO and ext in ('.exr', '.dpx', '.tif', '.tiff', '.png'):
        return _load_oiio(path, exposure, gamma)
    elif HAS_CV2:
        return _load_cv2(path, exposure, gamma)
    return None

def _load_oiio(path, exposure, gamma):
    inp = oiio.ImageInput.open(path)
    if not inp: return None
    pixels = inp.read_image(oiio.FLOAT)
    inp.close()
    if pixels is None: return None
    arr = np.array(pixels, dtype=np.float32)
    if arr.ndim == 2: arr = np.stack([arr,arr,arr], axis=-1)
    if arr.shape[2] > 3: arr = arr[:,:,:3]
    arr = _apply_display(arr, exposure, gamma)
    arr = (np.clip(arr, 0.0, 1.0) * 255).astype(np.uint8)
    h, w, _ = arr.shape
    return QImage(arr[:,:,[0,1,2]].copy().data, w, h, w*3,
                  QImage.Format.Format_RGB888).copy()

def _load_cv2(path, exposure, gamma):
    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if img is None: return None
    if img.dtype == np.uint16: arr = img.astype(np.float32) / 65535.0
    elif img.dtype in (np.float32, np.float64): arr = img.astype(np.float32)
    else: arr = img.astype(np.float32) / 255.0
    if arr.ndim == 2: arr = np.stack([arr,arr,arr], axis=-1)
    if arr.shape[2] == 4: arr = arr[:,:,:3]
    arr = _apply_display(arr, exposure, gamma)
    arr = (np.clip(arr, 0.0, 1.0) * 255).astype(np.uint8)
    h, w, _ = arr.shape
    return QImage(cv2.cvtColor(arr, cv2.COLOR_BGR2RGB).data,
                  w, h, w*3, QImage.Format.Format_RGB888).copy()


# ── Timecode helpers ──────────────────────────────────────────────────────────

def _decode_smpte_bcd(word: int) -> str:
    """Unpack a 32-bit SMPTE BCD-packed timecode word to 'HH:MM:SS:FF'."""
    ff = ((word >> 4) & 0x3) * 10 + (word & 0xF)
    ss = ((word >> 12) & 0x7) * 10 + ((word >> 8) & 0xF)
    mm = ((word >> 20) & 0x7) * 10 + ((word >> 16) & 0xF)
    hh = ((word >> 28) & 0x3) * 10 + ((word >> 24) & 0xF)
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"

def _tc_str_to_frames(tc: str, fps: float) -> int:
    """Parse 'HH:MM:SS:FF' (non-drop) to absolute frame count."""
    parts = tc.replace(";", ":").split(":")
    if len(parts) != 4:
        return 0
    try:
        hh, mm, ss, ff = (int(p) for p in parts)
    except ValueError:
        return 0
    return int((hh * 3600 + mm * 60 + ss) * round(fps)) + ff

def _frames_to_tc_str(n: int, fps: float) -> str:
    """Convert a non-negative frame count to 'HH:MM:SS:FF'."""
    n = max(0, n)
    fps_i = max(1, round(fps))
    ff = n % fps_i
    s  = n // fps_i
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}:{ff:02d}"

def _probe_timecode(path: str):
    """Read timecode metadata without decoding pixels (~1 ms DPX, ~0.08 ms EXR).

    Returns (tc_str, fps, is_drop_frame):
        tc_str        'HH:MM:SS:FF' string, or None if absent.
                      None means the attribute is genuinely missing -- (0,0) SMPTE
                      encodes 00:00:00:00 and is treated as present, not absent.
        fps           float from file metadata, or None
        is_drop_frame True when the SMPTE DF flag (bit 6) is set or ';' separator found

    Drop-frame TC is detected and flagged; callers show '--:--:--:--' rather than
    computing incorrect non-drop offsets.
    # Midnight rollover (TC wrapping past 23:59:59:FF) is NOT corrected --
    # shot lengths don't approach 24 h so this is acceptable.
    """
    if not HAS_OIIO or not os.path.exists(path):
        return None, None, False
    inp = oiio.ImageInput.open(path)
    if not inp:
        return None, None, False
    spec = inp.spec()
    inp.close()

    tc_str, is_drop = None, False

    # DPX: pre-formatted string is cheapest and most reliable
    dpx_tc = spec.getattribute("dpx:TimeCode")
    if dpx_tc is not None and isinstance(dpx_tc, str):
        tc_str = dpx_tc
        smpte  = spec.getattribute("smpte:TimeCode")
        if smpte is not None:
            is_drop = bool(smpte[0] & (1 << 6))

    # EXR / DPX fallback: BCD-packed SMPTE uint32 pair
    if tc_str is None:
        smpte = spec.getattribute("smpte:TimeCode")
        if smpte is not None:           # (0, 0) means TC=00:00:00:00, NOT absent
            is_drop = bool(smpte[0] & (1 << 6))
            tc_str  = _decode_smpte_bcd(smpte[0])

    # Nuke-rendered EXR: plain string fallback
    if tc_str is None:
        nuke_tc = spec.getattribute("nuke/input/timecode")
        if nuke_tc is not None:
            tc_str = nuke_tc

    # String-separator drop-frame detection (belt-and-suspenders)
    if tc_str and ";" in tc_str:
        is_drop = True

    # FPS from file metadata
    fps = None
    fr = spec.getattribute("framesPerSecond")   # EXR rational2i -> tuple in OIIO
    if fr is not None:
        try:
            fps = (fr[0] / fr[1]
                   if isinstance(fr, (list, tuple)) and len(fr) == 2
                   else float(fr))
        except (TypeError, ZeroDivisionError, ValueError):
            fps = None
    if fps is None:
        dfr = spec.getattribute("dpx:FrameRate")
        if dfr is not None:
            try:
                fps = float(dfr)
            except (TypeError, ValueError):
                fps = None

    return tc_str, fps, is_drop


# ── Background cache loader ───────────────────────────────────────────────────

class FrameCache:
    def __init__(self):
        self._cache: Dict[int, QImage] = {}
        self._lock  = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._on_progress = None
        self._dir = ""; self._prefix = ""; self._padding = 4
        self._ext = ".exr"; self._start = 1001; self._end = 1072
        self._exposure = 0.0; self._gamma = 1.0; self._playhead = 1001

    def configure(self, directory, prefix, padding, ext, start, end, exposure, gamma):
        self._stop_preload()
        with self._lock: self._cache.clear()
        self._dir=directory; self._prefix=prefix; self._padding=padding
        self._ext=ext; self._start=start; self._end=end
        self._exposure=exposure; self._gamma=gamma
        self._start_preload()

    def invalidate(self):
        self._stop_preload()
        with self._lock: self._cache.clear()
        self._start_preload()

    def set_playhead(self, frame: int): self._playhead = frame

    def get(self, frame: int) -> Optional[QImage]:
        with self._lock: return self._cache.get(frame)

    def cached_frames(self) -> Set[int]:
        with self._lock: return set(self._cache.keys())

    def _frame_path(self, f):
        return os.path.join(self._dir,
            f"{self._prefix}{f:0{self._padding}d}{self._ext}")

    def _stop_preload(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._stop_event.clear()

    def _start_preload(self):
        if not self._dir: return
        self._thread = threading.Thread(target=self._preload_worker, daemon=True)
        self._thread.start()

    def _preload_worker(self):
        start, end = self._start, self._end
        ph = max(start, min(end, self._playhead))
        forward  = list(range(ph, end+1))
        backward = list(range(ph-1, start-1, -1))
        order = []
        fi, bi = 0, 0
        while fi < len(forward) or bi < len(backward):
            if fi < len(forward):  order.append(forward[fi]);  fi += 1
            if bi < len(backward): order.append(backward[bi]); bi += 1
        for frame in order:
            if self._stop_event.is_set(): return
            with self._lock:
                if frame in self._cache: continue
            img = load_frame(self._frame_path(frame), self._exposure, self._gamma)
            if img and not self._stop_event.is_set():
                with self._lock: self._cache[frame] = img
                if self._on_progress:
                    try: self._on_progress()
                    except Exception: pass

    def stop(self): self._stop_preload()


# ── Viewer canvas with zoom/pan ───────────────────────────────────────────────

class ViewerCanvas(QWidget):
    # Emitted on Alt+LMB drag. Carries the horizontal pixel delta from the press
    # point and the canvas width, so the parent can scrub RELATIVE to the frame
    # the drag started on (no positional snap). scrubBegan marks the anchor.
    scrubBegan    = pyqtSignal()
    scrubRequested = pyqtSignal(float, float)  # (dx_pixels, canvas_width)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap = None
        self._burn_lines = []   # [line1_frame, line2_tc_meta, line3_tc_fps]
        self._mode = "SOURCE"

        # Zoom/pan state
        self._zoom   = 1.0
        self._offset = QPointF(0.0, 0.0)   # offset in widget pixels
        self._pan_start  = None
        self._pan_offset_start = None
        self._zoom_drag_start  = None
        self._zoom_drag_zoom   = 1.0
        self._alt_held = False
        self._scrubbing = False
        self._scrub_press_x = 0.0

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 180)
        self.setStyleSheet("background-color: #080809;")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_frame(self, img, burn_lines, mode):
        self._pixmap = QPixmap.fromImage(img) if img else None
        self._burn_lines = burn_lines if burn_lines else []
        self._mode = mode
        self.update()

    def fit_to_view(self):
        """Reset zoom and center the image."""
        self._zoom   = 1.0
        self._offset = QPointF(0.0, 0.0)
        self.update()

    def _image_rect(self):
        """Returns the rect where the image should be drawn at current zoom/pan."""
        if not self._pixmap:
            return QRectF(0, 0, self.width(), self.height())
        w, h = self.width(), self.height()
        pw, ph = self._pixmap.width(), self._pixmap.height()
        base_scale = min(w / pw, h / ph)
        scaled_w = pw * base_scale * self._zoom
        scaled_h = ph * base_scale * self._zoom
        cx = w / 2 + self._offset.x()
        cy = h / 2 + self._offset.y()
        return QRectF(cx - scaled_w/2, cy - scaled_h/2, scaled_w, scaled_h)

    def paintEvent(self, _):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor("#080809"))

        if self._pixmap:
            r = self._image_rect()
            p.drawPixmap(r.toRect(), self._pixmap)

            # Helper: draw white text with a subtle 1px dark shadow so it stays
            # readable over both dark and bright parts of the image — no opaque
            # background box.
            def _overlay_text(x, y, text, color="#ffffff"):
                p.setPen(QPen(QColor(0, 0, 0, 160)))
                p.drawText(int(x) + 1, int(y) + 1, text)
                p.setPen(QPen(QColor(color)))
                p.drawText(int(x), int(y), text)

            # Two-column burn-in — bottom-right, stacks upward.
            # Each entry is (left, right): left values are right-aligned into a
            # column so their right edges line up; right '(src ...)' parentheticals
            # are left-aligned into a second column starting at a consistent gap.
            # List order [frame, TC-metadata, TC-fps] is drawn bottom-up so the
            # frame line sits on top. In SOURCE mode the right column is empty,
            # collapsing to a single right-aligned column (gap = 0).
            if self._burn_lines:
                p.setFont(QFont("Monospace", 9))
                fm  = QFontMetrics(p.font())
                lh  = fm.height() + 2
                rows = [pair for pair in self._burn_lines if pair[0] or pair[1]]
                if rows:
                    left_w  = max(fm.horizontalAdvance(l) for l, _ in rows)
                    right_w = max(fm.horizontalAdvance(r) for _, r in rows)
                    gap     = fm.horizontalAdvance("  ") if right_w else 0
                    block_x   = w - 12 - (left_w + gap + right_w)
                    left_edge = block_x + left_w          # right edge of left column
                    right_x   = block_x + left_w + gap    # left edge of right column
                    y = h - 12
                    for left, right in reversed(rows):
                        if left:
                            _overlay_text(left_edge - fm.horizontalAdvance(left),
                                          y, left, "#ffffff")
                        if right:
                            _overlay_text(right_x, y, right, "#ffffff")
                        y -= lh

            # Zoom level indicator
            if abs(self._zoom - 1.0) > 0.01:
                zoom_txt = f"{self._zoom*100:.0f}%"
                p.setFont(QFont("Monospace", 9))
                _overlay_text(w-56, 21, zoom_txt, "#ffffff")
        else:
            p.setPen(QPen(QColor("#3a3a40")))
            p.setFont(QFont("Monospace", 11))
            p.drawText(QRectF(0,0,w,h), Qt.AlignmentFlag.AlignCenter,
                       "No sequence loaded\nSet input directory in the I/O panel")

        # Hint
        p.setPen(QPen(QColor("#2a2a2e")))
        p.setFont(QFont("Monospace", 9))
        p.drawText(QRectF(0, h-16, w, 14),
                   Qt.AlignmentFlag.AlignCenter,
                   "Mid-drag pan  ·  Alt+mid-drag zoom  ·  F fit")
        p.end()

    def _x_to_fraction(self, x: float) -> float:
        w = max(1, self.width())
        return max(0.0, min(1.0, x / w))

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            if event.modifiers() & Qt.KeyboardModifier.AltModifier:
                # Alt+LMB = relative scrub. Anchor at the press point and the
                # current frame; drag moves the playhead relative to there, so
                # there's no jump to the cursor's absolute position.
                self._scrubbing = True
                self._scrub_press_x = event.position().x()
                self.scrubBegan.emit()
            # Plain LMB: intentionally unused (reserved for a future tool).
            return
        if event.button() == Qt.MouseButton.MiddleButton:
            self._alt_held = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
            if self._alt_held:
                self._zoom_drag_start = event.position().y()
                self._zoom_drag_zoom  = self._zoom
            else:
                self._pan_start        = event.position()
                self._pan_offset_start = QPointF(self._offset)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._scrubbing and (event.buttons() & Qt.MouseButton.LeftButton):
            dx = event.position().x() - self._scrub_press_x
            self.scrubRequested.emit(dx, float(max(1, self.width())))
            return
        if event.buttons() & Qt.MouseButton.MiddleButton:
            if self._zoom_drag_start is not None:
                dy = self._zoom_drag_start - event.position().y()
                factor = math.exp(dy * 0.005)
                self._zoom = max(0.05, min(32.0, self._zoom_drag_zoom * factor))
                self.update()
            elif self._pan_start is not None:
                delta = event.position() - self._pan_start
                self._offset = QPointF(
                    self._pan_offset_start.x() + delta.x(),
                    self._pan_offset_start.y() + delta.y()
                )
                self.update()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self._scrubbing = False
            return
        if event.button() == Qt.MouseButton.MiddleButton:
            self._pan_start        = None
            self._pan_offset_start = None
            self._zoom_drag_start  = None

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        self.fit_to_view()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_F:
            self.fit_to_view()
        else:
            super().keyPressEvent(event)

    def wheelEvent(self, event: QWheelEvent):
        delta  = event.angleDelta().y()
        factor = 1.15 if delta > 0 else 1/1.15
        # Zoom toward cursor position
        pos = event.position()
        cx  = self.width()  / 2 + self._offset.x()
        cy  = self.height() / 2 + self._offset.y()
        dx  = pos.x() - cx
        dy  = pos.y() - cy
        old_zoom  = self._zoom
        self._zoom = max(0.05, min(32.0, self._zoom * factor))
        scale_ratio = self._zoom / old_zoom
        self._offset = QPointF(
            self._offset.x() + dx * (1 - scale_ratio),
            self._offset.y() + dy * (1 - scale_ratio)
        )
        self.update()


# ── Scrubber ──────────────────────────────────────────────────────────────────

class Scrubber(QWidget):
    valueChanged    = pyqtSignal(int)
    inPointChanged  = pyqtSignal(int)
    outPointChanged = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._min = 1001; self._max = 1072
        self._value = 1001; self._in_point = 1001; self._out_point = 1072
        self._cached: Set[int] = set()
        self._keyframes: list = []
        self._dragging = None
        self.setFixedHeight(18)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)

    def setRange(self, mn, mx, reset_points: bool = False):
        # Detect first-ever range set (points still at their construction defaults
        # AND no real range established yet) so initial setup gets sensible full-range
        # in/out, but subsequent curve edits preserve the user's in/out points.
        first_set = not getattr(self, "_range_set", False)
        self._min = mn; self._max = mx
        self._range_set = True

        if reset_points or first_set:
            self._in_point = mn
            self._out_point = mx
        else:
            # Preserve user's in/out across range changes; just clamp into new bounds
            # and keep in < out by at least one frame.
            self._in_point  = max(mn, min(mx - 1, self._in_point))
            self._out_point = max(self._in_point + 1, min(mx, self._out_point))

        # Emit signals so external listeners (e.g. in/out point fields) stay in sync
        self.inPointChanged.emit(self._in_point)
        self.outPointChanged.emit(self._out_point)
        self.update()

    def setValue(self, v):
        v = max(self._min, min(self._max, v))
        if v != self._value:
            self._value = v
            self.valueChanged.emit(v)
            self.update()

    def value(self): return self._value
    def setInPoint(self, v):
        self._in_point = max(self._min, min(self._out_point-1, v))
        self.inPointChanged.emit(self._in_point); self.update()
    def setOutPoint(self, v):
        self._out_point = max(self._in_point+1, min(self._max, v))
        self.outPointChanged.emit(self._out_point); self.update()
    def inPoint(self):  return self._in_point
    def outPoint(self): return self._out_point

    def set_cached(self, frames: Set[int]):
        self._cached = frames; self.update()

    def set_keyframes(self, out_frames: list):
        self._keyframes = list(out_frames)
        self.update()

    def _track_rect(self): return QRectF(8, 5, self.width()-16, 8)
    def _x_for(self, val):
        r = self._track_rect()
        if self._max == self._min: return r.left()
        return r.left() + (val-self._min)/(self._max-self._min)*r.width()
    def _val_for(self, x):
        r = self._track_rect()
        if r.width() == 0: return self._min
        return round(self._min + ((x-r.left())/r.width())*(self._max-self._min))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self._track_rect()
        total = max(1, self._max-self._min)
        p.fillRect(r, QColor("#111115"))
        if self._cached:
            sorted_frames = sorted(f for f in self._cached if self._min <= f <= self._max)
            if sorted_frames:
                runs = []
                rs = re = sorted_frames[0]
                for f in sorted_frames[1:]:
                    if f == re+1: re = f
                    else: runs.append((rs,re)); rs=re=f
                runs.append((rs,re))
                p.setPen(Qt.PenStyle.NoPen)
                for rs2, re2 in runs:
                    x1 = self._x_for(rs2)
                    x2 = self._x_for(re2) + (r.width()/total)
                    p.fillRect(QRectF(x1, r.top(), x2-x1, r.height()), QColor(100,100,108,80))
        ix = self._x_for(self._in_point)
        ox = self._x_for(self._out_point)
        p.fillRect(QRectF(ix, r.top(), ox-ix, r.height()), QColor(74,158,255,30))
        p.setPen(QPen(QColor("#4a9eff"), 1.5))
        p.drawLine(QPointF(ix, r.top()-2), QPointF(ix, r.bottom()+2))
        p.drawLine(QPointF(ix, r.top()-2), QPointF(ix+5, r.top()-2))
        p.setPen(QPen(QColor("#4a9eff"), 1.5))
        p.drawLine(QPointF(ox, r.top()-2), QPointF(ox, r.bottom()+2))
        p.drawLine(QPointF(ox, r.top()-2), QPointF(ox-5, r.top()-2))
        # Keyframe tick marks — small amber verticals on the track.
        # Easier to read at a glance than diamonds: the tick's X is unambiguous,
        # and the minimal weight doesn't compete with playhead or in/out cursors.
        if self._keyframes:
            tick_color = QColor("#ffb830")
            p.setPen(QPen(tick_color, 1.5))
            # Tick extends slightly above and below the track rectangle
            top    = r.top() + 1
            bottom = r.bottom() - 1
            for kf in self._keyframes:
                if self._min <= kf <= self._max:
                    kx = self._x_for(kf)
                    p.drawLine(QPointF(kx, top), QPointF(kx, bottom))

        vx = self._x_for(self._value)
        p.fillRect(QRectF(vx-1, r.top()-2, 2, r.height()+4), QColor("#f5a623"))
        p.end()

    def mousePressEvent(self, e):
        x  = e.position().x()
        ix = self._x_for(self._in_point)
        ox = self._x_for(self._out_point)
        if abs(x-ix) < 6:   self._dragging = 'in'
        elif abs(x-ox) < 6: self._dragging = 'out'
        else:
            self._dragging = 'value'
            self.setValue(self._val_for(x))

    def mouseMoveEvent(self, e):
        if not self._dragging: return
        v = max(self._min, min(self._max, self._val_for(e.position().x())))
        if self._dragging == 'value':     self.setValue(v)
        elif self._dragging == 'in':      self.setInPoint(v)
        elif self._dragging == 'out':     self.setOutPoint(v)

    def mouseReleaseEvent(self, e): self._dragging = None


# ── Button helpers ────────────────────────────────────────────────────────────

BTN_STYLE = (
    "QPushButton{background:#1e1e21;border:1px solid #2a2a2e;"
    "color:#8a8a92;font-family:monospace;font-size:10pt;padding:0;}"
    "QPushButton:hover{background:#27272b;color:#c8c8cc;border-color:#3a3a40;}"
    "QPushButton:pressed{background:#161618;border-color:#5a5a62;}"
)

def _btn(text, tooltip, width=24):
    b = QPushButton(text)
    b.setFixedWidth(width); b.setFixedHeight(22)
    b.setToolTip(tooltip); b.setStyleSheet(BTN_STYLE)
    return b

def _sep():
    s = QFrame(); s.setFrameShape(QFrame.Shape.VLine)
    s.setFixedHeight(16); s.setStyleSheet("color:#2a2a2e; margin:0 2px;")
    return s


# ── Range zoom button ────────────────────────────────────────────────────────

class RangeZoomButton(QWidget):
    """Toggle button: zoom scrubber to in/out range vs full range."""
    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._active = False
        self.setFixedSize(28, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(
            "Range Zoom — click to toggle.\n"
            "When active, the scrubber, curve editor, and dope sheet all\n"
            "zoom to show only the frames between the in and out points.\n"
            "Toggle off to see the full sequence again.")

    def set_active(self, v: bool):
        self._active = v
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        # Background
        bg = QColor("#1a2535") if self._active else QColor("#1e1e21")
        bc = QColor("#4a9eff") if self._active else QColor("#2a2a2e")
        p.fillRect(0, 0, w, h, bg)
        p.setPen(QPen(bc, 1.0))
        p.drawRect(0, 0, w-1, h-1)

        # Icon color
        c = QColor("#4a9eff") if self._active else QColor("#6a6a72")
        p.setPen(QPen(c, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))

        cx = w / 2
        cy = h / 2
        bar_h = 8
        arr = 3  # arrow size

        # Left bar
        p.drawLine(QPointF(4, cy - bar_h/2), QPointF(4, cy + bar_h/2))
        # Right bar
        p.drawLine(QPointF(w-5, cy - bar_h/2), QPointF(w-5, cy + bar_h/2))

        if self._active:
            # Arrows pointing outward (zoom out state)
            # Left arrow pointing left
            p.drawLine(QPointF(cx-1, cy), QPointF(7, cy))
            p.drawLine(QPointF(7, cy), QPointF(7+arr, cy-arr))
            p.drawLine(QPointF(7, cy), QPointF(7+arr, cy+arr))
            # Right arrow pointing right
            p.drawLine(QPointF(cx+1, cy), QPointF(w-8, cy))
            p.drawLine(QPointF(w-8, cy), QPointF(w-8-arr, cy-arr))
            p.drawLine(QPointF(w-8, cy), QPointF(w-8-arr, cy+arr))
        else:
            # Arrows pointing inward (zoom in state)
            # Left arrow pointing right
            p.drawLine(QPointF(7, cy), QPointF(cx-1, cy))
            p.drawLine(QPointF(cx-1, cy), QPointF(cx-1-arr, cy-arr))
            p.drawLine(QPointF(cx-1, cy), QPointF(cx-1-arr, cy+arr))
            # Right arrow pointing left
            p.drawLine(QPointF(w-8, cy), QPointF(cx+1, cy))
            p.drawLine(QPointF(cx+1, cy), QPointF(cx+1+arr, cy-arr))
            p.drawLine(QPointF(cx+1, cy), QPointF(cx+1+arr, cy+arr))

        p.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._active = not self._active
            self.update()
            self.clicked.emit()


# ── Click-to-reveal value readout (exposure / gamma) ─────────────────────────

class ValuePopup(QFrame):
    """Frameless floating popup with a slider, a numeric field, and a reset.
    Appears above its anchor readout; closes on click-away or Esc."""
    changed = pyqtSignal(float)        # emits the resolved value

    def __init__(self, title, slider_range, to_value, from_value,
                 default_value, fmt="{:.2f}", parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self._to_value   = to_value      # slider int -> real value
        self._from_value = from_value    # real value -> slider int
        self._default    = default_value
        self._fmt        = fmt
        # Translucent panel: only the background is ~50% see-through; the slider,
        # field and text stay fully opaque and readable.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(
            "ValuePopup{background:rgba(0,0,0,128);border:1px solid #3a3a40;"
            "border-radius:5px;}")
        lay = QVBoxLayout(self); lay.setContentsMargins(8, 6, 8, 6); lay.setSpacing(0)
        row = QHBoxLayout(); row.setSpacing(8)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(*slider_range)
        self.slider.setFixedWidth(170)
        reset = QPushButton("reset")
        reset.setFixedHeight(20)
        reset.setStyleSheet(
            "QPushButton{background:#1e1e21;border:1px solid #3a3a40;color:#c8c8cc;"
            "font-family:monospace;font-size:8pt;padding:0 6px;border-radius:3px;}"
            "QPushButton:hover{border-color:#4a9eff;color:#e8e8ec;}")
        reset.setToolTip(f"Reset {title} to default")
        row.addWidget(self.slider); row.addWidget(reset)
        lay.addLayout(row)

        self.slider.valueChanged.connect(self._on_slider)
        reset.clicked.connect(lambda: self.set_value(self._default))

    def set_value(self, v):
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(self._from_value(v))))
        self.slider.blockSignals(False)
        self.changed.emit(v)

    def _on_slider(self, s):
        self.changed.emit(self._to_value(s))

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(e)


class ValueReadout(QLabel):
    """Compact 'label: value' readout overlaid on the canvas. Click to pop up
    its slider. Repaints its value live."""
    clicked = pyqtSignal()

    def __init__(self, title, value_fmt="{:.2f}", parent=None):
        super().__init__(parent)
        self._title = title
        self._fmt   = value_fmt
        self.setStyleSheet(
            "color:#ffffff;font-family:monospace;font-size:10pt;"
            "background:transparent;")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"Click to adjust {title}")
        self.set_value(0.0)

    def set_value(self, v):
        self.setText(f"{self._title}: {self._fmt.format(v)}")
        self.adjustSize()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        else:
            super().mousePressEvent(e)


# ── Main sequence viewer ──────────────────────────────────────────────────────

class SequenceViewer(QWidget):
    frameChanged                    = pyqtSignal(int)
    cacheUpdated                    = pyqtSignal(int, int)   # (cached_count, total)
    # User picked a display mode from this viewer's dropdown. MainWindow listens
    # and routes through its own set_display_mode (single source of truth).
    displayModeUserChanged          = pyqtSignal(str)
    # Fired after each _probe_timecode_for_sequence run with the new boolean.
    # MainWindow listens and forwards via tcMetadataAvailableChanged so the curve
    # editor's dropdown can match this viewer's enable/disable state in lockstep.
    tcMetadataAvailableUserChanged  = pyqtSignal(bool)
    # Fired when the user clicks a disabled item in the display-mode dropdown.
    # Qt swallows clicks on disabled items (no currentIndexChanged), so we hook
    # the popup view's MouseButtonPress to surface the click as feedback.
    # Payload = the clicked item's text (e.g. "TC (metadata)").
    disabledModeClicked             = pyqtSignal(str)

    LOOP_NONE = 0; LOOP_LOOP = 1; LOOP_PINGPONG = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mode = "SOURCE"
        self._playing = False; self._play_dir = 1
        self._loop_mode = self.LOOP_LOOP; self._pingpong_dir = 1
        self._src_dir=""; self._src_prefix=""; self._src_padding=4
        self._src_ext=".exr"; self._src_start=1001; self._src_end=1072
        self._out_dir=""; self._out_prefix=""; self._out_padding=4
        self._out_ext=".exr"; self._out_start=1001; self._out_end=1001
        self._frame_map = []; self._keyframe_out_frames = []
        self._current_frame = 1001; self._exposure = 0.0; self._gamma = 1.0
        self._range_zoomed = False
        self._scrub_anchor_frame = 1001
        self._tc_start_str: str | None = None
        self._tc_fps: float | None = None
        self._tc_has_meta = False
        self._tc_is_drop  = False
        self._display_mode = "Frame"   # "Frame" | "TC (metadata)" | "TC (fps)"
        self._cache = FrameCache()
        self._cache._on_progress = self._on_cache_progress
        self._build_ui()
        self._timer = QTimer(self)
        self._timer.setInterval(41)
        self._timer.timeout.connect(self._on_tick)
        self._cache_ui_timer = QTimer(self)
        self._cache_ui_timer.setInterval(200)
        self._cache_ui_timer.timeout.connect(self._refresh_cache_indicator)
        self._cache_ui_timer.start()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def _build_ui(self):
        # Match settings_panel tooltip styling for consistency across the app
        self.setStyleSheet(
            "QToolTip{background:#1e1e22;color:#ffffff;"
            "border:1px solid #4a9eff;font-family:monospace;font-size:9pt;}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0); layout.setSpacing(0)
        self.canvas = ViewerCanvas()
        self.canvas.scrubBegan.connect(self._on_scrub_began)
        self.canvas.scrubRequested.connect(self._on_canvas_scrub)
        layout.addWidget(self.canvas, stretch=1)

        transport = QWidget()
        transport.setFixedHeight(56)
        transport.setStyleSheet("background:#161618; border-top:1px solid #2a2a2e;")
        tl = QVBoxLayout(transport)
        tl.setContentsMargins(8,4,8,4); tl.setSpacing(3)

        # Scrubber row
        sr = QHBoxLayout(); sr.setSpacing(4)
        _SCRUB_LBL_STYLE = "color:#a0a0aa;font-family:monospace;font-size:9pt;"
        self._lbl_start = QLabel("1001")
        self._lbl_end   = QLabel("1072")
        # Identical alignment / width / size policy so the two labels read as a
        # matched pair (AlignCenter = horizontal + vertical centre on both).
        for _lbl in (self._lbl_start, self._lbl_end):
            _lbl.setFixedWidth(100)
            _lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            _lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
            _lbl.setStyleSheet(_SCRUB_LBL_STYLE)
        self.scrubber = Scrubber()
        self.scrubber.valueChanged.connect(self._on_scrub)
        sr.addWidget(self._lbl_start)
        sr.addWidget(self.scrubber)
        sr.addWidget(self._lbl_end)
        tl.addLayout(sr)

        # Controls row
        cr = QHBoxLayout(); cr.setSpacing(3)

        # LEFT: display-mode combo, fixed 138px = SOURCE(62)+Timewarp(76) on the right
        self.display_mode_combo = QComboBox()
        self.display_mode_combo.addItems(["Frame", "TC (metadata)", "TC (fps)"])
        self.display_mode_combo.setFixedWidth(138)
        self.display_mode_combo.setFixedHeight(22)
        self.display_mode_combo.setToolTip(
            "Frame-number display mode for the scrubber labels and the\n"
            "canvas burn-in.\n"
            "  Frame          plain frame numbers\n"
            "  TC (metadata)  timecode from the file's embedded SMPTE TC\n"
            "                 (disabled when the sequence has no TC)\n"
            "  TC (fps)       timecode computed from frame count and fps")
        self.display_mode_combo.currentIndexChanged.connect(self._on_display_mode_changed)
        # Hook the popup view so clicks on disabled items surface as feedback
        # (Qt swallows those clicks silently — see disabledModeClicked).
        self.display_mode_combo.view().viewport().installEventFilter(self)
        cr.addWidget(self.display_mode_combo)

        # Source / Timewarp toggle — created here, added to the layout at the far
        # right (after the transport block) so the centre group stays centred.
        # NOTE: the button text is display-only; the internal mode value stays
        # "RETIMED" everywhere in the logic (see _set_retimed_mode / self._mode).
        self.btn_source  = QPushButton("SOURCE")
        self.btn_retimed = QPushButton("TIMEWARP")
        self.btn_source.setToolTip(
            "View the source (input) sequence at the playhead.\n"
            "Shows the original frames before timewarp is applied.\n"
            "Useful for checking source content and setting in/out points.")
        self.btn_retimed.setToolTip(
            "View the retimed (output) sequence at the playhead.\n"
            "Shows what the rendered frames will look like based on the current curve.\n"
            "Frames are interpolated on-the-fly for preview.")
        self.btn_source.setFixedHeight(22);  self.btn_source.setFixedWidth(62)
        self.btn_retimed.setFixedHeight(22); self.btn_retimed.setFixedWidth(76)
        self.btn_source.clicked.connect(self._set_source_mode)
        self.btn_retimed.clicked.connect(self._set_retimed_mode)
        self._update_mode_buttons()

        # Left-cluster fps controls (label + value combo)
        fps_lbl = QLabel("fps")
        fps_lbl.setStyleSheet("color:#4a4a52;font-size:9pt;")
        # Fixed width so the centring math below matches the rendered width
        # (a bare label's sizeHint isn't reliable until fonts are realized).
        fps_lbl.setFixedWidth(24)
        # Editable combo — preset list (Nuke-style) plus free-typing for any value
        self.fps_combo = QComboBox()
        self.fps_combo.setEditable(True)
        self.fps_combo.addItems([
            "1", "2", "4", "6", "8", "12", "15",
            "23.976", "24", "25", "29.97", "30",
            "48", "50", "59.94", "60",
        ])
        self.fps_combo.setCurrentText("24")
        self.fps_combo.setFixedWidth(72); self.fps_combo.setFixedHeight(22)
        self.fps_combo.setToolTip(
            "Playback frame rate for the viewer.\n"
            "Pick a preset or type any value (e.g. 0.5 for very slow inspection).\n"
            "Affects only in-app preview \u2014 does not change render output.")
        self.fps_combo.setStyleSheet(
            "QComboBox{background:#1e1e21;border:1px solid #2a2a2e;color:#8a8a92;"
            "font-family:monospace;font-size:9pt;padding:0 4px;}"
            "QComboBox:editable{background:#1e1e21;}")
        # Commit on Enter or focus-out; ignore intermediate typing
        from PyQt6.QtGui import QDoubleValidator
        validator = QDoubleValidator(0.1, 240.0, 3)
        validator.setNotation(QDoubleValidator.Notation.StandardNotation)
        self.fps_combo.setValidator(validator)
        self.fps_combo.lineEdit().editingFinished.connect(self._on_fps_combo_changed)
        self.fps_combo.currentIndexChanged.connect(self._on_fps_combo_changed)
        # Keep a parallel attribute so legacy code that reads .fps_spin still works
        self.fps_spin = self.fps_combo
        cr.addSpacing(12)   # breathing room between the dropdown and the fps cluster
        cr.addWidget(fps_lbl); cr.addWidget(self.fps_combo)

        # fps now lives in the LEFT cluster (next to the dropdown). The stretch
        # that centres the transport block goes here, after the fps controls.
        # Centring requires the left-anchored group (dropdown + fps) and the
        # right-anchored group (SOURCE+TIMEWARP) to be equal width, so compute
        # the right-side pad that mirrors the fps cluster's overflow.
        _sp = cr.spacing()
        # Left cluster widths: dropdown 138, +12 breathing space, fps_lbl 24,
        # fps_combo 72, plus the inter-item gaps. (Loop button is centre now.)
        _left_w = (138 + _sp + 12 + 24 + _sp + 72)
        # SOURCE(62) + gap + TIMEWARP(76). No extra gap subtracted: Qt adds a 3px
        # gap after a widget that precedes a spacer (fps_combo→stretch on the left)
        # but none after a spacer (stretch→pad→SOURCE on the right), so the left
        # is one gap heavier — leaving the pad 3px larger compensates and centres
        # the transport block.
        _right_pad = max(0, _left_w - (62 + _sp + 76))
        cr.addStretch(1)

        # Transport — confirmed order:
        # [  |◀  ⁂◀  ◀|  ◀  [frame]  ▶  |▶  ▶⁂  ▶|  ]
        self.btn_in_point  = _btn(
            "[", "Set the IN point at the current frame.\n"
                 "Shortcut: [\n"
                 "Right-click: reset to sequence start.", 22)
        self.btn_in_point.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.btn_in_point.customContextMenuRequested.connect(
            lambda _: self.scrubber.setInPoint(self._src_start))
        self.btn_first     = _btn(
            "|◀", "Go to the first frame of the sequence.\n"
                  "Shortcut: Home", 28)
        self.btn_prev_kf   = _btn(
            "•◀", "Go to the previous keyframe on the curve.\n"
                  "Shortcut: Alt+\u2190 (Alt+Left)", 28)
        self.btn_prev      = _btn(
            "◀|", "Step back one frame.\n"
                  "Shortcut: \u2190 (Left) or J (hold for reverse play)", 26)
        self.btn_play_rev  = _btn(
            "◀", "Play backwards / stop.\n"
                 "Shortcut: J (tap to play, tap again to stop)", 26)

        self.frame_input = QLineEdit("1001")
        self.frame_input.setFixedWidth(100); self.frame_input.setFixedHeight(22)
        self.frame_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.frame_input.setToolTip(
            "Current playhead frame.\n"
            "Type a frame number and press Enter to jump to it.\n"
            "Highlighted in amber when the playhead is at a keyframe.")
        self.frame_input.setStyleSheet(
            "background:#1e1e21;border:1px solid #3a3a40;color:#f5a623;"
            "font-family:monospace;font-size:10pt;padding:0 4px;")
        self.frame_input.returnPressed.connect(self._on_frame_input)

        self.btn_play_fwd  = _btn(
            "▶", "Play forwards / stop.\n"
                 "Shortcut: L or Spacebar (tap to play, tap again to stop)\n"
                 "Hold L repeatedly for faster playback.", 26)
        self.btn_next      = _btn(
            "|▶", "Step forward one frame.\n"
                  "Shortcut: \u2192 (Right) or L", 26)
        self.btn_next_kf   = _btn(
            "▶•", "Go to the next keyframe on the curve.\n"
                  "Shortcut: Alt+\u2192 (Alt+Right)", 28)
        self.btn_last      = _btn(
            "▶|", "Go to the last frame of the sequence.\n"
                  "Shortcut: End", 28)
        self.btn_out_point = _btn(
            "]", "Set the OUT point at the current frame.\n"
                 "Shortcut: ]\n"
                 "Right-click: reset to sequence end.", 22)
        self.btn_out_point.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.btn_out_point.customContextMenuRequested.connect(
            lambda _: self.scrubber.setOutPoint(self._src_end))

        # In/Out point editable fields — sit next to their bracket buttons
        IO_FIELD_STYLE = (
            "background:#1e1e21;border:1px solid #3a3a40;color:#6aa9ff;"
            "font-family:monospace;font-size:9pt;padding:0 4px;")
        self.in_point_input = QLineEdit()
        self.in_point_input.setFixedWidth(100); self.in_point_input.setFixedHeight(22)
        self.in_point_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.in_point_input.setPlaceholderText("--")
        self.in_point_input.setStyleSheet(IO_FIELD_STYLE)
        self.in_point_input.setToolTip(
            "In point frame.\n"
            "Type a frame number and press Enter to set the in point.\n"
            "Click [ button to set to current frame; right-click [ to reset.")
        self.in_point_input.returnPressed.connect(self._on_in_point_input)

        self.out_point_input = QLineEdit()
        self.out_point_input.setFixedWidth(100); self.out_point_input.setFixedHeight(22)
        self.out_point_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.out_point_input.setPlaceholderText("--")
        self.out_point_input.setStyleSheet(IO_FIELD_STYLE)
        self.out_point_input.setToolTip(
            "Out point frame.\n"
            "Type a frame number and press Enter to set the out point.\n"
            "Click ] button to set to current frame; right-click ] to reset.")
        self.out_point_input.returnPressed.connect(self._on_out_point_input)

        self.btn_range_zoom = RangeZoomButton()
        self.btn_range_zoom.clicked.connect(self._toggle_range_zoom)

        # Loop / play-mode button — sized to match btn_range_zoom (28x22) so the
        # two form a consistent pair bookending the transport; larger glyph.
        self.btn_loop = _btn("↺",
            "Loop mode (click to cycle).\n"
            "—  No loop: stop at the end.\n"
            "↺  Loop: restart from in point at end.\n"
            "⇄  Ping-pong: reverse direction at each end.", 28)
        self.btn_loop.setStyleSheet(BTN_STYLE.replace("font-size:10pt", "font-size:13pt"))
        self.btn_loop.clicked.connect(self._cycle_loop)
        self._update_loop_btn()
        cr.addWidget(self.btn_loop)
        cr.addWidget(_sep())

        # Order: in_field [ |◀ •◀ ◀| ◀ [frame] ▶ |▶ ▶• ▶| ] out_field
        for w in [self.in_point_input,
                  self.btn_in_point, self.btn_first, self.btn_prev_kf,
                  self.btn_prev, self.btn_play_rev, self.frame_input,
                  self.btn_play_fwd, self.btn_next, self.btn_next_kf,
                  self.btn_last, self.btn_out_point,
                  self.out_point_input]:
            cr.addWidget(w)

        cr.addWidget(_sep())
        cr.addWidget(self.btn_range_zoom)

        # Right side: stretch, then a pad mirroring the left fps-cluster overflow
        # (_right_pad) so the left- and right-anchored groups are equal width and
        # the transport block stays centred, then SOURCE/TIMEWARP pinned far right.
        cr.addStretch(1)
        cr.addSpacing(_right_pad)
        cr.addWidget(self.btn_source)
        cr.addWidget(self.btn_retimed)

        # ── Exposure / Gamma: click-to-reveal readouts overlaid on the canvas,
        #    bottom-left, just above the burn-in info. Each opens a floating
        #    slider+field popup. ────────────────────────────────────────────────
        self.exp_readout = ValueReadout("exposure", parent=self.canvas)
        self.gam_readout = ValueReadout("gamma",    parent=self.canvas)
        self.exp_readout.set_value(0.0)
        self.gam_readout.set_value(1.0)

        self.exp_popup = ValuePopup(
            "exposure (stops)", (-400, 400),
            to_value=lambda s: s / 100.0,
            from_value=lambda v: v * 100.0,
            default_value=0.0)
        self.gam_popup = ValuePopup(
            "gamma", (-100, 100),
            to_value=lambda s: 2.0 ** (s / 50.0),
            from_value=lambda v: 50.0 * math.log2(max(0.01, v)),
            default_value=1.0)

        # Slider aliases (the popups own the sliders).
        self.exp_slider = self.exp_popup.slider
        self.gam_slider = self.gam_popup.slider

        self.exp_popup.changed.connect(self._on_exp_value)
        self.gam_popup.changed.connect(self._on_gam_value)
        self.exp_readout.clicked.connect(
            lambda: self._open_value_popup(self.exp_popup, self.exp_readout))
        self.gam_readout.clicked.connect(
            lambda: self._open_value_popup(self.gam_popup, self.gam_readout))

        self.canvas.installEventFilter(self)
        self._position_readouts()


        # Initialize in/out point fields from the scrubber and keep them in sync
        self.scrubber.inPointChanged.connect(self._on_in_point_changed_external)
        self.scrubber.outPointChanged.connect(self._on_out_point_changed_external)
        self._on_in_point_changed_external(self.scrubber.inPoint())
        self._on_out_point_changed_external(self.scrubber.outPoint())

        self.btn_in_point.clicked.connect(lambda: self.scrubber.setInPoint(self._current_frame))
        self.btn_first.clicked.connect(self._go_first)
        self.btn_prev_kf.clicked.connect(self._prev_keyframe)
        self.btn_play_rev.clicked.connect(self._play_reverse)
        self.btn_prev.clicked.connect(lambda: self._step(-1))
        self.btn_next.clicked.connect(lambda: self._step(1))
        self.btn_play_fwd.clicked.connect(self._play_forward)
        self.btn_next_kf.clicked.connect(self._next_keyframe)
        self.btn_last.clicked.connect(self._go_last)
        self.btn_out_point.clicked.connect(lambda: self.scrubber.setOutPoint(self._current_frame))

        tl.addLayout(cr)
        layout.addWidget(transport)

    # ── Mode buttons ──────────────────────────────────────────────────────────

    _ACTIVE_SRC = (
        "QPushButton{background:#1a2a3a;border:1px solid #4a9eff;"
        "border-right:none;color:#4a9eff;font-family:monospace;"
        "font-size:9pt;padding:0 6px;border-radius:0;}"
    )
    _INACTIVE_SRC = (
        "QPushButton{background:#161618;border:1px solid #2a2a2e;"
        "border-right:none;color:#3a3a42;font-family:monospace;"
        "font-size:9pt;padding:0 6px;border-radius:0;}"
        "QPushButton:hover{color:#6a6a72;}"
    )
    _ACTIVE_RET = (
        "QPushButton{background:#1a3a2a;border:1px solid #3ecf6e;"
        "color:#3ecf6e;font-family:monospace;"
        "font-size:9pt;padding:0 6px;border-radius:0;}"
    )
    _INACTIVE_RET = (
        "QPushButton{background:#161618;border:1px solid #2a2a2e;"
        "color:#3a3a42;font-family:monospace;"
        "font-size:9pt;padding:0 6px;border-radius:0;}"
        "QPushButton:hover{color:#6a6a72;}"
    )

    def _update_mode_buttons(self):
        if self._mode == "SOURCE":
            self.btn_source.setStyleSheet(self._ACTIVE_SRC)
            self.btn_retimed.setStyleSheet(self._INACTIVE_RET)
        else:
            self.btn_source.setStyleSheet(self._INACTIVE_SRC)
            self.btn_retimed.setStyleSheet(self._ACTIVE_RET)

    def _set_source_mode(self):
        if self._mode == "SOURCE": return
        self._mode = "SOURCE"
        self._update_mode_buttons()
        if self._range_zoomed:
            # Keep the zoomed in/out window across the mode switch rather than
            # snapping back to the full source range.
            lo, hi = self.scrubber._min, self.scrubber._max
            f = max(lo, min(hi, self._current_frame))
            self.scrubber.setRange(lo, hi)
            self._lbl_start.setText(str(lo)); self._lbl_end.setText(str(hi))
        else:
            f = max(self._src_start, min(self._src_end, self._current_frame))
            self.scrubber.setRange(self._src_start, self._src_end)
            self._lbl_start.setText(str(self._src_start))
            self._lbl_end.setText(str(self._src_end))
        self.scrubber.blockSignals(True); self.scrubber.setValue(f); self.scrubber.blockSignals(False)
        self._current_frame = f; self.frame_input.setText(self._fmt_field(f)); self._show_frame(f)

    def _set_retimed_mode(self):
        if self._mode == "RETIMED": return
        self._mode = "RETIMED"
        self._update_mode_buttons()
        if self._range_zoomed:
            lo, hi = self.scrubber._min, self.scrubber._max
            f = max(lo, min(hi, self._current_frame))
            self.scrubber.setRange(lo, hi)
            self._lbl_start.setText(str(lo)); self._lbl_end.setText(str(hi))
        else:
            f = max(self._out_start, min(self._out_end, self._current_frame))
            self.scrubber.setRange(self._out_start, self._out_end)
            self._lbl_start.setText(str(self._out_start))
            self._lbl_end.setText(str(self._out_end))
        self.scrubber.blockSignals(True); self.scrubber.setValue(f); self.scrubber.blockSignals(False)
        self._current_frame = f; self.frame_input.setText(self._fmt_field(f)); self._show_frame(f)

    # ── Loop ──────────────────────────────────────────────────────────────────

    def _update_loop_btn(self):
        icons = {self.LOOP_NONE:"—", self.LOOP_LOOP:"↺", self.LOOP_PINGPONG:"⇄"}
        tips  = {
            self.LOOP_NONE:
                "No loop — currently active.\n"
                "Playback stops at the end of the sequence.\n"
                "Click to switch to loop mode.",
            self.LOOP_LOOP:
                "Loop — currently active.\n"
                "Playback restarts from the in point when it reaches the end.\n"
                "Click to switch to ping-pong mode.",
            self.LOOP_PINGPONG:
                "Ping-pong — currently active.\n"
                "Playback reverses direction at each end.\n"
                "Click to switch to no-loop mode.",
        }
        if hasattr(self, 'btn_loop'):
            self.btn_loop.setText(icons[self._loop_mode])
            self.btn_loop.setToolTip(tips[self._loop_mode])

    def _cycle_loop(self):
        self._loop_mode = (self._loop_mode + 1) % 3
        self._update_loop_btn()

    # ── Cache ─────────────────────────────────────────────────────────────────

    def _on_cache_progress(self): pass

    def _refresh_cache_indicator(self):
        cached = self._cache.cached_frames()
        total  = max(1, self._src_end - self._src_start + 1)
        self.scrubber.set_cached(cached)
        n = len(cached)
        self.cacheUpdated.emit(n, total)
        # If viewer is blank but we now have the current frame in cache, show it
        if self.canvas._pixmap is None and self._current_frame in cached:
            self._show_frame(self._current_frame)

    # ── Timecode / display helpers ─────────────────────────────────────────────

    def _probe_timecode_for_sequence(self):
        """Read TC metadata from the first source frame; cache it in _tc_* attrs.

        Also enables/disables the "TC (metadata)" combo item: it stays disabled
        when the sequence has no embedded timecode. If the current mode is
        "TC (metadata)" but the new sequence has none, revert to "Frame".
        """
        self._tc_start_str = None
        self._tc_fps       = None
        self._tc_has_meta  = False
        self._tc_is_drop   = False
        if self._src_dir:
            first_path = os.path.join(
                self._src_dir,
                f"{self._src_prefix}{self._src_start:0{self._src_padding}d}{self._src_ext}")
            tc_str, fps, is_drop = _probe_timecode(first_path)
            self._tc_start_str = tc_str
            self._tc_fps       = fps
            self._tc_has_meta  = tc_str is not None
            self._tc_is_drop   = is_drop

        # Enable/disable the "TC (metadata)" item based on presence of TC.
        idx = self.display_mode_combo.findText("TC (metadata)")
        if idx >= 0:
            item = self.display_mode_combo.model().item(idx)
            if item is not None:
                item.setEnabled(self._tc_has_meta)
        # Tell MainWindow so the curve editor's combo can match in lockstep.
        self.tcMetadataAvailableUserChanged.emit(self._tc_has_meta)
        # Current mode no longer valid → fall back to Frame.
        if self._display_mode == "TC (metadata)" and not self._tc_has_meta:
            self.display_mode_combo.setCurrentText("Frame")

    def display_frame(self, frame: int, is_src: bool) -> str:
        """Format a frame number according to the active display mode.

        is_src=True  → frame is a source frame; offset from _src_start.
        is_src=False → frame is an output frame; offset from _out_start.

        TC (metadata) references the first frame's embedded TC and is keyed off
        the source position, so it always offsets from _src_start. It returns
        '--:--:--:--' when there is no TC or the sequence is drop-frame.
        """
        if self._display_mode == "Frame":
            return str(frame)
        if self._display_mode == "TC (metadata)":
            if not self._tc_has_meta or self._tc_is_drop or self._tc_start_str is None:
                return "--:--:--:--"
            fps      = self._tc_fps if self._tc_fps else 24.0
            base_abs = _tc_str_to_frames(self._tc_start_str, fps)
            return _frames_to_tc_str(base_abs + (frame - self._src_start), fps)
        if self._display_mode == "TC (fps)":
            fps = self._tc_fps if self._tc_fps else 24.0
            ref = self._src_start if is_src else self._out_start
            return _frames_to_tc_str(frame - ref, fps)
        return str(frame)

    def _update_scrubber_labels(self):
        """Refresh the start/end labels beside the scrubber for the current mode."""
        lo, hi = self.scrubber._min, self.scrubber._max
        is_src = self._mode == "SOURCE"
        self._lbl_start.setText(self.display_frame(lo, is_src))
        self._lbl_end.setText(self.display_frame(hi, is_src))

    def _fmt_field(self, frame: int) -> str:
        """Format a transport-row frame value for the active display mode.

        These are scrubber-coordinate frames (source frames in SOURCE mode,
        output frames in RETIMED mode), so is_src tracks the mode exactly like
        _update_scrubber_labels does.
        """
        return self.display_frame(frame, self._mode == "SOURCE")

    def _refresh_frame_fields(self):
        """Re-render the in-point / current / out-point fields for the current
        display mode. Skips a field while it has focus so live typing isn't
        clobbered."""
        self.frame_input.setText(self._fmt_field(self._current_frame))
        if not self.in_point_input.hasFocus():
            self.in_point_input.setText(self._fmt_field(self.scrubber.inPoint()))
        if not self.out_point_input.hasFocus():
            self.out_point_input.setText(self._fmt_field(self.scrubber.outPoint()))

    def _on_display_mode_changed(self, _index=None):
        # User selected a mode in THIS viewer's dropdown. Send it up to
        # MainWindow; the round-trip via displayModeChanged will land back here
        # in apply_display_mode(), which applies the local effects.
        text = self.display_mode_combo.currentText()
        self.displayModeUserChanged.emit(text)

    def apply_display_mode(self, mode: str):
        """Apply a mode change received from MainWindow's shared signal.
        Syncs the combo text with signals blocked (no re-emit), then runs the
        existing side-effect chain so the viewer redraws."""
        if mode == self._display_mode:
            # Still ensure combo text matches (initial-sync convenience).
            if self.display_mode_combo.currentText() != mode:
                self.display_mode_combo.blockSignals(True)
                self.display_mode_combo.setCurrentText(mode)
                self.display_mode_combo.blockSignals(False)
            return
        self._display_mode = mode
        self.display_mode_combo.blockSignals(True)
        self.display_mode_combo.setCurrentText(mode)
        self.display_mode_combo.blockSignals(False)
        self._update_scrubber_labels()
        self._refresh_frame_fields()
        self._show_frame(self._current_frame)

    def _make_burn_lines(self, out_frame: int) -> list:
        """Build the canvas burn-in as a list of (left, right) column pairs:
        [(frame_l, frame_r), (meta_l, meta_r), (fps_l, fps_r)].

        SOURCE mode: right column is '' on every line (single, right-aligned col).
        RETIMED mode: left = OUTPUT value, right = '(src <SOURCE value>)', per line:
            frame   f{out}          (src f{src})
            meta TC {out_meta_TC}   (src {src_meta_TC})
            fps  TC {out_fps_TC}    (src {src_fps_TC})
        Output values offset from _out_start, source values from _src_start;
        metadata TC = base TC (from _tc_start_str) + the relevant offset.
        A ('', '') pair is an absent line (skipped by the renderer);
        drop-frame metadata renders as '--:--:--:--'.
        """
        retimed = self._mode == "RETIMED"
        if not retimed:
            src_frame = out_frame
        else:
            src_raw = next((inf for of, inf in self._frame_map if of == out_frame), None)
            if src_raw is None and self._frame_map:
                src_raw = self._frame_map[0][1]
            src_frame = int(round(src_raw)) if src_raw is not None else out_frame

        has_meta = (self._tc_has_meta and not self._tc_is_drop
                    and self._tc_start_str is not None)
        fps_meta = self._tc_fps if self._tc_fps else 24.0
        base_abs = _tc_str_to_frames(self._tc_start_str, fps_meta) if has_meta else 0

        def meta_tc(frame, ref):
            return _frames_to_tc_str(base_abs + (frame - ref), fps_meta)

        def fps_tc(frame, ref):
            return _frames_to_tc_str(frame - ref, self._tc_fps)

        # Line 1: frame number.
        if retimed:
            line1 = (f"f{out_frame}", f"(src f{src_frame})")
        else:
            line1 = (f"f{src_frame}", "")

        # Line 2: TC from embedded metadata.
        if has_meta:
            if retimed:
                line2 = (meta_tc(out_frame, self._out_start),
                         f"(src {meta_tc(src_frame, self._src_start)})")
            else:
                line2 = (meta_tc(src_frame, self._src_start), "")
        elif self._tc_has_meta and self._tc_is_drop:
            line2 = (("--:--:--:--", "(src --:--:--:--)") if retimed
                     else ("--:--:--:--", ""))
        else:
            line2 = ("", "")

        # Line 3: TC computed purely from fps metadata.
        if self._tc_fps is not None:
            if retimed:
                line3 = (fps_tc(out_frame, self._out_start),
                         f"(src {fps_tc(src_frame, self._src_start)})")
            else:
                line3 = (fps_tc(src_frame, self._src_start), "")
        else:
            line3 = ("", "")

        return [line1, line2, line3]

    # ── Public API ────────────────────────────────────────────────────────────

    def set_source_sequence(self, directory, prefix, padding, ext, start, end):
        # Only reconfigure cache if sequence actually changed
        seq_changed = (
            directory != self._src_dir or
            prefix    != self._src_prefix or
            padding   != self._src_padding or
            ext       != self._src_ext or
            start     != self._src_start or
            end       != self._src_end
        )
        self._src_dir=directory; self._src_prefix=prefix
        self._src_padding=padding; self._src_ext=ext
        self._src_start=start; self._src_end=end

        if seq_changed:
            self._cache.configure(directory, prefix, padding, ext, start, end,
                                  self._exposure, self._gamma)
            self._cache.set_playhead(start)
            self._probe_timecode_for_sequence()
            self._update_scrubber_labels()
            if self._mode == "SOURCE":
                self._current_frame = start
                self.scrubber.setRange(start, end, reset_points=True)
                self._lbl_start.setText(str(start)); self._lbl_end.setText(str(end))
                self.scrubber.blockSignals(True); self.scrubber.setValue(start); self.scrubber.blockSignals(False)
                self.frame_input.setText(self._fmt_field(start))
            # Force immediate display
            self._show_frame(self._current_frame)
            QTimer.singleShot(200, lambda: self._show_frame(self._current_frame))
            QTimer.singleShot(500, lambda: self._show_frame(self._current_frame))
        else:
            # Sequence unchanged — just refresh display without restarting cache
            self._show_frame(self._current_frame)

    def set_output_sequence(self, directory, prefix, padding, ext, start, end):
        self._out_dir=directory; self._out_prefix=prefix
        self._out_padding=padding; self._out_ext=ext
        self._out_start=start; self._out_end=end
        if self._mode == "RETIMED":
            if self._range_zoomed:
                # Range-zoomed: keep the in/out window the user zoomed to rather
                # than snapping the scrubber back to the full output range on a
                # curve edit. Only the full-range end label is updated.
                self._lbl_end.setText(str(self.scrubber._max))
            else:
                self.scrubber.setRange(start, end)
                self._lbl_end.setText(str(end))

    def set_frame_map(self, fm):
        self._frame_map = fm
        # In RETIMED mode the displayed source frame is resolved from the map,
        # so a curve edit must re-resolve the current frame immediately rather
        # than waiting for the next scrub.
        if self._mode == "RETIMED":
            self._show_frame(self._current_frame)

    def set_keyframes(self, out_frames):
        self._keyframe_out_frames = sorted(out_frames)
        self.scrubber.set_keyframes(out_frames)

    def _on_scrub_began(self):
        # Remember the frame the scrub started on; relative drag is applied
        # against this anchor.
        self._scrub_anchor_frame = self._current_frame

    def _on_canvas_scrub(self, dx: float, width: float):
        # Relative scrub: map horizontal pixel delta to a frame delta. A full
        # canvas-width drag spans the full current range, so sensitivity adapts
        # to sequence length. Applied against the anchor frame so there is no
        # jump to the cursor's absolute position.
        mn, mx = self.scrubber._min, self.scrubber._max
        span = mx - mn
        frame_delta = (dx / width) * span
        anchor = getattr(self, "_scrub_anchor_frame", self._current_frame)
        frame = int(round(anchor + frame_delta))
        frame = max(mn, min(mx, frame))
        if frame != self._current_frame:
            self.go_to_out_frame(frame)

    def go_to_out_frame(self, out_frame: int):
        self._current_frame = out_frame
        self.scrubber.blockSignals(True); self.scrubber.setValue(out_frame); self.scrubber.blockSignals(False)
        self.frame_input.setText(self._fmt_field(out_frame)); self._show_frame(out_frame)

    # ── Transport ─────────────────────────────────────────────────────────────

    def _in(self):  return self.scrubber.inPoint()
    def _out(self): return self.scrubber.outPoint()

    def _go_first(self): self._stop(); self.scrubber.setValue(self._in())
    def _go_last(self):  self._stop(); self.scrubber.setValue(self._out())

    def _step(self, d):
        self._stop()
        self.scrubber.setValue(max(self._in(), min(self._out(), self._current_frame+d)))

    def _prev_keyframe(self):
        self._stop()
        kfs = [f for f in self._keyframe_out_frames if f < self._current_frame]
        if kfs: self.scrubber.setValue(kfs[-1])

    def _next_keyframe(self):
        self._stop()
        kfs = [f for f in self._keyframe_out_frames if f > self._current_frame]
        if kfs: self.scrubber.setValue(kfs[0])

    def _toggle_range_zoom(self):
        self._range_zoomed = self.btn_range_zoom._active
        in_pt  = self.scrubber.inPoint()
        out_pt = self.scrubber.outPoint()
        if self._range_zoomed:
            # Zoom scrubber to in/out range
            self.scrubber.setRange(in_pt, out_pt)
            self._lbl_start.setText(str(in_pt))
            self._lbl_end.setText(str(out_pt))
            # Clamp current frame
            frame = max(in_pt, min(out_pt, self._current_frame))
            self.scrubber.blockSignals(True)
            self.scrubber.setValue(frame)
            self.scrubber.blockSignals(False)
        else:
            # Restore full range
            start = self._src_start if self._mode == "SOURCE" else self._out_start
            end   = self._src_end   if self._mode == "SOURCE" else self._out_end
            self.scrubber.setRange(start, end)
            self._lbl_start.setText(str(start))
            self._lbl_end.setText(str(end))
            # Restore in/out points on scrubber
            self.scrubber.setInPoint(in_pt)
            self.scrubber.setOutPoint(out_pt)

    def _play_forward(self):
        if self._playing and self._play_dir == 1:
            self._stop()
        else:
            self._play_dir=1; self._pingpong_dir=1
            self._playing=True; self._timer.start()
        self._update_play_buttons()

    def _play_reverse(self):
        if self._playing and self._play_dir == -1:
            self._stop()
        else:
            self._play_dir=-1; self._pingpong_dir=-1
            self._playing=True; self._timer.start()
        self._update_play_buttons()

    def _stop(self):
        self._playing=False; self._timer.stop()
        self._update_play_buttons()

    def _update_play_buttons(self):
        if self._playing and self._loop_mode == self.LOOP_PINGPONG:
            # Ping-pong — both show stop
            self.btn_play_fwd.setText("■")
            self.btn_play_rev.setText("■")
        elif self._playing and self._play_dir == 1:
            self.btn_play_fwd.setText("■")
            self.btn_play_rev.setText("◀")
        elif self._playing and self._play_dir == -1:
            self.btn_play_rev.setText("■")
            self.btn_play_fwd.setText("▶")
        else:
            self.btn_play_fwd.setText("▶")
            self.btn_play_rev.setText("◀")

    def _on_tick(self):
        lo, hi = self._in(), self._out()
        if self._loop_mode == self.LOOP_PINGPONG:
            nf = self._current_frame + self._pingpong_dir
            if nf > hi: self._pingpong_dir=-1; nf=hi-1
            elif nf < lo: self._pingpong_dir=1; nf=lo+1
        else:
            nf = self._current_frame + self._play_dir
            if nf > hi:
                if self._loop_mode == self.LOOP_LOOP: nf=lo
                else: self._stop(); return
            elif nf < lo:
                if self._loop_mode == self.LOOP_LOOP: nf=hi
                else: self._stop(); return
        self.scrubber.setValue(nf)

    # ── Keyboard ─────────────────────────────────────────────────────────────

    def keyPressEvent(self, e: QKeyEvent):
        alt = bool(e.modifiers() & Qt.KeyboardModifier.AltModifier)
        key = e.key()

        if key == Qt.Key.Key_J:
            self._play_reverse()
        elif key == Qt.Key.Key_K:
            self._stop()
        elif key == Qt.Key.Key_L:
            self._play_forward()
        elif key == Qt.Key.Key_Left and alt:
            self._prev_keyframe()
        elif key == Qt.Key.Key_Right and alt:
            self._next_keyframe()
        elif key == Qt.Key.Key_Left:
            self._step(-1)
        elif key == Qt.Key.Key_Right:
            self._step(1)
        elif key == Qt.Key.Key_Home:
            self._go_first()
        elif key == Qt.Key.Key_End:
            self._go_last()
        elif key == Qt.Key.Key_BracketLeft and alt:
            self.scrubber.setInPoint(self._src_start)
        elif key == Qt.Key.Key_BracketRight and alt:
            self.scrubber.setOutPoint(self._src_end)
        elif key == Qt.Key.Key_BracketLeft:
            self.scrubber.setInPoint(self._current_frame)
        elif key == Qt.Key.Key_BracketRight:
            self.scrubber.setOutPoint(self._current_frame)
        elif key == Qt.Key.Key_F:
            self.canvas.fit_to_view()
        else:
            super().keyPressEvent(e)

    # ── Scrub ─────────────────────────────────────────────────────────────────

    def _on_scrub(self, value):
        self._current_frame = value
        self.frame_input.setText(self._fmt_field(value))
        self._cache.set_playhead(value)
        self._show_frame(value)

    def _on_frame_input(self):
        # Display-only TC for now: typed entry still parses a plain frame number.
        # If the field holds a timecode string (or other non-integer), restore
        # the formatted current frame rather than navigating.
        try:
            v = max(self._in(), min(self._out(), int(self.frame_input.text())))
            self.scrubber.setValue(v)
        except ValueError:
            self.frame_input.setText(self._fmt_field(self._current_frame))

    # ── Frame display ─────────────────────────────────────────────────────────

    def _show_frame(self, out_frame):
        if self._mode == "SOURCE":
            frame = max(self._src_start, min(self._src_end, out_frame))
            img   = self._cache.get(frame)
            if img is None and self._src_dir:
                # Always try direct disk load — never leave viewer blank
                img = load_frame(
                    os.path.join(self._src_dir,
                        f"{self._src_prefix}{frame:0{self._src_padding}d}{self._src_ext}"),
                    self._exposure, self._gamma)
                # Store in cache for next time
                if img is not None:
                    with self._cache._lock:
                        self._cache._cache[frame] = img
        else:
            # RETIMED is a TIMING preview only: always show the nearest source
            # frame the curve maps this out-frame to. It never loads rendered
            # output, so the preview always responds to curve edits (and a fresh
            # render no longer "freezes" the view onto the rendered frames).
            in_f = next((inf for of, inf in self._frame_map if of == out_frame), None)
            if in_f is None and self._frame_map:
                in_f = self._frame_map[0][1]
            if in_f is not None:
                sf  = int(round(in_f))
                img = self._cache.get(sf)
                if img is None:
                    img = load_frame(
                        os.path.join(self._src_dir,
                            f"{self._src_prefix}{sf:0{self._src_padding}d}{self._src_ext}"),
                        self._exposure, self._gamma)
            else:
                img = None

        burn_lines = self._make_burn_lines(out_frame)
        self.canvas.set_frame(img, burn_lines, self._mode)
        self.frameChanged.emit(out_frame)

    def _on_fps_changed(self, v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return
        if f <= 0:
            return
        self._timer.setInterval(max(1, int(1000 / f)))

    def _on_fps_combo_changed(self, *_):
        """Handle either a preset selection or a typed value."""
        text = self.fps_combo.currentText().strip()
        try:
            f = float(text)
        except ValueError:
            return  # invalid input — keep previous interval
        if f <= 0:
            return
        self._timer.setInterval(max(1, int(1000 / f)))

    def _on_exposure_changed(self, v):
        self._exposure = v
        self._schedule_cache_reconfigure()

    def _on_gamma_changed(self, v):
        self._gamma = v
        self._schedule_cache_reconfigure()

    def _schedule_cache_reconfigure(self):
        """Coalesce rapid exposure/gamma changes (e.g. slider dragging) into a
        single cache rebuild after the user pauses. Reconfiguring the cache
        clears and re-decodes every frame on a background thread, so doing it on
        every slider tick caused a stutter — debouncing fixes that. The current
        frame is refreshed immediately for live preview (cheap), while the full
        rebuild waits for the drag to settle."""
        # Immediate, cheap live preview of just the current frame.
        self._refresh_current_frame_preview()
        # Debounce the expensive full-cache reconfigure.
        if not hasattr(self, "_eg_timer"):
            self._eg_timer = QTimer(self)
            self._eg_timer.setSingleShot(True)
            self._eg_timer.timeout.connect(self._apply_cache_reconfigure)
        self._eg_timer.start(120)

    def _apply_cache_reconfigure(self):
        if not getattr(self, "_src_dir", None):
            return
        self._cache.configure(self._src_dir, self._src_prefix, self._src_padding,
                              self._src_ext, self._src_start, self._src_end,
                              self._exposure, self._gamma)

    def _refresh_current_frame_preview(self):
        """Instant preview of the current frame at the new exposure/gamma. Forces
        a fresh decode (bypassing the stale cache, which still holds the old
        exp/gam) of just this one frame — cheap relative to rebuilding the whole
        cache. Best-effort; skips silently if the source isn't ready."""
        try:
            if not getattr(self, "_src_dir", None):
                return
            if self._mode == "SOURCE":
                frame = max(self._src_start, min(self._src_end, self._current_frame))
            else:
                in_f = next((inf for of, inf in self._frame_map
                             if of == self._current_frame), None)
                if in_f is None and self._frame_map:
                    in_f = self._frame_map[0][1]
                if in_f is None:
                    return
                frame = int(round(in_f))
            path = os.path.join(
                self._src_dir,
                f"{self._src_prefix}{frame:0{self._src_padding}d}{self._src_ext}")
            img = load_frame(path, self._exposure, self._gamma)
            if img is not None:
                burn_lines = self._make_burn_lines(self._current_frame)
                self.canvas.set_frame(img, burn_lines, self._mode)
        except Exception:
            pass

    # ── Exposure / gamma readout + popup ──────────────────────────────────────

    def _on_exp_value(self, val):
        val = max(-4.0, min(4.0, val))
        self.exp_readout.set_value(val)
        self._position_readouts()
        self._on_exposure_changed(val)

    def _on_gam_value(self, val):
        val = max(0.25, min(4.0, val))
        self.gam_readout.set_value(val)
        self._position_readouts()
        self._on_gamma_changed(val)

    def _open_value_popup(self, popup, readout):
        # Seed the popup from the current readout-displayed value, then show it
        # just above the readout (in global coords).
        gp = readout.mapToGlobal(readout.rect().topLeft())
        popup.adjustSize()
        x = gp.x()
        y = gp.y() - popup.height() - 4
        popup.move(x, y)
        popup.show()
        popup.raise_()
        popup.activateWindow()

    def _position_readouts(self):
        # Bottom-left corner, side by side on one line. The frame burn-in moved
        # to the bottom-right, so the readouts now sit at the very bottom-left
        # (the line the burn-in used to occupy).
        if not hasattr(self, "exp_readout"):
            return
        c = self.canvas
        margin_x = 12
        y = c.height() - 8 - self.exp_readout.height()
        self.exp_readout.move(margin_x, y)
        gx = margin_x + self.exp_readout.width() + 18
        self.gam_readout.move(gx, y)
        self.exp_readout.show(); self.gam_readout.show()

    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        if obj is self.canvas and event.type() == QEvent.Type.Resize:
            self._position_readouts()
        # Display-mode combo popup: surface clicks on disabled items as a
        # disabledModeClicked signal so MainWindow can show feedback. Qt would
        # otherwise swallow the click silently (no currentIndexChanged).
        view = self.display_mode_combo.view()
        if obj is view.viewport() and event.type() == QEvent.Type.MouseButtonPress:
            idx = view.indexAt(event.position().toPoint())
            if idx.isValid():
                item = self.display_mode_combo.model().item(idx.row())
                if item is not None and not item.isEnabled():
                    self.disabledModeClicked.emit(item.text())
                    # Let Qt's default handling continue — it's already a no-op
                    # for disabled items, but no need to consume the event.
        return super().eventFilter(obj, event)


    def _on_in_point_input(self):
        """In-point field committed — set scrubber's in point.
        Display-only TC: typed entry still parses a plain frame number; a
        timecode string falls back to restoring the formatted current value."""
        try:
            f = int(self.in_point_input.text())
        except ValueError:
            self.in_point_input.setText(self._fmt_field(self.scrubber.inPoint()))
            return
        # Scrubber clamps internally — just call setInPoint
        self.scrubber.setInPoint(f)
        # Refresh field text from scrubber (clamping may have adjusted it)
        self.in_point_input.setText(self._fmt_field(self.scrubber.inPoint()))

    def _on_out_point_input(self):
        """Out-point field committed — set scrubber's out point.
        Display-only TC: typed entry still parses a plain frame number; a
        timecode string falls back to restoring the formatted current value."""
        try:
            f = int(self.out_point_input.text())
        except ValueError:
            self.out_point_input.setText(self._fmt_field(self.scrubber.outPoint()))
            return
        self.scrubber.setOutPoint(f)
        self.out_point_input.setText(self._fmt_field(self.scrubber.outPoint()))

    def _on_in_point_changed_external(self, frame):
        """Scrubber's in point changed (drag, button, etc) — update field."""
        if hasattr(self, 'in_point_input') and not self.in_point_input.hasFocus():
            self.in_point_input.blockSignals(True)
            self.in_point_input.setText(self._fmt_field(frame))
            self.in_point_input.blockSignals(False)

    def _on_out_point_changed_external(self, frame):
        """Scrubber's out point changed (drag, button, etc) — update field."""
        if hasattr(self, 'out_point_input') and not self.out_point_input.hasFocus():
            self.out_point_input.blockSignals(True)
            self.out_point_input.setText(self._fmt_field(frame))
            self.out_point_input.blockSignals(False)

    def closeEvent(self, event):
        self._cache.stop(); super().closeEvent(event)
