# SPDX-License-Identifier: GPL-3.0-or-later
"""Five-second OK holds for Linux/CoreELEC result pickers.

Kodi's Python Action API has no key-release or hold-duration accessor. Read
only the current OK-key state from Linux input devices while the picker is
open; never grab the devices, consume events, or change global keymaps.
Other platforms keep their normal Kodi context-menu shortcut.
"""

import glob
import os
import threading
import time

import xbmc

try:
    import fcntl
except ImportError:  # Windows / Android builds without this module
    fcntl = None

_OK_KEYS = (28, 96, 304, 352, 353)  # Enter, keypad Enter, gamepad A, OK, Select
_BITMAP_BYTES = 96
_EVIOCGKEY = 0x80004518 | (_BITMAP_BYTES << 16)
_EVIOCGBIT_KEY = 0x80004521 | (_BITMAP_BYTES << 16)
HOLD_SECONDS = 5.0


def _has_key(bitmap, key):
    return bool(bitmap[key // 8] & (1 << (key % 8)))


class PressedOkKeys:
    """Non-exclusive, read-only access to just the supported OK buttons."""

    def __init__(self):
        self._devices = []
        if fcntl is None:
            return
        for path in glob.glob("/dev/input/event*"):
            fd = None
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                bitmap = bytearray(_BITMAP_BYTES)
                fcntl.ioctl(fd, _EVIOCGBIT_KEY, bitmap, True)
                keys = tuple(key for key in _OK_KEYS if _has_key(bitmap, key))
                if keys:
                    self._devices.append((fd, keys))
                    fd = None
            except OSError:
                pass
            finally:
                if fd is not None:
                    os.close(fd)

    @property
    def available(self):
        """Whether at least one accessible device has an OK button."""
        return bool(self._devices)

    def pressed(self):
        """Return identities of held OK buttons, ignoring all other keys."""
        pressed = set()
        for fd, keys in self._devices:
            bitmap = bytearray(_BITMAP_BYTES)
            try:
                fcntl.ioctl(fd, _EVIOCGKEY, bitmap, True)
            except OSError:
                continue
            pressed.update((fd, key) for key in keys if _has_key(bitmap, key))
        return pressed

    def close(self):
        """Release every input descriptor."""
        for fd, _ in self._devices:
            os.close(fd)
        self._devices = []


class OkHold:
    """Keep short selection and a five-second filter bypass mutually exclusive."""

    def __init__(self, on_hold, on_select):
        self._on_hold = on_hold
        self._on_select = on_select
        self._reader = None
        self._thread = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._keys = set()
        self._started = 0.0
        self._fired = False
        self._pending_select = False
        self._suppress_until = 0.0

    def start(self):
        """Watch only while this picker is alive."""
        self._reader = PressedOkKeys()
        if self._reader.available:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    @property
    def available(self):
        """Whether exact hold timing is supported on this device."""
        return self._reader is not None and self._reader.available

    def _advance(self, keys, now):
        """Update one sample under the lock; return a callback to run outside it."""
        if keys:
            if not self._keys.intersection(keys):
                self._started = now
                self._fired = False
                self._pending_select = False
            self._keys = keys
            if not self._fired and now - self._started >= HOLD_SECONDS:
                self._fired = True
                self._pending_select = False
                return self._on_hold
        elif self._keys:
            select = self._pending_select and not self._fired
            if self._fired or select:
                # Discard Kodi's queued click/release after this same gesture.
                self._suppress_until = now + 0.25
            self._keys = set()
            self._pending_select = False
            if select:
                return self._on_select
        return None

    def defer_selection(self):
        """Defer a physical OK press until release or the five-second mark."""
        if not self.available or self._stop.is_set():
            return False
        with self._lock:
            now = time.monotonic()
            callback = self._advance(self._reader.pressed(), now)
            defer = bool(self._keys) or now < self._suppress_until
            if self._keys and not self._fired:
                self._pending_select = True
        if callback and not self._stop.is_set():
            callback()
        return defer or callback is not None

    def _run(self):
        monitor = xbmc.Monitor()
        try:
            while not self._stop.is_set() and not monitor.abortRequested():
                with self._lock:
                    callback = self._advance(self._reader.pressed(), time.monotonic())
                if callback and not self._stop.is_set():
                    callback()
                if monitor.waitForAbort(0.025):
                    break
        finally:
            self._reader.close()

    def close(self):
        """Stop watching; never wait for our own callback thread."""
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        elif self._thread is None and self._reader is not None:
            self._reader.close()
