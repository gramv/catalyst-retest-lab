-- WATCHING is backed by a durable observer; no execution transitions are enabled.
INSERT INTO lab.phase1_transitions VALUES
    ('VALIDATED', 'WATCHING'), ('VALIDATED', 'EXPIRED_UNTRIGGERED'),
    ('WATCHING', 'TRIGGER_CONFIRMED'), ('WATCHING', 'INVALIDATED'),
    ('WATCHING', 'EXPIRED_UNTRIGGERED');

CREATE TABLE lab.watch_contexts (
    event_seq bigint PRIMARY KEY REFERENCES lab.trade_events(seq),
    candidate_id uuid NOT NULL REFERENCES lab.candidates,
    payload_json jsonb NOT NULL CHECK (jsonb_typeof(payload_json) = 'object')
);
CREATE INDEX watch_context_latest ON lab.watch_contexts(candidate_id, event_seq DESC);
CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON lab.watch_contexts
    FOR EACH ROW EXECUTE FUNCTION lab.reject_mutation();
CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON lab.watch_contexts
    FOR EACH STATEMENT EXECUTE FUNCTION lab.reject_mutation();
REVOKE ALL ON lab.watch_contexts FROM PUBLIC;
GRANT SELECT, INSERT ON lab.watch_contexts TO catalyst_app;

ALTER TABLE lab.market_snapshots
    ADD COLUMN event_id uuid UNIQUE REFERENCES lab.trade_events(event_id),
    ADD COLUMN observation_key text,
    ADD COLUMN observation_type text,
    ADD COLUMN provider_timestamp text;
CREATE UNIQUE INDEX snapshot_observation_key
    ON lab.market_snapshots(candidate_id, observation_key);
GRANT INSERT ON lab.market_snapshots TO catalyst_app;
INSERT INTO lab.schema_migrations(version) VALUES (3);
