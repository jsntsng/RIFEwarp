# RIFEwarp

A desktop timewarp tool for VFX production. RIFEwarp retimes image sequences
using RIFE optical-flow frame interpolation, with a curve-based editor for
animating retime speed, snapshot versioning, and timecode-aware playback.

## Features

- Curve-driven timewarp editing with real-time preview
- RIFE optical-flow interpolation for generated in-between frames
- EXR and DPX sequence support via OpenImageIO, including SMPTE timecode
  metadata (burn-in display for frame / metadata TC / fps TC)
- Snapshot system for versioning retime curves within a project
- Render queue for batch processing
- Full-sequence RAM caching for interactive playback
- Project files (`.rtp`) for saving and restoring sessions

## Requirements

- Linux (developed on Rocky Linux 9 / Nobara)
- NVIDIA GPU with CUDA support
- Python 3.11
- OpenImageIO 3.1.x

## Installation

```
git clone https://github.com/jsntsng/RIFEwarp.git
cd RIFEwarp
./setup.sh
```

`setup.sh` creates a Python virtual environment, installs dependencies
(PyTorch, OpenImageIO, OpenCV, PyQt6), and downloads RIFE model weights.

## Model notes

RIFE **4.18** and **4.22** are the recommended models for live-action footage
with complex motion. Versions 4.25/4.26 are available but can produce
artifacts on some sequences.

## Acknowledgments

Frame interpolation is powered by [RIFE](https://github.com/hzwer/ECCV2022-RIFE)
(Real-Time Intermediate Flow Estimation), © Megvii Inc., MIT License.
Model weights are downloaded at install time from the
[Practical-RIFE](https://github.com/hzwer/Practical-RIFE) project and are not
distributed with this repository.

## License

MIT — see [LICENSE](LICENSE).
