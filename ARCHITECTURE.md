# Architecture

## Goals

1. One setup experience for fingerprint and face.
2. What PAM checks is a TPM-sealed secret, not a biometric daemon's
   "match: true".
3. Modalities (fingerprint, face, and later others) sit behind one interface,
   so new sensor types don't touch PAM, D-Bus or the app.
4. The password always works. OpenHello is only ever `sufficient`.

## Components

```
 ┌──────────────────────┐   ┌──────────────────────┐
 │ openhello-setup      │   │ pam_openhello.so (C) │
 │ GTK 4 / libadwaita   │   │ GDM, sudo, su, polkit│
 └──────────┬───────────┘   └──────────┬───────────┘
            │   D-Bus system bus: org.openhello.Daemon1
            │   (settings: Unlock via polkit; PAM methods: root only)
 ┌──────────▼──────────────────────────▼───────────┐
 │ openhellod — daemon/service.py (IPC adapter)    │
 │            — daemon/orchestrator.py (domain)    │
 └───────┬──────────────────┬──────────────────┬───┘
         │ Modality         │ Modality         │
 ┌───────▼────────┐ ┌───────▼─────────┐ ┌──────▼──────────┐
 │ fingerprint    │ │ face            │ │ tpm/keystore    │
 │ → fprintd      │ │ probe → IR / 3D │ │ → TPM 2.0       │
 │   (D-Bus)      │ │ → V4L2, ONNX    │ │   (tpm2-pytss)  │
 └────────────────┘ └─────────────────┘ └─────────────────┘
```

### Code layout and dependency rule

```
src/openhello/
  core/      paths, username rules, auth-token format, GLib shims (no devices, no IPC)
  tpm/       sealing and unsealing (depends on core)
  backends/  base.Modality contract; fingerprint; face/ (probe, ir/, depth)
  daemon/    orchestrator.py (domain, no IPC) → service.py (D-Bus) → __main__.py
  gui/       client.py (D-Bus client) → window.py → pages/, widgets/
pam/         C PAM module (sd-bus, OpenSSL); mirrors core/authtoken.py and core/users.py
data/        D-Bus policy and activation, polkit policy, systemd unit, desktop files, icons
packaging/   Fedora spec (supported), Debian/Arch recipes (experimental), tarball script
tools/       development tools: calibration, benchmarks, screenshots, dev stack
```

Dependencies only point downwards: `daemon` → `backends`/`tpm` → `core`. The
app imports nothing from the daemon; its only contract is the D-Bus API in
`daemon/org.openhello.Daemon1.xml`. Adding a sensor type means implementing
`backends.base.Modality`.

## D-Bus API

`org.openhello.Daemon1` at `/org/openhello/Daemon1` on the system bus. The
XML file is authoritative; summary:

| Member | Who may call | What it does |
|---|---|---|
| `Unlock()` | polkit `org.openhello.settings` (`auth_self`, never cached) | unlocks the settings methods below for this connection until it closes; the app calls it on every launch |
| `GetStatus()` | unlocked | modalities for the caller: available, enrolled, security note |
| `ListFingers()` | unlocked | the caller's fingers enrolled in fprintd |
| `EnrollStart(modality, options)` | unlocked | starts enrollment; progress via `EnrollProgress`, outcome via `EnrollFinished`, both sent only to the caller |
| `EnrollCancel()` | unlocked, same client | cancels; also automatic if the client disconnects |
| `Remove(modality)` | unlocked | removing the last modality deletes the sealed credential |
| `GetSignInMethods()` / `SetSignInMethods(methods)` | unlocked | which enrolled methods are used for sign-in |
| `GetAuthMethods(user)` | root only | what `Authenticate` will run (lets PAM prompt accurately) |
| `Authenticate(user, nonce, service)` | root only (bus policy and daemon check) | runs the chosen methods; returns an Ed25519 signature |

Every method except the two root-only ones acts on the calling user as
resolved from the bus connection, never on a username argument.

## Enrollment

1. The app starts locked and calls `Unlock()`. polkit authenticates the user
   (every launch), so someone at an unlocked session can't add their own
   biometrics to your account.
2. The modality captures data. Fingerprint reuses prints already in fprintd,
   or runs an fprintd enrollment for a new finger. Face stores embeddings in
   `/var/lib/openhello/<user>/face.npz` (0600).
3. If the user has no working credential yet, the daemon generates a random
   256-bit secret and seals it into the TPM (see "TPM policy"). There is one
   credential per user, shared by all modalities.
4. The daemon unseals it once to prove the seal works, derives an Ed25519
   keypair from the secret, and writes only the public key to `auth.pub`.
5. The modality is added to `enrollment.json` and switched on for sign-in.

## Authentication

1. `pam_openhello.so` asks `GetAuthMethods(user)`. If nothing can sign the
   user in (all methods off, sensor missing), it returns at once so the
   password prompt appears without delay. Otherwise it shows a matching
   prompt ("Scan your fingerprint or look at the camera").
2. It generates a fresh 32-byte nonce and calls
   `Authenticate(user, nonce, service)`.
3. The daemon runs all chosen methods concurrently; the first live match
   cancels the others. Meanwhile the TPM policy session is prepared in the
   background, so after a match only the final `Unseal` command remains.
4. The daemon signs `"openhello-auth-v1\0" ‖ nonce ‖ user ‖ "\0" ‖ service`
   with the key derived from the unsealed secret (`core/authtoken.py`). The
   secret itself never leaves the daemon.
5. PAM verifies the signature against `auth.pub`. The file and its directory
   must be root-owned and not group/world-writable. A reply without a valid
   signature is rejected, and a captured signature is useless for any other
   nonce or service.

Scans stop as soon as nobody is waiting: when the PAM caller disconnects
(e.g. GDM after a password unlock) and when logind announces suspend.

PAM talks to the daemon over a private sd-bus connection. sd-bus reads
`DBUS_SYSTEM_BUS_ADDRESS` via `secure_getenv()`, so inside setuid programs a
user can't redirect the module to another bus.

## Face modality selection

At startup `backends/face.get_face_modality()` probes the cameras with V4L2
ioctls:

```
          depth stream found      IR-only capture node        neither
                 │                        │                      │
       Depth3DFaceModality         IRFaceModality       UnavailableFaceModality
     (point cloud + ICP;          (IR embedding match   (face not offered;
      not implemented yet)         + photo check)        other modalities work)
```

This mirrors Windows Hello, which uses whatever the camera supports. Depth
is a stronger option when present, not a requirement.

### IR face pipeline

The IR emitter strobes on alternate frames. `ir/capture.py` pairs each lit
frame with the next dark one, so `lit − dark` isolates the emitter's own
reflection from ambient IR. YuNet finds the face; SFace (or, optionally,
ArcFace) embeds it; a cosine score of at least 0.7 on two frames is required.
Each of those frames must also pass the photo check in `ir/liveness.py`: 3D
shading (nose brighter than cheeks under the emitter), a corneal glint, and
no glare clipping. The thresholds and the data behind them are in
[docs/HARDWARE.md](docs/HARDWARE.md).

## TPM policy

`sealed secret authPolicy = [PolicyPCR(sha256:pcrs)] → PolicySecret(gate)`

- **gate:** a TPM-generated HMAC key under the SRK, with a random 256-bit
  auth value in `/var/lib/openhello/gate.auth` (root, 0600).
  `PolicySecret(gate)` is the daemon's "unlock intent". It's a loadable
  object rather than an NV index, so OpenHello writes no persistent TPM
  state and needs no owner auth. Its auth can be rotated with
  `ObjectChangeAuth` without re-sealing any credential.
- **PCRs:** none by default. Without Secure Boot, PCR 7 says little, and
  PCRs 4/8/9 change with every boot-chain update. The PCR list is stored
  per credential, so enabling PCR 7 later doesn't break existing
  enrollments. A PCR mismatch only disables biometrics until re-enrollment.
- **Objects:** the SRK is the TCG-standard ECC P-256 storage template. The
  sealed object is `fixedTPM|fixedParent|noDA|adminWithPolicy`, **without**
  `userWithAuth`, so the policy is the only way to unseal. The gate is
  `noDA`, so a daemon bug can't trigger the TPM's dictionary-attack lockout
  (which would also affect other TPM users).
- **Bus encryption:** sessions are salted with the SRK. The secret travels
  to `Create` parameter-encrypted, the `Unseal` response is encrypted, and
  the gate auth is proven by HMAC, never sent as a password.
- **Latency:** each TPM command can take hundreds of milliseconds. The
  daemon keeps the SRK, gate and sealed objects loaded for the life of its
  TPM connection, lets sessions close themselves, and prepares the policy
  session during the scan. Any TPM error drops the cached objects and
  retries once.
- **Per machine by design:** a credential is bound to one TPM, so each
  device is enrolled separately.

## Why TPM sealing instead of a pass/fail check

With fprintd or Howdy alone, a daemon says "yes" and PAM believes it:
anything that can fake that answer gets in. Here the secret PAM needs is
released only through the TPM's own policy, and PAM checks a signature over
its own fresh nonce, so a forged or replayed "yes" signs nobody in. This is
the same idea as Windows Hello's TPM-backed keys, adapted to what a
commodity TPM 2.0 offers.

## Why FIDO2/WebAuthn is out of scope for now

Everything above is built on existing, documented interfaces: tpm2-tss,
fprintd's D-Bus API, UVC cameras. A FIDO2 *platform authenticator* (what
lets Windows Hello sign you in to websites) needs the OS to implement CTAP2,
present itself to browsers as an authenticator, and manage attestation and
resident keys. Windows and macOS provide that plumbing; Linux currently only
has external security keys through libfido2. The missing piece is that
foundation, not the biometric part.

## Design decisions

| Date | Decision |
|---|---|
| 2026-09-24 | TPM policy: `PolicySecret` on a daemon-held gate, PCR list optional per credential (see "TPM policy"). |
| 2026-09-24 | Fingerprint reuses prints already enrolled in fprintd; a new print is captured only if there are none. |
| 2026-09-24 | Face embedding model is pluggable. SFace (Apache-2.0) with YuNet (MIT) ships by default; InsightFace ArcFace weights are non-commercial, so they're never bundled and admins may add them in `/var/lib/openhello/models`. |
| 2026-09-25 | IPC is D-Bus with polkit, replacing a custom unix socket, so the app and PAM use one standard, activatable API. |
| 2026-09-25 | PAM ↔ daemon trust is Ed25519 challenge-response keyed from the sealed secret; PAM never holds the secret. |
| 2026-09-25 | Face sign-in is enabled before liveness calibration is complete, with a strict match threshold and the limitation stated in the app. |
| 2026-09-25 | Fedora is the first supported distribution; other packaging stays experimental. |
| 2026-09-28 | Provisional photo check (3D shading, eye glint, glare) with thresholds below the genuine minimum; to be tightened once IR-visible prints are measured. |
| 2026-09-28 | The settings app requires authentication on every launch, enforced by the daemon per connection (`Unlock`). |

## Open questions

- **Photo check vs. IR-visible prints:** laser prints and photocopies show a
  face in IR; the thresholds need measuring against them.
- **Impostor rate:** no scores from other people's faces yet, so the 0.7
  threshold isn't tied to a false-accept rate.
- **Depth cameras:** `Depth3DFaceModality` needs hardware to build and test on.
- **Memory:** face recognition keeps OpenCV and the models loaded in the
  daemon; a short-lived helper process would return that memory.
- **Least privilege:** the daemon runs as root; a dedicated user and an
  SELinux policy would narrow it.
