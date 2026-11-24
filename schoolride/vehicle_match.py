"""Match route headcount against a vehicle's seat count."""

def compatible(route, vehicle):
    return route['students'] <= vehicle['seats']
