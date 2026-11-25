"""Lucida: audited forensic image and video processing core."""

__version__ = "0.4.1"
APP_NAME = "Lucida"

from ._noconsole import install as _no_console_windows

_no_console_windows()
