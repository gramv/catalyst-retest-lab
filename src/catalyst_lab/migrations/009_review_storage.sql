-- Step 4 only. No state transitions, risk grants, intent consumer or broker permissions.
CREATE ROLE catalyst_review LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
GRANT USAGE ON SCHEMA lab TO catalyst_review;

CREATE TABLE lab.review_contexts (
 context_hash text PRIMARY KEY CHECK(length(context_hash)=64),
 candidate_id uuid NOT NULL REFERENCES lab.candidates,
 revision integer NOT NULL CHECK(revision>0),
 strategy_version text NOT NULL CHECK(strategy_version='CATALYST_RETEST_V1'),
 cohort text NOT NULL CHECK(cohort IN ('JEV_ACTIVE_V1','JEV_ENGINEERING_TEST')),
 record_purpose text NOT NULL CHECK(record_purpose IN ('RESEARCH_REVIEW','ENGINEERING_TEST')),
 context_json jsonb NOT NULL,
 deadline timestamptz NOT NULL,
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq),
 UNIQUE(candidate_id,revision),
 CHECK(context_hash=encode(public.digest(context_json::text,'sha256'),'hex'))
);
CREATE TABLE lab.evidence_bundles (
 bundle_hash text PRIMARY KEY CHECK(length(bundle_hash)=64),
 candidate_id uuid NOT NULL REFERENCES lab.candidates,
 revision integer NOT NULL CHECK(revision>0),
 context_hash text NOT NULL REFERENCES lab.review_contexts,
 content_hash text NOT NULL CHECK(length(content_hash)=64),
 strategy_version text NOT NULL CHECK(strategy_version='CATALYST_RETEST_V1'),
 cohort text NOT NULL CHECK(cohort IN ('JEV_ACTIVE_V1','JEV_ENGINEERING_TEST')),
 record_purpose text NOT NULL CHECK(record_purpose IN ('RESEARCH_REVIEW','ENGINEERING_TEST')),
 record_json jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq),
 UNIQUE(candidate_id,revision), UNIQUE(candidate_id,content_hash),
 CHECK(bundle_hash=encode(public.digest(record_json::text,'sha256'),'hex'))
);
CREATE TABLE lab.entry_intents (
 intent_id uuid PRIMARY KEY,
 evidence_bundle_hash text NOT NULL REFERENCES lab.evidence_bundles,
 context_hash text NOT NULL REFERENCES lab.review_contexts,
 ai_decision_id uuid NOT NULL REFERENCES lab.ai_decisions(decision_id),
 strategy_version text NOT NULL CHECK(strategy_version='CATALYST_RETEST_V1'),
 cohort text NOT NULL CHECK(cohort IN ('JEV_ACTIVE_V1','JEV_ENGINEERING_TEST')),
 record_purpose text NOT NULL CHECK(record_purpose IN ('RESEARCH_REVIEW','ENGINEERING_TEST')),
 requested_action text NOT NULL CHECK(requested_action='ARM_ON_TRIGGER'),
 storage_status text NOT NULL CHECK(storage_status='STORED_ONLY_NOT_AUTHORIZED'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);
DO $$ DECLARE name text; BEGIN
 FOREACH name IN ARRAY ARRAY['review_contexts','evidence_bundles','entry_intents'] LOOP
  EXECUTE format('CREATE TRIGGER audit_jev BEFORE INSERT ON lab.%I
    FOR EACH ROW EXECUTE FUNCTION lab.audit_jev_row()',name);
  EXECUTE format('CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON lab.%I
    FOR EACH ROW EXECUTE FUNCTION lab.reject_mutation()',name);
  EXECUTE format('CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON lab.%I
    FOR EACH STATEMENT EXECUTE FUNCTION lab.reject_mutation()',name);
 END LOOP;
END $$;

CREATE FUNCTION lab.store_review_evidence(
 candidate uuid, requested_revision integer, sources jsonb, policy_id text,
 evidence_age_seconds integer, context_age_seconds integer
) RETURNS TABLE(bundle_hash text,context_hash text,revision integer,cohort text,deadline timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE c lab.candidates; v lab.validation_decisions; state_seq bigint;
 stamp timestamptz; due timestamptz; next_revision integer; purpose text; population text;
 context jsonb; body jsonb; ctx_hash text; body_hash text; content_hash text; source jsonb;
BEGIN
 PERFORM pg_advisory_xact_lock(719172026);
 stamp:=clock_timestamp();
 IF policy_id IS DISTINCT FROM 'MUSE_JEV_ACTIVE_V1'
 OR evidence_age_seconds IS DISTINCT FROM 60 OR context_age_seconds IS DISTINCT FROM 60 THEN
   RAISE EXCEPTION 'REVIEW_CONTEXT_POLICY_MISMATCH';
 END IF;
 SELECT * INTO c FROM lab.candidates WHERE candidate_id=candidate;
 SELECT * INTO v FROM lab.validation_decisions WHERE candidate_id=candidate
 ORDER BY decided_at DESC LIMIT 1;
 SELECT last_event_seq INTO state_seq FROM lab.candidate_states WHERE candidate_id=candidate;
 IF c.candidate_id IS NULL OR c.strategy_version IS DISTINCT FROM 'CATALYST_RETEST_V1'
 OR v.expires_at IS NULL OR v.evidence_json->>'official_close' IS NULL OR state_seq IS NULL THEN
   RAISE EXCEPTION 'SERVER_CONTEXT_UNAVAILABLE';
 END IF;
 due:=least(stamp+make_interval(secs=>evidence_age_seconds),
   stamp+make_interval(secs=>context_age_seconds),v.expires_at,
   (v.evidence_json->>'official_close')::timestamptz-interval '5 minutes');
 IF due<=stamp THEN RAISE EXCEPTION 'SERVER_CONTEXT_EXPIRED'; END IF;
 IF jsonb_typeof(sources) IS DISTINCT FROM 'array'
 OR jsonb_array_length(sources) NOT BETWEEN 1 AND 8 OR octet_length(sources::text)>12000 THEN
   RAISE EXCEPTION 'INVALID_SOURCES';
 END IF;
 FOR source IN SELECT value FROM jsonb_array_elements(sources) LOOP
  IF jsonb_typeof(source) IS DISTINCT FROM 'object'
   OR NOT source ?& ARRAY['source_id','url','excerpt','excerpt_hash','retrieved_at','published_at']
   OR source - ARRAY['source_id','url','excerpt','excerpt_hash','retrieved_at','published_at'] <> '{}'
   OR jsonb_typeof(source->'source_id') IS DISTINCT FROM 'string'
   OR jsonb_typeof(source->'url') IS DISTINCT FROM 'string'
   OR jsonb_typeof(source->'excerpt') IS DISTINCT FROM 'string'
   OR jsonb_typeof(source->'retrieved_at') IS DISTINCT FROM 'string'
   OR jsonb_typeof(source->'published_at') NOT IN ('string','null')
   OR (source->>'retrieved_at') !~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$'
   OR (source->>'published_at') !~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$'
   OR length(source->>'excerpt') NOT BETWEEN 1 AND 1200
   OR btrim(source->>'excerpt') = ''
   OR source->>'excerpt_hash' IS DISTINCT FROM encode(public.digest(source->>'excerpt','sha256'),'hex')
   OR (source->>'retrieved_at')::timestamptz > stamp
   OR (source->>'retrieved_at')::timestamptz < stamp-make_interval(secs=>evidence_age_seconds)
   OR (source->>'published_at')::timestamptz > (source->>'retrieved_at')::timestamptz THEN
    RAISE EXCEPTION 'INVALID_SOURCE_OR_TIMESTAMP';
  END IF;
  due:=least(due,(source->>'retrieved_at')::timestamptz
                  +make_interval(secs=>evidence_age_seconds));
 END LOOP;
 SELECT coalesce(max(b.revision),0)+1 INTO next_revision FROM lab.evidence_bundles b
 WHERE b.candidate_id=candidate;
 IF requested_revision IS DISTINCT FROM next_revision THEN
   RAISE EXCEPTION 'REVISION_CONFLICT';
 END IF;
 purpose:=CASE WHEN c.record_purpose='ENGINEERING_TEST' THEN 'ENGINEERING_TEST'
   ELSE 'RESEARCH_REVIEW' END;
 population:=CASE WHEN purpose='ENGINEERING_TEST' THEN 'JEV_ENGINEERING_TEST'
   ELSE 'JEV_ACTIVE_V1' END;
 context:=jsonb_build_object('candidate_id',candidate,'candidate_revision',1,
   'candidate_state_event_seq',state_seq,'evidence_revision',next_revision,
   'strategy_version',c.strategy_version,'ticker',c.ticker,'record_purpose',purpose,
   'cohort',population,'research_policy_id',policy_id,'captured_at',stamp,'deadline',due,
   'candidate_expiry',v.expires_at,
   'calendar_flatten_at',(v.evidence_json->>'official_close')::timestamptz-interval '5 minutes');
 ctx_hash:=encode(public.digest(context::text,'sha256'),'hex');
 body:=jsonb_build_object('context',context,'sources',sources,'market_state',
   jsonb_build_object('ticker',c.ticker,'catalyst',c.payload_json->>'catalyst',
    'thesis',c.payload_json->>'thesis','disproof',c.payload_json->>'disproof'));
 SELECT encode(public.digest(jsonb_build_object('market_state',body->'market_state',
   'sources',jsonb_agg(value-'retrieved_at' ORDER BY value->>'source_id'))::text,'sha256'),'hex')
 INTO content_hash FROM jsonb_array_elements(sources);
 body_hash:=encode(public.digest(body::text,'sha256'),'hex');
 INSERT INTO lab.review_contexts(context_hash,candidate_id,revision,strategy_version,
   cohort,record_purpose,context_json,deadline)
 VALUES(ctx_hash,candidate,next_revision,c.strategy_version,population,purpose,context,due);
 INSERT INTO lab.evidence_bundles(bundle_hash,candidate_id,revision,context_hash,content_hash,
   strategy_version,cohort,record_purpose,record_json)
 VALUES(body_hash,candidate,next_revision,ctx_hash,content_hash,c.strategy_version,
   population,purpose,body);
 RETURN QUERY SELECT body_hash,ctx_hash,next_revision,population,due;
END $$;
REVOKE ALL ON FUNCTION lab.store_review_evidence(uuid,integer,jsonb,text,integer,integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.store_review_evidence(uuid,integer,jsonb,text,integer,integer)
 TO catalyst_review;

-- Bind future adapter requests to exact persisted evidence, without interpreting their answers.
CREATE FUNCTION lab.bind_review_request() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE b lab.evidence_bundles; ctx lab.review_contexts; state jsonb;
BEGIN
 IF NEW.evidence_identity ? 'evidence_bundle_hash' THEN
  SELECT * INTO b FROM lab.evidence_bundles
    WHERE bundle_hash=NEW.evidence_identity->>'evidence_bundle_hash';
  SELECT * INTO ctx FROM lab.review_contexts WHERE context_hash=b.context_hash;
  SELECT (b.record_json->'market_state') || jsonb_build_object('sources',jsonb_agg(
    jsonb_build_object('source_id',value->>'source_id','excerpt',value->>'excerpt')))
  INTO state FROM jsonb_array_elements(b.record_json->'sources');
  IF b.bundle_hash IS NULL
   OR NEW.evidence_identity->>'context_hash' IS DISTINCT FROM b.context_hash
   OR NEW.evidence_identity->>'strategy_version' IS DISTINCT FROM b.strategy_version
   OR NEW.evidence_identity->>'cohort' IS DISTINCT FROM b.cohort
   OR NEW.evidence_identity->>'candidate_id' IS DISTINCT FROM b.candidate_id::text
   OR NEW.evidence_identity->>'evidence_revision' IS DISTINCT FROM b.revision::text
   OR NEW.record_purpose IS DISTINCT FROM b.record_purpose
   OR NEW.evidence_identity->>'research_policy_id' IS DISTINCT FROM ctx.context_json->>'research_policy_id'
   OR NEW.deadline>ctx.deadline
   OR NEW.request_json::jsonb->'state' IS DISTINCT FROM state THEN
    RAISE EXCEPTION 'REVIEW_EVIDENCE_BINDING_MISMATCH';
  END IF;
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION lab.bind_review_request() FROM PUBLIC;
CREATE TRIGGER bind_review BEFORE INSERT ON lab.jev_requests
 FOR EACH ROW EXECUTE FUNCTION lab.bind_review_request();

CREATE FUNCTION lab.store_entry_intent(intent uuid,bundle text,decision uuid,strategy text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE b lab.evidence_bundles; identity jsonb;
BEGIN
 SELECT * INTO b FROM lab.evidence_bundles WHERE bundle_hash=bundle;
 SELECT q.evidence_identity INTO identity FROM lab.ai_decisions d
 JOIN lab.jev_receipts r USING(receipt_id) JOIN lab.jev_requests q USING(request_id)
 WHERE d.decision_id=decision AND r.outcome='VALID';
 IF strategy IS DISTINCT FROM 'CATALYST_RETEST_V1' OR b.bundle_hash IS NULL
 OR identity->>'evidence_bundle_hash' IS DISTINCT FROM bundle
 OR identity->>'context_hash' IS DISTINCT FROM b.context_hash
 OR identity->>'strategy_version' IS DISTINCT FROM strategy
 OR identity->>'candidate_id' IS DISTINCT FROM b.candidate_id::text
 OR identity->>'cohort' IS DISTINCT FROM b.cohort
 OR identity->>'evidence_revision' IS DISTINCT FROM b.revision::text THEN
   RAISE EXCEPTION 'INTENT_BINDING_MISMATCH';
 END IF;
 -- Deliberately no APPROVE test, no risk check, no state transition, no consumption.
 INSERT INTO lab.entry_intents(intent_id,evidence_bundle_hash,context_hash,ai_decision_id,
   strategy_version,cohort,record_purpose,requested_action,storage_status)
 VALUES(intent,bundle,b.context_hash,decision,strategy,b.cohort,b.record_purpose,
   'ARM_ON_TRIGGER','STORED_ONLY_NOT_AUTHORIZED');
END $$;
REVOKE ALL ON FUNCTION lab.store_entry_intent(uuid,text,uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.store_entry_intent(uuid,text,uuid,text) TO catalyst_review;
REVOKE ALL ON lab.review_contexts,lab.evidence_bundles,lab.entry_intents FROM PUBLIC;
GRANT SELECT ON lab.review_contexts,lab.evidence_bundles TO catalyst_review,catalyst_jev;
-- No role used by an application may SELECT entry_intents. Only narrow insert function.

CREATE VIEW lab.review_observations AS
 SELECT d.decision_id,d.receipt_id,d.question,d.answer_json,d.probability,d.decision_confidence,
 q.strategy_version,q.cohort,q.record_purpose,q.bundle_hash,q.context_hash,q.candidate_id,
 q.revision AS evidence_revision,r.actual_model AS model,r.latency_ms,r.completed_at,
 r.outcome,d.event_seq
 FROM lab.ai_decisions d JOIN lab.jev_receipts r USING(receipt_id)
 JOIN lab.jev_requests req USING(request_id)
 JOIN lab.evidence_bundles q ON q.bundle_hash=req.evidence_identity->>'evidence_bundle_hash';
GRANT SELECT ON lab.review_observations TO catalyst_review;
-- Existing unbound receipts stay historically attributed; they are not active strategy runs.
CREATE VIEW lab.jev_cohort_decisions AS
 SELECT d.*,CASE WHEN q.record_purpose='ENGINEERING_TEST' THEN 'JEV_ENGINEERING_TEST'
   WHEN b.bundle_hash IS NOT NULL THEN b.cohort ELSE 'JEV_REVIEW_UNASSIGNED' END AS cohort,
 q.record_purpose,'CATALYST_RETEST_V1'::text AS strategy_version
 FROM lab.jev_decisions d JOIN lab.jev_receipts r USING(receipt_id)
 JOIN lab.jev_requests q USING(request_id)
 LEFT JOIN lab.evidence_bundles b ON b.bundle_hash=q.evidence_identity->>'evidence_bundle_hash';
GRANT SELECT ON lab.jev_cohort_decisions TO catalyst_jev;
INSERT INTO lab.schema_migrations(version) VALUES(9);
