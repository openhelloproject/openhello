"""
Face detection + embedding for the IR backend.

Detector: YuNet (OpenCV zoo, MIT) — face box + 5 landmarks.
Embedders (pluggable, see ARCHITECTURE.md "Face embedding model"):
  - "sface":   OpenCV SFace (Apache-2.0). Default; OK to ship.
  - "arcface": InsightFace buffalo_l w600k_r50 (non-commercial license —
               never bundled; the user downloads it themselves).

Both produce L2-normalised embeddings compared by cosine similarity. They
are NOT interchangeable: templates record which model made them, and a
model change means re-enrolling.

Model files are looked up along core.paths.model_dirs(); models_installed()
tells the modality whether face can be offered at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import cv2
import numpy as np

from ....core.paths import find_model, model_dirs

YUNET_FILE = "face_detection_yunet_2023mar.onnx"
SFACE_FILE = "face_recognition_sface_2021dec.onnx"
ARCFACE_FILE = "w600k_r50.onnx"   # from InsightFace buffalo_l.zip

# InsightFace's canonical 112x112 landmark positions (subject's right eye,
# left eye, nose tip, right mouth corner, left mouth corner) — the same order
# YuNet emits.
ARCFACE_TEMPLATE = np.array([
    [38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
    [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)


EMBEDDER_FILES = {"sface": SFACE_FILE, "arcface": ARCFACE_FILE}


def _require(filename: str):
    path = find_model(filename)
    if path is None:
        raise FileNotFoundError(f"face model {filename} not found in "
                                f"{[str(d) for d in model_dirs()]}")
    return path


def default_model_name() -> str:
    return os.environ.get("OPENHELLO_FACE_MODEL", "sface")


def models_installed(embedder: str | None = None) -> bool:
    """Detector + the chosen embedder are present on disk."""
    embedder = embedder or default_model_name()
    return (find_model(YUNET_FILE) is not None
            and find_model(EMBEDDER_FILES.get(embedder, "")) is not None)


@dataclass
class Face:
    box: tuple[float, float, float, float]   # x, y, w, h
    landmarks: np.ndarray                    # 5x2
    score: float
    raw: np.ndarray                          # YuNet row, needed by SFace alignCrop


class FaceDetector:
    def __init__(self, score_threshold: float = 0.8):
        self._det = cv2.FaceDetectorYN.create(
            str(_require(YUNET_FILE)), "", (640, 360), score_threshold)

    def detect(self, gray: np.ndarray) -> Face | None:
        """Largest face in a greyscale frame, or None."""
        h, w = gray.shape
        self._det.setInputSize((w, h))
        _, faces = self._det.detect(to_bgr(normalize_ir(gray)))
        if faces is None or len(faces) == 0:
            return None
        row = max(faces, key=lambda f: f[2] * f[3])
        return Face(box=tuple(float(v) for v in row[:4]),
                    landmarks=row[4:14].reshape(5, 2).astype(np.float32),
                    score=float(row[14]), raw=row)


def normalize_ir(gray: np.ndarray) -> np.ndarray:
    """IR frames are dim (mean ~30); stretch contrast so the detector and
    embedders see a normally exposed face. CLAHE keeps local contrast
    without blowing out the emitter's specular highlights."""
    return cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray)


def to_bgr(gray: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def _l2(v: np.ndarray) -> np.ndarray:
    v = v.astype(np.float32).ravel()
    return v / (np.linalg.norm(v) + 1e-12)


class SFaceEmbedder:
    name = "sface"

    def __init__(self):
        self._rec = cv2.FaceRecognizerSF.create(str(_require(SFACE_FILE)), "")

    def embed(self, gray: np.ndarray, face: Face) -> np.ndarray:
        img = to_bgr(normalize_ir(gray))
        crop = self._rec.alignCrop(img, face.raw)
        return _l2(self._rec.feature(crop))


class ArcFaceEmbedder:
    name = "arcface"

    def __init__(self):
        import onnxruntime as ort
        path = find_model(ARCFACE_FILE)
        if path is None:
            raise FileNotFoundError(
                f"{ARCFACE_FILE} not found in {[str(d) for d in model_dirs()]}. ArcFace "
                "weights are licensed for non-commercial research use only and are not "
                "shipped; see ARCHITECTURE.md.")
        self._sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        self._input = self._sess.get_inputs()[0].name

    def embed(self, gray: np.ndarray, face: Face) -> np.ndarray:
        m, _ = cv2.estimateAffinePartial2D(face.landmarks, ARCFACE_TEMPLATE, method=cv2.LMEDS)
        crop = cv2.warpAffine(to_bgr(normalize_ir(gray)), m, (112, 112), borderValue=0)
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB).astype(np.float32)
        blob = ((rgb - 127.5) / 127.5).transpose(2, 0, 1)[None]
        return _l2(self._sess.run(None, {self._input: blob})[0])


EMBEDDERS = {"sface": SFaceEmbedder, "arcface": ArcFaceEmbedder}


def get_embedder(name: str | None = None):
    name = name or default_model_name()
    try:
        return EMBEDDERS[name]()
    except KeyError:
        raise ValueError(f"unknown face model {name!r}; choose from {sorted(EMBEDDERS)}") from None
