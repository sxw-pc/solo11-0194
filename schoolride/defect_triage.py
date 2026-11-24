"""Workshop triage of bus brake lining readings."""


def classify(inspection):
    lowest = min(inspection['brake_lining_mm'])
    return 'red' if lowest < 3.0 else 'amber' if lowest < 4.0 else 'clear'
