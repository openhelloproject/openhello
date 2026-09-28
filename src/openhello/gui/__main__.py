"""openhello-setup entry point: python3 -m openhello.gui"""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")


def main() -> int:
    from .application import OpenHelloApplication
    return OpenHelloApplication().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
