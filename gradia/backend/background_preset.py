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

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from gradia.backend.settings import Settings
from gradia.utils.colors import parse_rgb_string

MODES = ("none", "solid", "gradient", "image")

DEFAULT_SOLID: dict[str, Any] = {"color": "#4A90E2", "alpha": 1.0}
DEFAULT_GRADIENT: dict[str, Any] = {
    "mode": "linear",
    "steps": [[0.0, "rgb(87,227,137)"], [1.0, "rgb(53,132,228)"]],
    "angle": 135.0,
}
DEFAULT_IMAGE: dict[str, Any] = {"file_path": ""}
DEFAULT_IMAGE_OPTIONS: dict[str, Any] = {
    "padding": 5,
    "corner_radius": 2,
    "aspect_ratio": "",
    "shadow_strength": 5,
    "auto_balance": False,
}


def _color_key(value: Any) -> Any:
    """A colour reduced to its channels, so hex and rgb() spellings compare equal."""
    try:
        return parse_rgb_string(str(value))
    except (ValueError, TypeError):
        return str(value)


def _steps_key(steps: Any) -> tuple:
    """Gradient stops as (position, channels), rounded past the editor's float noise."""
    try:
        return tuple(
            (round(float(position), 4), _color_key(color))
            for position, color in steps
        )
    except (TypeError, ValueError):
        return (repr(steps),)


@dataclass
class BackgroundPreset:
    """A named snapshot of the background mode, every mode's own settings and the image options."""

    name: str
    mode: str = "gradient"
    solid: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SOLID))
    gradient: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_GRADIENT))
    image: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_IMAGE))
    image_options: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_IMAGE_OPTIONS))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mode": self.mode,
            "solid": self.solid,
            "gradient": self.gradient,
            "image": self.image,
            "image_options": self.image_options,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BackgroundPreset":
        mode = data.get("mode", "gradient")
        return cls(
            name=str(data.get("name") or _("Preset")),
            mode=mode if mode in MODES else "gradient",
            solid={**DEFAULT_SOLID, **(data.get("solid") or {})},
            gradient={**DEFAULT_GRADIENT, **(data.get("gradient") or {})},
            image={**DEFAULT_IMAGE, **(data.get("image") or {})},
            image_options={**DEFAULT_IMAGE_OPTIONS, **(data.get("image_options") or {})},
        )

    def copy_as(self, name: str) -> "BackgroundPreset":
        return BackgroundPreset.from_dict({**self.to_dict(), "name": name})

    def fingerprint(self) -> tuple:
        """
        Everything this preset actually renders, in a comparable form.

        The sidebar compares the stored preset against the live settings with
        this to decide whether they have diverged. Two things matter:

        Only the selected mode's own settings count. Applying a preset writes
        all four modes, but the inactive three are off screen and change
        nothing on the canvas, so counting them would light up the save button
        for a difference nobody can see -- and would do so immediately, since a
        preset with no background image still leaves the live image background
        pointing at whatever it last loaded.

        Values are normalised, because the two sides are spelled differently: a
        stored preset can carry hex colours and integer angles, while the live
        state comes back from the editors as rgb() strings and floats.
        """
        return self.background_key() + (
            int(self.image_options.get("padding", 0)),
            int(self.image_options.get("corner_radius", 0)),
            str(self.image_options.get("aspect_ratio") or ""),
            int(self.image_options.get("shadow_strength", 0)),
            bool(self.image_options.get("auto_balance", False)),
        )

    def background_key(self) -> tuple:
        """
        What this preset paints, without the framing around it.

        Two presets with the same key draw the same background, so the sidebar
        uses it to avoid re-rendering -- and re-decoding an image file -- for a
        preview it already has.
        """
        return (self.mode,) + self._mode_fingerprint()

    def _mode_fingerprint(self) -> tuple:
        if self.mode == "solid":
            return (
                _color_key(self.solid.get("color")),
                round(float(self.solid.get("alpha", 1.0)), 4),
            )

        if self.mode == "gradient":
            gradient_mode = self.gradient.get("mode")
            # A radial gradient draws from the centre out, so its angle is inert
            # -- the selector greys it out, and it must not count as a change.
            angle = (
                None if gradient_mode == "radial"
                else round(float(self.gradient.get("angle", 0.0)), 3)
            )
            return (gradient_mode, angle, _steps_key(self.gradient.get("steps") or ()))

        if self.mode == "image":
            return (str(self.image.get("file_path") or ""),)

        return ()


def builtin_presets() -> list["BackgroundPreset"]:
    """
    The presets Gradia ships with: five backgrounds covering the situations a
    screenshot usually ends up in.

    Built here rather than at module scope so the names go through gettext at
    call time, once the translations are installed.
    """
    return [BackgroundPreset.from_dict(entry) for entry in (
        {
            # A dark neutral makes a light interface read as the subject. Wide
            # padding and a deep shadow lift it off the page.
            "name": _("Studio Slate"),
            "mode": "solid",
            "solid": {"color": "#1D2126", "alpha": 1.0},
            "image_options": {
                "padding": 8, "corner_radius": 2, "aspect_ratio": "",
                "shadow_strength": 6, "auto_balance": False,
            },
        },
        {
            # Warm off-white rather than pure white, so the screenshot's own
            # white surfaces still have an edge against it. Tight and quiet,
            # for documentation and anything headed for print.
            "name": _("Paper White"),
            "mode": "solid",
            "solid": {"color": "#F4F2EE", "alpha": 1.0},
            "image_options": {
                "padding": 6, "corner_radius": 1, "aspect_ratio": "",
                "shadow_strength": 3, "auto_balance": False,
            },
        },
        {
            # Indigo through violet into cyan: the hero-image gradient. Locked
            # to 16:9 because this is the one headed for a slide or a banner.
            "name": _("Aurora"),
            "mode": "gradient",
            "gradient": {
                "mode": "linear",
                "steps": [[0.0, "#312E81"], [0.55, "#6D28D9"], [1.0, "#22D3EE"]],
                "angle": 135.0,
            },
            "image_options": {
                "padding": 10, "corner_radius": 2, "aspect_ratio": "16:9",
                "shadow_strength": 7, "auto_balance": False,
            },
        },
        {
            # Amber to crimson, on the diagonal. Warm enough for a blog header
            # or a release note without tipping into neon.
            "name": _("Ember"),
            "mode": "gradient",
            "gradient": {
                "mode": "linear",
                "steps": [[0.0, "#FBB040"], [0.5, "#EF6C4D"], [1.0, "#C9184A"]],
                "angle": 45.0,
            },
            "image_options": {
                "padding": 9, "corner_radius": 2, "aspect_ratio": "",
                "shadow_strength": 6, "auto_balance": False,
            },
        },
        {
            # A radial falloff to near-black is a vignette: the eye goes to the
            # middle and stays there. The most padding of the five, so the
            # darkness has room to close in.
            "name": _("Spotlight"),
            "mode": "gradient",
            "gradient": {
                "mode": "radial",
                "steps": [[0.0, "#3A3F4B"], [1.0, "#0B0D10"]],
                "angle": 0.0,
            },
            "image_options": {
                "padding": 12, "corner_radius": 2, "aspect_ratio": "",
                "shadow_strength": 8, "auto_balance": False,
            },
        },
    )]


class BackgroundPresetStore:
    """Persists the preset list and the selected index in GSettings."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or Settings()
        self._presets: list[BackgroundPreset] = []
        self._active_index = 0
        self._load()

    """
    Loading/Saving
    """

    def _load(self) -> None:
        raw = self.settings.background_presets
        presets: list[BackgroundPreset] = []

        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    presets = [BackgroundPreset.from_dict(entry) for entry in data if isinstance(entry, dict)]
            except (ValueError, TypeError):
                presets = []

        if not presets:
            # A first run keeps whatever the app is already rendering as preset
            # zero, so nothing about the user's background changes underneath
            # them just because presets now exist.
            presets = [self._preset_from_live_settings(_("Default"))]

        presets = self._seed_builtins(presets)

        self._presets = presets
        self._active_index = max(0, min(self.settings.background_preset_index, len(presets) - 1))
        self.save()

    def _seed_builtins(self, presets: list[BackgroundPreset]) -> list[BackgroundPreset]:
        """
        Append the shipped presets, once ever.

        Guarded by its own setting rather than by "is the list empty", so that
        installs which already had presets before this existed still receive
        them -- appended after what is already there, leaving the selected
        index pointing at the same preset it did before.
        """
        if self.settings.builtin_presets_seeded:
            return presets

        existing = {preset.name for preset in presets}
        presets = presets + [
            preset for preset in builtin_presets() if preset.name not in existing
        ]
        self.settings.builtin_presets_seeded = True
        return presets

    def save(self) -> None:
        self.settings.background_presets = json.dumps([preset.to_dict() for preset in self._presets])
        self.settings.background_preset_index = self._active_index

    def _preset_from_live_settings(self, name: str) -> BackgroundPreset:
        """Seed a preset from the settings the app is currently rendering with."""
        return BackgroundPreset.from_dict({
            "name": name,
            "mode": self.settings.background_mode,
            "solid": _parse_json_object(self.settings.solid_state),
            "gradient": _parse_json_object(self.settings.gradient_state),
            "image": _parse_json_object(self.settings.image_state),
            "image_options": {
                "padding": self.settings.image_padding,
                "corner_radius": self.settings.image_corner_radius,
                "aspect_ratio": self.settings.image_aspect_ratio,
                "shadow_strength": self.settings.image_shadow_strength,
                "auto_balance": self.settings.image_auto_balance,
            },
        })

    """
    Accessors
    """

    @property
    def presets(self) -> list[BackgroundPreset]:
        return self._presets

    @property
    def names(self) -> list[str]:
        return [preset.name for preset in self._presets]

    @property
    def active_index(self) -> int:
        return self._active_index

    @property
    def active(self) -> BackgroundPreset:
        return self._presets[self._active_index]

    def set_active_index(self, index: int) -> bool:
        if 0 <= index < len(self._presets) and index != self._active_index:
            self._active_index = index
            self.save()
            return True
        return False

    """
    Mutations
    """

    def unique_name(self, base: str) -> str:
        existing = set(self.names)
        if base not in existing:
            return base
        counter = 2
        while f"{base} {counter}" in existing:
            counter += 1
        return f"{base} {counter}"

    def add(self, preset: BackgroundPreset, make_active: bool = True) -> int:
        preset.name = self.unique_name(preset.name)
        self._presets.append(preset)
        index = len(self._presets) - 1
        if make_active:
            self._active_index = index
        self.save()
        return index

    def remove(self, index: int) -> bool:
        if len(self._presets) <= 1 or not 0 <= index < len(self._presets):
            return False
        del self._presets[index]
        self._active_index = min(self._active_index, len(self._presets) - 1)
        self.save()
        return True

    def rename(self, index: int, name: str) -> bool:
        name = name.strip()
        if not name or not 0 <= index < len(self._presets):
            return False
        if name == self._presets[index].name:
            return False
        others = self.names[:index] + self.names[index + 1:]
        if name in others:
            name = self.unique_name(name)
        self._presets[index].name = name
        self.save()
        return True

    def update_active(
        self,
        mode: Optional[str] = None,
        solid: Optional[dict[str, Any]] = None,
        gradient: Optional[dict[str, Any]] = None,
        image: Optional[dict[str, Any]] = None,
        image_options: Optional[dict[str, Any]] = None,
    ) -> None:
        preset = self.active
        if mode is not None and mode in MODES:
            preset.mode = mode
        if solid is not None:
            preset.solid = solid
        if gradient is not None:
            preset.gradient = gradient
        if image is not None:
            preset.image = image
        if image_options is not None:
            preset.image_options = image_options
        self.save()


def _parse_json_object(raw: Optional[str]) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}
