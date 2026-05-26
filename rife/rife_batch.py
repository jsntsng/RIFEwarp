"""
rife_batch.py — batch RIFE interpolation.
- Loads model ONCE per job
- Direct timestep inference (no bisectional search)
- FP16, tile processing, alpha preservation support
- Handles inconsistent source frame dimensions
"""
import os
import sys
import json
import argparse
import numpy as np
import cv2
import torch
from torch.nn import functional as F
import warnings
warnings.filterwarnings("ignore")

os.environ["OPENCV_IO_ENABLE_OPENEXR"]    = "1"
os.environ["PYTORCH_CUDA_ALLOC_CONF"]     = "expandable_segments:True"

torch.set_grad_enabled(False)
if torch.cuda.is_available():
    torch.backends.cudnn.enabled   = True
    torch.backends.cudnn.benchmark = True

parser = argparse.ArgumentParser()
parser.add_argument("--tasks",         required=True)
parser.add_argument("--model",         default="train_log")
parser.add_argument("--fp16",          action="store_true")
parser.add_argument("--tile",          action="store_true",
                    help="Tile processing for large frames / low VRAM")
parser.add_argument("--tile-size",     type=int, default=512)
parser.add_argument("--preserve-alpha",action="store_true")
parser.add_argument("--ensemble",       action="store_true",
                    help="Average forward/backward flow for cleaner results")
parser.add_argument("--scale",          type=float, default=1.0,
                    help="Optical flow scale: 0.25/0.5/1.0/2.0")
parser.add_argument("--scene-cut",      type=float, default=0.0,
                    help="Skip interpolation if frames differ more than this")
args = parser.parse_args()

# ── Load model ────────────────────────────────────────────────────────────────
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

import importlib.util

def _load_from_dir(model_dir, class_name, module_file):
    """Dynamically import Model class from a specific directory.
    Handles both 'from train_log.X import' and 'from model.X import' style imports
    by ensuring the parent directory is in sys.path and creating a package alias.
    """
    path = os.path.join(model_dir, module_file)
    if not os.path.exists(path):
        return None

    parent_dir = os.path.dirname(model_dir)
    folder_name = os.path.basename(model_dir)

    # The rife/model/ directory contains shared files like warplayer.py
    rife_model_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model")

    # Add all relevant dirs to sys.path
    for p in [model_dir, parent_dir, rife_model_dir]:
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)

    # Create package aliases so 'from train_log.X', 'from model.X'
    # both resolve correctly regardless of folder name
    import types
    for alias, search_path in [
        ("train_log", model_dir),
        ("model",     rife_model_dir if os.path.isdir(rife_model_dir) else model_dir),
        (folder_name, model_dir),
    ]:
        if alias not in sys.modules:
            pkg = types.ModuleType(alias)
            pkg.__path__ = [search_path]
            pkg.__package__ = alias
            sys.modules[alias] = pkg

    spec = importlib.util.spec_from_file_location(module_file[:-3], path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, class_name, None)

def load_model(model_dir):
    """Try each known architecture file in order of preference."""
    candidates = [
        "RIFE_HDv3.py",   # v3.x
        "RIFE_HDv2.py",   # v2.x
        "RIFE_HD.py",     # v1.x
        "RIFE.py",        # original
    ]
    for fname in candidates:
        ModelClass = _load_from_dir(model_dir, "Model", fname)
        if ModelClass is not None:
            try:
                m = ModelClass()
                m.load_model(model_dir, -1)
                print(f"Loaded model from {fname}", flush=True)
                return m
            except Exception as e:
                print(f"      {fname} failed: {e}", flush=True)
                continue
    raise RuntimeError(f"No compatible model architecture found in {model_dir}")

model = load_model(args.model)
model.eval()
model.device()

if args.fp16:
    try:
        # Convert all model parameters and buffers to fp16
        for module in [model.flownet]:
            module.half()
            for buf_name, buf in module.named_buffers():
                if buf.dtype == torch.float32:
                    module.register_buffer(buf_name, buf.half())
        print("FP16 enabled.", flush=True)
    except Exception as e:
        print(f"FP16 failed, falling back to FP32: {e}", flush=True)
        args.fp16 = False

print(f"Device: {device}", flush=True)

# ── Load tasks ────────────────────────────────────────────────────────────────
with open(args.tasks) as f:
    tasks = json.load(f)
print(f"Tasks: {len(tasks)}", flush=True)

# ── Image I/O ─────────────────────────────────────────────────────────────────
def read_img(path):
    is_exr = path.lower().endswith('.exr')
    if is_exr:
        img = cv2.imread(path, cv2.IMREAD_COLOR | cv2.IMREAD_ANYDEPTH)
    else:
        img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise FileNotFoundError(f"Cannot read: {path}")
    if not is_exr:
        if img.dtype == np.uint8:
            img = img.astype(np.float32) / 255.0
        elif img.dtype == np.uint16:
            img = img.astype(np.float32) / 65535.0
        else:
            img = img.astype(np.float32)
    else:
        img = img.astype(np.float32)
    if img.ndim == 2:
        img = np.stack([img, img, img], axis=2)
    return img  # HWC, float32, BGR

def img_to_tensor(img):
    t = torch.from_numpy(img[:,:,:3].transpose(2, 0, 1)).to(device).unsqueeze(0)
    if args.fp16:
        t = t.half()
    else:
        t = t.float()
    return t

def read_alpha(path):
    """Read alpha channel if present."""
    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if img is None or img.ndim < 3 or img.shape[2] < 4:
        return None
    alpha = img[:,:,3].astype(np.float32)
    if img.dtype == np.uint8:   alpha /= 255.0
    elif img.dtype == np.uint16: alpha /= 65535.0
    return alpha

def write_img(tensor, path, h, w, alpha=None):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    arr = tensor[0].float().cpu().numpy().transpose(1, 2, 0)[:h, :w]
    ext = os.path.splitext(path)[1].lower()
    if ext == ".exr":
        if alpha is not None and args.preserve_alpha:
            a = alpha[:h, :w, np.newaxis]
            out = np.concatenate([arr, a], axis=2)
            cv2.imwrite(path, out,
                        [cv2.IMWRITE_EXR_TYPE, cv2.IMWRITE_EXR_TYPE_HALF])
        else:
            cv2.imwrite(path, arr,
                        [cv2.IMWRITE_EXR_TYPE, cv2.IMWRITE_EXR_TYPE_HALF])
    elif ext in (".tif", ".tiff"):
        cv2.imwrite(path, (np.clip(arr, 0, 1)*65535).astype(np.uint16))
    else:
        cv2.imwrite(path, (np.clip(arr, 0, 1)*255).astype(np.uint8))

# ── Padding ───────────────────────────────────────────────────────────────────
def pad_pair(i0, i1):
    """Pad both tensors to identical dimensions, then pad to multiple of 32.
    Forces both frames to the exact same h/w before entering the model,
    which prevents internal tensor mismatches in models like 4.25.
    Preserves dtype for FP16 compatibility."""
    h0, w0 = i0.shape[2], i0.shape[3]
    h1, w1 = i1.shape[2], i1.shape[3]
    # Force both to the same size by padding the smaller one
    h = max(h0, h1)
    w = max(w0, w1)
    if h0 != h or w0 != w:
        i0 = F.pad(i0, (0, w - w0, 0, h - h0)).to(i0.dtype)
    if h1 != h or w1 != w:
        i1 = F.pad(i1, (0, w - w1, 0, h - h1)).to(i1.dtype)
    # Now pad both to multiple of 32
    ph = ((h - 1) // 64 + 1) * 64
    pw = ((w - 1) // 64 + 1) * 64
    i0p = F.pad(i0, (0, pw - w, 0, ph - h)).to(i0.dtype)
    i1p = F.pad(i1, (0, pw - w, 0, ph - h)).to(i1.dtype)
    return i0p, i1p, h0, w0

# ── Image cache ───────────────────────────────────────────────────────────────
_img_cache = {}

def get_img(path):
    if path not in _img_cache:
        _img_cache[path] = read_img(path)
        if len(_img_cache) > 4:
            del _img_cache[next(iter(_img_cache))]
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
    return _img_cache[path]

# ── Inference ─────────────────────────────────────────────────────────────────
def frame_diff(img0, img1):
    """Mean absolute difference between two BGR float32 images. Range 0-1."""
    return float(np.mean(np.abs(img0.astype(np.float32) - img1.astype(np.float32))))

def infer(i0_t, i1_t, ratio):
    """
    Direct timestep inference with graceful fallback.
    Try progressively simpler signatures until one works.
    """
    # Clear warplayer grid cache to prevent stale grids from mismatched frame sizes
    import sys
    for mod in list(sys.modules.values()):
        if hasattr(mod, 'backwarp_tenGrid') and isinstance(getattr(mod, 'backwarp_tenGrid'), dict):
            mod.backwarp_tenGrid.clear()
    with torch.no_grad():
        # Try full signature (scale + ensemble)
        try:
            return model.inference(i0_t, i1_t, ratio,
                                   scale=args.scale,
                                   ensemble=args.ensemble)
        except TypeError:
            pass
        # Try with scale only
        try:
            return model.inference(i0_t, i1_t, ratio, scale=args.scale)
        except TypeError:
            pass
        # Try ratio only
        try:
            return model.inference(i0_t, i1_t, ratio)
        except TypeError:
            pass
        # Bisectional fallback for models that don't support arbitrary timestep
        return _bisect(i0_t, i1_t, ratio)

def _bisect(i0, i1, ratio, threshold=0.02, max_cycles=8):
    if ratio <= threshold / 2:
        return i0
    if ratio >= 1.0 - threshold / 2:
        return i1
    t0, t1 = i0, i1
    r0, r1 = 0.0, 1.0
    with torch.no_grad():
        mid = model.inference(t0, t1)
    for _ in range(max_cycles):
        mr = (r0 + r1) / 2
        if abs(ratio - mr) <= threshold / 2:
            break
        if ratio > mr:
            t0 = mid; r0 = mr
        else:
            t1 = mid; r1 = mr
        with torch.no_grad():
            mid = model.inference(t0, t1)
    return mid

# ── Tile inference ────────────────────────────────────────────────────────────
def infer_tiled(i0_t, i1_t, ratio, tile_size, h, w):
    """Process frame in tiles for large resolutions / low VRAM."""
    ph, pw = i0_t.shape[2], i0_t.shape[3]
    out = torch.zeros_like(i0_t)
    for y in range(0, ph, tile_size):
        for x in range(0, pw, tile_size):
            ye = min(y + tile_size, ph)
            xe = min(x + tile_size, pw)
            t0 = i0_t[:, :, y:ye, x:xe]
            t1 = i1_t[:, :, y:ye, x:xe]
            out[:, :, y:ye, x:xe] = infer(t0, t1, ratio)
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
    return out

# ── Process all tasks ─────────────────────────────────────────────────────────
done = 0
for task in tasks:
    frame_num = task.get("frame", done + 1)
    try:
        img0 = get_img(task["img0"])
        img1 = get_img(task["img1"])
        i0_t = img_to_tensor(img0)
        i1_t = img_to_tensor(img1)
        i0_p, i1_p, h, w = pad_pair(i0_t, i1_t)

        ratio = float(task["ratio"])

        # Scene cut detection — skip interpolation if frames are too different
        if args.scene_cut > 0.0:
            diff = frame_diff(img0[:,:,:3], img1[:,:,:3])
            if diff > args.scene_cut:
                # Copy source frame instead of interpolating
                result = i0_p
                print(f"      SKIP f{frame_num} (scene cut, diff={diff:.3f})", flush=True)
            elif args.tile:
                result = infer_tiled(i0_p, i1_p, ratio, args.tile_size, h, w)
            else:
                result = infer(i0_p, i1_p, ratio)
        elif args.tile:
            result = infer_tiled(i0_p, i1_p, ratio, args.tile_size, h, w)
        else:
            result = infer(i0_p, i1_p, ratio)

        # Alpha from source frame
        alpha = read_alpha(task["img0"]) if args.preserve_alpha else None

        write_img(result, task["out"], h, w, alpha)

        del result, i0_t, i1_t, i0_p, i1_p
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        print(f"DONE {frame_num}", flush=True)

    except Exception as e:
        print(f"ERROR {frame_num}: {e}", flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)
    done += 1

print(f"FINISHED {done}", flush=True)
