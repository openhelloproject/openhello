"""Adw.Application: single instance, loads the stylesheet, opens the window."""

from __future__ import annotations

from importlib import resources

from gi.repository import Adw, Gdk, Gio, Gtk

from .client import DaemonClient
from .window import MainWindow

APP_ID = "org.openhello.Setup"


class OpenHelloApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self._client: DaemonClient | None = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_string(resources.files(__package__).joinpath("style.css").read_text())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def do_activate(self):
        window = self.get_active_window()
        if window is None:
            self._client = DaemonClient()
            window = MainWindow(self, self._client)
        window.present()
