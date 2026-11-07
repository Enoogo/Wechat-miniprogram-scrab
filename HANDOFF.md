# 交接文档（给下一个 AI / 开发者）

> 目的：让你在 20 分钟内接手，不用重复踩我已经踩过的坑。
> 结论先行：**本工具已在真机上跑通**（微信 4.x + 小程序「鹅打卡」）。
> `python run.py --selftest` 现在 **46 项、45 通过、1 跳过、0 失败**；
> `python scratch/gui_smoke.py` 27 项全过（含排版断言）；
> `python scratch/hls_e2e.py` 四种 HLS 组合全过。
> 仍然没有在**真实带视频的小程序**里端到端下到一条完整视频（见第 8 节）。

---

## 0. ⚠️ 给下一个 AI 的三条硬规矩

1. **测试完记得删掉没用的截图/媒体产物。**
   `scratch/` 里只留**可复用的脚本**，不要留 `*.png / *.mp4 / shots/` 这类一次性产物。
   `scratch/gui_smoke.py` 已经默认在结束时删掉自己的截图（`--keep-shots` 才保留），
   你自己临时生成的截图也照这个规矩办。历史上 `scratch/` 曾堆了 11 个截图/录屏文件，
   全是没用的，已经删过一轮。
2. **改中文源码只能用编辑工具（edit/write），绝对不要用 PowerShell 的
   `Get-Content | Set-Content` 原地改** —— 会把 UTF-8 中文写成半截 GBK，文件直接坏掉。
3. **改完必须跑**：`python run.py --selftest` + `python scratch/gui_smoke.py`。
   两者都绿了再交付，并把结果写回本文档第 8 节。

---

## 1. 环境事实（本机实测）

| 项目 | 位置 / 版本 | 备注 |
|---|---|---|
| Python | `D:\MiniPrograms\Python3117\python.exe` | 3.11.7，**64 位**；不在 PATH 里时请用绝对路径 |
| ffmpeg | `C:\Users\enoog\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe` | gyan.dev 9.0.2-full_build |
| ffprobe | 同上目录 `ffprobe.exe` | |
| 微信主程序（4.x） | `C:\Program Files\Tencent\Weixin\Weixin.exe` | **注意不是 WeChat.exe** |
| 微信小程序运行时 | `%APPDATA%\Tencent\xwechat\XPlugin\Plugins\RadiumWMPF\WeChatAppEx.exe` | 插件版本目录 `26506`，会随升级变化 |
| 微信小程序数据根 | `%APPDATA%\Tencent\xwechat\radium` | 目录名 **xwechat** |
| 小程序缓存视频实际位置 | `...\radium\users\<userhash>\applet\local\<appid>\temp\*.mp4` | **最有价值的一条** |
| Chromium 缓存 | `...\radium\web\profiles\<profile>\Cache\Cache_Data\{data_N,f_*}` | `data_N` 常被独占 |
| QQ NT 内置 WMPF（要排除） | `...\QQNT\versions\*\resources\app\wmpfsdk\runtime\WeChatAppEx.exe` | 同名进程，靠**路径**区分 |
| 系统 | Windows 10.0.26200（Win11 内核），屏幕 3120x2080，DPI 144（150%） | 高 DPI 对 GUI 影响很大 |
| 权限 | 普通用户，**不需要管理员** | 同用户进程 `PROCESS_VM_READ` 可直接读 |

相关包：`psutil 7.2.2`、`requests 2.34.2`、`websocket-client 1.9.0`、`websockets 15.0.1`、
`pillow 12.3.0`、`numpy 2.4.6`、`pywin32 311`、`pyinstaller 6.19.0`、`opencv-python`、`certifi`。

---

## 2. 架构与数据流（v1.1）

```
                     ┌──────────────── 探测（probe.py）──────────────────┐
 detect() 窗口候选 ──▶│ 1 memory_scan  → URL 集合                         │
 windows.py           │ 2 cache_scan   → 本地 mp4/jpg/png 文件（最可靠）   │──▶ urls.analyze()
 winapi.py            │ 3 cache_scan   → Chromium 缓存里的 URL             │   分类/去重/打分
                      │ 4 cdp          → 页面与网络请求（通常不可用）       │   视频 + 图片分开
                      └───────────────────────────────────────────────────┘
                                        │
                        list[MediaItem]（models.py，media_type=video|image）
                                        │
                             hls.expand_hls_items()  ← m3u8 主清单展开成各清晰度
                                        │
                        ┌───────────────┴────────────────┐
                        ▼                                ▼
             downloader.Downloader                 gui.py / run.py CLI
        （直链断点续传 / HLS 分片 / 本地复制 /
          并发队列 / 下载后完整性体检）
                        │
                        └─▶ hls.download_media_playlist()（逐片下载+校验+重试）
                            或 ffmpeg 直连（兜底）
```

配置来源：**`config.json` → `Settings`（`config.load_settings()`）**。
所有入口（GUI / `run.py --probe` / selftest）都必须用 `load_settings()`，
不要再直接 `config.Settings()`，否则用户改的配置不生效。

**录制功能已删除**（v1.1）：`capture.py` 只剩 `grab_window_png()`（窗口截图预览），
`sysinfo` 里的 dshow 音频设备枚举也删了。想录屏请用系统录屏软件。

关键设计原则（改代码时请保持）：

1. **每个通道独立失败不影响其它通道**，异常都要 `try/except` + 写日志 + 变成 `notes`。
2. **GUI 线程模型**：Tk 只在主线程；后台线程只往 `ui_queue` 里放事件；
   日志走 `logging → GUI_BRIDGE → _drain_logs`；主线程 100ms 轮询。绝不在工作线程里碰控件。
3. **日志优先**：任何“为什么没结果”都要能从 `logs/*.log` 里读出来。

---

## 3. 这一轮改了什么（对照用户需求，别改回去）

| # | 用户要求 | 实现 |
|---|---|---|
| 1 | 内存预算/下载并发/CDP端口 三个输入框挪到复选框右侧，给「详情」腾纵向高度 | `gui._build_middle_panel()` 改成统一 grid：第 0 列复选框、第 1/2 列标签+输入框，**同一行并排**；少 2 行 |
| 2 | 「开始探测 / 取消当前操作」合并成一个按钮，点一下切换，按钮只够 4 个字，进度条放右边 | `self.btn_action` + `_set_action_mode()`；按钮放在 `holder`（`grid_propagate(False)` 固定宽度 = `text_width("开始探测")+px(30)`）；`self.progress` 在 `act` 第 1 列 |
| 3 | 自动获取所有 m3u8 视频 | ① `urls.classify_url` 认 `.m3u8`（含无扩展名/签名在 query 的写法）；② `probe` 探测结束 `hls.expand_hls_items()` 把**主清单展开成每个清晰度一条**；③ GUI「全部m3u8」按钮 = `start_download_hls()`；④ CLI `--download m3u8` |
| 4 | 默认关闭内存扫描、本地缓存文件；加 config.json 配置所有开关/数值默认值 | `config.json` + `config.DEFAULT_CONFIG` / `load_config()` / `apply_config()` / `load_settings()`；`Settings.use_memory/use_cache_files=False`，`use_cache_urls=True` |
| 5 | 也默认关闭 CDP | `channels.cdp=false`（`Settings.use_cdp=False`） |
| 6 | 「自动滚动」最后一个字显示不全 | 日志工具条改 **grid**，并把那一列设 `columnconfigure(2, minsize=text_width("自动滚动")+px(30))`；`text_width()` 用 `tkfont.nametofont("TkDefaultFont").measure()`。**不要换回 pack**，pack 在窗口变窄时会压掉最后一个字 |
| 7 | 对勾换成 ✓ | `models.MARK_ON="✓"`、`ITEM_COLUMNS` 表头 `("sel","✓",…)`；**不要再出现 √** |
| 8 | 全选/全不选 合成一个勾选框 | `self.chk_select_all` + `var_select_all` + `_on_select_all()`；`_update_item_count()` 里用 `_syncing_select_all` 防重入地同步状态 |
| 9 | 删掉 scratch 里没用的截图；handoff 里交代以后测试完删截图 | 已删 11 个 png/mp4 + `test_capture_methods.py`；规矩写在第 0 节 |
| 10 | 删掉右上角「打开日志目录」按钮 | header 只剩「环境自检 / 打开下载目录」；④ 的「打开日志文件」改为 explorer 定位当前日志 |
| 11 | 下载不完整（时长对，但前半段没画面没声音、播放器自动跳到后半段） | 见第 5.4 节：内置 HLS 分片下载器 + 等文件写完 + 零块检测 + 时长核对 + 解码体检 + 状态「已完成⚠」 |
| 12 | 删掉录制功能 | `capture.record/build_*_command/CaptureResult` 全删；`--record`/`--seconds` 删除；selftest 的录制检查换成 PrintStream 截图 + HLS 检查 |
| 13 | 增加获取所有图片（jpg/png） | `urls.PHOTO_EXTS` + `UrlInfo.kind="image"`；`UrlReport.images`；`analyze(want_images=True)`；`cache_scan` 支持 local 图片与缓存图片 URL；`MediaItem.media_type="image"`；下载器图片头校验 + 小于 `min_image_kb` 自动跳过；GUI 有「图片（JPG/PNG）」复选框 + 「视频/图片」筛选。**默认关**（实测一次探测 896 张图，会把列表淹掉） |

其它顺手修的：
* `_on_download_state` / `download_states` 用 `TaskState.ok`，状态新增「已完成⚠」（`DONE_WARN`），
  所以**任何“是否成功”的判断都要用 `status.startswith("已完成")` 或 `.ok`**。
* 403 时按微信文档的 `referrer-policy` 换 `https://servicewechat.com/{appid}/0/page-frame.html` 重试。
* `--probe` 新增 `--concurrency`、`--channels` 缺省取 config、`--download m3u8`。
* 菜单加了「文件 → 打开配置文件 config.json」「工具 → 重新加载 config.json」。

---

## 4. 视频/图片从哪来（各通道实测细节）

1. **本地缓存文件（最实用）**
   实测路径 `...\radium\users\<hash>\applet\local\wxfc4ef6d539d03373\temp\19df647bf96_ca6.mp4`。
   ffprobe 验证：`h264 + aac, 1280x720, 30fps, 30~58s`，是**完整可播放的原始视频**。
   注意 `temp` 目录会随小程序清理而消失，越早复制越好。
   **坑：这里也会混入广告**（鹅打卡实测 10 个 mp4 全是广告），所以默认关。
2. **内存扫描**
   打开 `WeChatAppEx.exe` 根进程 + 全部子进程（renderer/network service/gpu），
   `VirtualQueryEx` 遍历 + `ReadProcessMemory` + 正则。实测 900MB 预算下能捞到 2400+ 条 URL。
   **必须同时扫子进程** —— 页面/媒体元素主要在 renderer 里。
3. **Chromium 缓存**
   `Cache_Data\f_xxxxxx` 是 Simple-Cache 的 entry 文件，**URL 以明文嵌在里面**，可直接捞。
   `data_N` 常被微信**独占打开**，必须 `CreateFileW(..., FILE_SHARE_*..., ...)`（`cache_scan.read_file_shared()`）。
   实测：6 个 Cache_Data、1340 条 URL → 65 条媒体（8 条可下载、57 条过期）+ 896 张图片，约 2.5 秒。
4. **CDP**
   微信**默认不监听任何端口**（扫过所有 LISTEN），这条通道“存在但通常不可用”。
   要真用需要 WMPFDebugger/Frida 注入打开端口，再填进「CDP 端口」。
   性能坑：端口探测**必须并行 + 短超时**（0.3s + 线程池），否则被 DROP 的端口会串行卡十几秒。
5. **图片**
   不是独立来源：`want_images=True` 时 `analyze()` 会把 jpg/jpeg/png 单独收进 `report.images`
   （含 `?wx_fmt=jpeg`、`path` 里带 `.jpg` 片段这种“路径没扩展名”的写法）。
   同时 `cache_scan.scan_applet_local(include_images=True)` 会收 local 目录里的 jpg/png，
   `min_image_kb`（默认 24KB）过滤图标；下载后再小就删掉。

**“不播放能不能拿到缓存 URL？”（用户问过，结论如下）**
- **历史 URL 能**：Chromium 缓存里存着上次访问的地址，「缓存 URL」通道不播放也能回收，但可能已过期。
- **当前会话的新地址基本要靠发请求**：视频组件 `autoplay` 默认 `false`（微信官方文档 video 组件属性表），
  播放器只有在真正请求媒体时才把 m3u8/分片地址写进内存与网络栈。
  不过不少小程序会**预加载**首段（`wx.preloadVideo` / `initial-time`），
  所以“打开过页面但没点播放”也可能扫到一部分。
- 想把这条做到最好：勾「内存扫描」+「缓存 URL」，先把目标页面打开/划过触发预加载；
  有 DevTools 端口时用 CDP 最干净。

---

## 5. 核心事实与「踩过的坑」（按血泪排序）

### 5.1 窗口识别

- 小程序窗口：进程 `WeChatAppEx.exe`，窗口类 **`Chrome_WidgetWin_0`**，
  子窗口链 `ContainerWindow` → `Chrome_RenderWidgetHostHWND` / `Intermediate D3D Window`。
- 微信主窗口是 `Chrome_WidgetWin_1`，进程 **`Weixin.exe`**（4.x）/ `WeChat.exe`（3.x）；
  浏览器也是 `Chrome_WidgetWin_1`，必须按进程过滤。
- **坑：QQ NT 也打包了同名的 `WeChatAppEx.exe`**（路径含 `QQNT\...\wmpfsdk`）。
  只看进程名会把 QQ 的小程序窗口当微信的 —— 必须用 `QueryFullProcessImageNameW` 拿路径判断。
  `windows.classify_process()`。
- 隐藏窗口会污染枚举（本机顶层窗口 700+ 个），已过滤：只保留可见窗口 + 有标题的 Chromium 窗口。

### 5.2 内存扫描的坑

- **不要按地址顺序扫**：预算会被前几个几十 MB 的大区块吃掉（读 120MB → 0 条）。
  现在先枚举区块元数据，**优先扫「≤16MB 的 Private/Mapped 区块」**，再扫剩下的。
- 一律跳过 `PAGE_GUARD` / `PAGE_NOACCESS`；区块边界留 8KB overlap。
- 内存里的字符串有 **UTF-16LE** 编码的（`h\x00t\x00t\x00p\x00`），只扫 ASCII 会漏。
- 内存里的 URL 会**粘连**（两个 URL 连一起），在第二个 `http` 处切；还有 JSON 转义（`\/`、`\u0026`）。
- 微信 CDN 链接带 `dis_t`（Unix 秒）签名时间戳，**会过期**；过期条目被判为不可下载、不进入列表。
- 预算要可中断（GUI 的「取消」），`threading.Event` 在区块间检查。

### 5.3 下载器的坑

- `requests` 断点续传：`.part` + `Range: bytes=N-`；服务器返回 **416** 时要删 `.part` 重来。
- 服务器返回 `text/html` 或 `application/json` 要当错误处理（否则会存下一个错误页）。
- 下载完必须校验文件头（mp4 的 `ftyp` 在偏移 4；图片看 JPEG/PNG 魔数），并对比 Content-Length。
- **HLS 交给 ffmpeg 的老坑（依然有效）**：
  1. `-headers "A: b\r\nC: d"` 在 Windows 上会因为命令行里塞 CRLF 而报
     `No trailing CRLF found in HTTP header`。**改用 `-user_agent` / `-referer`**。
  2. Windows 版 ffmpeg（openssl）**默认没有 CA bundle**，访问 https 的 m3u8 会报
     `Peer certificate failed verification`（requests 却正常）。**解决：`-ca_file <certifi 的 cacert.pem>`**。
  3. **新坑（v1.1 实测）**：`-ca_file` 是 tls 协议选项，**只对 https 输入有效**；
     拿它去开 `http://` 的 m3u8，ffmpeg 会报 `Error opening input files: Option not found`
     （没被消费的输入选项被当成错误）。现在只在 URL 以 `https` 开头时才加。
  4. **新坑**：m3u8 可能引用 `.key` / `.m4s` 这类 ffmpeg 认为“不像媒体”的扩展名，
     不加 `-allowed_extensions ALL` 会直接失败
     （`Filename extension ... blocked for security reasons`）。
  5. `-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -rw_timeout 30000000`
     放在 `-i` 前可以让直连更抗抖动（http 协议选项）。
- 微信 CDN 直链大多需要 `Referer`，默认带 `https://servicewechat.com/` + 微信 UA；
  403 时按 appid 换 `page-frame.html` 形式再试。

### 5.4 “下载不完整”专题（用户实测症状 + 这一版的应对）

**症状**：播放器里时长是对的（等于原片），但**前半段只有画面没有声音、画面也不动**，
播放器自动从后半段开始放；手动拖到前半段，播一会儿又跳回后面。

**判断**：这是**数据缺失**（不是索引坏）：moov 完整所以时长对，但 mdat 前段没有真实数据。
常见成因三种：
1. 直接把 m3u8 丢给 ffmpeg —— 某个分片请求失败时 ffmpeg 可能**跳过该分片继续写**；
2. 复制**还在写入**的小程序缓存文件，拿到半成品快照；
3. 断点续传时 `Content-Range` 起点对不上，文件中间出现空洞。

**这一版的 6 道防线（在 `hls.py` / `downloader.py`）**：

| 防线 | 位置 | 说明 |
|---|---|---|
| 逐片下载 + 每片校验 + 重试 | `hls._download_one()` / `download_media_playlist()` | 每片比对 `Content-Length`；失败按 `http_retries` 重试；**任何一片失败就整体报错**，绝不写出“缺一段”的文件 |
| 本地清单拼接 | `hls._write_local_playlist()` + `run_ffmpeg_remux()` | 所有 URI 都改成本地文件后再让 ffmpeg 转封装，网络因素彻底排除。**重写时必须保留 `EXT-X-MEDIA-SEQUENCE`**（AES-128 默认 IV 由序列号推导）和 KEY/MAP 的“持续生效”语义 |
| 等本地文件写完 | `downloader._wait_file_stable()` | 复制前等大小稳定（`local_wait_seconds`，默认 8s），复制到 `.part` 再改名，复制后再对比源文件是否又变大 |
| 零块检测 | `downloader._zero_ratio()` | 前 8MB 全零块占比 >15% 直接告警（这条最直接命中用户的症状） |
| 时长核对 | `hls.download_media_playlist()` + `_check_integrity()` | 成品时长 vs 清单声明时长，差 >3%（或 2s）告警 |
| 解码体检 | `sysinfo.probe_decode_errors()` | 用 ffmpeg 解码开头 15 秒统计报错行数；有错就告警 |

体检结论会写进日志和条目的「详情」面板，状态显示为 **「已完成⚠」**。
如果仍然不完整，基本就是数据源本身残（缓存没下完 / CDN 给了残缺流）：
**等视频完整播放一遍再重新探测**，或改用直链而不是本地缓存快照。

**已验证**：`scratch/hls_e2e.py` 用 ffmpeg 现造 6 秒 HLS（明文 / AES-128 × 分片下载器 / ffmpeg 兜底
四种组合）全部下载成功，产物 138113B、时长 6.037s、无告警。
**未验证**：真实小程序里那条“前半段没声音”的视频（手上没有可复现的素材，见第 8 节）。

### 5.5 HLS / m3u8 的其它坑

- `EXT-X-KEY`、`EXT-X-MAP` 是**持续生效**的（到下一个同名标签为止），
  第一版把“只在紧邻分片上生效”写错了，会导致加密流只有第一片能解、后面全黑。
- 主清单（`#EXT-X-STREAM-INF`）里每个变体就是一路视频，**“所有 m3u8 视频”= 展开所有变体**。
- **自动展开的耗时坑（务必保持并行+单次尝试）**：探测结束时会对列表里的 m3u8 逐个问一次
  “你是不是主清单”。第一版用默认的 `http_retries=3` + 2/4 秒退避串行探测，
  实测 7 个 DRM 链接（`encrypt-k-vod.xet.tech/.../drm/v.f100230.m3u8` 返回 403）
  把一次探测从 6 秒拖到 **33 秒**。现在 `expand_hls_items()` 是
  **线程池 4 并发 + `retries=1` + 6 秒超时**，探测回到 **11.1 秒**。
- `EXT-X-BYTERANGE` 的分片要带 Range 下载，但写本地清单时**必须去掉 BYTERANGE**
  （因为已经落地成独立完整文件）。
- fMP4 的 `EXT-X-MAP` init 段必须一起下并在本地清单里保留，否则 ffmpeg 报 “could not find codec parameters”。
- `-bsf:a aac_adtstoasc` 只对 **TS** 输入有用；fMP4 输入加了反而报错，
  所以 `run_ffmpeg_remux()` 按 `is_fmp4` 决定加不加。
- ffmpeg 的 `-hls_key_info_file` 第一行才是**写进 m3u8 的 URI**；
  写绝对 Windows 路径会被当转义序列吃掉（`C:\Users` 变成 `C:Users`）。生成测试素材时踩过。
- 有些播放器/调试环境会把 AES 密钥写成 `file:///...` 或本地绝对路径，
  这种 URI 交给 requests 会报 `InvalidSchema`；`hls._download_one()` 现在对
  `data:` / `file:` / 本地路径都会自己读。

### 5.6 GUI / tkinter 的坑

- **高 DPI**：本机 3120x2080、DPI=144。Tk 的几何单位是**物理像素**，但字号按 point 自动放大 1.5 倍。
  所有固定像素都要过 `App.px()`，并用 `tk scaling = dpi/72`。
- **`pack` 顺序**：状态栏必须**先** pack（`side="bottom"`），再放可伸缩的 PanedWindow。
- **文字被截断（这次修的）**：「自动滚动」用 pack 时窗口一窄，最后那个字会被压掉。
  换成 grid + `columnconfigure(col, minsize=text_width(text)+px(30))` 后稳定；
  同类问题以后都用 `App.text_width()` 量，不要拍脑袋写 `width=`。
- **最小窗口宽度是算出来的，别乱改小**：③ 的工具条需要 978 物理像素（1.5 倍 DPI），
  加上右栏 486 逻辑像素与面板内边距，窗口不能窄于 **1260 逻辑像素**，否则「移除/粘贴/筛选」
  会被裁掉。`gui_smoke.py` 有一条断言专门守它（`vbar.winfo_width() >= vbar.winfo_reqwidth()`）。
  为了腾空间，工具条上用的是「移除」「粘贴」这种两字按钮和 `共N · 选N` 这种极短计数，
  视频/图片的拆分看「筛选」下拉或状态栏 —— **别再把这个标签写长**。
- **合并按钮**：`ttk.Button` 的宽度是“字符数”，中文字符不等宽，所以按钮宽度用
  `holder = ttk.Frame(width=..., height=...); holder.grid_propagate(False)` 来锁死，
  再让按钮 `pack(fill="both")`。切换身份只改 `text` + `command`。
- **筛选/全选**：Treeview 重建（`_render_items()`）比逐行删改更省事，但要注意
  `self.items` 才是唯一数据源，`iid` 用 `item.uid`。
- UIA（`uiautomation`）在本机报权限错误：`comtypes.client.gen_dir = <可写目录>` 后再 import。
  当前没用到 UIA。
- GUI 无头验证：`scratch/gui_smoke.py` 会真建窗口、跑探测、下载一条、截图，
  并且**断言排版**（三个输入框在复选框右侧、进度条在按钮右边、自动滚动宽度 ≥ 需要宽度、
  右上角没有「打开日志目录」、表头是 ✓、全选是勾选框、按钮能切换「开始探测/取消」）。
  截图用 `capture.grab_window_png()`（PrintWindow），**别用 ImageGrab 抓屏幕**。

### 5.7 工具链的坑

- **不要用 PowerShell 的 `Get-Content | Set-Content` 原地改中文源码**（见第 0 节）。
- `subprocess.run(..., errors="replace")` 不传 `text=True` 也会**强制进入文本模式**，
  返回 `str` 而不是 `bytes`；统一按 bytes 抓再分别按 utf-8/gbk 解码。
- 控制台编码：跑 CLI 时用 `$env:PYTHONIOENCODING="utf-8"`。
- 本机 shell 是 PowerShell 7；`Select-String`/`Get-Process` 用来快速核对。
  **PowerShell 不支持 heredoc**，要喂多行 Python 请先写临时 .py 文件。
- 自检里下载测试的载荷是**用 ffmpeg 现造的真 mp4**（`selftest._make_sample_mp4()`）。
  别改回“ftyp + 随机字节”，否则下载后体检会把每个用例都判成「已完成⚠」。

---

## 6. 关键实现细节（改代码时别破坏）

- `media_items` 的 `uid` 是自增编号，GUI 用 Treeview 的 iid 直接对应，不要复用。
- `urls.analyze()` 的去重键是 `host+path+关键参数(sha256/dis_k/filekey/vid/m)`；
  **视频和图片分两个 dict**（`seen_identity` / `seen_images`），别混。
- 打分阈值：`probe` 只把 `info.is_downloadable`（`kind ∈ direct/hls/dash/audio/image` 且未过期）
  变成条目；`< 0` 视为噪音。图片的 host 负分会被忽略（否则微信图片 CDN 全被扣成负数）。
- 内存扫描预算：`Settings.scan_budget_mb`（默认 900），**按进程均摊**（`max(40MB, 总预算/进程数)`）。
- Chromium 缓存扫描：`cache_url_budget_mb=512`、`cache_url_file_mb=64`；
  性能关键：先 `data.find(b"http")` 定位（C 速度），再对 800 字节窗口跑锚定正则。
- `MediaItem.media_type`（"video"/"image"）决定列颜色、筛选、图片尺寸过滤；
  `is_hls` 属性是「全部m3u8」和 HLS 分派的依据。
- `TaskState.ok / status / warn`：成功判定统一用 `.ok`（`已完成` 或 `已完成⚠` 或 `已存在`）。
- `config.DEFAULT_CONFIG` 是“出厂默认值”，`config.json` 是用户可改的覆盖层；
  selftest 断言的是**出厂默认**（否则用户一改配置自检就红）。
- `logs/` 与 `downloads/` 自动创建；`logs` 轮转 8MB×6。
- 所有子进程（ffmpeg）都要有超时与收尾逻辑。

---

## 7. 如何验证（每次改动后跑一遍）

```powershell
$env:PYTHONIOENCODING="utf-8"
D:\MiniPrograms\Python3117\python.exe run.py --selftest                  # 46 项，应 0 失败
D:\MiniPrograms\Python3117\python.exe run.py --list-windows --show-all
D:\MiniPrograms\Python3117\python.exe run.py --probe --channels cacheurl --json scratch\p.json
D:\MiniPrograms\Python3117\python.exe run.py --probe --channels cache --download 1
D:\MiniPrograms\Python3117\python.exe scratch\gui_smoke.py               # 27 项 + 排版断言
D:\MiniPrograms\Python3117\python.exe scratch\hls_e2e.py                 # 明文 HLS 端到端
D:\MiniPrograms\Python3117\python.exe scratch\hls_e2e.py --encrypt --fallback
```

- `--selftest` 自带**假 HTTP 服务**（Range/图片/m3u8/分片）和**假 CDP WebSocket 服务**，
  所以不需要微信在播视频也能验证下载器、HLS、CDP 客户端代码。
- `scratch/hls_e2e.py` 是唯一能验证“真 HLS 分片 → ffmpeg 封装 → 产出可播放 mp4”的脚本
  （自检里的分片是假字节，没法真的转封装）。四种组合都要过。
- **测试产物：跑完请检查 `scratch/` 有没有多出 png/mp4/shots，有就删掉。**
  `gui_smoke.py` 默认自动删截图和 `scratch/dl_test`；`--keep-shots` 保留时需要你手动删。
- 全通道探测（`--channels mem,cache,cacheurl,cdp,images`）本机约 **11 秒**（内存预算 300MB）。
  如果突然变成 30 秒以上，先看日志里 m3u8 展开是不是又退回串行重试了（见 5.5）。

---

## 8. 验证状态（诚实记录，2026-11-02 v1.1）

**已在本机真机验证**

- 窗口检测：识别 `鹅打卡`（hwnd=0x2ab1796, pid=16872, score=80~95），正确排除 QQ/浏览器窗口。
- 本地缓存：扫到 10 个 mp4（63.89MB），ffprobe 确认可播放（30.3s h264+aac 1280x720）。
- Chromium 缓存：6 个 Cache_Data、1340 条 URL → 65 条媒体 + 896 张图片，约 2.5~3.9 秒。
- 内存扫描：120MB 预算（自检）、10 个进程；原始 URL 26~27 条，当时无有效媒体（鹅打卡在播广告）。
  300MB 预算全通道探测约 1.5 秒读完内存。
- 全通道探测（mem+cache+cacheurl+cdp+images、预算 300MB）：**11.1 秒**，
  结果 38 个本地文件 + 904 个远端链接（18 视频，其中 7 个 m3u8[DRM] / 924 图片）。
- 下载器：真 mp4 下载/断点续传/本地复制/404 优雅失败/图片下载/空壳文件识别，全部通过。
- HLS：解析（主清单/媒体清单/加密/序列号）、分片下载、本地清单生成、主清单展开、四种端到端组合全过。
- CDP：假服务验证 `/json/version`、`/json/list`、Network 事件、Runtime.evaluate 全通；
  真机仍**无调试端口**（微信默认不开，扫到 9222 有别的服务返回 404，62000-62010 全部超时）。
- GUI：27 项冒烟 + 排版断言全过（含“自动滚动完整显示”“按钮切换”“✓ 表头”“无打开日志目录”
  “下载一个 12MB 缓存 mp4 且体检无告警”）。
- 自检：46 项、45 通过、1 跳过（“内存扫描→媒体”，因为当时内存里没有可下的媒体）、0 失败。

**未验证 / 已知缺口**

1. **没有真正从小程序里下到一条“完整视频”并复验“前半段无声音”那个现象** ——
   手上没有可复现的素材。防线已就位（第 5.4 节），请在下个真的带视频的小程序里复验：
   `--probe --channels mem,cacheurl --download m3u8` 然后看日志里的“体检结论”。
2. **CDP 真实端点从未连上**（没有 Frida/WMPFDebugger）。
3. **图片通道在真实小程序上的观感**只测过鹅打卡（896 张，多为图标/头像/省份图标），
   所以默认关；如果用户主用图片，建议他改 `channels.images=true` 并调 `min_image_kb`。
4. `appid → 小程序名称` 没做映射（`applet/data/8/applet.db` 被加密，不是明文 SQLite）。
5. 微信 3.x 的目录结构只做了兼容性猜测，未实测。
6. 没做打包（`pyinstaller -F -w run.py`，ffmpeg 需另带）。

---

## 9. 下一步建议（按性价比排序）

1. **在真实带视频的小程序里跑一遍完整链路**，把日志里的“体检结论”贴进本文档 ——
   这是唯一没闭环的主路径（尤其是“下载不完整”的复验）。
2. **HLS 直播/`SAMPLE-AES` 支持**：目前 SAMPLE-AES 只能下到密文，会明确失败；
   要做得靠 ffmpeg + 密钥服务，复杂度高。
3. **appid → 名称映射**：读 `Local Storage\leveldb` 或 `mmkv\xweb_global_storage`。
4. **CDP + Frida 集成**：端口打通后能用 `cdp.sniff_target()` 精确抓 URL，比内存扫描干净。
5. **下载队列持久化**（`queue.json`），重启后继续。
6. **打包发布**，把 `run.py --selftest` 作为打包后的冒烟测试。

---

## 10. 快速排障索引

| 现象 | 先看这里 |
|---|---|
| 检测不到窗口 | `windows.detect()` 的 INFO 日志；`--list-windows --show-all` |
| 探测 0 条 | 日志里 `[内存扫描]/[本地缓存]/[缓存URL]/[URL 分析]` 段；预算是否太小；视频是否在播放 |
| 下载 403 | 日志里的 `HTTP 响应`/`响应片段`；`dis_t` 是否过期；换 Referer 的重试记录 |
| ffmpeg 报 `Option not found` | 是不是给 http 输入加了 `-ca_file`（只对 https 有效） |
| ffmpeg 报扩展名被 block | 缺 `-allowed_extensions ALL` |
| ffmpeg 报 `No trailing CRLF` | 是不是用了 `-headers` 塞 CRLF（改用 `-user_agent/-referer`） |
| 文件打不开 / 只有前段有声 | 日志里的 `文件头`/`零块占比`/`时长校验`/`体检结论`；状态是否「已完成⚠」 |
| 条目是「已完成⚠」 | 详情面板的「下载告警」；多半是缓存没下完或 CDN 给的流残缺 |
| GUI 文字被截断 | `App.ui_scale` 是否正确；是否漏了 `self.px()`；pack 换 grid + minsize |
| GUI 卡住不动 | 是否在后台线程里碰了 Tk；`_drain_events` 是否抛异常 |
| 中文变问号 | 是否用 shell 重写过源码（第 0 节） |
| 配置不生效 | 是否用了 `config.load_settings()`；改完是否「重新加载 config.json」或重启 |

---

## 11. 参考资料

- 微信 `video` 组件官方文档（`m3u8` 支持表、`autoplay` 默认 `false`、
  `referrer-policy` 固定为 `https://servicewechat.com/{appid}/{version}/page-frame.html`）：
  <https://developers.weixin.qq.com/miniprogram/dev/component/video.html>
- 小程序视频缓存导致“换源不生效”的实测（说明 WebView/播放器对 URL 与缓存的处理）：
  <https://juejin.cn/post/7362842553845088293>
- 成熟 HLS 下载器的分片/重试/选流做法（本项目的 `hls.py` 思路参考）：
  <https://github.com/nilaoda/N_m3u8DL-RE>
- “只有画面没声音/音频没合并”的常见成因（缺 ffmpeg、只下到纯视频轨）：
  <https://github.com/1989Mei/x-video-downloader>
- Chromium 自己抓窗口截图就用 `PrintWindow(hwnd, hdc, PW_CLIENTONLY | PW_RENDERFULLCONTENT)`：
  <https://chromium.googlesource.com/chromium/src.git/+/62.0.3178.1/ui/snapshot/snapshot_win.cc>
- PrintWindow 黑屏 → 第三参数设 `2`（PW_RENDERFULLCONTENT）：
  <https://stackoverflow.com/questions/69605967/c-printwindow-api-returns-black-or-partial-images-alternative-methods>
- ffmpeg HLS demuxer 选项（`allowed_extensions` 等）：
  <https://ffmpeg.org/ffmpeg-formats.html#hls-2>
- ffmpeg http 协议选项（`reconnect*`、`rw_timeout`、`user_agent`、`referer`）：
  <https://ffmpeg.org/ffmpeg-protocols.html#http>
- 微信 4.x 抓包变难、旧版才能走代理的经验（为何不用 MITM 代理做主线）：
  <https://www.yangdx.com/2022/10/226.html>
