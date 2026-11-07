"""Throwaway probe #2: process tree, listening ports, child windows, UIA tree sample."""
import ctypes
import ctypes.wintypes as w
import io
import json
import subprocess
import sys

import psutil

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

user32 = ctypes.WinDLL("user32", use_last_error=True)
WNDENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

TARGET_HWND = 0x28D14E4


def get_text(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(hwnd, buf, n + 2)
    return buf.value


def get_class(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    user32.GetClassNameW(hwnd, buf, 512)
    return buf.value


print("=== 1. WeChat-family processes (name, pid, ppid, exe, cmdline tail) ===")
procs = []
for p in psutil.process_iter(["pid", "ppid", "name", "exe", "cmdline", "create_time"]):
    n = (p.info["name"] or "").lower()
    if "wechat" in n or "wmpf" in n:
        procs.append(p.info)
for i in procs:
    cmd = i["cmdline"] or []
    tail = " ".join(cmd[1:])[:150]
    print(f"pid={i['pid']:<7} ppid={i['ppid']:<7} {i['name']:<22} exe={i['exe']}")
    if tail:
        print(f"        cmd: {tail}")

print("\n=== 2. listening TCP ports owned by WeChat-family pids ===")
pids = {i["pid"] for i in procs}
try:
    for c in psutil.net_connections(kind="tcp"):
        if c.pid in pids and c.status == psutil.CONN_LISTEN:
            print(f"pid={c.pid} {c.laddr} status={c.status}")
except Exception as e:
    print("net_connections failed:", e)
print("(done)")

print("\n=== 3. window tree under mini-program HWND ===")


def walk(hwnd, depth=0, limit=[0]):
    if limit[0] > 60:
        return
    limit[0] += 1
    r = w.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    print("  " * depth + f"[{get_class(hwnd)}] {get_text(hwnd)!r} rect=({r.left},{r.top},{r.right},{r.bottom})")

    kids = []

    def kcb(h, _):
        kids.append(h)
        return True

    user32.EnumChildWindows(hwnd, WNDENUMPROC(kcb), 0)
    for k in kids:
        walk(k, depth + 1, limit)


walk(TARGET_HWND)

print("\n=== 4. UIA tree sample (uiautomation) ===")
try:
    import uiautomation as auto
    root = auto.ControlFromHandle(TARGET_HWND)
    print("root:", root.ControlTypeName, repr(root.Name))


    def dump(ctrl, depth=0, budget=[0]):
        if depth > 3 or budget[0] > 40:
            return
        budget[0] += 1
        try:
            print("  " * depth + f"{ctrl.ControlTypeName} name={ctrl.Name!r} class={ctrl.ClassName!r} rect={ctrl.BoundingRectangle}")
        except Exception as e:
            print("  " * depth + f"<err {e}>")
            return
        try:
            for c in ctrl.GetChildren():
                dump(c, depth + 1, budget)
        except Exception as e:
            print("  " * depth + f"<children err {e}>")

    dump(root)
except Exception as e:
    print("UIA failed:", type(e).__name__, e)

print("\n=== 5. port probe 9222/9223/9229/62000-62010 CDP endpoints ===")
import urllib.request
cands = [9222, 9223, 9229, 9333] + list(range(62000, 62011))
for port in cands:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.25) as r:
            print(f"port {port} OPEN -> {r.read()[:200]}")
    except Exception:
        pass
print("(probe done)")
