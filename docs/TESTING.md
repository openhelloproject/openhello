# Testing

Nothing in the automated suite touches the machine's TPM or `/etc/pam.d`.
TPM tests use throwaway `swtpm` instances, D-Bus tests use a private
`dbus-daemon`, and the PAM tests load `pam_openhello.so` through
`pam_start_confdir()` with a private configuration directory.

## Automated tests

```bash
cmake -S pam -B pam/build && cmake --build pam/build
python3 -m pytest tests/
```

| File | What's real | What's stubbed |
|---|---|---|
| `test_camera_detect.py` | camera classification; one probe of the real `/dev/video*` | — |
| `test_models_path.py` | face model lookup order; face unavailable without models | — |
| `test_face_ir.py` | IR frame pairing (incl. daylight), match and photo-check rules | camera frames |
| `test_fingerprint.py` | fprintd recovery and retry logic | fprintd |
| `test_keystore_swtpm.py` | TPM sealing on swtpm, bus encryption, no leaks, one command after prepare | — |
| `test_orchestrator.py` | concurrent sign-in, sign-in choice, record migration | biometric match, TPM (dev mode) |
| `test_dbus_service.py` | `org.openhello.Daemon1` on a private bus, TPM on swtpm | polkit (mock), biometric match |
| `test_gui_client.py` | the app's D-Bus client against that service | polkit (mock), biometric match |
| `test_pam_e2e.py` | the PAM module → private D-Bus → real service → swtpm, incl. lying-daemon cases | biometric match |

Lint: `ruff check src tests tools` and `shellcheck` on the shell scripts
(`.github/workflows/ci.yml` runs both).

## The whole stack, unprivileged

```bash
tools/dev_run.sh
```

Starts swtpm, a development-only polkit stand-in that allows everything
(`tools/dev_polkit_allow.py`; real polkit exists only on the system bus),
`openhellod --bus session`, and the setup app with `OPENHELLO_BUS=session`.
It uses the real fingerprint reader and camera; state goes to
`~/.cache/openhello/dev-run`.

### PAM against the development stack

With `tools/dev_run.sh` running, point the PAM harness's "system bus" at
your session bus. This private configuration uses `required` only so the
module's own return code is visible; real deployments are always
`sufficient`.

```bash
mkdir -p /tmp/oh-pam
echo "auth required $PWD/pam/build/pam_openhello.so state_dir=$HOME/.cache/openhello/dev-run/state" \
  > /tmp/oh-pam/openhello-dev
DBUS_SYSTEM_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS \
  pam/build/pam_openhello_harness /tmp/oh-pam openhello-dev "$USER"   # 0 = success
```

## App screenshots without a display

```bash
gtk4-broadwayd :9 &
GDK_BACKEND=broadway BROADWAY_DISPLAY=:9 python3 tools/gui_preview.py screenshots/
```

Renders every screen, light and dark, with a fake daemon client that exists
only in `tools/`.

## Face calibration

```bash
export OPENHELLO_STATE_DIR=~/.cache/openhello/dev-state OPENHELLO_MODEL_DIR=~/.cache/openhello/models
python3 tools/face_calibrate.py enroll                  # ~20 s, small head movements
python3 tools/face_calibrate.py capture --label genuine # or: print, screen, other-person
python3 tools/face_calibrate.py report
```

Every model present runs on the same frames. The shipped models
(`face_detection_yunet_2023mar.onnx`, `face_recognition_sface_2021dec.onnx`)
come from opencv_zoo; `w600k_r50.onnx` (InsightFace `buffalo_l`,
non-commercial) is optional. Results go into [HARDWARE.md](HARDWARE.md).

## TPM latency

```bash
sudo python3 tools/tpm_bench.py
```

Times the prepare step (during the scan) and the finish step (after a match)
on the real TPM, using a throwaway state directory; it never touches
`/var/lib/openhello`.

## On real hardware

After installing the packages and running `openhello-pam enable`, check:
enrollment in the app, `sudo` with each method, the GDM lock screen, the
password fallback with all methods switched off, and the app's unlock prompt
on every launch. `journalctl -u openhellod` logs per-sign-in timings, face
scores and photo-check values. Record results in [HARDWARE.md](HARDWARE.md).
