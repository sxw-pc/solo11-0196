"""Vehicle energy needed before committed departures."""

def required(jobs):
    return {j['vehicle']: j['required_kwh'] for j in jobs}
