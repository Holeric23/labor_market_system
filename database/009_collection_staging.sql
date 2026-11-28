-- Collected posts stay invisible until their source job succeeds.
CREATE TABLE collection_staged_publications (
    run_id uuid NOT NULL REFERENCES parser_runs(id) ON DELETE CASCADE,
    url text NOT NULL,
    text text NOT NULL,
    published_at timestamptz,
    PRIMARY KEY(run_id,url)
);
UPDATE app_settings SET retention_enabled=true WHERE singleton=true;
