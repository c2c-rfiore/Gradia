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
later screenshots reach the already-running stack through the ordinary
command-line hand-off, with no IPC of our own.

There is one stack, not one per monitor. Each screenshot names the screen it was
taken on, and the stack moves there, cards and all, so the previews are only ever
on the screen being worked on and never left behind on the one just used.

The screen is named on the command line rather than worked out here: deciding it
needs the Wayland backend (see gradia.backend.monitor_probe) and this process is
pinned to XWayland. Without a name — an X11 session, or a probe that did not
answer — the pointer is used instead, which is accurate on X11.
"""

import os
import sys
from typing import Optional

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from gradia.backend.logger import Logger
from gradia.backend.preview_spawner import MONITOR_ARG, PREVIEW_ARG
from gradia.backend.x11_placement import X11Placement
from gradia.constants import app_id, rootdir  # pyright: ignore
from gradia.ui.screenshot_preview import ScreenshotPreviewStack

logging = Logger()

def monitor_at(monitors: list, pointer: Optional[tuple[int, int]]):
    """The monitor holding the pointer, falling back to the first one.

    Only reached when the screen was not named on the command line. The first
    monitor stands in where the pointer is unknown or in the dead space an
    L-shaped layout leaves between screens, because a preview has to appear
    somewhere.

    Kept separate from the display so multi-monitor placement can be tested
    without a second screen attached.
    """
    if not monitors:
        return None

    if pointer is not None:
        x, y = pointer
        for monitor in monitors:
            geometry = monitor.get_geometry()
            if (geometry.x <= x < geometry.x + geometry.width
                    and geometry.y <= y < geometry.y + geometry.height):
                return monitor

    return monitors[0]


class PreviewApplication(Adw.Application):
    """Holds the one preview stack and keeps it on the screen being used."""

    def __init__(self, editor_command: Optional[list[str]] = None) -> None:
        super().__init__(
            application_id=f"{app_id}.Preview",
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.editor_command = editor_command or [sys.argv[0]]
        self.placement = X11Placement()
        self.stack: Optional[ScreenshotPreviewStack] = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        self._load_style()

    def _load_style(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_resource(f"{rootdir}/style.css")
        display = Gdk.Display.get_default()
        if display:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        arguments = command_line.get_arguments()[1:]
        paths = [a[len(PREVIEW_ARG):] for a in arguments if a.startswith(PREVIEW_ARG)]
        named = [a[len(MONITOR_ARG):] for a in arguments if a.startswith(MONITOR_ARG)]

        if not paths:
            logging.warning("Preview process started with no --preview-file argument.")
            return 0

        for path in paths:
            self.show_preview(path, named[-1] if named else None)
        return 0

    """
    Previews
    """

    def show_preview(self, file_path: str, connector: Optional[str] = None) -> None:
        if not os.path.exists(file_path):
            logging.warning(f"Preview requested for a file that is not there: {file_path}")
            return

        monitor = self._monitor_for(connector)
        if monitor is None:
            logging.warning("No monitor available for the screenshot preview.")
            return

        stack = self._stack_on(monitor)
        stack.add_screenshot(file_path)
        stack.present()
        logging.info(
            f"Preview shown for {file_path} on {monitor.get_connector()} "
            f"({len(stack.cards)} in the stack)"
        )

    def _stack_on(self, monitor: Gdk.Monitor) -> ScreenshotPreviewStack:
        """The one stack there is, brought onto ``monitor`` if it is elsewhere."""
        if self.stack is None:
            self.stack = ScreenshotPreviewStack(
                monitor=monitor,
                placement=self.placement,
                on_edit=self.open_in_editor,
                on_empty=self._stack_emptied,
            )
            self.add_window(self.stack)
        else:
            self.stack.set_monitor(monitor)
        return self.stack

    def _stack_emptied(self, _stack: ScreenshotPreviewStack) -> None:
        self.stack = None

    def _monitor_for(self, connector: Optional[str]) -> Optional[Gdk.Monitor]:
        """The screen to stack on: the one named, else wherever the pointer is."""
        display = Gdk.Display.get_default()
        if display is None:
            return None

        monitor_list = display.get_monitors()
        monitors = [monitor_list.get_item(i) for i in range(monitor_list.get_n_items())]
        if not monitors:
            return None

        if connector:
            for monitor in monitors:
                if monitor.get_connector() == connector:
                    return monitor
            logging.warning(f"No monitor named {connector} is attached; using the pointer.")

        return monitor_at(monitors, self.placement.pointer_position())

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
