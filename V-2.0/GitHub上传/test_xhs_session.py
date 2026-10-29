import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from app import ChromeCollector, XhsLoginRequired, fetch_submission, fetch_submissions


class FakeSocket:
    def __init__(self):
        self.queue = []
        self.sent = []

    def send(self, text):
        command = json.loads(text)
        self.sent.append(command)
        if command['method'] == 'Runtime.evaluate':
            expression = command['params']['expression']
            value = {} if '__INITIAL_STATE__' in expression else {
                'url': 'https://www.xiaohongshu.com/explore/6a9ade8c000000002a005458', 'title': '作品'}
            self.queue.append(json.dumps({'id': command['id'], 'result': {'result': {'value': value}}}))

    def recv(self):
        return self.queue.pop(0)

    def close(self):
        pass


class XhsSessionTests(unittest.TestCase):
    def test_login_in_same_tab_resumes_original_note(self):
        class LoginSocket(FakeSocket):
            probes = 0

            def send(self, text):
                command = json.loads(text)
                if command['method'] == 'Runtime.evaluate' and 'return {login, captcha' in command['params']['expression']:
                    self.sent.append(command)
                    self.probes += 1
                    self.queue.append(json.dumps({'id': command['id'], 'result': {'result': {'value': {'login': self.probes == 1}}}}))
                else:
                    super().send(text)

        class Clock:
            value = 0

            def __call__(self):
                self.value += 0.2
                return self.value

        collector = ChromeCollector('xiaohongshu', Path('/private/tmp/xhs-unused'), 'https://www.xiaohongshu.com/')
        collector.port = 12345
        target = {'id': 'same-tab', 'webSocketDebuggerUrl': 'ws://127.0.0.1/test'}
        ws = LoginSocket()
        url = 'https://www.xiaohongshu.com/explore/6a9ade8c000000002a005458?xsec_token=keep'
        with patch.object(collector, 'start'), patch.object(collector, '_submission_target', return_value=target), \
             patch('app.requests.get'), patch('app.websocket.create_connection', return_value=ws), patch('app.time.time', new=Clock()):
            page = collector.collect_submission_page(url, wait_seconds=4)
        navigations = [c['params']['url'] for c in ws.sent if c['method'] == 'Page.navigate']
        self.assertEqual(navigations, [url, url])
        self.assertFalse(collector.login_waiting)
        self.assertEqual(page['title'], '作品')

    def test_all_notes_reuse_existing_tab_and_do_not_close_it(self):
        collector = ChromeCollector('xiaohongshu', Path('/private/tmp/xhs-test-unused'), 'https://www.xiaohongshu.com/')
        collector.port = 12345
        target = {'id': 'persistent-tab', 'type': 'page', 'url': 'https://www.xiaohongshu.com/', 'webSocketDebuggerUrl': 'ws://127.0.0.1/test'}
        sockets = [FakeSocket(), FakeSocket()]
        with patch.object(collector, 'start'), patch('app.requests.get', return_value=Mock(json=lambda: [target])) as get, \
             patch('app.requests.put') as put, patch('app.websocket.create_connection', side_effect=sockets):
            for i in range(2):
                collector.collect_submission_page(f'https://www.xiaohongshu.com/explore/{i}', wait_seconds=0)
            put.assert_not_called()
            self.assertEqual(collector.submission_target_id, 'persistent-tab')
            self.assertTrue(all('/json/close/' not in call.args[0] for call in get.call_args_list))
            for ws in sockets:
                navigation = next(c for c in ws.sent if c['method'] == 'Page.navigate')
                self.assertEqual(navigation['params']['referrer'], 'https://www.xiaohongshu.com/explore')

    def test_fallback_preserves_signed_query_and_skips_anonymous_request(self):
        url = 'https://www.xiaohongshu.com/discovery/item/6a9ade8c000000002a005458?xsec_token=TOKEN&xsec_source=app_share'
        page = {'url': url, 'payloads': [], 'text': ''}
        with patch('app.requests.Session') as session, \
             patch('app.XHS_COLLECTOR.collect_submission_page', return_value=page) as collect:
            fetch_submission(url, ['活动'], ['Pocket4'])
            session.return_value.get.assert_not_called()
            fallback = collect.call_args_list[1].args[0]
            self.assertIn('/explore/6a9ade8c000000002a005458', fallback)
            self.assertIn('xsec_token=TOKEN', fallback)
            self.assertIn('xsec_source=app_share', fallback)

    def test_login_timeout_stops_batch_once_without_discarding_completed_rows(self):
        with patch('app.fetch_submission', side_effect=[{'tag_ok': True, 'over_200': False}, XhsLoginRequired('请完成登录')]) as fetch, \
             patch('app.MONTHLY_GOAL.register'):
            result = fetch_submissions(['https://xhslink.cn/a', 'https://xhslink.cn/b', 'https://xhslink.cn/c'], ['活动'], ['Pocket4'])
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(result['total'], 1)
            self.assertEqual(result['requested_total'], 3)
            self.assertEqual(result['login_required'], '请完成登录')
