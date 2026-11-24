"""Passenger curb readiness derived from flight and baggage evidence."""


def ready_minute(request):
    return request['landing_minute'] + request['baggage_minutes'] + request.get('flight_delay_minutes', 0)
