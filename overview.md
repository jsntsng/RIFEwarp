# RIFEwarp Roadmap

## Current: v1.1.3
- Fix: default curve span now tracks the loaded sequence length (TimewarpCurve.is_untouched_default / reset_to_identity)

## Next
1. Tangent handles in the curve editor. Data model exists in core/timewarp.py; UI never built. Additive so existing .rtp projects are unaffected.
2. Snapshot pinning / working set. Pinned snapshots float above a divider; persisted as pinned: bool on Snapshot in .rtp. Active = one, pinned = many. Pins drive ghost-compare overlay and a future render-subset. Open: pin via icon vs right-click vs both; drag-reorder within sections only.
3. Snapshot lock/unlock. Protects versions from curve edits, delete, rename. Locked snapshots can still be active for viewing. Duplicate of a locked snapshot is unlocked. Persisted as locked: bool. Open: exact scope; behavior when editing an active+locked curve.
4. Edit queued jobs in place. Change model/snapshot/settings on READY jobs without re-queueing. Snapshot edit re-stamps, does not re-link. RUNNING jobs not editable. Open: editable field set; rules for DONE/ERROR rows.
5. Windows port. Audit pass first (paths, shell calls, multiprocessing, subprocess, file I/O), then distribution (conda/constructor), then platform-neutral fixes on Linux before VM work. Open: audience scope; branch strategy.
