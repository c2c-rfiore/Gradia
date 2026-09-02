# Copyright (C) 2025 Alexander Vanhee, tfuxu
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

import cairo
from gi.repository import Adw, Gdk, Gio, Graphene, Gtk, GObject
from typing import Tuple
from enum import Enum

from gradia.overlay.drawing_actions import *
from gradia.overlay.text_entry_popover import TextEntryPopover

HANDLE_SIZE = 8
# The grab area is deliberately much larger than the drawn handle: an 8px target
# is nearly impossible to hit, and the handle is only a visual marker.
HANDLE_GRAB_SIZE = 22
EDGE_GRAB_MARGIN = 10

class ResizeHandle(Enum):
    NONE = "none"
    TOP_LEFT = "top_left"
    TOP_RIGHT = "top_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"
    TOP = "top"
    BOTTOM = "bottom"
    LEFT = "left"
    RIGHT = "right"

    @classmethod
    def get_cursor_for_handle(cls, handle) -> str:
        cursor_map = {
            cls.TOP_LEFT: "nw-resize",
            cls.TOP_RIGHT: "ne-resize",
            cls.BOTTOM_LEFT: "sw-resize",
            cls.BOTTOM_RIGHT: "se-resize",
            cls.TOP: "ns-resize",
            cls.BOTTOM: "ns-resize",
            cls.LEFT: "ew-resize",
            cls.RIGHT: "ew-resize",
            cls.NONE: "grab"
        }
        return cursor_map.get(handle, "default")

def _identity_coords(x, y) -> Tuple[float, float]:
    """Projection used while rasterising an action into its cached node."""
    return float(x), float(y)


# A cached node is rasterised across the image plus a margin, so a stroke whose
# line width or arrow head reaches past the image edge is not clipped out of its
# own node before the overlay's own clip gets a say.
NODE_CACHE_MARGIN = 64


class DrawingOverlay(Gtk.Widget):
    __gtype_name__ = "GradiaDrawingOverlay"

    __gsignals__ = {
        'selection-changed': (GObject.SignalFlags.RUN_FIRST, None, (object,))
    }

    def __init__(self, **kwargs):
        super().__init__(can_focus=True, **kwargs)

        # Finished annotations are rasterised once into their own render node
        # and composited on the GPU thereafter; see do_snapshot.
        self._node_cache: dict[int, tuple] = {}
        self._node_cache_size = None

        self.coordinate_transform = None
        self.delta_transform = None

        self.picture_widget = None
        self.options = None
        self.font_size = 22
        self.is_drawing = False
        self.current_stroke = []
        self.start_point = None
        self.end_point = None
        self.actions: list[DrawingAction] = []
        self.redo_stack = []
        self._next_number = 1

        self._selected_action: DrawingAction | None = None
        self.selection_start_pos = None
        self.is_moving_selection = False
        self.move_start_point = None
        self.current_shift_pressed = False

        self.is_resizing = False
        self.resize_handle = ResizeHandle.NONE
        self.resize_start_bounds = None
        self.resize_start_mouse = None

        self._action_clipboard: DrawingAction | None = None
        self._paste_count = 0

        self.text_entry_popup = None
        self.text_position = None
        self.is_text_editing = False
        self.live_text = None
        self.editing_text_action = None

        self._setup_gestures()

    def set_picture_reference(self, picture: Gtk.Picture) -> None:
        self.picture_widget = picture
        picture.connect("notify::paintable", lambda *args: self.queue_draw())

    def set_erase_selected_revealer(self, erase_selected_revealer: Gtk.Revealer) -> None:
        self.erase_selected_revealer = erase_selected_revealer

    @property
    def selected_action(self) -> DrawingAction | None:
        return self._selected_action

    @selected_action.setter
    def selected_action(self, action: DrawingAction | None) -> None:
        self._selected_action = action
        self.erase_selected_revealer.set_reveal_child(action is not None)
        self.emit('selection-changed', action.options if action else None)

    def _can_resize_action(self, action: DrawingAction) -> bool:
        return isinstance(action, (RectAction, CircleAction, CensorAction, ArrowAction, LineAction))

    def _get_resize_handles(self, action: DrawingAction) -> list:
        if not self._can_resize_action(action):
            return []

        if isinstance(action, (ArrowAction, LineAction)):
            start_x_widget, start_y_widget = self._image_to_widget_coords(*action.start)
            end_x_widget, end_y_widget = self._image_to_widget_coords(*action.end)

            handles = [
                (ResizeHandle.TOP_LEFT, start_x_widget - HANDLE_SIZE/2, start_y_widget - HANDLE_SIZE/2),
                (ResizeHandle.BOTTOM_RIGHT, end_x_widget - HANDLE_SIZE/2, end_y_widget - HANDLE_SIZE/2),
            ]
            return handles
        else:
            min_x_img, min_y_img, max_x_img, max_y_img = action.get_bounds().get_bounding_rect()
            x1_widget, y1_widget = self._image_to_widget_coords(min_x_img, min_y_img)
            x2_widget, y2_widget = self._image_to_widget_coords(max_x_img, max_y_img)

            handles = [
                (ResizeHandle.TOP_LEFT, x1_widget - HANDLE_SIZE/2, y1_widget - HANDLE_SIZE/2),
                (ResizeHandle.TOP_RIGHT, x2_widget - HANDLE_SIZE/2, y1_widget - HANDLE_SIZE/2),
                (ResizeHandle.BOTTOM_LEFT, x1_widget - HANDLE_SIZE/2, y2_widget - HANDLE_SIZE/2),
                (ResizeHandle.BOTTOM_RIGHT, x2_widget - HANDLE_SIZE/2, y2_widget - HANDLE_SIZE/2),
            ]
            return handles

    def _get_handle_at_point(self, x_widget: float, y_widget: float) -> ResizeHandle:
        if not self.selected_action or not self._can_resize_action(self.selected_action):
            return ResizeHandle.NONE

        # Grab from the handle's centre outwards, so the target is HANDLE_GRAB_SIZE
        # across rather than the HANDLE_SIZE square that gets painted.
        reach = HANDLE_GRAB_SIZE / 2
        handles = self._get_resize_handles(self.selected_action)
        nearest, nearest_distance = ResizeHandle.NONE, None
        for handle_type, handle_x, handle_y in handles:
            center_x = handle_x + HANDLE_SIZE / 2
            center_y = handle_y + HANDLE_SIZE / 2
            dx, dy = abs(x_widget - center_x), abs(y_widget - center_y)
            if dx <= reach and dy <= reach:
                distance = dx * dx + dy * dy
                if nearest_distance is None or distance < nearest_distance:
                    nearest, nearest_distance = handle_type, distance

        # Overlapping grab areas on a short shape: take the closest handle.
        if nearest != ResizeHandle.NONE:
            return nearest

        if isinstance(self.selected_action, (ArrowAction, LineAction)):
            return ResizeHandle.NONE

        min_x_img, min_y_img, max_x_img, max_y_img = self.selected_action.get_bounds().get_bounding_rect()
        x1_widget, y1_widget = self._image_to_widget_coords(min_x_img, min_y_img)
        x2_widget, y2_widget = self._image_to_widget_coords(max_x_img, max_y_img)

        margin = EDGE_GRAB_MARGIN

        if abs(y_widget - y1_widget) <= margin and x1_widget <= x_widget <= x2_widget:
            return ResizeHandle.TOP
        if abs(y_widget - y2_widget) <= margin and x1_widget <= x_widget <= x2_widget:
            return ResizeHandle.BOTTOM
        if abs(x_widget - x1_widget) <= margin and y1_widget <= y_widget <= y2_widget:
            return ResizeHandle.LEFT
        if abs(x_widget - x2_widget) <= margin and y1_widget <= y_widget <= y2_widget:
            return ResizeHandle.RIGHT

        return ResizeHandle.NONE

    def _resize_action(
        self,
        action: DrawingAction,
        handle: ResizeHandle,
        start_bounds: tuple,
        start_mouse: tuple,
        current_mouse: tuple,
        shift_pressed: bool
    ):
        if isinstance(action, (ArrowAction, LineAction)):
            current_mouse_x, current_mouse_y = current_mouse

            if handle == ResizeHandle.TOP_LEFT:
                action.start = (int(current_mouse_x), int(current_mouse_y))
            elif handle == ResizeHandle.BOTTOM_RIGHT:
                action.end = (int(current_mouse_x), int(current_mouse_y))
        else:
            min_x_start, min_y_start, max_x_start, max_y_start = start_bounds
            start_mouse_x, start_mouse_y = start_mouse
            current_mouse_x, current_mouse_y = current_mouse

            delta_x = current_mouse_x - start_mouse_x
            delta_y = current_mouse_y - start_mouse_y

            if handle in [ResizeHandle.TOP_LEFT, ResizeHandle.TOP, ResizeHandle.TOP_RIGHT]:
                min_y_start += delta_y
            if handle in [ResizeHandle.BOTTOM_LEFT, ResizeHandle.BOTTOM, ResizeHandle.BOTTOM_RIGHT]:
                max_y_start += delta_y
            if handle in [ResizeHandle.TOP_LEFT, ResizeHandle.LEFT, ResizeHandle.BOTTOM_LEFT]:
                min_x_start += delta_x
            if handle in [ResizeHandle.TOP_RIGHT, ResizeHandle.RIGHT, ResizeHandle.BOTTOM_RIGHT]:
                max_x_start += delta_x

            action.start = (int(min_x_start), int(min_y_start))
            action.end = (int(max_x_start), int(max_y_start))

    def _get_image_bounds(self) -> Tuple[float, float, float, float]:
        if not self.picture_widget or not self.picture_widget.get_paintable():
            return 0, 0, float(self.get_width()), float(self.get_height())

        widget_w = float(self.picture_widget.get_width())
        widget_h = float(self.picture_widget.get_height())
        img_w_intrinsic = float(self.picture_widget.get_paintable().get_intrinsic_width())
        img_h_intrinsic = float(self.picture_widget.get_paintable().get_intrinsic_height())

        if img_w_intrinsic <= 0 or img_h_intrinsic <= 0:
            return 0, 0, widget_w, widget_h

        scale = min(widget_w / img_w_intrinsic, widget_h / img_h_intrinsic)

        disp_w = img_w_intrinsic * scale
        disp_h = img_h_intrinsic * scale

        offset_x = (widget_w - disp_w) / 2
        offset_y = (widget_h - disp_h) / 2
        return offset_x, offset_y, disp_w, disp_h

    def _get_modified_image_bounds(self) -> Tuple[int, int]:
        if not self.picture_widget or not self.picture_widget.get_paintable():
            return 0, 0
        return self.picture_widget.get_paintable().get_intrinsic_width(), \
               self.picture_widget.get_paintable().get_intrinsic_height()

    def _get_scale_factor(self) -> float:
        _, _, dw, dh = self._get_image_bounds()
        if not self.picture_widget or not self.picture_widget.get_paintable():
            return 1.0
        img_w_intrinsic = self.picture_widget.get_paintable().get_intrinsic_width()
        return dw / img_w_intrinsic if img_w_intrinsic else 1.0

    def _widget_to_image_coords(self, x_widget: float, y_widget: float) -> Tuple[int, int]:
        ox, oy, disp_w, disp_h = self._get_image_bounds()
        scale = self._get_scale_factor()

        rel_x_on_disp_image = x_widget - ox
        rel_y_on_disp_image = y_widget - oy

        img_x_intrinsic_top_left = rel_x_on_disp_image / scale
        img_y_intrinsic_top_left = rel_y_on_disp_image / scale

        img_w_intrinsic, img_h_intrinsic = self._get_modified_image_bounds()

        center_x_intrinsic = img_w_intrinsic / 2
        center_y_intrinsic = img_h_intrinsic / 2

        img_x_centered = round(img_x_intrinsic_top_left - center_x_intrinsic)
        img_y_centered = round(img_y_intrinsic_top_left - center_y_intrinsic)

        return img_x_centered, img_y_centered

    def _image_to_widget_coords(self, x_image: int, y_image: int) -> Tuple[float, float]:
        ox, oy, disp_w, disp_h = self._get_image_bounds()
        scale = self._get_scale_factor()

        img_w_intrinsic, img_h_intrinsic = self._get_modified_image_bounds()

        center_x_intrinsic = img_w_intrinsic / 2
        center_y_intrinsic = img_h_intrinsic / 2

        img_x_intrinsic_top_left = center_x_intrinsic + x_image
        img_y_intrinsic_top_left = center_y_intrinsic + y_image

        rel_x_on_disp_image = img_x_intrinsic_top_left * scale
        rel_y_on_disp_image = img_y_intrinsic_top_left * scale

        widget_x = ox + rel_x_on_disp_image
        widget_y = oy + rel_y_on_disp_image

        return widget_x, widget_y

    def _is_point_in_image(self, x_widget: float, y_widget: float) -> bool:
        ox, oy, dw, dh = self._get_image_bounds()
        return ox <= x_widget <= ox + dw and oy <= y_widget <= oy + dh

    def _get_background_pixbuf(self):
        if not self.picture_widget:
            return None

        paintable = self.picture_widget.get_paintable()
        if isinstance(paintable, Gdk.Texture):
            return Gdk.pixbuf_get_from_texture(paintable)
        return None

    def _setup_actions(self):
        for mode in DrawingMode:
            action = Gio.SimpleAction.new(f"drawing-mode-{mode.value}", None)
            action.connect("activate", lambda a, p, m=mode: self.set_drawing_mode(m))
            root = self.get_root()
            if hasattr(root, "add_action"):
                root.add_action(action)

    def _get_number_actions(self) -> list:
        return [action for action in self.actions if isinstance(action, NumberStampAction)]

    def _renumber_actions(self):
        number_actions = self._get_number_actions()
        number_actions.sort(key=lambda action: action.creation_time)

        for i, action in enumerate(number_actions, 1):
            action.number = i
        self._next_number = len(number_actions) + 1

    def remove_selected_action(self) -> bool:
        if self.selected_action and self.selected_action in self.actions:
            was_number_action = isinstance(self.selected_action, NumberStampAction)
            self.actions.remove(self.selected_action)
            self.selected_action = None
            self.redo_stack.clear()

            if was_number_action:
                self._renumber_actions()

            self._update_undo_redo_action_states()
            self.queue_draw()
            return True
        return False

    # A duplicate lands offset from its original, so it reads as a second element
    # rather than looking like nothing happened.
    PASTE_OFFSET_IMG = 20

    def copy_selected_action(self) -> bool:
        """Put the selected element on the element clipboard. False if nothing is selected."""
        if not self.selected_action:
            return False
        self._action_clipboard = self.selected_action.copy()
        self._paste_count = 0
        return True

    def paste_copied_action(self) -> bool:
        """Drop a duplicate of the element clipboard on the canvas and select it."""
        if self._action_clipboard is None:
            return False

        pasted = self._action_clipboard.copy()
        self._paste_count += 1
        offset = self._paste_offset(pasted, self.PASTE_OFFSET_IMG * self._paste_count)
        pasted.translate(offset, offset)
        self.actions.append(pasted)

        if isinstance(pasted, NumberStampAction):
            self._renumber_actions()

        self.redo_stack.clear()
        self._update_undo_redo_action_states()
        # A tool only touches its own elements, so hand over to the one that owns
        # what was just pasted, or it could not be moved.
        self._activate_tool_for(pasted)
        self.selected_action = pasted
        self.queue_draw()
        return True

    def forget_copied_action(self) -> None:
        self._action_clipboard = None
        self._paste_count = 0

    def _paste_offset(self, action: DrawingAction, offset: int) -> int:
        """Shift the duplicate clear of its original, but never off the image."""
        image_width, image_height = self._get_modified_image_bounds()
        if image_width <= 0 or image_height <= 0:
            return offset
        _, _, max_x, max_y = action.get_bounds().get_bounding_rect()
        room = min(image_width / 2 - max_x, image_height / 2 - max_y)
        return int(max(0, min(offset, room)))

    def _activate_tool_for(self, action: DrawingAction) -> None:
        mode = action.get_drawing_mode()
        # The select tool already reaches every element, so leave it alone.
        if mode == self.options.mode or self.options.mode == DrawingMode.SELECT:
            return
        root = self.get_root()
        if root is not None and hasattr(root, "image_bin"):
            # Via the window, so the toolbar's tool button follows along.
            root.image_bin.set_drawing_mode(mode)
        else:
            self.set_drawing_mode(mode)

    def set_drawing_mode(self, mode: DrawingMode) -> None:
        if self.text_entry_popup:
            self._close_text_entry()

        if mode != DrawingMode.SELECT:
            self.selected_action = None

        self.options.mode = mode
        self.is_drawing = False
        self.is_moving_selection = False
        self.is_resizing = False
        self.resize_handle = ResizeHandle.NONE
        self.current_stroke.clear()
        self.start_point = None
        self.end_point = None
        self.queue_draw()

    # Freehand tools stay purely additive: drawing over existing ink is normal.
    # Every other tool defers to an element already under the pointer.
    FREEHAND_MODES = (DrawingMode.PEN, DrawingMode.HIGHLIGHTER)

    def _mode_edits_existing(self) -> bool:
        mode = self.options.mode
        return mode != DrawingMode.SELECT and mode not in self.FREEHAND_MODES

    def _tool_owns(self, action: DrawingAction) -> bool:
        """Whether the active tool is the one that draws this kind of element."""
        return action.get_drawing_mode() == self.options.mode

    def _existing_action_at(self, x_widget: float, y_widget: float, x_image: int, y_image: int):
        """What a press here would act on: a resize handle, a move, or a selection.

        Each tool sees only its own elements, so the rectangle tool draws straight
        over an arrow instead of grabbing it. The select tool has no such blinkers.
        """
        selected = self.selected_action
        if selected is not None and self._tool_owns(selected):
            if self._can_resize_action(selected):
                handle = self._get_handle_at_point(x_widget, y_widget)
                if handle != ResizeHandle.NONE:
                    return ("handle", handle)

            if self._is_point_in_selection_bounds(x_image, y_image):
                return ("move", selected)

        action = self._find_action_at_point(x_image, y_image, own_tool_only=True)
        if action is not None:
            return ("select", action)

        return None

    def _begin_edit_interaction(self, hit, x_image: int, y_image: int) -> None:
        kind, value = hit
        if kind == "handle":
            self.is_resizing = True
            self.resize_handle = value
            self.resize_start_bounds = self.selected_action.get_bounds().get_bounding_rect()
            self.resize_start_mouse = (x_image, y_image)
            return

        if kind == "select":
            self.selected_action = value
            self.queue_draw()

        self.is_moving_selection = True
        self.move_start_point = (x_image, y_image)

    def _find_action_at_point(self, x_image: int, y_image: int, own_tool_only: bool = False) -> DrawingAction | None:
        for action in reversed(self.actions):
            if own_tool_only and not self._tool_owns(action):
                continue
            if action.contains_point(x_image, y_image):
                return action
        return None


    def _is_point_in_selection_bounds(self, x_image: int, y_image: int) -> bool:
        if not self.selected_action:
            return False

        min_x, min_y, max_x, max_y = self.selected_action.get_bounds().get_bounding_rect()
        padding_img = max(self.options.size, self.font_size / 2)

        return min_x - padding_img <= x_image <= max_x + padding_img and \
               min_y - padding_img <= y_image <= max_y + padding_img

    def _draw_selection_box(self, cr: cairo.Context, scale: float):
        if not self.selected_action:
            return

        bounds = self.selected_action.get_bounds()
        points = bounds.get_points()
        widget_points = [self._image_to_widget_coords(int(p[0]), int(p[1])) for p in points]
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        cr.set_source_rgba(*accent)
        cr.set_line_width(2)
        cr.move_to(*widget_points[0])
        for point in widget_points[1:]:
            cr.line_to(*point)
        cr.close_path()
        cr.stroke()
        if self._can_resize_action(self.selected_action):
            handles = self._get_resize_handles(self.selected_action)
            for handle_type, handle_x, handle_y in handles:
                cr.set_source_rgba(1.0, 1.0, 1.0, 1.0)
                cr.rectangle(handle_x, handle_y, HANDLE_SIZE, HANDLE_SIZE)
                cr.fill()
                cr.set_source_rgba(*accent)
                cr.rectangle(handle_x, handle_y, HANDLE_SIZE, HANDLE_SIZE)
                cr.stroke()

    def _setup_gestures(self):
        click = Gtk.GestureClick.new()
        click.set_button(1)
        click.connect("pressed", self._on_click)
        self.add_controller(click)

        drag = Gtk.GestureDrag.new()
        drag.set_button(1)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

        motion = Gtk.EventControllerMotion.new()
        motion.connect("motion", self._on_motion)
        self.add_controller(motion)

    def update_shift_state(self, gesture):
        state = gesture.get_current_event_state()
        shift_pressed = bool(state & Gdk.ModifierType.SHIFT_MASK)
        self.current_shift_pressed = shift_pressed

    def _on_click(self, gesture, n_press, x_widget, y_widget):
        original_x, original_y = x_widget, y_widget
        x_widget, y_widget = self.coordinate_transform(x_widget, y_widget)

        # Clicking an existing element with a drawing tool selects it rather than
        # stamping another one on top; existing text opens for editing instead.
        if (self._mode_edits_existing() and n_press == 1
                and self._is_point_in_image(x_widget, y_widget)):
            img_x, img_y = self._widget_to_image_coords(x_widget, y_widget)
            hit = self._existing_action_at(x_widget, y_widget, img_x, img_y)
            if hit is not None:
                self.grab_focus()
                kind, value = hit
                if kind == "select":
                    self.selected_action = value
                    self.queue_draw()
                if (self.options.mode == DrawingMode.TEXT
                        and isinstance(self.selected_action, TextAction)):
                    self._start_text_edit(self.selected_action, original_x, original_y)
                return

        if self.options.mode == DrawingMode.TEXT and self._is_point_in_image(x_widget, y_widget):
            self.grab_focus()
            if n_press == 1:
                self._show_text_entry(original_x,original_y)
        elif self.options.mode == DrawingMode.NUMBER and self._is_point_in_image(x_widget, y_widget) and n_press == 1:
            self.grab_focus()
            img_x, img_y = self._widget_to_image_coords(x_widget, y_widget)
            number_action = NumberStampAction(
                position=(img_x, img_y),
                number=self._next_number,
                options=self.options.copy()
            )

            self.actions.append(number_action)
            self._renumber_actions()
            self.redo_stack.clear()
            self._update_undo_redo_action_states()
            self.queue_draw()

        elif self.options.mode == DrawingMode.SELECT and self._is_point_in_image(x_widget, y_widget):
            self.grab_focus()
            img_x, img_y = self._widget_to_image_coords(x_widget, y_widget)

            if (n_press == 2 and
                self.selected_action and
                isinstance(self.selected_action, TextAction) and
                self.selected_action.contains_point(img_x, img_y)):
                self._start_text_edit(self.selected_action,original_x,original_y)
                return

            if n_press == 1:
                handle = self._get_handle_at_point(x_widget, y_widget)
                if handle != ResizeHandle.NONE and self.selected_action and self._can_resize_action(self.selected_action):
                    return

                if self.selected_action and not self._is_point_in_selection_bounds(img_x, img_y):
                    self.selected_action = None
                    self.queue_draw()

                action = self._find_action_at_point(img_x, img_y)
                if action and action != self.selected_action:
                    self.selected_action = action
                    self.queue_draw()
                elif not action and self.selected_action:
                    self.selected_action = None
                    self.queue_draw()

    def _start_text_edit(self, text_action, widget_x, widget_y):
        self.editing_text_action = text_action
        self.text_position = text_action.position
        self.is_text_editing = True
        self.live_text = text_action.text
        self.text_entry_popup = TextEntryPopover(
            parent=self,
            on_text_activate=self._on_text_entry_activate,
            on_text_changed=self._on_text_entry_changed,
            on_font_size_changed=self._on_font_size_changed,
            font_size=text_action.font_size,
            initial_text=text_action.text
        )
        self.text_entry_popup.connect("closed", self._on_text_entry_popover_closed)
        self.text_entry_popup.popup_at_widget_coords(self, widget_x, widget_y)

    def _show_text_entry(self, x_widget, y_widget):
        original_x, original_y = x_widget, y_widget
        x_widget, y_widget = self.coordinate_transform(x_widget, y_widget)

        if self.text_entry_popup:
            self.text_entry_popup.popdown()
            self.text_entry_popup = None

        self.text_position = self._widget_to_image_coords(x_widget, y_widget)
        self.is_text_editing = True
        self.live_text = ""
        self.editing_text_action = None
        self.text_entry_popup = TextEntryPopover(
            parent=self,
            on_text_activate=self._on_text_entry_activate,
            on_text_changed=self._on_text_entry_changed,
            on_font_size_changed=self._on_font_size_changed,
            font_size=self.font_size
        )
        self.text_entry_popup.connect("closed", self._on_text_entry_popover_closed)
        self.text_entry_popup.popup_at_widget_coords(self, original_x, original_y)

    def _on_font_size_changed(self, spin_button):
        font_size = spin_button.get_value()
        if self.editing_text_action:
            self.editing_text_action.font_size = font_size
            self.editing_text_action.font_size = font_size
            if self.selected_action == self.editing_text_action:
                self.queue_draw()
        else:
            self.font_size = font_size

        if self.live_text:
            self.queue_draw()

    def _on_text_entry_popover_closed(self, popover):
        if self.text_entry_popup and self.text_position:
            text = self.text_entry_popup.get_text().strip()

            if self.editing_text_action:
                if text:
                    self.editing_text_action.text = text
                    if hasattr(self.text_entry_popup, 'spin'):
                        self.editing_text_action.font_size = self.text_entry_popup.spin.get_value()
                else:
                    if self.editing_text_action in self.actions:
                        self.actions.remove(self.editing_text_action)
                    if self.selected_action == self.editing_text_action:
                        self.selected_action = None
                self.redo_stack.clear()
                self._update_undo_redo_action_states()
            else:
                if text:
                    current_settings = self.options.copy()
                    if hasattr(self.text_entry_popup, 'spin'):
                        current_settings.font_size = self.text_entry_popup.spin.get_value()

                    action = TextAction(
                        self.text_position,
                        text,
                        self._get_modified_image_bounds(),
                        current_settings,
                        self.font_size
                    )
                    self.actions.append(action)
                    self.redo_stack.clear()
                    self._update_undo_redo_action_states()

        self._cleanup_text_entry()
        self.queue_draw()

    def _on_text_entry_changed(self, entry):
        if not self.text_entry_popup:
            return
        self.live_text = self.text_entry_popup.get_text().strip()
        if self.editing_text_action:
            self.editing_text_action.text = self.live_text

        self.queue_draw()

    def _on_text_entry_activate(self, entry):
        self._close_text_entry()
        self.queue_draw()

    def _cleanup_text_entry(self):
        if self.text_entry_popup:
            self.text_entry_popup = None
        self.text_position = None
        self.live_text = None
        self.is_text_editing = False
        self.editing_text_action = None

    def _close_text_entry(self):
        if self.text_entry_popup:
            self.text_entry_popup.popdown()
            self.text_entry_popup = None
        self.text_position = None
        self.live_text = None
        self.is_text_editing = False
        self.editing_text_action = None

    def _on_drag_begin(self, gesture, x_widget, y_widget):
        x_widget, y_widget = self.coordinate_transform(x_widget, y_widget)
        if self.text_entry_popup:
            return
        if not self._is_point_in_image(x_widget, y_widget):
            return

        img_x, img_y = self._widget_to_image_coords(x_widget, y_widget)
        self.update_shift_state(gesture)

        # An element already under the pointer gets moved or resized, whichever
        # tool is active, instead of a second one being drawn on top of it.
        if self._mode_edits_existing():
            hit = self._existing_action_at(x_widget, y_widget, img_x, img_y)
            if hit is not None:
                self.grab_focus()
                self._begin_edit_interaction(hit, img_x, img_y)
                return

        if self.options.mode == DrawingMode.TEXT or self.options.mode == DrawingMode.NUMBER:
            return

        self.grab_focus()

        if self.options.mode == DrawingMode.SELECT:
            if self.selected_action:
                handle = self._get_handle_at_point(x_widget, y_widget)
                if handle != ResizeHandle.NONE and self._can_resize_action(self.selected_action):
                    self.is_resizing = True
                    self.resize_handle = handle
                    self.resize_start_bounds = self.selected_action.get_bounds().get_bounding_rect()
                    self.resize_start_mouse = (img_x, img_y)
                    return
                elif self._is_point_in_selection_bounds(img_x, img_y):
                    self.is_moving_selection = True
                    self.move_start_point = (img_x, img_y)
                    return

            self.selected_action = self._find_action_at_point(img_x, img_y)
            if self.selected_action:
                self.is_moving_selection = True
                self.move_start_point = (img_x, img_y)
            self.queue_draw()
            return

        self.is_drawing = True
        if self.options.mode == DrawingMode.PEN or self.options.mode == DrawingMode.HIGHLIGHTER:
            self.current_stroke = [(img_x, img_y)]
        else:
            self.start_point = (img_x, img_y)
            self.end_point = (img_x, img_y)

    def _on_drag_update(self, gesture, dx_widget, dy_widget):
        dx_widget, dy_widget = self.delta_transform(dx_widget, dy_widget)
        editing = self.is_resizing or self.is_moving_selection
        if not editing and self.options.mode in (DrawingMode.TEXT, DrawingMode.NUMBER):
            return

        start_x_raw, start_y_raw = gesture.get_start_point().x, gesture.get_start_point().y
        start_x_widget, start_y_widget = self.coordinate_transform(start_x_raw, start_y_raw)
        cur_x_widget, cur_y_widget = start_x_widget + dx_widget, start_y_widget + dy_widget
        img_x, img_y = self._widget_to_image_coords(cur_x_widget, cur_y_widget)

        self.update_shift_state(gesture)

        if self.is_resizing and self.selected_action and self.resize_start_bounds:
            self._resize_action(self.selected_action, self.resize_handle, self.resize_start_bounds,
                              self.resize_start_mouse, (img_x, img_y), self.current_shift_pressed)
            self.queue_draw()
            return

        if self.is_moving_selection and self.selected_action and self.move_start_point:
            old_x_img, old_y_img = self.move_start_point
            delta_x_img = img_x - old_x_img
            delta_y_img = img_y - old_y_img
            self.selected_action.translate(delta_x_img, delta_y_img)
            self.move_start_point = (img_x, img_y)
            self.queue_draw()
            return

        if not self.is_drawing:
            return

        if self.options.mode == DrawingMode.PEN or self.options.mode == DrawingMode.HIGHLIGHTER:
            self.current_stroke.append((img_x, img_y))
        else:
            self.end_point = (img_x, img_y)
        self.queue_draw()

    def _on_drag_end(self, gesture, dx_widget, dy_widget):
        dx_widget, dy_widget = self.delta_transform(dx_widget, dy_widget)

        if self.is_resizing:
            self.is_resizing = False
            self.resize_handle = ResizeHandle.NONE
            self.resize_start_bounds = None
            self.resize_start_mouse = None
            self.redo_stack.clear()
            self._update_undo_redo_action_states()
            return

        if self.is_moving_selection:
            self.is_moving_selection = False
            self.move_start_point = None
            return

        if self.options.mode in (DrawingMode.TEXT, DrawingMode.NUMBER, DrawingMode.SELECT):
            return

        if not self.is_drawing:
            return

        self.update_shift_state(gesture)

        self.is_drawing = False
        mode = self.options.mode
        if (mode == DrawingMode.PEN or mode == DrawingMode.HIGHLIGHTER) and len(self.current_stroke) > 1:
            if mode == DrawingMode.PEN:
                self.actions.append(StrokeAction(self.current_stroke.copy(), self.options.copy()))
            else:
                self.actions.append(HighlighterAction(self.current_stroke.copy(), self.options.copy(), self.current_shift_pressed))
            self.current_stroke.clear()
        elif self.start_point and self.end_point:
            if mode == DrawingMode.ARROW:
                self.actions.append(ArrowAction(self.start_point, self.end_point,self.current_shift_pressed, self.options.copy()))
            elif mode == DrawingMode.LINE:
                self.actions.append(LineAction(self.start_point, self.end_point,self.current_shift_pressed, self.options.copy()))
            elif mode == DrawingMode.SQUARE:
                self.actions.append(RectAction(self.start_point, self.end_point,self.current_shift_pressed, self.options.copy()))
            elif mode == DrawingMode.CIRCLE:
                self.actions.append(CircleAction(self.start_point, self.end_point,self.current_shift_pressed, self.options.copy()))
            elif mode == DrawingMode.CENSOR:
                censor_action = CensorAction(self.start_point, self.end_point, self._get_background_pixbuf(), self.options.copy())
                current_scale = self._get_scale_factor()
                censor_action.set_original_scale(current_scale)
                self.actions.append(censor_action)

        self.start_point = None
        self.end_point = None
        self.redo_stack.clear()
        self._update_undo_redo_action_states()
        self.queue_draw()

    def _on_motion(self, controller, x_widget, y_widget):
        x_widget, y_widget = self.coordinate_transform(x_widget, y_widget)
        mode = self.options.mode

        if mode == DrawingMode.SELECT:
            name = self._get_select_mode_cursor(x_widget, y_widget)

        elif not self._is_point_in_image(x_widget, y_widget):
            name = "default"

        elif self._hovering_existing_action(x_widget, y_widget):
            # Same feedback the select tool gives, because a press does the same.
            name = self._get_select_mode_cursor(x_widget, y_widget)

        elif mode == DrawingMode.TEXT:
            name = "text"

        else:
            name = "crosshair"

        self.set_cursor(Gdk.Cursor.new_from_name(name, None))

    def _hovering_existing_action(self, x_widget: float, y_widget: float) -> bool:
        if not self._mode_edits_existing() or self.is_drawing:
            return False
        img_x, img_y = self._widget_to_image_coords(x_widget, y_widget)
        return self._existing_action_at(x_widget, y_widget, img_x, img_y) is not None

    def _get_select_mode_cursor(self, x_widget, y_widget):
        if self.is_resizing or self.is_moving_selection:
            if self.is_resizing:
                if isinstance(self.selected_action, (ArrowAction, LineAction)):
                    return "move"
                else:
                    return ResizeHandle.get_cursor_for_handle(self.resize_handle)
            else:
                return "grab"

        img_x, img_y = self._widget_to_image_coords(x_widget, y_widget)

        if self.selected_action:
            handle = self._get_handle_at_point(x_widget, y_widget)
            if handle != ResizeHandle.NONE and self._can_resize_action(self.selected_action):
                if isinstance(self.selected_action, (ArrowAction, LineAction)):
                    return "move"
                else:
                    return ResizeHandle.get_cursor_for_handle(handle)
            elif self._is_point_in_selection_bounds(img_x, img_y):
                return "grab"
            else:
                return "pointer" if self._find_action_at_point(img_x, img_y) else "default"
        else:
            return "pointer" if self._find_action_at_point(img_x, img_y) else "default"

    def _action_node(self, action, image_width: int, image_height: int):
        """
        The action, rasterised once in image space.

        Drawn with the identity projection at scale 1.0 so the node is
        independent of zoom, pan and window size -- do_snapshot applies those
        as a transform, which is what lets the node survive between frames.

        The cache holds the action alongside its node. That keeps the action
        alive, so its id cannot be recycled onto a different object while a node
        is cached under it, and it makes the identity check below meaningful.
        """
        key = id(action)
        cached = self._node_cache.get(key)
        if cached is not None and cached[0] is action:
            return cached[1]

        bounds = Graphene.Rect().init(
            -image_width / 2 - NODE_CACHE_MARGIN,
            -image_height / 2 - NODE_CACHE_MARGIN,
            image_width + 2 * NODE_CACHE_MARGIN,
            image_height + 2 * NODE_CACHE_MARGIN)

        snapshot = Gtk.Snapshot()
        cr = snapshot.append_cairo(bounds)
        cr.set_line_cap(cairo.LineCap.ROUND)
        cr.set_line_join(cairo.LineJoin.ROUND)
        action.draw(cr, _identity_coords, 1.0)
        node = snapshot.to_node()

        self._node_cache[key] = (action, node)
        return node

    def _sync_node_cache(self, image_width: int, image_height: int) -> None:
        """
        Drop nodes for annotations that are gone, and keep the rest.

        Pruning rather than clearing is the whole value: adding the fiftieth
        stroke must not throw away the forty-nine already rasterised. Nodes are
        tied to the image size, so a new image starts over.
        """
        size = (image_width, image_height)
        if size != self._node_cache_size:
            self._node_cache.clear()
            self._node_cache_size = size
            return

        if len(self._node_cache) <= len(self.actions):
            # Nothing can have gone missing without the count dropping, and an
            # entry replaced in place is caught by the identity check on read.
            present = {id(a) for a in self.actions}
            if all(key in present for key in self._node_cache):
                return

        present = {id(a) for a in self.actions}
        for key in [k for k in self._node_cache if k not in present]:
            del self._node_cache[key]

    def invalidate_node_cache(self) -> None:
        self._node_cache.clear()
        self._node_cache_size = None

    def _is_live(self, action) -> bool:
        """An action being edited is redrawn every frame instead of cached."""
        if action is self.selected_action:
            return True
        if self.is_text_editing and action is self.editing_text_action:
            return True
        return False

    def do_snapshot(self, snapshot) -> None:
        ox, oy, dw, dh = self._get_image_bounds()
        if dw <= 0 or dh <= 0:
            return

        scale = self._get_scale_factor()
        image_width, image_height = self._get_modified_image_bounds()
        self._sync_node_cache(image_width, image_height)

        snapshot.push_clip(Graphene.Rect().init(ox, oy, dw, dh))

        # Settled annotations: cached nodes, placed by one transform. This is
        # the whole point -- adding the fiftieth stroke no longer re-rasterises
        # the other forty-nine.
        if self.actions and scale > 0:
            snapshot.save()
            snapshot.translate(Graphene.Point().init(
                ox + (image_width / 2) * scale, oy + (image_height / 2) * scale))
            snapshot.scale(scale, scale)
            for action in self.actions:
                if self.is_text_editing and action is self.editing_text_action:
                    continue
                if self._is_live(action):
                    # Evict rather than merely skip: the action is being moved
                    # or restyled, so any node cached before it was picked up is
                    # already wrong and must not be served when it settles.
                    self._node_cache.pop(id(action), None)
                    continue
                node = self._action_node(action, image_width, image_height)
                if node is not None:
                    snapshot.append_node(node)
            snapshot.restore()

        # Everything still moving under the pointer, drawn in widget space
        # exactly as before.
        cr = snapshot.append_cairo(Graphene.Rect().init(ox, oy, dw, dh))
        cr.set_line_cap(cairo.LineCap.ROUND)
        cr.set_line_join(cairo.LineJoin.ROUND)
        self._draw_live(cr, scale)

        snapshot.pop()

    def _draw_live(self, cr: cairo.Context, scale: float) -> None:
        if self.selected_action is not None and not (
                self.is_text_editing and self.selected_action is self.editing_text_action):
            self.selected_action.draw(cr, self._image_to_widget_coords, scale)

        if self.is_drawing and self.options.mode != DrawingMode.TEXT and self.options.mode != DrawingMode.NUMBER:
            cr.set_source_rgba(*self.options.primary_color)
            if self.options.mode == DrawingMode.PEN and len(self.current_stroke) > 1:
                StrokeAction(self.current_stroke, self.options.copy()).draw(cr, self._image_to_widget_coords, scale)
            elif self.options.mode == DrawingMode.HIGHLIGHTER and len(self.current_stroke) > 1:
                HighlighterAction(self.current_stroke, self.options.copy(), self.current_shift_pressed).draw(cr, self._image_to_widget_coords, scale)
            elif self.start_point and self.end_point:
                if self.options.mode == DrawingMode.ARROW:
                    ArrowAction(self.start_point, self.end_point,self.current_shift_pressed, self.options.copy()).draw(cr, self._image_to_widget_coords, scale)
                elif self.options.mode == DrawingMode.LINE:
                    LineAction(self.start_point, self.end_point,self.current_shift_pressed, self.options.copy()).draw(cr, self._image_to_widget_coords, scale)
                elif self.options.mode == DrawingMode.SQUARE:
                    RectAction(self.start_point, self.end_point, self.current_shift_pressed, self.options.copy()).draw(cr, self._image_to_widget_coords, scale)
                elif self.options.mode == DrawingMode.CIRCLE:
                    CircleAction(self.start_point, self.end_point, self.current_shift_pressed, self.options.copy()).draw(cr, self._image_to_widget_coords, scale)
                elif self.options.mode == DrawingMode.CENSOR:
                    temp_censor = CensorAction(self.start_point, self.end_point, self._get_background_pixbuf(), self.options.copy())
                    temp_censor.set_original_scale(scale)
                    temp_censor.draw(cr, self._image_to_widget_coords, scale)

        if self.is_text_editing and self.text_position and self.live_text:
            if self.editing_text_action:
                preview = TextAction(
                    self.text_position,
                    self.live_text,
                    self._get_modified_image_bounds(),
                    self.editing_text_action.options.copy(),
                    self.editing_text_action.font_size
                )
            else:
                preview = TextAction(
                    self.text_position,
                    self.live_text,
                    self._get_modified_image_bounds(),
                    self.options.copy(),
                    self.font_size
                )
            preview.draw(cr, self._image_to_widget_coords, scale)

        if self.selected_action:
            self._draw_selection_box(cr, scale)

    def export_to_pixbuf(self, requested_width, requested_height) -> GdkPixbuf.Pixbuf | None:
        if not self.picture_widget or not self.picture_widget.get_paintable():
            return None

        paintable = self.picture_widget.get_paintable()
        img_w = paintable.get_intrinsic_width()
        img_h = paintable.get_intrinsic_height()

        scale_factor_x = requested_width / img_w
        scale_factor_y = requested_height / img_h

        return render_actions_to_pixbuf(self.actions, requested_width, requested_height, scale_factor_x, scale_factor_y)

    def clear_drawing(self) -> None:
        self._close_text_entry()
        self.actions.clear()
        self.redo_stack.clear()
        self.selected_action = None
        self.forget_copied_action()
        self._next_number = 1
        self._update_undo_redo_action_states()
        self.queue_draw()

    def undo(self) -> None:
        if self.actions:
            undone_action = self.actions.pop()
            self.redo_stack.append(undone_action)
            self.selected_action = None

            if isinstance(undone_action, NumberStampAction):
                self._renumber_actions()

            self._update_undo_redo_action_states()
            self.queue_draw()

    def redo(self) -> None:
        if self.redo_stack:
            redone_action = self.redo_stack.pop()
            self.actions.append(redone_action)
            self.selected_action = None

            if isinstance(redone_action, NumberStampAction):
                self._renumber_actions()

            self._update_undo_redo_action_states()
            self.queue_draw()

    def _update_undo_redo_action_states(self) -> None:
        root = self.get_root()
        if root:
            undo_action = root.lookup_action("undo")
            if undo_action:
                undo_action.set_enabled(bool(self.actions))
            
            redo_action = root.lookup_action("redo")
            if redo_action:
                redo_action.set_enabled(bool(self.redo_stack))


    def set_drawing_visible(self, is_visible: bool) -> None:
        self.set_visible(is_visible)

    def get_drawing_visible(self) -> bool:
        return self.get_visible()


def render_actions_to_pixbuf(actions: list[DrawingAction], width: int, height: int, scale_factor_x: float = 1.0, scale_factor_y: float = 1.0) -> GdkPixbuf.Pixbuf | None:
    if width <= 0 or height <= 0:
        return None

    surface = cairo.ImageSurface(cairo.Format.ARGB32, width, height)
    cr = cairo.Context(surface)

    cr.set_operator(cairo.Operator.CLEAR)
    cr.paint()
    cr.set_operator(cairo.Operator.OVER)

    def image_coords_to_intrinsic_pixels(x_image: int, y_image: int) -> Tuple[float, float]:
        center_x_intrinsic = width / 2.0
        center_y_intrinsic = height / 2.0
        return (center_x_intrinsic + x_image * scale_factor_x, center_y_intrinsic + y_image * scale_factor_y)

    cr.set_line_cap(cairo.LineCap.ROUND)
    cr.set_line_join(cairo.LineJoin.ROUND)

    scale_factor = (scale_factor_x + scale_factor_y) / 2.0

    for action in actions:
        action.draw(cr, image_coords_to_intrinsic_pixels, scale_factor)

    surface.flush()

    return Gdk.pixbuf_get_from_surface(surface, 0, 0, width, height)
