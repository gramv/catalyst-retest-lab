-- Public page V3 (package public-page-v3, 2026-10-03; owner: "live page doesn't show charts and
-- current amount"). Display only: no trading rule changes. DDL only: four new read-only views
-- granted to the existing catalyst_public role. No data row or event is written, no audited table
-- changes and every object of migrations 024 and 028 stays exactly as it is.
--
-- What the new views add, all already in the ledger (or appended by this release's engine):
-- * the account's equity series: ACCOUNT_EQUITY_SNAPSHOT_V1, one record-only managed event at
--   most every five minutes from the account read the trader's account safety tick already
--   makes (equity, cash, long market value, the positions' unrealized P&L, last_equity);
-- * every New York day's starting equity: RISK_SESSION_BASELINE_V2's basis when recorded, else
--   the lab.risk_sessions row (the page reconstructs the curve before the first snapshot from
--   these and the closed trades' P&L);
-- * the account's daily loss halts and soft-limit latches (a latch withdrawn by
--   DAILY_SOFT_LOSS_LIMIT_WITHDRAWN is not shown), as markers on the equity curve;
-- * STATS_EXCLUSION_V1 records (owner ruling 2026-10-03): which trades the performance
--   statistics leave out, by trade number, with the day and the reason code.
--
-- As in 024 and 028: codes pass only in their code shape, times and amounts only through the
-- public helpers, and no identifier (setup ID, account ID, order ID), request or response body,
-- token or raw answer is exposed. Engineering enrollments stay out (the 024 scope views).

CREATE VIEW lab.public_page_equity AS
 SELECT coalesce(lab.public_timestamp(e.body->'observed_at'), e.recorded_at) AS at,
  lab.public_decimal(e.body->'equity') AS equity,
  lab.public_decimal(e.body->'cash') AS cash,
  lab.public_decimal(e.body->'long_market_value') AS long_market_value,
  lab.public_decimal(e.body->'unrealized_pl') AS unrealized_pl,
  lab.public_decimal(e.body->'last_equity') AS last_equity
 FROM lab.managed_events e
 WHERE e.kind='ACCOUNT_EQUITY_SNAPSHOT' AND e.setup_id IS NULL
  AND e.body->>'version'='ACCOUNT_EQUITY_SNAPSHOT_V1'
  AND lab.public_decimal(e.body->'equity')>0;

CREATE VIEW lab.public_page_day_starts AS
 WITH v2 AS (
  SELECT (regexp_match(e.idempotency_key,
    '^risk-session-baseline-v2:([0-9]{4}-[0-9]{2}-[0-9]{2})$'))[1]::date AS day,
   lab.public_decimal(e.body->'day_start_equity') AS equity,
   coalesce(lab.public_timestamp(e.body->'observed_at'), e.recorded_at) AS observed_at
  FROM lab.managed_events e
  WHERE e.kind='RISK_SESSION_BASELINE_V2' AND e.setup_id IS NULL
   AND e.idempotency_key ~ '^risk-session-baseline-v2:[0-9]{4}-[0-9]{2}-[0-9]{2}$'
 )
 SELECT coalesce(v.day, s.session_date) AS day,
  coalesce(v.equity, s.day_start_equity) AS day_start_equity,
  coalesce(v.observed_at, s.observed_at) AS observed_at,
  CASE WHEN v.day IS NOT NULL THEN 'ACCOUNT_EQUITY_AT_NY_MIDNIGHT_V2'
   ELSE 'ALPACA_LAST_EQUITY' END AS basis,
  s.day_start_equity AS session_row_equity
 FROM v2 v FULL JOIN lab.risk_sessions s ON s.session_date=v.day;

CREATE VIEW lab.public_page_account_marks AS
 SELECT 'DAILY_HALT'::text AS kind, h.session_date AS day, e.created_at AS at,
  h.realized_pnl+h.unrealized_pnl AS pnl, h.threshold
 FROM lab.daily_risk_halts h JOIN lab.trade_events e ON e.seq=h.event_seq
 UNION ALL
 SELECT 'SOFT_LIMIT'::text, (l.body->>'session_date')::date, l.recorded_at,
  lab.public_decimal(l.body->'total_pnl'), lab.public_decimal(l.body->'threshold')
 FROM lab.managed_events l
 WHERE l.kind='DAILY_SOFT_LOSS_LIMIT' AND l.setup_id IS NULL
  AND l.body->>'session_date' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
  AND NOT EXISTS(SELECT 1 FROM lab.managed_events d WHERE d.setup_id IS NULL
   AND d.idempotency_key='daily-soft-loss-limit-correction:'||l.event_seq
   AND d.kind='DAILY_SOFT_LOSS_LIMIT_WITHDRAWN');

CREATE VIEW lab.public_page_stats_exclusions AS
 SELECT t.trade_no, x.day, x.reason, x.recorded_at, x.version
 FROM (
  SELECT CASE WHEN e.body->>'day' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
    THEN (e.body->>'day')::date END AS day,
   CASE WHEN e.body->>'reason' ~ '^[A-Z][A-Z0-9_-]{2,63}$' THEN e.body->>'reason' END AS reason,
   e.recorded_at, 'STATS_EXCLUSION_V1'::text AS version, s.setup_id
  FROM lab.managed_events e
  CROSS JOIN LATERAL jsonb_array_elements_text(CASE WHEN jsonb_typeof(e.body->'setup_ids')='array'
   THEN e.body->'setup_ids' ELSE '[]'::jsonb END) s(setup_id)
  WHERE e.kind='STATS_EXCLUSION' AND e.setup_id IS NULL
   AND e.body->>'version'='STATS_EXCLUSION_V1'
 ) x JOIN lab.experiment_trade_facts t ON t.setup_id::text=x.setup_id;

REVOKE ALL ON lab.public_page_equity, lab.public_page_day_starts, lab.public_page_account_marks,
 lab.public_page_stats_exclusions FROM PUBLIC;
GRANT SELECT ON lab.public_page_equity, lab.public_page_day_starts, lab.public_page_account_marks,
 lab.public_page_stats_exclusions TO catalyst_public;
INSERT INTO lab.schema_migrations(version) VALUES(29);
