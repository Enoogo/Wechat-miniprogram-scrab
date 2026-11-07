"""日志系统。

设计要点（用户要求“日志完善、多输出底层有用信息”）：
* 三条日志通道：轮转文件（UTF-8，含全部 DEBUG）、控制台、GUI 队列。
* 统一格式含毫秒、线程名、模块名，便于对齐多线程时序。
* 记录环境快照（Python/系统/权限/DPI/ffmpeg 路径/微信版本），出问题先看这一段。
* Win32 错误统一带上错误码 + FormatMessage 文本。
* 安装 sys.excepthook / threading.excepthook，未捕获异常也会进日志文件。
* 每条关键操作用 log_timing 记录耗时，方便定位慢点。
"""
from __future__ import annotations

import ctypes
import datetime as _dt
import logging
import logging.handlers
import os
import platform
import queue
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Iterable

from . import config

LOG_FORMAT = "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(name)-22s | %(threadName)-18s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_LEVEL_NAMES = {"TRACE": 5, "DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


class GuiLogBridge:
    """把 logging 记录搬到 GUI。使用有界队列，界面卡死时丢弃而不是阻塞工作线程。"""

    def __init__(self, maxsize: int = 20000) -> None:
        self.queue: "queue.Queue[tuple[float, str, str, str]]" = queue.Queue(maxsize=maxsize)
        self.dropped = 0

    def emit_record(self, record: logging.LogRecord, text: str) -> None:
        try:
            self.queue.put_nowait((record.created, record.levelname, record.name, text))
        except queue.Full:
            self.dropped += 1

    def drain(self, limit: int = 800) -> list[tuple[float, str, str, str]]:
        out: list[tuple[float, str, str, str]] = []
        for _ in range(limit):
            try:
                out.append(self.queue.get_nowait())
            except queue.Empty:
                break
        return out


GUI_BRIDGE = GuiLogBridge()


class GuiQueueHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            GUI_BRIDGE.emit_record(record, self.format(record))
        except Exception:  # noqa: BLE001 - 日志处理器绝不能抛出
            pass


class _TagFormatter(logging.Formatter):
    """给不同来源加短标签，方便在 GUI 里一眼区分后端。"""

    def format(self, record: logging.LogRecord) -> str:
        return super().format(record)


_initialised = False
_log_file: Path | None = None


def log_file_path() -> Path | None:
    return _log_file


def setup(level: str = "DEBUG", console_level: str = "INFO", gui: bool = True) -> Path:
    """初始化日志。可重复调用（幂等）。返回日志文件路径。"""
    global _initialised, _log_file
    if _initialised:
        set_level(level)
        return _log_file  # type: ignore[return-value]

    config.ensure_dirs()
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    _log_file = config.LOGS_DIR / f"wxs_{ts}_{os.getpid()}.log"

    root = logging.getLogger()
    root.setLevel(5)
    for h in list(root.handlers):
        root.removeHandler(h)

    fh = logging.handlers.RotatingFileHandler(
        _log_file, maxBytes=8 * 1024 * 1024, backupCount=6, encoding="utf-8", delay=False
    )
    fh.setLevel(_LEVEL_NAMES.get(level.upper(), 10))
    fh.setFormatter(_TagFormatter(LOG_FORMAT, DATE_FORMAT))
    root.addHandler(fh)

    ch = logging.StreamHandler(stream=sys.stdout)
    ch.setLevel(_LEVEL_NAMES.get(console_level.upper(), 20))
    ch.setFormatter(_TagFormatter(LOG_FORMAT, DATE_FORMAT))
    root.addHandler(ch)

    if gui:
        gh = GuiQueueHandler()
        gh.setLevel(logging.INFO if level.upper() == "INFO" else 5)
        gh.setFormatter(_TagFormatter("%(asctime)s | %(levelname)-7s | %(name)-14s | %(message)s", DATE_FORMAT))
        root.addHandler(gh)

    # 第三方库降噪（但保留警告）
    for noisy in ("urllib3", "requests", "websocket", "websockets", "psutil"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.addLevelName(5, "TRACE")
    _install_excepthooks()
    _initialised = True

    lg = logging.getLogger("wxs.boot")
    lg.info("=" * 100)
    lg.info("WechatMiniProgramScrab 启动 | pid=%s | python=%s", os.getpid(), sys.version.split()[0])
    lg.info("日志文件: %s", _log_file)
    lg.info("=" * 100)
    dump_environment()
    return _log_file


def set_level(level: str) -> None:
    """调整文件日志级别（GUI 下拉框调用）。"""
    lv = _LEVEL_NAMES.get(level.upper(), 10)
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler):
            h.setLevel(lv)


def _install_excepthooks() -> None:
    def _hook(exc_type, exc, tb):  # noqa: ANN001
        if issubclass(exc_type, KeyboardInterrupt):
            return
        logging.getLogger("wxs.uncaught").critical(
            "未捕获异常: %s\n%s", exc, "".join(traceback.format_exception(exc_type, exc, tb))
        )

    def _thread_hook(args):  # noqa: ANN001
        logging.getLogger("wxs.uncaught").critical(
            "线程 [%s] 未捕获异常: %s\n%s",
            getattr(args.thread, "name", "?"),
            args.exc_value,
            "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
        )

    sys.excepthook = _hook
    threading.excepthook = _thread_hook


# ---------------------------------------------------------------- 辅助工具

def win32_error_text(code: int) -> str:
    """把 GetLastError 格式化成可读文本。"""
    try:
        buf = ctypes.create_unicode_buffer(512)
        n = ctypes.windll.kernel32.FormatMessageW(
            0x00001000, None, code, 0, buf, 512, None
        )
        msg = buf.value.strip() if n else ""
    except Exception:  # noqa: BLE001
        msg = ""
    if not msg:
        msg = {2: "文件未找到", 5: "拒绝访问", 87: "参数错误", 6: "句柄无效", 299: "部分读取",
               1168: "元素未找到"}.get(code, "未知错误")
    return f"[Win32 {code}] {msg}"


def get_last_error(fn_name: str = "") -> str:
    return f"{fn_name}: {win32_error_text(ctypes.get_last_error() if hasattr(ctypes, 'get_last_error') else ctypes.windll.kernel32.GetLastError())}"


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def dump_environment() -> None:
    """把环境信息写进日志 —— 排查问题时第一件事就是看这里。"""
    lg = logging.getLogger("wxs.env")
    try:
        import psutil  # noqa: F401  (只为在环境里体现版本)
        psutil_ver = getattr(psutil, "__version__", "?")
    except Exception:  # noqa: BLE001
        psutil_ver = "未安装"

    try:
        import tkinter

        tk_ver = tkinter.TkVersion
    except Exception as exc:  # noqa: BLE001
        tk_ver = f"不可用({exc})"

    lines = [
        f"操作系统        : {platform.platform()} {platform.release()}",
        f"机器/架构       : {platform.node()} / {platform.machine()}",
        f"Python          : {platform.python_version()} ({sys.executable})",
        f"Python 架构     : {'64位' if sys.maxsize > 2**32 else '32位'}",
        f"进程 PID        : {os.getpid()}",
        f"管理员权限      : {'是' if is_admin() else '否（部分进程内存不可读）'}",
        f"DWM 合成        : {_dwm_enabled()}",
        f"进程 DPI 感知   : {_dpi_awareness()}",
        f"系统 DPI        : {_system_dpi()}",
        f"psutil          : {psutil_ver}",
        f"tkinter         : {tk_ver}",
        f"日志级别        : 文件 DEBUG / 控制台 INFO",
        f"工作目录        : {Path.cwd()}",
        f"程序根目录      : {config.ROOT}",
        f"下载目录        : {config.DOWNLOADS_DIR}",
        f"APPDATA         : {os.environ.get('APPDATA', '')}",
        f"TEMP            : {os.environ.get('TEMP', '')}",
        f"桌面会话        : {os.environ.get('SESSIONNAME', '?')}",
        f"显示器数量      : {_monitor_count()}",
        "微信数据目录    :",
    ]
    roots = config.wechat_data_roots()
    if roots:
        lines += [f"    √ {p}" for p in roots]
    else:
        lines.append("    × 未找到（微信可能没装/没登录过小程序）")
    lines.append("小程序 local 目录:")
    for p in config.applet_local_roots()[:8]:
        lines.append(f"    √ {p}")
    lines.append("Chromium 缓存目录:")
    for p in config.chromium_cache_roots()[:8]:
        lines.append(f"    √ {p}")
    for ln in lines:
        lg.info("%s", ln)


def _dwm_enabled() -> str:
    try:
        enabled = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmIsCompositionEnabled(ctypes.byref(enabled))
        return "开启" if enabled.value else "关闭"
    except Exception:  # noqa: BLE001
        return "未知"


def _dpi_awareness() -> str:
    try:
        # PROCESS_DPI_AWARENESS: 0 不感知 / 1 系统 / 2 每显示器
        val = ctypes.c_int(0)
        hr = ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(val))
        if hr != 0:
            return "查询失败"
        return {0: "不感知", 1: "系统级", 2: "每显示器级"}.get(val.value, str(val.value))
    except Exception:  # noqa: BLE001
        return "未知"


def _system_dpi() -> str:
    try:
        return str(ctypes.windll.user32.GetDpiForSystem())
    except Exception:  # noqa: BLE001
        return "未知"


def _monitor_count() -> str:
    try:
        return str(ctypes.windll.user32.GetSystemMetrics(80))  # SM_CMONITORS
    except Exception:  # noqa: BLE001
        return "未知"


class log_timing:
    """上下文管理器：记录一段操作的耗时，异常时记录并重抛。

        with log_timing(log, "扫描进程 %s", pid):
            ...
    """

    def __init__(self, logger: logging.Logger, msg: str, *args: Any, level: int = logging.DEBUG) -> None:
        self.logger = logger
        self.msg = (msg % args) if args else msg
        self.level = level
        self.t0 = 0.0

    def __enter__(self) -> "log_timing":
        self.t0 = time.perf_counter()
        self.logger.log(self.level, "▶ 开始: %s", self.msg)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        dt = (time.perf_counter() - self.t0) * 1000
        if exc is None:
            self.logger.log(self.level, "✔ 完成: %s (%.1f ms)", self.msg, dt)
        else:
            self.logger.log(
                logging.WARNING, "✘ 失败: %s (%.1f ms) -> %s: %s", self.msg, dt, type(exc).__name__, exc
            )
        return False


def human_size(n: float | int | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.2f} TB"


def human_speed(bps: float | None) -> str:
    if not bps:
        return "-"
    return human_size(bps) + "/s"


def human_duration(sec: float | None) -> str:
    if sec is None or sec < 0:
        return "-"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def hexdump_head(path: str | Path, n: int = 32) -> str:
    """读文件头十六进制 —— 用于判断返回的是 mp4 还是 HTML 错误页。"""
    try:
        with open(path, "rb") as fh:
            data = fh.read(n)
        return " ".join(f"{b:02x}" for b in data) + f"  |{data[:16]!r}|"
    except OSError as exc:
        return f"<读取失败 {exc}>"


def run_in_thread(fn: Callable[..., Any], *args: Any, name: str | None = None, daemon: bool = True, **kwargs: Any) -> threading.Thread:
    t = threading.Thread(target=fn, args=args, kwargs=kwargs, name=name, daemon=daemon)
    t.start()
    return t


def level_color(level: str) -> str:
    return {
        "TRACE": "#7a7a7a",
        "DEBUG": "#8a8a8a",
        "INFO": "#d4d4d4",
        "WARNING": "#e2b93b",
        "ERROR": "#f14c4c",
        "CRITICAL": "#ff2d95",
    }.get(level, "#d4d4d4")


__all__ = [
    "setup", "set_level", "GuiLogBridge", "GUI_BRIDGE", "log_file_path",
    "log_timing", "win32_error_text", "get_last_error", "human_size",
    "human_speed", "human_duration", "hexdump_head", "dump_environment",
    "is_admin", "run_in_thread", "level_color",
]
