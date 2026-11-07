"""外部工具定位与媒体信息探测（ffmpeg / ffprobe）。

注意：窗口录制功能已经删除，所以原来那套 dshow 回环音频设备枚举也一并删掉了。
现在 ffmpeg 只用于两件事：
1. HLS/DASH 清单的合并转封装（见 hls.py / downloader.py）；
2. 下载后的“完整性体检”（ffprobe 时长 + 开头若干秒解码报错数）。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("wxs.tools")

FFMPEG_NAMES = ("ffmpeg.exe", "ffmpeg")
FFPROBE_NAMES = ("ffprobe.exe", "ffprobe")


def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        dirs.append(Path(local) / "Microsoft" / "WinGet" / "Links")
    dirs += [
        Path(r"C:\ffmpeg\bin"),
        Path(r"C:\ProgramData\chocolatey\bin"),
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "ffmpeg" / "bin",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "ffmpeg" / "bin",
    ]
    try:
        from . import config

        dirs += [config.ROOT / "tools", config.ROOT / "tools" / "ffmpeg" / "bin"]
    except Exception:  # noqa: BLE001
        pass
    return dirs


def find_tool(names: tuple[str, ...], explicit: str = "", env_var: str = "") -> str:
    if explicit and Path(explicit).is_file():
        return explicit
    if env_var and os.environ.get(env_var):
        p = os.environ[env_var]
        if Path(p).is_file():
            return p
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for d in _candidate_dirs():
        for name in names:
            p = d / name
            try:
                if p.is_file():
                    return str(p)
            except OSError:
                continue
    return ""


def find_ffmpeg(settings=None) -> str:
    explicit = getattr(settings, "ffmpeg_path", "") if settings else ""
    path = find_tool(FFMPEG_NAMES, explicit, "FFMPEG_PATH")
    if path:
        log.info("ffmpeg: %s", path)
    else:
        log.warning("未找到 ffmpeg —— HLS 合并与下载后体检不可用（下载直链 mp4 不受影响）")
    return path


def find_ffprobe(settings=None) -> str:
    explicit = getattr(settings, "ffprobe_path", "") if settings else ""
    path = find_tool(FFPROBE_NAMES, explicit, "FFPROBE_PATH")
    if path:
        log.debug("ffprobe: %s", path)
    else:
        log.info("未找到 ffprobe —— 下载后不做媒体信息探测（不影响下载）")
    return path


def version_of(tool: str) -> str:
    if not tool:
        return ""
    # 坑：不要给 subprocess.run 传 errors= 又不传 text=True —— 那会强制文本模式，
    # 返回 str；这里显式 text=True 保持一致。
    try:
        r = subprocess.run([tool, "-version"], capture_output=True, text=True,
                           timeout=15, errors="replace")
        return (r.stdout or r.stderr).splitlines()[0][:160] if (r.stdout or r.stderr) else ""
    except Exception as exc:  # noqa: BLE001
        return f"<版本查询失败 {exc}>"


def ffprobe_summary(ffprobe: str, path: Path | str, timeout: float = 40.0) -> str:
    """返回“时长/分辨率/编码”一行摘要，失败返回空串。"""
    if not ffprobe:
        return ""
    cmd = [
        ffprobe, "-v", "error", "-show_entries",
        "format=duration,size,format_name,bit_rate:stream=index,codec_type,codec_name,width,height,avg_frame_rate,bit_rate",
        "-of", "default=noprint_wrappers=1", str(path),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
    except Exception as exc:  # noqa: BLE001
        log.debug("ffprobe 失败: %s", exc)
        return ""
    if r.returncode != 0:
        log.debug("ffprobe 非零退出 %s: %s", r.returncode, (r.stderr or "")[:200])
        return ""

    fields: dict[str, str] = {}
    streams: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for line in (r.stdout or "").splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k == "index":
            if cur:
                streams.append(cur)
            cur = {"index": v}
        elif cur and k in ("codec_type", "codec_name", "width", "height", "avg_frame_rate", "bit_rate"):
            cur[k] = v
        else:
            fields[k] = v
    if cur:
        streams.append(cur)

    parts: list[str] = []
    dur = fields.get("duration", "")
    if dur:
        try:
            parts.append(f"时长={float(dur):.1f}s")
        except ValueError:
            parts.append(f"时长={dur}")
    for s in streams:
        if s.get("codec_type") == "video":
            res = f"{s.get('width')}x{s.get('height')}" if s.get("width") else "?"
            parts.append(f"视频={s.get('codec_name')}/{res}/{s.get('avg_frame_rate', '?')}fps")
        elif s.get("codec_type") == "audio":
            parts.append(f"音频={s.get('codec_name')}")
    if fields.get("format_name"):
        parts.append(f"容器={fields['format_name']}")
    return " ".join(parts)


def ffprobe_duration(ffprobe: str, path: Path | str, timeout: float = 40.0) -> float | None:
    """只要时长（秒）。拿不到返回 None。"""
    if not ffprobe:
        return None
    cmd = [ffprobe, "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
        if r.returncode != 0:
            return None
        raw = (r.stdout or "").strip().splitlines()
        return float(raw[-1]) if raw else None
    except Exception:  # noqa: BLE001
        return None


def ffprobe_streams(ffprobe: str, path: Path | str, timeout: float = 40.0) -> list[dict]:
    """列出流（codec_type/duration/bit_rate），用于判断“有没有音轨”。"""
    if not ffprobe:
        return []
    cmd = [ffprobe, "-v", "error", "-show_entries",
           "stream=index,codec_type,codec_name,duration,bit_rate",
           "-of", "json", str(path)]
    try:
        import json

        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
        if r.returncode != 0:
            return []
        return list((json.loads(r.stdout or "{}") or {}).get("streams") or [])
    except Exception:  # noqa: BLE001
        return []


def probe_decode_errors(ffmpeg: str, path: Path | str, seconds: float = 15.0,
                        timeout: float = 90.0) -> tuple[int, str]:
    """解码文件开头 seconds 秒，统计 ffmpeg 的报错行数。

    这是“下下来的视频前半段没声音/画面不动”的直接体检：真正完整的文件，
    解码开头不会有 error 行；有缺失数据的文件会刷一堆
    "Invalid NAL unit size" / "no frame" / "Error while decoding stream"。

    返回 (错误行数, 最后几行摘要)。只看前 15 秒 —— 用户遇到问题的正好是前半段，
    而且这样对大文件也只要一两秒。
    """
    if not ffmpeg:
        return 0, ""
    cmd = [ffmpeg, "-hide_banner", "-v", "error", "-t", f"{max(1.0, seconds):.0f}",
           "-i", str(path), "-map", "0", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
    except Exception as exc:  # noqa: BLE001
        log.debug("解码体检跳过: %s", exc)
        return 0, ""
    lines = [ln.strip() for ln in (r.stderr or "").splitlines() if ln.strip()]
    if not lines:
        return 0, ""
    return len(lines), " | ".join(lines[-3:])[:400]


def check_environment(settings) -> dict:
    """给 GUI 的“环境自检”用：把关键工具/路径一次性检查并写日志。"""
    from . import config

    info = {
        "ffmpeg": find_ffmpeg(settings),
        "ffprobe": find_ffprobe(settings),
        "applet_roots": [str(p) for p in config.applet_local_roots()],
        "cache_roots": [str(p) for p in config.chromium_cache_roots()],
        "wechat_roots": [str(p) for p in config.wechat_data_roots()],
    }
    info["ffmpeg_version"] = version_of(info["ffmpeg"])
    log.info("环境自检: ffmpeg=%s", info["ffmpeg"] or "缺失")
    log.info("          %s", info["ffmpeg_version"])
    log.info("          小程序缓存目录 %d 个", len(info["applet_roots"]))
    log.info("          Chromium 缓存目录 %d 个", len(info["cache_roots"]))
    return info


__all__ = [
    "find_ffmpeg", "find_ffprobe", "version_of", "ffprobe_summary", "ffprobe_duration",
    "ffprobe_streams", "probe_decode_errors", "find_tool", "check_environment",
]
