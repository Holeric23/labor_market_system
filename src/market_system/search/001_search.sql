CREATE TABLE result_sets (
 result_set_id TEXT PRIMARY KEY, model_version TEXT NOT NULL,
 skills_run_id TEXT NOT NULL, profiles_run_id TEXT NOT NULL,
 versions_json TEXT NOT NULL CHECK(json_valid(versions_json)),
 provenance_json TEXT NOT NULL CHECK(json_valid(provenance_json)),
 coverage_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(coverage_json)),
 state TEXT NOT NULL CHECK(state IN ('building','ready','failed')),
 created_at TEXT NOT NULL, ready_at TEXT, error TEXT
);
CREATE TABLE classes (
 class_id INTEGER PRIMARY KEY, namespace TEXT NOT NULL CHECK(length(namespace)>0),
 class_key TEXT NOT NULL CHECK(length(class_key)>0), name TEXT NOT NULL,
 definition_version TEXT NOT NULL, UNIQUE(namespace,class_key)
);
CREATE TABLE document_processing (
 result_set_id TEXT NOT NULL REFERENCES result_sets(result_set_id),
 content_id INTEGER NOT NULL REFERENCES contents(id),
 status TEXT NOT NULL CHECK(status IN ('processed','no_classes')),
 PRIMARY KEY(result_set_id,content_id)
);
CREATE TABLE document_classes (
 result_set_id TEXT NOT NULL, class_id INTEGER NOT NULL REFERENCES classes(class_id),
 content_id INTEGER NOT NULL, role TEXT NOT NULL DEFAULT 'none' CHECK(length(trim(role))>0),
 score REAL CHECK(score IS NULL OR (score>=0 AND score<=1)),
 PRIMARY KEY(result_set_id,class_id,content_id,role),
 FOREIGN KEY(result_set_id,content_id) REFERENCES document_processing(result_set_id,content_id)
);
CREATE INDEX document_classes_forward ON document_classes(result_set_id,class_id,role,content_id);
CREATE INDEX document_classes_reverse ON document_classes(result_set_id,content_id,class_id,role);
CREATE TABLE search_catalog (
 result_set_id TEXT NOT NULL, content_id INTEGER NOT NULL,
 post_type TEXT CHECK(post_type IN ('vacancy','resume')),
 published_at_utc TEXT, publication_count INTEGER NOT NULL CHECK(publication_count>=0),
 salary_from REAL, salary_to REAL, currency TEXT, salary_period TEXT, tax_mode TEXT,
 work_format TEXT, schedule TEXT, employment_type TEXT, working_hours TEXT, hours_per_week INTEGER,
 review_status TEXT NOT NULL CHECK(review_status IN ('unreviewed','needs_review')),
 salary_scope TEXT NOT NULL CHECK(salary_scope IN ('document','unattributed')),
 PRIMARY KEY(result_set_id,content_id),
 FOREIGN KEY(result_set_id,content_id) REFERENCES document_processing(result_set_id,content_id)
);
CREATE INDEX catalog_date ON search_catalog(result_set_id,post_type,published_at_utc,content_id);
CREATE INDEX catalog_salary ON search_catalog(result_set_id,currency,salary_period,salary_from,content_id);
CREATE TABLE result_evidence (
 result_set_id TEXT NOT NULL, content_id INTEGER NOT NULL,
 classification_json TEXT NOT NULL CHECK(json_valid(classification_json)),
 profile_json TEXT CHECK(profile_json IS NULL OR json_valid(profile_json)),
 skills_json TEXT NOT NULL CHECK(json_valid(skills_json)),
 PRIMARY KEY(result_set_id,content_id),
 FOREIGN KEY(result_set_id,content_id) REFERENCES document_processing(result_set_id,content_id)
);
CREATE TABLE result_publications (
 result_set_id TEXT NOT NULL, content_id INTEGER NOT NULL,
 publication_id INTEGER NOT NULL REFERENCES publications(id), published_at_utc TEXT,
 PRIMARY KEY(result_set_id,publication_id),
 FOREIGN KEY(result_set_id,content_id) REFERENCES document_processing(result_set_id,content_id)
);
CREATE INDEX result_publications_content ON result_publications(result_set_id,content_id,publication_id);
CREATE TABLE active_result_set (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 result_set_id TEXT NOT NULL REFERENCES result_sets(result_set_id)
);
CREATE TABLE result_set_switches (
 id INTEGER PRIMARY KEY, previous_id TEXT REFERENCES result_sets(result_set_id),
 result_set_id TEXT NOT NULL REFERENCES result_sets(result_set_id), switched_at TEXT NOT NULL
);
CREATE TRIGGER active_insert BEFORE INSERT ON active_result_set
 WHEN (SELECT state FROM result_sets WHERE result_set_id=NEW.result_set_id) IS NOT 'ready'
 BEGIN SELECT RAISE(ABORT,'active result set must be ready'); END;
CREATE TRIGGER active_update BEFORE UPDATE ON active_result_set
 WHEN (SELECT state FROM result_sets WHERE result_set_id=NEW.result_set_id) IS NOT 'ready'
 BEGIN SELECT RAISE(ABORT,'active result set must be ready'); END;
CREATE TRIGGER result_set_immutable BEFORE UPDATE ON result_sets WHEN OLD.state!='building'
 BEGIN SELECT RAISE(ABORT,'finished result set is immutable'); END;
CREATE TRIGGER class_immutable BEFORE UPDATE ON classes
 BEGIN SELECT RAISE(ABORT,'create a new definition namespace instead'); END;
CREATE TRIGGER class_no_delete BEFORE DELETE ON classes
 BEGIN SELECT RAISE(ABORT,'class registry is append-only'); END;
CREATE TRIGGER ready_coverage BEFORE UPDATE OF state ON result_sets
 WHEN NEW.state='ready' AND (
 json_type(NEW.coverage_json,'$.expected') IS NOT 'integer' OR
 json_extract(NEW.coverage_json,'$.expected') != (SELECT count(*) FROM document_processing WHERE result_set_id=NEW.result_set_id) OR
 json_extract(NEW.coverage_json,'$.expected') != (SELECT count(*) FROM search_catalog WHERE result_set_id=NEW.result_set_id) OR
 json_extract(NEW.coverage_json,'$.expected') != (SELECT count(*) FROM result_evidence WHERE result_set_id=NEW.result_set_id))
 BEGIN SELECT RAISE(ABORT,'incomplete result set'); END;
CREATE TRIGGER result_set_initial_state BEFORE INSERT ON result_sets WHEN NEW.state!='building'
 BEGIN SELECT RAISE(ABORT,'new sets must start building'); END;
CREATE TRIGGER result_set_no_delete BEFORE DELETE ON result_sets
 BEGIN SELECT RAISE(ABORT,'result history is append-only'); END;
