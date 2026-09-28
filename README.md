# OpenHello

**Windows Hello–style sign-in for Linux: fingerprint and infrared face
recognition, with the credential sealed in the TPM.**

OpenHello unifies fingerprint (via fprintd) and IR face recognition behind
one system service and one setup app. A biometric match doesn't simply
answer "yes": it unseals a secret held by the computer's TPM chip, and the
PAM module accepts only a fresh signature made with that secret. Your
password always keeps working.

Fedora is the first supported distribution.

## Features

- **Fingerprint** through fprintd/libfprint, reusing fingers already enrolled
  in GNOME Settings.
- **IR face recognition** on "Windows Hello compatible" laptops (IR camera +
  IR emitter). Phone screens and inkjet prints are invisible to the IR
  camera; a provisional photo check (3D shading, eye glint, glare) guards
  against prints that are visible. See [docs/SECURITY.md](docs/SECURITY.md).
- **TPM 2.0 sealing** of the per-user credential, with Ed25519
  challenge-response between daemon and PAM, so a lying daemon or a
  replayed reply can't sign anyone in.
- **Fingerprint and face at the same time**: touch the sensor or look at the
  camera, whichever comes first.
- **Setup app** (GTK 4 / libadwaita) to enroll, choose which methods sign you
  in, and remove them. It asks you to authenticate every time it opens.
- Works for the login screen (GDM), lock screen, `sudo`, `su` and polkit
  prompts.

## Install (Fedora)

Packages will be published on [COPR](https://copr.fedorainfracloud.org), so
installing becomes `sudo dnf copr enable <owner>/openhello` followed by
`sudo dnf install openhello-setup`. Until then, build the RPMs from this
repository:

```bash
packaging/container-build.sh fedora dist/fedora     # needs podman or docker
sudo dnf install dist/fedora/openhello-0*.rpm dist/fedora/openhello-setup-0*.rpm
```

Then open **Biometric Sign-In**, set up your fingerprint and/or face, and
turn OpenHello on for login and sudo:

```bash
sudo openhello-pam enable      # reversible: sudo openhello-pam disable
```

`openhello-pam` uses authselect and replaces `pam_fprintd` with
`pam_openhello` in the local login stacks. Remote logins are not changed.

## Requirements

- A TPM 2.0 (`/dev/tpmrm0`); nearly all laptops since about 2016.
- For fingerprint: a sensor supported by libfprint.
- For face: an IR camera with IR emitter. Plain RGB webcams aren't used for
  face sign-in, because they can't tell a face from a photo.

## Status

Pre-1.0 and not externally audited: treat face sign-in as a convenience
feature for now. What has been tested, on which hardware, is listed in
[docs/HARDWARE.md](docs/HARDWARE.md). The main open items:

- The photo check hasn't been measured against laser prints or photocopies
  (carbon toner is visible in IR).
- Face match thresholds haven't been measured against other people's faces
  yet.
- Depth-camera (3D) face sign-in is designed but not implemented.

## Building from source

```bash
sudo dnf install gcc cmake pam-devel systemd-devel openssl-devel \
    python3-gobject gtk4 libadwaita python3-tpm2-pytss python3-cryptography \
    python3-numpy python3-opencv python3-onnxruntime fprintd swtpm dbus-daemon
cmake -S pam -B pam/build && cmake --build pam/build
python3 -m pytest tests/        # swtpm + a private D-Bus; never touches your TPM
tools/dev_run.sh                # whole stack unprivileged, with the setup app
```

See [docs/TESTING.md](docs/TESTING.md) for the development workflow and
[packaging/](packaging/) for building the RPMs.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md): components, D-Bus API, TPM policy,
  design decisions
- [docs/SECURITY.md](docs/SECURITY.md): threat model and what's tested
- [docs/HARDWARE.md](docs/HARDWARE.md): tested hardware and measurements
- [docs/TESTING.md](docs/TESTING.md): tests and development tools
- [CONTRIBUTING.md](CONTRIBUTING.md)

## License

MIT, see [LICENSE](LICENSE). The bundled face models are MIT (YuNet) and
Apache-2.0 (SFace); see [packaging/models-LICENSES.md](packaging/models-LICENSES.md).
