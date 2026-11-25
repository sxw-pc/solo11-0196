"""Generate the signed ECU-swap maintenance fixture.

Run from the repository root:

    PYTHONPATH=. python3 -I tools/generate_ecu_fixture.py

The fixture is deterministic: every signature is recomputed from the same
offline test keystore in taxiproof.odometer_capture, so re-running the
generator only changes the file when the underlying evidence changes.
"""
import copy
import json
from pathlib import Path

from taxiproof.odometer_capture import sign

VIN = 'LFV-CAB4-0001'
PULSE_PER_KM = 1000


def build_fixture():
    readings = [
        {'id': 'M1-A', 'vin': VIN, 'ecu': 'M1', 'event_at': 10, 'received_at': 10,
         'kilometres': 150000, 'pulses': 150000000, 'replaced_ecu': None},
        {'id': 'M2-A', 'vin': VIN, 'ecu': 'M2', 'event_at': 20, 'received_at': 20,
         'kilometres': 1800, 'pulses': 1800000, 'replaced_ecu': 'M1'},
    ]

    swap = {
        'id': 'SWAP-1', 'vin': VIN, 'removed_ecu': 'M1', 'installed_ecu': 'M2',
        'event_at': 15, 'reason': 'meter controller failure',
        'seal': {
            'removed_seal_id': 'SEAL-M1-7781', 'installed_seal_id': 'SEAL-M2-3320',
            'inspector': 'INSPECTOR-QI-09',
        },
        'handover': {
            'removed_reading_km': 150000, 'removed_pulses': 150000000,
            'pulse_per_km': PULSE_PER_KM,
            'wheel_circumference_mm': 2050,
            'calibration_device': 'CAL-RIG-12', 'calibration_tolerance_pct': 0.1,
        },
    }
    sign(swap['seal'], 'inspector_signature', 'seal-test-key')
    sign(swap['handover'], 'calibration_signature', 'calibration-test-key')
    sign(swap, 'garage_signature', 'garage-test-key')
    sign(swap, 'fleet_reviewer_signature', 'fleet-test-key')

    # Legacy valid service anchor: the only interval start before the swap.
    last_service = {'order_id': 'SV-LEGACY', 'cumulative_km': 149500,
                    'note': 'last independently evidenced maintenance'}

    # The wrongly issued certificate frozen under the baseline logic.
    initial_certificate = {
        'certificate_id': 'CERT-INIT', 'vin': VIN, 'issued_at': 20,
        'status': '未到期', 'frozen_meter_km': 1800,
        'frozen_service_km': 149500, 'frozen_interval_km': 2000,
        'meter_segments': [
            {'ecu': 'M1', 'raw_end_km': 150000, 'cumulative_end_km': 150000},
            {'ecu': 'M2', 'raw_end_km': 1800, 'cumulative_end_km': None,
             'note': 'baseline treated raw 1800 as total vehicle distance'},
        ],
        'service_ref': 'SV-LEGACY',
        'baseline_error': 'new controller low reading subtracted directly from last service km',
    }

    bundle = {
        'vehicle': 'CAB-4', 'vin': VIN, 'interval_km': 2000,
        'last_service_km': last_service['cumulative_km'],
        'last_service_ref': last_service['order_id'],
        'readings': readings,
        'ecu_swaps': [swap],
        'service_orders': [],
        'no_service_statements': [],
        'last_service': last_service,
        'initial_certificate': initial_certificate,
    }
    return bundle


def main():
    target = Path(__file__).resolve().parents[1] / 'fixtures' / 'ecu-swap-service.json'
    target.write_text(json.dumps(build_fixture(), ensure_ascii=False, indent=2) + '\n',
                      encoding='utf-8')
    print(f'wrote {target}')


if __name__ == '__main__':
    main()
