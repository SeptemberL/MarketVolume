@echo off
rem ============================================
rem  全市场量能监控 - Windows 悬浮框 启动脚本
rem  双击运行即可, 无控制台窗口(优先 pythonw)
rem  依赖: Python 3.8+ (官方安装包自带 tkinter, 无需 pip 安装任何包)
rem ============================================
cd /d "%~dp0"

set "PYCMD="
rem 1) 优先用 pythonw(无黑色控制台)
where pythonw >nul 2>nul
if %errorlevel%==0 (
    set "PYCMD=pythonw"
    goto :run
)
rem 2) 退而求其次用 python
where python >nul 2>nul
if %errorlevel%==0 (
    set "PYCMD=python"
    goto :run
)
rem 3) Windows 商店版 launcher
where py >nul 2>nul
if %errorlevel%==0 (
    set "PYCMD=py -3"
    goto :run
)

echo [错误] 未找到 Python 环境!
echo 请先安装 Python 3.8+ : https://www.python.org/downloads/windows/
echo 安装时务必勾选 "Add Python to PATH"
pause
exit /b 1

:run
echo 正在启动 量能监控悬浮框...
if "%PYCMD%"=="pythonw" (
    start "" pythonw "%~dp0volume_float_win.py"
) else (
    %PYCMD% "%~dp0volume_float_win.py"
)
exit /b 0

