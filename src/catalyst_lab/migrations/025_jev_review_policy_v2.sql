-- JEV_LIVE_REVIEW_POLICY_V2 (owner ruling 2026-09-28, docs/CONTRACT-RESOLUTIONS.md): V1's exact
-- values except version, question_timeout_seconds 8 and batch_timeout_seconds 8. The overall
-- review deadline stays 10 s. DDL only: no row is written except schema_migrations, so no
-- audit event is appended. V1 is unchanged: the same exact JSON registers the same scope.

-- 1. The runtime scope. Exactly V1's JSON (migration 011's literal, byte for byte, and its
--    scope id) or exactly V2's JSON; anything else is refused as before. A V2 scope has its own
--    id, so its stored policy_json, which lab.review_attempt_permit reads for each attempt's
--    expiry (batch_timeout_seconds), is V2's, and its breaker, heartbeats and permits are its
--    own. The slot pattern allows no ':', so no V1 id can equal a V2 id.
CREATE OR REPLACE FUNCTION lab.register_review_scope(slot text, policy jsonb) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE sid text; BEGIN
 IF slot !~ '^[a-z][a-z0-9-]{0,47}$' OR slot IS NULL THEN
  RAISE EXCEPTION 'EXPLICIT_APPROVED_WORKER_POLICY_REQUIRED'; END IF;
 IF policy IS NOT DISTINCT FROM $policy${"backoff_base_ms":250,"backoff_cap_ms":2000,"backoff_multiplier":2,"batch_timeout_seconds":3,"breaker_cooldown_seconds":30,"breaker_failure_classes":["HTTP_ERROR","TRANSPORT_FAILURE","TIMEOUT","INVALID_RESPONSE","MODEL_MISMATCH","CREDENTIAL_ECHO"],"breaker_failure_threshold":3,"breaker_recovery_successes":2,"breaker_scope":"PROVIDER_MODEL_CREDENTIAL_SLOT","context_max_age_seconds":60,"duplicate_policy":"AUDIT_REJECT_SAME_REQUEST_RETRY","evidence_max_age_seconds":60,"expiry_grace_seconds":0,"half_open_max_attempts":1,"half_open_max_inflight":1,"heartbeat_deadline_seconds":15,"heartbeat_period_seconds":5,"jitter_lower_fraction":0.5,"jitter_mode":"EQUAL","material_supersession_policy":"MATERIAL_REVISION_ONLY","max_attempts":3,"max_clock_offset_ms":250,"overall_review_deadline_seconds":10,"question_timeout_seconds":3,"recovery_probe_policy":"SYNTHETIC_NON_AUTHORIZING","recovery_probe_spacing_seconds":1,"retry_after_policy":"RESPECT_MINIMUM_OR_ABANDON","retry_http_statuses":[429,529],"same_candidate_max_active_chains":1,"selection_policy":"EXACT_REVISION_FIRST_VALID_CONFLICT_NEEDS_REVIEW","version":"JEV_LIVE_REVIEW_POLICY_V1"}$policy$::jsonb THEN
  sid:=encode(public.digest('TYPESAFE:jev-1.13.0:'||slot,'sha256'),'hex');
 ELSIF policy IS NOT DISTINCT FROM $policy_v2${"backoff_base_ms":250,"backoff_cap_ms":2000,"backoff_multiplier":2,"batch_timeout_seconds":8,"breaker_cooldown_seconds":30,"breaker_failure_classes":["HTTP_ERROR","TRANSPORT_FAILURE","TIMEOUT","INVALID_RESPONSE","MODEL_MISMATCH","CREDENTIAL_ECHO"],"breaker_failure_threshold":3,"breaker_recovery_successes":2,"breaker_scope":"PROVIDER_MODEL_CREDENTIAL_SLOT","context_max_age_seconds":60,"duplicate_policy":"AUDIT_REJECT_SAME_REQUEST_RETRY","evidence_max_age_seconds":60,"expiry_grace_seconds":0,"half_open_max_attempts":1,"half_open_max_inflight":1,"heartbeat_deadline_seconds":15,"heartbeat_period_seconds":5,"jitter_lower_fraction":0.5,"jitter_mode":"EQUAL","material_supersession_policy":"MATERIAL_REVISION_ONLY","max_attempts":3,"max_clock_offset_ms":250,"overall_review_deadline_seconds":10,"question_timeout_seconds":8,"recovery_probe_policy":"SYNTHETIC_NON_AUTHORIZING","recovery_probe_spacing_seconds":1,"retry_after_policy":"RESPECT_MINIMUM_OR_ABANDON","retry_http_statuses":[429,529],"same_candidate_max_active_chains":1,"selection_policy":"EXACT_REVISION_FIRST_VALID_CONFLICT_NEEDS_REVIEW","version":"JEV_LIVE_REVIEW_POLICY_V2"}$policy_v2$::jsonb THEN
  sid:=encode(public.digest('TYPESAFE:jev-1.13.0:'||slot||':JEV_LIVE_REVIEW_POLICY_V2','sha256'),'hex');
 ELSE
  RAISE EXCEPTION 'EXPLICIT_APPROVED_WORKER_POLICY_REQUIRED';
 END IF;
 PERFORM pg_advisory_xact_lock(719172026);
 IF NOT EXISTS(SELECT 1 FROM lab.review_runtime_scopes WHERE scope_id=sid) THEN
  INSERT INTO lab.review_runtime_scopes(scope_id,credential_slot,model,policy_json)
  VALUES(sid,slot,'jev-1.13.0',policy);
 END IF;
 RETURN sid;
END $$;

-- 2. The research intake (migration 010) reads four gate1 fields: the version, the overall
--    deadline and the evidence and context ages. V2 changes none of the last three. Migration
--    010's function is kept byte for byte under a new name and stays the only place that checks
--    and uses them; the entry point passes a V1 gate1 through unchanged and checks a V2 gate1
--    under V1's label, so a V2 gate1 with other timing is refused exactly like a V1 one.
ALTER FUNCTION lab.store_research_report(jsonb,jsonb) RENAME TO store_research_report_v1;
REVOKE ALL ON FUNCTION lab.store_research_report_v1(jsonb,jsonb) FROM catalyst_review;
CREATE FUNCTION lab.store_research_report(body jsonb, gate1 jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab AS $$ BEGIN
 IF gate1->>'version'='JEV_LIVE_REVIEW_POLICY_V2' THEN
  gate1:=gate1||'{"version":"JEV_LIVE_REVIEW_POLICY_V1"}'::jsonb;
 END IF;
 RETURN lab.store_research_report_v1(body,gate1);
END $$;
REVOKE ALL ON FUNCTION lab.store_research_report(jsonb,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.store_research_report(jsonb,jsonb) TO catalyst_review;
INSERT INTO lab.schema_migrations(version) VALUES(25);
