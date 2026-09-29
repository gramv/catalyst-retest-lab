"""Read-only acceptance-evidence collector for one managed paper trade (plan 0.10, gate G3).

Builds a deterministic ``MANAGED_ACCEPTANCE_EVIDENCE_V1`` manifest from rows already read from
the append-only ledger, so the owner's first supervised managed trade is proved from the audit
trail instead of hand-collected. Every function below is pure (it only reads the row lists or
single rows the caller passes in); ``collect_acceptance_evidence`` is the one function that talks
to the database, and it only ever issues ``SELECT`` statements over a connection the caller
already opened. This module never opens a connection, never prints anything and never contacts a
broker or provider.

Role boundary (verified against the migrations, not assumed): ``catalyst_review`` can read every
``lab.managed_*`` table and view, ``lab.account_risk_policies``, ``lab.ledger_account_binding``
and the research/review tables, and from migration 019 also ``lab.execution_halts`` and
``lab.trade_events`` (so it computes the audit hash chain itself); it cannot read
``lab.reconciliation_runs`` or ``lab.execution_halt_records``. ``catalyst_reporting`` can read
only the frozen V1 ``strategy_*``/``public_*`` views and none of the managed tables. Whether a
table is readable is probed with ``has_table_privilege``: a section this role cannot read (for
example on a ledger before migration 019) is reported honestly as ``present: false`` with a
reason, never silently skipped or assumed clean, and every check that needs it fails closed. An
optional owner-exported ``lab.trade_events`` file (``catalyst-lab export``, run separately by the
owner under ``catalyst_app``), when supplied, is verified instead of the direct read — see
``verify_audit_export`` and ``docs/OPERATIONS-RUNBOOK.md``.

Determinism: nothing here reads the wall clock, generates a random id, or depends on dict/set
iteration order beyond what ``encoded()`` (sort_keys JSON) already normalizes. The same ledger
rows always produce byte-identical output.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from catalyst_lab.audit import credential_findings, verify_events
from catalyst_lab.jev_contract import digest, encoded
from catalyst_lab.repository import json_safe

MANIFEST_VERSION = "MANAGED_ACCEPTANCE_EVIDENCE_V1"

# Roles this tool refuses outright: each can mutate the ledger, or (catalyst_app/catalyst_risk)
# read every table including credentials-adjacent ones, or (lab_owner) bypass every grant.
FORBIDDEN_ROLES = frozenset({"catalyst_app", "catalyst_risk", "lab_owner"})
# The only roles this tool will run under. catalyst_review sees the managed evidence;
# catalyst_reporting sees only the frozen V1 baseline and is accepted but mostly unavailable
# here — see the module docstring.
ALLOWED_ROLES = frozenset({"catalyst_review", "catalyst_reporting"})

# lab.managed_risk_decisions CHECK: expires_at <= created_at + interval '5 seconds' whenever the
# decision is APPROVED. Re-verified here from the readable columns, not assumed from the CHECK.
RISK_DECISION_TTL_SECONDS = 5

PROTECTIVE_ACTIONS = frozenset({"PROTECT", "AMEND"})


class RoleRefused(PermissionError):
    """The connected database role may not run this read-only tool."""


class SetupNotFound(LookupError):
    pass


# ---------------------------------------------------------------------------
# Generic, pure helpers
# ---------------------------------------------------------------------------


def canonical_json(value) -> str:
    """The same deterministic encoding (sorted keys, compact separators) used elsewhere for
    hashing (``jev_contract.encoded``), over the JSON-safe form of ``value``."""
    return encoded(json_safe(value))


def rows_sha256(value) -> str:
    return digest(canonical_json(value))


def _event_seqs(rows) -> list:
    return sorted({row["event_seq"] for row in rows if row.get("event_seq") is not None})


def _present(rows, summary: dict) -> dict:
    return {
        "present": bool(rows),
        "event_seqs": _event_seqs(rows),
        "sha256": rows_sha256(rows) if rows else None,
        "summary": summary,
    }


def _absent(reason: str, *, event_seqs=()) -> dict:
    return {
        "present": False,
        "event_seqs": sorted(event_seqs),
        "sha256": None,
        "summary": {"reason": reason},
    }


def _dt(value):
    """A ``datetime`` from either a ``datetime`` (as psycopg returns) or an ISO string."""
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _seconds_between(later, earlier):
    later, earlier = _dt(later), _dt(earlier)
    if later is None or earlier is None:
        return None
    return (later - earlier).total_seconds()


def _decimal_str(value):
    return None if value is None else str(value)


def _sum_decimal(values):
    total = Decimal(0)
    for value in values:
        total += Decimal(str(value))
    return total


# ---------------------------------------------------------------------------
# Section builders — each is a pure function over already-fetched rows.
# ---------------------------------------------------------------------------


def stream_subscription_section(account_events, *, market: str, symbol: str, trigger_event_seq):
    """``account_events``: ``lab.managed_events`` rows with ``setup_id IS NULL`` and kind in
    ``RUNTIME_MARKET_CONNECTED``/``RUNTIME_STREAM_CONNECTED`` (managed_runtime.py). Absent
    entirely when the setup's lifecycle never ran under ``ManagedRuntime`` (for example a fixture
    that drives ``ManagedExecution`` directly) — that is reported, not guessed at.
    """
    market_code = "US" if market == "US_STOCKS" else market
    market_acks = sorted(
        (
            e
            for e in account_events
            if e["kind"] == "RUNTIME_MARKET_CONNECTED"
            and e["body"].get("market") == market_code
            and symbol in (e["body"].get("symbols") or [])
        ),
        key=lambda e: e["event_seq"],
    )
    stream_acks = sorted(
        (e for e in account_events if e["kind"] == "RUNTIME_STREAM_CONNECTED"),
        key=lambda e: e["event_seq"],
    )
    rows = sorted(market_acks + stream_acks, key=lambda e: e["event_seq"])
    if not rows:
        return _absent("NO_RUNTIME_SUBSCRIPTION_EVENT_OBSERVED_FOR_THIS_SETUP")
    earliest_market = market_acks[0] if market_acks else None
    earliest_stream = stream_acks[0] if stream_acks else None
    before_trigger = (
        trigger_event_seq is not None
        and earliest_market is not None
        and earliest_stream is not None
        and earliest_market["event_seq"] < trigger_event_seq
        and earliest_stream["event_seq"] < trigger_event_seq
    )
    summary = {
        "market": market_code,
        "symbol": symbol,
        "market_subscription_acknowledged": earliest_market is not None,
        "trade_updates_stream_acknowledged": earliest_stream is not None,
        "market_ack_event_seq": earliest_market["event_seq"] if earliest_market else None,
        "market_ack_at": _decimal_str(earliest_market["recorded_at"]) if earliest_market else None,
        "trade_stream_ack_event_seq": earliest_stream["event_seq"] if earliest_stream else None,
        "trade_stream_ack_at": (
            _decimal_str(earliest_stream["recorded_at"]) if earliest_stream else None
        ),
        "acknowledged_before_trigger": before_trigger,
    }
    return _present(rows, summary)


def trigger_print_section(setup_events):
    """``MARKET_PRINT``/``MARKET_PRINT_CONSUMED``/``TRIGGER_CONFIRMED`` rows for this setup.

    A confirmed trigger's observation carries the same ``trade_id``/``trade_at`` as the
    ``MARKET_PRINT`` that produced it (``managed_runtime._process_print`` merges the persisted
    print's trade fields with a fresh quote before calling ``observe_trigger``), so the two are
    matched on those fields. A trigger with no matching print is reported as such, not treated as
    unproven: quiet prints above the entry trigger are coalesced into ``MARKET_PRINT_SUMMARY``
    and a fixture that calls ``ManagedExecution.observe_trigger`` directly persists no print.
    """
    triggers = sorted(
        (e for e in setup_events if e["kind"] == "TRIGGER_CONFIRMED"), key=lambda e: e["event_seq"]
    )
    prints = [e for e in setup_events if e["kind"] == "MARKET_PRINT"]
    consumed = {
        e["body"]["print_event_seq"]: e
        for e in setup_events
        if e["kind"] == "MARKET_PRINT_CONSUMED"
    }
    if not triggers:
        return _absent("NO_TRIGGER_CONFIRMED_EVENT")
    matches = []
    for trigger in triggers:
        body = trigger["body"]
        match = next(
            (
                p
                for p in prints
                if p["body"].get("trade_id") == body.get("trade_id")
                and p["body"].get("trade_at") == body.get("trade_at")
            ),
            None,
        )
        matches.append((trigger, match))
    rows = list(triggers) + prints + list(consumed.values())
    last_trigger, last_match = matches[-1]
    summary = {
        "trigger_count": len(triggers),
        "trade_price": _decimal_str(last_trigger["body"].get("trade_price")),
        "trade_at": last_trigger["body"].get("trade_at"),
        "quote_at": last_trigger["body"].get("quote_at"),
        "bid": _decimal_str(last_trigger["body"].get("bid")),
        "ask": _decimal_str(last_trigger["body"].get("ask")),
        "trigger_event_seq": last_trigger["event_seq"],
        "trigger_recorded_at": _decimal_str(last_trigger["recorded_at"]),
        "matched_market_print_event_seq": match["event_seq"] if (match := last_match) else None,
        "matched_market_print": last_match is not None,
    }
    return _present(rows, summary)


def entry_eligibility_section(setup_events):
    rows = sorted(
        (e for e in setup_events if e["kind"] == "ENTRY_ELIGIBILITY"), key=lambda e: e["event_seq"]
    )
    if not rows:
        return _absent("NO_ENTRY_ELIGIBILITY_EVENT")
    last = rows[-1]["body"]
    summary = {
        "count": len(rows),
        "last_reason": last.get("reason"),
        "eligible": not last.get("reason"),
    }
    return _present(rows, summary)


def risk_check_section(setup_events):
    rows = sorted(
        (e for e in setup_events if e["kind"] == "RISK_CHECK"), key=lambda e: e["event_seq"]
    )
    if not rows:
        return _absent("NO_RISK_CHECK_EVENT")
    last = rows[-1]["body"]
    summary = {
        "count": len(rows),
        "equity": _decimal_str(last.get("equity")),
        "qty": _decimal_str(last.get("qty")),
    }
    return _present(rows, summary)


def risk_decision_section(decisions, *, trigger_event_seq):
    """``decisions``: ``lab.managed_risk_decisions`` rows for the setup, each carrying whether a
    matching ``lab.managed_claims`` row exists (``claimed``), from the collector's join.
    """
    if not decisions:
        return _absent("NO_RISK_DECISION_ROW")
    entry = next(
        (d for d in decisions if d["action"] == "ENTRY" and d["outcome"] == "APPROVED"), None
    )
    summary = {
        "decision_count": len(decisions),
        "actions": sorted({d["action"] for d in decisions}),
    }
    if entry is not None:
        ttl_seconds = _seconds_between(entry["expires_at"], entry["created_at"])
        summary.update(
            {
                "entry_decision_id": str(entry["decision_id"]),
                "entry_outcome": entry["outcome"],
                "entry_method": entry["method"],
                "entry_path": entry["path"],
                "entry_qty": _decimal_str(entry["payload"].get("qty")),
                "entry_equity": _decimal_str(entry["equity"]),
                "entry_created_at": _decimal_str(entry["created_at"]),
                "entry_expires_at": _decimal_str(entry["expires_at"]),
                "ttl_seconds": ttl_seconds,
                "ttl_within_5_seconds": (
                    ttl_seconds is not None and ttl_seconds <= RISK_DECISION_TTL_SECONDS
                ),
                "claimed": bool(entry["claimed"]),
                "trigger_to_decision_seconds": (
                    None
                    if trigger_event_seq is None
                    else entry["event_seq"] - trigger_event_seq
                ),
            }
        )
    else:
        summary["entry_decision_id"] = None
    return _present(decisions, summary)


def broker_ack_section(setup_events, *, entry_decision_created_at=None):
    rows = sorted(
        (e for e in setup_events if e["kind"] == "BROKER_ACK"), key=lambda e: e["event_seq"]
    )
    if not rows:
        return _absent("NO_BROKER_ACK_EVENT")
    by_action = {}
    for row in rows:
        by_action.setdefault(row["body"].get("action"), []).append(row)
    entry_ack = by_action.get("ENTRY", [None])[0]
    summary = {
        "count": len(rows),
        "actions": sorted(by_action),
        "entry_ack_event_seq": entry_ack["event_seq"] if entry_ack else None,
        "entry_ack_at": _decimal_str(entry_ack["recorded_at"]) if entry_ack else None,
        "decision_to_entry_ack_seconds": _seconds_between(
            entry_ack["recorded_at"] if entry_ack else None, entry_decision_created_at
        ),
    }
    return _present(rows, summary)


def fills_section(fills, setup_events):
    broker_events = [e for e in setup_events if e["kind"] == "BROKER_EVENT"]
    backfill = [e for e in setup_events if e["kind"] == "BROKER_REST_BACKFILL"]
    if not fills:
        return _absent("NO_FILL_RECORDED", event_seqs=_event_seqs(broker_events + backfill))
    buys = [f for f in fills if f["side"] == "buy"]
    sells = [f for f in fills if f["side"] == "sell"]
    buy_qty = _sum_decimal(f["qty"] for f in buys)
    sell_qty = _sum_decimal(f["qty"] for f in sells)
    first_fill, last_fill = fills[0], fills[-1]
    summary = {
        "fill_count": len(fills),
        "buy_qty": str(buy_qty),
        "sell_qty": str(sell_qty),
        "net_qty": str(buy_qty - sell_qty),
        "first_fill_at": _decimal_str(first_fill["filled_at"]),
        "first_fill_price": _decimal_str(first_fill["price"]),
        "last_fill_at": _decimal_str(last_fill["filled_at"]),
        "last_fill_side": last_fill["side"],
        "sources": sorted({f["source"] for f in fills}),
        "rest_backfilled_count": sum(1 for f in fills if "BACKFILL" in f["source"]),
        "broker_event_count": len(broker_events),
        "rest_backfill_event_count": len(backfill),
    }
    return _present(list(fills) + broker_events + backfill, summary)


def broker_position_section(setup_events):
    rows = sorted(
        (e for e in setup_events if e["kind"] == "BROKER_POSITION"), key=lambda e: e["event_seq"]
    )
    if not rows:
        return _absent("NO_BROKER_POSITION_EVENT")
    last = rows[-1]["body"]
    # Decimal arithmetic on a fully-closed crypto position can net to "0.0000" or "0E-8" rather
    # than the literal string "0" — compare numerically, never by string equality.
    try:
        flat = Decimal(str(last.get("qty"))) == 0
    except (ArithmeticError, TypeError, ValueError):
        flat = False
    summary = {
        "count": len(rows),
        "last_qty": _decimal_str(last.get("qty")),
        "last_occurred_at": last.get("occurred_at"),
        "flat": flat,
    }
    return _present(rows, summary)


def protection_section(setup_events, *, first_fill_event_seq):
    plans = [e for e in setup_events if e["kind"] == "PROTECTION_PLAN"]
    transitioning = [e for e in setup_events if e["kind"] == "PROTECTION_TRANSITIONING"]
    acks = sorted(
        (
            e
            for e in setup_events
            if e["kind"] == "BROKER_ACK" and e["body"].get("action") in PROTECTIVE_ACTIONS
        ),
        key=lambda e: e["event_seq"],
    )
    entry_ack = next(
        (
            e
            for e in setup_events
            if e["kind"] == "BROKER_ACK"
            and e["body"].get("action") == "ENTRY"
            and (e["body"].get("order") or {}).get("legs")
        ),
        None,
    )
    rows = plans + transitioning + acks + ([entry_ack] if entry_ack else [])
    if not rows:
        return _absent("NO_PROTECTION_EVIDENCE")
    # A bracket's protective legs are acknowledged with the entry order itself (necessarily at or
    # before the fill it protects); a crypto native stop is a separate PROTECT ack that follows
    # the fill. Either satisfies "protection is in force for the fill".
    bracket_ack_event_seq = entry_ack["event_seq"] if entry_ack else None
    native_protect_after_fill = next(
        (
            a
            for a in acks
            if first_fill_event_seq is not None and a["event_seq"] > first_fill_event_seq
        ),
        None,
    )
    summary = {
        "protection_plan_count": len(plans),
        "protection_transitioning_count": len(transitioning),
        "protect_or_amend_ack_count": len(acks),
        "bracket_protection_ack_event_seq": bracket_ack_event_seq,
        "native_protect_ack_after_fill_event_seq": (
            native_protect_after_fill["event_seq"] if native_protect_after_fill else None
        ),
        "protected": bracket_ack_event_seq is not None or native_protect_after_fill is not None,
        "last_plan_state": plans[-1]["body"].get("state") if plans else None,
        "last_plan_reason": plans[-1]["body"].get("reason") if plans else None,
    }
    return _present(rows, summary)


def exit_section(setup_events, decisions, fills):
    exit_requests = sorted(
        (
            e
            for e in setup_events
            if e["kind"] == "STATE" and e["body"].get("exit_requested")
        ),
        key=lambda e: e["event_seq"],
    )
    exit_decisions = sorted(
        (d for d in decisions if d["action"] in ("EXIT", "CANCEL") and d["outcome"] == "APPROVED"),
        key=lambda d: d["event_seq"],
    )
    sell_fills = sorted((f for f in fills if f["side"] == "sell"), key=lambda f: f["event_seq"])
    rows = list(exit_requests) + list(exit_decisions) + list(sell_fills)
    if not rows:
        return _absent("NO_EXIT_REQUEST_OR_EXIT_FILL")
    last_request = exit_requests[-1]["body"] if exit_requests else {}
    last_sell = sell_fills[-1] if sell_fills else None
    summary = {
        "exit_reason": last_request.get("exit_requested"),
        "exit_request_event_seq": exit_requests[-1]["event_seq"] if exit_requests else None,
        "exit_decision_count": len(exit_decisions),
        "exit_fill_count": len(sell_fills),
        "exit_fill_qty": _decimal_str(last_sell["qty"]) if last_sell else None,
        "exit_fill_price": _decimal_str(last_sell["price"]) if last_sell else None,
        "exit_fill_at": _decimal_str(last_sell["filled_at"]) if last_sell else None,
    }
    return _present(rows, summary)


def state_closed_section(setup_events):
    rows = sorted(
        (e for e in setup_events if e["kind"] == "STATE"), key=lambda e: e["event_seq"]
    )
    if not rows:
        return _absent("NO_STATE_EVENT")
    last = rows[-1]
    summary = {
        "transition_count": len(rows),
        "states_seen": [r["body"].get("state") for r in rows],
        "final_state": last["body"].get("state"),
        "final_state_event_seq": last["event_seq"],
        "final_reason": last["body"].get("reason"),
        "closed_at": last["body"].get("closed_at"),
        "is_closed": last["body"].get("state") == "CLOSED",
    }
    return _present(rows, summary)


def reconciliation_section(account_events, *, after_event_seq):
    rows = sorted(
        (e for e in account_events if e["kind"] == "BROKER_RECONCILIATION"),
        key=lambda e: e["event_seq"],
    )
    if not rows:
        return _absent("NO_BROKER_RECONCILIATION_EVENT")
    after_close = [
        r for r in rows if after_event_seq is not None and r["event_seq"] > after_event_seq
    ]
    clean_after_close = next((r for r in after_close if r["body"].get("clean")), None)
    last = rows[-1]
    clean_seq = clean_after_close["event_seq"] if clean_after_close else None
    summary = {
        "count": len(rows),
        "last_clean": last["body"].get("clean"),
        "last_event_seq": last["event_seq"],
        "runs_after_exit": len(after_close),
        "clean_run_after_exit_event_seq": clean_seq,
        "clean_after_exit": clean_after_close is not None,
    }
    return _present(rows, summary)


def halts_section(rows, *, available: bool):
    """``rows``: ``lab.execution_halts`` rows, or ``()`` when ``available`` is False because the
    connected role lacks SELECT on it (verified with ``has_table_privilege`` by the collector,
    never inferred from a caught exception)."""
    if not available:
        return _absent("ROLE_CANNOT_READ_LAB_EXECUTION_HALTS")
    summary = {"active_halt_count": len(rows), "reasons": sorted({r["reason"] for r in rows})}
    return {**_present(rows, summary), "present": True}


def reservations_section(reservation, release, *, active_reservation_present: bool):
    rows = [r for r in (reservation, release) if r is not None]
    if reservation is None:
        return _absent("NO_RESERVATION_ROW")
    summary = {
        "reserved_budget": _decimal_str(reservation["budget"]),
        "planned_risk": _decimal_str(reservation["planned_risk"]),
        "qty": _decimal_str(reservation["qty"]),
        "sector": reservation["sector"],
        "theme": reservation["theme"],
        "released": release is not None,
        "release_reason": release["reason"] if release else None,
        "active_reservation_present": active_reservation_present,
    }
    return _present(rows, summary)


def fee_section(fills):
    if not fills:
        return _absent("NO_FILLS_RECORDED")
    known = [f for f in fills if f["fee_usd"] is not None]
    unknown = [f for f in fills if f["fee_usd"] is None]
    total_known = _sum_decimal(f["fee_usd"] for f in known) if known else None
    summary = {
        "fill_count": len(fills),
        "known_fee_count": len(known),
        "unknown_fee_count": len(unknown),
        "unknown_fill_ids": [f["fill_id"] for f in unknown],
        "total_known_fee_usd": _decimal_str(total_known),
        "fees_fully_known": not unknown,
        "pnl_basis": "GROSS_UNLESS_FEES_FULLY_KNOWN",
    }
    return _present(fills, summary)


def verify_audit_export(export_rows, admission_event_seq: int) -> dict:
    """Compute both audit heads from an owner-exported ``lab.trade_events`` file (the shape
    ``Repository.export_events()``/``catalyst-lab export`` produce), using
    ``catalyst_lab.audit.verify_events`` — never touching ``lab.trade_events`` itself, which this
    tool's read-only role cannot read. A tampered or truncated row anywhere in the export, or in
    the admission-to-now segment, is caught here (``verify_events`` raises) and reported as
    ``valid: False``, never silently accepted.
    """
    rows = sorted(export_rows, key=lambda r: r["seq"])
    result = {
        "export_row_count": len(rows),
        "admission_event_seq": admission_event_seq,
        "head_before": None,
        "head_after": None,
        "valid": False,
        "error": None,
    }
    try:
        after = verify_events(rows)
    except ValueError as exc:
        result["error"] = str(exc)
        return result
    result["head_after"] = {"event_seq": after["last_seq"], "hash": after["head_hash"]}
    index = next((i for i, row in enumerate(rows) if row["seq"] == admission_event_seq), None)
    if index is None:
        result["error"] = "ADMISSION_EVENT_SEQ_NOT_IN_EXPORT"
        return result
    try:
        before = verify_events(rows[: index + 1])
    except ValueError as exc:
        result["error"] = str(exc)
        return result
    result["head_before"] = {"event_seq": before["last_seq"], "hash": before["head_hash"]}
    result["valid"] = True
    return result


def audit_section(setup, all_rows_by_group: dict, *, audit_export_rows=None, ledger_rows=None):
    """The audit chain from an owner export when one is supplied, else from ``ledger_rows``
    (``lab.trade_events`` read directly, in the export's shape) when the role may read them.
    ``present`` is False, with a reason, only when neither is available."""
    admission_event_seq = setup["event_seq"]
    seqs = [admission_event_seq]
    for group in all_rows_by_group.values():
        seqs.extend(_event_seqs(group))
    observed_range = {"min": min(seqs), "max": max(seqs)}
    if audit_export_rows is not None:
        rows, source = audit_export_rows, "AUDIT_EXPORT"
    else:
        rows, source = ledger_rows, ("LAB_TRADE_EVENTS" if ledger_rows is not None else None)
    result = {
        "admission_event_seq": admission_event_seq,
        "evidence_event_seq_range": observed_range,
        "export_supplied": audit_export_rows is not None,
        "present": rows is not None,
        "source": source,
        "chain": None,
    }
    if rows is None:
        result["reason"] = "ROLE_CANNOT_READ_LAB_TRADE_EVENTS"
    else:
        result["chain"] = verify_audit_export(rows, admission_event_seq)
    return result


def broker_snapshot_section(snapshot: dict | None, *, symbol: str):
    """``snapshot``: an owner-captured ``{"positions": [...], "open_orders": [...]}`` JSON
    document, read only from the file the script was given — never fetched here."""
    if snapshot is None:
        return _absent("NO_BROKER_SNAPSHOT_SUPPLIED")
    normalized = symbol.replace("/", "").upper()
    positions = [
        p
        for p in snapshot.get("positions", [])
        if str(p.get("symbol", "")).replace("/", "").upper() == normalized
    ]
    orders = [
        o
        for o in snapshot.get("open_orders", [])
        if str(o.get("symbol", "")).replace("/", "").upper() == normalized
    ]
    summary = {
        "captured_at": snapshot.get("captured_at"),
        "positions_for_symbol": len(positions),
        "open_orders_for_symbol": len(orders),
        "flat_and_no_open_orders": not positions and not orders,
    }
    return {
        "present": True,
        "event_seqs": [],
        "sha256": rows_sha256(snapshot),
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Checks — G3 pass/fail conditions, computed from the sections above.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str

    def as_dict(self):
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


def build_checks(sections: dict) -> list:
    checks = []

    sub = sections["stream_subscriptions"]
    if sub["present"] and sub["summary"].get("acknowledged_before_trigger"):
        checks.append(
            Check(
                "SUBSCRIPTION_ACKNOWLEDGED_BEFORE_TRIGGER",
                True,
                "Market and trade-updates stream acks recorded before the trigger print.",
            )
        )
    else:
        checks.append(
            Check(
                "SUBSCRIPTION_ACKNOWLEDGED_BEFORE_TRIGGER",
                False,
                sub["summary"].get("reason", "Subscription evidence does not precede the trigger."),
            )
        )

    decision = sections["risk_decision"]
    if decision["present"] and decision["summary"].get("entry_decision_id") and (
        decision["summary"].get("ttl_within_5_seconds") and decision["summary"].get("claimed")
    ):
        checks.append(
            Check(
                "RISK_DECISION_CLAIMED_WITHIN_TTL",
                True,
                f"Decision expires_at-created_at = {decision['summary']['ttl_seconds']}s (<=5s) "
                "and a managed_claims row exists.",
            )
        )
    else:
        checks.append(
            Check(
                "RISK_DECISION_CLAIMED_WITHIN_TTL",
                False,
                "No approved, claimed ENTRY decision within the 5-second TTL was found.",
            )
        )

    fills = sections["fills"]
    quantity_matches = False
    if (
        fills["present"]
        and decision["present"]
        and decision["summary"].get("entry_qty") is not None
        and fills["summary"].get("buy_qty") is not None
    ):
        # Numeric, never string, comparison: Decimal addition across several partial fills can
        # render as "79.0" while the authorized payload qty renders as "79" — equal in value,
        # not in text.
        try:
            quantity_matches = Decimal(fills["summary"]["buy_qty"]) == Decimal(
                decision["summary"]["entry_qty"]
            )
        except (ArithmeticError, TypeError, ValueError):
            quantity_matches = False
    if quantity_matches:
        checks.append(
            Check(
                "FILL_QUANTITY_MATCHES_AUTHORIZED_QUANTITY",
                True,
                f"Buy fills sum to {fills['summary']['buy_qty']}, "
                f"equal to the authorized qty {decision['summary']['entry_qty']}.",
            )
        )
    else:
        checks.append(
            Check(
                "FILL_QUANTITY_MATCHES_AUTHORIZED_QUANTITY",
                False,
                "Filled quantity does not match the risk-authorized quantity, or either is "
                "unavailable.",
            )
        )

    protection = sections["protection"]
    checks.append(
        Check(
            "PROTECTION_ACKNOWLEDGED_FOR_FILL",
            bool(protection["present"] and protection["summary"].get("protected")),
            "A bracket protection ack (at entry) or a native PROTECT ack after the fill was found."
            if protection["present"] and protection["summary"].get("protected")
            else "No protective acknowledgement covering the fill was found.",
        )
    )

    exit_ = sections["exit"]
    state = sections["state_closed"]
    exit_closes = (
        exit_["present"]
        and exit_["summary"].get("exit_fill_count", 0) > 0
        and state["present"]
        and state["summary"].get("is_closed")
    )
    checks.append(
        Check(
            "EXIT_FILL_CLOSES_POSITION",
            bool(exit_closes),
            "An exit fill was recorded and the setup's final state is CLOSED."
            if exit_closes
            else "No exit fill was recorded, or the setup's final state is not CLOSED.",
        )
    )

    reservations = sections["reservations"]
    no_active_reservation = bool(
        reservations["present"] and not reservations["summary"].get("active_reservation_present")
    )
    checks.append(
        Check(
            "ZERO_RESIDUAL_NO_ACTIVE_RESERVATION",
            no_active_reservation,
            "lab.managed_active_reservations has no row for this setup."
            if no_active_reservation
            else "An active reservation still exists for this setup, or none was ever recorded.",
        )
    )

    position = sections["broker_position"]
    no_open_position = bool(position["present"] and position["summary"].get("flat"))
    checks.append(
        Check(
            "ZERO_RESIDUAL_NO_OPEN_POSITION_EVENT",
            no_open_position,
            "The latest BROKER_POSITION event reports zero quantity."
            if no_open_position
            else "No BROKER_POSITION evidence, or the latest one is not flat.",
        )
    )

    halts = sections["halts"]
    if halts["present"]:
        no_halts = halts["summary"].get("active_halt_count") == 0
        checks.append(
            Check(
                "ZERO_RESIDUAL_NO_HALTS",
                no_halts,
                "lab.execution_halts is empty."
                if no_halts
                else f"{halts['summary'].get('active_halt_count')} active halt(s) recorded.",
            )
        )
    else:
        checks.append(
            Check(
                "ZERO_RESIDUAL_NO_HALTS",
                False,
                "Fails closed: " + halts["summary"]["reason"],
            )
        )

    reconciliation = sections["reconciliation"]
    clean_after = bool(
        reconciliation["present"] and reconciliation["summary"].get("clean_after_exit")
    )
    checks.append(
        Check(
            "ZERO_RESIDUAL_CLEAN_RECONCILIATION_AFTER_EXIT",
            clean_after,
            "A clean BROKER_RECONCILIATION event was recorded after the exit."
            if clean_after
            else "No clean reconciliation recorded after the exit.",
        )
    )

    return [c.as_dict() for c in checks]


# ---------------------------------------------------------------------------
# unknowns / manifest assembly
# ---------------------------------------------------------------------------


def collect_unknowns(sections: dict) -> dict:
    unknowns = {}
    for name, section in sections.items():
        if not section["present"]:
            unknowns[name] = section["summary"].get("reason", "UNAVAILABLE")
    fees = sections.get("fees", {})
    if fees.get("present") and not fees["summary"].get("fees_fully_known"):
        unknowns["fees"] = {
            "reason": "FEE_NOT_REPORTED_BY_BROKER_EVENT",
            "unknown_fill_ids": fees["summary"]["unknown_fill_ids"],
        }
    return unknowns


def build_manifest(
    setup,
    *,
    setup_events,
    account_events,
    decisions,
    fills,
    reservation,
    release,
    active_reservation_present,
    halt_rows,
    halts_available,
    audit_export_rows=None,
    broker_snapshot=None,
    audit_ledger_rows=None,
) -> dict:
    """Assemble ``MANAGED_ACCEPTANCE_EVIDENCE_V1`` from already-fetched rows. Pure: raises
    nothing that depends on wall-clock time, and produces byte-identical output for the same
    inputs.
    """
    trigger = trigger_print_section(setup_events)
    trigger_event_seq = trigger["summary"].get("trigger_event_seq") if trigger["present"] else None
    entry_decision = next(
        (d for d in decisions if d["action"] == "ENTRY" and d["outcome"] == "APPROVED"), None
    )
    first_buy_fill = next((f for f in fills if f["side"] == "buy"), None)

    risk_decision = risk_decision_section(decisions, trigger_event_seq=trigger_event_seq)
    state_closed = state_closed_section(setup_events)
    close_event_seq = (
        state_closed["summary"]["final_state_event_seq"]
        if state_closed["present"] and state_closed["summary"]["is_closed"]
        else None
    )

    sections = {
        "stream_subscriptions": stream_subscription_section(
            account_events,
            market=setup["market"],
            symbol=setup["symbol"],
            trigger_event_seq=trigger_event_seq,
        ),
        "trigger_print": trigger,
        "entry_eligibility": entry_eligibility_section(setup_events),
        "risk_check": risk_check_section(setup_events),
        "risk_decision": risk_decision,
        "broker_ack": broker_ack_section(
            setup_events,
            entry_decision_created_at=entry_decision["created_at"] if entry_decision else None,
        ),
        "fills": fills_section(fills, setup_events),
        "broker_position": broker_position_section(setup_events),
        "protection": protection_section(
            setup_events,
            first_fill_event_seq=first_buy_fill["event_seq"] if first_buy_fill else None,
        ),
        "exit": exit_section(setup_events, decisions, fills),
        "state_closed": state_closed,
        "reconciliation": reconciliation_section(account_events, after_event_seq=close_event_seq),
        "halts": halts_section(halt_rows, available=halts_available),
        "reservations": reservations_section(
            reservation, release, active_reservation_present=active_reservation_present
        ),
        "fees": fee_section(fills),
        "broker_snapshot": broker_snapshot_section(broker_snapshot, symbol=setup["symbol"]),
    }

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "setup_id": str(setup["setup_id"]),
        "market": setup["market"],
        "symbol": setup["symbol"],
        "strategy_version": setup["strategy_version"],
        "cohort": setup["cohort"],
        "policy_id": setup["policy_id"],
        "sections": sections,
        "audit": audit_section(
            setup,
            {
                "setup_events": setup_events,
                "account_events": account_events,
                "decisions": decisions,
                "fills": fills,
            },
            audit_export_rows=audit_export_rows,
            ledger_rows=audit_ledger_rows,
        ),
    }
    manifest["checks"] = build_checks(sections)
    manifest["unknowns"] = collect_unknowns(sections)
    return manifest


def scan_for_credential_shapes(manifest: dict) -> list:
    """Sorted credential-pattern names found in the manifest's JSON text, for the caller to
    assert against before ever writing or printing the manifest. Reuses the one pattern list the
    audit checkpoint writer and the repository-hygiene test already share."""
    return credential_findings(canonical_json(manifest))


# ---------------------------------------------------------------------------
# The one function that touches the database.
# ---------------------------------------------------------------------------


def check_role(conn) -> str:
    role = conn.execute("SELECT current_user AS role").fetchone()["role"]
    if role in FORBIDDEN_ROLES:
        raise RoleRefused(
            f"Refusing to run as {role!r}: this tool must connect as a read-only role "
            "(catalyst_review or catalyst_reporting), never catalyst_app, catalyst_risk or "
            "lab_owner."
        )
    if role not in ALLOWED_ROLES:
        raise RoleRefused(
            f"Refusing to run as {role!r}: expected catalyst_review or catalyst_reporting."
        )
    return role


def _require_managed_setups_readable(conn, role: str) -> None:
    """``catalyst_reporting`` is an allowed role (it is genuinely read-only) but its grants are
    the frozen V1 strategy_*/public_* views only — no lab.managed_* table, not even the one this
    collector must read first to know which setup it is looking at. Refuse clearly here instead
    of letting a raw permission-denied surface mid-query, or worse, silently returning a manifest
    with every section absent."""
    if not _has_privilege(conn, "lab.managed_setups"):
        raise RoleRefused(
            f"Refusing to continue as {role!r}: it cannot read lab.managed_setups, so it cannot "
            "identify a managed setup at all. Use catalyst_review to run this tool."
        )


def _resolve_latest_closed_setup_id(conn):
    row = conn.execute(
        "SELECT s.setup_id FROM lab.managed_setups s "
        "JOIN lab.managed_states t USING(setup_id) "
        "WHERE t.body->>'state'='CLOSED' ORDER BY s.event_seq DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise SetupNotFound("No CLOSED managed setup is visible to this role.")
    return row["setup_id"]


def _has_privilege(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT has_table_privilege(current_user,%s,'SELECT') AS allowed", (table,)
    ).fetchone()
    return bool(row["allowed"])


def _trade_event_rows(conn) -> list:
    """``lab.trade_events`` in exactly the shape ``Repository.export_events`` writes, so
    ``verify_audit_export`` treats a direct read and an owner export alike."""
    rows = []
    for row in conn.execute("SELECT * FROM lab.trade_events ORDER BY seq").fetchall():
        item = json_safe(row)
        item["created_at"] = row["created_at"].astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        rows.append(item)
    return rows


def collect_acceptance_evidence(
    conn, *, setup_id=None, latest_closed=False, audit_export_rows=None, broker_snapshot=None
) -> dict:
    """Read one managed setup's evidence over ``conn`` (already connected, read-only role) and
    return the deterministic manifest. Exactly one of ``setup_id``/``latest_closed`` is used to
    pick the setup. Raises ``RoleRefused`` if the connected role may not run this tool, and
    ``SetupNotFound`` if the requested setup does not exist or is not visible to this role.
    """
    if bool(setup_id) == bool(latest_closed):
        raise ValueError("Pass exactly one of setup_id or latest_closed")
    role = check_role(conn)
    _require_managed_setups_readable(conn, role)
    if latest_closed:
        setup_id = _resolve_latest_closed_setup_id(conn)
    setup = conn.execute(
        "SELECT setup_id, cycle_id, revision, symbol, market, strategy_version, policy_id, "
        "cohort, receipt_id, evidence_hash, expires_at, record_json, event_seq "
        "FROM lab.managed_setups WHERE setup_id=%s",
        (setup_id,),
    ).fetchone()
    if not setup:
        raise SetupNotFound(f"No managed setup {setup_id} is visible to this role.")

    setup_events = conn.execute(
        "SELECT event_seq, kind, body, recorded_at FROM lab.managed_events "
        "WHERE setup_id=%s ORDER BY event_seq",
        (setup["setup_id"],),
    ).fetchall()
    account_events = conn.execute(
        "SELECT event_seq, kind, body, recorded_at FROM lab.managed_events "
        "WHERE setup_id IS NULL AND kind = ANY(%s) ORDER BY event_seq",
        (["RUNTIME_MARKET_CONNECTED", "RUNTIME_STREAM_CONNECTED", "BROKER_RECONCILIATION"],),
    ).fetchall()
    decisions = conn.execute(
        "SELECT d.decision_id, d.action, d.outcome, d.reason, d.method, d.path, d.payload, "
        "d.context, d.equity, d.expires_at, d.created_at, d.event_seq, "
        "(c.decision_id IS NOT NULL) AS claimed "
        "FROM lab.managed_risk_decisions d LEFT JOIN lab.managed_claims c USING(decision_id) "
        "WHERE d.setup_id=%s ORDER BY d.event_seq",
        (setup["setup_id"],),
    ).fetchall()
    fills = conn.execute(
        "SELECT fill_id, broker_order_id, side, qty, price, filled_at, fee_usd, source, "
        "event_seq FROM lab.managed_fills WHERE setup_id=%s ORDER BY event_seq",
        (setup["setup_id"],),
    ).fetchall()
    reservation = conn.execute(
        "SELECT * FROM lab.managed_reservations WHERE setup_id=%s", (setup["setup_id"],)
    ).fetchone()
    release = conn.execute(
        "SELECT * FROM lab.managed_releases WHERE setup_id=%s", (setup["setup_id"],)
    ).fetchone()
    active_reservation_present = (
        conn.execute(
            "SELECT 1 FROM lab.managed_active_reservations WHERE setup_id=%s", (setup["setup_id"],)
        ).fetchone()
        is not None
    )
    halts_available = _has_privilege(conn, "lab.execution_halts")
    halt_rows = (
        conn.execute("SELECT event_seq, reason, candidate_id FROM lab.execution_halts").fetchall()
        if halts_available
        else []
    )
    # The chain from the ledger itself when this role may read it and no export was given.
    audit_ledger_rows = (
        _trade_event_rows(conn)
        if audit_export_rows is None and _has_privilege(conn, "lab.trade_events")
        else None
    )

    return build_manifest(
        setup,
        setup_events=setup_events,
        account_events=account_events,
        decisions=decisions,
        fills=fills,
        reservation=reservation,
        release=release,
        active_reservation_present=active_reservation_present,
        halt_rows=halt_rows,
        halts_available=halts_available,
        audit_export_rows=audit_export_rows,
        broker_snapshot=broker_snapshot,
        audit_ledger_rows=audit_ledger_rows,
    )
