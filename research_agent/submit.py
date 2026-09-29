"""POST research records to the app, with the agent's own bearer token.

The package's only side-effecting calls, and each is a write to one of the app's research
intakes, never a broker order: ``research_agent`` adds no broker order code path or
endpoint at all (paper-only, per the package's hard rules). Three routes, each reached
only by its own explicit CLI step (``submit``, ``outlook-submit``, ``postmortem-submit``),
never by the deterministic ones, so nothing reaches the app without a human or scheduler
asking for that exact step:

* ``POST /api/v1/lab/research-reports``: the morning's ``AGENT_RESEARCH_REPORT_V3``;
* ``POST /api/v1/lab/market-outlooks``: the morning's ``MARKET_OUTLOOK_V1`` (package
  learning-kit, 2026-09-28);
* ``POST /api/v1/lab/post-mortems``: the evening's ``POST_MORTEM_V1`` notes.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

SUBMIT_ROUTE = "/api/v1/lab/research-reports"
OUTLOOK_ROUTE = "/api/v1/lab/market-outlooks"
POST_MORTEM_ROUTE = "/api/v1/lab/post-mortems"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class BaseUrlRefused(ValueError):
    """The app base URL would send the bearer token somewhere it must not go."""


def checked_base_url(base_url):
    """The app's base URL: ``https://`` (the Railway app), or ``http://`` only on this
    machine's loopback (a local app or the session harness). The agent's bearer token never
    crosses a network in clear text, and never goes to a URL with credentials, a query or a
    fragment."""
    try:
        url = urlsplit(base_url)
        host = url.hostname
    except (TypeError, ValueError, AttributeError):
        raise BaseUrlRefused("RESEARCH_AGENT_BASE_URL_REFUSED: not a URL") from None
    secure = url.scheme == "https" and bool(host)
    local = url.scheme == "http" and host in LOOPBACK_HOSTS
    if (not (secure or local) or url.username or url.password or url.query or url.fragment
            or url.path not in {"", "/"}):
        raise BaseUrlRefused(
            "RESEARCH_AGENT_BASE_URL_REFUSED: use https://<app host> "
            "(http:// only for 127.0.0.1 or localhost), with no path, query or credentials"
        )
    return base_url.rstrip("/")


class SubmitError(Exception):
    """A transport-level failure (connection refused, timeout, TLS, ...) — never a 4xx
    or 5xx response, which comes back as an ordinary ``SubmitResult`` instead."""


@dataclass(frozen=True)
class SubmitResult:
    status_code: int
    body: dict | None  # Parsed JSON body, or None when the response was not JSON.
    raw_text: str

    @property
    def accepted(self):
        return self.status_code == 202


def post_json(base_url, token, route, body, *, client=None, timeout=30.0,
              error_code="RESEARCH_AGENT_POST_CONNECTION_ERROR"):
    """POST ``body`` (a plain JSON-safe dict) to ``route`` on the app.

    ``client`` is an injectable ``httpx.Client`` (tests use one on
    ``httpx.MockTransport``; a real client is opened and closed when omitted). Returns a
    ``SubmitResult`` for any HTTP response at all, 2xx or not, so the caller decides what
    counts as success; raises ``SubmitError`` only when the request itself could not be
    made. The token is used exactly once, in the ``Authorization`` header, and is never
    included in the returned result or logged.
    """
    try:
        base_url = checked_base_url(base_url)
    except BaseUrlRefused as exc:
        raise SubmitError(str(exc)) from None
    # Pre-encoded bytes go out exactly as given, so a resend can be byte-identical to the
    # first send (outlook-submit keeps them in outlook-sent.json).
    payload = ({"content": body} if isinstance(body, bytes) else {"json": body})
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    if isinstance(body, bytes):
        headers["Content-Type"] = "application/json"
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        try:
            response = client.post(base_url + route, headers=headers, **payload)
        except httpx.HTTPError as exc:
            raise SubmitError(f"{error_code}: {type(exc).__name__}") from None
    finally:
        if owns_client:
            client.close()
    try:
        parsed = response.json()
    except ValueError:
        parsed = None
    return SubmitResult(status_code=response.status_code, body=parsed, raw_text=response.text)


def submit_report(base_url, token, report, *, client=None, timeout=30.0):
    """POST one ``AGENT_RESEARCH_REPORT_V3`` to the research-reports intake."""
    return post_json(base_url, token, SUBMIT_ROUTE, report, client=client, timeout=timeout,
                     error_code="RESEARCH_REPORT_SUBMIT_CONNECTION_ERROR")


def submit_outlook(base_url, token, outlook, *, client=None, timeout=30.0):
    """POST one ``MARKET_OUTLOOK_V1`` (202 ``MARKET_OUTLOOK_RECORDED`` when accepted)."""
    return post_json(base_url, token, OUTLOOK_ROUTE, outlook, client=client, timeout=timeout,
                     error_code="MARKET_OUTLOOK_SUBMIT_CONNECTION_ERROR")


def submit_post_mortem(base_url, token, note, *, client=None, timeout=30.0):
    """POST one ``POST_MORTEM_V1`` note of up to 30 items (202 ``POST_MORTEM_RECORDED``, with
    ``item_results``, when at least one item was accepted)."""
    return post_json(base_url, token, POST_MORTEM_ROUTE, note, client=client, timeout=timeout,
                     error_code="POST_MORTEM_SUBMIT_CONNECTION_ERROR")
