"""HLS(m3u8) 解析、主清单展开与「分片级」下载。

为什么要自己下分片，而不是直接把 m3u8 丢给 ffmpeg
--------------------------------------------------
实测过“下下来的视频时长是对的，但前半段只有画面没有声音、画面也不动，
播放器还会自动跳到后半段”。这类文件的 moov（时长/索引）是完整的，
缺的是 mdat 里的一部分数据 —— 典型成因有三个：

1. 直接把 m3u8 交给 ffmpeg：某个分片请求超时/失败时，ffmpeg 可能**跳过该分片
   继续往下写**，产出“时长对、前面空”的文件（音视频轨从缺失点往后才对齐）；
2. 复制还在写入的本地缓存文件（见 downloader 的“等大小稳定”逻辑）；
3. 断点续传时 Content-Range 起点对不上，导致文件中间出现空洞。

本模块的做法
------------
* 先拿清单，**逐片下载**：每片校验 Content-Length，失败按 `http_retries` 重试；
* 全部到位后才生成一份**本地清单**（所有 URI 都指向本地文件），再让 ffmpeg
  只做“本地拼接 + 转封装”，网络因素被彻底排除；
* 支持 `EXT-X-KEY:METHOD=AES-128`（key 也下到本地，交给 ffmpeg 解密）、
  `EXT-X-MAP`（fMP4 的 init 段）、`EXT-X-BYTERANGE`、`EXT-X-DISCONTINUITY`；
* 下完用 ffprobe 对比“清单声明总时长”和“成品时长”，差异过大就在日志里明确警告。

公开接口
--------
fetch_playlist()        取清单并解析
list_variants()         主清单 -> 所有清晰度（“自动获取所有 m3u8 视频”靠它）
expand_hls_items()      把列表里的主清单条目展开成多条清晰度条目
download_media_playlist()  分片级下载 + 本地拼接
"""
from __future__ import annotations

import base64
import logging
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from . import models
from .logsetup import human_size

log = logging.getLogger("wxs.hls")

ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


# ---------------------------------------------------------------- 数据结构

@dataclass
class Variant:
    url: str
    bandwidth: int = 0
    average_bandwidth: int = 0
    resolution: str = ""
    codecs: str = ""
    name: str = ""

    @property
    def label(self) -> str:
        parts = []
        if self.name:
            parts.append(self.name)
        if self.resolution:
            parts.append(self.resolution.replace("x", "x"))
        if self.bandwidth:
            parts.append(f"{self.bandwidth // 1000}kbps")
        return "_".join(parts) or "variant"


@dataclass
class KeyInfo:
    method: str = ""
    uri: str = ""
    iv: str = ""

    @property
    def is_encrypted(self) -> bool:
        return self.method.upper() in ("AES-128", "SAMPLE-AES")

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.method, self.uri, self.iv)


@dataclass
class Segment:
    index: int
    url: str
    duration: float
    byte_range: tuple[int, int] | None = None
    key: KeyInfo | None = None
    init_uri: str = ""
    discontinuity: bool = False


@dataclass
class Playlist:
    url: str
    is_master: bool = False
    variants: list[Variant] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    total_duration: float = 0.0
    target_duration: int = 0
    media_sequence: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and (self.is_master or bool(self.segments))


# ---------------------------------------------------------------- 解析

def _attrs(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, val in ATTR_RE.findall(text or ""):
        out[key.upper()] = val.strip('"')
    return out


def _resolve(base: str, uri: str) -> str:
    uri = (uri or "").strip()
    if not uri:
        return ""
    return urllib.parse.urljoin(base, uri)


def parse_playlist(text: str, base_url: str = "") -> Playlist:
    """解析 m3u8 文本（主清单或媒体清单）。"""
    pl = Playlist(url=base_url)
    if not text or "#EXTM3U" not in text[:2048]:
        pl.error = "内容不是 m3u8（没有 #EXTM3U）"
        return pl

    lines = [ln.strip() for ln in text.splitlines()]
    duration = 0.0
    pending_dur = 0.0
    pending_range: tuple[int, int] | None = None
    pending_disc = False
    cur_key: KeyInfo | None = None
    cur_map = ""
    idx = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                pl.media_sequence = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
            continue
        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                pl.target_duration = int(float(line.split(":", 1)[1].strip()))
            except ValueError:
                pass
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = _attrs(line.split(":", 1)[1])
            uri = ""
            # 下一行（跳过注释）是变体地址
            j = i
            while j < len(lines):
                nxt = lines[j]
                j += 1
                if nxt and not nxt.startswith("#"):
                    uri = nxt
                    break
            i = j
            if uri:
                pl.variants.append(Variant(
                    url=_resolve(base_url, uri),
                    bandwidth=int(attrs.get("BANDWIDTH", "0") or 0),
                    average_bandwidth=int(attrs.get("AVERAGE-BANDWIDTH", "0") or 0),
                    resolution=attrs.get("RESOLUTION", ""),
                    codecs=attrs.get("CODECS", ""),
                    name=attrs.get("NAME", ""),
                ))
            continue
        if line.startswith("#EXTINF:"):
            raw = line.split(":", 1)[1]
            num = raw.split(",", 1)[0].strip()
            try:
                pending_dur = float(num)
            except ValueError:
                pending_dur = 0.0
            continue
        if line.startswith("#EXT-X-BYTERANGE:"):
            raw = line.split(":", 1)[1].strip()
            try:
                if "@" in raw:
                    length, offset = raw.split("@", 1)
                    pending_range = (int(offset), int(length))
                else:
                    pending_range = (0, int(raw))   # 偏移依赖上一片，极罕见
            except ValueError:
                pending_range = None
            continue
        if line.startswith("#EXT-X-KEY:"):
            attrs = _attrs(line.split(":", 1)[1])
            method = attrs.get("METHOD", "").upper()
            if method in ("NONE", ""):
                cur_key = None
            else:
                cur_key = KeyInfo(
                    method=method,
                    uri=_resolve(base_url, attrs.get("URI", "")),
                    iv=attrs.get("IV", ""),
                )
            continue
        if line.startswith("#EXT-X-MAP:"):
            attrs = _attrs(line.split(":", 1)[1])
            cur_map = _resolve(base_url, attrs.get("URI", ""))
            continue
        if line.startswith("#EXT-X-DISCONTINUITY"):
            pending_disc = True
            continue
        if line.startswith("#"):
            continue
        # 普通行 = 分片地址
        url = _resolve(base_url, line)
        if not url:
            continue
        seg = Segment(
            index=idx,
            url=url,
            duration=pending_dur,
            byte_range=pending_range,
            key=cur_key,          # EXT-X-KEY 是“持续生效”到下一个 KEY 标签
            init_uri=cur_map,     # 同理 EXT-X-MAP
            discontinuity=pending_disc,
        )
        duration += pending_dur
        idx += 1
        pl.segments.append(seg)
        pending_dur = 0.0
        pending_range = None
        pending_disc = False

    pl.is_master = bool(pl.variants) and not pl.segments
    pl.total_duration = duration
    if not pl.is_master and not pl.segments:
        pl.error = "清单里没有分片，且不是主清单"
    return pl


# ---------------------------------------------------------------- 网络

def _headers(settings, extra: dict | None = None) -> dict[str, str]:
    h = {
        "User-Agent": getattr(settings, "user_agent", "Mozilla/5.0"),
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": getattr(settings, "referer", "https://servicewechat.com/"),
    }
    if extra:
        h.update(extra)
    return h


def fetch_text(url: str, settings, cancel: threading.Event | None = None,
               timeout: float | None = None,
               retries: int | None = None) -> tuple[str, str]:
    """取清单文本。返回 (text, error)。

    `retries` 传 1 表示“只试一次”：探测阶段要批量探十几个清单，
    每个都按 http_retries 重试（还带 2/4 秒退避）会把探测拖到几十秒 ——
    实测 7 个 DRM 链接让一次探测从 6 秒变成 33 秒。
    """
    import requests

    attempts = max(1, int(retries if retries is not None
                          else getattr(settings, "http_retries", 3)))
    to = float(timeout or getattr(settings, "http_timeout", 30.0))
    last = ""
    for attempt in range(1, attempts + 1):
        if cancel is not None and cancel.is_set():
            return "", "已取消"
        try:
            resp = requests.get(url, headers=_headers(settings), timeout=to, allow_redirects=True)
            if resp.status_code >= 400:
                last = f"HTTP {resp.status_code}"
                log.debug("取 m3u8 失败 %s（%s），第 %d/%d 次", url[:160], last, attempt, attempts)
            else:
                text = resp.text or ""
                if "#EXTM3U" in text[:2048]:
                    return text, ""
                last = f"返回内容不是 m3u8（{resp.headers.get('Content-Type', '?')}）"
                log.debug("%s 第 %d/%d 次：%s", url[:160], attempt, attempts, last)
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            log.debug("取 m3u8 异常 %s（%s），第 %d/%d 次", url[:160], last, attempt, attempts)
        if attempt < attempts:
            time.sleep(min(2 ** attempt, 6))
    return "", last or "取清单失败"


def fetch_playlist(url: str, settings, cancel: threading.Event | None = None,
                   timeout: float | None = None,
                   retries: int | None = None) -> Playlist:
    text, err = fetch_text(url, settings, cancel, timeout=timeout, retries=retries)
    if not text:
        return Playlist(url=url, error=err)
    return parse_playlist(text, url)


def list_variants(url: str, settings, cancel: threading.Event | None = None,
                  timeout: float | None = None,
                  retries: int | None = None) -> tuple[list[Variant], str]:
    """主清单 -> 所有清晰度；媒体清单 -> ([], '')（它本身就是具体流）。"""
    pl = fetch_playlist(url, settings, cancel, timeout=timeout, retries=retries)
    if not pl.ok:
        return [], pl.error
    if pl.is_master:
        log.info("m3u8 主清单: %s -> %d 个清晰度", url[:160], len(pl.variants))
        for v in pl.variants:
            log.info("   清晰度 %s  %s  %dkbps", v.resolution or "?", v.codecs or "?", v.bandwidth // 1000)
        return pl.variants, ""
    return [], ""


# ---------------------------------------------------------------- 展开条目

def expand_hls_items(items: list[models.MediaItem], settings,
                     cancel: threading.Event | None = None,
                     limit: int = 12,
                     timeout: float = 6.0) -> tuple[list[models.MediaItem], list[models.MediaItem], list[str]]:
    """把列表里的 m3u8 主清单展开成“每个清晰度一条”。

    返回 (新条目, 被替换掉的原始条目, notes)。
    - 只有确实是主清单的才展开；普通媒体清单原样保留。
    - 展开失败的（网络/过期）保留原条目，不丢东西。
    - **并行 + 只试一次 + 短超时**：探测阶段要一次问十几个清单，
      串行重试会把探测时间翻好几倍（实测 33s vs 6s）。
    """
    new_items: list[models.MediaItem] = []
    replaced: list[models.MediaItem] = []
    notes: list[str] = []
    targets = [it for it in items if it.is_hls and it.is_remote]
    if not targets:
        return new_items, replaced, notes
    if len(targets) > limit:
        notes.append(f"m3u8 主清单 {len(targets)} 个，只自动展开前 {limit} 个（其余仍可单独下载）")
        targets = targets[:limit]

    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: dict[str, tuple[list[Variant], str]] = {}
    with ThreadPoolExecutor(max_workers=min(4, len(targets)),
                            thread_name_prefix="hls-exp") as pool:
        futures = {pool.submit(list_variants, it.url, settings, cancel, timeout, 1): it
                   for it in targets}
        for fut in as_completed(futures):
            it = futures[fut]
            try:
                results[it.uid] = fut.result()
            except Exception as exc:  # noqa: BLE001
                log.warning("展开 m3u8 异常 %s: %s", it.url[:120], exc)
                results[it.uid] = ([], str(exc))

    for it in targets:
        if cancel is not None and cancel.is_set():
            notes.append("展开 m3u8 清晰度被取消")
            break
        variants, err = results.get(it.uid, ([], "未探测"))
        if not variants:
            log.info("不是主清单（%s），按单条处理: %s", err or "媒体清单", it.url[:120])
            continue
        stem = Path(it.title).stem or "hls"
        made = 0
        for v in variants:
            if len(variants) == 1:
                title = f"{stem}.m3u8"
            else:
                title = f"{stem}_{v.label}.m3u8"
            item = models.MediaItem(
                source=f"{it.source}·清晰度",
                kind="remote",
                media_type="video",
                url=v.url,
                title=title,
                ext=".m3u8",
                kind_label="HLS 播放列表",
                score=max(60, it.score),
                detail=f"来自主清单 {it.title}；{v.resolution or '?'} {v.codecs or ''} "
                       f"{v.bandwidth // 1000}kbps",
                extra={"host": urllib.parse.urlsplit(v.url).hostname or "",
                       "resolution": v.resolution, "bandwidth": v.bandwidth,
                       "from_master": it.url},
            )
            new_items.append(item)
            made += 1
        if made:
            replaced.append(it)
            notes.append(f"m3u8 主清单 {it.title} 展开出 {made} 个清晰度")
    return new_items, replaced, notes


# ---------------------------------------------------------------- 分片下载

def _download_one(url: str, dest: Path, settings, cancel: threading.Event | None,
                  byte_range: tuple[int, int] | None = None,
                  referer: str = "") -> tuple[bool, int, str]:
    """下载单个分片/关键帧文件到 dest。返回 (ok, 字节数, 错误)。

    除了 http(s)，也支持 `data:` 与本地路径（file: / Windows 路径）——
    有些播放器/调试环境会把 AES 密钥写成 `file:///...` 或直接给绝对路径，
    这类 URI 交给 requests 会报 InvalidSchema，必须在这里自己读。
    """
    import requests

    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if url.startswith("data:") or scheme in ("", "file"):
        try:
            if url.startswith("data:"):
                raw = base64.b64decode(url.split(",", 1)[1])
            else:
                p = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path or url))
                raw = p.read_bytes()
            if byte_range is not None:
                start, length = byte_range
                raw = raw[start:start + length]
            dest.write_bytes(raw)
            return True, len(raw), ""
        except Exception as exc:  # noqa: BLE001
            return False, 0, f"本地读取失败: {exc}"

    attempts = max(1, int(getattr(settings, "http_retries", 3)))
    headers = _headers(settings, {"Referer": referer} if referer else None)
    if byte_range is not None:
        start, length = byte_range
        headers["Range"] = f"bytes={start}-{start + length - 1}"
    timeout = float(getattr(settings, "http_timeout", 30.0))
    last = ""
    for attempt in range(1, attempts + 1):
        if cancel is not None and cancel.is_set():
            return False, 0, "已取消"
        try:
            with requests.get(url, headers=headers, stream=True, timeout=timeout,
                              allow_redirects=True) as resp:
                if resp.status_code >= 400:
                    last = f"HTTP {resp.status_code}"
                    log.warning("分片失败 %s（%s）第 %d/%d 次", url[:140], last, attempt, attempts)
                else:
                    expect = _expected_len(resp, byte_range)
                    tmp = dest.with_suffix(dest.suffix + ".part")
                    got = 0
                    with open(tmp, "wb") as fh:
                        for chunk in resp.iter_content(chunk_size=1 << 16):
                            if cancel is not None and cancel.is_set():
                                fh.close()
                                tmp.unlink(missing_ok=True)
                                return False, got, "已取消"
                            if chunk:
                                fh.write(chunk)
                                got += len(chunk)
                    if expect and got != expect:
                        last = f"大小不符 {got}/{expect}"
                        tmp.unlink(missing_ok=True)
                        log.warning("分片不完整 %s（%s）第 %d/%d 次", url[:140], last, attempt, attempts)
                    else:
                        tmp.replace(dest)
                        return True, got, ""
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            log.warning("分片异常 %s（%s）第 %d/%d 次", url[:140], last, attempt, attempts)
        if attempt < attempts:
            time.sleep(min(2 ** attempt, 5))
    return False, 0, last or "下载失败"


def _expected_len(resp, byte_range: tuple[int, int] | None) -> int | None:
    try:
        if byte_range is not None:
            return byte_range[1]
        cl = resp.headers.get("Content-Length")
        if cl:
            return int(cl)
    except (TypeError, ValueError):
        pass
    return None


def _ext_for(url: str, init: bool = False) -> str:
    path = urllib.parse.urlsplit(url).path.lower()
    for ext in (".ts", ".m4s", ".mp4", ".m4v", ".aac", ".m4a", ".m4f", ".cmfv", ".cmfa", ".key"):
        if path.endswith(ext):
            return ext
    if init:
        return ".mp4"
    return ".ts"


def _write_local_playlist(pl: Playlist, seg_files: list[str], key_files: dict[str, str],
                          map_files: dict[str, str], path: Path) -> None:
    """生成一份只引用本地文件的 m3u8（让 ffmpeg 只做本地拼接）。"""
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{max(1, pl.target_duration)}",
             f"#EXT-X-MEDIA-SEQUENCE:{pl.media_sequence}", "#EXT-X-PLAYLIST-TYPE:VOD"]
    cur_key: KeyInfo | None = None
    cur_map = ""
    for seg, fname in zip(pl.segments, seg_files):
        if seg.discontinuity:
            lines.append("#EXT-X-DISCONTINUITY")
        key = seg.key
        if (key.key if key else None) != (cur_key.key if cur_key else None):
            if key and key.is_encrypted:
                local_key = key_files.get(key.uri, "")
                iv = f',IV={key.iv}' if key.iv else ""
                if local_key:
                    lines.append(f'#EXT-X-KEY:METHOD={key.method},URI="{local_key}"{iv}')
                else:
                    lines.append("#EXT-X-KEY:METHOD=NONE")
            else:
                lines.append("#EXT-X-KEY:METHOD=NONE")
            cur_key = key
        if seg.init_uri and seg.init_uri != cur_map:
            local_map = map_files.get(seg.init_uri, "")
            if local_map:
                lines.append(f'#EXT-X-MAP:URI="{local_map}"')
                cur_map = seg.init_uri
        lines.append(f"#EXTINF:{seg.duration:.3f},")
        lines.append(fname)
    lines.append("#EXT-X-ENDLIST")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_ffmpeg_remux(ffmpeg: str, local_playlist: Path, dest: Path, is_fmp4: bool,
                     cancel: threading.Event | None = None,
                     timeout: float = 3600.0) -> tuple[bool, str]:
    """把本地清单转封装成 mp4（纯本地操作，无网络风险）。"""
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "info",
        "-allowed_extensions", "ALL",
        "-protocol_whitelist", "file,crypto,data",
        "-i", str(local_playlist),
        "-c", "copy",
    ]
    if not is_fmp4:
        # TS -> MP4 需要把 ADTS 音频转成 ASC；fMP4 输入已经是 ASC，加了反而报错
        cmd += ["-bsf:a", "aac_adtstoasc"]
    cmd += ["-movflags", "+faststart", str(dest)]
    log.info("ffmpeg 本地合并: %s", subprocess.list2cmdline(cmd))
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
    except OSError as exc:
        return False, f"启动 ffmpeg 失败: {exc}"
    tail: list[str] = []
    assert proc.stderr is not None
    try:
        for line in proc.stderr:
            line = line.rstrip()
            if not line:
                continue
            tail.append(line)
            if len(tail) > 30:
                tail.pop(0)
            log.info("  [ffmpeg] %s", line[:300])
            if cancel is not None and cancel.is_set():
                log.warning("取消 HLS 合并，终止 ffmpeg")
                proc.terminate()
                break
        code = proc.wait(timeout=min(120.0, timeout))
        log.info("ffmpeg 退出码=%s", code)
        if code not in (0, 255) or not dest.exists():
            return False, f"ffmpeg 合并失败（退出码 {code}）：{' | '.join(tail[-3:])}"
        return True, ""
    except subprocess.TimeoutExpired:
        proc.kill()
        return False, "ffmpeg 合并超时"
    except Exception as exc:  # noqa: BLE001
        return False, f"ffmpeg 合并异常: {exc}"


@dataclass
class HlsDownloadResult:
    ok: bool = False
    path: str = ""
    segments: int = 0
    failed_segments: int = 0
    total_bytes: int = 0
    expected_duration: float = 0.0
    actual_duration: float | None = None
    detail: str = ""


def download_media_playlist(url: str, dest: Path, settings, ffmpeg: str,
                            cancel: threading.Event | None = None,
                            on_progress=None) -> HlsDownloadResult:
    """分片级下载一个 HLS 媒体清单并转封装成 mp4。

    `on_progress(done_segments, total_segments, bytes)`
    """
    res = HlsDownloadResult()
    pl = fetch_playlist(url, settings, cancel)
    if not pl.ok:
        res.detail = pl.error or "清单不可用"
        return res
    if pl.is_master:
        # 用户直接点了主清单：自动挑最高码率
        best = max(pl.variants, key=lambda v: (v.bandwidth, v.average_bandwidth))
        log.info("目标主清单，自动选择最高码率: %s %s", best.resolution, best.url[:140])
        pl = fetch_playlist(best.url, settings, cancel)
        if not pl.ok:
            res.detail = pl.error or "清晰度清单不可用"
            return res

    total = len(pl.segments)
    res.expected_duration = pl.total_duration
    log.info("HLS 分片下载: %d 片, 声明总时长 %.1fs -> %s", total, pl.total_duration, dest.name)
    if total == 0:
        res.detail = "清单里没有分片"
        return res

    work = dest.with_name(f".{dest.stem}_hls")
    try:
        work.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        res.detail = f"创建临时目录失败: {exc}"
        return res

    key_files: dict[str, str] = {}
    map_files: dict[str, str] = {}
    seg_files: list[str] = []
    got_bytes = 0
    failed = 0
    t0 = time.time()
    is_fmp4 = any(s.init_uri for s in pl.segments) or any(
        _ext_for(s.url) in (".m4s", ".mp4", ".m4f", ".cmfv") for s in pl.segments)
    try:
        # init 段（fMP4 必需）
        for seg in pl.segments:
            if not seg.init_uri or seg.init_uri in map_files:
                continue
            name = f"init_{len(map_files)}.mp4"
            ok, _n, err = _download_one(seg.init_uri, work / name, settings, cancel,
                                        referer=url)
            if not ok:
                res.detail = f"init 段下载失败: {err}"
                return res
            map_files[seg.init_uri] = name

        # 密钥（AES-128）
        for seg in pl.segments:
            key = seg.key
            if not key or not key.is_encrypted or not key.uri or key.uri in key_files:
                continue
            name = f"key_{len(key_files)}.key"
            if key.uri.startswith("data:"):
                try:
                    raw = base64.b64decode(key.uri.split(",", 1)[1])
                    (work / name).write_bytes(raw)
                    key_files[key.uri] = name
                    continue
                except Exception as exc:  # noqa: BLE001
                    res.detail = f"data: 密钥解析失败: {exc}"
                    return res
            ok, _n, err = _download_one(key.uri, work / name, settings, cancel, referer=url)
            if not ok:
                res.detail = f"密钥下载失败: {err}"
                return res
            key_files[key.uri] = name

        # 分片
        for seg in pl.segments:
            if cancel is not None and cancel.is_set():
                res.detail = "已取消"
                return res
            ext = _ext_for(seg.url) if not is_fmp4 else (_ext_for(seg.url) or ".m4s")
            name = f"seg_{seg.index:05d}{ext}"
            ok, n, err = _download_one(seg.url, work / name, settings, cancel,
                                       byte_range=seg.byte_range, referer=url)
            if not ok:
                failed += 1
                res.detail = f"第 {seg.index + 1}/{total} 片失败: {err}"
                log.error("HLS 分片下载失败 %s", res.detail)
                return res
            got_bytes += n
            seg_files.append(name)
            if on_progress is not None:
                try:
                    on_progress(len(seg_files), total, got_bytes)
                except Exception:  # noqa: BLE001
                    pass

        local_pl = work / "local.m3u8"
        _write_local_playlist(pl, seg_files, key_files, map_files, local_pl)
        log.info("本地清单已生成: %s（%d 片，%s，用时 %.1fs）",
                 local_pl, len(seg_files), human_size(got_bytes), time.time() - t0)

        if not ffmpeg:
            res.detail = "缺少 ffmpeg，无法把分片合并成 mp4"
            return res
        tmp_dest = dest.with_suffix(dest.suffix + ".part.mp4")
        ok, err = run_ffmpeg_remux(ffmpeg, local_pl, tmp_dest, is_fmp4, cancel)
        if not ok:
            res.detail = err
            try:
                tmp_dest.unlink(missing_ok=True)
            except OSError:
                pass
            return res
        tmp_dest.replace(dest)
        res.ok = True
        res.path = str(dest)
        res.segments = len(seg_files)
        res.failed_segments = failed
        res.total_bytes = dest.stat().st_size if dest.exists() else got_bytes

        from . import sysinfo

        fp = sysinfo.find_ffprobe(settings)
        if fp:
            res.actual_duration = sysinfo.ffprobe_duration(fp, dest)
            if res.actual_duration and pl.total_duration > 1:
                diff = abs(res.actual_duration - pl.total_duration)
                if diff > max(2.0, pl.total_duration * 0.03):
                    res.detail = (f"成品时长 {res.actual_duration:.1f}s 与清单声明 "
                                  f"{pl.total_duration:.1f}s 相差 {diff:.1f}s，可能仍不完整")
                    log.warning("%s", res.detail)
                else:
                    log.info("时长校验通过: 成品 %.1fs / 清单 %.1fs",
                             res.actual_duration, pl.total_duration)
        return res
    finally:
        if res.ok:
            shutil.rmtree(work, ignore_errors=True)
        else:
            log.warning("HLS 临时文件保留在 %s（排查用，可手工删除）", work)


__all__ = [
    "Variant", "KeyInfo", "Segment", "Playlist", "HlsDownloadResult",
    "parse_playlist", "fetch_playlist", "fetch_text", "list_variants",
    "expand_hls_items", "download_media_playlist", "run_ffmpeg_remux",
]
