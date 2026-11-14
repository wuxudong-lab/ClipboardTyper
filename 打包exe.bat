@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=D:\DevelopAppDate\Python\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m PyInstaller --noconfirm --onefile --windowed --name "ClipboardTyper" ^
    --distpath dist --workpath build --specpath build clip_typer.py
echo.
echo 打包完成：dist\ClipboardTyper.exe
pause
