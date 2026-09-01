#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."
APP_NAME="抖音周更雷达"
VERSION="1.2.4"

python3 -m venv .build-venv
.build-venv/bin/python -m pip install --upgrade pip
.build-venv/bin/python -m pip install -r requirements-build.txt

rm -rf build dist "release/${APP_NAME}-${VERSION}-macOS-universal.dmg" .dmg-stage
.build-venv/bin/pyinstaller \
  --noconfirm --clean --windowed \
  --target-arch universal2 \
  --name "$APP_NAME" \
  --osx-bundle-identifier "com.douyinweeklyradar.app" \
  --add-data "static:static" \
  --hidden-import xlsxwriter \
  app.py

codesign --force --deep --sign - "dist/${APP_NAME}.app"
mkdir -p .dmg-stage release
cp -R "dist/${APP_NAME}.app" .dmg-stage/
cp "安装与使用教程.md" .dmg-stage/
ln -s /Applications .dmg-stage/Applications
hdiutil create -volname "$APP_NAME" -srcfolder .dmg-stage -ov -format UDZO \
  "release/${APP_NAME}-${VERSION}-macOS-universal.dmg"
rm -rf .dmg-stage
echo "完成：release/${APP_NAME}-${VERSION}-macOS-universal.dmg"
