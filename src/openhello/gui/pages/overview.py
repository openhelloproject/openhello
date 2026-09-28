"""Home page: sign-in methods with their state, and how data is protected."""

from __future__ import annotations

from collections.abc import Callable
from gettext import gettext as _
from gettext import ngettext

from gi.repository import Adw, Gtk

from ..client import ModalityStatus

ICONS = {"fingerprint": "auth-fingerprint-symbolic", "face": "auth-face-symbolic"}
TITLES = {"fingerprint": _("Fingerprint"), "face": _("Face")}

# Face sign-in works before IR liveness is calibrated (ARCHITECTURE.md), so
# say plainly what it can't do yet instead of implying Windows Hello parity.
FACE_CAVEAT = _("Photo check is provisional: laser-printed photos aren't tested yet")


class OverviewPage(Adw.NavigationPage):
    def __init__(self, on_setup: Callable[[str], None], on_remove: Callable[[str], None],
                 on_sign_in_changed: Callable[[str, bool], None]):
        super().__init__(title=_("Biometric Sign-In"), tag="overview")
        self._on_setup, self._on_remove = on_setup, on_remove
        self._on_sign_in_changed = on_sign_in_changed

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        self.banner = Adw.Banner(button_label=_("Retry"))
        toolbar.add_top_bar(self.banner)

        page = Adw.PreferencesPage()
        toolbar.set_content(page)
        self.set_child(toolbar)

        hero = Adw.PreferencesGroup()
        hero.add(self._hero())
        page.add(hero)

        self._methods = Adw.PreferencesGroup(title=_("Sign-in methods"))
        page.add(self._methods)
        self._rows: list[Adw.ActionRow] = []

        self._sign_in = Adw.PreferencesGroup(
            title=_("Use for sign-in"),
            description=_("Choose what unlocks the lock screen, login and sudo. "
                          "Methods you turn off stay set up."))
        self._sign_in.set_visible(False)
        page.add(self._sign_in)
        self._switches: list[Adw.SwitchRow] = []

        privacy = Adw.PreferencesGroup(title=_("How your data is protected"))
        for icon, title, subtitle in (
            ("computer-symbolic", _("Stays on this device"),
             _("Fingerprints and face data are never uploaded anywhere.")),
            ("security-high-symbolic", _("Locked in your TPM chip"),
             _("The sign-in key is sealed by your computer's security chip, "
               "not stored as a file.")),
            ("dialog-password-symbolic", _("Your password always works"),
             _("Biometrics are an extra way in, never the only one.")),
        ):
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            row.add_prefix(Gtk.Image(icon_name=icon))
            privacy.add(row)
        page.add(privacy)

    @staticmethod
    def _hero() -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=12, margin_bottom=6)
        icon = Gtk.Image(icon_name="auth-fingerprint-symbolic", pixel_size=64)
        icon.add_css_class("hero-icon")
        title = Gtk.Label(label=_("Sign in with a touch or a glance"))
        title.add_css_class("title-1")
        subtitle = Gtk.Label(label=_("Unlock your computer, sudo and apps with your "
                                     "fingerprint or face."), wrap=True,
                             justify=Gtk.Justification.CENTER)
        subtitle.add_css_class("dim-label")
        for w in (icon, title, subtitle):
            box.append(w)
        return box

    def show_sign_in(self, enrolled: list[str], enabled: list[str], supported: list[str]) -> None:
        """One switch per enrolled method. Methods that can't sign in yet
        (face before liveness is finished) get a disabled switch that says why."""
        for row in self._switches:
            self._sign_in.remove(row)
        self._switches.clear()
        for name in enrolled:
            row = Adw.SwitchRow(title=TITLES.get(name, name.title()))
            row.add_prefix(Gtk.Image(icon_name=ICONS.get(name, "dialog-question-symbolic")))
            if name in supported:
                row.set_active(name in enabled)
                if name == "face":
                    row.set_subtitle(FACE_CAVEAT)
                row.connect("notify::active",
                            lambda r, _p, n=name: self._on_sign_in_changed(n, r.get_active()))
            else:
                row.set_active(False)
                row.set_sensitive(False)
                row.set_subtitle(_("Available once photo and screen rejection is finished"))
            self._sign_in.add(row)
            self._switches.append(row)
        self._sign_in.set_visible(bool(enrolled))

    def show_status(self, statuses: list[ModalityStatus], fingers: list[str]) -> None:
        for row in self._rows:
            self._methods.remove(row)
        self._rows.clear()
        for s in statuses:
            row = self._method_row(s, fingers if s.name == "fingerprint" else [])
            self._methods.add(row)
            self._rows.append(row)

    def _method_row(self, s: ModalityStatus, fingers: list[str]) -> Adw.ActionRow:
        row = Adw.ActionRow(title=TITLES.get(s.name, s.name.title()))
        row.add_prefix(Gtk.Image(icon_name=ICONS.get(s.name, "dialog-question-symbolic")))

        if not s.available:
            row.set_subtitle(_("No compatible sensor found on this device"))
            row.set_sensitive(False)
            return row

        if s.enrolled:
            detail = (ngettext("On · {n} finger", "On · {n} fingers", len(fingers))
                      .format(n=len(fingers)) if fingers else _("On"))
            row.set_subtitle(detail)
            badge = Gtk.Image(icon_name="object-select-symbolic", tooltip_text=_("Set up"))
            badge.add_css_class("success")
            row.add_suffix(badge)
            if s.name == "fingerprint":
                add = Gtk.Button(label=_("Add Finger"), valign=Gtk.Align.CENTER)
                add.add_css_class("pill")
                add.connect("clicked", lambda *_: self._on_setup(s.name))
                row.add_suffix(add)
            remove = Gtk.Button(label=_("Remove"), valign=Gtk.Align.CENTER)
            remove.add_css_class("flat")
            remove.connect("clicked", lambda *_: self._on_remove(s.name))
            row.add_suffix(remove)
        else:
            row.set_subtitle(_("Not set up") + (" · " + FACE_CAVEAT if s.name == "face" else ""))
            setup = Gtk.Button(label=_("Set Up"), valign=Gtk.Align.CENTER)
            setup.add_css_class("suggested-action")
            setup.add_css_class("pill")
            setup.connect("clicked", lambda *_: self._on_setup(s.name))
            row.add_suffix(setup)
        row.set_tooltip_text(s.security_note)
        return row
