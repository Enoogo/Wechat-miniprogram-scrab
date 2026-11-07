"""真实远端链接下载验证：先 HEAD 看体积，只下载小于阈值的，避免拉大文件。"""
import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import requests  # noqa: E402

from wxs import config, downloader, logsetup, probe, winapi  # noqa: E402

logsetup.setup(level="DEBUG", console_level="WARNING", gui=False)
winapi.set_dpi_awareness()

settings = config.load_settings()   # 走 config.json 的默认值
settings.out_dir = ROOT / "scratch" / "dl_remote"
settings.out_dir.mkdir(parents=True, exist_ok=True)

opts = probe.ProbeOptions(use_memory=True, use_cache_files=False, use_cache_urls=True,
                          use_cdp=False, use_images=False)
result = probe.probe(None, settings, opts)
remote = [i for i in result.items if i.kind == "remote"]
print(f"\n远端条目 {len(remote)} 个，逐个 HEAD 探测：")

picked = []
for idx, item in enumerate(remote, 1):
    try:
        r = requests.head(item.url, timeout=12, allow_redirects=True,
                          headers={"User-Agent": settings.user_agent, "Referer": settings.referer})
        size = r.headers.get("Content-Length", "?")
        ctype = r.headers.get("Content-Type", "?")
        print(f"  [{idx}] {r.status_code} {ctype} size={size} {item.url[:100]}")
        if r.status_code < 400 and str(size).isdigit() and int(size) < 30 * 1024 * 1024:
            picked.append(item)
        elif r.status_code < 400 and not str(size).isdigit():
            picked.append(item)  # HLS 没有 Content-Length
    except Exception as exc:  # noqa: BLE001
        print(f"  [{idx}] HEAD 失败: {type(exc).__name__}: {exc}")

print(f"\n选中下载 {len(picked)} 个（<30MB 或无长度信息）")
if not picked:
    print("没有可下载的小文件，跳过实际下载验证")
    sys.exit(0)

dl = downloader.Downloader(settings)
t0 = time.time()
states = dl.run(picked)
print(f"\n耗时 {time.time() - t0:.1f}s")
for uid, st in states.items():
    print(f"  {st.status:6} {st.progress_label():24} {Path(st.out_path).name if st.out_path else '-'} "
          f"{st.warn or st.error}")
sys.exit(0 if any(s.ok for s in states.values()) else 1)
