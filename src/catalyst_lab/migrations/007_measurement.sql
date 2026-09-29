-- Derived projections are rebuilt only by a fixed SQL function. Source ledgers remain append-only.
ALTER TABLE lab.market_snapshots ADD COLUMN high numeric, ADD COLUMN low numeric;
CREATE INDEX snapshots_trade_window ON lab.market_snapshots(candidate_id,market_data_timestamp);
CREATE TABLE lab.exchange_sessions (
 event_seq bigint PRIMARY KEY REFERENCES lab.trade_events(seq), session_date date NOT NULL,
 opens_at timestamptz NOT NULL, closes_at timestamptz NOT NULL,
 data_provider text NOT NULL CHECK(data_provider='ALPACA'), CHECK(opens_at<closes_at)
);
CREATE VIEW lab.current_exchange_sessions AS
 SELECT DISTINCT ON(session_date) * FROM lab.exchange_sessions ORDER BY session_date,event_seq DESC;
CREATE FUNCTION lab.require_calendar_event() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM lab.trade_events WHERE seq=NEW.event_seq
   AND payload_json->>'kind'='EXCHANGE_CALENDAR_OBSERVED'
   AND (payload_json->>'session_date')::date=NEW.session_date
   AND (payload_json->>'opens')::timestamptz=NEW.opens_at
   AND (payload_json->>'closes')::timestamptz=NEW.closes_at
   AND payload_json->>'data_provider'=NEW.data_provider) THEN
  RAISE EXCEPTION 'Calendar must match its immutable source event';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER require_calendar_event BEFORE INSERT ON lab.exchange_sessions
 FOR EACH ROW EXECUTE FUNCTION lab.require_calendar_event();

CREATE FUNCTION lab.require_measurement_event() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
DECLARE source lab.trade_events; obs jsonb; quote jsonb; expected_price numeric;
BEGIN
 SELECT * INTO source FROM lab.trade_events WHERE event_id=NEW.event_id;
 IF source.candidate_id IS DISTINCT FROM NEW.candidate_id OR
    source.payload_json->>'kind' NOT IN ('WATCH_EVALUATION','TRADE_MARKET_OBSERVATION') THEN
  RAISE EXCEPTION 'Snapshot must match its immutable source event';
 END IF;
 obs := source.payload_json->'observation';
 quote := CASE WHEN source.payload_json->'quote' IS NOT NULL
    AND source.payload_json->'quote'<>'null'::jsonb THEN source.payload_json->'quote' ELSE obs END;
 expected_price := coalesce((obs->>'price')::numeric,
   ((obs->>'bid')::numeric+(obs->>'ask')::numeric)/2);
 IF obs IS NULL OR obs='null'::jsonb
   OR NEW.price IS DISTINCT FROM expected_price
   OR NEW.market_data_timestamp IS DISTINCT FROM (obs->>'provider_timestamp')::timestamptz
   OR NEW.provider_timestamp IS DISTINCT FROM obs->>'provider_timestamp'
   OR NEW.data_feed IS DISTINCT FROM obs->>'data_feed'
   OR NEW.data_provider IS DISTINCT FROM obs->>'data_provider'
   OR NEW.observation_type IS DISTINCT FROM obs->>'kind'
   OR NEW.observation_key IS DISTINCT FROM source.payload_json->>'observation_key'
   OR NEW.volume IS DISTINCT FROM (obs->>'volume')::numeric
   OR NEW.bid IS DISTINCT FROM (quote->>'bid')::numeric
   OR NEW.ask IS DISTINCT FROM (quote->>'ask')::numeric
   OR (source.payload_json->>'kind'='TRADE_MARKET_OBSERVATION' AND
     (NEW.high IS DISTINCT FROM (obs->>'high')::numeric OR NEW.low IS DISTINCT FROM (obs->>'low')::numeric)) THEN
  RAISE EXCEPTION 'Snapshot must match its immutable source event';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER require_measurement_event BEFORE INSERT ON lab.market_snapshots
 FOR EACH ROW EXECUTE FUNCTION lab.require_measurement_event();

CREATE TABLE lab.measurement_runs (
 event_seq bigint PRIMARY KEY REFERENCES lab.trade_events(seq), source_event_seq bigint NOT NULL,
 projected_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
DO $$ DECLARE name text; BEGIN
 FOREACH name IN ARRAY ARRAY['exchange_sessions','measurement_runs'] LOOP
  EXECUTE format('CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON lab.%I
    FOR EACH ROW EXECUTE FUNCTION lab.reject_mutation()',name);
  EXECUTE format('CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON lab.%I
    FOR EACH STATEMENT EXECUTE FUNCTION lab.reject_mutation()',name);
 END LOOP;
END $$;

ALTER TABLE lab.trades
 ADD COLUMN ticker text, ADD COLUMN session_date date, ADD COLUMN catalyst text,
 ADD COLUMN entry_qty numeric, ADD COLUMN exit_qty numeric, ADD COLUMN open_qty numeric,
 ADD COLUMN entry_price numeric, ADD COLUMN exit_price numeric, ADD COLUMN stop_price numeric,
 ADD COLUMN planned_filled_risk numeric, ADD COLUMN authorized_order_risk numeric,
 ADD COLUMN r_actual_filled numeric, ADD COLUMN r_planned_filled numeric,
 ADD COLUMN r_authorized_order numeric, ADD COLUMN snapshot_count bigint,
 ADD COLUMN coverage_json jsonb, ADD COLUMN feeds_json jsonb,
 ADD COLUMN source_event_seq bigint, ADD COLUMN projected_at timestamptz,
 ADD COLUMN realized_pnl numeric, ADD COLUMN last_mark numeric, ADD COLUMN marked_at timestamptz,
 ADD COLUMN open_unrealized_pnl numeric, ADD COLUMN pnl_quality text;

CREATE VIEW lab.trade_projection_source AS
WITH executions AS (
 SELECT o.candidate_id, sum((b.payload_json->>'qty')::numeric) FILTER(WHERE f.side='buy') AS bought,
  coalesce(sum((b.payload_json->>'qty')::numeric) FILTER(WHERE f.side='sell'),0) AS sold,
  sum((b.payload_json->>'qty')::numeric*(b.payload_json->>'price')::numeric)
    FILTER(WHERE f.side='buy') AS cost,
  coalesce(sum((b.payload_json->>'qty')::numeric*(b.payload_json->>'price')::numeric)
    FILTER(WHERE f.side='sell'),0) AS proceeds,
  min(f.timestamp) FILTER(WHERE f.side='buy') AS opened,
  max(f.timestamp) FILTER(WHERE f.side='sell') AS last_exit,
  max(e.seq) AS fill_seq
 FROM lab.fills f JOIN lab.orders o USING(order_id)
 JOIN lab.trade_events e ON e.event_id=f.event_id
 JOIN lab.broker_events b ON b.event_seq=e.seq
 WHERE f.execution_source='ALPACA_PAPER' AND b.broker_event_type IN ('fill','partial_fill')
   AND e.payload_json->'message'=b.payload_json
 GROUP BY o.candidate_id
), basis AS (
 SELECT x.*,c.ticker,c.session_date,c.strategy_version,c.record_purpose,
  c.payload_json->>'catalyst' AS catalyst, x.cost/nullif(x.bought,0) AS entry,
  x.proceeds/nullif(x.sold,0) AS exit,
  (d.payload_json->'stop_loss'->>'stop_price')::numeric AS stop,
  (d.payload_json->>'limit_price')::numeric AS maximum_entry,
  d.planned_risk AS authorized_risk,s.state,e.payload_json->>'reason' AS exit_reason,
  greatest(x.fill_seq,s.last_event_seq) AS source_seq,
  EXISTS(SELECT 1 FROM lab.broker_events corrected JOIN lab.order_links link
    ON link.broker_order_id=corrected.broker_order_id
    WHERE link.order_id=o.order_id AND corrected.broker_event_type IN ('trade_bust','trade_correct'))
    AS disputed
 FROM executions x JOIN lab.candidates c USING(candidate_id)
 JOIN lab.orders o USING(candidate_id)
 JOIN lab.risk_decisions d ON d.candidate_id=c.candidate_id AND d.action='ENTRY'
   AND d.decision='APPROVED'
 JOIN lab.candidate_states s ON s.candidate_id=c.candidate_id
 JOIN lab.trade_events e ON e.seq=s.last_event_seq
 WHERE x.bought>0
), observations AS (
 SELECT m.* FROM basis b JOIN lab.market_snapshots m USING(candidate_id)
 WHERE m.market_data_timestamp>=b.opened
   AND m.market_data_timestamp<=CASE WHEN b.sold=b.bought THEN b.last_exit ELSE clock_timestamp() END
   AND (m.observation_type='trade' OR
    (m.observation_type IN ('bar','bar_update') AND m.high IS NOT NULL AND m.low IS NOT NULL
      AND m.market_data_timestamp+interval '1 minute'<=
        CASE WHEN b.sold=b.bought THEN b.last_exit ELSE clock_timestamp() END))
), latest_observations AS (
 -- Corrected minute bars supersede earlier bars for measurement only, never for the trigger.
 SELECT DISTINCT ON(candidate_id,observation_type IN ('bar','bar_update'),
     CASE WHEN observation_type IN ('bar','bar_update') THEN market_data_timestamp::text
          ELSE observation_key END) * FROM observations
 ORDER BY candidate_id,observation_type IN ('bar','bar_update'),
     CASE WHEN observation_type IN ('bar','bar_update') THEN market_data_timestamp::text
          ELSE observation_key END,observed_at DESC,snapshot_id
), extremes AS (
 SELECT m.candidate_id,count(*) AS n,
  max(coalesce(high,price)) AS peak,min(coalesce(low,price)) AS trough,
  min(market_data_timestamp) AS first_at,max(market_data_timestamp) AS last_at,
  count(DISTINCT date_trunc('minute',market_data_timestamp)) AS observed_minutes,
  jsonb_agg(DISTINCT jsonb_build_object('data_provider',data_provider,'data_feed',data_feed,
    'resolution',observation_resolution)) AS feeds,
  max(e.seq) AS snapshot_seq
 FROM latest_observations m JOIN lab.trade_events e ON e.event_id=m.event_id GROUP BY m.candidate_id
)
SELECT b.*,
 CASE WHEN b.bought=b.sold THEN b.proceeds-b.cost END AS gross_pnl,
 b.sold*(b.exit-b.entry) AS realized,
 b.bought*(b.entry-b.stop) AS actual_risk,
 b.bought*(b.maximum_entry-b.stop) AS planned_risk,
 CASE WHEN z.n>0 THEN greatest(0,z.peak-b.entry) END AS favorable_per_share,
 CASE WHEN z.n>0 THEN least(0,z.trough-b.entry) END AS adverse_per_share,
 coalesce(z.n,0) AS samples, coalesce(z.feeds,'[]'::jsonb) AS feeds,
 jsonb_build_object('status',CASE WHEN z.n>0 THEN 'OBSERVED_ONLY' ELSE 'NO_OBSERVATIONS' END,
  'first_observation',z.first_at,'last_observation',z.last_at,
  'observed_minutes',coalesce(z.observed_minutes,0),
  'note','Only in-position trades and fully contained minute bars. Missing observations are not zero.')
  AS coverage,
 greatest(b.source_seq,coalesce(z.snapshot_seq,0)) AS through_seq,
 mark.price AS last_mark,mark.market_data_timestamp AS marked_at,
 spread.spread_bps AS entry_spread
FROM basis b LEFT JOIN extremes z USING(candidate_id)
LEFT JOIN LATERAL (
 SELECT price,market_data_timestamp FROM lab.market_snapshots m
 WHERE m.candidate_id=b.candidate_id AND m.market_data_timestamp>=b.opened
   AND m.market_data_timestamp<=clock_timestamp() AND m.observation_type IN ('trade','quote')
 ORDER BY market_data_timestamp DESC LIMIT 1
) mark ON true
LEFT JOIN LATERAL (
 SELECT spread_bps FROM lab.market_snapshots m WHERE m.candidate_id=b.candidate_id
 AND m.observation_type='quote' AND m.market_data_timestamp<=b.opened
 AND m.market_data_timestamp>=b.opened-interval '5 seconds'
 ORDER BY market_data_timestamp DESC LIMIT 1
) spread ON true;

CREATE FUNCTION lab.refresh_measurements() RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE watermark bigint; previous bigint; recorded lab.trade_events;
BEGIN
 -- Use the same lock/order as fills so a committed projection has one coherent source boundary.
 PERFORM pg_advisory_xact_lock(719172026);
 SELECT greatest(coalesce((SELECT max(e.seq) FROM lab.fills f JOIN lab.trade_events e
  ON e.event_id=f.event_id),0),coalesce((SELECT max(e.seq) FROM lab.market_snapshots m
  JOIN lab.trade_events e ON e.event_id=m.event_id JOIN lab.orders o ON o.candidate_id=m.candidate_id),0),
  coalesce((SELECT max(s.last_event_seq) FROM lab.candidate_states s
   JOIN lab.orders o USING(candidate_id)),0),coalesce((SELECT max(event_seq)
    FROM lab.broker_events WHERE broker_event_type IN ('trade_bust','trade_correct')),0)) INTO watermark;
 SELECT source_event_seq INTO previous FROM lab.measurement_runs ORDER BY event_seq DESC LIMIT 1;
 IF watermark=coalesce(previous,0) THEN RETURN coalesce(previous,0); END IF;
 -- trades is disposable materialization; direct application INSERT/UPDATE/DELETE stays denied.
 DELETE FROM lab.trades;
 INSERT INTO lab.trades(trade_id,candidate_id,strategy_version,execution_source,market,status,
  opened_at,closed_at,exit_reason,broker_paper_pnl,initial_risk_dollars,mfe,mae,observed_spread,
  last_event_seq,record_purpose,ticker,session_date,catalyst,entry_qty,exit_qty,open_qty,
  entry_price,exit_price,stop_price,planned_filled_risk,authorized_order_risk,
  r_actual_filled,r_planned_filled,r_authorized_order,snapshot_count,coverage_json,feeds_json,
  source_event_seq,projected_at,realized_pnl,last_mark,marked_at,open_unrealized_pnl,pnl_quality)
 SELECT candidate_id,candidate_id,strategy_version,'ALPACA_PAPER','US',
  CASE WHEN disputed THEN 'REVIEW_REQUIRED'
       WHEN bought=sold AND state='CLOSED' THEN 'CLOSED' ELSE 'OPEN' END,
  opened,CASE WHEN bought=sold AND state='CLOSED' THEN last_exit END,exit_reason,
  CASE WHEN state='CLOSED' AND NOT disputed THEN gross_pnl END,actual_risk,
  favorable_per_share,adverse_per_share,entry_spread,through_seq,record_purpose,
  ticker,session_date,catalyst,bought,sold,bought-sold,entry,exit,stop,planned_risk,authorized_risk,
  CASE WHEN actual_risk>0 AND state='CLOSED' AND NOT disputed THEN gross_pnl/actual_risk END,
  CASE WHEN planned_risk>0 AND state='CLOSED' AND NOT disputed THEN gross_pnl/planned_risk END,
  CASE WHEN authorized_risk>0 AND state='CLOSED' AND NOT disputed THEN gross_pnl/authorized_risk END,
  samples,coverage,feeds,through_seq,clock_timestamp(),coalesce(realized,0),last_mark,marked_at,
  CASE WHEN bought>sold AND marked_at>=clock_timestamp()-interval '5 seconds'
    THEN (bought-sold)*(last_mark-entry) END,
    CASE WHEN disputed THEN 'BROKER_CORRECTION_REVIEW_REQUIRED'
         ELSE 'GROSS_BROKER_FILLS_FEES_NOT_MODELLED' END
 FROM lab.trade_projection_source;
 INSERT INTO lab.trade_events(strategy_version,event_type,payload_json)
 VALUES('CATALYST_RETEST_V1','SYSTEM_EVENT',jsonb_build_object('kind','MEASUREMENTS_REFRESHED',
  'source_event_seq',watermark,'trade_count',(SELECT count(*) FROM lab.trades))) RETURNING * INTO recorded;
 INSERT INTO lab.measurement_runs(event_seq,source_event_seq) VALUES(recorded.seq,watermark);
 RETURN watermark;
END $$;

-- Public selection is delayed by the persisted exchange calendar, never a fixed 16:00 rule.
CREATE VIEW lab.reporting_candidates AS
 SELECT c.candidate_id,c.strategy_version,c.ticker,c.session_date,c.received_at,
  c.payload_json->>'catalyst' AS catalyst,c.payload_json->>'thesis' AS thesis,
  c.payload_json->>'disproof' AS disproof,c.payload_json->>'entry_trigger' AS entry_trigger,
  c.payload_json->>'max_entry_price' AS max_entry_price,c.payload_json->>'stop' AS stop,
  c.payload_json->>'target' AS target,s.state,
  coalesce(d.failed_rule,e.payload_json->>'reason',d.reason) AS reason,
  s.last_event_seq,cal.closes_at AS publish_after,
  c.received_at>cal.closes_at AS late_added
 FROM lab.strategy_candidates c JOIN lab.candidate_states s USING(candidate_id)
 JOIN lab.trade_events e ON e.seq=s.last_event_seq
 JOIN lab.validation_decisions d ON d.candidate_id=c.candidate_id
 LEFT JOIN lab.current_exchange_sessions cal ON cal.session_date=c.session_date;
CREATE VIEW lab.public_candidates AS SELECT * FROM lab.reporting_candidates
 WHERE publish_after<=clock_timestamp();
CREATE VIEW lab.public_trades AS SELECT t.* FROM lab.trades t JOIN lab.public_candidates c USING(candidate_id)
 WHERE t.record_purpose='STRATEGY' AND t.execution_source='ALPACA_PAPER' AND t.market='US';
-- Recreate the filtered view to expose the new projection columns.
CREATE OR REPLACE VIEW lab.strategy_trades AS SELECT t.* FROM lab.trades t
 JOIN lab.strategy_candidates c USING(candidate_id);
CREATE VIEW lab.reporting_status AS SELECT
 (SELECT max(projected_at) FROM lab.measurement_runs) AS projected_at,
 (SELECT max(source_event_seq) FROM lab.measurement_runs) AS through_event_seq,
 (SELECT event_hash FROM lab.trade_events ORDER BY seq DESC LIMIT 1) AS audit_head,
 (SELECT count(*) FROM lab.trade_events) AS audit_events;
CREATE VIEW lab.reporting_baseline AS SELECT session_date,day_start_equity AS equity,source
 FROM lab.risk_sessions ORDER BY session_date LIMIT 1;

-- Manual research lives in its own ledger. It cannot create US orders, candidates or fills.
CREATE TABLE lab.research_results (
 event_seq bigint PRIMARY KEY REFERENCES lab.trade_events(seq), external_id text NOT NULL,
 market text NOT NULL CHECK(market IN ('INDIA','CRYPTO')), strategy_version text NOT NULL,
 execution_source text NOT NULL DEFAULT 'MUSE_MANUAL' CHECK(execution_source='MUSE_MANUAL'),
 payload_json jsonb NOT NULL, received_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON lab.research_results
 FOR EACH ROW EXECUTE FUNCTION lab.reject_mutation();
CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON lab.research_results
 FOR EACH STATEMENT EXECUTE FUNCTION lab.reject_mutation();
CREATE VIEW lab.current_research_results AS SELECT DISTINCT ON(market,external_id) *
 FROM lab.research_results ORDER BY market,external_id,event_seq DESC;
CREATE VIEW lab.public_research_results AS SELECT * FROM lab.current_research_results
 WHERE (payload_json->>'closed_at')::timestamptz <
   (date_trunc('day',clock_timestamp() AT TIME ZONE 'America/New_York') AT TIME ZONE 'America/New_York');
-- Preserve version attribution for research-only events; execution strategy remains frozen.
DO $$ DECLARE definition text; old text; replacement text; BEGIN
 old := 'IF NEW.strategy_version <> ''CATALYST_RETEST_V1'' THEN';
 replacement := 'IF NEW.strategy_version <> ''CATALYST_RETEST_V1'' AND NOT (
   NEW.candidate_id IS NULL AND NEW.event_type IN (''SYSTEM_EVENT'',''CORRECTION'')
   AND NEW.payload_json->>''kind''=''RESEARCH_RESULT''
   AND NEW.payload_json->>''execution_source''=''MUSE_MANUAL''
   AND NEW.payload_json->>''market'' IN (''INDIA'',''CRYPTO'')) THEN';
 SELECT pg_get_functiondef('lab.stamp_event()'::regprocedure) INTO definition;
 IF strpos(definition,old)=0 THEN RAISE EXCEPTION 'unexpected audit function version'; END IF;
 EXECUTE replace(definition,old,replacement);
END $$;
CREATE FUNCTION lab.require_research_event() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,lab AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM lab.trade_events WHERE seq=NEW.event_seq
   AND strategy_version=NEW.strategy_version AND candidate_id IS NULL
   AND payload_json->>'kind'='RESEARCH_RESULT' AND payload_json->>'execution_source'='MUSE_MANUAL'
   AND payload_json->>'market'=NEW.market AND payload_json->'record'=NEW.payload_json) THEN
  RAISE EXCEPTION 'Research must match its immutable source event';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER require_research_event BEFORE INSERT ON lab.research_results
 FOR EACH ROW EXECUTE FUNCTION lab.require_research_event();

CREATE FUNCTION lab.rollup_session(day date) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE version text; metrics jsonb; source_seq bigint; rev integer; recorded lab.trade_events;
BEGIN
 IF NOT EXISTS(SELECT 1 FROM lab.current_exchange_sessions
   WHERE session_date=day AND closes_at<=clock_timestamp()) THEN
  RAISE EXCEPTION 'An official completed session is required';
 END IF;
 PERFORM lab.refresh_measurements();
 SELECT coalesce(max(source_event_seq),0) INTO source_seq FROM lab.measurement_runs;
 FOR version IN SELECT DISTINCT strategy_version FROM lab.strategy_candidates WHERE session_date=day LOOP
  SELECT jsonb_build_object('closed_trades',count(*) FILTER(WHERE status='CLOSED'),
   'open_trades',count(*) FILTER(WHERE status='OPEN'),
   'review_required_trades',count(*) FILTER(WHERE status='REVIEW_REQUIRED'),
   'wins',count(*) FILTER(WHERE status='CLOSED' AND broker_paper_pnl>0),
   'gross_pnl',coalesce(sum(broker_paper_pnl) FILTER(WHERE status='CLOSED'),0),
   'r_actual_filled',sum(r_actual_filled) FILTER(WHERE status='CLOSED'),
   'r_planned_filled',sum(r_planned_filled) FILTER(WHERE status='CLOSED'),
   'r_authorized_order',sum(r_authorized_order) FILTER(WHERE status='CLOSED'),
   'missing_excursions',count(*) FILTER(WHERE snapshot_count=0)) INTO metrics
   FROM lab.strategy_trades WHERE session_date=day AND strategy_version=version;
  IF (metrics->>'review_required_trades')::integer>0 THEN
   metrics := metrics || jsonb_build_object('gross_pnl',NULL,'wins',NULL,
     'r_actual_filled',NULL,'r_planned_filled',NULL,'r_authorized_order',NULL);
  END IF;
  metrics := metrics || jsonb_build_object('candidates',(SELECT count(*) FROM lab.strategy_candidates
    WHERE session_date=day AND strategy_version=version),'engineering_excluded',true,
    'pnl_basis','GROSS_BROKER_FILLS_FEES_NOT_MODELLED');
  IF (SELECT metrics_json FROM lab.daily_stats WHERE session_date=day AND strategy_version=version
    AND execution_source='ALPACA_PAPER' AND market='US' ORDER BY revision DESC LIMIT 1)=metrics
    THEN CONTINUE; END IF;
  SELECT coalesce(max(revision),0)+1 INTO rev FROM lab.daily_stats WHERE session_date=day
    AND strategy_version=version AND execution_source='ALPACA_PAPER' AND market='US';
  INSERT INTO lab.trade_events(strategy_version,event_type,payload_json)
   VALUES(version,'SYSTEM_EVENT',jsonb_build_object('kind','DAILY_ROLLUP','session_date',day,
     'revision',rev,'metrics',metrics,'source_event_seq',source_seq)) RETURNING * INTO recorded;
  INSERT INTO lab.daily_stats VALUES(day,version,'ALPACA_PAPER','US',rev,metrics,recorded.seq,clock_timestamp());
 END LOOP;
 RETURN coalesce(rev,0);
END $$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA lab FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA lab FROM PUBLIC;
GRANT SELECT ON ALL TABLES IN SCHEMA lab TO catalyst_app;
GRANT INSERT ON lab.exchange_sessions TO catalyst_app;
GRANT INSERT ON lab.research_results TO catalyst_app;
GRANT EXECUTE ON FUNCTION lab.refresh_measurements(),lab.rollup_session(date) TO catalyst_app,catalyst_risk;
ALTER ROLE catalyst_reporting LOGIN;
GRANT SELECT ON lab.public_candidates,lab.public_trades,lab.reporting_status,lab.current_exchange_sessions,
 lab.measurement_runs,lab.reporting_baseline,lab.daily_stats TO catalyst_reporting;
GRANT SELECT ON lab.public_research_results TO catalyst_reporting;
GRANT SELECT ON lab.strategy_trades TO catalyst_reporting;
INSERT INTO lab.schema_migrations(version) VALUES(7);
