"""Throwaway probe #4: cache content for media urls; applet dirs; UTF-16 memory scan."""
import io
import os
import re
import sys
import collections

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

RADIUM = r"C:\Users\enoog\AppData\Roaming\Tencent\xwechat\radium"

print("=== A. grep cache files for media url candidates ===")
pat = re.compile(rb"https?://[^\x00-\x20\"'<>]{8,300}?\.(?:mp4|m3u8|flv|m4s|ts|mov)(?:\?[^\x00-\x20\"'<>]{0,300})?")
targets = []
for dp, dn, fn in os.walk(os.path.join(RADIUM, "web")):
    if "Cache_Data" in dp:
        for f in fn:
            fp = os.path.join(dp, f)
            try:
                if os.path.getsize(fp) > 64 * 1024:
                    targets.append(fp)
            except OSError:
                pass
print("cache files >64k:", len(targets))
found = collections.Counter()
samples = {}
for fp in sorted(targets, key=lambda p: -os.path.getsize(p))[:14]:
    try:
        with open(fp, "rb") as fh:
            data = fh.read(200 * 1024 * 1024)
    except OSError as e:
        print(" read fail", fp, e)
        continue
    hits = pat.findall(data)
    print(f"  {os.path.getsize(fp)/1048576:7.2f}MB {os.path.basename(fp):<24} media-url-hits={len(hits)}")
    for h in hits[:3]:
        u = h.decode("latin-1")
        found[u] += 1
        samples.setdefault(u, fp)
print("  distinct media urls in cache:", len(found))
for u in list(found)[:10]:
    print("   *", u[:180])

print("\n=== B. radium layout (2 levels) ===")
for dp, dn, fn in os.walk(RADIUM):
    depth = dp[len(RADIUM):].count(os.sep)
    if depth <= 2:
        print("  " * depth + os.path.basename(dp) + f"/  ({len(fn)} files, {len(dn)} dirs)")
    if depth >= 2:
        dn[:] = []

print("\n=== C. per-applet dirs (users/*/applet) ===")
udir = os.path.join(RADIUM, "users")
if os.path.isdir(udir):
    for u in os.listdir(udir):
        ap = os.path.join(udir, u, "applet")
        if os.path.isdir(ap):
            ents = os.listdir(ap)
            print(f"  user={u} applets={len(ents)} sample={ents[:8]}")
            for a in ents[:6]:
                apath = os.path.join(ap, a)
                n = sum(len(f) for _, _, f in os.walk(apath))
                print(f"     {a}: files={n}")
                for dp2, dn2, fn2 in os.walk(apath):
                    for f2 in fn2:
                        fp2 = os.path.join(dp2, f2)
                        try:
                            if os.path.getsize(fp2) > 300 * 1024:
                                print(f"        big: {os.path.getsize(fp2)/1048576:6.2f}MB {fp2[len(apath)+1:]}")
                        except OSError:
                            pass
