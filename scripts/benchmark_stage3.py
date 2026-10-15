"""Reproducible search benchmark on a SQLite backup of the NEW project only."""
import argparse
import hashlib
import json
import math
import os
import platform
import sqlite3
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from market_system.paths import DATABASE_PATH, PROJECT_ROOT, local_path
from market_system.search import Catalog, SearchFilter
from market_system.search.service import compile_page
from market_system.search.optimize import optimize
from market_system.sqlite import connect


def sample(con, sql, params, repeats):
    for _ in range(3):
        con.execute(sql, params).fetchall()
    samples = []
    for _ in range(repeats):
        start = time.perf_counter_ns()
        rows = con.execute(sql, params).fetchall()
        samples.append((time.perf_counter_ns() - start) / 1e6)
    return {'median_ms': statistics.median(samples), 'p95_ms': sorted(samples)[math.ceil(.95 * len(samples)) - 1],
            'samples_ms': samples, 'rows': len(rows), 'sql': sql, 'parameters': params,
            'plan': [list(r) for r in con.execute('EXPLAIN QUERY PLAN ' + sql, params)]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repeats', type=int, default=30)
    parser.add_argument('--output', default='.artifacts/stage3-benchmark')
    args = parser.parse_args()
    if args.repeats < 20:
        raise ValueError('At least 20 repeats required for p95')
    directory = local_path(args.output)
    directory.mkdir(parents=True, exist_ok=False)
    target = directory / 'catalog.db'
    with connect(DATABASE_PATH.as_uri() + '?mode=ro', uri=True) as source:
        with connect(target) as destination:
            source.backup(destination)
    con = connect(target)
    # Source may already have migration 2 after deployment. Restore only the
    # benchmark copy to its stage-2 index layout to reproduce the comparison.
    con.execute('DROP INDEX IF EXISTS catalog_all_dates')
    con.execute('DELETE FROM schema_migrations WHERE version=2')
    con.commit()
    identity = con.execute('SELECT result_set_id FROM active_result_set').fetchone()[0]
    classes = [r[0] for r in con.execute("""SELECT d.class_id,count(DISTINCT d.content_id) n
        FROM document_classes d JOIN classes c ON c.class_id=d.class_id
        WHERE d.result_set_id=? AND c.namespace LIKE 'skill:%' AND d.role IN ('required','preferred')
        GROUP BY d.class_id ORDER BY n DESC,d.class_id LIMIT 3""", (identity,))]
    cases = {
        'unfiltered': SearchFilter(),
        'one_class': SearchFilter(skill_ids=tuple(classes[:1])),
        'several_all': SearchFilter(skill_ids=tuple(classes[:2])),
        'several_any': SearchFilter(skill_ids=tuple(classes[:2]), skills_mode='ANY'),
        'combined': SearchFilter(skill_ids=tuple(classes[:2]), skills_mode='ANY', post_type='vacancy',
                                 salary_min=100000, currency='RUB', salary_period='month',
                                 published_from='2020-01-01T00:00:00Z'),
        'conditions': SearchFilter(post_type='vacancy', work_format='remote', employment_type='full_time'),
    }
    hardware_path = PROJECT_ROOT / '.artifacts/stage3-hardware.json'
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'python': platform.python_version(),
              'sqlite': sqlite3.sqlite_version, 'os': platform.platform(), 'processor': platform.processor(),
              'logical_cpus': os.cpu_count(), 'hardware': json.loads(hardware_path.read_text(encoding='utf-8-sig')) if hardware_path.exists() else None,
              'repeats': args.repeats, 'warmups': 3, 'page_size': 20,
              'method': 'Single connection per phase, warm SQLite/OS caches, sequential phases, no cache eviction; elapsed execute+fetchall, p95 nearest-rank. No concurrent workload.',
              'database_bytes': target.stat().st_size, 'result_set_id': identity,
              'catalog_rows': con.execute('SELECT count(*) FROM search_catalog WHERE result_set_id=?', (identity,)).fetchone()[0],
              'link_rows': con.execute('SELECT count(*) FROM document_classes WHERE result_set_id=?', (identity,)).fetchone()[0],
              'source_hashes': {name: hashlib.sha256((PROJECT_ROOT / name).read_bytes()).hexdigest()
                                for name in ('src/market_system/search/service.py', 'src/market_system/search/002_search.sql', 'scripts/benchmark_stage3.py')},
              'cases': {}}
    for name, f in cases.items():
        sql, params = compile_page(f.normalized(), identity, 21, strategy='baseline')
        report['cases'][name] = {'filter': f.normalized(), 'before': sample(con, sql, params, args.repeats)}
        print(name, 'before', report['cases'][name]['before']['median_ms'], flush=True)
    con.close()
    optimize(target)
    con = connect(target)
    for name, f in cases.items():
        sql, params = compile_page(f.normalized(), identity, 21)
        item = report['cases'][name]
        item['after'] = sample(con, sql, params, args.repeats)
        baseline = compile_page(f.normalized(), identity, -1, strategy='baseline')
        optimized = compile_page(f.normalized(), identity, -1)
        before_rows = con.execute(*baseline).fetchall()
        after_rows = con.execute(*optimized).fetchall()
        if before_rows != after_rows:
            raise AssertionError('Different full ordered results: ' + name)
        item['equal_full_ordered_results'] = True
        item['total_matches'] = len(after_rows)
        item['median_ratio_before_over_after'] = item['before']['median_ms'] / item['after']['median_ms']
        print(name, 'after', item['after']['median_ms'], 'matches', len(after_rows), flush=True)
    # Include complete service latency (new readonly connection, validation, page,
    # text, evidence, all source publications) separately from key selection.
    catalog = Catalog(target)
    for name, f in cases.items():
        for _ in range(3):
            catalog.search(f)
        samples = []
        for _ in range(args.repeats):
            start = time.perf_counter_ns()
            catalog.search(f)
            samples.append((time.perf_counter_ns() - start) / 1e6)
        report['cases'][name]['service'] = {'median_ms': statistics.median(samples),
            'p95_ms': sorted(samples)[math.ceil(.95 * len(samples)) - 1], 'samples_ms': samples}
    report['integrity_check'] = con.execute('PRAGMA integrity_check').fetchone()[0]
    report['foreign_key_check'] = con.execute('PRAGMA foreign_key_check').fetchall()
    con.close()
    (directory / 'results.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print(directory / 'results.json', flush=True)


if __name__ == '__main__':
    main()
