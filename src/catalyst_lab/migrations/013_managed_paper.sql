-- Separate opt-in engineering workflow. Frozen strategy tables/history are preserved.
CREATE TABLE lab.managed_setups (
 setup_id uuid PRIMARY KEY, cycle_id uuid NOT NULL, revision integer NOT NULL CHECK(revision>0),
 symbol text NOT NULL, market text NOT NULL CHECK(market IN ('US_STOCKS','CRYPTO')),
 strategy_version text NOT NULL, policy_id text NOT NULL, cohort text NOT NULL,
 receipt_id uuid NOT NULL REFERENCES lab.jev_receipts,
 evidence_hash text NOT NULL, expires_at timestamptz NOT NULL, record_json jsonb NOT NULL,
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq),
 UNIQUE(cycle_id,symbol,revision), UNIQUE(receipt_id),
 CHECK((market='US_STOCKS' AND strategy_version='CATALYST_RETEST_V1') OR
       (market='CRYPTO' AND strategy_version='CRYPTO_STRUCTURAL_RETEST_TEST_V1')),
 CHECK(policy_id='MUSE_JEV_MANAGED_TEST_V1'), CHECK(cohort='JEV_MANAGED_PAPER_V1')
);
CREATE TABLE lab.managed_events (
 event_id uuid PRIMARY KEY, setup_id uuid REFERENCES lab.managed_setups,
 idempotency_key text NOT NULL UNIQUE, kind text NOT NULL, body jsonb NOT NULL,
 recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);
CREATE VIEW lab.managed_states AS
 SELECT DISTINCT ON(setup_id) setup_id,body,recorded_at,event_seq FROM lab.managed_events
 WHERE kind='STATE' ORDER BY setup_id,event_seq DESC;
CREATE TABLE lab.managed_risk_decisions (
 decision_id uuid PRIMARY KEY, setup_id uuid NOT NULL REFERENCES lab.managed_setups,
 action text NOT NULL CHECK(action IN ('ENTRY','PROTECT','CANCEL','EXIT','AMEND')),
 outcome text NOT NULL CHECK(outcome IN ('APPROVED','REJECTED')), reason text NOT NULL,
 method text NOT NULL CHECK(method IN ('POST','DELETE','PATCH','NONE')), path text NOT NULL,
 payload jsonb NOT NULL, context jsonb NOT NULL, equity numeric NOT NULL,
 expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 transaction_id bigint NOT NULL DEFAULT txid_current(),
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq),
 CHECK(outcome='REJECTED' OR (method<>'NONE' AND expires_at>created_at AND
      expires_at<=created_at+interval '5 seconds'))
);
CREATE UNIQUE INDEX managed_submit_operation ON lab.managed_risk_decisions
 ((payload->>'client_order_id')) WHERE outcome='APPROVED' AND method='POST';
CREATE TABLE lab.managed_claims (
 decision_id uuid PRIMARY KEY REFERENCES lab.managed_risk_decisions,
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);
CREATE TABLE lab.managed_reservations (
 setup_id uuid PRIMARY KEY REFERENCES lab.managed_setups,
 decision_id uuid NOT NULL UNIQUE REFERENCES lab.managed_risk_decisions,
 budget numeric NOT NULL CHECK(budget>0), planned_risk numeric NOT NULL CHECK(planned_risk>0),
 qty numeric NOT NULL CHECK(qty>0), max_entry numeric NOT NULL CHECK(max_entry>0),
 sector text NOT NULL, theme text NOT NULL,
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);
CREATE TABLE lab.managed_releases (
 setup_id uuid PRIMARY KEY REFERENCES lab.managed_reservations, reason text NOT NULL,
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);
CREATE VIEW lab.managed_active_reservations AS SELECT r.* FROM lab.managed_reservations r
 WHERE NOT EXISTS(SELECT 1 FROM lab.managed_releases x WHERE x.setup_id=r.setup_id);
CREATE VIEW lab.account_risk_reservations AS
 SELECT candidate_id AS reference_id,budget,planned_risk,qty::numeric,max_entry,sector,theme,
 'FROZEN'::text AS source FROM lab.active_reservations
 UNION ALL SELECT setup_id,budget,planned_risk,qty,max_entry,sector,theme,'MANAGED'
 FROM lab.managed_active_reservations;
CREATE TABLE lab.managed_fills (
 fill_id text PRIMARY KEY, setup_id uuid NOT NULL REFERENCES lab.managed_setups,
 broker_order_id text NOT NULL, side text NOT NULL CHECK(side IN ('buy','sell')),
 qty numeric NOT NULL CHECK(qty>0), price numeric NOT NULL CHECK(price>0),
 filled_at timestamptz NOT NULL, fee_usd numeric, source text NOT NULL,
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);

CREATE FUNCTION lab.guard_managed_reservation() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
DECLARE d lab.managed_risk_decisions; s lab.managed_setups; c record; total numeric;
BEGIN
 PERFORM pg_advisory_xact_lock(719172026);
 SELECT * INTO d FROM lab.managed_risk_decisions WHERE decision_id=NEW.decision_id;
 SELECT * INTO s FROM lab.managed_setups WHERE setup_id=NEW.setup_id;
 SELECT * INTO c FROM lab.current_classifications WHERE ticker=s.symbol;
 IF current_user<>'catalyst_risk' OR d.outcome IS DISTINCT FROM 'APPROVED'
 OR d.action IS DISTINCT FROM 'ENTRY' OR d.setup_id IS DISTINCT FROM NEW.setup_id
 OR d.transaction_id<>txid_current() OR d.expires_at<=clock_timestamp()
 OR NEW.budget<>d.equity*0.01 OR NEW.planned_risk>NEW.budget
 OR NEW.qty*NEW.max_entry>d.equity OR NEW.sector IS DISTINCT FROM c.sector
 OR NEW.theme IS DISTINCT FROM c.theme OR c.ticker IS NULL
 OR NEW.max_entry<>(s.record_json->'levels'->>'max_entry_price')::numeric
 OR NEW.planned_risk<>NEW.qty*(NEW.max_entry-(s.record_json->'levels'->>'stop')::numeric)
 OR NEW.qty<>(d.payload->>'qty')::numeric
 THEN RAISE EXCEPTION 'INVALID_MANAGED_RISK_RESERVATION'; END IF;
 SELECT coalesce(sum(budget),0) INTO total FROM lab.account_risk_reservations;
 IF total+NEW.budget>0.02*d.equity THEN RAISE EXCEPTION 'COMBINED_RISK_CAP'; END IF;
 IF EXISTS(SELECT 1 FROM lab.account_risk_reservations WHERE sector=NEW.sector OR theme=NEW.theme)
 THEN RAISE EXCEPTION 'CORRELATION_LIMIT'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_reservation BEFORE INSERT ON lab.managed_reservations
 FOR EACH ROW EXECUTE FUNCTION lab.guard_managed_reservation();
-- Both engines account for each other under the same lock, including direct DB inserts.
CREATE FUNCTION lab.guard_legacy_shared_budget() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
DECLARE equity numeric; total numeric; BEGIN
 PERFORM pg_advisory_xact_lock(719172026);
 SELECT d.equity INTO equity FROM lab.risk_decisions d WHERE d.risk_decision_id=NEW.risk_decision_id;
 SELECT coalesce(sum(budget),0) INTO total FROM lab.account_risk_reservations;
 IF total+NEW.budget>0.02*equity THEN RAISE EXCEPTION 'COMBINED_RISK_CAP'; END IF;
 IF EXISTS(SELECT 1 FROM lab.managed_active_reservations WHERE sector=NEW.sector OR theme=NEW.theme)
 THEN RAISE EXCEPTION 'CORRELATION_LIMIT'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_shared_budget BEFORE INSERT ON lab.risk_reservations
 FOR EACH ROW EXECUTE FUNCTION lab.guard_legacy_shared_budget();

DO $$ DECLARE n text; BEGIN
 FOREACH n IN ARRAY ARRAY['managed_setups','managed_events','managed_risk_decisions',
 'managed_claims','managed_reservations','managed_releases','managed_fills'] LOOP
 EXECUTE format('CREATE TRIGGER audit_jev BEFORE INSERT ON lab.%I FOR EACH ROW EXECUTE FUNCTION lab.audit_jev_row()',n);
 EXECUTE format('CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON lab.%I FOR EACH ROW EXECUTE FUNCTION lab.reject_mutation()',n);
 EXECUTE format('CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON lab.%I FOR EACH STATEMENT EXECUTE FUNCTION lab.reject_mutation()',n);
 EXECUTE format('REVOKE ALL ON lab.%I FROM PUBLIC',n);
 EXECUTE format('GRANT SELECT ON lab.%I TO catalyst_app,catalyst_review',n);
 EXECUTE format('GRANT INSERT ON lab.%I TO catalyst_risk',n);
 END LOOP;
END $$;
GRANT SELECT ON lab.managed_states,lab.managed_active_reservations,lab.account_risk_reservations TO catalyst_app,catalyst_review;
REVOKE ALL ON FUNCTION lab.guard_managed_reservation(),lab.guard_legacy_shared_budget() FROM PUBLIC;

CREATE FUNCTION lab.managed_review_failure(packet jsonb) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE selected lab.managed_events; latest lab.managed_events; receipt lab.jev_receipts;
        request lab.jev_requests; answers jsonb; field text;
BEGIN
 SELECT * INTO selected FROM lab.managed_events
  WHERE event_seq=(packet->>'selection_event_seq')::bigint;
 IF selected.event_id IS NULL OR selected.kind<>'RESEARCH_SELECTED'
 OR selected.body->'packet' IS DISTINCT FROM packet-'selection_event_seq'
 OR NOT lab.research_audit_matches('MANAGED_EVENTS',to_jsonb(selected),selected.event_seq)
 THEN RETURN 'SELECTION_INTEGRITY_FAILURE'; END IF;
 SELECT * INTO latest FROM lab.managed_events WHERE kind='RESEARCH_PACKET'
 AND body->>'cycle_id'=packet->>'cycle_id' AND body->>'item_key'=packet->>'item_key'
 ORDER BY event_seq DESC LIMIT 1;
 IF latest.event_id IS NULL OR latest.body->>'revision' IS DISTINCT FROM packet->>'revision'
 OR latest.body->>'evidence_hash' IS DISTINCT FROM packet->>'evidence_hash'
 OR NOT lab.research_audit_matches('MANAGED_EVENTS',to_jsonb(latest),latest.event_seq)
 THEN RETURN 'REVIEW_SUPERSEDED_OR_UNBOUND'; END IF;
 SELECT * INTO receipt FROM lab.jev_receipts WHERE receipt_id=(packet->>'receipt_id')::uuid;
 SELECT * INTO request FROM lab.jev_requests WHERE request_id=receipt.request_id;
 IF NOT lab.research_receipt_intact(receipt.receipt_id)
 OR request.evidence_identity->>'evidence_hash' IS DISTINCT FROM packet->>'evidence_hash'
 OR request.evidence_identity->>'candidate_revision' IS DISTINCT FROM packet->>'revision'
 OR request.evidence_identity->>'cycle_id' IS DISTINCT FROM packet->>'cycle_id'
 OR request.evidence_identity->>'research_item_key' IS DISTINCT FROM packet->>'item_key'
 THEN RETURN 'RECEIPT_BINDING_FAILURE'; END IF;
 IF (packet->>'review_valid_until') IS NULL OR (packet->>'expires_at') IS NULL
 OR clock_timestamp()>=least((packet->>'review_valid_until')::timestamptz,
                            (packet->>'expires_at')::timestamptz)
 OR receipt.completed_at>request.deadline THEN RETURN 'REVIEW_EXPIRED'; END IF;
 IF request.request_json::jsonb->'state' IS DISTINCT FROM latest.body->'state'
 OR packet->'levels' IS DISTINCT FROM latest.body->'state'->'levels'
 OR packet->>'symbol' IS DISTINCT FROM latest.body->>'symbol'
 OR packet->>'expires_at' IS DISTINCT FROM latest.body->>'expires_at'
 OR packet->>'market' IS DISTINCT FROM
    (CASE WHEN latest.body->>'market'='US' THEN 'US_STOCKS' ELSE latest.body->>'market' END)
 THEN RETURN 'RESEARCH_CONTENT_BINDING_FAILURE'; END IF;
 FOREACH field IN ARRAY ARRAY['thesis','disproof','sources'] LOOP
  IF packet->field IS DISTINCT FROM latest.body->'state'->field
  THEN RETURN 'RESEARCH_CONTENT_BINDING_FAILURE'; END IF;
 END LOOP;
 answers:=convert_from(receipt.response_bytes,'UTF8')::jsonb->'answers';
 IF answers->'verdict'->>'choice' IS DISTINCT FROM 'APPROVE'
 OR answers->'news_stale'->>'choice' IS DISTINCT FROM 'NO'
 OR answers->'unsupported_inference'->>'choice' IS DISTINCT FROM 'NO'
 OR answers->'already_priced'->>'choice' NOT IN ('LOW','MEDIUM')
 THEN RETURN 'JEV_SELECTION_REQUIRED'; END IF;
 RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION lab.managed_review_failure(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.managed_review_failure(jsonb) TO catalyst_risk;

INSERT INTO lab.schema_migrations(version) VALUES(13);
