"""Screenshots of the public live page (EXPERIMENT_DASHBOARD_V3), from disposable fixture
databases only.

    python -m scripts.experiment_page_screenshots [--out artifacts/public-page-v3]
    python -m scripts.experiment_page_screenshots --serve     # keep the pages up to inspect
    python -m scripts.experiment_page_screenshots --like live.json --public-market

``--like FILE`` adds a third, realistic fixture ledger shaped like a saved public page document
(scripts/experiment_realistic.py); ``--public-market`` lets the realistic page's service read
Alpaca's keyless public crypto bars and quotes (read-only, no credential), so its candles, the
BTC benchmark and today's snapshots are real market prices. The demo ledger is
tests/experiment_fixtures.py's build_demo_v3 (V2's demo plus day starts, equity snapshots, a
soft limit and a stats exclusion).

Builds a private /tmp PostgreSQL cluster (never an owner ledger), migrates it, enables LOGIN for
catalyst_public exactly as the tests do, and serves two copies of the page as catalyst_public:
an empty ledger and tests/experiment_fixtures.py's V2 demo (build_demo_v2: the demo experiment
plus phase-A trades, entry waits, the day's baseline and market regimes). Both pass
``fixture_data=True``, so every screenshot carries the "FIXTURE DATA - not real results" banner.
Headless Chrome (DevTools protocol) waits for the page's own script to render, then captures
the live page and a trade's own page at desktop 1440 px and phone 390 px (PNG). checks.json
records sideways page scrolling, tables that scroll inside their own box, console errors, the
number of scripts and the JSON polls the page made.
"""

import argparse
import base64
import json
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg
import uvicorn
from psycopg import sql
from websockets.sync.client import connect as ws_connect

from catalyst_lab import localdb
from catalyst_lab.experiment_page import create_experiment_app

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.experiment_realistic import build_realistic  # noqa: E402
from tests.experiment_fixtures import (  # noqa: E402
    ExperimentLedger,
    build_demo_v3,
    enable_public_login,
    public_url,
)

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
VIEWPORTS = {"desktop-1440": (1440, 1000, False), "phone-390": (390, 844, True)}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(app, port):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           server_header=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("SERVER_DID_NOT_START")


def databases(root, names=("experiment_demo", "experiment_like")):
    """The empty ledger (catalyst_lab) and copies made from it before any row is added."""
    admin = localdb.connection_url(root, "lab_owner").replace("dbname=catalyst_lab",
                                                              "dbname=postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        for name in names:
            conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE catalyst_lab").format(
                sql.Identifier(name)))
    empty = localdb.connection_url(root)
    return empty, *(empty.replace("dbname=catalyst_lab", "dbname=" + n) for n in names)


class Chrome:
    def __init__(self, profile):
        self.process = subprocess.Popen([
            CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
            "--no-default-browser-check", "--disable-background-networking",
            "--disable-component-update", "--disable-sync", "--disable-extensions",
            "--no-pings", "--metrics-recording-only", f"--user-data-dir={profile}",
            "--remote-debugging-port=0", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file = Path(profile) / "DevToolsActivePort"
        for _ in range(200):
            if port_file.exists() and port_file.read_text().strip():
                break
            time.sleep(0.05)
        port = int(port_file.read_text().splitlines()[0])
        targets = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=10).json()
        page = next(t for t in targets if t["type"] == "page")
        self.ws = ws_connect(page["webSocketDebuggerUrl"], max_size=None)
        self.next_id = 0
        self.errors = []  # Script exceptions, console errors and CSP reports seen by the page.
        self.call("Runtime.enable")
        self.call("Log.enable")

    def call(self, method, **params):
        self.next_id += 1
        self.ws.send(json.dumps({"id": self.next_id, "method": method, "params": params}))
        while True:
            message = json.loads(self.ws.recv())
            method = message.get("method")
            params = message.get("params", {})
            if method == "Runtime.exceptionThrown" or (
                    method == "Runtime.consoleAPICalled" and params.get("type") == "error") or (
                    method == "Log.entryAdded" and params["entry"].get("level") == "error"):
                self.errors.append(method)
            if message.get("id") == self.next_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})

    def hover(self, selector, fraction):
        """Move the pointer over a chart (at ``fraction`` of its width) to show its tooltip."""
        self.call("Runtime.evaluate", expression=f"""(() => {{
            const hit = document.querySelector({json.dumps(selector)});
            if (!hit) return false;
            const box = hit.getBoundingClientRect();
            hit.scrollIntoView({{block: 'center'}});
            const b = hit.getBoundingClientRect();
            hit.dispatchEvent(new PointerEvent('pointermove', {{bubbles: true,
                clientX: b.left + b.width * {fraction}, clientY: b.top + b.height / 2}}));
            hit.dispatchEvent(new PointerEvent('pointerenter', {{bubbles: false,
                clientX: b.left + b.width * {fraction}, clientY: b.top + b.height / 2}}));
            return box.width > 0;
        }})()""")
        time.sleep(0.3)
        shot = self.call("Page.captureScreenshot", format="png")["data"]
        return base64.b64decode(shot)

    def capture(self, url, width, height, mobile, path):
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
                  deviceScaleFactor=1 if not mobile else 2, mobile=mobile)
        self.call("Page.enable")
        self.errors.clear()
        self.call("Page.navigate", url=url)
        for _ in range(200):  # The page's own script sets data-ready after its first render.
            state = self.call("Runtime.evaluate", expression="document.body && "
                              "document.body.dataset.ready === '1' && "
                              "document.readyState === 'complete'")
            if state["result"].get("value") is True:
                break
            time.sleep(0.05)
        for _ in range(200):  # A trade page reads its price bars after the first render.
            state = self.call("Runtime.evaluate", expression="!document.body.textContent"
                              ".includes('Loading the price bars')")
            if state["result"].get("value") is True:
                break
            time.sleep(0.05)
        time.sleep(1.2)
        checks = self.call("Runtime.evaluate", returnByValue=True, expression="""({
            scrollWidth: document.documentElement.scrollWidth,
            innerWidth: window.innerWidth,
            scrollingTables: [...document.querySelectorAll('.table-wrap')]
                .filter((w) => w.scrollWidth > w.clientWidth + 1).length,
            executableScripts: [...document.scripts]
                .filter((s) => s.type !== 'application/json').length,
            polls: performance.getEntriesByType('resource')
                .filter((e) => e.name.endsWith('/api/public/experiment')).length,
            height: document.documentElement.scrollHeight,
            fixtureBanner: !!document.querySelector('.fixture-banner'),
            rendered: document.body.dataset.ready === '1',
            status: (document.querySelector('.status-label') || {}).textContent,
            openRows: document.querySelectorAll('#open-body tbody tr').length,
            closedRows: document.querySelectorAll('#closed-body tbody tr').length,
            timeline: document.querySelectorAll('.timeline li').length,
            charts: document.querySelectorAll('svg.chart-svg').length,
        })""")["result"]["value"]
        full = self.call("Page.getLayoutMetrics")["cssContentSize"]
        clip = {"x": 0, "y": 0, "width": width, "height": full["height"], "scale": 1}
        data = self.call("Page.captureScreenshot", format="png", clip=clip,
                         captureBeyondViewport=True)["data"]
        raw = base64.b64decode(data)
        path.write_bytes(raw)
        return {**checks, "consoleErrors": len(self.errors), "file": path.name, "bytes": len(raw),
                "horizontal_overflow": checks["scrollWidth"] > checks["innerWidth"]}

    def close(self):
        try:
            self.ws.close()
        finally:
            self.process.terminate()
            self.process.wait(timeout=10)


def _stop(signum, frame):
    raise KeyboardInterrupt  # SIGTERM still stops the private cluster and removes its files.


def main(argv=None):
    signal.signal(signal.SIGTERM, _stop)
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "artifacts" / "public-page-v3"))
    parser.add_argument("--serve", action="store_true", help="serve the pages until Ctrl-C")
    parser.add_argument("--like", help="a saved public page JSON to shape a realistic ledger on")
    parser.add_argument("--public-market", action="store_true",
                        help="read Alpaca's keyless public crypto data for the realistic page")
    args = parser.parse_args(argv)
    out = Path(args.out)
    with tempfile.TemporaryDirectory(prefix="catalyst-shots-", dir="/tmp") as directory:
        root = Path(directory) / "cluster"
        localdb.start(root)  # A fresh private cluster; never an owner ledger.
        try:
            enable_public_login(root)
            empty_url, demo_url, like_url = databases(root)
            now = datetime.now(UTC)
            ledger = ExperimentLedger(demo_url)
            build_demo_v3(ledger, now)
            market = None
            if args.public_market:
                from catalyst_lab.public_market import PublicMarketData

                market = PublicMarketData()
            apps = {
                "empty": create_experiment_app(public_url(empty_url), fixture_data=True,
                                               environ={}),
                "demo": create_experiment_app(public_url(demo_url), fixture_data=True,
                                              environ={}),
            }
            if args.like:
                build_realistic(ExperimentLedger(like_url),
                                json.loads(Path(args.like).read_text()), market, now)
                apps["like"] = create_experiment_app(public_url(like_url), fixture_data=True,
                                                     environ={}, market=market)
            ports = {name: free_port() for name in apps}
            servers = [serve(app, ports[name]) for name, app in apps.items()]
            pages = {}
            for name in ("demo", "like") if args.like else ("demo",):
                document = httpx.get(f"http://127.0.0.1:{ports[name]}/api/public/experiment",
                                     timeout=60).json()
                closed = document["past"]["closed_trades"]
                planned = next((t for t in closed if t["plan"]), closed[0])
                if name == "like":  # A stop-out with a day of price after it.
                    planned = next((t for t in closed if t["exit_reason"] and
                                    "Stop" in t["exit_reason"]), planned)
                held = next((t for t in document["live_trades"] if t["plan"]),
                            document["live_trades"][0])
                pages[name] = {"main": "/", "trade": f"/trade/{planned['trade_no']}",
                               "open-trade": f"/trade/{held['trade_no']}"}
            if args.serve:
                for name, port in ports.items():
                    print(f"{name}: http://127.0.0.1:{port}/", flush=True)
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    pass
                return
            out.mkdir(parents=True, exist_ok=True)
            for old in out.glob("*.png"):
                old.unlink()
            profile = Path(directory) / "chrome"
            chrome = Chrome(profile)
            results = []
            try:
                shots = [(name, page, viewport) for name in pages for page in pages[name]
                         for viewport in VIEWPORTS]
                shots.append(("empty", "main", "desktop-1440"))
                for name, page, viewport in shots:
                    width, height, mobile = VIEWPORTS[viewport]
                    path = out / f"{name}-{page}-{viewport}.png"
                    route = pages.get(name, {"main": "/"})[page]
                    result = chrome.capture(f"http://127.0.0.1:{ports[name]}{route}",
                                            width, height, mobile, path)
                    results.append({"ledger": name, "page": page, "viewport": viewport,
                                    **result})
                    hovers = {"main": [(".chart-equity .hit", 0.62, "equity"),
                                       (".chart-daily .hit:nth-last-of-type(2)", 0.5, "daily")],
                              "trade": [(".chart-candles .hit", 0.4, "candles")],
                              "open-trade": [(".chart-candles .hit", 0.7, "candles")]}
                    if viewport == "desktop-1440" and name != "empty":
                        for selector, fraction, label in hovers.get(page, []):
                            (out / f"{name}-{page}-hover-{label}.png").write_bytes(
                                chrome.hover(selector, fraction))
                    print(json.dumps(results[-1]), flush=True)
            finally:
                chrome.close()
                for server in servers:
                    server.should_exit = True
            (out / "checks.json").write_text(json.dumps({
                "captured_at": now.isoformat(timespec="seconds"),
                "database": "disposable /tmp cluster, removed after the run",
                "fixture_data_flag": True, "results": results,
            }, indent=2) + "\n")
        finally:
            localdb.stop(root)


if __name__ == "__main__":
    main()
