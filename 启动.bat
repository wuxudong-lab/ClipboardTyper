@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 优先启动打包好的 exe
if exist "%~dp0dist\ClipboardTyper.exe" (
    start "" "%~dp0dist\ClipboardTyper.exe"
    exit /b
)

rem 否则用本机 Python 运行（需要带 tkinter 的 Python 3）
set "PYW="
if exist "D:\DevelopAppDate\Python\pythonw.exe" set "PYW=D:\DevelopAppDate\Python\pythonw.exe"
if not defined PYW (
    for /f "delims=" %%I in ('where pythonw 2^>nul') do (
        if not defined PYW set "PYW=%%I"
    )
)
if defined PYW (
    start "" "%PYW%" "%~dp0clip_typer.py"
    exit /b
)

echo 未找到 pythonw.exe，请安装 Python 3 或直接使用 dist\ClipboardTyper.exe
pause
