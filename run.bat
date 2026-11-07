@echo off
chcp 65001 >nul
title 微信小程序视频下载器
cd /d "%~dp0"

set "PY=python"
where %PY% >nul 2>nul
if errorlevel 1 (
    if exist "D:\MiniPrograms\Python3117\python.exe" (
        set "PY=D:\MiniPrograms\Python3117\python.exe"
    ) else (
        echo [!] 未找到 python，请先安装 Python 3.9+ 并加入 PATH
        pause
        exit /b 1
    )
)

set PYTHONIOENCODING=utf-8
"%PY%" run.py %*

if errorlevel 1 (
    echo.
    echo [i] 程序异常退出，最近的日志在 logs\ 目录
    pause
)
