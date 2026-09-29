"""Screenshots of the public live dashboard, from disposable fixture databases only.

    python -m scripts.experiment_page_screenshots [--out artifacts/experiment-page-2026-09-27]
    python -m scripts.experiment_page_screenshots --serve     # keep both pages up to inspect

Builds a private /tmp PostgreSQL cluster (never an owner ledger), migrates it, enables LOGIN for
catalyst_public exactly as the tests do, and serves two copies of the dashboard as
catalyst_public: an empty ledger and tests/experiment_fixtures.py's demo experiment. Both pass
``fixture_data=True``, so every screenshot carries the "FIXTURE DATA - not real results" banner.
Headless Chrome (DevTools protocol) waits for the page's own script to render, then captures
desktop 1440 px and mobile 390 px in light and dark, plus one desktop capture with the
collapsible lists opened. checks.json records sideways scrolling, clipped tables, console
errors, the number of scripts and the JSON polls the page made.
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
from tests.experiment_fixtures import (  # noqa: E402
    ExperimentLedger,
    build_demo_experiment,
    enable_public_login,
    public_url,
)

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
VIEWPORTS = {"desktop-1440": (1440, 1000, False), "mobile-390": (390, 844, True)}
SCHEMES = ("light", "dark")
MAX_BYTES = 1_000_000  # The repository caps tracked files at 1 MiB.


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


def databases(root):
    """The empty ledger (catalyst_lab) and a demo copy made from it before any row is added."""
    admin = localdb.connection_url(root, "lab_owner").replace("dbname=catalyst_lab",
                                                              "dbname=postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE catalyst_lab").format(
            sql.Identifier("experiment_demo")))
    empty = localdb.connection_url(root)
    demo = empty.replace("dbname=catalyst_lab", "dbname=experiment_demo")
    return empty, demo


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

    def capture(self, url, width, height, mobile, scheme, path, expand=False):
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
                  deviceScaleFactor=1, mobile=mobile)
        self.call("Emulation.setEmulatedMedia",
                  features=[{"name": "prefers-color-scheme", "value": scheme}])
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
        time.sleep(0.6)
        if expand:  # Open the collapsible lists (latest picks, recent closed trades).
            self.call("Runtime.evaluate", expression="document.querySelectorAll('details')"
                      ".forEach((d) => { d.open = true; })")
            time.sleep(0.4)
        checks = self.call("Runtime.evaluate", returnByValue=True, expression="""({
            scrollWidth: document.documentElement.scrollWidth,
            innerWidth: window.innerWidth,
            clippedTables: [...document.querySelectorAll('.table-wrap')]
                .filter((w) => w.scrollWidth > w.clientWidth + 1).length,
            executableScripts: [...document.scripts]
                .filter((s) => s.type !== 'application/json').length,
            polls: performance.getEntriesByType('resource')
                .filter((e) => e.name.endsWith('/api/public/experiment')).length,
            height: document.documentElement.scrollHeight,
            fixtureBanner: !!document.querySelector('.fixture-banner'),
            rendered: document.body.dataset.ready === '1',
            pill: (document.getElementById('pill') || {}).textContent,
            liveRows: document.querySelectorAll('#live-body tbody tr').length,
            feedItems: document.querySelectorAll('#feed-body li').length,
            agentCards: document.querySelectorAll('#agents-body .card').length,
            scripts: document.scripts.length,
            dark: matchMedia('(prefers-color-scheme: dark)').matches
        })""")["result"]["value"]
        full = self.call("Page.getLayoutMetrics")["cssContentSize"]
        clip = {"x": 0, "y": 0, "width": width, "height": full["height"], "scale": 1}
        for quality in (70, 60, 50):  # JPEG keeps the committed files small.
            data = self.call("Page.captureScreenshot", format="jpeg", quality=quality,
                             clip=clip, captureBeyondViewport=True)["data"]
            raw = base64.b64decode(data)
            if len(raw) <= MAX_BYTES:
                break
        path = path.with_suffix(".jpg")
        path.write_bytes(raw)
        return {**checks, "consoleErrors": len(self.errors), "file": path.name, "bytes": len(raw),
                "horizontal_overflow": checks["scrollWidth"] > checks["innerWidth"]
                or checks["clippedTables"] > 0}

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
    parser.add_argument("--out", default=str(ROOT / "artifacts" / "experiment-page-2026-09-27"))
    parser.add_argument("--serve", action="store_true", help="serve both pages until Ctrl-C")
    args = parser.parse_args(argv)
    out = Path(args.out)
    with tempfile.TemporaryDirectory(prefix="catalyst-shots-", dir="/tmp") as directory:
        root = Path(directory) / "cluster"
        localdb.start(root)  # A fresh private cluster; never an owner ledger.
        try:
            enable_public_login(root)
            empty_url, demo_url = databases(root)
            now = datetime.now(UTC)
            ledger = ExperimentLedger(demo_url)
            build_demo_experiment(ledger, now)
            apps = {
                "empty": create_experiment_app(public_url(empty_url), fixture_data=True,
                                               environ={}),
                "demo": create_experiment_app(public_url(demo_url), fixture_data=True,
                                              environ={}),
            }
            ports = {name: free_port() for name in apps}
            servers = [serve(app, ports[name]) for name, app in apps.items()]
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
            for old in out.glob("experiment-*-fixture-*"):
                old.unlink()
            profile = Path(directory) / "chrome"
            chrome = Chrome(profile)
            results = []
            try:
                for name, port in ports.items():
                    for viewport, (width, height, mobile) in VIEWPORTS.items():
                        for scheme in SCHEMES:
                            path = out / f"experiment-{name}-fixture-{viewport}-{scheme}.jpg"
                            result = chrome.capture(f"http://127.0.0.1:{port}/", width, height,
                                                    mobile, scheme, path)
                            results.append({"page": name, "viewport": viewport,
                                            "scheme": scheme, **result})
                            print(json.dumps(results[-1]), flush=True)
                path = out / "experiment-demo-fixture-desktop-1440-light-expanded.jpg"
                result = chrome.capture(f"http://127.0.0.1:{ports['demo']}/", 1440, 1000, False,
                                        "light", path, expand=True)
                results.append({"page": "demo", "viewport": "desktop-1440", "scheme": "light",
                                "expanded": True, **result})
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
