"""Filesystem locations and safe file writing."""

from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    """Root-owned state directory; overridable for tests and dev runs."""
    return Path(os.environ.get("OPENHELLO_STATE_DIR", "/var/lib/openhello"))


def user_dir(user: str) -> Path:
    """Per-user state directory. `user` must already be validated
    (see core.users.validate_username)."""
    return state_dir() / user


# Face model weights (ONNX), first match wins:
#   $OPENHELLO_MODEL_DIR          development override
#   /var/lib/openhello/models     admin-added models (e.g. ArcFace, which can't be shipped)
#   /usr/share/openhello/models   models shipped in the package (YuNet, SFace)
SYSTEM_MODEL_DIRS = (Path("/var/lib/openhello/models"), Path("/usr/share/openhello/models"))


def model_dirs() -> list[Path]:
    override = os.environ.get("OPENHELLO_MODEL_DIR")
    return [Path(override)] if override else list(SYSTEM_MODEL_DIRS)


def find_model(filename: str) -> Path | None:
    """Path of a model file from the search path, or None if not installed."""
    for d in model_dirs():
        if (d / filename).is_file():
            return d / filename
    return None


def write_private(path: Path, data: bytes) -> None:
    """Write a file that is 0600 from the moment it exists."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path
