#!/bin/sh
# Build the release tarball the distro packages consume:
#   packaging/make-tarball.sh [OUT_DIR]  ->  OUT_DIR/openhello-<version>.tar.gz
#
# The tarball includes the permissively licensed face models (YuNet: MIT,
# SFace: Apache-2.0) so distro builds never need network access. They're not
# kept in git; they're fetched here and verified against pinned SHA-256s.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=${1:-$ROOT/dist}
VERSION=$(sed -n 's/^__version__ = "\([0-9.]*\)"/\1/p' "$ROOT/src/openhello/__init__.py")
CACHE=${OPENHELLO_MODEL_CACHE:-$HOME/.cache/openhello/models}
ZOO=https://github.com/opencv/opencv_zoo/raw/main/models

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$OUT" "$CACHE" "$STAGE/models"

fetch() {  # fetch FILE URL SHA256
    if [ ! -f "$CACHE/$1" ] || ! echo "$3  $CACHE/$1" | sha256sum -c --status; then
        curl -fsSL -o "$CACHE/$1.part" "$2" && mv "$CACHE/$1.part" "$CACHE/$1"
    fi
    echo "$3  $CACHE/$1" | sha256sum -c --status || { echo "checksum mismatch: $1" >&2; exit 1; }
    cp "$CACHE/$1" "$STAGE/models/$1"
}
fetch face_detection_yunet_2023mar.onnx "$ZOO/face_detection_yunet/face_detection_yunet_2023mar.onnx" \
      8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4
fetch face_recognition_sface_2021dec.onnx "$ZOO/face_recognition_sface/face_recognition_sface_2021dec.onnx" \
      0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79
cp "$ROOT/packaging/models-LICENSES.md" "$STAGE/models/LICENSES.md"

tar -czf "$OUT/openhello-$VERSION.tar.gz" \
    --exclude='__pycache__' --exclude='*/build' --exclude='*.egg-info' \
    --transform "s,^\.,openhello-$VERSION," \
    -C "$ROOT" ./pyproject.toml ./README.md ./ARCHITECTURE.md ./LICENSE \
    ./src ./pam ./data ./docs ./tests ./tools ./packaging \
    -C "$STAGE" ./models
echo "$OUT/openhello-$VERSION.tar.gz"
