"""Local equipment scheduling with atomic persistence and per-unit conflict checks."""
import json
import threading
import uuid
from datetime import date
from pathlib import Path

MODELS = ('pocket4', 'action6', 'nano', 'osmo360')


class FlowError(ValueError):
    pass


def date_range(start, end):
    try:
        a, b = date.fromisoformat(start), date.fromisoformat(end)
    except (ValueError, TypeError):
        raise FlowError('请选择有效的开始和结束日期')
    if a > b:
        raise FlowError('结束日期不能早于开始日期')
    return a.isoformat(), b.isoformat()


def quantity(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 500:
        raise FlowError('设备数量必须为 0–500 的整数')
    return value


def overlaps(booking, start, end):
    """None means no end date, not a distant placeholder date."""
    booking_end = booking.get('end')
    if booking_end is not None and booking_end < booking['start']:
        return False  # a future booking cancelled before it ever started
    return ((end is None or booking['start'] <= end)
            and (booking_end is None or booking_end >= start))


class DeviceFlow:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def load(self):
        if not self.path.exists():
            return {'inventory': {m: 0 for m in MODELS}, 'bookings': []}
        try:
            return json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise FlowError('设备记录读取失败，请保留数据文件并检查，避免覆盖原记录')

    def save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.path)

    @staticmethod
    def units(model, count):
        return [f'{model}-{n:03d}' for n in range(1, count + 1)]

    def available(self, data, start, end, exclude=''):
        busy = {unit for b in data['bookings'] if b['id'] != exclude
                and overlaps(b, start, end) for unit in b['units']}
        return {m: [u for u in self.units(m, data['inventory'][m]) if u not in busy] for m in MODELS}

    def state(self, start, end, long_term=False):
        if not isinstance(long_term, bool):
            raise FlowError('长期流转选项必须为是或否')
        if long_term:
            start, _ = date_range(start, start)
            end = None
        else:
            start, end = date_range(start, end)
        with self.lock:
            data = self.load()
            return {**data, 'available': self.available(data, start, end), 'start': start, 'end': end, 'long_term': long_term}

    def inventory(self, values):
        if not isinstance(values, dict) or set(values) != set(MODELS):
            raise FlowError('请填写四种设备的数量')
        counts = {m: quantity(values[m]) for m in MODELS}
        with self.lock:
            data = self.load()
            retained = {u for m in MODELS for u in self.units(m, counts[m])}
            for b in data['bookings']:
                if overlaps(b, date.today().isoformat(), None) and any(u not in retained for u in b['units']):
                    raise FlowError('减少的设备仍有当前或未来流转安排，请先修改或取消相关安排')
            data['inventory'] = counts
            self.save(data)

    def book(self, payload):
        long_term = payload.get('long_term', False)
        if not isinstance(long_term, bool):
            raise FlowError('长期流转选项必须为是或否')
        if long_term:
            start, _ = date_range(payload.get('start'), payload.get('start'))
            end = None
        else:
            start, end = date_range(payload.get('start'), payload.get('end'))
        student = str(payload.get('student', '')).strip()
        note = str(payload.get('note', '')).strip()
        model = payload.get('model')
        count = quantity(payload.get('quantity'))
        if not student or len(student) > 80 or len(note) > 500:
            raise FlowError('请填写同学姓名（最多80字），备注最多500字')
        if model not in MODELS or count < 1:
            raise FlowError('请选择设备型号及至少一台设备')
        booking_id = str(payload.get('id') or '')
        with self.lock:
            data = self.load()
            old = next((b for b in data['bookings'] if b['id'] == booking_id), None)
            if booking_id and not old:
                raise FlowError('这条流转记录已不存在，请刷新后重试')
            if old and old.get('cancelled_on'):
                raise FlowError('已取消的长期流转记录保留作历史，不能直接修改，请新建安排')
            free = self.available(data, start, end, booking_id)[model]
            # Prefer retaining the original unit identities when editing.
            preferred = old['units'] if old and old['model'] == model else []
            free = [u for u in preferred if u in free] + [u for u in free if u not in preferred]
            if len(free) < count:
                raise FlowError(f'{model} 在所选日期内仅有 {len(free)} 台全程空闲，无法安排 {count} 台')
            record = {'id': booking_id or uuid.uuid4().hex, 'model': model, 'quantity': count,
                      'units': free[:count], 'student': student, 'start': start, 'end': end,
                      'long_term': long_term, 'note': note}
            data['bookings'] = [b for b in data['bookings'] if b['id'] != booking_id] + [record]
            self.save(data)
            return record

    def cancel_long_term(self, booking_id, today=None):
        """Release from the manual cancellation day while retaining past calendar history."""
        today = today or date.today()
        with self.lock:
            data = self.load()
            record = next((b for b in data['bookings'] if b['id'] == booking_id), None)
            if record is None:
                raise FlowError('这条流转记录已不存在')
            if not record.get('long_term'):
                raise FlowError('这不是长期流转，请使用普通安排的取消操作')
            if record.get('cancelled_on'):
                return record
            record['end'] = date.fromordinal(today.toordinal() - 1).isoformat()
            record['cancelled_on'] = today.isoformat()
            self.save(data)
            return record

    def delete(self, booking_id):
        with self.lock:
            data = self.load()
            if not any(b['id'] == booking_id for b in data['bookings']):
                raise FlowError('这条流转记录已不存在')
            if any(b['id'] == booking_id and b.get('long_term') for b in data['bookings']):
                raise FlowError('长期流转请使用“取消长期流转”，以保留历史记录')
            data['bookings'] = [b for b in data['bookings'] if b['id'] != booking_id]
            self.save(data)
