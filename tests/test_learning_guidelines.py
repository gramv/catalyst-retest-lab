"""MUSE_RESEARCH_GUIDELINES_V6: V5 byte for byte plus the learning loop (package learning-app).

Fixture evidence only: the guidelines text, its document, and report intake on a disposable
PostgreSQL database with a mock Jev (no provider, broker or network contact).
"""

from pathlib import Path

from catalyst_lab.muse_guidelines import (
    MUSE_GUIDELINES,
    MUSE_GUIDELINES_V2,
    MUSE_GUIDELINES_V5,
    MUSE_GUIDELINES_V5_SHA256,
    MUSE_GUIDELINES_V5_VERSION,
    MUSE_GUIDELINES_V6,
    MUSE_GUIDELINES_V6_LEARNING,
    MUSE_GUIDELINES_V6_SHA256,
    MUSE_GUIDELINES_V6_VERSION,
)
from tests.test_execution import er as er  # noqa: F401
from tests.test_execution import pristine_cluster as pristine_cluster  # noqa: F401
from tests.test_research_report_v3 import lab as lab  # noqa: F401
from tests.test_research_report_v3 import pick, report_v3, v3_intake

ROOT = Path(__file__).resolve().parents[1]


def test_guidelines_v6_is_v5_plus_the_learning_section():
    assert MUSE_GUIDELINES_V5_SHA256 == (
        "58f6434da9bb463ef01010533aa935bdbdf18ab215788b1de6def847341b9121")  # Unchanged.
    assert MUSE_GUIDELINES_V6 == MUSE_GUIDELINES_V5 + MUSE_GUIDELINES_V6_LEARNING
    assert MUSE_GUIDELINES_V6_VERSION == "MUSE_RESEARCH_GUIDELINES_V6"
    assert MUSE_GUIDELINES_V6_SHA256 == (
        "075acc02e74afd522b7ad432ac2e893c4a5eeac10fe9f2c0871f5684d819b94d")
    assert all(ord(c) < 128 for c in MUSE_GUIDELINES_V6_LEARNING)
    # The provider prompt of the Muse worker is unchanged (it still produces V2 reports).
    assert MUSE_GUIDELINES == MUSE_GUIDELINES_V2


def test_the_learning_section_states_the_plan_and_the_shared_rules():
    text = " ".join(MUSE_GUIDELINES_V6_LEARNING.split())
    for phrase in (
        # Plan 5: lessons for emphasis, never coverage; recording what was applied.
        "Emphasis, never coverage", "never narrow coverage", "aim for 20 picks every run",
        "never try to change a trading rule", "leave out trades whose post-mortem cause is "
        "MARKET_WIDE or SURPRISE", "why_over_peers", "run notes", "bump agent_version",
        # Plan 5c: the morning outlook covers every coin, graded on its forward window.
        "MARKET_OUTLOOK_V1", "/api/v1/lab/market-outlooks", "exactly one entry for every coin",
        "SKIPPED with a skip_reason", "forward window from receipt to 24 hours later",
        "less than 1.5% is FLAT", "a move of 5% or more", "confidence 0.6 or more",
        "Nasdaq and S&P futures and COIN and MSTR",
        # Plans 5b and 5c: the review, the knowable-before-move rule and the citation rules.
        "POST_MORTEM_V1", "/api/v1/lab/post-mortems", "a stop-out, a win above 1.5R",
        "COIN_NEWS, MARKET_WIDE, NO_NEWS or SURPRISE", "public before move_start_at",
        "published_at is at or before that time", "excerpts cut verbatim",
        "publish times from the page's own metadata", "your own dating was wrong",
        # The coordinator's checklist rules of 2026-09-28.
        "lift is the hit rate divided by the mover share of the universe",
        "10 or more occurrences with a lift of 2.0 or more",
        "over its last 10 occurrences falls below 1.25", "NEWS_TYPE, EVENT, MARKET_FACTOR and "
        "SOURCE_QUALITY", "hit rate of 50% or more", "falls below 40%",
        "may become ACTIVE again under the same rule", "every status change is logged",
        "only order what you check first",
        "never reach Jev and change no trade",
    ):
        assert phrase in text, phrase


def test_the_document_and_the_context_serve_v6():
    document = (ROOT / "docs" / "MUSE-GUIDELINES.md").read_text()
    runtime = document.split("<!-- runtime-guidelines-v6:start -->", 1)[1].split(
        "<!-- runtime-guidelines-v6:end -->", 1)[0]
    assert runtime == MUSE_GUIDELINES_V6_LEARNING
    assert MUSE_GUIDELINES_V6_SHA256 in document
    context = (ROOT / "src" / "catalyst_lab" / "research_context.py").read_text()
    assert '"guidelines_version": MUSE_GUIDELINES_V6_VERSION' in context
    assert '"guidelines_sha256": MUSE_GUIDELINES_V6_SHA256' in context


def test_reports_declaring_v5_or_v6_are_both_accepted(lab):  # noqa: F811
    """As during the V4-to-V5 switch, intake records a declared version as reported."""
    for version, sha in ((MUSE_GUIDELINES_V5_VERSION, MUSE_GUIDELINES_V5_SHA256),
                         (MUSE_GUIDELINES_V6_VERSION, MUSE_GUIDELINES_V6_SHA256)):
        raw = report_v3([pick(0)])
        raw["agent"].update(guidelines_version=version, guidelines_sha256=sha)
        result = lab.cycle.start_report(raw, max_seconds=86400, v3=v3_intake())
        assert result["item_results"][0]["status"] == "ACCEPTED", version
