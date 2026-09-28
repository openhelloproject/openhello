#!/usr/bin/env python3
"""
Dev/calibration tool for the IR face backend. NOT an auth path — it scores
matches without any liveness gate, purely to collect calibration data.

  face_calibrate.py enroll
  face_calibrate.py capture --label genuine|print|screen|other-person [--seconds 8]
  face_calibrate.py report

Every model whose weights are present (SFace always, ArcFace if downloaded)
runs on the *same* frames, so both get calibrated from one session.
Templates go to $OPENHELLO_STATE_DIR/calib/<model>.npy (separate from the
backend's real face.npz). Captures append one JSON line per usable frame
(score per model + liveness features) to $OPENHELLO_STATE_DIR/calibration.jsonl,
and save a few face crops next to it for eyeballing. Use a dev state dir, e.g.:

  OPENHELLO_STATE_DIR=~/.cache/openhello/dev-state \\
  OPENHELLO_MODEL_DIR=~/.cache/openhello/models \\
  python3 tools/face_calibrate.py enroll
"""

import argparse
import getpass
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openhello.backends.face.ir.backend import IRFaceModality as B  # noqa: E402
from openhello.backends.face.ir.capture import IRCamera  # noqa: E402
from openhello.backends.face.ir.liveness import extract_features  # noqa: E402
from openhello.backends.face.ir.models import EMBEDDERS, FaceDetector  # noqa: E402
from openhello.backends.face.probe import CameraCapability, probe_camera  # noqa: E402
from openhello.core.paths import state_dir  # noqa: E402


def ir_node() -> str:
    probe = probe_camera()
    if probe.capability is not CameraCapability.IR_ONLY:
        sys.exit(f"no IR-only camera detected ({probe})")
    return probe.node


def embedders() -> dict:
    out = {}
    for name, cls in EMBEDDERS.items():
        try:
            out[name] = cls()
        except FileNotFoundError as e:
            print(f"  ({name} skipped: {e})")
    return out


def usable_frames(timeout_s):
    det = FaceDetector()
    with IRCamera(ir_node()) as cam:
        for pair in cam.pairs(timeout_s):
            face = det.detect(pair.lit)
            if face is not None and face.score >= B.MIN_DET_SCORE and face.box[2] >= B.MIN_FACE_WIDTH:
                yield pair, face


def calib_dir() -> Path:
    d = state_dir() / "calib"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cmd_enroll(args):
    embs = embedders()
    samples = {name: [] for name in embs}
    last_t = 0.0
    for pair, face in usable_frames(B.ENROLL_TIMEOUT_S):
        if pair.t - last_t < 0.3:
            continue
        last_t = pair.t
        for name, e in embs.items():
            samples[name].append(e.embed(pair.lit, face))
        n = len(next(iter(samples.values())))
        print(f"  sample {n}/{B.ENROLL_SAMPLES}", flush=True)
        if n >= B.ENROLL_SAMPLES:
            break
    for name, s in samples.items():
        np.save(calib_dir() / f"{name}.npy", np.stack(s))
    print(f"enrolled {len(next(iter(samples.values())))} samples for {sorted(samples)}")


def cmd_capture(args):
    import cv2
    embs = embedders()
    templates = {n: np.load(calib_dir() / f"{n}.npy") for n in embs if (calib_dir() / f"{n}.npy").exists()}
    out = state_dir() / "calibration.jsonl"
    crops = state_dir() / "crops"
    crops.mkdir(parents=True, exist_ok=True)
    n = 0
    with out.open("a") as f:
        for pair, face in usable_frames(args.seconds):
            n += 1
            scores = {f"score_{m}": float(np.max(t @ embs[m].embed(pair.lit, face)))
                      for m, t in templates.items()}
            feats = extract_features(pair, face)
            f.write(json.dumps({"label": args.label, "t": time.time(),
                                **scores, **feats.as_dict()}) + "\n")
            if n <= 3:
                x, y, w, h = (int(v) for v in face.box)
                pad = int(w * 0.2)
                crop = pair.lit[max(y - pad, 0):y + h + pad, max(x - pad, 0):x + w + pad]
                emit = pair.emitter_only[max(y - pad, 0):y + h + pad, max(x - pad, 0):x + w + pad]
                stamp = f"{args.label}-{int(time.time())}-{n}"
                cv2.imwrite(str(crops / f"{stamp}-lit.png"), cv2.normalize(crop, None, 0, 255, cv2.NORM_MINMAX))
                cv2.imwrite(str(crops / f"{stamp}-emit.png"), cv2.normalize(emit, None, 0, 255, cv2.NORM_MINMAX))
            print("  " + "  ".join(f"{k} {v:.3g}" for k, v in {**scores, **feats.as_dict()}.items()), flush=True)
    print(f"{n} usable frames for label {args.label!r} -> {out}")


def cmd_report(args):
    path = state_dir() / "calibration.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    keys = sorted({k for r in rows for k in r if k not in ("label", "t")})
    for label in sorted({r["label"] for r in rows}):
        sel = [r for r in rows if r["label"] == label]
        print(f"\n== {label}: {len(sel)} frames")
        for k in keys:
            v = np.array([r[k] for r in sel if k in r], dtype=float)
            if v.size == 0:
                continue
            print(f"  {k:18s} min {v.min():8.3g}  p10 {np.percentile(v,10):8.3g}  "
                  f"median {np.median(v):8.3g}  p90 {np.percentile(v,90):8.3g}  max {v.max():8.3g}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--user", default=getpass.getuser())
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("enroll")
    c = sub.add_parser("capture")
    c.add_argument("--label", required=True)
    c.add_argument("--seconds", type=float, default=8)
    sub.add_parser("report")
    args = p.parse_args()
    {"enroll": cmd_enroll, "capture": cmd_capture, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
