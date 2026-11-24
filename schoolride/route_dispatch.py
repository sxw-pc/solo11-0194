"""Assign the first free vehicle to each route."""
from .vehicle_match import compatible

def assign(routes, vehicles):
    free = list(vehicles)
    result = {}
    for route in routes:
        vehicle = next((v for v in free if compatible(route, v)), None)
        result[route['id']] = vehicle['id'] if vehicle else None
        if vehicle:
            free.remove(vehicle)
    return result
