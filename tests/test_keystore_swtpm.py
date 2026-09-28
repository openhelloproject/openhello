"""
TPM keystore tests against a real software TPM (swtpm). Each test module
run spins up its own throwaway swtpm instance(s) in a temp dir — nothing
touches the machine's hardware TPM.

Run: python3 -m pytest tests/test_keystore_swtpm.py -v
"""
import re
import shutil

import pytest

tpm2_pytss = pytest.importorskip("tpm2_pytss")
if shutil.which("swtpm") is None:
    pytest.skip("swtpm not installed", allow_module_level=True)

from tpm2_pytss import (  # noqa: E402  # noqa: E402
    ESYS_TR,
    TPM2B_PRIVATE,
    TPM2B_PUBLIC,
    TSS2_Exception,
)

from conftest import SWTPM  # noqa: E402
from openhello.tpm.keystore import SealedCredential, TPMKeystore  # noqa: E402


def test_seal_unseal_roundtrip(swtpm, state_dir):
    ks = TPMKeystore(tcti=swtpm.tcti)
    cred = ks.generate_and_seal("alice")
    cred.save()
    secret = ks.unseal(SealedCredential.load("alice"))
    assert len(secret) == 32
    # Secret must not appear anywhere on disk.
    for f in state_dir.rglob("*"):
        if f.is_file():
            assert secret not in f.read_bytes(), f
            assert f.stat().st_mode & 0o077 == 0, f"{f} is group/world accessible"
    ks.close()


def test_unseal_survives_new_esapi_connection(swtpm):
    """SRK is recreated transiently each time — must be deterministic."""
    ks = TPMKeystore(tcti=swtpm.tcti)
    cred = ks.generate_and_seal("alice")
    ks.close()
    ks2 = TPMKeystore(tcti=swtpm.tcti)
    assert ks2.unseal(cred) == ks2.unseal(cred)
    ks2.close()


def test_distinct_users_get_distinct_secrets(swtpm):
    ks = TPMKeystore(tcti=swtpm.tcti)
    a = ks.unseal(ks.generate_and_seal("alice"))
    b = ks.unseal(ks.generate_and_seal("bob"))
    assert a != b
    ks.close()


def test_wrong_gate_auth_fails(swtpm, state_dir):
    ks = TPMKeystore(tcti=swtpm.tcti)
    cred = ks.generate_and_seal("alice")
    (state_dir / "gate.auth").write_bytes(b"\x00" * 32)
    with pytest.raises(TSS2_Exception):
        ks.unseal(cred)
    ks.close()


def test_blob_from_other_tpm_fails(swtpm, tmp_path):
    ks = TPMKeystore(tcti=swtpm.tcti)
    cred = ks.generate_and_seal("alice")
    ks.close()

    other = SWTPM(tmp_path / "other-tpm")
    try:
        ks_other = TPMKeystore(tcti=other.tcti)
        with pytest.raises(TSS2_Exception):
            ks_other.unseal(cred)  # SRK differs -> integrity check fails on load
        ks_other.close()
    finally:
        other.stop()


def test_tampered_private_blob_fails(swtpm):
    ks = TPMKeystore(tcti=swtpm.tcti)
    cred = ks.generate_and_seal("alice")
    priv = bytearray(cred.private_blob)
    priv[-1] ^= 0xFF
    cred.private_blob = bytes(priv)
    with pytest.raises(TSS2_Exception):
        ks.unseal(cred)
    ks.close()


def test_sealed_object_rejects_password_auth(swtpm):
    """No userWithAuth: the empty-password path must not unseal."""
    ks = TPMKeystore(tcti=swtpm.tcti)
    cred = ks.generate_and_seal("alice")
    esys = ks._esys
    srk = ks._srk_handle()   # reuse the cached SRK: swtpm has only 3 object slots
    obj = esys.load(srk, TPM2B_PRIVATE.unmarshal(cred.private_blob)[0],
                    TPM2B_PUBLIC.unmarshal(cred.public_blob)[0])
    try:
        with pytest.raises(TSS2_Exception):
            esys.unseal(obj, session1=ESYS_TR.PASSWORD)
    finally:
        ks._flush(obj)
    ks.close()


def test_pcr_bound_seal_breaks_when_pcr_changes(swtpm):
    """PCR-ready path: PCR 16 is the resettable debug PCR, safe to extend."""
    ks = TPMKeystore(tcti=swtpm.tcti, pcrs=[16])
    cred = ks.generate_and_seal("alice")
    assert cred.pcrs == [16]
    ks.unseal(cred)  # unchanged PCR -> works

    ks._esys.pcr_event(ESYS_TR.PCR16, b"simulated boot-chain change")
    with pytest.raises(TSS2_Exception):
        ks.unseal(cred)
    ks.close()


def test_pcr_list_persists_across_save_load(swtpm, state_dir):
    ks = TPMKeystore(tcti=swtpm.tcti, pcrs=[16])
    ks.generate_and_seal("alice").save()
    ks.close()   # swtpm has no resource manager: one keystore at a time
    # A keystore configured with *different* PCRs must still unseal old
    # blobs using the PCR list recorded alongside them.
    ks2 = TPMKeystore(tcti=swtpm.tcti, pcrs=[])
    assert len(ks2.unseal(SealedCredential.load("alice"))) == 32
    ks2.close()


def bus_bytes(swtpm_log) -> bytes:
    """Every TPM command/response byte swtpm logged (log level 20 hex-dumps
    each SWTPM_IO_Read/Write) — i.e. exactly what a bus interposer sees."""
    out = bytearray()
    for line in swtpm_log.read_text().splitlines():
        if re.fullmatch(r"\s*([0-9A-F]{2} )+[0-9A-F]{2}\s*|\s*([0-9A-F]{2} )+", line):
            out += bytes.fromhex(line)
    return bytes(out)


def test_secret_and_gate_auth_never_cross_bus_in_clear(swtpm, state_dir):
    ks = TPMKeystore(tcti=swtpm.tcti)
    cred = ks.generate_and_seal("alice")
    secret = ks.unseal(cred)
    ks.close()

    gate_auth = (state_dir / "gate.auth").read_bytes()
    wire = bus_bytes(swtpm.log)
    assert len(wire) > 1000, "swtpm log didn't capture TPM traffic"
    assert secret not in wire, "sealed secret crossed the TPM bus in cleartext"
    assert gate_auth not in wire, "gate authValue crossed the TPM bus in cleartext"


def test_bus_capture_would_catch_cleartext(swtpm):
    """Control for the test above: an unencrypted unseal DOES leak on the
    wire, so the capture is actually capable of catching a regression."""
    ks = TPMKeystore(tcti=swtpm.tcti)
    cred = ks.generate_and_seal("alice")
    srk = ks._srk_handle()
    gate = ks._gate_handle(srk)
    obj = ks._esys.load(srk, TPM2B_PRIVATE.unmarshal(cred.private_blob)[0],
                        TPM2B_PUBLIC.unmarshal(cred.public_blob)[0])
    policy = ks._salted_session(srk, tpm2_pytss.TPM2_SE.POLICY)  # no ENCRYPT
    ks._apply_policy(policy, srk, gate, [])
    secret = bytes(ks._esys.unseal(obj, session1=policy))
    ks._flush(policy, obj)
    ks.close()
    assert secret in bus_bytes(swtpm.log)


def test_srk_is_created_once_and_reused(swtpm):
    """The 2 s-per-unseal fix: the ECC primary is created once per connection."""
    ks = TPMKeystore(tcti=swtpm.tcti)
    calls = []
    real = ks._create_srk
    ks._create_srk = lambda: calls.append(1) or real()
    cred = ks.generate_and_seal("alice")
    for _ in range(3):
        ks.unseal(cred)
    assert len(calls) == 1
    ks.close()


def test_recovers_when_cached_objects_disappear(swtpm):
    """If the TPM loses our cached SRK/gate (e.g. across suspend), the next
    unseal transparently rebuilds them instead of failing."""
    ks = TPMKeystore(tcti=swtpm.tcti)
    cred = ks.generate_and_seal("alice")
    ks._flush(ks._gate, ks._srk)        # simulate the TPM dropping them
    assert len(ks.unseal(cred)) == 32
    ks.close()


class CountingESAPI:
    """Wraps ESAPI and counts TPM commands (method calls that hit the TPM)."""

    LOCAL = {"tr_set_auth", "trsess_set_attributes", "close"}

    def __init__(self, esys):
        self._esys, self.calls = esys, []

    def __getattr__(self, name):
        attr = getattr(self._esys, name)
        if callable(attr) and name not in self.LOCAL:
            def counted(*a, **k):
                self.calls.append(name)
                return attr(*a, **k)
            return counted
        return attr


def test_after_prepare_only_one_tpm_command_remains(swtpm):
    """Real TPMs can take hundreds of ms per command (docs/HARDWARE.md), so
    after a match only the Unseal itself may be left."""
    ks = TPMKeystore(tcti=swtpm.tcti)
    cred = ks.generate_and_seal("alice")
    ks.unseal(cred)                      # warm the caches (SRK, gate, object)
    prepared = ks.prepare_unseal(cred)
    ks._esys = counting = CountingESAPI(ks._esys)
    assert len(ks.finish_unseal(prepared)) == 32
    assert counting.calls == ["unseal"]
    ks._esys = counting._esys
    ks.close()


def test_no_session_or_object_leaks(swtpm):
    """swtpm has only a few session/object slots and no resource manager, so
    a leak here fails within a handful of rounds."""
    ks = TPMKeystore(tcti=swtpm.tcti)
    cred = ks.generate_and_seal("alice")
    for _ in range(10):
        ks.unseal(cred)
        ks.abort_unseal(ks.prepare_unseal(cred))   # the "no match" path
    assert len(ks.unseal(cred)) == 32
    ks.close()
