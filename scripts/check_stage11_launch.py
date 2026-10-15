"""Real HTTP and restart proof ONLY on an isolated restored copy, never the working DB."""
from html import unescape
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import uuid


def main():
    from market_system.operations.config import Operations
    config = Operations.load()
    if not any(p.startswith('stage11-restored') for p in config.home.parts) or '.artifacts' not in config.home.parts:
        raise ValueError('This autonomous check requires .artifacts/stage11-restored')
    config.prepare()
    from market_system.web.state import WebState
    from market_system.telegram_bot.store import BotStore
    from market_system.jobs import Jobs
    from market_system.search import Catalog,SearchFilter
    from market_system.sqlite import connect
    from contextlib import closing
    import httpx
    state = WebState(config.paths['web_state'])
    state.initialize()
    password = 'Autonomous-restored-test-only-11'
    for username,role in [('stage11owner','OWNER'),('stage11user','USER')]:
        with state.connection() as con:
            prior = con.execute('SELECT * FROM web_users WHERE username=?',(username,)).fetchone()
        if prior:
            if prior['role']!=role or not state.hasher.verify(prior['password_hash'],password):
                raise ValueError('Refusing to modify an unknown account')
            with state.connection() as con:
                con.execute('UPDATE web_users SET active=1 WHERE id=?',(prior['id'],))
        else:
            state.create_user(username,password,role)
    store = BotStore(config.paths['database'])
    store.initialize()
    store.bootstrap_owners({910011})
    store.grant(910011,910012)
    subscription = store.subscribe(910012,{'post_type':'vacancy'})
    catalog = Catalog(config.paths['database'])
    page = catalog.search(SearchFilter(post_type='vacancy'),page_size=1)
    identity = page.cards[0]['content_id']
    jobs = Jobs(config.paths['database'])
    prior = [row for row in jobs.list() if row['kind']=='sample' and
             json.loads(row['parameters_json'])['request']['requested_ids']==[identity]]
    job = prior[0]['job_id'] if prior else jobs.submit('sample',[identity],batch_size=1,corrections={})
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0))
        port = listener.getsockname()[1]
    values = dict(config.values,port=port)
    config.config_file.write_text(json.dumps(values,indent=2),encoding='utf-8')
    proofs = []
    try:
        for cycle in (1,2):
            stop = config.home/'.artifacts'/f'proof-stop-{cycle}-{uuid.uuid4().hex}'
            environment = dict(os.environ,MARKET_STOP_FILE=str(stop),PYTHONIOENCODING='utf-8')
            started = subprocess.Popen([sys.executable,'-m','market_system.operations','launch'],cwd=config.home,env=environment,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,
                                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic()+120
                while True:
                    if started.poll() is not None:
                        raise RuntimeError('Launcher exited before readiness')
                    try:
                        response = httpx.get(f'http://127.0.0.1:{port}/login',timeout=2,trust_env=False)
                        if response.status_code==200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic()>deadline:
                        raise TimeoutError('HTTP readiness')
                    time.sleep(.2)
                roles = {}
                for name in ('stage11owner','stage11user'):
                    with httpx.Client(base_url=f'http://127.0.0.1:{port}',follow_redirects=True,timeout=90,trust_env=False) as client:
                        token = unescape(re.search(r'name="csrf" value="([^"]+)"',client.get('/login').text)[1])
                        assert client.post('/login',data={'username':name,'password':password,'csrf':token}).status_code==200
                        assert client.get('/catalog',params={'post_type':'vacancy'}).status_code==200
                        assert client.get(f'/cards/{identity}',params={'result_set_id':page.result_set_id}).status_code==200
                        diagnostic_status = client.get('/diagnostics').status_code
                        assert diagnostic_status==(200 if name.endswith('owner') else 403)
                        roles[name] = diagnostic_status
                assert BotStore(jobs.path).subscriptions(910012)[0]['id']==subscription
                assert BotStore(jobs.path).subscriptions(910011)==[]
                assert not BotStore(jobs.path).delete(910011,subscription)
                deadline = time.monotonic()+120
                while jobs.get(job)['state']!='succeeded':
                    if jobs.get(job)['state']=='failed':
                        raise RuntimeError(jobs.get(job)['error'])
                    if time.monotonic()>deadline:
                        raise TimeoutError('Small processing job')
                    time.sleep(.2)
                proofs.append({'cycle':cycle,'real_http_search':True,'real_http_card':True,'roles':roles,
                               'job':job,'job_state':jobs.get(job)['state'],'subscription_persisted':True})
            finally:
                stop.touch()
                try:
                    code = started.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    started.kill()
                    started.wait()
                    raise
                if code!=0:
                    raise RuntimeError(f'Launcher stop code {code}')
            try:
                httpx.get(f'http://127.0.0.1:{port}/login',timeout=1,trust_env=False)
                raise RuntimeError('Orphan web process still listens')
            except httpx.TransportError:
                pass
        print(json.dumps({'ok':True,'cycles':proofs,'external_services':False,'source_home':str(config.home)},ensure_ascii=False,indent=2))
        (config.home/'launch-verification.json').write_text(json.dumps({'ok':True,'cycles':proofs},indent=2),encoding='utf-8')
    finally:
        # These are fixture-only accounts/IDs; no real credentials or external Telegram is used.
        with state.connection() as con:
            con.execute("UPDATE web_users SET active=0 WHERE username IN ('stage11owner','stage11user')")
            con.execute('DELETE FROM web_sessions')
        store.delete(910012,subscription)
        with closing(connect(jobs.path)) as con:
            con.execute('UPDATE telegram_access SET enabled=0 WHERE user_id IN (910011,910012)')
            con.commit()


if __name__=='__main__':
    main()
