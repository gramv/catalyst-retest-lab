-- Selection rule JEV_TOP_K_SELECTION_V3 (package jev-b2, plan docs/TRADING-QUALITY-PLAN.md B2
-- and section 10; owner 2026-10-02 "Phases B, C, D: yes", Jev design delegated). DDL only: no
-- audited row, table or column and no seeded row; every template hash is pinned below. V1 and
-- V2 (migrations 021 and 023), their question sets, functions and every stored event keep their
-- definitions and bytes. Numbered 030 after the public page's 028 and 029; nothing here reads or
-- changes their views, so renumbering is a file rename and the schema pin.
--
-- A V3 packet is admissible only when:
-- * every stored-binding check of migration 013 passes for its stage-1 receipt (migration 021's
--   lab.managed_review_failure_topk_bindings with the receipt checked as a V3 checks receipt:
--   one Noul for thesis_contradicted, concrete_coin_specific_catalyst and claim_1..claim_<n>,
--   n = min(6, the reviewed state's rationale claims));
-- * that receipt is of PICK_CHECK_QUESTIONS_V1_C<n> with its pinned hash, and its cycle started
--   after an intact V3 activation with the pinned question sets and thresholds;
-- * stage 1 did not veto, recomputed from the stored response bytes: thesis_contradicted below
--   0.70, and fewer than half of the asked factual claims (kind other than ECONOMIC_LINK) at or
--   below 0.30;
-- * its cycle's RESEARCH_COMPARISON_STATE is intact, its order is the SHA-256 order of
--   '<cycle_id>:<item_key>' (recomputed here), the packet is at its recorded position with its
--   symbol, and the comparison receipt (the final one of its request) is intact, of
--   COMPARATIVE_PICK_QUESTIONS_V1_N<m> with its pinned hash, bound to exactly that state;
-- * its candidate Noul in those bytes is at least 0.60 (T) and equals the packet's probability;
-- * the packet is the RANKED entry of its cycle's one RESEARCH_RANKING at its rank's position,
--   ranked entries first with probabilities never rising.

-- The answer names of a V3 checks receipt for a reviewed state.
CREATE FUNCTION lab.managed_topk_v3_check_names(state jsonb) RETURNS text[]
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT ARRAY['thesis_contradicted','concrete_coin_specific_catalyst']
  || coalesce(ARRAY(SELECT 'claim_'||g FROM generate_series(1,
   CASE WHEN jsonb_typeof(state->'rationale'->'claims')='array'
   THEN least(jsonb_array_length(state->'rationale'->'claims'),6) ELSE 0 END) g ORDER BY g),
   ARRAY[]::text[])
$$;

-- A Jev receipt whose answers are exactly ``names``, each projected intact with its ``kinds``
-- type (the same position in both arrays).
CREATE FUNCTION lab.managed_topk_v3_receipt_intact(receipt uuid, names text[], kinds text[])
RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE r lab.jev_receipts; q lab.jev_requests; d lab.ai_decisions; answers jsonb;
BEGIN
 IF names IS NULL OR kinds IS NULL OR cardinality(names)<>cardinality(kinds)
 OR cardinality(names)=0 THEN RETURN false; END IF;
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
  OR d.answer_json->>'type' IS DISTINCT FROM kinds[array_position(names,d.question)]
  THEN RETURN false; END IF;
 END LOOP;
 RETURN true;
EXCEPTION WHEN OTHERS THEN RETURN false;
END $$;

-- A stage-1 receipt: one Noul per check name of the reviewed state.
CREATE FUNCTION lab.managed_topk_v3_checks_receipt_intact(receipt uuid, state jsonb)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
 SELECT lab.managed_topk_v3_receipt_intact(receipt,lab.managed_topk_v3_check_names(state),
  array_fill('noul'::text,ARRAY[cardinality(lab.managed_topk_v3_check_names(state))]))
$$;

-- Migration 021's lab.managed_review_failure_topk_bindings, statement for statement, with the
-- receipt checked as a V3 checks receipt of the latest reviewed state.
-- tests/test_selection_topk_v3.py derives this body from 021's.
CREATE FUNCTION lab.managed_review_failure_topk_v3_bindings(packet jsonb) RETURNS text
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
 IF NOT lab.managed_topk_v3_checks_receipt_intact(receipt.receipt_id,latest.body->'state')
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

-- The top-K V3 branch.
CREATE FUNCTION lab.managed_review_failure_topk_v3(packet jsonb) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE failure text; receipt lab.jev_receipts; request lab.jev_requests;
        latest lab.managed_events; started lab.managed_events; activation lab.managed_events;
        comparison lab.managed_events; ranking lab.managed_events; creceipt lab.jev_receipts;
        crequest lab.jev_requests; answers jsonb; claims jsonb; entry jsonb;
        entry_position bigint; n integer; m integer; pos integer; i integer;
        factual integer:=0; unsupported integer:=0; set_version text; set_template text;
        probability numeric;
BEGIN
 IF packet->>'selection_policy' IS DISTINCT FROM 'JEV_TOP_K_SELECTION_V3' THEN
  RETURN 'SELECTION_RULE_NOT_ACTIVATED';
 END IF;
 -- Every stored-binding check of migration 013 for the stage-1 receipt.
 failure:=lab.managed_review_failure_topk_v3_bindings(packet);
 IF failure IS NOT NULL THEN RETURN failure; END IF;
 SELECT * INTO receipt FROM lab.jev_receipts WHERE receipt_id=(packet->>'receipt_id')::uuid;
 SELECT * INTO request FROM lab.jev_requests WHERE request_id=receipt.request_id;
 SELECT * INTO latest FROM lab.managed_events WHERE kind='RESEARCH_PACKET'
 AND body->>'cycle_id'=packet->>'cycle_id' AND body->>'item_key'=packet->>'item_key'
 ORDER BY event_seq DESC LIMIT 1;
 -- The pinned checks set for the reviewed state's claim count (at most 6).
 claims:=latest.body->'state'->'rationale'->'claims';
 n:=CASE WHEN jsonb_typeof(claims)='array' THEN least(jsonb_array_length(claims),6) ELSE 0 END;
 set_version:='PICK_CHECK_QUESTIONS_V1_C'||n;
 set_template:=CASE n
  WHEN 0 THEN '4f719a1868fe0d6e0326b3fb3ca8e10afb790e9cf80f464cbd071bce34dfd05f'
  WHEN 1 THEN '954a2957250862870cf176a5ef315496205d2a11ec93cce0cd9857f3c9cd67aa'
  WHEN 2 THEN '13a5e6da923fcc10796d656decbdb09f948810eff8b9f020d5f0b97368280707'
  WHEN 3 THEN '8bc4e46a6c6d229fd8ae1b753948851c003488aba7fc88e9bb9218e95775f579'
  WHEN 4 THEN 'ae1cd48d70b0fa5eb2495b9a4b794d29bb16648a68b43838651de95a89ce7f1e'
  WHEN 5 THEN 'dae84adcbf06f7cabacac4cb2367fba732b8867b9b1d26768344d052642571c7'
  WHEN 6 THEN '88c3187e549a4fd3a62126ef107af906d93ca60889996c45d26b5f0770a6773e'
 END;
 IF request.stage IS DISTINCT FROM 'SKEPTIC'
 OR request.question_set_version IS DISTINCT FROM set_version
 OR request.template_hash IS DISTINCT FROM set_template
 OR packet->>'question_set_version' IS DISTINCT FROM set_version
 THEN RETURN 'SELECTION_QUESTION_POLICY_MISMATCH'; END IF;
 SELECT * INTO started FROM lab.managed_events WHERE kind='RESEARCH_STARTED'
 AND body->>'cycle_id'=packet->>'cycle_id' ORDER BY event_seq LIMIT 1;
 SELECT * INTO activation FROM lab.managed_events WHERE event_seq=
  CASE WHEN started.body->'selection_rule'->>'activation_event_seq' ~ '^[0-9]{1,18}$'
  THEN (started.body->'selection_rule'->>'activation_event_seq')::bigint END;
 -- The owner's V3 activation is recorded, intact, earlier than the report V3 cycle it governs,
 -- with the pinned question sets and thresholds; the cycle's rule block records the same rule.
 IF started.event_id IS NULL OR activation.event_id IS NULL
 OR NOT lab.research_audit_matches('MANAGED_EVENTS',to_jsonb(started),started.event_seq)
 OR NOT lab.research_audit_matches('MANAGED_EVENTS',to_jsonb(activation),activation.event_seq)
 OR activation.kind IS DISTINCT FROM 'RESEARCH_SELECTION_RULE_ACTIVATED'
 OR activation.event_seq>=started.event_seq
 OR activation.event_id::text IS DISTINCT FROM
    started.body->'selection_rule'->>'activation_event_id'
 OR activation.body->>'selection_policy' IS DISTINCT FROM 'JEV_TOP_K_SELECTION_V3'
 OR activation.body->'question_sets' IS DISTINCT FROM '{"checks":{"PICK_CHECK_QUESTIONS_V1_C0":"4f719a1868fe0d6e0326b3fb3ca8e10afb790e9cf80f464cbd071bce34dfd05f",
  "PICK_CHECK_QUESTIONS_V1_C1":"954a2957250862870cf176a5ef315496205d2a11ec93cce0cd9857f3c9cd67aa",
  "PICK_CHECK_QUESTIONS_V1_C2":"13a5e6da923fcc10796d656decbdb09f948810eff8b9f020d5f0b97368280707",
  "PICK_CHECK_QUESTIONS_V1_C3":"8bc4e46a6c6d229fd8ae1b753948851c003488aba7fc88e9bb9218e95775f579",
  "PICK_CHECK_QUESTIONS_V1_C4":"ae1cd48d70b0fa5eb2495b9a4b794d29bb16648a68b43838651de95a89ce7f1e",
  "PICK_CHECK_QUESTIONS_V1_C5":"dae84adcbf06f7cabacac4cb2367fba732b8867b9b1d26768344d052642571c7",
  "PICK_CHECK_QUESTIONS_V1_C6":"88c3187e549a4fd3a62126ef107af906d93ca60889996c45d26b5f0770a6773e"},
  "comparison":{"COMPARATIVE_PICK_QUESTIONS_V1_N1":"9072234e67f9668f4e5525754397862936602e03e89167258a7cf5dc62d63881",
  "COMPARATIVE_PICK_QUESTIONS_V1_N10":"a1b070533ed3c56c686f36917c3f40b3eec195c9d767dd4c2581b43375c0d927",
  "COMPARATIVE_PICK_QUESTIONS_V1_N2":"1379d90bbe3feeafb729699478666a2d9db50cdabfef08033dd7e328debf8361",
  "COMPARATIVE_PICK_QUESTIONS_V1_N3":"8f3385f107244c1de66b04c5d0800cbeb24fd735b6460b26567190262037dce3",
  "COMPARATIVE_PICK_QUESTIONS_V1_N4":"0d3221956e70efdb6ca66ae175c7b2ac45fe18eb5c5760b793591f7a921848e5",
  "COMPARATIVE_PICK_QUESTIONS_V1_N5":"1e15bb295582ccb22c82e6434546cb1e664fc575dfb4e53e12bba0cb5c913c1a",
  "COMPARATIVE_PICK_QUESTIONS_V1_N6":"2ead06ce49b7fc1c2ec49d57a971d3177bf088e6cc1d9e9e9c418bf9ac501582",
  "COMPARATIVE_PICK_QUESTIONS_V1_N7":"d8aa1de2e6d16f4720f9eb67d58462537249a2005cc0099e60cfff70a8b134f3",
  "COMPARATIVE_PICK_QUESTIONS_V1_N8":"e39cbcdbf30ddc8ade87f5c0b1935398dc489b1f523dc1f66fb77fa018a9072d",
  "COMPARATIVE_PICK_QUESTIONS_V1_N9":"33c2e1bf4ab5cd8a87a9839cd8ce44b33e785fb6fe9a2f562d445df3f6668793"}}'::jsonb
 OR jsonb_build_object('veto_min_probability',activation.body->'veto_min_probability',
    'claim_unsupported_at',activation.body->'claim_unsupported_at',
    'select_at',activation.body->'select_at','reject_at',activation.body->'reject_at',
    'max_claims',activation.body->'max_claims','max_candidates',
    activation.body->'max_candidates','shuffle',activation.body->'shuffle')
    IS DISTINCT FROM '{"claim_unsupported_at":"0.30","max_candidates":10,"max_claims":6,"reject_at":"0.40","select_at":"0.60","shuffle":"SHA256_CYCLE_ITEM_ASCENDING_V1","veto_min_probability":"0.70"}'::jsonb
 OR jsonb_typeof(activation.body->'k') IS DISTINCT FROM 'number'
 OR coalesce(activation.body->>'k','') !~ '^([5-9]|10)$'
 OR activation.body ? 'quality_floor'
 OR started.body->>'selection_policy' IS DISTINCT FROM 'JEV_TOP_K_SELECTION_V3'
 OR started.body->>'report_schema_version' IS DISTINCT FROM 'AGENT_RESEARCH_REPORT_V3'
 OR (started.body->'selection_rule')-'activation_event_id'-'activation_event_seq'
    IS DISTINCT FROM (activation.body)-'runtime_id'-'source'
 OR latest.body->>'selection_policy' IS DISTINCT FROM 'JEV_TOP_K_SELECTION_V3'
 OR packet->'k' IS DISTINCT FROM activation.body->'k'
 OR packet->>'select_at' IS DISTINCT FROM '0.60'
 THEN RETURN 'SELECTION_RULE_NOT_ACTIVATED'; END IF;
 IF request.evidence_identity->>'selection_policy' IS DISTINCT FROM 'JEV_TOP_K_SELECTION_V3'
 THEN RETURN 'RECEIPT_BINDING_FAILURE'; END IF;
 -- Stage 1, from the stored bytes (research_selection_topk_v3.assess_checks): the thesis
 -- contradicted at 0.70 or more, or at least half of the asked factual claims at or below 0.30
 -- ("stated"), vetoes.
 answers:=convert_from(receipt.response_bytes,'UTF8')::jsonb->'answers';
 IF (answers->'thesis_contradicted'->>'noul')::numeric>=0.70 THEN RETURN 'TOPK_VETOED'; END IF;
 FOR i IN 1..n LOOP
  IF jsonb_typeof(claims->(i-1))='object'
  AND claims->(i-1)->>'kind' IS DISTINCT FROM 'ECONOMIC_LINK' THEN
   factual:=factual+1;
   IF (answers->('claim_'||i)->>'noul')::numeric<=0.30 THEN unsupported:=unsupported+1; END IF;
  END IF;
 END LOOP;
 IF factual>0 AND 2*unsupported>=factual THEN RETURN 'TOPK_VETOED'; END IF;
 -- Stage 2: the cycle's comparison state, its recomputed order and the packet's place in it.
 SELECT * INTO comparison FROM lab.managed_events WHERE event_seq=
  CASE WHEN packet->>'comparison_state_event_seq' ~ '^[0-9]{1,18}$'
  THEN (packet->>'comparison_state_event_seq')::bigint END;
 IF comparison.event_id IS NULL OR comparison.kind IS DISTINCT FROM 'RESEARCH_COMPARISON_STATE'
 OR comparison.idempotency_key IS DISTINCT FROM
    'research:'||(packet->>'cycle_id')||':comparison-state'
 OR NOT lab.research_audit_matches('MANAGED_EVENTS',to_jsonb(comparison),comparison.event_seq)
 OR comparison.event_seq<=started.event_seq
 OR comparison.body->>'cycle_id' IS DISTINCT FROM packet->>'cycle_id'
 OR comparison.body->>'selection_policy' IS DISTINCT FROM 'JEV_TOP_K_SELECTION_V3'
 OR comparison.body->>'shuffle' IS DISTINCT FROM 'SHA256_CYCLE_ITEM_ASCENDING_V1'
 OR jsonb_typeof(comparison.body->'order') IS DISTINCT FROM 'array'
 OR jsonb_array_length(comparison.body->'order') NOT BETWEEN 1 AND 10
 OR jsonb_typeof(comparison.body->'state'->'candidates') IS DISTINCT FROM 'array'
 OR jsonb_array_length(comparison.body->'state'->'candidates')
    IS DISTINCT FROM jsonb_array_length(comparison.body->'order')
 OR comparison.body->'order' IS DISTINCT FROM (SELECT jsonb_agg(o ORDER BY encode(
    public.digest((packet->>'cycle_id')||':'||(o->>'item_key'),'sha256'),'hex'))
    FROM jsonb_array_elements(comparison.body->'order') o)
 OR coalesce(packet->>'comparison_position','') !~ '^[0-9]$'
 THEN RETURN 'TOPK_COMPARISON_BINDING_FAILURE'; END IF;
 m:=jsonb_array_length(comparison.body->'order');
 pos:=(packet->>'comparison_position')::integer;
 IF pos>=m
 OR comparison.body->'order'->pos->>'item_key' IS DISTINCT FROM packet->>'item_key'
 OR comparison.body->'order'->pos->'revision' IS DISTINCT FROM packet->'revision'
 OR comparison.body->'state'->'candidates'->pos->>'symbol' IS DISTINCT FROM packet->>'symbol'
 THEN RETURN 'TOPK_COMPARISON_BINDING_FAILURE'; END IF;
 -- The comparison receipt: intact, the final one of its request, of the pinned set for m
 -- candidates, bound to exactly the recorded state and this cycle.
 SELECT * INTO creceipt FROM lab.jev_receipts
 WHERE receipt_id=(packet->>'comparison_receipt_id')::uuid;
 SELECT * INTO crequest FROM lab.jev_requests WHERE request_id=creceipt.request_id;
 IF creceipt.receipt_id IS NULL
 OR NOT lab.managed_topk_v3_receipt_intact(creceipt.receipt_id,
    ARRAY(SELECT 'candidate_'||g FROM generate_series(1,m) g ORDER BY g)
    ||ARRAY['best_candidate_first','best_candidate_reversed'],
    array_fill('noul'::text,ARRAY[m])||ARRAY['choice','choice'])
 OR EXISTS(SELECT 1 FROM lab.jev_receipts later WHERE later.request_id=creceipt.request_id
    AND (later.attempt,later.event_seq)>(creceipt.attempt,creceipt.event_seq))
 OR crequest.stage IS DISTINCT FROM 'TRIAGE'
 OR crequest.question_set_version IS DISTINCT FROM 'COMPARATIVE_PICK_QUESTIONS_V1_N'||m
 OR packet->>'comparison_question_set_version'
    IS DISTINCT FROM 'COMPARATIVE_PICK_QUESTIONS_V1_N'||m
 OR crequest.template_hash IS DISTINCT FROM (CASE m
  WHEN 1 THEN '9072234e67f9668f4e5525754397862936602e03e89167258a7cf5dc62d63881'
  WHEN 2 THEN '1379d90bbe3feeafb729699478666a2d9db50cdabfef08033dd7e328debf8361'
  WHEN 3 THEN '8f3385f107244c1de66b04c5d0800cbeb24fd735b6460b26567190262037dce3'
  WHEN 4 THEN '0d3221956e70efdb6ca66ae175c7b2ac45fe18eb5c5760b793591f7a921848e5'
  WHEN 5 THEN '1e15bb295582ccb22c82e6434546cb1e664fc575dfb4e53e12bba0cb5c913c1a'
  WHEN 6 THEN '2ead06ce49b7fc1c2ec49d57a971d3177bf088e6cc1d9e9e9c418bf9ac501582'
  WHEN 7 THEN 'd8aa1de2e6d16f4720f9eb67d58462537249a2005cc0099e60cfff70a8b134f3'
  WHEN 8 THEN 'e39cbcdbf30ddc8ade87f5c0b1935398dc489b1f523dc1f66fb77fa018a9072d'
  WHEN 9 THEN '33c2e1bf4ab5cd8a87a9839cd8ce44b33e785fb6fe9a2f562d445df3f6668793'
  WHEN 10 THEN 'a1b070533ed3c56c686f36917c3f40b3eec195c9d767dd4c2581b43375c0d927'
    END)
 OR crequest.request_id::text IS DISTINCT FROM comparison.body->>'request_id'
 OR crequest.request_json::jsonb->'state' IS DISTINCT FROM comparison.body->'state'
 OR crequest.input_hash IS DISTINCT FROM comparison.body->>'state_hash'
 OR crequest.evidence_identity->>'cycle_id' IS DISTINCT FROM packet->>'cycle_id'
 OR crequest.evidence_identity->>'research_item_key' IS DISTINCT FROM 'COMPARISON'
 OR crequest.evidence_identity->>'selection_policy' IS DISTINCT FROM 'JEV_TOP_K_SELECTION_V3'
 OR crequest.evidence_identity->>'evidence_hash' IS DISTINCT FROM comparison.body->>'state_hash'
 OR crequest.deadline>(comparison.body->>'expires_at')::timestamptz
 OR creceipt.completed_at>crequest.deadline
 THEN RETURN 'TOPK_COMPARISON_BINDING_FAILURE'; END IF;
 -- The candidate's Noul is at least T = 0.60 and is the packet's probability.
 probability:=(convert_from(creceipt.response_bytes,'UTF8')::jsonb->'answers'
  ->('candidate_'||(pos+1))->>'noul')::numeric;
 IF probability IS NULL OR probability<0.60 THEN RETURN 'TOPK_BELOW_THRESHOLD'; END IF;
 IF probability IS DISTINCT FROM (packet->>'probability')::numeric
 THEN RETURN 'TOPK_COMPARISON_BINDING_FAILURE'; END IF;
 -- The cycle's one ranking, intact, recorded after the comparison state and before this
 -- selection; the packet is its item's only entry, RANKED at its rank's position (ranked
 -- entries come first, probabilities never rising), with the same receipts and probability.
 SELECT * INTO ranking FROM lab.managed_events WHERE event_seq=
  CASE WHEN packet->>'ranking_event_seq' ~ '^[0-9]{1,18}$'
  THEN (packet->>'ranking_event_seq')::bigint END;
 IF ranking.event_id IS NULL OR ranking.kind IS DISTINCT FROM 'RESEARCH_RANKING'
 OR ranking.idempotency_key IS DISTINCT FROM 'research:ranking:'||(packet->>'cycle_id')
 OR ranking.event_id::text IS DISTINCT FROM packet->>'ranking_event_id'
 OR NOT lab.research_audit_matches('MANAGED_EVENTS',to_jsonb(ranking),ranking.event_seq)
 OR ranking.event_seq<=comparison.event_seq
 OR ranking.event_seq>=(packet->>'selection_event_seq')::bigint
 OR ranking.body->>'policy' IS DISTINCT FROM 'JEV_TOP_K_SELECTION_V3'
 OR ranking.body->>'cycle_id' IS DISTINCT FROM packet->>'cycle_id'
 OR ranking.body->'run_slot' IS DISTINCT FROM started.body->'run_slot'
 OR ranking.body->'k' IS DISTINCT FROM activation.body->'k'
 OR ranking.body->'comparison'->>'state_event_seq' IS DISTINCT FROM comparison.event_seq::text
 OR ranking.body->'comparison'->>'receipt_id' IS DISTINCT FROM packet->>'comparison_receipt_id'
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
 OR (entry->>'probability')::numeric IS DISTINCT FROM probability
 OR entry->'uncertain' IS DISTINCT FROM packet->'uncertain'
 OR entry->'veto_reasons' IS DISTINCT FROM '[]'::jsonb
 OR entry->>'receipt_id' IS DISTINCT FROM packet->>'receipt_id'
 OR entry->>'comparison_receipt_id' IS DISTINCT FROM packet->>'comparison_receipt_id'
 OR entry->'comparison_position' IS DISTINCT FROM packet->'comparison_position'
 OR EXISTS(SELECT 1 FROM jsonb_array_elements(ranking.body->'entries') WITH ORDINALITY a
     JOIN jsonb_array_elements(ranking.body->'entries') WITH ORDINALITY b
     ON b.ordinality=a.ordinality+1
     WHERE b.value->>'status'='RANKED' AND (a.value->>'status' IS DISTINCT FROM 'RANKED'
      OR (b.value->>'probability')::numeric>(a.value->>'probability')::numeric))
 THEN RETURN 'TOPK_RANKING_BINDING_FAILURE'; END IF;
 RETURN NULL;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
 RETURN 'RECEIPT_BINDING_FAILURE';
END $$;

-- The dispatcher: migration 023's body with the top-K V3 branch in front. CREATE OR REPLACE
-- keeps the function and its grants; every other packet takes 023's statements, byte for byte.
CREATE OR REPLACE FUNCTION lab.managed_review_failure(packet jsonb) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
BEGIN
 -- Top-K V3: its own branch (lab.managed_review_failure_topk_v3, above).
 IF packet->>'selection_policy'='JEV_TOP_K_SELECTION_V3' THEN
  RETURN lab.managed_review_failure_topk_v3(packet);
 END IF;
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
REVOKE ALL ON FUNCTION lab.managed_review_failure_topk_v3(jsonb),
 lab.managed_review_failure_topk_v3_bindings(jsonb),lab.managed_topk_v3_check_names(jsonb),
 lab.managed_topk_v3_receipt_intact(uuid,text[],text[]),
 lab.managed_topk_v3_checks_receipt_intact(uuid,jsonb) FROM PUBLIC;
INSERT INTO lab.schema_migrations(version) VALUES(30);
