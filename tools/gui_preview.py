#!/usr/bin/env python3
"""
Render every screen of the setup app to PNGs, driven by a FAKE daemon client.

For design review and docs screenshots only — the fake client lives here in
tools/ and is never installed, so the real app can't fake an enrollment.

Headless (no window on your desktop):
  gtk4-broadwayd :9 &
  GDK_BACKEND=broadway BROADWAY_DISPLAY=:9 python3 tools/gui_preview.py OUT_DIR
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Graphene, Gtk  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openhello.gui.application import OpenHelloApplication  # noqa: E402
from openhello.gui.client import ModalityStatus  # noqa: E402
from openhello.gui.window import MainWindow  # noqa: E402

FINGERS = ["right-thumb", "right-index-finger", "left-index-finger"]


class FakeClient(GObject.Object):
    __gsignals__ = {
        "enroll-progress": (GObject.SignalFlags.RUN_FIRST, None, (str, str, int, int, str)),
        "enroll-finished": (GObject.SignalFlags.RUN_FIRST, None, (str, bool, str, str)),
    }
    face_enrolled = False

    def unlock(self, cb):
        cb(None, None)

    def get_status(self, cb):
        cb([ModalityStatus("fingerprint", True, True, "Matched by fprintd."),
            ModalityStatus("face", True, self.face_enrolled, "IR face (preview).")], None)

    def list_fingers(self, cb):
        cb(list(FINGERS), None)

    def enroll_start(self, modality, cb, **_):
        cb(None, None)

    def enroll_cancel(self, cb=None):
        pass

    def remove(self, modality, cb):
        cb(None, None)

    def get_sign_in_methods(self, cb):
        methods = ["fingerprint"] + (["face"] if self.face_enrolled else [])
        cb((methods, methods), None)

    def set_sign_in_methods(self, methods, cb):
        cb(None, None)


def pump(seconds: float) -> None:
    ctx = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.005)


def shoot(win: Gtk.Window, path: Path) -> None:
    pump(0.9)   # let animations settle
    deadline = time.monotonic() + 10
    while True:
        w, h = win.get_width(), win.get_height()
        snap = Gtk.Snapshot()
        Gtk.WidgetPaintable.new(win).snapshot(snap, w, h)
        node = snap.to_node()
        if node is not None and w > 0:
            break
        if time.monotonic() > deadline:
            raise RuntimeError("window never rendered")
        pump(0.1)
    texture = win.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h))
    texture.save_to_png(str(path))
    print("wrote", path)


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    client = FakeClient()

    class PreviewApp(OpenHelloApplication):
        def __init__(self):
            super().__init__()
            # Never hand off to an already-running (e.g. installed) instance of
            # the app: it would just raise its window and we'd write nothing.
            self.set_flags(Gio.ApplicationFlags.NON_UNIQUE)

        def do_activate(self):
            try:
                run()
            finally:
                self.quit()

    app = PreviewApp()

    class LockedClient(FakeClient):
        """Unlock: first call never answers (prompt up), later calls refuse."""
        calls = 0

        def unlock(self, cb):
            LockedClient.calls += 1
            if LockedClient.calls > 1:
                from openhello.gui.client import DaemonError
                cb(None, DaemonError("PermissionDenied", "cancelled"))

    def run():
        locked = MainWindow(app, LockedClient())
        locked.set_default_size(560, 760)
        locked.present()
        shoot(locked, out / "00-locked-waiting.png")
        locked._unlock()
        shoot(locked, out / "00-locked-cancelled.png")
        locked.destroy()

        win = MainWindow(app, client)
        win.set_default_size(560, 760)
        win.present()
        shoot(win, out / "01-overview.png")

        win._begin_setup("fingerprint")
        shoot(win, out / "02-finger-chooser.png")

        win._start("fingerprint", "right-middle-finger", False)
        for i, status in enumerate(["enroll-stage-passed"] * 4 + ["enroll-retry-scan"], 1):
            client.emit("enroll-progress", "fingerprint", status, min(i, 4), 9, "")
            pump(0.15)
        client.emit("enroll-progress", "fingerprint", "enroll-stage-passed", 5, 9, "")
        shoot(win, out / "03-fingerprint-scan.png")

        client.emit("enroll-finished", "fingerprint", True, "", "")
        pump(0.6)
        shoot(win, out / "04-success.png")
        win._back_to_overview()

        win._begin_setup("face")
        shoot(win, out / "05-face-intro.png")
        win._start("face")
        client.emit("enroll-progress", "face", "face-searching", 0, 10, "")
        shoot(win, out / "06-face-searching.png")
        for i in range(1, 7):
            client.emit("enroll-progress", "face", "face-sample", i, 10, "")
            pump(0.1)
        shoot(win, out / "07-face-scan.png")

        client.emit("enroll-finished", "face", False, "DeviceError", "")
        shoot(win, out / "08-failure.png")
        win._back_to_overview()

        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        client.face_enrolled = True
        win.refresh()
        shoot(win, out / "09-overview-dark.png")
        win._start("fingerprint", "right-index-finger", False)
        client.emit("enroll-progress", "fingerprint", "enroll-stage-passed", 7, 9, "")
        shoot(win, out / "10-fingerprint-scan-dark.png")

    app.run([])


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "screenshots"))
