-- The common source row continues to own scheduling, publications and runs.
-- Telegram-specific identities live in a separate table linked to that row.
ALTER TABLE sources ADD CONSTRAINT sources_id_kind_unique UNIQUE(id,kind);

CREATE TABLE telegram_sources (
    source_id bigint PRIMARY KEY,
    kind text NOT NULL DEFAULT 'telegram' CHECK(kind='telegram'),
    username text NOT NULL CHECK(username ~ '^[a-z][a-z0-9_]{3,31}$'),
    provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
    FOREIGN KEY(source_id,kind) REFERENCES sources(id,kind) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX telegram_sources_username_unique ON telegram_sources(lower(username));

CREATE FUNCTION protect_telegram_source_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR OLD.source_id IS DISTINCT FROM NEW.source_id OR
       OLD.kind IS DISTINCT FROM NEW.kind OR OLD.username IS DISTINCT FROM NEW.username THEN
        RAISE EXCEPTION 'Telegram source identity is immutable';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER telegram_source_identity_immutable BEFORE UPDATE OR DELETE ON telegram_sources
    FOR EACH ROW EXECUTE FUNCTION protect_telegram_source_identity();

INSERT INTO telegram_sources(source_id,username)
SELECT id,lower(substring(address from '^https://t[.]me/([A-Za-z][A-Za-z0-9_]{3,31})/?$'))
FROM sources WHERE kind='telegram';

CREATE FUNCTION reject_source_identity_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.kind IS DISTINCT FROM NEW.kind OR OLD.address IS DISTINCT FROM NEW.address THEN
        RAISE EXCEPTION 'source identity is immutable';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER source_identity_immutable BEFORE UPDATE ON sources
    FOR EACH ROW EXECUTE FUNCTION reject_source_identity_change();

CREATE FUNCTION register_telegram_source() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE channel_name text;
BEGIN
    IF NEW.kind = 'telegram' THEN
        channel_name := lower(substring(NEW.address from '^https://t[.]me/([A-Za-z][A-Za-z0-9_]{3,31})/?$'));
        IF channel_name IS NULL THEN
            RAISE EXCEPTION 'invalid public Telegram channel address';
        END IF;
        INSERT INTO telegram_sources(source_id,username) VALUES (NEW.id,channel_name);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sources_register_telegram AFTER INSERT ON sources
    FOR EACH ROW EXECUTE FUNCTION register_telegram_source();
