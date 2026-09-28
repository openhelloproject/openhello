#!/usr/bin/env python3
"""
DEVELOPMENT ONLY: a stand-in polkit authority on the *session* bus that
allows every request, so an unprivileged `openhellod --bus session` can be
driven by the GUI. Real polkit only lives on the system bus. Never install.
"""

import logging
import sys
from pathlib import Path

from gi.repository import Gio, GLib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from openhello.core.gcompat import register_object  # noqa: E402

XML = """
<node><interface name="org.freedesktop.PolicyKit1.Authority">
  <method name="CheckAuthorization">
    <arg type="(sa{sv})" direction="in"/><arg type="s" direction="in"/>
    <arg type="a{ss}" direction="in"/><arg type="u" direction="in"/>
    <arg type="s" direction="in"/><arg type="(bba{ss})" direction="out"/>
  </method>
</interface></node>"""


def on_call(_c, _sender, _p, _i, _m, params, invocation):
    logging.warning("DEV polkit: ALLOWING %s (no password asked)", params.unpack()[1])
    invocation.return_value(GLib.Variant("((bba{ss}))", ((True, False, {}),)))


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    iface = Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0]
    register_object(conn, "/org/freedesktop/PolicyKit1/Authority", iface, on_call)
    Gio.bus_own_name_on_connection(conn, "org.freedesktop.PolicyKit1",
                                   Gio.BusNameOwnerFlags.NONE, None, None)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
