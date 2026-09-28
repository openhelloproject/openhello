# Contributing

Thanks for helping. OpenHello touches how people log in, so the bar is
"tested and honest" rather than "looks finished".

## Getting started

Development setup, tests and tools are described in
[docs/TESTING.md](docs/TESTING.md). In short:

```bash
cmake -S pam -B pam/build && cmake --build pam/build
python3 -m pytest tests/
tools/dev_run.sh          # whole stack unprivileged: swtpm + session-bus daemon + app
```

Nothing in the test suite touches your TPM or `/etc/pam.d`.

## Guidelines

- **Architecture:** read [ARCHITECTURE.md](ARCHITECTURE.md). Keep the
  dependency direction (`daemon` → `backends`/`tpm` → `core`); the app talks
  to the daemon only through the D-Bus API. New sensor types implement
  `backends.base.Modality`.
- **Security claims:** [docs/SECURITY.md](docs/SECURITY.md) only states what
  a test or measurement backs. Don't widen a claim without adding the
  evidence; prefer stating a limitation plainly.
- **Password fallback:** OpenHello is always `sufficient`; changes must never
  make the password path depend on the daemon.
- **Style:** `ruff check src tests tools` must pass; shell scripts must pass
  `shellcheck`. Comments explain *why*; measurements belong in
  [docs/HARDWARE.md](docs/HARDWARE.md), not in code comments.
- **Tests:** every behaviour change comes with a test. Prefer real
  components (swtpm, a private dbus-daemon, the real PAM module) over mocks;
  only the biometric match itself is stubbed.
- **Commits:** small, focused, with a message that says what and why.

## Hardware reports

Reports from other laptops are very valuable. Please open an issue with:

- `lsusb`, and `v4l2-ctl --list-devices` plus `--list-formats-ext` for each
  `/dev/video*`
- `fprintd-list $USER` and your sensor model
- what worked (enroll, sudo, lock screen, face) and `journalctl -u openhellod`
  output for anything that didn't
