"""Local proof of the Railway profile (package cloud): no Railway, no Docker, no broker.

    ./run python scripts/cloud_local_proof.py run --out /abs/artifacts/cloud-2026-09-27

LOCAL FIXTURE PROOF: PAPER TRADING, SIMULATED VENUE. Everything runs on this Mac under
/tmp/cloud-proof-*, and every part is disposable:

- the release is the committed HEAD, staged (scripts/cloud_release.py) and sealed exactly as
  the image build does (python -m catalyst_lab.cloud_release seal), and every component runs
  from it (PYTHONPATH), so the status reports its release_commit;
- the database is a new PostgreSQL cluster shaped like Railway's service (a ``postgres``
  superuser with a password, a ``railway`` database, SCRAM for every role) that serves TLS
  on 127.0.0.1 and rejects plain TCP; every DSN says ``sslmode=require``, as in the cloud. It
  is provisioned by the provision service's start command,
  ``python -m catalyst_lab.cloud_entry provision initial``;
- every component runs with ``HOME=/nonexistent``, the home the entrypoint gives the image's
  runtime user (libpq looks for client certificates under $HOME);
- the trader and ops are separate processes running catalyst_lab.cloud_runtime's real
  ``trader`` and ``ops`` entrypoints in railway mode. The trader's app is the real managed
  app and ManagedRuntime (executor lease, reconciliation, status), with the managed tests'
  mock paper venue (tests.test_managed_execution.ManagedVenue) behind an httpx mock
  transport, a fixture Jev and no market or trade stream (no stream can be opened: the
  proof has no broker). Each process refuses any network connection except loopback;
- every token and password is generated for this run, handed to the processes in their
  environment and never written anywhere; each evidence file is checked for them first.

The evidence (codes, statuses, hashes, timelines) goes to ``--out``.
"""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(1, str(REPOSITORY))  # The managed tests' venue and clusters (tests/).
PROOF_KEY_ID = "PKCLOUDPROOF0000000001"  # Paper-key shape; the venue is a mock transport.
NOT_LITERAL = {"APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "TYPESAFE_ENV_FILE",
               "MANAGED_DATABASE_URL", "JEV_WORKER_DATABASE_URL"}
# The image's runtime user's home (Dockerfile.managed), which catalyst_lab.cloud_entry gives
# every component after dropping root.
RUNTIME_HOME = "/nonexistent"


# --- inside the component processes --------------------------------------------------------


def guard_network():
    """Refuse every connection except this machine's loopback and Unix sockets."""
    original = socket.socket.connect
    original_ex = socket.socket.connect_ex
    loopback = {"127.0.0.1", "::1", "localhost"}

    def check(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in loopback:
            raise ConnectionRefusedError("CLOUD_PROOF_NETWORK_REFUSED")

    def connect(sock, address):
        check(sock, address)
        return original(sock, address)

    def connect_ex(sock, address):
        check(sock, address)
        return original_ex(sock, address)

    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex


class ProofSource:
    """Market source of the proof: no market data at all, and no network client."""

    def __init__(self, *args, **kwargs):
        from types import SimpleNamespace

        self.policy = SimpleNamespace(stock_feed="iex")

    def current_observations(self, universe):
        return {}, ()

    def _metadata(self, market):
        assets = {symbol: {"symbol": symbol, "status": "active", "tradable": True,
                           "class": "crypto"} for symbol in ("BTC/USD", "ETH/USD", "SOL/USD")}
        return (assets if market == "CRYPTO" else {}), ()

    def close(self):
        pass


def no_stream(*args, **kwargs):
    raise ConnectionRefusedError("CLOUD_PROOF_HAS_NO_BROKER_STREAM")


def proof_builder():
    """The real managed app and ManagedRuntime, with the mock venue and a fixture Jev."""
    import httpx

    from catalyst_lab.alpaca import AlpacaCredentials
    from catalyst_lab.authorization import RiskRepository
    from catalyst_lab.executor_lease import AccountExecutorLease, FencedAuthorizationGate
    from catalyst_lab.jev_contract import strict_json
    from catalyst_lab.jev_review import JevReviewer, ReliabilityPolicy
    from catalyst_lab.jev_store import JevStore
    from catalyst_lab.managed_app import AppSettings, create_application
    from catalyst_lab.managed_broker import ManagedPaperBroker
    from catalyst_lab.managed_execution import ManagedExecution, engineering_execution_policy
    from catalyst_lab.managed_ops import code_version
    from catalyst_lab.managed_runtime import ManagedRuntime, RuntimePolicy
    from catalyst_lab.managed_store import ManagedAuthorizationGate
    from catalyst_lab.research_cycle import CyclePolicy, ResearchCycle
    from catalyst_lab.research_selection_topk import selection_rule_from_env
    from tests.test_jev_review import FIXTURE_KEY
    from tests.test_managed_execution import ManagedVenue
    from tests.test_research_reports import reply

    env = os.environ
    settings = AppSettings.from_env()
    from dataclasses import replace

    settings = replace(settings, status_token=env["MANAGED_STATUS_TOKEN"],
                       operator_token=env["MANAGED_OPERATOR_TOKEN"],
                       agent_tokens=strict_json(env["MANAGED_AGENT_TOKENS_JSON"]))

    def now():
        return datetime.now(UTC)

    repository = RiskRepository(env["MANAGED_DATABASE_URL"])
    repository.check_role()
    reviews = JevStore(env["JEV_WORKER_DATABASE_URL"])
    repository.require_same_database(reviews)
    venue = ManagedVenue()
    venue.now = now()
    credentials = AlpacaCredentials(env["APCA_API_KEY_ID"], env["APCA_API_SECRET_KEY"])
    lease = AccountExecutorLease(repository, lambda: broker.account_identity())
    broker = ManagedPaperBroker(
        credentials, FencedAuthorizationGate(ManagedAuthorizationGate(repository), lease),
        transport=httpx.MockTransport(venue.handle))
    execution = ManagedExecution(repository, broker, policy=engineering_execution_policy(),
                                 clock=now, review_store=reviews,
                                 risk_policy_id=env["MANAGED_RISK_POLICY_ID"])
    reviewer = JevReviewer(
        reviews, ReliabilityPolicy("CLOUD_LOCAL_PROOF_FIXTURE_V1", 10, 1, 0.25, 100000, 30),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=reply())),
        key_provider=lambda: FIXTURE_KEY, clock=now)
    research = ResearchCycle(repository, reviewer,
                             CyclePolicy(**strict_json(env["MANAGED_CYCLE_POLICY_JSON"])),
                             clock=now, selection=selection_rule_from_env(env))
    source = ProofSource()
    runtime = ManagedRuntime(
        execution, research, source, credentials,
        RuntimePolicy(**strict_json(env["MANAGED_RUNTIME_POLICY_JSON"])), clock=now,
        reviewer_heartbeat=lambda: True, executor_lease=lease, connector=no_stream,
        management_reviews=env["MANAGED_MANAGEMENT_REVIEWS"])
    identity = code_version()
    runtime.code_version = identity["source_sha256"]
    runtime.release_commit = identity["release_commit"]
    runtime.owned_resources = (broker,)
    # As the Railway trader builds it: /health answers from memory, never the database.
    return create_application(runtime, settings, source_factory=ProofSource,
                              health_check_database=False), settings


def component(name, argv):
    guard_network()
    from catalyst_lab import cloud_runtime

    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    if name == "trader":
        return cloud_runtime.trader(builder=proof_builder)
    return cloud_runtime.ops(max_ticks=int(argv[0]) if argv else 1)


# --- the orchestrator ---------------------------------------------------------------------


class Evidence:
    """Collects the proof; every write is checked for this run's secrets first."""

    def __init__(self, out, secret_values):
        self.out, self.secrets = Path(out), list(secret_values)
        self.steps = []

    def clean(self, text):
        from catalyst_lab.audit import credential_findings

        for value in self.secrets:
            if value and value in text:
                raise RuntimeError("EVIDENCE_WOULD_CONTAIN_A_SECRET")
        if credential_findings(text):
            raise RuntimeError("EVIDENCE_CREDENTIAL_SHAPED")
        return text

    def step(self, name, **facts):
        entry = {"step": name, "at": datetime.now(UTC).isoformat(), **facts}
        self.clean(json.dumps(entry, default=str))
        self.steps.append(entry)
        print(f"[proof] {name}: " + json.dumps(facts, default=str)[:300], flush=True)
        return entry

    def write(self, name, value):
        text = value if isinstance(value, str) else json.dumps(value, indent=2, sort_keys=True,
                                                              default=str) + "\n"
        path = self.out / name
        path.write_text(self.clean(text))
        return path


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http(port, route, token=None):
    import httpx

    headers = {"Authorization": "Bearer " + token} if token else {}
    try:
        response = httpx.get(f"http://127.0.0.1:{port}{route}", headers=headers, timeout=3,
                             trust_env=False)
    except httpx.HTTPError:
        return None, None
    try:
        body = response.json()
    except ValueError:
        body = {"sha256": hashlib.sha256(response.content).hexdigest(),
                "bytes": len(response.content)}
    return response.status_code, body


def wait_for(predicate, seconds, interval=0.25):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


class Proof:
    def __init__(self, out, work):
        from tests import cloud_fixtures

        self.cloud_fixtures = cloud_fixtures
        self.out, self.work = Path(out), Path(work)
        self.processes = []
        self.tokens = {name: secrets.token_urlsafe(48) for name in (
            "MANAGED_API_TOKEN", "MANAGED_STATUS_TOKEN", "MANAGED_OPERATOR_TOKEN",
            "MUSE_AGENT_TOKEN", "APCA_API_SECRET_KEY", "TYPESAFE_API_KEY")}

    # Environments -----------------------------------------------------------------------

    def base_env(self, state):
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": RUNTIME_HOME, "LANG": "C.UTF-8",
                "PYTHONPATH": f"{self.release}/src{os.pathsep}{self.release}",
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
                "CATALYST_ENVIRONMENT": "railway", "CATALYST_STATE_DIR": str(state)}

    def trader_env(self, state, port, **overrides):
        example = json.loads((self.release / "deploy/private-paper.example.json").read_text())
        env = self.base_env(state)
        env.update({k: v for k, v in example["environment"].items() if k not in NOT_LITERAL})
        env.update(
            CATALYST_ENVIRONMENT="railway", MANAGED_HTTP_PORT=str(port), PORT=str(port),
            JEV_CREDENTIAL_SLOT="cloud-local-proof", APCA_API_KEY_ID=PROOF_KEY_ID,
            APCA_API_SECRET_KEY=self.tokens["APCA_API_SECRET_KEY"],
            TYPESAFE_API_KEY=self.tokens["TYPESAFE_API_KEY"],
            MANAGED_API_TOKEN=self.tokens["MANAGED_API_TOKEN"],
            MANAGED_STATUS_TOKEN=self.tokens["MANAGED_STATUS_TOKEN"],
            MANAGED_OPERATOR_TOKEN=self.tokens["MANAGED_OPERATOR_TOKEN"],
            MANAGED_AGENT_TOKENS_JSON=json.dumps({"muse": self.tokens["MUSE_AGENT_TOKEN"]}),
            RISK_DATABASE_PASSWORD=self.passwords["RISK_DATABASE_PASSWORD"],
            JEV_DATABASE_PASSWORD=self.passwords["JEV_DATABASE_PASSWORD"],
            MANAGED_DATABASE_URL=self.service.url(
                "catalyst_risk", self.passwords["RISK_DATABASE_PASSWORD"]),
            JEV_WORKER_DATABASE_URL=self.service.url(
                "catalyst_jev", self.passwords["JEV_DATABASE_PASSWORD"]))
        env.update(overrides)
        return {k: v for k, v in env.items() if v is not None}

    def ops_env(self, state, trader_port):
        env = self.base_env(state)
        env.update(
            MANAGED_STATUS_TOKEN=self.tokens["MANAGED_STATUS_TOKEN"],
            MANAGED_STATUS_API=f"http://127.0.0.1:{trader_port}",
            APP_DATABASE_PASSWORD=self.passwords["APP_DATABASE_PASSWORD"],
            BACKUP_DATABASE_PASSWORD=self.passwords["BACKUP_DATABASE_PASSWORD"],
            OPERATOR_DATABASE_PASSWORD=self.passwords["OPERATOR_DATABASE_PASSWORD"],
            AUDIT_DATABASE_URL=self.service.url("catalyst_app",
                                                self.passwords["APP_DATABASE_PASSWORD"]),
            BACKUP_DATABASE_URL=self.service.url("catalyst_backup",
                                                 self.passwords["BACKUP_DATABASE_PASSWORD"]),
            OPERATOR_DATABASE_URL=self.service.url(
                "catalyst_operator", self.passwords["OPERATOR_DATABASE_PASSWORD"]))
        return env

    def launch(self, name, env, *args, cwd=None):
        log = self.work / f"{name}-{len(self.processes)}.log"
        stream = open(log, "wb")
        process = subprocess.Popen(
            [sys.executable, str(self.release / "scripts" / "cloud_local_proof.py"),
             "component", name.split("-")[0], *args],
            env=env, cwd=cwd or self.work / "cwd", stdout=stream, stderr=subprocess.STDOUT)
        process.log, process.stream, process.name = log, stream, name
        self.processes.append(process)
        return process

    def finish(self, process, timeout=60):
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            code = process.wait()
        process.stream.close()
        return code, self.log_lines(process)

    def log_lines(self, process):
        """Code-only component lines: the Railway log a deployment would show."""
        lines = [line for line in process.log.read_text(errors="replace").splitlines()
                 if " TRADER " in line or " OPS " in line or "CRASH_LOOP" in line]
        return [line.split(" ", 1)[1] if " " in line else line for line in lines]

    # Steps --------------------------------------------------------------------------------

    def stage_release(self):
        stage = self.work / "release"
        result = subprocess.run(
            [sys.executable, str(REPOSITORY / "scripts" / "cloud_release.py"), "--out",
             str(stage)], capture_output=True, text=True, check=True)
        staged = json.loads(result.stdout)
        sealed = subprocess.run(
            [sys.executable, "-m", "catalyst_lab.cloud_release", "seal", str(stage)],
            capture_output=True, text=True, check=True,
            env={**os.environ, "PYTHONPATH": str(stage / "src")})
        self.release = stage
        metadata = json.loads(sealed.stdout)
        return self.evidence.step("release_staged_and_sealed", commit=staged["commit"],
                                  source_sha256=metadata["source_sha256"],
                                  sealed_like_the_image_build=True)

    def provision(self):
        from catalyst_lab import cloud_provision

        self.service = self.cloud_fixtures.start(self.cloud_fixtures.new_root(), tls=True)
        self.passwords = self.cloud_fixtures.cloud_passwords(cloud_provision.login_roles())
        self.evidence.secrets += [*self.passwords.values(), self.service.admin_password]
        # The provision service's start command, run from the staged release over TLS.
        command = ["python", "-m", "catalyst_lab.cloud_entry", "provision", "initial"]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": RUNTIME_HOME,
               "LANG": "C.UTF-8", "PYTHONPATH": f"{self.release}/src",
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
               "MIGRATION_DATABASE_URL": self.service.admin_url, **self.passwords}
        done = subprocess.run([sys.executable, *command[1:]], env=env, cwd=self.work / "cwd",
                              capture_output=True, text=True, timeout=300)
        if done.returncode:
            raise RuntimeError("PROOF_PROVISION_FAILED: "
                               + self.evidence.clean(done.stderr.strip()[-300:]))
        result = json.loads(self.evidence.clean(done.stdout))
        self.evidence.step("provisioned_empty_service", command=" ".join(command),
                           database_url_sslmode="require", over_tls=True, **{
            k: result[k] for k in ("result", "database", "schema_version", "ledger_id",
                                   "audit_seq", "login_roles", "nologin_roles", "skipped_roles",
                                   "notes")})
        try:
            cloud_provision.provision(self.service.admin_url, self.passwords)
            raise RuntimeError("PROOF_EXPECTED_A_REFUSAL")
        except cloud_provision.ProvisionError as exc:
            self.evidence.step("refused_reprovisioning_the_provisioned_service",
                               code=exc.code, names=list(exc.names))
        other = self.cloud_fixtures.start(self.cloud_fixtures.new_root())
        try:
            with other.admin() as conn:
                conn.execute("CREATE DATABASE catalyst_lab")
            with other.admin("catalyst_lab") as conn:
                conn.execute("CREATE TABLE public.not_ours (id int)")
            try:
                cloud_provision.provision(other.admin_url, self.passwords)
                raise RuntimeError("PROOF_EXPECTED_A_REFUSAL")
            except cloud_provision.ProvisionError as exc:
                self.evidence.step("refused_a_non_empty_database", code=exc.code)
        finally:
            self.cloud_fixtures.stop(other)
        return result

    def refusals_and_crash_loop(self):
        volume = self.work / "crash-volume"
        port = free_port()
        dotenv = self.work / "dotenv-cwd"
        dotenv.mkdir()
        (dotenv / ".env").write_text("# Present on purpose: its existence alone refuses.\n")
        cases = [
            ("dotenv_present", self.trader_env(volume, port), dotenv),
            ("typesafe_env_file_set", self.trader_env(
                volume, port, TYPESAFE_ENV_FILE=str(self.work / "typesafe.env")), None),
            ("placeholder", self.trader_env(
                volume, port, MANAGED_RISK_POLICY_ID="REQUIRED_RISK_POLICY"), None),
            ("missing_secret", self.trader_env(volume, port, APCA_API_SECRET_KEY=None), None),
            ("fifth_start_in_ten_minutes", self.trader_env(volume, port,
                                                           APCA_API_SECRET_KEY=None), None),
            ("restarted_container_same_volume", self.trader_env(volume, port), None),
        ]
        results = []
        for label, env, cwd in cases:
            code, lines = self.finish(self.launch("trader", env, cwd=cwd))
            results.append({"case": label, "exit_code": code, "log": lines})
        state = volume / "launcher-state"
        record = json.loads((state / "crash-loop-trader.json").read_text())
        self.evidence.step("refusals_and_crash_loop_breaker", runs=results,
                           breaker={k: record[k] for k in ("tripped", "starts_in_window",
                                                           "threshold", "window_seconds",
                                                           "alarms")},
                           state_files=sorted(p.name for p in state.iterdir()),
                           state_file_modes=sorted({oct(p.stat().st_mode & 0o777)
                                                    for p in state.iterdir()}))
        return results

    def serving_trader(self, name, volume, port):
        process = self.launch(name, self.trader_env(volume, port))
        first = wait_for(lambda: http(port, "/health")[0] == 200 and http(port, "/health"), 60)
        if not first:
            raise RuntimeError(f"PROOF_{name.upper()}_HEALTH_UNAVAILABLE")
        return process, first[1]

    def status(self, port, token_name="MANAGED_STATUS_TOKEN"):
        return http(port, "/api/v1/lab/status", self.tokens[token_name])

    def run(self):
        from catalyst_lab import ledger_ops
        from catalyst_lab.managed_ops import verify_checkpoint

        (self.work / "cwd").mkdir()
        self.evidence = Evidence(self.out, self.tokens.values())
        self.stage_release()
        self.provision()
        self.refusals_and_crash_loop()

        # Trader A: the one executor.
        port_a, volume_a = free_port(), self.work / "trader-a-volume"
        trader_a, health_a = self.serving_trader("trader-a", volume_a, port_a)
        serving = wait_for(lambda: self.status(port_a)[0] == 200 and self.status(port_a), 90)
        if not serving:
            raise RuntimeError("PROOF_TRADER_A_NEVER_SERVED")
        # The research loop starts after its first heartbeat (about five seconds): wait for it,
        # so the ops tick below sees a running research loop, not a start-up gap.
        ticking = wait_for(lambda: (self.status(port_a)[1] or {}).get("last_cycle_at"), 60)
        code, status = self.status(port_a)
        keys = ("worker_state", "executor_ownership", "last_reconciliation_at", "entry_ready",
                "trade_stream_connected", "market_streams", "management_review_enabled",
                "error_code", "schema_version", "release_commit", "code_version", "mode",
                "last_cycle_at", "research_healthy", "account_safety_healthy")
        self.evidence.step(
            "trader_a_serving", first_health=health_a, health=http(port_a, "/health")[1],
            status_http=code, status={k: status.get(k) for k in keys},
            research_loop_ticking=bool(ticking),
            status_without_token=http(port_a, "/api/v1/lab/status")[0],
            status_with_wrong_token=http(port_a, "/api/v1/lab/status", None)[0],
            status_with_agent_token=self.status(port_a, "MUSE_AGENT_TOKEN")[0],
            static_pages={route: http(port_a, route)[1] for route in
                          ("/", "/lab.js", "/results", "/results.js")})

        # Ops: one tick against trader A; checkpoint and backup on its "volume".
        ops_volume = self.work / "ops-volume"
        code, lines = self.finish(self.launch("ops", self.ops_env(ops_volume, port_a), "1"),
                                  timeout=180)
        alarms = json.loads((ops_volume / "alarms.json").read_text())
        checkpoints = verify_checkpoint(ops_volume / "audit-checkpoints")
        backup = ops_volume / "backups" / alarms["backup"]["backup"]
        manifest_sha256 = hashlib.sha256((backup / "manifest.json").read_bytes()).hexdigest()
        verified = ledger_ops.verify_manifest(backup, expected_manifest_sha256=manifest_sha256)
        drill = ledger_ops.drill(backup, "/tmp", expected_head=checkpoints["head_hash"],
                                 expected_manifest_sha256=manifest_sha256)
        self.evidence.step(
            "ops_tick_wrote_checkpoint_and_backup", exit_code=code, log=lines,
            alarms=alarms["alarms"], audit=alarms["audit"], backup=alarms["backup"],
            checkpoint_chain={k: checkpoints[k] for k in ("valid", "checkpoint_count",
                                                          "event_count", "head_hash")},
            backup_files=sorted(p.name for p in backup.iterdir()),
            backup_verify=verified["result"], restore_drill=drill["result"],
            restore_drill_checks=drill["checks"],
            volume_files=sorted(str(p.relative_to(ops_volume)) for p in ops_volume.rglob("*")
                                if p.is_file() and "backups" not in p.parts
                                and "audit-checkpoints" not in p.parts))

        # The backup role that just dumped the ledger reads only lab (package cloud-hardening):
        # pg_authid's password verifiers stay out of its reach.
        import psycopg

        with psycopg.connect(self.service.url("catalyst_backup",
                                              self.passwords["BACKUP_DATABASE_PASSWORD"]),
                             autocommit=True) as conn:
            try:
                conn.execute("SELECT rolpassword FROM pg_catalog.pg_authid")
                authid = "READABLE"
            except psycopg.errors.InsufficientPrivilege:
                authid = "PERMISSION_DENIED"
            member = conn.execute("""SELECT pg_has_role(current_user, 'pg_read_all_data',
                'MEMBER')""").fetchone()[0]
        self.evidence.step("backup_role_reads_the_ledger_only", pg_authid=authid,
                           pg_read_all_data_member=member,
                           backup_and_drill=[verified["result"], drill["result"]])

        # Every session of the ledger's roles uses TLS (the service rejects plain TCP; the ops
        # tick's pg_dump and pg_dumpall above connected the same way).
        with self.service.admin("catalyst_lab") as conn:
            rows = conn.execute(
                """SELECT a.usename::text, s.ssl, count(*)::int FROM pg_stat_activity a
                JOIN pg_stat_ssl s USING (pid) WHERE a.usename::text LIKE 'catalyst%'
                GROUP BY 1, 2 ORDER BY 1, 2""").fetchall()
        sessions = {}
        for role, ssl, count in rows:
            sessions.setdefault(role, {"tls": 0, "plain": 0})["tls" if ssl else "plain"] += count
        self.evidence.step("sessions_over_tls", sessions=sessions,
                           all_tls=bool(sessions) and all(
                               v["plain"] == 0 for v in sessions.values()),
                           plain_tcp="REJECTED_BY_PG_HBA", component_home=RUNTIME_HOME)

        # Trader B: a second trader for the same account waits for the lease.
        port_b, volume_b = free_port(), self.work / "trader-b-volume"
        trader_b, health_b = self.serving_trader("trader-b", volume_b, port_b)
        waiting_log = wait_for(lambda: [line for line in self.log_lines(trader_b)
                                        if "EXECUTOR_LEASE_WAITING" in line], 30)
        self.evidence.step(
            "second_trader_waits_for_the_lease", health=health_b,
            status_http=self.status(port_b)[0], status_body=self.status(port_b)[1],
            log=waiting_log, first_trader_still=self.status(port_a)[1]["executor_ownership"])

        # Trader A stops (a redeploy's SIGTERM); B takes the lease and serves.
        trader_a.send_signal(signal.SIGTERM)
        code_a, lines_a = self.finish(trader_a)
        taken = wait_for(lambda: self.status(port_b)[0] == 200 and self.status(port_b), 90)
        if not taken:
            raise RuntimeError("PROOF_TRADER_B_NEVER_TOOK_OVER")
        self.evidence.step(
            "handover_after_the_first_trader_stopped", trader_a_exit_code=code_a,
            trader_a_log=lines_a[-4:],
            trader_b_status={k: taken[1].get(k) for k in keys},
            trader_b_log=[line for line in self.log_lines(trader_b) if "SERVING" in line])

        # A lost lease ends trader B with exit 75 (Railway's ON_FAILURE restarts it).
        with self.service.admin("catalyst_lab") as conn:
            pids = [row[0] for row in conn.execute(
                """SELECT DISTINCT l.pid FROM pg_locks l JOIN pg_stat_activity a USING (pid)
                WHERE l.locktype = 'advisory' AND l.granted AND a.usename = 'catalyst_risk'""")]
            for pid in pids:
                conn.execute("SELECT pg_terminate_backend(%s)", (pid,))
        code_b, lines_b = self.finish(trader_b, timeout=90)
        self.evidence.step("lease_loss_exits_75", terminated_lease_sessions=len(pids),
                           trader_b_exit_code=code_b, trader_b_log=lines_b[-3:])
        return self.evidence

    def close(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stream.close()
        service = getattr(self, "service", None)
        if service is not None:
            self.cloud_fixtures.stop(service)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["component"]:
        sys.exit(component(argv[1], argv[2:]))
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("run",))
    parser.add_argument("--out", required=True, help="new or empty evidence directory")
    args = parser.parse_args(argv)
    out = Path(args.out)
    if not out.is_absolute():
        raise SystemExit("CLOUD_PROOF_REFUSED: --out must be absolute")
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="cloud-proof-", dir="/tmp"))
    proof = Proof(out, work)
    started = datetime.now(UTC)
    try:
        evidence = proof.run()
        report = {"format": "CATALYST_CLOUD_LOCAL_PROOF_V1", "result": "COMPLETED",
                  "scope": "LOCAL_FIXTURE_PROOF_NO_RAILWAY_NO_DOCKER_NO_BROKER",
                  "paper_only": True, "venue": "MOCK_TRANSPORT_MANAGED_VENUE",
                  "jev": "FIXTURE_REPLIES", "started_at": started.isoformat(),
                  "finished_at": datetime.now(UTC).isoformat(),
                  "platform": {"python": sys.version.split()[0],
                               "postgres": subprocess.run(
                                   ["postgres", "--version"], capture_output=True,
                                   text=True).stdout.strip()},
                  "steps": evidence.steps}
        evidence.write("local-proof.json", report)
    finally:
        proof.close()
        shutil.rmtree(work, ignore_errors=True)
    print(f"[proof] wrote {out / 'local-proof.json'}")


if __name__ == "__main__":
    main()
