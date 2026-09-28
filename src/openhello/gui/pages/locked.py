"""Shown on launch until the user authenticates (daemon Unlock(), polkit)."""

from __future__ import annotations

from collections.abc import Callable
from gettext import gettext as _

from gi.repository import Adw, Gtk


class LockedPage(Adw.NavigationPage):
    def __init__(self, on_unlock: Callable[[], None]):
        super().__init__(title=_("Biometric Sign-In"), tag="locked", can_pop=False)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        self._status = Adw.StatusPage(
            icon_name="system-lock-screen-symbolic",
            title=_("Biometric Sign-In is locked"),
            description=_("Authenticate to change how you sign in."))
        self._button = Gtk.Button(label=_("Unlock"), halign=Gtk.Align.CENTER)
        self._button.add_css_class("suggested-action")
        self._button.add_css_class("pill")
        self._button.connect("clicked", lambda *_: on_unlock())
        self._spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(self._spinner)
        box.append(self._button)
        self._status.set_child(box)
        toolbar.set_content(self._status)
        self.set_child(toolbar)
        self.set_waiting(True)

    def set_waiting(self, waiting: bool, message: str = "") -> None:
        """waiting: the system authentication dialog is up."""
        self._spinner.set_visible(waiting)
        self._button.set_visible(not waiting)
        if message:
            self._status.set_description(message)
