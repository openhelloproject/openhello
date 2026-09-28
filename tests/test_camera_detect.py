"""
Tests for backends/face.py camera capability detection.

The synthetic cases pin down the heuristic; test_real_hardware runs the
actual ioctl probe on this machine and just reports/sanity-checks it.
"""
import os

import pytest

from openhello.backends.face.probe import (  # noqa: E402
    CameraCapability,
    VideoNode,
    list_video_nodes,
    probe_camera,
)


def node(path, *fmts):
    return VideoNode(path=path, card="test", bus_info="usb-test", formats=frozenset(fmts))


def test_luxvisions_ir_module():
    # Mirrors the real Luxvisions 30c9:00f4 (metadata nodes already filtered out).
    probe = probe_camera([node("/dev/video0", "MJPG", "YUYV"), node("/dev/video2", "GREY")])
    assert probe.capability is CameraCapability.IR_ONLY
    assert probe.node == "/dev/video2"


def test_rgb_with_mono_mode_is_not_ir():
    probe = probe_camera([node("/dev/video0", "MJPG", "YUYV", "GREY")])
    assert probe.capability is CameraCapability.NONE


def test_depth_wins_over_ir():
    probe = probe_camera([node("/dev/video0", "GREY"), node("/dev/video2", "Z16 ")])
    assert probe.capability is CameraCapability.DEPTH
    assert probe.node == "/dev/video2"


def test_rgb_only():
    assert probe_camera([node("/dev/video0", "MJPG", "YUYV")]).capability is CameraCapability.NONE


def test_no_nodes():
    assert probe_camera([]).capability is CameraCapability.NONE


@pytest.mark.skipif(not any(p.startswith("video") for p in os.listdir("/dev")), reason="no /dev/video*")
def test_real_hardware():
    nodes = list_video_nodes()
    for n in nodes:
        print(n)
    probe = probe_camera()
    print(probe)
    assert probe.capability in CameraCapability
