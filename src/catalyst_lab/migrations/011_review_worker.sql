-- App-owned review worker. No broker or risk privileges are introduced.
CREATE TABLE lab.review_runtime_scopes (
 scope_id text PRIMARY KEY CHECK(length(scope_id)=64), credential_slot text NOT NULL,
 model text NOT NULL CHECK(model='jev-1.13.0'), policy_json jsonb NOT NULL,
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);
CREATE TABLE lab.review_attempt_permits (
 permit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), scope_id text NOT NULL REFERENCES lab.review_runtime_scopes,
 request_id uuid NOT NULL REFERENCES lab.jev_requests, attempt integer NOT NULL CHECK(attempt BETWEEN 1 AND 3),
 epoch integer NOT NULL, probe boolean NOT NULL, expires_at timestamptz NOT NULL,
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq), UNIQUE(request_id,attempt)
);
CREATE TABLE lab.review_breaker_events (
 breaker_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 scope_id text NOT NULL REFERENCES lab.review_runtime_scopes, state_json jsonb NOT NULL,
 reason text NOT NULL, receipt_id uuid UNIQUE REFERENCES lab.jev_receipts,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);
CREATE TABLE lab.review_worker_events (
 worker_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), worker_id uuid NOT NULL,
 scope_id text NOT NULL REFERENCES lab.review_runtime_scopes,
 status text NOT NULL CHECK(status IN ('RUNNING','CLOCK_UNHEALTHY','STOPPED','FAILED')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);
CREATE TABLE lab.review_job_events (
 job_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), worker_id uuid NOT NULL,
 item_id uuid NOT NULL REFERENCES lab.research_report_items,
 kind text NOT NULL CHECK(kind IN ('CLAIMED','DELIVERED','FAILED')),
 lease_until timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);
CREATE TABLE lab.review_operator_events (
 control_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 halted boolean NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);
CREATE ROLE catalyst_review_operator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
GRANT USAGE ON SCHEMA lab TO catalyst_review_operator;
DO $$ DECLARE name text; BEGIN
 FOREACH name IN ARRAY ARRAY['review_runtime_scopes','review_attempt_permits',
 'review_breaker_events','review_worker_events','review_job_events','review_operator_events'] LOOP
  EXECUTE format('CREATE TRIGGER audit_jev BEFORE INSERT ON lab.%I FOR EACH ROW EXECUTE FUNCTION lab.audit_jev_row()',name);
  EXECUTE format('CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON lab.%I FOR EACH ROW EXECUTE FUNCTION lab.reject_mutation()',name);
  EXECUTE format('CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON lab.%I FOR EACH STATEMENT EXECUTE FUNCTION lab.reject_mutation()',name);
  EXECUTE format('REVOKE ALL ON lab.%I FROM PUBLIC',name);
  EXECUTE format('GRANT SELECT ON lab.%I TO catalyst_jev',name);
 END LOOP;
END $$;

CREATE FUNCTION lab.register_review_scope(slot text, policy jsonb) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE sid text; BEGIN
 IF slot !~ '^[a-z][a-z0-9-]{0,47}$' OR slot IS NULL OR policy IS DISTINCT FROM $policy${"backoff_base_ms":250,"backoff_cap_ms":2000,"backoff_multiplier":2,"batch_timeout_seconds":3,"breaker_cooldown_seconds":30,"breaker_failure_classes":["HTTP_ERROR","TRANSPORT_FAILURE","TIMEOUT","INVALID_RESPONSE","MODEL_MISMATCH","CREDENTIAL_ECHO"],"breaker_failure_threshold":3,"breaker_recovery_successes":2,"breaker_scope":"PROVIDER_MODEL_CREDENTIAL_SLOT","context_max_age_seconds":60,"duplicate_policy":"AUDIT_REJECT_SAME_REQUEST_RETRY","evidence_max_age_seconds":60,"expiry_grace_seconds":0,"half_open_max_attempts":1,"half_open_max_inflight":1,"heartbeat_deadline_seconds":15,"heartbeat_period_seconds":5,"jitter_lower_fraction":0.5,"jitter_mode":"EQUAL","material_supersession_policy":"MATERIAL_REVISION_ONLY","max_attempts":3,"max_clock_offset_ms":250,"overall_review_deadline_seconds":10,"question_timeout_seconds":3,"recovery_probe_policy":"SYNTHETIC_NON_AUTHORIZING","recovery_probe_spacing_seconds":1,"retry_after_policy":"RESPECT_MINIMUM_OR_ABANDON","retry_http_statuses":[429,529],"same_candidate_max_active_chains":1,"selection_policy":"EXACT_REVISION_FIRST_VALID_CONFLICT_NEEDS_REVIEW","version":"JEV_LIVE_REVIEW_POLICY_V1"}$policy$::jsonb THEN
  RAISE EXCEPTION 'EXPLICIT_APPROVED_WORKER_POLICY_REQUIRED'; END IF;
 sid:=encode(public.digest('TYPESAFE:jev-1.13.0:'||slot,'sha256'),'hex');
 PERFORM pg_advisory_xact_lock(719172026);
 IF NOT EXISTS(SELECT 1 FROM lab.review_runtime_scopes WHERE scope_id=sid) THEN
  INSERT INTO lab.review_runtime_scopes(scope_id,credential_slot,model,policy_json)
  VALUES(sid,slot,'jev-1.13.0',policy);
 END IF;
 RETURN sid;
END $$;
CREATE FUNCTION lab.review_halted() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,lab AS $$
 SELECT coalesce((SELECT halted FROM lab.review_operator_events ORDER BY event_seq DESC LIMIT 1),false)
$$;
CREATE FUNCTION lab.review_operator_halt(stop boolean) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab AS $$ BEGIN
 IF stop IS NULL THEN RAISE EXCEPTION 'HALT_VALUE_REQUIRED'; END IF;
 PERFORM pg_advisory_xact_lock(719172026);
 INSERT INTO lab.review_operator_events(halted) VALUES(stop);
END $$;
CREATE FUNCTION lab.review_heartbeat(worker uuid,sid text,health text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab AS $$ BEGIN
 INSERT INTO lab.review_worker_events(worker_id,scope_id,status) VALUES(worker,sid,health);
END $$;

CREATE FUNCTION lab.review_breaker_state(sid text) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
 SELECT coalesce((SELECT state_json FROM lab.review_breaker_events WHERE scope_id=sid
  ORDER BY event_seq DESC LIMIT 1),
  '{"state":"CLOSED","epoch":0,"failures":0,"successes":0,"blocked_until":null,"probe_id":null,"probe_until":null,"next_probe_at":null}'::jsonb)
$$;
CREATE FUNCTION lab.review_attempt_permit(sid text,request uuid,number integer,is_probe boolean)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE s jsonb; policy jsonb; req lab.jev_requests; stamp timestamptz:=clock_timestamp();
 pid uuid:=gen_random_uuid(); due timestamptz; BEGIN
 PERFORM pg_advisory_xact_lock(719172026);
 stamp:=clock_timestamp();
 SELECT policy_json INTO policy FROM lab.review_runtime_scopes WHERE scope_id=sid;
 SELECT * INTO req FROM lab.jev_requests WHERE request_id=request;
 IF policy IS NULL OR req.request_id IS NULL OR req.evidence_identity->>'runtime_scope' IS DISTINCT FROM sid
 OR number NOT BETWEEN 1 AND (policy->>'max_attempts')::integer OR is_probe IS NULL THEN
  RAISE EXCEPTION 'REVIEW_PERMIT_BINDING_MISMATCH'; END IF;
 IF is_probe AND (req.stage<>'ROUTING' OR req.record_purpose<>'ENGINEERING_TEST'
 OR req.question_set_version<>'PROVIDER_HEALTH_V1'
 OR req.template_hash<>'44cb122e84f2a9c3fceafe5fc748e4fc8632d009b1a94ea10bff198c69ec8210'
 OR req.request_json::jsonb->'questions' IS DISTINCT FROM $health${"diagnostic":{"criteria":{"Insufficient evidence":"The supplied evidence cannot support a conclusion.","NO":"It describes something else.","YES":"It describes a diagnostic."},"instructions":"Does the supplied statement describe a diagnostic?","type":"choice"}}$health$::jsonb
 OR req.evidence_identity->>'health_probe' IS DISTINCT FROM 'true'
 OR req.evidence_identity ? 'research_item_id'
 OR req.request_json::jsonb->'state' IS DISTINCT FROM '{"diagnostic":"SYNTHETIC_HEALTH_CHECK","statement":"The sample is a diagnostic."}'::jsonb
 OR number<>1) THEN RAISE EXCEPTION 'SYNTHETIC_HEALTH_PROBE_REQUIRED'; END IF;
 IF lab.review_halted() THEN RETURN jsonb_build_object('allowed',false,'reason','OPERATOR_HALTED'); END IF;
 IF NOT EXISTS(SELECT 1 FROM lab.review_worker_status WHERE
  worker_id=(req.evidence_identity->>'review_worker_id')::uuid AND scope_id=sid AND status='RUNNING') THEN
  RETURN jsonb_build_object('allowed',false,'reason','WORKER_HEARTBEAT_DOWN'); END IF;
 IF stamp>=req.deadline THEN RETURN jsonb_build_object('allowed',false,'reason','REVIEW_DEADLINE_EXCEEDED'); END IF;
 IF EXISTS(SELECT 1 FROM lab.review_attempt_permits WHERE request_id=request AND attempt=number) THEN
  RETURN jsonb_build_object('allowed',false,'reason','ATTEMPT_ALREADY_ISSUED'); END IF;
 s:=lab.review_breaker_state(sid);
 IF s->>'state'='HALF_OPEN' AND s->>'probe_id' IS NOT NULL
  AND stamp>=(s->>'probe_until')::timestamptz THEN
  s:=s||jsonb_build_object('state','OPEN','epoch',(s->>'epoch')::integer+1,'successes',0,
    'probe_id',null,'probe_until',null,'blocked_until',stamp+make_interval(secs=>(policy->>'breaker_cooldown_seconds')::integer));
  INSERT INTO lab.review_breaker_events(scope_id,state_json,reason) VALUES(sid,s,'PROBE_LEASE_EXPIRED');
 END IF;
 IF s->>'state'='CLOSED' AND is_probe THEN
  RETURN jsonb_build_object('allowed',false,'reason','HEALTH_PROBE_NOT_REQUIRED'); END IF;
 IF s->>'state'<>'CLOSED' THEN
  IF NOT is_probe OR stamp<coalesce((s->>'blocked_until')::timestamptz,stamp)
   OR s->>'probe_id' IS NOT NULL OR stamp<coalesce((s->>'next_probe_at')::timestamptz,stamp) THEN
   RETURN jsonb_build_object('allowed',false,'reason','CIRCUIT_OPEN'); END IF;
 END IF;
 due:=least(req.deadline,stamp+make_interval(secs=>(policy->>'batch_timeout_seconds')::integer));
 INSERT INTO lab.review_attempt_permits(permit_id,scope_id,request_id,attempt,epoch,probe,expires_at)
 VALUES(pid,sid,request,number,(s->>'epoch')::integer,is_probe,due);
 IF is_probe THEN
  s:=s||jsonb_build_object('state','HALF_OPEN','probe_id',pid,'probe_until',due);
  INSERT INTO lab.review_breaker_events(scope_id,state_json,reason) VALUES(sid,s,'PROBE_LEASED');
 END IF;
 RETURN jsonb_build_object('allowed',true,'permit_id',pid,'expires_at',due);
END $$;

CREATE FUNCTION lab.complete_review_attempt() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE p lab.review_attempt_permits; policy jsonb; s jsonb; stamp timestamptz:=clock_timestamp();
 failed boolean; n integer; BEGIN
 SELECT * INTO p FROM lab.review_attempt_permits WHERE request_id=NEW.request_id AND attempt=NEW.attempt;
 IF p.permit_id IS NULL OR NEW.outcome NOT IN ('VALID','HTTP_ERROR','TRANSPORT_FAILURE','INVALID_RESPONSE') THEN RETURN NEW; END IF;
 PERFORM pg_advisory_xact_lock(719172026);
 SELECT policy_json INTO policy FROM lab.review_runtime_scopes WHERE scope_id=p.scope_id;
 s:=lab.review_breaker_state(p.scope_id); failed:=NEW.outcome<>'VALID';
 -- Responses that were in flight when the circuit opened never close that generation.
 IF p.epoch<>(s->>'epoch')::integer THEN RETURN NEW; END IF;
 IF p.probe THEN
  IF s->>'probe_id' IS DISTINCT FROM p.permit_id::text THEN RETURN NEW; END IF;
  failed:=failed OR stamp>=p.expires_at;
  n:=CASE WHEN failed THEN 0 ELSE (s->>'successes')::integer+1 END;
  s:=s||jsonb_build_object('probe_id',null,'probe_until',null,'successes',n,
    'next_probe_at',stamp+make_interval(secs=>(policy->>'recovery_probe_spacing_seconds')::integer));
  IF NOT failed AND n>=(policy->>'breaker_recovery_successes')::integer THEN
   s:=s||jsonb_build_object('state','CLOSED','failures',0,'successes',0,'blocked_until',null);
  ELSIF failed THEN
   s:=s||jsonb_build_object('state','OPEN','epoch',(s->>'epoch')::integer+1,
     'blocked_until',stamp+make_interval(secs=>(policy->>'breaker_cooldown_seconds')::integer));
  END IF;
 ELSIF s->>'state'='CLOSED' THEN
  n:=CASE WHEN failed THEN (s->>'failures')::integer+1 ELSE 0 END;
  s:=s||jsonb_build_object('failures',n);
  IF n>=(policy->>'breaker_failure_threshold')::integer THEN
   s:=s||jsonb_build_object('state','OPEN','epoch',(s->>'epoch')::integer+1,'successes',0,
     'blocked_until',stamp+make_interval(secs=>(policy->>'breaker_cooldown_seconds')::integer));
  END IF;
 END IF;
 INSERT INTO lab.review_breaker_events(scope_id,state_json,reason,receipt_id)
 VALUES(p.scope_id,s,CASE WHEN failed THEN 'PROVIDER_FAILURE' ELSE 'PROVIDER_VALID' END,NEW.receipt_id);
 RETURN NEW;
END $$;
CREATE TRIGGER complete_review_attempt AFTER INSERT ON lab.jev_receipts
 FOR EACH ROW EXECUTE FUNCTION lab.complete_review_attempt();

CREATE FUNCTION lab.claim_review_job(worker uuid,sid text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE chosen lab.research_report_items; last_health lab.review_worker_events;
 stamp timestamptz:=clock_timestamp(); heartbeat_seconds integer; BEGIN
 PERFORM pg_advisory_xact_lock(719172026);
 stamp:=clock_timestamp();
 SELECT (policy_json->>'heartbeat_deadline_seconds')::integer INTO heartbeat_seconds
  FROM lab.review_runtime_scopes WHERE scope_id=sid;
 SELECT * INTO last_health FROM lab.review_worker_events WHERE worker_id=worker AND scope_id=sid
  ORDER BY event_seq DESC LIMIT 1;
 IF lab.review_halted() OR last_health.status IS DISTINCT FROM 'RUNNING'
 OR last_health.created_at<=stamp-make_interval(secs=>heartbeat_seconds) THEN RETURN NULL; END IF;
 SELECT i.* INTO chosen FROM lab.research_report_items i
 WHERE i.review_deadline>stamp AND NOT EXISTS(SELECT 1 FROM lab.research_outcomes o WHERE o.item_id=i.item_id)
 AND i.revision=(SELECT max(revision) FROM lab.research_reports WHERE report_id=i.report_id)
 AND NOT EXISTS(SELECT 1 FROM lab.review_job_events j
   JOIN lab.research_report_items other ON other.item_id=j.item_id
   WHERE other.symbol=i.symbol AND other.record_json->'context'->>'market'=i.record_json->'context'->>'market'
     AND other.record_json->'context'->>'timeframe'=i.record_json->'context'->>'timeframe'
     AND j.kind='CLAIMED' AND j.lease_until>stamp AND NOT EXISTS
      (SELECT 1 FROM lab.review_job_events done WHERE done.item_id=j.item_id AND done.event_seq>j.event_seq))
 ORDER BY i.review_deadline,i.event_seq LIMIT 1;
 IF chosen.item_id IS NULL THEN RETURN NULL; END IF;
 INSERT INTO lab.review_job_events(worker_id,item_id,kind,lease_until)
 VALUES(worker,chosen.item_id,'CLAIMED',chosen.review_deadline);
 RETURN chosen.item_id;
END $$;
CREATE FUNCTION lab.deliver_review_job(worker uuid,item uuid,failed boolean) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE result jsonb; j lab.review_job_events; BEGIN
 PERFORM pg_advisory_xact_lock(719172026);
 SELECT * INTO j FROM lab.review_job_events WHERE item_id=item ORDER BY event_seq DESC LIMIT 1;
 IF j.worker_id IS DISTINCT FROM worker OR j.kind<>'CLAIMED' THEN
  RAISE EXCEPTION 'REVIEW_JOB_LEASE_MISMATCH'; END IF;
 result:=lab.finalize_research_item(item);
 IF result->>'disposition'='PENDING_REVIEW' THEN RETURN result; END IF;
 INSERT INTO lab.review_job_events(worker_id,item_id,kind,lease_until)
 VALUES(worker,item,CASE WHEN failed THEN 'FAILED' ELSE 'DELIVERED' END,j.lease_until);
 RETURN result;
END $$;

CREATE VIEW lab.review_worker_status AS
 SELECT w.worker_id,w.scope_id,w.status AS last_status,w.created_at AS last_heartbeat,
 CASE WHEN lab.review_halted() THEN 'HALTED'
  WHEN w.status<>'RUNNING' THEN w.status
  WHEN clock_timestamp()>=w.created_at+make_interval(secs=>(s.policy_json->>'heartbeat_deadline_seconds')::integer)
  THEN 'DOWN' ELSE 'RUNNING' END AS status
 FROM (SELECT DISTINCT ON(worker_id) * FROM lab.review_worker_events ORDER BY worker_id,event_seq DESC) w
 JOIN lab.review_runtime_scopes s USING(scope_id);
CREATE VIEW lab.research_output_events AS
 SELECT o.event_seq,'RESEARCH_OUTCOME'::text AS event_kind,i.report_id,i.revision,i.item_id,
 i.symbol,o.disposition AS recorded_disposition,r.status,r.reason,o.receipt_id,o.decided_at,
 i.evidence_deadline AS expires_at,i.record_json->'context'->>'cohort' AS cohort,
 false AS authorizes_entry FROM lab.research_outcomes o
 JOIN lab.research_report_items i USING(item_id) JOIN lab.research_report_results r USING(item_id)
 UNION ALL
 SELECT r.event_seq,'REPORT_RECEIVED',r.report_id,r.revision,NULL::uuid,NULL::text,
 NULL::text,'REFRESH_REPORT',CASE WHEN r.revision>1 THEN 'MATERIAL_REVISION_RECEIVED'
 ELSE 'NEW_REPORT' END,NULL::uuid,r.created_at,r.valid_until,r.cohort,false
 FROM lab.research_reports r;
GRANT SELECT ON lab.review_worker_status,lab.research_output_events TO catalyst_review,catalyst_jev;
REVOKE ALL ON FUNCTION lab.register_review_scope(text,jsonb),lab.review_halted(),
 lab.review_operator_halt(boolean),lab.review_heartbeat(uuid,text,text),lab.review_breaker_state(text),
 lab.review_attempt_permit(text,uuid,integer,boolean),lab.complete_review_attempt(),
 lab.claim_review_job(uuid,text),lab.deliver_review_job(uuid,uuid,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.register_review_scope(text,jsonb),lab.review_halted(),
 lab.review_heartbeat(uuid,text,text),lab.review_breaker_state(text),
 lab.review_attempt_permit(text,uuid,integer,boolean),lab.claim_review_job(uuid,text),
 lab.deliver_review_job(uuid,uuid,boolean) TO catalyst_jev;
GRANT EXECUTE ON FUNCTION lab.review_halted() TO catalyst_review;
GRANT EXECUTE ON FUNCTION lab.review_operator_halt(boolean) TO catalyst_review_operator;
INSERT INTO lab.schema_migrations(version) VALUES(11);

ALTER TABLE lab.jev_control_events DROP CONSTRAINT jev_control_events_reason_check;
ALTER TABLE lab.jev_control_events ADD CONSTRAINT jev_control_events_reason_check
 CHECK(reason IN ('DUPLICATE_REVIEW','REQUEST_ID_CONFLICT','INVALID_RETRY_AFTER',
 'RETRY_AFTER_EXCEEDS_BUDGET','REVIEW_DEADLINE_EXCEEDED'));

-- A halt or missing worker heartbeat during the provider round-trip cannot become a new selection.
ALTER FUNCTION lab.finalize_research_item(uuid) RENAME TO finalize_research_item_recorded;
REVOKE ALL ON FUNCTION lab.finalize_research_item_recorded(uuid) FROM catalyst_jev;
CREATE FUNCTION lab.finalize_research_item(item uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE req lab.jev_requests; receipt uuid; why text; BEGIN
 PERFORM pg_advisory_xact_lock(719172026);
 SELECT * INTO req FROM lab.jev_requests WHERE evidence_identity->>'research_item_id'=item::text;
 IF req.evidence_identity ? 'runtime_scope' AND NOT EXISTS
  (SELECT 1 FROM lab.research_outcomes WHERE item_id=item) THEN
  IF lab.review_halted() THEN why:='OPERATOR_HALTED';
  ELSIF NOT EXISTS(SELECT 1 FROM lab.review_worker_status WHERE
    scope_id=req.evidence_identity->>'runtime_scope' AND status='RUNNING') THEN why:='WORKER_HEARTBEAT_DOWN';
  END IF;
  IF why IS NOT NULL THEN
   SELECT receipt_id INTO receipt FROM lab.jev_receipts WHERE request_id=req.request_id ORDER BY attempt DESC LIMIT 1;
   INSERT INTO lab.research_outcomes(item_id,request_id,receipt_id,policy_id,disposition,reason,answers_json)
    VALUES(item,req.request_id,receipt,'JEV_SKEPTIC_RESEARCH_TEST_V1','NEEDS_REVIEW',why,'{}');
  END IF;
 END IF;
 RETURN lab.finalize_research_item_recorded(item);
END $$;
REVOKE ALL ON FUNCTION lab.finalize_research_item(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.finalize_research_item(uuid) TO catalyst_jev;
