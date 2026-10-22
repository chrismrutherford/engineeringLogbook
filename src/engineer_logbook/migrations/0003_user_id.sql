-- 0003 user_id: every row belongs to a user (later: the OAuth 'sub').
--
-- Existing rows are owned by 'chris'. ADD COLUMN ... DEFAULT fills them from
-- catalog metadata (no UPDATE, so no search triggers fire). The default is
-- then switched to the per-connection setting logbook.user_id, which the
-- server sets on every connection; a connection without it fails NOT NULL
-- instead of silently attributing rows to someone.

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'sessions', 'logs', 'notes', 'paths', 'tasks', 'decisions', 'snippets',
        'chat_bindings', 'search_index',
        'spec_workflows', 'spec_events', 'spec_artifacts'
    ] LOOP
        EXECUTE format(
            'ALTER TABLE %I ADD COLUMN IF NOT EXISTS user_id TEXT NOT NULL DEFAULT %L',
            t, 'chris');
        EXECUTE format(
            'ALTER TABLE %I ALTER COLUMN user_id SET DEFAULT current_setting(%L, true)',
            t, 'logbook.user_id');
    END LOOP;
END $$;

-- Uniqueness becomes per user.
ALTER TABLE paths DROP CONSTRAINT IF EXISTS paths_path_key;
CREATE UNIQUE INDEX IF NOT EXISTS idx_paths_user_path ON paths(user_id, path);

DROP INDEX IF EXISTS idx_spec_workflows_active;
CREATE UNIQUE INDEX IF NOT EXISTS idx_spec_workflows_active_user
    ON spec_workflows(user_id, project) WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, started_at);
CREATE INDEX IF NOT EXISTS idx_search_index_user ON search_index(user_id);

-- Search rows carry the owner of the source row (not the connection's
-- user), so a reindex or another user's write can never mis-attribute them.
CREATE OR REPLACE FUNCTION logbook_tsv(p_type text, p_id int,
                                       p_title text, p_content text,
                                       p_tags text, p_project text,
                                       p_tsv tsvector, p_user text) RETURNS void AS $$
BEGIN
    DELETE FROM search_index WHERE search_index.entry_type = p_type
      AND search_index.ref_id = p_id;
    IF p_tsv IS NOT NULL THEN
        INSERT INTO search_index(entry_type, ref_id, title, content, tags, project, tsv, user_id)
        VALUES (p_type, p_id, p_title, p_content, p_tags, p_project, p_tsv, p_user);
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION logs_tsv() RETURNS trigger AS $$
BEGIN
    PERFORM logbook_tsv('log', NEW.id, NEW.category, NEW.message, NEW.tags, NEW.project,
        setweight(to_tsvector('simple', NEW.category), 'A')
        || setweight(to_tsvector('simple', NEW.message), 'B')
        || setweight(to_tsvector('simple', NEW.tags), 'C'),
        NEW.user_id);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION notes_tsv() RETURNS trigger AS $$
BEGIN
    PERFORM logbook_tsv('note', NEW.id, NEW.title, NEW.content, NEW.tags, NEW.project,
        setweight(to_tsvector('simple', NEW.title), 'A')
        || setweight(to_tsvector('simple', NEW.content), 'B')
        || setweight(to_tsvector('simple', NEW.tags), 'C'),
        NEW.user_id);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION decisions_tsv() RETURNS trigger AS $$
BEGIN
    PERFORM logbook_tsv('decision', NEW.id, NEW.title,
        NEW.decision || ' ' || COALESCE(NEW.rationale, '') || ' ' || COALESCE(NEW.alternatives, ''),
        '', NEW.project,
        setweight(to_tsvector('simple', NEW.title), 'A')
        || setweight(to_tsvector('simple', NEW.decision || ' ' || COALESCE(NEW.rationale, '') || ' ' || COALESCE(NEW.alternatives, '')), 'B'),
        NEW.user_id);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION snippets_tsv() RETURNS trigger AS $$
BEGIN
    PERFORM logbook_tsv('snippet', NEW.id, COALESCE(NEW.description, NEW.language),
        NEW.code, NEW.tags, NEW.project,
        setweight(to_tsvector('simple', COALESCE(NEW.description, NEW.language)), 'A')
        || setweight(to_tsvector('simple', NEW.code), 'B')
        || setweight(to_tsvector('simple', NEW.tags), 'C'),
        NEW.user_id);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION spec_artifacts_tsv() RETURNS trigger AS $$
BEGIN
    PERFORM logbook_tsv('spec_artifact', NEW.id,
        NEW.step || ': ' || NEW.path,
        COALESCE(NEW.content, ''),
        'spec-artifact',
        (SELECT w.project FROM spec_workflows w WHERE w.id = NEW.workflow_id),
        setweight(to_tsvector('simple', NEW.step), 'A')
        || setweight(to_tsvector('simple', NEW.path), 'B')
        || setweight(to_tsvector('simple', COALESCE(NEW.content, '')), 'C'),
        NEW.user_id);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- The 7-argument version is no longer called by any trigger.
DROP FUNCTION IF EXISTS logbook_tsv(text, integer, text, text, text, text, tsvector);

-- Expose the owner on hydrated spec-artifact search rows (appended column).
CREATE OR REPLACE VIEW spec_artifacts_view AS
SELECT a.id, a.workflow_id, a.step, a.path, a.kind, a.content, a.recorded_at,
       w.project, a.user_id
FROM spec_artifacts a JOIN spec_workflows w ON w.id = a.workflow_id;
