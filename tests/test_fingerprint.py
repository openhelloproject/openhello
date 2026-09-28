"""Fingerprint backend logic that needs no sensor: the fprintd self-heal."""
import pytest
from gi.repository import GLib

from openhello.backends import fingerprint as fp_mod
from openhello.backends.base import ModalityError
from openhello.backends.fingerprint import FingerprintModality

WEDGED = ("GDBus.Error:net.reactivated.Fprint.Error.Internal: Open failed with error: "
          "The device has already been opened!")


class FakeBus:
    """Just enough of fprintd + systemd for Claim/RestartUnit."""

    def __init__(self, claim_errors):
        self.claim_errors = list(claim_errors)   # messages for successive Claims
        self.restarts = 0

    def call_sync(self, name, path, iface, method, args, reply_type, flags, timeout, cancel):
        if method == "RestartUnit":
            assert args.unpack() == ("fprintd.service", "replace")
            self.restarts += 1
            return None
        if method == "Claim" and self.claim_errors:
            msg = self.claim_errors.pop(0)
            if msg:
                raise GLib.Error(msg)
        if method == "GetDefaultDevice":
            return GLib.Variant("(o)", ("/net/reactivated/Fprint/Device/0",))
        if method == "GetDevices":
            return GLib.Variant("(ao)", (["/net/reactivated/Fprint/Device/0"],))
        return None


@pytest.fixture
def as_root(monkeypatch):
    monkeypatch.setattr(fp_mod.os, "geteuid", lambda: 0)
    monkeypatch.setattr(fp_mod, "_last_fprintd_restart", float("-inf"))


def test_wedged_fprintd_is_restarted_and_claim_retried(as_root):
    bus = FakeBus([WEDGED, None])
    fp = FingerprintModality(connection=bus)
    assert fp._claim("/net/reactivated/Fprint/Device/0", "alice")
    assert bus.restarts == 1


def test_restart_is_rate_limited(as_root):
    bus = FakeBus([WEDGED, WEDGED, WEDGED])
    fp = FingerprintModality(connection=bus)
    with pytest.raises(ModalityError):        # restarted once, still wedged
        fp._claim("/dev0", "alice")
    with pytest.raises(ModalityError):        # within a minute: no second restart
        fp._claim("/dev0", "alice")
    assert bus.restarts == 1


def test_never_restarts_when_not_root(monkeypatch):
    monkeypatch.setattr(fp_mod.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(fp_mod, "_last_fprintd_restart", float("-inf"))
    bus = FakeBus([WEDGED])
    with pytest.raises(ModalityError):
        FingerprintModality(connection=bus)._claim("/dev0", "alice")
    assert bus.restarts == 0


def test_other_claim_errors_are_not_healed(as_root):
    bus = FakeBus(["GDBus.Error:net.reactivated.Fprint.Error.AlreadyInUse: Device was already claimed"])
    with pytest.raises(ModalityError):
        FingerprintModality(connection=bus)._claim("/dev0", "alice")
    assert bus.restarts == 0   # e.g. the lock screen legitimately holds the sensor


def _scripted_session(monkeypatch, outcomes):
    bus = FakeBus([])
    fp = FingerprintModality(connection=bus)
    seq = list(outcomes)
    rounds = []

    def fake_round(device, signal, start, stop, timeout_s, on_status, cancel, requested_at=None):
        rounds.append(timeout_s)
        return seq.pop(0)
    monkeypatch.setattr(fp, "_run_until_done", fake_round)
    status = fp._session("/dev0", "alice", "VerifyStatus", start=("VerifyStart", None),
                         stop="VerifyStop", timeout_s=20, on_status=None, cancel=None,
                         attempts=fp_mod.VERIFY_ATTEMPTS)
    return status, rounds


def test_second_touch_can_match(monkeypatch):
    status, rounds = _scripted_session(monkeypatch, ["verify-no-match", "verify-match"])
    assert status == "verify-match" and len(rounds) == 2


def test_gives_up_after_three_misses(monkeypatch):
    status, rounds = _scripted_session(monkeypatch, ["verify-no-match"] * 5)
    assert status == "verify-no-match" and len(rounds) == 3
    assert rounds[1] <= rounds[0]   # later rounds only get the remaining budget


def test_cancel_is_not_retried(monkeypatch):
    from openhello.backends.base import Cancelled
    with pytest.raises(Cancelled):
        _scripted_session(monkeypatch, ["cancelled", "verify-match"])
