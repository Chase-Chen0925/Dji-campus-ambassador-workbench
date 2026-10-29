@echo off
chcp 65001 >nul
cd /d "%~dp0\.."
powershell -NoProfile -ExecutionPolicy Bypass -File ".\packaging\build_windows.ps1"
if errorlevel 1 (
  echo.
  echo 构建失败，请查看上方提示。
  pause
  exit /b 1
)
echo.
echo Windows 安装包已经生成到 release 文件夹。
pause
