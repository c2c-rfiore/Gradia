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

"""The screenshot preview process.

This runs beside the editor under its own application id so it can use the
XWayland backend, which is the only way to anchor a window to a screen corner
and keep it above other windows on GNOME. Being a unique GApplication also means
later screenshots reach the already-running previews through the ordinary
command-line hand-off, with no IPC of our own.

The previews are mirrored: every monitor carries an identical copy of the stack,
so the previews are always on the screen being worked on because they are on all
of them. Nothing here decides which screen the user is on, because GNOME Wayland
gives a client no faithful way to know: XQueryPointer only moves over our own
XWayland surfaces, Shell.Introspect is locked down, and where the compositor
places new windows trails the user by enough to feel wrong. Being everywhere is
the one placement that cannot be wrong. Deleting, dismissing or opening a
screenshot from any copy applies to every copy.
"""

import os
import sys
from collections import Counter
from typing import Optional

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from gradia.backend.logger import Logger
from gradia.backend.preview_spawner import PREVIEW_ACTION, PREVIEW_ARG
from gradia.backend.x11_placement import X11Placement
from gradia.constants import app_id, rootdir  # pyright: ignore
from gradia.ui.screenshot_preview import ScreenshotPreviewStack

logging = Logger()

# How long an emptied preview process stays up before quitting. While it is up
# the next screenshot is a bus call away instead of a cold Python and GTK start.
KEEP_WARM_MS = 10 * 60 * 1000


class PreviewApplication(Adw.Application):
    """Keeps one identical preview stack on every monitor."""

    def __init__(self, editor_command: Optional[list[str]] = None) -> None:
        super().__init__(
            application_id=f"{app_id}.Preview",
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.editor_command = editor_command or [sys.argv[0]]
        self.placement = X11Placement()
        self.stacks: dict[str, ScreenshotPreviewStack] = {}
        self.screenshots: list[str] = []
        self._monitor_model = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        self._load_style()
        self._install_actions()
        # Windows hold the application; once the last one closes this timer
        # runs, and a screenshot arriving before it fires cancels it again.
        self.set_inactivity_timeout(KEEP_WARM_MS)

    def _install_actions(self) -> None:
        """The editor reaches a running preview through this action, over D-Bus."""
        action = Gio.SimpleAction.new(PREVIEW_ACTION, GLib.VariantType.new("s"))
        action.connect("activate", lambda _action, param: self.show_preview(param.get_string()))
        self.add_action(action)

    def _load_style(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_resource(f"{rootdir}/style.css")
        display = Gdk.Display.get_default()
        if display:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        paths = [
            arg[len(PREVIEW_ARG):]
            for arg in command_line.get_arguments()[1:]
            if arg.startswith(PREVIEW_ARG)
        ]

        if not paths:
            logging.warning("Preview process started with no --preview-file argument.")
            return 0

        for path in paths:
            self.show_preview(path)
        return 0

    """
    Previews
    """

    def show_preview(self, file_path: str) -> None:
        if not os.path.exists(file_path):
            logging.warning(f"Preview requested for a file that is not there: {file_path}")
            return

        self.screenshots.append(file_path)
        self._watch_monitors()
        self._sync_stacks()
        logging.info(
            f"Preview shown for {file_path} on {len(self.stacks)} screen(s) "
            f"({len(self.screenshots)} in the stack)"
        )

    def _watch_monitors(self) -> None:
        """Monitors coming and going re-balance the stacks; connected once."""
        if self._monitor_model is not None:
            return
        display = Gdk.Display.get_default()
        if display is None:
            return
        self._monitor_model = display.get_monitors()
        self._monitor_model.connect("items-changed", lambda *_: self._sync_stacks())

    def _sync_stacks(self) -> None:
        """One stack per attached monitor, each holding every screenshot."""
        display = Gdk.Display.get_default()
        if display is None:
            return

        model = display.get_monitors()
        by_connector: dict[str, Gdk.Monitor] = {}
        for index in range(model.get_n_items()):
            monitor = model.get_item(index)
            by_connector[monitor.get_connector() or f"monitor-{index}"] = monitor

        # A stack whose screen went away closes. A replug hands out a same-named
        # but new monitor object, and the stack holds the dead one, so validity
        # matters as much as the name; the fresh screen gets a new stack below.
        for key, stack in list(self.stacks.items()):
            if key not in by_connector or not stack.monitor.is_valid():
                self.stacks.pop(key, None)
                stack.close()

        if not self.screenshots:
            return

        for key, monitor in by_connector.items():
            stack = self.stacks.get(key)
            if stack is None:
                stack = ScreenshotPreviewStack(
                    monitor=monitor,
                    placement=self.placement,
                    on_edit=self.open_in_editor,
                    on_card_removed=self._remove_everywhere,
                    on_empty=lambda s, key=key: self.stacks.pop(key, None),
                )
                self.add_window(stack)
                self.stacks[key] = stack

            # Top the copy up to the full screenshot list, duplicates included.
            have = Counter(card.file_path for card in stack.cards)
            for path in self.screenshots:
                if have[path]:
                    have[path] -= 1
                else:
                    stack.add_screenshot(path)
            stack.present()

    def _remove_everywhere(self, file_path: str) -> None:
        """A card acted on in one copy acts on all of them."""
        try:
            self.screenshots.remove(file_path)
        except ValueError:
            pass
        for stack in list(self.stacks.values()):
            stack.remove_path(file_path)

    def open_in_editor(self, file_path: str) -> None:
        """Hand the file to the editor, reusing a running one where possible.

        Spawning the launcher means a cold Python and GTK start, which is slow
        enough to notice. If an editor is already on the bus, activating its
        "open" action instead is effectively instant.
        """
        if self._activate_editor_action(file_path):
            return
        self._spawn_editor(file_path)

    def _activate_editor_action(self, file_path: str) -> bool:
        object_path = "/" + app_id.replace(".", "/")
        try:
            # Reuse the connection this application already holds; opening a new
            # one costs a few hundred milliseconds.
            bus = self.get_dbus_connection() or Gio.bus_get_sync(Gio.BusType.SESSION, None)
            bus.call_sync(
                app_id,
                object_path,
                "org.gtk.Actions",
                "Activate",
                GLib.Variant("(sava{sv})", ("open", [GLib.Variant("s", file_path)], {})),
                None,
                Gio.DBusCallFlags.NO_AUTO_START,
                1500,
                None,
            )
            logging.info(f"Handed {file_path} to the running editor.")
            return True
        except GLib.Error as e:
            logging.debug(f"No running editor to hand {file_path} to ({e.message}).")
            return False

    def _spawn_editor(self, file_path: str) -> None:
        try:
            Gio.Subprocess.new(
                [*self.editor_command, file_path],
                Gio.SubprocessFlags.NONE,
            )
            logging.info(f"Starting the editor for {file_path}.")
        except GLib.Error as e:
            logging.warning(f"Could not open {file_path} in the editor.", exception=e)


def main(editor_command: Optional[list[str]] = None) -> int:
    # XWayland is required: see gradia.backend.x11_placement for why.
    os.environ["GDK_BACKEND"] = "x11"
    return PreviewApplication(editor_command=editor_command).run(sys.argv)
