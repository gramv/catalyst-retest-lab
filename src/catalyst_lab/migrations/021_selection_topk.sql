-- Selection rule JEV_TOP_K_SELECTION_V1 (plan phase 2; docs/packages/selection-topk.md) with
-- the pick question sets NEWS_PICK_QUESTIONS_V1, CHART_PICK_QUESTIONS_V1 and
-- BOTH_PICK_QUESTIONS_V1 and the quality set MUSE_JEV_COMPARATIVE_QUALITY_V3. DDL only: no
-- audited row, table or column, and no seeded row; every template hash is pinned below.
-- lab.managed_review_failure keeps migration 020's dispatching statements byte for byte and
-- gains one branch in front of them: a top-K packet goes to lab.managed_review_failure_topk,
-- so V2, B1, B2 and engineering packets are routed exactly as migration 020 routed them. A top-K packet is admissible only
-- when every stored-binding check of migration 013 passes for its pick review receipt (the
-- pinned question set of the pick's kind), its cycle started after an intact top-K activation,
-- no veto label is the unique most probable answer in the stored response bytes, its
-- QUALITY_V3 receipt is intact and bound, the uncertain components and both scores recomputed
-- from those bytes equal the packet's, and the packet is the RANKED entry of its cycle's one
-- RESEARCH_RANKING with the same rank, scores, answers and receipts.

-- The answer names of a pick kind's question set: its components in code order, then the
-- verdict (recorded as dissent, never blocking).
CREATE FUNCTION lab.managed_topk_question_names(kind text) RETURNS text[]
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT CASE kind
  WHEN 'NEWS' THEN ARRAY['news_stale','already_priced','mechanism_contradicted',
   'factual_claims_supported','prices_consistent','verdict']
  WHEN 'CHART' THEN ARRAY['levels_supported_by_bars','setup_already_broken',
   'factual_claims_supported','prices_consistent','verdict']
  WHEN 'BOTH' THEN ARRAY['news_stale','already_priced','mechanism_contradicted',
   'levels_supported_by_bars','setup_already_broken','factual_claims_supported',
   'prices_consistent','verdict']
 END
$$;

-- A pick review receipt: one Choice answer for each question of its kind's set.
CREATE FUNCTION lab.managed_topk_receipt_intact(receipt uuid, kind text) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE r lab.jev_receipts; q lab.jev_requests; d lab.ai_decisions; answers jsonb;
        names text[]:=lab.managed_topk_question_names(kind);
BEGIN
 IF names IS NULL THEN RETURN false; END IF;
 SELECT * INTO r FROM lab.jev_receipts WHERE receipt_id=receipt;
 SELECT * INTO q FROM lab.jev_requests WHERE request_id=r.request_id;
 IF r.receipt_id IS NULL OR r.outcome<>'VALID' OR q.request_id IS NULL
 OR NOT lab.research_audit_matches('JEV_REQUESTS',to_jsonb(q),q.event_seq)
 OR NOT lab.research_audit_matches('JEV_RECEIPTS',to_jsonb(r),r.event_seq)
 THEN RETURN false; END IF;
 answers:=convert_from(r.response_bytes,'UTF8')::jsonb->'answers';
 IF jsonb_typeof(answers) IS DISTINCT FROM 'object'
 OR NOT answers ?& names OR answers-names<>'{}'
 OR (SELECT count(*) FROM lab.ai_decisions WHERE receipt_id=receipt)<>cardinality(names)
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

-- QUALITY_V3 receipts: four score answers and the quality_category Choice.
CREATE FUNCTION lab.managed_quality_v3_receipt_intact(receipt uuid) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE r lab.jev_receipts; q lab.jev_requests; d lab.ai_decisions; answers jsonb;
        names text[]:=ARRAY['evidence_support','timing_specificity','level_rationale',
                            'disproof_quality','quality_category'];
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
 OR (SELECT count(*) FROM lab.ai_decisions WHERE receipt_id=receipt)<>5
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

-- Migration 013's stored-binding checks, statement for statement, for a top-K packet: the
-- pick review receipt is checked by lab.managed_topk_receipt_intact for the kind of the
-- latest reviewed state instead of lab.research_receipt_intact, and V2's answer conjunction
-- (JEV_SELECTION_REQUIRED) is left out; the top-K rule below replaces it.
-- tests/test_selection_topk.py compares this body with 013's.
CREATE FUNCTION lab.managed_review_failure_topk_bindings(packet jsonb) RETURNS text
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
 IF NOT lab.managed_topk_receipt_intact(receipt.receipt_id,latest.body->'state'->>'kind')
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

-- The top-K branch.
CREATE FUNCTION lab.managed_review_failure_topk(packet jsonb) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE failure text; receipt lab.jev_receipts; request lab.jev_requests;
        latest lab.managed_events; started lab.managed_events; activation lab.managed_events;
        quality lab.managed_events; ranking lab.managed_events; answers jsonb; entry jsonb;
        entry_position bigint; pick_kind text; set_version text; set_template text;
        names text[]; field text; chosen text; resolved text; codes jsonb:='[]'::jsonb;
        total numeric:=0; score numeric;
        adjusted numeric; category text; floors text[]:=ARRAY['WEAK','ADEQUATE','STRONG'];
BEGIN
 IF packet->>'selection_policy' IS DISTINCT FROM 'JEV_TOP_K_SELECTION_V1' THEN
  RETURN 'SELECTION_RULE_NOT_ACTIVATED';
 END IF;
 -- Every stored-binding check of migration 013 for the pick review receipt; V2's answer
 -- conjunction does not apply.
 failure:=lab.managed_review_failure_topk_bindings(packet);
 IF failure IS NOT NULL THEN RETURN failure; END IF;
 SELECT * INTO receipt FROM lab.jev_receipts WHERE receipt_id=(packet->>'receipt_id')::uuid;
 SELECT * INTO request FROM lab.jev_requests WHERE request_id=receipt.request_id;
 SELECT * INTO latest FROM lab.managed_events WHERE kind='RESEARCH_PACKET'
 AND body->>'cycle_id'=packet->>'cycle_id' AND body->>'item_key'=packet->>'item_key'
 ORDER BY event_seq DESC LIMIT 1;
 -- The pinned question set of the reviewed pick's kind.
 pick_kind:=latest.body->'state'->>'kind';
 set_version:=CASE pick_kind WHEN 'NEWS' THEN 'NEWS_PICK_QUESTIONS_V1'
  WHEN 'CHART' THEN 'CHART_PICK_QUESTIONS_V1' WHEN 'BOTH' THEN 'BOTH_PICK_QUESTIONS_V1' END;
 set_template:=CASE pick_kind
  WHEN 'NEWS' THEN 'cabd489b5021c7dc1f201db918ad2e772558821a201e52570cb8f0094b105a0e'
  WHEN 'CHART' THEN '5082d2dd329badd9e27d1f487aa01027ac32bedebc5b95a77e518de7a41652bc'
  WHEN 'BOTH' THEN '7bb2871752c4eb6cd4fe20c160687af6821e65daeeea5928b277fd869865e4b9' END;
 IF set_version IS NULL OR request.stage IS DISTINCT FROM 'SKEPTIC'
 OR request.question_set_version IS DISTINCT FROM set_version
 OR request.template_hash IS DISTINCT FROM set_template
 OR packet->>'question_set_version' IS DISTINCT FROM set_version
 THEN RETURN 'SELECTION_QUESTION_POLICY_MISMATCH'; END IF;
 SELECT * INTO started FROM lab.managed_events WHERE kind='RESEARCH_STARTED'
 AND body->>'cycle_id'=packet->>'cycle_id' ORDER BY event_seq LIMIT 1;
 SELECT * INTO activation FROM lab.managed_events WHERE event_seq=
  CASE WHEN started.body->'selection_rule'->>'activation_event_seq' ~ '^[0-9]{1,18}$'
  THEN (started.body->'selection_rule'->>'activation_event_seq')::bigint END;
 -- The owner's top-K activation is recorded, intact and earlier than the report V3 cycle it
 -- governs; the cycle, its latest packet and this packet all name top-K and the same K.
 IF started.event_id IS NULL OR activation.event_id IS NULL
 OR NOT lab.research_audit_matches('MANAGED_EVENTS',to_jsonb(started),started.event_seq)
 OR NOT lab.research_audit_matches('MANAGED_EVENTS',to_jsonb(activation),activation.event_seq)
 OR activation.kind IS DISTINCT FROM 'RESEARCH_SELECTION_RULE_ACTIVATED'
 OR activation.event_seq>=started.event_seq
 OR activation.event_id::text IS DISTINCT FROM
    started.body->'selection_rule'->>'activation_event_id'
 OR activation.body->>'selection_policy' IS DISTINCT FROM 'JEV_TOP_K_SELECTION_V1'
 OR activation.body->>'quality_policy' IS DISTINCT FROM 'MUSE_JEV_COMPARATIVE_QUALITY_V3'
 OR activation.body->>'quality_template_hash' IS DISTINCT FROM
    'a9675c4ef501db83ed19e4fbfe07d22e55ee8daa60d45ef58fb6a372ab322f78'
 OR activation.body->'question_sets' IS DISTINCT FROM jsonb_build_object(
    'NEWS',jsonb_build_object('version','NEWS_PICK_QUESTIONS_V1','template_hash',
     'cabd489b5021c7dc1f201db918ad2e772558821a201e52570cb8f0094b105a0e'),
    'CHART',jsonb_build_object('version','CHART_PICK_QUESTIONS_V1','template_hash',
     '5082d2dd329badd9e27d1f487aa01027ac32bedebc5b95a77e518de7a41652bc'),
    'BOTH',jsonb_build_object('version','BOTH_PICK_QUESTIONS_V1','template_hash',
     '7bb2871752c4eb6cd4fe20c160687af6821e65daeeea5928b277fd869865e4b9'))
 OR jsonb_typeof(activation.body->'k') IS DISTINCT FROM 'number'
 OR coalesce(activation.body->>'k','') !~ '^([5-9]|10)$'
 OR activation.body ? 'quality_floor'
 OR started.body->>'selection_policy' IS DISTINCT FROM 'JEV_TOP_K_SELECTION_V1'
 OR started.body->>'report_schema_version' IS DISTINCT FROM 'AGENT_RESEARCH_REPORT_V3'
 OR started.body->'selection_rule'->>'selection_policy'
    IS DISTINCT FROM 'JEV_TOP_K_SELECTION_V1'
 OR started.body->'selection_rule'->>'quality_policy'
    IS DISTINCT FROM 'MUSE_JEV_COMPARATIVE_QUALITY_V3'
 OR started.body->'selection_rule'->'question_sets'
    IS DISTINCT FROM activation.body->'question_sets'
 OR started.body->'selection_rule'->'k' IS DISTINCT FROM activation.body->'k'
 OR latest.body->>'selection_policy' IS DISTINCT FROM 'JEV_TOP_K_SELECTION_V1'
 OR packet->'k' IS DISTINCT FROM activation.body->'k'
 THEN RETURN 'SELECTION_RULE_NOT_ACTIVATED'; END IF;
 answers:=convert_from(receipt.response_bytes,'UTF8')::jsonb->'answers';
 IF request.evidence_identity->>'selection_policy' IS DISTINCT FROM 'JEV_TOP_K_SELECTION_V1'
 OR packet->>'dissent' IS DISTINCT FROM answers->'verdict'->>'choice'
 THEN RETURN 'RECEIPT_BINDING_FAILURE'; END IF;
 -- No veto label is the unique most probable answer; every other component that does not
 -- pass is uncertain, coded as research_selection_topk.assess_review codes it. The verdict
 -- is bound above as the recorded dissent and never blocks.
 names:=lab.managed_topk_question_names(pick_kind);
 FOREACH field IN ARRAY names[1:cardinality(names)-1] LOOP
  chosen:=answers->field->>'choice';
  resolved:=lab.managed_resolved_choice(answers->field);
  IF chosen='Insufficient evidence' THEN
   codes:=codes||to_jsonb(upper(field)||'_INSUFFICIENT');
  ELSIF resolved IS NULL THEN
   codes:=codes||to_jsonb(upper(field)||'_TIED');
  ELSIF (field,resolved) IN (('news_stale','YES'),('mechanism_contradicted','YES'),
   ('factual_claims_supported','UNSUPPORTED'),('prices_consistent','NO'),
   ('levels_supported_by_bars','NO'),('setup_already_broken','YES')) THEN
   RETURN 'TOPK_VETOED';
  ELSIF (field,resolved) NOT IN (('news_stale','NO'),('already_priced','LOW'),
   ('already_priced','MEDIUM'),('mechanism_contradicted','NO'),
   ('factual_claims_supported','SUPPORTED'),('prices_consistent','YES'),
   ('levels_supported_by_bars','YES'),('setup_already_broken','NO')) THEN
   codes:=codes||to_jsonb(upper(field)||'_'||resolved);
  END IF;
 END LOOP;
 -- The pick's QUALITY_V3 receipt: intact and bound to this item, revision and reviewed state.
 IF packet->>'quality_policy' IS DISTINCT FROM 'MUSE_JEV_COMPARATIVE_QUALITY_V3'
 OR packet->>'quality_receipt_id' IS NULL
 THEN RETURN 'QUALITY_RECEIPT_REQUIRED'; END IF;
 SELECT * INTO receipt FROM lab.jev_receipts
 WHERE receipt_id=(packet->>'quality_receipt_id')::uuid;
 SELECT * INTO request FROM lab.jev_requests WHERE request_id=receipt.request_id;
 SELECT * INTO quality FROM lab.managed_events WHERE kind='RESEARCH_QUALITY'
 AND body->>'cycle_id'=packet->>'cycle_id' AND body->>'item_key'=packet->>'item_key'
 AND body->>'revision'=packet->>'revision' ORDER BY event_seq DESC LIMIT 1;
 IF receipt.receipt_id IS NULL OR quality.event_id IS NULL
 OR NOT lab.managed_quality_v3_receipt_intact(receipt.receipt_id)
 OR NOT lab.research_audit_matches('MANAGED_EVENTS',to_jsonb(quality),quality.event_seq)
 OR quality.body->>'status' IS DISTINCT FROM 'SCORED'
 OR quality.body->>'quality_policy' IS DISTINCT FROM 'MUSE_JEV_COMPARATIVE_QUALITY_V3'
 OR quality.body->>'request_id' IS DISTINCT FROM request.request_id::text
 OR quality.body->'receipt_ids'->>-1 IS DISTINCT FROM receipt.receipt_id::text
 OR request.stage IS DISTINCT FROM 'TRIAGE'
 OR request.question_set_version IS DISTINCT FROM 'MUSE_JEV_COMPARATIVE_QUALITY_V3'
 OR request.template_hash IS DISTINCT FROM
    'a9675c4ef501db83ed19e4fbfe07d22e55ee8daa60d45ef58fb6a372ab322f78'
 OR receipt.completed_at>request.deadline
 OR request.evidence_identity->>'quality_policy'
    IS DISTINCT FROM 'MUSE_JEV_COMPARATIVE_QUALITY_V3'
 OR request.evidence_identity->>'selection_policy' IS DISTINCT FROM 'JEV_TOP_K_SELECTION_V1'
 OR request.evidence_identity->>'evidence_hash' IS DISTINCT FROM packet->>'evidence_hash'
 OR request.evidence_identity->>'candidate_revision' IS DISTINCT FROM packet->>'revision'
 OR request.evidence_identity->>'cycle_id' IS DISTINCT FROM packet->>'cycle_id'
 OR request.evidence_identity->>'research_item_key' IS DISTINCT FROM packet->>'item_key'
 OR request.request_json::jsonb->'state' IS DISTINCT FROM latest.body->'state'
 THEN RETURN 'QUALITY_RECEIPT_BINDING_FAILURE'; END IF;
 -- The 0-100 score from the stored bytes (research_ranking.quality_score_v3) and the category.
 answers:=convert_from(receipt.response_bytes,'UTF8')::jsonb->'answers';
 FOREACH field IN ARRAY ARRAY['evidence_support','timing_specificity','level_rationale',
                              'disproof_quality'] LOOP
  total:=total+coalesce((answers->field->'probabilities'->>'1')::numeric,0)
      +2*coalesce((answers->field->'probabilities'->>'2')::numeric,0);
 END LOOP;
 score:=round(total*12.5,4);
 category:=lab.managed_resolved_choice(answers->'quality_category');
 IF category<>ALL(floors) THEN category:=NULL; END IF;
 IF packet->>'quality_category' IS DISTINCT FROM category
 OR quality.body->>'category' IS DISTINCT FROM category
 THEN RETURN 'QUALITY_RECEIPT_BINDING_FAILURE'; END IF;
 -- adjusted_score = max(0, quality_score - 10 x uncertain_count).
 adjusted:=greatest(0,score-10*jsonb_array_length(codes));
 IF score IS DISTINCT FROM (packet->>'quality_score')::numeric
 OR score IS DISTINCT FROM (quality.body->>'score')::numeric
 OR adjusted IS DISTINCT FROM (packet->>'adjusted_score')::numeric
 OR codes IS DISTINCT FROM packet->'uncertain'
 THEN RETURN 'TOPK_SCORE_MISMATCH'; END IF;
 -- The cycle's one ranking, intact, recorded before this selection; the packet is its item's
 -- only entry, RANKED at its rank's position (ranked entries come first, adjusted scores never
 -- rising), with the same scores, uncertain components, dissent and receipts.
 SELECT * INTO ranking FROM lab.managed_events WHERE event_seq=
  CASE WHEN packet->>'ranking_event_seq' ~ '^[0-9]{1,18}$'
  THEN (packet->>'ranking_event_seq')::bigint END;
 IF ranking.event_id IS NULL OR ranking.kind IS DISTINCT FROM 'RESEARCH_RANKING'
 OR ranking.idempotency_key IS DISTINCT FROM 'research:ranking:'||(packet->>'cycle_id')
 OR ranking.event_id::text IS DISTINCT FROM packet->>'ranking_event_id'
 OR NOT lab.research_audit_matches('MANAGED_EVENTS',to_jsonb(ranking),ranking.event_seq)
 OR ranking.event_seq<=started.event_seq
 OR ranking.event_seq>=(packet->>'selection_event_seq')::bigint
 OR ranking.body->>'policy' IS DISTINCT FROM 'JEV_TOP_K_SELECTION_V1'
 OR ranking.body->>'cycle_id' IS DISTINCT FROM packet->>'cycle_id'
 OR ranking.body->'run_slot' IS DISTINCT FROM started.body->'run_slot'
 OR ranking.body->'k' IS DISTINCT FROM activation.body->'k'
 OR jsonb_typeof(ranking.body->'entries') IS DISTINCT FROM 'array'
 THEN RETURN 'TOPK_RANKING_BINDING_FAILURE'; END IF;
 SELECT e.value,e.ordinality INTO entry,entry_position
 FROM jsonb_array_elements(ranking.body->'entries') WITH ORDINALITY e
 WHERE e.value->>'item_key'=packet->>'item_key';
 IF (SELECT count(*) FROM jsonb_array_elements(ranking.body->'entries') e
     WHERE e->>'item_key'=packet->>'item_key')<>1
 OR entry->>'status' IS DISTINCT FROM 'RANKED'
 OR entry->'revision' IS DISTINCT FROM packet->'revision'
 OR jsonb_typeof(entry->'rank') IS DISTINCT FROM 'number'
 OR entry->'rank' IS DISTINCT FROM packet->'rank'
 OR entry_position IS DISTINCT FROM
    (CASE WHEN entry->>'rank' ~ '^[0-9]{1,9}$' THEN (entry->>'rank')::bigint END)
 OR entry->>'question_set_version' IS DISTINCT FROM set_version
 OR entry->>'adjusted_score' IS DISTINCT FROM packet->>'adjusted_score'
 OR entry->>'quality_score' IS DISTINCT FROM packet->>'quality_score'
 OR entry->>'quality_category' IS DISTINCT FROM packet->>'quality_category'
 OR entry->'uncertain' IS DISTINCT FROM packet->'uncertain'
 OR entry->'veto_reasons' IS DISTINCT FROM '[]'::jsonb
 OR entry->>'dissent' IS DISTINCT FROM packet->>'dissent'
 OR entry->>'receipt_id' IS DISTINCT FROM packet->>'receipt_id'
 OR entry->>'quality_receipt_id' IS DISTINCT FROM packet->>'quality_receipt_id'
 OR EXISTS(SELECT 1 FROM jsonb_array_elements(ranking.body->'entries') WITH ORDINALITY a
     JOIN jsonb_array_elements(ranking.body->'entries') WITH ORDINALITY b
     ON b.ordinality=a.ordinality+1
     WHERE b.value->>'status'='RANKED' AND (a.value->>'status' IS DISTINCT FROM 'RANKED'
      OR (b.value->>'adjusted_score')::numeric>(a.value->>'adjusted_score')::numeric))
 THEN RETURN 'TOPK_RANKING_BINDING_FAILURE'; END IF;
 RETURN NULL;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
 RETURN 'RECEIPT_BINDING_FAILURE';
END $$;

-- The dispatcher: migration 020's body with the top-K branch in front. CREATE OR REPLACE
-- keeps the function and its grants; every non-top-K packet takes 020's statements.
CREATE OR REPLACE FUNCTION lab.managed_review_failure(packet jsonb) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
BEGIN
 -- Top-K: its own branch (lab.managed_review_failure_topk, above). Every other packet is
 -- routed by migration 020's statements that follow, byte for byte.
 IF packet->>'selection_policy'='JEV_TOP_K_SELECTION_V1' THEN
  RETURN lab.managed_review_failure_topk(packet);
 END IF;
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
REVOKE ALL ON FUNCTION lab.managed_review_failure(jsonb),lab.managed_review_failure_topk(jsonb),
 lab.managed_review_failure_topk_bindings(jsonb),lab.managed_topk_receipt_intact(uuid,text),
 lab.managed_quality_v3_receipt_intact(uuid),lab.managed_topk_question_names(text)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.managed_review_failure(jsonb) TO catalyst_risk;
INSERT INTO lab.schema_migrations(version) VALUES(21);
