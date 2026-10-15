"""Stage 12 acceptance: real local Ensemble; only source and delivery are simulated.

Creates an independent home below .artifacts. Never imports the old project,
trains a model, changes the working catalog, polls or sends to Telegram.
"""
import argparse
import asyncio
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from html import unescape
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import zipfile
import io
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def deny_external_io():
    old = Path(r'C:\Study\Анализ данных\Итоговый проект\Python_research_project').resolve()
    attempts = []
    def audit(event, args):
        if event in ('socket.connect', 'socket.bind'):
            # Windows asyncio builds its self-pipe with a loopback socket pair.
            address = args[1]
            if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1'):
                raise PermissionError('Acceptance is offline; external network I/O forbidden')
        if event not in ('open', 'sqlite3.connect', 'os.listdir', 'os.scandir'):
            return
        if not isinstance(args[0], (str, bytes, os.PathLike)):
            return
        value = os.fsdecode(args[0])
        if value.startswith('file:'):
            value = unquote(urlsplit(value).path)
            if os.name == 'nt' and value.startswith('/'):
                value = value[1:]
        if Path(value).resolve().is_relative_to(old):
            attempts.append(value)
            raise PermissionError('Old project I/O forbidden')
    sys.addaudithook(audit)
    return attempts


def source_feed(config, mode, cursor):
    from market_system.collection.runner import Batch
    from market_system.core.utils.dataclasses import RawPublication
    async def iterator():
        position = cursor['source'].get('high_water', 0)
        items = json.loads((ROOT / 'scripts/control-feed.json').read_text(encoding='utf-8'))[config['name']]
        for index in range(position, len(items)):
            current = deepcopy(cursor)
            current['source']['high_water'] = index + 1
            yield Batch([RawPublication('telegram', config['name'], items[index],
                        config['address'] + '/' + str(index + 1), datetime(2026, 10, 13, tzinfo=timezone.utc))],
                        current, index == len(items) - 1)
            cursor['source']['high_water'] = index + 1
        if position == len(items):
            yield Batch([], cursor, True)
    return iterator()


def child(mode):
    attempts = deny_external_io()
    from market_system.runtime import enable_guard
    enable_guard()
    from market_system.paths import DATABASE_PATH
    from market_system.search.store import connect
    from market_system.search import Catalog, SearchFilter
    from market_system.jobs import Jobs
    from market_system.jobs.worker import Worker
    from market_system.model_management.store import Models
    from market_system.model_management.adapters import load
    from market_system.operations.compatibility import resolved_spec
    from market_system.operations.config import Operations
    from market_system.review.store import Actor
    from market_system.analytics.store import filters
    owner = Actor('acceptance-owner', 'OWNER')
    config = Operations.load()
    models = Models(DATABASE_PATH)
    sources = models.analytics.bot.review.sources
    bot = models.analytics.bot
    review = bot.review
    def rows(sql, params=()):
        with connect(DATABASE_PATH) as con:
            return [dict(r) for r in con.execute(sql, params)]
    def active():
        return rows('SELECT result_set_id FROM active_result_set')[0]['result_set_id']
    def process(job, sender=None, **kwargs):
        Worker(models.jobs, collection_adapter=source_feed, telegram_sender=sender, **kwargs).run(once=True)
        record = models.jobs.get(job)
        assert record['state'] == 'succeeded', record['error']
        return record['result_set_id']
    if mode == 'crash':
        Worker(Jobs(DATABASE_PATH), after_batch=lambda _: os._exit(17)).run(once=True)
        raise AssertionError('Expected process exit after committed batch')
    if mode == 'resume':
        resume = json.loads((ROOT / 'scripts/resume.json').read_text(encoding='utf-8'))
        observed = []
        def factory(spec):
            predictor = load(resolved_spec(spec))
            def predict(texts):
                observed.extend(hashlib.sha256(t.encode()).hexdigest() for t in texts)
                return predictor.predict(texts)
            return predict
        job = resume['job']
        assert models.jobs.get(job)['state'] == 'running'
        assert models.jobs.get(job)['progress'] == 1
        checkpoint = models.jobs.get(job)['checkpoint']
        expected = [r['text_sha256'] for r in rows('SELECT text_sha256 FROM job_inputs WHERE job_id=? AND needs_analysis=1 AND content_id>? ORDER BY content_id', (job, checkpoint))]
        rid = process(job, analyzer_factory=factory)
        assert models.jobs.get(job)['attempts'] == 2
        assert len(observed) == models.jobs.get(job)['total'] - 1
        assert observed == expected
        models.publish(rid, owner)
        assert Catalog(DATABASE_PATH).get(resume['corrected'], active())['post_type'] == 'resume'
        assert models.analytics.load(resume['slice'], owner) == resume['slice_value']
        assert bot.subscriptions(20)[0]['id'] == resume['subscription']
        cursor_before = sources.get(resume['source'])['cursor_json']
        publications_before = rows('SELECT count(*) n FROM publications')[0]['n']
        collection = sources.enqueue(resume['source'], 'new')
        Worker(models.jobs, collection_adapter=source_feed).run(once=True)
        assert sources.job(collection)['state'] == 'succeeded'
        assert sources.job(collection)['progress'] == 0
        assert sources.get(resume['source'])['cursor_json'] == cursor_before
        assert rows('SELECT count(*) n FROM publications')[0]['n'] == publications_before
        from market_system.operations.diagnostics import inspect
        diagnostics = inspect(config, full=True)
        assert diagnostics['ok'], diagnostics['errors']
        proof = dict(ok=True, resumed_job=job, reprocessed_after_checkpoint=len(observed),
                     total=models.jobs.get(job)['total'], collection_reimported=0,
                     scientific_slice_unchanged=True, correction_preserved=True,
                     subscription_preserved=True, old_project_access_attempts=attempts,
                     diagnostics=diagnostics)
        write(ROOT / 'stage12-resume.json', proof)
        print('RESTORED CHECKPOINT, SOURCE CURSOR, CORRECTION AND SLICE: PASS')
        return

    models.initialize()
    from market_system.search.optimize import optimize
    optimize(DATABASE_PATH)
    source = sources.save('telegram', 'control', 'https://t.me/control', {'batch_size':1})
    empty_source = sources.save('telegram', 'control_empty', 'https://t.me/control_empty', {'batch_size':1})
    def collect(identity):
        from market_system.collection.runner import Collector
        collector = Collector(sources, 'acceptance-source', source_feed)
        assert collector.claim() == identity
        collector.execute(identity)
        assert sources.job(identity)['state'] == 'succeeded'
    collect(sources.enqueue(empty_source, 'history'))
    version = models.register(owner)
    baseline_job = models.designate(version, owner, batch_size=1)
    baseline = process(baseline_job)
    models.publish(baseline, owner)
    # Clear the durable initial handoff through the normal explicit retry.
    handoff = sources.dispatch(force=True)
    assert handoff
    baseline = process(handoff)
    models.publish(baseline, owner)
    assert rows('SELECT status FROM document_processing WHERE result_set_id=?', (baseline,)) == [{'status':'no_classes'}]
    assert models.jobs.get(handoff)['total'] == 0
    bot.bootstrap_owners({10})
    bot.grant(10, 20)
    subscription = bot.subscribe(20, {'post_type':'vacancy'})
    sent = []
    def sender(user, text):
        sent.append((user, text))
        return 1000 + len(sent)
    collection = sources.enqueue(source, 'history')
    Worker(models.jobs, collection_adapter=source_feed).run(once=True)
    assert sources.job(collection)['state'] == 'succeeded'
    job = next(h['processing_job_id'] for h in sources.handoffs() if h['collection_job_id'] == collection)
    ready = process(job, sender=sender)
    Worker(models.jobs, telegram_sender=sender).run(once=True)
    models.publish(ready, owner)
    cards = Catalog(DATABASE_PATH).search(SearchFilter(post_type='vacancy')).cards
    assert len(cards) == 2, [(c['content_id'], c['post_type']) for c in cards]
    corrected, unchanged = sorted(c['content_id'] for c in cards)
    assert len(sent) == 2 and all(u == 20 for u, _ in sent)
    assert all(r['state'] == 'sent' and r['message_id'] for r in rows('SELECT * FROM telegram_deliveries'))
    assert rows('SELECT count(*) n FROM contents')[0]['n'] == 3
    assert rows('SELECT count(*) n FROM publications')[0]['n'] == 4  # one exact repost
    links = rows('SELECT d.*,c.namespace,c.class_key FROM document_classes d JOIN classes c USING(class_id) WHERE result_set_id=? AND content_id=?', (ready, corrected))
    assert len({r['class_id'] for r in links}) >= 3
    skill = next(r['class_id'] for r in links if r['class_key'] == 'python' and r['role'] == 'required')
    with connect(DATABASE_PATH) as con:
        schema_before = [tuple(r) for r in con.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name")]
        indexes = {r['name']: [c['name'] for c in con.execute('PRAGMA index_info("' + r['name'] + '")')]
                   for r in con.execute("PRAGMA index_list('document_classes')")}
        assert any(columns[:3] == ['result_set_id','class_id','content_id'] for columns in indexes.values())
        assert any(columns[:3] == ['result_set_id','content_id','class_id'] for columns in indexes.values())
        plan = [tuple(r) for r in con.execute('EXPLAIN QUERY PLAN SELECT content_id FROM document_classes WHERE result_set_id=? AND class_id=?', (ready, skill))]
        assert any('INDEX' in r[-1] and 'SEARCH' in r[-1] for r in plan)
        for table in ('classes','document_classes','search_catalog'):
            assert 'text' not in [r['name'] for r in con.execute('PRAGMA table_info(' + table + ')')]
    # Actual HTTP route and Telegram controller consume the same skill filter.
    from fastapi.testclient import TestClient
    from market_system.web.app import WebConfig, create_app, filter_input
    from starlette.datastructures import QueryParams
    from market_system.telegram_bot.catalog import CatalogSearch
    from market_system.telegram_bot.unified import Controller
    from market_system.telegram_bot.infrastructure.sqlite_state import StateDatabase, SQLiteUserRepository
    query = 'post_type=vacancy&skill_ids=' + str(skill)
    normalized, _, _ = filter_input(QueryParams(query), {'post_type':'vacancy','page_size':20})
    page = Catalog(DATABASE_PATH).search(normalized, page_size=20)
    assert asyncio.run(CatalogSearch(DATABASE_PATH).search(normalized.normalized(), page_size=20)) == page
    class Telegram:
        def __init__(self): self.sent = []
        async def send_message(self, user, text, *args, **kwargs): self.sent.append((user,text)); return len(self.sent)
        async def delete_message(self, *args): pass
    telegram = Telegram()
    users = SQLiteUserRepository(StateDatabase(config.paths['bot_state']), {10})
    controller = Controller(bot, users, telegram)
    asyncio.run(controller.command(20, '/search ' + json.dumps({'post_type':'vacancy','skill_ids':[skill]})))
    telegram_ids = sorted(int(re.search(r'документ (\d+)', text)[1]) for _, text in telegram.sent if re.search(r'документ (\d+)', text))
    app = create_app(WebConfig(DATABASE_PATH, config.paths['web_state'], allowed_hosts=('testserver',)))
    password = 'Autonomous-acceptance-only-42'
    with TestClient(app) as client:
        state = app.state.web_state
        state.create_user('acceptance_owner', password, 'ADMIN')
        state.create_user('acceptance_user', password, 'USER')
        token = unescape(re.search(r'name="csrf" value="([^"]+)"', client.get('/login').text)[1])
        assert client.post('/login', data={'username':'acceptance_user','password':password,'csrf':token}).status_code == 200
        response = client.get('/catalog?' + query)
        assert response.status_code == 200
        web_ids = sorted(set(int(i) for i in re.findall(r'/cards/(\d+)\?', response.text)))
        assert web_ids == telegram_ids == sorted(c['content_id'] for c in page.cards)
        for url in ('/settings','/sources','/jobs','/review','/analytics','/models','/diagnostics'):
            assert client.get(url).status_code == 403
        assert client.post('/jobs', data={'kind':'full'}).status_code == 403
    assert bot.subscriptions(10) == [] and not bot.delete(10, subscription)
    try:
        bot.grant(20, 99)
    except PermissionError:
        pass
    else:
        raise AssertionError('USER gained admin access')
    before_report = models.analytics.report(filters(), owner)
    scientific = models.analytics.save(filters(), owner, active())
    slice_value = models.analytics.load(scientific, owner)
    annotation = deepcopy(review.get(corrected, owner)['annotation'])
    annotation.update(post_type='resume', skills=[], occupations=[], salary=[])
    review.save(corrected, 0, 'confirmed', annotation, owner, active())
    assert corrected not in [c['content_id'] for c in Catalog(DATABASE_PATH).search(normalized).cards]
    assert models.analytics.report(filters(), owner)['reports'] != before_report['reports']
    assert Catalog(DATABASE_PATH).get(corrected, ready)['post_type'] == 'vacancy'
    rerun = models.jobs.submit('incremental', ids=[corrected], batch_size=1)
    rerun_ready = process(rerun)
    models.publish(rerun_ready, owner)
    assert Catalog(DATABASE_PATH).get(corrected, active())['post_type'] == 'resume'
    assert len(rows('SELECT * FROM telegram_outbox')) == 2
    # Copy unchanged fitted weights; alter declared bundle version and skill definitions.
    # This is a compatibility control, not evidence of a newly trained/better model.
    candidate = ROOT / 'models/acceptance_candidate'
    shutil.copytree(config.paths['model'], candidate)
    metadata = json.loads((candidate / 'metadata.json').read_text(encoding='utf-8'))
    metadata.update(model_version='acceptance_candidate', evaluation_scope='Offline acceptance; unchanged fitted weights')
    write(candidate / 'metadata.json', metadata)
    dictionary = json.loads(config.paths['skill_lexicon'].read_text(encoding='utf-8'))
    dictionary['version'] = 'acceptance_changed_v1'
    for definition in dictionary['skills']:
        if definition['id'] == 'python': definition['id'] = 'python_acceptance'
    dictionary['skills'].append(dict(id='acceptance_control', name='AcceptanceControl', category='testing', aliases=['AcceptanceControl']))
    new_dictionary = ROOT / 'data/dictionaries/acceptance-skills.json'
    write(new_dictionary, dictionary)
    from market_system.runtime import load_config
    candidate_config = load_config()
    candidate_config.update(model=candidate, skill_lexicon=new_dictionary)
    new_version = models.register(owner, candidate_config)
    stable = active()
    build = models.designate(new_version, owner, batch_size=1)
    # Real transactional failure; the predictor itself is not mocked.
    with connect(DATABASE_PATH) as con:
        con.execute("CREATE TRIGGER acceptance_write_failure BEFORE INSERT ON result_evidence WHEN NEW.content_id=" + str(unchanged) + " BEGIN SELECT RAISE(ABORT,'acceptance injected storage failure'); END")
        con.commit()
    Worker(models.jobs).run(once=True)
    assert models.jobs.get(build)['state'] == 'failed'
    assert active() == stable
    assert Catalog(DATABASE_PATH).search(normalized).cards
    try:
        models.publish(models.jobs.get(build)['result_set_id'], owner)
    except ValueError:
        pass
    else:
        raise AssertionError('Failed build was published')
    with connect(DATABASE_PATH) as con:
        con.execute('DROP TRIGGER acceptance_write_failure'); con.commit()
    models.jobs.retry(build)
    new_ready = process(build)
    assert active() == stable
    changed_links = rows('SELECT c.class_key FROM document_classes d JOIN classes c USING(class_id) WHERE d.result_set_id=? AND d.content_id=?', (new_ready, unchanged))
    assert 'python_acceptance' in [r['class_key'] for r in changed_links]
    assert 'python' not in [r['class_key'] for r in changed_links]
    assert rows("SELECT count(*) n FROM classes WHERE class_key='acceptance_control'")[0]['n'] == 1
    with connect(DATABASE_PATH) as con:
        assert schema_before == [tuple(r) for r in con.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name")]
    models.publish(new_ready, owner)
    assert active() == new_ready
    assert Catalog(DATABASE_PATH).get(corrected, active())['post_type'] == 'resume'
    models.publish(stable, owner)
    assert active() == stable
    assert models.analytics.load(scientific, owner) == slice_value
    archive = models.analytics.export(slice_value, owner)
    replay_folder = ROOT / '.artifacts/scientific-replay'
    replay_folder.mkdir()
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        for name in ('inputs.json', 'reports.json', 'manifest.json', 'engine.py'):
            (replay_folder / name).write_bytes(zipped.read(name))
    replay = subprocess.run([sys.executable, str(replay_folder / 'engine.py'), str(replay_folder / 'inputs.json'), str(replay_folder / 'replay.json')], cwd=ROOT)
    assert replay.returncode == 0
    manifest = json.loads((replay_folder / 'manifest.json').read_text(encoding='utf-8'))
    assert sha(replay_folder / 'replay.json') == manifest['files_sha256']['reports.json']
    # Leave a real process-death checkpoint for the backup/restore scenario.
    pending = models.jobs.submit('incremental', ids=[corrected, unchanged], batch_size=1)
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--mode','crash'], cwd=ROOT)
    assert result.returncode == 17
    assert models.jobs.get(pending)['progress'] == 1 and models.jobs.get(pending)['state'] == 'running'
    write(ROOT / 'scripts/resume.json', dict(job=pending, corrected=corrected, source=source,
          slice=scientific, slice_value=slice_value, subscription=subscription))
    from market_system.operations.backup import create, restore, verify_bundle
    bundle = create(config, ROOT / 'backups/acceptance')
    verify_bundle(bundle)
    target = restore(bundle, ROOT / '.artifacts/restored')
    env = dict(os.environ, MARKET_HOME=str(target), PYTHONPATH=str(target / 'src'))
    for key in ('MARKET_RUNTIME_CONFIG','MARKET_OPERATIONS_CONFIG','MARKET_WEB_CATALOG','MARKET_WEB_STATE','MARKET_PATH_ALIASES'):
        env.pop(key, None)
    result = subprocess.run([sys.executable, str(target / 'scripts/check_stage12.py'), '--mode','resume'], env=env, cwd=target)
    assert result.returncode == 0
    resumed = json.loads((target / 'stage12-resume.json').read_text(encoding='utf-8'))
    assert not attempts
    proof = dict(ok=True, steps_passed=list(range(1,13)), real_model_version=version,
                 real_predictor=True, fitted_weights_unchanged_for_candidate=True,
                 simulated_boundaries=['source batches','Telegram transport'],
                 document_ids=[corrected, unchanged], web_ids=web_ids, telegram_ids=telegram_ids,
                 sent_simulated=len(sent), publications=4, unique_contents=3,
                 source_without_classes_status='no_classes', incremental_no_classes_total=0,
                 schema_unchanged=True, indexes=indexes, query_plan=plan,
                 failed_build=models.jobs.get(build)['attempts'] == 2,
                 scientific_slice=scientific, scientific_slice_unchanged=True,
                 scientific_archive_replayed=True,
                 old_project_access_attempts=attempts, resume=resumed,
                 live_integrations_tested=False, training=False)
    write(ROOT / 'stage12-acceptance.json', proof)
    print('ALL 12 AUTONOMOUS ACCEPTANCE STEPS: PASS')


def parent():
    from market_system.paths import PROJECT_ROOT, DATABASE_PATH
    from market_system.runtime import load_config, enable_guard
    enable_guard()
    before = sha(DATABASE_PATH)
    config = load_config()
    target = PROJECT_ROOT / '.artifacts' / ('stage12-control-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S'))
    target.mkdir()
    for name in ('config','data/dictionaries','models','scripts','state','.artifacts'):
        (target / name).mkdir(parents=True, exist_ok=True)
    shutil.copytree(PROJECT_ROOT / 'src/market_system', target / 'src/market_system', ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    shutil.copytree(config['model'], target / 'models/baseline')
    shutil.copyfile(config['skill_lexicon'], target / 'data/dictionaries/skills.json')
    shutil.copyfile(config['occupation_lexicon'], target / 'data/dictionaries/occupations.json')
    # Dedicated minimal legacy annotation input; unified review is used in the scenario.
    write(target / 'data/dataset.json', {'dataset_id':'acceptance','items':[],'skills':[],'occupations':[]})
    with closing(sqlite3.connect(target / 'data/annotations.sqlite')) as con:
        con.execute('CREATE TABLE revisions(id,revision,annotation,saved_at,dataset,status)'); con.commit()
    with closing(sqlite3.connect(DATABASE_PATH.as_uri() + '?mode=ro', uri=True)) as con:
        text = con.execute('SELECT text FROM contents WHERE id=134323').fetchone()[0]
    text += '\nТребования: знание Python, SQL, Figma. AcceptanceControl.\nКонтрольная публикация приёмки.'
    write(target / 'scripts/control-feed.json', {'control_empty':['https://example.org'],
          'control':[text, text, text + '\nВторая контрольная публикация.']})
    write(target / 'config/runtime.json', dict(database='data/app.db', model='models/baseline',
          skill_lexicon='data/dictionaries/skills.json', occupation_lexicon='data/dictionaries/occupations.json',
          annotation_database='data/annotations.sqlite', dataset='data/dataset.json'))
    shutil.copyfile(PROJECT_ROOT / 'config/operations.json', target / 'config/operations.json')
    shutil.copyfile(Path(__file__), target / 'scripts/check_stage12.py')
    env = dict(os.environ, MARKET_HOME=str(target), PYTHONPATH=str(target / 'src'), PYTHONDONTWRITEBYTECODE='1')
    for key in ('MARKET_RUNTIME_CONFIG','MARKET_OPERATIONS_CONFIG','MARKET_WEB_CATALOG','MARKET_WEB_STATE','MARKET_PATH_ALIASES'):
        env.pop(key, None)
    result = subprocess.run([sys.executable, str(target / 'scripts/check_stage12.py'), '--mode','scenario'], env=env, cwd=target)
    after = sha(DATABASE_PATH)
    write(PROJECT_ROOT / '.artifacts/stage12-run.json', dict(root=str(target), exit_code=result.returncode,
          working_database_sha256_before=before, working_database_sha256_after=after,
          working_database_unchanged=before == after))
    assert before == after, 'Working database changed'
    if result.returncode:
        raise SystemExit(result.returncode)
    shutil.copyfile(target / 'stage12-acceptance.json', PROJECT_ROOT / '.artifacts/stage12-acceptance.json')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('parent','scenario','crash','resume'), default='parent')
    mode = parser.parse_args().mode
    parent() if mode == 'parent' else child(mode)
