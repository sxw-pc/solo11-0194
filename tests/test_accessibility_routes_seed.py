import json
from pathlib import Path
from schoolride.vehicle_match import compatible
from schoolride.route_dispatch import assign

def test_greedy_consumes_only_accessible_bus_on_other_route():
    d = json.loads((Path(__file__).parents[1] / 'fixtures' / 'accessibility-routes.json').read_text())
    assert compatible(d['routes'][1], d['vehicles'][1]) is True
    assert assign(d['routes'], d['vehicles']) == {'R1':'BUS-A','R2':'BUS-B'}
    assert d['routes'][1]['wheelchair'] and not d['vehicles'][1]['wheelchair_lift']
