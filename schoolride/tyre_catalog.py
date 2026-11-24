"""Individual tyre eligibility from the latest tread reading."""
def eligible(tyre, bus):
    return tyre['size']==bus['tyre_size'] and tyre['load_index']>=bus['min_load_index'] and tyre['tread_mm']>=4.0
