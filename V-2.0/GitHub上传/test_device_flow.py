import tempfile
import unittest
from datetime import date
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from device_flow import DeviceFlow, FlowError, MODELS


class DeviceFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.flow = DeviceFlow(Path(self.temp.name) / 'flow.json')
        self.flow.inventory({m: 2 for m in MODELS})

    def book(self, **changes):
        payload = dict(model='pocket4', quantity=1, student='同学甲', start='2030-01-30', end='2030-02-02')
        payload.update(changes)
        return self.flow.book(payload)

    def test_long_term_has_no_expiry_and_survives_restart(self):
        record = self.book(long_term=True, end=None)
        self.assertIsNone(record['end'])
        restored = DeviceFlow(self.flow.path)
        self.assertEqual(len(restored.state('2030-01-29', '2030-01-29')['available']['pocket4']), 2)
        for start, end in [('2030-02-01', '2030-02-28'), ('2031-03-01', '2031-03-31'), ('2099-12-01', '2099-12-31')]:
            self.assertEqual(restored.state(start, end)['available']['pocket4'], ['pocket4-002'])

    def test_long_term_checks_all_future_reservations(self):
        self.flow.inventory({m: 1 for m in MODELS})
        self.book(start='2035-01-01', end='2035-01-02')
        state = self.flow.state('2030-01-01', None, long_term=True)
        self.assertIsNone(state['end'])
        self.assertEqual(state['available']['pocket4'], [])
        with self.assertRaises(FlowError):
            self.book(start='2030-01-01', end=None, long_term=True)

    def test_long_term_blocks_regular_booking_and_stock_reduction(self):
        self.book(long_term=True, quantity=2)
        with self.assertRaises(FlowError):
            self.book(start='2040-07-01', end='2040-07-31')
        with self.assertRaises(FlowError):
            self.flow.inventory({m: 1 for m in MODELS})

    def test_manual_cancel_releases_from_today_and_preserves_history(self):
        record = self.book(long_term=True)
        cancelled = self.flow.cancel_long_term(record['id'], today=date(2030, 2, 1))
        self.assertEqual(cancelled['cancelled_on'], '2030-02-01')
        self.assertEqual(cancelled['end'], '2030-01-31')
        self.assertEqual(len(self.flow.state('2030-01-31', '2030-01-31')['available']['pocket4']), 1)
        self.assertEqual(len(self.flow.state('2030-02-01', '2099-12-31')['available']['pocket4']), 2)
        self.assertEqual(len(self.flow.load()['bookings']), 1)
        self.assertEqual(self.flow.cancel_long_term(record['id'], today=date(2030, 3, 1))['cancelled_on'], '2030-02-01')
        with self.assertRaises(FlowError):
            self.flow.book({**record, 'student': '不能复活的历史记录'})
        with self.assertRaises(FlowError):
            self.flow.delete(record['id'])

    def test_cancel_before_start_never_blocks_any_range(self):
        record = self.book(long_term=True)
        self.flow.cancel_long_term(record['id'], today=date(2029, 12, 1))
        self.assertEqual(len(self.flow.state('2020-01-01', '2099-12-31')['available']['pocket4']), 2)

    def test_long_term_can_be_manually_changed_to_bounded_dates(self):
        record = self.book(long_term=True)
        self.flow.book({**record, 'long_term': False, 'end': '2030-02-02'})
        self.assertEqual(len(self.flow.state('2030-02-03', None, long_term=True)['available']['pocket4']), 2)

    def test_old_records_without_long_term_flag_and_invalid_flag(self):
        record = self.book()
        data = self.flow.load()
        del data['bookings'][0]['long_term']
        self.flow.save(data)
        self.assertEqual(len(self.flow.state(record['start'], record['end'])['available']['pocket4']), 1)
        with self.assertRaises(FlowError):
            self.book(long_term='yes')

    def test_persistence_and_inclusive_cross_month_availability(self):
        record = self.book()
        state = DeviceFlow(self.flow.path).state('2030-02-02', '2030-02-02')
        self.assertEqual(state['available']['pocket4'], ['pocket4-002'])
        self.assertEqual(state['bookings'][0]['units'], record['units'])
        self.assertEqual(len(self.flow.state('2030-02-03', '2030-02-03')['available']['pocket4']), 2)

    def test_conflict_and_failed_edit_preserves_existing_booking(self):
        a = self.book()
        b = self.book()
        with self.assertRaises(FlowError):
            self.book()
        with self.assertRaises(FlowError):
            self.flow.book({**a, 'quantity': 2})
        self.assertEqual(len(self.flow.state(a['start'], a['end'])['bookings']), 2)
        self.flow.delete(b['id'])
        self.flow.book({**a, 'quantity': 2})
        self.assertEqual(self.flow.state(a['start'], a['end'])['available']['pocket4'], [])

    def test_no_single_unit_available_for_whole_range(self):
        self.book()
        self.flow.book(dict(model='pocket4', quantity=2, student='乙', start='2030-02-03', end='2030-02-04'))
        with self.assertRaises(FlowError):
            self.flow.book(dict(model='pocket4', quantity=1, student='丙', start='2030-02-02', end='2030-02-03'))

    def test_inventory_reduction_cannot_remove_reserved_unit(self):
        self.book()
        self.book()
        with self.assertRaises(FlowError):
            self.flow.inventory({m: 1 for m in MODELS})

    def test_invalid_values(self):
        for count in [-1, 1.5, True, '2']:
            with self.assertRaises(FlowError):
                self.flow.inventory({m: count for m in MODELS})
        for start, end in [('2030-02-31', '2030-03-01'), ('2030-03-01', '2030-02-01')]:
            with self.assertRaises(FlowError):
                self.flow.state(start, end)

    def test_concurrent_reservations_cannot_double_book(self):
        def reserve(_):
            try:
                return self.book()['units'][0]
            except FlowError:
                return None
        with ThreadPoolExecutor(max_workers=5) as pool:
            units = list(pool.map(reserve, range(5)))
        self.assertEqual(sorted(u for u in units if u), ['pocket4-001', 'pocket4-002'])

    def test_corrupt_file_is_not_overwritten(self):
        self.flow.path.write_text('broken', encoding='utf-8')
        with self.assertRaises(FlowError):
            self.book()
        self.assertEqual(self.flow.path.read_text(), 'broken')
