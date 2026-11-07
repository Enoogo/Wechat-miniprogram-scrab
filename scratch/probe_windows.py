"""Throwaway probe: enumerate every top-level window + process, dump what we see."""
import ctypes
import ctypes.wintypes as w
import psutil

user32 = ctypes.WinDLL("user32", use_last_error=True)

WNDENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)


def get_text(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(hwnd, buf, n + 2)
    return buf.value


def get_class(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    user32.GetClassNameW(hwnd, buf, 512)
    return buf.value


def get_pid(hwnd):
    pid = w.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def get_rect(hwnd):
    r = w.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


rows = []


def cb(hwnd, _):
    if not user32.IsWindowVisible(hwnd):
        return True
    pid = get_pid(hwnd)
    try:
        pname = psutil.Process(pid).name()
    except Exception:
        pname = "?"
    rect = get_rect(hwnd)
    rows.append(
        {
            "hwnd": hwnd,
            "hwnd_hex": hex(hwnd),
            "pid": pid,
            "proc": pname,
            "class": get_class(hwnd),
            "title": get_text(hwnd),
            "rect": rect,
            "size": (rect[2] - rect[0], rect[3] - rect[1]),
        }
    )
    return True


user32.EnumWindows(WNDENUMPROC(cb), 0)

print(f"visible top-level windows: {len(rows)}\n")
interesting = [
    r for r in rows
    if "wechat" in r["proc"].lower() or "chrome_win" in r["class"].lower() or "wmpf" in r["class"].lower()
]
for r in interesting:
    print(
        f"{r['hwnd_hex']:>10} pid={r['pid']:<6} {r['proc']:<22} {r['class']:<28} "
        f"size={str(r['size']):<12} title={r['title']!r}"
    )

print("\n--- all classes histogram ---")
hist = {}
for r in rows:
    hist[r["class"]] = hist.get(r["class"], 0) + 1
for k, v in sorted(hist.items(), key=lambda kv: -kv[1])[:25]:
    print(f"{v:>4}  {k}")
