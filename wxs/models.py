"""统一的数据模型：媒体条目（远端 URL / 本地缓存文件 / 图片）。"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import urls as urls_mod

_counter = itertools.count(1)

MARK_ON = "✓"      # 标准勾（用户要求：不要用 √）
MARK_OFF = " "


def new_uid(prefix: str = "m") -> str:
    return f"{prefix}{next(_counter):05d}"


@dataclass
class MediaItem:
    """GUI 列表里的“一行”。"""

    uid: str = field(default_factory=lambda: new_uid())
    source: str = ""                 # 来源后端: 内存扫描 / 缓存文件 / 缓存URL / CDP / 手动
    kind: str = "remote"             # remote | local
    media_type: str = "video"        # video | image
    url: str = ""                    # kind=remote 时有值
    path: str = ""                   # kind=local 时有值
    title: str = ""                  # 展示名
    appid: str = ""
    ext: str = ".mp4"
    kind_label: str = "视频"
    size: int | None = None
    mtime: float | None = None
    score: int = 0
    expired: bool = False
    detail: str = ""
    extra: dict = field(default_factory=dict)

    # GUI 状态（不参与持久化）
    selected: bool = True
    status: str = "待下载"
    progress: float = 0.0

    # ---------------------------------------------------------- 构造

    @classmethod
    def from_url_info(cls, info: urls_mod.UrlInfo, source: str, index: int = 1) -> "MediaItem":
        is_image = info.kind == "image"
        fname = urls_mod.guess_filename(info, index)
        return cls(
            source=source,
            kind="remote",
            media_type="image" if is_image else "video",
            url=info.url,
            title=fname,
            ext=info.ext or (".jpg" if is_image else ".mp4"),
            kind_label=info.kind_label,
            score=info.score,
            expired=info.expired,
            detail=" / ".join(info.reasons),
            extra={"host": info.host, "signed": info.signed, "expiry_ts": info.expiry_ts},
        )

    @classmethod
    def from_local(cls, path: Path | str, source: str, appid: str = "") -> "MediaItem":
        p = Path(path)
        try:
            stat = p.stat()
            size, mtime = stat.st_size, stat.st_mtime
        except OSError:
            size, mtime = None, None
        is_image = p.suffix.lower() in urls_mod.PHOTO_EXTS
        return cls(
            source=source,
            kind="local",
            media_type="image" if is_image else "video",
            path=str(p),
            title=p.name,
            appid=appid,
            ext=p.suffix.lower() or (".jpg" if is_image else ".mp4"),
            kind_label="本地图片" if is_image else "本地缓存文件",
            size=size,
            mtime=mtime,
            score=80,
            detail=f"{p.parent}",
        )

    # ---------------------------------------------------------- 展示

    @property
    def is_file(self) -> bool:
        return self.kind == "local"

    @property
    def is_image(self) -> bool:
        return self.media_type == "image" or self.ext.lower() in urls_mod.PHOTO_EXTS

    @property
    def is_remote(self) -> bool:
        return self.kind == "remote"

    @property
    def is_hls(self) -> bool:
        """这条是不是 m3u8（“全部 m3u8”按钮用它筛）。"""
        if self.kind_label == "HLS 播放列表" or self.ext.lower() == ".m3u8":
            return True
        url = (self.url or "").lower()
        return ".m3u8" in url

    @property
    def size_label(self) -> str:
        from .logsetup import human_size

        if self.size:
            return human_size(self.size)
        return "-"

    @property
    def mtime_label(self) -> str:
        if not self.mtime:
            return "-"
        return time.strftime("%m-%d %H:%M:%S", time.localtime(self.mtime))

    def target_name(self) -> str:
        """落地文件名。"""
        if self.is_file:
            return urls_mod.sanitize_filename(self.title)
        base = self.title or ("wximage" if self.is_image else "wxvideo")
        if self.appid:
            base = f"{self.appid}_{base}"
        return urls_mod.sanitize_filename(base)

    def row(self) -> tuple:
        mark = MARK_ON if self.selected else MARK_OFF
        return (
            mark, self.kind_label, self.source, self.title[:70],
            self.size_label, self.mtime_label, self.status,
        )


__all__ = ["MediaItem", "new_uid", "MARK_ON", "MARK_OFF"]
