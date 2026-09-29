-- Operator controls: audited halt release, an entry-only pause and a durable flatten request.
-- DDL only: no existing table gains a column and no historical row or event is rewritten.
-- Halt records stay immutable. lab.execution_halts becomes the view of unreleased records
-- that every existing reader and the positional halt INSERT already use.
ALTER TABLE lab.execution_halts RENAME TO execution_halt_records;

CREATE ROLE catalyst_operator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
GRANT USAGE ON SCHEMA lab TO catalyst_operator;
-- The operator role receives no table grants; it acts only through the functions below.

CREATE TABLE lab.execution_halt_releases (
 halt_id bigint PRIMARY KEY REFERENCES lab.execution_halt_records(event_seq),
 halt_reason text NOT NULL,
 release_kind text NOT NULL CHECK(release_kind IN ('OPERATOR_RESUME','OPERATOR_RELEASE')),
 reason text NOT NULL CHECK(length(btrim(reason)) BETWEEN 10 AND 2000),
 operator_role text NOT NULL,
 reconciliation_seq bigint REFERENCES lab.trade_events(seq),
 correction_seq bigint REFERENCES lab.trade_events(seq),
 released_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq),
 CHECK((release_kind='OPERATOR_RELEASE')=(reconciliation_seq IS NOT NULL))
);
-- Explicit RESIDUAL_ACCEPTED decision for a crypto dust halt; it releases nothing by itself.
CREATE TABLE lab.execution_halt_residual_acceptances (
 halt_id bigint PRIMARY KEY REFERENCES lab.execution_halt_records(event_seq),
 decision text NOT NULL CHECK(decision='RESIDUAL_ACCEPTED'),
 reason text NOT NULL CHECK(length(btrim(reason)) BETWEEN 10 AND 2000),
 operator_role text NOT NULL,
 accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);
-- Durable request only. Broker execution of a flatten request is wired by a later package.
CREATE TABLE lab.operator_flatten_requests (
 request_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 scope text NOT NULL CHECK(scope='ALL_POSITIONS_AND_ORDERS'),
 reason text NOT NULL CHECK(length(btrim(reason)) BETWEEN 1 AND 2000),
 operator_role text NOT NULL,
 requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);

-- Auto-updatable view: SELECT and the positional INSERT reach the immutable records table.
CREATE VIEW lab.execution_halts AS
 SELECT r.* FROM lab.execution_halt_records r
 WHERE NOT EXISTS(SELECT 1 FROM lab.execution_halt_releases x WHERE x.halt_id=r.event_seq);

CREATE FUNCTION lab.halt_release_requirement(halt_reason text) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT CASE WHEN halt_reason='OPERATOR_PAUSE' THEN 'OPERATOR_RESUME'
  -- Broker trade_bust/trade_correct/replaced events (broker_ledger.consume).
  WHEN halt_reason='BROKER_EVENT_REQUIRES_MANUAL_REVIEW' THEN 'CORRECTION_EVENT'
  -- Managed crypto dust: RESIDUAL_BELOW_BROKER_MINIMUM and its unprotected variant.
  WHEN halt_reason ~ '^MANAGED_CRYPTO_.*RESIDUAL' THEN 'RESIDUAL_ACCEPTANCE'
  ELSE 'RECONCILIATION' END
$$;

-- Latest reconciliation evidence of either engine recorded after the halt. It must be clean
-- and committed by a later transaction than the halt: the reconciliation that latched a halt
-- can never justify releasing it.
CREATE FUNCTION lab.halt_reconciliation_evidence(halt_ref bigint,
 OUT evidence_seq bigint, OUT clean boolean, OUT later_transaction boolean)
LANGUAGE plpgsql STABLE SET search_path=pg_catalog,lab AS $$
DECLARE halt_xmin xid;
BEGIN
 SELECT r.xmin INTO halt_xmin FROM lab.execution_halt_records r WHERE r.event_seq=halt_ref;
 SELECT x.event_seq,coalesce(x.clean,false),coalesce(x.row_xmin<>halt_xmin,false)
 INTO evidence_seq,clean,later_transaction
 FROM (SELECT r.event_seq,r.clean,r.xmin AS row_xmin FROM lab.reconciliation_runs r
       WHERE r.event_seq>halt_ref
       UNION ALL
       SELECT e.event_seq,CASE WHEN jsonb_typeof(e.body->'clean')='boolean'
         THEN (e.body->>'clean')::boolean END,e.xmin
       FROM lab.managed_events e WHERE e.kind='BROKER_RECONCILIATION' AND e.event_seq>halt_ref
 ) x ORDER BY x.event_seq DESC LIMIT 1;
 clean:=coalesce(clean,false);
 later_transaction:=coalesce(later_transaction,false);
END $$;

-- Same in-flight test as reconciliation.py (BROKER_AUTHORIZATION_IN_FLIGHT) for the frozen
-- engine; a managed claim is unresolved until its BROKER_ACK or BROKER_REJECTED is recorded.
CREATE FUNCTION lab.unresolved_authorization_claims() RETURNS bigint
LANGUAGE sql STABLE SET search_path=pg_catalog,lab AS $$
 SELECT (SELECT count(*) FROM lab.authorization_claims c
   LEFT JOIN lab.current_authorization_results r USING(risk_decision_id)
   WHERE r.outcome IS NULL OR r.outcome IN ('UNKNOWN','NOT_FOUND'))
 + (SELECT count(*) FROM lab.managed_claims c JOIN lab.managed_risk_decisions d USING(decision_id)
   WHERE NOT EXISTS(SELECT 1 FROM lab.managed_events a
     WHERE a.idempotency_key='ack:'||c.decision_id::text)
   AND NOT EXISTS(SELECT 1 FROM lab.managed_events x WHERE x.setup_id=d.setup_id
     AND x.kind='BROKER_REJECTED' AND x.body->>'decision_id'=c.decision_id::text))
$$;

-- Risk-increasing decisions (release, resume, residual acceptance) need a substantive reason;
-- risk-reducing requests (pause, flatten) only a non-blank one.
CREATE FUNCTION lab.operator_reason_failure(reason text, minimum integer) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT CASE WHEN length(btrim(coalesce(reason,'')))=0 THEN 'OPERATOR_REASON_REQUIRED'
  WHEN length(btrim(reason))<minimum THEN 'OPERATOR_REASON_TOO_SHORT'
  WHEN length(btrim(reason))>2000 THEN 'OPERATOR_REASON_TOO_LONG' END
$$;

CREATE FUNCTION lab.halt_release_blockers(halt_ref bigint, kind text, correction_ref bigint)
RETURNS text[] LANGUAGE plpgsql STABLE SET search_path=pg_catalog,lab AS $$
DECLARE h lab.execution_halt_records; requirement text; evidence record; blockers text[]:='{}';
BEGIN
 SELECT * INTO h FROM lab.execution_halt_records WHERE event_seq=halt_ref;
 IF h.event_seq IS NULL THEN RETURN ARRAY['HALT_NOT_FOUND']; END IF;
 IF EXISTS(SELECT 1 FROM lab.execution_halt_releases WHERE halt_id=halt_ref) THEN
  RETURN ARRAY['HALT_ALREADY_RELEASED'];
 END IF;
 requirement:=lab.halt_release_requirement(h.reason);
 IF requirement='OPERATOR_RESUME' THEN
  IF kind IS DISTINCT FROM 'OPERATOR_RESUME' THEN
   RETURN ARRAY['OPERATOR_PAUSE_REQUIRES_RESUME'];
  END IF;
  IF correction_ref IS NOT NULL THEN RETURN ARRAY['CORRECTION_EVENT_NOT_APPLICABLE']; END IF;
  RETURN blockers;
 END IF;
 IF kind IS DISTINCT FROM 'OPERATOR_RELEASE' THEN
  RETURN ARRAY['RESUME_RELEASES_OPERATOR_PAUSE_ONLY'];
 END IF;
 SELECT * INTO evidence FROM lab.halt_reconciliation_evidence(halt_ref);
 IF evidence.evidence_seq IS NULL
  OR NOT coalesce(evidence.clean AND evidence.later_transaction,false) THEN
  blockers:=blockers||'CLEAN_RECONCILIATION_AFTER_HALT_REQUIRED'::text;
 END IF;
 IF lab.unresolved_authorization_claims()>0 THEN
  blockers:=blockers||'AUTHORIZATION_CLAIMS_UNRESOLVED'::text;
 END IF;
 IF requirement='CORRECTION_EVENT' THEN
  -- A later CORRECTION event must correct an event of the broker order under review.
  IF correction_ref IS NULL OR NOT EXISTS(SELECT 1 FROM lab.trade_events c
    JOIN lab.trade_events o ON o.event_id=c.correction_of
    JOIN lab.broker_events b ON b.event_seq=o.seq
    WHERE c.seq=correction_ref AND c.event_type='CORRECTION' AND c.seq>h.event_seq
    AND b.broker_order_id=h.payload_json->>'broker_order_id') THEN
   blockers:=blockers||'CORRECTION_EVENT_REQUIRED'::text;
  END IF;
 ELSIF correction_ref IS NOT NULL THEN
  blockers:=blockers||'CORRECTION_EVENT_NOT_APPLICABLE'::text;
 END IF;
 IF requirement='RESIDUAL_ACCEPTANCE' AND NOT EXISTS(
   SELECT 1 FROM lab.execution_halt_residual_acceptances WHERE halt_id=halt_ref) THEN
  blockers:=blockers||'RESIDUAL_ACCEPTANCE_REQUIRED'::text;
 END IF;
 RETURN blockers;
END $$;

-- Preconditions are enforced on every insert, including a direct owner insert.
CREATE FUNCTION lab.guard_halt_release() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
DECLARE blockers text[]; evidence record; failure text;
BEGIN
 PERFORM pg_advisory_xact_lock(719172026);
 IF NEW.operator_role IS DISTINCT FROM session_user::text THEN
  RAISE EXCEPTION 'OPERATOR_IDENTITY_MISMATCH';
 END IF;
 failure:=lab.operator_reason_failure(NEW.reason,10);
 IF failure IS NOT NULL THEN RAISE EXCEPTION '%',failure; END IF;
 blockers:=lab.halt_release_blockers(NEW.halt_id,NEW.release_kind,NEW.correction_seq);
 IF cardinality(blockers)>0 THEN RAISE EXCEPTION '%',blockers[1]; END IF;
 IF NEW.halt_reason IS DISTINCT FROM
   (SELECT reason FROM lab.execution_halt_records WHERE event_seq=NEW.halt_id) THEN
  RAISE EXCEPTION 'HALT_REASON_MISMATCH';
 END IF;
 IF NEW.release_kind='OPERATOR_RELEASE' THEN
  SELECT * INTO evidence FROM lab.halt_reconciliation_evidence(NEW.halt_id);
  IF NEW.reconciliation_seq IS DISTINCT FROM evidence.evidence_seq THEN
   RAISE EXCEPTION 'RECONCILIATION_EVIDENCE_MISMATCH';
  END IF;
 ELSIF NEW.reconciliation_seq IS NOT NULL OR NEW.correction_seq IS NOT NULL THEN
  RAISE EXCEPTION 'RESUME_TAKES_NO_EVIDENCE';
 END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION lab.guard_residual_acceptance() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
DECLARE h lab.execution_halt_records; failure text;
BEGIN
 PERFORM pg_advisory_xact_lock(719172026);
 IF NEW.operator_role IS DISTINCT FROM session_user::text THEN
  RAISE EXCEPTION 'OPERATOR_IDENTITY_MISMATCH';
 END IF;
 failure:=lab.operator_reason_failure(NEW.reason,10);
 IF failure IS NOT NULL THEN RAISE EXCEPTION '%',failure; END IF;
 SELECT * INTO h FROM lab.execution_halt_records WHERE event_seq=NEW.halt_id;
 IF h.event_seq IS NULL THEN RAISE EXCEPTION 'HALT_NOT_FOUND'; END IF;
 IF lab.halt_release_requirement(h.reason)<>'RESIDUAL_ACCEPTANCE' THEN
  RAISE EXCEPTION 'RESIDUAL_ACCEPTANCE_NOT_APPLICABLE';
 END IF;
 IF EXISTS(SELECT 1 FROM lab.execution_halt_releases WHERE halt_id=NEW.halt_id) THEN
  RAISE EXCEPTION 'HALT_ALREADY_RELEASED';
 END IF;
 IF EXISTS(SELECT 1 FROM lab.execution_halt_residual_acceptances WHERE halt_id=NEW.halt_id) THEN
  RAISE EXCEPTION 'RESIDUAL_ALREADY_ACCEPTED';
 END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION lab.guard_flatten_request() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
DECLARE failure text:=lab.operator_reason_failure(NEW.reason,1);
BEGIN
 IF NEW.operator_role IS DISTINCT FROM session_user::text THEN
  RAISE EXCEPTION 'OPERATOR_IDENTITY_MISMATCH';
 END IF;
 IF failure IS NOT NULL THEN RAISE EXCEPTION '%',failure; END IF;
 RETURN NEW;
END $$;

DO $$ DECLARE name text; BEGIN
 FOREACH name IN ARRAY ARRAY['execution_halt_releases','execution_halt_residual_acceptances',
  'operator_flatten_requests'] LOOP
  EXECUTE format('CREATE TRIGGER audit_jev BEFORE INSERT ON lab.%I
    FOR EACH ROW EXECUTE FUNCTION lab.audit_jev_row()',name);
  EXECUTE format('CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON lab.%I
    FOR EACH ROW EXECUTE FUNCTION lab.reject_mutation()',name);
  EXECUTE format('CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON lab.%I
    FOR EACH STATEMENT EXECUTE FUNCTION lab.reject_mutation()',name);
 END LOOP;
END $$;
CREATE TRIGGER guard_release BEFORE INSERT ON lab.execution_halt_releases
 FOR EACH ROW EXECUTE FUNCTION lab.guard_halt_release();
CREATE TRIGGER guard_acceptance BEFORE INSERT ON lab.execution_halt_residual_acceptances
 FOR EACH ROW EXECUTE FUNCTION lab.guard_residual_acceptance();
CREATE TRIGGER guard_request BEFORE INSERT ON lab.operator_flatten_requests
 FOR EACH ROW EXECUTE FUNCTION lab.guard_flatten_request();

-- Recorded exactly like execution.halt(): RISK_HALT event, system event and halt record.
-- It blocks entry paths only; no reader turns a halt into an exit.
CREATE FUNCTION lab.operator_pause(reason text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab SET TimeZone='UTC' AS $$
DECLARE note text:=btrim(coalesce(reason,'')); details jsonb; recorded lab.trade_events;
 failure text:=lab.operator_reason_failure(reason,1);
BEGIN
 IF failure IS NOT NULL THEN RAISE EXCEPTION '%',failure; END IF;
 PERFORM pg_advisory_xact_lock(719172026);
 details:=jsonb_build_object('operator_reason',note,'operator_role',session_user::text,
   'scope','ENTRY_ONLY');
 INSERT INTO lab.trade_events(strategy_version,event_type,payload_json)
 VALUES('CATALYST_RETEST_V1','SYSTEM_EVENT',
   jsonb_build_object('kind','RISK_HALT','reason','OPERATOR_PAUSE')||details)
 RETURNING * INTO recorded;
 INSERT INTO lab.system_events(event_id,event_type,payload_json)
 VALUES(recorded.event_id,'RISK_HALT',jsonb_build_object('reason','OPERATOR_PAUSE')||details);
 INSERT INTO lab.execution_halt_records(event_seq,reason,candidate_id,payload_json)
 VALUES(recorded.seq,'OPERATOR_PAUSE',NULL,details);
 RETURN recorded.seq;
END $$;

CREATE FUNCTION lab.operator_resume(reason text) RETURNS bigint[]
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab SET TimeZone='UTC' AS $$
DECLARE note text:=btrim(coalesce(reason,'')); pause bigint; released bigint[]:='{}';
BEGIN
 PERFORM pg_advisory_xact_lock(719172026);
 FOR pause IN SELECT r.event_seq FROM lab.execution_halt_records r
   WHERE r.reason='OPERATOR_PAUSE' AND NOT EXISTS(
     SELECT 1 FROM lab.execution_halt_releases x WHERE x.halt_id=r.event_seq)
   ORDER BY r.event_seq LOOP
  INSERT INTO lab.execution_halt_releases(halt_id,halt_reason,release_kind,reason,operator_role)
  VALUES(pause,'OPERATOR_PAUSE','OPERATOR_RESUME',note,session_user::text);
  released:=released||pause;
 END LOOP;
 IF cardinality(released)=0 THEN RAISE EXCEPTION 'NO_ACTIVE_OPERATOR_PAUSE'; END IF;
 RETURN released;
END $$;

CREATE FUNCTION lab.operator_release_halt(halt_ref bigint, reason text,
 correction_ref bigint DEFAULT NULL) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab SET TimeZone='UTC' AS $$
DECLARE h lab.execution_halt_records; evidence record; released lab.execution_halt_releases;
BEGIN
 PERFORM pg_advisory_xact_lock(719172026);
 SELECT * INTO h FROM lab.execution_halt_records WHERE event_seq=halt_ref;
 IF h.event_seq IS NULL THEN RAISE EXCEPTION 'HALT_NOT_FOUND'; END IF;
 SELECT * INTO evidence FROM lab.halt_reconciliation_evidence(halt_ref);
 INSERT INTO lab.execution_halt_releases(halt_id,halt_reason,release_kind,reason,operator_role,
   reconciliation_seq,correction_seq)
 VALUES(halt_ref,h.reason,'OPERATOR_RELEASE',btrim(coalesce(reason,'')),session_user::text,
   evidence.evidence_seq,correction_ref)
 RETURNING * INTO released;
 RETURN jsonb_build_object('halt_id',released.halt_id,'halt_reason',released.halt_reason,
   'released',true,'reconciliation_seq',released.reconciliation_seq,
   'correction_seq',released.correction_seq,'event_seq',released.event_seq);
END $$;

CREATE FUNCTION lab.operator_accept_residual(halt_ref bigint, reason text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab SET TimeZone='UTC' AS $$
DECLARE recorded bigint;
BEGIN
 PERFORM pg_advisory_xact_lock(719172026);
 INSERT INTO lab.execution_halt_residual_acceptances(halt_id,decision,reason,operator_role)
 VALUES(halt_ref,'RESIDUAL_ACCEPTED',btrim(coalesce(reason,'')),session_user::text)
 RETURNING event_seq INTO recorded;
 RETURN recorded;
END $$;

CREATE FUNCTION lab.operator_request_flatten_all(reason text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab SET TimeZone='UTC' AS $$
DECLARE request uuid;
BEGIN
 PERFORM pg_advisory_xact_lock(719172026);
 INSERT INTO lab.operator_flatten_requests(scope,reason,operator_role)
 VALUES('ALL_POSITIONS_AND_ORDERS',btrim(coalesce(reason,'')),session_user::text)
 RETURNING request_id INTO request;
 RETURN request;
END $$;

CREATE FUNCTION lab.operator_list_halts(include_released boolean DEFAULT false)
RETURNS TABLE(halt_id bigint, reason text, candidate_id uuid, payload_json jsonb,
 recorded_at timestamptz, release_requirement text, residual_accepted boolean,
 release_blockers text[], released boolean, release_kind text, release_reason text,
 released_by text, released_at timestamptz, reconciliation_seq bigint, correction_seq bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
 SELECT r.event_seq,r.reason,r.candidate_id,r.payload_json,e.created_at,
  lab.halt_release_requirement(r.reason),
  EXISTS(SELECT 1 FROM lab.execution_halt_residual_acceptances a WHERE a.halt_id=r.event_seq),
  CASE WHEN x.halt_id IS NULL THEN lab.halt_release_blockers(r.event_seq,
    CASE WHEN r.reason='OPERATOR_PAUSE' THEN 'OPERATOR_RESUME' ELSE 'OPERATOR_RELEASE' END,
    NULL) END,
  x.halt_id IS NOT NULL,x.release_kind,x.reason,x.operator_role,x.released_at,
  x.reconciliation_seq,x.correction_seq
 FROM lab.execution_halt_records r JOIN lab.trade_events e ON e.seq=r.event_seq
 LEFT JOIN lab.execution_halt_releases x ON x.halt_id=r.event_seq
 WHERE include_released OR x.halt_id IS NULL ORDER BY r.event_seq
$$;

REVOKE ALL ON lab.execution_halts,lab.execution_halt_releases,
 lab.execution_halt_residual_acceptances,lab.operator_flatten_requests FROM PUBLIC;
-- Identical to the renamed table's grants, so the readers and positional INSERT keep working.
GRANT SELECT,INSERT ON lab.execution_halts TO catalyst_app;
GRANT SELECT ON lab.execution_halt_releases,lab.execution_halt_residual_acceptances,
 lab.operator_flatten_requests TO catalyst_app;
REVOKE ALL ON FUNCTION lab.halt_release_requirement(text),lab.operator_reason_failure(text,integer),
 lab.halt_reconciliation_evidence(bigint),lab.unresolved_authorization_claims(),
 lab.halt_release_blockers(bigint,text,bigint),lab.guard_halt_release(),
 lab.guard_residual_acceptance(),lab.guard_flatten_request(),lab.operator_pause(text),
 lab.operator_resume(text),lab.operator_release_halt(bigint,text,bigint),
 lab.operator_accept_residual(bigint,text),lab.operator_request_flatten_all(text),
 lab.operator_list_halts(boolean) FROM PUBLIC;
-- A pause only reduces risk, so the risk engine may also record one. Nothing else is shared.
GRANT EXECUTE ON FUNCTION lab.operator_pause(text) TO catalyst_operator,catalyst_risk;
GRANT EXECUTE ON FUNCTION lab.operator_resume(text),
 lab.operator_release_halt(bigint,text,bigint),lab.operator_accept_residual(bigint,text),
 lab.operator_request_flatten_all(text),lab.operator_list_halts(boolean) TO catalyst_operator;
INSERT INTO lab.schema_migrations(version) VALUES(15);
