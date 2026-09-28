"""
GLib/PyGObject compatibility shims, so one codebase runs on both current
distros (GLib >= 2.84, PyGObject >= 3.52) and LTS ones like Ubuntu 24.04
(GLib 2.80), without deprecation warnings on the new ones.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable

from gi.repository import Gio, GLib


def register_object(conn: Gio.DBusConnection, path: str, iface: Gio.DBusInterfaceInfo,
                    on_method_call: Callable, on_get_property: Callable | None = None) -> int:
    """Gio.DBusConnection.register_object, preferring the closure-based API."""
    if hasattr(conn, "register_object_with_closures2"):
        return conn.register_object_with_closures2(path, iface, on_method_call,
                                                    on_get_property, None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return conn.register_object(path, iface, on_method_call, on_get_property, None)


def unix_signal_add(signum: int, handler: Callable[[], bool]) -> int:
    """Run handler on the main loop when signum arrives."""
    try:
        from gi.repository import GLibUnix
        return GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signum, handler)
    except (ImportError, AttributeError):
        return GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, handler)
