CREATE TABLE managed_sources (
 source_id INTEGER PRIMARY KEY REFERENCES sources(id), kind TEXT NOT NULL CHECK(kind IN ('telegram','designhunters')),
 address TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 parameters_json TEXT NOT NULL CHECK(json_valid(parameters_json)), revision INTEGER NOT NULL DEFAULT 1,
 state TEXT NOT NULL DEFAULT 'idle', error TEXT, last_success_at TEXT,
 cursor_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(cursor_json)), cursor_version INTEGER NOT NULL DEFAULT 0,
 schedule_enabled INTEGER NOT NULL DEFAULT 0 CHECK(schedule_enabled IN (0,1)),
 interval_seconds INTEGER NOT NULL DEFAULT 3600 CHECK(interval_seconds>=60), next_run_at TEXT,
 UNIQUE(kind,address)
);
CREATE TABLE collection_jobs (
 job_id TEXT PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES managed_sources(source_id),
 mode TEXT NOT NULL CHECK(mode IN ('history','new')), config_json TEXT NOT NULL CHECK(json_valid(config_json)),
 cursor_json TEXT NOT NULL CHECK(json_valid(cursor_json)), source_cursor_version INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','cancelled')),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT,
 progress INTEGER NOT NULL DEFAULT 0, batches INTEGER NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0, owner TEXT, error TEXT, actor TEXT NOT NULL
);
CREATE UNIQUE INDEX collection_no_overlap ON collection_jobs(source_id) WHERE state IN ('queued','running');
CREATE UNIQUE INDEX collection_one_running ON collection_jobs(state) WHERE state='running';
CREATE TABLE collection_attempts (
 job_id TEXT NOT NULL REFERENCES collection_jobs(job_id), attempt INTEGER NOT NULL, owner TEXT NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT, outcome TEXT, error TEXT, PRIMARY KEY(job_id,attempt)
);
CREATE TABLE publication_revisions (
 publication_id INTEGER NOT NULL REFERENCES publications(id), revision INTEGER NOT NULL,
 content_id INTEGER NOT NULL REFERENCES contents(id), published_at TEXT, observed_at TEXT,
 external_id TEXT, collection_job_id TEXT REFERENCES collection_jobs(job_id),
 PRIMARY KEY(publication_id,revision)
);
CREATE INDEX publication_revision_content ON publication_revisions(publication_id,content_id);
CREATE TRIGGER publication_revision_immutable BEFORE UPDATE ON publication_revisions
 BEGIN SELECT RAISE(ABORT,'publication revisions are immutable'); END;
CREATE TRIGGER publication_revision_no_delete BEFORE DELETE ON publication_revisions
 BEGIN SELECT RAISE(ABORT,'publication history is immutable'); END;
CREATE VIEW current_publications AS
 SELECT p.id,COALESCE(r.content_id,p.content_id) AS content_id,p.source_id,p.url,
 CASE WHEN r.revision IS NULL THEN p.published_at ELSE r.published_at END AS published_at
 FROM publications p LEFT JOIN publication_revisions r ON r.publication_id=p.id
 AND r.revision=(SELECT MAX(x.revision) FROM publication_revisions x WHERE x.publication_id=p.id);
CREATE TABLE collection_handoffs (
 collection_job_id TEXT NOT NULL REFERENCES collection_jobs(job_id), batch INTEGER NOT NULL,
 content_ids_json TEXT NOT NULL CHECK(json_valid(content_ids_json)),
 processing_job_id TEXT REFERENCES jobs(job_id), error TEXT, next_attempt_at TEXT,
 PRIMARY KEY(collection_job_id,batch)
);
