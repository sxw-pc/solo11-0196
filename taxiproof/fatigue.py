"""Decide whether a driver can be offered another shift."""
from .duty import minutes

def dispatchable(logs, driver, maximum_minutes):
    return minutes(logs, driver) <= maximum_minutes
