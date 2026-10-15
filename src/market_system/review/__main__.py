import argparse
import json
from market_system.paths import DATABASE_PATH
from market_system.runtime import enable_guard
from .store import Review, Actor


def main():
    enable_guard()
    parser=argparse.ArgumentParser(description='Unified ADMIN review administration')
    parser.add_argument('--db',default=str(DATABASE_PATH))
    parser.add_argument('command',choices=['init','import-legacy'])
    args=parser.parse_args()
    review=Review(args.db)
    review.initialize()
    if args.command=='import-legacy':
        from .import_legacy import import_legacy
        print(json.dumps(import_legacy(review,Actor('local-admin','ADMIN')),ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
