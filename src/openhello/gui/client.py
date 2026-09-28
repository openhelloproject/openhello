"""
Asynchronous client for org.openhello.Daemon1 (see
daemon/org.openhello.Daemon1.xml for the contract).

Deliberately independent of the daemon package, so the GUI can ship on its
own (e.g. as a Flatpak talking to the system daemon). All calls are async on
the GTK main loop; enrollment progress arrives as GObject signals.

OPENHELLO_BUS=session points it at a development daemon on the session bus.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from gi.repository import Gio, GLib, GObject

BUS_NAME = "org.openhello.Daemon1"
OBJECT_PATH = "/org/openhello/Daemon1"
INTERFACE = "org.openhello.Daemon1"
ERROR_PREFIX = "org.openhello.Error."

# Unlock may sit behind a polkit password prompt for a while.
ENROLL_START_TIMEOUT_MS = 5 * 60 * 1000
CALL_TIMEOUT_MS = 30 * 1000


@dataclass(frozen=True)
class ModalityStatus:
    name: str
    available: bool
    enrolled: bool
    security_note: str


class DaemonError(Exception):
    """code: the org.openhello.Error.<Code> suffix, or "ServiceUnavailable"
    when the daemon can't be reached at all."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    @classmethod
    def from_glib(cls, e: GLib.Error) -> DaemonError:
        remote = Gio.DBusError.get_remote_error(e) or ""
        if remote.startswith(ERROR_PREFIX):
            return cls(remote[len(ERROR_PREFIX):], Gio.DBusError.strip_remote_error(e) or e.message)
        if remote in ("org.freedesktop.DBus.Error.ServiceUnknown",
                      "org.freedesktop.DBus.Error.NameHasNoOwner",
                      "org.freedesktop.systemd1.NoSuchUnit"):
            return cls("ServiceUnavailable", "The OpenHello service isn't installed or running.")
        return cls("Failed", e.message)


Callback = Callable[[Any, DaemonError | None], None]


def _bus_type() -> Gio.BusType:
    return Gio.BusType.SESSION if os.environ.get("OPENHELLO_BUS") == "session" \
        else Gio.BusType.SYSTEM


class DaemonClient(GObject.Object):
    __gsignals__ = {
        # modality, status, done, total, message
        "enroll-progress": (GObject.SignalFlags.RUN_FIRST, None, (str, str, int, int, str)),
        # modality, success, error_code, message
        "enroll-finished": (GObject.SignalFlags.RUN_FIRST, None, (str, bool, str, str)),
    }

    def __init__(self, connection: Gio.DBusConnection | None = None):
        super().__init__()
        self._conn = connection or Gio.bus_get_sync(_bus_type(), None)
        self._sub = self._conn.signal_subscribe(
            BUS_NAME, INTERFACE, None, OBJECT_PATH, None,
            Gio.DBusSignalFlags.NONE, self._on_signal)

    def close(self) -> None:
        if self._sub:
            self._conn.signal_unsubscribe(self._sub)
            self._sub = 0

    def _on_signal(self, _conn, _sender, _path, _iface, name, params):
        args = params.unpack()
        if name == "EnrollProgress":
            self.emit("enroll-progress", *args)
        elif name == "EnrollFinished":
            self.emit("enroll-finished", *args)

    # -- plumbing ---------------------------------------------------------------
    def _call(self, method: str, args: GLib.Variant | None, reply_type: str | None,
              callback: Callback, convert: Callable[[tuple], Any] = lambda r: None,
              timeout_ms: int = CALL_TIMEOUT_MS) -> None:
        def done(conn, result):
            try:
                reply = conn.call_finish(result)
            except GLib.Error as e:
                callback(None, DaemonError.from_glib(e))
                return
            callback(convert(reply.unpack() if reply is not None else ()), None)

        self._conn.call(BUS_NAME, OBJECT_PATH, INTERFACE, method, args,
                        GLib.VariantType(reply_type) if reply_type else None,
                        Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION,
                        timeout_ms, None, done)

    # -- API --------------------------------------------------------------------
    def unlock(self, callback: Callback) -> None:
        """Ask the system to authenticate the user (polkit; every time). All
        other calls fail with PermissionDenied until this succeeds."""
        self._call("Unlock", None, None, callback, timeout_ms=ENROLL_START_TIMEOUT_MS)

    def get_status(self, callback: Callback) -> None:
        self._call("GetStatus", None, "(a(sbbs))", callback,
                   lambda r: [ModalityStatus(*row) for row in r[0]])

    def list_fingers(self, callback: Callback) -> None:
        self._call("ListFingers", None, "(as)", callback, lambda r: list(r[0]))

    def enroll_start(self, modality: str, callback: Callback, *,
                     finger: str | None = None, reuse_existing: bool | None = None) -> None:
        options: dict[str, GLib.Variant] = {}
        if finger is not None:
            options["finger"] = GLib.Variant("s", finger)
        if reuse_existing is not None:
            options["reuse_existing"] = GLib.Variant("b", reuse_existing)
        self._call("EnrollStart", GLib.Variant("(sa{sv})", (modality, options)), None,
                   callback, timeout_ms=ENROLL_START_TIMEOUT_MS)

    def enroll_cancel(self, callback: Callback | None = None) -> None:
        self._call("EnrollCancel", None, None, callback or (lambda *_: None))

    def get_sign_in_methods(self, callback: Callback) -> None:
        """callback((enabled, supported), err)"""
        self._call("GetSignInMethods", None, "(asas)", callback,
                   lambda r: (list(r[0]), list(r[1])))

    def set_sign_in_methods(self, methods: list[str], callback: Callback) -> None:
        self._call("SetSignInMethods", GLib.Variant("(as)", (methods,)), None, callback)

    def remove(self, modality: str, callback: Callback) -> None:
        self._call("Remove", GLib.Variant("(s)", (modality,)), None, callback)
