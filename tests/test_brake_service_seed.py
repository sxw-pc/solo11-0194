import json
from pathlib import Path
from schoolride.defect_triage import classify
from schoolride.repair_board import suggest


def test_board_suggests_unavailable_axle_without_release_test():
    data = json.loads((Path(__file__).parents[1] / 'fixtures' / 'brake-service.json').read_text())
    assert classify(data['inspection']) == 'red'
    assert suggest(data['inspection']) == {'vehicle': 'BUS-A', 'severity': 'red',
                                           'action': 'axle_swap', 'estimated_minutes': 45}
    assert data['parts'][0]['available_at'] > data['inspection']['at']
    safer_reading = {**data['inspection'], 'brake_lining_mm': [3.5, 3.8]}
    assert suggest(safer_reading)['action'] == 'pad_check'
