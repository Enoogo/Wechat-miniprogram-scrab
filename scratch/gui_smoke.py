"""GUI 冒烟测试：真的把界面建起来、跑一次探测和下载、并截图核对排版。

这不是 pytest，是一个可反复运行的脚本：
    python scratch/gui_smoke.py                 # 跑完自动删掉截图
    python scratch/gui_smoke.py --keep-shots    # 保留截图供人工查看（看完记得删！）

除了功能链路，它还专门断言了这一版改过的排版：
* ② 里 内存预算/下载并发/CDP端口 三个输入框在复选框**右侧**（x 坐标比复选框大）
* ② 只有一个“开始探测/取消”按钮，进度条在它右边
* ④ 的“自动滚动”在高 DPI + 最小窗口宽度下**完整显示**（宽度 >= 需要宽度）
* 右上角没有“打开日志目录”按钮
"""
from __future__ import annotations

import ctypes
import io
import os
import shutil
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

KEEP_SHOTS = "--keep-shots" in sys.argv
SHOT_DIR = ROOT / "scratch" / "shots"

from wxs import logsetup, winapi  # noqa: E402

log_file = logsetup.setup(level="DEBUG", console_level="WARNING", gui=True)
winapi.set_dpi_awareness()

from wxs.gui import App  # noqa: E402


def pump(root: tk.Tk, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def wait_idle(root: tk.Tk, app: App, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        root.update()
        if not app.busy:
            root.update()
            return True
        time.sleep(0.05)
    return False


def hwnd_of(widget: tk.Misc) -> int:
    hwnd = widget.winfo_id()
    user32 = ctypes.windll.user32
    while True:
        parent = user32.GetParent(hwnd)
        if not parent:
            return hwnd
        hwnd = parent


def shot(root: tk.Tk, name: str) -> str:
    """用 PrintWindow 抓自己的窗口内容（不受窗口位置/遮挡影响）。"""
    from wxs import capture

    hwnd = hwnd_of(root)
    info = winapi.read_window(hwnd)
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    out = SHOT_DIR / f"{name}.png"
    try:
        path = capture.grab_window_png(info, out, max_width=1700)
    except Exception as exc:  # noqa: BLE001
        print(f"  [截图失败] {exc} hwnd={hex(hwnd)}")
        return ""
    print(f"  [截图] {path}")
    return path


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"  {'✔' if ok else '✘'} {name}" + (f" — {detail}" if detail else ""))

    root = tk.Tk()
    app = App(root)
    app.settings.out_dir = ROOT / "scratch" / "dl_test"
    app.var_out.set(str(app.settings.out_dir))
    pump(root, 0.5)
    root.deiconify()
    root.lift()
    root.attributes("-topmost", True)
    pump(root, 0.5)
    root.attributes("-topmost", False)

    print("[1] 启动与窗口检测")
    ok = wait_idle(root, app, 30)
    check("窗口检测完成", ok and app.detect_result is not None,
          f"候选={app.detect_result.count if app.detect_result else '?'}")
    rows = app.tree_win.get_children()
    check("窗口列表已填充", len(rows) > 0, f"{len(rows)} 行")
    check("当前窗口已选中", app.current_window is not None,
          f"{app.current_window.title if app.current_window else '无'}")
    pump(root, 0.8)
    shot(root, "1_windows")

    print("[2] 排版断言（这一版改动的重点）")
    pump(root, 0.3)
    # 三个输入框必须在复选框右侧（opt 里第 0 列是复选框，第 1/2 列是标签和输入框）
    children = list(app.opt_frame.winfo_children())
    label_xs = [w.winfo_rootx() for w in children
                if isinstance(w, ttk.Label) and w.cget("text") in ("内存预算(MB)", "下载并发", "CDP 端口")]
    chk_xs = [w.winfo_rootx() for w in children if isinstance(w, ttk.Checkbutton)]
    spin_x = app.spin_budget.winfo_rootx()
    conc_x = app.spin_conc.winfo_rootx()
    port_x = app.entry_cdp_port.winfo_rootx()
    check("三个数值框在复选框右侧",
          bool(label_xs) and bool(chk_xs) and min(label_xs) > max(chk_xs),
          f"复选框最左 x={min(chk_xs) if chk_xs else '-'} 标签最左 x={min(label_xs) if label_xs else '-'}")
    check("三个数值框左边对齐", len({spin_x, conc_x, port_x}) == 1, f"{spin_x}/{conc_x}/{port_x}")

    # 合并后的动作按钮 + 右侧进度条
    check("只有一个动作按钮", app.btn_action.winfo_ismapped() and not hasattr(app, "btn_cancel"),
          f"按钮文本={app.btn_action.cget('text')}")
    btn_right = app.btn_action.winfo_rootx() + app.btn_action.winfo_width()
    check("进度条在按钮右边", app.progress.winfo_rootx() >= btn_right,
          f"按钮右={btn_right} 进度条左={app.progress.winfo_rootx()}")
    need = app.text_width("开始探测")
    check("按钮宽度只够 4 个字", need <= app.btn_action.winfo_width() <= app.px(160),
          f"按钮宽={app.btn_action.winfo_width()} 文字需={need} 上限={app.px(160)}")

    # ④ 自动滚动：把窗口压到最小宽度再检查
    root.minsize(app.px(1260), app.px(640))
    root.geometry(f"{app.px(1260)}x{app.px(900)}")
    pump(root, 0.6)
    chk = app.chk_autoscroll
    check("自动滚动完整显示（最小窗口宽度）",
          chk.winfo_width() >= chk.winfo_reqwidth(),
          f"实际宽={chk.winfo_width()} 需要宽={chk.winfo_reqwidth()}")
    check("③ 工具条没被裁掉（最小窗口宽度）",
          app.vbar.winfo_width() >= app.vbar.winfo_reqwidth(),
          f"实际宽={app.vbar.winfo_width()} 需要宽={app.vbar.winfo_reqwidth()}")
    shot(root, "2b_minwidth")
    # 还原窗口大小方便看整屏
    root.geometry(f"{app.px(1500)}x{app.px(950)}")
    pump(root, 0.6)
    header_texts = [w.cget("text") for w in app.header_btns.winfo_children()]
    check("右上角无“打开日志目录”按钮", "打开日志目录" not in header_texts, f"{header_texts}")
    check("③ 有“全部m3u8”按钮", app.btn_dl_hls.winfo_ismapped(), app.btn_dl_hls.cget("text"))
    check("③ 全选是勾选框", app.chk_select_all.winfo_class() == "TCheckbutton",
          app.chk_select_all.winfo_class())
    check("✓ 表头已替换", app.tree_items.heading("sel")["text"] == "✓",
          f"表头={app.tree_items.heading('sel')['text']}")
    shot(root, "2_layout")

    print("[3] 探测（只开本地缓存，快）")
    app.var_mem.set(False)
    app.var_cache_urls.set(False)
    app.var_cdp.set(False)
    app.var_images.set(True)
    app.var_cache_files.set(True)
    app.start_probe()
    check("探测时按钮变成“取消”", app.btn_action.cget("text") == "取消",
          f"文本={app.btn_action.cget('text')} busy={app.busy}")
    ok = wait_idle(root, app, 120)
    check("探测流程结束", ok, f"busy={app.busy}")
    check("探测后按钮变回“开始探测”", app.btn_action.cget("text") == "开始探测",
          f"文本={app.btn_action.cget('text')}")
    check("列表里有条目", len(app.items) > 0, f"{len(app.items)} 条")
    if app.items:
        first = next(iter(app.items.values()))
        app.tree_items.selection_set(first.uid)
        app._on_item_select()
        pump(root, 0.6)
        detail = app.txt_detail.get("1.0", "end").strip()
        check("详情面板有内容", len(detail) > 30, f"{len(detail)} 字符")
        check("勾选标记是 ✓", first.row()[0] in ("✓", " "), f"标记={first.row()[0]!r}")
    # 全选勾选框：勾上 -> 全选；再取消 -> 全不选
    app.var_select_all.set(True)
    app._on_select_all()
    all_on = all(i.selected for i in app.items.values())
    app.var_select_all.set(False)
    app._on_select_all()
    all_off = all(not i.selected for i in app.items.values())
    check("全选勾选框生效", all_on and all_off, f"全选={all_on} 全不选={all_off}")
    shot(root, "3_probe")

    print("[4] 下载（只下第一个，避免浪费时间）")
    if app.items:
        # 优先挑视频（图片可能小到被 min_image_kb 过滤掉，测不出下载链路）
        pool = [i for i in app.items.values() if not i.is_image] or list(app.items.values())
        target = max(pool, key=lambda i: (i.size or 0))
        for it in app.items.values():
            it.selected = (it.uid == target.uid)
        app._update_item_count()
        # 先清空输出目录，避免“已存在”掩盖真实下载
        shutil.rmtree(app.settings.out_dir, ignore_errors=True)
        app.settings.out_dir.mkdir(parents=True, exist_ok=True)
        print(f"  目标: {target.kind_label} {target.title} {target.size_label}")
        before = set(os.listdir(app.settings.out_dir))
        app.start_download(only_selected=True)
        ok = wait_idle(root, app, 180)
        after = set(os.listdir(app.settings.out_dir))
        new_files = after - before
        sizes = [app.settings.out_dir / f for f in new_files]
        total = sum(p.stat().st_size for p in sizes if p.exists())
        check("下载流程结束", ok, f"busy={app.busy}")
        check("产生了下载文件", bool(new_files), f"{new_files} 共 {total/1048576:.1f} MB")
        st = app.download_states.get(target.uid)
        check("下载状态成功", bool(st and st.ok), f"{st.status if st else '无状态'} "
                                                 f"告警={st.warn if st else '-'}")
        shot(root, "4_download")

    print("[5] 日志面板")
    log_text = app.txt_log.get("1.0", "end")
    check("日志面板有内容", len(log_text) > 500, f"{len(log_text)} 字符，{app.log_records} 条记录")
    check("状态栏已更新", app.var_stage.get() != "就绪", app.var_stage.get()[:60])
    lf = logsetup.log_file_path()
    check("日志文件存在且非空", bool(lf and lf.exists() and lf.stat().st_size > 2000),
          f"{lf} {lf.stat().st_size if lf and lf.exists() else 0} 字节")
    shot(root, "5_final")
    pump(root, 0.3)
    root.destroy()

    fails = [r for r in results if not r[1]]
    print("\n" + "=" * 60)
    print(f"GUI 冒烟测试: {len(results)} 项，失败 {len(fails)}")
    for name, _ok, detail in fails:
        print(f"  ✘ {name} {detail}")
    print("=" * 60)

    # 测试产物清理：截图默认删掉，避免 scratch 越攒越多
    if KEEP_SHOTS:
        print(f"[i] 截图保留在 {SHOT_DIR}（看完记得删）")
    else:
        shutil.rmtree(SHOT_DIR, ignore_errors=True)
        print(f"[i] 截图已清理: {SHOT_DIR}")
    shutil.rmtree(ROOT / "scratch" / "dl_test", ignore_errors=True)
    print(f"[i] 下载产物已清理: {ROOT / 'scratch' / 'dl_test'}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
