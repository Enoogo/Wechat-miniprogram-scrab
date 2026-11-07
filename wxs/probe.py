"""多后端总调度：一次探测把各条通道的结果合并成一份可下载列表。

通道与可靠性
-----------
1. 本地缓存文件   —— 最可靠。小程序把视频落到 local/<appid>/temp/*.mp4，直接复制即可。
2. 内存扫描       —— 拿到带签名的 CDN 直链，可单独下载；签名有时效。
3. Chromium 缓存  —— 从缓存文件里捞 URL，属于补充。
4. CDP            —— 需要调试端口，默认没有；有则最准。
5. 图片（JPG/PNG）—— 不是独立来源，而是“从上面启用的来源里额外收图片”的开关；
                     打开时也会扫小程序 local 目录里的 jpg/png。

m3u8 自动展开
------------
HLS 的主清单（master playlist）里其实藏着多个清晰度。`expand_hls_masters=True` 时，
探测结束会把主清单展开成“每个清晰度一条”，这样“下载全部 m3u8”就能拿到所有码率的视频。

GUI 里每个通道可以单独开关，方便定位“到底是哪条路出问题”。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import cache_scan, cdp, hls, memory_scan, urls as urls_mod, windows
from .config import Settings
from .logsetup import human_size, log_timing
from .models import MediaItem
from .winapi import WindowInfo

log = logging.getLogger("wxs.probe")


@dataclass
class ProbeOptions:
    use_memory: bool = False
    use_cache_files: bool = False
    use_cache_urls: bool = True
    use_cdp: bool = False
    use_images: bool = True
    expand_hls: bool = True          # 主清单自动展开成所有清晰度
    cdp_seconds: float = 6.0
    include_segments: bool = False

    @classmethod
    def from_settings(cls, settings: Settings, **overrides) -> "ProbeOptions":
        opts = cls(
            use_memory=settings.use_memory,
            use_cache_files=settings.use_cache_files,
            use_cache_urls=settings.use_cache_urls,
            use_cdp=settings.use_cdp,
            use_images=settings.use_images,
            expand_hls=settings.hls_expand_masters,
        )
        for key, val in overrides.items():
            if hasattr(opts, key):
                setattr(opts, key, val)
        return opts


@dataclass
class ProbeResult:
    items: list[MediaItem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    processes: list[dict] = field(default_factory=list)
    memory_urls: int = 0
    cache_urls: int = 0
    cdp_urls: int = 0
    image_urls: int = 0

    @property
    def local_count(self) -> int:
        return sum(1 for i in self.items if i.kind == "local")

    @property
    def remote_count(self) -> int:
        return sum(1 for i in self.items if i.kind == "remote")

    @property
    def image_count(self) -> int:
        return sum(1 for i in self.items if i.is_image)

    @property
    def video_count(self) -> int:
        return sum(1 for i in self.items if not i.is_image)

    @property
    def hls_count(self) -> int:
        return sum(1 for i in self.items if i.is_hls and not i.is_image)

    def summary_text(self) -> str:
        total = sum(i.size or 0 for i in self.items if i.size)
        return (
            f"本地文件 {self.local_count} 个 / 远端链接 {self.remote_count} 个"
            f"（视频 {self.video_count}，其中 m3u8 {self.hls_count}；图片 {self.image_count}；"
            f"已知体积合计 {human_size(total)}）"
        )


StageCallback = Callable[[str, str], None]      # (stage, message)
ItemCallback = Callable[[MediaItem], None]


def _stage(cb: StageCallback | None, stage: str, message: str) -> None:
    log.info("[%s] %s", stage, message)
    if cb is not None:
        try:
            cb(stage, message)
        except Exception:  # noqa: BLE001
            pass


def _emit(cb: ItemCallback | None, item: MediaItem) -> None:
    if cb is None:
        return
    try:
        cb(item)
    except Exception:  # noqa: BLE001
        pass


def probe(window: WindowInfo | None, settings: Settings,
          options: ProbeOptions | None = None,
          cancel: threading.Event | None = None,
          on_stage: StageCallback | None = None,
          on_item: ItemCallback | None = None) -> ProbeResult:
    """执行全部启用的通道，返回合并结果。任何单通道失败都不影响其它通道。"""
    opts = options or ProbeOptions.from_settings(settings)
    result = ProbeResult()
    t_all = time.time()

    _stage(on_stage, "开始",
           f"目标窗口: {window.label() if window else '未选择（仅扫描本地缓存）'} | "
           f"通道: {'/'.join(_channel_names(opts))}")
    if window is not None:
        result.processes = windows.process_tree(window.pid, include_children=settings.scan_include_children)
        result.stats["processes"] = len(result.processes)
        result.stats["process_mem_mb"] = sum(p.get("mem_mb", 0) for p in result.processes)

    collected_urls: set[str] = set()
    url_sources: dict[str, str] = {}

    # ---------------------------------------------------------- 1. 内存扫描
    if opts.use_memory and window is not None:
        _stage(on_stage, "内存扫描", f"准备扫描 {len(result.processes)} 个进程的内存")
        try:
            with log_timing(log, "内存扫描 %d 个进程", len(result.processes)):
                mem_urls, scan_results = memory_scan.scan_targets(
                    result.processes, total_budget_mb=settings.scan_budget_mb, cancel=cancel,
                )
            result.memory_urls = len(mem_urls)
            result.stats["memory_scanned_mb"] = round(
                sum(r.bytes_read for r in scan_results) / 1048576, 1
            )
            for u in mem_urls:
                if u not in collected_urls:
                    collected_urls.add(u)
                    url_sources[u] = "内存扫描"
            _stage(on_stage, "内存扫描",
                   f"读到 {result.stats['memory_scanned_mb']} MB，原始 URL {len(mem_urls)} 条")
        except Exception as exc:  # noqa: BLE001
            log.exception("内存扫描失败: %s", exc)
            result.notes.append(f"内存扫描失败: {exc}")
    elif opts.use_memory:
        result.notes.append("未选择小程序窗口，跳过内存扫描。")

    # ---------------------------------------------------------- 2. 本地缓存文件
    if opts.use_cache_files:
        _stage(on_stage, "本地缓存", "扫描小程序 local 目录里的视频/图片文件")
        try:
            min_bytes = int(max(0.0, settings.min_local_mb) * 1024 * 1024)
            min_image = int(max(0, settings.min_image_kb) * 1024)
            with log_timing(log, "本地缓存文件扫描"):
                local_items = cache_scan.scan_applet_local(
                    min_bytes=min_bytes, cancel=cancel,
                    include_images=opts.use_images, min_image_bytes=min_image,
                )
            recency = cache_scan.applet_recent_activity()
            for it in local_items:
                it.extra["activity"] = recency.get(it.appid, 0)
            local_items.sort(key=lambda i: (-(i.extra.get("activity") or 0), -(i.mtime or 0)))
            result.items.extend(local_items)
            _stage(on_stage, "本地缓存", f"命中 {len(local_items)} 个文件"
                                        f"（图片 {sum(1 for i in local_items if i.is_image)}）")
            for it in local_items:
                _emit(on_item, it)
        except Exception as exc:  # noqa: BLE001
            log.exception("本地缓存扫描失败: %s", exc)
            result.notes.append(f"本地缓存扫描失败: {exc}")

    # ---------------------------------------------------------- 3. Chromium 缓存 URL
    if opts.use_cache_urls:
        _stage(on_stage, "缓存URL", "扫描 Chromium 缓存文件里的媒体/图片地址")
        try:
            with log_timing(log, "Chromium 缓存 URL 扫描"):
                cache_urls = cache_scan.scan_chromium_cache(
                    max_file_mb=settings.cache_url_file_mb,
                    max_total_mb=settings.cache_url_budget_mb,
                    cancel=cancel,
                    include_images=opts.use_images,
                )
            result.cache_urls = len(cache_urls)
            for u in cache_urls:
                if u not in collected_urls:
                    collected_urls.add(u)
                    url_sources[u] = "缓存URL"
            _stage(on_stage, "缓存URL", f"命中 {len(cache_urls)} 条地址")
        except Exception as exc:  # noqa: BLE001
            log.exception("缓存 URL 扫描失败: %s", exc)
            result.notes.append(f"缓存 URL 扫描失败: {exc}")

    # ---------------------------------------------------------- 4. CDP
    if opts.use_cdp:
        _stage(on_stage, "CDP", "探测 DevTools 调试端口")
        try:
            pids = [window.pid] if window else []
            endpoints = cdp.discover(settings, pids)
            if endpoints:
                with log_timing(log, "CDP 抓取 %d 个端点", len(endpoints)):
                    cdp_urls = cdp.sniff_all(endpoints, seconds=opts.cdp_seconds, cancel=cancel)
                result.cdp_urls = len(cdp_urls)
                for u in cdp_urls:
                    if u not in collected_urls:
                        collected_urls.add(u)
                        url_sources[u] = "CDP"
                _stage(on_stage, "CDP", f"抓到 {len(cdp_urls)} 条媒体地址")
            else:
                result.notes.append("CDP 通道不可用（微信默认不开放调试端口），已跳过。")
        except Exception as exc:  # noqa: BLE001
            log.exception("CDP 通道失败: %s", exc)
            result.notes.append(f"CDP 通道失败: {exc}")

    # ---------------------------------------------------------- 5. 合并 URL -> 条目
    url_items: list[MediaItem] = []
    if collected_urls:
        _stage(on_stage, "解析", f"共收集 {len(collected_urls)} 条原始 URL，开始分类去重")
        report = urls_mod.analyze(collected_urls, keep_segments=opts.include_segments,
                                 want_images=opts.use_images)
        result.stats.update(
            urls_total=report.all_urls,
            urls_media=len(report.media),
            urls_downloadable=len(report.downloadable),
            urls_expired=report.expired,
            urls_duplicates=report.duplicates,
            urls_noise=report.noise,
            urls_images=len(report.images),
        )
        result.image_urls = report.image_urls
        for idx, info in enumerate(report.downloadable, 1):
            item = MediaItem.from_url_info(info, source=url_sources.get(info.url, "未知"), index=idx)
            item.extra["activity"] = time.time()
            url_items.append(item)
        for idx, info in enumerate(report.downloadable_images, 1):
            item = MediaItem.from_url_info(info, source=url_sources.get(info.url, "未知"), index=idx)
            item.extra["activity"] = time.time()
            url_items.append(item)
        if report.downloadable_images:
            _stage(on_stage, "解析", f"其中图片 {len(report.downloadable_images)} 张（JPG/PNG）")
        if not report.downloadable and not report.downloadable_images:
            result.notes.append(
                "抓到 URL 但没有可下载的媒体地址。可能原因：视频还没点开播放（先播放几秒再扫描）、"
                "视频是加密流、或只在 canvas/WebGL 里渲染。"
            )
        if report.expired:
            result.notes.append(f"有 {report.expired} 条地址签名已过期，下载会失败，请重新播放后再扫描。")
        if report.image_urls and not opts.use_images:
            result.notes.append(f"有 {report.image_urls} 条图片地址被忽略（未勾选“图片”）。")
    else:
        result.stats.update(urls_total=0, urls_media=0, urls_downloadable=0)
        if opts.use_images and not (opts.use_memory or opts.use_cache_urls or opts.use_cdp):
            result.notes.append(
                "“图片”目前只能收到本地缓存目录里的 jpg/png；"
                "想收网络图片请再启用 内存扫描 / 缓存URL / CDP 之一。")

    # ---------------------------------------------------------- 6. m3u8 主清单展开
    if opts.expand_hls and url_items:
        try:
            _stage(on_stage, "m3u8", "检查是否有 HLS 主清单，自动展开所有清晰度")
            new_items, replaced, notes = hls.expand_hls_items(
                url_items, settings, cancel=cancel, limit=12,
            )
            for n in notes:
                log.info("m3u8: %s", n)
            if replaced:
                drop = {id(it) for it in replaced}
                url_items = [it for it in url_items if id(it) not in drop]
                url_items.extend(new_items)
                result.notes.extend(notes)
                result.stats["hls_variants"] = len(new_items)
                _stage(on_stage, "m3u8", f"展开出 {len(new_items)} 个清晰度")
        except Exception as exc:  # noqa: BLE001
            log.exception("m3u8 展开失败: %s", exc)
            result.notes.append(f"m3u8 展开失败: {exc}")

    for item in url_items:
        result.items.append(item)
        _emit(on_item, item)

    # ---------------------------------------------------------- 排序 & 收尾
    result.items.sort(key=_sort_key)

    elapsed = time.time() - t_all
    result.stats["elapsed"] = round(elapsed, 2)
    _stage(on_stage, "完成", f"探测结束，用时 {elapsed:.1f}s：{result.summary_text()}")

    log.info("=" * 90)
    log.info("探测汇总: %s", result.summary_text())
    for k, v in result.stats.items():
        log.info("   %-22s = %s", k, v)
    for n in result.notes:
        log.warning("   注意: %s", n)
    log.info("=" * 90)
    return result


def _sort_key(item: MediaItem) -> tuple:
    """本地文件 > 视频 > 图片；同类按分数/时间倒序。"""
    kind_rank = 0 if item.kind == "local" else 1
    type_rank = 0 if not item.is_image else 1
    return (kind_rank, type_rank, -item.score, -(item.mtime or 0))


def _channel_names(opts: ProbeOptions) -> list[str]:
    names = []
    if opts.use_memory:
        names.append("内存扫描")
    if opts.use_cache_files:
        names.append("本地缓存")
    if opts.use_cache_urls:
        names.append("缓存URL")
    if opts.use_cdp:
        names.append("CDP")
    if opts.use_images:
        names.append("图片")
    return names or ["无"]


def copy_items_preview(items: list[MediaItem], limit: int = 5) -> str:
    """把结果拼成一段可读文本（用于日志/GUI 详情面板）。"""
    lines = []
    for it in items[:limit]:
        lines.append(f"{it.kind_label} | {it.title} | {it.size_label} | {it.source}")
        lines.append(f"    {it.url if it.kind == 'remote' else it.path}")
    if len(items) > limit:
        lines.append(f"... 共 {len(items)} 条")
    return "\n".join(lines)


__all__ = ["probe", "ProbeOptions", "ProbeResult", "copy_items_preview"]
