"""轮胎实装种子测试。

五组覆盖：
1. 当前“每车取头两条”的双占建议（原始特征测试，逐字保留）；
2. 全局至少两套合法实装（方案甲 / 方案乙）与备胎寿命差异；
3. T1 迟到测量补正 3.5 mm：甲失效、乙仍可执行；
4. 质检隔离 + 领出 / 退回实际时间：旧建议里的隔离胎不得放行、退库前占用；
5. 已签收发车锁定：工单重排不能挪胎，证书撤销保留历史 / 去向 / 里程。

另含成对兼容、载重置换反例、rules.py 检验证照闸门与 SQLite 哈希重放 audit。
运行：``python -B -m pytest -q -p no:cacheprovider``
"""
import hashlib
import json
from pathlib import Path

import pytest

from schoolride.tyre_catalog import eligible
from schoolride.axle_plan import suggest
from schoolride import tyre_capture as tc

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'tyre-fitment.json'


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


# ---------------------------------------------------------------------------
# 1. 当前双占建议（原始种子测试，保持逐字断言）
# ---------------------------------------------------------------------------
def test_two_bus_plans_reuse_same_physical_tyre_pair():
    f=json.loads((Path(__file__).parents[1]/'fixtures'/'tyre-fitment.json').read_text())
    assert eligible(f['tyres'][0],f['buses'][0])
    first=suggest(f['buses'][0],f['tyres'])
    second=suggest(f['buses'][1],f['tyres'])
    assert first == {'bus':'BUS-H','front_axle':['T1','T2'],'ready':True}
    assert second == {'bus':'BUS-L','front_axle':['T1','T2'],'ready':True}
    changed=json.loads(json.dumps(f['tyres']))
    changed[0]['tread_mm']=3.5
    assert not eligible(changed[0],f['buses'][0])
    assert suggest(f['buses'][0],changed)['front_axle'] == ['T2','T3']
    assert abs(changed[1]['tread_mm']-changed[2]['tread_mm']) > f['max_same_axle_tread_delta_mm']


def test_seed_fixture_concrete_facts():
    f = load_fixture()
    depths = {t['id']: t['tread_mm'] for t in f['tyres']}
    assert (depths['T1'], depths['T2']) == (5.0, 5.2)
    assert (depths['T3'], depths['T4']) == (7.0, 7.2)
    assert {t['id']: t['load_index'] for t in f['tyres']}['T5'] == 143
    assert f['max_same_axle_tread_delta_mm'] == 0.5
    h, low = f['buses']
    assert (h['id'], h['min_load_index'], h['service_km']) == ('BUS-H', 150, 140)
    assert (low['id'], low['min_load_index'], low['service_km']) == ('BUS-L', 140, 50)


def test_naive_suggestions_are_detected_as_double_booking():
    f = load_fixture()
    suggestions = [suggest(b, f['tyres']) for b in f['buses']]
    doubles = tc.detect_double_bookings(suggestions)
    assert {d['tyre_id']: d['buses'] for d in doubles} == {
        'T1': ['BUS-H', 'BUS-L'],
        'T2': ['BUS-H', 'BUS-L'],
    }
    # 把“头两条”相加并落证必须被全局实物唯一性拒绝，且不留半成品
    ledger = tc.FitmentLedger(f)
    before = dict(ledger.locations)
    with pytest.raises(tc.DoubleBookingError):
        ledger.commit_assignments({'BUS-H': ['T1', 'T2'],
                                   'BUS-L': ['T1', 'T2']})
    assert ledger.certificates == {}
    assert ledger.locations == before


# ---------------------------------------------------------------------------
# 2. 成对兼容 + 两套全局合法实装
# ---------------------------------------------------------------------------
def test_individually_eligible_does_not_mean_axle_pair_legal():
    f = load_fixture()
    h = f['buses'][0]
    by_id = {t['id']: t for t in f['tyres']}

    # T2/T3 单胎都 eligible，但 1.8 mm 差值让它们不能同轴
    assert eligible(by_id['T2'], h) and eligible(by_id['T3'], h)
    bad = tc.axle_check(by_id['T2'], by_id['T3'], h,
                        max_delta_mm=f['max_same_axle_tread_delta_mm'])
    assert not bad['ok']
    assert 'TREAD_DELTA' in bad['codes']
    assert bad['tread_delta_mm'] == 1.8

    # 三对“天然配对”本身都满足 0.5 mm 与花纹
    for left, right in [('T1', 'T2'), ('T3', 'T4'), ('T5', 'T6')]:
        assert tc.axle_check(by_id[left], by_id[right], h,
                             max_delta_mm=0.5)['codes'] != ['TREAD_DELTA'] \
            or left == 'T5'  # T5/T6 对 H 死于载重而非差值
    assert tc.axle_check(by_id['T1'], by_id['T2'], h)['ok']
    assert tc.axle_check(by_id['T3'], by_id['T4'], h)['ok']
    l_bus = f['buses'][1]
    assert tc.axle_check(by_id['T5'], by_id['T6'], l_bus)['ok']


def test_t3_t4_to_bus_l_and_t5_t6_to_bus_h_fails_load_index():
    f = load_fixture()
    swapped = {'BUS-H': ['T5', 'T6'], 'BUS-L': ['T3', 'T4']}
    result = tc.validate_assignment(f, swapped)
    assert not result['ok']
    h_axle = result['axles']['BUS-H']
    assert not h_axle['ok']
    assert {'LOAD_INDEX_A', 'LOAD_INDEX_B'} <= set(h_axle['codes'])
    # 143 < 150 是硬原因；与胎纹差无关（0.1 mm 完全合格）
    assert h_axle['tread_delta_mm'] == 0.1
    assert any(p['code'].startswith('LOAD_INDEX') for p in result['problems'])


def test_global_solver_offers_plan_jia_and_yi():
    f = load_fixture()
    plans = tc.build_plans(f)

    plan_a = tc.get_plan(f, 'A')
    plan_jia = tc.get_plan(f, '甲')
    assert plan_a is plan_jia or plan_a['assignments'] == plan_jia['assignments']
    assert plan_a['assignments'] == {'BUS-H': ['T1', 'T2'],
                                     'BUS-L': ['T5', 'T6']}
    plan_b = tc.get_plan(f, 'B')
    assert plan_b['assignments'] == {'BUS-H': ['T3', 'T4'],
                                     'BUS-L': ['T5', 'T6']}

    # 至少两套被推荐给管理员比较，而不是静默采用仓库顺序
    assert len(plans['recommended']) >= 2
    assert {p['plan_id'] for p in plans['recommended']} == {'A', 'B'}
    assert len(plans['comparison']) >= 2

    # 枚举到的每个完整方案都实体 ID 不重复且每轴合法
    for plan in plans['plans']:
        used = [tid for ids in plan['assignments'].values() for tid in ids]
        assert len(used) == len(set(used))
        assert all(axle['ok'] for axle in plan['axles'].values())


def test_plan_a_and_b_have_different_spare_life_and_next_high_load_stock():
    f = load_fixture()
    a = tc.get_plan(f, 'A')
    b = tc.get_plan(f, 'B')

    assert a['spares'] == ['T3', 'T4']
    assert b['spares'] == ['T1', 'T2']
    assert a['spare_life_mm'] == {'T3': 3.0, 'T4': 3.2}
    assert b['spare_life_mm'] == {'T1': 1.0, 'T2': 1.2}

    # 若夹具给出磨损系数，剩余 km 寿命也不同
    assert a['spare_tyres'][0]['estimated_remaining_km'] == 30000
    assert b['spare_tyres'][0]['estimated_remaining_km'] == 10000

    # 下一次高载重车（LI>=150）的可用备胎组合
    assert a['next_high_load_pairs'] == [['T3', 'T4']]
    assert b['next_high_load_pairs'] == [['T1', 'T2']]
    assert a['next_high_load_ready'] and b['next_high_load_ready']

    delta = tc.diff_plans(a, b)
    changed_positions = {c['position'] for c in delta['assignment_changes']}
    assert changed_positions == {'BUS-H/front/left', 'BUS-H/front/right'}
    assert set(delta['spares_added']) == {'T1', 'T2'}
    assert set(delta['spares_removed']) == {'T3', 'T4'}


def test_commit_plan_a_issues_one_certificate_per_wheel_position():
    f = load_fixture()
    ledger = tc.FitmentLedger(f)
    certs = ledger.commit_plan('A', at='2026-11-25T08:00', signed=True,
                               signed_by='foreman')
    assert len(certs) == 4
    by_position = {c['position_id']: c['tyre_id'] for c in certs}
    assert by_position == {
        'BUS-H/front/left': 'T1',
        'BUS-H/front/right': 'T2',
        'BUS-L/front/left': 'T5',
        'BUS-L/front/right': 'T6',
    }
    # 每个实体 ID 只在一个轮位；深纹 T3/T4 仍在库
    assert ledger.location_of('T1')['kind'] == 'position'
    assert ledger.location_of('T1')['ref'] == 'BUS-H/front/left'
    assert ledger.location_of('T3')['kind'] == 'stock'
    assert set(ledger.available_tyres()) == {'T3', 'T4'}

    # 全局位置唯一性不变量
    located = []
    for tid, loc in ledger.locations.items():
        if loc['kind'] == 'position':
            located.append((tid, loc['ref']))
    tyre_at_positions = [tid for tid, _ in located]
    assert len(tyre_at_positions) == len(set(tyre_at_positions)) == 4

    commitments = ledger.dispatch_commitments()
    assert commitments['releasable_bus_ids'] == ['BUS-H', 'BUS-L']
    # 清单同时兼容“车号列表”和“带明细行”两种读法
    assert ledger.releasable_buses() == ['BUS-H', 'BUS-L']
    assert 'BUS-H' in ledger.releasable_buses()
    assert ledger.releasable_buses()[0]['certificate_ids']


def test_two_ledgers_hold_plan_a_and_b_for_admin_comparison():
    f = load_fixture()
    la = tc.record_fitment(f, 'A', at='2026-11-25T08:00')
    lb = tc.record_fitment(f, 'B', at='2026-11-25T08:05')
    h_a = {c['position_id']: c['tyre_id'] for c in la.certificates.values()
           if c['bus'] == 'BUS-H'}
    h_b = {c['position_id']: c['tyre_id'] for c in lb.certificates.values()
           if c['bus'] == 'BUS-H'}
    assert sorted(h_a.values()) == ['T1', 'T2']
    assert sorted(h_b.values()) == ['T3', 'T4']
    # 两方案 BUS-L 相同，实物证 ID 不同（不同台账各自签发）
    assert sorted(c['tyre_id'] for c in la.certificates.values() if c['bus'] == 'BUS-L') \
        == ['T5', 'T6']
    assert la.dispatch_commitments()['releasable_bus_ids'] == ['BUS-H', 'BUS-L']
    assert lb.dispatch_commitments()['releasable_bus_ids'] == ['BUS-H', 'BUS-L']


# ---------------------------------------------------------------------------
# 3. 迟到测量补正
# ---------------------------------------------------------------------------
def test_late_measurement_t1_3_5_invalidates_a_but_b_survives():
    f = load_fixture()

    # 先复现上游：直接改字典后单胎 eligible 翻转，朴素 suggest 给出 T2/T3
    changed = json.loads(json.dumps(f))
    changed['tyres'][0]['tread_mm'] = 3.5
    h = changed['buses'][0]
    assert not eligible(changed['tyres'][0], h)
    naive = suggest(h, changed['tyres'])['front_axle']
    assert naive == ['T2', 'T3']
    by_id = {t['id']: t for t in changed['tyres']}
    assert 'TREAD_DELTA' in tc.axle_check(
        by_id['T2'], by_id['T3'], h, max_delta_mm=0.5)['codes']

    # 台账方式：迟到复核追加而不是覆盖，原件保留
    ledger = tc.FitmentLedger(f)
    ledger.correct_tread('T1', 3.5, at='2026-11-25T07:30',
                         measurement_id='M-T1-LATE-RECHECK')
    assert [r['tread_mm'] for r in ledger.tyres['T1']['readings']] == [5.0, 3.5]
    assert ledger.tyres['T1']['original_tread_mm'] == 5.0
    assert ledger._tyre_view('T1')['tread_mm'] == 3.5
    assert not tc.individual_eligible(ledger._tyre_view('T1'), h)

    report = ledger.change_report()
    correction = report['measurement_corrections'][0]
    assert (correction['tyre_id'], correction['from_mm'], correction['to_mm']) \
        == ('T1', 5.0, 3.5)
    assert correction['original_reading_preserved'] is True

    validity = report['plan_validity']
    assert validity['A']['executable'] is False
    assert validity['B']['executable'] is True
    codes = set()
    for problem in validity['A']['problems']:
        codes.add(problem['code'])
    assert {'TREAD_DEPTH_A', 'TREAD_DELTA'} <= codes
    assert report['plans_invalidated'] == ['A']
    assert report['plans_still_executable'] == ['B']

    # 甲不能落证；乙仍可完整执行且双车可放行
    with pytest.raises(tc.IncompatibleAxleError):
        ledger.commit_plan('A')
    ledger.commit_plan('B', at='2026-11-25T08:10', signed=True)
    assert ledger.dispatch_commitments()['releasable_bus_ids'] == ['BUS-H', 'BUS-L']


# ---------------------------------------------------------------------------
# 4. 质检隔离 + 领出 / 退回实际时间
# ---------------------------------------------------------------------------
def test_quarantined_tyre_in_old_suggestion_is_not_releasable():
    f = load_fixture()
    # 朴素 eligible 不看隔离位：隔离 T1 后旧建议仍是 T1/T2
    tyres_with_q = [{**t, 'quarantined': t['id'] == 'T1' or t['quarantined']}
                    for t in f['tyres']]
    assert suggest(f['buses'][0], tyres_with_q)['front_axle'] == ['T1', 'T2']

    ledger = tc.FitmentLedger(f)
    ledger.quarantine('T1', reason='胎侧鼓包', qc_id='QC-7',
                     at='2026-11-25T07:00')
    assert 'T1' not in ledger.available_tyres()
    validity = ledger.change_report()['plan_validity']
    assert validity['A']['executable'] is False
    assert 'QUARANTINED_A' in validity['A']['problems'][0]['code'] \
        or any(p['code'] == 'QUARANTINED_A' for p in validity['A']['problems'])
    assert validity['B']['executable'] is True
    with pytest.raises(tc.IncompatibleAxleError):
        ledger.commit_plan('A')


def test_quarantine_after_commit_blocks_release_and_return_keeps_occupancy():
    f = load_fixture()
    ledger = tc.FitmentLedger(f)
    ledger.commit_plan('B', at='2026-11-25T08:00', signed=True)
    assert ledger.dispatch_commitments()['releasable_bus_ids'] == ['BUS-H', 'BUS-L']

    # 旧安装方案 B 中确有 T3；迟到隔离后 BUS-H 不得放行
    assert 'T3' in tc.get_plan(f, 'B')['assignments']['BUS-H']
    ledger.quarantine('T3', reason='X 光复检异常', qc_id='QC-9',
                      at='2026-11-25T08:10')
    commitments = ledger.dispatch_commitments()
    assert commitments['releasable_bus_ids'] == ['BUS-L']
    h_row = next(r for r in commitments['commitments'] if r['bus'] == 'BUS-H')
    assert h_row['status'] == 'held'
    assert any('隔离' in b for b in h_row['blockers'])
    blocked = ledger.try_dispatch_bus('BUS-H', at='2026-11-25T08:20', mileage_km=140)
    assert blocked['dispatched'] is False
    with pytest.raises(tc.DispatchBlocked):
        ledger.dispatch_bus('BUS-H', at='2026-11-25T08:21', mileage_km=140)

    # 撤销该证后，实物退回前 T3 仍在轮位（占用），退回后进入隔离位而非可领用库
    cert_id = ledger.position_cert['BUS-H/front/left']
    ledger.revoke_certificate(cert_id, reason='隔离胎撤销放行',
                              at='2026-11-25T08:25')
    assert ledger.location_of('T3')['kind'] == 'position'
    assert 'T3' not in ledger.available_tyres()
    ledger.return_tyre('T3', at='2026-11-25T09:00', condition='quarantine-bay')
    loc = ledger.location_of('T3')
    assert loc['kind'] == 'quarantine'
    assert 'T3' not in ledger.available_tyres()
    kinds = [m['kind'] for m in ledger.tyre_history('T3')]
    assert {'mount', 'return'} <= set(kinds)
    ret = next(m for m in ledger.tyre_history('T3') if m['kind'] == 'return')
    assert ret['actual_return_at'] == '2026-11-25T09:00'


def test_checkout_and_return_actual_times_preserve_occupancy():
    f = load_fixture()
    ledger = tc.FitmentLedger(f)
    ledger.checkout('T5', work_order='WO-L', at='2026-11-25T07:10', by='storekeeper')
    loc = ledger.location_of('T5')
    assert loc['kind'] == 'checkout' and loc['ref'] == 'WO-L'
    assert 'T5' not in ledger.available_tyres()

    # 同一实体胎不能二次领出
    with pytest.raises(tc.TyreOccupiedError):
        ledger.checkout('T5', work_order='WO-H', at='2026-11-25T07:12')

    # 领出到 WO-OTHER 的胎不能装给 BUS-L（工单 / 车不匹配）
    ledger.checkout('T6', work_order='WO-OTHER', at='2026-11-25T07:15')
    with pytest.raises(tc.TyreOccupiedError):
        ledger.commit_assignments({'BUS-L': ['T5', 'T6']})

    # 实际退回时间留痕；退库后重新可用，领出时间原件保留
    ledger.return_tyre('T5', at='2026-11-25T07:40')
    ledger.return_tyre('T6', at='2026-11-25T07:42')
    assert ledger.location_of('T5')['kind'] == 'stock'
    assert set(ledger.available_tyres()) >= {'T5', 'T6'}
    t5_moves = ledger.tyre_history('T5')
    checkout = next(m for m in t5_moves if m['kind'] == 'checkout')
    returned = next(m for m in t5_moves if m['kind'] == 'return')
    assert checkout['actual_checkout_at'] == '2026-11-25T07:10'
    assert returned['actual_return_at'] == '2026-11-25T07:40'


# ---------------------------------------------------------------------------
# 5. 已发车锁定 + 证书撤销历史
# ---------------------------------------------------------------------------
def test_dispatched_positions_locked_through_work_order_reorder():
    f = load_fixture()
    ledger = tc.FitmentLedger(f)
    ledger.commit_plan('A', at='2026-11-25T08:00', signed=True)
    trip = ledger.dispatch_bus('BUS-H', at='2026-11-25T08:30',
                               run_id='RUN-H-AM', mileage_km=140)
    assert trip['mileage_km'] == 140
    locked_before = {p: ledger.certificates[cid]['tyre_id']
                     for p, cid in ledger.position_cert.items()}
    assert locked_before['BUS-H/front/left'] == 'T1'

    # 后来工单排序变成 L 先 H 后：锁定不得移动
    reorder = ledger.reorder_work_orders(['WO-L', 'WO-H'],
                                         at='2026-11-25T09:00')
    assert reorder['locked_preserved'] is True
    assert reorder['moved_positions'] == []
    assert ledger.work_order_order == ['BUS-L', 'BUS-H']

    # 重排后的全局组配仍把 T1/T2 固定在 BUS-H
    for plan in ledger.plans()['plans']:
        assert plan['assignments']['BUS-H'] == ['T1', 'T2']

    # 任何试图把 T1/T2 改给 BUS-L、或把 B 方案强压给 H 的提交都被拒绝
    with pytest.raises(tc.LockedPositionError):
        ledger.commit_assignments({'BUS-L': ['T1', 'T2']},
                                  at='2026-11-25T09:05')
    with pytest.raises(tc.LockedPositionError):
        ledger.commit_plan('B', at='2026-11-25T09:06')

    # 拒绝后原证原样
    after = {p: ledger.certificates[cid]['tyre_id']
             for p, cid in ledger.position_cert.items()}
    assert after == locked_before
    h_row = next(r for r in ledger.dispatch_commitments()['commitments']
                 if r['bus'] == 'BUS-H')
    assert h_row['status'] == 'dispatched'


def test_revoked_certificate_preserves_position_whereabouts_trip_mileage():
    f = load_fixture()
    ledger = tc.FitmentLedger(f)
    ledger.commit_plan('A', at='2026-11-25T08:00', signed=True)
    ledger.dispatch_bus('BUS-H', at='2026-11-25T08:30',
                        run_id='RUN-H-AM', trip_id='TRIP-9', mileage_km=140)

    cert_id = ledger.position_cert['BUS-H/front/left']
    ledger.revoke_certificate(cert_id, reason='补丁档案不符',
                              at='2026-11-25T11:00')
    cert = ledger.certificates[cert_id]
    assert cert['status'] == 'revoked'
    # 历史轮位、实物、车次、已跑里程都不抹掉
    assert cert['position_id'] == 'BUS-H/front/left'
    assert cert['tyre_id'] == 'T1'
    assert cert['run_id'] == 'RUN-H-AM'
    assert cert['trip_id'] == 'TRIP-9'
    assert cert['mileage_km'] == 140
    actions = [h['action'] for h in cert['history']]
    assert {'commit', 'dispatch_locked', 'revoke'} <= set(actions)

    report = ledger.change_report()
    assert len(report['revocations']) == 1
    assert report['release']['held'] == ['BUS-H']
    assert report['release']['dispatched'] == []

    # 受影响车次与里程仍在台账
    assert len(ledger.trips) == 1
    assert ledger.trips[0]['mileage_km'] == 140
    assert sum(t['mileage_km'] or 0 for t in ledger.trips) == 140

    # 实物退回后去向（库位移动链）保留，撤销证不删除
    ledger.return_tyre('T1', at='2026-11-25T11:20')
    assert ledger.location_of('T1')['kind'] == 'stock'
    assert cert_id in ledger.certificates
    assert 'BUS-H/front/left' not in ledger.position_cert
    assert [m['kind'] for m in ledger.tyre_history('T1')].count('return') == 1


# ---------------------------------------------------------------------------
# schoolride/rules.py 检验、证照闸门
# ---------------------------------------------------------------------------
def test_rules_py_inspection_and_certificates_gate_release():
    f = load_fixture()
    clearance = tc.rules_clearance(f)
    assert clearance['BUS-H']['status'] == 'valid'
    assert clearance['BUS-L']['clear'] is True

    ledger = tc.FitmentLedger(f)
    ledger.commit_plan('A', signed=True)
    assert ledger.dispatch_commitments()['releasable_bus_ids'] == ['BUS-H', 'BUS-L']

    expired = json.loads(json.dumps(f))
    for ev in expired['route_evidence']:
        if ev['bus'] == 'BUS-H':
            ev['inspection_until'] = '2026-11-24T00:00'  # 早于 service_end
    bad = tc.FitmentLedger(expired)
    bad.commit_plan('A', signed=True)
    bad_clearance = tc.rules_clearance(bad)
    assert bad_clearance['BUS-H']['status'] == 'invalid'
    assert bad_clearance['BUS-L']['status'] == 'valid'
    commitments = bad.dispatch_commitments()
    assert commitments['releasable_bus_ids'] == ['BUS-L']
    h_row = next(r for r in commitments['commitments'] if r['bus'] == 'BUS-H')
    assert any('rules.py' in b for b in h_row['blockers'])
    with pytest.raises(tc.DispatchBlocked):
        bad.dispatch_bus('BUS-H', mileage_km=140)


# ---------------------------------------------------------------------------
# SQLite 来源哈希 / 重放 / audit 可复算
# ---------------------------------------------------------------------------
def test_sqlite_source_hash_replay_and_audit(tmp_path):
    db_path = tmp_path / 'tyre.sqlite'
    store = tc.TyreStore(str(db_path))

    info = store.import_source(FIXTURE)
    raw = FIXTURE.read_bytes()
    assert info['sha'] == hashlib.sha256(raw).hexdigest()
    assert info['idempotent'] is False
    again = store.import_source(FIXTURE)
    assert again['idempotent'] is True

    events_path = tmp_path / 'tyre-events.json'
    events_path.write_text(json.dumps([
        {'id': 'EV-M1', 'kind': 'measurement', 'tyre_id': 'T1',
         'tread_mm': 3.5, 'at': '2026-11-25T07:30'},
        {'id': 'EV-C1', 'kind': 'commit_plan', 'plan_id': 'B',
         'at': '2026-11-25T08:00', 'signed': True},
        {'id': 'EV-D1', 'kind': 'dispatch', 'bus': 'BUS-H',
         'at': '2026-11-25T08:30', 'run_id': 'RUN-H-AM', 'mileage_km': 140},
    ]), encoding='utf-8')
    imported = store.import_source(events_path)
    assert imported['inserted_events'] == 3

    report = store.report()
    assert len(report['certificate_rows']) == 4
    assert report['snapshot']['commitments']['dispatched'] == ['BUS-H']
    assert report['snapshot']['tyres']['T1']['tread_mm'] == 3.5

    result = store.audit()
    assert result['ok'] is True
    assert result['source_count'] == 2

    # 同 event_id 异内容：整体回滚，audit 仍可复算
    collision = tmp_path / 'collision.json'
    collision.write_text(json.dumps([
        {'id': 'EV-M1', 'kind': 'measurement', 'tyre_id': 'T1',
         'tread_mm': 9.9},
    ]), encoding='utf-8')
    with pytest.raises(ValueError):
        store.import_source(collision)
    assert store.audit()['ok'] is True
    store.close()

    # 直接用模块级 audit / connect 对文件库再算一次
    db = tc.connect(str(db_path))
    try:
        assert tc.audit(db)['ok'] is True
    finally:
        db.close()


def test_module_level_release_and_report_carry_certificates():
    f = load_fixture()
    # 直接对裸夹具取可放行车清单：auto 落甲方案，双车放行
    rows = tc.releasable_buses(f)
    assert rows == ['BUS-H', 'BUS-L']
    report = tc.build_report(f)
    assert report['selected_plan'] == 'A'
    assert len(report['certificates']) == 4
    assert report['releasable_buses'] == ['BUS-H', 'BUS-L']
    assert tc.get_plan(f, '甲')['spares'] == ['T3', 'T4']


def test_seed_style_mutation_flows_through_ledger_and_fixture_diff():
    f = load_fixture()
    mutated = json.loads(json.dumps(f))
    mutated['tyres'][0]['tread_mm'] = 3.5  # 与种子测试同一种补正写法

    ledger = tc.FitmentLedger(mutated)
    assert ledger._tyre_view('T1')['tread_mm'] == 3.5
    assert ledger.tyres['T1']['original_tread_mm'] == 5.0
    assert [r['tread_mm'] for r in ledger.tyres['T1']['readings']] == [5.0, 3.5]

    # auto 改选乙方案，双车仍可放行
    assert tc.releasable_buses(mutated) == ['BUS-H', 'BUS-L']
    assert tc.build_report(mutated)['selected_plan'] == 'B'

    diff = tc.change_report(f, mutated)
    assert [c['tyre_id'] for c in diff['measurement_corrections']] == ['T1']
    assert diff['plans_invalidated'] == ['A']
    assert diff['plans_still_executable'] == ['B']


def test_existing_schoolride_sqlite_audit_still_recomputes(tmp_path):
    """新模块不破坏既有 roster/route/handoff 的哈希重放 audit。"""
    from schoolride.store import connect, import_file, audit
    from pathlib import Path as P
    root = P(__file__).resolve().parents[1]
    db = connect(tmp_path / 'school.sqlite')
    import_file(db, 'roster', root / 'fixtures' / 'roster-ok.json')
    import_file(db, 'route', root / 'fixtures' / 'route-ok.json')
    import_file(db, 'handoff', root / 'fixtures' / 'handoff-ok.json')
    assert audit(db)['ok'] is True
    db.close()
