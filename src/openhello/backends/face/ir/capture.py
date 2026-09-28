"""
IR camera capture with emitter lit/dark frame pairing.

"Windows Hello" IR modules strobe the IR emitter on alternate frames
(docs/HARDWARE.md has measured levels). Pairing each lit frame with its
neighbouring dark frame lets us subtract
ambient IR (sunlight, lamps) and keep only the emitter's own reflection —
the signal liveness analysis works on (see liveness.py).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import cv2
import numpy as np

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 640, 360
WARMUP_FRAMES = 20   # auto-exposure ramps over roughly this many frames
# A pair only counts as "emitter firing" if the emitter adds a clear amount
# of light on top of the ambient IR: lit - dark, not lit / dark. In daylight
# the dark frame is far from zero, and a bright surface near the camera can
# push the lit/dark ratio close to 1 while the emitter is plainly visible.
MIN_EMITTER_DELTA = 4.0


class EmitterNotActive(RuntimeError):
    """No lit/dark alternation seen — emitter off, or nothing in front."""


@dataclass
class FramePair:
    lit: np.ndarray    # uint8 HxW
    dark: np.ndarray   # uint8 HxW, the adjacent frame with the emitter off
    t: float

    @property
    def emitter_only(self) -> np.ndarray:
        """lit - dark, clipped: the emitter's reflection with ambient IR removed."""
        return cv2.subtract(self.lit, self.dark)


class IRCamera:
    def __init__(self, node: str):
        self.node = node
        self._cap = None

    def __enter__(self):
        cap = cv2.VideoCapture(self.node, cv2.CAP_V4L2)
        if not cap.isOpened():
            raise RuntimeError(f"cannot open IR camera {self.node}")
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"GREY"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
        cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
        self._cap = cap
        for _ in range(WARMUP_FRAMES):
            cap.read()
        return self

    def __exit__(self, *exc):
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def _read(self) -> np.ndarray:
        ok, frame = self._cap.read()
        if not ok:
            raise RuntimeError(f"read failed on {self.node}")
        return frame if frame.ndim == 2 else frame[:, :, 0]

    def pairs(self, timeout_s: float):
        """
        Yield FramePairs until timeout_s. Frames are classified relative to
        their neighbour rather than by a fixed threshold, so this survives
        auto-exposure changes. Raises EmitterNotActive if the whole window
        passes without a single valid pair.
        """
        deadline = time.monotonic() + timeout_s
        prev = self._read()
        yielded = False
        while time.monotonic() < deadline:
            cur = self._read()
            a, b = float(prev.mean()), float(cur.mean())
            lit, dark = (prev, cur) if a > b else (cur, prev)
            lm, dm = max(a, b), min(a, b)
            if lm - dm >= MIN_EMITTER_DELTA:
                yielded = True
                yield FramePair(lit=lit, dark=dark, t=time.monotonic())
                prev = self._read()   # start the next pair on a fresh frame
            else:
                prev = cur
        if not yielded:
            raise EmitterNotActive(
                f"no emitter lit/dark alternation on {self.node} within {timeout_s}s "
                "(emitter off, or no face close to the camera)"
            )
