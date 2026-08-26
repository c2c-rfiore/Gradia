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

import json
from collections.abc import Callable
from typing import Any, Optional

from gi.repository import GObject, Gio, Gtk, Adw, GLib

from gradia.graphics.gradient import Gradient, GradientBackground
from gradia.graphics.gradient_selector import GradientSelector
from gradia.graphics.solid import SolidSelector, SolidBackground
from gradia.graphics.image import ImageSelector, ImageBackground
from gradia.graphics.background import Background
from gradia.ui.widget.toggle_group import ToggleGroup
from gradia.constants import rootdir  # pyright: ignore
from gradia.backend.settings import Settings
from gradia.backend.background_preset import (
    MODES,
    BackgroundPreset,
    BackgroundPresetStore,
)

# Getter/applier pair the sidebar uses to put its image options into a preset.
ImageOptionsGetter = Callable[[], dict[str, Any]]
ImageOptionsApplier = Callable[[dict[str, Any]], None]


@Gtk.Template(resource_path=f"{rootdir}/ui/background_selector.ui")
class BackgroundSelector(Adw.Bin):
    __gtype_name__ = "GradiaBackgroundSelector"

    toggle_group: ToggleGroup = Gtk.Template.Child()
    stack: Gtk.Stack = Gtk.Template.Child()
    stack_revealer: Gtk.Revealer = Gtk.Template.Child()
    preset_dropdown: Gtk.DropDown = Gtk.Template.Child()
    preset_menu_button: Gtk.MenuButton = Gtk.Template.Child()

    def __init__(
        self,
        callback: Optional[Callable[[Background], None]] = None,
        **kwargs
    ) -> None:
        super().__init__(**kwargs)

        self.settings = Settings()
        self.solid = SolidBackground.from_json(self.settings.solid_state or '{}')
        self.gradient = GradientBackground.from_json(self.settings.gradient_state or '{}')
        self.image = ImageBackground.from_json(self.settings.image_state or '{}')
        self.callback = callback
        self.current_mode_callback = None
        self.current_mode = self.settings.background_mode if self.settings.background_mode in MODES else "gradient"
        self.initial_mode = self.current_mode

        self.preset_store = BackgroundPresetStore(self.settings)
        self._applying_preset = False
        self._updating_dropdown = False
        self._image_options_getter: Optional[ImageOptionsGetter] = None
        self._image_options_applier: Optional[ImageOptionsApplier] = None

        self.gradient_selector = GradientSelector(self.gradient, self._on_gradient_changed)
        self.solid_selector = SolidSelector(self.solid, self._on_solid_changed)
        self.image_selector = ImageSelector(self.image, self._on_image_changed)

        self._setup()

    """
    Setup Methods
    """

    def _setup(self) -> None:
        self.toggle_group.set_active_name(self.current_mode)

        self.stack.add_named(self.solid_selector, "solid")
        self.stack.add_named(self.gradient_selector, "gradient")
        self.stack.add_named(self.image_selector, "image")
        if self.current_mode != "none":
            self.stack.set_visible_child_name(self.current_mode)
        self._update_revealer_visibility()
        self._setup_preset_actions()
        self._refresh_preset_dropdown()

    def _setup_preset_actions(self) -> None:
        self.preset_actions = Gio.SimpleActionGroup()

        for name, handler in (
            ("new", self._on_preset_new),
            ("duplicate", self._on_preset_duplicate),
            ("rename", self._on_preset_rename),
            ("delete", self._on_preset_delete),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.preset_actions.add_action(action)

        self.insert_action_group("preset", self.preset_actions)

    """
    Callbacks
    """

    @Gtk.Template.Callback()
    def _on_group_changed(self, group: Adw.ToggleGroup, _param: GObject.ParamSpec, *args) -> None:
        if self._applying_preset:
            return

        active_name = group.get_active_name()
        if active_name in MODES and active_name != self.current_mode:
            self.current_mode = active_name
            self.settings.background_mode = active_name
            if self.current_mode != "none":
                self.stack.set_visible_child_name(active_name)
            self._update_revealer_visibility()
            self.save_active_preset()
            self._notify_current()

    @Gtk.Template.Callback()
    def _on_preset_dropdown_changed(self, dropdown: Gtk.DropDown, _param: GObject.ParamSpec) -> None:
        if self._updating_dropdown:
            return

        index = dropdown.get_selected()
        if index == Gtk.INVALID_LIST_POSITION:
            return

        if self.preset_store.set_active_index(index):
            self._apply_preset(self.preset_store.active)

    def _on_gradient_changed(self, gradient: GradientBackground) -> None:
        if self._applying_preset:
            return
        self.settings.gradient_state = gradient.to_json()
        self.save_active_preset()
        if self.current_mode == "gradient":
            self._notify_current()

    def _on_solid_changed(self, solid: SolidBackground) -> None:
        if self._applying_preset:
            return
        self.settings.solid_state = solid.to_json()
        self.save_active_preset()
        if self.current_mode == "solid":
            self._notify_current()

    def _on_image_changed(self, image: ImageBackground) -> None:
        self.settings.image_state = image.to_json()
        self.save_active_preset()
        if self.current_mode == "image":
            self._notify_current()

    def set_current_mode_callback(self, callback: Callable[[str], None]) -> None:
        self.current_mode_callback = callback
        self._notify_current()

    """
    Preset Methods
    """

    def set_image_options_hooks(
        self,
        getter: ImageOptionsGetter,
        applier: ImageOptionsApplier,
    ) -> None:
        """Let the sidebar contribute its image options to presets, and receive them back."""
        self._image_options_getter = getter
        self._image_options_applier = applier
        self.save_active_preset()

    def save_active_preset(self) -> None:
        """Write the live background and image options into the selected preset."""
        if self._applying_preset:
            return

        image_options = self._image_options_getter() if self._image_options_getter else None
        self.preset_store.update_active(
            mode=self.current_mode,
            solid=json.loads(self.solid.to_json()),
            gradient=json.loads(self.gradient.to_json()),
            image=json.loads(self.image.to_json()),
            image_options=image_options,
        )

    def _apply_preset(self, preset: BackgroundPreset) -> None:
        self._applying_preset = True
        try:
            self.solid.color = preset.solid.get("color", self.solid.color)
            self.solid.alpha = preset.solid.get("alpha", self.solid.alpha)
            self.settings.solid_state = self.solid.to_json()
            self.solid_selector.refresh_from_background()

            self.gradient_selector.set_gradient(Gradient.from_json(json.dumps(preset.gradient)))
            self.settings.gradient_state = self.gradient.to_json()

            self._apply_mode(preset.mode)

            if self._image_options_applier:
                self._image_options_applier(preset.image_options)
        finally:
            self._applying_preset = False

        # Loading an image is asynchronous, so it runs outside of the guard and
        # reports back through _on_image_changed once the file is decoded.
        self.image_selector.set_image_path(preset.image.get("file_path", ""))

        self._notify_current()

    def _apply_mode(self, mode: str) -> None:
        if mode not in MODES:
            mode = "gradient"

        self.current_mode = mode
        self.settings.background_mode = mode
        self.toggle_group.set_active_name(mode)
        if mode != "none":
            self.stack.set_visible_child_name(mode)
        self._update_revealer_visibility()

    def _refresh_preset_dropdown(self) -> None:
        self._updating_dropdown = True
        try:
            model = Gtk.StringList.new(self.preset_store.names)
            self.preset_dropdown.set_model(model)
            self.preset_dropdown.set_selected(self.preset_store.active_index)
        finally:
            self._updating_dropdown = False

        self.preset_actions.lookup_action("delete").set_enabled(len(self.preset_store.presets) > 1)

    """
    Preset Actions
    """

    def _on_preset_new(self, _action: Gio.SimpleAction, _param: Optional[GLib.Variant]) -> None:
        self._prompt_for_name(
            heading=_("New Preset"),
            initial=self.preset_store.unique_name(_("Preset")),
            on_accept=self._create_preset,
        )

    def _create_preset(self, name: str) -> None:
        # A new preset starts from the defaults; use Duplicate to branch off the current one.
        self._add_preset(BackgroundPreset(name=name))

    def _on_preset_duplicate(self, _action: Gio.SimpleAction, _param: Optional[GLib.Variant]) -> None:
        active = self.preset_store.active
        self._add_preset(active.copy_as(_("{name} copy").format(name=active.name)))

    def _add_preset(self, preset: BackgroundPreset) -> None:
        self.preset_store.add(preset)
        self._refresh_preset_dropdown()
        self._apply_preset(self.preset_store.active)

    def _on_preset_rename(self, _action: Gio.SimpleAction, _param: Optional[GLib.Variant]) -> None:
        self._prompt_for_name(
            heading=_("Rename Preset"),
            initial=self.preset_store.active.name,
            on_accept=self._rename_active_preset,
        )

    def _rename_active_preset(self, name: str) -> None:
        if self.preset_store.rename(self.preset_store.active_index, name):
            self._refresh_preset_dropdown()

    def _on_preset_delete(self, _action: Gio.SimpleAction, _param: Optional[GLib.Variant]) -> None:
        if len(self.preset_store.presets) <= 1:
            return

        name = self.preset_store.active.name
        dialog = Adw.AlertDialog(
            heading=_("Delete Preset?"),
            body=_("“{name}” will be removed permanently.").format(name=name),
        )
        dialog.add_response("cancel", _("_Cancel"))
        dialog.add_response("delete", _("_Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_delete_response)
        dialog.present(self.get_root())

    def _on_delete_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        if response != "delete":
            return
        if self.preset_store.remove(self.preset_store.active_index):
            self._refresh_preset_dropdown()
            self._apply_preset(self.preset_store.active)

    def _prompt_for_name(
        self,
        heading: str,
        initial: str,
        on_accept: Callable[[str], None],
    ) -> None:
        dialog = Adw.AlertDialog(heading=heading)
        entry = Gtk.Entry(text=initial, activates_default=True)
        entry.select_region(0, -1)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", _("_Cancel"))
        dialog.add_response("save", _("_Save"))
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
            name = entry.get_text().strip()
            if response == "save" and name:
                on_accept(name)

        dialog.connect("response", on_response)
        dialog.present(self.get_root())

    """
    Internal Methods
    """

    def _update_revealer_visibility(self) -> None:
        should_reveal = self.current_mode != "none"
        currently_revealed = self.stack_revealer.get_child_revealed()
        self.stack_revealer.set_reveal_child(should_reveal)

        if should_reveal:
            if not currently_revealed:
                GLib.timeout_add(300, lambda: (
                    self.stack_revealer.set_overflow(Gtk.Overflow.VISIBLE), False
                )[1])
            else:
                self.stack_revealer.set_overflow(Gtk.Overflow.VISIBLE)
        else:
            self.stack_revealer.set_overflow(Gtk.Overflow.HIDDEN)

    # TODO: Fix callback type error
    def _notify_current(self) -> None:
        if self.callback:
            current_background = self.get_current_background()
            self.callback(current_background)
        if self.current_mode_callback:
            self.current_mode_callback(self.current_mode)

    def get_current_background(self) -> GradientBackground | SolidBackground | ImageBackground | None:
        backgrounds: dict[str, GradientBackground | SolidBackground | ImageBackground] = {
            "gradient": self.gradient,
            "solid": self.solid,
            "image": self.image
        }
        return backgrounds.get(self.current_mode)
