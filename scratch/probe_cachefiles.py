"""Throwaway probe #5: validate cached mp4s, applet.db schema for appid->name mapping."""
import glob
import io
import os
import sqlite3
import subprocess
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

RADIUM = r"C:\Users\enoog\AppData\Roaming\Tencent\xwechat\radium"
LOCAL = glob.glob(os.path.join(RADIUM, "users", "*", "applet", "local"))[0]
print("local root:", LOCAL)

mp4s = []
for dp, dn, fn in os.walk(LOCAL):
    for f in fn:
        if f.lower().endswith((".mp4", ".m3u8", ".ts", ".mov", ".flv")):
            fp = os.path.join(dp, f)
            mp4s.append((os.path.getsize(fp), fp))
mp4s.sort(reverse=True)
print(f"\n=== media files under applet/local: {len(mp4s)} ===")
for sz, fp in mp4s[:12]:
    print(f"  {sz/1048576:7.2f}MB {fp[len(LOCAL)+1:]}")

ffprobe = r"C:\Users\enoog\AppData\Local\Microsoft\WinGet\Links\ffprobe.exe"
print("\n=== ffprobe first 3 ===")
for sz, fp in mp4s[:3]:
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "format=duration,size,format_name:stream=codec_type,codec_name,width,height,avg_frame_rate",
             "-of", "default=noprint_wrappers=1", fp],
            capture_output=True, text=True, timeout=30, errors="replace")
        print(" ", os.path.basename(fp))
        for ln in (r.stdout or "").strip().splitlines():
            print("    ", ln)
    except Exception as e:
        print("  ffprobe failed", e)

print("\n=== applet.db schema ===")
db = os.path.join(RADIUM, "users", os.listdir(os.path.join(RADIUM, "users"))[0], "applet", "data", "8", "applet.db")
print("db:", db, os.path.exists(db))
if os.path.exists(db):
    tmp = os.path.join(os.environ.get("TEMP", "."), "applet_db_copy.db")
    try:
        import shutil
        shutil.copy2(db, tmp)
        con = sqlite3.connect(tmp)
        cur = con.cursor()
        tabs = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        print(" tables:", tabs)
        for t in tabs:
            try:
                cols = [c[1] for c in cur.execute(f"PRAGMA table_info({t})")]
                n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                print(f"  {t} ({n} rows) cols={cols[:14]}")
            except Exception as e:
                print("   err", t, e)
        for t in tabs:
            cols = [c[1] for c in cur.execute(f"PRAGMA table_info({t})")]
            if any("appid" in c.lower() for c in cols):
                try:
                    rows = cur.execute(f"SELECT * FROM {t} LIMIT 4").fetchall()
                    print(f"\n  sample {t}:")
                    for r in rows:
                        print("    ", [str(x)[:60] for x in r])
                except Exception as e:
                    print("   sample err", e)
        con.close()
    except Exception as e:
        print(" db read failed:", e)
