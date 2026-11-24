import json
from pathlib import Path
from schoolride.duty_ledger import remaining
from schoolride.relief_board import suggest

def test_drive_balance_changes_first_driver_but_ignores_rest_and_travel():
    f=json.loads((Path(__file__).parents[1]/'fixtures'/'driver-duty.json').read_text())
    assert remaining(f['drivers'][0]) == 120
    assert suggest(f['drivers'], f['service']) == {'service':'AM-A','driver':'D1','remaining_after':30}
    revised=json.loads(json.dumps(f['drivers']))
    revised[0]['shifts'][0]['drive_minutes']=410
    assert remaining(revised[0]) == 70
    assert suggest(revised, f['service'])['driver'] == 'D2'
    assert f['service']['end'] < f['next_service']['start']
    assert f['travel_minutes']['NORTH-SOUTH'] > 15
