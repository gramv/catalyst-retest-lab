-- Apply once as a database owner. The HTTP service must use catalyst_app.
-- Local bootstrap creates a dedicated cluster; never reuse another project's DB.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE ROLE catalyst_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE SCHEMA lab;
REVOKE ALL ON SCHEMA lab FROM PUBLIC;
GRANT USAGE ON SCHEMA lab TO catalyst_app;

CREATE TABLE lab.schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO lab.schema_migrations(version) VALUES (1);

CREATE TABLE lab.candidates (
    candidate_id uuid PRIMARY KEY,
    payload_json jsonb NOT NULL,
    strategy_version text,
    signal_id text,
    ticker text,
    session_date date NOT NULL,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
-- Rejected duplicates also remain in candidates. Claims enforce uniqueness of attempts.
CREATE TABLE lab.signal_claims (
    signal_id text PRIMARY KEY,
    candidate_id uuid NOT NULL REFERENCES lab.candidates
);
CREATE TABLE lab.ticker_day_claims (
    ticker text NOT NULL,
    session_date date NOT NULL,
    candidate_id uuid NOT NULL REFERENCES lab.candidates,
    PRIMARY KEY(ticker, session_date)
);

CREATE TABLE lab.trade_events (
    seq bigint PRIMARY KEY,
    event_id uuid NOT NULL UNIQUE,
    candidate_id uuid REFERENCES lab.candidates,
    trade_id uuid,
    strategy_version text NOT NULL,
    event_type text NOT NULL,
    payload_json jsonb NOT NULL CHECK (jsonb_typeof(payload_json) = 'object'),
    correction_of uuid REFERENCES lab.trade_events(event_id),
    created_at timestamptz NOT NULL,
    previous_hash text NOT NULL CHECK (length(previous_hash) = 64),
    event_hash text NOT NULL CHECK (length(event_hash) = 64),
    event_body text NOT NULL,
    CHECK ((event_type = 'CORRECTION') = (correction_of IS NOT NULL))
);
CREATE INDEX events_candidate_seq ON lab.trade_events(candidate_id, seq DESC);
CREATE INDEX events_trade_seq ON lab.trade_events(trade_id, seq);

CREATE TABLE lab.phase1_transitions (
    from_state text NOT NULL,
    to_state text NOT NULL,
    PRIMARY KEY(from_state, to_state)
);
-- Only implemented transitions are enabled. Future phases require migrations.
INSERT INTO lab.phase1_transitions VALUES
    ('', 'RECEIVED'), ('RECEIVED', 'VALIDATING'),
    ('VALIDATING', 'REJECTED'), ('VALIDATING', 'VALIDATED');

CREATE FUNCTION lab.stamp_event() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, lab AS $$
DECLARE last_seq bigint; last_hash text; current_state text;
BEGIN
    -- Transaction-scoped serialization covers both sequence and predecessor selection.
    PERFORM pg_advisory_xact_lock(719172026);
    SELECT seq, event_hash INTO last_seq, last_hash
        FROM lab.trade_events ORDER BY seq DESC LIMIT 1;
    NEW.seq := coalesce(last_seq, 0) + 1;
    NEW.previous_hash := coalesce(last_hash, repeat('0', 64));
    NEW.event_id := gen_random_uuid();
    NEW.created_at := clock_timestamp();
    IF NEW.strategy_version <> 'CATALYST_RETEST_V1' THEN
        RAISE EXCEPTION 'unsupported strategy version';
    END IF;
    IF NEW.event_type NOT IN
        ('CANDIDATE_RECEIVED', 'STATE_TRANSITION', 'VALIDATION_DECISION',
         'SYSTEM_EVENT', 'CORRECTION') THEN
        RAISE EXCEPTION 'event type not implemented in Phase 1';
    END IF;
    IF NEW.event_type = 'STATE_TRANSITION' THEN
        IF NEW.candidate_id IS NULL THEN RAISE EXCEPTION 'candidate required'; END IF;
        SELECT payload_json->>'to_state' INTO current_state FROM lab.trade_events
            WHERE candidate_id = NEW.candidate_id AND event_type = 'STATE_TRANSITION'
            ORDER BY seq DESC LIMIT 1;
        IF coalesce(NEW.payload_json->>'from_state', '') <> coalesce(current_state, '')
           OR NOT EXISTS (SELECT 1 FROM lab.phase1_transitions
               WHERE from_state = coalesce(current_state, '')
               AND to_state = NEW.payload_json->>'to_state') THEN
            RAISE EXCEPTION 'invalid candidate state transition';
        END IF;
    END IF;
    NEW.event_body := jsonb_build_object(
        'seq', NEW.seq, 'event_id', NEW.event_id, 'candidate_id', NEW.candidate_id,
        'trade_id', NEW.trade_id, 'strategy_version', NEW.strategy_version,
        'event_type', NEW.event_type, 'payload_json', NEW.payload_json,
        'correction_of', NEW.correction_of,
        'created_at', to_char(NEW.created_at AT TIME ZONE 'UTC',
                              'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))::text;
    NEW.event_hash := encode(public.digest(
        NEW.previous_hash || NEW.event_body, 'sha256'), 'hex');
    RETURN NEW;
END $$;
CREATE TRIGGER stamp_event BEFORE INSERT ON lab.trade_events
    FOR EACH ROW EXECUTE FUNCTION lab.stamp_event();

CREATE FUNCTION lab.reject_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN RAISE EXCEPTION 'append-only relation: mutation forbidden'; END $$;

CREATE TABLE lab.validation_decisions (
    decision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id uuid NOT NULL REFERENCES lab.candidates,
    event_id uuid NOT NULL UNIQUE REFERENCES lab.trade_events(event_id),
    passed boolean NOT NULL,
    failed_rule text,
    reason text NOT NULL,
    expires_at timestamptz,
    evidence_json jsonb,
    policy_json jsonb,
    decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (passed = (failed_rule IS NULL))
);

CREATE TABLE lab.risk_decisions (
    risk_decision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id uuid NOT NULL REFERENCES lab.candidates,
    strategy_version text NOT NULL,
    equity numeric NOT NULL CHECK(equity > 0),
    risk_pct numeric NOT NULL,
    risk_dollars numeric NOT NULL,
    planned_risk numeric NOT NULL,
    theme_exposure_before numeric NOT NULL,
    theme_exposure_after numeric NOT NULL,
    computed_qty bigint NOT NULL CHECK(computed_qty >= 0),
    decision text NOT NULL,
    reason text NOT NULL,
    decided_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE lab.orders (
    order_id uuid PRIMARY KEY,
    candidate_id uuid NOT NULL REFERENCES lab.candidates,
    strategy_version text NOT NULL,
    execution_source text NOT NULL CHECK(execution_source = 'ALPACA_PAPER'),
    client_order_id text NOT NULL UNIQUE,
    alpaca_order_id text UNIQUE,
    bracket_legs jsonb NOT NULL,
    status text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE lab.fills (
    fill_id text PRIMARY KEY,
    order_id uuid NOT NULL REFERENCES lab.orders,
    strategy_version text NOT NULL,
    execution_source text NOT NULL CHECK(execution_source = 'ALPACA_PAPER'),
    qty numeric NOT NULL CHECK(qty > 0),
    price numeric NOT NULL CHECK(price > 0),
    timestamp timestamptz NOT NULL
);
CREATE TABLE lab.market_snapshots (
    snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id uuid NOT NULL REFERENCES lab.candidates,
    trade_id uuid,
    market_data_timestamp timestamptz NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    price numeric NOT NULL,
    bid numeric,
    ask numeric,
    spread_bps numeric,
    volume numeric,
    data_provider text NOT NULL,
    data_feed text NOT NULL,
    observation_resolution text NOT NULL
);
-- Projection storage is reserved; Phase 5 will supply the event projector.
-- The app has no INSERT/UPDATE/DELETE privilege on this relation.
CREATE TABLE lab.trades (
    trade_id uuid PRIMARY KEY,
    candidate_id uuid NOT NULL REFERENCES lab.candidates,
    strategy_version text NOT NULL,
    execution_source text NOT NULL CHECK(execution_source IN ('ALPACA_PAPER', 'MUSE_MANUAL')),
    market text NOT NULL,
    status text NOT NULL,
    opened_at timestamptz NOT NULL,
    closed_at timestamptz,
    exit_reason text,
    broker_paper_pnl numeric,
    initial_risk_dollars numeric,
    r_multiple numeric,
    mfe numeric,
    mae numeric,
    observed_spread numeric,
    assumed_slippage numeric,
    adjusted_entry numeric,
    adjusted_exit numeric,
    adjusted_r numeric,
    conservative_adjusted_pnl numeric,
    last_event_seq bigint NOT NULL REFERENCES lab.trade_events(seq)
);
CREATE TABLE lab.system_events (
    system_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id uuid NOT NULL UNIQUE REFERENCES lab.trade_events(event_id),
    event_type text NOT NULL,
    payload_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE lab.daily_stats (
    session_date date NOT NULL,
    strategy_version text NOT NULL,
    execution_source text NOT NULL,
    market text NOT NULL,
    revision integer NOT NULL,
    metrics_json jsonb NOT NULL,
    through_event_seq bigint NOT NULL REFERENCES lab.trade_events(seq),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(session_date, strategy_version, execution_source, market, revision)
);

CREATE VIEW lab.candidate_states AS
    SELECT DISTINCT ON (candidate_id) candidate_id,
        payload_json->>'to_state' AS state, seq AS last_event_seq
    FROM lab.trade_events WHERE event_type = 'STATE_TRANSITION'
    ORDER BY candidate_id, seq DESC;

DO $$ DECLARE name text; BEGIN
    FOREACH name IN ARRAY ARRAY['trade_events', 'candidates', 'signal_claims',
        'ticker_day_claims', 'validation_decisions', 'risk_decisions', 'orders',
        'fills', 'market_snapshots', 'system_events', 'daily_stats'] LOOP
        EXECUTE format('CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON lab.%I
            FOR EACH ROW EXECUTE FUNCTION lab.reject_mutation()', name);
        EXECUTE format('CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON lab.%I
            FOR EACH STATEMENT EXECUTE FUNCTION lab.reject_mutation()', name);
    END LOOP;
END $$;

REVOKE ALL ON ALL TABLES IN SCHEMA lab FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA lab FROM PUBLIC;
GRANT SELECT ON ALL TABLES IN SCHEMA lab TO catalyst_app;
GRANT INSERT ON lab.candidates, lab.signal_claims, lab.ticker_day_claims,
    lab.trade_events, lab.validation_decisions, lab.system_events TO catalyst_app;
