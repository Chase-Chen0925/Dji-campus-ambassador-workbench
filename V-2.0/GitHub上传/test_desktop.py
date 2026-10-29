"""Integration checks for the desktop lifecycle and bundled image/download routes."""
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import app
from monthly_goal import MonthlyGoal
from device_flow import DeviceFlow, MODELS


class DesktopTests(unittest.TestCase):
    def setUp(self):
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self.url = f'http://127.0.0.1:{self.port}'
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_native_launch_downloads_images_and_shutdown_without_browser(self):
        def ui_loop(*args):
            health = requests.get(self.url + '/api/health', timeout=2)
            self.assertEqual(health.json()['version'], app.APP_VERSION)
            for model in ('pocket4', 'action6', 'nano', 'osmo360'):
                photo = requests.get(self.url + f'/images/devices/{model}.png', timeout=2)
                self.assertEqual(photo.status_code, 200)
                self.assertEqual(photo.headers['Content-Type'], 'image/png')
                self.assertTrue(photo.content.startswith(b'\x89PNG'))
            export = requests.get(self.url + '/api/submission-export?token=desktop-test', timeout=2)
            self.assertEqual(export.status_code, 200)
            self.assertIn('attachment', export.headers['Content-Disposition'])
            self.assertTrue(export.content.startswith(b'PK'))
            self.assertEqual(requests.get(self.url + '/api/device-flow', timeout=2).status_code, 200)
            inventory = requests.post(self.url + '/api/device-inventory', json={'inventory': {m: 2 for m in MODELS}}, timeout=2)
            self.assertEqual(inventory.status_code, 200)
            booking = requests.post(self.url + '/api/device-booking', json={
                'model': 'pocket4', 'quantity': 1, 'student': '长期流转接口演示',
                'start': '2000-01-01', 'end': None, 'long_term': True}, timeout=2)
            self.assertEqual(booking.status_code, 200)
            future = requests.get(self.url + '/api/device-flow?start=2099-01-01&long_term=1', timeout=2).json()
            self.assertEqual(len(future['available']['pocket4']), 1)
            self.assertIsNone(future['end'])
            cancelled = requests.post(self.url + '/api/device-cancel-long-term', json={'id': booking.json()['booking']['id']}, timeout=2)
            self.assertEqual(cancelled.status_code, 200)
            self.assertEqual(len(requests.get(self.url + '/api/device-flow?start=2099-01-01&long_term=1', timeout=2).json()['available']['pocket4']), 2)
            review = {'platform_code': 'douyin', 'content_id': '123456789', 'tag_ok': True,
                      'tag_data_available': True, 'content_status': 'available', 'title': '接口测试'}
            app.MONTHLY_GOAL.register({'submissions': [review]})
            for _ in range(2):
                confirmed = requests.post(self.url + '/api/submission-watermark', json={'review_id': review['review_id'], 'confirmed': True}, timeout=2)
                self.assertEqual(confirmed.status_code, 200)
            self.assertEqual(requests.get(self.url + '/api/monthly-goal', timeout=2).json()['count'], 1)
            self.assertEqual(requests.post(self.url + '/api/submission-watermark', json={'review_id': 'unknown', 'confirmed': True}, timeout=2).status_code, 400)

        with patch.object(sys, 'argv', ['app.py', '--port', str(self.port)]), \
             patch('desktop_shell.create_desktop_window', return_value=(Mock(), Mock())), \
             patch('desktop_shell.start_desktop', side_effect=ui_loop), \
             patch.object(app.webbrowser, 'open') as external_browser, \
             patch.object(app, 'DATA_ROOT', Path(self.temp.name)), \
             patch.object(app, 'OUTPUTS', Path(self.temp.name) / 'outputs'), \
             patch.object(app, 'MONTHLY_GOAL', MonthlyGoal(Path(self.temp.name) / 'reviews.sqlite3')), \
             patch.object(app, 'DEVICE_FLOW', DeviceFlow(Path(self.temp.name) / 'devices.json')), \
             patch.object(app, 'SUBMISSION_EXPORT_CACHE', {'desktop-test': {'submissions': []}}), \
             patch.object(app.DOUYIN_COLLECTOR, 'close'), \
             patch.object(app.XHS_COLLECTOR, 'close'), \
             patch.object(app.PUBLIC_COLLECTOR, 'close'):
            app.main()
            external_browser.assert_not_called()
        with self.assertRaises(requests.ConnectionError):
            requests.get(self.url + '/api/health', timeout=1)

    def test_failed_window_creation_releases_server_port(self):
        with patch.object(sys, 'argv', ['app.py', '--port', str(self.port)]), \
             patch('desktop_shell.create_desktop_window', side_effect=RuntimeError('no renderer')):
            with self.assertRaisesRegex(RuntimeError, 'no renderer'):
                app.main()
        sock = socket.socket()
        try:
            sock.bind(('127.0.0.1', self.port))
        finally:
            sock.close()
