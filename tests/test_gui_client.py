"""The GUI's DaemonClient against the real D-Bus service (private bus, swtpm,
mock polkit — fixtures from test_dbus_service.py)."""
import time

import pytest

pytest.importorskip("tpm2_pytss")

from gi.repository import GLib  # noqa: E402

from openhello.gui.client import DaemonClient  # noqa: E402
from test_dbus_service import env  # noqa: E402,F401  (fixture)


def run_until(ctx, predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        ctx.iteration(False)
        time.sleep(0.005)


@pytest.fixture
def gui_client(env):  # noqa: F811
    ctx = GLib.MainContext.new()
    ctx.push_thread_default()
    client = DaemonClient(env["bus"].connect())
    yield client, ctx
    client.close()
    ctx.pop_thread_default()


def call(ctx, method, *args, **kwargs):
    out = {}
    method(*args, lambda result, err: out.update(result=result, err=err), **kwargs)
    run_until(ctx, lambda: out)
    return out["result"], out["err"]


def test_locked_until_unlock(gui_client):
    client, ctx = gui_client
    _, err = call(ctx, client.get_status)
    assert err.code == "PermissionDenied"
    _, err = call(ctx, client.unlock)
    assert err is None
    _, err = call(ctx, client.get_status)
    assert err is None


def test_status_and_enroll_flow(gui_client):
    client, ctx = gui_client
    assert call(ctx, client.unlock)[1] is None
    statuses, err = call(ctx, client.get_status)
    assert err is None and {s.name for s in statuses} == {"fingerprint", "face"}

    events = []
    client.connect("enroll-progress", lambda _c, *a: events.append(("progress",) + a))
    client.connect("enroll-finished", lambda _c, *a: events.append(("finished",) + a))
    _, err = call(ctx, client.enroll_start, "fingerprint", finger="right-thumb",
                  reuse_existing=False)
    assert err is None
    run_until(ctx, lambda: any(e[0] == "finished" for e in events))
    assert [e[3:5] for e in events if e[0] == "progress"] == [(1, 3), (2, 3), (3, 3)]
    assert events[-1] == ("finished", "fingerprint", True, "", "")

    fingers, err = call(ctx, client.list_fingers)
    assert (fingers, err) == (["right-index-finger"], None)


def test_errors_are_mapped(gui_client, env):  # noqa: F811
    client, ctx = gui_client
    env["svc"].polkit_allow = False
    _, err = call(ctx, client.unlock)
    assert err.code == "PermissionDenied"
    _, err = call(ctx, client.enroll_start, "fingerprint")
    assert err.code == "PermissionDenied"      # still locked
    env["svc"].polkit_allow = True
    assert call(ctx, client.unlock)[1] is None
    _, err = call(ctx, client.enroll_start, "iris")
    assert err.code == "UnknownModality"


def test_service_unavailable(private_bus):
    ctx = GLib.MainContext.new()
    ctx.push_thread_default()
    client = DaemonClient(private_bus.connect())   # no daemon on this bus
    _, err = call(ctx, client.get_status)
    ctx.pop_thread_default()
    assert err.code == "ServiceUnavailable"
