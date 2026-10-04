-- Account-risk policy JEV_MANAGED_RISK_V4 (docs/TRADING-QUALITY-PLAN.md phase A, item A1; owner
-- approval 2026-10-02 "A1-A5: yes"; docs/packages/risk-pacing.md). V3's numbers except the crypto
-- open-risk cap, 2% of equity (from 5%): every open CRYPTO reservation counts toward that one cap,
-- so all coins (alts, Bitcoin and Ether alike) are one cluster for it. The daily loss limits move
-- into the policy's definition: a hard limit of 3% (cancel and flatten, unchanged behaviour; under
-- earlier policies it stays the engine's literal) and a new soft limit of 2% (no new entries).
--
-- The pattern of migration 022: three audited, immutable inserts and no other row (the V4 policy
-- row, its CRYPTO terms, its daily limits). No audited table gains a column (the limits live in a
-- new table), no historical row or event is rewritten, every earlier policy row keeps its bytes
-- and no SQL function is replaced: lab.account_risk_failure (022) already sums every open CRYPTO
-- reservation against the row's CRYPTO cap, and lab.slice_sizing_failure reads V4's terms as it
-- reads V3's, so the Python mirror (account_risk.slice_size) needs no change either.

-- A MANAGED policy's daily loss limits, as fractions of the New York day's starting equity
-- (lab.risk_sessions.day_start_equity): at hard_loss_pct of realized plus unrealized loss the
-- engine records the daily halt and cancels and flattens; at soft_loss_pct it admits no new
-- entry for the rest of that day while every open position keeps its protection.
CREATE TABLE lab.account_risk_daily_limits (
 policy_id text PRIMARY KEY REFERENCES lab.account_risk_policies,
 hard_loss_pct numeric NOT NULL CHECK(hard_loss_pct>0 AND hard_loss_pct<=0.1),
 hard_action text NOT NULL CHECK(hard_action='CANCEL_AND_FLATTEN'),
 soft_loss_pct numeric CHECK(soft_loss_pct IS NULL OR (soft_loss_pct>0
  AND soft_loss_pct<hard_loss_pct)),
 soft_action text,
 CHECK((soft_loss_pct IS NULL AND soft_action IS NULL)
  OR (soft_loss_pct IS NOT NULL AND soft_action IS NOT DISTINCT FROM 'NO_NEW_ENTRIES')),
 owner_ruling_ref text NOT NULL CHECK(length(btrim(owner_ruling_ref)) BETWEEN 3 AND 300),
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);

-- Like a policy's market terms (022), its daily limits are part of its definition: written in the
-- transaction that inserts the MANAGED policy row itself, so no existing policy and no setup
-- admitted under one can have its rules changed by a row added afterwards.
CREATE FUNCTION lab.guard_account_risk_daily_limits() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM lab.account_risk_policies p WHERE p.policy_id=NEW.policy_id
   AND p.engine='MANAGED' AND p.xmin::text=(txid_current()%4294967296)::text) THEN
  RAISE EXCEPTION 'DAILY_LIMITS_REQUIRE_NEW_MANAGED_POLICY';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_daily_limits BEFORE INSERT ON lab.account_risk_daily_limits
 FOR EACH ROW EXECUTE FUNCTION lab.guard_account_risk_daily_limits();
CREATE TRIGGER audit_jev BEFORE INSERT ON lab.account_risk_daily_limits
 FOR EACH ROW EXECUTE FUNCTION lab.audit_jev_row();
CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON lab.account_risk_daily_limits
 FOR EACH ROW EXECUTE FUNCTION lab.reject_mutation();
CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON lab.account_risk_daily_limits
 FOR EACH STATEMENT EXECUTE FUNCTION lab.reject_mutation();

-- JEV_MANAGED_RISK_V4: V3's row (0.5% risk per trade, 5% account cap, US 3%, forex 0, two per
-- US sector and one per theme, the 2x intraday multiple, the 60-second capacity cooldown, the
-- 30% fixed-exit arm) with the CRYPTO cap at 2%.
INSERT INTO lab.account_risk_policies(policy_id,engine,risk_pct,account_cap_pct,market_caps,
 max_per_sector,max_per_theme,sector_limited_markets,leverage_allowed,
 intraday_buying_power_multiple,capacity_cooldown_seconds,fixed_exit_arm_pct,owner_ruling_ref)
VALUES('JEV_MANAGED_RISK_V4','MANAGED',0.005,0.05,'{"US_STOCKS":0.03,"CRYPTO":0.02,"FOREX":0}',
 2,1,'{US_STOCKS}',true,2,60,30,'TRADING-QUALITY-PLAN A1, owner approval 2026-10-02; risk-pacing');
-- V3's CRYPTO terms exactly: 10% equity slices, three open trades per sector, stops >= 2% below M.
INSERT INTO lab.account_risk_market_terms(policy_id,market,sizing_method,notional_pct,
 max_per_theme,min_stop_fraction,owner_ruling_ref)
VALUES('JEV_MANAGED_RISK_V4','CRYPTO','EQUITY_SLICE_RISK_CAPPED_V1',0.10,3,0.02,
 'TRADING-QUALITY-PLAN A1 (V3 terms kept), owner approval 2026-10-02; risk-pacing');
INSERT INTO lab.account_risk_daily_limits(policy_id,hard_loss_pct,hard_action,soft_loss_pct,
 soft_action,owner_ruling_ref)
VALUES('JEV_MANAGED_RISK_V4',0.03,'CANCEL_AND_FLATTEN',0.02,'NO_NEW_ENTRIES',
 'TRADING-QUALITY-PLAN A1, owner approval 2026-10-02; risk-pacing');

REVOKE ALL ON lab.account_risk_daily_limits FROM PUBLIC;
GRANT SELECT ON lab.account_risk_daily_limits TO catalyst_app,catalyst_review;
REVOKE ALL ON FUNCTION lab.guard_account_risk_daily_limits() FROM PUBLIC;
INSERT INTO lab.schema_migrations(version) VALUES(26);
