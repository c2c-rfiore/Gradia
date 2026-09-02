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

import base64
import io
import json
from collections.abc import Callable
from typing import Any, Optional

from gi.repository import GObject, Gdk, Gtk, Adw, GLib, Pango

from gradia.graphics.gradient import Gradient, GradientBackground
from gradia.graphics.gradient_selector import GradientSelector
from gradia.graphics.solid import SolidSelector, SolidBackground
from gradia.graphics.image import ImageSelector, ImageBackground
from gradia.graphics.background import Background
from gradia.ui.widget.toggle_group import ToggleGroup
from gradia.constants import rootdir  # pyright: ignore
from gradia.backend.settings import Settings
from gradia.utils.colors import hex_to_rgba, parse_rgb_string
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
    background_expander: Adw.ExpanderRow = Gtk.Template.Child()

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
        self._preset_rows: list[Gtk.ListBoxRow] = []
        self._preset_list_busy = False
        # Owned by the sidebar, which shows them above the sections.
        self.preset_button: Optional[Gtk.MenuButton] = None
        self.save_preset_button: Optional[Gtk.Button] = None
        self._button_fill_provider: Optional[Gtk.CssProvider] = None
        self._background_cache: dict[tuple, Optional[Background]] = {}
        self._button_css_cache: dict[tuple, str] = {}
        self._preset_dirty = False
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
        self._setup_preset_popover()
        self._refresh_presets()
        self.settings.bind_boolean(self.background_expander, "expanded", "expand-background")

    def _setup_preset_popover(self) -> None:
        """Build the preset popover: New Preset first, then the presets, then rename/delete."""
        self.preset_popover = Gtk.Popover(has_arrow=True, width_request=240)
        self.preset_popover.add_css_class("menu")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        self.new_preset_button = self._popover_button(
            _("New Preset…"), "list-add-symbolic", self._on_preset_new
        )
        box.append(self.new_preset_button)
        box.append(Gtk.Separator(margin_top=3, margin_bottom=3))

        self.preset_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.preset_list.add_css_class("preset-list")
        self.preset_list.connect("row-activated", self._on_preset_row_activated)

        scroller = Gtk.ScrolledWindow(
            propagate_natural_height=True,
            max_content_height=260,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            child=self.preset_list,
        )
        box.append(scroller)

        box.append(Gtk.Separator(margin_top=3, margin_bottom=3))
        self.rename_preset_button = self._popover_button(
            _("Rename…"), "document-edit-symbolic", self._on_preset_rename
        )
        self.delete_preset_button = self._popover_button(
            _("Delete Preset"), "user-trash-symbolic", self._on_preset_delete
        )
        self.delete_preset_button.add_css_class("destructive-label")
        box.append(self.rename_preset_button)
        box.append(self.delete_preset_button)

        self.preset_popover.set_child(box)

    def _popover_button(self, label: str, icon_name: str, handler) -> Gtk.Button:
        content = Gtk.Box(spacing=12)
        content.append(Gtk.Image(icon_name=icon_name))
        content.append(Gtk.Label(label=label, xalign=0, hexpand=True))

        button = Gtk.Button(child=content)
        button.add_css_class("flat")
        button.connect("clicked", lambda _b: (self.preset_popover.popdown(), handler()))
        return button

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
            self.refresh_preset_dirty_state()
            self._notify_current()

    def attach_preset_button(self, button: Gtk.MenuButton) -> None:
        """Adopt the preset button the sidebar shows above the sections."""
        self.preset_button = button
        button.set_popover(self.preset_popover)
        self._refresh_presets()

    def attach_preset_save_button(self, button: Gtk.Button) -> None:
        """Adopt the save button the sidebar shows beside the preset button."""
        self.save_preset_button = button
        button.connect("clicked", lambda _button: self.commit_active_preset())
        self._update_save_button()

    def _on_preset_row_activated(self, _list_box: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        if self._preset_list_busy:
            return

        self.preset_popover.popdown()
        if self.preset_store.set_active_index(row.get_index()):
            self._apply_preset(self.preset_store.active)
            self._refresh_presets()

    def _on_gradient_changed(self, gradient: GradientBackground) -> None:
        if self._applying_preset:
            return
        self.settings.gradient_state = gradient.to_json()
        self.refresh_preset_dirty_state()
        if self.current_mode == "gradient":
            self._notify_current()

    def _on_solid_changed(self, solid: SolidBackground) -> None:
        if self._applying_preset:
            return
        self.settings.solid_state = solid.to_json()
        self.refresh_preset_dirty_state()
        if self.current_mode == "solid":
            self._notify_current()

    def _on_image_changed(self, image: ImageBackground) -> None:
        self.settings.image_state = image.to_json()
        self.refresh_preset_dirty_state()
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
        self.refresh_preset_dirty_state()

    def _live_preset(self) -> BackgroundPreset:
        """The settings as they stand right now, in the shape of a preset."""
        image_options = self._image_options_getter() if self._image_options_getter else None
        return BackgroundPreset.from_dict({
            "name": self.preset_store.active.name,
            "mode": self.current_mode,
            "solid": json.loads(self.solid.to_json()),
            "gradient": json.loads(self.gradient.to_json()),
            "image": json.loads(self.image.to_json()),
            "image_options": image_options,
        })

    def refresh_preset_dirty_state(self) -> None:
        """
        Re-check whether the live settings still match the selected preset.

        Editing no longer writes back into the preset. Instead every control
        that changes the background calls this, and the save button beside the
        preset appears for as long as the two differ.
        """
        if self._applying_preset:
            return

        if self._image_options_getter is None:
            # The sidebar has not handed its image options over yet; comparing
            # now would read the defaults and report a difference that is not
            # really there.
            return

        dirty = self._live_preset().fingerprint() != self.preset_store.active.fingerprint()
        if dirty == self._preset_dirty:
            return

        self._preset_dirty = dirty
        self._update_save_button()

    def _update_save_button(self) -> None:
        """Live and green while there are unsaved changes, plain and dim otherwise."""
        if self.save_preset_button is None:
            return
        self.save_preset_button.set_sensitive(self._preset_dirty)
        if self._preset_dirty:
            self.save_preset_button.add_css_class("has-changes")
        else:
            self.save_preset_button.remove_css_class("has-changes")

    def commit_active_preset(self) -> None:
        """Overwrite the selected preset with the settings currently on screen."""
        live = self._live_preset()
        self.preset_store.update_active(
            mode=live.mode,
            solid=live.solid,
            gradient=live.gradient,
            image=live.image,
            image_options=live.image_options,
        )
        # The swatch in the popover is drawn from the stored preset, so it has
        # to be redrawn now that the stored preset has moved.
        self._refresh_presets()
        self.refresh_preset_dirty_state()

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
        # reports back through _on_image_changed once the file is decoded. Until
        # it does, the live image background still points at the old file, so
        # leave the comparison to that callback rather than flashing the save
        # button on and straight back off again.
        target_path = preset.image.get("file_path", "")
        awaiting_image = bool(target_path) and target_path != self.image.file_path
        self.image_selector.set_image_path(target_path)
        if not awaiting_image:
            self.refresh_preset_dirty_state()

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

    def _refresh_presets(self) -> None:
        """Rebuild the popover list and put the active preset's name on the button."""
        self._preset_list_busy = True
        try:
            for row in self._preset_rows:
                self.preset_list.remove(row)
            self._preset_rows = []

            for index, preset in enumerate(self.preset_store.presets):
                row = Gtk.ListBoxRow(activatable=True)
                content = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6,
                                  margin_start=6, margin_end=6)
                content.append(self._preset_swatch(preset))
                content.append(Gtk.Label(label=preset.name, xalign=0, hexpand=True,
                                         ellipsize=Pango.EllipsizeMode.END))

                check = Gtk.Image(icon_name="object-select-symbolic")
                check.set_opacity(1.0 if index == self.preset_store.active_index else 0.0)
                content.append(check)

                row.set_child(content)
                self.preset_list.append(row)
                self._preset_rows.append(row)
        finally:
            self._preset_list_busy = False

        if self.preset_button is not None:
            self.preset_button.set_label(self.preset_store.active.name)
        self._paint_preset_button()
        self.delete_preset_button.set_sensitive(len(self.preset_store.presets) > 1)

    """
    Preset Previews
    """

    SWATCH_SIZE = 34
    # The button's fill is rendered once at roughly its own proportions and then
    # stretched to whatever width the sidebar ends up at.
    BUTTON_FILL_WIDTH = 320
    BUTTON_FILL_HEIGHT = 40
    # Matches is_light_color_hex: 0.299r + 0.587g + 0.114b, out of 255.
    LIGHT_INK_ABOVE = 130

    def _background_for(self, preset: BackgroundPreset) -> Optional[Background]:
        """
        The preset's background, ready to render, kept between refreshes.

        Cached on what the preset paints rather than on the whole preset, so
        renaming one or nudging its padding does not throw the render away --
        and an image preset is decoded once instead of on every refresh.
        """
        key = preset.background_key()
        if key in self._background_cache:
            return self._background_cache[key]

        background: Optional[Background] = None
        try:
            if preset.mode == "solid":
                background = SolidBackground(
                    color=preset.solid.get("color", "#000000"),
                    alpha=float(preset.solid.get("alpha", 1.0)),
                )
            elif preset.mode == "gradient":
                background = GradientBackground.from_json(json.dumps(preset.gradient))
            elif preset.mode == "image":
                file_path = preset.image.get("file_path") or ""
                if file_path:
                    background = ImageBackground(file_path)
        except Exception as error:
            print(f"Could not prepare a preview for preset “{preset.name}”: {error}")
            background = None

        self._background_cache[key] = background
        return background

    def _render_preset(self, preset: BackgroundPreset, width: int, height: int):
        """The preset's background as a PIL image, or None if it has none."""
        background = self._background_for(preset)
        if background is None:
            return None
        try:
            return background.prepare_image(width, height)
        except Exception as error:
            print(f"Could not render preset “{preset.name}”: {error}")
            return None

    @staticmethod
    def _texture(image) -> Gdk.Texture:
        if image.mode != "RGBA":
            image = image.convert("RGBA")
        return Gdk.MemoryTexture.new(
            image.width, image.height,
            Gdk.MemoryFormat.R8G8B8A8,
            GLib.Bytes.new(image.tobytes()),
            image.width * 4,
        )

    def _preset_swatch(self, preset: BackgroundPreset) -> Gtk.Widget:
        """
        A thumbnail of what the preset paints, for its row in the popover.

        Every mode goes through the same render at exactly the swatch size, so
        the tiles are identical in size and the names beside them all start at
        the same place. Handing GTK a picture and asking it to shrink would not:
        a widget's size request is a floor, so an image background would have
        stretched the row to the photograph's own width.

        The tile underneath is a checkerboard, which is therefore what shows
        through a background that is partly transparent, and what is left when
        there is no background at all -- or when the image file has since moved.
        """
        swatch = Adw.Bin(
            width_request=self.SWATCH_SIZE,
            height_request=self.SWATCH_SIZE,
            hexpand=False,
            vexpand=False,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            overflow=Gtk.Overflow.HIDDEN,
        )
        swatch.add_css_class("preset-swatch")

        image = self._render_preset(preset, self.SWATCH_SIZE, self.SWATCH_SIZE)
        if image is not None:
            swatch.set_child(Gtk.Picture(
                paintable=self._texture(image),
                content_fit=Gtk.ContentFit.FILL,
                can_shrink=True,
            ))

        return swatch

    def _paint_preset_button(self) -> None:
        """
        Fill the preset button with the background it names.

        The fill has to land on the button node inside the menubutton, which a
        provider added to a widget's own style context cannot reach -- so this
        goes on the display and is swapped out whole each time the selected
        preset changes.
        """
        if self.preset_button is None:
            return

        display = self.preset_button.get_display()
        if display is None:
            return

        if self._button_fill_provider is not None:
            Gtk.StyleContext.remove_provider_for_display(display, self._button_fill_provider)
            self._button_fill_provider = None

        css = self._preset_button_css(self.preset_store.active)
        if not css:
            # Nothing to preview, so the accent fill in style.css stands.
            return

        provider = Gtk.CssProvider()
        provider.load_from_string(css)
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 3
        )
        self._button_fill_provider = provider

    def _preset_button_css(self, preset: BackgroundPreset) -> str:
        key = preset.background_key()
        if key in self._button_css_cache:
            return self._button_css_cache[key]

        css = self._build_preset_button_css(preset)
        self._button_css_cache[key] = css
        return css

    def _build_preset_button_css(self, preset: BackgroundPreset) -> str:
        if preset.mode == "solid":
            # A flat colour is crisper drawn by the CSS engine than resampled
            # from a bitmap, and it is the one case that needs no bitmap.
            try:
                rgba = hex_to_rgba(
                    preset.solid.get("color", "#000000"),
                    float(preset.solid.get("alpha", 1.0)),
                )
                fill = f"background-image: none; background-color: {rgba.to_string()};"
                luminance = self._luminance(parse_rgb_string(preset.solid.get("color", "#000000")))
            except Exception as error:
                print(f"Could not paint the preset button for “{preset.name}”: {error}")
                return ""
        else:
            image = self._render_preset(
                preset, self.BUTTON_FILL_WIDTH, self.BUTTON_FILL_HEIGHT)
            if image is None:
                return ""
            # Inlining the render rather than writing the gradient back out as
            # CSS is what keeps the button, the swatch and the canvas from
            # drifting: all three come from the same generator. It also settles
            # the ink for an image background without a second decode.
            fill = (
                f'background-image: url("{self._png_data_uri(image)}");'
                " background-color: transparent;"
                " background-size: 100% 100%; background-repeat: no-repeat;"
            )
            luminance = self._mean_luminance(image)

        if luminance >= self.LIGHT_INK_ABOVE:
            ink, shadow = "rgba(0, 0, 0, 0.85)", "0 1px 1px alpha(white, 0.35)"
        else:
            ink, shadow = "#ffffff", "0 1px 2px alpha(black, 0.45)"

        return (
            ".preset-menu-button > button {"
            f" {fill} color: {ink}; text-shadow: {shadow}; }}"
        )

    @staticmethod
    def _png_data_uri(image) -> str:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, "PNG", optimize=True)
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")

    @staticmethod
    def _luminance(rgb: tuple[int, int, int]) -> float:
        red, green, blue = rgb
        return 0.299 * red + 0.587 * green + 0.114 * blue

    @classmethod
    def _mean_luminance(cls, image) -> float:
        # Averaging by resizing to a single pixel, rather than walking 12,800.
        return cls._luminance(image.convert("RGB").resize((1, 1)).getpixel((0, 0)))

    """
    Preset Actions
    """

    def _on_preset_new(self) -> None:
        self._prompt_for_name(
            heading=_("New Preset"),
            initial=self.preset_store.unique_name(_("Preset")),
            on_accept=self._create_preset,
        )

    def _create_preset(self, name: str) -> None:
        # A new preset captures what is on screen, which is what makes diverging
        # from a preset safe: the save button overwrites the one you are on,
        # this keeps both. Nothing needs applying afterwards -- these settings
        # are already the live ones -- so the canvas does not flicker either.
        self.preset_store.add(self._live_preset().copy_as(name))
        self._refresh_presets()
        self.refresh_preset_dirty_state()

    def _on_preset_rename(self) -> None:
        self._prompt_for_name(
            heading=_("Rename Preset"),
            initial=self.preset_store.active.name,
            on_accept=self._rename_active_preset,
        )

    def _rename_active_preset(self, name: str) -> None:
        if self.preset_store.rename(self.preset_store.active_index, name):
            self._refresh_presets()

    def _on_preset_delete(self) -> None:
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
            self._refresh_presets()
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
