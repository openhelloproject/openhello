# Changelog

## 0.3.0 (unreleased)

First public release. Fedora is the supported target.

- System service `openhellod` on D-Bus (`org.openhello.Daemon1`), activated
  on demand, sandboxed by systemd, authorized with polkit.
- TPM 2.0-sealed per-user credential (`PolicySecret` on a daemon-held gate,
  optional PCRs); PAM verifies an Ed25519 signature over its own nonce.
- Fingerprint via fprintd, reusing existing prints; up to 3 touches per
  sign-in; recovery when fprintd gets stuck after suspend.
- IR face recognition (YuNet + SFace, bundled; ArcFace optional) with a
  provisional photo check; screens and inkjet prints are invisible to the
  IR camera.
- Fingerprint and face run concurrently; users choose which methods sign
  them in; PAM prompts accurately and steps aside when nothing is usable.
- Scans stop when the caller disconnects or the system suspends.
- ~0.2 s of TPM work after a match (the policy session is prepared during
  the scan).
- `openhello-setup` app (GTK 4 / libadwaita): enrollment with progress
  animations, sign-in switches, authentication required on every launch.
- `openhello-pam enable|disable|status` (authselect on Fedora).
