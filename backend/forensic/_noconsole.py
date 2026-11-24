"""On Windows, start every helper program (FFmpeg, ffprobe, the plate reader...) without a console window.

The desktop app runs under pythonw.exe, which has no console. Windows then gives each console program it starts
a console window of its own, so every frame decode flashed a terminal on screen for as long as FFmpeg ran (for
example when moving to another part of a video). CREATE_NO_WINDOW runs the program with no window at all; its
output still reaches Python through the pipes as before. Installed once, when the package is imported, so every
call site (and any library that uses subprocess) is covered. Does nothing on other systems.
"""

from __future__ import annotations

import subprocess
import sys

_FLAG = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def install() -> None:
    if sys.platform != "win32" or not _FLAG or getattr(subprocess.Popen, "_lucida_no_console", False):
        return

    class _Popen(subprocess.Popen):
        _lucida_no_console = True

        def __init__(self, *args, **kwargs):
            # A caller asking for a new console (or a detached process) keeps it; everything else gets no window.
            flags = kwargs.get("creationflags", 0) or 0
            if not flags & (subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS):
                kwargs["creationflags"] = flags | _FLAG
            super().__init__(*args, **kwargs)

    # subprocess.run, check_output and call look Popen up in the module at call time, so they use this too.
    subprocess.Popen = _Popen
