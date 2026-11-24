import json
from pathlib import Path
from schoolride.buckle import buckled_count
from schoolride.departure import clearance

def test_two_reports_from_one_seat_clear_two_students():
    d = json.loads((Path(__file__).parents[1] / 'fixtures' / 'buckle-reports.json').read_text())
    assert buckled_count(d['reports']) == 2
    assert clearance(d['manifest'], d['reports']) == 'clear'
    assert {r['seat'] for r in d['reports']} == {'A1'}
