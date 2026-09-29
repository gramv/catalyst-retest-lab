-- Selection rule JEV_TOP_K_SELECTION_V2 (2026-09-27; docs/packages/topk-v2.md and
-- docs/CONTRACT-RESOLUTIONS.md) with the pick question sets NEWS_PICK_QUESTIONS_V2 and
-- BOTH_PICK_QUESTIONS_V2 (CHART_PICK_QUESTIONS_V1 and MUSE_JEV_COMPARATIVE_QUALITY_V3 are
-- unchanged). DDL only: no audited row, table or column, and no seeded row; every template hash
-- is pinned below. JEV_TOP_K_SELECTION_V1, its question sets, migration 021's functions and every
-- stored event keep their definitions and bytes.
--
-- lab.managed_review_failure_topk_v2 is migration 021's lab.managed_review_failure_topk with
-- exactly these differences (tests/test_selection_topk_v2.py derives it from 021's text):
-- * the policy name JEV_TOP_K_SELECTION_V2 wherever V1's is named;
-- * the NEWS and BOTH question sets NEWS_PICK_QUESTIONS_V2 and BOTH_PICK_QUESTIONS_V2 and their
--   template hashes, in the pinned set of the pick's kind and in the activation's question_sets;
-- * the activation and the cycle's rule block record veto_min_probability '0.70';
-- * a veto label vetoes only when it is the unique most probable answer with a probability of
--   at least 0.70 in the stored response bytes; below that it is the uncertain code
--   <COMPONENT>_<LABEL>_UNSURE (10 points, like every uncertain component).
-- Migration 021's helpers (the receipt checks and the stored-binding checks of migration 013)
-- are reused unchanged: V2's sets have V1's question names.

-- The top-K V2 branch.
CREATE FUNCTION lab.managed_review_failure_topk_v2(packet jsonb) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE failure text; receipt lab.jev_receipts; request lab.jev_requests;
        latest lab.managed_events; started lab.managed_events; activation lab.managed_events;
        quality lab.managed_events; ranking lab.managed_events; answers jsonb; entry jsonb;
        entry_position bigint; pick_kind text; set_version text; set_template text;
        names text[]; field text; chosen text; resolved text; codes jsonb:='[]'::jsonb;
        total numeric:=0; score numeric;
        adjusted numeric; category text; floors text[]:=ARRAY['WEAK','ADEQUATE','STRONG'];
BEGIN
 IF packet->>'selection_policy' IS DISTINCT FROM 'JEV_TOP_K_SELECTION_V2' THEN
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
 set_version:=CASE pick_kind WHEN 'NEWS' THEN 'NEWS_PICK_QUESTIONS_V2'
  WHEN 'CHART' THEN 'CHART_PICK_QUESTIONS_V1' WHEN 'BOTH' THEN 'BOTH_PICK_QUESTIONS_V2' END;
 set_template:=CASE pick_kind
  WHEN 'NEWS' THEN '5fe5d168d6289e67fc24062fbb389b65c7e76875116d2518935fe60cebb61f84'
  WHEN 'CHART' THEN '5082d2dd329badd9e27d1f487aa01027ac32bedebc5b95a77e518de7a41652bc'
  WHEN 'BOTH' THEN 'f010bd3fcbca186ed713fc82de64802961e7b6c0d0eb9cc082c69a3b9b7a254e' END;
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
 OR activation.body->>'selection_policy' IS DISTINCT FROM 'JEV_TOP_K_SELECTION_V2'
 OR activation.body->>'quality_policy' IS DISTINCT FROM 'MUSE_JEV_COMPARATIVE_QUALITY_V3'
 OR activation.body->>'quality_template_hash' IS DISTINCT FROM
    'a9675c4ef501db83ed19e4fbfe07d22e55ee8daa60d45ef58fb6a372ab322f78'
 OR activation.body->'question_sets' IS DISTINCT FROM jsonb_build_object(
    'NEWS',jsonb_build_object('version','NEWS_PICK_QUESTIONS_V2','template_hash',
     '5fe5d168d6289e67fc24062fbb389b65c7e76875116d2518935fe60cebb61f84'),
    'CHART',jsonb_build_object('version','CHART_PICK_QUESTIONS_V1','template_hash',
     '5082d2dd329badd9e27d1f487aa01027ac32bedebc5b95a77e518de7a41652bc'),
    'BOTH',jsonb_build_object('version','BOTH_PICK_QUESTIONS_V2','template_hash',
     'f010bd3fcbca186ed713fc82de64802961e7b6c0d0eb9cc082c69a3b9b7a254e'))
 OR jsonb_typeof(activation.body->'k') IS DISTINCT FROM 'number'
 OR coalesce(activation.body->>'k','') !~ '^([5-9]|10)$'
 OR activation.body ? 'quality_floor'
 OR activation.body->>'veto_min_probability' IS DISTINCT FROM '0.70'
 OR started.body->'selection_rule'->>'veto_min_probability' IS DISTINCT FROM '0.70'
 OR started.body->>'selection_policy' IS DISTINCT FROM 'JEV_TOP_K_SELECTION_V2'
 OR started.body->>'report_schema_version' IS DISTINCT FROM 'AGENT_RESEARCH_REPORT_V3'
 OR started.body->'selection_rule'->>'selection_policy'
    IS DISTINCT FROM 'JEV_TOP_K_SELECTION_V2'
 OR started.body->'selection_rule'->>'quality_policy'
    IS DISTINCT FROM 'MUSE_JEV_COMPARATIVE_QUALITY_V3'
 OR started.body->'selection_rule'->'question_sets'
    IS DISTINCT FROM activation.body->'question_sets'
 OR started.body->'selection_rule'->'k' IS DISTINCT FROM activation.body->'k'
 OR latest.body->>'selection_policy' IS DISTINCT FROM 'JEV_TOP_K_SELECTION_V2'
 OR packet->'k' IS DISTINCT FROM activation.body->'k'
 THEN RETURN 'SELECTION_RULE_NOT_ACTIVATED'; END IF;
 answers:=convert_from(receipt.response_bytes,'UTF8')::jsonb->'answers';
 IF request.evidence_identity->>'selection_policy' IS DISTINCT FROM 'JEV_TOP_K_SELECTION_V2'
 OR packet->>'dissent' IS DISTINCT FROM answers->'verdict'->>'choice'
 THEN RETURN 'RECEIPT_BINDING_FAILURE'; END IF;
 -- No veto label is the unique most probable answer with a probability of at least 0.70; a
 -- unique most probable veto label below 0.70 is uncertain <COMPONENT>_<LABEL>_UNSURE, and
 -- every other component that does not pass is uncertain, coded as
 -- research_selection_topk.assess_review codes it under JEV_TOP_K_SELECTION_V2. The verdict
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
   IF (answers->field->'probabilities'->>resolved)::numeric>=0.70 THEN
    RETURN 'TOPK_VETOED';
   END IF;
   codes:=codes||to_jsonb(upper(field)||'_'||resolved||'_UNSURE');
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
 OR request.evidence_identity->>'selection_policy' IS DISTINCT FROM 'JEV_TOP_K_SELECTION_V2'
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
 OR ranking.body->>'policy' IS DISTINCT FROM 'JEV_TOP_K_SELECTION_V2'
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

-- The dispatcher: migration 021's body with the top-K V2 branch in front. CREATE OR REPLACE
-- keeps the function and its grants; every other packet takes 021's statements, byte for byte.
CREATE OR REPLACE FUNCTION lab.managed_review_failure(packet jsonb) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
BEGIN
 -- Top-K V2: its own branch (lab.managed_review_failure_topk_v2, above).
 IF packet->>'selection_policy'='JEV_TOP_K_SELECTION_V2' THEN
  RETURN lab.managed_review_failure_topk_v2(packet);
 END IF;
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
REVOKE ALL ON FUNCTION lab.managed_review_failure_topk_v2(jsonb) FROM PUBLIC;
INSERT INTO lab.schema_migrations(version) VALUES(23);
