"""Durable review ledger: a work can contribute to only its first confirmed month."""
import html
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

CN = timezone(timedelta(hours=8))
TARGET = 120


class GoalError(ValueError):
    pass


def work_identity(item):
    platform = item.get('platform_code') or {'抖音': 'douyin', '小红书': 'xiaohongshu', 'B站': 'bilibili', '视频号': 'weixin'}.get(item.get('platform'))
    content_id = str(item.get('content_id') or '')
    patterns = {
        'douyin': r'^\d+$', 'xiaohongshu': r'^[0-9a-fA-F]{24}$',
        'bilibili': r'^BV[0-9A-Za-z]{10}$', 'weixin': r'^[A-Za-z0-9_+=/.-]{1,200}$',
    }
    if platform in patterns and re.fullmatch(patterns[platform], content_id):
        return platform + ':' + (content_id.lower() if platform == 'xiaohongshu' else content_id)
    for url in (item.get('resolved_url'), item.get('original_url')):
        text = html.unescape(unquote(str(url or '')))
        parsed = urlparse(text)
        host = (parsed.hostname or '').lower()
        query = parse_qs(parsed.query)
        if platform == 'douyin' and (host == 'douyin.com' or host.endswith(('.douyin.com', '.iesdouyin.com'))):
            match = re.search(r'/(?:video|note)/(\d+)', parsed.path)
            value = match.group(1) if match else next((query[k][0] for k in ('modal_id', 'aweme_id', 'item_id') if k in query), '')
            if value.isdigit():
                return 'douyin:' + value
        elif platform == 'xiaohongshu' and (host == 'xiaohongshu.com' or host.endswith('.xiaohongshu.com')):
            match = re.search(r'/(?:explore|discovery/item)/([0-9a-fA-F]{24})', parsed.path)
            if match:
                return 'xiaohongshu:' + match.group(1).lower()
        elif platform == 'bilibili' and (host == 'bilibili.com' or host.endswith('.bilibili.com')):
            match = re.search(r'/video/(BV[A-Za-z0-9]{10})', parsed.path)
            if match:
                return 'bilibili:' + match.group(1)
        elif platform == 'weixin' and (host == 'weixin.qq.com' or host.endswith('.weixin.qq.com')):
            value = next((query[k][0] for k in ('objectId', 'object_id', 'feedId', 'feed_id') if k in query), '')
            if re.fullmatch(patterns['weixin'], value):
                return 'weixin:' + value
    # A share URL or title is not a stable work ID. Refuse to inflate totals.
    return ''


def eligible(item):
    return bool(item.get('tag_ok') and item.get('tag_data_available')
                and item.get('content_status') == 'available')


class MonthlyGoal:
    def __init__(self, path, now=None):
        self.path = Path(path)
        self.now = now or (lambda: datetime.now(CN))

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=15000')
        db.execute('CREATE TABLE IF NOT EXISTS reviews (id TEXT PRIMARY KEY, work_key TEXT NOT NULL, data TEXT NOT NULL, reviewed_at TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS works (work_key TEXT PRIMARY KEY, month TEXT NOT NULL, confirmed_at TEXT NOT NULL, active INTEGER NOT NULL, review_id TEXT NOT NULL, data TEXT NOT NULL)')
        db.execute('CREATE INDEX IF NOT EXISTS works_month ON works(month, active)')
        db.commit()
        return db

    def register(self, result):
        """Save each completed review; never count a pending watermark automatically."""
        now = self.now().isoformat(timespec='seconds')
        db = self.connect()
        try:
            with db:
                for item in result.get('submissions', []):
                    key = work_identity(item)
                    review_id = uuid.uuid4().hex
                    existing = db.execute('SELECT month, active FROM works WHERE work_key=?', (key,)).fetchone() if key else None
                    item.update(review_id=review_id, work_key=key,
                                watermark_status='confirmed' if existing and existing['active'] else 'pending',
                                counted_month=existing['month'] if existing and existing['active'] else '',
                                goal_eligible=eligible(item) and bool(key))
                    db.execute('INSERT INTO reviews VALUES (?,?,?,?)', (review_id, key, json.dumps(item, ensure_ascii=False), now))
        finally:
            db.close()

    def confirm(self, review_id, confirmed):
        if not isinstance(confirmed, bool):
            raise GoalError('水印确认状态必须为是或否')
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            review = db.execute('SELECT * FROM reviews WHERE id=?', (review_id,)).fetchone()
            if not review:
                raise GoalError('未找到审核记录，请重新审核后确认')
            data = json.loads(review['data'])
            key = review['work_key']
            if not key:
                raise GoalError('未获取到作品唯一 ID，无法可靠去重；请使用完整作品链接重新审核')
            if confirmed and not eligible(data):
                raise GoalError('只有可正常读取且设备和活动 Tag 均通过的作品，才能确认并计入目标')
            existing = db.execute('SELECT month, active FROM works WHERE work_key=?', (key,)).fetchone()
            now = self.now().isoformat(timespec='seconds')
            month = existing['month'] if existing else now[:7]
            added = bool(confirmed and not (existing and existing['active']))
            if confirmed:
                db.execute('INSERT INTO works VALUES (?,?,?,?,?,?) ON CONFLICT(work_key) DO UPDATE SET active=1, review_id=excluded.review_id, data=excluded.data',
                           (key, month, now, 1, review_id, json.dumps(data, ensure_ascii=False)))
            else:
                db.execute('UPDATE works SET active=0 WHERE work_key=?', (key,))
            db.commit()
            return {'ok': True, 'work_key': key, 'confirmed': confirmed,
                    'counted_month': month if confirmed else '', 'added': added,
                    'message': ('已计入 ' + month + '，同一作品不会重复累计') if confirmed else '已撤销该作品的有效计入'}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def summary(self, month=None):
        month = month or self.now().strftime('%Y-%m')
        if not re.fullmatch(r'\d{4}-(?:0[1-9]|1[0-2])', str(month)):
            raise GoalError('请选择有效月份')
        db = self.connect()
        try:
            rows = db.execute('SELECT * FROM works WHERE month=? AND active=1 ORDER BY confirmed_at DESC', (month,)).fetchall()
            entries = []
            for row in rows:
                item = json.loads(row['data'])
                entries.append({k: item.get(k, '') for k in ('platform', 'author_name', 'title', 'original_url', 'resolved_url')})
                entries[-1].update(work_key=row['work_key'], review_id=row['review_id'], confirmed_at=row['confirmed_at'])
            count = len(rows)
            months = [row['month'] for row in db.execute('SELECT DISTINCT month FROM works ORDER BY month DESC').fetchall()]
            return {'month': month, 'target': TARGET, 'count': count, 'remaining': max(0, TARGET-count),
                    'percent': round(count/TARGET*100, 1), 'entries': entries, 'months': months,
                    'rule': '设备及活动 Tag 通过 + 人工确认水印；按首次确认月份计入，同一作品跨审核、跨月份不重复累计。'}
        finally:
            db.close()
