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

"""Hands a freshly taken screenshot to the preview process.

The preview lives in its own process because it needs the XWayland backend. It
is a unique GApplication, so the first call starts it and later calls reach the
running stack. Those later calls take the bus rather than the launcher: starting
a Python just to have GApplication forward one argument costs a visible pause,
while activating an action on the process already there is instant.
"""

import os
import shutil
import sys
from typing import Optional

from gi.repository import Gio, GLib

from gradia.backend.logger import Logger
from gradia.constants import app_id  # pyright: ignore

logging = Logger()

# How the two processes speak to each other. The preview side imports these,
# so each spelling lives in exactly one place.
PREVIEW_ARG = "--preview-file="
PREVIEW_ACTION = "preview"
PREVIEW_APP_ID = f"{app_id}.Preview"


def _launcher_path() -> Optional[str]:
    """The gradia launcher, which doubles as the preview entry point."""
    candidate = sys.argv[0] if sys.argv else None
    if candidate and os.path.isabs(candidate) and os.path.exists(candidate):
        return candidate
    return shutil.which("gradia")


def spawn_preview(file_path: str) -> bool:
    """Show a floating preview for file_path. Returns whether it was launched."""
    if not file_path or not os.path.exists(file_path):
        logging.warning(f"Not previewing a missing file: {file_path}")
        return False

    if _hand_to_running_preview(file_path):
        return True

    launcher = _launcher_path()
    if not launcher:
        logging.warning("Could not locate the gradia launcher; skipping the preview.")
        return False

    try:
        Gio.Subprocess.new(
            [launcher, f"{PREVIEW_ARG}{file_path}"],
            Gio.SubprocessFlags.NONE,
        )
        logging.info(f"Screenshot preview requested for {file_path}")
        return True
    except GLib.Error as e:
        logging.warning("Could not start the screenshot preview.", exception=e)
        return False


def _hand_to_running_preview(file_path: str) -> bool:
    """Activate the preview action on a preview process that is already up.

    NO_AUTO_START keeps the bus from trying to launch anything: when nothing
    owns the name the call fails at once and the caller spawns the launcher.
    """
    object_path = "/" + PREVIEW_APP_ID.replace(".", "/")
    try:
        # The session bus connection is shared with GApplication, so this is a
        # lookup, not a new connection.
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync(
            PREVIEW_APP_ID,
            object_path,
            "org.gtk.Actions",
            "Activate",
            GLib.Variant("(sava{sv})", (PREVIEW_ACTION, [GLib.Variant("s", file_path)], {})),
            None,
            Gio.DBusCallFlags.NO_AUTO_START,
            1500,
            None,
        )
        logging.info(f"Handed {file_path} to the running preview.")
        return True
    except GLib.Error as e:
        logging.debug(f"No running preview to hand {file_path} to ({e.message}).")
        return False
