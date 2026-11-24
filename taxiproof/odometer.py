"""Baseline odometer samples across ECU swaps."""


def latest(readings):
    return max(readings, key=lambda row: row['received_at'])['kilometres']
