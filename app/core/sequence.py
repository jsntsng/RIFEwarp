"""Image sequence utilities."""
from __future__ import annotations
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

SUPPORTED_EXTS = {".exr", ".png", ".tif", ".tiff", ".dpx"}
FRAME_RE = re.compile(r"^(.*?)(\d+)(\.[a-zA-Z]+)$")


@dataclass
class SequenceInfo:
    directory: str
    prefix: str
    padding: int
    extension: str
    first_frame: int
    last_frame: int
    missing: List[int]

    @property
    def frame_count(self) -> int:
        return self.last_frame - self.first_frame + 1

    @property
    def pattern(self) -> str:
        return os.path.join(self.directory, f"{self.prefix}%0{self.padding}d{self.extension}")

    def frame_path(self, frame: int) -> str:
        return os.path.join(self.directory, f"{self.prefix}{frame:0{self.padding}d}{self.extension}")

    def __str__(self):
        missing_str = f"  [{len(self.missing)} missing]" if self.missing else ""
        return f"{self.pattern}  [{self.first_frame}-{self.last_frame}]  {self.frame_count}f{missing_str}"


def scan_sequence(directory: str) -> Optional[SequenceInfo]:
    if not os.path.isdir(directory):
        return None
    candidates: dict[Tuple[str, str, int], List[int]] = {}
    for fname in os.listdir(directory):
        m = FRAME_RE.match(fname)
        if not m:
            continue
        prefix, numstr, ext = m.group(1), m.group(2), m.group(3)
        if ext.lower() not in SUPPORTED_EXTS:
            continue
        key = (prefix, ext.lower(), len(numstr))
        candidates.setdefault(key, []).append(int(numstr))
    if not candidates:
        return None
    best_key = max(candidates, key=lambda k: len(candidates[k]))
    prefix, ext, padding = best_key
    frames = sorted(candidates[best_key])
    first, last = frames[0], frames[-1]
    missing = sorted(set(range(first, last + 1)) - set(frames))
    return SequenceInfo(directory=directory, prefix=prefix, padding=padding,
                        extension=ext, first_frame=first, last_frame=last, missing=missing)
