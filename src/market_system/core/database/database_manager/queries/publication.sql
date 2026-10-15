-- name: insert
INSERT INTO publications (
    content_id,
    source_id,
    url,
    published_at
)
VALUES (?, ?, ?, ?)
ON CONFLICT(url) DO NOTHING;