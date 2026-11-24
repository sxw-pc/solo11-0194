"""Current single-tank order board, using ticket balance and cheapest quote."""
from .fuel_stock import remaining

def plan(tank, deliveries, dispenses, runs, offers):
    stock = remaining(tank, deliveries, dispenses)
    required = sum(r['liters'] for r in runs)
    short = max(0, required - stock)
    offer = min(offers, key=lambda x: x['price_per_liter']) if short else None
    return {'tank': tank['id'], 'ticket_balance': stock, 'all_runs_ready': short == 0,
            'order': {'supplier': offer['supplier'], 'liters': short} if offer else None}
