-- Explicit US engineering cohort. Research SELECTED still grants no broker permission.
CREATE TABLE lab.jev_paper_admissions (
 item_id uuid PRIMARY KEY REFERENCES lab.research_report_items,
 candidate_id uuid UNIQUE REFERENCES lab.candidates,
 receipt_id uuid REFERENCES lab.jev_receipts,
 policy_id text NOT NULL CHECK(policy_id='JEV_US_SELECTED_FIXED_TEST_V1'),
 cohort text NOT NULL CHECK(cohort='JEV_US_SELECTED_FIXED_ENGINEERING'),
 outcome text NOT NULL CHECK(outcome IN ('VALIDATED','REJECTED')),
 reason text NOT NULL, expires_at timestamptz NOT NULL,
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);
CREATE TRIGGER audit_jev BEFORE INSERT ON lab.jev_paper_admissions
 FOR EACH ROW EXECUTE FUNCTION lab.audit_jev_row();
CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON lab.jev_paper_admissions
 FOR EACH ROW EXECUTE FUNCTION lab.reject_mutation();
CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON lab.jev_paper_admissions
 FOR EACH STATEMENT EXECUTE FUNCTION lab.reject_mutation();
REVOKE ALL ON lab.jev_paper_admissions FROM PUBLIC;
GRANT SELECT ON lab.jev_paper_admissions TO catalyst_app,catalyst_review;
GRANT INSERT ON lab.jev_paper_admissions TO catalyst_risk;
CREATE TABLE lab.jev_paper_revocations (
 candidate_id uuid PRIMARY KEY REFERENCES lab.candidates, reason text NOT NULL,
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);
CREATE TRIGGER audit_jev BEFORE INSERT ON lab.jev_paper_revocations
 FOR EACH ROW EXECUTE FUNCTION lab.audit_jev_row();
CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON lab.jev_paper_revocations
 FOR EACH ROW EXECUTE FUNCTION lab.reject_mutation();
CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON lab.jev_paper_revocations
 FOR EACH STATEMENT EXECUTE FUNCTION lab.reject_mutation();
REVOKE ALL ON lab.jev_paper_revocations FROM PUBLIC;
GRANT SELECT ON lab.jev_paper_revocations TO catalyst_app,catalyst_review;
GRANT INSERT ON lab.jev_paper_revocations TO catalyst_risk;

CREATE FUNCTION lab.jev_us_review_failure(item uuid) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE i lab.research_report_items; r lab.research_reports; o lab.research_outcomes;
 req lab.jev_requests;
BEGIN
 SELECT * INTO i FROM lab.research_report_items WHERE item_id=item;
 SELECT * INTO r FROM lab.research_reports WHERE report_id=i.report_id AND revision=i.revision;
 SELECT * INTO o FROM lab.research_outcomes WHERE item_id=item;
 SELECT * INTO req FROM lab.jev_requests WHERE request_id=o.request_id;
 IF i.item_id IS NULL OR r.market IS DISTINCT FROM 'US_STOCKS'
 OR r.record_purpose IS DISTINCT FROM 'ENGINEERING_TEST' THEN RETURN 'US_ENGINEERING_REVIEW_REQUIRED'; END IF;
 IF clock_timestamp()>=i.evidence_deadline THEN RETURN 'REVIEW_EXPIRED'; END IF;
 IF EXISTS(SELECT 1 FROM lab.research_report_items n JOIN lab.research_reports nr
   ON nr.report_id=n.report_id AND nr.revision=n.revision
   WHERE n.symbol=i.symbol AND nr.market=r.market AND nr.timeframe=r.timeframe
    AND n.event_seq>i.event_seq) OR
    i.revision<>(SELECT max(revision) FROM lab.research_reports WHERE report_id=i.report_id)
 THEN RETURN 'REVIEW_SUPERSEDED'; END IF;
 IF o.disposition IS DISTINCT FROM 'SELECTED' THEN RETURN 'JEV_SELECTION_REQUIRED'; END IF;
 IF NOT lab.research_audit_matches('RESEARCH_REPORTS',to_jsonb(r),r.event_seq)
 OR NOT lab.research_audit_matches('RESEARCH_REPORT_ITEMS',to_jsonb(i),i.event_seq)
 OR NOT lab.research_audit_matches('RESEARCH_OUTCOMES',to_jsonb(o),o.event_seq)
 OR NOT lab.research_receipt_intact(o.receipt_id) THEN RETURN 'RECEIPT_INTEGRITY_FAILURE'; END IF;
 IF lab.review_halted() THEN RETURN 'REVIEW_OPERATOR_HALTED'; END IF;
 IF NOT EXISTS(SELECT 1 FROM lab.review_worker_status WHERE status='RUNNING'
   AND scope_id=req.evidence_identity->>'runtime_scope') THEN RETURN 'WORKER_HEARTBEAT_DOWN'; END IF;
 IF lab.review_breaker_state(req.evidence_identity->>'runtime_scope')->>'state'<>'CLOSED'
 THEN RETURN 'JEV_PROVIDER_UNAVAILABLE'; END IF;
 RETURN NULL;
END $$;

CREATE FUNCTION lab.jev_us_item(item uuid) RETURNS jsonb
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
 SELECT jsonb_build_object('item_id',i.item_id,'research',i.record_json->'research',
  'item_hash',i.item_hash,'expires_at',i.evidence_deadline,'receipt_id',o.receipt_id,
  'failure',lab.jev_us_review_failure(i.item_id))
 FROM lab.research_report_items i LEFT JOIN lab.research_outcomes o USING(item_id) WHERE i.item_id=item
$$;
CREATE FUNCTION lab.jev_us_pending() RETURNS SETOF uuid
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
 SELECT i.item_id FROM lab.research_report_items i JOIN lab.research_outcomes o USING(item_id)
 WHERE o.disposition='SELECTED' AND NOT EXISTS
  (SELECT 1 FROM lab.jev_paper_admissions a WHERE a.item_id=i.item_id)
 AND lab.jev_us_review_failure(i.item_id) IS NULL ORDER BY o.event_seq LIMIT 50
$$;

CREATE FUNCTION lab.validate_jev_paper_admission() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE c lab.candidates; i lab.research_report_items; body jsonb; k text;
BEGIN
 PERFORM pg_advisory_xact_lock(719172026);
 IF NEW.outcome='VALIDATED' THEN
  SELECT * INTO i FROM lab.research_report_items WHERE item_id=NEW.item_id;
  SELECT * INTO c FROM lab.candidates WHERE candidate_id=NEW.candidate_id;
  body:=i.record_json->'research';
  IF c.candidate_id IS NULL OR c.record_purpose<>'ENGINEERING_TEST'
    OR c.strategy_version<>'CATALYST_RETEST_V1' OR c.ticker IS DISTINCT FROM i.symbol
    OR c.signal_id IS DISTINCT FROM 'TEST-JEV-'||i.item_id::text
    OR c.submission_context->>'origin' IS DISTINCT FROM 'JEV_US_SELECTED_FIXED_TEST_V1'
    OR c.submission_context->>'research_item_id' IS DISTINCT FROM i.item_id::text
    OR NEW.receipt_id IS DISTINCT FROM (SELECT receipt_id FROM lab.research_outcomes WHERE item_id=i.item_id)
    OR NEW.expires_at>i.evidence_deadline OR NEW.expires_at<=clock_timestamp()
    OR lab.jev_us_review_failure(i.item_id) IS NOT NULL
    OR NOT EXISTS(SELECT 1 FROM lab.candidate_states WHERE candidate_id=c.candidate_id AND state='VALIDATED')
    OR NOT EXISTS(SELECT 1 FROM lab.validation_decisions WHERE candidate_id=c.candidate_id AND passed
      AND expires_at=NEW.expires_at) THEN RAISE EXCEPTION 'INVALID_JEV_PAPER_BINDING'; END IF;
  FOREACH k IN ARRAY ARRAY['entry_trigger','max_entry_price','stop','target'] LOOP
   IF (c.payload_json->>k)::numeric IS DISTINCT FROM (body->'levels'->>k)::numeric THEN
    RAISE EXCEPTION 'JEV_LEVEL_BINDING_MISMATCH'; END IF;
  END LOOP;
  IF c.payload_json->>'thesis' IS DISTINCT FROM body->>'thesis'
    OR c.payload_json->>'disproof' IS DISTINCT FROM body->>'disproof'
    OR c.payload_json->>'catalyst' IS DISTINCT FROM body->>'catalyst'
  THEN RAISE EXCEPTION 'JEV_RESEARCH_BINDING_MISMATCH'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER validate_binding BEFORE INSERT ON lab.jev_paper_admissions
 FOR EACH ROW EXECUTE FUNCTION lab.validate_jev_paper_admission();

CREATE FUNCTION lab.jev_us_entry_failure(cid uuid) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE c lab.candidates; a lab.jev_paper_admissions; BEGIN
 SELECT * INTO c FROM lab.candidates WHERE candidate_id=cid;
 SELECT * INTO a FROM lab.jev_paper_admissions WHERE candidate_id=cid;
 IF c.submission_context->>'origin' IS DISTINCT FROM 'JEV_US_SELECTED_FIXED_TEST_V1'
    AND a.candidate_id IS NULL THEN RETURN NULL; END IF;
 IF a.candidate_id IS NULL OR a.outcome<>'VALIDATED' THEN RETURN 'JEV_BINDING_REQUIRED'; END IF;
 IF EXISTS(SELECT 1 FROM lab.jev_paper_revocations WHERE candidate_id=cid)
 THEN RETURN 'JEV_AUTHORIZATION_REVOKED'; END IF;
 IF NOT lab.research_audit_matches('JEV_PAPER_ADMISSIONS',to_jsonb(a),a.event_seq)
 THEN RETURN 'JEV_BINDING_INTEGRITY_FAILURE'; END IF;
 IF clock_timestamp()>=a.expires_at THEN RETURN 'REVIEW_EXPIRED'; END IF;
 RETURN lab.jev_us_review_failure(a.item_id);
END $$;
CREATE FUNCTION lab.guard_jev_entry() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE cid uuid; action text; why text;
BEGIN
 IF TG_TABLE_NAME='risk_decisions' THEN
  IF NEW.decision<>'APPROVED' OR NEW.action<>'ENTRY' THEN RETURN NEW; END IF;
  cid:=NEW.candidate_id;
 ELSE
  SELECT candidate_id,d.action INTO cid,action FROM lab.risk_decisions d
    WHERE risk_decision_id=NEW.risk_decision_id;
  IF action<>'ENTRY' THEN RETURN NEW; END IF;
 END IF;
 why:=lab.jev_us_entry_failure(cid);
 IF why IS NOT NULL THEN RAISE EXCEPTION 'JEV_ENTRY_REFUSED: %',why; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_jev_entry BEFORE INSERT ON lab.risk_decisions
 FOR EACH ROW EXECUTE FUNCTION lab.guard_jev_entry();
CREATE TRIGGER guard_jev_entry BEFORE INSERT ON lab.authorization_claims
 FOR EACH ROW EXECUTE FUNCTION lab.guard_jev_entry();
REVOKE ALL ON FUNCTION lab.jev_us_review_failure(uuid),lab.jev_us_item(uuid),lab.jev_us_pending(),
 lab.validate_jev_paper_admission(),lab.jev_us_entry_failure(uuid),lab.guard_jev_entry() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.jev_us_review_failure(uuid),lab.jev_us_item(uuid),lab.jev_us_pending(),
 lab.jev_us_entry_failure(uuid) TO catalyst_risk;
CREATE VIEW lab.jev_paper_status AS
 SELECT a.item_id,a.candidate_id,a.cohort,a.policy_id,a.outcome,a.reason,a.expires_at,
 s.state,coalesce(p.qty,0) AS open_qty,r.reason AS revocation_reason
 FROM lab.jev_paper_admissions a LEFT JOIN lab.candidate_states s USING(candidate_id)
 LEFT JOIN lab.strategy_positions p USING(candidate_id)
 LEFT JOIN lab.jev_paper_revocations r USING(candidate_id);
GRANT SELECT ON lab.jev_paper_status TO catalyst_review,catalyst_app;
INSERT INTO lab.schema_migrations(version) VALUES(12);
