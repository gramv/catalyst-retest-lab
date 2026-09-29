"""Operator-enrolled ENGINEERING_TEST managed crypto setup (plan 0.10, owner ruling R6 B).

The owner enrolls one paper-only crypto setup from this CLI over the separate
``catalyst_operator`` login. The database function ``lab.operator_enroll_managed_engineering``
(migration 017) writes one audited ``MANAGED_ENGINEERING_ENROLLED`` event and one
``RESEARCH_SELECTED``-shaped packet under selection policy
``MANAGED_ENGINEERING_ENROLLMENT_V1`` with purpose ``ENGINEERING_TEST``. No Jev request,
receipt or judgment exists for it and no Jev role ever receives it: selection is replaced by
the audited enrollment, and the position monitor never reviews the setup.

Everything after enrollment is the normal managed path: the running app admits the packet
(classification, the active-symbol attempt, the configured account-risk policy and the
randomized arm, the broker price grid), the acknowledged stream trigger, the exact one-use
five-second risk authorization for every broker mutation, protection and mechanical exits.
Reporting marks the setup ``engineering`` and keeps it out of every performance aggregate;
account risk and reconciliation include it. There is no HTTP route for any of this.

    python -m catalyst_lab.managed_engineering enroll --config PATH --symbol BTC/USD \\
        --entry-trigger 100 --max-entry 100.10 --stop 95 --target 111 \\
        --expires-minutes 60 --reason "First supervised managed trade (plan 0.10)"
    python -m catalyst_lab.managed_engineering status --config PATH

The operator connection is ``--database-url``, else ``OPERATOR_DATABASE_URL``, else the
configuration's ``MANAGED_DATABASE_URL`` with its user replaced by ``catalyst_operator``
(local trust authentication over the ledger's private socket). Output never contains a
connection string or credential.
"""

import argparse
import json
import os
import re
from decimal import Decimal
from uuid import uuid4

ENGINEERING_SELECTION_POLICY = "MANAGED_ENGINEERING_ENROLLMENT_V1"
ENGINEERING_PURPOSE = "ENGINEERING_TEST"
ENROLLED_KIND = "MANAGED_ENGINEERING_ENROLLED"
OPERATOR_ROLE = "catalyst_operator"
# Engineering run limit for the supervised session, not a strategy rule: the packet, and so
# the WATCHING window, lasts at most four hours; an open position keeps its own exits.
MAX_EXPIRES_MINUTES = 240
REASON_MINIMUM, REASON_MAXIMUM = 10, 2000
LEVEL_OPTIONS = (
    ("entry_trigger", "entry_trigger"),
    ("max_entry_price", "max_entry"),
    ("stop", "stop"),
    ("target", "target"),
)
CRYPTO_SYMBOL = re.compile(r"[A-Z0-9]{1,16}/USD")
TEST_SIGNAL = re.compile(r"TEST-[A-Z0-9][A-Z0-9-]{2,59}")
PRICE = re.compile(r"[0-9]+(?:\.[0-9]+)?")
REFUSAL_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
ENROLL_FUNCTION = (
    "lab.operator_enroll_managed_engineering(text,text,numeric,numeric,numeric,numeric,"
    "integer,text)"
)


def is_engineering(record_json):
    """True for a setup admitted from an operator ENGINEERING_TEST enrollment (plan 0.10).

    The purpose is written only by the operator enrollment function; migration 017 ties it
    to the enrollment policy and to a setup without a Jev receipt.
    """
    return isinstance(record_json, dict) and record_json.get("purpose") == ENGINEERING_PURPOSE


def enrollment_request(*, symbol, entry_trigger, max_entry, stop, target, expires_minutes,
                       reason, signal_id=None):
    """The database function's input rules, checked before connecting.

    Levels are exact decimal strings. The database repeats every rule under the shared lock
    and is authoritative; the recorded-grid check exists only there.
    """
    if not isinstance(symbol, str) or not CRYPTO_SYMBOL.fullmatch(symbol):
        raise ValueError("ENGINEERING_ENROLLMENT_CRYPTO_ONLY")
    supplied = {"entry_trigger": entry_trigger, "max_entry": max_entry, "stop": stop,
                "target": target}
    levels = {}
    for name, option in LEVEL_OPTIONS:
        value = supplied[option]
        if not isinstance(value, str) or not PRICE.fullmatch(value):
            raise ValueError("ENGINEERING_LEVELS_INVALID")
        levels[name] = Decimal(value)
    t, m, s, p = (levels[name] for name, _ in LEVEL_OPTIONS)
    if not 0 < s < t <= m < p:
        raise ValueError("ENGINEERING_LEVELS_INVALID")
    if p - m < 2 * (m - s):
        raise ValueError("MIN_REWARD_RISK")  # Admission's rule: reward/risk 2 at max entry.
    if type(expires_minutes) is not int or not 1 <= expires_minutes <= MAX_EXPIRES_MINUTES:
        raise ValueError("ENGINEERING_EXPIRY_INVALID")
    text = reason.strip() if isinstance(reason, str) else ""
    if not text:
        raise ValueError("OPERATOR_REASON_REQUIRED")
    if len(text) < REASON_MINIMUM:
        raise ValueError("OPERATOR_REASON_TOO_SHORT")
    if len(text) > REASON_MAXIMUM or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise ValueError("OPERATOR_REASON_INVALID")
    from catalyst_lab.audit import credential_findings

    if credential_findings(text):
        # The ledger is append-only: a pasted secret could never be removed from it.
        raise ValueError("OPERATOR_REASON_CREDENTIAL_SHAPED")
    signal = signal_id if signal_id is not None else "TEST-MANAGED-" + uuid4().hex[:12].upper()
    if not isinstance(signal, str) or not TEST_SIGNAL.fullmatch(signal):
        raise ValueError("ENGINEERING_SIGNAL_ID_INVALID")
    return {"signal_id": signal, "symbol": symbol, "levels": levels,
            "expires_minutes": expires_minutes, "reason": text}


def operator_database_url(config, override=None):
    """The operator login for this configuration's ledger; never printed or stored."""
    if override:
        return override
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    base = config["environment"].get("MANAGED_DATABASE_URL", "")
    if not base or base.startswith("REQUIRED"):
        raise RuntimeError("OPERATOR_DATABASE_URL_REQUIRED")
    try:
        params = conninfo_to_dict(base)
    except Exception:
        raise RuntimeError("OPERATOR_DATABASE_URL_REQUIRED") from None
    params.pop("password", None)  # The operator role never borrows the risk role's secret.
    params["user"] = OPERATOR_ROLE
    return make_conninfo(**params)


def _require_schema(conn):
    present = conn.execute(
        "SELECT to_regprocedure(%s) IS NOT NULL AS present", (ENROLL_FUNCTION,)
    ).fetchone()["present"]
    if not present:
        raise RuntimeError("ENGINEERING_ENROLLMENT_REQUIRES_SCHEMA_17")


def enroll(repository, request):
    """One audited enrollment and its packet, in one operator transaction."""
    from catalyst_lab.repository import json_safe

    repository.check_role()
    levels = request["levels"]
    with repository.connect() as conn:
        _require_schema(conn)
        result = conn.execute(
            """SELECT lab.operator_enroll_managed_engineering(%s,%s,%s,%s,%s,%s,%s,%s)
            AS result""",
            (request["signal_id"], request["symbol"], levels["entry_trigger"],
             levels["max_entry_price"], levels["stop"], levels["target"],
             request["expires_minutes"], request["reason"]),
        ).fetchone()["result"]
    return json_safe({"action": "enroll", "mode": "PAPER_ONLY", **result})


def status(repository):
    """Every enrollment with its packet, setup state and whether it is still active."""
    from catalyst_lab.repository import json_safe

    repository.check_role()
    with repository.connect() as conn:
        _require_schema(conn)
        rows = conn.execute("SELECT * FROM lab.operator_managed_engineering_status()").fetchall()
    return json_safe({"action": "status", "mode": "PAPER_ONLY", "enrollments": rows,
                      "active_count": sum(bool(row["active"]) for row in rows)})


def _refusal(exc):
    """This schema's own refusal code, or None; DSNs and driver text are never shown."""
    import psycopg

    if isinstance(exc, psycopg.errors.RaiseException):
        code = getattr(exc.diag, "message_primary", None)
    elif isinstance(exc, psycopg.errors.InsufficientPrivilege):
        code = "OPERATOR_PRIVILEGE_REQUIRED"
    elif isinstance(exc, psycopg.OperationalError):
        code = "OPERATOR_DATABASE_UNAVAILABLE"
    elif isinstance(exc, (RuntimeError, ValueError)) and not isinstance(exc, psycopg.Error):
        code = str(exc)
    else:
        code = None
    return code if code and REFUSAL_CODE.fullmatch(code) else None


def main(argv=None):
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--config", required=True, help="private launch configuration (0600)")
    shared.add_argument("--database-url", help="catalyst_operator DSN; else OPERATOR_DATABASE_URL")
    parser = argparse.ArgumentParser(
        prog="python -m catalyst_lab.managed_engineering",
        description="Operator enrollment of one ENGINEERING_TEST managed crypto setup "
        "(plan 0.10). Paper only; every execution gate stays in force.",
    )
    actions = parser.add_subparsers(dest="action", required=True)
    enrolling = actions.add_parser("enroll", parents=[shared])
    enrolling.add_argument("--symbol", required=True, help="crypto pair, e.g. BTC/USD")
    for option in ("--entry-trigger", "--max-entry", "--stop", "--target"):
        enrolling.add_argument(option, required=True, help="exact decimal price")
    enrolling.add_argument("--expires-minutes", required=True, type=int)
    enrolling.add_argument("--reason", required=True)
    enrolling.add_argument("--signal-id", help="TEST- signal; generated when omitted")
    actions.add_parser("status", parents=[shared])
    args = parser.parse_args(argv)
    try:
        from catalyst_lab.managed_ops import load_private_config, operator_repository

        config = load_private_config(args.config)
        request = None
        if args.action == "enroll":
            request = enrollment_request(
                symbol=args.symbol, entry_trigger=args.entry_trigger, max_entry=args.max_entry,
                stop=args.stop, target=args.target, expires_minutes=args.expires_minutes,
                reason=args.reason, signal_id=args.signal_id,
            )
        repository = operator_repository(operator_database_url(
            config, args.database_url or os.environ.get("OPERATOR_DATABASE_URL")
        ))
        result = enroll(repository, request) if request else status(repository)
    except Exception as exc:
        code = _refusal(exc)
        if code:
            raise SystemExit("ENGINEERING_ENROLLMENT_REFUSED: " + code) from None
        raise SystemExit("ENGINEERING_ENROLLMENT_FAILED") from None
    print(json.dumps(result, sort_keys=True))
    return result


if __name__ == "__main__":
    main()
