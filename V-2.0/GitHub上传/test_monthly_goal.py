import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from monthly_goal import CN, GoalError, MonthlyGoal, work_identity


class MonthlyGoalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime(2026, 10, 29, 12, 0, tzinfo=CN)
        self.store = MonthlyGoal(Path(self.tmp.name) / 'ledger.sqlite3', now=lambda: self.now)

    def review(self, **changes):
        item = {'platform': '小红书', 'platform_code': 'xiaohongshu', 'tag_ok': True,
                'tag_data_available': True, 'content_status': 'available',
                'original_url': 'https://www.xiaohongshu.com/explore/6a9ade8c000000002a005458?xsec_token=one',
                'title': '有效作品', 'author_name': '同学甲', 'over_200': False}
        item.update(changes)
        self.store.register({'submissions': [item]})
        return item

    def test_pending_and_repeated_confirmations(self):
        item = self.review()
        self.assertEqual(self.store.summary()['target'], 120)
        self.assertEqual(self.store.summary()['count'], 0)
        self.store.confirm(item['review_id'], True)
        self.store.confirm(item['review_id'], True)
        state = self.store.summary()
        self.assertEqual(state['count'], 1)
        self.assertEqual(state['remaining'], 119)
        self.assertEqual(len(state['entries']), 1)

    def test_short_long_and_tracking_query_dedup(self):
        first = self.review()
        second = self.review(original_url='https://xhslink.cn/o/example',
                             resolved_url='https://www.xiaohongshu.com/discovery/item/6a9ade8c000000002a005458?xsec_token=two')
        for item in (first, second):
            self.store.confirm(item['review_id'], True)
        self.assertEqual(first['work_key'], second['work_key'])
        self.assertEqual(self.store.summary()['count'], 1)
        again = self.review()
        self.assertEqual(again['watermark_status'], 'confirmed')

    def test_cross_month_reaudit_never_moves_or_duplicates_work(self):
        item = self.review()
        self.store.confirm(item['review_id'], True)
        self.now = datetime(2026, 11, 1, 0, 1, tzinfo=CN)
        again = self.review()
        self.store.confirm(again['review_id'], True)
        self.assertEqual(self.store.summary()['count'], 0)
        self.assertEqual(self.store.summary('2026-10')['count'], 1)
        self.store.confirm(again['review_id'], False)
        self.assertEqual(self.store.summary('2026-10')['count'], 0)
        self.store.confirm(again['review_id'], True)
        self.assertEqual(self.store.summary()['count'], 0)
        self.assertEqual(self.store.summary('2026-10')['count'], 1)

    def test_invalid_or_missing_identity_cannot_be_counted(self):
        for changes in ({'tag_ok': False}, {'content_status': 'deleted'}, {'content_status': 'unavailable'},
                        {'tag_data_available': False}, {'original_url': 'https://xhslink.cn/o/unresolved'}):
            item = self.review(**changes)
            with self.assertRaises(GoalError):
                self.store.confirm(item['review_id'], True)
        self.assertEqual(self.store.summary()['count'], 0)

    def test_platform_ids_and_bvid_are_stable(self):
        a = {'platform_code': 'douyin', 'original_url': 'https://www.douyin.com/video/12345678?share=1'}
        b = {'platform_code': 'douyin', 'original_url': 'https://v.douyin.com/a/', 'resolved_url': 'https://www.douyin.com/note/12345678'}
        self.assertEqual(work_identity(a), work_identity(b))
        self.assertEqual(work_identity({'platform_code': 'bilibili', 'content_id': 'BV1xx411c7mD', 'original_url': 'https://www.bilibili.com/video/av170001'}), 'bilibili:BV1xx411c7mD')
        self.assertEqual(work_identity({'platform_code': 'bilibili', 'original_url': 'https://www.bilibili.com/video/av170001'}), '')
        self.assertEqual(work_identity({'platform_code': 'weixin', 'resolved_url': 'https://channels.weixin.qq.com/platform/post?objectId=123456'}), 'weixin:123456')

    def test_database_persists_across_restarts(self):
        self.store.confirm(self.review()['review_id'], True)
        new = MonthlyGoal(self.store.path, now=lambda: self.now)
        self.assertEqual(new.summary()['count'], 1)

    def test_parallel_confirmation_does_not_inflate_count(self):
        reviews = [self.review() for _ in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda item: self.store.confirm(item['review_id'], True), reviews))
        self.assertEqual(self.store.summary()['count'], 1)

    def test_inputs_and_unknown_review_are_rejected(self):
        with self.assertRaises(GoalError):
            self.store.summary('2026-13')
        with self.assertRaises(GoalError):
            self.store.confirm('missing', True)
        with self.assertRaises(GoalError):
            self.store.confirm(self.review()['review_id'], 'true')
