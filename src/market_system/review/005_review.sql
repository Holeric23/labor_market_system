CREATE TABLE review_revisions (
 content_id INTEGER NOT NULL REFERENCES contents(id), revision INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('draft','confirmed','uncertain')),
 author TEXT, created_at TEXT NOT NULL, annotation_json TEXT NOT NULL CHECK(json_valid(annotation_json)),
 provenance_json TEXT NOT NULL CHECK(json_valid(provenance_json)),
 PRIMARY KEY(content_id,revision)
);
CREATE INDEX review_confirmed ON review_revisions(status,content_id,revision);
CREATE TRIGGER review_revision_immutable BEFORE UPDATE ON review_revisions BEGIN SELECT RAISE(ABORT,'review history is immutable'); END;
CREATE TRIGGER review_revision_no_delete BEFORE DELETE ON review_revisions BEGIN SELECT RAISE(ABORT,'review history is immutable'); END;
CREATE TABLE review_terms (
 term_id TEXT PRIMARY KEY, content_id INTEGER REFERENCES contents(id),
 kind TEXT NOT NULL CHECK(kind IN ('skill','occupation')), code TEXT NOT NULL, name TEXT NOT NULL,
 definition TEXT NOT NULL, proposed_by TEXT NOT NULL, proposed_at TEXT NOT NULL,
 class_id INTEGER REFERENCES classes(class_id), approved_by TEXT, approved_at TEXT,
 CHECK((class_id IS NULL AND approved_by IS NULL AND approved_at IS NULL) OR
       (class_id IS NOT NULL AND approved_by IS NOT NULL AND approved_at IS NOT NULL))
);
CREATE TRIGGER review_term_approved_immutable BEFORE UPDATE ON review_terms WHEN OLD.class_id IS NOT NULL
 BEGIN SELECT RAISE(ABORT,'approved definition is immutable'); END;
CREATE TRIGGER review_term_no_delete BEFORE DELETE ON review_terms BEGIN SELECT RAISE(ABORT,'term history is immutable'); END;
CREATE TABLE result_review_applications (
 result_set_id TEXT NOT NULL, content_id INTEGER NOT NULL, revision INTEGER NOT NULL,
 multiple_offers INTEGER NOT NULL CHECK(multiple_offers IN (0,1)),
 PRIMARY KEY(result_set_id,content_id),
 FOREIGN KEY(result_set_id,content_id) REFERENCES document_processing(result_set_id,content_id),
 FOREIGN KEY(content_id,revision) REFERENCES review_revisions(content_id,revision)
);
CREATE TABLE review_audit_queue (
 content_id INTEGER PRIMARY KEY REFERENCES contents(id), created_at TEXT NOT NULL, author TEXT NOT NULL
);
CREATE TABLE review_datasets (
 dataset_id TEXT PRIMARY KEY, origin TEXT NOT NULL, manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
 imported_at TEXT NOT NULL
);
CREATE TABLE review_memberships (
 dataset_id TEXT NOT NULL REFERENCES review_datasets(dataset_id), external_id TEXT NOT NULL,
 content_id INTEGER REFERENCES contents(id), cohort TEXT NOT NULL, group_key TEXT,
 PRIMARY KEY(dataset_id,external_id)
);
CREATE INDEX review_membership_content ON review_memberships(content_id,cohort);
CREATE TABLE review_type_labels (
 dataset_id TEXT NOT NULL REFERENCES review_datasets(dataset_id), external_id TEXT NOT NULL,
 content_id INTEGER REFERENCES contents(id), label TEXT NOT NULL,
 PRIMARY KEY(dataset_id,external_id)
);
CREATE VIEW review_training_candidates AS
 SELECT DISTINCT r.content_id,r.revision,r.annotation_json FROM review_revisions r
 WHERE r.status='confirmed' AND r.revision=(SELECT MAX(x.revision) FROM review_revisions x WHERE x.content_id=r.content_id AND x.status='confirmed')
 AND EXISTS(SELECT 1 FROM review_memberships m WHERE m.content_id=r.content_id AND m.cohort='train')
 AND NOT EXISTS(SELECT 1 FROM review_memberships m WHERE m.content_id=r.content_id AND m.cohort IN ('test','development','challenge'));
CREATE VIEW salary_occupation_facts AS
 SELECT s.result_set_id,s.content_id,d.class_id,s.salary_from,s.salary_to,s.currency,s.salary_period
 FROM search_catalog s JOIN result_sets rs ON rs.result_set_id=s.result_set_id AND rs.state='ready' JOIN document_classes d USING(result_set_id,content_id) JOIN classes k USING(class_id)
 LEFT JOIN result_review_applications a USING(result_set_id,content_id)
 WHERE s.post_type='vacancy' AND s.salary_scope='document' AND COALESCE(a.multiple_offers,0)=0
 AND s.currency IS NOT NULL AND s.salary_period IS NOT NULL AND (s.salary_from IS NOT NULL OR s.salary_to IS NOT NULL)
 AND k.namespace LIKE 'occupation:%' AND 1=(SELECT count(*) FROM document_classes x JOIN classes c USING(class_id)
   WHERE x.result_set_id=s.result_set_id AND x.content_id=s.content_id AND c.namespace LIKE 'occupation:%');
