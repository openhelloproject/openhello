"""
Main window: owns navigation and runs one enrollment at a time.

Flow:  locked -> overview -> (finger chooser | face intro) -> scan -> success/failure
The app starts locked and asks the system to authenticate the user every
time it opens (daemon Unlock(), polkit) — the daemon enforces this too.
The window is the only place that talks to DaemonClient; pages just render
state and report user intent through callbacks.
"""

from __future__ import annotations

from gettext import gettext as _

from gi.repository import Adw

from .client import DaemonClient, DaemonError
from .pages.face import FaceIntroPage, FaceScanPage
from .pages.fingerprint import FingerChooserPage, FingerprintScanPage
from .pages.locked import LockedPage
from .pages.overview import TITLES, OverviewPage
from .pages.result import FailurePage, SuccessPage
from .strings import error_text

SUCCESS_TEXT = {
    "fingerprint": (_("Fingerprint sign-in is on"),
                    _("Touch the sensor on the lock screen, at login, or when an app "
                      "asks for your password.")),
    "face": (_("Face sign-in is on"),
             _("Look at the camera on the lock screen, at login, or when an app asks "
               "for your password. Its photo check is still provisional.")),
}


REMOVE_BODY = {
    "fingerprint": _("Your fingerprint will no longer unlock the lock screen, login or "
                     "sudo. To pause it instead, turn it off under “Use for sign-in”. "
                     "Your fingerprints stay registered with your system."),
    "face": _("You can always sign in with your password. Your face data will be "
              "deleted from this computer."),
}


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, client: DaemonClient):
        super().__init__(application=app, title=_("OpenHello"),
                         default_width=560, default_height=720)
        self.set_size_request(360, 480)
        self._client = client
        self._scan_page: FingerprintScanPage | FaceScanPage | None = None
        self._active: str | None = None
        self._retry = None
        self._statuses = []

        self._toasts = Adw.ToastOverlay()
        self._nav = Adw.NavigationView()
        self._toasts.set_child(self._nav)
        self.set_content(self._toasts)

        self._overview = OverviewPage(on_setup=self._begin_setup, on_remove=self._confirm_remove,
                                      on_sign_in_changed=self._on_sign_in_changed)
        self._overview.banner.connect("button-clicked", lambda *_: self.refresh())
        self._locked = LockedPage(on_unlock=self._unlock)
        self._nav.add(self._locked)

        client.connect("enroll-progress", self._on_progress)
        client.connect("enroll-finished", self._on_finished)
        self.connect("close-request", self._on_close)
        self._unlock()

    # -- lock ---------------------------------------------------------------------
    def _unlock(self) -> None:
        self._locked.set_waiting(True)

        def done(_result, err: DaemonError | None):
            if err is None:
                self._nav.replace([self._overview])
                self.refresh()
            elif err.code == "PermissionDenied":
                self._locked.set_waiting(False, _("Authentication was cancelled. "
                                                  "Unlock to change how you sign in."))
            else:
                self._locked.set_waiting(False, error_text(err.code, str(err)))
        self._client.unlock(done)

    # -- overview -------------------------------------------------------------
    def refresh(self) -> None:
        def got_status(statuses, err: DaemonError | None):
            if err is not None:
                self._overview.banner.set_title(error_text(err.code, str(err)))
                self._overview.banner.set_revealed(True)
                return
            self._overview.banner.set_revealed(False)
            self._statuses = statuses
            fp = next((s for s in statuses if s.name == "fingerprint"), None)
            if fp is not None and fp.available:
                self._client.list_fingers(
                    lambda fingers, e: self._overview.show_status(statuses, fingers or []))
            else:
                self._overview.show_status(statuses, [])
            enrolled = [s.name for s in statuses if s.enrolled]

            def got_sign_in(result, err: DaemonError | None):
                if err is None:
                    self._overview.show_sign_in(enrolled, *result)
            self._client.get_sign_in_methods(got_sign_in)
        self._client.get_status(got_status)

    def _on_sign_in_changed(self, modality: str, on: bool) -> None:
        def done(_result, err: DaemonError | None):
            if err is not None:
                self._toast(error_text(err.code, str(err)))
            self.refresh()   # re-sync the switches with what the daemon has

        def got(result, err: DaemonError | None):
            if err is not None:
                done(None, err)
                return
            enabled = [m for m in result[0] if m != modality] + ([modality] if on else [])
            self._client.set_sign_in_methods(enabled, done)
        self._client.get_sign_in_methods(got)

    def _begin_setup(self, modality: str) -> None:
        if modality == "fingerprint":
            self._client.list_fingers(self._show_finger_chooser)
        else:
            self._nav.push(FaceIntroPage(on_start=lambda: self._start("face")))

    def _show_finger_chooser(self, fingers, err: DaemonError | None) -> None:
        in_use = any(s.name == "fingerprint" and s.enrolled for s in self._statuses)
        self._nav.push(FingerChooserPage(
            fingers or [],
            on_choose=lambda finger, reuse: self._start("fingerprint", finger, reuse),
            offer_reuse=not in_use))

    def _confirm_remove(self, modality: str) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Remove {what} sign-in?").format(what=TITLES.get(modality, modality)),
            body=REMOVE_BODY.get(modality, ""))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("remove", _("Remove"))
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_remove_response, modality)
        dialog.present(self)

    def _on_remove_response(self, _dialog, response: str, modality: str) -> None:
        if response == "remove":
            self._remove(modality)

    def _remove(self, modality: str) -> None:
        def done(_result, err: DaemonError | None):
            self._toast(error_text(err.code, str(err)) if err else _("Removed"))
            self.refresh()
        self._client.remove(modality, done)

    # -- enrollment -----------------------------------------------------------
    def _start(self, modality: str, finger: str | None = None, reuse: bool | None = None) -> None:
        self._active = modality
        self._retry = lambda: self._start(modality, finger, reuse)
        if modality == "fingerprint":
            self._scan_page = FingerprintScanPage(finger or "", on_cancel=self._cancel)
        else:
            self._scan_page = FaceScanPage(on_cancel=self._cancel)

        def started(_result, err: DaemonError | None):
            if err is not None:
                self._active = None
                if err.code == "PermissionDenied":
                    self._toast(error_text(err.code))
                else:
                    self._show_failure(error_text(err.code, str(err)))
                return
            self._nav.push(self._scan_page)

        kwargs = {"finger": finger, "reuse_existing": reuse} if modality == "fingerprint" else {}
        self._client.enroll_start(modality, started, **kwargs)

    def _cancel(self) -> None:
        self._client.enroll_cancel()

    def _on_progress(self, _client, modality, status, done, total, _message) -> None:
        if modality == self._active and self._scan_page is not None:
            self._scan_page.update(status, done, total)

    def _on_finished(self, _client, modality, success, error, message) -> None:
        if modality != self._active:
            return
        self._active = None
        if success:
            if isinstance(self._scan_page, FaceScanPage):
                self._scan_page.ring.celebrate()
            title, body = SUCCESS_TEXT[modality]
            self._replace_with(SuccessPage(title, body, on_done=self._back_to_overview))
        elif error == "Cancelled":
            self._back_to_overview()
        else:
            self._show_failure(error_text(error, message))

    def _show_failure(self, message: str) -> None:
        self._replace_with(FailurePage(message, on_retry=lambda: self._retry(),
                                       on_close=self._back_to_overview))

    def _replace_with(self, page) -> None:
        self._nav.pop_to_tag("overview")
        self._nav.push(page)

    def _back_to_overview(self) -> None:
        self._scan_page = None
        self._nav.pop_to_tag("overview")
        self.refresh()

    def _toast(self, text: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=text))

    def _on_close(self, *_):
        if self._active is not None:
            self._client.enroll_cancel()
        return False
