-- Package plugin-c3 (docs/TRADING-QUALITY-PLAN.md section 12 item 3; docs/packages/plugin-c3.md):
-- STRATEGY_PAPER_PATH_V1 (a promoted mechanical strategy plug-in's live signal admitted through
-- the same admission, system check, trade plan, pacing, risk gate and order path as a research
-- pick) and the account-risk policy JEV_MANAGED_RISK_V5 (V4 plus a per-strategy open-risk cap).
-- Everything is default-off: no setup is admitted under the new selection policy until the owner
-- promotes a strategy (an audited STRATEGY_PROMOTION event) AND lists it in the runtime's
-- MANAGED_STRATEGIES_JSON, and no setup is sized under V5 until MANAGED_RISK_POLICY_ID names it.
--
-- What changes, and what does not:
-- * Four audited, immutable inserts (the pattern of migrations 022 and 026): the V5 policy row,
--   its CRYPTO terms and daily limits (V4's exactly) and its one strategy cap (MECHANICAL 0.5%).
--   Every earlier policy row keeps its bytes; V4 has no strategy-cap row, so a V4 setup is
--   checked exactly as before.
-- * One new table, lab.account_risk_strategy_caps (written only with a new MANAGED policy row, as
--   026's daily limits), one new check (lab.strategy_risk_failure) and one new reservation
--   trigger that asks it. lab.account_risk_failure and lab.guard_managed_reservation are not
--   replaced: for every policy without a strategy-cap row the new check answers NULL.
-- * The receipt-free setup rule of migration 017 admits a second receipt-free selection policy,
--   STRATEGY_SIGNAL_SELECTION_V1 (no Jev receipt exists for a mechanical signal). The check is
--   restated with the same meaning for every existing row; engineering setups keep their rule.
--   lab.guard_engineering_setup() gains a strategy branch in front; its engineering statements
--   are unchanged.
-- * lab.managed_review_failure (030's dispatcher) gains one branch in front: a strategy packet is
--   checked by lab.managed_review_failure_strategy; every other packet takes 030's statements,
--   byte for byte.
-- No audited table gains a column and no historical row or event is rewritten.

-- --- JEV_MANAGED_RISK_V5: per-strategy open-risk caps -----------------------------------------

-- A MANAGED policy's open-risk cap per strategy, by the strategy's source: every open
-- reservation of one strategy (a setup's strategy_id; PULLBACK_V1 when it names none) counts
-- toward cap_pct x equity, separately for each strategy of that source. A source without a row
-- has no per-strategy cap (only the policy's market and account caps).
CREATE TABLE lab.account_risk_strategy_caps (
 policy_id text NOT NULL REFERENCES lab.account_risk_policies,
 strategy_source text NOT NULL CHECK(strategy_source IN ('MECHANICAL','RESEARCH_REPORT')),
 cap_pct numeric NOT NULL CHECK(cap_pct>0 AND cap_pct<=0.1),
 owner_ruling_ref text NOT NULL CHECK(length(btrim(owner_ruling_ref)) BETWEEN 3 AND 300),
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq),
 PRIMARY KEY(policy_id,strategy_source)
);

-- Like a policy's market terms (022) and daily limits (026), its strategy caps are part of its
-- definition: written in the transaction that inserts the MANAGED policy row itself.
CREATE FUNCTION lab.guard_account_risk_strategy_caps() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM lab.account_risk_policies p WHERE p.policy_id=NEW.policy_id
   AND p.engine='MANAGED' AND p.xmin::text=(txid_current()%4294967296)::text) THEN
  RAISE EXCEPTION 'STRATEGY_CAPS_REQUIRE_NEW_MANAGED_POLICY';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_strategy_caps BEFORE INSERT ON lab.account_risk_strategy_caps
 FOR EACH ROW EXECUTE FUNCTION lab.guard_account_risk_strategy_caps();
CREATE TRIGGER audit_jev BEFORE INSERT ON lab.account_risk_strategy_caps
 FOR EACH ROW EXECUTE FUNCTION lab.audit_jev_row();
CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON lab.account_risk_strategy_caps
 FOR EACH ROW EXECUTE FUNCTION lab.reject_mutation();
CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON lab.account_risk_strategy_caps
 FOR EACH STATEMENT EXECUTE FUNCTION lab.reject_mutation();

-- A setup's strategy and its source, from its admitted packet: a STRATEGY_SIGNAL_SELECTION_V1
-- packet is MECHANICAL; every other packet is a research pick (PULLBACK_V1 without an id).
CREATE FUNCTION lab.managed_setup_strategy(record jsonb) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT coalesce(nullif(record->>'strategy_id',''),'PULLBACK_V1')
$$;
CREATE FUNCTION lab.managed_setup_strategy_source(record jsonb) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT CASE WHEN record->>'selection_policy'='STRATEGY_SIGNAL_SELECTION_V1'
  THEN 'MECHANICAL' ELSE 'RESEARCH_REPORT' END
$$;

-- NULL, or why an entry of the setup ``record`` with planned risk ``budget`` breaks the policy's
-- cap for its strategy: the open reservations of the same strategy on the venue plus this one
-- above cap_pct x equity is STRATEGY_RISK_CAP (exactly the cap passes). The engine
-- (account_risk.strategy_cap_failure) asks the same question before it reserves.
CREATE FUNCTION lab.strategy_risk_failure(policy text, venue text, record jsonb,
 equity numeric, budget numeric) RETURNS text
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE p lab.account_risk_policies; cap numeric; held numeric;
BEGIN
 SELECT * INTO p FROM lab.account_risk_policies q WHERE q.policy_id=$1;
 IF p.policy_id IS NULL THEN RETURN 'RISK_POLICY_UNKNOWN'; END IF;
 SELECT c.cap_pct INTO cap FROM lab.account_risk_strategy_caps c
  WHERE c.policy_id=$1 AND c.strategy_source=lab.managed_setup_strategy_source($3);
 IF cap IS NULL THEN RETURN NULL; END IF;
 IF $2 IS DISTINCT FROM 'ALPACA_PAPER' OR $4 IS NULL OR $4<=0 OR $5 IS NULL OR $5<=0
 THEN RETURN 'INVALID_ACCOUNT_RISK_INPUT'; END IF;
 SELECT coalesce(sum(r.budget),0) INTO held FROM lab.managed_active_reservations r
  JOIN lab.managed_setups s USING(setup_id)
  JOIN lab.managed_risk_decisions d ON d.decision_id=r.decision_id
  WHERE coalesce(d.context->>'venue','ALPACA_PAPER')=$2
  AND lab.managed_setup_strategy(s.record_json)=lab.managed_setup_strategy($3);
 IF held+$5>cap*$4 THEN RETURN 'STRATEGY_RISK_CAP'; END IF;
 RETURN NULL;
END $$;

-- The reservation's own check of the per-strategy cap, after migration 027's guard (triggers fire
-- by name: guard_reservation, then guard_strategy_risk). A decision under a policy without a
-- strategy-cap row passes unchanged.
CREATE FUNCTION lab.guard_strategy_risk_reservation() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
DECLARE d lab.managed_risk_decisions; s lab.managed_setups; failure text;
BEGIN
 SELECT * INTO d FROM lab.managed_risk_decisions WHERE decision_id=NEW.decision_id;
 SELECT * INTO s FROM lab.managed_setups WHERE setup_id=NEW.setup_id;
 failure:=lab.strategy_risk_failure(
  coalesce(d.context->>'risk_policy_id','MUSE_JEV_MANAGED_TEST_V1'),
  coalesce(d.context->>'venue','ALPACA_PAPER'),s.record_json,d.equity,NEW.budget);
 IF failure IS NOT NULL THEN RAISE EXCEPTION '%',failure; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_strategy_risk BEFORE INSERT ON lab.managed_reservations
 FOR EACH ROW EXECUTE FUNCTION lab.guard_strategy_risk_reservation();

-- JEV_MANAGED_RISK_V5: V4's row, terms and daily limits exactly, plus a 0.5% open-risk cap per
-- mechanical strategy. Research picks (PULLBACK_V1) keep V4's rules: no per-strategy cap, inside
-- the 2% crypto cluster cap that every crypto reservation, mechanical or not, counts toward.
INSERT INTO lab.account_risk_policies(policy_id,engine,risk_pct,account_cap_pct,market_caps,
 max_per_sector,max_per_theme,sector_limited_markets,leverage_allowed,
 intraday_buying_power_multiple,capacity_cooldown_seconds,fixed_exit_arm_pct,owner_ruling_ref)
VALUES('JEV_MANAGED_RISK_V5','MANAGED',0.005,0.05,'{"US_STOCKS":0.03,"CRYPTO":0.02,"FOREX":0}',
 2,1,'{US_STOCKS}',true,2,60,30,'TRADING-QUALITY-PLAN 12.3 (V4 numbers kept); plugin-c3');
INSERT INTO lab.account_risk_market_terms(policy_id,market,sizing_method,notional_pct,
 max_per_theme,min_stop_fraction,owner_ruling_ref)
VALUES('JEV_MANAGED_RISK_V5','CRYPTO','EQUITY_SLICE_RISK_CAPPED_V1',0.10,3,0.02,
 'TRADING-QUALITY-PLAN 12.3 (V4 terms kept); plugin-c3');
INSERT INTO lab.account_risk_daily_limits(policy_id,hard_loss_pct,hard_action,soft_loss_pct,
 soft_action,owner_ruling_ref)
VALUES('JEV_MANAGED_RISK_V5',0.03,'CANCEL_AND_FLATTEN',0.02,'NO_NEW_ENTRIES',
 'TRADING-QUALITY-PLAN 12.3 (V4 limits kept); plugin-c3');
INSERT INTO lab.account_risk_strategy_caps(policy_id,strategy_source,cap_pct,owner_ruling_ref)
VALUES('JEV_MANAGED_RISK_V5','MECHANICAL',0.005,
 'TRADING-QUALITY-PLAN 12.3, a mechanical plug-in at most 0.5% open risk; plugin-c3');

-- --- STRATEGY_PAPER_PATH_V1: receipt-free strategy setups ------------------------------------

-- Migration 017's rule with a second receipt-free policy: a setup has no Jev receipt exactly
-- when its packet is an operator engineering enrollment or a promoted strategy's signal; only an
-- engineering enrollment carries the ENGINEERING_TEST purpose; receipt-free setups are crypto.
-- Every existing row satisfies both forms identically.
ALTER TABLE lab.managed_setups DROP CONSTRAINT managed_setups_receipt_or_engineering_test;
ALTER TABLE lab.managed_setups ADD CONSTRAINT managed_setups_receipt_or_receipt_free_policy CHECK(
 (receipt_id IS NULL)=coalesce(record_json->>'selection_policy' IN
  ('MANAGED_ENGINEERING_ENROLLMENT_V1','STRATEGY_SIGNAL_SELECTION_V1'),false)
 AND coalesce(record_json->>'selection_policy'='MANAGED_ENGINEERING_ENROLLMENT_V1',false)
  =coalesce(record_json->>'purpose'='ENGINEERING_TEST',false)
 AND (receipt_id IS NOT NULL OR market='CRYPTO'));
-- One setup per strategy signal, as one per Jev receipt and one per enrollment.
CREATE UNIQUE INDEX managed_setups_one_per_strategy_signal ON lab.managed_setups
 ((record_json->>'signal_event_seq'))
 WHERE receipt_id IS NULL AND record_json->>'selection_policy'='STRATEGY_SIGNAL_SELECTION_V1';

-- A strategy packet is admissible only when:
-- * its RESEARCH_SELECTED event is intact and holds exactly this packet;
-- * its STRATEGY_SIGNAL event is intact, recorded after the promotion and before the selection,
--   and names the same strategy, coin, levels, expiry, reviewed state and promotion;
-- * the promotion is an intact STRATEGY_PROMOTION_V1 of the same strategy with a history-report
--   hash, a shadow summary, the owner's ruling and either the ladder's paper rung or a recorded
--   owner override, and no STRATEGY_DEMOTION of the strategy follows it;
-- * it is unexpired (the signal's marketable entry window).
CREATE FUNCTION lab.managed_review_failure_strategy(packet jsonb) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE selected lab.managed_events; sig lab.managed_events; promo lab.managed_events;
        field text;
BEGIN
 SELECT * INTO selected FROM lab.managed_events
  WHERE event_seq=(packet->>'selection_event_seq')::bigint;
 IF selected.event_id IS NULL OR selected.kind<>'RESEARCH_SELECTED'
 OR selected.setup_id IS NOT NULL
 OR selected.body->'packet' IS DISTINCT FROM packet-'selection_event_seq'
 OR NOT lab.research_audit_matches('MANAGED_EVENTS',to_jsonb(selected),selected.event_seq)
 THEN RETURN 'SELECTION_INTEGRITY_FAILURE'; END IF;
 SELECT * INTO promo FROM lab.managed_events
  WHERE event_seq=(packet->>'promotion_event_seq')::bigint;
 IF promo.event_id IS NULL OR promo.kind<>'STRATEGY_PROMOTION' OR promo.setup_id IS NOT NULL
 OR NOT lab.research_audit_matches('MANAGED_EVENTS',to_jsonb(promo),promo.event_seq)
 OR promo.body->>'version' IS DISTINCT FROM 'STRATEGY_PROMOTION_V1'
 OR promo.body->>'strategy_id' IS DISTINCT FROM packet->>'strategy_id'
 OR promo.body->>'strategy_source' IS DISTINCT FROM 'MECHANICAL'
 OR coalesce(promo.body->>'history_report_sha256','') !~ '^[0-9a-f]{64}$'
 OR jsonb_typeof(promo.body->'shadow_summary') IS DISTINCT FROM 'object'
 OR length(btrim(coalesce(promo.body->>'owner_ruling_ref',''))) NOT BETWEEN 3 AND 300
 OR NOT (promo.body->>'rung' IS NOT DISTINCT FROM 'ELIGIBLE_FOR_OWNER_PAPER_REVIEW'
   OR length(btrim(coalesce(promo.body->>'owner_override_reason','')))>=10)
 THEN RETURN 'STRATEGY_NOT_PROMOTED'; END IF;
 IF EXISTS(SELECT 1 FROM lab.managed_events x WHERE x.kind='STRATEGY_DEMOTION'
   AND x.setup_id IS NULL AND x.body->>'strategy_id'=packet->>'strategy_id'
   AND x.event_seq>promo.event_seq)
 THEN RETURN 'STRATEGY_DEMOTED'; END IF;
 SELECT * INTO sig FROM lab.managed_events
  WHERE event_seq=(packet->>'signal_event_seq')::bigint;
 IF sig.event_id IS NULL OR sig.kind<>'STRATEGY_SIGNAL' OR sig.setup_id IS NOT NULL
 OR NOT lab.research_audit_matches('MANAGED_EVENTS',to_jsonb(sig),sig.event_seq)
 OR sig.event_seq<=promo.event_seq OR sig.event_seq>=selected.event_seq
 OR sig.body->>'path_version' IS DISTINCT FROM 'STRATEGY_PAPER_PATH_V1'
 OR sig.body->>'strategy_id' IS DISTINCT FROM packet->>'strategy_id'
 OR sig.body->>'promotion_event_seq' IS DISTINCT FROM packet->>'promotion_event_seq'
 OR sig.body->>'symbol' IS DISTINCT FROM packet->>'symbol'
 OR sig.body->'levels' IS DISTINCT FROM packet->'levels'
 OR sig.body->'levels' IS DISTINCT FROM packet->'state'->'levels'
 OR sig.body->>'expires_at' IS DISTINCT FROM packet->>'expires_at'
 OR sig.body->'state' IS DISTINCT FROM packet->'state'
 OR sig.body->>'evidence_hash' IS DISTINCT FROM packet->>'evidence_hash'
 OR packet->>'strategy_source' IS DISTINCT FROM 'MECHANICAL'
 OR packet->>'market' IS DISTINCT FROM 'CRYPTO'
 OR packet->>'receipt_id' IS NOT NULL
 THEN RETURN 'STRATEGY_SIGNAL_BINDING_FAILURE'; END IF;
 FOREACH field IN ARRAY ARRAY['thesis','disproof','sources'] LOOP
  IF packet->field IS DISTINCT FROM packet->'state'->field
  THEN RETURN 'STRATEGY_SIGNAL_BINDING_FAILURE'; END IF;
 END LOOP;
 IF (packet->>'expires_at') IS NULL
 OR clock_timestamp()>=(packet->>'expires_at')::timestamptz THEN RETURN 'REVIEW_EXPIRED'; END IF;
 RETURN NULL;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range
 OR invalid_datetime_format OR datetime_field_overflow THEN
 RETURN 'SELECTION_INTEGRITY_FAILURE';
END $$;

-- The dispatcher: migration 030's body with the strategy branch in front. CREATE OR REPLACE keeps
-- the function and its grants; every other packet takes 030's statements, byte for byte.
CREATE OR REPLACE FUNCTION lab.managed_review_failure(packet jsonb) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
BEGIN
 -- Strategy signals: their own branch (lab.managed_review_failure_strategy, above).
 IF packet->>'selection_policy'='STRATEGY_SIGNAL_SELECTION_V1' THEN
  RETURN lab.managed_review_failure_strategy(packet);
 END IF;
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

-- Migration 017's guard with a strategy branch in front: a receipt-free strategy setup must be
-- the exact packet of an admissible strategy selection and its columns must repeat it. The
-- engineering statements that follow are 017's, unchanged.
CREATE OR REPLACE FUNCTION lab.guard_engineering_setup() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
DECLARE failure text;
BEGIN
 IF NEW.receipt_id IS NOT NULL THEN RETURN NEW; END IF;
 PERFORM pg_advisory_xact_lock(719172026);
 IF NEW.record_json->>'selection_policy'='STRATEGY_SIGNAL_SELECTION_V1' THEN
  failure:=lab.managed_review_failure(NEW.record_json);
  IF failure IS NOT NULL THEN RAISE EXCEPTION '%',failure; END IF;
  IF NEW.market IS DISTINCT FROM 'CRYPTO'
  OR NEW.strategy_version IS DISTINCT FROM 'CRYPTO_STRUCTURAL_RETEST_TEST_V1'
  OR NEW.symbol IS DISTINCT FROM NEW.record_json->>'symbol'
  OR NEW.cycle_id::text IS DISTINCT FROM NEW.record_json->>'cycle_id'
  OR NEW.revision::text IS DISTINCT FROM NEW.record_json->>'revision'
  OR NEW.evidence_hash IS DISTINCT FROM NEW.record_json->>'evidence_hash'
  OR NEW.expires_at IS DISTINCT FROM (NEW.record_json->>'expires_at')::timestamptz
  THEN RAISE EXCEPTION 'STRATEGY_SETUP_BINDING_FAILURE'; END IF;
  RETURN NEW;
 END IF;
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

REVOKE ALL ON lab.account_risk_strategy_caps FROM PUBLIC;
GRANT SELECT ON lab.account_risk_strategy_caps TO catalyst_app,catalyst_review;
REVOKE ALL ON FUNCTION lab.managed_setup_strategy(jsonb),
 lab.managed_setup_strategy_source(jsonb),lab.guard_account_risk_strategy_caps(),
 lab.guard_strategy_risk_reservation(),lab.managed_review_failure_strategy(jsonb),
 lab.strategy_risk_failure(text,text,jsonb,numeric,numeric) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.strategy_risk_failure(text,text,jsonb,numeric,numeric),
 lab.managed_setup_strategy(jsonb),lab.managed_setup_strategy_source(jsonb) TO catalyst_risk;
INSERT INTO lab.schema_migrations(version) VALUES(31);
