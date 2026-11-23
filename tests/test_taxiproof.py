import json
import tempfile
import unittest
from pathlib import Path

from taxiproof.store import audit, connect, import_file, report

ROOT = Path(__file__).resolve().parents[1]


class TaxiProofTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = connect(Path(self.temp.name) / 'state.sqlite')
        self.addCleanup(self.db.close)

    def load(self, kind, name):
        return import_file(self.db, kind, ROOT / 'fixtures' / name)

    def result(self, name):
        return report(self.db)['decisions']['TRIP-1/' + name]['result']

    def test_fare_shift_and_late_evidence(self):
        for kind, name in [('meter', 'meter-ok.json'), ('shift', 'shift-ok.json'),
                           ('trip', 'trip-ok.json')]:
            self.load(kind, name)
        self.assertEqual(self.result('跨费率时段如何分段计费'), '31.00')
        self.assertEqual(self.result('候车时长是否据可信时间戳计价'), '4.00')
        self.assertEqual(self.result('多收款是否形成退款债务'), '9.00')
        self.load('meter', 'meter-bad.json')
        self.assertEqual(self.result('同版费率冲突是否冻结计费'), '冻结')
        self.assertEqual(self.result('计价器校准是否覆盖起终点'), '失准')
        self.load('shift', 'shift-bad.json')
        self.assertEqual(self.result('交班后旧司机能否继续接单'), '禁止')
        self.load('trip', 'trip-bad.json')
        self.assertEqual(self.result('轨迹缺口是否阻止确定里程'), '阻止')
        self.assertEqual(self.result('同车班次重叠由谁承运'), '申诉重开')
        self.assertEqual(self.result('多收款是否形成退款债务'), '待核')
        self.assertTrue(audit(self.db)['ok'])
        self.assertEqual(len(report(self.db)['entities']), 3)

    def test_region_certificate_gap_and_overlap(self):
        self.load('meter', 'meter-ok.json')
        self.load('shift', 'shift-ok.json')
        self.load('trip', 'trip-ok.json')
        shift = json.loads((ROOT / 'fixtures' / 'shift-bad.json').read_text(encoding='utf-8'))
        shift[0]['handover'] = {}
        shift[0]['assignments'][0]['license_until'] = '2026-11-24T09:00'
        shift[0]['assignments'][0]['regions'] = ['OUTER']
        path = Path(self.temp.name) / 'bad-shift.json'
        path.write_text(json.dumps(shift), encoding='utf-8')
        import_file(self.db, 'shift', path)
        self.assertEqual(self.result('司机证书是否覆盖班次'), '无资质')
        self.assertEqual(self.result('同车班次重叠由谁承运'), '争议')
        self.assertEqual(self.result('越区路线是否触发权限争议'), '越区')
        self.assertEqual(self.result('跨费率时段如何分段计费'), '待核')

    def test_idempotence_and_atomic_collision(self):
        self.load('meter', 'meter-ok.json')
        self.assertTrue(self.load('meter', 'meter-ok.json')['idempotent'])
        original = report(self.db)
        changed = json.loads((ROOT / 'fixtures' / 'meter-ok.json').read_text(encoding='utf-8'))
        changed[0]['rates'][0]['per_km'] = 999
        path = Path(self.temp.name) / 'collision.json'
        path.write_text(json.dumps(changed), encoding='utf-8')
        with self.assertRaises(ValueError):
            import_file(self.db, 'meter', path)
        self.assertEqual(report(self.db), original)

    def test_untrusted_wait_time_does_not_charge(self):
        self.load('meter', 'meter-ok.json')
        self.load('shift', 'shift-ok.json')
        trip = json.loads((ROOT / 'fixtures' / 'trip-ok.json').read_text(encoding='utf-8'))
        trip[0]['segments'][1]['timestamp_trusted'] = False
        target = Path(self.temp.name) / 'untrusted.json'
        target.write_text(json.dumps(trip), encoding='utf-8')
        import_file(self.db, 'trip', target)
        self.assertEqual(self.result('候车时长是否据可信时间戳计价'), '0.00')
        self.assertEqual(self.result('跨费率时段如何分段计费'), '27.00')
        self.assertEqual(self.result('多收款是否形成退款债务'), '13.00')

    def test_signed_driver_complaint_reopens_shift_and_later_review_recovers(self):
        for kind, name in [('meter', 'meter-ok.json'), ('shift', 'shift-ok.json'),
                           ('trip', 'trip-ok.json')]:
            self.load(kind, name)
        baseline = {key: value['result'] for key, value in report(self.db)['decisions'].items()}
        self.assertEqual(self.result('同车班次重叠由谁承运'), '无重叠')
        self.assertEqual(self.result('多收款是否形成退款债务'), '9.00')
        trip = json.loads((ROOT / 'fixtures' / 'trip-ok.json').read_text(encoding='utf-8'))
        trip[0].update(id='T-UNSIGNED', version=2, effective_at='2026-11-24T10:00')
        trip[0]['complaint'] = {'reason': 'driver_identity', 'vehicle': 'TAXI-1',
                                'observed_driver': 'D2', 'observed_at': '2026-11-24T09:05',
                                'passenger_signature': 'P1'}
        target = Path(self.temp.name) / 'complaint.json'
        target.write_text(json.dumps(trip), encoding='utf-8')
        import_file(self.db, 'trip', target)
        self.assertEqual({key: value['result'] for key, value in report(self.db)['decisions'].items()}, baseline)
        trip[0].update(id='T-DISPUTE', version=3, effective_at='2026-11-24T10:10')
        trip[0]['complaint']['reviewer_signature'] = 'R1'
        target.write_text(json.dumps(trip), encoding='utf-8')
        import_file(self.db, 'trip', target)
        self.assertEqual(self.result('同车班次重叠由谁承运'), '申诉重开')
        self.assertEqual(self.result('交班后旧司机能否继续接单'), '待核')
        self.assertEqual(self.result('跨费率时段如何分段计费'), '待核')
        self.assertEqual(self.result('多收款是否形成退款债务'), '待核')
        state = {e['subsystem']: e['state'] for e in report(self.db)['entities']}
        self.assertEqual(state['司机车辆营运班次'], '争议')
        self.assertEqual(state['行程价款与申诉'], '待核')
        trip[0].update(id='T-REVIEW-CLEARED', version=4, effective_at='2026-11-24T10:20')
        trip[0]['complaint'] = None
        target.write_text(json.dumps(trip), encoding='utf-8')
        import_file(self.db, 'trip', target)
        self.assertEqual({key: value['result'] for key, value in report(self.db)['decisions'].items()}, baseline)
        self.assertTrue(audit(self.db)['ok'])
        self.assertGreater(len(report(self.db)['decision_history']), len(baseline))


if __name__ == '__main__':
    unittest.main()
