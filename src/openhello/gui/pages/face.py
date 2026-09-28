"""Face flow: short intro with tips, then the scan ring."""

from __future__ import annotations

from collections.abc import Callable
from gettext import gettext as _

from gi.repository import Adw, Gtk

from ..strings import FACE_STATUS
from ..widgets.face_ring import FaceRing


class FaceIntroPage(Adw.NavigationPage):
    def __init__(self, on_start: Callable[[], None]):
        super().__init__(title=_("Face"), tag="face-intro")
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      valign=Gtk.Align.CENTER, margin_top=24, margin_bottom=24,
                      margin_start=24, margin_end=24)
        hero = FaceRing(170)
        hero.set_halign(Gtk.Align.CENTER)
        title = Gtk.Label(label=_("Set up face sign-in"))
        title.add_css_class("title-1")
        body = Gtk.Label(label=_("Your laptop's infrared camera sees your face even in "
                                 "the dark, without needing any room light."),
                         wrap=True, justify=Gtk.Justification.CENTER)
        body.add_css_class("dim-label")
        tips = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                       halign=Gtk.Align.CENTER, margin_top=6)
        for tip in (_("Sit about an arm's length from the screen"),
                    _("Take off sunglasses; regular glasses are fine"),
                    _("Move your head slightly when asked")):
            row = Gtk.Box(spacing=8)
            row.append(Gtk.Image(icon_name="object-select-symbolic"))
            row.append(Gtk.Label(label=tip, xalign=0))
            tips.append(row)
        start = Gtk.Button(label=_("Start"), halign=Gtk.Align.CENTER, margin_top=12)
        start.add_css_class("suggested-action")
        start.add_css_class("pill")
        start.connect("clicked", lambda *_: on_start())
        for w in (hero, title, body, tips, start):
            box.append(w)
        toolbar.set_content(Adw.Clamp(child=box, maximum_size=420))
        self.set_child(toolbar)


class FaceScanPage(Adw.NavigationPage):
    def __init__(self, on_cancel: Callable[[], None]):
        super().__init__(title=_("Scanning"), tag="face-scan", can_pop=False)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_back_button=False)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_: on_cancel())
        header.pack_start(cancel)
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      valign=Gtk.Align.CENTER, margin_top=24, margin_bottom=24)
        self.ring = FaceRing(260)
        self.ring.set_halign(Gtk.Align.CENTER)
        self._title = Gtk.Label(label=FACE_STATUS["face-searching"])
        self._title.add_css_class("title-2")
        self._hint = Gtk.Label(label=_("Keep your face inside the circle"), wrap=True,
                               justify=Gtk.Justification.CENTER)
        self._hint.add_css_class("dim-label")
        for w in (self.ring, self._title, self._hint):
            box.append(w)
        toolbar.set_content(Adw.Clamp(child=box, maximum_size=420))
        self.set_child(toolbar)
        self.ring.set_searching(True)

    def update(self, status: str, done: int, total: int) -> None:
        if status == "face-sample" and total:
            self.ring.set_searching(False)
            self.ring.set_progress(done / total)
            self._title.set_label(FACE_STATUS["face-sample"])
            self._hint.set_label(_("{done} of {total}").format(done=done, total=total))
        elif status in FACE_STATUS:
            self._title.set_label(FACE_STATUS[status])
