# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Current app version: **1.1.1** (see `/VERSION` — the single source of truth, read at runtime by `app/ui/main_window.py`).

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
    snapshot.py        Snapshot + SnapshotCollection — pure logic, no Qt
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
    snapshot_panel.py  Named curve-variant list (badge, swatch, drag-reorder,
                       notes, color tag); palette exported for queue reuse
    queue_panel.py     Render queue (RetimeJob dataclass, JobStatus enum,
                       Snapshot column reads from snapshot_panel palette)
  assets/style.qss     Dark theme stylesheet
rife/
  rife_batch.py        Standalone batch inference script (called as subprocess)
  model/               Shared model architecture files (warplayer.py, etc.)
  models/              Downloaded pretrained models
```

## Architecture

**Core layer** (`app/core/`) is pure Python with no Qt dependency, with one deliberate exception: `app/core/rife_runner.py` uses `QThread` and `pyqtSignal` for cross-thread render-progress signaling. This is intentional — the runner needs to live on a background thread and emit progress back to the UI thread, which is exactly what `QThread` is for. Treat `rife_runner.py` as the bridge between core logic and the Qt runtime; all other files under `app/core/` remain strictly Qt-free. `TimewarpCurve` owns all keypoint data and curve math; the UI never writes frame values directly — it calls `curve.evaluate()`, `curve.build_frame_list()`, etc.

**UI layer** (`app/ui/`) only reads from `core` objects and emits Qt signals upward. `MainWindow` is the signal hub: `io_panel.sequenceChanged` → `_on_sequence_changed()` resets the curve range; `curve_editor.curveChanged` → `_on_curve_changed()` propagates to dope sheet and viewer.

**Curve editor and dope sheet share the same `TimewarpCurve` object.** Undo/redo lives in `CurveEditor.canvas._undo_stack`; the dope sheet's `_snapshot_cb` and `_undo_cb` are wired to the same stack from `MainWindow.__init__`.

**Active-snapshot binding (Model A):** the active snapshot's curve *is* the working state — there is no separate working buffer. `curve_editor.canvas.curve`, `dope_sheet.curve`, and `snapshots.active().curve` are the same object. `MainWindow._bind_active_snapshot()` rebinds all UI to the new active snapshot when the user switches snapshots (click, PageUp/PageDown, or programmatic). Undo/redo stacks are cleared on switch since cross-snapshot undo would be confusing.

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

Plain JSON. `core/project.py` — `collect_project(main_window)` walks the live UI widgets to build the dict; `apply_project(main_window, data)` restores it.

Top-level shape (v2 / 1.1.0+):

- `version: 2`
- `input`, `output` — directory + format + padding + start/end
- `settings` — RIFE model name, fp16, tile, ensemble, scale, scene-cut
- `snapshots: [Snapshot.to_dict(), ...]` — the named curve variants
- `active_snapshot_id` — uuid hex string keying into `snapshots`
- `queue: [job_dict, ...]` — see "Render queue serialization" below

**Legacy v1 back-compat shim.** Files written before the snapshots system had top-level `curve` / `in_point` / `out_point` instead of `snapshots`. `_build_snapshot_collection()` in `core/project.py` detects the absence of a `snapshots` key and synthesizes a single `"default"` snapshot wrapping the legacy fields, so old projects load cleanly. Saving always uses the new schema; the legacy keys are not written back.

**Render queue serialization.** `collect_project` writes each `RetimeJob` as a dict containing name, paths, padding, frame range, curve, model name, scale, status, and the snapshot stamp (`snapshot_name` / `snapshot_color`) captured at queue time. **`apply_project` does NOT currently restore queued jobs** — the saved queue array is discarded on load. This is a known pre-existing gap; see "Deferred" below. The queue serializer is intentionally complete on the write side so a future restorer can read existing files.

## Snapshots system

A **snapshot** is a named, persisted curve variant within a project. A project always holds at least one snapshot; exactly one is "active" at a time. The active snapshot's curve and in/out points are what the curve editor, dope sheet, scrubber, in/out fields, and render queue read from and write to.

**Data model (`app/core/snapshot.py`, Qt-free):**

| Field | Type | Notes |
|---|---|---|
| `id` | str (uuid4 hex) | identity; assigned on creation, never changes |
| `name` | str | display only; duplicates allowed (uuid is identity) |
| `curve` | `TimewarpCurve` | full keypoint state including tangents |
| `in_point` / `out_point` | int | scrubber markers, per-snapshot |
| `notes` | str | free-form one-liner, edited inline in the Notes column |
| `color` | str \| None | palette key (`"red"`, `"blue"`, …) — not a hex; `None` = no swatch |

`SnapshotCollection` is the container: `add` / `remove` / `duplicate` / `rename` / `set_active` / `cycle(±1)` / `move(id, new_index)`. Invariants: ≥1 snapshot always (`remove` raises if `len == 1`), `active_id` always valid. `cycle` wraps. `duplicate` copies curve, notes, and color so a clone is faithful, not a fresh blank.

**UI (`app/ui/snapshot_panel.py`).** The panel is docked to the right of the curve editor (inside a `QSplitter` so the user can resize; initial 420 px, min 180 px). It owns no snapshot state — it renders and mutates `MainWindow.snapshots`. The widget is a `QTreeWidget` with two columns:

- **Column 0 — Name.** Rendered by a custom `_SnapshotDelegate`: `[ ▶|· (12 px) ][ swatch|· (12 px) ][ name ]`. Badge ▶ on the active row; swatch if color is set. Double-click enters inline rename; single-click sets active. `_SnapshotDelegate` is installed on column 0 only — column 1 uses Qt's default renderer.
- **Column 1 — Notes.** Plain text. Double-click enters inline edit (QLineEdit editor, Esc cancels, Enter/focus-loss commits). Single-click on the notes cell also sets active (same as single-click on the name cell). Active snapshot does NOT change as a side-effect of opening the notes editor.

Drag-to-reorder uses `QAbstractItemView.DragDropMode.InternalMove`; `_on_rows_moved` syncs the core list to the widget order. There is no bottom `QLineEdit` notes field — all notes editing happens inline. Right-click → `Color` submenu sets / clears the color tag.

**Palette.** `PALETTE_HEX` in `snapshot_panel.py` maps the keys (`red`, `orange`, `yellow`, `green`, `cyan`, `blue`, `purple`, `pink`) to muted hex values tuned for the dark theme. The keys are stored in projects; the hex values can be retuned freely. `make_swatch_icon(hex)` and `make_blank_icon()` are exported for reuse by the render queue's `Snapshot` column.

**Active-change fan-out.** `MainWindow._bind_active_snapshot()`:
1. Points the curve editor and dope sheet at the new active snapshot's curve.
2. Clears undo/redo stacks (cross-snapshot undo is confusing).
3. Restores the scrubber's in/out points from `active.in_point` / `active.out_point`, with `_suppress_scrubber_mirror` set so the mirror handler doesn't write the values straight back.
4. Calls `_on_curve_changed()` to refresh derived UI (output frame count, dope sheet, viewer frame map).

**Render queue stamp.** `MainWindow._add_to_queue()` reads `self.snapshots.active().name` and `.color` and stores them on the new `RetimeJob` as `snapshot_name` and `snapshot_color`. These are **frozen strings** — never re-resolved against the live `SnapshotCollection`. Renaming or deleting the source snapshot after the job is queued does not affect the queue row. The `Snapshot` column lives at `COL_SNAPSHOT = 3` in `queue_panel.py` (after Shot/Input/Output, before Frames/Status), shows the captured name with a swatch icon (or a transparent placeholder of the same footprint), and is interactive-resizable with a default width of 140 px.

## Curve editor top bar

The info bar is a single `QHBoxLayout` with two groups:

**Left group (flush left):**
`[count label][X label][X edit][·][Y label][Y edit][VLine][+][×][↺]`

- **+** adds a keypoint at the playhead; **×** deletes the keypoint at the playhead; these are unchanged since 1.1.0.
- **↺ Reset Curve** — modal-confirm reset of the *entire* active snapshot's curve to a two-point identity. This is a curve-level operation, NOT per-keypoint tangents. Calls `canvas._reset(1.0)` which pushes one undo entry. Per-keypoint tangent reset is right-click → `Reset Tangents to Auto`.

**Center cluster (between two addStretch):**
`[stretch][display dropdown][YKnob][VLine][Interp label][Interp combo][VLine][Break][Auto][stretch]`

- **Display dropdown** — mirrors the viewer's Frame / TC (metadata) / TC (fps) modes via `MainWindow.set_display_mode`. See Shared display mode below.
- **YKnob** — custom-painted `QWidget` (`YKnob` class in `curve_editor.py`). Click-drag horizontally scrubs the selected keypoint's in_frame. Modifiers: plain = 1 frame/px, Shift = 0.1, Ctrl = 10. Integer rounding at drag-end; one undo entry per drag. Formatter function injected by `_install_knob_formatter` — reuses `viewer.display_frame()` so TC formatting is single-sourced.
- **Break** / **Auto** — `TangentIconButton` instances with custom line-art icons. Break shows a dotted V (calls `_break_selected`, same as `B`); Auto shows a smooth S-curve through a dot (calls `_auto_selected`, same as `U`). The Auto tooltip reads "Auto tangents (U)" to surface the existing hotkey.

## Shared display mode

`MainWindow._display_mode: str` — one of `"Frame"`, `"TC (metadata)"`, `"TC (fps)"`. Single source of truth. `MainWindow.set_display_mode(mode)` is deduped (no emit if mode unchanged). `MainWindow.displayModeChanged(str)` signal fans out to both `viewer.apply_display_mode` and `curve_editor.apply_display_mode`.

Both widgets' dropdowns use `blockSignals(True)` during programmatic sync to prevent recursive emit loops. User selection in either dropdown calls `set_display_mode` on MainWindow; the signal round-trips back to both combos to sync them.

`TC (metadata)` is disabled in both combos when the sequence has no embedded TC, via:
- Viewer: `item.setEnabled(self._tc_has_meta)` in `_probe_timecode_for_sequence`.
- Curve editor: `apply_tc_metadata_available(bool)` called from `MainWindow.tcMetadataAvailableChanged`.
- Clicking a disabled item emits `disabledModeClicked(text)` (event-filter on `combo.view().viewport()`) and MainWindow posts a status-bar message.

Persists in `.rtp` as `display_mode` (top-level key). Back-compat default: `"Frame"`.

## Font sizing convention

All `font-size:` rules in `app/assets/style.qss` **and** in inline `setStyleSheet(...)` strings use **point sizes**, not pixels. Point sizes follow system DPI font scaling; pixel sizes do not. Do not introduce `font-size: Npx` literals.

Current tier values:
- Body text: `10pt` (QMainWindow / QWidget baseline)
- Labels, dropdowns, headers: `9pt`
- Hint / status / log text: `8pt`
- Action glyphs (+, ×, ↺, ▶): `11pt`

Canvas-painted text uses `QFont("Monospace", N)` (point size) directly — unchanged from 1.1.0.

## Queue job identity

`RetimeJob` carries `job_id: str = field(default_factory=lambda: uuid.uuid4().hex)` — a stable per-job UUID assigned at construction. All cell-widget callbacks (trash button) and runner-signal callbacks (progress / log / finished) dispatch via `_row_for_job_id(job_id)` rather than captured row indices. When a job is removed while its runner is still active, in-flight signals silently no-op (the id is no longer found in `self.jobs`).

## Sequence change and apply_project range restoration

`_on_sequence_changed` updates each snapshot's curve range metadata (`set_range`) but **never modifies keypoints**. Off-canvas keypoints (out_frame or in_frame outside the new range) persist in data and reappear if the range widens later.

`apply_project` uses `main_window.io_panel.has_sequence()` to decide range restoration:
- `True` — current sequence is authoritative; set all snapshot curves to the current sequence's range.
- `False` — no sequence on disk; preserve each snapshot's saved range from the `.rtp` file.

This replaces the previous sentinel check `c_in_start == 1001 and c_in_end == 1072`, which collided with real 1001–1072 sequences.

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

### Deferred — viewer-specific

- **Full internal "RETIMED" → "TIMEWARP" rename**: all code, attributes, and signals
  still use `"RETIMED"` / `_set_retimed_mode` / `RETIMED` mode strings. A `.rtp`
  compatibility shim will be needed when this lands.

## Current status (as of v1.1.1)

RIFEwarp is **functionally complete and working as intended** — it produces clean,
usable output. This is a stable baseline, NOT a work-in-progress with known breakage.
Treat existing functionality as working: when making changes, preserve current behavior
unless a change is explicitly requested.

1.1.1 shipped curve editor top-bar tangent buttons (Break / Auto), the YKnob Y scrubber, shared display mode, snapshot Notes column, font sizing normalization (px → pt), and several correctness fixes — see `CHANGELOG.md` for the full list.

The project remains in a **refinement phase**:
1. **Quality-of-life features** — next QoL work TBD per task.
2. **Design/layout rework** — revisiting the UI layout and color scheme (the dark
   theme in `assets/style.qss`); not yet specified beyond what was done above.

The render pipeline and curve math are considered solid and should be touched cautiously.
The work ahead is mostly in the `ui/` layer and `style.qss`, not `core/`.

## Deferred — project-level wishlist

These are known gaps and feature ideas, not bugs. Order is informational, not priority.

- **Queue restoration in `apply_project`.** The queue array is serialized on save but
  discarded on load (pre-existing gap recently identified during the snapshots work).
  The serializer side captures snapshot stamp + curve + paths + model name + scale +
  status, but several fields (`rife_script`, `model_dir`, `python_bin`, `use_fp16`,
  `use_tile`, `preserve_alpha`, `use_ensemble`, `scene_cut`, `frame_range_*`) are not
  written. A full restorer needs either to extend the serializer or to re-source the
  missing settings from the live settings panel at load time. Defer to its own brief.
- **Snapshot pinning / working set.** A selection layer above active: pinned
  snapshots float to the top of the panel above a divider. Would scope the next two
  items (ghost compare and render-subset).
- **Lock / unlock snapshots.** Prevent accidental edits to a locked snapshot's curve.
- **Edit queued jobs in place.** READY-only; snapshot re-stamp (frozen strings), not a live re-link.
- **TC entry to navigate.** Typed timecode string in the current-frame field navigates; currently `int()` parse only.
- **Ghost compare overlay in curve editor.** Render one or more non-active snapshots'
  curves at low opacity behind the active one. Would consume the pinned set if
  pinning lands first.
- **Export / import snapshots between projects.** Round-trip a snapshot (or several)
  through a sidecar file so users can share variants across `.rtp` projects.
- **Render-all-snapshots queue action.** Enqueue every snapshot's curve as a
  separate job with one click; cousin of "render pinned set only".
- **Full internal "RETIMED" → "TIMEWARP" rename.** Listed above under viewer-deferred;
  duplicated here so it's discoverable from this list.
- **Proxy / full-res workflow.** Source two resolutions, scrub on proxies, render
  full-res — would need a separate ingest path and a project-level toggle.
- **Bounded float-frame cache with display-time exposure/gamma transform.** Cache
  raw float frames; apply exp/gam at paint time. Removes the cache rebuild on
  exp/gam changes (currently debounced — fix #3 in the 1.1.0 changelog).
- **Dope sheet mode glyphs.** Match the curve editor's 5-way mode distinction
  (Constant / Linear / Hermite / Bezier / Natural) visually in the dope sheet rows.
- **Per-job error capture.** Surface RIFE stderr / Python tracebacks per job rather
  than aggregating them into the global log view.
- **Autosave / crash recovery + overwrite-warning dialog.** Periodic background
  snapshot of the in-memory project state; warn when "Save" would overwrite a file
  that changed on disk since open.
- **`.chan` / Nuke curve export.** Write the timewarp curve to Nuke's animation
  format so it can be applied in a downstream comp.
- **Curve editor + dope sheet viewport scrub.** Drag the playhead in either view
  directly with the mouse (currently scrubs only via the viewer).

## Working guardrails

- **Do not modify the `rife/` directory** (ECCV2022-RIFE submodule/vendored code) unless
  explicitly asked — it's external and the `importlib` loading logic is intentionally fragile.
- **Preserve the core/ ↔ ui/ separation**: never add Qt imports to `app/core/` (the sole
  documented exception is `rife_runner.py` — see Architecture above). UI reads from core
  and emits signals upward; core stays pure logic.
- **Show diffs and explain changes before applying**, especially in `core/` and the
  render pipeline.
- The app runs on a shared VFX/gaming workstation (Rocky Linux 9, RTX 4090). RIFE jobs
  are memory-hungry (~40GB+ RAM with `--ensemble`); keep this in mind for any pipeline changes.

## Design rework notes

- Current theme is a dark QSS stylesheet (`app/assets/style.qss`).
- Layout and color changes are wanted but not yet specified — ask for direction before
  large restructuring rather than assuming.
