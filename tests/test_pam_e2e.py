"""
End-to-end: real pam_openhello.so (via pam_start_confdir: no root, no
/etc/pam.d changes) -> D-Bus (a private dbus-daemon, reached through
DBUS_SYSTEM_BUS_ADDRESS) -> real org.openhello.Daemon1 service -> real TPM
sealing on swtpm -> Ed25519 challenge-response verified in C.

The only stand-in is the biometric matcher (StubModality); live fingerprint
matching is covered on hardware, see docs/TESTING.md. Build the module first:
    cmake -S pam -B pam/build && cmake --build pam/build
"""
import getpass
import os
import subprocess
import threading
import time
from importlib import resources
from pathlib import Path

import pytest

pytest.importorskip("tpm2_pytss")

from gi.repository import Gio, GLib  # noqa: E402

from openhello.core.gcompat import register_object  # noqa: E402
from openhello.daemon.service import BUS_NAME, INTERFACE, OBJECT_PATH  # noqa: E402
from test_dbus_service import env  # noqa: E402,F401  (fixture)

ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "pam" / "build" / "pam_openhello.so"
HARNESS = ROOT / "pam" / "build" / "pam_openhello_harness"
if not (MODULE.exists() and HARNESS.exists()):
    pytest.skip("pam_openhello not built (see module docstring)", allow_module_level=True)

PAM_SUCCESS, PAM_AUTH_ERR, PAM_AUTHINFO_UNAVAIL, PAM_USER_UNKNOWN = 0, 7, 9, 10
USER = getpass.getuser()   # the daemon resolves callers from the bus


class RogueDaemon:
    """Owns org.openhello.Daemon1 on the private bus and answers Authenticate
    however `reply(user, nonce, service)` says: a tuple (ok, sig, reason),
    or None to never answer."""

    def __init__(self, bus, reply):
        xml = resources.files("openhello.daemon").joinpath("org.openhello.Daemon1.xml").read_text()
        iface = Gio.DBusNodeInfo.new_for_xml(xml).lookup_interface(INTERFACE)
        ready = threading.Event()

        def on_call(_c, _s, _p, _i, method, params, invocation):
            if method == "GetAuthMethods":
                invocation.return_value(GLib.Variant("(as)", (["fingerprint"],)))
                return
            if method != "Authenticate":
                invocation.return_dbus_error("org.openhello.Error.Failed", "rogue")
                return
            user, nonce, service = params.unpack()
            answer = reply(user, bytes(nonce), service)
            if answer is not None:
                invocation.return_value(GLib.Variant("(bays)", answer))
            else:
                self.held.append(invocation)   # never answer

        def run():
            ctx = GLib.MainContext.new()
            ctx.push_thread_default()
            self.loop = GLib.MainLoop.new(ctx, False)
            self.conn = bus.connect()
            register_object(self.conn, OBJECT_PATH, iface, on_call)
            self.conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                "org.freedesktop.DBus", "RequestName",
                                GLib.Variant("(su)", (BUS_NAME, 0)), None,
                                Gio.DBusCallFlags.NONE, -1, None)
            ready.set()
            self.loop.run()

        self.held = []
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        assert ready.wait(10)

    def stop(self):
        self.loop.quit()
        self.thread.join(5)
        self.conn.close_sync(None)


def daemon_keystore(env):  # noqa: F811
    """The (stopped) daemon's own keystore — swtpm has no resource manager,
    so tests reuse its TPM connection instead of opening a second one."""
    return env["svc"].service._orch._keystore


def enroll(env):  # noqa: F811
    from test_dbus_service import enroll as dbus_enroll
    assert dbus_enroll(env["client"])[1] is True


def pam(env, user=USER, service="openhello-test", stack=None, args="", messages=None):  # noqa: F811
    """
    Without `stack`, the module is configured `required` *in this private
    test config only* so its own return code is observable (a lone failing
    `sufficient` module is ignored by libpam and the stack ends in
    PAM_PERM_DENIED). With `stack`, it's `sufficient` exactly as deployed.
    """
    confdir = env["state"].parent / "pam.d"
    confdir.mkdir(exist_ok=True)
    control = "sufficient" if stack else "required"
    quiet = "" if messages is not None else "quiet"
    line = f"auth {control} {MODULE} state_dir={env['state']} {quiet} {args}"
    (confdir / service).write_text("\n".join([line] + (stack or [])) + "\n")
    p = subprocess.run([str(HARNESS), str(confdir), service, user],
                       capture_output=True, text=True, timeout=30,
                       env={**os.environ, "DBUS_SYSTEM_BUS_ADDRESS": env["bus"].address})
    if messages is not None:   # PAM conversation messages the harness printed
        messages.extend(line for line in p.stderr.splitlines() if line.startswith("[pam msg"))
    return p.returncode


def test_enroll_writes_only_public_material(env):  # noqa: F811
    enroll(env)
    d = env["state"] / USER
    assert sorted(f.name for f in d.iterdir()) == [
        "auth.pub", "enrollment.json", "sealed.json", "sealed.priv", "sealed.pub"]
    assert len((d / "auth.pub").read_bytes()) == 32


def test_match_succeeds(env):  # noqa: F811
    enroll(env)
    assert pam(env) == PAM_SUCCESS
    assert env["fp"].verify_calls == 1


def test_no_match_fails(env):  # noqa: F811
    enroll(env)
    env["fp"].match = False
    assert pam(env) == PAM_AUTH_ERR


def test_not_enrolled_never_wakes_the_daemon(env):  # noqa: F811
    assert pam(env) == PAM_AUTHINFO_UNAVAIL
    assert getattr(env["fp"], "verify_calls", 0) == 0


def test_invalid_username_rejected(env):  # noqa: F811
    assert pam(env, user="../etc") == PAM_USER_UNKNOWN


def test_daemon_down(env):  # noqa: F811
    enroll(env)
    env["svc"].stop()
    assert pam(env) == PAM_AUTHINFO_UNAVAIL


def test_unseal_failure_is_not_success(env):  # noqa: F811
    enroll(env)
    (env["state"] / "gate.auth").write_bytes(b"\0" * 32)
    assert pam(env) == PAM_AUTH_ERR


# -- a lying daemon must not get anyone in -----------------------------------

@pytest.fixture
def rogue(env):  # noqa: F811
    """Enroll for real, then replace the daemon with a rogue one."""
    enroll(env)
    env["svc"].stop()
    started = []

    def start(reply):
        r = RogueDaemon(env["bus"], reply)
        started.append(r)
        return r
    yield start
    for r in started:
        r.stop()


def test_rogue_ok_without_signature(env, rogue):  # noqa: F811
    rogue(lambda u, n, s: (True, b"", ""))
    assert pam(env) == PAM_AUTH_ERR


def test_rogue_ok_with_garbage_signature(env, rogue):  # noqa: F811
    rogue(lambda u, n, s: (True, b"\0" * 64, ""))
    assert pam(env) == PAM_AUTH_ERR


def test_replayed_signature_rejected(env, rogue):  # noqa: F811
    """Capture a genuinely valid signature, then replay it for a new nonce."""
    from openhello.core import authtoken
    from openhello.tpm.keystore import SealedCredential
    secret = daemon_keystore(env).unseal(SealedCredential.load(USER))
    captured = {}

    def reply(user, nonce, service):
        captured.setdefault("sig", authtoken.sign_challenge(secret, user, nonce, service))
        return (True, captured["sig"], "")
    rogue(reply)
    assert pam(env) == PAM_SUCCESS    # control: first, genuine
    assert pam(env) == PAM_AUTH_ERR   # the same signature, replayed


def test_signature_bound_to_service(env, rogue):  # noqa: F811
    from openhello.core import authtoken
    from openhello.tpm.keystore import SealedCredential
    secret = daemon_keystore(env).unseal(SealedCredential.load(USER))
    sign_as = {"service": "openhello-test"}
    rogue(lambda u, n, s: (True, authtoken.sign_challenge(secret, u, n, sign_as["service"]), ""))
    assert pam(env) == PAM_SUCCESS    # control: correct service verifies
    sign_as["service"] = "some-other-service"
    assert pam(env) == PAM_AUTH_ERR


def test_daemon_hang_times_out(env, rogue):  # noqa: F811
    rogue(lambda u, n, s: None)
    assert pam(env, args="timeout=1") == PAM_AUTHINFO_UNAVAIL


def test_writable_pubkey_refused(env):  # noqa: F811
    enroll(env)
    os.chmod(env["state"] / USER / "auth.pub", 0o666)
    assert pam(env) == PAM_AUTHINFO_UNAVAIL


# -- `sufficient` semantics: failure always falls through to the password ----

def test_sufficient_falls_through_on_failure(env):  # noqa: F811
    enroll(env)
    env["fp"].match = False
    # pam_permit stands in for "user typed the right password"
    assert pam(env, stack=["auth required pam_permit.so"]) == PAM_SUCCESS
    env["svc"].stop()
    assert pam(env, stack=["auth required pam_permit.so"]) == PAM_SUCCESS


def test_sufficient_success_short_circuits(env):  # noqa: F811
    enroll(env)
    # pam_deny after us: only reachable if we *didn't* succeed
    assert pam(env, stack=["auth required pam_deny.so"]) == PAM_SUCCESS
    env["fp"].match = False
    assert pam(env, stack=["auth required pam_deny.so"]) == PAM_AUTH_ERR


# -- prompts and "nothing usable" -------------------------------------------

def test_prompt_matches_the_methods_that_will_run(env):  # noqa: F811
    enroll(env)
    msgs = []
    assert pam(env, messages=msgs) == PAM_SUCCESS
    assert any("Scan your fingerprint (" in m for m in msgs), msgs

    from test_dbus_service import enroll as dbus_enroll
    assert dbus_enroll(env["client"], "face")[1] is True
    msgs.clear()
    pam(env, messages=msgs)
    assert any("Scan your fingerprint or look at the camera" in m for m in msgs), msgs


def test_no_usable_method_skips_straight_to_password(env):  # noqa: F811
    """With every sign-in method switched off, PAM must not prompt or scan:
    it steps aside at once so the password prompt appears immediately."""
    enroll(env)
    env["client"].call("SetSignInMethods", ([],), "(as)")
    msgs = []
    started = time.monotonic()
    assert pam(env, messages=msgs) == PAM_AUTHINFO_UNAVAIL
    assert time.monotonic() - started < 3
    assert msgs == []
    assert getattr(env["fp"], "verify_calls", 0) == 0
    # ...and `sufficient` then falls through to the password
    assert pam(env, stack=["auth required pam_permit.so"]) == PAM_SUCCESS
