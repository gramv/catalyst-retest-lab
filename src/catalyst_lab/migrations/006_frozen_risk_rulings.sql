-- Frozen Phase 4 rulings: five-second capabilities, reconciled previous-close baseline,
-- immutable engineering provenance, and reporting views that cannot include plumbing tests.
ALTER TABLE lab.candidates ADD COLUMN record_purpose text GENERATED ALWAYS AS
 (CASE WHEN upper(btrim(coalesce(signal_id,payload_json->>'signal_id',''))) LIKE 'TEST-%'
       THEN 'ENGINEERING_TEST' ELSE 'STRATEGY' END) STORED;
ALTER TABLE lab.ticker_day_claims ADD COLUMN record_purpose text NOT NULL DEFAULT 'STRATEGY';
ALTER TABLE lab.ticker_day_claims DROP CONSTRAINT ticker_day_claims_pkey;
ALTER TABLE lab.ticker_day_claims ADD PRIMARY KEY(ticker,session_date,record_purpose);
ALTER TABLE lab.orders ADD COLUMN record_purpose text NOT NULL DEFAULT 'STRATEGY';
ALTER TABLE lab.fills ADD COLUMN record_purpose text NOT NULL DEFAULT 'STRATEGY';
ALTER TABLE lab.trades ADD COLUMN record_purpose text NOT NULL DEFAULT 'STRATEGY';

CREATE FUNCTION lab.inherit_record_purpose() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
BEGIN
 IF TG_TABLE_NAME='fills' THEN
   SELECT c.record_purpose INTO NEW.record_purpose FROM lab.orders o
    JOIN lab.candidates c USING(candidate_id) WHERE o.order_id=NEW.order_id;
 ELSE
   SELECT c.record_purpose INTO NEW.record_purpose FROM lab.candidates c
    WHERE c.candidate_id=NEW.candidate_id;
 END IF;
 IF NEW.record_purpose IS NULL THEN RAISE EXCEPTION 'candidate provenance required'; END IF;
 RETURN NEW;
END $$;
DO $$ DECLARE name text; BEGIN
 FOREACH name IN ARRAY ARRAY['orders','fills','trades','ticker_day_claims'] LOOP
  EXECUTE format('CREATE TRIGGER inherit_record_purpose BEFORE INSERT ON lab.%I
   FOR EACH ROW EXECUTE FUNCTION lab.inherit_record_purpose()',name);
 END LOOP;
END $$;

CREATE FUNCTION lab.classify_candidate_event() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
DECLARE purpose text;
BEGIN
 IF NEW.candidate_id IS NOT NULL THEN
  SELECT record_purpose INTO purpose FROM lab.candidates WHERE candidate_id=NEW.candidate_id;
  NEW.payload_json := NEW.payload_json || jsonb_build_object('record_purpose',purpose,
                                        'strategy_eligible',purpose='STRATEGY');
 END IF;
 RETURN NEW;
END $$;
-- Trigger runs before stamp_event so the immutable classification is covered by the hash.
CREATE TRIGGER classify_candidate_event BEFORE INSERT ON lab.trade_events
 FOR EACH ROW EXECUTE FUNCTION lab.classify_candidate_event();

CREATE VIEW lab.strategy_candidates AS SELECT * FROM lab.candidates WHERE record_purpose='STRATEGY';
CREATE VIEW lab.strategy_orders AS SELECT o.* FROM lab.orders o
 JOIN lab.strategy_candidates c USING(candidate_id);
CREATE VIEW lab.strategy_fills AS SELECT f.* FROM lab.fills f
 JOIN lab.strategy_orders o USING(order_id);
CREATE VIEW lab.strategy_trades AS SELECT t.* FROM lab.trades t
 JOIN lab.strategy_candidates c USING(candidate_id);
CREATE ROLE catalyst_reporting NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
GRANT USAGE ON SCHEMA lab TO catalyst_reporting;
GRANT SELECT ON lab.strategy_candidates,lab.strategy_orders,lab.strategy_fills,lab.strategy_trades
 TO catalyst_reporting;

ALTER TABLE lab.risk_sessions ADD COLUMN reconciliation_seq bigint
 REFERENCES lab.reconciliation_runs(event_seq);
CREATE FUNCTION lab.require_reconciled_baseline() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
BEGIN
 IF NEW.source<>'ALPACA_LAST_EQUITY' OR NEW.reconciliation_seq IS NULL OR NOT EXISTS
   (SELECT 1 FROM lab.reconciliation_runs r WHERE r.event_seq=NEW.reconciliation_seq
    AND r.startup AND r.session_date=NEW.session_date) THEN
  RAISE EXCEPTION 'previous-close baseline must be captured at startup reconciliation';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER require_reconciled_baseline BEFORE INSERT ON lab.risk_sessions
 FOR EACH ROW EXECUTE FUNCTION lab.require_reconciled_baseline();

CREATE OR REPLACE FUNCTION lab.stamp_risk_decision() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
BEGIN
 IF current_user <> 'catalyst_risk' THEN RAISE EXCEPTION 'risk engine role required'; END IF;
 NEW.transaction_id := txid_current();
 NEW.decided_at := clock_timestamp();
 NEW.expires_at := NEW.decided_at + interval '5 seconds';
 IF NEW.decision = 'APPROVED' THEN
   IF NEW.http_method = 'NONE' THEN RAISE EXCEPTION 'invalid risk authorization'; END IF;
   IF NEW.action='ENTRY' AND (NEW.candidate_id IS NULL OR NEW.equity<=0
       OR NEW.risk_pct<>0.01 OR NEW.risk_dollars<>0.01*NEW.equity
       OR NEW.planned_risk>NEW.risk_dollars OR NEW.computed_qty<1
       OR NEW.http_method<>'POST' OR NEW.path<>'/v2/orders') THEN
     RAISE EXCEPTION 'invalid entry risk decision';
   END IF;
 END IF;
 RETURN NEW;
END $$;

CREATE TABLE lab.engineering_acceptance_runs (
 candidate_id uuid PRIMARY KEY REFERENCES lab.candidates,
 deadline timestamptz NOT NULL,
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);
CREATE FUNCTION lab.require_engineering_candidate() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM lab.candidates WHERE candidate_id=NEW.candidate_id
    AND record_purpose='ENGINEERING_TEST' AND signal_id LIKE 'TEST-%') THEN
  RAISE EXCEPTION 'TEST- engineering candidate required';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER require_engineering_candidate BEFORE INSERT ON lab.engineering_acceptance_runs
 FOR EACH ROW EXECUTE FUNCTION lab.require_engineering_candidate();
CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON lab.engineering_acceptance_runs
 FOR EACH ROW EXECUTE FUNCTION lab.reject_mutation();
CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON lab.engineering_acceptance_runs
 FOR EACH STATEMENT EXECUTE FUNCTION lab.reject_mutation();
CREATE TABLE lab.engineering_acceptance_results (
 candidate_id uuid PRIMARY KEY REFERENCES lab.engineering_acceptance_runs,
 outcome text NOT NULL CHECK(outcome IN ('PASSED','NOT_ENTERED','FAILED_CLOSED')),
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);
CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON lab.engineering_acceptance_results
 FOR EACH ROW EXECUTE FUNCTION lab.reject_mutation();
CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON lab.engineering_acceptance_results
 FOR EACH STATEMENT EXECUTE FUNCTION lab.reject_mutation();
REVOKE ALL ON ALL TABLES IN SCHEMA lab FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA lab FROM PUBLIC;
GRANT SELECT ON ALL TABLES IN SCHEMA lab TO catalyst_app;
GRANT INSERT ON lab.engineering_acceptance_runs,lab.engineering_acceptance_results TO catalyst_risk;
INSERT INTO lab.schema_migrations(version) VALUES(6);
