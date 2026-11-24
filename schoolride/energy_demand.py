"""Early estimate of route energy, before weather and reserve margins."""
def estimate(route):
    return round(route['km'] * route['kwh_per_km'], 1)
