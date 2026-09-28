"""Success animation: a circle draws itself, then a check mark strokes in."""

from __future__ import annotations

import math

from gi.repository import Gtk

from .common import SUCCESS, animate, rgba, set_source


class CheckMark(Gtk.DrawingArea):
    def __init__(self, size: int = 128):
        super().__init__(content_width=size, content_height=size,
                         halign=Gtk.Align.CENTER)
        self._t = 0.0
        self.set_draw_func(self._draw)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["Done"])

    def play(self) -> None:
        self._t = 0.0
        animate(self, 0.0, 1.0, 900, self._set_t)

    def _set_t(self, v: float) -> None:
        self._t = v
        self.queue_draw()

    def _draw(self, _area, cr, width: int, height: int) -> None:
        green = rgba(SUCCESS)
        size = min(width, height)
        cx, cy, r = width / 2, height / 2, size / 2 - 6
        cr.set_line_cap(1)
        cr.set_line_join(1)

        circle = min(1.0, self._t / 0.55)
        if circle > 0:
            set_source(cr, green, 0.15 * circle)
            cr.arc(cx, cy, r, 0, 2 * math.pi)
            cr.fill()
            cr.set_line_width(size * 0.045)
            set_source(cr, green)
            cr.arc(cx, cy, r, -math.pi / 2, -math.pi / 2 + 2 * math.pi * circle)
            cr.stroke()

        tick = max(0.0, (self._t - 0.45) / 0.55)
        if tick > 0:
            pts = [(-0.38, 0.02), (-0.1, 0.3), (0.4, -0.25)]
            pts = [(cx + x * r, cy + y * r) for x, y in pts]
            seg1 = math.dist(pts[0], pts[1])
            seg2 = math.dist(pts[1], pts[2])
            drawn = tick * (seg1 + seg2)
            cr.move_to(*pts[0])
            if drawn <= seg1:
                f = drawn / seg1
                cr.line_to(pts[0][0] + (pts[1][0] - pts[0][0]) * f,
                           pts[0][1] + (pts[1][1] - pts[0][1]) * f)
            else:
                f = (drawn - seg1) / seg2
                cr.line_to(*pts[1])
                cr.line_to(pts[1][0] + (pts[2][0] - pts[1][0]) * f,
                           pts[1][1] + (pts[2][1] - pts[1][1]) * f)
            cr.set_line_width(size * 0.07)
            set_source(cr, green)
            cr.stroke()
