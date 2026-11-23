"""Current and historical evidence report from persisted SQLite rows."""
import json


def render_report(db):
    return {'decisions': {r['name']: {'result': r['result'], 'reason': r['reason'],
                                    'evidence': json.loads(r['evidence'])}
                          for r in db.execute('SELECT * FROM decisions ORDER BY name')},
            'decision_history': [dict(r) for r in db.execute('SELECT * FROM decision_history ORDER BY revision,name')],
            'entities': [dict(r) for r in db.execute('SELECT * FROM entity_state ORDER BY subsystem,entity_id')],
            'business_records': [dict(r) for r in db.execute('SELECT * FROM business_record ORDER BY subsystem,entity_type,entity_id,version')],
            'sources': [dict(r) for r in db.execute('SELECT sha,kind FROM source_files ORDER BY sha')]}
