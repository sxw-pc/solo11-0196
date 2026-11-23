import json
import sqlite3
from pathlib import Path

from . import rules
from .source import read_source
from .report import render_report

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_files (sha TEXT PRIMARY KEY, kind TEXT NOT NULL, raw BLOB NOT NULL, imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL, sha TEXT NOT NULL, seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (name TEXT PRIMARY KEY, result TEXT NOT NULL, reason TEXT NOT NULL, evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entity_state (subsystem TEXT NOT NULL, entity_id TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL, PRIMARY KEY(subsystem,entity_id));
CREATE TABLE IF NOT EXISTS decision_history (revision INTEGER NOT NULL, name TEXT NOT NULL, result TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(revision,name));
CREATE TABLE IF NOT EXISTS business_record (subsystem TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, source_event TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(subsystem,entity_type,entity_id,version));
"""

def connect(path):
    db=sqlite3.connect(path)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript(SCHEMA)
    return db

def import_file(db, kind, file):
    raw,sha,payload=read_source(file)
    if db.execute('SELECT 1 FROM source_files WHERE sha=?',(sha,)).fetchone():
        return {'sha':sha,'inserted':0,'idempotent':True}
    db.execute('SAVEPOINT ingest')
    try:
        seq=db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
        inserted=0
        for event in payload:
            if event.get('kind')!=kind or not event.get('id'):
                raise ValueError('event kind/id mismatch')
            body=json.dumps(event,ensure_ascii=False,sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT body FROM events WHERE event_id=?',(event['id'],)).fetchone()
            if old:
                if old[0]!=body: raise ValueError('same event id with different content')
                continue
            seq+=1
            inserted+=1
            db.execute('INSERT INTO events VALUES (?,?,?,?,?)',(event['id'],kind,body,sha,seq))
        db.execute('INSERT INTO source_files(sha,kind,raw) VALUES (?,?,?)',(sha,kind,raw))
        rebuild(db)
        db.execute('RELEASE SAVEPOINT ingest')
        db.commit()
    except Exception:
        db.execute('ROLLBACK TO SAVEPOINT ingest')
        db.execute('RELEASE SAVEPOINT ingest')
        db.rollback()
        raise
    return {'sha':sha,'inserted':inserted,'idempotent':False}

def rebuild(db):
    events=[json.loads(row[0]) for row in db.execute('SELECT body FROM events ORDER BY seq')]
    decisions,states=rules.calculate(events)
    revision=db.execute('SELECT COUNT(*) FROM source_files').fetchone()[0]+1
    db.execute('DELETE FROM decisions')
    db.execute('DELETE FROM entity_state')
    db.execute('DELETE FROM business_record')
    for name,item in sorted(decisions.items()):
        db.execute('INSERT INTO decisions VALUES (?,?,?,?)',(name,item['result'],item['reason'],json.dumps(item['evidence'],ensure_ascii=False,sort_keys=True)))
        db.execute('INSERT OR REPLACE INTO decision_history VALUES (?,?,?,?)',(revision,name,item['result'],item['reason']))
    for subsystem,entity,state,version in states:
        db.execute('INSERT INTO entity_state VALUES (?,?,?,?)',(subsystem,entity,state,version))
    for event in events:
        for subsystem,entity_type,entity_id,body in rules.business_records(event):
            db.execute('INSERT OR REPLACE INTO business_record VALUES (?,?,?,?,?,?)',(subsystem,entity_type,entity_id,event['id'],event.get('version',1),json.dumps(body,ensure_ascii=False,sort_keys=True)))

def report(db):
    return render_report(db)

def audit(db):
    before=report(db)
    source_rows=list(db.execute('SELECT kind,raw FROM source_files ORDER BY rowid'))
    memory=connect(':memory:')
    for kind,raw in source_rows:
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(raw); path=tmp.name
        try: import_file(memory,kind,path)
        finally: Path(path).unlink()
    after=report(memory)
    return {'ok':before==after,'source_count':len(source_rows),'decision_count':len(before['decisions'])}
