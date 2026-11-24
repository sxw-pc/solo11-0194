"""Driver drive-minute balance from submitted shifts."""
def remaining(driver, max_drive_minutes=480):
    return max_drive_minutes-sum(s['drive_minutes'] for s in driver['shifts'])
