# Copyright (C) 2025 tfuxu, Alexander Vanhee
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

from typing import Callable
from dataclasses import dataclass
from gi.repository import Gtk, Adw
from gradia.ui.background_selector import BackgroundSelector
from gradia.ui.widget.background_aspect_ratio_selector import AspectRatioSelector
from gradia.graphics.background import Background
from gradia.constants import rootdir  # pyright: ignore
from gradia.backend.settings import Settings


@dataclass
class ImageOptions:
    background: Background
    padding: int
    corner_radius: int
    aspect_ratio: str
    shadow_strength: int
    auto_balance: bool
    rotation: int


@Gtk.Template(resource_path=f"{rootdir}/ui/image_sidebar.ui")
class ImageSidebar(Adw.Bin):
    __gtype_name__ = "GradiaImageSidebar"

    background_selector_group: Adw.PreferencesGroup = Gtk.Template.Child()
    image_options_group = Gtk.Template.Child()
    padding_row: Adw.SpinRow = Gtk.Template.Child()
    padding_adjustment: Gtk.Adjustment = Gtk.Template.Child()
    corner_radius_row: Adw.SpinRow = Gtk.Template.Child()
    shadow_strength_row: Adw.ActionRow = Gtk.Template.Child()
    corner_radius_adjustment: Gtk.Adjustment = Gtk.Template.Child()
    shadow_strength_scale: Gtk.Scale = Gtk.Template.Child()
    auto_balance_toggle: Gtk.Switch = Gtk.Template.Child()
    filename_row: Adw.ActionRow = Gtk.Template.Child()
    location_row: Adw.ActionRow = Gtk.Template.Child()
    processed_size_row: Adw.ActionRow = Gtk.Template.Child()
    share_button: Gtk.Button = Gtk.Template.Child()
    rotate_left_button: Gtk.Button = Gtk.Template.Child()
    rotate_right_button: Gtk.Button = Gtk.Template.Child()
    aspect_ratio_selector: AspectRatioSelector = Gtk.Template.Child()
    image_options_expander: Adw.ExpanderRow = Gtk.Template.Child()
    file_info_expander: Adw.ExpanderRow = Gtk.Template.Child()
    rotation_row: Adw.ActionRow = Gtk.Template.Child()
    padding_slider_row: Adw.ActionRow = Gtk.Template.Child()
    corner_radius_slider_row: Adw.ActionRow = Gtk.Template.Child()

    def __init__(
        self,
        on_image_options_changed: Callable[[ImageOptions], None],
        **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self._background_mode = "none"
        self.on_image_options_changed = on_image_options_changed
        self.settings = Settings()
        self._updating_widgets = False
        self._current_rotation = 0
        self._current_background = None
        self._sections_ready = False

        self.background_selector: BackgroundSelector = BackgroundSelector(
            callback=self._on_background_changed
        )
        self.background_selector.set_current_mode_callback(self._on_background_mode_changed)

        self.background_selector_group.add(self.background_selector)

        self._setup_widgets()
        self._connect_signals()
        self.background_selector.set_image_options_hooks(
            self._get_preset_image_options,
            self._apply_preset_image_options,
        )
        self._setup_sections()

    def _setup_widgets(self) -> None:
        self.padding_adjustment.set_value(self.settings.image_padding)
        self.corner_radius_adjustment.set_value(self.settings.image_corner_radius)
        self.shadow_strength_scale.set_value(self.settings.image_shadow_strength)
        self.auto_balance_toggle.set_active(self.settings.image_auto_balance)
        self.aspect_ratio_selector.set_ratio(self.settings.image_aspect_ratio)

    """
    Sidebar Sections
    """

    ROW_VISIBILITY_KEYS = (
        "show-padding",
        "show-corner-radius",
        "show-aspect-ratio",
        "show-shadow",
        "show-auto-balance",
        "show-rotation",
    )
    CONTROL_STYLE_KEYS = ("padding-control-style", "corner-radius-control-style")
    STEP_KEYS = ("padding-step", "corner-radius-step")

    def _setup_sections(self) -> None:
        self.settings.bind_boolean(self.image_options_expander, "expanded", "expand-image-options")
        self.settings.bind_boolean(self.file_info_expander, "expanded", "expand-file-info")

        self._sections_ready = True

        # Which rows appear, and whether padding/corner radius are increment
        # buttons or sliders, are all preferences.
        for key in self.ROW_VISIBILITY_KEYS + self.CONTROL_STYLE_KEYS:
            self.settings.connect_changed(key, self._apply_row_visibility)
        for key in self.STEP_KEYS:
            self.settings.connect_changed(key, self._apply_step_increments)

        self._apply_step_increments()
        self._apply_row_visibility()

    def _apply_step_increments(self) -> None:
        """How much one press of a plus/minus button moves the value."""
        self.padding_adjustment.set_step_increment(self.settings.padding_step)
        self.padding_adjustment.set_page_increment(self.settings.padding_step)
        self.corner_radius_adjustment.set_step_increment(self.settings.corner_radius_step)
        self.corner_radius_adjustment.set_page_increment(self.settings.corner_radius_step)

    def _apply_row_visibility(self) -> None:
        """A row shows when the preference allows it and the current mode supports it."""
        if not self._sections_ready:
            return  # called from the background selector before the sections are set up

        mode_allows = self._background_mode != "none"
        show = self.settings.get_boolean

        # Padding and corner radius each have a spin row and a slider row sharing
        # one adjustment; the preference decides which of the pair is on screen.
        padding_spin = self.settings.padding_control_style == "spin"
        self.padding_row.set_visible(show("show-padding") and padding_spin)
        self.padding_slider_row.set_visible(show("show-padding") and not padding_spin)
        # Padding stays visible but goes insensitive without a background.
        self.padding_row.set_sensitive(mode_allows)
        self.padding_slider_row.set_sensitive(mode_allows)

        radius_spin = self.settings.corner_radius_control_style == "spin"
        radius_wanted = show("show-corner-radius") and mode_allows
        self.corner_radius_row.set_visible(radius_wanted and radius_spin)
        self.corner_radius_slider_row.set_visible(radius_wanted and not radius_spin)

        self.aspect_ratio_selector.set_visible(show("show-aspect-ratio") and mode_allows)
        self.shadow_strength_row.set_visible(show("show-shadow") and mode_allows)
        self.auto_balance_toggle.set_visible(show("show-auto-balance"))
        self.rotation_row.set_visible(show("show-rotation"))

        self.image_options_expander.set_visible(
            any(show(key) for key in self.ROW_VISIBILITY_KEYS)
        )

    def _on_background_changed(self, updated_background: Background) -> None:
        self._current_background = updated_background
        if updated_background != None:
            self._notify_image_options_changed()

    @Gtk.Template.Callback()
    def _on_aspect_ratio_changed(self, widget, ratio) -> None:
        if not self._updating_widgets:
            self.settings.image_aspect_ratio = ratio
            self.background_selector.save_active_preset()
            self._notify_image_options_changed()

    def _connect_signals(self) -> None:
        # The spin row and the slider share one adjustment, so watch the adjustment
        # rather than either widget.
        self.padding_adjustment.connect("value-changed", self._on_padding_changed)
        self.corner_radius_adjustment.connect("value-changed", self._on_corner_radius_changed)
        self.shadow_strength_scale.connect("value-changed", self._on_shadow_strength_changed)
        self.auto_balance_toggle.connect("notify::active", self._on_auto_balance_changed)
        self.rotate_left_button.connect("clicked", self._on_rotate_left_clicked)
        self.rotate_right_button.connect("clicked", self._on_rotate_right_clicked)

    def _on_padding_changed(self, widget) -> None:
        if not self._updating_widgets:
            value = int(widget.get_value())
            self.settings.image_padding = value
            self.background_selector.save_active_preset()
            self._notify_image_options_changed()

    def _on_corner_radius_changed(self, widget) -> None:
        if not self._updating_widgets:
            value = int(widget.get_value())
            self.settings.image_corner_radius = value
            self.background_selector.save_active_preset()
            self._notify_image_options_changed()

    def _on_shadow_strength_changed(self, widget) -> None:
        if not self._updating_widgets:
            value = int(widget.get_value())
            self.settings.image_shadow_strength = value
            self.background_selector.save_active_preset()
            self._notify_image_options_changed()

    def _on_auto_balance_changed(self, widget, pspec) -> None:
        if not self._updating_widgets:
            value = widget.get_active()
            self.settings.image_auto_balance = value
            self.background_selector.save_active_preset()
            self._notify_image_options_changed()

    def _on_rotate_left_clicked(self, button: Gtk.Button) -> None:
        if not self._updating_widgets:
            self._current_rotation = (self._current_rotation - 90) % 360
            self._notify_image_options_changed()

    def _on_rotate_right_clicked(self, button: Gtk.Button) -> None:
        if not self._updating_widgets:
            self._current_rotation = (self._current_rotation + 90) % 360
            self._notify_image_options_changed()

    def reset_rotation(self) -> None:
        self._current_rotation = 0
        self.settings.image_rotation = 0
        self._notify_image_options_changed()

    """
    Background Preset Hooks
    """

    def _get_preset_image_options(self) -> dict:
        """Snapshot the image options for storage in a background preset."""
        return {
            "padding": int(self.padding_adjustment.get_value()),
            "corner_radius": int(self.corner_radius_adjustment.get_value()),
            "aspect_ratio": self.aspect_ratio_selector.get_ratio(),
            "shadow_strength": int(self.shadow_strength_scale.get_value()),
            "auto_balance": self.auto_balance_toggle.get_active(),
        }

    def _apply_preset_image_options(self, options: dict) -> None:
        """Restore the image options of a background preset without re-triggering a save."""
        was_updating = self._updating_widgets
        self._updating_widgets = True
        try:
            self.settings.image_padding = int(options["padding"])
            self.settings.image_corner_radius = int(options["corner_radius"])
            self.settings.image_aspect_ratio = str(options["aspect_ratio"])
            self.settings.image_shadow_strength = int(options["shadow_strength"])
            self.settings.image_auto_balance = bool(options["auto_balance"])

            self.padding_adjustment.set_value(self.settings.image_padding)
            self.corner_radius_adjustment.set_value(self.settings.image_corner_radius)
            self.shadow_strength_scale.set_value(self.settings.image_shadow_strength)
            self.auto_balance_toggle.set_active(self.settings.image_auto_balance)
            self.aspect_ratio_selector.set_ratio(self.settings.image_aspect_ratio)
        except (KeyError, TypeError, ValueError) as e:
            print(f"Skipping malformed image options in preset: {e}")
        finally:
            self._updating_widgets = was_updating

    def _get_current_options(self) -> ImageOptions:
        return ImageOptions(
            padding=int(self.padding_adjustment.get_value()),
            corner_radius=int(self.corner_radius_adjustment.get_value()),
            aspect_ratio=self.aspect_ratio_selector.get_ratio(),
            shadow_strength=int(self.shadow_strength_scale.get_value()),
            auto_balance=self.auto_balance_toggle.get_active(),
            rotation=self._current_rotation,
            background=self._current_background
        )

    def _get_disabled_options(self) -> ImageOptions:
        return ImageOptions(
            padding=0,
            corner_radius=0,
            aspect_ratio="",
            shadow_strength=0,
            auto_balance=self.auto_balance_toggle.get_active(),
            rotation=self._current_rotation,
            background = None
        )

    def _get_settings_options(self) -> ImageOptions:
        return ImageOptions(
            padding=self.settings.image_padding,
            corner_radius=self.settings.image_corner_radius,
            aspect_ratio=self.settings.image_aspect_ratio,
            shadow_strength=self.settings.image_shadow_strength,
            auto_balance=self.settings.image_auto_balance,
            background=None
        )

    def _notify_image_options_changed(self) -> None:
        if self._background_mode == "none":
            options = self._get_disabled_options()
        else:
            options = self._get_current_options()

        self.on_image_options_changed(options)

    def _on_background_mode_changed(self, mode: str) -> None:
        self._background_mode = mode
        is_disabled = mode == "none"
        self._updating_widgets = True

        self._apply_row_visibility()

        if is_disabled:
            options = self._get_disabled_options()
        else:
            options = self._get_current_options()

        self.on_image_options_changed(options)
        self._updating_widgets = False

