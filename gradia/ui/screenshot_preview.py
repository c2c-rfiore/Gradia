# Copyright (C) 2026 Alexander Vanhee
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

"""Floating screenshot previews, stacked in a corner of the screen.

One window per monitor holds a vertical stack of cards. Cards never expire on
their own: each one stays until it is deleted, opened in the editor, or closed.
"""

import os
from typing import Callable, Optional

from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

from gradia.backend.logger import Logger
from gradia.backend.x11_placement import X11Placement

logging = Logger()

# Deterministic geometry: the stack computes its own height so it can be placed
# before the compositor has told us anything about the allocation.
THUMB_MAX_WIDTH = 260
THUMB_MAX_HEIGHT = 170
ACTION_ROW_HEIGHT = 40
CARD_PADDING = 8
CARD_SPACING = 10
SCREEN_MARGIN = 24
IDLE_OPACITY = 0.85


class ScreenshotPreviewCard(Gtk.Box):
    """One screenshot: a rounded thumbnail with delete, edit and close."""

    __gtype_name__ = "GradiaScreenshotPreviewCard"

    def __init__(
        self,
        file_path: str,
        on_edit: Callable[[str], None],
        on_removed: Callable[["ScreenshotPreviewCard"], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=CARD_PADDING)

        self.file_path = file_path
        self._on_edit = on_edit
        self._on_removed = on_removed
        self.thumb_width = THUMB_MAX_WIDTH
        self.thumb_height = THUMB_MAX_HEIGHT

        self.add_css_class("screenshot-preview-card")
        self.set_margin_top(CARD_PADDING)
        self.set_margin_bottom(CARD_PADDING)
        self.set_margin_start(CARD_PADDING)
        self.set_margin_end(CARD_PADDING)

        self._build_thumbnail()
        self._build_actions()

    @property
    def total_height(self) -> int:
        return self.thumb_height + ACTION_ROW_HEIGHT + CARD_SPACING + 2 * CARD_PADDING

    def _build_thumbnail(self) -> None:
        picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER)

        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(self.file_path)
            width, height = pixbuf.get_width(), pixbuf.get_height()
            scale = min(THUMB_MAX_WIDTH / width, THUMB_MAX_HEIGHT / height, 1.0)
            self.thumb_width = max(1, int(width * scale))
            self.thumb_height = max(1, int(height * scale))
            picture.set_pixbuf(
                pixbuf.scale_simple(
                    self.thumb_width, self.thumb_height, GdkPixbuf.InterpType.BILINEAR
                )
            )
        except Exception as e:
            logging.warning(f"Could not load preview thumbnail for {self.file_path}.", exception=e)
            self.thumb_width, self.thumb_height = THUMB_MAX_WIDTH, THUMB_MAX_HEIGHT

        picture.set_size_request(self.thumb_width, self.thumb_height)

        frame = Gtk.Box(halign=Gtk.Align.CENTER)
        frame.add_css_class("screenshot-preview-thumb")
        frame.set_overflow(Gtk.Overflow.HIDDEN)
        frame.append(picture)
        self.append(frame)

    def _build_actions(self) -> None:
        row = Gtk.Box(spacing=6, height_request=ACTION_ROW_HEIGHT)

        delete_button = self._action_button(
            "user-trash-symbolic", _("Delete Screenshot"), self._on_delete_clicked
        )
        delete_button.add_css_class("destructive-action")

        edit_button = self._action_button(
            "document-edit-symbolic", _("Open in Editor"), self._on_edit_clicked
        )
        edit_button.set_hexpand(True)

        close_button = self._action_button(
            "window-close-symbolic", _("Dismiss Preview"), self._on_close_clicked
        )

        row.append(delete_button)
        row.append(edit_button)
        row.append(close_button)
        self.append(row)

    def _action_button(self, icon_name: str, tooltip: str, handler) -> Gtk.Button:
        button = Gtk.Button(icon_name=icon_name, tooltip_text=tooltip, valign=Gtk.Align.CENTER)
        button.add_css_class("circular")
        button.connect("clicked", handler)
        return button

    def _on_delete_clicked(self, _button: Gtk.Button) -> None:
        """Delete for good, as configured; the file does not go to Trash."""
        try:
            os.unlink(self.file_path)
            logging.info(f"Deleted screenshot {self.file_path}")
        except FileNotFoundError:
            pass
        except OSError as e:
            logging.warning(f"Could not delete {self.file_path}.", exception=e)
        self._on_removed(self)

    def _on_edit_clicked(self, _button: Gtk.Button) -> None:
        self._on_edit(self.file_path)
        self._on_removed(self)

    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        self._on_removed(self)


class ScreenshotPreviewStack(Gtk.Window):
    """The floating stack of previews anchored to one monitor's bottom-left corner."""

    __gtype_name__ = "GradiaScreenshotPreviewStack"

    def __init__(
        self,
        monitor: Gdk.Monitor,
        placement: X11Placement,
        on_edit: Callable[[str], None],
        on_empty: Callable[["ScreenshotPreviewStack"], None],
    ) -> None:
        super().__init__()

        self.monitor = monitor
        self.placement = placement
        self._on_edit = on_edit
        self._on_empty = on_empty
        self.cards: list[ScreenshotPreviewCard] = []

        self.set_decorated(False)
        self.set_resizable(False)
        self.set_opacity(IDLE_OPACITY)
        self.add_css_class("screenshot-preview-window")

        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=CARD_SPACING)
        self.set_child(self.box)

        self._setup_hover()
        self.connect("map", lambda *_: self._place())

    def _setup_hover(self) -> None:
        """Solid while pointed at, so the buttons are easy to read."""
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self.set_opacity(1.0))
        motion.connect("leave", lambda *_: self.set_opacity(IDLE_OPACITY))
        self.add_controller(motion)

    """
    Cards
    """

    def add_screenshot(self, file_path: str) -> ScreenshotPreviewCard:
        card = ScreenshotPreviewCard(file_path, self._on_edit, self.remove_card)
        self.cards.append(card)
        self.box.append(card)
        self._resize_and_place()
        return card

    def remove_card(self, card: ScreenshotPreviewCard) -> None:
        if card not in self.cards:
            return

        self.cards.remove(card)
        self.box.remove(card)

        if not self.cards:
            self._on_empty(self)
            self.close()
            return

        self._resize_and_place()

    """
    Placement
    """

    @property
    def stack_width(self) -> int:
        widest = max((card.thumb_width for card in self.cards), default=THUMB_MAX_WIDTH)
        return widest + 2 * CARD_PADDING

    @property
    def stack_height(self) -> int:
        if not self.cards:
            return 0
        total = sum(card.total_height for card in self.cards)
        return total + CARD_SPACING * (len(self.cards) - 1)

    def _resize_and_place(self) -> None:
        if not self.cards:
            return
        self.set_default_size(self.stack_width, self.stack_height)
        # The move has to follow the resize, or the bottom edge drifts.
        GLib.idle_add(self._place)

    def _place(self) -> bool:
        """Anchor the bottom-left corner inside this monitor's work area."""
        surface = self.get_surface()
        if surface is None or not self.placement.available:
            return False

        xid = self._surface_xid(surface)
        if xid is None:
            return False

        geometry = self.monitor.get_geometry()
        height = self.get_height() or self.stack_height
        x = geometry.x + SCREEN_MARGIN
        y = geometry.y + geometry.height - height - SCREEN_MARGIN

        self.placement.move(xid, x, y)
        self.placement.keep_above(xid)
        return False

    @staticmethod
    def _surface_xid(surface) -> Optional[int]:
        getter = getattr(surface, "get_xid", None)
        if getter is None:
            return None
        try:
            return getter()
        except Exception as e:
            logging.warning("Could not read the X window id of a preview.", exception=e)
            return None
