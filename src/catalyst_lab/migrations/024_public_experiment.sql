-- Public live dashboard (package experiment-page, 2026-09-27). Display only: no trading rule
-- changes. DDL only: one NOLOGIN role, three pure helpers, one index and read-only views. No data
-- row or event is written and no existing object changes.
--
-- catalyst_public is NOLOGIN here; in production the cloud provisioner enables LOGIN with a
-- generated password. It may read exactly the four lab.public_dashboard_* views granted at the
-- end and execute the three helpers those views call. It reads no base table and no other view,
-- executes no other function, owns nothing and is a member of no role.
--
-- The views expose only what the dashboard shows: coin symbols, the randomized arm, times,
-- levels, exit reason codes, fill-based P&L and fee components, the freshest price the ledger
-- already holds for an open trade, the research agents' names, one-line reasons (at most 160
-- characters of a pick's thesis or an agent's answer), Jev's actions with their top probability,
-- research-run counts, the paper account's last recorded equity and the runtime's heartbeat.
-- Never an account number or hash, a broker or client order ID, a request or response body, a
-- token, a raw Jev/TypeSafe answer, a source excerpt or an internal UUID: trades and research
-- runs carry opaque sequence numbers instead.
--
-- Scope: every report-V3 crypto setup of the managed cohort (the crypto research-and-trading
-- loop, docs/CRYPTO-AGENT-LOOP.md) and every AGENT_RESEARCH_REPORT_V3 research run. Operator
-- ENGINEERING_TEST enrollments are never part of it (docs/ENGINEERING-ACCEPTANCE.md). A trade is
-- numbered by the audit sequence of its first recorded buy fill, so a number never changes.
--
-- Fee resolution follows managed_analytics.resolved_fill_costs: a fill's own fee, overridden by
-- its chain of FILL_COST_CORRECTION events; any malformed link keeps that fill's fee unknown. The
-- structural checks are ported one for one; the two SHA-256 bindings (fill binding and evidence
-- hash) are required to be present but are not recomputed here (docs/packages/
-- experiment-page.md). Net P&L and planned-filled risk follow managed_measurement exactly.

CREATE ROLE catalyst_public NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
 CONNECTION LIMIT 8;
GRANT USAGE ON SCHEMA lab TO catalyst_public;

-- Kind-ordered reads (latest heartbeat, research runs) without scanning every managed event.
CREATE INDEX managed_events_kind_seq ON lab.managed_events(kind, event_seq);

-- A decimal string or JSON number as numeric, else NULL (never an error, never NaN/Infinity).
CREATE FUNCTION lab.public_decimal(value jsonb) RETURNS numeric
LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
DECLARE t text;
BEGIN
 IF value IS NULL OR jsonb_typeof(value) NOT IN ('string','number') THEN RETURN NULL; END IF;
 t := btrim(value #>> '{}');
 IF t !~ '^[-+]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][-+]?[0-9]{1,4})?$' THEN RETURN NULL; END IF;
 RETURN t::numeric;
EXCEPTION WHEN others THEN RETURN NULL;
END $$;

-- An ISO-8601 string with an explicit offset as timestamptz, else NULL (never an error).
CREATE FUNCTION lab.public_timestamp(value jsonb) RETURNS timestamptz
LANGUAGE plpgsql STABLE SET search_path=pg_catalog AS $$
DECLARE t text;
BEGIN
 IF value IS NULL OR jsonb_typeof(value) <> 'string' THEN RETURN NULL; END IF;
 t := value #>> '{}';
 IF t !~ ('^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]{1,6})?)?'
          '(Z|z|[+-][0-9]{2}(:?[0-9]{2})?)$') THEN RETURN NULL; END IF;
 RETURN t::timestamptz;
EXCEPTION WHEN others THEN RETURN NULL;
END $$;

-- One line of agent-written text: control characters and runs of white space become single
-- spaces; at most 160 characters (157 and an ellipsis). A non-string is NULL.
CREATE FUNCTION lab.public_line(value jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
DECLARE t text;
BEGIN
 IF value IS NULL OR jsonb_typeof(value) <> 'string' THEN RETURN NULL; END IF;
 t := btrim(regexp_replace(value #>> '{}', '[[:space:][:cntrl:]]+', ' ', 'g'));
 IF t = '' THEN RETURN NULL; END IF;
 IF length(t) > 160 THEN RETURN left(t, 157) || '...'; END IF;
 RETURN t;
END $$;

-- Internal helpers (granted to no role): the experiment's setups, research runs, fill costs,
-- trades and picks. The public views below read them with the view owner's privileges.
CREATE VIEW lab.experiment_scope_setups AS
 SELECT s.setup_id, s.cycle_id, s.symbol, s.event_seq AS setup_seq,
  s.record_json->>'item_key' AS item_key,
  lab.public_decimal(s.record_json->'levels'->'entry_trigger') AS planned_entry,
  lab.public_decimal(s.record_json->'levels'->'max_entry_price') AS max_entry,
  lab.public_decimal(s.record_json->'levels'->'stop') AS planned_stop,
  lab.public_decimal(s.record_json->'levels'->'target') AS planned_target,
  t.body AS state
 FROM lab.managed_setups s LEFT JOIN lab.managed_states t ON t.setup_id=s.setup_id
 WHERE s.cohort='JEV_MANAGED_PAPER_V1' AND s.market='CRYPTO' AND s.receipt_id IS NOT NULL
  AND s.record_json->>'report_schema_version'='AGENT_RESEARCH_REPORT_V3'
  AND coalesce(s.record_json->>'purpose','')<>'ENGINEERING_TEST'
  AND coalesce(s.record_json->>'selection_policy','')<>'MANAGED_ENGINEERING_ENROLLMENT_V1';

CREATE VIEW lab.experiment_run_facts AS
 SELECT e.body->>'cycle_id' AS cycle_id, e.event_seq, e.recorded_at AS submitted_at,
  coalesce(lab.public_timestamp(e.body->'run_slot'), e.recorded_at) AS run_at,
  row_number() OVER (ORDER BY e.event_seq) AS run_no,
  CASE WHEN e.body->'agent'->>'agent_id' ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$'
   THEN e.body->'agent'->>'agent_id' END AS agent
 FROM lab.managed_events e
 WHERE e.kind='RESEARCH_STARTED' AND e.setup_id IS NULL
  AND e.body->>'report_schema_version'='AGENT_RESEARCH_REPORT_V3';

CREATE VIEW lab.experiment_fill_costs AS
 WITH fills AS (
  SELECT f.fill_id, f.setup_id, f.side, f.qty, f.price, f.filled_at, f.event_seq,
   CASE WHEN f.fee_usd::text ~ '^[0-9]+(\.[0-9]+)?$' THEN f.fee_usd END AS native_fee
  FROM lab.managed_fills f JOIN lab.experiment_scope_setups x ON x.setup_id=f.setup_id
 ), chain AS (
  SELECT f.fill_id, f.setup_id, f.qty AS fill_qty, f.filled_at, f.event_seq AS fill_event_seq,
   e.event_seq, e.body,
   lab.public_decimal(e.body->'fee_usd') AS fee,
   lab.public_decimal(e.body->'base_asset_fee_qty') AS base_qty,
   lab.public_decimal(e.body->'base_asset_fee_usd') AS base_value,
   lab.public_timestamp(e.body->'observed_at') AS observed_at,
   lab.public_timestamp(e.body->'recorded_at') AS recorded_at,
   lag(e.body->'correction_id') OVER w AS prior_correction,
   row_number() OVER w AS position
  FROM fills f JOIN lab.managed_events e ON e.setup_id=f.setup_id
   AND e.kind='FILL_COST_CORRECTION' AND jsonb_typeof(e.body->'fill_id')='string'
   AND e.body->>'fill_id'=f.fill_id
  WINDOW w AS (PARTITION BY f.fill_id ORDER BY e.event_seq)
 ), checked AS (
  SELECT c.fill_id, c.event_seq, c.fee, c.base_qty, c.base_value, c.body->>'source' AS source,
   coalesce(
    c.body ?& ARRAY['correction_id','setup_id','fill_event_seq','fee_usd','base_asset_fee_qty',
     'base_asset_fee_usd','source','broker_source_reference','observed_at','recorded_at',
     'supersedes_correction_id','policy_version','fill_binding_hash','evidence_hash']
    -- Amounts: exact non-negative decimals; a JSON number only in integer form, as the Python
    -- reader refuses a float.
    AND c.fee>=0 AND c.base_qty>=0 AND c.base_value>=0
    AND (jsonb_typeof(c.body->'fee_usd')='string' OR c.body->>'fee_usd' ~ '^[0-9]+$')
    AND (jsonb_typeof(c.body->'base_asset_fee_qty')='string'
         OR c.body->>'base_asset_fee_qty' ~ '^[0-9]+$')
    AND (jsonb_typeof(c.body->'base_asset_fee_usd')='string'
         OR c.body->>'base_asset_fee_usd' ~ '^[0-9]+$')
    AND c.base_value<=c.fee AND (c.base_qty>0)=(c.base_value>0)
    AND c.body->>'policy_version'='MANAGED_BROKER_COST_CORRECTION_V1'
    AND jsonb_typeof(c.body->'setup_id')='string' AND c.body->>'setup_id'=c.setup_id::text
    AND jsonb_typeof(c.body->'fill_event_seq')='number'
    AND lab.public_decimal(c.body->'fill_event_seq')=c.fill_event_seq
    -- Each correction supersedes exactly the one before it (the first supersedes nothing).
    AND c.body->'supersedes_correction_id'=coalesce(c.prior_correction,'null'::jsonb)
    AND jsonb_typeof(c.body->'source')='string'
    AND c.body->>'source' IN ('BROKER_ACTIVITY','BROKER_STATEMENT','LAB_FIXTURE',
     'ALPACA_PAPER_ACTIVITY')
    AND CASE jsonb_typeof(c.body->'broker_source_reference')
     WHEN 'string' THEN c.body->>'broker_source_reference'<>''
     WHEN 'number' THEN lab.public_decimal(c.body->'broker_source_reference')<>0
     WHEN 'boolean' THEN c.body->'broker_source_reference'='true'::jsonb
     WHEN 'array' THEN jsonb_array_length(c.body->'broker_source_reference')>0
     WHEN 'object' THEN c.body->'broker_source_reference'<>'{}'::jsonb
     ELSE false END
    AND c.filled_at<=c.observed_at AND c.observed_at<=c.recorded_at
    AND c.base_qty<=c.fill_qty
    AND c.body->>'fill_binding_hash' ~ '^[0-9a-f]{64}$'
    AND c.body->>'evidence_hash' ~ '^[0-9a-f]{64}$', false) AS valid
  FROM chain c
 ), per_fill AS (
  -- As in Python, a malformed link leaves the coin quantity of the valid links before it.
  SELECT fill_id, bool_and(valid) AS chain_valid,
   (array_agg(fee ORDER BY event_seq DESC))[1] AS fee,
   (array_agg(base_qty ORDER BY event_seq DESC))[1] AS base_qty,
   (array_agg(base_qty ORDER BY event_seq DESC) FILTER (WHERE prefix_valid))[1] AS prefix_qty,
   (array_agg(base_value ORDER BY event_seq DESC))[1] AS base_value,
   (array_agg(source ORDER BY event_seq DESC))[1] AS source
  FROM (SELECT c.*, bool_and(c.valid) OVER (PARTITION BY c.fill_id ORDER BY c.event_seq)
    AS prefix_valid FROM checked c) prefixed
  GROUP BY fill_id
 )
 SELECT f.fill_id, f.setup_id, f.side, f.qty, f.price, f.filled_at, f.event_seq,
  CASE WHEN p.fill_id IS NULL THEN f.native_fee WHEN p.chain_valid THEN p.fee END AS fee_usd,
  CASE WHEN p.fill_id IS NULL THEN f.native_fee WHEN p.chain_valid THEN p.fee-p.base_value END
   AS cash_fee_usd,
  CASE WHEN p.chain_valid THEN p.base_value ELSE 0 END AS coin_fee_usd,
  CASE WHEN p.chain_valid THEN p.base_qty ELSE coalesce(p.prefix_qty,0) END AS coin_fee_qty,
  CASE WHEN p.fill_id IS NULL THEN
    CASE WHEN f.native_fee IS NULL THEN 'UNKNOWN' ELSE 'FILL_EVENT' END
   WHEN p.chain_valid THEN p.source ELSE 'INVALID_CORRECTION' END AS cost_source
 FROM fills f LEFT JOIN per_fill p ON p.fill_id=f.fill_id;

CREATE VIEW lab.experiment_trade_facts AS
 WITH agg AS (
  SELECT setup_id,
   count(*) FILTER (WHERE side='buy') AS buy_count,
   count(*) FILTER (WHERE side='sell') AS sell_count,
   coalesce(sum(qty) FILTER (WHERE side='buy'),0) AS bought_qty,
   coalesce(sum(qty) FILTER (WHERE side='sell'),0) AS sold_qty,
   coalesce(sum(qty*price) FILTER (WHERE side='buy'),0) AS buy_cost,
   coalesce(sum(qty*price) FILTER (WHERE side='sell'),0) AS sell_proceeds,
   min(filled_at) FILTER (WHERE side='buy') AS entry_at,
   max(filled_at) FILTER (WHERE side='sell') AS last_sell_at,
   min(event_seq) FILTER (WHERE side='buy') AS first_buy_seq,
   bool_and(fee_usd IS NOT NULL) AS fees_known,
   sum(fee_usd) AS fee_sum, sum(cash_fee_usd) AS cash_fee_sum, sum(coin_fee_usd) AS coin_fee_sum,
   sum(coin_fee_qty) AS coin_fee_qty,
   bool_or(cost_source='LAB_FIXTURE') AS fixture_fee_evidence
  FROM lab.experiment_fill_costs GROUP BY setup_id
 ), measured AS (
  SELECT x.setup_id, x.cycle_id, x.item_key, x.symbol, x.planned_entry, x.max_entry,
   x.planned_stop, x.planned_target, x.state, a.buy_count, a.sell_count, a.bought_qty,
   a.sold_qty, a.buy_cost, a.sell_proceeds, a.entry_at, a.last_sell_at, a.first_buy_seq,
   a.fees_known, a.fee_sum, a.cash_fee_sum, a.coin_fee_sum, a.coin_fee_qty,
   a.fixture_fee_evidence,
   coalesce(x.state->>'state','')='CLOSED' AS closed,
   CASE WHEN coalesce(x.state->>'state','')='CLOSED' AND a.sell_count>0
    THEN a.sell_proceeds-a.buy_cost END AS gross_pnl_usd,
   a.bought_qty=a.sold_qty+a.coin_fee_qty AS inventory_reconciled,
   CASE WHEN x.max_entry IS NOT NULL AND x.planned_stop IS NOT NULL
    THEN a.bought_qty*(x.max_entry-x.planned_stop) END AS planned_risk_usd
  FROM lab.experiment_scope_setups x JOIN agg a ON a.setup_id=x.setup_id
  WHERE a.buy_count>0
 )
 SELECT row_number() OVER (ORDER BY m.first_buy_seq) AS trade_no, m.setup_id, m.cycle_id,
  m.item_key, r.run_no,
  CASE WHEN m.symbol ~ '^[A-Z0-9]{1,16}/USD$' THEN m.symbol END AS symbol,
  CASE WHEN m.state->>'arm' IN ('JEV_MANAGED','FIXED_EXIT') THEN m.state->>'arm' END AS arm,
  CASE WHEN m.state->>'state' ~ '^[A-Z][A-Z0-9_]{1,63}$' THEN m.state->>'state' END
   AS state_name, m.closed, m.state,
  m.planned_entry, m.max_entry, m.planned_stop, m.planned_target,
  lab.public_decimal(m.state->'stop') AS current_stop,
  lab.public_decimal(m.state->'target') AS current_target,
  m.entry_at, m.last_sell_at, m.bought_qty, m.sold_qty,
  round(m.buy_cost/m.bought_qty,10) AS avg_entry_price,
  CASE WHEN m.sold_qty>0 THEN round(m.sell_proceeds/m.sold_qty,10) END AS avg_exit_price,
  m.gross_pnl_usd, m.fees_known,
  CASE WHEN m.fees_known THEN m.fee_sum END AS fees_usd,
  CASE WHEN m.fees_known THEN m.cash_fee_sum END AS cash_fees_usd,
  CASE WHEN m.fees_known THEN m.coin_fee_sum END AS coin_fees_usd,
  m.inventory_reconciled,
  CASE WHEN m.gross_pnl_usd IS NOT NULL AND m.fees_known AND m.inventory_reconciled
   THEN m.gross_pnl_usd-m.cash_fee_sum END AS net_pnl_usd,
  CASE WHEN m.planned_risk_usd>0 THEN m.planned_risk_usd END AS planned_risk_usd,
  coalesce(m.fixture_fee_evidence,false) AS fixture_fee_evidence
 FROM measured m LEFT JOIN lab.experiment_run_facts r ON r.cycle_id=m.cycle_id::text;


CREATE VIEW lab.experiment_pick_facts AS
 WITH packets AS (
  SELECT DISTINCT ON (e.body->>'cycle_id', e.body->>'item_key')
   r.run_no, r.agent, e.body->>'cycle_id' AS cycle_id, e.body->>'item_key' AS item_key,
   e.body AS packet, e.recorded_at AS proposed_at
  FROM lab.experiment_run_facts r JOIN lab.managed_events e ON e.body->>'cycle_id'=r.cycle_id
   AND e.kind='RESEARCH_PACKET' AND e.setup_id IS NULL
   AND e.body->>'report_schema_version'='AGENT_RESEARCH_REPORT_V3'
  ORDER BY e.body->>'cycle_id', e.body->>'item_key', e.event_seq DESC
 ), selections AS (
  SELECT DISTINCT e.body->>'cycle_id' AS cycle_id, e.body->'packet'->>'item_key' AS item_key
  FROM lab.experiment_run_facts r JOIN lab.managed_events e ON e.body->>'cycle_id'=r.cycle_id
   AND e.kind='RESEARCH_SELECTED'
 ), rankings AS (
  SELECT r.cycle_id, e.recorded_at AS ranked_at,
   CASE WHEN jsonb_typeof(e.body->'entries')='array' THEN e.body->'entries' ELSE '[]'::jsonb END
    AS entries
  FROM lab.experiment_run_facts r JOIN lab.managed_events e ON e.body->>'cycle_id'=r.cycle_id
   AND e.kind='RESEARCH_RANKING' AND e.idempotency_key='research:ranking:'||r.cycle_id
 ), ranking_entries AS (
  SELECT DISTINCT ON (k.cycle_id, x.entry->>'item_key')
   k.cycle_id, k.ranked_at, x.entry->>'item_key' AS item_key, x.entry
  FROM rankings k CROSS JOIN LATERAL jsonb_array_elements(k.entries) WITH ORDINALITY
   AS x(entry, position)
  ORDER BY k.cycle_id, x.entry->>'item_key', x.position DESC
 ), setups AS (
  SELECT DISTINCT ON (s.cycle_id, s.record_json->>'item_key')
   s.cycle_id::text AS cycle_id, s.record_json->>'item_key' AS item_key, s.setup_id,
   coalesce(s.record_json->>'purpose','')='ENGINEERING_TEST' AS engineering
  FROM lab.managed_setups s WHERE s.record_json->>'item_key' IS NOT NULL
  ORDER BY s.cycle_id, s.record_json->>'item_key', s.event_seq DESC
 )
 SELECT p.run_no, p.agent, p.cycle_id, p.item_key, p.proposed_at,
  CASE WHEN p.packet->>'symbol' ~ '^[A-Z0-9]{1,16}/USD$' THEN p.packet->>'symbol' END AS symbol,
  CASE WHEN p.packet->'state'->>'kind' IN ('NEWS','CHART','BOTH')
   THEN p.packet->'state'->>'kind' END AS pick_kind,
  lab.public_decimal(p.packet->'levels'->'entry_trigger') AS entry,
  lab.public_decimal(p.packet->'levels'->'max_entry_price') AS max_entry,
  lab.public_decimal(p.packet->'levels'->'stop') AS stop,
  lab.public_decimal(p.packet->'levels'->'target') AS target,
  lab.public_line(p.packet->'state'->'thesis') AS thesis_line,
  s.item_key IS NOT NULL AS selected,
  re.ranked_at, re.entry->>'status' AS ranking_status,
  CASE WHEN re.entry->>'rank' ~ '^[1-9][0-9]{0,5}$' THEN (re.entry->>'rank')::integer END
   AS jev_rank,
  (SELECT string_agg(v #>> '{}', ',' ORDER BY v #>> '{}')
   FROM jsonb_array_elements(CASE WHEN jsonb_typeof(re.entry->'veto_reasons')='array'
    THEN re.entry->'veto_reasons' ELSE '[]'::jsonb END) v
   WHERE jsonb_typeof(v)='string' AND v #>> '{}' ~ '^[A-Z][A-Z0-9_]{1,63}$') AS veto_reasons,
  st.setup_id, coalesce(st.engineering,false) AS engineering
 FROM packets p
 LEFT JOIN selections s ON s.cycle_id=p.cycle_id AND s.item_key=p.item_key
 LEFT JOIN ranking_entries re ON re.cycle_id=p.cycle_id AND re.item_key=p.item_key
 LEFT JOIN setups st ON st.cycle_id=p.cycle_id AND st.item_key=p.item_key
 WHERE lab.public_timestamp(p.packet->'created_at') IS NOT NULL;

-- Public views (granted to catalyst_public). Codes pass only in their code shape.
CREATE VIEW lab.public_dashboard_status AS
 WITH heartbeat AS (
  SELECT e.recorded_at, e.body FROM lab.managed_events e
  WHERE e.kind='RUNTIME_HEARTBEAT' AND e.setup_id IS NULL ORDER BY e.event_seq DESC LIMIT 1
 ), equity AS (
  SELECT d.equity, d.created_at FROM lab.managed_risk_decisions d
  ORDER BY d.event_seq DESC LIMIT 1
 ), receipt AS (
  SELECT r.completed_at, r.outcome FROM lab.jev_receipts r ORDER BY r.event_seq DESC LIMIT 1
 ), today AS (
  SELECT (clock_timestamp() AT TIME ZONE 'America/New_York')::date AS day
 )
 SELECT clock_timestamp() AS as_of, (SELECT day FROM today) AS ny_today,
  (SELECT min(run_at) FROM lab.experiment_run_facts) AS experiment_started_at,
  (SELECT recorded_at FROM heartbeat) AS last_heartbeat_at,
  (SELECT CASE WHEN body->>'management_reviews' IN ('ENABLED','DISABLED')
   THEN body->>'management_reviews' END FROM heartbeat) AS heartbeat_management_reviews,
  (SELECT CASE WHEN body->'jev_breaker'->>'state' IN ('CLOSED','OPEN','HALF_OPEN')
   THEN body->'jev_breaker'->>'state' END FROM heartbeat) AS jev_breaker,
  (SELECT count(*) FROM lab.execution_halts) AS active_halts,
  EXISTS(SELECT 1 FROM lab.daily_risk_halts WHERE session_date=(SELECT day FROM today))
   AS daily_loss_halt_today,
  (SELECT equity FROM equity) AS equity_usd, (SELECT created_at FROM equity) AS equity_at,
  (SELECT completed_at FROM receipt) AS jev_last_call_at,
  (SELECT outcome FROM receipt) AS jev_last_call_outcome,
  (SELECT count(*) FROM lab.jev_requests q
   WHERE (q.created_at AT TIME ZONE 'America/New_York')::date=(SELECT day FROM today))
   AS jev_calls_today,
  (SELECT max(version) FROM lab.schema_migrations) AS schema_version;

CREATE VIEW lab.public_dashboard_trades AS
 SELECT t.trade_no, t.run_no, t.symbol, t.arm, t.state_name AS status, t.closed,
  t.entry_at, coalesce(t.last_sell_at, lab.public_timestamp(t.state->'closed_at')) AS exit_at,
  t.planned_entry, t.max_entry, t.planned_stop, t.planned_target, t.current_stop,
  t.current_target, t.avg_entry_price, t.avg_exit_price, t.bought_qty, t.sold_qty,
  lab.public_decimal(t.state->'qty') AS open_qty,
  CASE WHEN coalesce(t.state->>'exit_reason', t.state->>'reason') ~ '^[A-Z][A-Z0-9_]{1,63}$'
   THEN coalesce(t.state->>'exit_reason', t.state->>'reason') END AS exit_reason,
  coalesce(t.state->>'exit_requested','')<>'' AS exit_in_progress,
  lab.public_timestamp(t.state->'day_review_at') AS next_review_at,
  lab.public_decimal(t.state->'continuations') AS continuations,
  t.gross_pnl_usd, t.fees_known, t.fees_usd, t.inventory_reconciled, t.net_pnl_usd,
  t.planned_risk_usd, t.fixture_fee_evidence,
  -- The freshest price the ledger already holds: the protection loop's latest position sample
  -- or the latest maintenance decision's quote, whichever is newer (the bid: the sell side).
  CASE WHEN q.sample_at IS NOT NULL AND (m.quote_at IS NULL OR q.sample_at>=m.quote_at)
   THEN q.bid ELSE m.bid END AS price,
  CASE WHEN q.sample_at IS NOT NULL AND (m.quote_at IS NULL OR q.sample_at>=m.quote_at)
   THEN q.sample_at ELSE m.quote_at END AS price_at,
  j.kind AS jev_last_kind, j.outcome AS jev_last_outcome, j.action AS jev_last_action,
  j.at AS jev_last_at, j.confidence AS jev_last_confidence, j.new_stop AS jev_last_stop,
  j.new_target AS jev_last_target, j.basis AS jev_last_basis,
  c.action AS jev_change_action, c.at AS jev_change_at, c.new_stop AS jev_change_stop,
  c.new_target AS jev_change_target, c.basis AS jev_change_basis
 FROM lab.experiment_trade_facts t
 LEFT JOIN LATERAL (
  SELECT coalesce(lab.public_decimal(e.body->'bid'), lab.public_decimal(e.body->'price')) AS bid,
   coalesce(lab.public_timestamp(e.body->'received_at'), e.recorded_at) AS sample_at
  FROM lab.managed_events e WHERE NOT t.closed AND e.setup_id=t.setup_id
   AND e.kind='POSITION_MARKET_SNAPSHOT'
  ORDER BY e.event_seq DESC LIMIT 1) q ON true
 LEFT JOIN LATERAL (
  SELECT lab.public_decimal(e.body->'quote'->'bid') AS bid,
   coalesce(lab.public_timestamp(e.body->'quote'->'read_at'), e.recorded_at) AS quote_at
  FROM lab.managed_events e WHERE NOT t.closed AND e.setup_id=t.setup_id
   AND e.kind='MAINTENANCE_DECISION' AND jsonb_typeof(e.body->'quote')='object'
  ORDER BY e.event_seq DESC LIMIT 1) m ON true
 -- An open trade's last level change by Jev (reviews run every minute, so the latest decision
 -- is usually a hold).
 LEFT JOIN LATERAL (
  SELECT CASE WHEN e.body->>'action' ~ '^[A-Z][A-Z0-9_]{1,63}$' THEN e.body->>'action' END
    AS action,
   coalesce(lab.public_timestamp(e.body->'decided_at'), e.recorded_at) AS at,
   lab.public_decimal(e.body->'levels_after'->'stop') AS new_stop,
   lab.public_decimal(e.body->'levels_after'->'target') AS new_target,
   CASE WHEN coalesce(e.body->'stop'->'bases'->>0, e.body->'target'->'bases'->>0)
    ~ '^[A-Z][A-Z0-9_]{1,63}$'
    THEN coalesce(e.body->'stop'->'bases'->>0, e.body->'target'->'bases'->>0) END AS basis
  FROM lab.managed_events e WHERE NOT t.closed AND e.setup_id=t.setup_id
   AND e.kind='MAINTENANCE_DECISION' AND e.body->>'outcome'='APPLIED'
  ORDER BY e.event_seq DESC LIMIT 1) c ON true
 LEFT JOIN LATERAL (
  SELECT e.kind,
   CASE WHEN e.body->>'outcome' ~ '^[A-Z][A-Z0-9_]{1,63}$' THEN e.body->>'outcome' END
    AS outcome,
   CASE WHEN e.body->>'action' ~ '^[A-Z][A-Z0-9_]{1,63}$' THEN e.body->>'action' END AS action,
   coalesce(lab.public_timestamp(e.body->'decided_at'), e.recorded_at) AS at,
   CASE e.kind WHEN 'MAINTENANCE_DECISION'
    THEN lab.public_decimal(e.body->'answers'->'action'->'top_p')
    ELSE lab.public_decimal(e.body->'jev'->'results'->-1->'answer'->'summary'->'decision'
     ->'top_p') END AS confidence,
   lab.public_decimal(e.body->'levels_after'->'stop') AS new_stop,
   lab.public_decimal(e.body->'levels_after'->'target') AS new_target,
   CASE WHEN coalesce(e.body->'stop'->'bases'->>0, e.body->'target'->'bases'->>0)
    ~ '^[A-Z][A-Z0-9_]{1,63}$'
    THEN coalesce(e.body->'stop'->'bases'->>0, e.body->'target'->'bases'->>0) END AS basis
  FROM lab.managed_events e WHERE e.setup_id=t.setup_id
   AND e.kind IN ('MAINTENANCE_DECISION','DAY_REVIEW_DECISION')
  ORDER BY e.event_seq DESC LIMIT 1) j ON true
 WHERE t.state_name IS NULL OR t.state_name NOT IN ('INVALIDATED','EXPIRED_UNTRIGGERED',
  'RISK_REJECTED','REJECTED') OR t.closed;

-- One row per research run: when it ran and was received, its picks, Jev's ranking (ranked,
-- vetoed, not ranked, selected, with the selected coins in Jev's order) and the trades it led to.
CREATE VIEW lab.public_dashboard_runs AS
 SELECT r.run_no, r.agent, r.run_at, r.submitted_at, max(p.ranked_at) AS ranked_at,
  count(p.item_key) AS picks,
  count(p.item_key) FILTER (WHERE p.ranking_status='RANKED') AS ranked,
  count(p.item_key) FILTER (WHERE p.ranking_status='VETOED') AS vetoed,
  count(p.item_key) FILTER (WHERE p.ranking_status='NOT_RANKED') AS not_ranked,
  count(p.item_key) FILTER (WHERE p.selected) AS selected,
  string_agg(p.symbol, ',' ORDER BY p.jev_rank NULLS LAST, p.symbol)
   FILTER (WHERE p.selected AND p.symbol IS NOT NULL) AS selected_symbols,
  count(t.trade_no) AS traded
 FROM lab.experiment_run_facts r
 LEFT JOIN lab.experiment_pick_facts p ON p.cycle_id=r.cycle_id AND NOT p.engineering
 LEFT JOIN lab.experiment_trade_facts t ON t.setup_id=p.setup_id
 GROUP BY r.run_no, r.agent, r.run_at, r.submitted_at;

-- Every decision the dashboard lists, one row each: the research agents' picks, 24-hour review
-- answers and exit flags, and Jev's selections, maintenance actions and 24-hour decisions. Per
-- trade, only the latest 60 maintenance decisions are listed (reviews run every minute).
CREATE VIEW lab.public_dashboard_decisions AS
 WITH trades AS (SELECT setup_id, trade_no, symbol FROM lab.experiment_trade_facts),
 picks AS (
  SELECT p.*, t.trade_no FROM lab.experiment_pick_facts p
  LEFT JOIN lab.experiment_trade_facts t ON t.setup_id=p.setup_id WHERE NOT p.engineering
 )
 SELECT p.proposed_at AS at, 'RESEARCH_AGENT' AS actor, p.agent, 'PICK' AS kind,
  'PROPOSED' AS outcome, p.pick_kind AS action, p.symbol, p.trade_no, p.run_no,
  NULL::integer AS jev_rank, NULL::numeric AS confidence, p.entry, p.stop, p.target,
  NULL::text AS basis, p.thesis_line AS note
 FROM picks p
 UNION ALL
 SELECT coalesce(p.ranked_at, p.proposed_at), 'JEV', NULL, 'SELECTION',
  CASE WHEN p.selected THEN 'SELECTED' WHEN p.ranking_status='RANKED' THEN 'PASSED'
   WHEN p.ranking_status IN ('VETOED','NOT_RANKED') THEN p.ranking_status END,
  NULL, p.symbol, p.trade_no, p.run_no, p.jev_rank, NULL, NULL, NULL, NULL, NULL,
  p.veto_reasons
 FROM picks p WHERE p.selected OR p.ranking_status IS NOT NULL
 UNION ALL
 SELECT d.at, 'JEV', NULL, 'MAINTENANCE', d.outcome, d.action, t.symbol, t.trade_no, NULL,
  NULL, d.confidence, NULL, d.new_stop, d.new_target, d.basis, d.code
 FROM trades t CROSS JOIN LATERAL (
  SELECT coalesce(lab.public_timestamp(e.body->'decided_at'), e.recorded_at) AS at,
   CASE WHEN e.body->>'outcome' ~ '^[A-Z][A-Z0-9_]{1,63}$' THEN e.body->>'outcome' END
    AS outcome,
   CASE WHEN e.body->>'action' ~ '^[A-Z][A-Z0-9_]{1,63}$' THEN e.body->>'action' END AS action,
   lab.public_decimal(e.body->'answers'->'action'->'top_p') AS confidence,
   lab.public_decimal(e.body->'levels_after'->'stop') AS new_stop,
   lab.public_decimal(e.body->'levels_after'->'target') AS new_target,
   CASE WHEN coalesce(e.body->'stop'->'bases'->>0, e.body->'target'->'bases'->>0)
    ~ '^[A-Z][A-Z0-9_]{1,63}$'
    THEN coalesce(e.body->'stop'->'bases'->>0, e.body->'target'->'bases'->>0) END AS basis,
   CASE WHEN e.body->>'code' ~ '^[A-Z][A-Z0-9_]{1,63}$' THEN e.body->>'code' END AS code
  FROM lab.managed_events e WHERE e.setup_id=t.setup_id AND e.kind='MAINTENANCE_DECISION'
  ORDER BY e.event_seq DESC LIMIT 60) d
 UNION ALL
 SELECT coalesce(lab.public_timestamp(e.body->'decided_at'), e.recorded_at), 'JEV', NULL,
  'DAY_REVIEW',
  CASE WHEN e.body->>'outcome' ~ '^[A-Z][A-Z0-9_]{1,63}$' THEN e.body->>'outcome' END, NULL,
  t.symbol, t.trade_no, NULL, NULL,
  lab.public_decimal(e.body->'jev'->'results'->-1->'answer'->'summary'->'decision'->'top_p'),
  NULL, lab.public_decimal(e.body->'levels_after'->'stop'),
  lab.public_decimal(e.body->'levels_after'->'target'), NULL,
  CASE WHEN e.body->>'code' ~ '^[A-Z][A-Z0-9_]{1,63}$' THEN e.body->>'code' END
 FROM trades t JOIN lab.managed_events e ON e.setup_id=t.setup_id
  AND e.kind='DAY_REVIEW_DECISION'
 UNION ALL
 SELECT coalesce(lab.public_timestamp(e.body->'received_at'), e.recorded_at), 'RESEARCH_AGENT',
  CASE WHEN e.body->>'agent_id' ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$'
   THEN e.body->>'agent_id' END,
  CASE e.kind WHEN 'DAY_REVIEW_AGENT_ANSWER' THEN 'REVIEW_ANSWER' ELSE 'FLAG_ANSWER' END,
  CASE WHEN e.body->'answer'->>'decision' IN ('CONTINUE','EXIT')
   THEN e.body->'answer'->>'decision' END, NULL, t.symbol, t.trade_no, NULL, NULL, NULL, NULL,
  NULL, NULL, NULL, lab.public_line(e.body->'answer'->'what_changed')
 FROM trades t JOIN lab.managed_events e ON e.setup_id=t.setup_id
  AND e.kind IN ('DAY_REVIEW_AGENT_ANSWER','EXIT_FLAG_AGENT_ANSWER')
 UNION ALL
 SELECT coalesce(lab.public_timestamp(e.body->'raised_at'), e.recorded_at), 'RESEARCH_AGENT',
  CASE WHEN e.body->'raised_by'->>'agent_id' ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$'
   THEN e.body->'raised_by'->>'agent_id' END,
  'EXIT_FLAG', 'EXIT', NULL, t.symbol, t.trade_no, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
  lab.public_line(e.body->'reasons'->'what_changed')
 FROM trades t JOIN lab.managed_events e ON e.setup_id=t.setup_id
  AND e.kind='EXIT_FLAG_RAISED' AND e.body->>'side'='AGENT';

REVOKE ALL ON lab.experiment_scope_setups, lab.experiment_run_facts, lab.experiment_fill_costs,
 lab.experiment_trade_facts, lab.experiment_pick_facts, lab.public_dashboard_status,
 lab.public_dashboard_trades, lab.public_dashboard_runs, lab.public_dashboard_decisions
 FROM PUBLIC;
REVOKE ALL ON FUNCTION lab.public_decimal(jsonb), lab.public_timestamp(jsonb),
 lab.public_line(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.public_decimal(jsonb), lab.public_timestamp(jsonb),
 lab.public_line(jsonb) TO catalyst_public;
GRANT SELECT ON lab.public_dashboard_status, lab.public_dashboard_trades,
 lab.public_dashboard_runs, lab.public_dashboard_decisions TO catalyst_public;
INSERT INTO lab.schema_migrations(version) VALUES(24);
