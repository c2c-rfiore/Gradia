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

"""Which screen the user is working on, asked of the compositor.

Nothing on a GNOME Wayland session will tell a client this outright. The pointer
XWayland reports through XQueryPointer only moves while the pointer is over an
XWayland surface; the moment it is over a native Wayland window the value freezes
at wherever it last crossed out, so it names the wrong screen and never changes.
org.gnome.Shell.Introspect answers "GetWindows is not allowed", and the Shell and
Mutter bus names are not visible from inside the flatpak at all.

What the compositor will do is place a new window on the screen in use. Mapping a
window and listening for the surface's enter-monitor event turns that placement
decision into the answer, in under ~100ms.

Two traps, both measured here rather than guessed: a window with nothing to draw
(opacity 0, or a fully transparent background) never commits a buffer, so the
compositor never maps it onto any output and never says where it is; and asking
gdk_display_get_monitor_at_surface before the enter event has arrived falls back
to a made-up overlap at (0, 0), i.e. whichever monitor owns the origin. So the
probe window is one pixel at 2% opacity — composited, but impossible to see —
and the answer is taken from the enter-monitor event itself.

This has to run on the Wayland backend, so it cannot happen inside the preview
process, which is pinned to XWayland to be able to place itself at all. It runs
in the editor process instead, and the answer is passed to the preview on its
command line.
"""

from typing import Optional

from gi.repository import Gdk, GLib, Gtk

from gradia.backend.logger import Logger

logging = Logger()

# Generous: the probe normally answers in well under 100ms, and this only has to
# stop a missing "map" from hanging the caller.
PROBE_TIMEOUT_MS = 1500


def _is_wayland(display: Gdk.Display) -> bool:
    return type(display).__name__.startswith("GdkWayland")


def active_monitor_connector() -> Optional[str]:
    """The connector of the screen in use, or None if it cannot be determined.

    None is not a failure worth reporting to the user: on an X11 session the
    compositor does not place windows this way, and the caller falls back to the
    pointer, which is reliable there.
    """
    display = Gdk.Display.get_default()
    if display is None or not _is_wayland(display):
        return None

    # One pixel at 2% opacity: enough to be composited, impossible to see. Full
    # transparency would mean no buffer, no mapping, and no answer (see above).
    window = Gtk.Window(decorated=False, resizable=False,
                        default_width=1, default_height=1)
    window.set_opacity(0.02)
    window.set_can_focus(False)
    window.set_focus_on_click(False)

    loop = GLib.MainLoop()
    answer: dict[str, Optional[str]] = {}
    timeout_id: Optional[int] = None

    def finish() -> None:
        nonlocal timeout_id
        if timeout_id is not None:
            GLib.source_remove(timeout_id)
            timeout_id = None
        window.set_visible(False)
        window.destroy()
        loop.quit()

    def on_enter(_surface, monitor: Gdk.Monitor) -> bool:
        # The compositor's own word on where it put the window. Tear down on
        # idle rather than from inside the surface's signal emission.
        answer["connector"] = monitor.get_connector()
        GLib.idle_add(finish)
        return False

    def on_realize(*_args) -> None:
        window.get_surface().connect("enter-monitor", on_enter)

    def give_up() -> bool:
        nonlocal timeout_id
        timeout_id = None
        logging.warning("The active-monitor probe was never composited; falling back.")
        finish()
        return False

    window.connect("realize", on_realize)
    timeout_id = GLib.timeout_add(PROBE_TIMEOUT_MS, give_up)
    window.present()

    # A nested loop keeps this a plain function call for the caller. It is short
    # enough that blocking through it is not felt.
    loop.run()

    connector = answer.get("connector")
    if connector:
        logging.debug(f"The screen in use is {connector}.")
    return connector
