"""HLS 分片下载器的端到端验证（需要 ffmpeg）。

自检里只能验证“解析 + 单片下载 + 本地清单生成”，因为自检的假分片不是真媒体，
没法交给 ffmpeg 转封装。这个脚本补上最后一环：

1. 用 ffmpeg 现场生成一段 6 秒的 HLS 点播流（`-f hls`，2 秒一片）；
2. 起一个本地 HTTP 服务把它当成真 CDN；
3. 走 `downloader.Downloader` 下载它（也就是 GUI 里点“全部m3u8”的路径）；
4. 校验：状态、文件存在、ffprobe 时长、体检是否告警。

    python scratch/hls_e2e.py            # 明文 HLS（走内置分片下载器）
    python scratch/hls_e2e.py --encrypt  # AES-128 加密 HLS（验证密钥下载+解密）
    python scratch/hls_e2e.py --fallback # 强制走 ffmpeg 直连（验证兜底路径）

产物写在临时目录里，脚本结束自动删。
"""
from __future__ import annotations

import functools
import http.server
import io
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from wxs import config, downloader, logsetup, models, sysinfo  # noqa: E402

logsetup.setup(level="DEBUG", console_level="WARNING", gui=False)

ENCRYPT = "--encrypt" in sys.argv
FALLBACK = "--fallback" in sys.argv


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """支持 Range 的静态文件服务（HLS 分片有时会带 Range）。"""

    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def send_head(self):  # noqa: N802
        return super().send_head()


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def make_hls(directory: Path, seconds: int = 6) -> Path:
    ffmpeg = sysinfo.find_ffmpeg()
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=15",
        "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=44100",
        "-t", str(seconds),
        "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "64k",
        "-f", "hls", "-hls_time", "2", "-hls_list_size", "0",
        "-hls_segment_filename", str(directory / "seg%03d.ts"),
    ]
    if ENCRYPT:
        key = directory / "enc.key"
        key.write_bytes(os.urandom(16))
        info = directory / "key_info.txt"
        # key_info 第一行是 **写进 m3u8 里的 URI**，必须是相对/HTTP 的形式；
        # 写绝对 Windows 路径会被 ffmpeg 当成转义序列吃掉（反斜杠）。
        info.write_text(f"enc.key\n{key}\n", encoding="utf-8")
        cmd += ["-hls_key_info_file", str(info)]
    cmd.append(str(directory / "index.m3u8"))
    print("[i] 生成 HLS:", subprocess.list2cmdline(cmd))
    subprocess.run(cmd, check=True)
    return directory / "index.m3u8"


def main() -> int:
    if not sysinfo.find_ffmpeg():
        print("[!] 没有 ffmpeg，跳过")
        return 2
    settings = config.load_settings()
    settings.concurrency = 1
    fails = 0
    with tempfile.TemporaryDirectory(prefix="wxs_hlse2e_") as td:
        work = Path(td) / "cdn"
        work.mkdir()
        make_hls(work)
        index = work / "index.m3u8"
        print("[i] 清单内容:")
        print("    " + index.read_text(encoding="utf-8").replace("\n", "\n    ")[:800])

        handler = functools.partial(_QuietHandler, directory=str(work))
        srv = _Server(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        url = f"http://127.0.0.1:{port}/index.m3u8"
        print(f"[i] 本地 CDN: {url}")

        out = Path(td) / "out"
        out.mkdir()
        settings.out_dir = out
        settings.ffmpeg_path = sysinfo.find_ffmpeg()
        settings.ffprobe_path = sysinfo.find_ffprobe()
        settings.native_hls = not FALLBACK
        item = models.MediaItem(source="e2e", kind="remote", url=url,
                               title="e2e_hls.m3u8", ext=".m3u8", kind_label="HLS 播放列表")
        dl = downloader.Downloader(settings)
        states = dl.run([item])
        st = states[item.uid]
        srv.shutdown()

        path = Path(st.out_path) if st.out_path else None
        dur = sysinfo.ffprobe_duration(settings.ffprobe_path, path) if path and path.exists() else None
        print(f"\n[结果] 状态={st.status} 文件={path}")
        print(f"[结果] 大小={path.stat().st_size if path and path.exists() else 0}B "
              f"时长={dur} 告警={st.warn or '无'} 错误={st.error or '无'}")
        ok = bool(st.ok and path and path.exists() and (dur or 0) >= 5.0)
        mode = ("明文" if not ENCRYPT else "AES-128") + ("/ffmpeg 兜底" if FALLBACK else "/分片下载器")
        print(("✔ PASS" if ok else "✘ FAIL") + f"  {mode} HLS 端到端下载")
        fails += 0 if ok else 1
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
