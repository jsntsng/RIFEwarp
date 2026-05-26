"""RIFE Retime Project (.rtp) — JSON save/load."""
from __future__ import annotations
import json

PROJECT_VERSION = 1


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
    ce  = main_window.curve_editor
    qp  = main_window.queue_panel

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
        "curve":     ce.curve.to_dict(),
        "in_point":  main_window.viewer.scrubber.inPoint(),
        "out_point": main_window.viewer.scrubber.outPoint(),
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
                "name":       job.name,
                "in_dir":     job.in_dir,
                "in_prefix":  job.in_prefix,
                "in_padding": job.in_padding,
                "in_ext":     job.in_ext,
                "in_start":   job.in_start,
                "in_end":     job.in_end,
                "out_dir":    job.out_dir,
                "out_prefix": job.out_prefix,
                "out_padding":job.out_padding,
                "out_ext":    job.out_ext,
                "out_start":  job.out_start,
                "curve":      job.curve.to_dict(),
                "model_name": job.model_name,
                "scale":      job.scale,
                "status":     job.status.value,
            }
            for job in qp.jobs
        ],
    }


def apply_project(main_window, data: dict):
    from core.timewarp import TimewarpCurve

    io  = main_window.io_panel
    st  = main_window.settings_panel
    ce  = main_window.curve_editor

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

    # Restore in/out points
    if "in_point"  in data: main_window.viewer.scrubber.setInPoint(data["in_point"])
    if "out_point" in data: main_window.viewer.scrubber.setOutPoint(data["out_point"])

    # Restore curve AFTER _on_sequence_changed so it doesn't get reset
    if "curve" in data:
        curve = TimewarpCurve.from_dict(data["curve"])
        in_start = io.get_in_start()
        in_end   = io.get_in_end()
        # Preserve saved range if sequence not found on disk
        if in_start == 1001 and in_end == 1072 and curve.in_start != 1001:
            in_start = curve.in_start
            in_end   = curve.in_end
        curve.set_range(in_start, in_end, in_start)
        # Set on both curve editor canvas and dope sheet — same object
        ce.canvas.curve = curve
        ce.canvas._selected_frames = set()
        ce._kp_label.setText(f"{len(curve.keypoints)} keys")
        ce._update_info_bar()
        ce.canvas.reset_view()
        ce.canvas.update()
        main_window.dope_sheet.set_curve(curve)
        main_window._on_curve_changed()
