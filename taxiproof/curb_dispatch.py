"""Current independent nearest-ready cab assignment."""
from .flight_arrival import ready_minute


def assign(requests, cabs):
    result = []
    for request in requests:
        ready = ready_minute(request)
        candidates = [cab for cab in cabs if cab['ready_minute'] <= ready and
                      request['region'] in cab['regions']]
        cab = candidates[0] if candidates else None
        result.append({'request': request['id'], 'cab': cab['id'] if cab else None,
                       'pickup_minute': ready if cab else None})
    return result
