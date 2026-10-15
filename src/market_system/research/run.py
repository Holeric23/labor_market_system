"""One-command refresh; expensive classification runs are explicitly requested."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import json

from market_system.research.benchmark import ROOT, dump


def main():
    parser=argparse.ArgumentParser(description='Обновить оценку по разметке и итоговый ноутбук')
    parser.add_argument('--retrain',action='store_true',help='Повторить все классификационные эксперименты в новом каталоге')
    args=parser.parse_args()
    pointer=ROOT/'research/results/current_classification.json'
    run=ROOT/'research/results/run_20260930_v2'
    if pointer.exists():run=ROOT/json.loads(pointer.read_text(encoding='utf-8'))['path']
    if not args.retrain and not (run/'manifest.json').exists():
        raise FileNotFoundError('Research artifacts missing; training requires explicit --retrain')
    if args.retrain:
        from market_system.research.benchmark import run as benchmark
        run=ROOT/'research/results'/('run_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        benchmark(run)
    dump(pointer,{'path':str(run.relative_to(ROOT))})
    from market_system.research.annotation import EXTRACTION, prepare, evaluate
    if not (EXTRACTION/'weak_students.joblib').exists() and not args.retrain:
        raise FileNotFoundError('Extraction artifacts missing; training requires explicit --retrain')
    if not (EXTRACTION/'weak_students.joblib').exists():
        from market_system.research.extraction import run as extract
        extract(EXTRACTION)
    prepare()
    evaluation=evaluate()
    from market_system.research.report import build
    notebook=build(run)
    if (ROOT/'research/results/current_precision_benchmark.json').exists():
        from market_system.research.precision_report import build as build_precision
        build_precision()
    if (ROOT/'research/results/current_precision_benchmark.json').exists():
        from market_system.research.precision_report import build as build_precision
        build_precision()
    print('Оценка:',evaluation)
    print('Отчёт:',notebook)


if __name__=='__main__':
    from market_system.runtime import enable_guard
    enable_guard()
    main()

