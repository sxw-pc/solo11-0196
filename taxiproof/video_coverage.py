"""Current video envelope, without interval continuity or clock alignment."""


def envelope(segments):
    return min(row['start_second'] for row in segments), max(row['end_second'] for row in segments)
