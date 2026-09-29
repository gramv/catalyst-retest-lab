"""What the outlook and post-mortem sends share: finished sources, the agent block, the
app's own screens, and the run folder's research-run ID.

Sources are checked, never trusted: each cited page is fetched again
(``sources.check_sources``), the excerpt must verify exactly, a ``published_at`` must be
one of the page's own metadata times, and the finished object (with a neutral
``source_id`` and the fetch's real ``retrieved_at``) must pass the app's own
``SourceExcerpt`` model, the "report's source object exactly" both routes take. The whole
body then goes through the app's own credential, e-mail and 0x-address screen
(``research_evidence.sensitive_paths``), so the kit refuses locally what the app would
refuse. This module imports ``catalyst_lab`` for those models only, the same one-way
dependency ``build`` has.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError

from catalyst_lab.research_evidence import sensitive_paths
from catalyst_lab.review_storage import SourceExcerpt
from research_agent import build, sources

RUN_FILE = "run.json"


class RecordError(Exception):
    """A whole-send refusal (no schedule, no guidelines in the context, ...)."""


def run_id(run_dir, *, now=None):
    """The research run's ID for this run folder: the morning's report and outlook share it
    (``agent.run_id`` is "a UUID per research run"). Created once, then reused."""
    path = run_dir / RUN_FILE
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))["run_id"]
    value = str(uuid4())
    path.write_text(json.dumps({"run_id": value, "created_at": (now or datetime.now(UTC))
                                .astimezone(UTC).isoformat()}, indent=2) + "\n",
                    encoding="utf-8")
    return value


def agent_block(*, agent_id, agent_version, context, run):
    """The report agent block both learning routes take, with the guidelines the context
    serves."""
    build.check_agent(agent_id, agent_version)
    report_format = context.get("report_format") or {}
    version, sha = report_format.get("guidelines_version"), report_format.get("guidelines_sha256")
    if not version or not sha:
        raise RecordError("CONTEXT_HAS_NO_GUIDELINES: the research context's report_format "
                          "names no guidelines version and SHA-256 to declare")
    return {"agent_id": agent_id, "agent_version": agent_version, "guidelines_version": version,
            "guidelines_sha256": sha, "run_id": run}


def run_slot(context, now):
    """The run an outlook sent at ``now`` answers (``build.run_slot_for``)."""
    slot, _limit = build.run_slot_for(context.get("schedule"), now)
    if not slot:
        raise RecordError("RESEARCH_CONTEXT_HAS_NO_SCHEDULE: no run slot to declare")
    return slot


def verify_citations(cited, *, client=None, clock=None):
    """``({path: finished source}, problems)`` for ``[(path, {url, excerpt,
    published_at})]``. Source IDs are ``s1``, ``s2``, ... in order: neutral, so they can
    never name the agent."""
    checks = sources.check_sources(
        [(source["url"], source["excerpt"], source.get("published_at")) for _p, source in cited],
        client=client, clock=clock)
    verified, problems = {}, []
    for number, ((path, source), check) in enumerate(zip(cited, checks, strict=True), 1):
        if not check.ok:
            problems.append(f"{path}.source: {check.problem}")
            continue
        finished = {"source_id": f"s{number}", "url": source["url"],
                    "excerpt": source["excerpt"], "published_at": source.get("published_at"),
                    "retrieved_at": check.retrieved_at.astimezone(UTC).isoformat()}
        try:
            SourceExcerpt.model_validate(finished)
        except ValidationError as exc:
            codes = "; ".join(sorted({str(error.get("msg")) for error in exc.errors()}))
            problems.append(f"{path}.source: refused by the app's source model ({codes})")
            continue
        verified[path] = finished
    return verified, problems


def app_model_problems(body, *, model, items_model=None):
    """What the app's own learning models (``catalyst_lab.learning_intake``, package
    learning-app) find wrong with ``body``: none before that package is merged into this
    checkout, when the kit's own checks (which mirror them) are all there is. ``items_model``
    also checks each of ``body["items"]``, as the post-mortem intake does item by item."""
    try:
        from catalyst_lab import learning_intake
    except ImportError:
        return []
    found = []
    checks = [(getattr(learning_intake, model), body, "")]
    if items_model:
        checks += [(getattr(learning_intake, items_model), item, f"items[{index}].")
                   for index, item in enumerate(body.get("items") or [])]
    for checker, value, prefix in checks:
        try:
            checker.model_validate(value)
        except ValidationError as exc:
            found.extend(f"{prefix}{'.'.join(str(part) for part in error['loc'])}: "
                         f"{error['msg']} (the app's model)" for error in exc.errors())
    return found


def screen(body, *, max_bytes):
    """Problems the app would refuse the whole body for: a credential, an e-mail address or
    an 0x address anywhere (paths only, never the values), or its size."""
    problems = [f"{path}: SENSITIVE_EVIDENCE_REJECTED" for path in sensitive_paths(body)]
    size = len(json.dumps(body, separators=(",", ":")).encode("utf-8"))
    if size > max_bytes:
        problems.append(f"body: {size} bytes, over the route's {max_bytes}")
    return problems
