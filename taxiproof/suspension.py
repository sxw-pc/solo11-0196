"""Baseline suspension event intake keyed only by uploaded report id."""


def positive_reports(rows):
    return len({row['report_id'] for row in rows if row['vertical_g'] >= 2.5})
