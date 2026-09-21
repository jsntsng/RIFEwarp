# Changelog

All notable changes to RIFEwarp are documented in this file. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.4] - 2026-09-21

### Changed
- `app/setup.sh` prerequisite check is now distro-aware (Rocky Linux 9/10 and
  Ubuntu 22.04/24.04). Detects apt vs dnf and prints the matching install
  hints, including the deadsnakes PPA and `python3.11-venv` guidance on apt.
- Added an `ensurepip` probe that catches a missing `python3.11-venv` package.
- Added a non-fatal warning when `libxcb-cursor.so.0` is absent (Qt xcb
  platform plugin may fail to load).

## [1.1.3] - 2026-09-17

### Fixed
- Default curve span now tracks the loaded sequence length instead of the
  app-init fallback range. `TimewarpCurve.is_untouched_default` and
  `reset_to_identity` (`core/timewarp.py`) detect an unedited default curve
  and rebuild it as a 1:1 identity curve spanning the newly loaded sequence.

### Added
- Roadmap document (`overview.md`).

## [1.1.1] — 2026-05-30

### Added — Curve editor top bar
- Tangent operation buttons: Break and Auto, with custom line-art icons. Break
  shows a dotted V (handles split); Auto shows a smooth curve through the
  keypoint (handles unified). Both wired to existing tangent handlers;
  existing `B` and `U` hotkeys preserved.
- Flame-style Y knob (`YKnob`) for scrubbing the selected keypoint's in_frame.
  Click-drag horizontally to scrub; Shift = fine (0.1 frame/px), Ctrl =
  coarse (10 frame/px). Single undo entry per drag. Shows `--` when no
  keypoint is selected or selection has mixed Y values.
- Shared Frame / TC (metadata) / TC (fps) display dropdown mirrored from the
  viewer. Selecting in either dropdown syncs both. Mode persists in `.rtp`.
- `↺` Reset Curve button replaces the old per-keypoint tangent reset; see
  Changed section below.

### Added — Snapshots panel
- Notes column next to the snapshot name. Inline edit via double-click on the
  notes cell; single-click on any cell still sets active.

### Added — UX feedback
- Visible disabled state on combo items: muted color `#5a5a5e` via QSS
  `::item:disabled` rule instead of being indistinguishable from enabled items.
- Click-feedback status message when a disabled combo item is clicked (e.g.
  `"TC (metadata) not available — this sequence has no embedded timecode."`).

### Changed
- Side panels (settings + snapshot) widened from 360 px to 420 px initial
  splitter size. Minimum width 180 px unchanged.
- Global font sizing converted from pixel-based QSS rules to point-based, with
  calibrated tier bumps for readability. Body text 10 pt, labels 9 pt,
  hint/status 8 pt (up from 11/10/9 px equivalent).
- Snapshot notes editing moved from a `QLineEdit` below the snapshot list to
  an inline cell in the new Notes column. The bottom notes field removed.
- The `↺` button in the keypoint group now resets the entire active snapshot's
  curve to identity (modal confirm required). Per-keypoint tangent reset is
  still available via right-click → Reset.
- Snapshot panel switched from `QListWidget` to `QTreeWidget` with two columns
  (Name / Notes).
- Sequence change preserves snapshot keypoints unconditionally; off-canvas
  keypoints persist and reappear when the range later widens.
  `apply_project` uses `IOPanel.has_sequence()` to decide between
  current-sequence range and saved range, replacing the previous 1001/1072
  value sentinel.

### Fixed
- Queue cell-widget (trash button) and runner-signal callbacks no longer use
  captured row indices. `RetimeJob` now carries a stable `job_id`; all
  callbacks dispatch via id-lookup. Eliminates silent corruption when
  removing or clearing jobs would route subsequent updates to the wrong row.
- Opening a project (`_open_project_path`) now clears the previous project's
  queue before applying the new project's state. Previously, project A's
  queued jobs could survive into project B.
- `YKnob` width increased to 120 px to fit the longest TC string with padding.
- Dead code removed: unused `make_default_snapshot` helper, dead `Callable`
  and several unused PyQt6 imports.

[1.1.1]: #111--2026-05-30

## [1.1.0] — 2026-05-30

### Added — Snapshots system
- Snapshot panel docked to the right of the curve editor: named curve variants
  persisted in `.rtp` project files. New / Duplicate / Rename / Delete
  operations. Active snapshot drives the curve editor, dope sheet, scrubber,
  in/out fields, and render queue. PageUp / PageDown cycle through snapshots
  when focus is in the panel or curve editor; text fields are excluded.
- Per-snapshot UI: render-target badge (▶) on the active row, drag-to-reorder,
  notes field per snapshot, color tag from a fixed preset palette set via
  right-click → Color submenu.
- Render queue Snapshot column: each queued job displays the source snapshot's
  name and color, frozen at queue time. Renaming or deleting the source
  snapshot after queueing does not affect the queue row.

### Added — Curve editor tangent system
- 5-mode tangent system: Constant, Linear, Hermite, Bezier, Natural.
  Per-keypoint, mixable across the curve. Weighted breakable handles
  (Flame-accurate).
- Rubber-band multi-select for keypoints.
- Right-click context menu for mode selection and Break / Unify / Reset
  operations.
- Keyboard shortcuts: `B` (Break), `U` (Unify).
- Interp dropdown displays "Mixed" read-only when selection spans multiple
  modes.

### Added — Viewer display modes and overlays
- Frame / TC (metadata) / TC (fps) display modes selectable via dropdown at
  the left of the viewer control row. Metadata TC reads embedded SMPTE
  timecode from DPX and EXR sources via OpenImageIO. Drop-frame timecode
  detected and displayed as `--:--:--:--`.
- Three-line canvas burn-in in the bottom-right corner: frame / metadata-TC /
  fps-TC. In TIMEWARP mode, each line includes its source counterpart in
  parentheses, two-column aligned.
- Click-to-reveal exposure and gamma controls: readouts overlaid on the canvas
  bottom-left, click to reveal slider + reset in a translucent popup, dismiss
  via click-away or Esc.

### Changed
- Viewer layout rearranged: display-mode dropdown and fps controls at far
  left of the control row; SOURCE / TIMEWARP buttons at far right; loop
  button enlarged to match range-zoom button. Cache counter moved from
  viewer to the I/O panel's INPUT SEQUENCE group.
- RETIMED button relabeled "TIMEWARP" (display only — internal mode token
  unchanged for project-file compatibility).
- Removed redundant SOURCE / RETIMED mode badge from the viewport top-left.
- Shift key during keypoint drag now auto axis-locks (replaces
  snap-to-integer; fractional precision always on).
- Curve editor hover tooltip and drag readout: two-line, centered above the
  keypoint, no background, no border.

### Fixed
- Range-zoom Y-fit now frames actual keypoints in the visible range rather
  than a diagonal geometric guess.
- Exposure / gamma slider freeze: cache reconfigure debounced (120 ms) so
  adjustments don't rebuild the cache per slider tick.
- Scrubber start / end labels centered consistently on both axes.

[1.1.0]: #110--2026-05-30
