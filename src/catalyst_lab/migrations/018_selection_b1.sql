-- Selection rule B1 (MUSE_JEV_RESEARCH_SELECTION_B1_V1), plan 1.7 lean form. DDL only: no
-- audited row, table or column. Every non-B1 packet (V2 and every earlier policy) is judged by
-- the function this migration found, renamed managed_review_failure_before_b1 and delegated to
-- unchanged, so any branch an earlier migration added survives. A B1 packet is admissible only
-- when its cycle started after an intact owner activation, its three SKEPTIC components pass in
-- the stored response bytes (the verdict is recorded as dissent and never blocks) and its
-- QUALITY_V2 category is at or above the activated floor, in addition to every stored-binding
-- check of migration 013.

-- QUALITY_V2 receipts: V1's three score answers plus the quality_category Choice.
CREATE FUNCTION lab.managed_quality_v2_receipt_intact(receipt uuid) RETURNS boolean
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
 OR NOT answers ?& ARRAY['evidence_quality','catalyst_specificity','disproof_quality',
                         'quality_category']
 OR answers-ARRAY['evidence_quality','catalyst_specificity','disproof_quality',
                  'quality_category']<>'{}'
 OR (SELECT count(*) FROM lab.ai_decisions WHERE receipt_id=receipt)<>4
 THEN RETURN false; END IF;
 FOR d IN SELECT * FROM lab.ai_decisions WHERE receipt_id=receipt LOOP
  IF NOT lab.research_audit_matches('AI_DECISIONS',to_jsonb(d),d.event_seq)
  OR d.answer_json IS DISTINCT FROM answers->d.question
  OR d.answer_json->>'type' IS DISTINCT FROM
     (CASE WHEN d.question='quality_category' THEN 'choice' ELSE 'score' END)
  THEN RETURN false; END IF;
 END LOOP;
 RETURN true;
EXCEPTION WHEN OTHERS THEN RETURN false;
END $$;

-- The unique most probable label of one Choice answer; NULL when tied or malformed.
CREATE FUNCTION lab.managed_resolved_choice(answer jsonb) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT CASE WHEN answer->>'type'='choice'
   AND jsonb_typeof(answer->'probabilities')='object'
   AND (answer->'probabilities'->>(answer->>'choice'))::numeric=
       (SELECT max(v.value::numeric) FROM jsonb_each_text(answer->'probabilities') v)
   AND (SELECT count(*) FROM jsonb_each_text(answer->'probabilities') p
        WHERE p.value::numeric=(SELECT max(v.value::numeric)
          FROM jsonb_each_text(answer->'probabilities') v))=1
 THEN answer->>'choice' END
$$;

ALTER FUNCTION lab.managed_review_failure(jsonb) RENAME TO managed_review_failure_before_b1;
CREATE FUNCTION lab.managed_review_failure(packet jsonb) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE failure text; receipt lab.jev_receipts; request lab.jev_requests;
        latest lab.managed_events; started lab.managed_events; activation lab.managed_events;
        quality lab.managed_events; answers jsonb; category text; score numeric:=0;
        field text; floors text[]:=ARRAY['WEAK','ADEQUATE','STRONG'];
BEGIN
 -- Every other policy: the previous function, unchanged (migration 014's body for V2).
 IF packet->>'selection_policy' IS DISTINCT FROM 'MUSE_JEV_RESEARCH_SELECTION_B1_V1' THEN
  RETURN lab.managed_review_failure_before_b1(packet);
 END IF;
 -- Every stored-binding check of migration 013. Its final check is V2's answer
 -- conjunction (JEV_SELECTION_REQUIRED), which B1 replaces with the component rule below.
 failure:=lab.managed_review_failure_v13(packet);
 IF failure IS NOT NULL AND failure<>'JEV_SELECTION_REQUIRED' THEN RETURN failure; END IF;
 SELECT * INTO receipt FROM lab.jev_receipts WHERE receipt_id=(packet->>'receipt_id')::uuid;
 SELECT * INTO request FROM lab.jev_requests WHERE request_id=receipt.request_id;
 IF request.stage IS DISTINCT FROM 'SKEPTIC'
 OR request.question_set_version IS DISTINCT FROM 'SKEPTIC_QUESTIONS_V1'
 OR request.template_hash IS DISTINCT FROM 'abd65f517fb0ff324f70b07290326c780d81f16a0df11e3548221aff8b0b5cd5'
 THEN RETURN 'SELECTION_QUESTION_POLICY_MISMATCH'; END IF;
 SELECT * INTO latest FROM lab.managed_events WHERE kind='RESEARCH_PACKET'
 AND body->>'cycle_id'=packet->>'cycle_id' AND body->>'item_key'=packet->>'item_key'
 ORDER BY event_seq DESC LIMIT 1;
 SELECT * INTO started FROM lab.managed_events WHERE kind='RESEARCH_STARTED'
 AND body->>'cycle_id'=packet->>'cycle_id' ORDER BY event_seq LIMIT 1;
 SELECT * INTO activation FROM lab.managed_events WHERE event_seq=
  CASE WHEN started.body->'selection_rule'->>'activation_event_seq' ~ '^[0-9]{1,18}$'
  THEN (started.body->'selection_rule'->>'activation_event_seq')::bigint END;
 -- The owner's activation is recorded, intact and earlier than the cycle it governs; the
 -- cycle, its latest packet and this packet all name B1 and the activated floor.
 IF started.event_id IS NULL OR activation.event_id IS NULL
 OR NOT lab.research_audit_matches('MANAGED_EVENTS',to_jsonb(started),started.event_seq)
 OR NOT lab.research_audit_matches('MANAGED_EVENTS',to_jsonb(activation),activation.event_seq)
 OR activation.kind IS DISTINCT FROM 'RESEARCH_SELECTION_RULE_ACTIVATED'
 OR activation.event_seq>=started.event_seq
 OR activation.event_id::text IS DISTINCT FROM
    started.body->'selection_rule'->>'activation_event_id'
 OR activation.body->>'selection_policy' IS DISTINCT FROM 'MUSE_JEV_RESEARCH_SELECTION_B1_V1'
 OR activation.body->>'quality_policy' IS DISTINCT FROM 'MUSE_JEV_COMPARATIVE_QUALITY_V2'
 OR coalesce(activation.body->>'quality_floor','')<>ALL(floors)
 OR started.body->>'selection_policy' IS DISTINCT FROM 'MUSE_JEV_RESEARCH_SELECTION_B1_V1'
 OR started.body->'selection_rule'->>'selection_policy'
    IS DISTINCT FROM 'MUSE_JEV_RESEARCH_SELECTION_B1_V1'
 OR started.body->'selection_rule'->>'quality_policy'
    IS DISTINCT FROM 'MUSE_JEV_COMPARATIVE_QUALITY_V2'
 OR started.body->'selection_rule'->>'quality_floor'
    IS DISTINCT FROM activation.body->>'quality_floor'
 OR latest.body->>'selection_policy' IS DISTINCT FROM 'MUSE_JEV_RESEARCH_SELECTION_B1_V1'
 OR packet->>'quality_floor' IS DISTINCT FROM activation.body->>'quality_floor'
 THEN RETURN 'SELECTION_RULE_NOT_ACTIVATED'; END IF;
 answers:=convert_from(receipt.response_bytes,'UTF8')::jsonb->'answers';
 IF request.evidence_identity->>'selection_policy'
    IS DISTINCT FROM 'MUSE_JEV_RESEARCH_SELECTION_B1_V1'
 OR packet->>'dissent' IS DISTINCT FROM answers->'verdict'->>'choice'
 THEN RETURN 'RECEIPT_BINDING_FAILURE'; END IF;
 -- The three components decide: each a unique most probable passing label. The verdict is
 -- bound above as the recorded dissent and never blocks.
 IF lab.managed_resolved_choice(answers->'news_stale') IS DISTINCT FROM 'NO'
 OR lab.managed_resolved_choice(answers->'unsupported_inference') IS DISTINCT FROM 'NO'
 OR coalesce(lab.managed_resolved_choice(answers->'already_priced'),'') NOT IN ('LOW','MEDIUM')
 THEN RETURN 'B1_COMPONENTS_REQUIRED'; END IF;
 -- The owner's QUALITY floor: a category check on a bound QUALITY_V2 receipt.
 IF packet->'quality_required' IS DISTINCT FROM 'true'::jsonb
 OR packet->>'quality_policy' IS DISTINCT FROM 'MUSE_JEV_COMPARATIVE_QUALITY_V2'
 OR packet->>'quality_receipt_id' IS NULL
 THEN RETURN 'QUALITY_RECEIPT_REQUIRED'; END IF;
 SELECT * INTO receipt FROM lab.jev_receipts
 WHERE receipt_id=(packet->>'quality_receipt_id')::uuid;
 SELECT * INTO request FROM lab.jev_requests WHERE request_id=receipt.request_id;
 SELECT * INTO quality FROM lab.managed_events WHERE kind='RESEARCH_QUALITY'
 AND body->>'cycle_id'=packet->>'cycle_id' AND body->>'item_key'=packet->>'item_key'
 AND body->>'revision'=packet->>'revision' ORDER BY event_seq DESC LIMIT 1;
 IF receipt.receipt_id IS NULL OR quality.event_id IS NULL
 OR NOT lab.managed_quality_v2_receipt_intact(receipt.receipt_id)
 OR NOT lab.research_audit_matches('MANAGED_EVENTS',to_jsonb(quality),quality.event_seq)
 OR quality.body->>'status' IS DISTINCT FROM 'RANKED'
 OR quality.body->>'quality_policy' IS DISTINCT FROM 'MUSE_JEV_COMPARATIVE_QUALITY_V2'
 OR quality.body->>'request_id' IS DISTINCT FROM request.request_id::text
 OR quality.body->'receipt_ids'->>-1 IS DISTINCT FROM receipt.receipt_id::text
 OR request.stage IS DISTINCT FROM 'TRIAGE'
 OR request.question_set_version IS DISTINCT FROM 'MUSE_JEV_COMPARATIVE_QUALITY_V2'
 OR request.template_hash IS DISTINCT FROM 'd0e98e9257da96c03f8f394bdb81ca625ca65448541893134c028432bd55beb7'
 OR receipt.completed_at>request.deadline
 OR request.evidence_identity->>'quality_policy'
    IS DISTINCT FROM 'MUSE_JEV_COMPARATIVE_QUALITY_V2'
 OR request.evidence_identity->>'selection_policy'
    IS DISTINCT FROM 'MUSE_JEV_RESEARCH_SELECTION_B1_V1'
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
 category:=lab.managed_resolved_choice(answers->'quality_category');
 IF category<>ALL(floors) THEN category:=NULL; END IF;
 IF packet->>'quality_category' IS DISTINCT FROM category
 OR quality.body->>'category' IS DISTINCT FROM category
 THEN RETURN 'QUALITY_RECEIPT_BINDING_FAILURE'; END IF;
 IF category IS NULL
 OR array_position(floors,category)<array_position(floors,packet->>'quality_floor')
 THEN RETURN 'QUALITY_FLOOR_NOT_MET'; END IF;
 RETURN NULL;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
 RETURN 'RECEIPT_BINDING_FAILURE';
END $$;
REVOKE ALL ON FUNCTION lab.managed_review_failure(jsonb),
 lab.managed_review_failure_before_b1(jsonb),lab.managed_quality_v2_receipt_intact(uuid),
 lab.managed_resolved_choice(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION lab.managed_review_failure_before_b1(jsonb) FROM catalyst_risk;
GRANT EXECUTE ON FUNCTION lab.managed_review_failure(jsonb) TO catalyst_risk;

-- Read-only selection replay (scripts/replay_selection_policy.py) as catalyst_review, which
-- already reads the managed research events: selection receipts without request_json, so
-- the replay source carries no evidence text. No write grant and no base-table grant.
CREATE VIEW lab.selection_replay_requests AS
 SELECT request_id,evidence_identity,stage,question_set_version,template_hash,deadline,created_at
 FROM lab.jev_requests WHERE question_set_version IN
  ('SKEPTIC_QUESTIONS_V1','MUSE_JEV_COMPARATIVE_QUALITY_V1','MUSE_JEV_COMPARATIVE_QUALITY_V2');
CREATE VIEW lab.selection_replay_receipts AS
 SELECT r.receipt_id,r.request_id,r.attempt,r.outcome,r.http_status,r.response_bytes,
  r.response_hash,r.actual_model,r.completed_at,r.error_code
 FROM lab.jev_receipts r JOIN lab.selection_replay_requests q USING(request_id);
CREATE VIEW lab.selection_replay_decisions AS
 SELECT d.decision_id,d.receipt_id,d.question,d.answer_json
 FROM lab.ai_decisions d JOIN lab.selection_replay_receipts r USING(receipt_id);
REVOKE ALL ON lab.selection_replay_requests,lab.selection_replay_receipts,
 lab.selection_replay_decisions FROM PUBLIC;
GRANT SELECT ON lab.selection_replay_requests,lab.selection_replay_receipts,
 lab.selection_replay_decisions TO catalyst_review;
INSERT INTO lab.schema_migrations(version) VALUES(18);
