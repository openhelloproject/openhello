# Tested hardware and measurements

What OpenHello has actually been run on, and the measurements behind its
thresholds. Please add your hardware: see [CONTRIBUTING.md](../CONTRIBUTING.md).

## Reference system (Fedora 44, x86-64 laptop)

| Component | Device | Result |
|---|---|---|
| Fingerprint | Goodix MOC `27c6:6594` (press type, 9 enroll stages), fprintd 1.94.5 / libfprint 1.94.100 | ✅ enroll, verify, GDM lock screen, sudo, polkit |
| IR camera | Luxvisions `30c9:00f4` "Integrated RGB Camera": `/dev/video2` GREY 640×360 @ 15 fps, IR emitter strobes on alternate frames without extra drivers | ✅ face enroll, sign-in, photo check |
| RGB camera | same module, `/dev/video0` MJPG/YUYV | not used for face sign-in (by design) |
| TPM | TPM 2.0 via `/dev/tpmrm0` | ✅ sealing, unsealing |
| Secure Boot | disabled | default PCR-free policy |

## IR camera

- Emitter levels (mean pixel value): lit frames ~30–46, dark frames ~0.5 in
  a dark room and ~10 in daylight. Pairing uses `lit − dark ≥ 4`, not a
  ratio, because in daylight a bright surface pushes lit/dark close to 1.
- Auto-exposure settles after ~20 frames; the first frames are discarded.

## Face: presentation attacks

| Attack | IR frame pairs | Faces detected (detector threshold 0.3) |
|---|---|---|
| Phone screen showing the user's face | 15 | 0 |
| Colour inkjet print, printed side to camera, tilted to avoid glare | 109 | 0 |
| Grayscale inkjet print (same printer), tilted | 113 | 0 |
| Laser print / photocopy | — | not tested |

In near-IR the phone shows only the emitter's reflection on the glass, and
both prints show blank paper: the inks are transparent to near-IR.

## Face: genuine-user calibration

Photo-check features on frames of the enrolled user (`tools/face_calibrate.py`):

| Session | Frames | 3D shading (nose/cheek) | Eye glint | Glare (clipped) |
|---|---|---|---|---|
| Dark room | 25 | 1.19 – 1.27 | 1.70 – 2.46 | 0 |
| Daylight | 32 | 1.25 – 1.36 | 1.42 – 2.52 | 0 |
| **Thresholds** | | **≥ 1.10** | **≥ 1.25** | **≤ 2 %** |

Varied poses (leaning, head turned, looking up/down), 15 s: 88 usable frames,
71 matched (score ≥ 0.7), **64 of those passed the photo check (90 %)**. The
misses were turned heads, where the nose-to-cheek comparison assumes a
roughly frontal face. Sign-in needs two passing frames, so this hasn't caused
noticeable rejections.

SFace match scores: 0.94–0.96 right after enrollment, 0.86–0.91 in later
sessions, median 0.76 in deliberately varied poses. No impostor scores have
been measured yet.

## Sign-in timing

| Step | Time |
|---|---|
| Fingerprint sensor ready after the request | 0.37 s |
| Request → fingerprint match (includes placing the finger) | ~2.0–2.6 s |
| Request → face match | ~2.1–2.2 s |
| TPM unseal after a match | 0.22 s |
| Total, typical | 2.1–2.8 s |

The TPM costs 160–280 ms **per command**, flushes included, so latency
depends on the number of commands, not on cryptography (`tools/tpm_bench.py`).
Unseal originally used 8 commands (~1.8 s after a match); preparing the
policy session during the scan, keeping objects loaded and letting sessions
close themselves leaves 1.

## fprintd behaviour relied on

- fprintd exits after ~30 s idle and is D-Bus-activated again; OpenHello
  addresses it by its well-known name so this is transparent.
- It serves one client at a time; the GDM lock screen holds the sensor while
  locked.
- A verify interrupted by suspend ("Cannot run while suspended") can leave
  it refusing every `Claim` with "The device has already been opened!"
  until restarted. OpenHello cancels scans before suspend and, if it happens
  anyway, restarts fprintd automatically.
