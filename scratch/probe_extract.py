"""Throwaway probe #3: memory scanning, cache dirs, ffmpeg devices, UIA workaround."""
import ctypes
import ctypes.wintypes as w
import io
import os
import re
import subprocess
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}


class MEMORY_BASIC_INFORMATION64(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong),
        ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", w.DWORD),
        ("__alignment1", w.DWORD),
        ("RegionSize", ctypes.c_ulonglong),
        ("State", w.DWORD),
        ("Protect", w.DWORD),
        ("Type", w.DWORD),
        ("__alignment2", w.DWORD),
    ]


k32.OpenProcess.restype = w.HANDLE
k32.VirtualQueryEx.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.POINTER(MEMORY_BASIC_INFORMATION64), ctypes.c_size_t]
k32.ReadProcessMemory.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]


def scan_pid(pid, needles, max_bytes=400 * 1024 * 1024):
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return None, ctypes.get_last_error()
    hits = []
    scanned = 0
    addr = 0
    mbi = MEMORY_BASIC_INFORMATION64()
    read = ctypes.c_size_t(0)
    try:
        while addr < 0x7FFFFFFFFFFF and scanned < max_bytes:
            if k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)) == 0:
                break
            size = mbi.RegionSize
            if (
                mbi.State == MEM_COMMIT
                and mbi.Protect in READABLE
                and not (mbi.Protect & PAGE_GUARD)
                and size > 0
                and size <= 512 * 1024 * 1024
            ):
                buf = ctypes.create_string_buffer(size)
                if k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(read)):
                    data = buf.raw[: read.value]
                    scanned += len(data)
                    for nd in needles:
                        for m in re.finditer(nd, data):
                            s = max(0, m.start() - 200)
                            e = min(len(data), m.end() + 200)
                            hits.append(data[s:e])
            addr += max(size, 4096)
    finally:
        k32.CloseHandle(h)
    return hits, scanned


print("=== A. memory scan of WeChatAppEx root pid 16872 (xwechat) ===")
needles = [rb"https?://[a-z0-9.\-]+\.(?:qpic\.cn|qq\.com)[^\x00-\x20\"'<>]{0,180}",
           rb"https?://[^\x00-\x20\"'<>]{6,200}\.(?:mp4|m3u8|flv|mov|m4s)[^\x00-\x20\"'<>]{0,120}"]
hits, scanned = scan_pid(16872, needles)
print("scanned MB:", round((scanned or 0) / 1048576, 1), "hits:", None if hits is None else len(hits))
if hits is None:
    print("  OpenProcess failed err=", scanned)
else:
    seen = {}
    for h_ in hits:
        txt = h_.decode("latin-1")
        for m2 in re.finditer(r"https?://[^\x00-\x20\"'<>]{6,240}", txt):
            u = m2.group(0)
            if u not in seen:
                seen[u] = 1
    print("  distinct urls:", len(seen))
    import collections
    hosts = collections.Counter(re.sub(r"^https?://([^/?#]+).*", r"\1", u) for u in seen)
    print("  top hosts:", hosts.most_common(12))
    med = [u for u in seen if re.search(r"\.(mp4|m3u8|flv|m4s|mov)(\?|$)", u) or "mpvideo" in u or "video" in u]
    print("  media-ish:", len(med))
    for u in med[:15]:
        print("   *", u[:200])

print("\n=== B. renderer pids scan (first 2 renderers, small budget) ===")
import psutil
for p in psutil.process_iter(["pid", "name", "cmdline", "ppid"]):
    if p.info["ppid"] == 16872 and "--type=renderer" in " ".join(p.info["cmdline"] or []):
        print(" renderer", p.info["pid"])
        h2, sc2 = scan_pid(p.info["pid"], [rb"https?://[^\x00-\x20\"'<>]{8,200}"], max_bytes=200 * 1024 * 1024)
        print("   scanned MB:", round((sc2 or 0) / 1048576, 1), "found:", None if h2 is None else len(h2))
        if h2:
            urls = set()
            for blob in h2:
                for m in re.finditer(rb"https?://[^\x00-\x20\"'<>]{8,200}", blob):
                    urls.add(m.group(0).decode("latin-1"))
            for u in list(urls)[:12]:
                print("     ", u[:160])
        break

print("\n=== C. WMPF cache / radium dirs ===")
for root in [
    r"C:\Users\enoog\AppData\Roaming\Tencent\xwechat\radium",
    r"C:\Users\enoog\AppData\Roaming\Tencent\xwechat\XPlugin\Plugins\RadiumWMPF",
]:
    if not os.path.isdir(root):
        print("missing:", root)
        continue
    total = 0
    media = []
    for dp, dn, fn in os.walk(root):
        for f in fn:
            fp = os.path.join(dp, f)
            try:
                total += 1
                if os.path.getsize(fp) > 1024 * 200:
                    media.append((os.path.getsize(fp), fp))
            except OSError:
                pass
        if total > 60000:
            break
    print(f"{root}: files={total}")
    for sz, fp in sorted(media, reverse=True)[:8]:
        print(f"   {sz/1048576:8.2f} MB  {fp}")

print("\n=== D. ffmpeg version ===")
ff = r"C:\Users\enoog\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"
print("  ffmpeg version:")
try:
    r = subprocess.run([ff, "-version"], capture_output=True, text=True, timeout=20, errors="replace")
    print("   ", r.stdout.splitlines()[0] if r.stdout else r.stderr[:200])
except Exception as e:
    print("   failed", e)

print("\n=== E. UIA with writable gen_dir workaround ===")
try:
    gen = os.path.join(tempfile.gettempdir(), "comtypes_gen_probe")
    os.makedirs(gen, exist_ok=True)
    import comtypes.client as cc
    cc.gen_dir = gen
    sys.path.insert(0, gen)
    import uiautomation as auto
    c = auto.ControlFromHandle(0x28D14E4)
    print("  OK root:", c.ControlTypeName, repr(c.Name))
    n = 0
    for ch in c.GetChildren():
        print("   child:", ch.ControlTypeName, repr(ch.Name)[:60])
        n += 1
        if n > 5:
            break
except Exception as e:
    print("  UIA failed:", type(e).__name__, str(e)[:300])
