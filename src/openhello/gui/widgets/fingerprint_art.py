"""
Animated fingerprint: stylised ridges that light up from the core outwards as
enrollment stages pass, a ripple on each accepted touch, and a shake when the
sensor asks to retry.
"""

from __future__ import annotations

import math

from gi.repository import Adw, Gtk

from .common import accent_color, animate, set_source

RIDGES = 11
BASE_SIZE = 220.0
Y_STRETCH = 1.28   # fingertips are taller than wide


class FingerprintArt(Gtk.DrawingArea):
    def __init__(self, size: int = 220):
        super().__init__(content_width=size, content_height=size)
        self._fill = 0.0       # 0..1, animated
        self._ripple = 0.0     # 0..1 while a ripple plays
        self._shake = 0.0      # 0..1 while a shake plays
        self._fill_anim: Adw.TimedAnimation | None = None
        self.set_draw_func(self._draw)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["Fingerprint scan progress"])

    # -- public API -----------------------------------------------------------
    def set_progress(self, fraction: float) -> None:
        fraction = max(0.0, min(1.0, fraction))
        if self._fill_anim is not None:
            self._fill_anim.pause()
        self._fill_anim = animate(self, self._fill, fraction, 450, self._set_fill)

    def ripple(self) -> None:
        animate(self, 0.0, 1.0, 650, self._set_ripple)

    def shake(self) -> None:
        animate(self, 0.0, 1.0, 480, self._set_shake, easing=Adw.Easing.LINEAR)

    def reset(self) -> None:
        self._fill = self._ripple = self._shake = 0.0
        self.queue_draw()

    # -- animation targets ------------------------------------------------------
    def _set_fill(self, v: float) -> None:
        self._fill = v
        self.queue_draw()

    def _set_ripple(self, v: float) -> None:
        self._ripple = 0.0 if v >= 1.0 else v
        self.queue_draw()

    def _set_shake(self, v: float) -> None:
        self._shake = 0.0 if v >= 1.0 else v
        self.queue_draw()

    # -- drawing --------------------------------------------------------------------
    def _draw(self, _area, cr, width: int, height: int) -> None:
        scale = min(width, height) / BASE_SIZE
        shake_px = math.sin(self._shake * math.pi * 6) * (1 - self._shake) * 10 * scale
        cx, cy = width / 2 + shake_px, height / 2 + 4 * scale
        dim, accent = self.get_color(), accent_color()

        cr.set_line_cap(1)  # ROUND
        cr.set_line_width(3.4 * scale)
        lit_ridges = self._fill * RIDGES
        for i in range(RIDGES):
            start, end, r = self._ridge_geometry(i, scale)
            self._ridge(cr, cx, cy, r, start, end)
            set_source(cr, dim, 0.16)
            cr.stroke()

            lit = max(0.0, min(1.0, lit_ridges - i))
            if lit > 0:
                self._ridge(cr, cx, cy, r, start, start + (end - start) * lit)
                set_source(cr, accent, 1.0)
                cr.stroke()

        if self._ripple > 0:
            radius = (70 + 60 * self._ripple) * scale
            cr.save()
            cr.translate(cx, cy)
            cr.scale(1, Y_STRETCH)
            cr.arc(0, 0, radius, 0, 2 * math.pi)
            cr.restore()
            cr.set_line_width(4 * scale * (1 - self._ripple) + 0.5)
            set_source(cr, accent, 0.45 * (1 - self._ripple))
            cr.stroke()

    @staticmethod
    def _ridge_geometry(i: int, scale: float) -> tuple[float, float, float]:
        """Start/end angle and radius of ridge i. Ridges are nearly closed
        loops whose small gap wanders left and right of the bottom — that
        irregularity is what makes a real print read as organic. The two
        innermost ridges form a tighter core loop opening upwards."""
        r = (9 + i * 8.4) * scale
        if i < 2:
            centre, half_gap = -math.pi / 2, 0.9 - 0.3 * i     # core: opens at the top
        else:
            centre = math.pi / 2 + 0.55 * math.sin(i * 1.9)
            half_gap = 0.16 + 0.05 * ((i * 7) % 3)
        return centre + half_gap, centre - half_gap + 2 * math.pi, r

    @staticmethod
    def _ridge(cr, cx: float, cy: float, r: float, a0: float, a1: float) -> None:
        cr.save()
        cr.translate(cx, cy)
        cr.scale(1, Y_STRETCH)
        cr.new_sub_path()
        cr.arc(0, 0, r, a0, a1)
        cr.restore()
