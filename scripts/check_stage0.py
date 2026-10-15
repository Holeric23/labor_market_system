"""Offline structural checks. Does not claim dependency or ML compatibility."""
import ast
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(r"C:\Study\Анализ данных\Итоговый проект\Python_research_project")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-originals', action='store_true',
                        help='Explicit historical audit of the read-only old project; not needed for ordinary verification')
    args = parser.parse_args()
    errors = []
    paths = list((ROOT / "src").rglob("*.py")) + list((ROOT / "tests").rglob("*.py"))
    for path in paths:
        text = path.read_text(encoding="utf-8-sig")
        ast.parse(text, filename=str(path))
        # This constant rejects old working paths; negative tests exercise that boundary.
        boundary = path == ROOT / 'src/market_system/paths.py' or path == ROOT / 'tests/test_operations.py'
        if (str(SOURCE) in text or "Python_research_project" in text) and not boundary:
            errors.append(f"Old project reference: {path}")
        if path.is_symlink() or path.stat().st_nlink != 1:
            errors.append(f"Linked file: {path}")
    manifest = json.loads((ROOT / "docs/COPY_MANIFEST.json").read_text(encoding="utf-8"))
    for item in manifest:
        target = ROOT / item["target"]
        if target.is_symlink() or target.stat().st_nlink != 1:
            errors.append(f"Linked file: {target}")
    inventoried = verified = 0
    if args.verify_originals:
        for item in manifest:
            original = SOURCE / item['source']
            if hashlib.sha256(original.read_bytes()).hexdigest() != item['source_sha256']:
                errors.append(f'Source changed: {original}')
        verified = len(manifest)
        baseline = json.loads((ROOT / 'docs/source_inventory.json').read_text(encoding='utf-8'))
        current = {str(p.relative_to(SOURCE)): [p.stat().st_size, p.stat().st_mtime_ns]
                   for p in SOURCE.rglob('*') if p.is_file()}
        inventoried = len(current)
        changed = [name for name in baseline.keys() | current.keys() if baseline.get(name) != current.get(name)]
        errors.extend(f'Source inventory changed since stage 0: {name}' for name in changed)
    result = {"python": sys.version, "parsed_python_files": len(paths),
              "verified_source_hashes": verified, "inventoried_source_files": inventoried,
              "old_project_read": args.verify_originals,
              "errors": errors,
              "limits": "Structural check; runtime independence is proven by check_stage12.py. Optional historical inventory uses size/mtime and hashes copied inputs."}
    (ROOT / ".artifacts/static-check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return bool(errors)

if __name__ == "__main__":
    raise SystemExit(main())
