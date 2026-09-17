"""
Timewarp curve — output frame -> input frame mapping.
Three interpolation modes: Smooth (Catmull-Rom), Linear, Constant.
No manual tangent handles.
"""
from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import List, Tuple, Optional
from enum import Enum


class InterpMode(Enum):
    CONSTANT = "constant"
    LINEAR   = "linear"
    HERMITE  = "hermite"
    BEZIER   = "bezier"
    NATURAL  = "natural"
    # Back-compat: projects/code created before the Hermite/Bezier split used
    # "smooth". It maps to HERMITE on load (see from_dict / coerce_interp).
    SMOOTH   = "smooth"


INTERP_LABELS = {
    InterpMode.CONSTANT: "Constant",
    InterpMode.LINEAR:   "Linear",
    InterpMode.HERMITE:  "Hermite",
    InterpMode.BEZIER:   "Bezier",
    InterpMode.NATURAL:  "Natural",
    InterpMode.SMOOTH:   "Hermite",   # legacy alias displays as Hermite
}

# The smooth (tangent-handle) interpolation types.
SMOOTH_MODES = (InterpMode.HERMITE, InterpMode.BEZIER, InterpMode.NATURAL,
                InterpMode.SMOOTH)

INTERP_ORDER = [InterpMode.CONSTANT, InterpMode.LINEAR,
                InterpMode.HERMITE, InterpMode.BEZIER, InterpMode.NATURAL]


def coerce_interp(mode: "InterpMode") -> "InterpMode":
    """Map the legacy SMOOTH alias to HERMITE so the rest of the code only ever
    deals with the four real modes."""
    return InterpMode.HERMITE if mode == InterpMode.SMOOTH else mode


# Smooth-curve tangent behavior:
#   False = Flame-style unclamped tangents (may overshoot past neighbour values
#           — gives ease in/out feel, can run non-monotonic).
#   True  = Fritsch-Carlson monotone clamp (no overshoot — original behavior).
# Flip to True to restore the no-overshoot default.
MONOTONE_SMOOTH = False


@dataclass
class Keypoint:
    out_frame: float
    in_frame:  float
    interp:    InterpMode = InterpMode.HERMITE
    # Per-side tangent overrides. None = auto (computed from neighbours).
    #   *_tangent : slope in source-frames per output-frame (the handle angle).
    #   *_weight  : handle length as a fraction of the adjacent segment's
    #               out-frame span (Bezier control-point distance / Hermite
    #               tangent magnitude). None = default weight (1/3 of segment).
    # broken    : if True the two sides move independently; if False they stay
    #             collinear (unified) — though lengths may still differ.
    in_tangent:  Optional[float] = None
    out_tangent: Optional[float] = None
    in_weight:   Optional[float] = None
    out_weight:  Optional[float] = None
    broken:      bool = False

    def __post_init__(self):
        # Normalise the legacy SMOOTH alias to HERMITE on construction.
        if self.interp == InterpMode.SMOOTH:
            self.interp = InterpMode.HERMITE

    def speed_hint(self, prev: "Optional[Keypoint]") -> float:
        if prev is None:
            return 1.0
        dout = self.out_frame - prev.out_frame
        if abs(dout) < 1e-9:
            return 1.0
        return (self.in_frame - prev.in_frame) / dout


class TimewarpCurve:
    def __init__(self):
        self.keypoints: List[Keypoint] = []
        self.in_start:  int = 1001
        self.in_end:    int = 1072
        self.out_start: int = 1001
        self.mode = InterpMode.HERMITE
        self._init_default()

    def _init_default(self):
        n = self.in_end - self.in_start
        self.keypoints = [
            Keypoint(out_frame=0,       in_frame=0,       interp=InterpMode.HERMITE),
            Keypoint(out_frame=float(n), in_frame=float(n), interp=InterpMode.HERMITE),
        ]

    def set_range(self, in_start: int, in_end: int, out_start: int = 1001):
        self.in_start  = in_start
        self.in_end    = in_end
        self.out_start = out_start

    def is_untouched_default(self) -> bool:
        """True if this curve is still the unedited 1:1 identity curve created
        by _init_default() for the *current* in_start/in_end — i.e. no
        keypoint has ever been added, moved, or retimed by the user."""
        if len(self.keypoints) != 2:
            return False
        first, last = self.keypoints
        span = self.in_end - self.in_start
        return (first.out_frame == 0 and first.in_frame == 0
                and last.out_frame == last.in_frame == float(span))

    def reset_to_identity(self, in_start: int, in_end: int, out_start: int = 1001):
        """Re-range and rebuild as a fresh 1:1 identity curve spanning the new
        range. Used when a still-untouched default curve should track a newly
        loaded sequence's length rather than keep the old default span."""
        self.set_range(in_start, in_end, out_start)
        self._init_default()

    @property
    def out_frame_count(self) -> int:
        if not self.keypoints:
            return 0
        return int(self.keypoints[-1].out_frame) + 1

    @property
    def in_frame_count(self) -> int:
        return self.in_end - self.in_start + 1

    def sorted_keypoints(self) -> List[Keypoint]:
        return sorted(self.keypoints, key=lambda k: k.out_frame)

    def evaluate(self, out_frame: float) -> float:
        kps = self.sorted_keypoints()
        if not kps:
            return 0.0
        if out_frame <= kps[0].out_frame:
            return kps[0].in_frame
        if out_frame >= kps[-1].out_frame:
            return kps[-1].in_frame
        for i in range(len(kps) - 1):
            k0, k1 = kps[i], kps[i+1]
            if k0.out_frame <= out_frame <= k1.out_frame:
                t = (out_frame - k0.out_frame) / (k1.out_frame - k0.out_frame)
                return self._interp_segment(kps, i, t)
        return kps[-1].in_frame

    def _interp_segment(self, kps, i, t):
        k0, k1 = kps[i], kps[i+1]
        mode = coerce_interp(k0.interp)

        if mode == InterpMode.CONSTANT:
            return k0.in_frame

        if mode == InterpMode.LINEAR:
            return k0.in_frame + t * (k1.in_frame - k0.in_frame)

        if mode == InterpMode.BEZIER:
            return _bezier_segment(kps, i, t)

        if mode == InterpMode.NATURAL:
            nat = self._natural_tangents(kps)
            return _natural_segment(kps, i, t, nat)

        # HERMITE (default smooth)
        return _hermite_segment(kps, i, t)

    def _natural_tangents(self, kps):
        """Globally-solved natural tangents, cached and re-solved whenever the
        keypoint geometry changes (positions/order). Matches Flame's 'tangents
        re-evaluated when you move a point'."""
        key = tuple((k.out_frame, k.in_frame) for k in kps)
        if getattr(self, "_nat_cache_key", None) != key:
            self._nat_cache = _solve_natural_tangents(kps)
            self._nat_cache_key = key
        return self._nat_cache

    def build_frame_list(self) -> List[Tuple[int, float]]:
        frames = []
        n_out  = self.out_frame_count
        in_max = float(self.in_frame_count - 1)

        # Collect fractional keypoint out positions that fall between integers
        kps = self.sorted_keypoints()
        fractional_outs = set()
        for kp in kps:
            if kp.out_frame != round(kp.out_frame):
                # Has a meaningful decimal — insert between floor and ceil
                fractional_outs.add(kp.out_frame)

        # Build set of all out positions to evaluate
        out_positions = list(range(n_out))  # integer steps
        for f in fractional_outs:
            if 0 <= f < n_out:
                # Replace the integer step at floor(f) with the fractional position
                # Output file number is always integer (floor)
                pass  # handled below

        for i in range(n_out):
            # Check if a fractional keypoint falls in (i, i+1)
            frac = next((f for f in fractional_outs if i < f < i+1), None)
            if frac is not None:
                # Emit the fractional position instead of the integer
                in_pos = self.evaluate(frac)
                in_pos = max(0.0, min(in_max, in_pos))
                frames.append((self.out_start + i, self.in_start + in_pos))
            else:
                in_pos = self.evaluate(float(i))
                in_pos = max(0.0, min(in_max, in_pos))
                frames.append((self.out_start + i, self.in_start + in_pos))
        return frames

    def add_keypoint(self, out_frame: float, in_frame: float,
                     interp: InterpMode = InterpMode.HERMITE):
        # out_frame is a render-timeline position (sub-frame allowed for curve
        # shaping). in_frame is a SOURCE sampling position on the synthesized
        # timeline — kept fractional (2dp) so a keypoint can demand a RIFE
        # sub-frame (e.g. source 1042.5). Snapping to a whole source frame is a
        # deliberate action done in the UI (Shift), not forced here.
        self.keypoints.append(Keypoint(
            out_frame=round(float(out_frame), 2),
            in_frame=round(float(in_frame), 2),
            interp=interp,
        ))
        self.keypoints.sort(key=lambda k: k.out_frame)

    def remove_keypoint(self, index: int):
        if len(self.keypoints) > 2:
            self.keypoints.pop(index)

    def move_keypoint(self, index: int, out_frame: float, in_frame: float):
        kps = self.sorted_keypoints()
        kp  = kps[index]
        if index == 0 or index == len(kps) - 1:
            kp.out_frame = kps[index].out_frame  # locked
        else:
            kp.out_frame = round(out_frame)
        kp.in_frame = round(float(in_frame), 2)
        self.keypoints.sort(key=lambda k: k.out_frame)

    def set_interp(self, index: int, mode: InterpMode):
        kps = self.sorted_keypoints()
        if index < len(kps):
            kps[index].interp = mode

    def reset(self, speed: float = 1.0):
        n_in  = self.in_end - self.in_start
        n_out = round(n_in / speed) if speed > 0 else n_in
        self.keypoints = [
            Keypoint(out_frame=0,           in_frame=0,           interp=InterpMode.HERMITE),
            Keypoint(out_frame=float(n_out), in_frame=float(n_in), interp=InterpMode.HERMITE),
        ]

    def to_dict(self) -> dict:
        def kp_dict(k):
            d = {"out_frame": k.out_frame,
                 "in_frame":  k.in_frame,
                 "interp":    coerce_interp(k.interp).value}
            # Only write tangent fields when set, so untouched curves and old
            # projects stay compact and forward-compatible.
            if k.in_tangent  is not None: d["in_tangent"]  = k.in_tangent
            if k.out_tangent is not None: d["out_tangent"] = k.out_tangent
            if k.in_weight   is not None: d["in_weight"]   = k.in_weight
            if k.out_weight  is not None: d["out_weight"]  = k.out_weight
            if k.broken:                  d["broken"]      = True
            return d
        return {
            "in_start":  self.in_start,
            "in_end":    self.in_end,
            "out_start": self.out_start,
            "keypoints": [kp_dict(k) for k in self.keypoints]
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TimewarpCurve":
        c = cls.__new__(cls)
        c.in_start  = d["in_start"]
        c.in_end    = d["in_end"]
        c.out_start = d.get("out_start", d["in_start"])
        c.mode      = InterpMode.HERMITE
        c.keypoints = []
        for kd in d["keypoints"]:
            try:
                interp = coerce_interp(InterpMode(kd.get("interp", "hermite")))
            except ValueError:
                interp = InterpMode.HERMITE
            c.keypoints.append(Keypoint(
                out_frame=kd["out_frame"],
                in_frame=kd["in_frame"],
                interp=interp,
                in_tangent=kd.get("in_tangent"),
                out_tangent=kd.get("out_tangent"),
                in_weight=kd.get("in_weight"),
                out_weight=kd.get("out_weight"),
                broken=kd.get("broken", False),
            ))
        return c


def _auto_tangents(kps, i):
    """Auto (Catmull-Rom averaged-chord) slopes for the segment k_i -> k_{i+1},
    then apply any per-side user overrides. Returns (m0, m1, delta, h)."""
    k0, k1 = kps[i], kps[i+1]
    y0, y1 = k0.in_frame, k1.in_frame
    h = k1.out_frame - k0.out_frame
    if abs(h) < 1e-9:
        return 0.0, 0.0, 0.0, h
    delta = (y1 - y0) / h

    if i == 0:
        m0 = delta
    else:
        prev_h = k0.out_frame - kps[i-1].out_frame
        prev_d = (k0.in_frame - kps[i-1].in_frame) / max(prev_h, 1e-9)
        m0 = (prev_d + delta) * 0.5

    if i == len(kps) - 2:
        m1 = delta
    else:
        next_h = kps[i+2].out_frame - k1.out_frame
        next_d = (kps[i+2].in_frame - k1.in_frame) / max(next_h, 1e-9)
        m1 = (delta + next_d) * 0.5

    # User overrides: out side of k0, in side of k1.
    if k0.out_tangent is not None:
        m0 = k0.out_tangent
    if k1.in_tangent is not None:
        m1 = k1.in_tangent
    return m0, m1, delta, h


def _solve_natural_tangents(kps):
    """Solve for the tangent at every keypoint so the cubic spline is
    C2-continuous (matching first AND second derivatives) at interior keypoints,
    with natural boundary conditions (second derivative = 0 at the ends).
    Classic tridiagonal natural-cubic-spline solve; returns one tangent per kp."""
    n = len(kps)
    if n == 0:
        return []
    if n == 1:
        return [0.0]
    xs = [k.out_frame for k in kps]
    ys = [k.in_frame  for k in kps]
    h  = [max(xs[i+1]-xs[i], 1e-9) for i in range(n-1)]

    a = [0.0]*n; b = [0.0]*n; c = [0.0]*n; rhs = [0.0]*n
    # Natural boundaries (S''=0 at the ends).
    b[0] = 2.0*h[0]; c[0] = h[0]; rhs[0] = 3.0*(ys[1]-ys[0])
    a[n-1] = h[n-2]; b[n-1] = 2.0*h[n-2]; rhs[n-1] = 3.0*(ys[n-1]-ys[n-2])
    # Interior C2 continuity.
    for i in range(1, n-1):
        a[i] = h[i]
        b[i] = 2.0*(h[i-1]+h[i])
        c[i] = h[i-1]
        rhs[i] = 3.0*( h[i]*(ys[i]-ys[i-1])/h[i-1]
                     + h[i-1]*(ys[i+1]-ys[i])/h[i] )
    # Thomas algorithm.
    cp = [0.0]*n; dp = [0.0]*n
    cp[0] = c[0]/b[0]; dp[0] = rhs[0]/b[0]
    for i in range(1, n):
        denom = b[i] - a[i]*cp[i-1]
        if abs(denom) < 1e-12: denom = 1e-12
        cp[i] = c[i]/denom
        dp[i] = (rhs[i] - a[i]*dp[i-1]) / denom
    m = [0.0]*n
    m[n-1] = dp[n-1]
    for i in range(n-2, -1, -1):
        m[i] = dp[i] - cp[i]*m[i+1]
    return m


def _natural_segment(kps, i, t, nat_tangents):
    """Hermite-basis evaluation using the globally-solved natural tangents,
    honoring per-side manual overrides (a dragged handle pins that side)."""
    k0, k1 = kps[i], kps[i+1]
    y0, y1 = k0.in_frame, k1.in_frame
    h = k1.out_frame - k0.out_frame
    if abs(h) < 1e-9:
        return y1
    m0 = k0.out_tangent if k0.out_tangent is not None else nat_tangents[i]
    m1 = k1.in_tangent  if k1.in_tangent  is not None else nat_tangents[i+1]
    t2 = t*t; t3 = t2*t
    h00 =  2*t3 - 3*t2 + 1
    h10 =    t3 - 2*t2 + t
    h01 = -2*t3 + 3*t2
    h11 =    t3 -   t2
    return h00*y0 + h10*h*m0 + h01*y1 + h11*h*m1


def _hermite_segment(kps, i, t):
    """Cubic Hermite interpolation for segment k_i -> k_{i+1}.
    Honors per-side tangent slope overrides and per-side weight (handle length,
    which scales the tangent magnitude — longer handle = stronger pull).
    Overshoot allowed unless the MONOTONE_SMOOTH clamp is enabled."""
    k0, k1 = kps[i], kps[i+1]
    y0, y1 = k0.in_frame, k1.in_frame
    m0, m1, delta, h = _auto_tangents(kps, i)
    if abs(h) < 1e-9:
        return y1

    if MONOTONE_SMOOTH:
        if abs(delta) < 1e-9:
            m0 = m1 = 0.0
        else:
            a = m0 / delta
            b = m1 / delta
            if a < 0: m0 = 0.0
            if b < 0: m1 = 0.0
            r = math.hypot(a, b)
            if r > 3.0:
                t3c = 3.0 / r
                m0 = t3c * a * delta
                m1 = t3c * b * delta

    # Weight scales tangent magnitude. Default weight 1/3 maps to scale 1.0, so
    # untouched handles behave exactly as before; a longer handle pulls harder.
    DEF_W = 1.0/3.0
    w0 = k0.out_weight if k0.out_weight is not None else DEF_W
    w1 = k1.in_weight  if k1.in_weight  is not None else DEF_W
    s0 = w0 / DEF_W
    s1 = w1 / DEF_W

    t2 = t * t
    t3 = t2 * t
    h00 =  2*t3 - 3*t2 + 1
    h10 =    t3 - 2*t2 + t
    h01 = -2*t3 + 3*t2
    h11 =    t3 -   t2
    return h00*y0 + h10*h*m0*s0 + h01*y1 + h11*h*m1*s1


# Back-compat alias for any external caller.
_monotone_cubic = _hermite_segment


def _bezier_segment(kps, i, t):
    """Cubic Bezier interpolation for segment k_i -> k_{i+1}.

    Control points are derived from the per-side tangent slopes and weights:
      P0 = (x0, y0)                              the leaving keypoint
      P1 = P0 + (wout*h, wout*h*m0)              out-handle of k0
      P2 = P1' = (x1 - win*h, y1 - win*h*m1)     in-handle of k1
      P3 = (x1, y1)                              the arriving keypoint
    The handle out-frame offsets are X-CLAMPED so neither control point crosses
    the opposite keypoint in time (prevents a curve that folds back on itself,
    which would be an invalid timewarp). Because X is not uniform, we solve for
    the Bezier parameter u that gives the requested out_frame, then evaluate Y.
    """
    k0, k1 = kps[i], kps[i+1]
    x0, y0 = k0.out_frame, k0.in_frame
    x1, y1 = k1.out_frame, k1.in_frame
    h = x1 - x0
    if abs(h) < 1e-9:
        return y1

    m0, m1, _delta, _h = _auto_tangents(kps, i)

    # Handle lengths (weights) as a fraction of the segment span. Default 1/3
    # gives a Bezier that closely matches the Hermite of the same tangents.
    wout = k0.out_weight if k0.out_weight is not None else (1.0/3.0)
    win  = k1.in_weight  if k1.in_weight  is not None else (1.0/3.0)

    # X offsets of the two interior control points, clamped so they stay within
    # the segment in time (0 < cx < h) — this is the X-clamp that prevents folds.
    dx0 = max(1e-3, min(h - 1e-3, wout * h))
    dx1 = max(1e-3, min(h - 1e-3, win  * h))
    # If the two interior points would cross, pull them back proportionally.
    if dx0 + dx1 > h:
        s = h / (dx0 + dx1)
        dx0 *= s; dx1 *= s

    # Control points (x, y)
    p0x, p0y = x0, y0
    p1x, p1y = x0 + dx0,      y0 + dx0 * m0
    p2x, p2y = x1 - dx1,      y1 - dx1 * m1
    p3x, p3y = x1, y1

    target_x = x0 + t * h

    # Solve cubic Bezier X(u) = target_x for u in [0,1] (X is monotonic because
    # control points are X-clamped within the segment). Bisection is plenty.
    def bx(u):
        mu = 1 - u
        return (mu*mu*mu*p0x + 3*mu*mu*u*p1x + 3*mu*u*u*p2x + u*u*u*p3x)
    lo, hi = 0.0, 1.0
    for _ in range(40):
        mid = (lo + hi) * 0.5
        if bx(mid) < target_x:
            lo = mid
        else:
            hi = mid
    u = (lo + hi) * 0.5
    mu = 1 - u
    return (mu*mu*mu*p0y + 3*mu*mu*u*p1y + 3*mu*u*u*p2y + u*u*u*p3y)
