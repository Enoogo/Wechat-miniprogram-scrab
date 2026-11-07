"""Win32 / ctypes 底层封装。

只放“薄”封装：结构体、常量、错误处理。业务判断在 windows.py / memory_scan.py。
所有函数都不抛异常（出错返回 None / False），错误信息写入日志并带 Win32 错误码。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import logging
import os
from dataclasses import dataclass, field

from .logsetup import win32_error_text

log = logging.getLogger("wxs.winapi")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
except OSError:  # pragma: no cover
    dwmapi = None
try:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
except OSError:  # pragma: no cover
    advapi32 = None


# ---------------------------------------------------------------- 常量

GW_OWNER = 4
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
SW_RESTORE = 9

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010

MEM_COMMIT = 0x1000
MEM_FREE = 0x10000
MEM_RESERVE = 0x2000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
# 可读保护位：R(0x02) RW(0x04) WC(0x08) ER(0x20) ERW(0x40) ERWC(0x80)
READABLE_PROTECT = (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)

TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY = 0x0008
SE_PRIVILEGE_ENABLED = 0x00000002


# ---------------------------------------------------------------- 结构体

class RECT(ctypes.Structure):
    _fields_ = [("left", w.LONG), ("top", w.LONG), ("right", w.LONG), ("bottom", w.LONG)]

    def size(self) -> tuple[int, int]:
        return (self.right - self.left, self.bottom - self.top)

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    """64 位版本。注意对齐字段，填错会拿到垃圾 RegionSize。"""

    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong),
        ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", w.DWORD),
        ("__alignment1", w.DWORD),
        ("RegionSize", ctypes.c_ulonglong),
        ("State", w.DWORD),
        ("Protect", w.DWORD),
        ("Type", w.DWORD),
        ("__alignment2", w.DWORD),
    ]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", w.DWORD), ("HighPart", w.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", w.DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", w.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]


if advapi32 is not None:
    # 坑：不给 argtypes，ctypes 会把 Python int 按 C int(32 位) 传，
    # GetCurrentProcess() 返回的伪句柄是 0xFFFFFFFFFFFFFFFF，直接 OverflowError。
    # 实测报错："argument 1: OverflowError: int too long to convert"，导致提权静默失败。
    advapi32.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    advapi32.OpenProcessToken.restype = w.BOOL
    advapi32.LookupPrivilegeValueW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.POINTER(LUID)]
    advapi32.LookupPrivilegeValueW.restype = w.BOOL
    advapi32.AdjustTokenPrivileges.argtypes = [
        w.HANDLE, w.BOOL, ctypes.c_void_p, w.DWORD, ctypes.c_void_p, ctypes.c_void_p,
    ]
    advapi32.AdjustTokenPrivileges.restype = w.BOOL


WNDENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

# ---------------------------------------------------------------- 签名

user32.EnumWindows.argtypes = [WNDENUMPROC, w.LPARAM]
user32.EnumWindows.restype = w.BOOL
user32.EnumChildWindows.argtypes = [w.HWND, WNDENUMPROC, w.LPARAM]
user32.GetWindowTextLengthW.argtypes = [w.HWND]
user32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
user32.GetWindowThreadProcessId.restype = w.DWORD
user32.IsWindowVisible.argtypes = [w.HWND]
user32.IsWindow.argtypes = [w.HWND]
user32.IsIconic.argtypes = [w.HWND]
user32.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(RECT)]
user32.GetWindowLongW.argtypes = [w.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = w.LONG
user32.GetAncestor.argtypes = [w.HWND, w.UINT]
user32.GetAncestor.restype = w.HWND
user32.SetForegroundWindow.argtypes = [w.HWND]
user32.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
user32.GetDpiForWindow.argtypes = [w.HWND]

kernel32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
kernel32.OpenProcess.restype = w.HANDLE
kernel32.CloseHandle.argtypes = [w.HANDLE]
kernel32.ReadProcessMemory.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.ReadProcessMemory.restype = w.BOOL
kernel32.VirtualQueryEx.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
kernel32.VirtualQueryEx.restype = ctypes.c_size_t
kernel32.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
kernel32.GetCurrentProcess.argtypes = []
kernel32.GetCurrentProcess.restype = w.HANDLE


# ---------------------------------------------------------------- DPI

def set_dpi_awareness() -> str:
    """必须在创建任何窗口/Tk 之前调用，否则窗口坐标会被系统缩放，截屏错位。"""
    try:
        # -4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "PerMonitorV2"
    except Exception:  # noqa: BLE001
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return "PerMonitor"
    except Exception:  # noqa: BLE001
        pass
    try:
        user32.SetProcessDPIAware()
        return "System"
    except Exception as exc:  # noqa: BLE001
        log.warning("设置 DPI 感知失败: %s", exc)
        return "None"


def window_dpi(hwnd: int) -> int:
    try:
        v = user32.GetDpiForWindow(hwnd)
        return int(v) if v else 96
    except Exception:  # noqa: BLE001
        return 96


# ---------------------------------------------------------------- 窗口读取

def get_title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(hwnd, buf, n + 2)
    return buf.value


def get_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    user32.GetClassNameW(hwnd, buf, 512)
    return buf.value


def get_pid(hwnd: int) -> int:
    pid = w.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def get_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = RECT()
    if user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return r.as_tuple()
    return (0, 0, 0, 0)


def get_dwm_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """DWM 扩展边框 —— 去掉阴影后的真实可见区域，截屏必须用它。"""
    if dwmapi is None:
        return None
    r = RECT()
    try:
        hr = dwmapi.DwmGetWindowAttribute(
            w.HWND(hwnd), w.DWORD(DWMWA_EXTENDED_FRAME_BOUNDS), ctypes.byref(r), ctypes.sizeof(r)
        )
        if hr == 0:
            return r.as_tuple()
        log.debug("DwmGetWindowAttribute 失败 hwnd=%s hr=0x%08x", hex(hwnd), hr)
    except Exception as exc:  # noqa: BLE001
        log.debug("DwmGetWindowAttribute 异常: %s", exc)
    return None


def is_cloaked(hwnd: int) -> bool:
    """UWP/挂起的窗口会被 DWM cloak，此时 IsWindowVisible 仍返回 True。"""
    if dwmapi is None:
        return False
    val = w.DWORD(0)
    try:
        hr = dwmapi.DwmGetWindowAttribute(w.HWND(hwnd), w.DWORD(DWMWA_CLOAKED), ctypes.byref(val), ctypes.sizeof(val))
        return hr == 0 and val.value != 0
    except Exception:  # noqa: BLE001
        return False


def style_flags(hwnd: int) -> tuple[int, int]:
    return (int(user32.GetWindowLongW(w.HWND(hwnd), GWL_STYLE)), int(user32.GetWindowLongW(w.HWND(hwnd), GWL_EXSTYLE)))


def is_visible(hwnd: int) -> bool:
    return bool(user32.IsWindowVisible(w.HWND(hwnd)))


def is_minimized(hwnd: int) -> bool:
    return bool(user32.IsIconic(w.HWND(hwnd)))


def bring_to_front(hwnd: int) -> bool:
    """把窗口提到前台（调试/人工确认时用；本工具主流程已不依赖它）。"""
    try:
        if is_minimized(hwnd):
            log.info("窗口最小化，先还原 hwnd=%s", hex(hwnd))
            user32.ShowWindow(w.HWND(hwnd), SW_RESTORE)
        ok = bool(user32.SetForegroundWindow(w.HWND(hwnd)))
        if not ok:
            log.warning("SetForegroundWindow 失败（系统前台锁），hwnd=%s task=%s", hex(hwnd), win32_error_text(ctypes.get_last_error()))
        return ok
    except Exception as exc:  # noqa: BLE001
        log.warning("前置窗口异常: %s", exc)
        return False


# ---------------------------------------------------------------- 进程

def process_image_path(pid: int) -> str:
    """QueryFullProcessImageNameW：比 psutil 更可靠（不受 cmdline 权限影响）。"""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        log.debug("OpenProcess(QUERY_LIMITED) 失败 pid=%s %s", pid, win32_error_text(ctypes.get_last_error()))
        return ""
    try:
        size = w.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        log.debug("QueryFullProcessImageNameW 失败 pid=%s %s", pid, win32_error_text(ctypes.get_last_error()))
        return ""
    finally:
        kernel32.CloseHandle(h)


def process_cmdline(pid: int) -> list[str]:
    try:
        import psutil

        return psutil.Process(pid).cmdline()
    except Exception:  # noqa: BLE001
        return []


def process_cwd_name(pid: int) -> str:
    try:
        import psutil

        return psutil.Process(pid).name()
    except Exception:  # noqa: BLE001
        return ""


def enable_debug_privilege() -> bool:
    """开启 SeDebugPrivilege：读别的进程内存或高权限进程时可能需要。"""
    if advapi32 is None:
        return False
    try:
        token = w.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(token)):
            log.debug("OpenProcessToken 失败 %s", win32_error_text(ctypes.get_last_error()))
            return False
        try:
            luid = LUID()
            if not advapi32.LookupPrivilegeValueW(None, "SeDebugPrivilege", ctypes.byref(luid)):
                log.debug("LookupPrivilegeValue 失败 %s", win32_error_text(ctypes.get_last_error()))
                return False
            tp = TOKEN_PRIVILEGES()
            tp.PrivilegeCount = 1
            tp.Privileges[0].Luid = luid
            tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
            ok = advapi32.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None)
            err = ctypes.get_last_error()
            if err == 1300:  # ERROR_NOT_ALL_ASSIGNED
                log.info("SeDebugPrivilege 未持有（需要管理员），继续以普通权限扫描")
                return False
            if ok:
                log.info("已启用 SeDebugPrivilege")
                return True
            log.debug("AdjustTokenPrivileges 失败 %s", win32_error_text(err))
            return False
        finally:
            kernel32.CloseHandle(token)
    except Exception as exc:  # noqa: BLE001
        log.debug("启用调试特权异常: %s", exc)
        return False


# ---------------------------------------------------------------- 窗口信息

@dataclass
class WindowInfo:
    hwnd: int
    pid: int
    class_name: str
    title: str
    rect: tuple[int, int, int, int]
    dwm_rect: tuple[int, int, int, int] | None = None
    visible: bool = True
    minimized: bool = False
    cloaked: bool = False
    style: int = 0
    exstyle: int = 0
    exe: str = ""
    cmdline: list[str] = field(default_factory=list)
    dpi: int = 96
    children: list[str] = field(default_factory=list)

    @property
    def size(self) -> tuple[int, int]:
        return (self.rect[2] - self.rect[0], self.rect[3] - self.rect[1])

    @property
    def capture_rect(self) -> tuple[int, int, int, int]:
        return self.dwm_rect or self.rect

    def label(self) -> str:
        return f"{self.title or '(无标题)'} — {self.class_name} [hwnd={hex(self.hwnd)} pid={self.pid}]"

    def as_row(self) -> dict:
        return {
            "hwnd": hex(self.hwnd),
            "pid": self.pid,
            "exe": os.path.basename(self.exe) or "?",
            "class": self.class_name,
            "title": self.title,
            "size": f"{self.size[0]}x{self.size[1]}",
            "rect": f"{self.rect}",
            "dpi": self.dpi,
            "minimized": self.minimized,
            "visible": self.visible,
        }


def _enum_callback(hwnd: int, _lparam, sink: list) -> bool:
    sink.append(hwnd)
    return True


def enum_top_windows(include_invisible: bool = False) -> list[WindowInfo]:
    """枚举所有顶层窗口，返回 WindowInfo（含子窗口类名，便于确认是 Chromium）。"""
    hwnds: list[int] = []
    cb = WNDENUMPROC(lambda h, l: _enum_callback(h, l, hwnds))
    if not user32.EnumWindows(cb, 0):
        log.warning("EnumWindows 返回失败 %s", win32_error_text(ctypes.get_last_error()))
    out: list[WindowInfo] = []
    for h in hwnds:
        if not include_invisible and not is_visible(h):
            continue
        info = read_window(h, with_children=True)
        if info is not None:
            out.append(info)
    log.debug("枚举顶层窗口: 句柄总数=%d 通过可见过滤=%d", len(hwnds), len(out))
    return out


def enum_child_classes(hwnd: int, limit: int = 12) -> list[str]:
    kids: list[int] = []
    cb = WNDENUMPROC(lambda h, l: _enum_callback(h, l, kids))
    user32.EnumChildWindows(w.HWND(hwnd), cb, 0)
    return [get_class(k) for k in kids[:limit]]


def read_window(hwnd: int, with_children: bool = False) -> WindowInfo | None:
    try:
        pid = get_pid(hwnd)
        if pid == 0:
            return None
        style, exstyle = style_flags(hwnd)
        record = WindowInfo(
            hwnd=hwnd,
            pid=pid,
            class_name=get_class(hwnd),
            title=get_title(hwnd),
            rect=get_rect(hwnd),
            dwm_rect=get_dwm_rect(hwnd),
            visible=is_visible(hwnd),
            minimized=is_minimized(hwnd),
            cloaked=is_cloaked(hwnd),
            style=style,
            exstyle=exstyle,
            exe=process_image_path(pid),
            dpi=window_dpi(hwnd),
        )
        if with_children:
            record.children = enum_child_classes(hwnd)
        return record
    except Exception as exc:  # noqa: BLE001
        log.debug("读取窗口信息失败 hwnd=%s: %s", hex(hwnd), exc)
        return None


__all__ = [
    "WindowInfo", "RECT", "MEMORY_BASIC_INFORMATION", "WNDENUMPROC",
    "user32", "kernel32", "dwmapi", "advapi32",
    "set_dpi_awareness", "get_title", "get_class", "get_pid", "get_rect",
    "get_dwm_rect", "is_cloaked", "is_visible", "is_minimized", "bring_to_front",
    "style_flags", "window_dpi", "process_image_path", "process_cmdline",
    "process_cwd_name", "enable_debug_privilege", "enum_top_windows",
    "enum_child_classes", "read_window",
    "PROCESS_QUERY_INFORMATION", "PROCESS_QUERY_LIMITED_INFORMATION", "PROCESS_VM_READ",
    "MEM_COMMIT", "PAGE_GUARD", "PAGE_NOACCESS", "READABLE_PROTECT",
    "WS_VISIBLE", "WS_EX_TOOLWINDOW",
]
