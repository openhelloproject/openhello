"""
IR reflectance liveness — the part Howdy skips (see docs/SECURITY.md).

Features are computed on the emitter-only image (lit - dark, see
capture.FramePair) within the detected face, so ambient IR can't fake them:

  emitter_fraction  share of the face's brightness that comes from the
                    emitter. Phone/laptop screens emit visible light but
                    almost no near-IR, so a replayed face on a screen barely
                    responds to the emitter.
  center_falloff    nose-region vs cheek/edge brightness in the emitter
                    image. The emitter is a point source ~at the camera, so a
                    real (curved, closer-in-the-middle) face is brightest at
                    the nose and falls off towards the edges; a flat print
                    falls off far less.
  eye_glint         brightest pixel in the eye regions vs face median. A live
                    cornea mirrors the emitter as a small sharp glint; paper
                    doesn't.
  texture           Laplacian energy relative to brightness (paper and
                    screens have different micro-texture from skin under IR).
  saturation        fraction of clipped pixels (glossy prints tend to clip).

STATUS: PROVISIONAL. Phone screens and dye-ink (inkjet) prints are invisible
in near-IR, so no face is detected and they never reach this check. No
IR-visible spoof (laser print / photocopy: carbon toner absorbs IR) has been
measured yet, so the thresholds below sit with a margin *below the genuine
minimum* rather than between genuine and spoof distributions. They reject
flat, glint-free or glare-clipped faces; a carefully made print could still
imitate shading and catchlights. Calibration data: docs/HARDWARE.md.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import cv2
import numpy as np

from .capture import FramePair
from .models import Face


@dataclass
class LivenessFeatures:
    emitter_fraction: float
    center_falloff: float
    eye_glint: float
    texture: float
    saturation: float
    face_width: float

    def as_dict(self) -> dict:
        return asdict(self)


def _patch(img: np.ndarray, cx: float, cy: float, r: float) -> np.ndarray:
    h, w = img.shape
    x0, x1 = int(max(cx - r, 0)), int(min(cx + r, w))
    y0, y1 = int(max(cy - r, 0)), int(min(cy + r, h))
    return img[y0:y1, x0:x1]


def extract_features(pair: FramePair, face: Face) -> LivenessFeatures:
    x, y, w, h = face.box
    H, W = pair.lit.shape
    x0, y0 = int(max(x, 0)), int(max(y, 0))
    x1, y1 = int(min(x + w, W)), int(min(y + h, H))
    lit = pair.lit[y0:y1, x0:x1].astype(np.float32)
    emit = pair.emitter_only.astype(np.float32)
    emit_face = emit[y0:y1, x0:x1]

    lit_mean = float(lit.mean()) + 1e-6
    emitter_fraction = float(emit_face.mean()) / lit_mean

    re, le, nose, rm, lm = face.landmarks
    r = max(w * 0.08, 2.0)
    nose_v = float(_patch(emit, *nose, r).mean())
    # cheeks: halfway between each eye and the face edge, level with the nose
    cheek_l = _patch(emit, x + w * 0.12, nose[1], r).mean()
    cheek_r = _patch(emit, x + w * 0.88, nose[1], r).mean()
    center_falloff = nose_v / (float(cheek_l + cheek_r) / 2 + 1e-6)

    face_median = float(np.median(emit_face)) + 1e-6
    eye_r = max(w * 0.06, 2.0)
    glint = max(float(_patch(emit, *re, eye_r).max(initial=0)),
                float(_patch(emit, *le, eye_r).max(initial=0)))
    eye_glint = glint / face_median

    gray_face = pair.lit[y0:y1, x0:x1]
    texture = float(cv2.Laplacian(gray_face, cv2.CV_32F).var()) / (lit_mean ** 2)
    saturation = float((gray_face >= 250).mean())

    return LivenessFeatures(emitter_fraction, center_falloff, eye_glint,
                            texture, saturation, float(w))


# Provisional thresholds (see module docstring for the data behind them).
MIN_CENTER_FALLOFF = 1.10   # genuine minimum 1.19; a flat, evenly lit surface ~1.0
MIN_EYE_GLINT = 1.25        # genuine minimum 1.42; paper has no corneal reflection
MAX_SATURATION = 0.02       # genuine 0.0; glossy prints clip where they mirror the emitter


def is_calibrated() -> bool:
    """True when is_live() has thresholds based on measurements (provisional:
    genuine-side only so far — see module docstring)."""
    return True


def is_live(features: LivenessFeatures) -> bool:
    return (features.center_falloff >= MIN_CENTER_FALLOFF
            and features.eye_glint >= MIN_EYE_GLINT
            and features.saturation <= MAX_SATURATION)
