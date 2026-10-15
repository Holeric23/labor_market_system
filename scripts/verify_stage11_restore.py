"""Offline restored-system proof; explicitly limited to three real documents."""
import json
import os
import sys
from pathlib import Path
from urllib.parse import unquote,urlsplit

from market_system.operations.config import Operations
from market_system.operations.compatibility import resolved_spec
from market_system.operations.diagnostics import inspect


def main():
    from market_system.paths import OLD_PROJECT
    old = Path(OLD_PROJECT).resolve()
    attempts = []
    def deny_old(event,args):
        if event not in ('open','sqlite3.connect','os.listdir','os.scandir') or not isinstance(args[0],(str,bytes,os.PathLike)):
            return
        raw = os.fsdecode(args[0])
        if raw.startswith('file:'):
            raw = unquote(urlsplit(raw).path)
            if os.name=='nt' and raw.startswith('/'):
                raw = raw[1:]
        if Path(raw).resolve().is_relative_to(old):
            attempts.append(raw)
            raise PermissionError('Old project I/O is denied')
    sys.addaudithook(deny_old)
    config = Operations.load()
    config.prepare()
    from market_system.search import Catalog, SearchFilter
    from market_system.model_management.store import active_spec
    from market_system.model_management.adapters import load
    from market_system.sqlite import connect
    from contextlib import closing
    diagnostics = inspect(config,full=True)
    if not diagnostics['ok']:
        raise ValueError(diagnostics['errors'])
    catalog = Catalog(config.paths['database'])
    page = catalog.search(SearchFilter(post_type='vacancy'),page_size=3)
    if len(page.cards)!=3:
        raise ValueError('Expected three real vacancies')
    identities = [c['content_id'] for c in page.cards]
    for identity in identities:
        if not catalog.get(identity,page.result_set_id):
            raise ValueError('Restored card unavailable')
    with closing(connect(config.paths['database'].as_uri()+'?mode=ro',uri=True)) as con:
        texts = [con.execute('SELECT text FROM contents WHERE id=?',(identity,)).fetchone()[0] for identity in identities]
    model = load(resolved_spec(active_spec(config.paths['database'])))
    predictions = model.predict(texts)
    if len(predictions)!=3:
        raise ValueError('Partial prediction')
    proof = {'ok':True,'root':str(config.home),'diagnostics':diagnostics,'search_count':len(page.cards),
             'card_ids':identities,'predicted_documents':len(predictions),'labels':[p['label'] for p in predictions],
             'training':False,'telegram':False,'old_project_io_denied':True,'old_project_access_attempts':attempts}
    if attempts:
        raise ValueError('Unexpected old-project dependency')
    (config.home/'verification-stage11.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in proof.items() if k!='diagnostics'},ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
