"""
D-Bus adapter: exposes daemon.orchestrator.Orchestrator as
org.openhello.Daemon1 (API contract: org.openhello.Daemon1.xml).

Responsibilities, and only these:
  - resolve who is calling (uid from the bus, never from arguments),
  - authorize: settings methods need an Unlock()ed connection (polkit, asks
    every time the app opens); PAM's methods need root,
  - run each call on a worker thread so the main loop never blocks,
  - map OpenHelloError codes to org.openhello.Error.* names,
  - deliver enrollment progress as signals addressed to the caller only,
  - stop biometric scans nobody is waiting for: when the caller disconnects
    (GDM dropping a lock-screen conversation, Ctrl-C in sudo) and when the
    system is about to suspend (logind PrepareForSleep); a verify still
    running when the system suspends can leave fprintd stuck.
"""

from __future__ import annotations

import logging
import os
import pwd
import threading
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from gi.repository import Gio, GLib

from .. import __version__
from ..backends.base import CancelToken, Progress
from ..core.authtoken import NONCE_LEN
from ..core.gcompat import register_object
from ..core.users import InvalidUsername, is_valid_username
from .orchestrator import OpenHelloError, Orchestrator

log = logging.getLogger(__name__)

BUS_NAME = "org.openhello.Daemon1"
OBJECT_PATH = "/org/openhello/Daemon1"
INTERFACE = "org.openhello.Daemon1"
ERROR_PREFIX = "org.openhello.Error."

# One action gates the whole settings API. auth_self without "keep" in the
# policy, so polkit asks on every Unlock() — i.e. every time the app opens.
ACTION_SETTINGS = "org.openhello.settings"

LOGIND_NAME = "org.freedesktop.login1"
LOGIND_PATH = "/org/freedesktop/login1"
LOGIND_MANAGER_IFACE = "org.freedesktop.login1.Manager"

POLKIT_NAME = "org.freedesktop.PolicyKit1"
POLKIT_PATH = "/org/freedesktop/PolicyKit1/Authority"
POLKIT_IFACE = "org.freedesktop.PolicyKit1.Authority"
POLKIT_ALLOW_USER_INTERACTION = 0x1
POLKIT_TIMEOUT_MS = 5 * 60 * 1000   # the user may take a while to type a password

_ERROR_NAMES = {
    "not-available": "NotAvailable",
    "not-implemented": "NotImplemented",
    "device-error": "DeviceError",
    "enroll-failed": "EnrollFailed",
    "cancelled": "Cancelled",
    "busy": "Busy",
    "unknown-modality": "UnknownModality",
    "invalid-argument": "InvalidArgument",
    "permission-denied": "PermissionDenied",
    "not-enrolled": "NotEnrolled",
    "duplicate": "Duplicate",
}


def error_name(code: str) -> str:
    return _ERROR_NAMES.get(code, "Failed")


@dataclass(frozen=True)
class Caller:
    sender: str
    uid: int
    user: str


@dataclass
class _EnrollJob:
    caller: Caller
    modality: str
    cancel: CancelToken = field(default_factory=CancelToken)
    watch_id: int = 0


class DaemonService:
    def __init__(self, orchestrator: Orchestrator, connection: Gio.DBusConnection):
        self._orch = orchestrator
        self._conn = connection
        xml = resources.files(__package__).joinpath("org.openhello.Daemon1.xml").read_text()
        self._iface = Gio.DBusNodeInfo.new_for_xml(xml).lookup_interface(INTERFACE)
        # uids allowed to call Authenticate: root, plus the daemon's own uid so
        # unprivileged dev/test runs work (a real install runs as root).
        self._auth_uids = {0, os.geteuid()}
        self._job: _EnrollJob | None = None
        self._job_lock = threading.Lock()
        self._reg_id = 0
        self._sleep_sub = 0
        self._active_auth: set[CancelToken] = set()
        self._auth_lock = threading.Lock()
        # bus names that passed Unlock(), until they disconnect
        self._unlocked: set[str] = set()
        self._unlock_lock = threading.Lock()

    # -- registration ---------------------------------------------------------
    def register(self) -> None:
        self._reg_id = register_object(self._conn, OBJECT_PATH, self._iface,
                                       self._on_method_call, self._on_get_property)
        self._sleep_sub = self._conn.signal_subscribe(
            LOGIND_NAME, LOGIND_MANAGER_IFACE, "PrepareForSleep", LOGIND_PATH, None,
            Gio.DBusSignalFlags.NONE, self._on_prepare_for_sleep)

    def unregister(self) -> None:
        if self._sleep_sub:
            self._conn.signal_unsubscribe(self._sleep_sub)
            self._sleep_sub = 0
        if self._reg_id:
            self._conn.unregister_object(self._reg_id)
            self._reg_id = 0

    def _on_prepare_for_sleep(self, _conn, _sender, _path, _iface, _signal, params):
        (going_to_sleep,) = params.unpack()
        if not going_to_sleep:
            return
        with self._auth_lock:
            tokens = list(self._active_auth)
        with self._job_lock:
            job = self._job
        if tokens or job:
            log.info("system suspending: cancelling %d sign-in(s)%s", len(tokens),
                     " and an enrollment" if job else "")
        for token in tokens:
            token.cancel()
        if job is not None:
            job.cancel.cancel()

    def _on_get_property(self, _conn, _sender, _path, _iface, name):
        if name == "Version":
            return GLib.Variant("s", __version__)
        return None

    def _on_method_call(self, _conn, sender, _path, _iface, method, params, invocation):
        handler = getattr(self, f"_m_{method}", None)
        if handler is None:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
            return
        kwargs, cleanup = {}, None
        if method == "Unlock":
            # Forget the unlock when the app closes. Watch set up here, on the
            # main-loop thread; harmless if the Unlock then fails.
            def forget(*_):
                with self._unlock_lock:
                    self._unlocked.discard(sender)
                Gio.bus_unwatch_name(watch)
            watch = Gio.bus_watch_name_on_connection(
                self._conn, sender, Gio.BusNameWatcherFlags.NONE, None, forget)
        if method == "Authenticate":
            # Set up here, on the service's main-loop thread, so the watch
            # fires on the loop that actually runs.
            token = CancelToken()
            watch = Gio.bus_watch_name_on_connection(
                self._conn, sender, Gio.BusNameWatcherFlags.NONE, None,
                lambda *_: token.cancel())
            with self._auth_lock:
                self._active_auth.add(token)
            kwargs["cancel"] = token

            def cleanup():
                Gio.bus_unwatch_name(watch)
                with self._auth_lock:
                    self._active_auth.discard(token)
        threading.Thread(target=self._dispatch, name=f"dbus-{method}",
                         args=(handler, sender, params.unpack(), invocation, kwargs, cleanup),
                         daemon=True).start()

    def _dispatch(self, handler, sender: str, args: tuple, invocation,
                  kwargs: dict | None = None, cleanup=None) -> None:
        try:
            result = handler(self._caller(sender), *args, **(kwargs or {}))
            invocation.return_value(result)
        except OpenHelloError as e:
            invocation.return_dbus_error(ERROR_PREFIX + error_name(e.code), str(e))
        except InvalidUsername as e:
            invocation.return_dbus_error(ERROR_PREFIX + "InvalidArgument", str(e))
        except Exception:
            log.exception("%s failed", handler.__name__)
            invocation.return_dbus_error(ERROR_PREFIX + "Failed", "internal error (see journal)")
        finally:
            if cleanup is not None:
                cleanup()

    # -- caller identity & authorization ---------------------------------------
    def _caller(self, sender: str) -> Caller:
        reply = self._conn.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "GetConnectionUnixUser", GLib.Variant("(s)", (sender,)),
            GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, -1, None)
        uid = reply.unpack()[0]
        try:
            user = pwd.getpwuid(uid).pw_name
        except KeyError:
            user = ""
        return Caller(sender, uid, user)

    def _authorize(self, caller: Caller, action: str) -> None:
        if caller.uid == 0:
            return
        subject = ("system-bus-name", {"name": GLib.Variant("s", caller.sender)})
        try:
            reply = self._conn.call_sync(
                POLKIT_NAME, POLKIT_PATH, POLKIT_IFACE, "CheckAuthorization",
                GLib.Variant("((sa{sv})sa{ss}us)",
                             (subject, action, {}, POLKIT_ALLOW_USER_INTERACTION, "")),
                GLib.VariantType("((bba{ss}))"), Gio.DBusCallFlags.NONE,
                POLKIT_TIMEOUT_MS, None)
        except GLib.Error as e:
            log.warning("polkit check for %s failed: %s", action, e.message)
            raise OpenHelloError("permission-denied", "authorization check failed") from e
        authorized, _challenge, _details = reply.unpack()[0]
        if not authorized:
            raise OpenHelloError("permission-denied", f"not authorized for {action}")

    def _require_unlocked(self, caller: Caller) -> None:
        if caller.uid == 0:
            return
        with self._unlock_lock:
            if caller.sender in self._unlocked:
                return
        raise OpenHelloError("permission-denied", "locked: call Unlock() first")

    @staticmethod
    def _require_user(caller: Caller) -> str:
        if not is_valid_username(caller.user):
            raise OpenHelloError("invalid-argument", f"unsupported account for uid {caller.uid}")
        return caller.user

    # -- methods ----------------------------------------------------------------
    def _m_Unlock(self, caller: Caller):
        self._require_user(caller)
        self._authorize(caller, ACTION_SETTINGS)
        with self._unlock_lock:
            self._unlocked.add(caller.sender)
        log.info("settings unlocked for %s", caller.user)
        return None

    def _m_GetStatus(self, caller: Caller):
        self._require_unlocked(caller)
        rows = [(s.name, s.available, s.enrolled, s.security_note)
                for s in self._orch.status(self._require_user(caller))]
        return GLib.Variant("(a(sbbs))", (rows,))

    def _m_ListFingers(self, caller: Caller):
        self._require_unlocked(caller)
        fingers = self._orch.enrolled_items(self._require_user(caller), "fingerprint")
        return GLib.Variant("(as)", (fingers,))

    def _m_EnrollStart(self, caller: Caller, modality: str, options: dict[str, Any]):
        user = self._require_user(caller)
        if modality not in self._orch.modality_names:
            raise OpenHelloError("unknown-modality", f"unknown modality {modality!r}")
        opts = self._validate_enroll_options(options)
        self._require_unlocked(caller)

        with self._job_lock:
            if self._job is not None:
                raise OpenHelloError("busy", "another enrollment is in progress")
            job = self._job = _EnrollJob(caller, modality)
        # If the client disappears (GUI closed), cancel its enrollment.
        job.watch_id = Gio.bus_watch_name_on_connection(
            self._conn, caller.sender, Gio.BusNameWatcherFlags.NONE,
            None, lambda *_: job.cancel.cancel())
        threading.Thread(target=self._run_enroll, args=(job, user, opts),
                         name="enroll", daemon=True).start()
        return None

    def _m_EnrollCancel(self, caller: Caller):
        self._require_unlocked(caller)
        with self._job_lock:
            job = self._job
        if job is None or job.caller.sender != caller.sender:
            raise OpenHelloError("invalid-argument", "no enrollment of yours is running")
        job.cancel.cancel()
        return None

    def _m_Remove(self, caller: Caller, modality: str):
        user = self._require_user(caller)
        if modality not in self._orch.modality_names:
            raise OpenHelloError("unknown-modality", f"unknown modality {modality!r}")
        self._require_unlocked(caller)
        self._orch.remove(user, modality)
        return None

    def _m_GetSignInMethods(self, caller: Caller):
        self._require_unlocked(caller)
        enabled, supported = self._orch.sign_in_methods(self._require_user(caller))
        return GLib.Variant("(asas)", (enabled, supported))

    def _m_SetSignInMethods(self, caller: Caller, methods: list[str]):
        user = self._require_user(caller)
        self._require_unlocked(caller)
        self._orch.set_sign_in(user, list(methods))
        return None

    def _require_pam_caller(self, caller: Caller, method: str) -> None:
        if caller.uid not in self._auth_uids:
            raise OpenHelloError("permission-denied",
                                 f"{method} is for pam_openhello (root) only")

    def _m_GetAuthMethods(self, caller: Caller, user: str):
        self._require_pam_caller(caller, "GetAuthMethods")
        return GLib.Variant("(as)", (self._orch.auth_methods(user),))

    def _m_Authenticate(self, caller: Caller, user: str, nonce: bytes, service: str,
                        cancel: CancelToken | None = None):
        self._require_pam_caller(caller, "Authenticate")
        nonce = bytes(nonce)
        if len(nonce) != NONCE_LEN:
            raise OpenHelloError("invalid-argument", f"nonce must be {NONCE_LEN} bytes")
        if "\0" in service or len(service) > 255:
            raise OpenHelloError("invalid-argument", "bad service name")
        result = self._orch.authenticate(user, nonce, service, cancel=cancel)
        return GLib.Variant("(bays)", (result.ok, result.signature, result.reason))

    # -- enrollment job -----------------------------------------------------------
    @staticmethod
    def _validate_enroll_options(options: dict[str, Any]) -> dict[str, Any]:
        allowed = {"finger": str, "reuse_existing": bool}
        out = {}
        for key, value in options.items():
            if key not in allowed or not isinstance(value, allowed[key]):
                raise OpenHelloError("invalid-argument", f"bad option {key!r}")
            out[key] = value
        return out

    def _emit(self, job: _EnrollJob, signal: str, fmt: str, values: tuple) -> None:
        try:
            self._conn.emit_signal(job.caller.sender, OBJECT_PATH, INTERFACE, signal,
                                   GLib.Variant(fmt, values))
        except GLib.Error as e:
            log.debug("emit %s failed: %s", signal, e.message)

    def _run_enroll(self, job: _EnrollJob, user: str, options: dict[str, Any]) -> None:
        def progress(p: Progress) -> None:
            self._emit(job, "EnrollProgress", "(ssuus)",
                       (job.modality, p.status, max(p.done, 0), max(p.total, 0), p.message))

        ok, code, message = True, "", ""
        try:
            self._orch.enroll(user, job.modality, progress=progress,
                              cancel=job.cancel, options=options)
        except OpenHelloError as e:
            ok, code, message = False, error_name(e.code), str(e)
        except Exception:
            log.exception("enrollment crashed")
            ok, code, message = False, "Failed", "internal error (see journal)"
        finally:
            Gio.bus_unwatch_name(job.watch_id)
            with self._job_lock:
                self._job = None
        self._emit(job, "EnrollFinished", "(sbss)", (job.modality, ok, code, message))
