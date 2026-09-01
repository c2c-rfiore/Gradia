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
is a unique GApplication, so the first call starts it and later calls are
forwarded to the running stack by GApplication itself.

Which screen to put it on is worked out here rather than there. Deciding it needs
the Wayland backend (see gradia.backend.monitor_probe), which the preview process
cannot have, but this process does.
"""

import os
import shutil
import sys
from typing import Optional

from gi.repository import Gio, GLib

from gradia.backend.logger import Logger
from gradia.backend.monitor_probe import active_monitor_connector

logging = Logger()

# The flags the two processes speak to each other. The preview side imports
# these, so the spelling lives in exactly one place.
PREVIEW_ARG = "--preview-file="
MONITOR_ARG = "--preview-monitor="


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

    launcher = _launcher_path()
    if not launcher:
        logging.warning("Could not locate the gradia launcher; skipping the preview.")
        return False

    arguments = [launcher, f"{PREVIEW_ARG}{file_path}"]

    # Asked for now, while the screenshot has just been taken and the screen the
    # user is on is still the screen they took it on.
    connector = active_monitor_connector()
    if connector:
        arguments.append(f"{MONITOR_ARG}{connector}")

    try:
        Gio.Subprocess.new(arguments, Gio.SubprocessFlags.NONE)
        logging.info(
            f"Screenshot preview requested for {file_path}"
            + (f" on {connector}" if connector else " (screen undetermined)")
        )
        return True
    except GLib.Error as e:
        logging.warning("Could not start the screenshot preview.", exception=e)
        return False
