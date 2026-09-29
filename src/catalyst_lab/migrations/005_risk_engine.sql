-- The HTTP/candidate role cannot manufacture a risk authorization.
CREATE ROLE catalyst_risk LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT;
GRANT catalyst_app TO catalyst_risk;
ALTER TABLE lab.order_links DROP CONSTRAINT order_links_role_check;
ALTER TABLE lab.order_links ADD CHECK(role IN ('ENTRY','STOP','TARGET','TIME_EXIT','EMERGENCY_EXIT'));
ALTER TABLE lab.fills DROP CONSTRAINT fills_role_check;
ALTER TABLE lab.fills ADD CHECK(role IN ('ENTRY','STOP','TARGET','TIME_EXIT','EMERGENCY_EXIT'));
ALTER TABLE lab.risk_decisions ALTER COLUMN candidate_id DROP NOT NULL;
ALTER TABLE lab.risk_decisions DROP CONSTRAINT risk_decisions_equity_check;
ALTER TABLE lab.risk_decisions ADD COLUMN event_id uuid NOT NULL UNIQUE REFERENCES lab.trade_events(event_id),
 ADD COLUMN action text NOT NULL CHECK(action IN ('ENTRY','CANCEL','FLATTEN')),
 ADD COLUMN session_date date NOT NULL,
 ADD COLUMN expires_at timestamptz NOT NULL,
 ADD COLUMN http_method text NOT NULL CHECK(http_method IN ('POST','DELETE','NONE')),
 ADD COLUMN path text NOT NULL,
 ADD COLUMN payload_json jsonb NOT NULL,
 ADD COLUMN context_json jsonb NOT NULL,
 ADD COLUMN transaction_id bigint NOT NULL DEFAULT txid_current();
CREATE TABLE lab.risk_classifications (
 event_seq bigint PRIMARY KEY REFERENCES lab.trade_events(seq),
 ticker text NOT NULL, sector text NOT NULL CHECK(length(sector)>0),
 theme text NOT NULL CHECK(length(theme)>0), source text NOT NULL CHECK(length(source)>0)
);
CREATE VIEW lab.current_classifications AS
 SELECT DISTINCT ON(ticker) * FROM lab.risk_classifications ORDER BY ticker,event_seq DESC;
CREATE TABLE lab.risk_sessions (
 session_date date PRIMARY KEY,
 day_start_equity numeric NOT NULL CHECK(day_start_equity>0),
 source text NOT NULL, observed_at timestamptz NOT NULL,
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);
CREATE TABLE lab.risk_reservations (
 candidate_id uuid PRIMARY KEY REFERENCES lab.candidates,
 risk_decision_id uuid NOT NULL UNIQUE REFERENCES lab.risk_decisions,
 budget numeric NOT NULL CHECK(budget>0), planned_risk numeric NOT NULL CHECK(planned_risk>0),
 qty bigint NOT NULL CHECK(qty>0), max_entry numeric NOT NULL CHECK(max_entry>0),
 sector text NOT NULL, theme text NOT NULL,
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);
CREATE TABLE lab.reservation_releases (
 candidate_id uuid PRIMARY KEY REFERENCES lab.risk_reservations,
 reason text NOT NULL, event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);
CREATE VIEW lab.active_reservations AS
 SELECT r.* FROM lab.risk_reservations r
 WHERE NOT EXISTS(SELECT 1 FROM lab.reservation_releases x WHERE x.candidate_id=r.candidate_id);
CREATE TABLE lab.daily_risk_halts (
 session_date date PRIMARY KEY REFERENCES lab.risk_sessions,
 realized_pnl numeric NOT NULL, unrealized_pnl numeric NOT NULL,
 threshold numeric NOT NULL, event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);
CREATE TABLE lab.authorization_claims (
 risk_decision_id uuid PRIMARY KEY REFERENCES lab.risk_decisions,
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq),
 claimed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE lab.authorization_results (
 event_seq bigint PRIMARY KEY REFERENCES lab.trade_events(seq),
 risk_decision_id uuid NOT NULL REFERENCES lab.risk_decisions,
 outcome text NOT NULL CHECK(outcome IN
  ('ACCEPTED','REJECTED','UNKNOWN','RECOVERED','NOT_FOUND','EXPIRED','CANCELED')),
 broker_order_id text, payload_json jsonb NOT NULL
);
CREATE VIEW lab.current_authorization_results AS
 SELECT DISTINCT ON(risk_decision_id) * FROM lab.authorization_results
 ORDER BY risk_decision_id,event_seq DESC;
CREATE TABLE lab.risk_exit_requests (
 exit_request_id uuid PRIMARY KEY,
 candidate_id uuid REFERENCES lab.candidates,
 session_date date NOT NULL, ticker text NOT NULL, reason text NOT NULL,
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);
CREATE TABLE lab.risk_exit_completions (
 exit_request_id uuid PRIMARY KEY REFERENCES lab.risk_exit_requests,
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);
CREATE VIEW lab.pending_risk_exits AS
 SELECT r.* FROM lab.risk_exit_requests r WHERE NOT EXISTS
 (SELECT 1 FROM lab.risk_exit_completions c WHERE c.exit_request_id=r.exit_request_id);

-- DB authorizations use server time and are stamped with their actual transaction identity.
CREATE FUNCTION lab.stamp_risk_decision() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
BEGIN
 IF current_user <> 'catalyst_risk' THEN RAISE EXCEPTION 'risk engine role required'; END IF;
 NEW.transaction_id := txid_current();
 NEW.decided_at := clock_timestamp();
 IF NEW.decision = 'APPROVED' THEN
   IF NEW.expires_at <= NEW.decided_at OR NEW.http_method = 'NONE' THEN
     RAISE EXCEPTION 'invalid risk authorization';
   END IF;
   IF NEW.action='ENTRY' AND (NEW.candidate_id IS NULL OR NEW.equity<=0
       OR NEW.risk_pct<>0.01 OR NEW.risk_dollars<>0.01*NEW.equity
       OR NEW.planned_risk>NEW.risk_dollars OR NEW.computed_qty<1
       OR NEW.http_method<>'POST' OR NEW.path<>'/v2/orders') THEN
     RAISE EXCEPTION 'invalid entry risk decision';
   END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER stamp_risk_decision BEFORE INSERT ON lab.risk_decisions
 FOR EACH ROW EXECUTE FUNCTION lab.stamp_risk_decision();

CREATE FUNCTION lab.enforce_risk_reservation() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
DECLARE d lab.risk_decisions; c lab.candidates; classification record; total numeric;
        sector_count integer; theme_count integer; m numeric; s numeric;
BEGIN
 PERFORM pg_advisory_xact_lock(719172026);
 SELECT * INTO d FROM lab.risk_decisions WHERE risk_decision_id=NEW.risk_decision_id;
 SELECT * INTO c FROM lab.candidates WHERE candidate_id=NEW.candidate_id;
 SELECT * INTO classification FROM lab.current_classifications WHERE ticker=c.ticker;
 m := (c.payload_json->>'max_entry_price')::numeric;
 s := (c.payload_json->>'stop')::numeric;
 IF d.risk_decision_id IS NULL OR c.candidate_id IS NULL
  OR d.decision<>'APPROVED' OR d.action<>'ENTRY' OR d.candidate_id<>NEW.candidate_id
  OR d.transaction_id<>txid_current() OR d.expires_at<=clock_timestamp()
  OR NEW.budget<>d.risk_dollars OR NEW.budget<>0.01*d.equity OR NEW.qty<>d.computed_qty
  OR NEW.max_entry<>m OR NEW.planned_risk<>NEW.qty*(m-s)
  OR NEW.planned_risk<>d.planned_risk OR NEW.planned_risk>NEW.budget
  OR NEW.qty*m>d.equity OR classification.ticker IS NULL
  OR NEW.sector<>classification.sector OR NEW.theme<>classification.theme
  OR NOT (d.payload_json ?& ARRAY['symbol','qty','side','type','limit_price',
        'time_in_force','order_class','stop_loss','take_profit','client_order_id'])
  OR (d.payload_json->>'symbol') IS DISTINCT FROM c.ticker
  OR (d.payload_json->>'side') IS DISTINCT FROM 'buy'
  OR (d.payload_json->>'type') IS DISTINCT FROM 'limit'
  OR (d.payload_json->>'order_class') IS DISTINCT FROM 'bracket'
  OR (d.payload_json->>'time_in_force') IS DISTINCT FROM 'day'
  OR (d.payload_json->>'qty')::numeric IS DISTINCT FROM NEW.qty
  OR (d.payload_json->>'limit_price')::numeric IS DISTINCT FROM m
  OR (d.payload_json->'stop_loss'->>'stop_price')::numeric IS DISTINCT FROM s
  OR (d.payload_json->'take_profit'->>'limit_price')::numeric
       IS DISTINCT FROM (c.payload_json->>'target')::numeric
 THEN RAISE EXCEPTION 'risk reservation does not match frozen sizing and request'; END IF;
 SELECT coalesce(sum(budget),0),count(*) FILTER(WHERE sector=NEW.sector),
        count(*) FILTER(WHERE theme=NEW.theme)
   INTO total,sector_count,theme_count FROM lab.active_reservations;
 IF total+NEW.budget>0.02*d.equity THEN RAISE EXCEPTION 'combined risk exceeds two percent'; END IF;
 IF sector_count>=coalesce((d.context_json->'policy'->>'max_per_sector')::integer,1)
  OR theme_count>=coalesce((d.context_json->'policy'->>'max_per_theme')::integer,1)
 THEN RAISE EXCEPTION 'correlation risk limit'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER enforce_risk_reservation BEFORE INSERT ON lab.risk_reservations
 FOR EACH ROW EXECUTE FUNCTION lab.enforce_risk_reservation();

CREATE FUNCTION lab.authorize_order_transition() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
DECLARE decision lab.risk_decisions;
BEGIN
 IF NEW.event_type='STATE_TRANSITION' AND NEW.payload_json->>'to_state'='ORDER_SUBMITTED' THEN
   SELECT * INTO decision FROM lab.risk_decisions
   WHERE risk_decision_id=(NEW.payload_json->>'risk_decision_id')::uuid;
   IF decision.risk_decision_id IS NULL OR decision.decision<>'APPROVED'
      OR decision.action<>'ENTRY' OR decision.candidate_id<>NEW.candidate_id
      OR decision.transaction_id<>txid_current() OR decision.expires_at<=clock_timestamp()
      OR NOT EXISTS(SELECT 1 FROM lab.active_reservations r
         WHERE r.risk_decision_id=decision.risk_decision_id) THEN
     RAISE EXCEPTION 'fresh atomic risk decision and reservation required';
   END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER authorize_order_transition BEFORE INSERT ON lab.trade_events
 FOR EACH ROW EXECUTE FUNCTION lab.authorize_order_transition();

DO $$ DECLARE name text; BEGIN
 FOREACH name IN ARRAY ARRAY['risk_classifications','risk_sessions','risk_reservations',
  'reservation_releases','daily_risk_halts','authorization_claims','authorization_results',
  'risk_exit_requests','risk_exit_completions'] LOOP
  EXECUTE format('CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON lab.%I
   FOR EACH ROW EXECUTE FUNCTION lab.reject_mutation()',name);
  EXECUTE format('CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON lab.%I
   FOR EACH STATEMENT EXECUTE FUNCTION lab.reject_mutation()',name);
 END LOOP;
END $$;
REVOKE ALL ON ALL TABLES IN SCHEMA lab FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA lab FROM PUBLIC;
GRANT SELECT ON ALL TABLES IN SCHEMA lab TO catalyst_app;
GRANT INSERT ON lab.risk_decisions,lab.risk_classifications,lab.risk_sessions,
 lab.risk_reservations,lab.reservation_releases,lab.daily_risk_halts,
 lab.authorization_claims,lab.authorization_results,lab.risk_exit_requests,
 lab.risk_exit_completions TO catalyst_risk;
INSERT INTO lab.schema_migrations(version) VALUES(5);
