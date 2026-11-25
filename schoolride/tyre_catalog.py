"""Individual tyre eligibility from the latest tread reading.

本模块只回答“单条胎本身是否满足某车的规格 / 载重 / 最低花纹”。
注意：两个单胎 eligible 都为真，并不推出两条胎可以同轴——成对条件
（相容花纹、同轴胎纹差 <= 0.5 mm、跨车实物唯一）见
:mod:`schoolride.tyre_capture`。这里额外提供 :func:`pair_eligible`
作为最小成对布尔口径，供只需要真值判断的调用方使用。
"""


def eligible(tyre, bus):
    return tyre['size'] == bus['tyre_size'] and tyre['load_index'] >= bus['min_load_index'] and tyre['tread_mm'] >= 4.0


def pair_eligible(tyre_a, tyre_b, bus, max_tread_delta_mm=0.5, check_quarantine=True):
    """两条胎作为同一根车轴成对可用的最小布尔口径。

    等于两个单胎 eligible 之和再加上：同 / 相容花纹，以及同轴胎纹差上限。
    需要失败原因与完整核销请用 ``tyre_capture.axle_check``。
    """
    if not eligible(tyre_a, bus) or not eligible(tyre_b, bus):
        return False
    if check_quarantine and (tyre_a.get('quarantined') or tyre_b.get('quarantined')):
        return False
    pattern_a, pattern_b = tyre_a.get('pattern'), tyre_b.get('pattern')
    if pattern_a != pattern_b:
        return False
    return abs(tyre_a['tread_mm'] - tyre_b['tread_mm']) <= max_tread_delta_mm


def axle_eligible(tyre_a, tyre_b, bus, max_tread_delta_mm=0.5):
    """pair_eligible 的语义别名。"""
    return pair_eligible(tyre_a, tyre_b, bus, max_tread_delta_mm)
