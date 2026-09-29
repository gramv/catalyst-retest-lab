import json
import threading
from datetime import UTC, datetime, timedelta

import pytest

from catalyst_lab.muse_worker import MuseHttp, MuseSpool, MuseWorker, service_lanes

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)


class Provider:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def invoke(self, request, schema=None):
        self.calls.append(request)
        return self.replies.pop(0)


class Api:
    def __init__(self):
        self.calls, self.fail_once, self.responses = [], False, {}

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("uncertain")
        return self.responses.get((method, path), {})


def item():
    return {
        "signal_id": "s1",
        "market": "US_STOCKS",
        "symbol": "TEST",
        "direction": "LONG",
        "catalyst": "TEST",
        "thesis": "Supported thesis.",
        "disproof": "Issuer withdraws statement.",
        "economic_relationship": "Customers buy product.",
        "technical_analysis": "Completed bars show a retest.",
        "levels": {"entry_trigger": "10", "max_entry_price": "10.1", "stop": "9", "target": "12.3"},
        "sources": [
            {
                "source_id": "issuer",
                "url": "https://issuer.example/a",
                "excerpt": "Issuer statement.",
                "retrieved_at": NOW.isoformat(),
                "published_at": None,
            }
        ],
    }


def test_uncertain_report_reuses_persisted_body_after_restart(tmp_path):
    spool, api = MuseSpool(tmp_path / "spool.db"), Api()
    worker = MuseWorker(
        spool,
        api,
        Provider([{"kind": "report", "reason": None, "items": [item()]}]),
        clock=lambda: NOW,
    )
    report_id = worker.prepare_report("US_STOCKS")
    saved = spool.pending()[0]["body"]
    api.fail_once = True
    worker.flush()
    restarted = MuseWorker(MuseSpool(tmp_path / "spool.db"), api, Provider([]), clock=lambda: NOW)
    restarted.flush()
    assert json.dumps(api.calls[-1][2], sort_keys=True, separators=(",", ":")) == saved
    assert api.calls[-1][2]["report_id"] == report_id


def test_cursor_claim_followup_persists_task_bound_revision(tmp_path):
    spool, api = MuseSpool(tmp_path / "spool.db"), Api()
    cycle, task_id = "00000000-0000-4000-8000-000000000001", "task-1"
    task = {
        "task_id": task_id,
        "item_key": "US_STOCKS:TEST",
        "revision": 1,
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
    }
    api.responses[("GET", "/api/v1/lab/outputs?after=0&limit=10")] = {
        "items": [{"event_seq": 8, "kind": "RESEARCH_EVIDENCE_TASK", "body": {"cycle_id": cycle}}],
        "next_cursor": 8,
    }
    api.responses[("POST", f"/api/v1/lab/cycles/{cycle}/evidence-tasks/claim")] = {"tasks": [task]}
    api.responses[("GET", f"/api/v1/lab/cycles/{cycle}/outputs")] = {
        "items": [
            {
                "kind": "RESEARCH_PACKET",
                "body": {"item_key": task["item_key"], "state": {"thesis": "x"}},
            }
        ]
    }
    answer = {
        "kind": "followup",
        "sources": item()["sources"],
        "thesis": "New thesis",
        "disproof": "Disproof",
        "economic_relationship": "Direct link",
        "technical_facts": {
            "observed_at": NOW.isoformat(),
            "timeframe": "1MIN",
            "summary": "Retest",
            "facts": ["fact"],
            "source_ids": ["issuer"],
        },
    }
    MuseWorker(spool, api, Provider([answer]), clock=lambda: NOW).poll()
    body = json.loads(spool.pending()[0]["body"])
    assert body["task_id"] == task_id and body["revision"] == 2
    assert spool.get("cursor") == "8"


def test_noop_expiry_and_position_news(tmp_path):
    spool, api = MuseSpool(tmp_path / "spool.db"), Api()
    worker = MuseWorker(
        spool,
        api,
        Provider([{"kind": "no_op", "reason": "NO_SETUP", "items": []}]),
        clock=lambda: NOW,
    )
    assert worker.prepare_report("CRYPTO") is None
    spool.enqueue("POST", "/api/v1/lab/research-reports", {"x": 1}, NOW - timedelta(seconds=1))
    worker.flush()
    assert not spool.pending() and not api.calls

    api.responses[("GET", "/api/v1/lab/positions")] = {
        "items": [{"setup_id": "s1", "state": {"state": "OPEN"}}]
    }
    api.responses[("GET", "/api/v1/lab/positions/s1/news")] = {
        "lifecycle_id": "life",
        "revision": 2,
    }
    MuseWorker(
        spool,
        api,
        Provider([{"kind": "news", "sources": item()["sources"], "material": True}]),
        clock=lambda: NOW,
    ).refresh_position_news()
    news = json.loads(spool.pending()[0]["body"])
    assert news["expected_news_revision"] == 2
    assert news["lifecycle_id"] == "life" and news["news_id"]
    assert set(news) == {"news_id", "lifecycle_id", "expected_news_revision", "sources"}


def test_http_allowlist_and_sanitized_timeout():
    def failure(request, timeout):
        raise TimeoutError("secret body")

    api = MuseHttp("http://127.0.0.1:8770", "private-token", opener=failure)
    with pytest.raises(ValueError, match="NOT_ALLOWED"):
        api.call("POST", "/v2/orders", {})
    with pytest.raises(RuntimeError, match="UNCERTAIN") as caught:
        api.call("GET", "/api/v1/lab/positions")
    assert "secret" not in str(caught.value)


def test_poll_drains_more_than_one_claim_page_and_remembers_cycle(tmp_path):
    cycle = "00000000-0000-4000-8000-000000000009"

    class PagedApi:
        def __init__(self):
            self.global_reads = 0
            self.claims = 0

        def call(self, method, path, body=None):
            if path.startswith("/api/v1/lab/outputs"):
                self.global_reads += 1
                if self.global_reads == 1:
                    return {
                        "items": [{"kind": "RESEARCH_EVIDENCE_TASK", "body": {"cycle_id": cycle}}],
                        "next_cursor": 1,
                    }
                return {"items": [], "next_cursor": 1}
            if path.startswith(f"/api/v1/lab/cycles/{cycle}/outputs"):
                return {"items": [], "next_cursor": 0}
            if path.endswith("/evidence-tasks/claim"):
                start = self.claims * 10
                self.claims += 1
                count = 10 if start == 0 else 2 if start == 10 else 0
                return {
                    "tasks": [
                        {
                            "task_id": f"task-{index}",
                            "item_key": f"US_STOCKS:T{index}",
                            "revision": 1,
                            "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
                        }
                        for index in range(start, start + count)
                    ]
                }
            return {}

    reply = {
        "kind": "followup",
        "sources": item()["sources"],
        "thesis": "new",
        "disproof": "disproof",
        "economic_relationship": "direct",
        "technical_facts": None,
    }
    spool, api = MuseSpool(tmp_path / "spool.db"), PagedApi()
    MuseWorker(spool, api, Provider([reply] * 12), clock=lambda: NOW).poll()
    assert len(spool.pending()) == 12
    assert spool.cycles() == [cycle]
    assert api.claims == 2


def test_malformed_cached_job_is_terminal_and_does_not_block_other_work(tmp_path):
    spool, api = MuseSpool(tmp_path / "spool.db"), Api()
    provider = Provider([{"kind": "report"}, {"kind": "report", "items": [item()]}])
    worker = MuseWorker(spool, api, provider, clock=lambda: NOW)
    assert worker.prepare_report("US_STOCKS") is None
    assert worker.prepare_report("CRYPTO")
    assert len(provider.calls) == 2

    api.responses[("GET", "/api/v1/lab/positions")] = {
        "items": [
            {"setup_id": "bad", "state": {"state": "OPEN"}},
            {"setup_id": "good", "state": {"state": "OPEN"}},
        ]
    }
    for setup in ("bad", "good"):
        api.responses[("GET", f"/api/v1/lab/positions/{setup}/news")] = {
            "lifecycle_id": "life-" + setup,
            "news_revision": 0,
        }
    news_provider = Provider(
        [
            {"kind": "news", "material": True},
            {"kind": "news", "material": True, "sources": item()["sources"]},
        ]
    )
    MuseWorker(spool, api, news_provider, clock=lambda: NOW).refresh_position_news()
    assert any(row["path"].endswith("/good/news") for row in spool.pending())
    calls = len(news_provider.calls)
    MuseWorker(spool, api, news_provider, clock=lambda: NOW).refresh_position_news()
    assert len(news_provider.calls) == calls


def test_blocked_research_lane_does_not_block_delivery(tmp_path):
    path = tmp_path / "spool.db"
    MuseSpool(path).enqueue(
        "POST",
        "/api/v1/lab/research-reports",
        {"report_id": "prepared"},
        # service_lanes uses the production real-time clock, so this delivery
        # deadline must use that same clock domain instead of the fixed fixture time.
        datetime.now(UTC) + timedelta(minutes=5),
    )
    invoked, delivered, release, stop = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    class BlockingProvider:
        def invoke(self, request, schema=None):
            invoked.set()
            release.wait(5)
            return {"kind": "no_op", "reason": "fixture", "items": []}

    class LaneApi:
        def call(self, method, path, body=None):
            if method == "POST" and path == "/api/v1/lab/research-reports":
                delivered.set()
                return {}
            if path == "/api/v1/lab/positions":
                return {"items": []}
            if path.startswith("/api/v1/lab/outputs"):
                return {"items": [], "next_cursor": 0}
            return {}

    lanes = service_lanes(path, LaneApi, BlockingProvider, stop, 1800, 1800)
    assert invoked.wait(2)
    assert delivered.wait(2)
    stop.set()
    release.set()
    for lane in lanes:
        lane.join(2)
    assert not MuseSpool(path).pending()


def test_permanent_cycle_reject_is_terminal_without_blocking_newer_cycle(tmp_path):
    class MixedPolicyApi:
        def __init__(self):
            self.global_reads = 0
            self.claimed = []

        def call(self, method, path, body=None):
            if path.startswith("/api/v1/lab/outputs"):
                self.global_reads += 1
                if self.global_reads == 1:
                    return {
                        "items": [
                            {"kind": "RESEARCH_STARTED", "body": {"cycle_id": "old"}},
                            {"kind": "RESEARCH_STARTED", "body": {"cycle_id": "new"}},
                        ],
                        "next_cursor": 2,
                    }
                return {"items": [], "next_cursor": 2}
            if "/cycles/old/" in path:
                raise ValueError("MUSE_REQUEST_REJECTED")
            if path.startswith("/api/v1/lab/cycles/new/outputs"):
                return {"items": [], "next_cursor": 0}
            if path.endswith("/evidence-tasks/claim"):
                self.claimed.append(path)
                return {"tasks": []}
            raise AssertionError(path)

    spool, api = MuseSpool(tmp_path / "spool.db"), MixedPolicyApi()
    MuseWorker(spool, api, Provider([]), clock=lambda: NOW).poll()
    assert api.claimed == ["/api/v1/lab/cycles/new/evidence-tasks/claim"]
    assert spool.cycles() == ["new"]


def test_report_ids_are_installation_scoped_and_restart_stable(tmp_path):
    first = MuseSpool(tmp_path / "first.db")
    second = MuseSpool(tmp_path / "second.db")
    first_id = MuseWorker(
        first,
        Api(),
        Provider([{"kind": "report", "items": [item()]}]),
        clock=lambda: NOW,
    ).prepare_report("CRYPTO")
    second_id = MuseWorker(
        second,
        Api(),
        Provider([{"kind": "report", "items": [item()]}]),
        clock=lambda: NOW,
    ).prepare_report("CRYPTO")
    assert first_id != second_id
    assert first.installation_id() == MuseSpool(tmp_path / "first.db").installation_id()


def test_output_cursors_paginate_large_technical_packet_streams_in_bounded_pages(tmp_path):
    cycle = "00000000-0000-4000-8000-000000000077"

    class TechnicalPages:
        def __init__(self):
            self.paths = []

        def call(self, method, path, body=None):
            self.paths.append(path)
            if path == "/api/v1/lab/outputs?after=0&limit=10":
                return {
                    "items": [{"kind": "RESEARCH_STARTED", "body": {"cycle_id": cycle}}],
                    "next_cursor": 1,
                }
            if path == "/api/v1/lab/outputs?after=1&limit=10":
                return {"items": [], "next_cursor": 1}
            prefix = f"/api/v1/lab/cycles/{cycle}/outputs"
            if path == prefix + "?after=0&limit=10":
                return {
                    "items": [
                        {
                            "kind": "RESEARCH_PACKET",
                            "body": {
                                "item_key": f"US_STOCKS:T{index}",
                                "state": {"technical_context": {"observed_facts": {"bars": 64}}},
                            },
                        }
                        for index in range(10)
                    ],
                    "next_cursor": 10,
                }
            if path == prefix + "?after=10&limit=10":
                return {
                    "items": [
                        {
                            "kind": "RESEARCH_PACKET",
                            "body": {
                                "item_key": "US_STOCKS:T10",
                                "state": {"technical_context": {"observed_facts": {"bars": 64}}},
                            },
                        }
                    ],
                    "next_cursor": 11,
                }
            if path == prefix + "?after=11&limit=10":
                return {"items": [], "next_cursor": 11}
            if path.endswith("/evidence-tasks/claim"):
                return {"tasks": []}
            raise AssertionError(path)

    api = TechnicalPages()
    MuseWorker(MuseSpool(tmp_path / "spool.db"), api, Provider([]), clock=lambda: NOW).poll()
    assert f"/api/v1/lab/cycles/{cycle}/outputs?after=10&limit=10" in api.paths
    assert all("limit=1000" not in path for path in api.paths)
