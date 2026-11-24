"""Per-bus monthly lease overage preview, before pooled allowance reconciliation."""
from .meter_chain import distance

def preview(contracts, readings):
    return [{'bus': c['bus'], 'km': distance(readings,c['bus']),
             'overage_km': max(0,distance(readings,c['bus'])-c['included_km']),
             'overage_due': max(0,distance(readings,c['bus'])-c['included_km'])*c['rate_per_km']}
            for c in contracts]
