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
their own: each one stays until it is deleted or dismissed, and opening it in
the editor leaves it in place.
"""

import os
from math import ceil
from typing import Callable, Optional

from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

from gradia.backend.logger import Logger
from gradia.backend.x11_placement import X11Placement

logging = Logger()

# Every preview is the same size whatever the screenshot's shape: the image is
# scaled to cover the box and centre-cropped to it.
THUMB_WIDTH = 260
THUMB_HEIGHT = 170
ACTION_ROW_HEIGHT = 40
CARD_PADDING = 8
CARD_SPACING = 10
SCREEN_MARGIN = 24
IDLE_OPACITY = 0.85
# Long enough to read as the stack having some weight to it.
TRANSITION_MS = 280


class ScreenshotPreviewCard(Gtk.Box):
    """One screenshot: a rounded thumbnail with delete, edit and dismiss."""

    __gtype_name__ = "GradiaScreenshotPreviewCard"

    # thumbnail + action row, the box spacing between them, and the card margins
    TOTAL_HEIGHT = THUMB_HEIGHT + ACTION_ROW_HEIGHT + CARD_PADDING + 2 * CARD_PADDING
    TOTAL_WIDTH = THUMB_WIDTH + 2 * CARD_PADDING

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

        self.add_css_class("screenshot-preview-card")
        self.set_margin_top(CARD_PADDING)
        self.set_margin_bottom(CARD_PADDING)
        self.set_margin_start(CARD_PADDING)
        self.set_margin_end(CARD_PADDING)

        self._build_thumbnail()
        self._build_actions()
        self._setup_hover()

    @property
    def total_height(self) -> int:
        return self.TOTAL_HEIGHT

    def _setup_hover(self) -> None:
        """Only the card under the pointer brightens, not the whole stack."""
        self.set_opacity(IDLE_OPACITY)
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self.set_opacity(1.0))
        motion.connect("leave", lambda *_: self.set_opacity(IDLE_OPACITY))
        self.add_controller(motion)

    def _build_thumbnail(self) -> None:
        picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER)
        picture.set_size_request(THUMB_WIDTH, THUMB_HEIGHT)

        thumbnail = self._cover_crop(self.file_path)
        if thumbnail is not None:
            picture.set_pixbuf(thumbnail)

        frame = Gtk.Box(halign=Gtk.Align.CENTER)
        frame.add_css_class("screenshot-preview-thumb")
        frame.set_overflow(Gtk.Overflow.HIDDEN)
        frame.set_size_request(THUMB_WIDTH, THUMB_HEIGHT)
        frame.append(picture)
        self.append(frame)

    @staticmethod
    def _cover_crop(file_path: str) -> Optional[GdkPixbuf.Pixbuf]:
        """Fill exactly THUMB_WIDTH x THUMB_HEIGHT, cropping rather than distorting.

        The size is read from the header and the file decoded straight to the
        size we need, so a 4K screenshot never gets decoded at full resolution.
        """
        try:
            _format, width, height = GdkPixbuf.Pixbuf.get_file_info(file_path)
        except Exception as e:
            logging.warning(f"Could not read image header of {file_path}.", exception=e)
            return None

        if not width or not height or width <= 0 or height <= 0:
            logging.warning(f"Unusable image dimensions for {file_path}.")
            return None

        # Scale to cover the box, then take the middle of it.
        scale = max(THUMB_WIDTH / width, THUMB_HEIGHT / height)
        scaled_width = max(THUMB_WIDTH, ceil(width * scale))
        scaled_height = max(THUMB_HEIGHT, ceil(height * scale))

        try:
            scaled = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                file_path, scaled_width, scaled_height, False
            )
        except Exception as e:
            logging.warning(f"Could not load preview thumbnail for {file_path}.", exception=e)
            return None

        if scaled is None:
            return None

        return GdkPixbuf.Pixbuf.new_subpixbuf(
            scaled,
            (scaled.get_width() - THUMB_WIDTH) // 2,
            (scaled.get_height() - THUMB_HEIGHT) // 2,
            THUMB_WIDTH,
            THUMB_HEIGHT,
        )

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
        # The preview outlives the editor: only its own close button dismisses it.
        self._on_edit(self.file_path)

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
        self._revealers: dict[ScreenshotPreviewCard, Gtk.Revealer] = {}
        self._settling = 0
        self._tick_id: Optional[int] = None
        self._last_y: Optional[int] = None
        self._kept_above = False
        self._reported_empty = False

        self.set_decorated(False)
        self.set_resizable(False)
        self.add_css_class("screenshot-preview-window")

        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=CARD_SPACING)
        self.set_child(self.box)

        self.connect("map", lambda *_: self._start_settling())

    """
    Cards
    """

    def add_screenshot(self, file_path: str) -> ScreenshotPreviewCard:
        card = ScreenshotPreviewCard(file_path, self._on_edit, self.remove_card)
        revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
            transition_duration=TRANSITION_MS,
            reveal_child=False,
            child=card,
        )

        self.cards.append(card)
        self._revealers[card] = revealer
        self.box.append(revealer)

        self._start_settling()
        GLib.idle_add(self._reveal, revealer)
        return card

    @staticmethod
    def _reveal(revealer: Gtk.Revealer) -> bool:
        revealer.set_reveal_child(True)
        return False

    def remove_card(self, card: ScreenshotPreviewCard) -> None:
        """Collapse the card away; the ones above it settle down into the gap."""
        revealer = self._revealers.get(card)
        if card not in self.cards or revealer is None:
            return

        self.cards.remove(card)
        revealer.set_reveal_child(False)
        revealer.connect("notify::child-revealed", self._on_collapsed, card)
        self._start_settling()

    def _on_collapsed(self, revealer: Gtk.Revealer, _param, card: ScreenshotPreviewCard) -> None:
        if revealer.get_child_revealed():
            return

        self.box.remove(revealer)
        self._revealers.pop(card, None)

        # Several cards can be closed at once, so wait for the last one to
        # finish collapsing and report only once.
        if not self.cards and not self._revealers and not self._reported_empty:
            self._reported_empty = True
            self._stop_settling()
            self._on_empty(self)
            self.close()

    """
    Placement

    The window is sized by its content, and X11 keeps a window's top-left corner
    put when it resizes. So every time the stack grows or shrinks the bottom edge
    would drift, and reading the height once after the change is too early: the
    new allocation is not in yet. Instead the bottom edge is re-pinned every
    frame for as long as something is moving, which is also what makes the
    remaining cards fall into the gap rather than hang in the air.
    """

    @property
    def stack_width(self) -> int:
        return ScreenshotPreviewCard.TOTAL_WIDTH

    @property
    def stack_height(self) -> int:
        if not self.cards:
            return 0
        total = sum(card.total_height for card in self.cards)
        return total + CARD_SPACING * (len(self.cards) - 1)

    def _start_settling(self) -> None:
        self._settling += 1
        if self._tick_id is None:
            self._tick_id = self.add_tick_callback(self._on_tick)
        # Keep pinning a little past the transition so the last frames land too.
        GLib.timeout_add(TRANSITION_MS + 120, self._finish_settling)

    def _finish_settling(self) -> bool:
        self._settling = max(0, self._settling - 1)
        if self._settling == 0:
            self._stop_settling()
        return False

    def _stop_settling(self) -> None:
        if self._tick_id is not None:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = None
        self._pin_bottom(exact=True)

    def _on_tick(self, _widget, _frame_clock) -> bool:
        self._pin_bottom()
        return GLib.SOURCE_CONTINUE

    def _pin_bottom(self, exact: bool = False) -> None:
        """Put the bottom edge on the margin.

        While the stack is moving the measured height is what makes the cards
        appear to fall; once it has settled the computed height is authoritative,
        because the allocation can lag the last frame.
        """
        surface = self.get_surface()
        if surface is None or not self.placement.available:
            return

        xid = self._surface_xid(surface)
        if xid is None:
            return

        geometry = self.monitor.get_geometry()
        if exact:
            height = self.stack_height or self.get_height()
        else:
            height = self.get_height() or self.stack_height
        x = geometry.x + SCREEN_MARGIN
        y = geometry.y + geometry.height - height - SCREEN_MARGIN

        if y != self._last_y:
            self.placement.move(xid, x, y)
            self._last_y = y

        # Once is enough; this runs on every frame while the stack settles.
        if not self._kept_above:
            self.placement.keep_above(xid)
            self._kept_above = True

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
