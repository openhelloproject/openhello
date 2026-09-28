"""Shared drawing/animation helpers for the custom widgets."""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gdk, Gtk

FALLBACK_ACCENT = "#3584e4"   # libadwaita's default blue
SUCCESS = "#2ec27e"
ERROR = "#e01b24"


def rgba(spec: str) -> Gdk.RGBA:
    c = Gdk.RGBA()
    c.parse(spec)
    return c


def accent_color() -> Gdk.RGBA:
    """System accent colour (libadwaita >= 1.6), else the default blue."""
    manager = Adw.StyleManager.get_default()
    getter = getattr(manager, "get_accent_color_rgba", None)
    return getter() if getter is not None else rgba(FALLBACK_ACCENT)


def set_source(cr, color: Gdk.RGBA, alpha: float = 1.0) -> None:
    cr.set_source_rgba(color.red, color.green, color.blue, color.alpha * alpha)


def animate(widget: Gtk.Widget, start: float, end: float, duration_ms: int,
            on_value: Callable[[float], None], *,
            easing: Adw.Easing = Adw.Easing.EASE_OUT_CUBIC,
            repeat: int = 1) -> Adw.TimedAnimation:
    """Run a TimedAnimation that calls on_value(v) each frame. repeat=0 loops
    forever. Respects the system's reduced-motion setting (libadwaita skips
    to the end when animations are disabled)."""
    target = Adw.CallbackAnimationTarget.new(on_value)
    anim = Adw.TimedAnimation.new(widget, start, end, duration_ms, target)
    anim.set_easing(easing)
    anim.set_repeat_count(repeat)
    anim.play()
    return anim
