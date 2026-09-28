"""
Camera capability detection: which face modality can this machine support?

    depth-capable (e.g. Intel RealSense, a UVC camera exposing a depth
    stream)  -> Depth3DFaceModality (3D face geometry)
    IR-only (the common "Windows Hello compatible" module: IR sensor + IR
    illuminator, no depth) -> IRFaceModality (IR reflectance)
    neither  -> face unavailable

Uses V4L2 ioctls directly (no v4l-utils dependency).
"""

from __future__ import annotations

import ctypes
import fcntl
import glob
import itertools
import logging
import os
import re
import struct
from dataclasses import dataclass
from enum import Enum

log = logging.getLogger(__name__)


class CameraCapability(Enum):
    DEPTH = "depth"
    IR_ONLY = "ir"
    NONE = "none"


# --------------------------------------------------------------------------
# Hardware detection
# --------------------------------------------------------------------------

# V4L2 fourccs, exactly as the kernel reports them (4 chars, space-padded).
# Depth: Z16 (16-bit depth), INZI (IR+depth interleaved), Y12I/Y8I (stereo
# IR pairs RealSense exposes alongside depth).
DEPTH_FOURCCS = frozenset({"Z16 ", "INZI", "Y12I", "Y8I "})
# Single-channel greyscale formats an IR sensor streams in.
GREY_FOURCCS = frozenset({"GREY", "Y10 ", "Y12 ", "Y16 "})

_V4L2_CAP_VIDEO_CAPTURE = 0x00000001
_V4L2_BUF_TYPE_VIDEO_CAPTURE = 1


@dataclass(frozen=True)
class VideoNode:
    path: str
    card: str
    bus_info: str
    formats: frozenset[str]  # fourccs of VIDEO_CAPTURE formats


@dataclass(frozen=True)
class CameraProbe:
    capability: CameraCapability
    node: str | None = None  # the depth or IR /dev/video* node, if any


def detect_camera_capability() -> CameraCapability:
    return probe_camera().capability


def probe_camera(nodes: list[VideoNode] | None = None) -> CameraProbe:
    """
    Best-effort hardware probe. Not a certified detection routine — treat
    this as a heuristic refined against real devices, not a guarantee.
    Checked in order of confidence:

      1. librealsense (pyrealsense2), if installed: purpose-built to
         enumerate Intel RealSense depth cameras.
      2. V4L2 format probing via ioctl (no v4l-utils dependency): a
         capture node advertising a depth fourcc (DEPTH_FOURCCS).
      3. IR: a capture node whose formats are *all* greyscale
         (GREY_FOURCCS). Requiring greyscale-only, rather than "offers
         GREY among others", avoids misclassifying RGB webcams that also
         expose a mono mode.

    Metadata nodes (UVC "UVCH"/"UVCM") aren't VIDEO_CAPTURE and are skipped.
    Tested cameras: docs/HARDWARE.md — extend it as more hardware is tested.
    """
    if nodes is None and _has_realsense_device():
        return CameraProbe(CameraCapability.DEPTH)

    if nodes is None:
        nodes = list_video_nodes()

    for n in nodes:
        if n.formats & DEPTH_FOURCCS:
            return CameraProbe(CameraCapability.DEPTH, n.path)

    for n in nodes:
        if n.formats and n.formats <= GREY_FOURCCS:
            return CameraProbe(CameraCapability.IR_ONLY, n.path)

    return CameraProbe(CameraCapability.NONE)


def _has_realsense_device() -> bool:
    try:
        import pyrealsense2 as rs  # type: ignore
    except ImportError:
        return False
    try:
        ctx = rs.context()
        return len(ctx.query_devices()) > 0
    except Exception as e:
        log.debug("pyrealsense2 present but query failed: %s", e)
        return False


class _v4l2_capability(ctypes.Structure):
    _fields_ = [
        ("driver", ctypes.c_char * 16),
        ("card", ctypes.c_char * 32),
        ("bus_info", ctypes.c_char * 32),
        ("version", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint32),
        ("device_caps", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 3),
    ]


class _v4l2_fmtdesc(ctypes.Structure):
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("description", ctypes.c_char * 32),
        ("pixelformat", ctypes.c_uint32),
        ("mbus_code", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 3),
    ]


def _ioc(direction: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord("V") << 8) | nr


_VIDIOC_QUERYCAP = _ioc(2, 0, ctypes.sizeof(_v4l2_capability))   # _IOR
_VIDIOC_ENUM_FMT = _ioc(3, 2, ctypes.sizeof(_v4l2_fmtdesc))      # _IOWR


def _query_node(path: str) -> VideoNode | None:
    """QUERYCAP + ENUM_FMT on one node; None if it isn't a capture node."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError as e:
        log.debug("cannot open %s: %s", path, e)
        return None
    try:
        cap = _v4l2_capability()
        fcntl.ioctl(fd, _VIDIOC_QUERYCAP, cap)
        if not cap.device_caps & _V4L2_CAP_VIDEO_CAPTURE:
            return None  # e.g. UVC metadata node
        formats = set()
        for i in itertools.count():
            desc = _v4l2_fmtdesc(index=i, type=_V4L2_BUF_TYPE_VIDEO_CAPTURE)
            try:
                fcntl.ioctl(fd, _VIDIOC_ENUM_FMT, desc)
            except OSError:
                break
            formats.add(struct.pack("<I", desc.pixelformat).decode("ascii", "replace"))
        return VideoNode(
            path=path,
            card=cap.card.decode(errors="replace"),
            bus_info=cap.bus_info.decode(errors="replace"),
            formats=frozenset(formats),
        )
    except OSError as e:
        log.debug("V4L2 query failed on %s: %s", path, e)
        return None
    finally:
        os.close(fd)


def list_video_nodes() -> list[VideoNode]:
    nodes = []
    for path in sorted(glob.glob("/dev/video*"), key=lambda p: int(re.sub(r"\D", "", p) or 0)):
        node = _query_node(path)
        if node is not None:
            nodes.append(node)
    return nodes
