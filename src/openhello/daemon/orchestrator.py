"""
Domain core of openhellod: enrollment, authentication and removal.

Knows nothing about IPC — the D-Bus service (service.py) and tests drive it
directly. Everything here is thread-safe: the service runs each long
operation on a worker thread.

Per-user state under core.paths.user_dir(user):
  enrollment.json   which modalities the user enrolled, and which of those they
                    chose to sign in with (EnrollmentRecord)
  sealed.{pub,priv,json}   TPM-sealed secret (tpm/keystore.py), one per user,
                    shared by all modalities
  auth.pub          Ed25519 public key PAM verifies signatures with
                    (core/authtoken.py)
  face.npz          face templates (backends/face/ir), if face is enrolled
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..backends.base import (
    Cancelled,
    CancelToken,
    EnrollRejected,
    Modality,
    ModalityError,
    ProgressCallback,
)
from ..core import authtoken
from ..core.paths import ensure_private_dir, user_dir, write_private
from ..core.users import validate_username
from ..tpm.keystore import SealedCredential, TPMKeystore

log = logging.getLogger(__name__)

RECORD_FILE = "enrollment.json"
CREDENTIAL_FILES = ("sealed.pub", "sealed.priv", "sealed.json", "auth.pub")


class OpenHelloError(Exception):
    """A failure with a stable machine-readable code (mapped to D-Bus error
    names by the service layer)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class EnrollmentRecord:
    """enrollment.json: which modalities the user enrolled, and which of those
    they chose to sign in with (a subset; see set_sign_in)."""
    modalities: list[str] = field(default_factory=list)
    sign_in: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, user: str) -> EnrollmentRecord:
        try:
            data = json.loads((user_dir(user) / RECORD_FILE).read_text())
        except FileNotFoundError:
            return cls()
        modalities = [str(m) for m in data.get("modalities", [])]
        # version-1 records have no sign-in choice: everything enrolled is on.
        sign_in = [str(m) for m in data.get("sign_in", modalities) if m in modalities]
        return cls(modalities, sign_in)

    def save(self, user: str) -> None:
        ensure_private_dir(user_dir(user))
        write_private(user_dir(user) / RECORD_FILE, json.dumps(
            {"version": 2, "modalities": self.modalities, "sign_in": self.sign_in}).encode())


@dataclass(frozen=True)
class ModalityStatus:
    name: str
    available: bool
    enrolled: bool
    security_note: str


class _Preparation:
    """A PreparedUnseal being made in the background during a scan."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value = None

    def wait(self):
        self.done.wait()
        return self.value


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    signature: bytes = b""
    reason: str = ""


class Orchestrator:
    def __init__(self, keystore: TPMKeystore, modalities: Mapping[str, Modality]):
        self._keystore = keystore
        self._modalities = dict(modalities)
        self._tpm_lock = threading.Lock()      # ESAPI contexts aren't thread-safe
        self._record_lock = threading.Lock()

    @property
    def modality_names(self) -> list[str]:
        return list(self._modalities)

    # -- queries --------------------------------------------------------------
    def status(self, user: str) -> list[ModalityStatus]:
        user = validate_username(user)
        registered = EnrollmentRecord.load(user).modalities
        out = []
        for name, m in self._modalities.items():
            out.append(ModalityStatus(
                name=name,
                available=m.is_available(),
                enrolled=name in registered and m.is_enrolled(user),
                security_note=m.security_note(),
            ))
        return out

    def enrolled_items(self, user: str, modality: str) -> list[str]:
        user = validate_username(user)
        try:
            return self._modality(modality).enrolled_items(user)
        except ModalityError as e:
            raise OpenHelloError("device-error", str(e)) from e

    # -- enrollment -----------------------------------------------------------
    def enroll(self, user: str, modality: str, *, progress: ProgressCallback = lambda p: None,
               cancel: CancelToken | None = None,
               options: Mapping[str, Any] | None = None) -> None:
        user = validate_username(user)
        m = self._modality(modality)
        if not m.is_available():
            raise OpenHelloError("not-available", f"{modality} hardware is not available")
        try:
            ok = m.enroll(user, progress=progress, cancel=cancel, options=options)
        except Cancelled:
            raise OpenHelloError("cancelled", "enrollment cancelled") from None
        except NotImplementedError as e:
            raise OpenHelloError("not-implemented", str(e)) from e
        except ModalityError as e:
            raise OpenHelloError("device-error", str(e)) from e
        except EnrollRejected as e:
            raise OpenHelloError(e.code, str(e)) from e
        if not ok:
            raise OpenHelloError("enroll-failed", f"{modality} enrollment did not complete")

        self._ensure_credential(user)
        with self._record_lock:
            record = EnrollmentRecord.load(user)
            if modality not in record.modalities:
                record.modalities.append(modality)
                record.sign_in.append(modality)   # new methods are on by default
                record.save(user)
        log.info("enrolled %s for %s", modality, user)

    def remove(self, user: str, modality: str) -> None:
        user = validate_username(user)
        m = self._modality(modality)
        m.remove(user)
        with self._record_lock:
            record = EnrollmentRecord.load(user)
            record.modalities = [n for n in record.modalities if n != modality]
            record.sign_in = [n for n in record.sign_in if n != modality]
            if record.modalities:
                record.save(user)
            else:
                self._delete_credential(user)
        log.info("removed %s for %s", modality, user)

    # -- authentication -------------------------------------------------------
    # -- sign-in choice --------------------------------------------------------
    def sign_in_methods(self, user: str) -> tuple[list[str], list[str]]:
        """(enabled, supported): the user's chosen sign-in methods, and the
        enrolled ones that can currently authenticate at all (the GUI only
        offers switches for those)."""
        user = validate_username(user)
        record = EnrollmentRecord.load(user)
        supported = [n for n in record.modalities
                     if n in self._modalities and self._modalities[n].can_authenticate]
        return record.sign_in, supported

    def set_sign_in(self, user: str, methods: list[str]) -> None:
        """Choose which enrolled methods are used for sign-in. An empty list
        is allowed: OpenHello then stays out of the way (password only)."""
        user = validate_username(user)
        with self._record_lock:
            record = EnrollmentRecord.load(user)
            unknown = [m for m in methods if m not in record.modalities]
            if unknown:
                raise OpenHelloError("not-enrolled", f"not enrolled: {', '.join(unknown)}")
            record.sign_in = [m for m in record.modalities if m in methods]  # keep order
            record.save(user)
        log.info("sign-in methods for %s: %s", user, record.sign_in)

    def auth_methods(self, user: str) -> list[str]:
        """What authenticate() will actually try right now: chosen by the user,
        hardware present, and able to authenticate (e.g. face needs liveness)."""
        user = validate_username(user)
        return [n for n in EnrollmentRecord.load(user).sign_in
                if n in self._modalities
                and self._modalities[n].can_authenticate
                and self._modalities[n].is_available()]

    # -- authentication -------------------------------------------------------
    def authenticate(self, user: str, nonce: bytes, service: str, *,
                     cancel: CancelToken | None = None) -> AuthResult:
        """
        Run every usable sign-in method at once (like Windows Hello: touch the
        sensor *or* look at the camera). The first live match cancels the
        others, unseals the TPM secret and signs PAM's challenge.
        """
        user = validate_username(user)
        if not EnrollmentRecord.load(user).modalities:
            return AuthResult(False, reason="not-enrolled")
        methods = self.auth_methods(user)
        if not methods:
            return AuthResult(False, reason="unavailable: no usable sign-in method")

        started = time.monotonic()
        prepared = self._prepare_in_background(user)   # TPM work while the user reaches the sensor
        stop = CancelToken()
        results: queue.Queue[tuple[str, str, str]] = queue.Queue()

        def run(name: str) -> None:
            try:
                log.info("authenticate %s: trying %s", user, name)
                matched = self._modalities[name].verify(user, cancel=stop)
                results.put((name, "match" if matched else "no-match", ""))
            except Cancelled:
                results.put((name, "cancelled", ""))
            except NotImplementedError as e:
                results.put((name, "unsupported", str(e)))
            except ModalityError as e:
                results.put((name, "error", str(e)))
            except Exception as e:   # never leave authenticate() waiting forever
                log.exception("%s.verify crashed", name)
                results.put((name, "error", repr(e)))

        for name in methods:
            threading.Thread(target=run, args=(name,), name=f"verify-{name}",
                             daemon=True).start()

        errors, tried = [], False
        for _ in methods:
            while True:
                try:
                    name, outcome, detail = results.get(timeout=0.2)
                    break
                except queue.Empty:
                    if cancel is not None and cancel.cancelled:
                        stop.cancel()
            if outcome == "match":
                stop.cancel()   # the other methods stop scanning
                matched_at = time.monotonic()
                result = self._sign(user, nonce, service, prepared)
                log.info("authenticate %s: %s matched after %.2f s, signed in %.2f s "
                         "(total %.2f s)", user, name, matched_at - started,
                         time.monotonic() - matched_at, time.monotonic() - started)
                return result
            if outcome == "no-match":
                tried = True
            elif outcome == "error":
                log.warning("%s.verify failed: %s", name, detail)
                errors.append(f"{name}: {detail}")
        self._discard_in_background(prepared)
        if cancel is not None and cancel.cancelled:
            return AuthResult(False, reason="cancelled")
        if tried:
            return AuthResult(False, reason="no-match")
        # Nothing got as far as comparing a biometric — say why instead of
        # reporting a misleading "no-match".
        detail = "; ".join(errors) or "no usable sign-in method"
        return AuthResult(False, reason=f"unavailable: {detail}")

    # -- internals ----------------------------------------------------------
    def _modality(self, name: str) -> Modality:
        try:
            return self._modalities[name]
        except KeyError:
            raise OpenHelloError("unknown-modality", f"unknown modality {name!r}") from None

    def _sign(self, user: str, nonce: bytes, service: str,
              prepared: _Preparation | None = None) -> AuthResult:
        """Unseal the TPM secret and sign PAM's challenge. Uses the unseal
        prepared during the scan when there is one (one TPM command left)."""
        ready = prepared.wait() if prepared is not None else None
        try:
            with self._tpm_lock:
                if ready is not None:
                    secret = self._keystore.finish_unseal(ready)
                else:
                    try:
                        cred = SealedCredential.load(user)
                    except FileNotFoundError:
                        return AuthResult(False, reason="not-enrolled")
                    secret = self._keystore.unseal(cred)
        except Exception:
            # Logged (with the TPM error) by the keystore. Typical cause once
            # PCRs are in the policy: boot chain changed -> re-enroll.
            return AuthResult(False, reason="unseal-failed")
        return AuthResult(True, signature=authtoken.sign_challenge(secret, user, nonce, service))

    def _prepare_in_background(self, user: str) -> _Preparation:
        prep = _Preparation()

        def run() -> None:
            try:
                cred = SealedCredential.load(user)
                with self._tpm_lock:
                    prep.value = self._keystore.prepare_unseal(cred)
            except Exception as e:   # no credential / TPM error: _sign falls back
                log.debug("preparing unseal for %s failed: %s", user, e)
            finally:
                prep.done.set()
        threading.Thread(target=run, name="prepare-unseal", daemon=True).start()
        return prep

    def _discard_in_background(self, prep: _Preparation) -> None:
        """No match: close the prepared session without delaying the reply."""
        def run() -> None:
            value = prep.wait()
            if value is not None:
                with self._tpm_lock:
                    self._keystore.abort_unseal(value)
        threading.Thread(target=run, name="discard-unseal", daemon=True).start()

    def _ensure_credential(self, user: str) -> None:
        """Seal a credential for `user` unless a working one already exists.
        Always unseals once to prove the seal works before reporting success."""
        with self._tpm_lock:
            try:
                secret = self._keystore.unseal(SealedCredential.load(user))
                return
            except FileNotFoundError:
                pass
            except Exception:
                log.warning("existing credential for %s doesn't unseal; re-sealing", user)
            cred = self._keystore.generate_and_seal(user)
            cred.save()
            secret = self._keystore.unseal(cred)
        write_private(user_dir(user) / "auth.pub", authtoken.public_key(secret, user))

    def _delete_credential(self, user: str) -> None:
        d = user_dir(user)
        for f in CREDENTIAL_FILES + (RECORD_FILE,):
            (d / f).unlink(missing_ok=True)
