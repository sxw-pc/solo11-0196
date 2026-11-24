"""Report whether an accessible ride appears served."""
from .lift import deployments

def served(booking, events):
    return bool(deployments(events, booking['vehicle']))
