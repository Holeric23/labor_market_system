from market_system.sqlite import connect as sqlite_connect
"""Offline startup smoke command; no server, collection or training."""
import argparse
from contextlib import closing
import json
import sqlite3
from .runtime import enable_guard, load_config
from .paths import MODEL_CONFIG, local_path


def smoke(config=MODEL_CONFIG):
    enable_guard()
    paths = load_config(config)
    from .labor_market import LaborMarketPipeline
    pipeline = LaborMarketPipeline.load(paths['model'], skill_lexicon=paths['skill_lexicon'],
                                        occupation_lexicon=paths['occupation_lexicon'])
    with closing(sqlite_connect(paths['database'].as_uri()+'?mode=ro',uri=True)) as con:
        rows = con.execute('''SELECT c.id,c.text,c.text_hash FROM contents c JOIN labeled_contents l
                             ON c.text_hash=l.text_hash WHERE l.final_label='vacancy'
                             ORDER BY c.id LIMIT 3''').fetchall()
    if len(rows) != 3:
        raise ValueError('Expected at least three real vacancy texts')
    results = []
    for identity, text, digest in rows:
        value = pipeline.analyze(text)
        results.append({'content_id': identity, 'text_hash': digest, 'label': value['label'],
                        'skills': value['skills'], 'salary_status': value['salary_status']})
    return {'model_version': pipeline.classifier.version, 'paths': {k:str(v) for k,v in paths.items()},
            'real_documents_analyzed': results, 'training': False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['smoke'])
    parser.add_argument('--config', default=MODEL_CONFIG)
    parser.add_argument('--output')
    args = parser.parse_args()
    result = smoke(args.config)
    encoded = json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:
        local_path(args.output).write_text(encoded,encoding='utf-8')
    print(encoded)


if __name__ == '__main__':
    main()
