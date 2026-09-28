"""Face model lookup order and 'no models -> face unavailable, no crash'."""
import pytest

from openhello.core import paths


def test_override_wins(tmp_path, monkeypatch):
    (tmp_path / "m.onnx").write_bytes(b"x")
    monkeypatch.setenv("OPENHELLO_MODEL_DIR", str(tmp_path))
    assert paths.find_model("m.onnx") == tmp_path / "m.onnx"
    assert paths.find_model("missing.onnx") is None


def test_admin_dir_before_packaged_dir(tmp_path, monkeypatch):
    admin, packaged = tmp_path / "var", tmp_path / "usr"
    admin.mkdir()
    packaged.mkdir()
    (packaged / "a.onnx").write_bytes(b"shipped")
    (packaged / "b.onnx").write_bytes(b"shipped")
    (admin / "a.onnx").write_bytes(b"admin")
    monkeypatch.delenv("OPENHELLO_MODEL_DIR", raising=False)
    monkeypatch.setattr(paths, "SYSTEM_MODEL_DIRS", (admin, packaged))
    assert paths.find_model("a.onnx").read_bytes() == b"admin"
    assert paths.find_model("b.onnx").read_bytes() == b"shipped"


def test_face_unavailable_without_models(tmp_path, monkeypatch):
    pytest.importorskip("cv2")
    from openhello.backends.face.ir.backend import IRFaceModality
    from openhello.backends.face.ir.models import models_installed
    monkeypatch.setenv("OPENHELLO_MODEL_DIR", str(tmp_path))   # empty dir
    assert models_installed() is False
    assert IRFaceModality("/dev/video2").is_available() is False
