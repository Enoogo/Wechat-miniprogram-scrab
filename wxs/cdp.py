"""Chrome DevTools Protocol 后端（可选通道）。

什么时候能用
------------
微信小程序（WMPF）本质是 Chromium，只要它开了 DevTools 调试端口，就能用
WebSocket 直接读页面状态、抓网络请求，从而拿到最准确的视频地址。

现实情况（实测）
----------------
默认情况下 WeChatAppEx.exe **不监听任何端口**（本机 psutil 扫过所有 LISTEN，
微信相关 PID 一个都没有）。要打开调试端口需要额外手段：
  * WMPFDebugger / frida 注入：Hook 掉调试开关，再暴露 127.0.0.1:62000 左右端口；
  * 微信开发者工具：调试基础库，可控但不是用户实际运行环境。
因此本后端定位为“有则用，无则跳过”，绝不假装能用。

设计
----
* discover()：先探用户给的端口，再探常用端口，再看微信进程自己监听了什么端口。
* CDPSniffer：attach 到某个 page target 的 ws 地址，Enable Network，
  监听 requestWillBeSent / responseReceived，把 video/mp4/video-mp4 之类收下来；
  另外用 Runtime.evaluate 读 <video>.src 与 performance 资源列表（不用等播放）。
* 全程逐条写日志，连不上就说清楚“连不上 + 原因”。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("wxs.cdp")

DEFAULT_PORTS = (9222, 9223, 9229, 9333, 9444)
WMPF_DEBUG_PORT_RANGE = (62000, 62010)

MEDIA_MIME_PREFIX = ("video/", "audio/")
MEDIA_URL_HINTS = (".mp4", ".m3u8", ".m4s", ".flv", ".ts?", "mpvideo", "snsvideodownload", "wxsmw")

EVAL_JS = r"""
(() => {
  const out = new Set();
  try {
    document.querySelectorAll('video,audio,source').forEach(el => {
      if (el.src) out.add(el.src);
      if (el.currentSrc) out.add(el.currentSrc);
      const ss = el.querySelectorAll && el.querySelectorAll('source');
      if (ss) ss.forEach(s => { if (s.src) out.add(s.src); });
    });
  } catch (e) {}
  try {
    for (const e of performance.getEntriesByType('resource')) {
      const n = e.name || '';
      if (/\.(mp4|m3u8|m4s|flv|mov)(\?|$)/i.test(n) || /mpvideo|snsvideodownload|wxsmw/i.test(n)) out.add(n);
    }
  } catch (e) {}
  return JSON.stringify(Array.from(out));
})()
"""


@dataclass
class Endpoint:
    port: int
    version: dict = field(default_factory=dict)
    targets: list[dict] = field(default_factory=list)

    @property
    def browser_ws(self) -> str:
        return self.version.get("webSocketDebuggerUrl", "")

    def pages(self) -> list[dict]:
        return [t for t in self.targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]


def _http_json(port: int, path: str, timeout: float = 0.3) -> dict | list | None:
    """探测单个端口上的 DevTools HTTP 端点。

    超时给得很短：本机 localhost 上“端口没开”会立刻 RST（毫秒级）；
    但被防火墙 DROP 的端口会等满超时（实测 62000-62010 这段就是这样，
    16 个端口 × 0.8s = 约 12 秒，白白拖慢探测）。0.3s 足够本地端点应答。
    """
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read(4 * 1024 * 1024)
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        log.debug("CDP 探测 %s 失败: %s", url, exc)
        return None


def probe_port(port: int) -> Endpoint | None:
    ver = _http_json(port, "/json/version")
    if not isinstance(ver, dict):
        return None
    targets = _http_json(port, "/json/list")
    ep = Endpoint(port=port, version=ver, targets=targets if isinstance(targets, list) else [])
    log.info("发现 CDP 端点 port=%d browser=%s protocol=%s targets=%d",
             port, ver.get("Browser", "?"), ver.get("Protocol-Version", "?"), len(ep.targets))
    for t in ep.targets:
        log.info("   target: type=%s title=%r url=%s", t.get("type"), t.get("title"), (t.get("url") or "")[:120])
    return ep


def listening_ports_for_pids(pids: list[int]) -> list[int]:
    """看微信进程自己监听了哪些端口 —— 有 frida 注入的调试器时会命中。"""
    if not pids:
        return []
    try:
        import psutil

        ports = set()
        wanted = set(pids)
        for conn in psutil.net_connections(kind="tcp"):
            if conn.pid in wanted and conn.status == psutil.CONN_LISTEN and conn.laddr:
                ports.add(conn.laddr.port)
        if ports:
            log.info("微信进程监听的端口: %s", sorted(ports))
        return sorted(ports)
    except Exception as exc:  # noqa: BLE001
        log.debug("枚举监听端口失败: %s", exc)
        return []


def discover(settings=None, wechat_pids: list[int] | None = None) -> list[Endpoint]:
    """按优先级探测 CDP 端点。返回可用端点列表（可能为空）。

    端口探测并行做：串行探 16 个端口，遇到被防火墙 DROP 的端口每个要等满超时，
    实测串行约 5~12 秒，并行后不到 1 秒。
    """
    candidates: list[int] = []
    hint = ""
    if settings is not None:
        hint = str(getattr(settings, "cdp_port_hint", "") or "").strip()
    if hint:
        for part in hint.replace(";", ",").split(","):
            part = part.strip()
            if part.isdigit():
                candidates.append(int(part))
    if settings is None or getattr(settings, "probe_std_ports", True):
        candidates += list(DEFAULT_PORTS)
        candidates += list(range(WMPF_DEBUG_PORT_RANGE[0], WMPF_DEBUG_PORT_RANGE[1] + 1))
    candidates += listening_ports_for_pids(wechat_pids or [])

    ordered: list[int] = []
    seen: set[int] = set()
    for port in candidates:
        if port not in seen:
            seen.add(port)
            ordered.append(port)

    log.info("CDP 探测: 候选端口 %d 个 -> %s", len(ordered), ordered[:20])
    if not ordered:
        return []

    endpoints: list[Endpoint] = []
    from concurrent.futures import ThreadPoolExecutor

    workers = min(12, len(ordered))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cdp-probe") as pool:
        futures = {pool.submit(probe_port, port): port for port in ordered}
        results: dict[int, Endpoint] = {}
        for fut, port in futures.items():
            try:
                ep = fut.result(timeout=5)
            except Exception:  # noqa: BLE001
                ep = None
            if ep:
                results[port] = ep
    # 保持候选顺序
    for port in ordered:
        if port in results:
            endpoints.append(results[port])

    if not endpoints:
        log.warning(
            "没有发现可用的 CDP 调试端口。这属于正常情况：微信小程序默认不开放 DevTools。"
            "可改用「内存扫描」和「本地缓存」两条通道（不需要调试端口）。"
            "若确实需要 CDP，请用 WMPFDebugger / frida 打开调试端口后，把端口填到「CDP 端口」里重试。"
        )
    return endpoints


# ---------------------------------------------------------------- WebSocket 客户端

class CDPSession:
    """最小可用的 CDP 会话：同步 send + 后台线程收事件。"""

    def __init__(self, ws_url: str, timeout: float = 3.0) -> None:
        self.ws_url = ws_url
        self.timeout = timeout
        self._ws = None
        self._next_id = 1
        self._pending: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self.on_event: Callable[[str, dict], None] | None = None
        self.last_error = ""

    def connect(self) -> bool:
        try:
            import websocket  # websocket-client
        except ImportError:
            self.last_error = "缺少 websocket-client 库（pip install websocket-client）"
            log.error("CDP 不可用: %s", self.last_error)
            return False
        try:
            self._ws = websocket.create_connection(self.ws_url, timeout=self.timeout, suppress_origin=True)
            self._ws.settimeout(0.5)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.error("连接 CDP WebSocket 失败 %s: %s", self.ws_url, self.last_error)
            return False
        self._reader = threading.Thread(target=self._read_loop, name="cdp-reader", daemon=True)
        self._reader.start()
        log.info("CDP 已连接: %s", self.ws_url)
        return True

    def _read_loop(self) -> None:
        import websocket

        assert self._ws is not None
        while not self._stop.is_set():
            try:
                raw = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as exc:  # noqa: BLE001
                if not self._stop.is_set():
                    log.warning("CDP 接收中断: %s: %s", type(exc).__name__, exc)
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if "id" in msg:
                with self._lock:
                    slot = self._pending.pop(msg["id"], None)
                if slot is not None:
                    slot["result"] = msg
                    slot["event"].set()
            elif "method" in msg:
                if self.on_event:
                    try:
                        self.on_event(msg["method"], msg.get("params") or {})
                    except Exception:  # noqa: BLE001
                        pass

    def send(self, method: str, params: dict | None = None, timeout: float = 8.0) -> dict | None:
        if self._ws is None:
            return None
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            slot = {"event": threading.Event(), "result": None}
            self._pending[req_id] = slot
        payload = json.dumps({"id": req_id, "method": method, "params": params or {}})
        try:
            self._ws.send(payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("CDP 发送失败 %s: %s", method, exc)
            with self._lock:
                self._pending.pop(req_id, None)
            return None
        if not slot["event"].wait(timeout):
            log.warning("CDP 方法 %s 超时 %.1fs", method, timeout)
            with self._lock:
                self._pending.pop(req_id, None)
            return None
        result = slot["result"]
        if isinstance(result, dict) and "error" in result:
            log.warning("CDP %s 返回错误: %s", method, result["error"])
            return None
        return result.get("result") if isinstance(result, dict) else None

    def evaluate(self, expression: str) -> str:
        res = self.send("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        if not res:
            return ""
        return str((res.get("result") or {}).get("value") or "")

    def close(self) -> None:
        self._stop.set()
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:  # noqa: BLE001
            pass


@dataclass
class SniffResult:
    urls: set[str] = field(default_factory=set)
    requests_seen: int = 0
    responses_seen: int = 0
    eval_urls: int = 0
    target: str = ""
    error: str = ""


def sniff_target(ws_url: str, seconds: float = 6.0,
                 cancel: threading.Event | None = None,
                 on_url: Callable[[str, str], None] | None = None) -> SniffResult:
    """连到页面，抓 seconds 秒的网络请求 + 读页面里已有的媒体地址。"""
    result = SniffResult(target=ws_url)
    sess = CDPSession(ws_url)
    if not sess.connect():
        result.error = sess.last_error
        return result

    def on_event(method: str, params: dict) -> None:
        if method == "Network.requestWillBeSent":
            result.requests_seen += 1
            url = ((params.get("request") or {}).get("url") or "")
            if not url:
                return
            low = url.lower()
            if any(h in low for h in MEDIA_URL_HINTS):
                result.urls.add(url)
                log.info("  [CDP] 请求命中: %s", url[:200])
                if on_url:
                    on_url(url, "网络请求")
        elif method == "Network.responseReceived":
            result.responses_seen += 1
            resp = params.get("response") or {}
            mime = str(resp.get("mimeType") or "").lower()
            url = resp.get("url") or ""
            if mime.startswith(MEDIA_MIME_PREFIX) or any(h in url.lower() for h in MEDIA_URL_HINTS):
                if url:
                    result.urls.add(url)
                    log.info("  [CDP] 响应 %s [%s]: %s", mime, resp.get("status"), url[:200])
                    if on_url:
                        on_url(url, f"网络响应 {mime}")

    sess.on_event = on_event
    for method in ("Network.enable", "Runtime.enable", "Page.enable"):
        sess.send(method, {"maxTotalBufferSize": 50 * 1024 * 1024} if method == "Network.enable" else {})
    log.info("CDP 已启用 Network/Runtime，开始监听 %.1fs ...", seconds)

    # 先抓一次页面里已经存在的地址，不用等播放
    raw = sess.evaluate(EVAL_JS)
    if raw:
        try:
            found = json.loads(raw)
            if isinstance(found, list):
                result.eval_urls = len(found)
                for u in found:
                    if isinstance(u, str) and u.startswith("http"):
                        result.urls.add(u)
                        log.info("  [CDP] 页面元素/资源: %s", u[:200])
        except json.JSONDecodeError:
            log.debug("CDP eval 返回无法解析: %s", raw[:200])

    t0 = time.time()
    while time.time() - t0 < seconds:
        if cancel is not None and cancel.is_set():
            break
        time.sleep(0.2)
    sess.close()
    log.info("CDP 抓取结束: 请求 %d 响应 %d 页面内 %d -> URL %d 条",
             result.requests_seen, result.responses_seen, result.eval_urls, len(result.urls))
    return result


def sniff_all(endpoints: list[Endpoint], seconds: float = 6.0,
              cancel: threading.Event | None = None,
              on_url: Callable[[str, str], None] | None = None) -> set[str]:
    """对所有端点的所有 page target 抓一轮。"""
    urls: set[str] = set()
    for ep in endpoints:
        for target in ep.pages():
            ws = target.get("webSocketDebuggerUrl")
            if not ws:
                continue
            log.info("CDP 目标: %r (%s)", target.get("title"), ws)
            res = sniff_target(ws, seconds=seconds, cancel=cancel, on_url=on_url)
            urls |= res.urls
    return urls


__all__ = [
    "Endpoint", "SniffResult", "discover", "probe_port", "listening_ports_for_pids",
    "CDPSession", "sniff_target", "sniff_all", "DEFAULT_PORTS",
]
