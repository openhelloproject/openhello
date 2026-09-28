"""IR face backend logic that doesn't need the camera: frame pairing and the
guarantee that face auth can't succeed while liveness is uncalibrated."""
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("cv2")

from openhello.backends.face.ir import liveness, models  # noqa: E402
from openhello.backends.face.ir.backend import IRFaceModality  # noqa: E402
from openhello.backends.face.ir.capture import EmitterNotActive, IRCamera  # noqa: E402
from openhello.backends.face.ir.liveness import LivenessFeatures  # noqa: E402


def frame(v):
    return np.full((360, 640), v, dtype=np.uint8)


def fake_camera(values):
    cam = IRCamera("/dev/null")
    it = iter(values)
    cam._read = lambda: frame(next(it))
    return cam


def test_pairs_alternating_either_phase():
    for seq in ([30, 0, 30, 0, 30, 0, 30, 0], [0, 30, 0, 30, 0, 30, 0, 30]):
        pairs = list(fake_camera(seq + [30, 0] * 1000).pairs(0.05))
        assert pairs, seq
        assert all(p.lit.mean() == 30 and p.dark.mean() == 0 for p in pairs)


def test_pairs_follow_exposure_changes():
    seq = [30, 1, 12, 1, 60, 2] + [40, 2] * 1000
    pairs = list(fake_camera(seq).pairs(0.05))
    assert [int(p.lit.mean()) for p in pairs[:3]] == [30, 12, 60]


def test_no_alternation_raises():
    with pytest.raises(EmitterNotActive):
        list(fake_camera([20] * 100000).pairs(0.05))   # constant: emitter off
    with pytest.raises(EmitterNotActive):
        list(fake_camera([1, 0] * 100000).pairs(0.05))  # nothing close enough


class ScoreEmbedder:
    """Embeds every frame so that its cosine score against the template is `score`."""
    name = "sface"

    def __init__(self, score):
        self.score = score

    def embed(self, gray, face):
        return np.array([self.score, np.sqrt(1 - self.score ** 2)], dtype=np.float32)


def face_modality(monkeypatch, score, frames=5, calibrated=False, live=True):
    m = IRFaceModality("/dev/null")
    monkeypatch.setattr(m, "_load_templates",
                        lambda user: ("sface", np.array([[1.0, 0.0]], np.float32)))
    frame = SimpleNamespace(lit=None)   # the stubbed embedder ignores pixels
    monkeypatch.setattr(m, "_usable_frames", lambda t, c: iter([(frame, None)] * frames))
    monkeypatch.setattr(models, "get_embedder", lambda name=None: ScoreEmbedder(score))
    monkeypatch.setattr(liveness, "is_calibrated", lambda: calibrated)
    monkeypatch.setattr(liveness, "is_live", lambda feats: live)
    monkeypatch.setattr(liveness, "extract_features",
                        lambda pair, face: LivenessFeatures(1, 1.3, 2, 0.02, 0, 116))
    return m


def test_face_signs_in_before_liveness_calibration(monkeypatch):
    """Face sign-in works even when no photo check is configured (see
    ARCHITECTURE.md, "Face sign-in before liveness")."""
    assert face_modality(monkeypatch, score=0.95).verify("alice") is True
    assert IRFaceModality("/dev/null").can_authenticate is True


def test_below_threshold_never_matches(monkeypatch):
    assert face_modality(monkeypatch, score=0.65).verify("alice") is False


def test_needs_two_matching_frames(monkeypatch):
    assert face_modality(monkeypatch, score=0.95, frames=1).verify("alice") is False


def test_calibrated_liveness_gates_every_match(monkeypatch):
    """Once is_calibrated() is true, a failed liveness check blocks even a
    perfect match — the hook that makes the calibration work take effect."""
    assert face_modality(monkeypatch, score=0.99, calibrated=True, live=False).verify("a") is False
    assert face_modality(monkeypatch, score=0.99, calibrated=True, live=True).verify("a") is True


def test_pairs_work_in_daylight():
    """In daylight the dark frame is far from zero and lit/dark can drop
    below 3x; pairing must use the emitter's own contribution (lit - dark)."""
    seq = [12, 30, 11, 28, 12, 29] + [12, 30] * 1000   # ratio ~2.5x, delta ~18
    pairs = list(fake_camera(seq).pairs(0.05))
    assert pairs and all(p.lit.mean() > p.dark.mean() for p in pairs)



# -- provisional photo check (liveness.is_live) ------------------------------
# Extremes of the genuine calibration set in docs/HARDWARE.md. Every genuine
# frame must pass; flat, glint-free or glare-clipped faces must fail.
GENUINE_EXTREMES = [
    LivenessFeatures(1.0, 1.189, 1.701, 0.021, 0.0, 105),   # lowest falloff (dark room)
    LivenessFeatures(0.992, 1.251, 1.415, 0.013, 0.0, 142),  # lowest glint (daylight)
    LivenessFeatures(0.992, 1.358, 2.524, 0.014, 0.0, 144),  # highest values
]


@pytest.mark.parametrize("feats", GENUINE_EXTREMES)
def test_every_measured_genuine_frame_passes(feats):
    assert liveness.is_live(feats) is True


@pytest.mark.parametrize("change", [
    {"center_falloff": 1.0},    # flat: no 3D shading
    {"eye_glint": 1.0},         # no corneal reflection
    {"saturation": 0.10},       # glossy glare clipping
])
def test_flat_glintless_or_glare_faces_fail(change):
    base = GENUINE_EXTREMES[0].as_dict()
    assert liveness.is_live(LivenessFeatures(**{**base, **change})) is False


def test_photo_check_is_active():
    assert liveness.is_calibrated() is True
