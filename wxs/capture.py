"""窗口截图（PrintWindow）。

录制功能已经删除（用户有专门的录屏软件，不需要本程序兜底），这里只保留
「抓一张窗口截图」这一项能力，用途是：
* 下载前确认选中的窗口是不是目标小程序（GUI 的“预览窗口截图”）；
* 自检时验证 PrintWindow 能真的抓到 Chromium 的 GPU 合成画面。

为什么用 PrintWindow 而不是 ImageGrab
------------------------------------
PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT=2) 能拿到 DirectComposition 合成的内容，
实测窗口被别的窗口完全遮挡时依然能抓到正确画面；而 ImageGrab 抓的是屏幕区域，
多显示器/高 DPI 缩放下坐标经常对不上。

坑
--
* PrintWindow 返回非 0 不代表成功，必须检查像素（全黑要退回 ImageGrab）。
* 窗口最小化时 PrintWindow 抓不到内容。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import logging
import time
from pathlib import Path

log = logging.getLogger("wxs.snap")

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG),
        ("biPlanes", w.WORD), ("biBitCount", w.WORD), ("biCompression", w.DWORD),
        ("biSizeImage", w.DWORD), ("biXPelsPerMeter", w.LONG), ("biYPelsPerMeter", w.LONG),
        ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD),
    ]


def _even(v: int) -> int:
    return v - (v % 2)


class WindowGrabber:
    """可复用的 PrintWindow 抓帧器（GDI 对象只建一次）。"""

    def __init__(self, hwnd: int, width: int, height: int) -> None:
        self.hwnd = hwnd
        self.width = _even(max(2, width))
        self.height = _even(max(2, height))
        self._hdc_screen = user32.GetDC(None)
        self._hdc_mem = gdi32.CreateCompatibleDC(self._hdc_screen)
        self._bitmap = gdi32.CreateCompatibleBitmap(self._hdc_screen, self.width, self.height)
        gdi32.SelectObject(self._hdc_mem, self._bitmap)
        self._bi = _BITMAPINFOHEADER()
        self._bi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        self._bi.biWidth = self.width
        self._bi.biHeight = -self.height  # top-down
        self._bi.biPlanes = 1
        self._bi.biBitCount = 32
        self._bi.biCompression = 0
        self._buf = ctypes.create_string_buffer(self.width * self.height * 4)
        self.closed = False

    def grab(self) -> bytes | None:
        """返回 BGRA 字节；失败返回 None。"""
        if self.closed:
            return None
        ok = user32.PrintWindow(self.hwnd, self._hdc_mem, 2)  # PW_RENDERFULLCONTENT
        if not ok:
            # 有些窗口 PW_RENDERFULLCONTENT 不支持，退回 0（普通 PrintWindow）
            ok = user32.PrintWindow(self.hwnd, self._hdc_mem, 0)
        got = gdi32.GetDIBits(self._hdc_mem, self._bitmap, 0, self.height,
                              self._buf, ctypes.byref(self._bi), 0)
        if not got:
            return None
        return self._buf.raw

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            gdi32.DeleteObject(self._bitmap)
            gdi32.DeleteDC(self._hdc_mem)
            user32.ReleaseDC(None, self._hdc_screen)
        except Exception:  # noqa: BLE001
            pass


def grab_window_png(window, out_path: Path | str | None = None,
                    max_width: int = 1200) -> str:
    """抓一张窗口静态截图（PNG），不需要 ffmpeg，抗遮挡。

    小程序的 Chromium 窗口内容在 GPU 合成层，PrintWindow + PW_RENDERFULLCONTENT(2)
    能抓到，而 gdigrab title= / ImageGrab 抓屏幕都会出问题。
    """
    from PIL import Image

    rect = window.capture_rect
    width = max(2, rect[2] - rect[0])
    height = max(2, rect[3] - rect[1])
    grabber = WindowGrabber(window.hwnd, width, height)
    try:
        data = grabber.grab()
        if data is None:
            raise RuntimeError("PrintWindow/GetDIBits 失败")
        image = Image.frombuffer("RGB", (grabber.width, grabber.height), data, "raw", "BGRX", 0, 1)
        extrema = image.convert("L").getextrema()
        log.info("PrintWindow hwnd=%s 尺寸=%dx%d 灰度范围=%s", hex(window.hwnd),
                 grabber.width, grabber.height, extrema)
        if extrema == (0, 0):
            log.warning("PrintWindow 得到全黑图像，改用 ImageGrab 抓窗口区域")
            raise RuntimeError("black")
    except Exception as exc:  # noqa: BLE001
        log.debug("PrintWindow 不可用(%s)，退回 ImageGrab", exc)
        try:
            from PIL import ImageGrab

            image = ImageGrab.grab(bbox=(rect[0], rect[1], rect[2], rect[3]), all_screens=True)
        except Exception as exc2:  # noqa: BLE001
            raise RuntimeError(f"截图失败: PrintWindow={exc} ImageGrab={exc2}") from exc2
    finally:
        grabber.close()

    if out_path is None:
        from . import config

        out_path = config.DOWNLOADS_DIR / f"preview_{hex(window.hwnd)}_{time.strftime('%H%M%S')}.png"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if image.width > max_width:
        ratio = max_width / image.width
        image = image.resize((max_width, int(image.height * ratio)), Image.LANCZOS)
    image.save(out_path)
    log.info("窗口截图已保存: %s (%dx%d)", out_path, image.width, image.height)
    return str(out_path)


__all__ = ["grab_window_png", "WindowGrabber"]
