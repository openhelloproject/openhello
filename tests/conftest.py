"""Shared fixtures: throwaway swtpm instances and an isolated state dir."""
import os
import socket
import subprocess
import sys
import time

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(__file__))


def pytest_configure(config):
    # The daemon grants root what it denies users (no Unlock/polkit needed,
    # may call Authenticate), so the access-control tests only mean something
    # when run unprivileged.
    if os.geteuid() == 0:
        pytest.exit("run the tests as an unprivileged user, not root "
                    "(e.g. `runuser -u <user> -- python3 -m pytest`)", returncode=2)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SWTPM:
    def __init__(self, tmp_path):
        self.dir = tmp_path
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log = self.dir / "swtpm.log"
        # swtpm needs port and port+1 (server + ctrl); retry on collisions.
        for _ in range(10):
            self.port = _free_port()
            self.proc = subprocess.Popen(
                ["swtpm", "socket", "--tpm2",
                 "--tpmstate", f"dir={self.dir}",
                 "--server", f"type=tcp,port={self.port}",
                 "--ctrl", f"type=tcp,port={self.port + 1}",
                 "--flags", "not-need-init,startup-clear",
                 # level 20 hex-dumps every command/response: see bus_bytes()
                 "--log", f"file={self.log},level=20"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", self.port), 0.1).close()
                    return
                except OSError:
                    if self.proc.poll() is not None:
                        break
                    time.sleep(0.05)
            self.proc.kill()
        raise RuntimeError("could not start swtpm")

    @property
    def tcti(self) -> str:
        return f"swtpm:port={self.port}"

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(5)


@pytest.fixture
def swtpm(tmp_path):
    t = SWTPM(tmp_path / "tpm")
    yield t
    t.stop()


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setenv("OPENHELLO_STATE_DIR", str(d))
    return d



class PrivateBus:
    """A throwaway dbus-daemon (session-bus config) for tests."""

    def __init__(self):
        from gi.repository import Gio
        self.proc = subprocess.Popen(
            ["dbus-daemon", "--session", "--nofork", "--print-address=1"],
            stdout=subprocess.PIPE, text=True)
        self.address = self.proc.stdout.readline().strip()
        self._gio = Gio

    def connect(self):
        Gio = self._gio
        return Gio.DBusConnection.new_for_address_sync(
            self.address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None)

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(5)


@pytest.fixture
def private_bus():
    bus = PrivateBus()
    yield bus
    bus.stop()
