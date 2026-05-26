# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

RIFEwarp is a PyQt6 desktop application for VFX retiming of image sequences. The user draws a **timewarp curve** (output frame → source frame mapping), then renders the result using the RIFE AI model to synthesize sub-frame interpolations. Integer-aligned frames are copied directly; fractional positions are AI-interpolated at a precise blend `t`.

## Running the app

```bash
# From the install root (requires venv to be set up via app/setup.sh)
./launch.sh

# Or directly from app/:
cd app
../venv/bin/python3 main.py
```

`launch.sh` exports `RIFEWARP_INSTALL_DIR` which `core/paths.py` uses to locate `rife/` and `venv/` at runtime. The app has no config files — all paths are derived from the install root.

## Setup (first time)

```bash
# Creates ~/rife_retime/, clones ECCV2022-RIFE, creates venv, installs deps
bash app/setup.sh

# After setup, download the pretrained model and place it at:
# rife/train_log/   OR   rife/models/<model-name>/flownet.pkl
```

Dependencies: Python 3.11, PyQt6, PyTorch (CUDA 12.6), numpy, opencv-python-headless.

## Directory layout

```
app/
  main.py              Entry point
  core/
    timewarp.py        TimewarpCurve model — pure logic, no Qt
    rife_runner.py     QThread that drives rendering
    sequence.py        Image sequence scanner (SequenceInfo)
    paths.py           Runtime path resolution (install dir, venv, model)
    project.py         .rtp project file save/load
  ui/
    main_window.py     Top-level window, wires all signals
    curve_editor.py    Interactive curve canvas with tangent handles
    dope_sheet.py      Horizontal keyframe timeline view of the same curve
    io_panel.py        Input/output directory + format fields
    sequence_viewer.py Scrubable image viewer
    settings_panel.py  RIFE model/FP16/tile/ensemble/scale/scene-cut settings
    queue_panel.py     Render queue (RetimeJob dataclass, JobStatus enum)
  assets/style.qss     Dark theme stylesheet
rife/
  rife_batch.py        Standalone batch inference script (called as subprocess)
  model/               Shared model architecture files (warplayer.py, etc.)
  models/              Downloaded pretrained models
```

## Architecture

**Core layer** (`app/core/`) is pure Python with no Qt dependency. `TimewarpCurve` owns all keypoint data and curve math; the UI never writes frame values directly — it calls `curve.evaluate()`, `curve.build_frame_list()`, etc.

**UI layer** (`app/ui/`) only reads from `core` objects and emits Qt signals upward. `MainWindow` is the signal hub: `io_panel.sequenceChanged` → `_on_sequence_changed()` resets the curve range; `curve_editor.curveChanged` → `_on_curve_changed()` propagates to dope sheet and viewer.

**Curve editor and dope sheet share the same `TimewarpCurve` object.** Undo/redo lives in `CurveEditor.canvas._undo_stack`; the dope sheet's `_snapshot_cb` and `_undo_cb` are wired to the same stack from `MainWindow.__init__`.

**Rendering pipeline:** `MainWindow._add_to_queue()` snapshots a `RetimeJob` dataclass (deep copy of the curve + all I/O settings) and hands it to `QueuePanel`. `QueuePanel` spawns a `RifeRunner(QThread)` per job. `RifeRunner.run()` splits the frame list into:
1. Integer frames → direct `shutil.copy2`
2. Fractional frames → written to a JSON task file, then `rife_batch.py` is called **once per job** as a subprocess (so the RIFE model loads only once per job, not per frame).

`rife_batch.py` prints `DONE <frame_num>` per frame to stdout; `RifeRunner` reads these lines to update the progress signal live.

## Curve interpolation modes

`TimewarpCurve` supports five interpolation modes on a per-keypoint basis (set on the outgoing keypoint of each segment):

| Mode | Description |
|------|-------------|
| `HERMITE` | Catmull-Rom averaged-chord tangents; per-side overrides and weight handles |
| `BEZIER` | Cubic Bezier; X-clamped to prevent time-fold; solved by bisection |
| `NATURAL` | Globally-solved C2-continuous spline (tridiagonal Thomas solve) |
| `LINEAR` | Straight line between keypoints |
| `CONSTANT` | Hold value until next keypoint |

`SMOOTH` is a legacy alias that maps to `HERMITE` via `coerce_interp()`. All serialized projects store the coerced value.

## Project files (.rtp)

Plain JSON. `core/project.py` — `collect_project(main_window)` walks the live UI widgets to build the dict; `apply_project(main_window, data)` restores it. The curve is embedded as `TimewarpCurve.to_dict()` / `from_dict()`. The queue is serialized but job status is not restored (jobs reload as READY).

## Model loading in rife_batch.py

`rife_batch.py` dynamically imports the `Model` class from the model directory using `importlib.util`, trying architecture files in order (`RIFE_HDv3.py` → `RIFE_HDv2.py` → `RIFE_HD.py` → `RIFE.py`). It creates `sys.modules` aliases (`train_log`, `model`, folder name) so internal relative imports in older RIFE model files resolve correctly regardless of the model folder name.

## Sequence viewer — control bar and burn-in (as of last session)

### Frame / TC display dropdown (`display_mode_combo`)

A QComboBox (138 px wide) at the far-left of the transport controls row. Items:
- **Frame** — plain frame numbers (always enabled).
- **TC (metadata)** — SMPTE timecode read from the first source frame via OIIO.
  Disabled automatically when no embedded TC is found; reverts to Frame if the
  current mode becomes invalid on a sequence change.
- **TC (fps)** — timecode computed from frame count relative to sequence start,
  using the fps value from file metadata (or 24.0 as fallback).

Switching modes calls `_on_display_mode_changed()`, which refreshes:
- scrubber start/end labels (`_lbl_start` / `_lbl_end`, both 100 px wide)
- current / in-point / out-point transport fields (`frame_input`, `in_point_input`,
  `out_point_input`, all 100 px) via `_refresh_frame_fields()`
- the canvas burn-in via `_show_frame()`

TC metadata is probed once per sequence change in `_probe_timecode_for_sequence()`:
reads the first source frame header only (~1 ms for DPX, ~0.08 ms for EXR) via
OIIO. Probe order: `dpx:TimeCode` string → `smpte:TimeCode` BCD uint32 →
`nuke/input/timecode` string. Drop-frame is detected via bit 6 of the SMPTE word
or a `;` separator; drop-frame sequences show `--:--:--:--` rather than incorrect
non-drop offsets. FPS is read from `framesPerSecond` (EXR rational pair) or
`dpx:FrameRate`. Results cached in `_tc_start_str`, `_tc_fps`, `_tc_has_meta`,
`_tc_is_drop`.

### Canvas burn-in (three lines, two columns)

`_make_burn_lines(out_frame)` → list of `(left, right)` string pairs rendered
bottom-right on the canvas by `ViewerCanvas.paintEvent`:

| Row | SOURCE mode | RETIMED mode |
|-----|-------------|--------------|
| Line 1 | `f{src}` | `f{out}` / `(src f{src})` |
| Line 2 | metadata TC (absent if no TC or drop-frame) | output meta TC / `(src {src meta TC})` |
| Line 3 | fps TC (absent if no fps metadata) | output fps TC / `(src {src fps TC})` |

The renderer right-aligns the left column and left-aligns the right `(src ...)` column
at a consistent gap so the two columns track vertically. In SOURCE mode the right column
is always empty, collapsing to a single right-aligned column (gap = 0). Lines where
both strings are empty are skipped entirely. White text with 1px dark shadow — no
opaque background box.

### Layout changes

- **fps** moved from a standalone cluster to the **left side** of the controls row,
  immediately after the display-mode dropdown (12 px breathing room, then a 24 px
  `fps` label and a 72 px editable QComboBox). `fps_spin` is kept as an alias for
  `fps_combo` so legacy call sites still work.
- **SOURCE / TIMEWARP buttons** (62 px / 76 px) pinned to the **far right** of the
  controls row, after the right stretch. Button text is "TIMEWARP" for display; the
  internal `self._mode` value and all logic remain `"RETIMED"` — no internal rename yet.
- **Loop button** enlarged to 28 px wide with `font-size:15px` (was 24 px / 11 px).
- **Cache counter** removed from the viewer transport bar. It now lives in the
  **I/O panel's INPUT SEQUENCE group** as a "Cache" form row driven by
  `viewer.cacheUpdated` signal → `io_panel.update_cache_display(n, total)`. The
  signal is wired in `MainWindow.__init__` (line 53).
- **SOURCE/RETIMED viewport badge** (top-left canvas overlay) has been removed; mode
  is communicated solely by the SOURCE / TIMEWARP button states.

### Deferred (not yet implemented)

- **TC entry to navigate**: `_on_frame_input` parses with `int()` only; typing a
  timecode string into the current-frame field does not navigate. Full TC-aware
  parsing in the transport fields is deferred.
- **Full internal "RETIMED" → "TIMEWARP" rename**: all code, attributes, and signals
  still use `"RETIMED"` / `_set_retimed_mode` / `RETIMED` mode strings.

## Current status (as of last session)

RIFEwarp is **functionally complete and working as intended** — it produces clean,
usable output. This is a stable baseline, NOT a work-in-progress with known breakage.
Treat existing functionality as working: when making changes, preserve current behavior
unless a change is explicitly requested.

The project is in a **refinement phase**:
1. **Quality-of-life features** — the Frame/TC display dropdown and burn-in were
   completed last session. Next QoL work TBD per task.
2. **Design/layout rework** — revisiting the UI layout and color scheme (the dark
   theme in `assets/style.qss`); not yet specified beyond what was done above.

The render pipeline and curve math are considered solid and should be touched cautiously.
The work ahead is mostly in the `ui/` layer and `style.qss`, not `core/`.

## Working guardrails

- **Do not modify the `rife/` directory** (ECCV2022-RIFE submodule/vendored code) unless
  explicitly asked — it's external and the `importlib` loading logic is intentionally fragile.
- **Preserve the core/ ↔ ui/ separation**: never add Qt imports to `app/core/`. UI reads
  from core and emits signals upward; core stays pure logic.
- **Show diffs and explain changes before applying**, especially in `core/` and the
  render pipeline.
- The app runs on a shared VFX/gaming workstation (Rocky Linux 9, RTX 4090). RIFE jobs
  are memory-hungry (~40GB+ RAM with `--ensemble`); keep this in mind for any pipeline changes.

## Design rework notes

- Current theme is a dark QSS stylesheet (`app/assets/style.qss`).
- Layout and color changes are wanted but not yet specified — ask for direction before
  large restructuring rather than assuming.
