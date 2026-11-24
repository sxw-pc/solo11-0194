import json
from pathlib import Path
from schoolride.energy_demand import estimate
from schoolride.charge_board import suggest

def test_first_free_charger_ignores_site_power_and_weather():
    f=json.loads((Path(__file__).parents[1]/'fixtures'/'depot-charge-fleet.json').read_text())
    assert estimate(f['route']) == 52.8
    assert suggest(f['bus'],f['route'],f['chargers']) == {'bus':'EV-1','charger':'C-FAST','estimated_kwh':52.8,'estimated_minutes':35}
    assert f['chargers'][0]['kw'] + f['other_load_kw'][0]['kw'] > f['site_limit_kw']
    assert estimate({**f['route'],'km':60}) == 66.0
    assert suggest(f['bus'],{**f['route'],'km':60},f['chargers'])['estimated_minutes'] == 44

def test_two_buses_claim_same_charger():
    f=json.loads((Path(__file__).parents[1]/'fixtures'/'depot-charge-fleet.json').read_text())
    assert suggest(f['bus'],f['route'],f['chargers'])['charger'] == 'C-FAST'
    assert suggest(f['second_bus'],f['second_route'],f['chargers'])['charger'] == 'C-FAST'
