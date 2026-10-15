-- name: insert
INSERT OR IGNORE INTO contents (text, text_hash)
VALUES (?, ?);

-- name: select
SELECT id
FROM contents
WHERE text_hash = ?;

-- name:select_all
SELECT text_hash, text
FROM contents;