"""tkinter 图形界面。

线程模型
--------
* Tk 只在主线程被访问。
* 所有耗时操作（窗口检测、探测、下载）在后台线程跑，通过 `self.ui_queue`
  把结构化事件丢回主线程，主线程每 100ms 轮询一次。
* 日志走 logging -> GuiQueueHandler -> GUI_BRIDGE，同样在主线程轮询时刷进 Text。
* 所有回调都做了异常保护：界面代码抛异常不能把工作线程带崩。

布局要点（这一版专门为“给详情模块腾纵向高度”重排过）
----------------------------------------------------
* ② 探测选项：左列是 5 个通道复选框，右侧同一行放 内存预算 / 下载并发 / CDP 端口，
  省掉原来“复选框区块 + 三个输入行”的 3 行高度。
* ② 底部：“开始探测 / 取消”合并成**一个按钮**（宽度只够 4 个字），进度条放在它右边，
  又省掉 1 行。
* ① 顶部右上角只保留「环境自检」「打开下载目录」（“打开日志目录”已删除，
  功能与 ④ 的「打开日志文件」重复）。
* ④ 日志工具条用 grid + minsize，保证“自动滚动”四个字完整显示（高 DPI 下曾
  被压掉最后一个字）。

高 DPI
------
实测本机：屏幕 3120x2080，DPI=144。Tk 的几何单位是物理像素，但字号按 point 自动放大 1.5 倍，
所以如果窗口仍用 1500px 宽，内容会被挤扁、文字被截断。
解决：所有固定像素尺寸都过 `self.px()`（乘以 winfo_fpixels('1i')/96），窗口尺寸同理。

界面结构
--------
① 小程序窗口    —— 检测结果列表，多个时手动选一个；可抓窗口截图确认
② 探测选项      —— 五条通道开关、内存预算、并发、CDP 端口、输出目录、开始/取消 + 进度
③ 视频/图片列表 —— ✓ 选择 + 详情，支持批量/单条/全部 m3u8 下载
④ 运行日志      —— 分级着色、可过滤、可保存
状态栏          —— 阶段进度、计数、ffmpeg/管理员状态
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

from . import __version__, capture, config, downloader, logsetup, models, probe, sysinfo, urls, winapi, windows
from .logsetup import GUI_BRIDGE, human_size

log = logging.getLogger("wxs.gui")

WINDOW_COLUMNS = (
    ("idx", "#", 40),
    ("title", "小程序窗口标题", 220),
    ("hwnd", "HWND", 90),
    ("pid", "PID", 70),
    ("exe", "进程", 130),
    ("class", "窗口类", 150),
    ("size", "尺寸", 90),
    ("score", "置信度", 70),
    ("reasons", "判定依据", 380),
)

ITEM_COLUMNS = (
    ("sel", "✓", 32),
    ("type", "类型", 100),
    ("source", "来源", 90),
    ("title", "名称", 260),
    ("size", "大小", 90),
    ("time", "时间", 105),
    ("status", "状态", 90),
    ("progress", "进度", 150),
)


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.cfg = config.load_config()
        self.settings = config.load_settings(self.cfg)
        self.ui_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.detect_result: windows.DetectionResult | None = None
        self.current_window: winapi.WindowInfo | None = None
        self.items: dict[str, models.MediaItem] = {}
        self.download_states: dict[str, downloader.TaskState] = {}
        self.probe_result: probe.ProbeResult | None = None
        self.cancel_event = threading.Event()
        self.busy = ""            # "", "detect", "probe", "download"
        self.log_records = 0
        self.env_info: dict = {}
        self._win_rows: dict[str, windows.Candidate] = {}
        self._preview_refs: list = []
        self._action_mode = "probe"
        self._syncing_select_all = False

        # Tk 的几何单位是物理像素，字号却按 point 自动放大。144 DPI 下字号放大 1.5 倍，
        # 窗口必须同步放大，否则内容被挤扁、文字截断。
        try:
            self.ui_scale = max(1.0, float(root.winfo_fpixels("1i")) / 96.0)
        except Exception:  # noqa: BLE001
            self.ui_scale = 1.0
        self.scale = self.ui_scale

        self._build_ui()
        self._apply_scale()
        self.root.after(100, self._poll_ui)
        self.root.after(400, self._startup)

    def px(self, value: int | float) -> int:
        """把设计稿像素换算成当前 DPI 下的像素。"""
        return max(1, int(round(float(value) * self.ui_scale)))

    def text_width(self, text: str) -> int:
        """按当前字体量一段文字有多少像素（避免中文被截断）。"""
        try:
            return int(tkfont.nametofont("TkDefaultFont").measure(text))
        except Exception:  # noqa: BLE001
            return self.px(len(text) * 14)

    # =============================================================== UI 构建

    def _build_ui(self) -> None:
        self.root.title(f"微信小程序视频/图片下载器 v{__version__}")
        try:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        except Exception:  # noqa: BLE001
            sw, sh = 1600, 1000
        w = min(self.px(1500), max(900, sw - 60))
        h = min(self.px(950), max(600, sh - 120))
        self.root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2)}")
        # 最小宽度按 ③ 工具条的实际需求定（实测：工具条 978 物理像素 + 右栏 + 面板内边距
        # 需要 1260 逻辑像素），再窄下去按钮就会被裁掉（和“自动滚动”被切掉是同一类问题）。
        self.root.minsize(self.px(1260), self.px(640))

        self._build_menubar()

        header = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        header.pack(fill="x")
        ttk.Label(header, text="小程序资源抓取",
                  font=("Microsoft YaHei UI", 14, "bold")).pack(side="left")
        self.header_status = ttk.Label(
            header, text="初始化…", foreground="#666", font=("Microsoft YaHei UI", 9)
        )
        self.header_status.pack(side="left", padx=16)

        btns = ttk.Frame(header)
        btns.pack(side="right")
        self.header_btns = btns
        ttk.Button(btns, text="环境自检", command=self.on_env_check).pack(side="left", padx=3)
        ttk.Button(btns, text="打开下载目录", command=self.open_download_dir).pack(side="left", padx=3)
        # 注意：这里原来还有个「打开日志目录」按钮，与 ④ 的「打开日志文件」重复，已删除。

        # 注意 pack 顺序：先把状态栏占住底部，再放可伸缩的分栏，否则状态栏会被挤掉
        self._build_statusbar()
        self.paned = ttk.PanedWindow(self.root, orient="vertical")
        self.paned.pack(fill="both", expand=True, padx=8, pady=4)

        self._build_window_panel()
        self._build_middle_panel()
        self._build_log_panel()

    def _build_menubar(self) -> None:
        menubar = tk.Menu(self.root)
        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label="选择输出目录…", command=self.choose_out_dir)
        m_file.add_command(label="打开下载目录", command=self.open_download_dir)
        m_file.add_separator()
        m_file.add_command(label="打开配置文件 config.json", command=self.open_config_file)
        m_file.add_separator()
        m_file.add_command(label="退出", command=self.on_close)
        menubar.add_cascade(label="文件", menu=m_file)

        m_tool = tk.Menu(menubar, tearoff=0)
        m_tool.add_command(label="环境自检", command=self.on_env_check)
        m_tool.add_command(label="重新检测窗口", command=self.refresh_windows)
        m_tool.add_command(label="导入剪贴板中的链接", command=self.import_clipboard)
        m_tool.add_separator()
        m_tool.add_command(label="重新加载 config.json", command=self.reload_config)
        m_tool.add_separator()
        m_tool.add_command(label="清空日志", command=self.clear_log)
        m_tool.add_command(label="保存日志副本…", command=self.save_log_copy)
        m_tool.add_command(label="打开日志文件", command=self.open_log_file)
        menubar.add_cascade(label="工具", menu=m_tool)

        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="使用说明", command=self.show_help)
        m_help.add_command(label="关于", command=self.show_about)
        menubar.add_cascade(label="帮助", menu=m_help)
        self.root.config(menu=menubar)

    def _build_window_panel(self) -> None:
        frame = ttk.LabelFrame(self.paned, text=" ① 小程序窗口（检测到多个时可手动选择） ", padding=6)
        self.paned.add(frame, weight=2)

        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(0, 4))
        self.btn_detect = ttk.Button(bar, text="重新检测窗口", command=self.refresh_windows)
        self.btn_detect.pack(side="left")
        self.btn_preview = ttk.Button(bar, text="预览窗口截图", command=self.preview_window)
        self.btn_preview.pack(side="left", padx=(6, 0))
        self.var_show_all = tk.BooleanVar(value=bool(self.cfg.get("gui", {}).get("show_all_windows", False)))
        ttk.Checkbutton(bar, text="显示全部 Chromium 窗口（含 QQ/浏览器）",
                        variable=self.var_show_all, command=self._render_windows).pack(side="left", padx=10)
        self.lbl_window = ttk.Label(bar, text="未选择窗口", foreground="#0a7d32")
        self.lbl_window.pack(side="left", padx=12)
        ttk.Label(bar, text="双击一行 = 选中它", foreground="#777").pack(side="right")

        wrap = ttk.Frame(frame)
        wrap.pack(fill="both", expand=True)
        self.tree_win = ttk.Treeview(
            wrap, columns=[c[0] for c in WINDOW_COLUMNS], show="headings", height=3,
            selectmode="browse",
        )
        for key, title, width in WINDOW_COLUMNS:
            self.tree_win.heading(key, text=title)
            self.tree_win.column(key, width=self.px(width), anchor="w", stretch=(key == "reasons"))
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree_win.yview)
        self.tree_win.configure(yscrollcommand=vs.set)
        self.tree_win.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree_win.bind("<<TreeviewSelect>>", self._on_window_select)
        self.tree_win.bind("<Double-1>", self._on_window_select)

    def _build_middle_panel(self) -> None:
        frame = ttk.Frame(self.paned)
        self.paned.add(frame, weight=5)
        # 用 grid 明确划分左右两栏：右栏固定宽度，避免 pack 把选项面板压窄导致文字被截断
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, minsize=self.px(486), weight=0)
        frame.rowconfigure(0, weight=1)

        left = ttk.Frame(frame)
        left.grid(row=0, column=0, sticky="nsew")
        right = ttk.Frame(frame)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        # ---- 探测选项（复选框在左，三个数值输入在右，同一个 row 里并排）
        opt = ttk.LabelFrame(right, text=" ② 探测选项与操作 ", padding=8)
        opt.pack(fill="x")
        opt.columnconfigure(0, weight=1)   # 复选框列吃掉多余宽度
        self.opt_frame = opt

        self.var_mem = tk.BooleanVar(value=bool(self.settings.use_memory))
        self.var_cache_files = tk.BooleanVar(value=bool(self.settings.use_cache_files))
        self.var_cache_urls = tk.BooleanVar(value=bool(self.settings.use_cache_urls))
        self.var_cdp = tk.BooleanVar(value=bool(self.settings.use_cdp))
        self.var_images = tk.BooleanVar(value=bool(self.settings.use_images))

        ttk.Checkbutton(opt, text="内存扫描（CDN 直链）",
                        variable=self.var_mem).grid(row=0, column=0, sticky="w")
        ttk.Label(opt, text="内存预算(MB)").grid(row=0, column=1, sticky="e", padx=(10, 4))
        self.var_budget = tk.StringVar(value=str(self.settings.scan_budget_mb))
        self.spin_budget = ttk.Spinbox(opt, from_=32, to=8192, increment=64, width=8,
                                       textvariable=self.var_budget)
        self.spin_budget.grid(row=0, column=2, sticky="w")

        ttk.Checkbutton(opt, text="本地缓存文件（最稳）",
                        variable=self.var_cache_files).grid(row=1, column=0, sticky="w")
        ttk.Label(opt, text="下载并发").grid(row=1, column=1, sticky="e", padx=(10, 4))
        self.var_conc = tk.StringVar(value=str(self.settings.concurrency))
        self.spin_conc = ttk.Spinbox(opt, from_=1, to=16, width=8, textvariable=self.var_conc)
        self.spin_conc.grid(row=1, column=2, sticky="w")

        ttk.Checkbutton(opt, text="缓存 URL（补充）",
                        variable=self.var_cache_urls).grid(row=2, column=0, sticky="w")
        ttk.Label(opt, text="CDP 端口").grid(row=2, column=1, sticky="e", padx=(10, 4))
        self.var_cdp_port = tk.StringVar(value=str(self.settings.cdp_port_hint or ""))
        self.entry_cdp_port = ttk.Entry(opt, textvariable=self.var_cdp_port, width=10)
        self.entry_cdp_port.grid(row=2, column=2, sticky="w")

        ttk.Checkbutton(opt, text="CDP 调试端口（微信默认不开放）",
                        variable=self.var_cdp).grid(row=3, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(opt, text="图片（JPG/PNG）",
                        variable=self.var_images).grid(row=4, column=0, columnspan=3, sticky="w")

        ttk.Label(opt, text="提示：先在小程序里点开视频播放 3~5 秒，再点“开始探测”",
                  foreground="#666", wraplength=self.px(400)).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(2, 6))

        ttk.Label(opt, text="输出目录").grid(row=6, column=0, columnspan=3, sticky="w", pady=(2, 0))
        out_row = ttk.Frame(opt)
        out_row.grid(row=7, column=0, columnspan=3, sticky="ew")
        self.var_out = tk.StringVar(value=str(self.settings.out_dir))
        ttk.Entry(out_row, textvariable=self.var_out).pack(side="left", fill="x", expand=True)
        ttk.Button(out_row, text="…", width=3, command=self.choose_out_dir).pack(side="left", padx=(4, 0))

        # ---- 开始探测 / 取消：合并成一个按钮，进度条放右边（省一行高度）
        act = ttk.Frame(opt)
        act.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        act.columnconfigure(1, weight=1)
        btn_w = self.text_width("开始探测") + self.px(30)
        holder = ttk.Frame(act, width=btn_w, height=self.px(34))
        holder.grid(row=0, column=0, sticky="w")
        holder.grid_propagate(False)   # 固定宽度：只够放下 4 个汉字
        self.btn_action = ttk.Button(holder, text="开始探测", command=self.start_probe)
        self.btn_action.pack(fill="both", expand=True)
        self.progress = ttk.Progressbar(act, mode="indeterminate")
        self.progress.grid(row=0, column=1, sticky="ew", padx=(8, 0))

        # ---- 详情
        det = ttk.LabelFrame(right, text=" 详情（选中行的底层信息） ", padding=6)
        det.pack(fill="both", expand=True, pady=(8, 0))
        self.txt_detail = tk.Text(det, wrap="word", height=12, font=("Consolas", 9),
                                  bg="#1e1e1e", fg="#d4d4d4")
        dsb = ttk.Scrollbar(det, orient="vertical", command=self.txt_detail.yview)
        self.txt_detail.configure(yscrollcommand=dsb.set)
        self.txt_detail.pack(side="left", fill="both", expand=True)
        dsb.pack(side="right", fill="y")

        # ---- 视频/图片列表
        vids = ttk.LabelFrame(left, text=" ③ 列表（点 ✓ 列切换选择 · 双击看详情） ", padding=6)
        vids.pack(fill="both", expand=True)

        vbar = ttk.Frame(vids)
        vbar.pack(fill="x", pady=(0, 4))
        self.vbar = vbar
        # 全选 / 全不选 合并成一个勾选框
        self.var_select_all = tk.BooleanVar(value=False)
        self.chk_select_all = ttk.Checkbutton(vbar, text="全选", variable=self.var_select_all,
                                             command=self._on_select_all)
        self.chk_select_all.pack(side="left")
        ttk.Button(vbar, text="反选", width=6, command=self._invert_selection).pack(side="left", padx=2)
        ttk.Separator(vbar, orient="vertical").pack(side="left", fill="y", padx=8)
        self.btn_dl_sel = ttk.Button(vbar, text="下载选中",
                                     command=lambda: self.start_download(only_selected=True))
        self.btn_dl_sel.pack(side="left", padx=2)
        self.btn_dl_all = ttk.Button(vbar, text="下载全部",
                                     command=lambda: self.start_download(only_selected=False))
        self.btn_dl_all.pack(side="left", padx=2)
        self.btn_dl_hls = ttk.Button(vbar, text="全部m3u8", command=self.start_download_hls)
        self.btn_dl_hls.pack(side="left", padx=2)
        ttk.Separator(vbar, orient="vertical").pack(side="left", fill="y", padx=8)
        self.btn_remove = ttk.Button(vbar, text="移除", width=4, command=self.remove_selected)
        self.btn_remove.pack(side="left", padx=2)
        self.btn_paste = ttk.Button(vbar, text="粘贴", width=4, command=self.import_clipboard)
        self.btn_paste.pack(side="left", padx=2)

        self.lbl_items = ttk.Label(vbar, text="共0 · 选0", foreground="#555")
        self.lbl_items.pack(side="right")
        self.var_filter = tk.StringVar(value="全部")
        filt = ttk.Combobox(vbar, textvariable=self.var_filter, width=5, state="readonly",
                            values=("全部", "视频", "图片"))
        filt.pack(side="right", padx=(8, 8))
        filt.bind("<<ComboboxSelected>>", lambda e: self._render_items())

        wrap = ttk.Frame(vids)
        wrap.pack(fill="both", expand=True)
        self.tree_items = ttk.Treeview(
            wrap, columns=[c[0] for c in ITEM_COLUMNS], show="headings", selectmode="extended"
        )
        for key, title, width in ITEM_COLUMNS:
            self.tree_items.heading(key, text=title)
            self.tree_items.column(key, width=self.px(width), anchor="w", stretch=(key == "title"))
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree_items.yview)
        self.tree_items.configure(yscrollcommand=vsb.set)
        self.tree_items.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree_items.bind("<Button-1>", self._on_item_click)
        self.tree_items.bind("<<TreeviewSelect>>", self._on_item_select)
        self.tree_items.bind("<space>", lambda e: self._toggle_focused_item())
        self.tree_items.bind("<Double-1>", self._on_item_select)
        self.tree_items.tag_configure("local", foreground="#0a7d32")
        self.tree_items.tag_configure("remote", foreground="#1a1a1a")
        self.tree_items.tag_configure("image", foreground="#6a1b9a")
        self.tree_items.tag_configure("expired", foreground="#c62828")
        self.tree_items.tag_configure("done", foreground="#1565c0")

    def _build_log_panel(self) -> None:
        frame = ttk.LabelFrame(self.paned, text=" ④ 运行日志（含底层细节，可保存后发给开发者） ", padding=6)
        self.paned.add(frame, weight=4)

        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Label(bar, text="级别").grid(row=0, column=0, sticky="w")
        self.var_level = tk.StringVar(value=str(self.cfg.get("gui", {}).get("log_level", "DEBUG")))
        combo = ttk.Combobox(bar, textvariable=self.var_level, width=9, state="readonly",
                             values=("TRACE", "DEBUG", "INFO", "WARNING", "ERROR"))
        combo.grid(row=0, column=1, sticky="w", padx=(4, 10))
        combo.bind("<<ComboboxSelected>>", lambda e: logsetup.set_level(self.var_level.get()))
        self.var_autoscroll = tk.BooleanVar(value=bool(self.cfg.get("gui", {}).get("autoscroll", True)))
        self.chk_autoscroll = ttk.Checkbutton(bar, text="自动滚动", variable=self.var_autoscroll)
        self.chk_autoscroll.grid(row=0, column=2, sticky="w")
        ttk.Button(bar, text="清空", width=6, command=self.clear_log).grid(
            row=0, column=3, sticky="w", padx=(10, 0))
        ttk.Button(bar, text="保存日志副本", command=self.save_log_copy).grid(
            row=0, column=4, sticky="w", padx=(4, 0))
        ttk.Button(bar, text="打开日志文件", command=self.open_log_file).grid(
            row=0, column=5, sticky="w", padx=(4, 0))
        bar.columnconfigure(6, weight=1)   # 弹性空隙：窗口变窄时先压这里
        self.lbl_logcount = ttk.Label(bar, text="", foreground="#666")
        self.lbl_logcount.grid(row=0, column=7, sticky="e", padx=(8, 0))
        # 关键修复：给“自动滚动”这一列一个按字体量出来的最小宽度。
        # 之前用 pack，窗口一窄 Tk 就把最后一个字“动”压掉了。
        bar.columnconfigure(2, minsize=self.text_width("自动滚动") + self.px(30))

        wrap = ttk.Frame(frame)
        wrap.pack(fill="both", expand=True)
        self.txt_log = tk.Text(wrap, wrap="none", height=8, font=("Consolas", 9),
                               bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4")
        ysb = ttk.Scrollbar(wrap, orient="vertical", command=self.txt_log.yview)
        xsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.txt_log.xview)
        self.txt_log.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.txt_log.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        for lvl in ("TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            self.txt_log.tag_configure(lvl, foreground=logsetup.level_color(lvl))
        self.txt_log.configure(state="disabled")

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self.root, padding=(10, 3))
        bar.pack(fill="x", side="bottom")
        self.var_stage = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.var_stage, foreground="#0a5").pack(side="left")
        self.var_counter = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.var_counter, foreground="#555").pack(side="right")
        self.var_env = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.var_env, foreground="#555").pack(side="right", padx=16)

    def _apply_scale(self) -> None:
        """让 ttk 的行高/按钮内边距跟上 DPI，否则高 DPI 下表格行会挤在一起。"""
        try:
            self.root.tk.call("tk", "scaling", (self.ui_scale * 96) / 72.0)
        except Exception:  # noqa: BLE001
            pass
        style = ttk.Style()
        try:
            style.configure("Treeview", rowheight=self.px(22))
            style.configure("TButton", padding=(self.px(6), self.px(3)))
        except Exception:  # noqa: BLE001
            pass
        try:
            self.root.update_idletasks()  # 否则 geometry 还没生效，日志里会是 1x1
        except Exception:  # noqa: BLE001
            pass
        log.info("界面缩放: DPI=%.0f 比例=%.2f 窗口=%s", self.ui_scale * 96, self.ui_scale,
                 self.root.geometry())
        log.info("配置: %s | 文件=%s", config.config_summary(self.cfg), config.CONFIG_FILE)

    # =============================================================== 启动

    def _startup(self) -> None:
        self.set_stage("正在检测小程序窗口…")
        try:
            logsetup.set_level(self.var_level.get())
        except Exception:  # noqa: BLE001
            pass
        self.refresh_windows()
        threading.Thread(target=self._env_check_worker, name="env", daemon=True).start()

    def _env_check_worker(self) -> None:
        try:
            info = sysinfo.check_environment(self.settings)
            self.settings.ffmpeg_path = info.get("ffmpeg", "")
            self.settings.ffprobe_path = info.get("ffprobe", "")
            self.ui_queue.put(("env", info))
        except Exception:  # noqa: BLE001
            log.exception("环境自检失败")
            self.ui_queue.put(("env", {}))

    def on_env_check(self) -> None:
        self.set_stage("环境自检中…")
        threading.Thread(target=self._env_check_worker, name="env", daemon=True).start()

    # =============================================================== 日志刷新

    def _poll_ui(self) -> None:
        try:
            self._drain_logs()
            self._drain_events()
        except Exception:  # noqa: BLE001
            log.exception("UI 轮询异常")
        finally:
            self.root.after(100, self._poll_ui)

    def _drain_logs(self) -> None:
        records = GUI_BRIDGE.drain(600)
        if not records:
            return
        min_level = {"TRACE": 0, "DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}.get(
            self.var_level.get(), 10)
        rank = {"TRACE": 5, "DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        self.txt_log.configure(state="normal")
        shown = 0
        for _ts, level, _name, text in records:
            self.log_records += 1
            if rank.get(level, 20) < min_level:
                continue
            self.txt_log.insert("end", text + "\n", level)
            shown += 1
        if shown:
            total_lines = int(self.txt_log.index("end-1c").split(".")[0])
            if total_lines > 4000:  # 防止 Text 无限增长把内存吃光
                self.txt_log.delete("1.0", f"{total_lines - 3000}.0")
            if self.var_autoscroll.get():
                self.txt_log.see("end")
        self.txt_log.configure(state="disabled")
        dropped = GUI_BRIDGE.dropped
        self.lbl_logcount.configure(
            text=f"已记录 {self.log_records} 条" + (f"，丢弃 {dropped} 条" if dropped else "")
        )

    def _drain_events(self) -> None:
        while True:
            try:
                kind, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle_event(kind, payload)
            except Exception:  # noqa: BLE001
                log.exception("处理界面事件失败 kind=%s", kind)

    def _handle_event(self, kind: str, payload) -> None:
        if kind == "detect_done":
            self.busy = ""
            self.btn_detect.configure(state="normal")
            self._progress_stop()
            result: windows.DetectionResult = payload  # type: ignore[assignment]
            self.detect_result = result
            self._render_windows()
            if result.count == 1:
                self.set_stage(f"检测到 1 个小程序窗口：{result.candidates[0].title}（已自动选中）")
                self._auto_select_first()
            elif result.count > 1:
                self.set_stage(f"检测到 {result.count} 个候选窗口，请在 ① 里选择一个")
                self._auto_select_first()
            else:
                self.set_stage("未检测到微信小程序窗口 —— 请先在微信里打开一个小程序")
            for n in result.notes:
                log.warning("提示: %s", n)
            self.var_counter.set(f"窗口候选 {result.count} 个")
        elif kind == "detect_error":
            self.busy = ""
            self.btn_detect.configure(state="normal")
            self._progress_stop()
            self.set_stage("窗口检测失败，详见日志")
        elif kind == "env":
            self.env_info = payload or {}
            ff_path = self.env_info.get("ffmpeg") or ""
            ff_short = ("…" + ff_path[-34:]) if len(ff_path) > 36 else ff_path
            ff = f"ffmpeg ✓ {ff_short}" if ff_path else "ffmpeg × 未找到"
            admin = "管理员 ✓" if logsetup.is_admin() else "普通权限"
            self.var_env.set(f"{ff} | {admin} | 微信目录 {len(self.env_info.get('wechat_roots', []))} 个")
            self.header_status.configure(
                text=f"{'ffmpeg 可用' if ff_path else 'ffmpeg 缺失（m3u8 合并不可用）'}  |  "
                     f"小程序缓存目录 {len(self.env_info.get('applet_roots', []))} 个  |  "
                     f"Chrome 缓存目录 {len(self.env_info.get('cache_roots', []))} 个"
            )
        elif kind == "stage":
            self.set_stage(str(payload))
        elif kind == "probe_item":
            self._add_item(payload)  # type: ignore[arg-type]
        elif kind == "preview":
            self._show_preview(str(payload or ""))
        elif kind == "probe_done":
            self._on_probe_done(payload)  # type: ignore[arg-type]
        elif kind == "probe_error":
            self.busy = ""
            self._set_buttons(True)
            self._progress_stop()
            self.set_stage("探测失败，详见日志")
        elif kind == "dl_state":
            self._on_download_state(payload)
        elif kind == "dl_done":
            self.busy = ""
            self._set_buttons(True)
            self._progress_stop()
            states: dict = payload or {}  # type: ignore[assignment]
            ok = sum(1 for s in states.values() if s.ok)
            warn = sum(1 for s in states.values() if s.warn)
            fail = sum(1 for s in states.values()
                       if not s.ok and s.status not in ("已取消", "已跳过"))
            self.set_stage(f"下载结束：成功 {ok}" + (f"（其中 {warn} 个有点可疑，见日志）" if warn else "")
                           + f"，失败 {fail}")
            self.var_counter.set(f"成功 {ok} / 失败 {fail}")

    # =============================================================== 窗口检测

    def refresh_windows(self) -> None:
        if self.busy:
            log.warning("当前有操作在进行（%s），已忽略本次检测请求", self.busy)
            return
        self.busy = "detect"
        self.btn_detect.configure(state="disabled")
        self._progress_start()
        self.set_stage("正在枚举窗口…")

        def worker() -> None:
            try:
                result = windows.detect()
                self.ui_queue.put(("detect_done", result))
            except Exception:  # noqa: BLE001
                log.exception("窗口检测异常")
                self.ui_queue.put(("detect_error", None))

        threading.Thread(target=worker, name="detect", daemon=True).start()

    def _render_windows(self) -> None:
        self.tree_win.delete(*self.tree_win.get_children())
        self._win_rows = {}
        if not self.detect_result:
            return
        rows = list(self.detect_result.candidates)
        if self.var_show_all.get():
            extras = [c for c in self.detect_result.all_chromium if c not in rows]
            rows = rows + extras
        for idx, cand in enumerate(rows, 1):
            info = cand.info
            iid = f"w{idx}"
            self._win_rows[iid] = cand
            self.tree_win.insert(
                "", "end", iid=iid,
                values=(idx, info.title or "(无标题)", hex(info.hwnd), info.pid,
                        os.path.basename(info.exe) or "?",
                        info.class_name, f"{info.size[0]}x{info.size[1]}",
                        cand.score, " / ".join(cand.reasons)),
            )

    def _auto_select_first(self) -> None:
        children = self.tree_win.get_children()
        if not children:
            return
        first = children[0]
        self.tree_win.selection_set(first)
        self.tree_win.focus(first)
        self.tree_win.see(first)
        self._on_window_select()

    def _on_window_select(self, _event=None) -> None:
        sel = self.tree_win.selection()
        if not sel:
            return
        cand = self._win_rows.get(sel[0])
        if cand is None:
            return
        self.current_window = cand.info
        info = cand.info
        self.lbl_window.configure(
            text=f"已选中：{info.title or '(无标题)'}   hwnd={hex(info.hwnd)}   pid={info.pid}   "
                 f"{info.size[0]}x{info.size[1]}"
        )
        log.info("用户选择窗口: %s", cand.summary())
        log.info("   可执行文件: %s", info.exe)
        log.info("   窗口矩形: %s  DWM矩形: %s  DPI: %d", info.rect, info.dwm_rect, info.dpi)
        log.info("   子窗口: %s", info.children)

    def preview_window(self) -> None:
        """抓一张窗口截图，确认选中的是不是目标小程序（不依赖 ffmpeg）。"""
        if self.current_window is None:
            messagebox.showwarning("未选择窗口", "请先在 ① 里选择一个小程序窗口。")
            return
        if self.busy:
            log.warning("当前有操作在进行（%s），已忽略截图请求", self.busy)
            return
        self.set_stage("正在抓窗口截图…")
        out = Path(self.settings.out_dir) / f"预览_{time.strftime('%H%M%S')}.png"

        def worker() -> None:
            try:
                path = capture.grab_window_png(self.current_window, out)
                self.ui_queue.put(("preview", path))
            except Exception as exc:  # noqa: BLE001
                log.exception("窗口截图失败: %s", exc)
                self.ui_queue.put(("preview", ""))

        threading.Thread(target=worker, name="preview", daemon=True).start()

    def _show_preview(self, path: str) -> None:
        if not path:
            self.set_stage("窗口截图失败，详见日志")
            return
        self.set_stage(f"窗口截图: {path}")
        try:
            from PIL import Image, ImageTk

            top = tk.Toplevel(self.root)
            top.title(f"窗口预览 — {self.current_window.title if self.current_window else ''}")
            img = Image.open(path)
            if img.width > self.px(1200):
                ratio = self.px(1200) / img.width
                img = img.resize((self.px(1200), int(img.height * ratio)), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            label = tk.Label(top, image=photo)
            label.image = photo  # 防止被 GC
            label.pack(fill="both", expand=True)
            ttk.Label(top, text=f"{path}    {img.width}x{img.height}").pack(fill="x")
            self._preview_refs.append(photo)
            if len(self._preview_refs) > 4:
                self._preview_refs.pop(0)
        except Exception as exc:  # noqa: BLE001
            log.warning("内置预览失败(%s)，改用系统查看器打开", exc)
            try:
                os.startfile(path)  # noqa: S606
            except Exception as exc2:  # noqa: BLE001
                messagebox.showerror("无法预览", f"{exc2}\n\n文件位置:\n{path}")

    # =============================================================== 探测

    def _sync_settings(self) -> bool:
        try:
            budget = int(self.var_budget.get())
            conc = int(self.var_conc.get())
        except ValueError:
            messagebox.showerror("参数错误", "内存预算/并发数必须是整数")
            return False
        self.settings.scan_budget_mb = max(16, budget)
        self.settings.concurrency = max(1, min(16, conc))
        self.settings.cdp_port_hint = self.var_cdp_port.get().strip()
        self.settings.use_memory = bool(self.var_mem.get())
        self.settings.use_cache_files = bool(self.var_cache_files.get())
        self.settings.use_cache_urls = bool(self.var_cache_urls.get())
        self.settings.use_cdp = bool(self.var_cdp.get())
        self.settings.use_images = bool(self.var_images.get())
        out = self.var_out.get().strip()
        if out:
            self.settings.out_dir = Path(out)
        try:
            self.settings.out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("输出目录不可用", f"{self.settings.out_dir}\n{exc}")
            return False
        return True

    def start_probe(self) -> None:
        if self.busy:
            log.warning("当前有操作在进行（%s），已忽略探测请求", self.busy)
            return
        if not self._sync_settings():
            return
        if not any((self.var_mem.get(), self.var_cache_files.get(),
                    self.var_cache_urls.get(), self.var_cdp.get(), self.var_images.get())):
            messagebox.showwarning("没有通道", "至少要勾选一条探测通道。")
            return
        if self.current_window is None and not self.var_cache_files.get():
            messagebox.showwarning("未选择窗口", "请先在 ① 里选择一个小程序窗口；\n"
                                            "或者只勾选“本地缓存文件”以扫描所有小程序的缓存。")
            return

        opts = probe.ProbeOptions(
            use_memory=self.var_mem.get(),
            use_cache_files=self.var_cache_files.get(),
            use_cache_urls=self.var_cache_urls.get(),
            use_cdp=self.var_cdp.get(),
            use_images=self.var_images.get(),
            expand_hls=self.settings.hls_expand_masters,
            cdp_seconds=6.0,
        )
        self.busy = "probe"
        self._set_buttons(False)
        self._progress_start()
        self.cancel_event.clear()
        self.items.clear()
        self.download_states.clear()
        self.tree_items.delete(*self.tree_items.get_children())
        self._update_item_count()
        self.set_stage("探测中…（内存扫描可能需要 10~60 秒，可点“取消”）")
        if self.current_window is None:
            log.warning("未选择窗口：内存扫描/CDP 会跳过，仅扫描本地缓存")

        def worker() -> None:
            try:
                result = probe.probe(
                    self.current_window, self.settings, opts, cancel=self.cancel_event,
                    on_stage=lambda stage, msg: self.ui_queue.put(("stage", f"[{stage}] {msg}")),
                    on_item=lambda it: self.ui_queue.put(("probe_item", it)),
                )
                self.ui_queue.put(("probe_done", result))
            except Exception:  # noqa: BLE001
                log.exception("探测线程异常")
                self.ui_queue.put(("probe_error", None))

        threading.Thread(target=worker, name="probe", daemon=True).start()

    def _on_probe_done(self, result: probe.ProbeResult) -> None:
        self.busy = ""
        self._set_buttons(True)
        self._progress_stop()
        self.probe_result = result
        self._update_item_count()
        if result.items:
            self.set_stage(f"探测完成：{result.summary_text()}")
            self.var_counter.set(
                f"视频 {result.video_count} 条（m3u8 {result.hls_count}）/ 图片 {result.image_count} 张")
            log.info("探测结果:\n%s", probe.copy_items_preview(result.items, limit=20))
        else:
            self.set_stage("探测完成，但没有找到媒体。详见日志与提示。")
            hints = "\n".join(f"· {n}" for n in result.notes) or "· 视频可能还没播放"
            messagebox.showinfo(
                "没有找到媒体",
                f"{hints}\n\n可以尝试：\n"
                "1) 在小程序里点开视频播放 3~5 秒后再探测\n"
                "2) 只勾选“本地缓存文件”再探测一次\n"
                "3) 想看图片就勾上“图片（JPG/PNG）”，并至少启用一个来源通道",
            )
        for n in result.notes:
            log.warning("注意: %s", n)

    # =============================================================== 列表

    def _row_tag(self, item: models.MediaItem) -> str:
        if item.status.startswith("已完成"):
            return "done"
        if item.expired:
            return "expired"
        if item.is_image:
            return "image"
        return "local" if item.kind == "local" else "remote"

    def _progress_label(self, item: models.MediaItem) -> str:
        st = self.download_states.get(item.uid)
        return st.progress_label() if st else ""

    def _row_values(self, item: models.MediaItem) -> tuple:
        return item.row() + (self._progress_label(item),)

    def _match_filter(self, item: models.MediaItem) -> bool:
        mode = self.var_filter.get()
        if mode == "视频":
            return not item.is_image
        if mode == "图片":
            return item.is_image
        return True

    def _add_item(self, item: models.MediaItem) -> None:
        self.items[item.uid] = item
        if self._match_filter(item) and not self.tree_items.exists(item.uid):
            self.tree_items.insert("", "end", iid=item.uid, values=self._row_values(item),
                                   tags=(self._row_tag(item),))
        self._update_item_count()

    def _render_items(self) -> None:
        """按当前筛选重建列表（切换“视频/图片”时用）。"""
        self.tree_items.delete(*self.tree_items.get_children())
        for item in self.items.values():
            if self._match_filter(item):
                self.tree_items.insert("", "end", iid=item.uid, values=self._row_values(item),
                                       tags=(self._row_tag(item),))
        self._update_item_count()

    def _refresh_row(self, item: models.MediaItem) -> None:
        if self.tree_items.exists(item.uid):
            self.tree_items.item(item.uid, values=self._row_values(item),
                                 tags=(self._row_tag(item),))

    def _update_item_count(self) -> None:
        items = list(self.items.values())
        selected = sum(1 for i in items if i.selected)
        # 工具条空间宝贵：只放最短形式；视频/图片拆分看「筛选」下拉或状态栏
        self.lbl_items.configure(text=f"共{len(items)} · 选{selected}")
        # 同步“全选”勾选框状态（注意防重入）
        self._syncing_select_all = True
        try:
            self.var_select_all.set(bool(items) and selected == len(items))
        finally:
            self._syncing_select_all = False

    def _on_select_all(self) -> None:
        if self._syncing_select_all:
            return
        value = bool(self.var_select_all.get())
        for item in self.items.values():
            item.selected = value
        self._render_items()
        log.info("全选 -> %s", "全选" if value else "全不选")

    def _on_item_click(self, event) -> None:
        region = self.tree_items.identify("region", event.x, event.y)
        if region != "cell":
            return
        if self.tree_items.identify_column(event.x) != "#1":
            return
        row = self.tree_items.identify_row(event.y)
        item = self.items.get(row)
        if item is None:
            return
        item.selected = not item.selected
        self._refresh_row(item)
        self._update_item_count()

    def _toggle_focused_item(self) -> None:
        for row in self.tree_items.selection():
            item = self.items.get(row)
            if item:
                item.selected = not item.selected
                self._refresh_row(item)
        self._update_item_count()

    def _on_item_select(self, _event=None) -> None:
        sel = self.tree_items.selection()
        if not sel:
            return
        item = self.items.get(sel[0])
        if item is not None:
            self._show_detail(item)

    def _show_detail(self, item: models.MediaItem) -> None:
        lines = [
            f"名称      : {item.title}",
            f"类型      : {item.kind_label}（{'图片' if item.is_image else '视频'}）",
            f"来源通道  : {item.source}",
            f"分数      : {item.score}",
            f"体积      : {item.size_label}",
            f"时间      : {item.mtime_label}",
            f"appid     : {item.appid or '-'}",
            f"过期      : {'是（签名已过期，需重新播放后再探测）' if item.expired else '否'}",
            f"状态      : {item.status}",
            "",
        ]
        state = self.download_states.get(item.uid)
        if state and (state.warn or state.error):
            lines += [f"下载告警  : {state.warn or '-'}", f"下载错误  : {state.error or '-'}", ""]
        if item.kind == "remote":
            lines += ["URL:", item.url]
        else:
            lines += ["本地路径:", item.path]
        lines += ["", f"判定依据  : {item.detail}", "", "额外信息  :",
                  json.dumps(item.extra, ensure_ascii=False, indent=2, default=str)]
        self.txt_detail.delete("1.0", "end")
        self.txt_detail.insert("1.0", "\n".join(lines))

    def _invert_selection(self) -> None:
        for item in self.items.values():
            item.selected = not item.selected
        self._render_items()

    def remove_selected(self) -> None:
        rows = list(self.tree_items.selection())
        if not rows:
            rows = [uid for uid, it in self.items.items() if it.selected]
        for uid in rows:
            self.items.pop(uid, None)
            if self.tree_items.exists(uid):
                self.tree_items.delete(uid)
        self._update_item_count()
        log.info("移除 %d 条", len(rows))

    # =============================================================== 下载

    def start_download(self, only_selected: bool = True) -> None:
        if self.busy:
            log.warning("当前有操作在进行（%s），已忽略下载请求", self.busy)
            return
        if not self._sync_settings():
            return
        items = [i for i in self.items.values() if (i.selected or not only_selected)]
        if not items:
            messagebox.showinfo("没有可下载条目", "列表里没有条目。先点「开始探测」。")
            return
        self._launch_download(items, "选中" if only_selected else "全部")

    def start_download_hls(self) -> None:
        """一键下载列表里所有 m3u8（主清单在探测阶段已经展开成各清晰度）。"""
        if self.busy:
            log.warning("当前有操作在进行（%s），已忽略下载请求", self.busy)
            return
        if not self._sync_settings():
            return
        items = [i for i in self.items.values() if i.is_hls and i.is_remote]
        if not items:
            messagebox.showinfo("没有 m3u8",
                                "列表里没有 m3u8 条目。\n"
                                "先播放视频再探测，或在 config.json 打开 channels.memory / cache_urls。")
            return
        log.info("“全部m3u8”选中 %d 条 HLS", len(items))
        self._launch_download(items, "全部m3u8")

    def _launch_download(self, items: list[models.MediaItem], label: str) -> None:
        expired = sum(1 for i in items if i.expired and i.kind == "remote")
        if expired and not messagebox.askyesno(
            "有已过期链接",
            f"其中 {expired} 条链接的签名时间戳已过期，下载大概率失败。\n仍要继续吗？",
        ):
            return

        self.busy = "download"
        self._set_buttons(False)
        self._progress_determinate()
        self.cancel_event.clear()
        self.set_stage(f"开始下载 {len(items)} 个条目…")

        def on_event(kind: str, uid: str, payload: dict) -> None:
            if kind == "state":
                self.ui_queue.put(("dl_state", payload))
            elif kind == "done":
                self.ui_queue.put(("dl_state", {"done": True, **payload}))

        def worker() -> None:
            try:
                dl = downloader.Downloader(self.settings, on_event=on_event)
                dl.cancel_event = self.cancel_event
                if self.settings.ffmpeg_path:
                    dl.ffmpeg = self.settings.ffmpeg_path
                states = dl.run(items, label=label)
                self.ui_queue.put(("dl_done", states))
            except Exception:  # noqa: BLE001
                log.exception("下载线程异常")
                self.ui_queue.put(("dl_done", {}))

        threading.Thread(target=worker, name="download", daemon=True).start()

    def _on_download_state(self, payload: dict) -> None:
        if "done" in payload:
            uid = payload.get("uid", "")
            item = self.items.get(uid)
            state = self.download_states.get(uid)
            if item:
                item.status = state.status if state else "已完成"
                item.progress = 100.0
                self._refresh_row(item)
            return
        state = payload.get("state")
        if state is None:
            return
        self.download_states[state.uid] = state
        item = self.items.get(state.uid)
        if item is None:
            return
        item.status = state.status
        item.progress = state.percent
        self._refresh_row(item)
        done = sum(1 for s in self.download_states.values() if s.ok)
        total = len(self.download_states)
        if total:
            self.progress.configure(mode="determinate", value=done * 100 / total)
        speed = f" {human_size(state.speed)}/s" if state.speed else ""
        self.set_stage(f"下载中：{state.status} {state.progress_label()}{speed}")

    # =============================================================== 操作按钮

    def _set_action_mode(self, mode: str) -> None:
        """一个按钮两种身份：空闲=开始探测，忙=取消。"""
        self._action_mode = mode
        if mode == "cancel":
            self.btn_action.configure(text="取消", command=self.cancel_current, state="normal")
        else:
            self.btn_action.configure(text="开始探测", command=self.start_probe, state="normal")

    def cancel_current(self) -> None:
        if not self.busy:
            return
        log.warning("用户请求取消当前操作: %s", self.busy)
        self.cancel_event.set()
        self.set_stage("正在取消…（收尾可能需要几秒）")

    def _progress_start(self) -> None:
        try:
            self.progress.configure(mode="indeterminate", value=0)
            self.progress.start(12)
        except Exception:  # noqa: BLE001
            pass

    def _progress_stop(self) -> None:
        try:
            self.progress.stop()
            self.progress.configure(mode="indeterminate", value=0)
        except Exception:  # noqa: BLE001
            pass

    def _progress_determinate(self) -> None:
        try:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0, maximum=100)
        except Exception:  # noqa: BLE001
            pass

    # =============================================================== 其它操作

    def import_clipboard(self) -> None:
        """把剪贴板/手动输入的链接导入列表（视频和图片都收）。"""
        from tkinter import simpledialog

        try:
            clip = self.root.clipboard_get()
        except tk.TclError:
            clip = ""
        text = simpledialog.askstring(
            "导入链接", "粘贴媒体地址（可多行，会自动分类；支持 mp4/m3u8/jpg/png）：",
            initialvalue=clip, parent=self.root,
        )
        if not text:
            return
        report = urls.analyze({line.strip() for line in text.splitlines() if line.strip()},
                             want_images=True)
        added = 0
        for idx, info in enumerate(report.downloadable, 1):
            self._add_item(models.MediaItem.from_url_info(info, source="手动", index=idx))
            added += 1
        for idx, info in enumerate(report.downloadable_images, 1):
            self._add_item(models.MediaItem.from_url_info(info, source="手动", index=idx))
            added += 1
        self._update_item_count()
        log.info("手动导入: %d 条", added)
        if not added:
            messagebox.showwarning("没有可用链接",
                                   "没有识别出可下载的地址（支持 http(s) 直链、m3u8、jpg/png）。")

    def choose_out_dir(self) -> None:
        d = filedialog.askdirectory(initialdir=str(self.settings.out_dir), title="选择下载目录")
        if d:
            self.var_out.set(d)
            self.settings.out_dir = Path(d)
            log.info("输出目录改为: %s", d)

    def open_download_dir(self) -> None:
        self._open_path(self.settings.out_dir)

    def open_config_file(self) -> None:
        path = config.CONFIG_FILE
        if not path.exists():
            config.load_config()   # 会按默认值生成一份
        try:
            os.startfile(str(path))  # noqa: S606
            log.info("打开配置文件: %s", path)
        except Exception as exc:  # noqa: BLE001
            log.warning("打开配置文件失败 %s: %s", path, exc)
            messagebox.showinfo("配置文件", f"路径：\n{path}\n\n{exc}")

    def open_log_file(self) -> None:
        """定位并打开当前日志文件（资源管理器里选中它）。"""
        lf = logsetup.log_file_path()
        if lf and lf.exists():
            log.info("当前日志文件: %s", lf)
            try:
                os.system(f'explorer /select,"{lf}"')  # noqa: S605 - 本地固定路径
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("资源管理器定位失败: %s", exc)
        self._open_path(config.LOGS_DIR)

    def reload_config(self) -> None:
        """重新读 config.json 并刷新界面默认值（不重启程序）。"""
        if self.busy:
            log.warning("当前有操作在进行（%s），已忽略重新加载配置", self.busy)
            return
        old_ffmpeg = self.settings.ffmpeg_path
        old_ffprobe = self.settings.ffprobe_path
        self.cfg = config.load_config()
        self.settings = config.load_settings(self.cfg)
        self.settings.ffmpeg_path = old_ffmpeg
        self.settings.ffprobe_path = old_ffprobe
        self._apply_config_to_ui()
        log.info("已重新加载配置: %s", config.config_summary(self.cfg))
        self.set_stage("配置已重新加载（见日志）")

    def _apply_config_to_ui(self) -> None:
        cfg = self.cfg
        gui = cfg.get("gui", {})
        self.var_mem.set(bool(self.settings.use_memory))
        self.var_cache_files.set(bool(self.settings.use_cache_files))
        self.var_cache_urls.set(bool(self.settings.use_cache_urls))
        self.var_cdp.set(bool(self.settings.use_cdp))
        self.var_images.set(bool(self.settings.use_images))
        self.var_budget.set(str(self.settings.scan_budget_mb))
        self.var_conc.set(str(self.settings.concurrency))
        self.var_cdp_port.set(str(self.settings.cdp_port_hint or ""))
        self.var_out.set(str(self.settings.out_dir))
        self.var_autoscroll.set(bool(gui.get("autoscroll", True)))
        self.var_show_all.set(bool(gui.get("show_all_windows", False)))
        self.var_level.set(str(gui.get("log_level", "DEBUG")))
        self._render_windows()

    def _open_path(self, path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))  # noqa: S606
        except Exception as exc:  # noqa: BLE001
            log.warning("打开目录失败 %s: %s", path, exc)

    def clear_log(self) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")
        self.log_records = 0
        self.lbl_logcount.configure(text="")

    def save_log_copy(self) -> None:
        src = logsetup.log_file_path()
        default = f"wxs_log_{time.strftime('%Y%m%d_%H%M%S')}.log"
        dest = filedialog.asksaveasfilename(defaultextension=".log", initialfile=default,
                                            title="保存日志副本")
        if not dest:
            return
        try:
            if src and src.exists():
                Path(dest).write_text(src.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
            else:
                Path(dest).write_text(self.txt_log.get("1.0", "end"), encoding="utf-8")
            log.info("日志已另存: %s", dest)
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))

    def show_help(self) -> None:
        messagebox.showinfo(
            "使用说明",
            "1) 在微信里打开目标小程序，并播放要下载的视频 3~5 秒（让地址/缓存出现）\n"
            "2) 点「重新检测窗口」，在 ① 中选中该小程序窗口（可点「预览窗口截图」确认）\n"
            "3) 勾选探测通道后点「开始探测」（探测中这个按钮会变成「取消」）\n"
            "4) 在 ③ 里用 ✓ 列选择（「全选」勾选框可一键全选/全不选），然后下载\n\n"
            "· 「全部m3u8」会把列表里所有 HLS 一次性下载；探测时主清单已自动展开成各清晰度\n"
            "· 勾上「图片（JPG/PNG）」会额外收小程序里的图片\n"
            "· 默认值都在 config.json 里改（通道开关、内存预算、并发、CDP 端口……）\n\n"
            "找不到视频时：\n"
            "· 只勾「本地缓存文件」再探测（最稳，直接复制已落盘的 mp4）\n"
            "· 链接签名过期会下载失败，重新播放后再探测\n"
            "· 下载完成的条目如果是“已完成⚠”，说明文件可能不完整，详情面板会写原因\n\n"
            "日志文件在 logs/ 目录，出问题先看那里。",
        )

    def show_about(self) -> None:
        import sys

        messagebox.showinfo(
            "关于",
            f"微信小程序视频/图片下载器 v{__version__}\n\n"
            f"Python {sys.version.split()[0]} ({sys.executable})\n"
            f"配置文件: {config.CONFIG_FILE}\n"
            f"日志: {logsetup.log_file_path()}\n"
            f"下载目录: {self.settings.out_dir}",
        )

    # =============================================================== 工具

    def _set_buttons(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for btn in (self.btn_detect, self.btn_dl_sel, self.btn_dl_all, self.btn_dl_hls,
                    self.btn_remove, self.btn_paste, self.btn_preview):
            try:
                btn.configure(state=state)
            except tk.TclError:
                pass
        self._set_action_mode("probe" if enabled else "cancel")

    def set_stage(self, text: str) -> None:
        self.var_stage.set(text)
        log.debug("[阶段] %s", text)

    def on_close(self) -> None:
        if self.busy in ("download", "probe"):
            if not messagebox.askyesno("正在运行", f"当前正在{self.busy}，确定退出吗？"):
                return
            self.cancel_event.set()
            time.sleep(0.3)
        log.info("用户关闭程序")
        logging.shutdown()
        self.root.destroy()


def run() -> int:
    logsetup.setup(level="DEBUG", console_level="INFO", gui=True)
    dpi_mode = winapi.set_dpi_awareness()
    log.info("DPI 感知模式: %s", dpi_mode)
    winapi.enable_debug_privilege()

    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:  # noqa: BLE001
        pass
    app = App(root)

    def _on_error(exc, val, tb):  # noqa: ANN001
        log.critical("Tk 回调异常: %s\n%s", val, "".join(traceback.format_exception(exc, val, tb)))
        try:
            messagebox.showerror("程序内部错误",
                                 f"{val}\n\n详细信息已写入日志：\n{logsetup.log_file_path()}")
        except Exception:  # noqa: BLE001
            pass

    root.report_callback_exception = _on_error
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0


__all__ = ["App", "run"]
