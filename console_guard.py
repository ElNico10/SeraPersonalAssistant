"""Keeps a paused console from freezing the bot.

In a Windows console, clicking in the window starts a text selection and the console stops accepting output
until Esc, Enter or Ctrl+C is pressed; any print() then blocks. Writing to the console from a background
thread means only that thread waits, so the bot keeps working and the text appears when the selection clears.
"""
import atexit
import queue
import sys
import threading
import time


class _Background:
    def __init__(self, real):
        self._real = real
        self._queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while True:
            text = self._queue.get()
            try:
                self._real.write(text)
                self._real.flush()
            except Exception:
                pass  # shortcut: a console that fails to write drops that text instead of crashing the bot

    def write(self, text):
        self._queue.put(text)
        return len(text)

    def flush(self):
        pass

    def pending(self):
        return not self._queue.empty()

    def __getattr__(self, name):  # encoding, isatty, fileno and the rest come from the real stream
        return getattr(self._real, name)


def _drain(streams):
    deadline = time.monotonic() + 2  # let a last traceback out, but never hang the exit on a paused console
    while any(s.pending() for s in streams) and time.monotonic() < deadline:
        time.sleep(0.05)


def install():
    wrapped = []
    for name in ('stdout', 'stderr'):
        stream = getattr(sys, name)
        if stream is not None and not isinstance(stream, _Background):
            stream = _Background(stream)
            setattr(sys, name, stream)
        if isinstance(stream, _Background):
            wrapped.append(stream)
    atexit.register(_drain, wrapped)
