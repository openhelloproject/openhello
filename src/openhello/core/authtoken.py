"""
Challenge-response token between openhellod and pam_openhello.

PAM never holds the TPM-sealed secret, so it can't derive a shared-key
token itself. Instead:

  enroll: daemon derives an Ed25519 keypair from the unsealed secret and
          writes only the 32-byte public key to <state_dir>/<user>/auth.pub.
  auth:   PAM sends a fresh random 32-byte nonce; after a biometric match
          the daemon unseals the secret, re-derives the private key and
          signs AUTH_CONTEXT || nonce || user || NUL || service. PAM verifies
          against auth.pub (see pam/pam_openhello.c: build_message()).

A daemon that hasn't actually unsealed the secret can't produce a valid
signature, and a captured reply is useless for any other nonce.

The message layout here and in pam/pam_openhello.c must stay byte-identical.
"""

from __future__ import annotations

import hashlib
import hmac

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

AUTH_CONTEXT = b"openhello-auth-v1\0"
_KDF_LABEL = b"openhello-ed25519-v1\0"
NONCE_LEN = 32


def _signing_key(secret: bytes, user: str) -> Ed25519PrivateKey:
    seed = hmac.new(secret, _KDF_LABEL + user.encode(), hashlib.sha256).digest()
    return Ed25519PrivateKey.from_private_bytes(seed)


def public_key(secret: bytes, user: str) -> bytes:
    return _signing_key(secret, user).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def build_message(nonce: bytes, user: str, service: str) -> bytes:
    if len(nonce) != NONCE_LEN:
        raise ValueError(f"nonce must be {NONCE_LEN} bytes")
    if "\0" in user or "\0" in service:
        raise ValueError("NUL in user/service")
    return AUTH_CONTEXT + nonce + user.encode() + b"\0" + service.encode()


def sign_challenge(secret: bytes, user: str, nonce: bytes, service: str) -> bytes:
    return _signing_key(secret, user).sign(build_message(nonce, user, service))
