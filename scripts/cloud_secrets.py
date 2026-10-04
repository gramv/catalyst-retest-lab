"""Generate the cloud's tokens and role passwords straight into Railway (owner-run, package cloud).

    python scripts/cloud_secrets.py --agent-token-file /abs/private/muse-agent-token [--dry-run]
    python scripts/cloud_secrets.py --agent-token-file ... --rotate RISK_DATABASE_PASSWORD

Run it from the repository root after ``railway login`` and ``railway link`` (and after the
first ``railway config apply`` created the services). Each value is generated here with
``secrets.token_urlsafe`` and handed to ``railway variable set NAME --service S --skip-deploys
--stdin`` on its standard input: it is never printed, logged, written to a file, put in an
argv or read from anywhere else. The only exception is a research agent's token: the first
deployment's ``MANAGED_AGENT_TOKENS_JSON`` is ``{"muse": <token>}`` (the identity screens match
a report's agent to its credential's own ID, so Muse's ID is exactly ``muse``), and another
agent is added below. Each agent's token is also written to its owner-chosen file
(``--agent-token-file``, ``--token-file``: absolute, outside the repository, outside ~/Documents
and iCloud Drive, mode 0600), which the owner hands to that agent without opening it or pasting
it into chat; this script reads such files only to rebuild the variable.
``MANAGED_API_TOKEN`` is generated like every other token and never issued to anyone: it is the
legacy identity, kept out of every hand-off.

Idempotent: a secret whose variables all exist already is skipped. A secret that exists on
only some of its services (or whose agent file is missing) is refused until you pass
``--rotate NAME``, which generates a new value for every place that secret lives. Nothing is
changed unless the whole plan is valid. The three external secrets (the Alpaca Paper key ID
and secret, the TypeSafe key) are never generated or touched here; enter them yourself as
sealed variables.

After rotating a ``*_DATABASE_PASSWORD``, the role's verifier in PostgreSQL must change too:
run the provisioner's ``rotate-passwords`` mode, then redeploy the services that use it
(docs/RAILWAY-DEPLOYMENT.md, "Rotating secrets").

Another research agent (package agent-api; owner direction of 2026-09-29 evening: one research
agent at a time, another agent allowed besides Muse), without replacing anyone's token:

    python scripts/cloud_secrets.py --add-agent dots-agent \\
        --token-file /abs/private/dots-agent-token \\
        --agent-token-file /abs/private/muse-agent-token [--keep-agent ID=/abs/file ...] \\
        [--dry-run]

writes ``MANAGED_AGENT_TOKENS_JSON`` as the union of every configured agent's LOCAL token file
(``--agent-token-file`` for ``--agent-id``, default ``muse``, and one ``--keep-agent`` per agent
added earlier) and the new agent's. The script cannot read the deployed value, so the owner
lists every agent that must keep working; the summary it prints first names the agent IDs, and
never a token. The new agent's token is generated only when ``--token-file`` does not exist
(then it is written mode 0600, before Railway is changed, so a rerun reuses it); an existing
file must be a private 0600 file and its token is reused. IDs follow ``agent_identity``
(``^[a-z][a-z0-9_-]{1,31}$``, at most 32 agents, each ID once); every token must be distinct.
Rotating ``MANAGED_AGENT_TOKENS_JSON`` keeps the agents named with ``--keep-agent`` the same way.
Then redeploy the trader, which reads the variable at startup.
"""

import argparse
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
# catalyst_lab.agent_identity's rules (AGENT_ID_PATTERN, MAX_AGENT_TOKENS, valid_token), kept
# here because the owner runs this script with a plain python3, outside the package's venv.
AGENT_ID = re.compile(r"[a-z][a-z0-9_-]{1,31}")
MAX_AGENTS = 32
MIN_TOKEN_CHARACTERS = 32
MAX_TOKEN_FILE_BYTES = 4096
AGENT_TOKENS = "MANAGED_AGENT_TOKENS_JSON"
AGENT_SERVICE = "trader"
NAME = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
TOKEN_BYTES = 48  # token_urlsafe(48): 64 URL-safe characters.


@dataclass(frozen=True)
class Secret:
    name: str
    targets: tuple  # (service, variable) pairs that hold the same value
    agent: bool = False


# Where every generated value lives (.railway/railway.ts declares each one with preserve()).
CATALOG = (
    Secret("RISK_DATABASE_PASSWORD", (("trader", "RISK_DATABASE_PASSWORD"),)),
    Secret("JEV_DATABASE_PASSWORD", (("trader", "JEV_DATABASE_PASSWORD"),)),
    Secret("APP_DATABASE_PASSWORD", (("ops", "APP_DATABASE_PASSWORD"),)),
    Secret("BACKUP_DATABASE_PASSWORD", (("ops", "BACKUP_DATABASE_PASSWORD"),)),
    Secret("OPERATOR_DATABASE_PASSWORD", (("ops", "OPERATOR_DATABASE_PASSWORD"),)),
    Secret("PUBLIC_DATABASE_PASSWORD", (("ops", "PUBLIC_DATABASE_PASSWORD"),)),
    Secret("MANAGED_API_TOKEN", (("trader", "MANAGED_API_TOKEN"),)),
    Secret("MANAGED_STATUS_TOKEN", (("trader", "MANAGED_STATUS_TOKEN"),
                                    ("ops", "MANAGED_STATUS_TOKEN"))),
    Secret("MANAGED_OPERATOR_TOKEN", (("trader", "MANAGED_OPERATOR_TOKEN"),)),
    Secret("MANAGED_AGENT_TOKENS_JSON", (("trader", "MANAGED_AGENT_TOKENS_JSON"),), agent=True),
)
SERVICES = tuple(sorted({service for secret in CATALOG for service, _ in secret.targets}))


class Refused(Exception):
    """A refusal code plus names; never a value."""


def forbidden_roots(home=None):
    home = Path(home or Path.home())
    return [REPOSITORY, home / "Documents", home / "Library" / "Mobile Documents"]


def agent_file_path(value, *, home=None):
    """Absolute, outside the repository, ~/Documents and iCloud Drive, in a private parent."""
    if not value or not os.path.isabs(value) or "\x00" in value:
        raise Refused("AGENT_TOKEN_FILE_MUST_BE_ABSOLUTE")
    path = Path(os.path.normpath(value))
    resolved = path.parent.resolve(strict=False) / path.name
    for root in forbidden_roots(home):
        root = root.resolve(strict=False)
        if resolved == root or resolved.is_relative_to(root):
            raise Refused("AGENT_TOKEN_FILE_LOCATION_FORBIDDEN")
    try:
        parent = os.lstat(resolved.parent)
    except OSError:
        raise Refused("AGENT_TOKEN_FILE_DIRECTORY_MISSING") from None
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) & 0o022):
        raise Refused("AGENT_TOKEN_FILE_DIRECTORY_NOT_PRIVATE")
    return resolved


def agent_file_state(path):
    """``"ABSENT"``, ``"PRIVATE"`` (a regular 0600 file of this user) or refused."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "ABSENT"
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise Refused("AGENT_TOKEN_FILE_NOT_PRIVATE")
    return "PRIVATE"


def write_agent_file(path, token, *, replace):
    """A new owner-only file, or an atomic replacement of the existing one."""
    data = (token + "\n").encode()
    if not replace:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return
    fd, temporary = tempfile.mkstemp(prefix=".agent-token-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def valid_token(value):
    """``agent_identity.valid_token``: at least 32 characters, no whitespace."""
    return (isinstance(value, str) and len(value) >= MIN_TOKEN_CHARACTERS
            and not any(c.isspace() for c in value))


def read_agent_token(path, agent_id):
    """The token of an agent's private local file (the file this script writes: the token and
    a newline). Refusals name the agent and a code, never the file's content."""
    if agent_file_state(path) != "PRIVATE":
        raise Refused(f"AGENT_TOKEN_FILE_MISSING {agent_id}")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise Refused(f"AGENT_TOKEN_FILE_UNREADABLE {agent_id}") from None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise Refused(f"AGENT_TOKEN_FILE_NOT_PRIVATE {agent_id}")
        data = stream.read(MAX_TOKEN_FILE_BYTES + 1)
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise Refused(f"AGENT_TOKEN_FILE_INVALID {agent_id}") from None
    token = text[:-1] if text.endswith("\n") else text
    if len(data) > MAX_TOKEN_FILE_BYTES or not valid_token(token):
        raise Refused(f"AGENT_TOKEN_FILE_INVALID {agent_id}")
    return token


def kept_agents(values, *, home=None):
    """``--keep-agent ID=/abs/file`` values: ``[(agent ID, validated path)]`` in order."""
    kept = []
    for value in values or ():
        agent_id, separator, path = value.partition("=")
        if not separator or not AGENT_ID.fullmatch(agent_id):
            raise Refused("KEEP_AGENT_INVALID (expected ID=/absolute/token-file)")
        kept.append((agent_id, agent_file_path(path, home=home)))
    return kept


def agent_union(agents, *, extra=None):
    """``{agent ID: token}`` of ``agents`` (``[(ID, path)]``) read from their local files, plus
    ``extra`` (``(ID, token)``): each ID once, at most MAX_AGENTS, every token distinct."""
    ids = [agent_id for agent_id, _ in agents] + ([extra[0]] if extra else [])
    duplicated = sorted({agent_id for agent_id in ids if ids.count(agent_id) > 1})
    if duplicated:
        raise Refused("AGENT_ID_DUPLICATE " + ",".join(duplicated))
    if len(ids) > MAX_AGENTS:
        raise Refused(f"AGENT_TOKENS_OVER_LIMIT {MAX_AGENTS}")
    paths = [path for _, path in agents]
    if len(set(paths)) != len(paths):
        raise Refused("AGENT_TOKEN_FILE_SHARED")
    tokens = {agent_id: read_agent_token(path, agent_id) for agent_id, path in agents}
    if extra:
        tokens[extra[0]] = extra[1]
    if len(set(tokens.values())) != len(tokens):
        raise Refused("AGENT_TOKENS_NOT_DISTINCT")
    return tokens


class RailwayCli:
    """The two documented commands this script needs; values travel only on stdin."""

    def __init__(self, executable, environment=None):
        self.executable, self.environment = executable, environment

    def _scope(self, service):
        scope = ["--service", service]
        return scope + (["--environment", self.environment] if self.environment else [])

    def names(self, service):
        """Variable names of one service. Values arrive in memory and are dropped here."""
        result = subprocess.run([self.executable, "variable", "list", *self._scope(service),
                                 "--json"], capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise Refused(f"RAILWAY_VARIABLE_LIST_FAILED {service}")
        try:
            data = json.loads(result.stdout)
        except ValueError:
            raise Refused(f"RAILWAY_VARIABLE_LIST_UNREADABLE {service}") from None
        if isinstance(data, dict) and isinstance(data.get("variables"), (dict, list)):
            data = data["variables"]
        if isinstance(data, dict):
            names = set(data)
        elif isinstance(data, list) and all(isinstance(item, dict) for item in data):
            names = {item.get("name") or item.get("key") for item in data}
        else:
            raise Refused(f"RAILWAY_VARIABLE_LIST_UNREADABLE {service}")
        return {name for name in names if isinstance(name, str)}

    def set(self, service, name, value):
        result = subprocess.run([self.executable, "variable", "set", name, *self._scope(service),
                                 "--skip-deploys", "--stdin"], input=value, capture_output=True,
                                text=True, timeout=120)
        if result.returncode:
            # The CLI's own words, with every line that could hold the value withheld: without
            # them a refusal cannot be diagnosed (first cloud run, 2026-09-28).
            raise Refused(f"RAILWAY_VARIABLE_SET_FAILED {service} {name} "
                          f"exit={result.returncode}" + cli_reason(result, value))


def cli_reason(result, value):
    """The CLI's output for a refused set, never the value: a line containing the value, or
    any 8-character piece of it, is withheld whole. At most 12 lines of 200 characters."""
    pieces = {value[i:i + 8] for i in range(max(1, len(value) - 7))} if value else set()
    lines = []
    for line in (result.stderr or "").splitlines() + (result.stdout or "").splitlines():
        line = line.strip()
        if not line or "Config as Code" in line or "config migrate" in line \
                or "keep working until" in line:
            continue
        if value and (value in line or any(piece in line for piece in pieces)):
            line = "<a line withheld: it may contain the value>"
        lines.append(line[:200])
    return (" | CLI: " + " / ".join(lines[:12])) if lines else ""


def plan(existing, agent_state, rotate):
    """``{name: "GENERATE"|"ROTATE"|"SKIP"}`` or a refusal naming the partial secrets."""
    unknown = sorted(set(rotate) - {secret.name for secret in CATALOG})
    if unknown:
        raise Refused("ROTATE_NAME_UNKNOWN " + ",".join(unknown))
    actions, partial = {}, []
    for secret in CATALOG:
        present = [(s, v) in existing for s, v in secret.targets]
        if secret.agent:
            present.append(agent_state == "PRIVATE")
        if secret.name in rotate:
            actions[secret.name] = "ROTATE"
        elif all(present):
            actions[secret.name] = "SKIP"
        elif not any(present):
            actions[secret.name] = "GENERATE"
        else:
            partial.append(secret.name)
    if partial:
        raise Refused("SECRET_PARTIALLY_SET " + ",".join(partial)
                      + " (pass --rotate NAME to replace it everywhere)")
    return actions


def railway_cli(args):
    executable = args.railway or shutil.which("railway")
    if not executable:
        raise Refused("RAILWAY_CLI_REQUIRED")
    return RailwayCli(executable, args.environment)


def run(args, *, cli=None, home=None, out=sys.stdout):
    if getattr(args, "add_agent", None) is not None:
        return add_agent(args, cli=cli, home=home, out=out)
    if getattr(args, "token_file", None) is not None:
        raise Refused("TOKEN_FILE_ONLY_WITH_ADD_AGENT")
    if not AGENT_ID.fullmatch(args.agent_id):
        raise Refused("AGENT_ID_INVALID")
    agent_path = agent_file_path(args.agent_token_file, home=home)
    agent_state = agent_file_state(agent_path)
    # Agents added earlier keep their tokens when the agent token is rotated (package agent-api):
    # read from their local files, checked before anything changes.
    kept = kept_agents(getattr(args, "keep_agent", None), home=home)
    kept_tokens = {}
    if kept:
        if AGENT_TOKENS not in set(args.rotate or ()):
            raise Refused("KEEP_AGENT_ONLY_WITH_ADD_AGENT_OR_ROTATE " + AGENT_TOKENS)
        if args.agent_id in {agent_id for agent_id, _ in kept}:
            raise Refused("AGENT_ID_DUPLICATE " + args.agent_id)
        if agent_path in {path for _, path in kept}:
            raise Refused("AGENT_TOKEN_FILE_SHARED")
        if len(kept) + 1 > MAX_AGENTS:
            raise Refused(f"AGENT_TOKENS_OVER_LIMIT {MAX_AGENTS}")
        kept_tokens = agent_union(kept)
    if cli is None:
        cli = railway_cli(args)
    existing = {(service, name) for service in SERVICES for name in cli.names(service)}
    actions = plan(existing, agent_state, set(args.rotate or ()))
    report = {"agent_token_file": str(agent_path), "dry_run": args.dry_run, "secrets": {}}
    for secret in CATALOG:
        action = actions[secret.name]
        report["secrets"][secret.name] = {
            "action": action, "services": [service for service, _ in secret.targets]}
        if secret.agent:
            report["secrets"][secret.name]["agents"] = sorted({args.agent_id, *kept_tokens})
        if action == "SKIP" or args.dry_run:
            continue
        token = secrets.token_urlsafe(TOKEN_BYTES)
        if secret.agent and token in kept_tokens.values():
            raise Refused("AGENT_TOKENS_NOT_DISTINCT")
        value = (json.dumps({**kept_tokens, args.agent_id: token}, sort_keys=True)
                 if secret.agent else token)
        for service, name in secret.targets:
            cli.set(service, name, value)
        if secret.agent:
            write_agent_file(agent_path, token, replace=agent_state == "PRIVATE")
        del token, value
    print(json.dumps(report, indent=2, sort_keys=True), file=out)
    return report


def add_agent(args, *, cli=None, home=None, out=sys.stdout):
    """``--add-agent``: ``MANAGED_AGENT_TOKENS_JSON`` becomes the listed agents' local tokens
    plus the new agent's, and nothing else changes. Everything is checked first; the summary
    (agent IDs, never a token) is printed before anything is applied."""
    new_id = args.add_agent
    if not AGENT_ID.fullmatch(new_id) or not AGENT_ID.fullmatch(args.agent_id):
        raise Refused("AGENT_ID_INVALID")
    if args.rotate:
        raise Refused("ADD_AGENT_WITH_ROTATE_REFUSED")
    if getattr(args, "token_file", None) is None:
        raise Refused("TOKEN_FILE_REQUIRED")
    kept = [(args.agent_id, agent_file_path(args.agent_token_file, home=home)),
            *kept_agents(getattr(args, "keep_agent", None), home=home)]
    ids = [agent_id for agent_id, _ in kept]
    if new_id in ids:
        raise Refused("AGENT_ALREADY_LISTED " + new_id)
    if len(ids) + 1 > MAX_AGENTS:
        raise Refused(f"AGENT_TOKENS_OVER_LIMIT {MAX_AGENTS}")
    new_path = agent_file_path(args.token_file, home=home)
    if new_path in {path for _, path in kept}:
        raise Refused("AGENT_TOKEN_FILE_SHARED")
    reuse = agent_file_state(new_path) == "PRIVATE"
    new_token = read_agent_token(new_path, new_id) if reuse else None
    tokens = agent_union(kept, extra=(new_id, new_token) if reuse else None)
    if cli is None:
        cli = railway_cli(args)
    if AGENT_TOKENS not in cli.names(AGENT_SERVICE):
        raise Refused(f"AGENT_TOKENS_VARIABLE_MISSING {AGENT_SERVICE} {AGENT_TOKENS} "
                      "(run the first deployment's secrets step first)")
    report = {"mode": "ADD_AGENT", "dry_run": args.dry_run, "applied": False,
              "service": AGENT_SERVICE, "variable": AGENT_TOKENS,
              "agents": sorted({*tokens, new_id}), "new_agent": new_id,
              "new_agent_token": "REUSE_LOCAL_FILE" if reuse else "GENERATE",
              "new_agent_token_file": str(new_path)}
    print(json.dumps(report, indent=2, sort_keys=True), file=out)
    if args.dry_run:
        return report
    if not reuse:
        new_token = secrets.token_urlsafe(TOKEN_BYTES)
        if new_token in tokens.values():
            raise Refused("AGENT_TOKENS_NOT_DISTINCT")
        # Written before Railway changes: a failed set leaves a file the rerun reuses.
        write_agent_file(new_path, new_token, replace=False)
    value = json.dumps({**tokens, new_id: new_token}, sort_keys=True)
    cli.set(AGENT_SERVICE, AGENT_TOKENS, value)
    del value, new_token, tokens
    report = {**report, "applied": True}
    print(json.dumps(report, indent=2, sort_keys=True), file=out)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent-token-file", required=True,
                        help="absolute path for Muse's agent token (outside the repo)")
    parser.add_argument("--agent-id", default="muse",
                        help="the agent ID of that token (default and cloud value: muse)")
    parser.add_argument("--rotate", action="append", metavar="NAME",
                        help="generate a new value for this secret everywhere it lives")
    parser.add_argument("--add-agent", metavar="ID",
                        help="add this research agent to MANAGED_AGENT_TOKENS_JSON, keeping "
                             "--agent-id and every --keep-agent (package agent-api)")
    parser.add_argument("--token-file", metavar="PATH",
                        help="with --add-agent: the new agent's absolute token file "
                             "(generated only when it does not exist)")
    parser.add_argument("--keep-agent", action="append", metavar="ID=PATH",
                        help="another agent already in MANAGED_AGENT_TOKENS_JSON and its local "
                             "token file (with --add-agent or --rotate MANAGED_AGENT_TOKENS_JSON)")
    parser.add_argument("--environment", help="Railway environment (default: the linked one)")
    parser.add_argument("--railway", help="path to the railway CLI (default: on PATH)")
    parser.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except Refused as exc:
        raise SystemExit(f"CLOUD_SECRETS_REFUSED: {exc}") from None

if __name__ == "__main__":
    main()
