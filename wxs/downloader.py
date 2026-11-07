"""下载器：HTTP(S) 断点续传 / HLS 分片下载 / 本地缓存复制 / 批量队列。

设计
----
* 一次调用下载一批 MediaItem，内部 ThreadPoolExecutor 并发；每个条目状态独立。
* 远端下载用 .part 临时文件 + Range 续传；中断后重跑会接着写。
* **完整性** 是本模块的重点（用户实测过“时长对但前半段没声音/画面不动”）：
  - 本地缓存：先等文件大小稳定（小程序可能还在写入），复制到 .part 再改名，
    复制完对比源文件有没有变大；
  - 远端：写完核对 Content-Length，中间出现空洞/零块要告警；
  - HLS：交给 hls.py 逐片下载+重试（比直接丢给 ffmpeg 全得多）；
  - 收尾统一做“体检”：文件头、ffprobe 时长、开头若干秒解码报错数。
* m3u8/mpd 优先用内置分片下载器，失败再退回 ffmpeg 合并。
* 所有输出事件（状态/进度/收尾）通过回调抛给 GUI，绝不直接碰 Tk。
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from . import hls, sysinfo
from .logsetup import human_size, human_speed, hexdump_head
from .models import MediaItem

log = logging.getLogger("wxs.dl")

HTML_HINTS = (b"<!doctype html", b"<html", b"{\"code\"", b"<?xml")
IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")
DONE = "已完成"
DONE_WARN = "已完成⚠"


@dataclass
class TaskState:
    uid: str
    status: str = "待下载"
    done_bytes: int = 0
    total_bytes: int | None = None
    speed: float = 0.0
    out_path: str = ""
    error: str = ""
    warn: str = ""
    started: float = 0.0
    finished: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status.startswith(DONE) or self.status == "已存在"

    @property
    def percent(self) -> float:
        if self.status.startswith(DONE):
            return 100.0
        if not self.total_bytes:
            return 0.0
        return min(99.9, self.done_bytes * 100.0 / self.total_bytes)

    def progress_label(self) -> str:
        if self.status.startswith(DONE):
            return f"100% ({human_size(self.done_bytes)})"
        if self.total_bytes:
            return f"{self.percent:5.1f}% {human_size(self.done_bytes)}/{human_size(self.total_bytes)}"
        return f"{human_size(self.done_bytes)}" if self.done_bytes else "-"

    def row(self) -> tuple:
        return (
            self.status,
            self.progress_label(),
            human_speed(self.speed) if self.status == "下载中" else "-",
            os.path.basename(self.out_path) if self.out_path else "-",
            (self.warn or self.error)[:60],
        )


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(2, 1000):
        cand = path.with_name(f"{stem}_{i}{suffix}")
        if not cand.exists():
            return cand
    return path.with_name(f"{stem}_{int(time.time())}{suffix}")


def _ca_bundle() -> str:
    """找一份 CA 证书给 ffmpeg 用。

    坑：Windows 上 ffmpeg（gyan.dev 的 openssl 版）默认没有 CA bundle，
    访问 https 的 m3u8 会报 "Peer certificate failed verification"，
    而 requests 却能正常访问 —— 会让人误以为是链接失效。
    requests 依赖 certifi，直接复用它的 cacert.pem 即可。
    """
    try:
        import certifi

        path = Path(certifi.where())
        if path.is_file():
            return str(path)
    except Exception:  # noqa: BLE001
        pass
    candidates = [
        Path(sys.prefix) / "Lib" / "site-packages" / "certifi" / "cacert.pem",
        Path(__file__).resolve().parent.parent / "tools" / "cacert.pem",
    ]
    for p in candidates:
        if p.is_file():
            return str(p)
    return ""


def _media_signature_ok(path: Path) -> tuple[bool, str]:
    """粗校验：mp4/mov 的 ftyp、webm 的 EBML、mp3/图片的文件头。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
    except OSError as exc:
        return False, f"读文件头失败: {exc}"
    if not head:
        return False, "文件为空"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return True, f"ftyp brand={head[8:12]!r}"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return True, "EBML/Matroska"
    if head[:3] == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return True, "MP3"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True, "WebP"
    if head[:4] == b"RIFF":
        return True, "RIFF/WAV"
    if head[:3] == b"\xff\xd8\xff":
        return True, "JPEG"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return True, "PNG"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return True, "GIF"
    if head[:2] == b"BM":
        return True, "BMP"
    if head[:4] == b"\x00\x00\x01\x00":
        return True, "ICO"
    if head[:5].lower() in (b"<?xml", b"<svg "):
        return True, "SVG/XML"
    return False, f"未知文件头 {head[:16]!r}"


def _zero_ratio(path: Path, limit: int = 8 * 1024 * 1024, block: int = 64 * 1024) -> float:
    """前 limit 字节里“全零块”的占比。

    “时长对、前半段没声音画面也不动”的文件，特征就是前段 mdat 是零。
    这个指标成本极低（只读 8MB），能直接把这台机器的典型症状指出来。
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read(limit)
    except OSError:
        return 0.0
    if not data:
        return 1.0
    total_blocks = 0
    zero_blocks = 0
    for off in range(0, len(data), block):
        chunk = data[off:off + block]
        if not chunk:
            break
        total_blocks += 1
        if not chunk.strip(b"\x00"):
            zero_blocks += 1
    if not total_blocks:
        return 0.0
    return zero_blocks / total_blocks


def _wait_file_stable(path: Path, max_wait: float,
                      cancel: threading.Event | None = None,
                      stable_for: float = 1.2,
                      fresh_age: float = 3.0) -> tuple[int, bool]:
    """等文件大小稳定下来（小程序可能还在往里写）。

    返回 (最后大小, 是否稳定)。超时也算稳定，但调用方会据此告警。

    性能考虑：批里可能有几十个缓存文件，每个都干等 1.2 秒会白花半分钟。
    所以先看 mtime —— 已经 3 秒没动过的文件直接当成写完了，只有“刚被写过”的才盯着看。
    """
    try:
        stat = path.stat()
    except OSError:
        return 0, False
    size = stat.st_size
    if max_wait <= 0 or (time.time() - stat.st_mtime) >= fresh_age:
        return size, True
    t0 = time.time()
    last_change = time.time()
    while time.time() - t0 < max_wait:
        if cancel is not None and cancel.is_set():
            return size, False
        time.sleep(0.4)
        try:
            new_size = path.stat().st_size
        except OSError:
            return size, False
        if new_size != size:
            size = new_size
            last_change = time.time()
        elif time.time() - last_change >= stable_for:
            return size, True
    return size, False


class Downloader:
    """批量下载/复制执行器。线程安全：状态字典加锁更新。"""

    def __init__(self, settings, on_event=None) -> None:
        self.settings = settings
        self.on_event = on_event or (lambda *_a, **_k: None)
        self.states: dict[str, TaskState] = {}
        self._lock = threading.Lock()
        self.cancel_event = threading.Event()
        self.ffmpeg = sysinfo.find_ffmpeg(settings)
        self.ffprobe = sysinfo.find_ffprobe(settings)

    # ------------------------------------------------------------ 事件

    def _emit(self, kind: str, uid: str, **payload) -> None:
        try:
            self.on_event(kind, uid, payload)
        except Exception:  # noqa: BLE001
            pass

    def _set(self, uid: str, **fields) -> None:
        with self._lock:
            st = self.states.setdefault(uid, TaskState(uid=uid))
            for k, v in fields.items():
                setattr(st, k, v)
            snapshot = TaskState(**{**st.__dict__})
        self._emit("state", uid, state=snapshot)

    # ------------------------------------------------------------ 主入口

    def run(self, items: list[MediaItem], label: str = "") -> dict[str, TaskState]:
        out_dir = Path(self.settings.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        self.cancel_event.clear()

        for it in items:
            self._set(it.uid, status="排队中", done_bytes=0, total_bytes=None, speed=0.0,
                      error="", warn="", out_path="", started=0.0, finished=0.0)

        log.info("=" * 90)
        log.info("开始下载%s: %d 个条目, 并发=%d, 输出目录=%s",
                 f"（{label}）" if label else "", len(items), self.settings.concurrency, out_dir)
        for i, it in enumerate(items, 1):
            log.info("  [%02d] %s | %s | %s", i, it.kind_label, it.title[:60],
                     it.url[:120] if it.kind == "remote" else it.path)
        log.info("=" * 90)

        ok = failed = skipped = warned = 0
        t0 = time.perf_counter()
        workers = max(1, min(int(self.settings.concurrency), len(items) or 1))

        if len(items) == 1:
            worker = self._download_local if items[0].is_file else self._download_remote
            try:
                worker(items[0], out_dir)
            except Exception as exc:  # noqa: BLE001
                log.exception("下载异常: %s", exc)
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dl") as pool:
                futures = {}
                for it in items:
                    fn = self._download_local if it.is_file else self._download_remote
                    futures[pool.submit(fn, it, out_dir)] = it
                for fut in as_completed(futures):
                    it = futures[fut]
                    try:
                        fut.result()
                    except Exception as exc:  # noqa: BLE001
                        log.exception("条目 %s 下载异常: %s", it.title, exc)
                        self._set(it.uid, status="失败", error=str(exc)[:200])

        with self._lock:
            for it in items:
                st = self.states.get(it.uid)
                if not st:
                    continue
                if st.status == DONE_WARN:
                    ok += 1
                    warned += 1
                elif st.status == DONE or st.status == "已存在":
                    ok += 1
                elif st.status in ("已取消", "已跳过"):
                    skipped += 1
                else:
                    failed += 1

        elapsed = time.perf_counter() - t0
        total_bytes = sum((self.states[i.uid].done_bytes for i in items if i.uid in self.states), 0)
        log.info(
            "下载结束: 成功 %d（其中可疑 %d）/ 跳过 %d / 失败 %d | 共 %s | 耗时 %.1fs | 平均 %s",
            ok, warned, skipped, failed, human_size(total_bytes), elapsed,
            human_speed(total_bytes / elapsed if elapsed > 0 else 0),
        )
        for it in items:
            st = self.states.get(it.uid)
            if st and not st.ok:
                log.warning("   未完成: %s -> %s %s", it.title[:60], st.status, st.error)
            elif st and st.warn:
                log.warning("   可疑: %s -> %s", it.title[:60], st.warn)
        return dict(self.states)

    # ------------------------------------------------------------ 本地文件复制

    def _download_local(self, item: MediaItem, out_dir: Path) -> None:
        src = Path(item.path)
        if not src.exists():
            log.error("源文件不存在: %s", src)
            self._set(item.uid, status="失败", error="源文件不存在")
            return
        try:
            src_size = src.stat().st_size
        except OSError as exc:
            self._set(item.uid, status="失败", error=f"读源文件失败: {exc}")
            return
        if src_size <= 0:
            self._set(item.uid, status="失败", error="源文件为空")
            return

        # 关键：小程序可能还在往这个文件里写。等它稳定再复制，
        # 否则复制出来就是“时长对但前段没数据”的半成品（用户实测过的症状）。
        warn = ""
        wait = float(getattr(self.settings, "local_wait_seconds", 8.0) or 0.0)
        stable_size, stable = _wait_file_stable(src, wait, self.cancel_event)
        if not stable and stable_size != src_size:
            warn = "源文件一直在变大（可能仍在写入），复制的是不完整快照"
            log.warning("本地缓存 %s 仍在写入（%s），%s", src.name, human_size(stable_size), warn)

        dest = out_dir / item.target_name()
        if dest.exists() and src.exists() and dest.stat().st_size == src.stat().st_size:
            log.info("本地文件已存在且大小一致，跳过: %s", dest.name)
            self._set(item.uid, status="已存在", out_path=str(dest),
                      done_bytes=dest.stat().st_size, total_bytes=dest.stat().st_size)
            return
        dest = _unique_path(dest)
        part = dest.with_suffix(dest.suffix + ".part")
        total = src_size
        log.info("复制本地缓存: %s (%s) -> %s", src, human_size(total), dest)
        self._set(item.uid, status="下载中", total_bytes=total, out_path=str(dest),
                  started=time.time())

        done = 0
        speed_win: deque[tuple[float, int]] = deque(maxlen=24)
        last_emit = 0.0
        try:
            with open(src, "rb") as fin, open(part, "wb") as fout:
                while True:
                    if self.cancel_event.is_set():
                        log.warning("用户取消，已复制 %s: %s", human_size(done), dest.name)
                        part.unlink(missing_ok=True)
                        self._set(item.uid, status="已取消", done_bytes=done)
                        return
                    buf = fin.read(8 * 1024 * 1024)
                    if not buf:
                        break
                    fout.write(buf)
                    done += len(buf)
                    now = time.perf_counter()
                    speed_win.append((now, done))
                    speed = self._speed(speed_win)
                    if now - last_emit > 0.15 or done >= total:
                        last_emit = now
                        self._set(item.uid, done_bytes=done, speed=speed,
                                  total_bytes=max(total, done))
            try:
                shutil.copystat(src, part)
            except OSError:
                pass
            # 复制期间源文件又变大了 = 拿到的仍是快照
            try:
                now_size = src.stat().st_size
            except OSError:
                now_size = done
            if now_size > done:
                warn = (f"复制期间源文件又增长了 {human_size(now_size - done)}，"
                        f"这份可能是半成品（等视频播放/缓存完成后再复制更稳）")
                log.warning("%s: %s", dest.name, warn)
            part.replace(dest)
            self._finalize(item, dest, total_bytes=done, warn=warn)
        except OSError as exc:
            log.exception("复制失败 %s: %s", src, exc)
            part.unlink(missing_ok=True)
            self._set(item.uid, status="失败", error=str(exc)[:200])

    # ------------------------------------------------------------ 远端下载

    def _download_remote(self, item: MediaItem, out_dir: Path) -> None:
        url_path = item.url.lower().split("?")[0]
        if item.is_hls or item.kind_label in ("HLS 播放列表", "DASH 清单") or \
                url_path.endswith((".m3u8", ".mpd")):
            self._download_stream_manifest(item, out_dir)
            return

        import requests

        dest = out_dir / item.target_name()
        part = dest.with_suffix(dest.suffix + ".part")
        resume_from = part.stat().st_size if part.exists() else 0
        attempt = 0
        max_attempts = max(1, int(self.settings.http_retries))
        ref_idx = 0
        while attempt < max_attempts:
            attempt += 1
            if self.cancel_event.is_set():
                self._set(item.uid, status="已取消")
                return
            headers = self._base_headers(item, ref_idx)
            if resume_from:
                log.info("检测到未完成文件 %s (%s)，尝试断点续传", part.name, human_size(resume_from))
                headers["Range"] = f"bytes={resume_from}-"
            log.info("HTTP 请求: GET %s", item.url)
            for k, v in headers.items():
                log.debug("   请求头 %s: %s", k, v)
            try:
                t0 = time.perf_counter()
                with requests.get(
                    item.url, headers=headers, stream=True, timeout=self.settings.http_timeout,
                    allow_redirects=True,
                ) as resp:
                    elapsed = (time.perf_counter() - t0) * 1000
                    log.info("HTTP 响应: %s %s | %s | 最终URL=%s | 首字节 %.0fms",
                             resp.status_code, resp.reason, resp.headers.get("Content-Type", "?"),
                             resp.url[:160], elapsed)
                    for k in ("Content-Length", "Content-Range", "Accept-Ranges", "Content-Type", "Server"):
                        if k in resp.headers:
                            log.debug("   响应头 %s: %s", k, resp.headers[k])

                    if resp.status_code == 416 and resume_from:
                        log.warning("服务器返回 416，可能已下载完成，删除 .part 重试")
                        part.unlink(missing_ok=True)
                        resume_from = 0
                        continue
                    if resp.status_code in (401, 403):
                        body = resp.raw.read(400, decode_content=True)[:400]
                        log.error("HTTP %s 拒绝访问，签名可能已过期或需要登录。响应片段: %r",
                                  resp.status_code, body[:200])
                        # Referer 换一种写法再试（微信 CDN 对 referer 很挑）
                        if ref_idx < 2:
                            ref_idx += 1
                            attempt = max(1, attempt - 1)  # 这一次不算失败
                            log.info("换 Referer 重试: %s", self._base_headers(item, ref_idx).get("Referer"))
                            continue
                        self._set(item.uid, status="失败",
                                  error=f"HTTP {resp.status_code} 无权限/签名过期")
                        return
                    if resp.status_code >= 400:
                        log.error("HTTP %s %s", resp.status_code, resp.reason)
                        if attempt < max_attempts:
                            time.sleep(min(2 ** attempt, 8))
                            continue
                        self._set(item.uid, status="失败", error=f"HTTP {resp.status_code}")
                        return

                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    if "text/html" in ctype or "application/json" in ctype:
                        body = resp.raw.read(600, decode_content=True)[:600]
                        log.error("服务器返回的是网页/JSON 而不是媒体（%s）。片段: %r", ctype, body[:300])
                        self._set(item.uid, status="失败", error=f"返回内容不是媒体({ctype})")
                        return

                    total = self._total_bytes(resp, resume_from)
                    mode = "ab" if resume_from else "wb"
                    done = resume_from
                    self._set(item.uid, status="下载中", total_bytes=total, done_bytes=done,
                              out_path=str(dest), started=time.time(), error="")
                    speed_win: deque[tuple[float, int]] = deque(maxlen=24)
                    last_emit = 0.0
                    with open(part, mode) as fout:
                        for chunk in resp.iter_content(chunk_size=self.settings.chunk_size):
                            if self.cancel_event.is_set():
                                log.warning("用户取消，保留断点文件 %s (%s)", part.name, human_size(done))
                                self._set(item.uid, status="已取消", done_bytes=done)
                                return
                            if not chunk:
                                continue
                            fout.write(chunk)
                            done += len(chunk)
                            now = time.perf_counter()
                            speed_win.append((now, done))
                            if now - last_emit > 0.15:
                                last_emit = now
                                self._set(item.uid, done_bytes=done, speed=self._speed(speed_win))
                    self._set(item.uid, done_bytes=done, speed=0.0)

                # 校验 + 改名
                if total and done < total:
                    log.warning("下载不完整: %s / %s，保留 .part 以便续传",
                                human_size(done), human_size(total))
                    if attempt < max_attempts:
                        time.sleep(1.5)
                        resume_from = done
                        continue
                    self._set(item.uid, status="失败",
                              error=f"不完整 {human_size(done)}/{human_size(total)}")
                    return

                ok, why = _media_signature_ok(part)
                log.info("文件校验: %s | %s", "通过" if ok else "可疑", why)
                if not ok and item.is_image:
                    # 图片有时候是 svg/webp，头对不上就放弃
                    log.warning("图片文件头不认识，仍保留: %s", part.name)
                final = dest if not dest.exists() else _unique_path(dest)
                part.replace(final)
                self._finalize(item, final, total_bytes=done)
                return

            except requests.RequestException as exc:
                log.warning("第 %d/%d 次请求失败: %s: %s", attempt, max_attempts,
                            type(exc).__name__, exc)
                if attempt < max_attempts:
                    delay = min(2 ** attempt, 10)
                    log.info("  %.1fs 后重试", delay)
                    time.sleep(delay)
                    if part.exists():
                        resume_from = part.stat().st_size
                else:
                    self._set(item.uid, status="失败",
                              error=f"{type(exc).__name__}: {exc}"[:200])
            except OSError as exc:
                log.exception("写文件失败: %s", exc)
                self._set(item.uid, status="失败", error=str(exc)[:200])
                return

    def _total_bytes(self, resp, resume_from: int) -> int | None:
        try:
            if resp.status_code == 206:
                cr = resp.headers.get("Content-Range", "")
                if "/" in cr:
                    return int(cr.rsplit("/", 1)[1])
                cl = resp.headers.get("Content-Length")
                return (resume_from + int(cl)) if cl else None
            cl = resp.headers.get("Content-Length")
            return int(cl) if cl else None
        except (ValueError, TypeError):
            return None

    def _base_headers(self, item: MediaItem, variant: int = 0) -> dict[str, str]:
        """请求头。variant 用来在 403 时换一种 Referer 再试。

        微信文档写明 referrer 的规范形态是
        `https://servicewechat.com/{appid}/{version}/page-frame.html`
        （video 组件的 referrer-policy），所以知道 appid 时优先用它。
        """
        h = {
            "User-Agent": self.settings.user_agent,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
        }
        appid = (item.appid or "").strip()
        variants = []
        if appid:
            variants.append(f"https://servicewechat.com/{appid}/0/page-frame.html")
        variants.append(self.settings.referer or "https://servicewechat.com/")
        variants.append("https://servicewechat.com/")
        if variant > 0:
            variants.append("")
        h["Referer"] = variants[min(variant, len(variants) - 1)]
        if not h["Referer"]:
            h.pop("Referer")
        return h

    # ------------------------------------------------------------ HLS / DASH

    def _download_stream_manifest(self, item: MediaItem, out_dir: Path) -> None:
        if not self.ffmpeg:
            log.error("需要 ffmpeg 才能下载 m3u8/mpd，但没找到 ffmpeg。请安装或设置路径。")
            self._set(item.uid, status="失败", error="缺少 ffmpeg")
            return
        dest = out_dir / item.target_name()
        if dest.suffix.lower() not in (".mp4", ".mkv", ".ts"):
            dest = dest.with_suffix(".mp4")
        dest = _unique_path(dest)
        self._set(item.uid, status="下载中", out_path=str(dest), started=time.time())

        # 首选：内置分片下载器（逐片校验+重试，能避免“缺一段还写出文件”）
        if item.is_hls and getattr(self.settings, "native_hls", True):
            log.info("使用内置 HLS 分片下载器: %s", item.url[:160])

            def on_progress(done: int, total: int, got: int) -> None:
                self._set(item.uid, done_bytes=got, total_bytes=None,
                          status=f"下载中 {done}/{total} 片")

            try:
                res = hls.download_media_playlist(
                    item.url, dest, self.settings, self.ffmpeg,
                    cancel=self.cancel_event, on_progress=on_progress,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("内置 HLS 下载异常: %s", exc)
                res = hls.HlsDownloadResult(ok=False, detail=str(exc))
            if res.ok:
                log.info("内置 HLS 下载完成: %s（%d 片，%s）", dest.name, res.segments,
                         human_size(res.total_bytes))
                warn = res.detail or ""
                self._finalize(item, dest, total_bytes=res.total_bytes, warn=warn,
                               expected_duration=res.actual_duration or res.expected_duration)
                return
            log.warning("内置 HLS 下载失败（%s），退回 ffmpeg 直连方式", res.detail)
            self._set(item.uid, status="下载中", error="")

        log.info("使用 ffmpeg 合并流: %s", item.url)

        # 坑：不要用 -headers 'Referer: x\r\nUser-Agent: y'。
        # ffmpeg 的命令行参数里塞 CRLF 会被 CreateProcess/解析器吃掉，
        # 报 "No trailing CRLF found in HTTP header"。HTTP 协议本身有官方选项：
        #   -user_agent / -referer / -cookies
        headers = self._base_headers(item)
        cmd = [
            self.ffmpeg, "-y", "-hide_banner", "-loglevel", "info", "-stats",
            "-user_agent", headers.get("User-Agent", self.settings.user_agent),
        ]
        if headers.get("Referer"):
            cmd += ["-referer", headers["Referer"]]
        ca = _ca_bundle()
        # 坑：-ca_file 属于 tls 协议选项。http:// 的输入用不到它，ffmpeg 会把没被消费的
        # 输入选项当成错误 —— 实测报 "Error opening input files: Option not found"。
        if ca and item.url.lower().startswith("https"):
            cmd += ["-ca_file", ca]
            log.info("ffmpeg 使用 CA 证书: %s", ca)
        # 网络抖动时让 ffmpeg 自己重连（直连 HLS 时能少丢分片）；
        # -allowed_extensions ALL 是必须的：m3u8 里常引用 .key/.m4s 之类
        # ffmpeg 认为“不像媒体”的扩展名，不放行会报
        # "Filename extension ... blocked for security reasons" 直接失败。
        cmd += [
            "-allowed_extensions", "ALL",
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
            "-rw_timeout", "30000000",
            "-i", item.url, "-c", "copy", "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart", str(dest),
        ]
        log.info("ffmpeg 命令: %s", subprocess.list2cmdline(cmd))
        ok = self._run_ffmpeg_progress(item, cmd, dest)
        if ok and dest.exists():
            self._finalize(item, dest, total_bytes=dest.stat().st_size)
        elif not ok:
            self._set(item.uid, status="失败", error="ffmpeg 合并失败（详见日志）")

    def _run_ffmpeg_progress(self, item: MediaItem, cmd: list[str], dest: Path) -> bool:
        """跑 ffmpeg 并把 stderr 的每一行写进日志（这是排查问题的第一手材料）。"""
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except OSError as exc:
            log.error("启动 ffmpeg 失败: %s", exc)
            return False
        tail: deque[str] = deque(maxlen=12)
        last_size = 0
        t_last = time.perf_counter()
        try:
            assert proc.stderr is not None
            for line in proc.stderr:
                line = line.rstrip()
                if not line:
                    continue
                tail.append(line)
                log.info("  [ffmpeg] %s", line[:300])
                now = time.perf_counter()
                if now - t_last >= 0.5:
                    t_last = now
                    try:
                        size = dest.stat().st_size if dest.exists() else 0
                    except OSError:
                        size = 0
                    if size:
                        self._set(item.uid, done_bytes=size, speed=max(0.0, size - last_size) / 0.5)
                        last_size = size
                if self.cancel_event.is_set():
                    log.warning("取消 ffmpeg 合并，终止进程")
                    proc.terminate()
                    break
                if "=" in line and line.startswith(("out_time", "status")):
                    log.debug("  ffmpeg 进度 %s", line)
            code = proc.wait(timeout=30)
            log.info("ffmpeg 退出码=%s", code)
            if code not in (0, 255) or not dest.exists():
                log.error("ffmpeg 失败，最后输出:\n  %s", "\n  ".join(tail))
                return False
            return True
        except subprocess.TimeoutExpired:
            log.error("ffmpeg 未在 30s 内退出，强制结束")
            proc.kill()
            return False
        except Exception as exc:  # noqa: BLE001
            log.exception("ffmpeg 运行异常: %s", exc)
            return False

    # ------------------------------------------------------------ 收尾 / 体检

    def _finalize(self, item: MediaItem, final: Path, *, total_bytes: int | None,
                  warn: str = "", expected_duration: float | None = None) -> None:
        size = total_bytes
        try:
            size = final.stat().st_size
        except OSError:
            pass

        # 图片过小（图标/占位图）直接丢掉，避免输出目录被垃圾塞满
        if item.is_image:
            min_bytes = max(0, int(getattr(self.settings, "min_image_kb", 24))) * 1024
            if size and size < min_bytes:
                log.info("跳过过小的图片 %s (%s < %s)", final.name, human_size(size),
                         human_size(min_bytes))
                try:
                    final.unlink(missing_ok=True)
                except OSError:
                    pass
                self._set(item.uid, status="已跳过", done_bytes=0, error=f"图片过小({human_size(size)})")
                return

        log.info("✔ 下载完成: %s (%s) 耗时 %.1fs", final, human_size(size),
                 time.time() - (self.states.get(item.uid).started or time.time()))
        health = self._check_integrity(item, final, expected_duration)
        if health:
            warn = (warn + "；" if warn else "") + health
        status = DONE_WARN if warn else DONE
        log.info("   体检结论: %s", warn or "正常")
        self._set(item.uid, status=status, done_bytes=size or 0, total_bytes=size,
                  out_path=str(final), speed=0.0, finished=time.time(),
                  warn=warn[:300], error="")
        self._emit("done", item.uid, path=str(final), size=size, warn=warn)

    def _check_integrity(self, item: MediaItem, final: Path,
                         expected_duration: float | None = None) -> str:
        """下载后体检。返回告警文本（正常时返回空串）。

        检查项（都是低成本、能把“不完整”直接指出来）：
        1. 文件头（mp4 的 ftyp / 图片的 JPEG、PNG 魔数）
        2. 前 8MB 的全零块占比 —— 直接命中“前半段没数据”的症状
        3. ffprobe 时长（有期望时长时对比；没有期望时长就看能不能读出时长）
        4. 开头 15 秒解码报错数 —— 缺数据的文件会刷错误
        """
        warns: list[str] = []
        ok, why = _media_signature_ok(final)
        log.info("   文件头: %s | 判定=%s", hexdump_head(final, 24), why)
        if not ok:
            warns.append(f"文件头可疑（{why}）")
        if item.is_image:
            return "；".join(warns)

        try:
            size = final.stat().st_size
        except OSError:
            size = 0
        zero = _zero_ratio(final)
        if zero > 0.15:
            warns.append(f"前 8MB 有 {zero * 100:.0f}% 是空白数据，播放时可能从中间才开始")
            log.warning("   零块占比 %.1f%%（文件 %s）", zero * 100, human_size(size))

        duration = None
        if self.ffprobe:
            info = sysinfo.ffprobe_summary(self.ffprobe, final)
            if info:
                log.info("   ffprobe: %s", info)
            duration = sysinfo.ffprobe_duration(self.ffprobe, final)
            streams = sysinfo.ffprobe_streams(self.ffprobe, final)
            kinds = [s.get("codec_type") for s in streams]
            if kinds and "audio" not in kinds:
                warns.append("没有音轨")
            if expected_duration and duration:
                diff = abs(duration - expected_duration)
                if diff > max(2.0, expected_duration * 0.03):
                    warns.append(f"时长 {duration:.1f}s≠期望 {expected_duration:.1f}s")
        if self.ffmpeg and size > 64 * 1024:
            errors, tail = sysinfo.probe_decode_errors(self.ffmpeg, final, seconds=15)
            if errors:
                warns.append(f"开头解码报错 {errors} 行（可能缺数据）")
                log.warning("   解码体检: %d 行报错 | %s", errors, tail)
            else:
                log.info("   解码体检: 开头 15s 无报错")
        return "；".join(warns)

    @staticmethod
    def _speed(win: deque[tuple[float, int]]) -> float:
        if len(win) < 2:
            return 0.0
        t0, b0 = win[0]
        t1, b1 = win[-1]
        dt = t1 - t0
        if dt <= 0:
            return 0.0
        return max(0.0, (b1 - b0) / dt)


def sha256_of(path: Path, limit: int | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        remaining = limit
        while True:
            if remaining is not None and remaining <= 0:
                break
            buf = fh.read(1 << 20 if remaining is None else min(1 << 20, remaining))
            if not buf:
                break
            h.update(buf)
            if remaining is not None:
                remaining -= len(buf)
    return h.hexdigest()


__all__ = ["Downloader", "TaskState", "sha256_of", "DONE", "DONE_WARN"]
