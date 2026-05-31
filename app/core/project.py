"""RIFE Retime Project (.rtp) — JSON save/load."""
from __future__ import annotations
import json

# Bumped from 1 to 2 when snapshots replaced the top-level curve/in_point/out_point.
PROJECT_VERSION = 2


def save_project(path: str, data: dict):
    data["version"] = PROJECT_VERSION
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_project(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def collect_project(main_window) -> dict:
    io  = main_window.io_panel
    st  = main_window.settings_panel
    qp  = main_window.queue_panel

    # Mirror the live scrubber in/out into the active snapshot before saving so
    # the persisted snapshot reflects the user's current marker positions.
    snaps = main_window.snapshots
    active = snaps.active()
    active.in_point  = main_window.viewer.scrubber.inPoint()
    active.out_point = main_window.viewer.scrubber.outPoint()
    snap_data = snaps.to_dict()

    return {
        "input": {
            "directory": io.get_in_dir(),
            "format":    io.in_fmt.currentText(),
            "padding":   io.get_in_padding(),
            "start":     io.get_in_start(),
            "end":       io.get_in_end(),
        },
        "output": {
            "directory": io.out_dir.text().strip(),   # base dir without prefix subdir
            "format":    io.out_fmt.currentText(),
            "prefix":    io.out_prefix.text().strip(), # raw prefix without period
            "padding":   io.get_out_padding(),
        },
        "snapshots":          snap_data["snapshots"],
        "active_snapshot_id": snap_data["active_snapshot_id"],
        # Shared Frame/TC display mode (one of "Frame", "TC (metadata)", "TC (fps)").
        # Both the viewer's and curve editor's dropdowns mirror this value.
        "display_mode":       getattr(main_window, "_display_mode", "Frame"),
        "settings": {
            "model_name":     st.get_model_name(),
            "model":          st.get_model_name(),
            "use_fp16":       st.use_fp16.isChecked(),
            "use_tile":       st.use_tile.isChecked(),
            "preserve_alpha": st.preserve_alpha.isChecked(),
            "use_ensemble":   st.use_ensemble.isChecked(),
            "scale":          st.get_scale(),
            "scene_cut":      st.get_scene_cut(),
        },
        "queue": [
            {
                "job_id":         job.job_id,
                "name":           job.name,
                "in_dir":         job.in_dir,
                "in_prefix":      job.in_prefix,
                "in_padding":     job.in_padding,
                "in_ext":         job.in_ext,
                "in_start":       job.in_start,
                "in_end":         job.in_end,
                "out_dir":        job.out_dir,
                "out_prefix":     job.out_prefix,
                "out_padding":    job.out_padding,
                "out_ext":        job.out_ext,
                "out_start":      job.out_start,
                "curve":          job.curve.to_dict(),
                "model_name":     job.model_name,
                "scale":          job.scale,
                "status":         job.status.value,
                # Snapshot identity captured at queue time — frozen strings.
                "snapshot_name":  job.snapshot_name,
                "snapshot_color": job.snapshot_color,
            }
            for job in qp.jobs
        ],
    }


def _build_snapshot_collection(data: dict, io_in_start: int, io_in_end: int):
    """Build a SnapshotCollection from project data, applying a legacy shim
    when the file pre-dates the snapshots schema."""
    from core.snapshot import SnapshotCollection, Snapshot
    from core.timewarp import TimewarpCurve

    if "snapshots" in data:
        return SnapshotCollection.from_dict({
            "snapshots":          data["snapshots"],
            "active_snapshot_id": data.get("active_snapshot_id"),
        })

    # Legacy v1: synthesize one "default" snapshot from top-level curve/in/out.
    if "curve" in data:
        curve = TimewarpCurve.from_dict(data["curve"])
    else:
        curve = TimewarpCurve()
        curve.set_range(io_in_start, io_in_end, io_in_start)
        curve.reset(1.0)
    in_pt  = int(data.get("in_point",  io_in_start))
    out_pt = int(data.get("out_point", io_in_end))
    snap = Snapshot(curve=curve, in_point=in_pt, out_point=out_pt, name="default")
    return SnapshotCollection(snapshots=[snap], active_id=snap.id)


def apply_project(main_window, data: dict):
    io  = main_window.io_panel
    st  = main_window.settings_panel

    inp = data.get("input", {})
    out = data.get("output", {})
    stg = data.get("settings", {})

    # Block sequenceChanged while setting fields — fire once manually at end
    io.blockSignals(True)

    if inp.get("directory"):
        io.in_dir.setText(inp["directory"])
    fmt_idx = io.in_fmt.findText(inp.get("format", "EXR"))
    if fmt_idx >= 0: io.in_fmt.setCurrentIndex(fmt_idx)
    if "padding" in inp: io.in_padding.setValue(inp["padding"])

    if out.get("directory"):
        io.out_dir.setText(out["directory"])
    out_fmt_idx = io.out_fmt.findText(out.get("format", "EXR"))
    if out_fmt_idx >= 0: io.out_fmt.setCurrentIndex(out_fmt_idx)
    if "prefix"  in out: io.out_prefix.setText(out["prefix"])
    if "padding" in out: io.out_padding.setValue(out["padding"])

    io.blockSignals(False)
    # Manually trigger scan — scans disk and emits sequenceChanged once cleanly
    io._on_in_dir_changed()

    if "use_fp16"       in stg: st.use_fp16.setChecked(stg["use_fp16"])
    if "use_tile"       in stg: st.use_tile.setChecked(stg["use_tile"])
    if "preserve_alpha" in stg: st.preserve_alpha.setChecked(stg["preserve_alpha"])
    if "use_ensemble"   in stg: st.use_ensemble.setChecked(stg["use_ensemble"])
    if "scale"          in stg: st.scale_slider.setValue([0.25,0.5,1.0,2.0].index(stg["scale"]) if stg["scale"] in [0.25,0.5,1.0,2.0] else 2)
    if "scene_cut"      in stg: st.scene_cut_spin.setValue(stg["scene_cut"])
    mi = st.model_combo.findText(stg.get("model_name", stg.get("model", "")))
    if mi >= 0: st.model_combo.setCurrentIndex(mi)
    # Always clear custom model dir on load — never restore it
    st.model_dir.setText("")

    # Call _on_sequence_changed first so the range is set correctly
    main_window._on_sequence_changed()

    # Restore snapshots AFTER _on_sequence_changed so they're not reset.
    in_start_now = io.get_in_start()
    in_end_now   = io.get_in_end()

    snaps = _build_snapshot_collection(data, in_start_now, in_end_now)

    # Re-range each snapshot's curve to the current sequence when one is loaded,
    # otherwise keep the saved range. has_sequence() is the canonical check —
    # it avoids the old value-comparison sentinel (1001/1072) that collided
    # with genuine 1001-1072 sequences on disk.
    has_seq = main_window.io_panel.has_sequence()
    for snap in snaps.snapshots:
        if has_seq:
            c_in_start = in_start_now
            c_in_end   = in_end_now
        else:
            c_in_start = snap.curve.in_start
            c_in_end   = snap.curve.in_end
        snap.curve.set_range(c_in_start, c_in_end, c_in_start)

    main_window.set_snapshots(snaps)

    # Restore the shared display mode. Back-compat: pre-Brief files lack this
    # key and load as "Frame". set_display_mode dedupes — no signal emit when
    # the loaded mode matches what's already live.
    if hasattr(main_window, "set_display_mode"):
        main_window.set_display_mode(data.get("display_mode", "Frame"))
