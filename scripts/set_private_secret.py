"""Inject one secret into the private paper configuration without echo, history or output.

    python scripts/set_private_secret.py --config PATH alpaca [--replace]
    python scripts/set_private_secret.py --config PATH typesafe [--replace]
    python scripts/set_private_secret.py --config PATH token {muse,status,operator} [--generate]
                                         [--replace]
    python scripts/set_private_secret.py --config PATH check

Values are typed at a no-echo prompt (never on the command line, so they reach no shell
history or process list) or generated in-process for role tokens, and they never appear in
the output, a log or an exception. Every write is an owner-only mode-0600 file inside an
owner-only directory, created exclusively; an existing secret is replaced only with
``--replace``, and then atomically. The Alpaca Paper keys go into the configuration's
``environment`` map (the file is rewritten atomically with every other key unchanged and
re-validated before it replaces the old one); the TypeSafe key goes into the file the
configuration names as ``TYPESAFE_ENV_FILE`` (``TYPESAFE_API_KEY=…``, the exact format
``jev_secrets`` reads, so the worker never falls back to a working-directory ``.env``); a role
token goes into that role's ``token_file``. ``check`` reports only whether each secret is
present and correctly protected: never a value, and never its length.

Nothing here contacts a broker or provider; a written credential is proved only by the later
supervised launch. After replacing the Muse or status token, restart the components that read
it. Rotating a leaked key means rotating it at the provider first; the ledger is append-only and
cannot be scrubbed.
"""

import argparse
import getpass
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from pathlib import Path

from catalyst_lab.alpaca import AlpacaCredentials
from catalyst_lab.managed_ops import CONFIG_VERSION, load_private_config, private_bytes, role_token

TOKEN_ROLES = ("muse", "status", "operator")
TYPESAFE_LINE = re.compile(r"TYPESAFE_API_KEY=([^\s]+)")
PLACEHOLDER = re.compile(r"REQUIRED.*")


class Refused(Exception):
    """A refusal code for the operator; never carries a value."""


def _typed(prompt, label):
    value = prompt(label)
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise Refused("SECRET_MUST_BE_NON_EMPTY_WITHOUT_WHITESPACE")
    if not value.isascii() or not value.isprintable() or len(value) > 4096:
        raise Refused("SECRET_MUST_BE_PRINTABLE_ASCII")
    return value


def _owner_directory(path):
    """The parent must be an owner-only directory; it is created with mode 0700 if absent."""
    directory = Path(path).parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise Refused("PRIVATE_OWNER_DIRECTORY_REQUIRED")
    return directory


def _write_private(path, data, *, replace):
    """Exclusive owner-only creation; with ``replace`` an atomic rename over the old file.

    Symlinks are never followed or created over. The temporary file lives in the same
    directory, so the rename cannot cross file systems or leave a partial file behind.
    """
    target = Path(path)
    directory = _owner_directory(target)
    try:
        current = target.lstat()
    except FileNotFoundError:
        current = None
    if current is not None:
        if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
            raise Refused("SECRET_TARGET_MUST_BE_A_REGULAR_FILE")
        if not replace:
            raise Refused("SECRET_ALREADY_PRESENT_USE_REPLACE")
    if not replace:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return
    fd, temporary = tempfile.mkstemp(prefix=".secret-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        parent = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _config(path):
    config = load_private_config(path)
    if config.get("config_version") != CONFIG_VERSION:
        raise Refused("PRIVATE_CONFIG_V2_REQUIRED")
    return config


def _configured(value):
    return isinstance(value, str) and bool(value) and not PLACEHOLDER.fullmatch(value)


def set_alpaca(config_path, prompt, *, replace):
    config = _config(config_path)
    env = config["environment"]
    present = any(_configured(env.get(k)) for k in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"))
    if present and not replace:
        raise Refused("SECRET_ALREADY_PRESENT_USE_REPLACE")
    key_id = _typed(prompt, "Alpaca Paper key ID (no echo): ")
    secret = _typed(prompt, "Alpaca Paper secret key (no echo): ")
    try:
        AlpacaCredentials(key_id, secret)  # The same allowlist the runtime applies.
    except ValueError:
        raise Refused("ALPACA_PAPER_KEY_SHAPE_REJECTED") from None
    updated = json.loads(private_bytes(config_path))  # The raw file, every other key kept.
    updated["environment"] = {**updated["environment"], "APCA_API_KEY_ID": key_id,
                              "APCA_API_SECRET_KEY": secret}
    data = (json.dumps(updated, indent=2, sort_keys=True) + "\n").encode()
    # Validate the new content as the loader will before it replaces the old file.
    directory = _owner_directory(config_path)
    fd, probe = tempfile.mkstemp(prefix=".config-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        _config(probe)
    except ValueError:
        raise Refused("UPDATED_CONFIG_FAILED_VALIDATION") from None
    finally:
        os.unlink(probe)
    _write_private(config_path, data, replace=True)
    return {"secret": "alpaca", "written": str(config_path), "replaced": present}


def _typesafe_path(config):
    value = config["environment"].get("TYPESAFE_ENV_FILE")
    if not _configured(value) or not Path(value).is_absolute():
        raise Refused("TYPESAFE_ENV_FILE_NOT_CONFIGURED")
    return Path(value)


def set_typesafe(config_path, prompt, *, replace):
    path = _typesafe_path(_config(config_path))
    key = _typed(prompt, "TypeSafe API key (no echo): ")
    if len(key) < 16:
        raise Refused("TYPESAFE_KEY_TOO_SHORT")
    existed = path.exists()
    _write_private(path, f"TYPESAFE_API_KEY={key}\n".encode(), replace=replace)
    return {"secret": "typesafe", "written": str(path), "replaced": existed}


def set_token(config_path, prompt, role, *, generate, replace):
    if role not in TOKEN_ROLES:
        raise Refused("UNKNOWN_TOKEN_ROLE")
    path = Path(_config(config_path)[role]["token_file"])
    if generate:
        token = secrets.token_urlsafe(48)
    else:
        token = _typed(prompt, f"{role} role token (no echo, at least 32 characters): ")
        if len(token) < 32:
            raise Refused("ROLE_TOKEN_TOO_SHORT")
    existed = path.exists()
    _write_private(path, (token + "\n").encode(), replace=replace)
    role_token(path)  # The launcher's own reader accepts what was written.
    return {"secret": f"{role}-token", "written": str(path), "replaced": existed,
            "generated": generate}


def _typesafe_status(path):
    try:
        raw = private_bytes(path, limit=65536).decode()
    except ValueError:
        return "ABSENT_OR_UNPROTECTED" if not Path(path).exists() else "UNPROTECTED"
    lines = [line.strip() for line in raw.splitlines() if line.strip()
             and not line.strip().startswith("#")]
    return "PRESENT" if len(lines) == 1 and TYPESAFE_LINE.fullmatch(lines[0]) else "INVALID"


def check(config_path):
    """Presence and protection only; no value or length is ever reported."""
    config = _config(config_path)
    env = config["environment"]
    report = {"alpaca": "PRESENT" if all(
        _configured(env.get(k)) for k in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY")
    ) else "ABSENT"}
    try:
        report["typesafe"] = _typesafe_status(_typesafe_path(config))
    except Refused as refusal:
        report["typesafe"] = str(refusal)
    for role in TOKEN_ROLES:
        try:
            role_token(config[role]["token_file"])
            report[f"{role}-token"] = "PRESENT"
        except ValueError:
            report[f"{role}-token"] = "ABSENT_OR_INVALID"
    return report


def _terminal_prompt(label):
    if not sys.stdin.isatty():
        raise Refused("INTERACTIVE_TERMINAL_REQUIRED")
    return getpass.getpass(label)


def main(argv=None, *, prompt=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=__doc__)
    parser.add_argument("--config", required=True, help="private configuration (v2) path")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("alpaca", "typesafe"):
        sub = commands.add_parser(name)
        sub.add_argument("--replace", action="store_true", help="replace an existing secret")
    token = commands.add_parser("token")
    token.add_argument("role", choices=TOKEN_ROLES)
    token.add_argument("--generate", action="store_true",
                       help="generate a 64-character token instead of prompting for one")
    token.add_argument("--replace", action="store_true", help="replace an existing token")
    commands.add_parser("check")
    args = parser.parse_args(argv)
    os.umask(0o077)
    prompt = prompt or _terminal_prompt
    config_path = Path(args.config)
    try:
        if not config_path.is_absolute():
            raise Refused("ABSOLUTE_CONFIG_PATH_REQUIRED")
        if args.command == "alpaca":
            result = set_alpaca(config_path, prompt, replace=args.replace)
        elif args.command == "typesafe":
            result = set_typesafe(config_path, prompt, replace=args.replace)
        elif args.command == "token":
            result = set_token(config_path, prompt, args.role, generate=args.generate,
                               replace=args.replace)
        else:
            result = check(config_path)
    except Refused as refusal:
        print(json.dumps({"refused": str(refusal)}))
        return 2
    except ValueError as error:  # Loader refusals carry codes only, never file content.
        print(json.dumps({"refused": str(error)}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
