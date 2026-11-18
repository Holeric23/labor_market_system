"""PostgreSQL source registry and initial channel inventory."""
import uuid

import psycopg
import pytest
from database.connection import connect
from database.migrate import migrate
from database.repository import Repository


def test_initial_telegram_sources_are_linked_and_not_scheduled():
    migrate()
    with connect() as con:
        rows = con.execute('''SELECT s.name,s.address,s.enabled,s.schedule_enabled,t.username,t.provenance
          FROM telegram_sources t JOIN sources s ON s.id=t.source_id
          WHERE t.provenance->>'origin'='previous_project_publications' ORDER BY s.name''').fetchall()
        assert len(rows) == 17
        names = {r['name']:r['username'] for r in rows}
        assert names['vdhl'] == 'vdhl_good'
        assert names['uiux_job'] == 'uiux_jobs'
        assert names['smm_jobs'] == 'marketing_rabota_jobs'
        assert names['a-teams'] == 'huggabletalents'
        assert all(r['enabled'] and not r['schedule_enabled'] for r in rows)
        assert all(r['provenance']['historical_publications'] > 0 for r in rows)
        assert con.execute("SELECT count(*) AS n FROM parser_runs WHERE source_id IN (SELECT source_id FROM telegram_sources WHERE provenance->>'origin'='previous_project_publications')").fetchone()['n'] == 0


def test_new_source_registration_and_case_insensitive_update():
    repository = Repository()
    repository.bootstrap_owners({42})
    username = 'channel_'+uuid.uuid4().hex[:12]
    first = repository.save_source(42,'telegram','Канал','https://t.me/'+username.upper(),3600,True)
    second = repository.save_source(42,'telegram','Новое имя','https://t.me/'+username+'/',7200,False)
    assert first == second
    with connect() as con:
        row = con.execute('''SELECT s.*,t.username FROM sources s JOIN telegram_sources t ON t.source_id=s.id
          WHERE s.id=%s''',(first,)).fetchone()
        assert row['username'] == username and row['address'] == 'https://t.me/'+username
        assert row['name'] == 'Новое имя' and not row['enabled'] and row['interval_seconds'] == 7200
    with pytest.raises(psycopg.Error):
        with connect() as con:
            con.execute('UPDATE telegram_sources SET username=%s WHERE source_id=%s',('other_channel',first))
    with pytest.raises(psycopg.Error):
        with connect() as con:
            con.execute('DELETE FROM telegram_sources WHERE source_id=%s',(first,))
