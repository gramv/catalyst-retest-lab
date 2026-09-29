-- Operator-enrolled ENGINEERING_TEST managed crypto setup (plan 0.10, owner ruling R6 option B).
-- DDL only: no audited row is inserted and no historical row or event is rewritten. No audited
-- table gains a column: lab.managed_setups.receipt_id only loses NOT NULL, so every historical
-- row's to_jsonb, and with it its audit match, is unchanged. A NULL receipt is allowed for the
-- operator enrollment policy alone. The enrollment and its RESEARCH_SELECTED-shaped packet are
-- ordinary audited lab.managed_events rows that only a catalyst_operator session can write,
-- through lab.operator_enroll_managed_engineering (precedent: the 015 operator functions and
-- docs/ENGINEERING-ACCEPTANCE.md). No Jev request, receipt or judgment exists for them.
-- lab.managed_review_failure is replaced: a packet under MANAGED_ENGINEERING_ENROLLMENT_V1 is
-- checked by lab.managed_engineering_failure; every other packet runs the 014 body unchanged.
-- Admission, the stream trigger, the one-use five-second risk authorization, protection and
-- mechanical exits stay the normal managed code paths.

-- A receipt-free setup exists only for the engineering enrollment policy, and that policy only
-- ever admits a receipt-free CRYPTO setup marked ENGINEERING_TEST.
ALTER TABLE lab.managed_setups ALTER COLUMN receipt_id DROP NOT NULL;
ALTER TABLE lab.managed_setups ADD CONSTRAINT managed_setups_receipt_or_engineering_test CHECK(
 (receipt_id IS NULL)=coalesce(record_json->>'selection_policy'
  ='MANAGED_ENGINEERING_ENROLLMENT_V1',false)
 AND (receipt_id IS NULL)=coalesce(record_json->>'purpose'='ENGINEERING_TEST',false)
 AND (receipt_id IS NOT NULL OR market='CRYPTO'));
-- One setup per enrollment, as UNIQUE(receipt_id) gives one setup per Jev receipt.
CREATE UNIQUE INDEX managed_setups_one_per_engineering_enrollment ON lab.managed_setups
 ((record_json->>'enrollment_event_seq')) WHERE receipt_id IS NULL;

-- Levels exactly as admission checks them: exact decimals, 0 < S < T <= M < P and reward/risk
-- of at least 2 at the maximum entry (P - M >= 2 * (M - S)).
CREATE FUNCTION lab.managed_engineering_levels_failure(levels jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
DECLARE t numeric; m numeric; s numeric; p numeric;
BEGIN
 IF jsonb_typeof(levels) IS DISTINCT FROM 'object' THEN
  RETURN 'ENGINEERING_LEVELS_INVALID';
 END IF;
 IF NOT levels ?& ARRAY['entry_trigger','max_entry_price','stop','target']
 OR levels-ARRAY['entry_trigger','max_entry_price','stop','target']<>'{}'::jsonb
 OR EXISTS(SELECT 1 FROM jsonb_each(levels) e WHERE jsonb_typeof(e.value)<>'string'
   OR NOT (e.value#>>'{}') ~ '^[0-9]+(\.[0-9]+)?$')
 THEN RETURN 'ENGINEERING_LEVELS_INVALID'; END IF;
 t:=(levels->>'entry_trigger')::numeric; m:=(levels->>'max_entry_price')::numeric;
 s:=(levels->>'stop')::numeric; p:=(levels->>'target')::numeric;
 IF NOT (0<s AND s<t AND t<=m AND m<p) THEN RETURN 'ENGINEERING_LEVELS_INVALID'; END IF;
 IF p-m<2*(m-s) THEN RETURN 'MIN_REWARD_RISK'; END IF;
 RETURN NULL;
END $$;

-- Every enrollment with its packet, setup and admission outcome. An enrollment is active while
-- its setup is not terminal, or, before admission, until it expires or admission refuses it
-- for good (the runtime's RESEARCH_ADMISSION_DECLINED key, or the final crypto refusal key
-- ManagedExecution records for an engineering packet).
CREATE FUNCTION lab.managed_engineering_enrollments() RETURNS TABLE(
 enrollment_event_seq bigint, selection_event_seq bigint, packet_id uuid, signal_id text,
 symbol text, levels jsonb, expires_at timestamptz, enrolled_at timestamptz,
 operator_role text, reason text, grid_check text, setup_id uuid, setup_state text, arm text,
 risk_policy_id text, final_refusal text, active boolean)
LANGUAGE sql VOLATILE SET search_path=pg_catalog,lab SET TimeZone='UTC' AS $$
 SELECT e.event_seq,s.event_seq,s.event_id,e.body->>'signal_id',e.body->>'symbol',
  e.body->'levels',(e.body->>'expires_at')::timestamptz,e.recorded_at,e.body->>'operator_role',
  e.body->>'reason',e.body->>'grid_check',m.setup_id,t.body->>'state',t.body->>'arm',
  t.body->>'risk_policy_id',coalesce(d.body->>'reason',c.body->>'reason'),
  CASE WHEN m.setup_id IS NOT NULL THEN coalesce(t.body->>'state','')<>ALL(ARRAY['CLOSED',
   'INVALIDATED','EXPIRED_UNTRIGGERED','RISK_REJECTED','REJECTED'])
  ELSE clock_timestamp()<(e.body->>'expires_at')::timestamptz AND d.event_id IS NULL
   AND c.event_id IS NULL END
 FROM lab.managed_events e
 LEFT JOIN lab.managed_events s ON s.idempotency_key='managed-engineering-selected:'||e.event_seq
 LEFT JOIN lab.managed_setups m ON m.receipt_id IS NULL
  AND m.record_json->>'enrollment_event_seq'=e.event_seq::text
 LEFT JOIN lab.managed_states t ON t.setup_id=m.setup_id
 LEFT JOIN lab.managed_events d ON d.idempotency_key='research:admission-declined:'||s.event_seq
 LEFT JOIN lab.managed_events c
  ON c.idempotency_key='crypto-admission-refused:engineering:'||e.event_seq
 WHERE e.kind='MANAGED_ENGINEERING_ENROLLED' ORDER BY e.event_seq
$$;

-- The engineering branch of lab.managed_review_failure: NULL, or the first failing binding.
-- The packet must be the exact audited RESEARCH_SELECTED packet the operator function wrote
-- for an audited enrollment by catalyst_operator, name the same signal, symbol, levels, cycle
-- and expiry, carry no Jev receipt, be unexpired, and no other engineering setup may be active
-- (one engineering order at a time).
CREATE FUNCTION lab.managed_engineering_failure(packet jsonb) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,lab SET TimeZone='UTC' AS $$
DECLARE selected lab.managed_events; enrolled lab.managed_events; failure text;
BEGIN
 SELECT * INTO selected FROM lab.managed_events
  WHERE event_seq=(packet->>'selection_event_seq')::bigint;
 IF selected.event_id IS NULL OR selected.kind<>'RESEARCH_SELECTED'
 OR selected.setup_id IS NOT NULL
 OR selected.body->'packet' IS DISTINCT FROM packet-'selection_event_seq'
 OR NOT lab.research_audit_matches('MANAGED_EVENTS',to_jsonb(selected),selected.event_seq)
 THEN RETURN 'SELECTION_INTEGRITY_FAILURE'; END IF;
 SELECT * INTO enrolled FROM lab.managed_events
  WHERE event_seq=(packet->>'enrollment_event_seq')::bigint;
 IF enrolled.event_id IS NULL OR enrolled.kind<>'MANAGED_ENGINEERING_ENROLLED'
 OR enrolled.setup_id IS NOT NULL
 OR enrolled.idempotency_key IS DISTINCT FROM
    'managed-engineering-enrollment:'||(enrolled.body->>'signal_id')
 OR selected.idempotency_key IS DISTINCT FROM 'managed-engineering-selected:'||enrolled.event_seq
 OR NOT lab.research_audit_matches('MANAGED_EVENTS',to_jsonb(enrolled),enrolled.event_seq)
 OR enrolled.body->>'operator_role' IS DISTINCT FROM 'catalyst_operator'
 OR enrolled.body->>'selection_policy' IS DISTINCT FROM 'MANAGED_ENGINEERING_ENROLLMENT_V1'
 OR packet->>'purpose' IS DISTINCT FROM 'ENGINEERING_TEST'
 OR packet->>'execution_scope' IS DISTINCT FROM 'PAPER_ONLY'
 OR packet->>'market' IS DISTINCT FROM 'CRYPTO'
 OR packet->'receipt_id' IS DISTINCT FROM 'null'::jsonb
 OR packet ? 'quality_receipt_id'
 OR left(packet->>'signal_id',5) IS DISTINCT FROM 'TEST-'
 OR packet->>'signal_id' IS DISTINCT FROM enrolled.body->>'signal_id'
 OR packet->>'symbol' IS DISTINCT FROM enrolled.body->>'symbol'
 OR packet->'levels' IS DISTINCT FROM enrolled.body->'levels'
 OR packet->>'cycle_id' IS DISTINCT FROM enrolled.body->>'cycle_id'
 OR packet->>'expires_at' IS DISTINCT FROM enrolled.body->>'expires_at'
 THEN RETURN 'ENGINEERING_ENROLLMENT_BINDING_FAILURE'; END IF;
 failure:=lab.managed_engineering_levels_failure(packet->'levels');
 IF failure IS NOT NULL THEN RETURN failure; END IF;
 IF clock_timestamp()>=(packet->>'expires_at')::timestamptz
 THEN RETURN 'ENGINEERING_ENROLLMENT_EXPIRED'; END IF;
 IF EXISTS(SELECT 1 FROM lab.managed_setups x LEFT JOIN lab.managed_states t USING(setup_id)
   WHERE x.receipt_id IS NULL
   AND x.record_json->>'enrollment_event_seq' IS DISTINCT FROM packet->>'enrollment_event_seq'
   AND coalesce(t.body->>'state','')<>ALL(ARRAY['CLOSED','INVALIDATED','EXPIRED_UNTRIGGERED',
    'RISK_REJECTED','REJECTED']))
 THEN RETURN 'ENGINEERING_TEST_ALREADY_ACTIVE'; END IF;
 RETURN NULL;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range
 OR invalid_datetime_format OR datetime_field_overflow THEN
 RETURN 'ENGINEERING_ENROLLMENT_BINDING_FAILURE';
END $$;

-- Migration 014's body follows the inserted engineering branch unchanged, statement for
-- statement; tests/test_managed_engineering.py compares it with 014's text.
CREATE OR REPLACE FUNCTION lab.managed_review_failure(packet jsonb) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE failure text; receipt lab.jev_receipts; request lab.jev_requests;
        latest lab.managed_events; quality lab.managed_events; answers jsonb;
        score numeric:=0; field text;
BEGIN
 IF packet->>'selection_policy'='MANAGED_ENGINEERING_ENROLLMENT_V1' THEN
  RETURN lab.managed_engineering_failure(packet);
 END IF;
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

-- Only the operator enrollment function writes an enrollment or an engineering packet: a
-- catalyst_operator session has no table grants, so it reaches lab.managed_events only through
-- that SECURITY DEFINER function. The app's own roles (catalyst_risk inserts managed events;
-- catalyst_app, catalyst_review and catalyst_jev cannot) and any other login are refused.
CREATE FUNCTION lab.guard_engineering_enrollment_event() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
BEGIN
 IF (NEW.kind='MANAGED_ENGINEERING_ENROLLED'
     OR coalesce(NEW.body->'packet'->>'selection_policy','')='MANAGED_ENGINEERING_ENROLLMENT_V1'
     OR coalesce(NEW.body->'packet'->>'purpose','')='ENGINEERING_TEST')
 AND session_user::text IS DISTINCT FROM 'catalyst_operator' THEN
  RAISE EXCEPTION 'ENGINEERING_ENROLLMENT_OPERATOR_ONLY';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_engineering_enrollment BEFORE INSERT ON lab.managed_events
 FOR EACH ROW WHEN (NEW.kind='MANAGED_ENGINEERING_ENROLLED' OR NEW.body ? 'packet')
 EXECUTE FUNCTION lab.guard_engineering_enrollment_event();

-- A receipt-free setup must be the exact packet of a valid, unexpired enrollment, and its
-- columns must repeat that packet (Jev-reviewed setups are bound by their receipt instead).
CREATE FUNCTION lab.guard_engineering_setup() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
DECLARE failure text;
BEGIN
 IF NEW.receipt_id IS NOT NULL THEN RETURN NEW; END IF;
 PERFORM pg_advisory_xact_lock(719172026);
 IF NEW.record_json->>'selection_policy' IS DISTINCT FROM 'MANAGED_ENGINEERING_ENROLLMENT_V1'
 THEN RAISE EXCEPTION 'ENGINEERING_ENROLLMENT_REQUIRED'; END IF;
 failure:=lab.managed_review_failure(NEW.record_json);
 IF failure IS NOT NULL THEN RAISE EXCEPTION '%',failure; END IF;
 IF NEW.market IS DISTINCT FROM 'CRYPTO'
 OR NEW.strategy_version IS DISTINCT FROM 'CRYPTO_STRUCTURAL_RETEST_TEST_V1'
 OR NEW.symbol IS DISTINCT FROM NEW.record_json->>'symbol'
 OR NEW.cycle_id::text IS DISTINCT FROM NEW.record_json->>'cycle_id'
 OR NEW.revision::text IS DISTINCT FROM NEW.record_json->>'revision'
 OR NEW.evidence_hash IS DISTINCT FROM NEW.record_json->>'evidence_hash'
 OR NEW.expires_at IS DISTINCT FROM (NEW.record_json->>'expires_at')::timestamptz
 THEN RAISE EXCEPTION 'ENGINEERING_SETUP_BINDING_FAILURE'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_engineering_setup BEFORE INSERT ON lab.managed_setups
 FOR EACH ROW WHEN (NEW.receipt_id IS NULL)
 EXECUTE FUNCTION lab.guard_engineering_setup();

-- Operator step (risk-increasing, like a halt release): a substantive reason, TEST- signal,
-- a crypto symbol with a server classification, admission's levels rule, recorded broker price
-- grid when one exists (else admission checks the live grid), no halt or pending exit, and no
-- other active enrollment or managed setup on the symbol. Returns the two sequences.
CREATE FUNCTION lab.operator_enroll_managed_engineering(test_signal text, crypto_symbol text,
 trigger_price numeric, max_entry numeric, stop_price numeric, target_price numeric,
 expires_minutes integer, reason text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab SET TimeZone='UTC' AS $$
DECLARE note text:=btrim(coalesce(reason,'')); failure text; levels jsonb; expires timestamptz;
 increment numeric; grid text:='DEFERRED_TO_ADMISSION_LIVE_BROKER_CHECK'; name text;
 classification record; cycle uuid:=gen_random_uuid(); body jsonb; packet jsonb;
 enrolled lab.managed_events; selected lab.managed_events;
BEGIN
 IF session_user::text IS DISTINCT FROM 'catalyst_operator' THEN
  RAISE EXCEPTION 'ENGINEERING_ENROLLMENT_OPERATOR_ONLY';
 END IF;
 PERFORM pg_advisory_xact_lock(719172026);
 failure:=lab.operator_reason_failure(reason,10);
 IF failure IS NOT NULL THEN RAISE EXCEPTION '%',failure; END IF;
 IF coalesce(test_signal,'') !~ '^TEST-[A-Z0-9][A-Z0-9-]{2,59}$' THEN
  RAISE EXCEPTION 'ENGINEERING_SIGNAL_ID_INVALID';
 END IF;
 IF coalesce(crypto_symbol,'') !~ '^[A-Z0-9]{1,16}/USD$' THEN
  RAISE EXCEPTION 'ENGINEERING_ENROLLMENT_CRYPTO_ONLY';
 END IF;
 IF expires_minutes IS NULL OR expires_minutes NOT BETWEEN 1 AND 240 THEN
  RAISE EXCEPTION 'ENGINEERING_EXPIRY_INVALID';
 END IF;
 levels:=jsonb_build_object('entry_trigger',trigger_price::text,'max_entry_price',
  max_entry::text,'stop',stop_price::text,'target',target_price::text);
 failure:=lab.managed_engineering_levels_failure(levels);
 IF failure IS NOT NULL THEN RAISE EXCEPTION '%',failure; END IF;
 IF EXISTS(SELECT 1 FROM lab.execution_halts) THEN RAISE EXCEPTION 'RISK_HALT'; END IF;
 IF EXISTS(SELECT 1 FROM lab.daily_risk_halts
   WHERE session_date=(clock_timestamp() AT TIME ZONE 'America/New_York')::date)
 THEN RAISE EXCEPTION 'DAILY_RISK_HALT'; END IF;
 IF EXISTS(SELECT 1 FROM lab.pending_risk_exits) THEN
  RAISE EXCEPTION 'ACCOUNT_EXIT_PENDING';
 END IF;
 SELECT * INTO classification FROM lab.current_classifications WHERE ticker=crypto_symbol;
 IF classification.ticker IS NULL OR classification.sector IS NULL
 OR classification.theme IS NULL
 OR (classification.sector='CRYPTO' AND classification.theme='CRYPTO_UNLISTED') THEN
  RAISE EXCEPTION 'CORRELATION_UNKNOWN';
 END IF;
 IF EXISTS(SELECT 1 FROM lab.managed_events
   WHERE idempotency_key='managed-engineering-enrollment:'||test_signal) THEN
  RAISE EXCEPTION 'ENGINEERING_SIGNAL_ALREADY_ENROLLED';
 END IF;
 IF EXISTS(SELECT 1 FROM lab.managed_engineering_enrollments() x WHERE x.active) THEN
  RAISE EXCEPTION 'ENGINEERING_TEST_ALREADY_ACTIVE';
 END IF;
 IF EXISTS(SELECT 1 FROM lab.managed_setups x LEFT JOIN lab.managed_states t USING(setup_id)
   WHERE x.market='CRYPTO'
   AND upper(replace(x.symbol,'/',''))=upper(replace(crypto_symbol,'/',''))
   AND coalesce(t.body->>'state','')<>ALL(ARRAY['CLOSED','INVALIDATED','EXPIRED_UNTRIGGERED',
    'RISK_REJECTED','REJECTED'])) THEN
  RAISE EXCEPTION 'ACTIVE_SYMBOL_ALREADY_MANAGED';
 END IF;
 -- Broker price metadata ManagedExecution recorded in the last day (the intake rule).
 SELECT (e.body->>'price_increment')::numeric INTO increment FROM lab.managed_events e
  WHERE e.setup_id IS NULL AND e.kind='CRYPTO_ASSET_METADATA'
  AND upper(replace(e.body->>'symbol','/',''))=upper(replace(crypto_symbol,'/',''))
  AND e.recorded_at>clock_timestamp()-interval '1 day'
  ORDER BY e.event_seq DESC LIMIT 1;
 IF increment>0 THEN
  grid:='RECORDED_BROKER_METADATA';
  FOREACH name IN ARRAY ARRAY['entry_trigger','max_entry_price','stop','target'] LOOP
   IF (levels->>name)::numeric % increment<>0 THEN
    RAISE EXCEPTION 'CRYPTO_LEVEL_OFF_PRICE_GRID';
   END IF;
  END LOOP;
 END IF;
 expires:=date_trunc('second',clock_timestamp())+make_interval(mins=>expires_minutes);
 body:=jsonb_build_object('signal_id',test_signal,'symbol',crypto_symbol,'market','CRYPTO',
  'levels',levels,'expires_at',to_jsonb(expires),'cycle_id',cycle,'reason',note,
  'operator_role',session_user::text,'selection_policy','MANAGED_ENGINEERING_ENROLLMENT_V1',
  'purpose','ENGINEERING_TEST','execution_scope','PAPER_ONLY','grid_check',grid,
  'price_increment',to_jsonb(increment),'jev_review','NONE_ENGINEERING_TEST');
 INSERT INTO lab.managed_events(event_id,setup_id,idempotency_key,kind,body)
 VALUES(gen_random_uuid(),NULL,'managed-engineering-enrollment:'||test_signal,
  'MANAGED_ENGINEERING_ENROLLED',body)
 RETURNING * INTO enrolled;
 packet:=jsonb_build_object('cycle_id',cycle,'item_key','ENGINEERING_TEST:'||test_signal,
  'revision',1,'signal_id',test_signal,'symbol',crypto_symbol,'market','CRYPTO',
  'levels',levels,'thesis','ENGINEERING_TEST: operator-enrolled plumbing verification of '
  ||'the managed paper path (plan 0.10). Not a research thesis and not strategy evidence.',
  'disproof','Not applicable to an engineering test: the stop, target, protection and '
  ||'mechanical exits apply unchanged.','sources','[]'::jsonb,'receipt_id',NULL::text,
  'evidence_hash',encode(public.digest((body-'reason')::text,'sha256'),'hex'),
  'expires_at',to_jsonb(expires),'selection_policy','MANAGED_ENGINEERING_ENROLLMENT_V1',
  'purpose','ENGINEERING_TEST','execution_scope','PAPER_ONLY',
  'enrollment_event_seq',enrolled.event_seq,'jev_review','NONE_ENGINEERING_TEST');
 INSERT INTO lab.managed_events(event_id,setup_id,idempotency_key,kind,body)
 VALUES(gen_random_uuid(),NULL,'managed-engineering-selected:'||enrolled.event_seq,
  'RESEARCH_SELECTED',jsonb_build_object('packet',packet))
 RETURNING * INTO selected;
 RETURN jsonb_build_object('enrollment_event_seq',enrolled.event_seq,
  'selection_event_seq',selected.event_seq,'packet_id',selected.event_id,
  'signal_id',test_signal,'symbol',crypto_symbol,'levels',levels,'expires_at',to_jsonb(expires),
  'cycle_id',cycle,'grid_check',grid,'operator_role',session_user::text,
  'purpose','ENGINEERING_TEST','execution_scope','PAPER_ONLY','jev_review',
  'NONE_ENGINEERING_TEST');
END $$;

CREATE FUNCTION lab.operator_managed_engineering_status() RETURNS TABLE(
 enrollment_event_seq bigint, selection_event_seq bigint, packet_id uuid, signal_id text,
 symbol text, levels jsonb, expires_at timestamptz, enrolled_at timestamptz,
 operator_role text, reason text, grid_check text, setup_id uuid, setup_state text, arm text,
 risk_policy_id text, final_refusal text, active boolean)
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,lab SET TimeZone='UTC' AS $$
 SELECT * FROM lab.managed_engineering_enrollments()
$$;

REVOKE ALL ON FUNCTION lab.managed_engineering_levels_failure(jsonb),
 lab.managed_engineering_enrollments(),lab.managed_engineering_failure(jsonb),
 lab.guard_engineering_enrollment_event(),lab.guard_engineering_setup(),
 lab.operator_enroll_managed_engineering(text,text,numeric,numeric,numeric,numeric,integer,text),
 lab.operator_managed_engineering_status() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
 lab.operator_enroll_managed_engineering(text,text,numeric,numeric,numeric,numeric,integer,text),
 lab.operator_managed_engineering_status() TO catalyst_operator;
INSERT INTO lab.schema_migrations(version) VALUES(17);
