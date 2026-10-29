"""Export the matching installer and a clean, explicit GitHub source manifest together."""
import argparse
import hashlib
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = [
    '.gitignore', '.github/workflows/build-installers.yml', 'README.md',
    'app.py', 'desktop_shell.py', 'device_flow.py', 'monthly_goal.py',
    'test_app.py', 'test_desktop.py', 'test_device_flow.py', 'test_monthly_goal.py', 'test_xhs_session.py',
    'requirements.txt', 'requirements-build.txt', '安装与使用教程.md', '开发者构建教程.md',
    'packaging/build_macos.sh', 'packaging/build_windows.ps1', 'packaging/windows_installer.iss',
    'packaging/一键构建Windows安装包.bat', 'packaging/create_icons.py', 'packaging/export_release.py',
    'static/index.html', 'static/app.js', 'static/style.css',
    'static/device-flow.js', 'static/device-flow.css', 'static/device-cards.css',
    'static/monthly-goal.js', 'static/monthly-goal.css',
    'static/images/devices/pocket4.png', 'static/images/devices/action6.png',
    'static/images/devices/nano.png', 'static/images/devices/osmo360.png', 'static/images/devices/SOURCES.md',
    'static/branding/dji-campus.png', 'static/branding/dji-campus.icns', 'static/branding/dji-campus.ico',
]


def export_release(version, installer):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('版本号需为 major.minor.patch')
    installer = Path(installer).resolve()
    if not installer.is_file():
        raise FileNotFoundError(installer)
    missing = [name for name in MANIFEST if not (ROOT / name).is_file()]
    if missing:
        raise FileNotFoundError('缺少发布源码：' + ', '.join(missing))
    label = 'V-2.0' if version == '2.0.0' else 'V-' + version
    destination = ROOT / label
    sources = destination / 'GitHub上传'
    binaries = destination / '安装包'
    binaries.mkdir(parents=True, exist_ok=True)
    shutil.copy2(installer, binaries / installer.name)
    for name in MANIFEST:
        target = sources / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    checksum = hashlib.sha256(installer.read_bytes()).hexdigest()
    (destination / '发布说明.md').write_text(
        f'# DJI campus {label}\n\n内部版本号：{version}。\n\n'
        '## 安装包\n\n安装包位于“安装包”目录。macOS 包支持 Apple 芯片和 Intel；Windows 安装包需要通过 Windows 本机或 GitHub Actions 构建。\n\n'
        f'本次导出文件：`{installer.name}`\n\nSHA-256：`{checksum}`\n\n'
        '## 上传 GitHub\n\n将“GitHub上传”文件夹内的全部内容放到仓库根目录，包括隐藏的 `.github` 和 `.gitignore`。'
        '该目录包含与安装包对应的源码、测试、图片、图标、依赖及构建配置，不含登录信息和本机业务数据。'
        '在 GitHub Actions 中运行 Build macOS and Windows installers，可构建对应平台安装包。'
        '后续通过打包脚本构建时，也会自动同步导出对应源码。\n\n'
        '## 本版功能\n\n'
        '- 设备长期流转：勾选后无结束日期，之后每个月持续保留，人工取消当天释放，过去记录保留。\n'
        '- 月度目标的查看月份采用下拉选择，并保留有投稿记录的历史月份。\n'
        '- 投稿水印审核在大于 200 的右侧新增大于 1 万的检测，统计卡片与 Excel 导出同步显示；严格大于 10,000 才通过。\n'
        '- 保留投稿审核、作品去重统计、月度 120 条目标、小红书会话复用和设备图片等已有功能。\n\n'
        '升级前请退出旧版。软件沿用原有本机数据目录，升级不会清除已有设备和月度记录。\n', encoding='utf-8')
    print(f'已同步导出 {label}：安装包 + {len(MANIFEST)} 个 GitHub 源码文件')
    print(destination)
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', required=True)
    parser.add_argument('--installer', required=True)
    args = parser.parse_args()
    export_release(args.version, args.installer)
