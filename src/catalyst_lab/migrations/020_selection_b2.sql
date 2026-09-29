-- Selection rule B2 (MUSE_JEV_RESEARCH_SELECTION_B2_V1) and its question set
-- SKEPTIC_QUESTIONS_V2 (docs/CONTRACT-RESOLUTIONS.md). DDL only: no audited row, table or
-- column, and no seeded row; the V2 template hash is pinned in the B2 branch below, as
-- migrations 014 and 018 pin V1's. Migration 018's lab.managed_review_failure is renamed
-- managed_review_failure_before_b2 and kept byte for byte: the new dispatcher sends a B1
-- packet to it, and every other non-B2 packet to lab.managed_review_failure_before_b1, the
-- function migration 018 itself delegated those packets to. A B2 packet is admissible only
-- when every stored-binding check of migration 013 passes for its six-answer receipt, the
-- receipt is SKEPTIC_QUESTIONS_V2 with the pinned template hash, its cycle started after an
-- intact B2 activation, the reviewed state carries the agent's rationale, the five
-- components pass in the stored response bytes (the verdict is recorded as dissent and never
-- blocks) and its QUALITY_V2 category is at or above the activated floor, exactly as for B1.

-- SKEPTIC_QUESTIONS_V2 receipts: six Choice answers, where lab.research_receipt_intact
-- (migration 010) expects V1's four.
CREATE FUNCTION lab.managed_skeptic_v2_receipt_intact(receipt uuid) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE r lab.jev_receipts; q lab.jev_requests; d lab.ai_decisions; answers jsonb;
        names text[]:=ARRAY['news_stale','already_priced','mechanism_contradicted',
                            'inference_labelled','factual_claims_supported','verdict'];
BEGIN
 SELECT * INTO r FROM lab.jev_receipts WHERE receipt_id=receipt;
 SELECT * INTO q FROM lab.jev_requests WHERE request_id=r.request_id;
 IF r.receipt_id IS NULL OR r.outcome<>'VALID' OR q.request_id IS NULL
 OR NOT lab.research_audit_matches('JEV_REQUESTS',to_jsonb(q),q.event_seq)
 OR NOT lab.research_audit_matches('JEV_RECEIPTS',to_jsonb(r),r.event_seq)
 THEN RETURN false; END IF;
 answers:=convert_from(r.response_bytes,'UTF8')::jsonb->'answers';
 IF jsonb_typeof(answers) IS DISTINCT FROM 'object'
 OR NOT answers ?& names OR answers-names<>'{}'
 OR (SELECT count(*) FROM lab.ai_decisions WHERE receipt_id=receipt)<>6
 THEN RETURN false; END IF;
 FOR d IN SELECT * FROM lab.ai_decisions WHERE receipt_id=receipt LOOP
  IF NOT lab.research_audit_matches('AI_DECISIONS',to_jsonb(d),d.event_seq)
  OR d.answer_json IS DISTINCT FROM answers->d.question
  OR d.answer_json->>'type' IS DISTINCT FROM 'choice'
  THEN RETURN false; END IF;
 END LOOP;
 RETURN true;
EXCEPTION WHEN OTHERS THEN RETURN false;
END $$;

-- Migration 013's stored-binding checks, statement for statement, for a B2 packet: the
-- SKEPTIC receipt is checked by lab.managed_skeptic_v2_receipt_intact instead of
-- lab.research_receipt_intact, and V2's answer conjunction (JEV_SELECTION_REQUIRED) is left
-- out, as B1 ignores it; B2's components replace it. tests/test_selection_b2.py compares
-- this body with 013's.
CREATE FUNCTION lab.managed_review_failure_b2_bindings(packet jsonb) RETURNS text
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
 IF NOT lab.managed_skeptic_v2_receipt_intact(receipt.receipt_id)
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
 RETURN NULL;
END $$;

-- The B2 branch: migration 018's B1 branch with B2's question set, components, activation
-- and policy name, plus the rationale requirement.
CREATE FUNCTION lab.managed_review_failure_b2(packet jsonb) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE failure text; receipt lab.jev_receipts; request lab.jev_requests;
        latest lab.managed_events; started lab.managed_events; activation lab.managed_events;
        quality lab.managed_events; answers jsonb; category text; score numeric:=0;
        field text; floors text[]:=ARRAY['WEAK','ADEQUATE','STRONG'];
BEGIN
 IF packet->>'selection_policy' IS DISTINCT FROM 'MUSE_JEV_RESEARCH_SELECTION_B2_V1' THEN
  RETURN 'SELECTION_RULE_NOT_ACTIVATED';
 END IF;
 -- Every stored-binding check of migration 013, for a six-answer SKEPTIC_QUESTIONS_V2
 -- receipt; V2's answer conjunction does not apply.
 failure:=lab.managed_review_failure_b2_bindings(packet);
 IF failure IS NOT NULL THEN RETURN failure; END IF;
 SELECT * INTO receipt FROM lab.jev_receipts WHERE receipt_id=(packet->>'receipt_id')::uuid;
 SELECT * INTO request FROM lab.jev_requests WHERE request_id=receipt.request_id;
 IF request.stage IS DISTINCT FROM 'SKEPTIC'
 OR request.question_set_version IS DISTINCT FROM 'SKEPTIC_QUESTIONS_V2'
 OR request.template_hash IS DISTINCT FROM '754d70c80c68a4ea9f453d998ae34b64f004070dc118daee2e5dbe8a27e6722e'
 OR packet->>'question_set_version' IS DISTINCT FROM 'SKEPTIC_QUESTIONS_V2'
 THEN RETURN 'SELECTION_QUESTION_POLICY_MISMATCH'; END IF;
 SELECT * INTO latest FROM lab.managed_events WHERE kind='RESEARCH_PACKET'
 AND body->>'cycle_id'=packet->>'cycle_id' AND body->>'item_key'=packet->>'item_key'
 ORDER BY event_seq DESC LIMIT 1;
 SELECT * INTO started FROM lab.managed_events WHERE kind='RESEARCH_STARTED'
 AND body->>'cycle_id'=packet->>'cycle_id' ORDER BY event_seq LIMIT 1;
 SELECT * INTO activation FROM lab.managed_events WHERE event_seq=
  CASE WHEN started.body->'selection_rule'->>'activation_event_seq' ~ '^[0-9]{1,18}$'
  THEN (started.body->'selection_rule'->>'activation_event_seq')::bigint END;
 -- The owner's B2 activation is recorded, intact and earlier than the cycle it governs; the
 -- cycle, its latest packet and this packet all name B2, its question set and the floor.
 IF started.event_id IS NULL OR activation.event_id IS NULL
 OR NOT lab.research_audit_matches('MANAGED_EVENTS',to_jsonb(started),started.event_seq)
 OR NOT lab.research_audit_matches('MANAGED_EVENTS',to_jsonb(activation),activation.event_seq)
 OR activation.kind IS DISTINCT FROM 'RESEARCH_SELECTION_RULE_ACTIVATED'
 OR activation.event_seq>=started.event_seq
 OR activation.event_id::text IS DISTINCT FROM
    started.body->'selection_rule'->>'activation_event_id'
 OR activation.body->>'selection_policy' IS DISTINCT FROM 'MUSE_JEV_RESEARCH_SELECTION_B2_V1'
 OR activation.body->>'quality_policy' IS DISTINCT FROM 'MUSE_JEV_COMPARATIVE_QUALITY_V2'
 OR activation.body->>'question_set_version' IS DISTINCT FROM 'SKEPTIC_QUESTIONS_V2'
 OR coalesce(activation.body->>'quality_floor','')<>ALL(floors)
 OR started.body->>'selection_policy' IS DISTINCT FROM 'MUSE_JEV_RESEARCH_SELECTION_B2_V1'
 OR started.body->'selection_rule'->>'selection_policy'
    IS DISTINCT FROM 'MUSE_JEV_RESEARCH_SELECTION_B2_V1'
 OR started.body->'selection_rule'->>'quality_policy'
    IS DISTINCT FROM 'MUSE_JEV_COMPARATIVE_QUALITY_V2'
 OR started.body->'selection_rule'->>'question_set_version'
    IS DISTINCT FROM 'SKEPTIC_QUESTIONS_V2'
 OR started.body->'selection_rule'->>'quality_floor'
    IS DISTINCT FROM activation.body->>'quality_floor'
 OR latest.body->>'selection_policy' IS DISTINCT FROM 'MUSE_JEV_RESEARCH_SELECTION_B2_V1'
 OR packet->>'quality_floor' IS DISTINCT FROM activation.body->>'quality_floor'
 THEN RETURN 'SELECTION_RULE_NOT_ACTIVATED'; END IF;
 answers:=convert_from(receipt.response_bytes,'UTF8')::jsonb->'answers';
 IF request.evidence_identity->>'selection_policy'
    IS DISTINCT FROM 'MUSE_JEV_RESEARCH_SELECTION_B2_V1'
 OR packet->>'dissent' IS DISTINCT FROM answers->'verdict'->>'choice'
 THEN RETURN 'RECEIPT_BINDING_FAILURE'; END IF;
 -- B2 judges the proposer's claims: the reviewed state carries a rationale with claims.
 IF coalesce(CASE WHEN jsonb_typeof(latest.body->'state'->'rationale'->'claims')='array'
    THEN jsonb_array_length(latest.body->'state'->'rationale'->'claims') END,0)=0
 THEN RETURN 'RATIONALE_REQUIRED'; END IF;
 -- The five components decide: each a unique most probable passing label. The verdict is
 -- bound above as the recorded dissent and never blocks.
 IF lab.managed_resolved_choice(answers->'news_stale') IS DISTINCT FROM 'NO'
 OR coalesce(lab.managed_resolved_choice(answers->'already_priced'),'') NOT IN ('LOW','MEDIUM')
 OR lab.managed_resolved_choice(answers->'mechanism_contradicted') IS DISTINCT FROM 'NO'
 OR lab.managed_resolved_choice(answers->'inference_labelled') IS DISTINCT FROM 'YES'
 OR lab.managed_resolved_choice(answers->'factual_claims_supported')
    IS DISTINCT FROM 'SUPPORTED'
 THEN RETURN 'B2_COMPONENTS_REQUIRED'; END IF;
 -- The owner's QUALITY floor: migration 018's block, with B2's policy name.
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
    IS DISTINCT FROM 'MUSE_JEV_RESEARCH_SELECTION_B2_V1'
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

ALTER FUNCTION lab.managed_review_failure(jsonb) RENAME TO managed_review_failure_before_b2;
CREATE FUNCTION lab.managed_review_failure(packet jsonb) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
BEGIN
 -- B2: its own branch (lab.managed_review_failure_b2, above).
 IF packet->>'selection_policy'='MUSE_JEV_RESEARCH_SELECTION_B2_V1' THEN
  RETURN lab.managed_review_failure_b2(packet);
 END IF;
 -- B1: migration 018's function, renamed and unchanged.
 IF packet->>'selection_policy'='MUSE_JEV_RESEARCH_SELECTION_B1_V1' THEN
  RETURN lab.managed_review_failure_before_b2(packet);
 END IF;
 -- V2 and every other policy: the function migration 018 delegated them to, unchanged.
 RETURN lab.managed_review_failure_before_b1(packet);
END $$;
REVOKE ALL ON FUNCTION lab.managed_review_failure(jsonb),
 lab.managed_review_failure_before_b2(jsonb),lab.managed_review_failure_b2(jsonb),
 lab.managed_review_failure_b2_bindings(jsonb),lab.managed_skeptic_v2_receipt_intact(uuid)
 FROM PUBLIC;
REVOKE ALL ON FUNCTION lab.managed_review_failure_before_b2(jsonb) FROM catalyst_risk;
GRANT EXECUTE ON FUNCTION lab.managed_review_failure(jsonb) TO catalyst_risk;

-- The evidence-free selection-replay views of migration 018 (catalyst_review, read-only)
-- now also carry SKEPTIC_QUESTIONS_V2 requests, so B2 is replayed over its own receipts.
-- Same columns, same grants; still no request_json.
CREATE OR REPLACE VIEW lab.selection_replay_requests AS
 SELECT request_id,evidence_identity,stage,question_set_version,template_hash,deadline,created_at
 FROM lab.jev_requests WHERE question_set_version IN
  ('SKEPTIC_QUESTIONS_V1','SKEPTIC_QUESTIONS_V2','MUSE_JEV_COMPARATIVE_QUALITY_V1',
   'MUSE_JEV_COMPARATIVE_QUALITY_V2');
REVOKE ALL ON lab.selection_replay_requests,lab.selection_replay_receipts,
 lab.selection_replay_decisions FROM PUBLIC;
GRANT SELECT ON lab.selection_replay_requests,lab.selection_replay_receipts,
 lab.selection_replay_decisions TO catalyst_review;
INSERT INTO lab.schema_migrations(version) VALUES(20);
