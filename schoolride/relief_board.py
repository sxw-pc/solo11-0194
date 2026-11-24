"""First driver with enough drive minutes for a service block."""
from .duty_ledger import remaining

def suggest(drivers, service):
    eligible=[d for d in drivers if d['cert_until']>=service['end'] and remaining(d)>=service['drive_minutes']]
    return {'service':service['id'], 'driver':eligible[0]['id'] if eligible else None,
            'remaining_after':remaining(eligible[0])-service['drive_minutes'] if eligible else None}
