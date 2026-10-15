CREATE TABLE scientific_slices (
 slice_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, author TEXT NOT NULL,
 inputs_json TEXT NOT NULL CHECK(json_valid(inputs_json)),
 reports_json TEXT NOT NULL CHECK(json_valid(reports_json)),
 manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)), engine_text TEXT NOT NULL
);
CREATE TABLE scientific_documents (
 slice_id TEXT NOT NULL REFERENCES scientific_slices(slice_id),
 content_id INTEGER NOT NULL REFERENCES contents(id), sha256 TEXT NOT NULL,
 PRIMARY KEY(slice_id,content_id)
);
CREATE TRIGGER scientific_slice_update BEFORE UPDATE ON scientific_slices
 BEGIN SELECT RAISE(ABORT,'scientific slice is immutable'); END;
CREATE TRIGGER scientific_slice_delete BEFORE DELETE ON scientific_slices
 BEGIN SELECT RAISE(ABORT,'scientific slice is immutable'); END;
CREATE TRIGGER scientific_document_update BEFORE UPDATE ON scientific_documents
 BEGIN SELECT RAISE(ABORT,'scientific input is immutable'); END;
CREATE TRIGGER scientific_document_delete BEFORE DELETE ON scientific_documents
 BEGIN SELECT RAISE(ABORT,'scientific input is immutable'); END;
CREATE TRIGGER scientific_text_update BEFORE UPDATE ON contents
 WHEN EXISTS(SELECT 1 FROM scientific_documents WHERE content_id=OLD.id)
 BEGIN SELECT RAISE(ABORT,'scientific input text is immutable'); END;
CREATE TRIGGER scientific_text_delete BEFORE DELETE ON contents
 WHEN EXISTS(SELECT 1 FROM scientific_documents WHERE content_id=OLD.id)
 BEGIN SELECT RAISE(ABORT,'scientific input text is immutable'); END;
