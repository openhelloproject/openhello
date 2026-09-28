"""
TPM 2.0 sealing/unsealing for OpenHello.

This is the piece that actually differentiates OpenHello from a plain
PAM pass/fail daemon: the credential PAM checks lives sealed inside the
TPM, not in a plaintext file.

Requires: python3-tpm2-pytss, a TPM 2.0 chip (real, or swtpm for dev —
pass tcti="swtpm:port=2321").

Policy (see ARCHITECTURE.md "TPM policy"):

    sealed secret authPolicy = [PolicyPCR(pcrs) if pcrs] -> PolicySecret(gate)

  - `gate` is a daemon-owned HMAC key under the SRK whose authValue is a
    random 256-bit value stored root-only on disk (<state_dir>/gate.auth).
    PolicySecret on it is the "daemon unlock intent" from ARCHITECTURE.md.
    It's a loadable object rather than an NV index so OpenHello never
    writes persistent TPM state and never needs owner auth. Its auth can
    later be rotated (ObjectChangeAuth) without re-sealing user blobs,
    since the object's name — which is what the policy binds — doesn't
    change.
  - `pcrs` defaults to empty: without Secure Boot, PCR 7 says little, and
    PCRs 4/8/9 change on every boot-chain update. The list is recorded per blob so it can be turned
    on later without breaking already-sealed credentials.
  - Honest limit: with no PCRs, anyone who can read gate.auth (root,
    or offline disk access without disk encryption) and has *this* TPM can
    unseal. What it does stop: copying blobs to another machine.

Object attributes:
  - SRK: TCG-standard ECC P-256 storage primary, recreated transiently on
    each operation (deterministic from the owner seed, no NV writes).
  - gate: TPM-generated HMAC key, fixedTPM|fixedParent|sensitiveDataOrigin|
    userWithAuth|noDA|sign. noDA so a daemon bug hammering it can never
    trip the TPM's dictionary-attack lockout (which would also affect e.g.
    clevis); a 256-bit random auth doesn't need DA protection.
  - sealed secret: fixedTPM|fixedParent|noDA|adminWithPolicy, *no*
    userWithAuth — the policy is the only way to authorize an unseal.

All sessions are salted with the SRK: the secret is sent to Create with
parameter encryption, the Unseal response is encrypted, and the gate auth
is proven via HMAC rather than sent as a cleartext password — relevant for
discrete TPMs where the SPI/LPC bus can be sniffed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..core.paths import state_dir, write_private
from ..core.paths import user_dir as _user_dir

try:
    from tpm2_pytss import (
        ESAPI,
        ESYS_TR,
        TPM2_ALG,
        TPM2_SE,
        TPM2B_AUTH,
        TPM2B_PRIVATE,
        TPM2B_PUBLIC,
        TPM2B_SENSITIVE_CREATE,
        TPMA_OBJECT,
        TPMA_SESSION,
        TPML_PCR_SELECTION,
        TPMT_PUBLIC,
        TPMT_SYM_DEF,
        TSS2_Exception,
    )
    TPM_AVAILABLE = True
except ImportError:  # allows the rest of the daemon to run/dev without a TPM
    TPM_AVAILABLE = False

log = logging.getLogger(__name__)

SECRET_LEN_BYTES = 32  # 256-bit secret
BLOB_FORMAT_VERSION = 1


@dataclass
class SealedCredential:
    user: str
    public_blob: bytes
    private_blob: bytes
    pcrs: list[int] = field(default_factory=list)

    @staticmethod
    def user_dir(user: str) -> Path:
        return _user_dir(user)

    def path_public(self) -> Path:
        return self.user_dir(self.user) / "sealed.pub"

    def path_private(self) -> Path:
        return self.user_dir(self.user) / "sealed.priv"

    def path_meta(self) -> Path:
        return self.user_dir(self.user) / "sealed.json"

    def save(self) -> None:
        d = self.user_dir(self.user)
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_private(self.path_public(), self.public_blob)
        write_private(self.path_private(), self.private_blob)
        meta = {"version": BLOB_FORMAT_VERSION, "pcrs": self.pcrs}
        write_private(self.path_meta(), json.dumps(meta).encode())

    @classmethod
    def load(cls, user: str) -> SealedCredential:
        d = cls.user_dir(user)
        meta_path = d / "sealed.json"
        pcrs = json.loads(meta_path.read_text())["pcrs"] if meta_path.exists() else []
        return cls(
            user=user,
            public_blob=(d / "sealed.pub").read_bytes(),
            private_blob=(d / "sealed.priv").read_bytes(),
            pcrs=pcrs,
        )


@dataclass
class PreparedUnseal:
    """A policy session already satisfied for `cred`; see prepare_unseal()."""
    cred: SealedCredential
    obj: object = None
    policy: object = None


class TPMKeystore:
    """
    Wraps ESAPI to seal/unseal a random per-user secret under the policy
    described in the module docstring.

    tcti: None uses tss2's default TCTI search (tabrmd, then
    /dev/tpmrm0); "swtpm:port=2321" for a software TPM during development.
    """

    def __init__(self, dev_mode: bool = False, tcti: str | None = None,
                 pcrs: list[int] | None = None):
        self.dev_mode = dev_mode or not TPM_AVAILABLE
        self.pcrs = sorted(pcrs or [])
        self._esys = None if self.dev_mode else ESAPI(tcti)
        # SRK, gate and sealed objects stay loaded for the life of the ESAPI
        # connection: each TPM command can cost hundreds of milliseconds on
        # real hardware (docs/HARDWARE.md). Any TPM error drops them and
        # retries once.
        self._srk = None
        self._gate = None
        self._objects: dict[bytes, object] = {}   # sha256(private blob) -> loaded handle

    def close(self):
        if self._esys is not None:
            self._drop_cached()
            self._esys.close()
            self._esys = None

    def _drop_cached(self) -> None:
        self._flush(*self._objects.values(), self._gate or ESYS_TR.NONE,
                    self._srk or ESYS_TR.NONE)
        self._objects.clear()
        self._srk = self._gate = None

    def _object_handle(self, srk, cred: SealedCredential):
        key = hashlib.sha256(cred.private_blob).digest()
        if key not in self._objects:
            self._objects[key] = self._esys.load(
                srk, TPM2B_PRIVATE.unmarshal(cred.private_blob)[0],
                TPM2B_PUBLIC.unmarshal(cred.public_blob)[0])
        return self._objects[key]

    def _srk_handle(self):
        if self._srk is None:
            self._srk = self._create_srk()
        return self._srk

    def _gate_handle(self, srk, create: bool = False):
        if self._gate is None:
            self._gate = self._load_or_create_gate(srk) if create else self._load_gate(srk)
        else:
            # re-read the auth each time: it's the thing policy checks
            self._esys.tr_set_auth(self._gate, self._gate_paths()[2].read_bytes())
        return self._gate

    def _with_retry(self, operation):
        """Run operation() with the cached SRK/gate; on a TPM error drop the
        cache (e.g. objects flushed across suspend) and try once more."""
        try:
            return operation()
        except TSS2_Exception as e:
            log.debug("TPM operation failed with cached objects (%s); retrying fresh", e)
            self._drop_cached()
            return operation()

    # -- dev fallback (no TPM present) -----------------------------------
    def _dev_seal(self, secret: bytes) -> SealedCredential:
        # NOT secure — obviously plaintext-on-disk fallback so the rest of
        # the stack (PAM module, daemon protocol, GUI) can be developed and
        # tested on a machine without a TPM or inside a container. Real
        # sealing (swtpm or hardware) must be used for anything else.
        return SealedCredential(user="__dev__", public_blob=b"dev", private_blob=secret)

    def _dev_unseal(self, cred: SealedCredential) -> bytes:
        return cred.private_blob

    # -- real TPM path -----------------------------------------------------
    def generate_and_seal(self, user: str) -> SealedCredential:
        secret = secrets.token_bytes(SECRET_LEN_BYTES)

        if self.dev_mode:
            cred = self._dev_seal(secret)
            cred.user = user
            return cred

        def seal() -> SealedCredential:
            srk = self._srk_handle()
            gate = self._gate_handle(srk, create=True)
            enc = ESYS_TR.NONE
            try:
                policy_digest = self._build_policy(srk, gate, self.pcrs)
                sensitive = TPM2B_SENSITIVE_CREATE()
                sensitive.sensitive.userAuth = TPM2B_AUTH()
                sensitive.sensitive.data = secret
                enc = self._salted_session(srk, TPM2_SE.HMAC, TPMA_SESSION.DECRYPT)
                priv, pub, _, _, _ = self._esys.create(
                    srk, in_sensitive=sensitive,
                    in_public=self._sealed_object_template(policy_digest),
                    session1=enc,
                )
            finally:
                self._flush(enc)
            return SealedCredential(
                user=user,
                public_blob=bytes(pub.marshal()),
                private_blob=bytes(priv.marshal()),
                pcrs=list(self.pcrs),
            )

        cred = self._with_retry(seal)
        log.info("sealed new credential for %s (pcrs=%s)", user, self.pcrs)
        return cred

    def unseal(self, cred: SealedCredential) -> bytes:
        if self.dev_mode:
            return self._dev_unseal(cred)

        return self.finish_unseal(self.prepare_unseal(cred))

    # Split unseal: everything up to the final Unseal command can run while
    # the user is still touching the sensor, so only one TPM command remains
    # after a match. Preparing early grants nothing: it only proves the gate
    # auth the daemon holds anyway; the secret is released by finish_unseal(),
    # which the orchestrator only calls after a live biometric match.
    def prepare_unseal(self, cred: SealedCredential) -> PreparedUnseal:
        if self.dev_mode:
            return PreparedUnseal(cred)

        def prepare() -> PreparedUnseal:
            srk = self._srk_handle()
            obj = self._object_handle(srk, cred)
            policy = self._start_policy_session(srk, self._gate_handle(srk), cred.pcrs)
            return PreparedUnseal(cred, obj, policy)

        try:
            return self._with_retry(prepare)
        except Exception as e:
            log.warning("unseal FAILED for %s (prepare): %s", cred.user, e)
            raise

    def finish_unseal(self, prepared: PreparedUnseal, *, retry: bool = True) -> bytes:
        if self.dev_mode:
            return self._dev_unseal(prepared.cred)
        started = time.monotonic()
        try:
            data = bytes(self._esys.unseal(prepared.obj, session1=prepared.policy))
        except TSS2_Exception as e:
            self.abort_unseal(prepared)
            if not retry:
                log.warning("unseal FAILED for %s: %s", prepared.cred.user, e)
                raise
            # stale cached handles (e.g. after suspend): redo everything once
            log.debug("prepared unseal failed (%s); retrying from scratch", e)
            self._drop_cached()
            return self.finish_unseal(self.prepare_unseal(prepared.cred), retry=False)
        prepared.policy = None   # consumed: the TPM closed it (keep=False)
        log.info("unseal succeeded for %s (%.2f s)", prepared.cred.user,
                 time.monotonic() - started)
        return data

    def abort_unseal(self, prepared: PreparedUnseal) -> None:
        """No match: close the prepared policy session (the object stays cached)."""
        if not self.dev_mode and prepared.policy is not None:
            self._flush(prepared.policy)
            prepared.policy = None

    # -- templates / policy ------------------------------------------------
    def _create_srk(self):
        handle, _, _, _, _ = self._esys.create_primary(
            in_sensitive=TPM2B_SENSITIVE_CREATE(),
            in_public=self._storage_key_template(),
            primary_handle=ESYS_TR.OWNER,
        )
        return handle

    def _storage_key_template(self):
        # TCG "TPM v2.0 Provisioning Guidance" SRK template, ECC P-256
        # variant. Deterministic from the owner seed, so recreating it on
        # every call yields the same key and sealed blobs keep loading.
        return TPM2B_PUBLIC.parse(
            "ecc256:aes128cfb",
            objectAttributes=(
                TPMA_OBJECT.FIXEDTPM | TPMA_OBJECT.FIXEDPARENT
                | TPMA_OBJECT.SENSITIVEDATAORIGIN | TPMA_OBJECT.USERWITHAUTH
                | TPMA_OBJECT.NODA | TPMA_OBJECT.RESTRICTED | TPMA_OBJECT.DECRYPT
            ),
            nameAlg="sha256",
        )

    @staticmethod
    def _keyedhash_template(attrs, policy_digest: bytes = b"", hmac: bool = False):
        public = TPMT_PUBLIC(
            type=TPM2_ALG.KEYEDHASH,
            nameAlg=TPM2_ALG.SHA256,
            objectAttributes=attrs,
            authPolicy=policy_digest,
        )
        scheme = public.parameters.keyedHashDetail.scheme
        if hmac:
            scheme.scheme = TPM2_ALG.HMAC
            scheme.details.hmac.hashAlg = TPM2_ALG.SHA256
        else:
            scheme.scheme = TPM2_ALG.NULL
        return TPM2B_PUBLIC(publicArea=public)

    def _sealed_object_template(self, policy_digest: bytes):
        return self._keyedhash_template(
            TPMA_OBJECT.FIXEDTPM | TPMA_OBJECT.FIXEDPARENT
            | TPMA_OBJECT.NODA | TPMA_OBJECT.ADMINWITHPOLICY,
            policy_digest,
        )

    def _gate_template(self):
        # A TPM-generated HMAC key, used only as something to authorize
        # against. (A KEYEDHASH *data* object can't be used: the TPM rejects
        # one with empty data and sensitiveDataOrigin clear, and filler data
        # would just be an unsealable secret with no purpose.)
        return self._keyedhash_template(
            TPMA_OBJECT.FIXEDTPM | TPMA_OBJECT.FIXEDPARENT
            | TPMA_OBJECT.SENSITIVEDATAORIGIN | TPMA_OBJECT.USERWITHAUTH
            | TPMA_OBJECT.NODA | TPMA_OBJECT.SIGN_ENCRYPT,
            hmac=True,
        )

    # -- gate object -------------------------------------------------------
    @staticmethod
    def _gate_paths() -> tuple[Path, Path, Path]:
        d = state_dir()
        return d / "gate.pub", d / "gate.priv", d / "gate.auth"

    def _load_gate(self, srk):
        pub_p, priv_p, auth_p = self._gate_paths()
        pub = TPM2B_PUBLIC.unmarshal(pub_p.read_bytes())[0]
        priv = TPM2B_PRIVATE.unmarshal(priv_p.read_bytes())[0]
        gate = self._esys.load(srk, priv, pub)
        self._esys.tr_set_auth(gate, auth_p.read_bytes())
        return gate

    def _load_or_create_gate(self, srk):
        pub_p, priv_p, auth_p = self._gate_paths()
        if pub_p.exists():
            return self._load_gate(srk)

        auth = secrets.token_bytes(32)  # max authValue size for a sha256 name
        sensitive = TPM2B_SENSITIVE_CREATE()
        sensitive.sensitive.userAuth = auth
        enc = self._salted_session(srk, TPM2_SE.HMAC, TPMA_SESSION.DECRYPT)
        try:
            priv, pub, _, _, _ = self._esys.create(
                srk, in_sensitive=sensitive, in_public=self._gate_template(),
                session1=enc,
            )
        finally:
            self._flush(enc)
        state_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        write_private(auth_p, auth)
        write_private(priv_p, bytes(priv.marshal()))
        write_private(pub_p, bytes(pub.marshal()))
        log.info("created daemon gate object in %s", state_dir())
        return self._load_gate(srk)

    # -- sessions ----------------------------------------------------------
    def _salted_session(self, srk, session_type, attrs=0, keep: bool = True):
        """keep=False: the TPM closes the session after the one command that
        uses it, saving a FlushContext round trip. The caller must then only
        flush it if that command never ran or failed."""
        session = self._esys.start_auth_session(
            tpm_key=srk, bind=ESYS_TR.NONE, session_type=session_type,
            symmetric=TPMT_SYM_DEF.parse("aes128cfb"), auth_hash=TPM2_ALG.SHA256,
        )
        self._esys.trsess_set_attributes(
            session, (TPMA_SESSION.CONTINUESESSION if keep else 0) | attrs
        )
        return session

    def _apply_policy(self, session, srk, gate, pcrs: list[int]) -> None:
        """Policy steps shared by trial (seal) and real (unseal) sessions —
        order matters, so it lives in exactly one place."""
        if pcrs:
            sel = TPML_PCR_SELECTION.parse("sha256:" + ",".join(map(str, pcrs)))
            self._esys.policy_pcr(session, b"", sel)
        hmac_sess = self._salted_session(srk, TPM2_SE.HMAC, keep=False)
        try:
            self._esys.policy_secret(
                gate, session, nonce_tpm=b"", cp_hash_a=b"", policy_ref=b"",
                expiration=0, session1=hmac_sess,
            )
        except Exception:
            self._flush(hmac_sess)   # only still open if the command failed
            raise

    def _build_policy(self, srk, gate, pcrs: list[int]) -> bytes:
        trial = self._esys.start_auth_session(
            tpm_key=ESYS_TR.NONE, bind=ESYS_TR.NONE, session_type=TPM2_SE.TRIAL,
            symmetric=TPMT_SYM_DEF(algorithm=TPM2_ALG.NULL), auth_hash=TPM2_ALG.SHA256,
        )
        try:
            self._apply_policy(trial, srk, gate, pcrs)
            return bytes(self._esys.policy_get_digest(trial))
        finally:
            self._flush(trial)

    def _start_policy_session(self, srk, gate, pcrs: list[int]):
        # ENCRYPT: the Unseal response (the secret) is encrypted on the bus.
        # keep=False: the TPM closes it after the Unseal it authorizes.
        session = self._salted_session(srk, TPM2_SE.POLICY, TPMA_SESSION.ENCRYPT, keep=False)
        try:
            self._apply_policy(session, srk, gate, pcrs)
        except Exception:
            self._flush(session)
            raise
        return session

    def _flush(self, *handles) -> None:
        for h in handles:
            if h != ESYS_TR.NONE:
                try:
                    self._esys.flush_context(h)
                except Exception as e:  # already gone (e.g. session auto-closed)
                    log.debug("flush_context(%s) failed: %s", h, e)
