import json
from pathlib import Path
from schoolride.meter_chain import distance
from schoolride.lease_billing import preview

def test_meter_swap_causes_negative_usage_and_zero_invoice():
    f=json.loads((Path(__file__).parents[1]/'fixtures'/'lease-billing.json').read_text())
    assert distance(f['readings'],'BUS-A') == -9860
    assert preview(f['contracts'],f['readings']) == [
        {'bus':'BUS-A','km':-9860,'overage_km':0,'overage_due':0},
        {'bus':'BUS-B','km':200,'overage_km':0,'overage_due':0}]
    assert f['meter_swap']['signed'] is True
