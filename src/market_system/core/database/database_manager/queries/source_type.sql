-- name: insert
INSERT OR IGNORE INTO source_types (name)
VALUES (?);

-- name: select
SELECT id
FROM source_types
WHERE name = ?;