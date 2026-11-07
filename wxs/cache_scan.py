"""本地缓存扫描。

两条独立通道，都不联网、不注入、不需要微信配合：

A. 小程序本地缓存文件（最实用）
   实测路径：%APPDATA%\\Tencent\\xwechat\\radium\\users\\<userhash>\\applet\\local\\<appid>\\temp\\*.mp4
   这些是小程序播放/下载时落盘的真实视频（ffprobe 验证：h264+aac 1280x720 30fps）。
   → 直接复制出来即可，无需下载。

B. Chromium 缓存里的 URL
   ...\\radium\\web\\profiles\\<profile>\\Cache\\Cache_Data\\f_xxxxxx
   Simple-Cache 的 entry 文件里嵌着 URL 文本，可捞回媒体直链。
   坑：同目录 data_N（块文件，上百 MB）通常被微信独占打开，
       Python 的 open() 会拿到 errno 13；必须走 CreateFileW + FILE_SHARE_* 才能读。

还有一条兜底：applet/local/<appid>/temp 里有些文件没有扩展名，用文件头
`ftyp` 判定是不是视频，避免漏掉。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, winapi
from .logsetup import human_size, win32_error_text
from .models import MediaItem

log = logging.getLogger("wxs.cache")

GENERIC_FILE_SHARE = 0x1 | 0x2 | 0x4  # READ|WRITE|DELETE
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000

kernel32 = winapi.kernel32
kernel32.CreateFileW.argtypes = [
    w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE
]
kernel32.CreateFileW.restype = w.HANDLE
kernel32.ReadFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p]
kernel32.ReadFile.restype = w.BOOL
kernel32.GetFileSizeEx.argtypes = [w.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
kernel32.GetFileSizeEx.restype = w.BOOL


def read_file_shared(path: Path | str, max_bytes: int = 64 * 1024 * 1024) -> bytes:
    """以共享方式读取被其它进程占用的文件（微信的 Cache_Data/data_N）。"""
    handle = kernel32.CreateFileW(
        str(path), 0x80000000, GENERIC_FILE_SHARE, None, OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_SEQUENTIAL_SCAN, None,
    )
    if not handle or handle == w.HANDLE(-1).value:
        err = ctypes.get_last_error()
        log.debug("CreateFileW(共享读) 失败 %s %s", path, win32_error_text(err))
        return b""
    try:
        size = ctypes.c_longlong(0)
        if kernel32.GetFileSizeEx(handle, ctypes.byref(size)):
            limit = min(int(size.value), max_bytes)
        else:
            limit = max_bytes
        if limit <= 0:
            return b""
        chunks: list[bytes] = []
        got_total = 0
        read = w.DWORD(0)
        while got_total < limit:
            want = min(4 * 1024 * 1024, limit - got_total)
            buf = ctypes.create_string_buffer(want)
            if not kernel32.ReadFile(handle, buf, want, ctypes.byref(read), None):
                log.debug("ReadFile 失败 %s %s", path, win32_error_text(ctypes.get_last_error()))
                break
            if read.value == 0:
                break
            chunks.append(buf.raw[: read.value])
            got_total += read.value
        return b"".join(chunks)
    finally:
        kernel32.CloseHandle(handle)


# ---------------------------------------------------------------- A. 本地视频文件

def _looks_like_mp4(path: Path) -> bool:
    """无扩展名文件用文件头判断：MP4 的 ftyp 在偏移 4。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
        return len(head) >= 8 and head[4:8] == b"ftyp"
    except OSError:
        return False


def scan_applet_local(
    min_bytes: int = 80 * 1024,
    cancel: threading.Event | None = None,
    progress=None,
    include_images: bool = False,
    min_image_bytes: int = 24 * 1024,
) -> list[MediaItem]:
    """扫描小程序 local 目录，收集可直接复制的媒体文件。

    include_images=True 时顺带收 jpg/jpeg/png（小程序会把图片素材也落到这里），
    用 min_image_bytes 过滤掉图标之类的小图。
    """
    roots = config.applet_local_roots()
    log.info("本地缓存扫描: 发现 %d 个小程序 local 目录（图片=%s）", len(roots),
             "开" if include_images else "关")
    for r in roots:
        log.info("   √ %s", r)
    if not roots:
        log.warning("没有找到小程序缓存目录（微信未登录/未运行过小程序？）")
        return []

    items: list[MediaItem] = []
    scanned_files = 0
    skipped_size = 0
    image_count = 0
    for root in roots:
        base = root.parent.parent if root.name == "local" else root  # applet 目录
        for dirpath, dirnames, filenames in os.walk(root):
            if cancel is not None and cancel.is_set():
                log.info("本地缓存扫描被取消")
                return items
            # 限制深度，避免在超大目录里跑很久
            depth = Path(dirpath).relative_to(root).parts
            if len(depth) > 5:
                dirnames[:] = []
                continue
            for fname in filenames:
                scanned_files += 1
                fp = Path(dirpath) / fname
                suffix = fp.suffix.lower()
                is_image = suffix in config.PHOTO_EXTS
                if is_image:
                    if not include_images:
                        continue
                is_media = suffix in config.MEDIA_EXTS
                if not is_media and not is_image and suffix in ("", ".tmp", ".dat", ".cache"):
                    is_media = _looks_like_mp4(fp)
                if not is_media and not is_image:
                    continue
                try:
                    size = fp.stat().st_size
                except OSError:
                    continue
                limit = min_image_bytes if is_image else min_bytes
                if size < limit:
                    skipped_size += 1
                    continue
                rel = fp.relative_to(root)
                appid = rel.parts[0] if len(rel.parts) > 1 else base.name
                item = MediaItem.from_local(fp, source="缓存文件", appid=appid)
                item.media_type = "image" if is_image else "video"
                if is_image:
                    item.kind_label = "本地图片"
                    image_count += 1
                items.append(item)
                if progress is not None:
                    try:
                        progress(item)
                    except Exception:  # noqa: BLE001
                        pass
            if scanned_files > 200000:
                log.warning("文件数超过 20 万，提前停止扫描 %s", root)
                break

    items.sort(key=lambda i: -(i.mtime or 0))
    total = sum(i.size or 0 for i in items)
    log.info(
        "本地缓存扫描完成: 遍历 %d 个文件, 命中媒体 %d 个（其中图片 %d）, 合计 %s (因体积过小跳过 %d)",
        scanned_files, len(items), image_count, human_size(total), skipped_size,
    )
    groups: dict[str, int] = {}
    for it in items:
        groups[it.appid] = groups.get(it.appid, 0) + 1
    for appid, cnt in sorted(groups.items(), key=lambda kv: -kv[1]):
        newest = max((i.mtime or 0) for i in items if i.appid == appid)
        import time as _t

        log.info("   appid=%s 文件%d个 最近修改=%s", appid, cnt, _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(newest)))
    return items


# ---------------------------------------------------------------- B. Chromium 缓存 URL

def scan_chromium_cache(
    max_file_mb: int = 64,
    max_total_mb: int = 512,
    cancel: threading.Event | None = None,
    include_images: bool = False,
) -> set[str]:
    """从 Chromium 缓存文件里捞媒体 URL。

    性能坑：直接用正则 `findall` 扫几百 MB 缓存，Python 层逐字节匹配会慢到 50 秒以上。
    改成先用 `bytes.find(b"http")`（C 实现，极快）定位候选，再对每个候选附近 800 字节
    做一次锚定正则 —— 提速一个数量级。

    include_images=True 时同时回收 jpg/jpeg/png 地址（“获取所有图片”功能）。
    """
    roots = config.chromium_cache_roots()
    log.info("Chromium 缓存扫描: %d 个 Cache_Data 目录（图片=%s）", len(roots),
             "开" if include_images else "关")
    found: set[str] = set()
    if not roots:
        return found
    markers = MEDIA_EXT_MARKERS + (IMAGE_EXT_MARKERS if include_images else ())
    pattern = _media_url_re(include_images)

    files: list[Path] = []
    for root in roots:
        try:
            for p in root.iterdir():
                if p.is_file():
                    try:
                        if p.stat().st_size > 32 * 1024:
                            files.append(p)
                    except OSError:
                        continue
        except OSError as exc:
            log.debug("列举缓存目录失败 %s: %s", root, exc)
    files.sort(key=lambda p: -p.stat().st_size if p.exists() else 0)
    log.info("   待扫描缓存文件 %d 个, 最大 %s", len(files),
             human_size(files[0].stat().st_size) if files else "-")

    budget = max_total_mb * 1024 * 1024
    used = 0
    locked = 0
    t0 = time.time()
    for fp in files:
        if cancel is not None and cancel.is_set():
            break
        if used >= budget:
            log.info("缓存扫描预算用完（%s）", human_size(used))
            break
        limit = min(max_file_mb * 1024 * 1024, budget - used)
        data = read_file_shared(fp, max_bytes=limit)
        if not data:
            locked += 1
            continue
        used += len(data)
        hits = _find_media_urls_fast(data, markers=markers, pattern=pattern)
        if hits:
            log.info("   %s (%s): %d 条媒体URL", fp.name, human_size(len(data)), len(hits))
        found.update(hits)

    log.info("Chromium 缓存扫描完成: 读取 %s, 无法读取(被独占) %d 个, 命中 URL %d 条, 耗时 %.1fs",
             human_size(used), locked, len(found), time.time() - t0)
    return found


MEDIA_URL_START = re.compile(rb"https?://", re.IGNORECASE)
MEDIA_EXT_MARKERS = (b".mp4", b".m3u8", b".m4s", b".flv", b".mov", b".m4a", b".ts?")
IMAGE_EXT_MARKERS = (b".jpg", b".jpeg", b".png")
MEDIA_HOST_MARKERS = (b"mpvideo", b"snsvideodownload", b"wxsmw", b"finder.video", b"/video/")

_MEDIA_EXT_ALTERNATION = rb"mp4|m3u8|flv|m4s|mov|ts|m4a|mp3"
_IMAGE_EXT_ALTERNATION = rb"jpg|jpeg|png"


def _media_url_re(include_images: bool = False) -> re.Pattern:
    """只匹配单个窗口内的 ASCII URL，锚定在开头。"""
    alts = _MEDIA_EXT_ALTERNATION + (rb"|" + _IMAGE_EXT_ALTERNATION if include_images else b"")
    return re.compile(
        rb"https?://[\x21-\x7e]{8,600}?(?:\.(?:" + alts + rb"))(?:\?[\x21-\x7e]{0,400})?"
    )


WINDOW_URL_RE = _media_url_re(False)


def _find_media_urls_fast(data: bytes, markers: tuple = MEDIA_EXT_MARKERS,
                          pattern: re.Pattern | None = None) -> set[str]:
    """用 find 定位 'http'，再在局部窗口里找媒体 URL。"""
    out: set[str] = set()
    pattern = pattern or WINDOW_URL_RE
    pos = 0
    n = len(data)
    while True:
        i = data.find(b"http", pos)
        if i < 0 or i + 8 >= n:
            break
        window = data[i:i + 800]
        # 先用便宜的字节包含判断过滤掉绝大多数噪音
        low = window.lower()
        if any(m in low for m in markers) or any(m in low for m in MEDIA_HOST_MARKERS):
            m = pattern.match(window)
            if m:
                out.add(m.group(0).decode("latin-1", errors="replace"))
        pos = i + 4
    return out


def applet_recent_activity() -> dict[str, float]:
    """appid -> 最近修改时间，用来在 GUI 里把“当前正在用的小程序”排前面。"""
    out: dict[str, float] = {}
    for root in config.applet_local_roots():
        try:
            for entry in root.iterdir():
                if not entry.is_dir():
                    continue
                newest = entry.stat().st_mtime
                for dp, _dn, fns in os.walk(entry):
                    for f in fns:
                        try:
                            m = os.path.getmtime(os.path.join(dp, f))
                            if m > newest:
                                newest = m
                        except OSError:
                            pass
                out[entry.name] = newest
        except OSError:
            continue
    return out


__all__ = ["scan_applet_local", "scan_chromium_cache", "read_file_shared", "applet_recent_activity"]
