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
            presets = [self._preset_from_live_settings(_("Default"))]

        self._presets = presets
        self._active_index = max(0, min(self.settings.background_preset_index, len(presets) - 1))
        self.save()

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
