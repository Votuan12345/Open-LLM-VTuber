"""Context snapshot and a Windows desktop-metadata sensor.

Only lightweight metadata is read: user input idle time, the foreground
process's executable name, whether that window is fullscreen, and its
rectangle (geometry only, for contextual movement). Window titles are never
read.
"""

import ctypes
import ntpath
import sys
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Optional, Protocol, Tuple

from loguru import logger

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MONITOR_DEFAULTTONEAREST = 2
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

# Mili's own Electron window never counts as the "foreground app" rectangle.
OWN_PROCESS_NAMES = frozenset({"open-llm-vtuber-electron.exe", "electron.exe"})


@dataclass(frozen=True)
class ContextSnapshot:
    """Raw desktop metadata. Unknown fields are `None`, never 0/False."""

    taken_at_wall: datetime
    user_idle_seconds: Optional[float]
    process_name: Optional[str]
    fullscreen: Optional[bool]
    # (left, top, right, bottom) in physical pixels; used only for movement.
    foreground_rect: Optional[Tuple[int, int, int, int]] = None

    @classmethod
    def unknown(cls, now_wall: datetime) -> "ContextSnapshot":
        return cls(
            taken_at_wall=now_wall,
            user_idle_seconds=None,
            process_name=None,
            fullscreen=None,
        )


class ContextSensor(Protocol):
    def sample(self) -> ContextSnapshot: ...


class NullContextSensor:
    """Always reports all-unknown. Used off Windows or if setup fails."""

    def __init__(self, wall_clock=datetime.now):
        self._wall_clock = wall_clock

    def sample(self) -> ContextSnapshot:
        return ContextSnapshot.unknown(self._wall_clock())


def idle_seconds_from_ticks(now_tick: int, last_input_tick: int) -> float:
    """`GetTickCount` is a 32-bit DWORD; mask the difference to handle wrap-around."""

    return ((now_tick - last_input_tick) & 0xFFFFFFFF) / 1000


def classify_process(name: Optional[str], table: Mapping[str, str]) -> Optional[str]:
    """`None` (unknown sensor result) stays `None`; an unrecognized name is `"unknown"`."""

    if name is None:
        return None
    basename = ntpath.basename(name).casefold()
    folded_table = {key.casefold(): value for key, value in table.items()}
    return folded_table.get(basename, "unknown")


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class WindowsContextSensor:
    """Reads idle time, foreground process name and fullscreen state via ctypes only."""

    def __init__(self, wall_clock=datetime.now):
        self._wall_clock = wall_clock
        self._user32: Optional[ctypes.WinDLL] = None
        self._kernel32: Optional[ctypes.WinDLL] = None

    def _get_user32(self) -> ctypes.WinDLL:
        if self._user32 is None:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
            user32.GetLastInputInfo.restype = wintypes.BOOL
            user32.GetForegroundWindow.argtypes = []
            user32.GetForegroundWindow.restype = wintypes.HWND
            user32.GetWindowThreadProcessId.argtypes = [
                wintypes.HWND,
                ctypes.POINTER(wintypes.DWORD),
            ]
            user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            user32.GetDesktopWindow.argtypes = []
            user32.GetDesktopWindow.restype = wintypes.HWND
            user32.GetShellWindow.argtypes = []
            user32.GetShellWindow.restype = wintypes.HWND
            user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
            user32.GetWindowRect.restype = wintypes.BOOL
            user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            user32.MonitorFromWindow.restype = wintypes.HANDLE
            user32.GetMonitorInfoW.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(_MONITORINFO),
            ]
            user32.GetMonitorInfoW.restype = wintypes.BOOL
            user32.IsIconic.argtypes = [wintypes.HWND]
            user32.IsIconic.restype = wintypes.BOOL
            try:
                user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
                user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            except AttributeError:
                pass  # older Windows: rect is read without a DPI switch
            self._user32 = user32
        return self._user32

    def _get_kernel32(self) -> ctypes.WinDLL:
        if self._kernel32 is None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetTickCount.argtypes = []
            kernel32.GetTickCount.restype = wintypes.DWORD
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.QueryFullProcessImageNameW.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD),
            ]
            kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            self._kernel32 = kernel32
        return self._kernel32

    def _read_idle_seconds(self) -> float:
        user32 = self._get_user32()
        kernel32 = self._get_kernel32()
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        now_tick = kernel32.GetTickCount()
        return idle_seconds_from_ticks(now_tick, info.dwTime)

    def _read_process_name(self) -> str:
        user32 = self._get_user32()
        kernel32 = self._get_kernel32()
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            raise OSError("no foreground window")
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
        )
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            buf_len = wintypes.DWORD(260)
            buf = ctypes.create_unicode_buffer(buf_len.value)
            if not kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(buf_len)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            return ntpath.basename(buf.value)
        finally:
            kernel32.CloseHandle(handle)

    def _read_fullscreen(self) -> bool:
        user32 = self._get_user32()
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            raise OSError("no foreground window")
        desktop = user32.GetDesktopWindow()
        shell = user32.GetShellWindow()
        if hwnd == desktop or hwnd == shell:
            return False
        rect = _RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise ctypes.WinError(ctypes.get_last_error())
        monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        mon = info.rcMonitor
        return (
            rect.left <= mon.left
            and rect.top <= mon.top
            and rect.right >= mon.right
            and rect.bottom >= mon.bottom
        )

    def _read_foreground_rect(self) -> Optional[Tuple[int, int, int, int]]:
        """Foreground window rectangle in physical pixels, or `None`.

        The calling thread is switched to per-monitor-v2 DPI awareness for the
        `GetWindowRect` call only, so the result is in physical pixels.
        """

        user32 = self._get_user32()
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        if hwnd in (user32.GetDesktopWindow(), user32.GetShellWindow()):
            return None
        if user32.IsIconic(hwnd):
            return None
        if self._read_process_name().casefold() in OWN_PROCESS_NAMES:
            return None

        set_dpi = getattr(user32, "SetThreadDpiAwarenessContext", None)
        previous = (
            set_dpi(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2) if set_dpi else None
        )
        try:
            rect = _RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return None
        finally:
            if set_dpi and previous:
                set_dpi(previous)

        if rect.right <= rect.left or rect.bottom <= rect.top:
            return None
        return (rect.left, rect.top, rect.right, rect.bottom)

    def sample(self) -> ContextSnapshot:
        now_wall = self._wall_clock()

        try:
            idle_seconds: Optional[float] = self._read_idle_seconds()
        except Exception as exc:
            logger.debug(f"[Context] idle read failed: {exc}")
            idle_seconds = None

        try:
            process_name: Optional[str] = self._read_process_name()
        except Exception as exc:
            logger.debug(f"[Context] process read failed: {exc}")
            process_name = None

        try:
            fullscreen: Optional[bool] = self._read_fullscreen()
        except Exception as exc:
            logger.debug(f"[Context] fullscreen read failed: {exc}")
            fullscreen = None

        if (
            fullscreen
            and process_name is not None
            and process_name.casefold() in OWN_PROCESS_NAMES
        ):
            # Mili's own pet window spans the screen; focusing her is not a
            # fullscreen app.
            fullscreen = False

        try:
            foreground_rect = self._read_foreground_rect()
        except Exception as exc:
            logger.debug(f"[Context] foreground rect read failed: {exc}")
            foreground_rect = None

        return ContextSnapshot(
            taken_at_wall=now_wall,
            user_idle_seconds=idle_seconds,
            process_name=process_name,
            fullscreen=fullscreen,
            foreground_rect=foreground_rect,
        )


def make_default_sensor() -> ContextSensor:
    """Windows sensor when on Windows and construction succeeds; `NullContextSensor` otherwise."""

    if sys.platform == "win32":
        try:
            return WindowsContextSensor()
        except Exception as exc:
            logger.debug(f"[Context] Windows sensor unavailable: {exc}")
    return NullContextSensor()
