"""Success and failure end pages for an enrollment."""

from __future__ import annotations

from collections.abc import Callable
from gettext import gettext as _

from gi.repository import Adw, Gtk

from ..widgets.check_mark import CheckMark


class SuccessPage(Adw.NavigationPage):
    def __init__(self, title: str, description: str, on_done: Callable[[], None]):
        super().__init__(title=_("All Set"), tag="success", can_pop=False)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar(show_back_button=False))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      valign=Gtk.Align.CENTER, margin_top=24, margin_bottom=24,
                      margin_start=24, margin_end=24)
        self.check = CheckMark(128)
        heading = Gtk.Label(label=title, wrap=True, justify=Gtk.Justification.CENTER)
        heading.add_css_class("title-1")
        body = Gtk.Label(label=description, wrap=True, justify=Gtk.Justification.CENTER)
        body.add_css_class("dim-label")
        done = Gtk.Button(label=_("Done"), halign=Gtk.Align.CENTER, margin_top=12)
        done.add_css_class("suggested-action")
        done.add_css_class("pill")
        done.connect("clicked", lambda *_: on_done())
        for w in (self.check, heading, body, done):
            box.append(w)
        toolbar.set_content(Adw.Clamp(child=box, maximum_size=420))
        self.set_child(toolbar)
        self.connect("shown", lambda *_: self.check.play())


class FailurePage(Adw.NavigationPage):
    def __init__(self, message: str, on_retry: Callable[[], None], on_close: Callable[[], None]):
        super().__init__(title=_("Not Finished"), tag="failure", can_pop=False)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar(show_back_button=False))
        status = Adw.StatusPage(icon_name="dialog-warning-symbolic",
                                title=_("Setup didn't finish"), description=message)
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        retry = Gtk.Button(label=_("Try Again"))
        retry.add_css_class("suggested-action")
        retry.add_css_class("pill")
        retry.connect("clicked", lambda *_: on_retry())
        close = Gtk.Button(label=_("Back"))
        close.add_css_class("pill")
        close.connect("clicked", lambda *_: on_close())
        buttons.append(close)
        buttons.append(retry)
        status.set_child(buttons)
        toolbar.set_content(status)
        self.set_child(toolbar)
