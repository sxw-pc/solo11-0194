"""Depot tank balance estimated from tickets, before custody reconciliation."""

def remaining(tank, deliveries, dispenses):
    return tank['opening_liters'] + sum(x['liters'] for x in deliveries if x['tank_id'] == tank['id']) - sum(x['liters'] for x in dispenses if x['tank_id'] == tank['id'])
