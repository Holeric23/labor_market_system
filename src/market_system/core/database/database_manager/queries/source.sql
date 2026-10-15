-- name: insert
INSERT OR IGNORE INTO sources (source_type_id, name)
VALUES (?, ?);

-- name: select
SELECT id
FROM sources
WHERE source_type_id = ?
  AND name = ?;