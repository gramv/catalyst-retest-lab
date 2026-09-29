-- Trusted review writer has no broker, risk, candidate, or general event-write privileges.
CREATE ROLE catalyst_jev LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
GRANT USAGE ON SCHEMA lab TO catalyst_jev;
CREATE TABLE lab.jev_requests (
 request_id uuid PRIMARY KEY,
 evidence_identity jsonb NOT NULL CHECK(jsonb_typeof(evidence_identity)='object'),
 stage text NOT NULL CHECK(stage IN ('TRIAGE','SKEPTIC','ROUTING','TRACKING')),
 question_set_version text NOT NULL,
 model text NOT NULL CHECK(model='jev-1.13.0'),
 input_hash text NOT NULL CHECK(length(input_hash)=64),
 template_hash text NOT NULL CHECK(length(template_hash)=64),
 request_json text NOT NULL CHECK(jsonb_typeof(request_json::jsonb)='object'),
 request_hash text GENERATED ALWAYS AS
   (encode(public.digest(request_json,'sha256'),'hex')) STORED,
 deadline timestamptz NOT NULL,
 record_purpose text NOT NULL CHECK(record_purpose IN ('ENGINEERING_TEST','RESEARCH_REVIEW')),
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(input_hash,template_hash,evidence_identity),
 CHECK(request_json::jsonb->>'model'=model)
);
CREATE TABLE lab.jev_receipts (
 receipt_id uuid PRIMARY KEY,
 request_id uuid NOT NULL REFERENCES lab.jev_requests,
 attempt integer NOT NULL CHECK(attempt>=0),
 outcome text NOT NULL CHECK(outcome IN ('VALID','HTTP_ERROR','TRANSPORT_FAILURE',
   'INVALID_RESPONSE','EXPIRED','CIRCUIT_OPEN','CREDENTIAL_UNAVAILABLE')),
 http_status integer CHECK(http_status BETWEEN 100 AND 599),
 response_bytes bytea,
 response_hash text GENERATED ALWAYS AS
   (encode(public.digest(response_bytes,'sha256'),'hex')) STORED,
 actual_model text,
 started_at timestamptz NOT NULL,
 completed_at timestamptz NOT NULL CHECK(completed_at>=started_at),
 latency_ms numeric NOT NULL CHECK(latency_ms>=0),
 error_code text,
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq),
 UNIQUE(request_id,attempt),
 CHECK(outcome<>'VALID' OR (http_status=200 AND actual_model='jev-1.13.0'
   AND response_bytes IS NOT NULL AND error_code IS NULL))
);
CREATE TABLE lab.ai_decisions (
 decision_id uuid PRIMARY KEY,
 receipt_id uuid NOT NULL REFERENCES lab.jev_receipts,
 question text NOT NULL,
 answer_json jsonb NOT NULL,
 probability numeric CHECK(probability BETWEEN 0 AND 1),
 decision_confidence numeric CHECK(decision_confidence BETWEEN 0 AND 1),
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq),
 UNIQUE(receipt_id,question)
);
CREATE VIEW lab.jev_decisions AS
 SELECT d.*,q.evidence_identity,q.model,q.stage,q.input_hash,q.question_set_version,
   r.latency_ms,r.completed_at
 FROM lab.ai_decisions d JOIN lab.jev_receipts r USING(receipt_id)
 JOIN lab.jev_requests q USING(request_id);
CREATE TABLE lab.jev_control_events (
 control_id uuid PRIMARY KEY,
 request_id uuid NOT NULL REFERENCES lab.jev_requests,
 reason text NOT NULL CHECK(reason IN ('DUPLICATE_REVIEW','REQUEST_ID_CONFLICT')),
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE FUNCTION lab.audit_jev_row() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE recorded_seq bigint; receipt lab.jev_receipts; req lab.jev_requests; answer jsonb;
BEGIN
 IF TG_TABLE_NAME='ai_decisions' THEN
  SELECT * INTO receipt FROM lab.jev_receipts WHERE receipt_id=NEW.receipt_id;
  IF receipt.outcome IS DISTINCT FROM 'VALID' THEN
   RAISE EXCEPTION 'Only a valid receipt can supply judgments';
  END IF;
  answer := convert_from(receipt.response_bytes,'UTF8')::jsonb->'answers'->NEW.question;
  IF answer IS NULL OR answer IS DISTINCT FROM NEW.answer_json
   OR NEW.decision_confidence IS DISTINCT FROM (answer->>'confidence')::numeric
   OR NEW.probability IS DISTINCT FROM (CASE answer->>'type'
     WHEN 'choice' THEN (answer->'probabilities'->>(answer->>'choice'))::numeric
     WHEN 'noul' THEN (answer->>'noul')::numeric ELSE NULL END) THEN
   RAISE EXCEPTION 'Judgment must match the unmodified provider receipt';
  END IF;
 ELSIF TG_TABLE_NAME='jev_receipts' THEN
 IF NEW.outcome='VALID' THEN
  SELECT * INTO req FROM lab.jev_requests WHERE request_id=NEW.request_id;
  IF convert_from(NEW.response_bytes,'UTF8')::jsonb->>'model' IS DISTINCT FROM req.model
    OR NEW.completed_at>=req.deadline THEN
   RAISE EXCEPTION 'Valid receipt requires pinned model and unexpired request';
  END IF;
 END IF;
 END IF;
 INSERT INTO lab.trade_events(strategy_version,event_type,payload_json)
 VALUES('CATALYST_RETEST_V1','SYSTEM_EVENT',jsonb_build_object(
   'kind',upper(TG_TABLE_NAME),'row',to_jsonb(NEW)-'event_seq'-'request_hash'-'response_hash'))
 RETURNING seq INTO recorded_seq;
 NEW.event_seq:=recorded_seq;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION lab.audit_jev_row() FROM PUBLIC;
DO $$ DECLARE name text; BEGIN
 FOREACH name IN ARRAY ARRAY['jev_requests','jev_receipts','ai_decisions','jev_control_events'] LOOP
  EXECUTE format('CREATE TRIGGER audit_jev BEFORE INSERT ON lab.%I
    FOR EACH ROW EXECUTE FUNCTION lab.audit_jev_row()',name);
  EXECUTE format('CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON lab.%I
    FOR EACH ROW EXECUTE FUNCTION lab.reject_mutation()',name);
  EXECUTE format('CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON lab.%I
    FOR EACH STATEMENT EXECUTE FUNCTION lab.reject_mutation()',name);
 END LOOP;
END $$;
REVOKE ALL ON lab.jev_requests,lab.jev_receipts,lab.ai_decisions,lab.jev_control_events FROM PUBLIC;
GRANT SELECT,INSERT ON lab.jev_requests,lab.jev_receipts,lab.ai_decisions,lab.jev_control_events
 TO catalyst_jev;
GRANT SELECT ON lab.jev_decisions TO catalyst_jev;
GRANT SELECT ON lab.jev_requests,lab.jev_receipts,lab.ai_decisions,lab.jev_control_events,
 lab.jev_decisions TO catalyst_app,catalyst_risk;
INSERT INTO lab.schema_migrations(version) VALUES(8);
CREATE VIEW lab.jev_audit_events AS
 SELECT seq,event_body,event_hash,previous_hash,payload_json FROM lab.trade_events
 WHERE event_type='SYSTEM_EVENT'
   AND payload_json->>'kind' IN ('JEV_REQUESTS','JEV_RECEIPTS','AI_DECISIONS','JEV_CONTROL_EVENTS');
GRANT SELECT ON lab.jev_audit_events TO catalyst_jev,catalyst_app;
