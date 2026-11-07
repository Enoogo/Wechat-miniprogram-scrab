"""自检：不依赖微信、不依赖手工点击，把每个模块都跑一遍。

为什么要有它
------------
接手这个项目时，最快的判断方式是 `python run.py --selftest`：
它会告诉你窗口检测、内存扫描、缓存扫描、URL 解析（含图片）、HLS 解析/分片、
下载器、CDP 客户端到底哪一环在当前机器上是通的。

其中下载器、HLS 与 CDP 用**本地假服务**验证（HTTP + WebSocket），
所以即使微信没开着小程序，也能证明这些模块的代码是对的。
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import socketserver
import tempfile
import threading
import time
import traceback
from pathlib import Path

from . import (cache_scan, cdp, config, downloader, hls, logsetup, memory_scan, models, sysinfo,
               urls, winapi, windows)
from .logsetup import human_size

log = logsetup.logging.getLogger("wxs.selftest")

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
FP4 = b"\x00\x00\x00\x20ftypisom" + b"\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * 8
JPEG_HEAD = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.rows.append((name, status, detail))
        mark = {PASS: "✔", FAIL: "✘", SKIP: "○"}[status]
        print(f"  {mark} [{status:4}] {name}" + (f" — {detail}" if detail else ""))
        if status == FAIL:
            log.error("自检失败: %s %s", name, detail)
        else:
            log.info("自检 %s: %s %s", status, name, detail)

    @property
    def failures(self) -> int:
        return sum(1 for _n, s, _d in self.rows if s == FAIL)

    def summary(self) -> int:
        total = len(self.rows)
        fails = self.failures
        skips = sum(1 for _n, s, _d in self.rows if s == SKIP)
        print("\n" + "=" * 70)
        print(f"自检结果: {total} 项，通过 {total - fails - skips}，失败 {fails}，跳过 {skips}")
        print(f"日志文件: {logsetup.log_file_path()}")
        print("=" * 70)
        return 0 if fails == 0 else 1


# ---------------------------------------------------------------- 假服务

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    """假媒体服务：支持 Range（验证断点续传）、图片、以及 HLS 清单/分片。"""

    payload: bytes = b""
    image_payload: bytes = JPEG_HEAD + b"\x00" * (48 * 1024)
    segments: dict[str, bytes] = {}
    media_text: str = ""
    playlists: dict[str, str] = {}

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003
        log.debug("[http-fake] " + fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        # /nope 之类的路径返回 404，用来验证下载器的错误处理
        if "nope" in path:
            self._send(404, b"not found", "text/plain")
            return
        if path.endswith(".m3u8"):
            name = path.rsplit("/", 1)[-1]
            body = (self.playlists.get(name) or self.media_text).encode("utf-8")
            self._send(200, body, "application/vnd.apple.mpegurl")
            return
        if path.endswith(".jpg") or path.endswith(".png"):
            self._send(200, self.image_payload, "image/jpeg")
            return
        seg = self.segments.get(path.rsplit("/", 1)[-1])
        if seg is not None:
            self._send(200, seg, "video/mp2t")
            return

        data = self.payload
        total = len(data)
        rng = self.headers.get("Range")
        start = 0
        if rng and rng.startswith("bytes="):
            try:
                start = int(rng.split("=", 1)[1].split("-", 1)[0])
            except ValueError:
                start = 0
            body = data[start:]
            self._send(206, body, "video/mp4",
                       {"Content-Range": f"bytes {start}-{total - 1}/{total}",
                        "Accept-Ranges": "bytes"})
            return
        self._send(200, data, "video/mp4", {"Accept-Ranges": "bytes"})


class _JsonEndpoint(http.server.BaseHTTPRequestHandler):
    """假的 CDP HTTP 端点：/json/version 与 /json/list。"""

    ws_base: str = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003
        log.debug("[cdp-http] " + fmt, *args)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/json/version"):
            body = {
                "Browser": "FakeWMPF/1.0.0",
                "Protocol-Version": "1.3",
                "webSocketDebuggerUrl": f"{self.ws_base}/devtools/browser/fake",
            }
        elif self.path.startswith("/json/list"):
            body = [
                {
                    "type": "page",
                    "title": "fake mini program page",
                    "url": "https://servicewechat.com/wx1234/0/page-frame.html",
                    "webSocketDebuggerUrl": f"{self.ws_base}/devtools/page/fake1",
                }
            ]
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class ThreadedServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001
        """客户端提前断开（ConnectionReset 等）是测试里的正常现象，不该刷 traceback。"""
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            log.debug("[fake-server] 客户端断开: %s", exc)
            return
        super().handle_error(request, client_address)


def _start_http_server(handler_cls, port: int = 0) -> tuple[ThreadedServer, int]:
    srv = ThreadedServer(("127.0.0.1", port), handler_cls)
    t = threading.Thread(target=srv.serve_forever, name="fake-http", daemon=True)
    t.start()
    return srv, srv.server_address[1]


def _start_fake_ws(port: int) -> object:
    """用 websockets 的同步 server 起一个假 CDP WebSocket。"""
    from websockets.sync.server import serve

    def handler(ws) -> None:  # noqa: ANN001
        try:
            for raw in ws:
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                mid = msg.get("id")
                method = msg.get("method")
                log.debug("[fake-cdp] 收到 %s id=%s", method, mid)
                if method == "Runtime.evaluate":
                    value = json.dumps([
                        "https://mpvideo.qpic.cn/fake_eval.mp4?dis_t=9999999999",
                    ])
                    ws.send(json.dumps({"id": mid, "result": {"result": {"type": "string", "value": value}}}))
                elif method == "Network.enable":
                    ws.send(json.dumps({"id": mid, "result": {}}))
                    ws.send(json.dumps({
                        "method": "Network.requestWillBeSent",
                        "params": {"request": {"url": "https://mpvideo.qpic.cn/fake_event.mp4?dis_t=9999999999&sha256=abc"}},
                    }))
                    ws.send(json.dumps({
                        "method": "Network.responseReceived",
                        "params": {"response": {"url": "https://mpvideo.qpic.cn/fake_resp.mp4", "mimeType": "video/mp4", "status": 200}},
                    }))
                else:
                    ws.send(json.dumps({"id": mid, "result": {}}))
        except Exception as exc:  # noqa: BLE001
            log.debug("[fake-cdp] 连接结束: %s", exc)

    server = serve(handler, "127.0.0.1", port, max_size=None)
    threading.Thread(target=server.serve_forever, name="fake-cdp-ws", daemon=True).start()
    return server


# ---------------------------------------------------------------- 各项检查

def check_imports(rep: Report) -> None:
    try:
        import psutil  # noqa: F401
        import requests  # noqa: F401

        rep.add("依赖库 (psutil/requests)", PASS, f"psutil={psutil.__version__} requests={requests.__version__}")
    except ImportError as exc:
        rep.add("依赖库 (psutil/requests)", FAIL, f"{exc} -> pip install -r requirements.txt")
        return
    try:
        import websocket  # noqa: F401

        rep.add("websocket-client (CDP 需要)", PASS, "已安装")
    except ImportError:
        rep.add("websocket-client (CDP 需要)", SKIP, "未安装，仅 CDP 通道受影响")
    try:
        import tkinter

        rep.add("tkinter (GUI 需要)", PASS, f"Tk {tkinter.TkVersion}")
    except ImportError as exc:
        rep.add("tkinter (GUI 需要)", FAIL, str(exc))


def check_urls(rep: Report) -> None:
    cases = [
        (
            "粘连 URL 切分",
            "https://mp.weixin.qq.com/s/AbChttps://mp.weixin.qq.com/s?t=pages/video_detail",
            lambda u: u == "https://mp.weixin.qq.com/s/AbC",
        ),
        (
            "JSON 转义还原",
            'https:\\/\\/mpvideo.qpic.cn\\/a.mp4?dis_k=1\\u0026dis_t=2',
            lambda u: u.startswith("https://mpvideo.qpic.cn/a.mp4"),
        ),
        (
            "尾部标点清理",
            "https://mpvideo.qpic.cn/x.mp4?sha256=ff.',",
            lambda u: u.endswith("sha256=ff"),
        ),
    ]
    ok = True
    for name, raw, pred in cases:
        got = urls.clean_url(raw)
        if not pred(got):
            ok = False
            rep.add(f"URL 清洗: {name}", FAIL, f"输入={raw!r} 输出={got!r}")
        else:
            log.debug("URL 清洗通过 %s -> %s", name, got)
    if ok:
        rep.add("URL 清洗 (3 个用例)", PASS)

    media = urls.classify_url(
        "https://wxsmw.wxs.qq.com/131/20210/snssvpdownload/SH/x.mp4"
        "?dis_k=1&dis_t=4102444800&sha256=abc"   # dis_t=2100-01-01，避免测试用例自己过期
    )
    if media.kind == "direct" and media.score > 50 and media.is_downloadable:
        rep.add("分类: 微信 CDN 直链", PASS, f"score={media.score} 理由={'/'.join(media.reasons)}")
    else:
        rep.add("分类: 微信 CDN 直链", FAIL, f"kind={media.kind} score={media.score} expired={media.expired}")

    expired = urls.classify_url("https://mpvideo.qpic.cn/old.mp4?dis_t=1600000000")
    rep.add("分类: 过期签名识别", PASS if expired.expired and not expired.is_downloadable else FAIL,
            f"expired={expired.expired} score={expired.score}")

    hls = urls.classify_url("https://example.com/live/index.m3u8?token=abc")
    rep.add("分类: m3u8", PASS if hls.kind == "hls" else FAIL, f"kind={hls.kind} score={hls.score}")

    page = urls.classify_url("https://mp.weixin.qq.com/s?t=pages/video_detail_new&vid=wxv_1")
    rep.add("分类: 公众号页面被降权", PASS if page.kind == "page" and not page.is_downloadable else FAIL,
            f"kind={page.kind} score={page.score}")

    noise = urls.classify_url("https://res.wx.qq.com/wxdoc/dist/assets/index.js")
    rep.add("分类: js 被排除", PASS if not noise.is_downloadable else FAIL, f"kind={noise.kind}")

    utf16 = "https://mpvideo.qpic.cn/utf16.mp4?dis_t=1".encode("utf-16-le")
    blob = b"\x00" * 32 + utf16 + b"\x00" * 32
    got = urls.iter_urls_from_bytes(blob, include_utf16=True)
    rep.add("UTF-16LE 内存字符串提取", PASS if any("utf16.mp4" in u for u in got) else FAIL,
            f"提取到 {len(got)} 条")

    dup = urls.analyze({
        "https://mpvideo.qpic.cn/a.mp4?dis_k=1&sha256=zzz",
        "https://mpvideo.qpic.cn/a.mp4?dis_k=2&sha256=zzz",
    })
    rep.add("按 sha256 去重", PASS if len(dup.media) == 1 else FAIL, f"媒体条目={len(dup.media)}")

    # --- 图片（JPG/PNG）
    img_ext = urls.classify_url("https://mmbiz.qpic.cn/mmbiz_jpg/abc/640?wx_fmt=jpeg")
    rep.add("分类: 图片扩展名", PASS if img_ext.kind == "image" and img_ext.is_downloadable else FAIL,
            f"kind={img_ext.kind} score={img_ext.score}")
    img_q = urls.classify_url("https://mmbiz.qpic.cn/mmbiz_png/abc/0?wx_fmt=png")
    rep.add("分类: 图片(参数识别)", PASS if img_q.kind == "image" else FAIL,
            f"kind={img_q.kind} ext={img_q.ext}")
    webp = urls.classify_url("https://mmbiz.qpic.cn/a/0?wx_fmt=webp")
    rep.add("分类: webp 仍算噪音", PASS if webp.kind != "image" else FAIL, f"kind={webp.kind}")
    mixed = urls.analyze({"https://a.example/x.mp4", "https://a.example/y.jpg"}, want_images=True)
    rep.add("analyze 分离视频/图片",
            PASS if len(mixed.media) == 1 and len(mixed.images) == 1 else FAIL,
            f"媒体={len(mixed.media)} 图片={len(mixed.images)}")

    # --- m3u8 没有路径扩展名时也要能认出来
    hls_q = urls.classify_url("https://encrypt-k-vod.xet.tech/v.f100230.m3u8?sign=abc")
    rep.add("分类: m3u8(带签名)", PASS if hls_q.kind == "hls" else FAIL, f"kind={hls_q.kind}")


def check_config(rep: Report) -> None:
    """配置文件：能读、能生成、关键默认值符合预期。"""
    try:
        cfg = config.load_config()
        ch = cfg.get("channels", {})
        num = cfg.get("numbers", {})
        ok_shape = all(k in ch for k in ("memory", "cache_files", "cache_urls", "cdp", "images")) \
            and "scan_budget_mb" in num and "concurrency" in num
        rep.add("config.json 读取", PASS if ok_shape else FAIL,
                f"文件={config.CONFIG_FILE} 存在={config.CONFIG_FILE.exists()}；"
                f"{config.config_summary(cfg)}")
        settings = config.load_settings(cfg)
        rep.add("当前配置能构建 Settings",
                PASS if isinstance(settings.scan_budget_mb, int) else FAIL,
                f"预算={settings.scan_budget_mb}MB 并发={settings.concurrency} "
                f"图片下限={settings.min_image_kb}KB")
        # 内置默认值（不受用户改 config.json 影响）：内存/本地缓存/CDP/图片 应当默认关
        dch = config.DEFAULT_CONFIG["channels"]
        defaults_off = [k for k in ("memory", "cache_files", "cdp", "images") if not dch[k]]
        rep.add("内置默认关闭 内存/本地缓存/CDP/图片",
                PASS if len(defaults_off) == 4 else FAIL, f"默认关闭={defaults_off}")
    except Exception:  # noqa: BLE001
        rep.add("config.json 读取", FAIL, traceback.format_exc(limit=3))


def check_tools(rep: Report, settings) -> None:
    ff = sysinfo.find_ffmpeg(settings)
    if ff:
        rep.add("ffmpeg", PASS, f"{ff} | {sysinfo.version_of(ff)[:60]}")
    else:
        rep.add("ffmpeg", FAIL, "未找到 —— HLS 合并与下载后体检不可用")
    fp = sysinfo.find_ffprobe(settings)
    rep.add("ffprobe", PASS if fp else SKIP, fp or "未找到（下载后不探测媒体信息）")


def check_windows(rep: Report) -> object | None:
    try:
        result = windows.detect()
    except Exception as exc:  # noqa: BLE001
        rep.add("窗口检测", FAIL, f"{type(exc).__name__}: {exc}")
        return None
    if result.count:
        cand = result.candidates[0]
        rep.add("窗口检测", PASS, f"{result.count} 个候选；首个={cand.title!r} hwnd={hex(cand.hwnd)} "
                                 f"pid={cand.pid} score={cand.score}")
        return cand.info
    rep.add("窗口检测", SKIP, "没有小程序窗口（请在微信里打开一个小程序后重跑）")
    for n in result.notes:
        log.info("   提示: %s", n)
    return None


def check_process_tree(rep: Report, window) -> None:
    if window is None:
        rep.add("进程树", SKIP, "没有窗口")
        return
    try:
        tree = windows.process_tree(window.pid)
        rep.add("进程树", PASS if len(tree) >= 1 else FAIL,
                f"{len(tree)} 个进程: " + ", ".join(f"{p['pid']}({p['type']})" for p in tree[:8]))
    except Exception as exc:  # noqa: BLE001
        rep.add("进程树", FAIL, str(exc))


def check_memory_scan(rep: Report, window, settings, quick: bool) -> None:
    if window is None:
        rep.add("内存扫描", SKIP, "没有窗口")
        return
    if quick:
        rep.add("内存扫描", SKIP, "--quick 模式跳过")
        return
    try:
        tree = windows.process_tree(window.pid)
        url_set, results = memory_scan.scan_targets(tree, total_budget_mb=120)
        scanned = sum(r.bytes_read for r in results)
        fails = sum(r.read_failures for r in results)
        rep.add("内存扫描", PASS if url_set else FAIL,
                f"读取 {human_size(scanned)} 失败区块 {fails} 原始URL {len(url_set)} 条")
        if url_set:
            report = urls.analyze(url_set)
            rep.add("内存扫描 -> 媒体", PASS if report.downloadable else SKIP,
                    f"媒体 {len(report.media)} 条，可下载 {len(report.downloadable)} 条；"
                    f"噪音 {report.noise} 条")
    except Exception:  # noqa: BLE001
        rep.add("内存扫描", FAIL, traceback.format_exc(limit=3))


def check_cache(rep: Report) -> None:
    try:
        files = cache_scan.scan_applet_local()
        total = sum(f.size or 0 for f in files)
        rep.add("本地缓存文件扫描", PASS if files else SKIP,
                f"{len(files)} 个媒体文件，共 {human_size(total)}" if files else "没找到缓存视频（正常，取决于用过哪些小程序）")
        if files:
            fp = sysinfo.find_ffprobe(config.DEFAULT_SETTINGS)
            if fp:
                rep.add("缓存文件可播放性", PASS if sysinfo.ffprobe_summary(fp, files[0].path) else SKIP,
                        sysinfo.ffprobe_summary(fp, files[0].path)[:120])
    except Exception:  # noqa: BLE001
        rep.add("本地缓存文件扫描", FAIL, traceback.format_exc(limit=3))
    try:
        found = cache_scan.scan_chromium_cache(max_file_mb=24, max_total_mb=192)
        rep.add("Chromium 缓存 URL", PASS if found else SKIP, f"{len(found)} 条媒体 URL")
    except Exception:  # noqa: BLE001
        rep.add("Chromium 缓存 URL", FAIL, traceback.format_exc(limit=3))


def _make_sample_mp4(settings, tmp: Path) -> tuple[bytes, bool]:
    """用 ffmpeg 现造一段 2 秒的真 mp4 当下载载荷。

    为什么要真的：下载后体检会用 ffprobe/ffmpeg 解码开头，
    假数据（只有 ftyp + 随机字节）必然被判成“可疑”，测试就失去意义了。
    没有 ffmpeg 时退回假数据（此时体检查不了，不会误报）。
    """
    ff = sysinfo.find_ffmpeg(settings)
    if not ff:
        return b"", False
    out = tmp / "sample_src.mp4"
    cmd = [
        ff, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-t", "2", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-movflags", "+faststart", str(out),
    ]
    import subprocess

    try:
        subprocess.run(cmd, capture_output=True, timeout=90)
        if out.exists() and out.stat().st_size > 4000:
            log.info("自检用真 mp4 已生成: %s 字节", out.stat().st_size)
            return out.read_bytes(), True
    except Exception as exc:  # noqa: BLE001
        log.warning("生成自检用 mp4 失败: %s", exc)
    return b"", False


def check_download(rep: Report, settings, tmp: Path) -> None:
    real_payload, is_real = _make_sample_mp4(settings, tmp)
    payload = real_payload or (FP4 + os.urandom(2 * 1024 * 1024))
    _RangeHandler.payload = payload
    srv, port = _start_http_server(_RangeHandler)
    try:
        url = f"http://127.0.0.1:{port}/video.mp4"
        out = tmp / "dl"
        out.mkdir(parents=True, exist_ok=True)

        test_settings = config.Settings(out_dir=out, concurrency=1, http_timeout=10.0)
        test_settings.ffmpeg_path = sysinfo.find_ffmpeg(settings)
        item = models.MediaItem(
            source="自检", kind="remote", url=url, title="selftest_video.mp4", ext=".mp4", kind_label="直链视频"
        )
        dl = downloader.Downloader(test_settings)
        t0 = time.time()
        dl.run([item])
        st = dl.states[item.uid]
        got = Path(st.out_path) if st.out_path else None
        ok_size = got and got.exists() and got.stat().st_size == len(payload)
        rep.add("HTTP 下载 + 校验", PASS if st.ok and ok_size and not st.warn else FAIL,
                f"状态={st.status} 大小={(got.stat().st_size if got and got.exists() else 0)}/{len(payload)} "
                f"耗时={time.time() - t0:.2f}s 体检={st.warn or '正常'}"
                + ("（真 mp4）" if is_real else "（无 ffmpeg，假数据）"))

        # 断点续传：手工造一个 .part，再下载应走 Range
        url2 = f"http://127.0.0.1:{port}/video2.mp4"
        item2 = models.MediaItem(source="自检", kind="remote", url=url2, title="selftest_resume.mp4", ext=".mp4")
        dest2 = out / "selftest_resume.mp4"
        part2 = dest2.with_suffix(dest2.suffix + ".part")
        part2.write_bytes(payload[: len(payload) // 2])
        dl2 = downloader.Downloader(test_settings)
        dl2.run([item2])
        st2 = dl2.states[item2.uid]
        resumed_ok = (st2.ok and dest2.exists() and dest2.stat().st_size == len(payload)
                      and not st2.warn)
        rep.add("断点续传 (Range)", PASS if resumed_ok else FAIL,
                f"状态={st2.status} 最终大小={dest2.stat().st_size if dest2.exists() else 0}/{len(payload)}")

        # 本地缓存复制
        src_file = tmp / "fake_cached.mp4"
        src_file.write_bytes(payload)
        item3 = models.MediaItem.from_local(src_file, source="自检", appid="wxtest")
        dl3 = downloader.Downloader(test_settings)
        dl3.run([item3])
        st3 = dl3.states[item3.uid]
        copied = Path(st3.out_path) if st3.out_path else None
        rep.add("本地缓存复制", PASS if st3.ok and not st3.warn and copied
                and copied.stat().st_size == src_file.stat().st_size else FAIL,
                f"状态={st3.status} -> {copied} 体检={st3.warn or '正常'}")

        # 错误路径：404 应该干净地失败而不是崩
        bad = models.MediaItem(source="自检", kind="remote", url=f"http://127.0.0.1:{port}/nope.mp4",
                               title="selftest_404.mp4", ext=".mp4")
        test_settings.http_retries = 1
        dl4 = downloader.Downloader(test_settings)
        dl4.run([bad])
        st4 = dl4.states[bad.uid]
        rep.add("HTTP 404 优雅失败", PASS if st4.status == "失败" and st4.error else FAIL,
                f"状态={st4.status} 错误={st4.error}")

        # 图片下载（jpg/png 支持）
        img_item = models.MediaItem(source="自检", kind="remote", media_type="image",
                                    url=f"http://127.0.0.1:{port}/photo.jpg",
                                    title="selftest_photo.jpg", ext=".jpg", kind_label="图片")
        dl5 = downloader.Downloader(test_settings)
        dl5.run([img_item])
        st5 = dl5.states[img_item.uid]
        img_path = Path(st5.out_path) if st5.out_path else None
        rep.add("图片下载 (JPEG)",
                PASS if st5.ok and img_path and img_path.exists()
                and img_path.read_bytes()[:3] == b"\xff\xd8\xff" else FAIL,
                f"状态={st5.status} 文件={img_path.name if img_path else '-'} 告警={st5.warn or '无'}")

        # 完整性体检：全是零的“空壳”文件必须被指出来（用户遇到过“时长对但没数据”）
        bad_media = tmp / "hollow.mp4"
        bad_media.write_bytes(FP4 + b"\x00" * (2 * 1024 * 1024))
        hollow_item = models.MediaItem(source="自检", kind="remote", url="http://x/hollow.mp4",
                                      title="hollow.mp4", ext=".mp4", kind_label="直链视频")
        dl6 = downloader.Downloader(test_settings)
        warn = dl6._check_integrity(hollow_item, bad_media)
        rep.add("完整性体检识别空壳文件", PASS if ("空白" in warn or "报错" in warn) else FAIL,
                warn or "没检测出来")
    except Exception:  # noqa: BLE001
        rep.add("HTTP 下载", FAIL, traceback.format_exc(limit=4))
    finally:
        srv.shutdown()


def check_cdp(rep: Report) -> None:
    try:
        import websocket  # noqa: F401
    except ImportError:
        rep.add("CDP 客户端", SKIP, "未安装 websocket-client")
        return
    try:
        ws_port = _free_port()
        _JsonEndpoint.ws_base = f"ws://127.0.0.1:{ws_port}"
        ws_server = _start_fake_ws(ws_port)
        http_srv, http_port = _start_http_server(_JsonEndpoint)
        time.sleep(0.3)
        try:
            ep = cdp.probe_port(http_port)
            if not ep:
                rep.add("CDP 端点探测", FAIL, f"假端点 port={http_port} 未被识别")
                return
            rep.add("CDP 端点探测", PASS,
                    f"/json/version={ep.version.get('Browser')} targets={len(ep.targets)}")
            pages = ep.pages()
            if not pages:
                rep.add("CDP target 枚举", FAIL, "没有 page target")
                return
            result = cdp.sniff_target(pages[0]["webSocketDebuggerUrl"], seconds=1.5)
            joined = "\n".join(sorted(result.urls))
            got_event = "fake_event.mp4" in joined
            got_resp = "fake_resp.mp4" in joined
            got_eval = "fake_eval.mp4" in joined
            rep.add("CDP 抓取 (事件+响应+页面内)",
                    PASS if (got_event and got_eval) else FAIL,
                    f"事件={got_event} 响应={got_resp} 页面内={got_eval} 共{len(result.urls)}条")
        finally:
            http_srv.shutdown()
            try:
                ws_server.shutdown()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        rep.add("CDP 客户端", FAIL, traceback.format_exc(limit=4))


MASTER_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,CODECS="avc1.4d401e,mp4a.40.2"
media_360.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2"
media_720.m3u8
"""

MEDIA_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:7
#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x0123456789abcdef0123456789abcdef
#EXTINF:4.000,
seg0.ts
#EXTINF:4.000,
seg1.ts
#EXTINF:3.500,
seg2.ts
#EXT-X-ENDLIST
"""


def check_hls_parse(rep: Report) -> None:
    """纯解析：主清单/媒体清单/加密/序列号/时长。"""
    master = hls.parse_playlist(MASTER_PLAYLIST, "https://cdn.example/v/master.m3u8")
    ok_master = (master.ok and master.is_master and len(master.variants) == 2
                 and master.variants[1].resolution == "1280x720"
                 and master.variants[0].url == "https://cdn.example/v/media_360.m3u8")
    rep.add("HLS 主清单解析", PASS if ok_master else FAIL,
            f"变体={len(master.variants)} 首个={master.variants[0].url if master.variants else '-'}")

    media = hls.parse_playlist(MEDIA_PLAYLIST, "https://cdn.example/v/media_720.m3u8")
    key_ok = all(s.key is not None and s.key.method == "AES-128" for s in media.segments)
    ok_media = (media.ok and not media.is_master and len(media.segments) == 3
                and abs(media.total_duration - 11.5) < 0.01
                and media.media_sequence == 7 and key_ok
                and media.segments[0].url == "https://cdn.example/v/seg0.ts")
    rep.add("HLS 媒体清单解析", PASS if ok_media else FAIL,
            f"分片={len(media.segments)} 时长={media.total_duration}s "
            f"序列={media.media_sequence} 密钥延续={key_ok}")

    # 本地清单生成（关键：不能把加密/序列号/分片顺序写错）
    import tempfile as _tf

    with _tf.TemporaryDirectory(prefix="wxs_hls_") as td:
        path = Path(td) / "local.m3u8"
        hls._write_local_playlist(media, [f"seg_{i:05d}.ts" for i in range(3)],
                                  {"https://cdn.example/v/key.bin": "key_0.key"}, {}, path)
        text = path.read_text(encoding="utf-8")
        good = (text.count("#EXTINF") == 3 and 'URI="key_0.key"' in text
                and "#EXT-X-MEDIA-SEQUENCE:7" in text and text.rstrip().endswith("#EXT-X-ENDLIST"))
        rep.add("HLS 本地清单生成", PASS if good else FAIL, f"{len(text)} 字节")


def check_hls_download(rep: Report, settings) -> None:
    """分片下载 + 主清单展开：真的从假服务器拉 m3u8 + 分片。"""
    segs = {f"seg{i}.ts": os.urandom(200 * 1024 + i) for i in range(3)}
    _RangeHandler.segments = segs
    media_text = (
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n"
        + "".join(f"#EXTINF:4.000,\nseg{i}.ts\n" for i in range(3))
        + "#EXT-X-ENDLIST\n"
    )
    master_text = (
        "#EXTM3U\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,CODECS="avc1.4d401e"\n'
        "media_360.m3u8\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720,CODECS="avc1.4d401f"\n'
        "media_720.m3u8\n"
    )
    _RangeHandler.media_text = media_text
    _RangeHandler.playlists = {
        "master.m3u8": master_text,
        "media_360.m3u8": media_text,
        "media_720.m3u8": media_text,
    }
    srv, port = _start_http_server(_RangeHandler)
    try:
        base = f"http://127.0.0.1:{port}/media.m3u8"
        pl = hls.fetch_playlist(base, settings)
        rep.add("HLS 清单 HTTP 抓取", PASS if pl.ok and len(pl.segments) == 3 else FAIL,
                f"分片={len(pl.segments)} 错误={pl.error or '-'}")
        if pl.ok:
            with tempfile.TemporaryDirectory(prefix="wxs_hlsdl_") as td:
                ok_all = True
                got = 0
                for seg in pl.segments:
                    dest = Path(td) / f"s{seg.index}.ts"
                    ok, n, err = hls._download_one(seg.url, dest, settings, None,
                                                   referer=base)
                    if not ok or not dest.exists():
                        ok_all = False
                        rep.add("HLS 分片下载", FAIL, f"第 {seg.index} 片: {err}")
                        break
                    got += n
                if ok_all:
                    expect = sum(len(v) for v in segs.values())
                    rep.add("HLS 分片下载 + 大小校验", PASS if got == expect else FAIL,
                            f"{got}/{expect} 字节（{human_size(got)}）")

        # “自动获取所有 m3u8”：主清单要展开成每个清晰度一条
        master_item = models.MediaItem(source="自检", kind="remote",
                                       url=f"http://127.0.0.1:{port}/master.m3u8",
                                       title="master.m3u8", ext=".m3u8",
                                       kind_label="HLS 播放列表")
        new_items, replaced, notes = hls.expand_hls_items([master_item], settings)
        ok_expand = (len(new_items) == 2 and len(replaced) == 1
                     and any("1280x720" in it.title for it in new_items)
                     and all(it.url.endswith(".m3u8") for it in new_items))
        rep.add("m3u8 主清单自动展开", PASS if ok_expand else FAIL,
                f"展开出 {len(new_items)} 条：{[it.title for it in new_items]}")
    except Exception:  # noqa: BLE001
        rep.add("HLS 分片下载", FAIL, traceback.format_exc(limit=4))
    finally:
        _RangeHandler.playlists = {}
        srv.shutdown()


def check_snapshot(rep: Report, window, settings) -> None:
    """PrintWindow 截图（预览窗口用）——原来这里是录制命令检查。"""
    if window is None:
        rep.add("窗口截图 (PrintWindow)", SKIP, "没有窗口")
        return
    try:
        from . import capture

        path = capture.grab_window_png(window, Path(tempfile.gettempdir()) / "wxs_snap_check.png",
                                       max_width=400)
        from PIL import Image
        import numpy as _np

        arr = _np.asarray(Image.open(path).convert("L"))
        rep.add("窗口截图 (PrintWindow)", PASS if arr.std() > 2 else FAIL,
                f"{arr.shape[1]}x{arr.shape[0]} 灰度均值={arr.mean():.1f} 标准差={arr.std():.2f}"
                f"（>2 表示抓到真实内容）")
        try:
            os.unlink(path)   # 自检产物当场删掉，不留在临时目录里
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001
        rep.add("窗口截图 (PrintWindow)", FAIL, f"{type(exc).__name__}: {exc}")


def check_gui(rep: Report) -> None:
    try:
        from . import gui  # noqa: F401

        rep.add("GUI 模块导入", PASS, "wxs.gui 可导入")
    except Exception as exc:  # noqa: BLE001
        rep.add("GUI 模块导入", FAIL, traceback.format_exc(limit=3))

    # 尝试真的建一次 Tk 窗口（无显示环境会失败，属于正常跳过）
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        root.update()
        root.destroy()
        rep.add("GUI 可创建窗口", PASS)
    except Exception as exc:  # noqa: BLE001
        rep.add("GUI 可创建窗口", SKIP, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- 入口

def run_selftest(args=None) -> int:
    quick = bool(getattr(args, "quiet", False))
    settings = config.load_settings()
    print("=" * 70)
    print(f"微信小程序视频/图片下载器 自检 v{__import__('wxs').__version__}")
    print(f"日志: {logsetup.log_file_path()}")
    print(f"配置文件: {config.CONFIG_FILE}")
    print("=" * 70)

    rep = Report()
    winapi.set_dpi_awareness()
    rep.add("DPI 感知", PASS, winapi.set_dpi_awareness())

    print("\n[1/9] 依赖、工具与配置")
    check_imports(rep)
    check_tools(rep, settings)
    check_config(rep)

    print("\n[2/9] URL 解析（视频/图片/m3u8）")
    check_urls(rep)

    print("\n[3/9] HLS 解析与分片下载")
    check_hls_parse(rep)
    check_hls_download(rep, settings)

    print("\n[4/9] 窗口检测")
    window = check_windows(rep)
    check_process_tree(rep, window)

    print("\n[5/9] 本地缓存")
    check_cache(rep)

    print("\n[6/9] 内存扫描")
    check_memory_scan(rep, window, settings, quick=quick)

    print("\n[7/9] 下载器（本地假 HTTP 服务）")
    with tempfile.TemporaryDirectory(prefix="wxs_selftest_") as td:
        check_download(rep, settings, Path(td))

    print("\n[8/9] CDP 客户端（本地假 DevTools 服务）")
    check_cdp(rep)

    print("\n[9/9] 窗口截图与 GUI")
    check_snapshot(rep, window, settings)
    check_gui(rep)

    return rep.summary()


__all__ = ["run_selftest", "Report"]
