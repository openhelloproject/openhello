"""
Fingerprint modality — a thin wrapper around fprintd's D-Bus API.

We deliberately don't reimplement fingerprint matching: fprintd (backed by
libfprint) already does this well and is what GNOME Settings uses. Prints the
user already enrolled there are reused as-is (ARCHITECTURE.md).

fprintd API (net.reactivated.Fprint, system bus):
  Device.EnrollStart(finger) -> EnrollStatus(result: s, done: b) signals
  Device.VerifyStart(finger) -> VerifyStatus(result: s, done: b) signals
Every *Start must be paired with *Stop, and Claim with Release, even after a
done=True status — otherwise the device stays busy for other clients.

Implementation notes:
  - GDBus, not dbus-python: operations run in the daemon's worker threads,
    each with its own GLib.MainContext, so signal delivery never depends on
    (or blocks) the daemon's main loop.
  - Every call addresses the well-known name. fprintd exits after ~30 s idle
    and is re-activated on demand; binding to its unique name would break
    after the first idle exit.
  - fprintd serves one client at a time. While GNOME's lock screen or another
    verify holds the device, Claim fails with AlreadyInUse -> ModalityError.

Tested hardware: docs/HARDWARE.md.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping
from typing import Any

from gi.repository import Gio, GLib

from .base import (
    Cancelled,
    CancelToken,
    EnrollRejected,
    Modality,
    ModalityError,
    Progress,
    ProgressCallback,
)

log = logging.getLogger(__name__)

FPRINTD = "net.reactivated.Fprint"
MANAGER_PATH = "/net/reactivated/Fprint/Manager"
MANAGER_IFACE = "net.reactivated.Fprint.Manager"
DEVICE_IFACE = "net.reactivated.Fprint.Device"
ERR_NO_PRINTS = "net.reactivated.Fprint.Error.NoEnrolledPrints"

VERIFY_TIMEOUT_S = 20   # keep below pam_openhello's reply timeout
VERIFY_ATTEMPTS = 3     # touches allowed within VERIFY_TIMEOUT_S (pam_fprintd default)
ENROLL_TIMEOUT_S = 90   # whole session (9 touches on the reference sensor)
CANCEL_POLL_MS = 200
RESTART_MIN_INTERVAL_S = 60
WEDGED_MARKER = "has already been opened"
_last_fprintd_restart = float("-inf")

VERIFY_MATCH = "verify-match"
VERIFY_NO_MATCH = "verify-no-match"
ENROLL_COMPLETED = "enroll-completed"
TIMEOUT = "timeout"
CANCELLED = "cancelled"

FINGERS = (
    "left-thumb", "left-index-finger", "left-middle-finger", "left-ring-finger",
    "left-little-finger", "right-thumb", "right-index-finger", "right-middle-finger",
    "right-ring-finger", "right-little-finger",
)


def _remote_error(e: GLib.Error) -> str:
    return Gio.DBusError.get_remote_error(e) or ""


class FingerprintModality(Modality):
    name = "fingerprint"

    def __init__(self, connection: Gio.DBusConnection | None = None):
        self._conn = connection

    # -- D-Bus plumbing -------------------------------------------------------
    @property
    def _bus(self) -> Gio.DBusConnection:
        if self._conn is None:
            self._conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        return self._conn

    def _call(self, path: str, iface: str, method: str, args: GLib.Variant | None = None,
              timeout_ms: int = -1) -> tuple:
        reply = self._bus.call_sync(FPRINTD, path, iface, method, args, None,
                                    Gio.DBusCallFlags.NONE, timeout_ms, None)
        return reply.unpack() if reply is not None else ()

    def _default_device(self) -> str:
        try:
            return self._call(MANAGER_PATH, MANAGER_IFACE, "GetDefaultDevice")[0]
        except GLib.Error as e:
            raise ModalityError(f"no fingerprint device: {e.message}") from e

    def _num_enroll_stages(self, device: str) -> int:
        try:
            v = self._call(device, "org.freedesktop.DBus.Properties", "Get",
                           GLib.Variant("(ss)", (DEVICE_IFACE, "num-enroll-stages")))[0]
            return int(v)
        except (GLib.Error, TypeError, ValueError):
            return 0

    # -- Modality -----------------------------------------------------------
    def is_available(self) -> bool:
        try:
            return len(self._call(MANAGER_PATH, MANAGER_IFACE, "GetDevices")[0]) > 0
        except GLib.Error as e:
            log.warning("fprintd unavailable: %s", e.message)
            return False

    def enrolled_fingers(self, user: str) -> list[str]:
        device = self._default_device()
        try:
            return list(self._call(device, DEVICE_IFACE, "ListEnrolledFingers",
                                   GLib.Variant("(s)", (user,)))[0])
        except GLib.Error as e:
            if _remote_error(e) == ERR_NO_PRINTS:
                return []
            raise ModalityError(e.message) from e

    def enrolled_items(self, user: str) -> list[str]:
        return self.enrolled_fingers(user)

    def is_enrolled(self, user: str) -> bool:
        try:
            return bool(self.enrolled_fingers(user))
        except ModalityError:
            return False

    def enroll(self, user: str, *, progress: ProgressCallback = lambda p: None,
               cancel: CancelToken | None = None,
               options: Mapping[str, Any] | None = None) -> bool:
        """
        options:
          finger (str): which finger to capture (default right-index-finger)
          reuse_existing (bool, default True): if the user already has prints
            in fprintd, accept them without capturing anything.
        """
        options = options or {}
        finger = str(options.get("finger", "right-index-finger"))
        if finger not in FINGERS:
            raise ValueError(f"unknown finger {finger!r}")

        if options.get("reuse_existing", True):
            existing = self.enrolled_fingers(user)
            if existing:
                log.info("fingerprint enroll for %s: reusing fprintd prints %s", user, existing)
                progress(Progress("enroll-completed", 1, 1, "Using fingerprints already enrolled"))
                return True

        device = self._default_device()
        total = self._num_enroll_stages(device)
        passed = 0

        def on_status(result: str) -> None:
            nonlocal passed
            if result in ("enroll-stage-passed", ENROLL_COMPLETED):
                passed += 1
            progress(Progress(result, passed, total))

        status = self._session(
            device, user, "EnrollStatus",
            start=("EnrollStart", GLib.Variant("(s)", (finger,))), stop="EnrollStop",
            timeout_s=ENROLL_TIMEOUT_S, on_status=on_status, cancel=cancel)
        log.info("fingerprint enroll for %s (%s): %s", user, finger, status)
        if status in ("enroll-disconnected", "enroll-unknown-error"):
            raise ModalityError(status)
        if status == "enroll-duplicate":
            # The sensor recognised this print as one already enrolled
            # (possibly registered under a different finger name).
            raise EnrollRejected("duplicate", "this fingerprint is already registered")
        return status == ENROLL_COMPLETED

    def verify(self, user: str, *, cancel: CancelToken | None = None) -> bool:
        device = self._default_device()
        status = self._session(
            device, user, "VerifyStatus",
            start=("VerifyStart", GLib.Variant("(s)", ("any",))), stop="VerifyStop",
            timeout_s=VERIFY_TIMEOUT_S, on_status=None, cancel=cancel,
            attempts=VERIFY_ATTEMPTS)
        log.info("fingerprint verify for %s: %s", user, status)
        if status in (VERIFY_MATCH, VERIFY_NO_MATCH, TIMEOUT):
            return status == VERIFY_MATCH
        raise ModalityError(status)

    def security_note(self) -> str:
        return ("Matched by fprintd/libfprint. On match-on-chip sensors the "
                "fingerprint template never leaves the sensor.")

    # -- one Claim -> Start -> signals -> Stop -> Release session ---------------
    def _session(self, device: str, user: str, signal: str, *,
                 start: tuple[str, GLib.Variant], stop: str, timeout_s: int,
                 on_status: Callable[[str], None] | None,
                 cancel: CancelToken | None, attempts: int = 1) -> str:
        """Claim once, then run up to `attempts` Start/Stop rounds while the
        result is a clean no-match, all within one timeout_s budget (like
        pam_fprintd's max-tries: a sloppy first touch shouldn't end sign-in)."""
        requested_at = time.monotonic()
        device = self._claim(device, user)
        deadline = time.monotonic() + timeout_s
        try:
            for attempt in range(attempts):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    status = TIMEOUT
                    break
                status = self._run_until_done(device, signal, start, stop, remaining,
                                              on_status, cancel,
                                              requested_at=requested_at if attempt == 0 else None)
                if status != VERIFY_NO_MATCH or attempt == attempts - 1:
                    break
                log.debug("fingerprint no-match, attempt %d/%d", attempt + 1, attempts)
        finally:
            try:
                self._call(device, DEVICE_IFACE, "Release")
            except GLib.Error as e:
                log.debug("Release failed: %s", e.message)
        if status == CANCELLED:
            raise Cancelled()
        return status

    def _claim(self, device: str, user: str) -> str:
        """Claim the device; returns the (possibly new) device path. Recovers
        from the one known fprintd failure that persists until it restarts
        (see _restart_fprintd) by restarting it once and retrying."""
        try:
            self._call(device, DEVICE_IFACE, "Claim", GLib.Variant("(s)", (user,)))
            return device
        except GLib.Error as e:
            if not (WEDGED_MARKER in e.message and self._restart_fprintd()):
                raise ModalityError(f"Claim failed: {e.message}") from e
        device = self._default_device()
        try:
            self._call(device, DEVICE_IFACE, "Claim", GLib.Variant("(s)", (user,)))
        except GLib.Error as e:
            raise ModalityError(f"Claim failed after restarting fprintd: {e.message}") from e
        return device

    def _restart_fprintd(self) -> bool:
        """
        fprintd 1.94 can get stuck after a verify is interrupted by suspend
        ("Cannot run while suspended"): every later Claim fails with
        "Open failed with error: The device has already been opened!" until
        fprintd restarts, and while any client keeps it busy it never idles
        out. fprintd keeps no state of its own, so restarting it is safe.
        Root only, at most once a minute, logged.
        """
        global _last_fprintd_restart
        now = time.monotonic()
        if os.geteuid() != 0 or now - _last_fprintd_restart < RESTART_MIN_INTERVAL_S:
            return False
        _last_fprintd_restart = now
        log.warning("fprintd is wedged (device reported as already open); restarting it")
        try:
            self._bus.call_sync(
                "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
                "org.freedesktop.systemd1.Manager", "RestartUnit",
                GLib.Variant("(ss)", ("fprintd.service", "replace")), None,
                Gio.DBusCallFlags.NONE, -1, None)
        except GLib.Error as e:
            log.error("could not restart fprintd: %s", e.message)
            return False
        deadline = now + 10
        while time.monotonic() < deadline:   # wait for it to serve again
            if self.is_available():
                return True
            time.sleep(0.2)
        log.error("fprintd did not come back after restart")
        return False

    def _run_until_done(self, device, signal, start, stop, timeout_s, on_status, cancel,
                        requested_at: float | None = None) -> str:
        ctx = GLib.MainContext.new()
        ctx.push_thread_default()   # signal callbacks dispatch to this context
        loop = GLib.MainLoop.new(ctx, False)
        final = {"status": None}
        sources = []

        def finish(status: str) -> None:
            if final["status"] is None:
                final["status"] = status
                loop.quit()

        def handler(_conn, _sender, _path, _iface, _signal, params):
            result, done = params.unpack()
            log.debug("%s: %s (done=%s)", signal, result, done)
            if on_status is not None:
                on_status(result)
            if done:
                finish(result)

        def add_timeout(ms: int, fn) -> None:
            src = GLib.timeout_source_new(ms)
            src.set_callback(fn)
            src.attach(ctx)
            sources.append(src)

        def on_timeout(*_):
            finish(TIMEOUT)
            return GLib.SOURCE_REMOVE

        def on_cancel_poll(*_):
            if cancel is not None and cancel.cancelled:
                finish(CANCELLED)
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        sub = self._bus.signal_subscribe(FPRINTD, DEVICE_IFACE, signal, device, None,
                                         Gio.DBusSignalFlags.NONE, handler)
        started = False
        try:
            add_timeout(max(1, int(timeout_s * 1000)), on_timeout)
            add_timeout(CANCEL_POLL_MS, on_cancel_poll)
            try:
                self._call(device, DEVICE_IFACE, start[0], start[1])
            except GLib.Error as e:
                raise ModalityError(f"{start[0]} failed: {e.message}") from e
            started = True
            if requested_at is not None:
                # how long the user waits before a touch can register at all
                log.info("fingerprint sensor ready %.2f s after the request",
                         time.monotonic() - requested_at)
            loop.run()
        finally:
            self._bus.signal_unsubscribe(sub)
            for src in sources:
                src.destroy()
            if started:
                try:
                    self._call(device, DEVICE_IFACE, stop)
                except GLib.Error as e:
                    log.debug("%s failed: %s", stop, e.message)
            ctx.pop_thread_default()
        return final["status"]
