import json
import tempfile
import unittest
from pathlib import Path

from schoolride.store import audit, connect, import_file, report

ROOT = Path(__file__).resolve().parents[1]


class SchoolRideTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = connect(Path(self.temp.name) / 'test.sqlite')
        self.addCleanup(self.db.close)

    def load(self, kind, name):
        return import_file(self.db, kind, ROOT / 'fixtures' / name)

    def decision(self, student, name):
        return report(self.db)['decisions'][f'RUN1/{student}/{name}']['result']

    def test_three_ledgers_and_late_evidence(self):
        self.load('roster', 'roster-ok.json')
        self.load('route', 'route-ok.json')
        self.load('handoff', 'handoff-ok.json')
        self.assertEqual(self.decision('S1', '接领双签是否转移保管责任'), 'guardian')
        self.assertEqual(self.decision('S2', '上车点名是否与名册一致'), '点名')
        self.load('roster', 'roster-bad.json')
        self.assertEqual(self.decision('S1', '代理接领资格是否覆盖放学时段'), '未授权')
        self.assertEqual(self.decision('S2', '临时改站是否取得家长确认'), '待确认')
        self.load('route', 'route-bad.json')
        self.assertEqual(self.decision('S2', '上车点名是否与名册一致'), '待核')
        self.load('handoff', 'handoff-bad.json')
        self.assertEqual(self.decision('S1', '接领双签是否转移保管责任'), 'carrier')
        self.assertEqual(self.decision('S2', '下车位置是否属于批准站点'), '错站或未下车')
        self.assertTrue(audit(self.db)['ok'])
        self.assertEqual(len(report(self.db)['entities']), 3)
        self.assertGreaterEqual(len(report(self.db)['business_records']), 12)

    def test_idempotence_collision_and_atomic_failure(self):
        first = self.load('roster', 'roster-ok.json')
        self.assertFalse(first['idempotent'])
        self.assertTrue(self.load('roster', 'roster-ok.json')['idempotent'])
        original = report(self.db)
        changed = json.loads((ROOT / 'fixtures' / 'roster-ok.json').read_text(encoding='utf-8'))
        changed[0]['students'][0]['guardian'] = 'INTRUDER'
        target = Path(self.temp.name) / 'collision.json'
        target.write_text(json.dumps(changed), encoding='utf-8')
        with self.assertRaises(ValueError):
            import_file(self.db, 'roster', target)
        self.assertEqual(report(self.db), original)

    def test_cancelled_route_preserves_entitlement(self):
        self.load('roster', 'roster-ok.json')
        self.load('route', 'route-ok.json')
        changed = json.loads((ROOT / 'fixtures' / 'route-bad.json').read_text(encoding='utf-8'))
        changed[0]['cancelled'] = True
        changed[0]['id'] = 'V3'
        changed[0]['version'] = 3
        target = Path(self.temp.name) / 'cancelled.json'
        target.write_text(json.dumps(changed), encoding='utf-8')
        self.load('route', 'route-bad.json')
        import_file(self.db, 'route', target)
        decisions = report(self.db)['decisions']
        self.assertEqual(decisions['RUN1/停运后是否保留学生替代接送权益']['result'], '替代接送')
        self.assertEqual(self.decision('S1', '上车点名是否与名册一致'), '待核')

    def test_proxy_revocation_reopens_custody_and_missing_signature(self):
        self.load('roster', 'roster-ok.json')
        self.load('route', 'route-ok.json')
        handoff = json.loads((ROOT / 'fixtures' / 'handoff-ok.json').read_text(encoding='utf-8'))
        handoff[0]['scans'][2]['person'] = 'P2'
        handoff[0]['id'] = 'H-PROXY'
        target = Path(self.temp.name) / 'proxy.json'
        target.write_text(json.dumps(handoff), encoding='utf-8')
        import_file(self.db, 'handoff', target)
        self.assertEqual(self.decision('S1', '接领双签是否转移保管责任'), 'guardian')
        self.load('roster', 'roster-bad.json')
        self.assertEqual(self.decision('S1', '接领双签是否转移保管责任'), 'carrier')
        handoff[0]['id'] = 'H-UNSIGNED'
        handoff[0]['version'] = 3
        handoff[0]['scans'][2]['person'] = 'P1'
        handoff[0]['scans'][2]['receiver_signed'] = False
        target.write_text(json.dumps(handoff), encoding='utf-8')
        import_file(self.db, 'handoff', target)
        self.assertEqual(self.decision('S1', '接领双签是否转移保管责任'), 'carrier')

    def test_entitlement_capacity_inspection_and_stop_edges(self):
        self.load('roster', 'roster-ok.json')
        self.load('route', 'route-ok.json')
        self.load('handoff', 'handoff-ok.json')
        bad_route = json.loads((ROOT / 'fixtures' / 'route-bad.json').read_text(encoding='utf-8'))
        target = Path(self.temp.name) / 'route-edge.json'
        target.write_text(json.dumps(bad_route), encoding='utf-8')
        import_file(self.db, 'route', target)
        decisions = report(self.db)['decisions']
        self.assertEqual(decisions['RUN1/线路绕行是否触及学生停靠站']['result'], '漏站')
        self.assertEqual(decisions['RUN1/座位占用是否超过额定人数']['result'], '候补')
        bad_route[0]['id'], bad_route[0]['version'] = 'V3', 3
        bad_route[0]['inspection_until'] = '2026-11-24T08:00'
        target.write_text(json.dumps(bad_route), encoding='utf-8')
        import_file(self.db, 'route', target)
        decisions = report(self.db)['decisions']
        self.assertEqual(decisions['RUN1/车辆检验是否覆盖全程']['result'], '停运')
        self.assertEqual(decisions['RUN1/停运后是否保留学生替代接送权益']['result'], '替代接送')
        bad_roster = json.loads((ROOT / 'fixtures' / 'roster-bad.json').read_text(encoding='utf-8'))
        bad_roster[0]['students'][0]['route_authorization']['signed'] = False
        target.write_text(json.dumps(bad_roster), encoding='utf-8')
        import_file(self.db, 'roster', target)
        self.assertEqual(self.decision('S1', '学籍时段是否覆盖乘车日'), '无资格')


if __name__ == '__main__':
    unittest.main()
