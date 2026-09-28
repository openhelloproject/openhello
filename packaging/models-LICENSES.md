# Bundled face models

Installed to `/usr/share/openhello/models/`. Both come from the OpenCV Zoo
(https://github.com/opencv/opencv_zoo) and are redistributable:

| File | Model | License |
|---|---|---|
| `face_detection_yunet_2023mar.onnx` | YuNet face detector (Shiqi Yu et al.) | MIT |
| `face_recognition_sface_2021dec.onnx` | SFace face recognizer (Yaoyao Zhong et al.) | Apache-2.0 |

Not bundled: InsightFace ArcFace (`w600k_r50.onnx`), licensed for
non-commercial research only. Admins who accept that license can place it in
`/var/lib/openhello/models/` themselves (see ARCHITECTURE.md).
