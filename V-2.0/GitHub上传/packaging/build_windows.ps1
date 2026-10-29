$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$AppName = "DouyinWeeklyRadar"
$Version = "2.0.0"

py -3 -m venv .build-venv
& .\.build-venv\Scripts\python.exe -m pip install --upgrade pip
& .\.build-venv\Scripts\python.exe -m pip install -r requirements-build.txt

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
& .\.build-venv\Scripts\pyinstaller.exe `
  --noconfirm --clean --onefile --windowed `
  --name $AppName `
  --icon "static\branding\dji-campus.ico" `
  --add-data "static;static" `
  --hidden-import xlsxwriter `
  --hidden-import webview.platforms.winforms `
  --collect-data webview `
  app.py

$Iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $Iscc)) {
  throw "未找到 Inno Setup 6。请从 https://jrsoftware.org/isdl.php 安装后重试。"
}
New-Item -ItemType Directory -Force release | Out-Null
& $Iscc "packaging\windows_installer.iss"
Write-Host "完成：release\DouyinWeeklyRadar-$Version-Windows-Setup.exe"
& .\.build-venv\Scripts\python.exe packaging\export_release.py --version $Version --installer "release\DouyinWeeklyRadar-$Version-Windows-Setup.exe"
