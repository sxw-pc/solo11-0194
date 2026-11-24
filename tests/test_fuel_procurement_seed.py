import json
from pathlib import Path
from schoolride.fuel_stock import remaining
from schoolride.fuel_procurement import plan

def test_ticket_balance_makes_false_firm_fuel_commitment():
    f=json.loads((Path(__file__).parents[1]/'fixtures'/'fuel-procurement.json').read_text())
    assert remaining(f['tanks'][0],f['deliveries'],f['dispenses']) == 320
    assert plan(f['tanks'][0],f['deliveries'],f['dispenses'],f['runs'][:1],f['offers']) == {
        'tank':'T-A','ticket_balance':320,'all_runs_ready':True,'order':None}
    assert f['dips'][0]['maximum_liters'] < f['runs'][0]['liters']
    assert f['offers'][0]['deliver_at'] > f['runs'][0]['depart_at']
