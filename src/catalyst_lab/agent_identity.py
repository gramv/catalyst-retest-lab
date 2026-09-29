"""Research-agent identity without a registry (plan 1.3, minimal form).

Each configured agent token acts for exactly one ``agent_id``. The legacy Muse token
(``MANAGED_API_TOKEN``, private config ``muse.token_file``) acts for agent ``muse`` with
version ``LEGACY_UNDECLARED`` and is the only credential that may submit unversioned
legacy reports; those stay LEGACY_UNATTRIBUTED and are never inferred as Muse.

Identity is access and reporting metadata only. It is recorded in research event bodies,
setup records and the Jev ``evidence_identity``, never in a review state, so selection
reviews stay blind to the proposing agent. Agent versions and guideline hashes are declared
by the agent and recorded as reported; no registry verifies them in this form.

Dependency-free so the external Muse worker can import it.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID, uuid5

REPORT_SCHEMA_V2 = "AGENT_RESEARCH_REPORT_V2"
AGENT_ID_PATTERN = r"^[a-z][a-z0-9_-]{1,31}$"
AGENT_VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,31}$"
GUIDELINES_VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
AGENT_ID = re.compile(AGENT_ID_PATTERN)
AGENT_VERSION = re.compile(AGENT_VERSION_PATTERN)

LEGACY_AGENT_ID = "muse"
LEGACY_AGENT_VERSION = "LEGACY_UNDECLARED"
ATTRIBUTED = "AGENT_ATTRIBUTED"
LEGACY_UNATTRIBUTED = "LEGACY_UNATTRIBUTED"
MAX_AGENT_TOKENS = 32
# uuid5(NAMESPACE_URL, "urn:catalyst-retest-lab:agent-research-cycle:v1"). Fixed forever:
# recorded V2 cycle IDs are derived from it.
AGENT_CYCLE_NAMESPACE = UUID("284306f0-6a31-5d71-a753-f854db41ccfa")


def agent_cycle_id(agent_id, report_id):
    """V2 cycle ID: two agents reusing one report ID can never share a cycle."""
    if not isinstance(agent_id, str) or not AGENT_ID.fullmatch(agent_id):
        raise ValueError("AGENT_ID_INVALID")
    return str(uuid5(AGENT_CYCLE_NAMESPACE, f"{agent_id}:{UUID(str(report_id))}"))


def attribution(agent):
    """Reporting dimensions of a stored ``agent`` block; absent or null is legacy."""
    if isinstance(agent, Mapping) and isinstance(agent.get("agent_id"), str):
        return {
            "attribution": ATTRIBUTED,
            "agent_id": agent["agent_id"],
            "agent_version": agent.get("agent_version"),
            "guidelines_version": agent.get("guidelines_version"),
            "guidelines_sha256": agent.get("guidelines_sha256"),
        }
    return {"attribution": LEGACY_UNATTRIBUTED, "agent_id": None, "agent_version": None,
            "guidelines_version": None, "guidelines_sha256": None}


def agent_fields(agent):
    """The attribution carried on setup, position and result payloads."""
    return {k: v for k, v in attribution(agent).items()
            if k in {"attribution", "agent_id", "agent_version"}}


def valid_token(value):
    return isinstance(value, str) and len(value) >= 32 and not any(c.isspace() for c in value)


def validated_agent_tokens(value, *, reserved=()):
    """``{agent_id: token}`` with valid IDs and tokens, each distinct from every other token.

    ``reserved`` are the other role tokens (legacy Muse, status, operator). Errors never
    include a token.
    """
    if value is None:
        return {}
    if (
        not isinstance(value, Mapping)
        or len(value) > MAX_AGENT_TOKENS
        or any(not isinstance(k, str) or not AGENT_ID.fullmatch(k) for k in value)
        or any(not valid_token(token) for token in value.values())
    ):
        raise ValueError("SEPARATE_AGENT_TOKENS_REQUIRED")
    tokens = list(value.values())
    others = {token for token in reserved if token is not None}
    if len(set(tokens)) != len(tokens) or others & set(tokens):
        raise ValueError("SEPARATE_AGENT_TOKENS_REQUIRED")
    return dict(value)


@dataclass(frozen=True)
class Principal:
    """The authenticated caller: a role and, for research agents, the agent it acts for."""

    role: str
    agent_id: str | None = None
    legacy: bool = False

    def acts_for(self, agent_id):
        """May act for ``agent_id``; ``None`` is an unattributed legacy report or cycle."""
        if self.role != "muse":
            return False
        if agent_id is None:
            return self.legacy
        return agent_id == self.agent_id


def legacy_principal():
    return Principal("muse", LEGACY_AGENT_ID, legacy=True)
