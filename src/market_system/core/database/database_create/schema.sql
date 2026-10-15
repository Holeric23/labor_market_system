PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS source_types (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    CHECK (trim(name) <> '')
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    source_type_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    UNIQUE (source_type_id, name),
    FOREIGN KEY (source_type_id)
        REFERENCES source_types(id)
        ON DELETE RESTRICT
        ON UPDATE RESTRICT,
    CHECK (trim(name) <> '')
);

CREATE TABLE IF NOT EXISTS contents (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL UNIQUE,
    CHECK (trim(text) <> '')
);

CREATE TABLE IF NOT EXISTS publications (
    id INTEGER PRIMARY KEY,
    content_id INTEGER NOT NULL,
    source_id INTEGER NOT NULL,
    url TEXT NOT NULL UNIQUE,
    published_at TEXT NULL,
    FOREIGN KEY (content_id)
        REFERENCES contents(id)
        ON DELETE RESTRICT
        ON UPDATE RESTRICT,
    FOREIGN KEY (source_id)
        REFERENCES sources(id)
        ON DELETE RESTRICT
        ON UPDATE RESTRICT,
    CHECK (trim(url) <> '')
);

CREATE INDEX IF NOT EXISTS idx_contents_text_hash
    ON contents(text_hash);

CREATE INDEX IF NOT EXISTS idx_publications_content_id
    ON publications(content_id);