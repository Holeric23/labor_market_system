CREATE TABLE model_registry (
 model_version TEXT PRIMARY KEY, adapter TEXT NOT NULL,
 manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
 spec_json TEXT NOT NULL CHECK(json_valid(spec_json)),
 class_space_json TEXT NOT NULL CHECK(json_valid(class_space_json)),
 evaluation_scope TEXT NOT NULL, dependencies_json TEXT NOT NULL CHECK(json_valid(dependencies_json)),
 status TEXT NOT NULL CHECK(status IN ('candidate','approved','active','retired')),
 registered_by TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX one_active_model ON model_registry(status) WHERE status='active';
CREATE TRIGGER model_status_consistent BEFORE UPDATE OF status ON model_registry
 WHEN (NEW.status='active') != EXISTS(SELECT 1 FROM active_result_set a JOIN result_sets r USING(result_set_id) WHERE r.model_version=NEW.model_version)
 BEGIN SELECT RAISE(ABORT,'active model follows the working result set'); END;
CREATE TRIGGER model_definition_immutable BEFORE UPDATE ON model_registry
 WHEN NEW.model_version!=OLD.model_version OR NEW.adapter!=OLD.adapter OR NEW.manifest_json!=OLD.manifest_json
 OR NEW.spec_json!=OLD.spec_json OR NEW.class_space_json!=OLD.class_space_json OR NEW.evaluation_scope!=OLD.evaluation_scope
 OR NEW.dependencies_json!=OLD.dependencies_json OR NEW.registered_by!=OLD.registered_by OR NEW.created_at!=OLD.created_at
 BEGIN SELECT RAISE(ABORT,'model definition is immutable; register a new version'); END;
CREATE TRIGGER model_no_delete BEFORE DELETE ON model_registry BEGIN SELECT RAISE(ABORT,'model history is immutable'); END;
CREATE TABLE model_class_definitions (
 class_id INTEGER PRIMARY KEY REFERENCES classes(class_id), definition_json TEXT NOT NULL CHECK(json_valid(definition_json))
);
CREATE TRIGGER model_class_immutable BEFORE UPDATE ON model_class_definitions BEGIN SELECT RAISE(ABORT,'class semantics are immutable'); END;
CREATE TRIGGER model_class_no_delete BEFORE DELETE ON model_class_definitions BEGIN SELECT RAISE(ABORT,'class semantics are immutable'); END;
CREATE TABLE model_events (
 id INTEGER PRIMARY KEY, model_version TEXT NOT NULL REFERENCES model_registry(model_version),
 action TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, job_id TEXT REFERENCES jobs(job_id)
);
CREATE TRIGGER model_events_immutable BEFORE UPDATE ON model_events BEGIN SELECT RAISE(ABORT,'model audit is immutable'); END;
CREATE TRIGGER model_events_no_delete BEFORE DELETE ON model_events BEGIN SELECT RAISE(ABORT,'model audit is immutable'); END;
CREATE TABLE model_experiments (
 experiment_id TEXT PRIMARY KEY, model_version TEXT REFERENCES model_registry(model_version), kind TEXT NOT NULL,
 parameters_json TEXT NOT NULL CHECK(json_valid(parameters_json)), inputs_json TEXT NOT NULL CHECK(json_valid(inputs_json)),
 provenance_json TEXT NOT NULL CHECK(json_valid(provenance_json)), state TEXT NOT NULL CHECK(state IN ('running','succeeded','failed')),
 metrics_json TEXT CHECK(metrics_json IS NULL OR json_valid(metrics_json)), error TEXT,
 created_at TEXT NOT NULL, finished_at TEXT, actor TEXT NOT NULL
);
CREATE TRIGGER experiment_final_immutable BEFORE UPDATE ON model_experiments WHEN OLD.state!='running'
 BEGIN SELECT RAISE(ABORT,'finished experiment is immutable'); END;
CREATE TRIGGER experiment_inputs_immutable BEFORE UPDATE ON model_experiments
 WHEN NEW.experiment_id!=OLD.experiment_id OR NEW.model_version IS NOT OLD.model_version OR NEW.kind!=OLD.kind
 OR NEW.parameters_json!=OLD.parameters_json OR NEW.inputs_json!=OLD.inputs_json OR NEW.provenance_json!=OLD.provenance_json
 OR NEW.created_at!=OLD.created_at OR NEW.actor!=OLD.actor
 BEGIN SELECT RAISE(ABORT,'experiment inputs are immutable'); END;
CREATE TRIGGER experiment_no_delete BEFORE DELETE ON model_experiments BEGIN SELECT RAISE(ABORT,'experiment history is immutable'); END;
CREATE TRIGGER registry_activate_insert BEFORE INSERT ON active_result_set
 WHEN EXISTS(SELECT 1 FROM model_registry m JOIN result_sets r USING(model_version)
 WHERE r.result_set_id=NEW.result_set_id AND m.status NOT IN ('approved','active'))
 BEGIN SELECT RAISE(ABORT,'model must be approved before activation'); END;
CREATE TRIGGER registry_activate_update BEFORE UPDATE ON active_result_set
 WHEN EXISTS(SELECT 1 FROM model_registry m JOIN result_sets r USING(model_version)
 WHERE r.result_set_id=NEW.result_set_id AND m.status NOT IN ('approved','active'))
 BEGIN SELECT RAISE(ABORT,'model must be approved before activation'); END;
CREATE TRIGGER registry_sync_insert AFTER INSERT ON active_result_set BEGIN
 UPDATE model_registry SET status='approved' WHERE status='active' AND model_version!=(SELECT model_version FROM result_sets WHERE result_set_id=NEW.result_set_id);
 UPDATE model_registry SET status='active' WHERE model_version=(SELECT model_version FROM result_sets WHERE result_set_id=NEW.result_set_id);
END;
CREATE TRIGGER registry_sync_update AFTER UPDATE ON active_result_set BEGIN
 UPDATE model_registry SET status='approved' WHERE status='active' AND model_version!=(SELECT model_version FROM result_sets WHERE result_set_id=NEW.result_set_id);
 UPDATE model_registry SET status='active' WHERE model_version=(SELECT model_version FROM result_sets WHERE result_set_id=NEW.result_set_id);
END;
