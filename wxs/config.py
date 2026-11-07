"""配置、路径解析与运行期设置。

配置来源优先级
--------------
1. `config.json`（程序根目录，用户可改；首次启动自动生成，带中文说明字段）
2. `Settings` dataclass 里的硬编码默认值（兜底，config.json 缺项/写坏时用）

`config.json` 里能改的东西：
* `channels`  —— 五条探测通道默认是否勾选
* `numbers`   —— 所有数值型默认值（内存预算、并发、CDP 端口、缓存预算……）
* `gui`       —— 界面默认值（输出目录、日志级别、自动滚动、显示全部窗口）

改完存盘后**重启程序**生效（界面上改的只对当前会话有效，不写回配置文件）。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("wxs.config")


# ---------------------------------------------------------------- 路径

def app_root() -> Path:
    """程序根目录。

    - 源码运行: 仓库根目录（wxs 的父目录）
    - PyInstaller 打包: 可执行文件所在目录（_MEIPASS 是只读临时目录，不能写日志）
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = app_root()
LOGS_DIR = ROOT / "logs"
DOWNLOADS_DIR = ROOT / "downloads"
CONFIG_FILE = ROOT / "config.json"


def ensure_dirs() -> None:
    for d in (LOGS_DIR, DOWNLOADS_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------- 微信数据目录

def wechat_data_roots() -> list[Path]:
    """列出可能的微信/小程序运行时数据根目录（按存在性过滤）。

    坑：微信 4.x（xwechat）与 3.x 目录名不同，且 QQ NT 内置的 wmpfsdk 也叫
    WeChatAppEx.exe，其数据根是 QQ 的目录 —— 这里只收微信的。
    """
    appdata = Path(os.environ.get("APPDATA", ""))
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    userprofile = Path(os.environ.get("USERPROFILE", ""))
    docs = userprofile / "Documents"

    candidates = [
        appdata / "Tencent" / "xwechat" / "radium",           # 微信 4.x 小程序运行时
        appdata / "Tencent" / "WeChat" / "radium",            # 某些 4.x 变体
        appdata / "Tencent" / "WeChat" / "XPlugin",           # 微信 3.9.x WMPF 插件
        local / "Tencent" / "WeChat" / "radium",
        docs / "WeChat Files" / "Applet",                     # 微信 3.x 小程序数据
        docs / "xwechat_files",
    ]
    seen: set[Path] = set()
    out: list[Path] = []
    for c in candidates:
        try:
            key = Path(os.path.normcase(str(c)))
        except (OSError, ValueError):
            continue
        if key in seen:
            continue
        seen.add(key)
        if c.is_dir():
            out.append(c)
    return out


def applet_local_roots() -> list[Path]:
    """小程序 <appid>/local 目录集合（缓存视频所在处）。"""
    roots: list[Path] = []
    for base in wechat_data_roots():
        for pat in ("users/*/applet/local", "Applet", "*/*/applet/local"):
            roots.extend(p for p in base.glob(pat) if p.is_dir())
        local = base / "applet" / "local"
        if local.is_dir():
            roots.append(local)
    # 去重保序
    out, seen = [], set()
    for p in roots:
        k = os.path.normcase(str(p))
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def chromium_cache_roots() -> list[Path]:
    """Chromium Simple-Cache 目录（Cache_Data）。"""
    roots: list[Path] = []
    for base in wechat_data_roots():
        for pat in ("web/profiles/*/Cache/Cache_Data", "*/Cache/Cache_Data", "Cache/Cache_Data"):
            roots.extend(p for p in base.glob(pat) if p.is_dir())
    out, seen = [], set()
    for p in roots:
        k = os.path.normcase(str(p))
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


MEDIA_EXTS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".flv", ".avi", ".ts", ".m4s", ".3gp",
    ".mp3", ".m4a", ".aac", ".wav", ".ogg", ".m3u8",
}

# 只收这三种图片（用户要求 jpg/png；webp/gif/svg 等在 urls.py 里仍算噪音）
PHOTO_EXTS = {".jpg", ".jpeg", ".png"}


# ---------------------------------------------------------------- 配置文件

DEFAULT_CONFIG: dict = {
    "_说明": [
        "本文件是默认值配置。改完保存后重启程序生效。",
        "channels: 探测通道默认是否勾选（true/false）。",
        "numbers: 数值默认值；cdp_port 留空表示不指定端口。",
        "gui: 界面默认值；out_dir 留空表示用程序目录下的 downloads/。",
        "界面上临时改的勾选/数值只对当前这次运行有效，不会写回本文件。",
    ],
    "channels": {
        "memory": False,        # 内存扫描（CDN 直链）—— 鹅打卡里全是广告，默认关
        "cache_files": False,   # 本地缓存文件 —— 鹅打卡里全是广告，默认关
        "cache_urls": True,     # Chromium 缓存 URL（补充）
        "cdp": False,           # CDP 调试端口（微信默认不开放，默认关）
        # 图片：默认关。实测一次探测能从缓存里翻出 800+ 张图标/头像/广告图，
        # 会把列表淹掉；要看图片时在界面勾一下，或把这里改成 true。
        "images": False,
    },
    "numbers": {
        "scan_budget_mb": 900,      # 内存扫描总预算（MB，按进程均摊）
        "concurrency": 3,           # 下载并发
        "cdp_port": "",             # 手动指定 CDP 端口，如 "9222" / "9222,9223"
        "cache_url_budget_mb": 512, # Chromium 缓存 URL 扫描总读取上限
        "cache_url_file_mb": 64,    # 单个缓存文件最大读取
        "min_local_mb": 0.08,       # 本地缓存文件小于该体积忽略
        "min_image_kb": 24,         # 图片小于该体积忽略（过滤图标/小图）
        "http_timeout": 30.0,
        "http_retries": 3,
        "local_wait_seconds": 8.0,  # 复制本地缓存前最多等它“写完”多少秒
        "hls_expand_masters": True, # 遇到 m3u8 主清单时自动列出所有清晰度
        "native_hls": True,         # 用内置分片下载器下 HLS（比直接丢给 ffmpeg 全）
    },
    "gui": {
        "out_dir": "",
        "log_level": "DEBUG",
        "autoscroll": True,
        "show_all_windows": False,
    },
}


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on", "开", "是"):
            return True
        if low in ("0", "false", "no", "off", "关", "否", ""):
            return False
    log.warning("配置项应为布尔值，收到 %r，改用默认值 %s", value, default)
    return default


def _as_int(value, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        log.warning("配置项应为整数，收到 %r，改用默认值 %s", value, default)
        return default


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        log.warning("配置项应为数字，收到 %r，改用默认值 %s", value, default)
        return default


def _merge_into(base: dict, extra: dict, prefix: str = "") -> None:
    """把 extra 里“已知的”键合并进 base；未知键只记日志不报错。"""
    for key, value in extra.items():
        if key.startswith("_"):
            continue
        if key not in base:
            log.warning("config.json 里有未知配置项 %s%s（已忽略）", prefix, key)
            continue
        if isinstance(base[key], dict):
            if isinstance(value, dict):
                _merge_into(base[key], value, prefix=f"{prefix}{key}.")
            else:
                log.warning("配置项 %s%s 应为对象，已忽略", prefix, key)
            continue
        if isinstance(value, (dict, list)):
            log.warning("配置项 %s%s 类型不对（%s），已忽略", prefix, key, type(value).__name__)
            continue
        base[key] = value


def load_config(path: Path | str | None = None) -> dict:
    """读 config.json 并与内置默认值合并；文件不存在时按默认值生成一份。"""
    cfg_path = Path(path) if path else CONFIG_FILE
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if cfg_path.is_file():
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            log.error("读取配置文件失败 %s: %s（使用内置默认值）", cfg_path, exc)
            return cfg
        if isinstance(raw, dict):
            _merge_into(cfg, raw)
            log.info("已加载配置文件: %s", cfg_path)
        else:
            log.error("配置文件内容不是 JSON 对象，忽略: %s", cfg_path)
        return cfg

    try:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        log.info("已生成默认配置文件: %s", cfg_path)
    except OSError as exc:
        log.warning("无法生成配置文件 %s: %s（本次使用内置默认值）", cfg_path, exc)
    return cfg


def config_summary(cfg: dict) -> str:
    ch = cfg.get("channels", {})
    on = [k for k, v in ch.items() if v]
    num = cfg.get("numbers", {})
    return (f"通道默认开启={on or '无'} | 内存预算={num.get('scan_budget_mb')}MB "
            f"并发={num.get('concurrency')} CDP端口={num.get('cdp_port') or '自动'} "
            f"图片下限={num.get('min_image_kb')}KB")


# ---------------------------------------------------------------- 运行期设置

@dataclass
class Settings:
    """GUI/CLI 共用的运行期参数。默认值来自 config.json（见 load_settings）。"""

    out_dir: Path = field(default_factory=lambda: DOWNLOADS_DIR)
    concurrency: int = 3
    scan_budget_mb: int = 900        # 所有目标进程合计最多读多少 MB 内存
    cache_url_budget_mb: int = 512   # Chromium 缓存 URL 扫描的总读取上限
    cache_url_file_mb: int = 64      # 单个缓存文件最大读取
    scan_include_children: bool = True
    scan_utf16: bool = True
    min_local_mb: float = 0.08       # 小于这个体积的缓存文件忽略
    min_image_kb: int = 24           # 小于这个体积的图片忽略（过滤图标）
    http_timeout: float = 30.0
    http_retries: int = 3
    chunk_size: int = 1 << 20
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 MicroMessenger/7.0.20"
    )
    referer: str = "https://servicewechat.com/"
    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    cdp_port_hint: str = ""          # 用户手填，如 "9222"
    probe_std_ports: bool = True
    keep_part_files: bool = True
    max_url_age_hint: int = 6 * 3600  # 签名 URL 提醒阈值（秒）
    local_wait_seconds: float = 8.0   # 复制本地缓存前最多等它写完多少秒
    hls_expand_masters: bool = True   # m3u8 主清单自动展开成所有清晰度
    native_hls: bool = True           # 用内置分片下载器下载 HLS

    # 通道默认开关（GUI 复选框的初始状态 / CLI --probe 的默认通道）
    use_memory: bool = False
    use_cache_files: bool = False
    use_cache_urls: bool = True
    use_cdp: bool = False
    use_images: bool = False

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["out_dir"] = str(self.out_dir)
        return d


def apply_config(settings: Settings, cfg: dict | None = None) -> Settings:
    """把 config.json 的值套到 Settings 上（就地修改并返回）。"""
    cfg = cfg if cfg is not None else load_config()
    ch = cfg.get("channels") or {}
    settings.use_memory = _as_bool(ch.get("memory", settings.use_memory), settings.use_memory)
    settings.use_cache_files = _as_bool(ch.get("cache_files", settings.use_cache_files),
                                        settings.use_cache_files)
    settings.use_cache_urls = _as_bool(ch.get("cache_urls", settings.use_cache_urls),
                                       settings.use_cache_urls)
    settings.use_cdp = _as_bool(ch.get("cdp", settings.use_cdp), settings.use_cdp)
    settings.use_images = _as_bool(ch.get("images", settings.use_images), settings.use_images)

    num = cfg.get("numbers") or {}
    settings.scan_budget_mb = max(16, _as_int(num.get("scan_budget_mb", settings.scan_budget_mb),
                                              settings.scan_budget_mb))
    settings.concurrency = max(1, min(16, _as_int(num.get("concurrency", settings.concurrency),
                                                  settings.concurrency)))
    settings.cache_url_budget_mb = max(1, _as_int(
        num.get("cache_url_budget_mb", settings.cache_url_budget_mb), settings.cache_url_budget_mb))
    settings.cache_url_file_mb = max(1, _as_int(
        num.get("cache_url_file_mb", settings.cache_url_file_mb), settings.cache_url_file_mb))
    settings.min_local_mb = max(0.0, _as_float(num.get("min_local_mb", settings.min_local_mb),
                                               settings.min_local_mb))
    settings.min_image_kb = max(0, _as_int(num.get("min_image_kb", settings.min_image_kb),
                                          settings.min_image_kb))
    settings.http_timeout = max(3.0, _as_float(num.get("http_timeout", settings.http_timeout),
                                               settings.http_timeout))
    settings.http_retries = max(1, min(10, _as_int(num.get("http_retries", settings.http_retries),
                                                   settings.http_retries)))
    settings.local_wait_seconds = max(0.0, _as_float(
        num.get("local_wait_seconds", settings.local_wait_seconds), settings.local_wait_seconds))
    settings.hls_expand_masters = _as_bool(
        num.get("hls_expand_masters", settings.hls_expand_masters), settings.hls_expand_masters)
    settings.native_hls = _as_bool(num.get("native_hls", settings.native_hls),
                                   settings.native_hls)
    settings.cdp_port_hint = str(num.get("cdp_port", settings.cdp_port_hint) or "").strip()

    gui = cfg.get("gui") or {}
    out = str(gui.get("out_dir", "") or "").strip()
    if out:
        settings.out_dir = Path(out).expanduser()
    return settings


def load_settings(cfg: dict | None = None) -> Settings:
    """按 config.json 生成一份 Settings（程序各处入口都该用它）。"""
    settings = Settings()
    try:
        apply_config(settings, cfg)
    except Exception:  # noqa: BLE001 - 配置写坏不能让程序起不来
        log.exception("应用配置失败，使用内置默认值")
        return Settings()
    return settings


DEFAULT_SETTINGS = Settings()
