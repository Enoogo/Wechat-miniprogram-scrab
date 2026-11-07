"""微信小程序窗口检测、分类与选择。

实测事实（Windows 10/11 + 微信 4.x，2026 实测）
------------------------------------------------
* 小程序窗口由独立进程 `WeChatAppEx.exe` 承载（WMPF / RadiumWMPF 运行时）。
  典型路径: %APPDATA%\\Tencent\\xwechat\\XPlugin\\Plugins\\RadiumWMPF\\WeChatAppEx.exe
* 窗口类名是 `Chrome_WidgetWin_0`（Chromium CEF），子窗口依次为
  `ContainerWindow` → `Chrome_RenderWidgetHostHWND` / `Intermediate D3D Window`。
* 微信主程序（4.x）叫 `Weixin.exe`，目录 `C:\\Program Files\\Tencent\\Weixin`；
  3.x 叫 `WeChat.exe`。两者也会开 `Chrome_WidgetWin_1` 窗口 —— 不能当成小程序窗口。
* 坑：QQ NT 也内置同名的 `WeChatAppEx.exe`
  （C:\\Program Files\\Tencent\\QQNT\\versions\\*\\resources\\app\\wmpfsdk\\runtime\\WeChatAppEx.exe），
  必须按可执行文件路径区分，否则会把 QQ 内的小程序/频道窗口当成微信小程序。
* 坑：普通 Chrome/Edge 窗口同样是 `Chrome_WidgetWin_1`，必须按进程名过滤。

本模块只做判断，不做 UI。GUI 直接消费 `DetectionResult`。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from . import winapi
from .winapi import WindowInfo

log = logging.getLogger("wxs.windows")

# 微信主窗口标题（简体/繁体/英文/新版）
MAIN_TITLES = {"微信", "WeChat", "Weixin", "微信（测试版）"}

FAMILY_WECHAT_MP = "wechat_mp"       # 微信小程序运行时 WeChatAppEx.exe
FAMILY_QQ_WMPF = "qq_mp"             # QQ 内置 wmpfsdk 的同名进程
FAMILY_WECHAT_MAIN = "wechat_main"   # 微信主程序
FAMILY_OTHER = "other"

FAMILY_LABELS = {
    FAMILY_WECHAT_MP: "微信小程序运行时",
    FAMILY_QQ_WMPF: "QQ 内置小程序(需排除)",
    FAMILY_WECHAT_MAIN: "微信主程序",
    FAMILY_OTHER: "其它进程",
}


def classify_process(exe_path: str, proc_name: str = "") -> str:
    """根据可执行文件路径判定进程家族。这是整个检测的地基。"""
    path = (exe_path or "").lower().replace("/", "\\")
    name = (proc_name or os.path.basename(path)).lower()

    if name in ("weixin.exe", "wechat.exe") or path.endswith(("\\weixin.exe", "\\wechat.exe")):
        return FAMILY_WECHAT_MAIN
    if name == "wechatappex.exe":
        # QQ NT 的 wmpfsdk 打包了同名的 WeChatAppEx.exe，路径里带 qqnt 的一定不是微信的
        if "\\qqnt\\" in path or "wmpfsdk" in path:
            return FAMILY_QQ_WMPF
        if "\\xwechat\\" in path or "\\wechat\\" in path or "radiumwmpf" in path:
            return FAMILY_WECHAT_MP
        # 路径拿不到时，纯靠进程名也是微信的小程序运行时（保守处理但要提示）
        return FAMILY_WECHAT_MP
    return FAMILY_OTHER


def is_chromium_class(class_name: str) -> bool:
    return (class_name or "").startswith("Chrome_WidgetWin")


@dataclass
class Candidate:
    """一个小程序窗口候选，附带判断依据与置信度。"""

    info: WindowInfo
    family: str
    score: int
    reasons: list[str] = field(default_factory=list)
    is_mini_program: bool = False

    @property
    def hwnd(self) -> int:
        return self.info.hwnd

    @property
    def title(self) -> str:
        return self.info.title

    @property
    def pid(self) -> int:
        return self.info.pid

    def summary(self) -> str:
        return (
            f"hwnd={hex(self.hwnd)} pid={self.pid} class={self.info.class_name} "
            f"title={self.info.title!r} size={self.info.size[0]}x{self.info.size[1]} "
            f"family={FAMILY_LABELS.get(self.family, self.family)} score={self.score} "
            f"依据={'/'.join(self.reasons)}"
        )


@dataclass
class DetectionResult:
    all_chromium: list[Candidate] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)  # 判定为小程序窗口
    notes: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.candidates)

    def display_rows(self) -> list[dict]:
        rows = []
        for c in self.candidates:
            r = c.info.as_row()
            r["family"] = FAMILY_LABELS.get(c.family, c.family)
            r["score"] = c.score
            r["reasons"] = " / ".join(c.reasons)
            r["raw_title"] = c.info.title
            rows.append(r)
        return rows


def _wechat_hint_paths() -> list[str]:
    """已知的微信运行时目录，用于在路径拿不到时兜底识别。"""
    appdata = os.environ.get("APPDATA", "")
    hints = []
    if appdata:
        hints += [
            os.path.join(appdata, "Tencent", "xwechat").lower(),
            os.path.join(appdata, "Tencent", "WeChat").lower(),
        ]
    return hints


def analyze_window(info: WindowInfo) -> Candidate:
    """对单个窗口打分，给出是否小程序窗口的结论。"""
    proc_name = os.path.basename(info.exe) if info.exe else winapi.process_cwd_name(info.pid)
    family = classify_process(info.exe, proc_name)
    score = 0
    reasons: list[str] = []

    if not is_chromium_class(info.class_name):
        return Candidate(info, family, 0, ["非Chromium窗口类"], False)

    reasons.append(f"Chromium类:{info.class_name}")
    score += 10

    if family == FAMILY_WECHAT_MP:
        score += 40
        reasons.append("进程WeChatAppEx")
        if "\\xwechat\\" in info.exe.lower():
            score += 10
            reasons.append("路径xwechat(微信4.x)")
    elif family == FAMILY_QQ_WMPF:
        score -= 60
        reasons.append("QQ内置同名进程(排除)")
    elif family == FAMILY_WECHAT_MAIN:
        score -= 50
        reasons.append("微信主程序(排除)")
    else:
        # 路径取不到时的兜底：进程名 + 已知目录
        if proc_name.lower() == "wechatappex.exe":
            score += 40
            reasons.append("进程名WeChatAppEx(无路径)")
        else:
            if info.exe:
                for hint in _wechat_hint_paths():
                    if hint and info.exe.lower().startswith(hint):
                        score += 20
                        reasons.append("微信目录下进程")
                        break
            else:
                reasons.append("进程路径不可读")

    if info.children:
        if "Chrome_RenderWidgetHostHWND" in info.children:
            score += 15
            reasons.append("含RenderWidgetHost")
        if "ContainerWindow" in info.children:
            score += 5
            reasons.append("含ContainerWindow")

    if info.class_name == "Chrome_WidgetWin_0":
        score += 15
        reasons.append("类名为Win_0(小程序特征)")
    elif info.class_name == "Chrome_WidgetWin_1":
        score -= 5
        reasons.append("类名Win_1(常见于主窗口/浏览器)")

    if info.title in MAIN_TITLES:
        score -= 80
        reasons.append("主窗口标题(排除)")
    if "devtools" in info.title.lower():
        score -= 80
        reasons.append("开发者工具(排除)")
    if not info.title:
        score -= 10
        reasons.append("无标题")

    w, h = info.size
    if w < 200 or h < 200:
        score -= 30
        reasons.append(f"尺寸过小{w}x{h}")
    else:
        score += 5

    if info.cloaked:
        score -= 40
        reasons.append("被DWM隐藏(cloaked)")
    if not info.visible:
        score -= 20
        reasons.append("不可见")
    if info.minimized:
        score -= 5
        reasons.append("已最小化")
    if info.exstyle & winapi.WS_EX_TOOLWINDOW:
        score -= 20
        reasons.append("工具窗口")
    if info.style & winapi.WS_CHILD:
        score -= 40
        reasons.append("子窗口(非顶层)")

    is_mp = score >= 55 and family in (FAMILY_WECHAT_MP, FAMILY_OTHER)
    if family == FAMILY_QQ_WMPF or family == FAMILY_WECHAT_MAIN:
        is_mp = False
    return Candidate(info, family, score, reasons, is_mp)


def detect(include_hidden: bool = True) -> DetectionResult:
    """扫描所有顶层窗口，找出小程序窗口候选。include_hidden=True 时把最小化窗口也算进来。"""
    result = DetectionResult()
    windows = winapi.enum_top_windows(include_invisible=include_hidden)
    log.info("窗口扫描: 顶层窗口 %d 个", len(windows))

    for info in windows:
        # 枚举包含隐藏窗口（数量可达数百个消息窗口/辅助窗口）。
        # 只保留「可见窗口」以及「有标题的 Chromium 窗口」（后者可能是被最小化的小程序）。
        if not info.visible and not (is_chromium_class(info.class_name) and info.title):
            continue
        cand = analyze_window(info)
        if is_chromium_class(info.class_name):
            result.all_chromium.append(cand)
            log.debug("Chromium窗口: %s", cand.summary())
        if cand.is_mini_program:
            result.candidates.append(cand)

    result.candidates.sort(key=lambda c: (-c.score, c.title))
    result.all_chromium.sort(key=lambda c: (-c.score, c.title))

    if not result.candidates:
        result.notes.append(
            "没有检测到微信小程序窗口。请确认：①微信已登录；②小程序已打开且不是最小化到托盘；"
            "③如果同时开了 QQ 的小程序，本工具会主动排除 QQ 内置运行时。"
        )
        qq = [c for c in result.all_chromium if c.family == FAMILY_QQ_WMPF]
        if qq:
            result.notes.append(f"发现 {len(qq)} 个 QQ 内置 WMPF 窗口（已排除）。")
        other = [c for c in result.all_chromium if c.family == FAMILY_OTHER and c.score > 20]
        if other:
            result.notes.append(
                f"还有 {len(other)} 个非微信的 Chromium 窗口，可在“显示全部窗口”里手动选择。"
            )

    log.info(
        "窗口检测结果: 小程序窗口 %d 个%s",
        len(result.candidates),
        "" if not result.candidates else " | " + "; ".join(c.title or "?" for c in result.candidates),
    )
    for c in result.candidates:
        log.info("  候选: %s", c.summary())
    for n in result.notes:
        log.warning("  提示: %s", n)
    return result


def process_tree(pid: int, include_children: bool = True) -> list[dict]:
    """返回目标进程及其后代（renderer / network service / gpu）。

    内存扫描必须覆盖 renderer 与 network service —— 媒体 URL 主要活在它们的内存里。
    """
    import psutil

    out: list[dict] = []
    try:
        root = psutil.Process(pid)
    except Exception as exc:  # noqa: BLE001
        log.warning("获取进程 %s 失败: %s", pid, exc)
        return [{"pid": pid, "name": "?", "type": "?", "exe": "", "parent": 0}]

    procs = [root]
    if include_children:
        try:
            procs.extend(root.children(recursive=True))
        except Exception as exc:  # noqa: BLE001
            log.warning("枚举子进程失败 pid=%s: %s", pid, exc)

    for p in procs:
        try:
            with p.oneshot():
                cmd = p.cmdline()
                ptype = "browser"
                for tok in cmd:
                    if tok.startswith("--type="):
                        ptype = tok.split("=", 1)[1]
                        break
                out.append(
                    {
                        "pid": p.pid,
                        "name": p.name(),
                        "type": ptype,
                        "exe": p.exe() if p.pid != 0 else "",
                        "parent": p.ppid(),
                        "mem_mb": round(p.memory_info().rss / 1048576, 1),
                    }
                )
        except Exception:  # noqa: BLE001
            out.append({"pid": p.pid, "name": "?", "type": "?", "exe": "", "parent": 0, "mem_mb": 0})
    log.info("进程树: %d 个进程 -> %s", len(out), ", ".join(f"{i['pid']}({i['type']})" for i in out))
    return out


def wechat_runtime_version(info: WindowInfo | None = None) -> str:
    """从 WeChatAppEx 进程命令行里抠出运行时版本目录 / product 信息，便于写交接文档。"""
    try:
        import psutil

        for p in psutil.process_iter(["pid", "name", "cmdline", "ppid"]):
            if (p.info["name"] or "").lower() != "wechatappex.exe":
                continue
            cmd = " ".join(p.info["cmdline"] or [])
            if "--type=" in cmd:
                continue
            for token in (p.info["cmdline"] or []):
                if token.startswith("--") and ("version" in token or "instance" in token):
                    return token
            return cmd[:160]
    except Exception:  # noqa: BLE001
        pass
    return ""


__all__ = [
    "Candidate", "DetectionResult", "detect", "analyze_window", "classify_process",
    "process_tree", "wechat_runtime_version", "FAMILY_LABELS",
    "FAMILY_WECHAT_MP", "FAMILY_QQ_WMPF", "FAMILY_WECHAT_MAIN", "FAMILY_OTHER",
]
