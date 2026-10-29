#!/usr/bin/env python3
"""本地抖音 / 小红书主页近 7 天作品检查器。"""

from __future__ import annotations

import argparse
import atexit
import base64
import html
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

import requests
import xlsxwriter
from device_flow import DeviceFlow, FlowError
from monthly_goal import MonthlyGoal, GoalError

try:
    import websocket
except ImportError:  # startup still works and shows an actionable message
    websocket = None


IS_FROZEN = bool(getattr(sys, "frozen", False))
APP_ID = "DouyinWeeklyRadar"
APP_VERSION = "2.0.0"
DESKTOP_WINDOW = None
ROOT = Path(__file__).resolve().parent
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", ROOT))
STATIC = BUNDLE_ROOT / "static"


def _user_data_root() -> Path:
    if not IS_FROZEN:
        return ROOT
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "DouyinWeeklyRadar"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "DouyinWeeklyRadar"
    return Path.home() / ".local" / "share" / "DouyinWeeklyRadar"


DATA_ROOT = _user_data_root()
DEVICE_FLOW = DeviceFlow(DATA_ROOT / 'device-flow.json')
MONTHLY_GOAL = MonthlyGoal(DATA_ROOT / 'campus-submissions.sqlite3')
OUTPUTS = DATA_ROOT / "outputs"
DOUYIN_CHROME_PROFILE = DATA_ROOT / "chrome-profile"
XHS_CHROME_PROFILE = DATA_ROOT / "xiaohongshu-chrome-profile"
PUBLIC_CHROME_PROFILE = DATA_ROOT / "submission-chrome-profile"


def _chrome_candidates() -> List[Path]:
    if sys.platform == "darwin":
        return [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ]
    if sys.platform == "win32":
        roots = [
            os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", ""),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        suffixes = [
            Path("Google/Chrome/Application/chrome.exe"),
            Path("Microsoft/Edge/Application/msedge.exe"),
        ]
        return [Path(root) / suffix for root in roots if root for suffix in suffixes]
    return [Path("/usr/bin/google-chrome"), Path("/usr/bin/chromium"), Path("/usr/bin/microsoft-edge")]


CHROME_PATHS = _chrome_candidates()
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class CheckError(RuntimeError):
    pass


class XhsLoginRequired(CheckError):
    pass


@dataclass
class Work:
    id: str
    description: str
    tags: List[str]
    likes: int
    likes_text: str
    over_1000: bool
    published_at: str
    published_timestamp: int
    url: str


def parse_like_count(value: Any) -> int:
    """把 1.2万、3w、1,234 等格式转换为整数。"""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower().replace(",", "").replace("+", "")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([万w千k]?)", text)
    if not match:
        return 0
    number = float(match.group(1))
    multiplier = {"万": 10000, "w": 10000, "千": 1000, "k": 1000}.get(match.group(2), 1)
    return int(number * multiplier)


def extract_tags(description: str, text_extra: Optional[Iterable[Dict[str, Any]]] = None) -> List[str]:
    tags: List[str] = []
    for item in text_extra or []:
        if item.get("hashtag_name"):
            tags.append(str(item["hashtag_name"]).strip())
    tags.extend(re.findall(r"#([^#\s，。！？、,.;；:：!！?？]+)", description or ""))
    return list(dict.fromkeys(tag for tag in tags if tag))


def _first(data: Dict[str, Any], *paths: str, default: Any = None) -> Any:
    for path in paths:
        current: Any = data
        for key in path.split("."):
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]
        if current is not None:
            return current
    return default


def normalize_work(raw: Dict[str, Any]) -> Optional[Work]:
    work_id = str(_first(raw, "aweme_id", "id", "group_id", default=""))
    timestamp = int(_first(raw, "create_time", "createTime", default=0) or 0)
    if not work_id or not timestamp:
        return None
    description = str(_first(raw, "desc", "description", default="") or "")
    likes_raw = _first(raw, "statistics.digg_count", "stats.diggCount", "digg_count", default=0)
    likes = parse_like_count(likes_raw)
    published = datetime.fromtimestamp(timestamp, timezone(timedelta(hours=8)))
    return Work(
        id=work_id,
        description=description,
        tags=extract_tags(description, raw.get("text_extra") or raw.get("textExtra")),
        likes=likes,
        likes_text=f"{likes:,}",
        over_1000=likes > 1000,
        published_at=published.isoformat(timespec="seconds"),
        published_timestamp=timestamp,
        url=f"https://www.douyin.com/video/{work_id}",
    )


def _walk_for_awemes(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        if ("aweme_id" in value or "group_id" in value) and (
            "create_time" in value or "createTime" in value
        ):
            yield value
        for child in value.values():
            yield from _walk_for_awemes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_for_awemes(child)


def _decode_embedded_json(page: str) -> List[Any]:
    candidates: List[str] = []
    patterns = [
        r'<script[^>]+id=["\']RENDER_DATA["\'][^>]*>(.*?)</script>',
        r'<script[^>]+id=["\']SIGI_STATE["\'][^>]*>(.*?)</script>',
        r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
    ]
    for pattern in patterns:
        candidates.extend(re.findall(pattern, page, flags=re.I | re.S))
    decoded: List[Any] = []
    for candidate in candidates:
        text = html.unescape(candidate.strip())
        for possible in (text, urllib.parse.unquote(text)):
            try:
                decoded.append(json.loads(possible))
                break
            except (json.JSONDecodeError, ValueError):
                continue
    return decoded


def _extract_sec_uid(url: str, page: str) -> Optional[str]:
    decoded_url = urllib.parse.unquote(url)
    patterns = [
        r"/user/([^/?#]+)",
        r'["\']sec_uid["\']\s*:\s*["\']([^"\']+)',
        r'["\']secUid["\']\s*:\s*["\']([^"\']+)',
    ]
    for source in (decoded_url, page, urllib.parse.unquote(page)):
        for pattern in patterns:
            match = re.search(pattern, source)
            if match and match.group(1).startswith("MS4wLjAB"):
                return match.group(1)
    return None


def _cookie_dict(cookie_text: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for part in cookie_text.split(";"):
        if "=" in part:
            key, value = part.strip().split("=", 1)
            result[key] = value
    return result


def detect_platform(account_url: str) -> str:
    """识别主页链接平台。"""
    account_url = account_url.strip()
    if not re.match(r"^https?://", account_url):
        raise CheckError("请输入以 http:// 或 https:// 开头的主页链接")
    parsed = urllib.parse.urlparse(account_url)
    hostname = (parsed.hostname or "").lower()
    if hostname.endswith(("douyin.com", "iesdouyin.com")):
        return "douyin"
    if hostname.endswith(("xiaohongshu.com", "xhslink.com")):
        return "xiaohongshu"
    raise CheckError("仅支持抖音或小红书主页链接")


def validate_account_url(account_url: str) -> str:
    account_url = account_url.strip()
    detect_platform(account_url)
    return account_url


def _normalise_timestamp(value: Any, work_id: str = "") -> int:
    try:
        timestamp = int(float(value or 0))
    except (TypeError, ValueError):
        timestamp = 0
    while timestamp > 10_000_000_000:
        timestamp //= 1000
    # 小红书笔记 ID 通常以 8 位十六进制时间开头；仅在显式时间缺失时作合理性回退。
    if not timestamp and re.fullmatch(r"[0-9a-fA-F]{24}", work_id):
        inferred = int(work_id[:8], 16)
        if 1_350_000_000 <= inferred <= int(time.time()) + 86400:
            timestamp = inferred
    return timestamp


def _xhs_tags(card: Dict[str, Any], description: str) -> List[str]:
    tags = extract_tags(description)
    for path in ("tag_list", "tags", "topic_list", "topics"):
        values = card.get(path) or []
        if isinstance(values, dict):
            values = list(values.values())
        if not isinstance(values, list):
            continue
        for item in values:
            if isinstance(item, str):
                name = item
            elif isinstance(item, dict):
                name = _first(item, "name", "tag_name", "tagName", "topic_name", "title", default="")
            else:
                name = ""
            name = str(name or "").strip().lstrip("#")
            if name:
                tags.append(name)
    return list(dict.fromkeys(tags))


def normalize_xhs_note(raw: Dict[str, Any]) -> Optional[Work]:
    card = raw.get("note_card") or raw.get("noteCard") or raw
    if not isinstance(card, dict):
        return None
    work_id = str(_first(raw, "id", "note_id", "noteId", default="") or _first(card, "id", "note_id", "noteId", default=""))
    if not work_id:
        return None
    description = str(_first(card, "display_title", "displayTitle", "title", "desc", "description", default="") or "")
    timestamp = _normalise_timestamp(
        _first(card, "time", "create_time", "createTime", "publish_time", "publishTime", default=0),
        work_id,
    )
    if not timestamp:
        return None
    likes_raw = _first(
        card, "interact_info.liked_count", "interactInfo.likedCount", "liked_count", "likedCount", default=0
    )
    likes = parse_like_count(likes_raw)
    published = datetime.fromtimestamp(timestamp, timezone(timedelta(hours=8)))
    return Work(
        id=work_id,
        description=description,
        tags=_xhs_tags(card, description),
        likes=likes,
        likes_text=f"{likes:,}",
        over_1000=likes > 1000,
        published_at=published.isoformat(timespec="seconds"),
        published_timestamp=timestamp,
        url=f"https://www.xiaohongshu.com/explore/{work_id}",
    )


def _walk_for_xhs_notes(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        if ("note_card" in value or "noteCard" in value) and any(key in value for key in ("id", "note_id", "noteId")):
            yield value
        elif any(key in value for key in ("note_id", "noteId")) and any(
            key in value for key in ("display_title", "displayTitle", "title", "desc")
        ):
            yield value
        for child in value.values():
            yield from _walk_for_xhs_notes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_for_xhs_notes(child)


def _xhs_notes_from_dom(cards: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """当作品接口被风控或响应体被浏览器回收时，从已渲染的作品卡片回退解析。"""
    notes: List[Dict[str, Any]] = []
    for card in cards:
        href = str(card.get("url", ""))
        match = re.search(r"/(?:explore|discovery/item)/([0-9a-fA-F]{24})", href)
        if not match:
            continue
        work_id = match.group(1)
        text = str(card.get("text", "") or "").strip()
        title = str(card.get("title", "") or "").strip()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not title:
            title = next((
                line for line in lines
                if not re.fullmatch(r"[\d,.]+(?:\.[\d]+)?\s*[万w千k]?", line, flags=re.I)
                and line not in ("图文", "视频")
            ), "")
        likes_text = "0"
        for line in reversed(lines):
            if re.fullmatch(r"[\d,.]+(?:\.\d+)?\s*[万w千k]?\+?", line, flags=re.I):
                likes_text = line
                break
        notes.append({
            "id": work_id,
            "note_card": {
                "display_title": title,
                "interact_info": {"liked_count": likes_text},
            },
        })
    return list({str(note["id"]): note for note in notes}.values())


def _response_json(body: str) -> Optional[Dict[str, Any]]:
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def build_result(
    account_url: str,
    raw_awemes: Iterable[Dict[str, Any]],
    days: int = 7,
    warnings: Optional[List[str]] = None,
    account_name: str = "",
    platform: str = "douyin",
) -> Dict[str, Any]:
    unique: Dict[str, Work] = {}
    for raw in raw_awemes:
        work = normalize_xhs_note(raw) if platform == "xiaohongshu" else normalize_work(raw)
        if work:
            unique[work.id] = work

    cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    recent = sorted(
        (work for work in unique.values() if work.published_timestamp >= cutoff),
        key=lambda item: item.published_timestamp,
        reverse=True,
    )
    result_warnings = list(warnings or [])
    if not unique:
        platform_name = "小红书" if platform == "xiaohongshu" else "抖音"
        result_warnings.append(f"没有读取到作品，当前结果不能用于判断是否更新。可尝试打开{platform_name}登录窗口后重试。")
    return {
        "account_name": account_name or "—",
        "account_url": account_url,
        "platform": "小红书" if platform == "xiaohongshu" else "抖音",
        "checked_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "days": days,
        "has_updates": bool(recent),
        "recent_count": len(recent),
        "over_1000_count": sum(item.over_1000 for item in recent),
        "works": [asdict(item) for item in recent],
        "warnings": result_warnings,
        "data_status": "ok" if unique else "unavailable",
        "app_version": APP_VERSION,
    }


class ChromeCollector:
    """通过本机真实 Chrome 加载平台主页，捕获浏览器已签名的作品接口。"""

    def __init__(self, platform: str, profile: Path, home_url: str) -> None:
        self.platform = platform
        self.profile = profile
        self.home_url = home_url
        self.process: Optional[subprocess.Popen] = None
        self.port: Optional[int] = None
        self.lock = threading.Lock()
        self.submission_target_id = ''
        self.login_waiting = False
        self.login_timeout = 180

    def _submission_target(self) -> Dict[str, Any]:
        """Keep XHS sessionStorage and login state in a single persistent browser tab."""
        if self.platform == 'xiaohongshu':
            pages = requests.get(f'http://127.0.0.1:{self.port}/json', timeout=5).json()
            target = next((p for p in pages if p.get('type') == 'page' and p.get('id') == self.submission_target_id), None)
            if target is None:
                target = next((p for p in pages if p.get('type') == 'page' and
                               (urllib.parse.urlparse(p.get('url', '')).hostname or '').endswith('xiaohongshu.com')), None)
            if target is not None:
                self.submission_target_id = target['id']
                return target
        target = requests.put(f'http://127.0.0.1:{self.port}/json/new?about:blank', timeout=5).json()
        if self.platform == 'xiaohongshu':
            self.submission_target_id = target['id']
        return target

    def _chrome_path(self) -> Path:
        for path in CHROME_PATHS:
            if path.is_file():
                return path
        raise CheckError("未找到 Google Chrome 或 Microsoft Edge，请先安装其中一个浏览器")

    def _alive(self) -> bool:
        if not self.port:
            return False
        try:
            return requests.get(f"http://127.0.0.1:{self.port}/json/version", timeout=1).ok
        except requests.RequestException:
            return False

    def start(self) -> None:
        if websocket is None:
            raise CheckError("缺少 websocket-client，请重新运行：python3 -m pip install -r requirements.txt")
        if self._alive():
            return
        self.profile.mkdir(parents=True, exist_ok=True)
        active_port = self.profile / "DevToolsActivePort"
        if active_port.exists():
            try:
                old_port = int(active_port.read_text().splitlines()[0])
                if requests.get(f"http://127.0.0.1:{old_port}/json/version", timeout=1).ok:
                    self.port = old_port
                    return
            except (OSError, ValueError, IndexError, requests.RequestException):
                pass
            active_port.unlink(missing_ok=True)

        self.process = subprocess.Popen(
            [
                str(self._chrome_path()), "--remote-debugging-port=0", "--remote-allow-origins=*",
                "--no-first-run", "--no-default-browser-check", f"--user-data-dir={self.profile}",
                "--window-size=1280,900", self.home_url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(150):
            if active_port.exists():
                try:
                    self.port = int(active_port.read_text().splitlines()[0])
                    if self._alive():
                        return
                except (OSError, ValueError, IndexError):
                    pass
            time.sleep(0.1)
        raise CheckError("Chrome 启动超时，请关闭刚打开的窗口后重试")

    def open_login(self) -> None:
        with self.lock:
            self.start()
            if self.platform == 'xiaohongshu':
                target = self._submission_target()
                requests.get(f'http://127.0.0.1:{self.port}/json/activate/{target["id"]}', timeout=5).raise_for_status()
                if 'xiaohongshu.com' not in target.get('url', ''):
                    ws = websocket.create_connection(target['webSocketDebuggerUrl'], timeout=2, origin=f'http://127.0.0.1:{self.port}')
                    try:
                        self._send(ws, 1, 'Page.navigate', {'url': self.home_url})
                    finally:
                        ws.close()
                return
            requests.put(
                f"http://127.0.0.1:{self.port}/json/new?{urllib.parse.quote(self.home_url, safe=':/?=&')}",
                timeout=5,
            ).raise_for_status()

    def is_logged_in(self) -> bool:
        """检查专用 Chrome 中是否存在平台登录 Cookie。"""
        with self.lock:
            self.start()
            pages = requests.get(f"http://127.0.0.1:{self.port}/json", timeout=3).json()
            domain = "xiaohongshu.com" if self.platform == "xiaohongshu" else "douyin.com"
            page = next((
                item for item in pages
                if item.get("type") == "page" and domain in item.get("url", "")
            ), None) or next((item for item in pages if item.get("type") == "page"), None)
            if not page:
                return False
            os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
            ws = websocket.create_connection(
                page["webSocketDebuggerUrl"], timeout=2, origin=f"http://127.0.0.1:{self.port}"
            )
            try:
                self._send(ws, 1, "Network.getAllCookies")
                deadline = time.time() + 3
                while time.time() < deadline:
                    try:
                        message = json.loads(ws.recv())
                    except Exception:
                        continue
                    if message.get("id") == 1:
                        cookies = message.get("result", {}).get("cookies", [])
                        values = {
                            item.get("name"): item.get("value") for item in cookies
                            if domain in item.get("domain", "")
                        }
                        if self.platform == "xiaohongshu":
                            if not values.get("web_session"):
                                return False
                            self._send(ws, 2, "Runtime.evaluate", {
                                "expression": """(() => {
                                    const body = document.body ? document.body.innerText : '';
                                    const onLoginPage = location.pathname.startsWith('/login')
                                        || (body.includes('手机号登录') && body.includes('获取验证码'));
                                    return {url: location.href, ready: body.length > 20, onLoginPage};
                                })()""",
                                "returnByValue": True,
                            })
                            continue
                        return bool(values.get("sessionid") or values.get("sessionid_ss") or values.get("LOGIN_STATUS") == "1")
                    if message.get("id") == 2 and self.platform == "xiaohongshu":
                        state = message.get("result", {}).get("result", {}).get("value", {}) or {}
                        return bool(state.get("ready") and not state.get("onLoginPage"))
                return False
            finally:
                ws.close()

    @staticmethod
    def _send(ws: Any, command_id: int, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        ws.send(json.dumps({"id": command_id, "method": method, "params": params or {}}))

    def collect(self, account_url: str, cookie_text: str = "") -> Dict[str, Any]:
        account_url = validate_account_url(account_url)
        if detect_platform(account_url) != self.platform:
            raise CheckError("链接平台与当前采集器不匹配")
        with self.lock:
            self.start()
            target = requests.put(f"http://127.0.0.1:{self.port}/json/new?about:blank", timeout=5).json()
            target_id = target["id"]
            os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
            ws = websocket.create_connection(
                target["webSocketDebuggerUrl"], timeout=1.0, origin=f"http://127.0.0.1:{self.port}"
            )
            response_ids: List[str] = []
            target_request_ids: set[str] = set()
            body_seen: set[str] = set()
            pending_body_commands: Dict[int, str] = {}
            response_statuses: List[int] = []
            api_messages: List[str] = []
            raw_awemes: List[Dict[str, Any]] = []
            account_name = ""
            body_command_id = 1000

            def consume_body(body: str) -> None:
                payload = _response_json(body)
                if not payload:
                    return
                if self.platform == "xiaohongshu":
                    raw_awemes.extend(_walk_for_xhs_notes(payload))
                    message = _first(payload, "msg", "message", "data.message", default="")
                    code = _first(payload, "code", "data.code", default="")
                    if message and str(message).lower() not in ("success", "ok"):
                        detail = f"{code}: {message}" if code not in (None, "") else str(message)
                        api_messages.append(detail)
                else:
                    raw_awemes.extend(payload.get("aweme_list") or [])
            try:
                self._send(ws, 1, "Network.enable")
                self._send(ws, 2, "Page.enable")
                if cookie_text.strip():
                    cookies = [
                        {"name": key, "value": value, "domain": ".douyin.com", "path": "/", "secure": True}
                        for key, value in _cookie_dict(cookie_text).items()
                    ]
                    self._send(ws, 3, "Network.setCookies", {"cookies": cookies})
                self._send(ws, 4, "Page.navigate", {"url": account_url})

                deadline = time.time() + 18
                while time.time() < deadline:
                    try:
                        message = json.loads(ws.recv())
                    except Exception:
                        continue
                    if message.get("method") == "Network.responseReceived":
                        params = message["params"]
                        url = params["response"]["url"]
                        wanted = (
                            "/api/sns/web/v1/user_posted" if self.platform == "xiaohongshu"
                            else "/aweme/v1/web/aweme/post/"
                        )
                        if wanted in url:
                            request_id = params["requestId"]
                            response_ids.append(request_id)
                            target_request_ids.add(request_id)
                            response_statuses.append(int(params["response"].get("status", 0) or 0))
                    elif message.get("method") == "Network.loadingFinished":
                        request_id = message.get("params", {}).get("requestId", "")
                        if request_id in target_request_ids and request_id not in body_seen:
                            body_command_id += 1
                            pending_body_commands[body_command_id] = request_id
                            self._send(ws, body_command_id, "Network.getResponseBody", {"requestId": request_id})
                    elif message.get("id") in pending_body_commands:
                        command_id = int(message["id"])
                        request_id = pending_body_commands.pop(command_id)
                        result = message.get("result", {})
                        body = result.get("body", "")
                        if result.get("base64Encoded") and body:
                            body = base64.b64decode(body).decode("utf-8", "replace")
                        if body:
                            body_seen.add(request_id)
                            consume_body(body)

                # 某些页面不会及时发出 loadingFinished，对未读取的响应再做一次补偿读取。
                next_id = max(1100, body_command_id + 10)
                for request_id in dict.fromkeys(response_ids):
                    if request_id in body_seen:
                        continue
                    next_id += 1
                    self._send(ws, next_id, "Network.getResponseBody", {"requestId": request_id})
                    body = ""
                    body_deadline = time.time() + 3
                    while time.time() < body_deadline:
                        try:
                            message = json.loads(ws.recv())
                        except Exception:
                            continue
                        if message.get("id") == next_id:
                            result = message.get("result", {})
                            body = result.get("body", "")
                            if result.get("base64Encoded"):
                                body = base64.b64decode(body).decode("utf-8", "replace")
                            break
                    if body:
                        body_seen.add(request_id)
                        consume_body(body)

                next_id += 1
                self._send(
                    ws, next_id, "Runtime.evaluate",
                    {
                        "expression": """(() => ({
                            html: document.documentElement.outerHTML,
                            title: document.title,
                            text: document.body.innerText,
                            url: location.href,
                            cards: Array.from(document.querySelectorAll('a[href*="/explore/"],a[href*="/discovery/item/"]')).map(a => {
                                const box = a.closest('section') || a.closest('.note-item') || a.parentElement || a;
                                const image = a.querySelector('img');
                                return {
                                    url: a.href,
                                    text: (box.innerText || a.innerText || '').slice(0, 1000),
                                    title: a.getAttribute('title') || (image && image.getAttribute('alt')) || ''
                                };
                            })
                        }))()""",
                        "returnByValue": True,
                    },
                )
                page_data: Dict[str, Any] = {}
                eval_deadline = time.time() + 3
                while time.time() < eval_deadline:
                    try:
                        message = json.loads(ws.recv())
                    except Exception:
                        continue
                    if message.get("id") == next_id:
                        page_data = message.get("result", {}).get("result", {}).get("value", {}) or {}
                        break
                for state in _decode_embedded_json(page_data.get("html", "")):
                    raw_awemes.extend(
                        _walk_for_xhs_notes(state) if self.platform == "xiaohongshu" else _walk_for_awemes(state)
                    )
                if self.platform == "xiaohongshu":
                    raw_awemes.extend(_xhs_notes_from_dom(page_data.get("cards") or []))
                if self.platform == "xiaohongshu":
                    next_id += 1
                    self._send(ws, next_id, "Runtime.evaluate", {
                        "expression": "JSON.parse(JSON.stringify(window.__INITIAL_STATE__ || {}))",
                        "returnByValue": True,
                    })
                    state_deadline = time.time() + 3
                    while time.time() < state_deadline:
                        try:
                            message = json.loads(ws.recv())
                        except Exception:
                            continue
                        if message.get("id") == next_id:
                            state = message.get("result", {}).get("result", {}).get("value", {}) or {}
                            raw_awemes.extend(_walk_for_xhs_notes(state))
                            break
                title = str(page_data.get("title", ""))
                account_name = re.sub(r"\s*[-—_].*$", "", title).strip()
                page_text = str(page_data.get("text", ""))
                final_url = str(page_data.get("url", ""))
                warnings: List[str] = []
                if self.platform == "xiaohongshu":
                    if account_name in ("小红书", "安全限制", "登录小红书"):
                        account_name = ""
                    if not raw_awemes and (
                        "安全限制" in page_text or "IP存在风险" in page_text
                        or "300012" in page_text or "website-login/error" in final_url
                    ):
                        warnings.append("小红书返回安全限制（风控代码 300012），请切换可靠网络或登录后重试。")
                    elif not raw_awemes and "登录" in page_text:
                        warnings.append("小红书未登录或登录已失效，可先使用“小红书登录（可选）”后重试。")
                    if not raw_awemes and api_messages:
                        warnings.append("小红书作品接口提示：" + "；".join(dict.fromkeys(api_messages)))
                else:
                    if not raw_awemes and "登录" in page_text:
                        warnings.append("抖音尚未登录或登录已失效，请在弹出的 Chrome 中扫码登录后重试。")
                    if not raw_awemes and "服务异常" in page_text:
                        warnings.append("抖音作品接口提示服务异常，通常是登录状态或风控导致。")
                failed_statuses = sorted({status for status in response_statuses if status >= 400})
                if not raw_awemes and failed_statuses:
                    warnings.append("作品接口 HTTP 状态：" + "、".join(map(str, failed_statuses)))
                if not raw_awemes and not response_ids and not warnings:
                    warnings.append("页面未发起作品列表请求，请检查专用登录窗口中是否有验证码或网络拦截。")
                return build_result(
                    account_url, raw_awemes, warnings=warnings, account_name=account_name,
                    platform=self.platform,
                )
            finally:
                try:
                    ws.close()
                finally:
                    try:
                        requests.get(f"http://127.0.0.1:{self.port}/json/close/{target_id}", timeout=2)
                    except requests.RequestException:
                        pass

    def collect_submission_page(self, target_url: str, wait_seconds: int = 14) -> Dict[str, Any]:
        """加载单个作品页，同时保留页面文本和与作品有关的 JSON 响应。"""
        with self.lock:
            self.start()
            target = self._submission_target()
            target_id = target["id"]
            os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
            ws = websocket.create_connection(
                target["webSocketDebuggerUrl"], timeout=1.0,
                origin=f"http://127.0.0.1:{self.port}",
            )
            response_ids: List[str] = []
            watched_ids: set[str] = set()
            pending: Dict[int, str] = {}
            payloads: List[Dict[str, Any]] = []
            command_id = 2000

            def relevant(url: str, mime_type: str) -> bool:
                if "json" not in mime_type.lower():
                    return False
                return any(part in url for part in (
                    "/aweme/", "/api/sns/", "finder", "channels.weixin.qq.com",
                    "/x/web-interface/", "/x/tag/", "view.0.0.1", "getfeed",
                ))

            def consume(message: Dict[str, Any], request_id: str) -> None:
                result = message.get("result", {})
                body = result.get("body", "")
                if result.get("base64Encoded") and body:
                    body = base64.b64decode(body).decode("utf-8", "replace")
                payload = _response_json(body)
                if payload:
                    payloads.append(payload)

            try:
                self._send(ws, 1, "Network.enable")
                self._send(ws, 2, "Page.enable")
                self._send(ws, 3, "Network.setCacheDisabled", {"cacheDisabled": True})
                navigation = {'url': target_url}
                if self.platform == 'xiaohongshu':
                    navigation['referrer'] = 'https://www.xiaohongshu.com/explore'
                self._send(ws, 4, 'Page.navigate', navigation)
                deadline = time.time() + wait_seconds
                next_popup_close = time.time() + 1.5
                next_login_probe = time.time() + 2
                login_probe_id = None
                login_deadline = None
                while time.time() < deadline:
                    if self.platform == 'xiaohongshu' and login_probe_id is None and time.time() >= next_login_probe:
                        command_id += 1
                        login_probe_id = command_id
                        self._send(ws, command_id, 'Runtime.evaluate', {
                            'expression': """(() => {
                                const body = document.body ? document.body.innerText : '';
                                const visible = el => !!(el && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
                                const loginBox = [...document.querySelectorAll('.login-container,.login-modal,.login-modal-mask')].some(visible);
                                const login = location.pathname.startsWith('/login') || loginBox || (body.includes('手机号登录') && body.includes('获取验证码'));
                                const captcha = /完成验证|拖动滑块|请通过验证/.test(body);
                                return {login, captcha, url: location.href};
                            })()""", 'returnByValue': True,
                        })
                    if self.platform == "public" and time.time() >= next_popup_close:
                        # 视频号分享页首次打开可能出现登录/引导弹窗。只关闭可见弹窗，
                        # 不点击登录、授权或任何会提交信息的按钮。
                        command_id += 1
                        self._send(ws, command_id, "Runtime.evaluate", {
                            "expression": """(() => {
                                const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                                const selectors = [
                                    '[aria-label="关闭"]', '[aria-label="close"]',
                                    '.dialog-close', '.modal-close', '.close-btn',
                                    '[class*="dialog"] [class*="close"]',
                                    '[class*="modal"] [class*="close"]'
                                ];
                                for (const selector of selectors) {
                                    const el = [...document.querySelectorAll(selector)].find(visible);
                                    if (el) { el.click(); return true; }
                                }
                                const textClose = [...document.querySelectorAll('button,[role="button"]')]
                                    .find(el => visible(el) && ['关闭','我知道了','暂不登录'].includes((el.innerText || '').trim()));
                                if (textClose) { textClose.click(); return true; }
                                return false;
                            })()""",
                            "returnByValue": True,
                        })
                        command_id += 1
                        self._send(ws, command_id, "Input.dispatchKeyEvent", {
                            "type": "keyDown", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27,
                        })
                        command_id += 1
                        self._send(ws, command_id, "Input.dispatchKeyEvent", {
                            "type": "keyUp", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27,
                        })
                        next_popup_close = time.time() + 3
                    try:
                        message = json.loads(ws.recv())
                    except Exception:
                        continue
                    method = message.get("method")
                    if login_probe_id is not None and message.get('id') == login_probe_id:
                        state = message.get('result', {}).get('result', {}).get('value', {}) or {}
                        login_probe_id = None
                        next_login_probe = time.time() + 2
                        if state.get('login') or state.get('captcha'):
                            if login_deadline is None:
                                login_deadline = time.time() + self.login_timeout
                                try:
                                    requests.get(f'http://127.0.0.1:{self.port}/json/activate/{target_id}', timeout=2)
                                except requests.RequestException:
                                    pass
                            deadline = login_deadline
                            self.login_waiting = True
                        elif self.login_waiting:
                            self.login_waiting = False
                            deadline = time.time() + wait_seconds
                            # QR login can return to the home feed. Resume the original signed note link.
                            command_id += 1
                            self._send(ws, command_id, 'Page.navigate', navigation)
                        continue
                    if method == "Network.responseReceived":
                        params = message.get("params", {})
                        response = params.get("response", {})
                        request_id = params.get("requestId", "")
                        if request_id and relevant(str(response.get("url", "")), str(response.get("mimeType", ""))):
                            response_ids.append(request_id)
                            watched_ids.add(request_id)
                    elif method == "Network.loadingFinished":
                        request_id = message.get("params", {}).get("requestId", "")
                        if request_id in watched_ids and len(payloads) < 80:
                            command_id += 1
                            pending[command_id] = request_id
                            self._send(ws, command_id, "Network.getResponseBody", {"requestId": request_id})
                    elif message.get("id") in pending:
                        request_id = pending.pop(int(message["id"]))
                        consume(message, request_id)

                if self.platform == 'xiaohongshu' and self.login_waiting:
                    raise XhsLoginRequired('等待小红书登录或验证超时。窗口已保留，请完成一次登录后重新审核剩余链接；本轮不再逐条重复要求登录。')
                for request_id in dict.fromkeys(response_ids):
                    command_id += 1
                    self._send(ws, command_id, "Network.getResponseBody", {"requestId": request_id})
                    end = time.time() + 2
                    while time.time() < end:
                        try:
                            message = json.loads(ws.recv())
                        except Exception:
                            continue
                        if message.get("id") == command_id:
                            consume(message, request_id)
                            break

                command_id += 1
                self._send(ws, command_id, "Runtime.evaluate", {
                    "expression": """(() => ({
                        url: location.href,
                        title: document.title,
                        text: (document.body ? document.body.innerText : '').slice(0, 40000),
                        detailText: (() => {
                            const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                            const root = [
                                '.note-detail-mask .note-container', '.note-detail-mask',
                                '.note-container', '[class*="note-detail"]'
                            ].map(selector => document.querySelector(selector)).find(visible);
                            return root ? (root.innerText || '').slice(0, 20000) : '';
                        })(),
                        detailHtml: (() => {
                            const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                            const root = [
                                '.note-detail-mask .note-container', '.note-detail-mask',
                                '.note-container', '[class*="note-detail"]'
                            ].map(selector => document.querySelector(selector)).find(visible);
                            return root ? (root.outerHTML || '').slice(0, 400000) : '';
                        })(),
                        detailData: (() => {
                            const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                            const root = [
                                '.note-detail-mask .note-container', '.note-detail-mask',
                                '.note-container', '[class*="note-detail"]'
                            ].map(selector => document.querySelector(selector)).find(visible);
                            if (!root) return {};
                            const text = selectors => {
                                for (const selector of selectors) {
                                    const el = root.querySelector(selector);
                                    if (el && (el.innerText || el.textContent || '').trim()) {
                                        return (el.innerText || el.textContent || '').trim();
                                    }
                                }
                                return '';
                            };
                            const numberText = selectors => {
                                for (const selector of selectors) {
                                    for (const el of root.querySelectorAll(selector)) {
                                        const value = (el.innerText || el.textContent || '').trim();
                                        if (/\d|[万w千k]/i.test(value)) return value;
                                    }
                                }
                                return '';
                            };
                            const authorLink = root.querySelector('.author-container a.name, .author a.name, a[href*="/user/profile/"]');
                            return {
                                title: text(['#detail-title', '.note-content .title']),
                                description: text(['#detail-desc', '.note-content .desc']),
                                authorName: text(['.author-container .username', '.author .username']),
                                authorHref: authorLink ? authorLink.getAttribute('href') || '' : '',
                                likes: numberText([
                                    '.engage-bar-container .like-wrapper .count', '.engage-bar .like-wrapper .count',
                                    '.like-wrapper .count', '[class*="like-wrapper"] .count'
                                ]),
                                collects: numberText([
                                    '.engage-bar-container .collect-wrapper .count', '.engage-bar .collect-wrapper .count',
                                    '.collect-wrapper .count', '[class*="collect-wrapper"] .count'
                                ]),
                                comments: numberText([
                                    '.engage-bar-container .chat-wrapper .count', '.engage-bar .chat-wrapper .count',
                                    '.chat-wrapper .count', '.comment-wrapper .count', '[class*="chat-wrapper"] .count'
                                ]),
                                shares: numberText([
                                    '.engage-bar-container .share-wrapper .count', '.engage-bar .share-wrapper .count',
                                    '.share-wrapper .count', '[class*="share-wrapper"] .count'
                                ])
                            };
                        })(),
                        description: document.querySelector('meta[name="description"]')?.content
                            || document.querySelector('meta[property="og:description"]')?.content || '',
                        keywords: document.querySelector('meta[name="keywords"]')?.content || ''
                    }))()""",
                    "returnByValue": True,
                })
                page: Dict[str, Any] = {}
                end = time.time() + 4
                while time.time() < end:
                    try:
                        message = json.loads(ws.recv())
                    except Exception:
                        continue
                    if message.get("id") == command_id:
                        page = message.get("result", {}).get("result", {}).get("value", {}) or {}
                        break
                if self.platform == "xiaohongshu":
                    command_id += 1
                    self._send(ws, command_id, "Runtime.evaluate", {
                        "expression": "JSON.parse(JSON.stringify(window.__INITIAL_STATE__ || {}))",
                        "returnByValue": True,
                    })
                    end = time.time() + 4
                    while time.time() < end:
                        try:
                            message = json.loads(ws.recv())
                        except Exception:
                            continue
                        if message.get("id") == command_id:
                            state = message.get("result", {}).get("result", {}).get("value", {}) or {}
                            if isinstance(state, dict) and state:
                                payloads.append(state)
                            break
                page["payloads"] = payloads
                return page
            finally:
                self.login_waiting = False
                try:
                    ws.close()
                finally:
                    if self.platform != 'xiaohongshu':
                        try:
                            requests.get(f"http://127.0.0.1:{self.port}/json/close/{target_id}", timeout=2)
                        except requests.RequestException:
                            pass

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            # Browser.close lets Chrome/Edge flush the persistent login cookies on Windows too.
            try:
                version = requests.get(f'http://127.0.0.1:{self.port}/json/version', timeout=2).json()
                ws = websocket.create_connection(version['webSocketDebuggerUrl'], timeout=2, origin=f'http://127.0.0.1:{self.port}')
                try:
                    self._send(ws, 1, 'Browser.close')
                finally:
                    ws.close()
                self.process.wait(timeout=5)
            except Exception:
                self.process.terminate()


DOUYIN_COLLECTOR = ChromeCollector("douyin", DOUYIN_CHROME_PROFILE, "https://www.douyin.com/")
XHS_COLLECTOR = ChromeCollector("xiaohongshu", XHS_CHROME_PROFILE, "https://www.xiaohongshu.com/")
PUBLIC_COLLECTOR = ChromeCollector("public", PUBLIC_CHROME_PROFILE, "about:blank")
atexit.register(DOUYIN_COLLECTOR.close)
atexit.register(XHS_COLLECTOR.close)
atexit.register(PUBLIC_COLLECTOR.close)
EXPORT_CACHE: Dict[str, Dict[str, Any]] = {}
SUBMISSION_EXPORT_CACHE: Dict[str, Dict[str, Any]] = {}
PROGRESS_CACHE: Dict[str, Dict[str, Any]] = {}
CONTROL_CACHE: Dict[str, Dict[str, bool]] = {}
EXPORT_LOCK = threading.Lock()
CONTROL_CONDITION = threading.Condition()


def _set_progress(token: str, current: int, total: int, message: str, done: bool = False) -> None:
    if not token:
        return
    safe_total = max(0, int(total))
    safe_current = min(max(0, int(current)), safe_total) if safe_total else 0
    percent = round(safe_current * 100 / safe_total) if safe_total else 0
    with EXPORT_LOCK:
        PROGRESS_CACHE[token] = {
            "current": safe_current, "total": safe_total, "percent": percent,
            "message": message, "done": bool(done), "updated_at": time.time(),
        }
        while len(PROGRESS_CACHE) > 100:
            PROGRESS_CACHE.pop(next(iter(PROGRESS_CACHE)))


def _initialize_control(token: str) -> None:
    if not token:
        return
    with CONTROL_CONDITION:
        CONTROL_CACHE.setdefault(token, {"paused": False, "cancelled": False})
        while len(CONTROL_CACHE) > 100:
            CONTROL_CACHE.pop(next(iter(CONTROL_CACHE)))


def _update_control(token: str, action: str) -> Dict[str, bool]:
    if not token:
        raise CheckError("缺少任务标识，请重新开始读取")
    if action not in ("pause", "resume", "cancel"):
        raise CheckError("不支持的任务控制操作")
    with CONTROL_CONDITION:
        state = CONTROL_CACHE.setdefault(token, {"paused": False, "cancelled": False})
        if action == "pause" and not state["cancelled"]:
            state["paused"] = True
        elif action == "resume" and not state["cancelled"]:
            state["paused"] = False
        elif action == "cancel":
            state["cancelled"] = True
            state["paused"] = False
        CONTROL_CONDITION.notify_all()
        return dict(state)


def _progress_with_control(
    token: str, current: int, total: int, message: str, done: bool = False,
) -> bool:
    """更新进度；暂停时阻塞到继续，取消时返回 True。"""
    _set_progress(token, current, total, message, done)
    if not token or done:
        return False
    with CONTROL_CONDITION:
        state = CONTROL_CACHE.get(token, {"paused": False, "cancelled": False})
        while state["paused"] and not state["cancelled"]:
            _set_progress(token, current, total, f"已暂停 · {current}/{total} 已完成", False)
            CONTROL_CONDITION.wait(timeout=0.5)
            state = CONTROL_CACHE.get(token, state)
        return bool(state["cancelled"])


def fetch_account(account_url: str, cookie_text: str = "", days: int = 7) -> Dict[str, Any]:
    account_url = validate_account_url(account_url)

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    if cookie_text.strip():
        session.cookies.update(_cookie_dict(cookie_text))

    try:
        response = session.get(account_url.strip(), timeout=20, allow_redirects=True)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise CheckError(f"主页访问失败：{exc}") from exc

    final_url, page = response.url, response.text
    sec_uid = _extract_sec_uid(final_url, page)
    raw_awemes: List[Dict[str, Any]] = []
    for state in _decode_embedded_json(page):
        raw_awemes.extend(_walk_for_awemes(state))

    api_warning = ""
    if sec_uid:
        api_url = "https://www.douyin.com/aweme/v1/web/aweme/post/"
        params = {
            "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
            "sec_user_id": sec_uid, "max_cursor": "0", "count": "35",
            "publish_video_strategy_type": "2",
        }
        try:
            api_response = session.get(api_url, params=params, headers={"Referer": final_url}, timeout=20)
            if api_response.ok and "application/json" in api_response.headers.get("content-type", ""):
                payload = api_response.json()
                raw_awemes.extend(payload.get("aweme_list") or [])
            elif not raw_awemes:
                api_warning = f"作品接口返回 HTTP {api_response.status_code}，可能需要有效 Cookie"
        except (requests.RequestException, ValueError) as exc:
            api_warning = f"作品接口暂时不可用：{exc}"

    warnings: List[str] = []
    if api_warning:
        warnings.append(api_warning)
    return build_result(final_url, raw_awemes, days=days, warnings=warnings)


def fetch_accounts(
    account_urls: Iterable[str], cookie_text: str = "", days: int = 7,
    progress_callback: Optional[Callable[[int, int, str, bool], bool]] = None,
) -> Dict[str, Any]:
    urls = list(dict.fromkeys(url.strip() for url in account_urls if url.strip()))
    if not urls:
        raise CheckError("请至少输入一个抖音或小红书主页链接")
    if len(urls) > 50:
        raise CheckError("单次最多检测 50 个主页链接")
    platforms: Dict[str, str] = {}
    for url in urls:
        try:
            platforms[url] = detect_platform(url)
        except CheckError:
            # 让循环产生与输入顺序一致的错误行。
            platforms[url] = "unsupported"
    if "douyin" in platforms.values() and not cookie_text.strip() and not DOUYIN_COLLECTOR.is_logged_in():
        raise CheckError("当前列表包含抖音链接，请先完成抖音登录并验证。小红书登录为可选项。")
    accounts: List[Dict[str, Any]] = []
    total_urls = len(urls)
    cancelled = bool(progress_callback and progress_callback(0, total_urls, f"准备读取 {total_urls} 个账号", False))
    for index, url in enumerate(urls):
        if cancelled:
            break
        if progress_callback and progress_callback(index, total_urls, f"正在读取第 {index + 1}/{total_urls} 个账号", False):
            cancelled = True
            break
        try:
            platform = detect_platform(url)
            collector = XHS_COLLECTOR if platform == "xiaohongshu" else DOUYIN_COLLECTOR
            platform_cookie = cookie_text if platform == "douyin" else ""
            try:
                result = collector.collect(url, platform_cookie)
            except Exception as first_exc:
                if "socket is already closed" not in str(first_exc).lower():
                    raise
                time.sleep(1)
                result = collector.collect(url, platform_cookie)
            result["days"] = days
            accounts.append(result)
        except CheckError as exc:
            accounts.append({
                "account_name": "—", "account_url": url, "has_updates": False,
                "recent_count": 0, "over_1000_count": 0, "works": [],
                "warnings": [str(exc)], "data_status": "error", "platform": "未知",
            })
        except Exception as exc:
            accounts.append({
                "account_name": "—", "account_url": url, "has_updates": False,
                "recent_count": 0, "over_1000_count": 0, "works": [],
                "warnings": [f"检测失败：{exc}"], "data_status": "error", "platform": "未知",
            })
        if progress_callback and progress_callback(index + 1, total_urls, f"已读取 {index + 1}/{total_urls} 个账号", False):
            cancelled = True
            break
    result = {
        "accounts": accounts,
        "total": len(accounts),
        "updated_accounts": sum(item["data_status"] == "ok" and item["has_updates"] for item in accounts),
        "unavailable_accounts": sum(item["data_status"] != "ok" for item in accounts),
        "requested_total": total_urls, "cancelled": cancelled,
        "checked_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "app_version": APP_VERSION,
    }
    token = uuid.uuid4().hex[:16]
    result["export_token"] = token
    with EXPORT_LOCK:
        EXPORT_CACHE[token] = result
        while len(EXPORT_CACHE) > 30:
            EXPORT_CACHE.pop(next(iter(EXPORT_CACHE)))
    if progress_callback:
        final_message = f"已取消 · 保留 {len(accounts)}/{total_urls} 个账号结果" if cancelled else "账号检测完成"
        progress_callback(len(accounts), total_urls, final_message, True)
    return result


DEFAULT_DEVICE_TAGS = ["大疆Action4", "大疆Pocket4", "大疆Osmo360", "大疆Avata360", "大疆nano"]


def detect_submission_platform(url: str) -> str:
    text = url.strip()
    if not re.match(r"^https?://", text):
        raise CheckError("作品链接需以 http:// 或 https:// 开头")
    host = (urllib.parse.urlparse(text).hostname or "").lower()
    if host.endswith(("douyin.com", "iesdouyin.com")):
        return "douyin"
    if host.endswith(("xiaohongshu.com", "xhslink.com", "xhslink.cn")):
        return "xiaohongshu"
    if host.endswith(("bilibili.com", "b23.tv")):
        return "bilibili"
    if host.endswith(("weixin.qq.com", "channels.weixin.qq.com")):
        return "weixin"
    raise CheckError("仅支持抖音、小红书、B站或微信视频号作品链接")


def _clean_submission_url(value: str) -> str:
    """从 Excel/聊天软件粘贴的一整行文本中取出第一个作品链接。"""
    text = html.unescape(str(value or "").strip()).replace("\\_", "_").replace("\\&", "&")
    match = re.search(r"https?://[^\s<>\]\[\)）]+", text, flags=re.I)
    if not match:
        return text
    return match.group(0).rstrip(".,;:!?，。；：！？")


def _douyin_content_id(url: str) -> str:
    text = html.unescape(urllib.parse.unquote(str(url or "")))
    for pattern in (
        r"/(?:video|note)/(\d+)",
        r"[?&](?:modal_id|aweme_id|item_id)=(\d+)",
        r'["\'](?:aweme_id|awemeId|itemId)["\']\s*[:=]\s*["\']?(\d+)',
    ):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def _xhs_content_id(url: str) -> str:
    match = re.search(r"/(?:discovery/item|explore)/([0-9a-fA-F]{24})", str(url or ""))
    return match.group(1).lower() if match else ""


def _author_info(data: Dict[str, Any]) -> tuple[str, str]:
    author = _first(
        data, "author", "user", "owner", "noteCard.user", "note_card.user",
        "finderUser", "contact", "userInfo", default={},
    ) or {}
    if not isinstance(author, dict):
        author = {}
    name = str(_first(author, "nickname", "nickName", "name", "userName", "username", "displayName", default="") or "").strip()
    author_id = str(_first(
        author, "unique_id", "uniqueId", "redId", "red_id", "userId", "user_id",
        "short_id", "shortId", "sec_uid", "secUid", "uid", "mid", "id", default="",
    ) or "").strip()
    return name, author_id


def _xhs_note_candidate(value: Any, note_id: str) -> Optional[Dict[str, Any]]:
    """只选取目标笔记，避免从推荐流中误取互动数和博主。"""
    target = str(note_id or "").lower()
    if not target:
        return None
    best: Optional[Dict[str, Any]] = None
    best_score = -1
    for item in _walk_for_xhs_notes(value):
        ids = {
            str(_first(item, path, default="") or "").lower()
            for path in ("id", "noteId", "note_id", "noteCard.noteId", "noteCard.id", "note_card.note_id", "note_card.id")
        }
        if target not in ids:
            continue
        score = sum(_first(item, path, default=None) is not None for path in (
            "desc", "title", "displayTitle", "interactInfo", "noteCard.interactInfo", "note_card.interact_info",
        ))
        if score > best_score:
            best, best_score = item, score
    return best


def _submission_tags(text: str, extra: Optional[Iterable[str]] = None) -> List[str]:
    tags: List[str] = []
    for item in extra or []:
        clean = str(item or "").strip().lstrip("#").replace("[话题]", "")
        if clean:
            tags.append(clean)
    for match in re.findall(r"#\s*([^#\s，。！？、,.;；:：!！?？\[]+)(?:\[话题\])?", text or ""):
        clean = match.strip().replace("[话题]", "")
        if clean:
            tags.append(clean)
    return list(dict.fromkeys(tags))


def _normal_tag(tag: str) -> str:
    return re.sub(r"[\s#_－-]+", "", str(tag or "")).lower()


def _clean_tags(values: Union[str, Iterable[str]]) -> List[str]:
    raw_values = values.splitlines() if isinstance(values, str) else values
    return list(dict.fromkeys(str(value).strip().lstrip("#") for value in raw_values if str(value).strip().lstrip("#")))


def _looks_like_dji_device_tag(tag: str, campaign_tags: Union[str, Iterable[str]]) -> bool:
    """识别不在示例清单中的 DJI 型号话题，如 #大疆pocket4p、#大疆御4pro。"""
    normalized = _normal_tag(tag)
    campaign_normals = {_normal_tag(item) for item in _clean_tags(campaign_tags)}
    if not normalized.startswith("大疆") or normalized in campaign_normals:
        return False
    suffix = normalized[2:]
    if not suffix or any(word in suffix for word in ("校园", "计划", "活动", "赛事", "挑战赛")):
        return False
    return bool(re.search(r"[a-z0-9]", suffix)) or suffix.startswith(("御", "悟", "精灵", "如影", "灵眸", "机甲大师"))


def _meta_content(page: str, key: str) -> str:
    patterns = [
        rf'<meta[^>]+name=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']*)',
        rf'<meta[^>]+property=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']*)',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:name|property)=["\']{re.escape(key)}["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, page, flags=re.I | re.S)
        if match:
            return html.unescape(match.group(1)).strip()
    return ""


def _json_text(value: str) -> str:
    try:
        # value 来自 JSON 字符串正文，保留其中原有的转义后再交给 JSON 解码。
        return str(json.loads(f'"{value}"'))
    except (json.JSONDecodeError, ValueError):
        return value.replace("\\u002F", "/").replace("\\n", "\n")


def _walk_dicts(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _metric(data: Dict[str, Any], *names: str) -> int:
    for name in names:
        value = _first(data, name, default=None)
        if value not in (None, ""):
            return parse_like_count(value)
    return 0


def _empty_metrics() -> Dict[str, int]:
    return {"likes": 0, "comments": 0, "collects": 0, "shares": 0, "coins": 0, "hearts": 0}


def _parse_xhs_submission(page: str, resolved_url: str) -> Dict[str, Any]:
    note_match = re.search(r"/(?:discovery/item|explore)/([0-9a-fA-F]{24})", resolved_url)
    note_id = note_match.group(1) if note_match else ""
    anchor = page.find(f'"noteId":"{note_id}"') if note_id else -1
    # 小红书当前 SSR 数据把 interactInfo/title 放在 noteId 前面；前后都保留，
    # 同时限制窗口，避免误取推荐列表里其他笔记的互动数。
    segment_start = max(0, anchor - 24000) if anchor >= 0 else 0
    segment = page[segment_start:anchor + 12000] if anchor >= 0 else page
    relative_anchor = anchor - segment_start if anchor >= 0 else 0

    def field(name: str) -> str:
        matches = list(re.finditer(rf'"{re.escape(name)}":"((?:\\.|[^"\\])*)"', segment))
        match = min(matches, key=lambda item: abs(item.start() - relative_anchor)) if matches else None
        return _json_text(match.group(1)) if match else ""

    title = field("title") or re.sub(r"\s*-\s*小红书\s*$", "", _meta_content(page, "og:title"))
    description = field("desc") or _meta_content(page, "description")
    author_name = field("nickname") or field("nickName")
    author_id = field("redId") or field("userId") or field("user_id")
    metrics = _empty_metrics()
    aliases = {
        "likes": "likedCount", "comments": "commentCount",
        "collects": "collectedCount", "shares": "shareCount",
    }
    found = False
    if anchor >= 0:
        for target, source in aliases.items():
            matches = list(re.finditer(rf'"{source}":"?([0-9.,万w千k]*)"?', segment, flags=re.I))
            match = min(matches, key=lambda item: abs(item.start() - relative_anchor)) if matches else None
            if match:
                metrics[target] = parse_like_count(match.group(1))
                found = True
    generic_page = title.strip() in ("小红书", "登录小红书", "安全限制") or any(marker in f"{title} {description}" for marker in (
        "安全限制", "登录小红书", "IP存在风险", "验证码",
        "你的生活指南",
    ))
    content_found = bool(anchor >= 0 or (title and description and not generic_page))
    return {
        "title": title, "description": description, "tags": _submission_tags(description),
        "metrics": metrics, "metrics_available": found,
        "author_name": author_name, "author_id": author_id,
        "content_status": "available" if content_found else "unavailable",
    }


def _parse_bilibili_submission(page: str) -> Dict[str, Any]:
    state: Dict[str, Any] = {}
    match = re.search(r"window\.__INITIAL_STATE__=(\{.*?\});\(function", page, flags=re.S)
    if match:
        try:
            state = json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError):
            state = {}
    video = state.get("videoData") if isinstance(state.get("videoData"), dict) else {}
    stats = video.get("stat") if isinstance(video.get("stat"), dict) else {}
    tag_items = state.get("tags") if isinstance(state.get("tags"), list) else []
    extra_tags = [str(item.get("tag_name", "")) for item in tag_items if isinstance(item, dict)]
    title = str(video.get("title") or "")
    description = str(video.get("desc") or "")
    if not title:
        title_match = re.search(r"<title>(.*?)</title>", page, flags=re.I | re.S)
        title = html.unescape(title_match.group(1)).replace("_哔哩哔哩bilibili", "") if title_match else ""
    metrics = _empty_metrics()
    metrics.update({
        "likes": parse_like_count(stats.get("like")),
        "comments": parse_like_count(stats.get("reply")),
        "collects": parse_like_count(stats.get("favorite")),
        "shares": parse_like_count(stats.get("share")),
        "coins": parse_like_count(stats.get("coin")),
    })
    author_name, author_id = _author_info({"author": video.get("owner") or state.get("upData") or {}})
    return {
        "content_id": str(video.get('bvid') or ''),
        "title": title, "description": description,
        "tags": _submission_tags(description, extra_tags),
        "metrics": metrics, "metrics_available": bool(stats),
        "author_name": author_name, "author_id": author_id,
    }


def _parse_douyin_submission(page: str, resolved_url: str) -> Dict[str, Any]:
    """尽量从无需执行脚本的抖音作品页恢复文案、Tag 和公开互动数。"""
    target_id = _douyin_content_id(resolved_url)
    candidates: List[Dict[str, Any]] = []
    for state in _decode_embedded_json(page):
        candidates.extend(_walk_for_awemes(state))
    raw = next(
        (item for item in candidates if str(_first(item, "aweme_id", "id", default="")) == target_id),
        candidates[0] if candidates else None,
    )
    metrics = _empty_metrics()
    description = ""
    tags: List[str] = []
    available = False
    author_name = ""
    author_id = ""
    if raw:
        author_name, author_id = _author_info(raw)
        description = str(_first(raw, "desc", "description", default="") or "")
        stats = _first(raw, "statistics", "stats", default={}) or {}
        metrics.update({
            "likes": _metric(stats, "digg_count", "diggCount"),
            "comments": _metric(stats, "comment_count", "commentCount"),
            "collects": _metric(stats, "collect_count", "collectCount"),
            "shares": _metric(stats, "share_count", "shareCount"),
        })
        available = any(_first(stats, key, default=None) is not None for key in (
            "digg_count", "diggCount", "comment_count", "commentCount",
            "collect_count", "collectCount", "share_count", "shareCount",
        ))
        tags = extract_tags(description, raw.get("text_extra") or raw.get("textExtra"))
    if not description:
        description = _meta_content(page, "description") or _meta_content(page, "og:description")
        description = re.split(r"\s+-\s+.*?于\d{8}发布在抖音", description)[0].strip()
        tags = _submission_tags(description)
    title = _meta_content(page, "og:title") or description[:50]
    return {
        "title": title, "description": description, "tags": tags,
        "metrics": metrics, "metrics_available": available,
        "author_name": author_name, "author_id": author_id,
    }


def _dynamic_submission_data(
    platform: str, page: Dict[str, Any], expected_content_id: str = "",
) -> Dict[str, Any]:
    title = str(page.get("title", "") or "")
    description = str(page.get("description", "") or "")
    text = str(page.get("text", "") or "")
    payloads = page.get("payloads") or []
    metrics = _empty_metrics()
    metrics_available = False
    extra_tags: List[str] = []
    author_name = ""
    author_id = ""
    content_status = ""
    content_id = expected_content_id

    if platform == "douyin":
        page_content_id = _douyin_content_id(str(page.get("url", "")))
        target_id = expected_content_id or page_content_id
        candidates: List[Dict[str, Any]] = []
        for payload in payloads:
            candidates.extend(_walk_for_awemes(payload))
        raw = next((item for item in candidates if str(_first(item, "aweme_id", "id", default="")) == target_id), None)
        if not raw and candidates and not target_id:
            raw = candidates[0]
        if raw:
            content_id = str(_first(raw, 'aweme_id', 'id', default=content_id) or content_id)
            author_name, author_id = _author_info(raw)
            description = str(_first(raw, "desc", "description", default=description) or description)
            stats = _first(raw, "statistics", "stats", default={}) or {}
            metrics.update({
                "likes": _metric(stats, "digg_count", "diggCount"),
                "comments": _metric(stats, "comment_count", "commentCount"),
                "collects": _metric(stats, "collect_count", "collectCount"),
                "shares": _metric(stats, "share_count", "shareCount"),
            })
            metrics_available = True
            extra_tags = extract_tags(description, raw.get("text_extra") or raw.get("textExtra"))
        if not description:
            description = re.split(r"\s+-\s+.*?于\d{8}发布在抖音", str(page.get("description", "")))[0].strip()
        if not metrics_available and (not expected_content_id or page_content_id == expected_content_id):
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            tag_hint = next(iter(_submission_tags(description)), "")
            start = next((i for i, line in enumerate(lines) if (tag_hint and tag_hint in line) or (description[:12] and description[:12] in line)), -1)
            if start >= 0:
                values: List[int] = []
                for line in lines[start + 1:start + 14]:
                    if line in ("举报",) or line.startswith("发布时间"):
                        break
                    if re.fullmatch(r"[0-9,.]+(?:\.[0-9]+)?[万w千k]?", line, flags=re.I):
                        values.append(parse_like_count(line))
                if len(values) >= 4:
                    metrics.update(dict(zip(("likes", "comments", "collects", "shares"), values[:4])))
                    metrics_available = True
    elif platform == "weixin":
        aliases = {
            "likes": ("likeCountFmt", "likeCount", "like_count", "likeNum", "like_num", "agreeCount"),
            "comments": ("commentCountFmt", "commentCount", "comment_count", "commentNum", "comment_num"),
            "hearts": ("favCountFmt", "favCount", "friendLikeCount", "friend_like_count", "recommendCount", "heartCount"),
        }
        best: Optional[Dict[str, Any]] = None
        best_score = 0
        for payload in payloads:
            for item in _walk_dicts(payload):
                score = sum(any(_first(item, name, default=None) is not None for name in names) for names in aliases.values())
                if score > best_score:
                    best, best_score = item, score
        if best:
            content_id = str(_first(best, 'objectId', 'object_id', 'id', default='') or '')
            for target, names in aliases.items():
                metrics[target] = _metric(best, *names)
            description = str(_first(best, "description", "desc", "content", "objectDesc.description", default=description) or description)
            title = str(_first(best, "title", "objectDesc.title", default=title) or title)
            metrics_available = best_score >= 2
            author_name, author_id = _author_info(best)
        for target, label in (("likes", "点赞"), ("comments", "评论"), ("hearts", "爱心")):
            match = re.search(rf"(?:{label}\s*([0-9.万w千k]+)|([0-9.万w千k]+)\s*{label})", text, flags=re.I)
            if match:
                metrics[target] = parse_like_count(match.group(1) or match.group(2))
                metrics_available = True

    elif platform == "xiaohongshu":
        # 公开 HTML 被风控或内容延迟加载时，从登录 Chrome 捕获的 JSON 中补取。
        aliases = {
            "likes": ("likedCount", "liked_count", "likeCount", "interactInfo.likedCount", "interact_info.liked_count"),
            "comments": ("commentCount", "comment_count", "interactInfo.commentCount", "interact_info.comment_count"),
            "collects": ("collectedCount", "collected_count", "collectCount", "interactInfo.collectedCount", "interact_info.collected_count"),
            "shares": ("shareCount", "share_count", "interactInfo.shareCount", "interact_info.share_count"),
        }
        best: Optional[Dict[str, Any]] = None
        best_score = 0
        for payload in payloads:
            target_note = _xhs_note_candidate(payload, expected_content_id)
            if target_note:
                best = target_note
                best_score = sum(
                    any(_first(target_note, name, f"noteCard.{name}", f"note_card.{name}", default=None) is not None for name in names)
                    for names in aliases.values()
                )
                break
            if expected_content_id:
                continue
            for item in _walk_dicts(payload):
                score = sum(any(_first(item, name, default=None) is not None for name in names) for names in aliases.values())
                if score > best_score:
                    best, best_score = item, score
        if best:
            card = _first(best, "noteCard", "note_card", default=best) or best
            content_id = str(_first(card, 'noteId', 'note_id', 'id', default=content_id) or content_id)
            for target, names in aliases.items():
                metrics[target] = _metric(card, *names)
            description = str(_first(card, "desc", "description", default=description) or description)
            title = str(_first(card, "title", "displayTitle", default=title) or title)
            author_name, author_id = _author_info(best)
            metrics_available = best_score >= 2 or any(
                _first(card, name, default=None) is not None
                for names in aliases.values() for name in names
            )
        detail = page.get("detailData") or {}
        if isinstance(detail, dict) and (detail.get("title") or detail.get("description")):
            title = str(detail.get("title") or title)
            description = str(detail.get("description") or description)
            author_name = str(detail.get("authorName") or author_name)
            author_match = re.search(r"/user/profile/([^/?#]+)", str(detail.get("authorHref") or ""))
            if author_match:
                author_id = author_match.group(1)
            dom_metric_fields = {
                "likes": detail.get("likes"), "comments": detail.get("comments"),
                "collects": detail.get("collects"), "shares": detail.get("shares"),
            }
            dom_metrics_found = sum(value not in (None, "") for value in dom_metric_fields.values())
            for target, value in dom_metric_fields.items():
                if value not in (None, ""):
                    metrics[target] = parse_like_count(value)
            metrics_available = metrics_available or dom_metrics_found >= 2
            content_status = "available"
        else:
            content_status = "available" if best else "unavailable"

    elif platform == "bilibili":
        page_bvid = re.search(r'/video/(BV[A-Za-z0-9]{10})', str(page.get('url', '')))
        content_id = page_bvid.group(1) if page_bvid else content_id
        aliases = {
            "likes": ("like",), "comments": ("reply", "comment"),
            "collects": ("favorite", "collect"), "shares": ("share",), "coins": ("coin",),
        }
        best: Optional[Dict[str, Any]] = None
        best_score = 0
        for payload in payloads:
            for item in _walk_dicts(payload):
                if not content_id and item.get('bvid') and item.get('stat') and item.get('owner'):
                    content_id = str(item['bvid'])
                score = sum(any(_first(item, name, default=None) is not None for name in names) for names in aliases.values())
                if score > best_score:
                    best, best_score = item, score
        if best:
            for target, names in aliases.items():
                metrics[target] = _metric(best, *names)
            description = str(_first(best, "desc", "description", default=description) or description)
            title = str(_first(best, "title", default=title) or title)
            metrics_available = best_score >= 3
            author_name, author_id = _author_info(best)

    return {
        "content_id": content_id,
        "title": title, "description": description,
        "tags": _submission_tags(description, extra_tags),
        "metrics": metrics, "metrics_available": metrics_available,
        "author_name": author_name, "author_id": author_id,
        "content_status": content_status or ("available" if description or metrics_available else "unavailable"),
    }


def _submission_result(
    original_url: str, resolved_url: str, platform: str, data: Dict[str, Any],
    campaign_tags: Union[str, Iterable[str]], device_tags: List[str], warnings: Optional[List[str]] = None,
) -> Dict[str, Any]:
    platform_names = {"douyin": "抖音", "xiaohongshu": "小红书", "bilibili": "B站", "weixin": "视频号"}
    tags = list(dict.fromkeys(data.get("tags") or []))
    normalized = {_normal_tag(tag): tag for tag in tags}
    campaigns = _clean_tags(campaign_tags)
    matched_campaign = next((normalized[_normal_tag(tag)] for tag in campaigns if _normal_tag(tag) in normalized), "")
    campaign_ok = bool(matched_campaign)
    configured_device = next((tag for tag in device_tags if _normal_tag(tag) in normalized), "")
    detected_device = next((tag for tag in tags if _looks_like_dji_device_tag(tag, campaigns)), "")
    matched_device = normalized.get(_normal_tag(configured_device), configured_device) if configured_device else detected_device
    metrics = _empty_metrics()
    metrics.update(data.get("metrics") or {})
    interaction = sum(metrics.values())
    available = bool(data.get("metrics_available"))
    tag_data_available = bool(data.get("description") or tags)
    result_warnings = list(warnings or [])
    if not campaigns:
        result_warnings.append("请至少输入一个本次活动 Tag")
    if not available:
        result_warnings.append("未能完整读取互动数，请打开作品链接人工复核。")
    if platform == "weixin":
        formula = "点赞 + 评论 + 爱心"
    elif platform == "bilibili":
        formula = "点赞 + 投币 + 评论 + 收藏 + 分享"
    else:
        formula = "点赞 + 评论 + 收藏 + 分享"
    tag_ok = bool(campaign_ok and matched_device)
    over_200 = interaction > 200 if available else None
    over_10000 = interaction > 10000 if available else None
    content_status = str(data.get("content_status") or ("available" if tag_data_available or available else "unavailable"))
    if content_status == "deleted":
        over_200 = over_10000 = None
        submission_status = "已删除"
    elif not tag_data_available:
        submission_status = "待复核"
    elif not tag_ok:
        submission_status = "不满足投稿条件"
    elif over_200:
        submission_status = "优质作品"
    else:
        submission_status = "满足投稿条件"
    return {
        "platform": platform_names[platform], "platform_code": platform,
        "content_id": str(data.get('content_id') or ''),
        "original_url": original_url, "resolved_url": resolved_url,
        "author_name": data.get("author_name") or "", "author_id": data.get("author_id") or "",
        "content_status": content_status,
        "title": data.get("title") or "—", "description": data.get("description") or "",
        "tags": tags, "campaign_tags": campaigns, "campaign_tag": campaigns[0] if campaigns else "",
        "matched_campaign_tag": matched_campaign, "campaign_tag_ok": campaign_ok,
        "matched_device_tag": matched_device, "device_tag_ok": bool(matched_device),
        "tag_ok": tag_ok, "tag_data_available": tag_data_available,
        "metrics": metrics, "interaction_count": interaction,
        "interaction_formula": formula, "interaction_available": available,
        "over_200": over_200, "over_10000": over_10000, "submission_status": submission_status,
        "watermark_status": "pending", "warnings": result_warnings,
        "data_status": "ok" if available and (data.get("description") or tags) else "partial" if (data.get("description") or available) else "unavailable",
        "app_version": APP_VERSION,
    }


def fetch_submission(url: str, campaign_tags: Union[str, Iterable[str]], device_tags: List[str]) -> Dict[str, Any]:
    original_url = _clean_submission_url(url)
    platform = detect_submission_platform(original_url)
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    resolved_url = original_url
    page_html = ""
    warnings: List[str] = []
    redirect_sources: List[str] = [original_url]
    try:
        # XHS must use its persistent signed-in browser, not a fresh anonymous HTTP session per note.
        response = session.get(original_url, timeout=25, allow_redirects=True) if platform != 'xiaohongshu' else None
        if response is None:
            pass
        else:
            resolved_url = response.url
            redirect_sources.extend(item.url for item in response.history)
            redirect_sources.extend(str(item.headers.get("Location") or "") for item in response.history)
            redirect_sources.append(resolved_url)
            if response.ok:
                page_html = response.text
            else:
                warnings.append(f"公开页面返回 HTTP {response.status_code}")
    except requests.RequestException as exc:
        warnings.append(f"短链解析失败：{exc}")
    expected_douyin_id = ""
    if platform == "douyin":
        redirect_sources.append(page_html)
        expected_douyin_id = next((_douyin_content_id(item) for item in redirect_sources if _douyin_content_id(item)), "")

    if platform == "xiaohongshu":
        expected_xhs_id = _xhs_content_id(original_url) or _xhs_content_id(resolved_url)
        data = _parse_xhs_submission(page_html, original_url) if page_html else {
            "title": "", "description": "", "tags": [], "metrics": _empty_metrics(),
            "metrics_available": False, "author_name": "", "author_id": "",
        }
        if not data.get("metrics_available") or not data.get("author_name") or not data.get("author_id"):
            try:
                dynamic_page = XHS_COLLECTOR.collect_submission_page(original_url)
                resolved_url = str(dynamic_page.get("url") or resolved_url)
                expected_xhs_id = expected_xhs_id or _xhs_content_id(resolved_url)
                dynamic = _dynamic_submission_data(platform, dynamic_page, expected_xhs_id)
                if (
                    not dynamic.get('metrics_available') and not dynamic.get('description')
                ) and expected_xhs_id:
                    signed_query = urllib.parse.urlparse(original_url).query or urllib.parse.urlparse(resolved_url).query
                    canonical_url = f"https://www.xiaohongshu.com/explore/{expected_xhs_id}"
                    if signed_query:
                        canonical_url += '?' + signed_query
                    fallback_page = XHS_COLLECTOR.collect_submission_page(canonical_url, wait_seconds=10)
                    fallback = _dynamic_submission_data(platform, fallback_page, expected_xhs_id)
                    if (
                        fallback.get("metrics_available") or fallback.get("description")
                        or fallback.get("author_name") or fallback.get("author_id")
                    ):
                        dynamic = fallback
                        resolved_url = str(fallback_page.get("url") or resolved_url)
                for key in ("title", "description", "tags", "metrics", "author_name", "author_id", "content_id"):
                    if dynamic.get(key):
                        data[key] = dynamic[key]
                data["metrics_available"] = bool(dynamic.get("metrics_available") or data.get("metrics_available"))
                if dynamic.get("content_status") == "available":
                    data["content_status"] = "available"
            except XhsLoginRequired:
                raise
            except Exception as exc:
                warnings.append(f"小红书动态读取失败：{exc}")
    elif platform == "bilibili" and page_html:
        data = _parse_bilibili_submission(page_html)
        if not data.get("metrics_available"):
            try:
                dynamic = _dynamic_submission_data(platform, PUBLIC_COLLECTOR.collect_submission_page(original_url))
                data = dynamic if dynamic.get("metrics_available") else data
            except Exception as exc:
                warnings.append(f"B站动态读取失败：{exc}")
    else:
        collector = DOUYIN_COLLECTOR if platform == "douyin" else PUBLIC_COLLECTOR
        static_data = _parse_douyin_submission(page_html, resolved_url) if platform == "douyin" and page_html else None
        try:
            dynamic_page = collector.collect_submission_page(original_url)
            dynamic_url = str(dynamic_page.get("url") or resolved_url)
            dynamic_douyin_id = _douyin_content_id(dynamic_url) if platform == "douyin" else ""
            redirected_to_other_work = bool(
                platform == "douyin" and expected_douyin_id and dynamic_douyin_id
                and dynamic_douyin_id != expected_douyin_id
            )
            if redirected_to_other_work:
                data = {
                    "title": "原作品已删除或不可访问", "description": "", "tags": [],
                    "metrics": _empty_metrics(), "metrics_available": False, "content_status": "deleted",
                    "author_name": (static_data or {}).get("author_name", ""),
                    "author_id": (static_data or {}).get("author_id", ""),
                }
                warnings.append(
                    f"原抖音作品 {expected_douyin_id} 已删除或不可访问；页面自动跳转到了其他作品，程序已阻止读取跳转后的数据。"
                )
            else:
                resolved_url = dynamic_url
                data = _dynamic_submission_data(platform, dynamic_page, expected_douyin_id)
            page_message = " ".join((
                str(dynamic_page.get("title", "")), str(dynamic_page.get("text", ""))[:2000],
            ))
            if platform == "douyin" and "验证码" in page_message:
                warnings.append("抖音触发验证码，请在抖音登录窗口完成验证后重试。")
            original_deleted = bool(platform == "douyin" and any(
                marker in page_message for marker in ("作品不存在", "内容不见了", "视频不见了", "已删除")
            ))
            if original_deleted:
                previous_data = data
                data = {
                    "title": "原作品已删除或不可访问", "description": "", "tags": [],
                    "metrics": _empty_metrics(), "metrics_available": False, "content_status": "deleted",
                    "author_name": previous_data.get("author_name", ""),
                    "author_id": previous_data.get("author_id", ""),
                }
                warnings.append("原抖音作品已删除或不可访问，程序未采用页面推荐的其他作品数据。")
            if static_data and not redirected_to_other_work and not original_deleted:
                if not data.get("description"):
                    data["description"] = static_data.get("description", "")
                    data["tags"] = static_data.get("tags", [])
                    data["title"] = static_data.get("title") or data.get("title")
                if not data.get("metrics_available") and static_data.get("metrics_available"):
                    data["metrics"] = static_data["metrics"]
                    data["metrics_available"] = True
        except Exception as exc:
            data = static_data or {
                "title": "", "description": "", "tags": [],
                "metrics": _empty_metrics(), "metrics_available": False,
            }
            warnings.append(f"动态页面读取失败：{exc}")
        if platform == "weixin" and not data.get("metrics_available"):
            warnings.append("视频号分享页未公开完整互动数，请打开原作品人工核对点赞、评论和爱心。")
    return _submission_result(original_url, resolved_url, platform, data, campaign_tags, device_tags, warnings)


def fetch_submissions(
    urls: Iterable[str], campaign_tags: Union[str, Iterable[str]], device_tags: Iterable[str],
    progress_callback: Optional[Callable[[int, int, str, bool], bool]] = None,
) -> Dict[str, Any]:
    submission_urls = [_clean_submission_url(str(url)) for url in urls if str(url).strip()]
    if not submission_urls:
        raise CheckError("请至少输入一个投稿作品链接")
    campaigns = _clean_tags(campaign_tags)
    if not campaigns:
        raise CheckError("请至少输入一个本次活动 Tag")
    devices = list(dict.fromkeys(str(tag).strip().lstrip("#") for tag in device_tags if str(tag).strip()))
    if not devices:
        devices = list(DEFAULT_DEVICE_TAGS)
    results: List[Dict[str, Any]] = []
    login_required = ''
    total_urls = len(submission_urls)
    cancelled = bool(progress_callback and progress_callback(0, total_urls, f"准备读取 {total_urls} 个投稿", False))
    for index, url in enumerate(submission_urls):
        if cancelled:
            break
        if progress_callback and progress_callback(index, total_urls, f"正在读取第 {index + 1}/{total_urls} 个投稿", False):
            cancelled = True
            break
        try:
            results.append(fetch_submission(url, campaigns, devices))
        except XhsLoginRequired as exc:
            login_required = str(exc)
            break
        except CheckError as exc:
            results.append({
                "platform": "未知", "original_url": url, "resolved_url": url,
                "title": "—", "description": "", "tags": [], "tag_ok": False,
                "campaign_tag_ok": False, "device_tag_ok": False, "matched_device_tag": "",
                "metrics": _empty_metrics(), "interaction_count": 0, "interaction_available": False,
                "over_200": None, "over_10000": None, "watermark_status": "pending", "warnings": [str(exc)],
                "tag_data_available": False, "submission_status": "待复核",
                "data_status": "unavailable", "app_version": APP_VERSION,
            })
        except Exception as exc:
            results.append({
                "platform": "未知", "original_url": url, "resolved_url": url,
                "title": "—", "description": "", "tags": [], "tag_ok": False,
                "campaign_tag_ok": False, "device_tag_ok": False, "matched_device_tag": "",
                "metrics": _empty_metrics(), "interaction_count": 0, "interaction_available": False,
                "over_200": None, "over_10000": None, "watermark_status": "pending", "warnings": [f"审核失败：{exc}"],
                "tag_data_available": False, "submission_status": "待复核",
                "data_status": "unavailable", "app_version": APP_VERSION,
            })
        if progress_callback and progress_callback(index + 1, total_urls, f"已读取 {index + 1}/{total_urls} 个投稿", False):
            cancelled = True
            break
    result = {
        "submissions": results, "total": len(results),
        "tag_passed": sum(item.get("tag_ok") for item in results),
        "interaction_passed": sum(item.get("over_200") is True for item in results),
        "interaction_10000_passed": sum(item.get('over_10000') is True for item in results),
        "requested_total": total_urls, "cancelled": cancelled,
        "login_required": login_required,
        "campaign_tags": campaigns, "campaign_tag": campaigns[0], "device_tags": devices,
        "checked_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "app_version": APP_VERSION,
    }
    token = uuid.uuid4().hex[:16]
    result["export_token"] = token
    try:
        MONTHLY_GOAL.register(result)
    except Exception as exc:
        result['goal_error'] = f'审核结果已保留，但月度记录保存失败：{exc}'
    with EXPORT_LOCK:
        SUBMISSION_EXPORT_CACHE[token] = result
        while len(SUBMISSION_EXPORT_CACHE) > 30:
            SUBMISSION_EXPORT_CACHE.pop(next(iter(SUBMISSION_EXPORT_CACHE)))
    if progress_callback:
        final_message = login_required or (f"已取消 · 保留 {len(results)}/{total_urls} 个投稿结果" if cancelled else "投稿审核完成")
        progress_callback(len(results), total_urls, final_message, True)
    return result


def excel_rows(result: Dict[str, Any]) -> List[Dict[str, str]]:
    """把账号检测结果转换为用户指定的四列表格。"""
    rows: List[Dict[str, str]] = []
    for account in result.get("accounts", []):
        if account.get("data_status") != "ok":
            rows.append({
                "本周是否有发作品": "无法确认", "创作内容方向": "无法确认",
                "是否有爆款": "无法确认", "爆款来源": "无法确认",
            })
            continue
        works = account.get("works") or []
        tags = list(dict.fromkeys(tag for work in works for tag in (work.get("tags") or [])))
        if tags:
            direction = "、".join(f"#{tag}" for tag in tags)
        else:
            descriptions = list(dict.fromkeys(
                str(work.get("description", "")).strip() for work in works
                if str(work.get("description", "")).strip()
            ))
            direction = "；".join(text[:60] for text in descriptions[:3]) or "—"
        hot_works = [work for work in works if work.get("over_1000")]
        hot_tags = list(dict.fromkeys(
            tag for work in hot_works for tag in (work.get("tags") or [])
        ))
        rows.append({
            "本周是否有发作品": "是" if works else "否",
            "创作内容方向": direction,
            "是否有爆款": "是" if hot_works else "否",
            "爆款来源": "、".join(f"#{tag}" for tag in hot_tags) if hot_works and hot_tags
            else "无 Tag" if hot_works else "—",
        })
    return rows


def build_excel_export(token: str) -> Path:
    with EXPORT_LOCK:
        if token == "latest" and EXPORT_CACHE:
            token = next(reversed(EXPORT_CACHE))
        result = EXPORT_CACHE.get(token)
    if not result:
        raise CheckError("导出数据已过期，请重新检测后再导出")
    export_dir = OUTPUTS / token
    export_dir.mkdir(parents=True, exist_ok=True)
    output_path = export_dir / "社媒周更审核.xlsx"
    headers = ["本周是否有发作品", "创作内容方向", "是否有爆款", "爆款来源"]
    rows = excel_rows(result)
    try:
        workbook = xlsxwriter.Workbook(str(output_path))
        sheet = workbook.add_worksheet("周更审核")
        header_format = workbook.add_format({
            "bold": True, "font_color": "#FFFFFF", "bg_color": "#111318",
            "align": "left", "valign": "vcenter", "border": 0,
        })
        body_format = workbook.add_format({"font_color": "#24262B", "valign": "top", "text_wrap": True})
        center_format = workbook.add_format({"font_color": "#24262B", "align": "center", "valign": "top"})
        yes_format = workbook.add_format({
            "font_color": "#087D55", "bg_color": "#E6F8EF", "bold": True,
            "align": "center", "valign": "top",
        })
        hot_format = workbook.add_format({
            "font_color": "#BD1734", "bg_color": "#FDE9ED", "bold": True,
            "align": "center", "valign": "top",
        })
        for col, header in enumerate(headers):
            sheet.write(0, col, header, header_format)
        for row_index, row in enumerate(rows, start=1):
            for col, header in enumerate(headers):
                value = row.get(header, "—")
                cell_format = body_format
                if col in (0, 2):
                    cell_format = yes_format if value == "是" and col == 0 else hot_format if value == "是" else center_format
                sheet.write(row_index, col, value, cell_format)
            sheet.set_row(row_index, 34)
        sheet.set_row(0, 25)
        sheet.set_column("A:A", 18)
        sheet.set_column("B:B", 38)
        sheet.set_column("C:C", 15)
        sheet.set_column("D:D", 36)
        sheet.freeze_panes(1, 0)
        sheet.autofilter(0, 0, max(1, len(rows)), 3)
        sheet.hide_gridlines(2)
        workbook.close()
    except Exception as exc:
        raise CheckError(f"Excel 生成失败：{exc}") from exc
    return output_path


def submission_excel_rows(result: Dict[str, Any]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for item in result.get("submissions", []):
        content_status = item.get("content_status")
        if content_status == "deleted":
            qualifies = "已删除"
            interaction_result = "已删除"
        elif not item.get("tag_data_available"):
            qualifies = "无法确认"
            interaction_result = "是" if item.get("over_200") is True else "否" if item.get("over_200") is False else "无法确认"
        else:
            qualifies = "是" if item.get("tag_ok") else "否"
            interaction_result = "是" if item.get("over_200") is True else "否" if item.get("over_200") is False else "无法确认"
        status_text = "已删除" if content_status == "deleted" else "可正常读取" if content_status == "available" else "无法读取"
        over_10000 = item.get('over_10000')
        high_interaction_result = '已删除' if content_status == 'deleted' else '是' if over_10000 is True else '否' if over_10000 is False else '无法确认'
        rows.append({
            "平台": str(item.get("platform") or "未知"),
            "博主名字": str(item.get("author_name") or "无法读取"),
            "博主ID": str(item.get("author_id") or "无法读取"),
            "作品链接": str(item.get("original_url") or item.get("resolved_url") or ""),
            "作品状态": status_text,
            "是否符合投稿要求": qualifies,
            "是否大于200互动量": interaction_result,
            "是否大于1万互动量": high_interaction_result,
            "说明": "；".join(str(value) for value in item.get("warnings", []) if value) or "—",
        })
    return rows


def build_submission_excel_export(token: str) -> Path:
    with EXPORT_LOCK:
        if token == "latest" and SUBMISSION_EXPORT_CACHE:
            token = next(reversed(SUBMISSION_EXPORT_CACHE))
        result = SUBMISSION_EXPORT_CACHE.get(token)
    if not result:
        raise CheckError("投稿导出数据已过期，请重新审核后再导出")
    export_dir = OUTPUTS / f"submission-{token}"
    export_dir.mkdir(parents=True, exist_ok=True)
    output_path = export_dir / "投稿审核结果.xlsx"
    headers = ["平台", "博主名字", "博主ID", "作品链接", "作品状态", "是否符合投稿要求", "是否大于200互动量", "是否大于1万互动量", "说明"]
    rows = submission_excel_rows(result)
    try:
        workbook = xlsxwriter.Workbook(str(output_path))
        sheet = workbook.add_worksheet("投稿审核")
        header_format = workbook.add_format({
            "bold": True, "font_color": "#FFFFFF", "bg_color": "#111318",
            "align": "center", "valign": "vcenter",
        })
        body_format = workbook.add_format({
            "font_color": "#24262B", "align": "center", "valign": "vcenter", "text_wrap": True,
        })
        yes_format = workbook.add_format({
            "font_color": "#087D55", "bg_color": "#E6F8EF", "bold": True,
            "align": "center", "valign": "vcenter",
        })
        no_format = workbook.add_format({
            "font_color": "#BD1734", "bg_color": "#FDE9ED", "bold": True,
            "align": "center", "valign": "vcenter",
        })
        pending_format = workbook.add_format({
            "font_color": "#8A5B00", "bg_color": "#FFF1CC",
            "align": "center", "valign": "vcenter",
        })
        for col, header in enumerate(headers):
            sheet.write(0, col, header, header_format)
        for row_index, row in enumerate(rows, start=1):
            for col, header in enumerate(headers):
                value = row[header]
                if header in ("是否符合投稿要求", "是否大于200互动量", "是否大于1万互动量", "作品状态"):
                    cell_format = yes_format if value in ("是", "可正常读取") else no_format if value in ("否", "已删除") else pending_format
                else:
                    cell_format = body_format
                sheet.write(row_index, col, value, cell_format)
            sheet.set_row(row_index, 28)
        sheet.set_row(0, 28)
        sheet.set_column("A:A", 10)
        sheet.set_column("B:C", 20)
        sheet.set_column("D:D", 52)
        sheet.set_column("E:H", 22)
        sheet.set_column("I:I", 48)
        sheet.freeze_panes(1, 0)
        sheet.autofilter(0, 0, max(1, len(rows)), len(headers) - 1)
        sheet.hide_gridlines(2)
        workbook.close()
    except Exception as exc:
        raise CheckError(f"投稿 Excel 生成失败：{exc}") from exc
    return output_path


class Handler(BaseHTTPRequestHandler):
    def _shutdown_server(self) -> None:
        with CONTROL_CONDITION:
            for state in CONTROL_CACHE.values():
                state["cancelled"] = True
                state["paused"] = False
            CONTROL_CONDITION.notify_all()
        DOUYIN_COLLECTOR.close()
        XHS_COLLECTOR.close()
        PUBLIC_COLLECTOR.close()
        self.server.shutdown()
        if DESKTOP_WINDOW is not None:
            DESKTOP_WINDOW.destroy()

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        if path == '/api/monthly-goal':
            try:
                month = urllib.parse.parse_qs(parsed_url.query).get('month', [None])[0]
                result = MONTHLY_GOAL.summary(month)
                self._send(200, json.dumps(result, ensure_ascii=False).encode(), 'application/json; charset=utf-8')
            except GoalError as exc:
                self._send(400, json.dumps({'error': str(exc)}, ensure_ascii=False).encode(), 'application/json; charset=utf-8')
            except Exception:
                self._send(500, json.dumps({'error': '月度记录读取失败，请保留数据库文件并检查磁盘权限。'}, ensure_ascii=False).encode(), 'application/json; charset=utf-8')
            return
        if path == '/api/device-flow':
            query = urllib.parse.parse_qs(parsed_url.query)
            today = datetime.now().date().isoformat()
            try:
                result = DEVICE_FLOW.state(query.get('start', [today])[0], query.get('end', [today])[0], query.get('long_term', ['0'])[0] == '1')
                self._send(200, json.dumps(result, ensure_ascii=False).encode(), 'application/json; charset=utf-8')
            except FlowError as exc:
                self._send(400, json.dumps({'error': str(exc)}, ensure_ascii=False).encode(), 'application/json; charset=utf-8')
            return
        if path == "/api/health":
            body = json.dumps({"ok": True, "app": APP_ID, "version": APP_VERSION}).encode()
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/api/progress":
            token = urllib.parse.parse_qs(parsed_url.query).get("token", [""])[0]
            with EXPORT_LOCK:
                progress = dict(PROGRESS_CACHE.get(token, {
                    "current": 0, "total": 0, "percent": 0,
                    "message": "等待开始读取", "done": False,
                }))
            with CONTROL_CONDITION:
                progress.update(CONTROL_CACHE.get(token, {"paused": False, "cancelled": False}))
            if XHS_COLLECTOR.login_waiting and not progress.get('done'):
                progress['message'] = '等待小红书登录 / 验证：请在保留的专用窗口完成一次扫码，完成后将自动继续当前作品。'
            self._send(200, json.dumps(progress, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if path == "/api/export":
            try:
                token = urllib.parse.parse_qs(parsed_url.query).get("token", [""])[0]
                target = build_excel_export(token)
                body = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%E7%A4%BE%E5%AA%92%E5%91%A8%E6%9B%B4%E5%AE%A1%E6%A0%B8.xlsx")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except CheckError as exc:
                self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        if path == "/api/submission-export":
            try:
                token = urllib.parse.parse_qs(parsed_url.query).get("token", [""])[0]
                target = build_submission_excel_export(token)
                body = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%E6%8A%95%E7%A8%BF%E5%AE%A1%E6%A0%B8%E7%BB%93%E6%9E%9C.xlsx")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except CheckError as exc:
                self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return
        filename = "index.html" if path == "/" else path.lstrip("/")
        target = (STATIC / filename).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        mime = mimetypes.guess_type(str(target))[0] or 'application/octet-stream'
        if mime.startswith('text/') or mime in ('application/javascript', 'application/json'):
            mime += '; charset=utf-8'
        self._send(200, target.read_bytes(), mime)

    def do_HEAD(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        filename = "index.html" if path == "/" else path.lstrip("/")
        target = (STATIC / filename).resolve()
        status = 200 if STATIC.resolve() in target.parents and target.is_file() else 404
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in (
            "/api/check", "/api/login", "/api/login-status", "/api/xhs-login",
            "/api/xhs-login-status", "/api/submission-check", "/api/progress-control", "/api/shutdown",
            '/api/device-inventory', '/api/device-booking', '/api/device-delete',
            '/api/device-cancel-long-term',
            '/api/submission-watermark',
        ):
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        progress_token = ""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            raw_progress_token = str(payload.get("progress_token", ""))
            progress_token = raw_progress_token if re.fullmatch(r"[A-Za-z0-9_-]{8,80}", raw_progress_token) else ""
            if self.path == '/api/submission-watermark':
                result = MONTHLY_GOAL.confirm(str(payload.get('review_id', '')), payload.get('confirmed'))
                with EXPORT_LOCK:
                    for batch in SUBMISSION_EXPORT_CACHE.values():
                        for item in batch.get('submissions', []):
                            if item.get('work_key') == result['work_key']:
                                item['watermark_status'] = 'confirmed' if result['confirmed'] else 'pending'
                                item['counted_month'] = result['counted_month']
            elif self.path == '/api/device-inventory':
                DEVICE_FLOW.inventory(payload.get('inventory'))
                result = {'ok': True}
            elif self.path == '/api/device-booking':
                result = {'ok': True, 'booking': DEVICE_FLOW.book(payload)}
            elif self.path == '/api/device-delete':
                DEVICE_FLOW.delete(str(payload.get('id', '')))
                result = {'ok': True}
            elif self.path == '/api/device-cancel-long-term':
                result = {'ok': True, 'booking': DEVICE_FLOW.cancel_long_term(str(payload.get('id', '')))}
            elif self.path == "/api/shutdown":
                result = {"ok": True, "message": "程序已退出"}
                threading.Timer(0.5, self._shutdown_server).start()
            elif self.path == "/api/progress-control":
                state = _update_control(progress_token, str(payload.get("action", "")))
                result = {"ok": True, **state}
            elif self.path == "/api/login":
                DOUYIN_COLLECTOR.open_login()
                result = {"ok": True, "message": "抖音登录窗口已打开"}
            elif self.path == "/api/login-status":
                logged_in = DOUYIN_COLLECTOR.is_logged_in()
                result = {"ok": logged_in, "logged_in": logged_in}
            elif self.path == "/api/xhs-login":
                XHS_COLLECTOR.open_login()
                result = {"ok": True, "message": "小红书登录窗口已打开"}
            elif self.path == "/api/xhs-login-status":
                logged_in = XHS_COLLECTOR.is_logged_in()
                result = {"ok": logged_in, "logged_in": logged_in}
            elif self.path == "/api/submission-check":
                _initialize_control(progress_token)
                raw_urls = payload.get("urls")
                if not isinstance(raw_urls, list):
                    raw_urls = [payload.get("url", "")]
                raw_devices = payload.get("device_tags")
                if not isinstance(raw_devices, list):
                    raw_devices = str(raw_devices or "").splitlines()
                raw_campaigns = payload.get("campaign_tags")
                if not isinstance(raw_campaigns, list):
                    raw_campaigns = str(payload.get("campaign_tag", "")).splitlines()
                result = fetch_submissions(
                    (str(url) for url in raw_urls), (str(tag) for tag in raw_campaigns),
                    (str(tag) for tag in raw_devices),
                    progress_callback=lambda current, total, message, done: _progress_with_control(
                        progress_token, current, total, message, done
                    ),
                )
            else:
                _initialize_control(progress_token)
                raw_urls = payload.get("urls")
                if not isinstance(raw_urls, list):
                    raw_urls = [payload.get("url", "")]
                result = fetch_accounts(
                    (str(url) for url in raw_urls), str(payload.get("cookie", "")),
                    progress_callback=lambda current, total, message, done: _progress_with_control(
                        progress_token, current, total, message, done
                    ),
                )
            body, status = json.dumps(result, ensure_ascii=False).encode(), HTTPStatus.OK
        except (CheckError, FlowError, GoalError) as exc:
            _set_progress(progress_token, 0, 0, str(exc), True)
            body, status = json.dumps({"error": str(exc)}, ensure_ascii=False).encode(), HTTPStatus.BAD_REQUEST
        except Exception as exc:  # keep local UI informative
            _set_progress(progress_token, 0, 0, f"检测失败：{exc}", True)
            body, status = json.dumps({"error": f"检测失败：{exc}"}, ensure_ascii=False).encode(), HTTPStatus.INTERNAL_SERVER_ERROR
        self._send(status, body, "application/json; charset=utf-8")

    def log_message(self, fmt: str, *args: Any) -> None:
        if sys.stdout is not None:
            sys.stdout.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


def main() -> None:
    global DESKTOP_WINDOW
    parser = argparse.ArgumentParser(description="抖音 / 小红书主页近 7 天作品检查器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument('--browser', action='store_true', help='使用浏览器兼容模式（默认直接打开软件窗口）')
    args = parser.parse_args()
    server = None
    selected_port = args.port
    for candidate in range(args.port, args.port + 10):
        try:
            server = ThreadingHTTPServer((args.host, candidate), Handler)
            selected_port = candidate
            break
        except OSError:
            try:
                health = requests.get(f"http://{args.host}:{candidate}/api/health", timeout=1).json()
                if health.get("app") == APP_ID and health.get("version") == APP_VERSION:
                    if args.browser and not args.no_browser:
                        webbrowser.open(f"http://{args.host}:{candidate}")
                        return
                    if args.no_browser:
                        return
            except (requests.RequestException, ValueError):
                continue
    if server is None:
        raise RuntimeError(f"{args.port}-{args.port + 9} 端口均被占用，请关闭其他本地服务后重试")
    url = f"http://{args.host}:{selected_port}"
    print(f"服务已启动：{url}（按 Ctrl+C 停止）")
    if args.browser and not args.no_browser:
        webbrowser.open(url)
    if not args.no_browser and not args.browser:
        from desktop_shell import create_desktop_window, start_desktop
        try:
            webview, DESKTOP_WINDOW = create_desktop_window(url)
        except Exception:
            server.server_close()
            raise
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            start_desktop(webview, DATA_ROOT)
        finally:
            with CONTROL_CONDITION:
                for state in CONTROL_CACHE.values():
                    state['cancelled'] = True
                    state['paused'] = False
                CONTROL_CONDITION.notify_all()
            server.shutdown()
            server.server_close()
            DOUYIN_COLLECTOR.close()
            XHS_COLLECTOR.close()
            PUBLIC_COLLECTOR.close()
            DESKTOP_WINDOW = None
        return
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()


def show_startup_error(message: str) -> None:
    """安装版没有终端时，用系统对话框展示启动错误。"""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, "抖音周更雷达", 0x10)
            return
        except Exception:
            pass
    if sys.platform == "darwin":
        safe_message = message.replace("\\", "\\\\").replace('"', '\\"')
        try:
            subprocess.run(
                ["osascript", "-e", f'display alert "抖音周更雷达" message "{safe_message}" as critical'],
                timeout=10,
            )
            return
        except Exception:
            pass
    print(f"启动失败：{message}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        show_startup_error(str(exc))
        raise
