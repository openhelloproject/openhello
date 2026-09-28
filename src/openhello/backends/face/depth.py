"""
3D face geometry for depth-capable cameras (e.g. Intel RealSense).

NOT IMPLEMENTED — no depth hardware has been available to build and test
against. The plan: capture aligned depth + IR frames (pyrealsense2), crop the
face with the same YuNet detector, project to a point cloud with the depth
intrinsics, and verify by ICP alignment (open3d) against the enrolled cloud,
thresholding on residual fit error. A flat photo or screen has no matching
depth surface, so this is a much stronger spoof rejection than 2D IR.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..base import CancelToken, Modality, ProgressCallback


class Depth3DFaceModality(Modality):
    name = "face"

    def __init__(self, node: str | None):
        self.node = node

    def is_available(self) -> bool:
        return False  # never offer a modality that can't enroll

    def is_enrolled(self, user: str) -> bool:
        return False

    def enroll(self, user: str, *, progress: ProgressCallback = lambda p: None,
               cancel: CancelToken | None = None,
               options: Mapping[str, Any] | None = None) -> bool:
        raise NotImplementedError(
            "depth face capture + point-cloud enrollment (see module docstring)")

    def verify(self, user: str, *, cancel: CancelToken | None = None) -> bool:
        raise NotImplementedError("point-cloud ICP verification (see module docstring)")

    def security_note(self) -> str:
        return ("3D depth geometry match (not implemented yet) — a flat photo or "
                "screen has no matching depth surface.")
