"""The public live dashboard service: the page shell, the JSON, credential refusal, the login
check, security headers, the 5-second cache, health, the port rule and no private field in any
response.

Disposable PostgreSQL only, with this module's own cluster (catalyst_public gets LOGIN here as the
cloud provisioner grants it in production). The populated ledger is tests/experiment_fixtures.py's
demo experiment. The service never calls an exchange: every price comes from the ledger.
"""

import json
import re
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from catalyst_lab import experiment_html, experiment_page
from catalyst_lab.experiment_html import PRELOAD_FONTS, asset_version
from catalyst_lab.experiment_page import (
    CACHE_SECONDS,
    CSP,
    DEFAULT_PORT,
    FONT_FILES,
    STALE_LIMIT_SECONDS,
    ExperimentRefused,
    ReportCache,
    create_experiment_app,
    parse_args,
)
from catalyst_lab.experiment_report import DEFAULT_TITLE, FIXTURE_BANNER
from catalyst_lab.research_schedule import ResearchSchedule
from tests.experiment_fixtures import (
    FIXTURE_ACCOUNT_MARKER,
    FIXTURE_AGENT_ID,
    ExperimentLedger,
    build_demo_experiment,
    enable_public_login,
    public_url,
)
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401

NEW_YORK = ZoneInfo("America/New_York")
CENT = D("0.01")

SOURCE = Path(experiment_page.__file__).parent
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
HEX64 = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
# EXPERIMENT_DASHBOARD_V3 (package public-page-v3): the section titles.
HEADINGS = ("Trading P&amp;L", "Performance", "Open positions", "Closed today", "Past days")
SCRIPT = SOURCE / "static" / "experiment.js"
REMOVED = ("Not investment advice", "simulated money", "The questions", "How it works",
           "The rules", "Honest notes", "Too early", "PUBLIC_EXPERIMENT_REPORT_V1",
           "Rules change only as named versions")
PRIVATE_WORDS = ("setup_id", "cycle_id", "receipt", "order_id", "fill_id", "client_order",
                 "account_id", "account_hash", "evidence_hash", "lifecycle", "idempotency",
                 "broker_source", "correction_id", "thesis", "excerpt", "configuration_hash",
                 "runtime_id", "request_hash", "flag_ref", "item_key", FIXTURE_ACCOUNT_MARKER,
                 "fixture-order-", "fixture-fill-", "fixture-activity-", "Fixture source excerpt",
                 "report text withheld", "fixture-source", "fixture-news", "TEST-FIXTURE",
                 "PKFIXTURE", "WIF")


@pytest.fixture(scope="module", autouse=True)
def public_login(pristine_cluster):  # noqa: F811
    enable_public_login(pristine_cluster)


def client(er, **kwargs):  # noqa: F811
    kwargs.setdefault("environ", {})
    return TestClient(create_experiment_app(public_url(er.database_url), **kwargs))


@pytest.fixture
def demo(er):  # noqa: F811
    ledger = ExperimentLedger(er.database_url)
    ledger.built_at = datetime.now(UTC)
    runs, trades = build_demo_experiment(ledger, ledger.built_at)
    ledger.engineering_trade("WIF/USD", at=datetime.now(UTC) - timedelta(hours=3))
    return ledger, runs, trades


def embedded(html):
    """The first document, as the page embeds it for its script."""
    match = re.search(r'<script type="application/json" id="initial-data">(.*?)</script>', html,
                      re.S)
    return json.loads(match.group(1))


# --- The page shell -----------------------------------------------------------------------------


def test_an_empty_ledger_serves_the_dashboard_shell_and_its_first_data(er):  # noqa: F811
    with client(er) as c:
        page = c.get("/")
        data = c.get("/api/public/experiment").json()
    assert page.status_code == 200
    html = page.text
    assert f"<title>{DEFAULT_TITLE}</title>" in html and f">{DEFAULT_TITLE}</h1>" in html
    for heading in HEADINGS:
        assert f">{heading}</h2>" in html, heading
    for gone in REMOVED:
        assert gone not in html and gone not in json.dumps(data), gone
    assert "FIXTURE DATA" not in html and "fixture-banner" not in html
    assert f'<script defer src="/experiment.js?v={asset_version()}"></script>' in html
    assert '<a href="/api/public/experiment">' in html  # the noscript fallback
    # The status line's band (filled by the script) and the masthead's account line.
    assert re.search(r'<div class="status-band" id="status" role="status" aria-live="polite">',
                     html)
    assert '<div class="updated" id="updated">Paper account</div>' in html
    assert 'data-view="main"' in html
    for name in PRELOAD_FONTS:  # same-origin fonts, preloaded
        assert (f'<link rel="preload" href="/fonts/{name}" as="font" type="font/woff2" '
                'crossorigin>') in html
    first = embedded(html)
    assert first["dashboard_version"] == data["dashboard_version"] == "EXPERIMENT_DASHBOARD_V3"
    assert data["status"]["pill"] == "STOPPED" and data["system"]["state"] == "STOPPED"
    assert data["results"]["since_start"]["trades"] == 0 and data["market"]["day"] is None
    assert (data["overall"]["closed"], data["overall"]["open"], data["overall"]["pnl_usd"],
            data["overall"]["win_rate"], data["overall"]["equity_usd"],
            data["overall"]["account_label"]) == (0, 0, "0.00", None, None, "Paper account")
    assert (data["today"]["picks"], data["today"]["selected"]) == (0, 0)
    assert data["live_trades"] == [] and data["feed"] == [] and data["past"]["days"] == []
    assert data["agents"]["research"] == [] and data["agents"]["today_runs"] == []
    assert (data["agents"]["cycle"], data["agents"]["pending_run"],
            data["agents"]["jev"]["cycle"], data["agents"]["jev"]["today"]["lines"]) == (
        None, None, None, [])
    assert data["agents"]["schedule"]["text"] == "Daily at 08:00"  # the default schedule
    assert data["agents"]["jev"]["health"] == "No calls yet"
    assert (data["agents"]["jev"]["failed_today"], data["agents"]["jev"]["failed_today_more"]) \
        == (0, False)
    assert data["fixture_data"] is False and data["data_label"] is None
    assert "served_at" in data and "stale" not in data


def test_the_title_comes_from_configuration_and_is_escaped(er):  # noqa: F811
    with client(er, title="Our <paper> lab") as c:
        html = c.get("/").text
    assert "<title>Our &lt;paper&gt; lab</title>" in html and "<paper>" not in html
    assert embedded(html)["title"] == "Our <paper> lab"


def test_the_embedded_data_cannot_close_its_script_element(er):  # noqa: F811
    title = "</script><script>alert(1)</script>"
    with client(er, title=title) as c:
        html = c.get("/").text
    assert "<script>alert" not in html
    assert html.count("</script>") == 2  # the page's own script and the data block
    assert embedded(html)["title"] == title


# --- Refusals and the login ------------------------------------------------------------------


@pytest.mark.parametrize("name", ["APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "TYPESAFE_API_KEY"])
def test_refuses_to_start_when_trading_credentials_are_present(er, name, capsys):  # noqa: F811
    value = "not-a-real-value-" + name.lower()
    with pytest.raises(ExperimentRefused) as refused:
        create_experiment_app(public_url(er.database_url), environ={name: value})
    assert refused.value.code == "EXPERIMENT_REFUSES_TRADING_CREDENTIALS"
    assert name in str(refused.value) and value not in str(refused.value)
    with pytest.raises(SystemExit) as exited:
        experiment_page.main([], environ={name: value,
                                          "EXPERIMENT_DATABASE_URL": public_url(er.database_url)})
    assert exited.value.code == 2
    err = capsys.readouterr().err
    assert name in err and value not in err


def test_refuses_a_login_that_is_not_catalyst_public(er):  # noqa: F811
    app = create_experiment_app(er.database_url, environ={})  # catalyst_app
    with pytest.raises(ExperimentRefused, match="EXPERIMENT_REQUIRES_CATALYST_PUBLIC_ROLE"):
        with TestClient(app):
            pass


def test_an_unreachable_database_answers_503_not_stale_data(er):  # noqa: F811
    url = public_url(er.database_url).replace("port=55437", "port=55499")
    with TestClient(create_experiment_app(url, environ={})) as c:
        assert c.get("/health").status_code == 503
        assert c.get("/").status_code == 503
        assert c.get("/api/public/experiment").json() == {"detail": "Experiment data unavailable"}


def test_a_database_without_the_public_views_starts_and_answers_503(er):  # noqa: F811
    """A reachable database that is not (yet) a schema-24 ledger: no crash loop, just 503."""
    url = re.sub(r"dbname=\w+", "dbname=postgres", public_url(er.database_url))
    with TestClient(create_experiment_app(url, environ={})) as c:
        assert c.get("/health").status_code == 503
        assert c.get("/").status_code == 503


def test_the_fixture_flag_cannot_be_set_from_the_environment_or_command_line(er, monkeypatch):  # noqa: F811
    captured = {}

    def fake_app(url, **kwargs):
        captured.update(kwargs, url=url)
        return "app"

    monkeypatch.setattr(experiment_page, "create_experiment_app", fake_app)
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: captured.update(run=kwargs))
    environ = {"EXPERIMENT_DATABASE_URL": public_url(er.database_url), "PORT": "9123",
               "EXPERIMENT_TITLE": "Title", "EXPERIMENT_FIXTURE_DATA": "1",
               "FIXTURE_DATA": "true"}
    experiment_page.main(["--host", "0.0.0.0"], environ=environ)
    assert "fixture_data" not in captured
    assert captured["title"] == "Title" and captured["schedule"] is None
    assert (captured["run"]["host"], captured["run"]["port"]) == ("0.0.0.0", 9123)
    with pytest.raises(SystemExit):
        experiment_page.main(["--fixture-data"], environ=environ)


def test_the_research_schedule_is_read_from_its_variable(er, monkeypatch, capsys):  # noqa: F811
    captured = {}
    monkeypatch.setattr(experiment_page, "create_experiment_app",
                        lambda url, **kwargs: captured.update(kwargs) or "app")
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: None)
    environ = {"EXPERIMENT_DATABASE_URL": public_url(er.database_url),
               "EXPERIMENT_RESEARCH_SCHEDULE_JSON":
               '{"timezone":"America/New_York","runs":["08:00","14:00"]}'}
    experiment_page.main([], environ=environ)
    assert isinstance(captured["schedule"], ResearchSchedule)
    with pytest.raises(SystemExit) as exited:
        experiment_page.main([], environ={**environ, "EXPERIMENT_RESEARCH_SCHEDULE_JSON": "daily"})
    assert exited.value.code == 2
    assert "EXPERIMENT_RESEARCH_SCHEDULE_JSON" in capsys.readouterr().err


@pytest.mark.parametrize("environ,expected", [({"PORT": "9123"}, 9123), ({}, DEFAULT_PORT),
                                              ({"PORT": ""}, DEFAULT_PORT)])
def test_the_port_defaults_to_the_port_variable_else_8080(environ, expected):
    assert parse_args([], environ).port == expected
    assert parse_args(["--port", "7001"], environ).port == 7001
    assert parse_args([], environ).host == "127.0.0.1"


def test_an_invalid_port_or_a_missing_database_url_refuses_to_start(capsys):
    with pytest.raises(SystemExit):
        parse_args([], {"PORT": "eighty"})
    with pytest.raises(SystemExit) as exited:
        experiment_page.main([], environ={})
    assert exited.value.code == 2 and "EXPERIMENT_DATABASE_URL" in capsys.readouterr().err


# --- Headers, the script and caching ---------------------------------------------------------


def test_every_response_carries_the_strict_security_headers(er):  # noqa: F811
    assert CSP.startswith("default-src 'none'")
    directives = dict(part.strip().split(" ", 1) for part in CSP.split(";"))
    assert directives["script-src"] == directives["style-src"] == "'self'"
    assert directives["font-src"] == directives["img-src"] == "'self'"
    # V3: the service reads the public market data itself; the browser reads this origin only.
    assert directives["connect-src"] == "'self'"
    assert "unsafe-inline" not in CSP and "unsafe-eval" not in CSP and "*" not in CSP
    with client(er) as c:
        responses = [c.get(path) for path in (
            "/", "/experiment.css", "/experiment.js", "/favicon.svg", "/health",
            "/api/public/experiment", "/api/public/experiment/status",
            "/fonts/IBMPlexMono-Regular.woff2", "/api/public/experiment/nope", "/missing")]
        page = responses[0].text
    for response in responses:
        assert response.headers["content-security-policy"] == CSP
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
    assert [r.status_code for r in responses[-2:]] == [404, 404]
    assert responses[1].headers["content-type"].startswith("text/css")
    assert responses[2].headers["content-type"].startswith("text/javascript")
    assert responses[7].headers["content-type"] == "font/woff2"
    scripts = re.findall(r"<script\b[^>]*>", page)
    assert scripts == [f'<script defer src="/experiment.js?v={asset_version()}">',
                       '<script type="application/json" id="initial-data">']
    assert not re.search(r"<[^>]+\son\w+=", page)  # no inline event handlers
    assert not re.search(r"(src|href)=\"(https?:)?//", page)  # no third-party resource


def test_the_page_links_its_stylesheet_and_script_by_content_version(er, monkeypatch,  # noqa: F811
                                                                     tmp_path):
    """A release changes the asset URLs, so no browser pairs a new page with an old cached copy
    (the stylesheet is cached for an hour)."""
    version = asset_version()
    assert re.fullmatch(r"[0-9a-f]{12}", version)
    with client(er) as c:
        page = c.get("/").text
        css = c.get(f"/experiment.css?v={version}")
        script = c.get(f"/experiment.js?v={version}")
    assert f'<link rel="stylesheet" href="/experiment.css?v={version}">' in page
    assert css.status_code == script.status_code == 200
    assert css.text == (SOURCE / "static" / "experiment.css").read_text()
    assert script.headers["content-type"].startswith("text/javascript")
    for name in ("experiment.css", "experiment.js"):
        (tmp_path / name).write_text((SOURCE / "static" / name).read_text() + "\n/* x */\n")
    monkeypatch.setattr(experiment_html, "STATIC", tmp_path)
    asset_version.cache_clear()
    try:
        assert re.fullmatch(r"[0-9a-f]{12}", asset_version()) and asset_version() != version
    finally:
        monkeypatch.undo()
        asset_version.cache_clear()
    assert asset_version() == version


def script_code():
    return "\n".join(line for line in SCRIPT.read_text().splitlines()
                     if not line.strip().startswith("//"))  # code, not comments


def test_the_script_builds_the_page_with_dom_calls_and_reads_only_its_own_json():
    """V3: its own JSON and its own chart route only (the service reads the public market data
    server side); the charts are inline SVG built with DOM calls."""
    script = script_code()
    for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
                   "new Function", "localStorage", "sessionStorage", "document.cookie"):
        assert unsafe not in script, unsafe
    assert script.count("fetch(") == 2
    assert re.findall(r"fetch\((.+?), \{", script) == [
        "'/api/public/experiment/trades/' + number + '/chart'", "'/api/public/experiment'"]
    assert re.findall(r"https?://[^'\s]+", script) == ["http://www.w3.org/2000/svg"]
    for private in ("apca", "authorization", "api-key", "secret", "token", "alpaca"):
        assert private not in script.lower(), private
    assert "POLL_MS = 30000" in script and "DIM_AFTER_MS" in script
    assert "if (document.hidden || polling) return;" in script
    assert "POLL_TIMEOUT_MS = 20000" in script and "signal: abort.signal" in script
    assert "const TZ = 'America/New_York'" in script  # every time on the page is ET


def test_the_design_tokens_are_the_owner_approved_ones():
    """The 2026-10-03 design: white ground, one ink, thin rules, one accent, gains and losses
    with their sign, IBM Plex, no shadows or gradients; phone width with a 16 px gutter."""
    css = (SOURCE / "static" / "experiment.css").read_text()
    for token in ("--bg: #FFFFFF", "--ink: #111418", "--muted: #5B6470", "--rule: #E2E5E9",
                  "--accent: #1D4ED8", "--gain: #0B7A47", "--loss: #B42318"):
        assert token in css, token
    for effect in ("gradient", "box-shadow", "blur(", "text-shadow", "drop-shadow",
                   "border-radius"):
        assert effect not in css, effect
    for rule in (":focus-visible", "@media (max-width: 640px)", "padding-left: 16px",
                 ".table-wrap { overflow-x: auto;", "prefers-reduced-motion: reduce",
                 '.status-band[data-state="HALTED"]', '.status-band[data-state="PAUSED"]',
                 '.status-band[data-state="SOFT_LIMIT"]', '.status-band[data-state="TRADING"]'):
        assert rule in css, rule
    script = script_code()
    assert "(x > 0 ? '+' : MINUS)" in script  # signed figures


def test_a_trades_own_page_has_levels_plan_story_and_no_placeholder():
    script = script_code()
    for hook in ("label('Levels')", "label('The plan')", "'What happened'", "'After the sale'",
                 "label('Price')", "if (!p) return null;",
                 "if (!a || !Array.isArray(a.points) || !a.points.length) return null;",
                 "a.href = '/trade/' + t.trade_no", "th.scope = 'col'",
                 "wrap.setAttribute('role', 'region')", "'role', 'img'", "View as table"):
        assert hook in script, hook
    for placeholder in ("[price", "[R]", "[hh:mm]", "lorem"):
        assert placeholder not in script, placeholder
    html = experiment_html.render_page({"title": "t", "fixture_data": False, "data_label": None},
                                       7)
    assert 'data-view="trade" data-trade="7"' in html and '<div id="trade-body"></div>' in html
    main = experiment_html.render_page({"title": "t", "fixture_data": False, "data_label": None})
    for key, title in (("equity", "Trading P&amp;L"), ("performance", "Performance"),
                       ("open", "Open positions"), ("closed", "Closed today"),
                       ("past", "Past days")):
        assert f'<div class="section-head"><h2 id="{key}-title">{title}</h2>' in main, title
    for key in ("account", "tiles", "agents"):
        assert f'<div class="body" id="{key}-body"></div>' in main, key


def test_the_fonts_are_self_hosted_ibm_plex_under_the_ofl(er):  # noqa: F811
    fonts = SOURCE / "static" / "fonts"
    assert {p.name for p in fonts.glob("*.woff2")} == FONT_FILES
    assert "SIL OPEN FONT LICENSE Version 1.1" in (fonts / "OFL.txt").read_text()
    css = (SOURCE / "static" / "experiment.css").read_text()
    assert set(re.findall(r'url\("/fonts/([^"]+)"\)', css)) == FONT_FILES
    assert "IBM Plex Sans" in css and "IBM Plex Mono" in css and "font-display: swap" in css
    with client(er) as c:
        for name in sorted(FONT_FILES):
            response = c.get(f"/fonts/{name}")
            assert response.status_code == 200 and response.content[:4] == b"wOF2", name
            assert response.headers["content-type"] == "font/woff2"
            assert "immutable" in response.headers["cache-control"]
            assert len(response.content) < 1_048_576  # the repository's file cap
        assert c.get("/fonts/OFL.txt").text.startswith("Copyright")
        for bad in ("nope.woff2", "..%2Fexperiment.js", "OFL.TXT"):
            assert c.get(f"/fonts/{bad}").status_code == 404, bad


def test_the_service_calls_no_exchange_or_provider():
    for name in ("experiment_page.py", "experiment_report.py", "experiment_html.py"):
        text = (SOURCE / name).read_text()
        for word in ("httpx", "requests", "urllib", "websocket", "alpaca_", "AlpacaCredentials",
                     "managed_broker", "jev_review", "typesafe_client"):
            assert word not in text, (name, word)


def test_responses_are_cached_for_five_seconds(er, monkeypatch):  # noqa: F811
    assert CACHE_SECONDS == 5
    clock = [1000.0]
    reads = []
    real = experiment_page.read_snapshot

    def counting(conn, *args):  # read_snapshot(conn, schedule) since package research-loop-app.
        reads.append(1)
        return real(conn, *args)

    monkeypatch.setattr(experiment_page, "read_snapshot", counting)
    app = create_experiment_app(public_url(er.database_url), environ={},
                                monotonic=lambda: clock[0])
    with TestClient(app) as c:
        first = c.get("/api/public/experiment")
        c.get("/")
        clock[0] += 4.5
        c.get("/api/public/experiment/live_trades")
        assert len(reads) == 1
        clock[0] += 1
        c.get("/")
        assert len(reads) == 2
    assert first.headers["cache-control"] == "public, max-age=5"
    assert app.state.cache.builds == 2


def test_one_slow_build_at_a_time_and_other_requests_get_the_last_build_at_once():
    """2026-09-29: builds took 6.5 s against the page's 5 s poll. Every waiting request found
    the cache expired and built again, and the queue grew until the page itself stopped
    answering. The cache now counts from the end of a build, one build runs at a time, and a
    request meanwhile gets the last build at once."""
    clock = [0.0]
    second_started, release = threading.Event(), threading.Event()
    builds = []

    def build():
        builds.append(clock[0])
        if len(builds) == 2:
            second_started.set()
            assert release.wait(10)
        clock[0] += 6.5  # the build's own time
        return {"n": len(builds)}

    cache = ReportCache(build, CACHE_SECONDS, monotonic=lambda: clock[0])
    assert cache.get() == {"n": 1}
    clock[0] += 4.9
    assert cache.get() == {"n": 1} and len(builds) == 1  # 4.9 s after the build ended
    clock[0] += 0.2
    answers = []
    builder = threading.Thread(target=lambda: answers.append(("builder", cache.get())))
    builder.start()
    assert second_started.wait(10)
    reader = threading.Thread(target=lambda: answers.append(("reader", cache.get())))
    reader.start()
    reader.join(2)
    try:
        assert answers == [("reader", {"n": 1})]  # at once, while the second build runs
    finally:
        release.set()
        builder.join(10)
    assert answers[-1] == ("builder", {"n": 2}) and cache.builds == 2 and len(builds) == 2


def test_a_request_waits_for_the_running_build_when_the_last_one_is_too_old():
    """No build yet, or the last one older than the stale limit: a request waits for the
    build that is running, and does not build again after it."""
    clock = [0.0]
    started, release = threading.Event(), threading.Event()
    builds = []

    def build():
        builds.append(clock[0])
        if len(builds) == 2:
            started.set()
            assert release.wait(10)
        return {"n": len(builds)}

    cache = ReportCache(build, CACHE_SECONDS, monotonic=lambda: clock[0])
    assert cache.get() == {"n": 1}
    clock[0] += STALE_LIMIT_SECONDS + 1
    answers = []
    builder = threading.Thread(target=lambda: answers.append(cache.get()))
    builder.start()
    assert started.wait(10)
    reader = threading.Thread(target=lambda: answers.append(cache.get()))
    reader.start()
    reader.join(0.5)
    assert reader.is_alive() and answers == []  # waiting: the last build is too old to serve
    release.set()
    builder.join(10)
    reader.join(10)
    assert answers == [{"n": 2}, {"n": 2}] and len(builds) == 2


# --- A populated fixture ledger --------------------------------------------------------------


def test_the_demo_dashboard(er, demo):  # noqa: F811
    with client(er, fixture_data=True) as c:
        html = c.get("/").text
        data = c.get("/api/public/experiment").json()
        live = c.get("/api/public/experiment/live_trades").json()
    assert f'<div class="fixture-banner" role="alert">{FIXTURE_BANNER}</div>' in html
    assert data["fixture_data"] is True and data["data_label"] == FIXTURE_BANNER
    assert data["status"]["pill"] == "RUNNING"
    overall = data["overall"]
    assert (overall["closed"], overall["open"], overall["fees_pending"], overall["equity_usd"],
            overall["account_label"]) == (8, 4, 1, "10084.27", "Paper account")
    days = data["past"]["days"]
    assert days[-1]["cumulative_pnl_usd"] == overall["pnl_usd"]
    assert sum(D(d["pnl_usd"]) for d in days) == D(overall["pnl_usd"])
    # "Today" is the New York day of the database's clock, and the demo's latest run is at
    # 12:00 UTC (08:00 New York) of today or yesterday: between midnight and 10:00 New York it
    # belongs to yesterday. Count the runs the page should (found 2026-09-28 at 01:10 New York).
    _, runs, _ = demo
    page_day = datetime.fromisoformat(data["today"]["day"]).date()
    todays = [r for r in runs if r.run_slot.astimezone(NEW_YORK).date() == page_day]
    assert len(todays) <= 1
    assert (data["today"]["picks"], data["today"]["selected"]) == (20 * len(todays),
                                                                   10 * len(todays))
    trades = live["live_trades"]
    assert live["fixture_data"] is True and len(trades) == 4
    assert {t["tag"] for t in trades} == {"Jev-managed", "Fixed exit"}
    for trade in trades:  # How much each trade holds, and its dollar value at entry and now.
        assert trade["price"] is not None and trade["pnl_usd"] is not None
        qty = D(trade["qty"])
        assert D(trade["entry_value_usd"]) == (qty * D(trade["entry"])).quantize(CENT)
        assert D(trade["value_usd"]) == (qty * D(trade["price"])).quantize(CENT)
        assert D("990") <= D(trade["entry_value_usd"]) <= D("1010")  # the demo buys ~$1,000
    assert D(overall["open_entry_value_usd"]) == sum(
        D(t["qty"]) * D(t["entry"]) for t in trades).quantize(CENT)  # exact, then rounded
    for trade in data["past"]["closed_trades"]:
        assert D(trade["entry_value_usd"]) == (D(trade["qty"]) * D(trade["entry"])).quantize(CENT)
    assert data["past"]["closed_trades"][0]["cumulative_pnl_usd"] == overall["pnl_usd"]
    managed = [t for t in trades if t["tag"] == "Jev-managed"]
    assert all(t["jev_last"]["text"] == "Held" for t in managed)
    assert any(t["jev_last_change"] and t["jev_last_change"]["text"].startswith(
        "Raised stop to breakeven") for t in managed)
    # Each trade's detail: its story from the pick to now (or to its exit) and its levels.
    for trade in trades + data["past"]["closed_trades"]:
        kinds = [e["kind"] for e in trade["events"]]
        assert kinds[:4] == ["PICK", "SELECTION", "BUY", "LEVELS_SET"], (trade["trade_no"], kinds)
        buy = trade["events"][2]
        assert (D(buy["price"]), buy["limit"]) == (D(trade["entry"]), trade["limit"])
        assert (trade["levels"][0]["at"], trade["levels"][0]["stop"]) == (
            trade["entry_at"], trade["planned_stop"])
        if trade["tag"] == "Fixed exit":
            assert "REVIEW" not in kinds and len(trade["levels"]) == 1
    raises = [e for t in managed for e in t["events"] if e.get("outcome") == "APPLIED"]
    assert raises and all(" → " in e["text"] and e["confidence"] for e in raises)
    for trade in data["past"]["closed_trades"]:
        exit_event, result = trade["events"][-2:]
        assert (exit_event["kind"], exit_event["reason"], result["kind"]) == (
            "EXIT", trade["exit_reason"], "RESULT")
        assert (result["pnl_usd"], result["fees_pending"]) == (trade["pnl_usd"],
                                                               trade["fees_pending"])
    assert data["agents"]["schedule"]["text"] == "Daily at 08:00"
    assert data["agents"]["jev"]["failed_today"] == 0
    # The current cycle: the latest run (08:00 New York, daily), valid until the next run
    # plus an hour; its three traded picks are the three open trades.
    cycle = data["agents"]["cycle"]
    assert (cycle["run_no"], cycle["picks"], cycle["selected"], cycle["traded"]) == (
        4, 20, 10, 3)
    as_of = datetime.fromisoformat(data["as_of"])
    valid_until = datetime.fromisoformat(cycle["valid_until"])
    assert valid_until - datetime.fromisoformat(cycle["run_at"]) == timedelta(hours=25)
    statuses = [p["status"] for p in cycle["picks_list"]]
    waiting = "EXPIRED" if as_of >= valid_until else "NO_ENTRY"
    assert statuses == ["IN_TRADE"] * 2 + [waiting] * 4 + ["IN_TRADE"] + [waiting] * 3 + [
        "NOT_SELECTED"] * 10
    traded = {p["trade_no"] for p in cycle["picks_list"] if p["trade_no"]}
    assert len(traded) == 3 and traded < {t["trade_no"] for t in trades}  # one is older
    assert data["agents"]["pending_run"] is None and data["agents"]["latest_run"]["run_no"] == 4
    today_runs = [r["run_no"] for r in data["agents"]["today_runs"]]
    assert today_runs == [4] * len(todays)
    # Jev's scoped logs: this cycle opens with run 4's selection and its 20 verdicts.
    this_cycle = data["agents"]["jev"]["cycle"]
    verdicts = [line for line in this_cycle["lines"] if line["kind"] == "SELECTION"]
    assert this_cycle["run_no"] == 4 and len(verdicts) == 21
    assert verdicts[0]["text"].startswith("Selected 10 of 20 (run 4)")
    assert [line["outcome"] for line in verdicts[1:]] == (
        ["SELECTED"] * 10 + ["PASSED"] * 6 + ["VETOED"] * 2 + ["NOT_RANKED"] * 2)
    lines = this_cycle["lines"]
    assert [line["at"] for line in lines] == sorted((line["at"] for line in lines), reverse=True)
    assert any(line["kind"] == "BUY" for line in lines)  # the cycle's entries
    assert data["agents"]["jev"]["today"]["day"] == data["today"]["day"]
    [card] = data["agents"]["research"]
    assert (card["agent"], card["runs"], card["picks"]) == (FIXTURE_AGENT_ID, 4, 20)
    assert len(card["picks_list"]) == 20 and card["picks_list"][0]["jev"] == "SELECTED"
    assert all(p["why"] and len(p["why"]) <= 160 for p in card["picks_list"])
    assert {d["kind"] for d in card["decisions"]} >= {"REVIEW_ANSWER", "EXIT_FLAG"}
    jev = data["agents"]["jev"]
    assert jev["health"] == "OK" and jev["calls_today"] >= 1
    assert jev["latest_selection"]["selected"] == 10
    feed = data["feed"]
    # The page shows the newest 20 lines (the cap itself: test_experiment_report). The demo has
    # 19 until its oldest open Jev-managed trade has had its 24-hour review, which adds two: that
    # trade enters the day before at 16:03 UTC, so from 14:00 to 16:03 UTC it has not (found
    # 2026-09-28 at 14:40 UTC).
    ledger, _, trades = demo
    reviewed = any(t["arm"] == "JEV_MANAGED" and t["outcome"] is None
                   and ledger.built_at - t["entry_at"] >= timedelta(days=1) for t in trades)
    assert len(feed) == (20 if reviewed else 19) and feed[0]["actor"] == "JEV"
    assert any(item["repeats"] > 1 for item in feed)  # a trade's holds are one line
    assert [item["at"] for item in feed] == sorted((item["at"] for item in feed), reverse=True)


def test_failed_reviews_fold_and_a_protective_decisions_zero_equity_is_unknown(er, demo):  # noqa: F811
    """The live page of 2026-09-28: a stop change recorded equity 0 ("Paper account $0.00"),
    and Jev's reviews failed minute after minute on two trades, interleaved."""
    ledger, _, trades = demo
    managed = [t for t in trades if t["outcome"] is None and t["arm"] == "JEV_MANAGED"]
    newest, other = managed[-1], managed[0]
    now = datetime.now(UTC)
    for seconds in (50, 40, 30, 20, 10):
        ledger.maintenance_failed(newest["setup_id"], at=now - timedelta(seconds=seconds))
    for seconds in (45, 35, 25):
        ledger.maintenance_failed(other["setup_id"], at=now - timedelta(seconds=seconds))
    ledger.protective_decision(newest["setup_id"])
    with client(er) as c:
        data = c.get("/api/public/experiment").json()
    assert (data["overall"]["equity_usd"], data["overall"]["equity_at"]) == (None, None)
    folded = [f"{newest['pick'].symbol}: Review failed (5 in a row)",
              f"{other['pick'].symbol}: Review failed (3 in a row)"]
    assert [item["text"] for item in data["feed"][:2]] == folded
    assert [item["text"] for item in data["agents"]["jev"]["decisions"][:2]] == folded
    assert "REVIEW_UNAVAILABLE" not in json.dumps(data)  # the failure code stays private
    shown = {t["symbol"]: t for t in data["live_trades"]}[newest["pick"].symbol]
    assert (shown["jev_last"]["text"], shown["jev_last"]["outcome"]) == ("Review failed",
                                                                         "FAILED")
    assert shown["jev_last_change"]["text"].startswith("Raised stop to breakeven")


def test_no_private_field_reaches_any_response(er, demo):  # noqa: F811
    with client(er) as c:
        bodies = [c.get("/").text]
        bodies += [json.dumps(c.get(path).json()) for path in (
            "/api/public/experiment", "/health",
            *(f"/api/public/experiment/{s}" for s in experiment_page.SECTIONS))]
    for body in bodies:
        assert not UUID.search(body), UUID.search(body).group(0)
        assert not HEX64.search(body)
        for word in PRIVATE_WORDS:
            assert word not in body, word


def test_the_polled_json_is_compressed_for_readers_that_accept_it(er, demo):  # noqa: F811
    with client(er) as c:
        packed = c.get("/api/public/experiment", headers={"Accept-Encoding": "gzip"})
        plain = c.get("/api/public/experiment", headers={"Accept-Encoding": "identity"})
    assert packed.headers["content-encoding"] == "gzip"
    assert "content-encoding" not in plain.headers
    assert packed.headers["content-security-policy"] == CSP
    assert packed.json()["live_trades"] == plain.json()["live_trades"]


def test_every_section_has_its_own_endpoint(er, demo):  # noqa: F811
    with client(er) as c:
        whole = c.get("/api/public/experiment").json()
        for section in experiment_page.SECTIONS:
            part = c.get(f"/api/public/experiment/{section}").json()
            assert part[section] == whole[section], section
            assert part["as_of"] == whole["as_of"]
        assert c.get("/api/public/experiment/replay").status_code == 404


def test_a_failed_rebuild_serves_the_last_build_marked_stale(er, demo, monkeypatch):  # noqa: F811
    clock = [0.0]
    app = create_experiment_app(public_url(er.database_url), environ={},
                                monotonic=lambda: clock[0])
    with TestClient(app) as c:
        assert "stale" not in c.get("/api/public/experiment").json()
        clock[0] += 60

        def down(url):
            raise experiment_page.psycopg.OperationalError("fixture outage")

        monkeypatch.setattr(experiment_page, "connect", down)
        stale = c.get("/api/public/experiment").json()
        assert stale["stale"] is True and stale["overall"]["closed"] == 8
        clock[0] += 3600
        assert c.get("/api/public/experiment").status_code == 503


def test_health_reports_a_read_only_paper_surface(er):  # noqa: F811
    with client(er) as c:
        health = c.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "surface": "PUBLIC_EXPERIMENT_READ_ONLY",
                             "role": "catalyst_public", "paper_only": True,
                             "trading_enabled": False,
                             "dashboard_version": "EXPERIMENT_DASHBOARD_V3",
                             "fixture_data": False}
    assert health.headers["cache-control"] == "no-store"
