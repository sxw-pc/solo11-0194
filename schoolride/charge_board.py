"""First free charger board; transformer and state of charge are not modeled."""
from .energy_demand import estimate

def suggest(bus, route, chargers):
    need=estimate(route)
    charger=next(c for c in chargers if c['available_at'] <= bus['ready_at'])
    return {'bus': bus['id'], 'charger': charger['id'], 'estimated_kwh': need,
            'estimated_minutes': round(need / charger['kw'] * 60)}
