"""Isolated evaluation process; parent enforces a wall-clock deadline."""
import json
import sys
from market_system.runtime import enable_guard
from market_system.paths import local_path
from market_system.search.store import connect, encode
from market_system.jobs.store import text_digest
from .evaluation import evaluate


def main():
    enable_guard()
    request = json.loads(local_path(sys.argv[1]).read_text(encoding='utf-8'))
    with connect(request['database']) as con:
        con.execute('PRAGMA query_only=ON')
        for item in request['inputs']:
            row = con.execute('SELECT text FROM contents WHERE id=?',(item['content_id'],)).fetchone()
            if not row or text_digest(row[0]) != item['text_sha256']:
                raise ValueError('Frozen evaluation text changed')
            item['text'] = row[0]
    local_path(sys.argv[2]).write_text(encode(evaluate(request['spec'],request['inputs'])),encoding='utf-8')


if __name__=='__main__':
    main()
