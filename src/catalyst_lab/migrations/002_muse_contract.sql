ALTER TABLE lab.candidates ADD COLUMN submission_context jsonb NOT NULL DEFAULT '{}';
CREATE INDEX candidates_poll_date ON lab.candidates
    ((submission_context->>'date'), received_at DESC);
INSERT INTO lab.schema_migrations(version) VALUES (2);
