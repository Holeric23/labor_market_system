CREATE TABLE jobs (
 job_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('full','incremental','sample')),
 parameters_json TEXT NOT NULL CHECK(json_valid(parameters_json)),
 state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','cancelled')),
 created_at TEXT NOT NULL, started_at TEXT, updated_at TEXT NOT NULL, finished_at TEXT,
 progress INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL,
 error TEXT, attempts INTEGER NOT NULL DEFAULT 0, checkpoint INTEGER NOT NULL DEFAULT 0,
 owner TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
 result_set_id TEXT NOT NULL UNIQUE REFERENCES result_sets(result_set_id),
 actor TEXT NOT NULL, idempotency_key TEXT UNIQUE,
 CHECK(progress>=0 AND progress<=total), CHECK(attempts>=0)
);
CREATE UNIQUE INDEX jobs_one_running ON jobs(state) WHERE state='running';
CREATE TABLE job_inputs (
 job_id TEXT NOT NULL REFERENCES jobs(job_id), content_id INTEGER NOT NULL REFERENCES contents(id),
 text_sha256 TEXT NOT NULL, needs_analysis INTEGER NOT NULL CHECK(needs_analysis IN (0,1)),
 correction_json TEXT CHECK(correction_json IS NULL OR json_valid(correction_json)),
 PRIMARY KEY(job_id,content_id)
);
CREATE TABLE job_publications (
 job_id TEXT NOT NULL REFERENCES jobs(job_id), content_id INTEGER NOT NULL,
 publication_id INTEGER NOT NULL REFERENCES publications(id), published_at_utc TEXT,
 PRIMARY KEY(job_id,publication_id),
 FOREIGN KEY(job_id,content_id) REFERENCES job_inputs(job_id,content_id)
);
CREATE TABLE job_attempts (
 job_id TEXT NOT NULL REFERENCES jobs(job_id), attempt INTEGER NOT NULL,
 owner TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, outcome TEXT, error TEXT,
 PRIMARY KEY(job_id,attempt)
);
CREATE TABLE job_models (
 model_version TEXT PRIMARY KEY, manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json))
);
CREATE TRIGGER job_input_immutable BEFORE UPDATE ON job_inputs
 BEGIN SELECT RAISE(ABORT,'job input is immutable'); END;
CREATE TRIGGER job_input_no_delete BEFORE DELETE ON job_inputs
 BEGIN SELECT RAISE(ABORT,'job input is immutable'); END;
CREATE TRIGGER jobs_ready_coverage BEFORE UPDATE OF state ON result_sets
 WHEN NEW.state='ready' AND EXISTS(SELECT 1 FROM jobs WHERE result_set_id=NEW.result_set_id)
 AND (EXISTS(SELECT content_id FROM job_inputs WHERE job_id=(SELECT job_id FROM jobs WHERE result_set_id=NEW.result_set_id)
             EXCEPT SELECT content_id FROM document_processing WHERE result_set_id=NEW.result_set_id)
   OR EXISTS(SELECT content_id FROM document_processing WHERE result_set_id=NEW.result_set_id
             EXCEPT SELECT content_id FROM job_inputs WHERE job_id=(SELECT job_id FROM jobs WHERE result_set_id=NEW.result_set_id)))
 BEGIN SELECT RAISE(ABORT,'input coverage mismatch'); END;
CREATE TRIGGER sample_not_active_insert BEFORE INSERT ON active_result_set
 WHEN EXISTS(SELECT 1 FROM jobs WHERE result_set_id=NEW.result_set_id AND kind='sample')
 BEGIN SELECT RAISE(ABORT,'sample cannot be activated'); END;
CREATE TRIGGER sample_not_active_update BEFORE UPDATE ON active_result_set
 WHEN EXISTS(SELECT 1 FROM jobs WHERE result_set_id=NEW.result_set_id AND kind='sample')
 BEGIN SELECT RAISE(ABORT,'sample cannot be activated'); END;
