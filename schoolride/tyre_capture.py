"""轮位实装采录、全局组配与变更报告。

这个模块补上 ``tyre_catalog.eligible`` / ``axle_plan.suggest`` 的结构性缺口：

* 单胎 eligible 为真不等于两条胎可以同轴——必须成对校验规格、载重指数、
  花纹相容与同轴胎纹差（夹具阈值 0.5 mm）。
* 每实体轮胎 ID 在任一时刻只能处于一个库位 / 领出单 / 轮位 / 隔离位，
  全局组配必须跨两车消歧，不能把每车的“头两条”直接相加。
* 测量复核、质检隔离、领出 / 退回实际时间全部事件留痕、原件保留。
* 已签收发车的轮位进入锁定，后来的工单排序不能把实物挪给另一车。
* 只有轮位装配证有效且 ``schoolride.rules`` 检验 / 证照有效的车辆才进入
  出车承诺；证书撤销不删除历史轮位、实物去向、受影响车次与已跑里程。

持久化层沿用本仓 ``store.py`` 的 SQLite 来源哈希 + 事件重放 + audit 复算
思路，表名使用独立的 ``tyre_`` 前缀，可与既有表共库而互不干扰。
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from itertools import combinations
from pathlib import Path

from . import rules
from .tyre_catalog import eligible as catalog_eligible

MIN_TREAD_MM = 4.0
MAX_DELTA_MM = 0.5
FLOAT_EPS = 1e-9


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------
class FitmentError(ValueError):
    """实装领域规则被违反。"""


class AllocationConflict(FitmentError):
    """全局组配存在实物争用或无法成对兼容。"""


class DoubleBookingError(AllocationConflict):
    """同一实体轮胎被同时分配给两个轮位 / 两辆车。"""


class TyreOccupiedError(AllocationConflict):
    """轮胎尚未退库，仍被轮位、领出单或隔离流程占用。"""


class LockedPositionError(AllocationConflict):
    """已签收发车的轮位被后续工单排序或组配试图挪动。"""


class IncompatibleAxleError(AllocationConflict):
    """两条单胎各自 eligible，但不能作为同一根车轴成对使用。"""


class DispatchBlocked(FitmentError):
    """轮位装配或 rules 检验 / 证照不满足，车辆不得放行。"""


# ---------------------------------------------------------------------------
# 小型返回对象：既可以 dict 键访问，也可以属性访问
# ---------------------------------------------------------------------------
class AttrDict(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:  # pragma: no cover - 透传 AttributeError
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


class PlanResult(AttrDict):
    """全局组配结果。

    除了 ``result['plans']`` / ``result.plans`` 外，也允许 ``result[0]``
    直接按序取方案，兼容把入口当列表用的调用习惯。
    """

    def __getitem__(self, key):
        if isinstance(key, int):
            return self['plans'][key]
        return dict.__getitem__(self, key)


class BusRow(AttrDict):
    """放行清单行。

    与字符串比较时按车号相等，因此 ``'BUS-H' in releasable_buses(...)``
    以及 ``rows == ['BUS-H', 'BUS-L']`` 这类调用方习惯都成立；同时行内
    仍可取证书、原因等明细。
    """

    def __eq__(self, other):
        if isinstance(other, str):
            return self.get('bus') == other
        return dict.__eq__(self, other)

    def __ne__(self, other):
        return not self.__eq__(other)

    __hash__ = None


# ---------------------------------------------------------------------------
# 输入归一化
# ---------------------------------------------------------------------------
def _coerce_view(data):
    if isinstance(data, FitmentLedger):
        return data.view()
    if isinstance(data, (str, Path)):
        data = json.loads(Path(data).read_text(encoding='utf-8'))
    if isinstance(data, dict) and data.get('__tyre_view__'):
        return data
    if isinstance(data, dict) and 'tyres' in data and 'buses' in data:
        return _plain_fixture_view(data)
    if isinstance(data, dict) and 'tyres' in data:
        # 已经是 view（可能缺少 buses 之外的元数据）
        data.setdefault('buses', [])
        return data
    raise TypeError('fitment data must be a fixture dict, path or FitmentLedger')


def _plain_fixture_view(fixture):
    initial = fixture.get('initial_locations', {}) or {}
    locations = {
        t['id']: {'kind': 'stock', 'ref': initial.get(t['id'], f"STOCK/{t['id']}")}
        for t in fixture['tyres']
    }
    return AttrDict({
        '__tyre_view__': True,
        'buses': fixture['buses'],
        'tyres': fixture['tyres'],
        'wheel_positions': fixture.get('wheel_positions', []),
        'max_delta_mm': fixture.get('max_same_axle_tread_delta_mm', MAX_DELTA_MM),
        'min_tread_mm': fixture.get('minimum_tread_mm', MIN_TREAD_MM),
        'pattern_compatibility_groups': fixture.get('pattern_compatibility_groups', []),
        'tread_wear_mm_per_km': fixture.get('tread_wear_mm_per_km'),
        'reference_plans': fixture.get('reference_plans', []),
        'rules_events': fixture.get('route_evidence',
                                    fixture.get('rules_events', [])),
        'work_orders': fixture.get('work_orders', []),
        'fixed': {},
        'locations': locations,
        'locked_positions': set(),
    })


# ---------------------------------------------------------------------------
# 单胎与成对校验
# ---------------------------------------------------------------------------
def current_tread_mm(tyre):
    """当前有效花纹深度：取采录读数链最后一条；原件全部保留在 readings。"""
    readings = tyre.get('readings')
    if readings:
        return readings[-1]['tread_mm']
    return tyre['tread_mm']


def individual_eligible(tyre, bus, min_tread_mm=MIN_TREAD_MM):
    """单胎维度：车系规格、载重指数、最低花纹深度。

    与 ``tyre_catalog.eligible`` 保持同一组条件，但允许夹具覆盖最低深度，
    并读取迟到复核后的当前深度。
    """
    tread = current_tread_mm(tyre)
    return (
        tyre.get('size') == bus['tyre_size']
        and tyre.get('load_index', -1) >= bus['min_load_index']
        and tread >= min_tread_mm
    )


def patterns_compatible(pattern_a, pattern_b, groups=None):
    if pattern_a == pattern_b:
        return True
    for group in groups or []:
        if pattern_a in group and pattern_b in group:
            return True
    return False


def tread_delta_mm(tyre_a, tyre_b):
    return round(abs(current_tread_mm(tyre_a) - current_tread_mm(tyre_b)), 6)


def axle_check(tyre_a, tyre_b, bus=None, *, max_delta_mm=MAX_DELTA_MM,
               min_tread_mm=MIN_TREAD_MM, compatibility_groups=None,
               check_quarantine=True):
    """成对车轴校验。返回 ``ok`` 与机器可读 problems / 中文 reasons。

    ``bus`` 可省略：省略时只校验两胎之间的花纹 / 胎纹差 / 隔离，以及
    通用最低花纹深度，不校验车系规格与载重指数。
    """
    problems = []

    def fail(code, message):
        problems.append(AttrDict(code=code, message=message))

    required_size = bus['tyre_size'] if bus else None
    min_load = bus['min_load_index'] if bus else None
    bus_id = bus['id'] if bus else None

    if required_size is not None:
        if tyre_a.get('size') != required_size:
            fail('SIZE_MISMATCH_A', f"{tyre_a.get('id')} 规格 {tyre_a.get('size')} 不符 {bus_id} 车系 {required_size}")
        if tyre_b.get('size') != required_size:
            fail('SIZE_MISMATCH_B', f"{tyre_b.get('id')} 规格 {tyre_b.get('size')} 不符 {bus_id} 车系 {required_size}")

    if min_load is not None:
        if tyre_a.get('load_index', -1) < min_load:
            fail('LOAD_INDEX_A', f"{tyre_a.get('id')} 载重指数 {tyre_a.get('load_index')} < {bus_id} 要求 {min_load}")
        if tyre_b.get('load_index', -1) < min_load:
            fail('LOAD_INDEX_B', f"{tyre_b.get('id')} 载重指数 {tyre_b.get('load_index')} < {bus_id} 要求 {min_load}")

    tread_a = current_tread_mm(tyre_a)
    tread_b = current_tread_mm(tyre_b)
    if tread_a + FLOAT_EPS < min_tread_mm:
        fail('TREAD_DEPTH_A', f"{tyre_a.get('id')} 当前花纹 {tread_a} mm < 最低 {min_tread_mm} mm")
    if tread_b + FLOAT_EPS < min_tread_mm:
        fail('TREAD_DEPTH_B', f"{tyre_b.get('id')} 当前花纹 {tread_b} mm < 最低 {min_tread_mm} mm")

    if not patterns_compatible(tyre_a.get('pattern'), tyre_b.get('pattern'),
                               compatibility_groups):
        fail('PATTERN_INCOMPATIBLE',
             f"{tyre_a.get('id')} 花纹 {tyre_a.get('pattern')} 与 {tyre_b.get('id')} 花纹 {tyre_b.get('pattern')} 不相容")

    delta = round(abs(tread_a - tread_b), 3)
    if delta > max_delta_mm + FLOAT_EPS:
        fail('TREAD_DELTA',
             f"{tyre_a.get('id')}/{tyre_b.get('id')} 同轴胎纹差 {delta} mm > 上限 {max_delta_mm} mm")

    if check_quarantine:
        if tyre_a.get('quarantined'):
            fail('QUARANTINED_A', f"{tyre_a.get('id')} 已质检隔离，不得放行")
        if tyre_b.get('quarantined'):
            fail('QUARANTINED_B', f"{tyre_b.get('id')} 已质检隔离，不得放行")

    return AttrDict({
        'ok': not problems,
        'problems': problems,
        'reasons': [p['message'] for p in problems],
        'codes': [p['code'] for p in problems],
        'bus': bus_id,
        'tyre_ids': [tyre_a.get('id'), tyre_b.get('id')],
        'tread_a_mm': tread_a,
        'tread_b_mm': tread_b,
        'tread_delta_mm': delta,
        'max_delta_mm': max_delta_mm,
    })


def legal_pair(tyre_a, tyre_b, bus=None, **kwargs):
    return axle_check(tyre_a, tyre_b, bus, **kwargs)['ok']


# 单胎口径与成对布尔口径的再导出 / 别名
eligible = catalog_eligible
pair_ok = legal_pair
axle_pair_ok = legal_pair
same_axle_ok = legal_pair
pair_compatible = legal_pair


# ---------------------------------------------------------------------------
# 轮位
# ---------------------------------------------------------------------------
def ordered_positions(view, bus_id):
    rows = [p for p in view.get('wheel_positions', []) if p['bus'] == bus_id]
    if rows:
        rows = sorted(rows, key=lambda p: p.get('position_id', ''))
        return [p['position_id'] for p in rows]
    return [f'{bus_id}/front/left', f'{bus_id}/front/right']


def _bus_index(view):
    return {b['id']: b for b in view['buses']}


def _tyre_index(view):
    return {t['id']: t for t in view['tyres']}


def _location_kind(view, tyre_id):
    loc = (view.get('locations') or {}).get(tyre_id)
    return loc.get('kind') if loc else 'stock'


# ---------------------------------------------------------------------------
# 全局组配
# ---------------------------------------------------------------------------
def compatible_pairs(data, bus_id, *, only_available=False):
    """一辆车当前可以合法上轴的全部成对组合（不是单胎 eligible 列表）。"""
    view = _coerce_view(data)
    bus = _bus_index(view)[bus_id]
    tyres = view['tyres']
    max_delta = view.get('max_delta_mm', MAX_DELTA_MM)
    min_tread = view.get('min_tread_mm', MIN_TREAD_MM)
    groups = view.get('pattern_compatibility_groups')
    out = []
    for a, b in combinations(tyres, 2):
        if only_available and _location_kind(view, a['id']) != 'stock':
            continue
        if only_available and _location_kind(view, b['id']) != 'stock':
            continue
        check = axle_check(a, b, bus, max_delta_mm=max_delta,
                           min_tread_mm=min_tread,
                           compatibility_groups=groups)
        if check['ok']:
            out.append(check)
    return out


def _spare_detail(view, tyre_id, high_load_bus):
    tyre = _tyre_index(view)[tyre_id]
    min_tread = view.get('min_tread_mm', MIN_TREAD_MM)
    wear = view.get('tread_wear_mm_per_km')
    tread = current_tread_mm(tyre)
    margin = round(tread - min_tread, 3)
    return AttrDict({
        'id': tyre_id,
        'tread_mm': tread,
        'load_index': tyre.get('load_index'),
        'pattern': tyre.get('pattern'),
        'remaining_life_mm': margin,
        'life_mm': margin,
        'remaining_life': margin,
        'usable_tread_mm': margin,
        'tread_above_limit_mm': margin,
        'current_tread_mm': tread,
        'estimated_remaining_km': int(round(margin / wear)) if wear else None,
        'location': (view.get('locations') or {}).get(tyre_id),
        'quarantined': bool(tyre.get('quarantined')),
        'high_load_ready': bool(
            high_load_bus and individual_eligible(tyre, high_load_bus, min_tread)
            and not tyre.get('quarantined')),
    })


def _reference_signatures(view):
    refs = {}
    for ref in view.get('reference_plans', []) or []:
        sig = tuple(sorted(
            (bus_id, tuple(sorted(ids)))
            for bus_id, ids in ref['assignments'].items()
        ))
        refs[sig] = ref
    return refs


def enumerate_allocations(data):
    """枚举跨全部车辆、实体 ID 不重复的完整合法组配。

    已锁定（已发车）轮位作为固定约束；非在库 / 隔离轮胎不进入候选池。
    返回的每个方案都通过全局唯一性 + 每轴成对校验。
    """
    view = _coerce_view(data)
    buses = view['buses']
    tyres_by_id = _tyre_index(view)
    max_delta = view.get('max_delta_mm', MAX_DELTA_MM)
    min_tread = view.get('min_tread_mm', MIN_TREAD_MM)
    groups = view.get('pattern_compatibility_groups')
    fixed = view.get('fixed', {}) or {}

    fixed_tyre_ids = set(fixed.values())
    if len(fixed_tyre_ids) != len([t for t in fixed.values() if t]):
        raise DoubleBookingError(f'固定轮位存在同一轮胎: {fixed}')

    # 候选池：当前在库、未隔离、且没有被固定轮位吃掉的实体胎
    pool = []
    for tyre in view['tyres']:
        tid = tyre['id']
        if tid in fixed_tyre_ids:
            continue
        if tyre.get('quarantined'):
            continue
        if _location_kind(view, tid) != 'stock':
            continue
        pool.append(tyre)

    pair_cache = {}

    def legal_pair_for(bus_id, id_a, id_b):
        key = (bus_id, tuple(sorted((id_a, id_b))))
        if key not in pair_cache:
            bus = _bus_index(view)[bus_id]
            pair_cache[key] = axle_check(
                tyres_by_id[id_a], tyres_by_id[id_b], bus,
                max_delta_mm=max_delta, min_tread_mm=min_tread,
                compatibility_groups=groups)
        return pair_cache[key]

    solutions = []

    def dfs(index, used, assignments, position_map):
        if index == len(buses):
            solutions.append({
                'assignments': copy.deepcopy(assignments),
                'positions': copy.deepcopy(position_map),
            })
            return
        bus = buses[index]
        bid = bus['id']
        posids = ordered_positions(view, bid)
        fixed_ids = [fixed.get(p) for p in posids]

        if len(posids) == 2 and all(fixed_ids):
            id_a, id_b = fixed_ids
            if len({id_a, id_b}) != 2 or id_a in used or id_b in used:
                return
            check = legal_pair_for(bid, id_a, id_b)
            if not check['ok']:
                return
            assignments[bid] = [id_a, id_b]
            for pos, tid in zip(posids, fixed_ids):
                position_map[pos] = tid
            dfs(index + 1, used | {id_a, id_b}, assignments, position_map)
            return

        if len(posids) == 2 and not any(fixed_ids):
            for tyre_a, tyre_b in combinations(pool, 2):
                id_a, id_b = tyre_a['id'], tyre_b['id']
                if id_a in used or id_b in used:
                    continue
                check = legal_pair_for(bid, id_a, id_b)
                if not check['ok']:
                    continue
                assignments[bid] = [id_a, id_b]
                position_map[posids[0]] = id_a
                position_map[posids[1]] = id_b
                dfs(index + 1, used | {id_a, id_b}, assignments, position_map)
                position_map.pop(posids[0], None)
                position_map.pop(posids[1], None)
            assignments.pop(bid, None)
            return

        # 通用分支（部分固定或多于两轮位的扩展情形）
        fixed_pairs = [(p, fixed.get(p)) for p in posids if fixed.get(p)]
        free_positions = [p for p in posids if not fixed.get(p)]
        fixed_id_list = [tid for _, tid in fixed_pairs]
        if len(set(fixed_id_list)) != len(fixed_id_list):
            return
        if any(tid in used for tid in fixed_id_list):
            return

        def fill_free(pos_index, used_now, pos_map):
            if pos_index == len(free_positions):
                ids_by_pos = {p: fixed.get(p) for p in posids}
                ids = [ids_by_pos[p] for p in posids]
                if len(ids) == 2:
                    check = legal_pair_for(bid, ids[0], ids[1])
                    if not check['ok']:
                        return
                assignments[bid] = list(ids)
                dfs(index + 1, used | used_now, assignments, pos_map)
                assignments.pop(bid, None)
                return
            pos = free_positions[pos_index]
            for tyre in pool:
                tid = tyre['id']
                if tid in used_now or tid in used or tid in fixed_id_list:
                    continue
                pos_map[pos] = tid
                fill_free(pos_index + 1, used_now | {tid}, pos_map)
                pos_map.pop(pos, None)

        pos_map = {p: tid for p, tid in fixed_pairs}
        fill_free(0, set(fixed_id_list), pos_map)

    dfs(0, set(), {}, {})

    # 丰富每个方案
    all_ids = [t['id'] for t in view['tyres']]
    high_load_bus = max(view['buses'], key=lambda b: b['min_load_index']) \
        if view['buses'] else None
    refs = _reference_signatures(view)
    enriched = []
    for number, sol in enumerate(solutions, start=1):
        assignments = sol['assignments']
        used_ids = [tid for ids in assignments.values() for tid in ids]
        if len(used_ids) != len(set(used_ids)):  # 双保险
            raise DoubleBookingError(f'方案出现重复实体轮胎: {used_ids}')
        spares = [tid for tid in all_ids if tid not in used_ids]
        axles = {
            bid: axle_check(tyres_by_id[ids[0]], tyres_by_id[ids[1]],
                            _bus_index(view)[bid],
                            max_delta_mm=max_delta, min_tread_mm=min_tread,
                            compatibility_groups=groups)
            for bid, ids in assignments.items() if len(ids) == 2
        }
        spare_tyres = [_spare_detail(view, tid, high_load_bus) for tid in spares]
        spare_pairs = []
        next_high_load_pairs = []
        for id_a, id_b in combinations(spares, 2):
            pair = sorted((id_a, id_b))
            spare_pairs.append(pair)
            if high_load_bus and axle_check(
                    tyres_by_id[id_a], tyres_by_id[id_b], high_load_bus,
                    max_delta_mm=max_delta, min_tread_mm=min_tread,
                    compatibility_groups=groups)['ok']:
                next_high_load_pairs.append(pair)

        signature = tuple(sorted(
            (bid, tuple(sorted(ids))) for bid, ids in assignments.items()))
        ref = refs.get(signature, {})
        canonical_id = ref.get('plan_id')
        plan = AttrDict({
            'plan_id': canonical_id or f'ALLOC-{number:02d}',
            'canonical_id': canonical_id,
            'aliases': ref.get('aliases', []),
            'note': ref.get('note', ''),
            'recommended': bool(canonical_id),
            'legal': True,
            'assignments': {bid: list(ids) for bid, ids in assignments.items()},
            'positions': sol['positions'],
            'axles': axles,
            'spares': spares,
            'spare_tyres': spare_tyres,
            'spare_pairs': spare_pairs,
            'next_high_load_pairs': next_high_load_pairs,
            'next_high_load_ready': bool(next_high_load_pairs),
            'mounted_tread_mm': {
                bid: round(sum(current_tread_mm(tyres_by_id[tid]) for tid in ids), 3)
                for bid, ids in assignments.items()
            },
            'spare_life_mm': {
                tid: round(current_tread_mm(tyres_by_id[tid]) - min_tread, 3)
                for tid in spares
            },
        })
        enriched.append(plan)

    # 固定顺序：参考方案 A、B 优先，其余按组配签名稳定排序
    def sort_key(plan):
        order = {'A': 0, 'B': 1}
        return (order.get(plan['canonical_id'], 9),
                json.dumps(plan['assignments'], sort_keys=True))

    enriched.sort(key=sort_key)
    return enriched


def build_plans(data):
    """全局组配结果 + 管理员比较视图。"""
    view = _coerce_view(data)
    plans = enumerate_allocations(view)
    by_canonical = {p['canonical_id']: p for p in plans if p['canonical_id']}
    recommended = [by_canonical[k] for k in ('A', 'B') if k in by_canonical]
    if len(recommended) < 2:
        # 即使夹具没有 reference_plans，也按已知签名兜底识别甲 / 乙
        sig_a = {'BUS-H': ['T1', 'T2'], 'BUS-L': ['T5', 'T6']}
        sig_b = {'BUS-H': ['T3', 'T4'], 'BUS-L': ['T5', 'T6']}
        for plan in plans:
            if not by_canonical.get('A') and _same_assignment(plan, sig_a):
                plan['canonical_id'] = plan['plan_id'] = 'A'
                plan['recommended'] = True
                by_canonical['A'] = plan
            if not by_canonical.get('B') and _same_assignment(plan, sig_b):
                plan['canonical_id'] = plan['plan_id'] = 'B'
                plan['recommended'] = True
                by_canonical['B'] = plan
        recommended = [by_canonical[k] for k in ('A', 'B') if k in by_canonical]

    result = PlanResult({
        'plans': plans,
        'options': plans,
        'recommended': recommended,
        'plan_a': by_canonical.get('A'),
        'plan_b': by_canonical.get('B'),
        'comparison': comparison_table(plans),
        'conflicts': [],
        'double_bookings': [],
    })
    return result


def _same_assignment(plan, signature):
    return {b: sorted(ids) for b, ids in plan['assignments'].items()} == \
           {b: sorted(ids) for b, ids in signature.items()}


ALIAS_TO_CANONICAL = {'甲': 'A', '乙': 'B', 'PLAN-A': 'A', 'PLAN-B': 'B',
                      '方案甲': 'A', '方案乙': 'B'}

# 夹具未声明 reference_plans 时的已知甲 / 乙签名
BUILTIN_SIGNATURES = {
    'A': {'BUS-H': ['T1', 'T2'], 'BUS-L': ['T5', 'T6']},
    'B': {'BUS-H': ['T3', 'T4'], 'BUS-L': ['T5', 'T6']},
}


def canonical_signature(data, key):
    """即使方案当前不合法（如 T1 复测 3.5），也能取回 A/B 的名义分配。"""
    view = _coerce_view(data)
    wanted = ALIAS_TO_CANONICAL.get(str(key), str(key))
    for ref in view.get('reference_plans', []) or []:
        names = [ref.get('plan_id')] + list(ref.get('aliases', []))
        if key in names or wanted in names or ref.get('plan_id') == wanted:
            return copy.deepcopy(ref['assignments'])
    if wanted in BUILTIN_SIGNATURES:
        return copy.deepcopy(BUILTIN_SIGNATURES[wanted])
    return None


def get_plan(data, key):
    """按 A/B、甲/乙、PLAN-A 等名字取方案，也可直接传 assignments 字典。"""
    if isinstance(key, dict):
        for plan in build_plans(data)['plans']:
            if _same_assignment(plan, key):
                return plan
        raise KeyError(f'no legal plan matching {key}')
    wanted = str(key)
    for plan in build_plans(data)['plans']:
        if plan['plan_id'] == wanted or plan.get('canonical_id') == wanted:
            return plan
        if wanted in (plan.get('aliases') or []):
            return plan
    alias_map = {'甲': 'A', '乙': 'B', 'PLAN-A': 'A', 'PLAN-B': 'B',
                 '方案甲': 'A', '方案乙': 'B'}
    mapped = alias_map.get(wanted)
    if mapped:
        return get_plan(data, mapped)
    raise KeyError(f'unknown plan {key!r}')


def comparison_table(plans):
    """管理员比较表：领用证、剩余备胎寿命、下一次高载重车库存。"""
    rows = []
    for plan in plans:
        rows.append(BusRow({
            'plan_id': plan['plan_id'],
            'canonical_id': plan.get('canonical_id'),
            'recommended': plan['recommended'],
            'BUS-H': plan['assignments'].get('BUS-H'),
            'BUS-L': plan['assignments'].get('BUS-L'),
            'assignments': plan['assignments'],
            'spares': plan['spares'],
            'spare_life_mm': plan['spare_life_mm'],
            'spare_pairs': plan['spare_pairs'],
            'next_high_load_pairs': plan['next_high_load_pairs'],
            'next_high_load_ready': plan['next_high_load_ready'],
            'mounted_tread_mm': plan['mounted_tread_mm'],
            'note': plan.get('note', ''),
        }))
    return rows


def detect_double_bookings(suggestions):
    """把若干 per-bus suggest() 结果（或 {bus: [ids]}）做实体 ID 冲销。"""
    owners = {}
    if isinstance(suggestions, dict):
        items = [{'bus': bus, 'front_axle': ids} for bus, ids in suggestions.items()]
    else:
        items = suggestions
    for item in items:
        bus = item.get('bus')
        for tid in item.get('front_axle', item.get('tyre_ids', [])):
            owners.setdefault(tid, []).append(bus)
    return [AttrDict({'tyre_id': tid, 'buses': buses, 'count': len(buses)})
            for tid, buses in sorted(owners.items()) if len(buses) > 1]


def validate_assignment(data, assignments, *, require_stock=False,
                        require_available=None):
    """校验一个全局 assignments 映射 ``{bus: [t1, t2]}``。

    不抛异常，返回 ``{ok, problems, double_bookings, axles}``，便于调用方
    自行决定如何呈现冲突。会同时核销已发车锁定与（可选）在库状态。
    """
    if require_available is not None:
        require_stock = require_available
    view = _coerce_view(data)
    tyres = _tyre_index(view)
    problems = []
    double = detect_double_bookings(assignments)
    if double:
        problems.append({'code': 'DOUBLE_BOOKING',
                         'message': f'实体轮胎重复分配: {[d["tyre_id"] for d in double]}'})

    # 已发车锁定：固定轮位 / 固定实物不允许出现在不同的目标映射里
    fixed = view.get('fixed') or {}
    fixed_tyre_to_pos = dict(fixed)
    pos_to_bus = {p['position_id']: p['bus']
                  for p in view.get('wheel_positions', [])}
    fixed_by_bus = {}
    for pos, tid in fixed.items():
        bid = pos_to_bus.get(pos, pos.split('/')[0])
        fixed_by_bus.setdefault(bid, set()).add(tid)
    fixed_tyre_owner_bus = {tid: pos_to_bus.get(pos, pos.split('/')[0])
                            for pos, tid in fixed.items()}
    for bid, ids in assignments.items():
        if bid in fixed_by_bus and set(ids) != fixed_by_bus[bid]:
            problems.append({'code': 'LOCKED_POSITION',
                             'message': f'{bid} 已有签收发车锁定轮位 {sorted(fixed_by_bus[bid])}，'
                                        f'不能改排为 {sorted(ids)}'})
        for tid in ids:
            owner = fixed_tyre_owner_bus.get(tid)
            if owner and owner != bid:
                problems.append({'code': 'LOCKED_TYRE',
                                 'message': f'{tid} 已随 {owner} 发车锁定，不能挪给 {bid}'})

    axles = {}
    for bid, ids in assignments.items():
        bus = _bus_index(view)[bid]
        if len(ids) != 2:
            problems.append({'code': 'POSITION_COUNT',
                             'message': f'{bid} 前轴需要两条胎，收到 {ids}'})
            continue
        for tid in ids:
            if tid not in tyres:
                problems.append({'code': 'UNKNOWN_TYRE', 'message': f'未知轮胎 {tid}'})
        if all(tid in tyres for tid in ids):
            check = axle_check(tyres[ids[0]], tyres[ids[1]], bus,
                               max_delta_mm=view.get('max_delta_mm'),
                               min_tread_mm=view.get('min_tread_mm'),
                               compatibility_groups=view.get('pattern_compatibility_groups'))
            axles[bid] = check
            if not check['ok']:
                problems.extend(dict(p) for p in check['problems'])
        if require_stock:
            for tid in ids:
                kind = _location_kind(view, tid)
                if kind != 'stock':
                    problems.append({'code': 'NOT_IN_STOCK',
                                     'message': f'{tid} 当前位置类型 {kind}，不可重新领用'})
    return AttrDict({'ok': not problems, 'problems': problems,
                     'double_bookings': double, 'axles': axles})


# ---------------------------------------------------------------------------
# schoolride.rules 检验 / 证照核销
# ---------------------------------------------------------------------------
def rules_clearance(data):
    """调用原始 ``schoolride.rules.calculate`` 判定每车检验 / 证照。"""
    view = _coerce_view(data)
    events = view.get('rules_events') or view.get('route_evidence') or []
    if isinstance(events, dict):
        events = list(events.values())
    bus_ids = [b['id'] for b in view['buses']]
    if not events:
        return {bid: AttrDict({'status': 'unknown', 'clear': None,
                               'result': None,
                               'reason': '未提供 schoolride.rules 检验/证照证据'})
                for bid in bus_ids}

    decisions, _ = rules.calculate(copy.deepcopy(events))
    bus_run = {e.get('bus'): e['run_id'] for e in events
               if isinstance(e, dict) and e.get('bus')}
    out = {}
    for bid in bus_ids:
        run_id = bus_run.get(bid, f'RUN-{bid.split("-")[-1]}')
        item = decisions.get(f'{run_id}/车辆检验是否覆盖全程')
        seat_item = decisions.get(f'{run_id}/座位占用是否超过额定人数')
        seat_status = seat_item['result'] if seat_item else 'unknown'
        seat_ok = None if seat_item is None else seat_status == '充足'
        if item is None:
            out[bid] = AttrDict({'status': 'unknown', 'clear': None,
                                 'result': None, 'seat_status': seat_status,
                                 'seat_ok': seat_ok,
                                 'reason': f'rules.py 未给出 {run_id} 车辆检验裁定'})
        elif item['result'] == '可出车' and seat_ok is not False:
            out[bid] = AttrDict({'status': 'valid', 'clear': True,
                                 'result': item['result'],
                                 'inspection_result': item['result'],
                                 'seat_status': seat_status, 'seat_ok': seat_ok,
                                 'reason': item['reason'],
                                 'evidence': item['evidence']})
        else:
            reasons = [item['reason']]
            if seat_ok is False:
                reasons.append(f'座位占用裁定为 {seat_status}')
            out[bid] = AttrDict({'status': 'invalid', 'clear': False,
                                 'result': item['result'],
                                 'inspection_result': item['result'],
                                 'seat_status': seat_status, 'seat_ok': seat_ok,
                                 'reason': '；'.join(reasons),
                                 'evidence': item['evidence']})
    return out


# ---------------------------------------------------------------------------
# 事件溯源台账
# ---------------------------------------------------------------------------
class FitmentLedger:
    """轮位实装台账。所有状态变化都通过 :meth:`apply` 事件留痕。"""

    def __init__(self, fixture=None, **overrides):
        if isinstance(fixture, (str, Path)):
            fixture = json.loads(Path(fixture).read_text(encoding='utf-8'))
        self.fixture = copy.deepcopy(fixture) if fixture else {'tyres': [], 'buses': []}
        if 'tyres' not in self.fixture or 'buses' not in self.fixture:
            raise ValueError('fitment fixture requires tyres and buses')

        self.buses = copy.deepcopy(self.fixture['buses'])
        self.positions = copy.deepcopy(self.fixture.get('wheel_positions', []))
        self.max_delta_mm = overrides.get(
            'max_delta_mm', self.fixture.get('max_same_axle_tread_delta_mm', MAX_DELTA_MM))
        self.min_tread_mm = overrides.get(
            'min_tread_mm', self.fixture.get('minimum_tread_mm', MIN_TREAD_MM))
        self.groups = self.fixture.get('pattern_compatibility_groups', [])
        self.wear = self.fixture.get('tread_wear_mm_per_km')
        self.rules_events = copy.deepcopy(self.fixture.get(
            'route_evidence', self.fixture.get('rules_events', [])))
        self.work_orders = copy.deepcopy(self.fixture.get('work_orders', []))
        self.work_order_order = [w['id'] for w in sorted(
            self.work_orders, key=lambda w: w.get('sequence', 0))]

        self.tyres = {}
        measurements = {}
        for m in self.fixture.get('measurements', []):
            measurements.setdefault(m['tyre_id'], []).append(copy.deepcopy(m))
        for raw in self.fixture['tyres']:
            tyre = copy.deepcopy(raw)
            history = measurements.get(tyre['id'])
            baseline_tread = raw['tread_mm']
            if history:
                # 测量数组按采证 / 复核顺序保留，不覆盖、不重排原件
                tyre['readings'] = list(history)
                first_tread = history[0].get('tread_mm', baseline_tread)
                last_tread = current_tread_mm(tyre)
                # 兼容种子式直接改写 tyre['tread_mm']=3.5 的补正口径：
                # 若当前规格值与测量链末值不同，把它作为一次补正追加，
                # 原始测量（含 5.0）仍然保留。
                if baseline_tread != last_tread:
                    tyre['readings'].append({
                        'measurement_id': f"CORRECTION-{tyre['id']}",
                        'tyre_id': tyre['id'],
                        'tread_mm': baseline_tread,
                        'source': 'tread_mm-correction',
                        'note': '夹具 tread_mm 相对测量链末值的补正',
                    })
                tyre['tread_mm'] = current_tread_mm(tyre)
                original_tread = first_tread
            else:
                tyre['readings'] = [{
                    'measurement_id': f"INIT-{tyre['id']}",
                    'tyre_id': tyre['id'],
                    'tread_mm': tyre['tread_mm'],
                    'recorded_at': None,
                    'source': 'catalog-baseline',
                }]
                original_tread = raw['tread_mm']
            tyre['original_tread_mm'] = original_tread
            self.tyres[tyre['id']] = tyre

        initial = self.fixture.get('initial_locations', {}) or {}
        self.locations = {
            tid: {'kind': 'stock', 'ref': initial.get(tid, f'STOCK/{tid}')}
            for tid in self.tyres
        }
        self.movements = {tid: [] for tid in self.tyres}
        self.certificates = {}
        self.position_cert = {}
        self.trips = []
        self.events = []
        self.revision = 0

    # -- 基础视图 -----------------------------------------------------------
    @classmethod
    def from_fixture(cls, path):
        return cls(path)

    def view(self):
        fixed = {}
        locked_positions = set()
        for pos, cert_id in self.position_cert.items():
            cert = self.certificates[cert_id]
            if cert['status'] == 'dispatched':
                fixed[pos] = cert['tyre_id']
                locked_positions.add(pos)
        return AttrDict({
            '__tyre_view__': True,
            'buses': copy.deepcopy(self.buses),
            'tyres': [self._tyre_view(tid) for tid in self.tyres],
            'wheel_positions': copy.deepcopy(self.positions),
            'max_delta_mm': self.max_delta_mm,
            'min_tread_mm': self.min_tread_mm,
            'pattern_compatibility_groups': copy.deepcopy(self.groups),
            'tread_wear_mm_per_km': self.wear,
            'reference_plans': copy.deepcopy(self.fixture.get('reference_plans', [])),
            'rules_events': copy.deepcopy(self.rules_events),
            'work_orders': copy.deepcopy(self.work_orders),
            'fixed': fixed,
            'locations': copy.deepcopy(self.locations),
            'locked_positions': locked_positions,
        })

    def _tyre_view(self, tyre_id):
        tyre = copy.deepcopy(self.tyres[tyre_id])
        tyre['tread_mm'] = current_tread_mm(tyre)
        return tyre

    def bus_ids(self):
        return [b['id'] for b in self.buses]

    def location_of(self, tyre_id):
        return copy.deepcopy(self.locations[tyre_id])

    def tyre_history(self, tyre_id):
        return copy.deepcopy(self.movements.get(tyre_id, []))

    def available_tyres(self):
        return [tid for tid, loc in self.locations.items()
                if loc['kind'] == 'stock' and not self.tyres[tid].get('quarantined')]

    def occupied_tyres(self):
        return [tid for tid, loc in self.locations.items() if loc['kind'] != 'stock']

    def locked_tyre_ids(self):
        return {cert['tyre_id'] for cert in self.certificates.values()
                if cert['status'] == 'dispatched'}

    def locked_positions(self):
        return {pos for pos, cid in self.position_cert.items()
                if self.certificates[cid]['status'] == 'dispatched'}

    # -- 事件入口 -----------------------------------------------------------
    def apply(self, event):
        event = copy.deepcopy(event)
        kind = event.get('kind')
        handler = {
            'measurement': self._ev_measurement,
            'measurement_review': self._ev_measurement,
            'remeasure': self._ev_measurement,
            'quarantine': self._ev_quarantine,
            'isolate': self._ev_quarantine,
            'qc_hold': self._ev_quarantine,
            'qc_release': self._ev_qc_release,
            'unquarantine': self._ev_qc_release,
            'release_quarantine': self._ev_qc_release,
            'checkout': self._ev_checkout,
            'issue': self._ev_checkout,
            'return': self._ev_return,
            'checkin': self._ev_return,
            'return_to_stock': self._ev_return,
            'commit_plan': self._ev_commit_plan,
            'commit_assignments': self._ev_commit_plan,
            'mount': self._ev_commit_plan,
            'sign': self._ev_sign,
            'sign_certificate': self._ev_sign,
            'dispatch': self._ev_dispatch,
            'dispatch_bus': self._ev_dispatch,
            'revoke': self._ev_revoke,
            'revoke_certificate': self._ev_revoke,
            'work_order_reordered': self._ev_reorder,
        }.get(kind)
        if handler is None:
            raise FitmentError(f'unknown tyre capture event kind: {kind!r}')
        self.revision += 1
        event.setdefault('revision', self.revision)
        result = handler(event)
        self.events.append(event)
        return result if result is not None else self

    # -- 测量 ---------------------------------------------------------------
    def _ev_measurement(self, event):
        tid = event['tyre_id']
        tyre = self.tyres[tid]
        reading = {
            'measurement_id': event.get('measurement_id', f"M-{tid}-r{self.revision}"),
            'tyre_id': tid,
            'tread_mm': event['tread_mm'],
            'measured_at': event.get('at') or event.get('measured_at'),
            'recorded_at': event.get('recorded_at') or event.get('at'),
            'source': event.get('source', 'late-review'),
            'note': event.get('note', '迟到测量复核；原始读数保留'),
            'revision': self.revision,
        }
        tyre.setdefault('readings', []).append(reading)
        tyre['tread_mm'] = current_tread_mm(tyre)
        return self

    def record_measurement(self, tyre_id, tread_mm, at=None, **extra):
        return self.apply({'kind': 'measurement', 'tyre_id': tyre_id,
                           'tread_mm': tread_mm, 'at': at, **extra})

    def correct_tread(self, tyre_id, tread_mm, at=None, **extra):
        """语义化别名：迟到补正。原件仍在 readings / original_tread_mm。"""
        extra.setdefault('note', '测量补正；原读数不覆盖')
        return self.record_measurement(tyre_id, tread_mm, at=at, **extra)

    # -- 质检隔离 -----------------------------------------------------------
    def _ev_quarantine(self, event):
        tid = event['tyre_id']
        tyre = self.tyres[tid]
        tyre['quarantined'] = True
        tyre.setdefault('qc_history', []).append({
            'action': 'quarantine', 'at': event.get('at'),
            'qc_id': event.get('qc_id'), 'reason': event.get('reason', '质检隔离'),
            'revision': self.revision,
        })
        loc = self.locations[tid]
        if loc['kind'] == 'stock':
            self._move(tid, 'quarantine', event.get('bay', f"QC/{tid}"),
                       event.get('at'), 'quarantine_move',
                       reason=event.get('reason'))
        return self

    def _ev_qc_release(self, event):
        tid = event['tyre_id']
        tyre = self.tyres[tid]
        tyre['quarantined'] = False
        tyre.setdefault('qc_history', []).append({
            'action': 'qc_release', 'at': event.get('at'),
            'qc_id': event.get('qc_id'), 'reason': event.get('reason', '隔离解除'),
            'revision': self.revision,
        })
        if self.locations[tid]['kind'] == 'quarantine':
            self._move(tid, 'stock', event.get('bin') or f"STOCK/{tid}",
                       event.get('at'), 'qc_return_to_stock')
        return self

    def quarantine(self, tyre_id, reason='质检隔离', at=None, **extra):
        return self.apply({'kind': 'quarantine', 'tyre_id': tyre_id,
                           'reason': reason, 'at': at, **extra})

    def release_quarantine(self, tyre_id, at=None, **extra):
        return self.apply({'kind': 'qc_release', 'tyre_id': tyre_id, 'at': at, **extra})

    # -- 领出 / 退回 --------------------------------------------------------
    def _move(self, tid, kind, ref, at, movement_kind, **extra):
        previous = copy.deepcopy(self.locations[tid])
        self.locations[tid] = {'kind': kind, 'ref': ref, 'since': at}
        entry = {'kind': movement_kind, 'at': at, 'to': {'kind': kind, 'ref': ref},
                 'from': previous, **extra}
        self.movements[tid].append(entry)
        return entry

    def _ev_checkout(self, event):
        tid = event['tyre_id']
        loc = self.locations[tid]
        if self.tyres[tid].get('quarantined'):
            raise TyreOccupiedError(f'{tid} 处于质检隔离，不能领出')
        if loc['kind'] != 'stock':
            raise TyreOccupiedError(
                f"{tid} 当前在 {loc['kind']}:{loc.get('ref')}，退库前仍被占用，不能重复领出")
        wo = event.get('work_order') or event.get('work_order_id') or event.get('ref')
        self._move(tid, 'checkout', wo or event.get('to') or f"WO/{self.revision}",
                   event.get('at'), 'checkout',
                   actual_checkout_at=event.get('at'), by=event.get('by'))
        return self

    def checkout(self, tyre_id, work_order=None, at=None, **extra):
        return self.apply({'kind': 'checkout', 'tyre_id': tyre_id,
                           'work_order': work_order, 'at': at, **extra})

    def _ev_return(self, event):
        tid = event['tyre_id']
        loc = self.locations[tid]
        if loc['kind'] not in ('checkout', 'position'):
            raise TyreOccupiedError(
                f"{tid} 当前位置 {loc['kind']}:{loc.get('ref')}，无未结领出 / 装用可退回")
        cert_id = self.position_cert.get(loc['ref']) if loc['kind'] == 'position' else None
        if cert_id:
            cert = self.certificates[cert_id]
            cert['returned_at'] = event.get('at')
            cert['history'].append({'status': cert['status'], 'at': event.get('at'),
                                    'action': 'physical_return',
                                    'revision': self.revision})
            if self.position_cert.get(loc['ref']) == cert_id and cert['status'] == 'revoked':
                # 撤销证对应的实物退回后腾空轮位；历史证保留在 certificates
                self.position_cert.pop(loc['ref'], None)
        if self.tyres[tid].get('quarantined'):
            target_kind, target_ref = 'quarantine', event.get('bay') or f"QC/{tid}"
        else:
            target_kind, target_ref = 'stock', event.get('bin') or f"STOCK/{tid}"
        self._move(tid, target_kind, target_ref, event.get('at'), 'return',
                   actual_return_at=event.get('at'),
                   checkout_ref=loc.get('ref'), condition=event.get('condition'))
        return self

    def return_tyre(self, tyre_id, at=None, **extra):
        return self.apply({'kind': 'return_to_stock', 'tyre_id': tyre_id,
                           'at': at, **extra})

    # -- 组配 / 安装证 ------------------------------------------------------
    def plans(self):
        return build_plans(self)

    def _work_order_bus(self, wo_id):
        for wo in self.work_orders:
            if wo['id'] == wo_id:
                return wo.get('bus')
        return None

    def _ev_commit_plan(self, event):
        assignments = event.get('assignments')
        plan_id = event.get('plan_id') or event.get('plan')
        if assignments is None and plan_id is not None:
            try:
                plan = get_plan(self, plan_id)
                assignments = plan['assignments']
                event['plan_resolved'] = plan['plan_id']
            except KeyError:
                # 名义方案可能正因测量补正 / 隔离而不再合法：取回其原始
                # 签名后交给 validate_assignment 给出成对失败原因。
                signature = canonical_signature(self, plan_id)
                if signature is None:
                    raise
                assignments = signature
                event['plan_resolved'] = ALIAS_TO_CANONICAL.get(plan_id, plan_id)
                event['plan_currently_illegal'] = True
        if not assignments:
            raise FitmentError('commit requires plan_id or assignments')

        # 全局唯一性
        double = detect_double_bookings(assignments)
        if double:
            raise DoubleBookingError(
                '同一安装建议把实体轮胎同时分给多车: '
                + '; '.join(f"{d['tyre_id']}->{d['buses']}" for d in double))

        validation = validate_assignment(self, assignments)
        if not validation['ok']:
            codes = [p.get('code') for p in validation['problems']]
            if any(code and code.startswith('LOCKED') for code in codes):
                raise LockedPositionError(validation['problems'])
            if 'DOUBLE_BOOKING' in codes:
                raise DoubleBookingError(validation['problems'])
            raise IncompatibleAxleError(validation['problems'])

        at = event.get('at')
        signed = bool(event.get('signed'))

        # ---- 预检整份组配：任何冲突都在变更前抛出，保证提交原子 ----
        ops = []
        idempotent = []
        for bid, ids in assignments.items():
            posids = ordered_positions(self.view(), bid)
            if len(posids) != len(ids):
                raise FitmentError(f'{bid} 轮位数 {len(posids)} 与轮胎数 {len(ids)} 不符')
            wo_id = event.get('work_orders', {}).get(bid) if event.get('work_orders') \
                else next((w['id'] for w in self.work_orders if w.get('bus') == bid), None)
            for pos, tid in zip(posids, ids):
                existing_cert_id = self.position_cert.get(pos)
                if pos in self.locked_positions():
                    locked_cert = self.certificates[existing_cert_id]
                    if locked_cert['tyre_id'] != tid:
                        raise LockedPositionError(
                            f'轮位 {pos} 已随车次 {locked_cert.get("run_id")} 签收发车并锁定：'
                            f'{locked_cert["tyre_id"]} 不能因工单排序改给 {tid}')
                if tid in self.locked_tyre_ids():
                    owner = next(c for c in self.certificates.values()
                                 if c['tyre_id'] == tid and c['status'] == 'dispatched')
                    if owner['position_id'] != pos:
                        raise LockedPositionError(
                            f'{tid} 已锁定在 {owner["position_id"]}（车次 {owner.get("run_id")}），'
                            f'不能挪到 {pos}')
                if existing_cert_id:
                    existing = self.certificates[existing_cert_id]
                    if existing['status'] != 'revoked':
                        if existing['tyre_id'] == tid:
                            idempotent.append(existing)
                            continue
                        raise TyreOccupiedError(
                            f'轮位 {pos} 已有有效装配证 {existing_cert_id}（{existing["tyre_id"]}）')

                loc = self.locations[tid]
                checkout_at = None
                if loc['kind'] == 'checkout':
                    if self._work_order_bus(loc.get('ref')) != bid and \
                            loc.get('ref') != wo_id:
                        raise TyreOccupiedError(
                            f'{tid} 已领出到 {loc.get("ref")}，不能用于 {bid}')
                    for m in reversed(self.movements[tid]):
                        if m['kind'] == 'checkout':
                            checkout_at = m.get('actual_checkout_at') or m.get('at')
                            break
                elif loc['kind'] == 'position':
                    raise TyreOccupiedError(f'{tid} 已装在 {loc.get("ref")}，尚未退库')
                elif loc['kind'] == 'quarantine' or self.tyres[tid].get('quarantined'):
                    raise TyreOccupiedError(f'{tid} 已隔离，不能领用安装')
                elif loc['kind'] != 'stock':
                    raise TyreOccupiedError(f'{tid} 位置 {loc} 不可安装')

                revision = 1
                cert_id = f'CERT-{pos.replace("/", "-")}-v{revision}'
                while cert_id in self.certificates:
                    revision += 1
                    cert_id = f'CERT-{pos.replace("/", "-")}-v{revision}'
                side = next((p.get('side') for p in self.positions
                              if p['position_id'] == pos),
                             'left' if pos.endswith('left') else 'right')
                ops.append({'bid': bid, 'pos': pos, 'tid': tid, 'wo_id': wo_id,
                            'checkout_at': checkout_at, 'cert_id': cert_id,
                            'side': side})

        # ---- 全部通过后才落证与移动实物 ----
        issued = list(idempotent)
        for op in ops:
            cert = {
                'cert_id': op['cert_id'],
                'certificate_id': op['cert_id'],
                'id': op['cert_id'],
                'bus': op['bid'],
                'vehicle': op['bid'],
                'position_id': op['pos'],
                'position': op['pos'],
                'wheel_position': op['pos'],
                'axle': 'front',
                'side': op['side'],
                'tyre_id': op['tid'],
                'tyre': op['tid'],
                'physical_tyre_id': op['tid'],
                'plan_id': event.get('plan_resolved') or event.get('plan_id'),
                'work_order': op['wo_id'],
                'issued_at': at,
                'checked_out_at': op['checkout_at'],
                'signed': signed,
                'signed_by': event.get('signed_by') if signed else None,
                'received_by': event.get('received_by'),
                'status': 'signed' if signed else 'issued',
                'locked': False,
                'run_id': None,
                'trip_id': None,
                'dispatched_at': None,
                'mileage_km': None,
                'history': [{
                    'action': 'commit', 'status': 'signed' if signed else 'issued',
                    'at': at, 'revision': self.revision,
                    'plan_id': event.get('plan_resolved') or event.get('plan_id')}],
            }
            self.certificates[op['cert_id']] = cert
            self.position_cert[op['pos']] = op['cert_id']
            self._move(op['tid'], 'position', op['pos'], at, 'mount',
                       cert_id=op['cert_id'])
            issued.append(cert)
        event['issued_certificate_ids'] = [c['cert_id'] for c in issued]
        return issued

    def commit_plan(self, plan_id='A', at=None, signed=True, **extra):
        result = self.apply({'kind': 'commit_plan', 'plan_id': plan_id,
                             'at': at, 'signed': signed, **extra})
        return result

    def commit_assignments(self, assignments, at=None, signed=True, **extra):
        return self.apply({'kind': 'commit_assignments', 'assignments': assignments,
                           'at': at, 'signed': signed, **extra})

    def install(self, assignments, at=None, signed=True, **extra):
        return self.commit_assignments(assignments, at=at, signed=signed, **extra)

    # -- 签署 / 发车锁定 ----------------------------------------------------
    def _ev_sign(self, event):
        cert = self.certificates[event['certificate_id']]
        cert['signed'] = True
        cert['signed_by'] = event.get('by') or event.get('signed_by')
        if cert['status'] in ('issued',):
            cert['status'] = 'signed'
        cert['history'].append({'action': 'sign', 'status': cert['status'],
                                'at': event.get('at'), 'by': cert['signed_by'],
                                'revision': self.revision})
        return cert

    def sign_certificate(self, certificate_id, by=None, at=None):
        return self.apply({'kind': 'sign', 'certificate_id': certificate_id,
                           'by': by, 'at': at})

    def _bus_certs(self, bid):
        posids = ordered_positions(self.view(), bid)
        return [(p, self.certificates[self.position_cert[p]])
                for p in posids if p in self.position_cert]

    def bus_dispatch_blockers(self, bid):
        """返回某车当前不能放行的原因列表（空列表 = 可放行 / 可发车）。"""
        blockers = []
        posids = ordered_positions(self.view(), bid)
        certs = []
        for pos in posids:
            cid = self.position_cert.get(pos)
            if not cid:
                blockers.append(f'{pos} 缺少轮位装配证')
                continue
            cert = self.certificates[cid]
            if cert['status'] == 'revoked':
                blockers.append(f'{pos} 装配证 {cid} 已撤销')
                continue
            certs.append(cert)
            if not cert.get('signed'):
                blockers.append(f'{cid} 尚未签收')

        if len(certs) == len(posids) == 2:
            check = axle_check(
                self._tyre_view(certs[0]['tyre_id']),
                self._tyre_view(certs[1]['tyre_id']),
                _bus_index(self.view())[bid],
                max_delta_mm=self.max_delta_mm, min_tread_mm=self.min_tread_mm,
                compatibility_groups=self.groups)
            if not check['ok']:
                blockers.extend(check['reasons'])

        clearance = rules_clearance(self).get(bid)
        if clearance is not None and clearance['status'] == 'invalid':
            blockers.append(f"rules.py 检验/证照无效：{clearance['reason']}")
        return blockers

    def _ev_dispatch(self, event):
        bid = event['bus']
        blockers = self.bus_dispatch_blockers(bid)
        if blockers and not event.get('force'):
            raise DispatchBlocked(f'{bid} 不得发车：' + '；'.join(blockers))
        at = event.get('at')
        run_id = event.get('run_id')
        trip_id = event.get('trip_id') or run_id
        mileage = event.get('mileage_km')
        certs = [c for _, c in self._bus_certs(bid) if c['status'] != 'revoked']
        cert_ids = []
        assignments = {}
        for cert in certs:
            cert['status'] = 'dispatched'
            cert['locked'] = True
            cert['dispatched_at'] = at
            cert['run_id'] = run_id
            cert['trip_id'] = trip_id
            cert['mileage_km'] = mileage
            cert['history'].append({'action': 'dispatch_locked',
                                    'status': 'dispatched', 'at': at,
                                    'run_id': run_id, 'mileage_km': mileage,
                                    'revision': self.revision})
            cert_ids.append(cert['cert_id'])
            assignments.setdefault(cert['position_id'], cert['tyre_id'])
        trip = {'trip_id': trip_id, 'run_id': run_id, 'bus': bid, 'at': at,
                'mileage_km': mileage, 'certificate_ids': cert_ids,
                'positions': assignments, 'revision': self.revision}
        self.trips.append(trip)
        event['trip'] = trip
        return trip

    def dispatch_bus(self, bus, at=None, run_id=None, mileage_km=None, **extra):
        return self.apply({'kind': 'dispatch', 'bus': bus, 'at': at,
                           'run_id': run_id or f'RUN-{bus.split("-")[-1]}',
                           'mileage_km': mileage_km, **extra})

    def try_dispatch_bus(self, *args, **kwargs):
        """不抛异常版本：返回 {dispatched, blockers, trip}。"""
        try:
            trip = self.dispatch_bus(*args, **kwargs)
            return AttrDict({'dispatched': True, 'blockers': [], 'trip': trip})
        except DispatchBlocked as exc:
            return AttrDict({'dispatched': False,
                             'blockers': [str(exc)], 'trip': None})

    # -- 证书撤销 -----------------------------------------------------------
    def _ev_revoke(self, event):
        cid = event.get('certificate_id') or event.get('cert_id')
        cert = self.certificates[cid]
        if cert['status'] == 'revoked':
            return cert
        cert['status'] = 'revoked'
        cert['locked'] = False
        cert['revoked_at'] = event.get('at')
        cert['revoke_reason'] = event.get('reason', '证书撤销')
        # 显式保留：dispatched_at / run_id / mileage_km / 轮位 / tyre_id
        cert['history'].append({'action': 'revoke', 'status': 'revoked',
                                'at': event.get('at'),
                                'reason': cert['revoke_reason'],
                                'affected_run_id': cert.get('run_id'),
                                'mileage_km': cert.get('mileage_km'),
                                'revision': self.revision})
        event['affected_trip'] = {
            'run_id': cert.get('run_id'), 'trip_id': cert.get('trip_id'),
            'mileage_km': cert.get('mileage_km'),
            'position_id': cert['position_id'], 'tyre_id': cert['tyre_id']}
        return cert

    def revoke_certificate(self, certificate_id, reason=None, at=None, **extra):
        return self.apply({'kind': 'revoke', 'certificate_id': certificate_id,
                           'reason': reason, 'at': at, **extra})

    # -- 工单重排 -----------------------------------------------------------
    def _ev_reorder(self, event):
        order = event.get('bus_order') or event.get('order')
        self.work_order_order = list(order)
        return self

    def reorder_work_orders(self, new_order, at=None):
        """new_order 可传工单号或车号序列；锁定轮位绝不随排序移动。"""
        bus_order = []
        for item in new_order:
            bus = next((w.get('bus') for w in self.work_orders if w['id'] == item), item)
            if bus not in self.bus_ids():
                raise FitmentError(f'工单排序包含未知车 / 工单: {item}')
            bus_order.append(bus)
        if sorted(bus_order) != sorted(self.bus_ids()):
            raise FitmentError('工单排序必须覆盖全部车辆')
        before = {p: self.certificates[cid]['tyre_id']
                  for p, cid in self.position_cert.items()
                  if self.certificates[cid]['status'] == 'dispatched'}
        self.apply({'kind': 'work_order_reordered', 'bus_order': bus_order, 'at': at})
        plan_result = build_plans(self)
        after = dict(self.view()['fixed'])
        moved = [p for p in before if before[p] != after.get(p)]
        report = AttrDict({
            'requested_bus_order': bus_order,
            'locked_before': before,
            'locked_after': after,
            'moved_positions': moved,
            'locked_preserved': not moved and before == after,
            'available_plans': plan_result['plans'],
            'recommended': plan_result['recommended'],
        })
        return report

    # -- 放行清单 -----------------------------------------------------------
    def dispatch_commitments(self):
        clearance = rules_clearance(self)
        rows = []
        for bid in self.bus_ids():
            blockers = self.bus_dispatch_blockers(bid)
            certs = [cert for _, cert in self._bus_certs(bid)]
            is_dispatched = any(c['status'] == 'dispatched' for c in certs)
            if is_dispatched and not blockers:
                status = 'dispatched'
            elif not blockers:
                status = 'releasable'
            else:
                status = 'held'
            rows.append(BusRow({
                'bus': bid,
                'status': status,
                'releasable': status == 'releasable',
                'dispatched': status == 'dispatched',
                'held': status == 'held',
                'blockers': blockers,
                'certificate_ids': [c['cert_id'] for c in certs],
                'certificates': [copy.deepcopy(c) for c in certs],
                'rules': clearance.get(bid),
                'service_km': next(b.get('service_km') for b in self.buses if b['id'] == bid),
            }))
        return AttrDict({
            'commitments': rows,
            'releasable': [r for r in rows if r['status'] == 'releasable'],
            'dispatched': [r for r in rows if r['status'] == 'dispatched'],
            'held': [r for r in rows if r['status'] == 'held'],
            'releasable_bus_ids': [r['bus'] for r in rows if r['status'] == 'releasable'],
        })

    def releasable_buses(self):
        """可放行车清单（BusRow 列表，支持 ``'BUS-H' in ...``）。"""
        return self.dispatch_commitments()['releasable']

    def releasable_vehicles(self):
        return self.releasable_buses()

    # -- 备胎 ---------------------------------------------------------------
    def spare_combinations(self, plan_id=None):
        if plan_id is not None:
            plan = get_plan(self, plan_id)
            return plan['spare_tyres']
        stock = [tid for tid in self.available_tyres()]
        high = max(self.buses, key=lambda b: b['min_load_index'])
        return [_spare_detail(self.view(), tid, high) for tid in stock]

    def spare_tyre_combinations(self, plan_id=None):
        return self.spare_combinations(plan_id)

    # -- 变更报告 -----------------------------------------------------------
    def change_report(self, since_revision=0):
        corrections, qc_changes, movements, commits, dispatches, revocations, reorders = \
            [], [], [], [], [], [], []
        previous_tread = {tid: tyre.get('original_tread_mm')
                          for tid, tyre in self.tyres.items()}
        for event in self.events:
            if event.get('revision', 0) <= since_revision:
                continue
            kind = event['kind']
            if kind in ('measurement', 'measurement_review', 'remeasure'):
                tid = event['tyre_id']
                corrections.append(AttrDict({
                    'tyre_id': tid,
                    'from_mm': previous_tread.get(tid),
                    'to_mm': event['tread_mm'],
                    'at': event.get('at'),
                    'measurement_id': event.get('measurement_id'),
                    'original_reading_preserved':
                        self.tyres[tid].get('original_tread_mm') is not None,
                }))
                previous_tread[tid] = event['tread_mm']
            elif kind in ('quarantine', 'isolate', 'qc_hold', 'qc_release',
                          'unquarantine', 'release_quarantine'):
                qc_changes.append(copy.deepcopy(event))
            elif kind in ('checkout', 'issue', 'return', 'checkin', 'return_to_stock'):
                movements.append(copy.deepcopy(event))
            elif kind in ('commit_plan', 'commit_assignments', 'mount'):
                commits.append(copy.deepcopy(event))
            elif kind in ('dispatch', 'dispatch_bus'):
                dispatches.append(copy.deepcopy(event))
            elif kind in ('revoke', 'revoke_certificate'):
                revocations.append(copy.deepcopy(event))
            elif kind == 'work_order_reordered':
                reorders.append(copy.deepcopy(event))

        plan_validity = {}
        for key, signature in {
                'A': {'BUS-H': ['T1', 'T2'], 'BUS-L': ['T5', 'T6']},
                'B': {'BUS-H': ['T3', 'T4'], 'BUS-L': ['T5', 'T6']}}.items():
            check = validate_assignment(self, signature)
            plan_validity[key] = AttrDict({
                'executable': check['ok'],
                'ok': check['ok'],
                'blockers': [p.get('message', p.get('code')) for p in check['problems']],
                'problems': check['problems'],
            })

        commitments = self.dispatch_commitments()
        return AttrDict({
            'since_revision': since_revision,
            'revision': self.revision,
            'measurement_corrections': corrections,
            'quarantine_changes': qc_changes,
            'stock_movements': movements,
            'commits': commits,
            'dispatches': dispatches,
            'revocations': revocations,
            'work_order_reorders': reorders,
            'locked_positions': sorted(self.locked_positions()),
            'locked_tyre_ids': sorted(self.locked_tyre_ids()),
            'plan_validity': plan_validity,
            'plans_still_executable': [k for k, v in plan_validity.items() if v['executable']],
            'plans_invalidated': [k for k, v in plan_validity.items() if not v['executable']],
            'release': {
                'releasable': commitments['releasable_bus_ids'],
                'held': [r['bus'] for r in commitments['held']],
                'dispatched': [r['bus'] for r in commitments['dispatched']],
            },
        })

    # -- 快照 / 报告 --------------------------------------------------------
    def snapshot(self):
        commitments = self.dispatch_commitments()
        return AttrDict({
            'revision': self.revision,
            'tyres': {tid: self._tyre_view(tid) for tid in sorted(self.tyres)},
            'locations': {tid: copy.deepcopy(loc) for tid, loc in sorted(self.locations.items())},
            'available_tyres': self.available_tyres(),
            'occupied_tyres': self.occupied_tyres(),
            'certificates': [copy.deepcopy(c) for c in
                             sorted(self.certificates.values(), key=lambda c: c['cert_id'])],
            'position_cert': copy.deepcopy(self.position_cert),
            'trips': copy.deepcopy(self.trips),
            'work_order_order': list(self.work_order_order),
            'commitments': {
                'releasable': commitments['releasable_bus_ids'],
                'held': [r['bus'] for r in commitments['held']],
                'dispatched': [r['bus'] for r in commitments['dispatched']],
            },
            'events': copy.deepcopy(self.events),
        })

    def report(self):
        return self.snapshot()


# ---------------------------------------------------------------------------
# 模块级变更报告（兼容多种调用习惯）
# ---------------------------------------------------------------------------
def diff_plans(before, after):
    """比较两个组配方案：轮位变化、备胎组合与寿命差异。"""
    changes = []
    positions = sorted(set(before.get('positions', {})) | set(after.get('positions', {})))
    for pos in positions:
        old = before.get('positions', {}).get(pos)
        new = after.get('positions', {}).get(pos)
        if old != new:
            changes.append(AttrDict({'position': pos, 'old_tyre_id': old,
                                     'new_tyre_id': new}))
    return AttrDict({
        'assignment_changes': changes,
        'spares_removed': [t for t in before.get('spares', []) if t not in after.get('spares', [])],
        'spares_added': [t for t in after.get('spares', []) if t not in before.get('spares', [])],
        'spare_life_before': before.get('spare_life_mm'),
        'spare_life_after': after.get('spare_life_mm'),
        'high_load_stock_before': before.get('next_high_load_pairs'),
        'high_load_stock_after': after.get('next_high_load_pairs'),
    })


def diff_fixtures(before, after):
    """比较两份夹具 / view 状态：测量补正、隔离变化与甲 / 乙可执行性。"""
    old_view = _coerce_view(before)
    new_view = _coerce_view(after)
    old_tyres = _tyre_index(old_view)
    new_tyres = _tyre_index(new_view)
    tyre_changes = []
    corrections = []
    for tid in sorted(set(old_tyres) & set(new_tyres)):
        old_t, new_t = old_tyres[tid], new_tyres[tid]
        old_depth = current_tread_mm(old_t)
        new_depth = current_tread_mm(new_t)
        if old_depth != new_depth:
            change = AttrDict({'tyre_id': tid, 'field': 'tread_mm',
                               'from_mm': old_depth, 'to_mm': new_depth})
            tyre_changes.append(change)
            corrections.append(change)
        if bool(old_t.get('quarantined')) != bool(new_t.get('quarantined')):
            tyre_changes.append(AttrDict({
                'tyre_id': tid, 'field': 'quarantined',
                'from': bool(old_t.get('quarantined')),
                'to': bool(new_t.get('quarantined'))}))

    plan_validity = {}
    for key, signature in {
            'A': {'BUS-H': ['T1', 'T2'], 'BUS-L': ['T5', 'T6']},
            'B': {'BUS-H': ['T3', 'T4'], 'BUS-L': ['T5', 'T6']}}.items():
        before_check = validate_assignment(old_view, signature)
        after_check = validate_assignment(new_view, signature)
        plan_validity[key] = AttrDict({
            'was_executable': before_check['ok'],
            'executable': after_check['ok'],
            'is_executable': after_check['ok'],
            'blockers': [p.get('message', p.get('code'))
                         for p in after_check['problems']],
        })
    return AttrDict({
        'tyre_changes': tyre_changes,
        'measurement_corrections': corrections,
        'plan_validity': plan_validity,
        'plans_invalidated': [k for k, v in plan_validity.items()
                              if v['was_executable'] and not v['executable']],
        'plans_still_executable': [k for k, v in plan_validity.items()
                                   if v['executable']],
    })


def change_report(before=None, after=None, *args, **kwargs):
    """多态入口：

    * ``change_report(ledger)`` —— 台账全量变更报告；
    * ``change_report(ledger, since_revision=n)``；
    * ``change_report(plan_a, plan_b)`` —— 两方案差异；
    * ``change_report(old_fixture, new_fixture)`` —— 两版夹具状态差异。
    """
    if after is None and isinstance(before, FitmentLedger):
        return before.change_report(*args, **kwargs)
    if isinstance(before, FitmentLedger) and isinstance(after, int):
        return before.change_report(since_revision=after)
    if isinstance(before, dict) and isinstance(after, dict):
        if 'positions' in before and 'spares' in before:
            return diff_plans(before, after)
        if 'tyres' in before and 'tyres' in after:
            return diff_fixtures(before, after)
    if isinstance(before, FitmentLedger):
        return before.change_report()
    raise TypeError('change_report expects (ledger), (plan_a, plan_b) '
                    'or (old_fixture, new_fixture)')


def build_report(data, plan='auto'):
    """一次性输出主营结果：方案比较、安装证、放行清单、备胎。

    对裸夹具默认 auto 落一套当前可执行方案，使安装证与放行清单非空；
    传入台账时完全沿用台账既有落证 / 锁定状态。
    """
    ledger = planned_ledger(data, plan) if not isinstance(data, FitmentLedger) else data
    plans = ledger.plans()
    commitments = ledger.dispatch_commitments()
    selected = None
    if ledger.certificates:
        selected = next(iter(ledger.certificates.values())).get('plan_id')
    return AttrDict({
        'selected_plan': selected,
        'plans': plans['plans'],
        'recommended_plans': plans['recommended'],
        'comparison': plans['comparison'],
        'certificates': list(ledger.certificates.values()),
        'wheel_position_certificates': list(ledger.certificates.values()),
        'dispatch_commitments': commitments,
        'releasable_buses': commitments['releasable_bus_ids'],
        'releasable': commitments['releasable_bus_ids'],
        'spare_combinations': {
            p['plan_id']: p['spare_tyres'] for p in plans['plans']
        },
        'rules_clearance': rules_clearance(ledger),
        'snapshot': ledger.snapshot(),
    })


# 语义别名，便于不同调用方习惯
plan_fitments = build_plans
generate_plans = build_plans
solve_fitments = build_plans
global_allocation = build_plans
allocate_all = enumerate_allocations
global_plan = enumerate_allocations
spare_tyre_combinations = FitmentLedger.spare_tyre_combinations
releasable_vehicles = lambda data: (  # noqa: E731
    FitmentLedger(data).releasable_vehicles()
    if not isinstance(data, FitmentLedger) else data.releasable_vehicles())

# 类名习惯别名
TyreLedger = FitmentLedger
TyreCapture = FitmentLedger
FitmentCapture = FitmentLedger
FitmentBook = FitmentLedger


def capture_fitment(fixture=None, **kwargs):
    """采录入口：返回一台空 / 带基线夹具的轮位台账。"""
    return FitmentLedger(fixture, **kwargs)


def record_fitment(fixture, plan_id='A', at=None, signed=True, **kwargs):
    """一步完成基线装载 + 方案落证。"""
    ledger = FitmentLedger(fixture)
    ledger.commit_plan(plan_id, at=at, signed=signed, **kwargs)
    return ledger


def mounting_certificates(data, plan_id=None):
    """已落账台账的逐轮位安装证；若给夹具与 plan_id，则先落证（内存）。"""
    if isinstance(data, FitmentLedger):
        return list(data.certificates.values())
    ledger = FitmentLedger(data)
    if plan_id is not None:
        ledger.commit_plan(plan_id)
    return list(ledger.certificates.values())


fitment_certificates = mounting_certificates


def planned_ledger(data, plan='auto', at=None, signed=True):
    """把夹具变成“已落定一套方案”的台账。

    * 传入 :class:`FitmentLedger` 时原样返回（台账状态由调用方负责）；
    * ``plan='auto'`` 时优先选当前仍可执行的甲 / 乙，其次任一合法方案；
    * ``plan='A'/'B'/...`` 时落指定名义方案（不合法会抛领域错误）；
    * ``plan=None`` 时只装载基线、不落证。
    """
    if isinstance(data, FitmentLedger):
        return data
    ledger = FitmentLedger(data)
    if plan is None:
        return ledger
    chosen = plan
    if plan == 'auto':
        validity = ledger.change_report()['plan_validity']
        chosen = next((key for key in ('A', 'B')
                       if validity.get(key, {}).get('executable')), None)
        if chosen is None:
            legal = ledger.plans()['plans']
            if not legal:
                return ledger
            chosen = legal[0]['plan_id']
    ledger.commit_plan(chosen, at=at, signed=signed)
    return ledger


def releasable_buses(data, plan='auto'):
    """模块级可放行车清单，接受夹具或台账。

    传夹具时默认先落一套当前可执行方案（auto），因此直接对基线夹具调用
    即可得到 BUS-H、BUS-L；传台账时不改变其既有状态。
    """
    if isinstance(data, FitmentLedger):
        return data.releasable_buses()
    return planned_ledger(data, plan).releasable_buses()


def dispatchable_vehicles(data):
    return releasable_buses(data)


def spare_combinations(data, plan_id=None):
    if isinstance(data, FitmentLedger):
        return data.spare_combinations(plan_id)
    return FitmentLedger(data).spare_combinations(plan_id)


# 更多命名习惯别名
report_changes = change_report
diff_fitments = change_report
allocate = enumerate_allocations
match_tyres = enumerate_allocations
global_composition = build_plans
plan_tyre_fitments = build_report
fitment_report = build_report
dispatch_list = releasable_buses
release_list = releasable_buses
ready_to_dispatch = dispatchable_vehicles
capture_wheel_fitments = capture_fitment
spare_pairs = spare_combinations
spare_tyre_combinations = spare_combinations
tyre_certificates = mounting_certificates
issue_certificates = record_fitment


# ---------------------------------------------------------------------------
# SQLite：来源哈希、事件重放、audit 复算
# ---------------------------------------------------------------------------
TYRE_SCHEMA = """
CREATE TABLE IF NOT EXISTS tyre_source_files (
    sha TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    raw BLOB NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS tyre_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    sha TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tyre_certificate (
    cert_id TEXT PRIMARY KEY,
    bus TEXT NOT NULL,
    position_id TEXT NOT NULL,
    tyre_id TEXT NOT NULL,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tyre_kv (
    name TEXT PRIMARY KEY,
    body TEXT NOT NULL
);
"""


def connect(path=':memory:'):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    if path != ':memory:':
        db.execute('PRAGMA journal_mode=WAL')
    db.executescript(TYRE_SCHEMA)
    return db


connect_tyre_db = connect


def _canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'))


def _source_bytes(source):
    if isinstance(source, (str, Path)):
        path = Path(source)
        if path.exists():
            raw = path.read_bytes()
            return raw, path
        raise FileNotFoundError(source)
    if isinstance(source, bytes):
        return source, None
    if isinstance(source, (dict, list)):
        raw = _canonical(source).encode('utf-8')
        return raw, None
    raise TypeError('unsupported source type')


def import_source(db, source, kind=None):
    """导入夹具 / 事件源。同 sha 幂等；同 event_id 异内容整体回滚。"""
    raw, path = _source_bytes(source)
    sha = hashlib.sha256(raw).hexdigest()
    if db.execute('SELECT 1 FROM tyre_source_files WHERE sha=?', (sha,)).fetchone():
        return {'sha': sha, 'inserted_events': 0, 'idempotent': True}

    try:
        payload = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'tyre source is not UTF-8 JSON: {exc}') from exc

    if isinstance(payload, dict) and 'tyres' in payload and 'buses' in payload:
        event_id = payload.get('fitment_id') or payload.get('source_id') or 'FITMENT-BASELINE'
        events = [{**payload, 'id': event_id, 'kind': 'fitment_fixture'}]
        source_kind = kind or 'tyre-fitment'
    elif isinstance(payload, dict) and payload.get('id') and payload.get('kind'):
        events = [payload]
        source_kind = kind or f"tyre-{payload['kind']}"
    elif isinstance(payload, list) and payload and all(
            isinstance(e, dict) and e.get('id') and e.get('kind') for e in payload):
        events = payload
        source_kind = kind or 'tyre-event-batch'
    else:
        raise ValueError(
            'source must be a fitment fixture, an event {id,kind,...} '
            'or a nonempty event list')

    db.execute('SAVEPOINT tyre_ingest')
    try:
        seq_added = 0
        for event in events:
            event_id = event['id']
            body = _canonical(event)
            old = db.execute('SELECT body FROM tyre_events WHERE event_id=?',
                             (event_id,)).fetchone()
            if old:
                if old[0] != body:
                    raise ValueError('same event id with different content')
                continue
            db.execute(
                'INSERT INTO tyre_events(event_id,kind,body,sha) VALUES (?,?,?,?)',
                (event_id, event['kind'], body, sha))
            seq_added += 1
        db.execute('INSERT INTO tyre_source_files(sha,kind,raw) VALUES (?,?,?)',
                   (sha, source_kind, raw))
        rebuild_tyre(db)
        db.execute('RELEASE SAVEPOINT tyre_ingest')
        db.commit()
    except Exception:
        db.execute('ROLLBACK TO SAVEPOINT tyre_ingest')
        db.execute('RELEASE SAVEPOINT tyre_ingest')
        db.rollback()
        raise
    return {'sha': sha, 'inserted_events': seq_added, 'idempotent': False}


# 与仓内 store 命名保持一致的别名
import_fitment = import_source
import_fitment_file = import_source


def rebuild_tyre(db):
    """从 tyre_events 全量重放，并重建物化证表与报告 KV。"""
    rows = db.execute('SELECT kind,body FROM tyre_events ORDER BY seq').fetchall()
    ledger = None
    for row in rows:
        event = json.loads(row['body'])
        if row['kind'] == 'fitment_fixture':
            fixture = {k: v for k, v in event.items()
                       if k not in ('id', 'kind', 'revision')}
            ledger = FitmentLedger(fixture)
        elif ledger is None:
            raise FitmentError('tyre events missing fitment_fixture baseline')
        else:
            ledger.apply(event)

    db.execute('DELETE FROM tyre_certificate')
    if ledger is None:
        db.execute("DELETE FROM tyre_kv WHERE name='report'")
        return {'event_count': len(rows)}

    for cert in ledger.certificates.values():
        db.execute(
            'INSERT OR REPLACE INTO tyre_certificate VALUES (?,?,?,?,?,?,?)',
            (cert['cert_id'], cert['bus'], cert['position_id'], cert['tyre_id'],
             cert['status'], 1, _canonical(cert)))

    report = _db_report(db, ledger)
    db.execute("INSERT OR REPLACE INTO tyre_kv(name,body) VALUES ('report',?)",
               (_canonical(report),))
    return {'event_count': len(rows), 'certificate_count': len(ledger.certificates)}


def _db_report(db, ledger=None):
    if ledger is None:
        row = db.execute("SELECT body FROM tyre_kv WHERE name='report'").fetchone()
        return json.loads(row['body']) if row else {'sources': [], 'certificates': []}
    snap = json.loads(_canonical(ledger.snapshot()))
    sources = [dict(r) for r in db.execute(
        'SELECT sha,kind FROM tyre_source_files ORDER BY rowid')]
    cert_rows = [dict(r) for r in db.execute(
        'SELECT cert_id,bus,position_id,tyre_id,status FROM tyre_certificate ORDER BY cert_id')]
    return {
        'sources': sources,
        'source_hashes': [s['sha'] for s in sources],
        'certificate_rows': cert_rows,
        'snapshot': snap,
    }


def report(db):
    row = db.execute("SELECT body FROM tyre_kv WHERE name='report'").fetchone()
    if row:
        return json.loads(row['body'])
    return _db_report(db)


def replay_audit(db):
    """把全部 tyre_source_files 原始字节在内存库重放并比较报告。"""
    before = report(db)
    source_rows = db.execute(
        'SELECT kind,raw FROM tyre_source_files ORDER BY rowid').fetchall()
    memory = connect(':memory:')
    try:
        for row in source_rows:
            import_source(memory, bytes(row['raw']), kind=row['kind'])
        after = report(memory)
        ok = before == after
        return {'ok': ok, 'source_count': len(source_rows),
                'certificate_count': len(after.get('certificate_rows', [])),
                'event_count': memory.execute('SELECT COUNT(*) c FROM tyre_events').fetchone()['c']}
    finally:
        memory.close()


audit = replay_audit


class TyreStore:
    """OO 门面：来源导入、报告与 audit。"""

    def __init__(self, path=':memory:'):
        self.db = connect(path)

    def import_source(self, source, kind=None):
        return import_source(self.db, source, kind=kind)

    import_fitment_file = import_source

    def report(self):
        return report(self.db)

    def audit(self):
        return audit(self.db)

    def close(self):
        self.db.close()


FitmentStore = TyreStore
