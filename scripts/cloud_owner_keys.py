"""Copy the owner's three external keys into Railway's trader, never showing them (owner-run).

    python3 scripts/cloud_owner_keys.py --dry-run
    python3 scripts/cloud_owner_keys.py
    python3 scripts/cloud_owner_keys.py --replace TYPESAFE_API_KEY

Run it from the repository root after ``railway login`` and ``railway link``. It reads the
Alpaca **Paper** key ID and secret and the TypeSafe key from where this project already keeps
them on the owner's Mac, and hands each value to ``railway variable set NAME --service trader
--skip-deploys --stdin`` on its standard input. No value is printed, logged, written to a file,
put in an argv or compared with anything but its own format checks. The output names only the
variables, the file each came from and the action taken.

Sources, first match wins, per key:

1. The private config v2 (``--config``, default
   ``~/.local/share/catalyst-retest-lab/private/private.json``), which
   ``scripts/set_private_secret.py`` maintains: its ``environment`` map holds
   ``APCA_API_KEY_ID`` and ``APCA_API_SECRET_KEY``, and ``environment.TYPESAFE_ENV_FILE`` names
   the file holding ``TYPESAFE_API_KEY=...``.
2. The repository's ``.env`` (``--env-file``), for any key still missing.

Refusals (nothing is sent unless every key is found and valid):
``KEY_NOT_FOUND``, ``KEY_IS_A_PLACEHOLDER``, ``KEY_FORMAT_INVALID`` (empty, whitespace,
non-printable or over 4096 characters) and ``ALPACA_KEY_NOT_PAPER`` (a Paper key ID starts
with ``PK``; the app refuses anything else too). A variable that already exists on the trader
is skipped unless ``--replace NAME``. Seal the three variables in the Railway dashboard
afterwards (each variable's three-dot menu, **Seal**): the CLI has no seal command.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cloud_secrets import cli_reason  # noqa: E402  (the same withheld-value CLI reasons)

SERVICE = "trader"
KEYS = ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "TYPESAFE_API_KEY")
DEFAULT_CONFIG = Path.home() / ".local/share/catalyst-retest-lab/private/private.json"
PLACEHOLDER = re.compile(r"(?i)^(REQUIRED|CHANGE[_-]?ME|YOUR[_-]|<).*")
DOTENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


class Refused(Exception):
    """A refusal code plus names; never a value."""


def dotenv_values(path):
    """``{NAME: value}`` of a KEY=VALUE file: comments and blank lines skipped, one level of
    matching quotes removed. Missing or unreadable files give ``{}``."""
    try:
        text = Path(path).read_text()
    except (OSError, UnicodeError):
        return {}
    values = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = DOTENV_LINE.match(line)
        if not match:
            continue
        value = match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[match.group(1)] = value
    return values


def config_values(path):
    """The keys the private config v2 holds, and the TypeSafe file it names."""
    try:
        config = json.loads(Path(path).read_text())
    except (OSError, UnicodeError, ValueError):
        return {}, None
    environment = config.get("environment") if isinstance(config, dict) else None
    if not isinstance(environment, dict):
        return {}, None
    found = {k: environment[k] for k in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY")
             if isinstance(environment.get(k), str)}
    typesafe_file = environment.get("TYPESAFE_ENV_FILE")
    if isinstance(typesafe_file, str) and typesafe_file:
        value = dotenv_values(typesafe_file).get("TYPESAFE_API_KEY")
        if value is not None:
            found["TYPESAFE_API_KEY"] = value
        return found, typesafe_file
    return found, None


def check(name, value):
    if value is None:
        raise Refused(f"KEY_NOT_FOUND {name}")
    if PLACEHOLDER.match(value):
        raise Refused(f"KEY_IS_A_PLACEHOLDER {name}")
    if (not value or len(value) > 4096 or not value.isascii() or not value.isprintable()
            or any(c.isspace() for c in value)):
        raise Refused(f"KEY_FORMAT_INVALID {name}")
    if name == "APCA_API_KEY_ID" and not value.startswith("PK"):
        raise Refused("ALPACA_KEY_NOT_PAPER APCA_API_KEY_ID")


def collect(config_path, env_path):
    """``{name: (value, source)}`` for the three keys, or a refusal; never prints a value."""
    from_config, typesafe_file = config_values(config_path)
    from_env = dotenv_values(env_path)
    collected = {}
    for name in KEYS:
        if name in from_config:
            source = typesafe_file if name == "TYPESAFE_API_KEY" else str(config_path)
            value = from_config[name]
        else:
            value, source = from_env.get(name), str(env_path)
        check(name, value)
        collected[name] = (value, source)
    return collected


def trader_names(executable):
    result = subprocess.run([executable, "variable", "list", "--service", SERVICE, "--json"],
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise Refused(f"RAILWAY_VARIABLE_LIST_FAILED {SERVICE}")
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise Refused(f"RAILWAY_VARIABLE_LIST_UNREADABLE {SERVICE}") from None
    if isinstance(data, dict) and isinstance(data.get("variables"), (dict, list)):
        data = data["variables"]
    if isinstance(data, dict):
        return set(data)
    if isinstance(data, list):
        return {item.get("name") or item.get("key") for item in data if isinstance(item, dict)}
    raise Refused(f"RAILWAY_VARIABLE_LIST_UNREADABLE {SERVICE}")


def set_variable(executable, name, value):
    result = subprocess.run([executable, "variable", "set", name, "--service", SERVICE,
                             "--skip-deploys", "--stdin"], input=value, capture_output=True,
                            text=True, timeout=120)
    if result.returncode:
        raise Refused(f"RAILWAY_VARIABLE_SET_FAILED {SERVICE} {name} exit={result.returncode}"
                      + cli_reason(result, value))


def run(args, out=sys.stdout):
    unknown = sorted(set(args.replace or ()) - set(KEYS))
    if unknown:
        raise Refused("REPLACE_NAME_UNKNOWN " + ",".join(unknown))
    collected = collect(Path(args.config).expanduser(), Path(args.env_file).expanduser())
    executable = args.railway or shutil.which("railway")
    if not executable:
        raise Refused("RAILWAY_CLI_REQUIRED")
    existing = trader_names(executable)
    report = {"service": SERVICE, "dry_run": args.dry_run, "keys": {}}
    for name in KEYS:
        value, source = collected[name]
        action = ("SKIP" if name in existing and name not in (args.replace or ())
                  else "REPLACE" if name in existing else "SET")
        report["keys"][name] = {"source": source, "action": action}
        if action != "SKIP" and not args.dry_run:
            set_variable(executable, name, value)
        del value
    collected.clear()
    print(json.dumps(report, indent=2, sort_keys=True), file=out)
    if not args.dry_run:
        print("Next: seal APCA_API_KEY_ID, APCA_API_SECRET_KEY and TYPESAFE_API_KEY (and "
              "MANAGED_API_TOKEN) in the Railway dashboard: trader -> Variables -> ... -> Seal.",
              file=out)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="private config v2 (default: %(default)s)")
    parser.add_argument("--env-file", default=str(REPOSITORY / ".env"),
                        help="fallback KEY=VALUE file (default: the repository's .env)")
    parser.add_argument("--replace", action="append", metavar="NAME",
                        help="overwrite this key on the trader even if it is already set")
    parser.add_argument("--railway", help="path to the railway CLI (default: on PATH)")
    parser.add_argument("--dry-run", action="store_true",
                        help="find and check the keys, show sources and actions, send nothing")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except Refused as exc:
        raise SystemExit(f"CLOUD_OWNER_KEYS_REFUSED: {exc}") from None


if __name__ == "__main__":
    os.umask(0o077)
    main()
