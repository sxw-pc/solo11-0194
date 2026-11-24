"""Publish a departure restraint check."""
from .buckle import buckled_count

def clearance(manifest, reports):
    return 'clear' if buckled_count(reports) >= len(manifest) else 'hold'
