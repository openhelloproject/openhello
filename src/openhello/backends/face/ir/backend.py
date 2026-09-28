"""
IR face modality for the common "Windows Hello compatible" module (IR sensor +
IR illuminator, no depth).

Pipeline: capture emitter lit/dark frame pairs (capture.py) -> YuNet face
detection -> SFace/ArcFace embedding (models.py) -> cosine match against the
enrolled templates -> IR reflectance liveness on the lit-minus-dark image
(liveness.py).

STATUS: capture, detection, embedding, enrollment and matching are verified
on hardware. Liveness is PROVISIONAL (liveness.py): screens and inkjet prints
are invisible to the IR camera (measured); every match must also pass a 3D
shading + eye glint + glare check calibrated on genuine frames only. IR-visible
prints (laser/photocopy) are not measured yet.

Heavy imports (OpenCV, onnxruntime) happen lazily so the daemon starts on
machines without them; the modality then just reports itself unavailable.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator, Mapping
from typing import Any

import numpy as np

from ....core.paths import ensure_private_dir, user_dir, write_private
from ...base import CancelToken, Modality, Progress, ProgressCallback

log = logging.getLogger(__name__)

TEMPLATE_FILE = "face.npz"


class IRFaceModality(Modality):
    name = "face"

    ENROLL_SAMPLES = 10
    ENROLL_TIMEOUT_S = 30         # room for normal head movement between samples
    VERIFY_TIMEOUT_S = 6
    SAMPLE_SPACING_S = 0.3        # spread samples over natural head motion
    MIN_DET_SCORE = 0.9
    MIN_FACE_WIDTH = 80           # px at 640x360; closer faces give more IR signal
    REQUIRED_HITS = 2             # matching lit frames needed; reduces false accepts
    # Provisional cosine thresholds, deliberately strict. No impostor scores
    # have been measured yet, so nothing supports the 1-in-100,000 false-
    # accept target (ARCHITECTURE.md). Measurements: docs/HARDWARE.md.
    THRESHOLDS = {"sface": 0.7, "arcface": 0.7}

    def __init__(self, node: str):
        self.node = node

    # -- Modality -----------------------------------------------------------
    def is_available(self) -> bool:
        try:
            import cv2  # noqa: F401
        except ImportError:
            log.warning("face unavailable: OpenCV (python3-opencv) not installed")
            return False
        from .models import models_installed
        if not models_installed():
            log.warning("face unavailable: face model files not installed")
            return False
        from ..probe import CameraCapability, probe_camera
        probe = probe_camera()
        return probe.capability is CameraCapability.IR_ONLY and probe.node == self.node

    @property
    def can_authenticate(self) -> bool:
        try:
            import cv2  # noqa: F401
        except ImportError:   # no OpenCV: can't authenticate
            return False
        return True           # enabled before liveness calibration: see module docstring

    def is_enrolled(self, user: str) -> bool:
        return self._template_path(user).exists()

    def enroll(self, user: str, *, progress: ProgressCallback = lambda p: None,
               cancel: CancelToken | None = None,
               options: Mapping[str, Any] | None = None) -> bool:
        from .models import get_embedder

        emb = get_embedder()
        samples: list[np.ndarray] = []
        last_t = 0.0
        progress(Progress("face-searching", 0, self.ENROLL_SAMPLES, "Looking for your face"))
        for pair, face in self._usable_frames(self.ENROLL_TIMEOUT_S, cancel):
            if pair.t - last_t < self.SAMPLE_SPACING_S:
                continue
            last_t = pair.t
            samples.append(emb.embed(pair.lit, face))
            progress(Progress("face-sample", len(samples), self.ENROLL_SAMPLES))
            if len(samples) >= self.ENROLL_SAMPLES:
                break
        if len(samples) < self.ENROLL_SAMPLES:
            log.info("face enroll for %s: only %d usable samples", user, len(samples))
            progress(Progress("face-timeout", len(samples), self.ENROLL_SAMPLES,
                              "Couldn't see your face clearly enough"))
            return False
        self._save_templates(user, emb.name, np.stack(samples))
        log.info("face enroll for %s: %d templates (%s)", user, len(samples), emb.name)
        return True

    def verify(self, user: str, *, cancel: CancelToken | None = None) -> bool:
        from .liveness import extract_features, is_calibrated, is_live
        from .models import get_embedder

        model, templates = self._load_templates(user)
        emb = get_embedder(model)
        threshold = self.THRESHOLDS[model]
        check_liveness = is_calibrated()
        hits, photo_rejects = 0, 0
        for pair, face in self._usable_frames(self.VERIFY_TIMEOUT_S, cancel):
            score = float(np.max(templates @ emb.embed(pair.lit, face)))
            if score < threshold:
                continue
            feats = extract_features(pair, face) if check_liveness else None
            if feats is not None and not is_live(feats):
                photo_rejects += 1
                if photo_rejects == 1:   # the numbers tune the provisional thresholds
                    log.info("face for %s matched (%.3f) but failed the photo check: "
                             "falloff %.3f glint %.3f saturation %.3f", user, score,
                             feats.center_falloff, feats.eye_glint, feats.saturation)
                continue
            hits += 1
            if hits >= self.REQUIRED_HITS:
                if feats is None:
                    log.warning("face match for %s accepted WITHOUT photo check "
                                "(liveness not calibrated), score %.3f", user, score)
                else:
                    log.info("face match for %s: score %.3f, photo check falloff %.3f "
                             "glint %.3f", user, score, feats.center_falloff, feats.eye_glint)
                return True
        return False

    def remove(self, user: str) -> None:
        self._template_path(user).unlink(missing_ok=True)

    def security_note(self) -> str:
        return ("IR face recognition with a provisional photo check. Phone screens "
                "and inkjet prints can't fool it (the IR camera can't see them); "
                "laser prints and photocopies aren't tested yet. Better than plain "
                "webcam face recognition, but not as "
                "strong as 3D depth sensing. See docs/SECURITY.md.")

    # -- internals ----------------------------------------------------------
    def _usable_frames(self, timeout_s: float, cancel: CancelToken | None) -> Iterator:
        """Yield (FramePair, Face) for lit frames with a good-quality face."""
        from .capture import IRCamera
        from .models import FaceDetector

        det = FaceDetector()
        with IRCamera(self.node) as cam:
            for pair in cam.pairs(timeout_s):
                if cancel is not None:
                    cancel.check()
                face = det.detect(pair.lit)
                if face is not None and face.score >= self.MIN_DET_SCORE \
                        and face.box[2] >= self.MIN_FACE_WIDTH:
                    yield pair, face

    @staticmethod
    def _template_path(user: str):
        return user_dir(user) / TEMPLATE_FILE

    def _save_templates(self, user: str, model: str, embeddings: np.ndarray) -> None:
        ensure_private_dir(user_dir(user))
        buf = io.BytesIO()
        np.savez(buf, model=np.array(model), embeddings=embeddings.astype(np.float32))
        write_private(self._template_path(user), buf.getvalue())

    def _load_templates(self, user: str) -> tuple[str, np.ndarray]:
        with np.load(self._template_path(user)) as z:
            return str(z["model"]), z["embeddings"]
