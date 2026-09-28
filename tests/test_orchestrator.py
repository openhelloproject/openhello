"""Orchestrator logic: concurrent sign-in, sign-in choice, record migration.
Uses the keystore's plaintext dev mode — TPM sealing has its own swtpm tests."""
import json
import threading
import time

import pytest

from openhello.backends.base import Cancelled, Modality
from openhello.daemon.orchestrator import OpenHelloError, Orchestrator
from openhello.tpm.keystore import TPMKeystore


class Fake(Modality):
    def __init__(self, name, *, match=True, delay=0.0, block=False, can_auth=True):
        self.name, self.match, self.delay, self.block = name, match, delay, block
        self._can_auth = can_auth
        self.verify_calls = 0
        self.cancelled = threading.Event()

    @property
    def can_authenticate(self):
        return self._can_auth

    def is_available(self):
        return True

    def is_enrolled(self, user):
        return True

    def enroll(self, user, *, progress=lambda p: None, cancel=None, options=None):
        return True

    def verify(self, user, *, cancel=None):
        self.verify_calls += 1
        deadline = time.monotonic() + (60 if self.block else self.delay)
        while time.monotonic() < deadline:
            if cancel is not None and cancel.cancelled:
                self.cancelled.set()
                raise Cancelled()
            time.sleep(0.01)
        return self.match


@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENHELLO_STATE_DIR", str(tmp_path))

    def build(**mods):
        orch = Orchestrator(TPMKeystore(dev_mode=True), mods)
        for name in mods:
            orch.enroll("alice", name)
        return orch
    return build


def test_first_match_wins_and_cancels_the_rest(make):
    fp = Fake("fingerprint", block=True)          # user never touches the sensor
    face = Fake("face", delay=0.1)                # ...but looks at the camera
    orch = make(fingerprint=fp, face=face)
    started = time.monotonic()
    result = orch.authenticate("alice", b"\1" * 32, "sudo")
    assert result.ok and len(result.signature) == 64
    assert time.monotonic() - started < 5         # didn't wait for the fingerprint
    assert fp.cancelled.wait(2), "fingerprint scan wasn't cancelled"


def test_methods_run_concurrently_not_one_after_another(make):
    fp = Fake("fingerprint", match=False, delay=0.5)
    face = Fake("face", match=False, delay=0.5)
    orch = make(fingerprint=fp, face=face)
    started = time.monotonic()
    assert orch.authenticate("alice", b"\1" * 32, "sudo").reason == "no-match"
    assert time.monotonic() - started < 0.9


def test_method_that_cannot_authenticate_is_never_scanned(make):
    face = Fake("face", can_auth=False)
    orch = make(face=face)
    result = orch.authenticate("alice", b"\1" * 32, "sudo")
    assert (result.ok, result.reason) == (False, "unavailable: no usable sign-in method")
    assert face.verify_calls == 0
    assert orch.auth_methods("alice") == []


def test_sign_in_choice(make):
    fp, face = Fake("fingerprint"), Fake("face")
    orch = make(fingerprint=fp, face=face)
    assert orch.sign_in_methods("alice") == (["fingerprint", "face"], ["fingerprint", "face"])
    orch.set_sign_in("alice", ["face"])
    assert orch.auth_methods("alice") == ["face"]
    assert orch.authenticate("alice", b"\1" * 32, "sudo").ok
    assert fp.verify_calls == 0
    orch.set_sign_in("alice", [])
    assert orch.authenticate("alice", b"\1" * 32, "sudo").reason.startswith("unavailable")
    with pytest.raises(OpenHelloError) as e:
        orch.set_sign_in("alice", ["iris"])
    assert e.value.code == "not-enrolled"


def test_removing_a_method_also_drops_it_from_sign_in(make):
    orch = make(fingerprint=Fake("fingerprint"), face=Fake("face"))
    orch.remove("alice", "fingerprint")
    assert orch.sign_in_methods("alice")[0] == ["face"]


def test_v1_record_means_everything_enrolled_is_on(make, tmp_path):
    orch = make(fingerprint=Fake("fingerprint"), face=Fake("face"))
    (tmp_path / "alice" / "enrollment.json").write_text(
        json.dumps({"version": 1, "modalities": ["fingerprint", "face"]}))
    assert orch.auth_methods("alice") == ["fingerprint", "face"]


def test_not_enrolled_is_still_reported_as_such(make):
    orch = make(fingerprint=Fake("fingerprint"))
    assert orch.authenticate("bob", b"\1" * 32, "sudo").reason == "not-enrolled"


class SpyKeystore(TPMKeystore):
    def __init__(self):
        super().__init__(dev_mode=True)
        self.log = []

    def unseal(self, cred):
        self.log.append("unseal")
        return super().unseal(cred)

    def finish_unseal(self, prepared, **kw):
        self.log.append("finish")
        return super().finish_unseal(prepared, **kw)

    def abort_unseal(self, prepared):
        self.log.append("abort")
        super().abort_unseal(prepared)


def test_match_uses_the_unseal_prepared_during_the_scan(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENHELLO_STATE_DIR", str(tmp_path))
    ks = SpyKeystore()
    orch = Orchestrator(ks, {"fingerprint": Fake("fingerprint", delay=0.1)})
    orch.enroll("alice", "fingerprint")
    ks.log.clear()
    assert orch.authenticate("alice", b"\1" * 32, "sudo").ok
    assert ks.log == ["finish"]


def test_no_match_discards_the_prepared_unseal(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENHELLO_STATE_DIR", str(tmp_path))
    ks = SpyKeystore()
    orch = Orchestrator(ks, {"fingerprint": Fake("fingerprint", match=False)})
    orch.enroll("alice", "fingerprint")
    ks.log.clear()
    assert orch.authenticate("alice", b"\1" * 32, "sudo").reason == "no-match"
    deadline = time.monotonic() + 2
    while "abort" not in ks.log and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ks.log == ["abort"]
