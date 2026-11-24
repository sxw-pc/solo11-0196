"""Current full-trip export gate from only the outer timestamps."""
from .video_coverage import envelope


def decide(trip, segments):
    start, end = envelope(segments)
    return 'full_trip_exportable' if start <= trip['start_second'] and end >= trip['end_second'] else 'hold'
