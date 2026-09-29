"""The trader's public surface on Railway: only /health and static pages without a token.

Every route of the real managed app is enumerated, so a route added later without bearer auth
fails here (package cloud). Disposable PostgreSQL and a fixture runtime only.
"""

import inspect
import re
from dataclasses import replace
from uuid import uuid4

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from catalyst_lab import cloud_runtime, managed_service
from catalyst_lab.managed_app import create_application
from tests.test_managed_app import TOKEN
from tests.test_managed_app import runtime as runtime  # noqa: F401  (fixture)
from tests.test_managed_app import settings as settings  # noqa: F401  (fixture)
from tests.test_managed_app import source_factory as source_factory  # noqa: F401

STATUS = "fixture-cloud-surface-status-token-abcdefghijklmnopqrstuvwxyz"
OPERATOR = "fixture-cloud-surface-operator-token-abcdefghijklmnopqrstuvwxyz"
AGENT = "fixture-cloud-surface-agent-token-abcdefghijklmnopqrstuvwxyz"
OPEN = {("GET", "/health"), ("GET", "/"), ("GET", "/lab.js"), ("GET", "/lab.css"),
        ("GET", "/results"), ("GET", "/results.js")}
STATIC = {"/": managed_service.HTML, "/lab.js": managed_service.JS,
          "/lab.css": managed_service.CSS, "/results": managed_service.RESULTS_HTML,
          "/results.js": managed_service.RESULTS_JS}


def client(runtime, settings, source_factory):
    cloud = replace(settings, status_token=STATUS, operator_token=OPERATOR,
                    agent_tokens={"muse": AGENT})
    app = create_application(runtime, cloud, source_factory=source_factory[0])
    return app, TestClient(app, raise_server_exceptions=False)


def concrete(path):
    return re.sub(r"\{[^}]+\}", str(uuid4()), path)


def test_every_data_route_requires_a_bearer_token(runtime, settings, source_factory):
    app, http = client(runtime, settings, source_factory)
    routes = [(method, route.path) for route in app.routes if isinstance(route, APIRoute)
              for method in route.methods if method != "HEAD"]
    assert set(OPEN) <= set(routes)
    guarded = [(m, p) for m, p in routes if (m, p) not in OPEN]
    assert len(guarded) > 20
    for method, path in guarded:
        for headers in ({}, {"Authorization": "Bearer " + "x" * 40}):
            response = http.request(method, concrete(path), headers=headers, json={})
            assert response.status_code == 401, (method, path)
            assert response.json() == {"detail": "AUTHENTICATION_REQUIRED"}


def test_health_and_static_pages_are_constant_and_carry_no_data(runtime, settings,
                                                               source_factory):
    app, http = client(runtime, settings, source_factory)
    with http:
        for path, body in STATIC.items():
            response = http.get(path)
            assert response.status_code == 200 and response.text == body
            for secret in (TOKEN, STATUS, OPERATOR, AGENT):
                assert secret not in response.text
        health = http.get("/health")
        assert health.status_code == 200
        assert set(health.json()) == {"status", "alpaca", "surface", "cohort", "supervised"}
        for field in ("entry_ready", "worker_state", "ready", "executor_ownership"):
            assert field not in health.text
        # The status route holds the readiness, behind the status token.
        status = http.get("/api/v1/lab/status", headers={"Authorization": "Bearer " + STATUS})
        assert status.status_code == 200 and "entry_ready" in status.json()
        assert http.get("/api/v1/lab/status", headers={
            "Authorization": "Bearer " + AGENT}).status_code == 403


def test_the_railway_health_answers_from_memory_without_a_database_session(
        runtime, settings, source_factory, monkeypatch):
    """package cloud-hardening: /health is on the trader's public domain. A flood of it must not
    take the database sessions and threads that status, Muse, the watchdog and the trader's own
    authorization and protection writes need; readiness stays in the token-protected status."""
    repo = runtime.execution.store.repo
    real, opened = repo.connect, []

    def counted(*args, **kwargs):
        opened.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(repo, "connect", counted)
    cloud = replace(settings, status_token=STATUS, operator_token=OPERATOR,
                    agent_tokens={"muse": AGENT})
    railway = create_application(runtime, cloud, source_factory=source_factory[0],
                                 health_check_database=False)
    [route] = [r for r in railway.routes if isinstance(r, APIRoute) and r.path == "/health"]
    assert inspect.iscoroutinefunction(route.endpoint)  # Served on the event loop, no thread.
    http = TestClient(railway, raise_server_exceptions=False)
    opened.clear()  # Building the app checks its databases once; requests are what count.
    for _ in range(200):
        response = http.get("/health")
        assert response.status_code == 200
    assert opened == []  # Not one database session for 200 public requests.
    assert set(response.json()) == {"status", "alpaca", "surface", "cohort", "supervised"}
    # The Mac app on loopback keeps its database probe: one session per request, unchanged.
    mac = create_application(runtime, cloud, source_factory=source_factory[0])
    opened.clear()
    assert TestClient(mac).get("/health").status_code == 200 and len(opened) == 1


def test_the_starting_responder_answers_health_only():
    body = cloud_runtime.HEALTH_STARTING
    assert set(body) == {"status", "alpaca", "surface", "supervised", "phase"}
    assert body["phase"] == "STARTING" and "ready" not in str(body).lower()
