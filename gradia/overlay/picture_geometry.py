# Copyright (C) 2025 Alexander Vanhee
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

from gi.repository import Gtk


def follow_picture_size(owner: Gtk.Widget, picture: Gtk.Picture) -> None:
    """
    Redraw `owner` whenever the size of what `picture` shows changes.

    Every overlay on the canvas positions itself by asking the picture where
    its paintable is drawn, so each of them has to redraw when that moves.
    Watching `notify::paintable` is not enough to catch it: the preview
    composite is a single long-lived paintable that is re-rendered in place, so
    the picture's `paintable` property is set once, on the first composite, and
    never again. Changing the padding, the aspect ratio or the preset resizes
    the canvas inside that same paintable, which reaches a listener only as the
    paintable's own `invalidate-size`.

    Missing it left the transparency checkerboard drawing at whatever size the
    composite had when it first appeared, so a band of grey squares showed
    around the image.
    """
    tracked: dict = {"paintable": None, "handler": 0}

    def on_size_changed(*_args) -> None:
        owner.queue_draw()

    def rebind(*_args) -> None:
        previous = tracked["paintable"]
        if previous is not None and tracked["handler"]:
            previous.disconnect(tracked["handler"])

        paintable = picture.get_paintable()
        tracked["paintable"] = paintable
        tracked["handler"] = (
            paintable.connect("invalidate-size", on_size_changed)
            if paintable is not None else 0
        )
        on_size_changed()

    picture.connect("notify::paintable", rebind)
    rebind()
