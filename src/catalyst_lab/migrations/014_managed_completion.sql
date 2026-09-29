-- Preserve historical review semantics; strengthen newly selected research and hot reads.
CREATE INDEX managed_events_setup_kind_seq ON lab.managed_events(setup_id,kind,event_seq DESC);
CREATE INDEX managed_events_cycle_seq ON lab.managed_events((body->>'cycle_id'),event_seq);
CREATE INDEX managed_decisions_setup_method ON lab.managed_risk_decisions(setup_id,method,outcome,event_seq DESC);
CREATE INDEX managed_fills_setup_time ON lab.managed_fills(setup_id,filled_at,event_seq);

-- Comparative receipts have three score answers, unlike four-answer SKEPTIC receipts.
CREATE FUNCTION lab.managed_quality_receipt_intact(receipt uuid) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE r lab.jev_receipts; q lab.jev_requests; d lab.ai_decisions; answers jsonb;
BEGIN
 SELECT * INTO r FROM lab.jev_receipts WHERE receipt_id=receipt;
 SELECT * INTO q FROM lab.jev_requests WHERE request_id=r.request_id;
 IF r.receipt_id IS NULL OR r.outcome<>'VALID' OR q.request_id IS NULL
 OR NOT lab.research_audit_matches('JEV_REQUESTS',to_jsonb(q),q.event_seq)
 OR NOT lab.research_audit_matches('JEV_RECEIPTS',to_jsonb(r),r.event_seq)
 THEN RETURN false; END IF;
 answers:=convert_from(r.response_bytes,'UTF8')::jsonb->'answers';
 IF jsonb_typeof(answers) IS DISTINCT FROM 'object'
 OR NOT answers ?& ARRAY['evidence_quality','catalyst_specificity','disproof_quality']
 OR answers-ARRAY['evidence_quality','catalyst_specificity','disproof_quality']<>'{}'
 OR (SELECT count(*) FROM lab.ai_decisions WHERE receipt_id=receipt)<>3
 THEN RETURN false; END IF;
 FOR d IN SELECT * FROM lab.ai_decisions WHERE receipt_id=receipt LOOP
  IF NOT lab.research_audit_matches('AI_DECISIONS',to_jsonb(d),d.event_seq)
  OR d.answer_json IS DISTINCT FROM answers->d.question
  OR d.answer_json->>'type' IS DISTINCT FROM 'score'
  THEN RETURN false; END IF;
 END LOOP;
 RETURN true;
EXCEPTION WHEN OTHERS THEN RETURN false;
END $$;
REVOKE ALL ON FUNCTION lab.managed_quality_receipt_intact(uuid) FROM PUBLIC;

ALTER FUNCTION lab.managed_review_failure(jsonb) RENAME TO managed_review_failure_v13;
CREATE FUNCTION lab.managed_review_failure(packet jsonb) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE failure text; receipt lab.jev_receipts; request lab.jev_requests;
        latest lab.managed_events; quality lab.managed_events; answers jsonb;
        score numeric:=0; field text;
BEGIN
 failure:=lab.managed_review_failure_v13(packet);
 IF failure IS NOT NULL THEN RETURN failure; END IF;
 SELECT * INTO receipt FROM lab.jev_receipts WHERE receipt_id=(packet->>'receipt_id')::uuid;
 SELECT * INTO request FROM lab.jev_requests WHERE request_id=receipt.request_id;
 IF request.stage IS DISTINCT FROM 'SKEPTIC'
 OR request.question_set_version IS DISTINCT FROM 'SKEPTIC_QUESTIONS_V1'
 OR request.template_hash IS DISTINCT FROM 'abd65f517fb0ff324f70b07290326c780d81f16a0df11e3548221aff8b0b5cd5'
 THEN RETURN 'SELECTION_QUESTION_POLICY_MISMATCH'; END IF;
 IF packet->>'selection_policy' IS DISTINCT FROM 'MUSE_JEV_RESEARCH_SELECTION_V2'
 THEN RETURN NULL; END IF;
 IF packet->'quality_required' IS NULL OR jsonb_typeof(packet->'quality_required')<>'boolean'
 THEN RETURN 'QUALITY_POLICY_REQUIRED'; END IF;
 IF packet->'quality_required'='false'::jsonb THEN RETURN NULL; END IF;
 IF packet->>'quality_policy' IS DISTINCT FROM 'MUSE_JEV_COMPARATIVE_QUALITY_V1'
 OR packet->>'quality_receipt_id' IS NULL
 THEN RETURN 'QUALITY_RECEIPT_REQUIRED'; END IF;
 SELECT * INTO receipt FROM lab.jev_receipts
 WHERE receipt_id=(packet->>'quality_receipt_id')::uuid;
 SELECT * INTO request FROM lab.jev_requests WHERE request_id=receipt.request_id;
 SELECT * INTO latest FROM lab.managed_events WHERE kind='RESEARCH_PACKET'
 AND body->>'cycle_id'=packet->>'cycle_id' AND body->>'item_key'=packet->>'item_key'
 ORDER BY event_seq DESC LIMIT 1;
 SELECT * INTO quality FROM lab.managed_events WHERE kind='RESEARCH_QUALITY'
 AND body->>'cycle_id'=packet->>'cycle_id' AND body->>'item_key'=packet->>'item_key'
 AND body->>'revision'=packet->>'revision' ORDER BY event_seq DESC LIMIT 1;
 IF receipt.receipt_id IS NULL OR quality.event_id IS NULL
 OR NOT lab.managed_quality_receipt_intact(receipt.receipt_id)
 OR NOT lab.research_audit_matches('MANAGED_EVENTS',to_jsonb(quality),quality.event_seq)
 OR quality.body->>'status' IS DISTINCT FROM 'RANKED'
 OR quality.body->>'request_id' IS DISTINCT FROM request.request_id::text
 OR quality.body->'receipt_ids'->>-1 IS DISTINCT FROM receipt.receipt_id::text
 OR request.stage IS DISTINCT FROM 'TRIAGE'
 OR request.question_set_version IS DISTINCT FROM 'MUSE_JEV_COMPARATIVE_QUALITY_V1'
 OR request.template_hash IS DISTINCT FROM '4311ecc3e0533c8cec2f81e846a090a624f166d0c6f6eb433243acc2177ba8a8'
 OR receipt.completed_at>request.deadline
 OR request.evidence_identity->>'quality_policy' IS DISTINCT FROM 'MUSE_JEV_COMPARATIVE_QUALITY_V1'
 OR request.evidence_identity->>'selection_policy' IS DISTINCT FROM packet->>'selection_policy'
 OR request.evidence_identity->>'evidence_hash' IS DISTINCT FROM packet->>'evidence_hash'
 OR request.evidence_identity->>'candidate_revision' IS DISTINCT FROM packet->>'revision'
 OR request.evidence_identity->>'cycle_id' IS DISTINCT FROM packet->>'cycle_id'
 OR request.evidence_identity->>'research_item_key' IS DISTINCT FROM packet->>'item_key'
 OR request.request_json::jsonb->'state' IS DISTINCT FROM
    jsonb_build_object('candidate',latest.body->'state','muse_rank',latest.body->'rank')
 THEN RETURN 'QUALITY_RECEIPT_BINDING_FAILURE'; END IF;
 answers:=convert_from(receipt.response_bytes,'UTF8')::jsonb->'answers';
 FOREACH field IN ARRAY ARRAY['evidence_quality','catalyst_specificity','disproof_quality'] LOOP
   score:=score+coalesce((answers->field->'probabilities'->>'1')::numeric,0)
       +2*coalesce((answers->field->'probabilities'->>'2')::numeric,0);
 END LOOP;
 IF round(score,12) IS DISTINCT FROM round((packet->>'quality_score')::numeric,12)
 OR round(score,12) IS DISTINCT FROM round((quality.body->>'score')::numeric,12)
 OR quality.body->'answers' IS DISTINCT FROM answers
 THEN RETURN 'QUALITY_SCORE_MISMATCH'; END IF;
 RETURN NULL;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
 RETURN 'QUALITY_RECEIPT_BINDING_FAILURE';
END $$;
REVOKE ALL ON FUNCTION lab.managed_review_failure(jsonb),lab.managed_review_failure_v13(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION lab.managed_review_failure_v13(jsonb) FROM catalyst_risk;
GRANT EXECUTE ON FUNCTION lab.managed_review_failure(jsonb) TO catalyst_risk;
INSERT INTO lab.schema_migrations(version) VALUES(14);
