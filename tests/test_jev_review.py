import asyncio
import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count
from uuid import uuid4

import httpx
import psycopg
import pytest

from catalyst_lab import localdb
from catalyst_lab.audit import verify_events
from catalyst_lab.jev_contract import (
    INSUFFICIENT,
    JEV_ENDPOINT,
    JEV_MODEL,
    SKEPTIC,
    QuestionSet,
    encoded,
    validated_answers,
)
from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
from catalyst_lab.jev_store import JevStore

FIXTURE_KEY = "fixture-typesafe-secret-no-provider-access"
SEQUENCE = count()


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 19, tzinfo=UTC) + timedelta(days=next(SEQUENCE))
        self.ticks = 0.0

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.ticks += seconds

    async def sleep(self, seconds):
        self.advance(seconds)


@pytest.fixture
def store(cluster):
    return JevStore(localdb.connection_url(cluster, "catalyst_jev"))


@pytest.fixture
def timing():
    return Clock()


def response_for(template=SKEPTIC, verdict="APPROVE"):
    answers = {}
    for name, question in template.questions.items():
        if question["type"] == "choice":
            value = (
                verdict
                if name == "verdict"
                else next(k for k in question["criteria"] if k != INSUFFICIENT)
            )
            answers[name] = {
                "type": "choice",
                "choice": value,
                "confidence": 1.0,
                "probabilities": {k: float(k == value) for k in question["criteria"]},
            }
        elif question["type"] == "score":
            answers[name] = {
                "type": "score",
                "score": 0.25,
                "confidence": 0.2,
                "legend": {"0": "Absent", "1": "Present"},
                "probabilities": {"0": 0.75, "1": 0.25},
            }
        else:
            answers[name] = {"type": "noul", "noul": 0.5}
    return {
        "model": JEV_MODEL,
        "answers": answers,
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }


def build(store, timing, handler, **kwargs):
    return JevReviewer(
        store,
        kwargs.pop("policy", ReliabilityPolicy("LAB_FIXTURE_ONLY", 10, 3, 0.25, 10000, 30)),
        transport=httpx.MockTransport(handler),
        clock=lambda: timing.now,
        monotonic=lambda: timing.ticks,
        sleep=timing.sleep,
        jitter=lambda: 0,
        key_provider=kwargs.pop("key_provider", lambda: FIXTURE_KEY),
        **kwargs,
    )


def request(timing, **kwargs):
    return {
        "request_id": uuid4(),
        "identity": {
            "evidence_bundle_id": str(uuid4()),
            "candidate_revision": 1,
            "setup_revision": 1,
        },
        "state": {"thesis": "Synthetic source-supported product announcement."},
        "question_set": SKEPTIC,
        "expires_at": timing.now + timedelta(seconds=10),
        "purpose": "ENGINEERING_TEST",
        **kwargs,
    }


def run(reviewer, values):
    return asyncio.run(reviewer.jev_review(**values))


def test_exact_receipts_and_typed_skeptic_are_audited(store, timing, repo):
    sent = []
    raw_response = (" \n" + encoded(response_for()) + "\n").encode()

    def provider(req):
        assert str(req.url) == JEV_ENDPOINT
        assert req.headers["authorization"] == "Bearer " + FIXTURE_KEY
        sent.append(req.content)
        timing.advance(0.125)
        return httpx.Response(200, content=raw_response)

    result = run(build(store, timing, provider), request(timing))
    assert result.status == "RECORDED"
    assert result.answers["verdict"]["choice"] == "APPROVE"
    receipt = result.receipt_ids[0]
    assert store.verify(receipt)["valid"]
    with store.connect() as conn:
        row = conn.execute(
            """SELECT q.request_json,r.response_bytes FROM lab.jev_requests q
          JOIN lab.jev_receipts r USING(request_id) WHERE r.receipt_id=%s""",
            (receipt,),
        ).fetchone()
        assert row["request_json"].encode() == sent[0]
        assert bytes(row["response_bytes"]) == raw_response
        judgments = conn.execute(
            "SELECT * FROM lab.jev_decisions WHERE receipt_id=%s", (receipt,)
        ).fetchall()
        assert len(judgments) == 4
        assert all(row["latency_ms"] == 125 and row["model"] == JEV_MODEL for row in judgments)
        assert all(
            "decision_confidence" in row and "win_probability" not in row for row in judgments
        )
    assert FIXTURE_KEY not in encoded(repo.export_events())
    assert verify_events(repo.export_events())["valid"]


@pytest.mark.parametrize("kind", ["choice", "noul", "score"])
def test_provider_decimal_precision_survives_receipt_projection(store, timing, repo, kind):
    high, low = "0.9876543210987654321", "0.0123456789012345679"
    confidence = "0.8234567890123456789"
    criteria = (
        {"YES": "Supported", INSUFFICIENT: "Unavailable"}
        if kind == "choice"
        else ["Absent", "Present"]
    )
    question = {"type": kind, "instructions": "Evaluate the synthetic evidence."}
    if kind != "noul":
        question["criteria"] = criteria
    template = QuestionSet("PRECISION_FIXTURE_V1", "SKEPTIC", encoded({"test": question}))
    if kind == "noul":
        answer = {"type": kind, "noul": "HIGH_NUMBER"}
    elif kind == "choice":
        answer = {
            "type": kind, "choice": "YES", "confidence": "CONFIDENCE_NUMBER",
            "probabilities": {"YES": "HIGH_NUMBER", INSUFFICIENT: "LOW_NUMBER"},
        }
    else:
        answer = {
            "type": kind, "score": "LOW_NUMBER", "confidence": "CONFIDENCE_NUMBER",
            "legend": {"0": "Absent", "1": "Present"},
            "probabilities": {"0": "HIGH_NUMBER", "1": "LOW_NUMBER"},
        }
    raw = encoded({
        "model": JEV_MODEL, "answers": {"test": answer},
        "usage": {"input_tokens": 3, "output_tokens": 2},
    })
    for token, value in (("HIGH_NUMBER", high), ("LOW_NUMBER", low),
                         ("CONFIDENCE_NUMBER", confidence)):
        raw = raw.replace('"' + token + '"', value)
    raw = raw.encode()
    result = run(
        build(store, timing, lambda r: httpx.Response(200, content=raw)),
        request(timing, question_set=template),
    )
    assert result.status == "RECORDED"
    receipt = result.receipt_ids[0]
    assert store.verify(receipt)["valid"]
    with store.connect() as conn:
        row = conn.execute(
            """SELECT r.response_bytes,d.probability,d.decision_confidence,
              d.answer_json = convert_from(r.response_bytes,'UTF8')::jsonb
                ->'answers'->'test' AS exact_answer
            FROM lab.ai_decisions d JOIN lab.jev_receipts r USING(receipt_id)
            WHERE receipt_id=%s""", (receipt,),
        ).fetchone()
    assert bytes(row["response_bytes"]) == raw and row["exact_answer"]
    assert row["probability"] == (None if kind == "score" else Decimal(high))
    assert row["decision_confidence"] == (None if kind == "noul" else Decimal(confidence))
    assert verify_events(repo.export_events())["valid"]


@pytest.mark.parametrize(
    "operation",
    [
        "UPDATE lab.jev_receipts SET actual_model='forged'",
        "DELETE FROM lab.ai_decisions",
        "TRUNCATE lab.jev_requests",
        "INSERT INTO lab.orders(order_id) VALUES(gen_random_uuid())",
        "INSERT INTO lab.risk_decisions(candidate_id) VALUES(gen_random_uuid())",
        "INSERT INTO lab.trade_events(event_type) VALUES('SYSTEM_EVENT')",
        "SET ROLE catalyst_risk",
    ],
)
def test_review_role_cannot_mutate_or_trade(store, operation):
    with store.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(operation)


def test_app_cannot_forge_provider_receipt(repo):
    with repo.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("INSERT INTO lab.jev_receipts(receipt_id) VALUES(gen_random_uuid())")


def test_db_owner_mutation_trigger_and_privileged_tamper_verification(store, timing, cluster):
    result = run(
        build(store, timing, lambda r: httpx.Response(200, json=response_for())), request(timing)
    )
    receipt = result.receipt_ids[0]
    with psycopg.connect(localdb.connection_url(cluster, "lab_owner")) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(
                "UPDATE lab.jev_receipts SET actual_model='forged' WHERE receipt_id=%s", (receipt,)
            )
    # Deliberate privileged tampering ONLY in the disposable fixture DB; rollback afterwards.
    with psycopg.connect(localdb.connection_url(cluster, "lab_owner")) as conn:
        conn.execute("ALTER TABLE lab.jev_receipts DISABLE TRIGGER immutable_rows")
        conn.execute(
            "UPDATE lab.jev_receipts SET response_bytes=%s WHERE receipt_id=%s",
            (encoded(response_for(verdict="REJECT")).encode(), receipt),
        )
        conn.execute("ALTER TABLE lab.jev_receipts ENABLE TRIGGER immutable_rows")
    with pytest.raises(ValueError, match="AUDIT_MISMATCH"):
        store.verify(receipt)


@pytest.mark.parametrize("status", [401, 422, 302])
def test_nonretryable_error_never_approves_or_follows_redirect(store, timing, status):
    calls = []

    def provider(req):
        calls.append(req)
        return httpx.Response(
            status, headers={"Location": "https://unrelated.invalid"}, json={"error": "fixture"}
        )

    result = run(build(store, timing, provider), request(timing))
    assert len(calls) == 1 and result.status == "NEEDS_REVIEW" and not result.answers
    assert len(result.receipt_ids) == 1


def test_529_storm_opens_durable_circuit_and_records_every_judgment(store, timing):
    run(build(store, timing, lambda r: httpx.Response(200, json=response_for())), request(timing))
    calls = []

    def provider(req):
        calls.append(req.content)
        return httpx.Response(529, json={"error": "overloaded fixture"})

    policy = ReliabilityPolicy("LAB_STORM", 10, 3, 0.25, 3, 30)
    result = run(build(store, timing, provider, policy=policy), request(timing))
    assert result.status == "NEEDS_REVIEW" and len(result.receipt_ids) == 3
    assert len(set(calls)) == 1  # exact transport retries, not independent votes
    restarted = build(JevStore(store.database_url), timing, provider, policy=policy)
    second = run(restarted, request(timing))
    assert second.status == "NEEDS_REVIEW" and second.reason == "CIRCUIT_OPEN"
    assert len(calls) == 3 and len(second.receipt_ids) == 1
    timing.advance(31)
    recovered = run(
        build(store, timing, lambda r: httpx.Response(200, json=response_for()), policy=policy),
        request(timing),
    )
    assert recovered.status == "RECORDED"


def test_retry_recovers_and_keeps_both_responses(store, timing):
    calls = []

    def provider(req):
        calls.append(req.content)
        return (
            httpx.Response(429, json={"error": "retry"})
            if len(calls) == 1
            else httpx.Response(200, json=response_for())
        )

    result = run(build(store, timing, provider), request(timing))
    assert result.status == "RECORDED" and len(result.receipt_ids) == 2
    assert calls[0] == calls[1]
    assert all(store.verify(r)["valid"] for r in result.receipt_ids)


def invalid_distribution(template=SKEPTIC):
    """A 200 reply whose verdict distribution sums to 1.1: INVALID_DISTRIBUTION_SUM.

    Far outside JEV_RESPONSE_PRECISION_V1's two-decimal rounding allowance (0.005 per outcome).
    """
    body = response_for(template)
    body["answers"]["verdict"]["probabilities"]["REJECT"] = 0.1
    with pytest.raises(ValueError, match="^INVALID_DISTRIBUTION_SUM$"):
        validated_answers(encoded(body).encode(), template)
    return body


def attempt_rows(store, request_id):
    with store.connect() as conn:
        return conn.execute(
            """SELECT r.*,(SELECT count(*) FROM lab.ai_decisions d
                WHERE d.receipt_id=r.receipt_id) AS judgments
            FROM lab.jev_receipts r WHERE request_id=%s ORDER BY attempt""",
            (request_id,),
        ).fetchall()


def test_invalid_distribution_is_retried_once_with_its_own_receipt(store, timing):
    invalid = httpx.Response(200, json=invalid_distribution())
    replies, calls = [invalid, httpx.Response(200, json=response_for())], []

    def provider(req):
        calls.append(req.content)
        return replies[len(calls) - 1]

    values = request(timing)
    result = run(build(store, timing, provider), values)
    assert (result.status, result.reason, len(result.receipt_ids)) == ("RECORDED", None, 2)
    assert calls[0] == calls[1]  # The same exact request, not a new one.
    first, second = attempt_rows(store, values["request_id"])
    assert (first["attempt"], first["outcome"], first["http_status"], first["error_code"]) == (
        1, "INVALID_RESPONSE", 200, "INVALID_PROVIDER_RESPONSE")
    # The invalid distribution is retained exactly as received and never normalised or judged.
    assert bytes(first["response_bytes"]) == invalid.content and first["judgments"] == 0
    assert (second["attempt"], second["outcome"], second["judgments"]) == (2, "VALID", 4)
    assert result.answers == validated_answers(bytes(second["response_bytes"]), SKEPTIC)
    assert all(store.verify(r)["valid"] for r in result.receipt_ids)


@pytest.mark.parametrize(("max_attempts", "expected_calls"), [(2, 2), (3, 2), (1, 1)])
def test_invalid_distribution_twice_is_the_existing_failure(store, timing, max_attempts,
                                                           expected_calls):
    calls = []

    def provider(req):
        calls.append(req.content)
        return httpx.Response(200, json=invalid_distribution())

    values = request(timing)
    policy = ReliabilityPolicy("LAB_FIXTURE_ONLY", 10, max_attempts, 0.25, 10000, 30)
    result = run(build(store, timing, provider, policy=policy), values)
    # One further attempt only, inside max_attempts: then the unchanged failure result.
    assert (result.status, result.reason, result.answers) == (
        "NEEDS_REVIEW", "INVALID_PROVIDER_RESPONSE", {})
    assert len(calls) == len(result.receipt_ids) == expected_calls
    rows = attempt_rows(store, values["request_id"])
    assert [(r["outcome"], r["error_code"], r["judgments"]) for r in rows] == [
        ("INVALID_RESPONSE", "INVALID_PROVIDER_RESPONSE", 0)] * expected_calls


def test_invalid_distribution_retry_never_outlives_the_deadline(store, timing):
    calls = []

    def provider(req):
        calls.append(req.content)
        return httpx.Response(200, json=invalid_distribution())

    policy = ReliabilityPolicy("LAB_FIXTURE_ONLY", 10, 3, 20, 10000, 30)  # Backoff > deadline.
    result = run(build(store, timing, provider, policy=policy), request(timing))
    assert len(calls) == 1 and len(result.receipt_ids) == 2
    assert (result.status, result.reason) == ("NEEDS_REVIEW", "REVIEW_DEADLINE_EXCEEDED")
    assert store.verify(result.receipt_ids[-1])["outcome"] == "EXPIRED"


@pytest.mark.parametrize("reply", [httpx.Response(200, content=FIXTURE_KEY),
                                   httpx.Response(400, json={})])
def test_a_credential_echo_or_an_http_error_is_not_retried_as_invalid(store, timing, reply):
    calls = []
    result = run(build(store, timing, lambda r: calls.append(1) or reply), request(timing))
    assert len(calls) == 1 and len(result.receipt_ids) == 1
    assert result.status == "NEEDS_REVIEW"


def test_late_response_is_saved_but_not_acted_on(store, timing):
    def provider(req):
        timing.advance(11)
        return httpx.Response(200, json=response_for())

    result = run(build(store, timing, provider), request(timing))
    assert result.status == "NEEDS_REVIEW" and result.reason == "REVIEW_DEADLINE_EXCEEDED"
    assert not result.answers
    assert store.verify(result.receipt_ids[0])["outcome"] == "EXPIRED"


def test_preexpired_request_never_calls_provider(store, timing):
    result = run(
        build(store, timing, lambda r: pytest.fail("must not call")),
        request(timing, expires_at=timing.now),
    )
    assert result.status == "NEEDS_REVIEW" and result.reason == "REVIEW_DEADLINE_EXCEEDED"


def test_transport_failure_does_not_leak_exception(store, timing, repo):
    def provider(req):
        raise httpx.ConnectError(FIXTURE_KEY, request=req)

    result = run(build(store, timing, provider), request(timing))
    assert result.reason == "PROVIDER_TRANSPORT_FAILURE"
    assert FIXTURE_KEY not in encoded(repo.export_events())


# --- A transport failure is retried once per review (2026-09-28) -----------------------------


def test_a_transport_failure_is_retried_once_with_its_own_receipt(store, timing):
    calls = []

    def provider(req):
        calls.append(req.content)
        if len(calls) == 1:
            raise httpx.ReadTimeout("stalled", request=req)
        return httpx.Response(200, json=response_for())

    values = request(timing)
    result = run(build(store, timing, provider), values)
    assert (result.status, result.reason, len(result.receipt_ids)) == ("RECORDED", None, 2)
    assert calls[0] == calls[1]  # The same exact request, not a new one.
    first, second = attempt_rows(store, values["request_id"])
    assert (first["attempt"], first["outcome"], first["error_code"], first["judgments"]) == (
        1, "TRANSPORT_FAILURE", "PROVIDER_TRANSPORT_FAILURE", 0)
    assert (second["attempt"], second["outcome"], second["judgments"]) == (2, "VALID", 4)
    assert all(store.verify(r)["valid"] for r in result.receipt_ids)


@pytest.mark.parametrize(("max_attempts", "expected_calls"), [(3, 2), (2, 2), (1, 1)])
def test_a_second_transport_failure_is_the_existing_failure(store, timing, max_attempts,
                                                            expected_calls):
    calls = []

    def provider(req):
        calls.append(1)
        raise httpx.ConnectError("unreachable", request=req)

    values = request(timing)
    policy = ReliabilityPolicy("LAB_FIXTURE_ONLY", 10, max_attempts, 0.25, 10000, 30)
    result = run(build(store, timing, provider, policy=policy), values)
    assert (result.status, result.reason, result.answers) == (
        "NEEDS_REVIEW", "PROVIDER_TRANSPORT_FAILURE", {})
    assert len(calls) == len(result.receipt_ids) == expected_calls


def test_a_transport_failure_and_an_invalid_reply_each_get_their_one_retry(store, timing):
    replies = ["stall", httpx.Response(200, json=invalid_distribution()),
               httpx.Response(200, json=response_for())]
    calls = []

    def provider(req):
        calls.append(1)
        reply = replies[len(calls) - 1]
        if reply == "stall":
            raise httpx.ReadTimeout("stalled", request=req)
        return reply

    values = request(timing)
    result = run(build(store, timing, provider), values)
    assert (result.status, len(calls)) == ("RECORDED", 3)
    assert [r["outcome"] for r in attempt_rows(store, values["request_id"])] == [
        "TRANSPORT_FAILURE", "INVALID_RESPONSE", "VALID"]


def test_a_transport_retry_never_outlives_the_deadline(store, timing):
    calls = []

    def provider(req):
        calls.append(1)
        raise httpx.ReadTimeout("stalled", request=req)

    policy = ReliabilityPolicy("LAB_FIXTURE_ONLY", 10, 3, 20, 10000, 30)  # Backoff > deadline.
    result = run(build(store, timing, provider, policy=policy), request(timing))
    assert len(calls) == 1 and len(result.receipt_ids) == 2
    assert (result.status, result.reason) == ("NEEDS_REVIEW", "REVIEW_DEADLINE_EXCEEDED")
    assert store.verify(result.receipt_ids[-1])["outcome"] == "EXPIRED"


def test_missing_key_is_recorded_without_http(store, timing):
    result = run(
        build(store, timing, lambda r: pytest.fail("must not call"), key_provider=lambda: None),
        request(timing),
    )
    assert result.reason == "CREDENTIAL_UNAVAILABLE" and result.receipt_ids


def test_duplicate_packet_is_not_rerolled_even_with_new_request_id(store, timing):
    calls = []

    def provider(req):
        calls.append(req)
        return httpx.Response(200, json=response_for())

    reviewer = build(store, timing, provider)
    values = request(timing)
    assert run(reviewer, values).status == "RECORDED"
    for repeated in (values, {**values, "request_id": uuid4()}):
        assert run(reviewer, repeated).reason == "DUPLICATE_REVIEW"
    assert len(calls) == 1


def test_concurrent_duplicate_review_makes_one_call(store, timing):
    calls = []

    async def provider(req):
        calls.append(req)
        await asyncio.sleep(0)
        return httpx.Response(200, json=response_for())

    reviewer, values = build(store, timing, provider), request(timing)

    async def concurrent():
        return await asyncio.gather(*(reviewer.jev_review(**values) for _ in range(5)))

    results = asyncio.run(concurrent())
    assert sum(r.status == "RECORDED" for r in results) == 1 and len(calls) == 1


@pytest.mark.parametrize("value", [INSUFFICIENT, "NEEDS_REVIEW"])
def test_uncertainty_returns_needs_review_with_original_judgments(store, timing, value):
    result = run(
        build(store, timing, lambda r: httpx.Response(200, json=response_for(verdict=value))),
        request(timing),
    )
    assert result.status == "NEEDS_REVIEW" and result.reason == "UNCERTAIN_JUDGMENT"
    assert result.answers["verdict"]["choice"] == value


@pytest.mark.parametrize(
    "state",
    [
        {"api_key": "hidden"},
        {"thesis": "contact someone@example.com"},
        {"thesis": "apikey_" + "a" * 40},
        {"thesis": "x" * 12001},
        {"thesis": FIXTURE_KEY},
    ],
)
def test_sensitive_or_oversized_evidence_is_rejected_before_persistence(store, timing, state):
    values = request(timing, state=state)
    with pytest.raises(ValueError):
        run(build(store, timing, lambda r: pytest.fail("must not call")), values)
    with store.connect() as conn:
        assert not conn.execute(
            "SELECT 1 FROM lab.jev_requests WHERE request_id=%s", (values["request_id"],)
        ).fetchone()


def test_provider_secret_echo_never_enters_audit(store, timing, repo):
    result = run(
        build(store, timing, lambda r: httpx.Response(200, content=FIXTURE_KEY)), request(timing)
    )
    assert result.reason == "CREDENTIAL_ECHO_BLOCKED"
    assert FIXTURE_KEY not in encoded(repo.export_events())


@pytest.mark.parametrize(
    "mutation", ["model", "missing", "probability", "choice", "confidence", "extra"]
)
def test_malformed_typed_answer_never_becomes_a_judgment(store, timing, mutation):
    body = response_for()
    answer = body["answers"]["verdict"]
    if mutation == "model":
        body["model"] = "jev-0.0.0"
    if mutation == "missing":
        del body["answers"]["verdict"]
    if mutation == "probability":
        answer["probabilities"]["APPROVE"] = 0.3
    if mutation == "choice":
        answer["choice"] = "REJECT"
    if mutation == "confidence":
        answer["confidence"] = True
    if mutation == "extra":
        answer["execute_order"] = True
    result = run(build(store, timing, lambda r: httpx.Response(200, json=body)), request(timing))
    assert result.status == "NEEDS_REVIEW" and not result.answers
    with store.connect() as conn:
        assert not conn.execute(
            "SELECT 1 FROM lab.ai_decisions WHERE receipt_id=%s", (result.receipt_ids[0],)
        ).fetchone()


def test_all_three_official_primitives_are_validated():
    questions = {
        "choice": SKEPTIC.questions["verdict"],
        "score": {
            "type": "score",
            "instructions": "How explicit is source support?",
            "criteria": ["Absent", "Present"],
        },
        "noul": {"type": "noul", "instructions": "Is there explicit source support?"},
    }
    template = QuestionSet("FIXTURE_PRIMITIVES", "SKEPTIC", encoded(questions))
    answer = validated_answers(encoded(response_for(template)).encode(), template)
    assert answer["score"]["score"] == 0.25 and "confidence" not in answer["noul"]
    changed = copy.deepcopy(questions)
    del changed["choice"]["criteria"][INSUFFICIENT]
    with pytest.raises(ValueError, match="INSUFFICIENT"):
        QuestionSet("FIXTURE", "SKEPTIC", encoded(changed))


def test_no_default_reliability_policy():
    with pytest.raises(TypeError):
        ReliabilityPolicy()


def test_total_network_deadline_is_bounded(store, timing):
    async def slow(req):
        await asyncio.sleep(1)
        return httpx.Response(200, json=response_for())

    policy = ReliabilityPolicy("LAB_TIMEOUT", 0.01, 1, 0.25, 10000, 30)
    result = run(build(store, timing, slow, policy=policy), request(timing))
    assert result.status == "NEEDS_REVIEW" and not result.answers


def test_json_duplicate_keys_and_nonfinite_answers_are_rejected():
    for raw in (b'{"model":"a","model":"b"}', b'{"model":NaN}'):
        with pytest.raises(ValueError):
            validated_answers(raw, SKEPTIC)


def test_background_keychain_lookup_never_prompts(monkeypatch):
    from catalyst_lab import jev_secrets

    calls = []

    class Function:
        def __init__(self, result):
            self.result = result

        def __call__(self, *args):
            calls.append(args)
            return self.result

    class Security:
        SecKeychainSetUserInteractionAllowed = Function(0)
        SecKeychainFindGenericPassword = Function(-25308)

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(jev_secrets.sys, "platform", "darwin")
    monkeypatch.setattr(jev_secrets.ctypes, "CDLL", lambda p: Security())
    with pytest.raises(jev_secrets.CredentialUnavailable):
        jev_secrets.typesafe_key()
    assert calls[0] == (False,)


def test_secret_manager_environment_is_supported_without_keychain(monkeypatch):
    from catalyst_lab import jev_secrets

    monkeypatch.setenv("TYPESAFE_API_KEY", FIXTURE_KEY)
    monkeypatch.setattr(jev_secrets.ctypes, "CDLL", lambda p: pytest.fail("must not load Keychain"))
    assert jev_secrets.typesafe_key() == FIXTURE_KEY
