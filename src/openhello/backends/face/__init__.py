"""
Face modality, chosen by what the camera hardware supports (probe.py).
See ARCHITECTURE.md "Face modality: capability-detected, not fixed".
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from ..base import CancelToken, Modality, ProgressCallback
from .probe import CameraCapability, probe_camera

log = logging.getLogger(__name__)


class UnavailableFaceModality(Modality):
    """No usable camera: face is simply not offered; other modalities work."""

    name = "face"

    def is_available(self) -> bool:
        return False

    def is_enrolled(self, user: str) -> bool:
        return False

    def enroll(self, user: str, *, progress: ProgressCallback = lambda p: None,
               cancel: CancelToken | None = None,
               options: Mapping[str, Any] | None = None) -> bool:
        raise NotImplementedError("no supported face camera on this device")

    def verify(self, user: str, *, cancel: CancelToken | None = None) -> bool:
        raise NotImplementedError("no supported face camera on this device")


def get_face_modality() -> Modality:
    """Probe the hardware once and return the matching face modality."""
    probe = probe_camera()
    log.info("camera capability: %s (node=%s)", probe.capability.value, probe.node)
    if probe.capability is CameraCapability.DEPTH:
        from .depth import Depth3DFaceModality
        return Depth3DFaceModality(probe.node)
    if probe.capability is CameraCapability.IR_ONLY:
        from .ir.backend import IRFaceModality
        return IRFaceModality(probe.node)
    return UnavailableFaceModality()
