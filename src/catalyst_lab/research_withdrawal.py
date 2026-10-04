"""``AGENT_RESEARCH_WITHDRAWAL_V1``: a research agent withdraws its own unfilled picks.

docs/RESEARCH-LOOP-V2.md 3.3 (owner direction 2026-09-28, "cancels irrelevant ones"; package
research-loop-app). ``POST /api/v1/lab/research-withdrawals`` with the agent's own token names
1-30 coins, each with the agent's reason. In one ledger transaction under the shared lock
(``ManagedStore.transaction``: the lock admission, selection publication and the run
supersession pass take), for each coin:

* each of the calling agent's own report-V3 setups still ``WATCHING`` on that coin is revoked
  ``WITHDRAWN_BY_RESEARCH`` through the revoke path (``ManagedStore.revoke``, only while still
  WATCHING): one ``REVOKE`` keyed ``research:withdrawn:<withdrawal_id>:<setup_id>`` with the
  withdrawal ID, the agent's reason and its identity, and the ``INVALIDATED`` revision;
* each of its own unexpired report-V3 selections on that coin that is neither admitted nor
  declined gets the one ``RESEARCH_ADMISSION_DECLINED`` ``WITHDRAWN_BY_RESEARCH`` under the
  runtime's decline key, so it is never offered again. No ``TOPK_REPLACEMENT_V1`` decision
  follows (the agent's own newer research replaces it), and admission refuses a withdrawn
  selection even when it was admitting it at the same time.

The owner of a setup or selection is the ``agent`` block its packet recorded (the report's,
stored outside the reviewed state), read exactly as the research context reads the caller's
own trades. A setup past WATCHING (entry working, open, closing, closed), a non-V3 setup and
every other agent's record are never touched. A WATCHING setup has no broker order, so a
withdrawal makes no broker call and has no size, order or execution effect.

One ``RESEARCH_WITHDRAWAL`` event (no setup; key ``research-withdrawal:<agent_id>:
<withdrawal_id>``) records the request, its SHA-256 and one result per coin: ``WITHDRAWN``
(with the revoked setup IDs and the number of declined selections), ``NOT_WATCHING`` (nothing
withdrawn: the agent's live setup on that coin has left WATCHING) or ``NONE``. An identical
resend returns the stored results (``idempotent_replay``); a different body under the same ID is
``WITHDRAWAL_ID_CONFLICT``. The agent's identity is in these event bodies only; nothing here
reaches Jev. Errors name codes and field paths, never submitted values.
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from catalyst_lab.agent_identity import AGENT_ID_PATTERN, AGENT_VERSION_PATTERN
from catalyst_lab.jev_contract import digest, encoded
from catalyst_lab.managed_store import TERMINAL
from catalyst_lab.muse_reports import ResearchReportRejected, validation_errors
from catalyst_lab.repository import json_safe
from catalyst_lab.research_evidence import sensitive_paths
from catalyst_lab.research_report_v3 import REPORT_SCHEMA_V3, SYMBOL_PATTERN
from catalyst_lab.system_check import ADMISSION_DECLINED_KEY, WITHDRAWN_BY_RESEARCH

WITHDRAWAL_SCHEMA = "AGENT_RESEARCH_WITHDRAWAL_V1"
WITHDRAWAL_EVENT = "RESEARCH_WITHDRAWAL"
WITHDRAWAL_KEY = "research-withdrawal:"
REVOKE_KEY = "research:withdrawn:"
# The report route's limits (``POST /api/v1/lab/research-reports``): the 1 MiB body.
WITHDRAWAL_BODY_LIMIT = 1048576
MAX_WITHDRAWAL_ITEMS = 30
MAX_REASON_CHARS = 300
INVALID_WITHDRAWAL = "INVALID_RESEARCH_WITHDRAWAL"
WITHDRAWAL_TOO_LARGE = "RESEARCH_WITHDRAWAL_TOO_LARGE"
WITHDRAWAL_ID_CONFLICT = "WITHDRAWAL_ID_CONFLICT"
RECORDED = "RESEARCH_WITHDRAWAL_RECORDED"
WITHDRAWN, NOT_WATCHING, NONE = "WITHDRAWN", "NOT_WATCHING", "NONE"


class WithdrawalRejected(ResearchReportRejected):
    """A refused withdrawal, nothing stored; answered 422 like a refused report."""


class WithdrawalConflict(Exception):
    """The withdrawal ID is recorded with a different body (HTTP 409)."""

    def __init__(self):
        super().__init__(WITHDRAWAL_ID_CONFLICT)


class WithdrawalAgent(BaseModel):
    """The withdrawing agent: exactly its ID (the credential's) and its declared version."""

    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    agent_version: str = Field(pattern=AGENT_VERSION_PATTERN)


class WithdrawalItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    symbol: str = Field(min_length=1, max_length=32, pattern=SYMBOL_PATTERN)
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS)


class ResearchWithdrawal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["AGENT_RESEARCH_WITHDRAWAL_V1"]
    withdrawal_id: UUID
    agent: WithdrawalAgent
    items: list[WithdrawalItem] = Field(min_length=1, max_length=MAX_WITHDRAWAL_ITEMS)

    @field_validator("items")
    @classmethod
    def unique_symbols(cls, items):
        symbols = [item.symbol for item in items]
        if len(set(symbols)) != len(symbols):
            raise ValueError("DUPLICATE_SYMBOL_IN_WITHDRAWAL")
        return items


class _DeclaredWithdrawalAgent(BaseModel):
    """Only a withdrawal's schema version and agent block, for the credential check."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["AGENT_RESEARCH_WITHDRAWAL_V1"]
    agent: WithdrawalAgent


def declared_agent(raw):
    """The validated agent block of a withdrawal body: the HTTP layer compares it with the
    credential before anything else, as for reports."""
    try:
        return _DeclaredWithdrawalAgent.model_validate(raw).agent.model_dump(mode="json")
    except ValidationError as exc:
        raise WithdrawalRejected(INVALID_WITHDRAWAL, errors=validation_errors(exc)) from None


def parse_withdrawal(raw):
    """``(withdrawal, canonical)``: strict JSON, the credential/e-mail/0x screen over the whole
    body (``SENSITIVE_EVIDENCE_REJECTED``, as for reports), then the schema. Pure."""
    if not isinstance(raw, dict):
        raise WithdrawalRejected(INVALID_WITHDRAWAL)
    try:
        plain = json_safe(raw)
        encoded(plain)  # Strict JSON only before any screening.
    except (TypeError, ValueError):
        raise WithdrawalRejected(INVALID_WITHDRAWAL) from None
    flagged = sensitive_paths(plain)
    if flagged:
        raise WithdrawalRejected(
            "SENSITIVE_EVIDENCE_REJECTED",
            errors=[{"path": path, "code": "SENSITIVE_EVIDENCE_REJECTED"} for path in flagged],
        )
    try:
        withdrawal = ResearchWithdrawal.model_validate(raw)
    except ValidationError as exc:
        raise WithdrawalRejected(INVALID_WITHDRAWAL, errors=validation_errors(exc)) from None
    return withdrawal, withdrawal.model_dump(mode="json")


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("AWARE_CLOCK_REQUIRED")
    return value.astimezone(UTC)


def _owned(alias):
    """SQL restricting a packet (``alias``) to the caller's own: its agent's records, and
    unattributed legacy records for the legacy credential only (research_context's rule)."""
    return (f"(({alias}->'agent'->>'agent_id')=%(agent)s OR "
            f"(%(legacy)s AND ({alias}->'agent'->>'agent_id') IS NULL))")


class ResearchWithdrawals:
    """Records ``AGENT_RESEARCH_WITHDRAWAL_V1`` requests for the research-agent route.

    ``store`` is the managed store (``catalyst_risk``: it appends ``lab.managed_events``);
    ``clock`` stamps ``received_at``. Nothing here reads a broker or calls Jev.
    """

    def __init__(self, store, *, clock):
        self.store, self.clock = store, clock

    @staticmethod
    def key(agent_id, withdrawal_id):
        return f"{WITHDRAWAL_KEY}{agent_id}:{withdrawal_id}"

    @staticmethod
    def _receipt(body, *, replay):
        return json_safe({
            "status": RECORDED, "schema_version": WITHDRAWAL_SCHEMA,
            "withdrawal_id": body["withdrawal_id"], "agent_id": body["agent"]["agent_id"],
            "agent_version": body["agent"]["agent_version"], "results": body["results"],
            "idempotent_replay": replay, "trade_authorized": False,
        })

    def withdraw(self, raw, *, principal):
        """Validate and apply one withdrawal for ``principal`` (the authenticated caller, whose
        agent the HTTP layer checked against the body); returns the receipt."""
        withdrawal, canonical = parse_withdrawal(raw)
        if not principal.acts_for(withdrawal.agent.agent_id):
            raise PermissionError("AGENT_IDENTITY_MISMATCH")  # The HTTP layer answers 403 first.
        request_sha256 = digest(encoded(canonical))
        key = self.key(withdrawal.agent.agent_id, withdrawal.withdrawal_id)
        owner = {"agent": principal.agent_id, "legacy": bool(principal.legacy),
                 "v3": REPORT_SCHEMA_V3, "declined": ADMISSION_DECLINED_KEY}
        with self.store.transaction() as conn:
            prior = conn.execute(
                "SELECT kind,body FROM lab.managed_events WHERE idempotency_key=%s", (key,)
            ).fetchone()
            if prior is not None:
                if prior["kind"] != WITHDRAWAL_EVENT or (
                        prior["body"].get("request_sha256") != request_sha256):
                    raise WithdrawalConflict()
                return self._receipt(prior["body"], replay=True)
            received_at = _utc(self.clock())
            results = [self._item(conn, item, canonical, owner) for item in withdrawal.items]
            body = {
                "schema_version": WITHDRAWAL_SCHEMA,
                "withdrawal_id": canonical["withdrawal_id"],
                "agent": canonical["agent"],
                "request_sha256": request_sha256,
                "items": canonical["items"],
                "results": results,
                "received_at": received_at.isoformat(),
            }
            self.store.event(conn, WITHDRAWAL_EVENT, body, key=key)
        return self._receipt(body, replay=False)

    def _item(self, conn, item, canonical, owner):
        """One coin's effect and result, in the withdrawal's transaction."""
        params = {**owner, "symbol": item.symbol}
        setups = conn.execute(
            f"""SELECT s.setup_id,t.body->>'state' AS state FROM lab.managed_setups s
            JOIN lab.managed_states t USING(setup_id)
            WHERE s.symbol=%(symbol)s AND s.record_json->>'report_schema_version'=%(v3)s
            AND {_owned('s.record_json')} ORDER BY s.event_seq""",
            params,
        ).fetchall()
        details = {"withdrawal_id": canonical["withdrawal_id"], "withdrawal_reason": item.reason,
                   "agent": canonical["agent"]}
        revoked, live = [], []
        for row in setups:
            setup_id = row["setup_id"]
            if row["state"] == "WATCHING" and self.store.revoke(
                conn, setup_id, WITHDRAWN_BY_RESEARCH, watching_only=True, details=details,
                key=f"{REVOKE_KEY}{canonical['withdrawal_id']}:{setup_id}",
            ):
                revoked.append(str(setup_id))
            elif row["state"] not in TERMINAL:
                live.append(str(setup_id))  # Entry working, open or closing: never touched.
        # Offered for admission exactly as the runtime reads it (``_selected_packets``):
        # unexpired by the ledger's clock, without a setup and without a decline.
        selections = conn.execute(
            f"""SELECT e.event_seq,e.body->'packet' AS packet FROM lab.managed_events e
            WHERE e.setup_id IS NULL AND e.kind='RESEARCH_SELECTED'
            AND e.body->'packet'->>'report_schema_version'=%(v3)s
            AND e.body->'packet'->>'symbol'=%(symbol)s
            AND (e.body->'packet'->>'expires_at')::timestamptz>clock_timestamp()
            AND {_owned("e.body->'packet'")}
            AND NOT EXISTS(SELECT 1 FROM lab.managed_setups s
                WHERE s.receipt_id::text=e.body->'packet'->>'receipt_id')
            AND NOT EXISTS(SELECT 1 FROM lab.managed_events d
                WHERE d.idempotency_key=%(declined)s||e.event_seq::text)
            ORDER BY e.event_seq""",
            params,
        ).fetchall()
        for row in selections:
            packet = row["packet"]
            self.store.event(conn, "RESEARCH_ADMISSION_DECLINED", {
                "cycle_id": packet.get("cycle_id"),
                "item_key": packet.get("item_key"),
                "revision": packet.get("revision"),
                "receipt_id": packet.get("receipt_id"),
                "selection_event_seq": row["event_seq"],
                "reason": WITHDRAWN_BY_RESEARCH,
                **details,
            }, key=f"{ADMISSION_DECLINED_KEY}{row['event_seq']}")
        if revoked or selections:
            result = {"result": WITHDRAWN, "setup_ids": revoked,
                      "selections_declined": len(selections)}
        elif live:
            result = {"result": NOT_WATCHING, "setup_ids": live, "selections_declined": 0}
        else:
            result = {"result": NONE, "setup_ids": [], "selections_declined": 0}
        return {"symbol": item.symbol, **result}


__all__ = [
    "MAX_REASON_CHARS", "MAX_WITHDRAWAL_ITEMS", "NONE", "NOT_WATCHING", "RECORDED",
    "ResearchWithdrawal", "ResearchWithdrawals", "WITHDRAWAL_BODY_LIMIT", "WITHDRAWAL_EVENT",
    "WITHDRAWAL_ID_CONFLICT", "WITHDRAWAL_SCHEMA", "WITHDRAWAL_TOO_LARGE", "WITHDRAWN",
    "WithdrawalConflict", "WithdrawalItem", "WithdrawalRejected", "declared_agent",
    "parse_withdrawal",
]
