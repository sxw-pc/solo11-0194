"""Vehicle odometer readings kept for the lease desk."""

def distance(readings, bus):
    points = sorted((r for r in readings if r['bus'] == bus), key=lambda r: r['at'])
    return points[-1]['km'] - points[0]['km'] if len(points) > 1 else 0
