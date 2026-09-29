-- Operator flatten execution (follow-up to plan 0.6 and 4.3). The managed account-safety tick
-- consumes lab.operator_flatten_requests (migration 015) through the daily halt's
-- cancel-then-close path, where every cancel and every close has its own exact one-use
-- five-second authorization, and appends each outcome here as audited history.
-- DDL only: no audited row is inserted, no existing table gains a column and no historical row
-- or event is rewritten. The 015 request table, its function and its guard are unchanged, so a
-- pending request keeps its 015 row exactly and simply shows in the pending view below.

-- One row per recorded outcome of a flatten pass over a request. COMPLETED (nothing left at the
-- broker or in either ledger) is written once and ends the request; PARTIAL (a refusal, an
-- unknown response or exposure the app cannot act on) and FAILED (the pass itself failed, for
-- example a rate-limited read) are append-only history, and the request stays pending and is
-- retried by the next tick.
CREATE TABLE lab.operator_flatten_completions (
 completion_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 request_seq bigint NOT NULL REFERENCES lab.operator_flatten_requests(event_seq),
 runtime_id uuid NOT NULL,
 started_at timestamptz NOT NULL,
 finished_at timestamptz NOT NULL,
 cancels_attempted integer NOT NULL CHECK(cancels_attempted>=0),
 cancels_acknowledged integer NOT NULL,
 closes_attempted integer NOT NULL CHECK(closes_attempted>=0),
 closes_acknowledged integer NOT NULL,
 residual_codes text[] NOT NULL,
 outcome text NOT NULL CHECK(outcome IN ('COMPLETED','PARTIAL','FAILED')),
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq),
 CHECK(cancels_acknowledged BETWEEN 0 AND cancels_attempted),
 CHECK(closes_acknowledged BETWEEN 0 AND closes_attempted),
 CHECK(finished_at>=started_at),
 CHECK(cardinality(residual_codes)<=32 AND array_position(residual_codes,NULL) IS NULL
  AND array_to_string(residual_codes,',') ~ '^([A-Z][A-Z0-9_]{2,63}(,[A-Z][A-Z0-9_]{2,63})*)?$'),
 CHECK((outcome='COMPLETED')=(cardinality(residual_codes)=0))
);
CREATE UNIQUE INDEX operator_flatten_one_completed ON lab.operator_flatten_completions(request_seq)
 WHERE outcome='COMPLETED';
CREATE INDEX operator_flatten_completion_history
 ON lab.operator_flatten_completions(request_seq,event_seq);

-- A request is pending until it has a COMPLETED completion.
CREATE VIEW lab.pending_operator_flatten_requests AS
 SELECT r.* FROM lab.operator_flatten_requests r
 WHERE NOT EXISTS(SELECT 1 FROM lab.operator_flatten_completions c
  WHERE c.request_seq=r.event_seq AND c.outcome='COMPLETED');

-- Written only by the running app's risk role, never after the request completed.
CREATE FUNCTION lab.guard_flatten_completion() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(719172026);
 IF current_user<>'catalyst_risk' THEN RAISE EXCEPTION 'FLATTEN_COMPLETION_ROLE_REQUIRED'; END IF;
 IF EXISTS(SELECT 1 FROM lab.operator_flatten_completions c
   WHERE c.request_seq=NEW.request_seq AND c.outcome='COMPLETED') THEN
  RAISE EXCEPTION 'FLATTEN_ALREADY_COMPLETED';
 END IF;
 RETURN NEW;
END $$;

CREATE TRIGGER audit_jev BEFORE INSERT ON lab.operator_flatten_completions
 FOR EACH ROW EXECUTE FUNCTION lab.audit_jev_row();
CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON lab.operator_flatten_completions
 FOR EACH ROW EXECUTE FUNCTION lab.reject_mutation();
CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON lab.operator_flatten_completions
 FOR EACH STATEMENT EXECUTE FUNCTION lab.reject_mutation();
CREATE TRIGGER guard_completion BEFORE INSERT ON lab.operator_flatten_completions
 FOR EACH ROW EXECUTE FUNCTION lab.guard_flatten_completion();

-- The flatten records an OPERATOR_FLATTEN halt, which keeps 015's RECONCILIATION release rule
-- (operator release-halt after a clean reconciliation recorded after the halt, with no
-- unresolved authorization claim) and may not be released while any flatten request is still
-- pending, so entries cannot resume in the middle of a flatten. 015's function is renamed and
-- delegated to, never edited (the pattern of migration 018).
ALTER FUNCTION lab.halt_release_blockers(bigint,text,bigint)
 RENAME TO halt_release_blockers_before_019;
CREATE FUNCTION lab.halt_release_blockers(halt_ref bigint, kind text, correction_ref bigint)
RETURNS text[] LANGUAGE plpgsql STABLE SET search_path=pg_catalog,lab AS $$
DECLARE blockers text[]:=lab.halt_release_blockers_before_019(halt_ref,kind,correction_ref);
BEGIN
 IF EXISTS(SELECT 1 FROM lab.execution_halt_records r WHERE r.event_seq=halt_ref
   AND r.reason='OPERATOR_FLATTEN'
   AND NOT EXISTS(SELECT 1 FROM lab.execution_halt_releases x WHERE x.halt_id=r.event_seq))
 AND EXISTS(SELECT 1 FROM lab.pending_operator_flatten_requests) THEN
  blockers:=blockers||'OPERATOR_FLATTEN_PENDING'::text;
 END IF;
 RETURN blockers;
END $$;

-- The operator login still has no table grant: it reads requests and their latest outcome here.
CREATE FUNCTION lab.operator_list_flattens(include_completed boolean DEFAULT false)
RETURNS TABLE(request_seq bigint, request_id uuid, reason text, operator_role text,
 requested_at timestamptz, pending boolean, completion_rows bigint, completed_at timestamptz,
 last_outcome text, last_residual_codes text[], last_finished_at timestamptz,
 last_runtime_id uuid, cancels_attempted integer, cancels_acknowledged integer,
 closes_attempted integer, closes_acknowledged integer)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
 SELECT r.event_seq,r.request_id,r.reason,r.operator_role,r.requested_at,d.completed_at IS NULL,
  (SELECT count(*) FROM lab.operator_flatten_completions c WHERE c.request_seq=r.event_seq),
  d.completed_at,l.outcome,l.residual_codes,l.finished_at,l.runtime_id,l.cancels_attempted,
  l.cancels_acknowledged,l.closes_attempted,l.closes_acknowledged
 FROM lab.operator_flatten_requests r
 LEFT JOIN LATERAL(SELECT c.finished_at AS completed_at FROM lab.operator_flatten_completions c
   WHERE c.request_seq=r.event_seq AND c.outcome='COMPLETED') d ON true
 LEFT JOIN LATERAL(SELECT * FROM lab.operator_flatten_completions c
   WHERE c.request_seq=r.event_seq ORDER BY c.event_seq DESC LIMIT 1) l ON true
 WHERE include_completed OR d.completed_at IS NULL ORDER BY r.event_seq
$$;

REVOKE ALL ON lab.operator_flatten_completions,lab.pending_operator_flatten_requests FROM PUBLIC;
-- catalyst_risk inherits catalyst_app's SELECT (migration 005), which already covers the 015
-- request table; the running app's risk role alone may insert a completion.
GRANT SELECT ON lab.operator_flatten_completions,lab.pending_operator_flatten_requests
 TO catalyst_app;
GRANT INSERT ON lab.operator_flatten_completions TO catalyst_risk;
REVOKE ALL ON FUNCTION lab.guard_flatten_completion(),
 lab.halt_release_blockers(bigint,text,bigint),lab.operator_list_flattens(boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.operator_list_flattens(boolean) TO catalyst_operator;
-- A flatten completes only once no authorization claim of either engine is unresolved; the
-- tick asks the same 015 question the halt release asks. It reads tables the role reads anyway.
GRANT EXECUTE ON FUNCTION lab.unresolved_authorization_claims() TO catalyst_risk;

-- Read-only grants for the acceptance-evidence tool (scripts/managed_acceptance_evidence.py),
-- which runs as catalyst_review and until now reported its halts section (and with it the
-- ZERO_RESIDUAL_NO_HALTS check) absent and could verify the audit chain only from a separately
-- exported file. SELECT only: catalyst_review still cannot write either relation, and the view
-- exposes unreleased halt records only.
GRANT SELECT ON lab.execution_halts TO catalyst_review;
GRANT SELECT ON lab.trade_events TO catalyst_review;

INSERT INTO lab.schema_migrations(version) VALUES(19);
