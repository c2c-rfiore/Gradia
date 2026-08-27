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

"""Screen-corner placement for the screenshot previews.

Wayland has no protocol for a client to place its own window: xdg-shell omits it
on purpose, and GNOME does not implement wlr-layer-shell, which is what other
compositors offer for screen-anchored surfaces. The previews therefore run on
XWayland, where XMoveWindow and the EWMH window states still work, and this
module is the small ctypes shim that reaches libX11 to do it.
"""

import ctypes
import ctypes.util
from typing import Optional

from gradia.backend.logger import Logger

logging = Logger()

# X11 constants
_CLIENT_MESSAGE = 33
_SUBSTRUCTURE_NOTIFY = 1 << 19
_SUBSTRUCTURE_REDIRECT = 1 << 20
_NET_WM_STATE_ADD = 1
_XA_ATOM = 4


class _Data(ctypes.Union):
    _fields_ = [
        ("b", ctypes.c_char * 20),
        ("s", ctypes.c_short * 10),
        ("l", ctypes.c_long * 5),
    ]


class _ClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", _Data),
    ]


class _XEvent(ctypes.Union):
    _fields_ = [
        ("type", ctypes.c_int),
        ("xclient", _ClientMessageEvent),
        ("pad", ctypes.c_long * 24),
    ]


class X11Placement:
    """Moves windows and sets EWMH states through libX11. Inert when X11 is absent."""

    def __init__(self) -> None:
        self._x11 = None
        self._display = None
        self._atoms: dict[bytes, int] = {}

        library = ctypes.util.find_library("X11")
        if not library:
            logging.warning("libX11 not found; screenshot previews cannot be placed.")
            return

        try:
            x11 = ctypes.CDLL(library)
            x11.XOpenDisplay.restype = ctypes.c_void_p
            x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            x11.XInternAtom.restype = ctypes.c_ulong
            x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
            x11.XDefaultRootWindow.restype = ctypes.c_ulong
            x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]

            display = x11.XOpenDisplay(None)
            if not display:
                logging.warning("No X display available; screenshot previews cannot be placed.")
                return

            self._x11 = x11
            self._display = ctypes.c_void_p(display)
            self._root = x11.XDefaultRootWindow(self._display)
        except Exception as e:
            logging.warning("Could not set up X11 placement.", exception=e)

    @property
    def available(self) -> bool:
        return self._x11 is not None and self._display is not None

    def _atom(self, name: bytes) -> int:
        if name not in self._atoms:
            self._atoms[name] = self._x11.XInternAtom(self._display, name, False)
        return self._atoms[name]

    def move(self, xid: int, x: int, y: int) -> None:
        if not self.available:
            return
        self._x11.XMoveWindow(self._display, ctypes.c_ulong(xid), ctypes.c_int(x), ctypes.c_int(y))
        self._x11.XFlush(self._display)

    def keep_above(self, xid: int) -> None:
        """Float over other windows, and stay out of the task switcher."""
        for state in (b"_NET_WM_STATE_ABOVE", b"_NET_WM_STATE_SKIP_TASKBAR", b"_NET_WM_STATE_SKIP_PAGER"):
            self._add_state(xid, state)

    def _add_state(self, xid: int, state: bytes) -> None:
        if not self.available:
            return

        event = _XEvent()
        event.xclient.type = _CLIENT_MESSAGE
        event.xclient.window = xid
        event.xclient.message_type = self._atom(b"_NET_WM_STATE")
        event.xclient.format = 32
        event.xclient.data.l[0] = _NET_WM_STATE_ADD
        event.xclient.data.l[1] = self._atom(state)
        event.xclient.data.l[2] = 0
        event.xclient.data.l[3] = 1  # normal application source

        self._x11.XSendEvent(
            self._display,
            ctypes.c_ulong(self._root),
            0,
            ctypes.c_long(_SUBSTRUCTURE_REDIRECT | _SUBSTRUCTURE_NOTIFY),
            ctypes.byref(event),
        )
        self._x11.XFlush(self._display)

    def has_state(self, xid: int, state: bytes) -> bool:
        """Read _NET_WM_STATE back; used by the tests to prove the state stuck."""
        if not self.available:
            return False

        self._x11.XGetWindowProperty.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long, ctypes.c_long,
            ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
        ]
        actual_type = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        count = ctypes.c_ulong()
        remaining = ctypes.c_ulong()
        data = ctypes.POINTER(ctypes.c_ulong)()

        self._x11.XGetWindowProperty(
            self._display, ctypes.c_ulong(xid), self._atom(b"_NET_WM_STATE"),
            0, 32, False, _XA_ATOM,
            ctypes.byref(actual_type), ctypes.byref(actual_format),
            ctypes.byref(count), ctypes.byref(remaining), ctypes.byref(data),
        )
        if not count.value:
            return False
        return self._atom(state) in {data[i] for i in range(count.value)}

    def absolute_position(self, xid: int) -> Optional[tuple[int, int]]:
        """Where the window actually ended up, for verification."""
        if not self.available:
            return None
        child = ctypes.c_ulong()
        x, y = ctypes.c_int(), ctypes.c_int()
        self._x11.XTranslateCoordinates(
            self._display, ctypes.c_ulong(xid), ctypes.c_ulong(self._root),
            0, 0, ctypes.byref(x), ctypes.byref(y), ctypes.byref(child),
        )
        return x.value, y.value

    def pointer_position(self) -> Optional[tuple[int, int]]:
        """Root-relative pointer position, used to pick the monitor to stack on."""
        if not self.available:
            return None

        root_return, child_return = ctypes.c_ulong(), ctypes.c_ulong()
        root_x, root_y = ctypes.c_int(), ctypes.c_int()
        win_x, win_y = ctypes.c_int(), ctypes.c_int()
        mask = ctypes.c_uint()

        found = self._x11.XQueryPointer(
            self._display, ctypes.c_ulong(self._root),
            ctypes.byref(root_return), ctypes.byref(child_return),
            ctypes.byref(root_x), ctypes.byref(root_y),
            ctypes.byref(win_x), ctypes.byref(win_y), ctypes.byref(mask),
        )
        if not found:
            return None
        return root_x.value, root_y.value
