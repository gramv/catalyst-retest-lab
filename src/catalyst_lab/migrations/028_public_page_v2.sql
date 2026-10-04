-- Public page V2 (package public-page, 2026-10-03; docs/TRADING-QUALITY-PLAN.md section 11, items
-- 1-5, owner-approved design). Display only: no trading rule changes. DDL only: five new read-only
-- views granted to the existing catalyst_public role. No data row or event is written, no audited
-- table changes and every object of migration 024 stays exactly as it is (the old page's views
-- keep their meaning: their planned_stop is the research stop).
--
-- What the new views add, all already in the ledger:
-- * the levels actually traded: CRYPTO_TRADE_PLAN_V1's plan stop and target (recorded in the
--   setup's state at admission, migration 027), with its rule bases and hourly range, and the
--   risk the reservation guard plans on (qty x (max entry - least(research stop, plan stop)),
--   exactly lab.managed_planned_stop);
-- * each trade's recorded rule versions (risk policy, trade plan, maintenance, entry pacing,
--   stop-limit, holding) and its MARKET_REGIME_V1 tag at entry (TRADE_REGIME);
-- * the entry waits before each trade's buy (CRYPTO_ENTRY_PACING_WAIT,
--   DAILY_SOFT_LOSS_ENTRY_WAIT), folded per reason;
-- * the account's state now: the newest admitted setup's risk policy with its numbers (risk per
--   trade, the crypto cap, the daily soft and hard limits), the New York day's starting equity
--   (RISK_SESSION_BASELINE_V2's basis when recorded, the one JEV_MANAGED_RISK_V4 measures the
--   day from; else the lab.risk_sessions row), the day's baseline correction, today's soft-limit
--   latch still in force (a latch withdrawn by DAILY_SOFT_LOSS_LIMIT_WITHDRAWN is not) and its
--   kept/withdrawn decisions, the daily halt, unreleased execution halts, the last positive
--   account equity a risk decision recorded, the latest entry waits, and the rule versions new
--   setups record now with the time the first setup recorded them;
-- * CRYPTO_MAINTENANCE_V5's yes/no answers per review (probability, verdict, streak);
-- * each New York day's MARKET_REGIME_V1 tag and its worst hour.
--
-- As in 024: codes pass only in their code shape, times and amounts only through the public
-- helpers, and no identifier, account detail, order ID, request or response body, token or raw
-- Jev answer is exposed. Engineering enrollments stay out (the 024 scope views).

-- Internal helper (granted to no role): a scope setup's recorded rule versions, code-shaped.
CREATE VIEW lab.experiment_setup_versions AS
 SELECT x.setup_id, x.setup_seq, x.state,
  lab.public_timestamp(x.state->'admitted_at') AS admitted_at,
  CASE WHEN x.state->>'risk_policy_id' ~ '^[A-Z][A-Z0-9_]{1,63}$'
   THEN x.state->>'risk_policy_id' END AS risk_policy,
  CASE WHEN x.state->'trade_plan'->'policy'->>'policy_id' ~ '^[A-Z][A-Z0-9_]{1,63}$'
   THEN x.state->'trade_plan'->'policy'->>'policy_id' END AS trade_plan_policy,
  CASE WHEN x.state->'maintenance_policy'->>'policy_id' ~ '^[A-Z][A-Z0-9_]{1,63}$'
   THEN x.state->'maintenance_policy'->>'policy_id' END AS maintenance_policy,
  CASE WHEN x.state->>'entry_pacing_version' ~ '^[A-Z][A-Z0-9_]{1,63}$'
   THEN x.state->>'entry_pacing_version' END AS entry_pacing_policy,
  CASE WHEN x.state->'stop_limit_policy'->>'policy_id' ~ '^[A-Z][A-Z0-9_]{1,63}$'
   THEN x.state->'stop_limit_policy'->>'policy_id' END AS stop_limit_policy,
  CASE WHEN x.state->'holding_policy'->>'policy_id' ~ '^[A-Z][A-Z0-9_]{1,63}$'
   THEN x.state->'holding_policy'->>'policy_id' END AS holding_policy
 FROM lab.experiment_scope_setups x;

-- One row per trade (the trade numbers of lab.public_dashboard_trades).
CREATE VIEW lab.public_page_trades AS
 WITH planned AS (
  SELECT t.trade_no, t.setup_id, t.bought_qty, t.max_entry, t.planned_stop,
   CASE WHEN t.state->'trade_plan'->'policy'->>'policy_id'='CRYPTO_TRADE_PLAN_V1'
    THEN t.state->'trade_plan' END AS plan
  FROM lab.experiment_trade_facts t
 )
 SELECT p.trade_no, v.admitted_at, v.risk_policy, v.trade_plan_policy, v.maintenance_policy,
  v.entry_pacing_policy, v.stop_limit_policy, v.holding_policy,
  lab.public_decimal(p.plan->'stop') AS plan_stop,
  lab.public_decimal(p.plan->'target') AS plan_target,
  lab.public_decimal(p.plan->'target_cap') AS plan_target_cap,
  lab.public_decimal(p.plan->'hourly_range') AS hourly_range,
  lab.public_decimal(p.plan->'hourly_range_fraction') AS hourly_range_fraction,
  lab.public_decimal(p.plan->'range_floor') AS range_floor,
  CASE WHEN p.plan->>'stop_basis' ~ '^[A-Z][A-Z0-9_]{1,63}$' THEN p.plan->>'stop_basis' END
   AS stop_basis,
  CASE WHEN p.plan->>'target_basis' ~ '^[A-Z][A-Z0-9_]{1,63}$' THEN p.plan->>'target_basis' END
   AS target_basis,
  lab.public_decimal(p.plan->'policy'->'stop_range_multiple') AS stop_range_multiple,
  lab.public_decimal(p.plan->'policy'->'target_r_multiple') AS target_r_multiple,
  lab.public_decimal(p.plan->'policy'->'window_minutes') AS window_minutes,
  -- The stop the reservation guard plans on (lab.managed_planned_stop, migration 027).
  CASE WHEN lab.public_decimal(p.plan->'stop')>0 AND p.planned_stop IS NOT NULL
   THEN least(p.planned_stop, lab.public_decimal(p.plan->'stop')) ELSE p.planned_stop END
   AS risk_stop,
  CASE WHEN p.max_entry IS NOT NULL AND p.planned_stop IS NOT NULL
   AND p.max_entry > (CASE WHEN lab.public_decimal(p.plan->'stop')>0
    THEN least(p.planned_stop, lab.public_decimal(p.plan->'stop')) ELSE p.planned_stop END)
   THEN p.bought_qty*(p.max_entry-(CASE WHEN lab.public_decimal(p.plan->'stop')>0
    THEN least(p.planned_stop, lab.public_decimal(p.plan->'stop')) ELSE p.planned_stop END))
   END AS risk_usd,
  CASE WHEN g.body->>'day_tag' ~ '^[A-Z_]{2,16}(/[A-Z_]{2,16}){3}$' THEN g.body->>'day_tag' END
   AS regime_day_tag,
  CASE WHEN g.body->>'prior_day_tag' ~ '^[A-Z_]{2,16}(/[A-Z_]{2,16}){3}$'
   THEN g.body->>'prior_day_tag' END AS regime_prior_day_tag,
  CASE WHEN g.body->>'btc_1h' ~ '^[A-Z_0-9+]{2,16}$' THEN g.body->>'btc_1h' END AS regime_btc_1h,
  CASE WHEN g.body->>'btc_4h' ~ '^[A-Z_0-9+]{2,16}$' THEN g.body->>'btc_4h' END AS regime_btc_4h,
  CASE WHEN g.body->>'median_coin_1h' ~ '^[A-Z_0-9+]{2,16}$' THEN g.body->>'median_coin_1h' END
   AS regime_median_coin_1h,
  lab.public_decimal(g.body->'median_coin_1h_return_pct') AS regime_median_coin_1h_pct
 FROM planned p
 JOIN lab.experiment_setup_versions v ON v.setup_id=p.setup_id
 LEFT JOIN LATERAL (
  SELECT e.body FROM lab.managed_events e
  WHERE e.kind='TRADE_REGIME' AND e.setup_id IS NULL AND e.body->>'setup_id'=p.setup_id::text
  ORDER BY e.event_seq DESC LIMIT 1) g ON true;

-- CRYPTO_MAINTENANCE_V5's reviews: each asked question's probability, verdict and streak (the
-- answer rule's effect), joined by the page to lab.public_dashboard_decisions on (trade, time).
CREATE VIEW lab.public_page_reviews AS
 SELECT t.trade_no,
  coalesce(lab.public_timestamp(e.body->'decided_at'), e.recorded_at) AS at,
  CASE WHEN e.body->>'outcome' ~ '^[A-Z][A-Z0-9_]{1,63}$' THEN e.body->>'outcome' END AS outcome,
  CASE WHEN e.body->>'action' IN ('HOLD','CONFIRMING','FLAG_EARLY_EXIT')
   THEN e.body->>'action' END AS action,
  lab.public_decimal(coalesce(e.body->'verdicts', e.body->'answers')->'invalidation_met'->'p')
   AS invalidation_p,
  CASE WHEN coalesce(e.body->'verdicts', e.body->'answers')->'invalidation_met'->>'verdict'
   IN ('YES','NO','UNCERTAIN')
   THEN coalesce(e.body->'verdicts', e.body->'answers')->'invalidation_met'->>'verdict' END
   AS invalidation_verdict,
  CASE WHEN e.body->'verdicts'->'invalidation_met'->>'effect' ~ '^[A-Z][A-Z0-9_]{1,63}$'
   THEN e.body->'verdicts'->'invalidation_met'->>'effect' END AS invalidation_effect,
  lab.public_decimal(e.body->'verdicts'->'invalidation_met'->'streak') AS invalidation_streak,
  lab.public_decimal(coalesce(e.body->'verdicts', e.body->'answers')->'news_contradicts'->'p')
   AS news_p,
  CASE WHEN coalesce(e.body->'verdicts', e.body->'answers')->'news_contradicts'->>'verdict'
   IN ('YES','NO','UNCERTAIN')
   THEN coalesce(e.body->'verdicts', e.body->'answers')->'news_contradicts'->>'verdict' END
   AS news_verdict,
  CASE WHEN e.body->'verdicts'->'news_contradicts'->>'effect' ~ '^[A-Z][A-Z0-9_]{1,63}$'
   THEN e.body->'verdicts'->'news_contradicts'->>'effect' END AS news_effect,
  lab.public_decimal(e.body->'verdicts'->'news_contradicts'->'streak') AS news_streak
 FROM lab.experiment_trade_facts t
 JOIN lab.managed_events e ON e.setup_id=t.setup_id AND e.kind='MAINTENANCE_DECISION'
  AND e.body->>'policy_id'='CRYPTO_MAINTENANCE_V5';

-- A trade's entry waits before its first buy, folded per kind and reason.
CREATE VIEW lab.public_page_trade_waits AS
 SELECT t.trade_no,
  CASE e.kind WHEN 'CRYPTO_ENTRY_PACING_WAIT' THEN 'PACING' ELSE 'SOFT_LIMIT' END AS wait_kind,
  CASE WHEN e.body->>'reason' ~ '^[A-Z][A-Z0-9_]{1,63}$' THEN e.body->>'reason' END AS reason,
  min(coalesce(lab.public_timestamp(e.body->'waited_at'), e.recorded_at)) AS first_at,
  max(coalesce(lab.public_timestamp(e.body->'waited_at'), e.recorded_at)) AS last_at,
  count(*) AS waits,
  -- The worst median 1-hour return a market-drop wait recorded (a fraction, e.g. -0.031).
  min(lab.public_decimal(e.body->'detail'->'median_1h_return')) AS worst_median_1h_return,
  max(CASE WHEN e.body->'detail'->>'kind' IN ('CPI','FOMC') THEN e.body->'detail'->>'kind' END)
   AS release_kind
 FROM lab.experiment_trade_facts t
 JOIN lab.managed_events e ON e.setup_id=t.setup_id
  AND e.kind IN ('CRYPTO_ENTRY_PACING_WAIT','DAILY_SOFT_LOSS_ENTRY_WAIT')
  AND (t.entry_at IS NULL OR e.recorded_at<=t.entry_at)
 GROUP BY t.trade_no, 2, 3;

-- The account's state now, one row.
CREATE VIEW lab.public_page_status AS
 WITH today AS (
  SELECT (clock_timestamp() AT TIME ZONE 'America/New_York')::date AS day
 ), newest AS (
  SELECT v.* FROM lab.experiment_setup_versions v ORDER BY v.setup_seq DESC LIMIT 1
 ), policy AS (
  SELECT p.policy_id, p.risk_pct, p.account_cap_pct,
   lab.public_decimal(p.market_caps->'CRYPTO') AS crypto_cap_pct,
   l.hard_loss_pct, l.soft_loss_pct
  FROM lab.account_risk_policies p
  LEFT JOIN lab.account_risk_daily_limits l ON l.policy_id=p.policy_id
  WHERE p.policy_id=(SELECT risk_policy FROM newest)
 ), session AS (
  SELECT s.day_start_equity, s.observed_at FROM lab.risk_sessions s
  WHERE s.session_date=(SELECT day FROM today)
 ), baseline AS (
  -- RISK_SESSION_BASELINE_V2 (package baseline): key risk-session-baseline-v2:<NY date>.
  SELECT lab.public_decimal(e.body->'day_start_equity') AS day_start_equity,
   coalesce(lab.public_timestamp(e.body->'observed_at'), e.recorded_at) AS observed_at
  FROM lab.managed_events e
  WHERE e.kind='RISK_SESSION_BASELINE_V2' AND e.setup_id IS NULL
   AND e.idempotency_key='risk-session-baseline-v2:'||(SELECT day FROM today)::text
 ), corrected AS (
  SELECT e.recorded_at,
   lab.public_decimal(e.body->'old_basis'->'day_start_equity') AS old_equity,
   lab.public_decimal(e.body->'new_basis'->'day_start_equity') AS new_equity
  FROM lab.managed_events e
  WHERE e.kind='RISK_SESSION_BASELINE_CORRECTED' AND e.setup_id IS NULL
   AND e.idempotency_key='risk-session-baseline-corrected:'||(SELECT day FROM today)::text
 ), latches AS (
  -- Today's soft latches, each with its correction decision (withdrawn or kept), if any.
  SELECT e.event_seq, e.recorded_at, lab.public_decimal(e.body->'total_pnl') AS total_pnl,
   d.kind AS decision, d.recorded_at AS decided_at
  FROM lab.managed_events e
  LEFT JOIN lab.managed_events d ON d.setup_id IS NULL
   AND d.idempotency_key='daily-soft-loss-limit-correction:'||e.event_seq
   AND d.kind IN ('DAILY_SOFT_LOSS_LIMIT_WITHDRAWN','DAILY_SOFT_LOSS_LIMIT_KEPT')
  WHERE e.kind='DAILY_SOFT_LOSS_LIMIT' AND e.setup_id IS NULL
   AND e.body->>'session_date'=(SELECT day FROM today)::text
 ), soft AS (
  -- The latch still in force: the newest one not withdrawn.
  SELECT l.recorded_at, l.total_pnl, l.decision FROM latches l
  WHERE l.decision IS DISTINCT FROM 'DAILY_SOFT_LOSS_LIMIT_WITHDRAWN'
  ORDER BY l.event_seq DESC LIMIT 1
 ), withdrawn AS (
  SELECT l.recorded_at, l.decided_at FROM latches l
  WHERE l.decision='DAILY_SOFT_LOSS_LIMIT_WITHDRAWN' ORDER BY l.event_seq DESC LIMIT 1
 ), equity AS (
  -- The last positive account equity a risk decision recorded (protective decisions record 0).
  SELECT d.equity, d.created_at FROM lab.managed_risk_decisions d WHERE d.equity>0
  ORDER BY d.event_seq DESC LIMIT 1
 ), halt AS (
  SELECT h.realized_pnl, h.unrealized_pnl, h.threshold, e.created_at
  FROM lab.daily_risk_halts h JOIN lab.trade_events e ON e.seq=h.event_seq
  WHERE h.session_date=(SELECT day FROM today)
 ), execution_halt AS (
  SELECT CASE WHEN h.reason ~ '^[A-Z][A-Z0-9_]{1,63}$' THEN h.reason END AS reason,
   e.created_at
  FROM lab.execution_halts h JOIN lab.trade_events e ON e.seq=h.event_seq
  ORDER BY h.event_seq DESC LIMIT 1
 ), pacing AS (
  SELECT coalesce(lab.public_timestamp(e.body->'waited_at'), e.recorded_at) AS at,
   CASE WHEN e.body->>'reason' ~ '^[A-Z][A-Z0-9_]{1,63}$' THEN e.body->>'reason' END AS reason,
   CASE WHEN e.body->'detail'->>'kind' IN ('CPI','FOMC') THEN e.body->'detail'->>'kind' END
    AS release_kind,
   lab.public_timestamp(e.body->'detail'->'window_until') AS release_window_until,
   lab.public_decimal(e.body->'detail'->'median_1h_return') AS median_1h_return,
   lab.public_decimal(e.body->'detail'->'entries') AS recent_entries
  FROM lab.managed_events e WHERE e.kind='CRYPTO_ENTRY_PACING_WAIT'
  ORDER BY e.event_seq DESC LIMIT 1
 ), soft_wait AS (
  SELECT coalesce(lab.public_timestamp(e.body->'waited_at'), e.recorded_at) AS at
  FROM lab.managed_events e WHERE e.kind='DAILY_SOFT_LOSS_ENTRY_WAIT'
  ORDER BY e.event_seq DESC LIMIT 1
 )
 SELECT (SELECT day FROM today) AS ny_today,
  (SELECT policy_id FROM policy) AS risk_policy,
  (SELECT risk_pct FROM policy) AS risk_pct,
  (SELECT crypto_cap_pct FROM policy) AS crypto_cap_pct,
  -- Every policy without a daily-limits row halts at the engine's 3% literal
  -- (account_risk.LEGACY_DAILY_HARD_LOSS_PCT) and has no soft limit.
  CASE WHEN EXISTS(SELECT 1 FROM policy)
   THEN coalesce((SELECT hard_loss_pct FROM policy), 0.03) END AS hard_loss_pct,
  (SELECT soft_loss_pct FROM policy) AS soft_loss_pct,
  coalesce((SELECT day_start_equity FROM baseline), (SELECT day_start_equity FROM session))
   AS day_start_equity,
  coalesce((SELECT observed_at FROM baseline), (SELECT observed_at FROM session))
   AS day_start_observed_at,
  CASE WHEN EXISTS(SELECT 1 FROM baseline) THEN 'ACCOUNT_EQUITY_AT_NY_MIDNIGHT_V2'
   WHEN EXISTS(SELECT 1 FROM session) THEN 'ALPACA_LAST_EQUITY' END AS day_start_basis,
  (SELECT day_start_equity FROM session) AS session_row_equity,
  (SELECT recorded_at FROM corrected) AS baseline_corrected_at,
  (SELECT old_equity FROM corrected) AS baseline_corrected_from,
  (SELECT new_equity FROM corrected) AS baseline_corrected_to,
  (SELECT equity FROM equity) AS equity_usd,
  (SELECT created_at FROM equity) AS equity_at,
  (SELECT recorded_at FROM soft) AS soft_limit_at,
  (SELECT total_pnl FROM soft) AS soft_limit_pnl,
  (SELECT decision='DAILY_SOFT_LOSS_LIMIT_KEPT' FROM soft) AS soft_limit_kept,
  (SELECT recorded_at FROM withdrawn) AS soft_limit_withdrawn_latch_at,
  (SELECT decided_at FROM withdrawn) AS soft_limit_withdrawn_at,
  (SELECT created_at FROM halt) AS daily_halt_at,
  (SELECT realized_pnl+unrealized_pnl FROM halt) AS daily_halt_pnl,
  (SELECT count(*) FROM lab.execution_halts) AS execution_halts,
  (SELECT reason FROM execution_halt) AS execution_halt_reason,
  (SELECT created_at FROM execution_halt) AS execution_halt_at,
  (SELECT at FROM pacing) AS pacing_wait_at,
  (SELECT reason FROM pacing) AS pacing_wait_reason,
  (SELECT release_kind FROM pacing) AS pacing_release_kind,
  (SELECT release_window_until FROM pacing) AS pacing_release_until,
  (SELECT median_1h_return FROM pacing) AS pacing_median_1h_return,
  (SELECT recent_entries FROM pacing) AS pacing_recent_entries,
  (SELECT at FROM soft_wait) AS soft_wait_at,
  (SELECT admitted_at FROM newest) AS newest_admitted_at,
  (SELECT risk_policy FROM newest) AS rules_risk_policy,
  (SELECT trade_plan_policy FROM newest) AS rules_trade_plan_policy,
  (SELECT maintenance_policy FROM newest) AS rules_maintenance_policy,
  (SELECT entry_pacing_policy FROM newest) AS rules_entry_pacing_policy,
  (SELECT stop_limit_policy FROM newest) AS rules_stop_limit_policy,
  -- When the first setup recorded the newest setup's versions (the trade-plan, maintenance,
  -- pacing and stop-limit versions are recorded per arm, so only the account-wide ones count).
  (SELECT min(v.admitted_at) FROM lab.experiment_setup_versions v, newest n
   WHERE v.risk_policy IS NOT DISTINCT FROM n.risk_policy
    AND v.trade_plan_policy IS NOT DISTINCT FROM n.trade_plan_policy
    AND v.entry_pacing_policy IS NOT DISTINCT FROM n.entry_pacing_policy
    AND v.stop_limit_policy IS NOT DISTINCT FROM n.stop_limit_policy) AS rules_since;

-- Each New York day's MARKET_REGIME_V1 record (the latest one per day).
CREATE VIEW lab.public_page_regimes AS
 SELECT DISTINCT ON (e.body->>'day')
  e.body->>'day' AS day,
  CASE WHEN e.body->>'tag' ~ '^[A-Z_]{2,16}(/[A-Z_]{2,16}){3}$' THEN e.body->>'tag' END AS tag,
  lab.public_timestamp(e.body->'selloff'->'worst_hour_start') AS worst_hour_start,
  lab.public_decimal(e.body->'selloff'->'worst_hour_median_return_pct') AS worst_hour_pct,
  lab.public_decimal(e.body->'selloff'->'hours_measured') AS hours_measured,
  lab.public_decimal(e.body->'alt_breadth'->'share') AS breadth_share
 FROM lab.managed_events e
 WHERE e.kind='MARKET_REGIME' AND e.setup_id IS NULL
  AND e.body->>'regime_version'='MARKET_REGIME_V1'
  AND e.body->>'day' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
 ORDER BY e.body->>'day', e.event_seq DESC;

REVOKE ALL ON lab.experiment_setup_versions, lab.public_page_trades, lab.public_page_reviews,
 lab.public_page_trade_waits, lab.public_page_status, lab.public_page_regimes FROM PUBLIC;
GRANT SELECT ON lab.public_page_trades, lab.public_page_reviews, lab.public_page_trade_waits,
 lab.public_page_status, lab.public_page_regimes TO catalyst_public;
INSERT INTO lab.schema_migrations(version) VALUES(28);
