from copy import deepcopy

import pytest

from catalyst_lab.domain import BatchEnvelope
from tests.conftest import NOW


def envelope(raw):
    item = deepcopy(raw)
    version = item.pop("strategy_version")
    return {"strategy_version": version, "date": NOW.date().isoformat(), "candidates": [item]}


def test_batch_contract_and_date_polling(client, raw):
    request = envelope(raw)
    request["candidates"][0]["risk_reward"] = "1:2"
    response = client.post("/api/v1/candidates", json=request)
    assert response.status_code == 201
    record = response.json()["items"][0]
    assert record["state"] == "VALIDATED"
    assert record["status"] == "not_entered"
    assert record["strategy_version"] == request["strategy_version"]
    assert record["payload_json"] == request["candidates"][0]
    assert record["submission_context"]["date"] == "2026-09-18"
    assert record["fill_price"] is None and record["size_shares"] is None
    assert record["mfe_r"] is None and record["mae_r"] is None and record["net_r"] is None
    rows = client.get("/api/v1/candidates?date=2026-09-18&limit=200").json()["items"]
    assert record["candidate_id"] in {row["candidate_id"] for row in rows}
    other = client.get("/api/v1/candidates?date=2000-01-01").json()["items"]
    assert not other


def test_spec_example_rejected_on_computed_rr_not_muse_label(client, raw):
    request = envelope(raw)
    request["candidates"][0].update(
        entry_trigger=101, max_entry_price=101.15, stop=96.5, target=109, risk_reward="1:2.4"
    )
    record = client.post("/api/v1/candidates", json=request).json()["items"][0]
    assert record["status"] == "rejected"
    assert record["rejection_reason"] == "MIN_REWARD_RISK"


def test_batch_records_duplicate_and_bad_items_individually(client, raw):
    request = envelope(raw)
    request["candidates"] += [deepcopy(request["candidates"][0]), {"ticker": "BADITEM"}]
    records = client.post("/api/v1/candidates", json=request).json()["items"]
    assert [r["failed_rule"] for r in records] == [None, "DUPLICATE_SIGNAL_ID", "INVALID_SCHEMA"]


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        ({"date": "2026-09-19"}, "CANDIDATE_DATE_MISMATCH"),
        ({"strategy_version": "OTHER"}, "INVALID_SCHEMA"),
    ],
)
def test_batch_context_enforced(client, raw, change, rule):
    result = client.post("/api/v1/candidates", json=envelope(raw) | change).json()
    assert result["items"][0]["failed_rule"] == rule


def test_child_cannot_override_version(client, raw):
    request = envelope(raw)
    request["candidates"][0]["strategy_version"] = "OTHER"
    result = client.post("/api/v1/candidates", json=request).json()
    assert result["items"][0]["failed_rule"] == "STRATEGY_VERSION_MISMATCH"


def test_missing_strategy_still_attributed_to_validating_engine(client, raw):
    raw.pop("strategy_version")
    record = client.post("/api/v1/candidates", json=raw).json()
    assert record["failed_rule"] == "INVALID_SCHEMA"
    assert record["strategy_version"] == "CATALYST_RETEST_V1"
    assert "strategy_version" not in record["payload_json"]


@pytest.mark.parametrize(
    "change",
    [
        {"candidates": []},
        {"candidates": [{}] * 51},
        {"date": 1789700000},
        {"size_shares": 123},
        {"date": "20260918"},
    ],
)
def test_malformed_envelopes_rejected_before_ingestion(client, raw, change):
    assert client.post("/api/v1/candidates", json=envelope(raw) | change).status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        '{"ticker":"INTC","ticker":"OTHER"}',
        '{"stop":1e9999}',
        '{"thesis":"\\u0000"}',
        '{"thesis":"\\ud800"}',
    ],
)
def test_unrepresentable_or_ambiguous_json_rejected(client, body):
    assert client.post("/api/v1/candidates", content=body).status_code == 400


def test_batch_rollback_is_atomic(repo, raw, evidence, policy, monkeypatch):
    request = envelope(raw)
    request["candidates"] *= 2
    before = repo.export_events()
    original = repo.submit
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated DB failure halfway through batch")
        return original(*args, **kwargs)

    monkeypatch.setattr(repo, "submit", interrupted)
    with pytest.raises(RuntimeError):
        repo.submit_batch(BatchEnvelope(**request), NOW, lambda c, now: evidence, policy)
    assert repo.export_events() == before
