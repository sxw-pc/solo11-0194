"""First two individually eligible tyres for a bus front axle."""
from .tyre_catalog import eligible

def suggest(bus, tyres):
    chosen=[t['id'] for t in tyres if eligible(t,bus)][:2]
    return {'bus':bus['id'],'front_axle':chosen,'ready':len(chosen)==2}
