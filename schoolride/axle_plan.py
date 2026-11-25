"""First two individually eligible tyres for a bus front axle.

这是种子测试所固定的“朴素”行为：每车独立取仓库顺序里头两条单胎
eligible 的胎。它既不做成对兼容校验，也不知道另一辆车已经领走了哪些
实体胎，因此会产出双占建议与 T2/T3（胎纹差 1.8 mm）这种不成对组合。

全局合法组配、轮位采录与锁定见 :mod:`schoolride.tyre_capture`。
:suggest_compatible_pair: 是仅在本车列表内修正成对兼容的局部版本，
仍然解决不了跨两车的实物争用。
"""
from .tyre_catalog import eligible, pair_eligible


def suggest(bus, tyres):
    chosen=[t['id'] for t in tyres if eligible(t,bus)][:2]
    return {'bus':bus['id'],'front_axle':chosen,'ready':len(chosen)==2}


def suggest_compatible_pair(bus, tyres, max_tread_delta_mm=0.5):
    """在不考虑其他车辆的前提下，取第一对成对合法的胎。

    返回与 :func:`suggest` 同形的字典，并额外给出是否通过成对校验。
    注意：这只是“只修正同轴差值 / 成对条件”的局部解，不做全局消歧。
    """
    pool = [t for t in tyres if eligible(t, bus)]
    chosen = None
    for i, tyre_a in enumerate(pool):
        for tyre_b in pool[i + 1:]:
            if pair_eligible(tyre_a, tyre_b, bus, max_tread_delta_mm):
                chosen = [tyre_a['id'], tyre_b['id']]
                break
        if chosen:
            break
    chosen = chosen or []
    return {'bus': bus['id'], 'front_axle': chosen,
            'ready': len(chosen) == 2, 'pair_compatible': len(chosen) == 2}
