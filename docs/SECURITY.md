# Threat model

This document states what OpenHello protects against and what it doesn't.
Every claim is backed by a test or a measurement; claims that aren't are
listed as open. Measurements are in [HARDWARE.md](HARDWARE.md). To report a
vulnerability, see [../SECURITY.md](../SECURITY.md).

## Assets and trust boundaries

- **Per-user sign-in secret:** 256 bits, sealed in the TPM. It's what
  ultimately signs the user in.
- **Biometric data:** fingerprints stay in fprintd's store (on the sensor
  itself for match-on-chip sensors). Face embeddings are stored in
  `/var/lib/openhello/<user>/face.npz`, root-only.
- **Trusted:** the kernel, the TPM, root, fprintd, polkit, the D-Bus daemon.
- **Untrusted:** other local users, anyone with physical access to a locked
  or powered-off machine, and anything presented to the sensors.

## What TPM sealing provides

Tested against a software TPM in `tests/test_keystore_swtpm.py`, and used
daily on a hardware TPM (see HARDWARE.md):

- The secret is never stored in the clear on disk; an attacker who can read
  the disk but can't use this TPM can't recover it.
- Sealed blobs copied to another machine don't unseal there.
- Unsealing requires the daemon's gate authorization; a plain password
  session can't unseal.
- Neither the secret nor the gate authorization crosses the TPM bus in the
  clear (sessions are salted and parameter-encrypted). A control test
  confirms the bus capture would detect a leak.

**Limit of the default policy (no PCRs):** anyone who can read
`/var/lib/openhello/gate.auth` *and* use this machine's TPM can unseal. That
means root, or someone who boots another OS on the same machine when the
disk isn't encrypted. Full-disk encryption closes the second case; enabling
PCR 7 with Secure Boot narrows it.

## What the PAM module checks

Tested end to end in `tests/test_pam_e2e.py` (the real module, a real
D-Bus daemon, TPM sealing on swtpm):

- `"ok"` is never enough: PAM requires an Ed25519 signature over its own
  fresh nonce, the user and the PAM service, verified against the public key
  written at enrollment. Rejected: a missing signature, a garbage signature,
  a replayed genuine signature, and a signature made for another service.
- `auth.pub` is refused unless it and its directory are root-owned and not
  group/world-writable.
- A daemon that's down, hangs, or has no usable method for the user yields
  `PAM_AUTHINFO_UNAVAIL` (immediately, or after the timeout), and the
  `sufficient` stack falls through to the password.

## Local access control

- Settings methods act only on the calling user, as identified by the bus,
  and only after `Unlock()`: polkit `auth_self`, asked on every app launch
  and enforced per D-Bus connection.
- `Authenticate` and `GetAuthMethods` are root-only, enforced by both the
  D-Bus policy and the daemon.
- Usernames are validated (`[A-Za-z_][A-Za-z0-9_.-]{0,31}`) before they're
  used in paths, in both the daemon and the PAM module.
- Like `sudo`, the unlock prompt goes through the system PAM stack, so with
  OpenHello enabled a fingerprint or face can answer it.

## Face presentation attacks

Measured on the reference IR camera (details in HARDWARE.md):

| Attack | Result |
|---|---|
| Phone screen showing the user's face | Rejected: the displayed face is invisible in near-IR, so no face is detected |
| Colour inkjet print | Rejected: dye inks are transparent to near-IR, so the camera sees blank paper |
| Grayscale inkjet print | Rejected: same reason on the tested printer |
| Laser print / photocopy | **Not tested.** Carbon toner absorbs IR, so these do show a face |
| Other people's faces | **Not tested.** No impostor scores yet |

Every accepted face must match with a cosine score of at least 0.7 on two
frames, and each of those frames must pass a **provisional photo check**:
3D shading under the emitter, a corneal glint, and no glare clipping. The
thresholds are set below the lowest values measured on genuine faces, not
between genuine and spoof measurements, because no IR-visible spoof has
been measured. A carefully made laser print could imitate shading and
catchlights. **Treat face sign-in as convenience-grade until that
measurement exists.**

For comparison: published research has bypassed Windows Hello's own IR
checks with specially prepared IR images. "Considerably harder to fool than
webcam face recognition" is the honest claim; "unbreakable" isn't.

## What OpenHello does not protect against

- **Root on the running machine.** Root can drive the same code path a
  genuine match would. TPM sealing raises the bar for offline and remote
  secret extraction, not against local privilege escalation.
- **A fooled sensor.** The TPM protects the secret, not the decision to
  release it: whatever the matcher accepts gets in.
- **Web authentication.** OpenHello isn't a FIDO2/WebAuthn authenticator.

## Operational notes

- fprintd serves one client at a time. While another client holds the
  sensor, OpenHello reports "unavailable" rather than a failed match, and
  the password still works.
- If fprintd ends up in a stuck state (for example a scan interrupted by
  suspend), the daemon restarts it, at most once a minute, and logs it.
- Every unseal and every face accept or photo-check rejection is logged to
  the journal with its measurements.

## Recommendations for deployment

- Always `sufficient`, never `required` (`openhello-pam` only does this).
- On shared or high-value machines, keep face sign-in off for `sudo`/`su`
  until the photo check has been measured against laser prints.
- Use full-disk encryption.
