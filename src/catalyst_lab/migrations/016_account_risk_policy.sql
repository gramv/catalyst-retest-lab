-- Versioned account-risk policies (plan 2.1/2.2), one SQL source of truth for every
-- account-risk check, and the ledger/account binding. New tables and functions, a replaced
-- view and three replaced trigger functions only: no audited table gains a column and no
-- historical row or event is rewritten. Seeding the three policy rows appends their audit
-- events. V1's frozen checks are untouched: lab.stamp_risk_decision() is not replaced, and the
-- replaced lab.enforce_risk_reservation() keeps migration 005's lock, lookups and frozen
-- sizing/request equality block byte for byte; only its correlation/cap clause now asks
-- lab.account_risk_failure(), as the two cross-engine guards do.

CREATE FUNCTION lab.valid_market_caps(caps jsonb, account_cap numeric) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT jsonb_typeof(caps)='object' AND caps<>'{}'::jsonb AND NOT EXISTS(
  SELECT 1 FROM jsonb_each(caps) e
  WHERE e.key NOT IN ('US_STOCKS','CRYPTO','FOREX') OR jsonb_typeof(e.value)<>'number'
   OR (e.value::text)::numeric<0 OR (e.value::text)::numeric>account_cap)
$$;

-- Only migrations and explicit owner steps insert policies; no application role may.
-- A market absent from market_caps has a cap of zero. max_per_sector applies to reservations in
-- sector_limited_markets; max_per_theme applies in every market. capacity_cooldown_seconds NULL
-- keeps capacity rejections terminal; fixed_exit_arm_pct is the randomized FIXED_EXIT share.
CREATE TABLE lab.account_risk_policies (
 policy_id text PRIMARY KEY CHECK(policy_id ~ '^[A-Z][A-Z0-9_]{2,63}$'),
 engine text NOT NULL CHECK(engine IN ('FROZEN_V1','MANAGED')),
 risk_pct numeric NOT NULL,
 account_cap_pct numeric NOT NULL,
 market_caps jsonb NOT NULL,
 max_per_sector integer NOT NULL CHECK(max_per_sector BETWEEN 1 AND 100),
 max_per_theme integer NOT NULL CHECK(max_per_theme BETWEEN 1 AND 100),
 sector_limited_markets text[] NOT NULL
  CHECK(sector_limited_markets <@ ARRAY['US_STOCKS','CRYPTO','FOREX']::text[]),
 leverage_allowed boolean NOT NULL,
 intraday_buying_power_multiple numeric NOT NULL
  CHECK(intraday_buying_power_multiple>=1 AND intraday_buying_power_multiple<=4),
 capacity_cooldown_seconds integer CHECK(capacity_cooldown_seconds BETWEEN 1 AND 3600),
 fixed_exit_arm_pct integer NOT NULL CHECK(fixed_exit_arm_pct BETWEEN 0 AND 100),
 owner_ruling_ref text NOT NULL CHECK(length(btrim(owner_ruling_ref)) BETWEEN 3 AND 300),
 -- No timestamp column: owner steps insert from any session time zone, and the audit event
 -- already records the insertion time independently of it.
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq),
 CHECK(risk_pct>0 AND risk_pct<=account_cap_pct AND account_cap_pct<1),
 CHECK(lab.valid_market_caps(market_caps,account_cap_pct)),
 CHECK(leverage_allowed=(intraday_buying_power_multiple>1)),
 -- The archived V1 baseline keeps its frozen numbers: 1% per trade, 2% combined, one per
 -- sector and theme, whole-share cash sizing, terminal rejections, no randomized arm.
 CHECK(engine<>'FROZEN_V1' OR (risk_pct=0.01 AND account_cap_pct=0.02 AND max_per_sector=1
  AND max_per_theme=1 AND NOT leverage_allowed AND intraday_buying_power_multiple=1
  AND market_caps='{"US_STOCKS":0.02}'::jsonb AND sector_limited_markets='{US_STOCKS}'::text[]
  AND capacity_cooldown_seconds IS NULL AND fixed_exit_arm_pct=0))
);

-- One Alpaca Paper account per ledger: a hash of its identity, written once at the first clean
-- managed reconciliation. The raw broker account identifier is never stored.
CREATE TABLE lab.ledger_account_binding (
 venue text PRIMARY KEY CHECK(venue='ALPACA_PAPER'),
 account_hash text NOT NULL CHECK(account_hash ~ '^[0-9a-f]{64}$'),
 binding_version text NOT NULL CHECK(binding_version='LEDGER_ACCOUNT_BINDING_V1'),
 reconciliation_seq bigint NOT NULL REFERENCES lab.reconciliation_runs(event_seq),
 bound_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 event_seq bigint NOT NULL UNIQUE REFERENCES lab.trade_events(seq)
);
CREATE FUNCTION lab.guard_ledger_binding() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(719172026);
 IF current_user<>'catalyst_risk' OR NOT EXISTS(SELECT 1 FROM lab.reconciliation_runs r
   WHERE r.event_seq=NEW.reconciliation_seq AND r.clean) THEN
  RAISE EXCEPTION 'CLEAN_RECONCILIATION_REQUIRED_FOR_BINDING';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_binding BEFORE INSERT ON lab.ledger_account_binding
 FOR EACH ROW EXECUTE FUNCTION lab.guard_ledger_binding();

DO $$ DECLARE name text; BEGIN
 FOREACH name IN ARRAY ARRAY['account_risk_policies','ledger_account_binding'] LOOP
  EXECUTE format('CREATE TRIGGER audit_jev BEFORE INSERT ON lab.%I
    FOR EACH ROW EXECUTE FUNCTION lab.audit_jev_row()',name);
  EXECUTE format('CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON lab.%I
    FOR EACH ROW EXECUTE FUNCTION lab.reject_mutation()',name);
  EXECUTE format('CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON lab.%I
    FOR EACH STATEMENT EXECUTE FUNCTION lab.reject_mutation()',name);
 END LOOP;
END $$;

-- Seed rows reproduce the rules in force before this migration exactly; the V2 row carries the
-- numbers approved with the plan (CONTRACT-RESOLUTIONS.md, 2026-09-24; each adjustable only by
-- a new, separately named row).
INSERT INTO lab.account_risk_policies(policy_id,engine,risk_pct,account_cap_pct,market_caps,
 max_per_sector,max_per_theme,sector_limited_markets,leverage_allowed,
 intraday_buying_power_multiple,capacity_cooldown_seconds,fixed_exit_arm_pct,owner_ruling_ref)
VALUES('CATALYST_RETEST_V1','FROZEN_V1',0.01,0.02,'{"US_STOCKS":0.02}',1,1,'{US_STOCKS}',false,
 1,NULL,0,'CONTRACT-RESOLUTIONS 2026-09-17 Phase 4 frozen rulings');
INSERT INTO lab.account_risk_policies(policy_id,engine,risk_pct,account_cap_pct,market_caps,
 max_per_sector,max_per_theme,sector_limited_markets,leverage_allowed,
 intraday_buying_power_multiple,capacity_cooldown_seconds,fixed_exit_arm_pct,owner_ruling_ref)
VALUES('MUSE_JEV_MANAGED_TEST_V1','MANAGED',0.01,0.02,'{"US_STOCKS":0.02,"CRYPTO":0.02}',1,1,
 '{US_STOCKS,CRYPTO}',false,1,NULL,0,'MANAGED-WIRING-2026-09-19 schema-13 literals');
INSERT INTO lab.account_risk_policies(policy_id,engine,risk_pct,account_cap_pct,market_caps,
 max_per_sector,max_per_theme,sector_limited_markets,leverage_allowed,
 intraday_buying_power_multiple,capacity_cooldown_seconds,fixed_exit_arm_pct,owner_ruling_ref)
VALUES('JEV_MANAGED_RISK_V2','MANAGED',0.005,0.05,'{"US_STOCKS":0.03,"CRYPTO":0.02,"FOREX":0}',
 2,1,'{US_STOCKS}',true,2,60,30,'CONTRACT-RESOLUTIONS 2026-09-24');

-- Same leading columns as migration 013's view; market, venue and policy are appended. Legacy
-- rows map to the seeded policies: every frozen reservation to CATALYST_RETEST_V1, and a managed
-- decision without a context risk_policy_id to MUSE_JEV_MANAGED_TEST_V1.
CREATE OR REPLACE VIEW lab.account_risk_reservations AS
 SELECT candidate_id AS reference_id,budget,planned_risk,qty::numeric,max_entry,sector,theme,
 'FROZEN'::text AS source,'US_STOCKS'::text AS market,'ALPACA_PAPER'::text AS venue,
 'CATALYST_RETEST_V1'::text AS policy_id FROM lab.active_reservations
 UNION ALL SELECT r.setup_id,r.budget,r.planned_risk,r.qty,r.max_entry,r.sector,r.theme,'MANAGED',
 s.market,coalesce(d.context->>'venue','ALPACA_PAPER'),
 coalesce(d.context->>'risk_policy_id','MUSE_JEV_MANAGED_TEST_V1')
 FROM lab.managed_active_reservations r JOIN lab.managed_setups s USING(setup_id)
 JOIN lab.managed_risk_decisions d ON d.decision_id=r.decision_id;

-- NULL, or the first failing constraint. Correlation is the stricter of the new entry's policy
-- and the policy of every existing same-sector/same-theme reservation on the venue, so a rule
-- holds for as long as a reservation made under it is open. Caps use the new entry's policy.
CREATE FUNCTION lab.account_risk_failure(policy text, venue text, market text, sector text,
 theme text, equity numeric, budget numeric) RETURNS text
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE p lab.account_risk_policies; sector_count integer; theme_count integer;
        sector_limit integer; theme_limit integer; total numeric; market_total numeric;
        unknown integer; cap numeric;
BEGIN
 SELECT * INTO p FROM lab.account_risk_policies q WHERE q.policy_id=$1;
 IF p.policy_id IS NULL THEN RETURN 'RISK_POLICY_UNKNOWN'; END IF;
 IF $2 IS DISTINCT FROM 'ALPACA_PAPER' OR $3 IS NULL OR $3 NOT IN ('US_STOCKS','CRYPTO','FOREX')
 OR $6 IS NULL OR $6<=0 OR $7 IS NULL OR $7<=0 THEN RETURN 'INVALID_ACCOUNT_RISK_INPUT'; END IF;
 IF $4 IS NULL OR $5 IS NULL OR ($4='CRYPTO' AND $5='CRYPTO_UNLISTED')
 THEN RETURN 'CORRELATION_UNKNOWN'; END IF;
 SELECT count(*) FILTER(WHERE r.sector=$4),count(*) FILTER(WHERE r.theme=$5),
  min(q.max_per_sector) FILTER(WHERE r.sector=$4 AND r.market=ANY(q.sector_limited_markets)),
  min(q.max_per_theme) FILTER(WHERE r.theme=$5),coalesce(sum(r.budget),0),
  coalesce(sum(r.budget) FILTER(WHERE r.market=$3),0),count(*) FILTER(WHERE q.policy_id IS NULL)
 INTO sector_count,theme_count,sector_limit,theme_limit,total,market_total,unknown
 FROM lab.account_risk_reservations r LEFT JOIN lab.account_risk_policies q
  ON q.policy_id=r.policy_id WHERE r.venue=$2;
 IF unknown>0 THEN RETURN 'RISK_POLICY_UNKNOWN'; END IF;
 IF $3=ANY(p.sector_limited_markets) THEN
  sector_limit:=least(sector_limit,p.max_per_sector);
 END IF;
 IF sector_count>=sector_limit OR theme_count>=least(theme_limit,p.max_per_theme)
 THEN RETURN 'CORRELATION_LIMIT'; END IF;
 IF total+$7>p.account_cap_pct*$6 THEN RETURN 'MAX_OPEN_PLANNED_RISK'; END IF;
 cap:=CASE WHEN jsonb_typeof(p.market_caps->$3)='number'
  THEN ((p.market_caps->$3)::text)::numeric ELSE 0 END;
 IF market_total+$7>cap*$6 THEN RETURN 'MARKET_RISK_CAP'; END IF;
 RETURN NULL;
END $$;

-- Budget and leverage now come from the decision's policy row (context risk_policy_id; legacy
-- decisions without one are MUSE_JEV_MANAGED_TEST_V1, whose numbers equal the 013 literals).
-- Only a US DAY bracket may use the policy's intraday multiple; crypto stays cash-only.
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

-- Migration 005's body through the frozen equality block is reproduced verbatim; the V1-only
-- sector/theme count against the context's limits and the 2% total are replaced by the policy
-- function, so a frozen reservation raises exactly the reason the risk engine recorded.
CREATE OR REPLACE FUNCTION lab.enforce_risk_reservation() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
DECLARE d lab.risk_decisions; c lab.candidates; classification record;
        m numeric; s numeric; failure text;
BEGIN
 PERFORM pg_advisory_xact_lock(719172026);
 SELECT * INTO d FROM lab.risk_decisions WHERE risk_decision_id=NEW.risk_decision_id;
 SELECT * INTO c FROM lab.candidates WHERE candidate_id=NEW.candidate_id;
 SELECT * INTO classification FROM lab.current_classifications WHERE ticker=c.ticker;
 m := (c.payload_json->>'max_entry_price')::numeric;
 s := (c.payload_json->>'stop')::numeric;
 IF d.risk_decision_id IS NULL OR c.candidate_id IS NULL
  OR d.decision<>'APPROVED' OR d.action<>'ENTRY' OR d.candidate_id<>NEW.candidate_id
  OR d.transaction_id<>txid_current() OR d.expires_at<=clock_timestamp()
  OR NEW.budget<>d.risk_dollars OR NEW.budget<>0.01*d.equity OR NEW.qty<>d.computed_qty
  OR NEW.max_entry<>m OR NEW.planned_risk<>NEW.qty*(m-s)
  OR NEW.planned_risk<>d.planned_risk OR NEW.planned_risk>NEW.budget
  OR NEW.qty*m>d.equity OR classification.ticker IS NULL
  OR NEW.sector<>classification.sector OR NEW.theme<>classification.theme
  OR NOT (d.payload_json ?& ARRAY['symbol','qty','side','type','limit_price',
        'time_in_force','order_class','stop_loss','take_profit','client_order_id'])
  OR (d.payload_json->>'symbol') IS DISTINCT FROM c.ticker
  OR (d.payload_json->>'side') IS DISTINCT FROM 'buy'
  OR (d.payload_json->>'type') IS DISTINCT FROM 'limit'
  OR (d.payload_json->>'order_class') IS DISTINCT FROM 'bracket'
  OR (d.payload_json->>'time_in_force') IS DISTINCT FROM 'day'
  OR (d.payload_json->>'qty')::numeric IS DISTINCT FROM NEW.qty
  OR (d.payload_json->>'limit_price')::numeric IS DISTINCT FROM m
  OR (d.payload_json->'stop_loss'->>'stop_price')::numeric IS DISTINCT FROM s
  OR (d.payload_json->'take_profit'->>'limit_price')::numeric
       IS DISTINCT FROM (c.payload_json->>'target')::numeric
 THEN RAISE EXCEPTION 'risk reservation does not match frozen sizing and request'; END IF;
 failure:=lab.account_risk_failure('CATALYST_RETEST_V1','ALPACA_PAPER','US_STOCKS',
  NEW.sector,NEW.theme,d.equity,NEW.budget);
 IF failure IS NOT NULL THEN RAISE EXCEPTION '%',failure; END IF;
 RETURN NEW;
END $$;

-- Frozen reservations are always CATALYST_RETEST_V1 on the one paper account.
CREATE OR REPLACE FUNCTION lab.guard_legacy_shared_budget() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
DECLARE equity numeric; failure text; BEGIN
 PERFORM pg_advisory_xact_lock(719172026);
 SELECT d.equity INTO equity FROM lab.risk_decisions d
  WHERE d.risk_decision_id=NEW.risk_decision_id;
 failure:=lab.account_risk_failure('CATALYST_RETEST_V1','ALPACA_PAPER','US_STOCKS',
  NEW.sector,NEW.theme,equity,NEW.budget);
 IF failure IS NOT NULL THEN RAISE EXCEPTION '%',failure; END IF;
 RETURN NEW;
END $$;

REVOKE ALL ON lab.account_risk_policies,lab.ledger_account_binding FROM PUBLIC;
GRANT SELECT ON lab.account_risk_policies,lab.ledger_account_binding
 TO catalyst_app,catalyst_review;
GRANT INSERT ON lab.ledger_account_binding TO catalyst_risk;
REVOKE ALL ON FUNCTION lab.valid_market_caps(jsonb,numeric),lab.guard_ledger_binding(),
 lab.account_risk_failure(text,text,text,text,text,numeric,numeric) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.account_risk_failure(text,text,text,text,text,numeric,numeric)
 TO catalyst_risk;
INSERT INTO lab.schema_migrations(version) VALUES(16);
