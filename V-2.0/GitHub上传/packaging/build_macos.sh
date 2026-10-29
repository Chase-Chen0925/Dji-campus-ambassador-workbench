#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."
APP_NAME="DJI campus"
VERSION="2.0.0"
APP_DIST="dist/macos-${VERSION}"
APP_BUILD="build/macos-${VERSION}"
STAGE=".dmg-stage-${VERSION}"

python3 -m venv .build-venv
.build-venv/bin/python -m pip install --upgrade pip
.build-venv/bin/python -m pip install -r requirements-build.txt

mkdir -p "$APP_DIST" "$APP_BUILD" "$STAGE"
.build-venv/bin/pyinstaller \
  --noconfirm --clean --windowed \
  --target-arch universal2 \
  --name "$APP_NAME" \
  --distpath "$APP_DIST" --workpath "$APP_BUILD" \
  --icon "static/branding/dji-campus.icns" \
  --osx-bundle-identifier "com.douyinweeklyradar.app" \
  --add-data "static:static" \
  --hidden-import xlsxwriter \
  --hidden-import webview.platforms.cocoa \
  --collect-data webview \
  app.py

/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString ${VERSION}" "${APP_DIST}/${APP_NAME}.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :CFBundleVersion string ${VERSION}" "${APP_DIST}/${APP_NAME}.app/Contents/Info.plist"
codesign --force --deep --sign - "${APP_DIST}/${APP_NAME}.app"
mkdir -p release
ditto "${APP_DIST}/${APP_NAME}.app" "${STAGE}/${APP_NAME}.app"
cp "安装与使用教程.md" "$STAGE/"
if [ ! -e "$STAGE/Applications" ]; then ln -s /Applications "$STAGE/Applications"; fi
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO \
  "release/DJI-campus-${VERSION}-macOS-universal.dmg"
echo "完成：release/DJI-campus-${VERSION}-macOS-universal.dmg"
.build-venv/bin/python packaging/export_release.py --version "$VERSION" --installer "release/DJI-campus-${VERSION}-macOS-universal.dmg"
