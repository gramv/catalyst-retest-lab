-- Account-risk policy JEV_MANAGED_RISK_V3 (plan phase 4b; docs/packages/crypto-size-hold.md;
-- owner decision D2 of 2026-09-26 in docs/CRYPTO-AGENT-LOOP.md): crypto trades are sized as
-- equity slices of 10% of current equity, each capped so its planned risk qty x (M - S) is at
-- most 0.5% of equity, with at most 5% of equity open as crypto planned risk and at most three
-- open trades per crypto sector. Two audited, immutable inserts and no other row: the V3 policy
-- row and its CRYPTO market terms. No audited table gains a column (the terms live in a new
-- table), no historical row or event is rewritten, and every earlier policy row keeps its bytes.
--
-- One SQL source of truth, as migration 016: lab.account_risk_failure and the managed
-- reservation trigger read a market's terms from lab.account_risk_market_terms. For policies
-- without terms (CATALYST_RETEST_V1, MUSE_JEV_MANAGED_TEST_V1, JEV_MANAGED_RISK_V2) every answer
-- is unchanged: the theme limit falls back to the row's max_per_theme, the guard's non-slice
-- path is migration 016's statements byte for byte, and lab.enforce_risk_reservation,
-- lab.guard_legacy_shared_budget and lab.stamp_risk_decision are not replaced.

-- A market a MANAGED policy sizes in equity slices (EQUITY_SLICE_RISK_CAPPED_V1): an entry's
-- notional is at most notional_pct x equity, its planned risk qty x (M - S) at most the policy
-- row's risk_pct x equity, its stop at least min_stop_fraction x M below M, and the reservation
-- holds its actual planned risk (budget = planned risk), not a fixed budget. max_per_theme
-- replaces the row's per-theme limit in this market. Only CRYPTO is sliced (cash-only).
CREATE TABLE lab.account_risk_market_terms (
 policy_id text NOT NULL REFERENCES lab.account_risk_policies,
 market text NOT NULL CHECK(market='CRYPTO'),
 sizing_method text NOT NULL CHECK(sizing_method='EQUITY_SLICE_RISK_CAPPED_V1'),
 notional_pct numeric NOT NULL CHECK(notional_pct>0 AND notional_pct<=1),
 max_per_theme integer NOT NULL CHECK(max_per_theme BETWEEN 1 AND 100),
 min_stop_fraction numeric NOT NULL CHECK(min_stop_fraction>=0 AND min_stop_fraction<1),
 owner_ruling_ref text NOT NULL CHECK(length(btrim(owner_ruling_ref)) BETWEEN 3 AND 300),
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq),
 PRIMARY KEY(policy_id,market)
);

-- A policy's market terms are part of its definition: they are written in the transaction that
-- inserts the MANAGED policy row itself, so no existing policy (V1, V2 or any later one) and no
-- setup admitted under one can ever have its rules changed by a terms row added afterwards.
CREATE FUNCTION lab.guard_account_risk_market_terms() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM lab.account_risk_policies p WHERE p.policy_id=NEW.policy_id
   AND p.engine='MANAGED' AND p.xmin::text=(txid_current()%4294967296)::text) THEN
  RAISE EXCEPTION 'MARKET_TERMS_REQUIRE_NEW_MANAGED_POLICY';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_terms BEFORE INSERT ON lab.account_risk_market_terms
 FOR EACH ROW EXECUTE FUNCTION lab.guard_account_risk_market_terms();
CREATE TRIGGER audit_jev BEFORE INSERT ON lab.account_risk_market_terms
 FOR EACH ROW EXECUTE FUNCTION lab.audit_jev_row();
CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON lab.account_risk_market_terms
 FOR EACH ROW EXECUTE FUNCTION lab.reject_mutation();
CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON lab.account_risk_market_terms
 FOR EACH STATEMENT EXECUTE FUNCTION lab.reject_mutation();

-- JEV_MANAGED_RISK_V3 (CONTRACT-RESOLUTIONS.md, package crypto-size-hold 2026-09-27). The row
-- keeps V2's US numbers (0.5% risk budget per trade, 3% US cap, two per sector and one per
-- theme, the 2x intraday multiple for US day positions), the 60-second capacity cooldown and
-- the 30% fixed-exit arm; the crypto cap is 5% of equity and the account cap 5%. Its CRYPTO
-- terms: 10% equity slices, at most three open trades per sector (theme), stops at least 2%
-- below the maximum entry. The -3% daily halt is not a row value; it is unchanged.
INSERT INTO lab.account_risk_policies(policy_id,engine,risk_pct,account_cap_pct,market_caps,
 max_per_sector,max_per_theme,sector_limited_markets,leverage_allowed,
 intraday_buying_power_multiple,capacity_cooldown_seconds,fixed_exit_arm_pct,owner_ruling_ref)
VALUES('JEV_MANAGED_RISK_V3','MANAGED',0.005,0.05,'{"US_STOCKS":0.03,"CRYPTO":0.05,"FOREX":0}',
 2,1,'{US_STOCKS}',true,2,60,30,'CRYPTO-AGENT-LOOP 2026-09-26 D2; crypto-size-hold 2026-09-27');
INSERT INTO lab.account_risk_market_terms(policy_id,market,sizing_method,notional_pct,
 max_per_theme,min_stop_fraction,owner_ruling_ref)
VALUES('JEV_MANAGED_RISK_V3','CRYPTO','EQUITY_SLICE_RISK_CAPPED_V1',0.10,3,0.02,
 'CRYPTO-AGENT-LOOP 2026-09-26 D2 and 4.4; crypto-size-hold 2026-09-27');

-- NULL, or why a slice-sized entry of qty at max entry M with stop S breaks the market terms of
-- the policy. Exact numeric comparisons; the managed reservation trigger asks it, and the
-- engine's sizing (account_risk.slice_size) computes the largest quantity it accepts.
CREATE FUNCTION lab.slice_sizing_failure(policy text, market text, equity numeric, qty numeric,
 max_entry numeric, stop numeric) RETURNS text
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE p lab.account_risk_policies; t lab.account_risk_market_terms;
BEGIN
 SELECT * INTO p FROM lab.account_risk_policies q WHERE q.policy_id=$1;
 SELECT * INTO t FROM lab.account_risk_market_terms q WHERE q.policy_id=$1 AND q.market=$2;
 IF p.policy_id IS NULL OR t.policy_id IS NULL OR p.engine<>'MANAGED'
 THEN RETURN 'SLICE_TERMS_UNKNOWN'; END IF;
 IF $3 IS NULL OR $3<=0 OR $4 IS NULL OR $4<=0 OR $5 IS NULL OR $6 IS NULL OR $6<=0 OR $6>=$5
 THEN RETURN 'INVALID_SLICE_SIZING_INPUT'; END IF;
 IF $5-$6<t.min_stop_fraction*$5 THEN RETURN 'STOP_DISTANCE_BELOW_MINIMUM'; END IF;
 IF $4*$5>t.notional_pct*$3 THEN RETURN 'SLICE_NOTIONAL_EXCEEDED'; END IF;
 IF $4*($5-$6)>p.risk_pct*$3 THEN RETURN 'SLICE_RISK_EXCEEDED'; END IF;
 RETURN NULL;
END $$;

-- Migration 016's check with one change: a policy's per-theme limit in a market is its market
-- terms' max_per_theme when it has terms there, else the row's max_per_theme (as before). The
-- stricter-of rule is unchanged: every open same-theme reservation contributes the limit of its
-- own policy in its own market. A slice-sized reservation's budget is its planned risk, so the
-- caps sum what is actually at risk. Policies without terms answer exactly as under 016.
CREATE OR REPLACE FUNCTION lab.account_risk_failure(policy text, venue text, market text,
 sector text, theme text, equity numeric, budget numeric) RETURNS text
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE p lab.account_risk_policies; sector_count integer; theme_count integer;
        sector_limit integer; theme_limit integer; total numeric; market_total numeric;
        unknown integer; cap numeric; own_theme_limit integer;
BEGIN
 SELECT * INTO p FROM lab.account_risk_policies q WHERE q.policy_id=$1;
 IF p.policy_id IS NULL THEN RETURN 'RISK_POLICY_UNKNOWN'; END IF;
 IF $2 IS DISTINCT FROM 'ALPACA_PAPER' OR $3 IS NULL OR $3 NOT IN ('US_STOCKS','CRYPTO','FOREX')
 OR $6 IS NULL OR $6<=0 OR $7 IS NULL OR $7<=0 THEN RETURN 'INVALID_ACCOUNT_RISK_INPUT'; END IF;
 IF $4 IS NULL OR $5 IS NULL OR ($4='CRYPTO' AND $5='CRYPTO_UNLISTED')
 THEN RETURN 'CORRELATION_UNKNOWN'; END IF;
 SELECT count(*) FILTER(WHERE r.sector=$4),count(*) FILTER(WHERE r.theme=$5),
  min(q.max_per_sector) FILTER(WHERE r.sector=$4 AND r.market=ANY(q.sector_limited_markets)),
  min(coalesce(t.max_per_theme,q.max_per_theme)) FILTER(WHERE r.theme=$5),
  coalesce(sum(r.budget),0),
  coalesce(sum(r.budget) FILTER(WHERE r.market=$3),0),count(*) FILTER(WHERE q.policy_id IS NULL)
 INTO sector_count,theme_count,sector_limit,theme_limit,total,market_total,unknown
 FROM lab.account_risk_reservations r LEFT JOIN lab.account_risk_policies q
  ON q.policy_id=r.policy_id LEFT JOIN lab.account_risk_market_terms t
  ON t.policy_id=r.policy_id AND t.market=r.market WHERE r.venue=$2;
 IF unknown>0 THEN RETURN 'RISK_POLICY_UNKNOWN'; END IF;
 IF $3=ANY(p.sector_limited_markets) THEN
  sector_limit:=least(sector_limit,p.max_per_sector);
 END IF;
 SELECT coalesce((SELECT t.max_per_theme FROM lab.account_risk_market_terms t
  WHERE t.policy_id=p.policy_id AND t.market=$3),p.max_per_theme) INTO own_theme_limit;
 IF sector_count>=sector_limit OR theme_count>=least(theme_limit,own_theme_limit)
 THEN RETURN 'CORRELATION_LIMIT'; END IF;
 IF total+$7>p.account_cap_pct*$6 THEN RETURN 'MAX_OPEN_PLANNED_RISK'; END IF;
 cap:=CASE WHEN jsonb_typeof(p.market_caps->$3)='number'
  THEN ((p.market_caps->$3)::text)::numeric ELSE 0 END;
 IF market_total+$7>cap*$6 THEN RETURN 'MARKET_RISK_CAP'; END IF;
 RETURN NULL;
END $$;

-- Migration 016's trigger with one branch in front of its checks: a setup in a market its
-- decision's policy sizes in equity slices is checked against those terms (the reservation
-- holds its actual planned risk, its size passes lab.slice_sizing_failure) and the one account
-- check. Every other reservation runs migration 016's statements byte for byte.
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
   (s.record_json->'levels'->>'stop')::numeric) IS NOT NULL
  OR NEW.sector IS DISTINCT FROM c.sector
  OR NEW.theme IS DISTINCT FROM c.theme OR c.ticker IS NULL
  OR NEW.max_entry<>(s.record_json->'levels'->>'max_entry_price')::numeric
  OR NEW.planned_risk<>NEW.qty*(NEW.max_entry-(s.record_json->'levels'->>'stop')::numeric)
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
 OR NEW.planned_risk<>NEW.qty*(NEW.max_entry-(s.record_json->'levels'->>'stop')::numeric)
 OR NEW.qty<>(d.payload->>'qty')::numeric
 THEN RAISE EXCEPTION 'INVALID_MANAGED_RISK_RESERVATION'; END IF;
 failure:=lab.account_risk_failure(p.policy_id,coalesce(d.context->>'venue','ALPACA_PAPER'),
  s.market,NEW.sector,NEW.theme,d.equity,NEW.budget);
 IF failure IS NOT NULL THEN RAISE EXCEPTION '%',failure; END IF;
 RETURN NEW;
END $$;

REVOKE ALL ON lab.account_risk_market_terms FROM PUBLIC;
GRANT SELECT ON lab.account_risk_market_terms TO catalyst_app,catalyst_review;
REVOKE ALL ON FUNCTION lab.guard_account_risk_market_terms(),
 lab.slice_sizing_failure(text,text,numeric,numeric,numeric,numeric) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.slice_sizing_failure(text,text,numeric,numeric,numeric,numeric)
 TO catalyst_risk;
INSERT INTO lab.schema_migrations(version) VALUES(22);
