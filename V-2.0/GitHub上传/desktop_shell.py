"""Native application window; the UI runs in the operating system webview."""
from pathlib import Path


def create_desktop_window(url):
    try:
        import webview
    except ImportError as exc:
        raise RuntimeError('缺少桌面窗口组件，请重新安装完整版软件；源码运行请安装 requirements.txt') from exc
    webview.settings['ALLOW_DOWNLOADS'] = True
    webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = True
    window = webview.create_window(
        'DJI campus · 校园工作台', url, width=1360, height=920,
        min_size=(960, 680), background_color='#f4f1ea', text_select=True,
    )
    return webview, window


def start_desktop(webview, data_root):
    profile = Path(data_root) / 'desktop-profile'
    webview.start(
        private_mode=False, storage_path=str(profile),
        localization={
            'global.saveFile': '保存文件',
            'global.save': '保存',
            'global.cancel': '取消',
        },
    )
