"""Initial work order suggestion without inventory or release evidence."""
from .defect_triage import classify


def suggest(inspection):
    severity = classify(inspection)
    action = {'red': 'axle_swap', 'amber': 'pad_check', 'clear': 'release'}[severity]
    return {'vehicle': inspection['vehicle'], 'severity': severity,
            'action': action, 'estimated_minutes': 45 if action == 'axle_swap' else 20}
