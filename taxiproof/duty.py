"""Total offline reported driving minutes for a driver."""

def minutes(logs, driver):
    return sum(row['minutes'] for row in logs if row['driver'] == driver)
