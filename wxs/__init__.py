"""WechatMiniprogramScrab — 微信小程序窗口检测与视频/图片批量下载工具。

模块结构
--------
config      全局配置与路径解析（config.json 默认值）
logsetup    日志系统（文件 + 控制台 + GUI 队列）
winapi      Win32/ctypes 底层封装（枚举窗口、进程内存读取、DWM 边界）
windows     微信小程序窗口检测、分类与选择
urls        URL 提取、清洗、分类、打分（视频/图片）
memory_scan 进程内存扫描（从运行中的小程序里捞出媒体 URL）
cache_scan  本地缓存扫描（小程序 temp 目录里的真实视频/图片 + Chromium 缓存 URL）
cdp         Chrome DevTools Protocol 客户端（可选后端）
hls         m3u8 解析、主清单展开、分片级下载（比 ffmpeg 直连更完整）
capture     窗口截图（PrintWindow，抗遮挡；只用于预览，不再录制）
downloader  下载器（HTTP 断点续传 / HLS 分片 / 本地复制 / 完整性体检）
probe       多后端总调度
gui         tkinter 图形界面
"""

__version__ = "1.1.0"
__app_name__ = "WechatMiniProgramScrab"
__all__ = ["__version__", "__app_name__"]
