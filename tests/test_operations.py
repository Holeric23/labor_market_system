import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from contextlib import closing

import pytest
from market_system.paths import PROJECT_ROOT, local_path
from market_system.operations.config import Operations
from market_system.operations.backup import create, restore, verify_bundle
from market_system.operations.logs import JSONFormatter
from market_system.operations.diagnostics import inspect
from market_system.sqlite import connect
from market_system.web.state import WebState
from market_system.telegram_bot.store import BotStore
from market_system.search.store import activate
from test_jobs import jobs, submit, worker


def test_config_boundary_and_listener(tmp_path,monkeypatch):
    filename = tmp_path/'ops.json'
    values = json.loads((PROJECT_ROOT/'config/operations.json').read_text())
    values['host'] = '0.0.0.0'
    filename.write_text(json.dumps(values))
    with pytest.raises(ValueError,match='127.0.0.1'):
        Operations.load(filename)
    values['host'] = '127.0.0.1'
    values['web_state'] = r'C:\Study\Анализ данных\Итоговый проект\Python_research_project\state\web.db'
    filename.write_text(json.dumps(values))
    with pytest.raises(ValueError,match='read-only'):
        Operations.load(filename)
    values['web_state'] = 'state/web.db'
    values['allowed_hosts'] = ['*']
    filename.write_text(json.dumps(values))
    with pytest.raises(ValueError,match='wildcards'):
        Operations.load(filename)
    origin = r'C:\Archived\labor_market_system'
    monkeypatch.setenv('MARKET_PATH_ALIASES',json.dumps({origin:str(tmp_path)}))
    assert local_path(origin+r'\data\app.db')==tmp_path/'data/app.db'
    # Audit guard checks actual OS paths, not the relocation alias.
    assert local_path(PROJECT_ROOT/'data/app.db',relocate=False)==PROJECT_ROOT/'data/app.db'
    with pytest.raises(ValueError):
        local_path(origin+r'\data\app.db',relocate=False)


def test_nested_restore_alias_does_not_remap_current_home(tmp_path):
    code = '''from market_system.paths import local_path,PROJECT_ROOT
assert local_path(PROJECT_ROOT/'data/app.db') == PROJECT_ROOT/'data/app.db'
'''
    environment = dict(os.environ,MARKET_HOME=str(tmp_path),MARKET_PATH_ALIASES=json.dumps({str(PROJECT_ROOT):str(tmp_path)}))
    result = subprocess.run([sys.executable,'-c',code],env=environment,cwd=PROJECT_ROOT,capture_output=True)
    assert result.returncode==0,result.stderr


def test_runtime_guard_imports_external_module_without_bytecode(tmp_path):
    home = tmp_path/'home'
    home.mkdir()
    external = tmp_path/'external'
    external.mkdir()
    (external/'guard_import_probe.py').write_text('VALUE = 42\n')
    code = '''import os, sys
from pathlib import Path
from market_system.runtime import enable_guard
sys.dont_write_bytecode = False
enable_guard()
sys.path.insert(0, sys.argv[1])
import guard_import_probe
assert guard_import_probe.VALUE == 42
assert not (Path(sys.argv[1])/'__pycache__').exists()
assert os.environ['PYTHONDONTWRITEBYTECODE'] == '1'
try:
    (Path(sys.argv[1])/'forbidden.txt').write_text('blocked')
except ValueError:
    pass
else:
    raise AssertionError('External writes must remain blocked')
'''
    environment = dict(os.environ, MARKET_HOME=str(home))
    environment.pop('PYTHONDONTWRITEBYTECODE', None)
    result = subprocess.run([sys.executable, '-c', code, str(external)],
                            env=environment, cwd=PROJECT_ROOT, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_runtime_guard_rejects_old_writes_before_io():
    code = '''from market_system.runtime import enable_guard
from market_system.paths import OLD_PROJECT
import sqlite3
enable_guard()
for action in (lambda:open(OLD_PROJECT+'/stage11-forbidden','w'),lambda:sqlite3.connect(OLD_PROJECT+'/stage11-forbidden.db')):
    try: action()
    except ValueError: pass
    else: raise AssertionError('Old write was permitted')
'''
    result = subprocess.run([sys.executable,'-c',code],cwd=PROJECT_ROOT,capture_output=True)
    assert result.returncode==0,result.stderr


def test_graceful_checkpoint_and_subscription_isolation(jobs):
    stop = []
    identity = submit(jobs)
    worker(jobs,after_batch=lambda _:stop.append(True),should_stop=lambda:bool(stop)).run(once=True)
    assert jobs.get(identity)['state']=='running'
    assert jobs.get(identity)['progress']==1
    worker(jobs).run(once=True)
    assert jobs.get(identity)['state']=='succeeded'
    activate(jobs.path,jobs.get(identity)['result_set_id'])
    bot = BotStore(jobs.path)
    bot.initialize()
    bot.bootstrap_owners({10})
    bot.grant(10,20)
    subscription = bot.subscribe(20,{'post_type':'vacancy'})
    restarted = BotStore(jobs.path)
    assert restarted.subscriptions(10)==[]
    assert not restarted.delete(10,subscription)
    assert restarted.subscriptions(20)[0]['id']==subscription


def small_config(jobs,tmp_path):
    from market_system.model_management.store import Models
    from market_system.search.optimize import optimize
    optimize(jobs.path)
    Models(jobs.path).initialize()
    values = json.loads((PROJECT_ROOT/'config/operations.json').read_text())
    (tmp_path/'src/market_system').mkdir(parents=True)
    (tmp_path/'src/market_system/__init__.py').write_text('')
    for name in ('model','dictionary','dataset'):
        (tmp_path/name).mkdir()
    (tmp_path/'model/metadata.json').write_text('{}')
    skill = tmp_path/'dictionary/skills.json'
    occupation = tmp_path/'dictionary/occupations.json'
    dataset = tmp_path/'dataset/data.json'
    for p in (skill,occupation,dataset):
        p.write_text('{}')
    annotations = tmp_path/'annotations.sqlite'
    with closing(connect(annotations)) as con:
        con.execute('CREATE TABLE annotations(id PRIMARY KEY,value)')
        con.execute("INSERT INTO annotations VALUES(1,'human')")
        con.commit()
    web = WebState(tmp_path/'web.db')
    web.initialize()
    web.create_user('owner','local-fixture-password-11','ADMIN')
    (tmp_path/'state/secrets').mkdir(parents=True)
    (tmp_path/'state/secrets/telegram.env').write_text('BOT_TOKEN=private-fixture')
    paths = dict(database=jobs.path,model=tmp_path/'model',skill_lexicon=skill,occupation_lexicon=occupation,
                 dataset=dataset,annotation_database=annotations,web_state=web.path,bot_state=tmp_path/'bot.db',
                 bot_env=tmp_path/'state/secrets/telegram.env')
    return Operations(tmp_path,tmp_path/'config/operations.json',values,paths)


def test_sqlite_wal_backup_real_restore_and_tamper(jobs,tmp_path):
    config = small_config(jobs,tmp_path)
    identity = submit(jobs)
    worker(jobs).run(once=True)
    activate(jobs.path,jobs.get(identity)['result_set_id'])
    store = BotStore(jobs.path)
    store.bootstrap_owners({10})
    store.grant(10,20)
    subscription = store.subscribe(20,{'post_type':'vacancy'})
    # Leave committed rows in WAL while the writer connection remains open.
    with closing(connect(jobs.path)) as wal:
        wal.execute('PRAGMA journal_mode=WAL')
        pending = submit(jobs)
        bundle = create(config,tmp_path/'.artifacts/snapshot')
        manifest = verify_bundle(bundle)
        assert not list(bundle.rglob('*-wal')) and not list(bundle.rglob('*-shm'))
        assert not any('telegram.env' in p for p in manifest['files'])
        target = restore(bundle,tmp_path/'recovered')
    from market_system.jobs import Jobs
    from market_system.search import Catalog, SearchFilter
    restored_db = target/jobs.path.relative_to(tmp_path)
    assert Jobs(restored_db).get(pending)['state']=='queued'
    assert BotStore(restored_db).subscriptions(20)[0]['id']==subscription
    assert BotStore(restored_db).subscriptions(10)==[]
    page = Catalog(restored_db).search(SearchFilter(post_type='vacancy'))
    assert page.cards
    assert WebState(target/'web.db').authenticate('owner','local-fixture-password-11')
    with pytest.raises(ValueError,match='overwrite'):
        restore(bundle,target)
    with pytest.raises(ValueError,match='overwrite'):
        restore(bundle,PROJECT_ROOT)
    before = jobs.get(pending)
    (bundle/'dictionary/skills.json').write_text('changed')
    with pytest.raises(ValueError,match='checksum'):
        restore(bundle,tmp_path/'rejected')
    assert not (tmp_path/'rejected').exists()
    assert jobs.get(pending)==before


def test_backup_refuses_live_executor(jobs,tmp_path):
    from market_system.jobs.worker import worker_lock
    config = small_config(jobs,tmp_path)
    with worker_lock(jobs.path):
        with pytest.raises(RuntimeError,match='Another worker'):
            create(config,tmp_path/'.artifacts/blocked')
    assert not (tmp_path/'.artifacts/blocked').exists()


def test_json_logs_rotation_and_secrets(tmp_path,monkeypatch):
    from logging.handlers import RotatingFileHandler
    monkeypatch.setenv('BOT_TOKEN','private-test-token')
    handler = RotatingFileHandler(tmp_path/'log.jsonl',maxBytes=1024,backupCount=2)
    handler.setFormatter(JSONFormatter('test'))
    logger = logging.getLogger('stage11-test')
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        for _ in range(50):
            logger.info('private-test-token https://api.telegram.org/botfoo ' + 'x'*100)
    finally:
        logger.removeHandler(handler)
        handler.close()
    paths = list(tmp_path.glob('log.jsonl*'))
    assert len(paths)<=3
    for path in paths:
        for line in path.read_text().splitlines():
            value = json.loads(line)
            assert value['component']=='test'
            assert 'private-test-token' not in line and 'api.telegram.org' not in line


def alive(pid):
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL('kernel32',use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000,False,pid)
        if not handle:
            return False
        code = wintypes.DWORD()
        api.GetExitCodeProcess(handle,ctypes.byref(code))
        api.CloseHandle(handle)
        return code.value==259
    try:
        os.kill(pid,0)
        return True
    except ProcessLookupError:
        return False


def test_launcher_kills_uncooperative_descendant_tree(tmp_path):
    from market_system.operations.launcher import supervise
    script = '''import os,time,subprocess,sys
from pathlib import Path
while not Path(os.environ['MARKET_LAUNCH_GATE']).exists(): time.sleep(.02)
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(300)'])
Path('parent.pid').write_text(str(os.getpid()))
Path('child.pid').write_text(str(child.pid))
time.sleep(300)
'''
    started = time.monotonic()
    def stop():
        return (tmp_path/'child.pid').exists() or time.monotonic()-started>20
    assert supervise([('test',[sys.executable,'-c',script])],tmp_path,seconds=.3,stop_request=stop)==0
    assert (tmp_path/'child.pid').exists()
    assert not alive(int((tmp_path/'parent.pid').read_text()))
    assert not alive(int((tmp_path/'child.pid').read_text()))


@pytest.mark.parametrize('bot_enabled', [False, True])
def test_launch_prints_site_link(monkeypatch, capsys, bot_enabled):
    from market_system.operations import launcher
    from contextlib import nullcontext
    import market_system.jobs.worker as workers
    config = Operations.load()
    monkeypatch.setitem(config.values, 'bot_enabled', bot_enabled)
    monkeypatch.setitem(config.values, 'delivery_enabled', False)
    monkeypatch.setattr(Operations, 'telegram', lambda self: None)
    monkeypatch.setattr(workers, 'worker_lock', lambda path: nullcontext())
    monkeypatch.setattr(launcher, 'supervise', lambda *args, **kwargs: 0)
    assert launcher.launch(config) == 0
    suffix = ' и телеграмм-бота' if bot_enabled else ''
    assert capsys.readouterr().out == f'Запуск веб-системы http://127.0.0.1:8000{suffix}\n'
