"""Fingerprint flow: choose a finger (or reuse existing prints), then scan."""

from __future__ import annotations

from collections.abc import Callable
from gettext import gettext as _

from gi.repository import Adw, Gtk

from ..strings import FINGER_LABELS, FINGERPRINT_STATUS, HANDS, RETRY_STATUSES, finger_short
from ..widgets.fingerprint_art import FingerprintArt


class FingerChooserPage(Adw.NavigationPage):
    """on_choose(finger, reuse_existing)."""

    def __init__(self, enrolled: list[str], on_choose: Callable[[str, bool], None],
                 offer_reuse: bool = True):
        """offer_reuse: show "use my existing fingerprints" (only when OpenHello
        isn't using them yet; afterwards this page is for adding fingers)."""
        super().__init__(title=_("Fingerprint"), tag="finger-chooser")
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        toolbar.set_content(page)
        self.set_child(toolbar)

        if enrolled and offer_reuse:
            names = ", ".join(FINGER_LABELS.get(f, f) for f in enrolled)
            existing = Adw.PreferencesGroup(
                title=_("Already on this computer"),
                description=_("These fingers are already registered with your system "
                              "({names}). OpenHello can use them right away.").format(names=names))
            use = Gtk.Button(label=_("Use My Existing Fingerprints"),
                             halign=Gtk.Align.CENTER, margin_top=6)
            use.add_css_class("suggested-action")
            use.add_css_class("pill")
            use.connect("clicked", lambda *_: on_choose(enrolled[0], True))
            existing.add(use)
            page.add(existing)

        chooser = Adw.PreferencesGroup(
            title=_("Add a finger") if enrolled else _("Choose a finger"),
            description=_("Pick the finger you'll naturally use on the sensor."))
        for hand, fingers in HANDS:
            row = Adw.ActionRow(title=hand)
            box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
            for finger in fingers:
                btn = Gtk.Button(tooltip_text=FINGER_LABELS[finger])
                btn.add_css_class("finger-button")
                content = Gtk.Box(spacing=4, halign=Gtk.Align.CENTER)
                if finger in enrolled:
                    # Already registered: shown for orientation, not re-enrollable.
                    btn.add_css_class("enrolled")
                    btn.set_sensitive(False)
                    content.append(Gtk.Image(icon_name="object-select-symbolic"))
                    btn.set_tooltip_text(_("{finger} (registered)").format(
                        finger=FINGER_LABELS[finger]))
                content.append(Gtk.Label(label=finger_short(finger)))
                btn.set_child(content)
                btn.connect("clicked", lambda _b, f=finger: on_choose(f, False))
                box.append(btn)
            row.add_suffix(box)
            chooser.add(row)
        page.add(chooser)


class FingerprintScanPage(Adw.NavigationPage):
    def __init__(self, finger: str, on_cancel: Callable[[], None]):
        super().__init__(title=_("Scanning"), tag="finger-scan", can_pop=False)
        self._on_cancel = on_cancel
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_back_button=False)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_: self._on_cancel())
        header.pack_start(cancel)
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      valign=Gtk.Align.CENTER, margin_top=24, margin_bottom=24,
                      margin_start=24, margin_end=24)
        self.art = FingerprintArt(240)
        self.art.set_halign(Gtk.Align.CENTER)
        self._title = Gtk.Label(label=_("Place your {finger} on the sensor").format(
            finger=FINGER_LABELS.get(finger, _("finger")).lower()), wrap=True,
            justify=Gtk.Justification.CENTER)
        self._title.add_css_class("title-2")
        self._hint = Gtk.Label(label=_("Lift and place it again each time the ring moves"),
                               wrap=True, justify=Gtk.Justification.CENTER)
        self._hint.add_css_class("dim-label")
        self._count = Gtk.Label()
        self._count.add_css_class("numeric")
        self._count.add_css_class("caption")
        for w in (self.art, self._title, self._hint, self._count):
            box.append(w)
        toolbar.set_content(Adw.Clamp(child=box, maximum_size=420))
        self.set_child(toolbar)

    def update(self, status: str, done: int, total: int) -> None:
        if total:
            self.art.set_progress(done / total)
            self._count.set_label(_("{done} of {total}").format(done=done, total=total))
        if status in RETRY_STATUSES:
            self.art.shake()
        elif status == "enroll-stage-passed":
            self.art.ripple()
        text = FINGERPRINT_STATUS.get(status)
        if text:
            self._hint.set_label(text)
            if hasattr(self._hint, "announce"):   # GTK >= 4.14: screen readers
                self._hint.announce(text, Gtk.AccessibleAnnouncementPriority.MEDIUM)
