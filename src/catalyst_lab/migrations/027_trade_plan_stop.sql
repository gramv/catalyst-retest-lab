-- CRYPTO_TRADE_PLAN_V1's planned stop in the reservation guard (docs/TRADING-QUALITY-PLAN.md
-- phase A, item A3; owner approval 2026-10-02 "A1-A5: yes" and "ok go ahead with migration 027";
-- docs/packages/trade-plan.md). A report-V3 crypto setup admitted under the trade plan records
-- its plan in its admission state: a stop no tighter than two hourly ranges, sized to the same
-- dollar risk. Its reservation's planned risk is qty x (max entry - that stop); migration 022's
-- guard read the packet's (research) stop, so it refused every planned reservation.
--
-- No row, table or audited value changes: one new function, and migration 022's
-- lab.guard_managed_reservation() (026 did not replace it) with each of its three reads of the
-- packet's stop going through that function. For every setup without a trade plan the function
-- returns the packet's stop, so every other reservation is checked exactly as before. The engine
-- (ManagedExecution) plans new admissions once this function exists.

-- The stop a setup's reservation is planned on: CRYPTO_TRADE_PLAN_V1's stop recorded in the
-- setup's admission state (its first STATE event), never above the packet's stop, so a plan can
-- only ever reserve more risk than the research stop would; otherwise the packet's stop.
CREATE FUNCTION lab.managed_planned_stop(setup uuid) RETURNS numeric
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
 SELECT CASE
  WHEN a.body->'trade_plan'->'policy'->>'policy_id'='CRYPTO_TRADE_PLAN_V1'
   AND (a.body->'trade_plan'->>'stop')::numeric>0
  THEN least((s.record_json->'levels'->>'stop')::numeric,(a.body->'trade_plan'->>'stop')::numeric)
  ELSE (s.record_json->'levels'->>'stop')::numeric END
 FROM lab.managed_setups s
 LEFT JOIN LATERAL (SELECT e.body FROM lab.managed_events e
  WHERE e.setup_id=s.setup_id AND e.kind='STATE' ORDER BY e.event_seq LIMIT 1) a ON true
 WHERE s.setup_id=$1
$$;
REVOKE ALL ON FUNCTION lab.managed_planned_stop(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.managed_planned_stop(uuid) TO catalyst_risk,catalyst_app;

-- Migration 022's guard; only the packet-stop reads changed (to lab.managed_planned_stop).
CREATE OR REPLACE FUNCTION lab.guard_managed_reservation() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
DECLARE d lab.managed_risk_decisions; s lab.managed_setups; c record;
        p lab.account_risk_policies; multiple numeric:=1; failure text;
BEGIN
 PERFORM pg_advisory_xact_lock(719172026);
 SELECT * INTO d FROM lab.managed_risk_decisions WHERE decision_id=NEW.decision_id;
 SELECT * INTO s FROM lab.managed_setups WHERE setup_id=NEW.setup_id;
 SELECT * INTO c FROM lab.current_classifications WHERE ticker=s.symbol;
 SELECT * INTO p FROM lab.account_risk_policies
  WHERE policy_id=coalesce(d.context->>'risk_policy_id','MUSE_JEV_MANAGED_TEST_V1');
 IF EXISTS(SELECT 1 FROM lab.account_risk_market_terms t
   WHERE t.policy_id=p.policy_id AND t.market=s.market) THEN
  IF current_user<>'catalyst_risk' OR d.outcome IS DISTINCT FROM 'APPROVED'
  OR d.action IS DISTINCT FROM 'ENTRY' OR d.setup_id IS DISTINCT FROM NEW.setup_id
  OR d.transaction_id<>txid_current() OR d.expires_at<=clock_timestamp()
  OR p.engine<>'MANAGED' OR NEW.budget<>NEW.planned_risk
  OR lab.slice_sizing_failure(p.policy_id,s.market,d.equity,NEW.qty,NEW.max_entry,
   lab.managed_planned_stop(s.setup_id)) IS NOT NULL
  OR NEW.sector IS DISTINCT FROM c.sector
  OR NEW.theme IS DISTINCT FROM c.theme OR c.ticker IS NULL
  OR NEW.max_entry<>(s.record_json->'levels'->>'max_entry_price')::numeric
  OR NEW.planned_risk<>NEW.qty*(NEW.max_entry-lab.managed_planned_stop(s.setup_id))
  OR NEW.qty<>(d.payload->>'qty')::numeric
  THEN RAISE EXCEPTION 'INVALID_MANAGED_RISK_RESERVATION'; END IF;
  failure:=lab.account_risk_failure(p.policy_id,coalesce(d.context->>'venue','ALPACA_PAPER'),
   s.market,NEW.sector,NEW.theme,d.equity,NEW.budget);
  IF failure IS NOT NULL THEN RAISE EXCEPTION '%',failure; END IF;
  RETURN NEW;
 END IF;
 IF s.market='US_STOCKS' AND p.leverage_allowed
 AND d.payload->>'time_in_force'='day' AND d.payload->>'order_class'='bracket' THEN
  multiple:=p.intraday_buying_power_multiple;
 END IF;
 IF current_user<>'catalyst_risk' OR d.outcome IS DISTINCT FROM 'APPROVED'
 OR d.action IS DISTINCT FROM 'ENTRY' OR d.setup_id IS DISTINCT FROM NEW.setup_id
 OR d.transaction_id<>txid_current() OR d.expires_at<=clock_timestamp()
 OR p.policy_id IS NULL OR p.engine<>'MANAGED'
 OR NEW.budget<>d.equity*p.risk_pct OR NEW.planned_risk>NEW.budget
 OR NEW.qty*NEW.max_entry>d.equity*multiple OR NEW.sector IS DISTINCT FROM c.sector
 OR NEW.theme IS DISTINCT FROM c.theme OR c.ticker IS NULL
 OR NEW.max_entry<>(s.record_json->'levels'->>'max_entry_price')::numeric
 OR NEW.planned_risk<>NEW.qty*(NEW.max_entry-lab.managed_planned_stop(s.setup_id))
 OR NEW.qty<>(d.payload->>'qty')::numeric
 THEN RAISE EXCEPTION 'INVALID_MANAGED_RISK_RESERVATION'; END IF;
 failure:=lab.account_risk_failure(p.policy_id,coalesce(d.context->>'venue','ALPACA_PAPER'),
  s.market,NEW.sector,NEW.theme,d.equity,NEW.budget);
 IF failure IS NOT NULL THEN RAISE EXCEPTION '%',failure; END IF;
 RETURN NEW;
END $$;

INSERT INTO lab.schema_migrations(version) VALUES(27);
