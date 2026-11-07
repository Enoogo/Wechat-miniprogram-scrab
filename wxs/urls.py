"""URL 提取、清洗、分类与打分。

进程内存里捞出来的字符串是“脏”的：会有拼接、JSON 转义、UTF-16、
以及大量 js/css/图片/wxapkg 之类的噪音。本模块负责把它们变成可信的媒体地址。

实测噪音样本（真实存在于 WeChatAppEx 内存）：
    https://mp.weixin.qq.com/s/FXu8hJoGLWTQ84ru-pzhTwhttps://mp.weixin.qq.com/s?t=pages/...
    ↑ 两个 URL 粘在一起，必须在第二个 http 处切开
    https://mp.weixin.qq.com/s?t=pages/...&vidsn=&sharer_shareinfo_first=61
    ↑ 尾部被截断的也算可用（只是缺参数），保留但要打低分

真实可下载样本（微信 CDN 直链，带签名，会过期）：
    https://wxsmw.wxs.qq.com/131/20210/snssvpdownload/SH/reserved/ads_svp_video__xxx.f142000.mp4?dis_k=..&dis_t=1789892460&sha256=..
    http://wxsnsdythumb.wxs.qq.com/109/20204/snsvideodownload?m=..&filekey=..
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("wxs.urls")

# ---------------------------------------------------------------- 正则

ASCII_URL_RE = re.compile(rb"https?://[\x21-\x7e]{4,3000}")
UTF16_MARK = b"h\x00t\x00t\x00p\x00"
TEXT_URL_RE = re.compile(r"https?://[^\s\"'<>`\\\x00-\x1f\x7f]{4,3000}")

VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".flv", ".f4v", ".avi", ".3gp", ".wmv"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus"}
SEGMENT_EXTS = {".ts", ".m4s", ".m4f", ".cmfv", ".cmfa"}
HLS_EXTS = {".m3u8"}
DASH_EXTS = {".mpd"}
# 只收这三种图片：用户要求“jpg 和 png”。webp/gif/svg/ico 等仍然算噪音，
# 否则小程序里的图标、webp 占位图会把列表淹掉。
PHOTO_EXTS = {".jpg", ".jpeg", ".png"}
IMAGE_EXTS = PHOTO_EXTS | {".gif", ".webp", ".svg", ".ico", ".bmp", ".heic"}
NON_MEDIA_EXTS = (IMAGE_EXTS - PHOTO_EXTS) | {
    ".js", ".mjs", ".css", ".map", ".json", ".xml", ".html", ".htm", ".txt", ".woff",
    ".woff2", ".ttf", ".otf", ".eot", ".wasm", ".pak", ".wxapkg", ".zip", ".rar", ".7z",
    ".gz", ".br", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".apk",
    ".exe", ".dll", ".dat", ".bin", ".db", ".log",
}

MEDIA_HOST_HINTS = {
    "mpvideo.qpic.cn": 55,
    "mpvideo.wxs.qq.com": 55,
    "wxsnsdythumb.wxs.qq.com": 50,
    "wxsnsdy.wxs.qq.com": 50,
    "wxsmw.wxs.qq.com": 45,
    "finder.video.qq.com": 45,
    "video.qq.com": 30,
    "res.wx.qq.com": -10,
    "servicewechat.com": -5,
    "mp.weixin.qq.com": -35,
    "mmbiz.qpic.cn": -40,
    "mmgame.qpic.cn": -40,
    "wx.qlogo.cn": -40,
}

SIGNED_QUERY_KEYS = ("dis_t", "dis_k", "sha256", "vkey", "filekey", "mmfileid", "stodownloadurl")
EXPIRY_KEYS = ("dis_t", "expire", "expires", "deadline", "timelength2")

# 微信 CDN 的图片经常把格式放在查询参数里（路径没有扩展名），例如
#   https://mmbiz.qpic.cn/mmbiz_jpg/xxxx/0?wx_fmt=jpeg
IMAGE_QUERY_KEYS = ("wx_fmt", "format", "fm", "imgtype", "image_type")
IMAGE_SNIFF_RE = re.compile(r"\.(?:jpg|jpeg|png)(?:[/?#]|$)", re.IGNORECASE)

CHUNK_LIMIT = 3000


# ---------------------------------------------------------------- 提取

def iter_urls_from_bytes(data: bytes, include_utf16: bool = True) -> list[str]:
    """从一段进程内存里捞出所有 URL（ASCII 与 UTF-16LE 两种编码）。

    UTF-16 处理策略：先定位 `h\\x00t\\x00t\\x00p\\x00` 标记，再解一小段 utf-16-le。
    整个区域直接 decode 会把内存翻倍且容易把二进制当字符解码，得不偿失。
    """
    out: list[str] = []

    for m in ASCII_URL_RE.finditer(data):
        out.append(m.group(0).decode("latin-1", errors="replace"))

    if include_utf16:
        pos = 0
        n = len(data)
        while True:
            i = data.find(UTF16_MARK, pos)
            if i < 0:
                break
            chunk = data[i:i + CHUNK_LIMIT * 2]
            try:
                text = chunk.decode("utf-16-le", errors="ignore")
            except Exception:  # noqa: BLE001
                text = ""
            for m in TEXT_URL_RE.finditer(text):
                out.append(m.group(0))
            pos = i + len(UTF16_MARK)
            if n - pos < 8:
                break
    return out


def clean_url(raw: str) -> str:
    """把冗余的转义/拼接清理掉，返回第一个合法 URL 片段（可能为空串）。"""
    if not raw:
        return ""
    s = raw.strip()
    # JSON 转义
    s = s.replace("\\/", "/").replace("\\u0026", "&").replace("\\u003d", "=").replace("&amp;", "&")
    s = s.replace("\\", "")
    # 拼接的第二个 URL：从第二个 http 开始全部丢掉
    second = s.find("http", 4)
    if second > 0:
        s = s[:second]
    # 截断点：空白、控制字符、引号、尖括号、反引号、中文标点
    s = re.split(r"[\s\x00-\x1f\"'<>`，。；、）】》]", s, maxsplit=1)[0]
    # 尾部标点
    s = s.rstrip(".,;:!?)]}'\"")
    if not s.lower().startswith(("http://", "https://")):
        return ""
    try:
        parts = urlsplit(s)
    except ValueError:
        return ""
    if not parts.netloc or "." not in parts.netloc:
        return ""
    return s


def path_ext(url: str) -> str:
    try:
        path = urlsplit(url).path.lower()
    except ValueError:
        return ""
    idx = path.rfind(".")
    if idx < 0:
        return ""
    ext = path[idx:]
    return ext if len(ext) <= 6 else ""


# ---------------------------------------------------------------- 分类

@dataclass
class UrlInfo:
    url: str
    kind: str                                   # direct / hls / dash / segment / audio / image / page / other
    ext: str
    host: str
    score: int
    reasons: list[str] = field(default_factory=list)
    identity: str = ""
    expired: bool = False
    expiry_ts: int | None = None
    signed: bool = False

    @property
    def is_downloadable(self) -> bool:
        return self.kind in ("direct", "hls", "dash", "audio", "image") and not self.expired

    @property
    def is_image(self) -> bool:
        return self.kind == "image"

    @property
    def kind_label(self) -> str:
        return {
            "direct": "直链视频", "hls": "HLS 播放列表", "dash": "DASH 清单",
            "segment": "分片(需合并)", "audio": "音频", "image": "图片",
            "page": "网页/页面", "other": "其它",
        }.get(self.kind, self.kind)

    def summary(self) -> str:
        exp = ""
        if self.expiry_ts:
            try:
                exp = " 过期=" + datetime.fromtimestamp(self.expiry_ts).strftime("%m-%d %H:%M")
                if self.expired:
                    exp += "(已过期)"
            except (OSError, ValueError):
                exp = ""
        return f"[{self.kind_label}] score={self.score} host={self.host}{exp} url={self.url[:180]}"


def classify_url(url: str) -> UrlInfo:
    """判定 URL 类型并打分。分数越高越可能是“可直接下载的视频”。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return UrlInfo(url, "other", "", "", -100, ["URL解析失败"])

    host = (parts.hostname or "").lower()
    ext = path_ext(url)
    query = parse_qs(parts.query)
    qkeys = {k.lower() for k in query}
    lower_url = url.lower()

    score = 0
    reasons: list[str] = []

    # --- 扩展名判定
    if ext in PHOTO_EXTS:
        kind = "image"
        score += 30
        reasons.append(f"图片扩展名{ext}")
    elif ext in VIDEO_EXTS:
        kind = "direct"
        score += 45
        reasons.append(f"扩展名{ext}")
    elif ext in HLS_EXTS:
        kind = "hls"
        score += 50
        reasons.append("扩展名.m3u8")
    elif ext in DASH_EXTS:
        kind = "dash"
        score += 40
        reasons.append("扩展名.mpd")
    elif ext in SEGMENT_EXTS:
        kind = "segment"
        score += 5
        reasons.append(f"分片{ext}")
    elif ext in AUDIO_EXTS:
        kind = "audio"
        score += 30
        reasons.append(f"音频{ext}")
    elif ext in NON_MEDIA_EXTS:
        return UrlInfo(url, "other", ext, host, -100, [f"非媒体扩展名{ext}"], _identity(url), False, None, False)
    else:
        kind = "other"
        reasons.append("无扩展名")
        # 微信图片经常把格式塞在查询参数里（路径没扩展名）；m3u8 也可能没扩展名
        photo_ext = _photo_ext_hint(lower_url, query)
        if photo_ext:
            kind = "image"
            ext = ext or photo_ext
            score += 28
            reasons.append("按URL特征判定为图片")
        elif ".m3u8" in lower_url:
            kind = "hls"
            ext = ext or ".m3u8"
            score += 45
            reasons.append("URL含m3u8")

    # --- 域名权重（图片不吃负分：图片站/微信图片 CDN 本来就被视频规则扣成负数）
    host_bonus = 0
    for pattern, bonus in MEDIA_HOST_HINTS.items():
        if host == pattern or host.endswith("." + pattern):
            host_bonus = bonus
            reasons.append(f"域名{pattern}({'+' if bonus >= 0 else ''}{bonus})")
            break
    if not host_bonus and "video" in host:
        host_bonus = 12
        reasons.append("域名含video")
    score += max(0, host_bonus) if kind == "image" else host_bonus

    # --- 查询参数（视频专用；图片不要套这些规则）
    signed = any(k in qkeys for k in SIGNED_QUERY_KEYS)
    if signed:
        score += 12
        reasons.append("含CDN签名参数")
    if kind != "image":
        if "filekey" in qkeys or "snsvideodownload" in lower_url:
            score += 15
            reasons.append("视频云下载接口")
            if kind == "other":
                kind = "direct"
                reasons.append("按接口判定为视频")
        if any(k in qkeys for k in ("m", "mimetype")) and "snsvideo" in lower_url:
            score += 10

    # --- 页面链接（不是媒体文件，但可能是入口）
    if "mp.weixin.qq.com" == host and "/s" in parts.path:
        kind = "page"
        score -= 45
        if "video_detail" in lower_url or "vid=" in lower_url:
            reasons.append("公众号视频详情页(网页)")
        else:
            reasons.append("公众号文章页(网页)")

    # --- 明显噪音
    if ext in (".js", ".css", ".json", ".wxapkg"):
        score -= 60
    if len(url) < 24:
        score -= 20

    # --- 签名有效期
    expiry_ts, expired = _parse_expiry(query)
    if expired:
        score -= 40
        reasons.append("签名时间戳已过期")

    identity = _identity(url)
    info = UrlInfo(url, kind, ext, host, score, reasons, identity, expired, expiry_ts, signed)
    return info


def _identity(url: str) -> str:
    """去重键：同一视频可能带不同签名参数，用 host+path+关键标识归并。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    q = parse_qs(parts.query)
    for key in ("sha256", "dis_k", "filekey", "vid", "m", "mimetype"):
        vals = q.get(key)
        if vals:
            return f"{parts.hostname}{parts.path}|{key}={vals[0][:80]}"
    return f"{parts.hostname}{parts.path}"


def _photo_ext_hint(lower_url: str, query: dict) -> str:
    """路径没有扩展名时，用查询参数/路径片段判断是不是 jpg/png，并给出扩展名。

    微信图片 CDN 的典型形态：
        https://mmbiz.qpic.cn/mmbiz_jpg/xxxx/0?wx_fmt=jpeg&from=appmsg
        https://wx.qlogo.cn/mmhead/xxx/132          (头像，无扩展名，靠 path 里的 jpg 片段)
    返回 "" 表示不是图片；否则返回 ".jpg"/".jpeg"/".png"。
    """
    for key in IMAGE_QUERY_KEYS:
        vals = query.get(key)
        if vals and str(vals[0]).lower() in ("jpg", "jpeg", "png"):
            return "." + str(vals[0]).lower()
    m = IMAGE_SNIFF_RE.search(lower_url)
    if m:
        return "." + m.group(0).lstrip(".").rstrip("/?#").split("/")[0]
    return ""


def _parse_expiry(query: dict) -> tuple[int | None, bool]:
    for key in EXPIRY_KEYS:
        vals = query.get(key)
        if not vals:
            continue
        raw = vals[0]
        if not raw.isdigit():
            continue
        val = int(raw)
        if val >= 1_000_000_000:  # 像 Unix 秒
            expired = val < int(datetime.now().timestamp())
            return val, expired
    return None, False


# ---------------------------------------------------------------- 汇总

@dataclass
class UrlReport:
    all_urls: int = 0
    media: list[UrlInfo] = field(default_factory=list)
    images: list[UrlInfo] = field(default_factory=list)
    image_urls: int = 0          # 识别出的图片 URL 总数（含过期）
    images_expired: int = 0
    noise: int = 0
    duplicates: int = 0
    expired: int = 0

    @property
    def downloadable(self) -> list[UrlInfo]:
        return [u for u in self.media if u.is_downloadable]

    @property
    def downloadable_images(self) -> list[UrlInfo]:
        return [u for u in self.images if u.is_downloadable]


def analyze(urls: list[str] | set[str], keep_segments: bool = False,
            want_images: bool = False) -> UrlReport:
    """把清洗后的 URL 集合分类汇总，按可下载性排序。

    want_images=True 时，jpg/jpeg/png 会单独收进 `report.images`（默认不收，
    免得满屏图标）。
    """
    report = UrlReport()
    seen_identity: dict[str, UrlInfo] = {}
    seen_images: dict[str, UrlInfo] = {}
    seen_url: set[str] = set()

    for raw in urls:
        url = clean_url(raw)
        if not url or url in seen_url:
            continue
        seen_url.add(url)
        report.all_urls += 1
        info = classify_url(url)
        if info.kind == "image":
            report.image_urls += 1
            if not want_images:
                report.noise += 1
                continue
            if info.expired:
                report.images_expired += 1
                continue
            existing = seen_images.get(info.identity)
            if existing:
                report.duplicates += 1
                if info.score > existing.score:
                    seen_images[info.identity] = info
                continue
            seen_images[info.identity] = info
            continue
        if info.kind in ("other", "page") or info.score < 0:
            report.noise += 1
            continue
        if info.kind == "segment" and not keep_segments:
            report.noise += 1
            continue
        existing = seen_identity.get(info.identity)
        if existing:
            report.duplicates += 1
            if info.score > existing.score:
                seen_identity[info.identity] = info
            continue
        if info.expired:
            report.expired += 1
        seen_identity[info.identity] = info

    report.media = sorted(seen_identity.values(), key=lambda u: (-u.score, -len(u.url)))
    report.images = sorted(seen_images.values(), key=lambda u: (-u.score, -len(u.url)))
    log.info(
        "URL 分析: 输入 %d 个 -> 媒体 %d 个 (可下载 %d, 过期 %d, 去重 %d), 图片 %d 个, 噪音 %d",
        report.all_urls, len(report.media), len(report.downloadable), report.expired,
        report.duplicates, len(report.images), report.noise,
    )
    for info in report.media[:30]:
        log.info("   %s", info.summary())
    if len(report.media) > 30:
        log.info("   ... 其余 %d 条见下载列表", len(report.media) - 30)
    for info in report.images[:10]:
        log.info("   %s", info.summary())
    if len(report.images) > 10:
        log.info("   ... 其余 %d 张图片见下载列表", len(report.images) - 10)
    return report


def guess_filename(info: UrlInfo, index: int = 0, prefix: str = "") -> str:
    """根据 URL 猜一个安全的文件名。"""
    prefix = prefix or ("wximage" if info.kind == "image" else "wxvideo")
    try:
        parts = urlsplit(info.url)
    except ValueError:
        parts = None
    name = ""
    if parts:
        path = parts.path.rstrip("/")
        name = path.rsplit("/", 1)[-1] if "/" in path else ""
        if not name:
            q = parse_qs(parts.query)
            for key in ("sha256", "filekey", "vid", "m"):
                if key in q:
                    name = f"{key}_{q[key][0][:24]}"
                    break
    if not name:
        name = f"{prefix}_{index:03d}"
    name = sanitize_filename(name)
    known = VIDEO_EXTS | AUDIO_EXTS | HLS_EXTS | DASH_EXTS | PHOTO_EXTS
    if not name.lower().endswith(tuple(known)):
        ext = info.ext or (".jpg" if info.kind == "image" else ".mp4")
        name += ext
    return name


_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize_filename(name: str, max_len: int = 120) -> str:
    name = html.unescape(name or "")
    name = _BAD_CHARS.sub("_", name).strip(" .")
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: max_len - len(ext) - 1] + dot + ext) if dot and len(ext) <= 6 else name[:max_len]
    return name or "unnamed"


__all__ = [
    "UrlInfo", "UrlReport", "iter_urls_from_bytes", "clean_url", "classify_url",
    "analyze", "guess_filename", "sanitize_filename", "path_ext",
    "PHOTO_EXTS", "IMAGE_EXTS", "VIDEO_EXTS", "HLS_EXTS",
]
