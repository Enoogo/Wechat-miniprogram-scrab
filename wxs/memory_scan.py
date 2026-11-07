"""进程内存扫描：从正在运行的微信小程序里把媒体 URL 捞出来。

原理
----
小程序由 WMPF（Chromium）渲染，页面里的 `<video src>`、网络请求的 URL、
缓存索引都作为字符串活在这些进程的堆里。只要以 PROCESS_VM_READ 打开进程，
用 VirtualQueryEx 遍历已提交内存、ReadProcessMemory 分块读、正则匹配即可拿到 URL。
不需要 Frida/注入/管理员（同用户进程），这是本工具的主通道。

实测（本机 WeChatAppEx 根进程，400MB 预算）：
    6572 条不同 URL，其中包含真实可下载的
    https://wxsmw.wxs.qq.com/.../xxx.mp4?dis_k=..&dis_t=..&sha256=..
    http://wxsnsdythumb.wxs.qq.com/...snsvideodownload?m=..&filekey=..

坑
---
1. 必须扫 renderer 与 network service 子进程，URL 主要活在它们那里。
2. 内存里的字符串是“脏”的：URL 会粘连、会被截断、有 UTF-16LE 版本。
   清洗交给 urls.py，这里只负责尽量完整地取出字节。
3. 区块边界会把 URL 切断，所以分块读取要留 overlap。
4. PAGE_GUARD / PAGE_NOACCESS 区块读会失败并触发异常，必须跳过。
5. 32 位进程也用同一套 64 位 VirtualQueryEx 结构体即可（WOW64 地址空间同样可见）。
6. 预算必须可中断：大内存进程一次扫几百 MB，GUI 需要取消按钮。
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from dataclasses import dataclass, field

from . import urls as urls_mod
from . import winapi
from .logsetup import human_size, win32_error_text

log = logging.getLogger("wxs.mem")

CHUNK = 4 * 1024 * 1024
OVERLAP = 8 * 1024
MAX_REGION = 1024 * 1024 * 1024  # 单区块上限 1GB，超过分块处理
PROBE = 256 * 1024               # 全零探测长度
HOT_MAX = 16 * 1024 * 1024       # 小于此体积的私有/映射区块优先扫（堆大多在这里）
TYPE_NAMES = {0x1000000: "Image", 0x40000: "Mapped", 0x20000: "Private", 0: "-"}


@dataclass
class Region:
    base: int
    size: int
    type_name: str
    protect: int
    hot: bool = False


def enumerate_regions(handle) -> list[Region]:
    """先只遍历区块元数据（不读内容），用于排序与统计。"""
    k32 = winapi.kernel32
    mbi = winapi.MEMORY_BASIC_INFORMATION()
    regions: list[Region] = []
    addr = 0
    while addr < 0x7FFFFFFEFFFF:
        written = k32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
        if not written:
            err = ctypes.get_last_error()
            if err not in (0, 87):
                log.debug("VirtualQueryEx 提前结束 addr=%#x %s", addr, win32_error_text(err))
            break
        base = int(mbi.BaseAddress)
        size = int(mbi.RegionSize)
        if size <= 0:
            break
        readable = (
            mbi.State == winapi.MEM_COMMIT
            and (mbi.Protect & 0xFF) in winapi.READABLE_PROTECT
            and not (mbi.Protect & winapi.PAGE_GUARD)
            and (mbi.Protect & winapi.PAGE_NOACCESS) == 0
        )
        if readable:
            tname = TYPE_NAMES.get(int(mbi.Type), hex(int(mbi.Type)))
            # 堆/对象池几乎都在小体积私有或映射区块里；大的通常是 mmap 文件/保留区
            hot = size <= HOT_MAX and tname in ("Private", "Mapped")
            regions.append(Region(base, size, tname, int(mbi.Protect), hot))
        addr = base + size
        if addr <= base:
            break
    return regions


@dataclass
class ProcessScanResult:
    pid: int
    name: str
    ptype: str
    regions_total: int = 0
    regions_read: int = 0
    bytes_read: int = 0
    read_failures: int = 0
    urls: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    error: str = ""

    def summary(self) -> str:
        return (
            f"pid={self.pid}({self.ptype}) 区块 {self.regions_read}/{self.regions_total} "
            f"读取 {human_size(self.bytes_read)} 失败 {self.read_failures} "
            f"URL {len(self.urls)} 耗时 {self.elapsed:.2f}s"
            + (f" 错误={self.error}" if self.error else "")
        )


def _read_region_chunks(h, base: int, size: int, budget_left: int, sink: list[str],
                        result: ProcessScanResult, cancel: threading.Event | None) -> int:
    """分块读取一个内存区域，把 URL 收进 sink。返回实际读取字节数。

    * 小区块一次读完，不切块（否则 overlap 逻辑会把小区块反复读）。
    * 大区块按 CHUNK 切，块间留 OVERLAP 防止 URL 被切断。
    * 大于 1MB 的区块先读 256KB 探测：全零说明是保留/占位区，直接跳过，
      能省下大量无用的读取预算（实测大块零区很常见）。
    """
    k32 = winapi.kernel32
    read = ctypes.c_size_t(0)

    def _read_at(addr: int, length: int) -> bytes:
        buf = ctypes.create_string_buffer(length)
        if not k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, length, ctypes.byref(read)):
            result.read_failures += 1
            if result.read_failures <= 3:
                log.debug("ReadProcessMemory 失败 pid=%s addr=%#x size=%d %s",
                          result.pid, addr, length, win32_error_text(ctypes.get_last_error()))
            return b""
        return buf.raw[: read.value]

    if size <= CHUNK:
        if cancel is not None and cancel.is_set():
            return 0
        data = _read_at(base, size)
        if data:
            sink.extend(urls_mod.iter_urls_from_bytes(data, include_utf16=True))
        return len(data)

    consumed = 0
    first = True
    offset = 0
    while offset < size and consumed < budget_left:
        if cancel is not None and cancel.is_set():
            return consumed
        this = min(CHUNK, size - offset)
        data = _read_at(base + offset, this)
        if not data:
            offset += max(1, this - OVERLAP)
            continue
        if first:
            first = False
            head = data[:PROBE]
            if size > 1024 * 1024 and head and head.count(0) == len(head):
                log.debug("跳过全零大区块 base=%#x size=%s", base, human_size(size))
                return consumed
        consumed += len(data)
        sink.extend(urls_mod.iter_urls_from_bytes(data, include_utf16=True))
        offset += max(1, this - OVERLAP)
    return consumed


def scan_process(pid: int, *, budget_bytes: int = 400 * 1024 * 1024,
                 cancel: threading.Event | None = None,
                 name: str = "", ptype: str = "",
                 progress=None) -> ProcessScanResult:
    """扫描单个进程的内存，返回找到的原始 URL 列表。

    扫描顺序很关键：先扫「小体积私有/映射区块」（堆、对象池，URL 密度最高），
    再扫剩下的。早期版本按地址顺序扫，结果预算全被几个几十 MB 的大区块吃掉，
    URL 一条都没捞到 —— 这个坑必须留着注释。
    """
    k32 = winapi.kernel32
    result = ProcessScanResult(pid=pid, name=name, ptype=ptype)
    t0 = time.perf_counter()

    handle = k32.OpenProcess(
        winapi.PROCESS_VM_READ | winapi.PROCESS_QUERY_INFORMATION, False, pid
    )
    if not handle:
        err = ctypes.get_last_error()
        result.error = f"OpenProcess失败 {win32_error_text(err)}"
        result.elapsed = time.perf_counter() - t0
        log.warning("无法打开进程 pid=%s: %s（可能需要管理员权限，或进程已退出）", pid, result.error)
        return result

    urls_sink: list[str] = []
    consumed_total = 0
    try:
        regions = enumerate_regions(handle)
        result.regions_total = len(regions)
        hot = [r for r in regions if r.hot]
        cold = [r for r in regions if not r.hot]
        type_hist: dict[str, int] = {}
        for r in regions:
            type_hist[r.type_name] = type_hist.get(r.type_name, 0) + 1
        total_avail = sum(r.size for r in regions)
        log.info(
            "pid=%s(%s) 可读区块 %d 个 共 %s：热区 %d 个(%s) 冷区 %d 个 | 类型=%s",
            pid, ptype or "?", len(regions), human_size(total_avail),
            len(hot), human_size(sum(r.size for r in hot)), len(cold), type_hist,
        )

        for region in hot + cold:
            if consumed_total >= budget_bytes:
                log.info("pid=%s 预算用完（%s），停止", pid, human_size(consumed_total))
                break
            if cancel is not None and cancel.is_set():
                log.info("内存扫描被用户取消 pid=%s", pid)
                break
            remaining = budget_bytes - consumed_total
            got = _read_region_chunks(
                handle, region.base, min(region.size, MAX_REGION), remaining,
                urls_sink, result, cancel,
            )
            if got:
                consumed_total += got
                result.bytes_read += got
                result.regions_read += 1
                if progress is not None:
                    try:
                        progress(result, consumed_total, budget_bytes)
                    except Exception:  # noqa: BLE001
                        pass
    finally:
        k32.CloseHandle(handle)

    result.urls = urls_sink
    result.elapsed = time.perf_counter() - t0
    log.info("内存扫描 %s", result.summary())
    return result


def scan_targets(targets: list[dict], *, total_budget_mb: int = 900,
                 cancel: threading.Event | None = None,
                 progress=None) -> tuple[set[str], list[ProcessScanResult]]:
    """按优先级扫描一组进程，预算均摊，避免第一个进程独吞预算。

    优先级：network service（网络请求 URL 最多）> renderer（页面/媒体元素）
    > browser（主进程也有）> gpu/其它。
    """
    priority = {"network.mojom.NetworkService": 0, "renderer": 1, "browser": 2, "gpu-process": 3}
    ordered = sorted(targets, key=lambda t: priority.get(t.get("type", ""), 9))

    total_budget = max(16, int(total_budget_mb)) * 1024 * 1024
    # 每个进程至少 40MB，其余按进程数均摊 —— 单进程独吞会导致后面的进程一条都没扫
    per_process = max(40 * 1024 * 1024, total_budget // max(1, len(ordered)))
    all_urls: set[str] = set()
    results: list[ProcessScanResult] = []
    spent = 0

    log.info("开始内存扫描: 目标 %d 个进程, 总预算 %s, 单进程上限 %s",
             len(ordered), human_size(total_budget), human_size(per_process))
    for t in ordered:
        if cancel is not None and cancel.is_set():
            log.info("内存扫描整体取消")
            break
        left = total_budget - spent
        if left <= 8 * 1024 * 1024:
            log.info("预算用完（已读 %s），停止扫描剩余 %d 个进程", human_size(spent),
                     len(ordered) - len(results))
            break
        res = scan_process(
            int(t["pid"]), budget_bytes=min(per_process, left), cancel=cancel,
            name=str(t.get("name", "")), ptype=str(t.get("type", "")), progress=progress,
        )
        spent += res.bytes_read
        results.append(res)
        all_urls.update(res.urls)

    log.info(
        "内存扫描完成: 进程 %d 个, 读取 %s, 原始 URL 字符串 %d 条",
        len(results), human_size(spent), len(all_urls),
    )
    return all_urls, results


__all__ = ["scan_process", "scan_targets", "ProcessScanResult"]
