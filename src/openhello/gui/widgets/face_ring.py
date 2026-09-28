"""
Face scan ring: a circle of ticks around a face glyph. While searching, a soft
highlight sweeps around the ring; each captured sample lights up a share of
the ticks, and on success the whole ring glows.
"""

from __future__ import annotations

import math

from gi.repository import Adw, Gtk

from .common import accent_color, animate, set_source

TICKS = 72


class FaceRing(Gtk.Overlay):
    def __init__(self, size: int = 240):
        super().__init__()
        self._fill = 0.0
        self._sweep = -1.0      # < 0: no sweep highlight
        self._glow = 0.0
        self._sweep_anim: Adw.TimedAnimation | None = None
        self._fill_anim: Adw.TimedAnimation | None = None

        self._canvas = Gtk.DrawingArea(content_width=size, content_height=size)
        self._canvas.set_draw_func(self._draw)
        self.set_child(self._canvas)

        self._face = Gtk.Image(icon_name="avatar-default-symbolic", pixel_size=int(size * 0.42),
                               halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self._face.add_css_class("face-glyph")
        self.add_overlay(self._face)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["Face scan progress"])

    # -- public API -----------------------------------------------------------
    def set_searching(self, searching: bool) -> None:
        if searching and self._sweep_anim is None:
            self._sweep_anim = animate(self._canvas, 0.0, 1.0, 1800, self._set_sweep,
                                       easing=Adw.Easing.LINEAR, repeat=0)
        elif not searching and self._sweep_anim is not None:
            self._sweep_anim.pause()
            self._sweep_anim = None
            self._sweep = -1.0
            self._canvas.queue_draw()

    def set_progress(self, fraction: float) -> None:
        if self._fill_anim is not None:
            self._fill_anim.pause()
        self._fill_anim = animate(self._canvas, self._fill, max(0.0, min(1.0, fraction)),
                                  400, self._set_fill)

    def celebrate(self) -> None:
        self.set_searching(False)
        self.set_progress(1.0)
        animate(self._canvas, 0.0, 1.0, 700, self._set_glow)
        self._face.add_css_class("accent")

    def reset(self) -> None:
        self.set_searching(False)
        self._fill = self._glow = 0.0
        self._face.remove_css_class("accent")
        self._canvas.queue_draw()

    # -- animation targets ------------------------------------------------------
    def _set_sweep(self, v: float) -> None:
        self._sweep = v
        self._canvas.queue_draw()

    def _set_fill(self, v: float) -> None:
        self._fill = v
        self._canvas.queue_draw()

    def _set_glow(self, v: float) -> None:
        self._glow = v
        self._canvas.queue_draw()

    # -- drawing --------------------------------------------------------------------
    def _draw(self, _area, cr, width: int, height: int) -> None:
        cx, cy = width / 2, height / 2
        outer = min(width, height) / 2 - 6
        dim, accent = self._canvas.get_color(), accent_color()
        lit_ticks = self._fill * TICKS
        cr.set_line_cap(1)  # ROUND

        for k in range(TICKS):
            frac = k / TICKS
            angle = -math.pi / 2 + frac * 2 * math.pi
            lit = k < lit_ticks
            length = 20 if lit else 13
            inner = outer - length
            cos, sin = math.cos(angle), math.sin(angle)
            cr.move_to(cx + inner * cos, cy + inner * sin)
            cr.line_to(cx + outer * cos, cy + outer * sin)
            if lit:
                cr.set_line_width(4.0)
                set_source(cr, accent, 1.0)
            else:
                # sweep highlight: brightest at the sweep head, fading behind it
                behind = (self._sweep - frac) % 1.0 if self._sweep >= 0 else 1.0
                boost = max(0.0, 1.0 - behind * 5)
                cr.set_line_width(3.0)
                set_source(cr, dim, 0.16 + 0.5 * boost)
            cr.stroke()

        if self._glow > 0:
            cr.arc(cx, cy, outer + 2, 0, 2 * math.pi)
            cr.set_line_width(10 * (1 - self._glow) + 1)
            set_source(cr, accent, 0.35 * (1 - self._glow))
            cr.stroke()
