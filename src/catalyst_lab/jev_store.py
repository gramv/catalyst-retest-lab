"""Append-only review persistence. This role cannot authorize or mutate broker orders."""

from datetime import timedelta
from uuid import uuid4

from psycopg.types.json import Jsonb

from catalyst_lab.jev_contract import (
    JEV_MODEL,
    QuestionSet,
    digest,
    encoded,
    strict_json,
    validated_answers,
)
from catalyst_lab.repository import Repository


class JudgmentProjectionFailed(RuntimeError):
    def __init__(self, receipt_id):
        super().__init__("JUDGMENT_PROJECTION_FAILED")
        self.receipt_id = str(receipt_id)


class JevStore(Repository):
    def check_role(self):
        with self.connect() as conn:
            row = conn.execute("""
              SELECT current_user AS role,rolsuper,rolcreaterole,rolcreatedb,
                has_table_privilege(current_user,'lab.orders','INSERT') AS order_write,
                has_table_privilege(current_user,'lab.risk_decisions','INSERT') AS risk_write,
                has_table_privilege(current_user,'lab.trade_events','INSERT') AS event_write,
                has_table_privilege(current_user,'lab.jev_receipts','UPDATE') AS receipt_update,
                has_table_privilege(current_user,'lab.jev_receipts','DELETE') AS receipt_delete,
                has_schema_privilege(current_user,'lab','CREATE') AS ddl
              FROM pg_roles WHERE rolname=current_user
            """).fetchone()
            if row["role"] != "catalyst_jev" or any(v for k, v in row.items() if k != "role"):
                raise RuntimeError("Review adapter requires restricted catalyst_jev role")

    def start(self, request_id, identity, template, input_hash, request_json, deadline, purpose):
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            existing = conn.execute(
                """
              SELECT request_id FROM lab.jev_requests WHERE request_id=%s OR
               (input_hash=%s AND template_hash=%s AND evidence_identity=%s) OR
               (%s::text IS NOT NULL AND template_hash=%s AND
                 evidence_identity->>'research_content_hash'=%s)
              LIMIT 1
            """,
                (
                    request_id,
                    input_hash,
                    template.template_hash,
                    Jsonb(identity),
                    identity.get("research_content_hash"),
                    template.template_hash,
                    identity.get("research_content_hash"),
                ),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                  INSERT INTO lab.jev_control_events(control_id,request_id,reason)
                  VALUES(%s,%s,'DUPLICATE_REVIEW')
                """,
                    (uuid4(), existing["request_id"]),
                )
                return None
            conn.execute(
                """
              INSERT INTO lab.jev_requests(request_id,evidence_identity,stage,question_set_version,
                model,input_hash,template_hash,request_json,deadline,record_purpose)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
                (
                    request_id,
                    Jsonb(identity),
                    template.stage,
                    template.version,
                    JEV_MODEL,
                    input_hash,
                    template.template_hash,
                    request_json,
                    deadline,
                    purpose,
                ),
            )
            return request_id

    def finish(
        self,
        request_id,
        attempt,
        outcome,
        http_status,
        raw,
        actual_model,
        started,
        completed,
        latency_ms,
        error_code,
        answers,
    ):
        receipt_id = uuid4()
        with self.connect() as conn:
            # Serialize permit completion with report/worker/breaker state before audit locks.
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            conn.execute(
                """
              INSERT INTO lab.jev_receipts(receipt_id,request_id,attempt,outcome,http_status,
                response_bytes,actual_model,started_at,completed_at,latency_ms,error_code)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
                (
                    receipt_id,
                    request_id,
                    attempt,
                    outcome,
                    http_status,
                    raw,
                    actual_model,
                    started,
                    completed,
                    latency_ms,
                    error_code,
                ),
            )
        # Commit the exact observed response before projection. A failed projection
        # must not erase the provider receipt or cause another provider evaluation.
        if answers:
            try:
                self.project_receipt(receipt_id)
            except Exception:
                raise JudgmentProjectionFailed(receipt_id) from None
        return str(receipt_id)

    def project_receipt(self, receipt_id):
        """Idempotent local recovery from retained bytes; no provider request."""
        self.verify(receipt_id, require_decisions=False)
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(719172026)")
            row = conn.execute(
                """SELECT r.response_bytes,r.outcome,q.question_set_version,q.stage,q.request_json
                FROM lab.jev_receipts r JOIN lab.jev_requests q USING(request_id)
                WHERE receipt_id=%s""",
                (receipt_id,),
            ).fetchone()
            if row is None or row["outcome"] != "VALID":
                raise ValueError("VALID_RECEIPT_REQUIRED")
            body = strict_json(row["request_json"])
            template = QuestionSet(
                row["question_set_version"], row["stage"], encoded(body["questions"])
            )
            answers = validated_answers(bytes(row["response_bytes"]), template)
            for question in answers:
                if conn.execute(
                    "SELECT 1 FROM lab.ai_decisions WHERE receipt_id=%s AND question=%s",
                    (receipt_id, question),
                ).fetchone():
                    continue
                # Preserve provider decimal precision using the exact stored JSON.
                conn.execute(
                    """
                  INSERT INTO lab.ai_decisions(decision_id,receipt_id,question,answer_json,
                    probability,decision_confidence)
                  SELECT %s,%s,%s,answer,
                    CASE answer->>'type'
                      WHEN 'choice' THEN (answer->'probabilities'->>(answer->>'choice'))::numeric
                      WHEN 'noul' THEN (answer->>'noul')::numeric ELSE NULL END,
                    (answer->>'confidence')::numeric
                  FROM (SELECT convert_from(response_bytes,'UTF8')::jsonb->'answers'->%s AS answer
                    FROM lab.jev_receipts WHERE receipt_id=%s) observed
                """,
                    (uuid4(), receipt_id, question, question, receipt_id),
                )

    def circuit_open(self, now, failure_threshold, cooldown):
        with self.connect() as conn:
            rows = conn.execute(
                """
              SELECT outcome,completed_at FROM lab.jev_receipts
              WHERE outcome IN ('VALID','HTTP_ERROR','TRANSPORT_FAILURE','INVALID_RESPONSE')
              ORDER BY event_seq DESC LIMIT %s
            """,
                (failure_threshold,),
            ).fetchall()
        return (
            len(rows) == failure_threshold
            and all(row["outcome"] != "VALID" for row in rows)
            and now < max(row["completed_at"] for row in rows) + timedelta(seconds=cooldown)
        )

    def verify(self, receipt_id, *, require_decisions=True):
        """Bind exact bytes and metadata to the audit chain, including judgments."""
        with self.connect() as conn:
            receipt = conn.execute(
                "SELECT * FROM lab.jev_receipts WHERE receipt_id=%s", (receipt_id,)
            ).fetchone()
            if not receipt:
                raise ValueError("RECEIPT_MISSING")
            request = conn.execute(
                "SELECT * FROM lab.jev_requests WHERE request_id=%s", (receipt["request_id"],)
            ).fetchone()
            if digest(request["request_json"]) != request["request_hash"]:
                raise ValueError("REQUEST_HASH_MISMATCH")
            request_body = strict_json(request["request_json"])
            template = QuestionSet(
                request["question_set_version"],
                request["stage"],
                encoded(request_body["questions"]),
            )
            if digest(encoded(request_body["state"])) != request["input_hash"]:
                raise ValueError("INPUT_HASH_MISMATCH")
            if template.template_hash != request["template_hash"]:
                raise ValueError("TEMPLATE_HASH_MISMATCH")
            raw = (
                bytes(receipt["response_bytes"]) if receipt["response_bytes"] is not None else None
            )
            if (digest(raw) if raw is not None else None) != receipt["response_hash"]:
                raise ValueError("RESPONSE_HASH_MISMATCH")
            for table, identifier, value in (
                ("jev_requests", "request_id", request["request_id"]),
                ("jev_receipts", "receipt_id", receipt_id),
                ("ai_decisions", "receipt_id", receipt_id),
            ):
                # Identifiers above are fixed application constants, never user SQL.
                rows = conn.execute(
                    f"""
                  SELECT a.*, (to_jsonb(t)-'event_seq'-'request_hash'-'response_hash'
                    = a.payload_json->'row') AS row_matches
                  FROM lab.{table} t JOIN lab.jev_audit_events a ON a.seq=t.event_seq
                  WHERE t.{identifier}=%s
                """,
                    (value,),
                ).fetchall()
                if table != "ai_decisions" and len(rows) != 1:
                    raise ValueError("AUDIT_ENVELOPE_MISSING")
                if table == "ai_decisions" and receipt["outcome"] == "VALID":
                    answers = validated_answers(raw, template)
                    if require_decisions and len(rows) != len(answers):
                        raise ValueError("JUDGMENT_SET_MISMATCH")
                for row in rows:
                    if (
                        not row["row_matches"]
                        or digest(row["previous_hash"] + row["event_body"]) != row["event_hash"]
                    ):
                        raise ValueError("RECEIPT_AUDIT_MISMATCH")
                    if strict_json(row["event_body"])["payload_json"] != row["payload_json"]:
                        raise ValueError("RECEIPT_AUDIT_MISMATCH")
            return {
                "valid": True,
                "receipt_id": str(receipt_id),
                "request_hash": request["request_hash"],
                "response_hash": receipt["response_hash"],
                "outcome": receipt["outcome"],
            }

    def control(self, request_id, reason):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO lab.jev_control_events(control_id,request_id,reason) VALUES(%s,%s,%s)",
                (uuid4(), request_id, reason),
            )

    def metrics(self, purpose):
        with self.connect() as conn:
            row = conn.execute(
                """
              SELECT count(*) AS attempts,
                count(*) FILTER(WHERE outcome='VALID') AS valid_responses,
                percentile_cont(0.5) WITHIN GROUP(ORDER BY latency_ms)
                  FILTER(WHERE http_status IS NOT NULL) AS p50_ms,
                percentile_cont(0.95) WITHIN GROUP(ORDER BY latency_ms)
                  FILTER(WHERE http_status IS NOT NULL) AS p95_ms
              FROM lab.jev_receipts r JOIN lab.jev_requests q USING(request_id)
              WHERE q.record_purpose=%s
            """,
                (purpose,),
            ).fetchone()
        return row
