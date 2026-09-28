"""
org.openhello.Daemon1 over a real (private) D-Bus daemon, with real TPM
sealing on swtpm and a mock polkit authority. Only the biometric match is a
stub (StubModality).
"""
import shutil
import threading
import time

import pytest

pytest.importorskip("tpm2_pytss")
if shutil.which("swtpm") is None or shutil.which("dbus-daemon") is None:
    pytest.skip("needs swtpm and dbus-daemon", allow_module_level=True)

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # noqa: E402
from gi.repository import Gio, GLib  # noqa: E402

from conftest import SWTPM  # noqa: E402
from openhello.backends.base import Cancelled, EnrollRejected, Modality, Progress  # noqa: E402
from openhello.core import authtoken  # noqa: E402
from openhello.core.gcompat import register_object  # noqa: E402
from openhello.daemon.orchestrator import Orchestrator  # noqa: E402
from openhello.daemon.service import BUS_NAME, INTERFACE, OBJECT_PATH, DaemonService  # noqa: E402
from openhello.tpm.keystore import TPMKeystore  # noqa: E402

POLKIT_XML = """
<node><interface name="org.freedesktop.PolicyKit1.Authority">
  <method name="CheckAuthorization">
    <arg type="(sa{sv})" direction="in"/><arg type="s" direction="in"/>
    <arg type="a{ss}" direction="in"/><arg type="u" direction="in"/>
    <arg type="s" direction="in"/><arg type="(bba{ss})" direction="out"/>
  </method>
</interface></node>"""


class StubModality(Modality):
    def __init__(self, name):
        self.name = name
        self.match = True
        self.block_until_cancel = False
        self.enrolled = set()

    def is_available(self):
        return True

    def is_enrolled(self, user):
        return user in self.enrolled

    def enroll(self, user, *, progress=lambda p: None, cancel=None, options=None):
        if getattr(self, "reject", None):
            raise EnrollRejected(*self.reject)
        if self.block_until_cancel:
            while not cancel.cancelled:
                time.sleep(0.02)
            raise Cancelled()
        for i in range(1, 4):
            progress(Progress("stub-step", i, 3, f"step {i}"))
        self.enrolled.add(user)
        return True

    def verify(self, user, *, cancel=None):
        self.verify_calls = getattr(self, "verify_calls", 0) + 1
        if getattr(self, "verify_blocks", False):   # user never touches the sensor
            while not cancel.cancelled:
                time.sleep(0.02)
            self.verify_cancelled = True
            raise Cancelled()
        return self.match

    def remove(self, user):
        self.enrolled.discard(user)

    def enrolled_items(self, user):
        return ["right-index-finger"] if user in self.enrolled else []


class ServiceThread:
    """Runs the service (and a mock polkit) on their own GLib context."""

    def __init__(self, bus, orchestrator):
        self.polkit_allow = True
        self.polkit_actions = []
        ready = threading.Event()

        def run():
            ctx = GLib.MainContext.new()
            ctx.push_thread_default()
            self.loop = GLib.MainLoop.new(ctx, False)
            self.conn = bus.connect()
            self.service = DaemonService(orchestrator, self.conn)
            self.service.register()
            self.conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                "org.freedesktop.DBus", "RequestName",
                                GLib.Variant("(su)", (BUS_NAME, 0)), None,
                                Gio.DBusCallFlags.NONE, -1, None)
            iface = Gio.DBusNodeInfo.new_for_xml(POLKIT_XML).interfaces[0]
            register_object(self.conn, "/org/freedesktop/PolicyKit1/Authority", iface,
                            self._polkit)
            self.conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                "org.freedesktop.DBus", "RequestName",
                                GLib.Variant("(su)", ("org.freedesktop.PolicyKit1", 0)), None,
                                Gio.DBusCallFlags.NONE, -1, None)
            ready.set()
            self.loop.run()
            ctx.pop_thread_default()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        assert ready.wait(10)

    def _polkit(self, _c, _s, _p, _i, _m, params, invocation):
        self.polkit_actions.append(params.unpack()[1])
        invocation.return_value(GLib.Variant("((bba{ss}))", ((self.polkit_allow, False, {}),)))

    def stop(self):
        """Stop serving and drop the bus connection, releasing the name (so
        callers see "no such service", not a hang)."""
        if self.thread.is_alive():
            self.loop.quit()
            self.thread.join(5)
            self.conn.close_sync(None)


class Client:
    """A settings client. Like the app, it Unlock()s on connect unless told
    not to (tests of the lock itself)."""

    def __init__(self, bus, unlock=True):
        self.ctx = GLib.MainContext.new()
        self.ctx.push_thread_default()
        self.conn = bus.connect()
        self.signals = []
        self._seen = {}
        self.conn.signal_subscribe(BUS_NAME, INTERFACE, None, OBJECT_PATH, None,
                                   Gio.DBusSignalFlags.NONE,
                                   lambda *a: self.signals.append((a[4], a[5].unpack())))
        self.ctx.pop_thread_default()
        if unlock:
            self.call("Unlock")

    def call(self, method, args=None, fmt=None):
        reply = self.conn.call_sync(BUS_NAME, OBJECT_PATH, INTERFACE, method,
                                    GLib.Variant(fmt, args) if fmt else None, None,
                                    Gio.DBusCallFlags.NONE, 10000, None)
        return reply.unpack() if reply else ()

    def wait_signal(self, name, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for i, s in enumerate(self.signals):
                if s[0] == name and i >= self._seen.get(name, 0):
                    self._seen[name] = i + 1   # consume: next wait gets a newer one
                    return s[1]
            self.ctx.iteration(False)
            time.sleep(0.01)
        raise AssertionError(f"no {name} signal; got {self.signals}")


def dbus_error(excinfo) -> str:
    return Gio.DBusError.get_remote_error(excinfo.value)


@pytest.fixture
def env(tmp_path, monkeypatch, private_bus):
    monkeypatch.setenv("OPENHELLO_STATE_DIR", str(tmp_path / "state"))
    tpm = SWTPM(tmp_path / "tpm")
    fp, face = StubModality("fingerprint"), StubModality("face")
    orch = Orchestrator(TPMKeystore(tcti=tpm.tcti), {"fingerprint": fp, "face": face})
    svc = ServiceThread(private_bus, orch)
    client = Client(private_bus)
    yield {"svc": svc, "client": client, "fp": fp, "face": face, "bus": private_bus,
           "state": tmp_path / "state", "tcti": tpm.tcti}
    svc.stop()
    tpm.stop()


def enroll(client, modality="fingerprint", options=None):
    client.call("EnrollStart", (modality, options or {}), "(sa{sv})")
    return client.wait_signal("EnrollFinished")


def test_status_before_enrollment(env):
    (rows,) = env["client"].call("GetStatus")
    assert {r[0]: (r[1], r[2]) for r in rows} == {
        "fingerprint": (True, False), "face": (True, False)}


def test_enroll_emits_progress_then_finished(env):
    c = env["client"]
    assert enroll(c) == ("fingerprint", True, "", "")
    progress = [s[1] for s in c.signals if s[0] == "EnrollProgress"]
    assert [(p[2], p[3]) for p in progress] == [(1, 3), (2, 3), (3, 3)]
    assert env["svc"].polkit_actions == ["org.openhello.settings"]   # the Unlock only
    (rows,) = c.call("GetStatus")
    assert dict((r[0], r[2]) for r in rows)["fingerprint"] is True
    assert c.call("ListFingers") == (["right-index-finger"],)


def test_polkit_denial_keeps_settings_locked(env):
    env["svc"].polkit_allow = False          # user cancels / wrong password
    c = Client(env["bus"], unlock=False)
    with pytest.raises(GLib.Error) as e:
        c.call("Unlock")
    assert dbus_error(e) == "org.openhello.Error.PermissionDenied"
    with pytest.raises(GLib.Error) as e:
        c.call("EnrollStart", ("fingerprint", {}), "(sa{sv})")
    assert dbus_error(e) == "org.openhello.Error.PermissionDenied"
    assert not env["state"].exists() or not any(env["state"].rglob("sealed.pub"))


@pytest.mark.parametrize("method,args,fmt", [
    ("GetStatus", None, None),
    ("ListFingers", None, None),
    ("GetSignInMethods", None, None),
    ("EnrollStart", ("fingerprint", {}), "(sa{sv})"),
    ("EnrollCancel", None, None),
    ("Remove", ("fingerprint",), "(s)"),
    ("SetSignInMethods", ([],), "(as)"),
])
def test_settings_are_locked_until_unlock(env, method, args, fmt):
    with pytest.raises(GLib.Error) as e:
        Client(env["bus"], unlock=False).call(method, args, fmt)
    assert dbus_error(e) == "org.openhello.Error.PermissionDenied"


def test_unlock_is_per_app_launch(env):
    """Asked every time: each connection (app launch) needs its own Unlock,
    and each Unlock goes to polkit again — nothing is cached."""
    env["svc"].polkit_actions.clear()
    Client(env["bus"])
    Client(env["bus"])
    assert env["svc"].polkit_actions == ["org.openhello.settings"] * 2
    with pytest.raises(GLib.Error):
        Client(env["bus"], unlock=False).call("GetStatus")   # unlocking A doesn't unlock B


def test_closing_the_app_forgets_the_unlock(env):
    c = Client(env["bus"])
    name = c.conn.get_unique_name()
    assert name in env["svc"].service._unlocked
    c.conn.close_sync(None)
    _wait_for(lambda: name not in env["svc"].service._unlocked)


def test_signals_go_only_to_the_caller(env):
    other = Client(env["bus"])
    enroll(env["client"])
    for _ in range(20):
        other.ctx.iteration(False)
    assert other.signals == []


def test_busy_and_cancel(env):
    env["fp"].block_until_cancel = True
    c = env["client"]
    c.call("EnrollStart", ("fingerprint", {}), "(sa{sv})")
    with pytest.raises(GLib.Error) as e:
        Client(env["bus"]).call("EnrollStart", ("face", {}), "(sa{sv})")
    assert dbus_error(e) == "org.openhello.Error.Busy"
    with pytest.raises(GLib.Error) as e:     # someone else can't cancel it
        Client(env["bus"]).call("EnrollCancel")
    assert dbus_error(e) == "org.openhello.Error.InvalidArgument"
    c.call("EnrollCancel")
    assert c.wait_signal("EnrollFinished")[:3] == ("fingerprint", False, "Cancelled")
    env["fp"].block_until_cancel = False
    assert enroll(c)[1] is True  # device free again


@pytest.mark.parametrize("modality,options,error", [
    ("iris", {}, "UnknownModality"),
    ("fingerprint", {"finger": GLib.Variant("i", 3)}, "InvalidArgument"),
    ("fingerprint", {"bogus": GLib.Variant("b", True)}, "InvalidArgument"),
])
def test_enroll_rejects_bad_arguments(env, modality, options, error):
    with pytest.raises(GLib.Error) as e:
        env["client"].call("EnrollStart", (modality, options), "(sa{sv})")
    assert dbus_error(e) == f"org.openhello.Error.{error}"



def test_authenticate_signature_verifies(env):
    c = env["client"]
    enroll(c)
    import getpass
    user = getpass.getuser()
    nonce = b"\7" * 32
    ok, sig, reason = c.call("Authenticate", (user, nonce, "sudo"), "(says)")
    assert (ok, reason) == (True, "")
    pub = (env["state"] / user / "auth.pub").read_bytes()
    Ed25519PublicKey.from_public_bytes(pub).verify(
        bytes(sig), authtoken.build_message(nonce, user, "sudo"))


def test_authenticate_no_match_and_not_enrolled(env):
    c = env["client"]
    import getpass
    user = getpass.getuser()
    assert c.call("Authenticate", (user, b"\1" * 32, "sudo"), "(says)")[::2] == (False, "not-enrolled")
    enroll(c)
    env["fp"].match = False
    assert c.call("Authenticate", (user, b"\1" * 32, "sudo"), "(says)")[::2] == (False, "no-match")


def test_authenticate_rejects_bad_nonce_and_unprivileged_callers(env):
    c = env["client"]
    with pytest.raises(GLib.Error) as e:
        c.call("Authenticate", ("alice", b"short", "sudo"), "(says)")
    assert dbus_error(e) == "org.openhello.Error.InvalidArgument"
    env["svc"].service._auth_uids = {0}   # as in production: root only
    with pytest.raises(GLib.Error) as e:
        c.call("Authenticate", ("alice", b"\1" * 32, "sudo"), "(says)")
    assert dbus_error(e) == "org.openhello.Error.PermissionDenied"


def test_remove_last_modality_deletes_credential(env):
    c = env["client"]
    enroll(c)
    enroll(c, "face")
    import getpass
    d = env["state"] / getpass.getuser()
    c.call("Remove", ("fingerprint",), "(s)")
    assert (d / "sealed.pub").exists()        # face still uses it
    c.call("Remove", ("face",), "(s)")
    assert not (d / "sealed.pub").exists() and not (d / "auth.pub").exists()
    assert env["svc"].polkit_actions == ["org.openhello.settings"]   # one prompt per app


def test_version_property(env):
    reply = env["client"].conn.call_sync(
        BUS_NAME, OBJECT_PATH, "org.freedesktop.DBus.Properties", "Get",
        GLib.Variant("(ss)", (INTERFACE, "Version")), None, Gio.DBusCallFlags.NONE, 5000, None)
    assert reply.unpack()[0].startswith("0.")


def test_adding_a_second_finger_keeps_the_credential(env):
    """GUI "Add Finger": enrolling fingerprint again (a new finger) must keep
    the existing sealed credential and enrollment record intact."""
    import getpass
    c = env["client"]
    enroll(c)
    d = env["state"] / getpass.getuser()
    pub_before = (d / "auth.pub").read_bytes()
    assert enroll(c, options={"finger": GLib.Variant("s", "left-thumb"),
                              "reuse_existing": GLib.Variant("b", False)})[1] is True
    assert (d / "auth.pub").read_bytes() == pub_before
    import json
    record = json.loads((d / "enrollment.json").read_text())
    assert record["modalities"] == ["fingerprint"] and record["sign_in"] == ["fingerprint"]


@pytest.mark.parametrize("user", ["../../etc", "a/b", "", "-rf", "x" * 40, "root admin"])
def test_authenticate_rejects_bad_usernames(env, user):
    """Authenticate is the only method taking a username (PAM's); it must be
    validated before it can reach the filesystem."""
    with pytest.raises(GLib.Error) as e:
        env["client"].call("Authenticate", (user, b"\1" * 32, "sudo"), "(says)")
    assert dbus_error(e) == "org.openhello.Error.InvalidArgument"


def test_duplicate_finger_is_reported_as_such(env):
    """fprintd's enroll-duplicate must reach the GUI as a specific error, not
    a generic failure that looks like a broken sensor."""
    env["fp"].reject = ("duplicate", "this fingerprint is already registered")
    assert enroll(env["client"])[:3] == ("fingerprint", False, "Duplicate")


def test_sign_in_methods_over_dbus(env):
    import getpass
    c = env["client"]
    enroll(c)
    enroll(c, "face")
    assert c.call("GetSignInMethods") == (["fingerprint", "face"], ["fingerprint", "face"])
    c.call("SetSignInMethods", (["face"],), "(as)")
    assert env["svc"].polkit_actions == ["org.openhello.settings"]
    assert c.call("GetSignInMethods")[0] == ["face"]
    assert c.call("GetAuthMethods", (getpass.getuser(),), "(s)") == (["face"],)
    with pytest.raises(GLib.Error) as e:
        c.call("SetSignInMethods", (["iris"],), "(as)")
    assert dbus_error(e) == "org.openhello.Error.NotEnrolled"


def test_get_auth_methods_is_root_only(env):
    env["svc"].service._auth_uids = {0}   # as in production
    with pytest.raises(GLib.Error) as e:
        env["client"].call("GetAuthMethods", ("alice",), "(s)")
    assert dbus_error(e) == "org.openhello.Error.PermissionDenied"


def _authenticate_in_background(env):
    """Start Authenticate on its own connection; return (connection, result)."""
    import getpass
    conn = env["bus"].connect()
    result = {}

    def run():
        try:
            reply = conn.call_sync(BUS_NAME, OBJECT_PATH, INTERFACE, "Authenticate",
                                   GLib.Variant("(says)", (getpass.getuser(), b"\1" * 32, "sudo")),
                                   None, Gio.DBusCallFlags.NONE, 20000, None)
            result["reply"] = reply.unpack()
        except GLib.Error as e:
            result["error"] = e.message
    threading.Thread(target=run, daemon=True).start()
    return conn, result


def _wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def test_scan_stops_when_the_caller_disconnects(env):
    """GDM drops the lock-screen conversation when the user types their
    password; the fingerprint scan must stop instead of running on."""
    enroll(env["client"])
    env["fp"].verify_blocks = True
    conn, _ = _authenticate_in_background(env)
    _wait_for(lambda: getattr(env["fp"], "verify_calls", 0) == 1)
    conn.close_sync(None)
    _wait_for(lambda: getattr(env["fp"], "verify_cancelled", False))


def test_scan_stops_before_suspend(env):
    """A scan still running at suspend can leave fprintd stuck, so logind's
    PrepareForSleep(true) must cancel it."""
    enroll(env["client"])
    env["fp"].verify_blocks = True
    _conn, result = _authenticate_in_background(env)
    _wait_for(lambda: getattr(env["fp"], "verify_calls", 0) == 1)

    logind = env["bus"].connect()
    logind.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                     "RequestName", GLib.Variant("(su)", ("org.freedesktop.login1", 0)),
                     None, Gio.DBusCallFlags.NONE, -1, None)
    logind.emit_signal(None, "/org/freedesktop/login1", "org.freedesktop.login1.Manager",
                       "PrepareForSleep", GLib.Variant("(b)", (True,)))
    _wait_for(lambda: getattr(env["fp"], "verify_cancelled", False))
    _wait_for(lambda: "reply" in result)
    assert result["reply"][0] is False and result["reply"][2] == "cancelled"
