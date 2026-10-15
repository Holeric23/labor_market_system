"""Read-only SHA-256 inventory before/after acceptance; never execute old code."""
import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD = Path(r'C:\Study\Анализ данных\Итоговый проект\Python_research_project')


def checksum(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def inventory():
    return {str(p.relative_to(OLD)): [p.stat().st_size, p.stat().st_mtime_ns, checksum(p)]
            for p in sorted(OLD.rglob('*')) if p.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('before', 'after'))
    parser.add_argument('--name', default='stage12', help='Unique evidence prefix for a new before/after pair')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z0-9_-]+', args.name):
        parser.error('--name requires lowercase ASCII letters, digits, underscore or hyphen')
    folder = ROOT / '.artifacts'
    current = inventory()
    baseline = folder / (args.name + '-old-before.json')
    if args.mode == 'before':
        if baseline.exists():
            raise FileExistsError('Preserve the existing baseline; do not replace it')
        baseline.write_text(json.dumps(current, ensure_ascii=False), encoding='utf-8')
    else:
        before = json.loads(baseline.read_text(encoding='utf-8'))
        changes = [p for p in before.keys() | current.keys() if before.get(p) != current.get(p)]
        copied = json.loads((ROOT / 'docs/COPY_MANIFEST.json').read_text(encoding='utf-8'))
        source_changes = [item['source'] for item in copied
                          if checksum(OLD / item['source']) != item['source_sha256']]
        historical = json.loads((ROOT / 'docs/source_inventory.json').read_text(encoding='utf-8'))
        drift = [p for p in historical.keys() | current.keys()
                 if historical.get(p) != (current[p][:2] if p in current else None)]
        report = dict(ok=not changes and not source_changes, files=len(current),
                      changed_during_stage12=changes, changed_copied_sources=source_changes,
                      historical_inventory_drift=drift,
                      method='All old files: size, mtime_ns, SHA-256; no old programs executed')
        (folder / (args.name + '-preservation.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        assert report['ok'], report
        print(json.dumps({k:v for k,v in report.items() if k != 'historical_inventory_drift'}, ensure_ascii=False))
        print('Historical metadata drift (predates acceptance):', len(drift))
    print('Inventoried old files:', len(current))


if __name__ == '__main__':
    main()
