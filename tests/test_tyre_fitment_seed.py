import json
from pathlib import Path
from schoolride.tyre_catalog import eligible
from schoolride.axle_plan import suggest

def test_two_bus_plans_reuse_same_physical_tyre_pair():
    f=json.loads((Path(__file__).parents[1]/'fixtures'/'tyre-fitment.json').read_text())
    assert eligible(f['tyres'][0],f['buses'][0])
    first=suggest(f['buses'][0],f['tyres'])
    second=suggest(f['buses'][1],f['tyres'])
    assert first == {'bus':'BUS-H','front_axle':['T1','T2'],'ready':True}
    assert second == {'bus':'BUS-L','front_axle':['T1','T2'],'ready':True}
    changed=json.loads(json.dumps(f['tyres']))
    changed[0]['tread_mm']=3.5
    assert not eligible(changed[0],f['buses'][0])
    assert suggest(f['buses'][0],changed)['front_axle'] == ['T2','T3']
    assert abs(changed[1]['tread_mm']-changed[2]['tread_mm']) > f['max_same_axle_tread_delta_mm']
