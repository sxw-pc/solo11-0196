"""Provable odometer accumulation across ECU (meter-controller) swaps.

A taxi's odometer controller can be replaced.  The new controller (M2) starts
its own display from a low reading, so subtracting the last service distance
from the newest raw value silently discards every kilometre accumulated on the
old controller (M1).  This module rebuilds one continuous, vehicle-frame
(VIN) scoped cumulative distance from:

* raw readings, each bound to one controller;
* signed ECU-swap handovers that bind the old and new controller to the same
  VIN, record seal break/refit, wheel-size calibration and odometer pulses at
  the handover instant.

The chain is *provable*, not assumed: a missing or inadmissible swap, a VIN
mismatch, a non-monotonic controller segment or an inconsistent pulse count
breaks continuity and the caller must treat maintenance as unverified
(``待核``) instead of guessing.

The functions here are pure domain logic.  Cryptographic signature
verification lives in :mod:`taxiproof.odometer_capture`; when a keystore is
supplied it is used to verify signed envelopes, otherwise only the structural
presence of the required signatures is checked.
"""

TOLERANCE_PULSES = 1


class OdometerContinuityError(ValueError):
    """Raised when readings cannot be linked into one continuous VIN chain."""


def latest(readings):
    """Most recently uploaded raw meter value (controller display, not cumulative)."""
    return max(readings, key=lambda row: row['received_at'])['kilometres']


def _sorted_readings(readings):
    return sorted(readings, key=lambda row: (row['event_at'], row['id']))


def _pulse_consistent(pulses, kilometres, pulse_per_km):
    if pulses is None or pulse_per_km is None:
        return True
    return abs(pulses - kilometres * pulse_per_km) <= TOLERANCE_PULSES


def _signed(structure, envelope_path, keystore):
    """Structural/cryptographic signature check for a nested envelope."""
    node = structure
    for part in envelope_path[:-1]:
        node = node.get(part, {}) if isinstance(node, dict) else {}
    envelope = node.get(envelope_path[-1]) if isinstance(node, dict) else None
    if not isinstance(envelope, dict) or not envelope.get('key_id') or not envelope.get('value'):
        return False
    if keystore is None:
        return True
    from .odometer_capture import verify_envelope
    return verify_envelope(node, envelope_path[-1], keystore)


def admissible_swap(swap, removed_ecu, installed_ecu, vin, last_removed_reading,
                    first_installed_reading, keystore=None):
    """Validate the swap that is supposed to join two controller segments.

    The swap must bind both controllers to the same VIN, match the controller
    transition observed in the readings, happen between the two surrounding
    readings, carry dual sign-off (garage + fleet reviewer) and reconcile the
    signed handover pulses / wheel calibration with the adjacent raw readings.
    Returns a list of human-readable blockers; an empty list means admissible.
    """
    blockers = []
    if not swap:
        return [f'no ECU swap event binds {removed_ecu} to {installed_ecu}']
    if swap.get('vin') != vin:
        blockers.append(f"swap {swap.get('id')} VIN {swap.get('vin')} != vehicle VIN {vin}")
    if swap.get('removed_ecu') != removed_ecu or swap.get('installed_ecu') != installed_ecu:
        blockers.append(
            f"swap {swap.get('id')} controllers {swap.get('removed_ecu')}->"
            f"{swap.get('installed_ecu')} do not match {removed_ecu}->{installed_ecu}")
    handover = swap.get('handover') or {}
    if not handover:
        blockers.append(f"swap {swap.get('id')} has no signed handover")
        handover = {}
    if swap.get('event_at') is None or not (
            last_removed_reading['event_at'] <= swap['event_at']
            <= first_installed_reading['event_at']):
        blockers.append(f"swap {swap.get('id')} event time is outside the surrounding readings")
    if handover.get('removed_reading_km') != last_removed_reading['kilometres']:
        blockers.append(
            f"swap {swap.get('id')} handover removed km {handover.get('removed_reading_km')} "
            f"!= last {removed_ecu} reading {last_removed_reading['kilometres']}")
    ppk = handover.get('pulse_per_km')
    if ppk and not _pulse_consistent(handover.get('removed_pulses'),
                                     handover.get('removed_reading_km'), ppk):
        blockers.append(f"swap {swap.get('id')} removed-controller pulses do not reconcile")
    if first_installed_reading.get('pulses') is not None and ppk:
        if not _pulse_consistent(first_installed_reading['pulses'],
                                 first_installed_reading['kilometres'], ppk):
            blockers.append(f"swap {swap.get('id')} installed-controller pulses do not reconcile")
    seal = swap.get('seal') or {}
    if not seal.get('removed_seal_id') or not seal.get('installed_seal_id'):
        blockers.append(f"swap {swap.get('id')} seal break/refit ids missing")
    for field in ('garage_signature', 'fleet_reviewer_signature'):
        if not _signed(swap, (field,), keystore):
            blockers.append(f"swap {swap.get('id')} missing/invalid {field}")
    if not _signed(swap, ('seal', 'inspector_signature'), keystore):
        blockers.append(f"swap {swap.get('id')} missing/invalid seal inspector signature")
    if not _signed(swap, ('handover', 'calibration_signature'), keystore):
        blockers.append(f"swap {swap.get('id')} missing/invalid calibration signature")
    return blockers


def build_segments(readings, swaps=(), vin=None, keystore=None):
    """Join raw readings into continuous controller segments.

    Returns ``(segments, trace)`` where each segment records the raw controller
    span and the cumulative vehicle span it maps to, and ``trace`` gives the
    cumulative value at every reading.  Raises
    :class:`OdometerContinuityError` when the chain cannot be proven.
    """
    ordered = _sorted_readings(readings)
    if not ordered:
        raise OdometerContinuityError('no odometer readings')
    seen_ids = set()
    swap_index = {(s['removed_ecu'], s['installed_ecu']): s for s in swaps}
    segments = []
    trace = []
    offset = 0
    current = None

    for reading in ordered:
        if reading['id'] in seen_ids:
            raise OdometerContinuityError(f"duplicate reading id {reading['id']}")
        seen_ids.add(reading['id'])
        ecu = reading['ecu']
        if current is None or ecu != current['ecu']:
            if current is not None:
                swap = swap_index.get((current['ecu'], ecu))
                blockers = admissible_swap(
                    swap, current['ecu'], ecu, vin,
                    current['last_reading'], reading, keystore)
                if blockers:
                    raise OdometerContinuityError('; '.join(blockers))
                offset = current['last_cumulative']
                current = {'ecu': ecu, 'first_reading': reading, 'last_reading': reading,
                           'raw_start': reading['kilometres'], 'swap': swap}
            else:
                current = {'ecu': ecu, 'first_reading': reading, 'last_reading': reading,
                           'raw_start': reading['kilometres'], 'swap': None}
            segments.append(current)
        else:
            if reading['kilometres'] < current['last_reading']['kilometres']:
                raise OdometerContinuityError(
                    f"controller {ecu} reading {reading['id']} regressed from "
                    f"{current['last_reading']['kilometres']} to {reading['kilometres']}")
            current['last_reading'] = reading
        handover = (current['swap'] or {}).get('handover') or {}
        ppk = handover.get('pulse_per_km')
        if not _pulse_consistent(reading.get('pulses'), reading['kilometres'], ppk):
            raise OdometerContinuityError(
                f"reading {reading['id']} pulses inconsistent with wheel calibration")
        cumulative = offset + reading['kilometres']
        current['last_cumulative'] = cumulative
        trace.append({'reading_id': reading['id'], 'ecu': ecu,
                      'raw_km': reading['kilometres'], 'cumulative_km': cumulative})

    rendered = []
    for index, segment in enumerate(segments):
        start_cumulative = 0 if index == 0 else segments[index - 1]['last_cumulative']
        handover = (segment['swap'] or {}).get('handover') or {}
        rendered.append({
            'ecu': segment['ecu'],
            'first_reading_id': segment['first_reading']['id'],
            'last_reading_id': segment['last_reading']['id'],
            'raw_start_km': segment['raw_start'],
            'raw_end_km': segment['last_reading']['kilometres'],
            'cumulative_start_km': start_cumulative,
            'cumulative_end_km': segment['last_cumulative'],
            'swap_event_id': (segment['swap'] or {}).get('id'),
            'pulse_per_km': handover.get('pulse_per_km'),
        })
    return rendered, trace


def cumulative_km(data, keystore=None):
    """Cumulative vehicle distance with the proof status of the controller chain."""
    readings = data['readings']
    vin = data.get('vin')
    raw_latest = latest(readings)
    try:
        segments, trace = build_segments(readings, data.get('ecu_swaps', ()), vin, keystore)
    except OdometerContinuityError as exc:
        return {'continuous': False, 'cumulative_km': None, 'raw_latest_km': raw_latest,
                'segments': [], 'trace': [], 'blockers': [str(exc)]}
    return {'continuous': True, 'cumulative_km': trace[-1]['cumulative_km'],
            'raw_latest_km': raw_latest, 'segments': segments, 'trace': trace,
            'blockers': []}
