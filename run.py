#!/usr/bin/env python
"""入口：GUI（默认）或命令行模式。

命令行模式是为了两件事：
1) 让不擅长点鼠标的场景（批量/自动化/远程）也能用；
2) 让下一个接手的人（或 AI）能在**不改代码**的前提下验证整条链路。

示例
----
    python run.py                              # 打开 GUI
    python run.py --list-windows                # 只列窗口，不启动界面
    python run.py --probe                       # 探测（通道默认值来自 config.json）
    python run.py --probe --hwnd 0x28d14e4 --channels cache,mem
    python run.py --probe --download all --out D:\\videos
    python run.py --probe --channels cacheurl,images --json result.json
    python run.py --selftest                    # 自检（含本地 HTTP/CDP 假服务）

注：窗口录制功能已删除（用系统录屏软件即可）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import threading

# 让 `python run.py` 在任意 cwd 下都能 import wxs
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _parse_hwnd(value: str) -> int:
    v = value.strip().lower()
    return int(v, 16) if v.startswith("0x") else int(v, 0)


def _find_window(settings, hwnd: int | None, pid: int | None):
    from wxs import windows

    result = windows.detect()
    if result.count == 0:
        print("[!] 没有检测到微信小程序窗口。请先在微信里打开小程序。")
        return None, result
    if hwnd:
        for cand in result.candidates + result.all_chromium:
            if cand.hwnd == hwnd:
                return cand.info, result
        print(f"[!] 指定的 hwnd={hex(hwnd)} 不在检测结果里，改用第一个候选")
    if pid:
        for cand in result.candidates + result.all_chromium:
            if cand.pid == pid:
                return cand.info, result
        print(f"[!] 指定的 pid={pid} 不在检测结果里，改用第一个候选")
    return result.candidates[0].info, result


def cmd_list_windows(args) -> int:
    from wxs import windows

    result = windows.detect()
    print(f"\n检测到小程序窗口候选: {result.count} 个\n")
    headers = ("#", "标题", "HWND", "PID", "进程", "窗口类", "尺寸", "置信度")
    rows = []
    for i, c in enumerate(result.candidates, 1):
        info = c.info
        rows.append((i, (info.title or "")[:28], hex(info.hwnd), info.pid,
                     os.path.basename(info.exe), info.class_name,
                     f"{info.size[0]}x{info.size[1]}", c.score))
    _print_table(headers, rows)
    for c in result.candidates:
        print(f"  依据: {c.title} -> {' / '.join(c.reasons)}")
    if args.show_all and result.all_chromium:
        print("\n全部 Chromium 窗口（含 QQ/浏览器，仅供排查）:")
        rows = []
        for i, c in enumerate(result.all_chromium, 1):
            info = c.info
            rows.append((i, (info.title or "")[:28], hex(info.hwnd), info.pid,
                         os.path.basename(info.exe), info.class_name,
                         f"{info.size[0]}x{info.size[1]}", c.score))
        _print_table(headers, rows)
    for n in result.notes:
        print(f"  提示: {n}")
    return 0 if result.count else 2


def _print_table(headers, rows) -> None:
    widths = [len(str(h)) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)))


def _parse_channels(value: str | None, settings) -> set[str]:
    """把 --channels 解析成集合；不传（或 all）时用 config.json 里的默认通道。"""
    if not value or value.strip().lower() in ("all", "default"):
        chans = set()
        if settings.use_memory:
            chans.add("mem")
        if settings.use_cache_files:
            chans.add("cache")
        if settings.use_cache_urls:
            chans.add("cacheurl")
        if settings.use_cdp:
            chans.add("cdp")
        if settings.use_images:
            chans.add("images")
        return chans
    return {c.strip().lower() for c in value.split(",") if c.strip()}


def cmd_probe(args) -> int:
    from pathlib import Path

    from wxs import config, logsetup, probe, winapi

    settings = config.load_settings()
    if args.budget:
        settings.scan_budget_mb = args.budget
    if args.concurrency:
        settings.concurrency = args.concurrency
    if args.out:
        settings.out_dir = Path(args.out)
    settings.out_dir.mkdir(parents=True, exist_ok=True)
    if args.cdp_port is not None:
        settings.cdp_port_hint = args.cdp_port or ""
    if args.ffmpeg:
        settings.ffmpeg_path = args.ffmpeg

    winapi.set_dpi_awareness()
    winapi.enable_debug_privilege()

    window, _result = _find_window(settings, _parse_hwnd(args.hwnd) if args.hwnd else None, args.pid)
    chans = _parse_channels(args.channels, settings)
    if window is None and "cache" not in chans:
        print("[!] 未选择窗口。若只想扫本地缓存，请加 --channels cache")
        return 2
    if window is not None:
        print(f"[i] 使用窗口: {window.title!r} hwnd={hex(window.hwnd)} pid={window.pid} "
              f"class={window.class_name} size={window.size[0]}x{window.size[1]}")

    opts = probe.ProbeOptions(
        use_memory="mem" in chans,
        use_cache_files="cache" in chans,
        use_cache_urls="cacheurl" in chans,
        use_cdp="cdp" in chans,
        use_images="images" in chans,
        expand_hls=settings.hls_expand_masters,
    )
    size_on = opts.use_cache_files or opts.use_memory or opts.use_cache_urls or opts.use_cdp or opts.use_images
    if not size_on:
        print("[!] 没有启用的通道，--channels all 可全开")
        return 2
    print(f"[i] 通道: {sorted(chans)}  内存预算: {settings.scan_budget_mb}MB  "
          f"并发: {settings.concurrency}  图片: {'开' if opts.use_images else '关'}")

    cancel = threading.Event()
    t0 = time.time()
    result = probe.probe(window, settings, opts, cancel=cancel)
    elapsed = time.time() - t0

    print(f"\n探测完成，用时 {elapsed:.1f}s")
    print(f"  {result.summary_text()}")
    for k, v in result.stats.items():
        print(f"    {k} = {v}")
    headers = ("#", "类型", "来源", "名称", "大小", "状态", "地址/路径")
    rows = []
    for i, it in enumerate(result.items, 1):
        rows.append((i, it.kind_label, it.source, it.title[:40], it.size_label,
                     "已过期" if it.expired else "OK",
                     (it.url or it.path)[:90]))
    if rows:
        _print_table(headers, rows)
    for n in result.notes:
        print(f"  注意: {n}")

    if args.json:
        payload = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "window": None if window is None else {
                "hwnd": hex(window.hwnd), "pid": window.pid, "title": window.title,
                "class": window.class_name, "exe": window.exe,
            },
            "stats": result.stats,
            "items": [
                {"idx": i, "kind": it.kind, "media_type": it.media_type, "source": it.source,
                 "title": it.title, "url": it.url, "path": it.path, "size": it.size,
                 "score": it.score, "expired": it.expired, "detail": it.detail}
                for i, it in enumerate(result.items, 1)
            ],
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"\n[i] JSON 结果已写入 {args.json}")

    if args.download and result.items:
        from wxs import downloader

        low = args.download.lower()
        if low == "all":
            picked = list(result.items)
        elif low in ("m3u8", "hls"):
            picked = [it for it in result.items if it.is_hls and not it.is_image]
            print(f"[i] --download {low}: 选中 {len(picked)} 条 m3u8")
        else:
            idxs = {int(x) - 1 for x in args.download.replace(",", " ").split() if x.strip().isdigit()}
            picked = [it for i, it in enumerate(result.items) if i in idxs]
        if not picked:
            print("[!] --download 没有选中任何条目")
            return 3
        print(f"\n开始下载 {len(picked)} 个条目 -> {settings.out_dir}")
        dl = downloader.Downloader(settings)
        states = dl.run(picked)
        ok = sum(1 for s in states.values() if s.ok)
        print(f"下载结束: 成功 {ok}/{len(states)}")
        for _uid, st in states.items():
            if st.ok:
                print(f"   ✔ {st.out_path}" + (f"  ⚠ {st.warn}" if st.warn else ""))
            else:
                print(f"   ✘ {st.status}: {st.error}")
        return 0 if ok == len(states) else 4

    return 0 if result.items else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="微信小程序窗口检测 + 视频/图片解析下载工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--gui", action="store_true", help="启动图形界面（默认行为）")
    parser.add_argument("--list-windows", action="store_true", help="列出检测到的窗口后退出")
    parser.add_argument("--probe", action="store_true", help="命令行探测（不开界面）")
    parser.add_argument("--selftest", action="store_true", help="运行自检")
    parser.add_argument("--hwnd", help="指定窗口句柄，如 0x28d14e4")
    parser.add_argument("--pid", type=int, help="指定进程 PID")
    parser.add_argument("--channels", default=None,
                        help="探测通道，逗号分隔: mem,cache,cacheurl,cdp,images；"
                             "缺省用 config.json 的 channels")
    parser.add_argument("--budget", type=int, default=0, help="内存扫描预算 MB")
    parser.add_argument("--concurrency", type=int, default=0, help="下载并发（缺省用 config.json）")
    parser.add_argument("--out", help="下载目录")
    parser.add_argument("--ffmpeg", help="ffmpeg 可执行文件路径")
    parser.add_argument("--json", help="把探测结果写入该 JSON 文件")
    parser.add_argument("--download",
                        help="下载参数: all / m3u8 / 序号逗号分隔（如 1,3）")
    parser.add_argument("--cdp-port", help="指定 CDP 调试端口")
    parser.add_argument("--show-all", action="store_true", help="--list-windows 时显示全部 Chromium 窗口")
    parser.add_argument("--log-level", default="DEBUG", help="文件日志级别")
    parser.add_argument("--quiet", action="store_true", help="控制台只输出 WARNING 以上")

    args = parser.parse_args(argv)

    from wxs import logsetup

    if args.selftest:
        from wxs.selftest import run_selftest

        logsetup.setup(level="DEBUG", console_level="INFO", gui=False)
        return run_selftest(args)

    headless = args.list_windows or args.probe
    logsetup.setup(level=args.log_level, console_level="WARNING" if args.quiet else "INFO", gui=not headless)

    if args.list_windows:
        return cmd_list_windows(args)
    if args.probe:
        return cmd_probe(args)

    # 默认：GUI
    from wxs import gui

    return gui.run()


if __name__ == "__main__":
    sys.exit(main())
