-- Phase 3 consumes broker evidence but has no authorized network mutation path.
INSERT INTO lab.phase1_transitions VALUES
 ('TRIGGER_CONFIRMED','RISK_CHECK'), ('RISK_CHECK','RISK_REJECTED'),
 ('RISK_CHECK','ORDER_SUBMITTED'),
 ('ORDER_SUBMITTED','PARTIALLY_FILLED'), ('ORDER_SUBMITTED','FILLED'),
 ('ORDER_SUBMITTED','CANCELED'), ('ORDER_SUBMITTED','BROKER_REJECTED'),
 ('PARTIALLY_FILLED','FILLED'), ('PARTIALLY_FILLED','OPEN'),
 ('FILLED','OPEN'), ('OPEN','TARGET_EXIT'), ('OPEN','STOP_EXIT'),
 ('OPEN','TIME_EXIT'), ('OPEN','EMERGENCY_EXIT'),
 ('TARGET_EXIT','CLOSED'), ('STOP_EXIT','CLOSED'), ('TIME_EXIT','CLOSED'),
 ('EMERGENCY_EXIT','CLOSED'), ('CANCELED','PARTIALLY_FILLED'), ('CANCELED','FILLED'),
 ('BROKER_REJECTED','PARTIALLY_FILLED'), ('BROKER_REJECTED','FILLED'), ('CLOSED','OPEN');

CREATE TABLE lab.order_intents (
 intent_id uuid PRIMARY KEY,
 candidate_id uuid NOT NULL REFERENCES lab.candidates,
 strategy_version text NOT NULL CHECK(strategy_version = 'CATALYST_RETEST_V1'),
 kind text NOT NULL CHECK(kind IN ('ENTRY','TIME_EXIT')),
 payload_json jsonb NOT NULL,
 scheduled_for timestamptz,
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq),
 UNIQUE(candidate_id, kind)
);
ALTER TABLE lab.orders ADD COLUMN event_id uuid NOT NULL UNIQUE REFERENCES lab.trade_events(event_id),
 ADD COLUMN intent_id uuid NOT NULL UNIQUE REFERENCES lab.order_intents,
 ADD COLUMN ticker text NOT NULL, ADD COLUMN qty bigint NOT NULL CHECK(qty > 0);

CREATE TABLE lab.broker_events (
 event_seq bigint PRIMARY KEY REFERENCES lab.trade_events(seq),
 message_hash text NOT NULL UNIQUE,
 broker_order_id text,
 broker_event_type text,
 broker_timestamp timestamptz,
 payload_json jsonb NOT NULL
);
CREATE TABLE lab.order_links (
 broker_order_id text PRIMARY KEY,
 order_id uuid NOT NULL REFERENCES lab.orders,
 role text NOT NULL CHECK(role IN ('ENTRY','STOP','TARGET','TIME_EXIT')),
 symbol text NOT NULL,
 side text NOT NULL CHECK(side IN ('buy','sell')),
 qty bigint NOT NULL CHECK(qty > 0),
 order_type text NOT NULL,
 limit_price numeric,
 stop_price numeric,
 initial_status text NOT NULL,
 event_seq bigint NOT NULL REFERENCES lab.trade_events(seq)
);
CREATE TABLE lab.order_updates (
 event_seq bigint PRIMARY KEY REFERENCES lab.trade_events(seq),
 broker_order_id text NOT NULL REFERENCES lab.order_links,
 status text NOT NULL,
 filled_qty numeric NOT NULL CHECK(filled_qty >= 0),
 broker_timestamp timestamptz NOT NULL,
 reason text,
 order_qty bigint NOT NULL CHECK(order_qty > 0),
 timestamp_ns numeric(30,0) NOT NULL
);
ALTER TABLE lab.fills ADD COLUMN event_id uuid NOT NULL UNIQUE REFERENCES lab.trade_events(event_id),
 ADD COLUMN broker_order_id text NOT NULL REFERENCES lab.order_links,
 ADD COLUMN side text NOT NULL CHECK(side IN ('buy','sell')),
 ADD COLUMN role text NOT NULL CHECK(role IN ('ENTRY','STOP','TARGET','TIME_EXIT'));

CREATE TABLE lab.reconciliation_runs (
 event_seq bigint PRIMARY KEY REFERENCES lab.trade_events(seq),
 process_run_id uuid NOT NULL,
 session_date date NOT NULL,
 started_at timestamptz NOT NULL,
 completed_at timestamptz NOT NULL,
 startup boolean NOT NULL,
 clean boolean NOT NULL,
 discrepancies jsonb NOT NULL,
 broker_snapshot jsonb NOT NULL
);
CREATE INDEX reconciliation_latest ON lab.reconciliation_runs(process_run_id, event_seq DESC);
CREATE TABLE lab.execution_halts (
 event_seq bigint PRIMARY KEY REFERENCES lab.trade_events(seq),
 reason text NOT NULL,
 candidate_id uuid REFERENCES lab.candidates,
 payload_json jsonb NOT NULL
);

CREATE VIEW lab.broker_order_states AS
 SELECT l.broker_order_id,l.order_id,l.role,l.symbol,l.side,
        coalesce(u.order_qty,l.qty) AS qty,l.order_type,l.limit_price,l.stop_price,
        l.initial_status,l.event_seq,coalesce(u.status, l.initial_status) AS status,
        coalesce(f.filled_qty, 0) AS filled_qty, u.broker_timestamp
 FROM lab.order_links l
 LEFT JOIN LATERAL (
   SELECT status, broker_timestamp, order_qty FROM lab.order_updates
   WHERE broker_order_id = l.broker_order_id
   ORDER BY timestamp_ns DESC, event_seq DESC LIMIT 1
 ) u ON true
 LEFT JOIN LATERAL (
   SELECT sum(qty) AS filled_qty FROM lab.fills WHERE broker_order_id = l.broker_order_id
 ) f ON true;
CREATE VIEW lab.strategy_positions AS
 SELECT o.candidate_id, o.ticker, o.strategy_version,
   sum(CASE WHEN f.side = 'buy' THEN f.qty ELSE -f.qty END) AS qty
 FROM lab.orders o JOIN lab.fills f USING(order_id)
 GROUP BY o.candidate_id, o.ticker, o.strategy_version
 HAVING sum(CASE WHEN f.side = 'buy' THEN f.qty ELSE -f.qty END) <> 0;

DO $$ DECLARE name text; BEGIN
 FOREACH name IN ARRAY ARRAY['order_intents','broker_events','order_links','order_updates',
                             'reconciliation_runs','execution_halts'] LOOP
  EXECUTE format('CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON lab.%I
      FOR EACH ROW EXECUTE FUNCTION lab.reject_mutation()', name);
  EXECUTE format('CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON lab.%I
      FOR EACH STATEMENT EXECUTE FUNCTION lab.reject_mutation()', name);
 END LOOP;
END $$;
REVOKE ALL ON ALL TABLES IN SCHEMA lab FROM PUBLIC;
GRANT SELECT ON ALL TABLES IN SCHEMA lab TO catalyst_app;
GRANT INSERT ON lab.orders, lab.fills, lab.order_intents, lab.broker_events, lab.order_links,
 lab.order_updates, lab.reconciliation_runs, lab.execution_halts TO catalyst_app;
INSERT INTO lab.schema_migrations(version) VALUES (4);
