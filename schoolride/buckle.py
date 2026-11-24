"""Count positive buckle sensor reports."""

def buckled_count(reports):
    return sum(bool(row['latched']) for row in reports)
