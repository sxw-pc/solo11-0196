import argparse
import json
from pathlib import Path

from .store import audit, connect, import_file, report
from . import __file__ as module_file

IMPORTS = {'meter-import': 'meter', 'shift-import': 'shift', 'trip-import': 'trip'}
DEMO = [('meter-import', 'meter-ok.json'), ('shift-import', 'shift-ok.json'),
        ('trip-import', 'trip-ok.json'), ('meter-import', 'meter-bad.json'),
        ('shift-import', 'shift-bad.json'), ('trip-import', 'trip-bad.json')]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=[*IMPORTS, 'demo', 'report', 'audit'])
    parser.add_argument('--db', default='state.sqlite')
    parser.add_argument('--file')
    args = parser.parse_args(argv)
    db = connect(args.db)
    if args.command in IMPORTS:
        if not args.file:
            parser.error('--file required')
        result = import_file(db, IMPORTS[args.command], args.file)
    elif args.command == 'demo':
        result = {'steps': [], 'imported_module': str(Path(module_file).resolve())}
        for command, name in DEMO:
            item = import_file(db, IMPORTS[command], Path(__file__).resolve().parents[1] / 'fixtures' / name)
            result['steps'].append({'file': name, **item, 'decisions': len(report(db)['decisions'])})
        result['report'] = report(db)
        result['audit'] = audit(db)
    elif args.command == 'report':
        result = report(db)
    else:
        result = audit(db)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if args.command != 'audit' or result['ok'] else 2
